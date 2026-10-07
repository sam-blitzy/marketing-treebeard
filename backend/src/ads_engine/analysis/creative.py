"""Rules CR1, CR2 and CR3: Copy / creative changes for LinkedIn creatives.

AAP 0.5.4.6, 0.2.4 (small-sample methods), 0.5.4.2 P6/P7 and FR-7. The user asked three things
of the copy section: which ad variants are statistically distinguishable yet (not just
"currently ahead" on a small sample), which measurable funnel stage underperforms (open rate
vs. completion rate vs. click rate), and what single thing the next variant should change so
the read stays clean. Copy analysis covers LinkedIn creatives only; Google ad copy is out of
scope (AAP 0.7.2).

**Variants** are the creatives of one LinkedIn campaign with at least ``cr.min_impressions``
(1,000) window impressions. Only campaigns with at least ``cr.min_variants`` (2) variants are
analysed; a delivering campaign with fewer is listed as a withheld CR1 line with its variant
count observed and required (P6). Creatives under the minimum are excluded, never listed as
variants, and named in ``evidence.extra.excluded``.

**Stage rates**: click rate = clicks / impressions; open rate = lead form opens / impressions;
completion rate = leads / form opens. Non-lead-gen campaigns use only click rate.

* **CR1 distinguishability** (one item per variant pair): the end-to-end rate (leads /
  impressions, or clicks / impressions outside lead gen) of each pair is compared with
  Fisher's exact test when any expected cell is below ``cr.fisher_min_expected`` (5),
  otherwise the two-proportion z-test, and every pair of the campaign is Holm-adjusted
  together. A pair is "Distinguishable" only when its adjusted p is below ``cr.alpha``;
  otherwise the item says which variant is ahead, that it is not yet distinguishable, and how
  many more impressions per variant the observed difference needs at ``cr.power``. P7: no
  item calls a variant a winner, better or best unless CR1 marks the pair distinguishable.
* **CR2 weak stage**: the campaign's variants are pooled per stage, and the Wilson
  ``cr.ci_level`` (95%) interval is compared with the account baseline: the same stage rate
  across every LinkedIn creative of the same ad account and objective group in the window. A
  stage is weak when its upper bound is below the baseline; the weak stage reported is the one
  with the largest relative shortfall.
* **CR3 next test** (one item per campaign, carrying the CR2 sentence as its reason): the
  stage maps to exactly one variable (:data:`STAGE_VARIABLE`) and everything else stays
  identical. The per-variant sample to detect a ``cr.target_lift`` (20%) relative lift of the
  baseline at ``cr.alpha`` and ``cr.power`` is stated in the stage's own denominator:
  impressions for click and open rate, form opens for completion rate, which is also projected
  to impressions through the observed open rate. When no stage is weak, the stage with the
  lowest rate relative to its baseline is tested, labelled "exploratory".

Every item reads ``linkedin.analytics_daily`` and ``linkedin.entities``, depends on its
campaign (P1 holds) and offers "Mark done" only (no decision subject). Within a campaign group
(:func:`~ads_engine.analysis.context.creative_group`) the CR3 item ranks first, then the CR1
pairs by adjusted p-value; ``precedence.py`` orders the campaign groups by campaign spend.

Purity: no database, network, settings or logging access. Threshold values come only from
``config/thresholds.yaml`` through :class:`CreativeConfig`, and every rendered number in text
is the configured value, never a literal.

Example:
    >>> config = CreativeConfig(
    ...     1000, 2, Decimal("0.05"), Decimal("0.8"), Decimal("0.20"), Decimal("5"), Decimal("0.95")
    ... )
    >>> plan = plan_next_test(
    ...     "click_rate",
    ...     baseline=Decimal("0.012"),
    ...     exposure=40_000,
    ...     daily_impressions_per_variant=Decimal("700"),
    ...     config=config,
    ... )
    >>> plan.variable, plan.unit, plan.not_estimable_reason
    ('headline', 'impressions', None)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Context, Decimal
from types import MappingProxyType
from typing import Any, Final, Literal

from ads_engine.analysis.context import (
    CR1,
    CR3,
    CREATIVE,
    ENTITY_CAMPAIGN,
    ENTITY_CREATIVE_PAIR,
    FLAG_NOT_DISTINGUISHABLE,
    OBJ_LINKEDIN_LEAD_GEN,
    AnalysisContext,
    Baseline,
    CampaignInfo,
    Candidate,
    CreativeInfo,
    CreativeTotals,
    DecisionSubject,
    EntityRef,
    Flag,
    Metric,
    RuleResult,
    Sample,
    TestResult,
    Withheld,
    creative_group,
    creative_pair_external_id,
)
from ads_engine.analysis.metrics import cost_per_outcome
from ads_engine.analysis.stats import (
    Interval,
    ProportionTest,
    additional_n_for_difference,
    compare_proportions,
    holm,
    required_n,
    wilson,
)
from ads_engine.analysis.thresholds import Thresholds

__all__ = [
    "CR1_THRESHOLD_KEYS",
    "CR3_THRESHOLD_KEYS",
    "EXPLORATORY_TEXT",
    "NEXT_TEST_VARIANTS",
    "NOT_ESTIMABLE",
    "NOT_ESTIMABLE_BASELINE_UNAVAILABLE",
    "NOT_ESTIMABLE_NO_DIFFERENCE",
    "NOT_ESTIMABLE_POWER_UNREACHABLE",
    "NOT_ESTIMABLE_TARGET_ABOVE_100",
    "NOT_ESTIMABLE_ZERO_BASELINE",
    "NOT_ESTIMABLE_ZERO_EXPOSURE",
    "NOT_ESTIMABLE_ZERO_IMPRESSIONS",
    "NO_WEAK_STAGE_TEXT",
    "REQUIRED_DATASETS",
    "STAGES",
    "STAGE_VARIABLE",
    "STAGE_VARIABLE_KEY",
    "TEST_ELEMENTS",
    "CreativeConfig",
    "NextTestPlan",
    "StageKey",
    "StageSpec",
    "evaluate",
    "plan_next_test",
]

# ---------------------------------------------------------------------------
# Stages, variables and fixed texts (AAP 0.5.4.6)
# ---------------------------------------------------------------------------

type StageKey = Literal["click_rate", "open_rate", "completion_rate"]
"""A LinkedIn lead-gen funnel stage."""


@dataclass(frozen=True, slots=True)
class StageSpec:
    """One funnel stage: its rate is ``numerator / denominator`` of a creative's totals.

    Attributes:
        key: The stage key.
        label: Display label, for example "Open rate".
        numerator: The :class:`CreativeTotals` attribute counting the stage's successes.
        denominator: The :class:`CreativeTotals` attribute counting its trials, the stage's
            own denominator.
        success_unit: The plural noun of a success, for example "form opens".
        trial_unit: The plural noun of a trial: "impressions", or "form opens" for completion.
    """

    key: StageKey
    label: str
    numerator: str
    denominator: str
    success_unit: str
    trial_unit: str


STAGES: Final[tuple[StageSpec, ...]] = (
    StageSpec("click_rate", "Click rate", "clicks", "impressions", "clicks", "impressions"),
    StageSpec("open_rate", "Open rate", "form_opens", "impressions", "form opens", "impressions"),
    StageSpec("completion_rate", "Completion rate", "leads", "form_opens", "leads", "form opens"),
)
"""The lead-gen funnel stages in funnel order; ties in stage selection go to the earlier one."""

_STAGES_BY_KEY: Final[Mapping[str, StageSpec]] = MappingProxyType({s.key: s for s in STAGES})

STAGE_VARIABLE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "click_rate": "headline",
        "open_rate": "call-to-action label",
        "completion_rate": "number of form questions",
    }
)
"""CR3: the single variable a test of each stage changes, as rendered in the action."""

STAGE_VARIABLE_KEY: Final[Mapping[str, str]] = MappingProxyType(
    {"click_rate": "headline", "open_rate": "cta_label", "completion_rate": "form_questions"}
)
"""CR3: the machine key of each stage's variable (``evidence.extra.next_test.variable``)."""

TEST_ELEMENTS: Final[tuple[tuple[str, str], ...]] = (
    ("headline", "headline"),
    ("intro_text", "intro text"),
    ("cta_label", "call-to-action label"),
    ("form_questions", "number of form questions"),
    ("offer", "offer"),
)
"""Every creative element a next test holds fixed except the one variable it changes."""

NEXT_TEST_VARIANTS: Final = 2
"""CR3 proposes a two-variant test: the current creative against one changed variable."""

NOT_ESTIMABLE: Final = "not estimable"
"""Replaces a sample figure that cannot be computed; always followed by its reason."""

NOT_ESTIMABLE_NO_DIFFERENCE: Final = "no observed difference"
"""CR1: the pair's observed rates are identical, so no sample reaches the target power."""

NOT_ESTIMABLE_ZERO_IMPRESSIONS: Final = "a variant has zero impressions"
"""CR1: a variant without impressions has no rate to compare."""

NOT_ESTIMABLE_POWER_UNREACHABLE: Final = "target power unreachable at the observed difference"
"""CR1/CR3: no sample within the root finder's reach attains ``cr.power``."""

NOT_ESTIMABLE_ZERO_EXPOSURE: Final = "zero exposure"
"""CR3: the tested stage's own denominator (or the current delivery) is zero."""

NOT_ESTIMABLE_ZERO_BASELINE: Final = "baseline rate is 0"
"""CR3: a lift of a zero baseline is zero, so no test can detect it."""

NOT_ESTIMABLE_TARGET_ABOVE_100: Final = "target above 100%"
"""CR3: baseline x (1 + ``cr.target_lift``) exceeds 100%, which no rate can reach."""

NOT_ESTIMABLE_BASELINE_UNAVAILABLE: Final = "account baseline unavailable"
"""CR3: the account has no exposure at the stage, so there is no baseline to lift."""

NO_WEAK_STAGE_TEXT: Final = "No stage measurably below baseline"
"""CR2: the reason sentence when no stage's upper bound lies below its baseline."""

EXPLORATORY_TEXT: Final = "exploratory: tests the stage lowest relative to its baseline"
"""CR3: the label closing the action when no stage is weak and the test is exploratory."""

REQUIRED_DATASETS: Final[tuple[str, ...]] = ("linkedin.analytics_daily", "linkedin.entities")
"""The datasets every CR item reads: creative totals and the creative/campaign entities."""

CR1_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "cr.min_impressions",
    "cr.min_variants",
    "cr.alpha",
    "cr.power",
    "cr.fisher_min_expected",
)
"""The thresholds a CR1 pair applies, copied into its ``evidence.thresholds``."""

CR3_THRESHOLD_KEYS: Final[tuple[str, ...]] = (
    "cr.min_impressions",
    "cr.min_variants",
    "cr.ci_level",
    "cr.alpha",
    "cr.power",
    "cr.target_lift",
)
"""The thresholds a CR2/CR3 item applies, copied into its ``evidence.thresholds``."""

_EN_DASH: Final = "\u2013"
_ALPHA: Final = "\u03b1"
_QUANTUM: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_HUNDRED: Final = Decimal(100)
_NO_P_RANK: Final = Decimal(2)
"""Rank key of a pair without an adjusted p-value: after every real p in [0, 1]."""

_TEST_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"fisher_exact": "Fisher's exact test", "two_proportion_z": "two-proportion z-test"}
)


def _exact() -> Context:
    """Return a fresh, wide decimal context, so no caller's context affects a result."""
    return Context(prec=50, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Formatting: every rendered number comes from the evidence it cites
# ---------------------------------------------------------------------------


def _ratio(successes: int, trials: int) -> Decimal | None:
    """Return ``successes / trials`` at 6 places, or ``None`` when it is not a proportion.

    ``None`` covers zero trials and successes above trials (inconsistent platform counts),
    so a displayed or compared rate always lies in [0, 1].
    """
    if trials <= 0 or successes < 0 or successes > trials:
        return None
    return _exact().divide(Decimal(successes), Decimal(trials)).quantize(_QUANTUM)


def _percent(rate: Decimal) -> str:
    """Render a rate as a percentage with at least two significant digits below 1%.

    Example:
        >>> _percent(Decimal("0.005")), _percent(Decimal("0.000096")), _percent(Decimal(0))
        ('0.50%', '0.0096%', '0%')
    """
    value = _exact().multiply(rate, _HUNDRED)
    if value == 0:
        return "0%"
    places = 2 if value >= Decimal("0.1") else 3 if value >= Decimal("0.01") else 4
    return f"{value.quantize(Decimal(1).scaleb(-places)):f}%"


def _lift(value: Decimal) -> str:
    """Render a relative lift as a percentage without trailing zeros (0.20 → 20%)."""
    return f"{_exact().multiply(value, _HUNDRED).normalize():f}%"


def _p(value: Decimal) -> str:
    """Render a p-value at 4 places, or "< 0.0001" below that."""
    if value < Decimal("0.0001"):
        return "< 0.0001"
    return f"{value.quantize(Decimal('0.0001')):f}"


def _count(value: int) -> str:
    """Render a count with thousands separators."""
    return f"{value:,}"


def _ceil_div(numerator: int, denominator: int) -> int:
    """Return ``ceil(numerator / denominator)`` for positive integers, exactly."""
    return -(-numerator // denominator)


def _ceil(value: Decimal) -> int:
    """Return ``value`` rounded up to a whole number."""
    return int(value.to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------------------
# Configuration (config/thresholds.yaml ``cr`` namespace)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreativeConfig:
    """The ``cr.*`` thresholds of one run, read once from the loaded configuration.

    Attributes:
        min_impressions: ``cr.min_impressions``: a variant has at least this many window
            impressions.
        min_variants: ``cr.min_variants``: a campaign is analysed with at least this many.
        alpha: ``cr.alpha``: CR1 calls a pair distinguishable below it; the significance
            level of every sample estimate.
        power: ``cr.power``: the power of every sample estimate.
        target_lift: ``cr.target_lift``: the relative lift CR3 sizes the next test for.
        fisher_min_expected: ``cr.fisher_min_expected``: Fisher's exact test when any
            expected cell is below it.
        ci_level: ``cr.ci_level``: the confidence level of CR2's Wilson intervals.

    Raises:
        ValueError: On construction, for a value outside its valid range, including a power
            not above alpha, which no sample estimate can use.
    """

    min_impressions: int
    min_variants: int
    alpha: Decimal
    power: Decimal
    target_lift: Decimal
    fisher_min_expected: Decimal
    ci_level: Decimal

    def __post_init__(self) -> None:
        """Validate the ranges the statistics rely on."""
        if self.min_impressions < 1:
            raise ValueError(f"cr.min_impressions must be >= 1, found {self.min_impressions}")
        if self.min_variants < 2:
            raise ValueError(f"cr.min_variants must be >= 2, found {self.min_variants}")
        for name, value in (("cr.alpha", self.alpha), ("cr.ci_level", self.ci_level)):
            if not _ZERO < value < _ONE:
                raise ValueError(f"{name} must lie in (0, 1), found {value}")
        if not self.alpha < self.power < _ONE:
            raise ValueError(
                f"cr.power must lie in (cr.alpha, 1) = ({self.alpha}, 1), found {self.power}"
            )
        if self.target_lift <= _ZERO:
            raise ValueError(f"cr.target_lift must be > 0, found {self.target_lift}")
        if self.fisher_min_expected < _ZERO:
            raise ValueError(
                f"cr.fisher_min_expected must be >= 0, found {self.fisher_min_expected}"
            )

    @classmethod
    def from_thresholds(cls, thresholds: Thresholds) -> CreativeConfig:
        """Read the ``cr.*`` keys; there is no code default for any of them.

        Raises:
            ConfigError: When a key is missing or has the wrong type.
            ValueError: When a value is outside its valid range.
        """
        return cls(
            min_impressions=thresholds.int("cr.min_impressions"),
            min_variants=thresholds.int("cr.min_variants"),
            alpha=thresholds.decimal("cr.alpha"),
            power=thresholds.decimal("cr.power"),
            target_lift=thresholds.decimal("cr.target_lift"),
            fisher_min_expected=thresholds.decimal("cr.fisher_min_expected"),
            ci_level=thresholds.decimal("cr.ci_level"),
        )


# ---------------------------------------------------------------------------
# Variants and pooled stage measures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Creative:
    """A creative of the campaign under analysis, with its window totals when it delivered."""

    creative_id: int
    key: str
    name: str
    info: CreativeInfo | None
    totals: CreativeTotals | None

    @property
    def impressions(self) -> int:
        """Window impressions; 0 for a creative without window facts."""
        return 0 if self.totals is None else self.totals.impressions


def _stage_counts(totals: CreativeTotals, stage: StageSpec) -> tuple[int, int] | None:
    """Return ``(successes, trials)`` of one creative at one stage; ``None`` when unreported."""
    successes: int | None = getattr(totals, stage.numerator)
    trials: int | None = getattr(totals, stage.denominator)
    if successes is None or trials is None:
        return None
    return successes, trials


def _stages_for(objective: str) -> tuple[StageSpec, ...]:
    """Lead-gen campaigns use the whole funnel; every other objective only the click rate."""
    return STAGES if objective == OBJ_LINKEDIN_LEAD_GEN else STAGES[:1]


@dataclass(frozen=True, slots=True)
class _EndToEnd:
    """CR1's end-to-end rate of an objective group: ``numerator / impressions``."""

    label: str
    numerator: str
    noun: str


_LEAD_RATE: Final = _EndToEnd("lead rate (leads ÷ impressions)", "leads", "leads")
_CLICK_RATE: Final = _EndToEnd("click rate (clicks ÷ impressions)", "clicks", "clicks")


def _end_to_end(objective: str) -> _EndToEnd:
    """CR1's end-to-end rate: leads / impressions in lead gen, clicks / impressions otherwise."""
    return _LEAD_RATE if objective == OBJ_LINKEDIN_LEAD_GEN else _CLICK_RATE


def _level(value: Decimal) -> str:
    """Render a configured level or ratio as written, without trailing zeros (0.80 is 0.8)."""
    return f"{value.normalize():f}"


@dataclass(frozen=True, slots=True)
class _Measure:
    """One stage of one campaign: its pooled variants against the account baseline (CR2).

    Attributes:
        stage: The stage.
        successes: Pooled successes of the campaign's variants; ``None`` when unmeasured.
        trials: Pooled trials (the stage's own denominator); ``None`` when unmeasured.
        interval: The Wilson interval of the pooled rate; ``None`` when unmeasured.
        baseline: The account baseline rate; ``None`` when the account has no exposure.
        unmeasured: Why the campaign's stage has no interval, when it has none.
    """

    stage: StageSpec
    successes: int | None
    trials: int | None
    interval: Interval | None
    baseline: Decimal | None
    unmeasured: str | None

    @property
    def weak(self) -> bool:
        """CR2: the interval's upper bound lies below the baseline."""
        return (
            self.interval is not None
            and self.baseline is not None
            and self.interval.high < self.baseline
        )

    @property
    def shortfall(self) -> Decimal | None:
        """``(baseline - rate) / baseline``: the relative shortfall; ``None`` at a 0 baseline."""
        if self.interval is None or self.baseline is None or self.baseline <= 0:
            return None
        gap = _exact().subtract(self.baseline, self.interval.point)
        return _exact().divide(gap, self.baseline).quantize(_QUANTUM)

    @property
    def relative(self) -> Decimal | None:
        """The rate relative to its baseline (``rate / baseline``), for exploratory selection.

        A zero rate is the lowest rate any stage can show, so it ranks lowest whatever the
        baseline. That includes a zero baseline: the baseline pools every creative of the
        objective, the campaign's own among them, so a zero baseline means the stage recorded
        no success account-wide, which CR3 then reports as "baseline rate is 0".
        """
        if self.interval is None or self.baseline is None:
            return None
        if self.interval.point == 0:
            return _ZERO
        if self.baseline <= 0:
            return None
        return _exact().divide(self.interval.point, self.baseline).quantize(_QUANTUM)

    def to_json(self) -> dict[str, Any]:
        """Return the stage row of ``evidence.extra.stages``."""
        interval = self.interval
        return {
            "stage": self.stage.key,
            "label": self.stage.label,
            "successes": self.successes,
            "trials": self.trials,
            "unit": self.stage.trial_unit,
            "rate": None if interval is None else interval.point,
            "ci_low": None if interval is None else interval.low,
            "ci_high": None if interval is None else interval.high,
            "baseline": self.baseline,
            "weak": self.weak,
            "shortfall": self.shortfall,
            "unmeasured": self.unmeasured,
        }


type _BaselineKey = tuple[int, str]
"""``(ad account id, objective group)``: the scope of an account baseline."""


def _account_baselines(
    ctx: AnalysisContext,
) -> dict[_BaselineKey, dict[StageKey, tuple[int, int]]]:
    """Pool every LinkedIn creative's window totals per account, objective group and stage.

    AAP 0.5.4.6: the baseline is the stage rate "across all LinkedIn creatives of the same
    objective in the window". It is kept per ad account, as every account baseline is (AAP
    0.5.4.1), and pools every creative with window facts, variants and excluded creatives
    alike. A creative that does not report a stage's counts adds nothing to that stage.
    """
    pooled: dict[_BaselineKey, dict[StageKey, tuple[int, int]]] = defaultdict(dict)
    for totals in ctx.creative_totals.values():
        campaign = ctx.campaigns.get(totals.campaign_id)
        if campaign is None or campaign.source != "linkedin":
            continue
        stages = pooled[(campaign.account_id, campaign.objective_group)]
        for stage in _stages_for(campaign.objective_group):
            counts = _stage_counts(totals, stage)
            if counts is None:
                continue
            successes, trials = stages.get(stage.key, (0, 0))
            stages[stage.key] = (successes + counts[0], trials + counts[1])
    return dict(pooled)


def _measure(
    stage: StageSpec,
    variants: Sequence[_Creative],
    baseline_counts: tuple[int, int] | None,
    config: CreativeConfig,
) -> _Measure:
    """Pool a campaign's variants at one stage and compare them with the account baseline."""
    baseline = None if baseline_counts is None else _ratio(*baseline_counts)
    successes = trials = 0
    for variant in variants:
        counts = None if variant.totals is None else _stage_counts(variant.totals, stage)
        if counts is None:
            reason = f"{stage.success_unit} or {stage.trial_unit} not reported for every variant"
            return _Measure(stage, None, None, None, baseline, reason)
        successes += counts[0]
        trials += counts[1]
    if trials == 0:
        return _Measure(stage, successes, trials, None, baseline, f"no {stage.trial_unit}")
    if successes > trials:
        reason = (
            f"inconsistent counts: {_count(successes)} {stage.success_unit} exceed "
            f"{_count(trials)} {stage.trial_unit}"
        )
        return _Measure(stage, successes, trials, None, baseline, reason)
    interval = wilson(successes, trials, confidence=config.ci_level)
    return _Measure(stage, successes, trials, interval, baseline, None)


def _select_stage(measures: Sequence[_Measure]) -> tuple[_Measure, bool] | None:
    """CR2: return the reported stage and whether it is exploratory (no stage weak).

    The weak stage with the largest relative shortfall wins; with no weak stage, the stage
    with the lowest rate relative to its baseline (:attr:`_Measure.relative`) is tested,
    labelled exploratory. Ties go to the earlier funnel stage. ``None`` when no stage can be
    compared with a baseline.
    """
    weak = [(m.shortfall, -index, m) for index, m in enumerate(measures) if m.weak]
    if weak:
        best = max(weak, key=lambda item: (item[0] if item[0] is not None else _ZERO, item[1]))
        return best[2], False
    ranked = [
        (relative, index, m)
        for index, m in enumerate(measures)
        if (relative := m.relative) is not None
    ]
    if not ranked:
        return None
    lowest = min(ranked, key=lambda item: (item[0], item[1]))
    return lowest[2], True


# ---------------------------------------------------------------------------
# CR3: the next test
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NextTestPlan:
    """CR3's next test for one stage: one variable, two variants and the sample per variant.

    Attributes:
        stage: The tested stage.
        variable: Machine key of the one variable to change (:data:`STAGE_VARIABLE_KEY`).
        variable_label: Its display label (:data:`STAGE_VARIABLE`).
        keep_unchanged: Display labels of every other element, held identical.
        unit: The stage's own denominator: "impressions", or "form opens" for completion.
        baseline: The account baseline rate the lift applies to.
        target: ``baseline x (1 + cr.target_lift)``; ``None`` without a baseline.
        per_variant: Trials per variant to detect the lift; ``None`` when not estimable.
        projected_impressions: Completion rate only: ``per_variant`` divided by the observed
            open rate, in impressions; ``None`` otherwise or when not estimable.
        observed_open_rate: Completion rate only: the variants' pooled open rate.
        days: Days of current per-variant delivery the test needs; ``None`` when not
            estimable.
        not_estimable_reason: Why ``per_variant`` is not estimable; ``None`` when it is.
        projection_reason: Why a projection (impressions or days) is not estimable while
            ``per_variant`` is; ``None`` otherwise.
        exploratory: Whether the stage was chosen with no weak stage (CR2).
    """

    stage: StageKey
    variable: str
    variable_label: str
    keep_unchanged: tuple[str, ...]
    unit: str
    baseline: Decimal | None
    target: Decimal | None
    per_variant: int | None
    projected_impressions: int | None
    observed_open_rate: Decimal | None
    days: int | None
    not_estimable_reason: str | None
    projection_reason: str | None
    exploratory: bool

    @property
    def estimable(self) -> bool:
        """Whether the per-variant sample could be computed."""
        return self.per_variant is not None

    def action(self, config: CreativeConfig) -> str:
        """Render the CR3 action sentence from this plan and the configured levels.

        Example: "Next test: change only headline; keep everything else identical; 2 variants;
        35,427 impressions per variant to detect a 20% lift (alpha 0.05, power 0.8); ≈ 51 days
        at current per-variant delivery (projection)" for a 1.2% baseline at 700 impressions per
        variant per day, with the Greek letter for alpha. The
        sentence keeps the AAP's literal "Next test: change only" prefix; an exploratory plan
        ends with :data:`EXPLORATORY_TEXT` instead of relabelling that prefix.
        """
        head = (
            f"Next test: change only {self.variable_label}; keep everything else identical; "
            f"{NEXT_TEST_VARIANTS} variants"
        )
        if self.per_variant is None:
            parts = [f"{head}; sample per variant {NOT_ESTIMABLE} ({self.not_estimable_reason})"]
            if self.exploratory:
                parts.append(EXPLORATORY_TEXT)
            return "; ".join(parts)
        parts = [
            f"{head}; {_count(self.per_variant)} {self.unit} per variant to detect a "
            f"{_lift(config.target_lift)} lift ({_ALPHA} {_level(config.alpha)}, "
            f"power {_level(config.power)})"
        ]
        if self.projected_impressions is not None:
            parts.append(
                f"≈ {_count(self.projected_impressions)} impressions per variant "
                "(projected through the observed open rate)"
            )
        if self.days is not None:
            parts.append(f"≈ {_count(self.days)} days at current per-variant delivery (projection)")
        elif self.projection_reason is not None:
            parts.append(f"delivery projection {NOT_ESTIMABLE} ({self.projection_reason})")
        if self.exploratory:
            parts.append(EXPLORATORY_TEXT)
        return "; ".join(parts)

    def to_json(self) -> dict[str, Any]:
        """Return ``evidence.extra.next_test``."""
        return {
            "stage": self.stage,
            "variable": self.variable,
            "variable_label": self.variable_label,
            "keep_unchanged": list(self.keep_unchanged),
            "variants": NEXT_TEST_VARIANTS,
            "unit": self.unit,
            "baseline": self.baseline,
            "target": self.target,
            "sample_needed": NOT_ESTIMABLE if self.per_variant is None else self.per_variant,
            "not_estimable_reason": self.not_estimable_reason,
            "projected": (
                None
                if self.projected_impressions is None
                else {
                    "value": self.projected_impressions,
                    "unit": "impressions",
                    "basis": "projected through the observed open rate",
                }
            ),
            "projected_unit": None if self.projected_impressions is None else "impressions",
            "observed_open_rate": self.observed_open_rate,
            "days": self.days,
            "projection_reason": self.projection_reason,
            "exploratory": self.exploratory,
        }


def plan_next_test(
    stage: StageKey,
    *,
    baseline: Decimal | None,
    exposure: int,
    daily_impressions_per_variant: Decimal | None,
    config: CreativeConfig,
    open_counts: tuple[int, int] | None = None,
    exploratory: bool = False,
) -> NextTestPlan:
    """CR3: size the two-variant test of ``stage`` in the stage's own denominator.

    The sample is ``stats.required_n(baseline, baseline x (1 + cr.target_lift), alpha=cr.alpha,
    power=cr.power)``: Cohen's h at power ``cr.power`` and significance ``cr.alpha`` (AAP
    0.2.4), counted in impressions for click and open rate and in form opens for completion
    rate, which is then projected to impressions through the observed open rate. The days
    projection divides the impressions each variant needs by the current per-variant delivery.

    "Not estimable" replaces the figures, in this order, when: the stage has zero exposure
    (its own denominator is 0 across the variants); the baseline is unavailable; the
    baseline rate is 0; the target exceeds 100%; or no sample attains the power.

    Args:
        stage: The tested stage.
        baseline: The account baseline rate of the stage.
        exposure: The variants' pooled count of the stage's own denominator in the window.
        daily_impressions_per_variant: Current impressions per variant per delivery day;
            ``None`` or 0 leaves the days projection not estimable ("zero exposure").
        config: The ``cr.*`` thresholds.
        open_counts: Completion rate only: the variants' pooled ``(form opens, impressions)``,
            whose ratio is the observed open rate of the projection.
        exploratory: Whether the stage was chosen with no weak stage.

    Returns:
        The plan.

    Raises:
        KeyError: For an unknown stage.
    """
    spec = _STAGES_BY_KEY[stage]
    variable = STAGE_VARIABLE_KEY[stage]
    keep = tuple(label for key, label in TEST_ELEMENTS if key != variable)
    target = None if baseline is None else _exact().multiply(baseline, _ONE + config.target_lift)
    reason: str | None = None
    per_variant: int | None = None
    if exposure <= 0:
        reason = NOT_ESTIMABLE_ZERO_EXPOSURE
    elif baseline is None or target is None:
        reason = NOT_ESTIMABLE_BASELINE_UNAVAILABLE
    elif baseline <= 0:
        reason = NOT_ESTIMABLE_ZERO_BASELINE
    elif target > _ONE:
        reason = NOT_ESTIMABLE_TARGET_ABOVE_100
    else:
        per_variant = required_n(baseline, target, alpha=config.alpha, power=config.power)
        if per_variant is None:
            reason = NOT_ESTIMABLE_POWER_UNREACHABLE

    observed_open_rate: Decimal | None = None
    projected: int | None = None
    projection_reason: str | None = None
    impressions_needed: int | None = per_variant if spec.trial_unit == "impressions" else None
    if stage == "completion_rate":
        observed_open_rate = None if open_counts is None else _ratio(*open_counts)
        if per_variant is not None:
            if open_counts is None or open_counts[0] <= 0 or observed_open_rate is None:
                projection_reason = NOT_ESTIMABLE_ZERO_EXPOSURE
            else:
                opens, impressions = open_counts
                projected = _ceil_div(per_variant * impressions, opens)
                impressions_needed = projected

    days: int | None = None
    if impressions_needed is not None:
        if daily_impressions_per_variant is None or daily_impressions_per_variant <= 0:
            projection_reason = NOT_ESTIMABLE_ZERO_EXPOSURE
        else:
            days = _ceil(
                _exact().divide(Decimal(impressions_needed), daily_impressions_per_variant)
            )

    return NextTestPlan(
        stage=stage,
        variable=variable,
        variable_label=STAGE_VARIABLE[stage],
        keep_unchanged=keep,
        unit=spec.trial_unit,
        baseline=baseline,
        target=None if target is None else target.quantize(_QUANTUM),
        per_variant=per_variant,
        projected_impressions=projected,
        observed_open_rate=observed_open_rate,
        days=days,
        not_estimable_reason=reason,
        projection_reason=projection_reason if per_variant is not None else None,
        exploratory=exploratory,
    )


# ---------------------------------------------------------------------------
# Campaigns under analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Campaign:
    """One LinkedIn campaign's creatives, split into variants and excluded creatives.

    Attributes:
        info: The campaign.
        currency: The account currency of its spend.
        variants: Creatives with at least ``cr.min_impressions`` window impressions, ordered
            by name, then key.
        excluded: Every other creative of the campaign, most impressions first.
        spend: Window spend of all its creatives, the group's ranking key.
        delivered: Whether any creative had window impressions.
    """

    info: CampaignInfo
    currency: str
    variants: tuple[_Creative, ...]
    excluded: tuple[_Creative, ...]
    spend: Decimal
    delivered: bool

    @property
    def ref(self) -> EntityRef:
        """The campaign as an entity."""
        return EntityRef(
            "linkedin", ENTITY_CAMPAIGN, self.info.id, self.info.external_id, self.info.name
        )

    @property
    def subject(self) -> DecisionSubject:
        """The campaign whose unresolved discrepancy holds this campaign's CR items (P1)."""
        return DecisionSubject("campaign", self.info.id)

    def summary(self) -> dict[str, Any]:
        """Return ``evidence.extra.campaign``."""
        return {
            "id": self.info.id,
            "external_id": self.info.external_id,
            "name": self.info.name,
            "objective_group": self.info.objective_group,
        }


def _urn_tail(urn: str) -> str:
    """Return the id part of a URN (``urn:li:sponsoredCreative:42`` → ``42``)."""
    return urn.rsplit(":", 1)[-1]


def _creatives_by_campaign(
    ctx: AnalysisContext,
) -> dict[int, list[tuple[int, CreativeInfo | None, CreativeTotals | None]]]:
    """Return ``{campaign id: [(creative id, info, window totals)]}`` for LinkedIn campaigns.

    A creative is listed under its window facts' campaign when it delivered, else under its
    entity's campaign, so every creative of the active dataset appears exactly once.
    """
    listed: dict[int, list[tuple[int, CreativeInfo | None, CreativeTotals | None]]] = defaultdict(
        list
    )
    seen: set[int] = set()
    for creative_id, totals in sorted(ctx.creative_totals.items()):
        listed[totals.campaign_id].append((creative_id, ctx.creatives.get(creative_id), totals))
        seen.add(creative_id)
    for creative_id, info in sorted(ctx.creatives.items()):
        if creative_id not in seen:
            listed[info.campaign_id].append((creative_id, info, None))
    return {
        campaign_id: rows
        for campaign_id, rows in listed.items()
        if (campaign := ctx.campaigns.get(campaign_id)) is not None
        and campaign.source == "linkedin"
    }


def _named(
    rows: Sequence[tuple[int, CreativeInfo | None, CreativeTotals | None]],
) -> list[_Creative]:
    """Name each creative by its headline, disambiguating repeats within the campaign.

    Variants often share a headline and differ in image or intro text, so a repeated name
    gains the creative's id: "Pipeline guide (#42)".
    """
    bases: list[tuple[int, CreativeInfo | None, CreativeTotals | None, str, str]] = []
    for creative_id, info, totals in rows:
        key = info.urn if info is not None else f"creative:{creative_id}"
        tail = _urn_tail(info.urn) if info is not None else str(creative_id)
        headline = None if info is None or info.headline is None else info.headline.strip()
        bases.append((creative_id, info, totals, key, headline or f"Creative {tail}"))
    repeats = Counter(base for *_, base in bases)
    creatives: list[_Creative] = []
    for creative_id, info, totals, key, base in bases:
        tail = _urn_tail(key)
        name = f"{base} (#{tail})" if repeats[base] > 1 else base
        creatives.append(_Creative(creative_id, key, name, info, totals))
    return creatives


def _campaigns(ctx: AnalysisContext, config: CreativeConfig) -> list[_Campaign]:
    """Collect every LinkedIn campaign with creatives, split into variants and the excluded."""
    campaigns: list[_Campaign] = []
    for campaign_id, rows in sorted(_creatives_by_campaign(ctx).items()):
        info = ctx.campaigns[campaign_id]
        creatives = _named(rows)
        variants = sorted(
            (c for c in creatives if c.impressions >= config.min_impressions),
            key=lambda c: (c.name, c.key),
        )
        excluded = sorted(
            (c for c in creatives if c.impressions < config.min_impressions),
            key=lambda c: (-c.impressions, c.name, c.key),
        )
        spend = sum((c.totals.cost for c in creatives if c.totals is not None), _ZERO)
        campaigns.append(
            _Campaign(
                info=info,
                currency=ctx.account_currency(campaign_id),
                variants=tuple(variants),
                excluded=tuple(excluded),
                spend=spend,
                delivered=any(c.impressions > 0 for c in creatives),
            )
        )
    return campaigns


def _variant_row(creative: _Creative, objective: str, currency: str) -> dict[str, Any]:
    """Return one row of ``evidence.extra.variants``, the card's variants table.

    Rates a non-lead-gen campaign does not use, or whose counts are unreported or
    inconsistent, are ``None`` ("unavailable", never 0).
    """
    totals = creative.totals
    info = creative.info
    row: dict[str, Any] = {
        "creative_id": creative.creative_id,
        "urn": creative.key,
        "name": creative.name,
        "headline": None if info is None else info.headline,
        "cta_label": None if info is None else info.cta_label,
    }
    if totals is None:
        return row
    lead_gen = objective == OBJ_LINKEDIN_LEAD_GEN
    outcome = totals.leads if lead_gen else totals.website_conversions
    opens = totals.form_opens
    row.update(
        {
            "impressions": totals.impressions,
            "clicks": totals.clicks,
            "form_opens": opens,
            "leads": totals.leads,
            "click_rate": _ratio(totals.clicks, totals.impressions),
            "open_rate": (
                _ratio(opens, totals.impressions) if lead_gen and opens is not None else None
            ),
            "completion_rate": (
                _ratio(totals.leads, opens)
                if lead_gen and opens is not None and totals.leads is not None
                else None
            ),
            "cost": totals.cost,
            "currency": currency,
            "outcomes": outcome,
            "cost_per_outcome": cost_per_outcome(
                totals.cost, None if outcome is None else Decimal(outcome)
            ),
            "delivery_days": totals.delivery_days,
        }
    )
    return row


def _excluded_row(creative: _Creative) -> dict[str, Any]:
    """Return one row of ``evidence.extra.excluded``: a creative under the variant minimum."""
    return {
        "creative_id": creative.creative_id,
        "urn": creative.key,
        "name": creative.name,
        "impressions": creative.impressions,
    }


def _daily_impressions_per_variant(variants: Sequence[_Creative]) -> Decimal | None:
    """Mean impressions per variant per delivery day: the current per-variant delivery."""
    rates = [
        _exact().divide(Decimal(c.totals.impressions), Decimal(c.totals.delivery_days))
        for c in variants
        if c.totals is not None and c.totals.delivery_days > 0
    ]
    if not rates:
        return None
    return _exact().divide(sum(rates, _ZERO), Decimal(len(rates))).quantize(_QUANTUM)


# ---------------------------------------------------------------------------
# CR1: pairwise distinguishability
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pair:
    """One CR1 comparison of two variants' end-to-end rates.

    Attributes:
        a: The first variant (variant order).
        b: The second variant.
        x_a: Successes of ``a`` (leads, or clicks outside lead gen); ``None`` when unreported.
        n_a: Impressions of ``a``.
        x_b: Successes of ``b``.
        n_b: Impressions of ``b``.
        test: The unadjusted test; ``None`` when the pair cannot be compared.
        unavailable: Why the pair cannot be compared, when it cannot.
        p_adjusted: The Holm-adjusted p-value across the campaign's compared pairs.
    """

    a: _Creative
    b: _Creative
    x_a: int | None
    n_a: int
    x_b: int | None
    n_b: int
    test: ProportionTest | None
    unavailable: str | None
    p_adjusted: Decimal | None = None

    def ahead(self) -> _Creative | None:
        """The variant with the higher observed rate; ``None`` when level or not compared."""
        if self.test is None or self.x_a is None or self.x_b is None:
            return None
        left, right = self.x_a * self.n_b, self.x_b * self.n_a
        if left == right:
            return None
        return self.a if left > right else self.b


def _compare(a: _Creative, b: _Creative, objective: str, config: CreativeConfig) -> _Pair:
    """Run CR1's test on one pair: Fisher's exact test or the two-proportion z-test."""
    rate = _end_to_end(objective)
    n_a, n_b = a.impressions, b.impressions
    x_a: int | None = None if a.totals is None else getattr(a.totals, rate.numerator)
    x_b: int | None = None if b.totals is None else getattr(b.totals, rate.numerator)
    if x_a is None or x_b is None:
        return _Pair(a, b, x_a, n_a, x_b, n_b, None, f"{rate.noun} not reported")
    if n_a == 0 or n_b == 0:
        return _Pair(a, b, x_a, n_a, x_b, n_b, None, NOT_ESTIMABLE_ZERO_IMPRESSIONS)
    if x_a > n_a or x_b > n_b:
        reason = f"inconsistent counts: {rate.noun} exceed impressions"
        return _Pair(a, b, x_a, n_a, x_b, n_b, None, reason)
    test = compare_proportions(x_a, n_a, x_b, n_b, fisher_min_expected=config.fisher_min_expected)
    if test is None:
        return _Pair(a, b, x_a, n_a, x_b, n_b, None, NOT_ESTIMABLE_ZERO_IMPRESSIONS)
    return _Pair(a, b, x_a, n_a, x_b, n_b, test, None)


def _pairs(campaign: _Campaign, config: CreativeConfig) -> list[_Pair]:
    """Compare every pair of variants, then Holm-adjust the compared pairs together."""
    objective = campaign.info.objective_group
    variants = campaign.variants
    pairs = [
        _compare(variants[i], variants[j], objective, config)
        for i in range(len(variants))
        for j in range(i + 1, len(variants))
    ]
    tested = [index for index, pair in enumerate(pairs) if pair.test is not None]
    adjusted = holm(
        [p.test.p_value for p in (pairs[i] for i in tested) if p.test is not None],
        alpha=config.alpha,
    )
    for index, p_adjusted in zip(tested, adjusted, strict=True):
        pair = pairs[index]
        pairs[index] = _Pair(
            pair.a, pair.b, pair.x_a, pair.n_a, pair.x_b, pair.n_b, pair.test, None, p_adjusted
        )
    return pairs


def _pair_candidate(
    ctx: AnalysisContext,
    campaign: _Campaign,
    pair: _Pair,
    comparisons: int,
    config: CreativeConfig,
) -> Candidate:
    """Build one CR1 item: "Distinguishable (adjusted p = …)" or "{A} ahead, not yet …".

    P7: neither the action nor the reason calls a variant a winner, better or best; a
    distinguishable pair states only which observed rate is higher.
    """
    rate = _end_to_end(campaign.info.objective_group)
    test, p_adjusted = pair.test, pair.p_adjusted
    distinguishable = p_adjusted is not None and p_adjusted < config.alpha
    leader = pair.ahead()
    first, second = (pair.a, pair.b) if leader is not pair.b else (pair.b, pair.a)
    title = f"{first.name} vs {second.name}"

    sample_needed: int | str | None = None
    not_estimable_reason: str | None = None
    if not distinguishable:
        if test is None or pair.x_a is None or pair.x_b is None:
            not_estimable_reason = pair.unavailable or NOT_ESTIMABLE_ZERO_IMPRESSIONS
        elif leader is None:
            not_estimable_reason = NOT_ESTIMABLE_NO_DIFFERENCE
        else:
            sample_needed = additional_n_for_difference(
                pair.x_a, pair.n_a, pair.x_b, pair.n_b, alpha=config.alpha, power=config.power
            )
            if sample_needed is None:
                not_estimable_reason = NOT_ESTIMABLE_POWER_UNREACHABLE
        if not_estimable_reason is not None:
            sample_needed = NOT_ESTIMABLE

    adjusted_text = "" if p_adjusted is None else f" (adjusted p = {_p(p_adjusted)})"
    if distinguishable:
        label = f"Distinguishable{adjusted_text}"
    elif test is None:
        label = f"Not yet distinguishable; comparison {NOT_ESTIMABLE} ({not_estimable_reason})"
    elif leader is None:
        label = (
            f"Level, not yet distinguishable{adjusted_text}; more impressions per variant "
            f"needed: {NOT_ESTIMABLE} ({not_estimable_reason})"
        )
    elif isinstance(sample_needed, int):
        if sample_needed > 0:
            needed = (
                f"about {_count(sample_needed)} more impressions per variant needed at the "
                "observed difference"
            )
        else:
            needed = (
                f"no more impressions per variant needed: the current samples already reach "
                f"power {_level(config.power)} at the observed difference"
            )
        label = f"{leader.name} ahead, not yet distinguishable{adjusted_text}; {needed}"
    else:
        label = (
            f"{leader.name} ahead, not yet distinguishable{adjusted_text}; more impressions "
            f"per variant needed: {NOT_ESTIMABLE} ({not_estimable_reason})"
        )

    rate_a = None if pair.x_a is None else _ratio(pair.x_a, pair.n_a)
    rate_b = None if pair.x_b is None else _ratio(pair.x_b, pair.n_b)
    reason = _pair_reason(campaign, pair, rate.label, comparisons, config, distinguishable)
    metrics = [
        Metric("rate_a", f"{pair.a.name} {rate.label}", rate_a, "ratio"),
        Metric("rate_b", f"{pair.b.name} {rate.label}", rate_b, "ratio"),
        Metric(f"{rate.noun}_a", f"{pair.a.name} {rate.noun}", pair.x_a, rate.noun),
        Metric(f"{rate.noun}_b", f"{pair.b.name} {rate.noun}", pair.x_b, rate.noun),
        Metric("impressions_a", f"{pair.a.name} impressions", pair.n_a, "impressions"),
        Metric("impressions_b", f"{pair.b.name} impressions", pair.n_b, "impressions"),
    ]
    if test is not None:
        metrics.append(
            Metric("expected_min_cell", "Smallest expected cell", test.expected_min, "count")
        )
    if sample_needed is not None:
        metrics.append(
            Metric(
                "additional_impressions_per_variant",
                "More impressions per variant needed at the observed difference",
                sample_needed,
                "impressions",
            )
        )
    evidence = ctx.make_evidence(
        required_datasets=REQUIRED_DATASETS,
        crm_scope=None,
        metrics=metrics,
        # The variant minimum that admitted both creatives: the only sample gate of the pair.
        sample=Sample(min(pair.n_a, pair.n_b), config.min_impressions, "impressions"),
        test=(
            None
            if test is None
            else TestResult(
                name=test.method,
                p_value=test.p_value,
                p_adjusted=p_adjusted,
                alpha=config.alpha,
            )
        ),
        threshold_keys=CR1_THRESHOLD_KEYS,
        extra={
            "campaign": campaign.summary(),
            "outcome": rate.noun,
            "pair": [pair.a.key, pair.b.key],
            "names": [pair.a.name, pair.b.name],
            "ahead": None if leader is None else leader.key,
            "distinguishable": distinguishable,
            "label": label,
            "test": (
                None
                if test is None
                else {
                    "method": test.method,
                    "label": _TEST_LABELS[test.method],
                    "expected_min_cell": test.expected_min,
                }
            ),
            "comparisons": comparisons,
            "sample_needed": sample_needed,
            "not_estimable_reason": not_estimable_reason,
            "variant_count": len(campaign.variants),
            "excluded": [_excluded_row(c) for c in campaign.excluded],
            "campaign_spend": campaign.spend,
            "currency": campaign.currency,
        },
    )
    flags: tuple[Flag, ...] = ()
    if not distinguishable:
        flags = (Flag(FLAG_NOT_DISTINGUISHABLE, f"Not yet distinguishable{adjusted_text}"),)
    return Candidate(
        rule_id=CR1,
        section=CREATIVE,
        group=creative_group(campaign.info.id),
        entity_ref=EntityRef(
            "linkedin",
            ENTITY_CREATIVE_PAIR,
            None,
            creative_pair_external_id(pair.a.key, pair.b.key),
            f"{pair.a.name} vs {pair.b.name}",
        ),
        action=f"{title}: {label}",
        reason=reason,
        evidence=evidence,
        rank_key=(
            1,
            _NO_P_RANK if p_adjusted is None else p_adjusted,
            pair.a.name,
            pair.b.name,
        ),
        flags=flags,
        required_datasets=REQUIRED_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=(campaign.subject,),
    )


def _pair_reason(
    campaign: _Campaign,
    pair: _Pair,
    rate_label: str,
    comparisons: int,
    config: CreativeConfig,
    distinguishable: bool,
) -> str:
    """Render the CR1 reason sentence from the pair's own counts and test."""

    def side(creative: _Creative, successes: int | None, trials: int) -> str:
        if successes is None:
            return f"{creative.name} unavailable"
        value = _ratio(successes, trials)
        shown = "unavailable" if value is None else _percent(value)
        return f"{creative.name} {shown} ({_count(successes)} of {_count(trials)})"

    text = (
        f"{rate_label[:1].upper()}{rate_label[1:]}: {side(pair.a, pair.x_a, pair.n_a)} vs "
        f"{side(pair.b, pair.x_b, pair.n_b)}"
    )
    test, p_adjusted = pair.test, pair.p_adjusted
    if test is None or p_adjusted is None:
        return f"{text}; the pair could not be tested ({pair.unavailable})."
    plural = "pair" if comparisons == 1 else "pairs"
    text += (
        f"; {_TEST_LABELS[test.method]} p = {_p(test.p_value)}, Holm-adjusted across "
        f"{comparisons} {plural} in {campaign.info.name} = {_p(p_adjusted)}, "
        f"against {_ALPHA} {_level(config.alpha)}"
    )
    if distinguishable:
        return f"{text}; the two rates are statistically distinguishable."
    return f"{text}; the gap is not yet distinguishable from noise."


# ---------------------------------------------------------------------------
# CR2 + CR3: the campaign's weak stage and its next test
# ---------------------------------------------------------------------------


def _stage_sentence(measure: _Measure | None, exploratory: bool) -> str:
    """CR2's reason: "{Stage} is below account baseline: {rate} [{low} to {high}] vs {baseline}".

    The interval renders with an en dash between its bounds; with no weak stage (including an
    exploratory pick) the reason is :data:`NO_WEAK_STAGE_TEXT`.
    """
    if measure is None or exploratory or measure.interval is None or measure.baseline is None:
        return NO_WEAK_STAGE_TEXT
    interval = measure.interval
    return (
        f"{measure.stage.label} is below account baseline: {_percent(interval.point)} "
        f"[{_percent(interval.low)}{_EN_DASH}{_percent(interval.high)}] vs "
        f"{_percent(measure.baseline)}"
    )


def _weak_stage_json(measure: _Measure, exploratory: bool) -> dict[str, Any]:
    """Return ``evidence.extra.weak_stage``: the reported stage, its interval and baseline."""
    interval = measure.interval
    return {
        "stage": measure.stage.key,
        "label": measure.stage.label,
        "rate": None if interval is None else interval.point,
        "interval": None if interval is None else [interval.low, interval.high],
        "baseline": measure.baseline,
        "shortfall": measure.shortfall,
        "weak": measure.weak,
        "exploratory": exploratory,
    }


def _test_candidate(
    ctx: AnalysisContext,
    campaign: _Campaign,
    measures: Sequence[_Measure],
    selected: _Measure,
    exploratory: bool,
    config: CreativeConfig,
) -> Candidate:
    """Build the campaign's combined CR2 + CR3 item (``rule_id=CR3``).

    The reason is the CR2 sentence and the action the CR3 next test, so the card names the
    weak stage and the one variable to change in the same place.
    """
    by_key = {m.stage.key: m for m in measures}
    open_measure = by_key.get("open_rate")
    open_counts = (
        None
        if open_measure is None or open_measure.successes is None or open_measure.trials is None
        else (open_measure.successes, open_measure.trials)
    )
    plan = plan_next_test(
        selected.stage.key,
        baseline=selected.baseline,
        exposure=selected.trials or 0,
        daily_impressions_per_variant=_daily_impressions_per_variant(campaign.variants),
        config=config,
        open_counts=open_counts if selected.stage.key == "completion_rate" else None,
        exploratory=exploratory,
    )
    metrics: list[Metric] = []
    baselines: list[Baseline] = []
    for measure in measures:
        key, label, interval = measure.stage.key, measure.stage.label, measure.interval
        metrics.append(Metric(key, label, None if interval is None else interval.point, "ratio"))
        if measure is selected and interval is not None:
            metrics.append(Metric(f"{key}_ci_low", f"{label} interval low", interval.low, "ratio"))
            metrics.append(
                Metric(f"{key}_ci_high", f"{label} interval high", interval.high, "ratio")
            )
        baselines.append(
            Baseline(f"{key}_baseline", f"Account {label.lower()} baseline", measure.baseline)
        )
    metrics.append(
        Metric(
            "sample_per_variant",
            f"{selected.stage.trial_unit.capitalize()} per variant for the next test",
            NOT_ESTIMABLE if plan.per_variant is None else plan.per_variant,
            plan.unit,
        )
    )
    if plan.projected_impressions is not None:
        metrics.append(
            Metric(
                "projected_impressions_per_variant",
                "Impressions per variant (projected through the observed open rate)",
                plan.projected_impressions,
                "impressions",
            )
        )
    if plan.per_variant is not None:
        metrics.append(
            Metric(
                "projected_days",
                "Days at current per-variant delivery (projection)",
                NOT_ESTIMABLE if plan.days is None else plan.days,
                "days",
            )
        )
    metrics.append(Metric("variants", "Variants", len(campaign.variants), "variants"))
    metrics.append(
        Metric("campaign_spend", "Campaign spend", campaign.spend, "currency", campaign.currency)
    )
    objective = campaign.info.objective_group
    evidence = ctx.make_evidence(
        required_datasets=REQUIRED_DATASETS,
        crm_scope=None,
        metrics=metrics,
        baselines=baselines,
        # The variant minimum that admitted the campaign's smallest variant. The next test's
        # per-variant requirement is a plan for future delivery, not a minimum this item
        # failed to meet, so it travels in metrics and extra.next_test: a sample whose
        # observed count is below its requirement reads as an unmet minimum (P6).
        sample=Sample(
            min(c.impressions for c in campaign.variants), config.min_impressions, "impressions"
        ),
        threshold_keys=CR3_THRESHOLD_KEYS,
        extra={
            "campaign": campaign.summary(),
            "variant_count": len(campaign.variants),
            "variants": [_variant_row(c, objective, campaign.currency) for c in campaign.variants],
            "excluded": [_excluded_row(c) for c in campaign.excluded],
            "stages": [m.to_json() for m in measures],
            "weak_stage": _weak_stage_json(selected, exploratory),
            "next_test": plan.to_json(),
            "campaign_spend": campaign.spend,
            "currency": campaign.currency,
        },
    )
    return Candidate(
        rule_id=CR3,
        section=CREATIVE,
        group=creative_group(campaign.info.id),
        entity_ref=campaign.ref,
        action=plan.action(config),
        reason=_stage_sentence(selected, exploratory),
        evidence=evidence,
        rank_key=(0,),
        flags=(),
        required_datasets=REQUIRED_DATASETS,
        increases_spend_on=(),
        decision_subject=None,
        depends_on=(campaign.subject,),
    )


def _variants_withheld(campaign: _Campaign, config: CreativeConfig) -> Withheld:
    """P6: a delivering campaign with too few variants, with its count observed and required."""
    return Withheld(
        rule_id=CR1,
        section=CREATIVE,
        entity=campaign.ref,
        reason=(
            f"variants {len(campaign.variants)} of {config.min_variants} required "
            f"(creatives with at least {_count(config.min_impressions)} window impressions); "
            "copy analysis (CR1-CR3) compares variants within a campaign"
        ),
        observed=len(campaign.variants),
        required=config.min_variants,
        unit="variants",
    )


def _no_stage_withheld(campaign: _Campaign) -> Withheld:
    """CR2/CR3 without any stage comparable with an account baseline."""
    return Withheld(
        rule_id=CR3,
        section=CREATIVE,
        entity=campaign.ref,
        reason="no funnel stage could be measured against an account baseline",
        observed=0,
        required=1,
        unit="stages",
    )


def _campaign_result(
    ctx: AnalysisContext,
    campaign: _Campaign,
    baselines: Mapping[_BaselineKey, Mapping[StageKey, tuple[int, int]]],
    config: CreativeConfig,
) -> RuleResult:
    """Evaluate CR1, CR2 and CR3 for one campaign with enough variants."""
    pairs = _pairs(campaign, config)
    comparisons = sum(1 for pair in pairs if pair.test is not None)
    candidates = [_pair_candidate(ctx, campaign, pair, comparisons, config) for pair in pairs]
    account = baselines.get((campaign.info.account_id, campaign.info.objective_group), {})
    measures = [
        _measure(stage, campaign.variants, account.get(stage.key), config)
        for stage in _stages_for(campaign.info.objective_group)
    ]
    selection = _select_stage(measures)
    if selection is None:
        return RuleResult(candidates=tuple(candidates), withheld=(_no_stage_withheld(campaign),))
    selected, exploratory = selection
    test = _test_candidate(ctx, campaign, measures, selected, exploratory, config)
    return RuleResult(candidates=(test, *candidates))


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Rules CR1, CR2 and CR3: Copy / creative changes for every LinkedIn campaign.

    AAP 0.5.4.6, FR-7. Variants are a campaign's creatives with at least
    ``cr.min_impressions`` window impressions; only campaigns with at least
    ``cr.min_variants`` variants are analysed, and a delivering campaign with fewer is
    withheld with its count (P6).

    * CR1 (``cr.alpha``, ``cr.power``, ``cr.fisher_min_expected``): one item per variant pair.
      Fisher's exact test when an expected cell is below ``cr.fisher_min_expected``, else the
      two-proportion z-test, on leads / impressions (clicks / impressions outside lead gen),
      Holm-adjusted across the campaign's pairs. Rationale: "currently ahead" on a small
      sample is not a result, so a pair is distinguishable only below ``cr.alpha`` after the
      correction, and otherwise the item states the impressions still needed at the observed
      difference, flagged ``not_distinguishable`` (P7).
    * CR2 (``cr.ci_level``): the pooled Wilson interval of each stage against the account
      baseline; a stage is weak when its upper bound is below the baseline, and the largest
      relative shortfall is reported. Rationale: the Wilson interval stays valid at small
      counts and rates near zero, so a stage is called weak only when it is measurably so.
    * CR3 (``cr.target_lift``, ``cr.alpha``, ``cr.power``): the next test changes exactly the
      stage's variable and holds everything else identical, sized in the stage's own
      denominator. Rationale: one changed variable keeps the read clean.

    Args:
        ctx: The analysis context.

    Returns:
        Per analysed campaign, one CR3 item (``rank_key (0,)``) and one CR1 item per pair
        (``rank_key (1, adjusted p, names)``) in the campaign's creative group; withheld lines
        for delivering campaigns with too few variants; no findings or suppression counts.

    Raises:
        ConfigError: When a ``cr.*`` key is missing from the configuration.
        ValueError: When a ``cr.*`` value is outside its valid range (:class:`CreativeConfig`).
    """
    config = CreativeConfig.from_thresholds(ctx.thresholds)
    baselines = _account_baselines(ctx)
    results: list[RuleResult] = []
    withheld: list[Withheld] = []
    for campaign in _campaigns(ctx, config):
        if len(campaign.variants) < config.min_variants:
            if campaign.delivered:
                withheld.append(_variants_withheld(campaign, config))
            continue
        results.append(_campaign_result(ctx, campaign, baselines, config))
    return RuleResult(withheld=tuple(withheld)).merge(*results)
