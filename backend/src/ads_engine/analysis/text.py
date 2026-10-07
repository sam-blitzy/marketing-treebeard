"""Search-term text analysis: tokeniser, token Jaccard similarity and single-linkage clusters.

Google's Keyword Planner (``KeywordPlanIdeaService``) is not reachable through the n8n
webhook, so every related term the product suggests is derived from the account's own
search-term report and labelled "from your search-term report". This module holds the pure
text primitives those rules share:

* K2 expansion candidates list adjacent terms: other window search terms whose token
  Jaccard similarity with the converting term is at least ``k2.adjacent_jaccard``
  (:func:`adjacent_terms`).
* C3b launch ideas group converting search terms by single linkage at ``c3b.jaccard``
  (:func:`cluster_terms`) and name each cluster by its head tokens, the tokens present in at
  least ``c3b.head_token_share`` of its members (:func:`head_tokens`). The idea is raised
  only when no head token appears in any keyword text, ad group name or campaign name
  (:func:`contains_tokens`).

Design constraints:

* Pure and stdlib-only: no I/O, no logging, no third-party packages.
* Thresholds are parameters. Callers pass the configured ``Decimal`` values from
  ``Thresholds``; nothing here hard-codes a similarity cut-off or a head-token share.
* Exact: every "Jaccard >= threshold" and "share >= minimum" test is an exact rational
  comparison of cross-multiplied integers, so decimal rounding can neither create nor hide
  an edge or a head token.
* Deterministic: no result depends on set or dict iteration order, so the same input gives
  the same output under every ``PYTHONHASHSEED`` and analysis runs stay reproducible.

Example:
    >>> from decimal import Decimal
    >>> sorted(tokenize("Best B2B Attribution Software"))
    ['attribution', 'b2b', 'software']
    >>> jaccard(frozenset({"crm", "software"}), frozenset({"crm", "tool"}))
    Decimal('0.3333333333333333333333333333')
    >>> clusters = cluster_terms(
    ...     ["crm tool", "crm tool pricing", "tool pricing guide", "project management"],
    ...     threshold=Decimal("0.5"),
    ...     min_head_share=Decimal("0.5"),
    ... )
    >>> [cluster.members for cluster in clusters]
    [('crm tool', 'crm tool pricing', 'tool pricing guide'), ('project management',)]
    >>> clusters[0].label
    'crm pricing tool'
    >>> contains_tokens(["Brand", "CRM Tool Ad Group"], clusters[0].head_tokens)
    True
"""

import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal
from typing import Final

__all__ = [
    "STOPWORDS",
    "TermCluster",
    "adjacent_terms",
    "cluster_terms",
    "contains_tokens",
    "head_tokens",
    "jaccard",
    "tokenize",
]

# A fixed list, never loaded from a corpus or a third-party package, so token sets (and with
# them every adjacent term, cluster and head token) are reproducible across runs, hosts and
# library upgrades. Sorted for review. It holds English function words (articles,
# prepositions, conjunctions, pronouns, auxiliaries), the clitic fragments "s" and "t" that
# splitting on apostrophes leaves behind ("women's", "don't"), and search-intent modifiers
# that carry no topic ("best", "near", "me", "vs", "versus").
#
# Deliberately absent although common in generic stopword lists, because in B2B search
# terms they are usually topic words once lowercased: "it" and "its" (IT), "us" (US),
# "will" (legal wills) and "not" (it inverts meaning).
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "best",
        "but",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "me",
        "my",
        "near",
        "of",
        "on",
        "or",
        "our",
        "s",
        "should",
        "so",
        "t",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "versus",
        "vs",
        "was",
        "we",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "you",
        "your",
    }
)

# Runs of characters that are neither letters nor digits. Python's ``\W`` is Unicode-aware,
# and the underscore (which ``\w`` admits) is added explicitly, so this splits on exactly the
# non-alphanumeric runs. For ASCII text it is the same split as ``[^0-9a-z]+`` on lowercased
# input; unlike that pattern it keeps accented letters inside their word ("naïve" stays one
# token instead of becoming the fragments "na" and "ve", which would create false overlaps).
_NON_ALPHANUMERIC_RUN: Final = re.compile(r"[\W_]+")

# Jaccard values are reported with the default decimal precision (28 significant digits,
# banker's rounding) whatever context the caller has active, so evidence is reproducible.
# Threshold tests never use these rounded values; see ``_meets``.
_JACCARD_CONTEXT: Final = Context(prec=28, rounding=ROUND_HALF_EVEN)

# An exact rational threshold in (0, 1] as (numerator, denominator), from
# ``Decimal.as_integer_ratio``.
type _Ratio = tuple[int, int]


def _normalise(text: str) -> str:
    """Return ``text`` in Unicode compatibility-caseless form (NFKC, casefold, NFKC).

    ``casefold`` can emit characters that are no longer NFKC-normalised, so normalisation is
    applied on both sides of it: full-width letters, ligatures and composed accents compare
    equal to their ordinary forms (full-width "crm" and ASCII "crm" are the same token).
    """
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def tokenize(text: str) -> frozenset[str]:
    """Split a search term, keyword or entity name into its set of comparable tokens.

    The text is Unicode-normalised (NFKC) and casefolded, split on every run of
    non-alphanumeric characters (spaces, punctuation, underscores, symbols), and stripped of
    empty fragments and :data:`STOPWORDS`. Digits are kept, so "B2B" is the token "b2b" and
    "2026" is a token. The result is a set: repeated words count once, which is what token
    Jaccard similarity compares (AAP K2 and C3b: "lowercased, stopwords removed").

    Args:
        text: Any text: a search term, keyword text, ad group name or campaign name.

    Returns:
        The distinct tokens. Empty when the text holds only stopwords or punctuation.

    Example:
        >>> sorted(tokenize("Best B2B Attribution Software"))
        ['attribution', 'b2b', 'software']
        >>> sorted(tokenize("CRM-tool_pricing (2026)!"))
        ['2026', 'crm', 'pricing', 'tool']
        >>> tokenize("the and for")
        frozenset()
    """
    return frozenset(
        token
        for token in _NON_ALPHANUMERIC_RUN.split(_normalise(text))
        if token and token not in STOPWORDS
    )


def _overlap(a: AbstractSet[str], b: AbstractSet[str]) -> tuple[int, int]:
    """Return ``(len(a & b), len(a | b))`` without building the union."""
    intersection = len(a & b)
    return intersection, len(a) + len(b) - intersection


def jaccard(a: AbstractSet[str], b: AbstractSet[str]) -> Decimal:
    """Return the Jaccard similarity ``len(a & b) / len(a | b)`` of two token sets.

    The value is ``Decimal(len(a & b)) / Decimal(len(a | b))`` at 28 significant digits with
    banker's rounding, independent of the caller's decimal context. Terminating fractions
    are exact (``2/4`` is ``Decimal("0.5")``); others are rounded at the 28th digit
    (``1/3``). Threshold decisions in this module never rely on the rounded value: they
    compare the exact fraction.

    Args:
        a: A token set, typically from :func:`tokenize`.
        b: Another token set.

    Returns:
        A value in [0, 1]. ``Decimal(0)`` when both sets are empty, because two terms with
        no tokens share nothing to compare; disjoint sets also give 0, identical non-empty
        sets give 1.

    Example:
        >>> jaccard(frozenset({"crm", "tool", "pricing"}), frozenset({"tool", "pricing", "guide"}))
        Decimal('0.5')
    """
    intersection, union = _overlap(a, b)
    if union == 0:
        return Decimal(0)
    return _JACCARD_CONTEXT.divide(Decimal(intersection), Decimal(union))


def _ratio(name: str, value: object) -> _Ratio:
    """Validate a share or similarity threshold and return it as an exact integer ratio.

    Args:
        name: Parameter name used in the error message.
        value: The threshold the caller passed, expected to be a finite ``Decimal`` in (0, 1].

    Returns:
        ``(numerator, denominator)`` with ``numerator / denominator == value`` exactly.

    Raises:
        TypeError: ``value`` is not a ``Decimal``. Thresholds come from
            ``Thresholds.ratio()``, which yields ``Decimal``; a float would make the
            comparison inexact.
        ValueError: ``value`` is not finite or lies outside (0, 1]. Zero is rejected because
            every pair of terms, even two with no token in common, has Jaccard >= 0, so the
            rule would join unrelated terms; a zero head share would make every token a head
            token.
    """
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    if not value.is_finite() or value <= 0 or value > 1:
        raise ValueError(f"{name} must be a finite Decimal in (0, 1], got {value}")
    return value.as_integer_ratio()


def _reject_single_string(name: str, value: object) -> None:
    """Refuse a bare string where a collection of strings is expected.

    A ``str`` is itself a ``Sequence[str]`` and an ``Iterable[str]`` of its characters, so
    passing one term where a list of terms belongs would silently compare single letters.

    Raises:
        TypeError: ``value`` is a ``str``.
    """
    if isinstance(value, str):
        raise TypeError(f"{name} must be a collection of strings, not a single string")


def _meets(a: AbstractSet[str], b: AbstractSet[str], threshold: _Ratio) -> bool:
    """Return whether ``jaccard(a, b) >= threshold``, compared as exact fractions.

    ``len(a & b) / len(a | b) >= p / q`` is evaluated as
    ``len(a & b) * q >= len(a | b) * p`` on integers. Two empty sets have Jaccard 0, which
    never meets a threshold above 0.
    """
    intersection, union = _overlap(a, b)
    if union == 0:
        return False
    numerator, denominator = threshold
    return intersection * denominator >= union * numerator


def _head_of(token_sets: Sequence[AbstractSet[str]], min_share: _Ratio) -> frozenset[str]:
    """Return the tokens present in at least ``min_share`` of the given member token sets.

    A token's share is the number of members containing it divided by the number of
    members, compared exactly: ``count * q >= members * p``. No members, no head tokens.
    """
    members = len(token_sets)
    if members == 0:
        return frozenset()
    numerator, denominator = min_share
    counts = Counter(token for tokens in token_sets for token in tokens)
    return frozenset(
        token for token, count in counts.items() if count * denominator >= members * numerator
    )


def head_tokens(members: Sequence[str], *, min_share: Decimal) -> frozenset[str]:
    """Return the tokens present in at least ``min_share`` of the member terms (C3b).

    Each distinct member string counts once, as in :func:`cluster_terms`, and a token counts
    once per member however often the member repeats it. The comparison is inclusive: with
    ``min_share=Decimal("0.5")`` a token in exactly 2 of 4 members is a head token.

    Args:
        members: The member search terms of one cluster.
        min_share: The configured head-token share (``c3b.head_token_share``), a ``Decimal``
            in (0, 1].

    Returns:
        The head tokens; empty when there are no members or no token reaches the share.

    Raises:
        TypeError: ``members`` is a single string, or ``min_share`` is not a ``Decimal``.
        ValueError: ``min_share`` is outside (0, 1].

    Example:
        >>> from decimal import Decimal
        >>> sorted(
        ...     head_tokens(
        ...         ["crm tool", "crm tool pricing", "tool pricing guide"],
        ...         min_share=Decimal("0.5"),
        ...     )
        ... )
        ['crm', 'pricing', 'tool']
    """
    _reject_single_string("members", members)
    share = _ratio("min_share", min_share)
    return _head_of([tokenize(member) for member in set(members)], share)


@dataclass(frozen=True, slots=True)
class TermCluster:
    """One single-linkage cluster of search terms and the head tokens that name it (C3b).

    Instances are normalised on construction: ``members`` becomes the sorted tuple of
    distinct member strings and ``head_tokens`` a ``frozenset``, so equal clusters compare
    and hash equal however they were built.

    Attributes:
        members: The distinct member terms, sorted alphabetically (code-point order).
        head_tokens: Tokens present in at least the configured share of the members.

    Example:
        >>> cluster = TermCluster(("crm tool pricing", "crm tool"), frozenset({"tool", "crm"}))
        >>> cluster.members
        ('crm tool', 'crm tool pricing')
        >>> cluster.label
        'crm tool'
    """

    members: tuple[str, ...]
    head_tokens: frozenset[str]

    def __post_init__(self) -> None:
        """Normalise the fields and reject an empty cluster.

        Raises:
            TypeError: ``members`` is a single string.
            ValueError: ``members`` is empty; every cluster has at least one term.
        """
        _reject_single_string("members", self.members)
        members = tuple(sorted(set(self.members)))
        if not members:
            raise ValueError("a term cluster needs at least one member")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "head_tokens", frozenset(self.head_tokens))

    @property
    def size(self) -> int:
        """Number of distinct member terms (C3b compares it with ``c3b.min_terms``)."""
        return len(self.members)

    @property
    def sorted_head(self) -> tuple[str, ...]:
        """Head tokens in alphabetical order, the stable form used in evidence."""
        return tuple(sorted(self.head_tokens))

    @property
    def label(self) -> str:
        """Space-joined sorted head tokens, as in "Create ad group or campaign for '{label}'".

        Empty when the cluster has no head token; C3b never names such a cluster.
        """
        return " ".join(self.sorted_head)


class _DisjointSet:
    """Union-find over ``0 .. count - 1`` with path halving and union by size.

    Only the resulting partition is observable, and it does not depend on the order in which
    unions are applied, so clustering is deterministic even though candidate pairs are
    visited in set order.
    """

    __slots__ = ("_parent", "_size")

    def __init__(self, count: int) -> None:
        self._parent = list(range(count))
        self._size = [1] * count

    def find(self, item: int) -> int:
        """Return the representative of ``item``'s component."""
        parent = self._parent
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(self, a: int, b: int) -> None:
        """Merge the components of ``a`` and ``b``."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a == root_b:
            return
        if self._size[root_a] < self._size[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        self._size[root_a] += self._size[root_b]


def _ceil_div(dividend: int, divisor: int) -> int:
    """Return ``ceil(dividend / divisor)`` for a positive divisor, in exact integer arithmetic."""
    return -(-dividend // divisor)


def _min_overlap(size_a: int, size_b: int, threshold: _Ratio) -> int:
    """Return the fewest shared tokens two sets of these sizes need to reach ``threshold``.

    ``i / (size_a + size_b - i) >= p / q`` holds exactly when
    ``i >= p * (size_a + size_b) / (p + q)``, so the minimum overlap is that bound rounded
    up.
    """
    numerator, denominator = threshold
    return _ceil_div(numerator * (size_a + size_b), numerator + denominator)


def _probe_prefix_length(size: int, threshold: _Ratio) -> int:
    """Return how many of a set's rarest tokens are looked up in the index (prefix filter).

    If ``jaccard(x, y) >= t`` then ``len(x & y) >= t * len(x | y) >= ceil(t * len(x))``.
    With every set's tokens ordered by one global order, the first token ``x`` and ``y``
    share therefore lies within the first ``len(x) - ceil(t * len(x)) + 1`` tokens of ``x``
    (the prefix-filtering principle of set-similarity joins). For ``t`` in (0, 1] and a
    non-empty set the length lies in [1, len(x)]; an empty set has no prefix and therefore
    no similar term.
    """
    if size == 0:
        return 0
    numerator, denominator = threshold
    return size - _ceil_div(numerator * size, denominator) + 1


def _index_prefix_length(size: int, threshold: _Ratio) -> int:
    """Return how many of a set's rarest tokens are added to the index (indexing prefix).

    Sets are indexed in ascending size order, so a set ``y`` already in the index is never
    larger than the probing set ``x``. Then ``len(x & y) >= t / (1 + t) * (len(x) + len(y))
    >= 2t / (1 + t) * len(y)``, so the first shared token lies within the first
    ``len(y) - ceil(2t / (1 + t) * len(y)) + 1`` tokens of ``y``. This prefix is never
    longer than the probing prefix, and it keeps tokens common to most terms ("crm" in a CRM
    account) out of the index for short terms, where they would otherwise pair every term
    with every other.
    """
    if size == 0:
        return 0
    return size - _min_overlap(size, size, threshold) + 1


def cluster_terms(
    terms: Sequence[str], *, threshold: Decimal, min_head_share: Decimal
) -> list[TermCluster]:
    """Group search terms into single-linkage clusters of token Jaccard similarity (C3b).

    Two terms are linked when ``jaccard(tokenize(a), tokenize(b)) >= threshold`` (inclusive,
    compared exactly), and the clusters are the connected components of that graph: a term
    joins a cluster when it is similar enough to any one member, so "crm tool" and
    "tool pricing guide" share a cluster through "crm tool pricing" although they are not
    similar to each other. Every term belongs to exactly one cluster; a term similar to no
    other term forms a singleton cluster, and the caller applies its own minimum size
    (``c3b.min_terms``). Terms with no tokens (only stopwords or punctuation) are always
    singletons.

    Identical strings are de-duplicated first. Members are sorted alphabetically, and
    clusters are ordered by size (largest first), then by first member, so the output is
    identical for any input order and any hash seed.

    Candidate pairs come from an inverted index over each term's rarest tokens, using the
    prefix, size-ordered indexing and positional filters of set-similarity joins
    (``_probe_prefix_length``, ``_index_prefix_length``, ``_min_overlap``), so a token shared
    by most terms ("crm" in a CRM account) does not force a comparison of every pair. The
    pruning is exact: it skips only pairs that cannot reach the threshold, and every
    remaining pair is decided by the exact Jaccard comparison.

    Args:
        terms: Search terms, typically the window's converting terms.
        threshold: The configured link threshold (``c3b.jaccard``), a ``Decimal`` in (0, 1].
        min_head_share: The configured head-token share (``c3b.head_token_share``), a
            ``Decimal`` in (0, 1], used to compute each cluster's :attr:`TermCluster.head_tokens`
            as :func:`head_tokens` would.

    Returns:
        Every cluster, singletons included, largest first.

    Raises:
        TypeError: ``terms`` is a single string, or a threshold is not a ``Decimal``.
        ValueError: A threshold is outside (0, 1].
    """
    _reject_single_string("terms", terms)
    link = _ratio("threshold", threshold)
    share = _ratio("min_head_share", min_head_share)

    unique_terms = sorted(set(terms))
    token_sets = [tokenize(term) for term in unique_terms]
    frequency = Counter(token for tokens in token_sets for token in tokens)

    def rarest_first(token: str) -> tuple[int, str]:
        return frequency[token], token

    forest = _DisjointSet(len(unique_terms))
    # token -> [(term index, position of the token in that term's rarest-first order)]
    postings: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    by_size = sorted(range(len(unique_terms)), key=lambda i: (len(token_sets[i]), unique_terms[i]))
    for index in by_size:
        tokens = token_sets[index]
        size = len(tokens)
        ordered = sorted(tokens, key=rarest_first)
        compared: set[int] = set()
        for position, token in enumerate(ordered[: _probe_prefix_length(size, link)]):
            for other, other_position in postings[token]:
                if other in compared:
                    continue
                # The first sighting of ``other`` is at the first token the two terms share,
                # so every shared token sits at or after these positions in both orders
                # (positional filter): the overlap is at most what remains of the shorter tail.
                compared.add(other)
                other_size = len(token_sets[other])
                overlap_bound = min(size - position, other_size - other_position)
                if overlap_bound < _min_overlap(size, other_size, link):
                    continue
                if forest.find(other) != forest.find(index) and _meets(
                    tokens, token_sets[other], link
                ):
                    forest.union(other, index)
        for position, token in enumerate(ordered[: _index_prefix_length(size, link)]):
            postings[token].append((index, position))

    components: defaultdict[int, list[int]] = defaultdict(list)
    for index in range(len(unique_terms)):
        components[forest.find(index)].append(index)

    clusters = [
        TermCluster(
            members=tuple(unique_terms[index] for index in indices),
            head_tokens=_head_of([token_sets[index] for index in indices], share),
        )
        for indices in components.values()
    ]
    clusters.sort(key=lambda cluster: (-cluster.size, cluster.members[0]))
    return clusters


def contains_tokens(texts: Iterable[str], tokens: AbstractSet[str]) -> bool:
    """Return whether any of ``tokens`` appears among the tokens of any of ``texts`` (C3b).

    C3b raises a launch idea only when a cluster's head tokens "appear in no keyword text,
    ad group name or campaign name". The test is therefore *any token in any text*: a single
    head token found in a single name means the account already covers the theme, and the
    cluster is not a launch idea. C3b fires only when this function returns ``False``.

    Texts are tokenised with :func:`tokenize`, so matching is by whole token, caseless and
    stopword-free: "CRM Tool Ad Group" contains "crm" and "tool" but not "too". The given
    tokens are normalised the same way, so a head token compares equal however it is cased.

    Args:
        texts: Keyword texts, ad group names and campaign names to search.
        tokens: The tokens to look for, typically :attr:`TermCluster.head_tokens`.

    Returns:
        ``True`` when at least one token occurs in at least one text. ``False`` for an empty
        token set: there is nothing to find. A caller that needs a named theme must also
        require non-empty head tokens.

    Raises:
        TypeError: ``texts`` is a single string.

    Example:
        >>> contains_tokens(["Brand", "CRM Tool Ad Group"], frozenset({"crm", "tool"}))
        True
        >>> contains_tokens(["Brand", "Competitors"], frozenset({"pricing", "guide"}))
        False
    """
    _reject_single_string("texts", texts)
    _reject_single_string("tokens", tokens)
    needles = frozenset(
        normalised
        for token in tokens
        for normalised in _NON_ALPHANUMERIC_RUN.split(_normalise(token))
        if normalised
    )
    if not needles:
        return False
    return any(not needles.isdisjoint(tokenize(text)) for text in texts)


def _term_key(term: str) -> str:
    """Return the identity of a term: normalised, casefolded and whitespace-collapsed."""
    return " ".join(_normalise(term).split())


def adjacent_terms(term: str, candidates: Sequence[str], *, threshold: Decimal) -> list[str]:
    """Return the candidates whose token Jaccard similarity with ``term`` meets ``threshold``.

    K2 suggests adjacent terms for an expansion candidate from the account's own search-term
    report: other window search terms with token Jaccard at least ``k2.adjacent_jaccard``
    (inclusive, compared exactly). The term itself is excluded, also when a candidate spells
    it with different case or spacing ("CRM  Tool" is "crm tool"). Repeated candidate
    strings are returned once.

    The result keeps the candidates' input order and is otherwise unranked and uncapped. The
    caller (K2) drops terms that are already keywords, excluded terms and terms below the
    click minimum, orders by conversions then clicks, and keeps at most
    ``k2.adjacent_max_terms``.

    Args:
        term: The converting search term.
        candidates: The window's search terms.
        threshold: The configured similarity (``k2.adjacent_jaccard``), a ``Decimal`` in
            (0, 1].

    Returns:
        Matching candidates in first-occurrence order; empty when ``term`` has no tokens.

    Raises:
        TypeError: ``candidates`` is a single string, or ``threshold`` is not a ``Decimal``.
        ValueError: ``threshold`` is outside (0, 1].

    Example:
        >>> from decimal import Decimal
        >>> adjacent_terms(
        ...     "crm tool pricing",
        ...     ["crm tool", "CRM Tool Pricing", "tool pricing guide", "project management"],
        ...     threshold=Decimal("0.5"),
        ... )
        ['crm tool', 'tool pricing guide']
    """
    _reject_single_string("candidates", candidates)
    link = _ratio("threshold", threshold)
    own_tokens = tokenize(term)
    if not own_tokens:
        return []
    own_key = _term_key(term)
    seen: set[str] = set()
    matches: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _term_key(candidate) != own_key and _meets(own_tokens, tokenize(candidate), link):
            matches.append(candidate)
    return matches
