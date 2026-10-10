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
    """K2 and C3b: a term's tokens are its lowercased words, stopwords such as "best" removed."""
    assert tokenize("Best B2B Attribution Software") == frozenset(
        {"attribution", "b2b", "software"}
    )


def test_tokenize_drops_best_and_for_as_stopwords_and_strips_trailing_punctuation() -> None:
    """The stopword list drops "best" and "for", and trailing punctuation leaves no token."""
    # "best" is in STOPWORDS on purpose: it is a search-intent modifier with no topic, and
    # kept it would give unrelated terms ("best crm", "best laptop") a shared token. The
    # module's documented stopword list is the contract, so the expected set leaves "best"
    # out, although a generic stopword list (as a looser reading of K2's "lowercased,
    # stopwords removed" might assume) would keep it.
    assert tokenize("Best CRM Software for Small Business!") == frozenset(
        {"crm", "software", "small", "business"}
    )


def test_tokenize_splits_on_every_punctuation_and_underscore_run_and_keeps_digits() -> None:
    """Every run of punctuation, underscores or whitespace splits tokens, and digits are kept."""
    assert tokenize("CRM-tool_pricing (2026)!") == frozenset({"2026", "crm", "pricing", "tool"})
    assert tokenize("crm/tool,pricing;guide\tdemo\nfree...") == frozenset(
        {"crm", "tool", "pricing", "guide", "demo", "free"}
    )


def test_tokenize_keeps_b2b_as_one_lowercase_token() -> None:
    """A mixed letter-and-digit word such as "B2B" stays one token, lowercased to "b2b"."""
    assert tokenize("B2B") == frozenset({"b2b"})


def test_tokenize_counts_a_repeated_word_once() -> None:
    """Tokens form a frozenset, so a word repeated in any case is one token."""
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
    """Text that is empty or holds only whitespace, stopwords or punctuation has no tokens."""
    assert tokenize(value) == frozenset()


def test_tokenize_never_returns_a_stopword() -> None:
    """No token is ever a stopword, and text made of every stopword has no tokens."""
    tokens = tokenize(
        "What is the best CRM for a small team near me vs HubSpot, and how do I use it?"
    )

    assert tokens == frozenset({"crm", "small", "team", "hubspot", "use", "it"})
    assert tokens.isdisjoint(STOPWORDS)
    assert tokenize(" ".join(sorted(STOPWORDS))) == frozenset()


def test_tokenize_drops_the_clitic_fragments_apostrophes_leave() -> None:
    """Apostrophes split words, and the clitic fragments "s" and "t" they leave are stopwords."""
    assert tokenize("Women's leadership") == frozenset({"women", "leadership"})
    assert tokenize("why don't attribution tools work") == frozenset(
        {"don", "attribution", "tools", "work"}
    )


@pytest.mark.parametrize("word", ["it", "its", "us", "will", "not"])
def test_tokenize_keeps_topic_words_that_generic_stopword_lists_drop(word: str) -> None:
    """Words generic stopword lists drop, such as "it", "us" and "not", stay lowercased tokens."""
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
    """Tokens are the Unicode compatibility-caseless form of the text, accents kept in words.

    Full-width letters and ligatures fold to plain letters, the sharp s casefolds to "ss" and a
    decomposed accent composes, so every spelling of a word gives the same token.
    """
    assert tokenize(raw) == frozenset(expected)


def test_stopwords_are_a_fixed_set_of_lowercase_words_that_tokenize_removes() -> None:
    """``STOPWORDS`` is a frozenset of lowercase ASCII words, each dropped in lower or upper case.

    It includes the search-intent modifiers "best", "near", "me", "vs" and "versus", the clitic
    fragments "s" and "t", and the function words "the", "and" and "for".
    """
    assert isinstance(STOPWORDS, frozenset)
    assert {"best", "near", "me", "vs", "versus", "s", "t", "the", "and", "for"} <= STOPWORDS
    for word in STOPWORDS:
        assert re.fullmatch(r"[a-z]+", word), word
        assert tokenize(word) == frozenset(), word
        assert tokenize(word.upper()) == frozenset(), word


# --- jaccard ------------------------------------------------------------------------------


def test_jaccard_is_intersection_over_union_at_28_significant_digits() -> None:
    """Jaccard is a Decimal of intersection over union, rounded at 28 significant digits."""
    value = jaccard(frozenset({"a", "b"}), frozenset({"b", "c"}))

    assert isinstance(value, Decimal)
    assert value == Decimal(1) / Decimal(3)
    assert value == Decimal("0.3333333333333333333333333333")


def test_jaccard_reports_terminating_fractions_exactly() -> None:
    """A terminating fraction such as 2/4 is reported exactly and prints as "0.5"."""
    value = jaccard({"crm", "tool", "pricing"}, {"tool", "pricing", "guide"})

    assert value == HALF
    assert str(value) == "0.5"


def test_jaccard_of_identical_sets_is_one_and_of_disjoint_sets_is_zero() -> None:
    """Identical token sets have Jaccard 1 and disjoint token sets have Jaccard 0."""
    assert jaccard({"crm", "tool"}, frozenset({"tool", "crm"})) == Decimal(1)
    assert jaccard({"crm", "tool"}, {"project", "management"}) == Decimal(0)


def test_jaccard_of_two_empty_sets_is_zero_without_dividing_by_zero() -> None:
    """Jaccard is 0 when either set or both are empty, with no division by zero."""
    assert jaccard(frozenset(), frozenset()) == Decimal(0)
    assert jaccard(frozenset(), {"crm"}) == Decimal(0)
    assert jaccard({"crm"}, frozenset()) == Decimal(0)


def test_jaccard_is_symmetric() -> None:
    """Jaccard is symmetric in its two arguments."""
    left, right = tokenize(B), tokenize(C)

    assert jaccard(left, right) == jaccard(right, left) == HALF


def test_jaccard_ignores_the_callers_decimal_context() -> None:
    """The caller's decimal context changes neither the precision nor the rounding of Jaccard."""
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        value = jaccard(tokenize(A), tokenize(B))

    assert value == TWO_THIRDS_ROUNDED_UP


def test_thresholds_compare_the_exact_fraction_never_the_rounded_jaccard() -> None:
    """K2 and C3b thresholds compare the exact Jaccard fraction, never its rounded value.

    Exact 2/3 lies between its two 28-digit roundings, and ``jaccard`` reports the upper one. At
    a threshold equal to that value no adjacent term, cluster link or 2-of-3 head token
    qualifies; at the lower rounding all three do.
    """
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
    """C3b: head tokens are the tokens in at least the minimum share of the member terms."""
    assert head_tokens([A, B, C], min_share=HALF) == frozenset({"crm", "tool", "pricing"})


def test_head_tokens_include_a_token_in_exactly_the_minimum_share() -> None:
    """C3b: the head-token share is inclusive, so a token in exactly 2 of 4 members meets 0.5."""
    members = ["crm tool", "crm pricing", "tool demo", "guide software"]

    assert head_tokens(members, min_share=HALF) == frozenset({"crm", "tool"})
    assert head_tokens(members, min_share=Decimal("0.51")) == frozenset()


def test_head_tokens_count_each_distinct_member_once() -> None:
    """C3b: a repeated member string counts once in head-token shares, in a list or a tuple."""
    # With the repeat counted, "crm" would be in 2 of 3 members and meet 0.6.
    assert head_tokens(["crm tool", "crm tool", "pricing guide"], min_share=Decimal("0.6")) == (
        frozenset()
    )
    assert head_tokens(("crm tool", "crm tool", "pricing guide"), min_share=HALF) == frozenset(
        {"crm", "tool", "pricing", "guide"}
    )


def test_head_tokens_count_a_token_once_per_member() -> None:
    """C3b: a token repeated within one member counts once toward its head-token share."""
    assert head_tokens(["crm crm crm", "tool"], min_share=Decimal("0.6")) == frozenset()


def test_head_tokens_at_a_share_of_one_are_the_tokens_every_member_has() -> None:
    """C3b: at a share of 1 the head tokens are exactly the tokens every member contains."""
    assert head_tokens([A, B, C], min_share=Decimal(1)) == frozenset({"tool"})


def test_head_tokens_of_no_members_or_tokenless_members_are_empty() -> None:
    """C3b: no members, or members of only stopwords, give no head tokens."""
    assert head_tokens([], min_share=HALF) == frozenset()
    assert head_tokens(["the best", "for the"], min_share=HALF) == frozenset()


# --- TermCluster --------------------------------------------------------------------------


def test_term_cluster_sorts_and_deduplicates_members_and_freezes_head_tokens() -> None:
    """A ``TermCluster`` normalises its members to a sorted tuple of distinct terms.

    Its head tokens become a frozenset, ``size`` counts the distinct members, and
    ``sorted_head`` and ``label`` read the normalised fields.
    """
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
    """Clusters built from the same members in any order or repetition compare and hash equal."""
    first = TermCluster((B, A, B), frozenset({"tool", "crm"}))
    second = TermCluster((A, B), frozenset({"crm", "tool"}))

    assert first == second
    assert hash(first) == hash(second)
    assert len({first, second}) == 1


def test_term_cluster_label_lists_the_head_tokens_in_the_members_word_order() -> None:
    """C3b: ``label`` follows the members' word order while ``sorted_head`` stays alphabetical."""
    # "tool pricing guide" lacks "crm", so it votes only on the (tool, pricing) pair; the
    # points are crm 3, tool 2, pricing 0. ``sorted_head`` stays alphabetical.
    cluster = TermCluster((A, B, C), frozenset({"tool", "pricing", "crm"}))

    assert cluster.sorted_head == ("crm", "pricing", "tool")
    assert cluster.label == "crm tool pricing"


@pytest.mark.parametrize(
    ("members", "head", "label", "alphabetical"),
    [
        pytest.param(
            ("revenue forecasting tool", "b2b revenue forecasting", "revenue forecasting software"),
            {"revenue", "forecasting"},
            "revenue forecasting",
            ("forecasting", "revenue"),
            id="revenue-forecasting",
        ),
        pytest.param(
            ("hubspot alternative", "hubspot alternative free", "cheap hubspot alternative"),
            {"hubspot", "alternative"},
            "hubspot alternative",
            ("alternative", "hubspot"),
            id="hubspot-alternative",
        ),
        pytest.param(
            ("revenue intelligence", "revenue intelligence platform", "b2b revenue intelligence"),
            {"revenue", "intelligence"},
            "revenue intelligence",
            ("intelligence", "revenue"),
            id="revenue-intelligence",
        ),
        pytest.param(
            ("crème brûlée planner", "crème brûlée planner app", "Crème Brûlée Planner Pro"),
            {"crème", "brûlée", "planner"},
            "crème brûlée planner",
            ("brûlée", "crème", "planner"),
            id="accented",
        ),
        pytest.param(
            ("project management",),
            {"project", "management"},
            "project management",
            ("management", "project"),
            id="single-member",
        ),
    ],
)
def test_term_cluster_label_follows_how_the_terms_are_searched(
    members: tuple[str, ...], head: set[str], label: str, alphabetical: tuple[str, ...]
) -> None:
    """C3b: labels read as the terms are searched, unlike the alphabetical ``sorted_head``."""
    cluster = TermCluster(members, frozenset(head))

    assert cluster.label == label
    assert cluster.sorted_head == alphabetical


def test_term_cluster_label_reads_each_member_with_tokenize_rules() -> None:
    """C3b: the label reads each member's word order by the rules of ``tokenize``.

    Stopwords, punctuation and case move no head token, and a repeated word keeps its first
    position.
    """
    # Stopwords, punctuation and case do not move a head token: "the" and "for" are dropped,
    # "Revenue-Forecasting" splits as tokenize splits it, and a repeated word keeps its first
    # position, so every member places "revenue" before "forecasting".
    members = (
        "the Revenue-Forecasting tool",
        "best software for revenue forecasting",
        "revenue forecasting revenue",
    )
    cluster = TermCluster(members, frozenset({"revenue", "forecasting"}))

    assert cluster.label == "revenue forecasting"


def test_term_cluster_label_breaks_a_tie_in_points_alphabetically() -> None:
    """C3b: head tokens tied on word-order points are listed alphabetically.

    A head token that no member contains earns no point and ties with the last-placed token.
    """
    # One member puts "crm" first and the other "tool" first: one point each.
    tied = TermCluster(("tool crm", "crm tool"), frozenset({"crm", "tool"}))
    # A head token no member contains earns no point and ties with the last-placed token.
    absent = TermCluster(("pricing guide",), frozenset({"pricing", "demo", "guide"}))

    assert tied.label == "crm tool"
    assert absent.label == "pricing demo guide"


def test_term_cluster_label_ranks_by_points_across_members_lacking_some_head_tokens() -> None:
    """C3b: points from word order, not the alphabet, rank head tokens that some members lack."""
    # "zeta" leads in two members, "alpha" in one, and the third member lacks "zeta": points
    # are zeta 2, alpha 1, beta 0, so word order beats alphabetical order.
    cluster = TermCluster(
        ("zeta alpha", "zeta beta", "alpha beta"), frozenset({"alpha", "beta", "zeta"})
    )

    assert cluster.label == "zeta alpha beta"
    assert cluster.sorted_head == ("alpha", "beta", "zeta")


def test_term_cluster_label_is_independent_of_the_order_members_are_given_in() -> None:
    """C3b: every order of the same members gives the same label."""
    labels = {
        TermCluster(order, frozenset({"tool", "pricing", "crm"})).label
        for order in itertools.permutations([A, B, C])
    }

    assert labels == {"crm tool pricing"}


@pytest.mark.parametrize(
    "value",
    [
        "Best B2B Attribution Software",
        "CRM-tool_pricing (2026)!",
        "CRM crm Crm tool TOOL",
        "the and for",
        "",
        "\uff23\uff32\uff2d Tool",
        "nai\u0308ve bayes",
        "Women's leadership",
    ],
)
def test_ordered_tokens_are_the_tokenize_set_in_first_use_order(value: str) -> None:
    """The word-order tokens the label reads are exactly the tokens of ``tokenize``, each once."""
    ordered = text._ordered_tokens(value)

    assert len(ordered) == len(set(ordered))
    assert frozenset(ordered) == tokenize(value)


def test_ordered_tokens_keep_the_word_order_and_the_first_position_of_a_repeat() -> None:
    """Ordered tokens follow the text's word order, and a repeated word keeps its first position."""
    assert text._ordered_tokens("Tool for CRM, tool pricing") == ("tool", "crm", "pricing")


def test_term_cluster_without_head_tokens_has_an_empty_label() -> None:
    """A cluster without head tokens has an empty ``sorted_head`` and an empty label."""
    cluster = TermCluster(("the best",), frozenset())

    assert cluster.sorted_head == ()
    assert cluster.label == ""


def test_term_cluster_is_immutable() -> None:
    """A ``TermCluster`` is frozen: assigning to a field raises ``FrozenInstanceError``."""
    cluster = TermCluster((A,), frozenset({"crm", "tool"}))

    with pytest.raises(FrozenInstanceError):
        cluster.members = (B,)  # type: ignore[misc]


def test_term_cluster_rejects_an_empty_member_list() -> None:
    """Building a ``TermCluster`` with no members raises ``ValueError``."""
    with pytest.raises(ValueError, match=r"^a term cluster needs at least one member$"):
        TermCluster((), frozenset())


# --- cluster_terms ------------------------------------------------------------------------


def _members(clusters: Sequence[TermCluster]) -> list[tuple[str, ...]]:
    return [cluster.members for cluster in clusters]


def test_cluster_terms_links_a_chain_by_single_linkage_at_an_inclusive_threshold() -> None:
    """C3b: single linkage at an inclusive 0.5 joins A and C through B, although J(A, C) = 0.25.

    The unrelated D stays a singleton, and each cluster carries its head tokens and label.
    """
    assert jaccard(tokenize(A), tokenize(C)) == Decimal("0.25")
    assert jaccard(tokenize(B), tokenize(C)) == HALF

    clusters = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)

    assert clusters == [
        TermCluster((A, B, C), frozenset({"crm", "tool", "pricing"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ]
    assert clusters[0].size == 3
    assert clusters[0].label == "crm tool pricing"
    assert clusters[1].label == "project management"


def test_cluster_terms_breaks_the_chain_when_the_link_falls_below_the_threshold() -> None:
    """C3b: at 0.51 the chain breaks at its 0.5 link, and equal sizes sort by first member."""
    clusters = cluster_terms([A, B, C, D], threshold=Decimal("0.51"), min_head_share=HALF)

    # Equal sizes are ordered by first member: "project management" < "tool pricing guide".
    assert _members(clusters) == [(A, B), (D,), (C,)]


def test_cluster_terms_does_not_join_a_pair_at_jaccard_point_four_under_point_five() -> None:
    """C3b: a pair at Jaccard 0.4 stays apart at threshold 0.5 and joins at threshold 0.4."""
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
    """C3b: unlinked and tokenless terms are singletons, and tokenless ones have no head token."""
    clusters = cluster_terms(["the best", "crm", "for the"], threshold=HALF, min_head_share=HALF)

    assert clusters == [
        TermCluster(("crm",), frozenset({"crm"})),
        TermCluster(("for the",), frozenset()),
        TermCluster(("the best",), frozenset()),
    ]


def test_cluster_terms_does_not_link_terms_through_a_token_they_all_share() -> None:
    """C3b: a token all five terms share (pairwise J = 1/3) links none at 0.5 but all at 0.3."""
    terms = [f"crm {word}" for word in ("pricing", "demo", "guide", "software", "free")]

    singletons = cluster_terms(terms, threshold=HALF, min_head_share=HALF)
    joined = cluster_terms(terms, threshold=Decimal("0.3"), min_head_share=HALF)

    assert _members(singletons) == [(term,) for term in sorted(terms)]
    assert joined == [TermCluster(tuple(terms), frozenset({"crm"}))]


def test_cluster_terms_deduplicates_identical_terms() -> None:
    """C3b: identical terms are clustered, and counted in head-token shares, only once."""
    clusters = cluster_terms([A, B, A, B], threshold=HALF, min_head_share=HALF)

    # Counted once each, "pricing" is in 1 of 2 members (a head token at 0.5); with the
    # repeats counted it would be in 2 of 4.
    assert clusters == [TermCluster((A, B), frozenset({"crm", "tool", "pricing"}))]
    assert cluster_terms([A, B, A, B], threshold=HALF, min_head_share=Decimal("0.6")) == [
        TermCluster((A, B), frozenset({"crm", "tool"}))
    ]


def test_cluster_terms_of_no_terms_is_empty() -> None:
    """C3b: clustering no terms gives no clusters."""
    assert cluster_terms([], threshold=HALF, min_head_share=HALF) == []


def test_cluster_terms_gives_identical_clusters_in_identical_order_for_every_input_order() -> None:
    """C3b: every ordering of the terms, as a tuple or list, gives the same clusters in order."""
    expected = cluster_terms([A, B, C, D], threshold=HALF, min_head_share=HALF)

    for order in itertools.permutations([A, B, C, D]):
        assert cluster_terms(order, threshold=HALF, min_head_share=HALF) == expected
        assert cluster_terms(list(order), threshold=HALF, min_head_share=HALF) == expected


def test_cluster_terms_is_repeatable() -> None:
    """C3b: two calls on the same terms return equal clusters in distinct lists."""
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
    """C3b: the indexed clustering equals a brute-force single linkage over every pair.

    Over varied samples and thresholds from 0.25 to 1, index pruning skips no linkable pair,
    and each distinct term lands in exactly one cluster.
    """
    terms = _SAMPLES[sample]

    clusters = cluster_terms(terms, threshold=Decimal(threshold), min_head_share=HALF)

    assert clusters == _reference_clusters(terms, Decimal(threshold), HALF)
    assert sorted(member for cluster in clusters for member in cluster.members) == sorted(
        set(terms)
    )


def test_cluster_head_tokens_are_what_head_tokens_computes_for_the_members() -> None:
    """C3b: each cluster's head tokens equal ``head_tokens`` over its members at the same share."""
    share = Decimal("0.6")

    clusters = cluster_terms(_GENERATED_TERMS[::5], threshold=HALF, min_head_share=share)

    assert any(cluster.size > 1 for cluster in clusters)
    for cluster in clusters:
        assert cluster.head_tokens == head_tokens(cluster.members, min_share=share)


# --- contains_tokens ----------------------------------------------------------------------


def test_contains_tokens_finds_head_tokens_in_an_ad_group_name() -> None:
    """C3b: head tokens present in an ad group name are found, whatever its case."""
    assert contains_tokens(["Brand", "CRM Tool Ad Group"], frozenset({"crm", "tool"})) is True


def test_contains_tokens_is_false_when_no_token_appears_in_any_text() -> None:
    """C3b: the result is False when no token appears in any of the texts."""
    assert contains_tokens(["Brand", "Competitors"], frozenset({"pricing", "guide"})) is False


def test_contains_tokens_needs_only_one_token_in_one_text() -> None:
    """C3b: any head token in any keyword, ad group or campaign name means the theme is covered.

    The semantics are *any token in any text*, not all tokens: one head token found in one
    name is enough for ``contains_tokens`` to return True, and C3b fires only on False.
    """
    texts = ["Brand", "Competitors", "Pricing page visitors"]

    assert contains_tokens(texts, frozenset({"crm", "pricing", "guide"})) is True


def test_contains_tokens_matches_whole_tokens_only() -> None:
    """C3b: matching is by whole token, so neither a prefix nor a plural form matches."""
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"too"})) is False
    assert contains_tokens(["CRMs and tools"], frozenset({"crm", "tool"})) is False


def test_contains_tokens_normalises_the_tokens_it_looks_for() -> None:
    """C3b: sought tokens are normalised like text, whatever their case, width or hyphens."""
    assert contains_tokens(["crm tool"], frozenset({"CRM"})) is True
    assert contains_tokens(["crm tool"], frozenset({"\uff23\uff32\uff2d"})) is True
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"crm-tool"})) is True


def test_contains_tokens_never_matches_a_stopword() -> None:
    """C3b: a stopword is never found, even in a text that contains it."""
    assert contains_tokens(["The Best CRM"], frozenset({"best", "the"})) is False


def test_contains_tokens_is_false_for_no_tokens_or_no_texts() -> None:
    """C3b: no tokens, empty or punctuation-only tokens, or no texts give False."""
    assert contains_tokens(["CRM Tool Ad Group"], frozenset()) is False
    assert contains_tokens(["CRM Tool Ad Group"], frozenset({"", "--"})) is False
    assert contains_tokens([], frozenset({"crm"})) is False


def test_contains_tokens_accepts_any_iterable_of_texts() -> None:
    """C3b: texts may be any iterable, a one-shot generator included."""
    names = (name for name in ["Brand", "CRM Tool Ad Group"])

    assert contains_tokens(names, frozenset({"tool"})) is True


# --- adjacent_terms -----------------------------------------------------------------------


def test_adjacent_terms_returns_candidates_at_or_above_the_threshold() -> None:
    """K2: adjacent terms are the other candidates at or above the Jaccard threshold."""
    candidates = ["crm tool", "CRM Tool Pricing", "tool pricing guide", "project management"]

    assert adjacent_terms(B, candidates, threshold=HALF) == ["crm tool", "tool pricing guide"]


def test_adjacent_terms_threshold_is_inclusive() -> None:
    """K2: a candidate at Jaccard exactly 0.5 is adjacent at threshold 0.5 but not at 0.51."""
    assert jaccard(tokenize(B), tokenize(C)) == HALF

    assert adjacent_terms(B, [C], threshold=HALF) == [C]
    assert adjacent_terms(B, [C], threshold=Decimal("0.51")) == []


def test_adjacent_terms_drops_a_candidate_at_jaccard_point_four_under_point_five() -> None:
    """K2: a candidate at Jaccard 0.4 is dropped at threshold 0.5 and kept at threshold 0.4."""
    term, candidate = "crm tool pricing guide", "crm tool demo"

    assert adjacent_terms(term, [candidate], threshold=HALF) == []
    assert adjacent_terms(term, [candidate], threshold=Decimal("0.4")) == [candidate]


def test_adjacent_terms_excludes_the_term_itself_in_any_case_or_spacing() -> None:
    """K2: the term itself is never adjacent, however a candidate cases or spaces it."""
    candidates = [B, "CRM Tool Pricing", "  crm   TOOL\tpricing ", A]

    assert adjacent_terms(B, candidates, threshold=HALF) == [A]


def test_adjacent_terms_returns_a_repeated_candidate_once_in_first_occurrence_order() -> None:
    """K2: a repeated candidate is returned once, in the order candidates first occur."""
    candidates = [C, A, C, A, D]

    assert adjacent_terms(B, candidates, threshold=HALF) == [C, A]
    assert adjacent_terms(B, tuple(reversed(candidates)), threshold=HALF) == [A, C]


def test_adjacent_terms_are_uncapped_and_unranked() -> None:
    """K2: all seven matching candidates are returned in input order, with no cap or ranking."""
    candidates = [f"crm tool {word}" for word in ("a1", "b2", "c3", "d4", "e5", "f6", "g7")]

    assert adjacent_terms(A, candidates, threshold=HALF) == candidates


def test_adjacent_terms_skips_tokenless_candidates() -> None:
    """K2: an empty or stopword-only candidate is never adjacent, even at a 0.25 threshold."""
    assert adjacent_terms(A, ["the best", "", "for the", C], threshold=Decimal("0.25")) == [C]


def test_adjacent_terms_of_a_term_without_tokens_is_empty() -> None:
    """K2: a term of only stopwords has no adjacent terms."""
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
    """A K2 or C3b threshold that is not a Decimal raises TypeError naming it and its type."""
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
    """A K2 or C3b threshold that is not finite or lies outside (0, 1] raises ValueError.

    Zero, negative zero, negatives, values above 1, NaN, sNaN and both infinities are refused,
    and the message names the parameter and the value.
    """
    message = (
        rf"^{_parameter_name(case)} must be a finite Decimal in \(0, 1\], got "
        f"{re.escape(str(Decimal(value)))}$"
    )

    with pytest.raises(ValueError, match=message):
        _THRESHOLD_PARAMETERS[case](Decimal(value))


# Inputs on which the distinct accepted thresholds 1, 1E-30 and 0.5 give three different
# outputs. TOOL_CRM respells A, so J(A, TOOL_CRM) = 1 is the only pair a threshold of 1 links.
# J(B, C) and J(A, "crm") are exactly 1/2. CRM_DEMO shares one token with A, TOOL_CRM and B
# (J = 1/3, 1/3 and 1/4) and none with C, so only the tiny threshold links it. D shares no
# token with any other term, so no threshold links it.
TOOL_CRM = "tool crm"
CRM_DEMO = "crm demo"
_THRESHOLD_TERMS = (A, TOOL_CRM, B, C, D, CRM_DEMO)
# The cluster those terms form at 0.5: "tool" is in all 4 members, "crm" in 3, "pricing" in
# exactly 2 and "guide" in 1.
_HALF_CLUSTER = (A, B, TOOL_CRM, C)
_ADJACENT_CANDIDATES = (TOOL_CRM, B, "crm", C, D)

# "<function>-<parameter>" -> a call passing the value as that parameter over the inputs above.
_ACCEPTED_THRESHOLD_CALLS: dict[str, Callable[[Decimal], object]] = {
    "cluster_terms-threshold": lambda value: cluster_terms(
        _THRESHOLD_TERMS, threshold=value, min_head_share=HALF
    ),
    "cluster_terms-min_head_share": lambda value: cluster_terms(
        _THRESHOLD_TERMS, threshold=HALF, min_head_share=value
    ),
    "head_tokens-min_share": lambda value: head_tokens(_HALF_CLUSTER, min_share=value),
    "adjacent_terms-threshold": lambda value: adjacent_terms(
        A, _ADJACENT_CANDIDATES, threshold=value
    ),
}

# (case, value) -> the exact output of that call at that threshold.
_ACCEPTED_THRESHOLD_OUTPUTS: dict[tuple[str, str], object] = {
    ("cluster_terms-threshold", "1"): [
        TermCluster((A, TOOL_CRM), frozenset({"crm", "tool"})),
        TermCluster((CRM_DEMO,), frozenset({"crm", "demo"})),
        TermCluster((B,), frozenset({"crm", "tool", "pricing"})),
        TermCluster((D,), frozenset({"project", "management"})),
        TermCluster((C,), frozenset({"tool", "pricing", "guide"})),
    ],
    ("cluster_terms-threshold", "1E-30"): [
        TermCluster((CRM_DEMO, A, B, TOOL_CRM, C), frozenset({"crm", "tool"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ],
    ("cluster_terms-threshold", "0.5"): [
        TermCluster(_HALF_CLUSTER, frozenset({"crm", "tool", "pricing"})),
        TermCluster((CRM_DEMO,), frozenset({"crm", "demo"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ],
    ("cluster_terms-min_head_share", "1"): [
        TermCluster(_HALF_CLUSTER, frozenset({"tool"})),
        TermCluster((CRM_DEMO,), frozenset({"crm", "demo"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ],
    ("cluster_terms-min_head_share", "1E-30"): [
        TermCluster(_HALF_CLUSTER, frozenset({"crm", "tool", "pricing", "guide"})),
        TermCluster((CRM_DEMO,), frozenset({"crm", "demo"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ],
    ("cluster_terms-min_head_share", "0.5"): [
        TermCluster(_HALF_CLUSTER, frozenset({"crm", "tool", "pricing"})),
        TermCluster((CRM_DEMO,), frozenset({"crm", "demo"})),
        TermCluster((D,), frozenset({"project", "management"})),
    ],
    ("head_tokens-min_share", "1"): frozenset({"tool"}),
    ("head_tokens-min_share", "1E-30"): frozenset({"crm", "tool", "pricing", "guide"}),
    ("head_tokens-min_share", "0.5"): frozenset({"crm", "tool", "pricing"}),
    ("adjacent_terms-threshold", "1"): [TOOL_CRM],
    ("adjacent_terms-threshold", "1E-30"): [TOOL_CRM, B, "crm", C],
    ("adjacent_terms-threshold", "0.5"): [TOOL_CRM, B, "crm"],
}
# "1.000" is the threshold 1 written with trailing zeros, so it is held to the output of "1".
_ACCEPTED_THRESHOLD_OUTPUTS |= {
    (case, "1.000"): _ACCEPTED_THRESHOLD_OUTPUTS[case, "1"] for case in _ACCEPTED_THRESHOLD_CALLS
}


@pytest.mark.parametrize("value", ["1", "1.000", "1E-30", "0.5"])
@pytest.mark.parametrize("case", sorted(_THRESHOLD_PARAMETERS))
def test_thresholds_accept_every_decimal_in_the_half_open_unit_interval_and_apply_it_exactly(
    case: str, value: str
) -> None:
    """Every accepted C3b and K2 threshold is applied inclusively, at its exact value.

    A threshold of 1 links only terms with identical token sets and keeps only the head tokens
    every member has; ``1.000`` is the same threshold and gives the same output. At 0.5 a pair
    at Jaccard exactly 1/2 links and a token in exactly half the members is a head token. As a
    link threshold the tiny positive 1E-30 links any two terms that share a token, yet no two
    that share none; as a head share it makes every member token a head token.
    """
    output = _ACCEPTED_THRESHOLD_CALLS[case](Decimal(value))

    assert output == _ACCEPTED_THRESHOLD_OUTPUTS[case, value]


def test_threshold_validation_does_not_depend_on_the_input() -> None:
    """Invalid thresholds are refused even for empty input or a term with no tokens."""
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
    """A bare string given for a collection of strings raises TypeError naming the parameter."""
    with pytest.raises(
        TypeError, match=f"^{name} must be a collection of strings, not a single string$"
    ):
        call()


# --- internal guards ----------------------------------------------------------------------
# Both guards are unreachable through the public functions, which never compare two empty
# token sets and never union two terms already in one cluster, so they are exercised directly.


def test_exact_threshold_test_never_links_two_empty_token_sets() -> None:
    """Two empty token sets never meet even a tiny threshold, while equal non-empty sets meet 1."""
    assert text._meets(frozenset(), frozenset(), (1, 1_000_000)) is False
    assert text._meets({"crm"}, {"crm"}, (1, 1)) is True


def test_union_of_two_terms_already_in_one_cluster_changes_nothing() -> None:
    """A union of terms already in one cluster, or of a term with itself, changes nothing."""
    forest = text._DisjointSet(3)
    forest.union(0, 1)
    forest.union(1, 0)
    forest.union(0, 0)

    assert forest.find(0) == forest.find(1) != forest.find(2)


# --- module contract ----------------------------------------------------------------------


def test_docstring_examples_hold() -> None:
    """The doctest examples in the text module's docstrings exist and all pass."""
    results = doctest.testmod(text, report=False)

    assert results.attempted > 0
    assert results.failed == 0


def test_module_is_pure_and_imports_only_the_standard_library() -> None:
    """The text module imports only the standard library, by absolute import, and never logging."""
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
    """The text module exports exactly its eight public names in ``__all__``."""
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
