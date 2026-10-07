"""Unit tests for ``ads_engine.analysis.text``: tokeniser, token Jaccard and term clusters.

They pin the text primitives behind K2 adjacent terms and C3b launch ideas: tokens are
lowercased with stopwords removed, Jaccard similarity and head-token shares are compared as
exact fractions and inclusively, clusters use single linkage, and every result is independent
of input order. Clustering is also checked against a brute-force reference that compares every
pair, so the index pruning is shown to skip no linkable pair. Everything here is pure and
synchronous: no database and no network.
"""

import ast
import doctest
import inspect
import itertools
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError
from decimal import ROUND_DOWN, Decimal, localcontext
from fractions import Fraction
from typing import Any

import pytest

from ads_engine.analysis import text
from ads_engine.analysis.text import (
    STOPWORDS,
    TermCluster,
    adjacent_terms,
    cluster_terms,
    contains_tokens,
    head_tokens,
    jaccard,
    tokenize,
)

HALF = Decimal("0.5")

# The C3b chain from the module docstring: J(A, B) = 2/3 and J(B, C) = 2/4 link A and C
# through B, although J(A, C) = 1/4. D shares no token with any of them.
A = "crm tool"
B = "crm tool pricing"
C = "tool pricing guide"
D = "project management"

# Exact J("crm tool", "crm tool pricing") = 2/3. ``jaccard`` reports it rounded up at the 28th
# digit, so a threshold equal to that rounded value lies just above the exact fraction.
TWO_THIRDS_ROUNDED_UP = Decimal("0.6666666666666666666666666667")
TWO_THIRDS_ROUNDED_DOWN = Decimal("0.6666666666666666666666666666")

# A bare string where a collection of strings belongs; typed ``Any`` so the call sites below
# exercise the runtime guard rather than the static signature.
ONE_STRING: Any = "crm tool pricing"


# --- tokenize and STOPWORDS ----------------------------------------------------------------


def test_tokenize_lowercases_and_removes_stopwords() -> None:
    assert tokenize("Best B2B Attribution Software") == frozenset(
        {"attribution", "b2b", "software"}
    )


def test_tokenize_splits_on_every_punctuation_and_underscore_run_and_keeps_digits() -> None:
    assert tokenize("CRM-tool_pricing (2026)!") == frozenset({"2026", "crm", "pricing", "tool"})
    assert tokenize("crm/tool,pricing;guide\tdemo\nfree...") == frozenset(
        {"crm", "tool", "pricing", "guide", "demo", "free"}
    )


def test_tokenize_keeps_b2b_as_one_lowercase_token() -> None:
    assert tokenize("B2B") == frozenset({"b2b"})


def test_tokenize_counts_a_repeated_word_once() -> None:
    tokens = tokenize("CRM crm Crm tool TOOL")

    assert isinstance(tokens, frozenset)
    assert tokens == frozenset({"crm", "tool"})


@pytest.mark.parametrize(
    "value",
    ["", "   ", "the and for", "Best of the...", "--- !!! ___", "How do I?"],
)
def test_tokenize_gives_an_empty_set_for_text_of_only_stopwords_or_punctuation(
    value: str,
) -> None:
    assert tokenize(value) == frozenset()


def test_tokenize_never_returns_a_stopword() -> None:
    tokens = tokenize(
        "What is the best CRM for a small team near me vs HubSpot, and how do I use it?"
    )

    assert tokens == frozenset({"crm", "small", "team", "hubspot", "use", "it"})
    assert tokens.isdisjoint(STOPWORDS)
    assert tokenize(" ".join(sorted(STOPWORDS))) == frozenset()


def test_tokenize_drops_the_clitic_fragments_apostrophes_leave() -> None:
    assert tokenize("Women's leadership") == frozenset({"women", "leadership"})
    assert tokenize("why don't attribution tools work") == frozenset(
        {"don", "attribution", "tools", "work"}
    )


@pytest.mark.parametrize("word", ["it", "its", "us", "will", "not"])
def test_tokenize_keeps_topic_words_that_generic_stopword_lists_drop(word: str) -> None:
    assert word not in STOPWORDS
    assert tokenize(f"{word.upper()} services") == frozenset({word, "services"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("\uff23\uff32\uff2d Tool", {"crm", "tool"}, id="full-width-letters"),
        pytest.param("\ufb01nance software", {"finance", "software"}, id="ligature"),
        pytest.param("Stra\u00dfe", {"strasse"}, id="casefold"),
        pytest.param("nai\u0308ve bayes", {"na\u00efve", "bayes"}, id="decomposed-accent"),
        pytest.param("Na\u00efve Bayes", {"na\u00efve", "bayes"}, id="accent-stays-in-word"),
    ],
)
def test_tokenize_normalises_unicode_to_its_compatibility_caseless_form(
    raw: str, expected: set[str]
) -> None:
    assert tokenize(raw) == frozenset(expected)


def test_stopwords_are_a_fixed_set_of_lowercase_words_that_tokenize_removes() -> None:
    assert isinstance(STOPWORDS, frozenset)
    assert {"best", "near", "me", "vs", "versus", "s", "t", "the", "and", "for"} <= STOPWORDS
    for word in STOPWORDS:
        assert re.fullmatch(r"[a-z]+", word), word
        assert tokenize(word) == frozenset(), word
        assert tokenize(word.upper()) == frozenset(), word


# --- jaccard ------------------------------------------------------------------------------


def test_jaccard_is_intersection_over_union_at_28_significant_digits() -> None:
    value = jaccard(frozenset({"a", "b"}), frozenset({"b", "c"}))

    assert isinstance(value, Decimal)
    assert value == Decimal(1) / Decimal(3)
    assert value == Decimal("0.3333333333333333333333333333")


def test_jaccard_reports_terminating_fractions_exactly() -> None:
    value = jaccard({"crm", "tool", "pricing"}, {"tool", "pricing", "guide"})

    assert value == HALF
    assert str(value) == "0.5"


def test_jaccard_of_identical_sets_is_one_and_of_disjoint_sets_is_zero() -> None:
    assert jaccard({"crm", "tool"}, frozenset({"tool", "crm"})) == Decimal(1)
    assert jaccard({"crm", "tool"}, {"project", "management"}) == Decimal(0)


def test_jaccard_of_two_empty_sets_is_zero_without_dividing_by_zero() -> None:
    assert jaccard(frozenset(), frozenset()) == Decimal(0)
    assert jaccard(frozenset(), {"crm"}) == Decimal(0)
    assert jaccard({"crm"}, frozenset()) == Decimal(0)


def test_jaccard_is_symmetric() -> None:
    left, right = tokenize(B), tokenize(C)

    assert jaccard(left, right) == jaccard(right, left) == HALF


def test_jaccard_ignores_the_callers_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        value = jaccard(tokenize(A), tokenize(B))

    assert value == TWO_THIRDS_ROUNDED_UP


def test_thresholds_compare_the_exact_fraction_never_the_rounded_jaccard() -> None:
    # Exact J(A, B) = 2/3 lies between the two 28-digit roundings of two thirds, and the
    # reported value is the upper one, so a cut-off tested against it would link A and B.
    assert jaccard(tokenize(A), tokenize(B)) == TWO_THIRDS_ROUNDED_UP

    assert adjacent_terms(A, [B], threshold=TWO_THIRDS_ROUNDED_UP) == []
    assert adjacent_terms(A, [B], threshold=TWO_THIRDS_ROUNDED_DOWN) == [B]
    above = cluster_terms([A, B], threshold=TWO_THIRDS_ROUNDED_UP, min_head_share=HALF)
    below = cluster_terms([A, B], threshold=TWO_THIRDS_ROUNDED_DOWN, min_head_share=HALF)
    assert [cluster.members for cluster in above] == [(A,), (B,)]
    assert [cluster.members for cluster in below] == [(A, B)]

    # "crm" is in exactly 2 of 3 members.
    members = ["crm tool", "crm pricing", "guide"]
    assert head_tokens(members, min_share=TWO_THIRDS_ROUNDED_UP) == frozenset()
    assert head_tokens(members, min_share=TWO_THIRDS_ROUNDED_DOWN) == frozenset({"crm"})


# --- head_tokens --------------------------------------------------------------------------


def test_head_tokens_are_the_tokens_in_at_least_half_of_the_members() -> None:
    assert head_tokens([A, B, C], min_share=HALF) == frozenset({"crm", "tool", "pricing"})


def test_head_tokens_include_a_token_in_exactly_the_minimum_share() -> None:
    members = ["crm tool", "crm pricing", "tool demo", "guide software"]

    assert head_tokens(members, min_share=HALF) == frozenset({"crm", "tool"})
    assert head_tokens(members, min_share=Decimal("0.51")) == frozenset()


def test_head_tokens_count_each_distinct_member_once() -> None:
    # With the repeat counted, "crm" would be in 2 of 3 members and meet 0.6.
    assert head_tokens(["crm tool", "crm tool", "pricing guide"], min_share=Decimal("0.6")) == (
        frozenset()
    )
    assert head_tokens(("crm tool", "crm tool", "pricing guide"), min_share=HALF) == frozenset(
        {"crm", "tool", "pricing", "guide"}
    )


def test_head_tokens_count_a_token_once_per_member() -> None:
    assert head_tokens(["crm crm crm", "tool"], min_share=Decimal("0.6")) == frozenset()


def test_head_tokens_at_a_share_of_one_are_the_tokens_every_member_has() -> None:
    assert head_tokens([A, B, C], min_share=Decimal(1)) == frozenset({"tool"})


def test_head_tokens_of_no_members_or_tokenless_members_are_empty() -> None:
    assert head_tokens([], min_share=HALF) == frozenset()
    assert head_tokens(["the best", "for the"], min_share=HALF) == frozenset()


# --- TermCluster --------------------------------------------------------------------------


def test_term_cluster_sorts_and_deduplicates_members_and_freezes_head_tokens() -> None:
    raw_members: Any = ["crm tool pricing", "crm tool", "crm tool pricing"]
    raw_head: Any = {"tool", "crm"}

    cluster = TermCluster(raw_members, raw_head)

    assert cluster.members == ("crm tool", "crm tool pricing")
    assert isinstance(cluster.head_tokens, frozenset)
    assert cluster.head_tokens == frozenset({"crm", "tool"})
    assert cluster.size == 2
    assert cluster.sorted_head == ("crm", "tool")
    assert cluster.label == "crm tool"


def test_term_clusters_built_differently_compare_and_hash_equal() -> None:
    first = TermCluster((B, A, B), frozenset({"tool", "crm"}))
    second = TermCluster((A, B), frozenset({"crm", "tool"}))

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1


def test_term_cluster_label_is_the_space_joined_sorted_head_tokens() -> None:
    cluster = TermCluster((A, B, C), frozenset({"tool", "pricing", "crm"}))

    assert cluster.sorted_head == ("crm", "pricing", "tool")
    assert cluster.label == "crm pricing tool"


def test_term_cluster_without_head_tokens_has_an_empty_label() -> None:
    cluster = TermCluster(("the best",), frozenset())

    assert cluster.sorted_head == ()
    assert cluster.label == ""


def test_term_cluster_is_immutable() -> None:
    cluster = TermCluster((A,), frozenset({"crm", "tool"}))

    with pytest.raises(FrozenInstanceError):
        cluster.members = (B,)  # type: ignore[misc]


def test_term_cluster_rejects_an_empty_member_list() -> None:
    with pytest.raises(ValueError, match=r"^a term cluster needs at least one member$"):
        TermCluster((), frozenset())


# --- cluster_terms ------------------------------------------------------------------------


def _members(clusters: Sequence[TermCluster]) -> list[tuple[str, ...]]:
    return [cluster.members for cluster in clusters]


def test_cluster_terms_links_a_chain_by_single_linkage_at_an_inclusive_threshold() -> None:
    assert jaccard(tokenize(A), tokenize(C)) == Decimal("0.25")
    assert jaccard(tokenize(B), tokenize(C)) == HALF

    clusters = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)

    assert clusters == [
        TermCluster((A, B, C), frozenset({"crm", "tool", "pricing"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ]
    assert clusters[0].size == 3
    assert clusters[0].label == "crm pricing tool"
    assert clusters[1].label == "management project"


def test_cluster_terms_breaks_the_chain_when_the_link_falls_below_the_threshold() -> None:
    clusters = cluster_terms([A, B, C, D], threshold=Decimal("0.51"), min_head_share=HALF)

    # Equal sizes are ordered by first member: "project management" < "tool pricing guide".
    assert _members(clusters) == [(A, B), (D,), (C,)]


def test_cluster_terms_does_not_join_a_pair_at_jaccard_point_four_under_point_five() -> None:
    left, right = "crm tool pricing guide", "crm tool demo"
    assert jaccard(tokenize(left), tokenize(right)) == Decimal("0.4")

    assert _members(cluster_terms([left, right], threshold=HALF, min_head_share=HALF)) == [
        (right,),
        (left,),
    ]
    assert _members(
        cluster_terms([left, right], threshold=Decimal("0.4"), min_head_share=HALF)
    ) == [(right, left)]


def test_cluster_terms_keeps_unlinked_and_tokenless_terms_as_singletons() -> None:
    clusters = cluster_terms(["the best", "crm", "for the"], threshold=HALF, min_head_share=HALF)

    assert clusters == [
        TermCluster(("crm",), frozenset({"crm"})),
        TermCluster(("for the",), frozenset()),
        TermCluster(("the best",), frozenset()),
    ]


def test_cluster_terms_does_not_link_terms_through_a_token_they_all_share() -> None:
    terms = [f"crm {word}" for word in ("pricing", "demo", "guide", "software", "free")]

    singletons = cluster_terms(terms, threshold=HALF, min_head_share=HALF)
    joined = cluster_terms(terms, threshold=Decimal("0.3"), min_head_share=HALF)

    assert _members(singletons) == [(term,) for term in sorted(terms)]
    assert joined == [TermCluster(tuple(terms), frozenset({"crm"}))]


def test_cluster_terms_deduplicates_identical_terms() -> None:
    clusters = cluster_terms([A, B, A, B], threshold=HALF, min_head_share=HALF)

    # Counted once each, "pricing" is in 1 of 2 members (a head token at 0.5); with the
    # repeats counted it would be in 2 of 4.
    assert clusters == [TermCluster((A, B), frozenset({"crm", "tool", "pricing"}))]
    assert cluster_terms([A, B, A, B], threshold=HALF, min_head_share=Decimal("0.6")) == [
        TermCluster((A, B), frozenset({"crm", "tool"}))
    ]


def test_cluster_terms_of_no_terms_is_empty() -> None:
    assert cluster_terms([], threshold=HALF, min_head_share=HALF) == []


def test_cluster_terms_gives_identical_clusters_in_identical_order_for_every_input_order() -> None:
    expected = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)

    for order in itertools.permutations([A, B, C, D]):
        assert cluster_terms(order, threshold=HALF, min_head_share=HALF) == expected
        assert cluster_terms(list(order), threshold=HALF, min_head_share=HALF) == expected


def test_cluster_terms_is_repeatable() -> None:
    first = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)
    second = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)

    assert first == second
    assert first is not second


# Every combination of one to four words from eight, so token frequencies, set sizes and
# overlaps vary widely across the samples below.
_VOCABULARY = ("crm", "tool", "pricing", "guide", "software", "free", "demo", "b2b")
_GENERATED_TERMS = tuple(
    " ".join(words) for size in range(1, 5) for words in itertools.combinations(_VOCABULARY, size)
)
_SAMPLES = {
    "every-4th": _GENERATED_TERMS[::4],
    "every-5th-from-2": _GENERATED_TERMS[2::5],
    "every-7th-from-1": _GENERATED_TERMS[1::7],
    "every-9th-from-4": _GENERATED_TERMS[4::9],
    "every-13th-from-6-with-tokenless": (*_GENERATED_TERMS[6::13], "the best", "for the"),
    "all-share-crm": tuple(term for term in _GENERATED_TERMS if "crm" in term.split())[::3],
    # Each term beside a reordered, upper-cased spelling of the same tokens, so even the
    # threshold of 1 links distinct strings.
    "with-respelled-twins": tuple(
        spelling
        for term in _GENERATED_TERMS[3::11]
        for spelling in (term, " ".join(reversed(term.upper().split())))
    ),
}
_THRESHOLDS = ("0.25", "0.4", "0.5", "0.6", "0.75", "1")


def _reference_clusters(
    terms: Sequence[str], threshold: Decimal, min_share: Decimal
) -> list[TermCluster]:
    """Single linkage that compares every pair as exact fractions, with no index or pruning."""
    unique = sorted(set(terms))
    tokens = {term: tokenize(term) for term in unique}
    link = Fraction(threshold)
    parent = {term: term for term in unique}

    def root(term: str) -> str:
        while parent[term] != term:
            term = parent[term]
        return term

    for left, right in itertools.combinations(unique, 2):
        union = tokens[left] | tokens[right]
        if union and Fraction(len(tokens[left] & tokens[right]), len(union)) >= link:
            parent[root(left)] = root(right)

    components: dict[str, list[str]] = {}
    for term in unique:
        components.setdefault(root(term), []).append(term)
    clusters = []
    for members in components.values():
        counts = Counter(token for member in members for token in tokens[member])
        head = frozenset(
            token
            for token, count in counts.items()
            if Fraction(count, len(members)) >= Fraction(min_share)
        )
        clusters.append(TermCluster(tuple(members), head))
    return sorted(clusters, key=lambda cluster: (-cluster.size, cluster.members[0]))


@pytest.mark.parametrize("threshold", _THRESHOLDS)
@pytest.mark.parametrize("sample", sorted(_SAMPLES))
def test_cluster_terms_matches_a_brute_force_single_linkage_over_every_pair(
    sample: str, threshold: str
) -> None:
    terms = _SAMPLES[sample]

    clusters = cluster_terms(terms, threshold=Decimal(threshold), min_head_share=HALF)

    assert clusters == _reference_clusters(terms, Decimal(threshold), HALF)
    assert sorted(member for cluster in clusters for member in cluster.members) == sorted(
        set(terms)
    )


def test_cluster_head_tokens_are_what_head_tokens_computes_for_the_members() -> None:
    share = Decimal("0.6")

    clusters = cluster_terms(_GENERATED_TERMS[::5], threshold=HALF, min_head_share=share)

    assert any(cluster.size > 1 for cluster in clusters)
    for cluster in clusters:
        assert cluster.head_tokens == head_tokens(cluster.members, min_share=share)


# --- contains_tokens ----------------------------------------------------------------------


def test_contains_tokens_finds_head_tokens_in_an_ad_group_name() -> None:
    assert contains_tokens(["Brand", "CRM Tool Ad Group"], frozenset({"crm", "tool"})) is True


def test_contains_tokens_is_false_when_no_token_appears_in_any_text() -> None:
    assert contains_tokens(["Brand", "Competitors"], frozenset({"pricing", "guide"})) is False


def test_contains_tokens_needs_only_one_token_in_one_text() -> None:
    texts = ["Brand", "Competitors", "Pricing page visitors"]

    assert contains_tokens(texts, frozenset({"crm", "pricing", "guide"})) is True


def test_contains_tokens_matches_whole_tokens_only() -> None:
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"too"})) is False
    assert contains_tokens(["CRMs and tools"], frozenset({"crm", "tool"})) is False


def test_contains_tokens_normalises_the_tokens_it_looks_for() -> None:
    assert contains_tokens(["crm tool"], frozenset({"CRM"})) is True
    assert contains_tokens(["crm tool"], frozenset({"\uff23\uff32\uff2d"})) is True
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"crm-tool"})) is True


def test_contains_tokens_never_matches_a_stopword() -> None:
    assert contains_tokens(["The Best CRM"], frozenset({"best", "the"})) is False


def test_contains_tokens_is_false_for_no_tokens_or_no_texts() -> None:
    assert contains_tokens(["CRM Tool Ad Group"], frozenset()) is False
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"", "--"})) is False
    assert contains_tokens([], frozenset({"crm"})) is False


def test_contains_tokens_accepts_any_iterable_of_texts() -> None:
    names = (name for name in ["Brand", "CRM Tool Ad Group"])

    assert contains_tokens(names, frozenset({"tool"})) is True


# --- adjacent_terms -----------------------------------------------------------------------


def test_adjacent_terms_returns_candidates_at_or_above_the_threshold() -> None:
    candidates = ["crm tool", "CRM Tool Pricing", "tool pricing guide", "project management"]

    assert adjacent_terms(B, candidates, threshold=HALF) == ["crm tool", "tool pricing guide"]


def test_adjacent_terms_threshold_is_inclusive() -> None:
    assert jaccard(tokenize(B), tokenize(C)) == HALF

    assert adjacent_terms(B, [C], threshold=HALF) == [C]
    assert adjacent_terms(B, [C], threshold=Decimal("0.51")) == []


def test_adjacent_terms_drops_a_candidate_at_jaccard_point_four_under_point_five() -> None:
    term, candidate = "crm tool pricing guide", "crm tool demo"

    assert adjacent_terms(term, [candidate], threshold=HALF) == []
    assert adjacent_terms(term, [candidate], threshold=Decimal("0.4")) == [candidate]


def test_adjacent_terms_excludes_the_term_itself_in_any_case_or_spacing() -> None:
    candidates = [B, "CRM Tool Pricing", "  crm   TOOL\tpricing ", A]

    assert adjacent_terms(B, candidates, threshold=HALF) == [A]


def test_adjacent_terms_returns_a_repeated_candidate_once_in_first_occurrence_order() -> None:
    candidates = [C, A, C, A, D]

    assert adjacent_terms(B, candidates, threshold=HALF) == [C, A]
    assert adjacent_terms(B, tuple(reversed(candidates)), threshold=HALF) == [A, C]


def test_adjacent_terms_are_uncapped_and_unranked() -> None:
    candidates = [f"crm tool {word}" for word in ("a1", "b2", "c3", "d4", "e5", "f6", "g7")]

    assert adjacent_terms(A, candidates, threshold=HALF) == candidates


def test_adjacent_terms_skips_tokenless_candidates() -> None:
    assert adjacent_terms(A, ["the best", "", "for the", C], threshold=Decimal("0.25")) == [C]


def test_adjacent_terms_of_a_term_without_tokens_is_empty() -> None:
    assert adjacent_terms("the best", ["the best", "best of", A], threshold=HALF) == []


# --- validation ---------------------------------------------------------------------------

# "<function>-<parameter>" -> a call passing the value as that parameter.
_THRESHOLD_PARAMETERS: dict[str, Callable[[Any], object]] = {
    "cluster_terms-threshold": lambda value: cluster_terms(
        [A, B], threshold=value, min_head_share=HALF
    ),
    "cluster_terms-min_head_share": lambda value: cluster_terms(
        [A, B], threshold=HALF, min_head_share=value
    ),
    "head_tokens-min_share": lambda value: head_tokens([A, B], min_share=value),
    "adjacent_terms-threshold": lambda value: adjacent_terms(A, [B], threshold=value),
}


def _parameter_name(case: str) -> str:
    return case.partition("-")[2]


@pytest.mark.parametrize("value", [0.5, 1, "0.5", None, Fraction(1, 2), True])
@pytest.mark.parametrize("case", sorted(_THRESHOLD_PARAMETERS))
def test_thresholds_must_be_decimals(case: str, value: Any) -> None:
    message = f"^{_parameter_name(case)} must be a Decimal, got {type(value).__name__}$"

    with pytest.raises(TypeError, match=message):
        _THRESHOLD_PARAMETERS[case](value)


@pytest.mark.parametrize(
    "value",
    ["0", "-0", "-0.5", "1.0001", "2", "NaN", "sNaN", "Infinity", "-Infinity"],
)
@pytest.mark.parametrize("case", sorted(_THRESHOLD_PARAMETERS))
def test_thresholds_must_be_finite_and_in_the_half_open_unit_interval(
    case: str, value: str
) -> None:
    message = (
        rf"^{_parameter_name(case)} must be a finite Decimal in \(0, 1\], got "
        f"{re.escape(str(Decimal(value)))}$"
    )

    with pytest.raises(ValueError, match=message):
        _THRESHOLD_PARAMETERS[case](Decimal(value))


@pytest.mark.parametrize("value", ["1", "1.000", "1E-30", "0.5"])
@pytest.mark.parametrize("case", sorted(_THRESHOLD_PARAMETERS))
def test_thresholds_accept_every_decimal_in_the_half_open_unit_interval(
    case: str, value: str
) -> None:
    _THRESHOLD_PARAMETERS[case](Decimal(value))


def test_threshold_validation_does_not_depend_on_the_input() -> None:
    float_half: Any = 0.5

    with pytest.raises(TypeError, match=r"^threshold must be a Decimal, got float$"):
        cluster_terms([], threshold=float_half, min_head_share=HALF)
    with pytest.raises(
        ValueError, match=r"^threshold must be a finite Decimal in \(0, 1\], got 0$"
    ):
        adjacent_terms("the best", [], threshold=Decimal(0))
    with pytest.raises(
        ValueError, match=r"^min_share must be a finite Decimal in \(0, 1\], got 2$"
    ):
        head_tokens([], min_share=Decimal(2))


@pytest.mark.parametrize(
    ("name", "call"),
    [
        pytest.param(
            "terms",
            lambda: cluster_terms(ONE_STRING, threshold=HALF, min_head_share=HALF),
            id="cluster_terms",
        ),
        pytest.param("members", lambda: head_tokens(ONE_STRING, min_share=HALF), id="head_tokens"),
        pytest.param("members", lambda: TermCluster(ONE_STRING, frozenset()), id="TermCluster"),
        pytest.param(
            "texts",
            lambda: contains_tokens(ONE_STRING, frozenset({"crm"})),
            id="contains_tokens-texts",
        ),
        pytest.param(
            "tokens",
            lambda: contains_tokens(["CRM Tool Ad Group"], ONE_STRING),
            id="contains_tokens-tokens",
        ),
        pytest.param(
            "candidates",
            lambda: adjacent_terms(A, ONE_STRING, threshold=HALF),
            id="adjacent_terms",
        ),
    ],
)
def test_a_single_string_is_refused_where_a_collection_of_strings_belongs(
    name: str, call: Callable[[], object]
) -> None:
    with pytest.raises(
        TypeError, match=f"^{name} must be a collection of strings, not a single string$"
    ):
        call()


# --- internal guards ----------------------------------------------------------------------
# Both guards are unreachable through the public functions, which never compare two empty
# token sets and never union two terms already in one cluster, so they are exercised directly.


def test_exact_threshold_test_never_links_two_empty_token_sets() -> None:
    assert text._meets(frozenset(), frozenset(), (1, 1_000_000)) is False
    assert text._meets({"crm"}, {"crm"}, (1, 1)) is True


def test_union_of_two_terms_already_in_one_cluster_changes_nothing() -> None:
    forest = text._DisjointSet(3)
    forest.union(0, 1)
    forest.union(1, 0)
    forest.union(0, 0)

    assert forest.find(0) == forest.find(1) != forest.find(2)


# --- module contract ----------------------------------------------------------------------


def test_docstring_examples_hold() -> None:
    results = doctest.testmod(text, report=False)

    assert results.attempted > 0
    assert results.failed == 0


def test_module_is_pure_and_imports_only_the_standard_library() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(text))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module)

    assert imported
    for module in imported:
        assert module.partition(".")[0] in sys.stdlib_module_names, module
    assert "logging" not in imported


def test_public_names_are_exactly_the_documented_api() -> None:
    assert sorted(text.__all__) == [
        "STOPWORDS",
        "TermCluster",
        "adjacent_terms",
        "cluster_terms",
        "contains_tokens",
        "head_tokens",
        "jaccard",
        "tokenize",
    ]
