"""Precedence: the G1 guard, B3 allocation, P1-P8, feedback suppression and ranking.

AAP 0.5.4.2 fixes the order in which the rule outputs of one run are guarded, held, flagged,
suppressed and ranked; :func:`apply_precedence` applies it exactly, after every rule module
has returned its :class:`~ads_engine.analysis.context.RuleResult` and before ``runner.py``
persists the run (AAP 0.5.1 "G1 guard, holds, suppression, flags, ranking"):

1. **Collect** every ``RuleResult``: candidates, findings and withheld patterns by section,
   with each rule's own ``suppressed`` counts carried into its section's counts.
2. **G1 guard** (AAP 0.5.4.2 G1, FR-6 "never recommend increasing spend on high-utilization
   + poor-efficiency campaigns"): every candidate whose ``increases_spend_on`` meets
   :func:`~ads_engine.analysis.metrics.spend_increase_blocked` is dropped (C2 Scale, B1, a W1
   naming a blocked campaign) and counted ``g1_blocked`` in its section
   (:func:`apply_g1`). An independent post-check then raises
   :class:`PrecedenceInvariantError` when any increase survives on a blocked target.
3. **B3 allocation** (AAP 0.5.4.2): the freed amount of each B3 donor funds the B1 increases
   that survived G1 in the same source and currency, deterministically
   (:func:`allocate_b3`); no receiver is ever funded beyond its B1 increase (checked), and
   unallocated freed amount stays a plain decrease. B1 and B3 cards are re-rendered with
   ``budget.render_b1_funding`` and ``budget.render_b3_allocation``, and each depends on its
   allocation partners: a donor on the receivers it funds, a receiver on its donors.
4. **P1 discrepancies first**: each candidate's ``depends_on`` (allocation partners
   included) is expanded and frozen in ``evidence.extra.depends_on`` for the read-time
   overlay; every non-C0 item depending on a discrepant campaign or budget is held ("Resolve
   discrepancy first"); C0 is exempt from holds, feedback and ordinary ranking and is pinned
   first; every other item is flagged "Unverified while N discrepancies are open".
5. **P2 tracking**: a source with window spend and zero recorded outcomes records
   ``conversion_tracking_absent`` in each affected section and suppresses that source's K1,
   C1, the zero-outcome branch of B2 and W2 (counted ``tracking_absent``).
6. **P3 simulated data**: ``data_status`` from the frozen provenance modes (``simulated``,
   ``mixed`` or ``live``) with the ``simulated`` flag.
7. **P4 stale / incomplete**: per item, from its own ``required_datasets`` only (AAP 0.3.2:
   "A successful sync of one dataset never hides a failed refresh of another"); a live item
   with a stale dataset gets ``data_status = "stale"``; one ``incomplete_dataset`` finding per
   incomplete dataset per section, its ``evidence.items`` recounted over the stored items
   after feedback (step 10). The configured ``sources.stale_after_hours`` the datasets were
   judged against is frozen into ``evidence.thresholds`` of every item with a required
   dataset, stale or not (AAP 0.5.4.8 "the thresholds applied").
8. **P5 short window**: the ``short_window`` flag on every item (the banner is the
   overlay's, from ``Run.short_window``). The ``window.trusted_min_days`` the window was
   judged against is frozen into ``evidence.thresholds`` of every item, short window or not.
9. **P6/P7/P8 checks**: no item carries a sample below its minimum (P6: such patterns are
   ``Withheld``), a CR1 pair that is not distinguishable carries ``not_distinguishable`` and
   no winner wording (P7), and the attribution floor flags set by the rules pass through
   unchanged (P8).
10. **Feedback suppression**: active dismiss, snooze and done feedback drops its rule and
    entity (C0 exempt), counted ``feedback_dismiss`` / ``feedback_snooze`` /
    ``feedback_done``. Each section's ``no_crm_link`` finding then recounts its
    ``evidence.items`` over the section's surviving items that carry the flag, held and C0
    items included, so the stored count matches the stored items
    (:func:`_recount_no_crm_link`). Each ``incomplete_dataset`` finding likewise recounts its
    ``evidence.items`` over the section's surviving items that read the dataset incomplete,
    held and C0 items included, and cites only the reasons those items have; a finding with no
    such item keeps its reasons with ``items`` 0 (:func:`_recount_incomplete_datasets`).
    Each section left with no item then gets one ``incomplete_dataset`` finding, ``items`` 0,
    per dataset its rules read that is not ``complete`` over the run's window and that no
    finding of the section names yet, so an empty section states why (AAP 0.5.5 "Each
    section shows an EmptyState with the reason"); a dataset that resolves to no dataset
    gives the reason "no active {source} dataset" (:func:`_section_data_findings`).
    Every surviving C0 item then leads with ``held_recommendations`` (AAP 0.1.2 "Every card
    leads with one headline number"; P1 "resolve first, before any other recommendation is
    trusted"): the number of surviving non-C0 items whose ``extra.blocking_discrepancies``
    names its discrepancy, 0 included, appended as its last metric and named as its headline
    (:func:`_with_held_counts`); an item held by two discrepancies counts for both. An
    independent post-check then raises :class:`PrecedenceInvariantError` when an item outside
    White papers does not name one of its own metrics as its headline
    (:func:`_check_headlines`).
11. **Ranking** (AAP 0.5.4.1 "Ranking ties: broken by entity name, then by id, so runs are
    deterministic"): C0 pinned first, then each group in display order, unheld before held,
    by ``rank_key``, entity name, id and key; ``rank`` is 1-based across the section.

Test seams: the G1 filtering step (:func:`apply_g1`) and the B3 allocation step
(:func:`allocate_b3`) are module-level functions that :func:`apply_precedence` looks up at
call time, so a test can replace either one; the independent post-checks then trip
:class:`PrecedenceInvariantError`.

Purity: reads only the :class:`~ads_engine.analysis.context.AnalysisContext` and the rule
results; no database, network, settings or logging access. Candidates are frozen, so every
change is a copy made with :func:`dataclasses.replace`,
:meth:`~ads_engine.analysis.context.Evidence.with_extra`,
:meth:`~ads_engine.analysis.context.Evidence.with_thresholds` and
:meth:`~ads_engine.analysis.context.Evidence.with_headline`. Never imports ``runner`` or
``overlay``, which consume this module's output.

Example:
    >>> results = [keywords.evaluate(ctx), campaigns.evaluate(ctx)]  # doctest: +SKIP
    >>> out = apply_precedence(results, ctx)  # doctest: +SKIP
    >>> [(item.rank, item.candidate.rule_id) for item in out.items["keywords"]]  # doctest: +SKIP
    [(1, 'K1'), (2, 'K2')]
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from ads_engine.analysis import budget, campaigns, keywords, metrics, white_papers
from ads_engine.analysis.context import (
    B1,
    B2,
    B3,
    BUDGET,
    C1,
    CAMPAIGNS,
    CR1,
    CREATIVE,
    DATA_LIVE,
    DATA_MIXED,
    DATA_SIMULATED,
    DATA_STALE,
    ENTITY_AD_GROUP,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CREATIVE,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    EXTRA_HEADLINE_METRIC,
    FINDING_CODES,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_INCOMPLETE_DATASET,
    FINDING_NO_CRM_LINK,
    FLAG_INCOMPLETE,
    FLAG_NO_CRM_LINK,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_SHORT_WINDOW,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    FULL_STATE_DATASETS,
    GROUP_ORDER,
    HELD_DISCREPANCY_REASON,
    K1,
    KEYWORDS,
    RULE_IDS,
    SECTION_ORDER,
    SUPPRESS_G1_BLOCKED,
    SUPPRESS_TRACKING_ABSENT,
    SUPPRESSION_REASONS,
    W2,
    WHITE_PAPERS,
    AnalysisContext,
    Candidate,
    DecisionSubject,
    EntityRef,
    Evidence,
    FeedbackInfo,
    Finding,
    Flag,
    Freshness,
    Metric,
    RuleResult,
    SubjectKey,
    Withheld,
    cohort_group_currency,
    creative_group_campaign,
    format_instant,
    short_window_message,
    unverified_message,
)
from ads_engine.analysis.creative import SECTION_DATASETS as CREATIVE_DATASETS
from ads_engine.analysis.thresholds import Thresholds

__all__ = [
    "TRACKING_ABSENT_MESSAGE",
    "Allocation",
    "PrecedenceInvariantError",
    "PrecedenceOutput",
    "RankedItem",
    "allocate_b3",
    "apply_g1",
    "apply_precedence",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRACKING_ABSENT_MESSAGE: Final = (
    "No conversions or leads recorded — verify tracking before acting on zero-outcome rules"
)
"""The P2 ``conversion_tracking_absent`` finding message, verbatim from AAP 0.5.4.2."""

_ZERO_OUTCOMES_BRANCH: Final = "zero_outcomes"
"""``evidence.extra.branch`` of a B2 item fired by zero outcomes, which P2 suppresses."""

type _SectionRules = tuple[tuple[str, tuple[str, ...]], ...]

_TRACKING_SECTIONS: Final[Mapping[str, _SectionRules]] = MappingProxyType(
    {
        "google": ((KEYWORDS, (K1,)), (CAMPAIGNS, (C1,)), (BUDGET, (B2,))),
        "linkedin": ((CAMPAIGNS, (C1,)), (BUDGET, (B2,)), (WHITE_PAPERS, (W2,))),
    }
)
"""Per ad source, the sections P2 affects and the zero-outcome rules it suppresses there.

AAP 0.5.4.2 P2 names K1, C1, the zero-outcome branch of B2 and W2. K1 reads Google search
terms only and W2 LinkedIn white papers only, so each section lists the rules of that source
it can hold; the ``conversion_tracking_absent`` finding is added to exactly these sections.
"""

_RULE_LOOKBACKS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {K1: MappingProxyType({"google.metrics_daily": "baseline_window"})}
)
"""``rule_id -> {dataset name: evidence.extra key}``: lookbacks reaching before the window.

K1's baseline rate reads ``google.metrics_daily`` over the trailing ``k1.baseline_days``
ending at the window end (AAP 0.5.4.3), so a K1 item's metrics dataset must be complete from
its frozen baseline start. The read-time overlay applies the same widening, so the flags a
card carries at compute time and at read time are judged over the same range.
"""

_LOOKBACK_DAYS_KEYS: Final[Mapping[str, str]] = MappingProxyType({K1: "k1.baseline_days"})
"""The threshold giving each lookback's length, used when its frozen start is unusable."""

_SECTION_DATASETS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        KEYWORDS: keywords.SECTION_DATASETS,
        CAMPAIGNS: campaigns.SECTION_DATASETS,
        BUDGET: budget.SECTION_DATASETS,
        CREATIVE: CREATIVE_DATASETS,
        WHITE_PAPERS: white_papers.SECTION_DATASETS,
    }
)
"""``section -> datasets``: every dataset each section's rules read, from the rule modules.

A section that stores no item is judged over these (:func:`_section_data_findings`), so its
empty state names each dataset that does not cover the window (AAP 0.5.5 "Each section shows
an EmptyState with the reason"). Each module derives its tuple from its own rule-dataset
constants, so the two never drift.
"""

_WINDOW_START_LABEL: Final = "window start"
"""How an incomplete reason names a required range that starts at the window start."""

_STALE_AFTER_KEY: Final = "sources.stale_after_hours"
"""The threshold P4 judges every required dataset's age against, frozen into the evidence."""

_TRUSTED_MIN_DAYS_KEY: Final = "window.trusted_min_days"
"""The threshold P5 judges the window's length against, frozen into the evidence."""

_HELD_METRIC_KEY: Final = "held_recommendations"
"""The key of a C0 item's headline metric: the recommendations its discrepancy holds."""

_HELD_METRIC_LABEL: Final = "Recommendations held until resolved"
"""The display label of a C0 item's headline metric."""

_HELD_METRIC_UNIT: Final = "recommendations"
"""The unit of a C0 item's headline metric."""

_SOURCE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"google": "Google", "linkedin": "LinkedIn", "hubspot": "HubSpot"}
)
"""Display names of the sources, for flag messages."""

_SOURCE_ORDER: Final[tuple[str, ...]] = ("google", "linkedin", "hubspot")
"""The fixed order sources are named in."""

_WINNER_WORDING: Final = re.compile(
    r"\b(?:winners?|winning|wins|won|better|best|beats?|outperform(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
"""Wording that calls a creative variant better (P7: only a distinguishable pair may)."""

_RULE_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {rule: index for index, rule in enumerate(RULE_IDS)}
)
_FINDING_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {code: index for index, code in enumerate(FINDING_CODES)}
)
_REASON_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {reason: index for index, reason in enumerate(SUPPRESSION_REASONS)}
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class PrecedenceInvariantError(AssertionError):
    """A precedence guarantee was broken; the run must fail rather than persist it.

    Raised explicitly (never with ``assert``, so it also holds under ``python -O``) when a
    spend increase survives on a G1-blocked target, a B3 allocation funds a receiver beyond
    its B1 increase or a donor beyond its freed amount, an item carries a sample below its
    minimum (P6), a CR1 pair that is not distinguishable lacks ``not_distinguishable`` or
    uses winner wording (P7), an item's ``evidence.thresholds`` records a threshold P4 or
    P5 applied with another value, or an item outside White papers leaves without a
    headline metric of its own evidence. Tests assert it trips when a guard step is bypassed.
    """


@dataclass(frozen=True, slots=True)
class RankedItem:
    """One surviving candidate with its 1-based rank within its section.

    Attributes:
        candidate: The candidate as precedence left it: expanded ``depends_on``, holds,
            flags, ``data_status`` and pinning applied.
        rank: The 1-based position across the whole section, in display order.

    Raises:
        TypeError: On construction, when ``candidate`` is not a ``Candidate`` or ``rank`` is
            not an ``int``.
        ValueError: On construction, when ``rank`` is below 1.
    """

    candidate: Candidate
    rank: int

    def __post_init__(self) -> None:
        """Validate the candidate and the rank."""
        if not isinstance(self.candidate, Candidate):
            raise TypeError(f"RankedItem.candidate must be a Candidate, found {self.candidate!r}")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise TypeError(f"RankedItem.rank must be an int, found {self.rank!r}")
        if self.rank < 1:
            raise ValueError(f"RankedItem.rank is 1-based, found {self.rank}")

    @property
    def section(self) -> str:
        """The candidate's section."""
        return self.candidate.section

    @property
    def group(self) -> str:
        """The candidate's group within its section."""
        return self.candidate.group


@dataclass(frozen=True, slots=True)
class Allocation:
    """One B3 reallocation: part of a donor's freed amount funding a receiver's B1 increase.

    A reallocation is funding for the receiver's B1 increase, never an increase of its own
    (AAP 0.5.4.2 "Presenting reallocations").

    Attributes:
        donor: The B3 subject giving the amount.
        receiver: The B1 subject whose recommended increase the amount funds.
        amount: The amount moved, in the donor's and receiver's shared currency.

    Raises:
        TypeError: On construction, when ``amount`` is not a ``Decimal``.
        ValueError: On construction, when ``amount`` is not finite.
    """

    donor: SubjectKey
    receiver: SubjectKey
    amount: Decimal

    def __post_init__(self) -> None:
        """Validate the amount."""
        if not isinstance(self.amount, Decimal):
            raise TypeError(f"Allocation.amount must be a Decimal, found {self.amount!r}")
        if not self.amount.is_finite():
            raise ValueError(f"Allocation.amount must be finite, found {self.amount!r}")

    def to_json(self) -> dict[str, Any]:
        """Return ``{donor: {type, id}, receiver: {type, id}, amount}`` with a decimal string."""
        return {
            "donor": {"type": self.donor[0], "id": self.donor[1]},
            "receiver": {"type": self.receiver[0], "id": self.receiver[1]},
            "amount": format(self.amount, "f"),
        }


def _section_keyed[T](value: Mapping[str, T], what: str) -> Mapping[str, T]:
    """Return a read-only copy of a mapping keyed by exactly :data:`SECTION_ORDER`.

    Raises:
        TypeError: When ``value`` is not a mapping.
        ValueError: When its keys are not exactly the five sections.
    """
    if not isinstance(value, Mapping):
        raise TypeError(f"PrecedenceOutput.{what} must be a mapping, found {value!r}")
    if set(value) != set(SECTION_ORDER):
        raise ValueError(
            f"PrecedenceOutput.{what} must be keyed by exactly {SECTION_ORDER}, "
            f"found {tuple(value)}"
        )
    return MappingProxyType({section: value[section] for section in SECTION_ORDER})


@dataclass(frozen=True, slots=True)
class PrecedenceOutput:
    """Everything a run persists, per section in :data:`SECTION_ORDER` (AAP 0.5.3).

    Attributes:
        items: The ranked items of each section, in rank order.
        findings: The findings of each section: P2 tracking findings first, then the rules'
            findings (a ``no_crm_link`` finding's ``items`` recounted over the section's
            stored items), then P4 ``incomplete_dataset`` findings (``items`` and ``reasons``
            recounted over the section's stored items; a section with no stored item also gets
            one, ``items`` 0, per dataset it reads that is not complete over the window), by
            dataset name and id.
        withheld: The withheld patterns of each section, in rule order (P6).
        suppressed: Per section, ``{reason: count}`` of the items precedence or a rule
            suppressed: ``g1_blocked``, ``tracking_absent``, ``feedback_*`` and any rule-level
            reason; only positive counts.
        allocations: Every B3 reallocation, in allocation order.

    Raises:
        TypeError: On construction, when a field is not a mapping.
        ValueError: On construction, when a mapping is not keyed by exactly the five sections.
    """

    items: Mapping[str, tuple[RankedItem, ...]]
    findings: Mapping[str, tuple[Finding, ...]]
    withheld: Mapping[str, tuple[Withheld, ...]]
    suppressed: Mapping[str, Mapping[str, int]]
    allocations: tuple[Allocation, ...] = ()

    def __post_init__(self) -> None:
        """Copy every mapping read-only, keyed in section order."""
        object.__setattr__(self, "items", _section_keyed(self.items, "items"))
        object.__setattr__(self, "findings", _section_keyed(self.findings, "findings"))
        object.__setattr__(self, "withheld", _section_keyed(self.withheld, "withheld"))
        suppressed = _section_keyed(self.suppressed, "suppressed")
        object.__setattr__(
            self,
            "suppressed",
            MappingProxyType(
                {section: MappingProxyType(dict(counts)) for section, counts in suppressed.items()}
            ),
        )
        object.__setattr__(self, "allocations", tuple(self.allocations))

    def all_items(self) -> tuple[RankedItem, ...]:
        """Every ranked item, section by section in :data:`SECTION_ORDER`, in rank order."""
        return tuple(item for section in SECTION_ORDER for item in self.items[section])

    def suppressed_total(self, reason: str) -> int:
        """The count of ``reason`` summed over every section."""
        return sum(counts.get(reason, 0) for counts in self.suppressed.values())


# ---------------------------------------------------------------------------
# Ordering helpers
# ---------------------------------------------------------------------------


def _orderable(value: object) -> tuple[Any, ...]:
    """Map a rank-key element to a value that compares with any other element.

    Rules build ``rank_key`` tuples from ``Decimal`` and ``int`` amounts, strings, instants
    and nested tuples. Plain tuple comparison raises ``TypeError`` when one position holds
    values of different kinds (``None`` against a ``Decimal``, a ``date`` against a
    ``datetime``), so each element is tagged by kind first: numbers, strings, instants,
    dates and tuples each compare among themselves, and ``None`` (an unavailable value)
    always sorts last. The order of every well-typed key is unchanged.
    """
    if value is None:
        return (9,)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, Decimal):
        return (1, value) if value.is_finite() else (8, str(value))
    if isinstance(value, int):
        return (1, value)
    if isinstance(value, float):
        return (1, value) if math.isfinite(value) else (8, repr(value))
    if isinstance(value, str):
        return (2, value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return (3, 1, value.isoformat())
        return (3, 0, value.astimezone(UTC).isoformat(timespec="microseconds"))
    if isinstance(value, date):
        return (4, value.toordinal())
    if isinstance(value, tuple | list):
        return (5, tuple(_orderable(item) for item in value))
    return (7, type(value).__name__, repr(value))


def _rank_order(candidate: Candidate) -> tuple[Any, ...]:
    """The total, input-independent order of candidates within one group (AAP 0.5.4.1).

    ``rank_key``, then entity name, then entity id (entities with an id first), then entity
    key, exactly :attr:`Candidate.sort_key` with the rank key made comparable
    (:func:`_orderable`). Rule id, action and reason break any remaining tie, so even two
    items on one entity never depend on the order the rule results were passed in.
    """
    rank_key, name, id_slot, key = candidate.sort_key
    return (
        _orderable(rank_key),
        name,
        id_slot,
        key,
        _RULE_ORDER.get(candidate.rule_id, len(_RULE_ORDER)),
        candidate.action,
        candidate.reason,
    )


def _entity_order(entity: EntityRef | None) -> tuple[str, str]:
    """Order findings and withheld lines by entity: section-wide ones first, then by key."""
    return ("", "") if entity is None else (entity.key, entity.name)


def _finding_order(finding: Finding) -> tuple[Any, ...]:
    """A finding's deterministic position within its section's rule findings."""
    return (
        _FINDING_ORDER.get(finding.code, len(_FINDING_ORDER)),
        _entity_order(finding.entity),
        finding.message,
    )


def _withheld_order(item: Withheld) -> tuple[Any, ...]:
    """A withheld line's deterministic position: rule order, then entity, then reason."""
    return (
        _RULE_ORDER.get(item.rule_id, len(_RULE_ORDER)),
        _entity_order(item.entity),
        item.reason,
        _orderable(item.observed),
        _orderable(item.required),
        item.unit,
    )


def _source_order(source: str) -> int:
    return _SOURCE_ORDER.index(source) if source in _SOURCE_ORDER else len(_SOURCE_ORDER)


def _source_list(sources: Iterable[str]) -> str:
    """Distinct source display names in source order, comma-joined."""
    distinct = sorted(set(sources), key=lambda source: (_source_order(source), source))
    return ", ".join(_SOURCE_LABELS.get(source, source) for source in distinct)


# ---------------------------------------------------------------------------
# Step 1: collect the rule results
# ---------------------------------------------------------------------------


class _Counts:
    """Suppression counts per section and reason, accumulated across the steps."""

    __slots__ = ("_counts",)

    def __init__(self) -> None:
        """Start with an empty count for every section."""
        self._counts: dict[str, dict[str, int]] = {section: {} for section in SECTION_ORDER}

    def add(self, section: str, reason: str, count: int = 1) -> None:
        """Add ``count`` items suppressed in ``section`` for ``reason``."""
        if count <= 0:
            return
        bucket = self._counts[section]
        bucket[reason] = bucket.get(reason, 0) + count

    def frozen(self) -> dict[str, Mapping[str, int]]:
        """Return the positive counts per section, reasons in :data:`SUPPRESSION_REASONS` order."""
        result: dict[str, Mapping[str, int]] = {}
        for section in SECTION_ORDER:
            bucket = self._counts[section]
            ordered = sorted(
                (reason for reason, count in bucket.items() if count > 0),
                key=lambda reason: (_REASON_ORDER.get(reason, len(_REASON_ORDER)), reason),
            )
            result[section] = MappingProxyType({reason: bucket[reason] for reason in ordered})
        return result


@dataclass(frozen=True, slots=True)
class _Collected:
    """Step 1's merged inputs.

    Attributes:
        candidates: Every candidate, in the order the results were given.
        findings: Every rule finding per section.
        withheld: Every withheld pattern per section.
    """

    candidates: tuple[Candidate, ...]
    findings: Mapping[str, tuple[Finding, ...]]
    withheld: Mapping[str, tuple[Withheld, ...]]


def _result_section(result: RuleResult) -> str:
    """Return the one section a rule result's own ``suppressed`` counts belong to.

    ``RuleResult.suppressed`` carries no section, while ``run_suppression`` stores counts per
    section (AAP 0.5.3). Every rule module evaluates rules of exactly one section, so the
    section is the one its candidates, findings and withheld lines name.

    Raises:
        ValueError: When the result names no section or several, so its counts cannot be
            attributed. Counts are never filed under a guessed section (AAP 0.11.1: no silent
            defaults).
    """
    sections = {
        *(candidate.section for candidate in result.candidates),
        *(finding.section for finding in result.findings),
        *(item.section for item in result.withheld),
    }
    if len(sections) != 1:
        found = ", ".join(sorted(sections)) or "none"
        raise ValueError(
            "a RuleResult's suppressed counts must belong to exactly one section; its "
            f"candidates, findings and withheld lines name {found} "
            f"(counts {dict(result.suppressed)})"
        )
    return next(iter(sections))


def _collect(results: Iterable[RuleResult], counts: _Counts) -> _Collected:
    """Step 1: merge every rule result and carry its own suppression counts by section.

    Raises:
        TypeError: When an element is not a ``RuleResult``.
        ValueError: When a result's suppression counts cannot be attributed to one section.
    """
    candidates: list[Candidate] = []
    findings: dict[str, list[Finding]] = {section: [] for section in SECTION_ORDER}
    withheld: dict[str, list[Withheld]] = {section: [] for section in SECTION_ORDER}
    for result in results:
        if not isinstance(result, RuleResult):
            raise TypeError(f"apply_precedence takes RuleResult values, found {result!r}")
        candidates.extend(result.candidates)
        for finding in result.findings:
            findings[finding.section].append(finding)
        for item in result.withheld:
            withheld[item.section].append(item)
        if result.suppressed:
            section = _result_section(result)
            for reason, count in result.suppressed.items():
                counts.add(section, reason, count)
    return _Collected(
        candidates=tuple(candidates),
        findings=MappingProxyType(
            {
                section: tuple(sorted(items, key=_finding_order))
                for section, items in findings.items()
            }
        ),
        withheld=MappingProxyType(
            {
                section: tuple(sorted(items, key=_withheld_order))
                for section, items in withheld.items()
            }
        ),
    )


# ---------------------------------------------------------------------------
# Step 2: the G1 guard (AAP 0.5.4.2 G1, FR-6)
# ---------------------------------------------------------------------------


def apply_g1(
    candidates: Sequence[Candidate], blocked: frozenset[SubjectKey]
) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
    """The G1 filtering step: drop every spend increase on a blocked target.

    AAP 0.5.4.2 G1: spend increases are blocked on any target that meets the B2 condition,
    and on a shared budget or any of its members when the budget's aggregate meets B2 or a
    member meets the member-level safety predicate. ``blocked`` is that set
    (:func:`~ads_engine.analysis.metrics.spend_increase_blocked`); every candidate declares
    the campaigns and budgets whose spend it would raise (``increases_spend_on``: a C2 Scale,
    a B1, a W1 double down), and one whose targets meet ``blocked`` is removed whole.

    This is the step a test replaces to bypass the guard: :func:`apply_precedence` resolves
    it at call time and checks the outcome independently (:func:`_check_g1`).

    Args:
        candidates: Every candidate of the run.
        blocked: The subjects no spend increase may target.

    Returns:
        ``(kept, removed)``, each in input order.
    """
    kept: list[Candidate] = []
    removed: list[Candidate] = []
    for candidate in candidates:
        if candidate.spend_targets & blocked:
            removed.append(candidate)
        else:
            kept.append(candidate)
    return tuple(kept), tuple(removed)


def _check_g1(candidates: Sequence[Candidate], blocked: frozenset[SubjectKey]) -> None:
    """The G1 post-check: no surviving candidate may raise spend on a blocked target.

    Two independent tests run. The declared targets of every survivor must miss ``blocked``.
    And no survivor may carry ``evidence.extra.g1_blocked``: ``campaigns.scale_outcomes``
    marks a C2 Scale and its B1 that way when it built them against the same memoised G1 set,
    so an increase the rules saw as blocked still trips the check when the predicate is
    replaced after the candidates were built.

    Raises:
        PrecedenceInvariantError: When an increase survives on a blocked target.
    """
    for candidate in candidates:
        overlap = candidate.spend_targets & blocked
        if overlap:
            raise PrecedenceInvariantError(
                f"G1: {candidate.rule_id} on {candidate.entity_ref.key} still raises spend on "
                f"blocked targets {sorted(overlap)}"
            )
        if candidate.evidence.extra.get("g1_blocked") is True and candidate.increases_spend_on:
            raise PrecedenceInvariantError(
                f"G1: {candidate.rule_id} on {candidate.entity_ref.key} was built as a "
                "G1-blocked spend increase but survived the guard"
            )


# ---------------------------------------------------------------------------
# Step 3: B3 headroom allocation to surviving B1 receivers (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def allocate_b3(
    donors: Sequence[tuple[SubjectKey, Decimal]],
    receivers: Sequence[tuple[SubjectKey, Decimal]],
) -> tuple[Allocation, ...]:
    """The B3 allocation step: fund B1 increases from B3 freed amounts, deterministically.

    AAP 0.5.4.2 "B3 reallocation, deterministic allocation", applied to one source and
    currency. Donors are taken in the order given (freed amount descending, then entity name,
    then entity id) and receivers in B1 rank order (cost per outcome ascending, then name,
    then id); the entity key breaks only a tie that name and id leave. Each donor gives
    ``min(its remaining freed amount, the receiver's remaining headroom)`` to the first
    receiver with headroom left, then moves on to the next receiver until its freed amount
    or all headroom is used up. The sum of all reallocations into a receiver therefore never
    exceeds its B1 increase, and freed amount left over stays a plain decrease.

    This is the step a test replaces to over-allocate: :func:`apply_precedence` resolves it
    at call time and checks the result (:func:`_check_allocations`).

    Args:
        donors: ``(B3 subject, freed amount)``, in donor order.
        receivers: ``(B1 subject, B1 increase)``, in receiver order.

    Returns:
        The allocations, in the order they were made.

    Example:
        >>> a, b, r = ("campaign", 1), ("campaign", 2), ("campaign", 3)
        >>> [
        ...     x.amount
        ...     for x in allocate_b3([(a, Decimal(300)), (b, Decimal(200))], [(r, Decimal(400))])
        ... ]
        [Decimal('300'), Decimal('100')]
    """
    keys = [key for key, _ in receivers]
    headroom = [amount for _, amount in receivers]
    allocations: list[Allocation] = []
    for donor, freed in donors:
        remaining = freed
        for position, receiver in enumerate(keys):
            if remaining <= 0:
                break
            left = headroom[position]
            if receiver == donor or left <= 0:
                continue
            amount = min(remaining, left)
            allocations.append(Allocation(donor=donor, receiver=receiver, amount=amount))
            headroom[position] = left - amount
            remaining -= amount
    return tuple(allocations)


def _check_allocations(
    allocations: Sequence[Allocation],
    donors: Sequence[tuple[SubjectKey, Decimal]],
    receivers: Sequence[tuple[SubjectKey, Decimal]],
    blocked: frozenset[SubjectKey],
) -> None:
    """The allocation post-check (the AAP 0.5.4.2 runner assertion).

    Every allocation must be an :class:`Allocation` of a positive amount between a known
    donor and a known receiver of the same cohort, never into a G1-blocked target, never a
    subject into itself; the sum into each receiver must not exceed its B1 increase and the
    sum out of each donor must not exceed its freed amount.

    Raises:
        PrecedenceInvariantError: When any of these fails.
    """
    donor_limits = dict(donors)
    receiver_limits = dict(receivers)
    given: dict[SubjectKey, Decimal] = {}
    received: dict[SubjectKey, Decimal] = {}
    for allocation in allocations:
        if not isinstance(allocation, Allocation):
            raise PrecedenceInvariantError(f"B3: {allocation!r} is not an Allocation")
        if allocation.donor not in donor_limits:
            raise PrecedenceInvariantError(f"B3: {allocation.donor} is not a B3 donor here")
        if allocation.receiver not in receiver_limits:
            raise PrecedenceInvariantError(
                f"B3: {allocation.receiver} is not a surviving B1 receiver in the donor's cohort"
            )
        if allocation.receiver in blocked:
            raise PrecedenceInvariantError(f"B3: {allocation.receiver} is G1-blocked")
        if allocation.donor == allocation.receiver:
            raise PrecedenceInvariantError(f"B3: {allocation.donor} cannot fund itself")
        if allocation.amount <= 0:
            raise PrecedenceInvariantError(
                f"B3: allocation {allocation.amount} from {allocation.donor} is not positive"
            )
        given[allocation.donor] = given.get(allocation.donor, Decimal(0)) + allocation.amount
        received[allocation.receiver] = (
            received.get(allocation.receiver, Decimal(0)) + allocation.amount
        )
    for receiver, total in received.items():
        if total > receiver_limits[receiver]:
            raise PrecedenceInvariantError(
                f"B3: allocations of {total} into {receiver} exceed its B1 increase of "
                f"{receiver_limits[receiver]}"
            )
    for donor, total in given.items():
        if total > donor_limits[donor]:
            raise PrecedenceInvariantError(
                f"B3: allocations of {total} from {donor} exceed its freed amount of "
                f"{donor_limits[donor]}"
            )


def _positive_amount(candidate: Candidate, key: str) -> Decimal | None:
    """The positive ``Decimal`` at ``evidence.extra[key]``, or ``None`` when there is none."""
    value = candidate.evidence.extra.get(key)
    if isinstance(value, Decimal) and value.is_finite() and value > 0:
        return value
    return None


def _candidate_currency(ctx: AnalysisContext, candidate: Candidate) -> str | None:
    """The currency of a budget item: ``extra.currency``, else its decision subject's."""
    value = candidate.evidence.extra.get("currency")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    subject = candidate.decision_subject
    if subject is None:
        return None
    if subject.type == "campaign":
        return ctx.account_currency(subject.id) if subject.id in ctx.campaigns else None
    found = ctx.budgets.get(subject.id)
    return None if found is None else found.currency


def _cost_per_outcome(candidate: Candidate) -> Decimal | None:
    """A B1 receiver's cost per outcome (its ``cost_per_outcome`` metric), ``None`` if absent."""
    for metric in candidate.evidence.metrics:
        if metric.key == "cost_per_outcome":
            value = metric.value
            return value if isinstance(value, Decimal) else None
    return None


@dataclass(frozen=True, slots=True)
class _Party:
    """A B3 donor or B1 receiver within one (source, currency) cohort."""

    subject: SubjectKey
    candidate: Candidate
    amount: Decimal
    order: tuple[Any, ...]


def _parties(
    ctx: AnalysisContext, candidates: Sequence[Candidate], rule_id: str, amount_key: str
) -> dict[tuple[str, str], list[_Party]]:
    """The B3 donors (``freed``) or B1 receivers (``increase``) per (source, currency).

    Donors order by freed amount descending, then entity name, then entity id; receivers by
    cost per outcome ascending (an unavailable cost last), then name, then id (AAP 0.5.4.2).
    Name, id and key are the tie-breaks of :attr:`Candidate.sort_key`: the numeric internal
    id (entities with an id first), with the entity key only as the final fallback. A
    subject listed twice keeps its first item, so no subject is funded or drawn on twice.
    """
    cohorts: dict[tuple[str, str], list[_Party]] = {}
    seen: set[SubjectKey] = set()
    for candidate in candidates:
        if candidate.rule_id != rule_id or candidate.decision_subject is None:
            continue
        amount = _positive_amount(candidate, amount_key)
        currency = _candidate_currency(ctx, candidate)
        subject = candidate.decision_subject.key
        if amount is None or currency is None or subject in seen:
            continue
        seen.add(subject)
        _, name, id_slot, key = candidate.sort_key
        if rule_id == B3:
            order: tuple[Any, ...] = (_orderable(-amount), name, id_slot, key)
        else:
            order = (_orderable(_cost_per_outcome(candidate)), name, id_slot, key)
        cohort = (candidate.entity_ref.source, currency)
        cohorts.setdefault(cohort, []).append(_Party(subject, candidate, amount, order))
    for parties in cohorts.values():
        parties.sort(key=lambda party: party.order)
    return cohorts


def _subject_json(key: SubjectKey) -> dict[str, Any]:
    return {"type": key[0], "id": key[1]}


def _partner_subjects(
    ctx: AnalysisContext, index: _EntityIndex, partners: Iterable[SubjectKey]
) -> tuple[DecisionSubject, ...]:
    """The subjects an item's allocation partners add to its ``depends_on`` (AAP 0.5.4.2 P1).

    A B3 card tells the user to move money to its receivers and a B1 card counts on its
    donors' money, so a discrepancy on a partner holds the card exactly as one on a subject
    the card is itself about: each partner is listed, and a budget partner brings every
    member campaign (:func:`_budget_members`); a campaign partner's own budget is added by
    the P1 expansion (:func:`_expanded_depends_on`), as for every campaign listed. Partners
    of the partner are not followed: a donor depends on its receiver, never on the
    receiver's other donors.
    """
    ordered: dict[SubjectKey, None] = {}
    for key in partners:
        ordered.setdefault(key, None)
        if key[0] == "budget":
            for campaign_id in _budget_members(ctx, index, key[1]):
                ordered.setdefault(("campaign", campaign_id), None)
    return tuple(DecisionSubject.of(key) for key in ordered)


def _with_b1_funding(
    candidate: Candidate,
    funding: Sequence[tuple[Allocation, str]],
    currency: str,
    partners: Sequence[DecisionSubject],
) -> Candidate:
    """Re-render a surviving B1 with how its increase is funded (AAP 0.5.4.2).

    ``extra.funding`` always holds ``budget.render_b1_funding``'s sentence ("Raise by
    {increase}: {Σ allocations} funded by reallocation from {donors}, {remainder} from new
    budget"), with ``allocations``, ``funded_by_reallocation`` and ``from_new_budget`` beside
    it; the reason gains the sentence when some of the increase is funded by reallocation.
    The action ("Raise daily budget from … to …") is unchanged: it prefills the decision.
    ``partners`` (:func:`_partner_subjects` of the funding donors) are appended to
    ``depends_on``, so a discrepancy on a donor holds the B1 it funds.
    """
    increase = candidate.evidence.extra["increase"]
    # Starting from ``increase - increase`` (zero at the increase's six-place scale, as B3
    # starts from ``freed - freed``) keeps an unfunded receiver's amounts at the same scale as
    # a funded one's: ``0.000000``, never ``0``.
    funded = sum((allocation.amount for allocation, _ in funding), increase - increase)
    sentence = budget.render_b1_funding(
        increase, [(name, allocation.amount) for allocation, name in funding], currency
    )
    evidence = candidate.evidence.with_extra(
        allocations=tuple(
            {
                "donor": _subject_json(allocation.donor),
                "donor_name": name,
                "amount": allocation.amount,
            }
            for allocation, name in funding
        ),
        funded_by_reallocation=funded,
        from_new_budget=increase - funded,
        funding=sentence,
    )
    reason = f"{candidate.reason} {sentence}." if funded > 0 else candidate.reason
    depends_on = (*candidate.depends_on, *partners)
    return replace(candidate, evidence=evidence, reason=reason, depends_on=depends_on)


def _with_b3_allocations(
    candidate: Candidate,
    given: Sequence[tuple[Allocation, str]],
    currency: str,
    partners: Sequence[DecisionSubject],
) -> Candidate:
    """Re-render a B3 donor with each allocation as funding for a named B1 (AAP 0.5.4.2).

    ``extra.allocations`` lists ``{receiver, receiver_name, amount, text}`` per allocation,
    each ``text`` from ``budget.render_b3_allocation`` ("Move {allocation} to fund
    {receiver}'s recommended increase (B1)"), and ``allocated`` / ``unallocated`` split the
    freed amount; the unallocated part stays a plain decrease. A donor with no allocation
    keeps ``allocations`` empty and its reason unchanged. ``partners``
    (:func:`_partner_subjects` of the funded receivers) are appended to ``depends_on``, so a
    discrepancy on a receiver holds the whole card that moves money to it.
    """
    freed = candidate.evidence.extra["freed"]
    allocated = sum((allocation.amount for allocation, _ in given), freed - freed)
    lines = [
        budget.render_b3_allocation(allocation.amount, name, currency) for allocation, name in given
    ]
    extra: dict[str, Any] = {"allocated": allocated, "unallocated": freed - allocated}
    if given:
        extra["allocations"] = tuple(
            {
                "receiver": _subject_json(allocation.receiver),
                "receiver_name": name,
                "amount": allocation.amount,
                "text": line,
            }
            for (allocation, name), line in zip(given, lines, strict=True)
        )
    evidence = candidate.evidence.with_extra(**extra)
    reason = f"{candidate.reason} {'; '.join(lines)}." if lines else candidate.reason
    depends_on = (*candidate.depends_on, *partners)
    return replace(candidate, evidence=evidence, reason=reason, depends_on=depends_on)


def _reallocate(
    ctx: AnalysisContext,
    candidates: Sequence[Candidate],
    blocked: frozenset[SubjectKey],
    index: _EntityIndex,
) -> tuple[list[Candidate], tuple[Allocation, ...]]:
    """Step 3: allocate B3 freed amounts to the B1 items that survived G1, then re-render.

    Receivers are the campaigns or shared budgets with a surviving B1 item in the same source
    and currency as the donor, each with its B1 increase (``extra.increase``) as headroom;
    donors are the B3 items with their freed amount (``extra.freed``). Cohorts are visited in
    (source, currency) order, so the result never depends on the order of the rule results.

    Each re-rendered card depends on its allocation partners (:func:`_partner_subjects`): a
    donor on every receiver it funds, a receiver on every donor funding it. P1 (step 4) then
    holds the whole card when a partner is discrepant, and freezes the partners in
    ``extra.depends_on`` for the read-time overlay.

    A receiver held later by P1 (step 4) or hidden by feedback (step 10) keeps its
    allocation: AAP 0.5.4.2 defines receivers as the B1 items that survive G1 and runs holds
    and feedback after the allocation, so the funding a card shows never depends on the
    user's dismissals or the current discrepancies.

    Raises:
        PrecedenceInvariantError: When an allocation breaks :func:`_check_allocations`.
    """
    donors = _parties(ctx, candidates, B3, "freed")
    receivers = _parties(ctx, candidates, B1, "increase")
    allocations: list[Allocation] = []
    for cohort in sorted(donors):
        donor_amounts = [(party.subject, party.amount) for party in donors[cohort]]
        receiver_amounts = [(party.subject, party.amount) for party in receivers.get(cohort, [])]
        if not receiver_amounts:
            continue
        # Resolved at call time on purpose: the allocation step is a documented test seam.
        made = allocate_b3(donor_amounts, receiver_amounts)
        _check_allocations(made, donor_amounts, receiver_amounts, blocked)
        allocations.extend(made)

    names: dict[SubjectKey, str] = {}
    currencies: dict[SubjectKey, str] = {}
    for cohort, parties in (*donors.items(), *receivers.items()):
        for party in parties:
            names[party.subject] = party.candidate.entity_ref.name
            currencies[party.subject] = cohort[1]
    # Keyed by identity: a candidate holds read-only mappings, so it is not hashable.
    donor_items = {
        id(party.candidate): party.subject for parties in donors.values() for party in parties
    }
    receiver_items = {
        id(party.candidate): party.subject for parties in receivers.values() for party in parties
    }
    result: list[Candidate] = []
    for candidate in candidates:
        if id(candidate) in receiver_items:
            subject = receiver_items[id(candidate)]
            funding = [(a, names[a.donor]) for a in allocations if a.receiver == subject]
            donors_of = _partner_subjects(ctx, index, (a.donor for a, _ in funding))
            result.append(_with_b1_funding(candidate, funding, currencies[subject], donors_of))
        elif id(candidate) in donor_items:
            subject = donor_items[id(candidate)]
            given = [(a, names[a.receiver]) for a in allocations if a.donor == subject]
            funded = _partner_subjects(ctx, index, (a.receiver for a, _ in given))
            result.append(_with_b3_allocations(candidate, given, currencies[subject], funded))
        else:
            result.append(candidate)
    return result, tuple(allocations)


# ---------------------------------------------------------------------------
# Step 4: P1 discrepancies first (AAP 0.5.4.2 P1)
# ---------------------------------------------------------------------------


def _first_part(external_id: str) -> str:
    return external_id.split("|", 1)[0]


@dataclass(frozen=True, slots=True)
class _EntityIndex:
    """Lookups from an item's entity to the campaigns behind it, built once per run.

    Attributes:
        ad_group_campaigns: Google ad group external id → its campaigns.
        campaigns_by_external: ``(source, campaign external id)`` → campaign ids.
        creative_campaigns: LinkedIn creative URN → its campaigns.
        budget_members: Budget id → the campaigns drawing from it now.
    """

    ad_group_campaigns: Mapping[str, tuple[int, ...]]
    campaigns_by_external: Mapping[tuple[str, str], tuple[int, ...]]
    creative_campaigns: Mapping[str, tuple[int, ...]]
    budget_members: Mapping[int, tuple[int, ...]]

    @classmethod
    def of(cls, ctx: AnalysisContext) -> _EntityIndex:
        """Index the context's ad groups, campaigns, creatives and budget memberships."""
        ad_groups: dict[str, set[int]] = {}
        for ad_group in ctx.ad_groups.values():
            ad_groups.setdefault(ad_group.external_id, set()).add(ad_group.campaign_id)
        campaigns: dict[tuple[str, str], set[int]] = {}
        members: dict[int, set[int]] = {}
        for campaign in ctx.campaigns.values():
            campaigns.setdefault((campaign.source, campaign.external_id), set()).add(campaign.id)
            if campaign.budget_id is not None:
                members.setdefault(campaign.budget_id, set()).add(campaign.id)
        creatives: dict[str, set[int]] = {}
        for creative in ctx.creatives.values():
            creatives.setdefault(creative.urn, set()).add(creative.campaign_id)

        def frozen[K](found: Mapping[K, set[int]]) -> Mapping[K, tuple[int, ...]]:
            return MappingProxyType({key: tuple(sorted(ids)) for key, ids in found.items()})

        return cls(frozen(ad_groups), frozen(campaigns), frozen(creatives), frozen(members))


def _entity_campaigns(ctx: AnalysisContext, index: _EntityIndex, ref: EntityRef) -> tuple[int, ...]:
    """The campaigns an item's entity belongs to (AAP 0.5.4.2 P1 "its keywords and creatives").

    A campaign is itself; a keyword, ad group or creative belongs to its campaign; a search
    term to the campaign of its ad group; a demographic segment to its campaign; a CR1 pair
    to the campaign of its creatives. Budgets, white papers, term clusters and accounts name
    no campaign of their own: their rules list the campaigns they read in ``depends_on``.
    """
    kind = ref.type
    if kind == ENTITY_CAMPAIGN:
        if ref.id is not None:
            return (ref.id,)
        return index.campaigns_by_external.get((ref.source, ref.external_id), ())
    if kind == ENTITY_KEYWORD:
        keyword = None if ref.id is None else ctx.keywords.get(ref.id)
        if keyword is not None:
            return (keyword.campaign_id,)
        return index.ad_group_campaigns.get(_first_part(ref.external_id), ())
    if kind == ENTITY_AD_GROUP:
        ad_group = None if ref.id is None else ctx.ad_groups.get(ref.id)
        if ad_group is not None:
            return (ad_group.campaign_id,)
        return index.ad_group_campaigns.get(ref.external_id, ())
    if kind == ENTITY_SEARCH_TERM:
        return index.ad_group_campaigns.get(_first_part(ref.external_id), ())
    if kind == ENTITY_CREATIVE:
        creative = None if ref.id is None else ctx.creatives.get(ref.id)
        if creative is not None:
            return (creative.campaign_id,)
        return index.creative_campaigns.get(ref.external_id, ())
    if kind == ENTITY_SEGMENT:
        return index.campaigns_by_external.get((ref.source, _first_part(ref.external_id)), ())
    if kind == ENTITY_CREATIVE_PAIR:
        found: set[int] = set()
        for urn in ref.external_id.split("|"):
            found.update(index.creative_campaigns.get(urn, ()))
        return tuple(sorted(found))
    return ()


def _budget_members(ctx: AnalysisContext, index: _EntityIndex, budget_id: int) -> tuple[int, ...]:
    """Every campaign of a budget, ascending.

    Those drawing from it now, plus, for an explicitly shared budget, those with member days
    in the window (``metrics.subject_campaigns``).
    """
    members = set(index.budget_members.get(budget_id, ()))
    found = ctx.budgets.get(budget_id)
    if found is not None and found.explicitly_shared:
        members.update(metrics.subject_campaigns(ctx, ("budget", budget_id)))
    return tuple(sorted(members))


def _expanded_depends_on(
    ctx: AnalysisContext, index: _EntityIndex, candidate: Candidate
) -> tuple[SubjectKey, ...]:
    """Every subject whose unresolved discrepancy holds the item (AAP 0.5.4.2 P1).

    Seeds, in order: the item's ``depends_on`` (the rule's own, followed on a B1 or B3 card
    by the allocation partners step 3 added: each donor funding a B1, each receiver a B3
    funds, a budget partner with its member campaigns; :func:`_partner_subjects`), the
    item's ``decision_subject`` (a decision on the item would write it), its
    ``increases_spend_on`` targets, its own budget entity and the campaigns behind its
    entity (:func:`_entity_campaigns`). Then each seeded campaign adds the budget it draws
    from, which for a shared-budget member is the shared budget; and each budget the item is
    itself about (its entity, decision subject or spend target) adds every member campaign
    (:func:`_budget_members`).

    The member expansion is deliberately not transitive: a campaign's item depends on its
    shared budget, but not on the budget's other members, because a sibling's discrepancy
    says nothing about this campaign's own keywords, creatives or pause.
    """
    seeds: list[SubjectKey] = [subject.key for subject in candidate.depends_on]
    own_budgets: list[int] = []
    if candidate.decision_subject is not None:
        seeds.append(candidate.decision_subject.key)
        if candidate.decision_subject.type == "budget":
            own_budgets.append(candidate.decision_subject.id)
    for target in candidate.increases_spend_on:
        seeds.append(target.key)
        if target.type == "budget":
            own_budgets.append(target.id)
    ref = candidate.entity_ref
    if ref.type == ENTITY_BUDGET and ref.id is not None:
        seeds.append(("budget", ref.id))
        own_budgets.append(ref.id)
    seeds.extend(("campaign", campaign_id) for campaign_id in _entity_campaigns(ctx, index, ref))

    ordered: dict[SubjectKey, None] = {}
    for key in seeds:
        ordered.setdefault(key, None)
        if key[0] == "campaign":
            campaign = ctx.campaigns.get(key[1])
            if campaign is not None and campaign.budget_id is not None:
                ordered.setdefault(("budget", campaign.budget_id), None)
    for budget_id in dict.fromkeys(own_budgets):
        for campaign_id in _budget_members(ctx, index, budget_id):
            ordered.setdefault(("campaign", campaign_id), None)
    return tuple(ordered)


def _merged_flags(flags: tuple[Flag, ...], flag: Flag) -> tuple[Flag, ...]:
    """Add ``flag``; a code already present keeps one badge whose message gains the new text.

    A candidate keeps one flag per code (the first message wins on construction), so a
    precedence flag whose code a rule already set, such as K1's own baseline ``incomplete``,
    is merged into that badge rather than lost. ``flags`` itself is returned when its badge
    already carries the message, and a new code is appended last.
    """
    for position, existing in enumerate(flags):
        if existing.code != flag.code:
            continue
        if flag.message in existing.message:
            return flags
        merged = Flag(flag.code, f"{existing.message}; {flag.message}")
        return (*flags[:position], merged, *flags[position + 1 :])
    return (*flags, flag)


def _apply_p1(
    ctx: AnalysisContext, candidates: Sequence[Candidate], index: _EntityIndex
) -> list[Candidate]:
    """Step 4: expand ``depends_on``, hold dependent items, pin C0, flag the rest.

    * Every item's expanded subjects (:func:`_expanded_depends_on`) are stored as
      ``Candidate.depends_on`` and frozen as ``evidence.extra.depends_on`` (``[{type, id}]``),
      which the read-time overlay reads to hold items on discrepancies raised after the run.
      A B1 or B3 card's subjects include its allocation partners (step 3), so a discrepancy
      on a receiver holds every donor card funding it and one on a donor holds the B1 it
      funds, at compute time and at read time alike.
    * A non-C0 item depending on a subject with an unresolved discrepancy is held with
      ``"Resolve discrepancy first"`` and lists ``extra.blocking_discrepancies`` (ids
      ascending); a hold a rule set itself keeps its reason.
    * C0 items are exempt from holds and pinned (ranked first, oldest first, in step 11).
    * While any discrepancy is unresolved, every non-C0 item, held or not, carries
      ``unverified_discrepancies`` with the count of all of them, exactly as the overlay
      flags items at read time.
    """
    by_subject: dict[SubjectKey, list[int]] = {}
    for item in ctx.discrepancies:
        by_subject.setdefault(item.key, []).append(item.id)
    open_count = len(ctx.discrepancies)
    unverified = (
        Flag(FLAG_UNVERIFIED_DISCREPANCIES, unverified_message(open_count)) if open_count else None
    )
    result: list[Candidate] = []
    for candidate in candidates:
        depends = _expanded_depends_on(ctx, index, candidate)
        subjects = tuple(DecisionSubject.of(key) for key in depends)
        extra: dict[str, Any] = {"depends_on": [_subject_json(key) for key in depends]}
        if candidate.is_discrepancy:
            evidence = candidate.evidence.with_extra(**extra)
            result.append(replace(candidate, evidence=evidence, depends_on=subjects, pinned=True))
            continue
        blocking = sorted({did for key in depends for did in by_subject.get(key, ())})
        # One copy per item: the unverified badge is merged into the same replace.
        flags = candidate.flags
        if unverified is not None:
            flags = _merged_flags(flags, unverified)
        if blocking:
            extra["blocking_discrepancies"] = blocking
            reason = candidate.held_reason if candidate.held else HELD_DISCREPANCY_REASON
            updated = replace(
                candidate,
                evidence=candidate.evidence.with_extra(**extra),
                depends_on=subjects,
                held=True,
                held_reason=reason,
                flags=flags,
            )
        else:
            updated = replace(
                candidate,
                evidence=candidate.evidence.with_extra(**extra),
                depends_on=subjects,
                flags=flags,
            )
        result.append(updated)
    return result


# ---------------------------------------------------------------------------
# Step 5: P2 conversion tracking absent (AAP 0.5.4.2 P2)
# ---------------------------------------------------------------------------


def _tracking_suppressed(candidate: Candidate, sources: frozenset[str]) -> bool:
    """Whether P2 suppresses ``candidate``.

    It does for K1, C1, B2's zero-outcome branch and W2 of a source whose tracking is absent.
    """
    if candidate.entity_ref.source not in sources:
        return False
    if candidate.rule_id in (K1, C1, W2):
        return True
    return (
        candidate.rule_id == B2 and candidate.evidence.extra.get("branch") == _ZERO_OUTCOMES_BRANCH
    )


def _window_spend_by_currency(ctx: AnalysisContext, source: str) -> dict[str, Decimal]:
    """A source's window spend per account currency; amounts are never summed across them."""
    window = ctx.window
    spend: dict[str, Decimal] = {}
    for campaign in ctx.campaigns.values():
        if campaign.source != source:
            continue
        total = sum(
            (
                day.cost
                for day in ctx.campaign_days.get(campaign.id, ())
                if window.start <= day.date <= window.end
            ),
            Decimal(0),
        )
        if total > 0:
            currency = ctx.account_currency(campaign.id)
            spend[currency] = spend.get(currency, Decimal(0)) + total
    return dict(sorted(spend.items()))


def _apply_p2(
    ctx: AnalysisContext, candidates: Sequence[Candidate], counts: _Counts
) -> tuple[list[Candidate], dict[str, tuple[Finding, ...]]]:
    """Step 5: suppress zero-outcome rules of a source that recorded no outcome at all.

    AAP 0.5.4.2 P2: a source with spend > 0 and zero outcomes across the window
    (``metrics.source_totals``) gets a ``conversion_tracking_absent`` finding, "No
    conversions or leads recorded — verify tracking before acting on zero-outcome rules", in
    each section it affects (:data:`_TRACKING_SECTIONS`), and its K1, C1, zero-outcome B2 and
    W2 items are suppressed, counted ``tracking_absent`` in their sections. The finding's
    evidence names the source (which the overlay's tracking banner reads), its spend per
    currency, the rules suppressed in that section and how many items were.
    """
    totals = metrics.source_totals(ctx)
    absent = {
        source: outcomes
        for source, (spend, outcomes) in totals.items()
        if spend > 0 and outcomes == 0 and source in _TRACKING_SECTIONS
    }
    findings: dict[str, list[Finding]] = {section: [] for section in SECTION_ORDER}
    if not absent:
        return list(candidates), dict.fromkeys(SECTION_ORDER, ())
    absent_sources: frozenset[str] = frozenset(absent)
    kept: list[Candidate] = []
    suppressed: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        if _tracking_suppressed(candidate, absent_sources):
            counts.add(candidate.section, SUPPRESS_TRACKING_ABSENT)
            key = (candidate.entity_ref.source, candidate.section)
            suppressed[key] = suppressed.get(key, 0) + 1
        else:
            kept.append(candidate)
    window = ctx.window
    for source in sorted(absent, key=_source_order):
        spend = _window_spend_by_currency(ctx, source)
        for section, rules in _TRACKING_SECTIONS[source]:
            findings[section].append(
                Finding(
                    code=FINDING_CONVERSION_TRACKING_ABSENT,
                    section=section,
                    entity=None,
                    message=TRACKING_ABSENT_MESSAGE,
                    evidence={
                        "source": source,
                        "source_label": _SOURCE_LABELS[source],
                        "spend": spend,
                        "outcomes": absent[source],
                        "window": {
                            "start": window.start,
                            "end": window.end,
                            "days": window.days,
                        },
                        "suppressed_rules": list(rules),
                        "suppressed_items": suppressed.get((source, section), 0),
                    },
                )
            )
    return kept, {section: tuple(items) for section, items in findings.items()}


# ---------------------------------------------------------------------------
# Steps 6 and 7: P3 simulated data and P4 stale / incomplete (AAP 0.5.4.2, 0.3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Check:
    """One required dataset of one item, judged at compute time (P4).

    Attributes:
        name: The dataset name.
        dataset_id: The dataset the name resolves to for this item; ``None`` when none does.
        row: Its ``dataset_freshness`` row; ``None`` when there is none.
        start: The first date the dataset must cover completely: the window start, or the
            earlier start of the rule's lookback (:data:`_RULE_LOOKBACKS`).
        start_label: How reasons name ``start``.
        stale: Old (``sources.stale_after_hours``), never succeeded, or covering less than
            the window end.
        incomplete: Not ``complete`` over ``[start, window end]``.
    """

    name: str
    dataset_id: int | None
    row: Freshness | None
    start: date
    start_label: str
    stale: bool
    incomplete: bool


def _linkedin_scoped(candidate: Candidate) -> bool:
    """Whether an item is LinkedIn-attributed (AAP 0.5.4.1, 0.3.3).

    Its ``hubspot.*`` datasets are then those of the effective CRM dataset of the LinkedIn
    dataset in use: the companion while LinkedIn is simulated.
    """
    return any(entry.source == "linkedin" for entry in candidate.evidence.provenance) or any(
        name.startswith("linkedin.") for name in candidate.required_datasets
    )


def _dataset_id(
    ctx: AnalysisContext, candidate: Candidate, name: str, *, linkedin_scoped: bool
) -> int | None:
    """Resolve a required dataset name to the dataset the item read, as its provenance says.

    ``google.*`` and ``linkedin.*`` resolve to their source's active primary dataset. A
    ``hubspot.*`` name resolves to the effective CRM dataset for a LinkedIn-attributed item
    and to HubSpot's primary otherwise (``ctx.dataset_id_for``); when the frozen provenance
    records a different HubSpot dataset, the recorded one is judged, since that is the one
    the item's numbers came from.
    """
    if not name.startswith("hubspot."):
        return ctx.dataset_id_for(name, crm_scope=None)
    recorded = {
        entry.dataset_id for entry in candidate.evidence.provenance if entry.source == "hubspot"
    }
    preferred = ctx.dataset_id_for(name, crm_scope="linkedin" if linkedin_scoped else None)
    if preferred is not None and (not recorded or preferred in recorded):
        return preferred
    for scope in ("linkedin", "google", None):
        option = ctx.dataset_id_for(name, crm_scope=scope)
        if option is not None and option in recorded:
            return option
    if len(recorded) == 1:
        return next(iter(recorded))
    return preferred


def _lookback_days(thresholds: Thresholds, rule_id: str) -> int:
    """The configured length of a rule's lookback (:data:`_LOOKBACK_DAYS_KEYS`).

    Raises:
        ConfigError: When the key is missing from the configuration.
    """
    return thresholds.int(_LOOKBACK_DAYS_KEYS[rule_id])


def _frozen_start(value: object) -> date | None:
    """The ``start`` native date of a frozen lookback span, or ``None`` when unusable."""
    start = value.get("start") if isinstance(value, Mapping) else None
    if isinstance(start, datetime):
        return None
    if isinstance(start, date):
        return start
    if isinstance(start, str):
        try:
            return date.fromisoformat(start)
        except ValueError:
            return None
    return None


def _days_back(end: date, days: int) -> date:
    """The first day of the ``days``-day span ending on ``end`` (``date.min`` on overflow)."""
    try:
        return end - timedelta(days=days - 1)
    except OverflowError:
        return date.min


def _required_start(ctx: AnalysisContext, candidate: Candidate, name: str) -> tuple[date, str]:
    """The first date a required dataset must cover for this item, with its label.

    The window start, widened to an earlier start where the item's rule reads ``name``
    further back: a K1 item's ``google.metrics_daily`` must cover its frozen
    ``extra.baseline_window`` from its start. A missing or malformed start fails closed to
    the lookback's length counted back from the window end (the frozen
    ``evidence.thresholds`` value when a positive integer, else the configured one), never to
    the window start. The overlay judges the same range at read time.
    """
    window = ctx.window
    lookbacks = _RULE_LOOKBACKS.get(candidate.rule_id)
    extra_key = None if lookbacks is None else lookbacks.get(name)
    if extra_key is None:
        return window.start, _WINDOW_START_LABEL
    start = _frozen_start(candidate.evidence.extra.get(extra_key))
    if start is None:
        frozen = candidate.evidence.thresholds.get(_LOOKBACK_DAYS_KEYS[candidate.rule_id])
        if not isinstance(frozen, bool) and isinstance(frozen, int) and frozen >= 1:
            days = frozen
        else:
            days = _lookback_days(ctx.thresholds, candidate.rule_id)
        start = _days_back(window.end, days)
    if start >= window.start:
        return window.start, _WINDOW_START_LABEL
    lookback = extra_key.removesuffix("_window").replace("_", " ")
    return start, f"{candidate.rule_id} {lookback} start"


def _checks(
    ctx: AnalysisContext, candidate: Candidate, stale_after: timedelta
) -> tuple[_Check, ...]:
    """Judge each of the item's own required datasets (P4); no other dataset counts.

    A dataset with no freshness row, or that no dataset resolves to, has never succeeded:
    it is stale and incomplete, never silently fresh (AAP 0.11.1, the overlay's rule too).
    """
    scoped = _linkedin_scoped(candidate)
    window_end = ctx.window.end
    checks: list[_Check] = []
    for name in candidate.required_datasets:
        dataset_id = _dataset_id(ctx, candidate, name, linkedin_scoped=scoped)
        row = ctx.freshness_for(name, dataset_id)
        start, label = _required_start(ctx, candidate, name)
        if row is None:
            checks.append(_Check(name, dataset_id, None, start, label, stale=True, incomplete=True))
            continue
        checks.append(
            _Check(
                name,
                dataset_id,
                row,
                start,
                label,
                stale=row.is_stale(now=ctx.now, stale_after=stale_after, window_end=window_end),
                incomplete=not row.is_complete_over(window_start=start, window_end=window_end),
            )
        )
    return tuple(checks)


def _stale_flag(checks: Sequence[_Check], window_end: date, *, display_timezone: str) -> Flag:
    """The ``stale`` flag: "Data as of {oldest last success}", naming any coverage shortfall.

    The last success is an instant, rendered in the run's ``display_timezone``
    (:func:`~ads_engine.analysis.context.format_instant`, as the overlay renders its current
    flag); coverage ends are native dates and stay ISO dates (AAP 0.5.4.1).
    """
    never: list[str] = []
    successes: list[datetime] = []
    shortfalls: list[str] = []
    for check in checks:
        row = check.row
        if row is None or row.last_success_at is None:
            never.append(check.name)
            continue
        successes.append(row.last_success_at)
        if check.name in FULL_STATE_DATASETS:
            continue
        if row.covered_through is None:
            shortfalls.append(f"{check.name} covers no dates yet")
        elif row.covered_through < window_end:
            shortfalls.append(f"{check.name} covers through {row.covered_through.isoformat()}")
    if never:
        message = f"Data as of: no successful sync of {', '.join(dict.fromkeys(never))}"
    else:
        message = f"Data as of {format_instant(min(successes), display_timezone)}"
    if shortfalls:
        message = f"{message}; {'; '.join(shortfalls)}"
    return Flag(FLAG_STALE, message)


def _incomplete_reasons(check: _Check, window_end: date) -> tuple[str, ...]:
    """Why an incomplete dataset is not ``complete`` over its required range, each once.

    What the row lacks over that range comes first
    (:meth:`~ads_engine.analysis.context.Freshness.incomplete_reason_over`), so a dated Q-G6
    shortfall names only its days inside the range, then any coverage shortfall.
    """
    row = check.row
    if row is None:
        return ("no freshness record",)
    reasons: list[str] = []
    shortfall = row.incomplete_reason_over(window_start=check.start, window_end=window_end)
    if shortfall is not None:
        reasons.append(shortfall)
    if row.dataset in FULL_STATE_DATASETS:
        return tuple(reasons)
    if row.covered_from is None and row.covered_through is None:
        reasons.append("covers no dates")
        return tuple(reasons)
    if row.covered_from is None:
        reasons.append("records no coverage start")
    elif row.covered_from > check.start:
        reasons.append(
            f"covers from {row.covered_from.isoformat()}, "
            f"after the {check.start_label} {check.start.isoformat()}"
        )
    if row.covered_through is None:
        reasons.append("records no coverage end")
    elif row.covered_through < window_end:
        reasons.append(
            f"covers through {row.covered_through.isoformat()}, "
            f"before the window end {window_end.isoformat()}"
        )
    return tuple(reasons)


def _simulated_flag(status: str, simulated_sources: Sequence[str]) -> Flag:
    """The ``simulated`` flag of a ``simulated`` or ``mixed`` item (P3)."""
    labels = _source_list(simulated_sources)
    if status == DATA_MIXED:
        detail = f" ({labels} simulated)" if labels else ""
        return Flag(FLAG_SIMULATED, f"Joins live and simulated data{detail}")
    return Flag(FLAG_SIMULATED, f"Simulated data ({labels})" if labels else "Simulated data")


@dataclass(slots=True)
class _IncompleteDataset:
    """One incomplete dataset of one section, for its ``incomplete_dataset`` finding."""

    name: str
    dataset_id: int | None
    row: Freshness | None
    reasons: dict[str, None]
    items: int = 0


def _incomplete_message(name: str, reasons: Sequence[str]) -> str:
    """The message of an ``incomplete_dataset`` finding, naming each of its reasons (P4)."""
    return f"{name} is not complete over the window: {'; '.join(reasons)}"


def _incomplete_finding(section: str, found: _IncompleteDataset) -> Finding:
    """The ``incomplete_dataset`` finding of one dataset in one section (P4)."""
    row = found.row
    reasons = list(found.reasons)
    return Finding(
        code=FINDING_INCOMPLETE_DATASET,
        section=section,
        entity=None,
        message=_incomplete_message(found.name, reasons),
        evidence={
            "dataset": found.name,
            "dataset_id": found.dataset_id,
            "completeness": "unknown" if row is None else row.completeness,
            "covered_from": None if row is None else row.covered_from,
            "covered_through": None if row is None else row.covered_through,
            "reason": None if row is None else row.reason,
            "reasons": reasons,
            "items": found.items,
        },
    )


def _applied_thresholds(candidate: Candidate, applied: Mapping[str, Any]) -> Evidence:
    """Return the item's evidence with the thresholds a precedence step applied (AAP 0.5.4.8).

    ``evidence.thresholds`` records every threshold the item's verdicts rest on, so the
    configured value a P4 or P5 judgement used is stored with the run: the returned evidence
    (:meth:`~ads_engine.analysis.context.Evidence.with_thresholds`, which freezes only the
    added values) has each key of ``applied`` it lacks appended after the keys already
    recorded, which keep their values and order. A key already recorded with the same value
    is left as it is, and when every key is, the item's own evidence is returned.

    Raises:
        PrecedenceInvariantError: When the evidence already records a key of ``applied`` with
            another value or type; the conflict is never overwritten or hidden.
    """
    recorded = candidate.evidence.thresholds
    added: dict[str, Any] = {}
    for key, value in applied.items():
        if key not in recorded:
            added[key] = value
            continue
        existing = recorded[key]
        if type(existing) is not type(value) or existing != value:
            raise PrecedenceInvariantError(
                f"{candidate.rule_id} {candidate.entity_ref.key} records {key} = {existing!r} "
                f"in evidence.thresholds, but precedence applied {value!r}"
            )
    if not added:
        return candidate.evidence
    return candidate.evidence.with_thresholds(added)


def _apply_data_state(
    ctx: AnalysisContext, candidates: Sequence[Candidate]
) -> tuple[list[Candidate], dict[str, tuple[Finding, ...]]]:
    """Steps 6 and 7: ``data_status`` and the ``simulated``, ``stale`` and ``incomplete`` flags.

    P3, from the frozen ``evidence.provenance`` modes: every dataset simulated →
    ``simulated``; live and simulated joined → ``mixed``; otherwise ``live``. Both simulated
    statuses carry the ``simulated`` flag naming the simulated sources.

    P4, per item from its own ``required_datasets`` only, so a fresh entity sync never clears
    a stale metrics dataset (AAP 0.3.2): ``stale`` ("Data as of …", the instant in
    ``ctx.display_timezone``) when a required dataset last succeeded more than
    ``sources.stale_after_hours`` before ``ctx.now`` or covers less
    than the window end, and ``incomplete`` when one is not ``complete`` over its required
    range (:func:`_required_start`). A ``live`` item with a stale dataset becomes ``stale``.
    Every item with at least one required dataset, stale or not, has the configured
    ``sources.stale_after_hours`` its datasets were judged against frozen into its
    ``evidence.thresholds`` (:func:`_applied_thresholds`). Each section gets one
    ``incomplete_dataset`` finding per incomplete dataset its items read, naming every reason
    and how many items read it; step 10 recounts ``items`` and narrows ``reasons`` over the
    items the run stores, after feedback suppression (:func:`_recount_incomplete_datasets`).

    Raises:
        ConfigError: When ``sources.stale_after_hours`` is missing or not an integer.
        PrecedenceInvariantError: When an item's evidence records that key with another
            value.
    """
    stale_after = ctx.stale_after
    applied = {_STALE_AFTER_KEY: ctx.thresholds.int(_STALE_AFTER_KEY)}
    window_end = ctx.window.end
    per_section: dict[str, dict[tuple[str, int | None], _IncompleteDataset]] = {
        section: {} for section in SECTION_ORDER
    }
    result: list[Candidate] = []
    for candidate in candidates:
        entries = candidate.evidence.provenance
        simulated_sources = [entry.source for entry in entries if entry.mode == "simulated"]
        if entries and len(simulated_sources) == len(entries):
            base = DATA_SIMULATED
        elif simulated_sources:
            base = DATA_MIXED
        else:
            base = DATA_LIVE
        checks = _checks(ctx, candidate, stale_after)
        stale = [check for check in checks if check.stale]
        incomplete = [check for check in checks if check.incomplete]
        status = DATA_STALE if base == DATA_LIVE and stale else base
        evidence = _applied_thresholds(candidate, applied) if checks else candidate.evidence
        # The flags are merged in order (simulated, stale, incomplete) and stored with the
        # status and evidence in one copy of the item.
        flags = candidate.flags
        if base in (DATA_SIMULATED, DATA_MIXED):
            flags = _merged_flags(flags, _simulated_flag(base, simulated_sources))
        if stale:
            flags = _merged_flags(
                flags, _stale_flag(stale, window_end, display_timezone=ctx.display_timezone)
            )
        if incomplete:
            parts = [
                f"{check.name} ({'; '.join(_incomplete_reasons(check, window_end))})"
                for check in incomplete
            ]
            flags = _merged_flags(flags, Flag(FLAG_INCOMPLETE, f"Incomplete: {'; '.join(parts)}"))
            bucket = per_section[candidate.section]
            for check in incomplete:
                key = (check.name, check.dataset_id)
                found = bucket.get(key)
                if found is None:
                    found = _IncompleteDataset(check.name, check.dataset_id, check.row, {})
                    bucket[key] = found
                found.items += 1
                for reason in _incomplete_reasons(check, window_end):
                    found.reasons.setdefault(reason, None)
        result.append(replace(candidate, data_status=status, evidence=evidence, flags=flags))
    findings = {
        section: tuple(
            _incomplete_finding(section, bucket[key])
            for key in sorted(bucket, key=lambda key: (key[0], _orderable(key[1])))
        )
        for section, bucket in per_section.items()
    }
    return result, findings


# ---------------------------------------------------------------------------
# Step 8: P5 short window (AAP 0.5.4.1, 0.5.4.2 P5)
# ---------------------------------------------------------------------------


def _apply_short_window(ctx: AnalysisContext, candidates: Sequence[Candidate]) -> list[Candidate]:
    """Step 8: the ``short_window`` flag on every item of a short window.

    A window is short below ``window.trusted_min_days``; the configured values, never
    literals, fill the flag's message. Every item, short window or not, has the
    ``window.trusted_min_days`` its window was judged against frozen into its
    ``evidence.thresholds`` (:func:`_applied_thresholds`).

    Raises:
        PrecedenceInvariantError: When an item's evidence records that key with another
            value.
    """
    window = ctx.window
    applied = {_TRUSTED_MIN_DAYS_KEY: window.trusted_min_days}
    flag = (
        Flag(FLAG_SHORT_WINDOW, short_window_message(window.days, window.trusted_min_days))
        if window.short_window
        else None
    )
    result: list[Candidate] = []
    for candidate in candidates:
        evidence = _applied_thresholds(candidate, applied)
        flags = candidate.flags if flag is None else _merged_flags(candidate.flags, flag)
        if evidence is candidate.evidence and flags is candidate.flags:
            result.append(candidate)
        else:
            # The evidence and the flag are stored in one copy of the item.
            result.append(replace(candidate, evidence=evidence, flags=flags))
    return result


# ---------------------------------------------------------------------------
# Step 9: P6, P7 and P8 checks (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def _names_of(candidate: Candidate) -> list[str]:
    """The entity, variant and campaign names an item's text may quote, longest first."""
    names = [candidate.entity_ref.name]
    extra = candidate.evidence.extra
    listed = extra.get("names")
    if isinstance(listed, tuple | list):
        names.extend(name for name in listed if isinstance(name, str))
    campaign = extra.get("campaign")
    if isinstance(campaign, Mapping) and isinstance(campaign.get("name"), str):
        names.append(campaign["name"])
    return sorted({name for name in names if name.strip()}, key=len, reverse=True)


def _check_items(candidates: Sequence[Candidate]) -> None:
    """Step 9: assert P6 and P7; P8 floor flags pass through unchanged.

    * **P6**: no item carries an evidence sample below its minimum; a pattern below its
      minimum is a ``Withheld`` line, never an item.
    * **P7**: a CR1 pair that CR1 has not marked distinguishable (``extra.distinguishable``
      is not ``True``) carries ``not_distinguishable`` and no winner wording ("winner",
      "better", "best", "beats", "outperforms" …) in its action or reason. The variant,
      pair and campaign names are removed before the wording is searched, so a creative
      named "Best practices" never trips the check.
    * **P8**: the rules' attribution, creative and deal floor flags are neither added nor
      removed here.

    Raises:
        PrecedenceInvariantError: When P6 or P7 is broken.
    """
    for candidate in candidates:
        sample = candidate.evidence.sample
        if sample is not None and not sample.met:
            raise PrecedenceInvariantError(
                f"P6: {candidate.rule_id} on {candidate.entity_ref.key} is an item with a "
                f"sample of {sample.observed} {sample.unit} below the required "
                f"{sample.required}; a pattern below its minimum must be withheld"
            )
        if candidate.rule_id != CR1 or candidate.evidence.extra.get("distinguishable") is True:
            continue
        if not candidate.has_flag(FLAG_NOT_DISTINGUISHABLE):
            raise PrecedenceInvariantError(
                f"P7: CR1 pair {candidate.entity_ref.key} is not distinguishable but lacks the "
                f"{FLAG_NOT_DISTINGUISHABLE} flag"
            )
        text = f"{candidate.action} {candidate.reason}"
        for name in _names_of(candidate):
            text = text.replace(name, " ")
        match = _WINNER_WORDING.search(text)
        if match is not None:
            raise PrecedenceInvariantError(
                f"P7: CR1 pair {candidate.entity_ref.key} is not distinguishable but its text "
                f"calls a winner ({match.group(0)!r})"
            )


# ---------------------------------------------------------------------------
# Step 10: feedback suppression (AAP 0.5.4.2, 0.5.2 recommendation_feedback)
# ---------------------------------------------------------------------------


def _apply_feedback(
    ctx: AnalysisContext, candidates: Sequence[Candidate], counts: _Counts
) -> list[Candidate]:
    """Step 10: drop every item an active dismiss, snooze or done hides; C0 is exempt.

    Feedback matches on ``(rule_id, entity type, entity key)`` (:attr:`Candidate.feedback_key`)
    and is in force while its ``until`` is after ``ctx.now``; its duration was fixed by the
    writer (``feedback.dismiss_days``, ``feedback.snooze_days``, ``feedback.done_days``).
    When several entries match one item, the latest written wins (``ctx.feedback`` is in
    write order), as the overlay's ``hidden_by`` does. Each dropped item is counted under
    ``feedback_dismiss``, ``feedback_snooze`` or ``feedback_done`` in its section.
    """
    active: dict[tuple[str, str, str], FeedbackInfo] = {}
    for entry in ctx.feedback:
        if entry.active_at(ctx.now):
            active[entry.key] = entry
    if not active:
        return list(candidates)
    kept: list[Candidate] = []
    for candidate in candidates:
        found = None if candidate.is_discrepancy else active.get(candidate.feedback_key)
        if found is not None and found.matches(candidate):
            counts.add(candidate.section, found.suppression_reason)
            continue
        kept.append(candidate)
    return kept


def _recount_no_crm_link(
    findings: Mapping[str, tuple[Finding, ...]], candidates: Sequence[Candidate]
) -> dict[str, tuple[Finding, ...]]:
    """Recount each section's ``no_crm_link`` finding over the items the run stores.

    ``campaigns.py`` and ``budget.py`` count the Google items carrying ``no_crm_link`` as
    they build them, before G1 (step 2), P2 (step 5) and feedback (step 10) remove any, while
    the run stores the finding beside the items that survive (AAP 0.5.3 "Where outputs are
    frozen"). Each ``no_crm_link`` finding is therefore rebuilt with ``evidence.items`` set to
    the number of ``candidates`` of its section that carry the flag, held and C0 items
    included; its code, section, entity, message and every other evidence key are kept, as
    is the findings' order. A finding whose items were all removed stays, with ``items`` 0:
    the link gap held for the items the rules built, and the section's suppression counts
    say why they are gone. The count depends only on the set of ``candidates``, never their
    order.

    Args:
        findings: The rules' findings per section, in their stored order.
        candidates: The items that survived every suppression step.

    Returns:
        The findings per section, each ``no_crm_link`` one carrying the recounted ``items``.
    """
    flagged: dict[str, int] = dict.fromkeys(findings, 0)
    for candidate in candidates:
        if candidate.has_flag(FLAG_NO_CRM_LINK):
            flagged[candidate.section] = flagged.get(candidate.section, 0) + 1
    return {
        section: tuple(
            replace(finding, evidence={**finding.evidence, "items": flagged[section]})
            if finding.code == FINDING_NO_CRM_LINK
            else finding
            for finding in items
        )
        for section, items in findings.items()
    }


def _recount_incomplete_datasets(
    ctx: AnalysisContext,
    findings: Mapping[str, tuple[Finding, ...]],
    candidates: Sequence[Candidate],
) -> dict[str, tuple[Finding, ...]]:
    """Recount each section's ``incomplete_dataset`` findings over the items the run stores.

    Step 7 counts the items that read each incomplete dataset as it flags them, before
    feedback (step 10) removes any, while the run stores the finding beside the items that
    survive (AAP 0.5.3 "Where outputs are frozen"). Each surviving item's own datasets are
    judged again with :func:`_checks`, which reads only what steps 8 to 10 leave unchanged
    (required datasets, provenance, the frozen lookback start and its days threshold), so it
    finds exactly the incomplete datasets step 7 flagged on that item. Each finding, keyed by
    its section, ``dataset`` and ``dataset_id`` as step 7 keys it, is rebuilt with:

    * ``evidence.items``: the number of ``candidates`` of its section that read the dataset
      incomplete, held and C0 items included;
    * ``evidence.reasons``, and the message naming them: the step-7 reasons, in their order,
      that at least one of those items has, so a reason only a removed item's range gave (a
      K1 baseline start) is no longer cited.

    A finding no surviving item reads stays, with ``items`` 0 and its step-7 reasons: the
    dataset is still incomplete for the items the rules built, and the section's suppression
    counts say why they are gone. Its code, section, entity, every other evidence key and the
    findings' order are kept. The result depends only on the set of ``candidates``, never
    their order.

    Args:
        ctx: The analysis context step 7 judged the datasets in.
        findings: Step 7's ``incomplete_dataset`` findings per section, in their stored order.
        candidates: The items that survived every suppression step.

    Returns:
        The findings per section, each carrying the recounted ``items`` and its ``reasons``.
    """
    window_end = ctx.window.end
    stale_after = ctx.stale_after
    readers: dict[tuple[str, str, int | None], int] = {}
    read_reasons: dict[tuple[str, str, int | None], set[str]] = {}
    for candidate in candidates:
        for check in _checks(ctx, candidate, stale_after):
            if not check.incomplete:
                continue
            key = (candidate.section, check.name, check.dataset_id)
            readers[key] = readers.get(key, 0) + 1
            read_reasons.setdefault(key, set()).update(_incomplete_reasons(check, window_end))

    def recounted(finding: Finding) -> Finding:
        """One finding with ``items`` and ``reasons`` over the surviving items."""
        name = finding.evidence["dataset"]
        key = (finding.section, name, finding.evidence["dataset_id"])
        built = list(finding.evidence["reasons"])
        cited = read_reasons.get(key, set())
        reasons = [reason for reason in built if reason in cited] or built
        return replace(
            finding,
            message=_incomplete_message(name, reasons),
            evidence={**finding.evidence, "reasons": reasons, "items": readers.get(key, 0)},
        )

    return {
        section: tuple(
            recounted(finding) if finding.code == FINDING_INCOMPLETE_DATASET else finding
            for finding in items
        )
        for section, items in findings.items()
    }


def _no_dataset_reason(ctx: AnalysisContext, name: str) -> str:
    """Why a section dataset resolves to no dataset: the source whose active dataset is missing.

    ``google.*`` and ``linkedin.*`` name their own source. A ``hubspot.*`` name resolves to the
    effective CRM dataset of the LinkedIn dataset in use (AAP 0.5.4.1, 0.3.3), so while
    LinkedIn has no active dataset that is the one missing; otherwise HubSpot's is.
    """
    source = name.split(".", 1)[0]
    if source == "hubspot" and ctx.dataset("linkedin") is None:
        source = "linkedin"
    return f"no active {_SOURCE_LABELS.get(source, source)} dataset"


def _section_data_findings(
    ctx: AnalysisContext,
    findings: Mapping[str, tuple[Finding, ...]],
    candidates: Sequence[Candidate],
) -> dict[str, tuple[Finding, ...]]:
    """Say why a section that stores no item is empty, from its own datasets (P4).

    AAP 0.5.5: "Each section shows an EmptyState with the reason when it has no items". A
    section with no item gets no item-based ``incomplete_dataset`` finding from step 7, yet
    the usual reason it is empty is that its data does not cover the window: a custom window
    ending before the backfill, or a source left with no active dataset after a probe defect
    (AAP 0.3.3: a defect never falls back to simulated data). So, for every section with no
    surviving ``candidates``, each dataset its rules read (:data:`_SECTION_DATASETS`) is
    judged over the run's window, with no rule lookback, exactly as
    :meth:`~ads_engine.analysis.context.Freshness.is_complete_over` judges an item's.
    ``google.*`` and ``linkedin.*`` resolve to their source's active primary and ``hubspot.*``
    to the effective CRM dataset (``crm_scope="linkedin"``). Each dataset that is not
    ``complete`` over the window, and that no ``incomplete_dataset`` finding of the section
    already names, adds one finding of the step-7 shape with ``items`` 0. Its reasons are
    :func:`_incomplete_reasons`, or "no active {source} dataset" when the name resolves to no
    dataset (:func:`_no_dataset_reason`). A section with a surviving item is left unchanged:
    its items carry their own flags and findings.

    The section's ``incomplete_dataset`` findings are then ordered by dataset name and id, as
    step 7 orders them. The result depends only on the set of ``candidates``, never their
    order.

    Args:
        ctx: The analysis context the rules evaluated.
        findings: The recounted ``incomplete_dataset`` findings per section.
        candidates: The items that survived every suppression step.

    Returns:
        The ``incomplete_dataset`` findings per section, in :data:`SECTION_ORDER`.
    """
    window = ctx.window
    stale_after = ctx.stale_after
    occupied = {candidate.section for candidate in candidates}
    result: dict[str, tuple[Finding, ...]] = {}
    for section in SECTION_ORDER:
        existing = tuple(findings.get(section, ()))
        if section in occupied:
            result[section] = existing
            continue
        named = {
            finding.evidence["dataset"]
            for finding in existing
            if finding.code == FINDING_INCOMPLETE_DATASET
        }
        added: list[Finding] = []
        for name in _SECTION_DATASETS[section]:
            if name in named:
                continue
            dataset_id = ctx.dataset_id_for(
                name, crm_scope="linkedin" if name.startswith("hubspot.") else None
            )
            row = ctx.freshness_for(name, dataset_id)
            if row is not None and row.is_complete_over(
                window_start=window.start, window_end=window.end
            ):
                continue
            if dataset_id is None:
                reasons: tuple[str, ...] = (_no_dataset_reason(ctx, name),)
            else:
                stale = row is None or row.is_stale(
                    now=ctx.now, stale_after=stale_after, window_end=window.end
                )
                check = _Check(
                    name,
                    dataset_id,
                    row,
                    window.start,
                    _WINDOW_START_LABEL,
                    stale=stale,
                    incomplete=True,
                )
                reasons = _incomplete_reasons(check, window.end)
            found = _IncompleteDataset(name, dataset_id, row, dict.fromkeys(reasons))
            added.append(_incomplete_finding(section, found))
        result[section] = tuple(
            sorted(
                (*existing, *added),
                key=lambda finding: (
                    finding.evidence["dataset"],
                    _orderable(finding.evidence["dataset_id"]),
                ),
            )
        )
    return result


def _with_held_counts(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Lead every C0 item with the number of stored recommendations its discrepancy holds.

    AAP 0.1.2: "Every card leads with one headline number"; P1: a discrepancy is resolved
    first, "before any other recommendation is trusted". The number that justifies resolving
    it is how many recommendations wait on it, which is only final once P1 has held them and
    feedback (step 10) has dropped what the run will not store. Each C0 item's evidence
    therefore gets :meth:`~ads_engine.analysis.context.Evidence.with_headline` of
    ``held_recommendations``: the number of ``candidates`` that are not C0 items and whose
    ``extra.blocking_discrepancies`` (set by P1) contains the item's
    ``extra.discrepancy_id``. An item held by two discrepancies counts for both, and 0 is a
    valid count. The metric is appended after the rule's own metrics, whose keys and order
    are kept. The counts depend only on the set of ``candidates``, never their order.

    Args:
        candidates: The items that survived every suppression step.

    Returns:
        ``candidates`` in their order, each C0 item with its headline metric added.

    Raises:
        ValueError: When a C0 item's ``extra.discrepancy_id`` is not an integer, or its
            evidence already names a headline metric or has a ``held_recommendations``
            metric (:meth:`~ads_engine.analysis.context.Evidence.with_headline`).
    """
    held: dict[int, int] = {}
    for candidate in candidates:
        if candidate.is_discrepancy:
            continue
        for discrepancy_id in candidate.evidence.extra.get("blocking_discrepancies", ()):
            held[discrepancy_id] = held.get(discrepancy_id, 0) + 1
    result: list[Candidate] = []
    for candidate in candidates:
        if not candidate.is_discrepancy:
            result.append(candidate)
            continue
        discrepancy_id = candidate.evidence.extra.get("discrepancy_id")
        if not isinstance(discrepancy_id, int) or isinstance(discrepancy_id, bool):
            raise ValueError(
                f"C0 item {candidate.entity_ref.key} has no integer extra.discrepancy_id "
                f"({discrepancy_id!r}), so the recommendations it holds cannot be counted"
            )
        metric = Metric(
            _HELD_METRIC_KEY, _HELD_METRIC_LABEL, held.get(discrepancy_id, 0), _HELD_METRIC_UNIT
        )
        result.append(replace(candidate, evidence=candidate.evidence.with_headline(metric)))
    return result


def _check_headlines(candidates: Sequence[Candidate]) -> None:
    """The headline post-check: every card leads with one metric of its own evidence.

    AAP 0.1.2: "Every card leads with one headline number, and its full evidence follows".
    Every item outside White papers, whose items are table rows with fixed columns rather
    than cards, must leave precedence with ``extra.headline_metric``
    (:data:`~ads_engine.analysis.context.EXTRA_HEADLINE_METRIC`) naming the key of one of
    its own ``evidence.metrics``. The rules select theirs when they build the evidence; C0's
    is set by :func:`_with_held_counts`, which this check therefore follows.

    Raises:
        PrecedenceInvariantError: When an item outside White papers names no headline
            metric, or names a key that none of its own metrics has.
    """
    for candidate in candidates:
        if candidate.section == WHITE_PAPERS:
            continue
        headline = candidate.evidence.extra.get(EXTRA_HEADLINE_METRIC)
        keys = [metric.key for metric in candidate.evidence.metrics]
        if not isinstance(headline, str) or headline not in keys:
            raise PrecedenceInvariantError(
                f"headline: {candidate.rule_id} on {candidate.entity_ref.key} leads with "
                f"{headline!r}, which is not one of its evidence metrics {keys}; every card "
                "leads with one of its own metrics"
            )


# ---------------------------------------------------------------------------
# Step 11: ranking (AAP 0.5.4.1 "Ranking ties", 0.5.4.2 "Ranking", 0.5.5)
# ---------------------------------------------------------------------------


def _currency_code(value: object) -> str | None:
    """A currency as its upper-case code, or ``None`` when it is not a non-blank string."""
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _creative_group_order(
    ctx: AnalysisContext, group: str, items: Sequence[Candidate]
) -> tuple[Any, ...]:
    """A creative group's position: its campaign's currency, then window spend descending.

    AAP 0.5.4.6: "Campaigns are ranked by spend, descending." Amounts are never compared
    across currencies (AAP 0.1.1, 0.5.4.1), so the groups are partitioned by the campaign's
    account currency first, in currency-code order as the white-paper cohorts are, and ranked
    by spend within each partition; ties fall to the campaign name, id and group key. The
    currency and spend are the context's (``ctx.account_currency``, the
    ``metrics.window_totals`` spend); a campaign missing from the context falls back to the
    ``currency`` and ``campaign_spend`` its items froze, the lowest code when the items
    disagree. A group of unknown currency ranks after every currency partition, and an
    unknown spend ranks last within its partition.
    """
    campaign_id = creative_group_campaign(group)
    currency: str | None = None
    spend: Decimal | None = None
    if campaign_id is not None and campaign_id in ctx.campaigns:
        currency = _currency_code(ctx.account_currency(campaign_id))
        spend = metrics.window_totals(ctx, (campaign_id,)).spend
    else:
        codes = [
            code
            for code in (_currency_code(item.evidence.extra.get("currency")) for item in items)
            if code is not None
        ]
        currency = min(codes) if codes else None
        frozen = [
            value
            for value in (item.evidence.extra.get("campaign_spend") for item in items)
            if isinstance(value, Decimal) and value.is_finite()
        ]
        spend = max(frozen) if frozen else None
    name = group if campaign_id is None else ctx.campaign_name(campaign_id)
    return (
        (currency is None, currency or ""),
        _orderable(None if spend is None else -spend),
        name,
        campaign_id or 0,
        group,
    )


def _group_sequence(
    ctx: AnalysisContext, section: str, groups: Mapping[str, Sequence[Candidate]]
) -> list[str]:
    """The display order of a section's groups.

    The static groups of :data:`GROUP_ORDER` first, in that order; then the dynamic creative
    groups (one per campaign) partitioned by the campaign's account currency in currency-code
    order and by campaign spend descending within each currency, since amounts are never
    compared across currencies (:func:`_creative_group_order`); then the white-paper currency
    cohorts by currency code; then any other group by key, so no item is ever dropped.
    """
    static = [group for group in GROUP_ORDER.get(section, ()) if group in groups]
    rest = [group for group in groups if group not in static]
    creative = [group for group in rest if creative_group_campaign(group) is not None]
    cohorts = [
        group
        for group in rest
        if group not in creative and cohort_group_currency(group) is not None
    ]
    other = sorted(group for group in rest if group not in creative and group not in cohorts)
    creative.sort(key=lambda group: _creative_group_order(ctx, group, groups[group]))
    cohorts.sort(key=lambda group: (cohort_group_currency(group) or "", group))
    return [*static, *creative, *cohorts, *other]


def _rank(
    ctx: AnalysisContext, candidates: Sequence[Candidate]
) -> dict[str, tuple[RankedItem, ...]]:
    """Step 11: order each section and number it from 1.

    C0 items are pinned first in their section, oldest ``detected_at`` first (their
    ``rank_key`` is ``(detected_at, id)``), exempt from ordinary ranking. Every other item
    follows its group's display order (:func:`_group_sequence`); within a group unheld items
    come before held ones (held cards collapse at the end of their group, AAP 0.5.5), each by
    ``rank_key``, entity name, entity id and entity key (:func:`_rank_order`). The order is
    total, so shuffled rule results rank identically.
    """
    by_section: dict[str, list[Candidate]] = {section: [] for section in SECTION_ORDER}
    for candidate in candidates:
        by_section[candidate.section].append(candidate)
    ranked: dict[str, tuple[RankedItem, ...]] = {}
    for section, items in by_section.items():
        pinned = sorted(
            (item for item in items if item.pinned or item.is_discrepancy), key=_rank_order
        )
        groups: dict[str, list[Candidate]] = {}
        for item in items:
            if not (item.pinned or item.is_discrepancy):
                groups.setdefault(item.group, []).append(item)
        ordered = list(pinned)
        for group in _group_sequence(ctx, section, groups):
            ordered.extend(sorted(groups[group], key=lambda item: (item.held, _rank_order(item))))
        ranked[section] = tuple(
            RankedItem(candidate=item, rank=position) for position, item in enumerate(ordered, 1)
        )
    return ranked


# ---------------------------------------------------------------------------
# The precedence pipeline (AAP 0.5.4.2)
# ---------------------------------------------------------------------------


def apply_precedence(
    results: RuleResult | Iterable[RuleResult], ctx: AnalysisContext
) -> PrecedenceOutput:
    """Guard, allocate, hold, suppress, flag and rank one run's rule outputs (AAP 0.5.4.2).

    The steps run in the fixed order of the module docstring: collect, G1, B3 allocation,
    P1, P2, P3, P4, P5, the P6/P7/P8 checks, feedback suppression with the ``no_crm_link``
    and ``incomplete_dataset`` recounts, the ``incomplete_dataset`` findings of each section
    left with no item (one per section dataset not complete over the window, ``items`` 0)
    and each C0 item's ``held_recommendations`` headline over the surviving items, the
    headline post-check, ranking. Pure: the inputs are unchanged, and the same inputs, in
    any order, give the same output.

    Args:
        results: The ``RuleResult`` of every rule module (``discrepancy``, ``keywords``,
            ``campaigns``, ``budget``, ``creative``, ``white_papers``), in any order; a single
            ``RuleResult`` is accepted too.
        ctx: The analysis context the rules evaluated.

    Returns:
        The ranked items, findings, withheld patterns and suppression counts of each section,
        and every B3 allocation, ready for ``runner.py`` to persist in one transaction.

    Raises:
        TypeError: When ``ctx`` is not an ``AnalysisContext`` or a result is not a
            ``RuleResult``.
        ValueError: When a result's own suppression counts cannot be attributed to one
            section, or a C0 item has no integer ``extra.discrepancy_id``, already names a
            headline metric or already has a ``held_recommendations`` metric.
        PrecedenceInvariantError: When a guarantee is broken: an increase survives on a
            G1-blocked target, a B3 allocation exceeds a receiver's increase or a donor's
            freed amount, an item's sample is below its minimum (P6), a CR1 pair that is
            not distinguishable lacks its flag or calls a winner (P7), an item's evidence
            records ``sources.stale_after_hours`` or ``window.trusted_min_days`` with a value
            other than the one P4 or P5 applied, or an item outside White papers leaves
            without a headline metric of its own evidence.
        ConfigError: When a threshold key precedence reads is missing.
    """
    if not isinstance(ctx, AnalysisContext):
        raise TypeError(f"apply_precedence needs an AnalysisContext, found {ctx!r}")
    inputs: Iterable[RuleResult] = (results,) if isinstance(results, RuleResult) else results
    counts = _Counts()

    # 1. Collect.
    collected = _collect(inputs, counts)

    # 2. G1 guard. Both steps are resolved at call time on purpose: documented test seams.
    blocked = metrics.spend_increase_blocked(ctx)
    kept, removed = apply_g1(collected.candidates, blocked)
    for candidate in removed:
        counts.add(candidate.section, SUPPRESS_G1_BLOCKED)
    _check_g1(kept, blocked)

    # 3. B3 allocation to the B1 items that survived G1. The entity index serves steps 3 and 4.
    index = _EntityIndex.of(ctx)
    candidates, allocations = _reallocate(ctx, kept, blocked, index)

    # 4. P1 discrepancies first.
    candidates = _apply_p1(ctx, candidates, index)

    # 5. P2 conversion tracking absent.
    candidates, tracking = _apply_p2(ctx, candidates, counts)

    # 6 and 7. P3 simulated data, P4 stale and incomplete.
    candidates, incomplete = _apply_data_state(ctx, candidates)

    # 8. P5 short window.
    candidates = _apply_short_window(ctx, candidates)

    # 9. P6, P7 and P8 checks.
    _check_items(candidates)

    # 10. Feedback suppression (C0 exempt), then the no_crm_link and incomplete_dataset item
    # counts over the surviving items, the incomplete_dataset findings of each section left with
    # no item, each C0 item's held-recommendation headline, and the independent headline
    # post-check.
    candidates = _apply_feedback(ctx, candidates, counts)
    rule_findings = _recount_no_crm_link(collected.findings, candidates)
    incomplete = _recount_incomplete_datasets(ctx, incomplete, candidates)
    incomplete = _section_data_findings(ctx, incomplete, candidates)
    candidates = _with_held_counts(candidates)
    _check_headlines(candidates)

    # 11. Ranking.
    items = _rank(ctx, candidates)

    findings = {
        section: (*tracking[section], *rule_findings[section], *incomplete[section])
        for section in SECTION_ORDER
    }
    return PrecedenceOutput(
        items=items,
        findings=findings,
        withheld=collected.withheld,
        suppressed=counts.frozen(),
        allocations=allocations,
    )
