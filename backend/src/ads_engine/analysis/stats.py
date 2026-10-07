"""Small-sample statistics for the copy (CR1-CR3) and budget (B3, B4) rules.

The rules decide on small samples: a few leads per creative, a handful of weekend outcomes.
Each function here wraps one library method that stays valid at those sizes, so no rule
declares a winner from "currently ahead" counts or a time pattern from noise:

* CR1 distinguishability: :func:`compare_proportions` runs Fisher's exact test when any
  expected cell of the 2x2 table is below ``cr.fisher_min_expected`` (5), otherwise the
  pooled two-proportion z-test. :func:`holm` applies the Holm step-down correction across a
  campaign's pairwise comparisons. :func:`additional_n_for_difference` sizes the extra sample
  a not-yet-distinguishable pair needs at its observed difference.
* CR2 weak stage: :func:`wilson` gives the Wilson score interval of a funnel-stage rate,
  which stays valid at small counts and at rates near zero.
* CR3 next test: :func:`required_n` sizes a two-variant test to detect a relative lift, from
  Cohen's h (:func:`cohens_h`) and the normal power equation, in the tested stage's own
  denominator (impressions for click and open rate, form opens for completion rate).
* B4 weekday/weekend: :func:`poisson_rate_test` compares outcomes per unit of spend with the
  two-sample Poisson score test. It is valid only for unweighted event counts.
* B3 under-utilized: :func:`percentile` is the linear-interpolation percentile (NumPy's
  default ``"linear"`` method) of daily spend, computed exactly in ``Decimal``.

Libraries, never hand-written numerics: ``scipy.stats.fisher_exact`` and, from statsmodels,
``proportions_ztest``, ``multipletests(method="holm")``, ``proportion_confint(method="wilson")``,
``proportion_effectsize``, ``NormalIndPower`` and ``test_poisson_2indep(method="score")``.
The only arithmetic done here is exact bookkeeping: expected cells as fractions, rates as
decimal ratios and the percentile interpolation.

Conventions:

* Boundary types: counts are ``int``; rates, probabilities, p-values, exposures and
  thresholds are ``Decimal`` (``int`` is accepted wherever a ``Decimal`` is, being exact).
  ``float`` exists only inside the library calls. Library results come back through
  ``Decimal(repr(float(value)))`` rounded to 6 decimal places with banker's rounding
  (``ROUND_HALF_EVEN``), independent of the caller's decimal context. Threshold decisions
  inside this module use exact counts, never the rounded values.
* "Not estimable" is ``None``: degenerate input (zero trials, zero exposure, no observed
  difference, a baseline of 0 or a target above 100%) returns ``None``, which the rules
  render as "not estimable" with their reason. No result is ever NaN or infinite.
* Invalid input is a programming or configuration error and raises: ``TypeError`` for a
  wrong type (a ``float`` or ``bool`` where an ``int`` count or ``Decimal`` is required) and
  ``ValueError`` for a value out of range (a negative count, more successes than trials, a
  probability outside [0, 1], a non-finite ``Decimal``).
* Pure and thread-safe: no I/O, no logging, no module state. Every decimal operation uses a
  fresh context and every power analysis a fresh ``NormalIndPower``, whose solver keeps
  per-instance state.

Example:
    >>> from decimal import Decimal
    >>> test = compare_proportions(1, 20, 4, 20)
    >>> test.method, test.p_value, test.expected_min
    ('fisher_exact', Decimal('0.341649'), Decimal('2.500000'))
    >>> holm([Decimal("0.01"), Decimal("0.04"), Decimal("0.03")])
    [Decimal('0.030000'), Decimal('0.060000'), Decimal('0.060000')]
    >>> required_n(Decimal("0.02"), Decimal("0.024"), alpha=Decimal("0.05"), power=Decimal("0.8"))
    21067
    >>> percentile([Decimal(10), Decimal(20), Decimal(30), Decimal(40)], Decimal("0.9"))
    Decimal('37.000000')
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from typing import Final, Literal, SupportsFloat

from scipy.stats import fisher_exact
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import (
    proportion_confint,
    proportion_effectsize,
    proportions_ztest,
)

# Aliased so the module namespace holds no ``test_*`` name for a test collector to pick up.
from statsmodels.stats.rates import test_poisson_2indep as _poisson_2indep

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_CONFIDENCE",
    "FISHER_MIN_EXPECTED",
    "POISSON_RATE_METHOD",
    "Interval",
    "ProportionMethod",
    "ProportionTest",
    "RateTest",
    "additional_n_for_difference",
    "cohens_h",
    "compare_proportions",
    "holm",
    "percentile",
    "poisson_rate_test",
    "required_n",
    "wilson",
]

type ProportionMethod = Literal["fisher_exact", "two_proportion_z"]
"""The CR1 test chosen for a pair: Fisher's exact test or the pooled two-proportion z-test."""

DEFAULT_ALPHA: Final = Decimal("0.05")
"""Default family-wise significance level of :func:`holm` (``cr.alpha``)."""

DEFAULT_CONFIDENCE: Final = Decimal("0.95")
"""Default confidence level of :func:`wilson` (``cr.ci_level``)."""

FISHER_MIN_EXPECTED: Final = 5
"""Default ``cr.fisher_min_expected``: Fisher's exact test when any expected cell is below it."""

POISSON_RATE_METHOD: Final = "two_sample_poisson_score"
"""Method label of :func:`poisson_rate_test`: the score test of a rate ratio of 1."""

# Every Decimal leaving this module carries 6 decimal places, the scale of the stored
# numeric(18,6) money and conversion columns.
_QUANTUM: Final = Decimal("0.000001")
# Working precision of the exact bookkeeping. 50 digits hold any count ratio or spend sum the
# product stores (numeric(18,6) has at most 18 digits) with room for the 6-place quantum.
_PRECISION: Final = 50
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
# Clamp bounds at the output scale, so a clamped value prints like every other output.
_ZERO_6: Final = Decimal("0.000000")
_ONE_6: Final = Decimal("1.000000")
_TWO_SIDED: Final = "two-sided"
# The power solver brackets the sample size from 2 upwards, so samples of 1 and 2 per group
# are checked directly with the power function before the solver runs (see _per_group_n).
_SMALLEST_SAMPLES: Final = (1, 2)


@dataclass(frozen=True, slots=True)
class ProportionTest:
    """Result of a two-proportion comparison (CR1).

    Attributes:
        method: ``"fisher_exact"`` when any expected cell is below the Fisher threshold,
            otherwise ``"two_proportion_z"``.
        p_value: Two-sided p-value in [0, 1], unadjusted; pass a campaign's p-values
            through :func:`holm` before calling a pair distinguishable.
        rate_a: Observed rate ``x_a / n_a``.
        rate_b: Observed rate ``x_b / n_b``.
        expected_min: Smallest expected cell count of the 2x2 table under independence,
            rounded down so the shown value never crosses the threshold that chose ``method``.
    """

    method: ProportionMethod
    p_value: Decimal
    rate_a: Decimal
    rate_b: Decimal
    expected_min: Decimal


@dataclass(frozen=True, slots=True)
class Interval:
    """A rate with its confidence interval (CR2).

    Attributes:
        point: Observed rate ``successes / trials``.
        low: Lower bound, never below 0 or above ``point``.
        high: Upper bound, never above 1 or below ``point``.
    """

    point: Decimal
    low: Decimal
    high: Decimal


@dataclass(frozen=True, slots=True)
class RateTest:
    """Result of a two-sample Poisson rate comparison (B4).

    Attributes:
        method: Always :data:`POISSON_RATE_METHOD`.
        p_value: Two-sided p-value in [0, 1] for a rate ratio of 1.
        rate_a: Outcomes per unit of exposure on side A (``count_a / exposure_a``).
        rate_b: Outcomes per unit of exposure on side B (``count_b / exposure_b``).
    """

    method: str
    p_value: Decimal
    rate_a: Decimal
    rate_b: Decimal


# --- Validation and conversion helpers ------------------------------------------------------
# The helpers take ``object`` so their runtime type checks stay reachable under mypy's
# warn_unreachable: callers are typed, but the checks guard untyped and test callers too.


def _context() -> Context:
    """Return a fresh decimal context, so no caller's context or another thread interferes."""
    return Context(prec=_PRECISION, rounding=ROUND_HALF_EVEN)


def _quantize(value: Decimal, *, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """Round ``value`` to 6 decimal places; a negative zero becomes a plain zero."""
    result = value.quantize(_QUANTUM, rounding=rounding, context=_context())
    return result.copy_abs() if result.is_zero() else result


def _require_count(name: str, value: object) -> int:
    """Return ``value`` as a non-negative ``int`` count, rejecting ``bool`` and ``float``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int count, got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value}")
    return value


def _require_successes(
    successes_name: str, successes: object, trials_name: str, trials: object
) -> tuple[int, int]:
    """Validate a success count against its trial count.

    Zero trials define no rate, so callers return "not estimable" for them before any
    comparison; the success count is checked against the trials only when trials exist.
    """
    x = _require_count(successes_name, successes)
    n = _require_count(trials_name, trials)
    if n > 0 and x > n:
        raise ValueError(f"{successes_name} ({x}) must not exceed {trials_name} ({n})")
    return x, n


def _require_decimal(name: str, value: object) -> Decimal:
    """Return ``value`` as a finite ``Decimal``; ``int`` is accepted, ``float`` is not."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise TypeError(f"{name} must be a Decimal, got {type(value).__name__}")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{name} must be finite, got {result}")
    return result


def _require_probability(name: str, value: object) -> Decimal:
    """Return ``value`` as a ``Decimal`` in [0, 1]."""
    result = _require_decimal(name, value)
    if not _ZERO <= result <= _ONE:
        raise ValueError(f"{name} must be in [0, 1], got {result}")
    return result


def _require_level(name: str, value: object) -> Decimal:
    """Return a significance, power or confidence level as a ``Decimal`` in (0, 1)."""
    result = _require_decimal(name, value)
    if not _ZERO < result < _ONE:
        raise ValueError(f"{name} must be in (0, 1), got {result}")
    return result


def _require_power_levels(alpha: object, power: object) -> tuple[float, float]:
    """Validate ``alpha`` and ``power`` for a sample-size estimate and return them as floats.

    A two-sided test attains power ``alpha`` with no data at all, so a target power at or
    below ``alpha`` has no sample size and is a configuration error.
    """
    level = _require_level("alpha", alpha)
    target = _require_level("power", power)
    if target <= level:
        raise ValueError(f"power ({target}) must exceed alpha ({level})")
    return float(level), float(target)


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    """Return ``numerator / denominator`` (denominator non-zero) at 6 decimal places."""
    return _quantize(_context().divide(Decimal(numerator), Decimal(denominator)))


def _from_float(value: SupportsFloat) -> Decimal | None:
    """Convert a library float to a 6-place ``Decimal``; ``None`` when NaN or infinite.

    ``float()`` comes first because ``repr`` of a NumPy scalar is ``"np.float64(...)"``.
    """
    number = float(value)
    if not math.isfinite(number):
        return None
    return _quantize(Decimal(repr(number)))


def _probability_from_float(value: SupportsFloat) -> Decimal | None:
    """Convert a library probability, clamped to [0, 1]; ``None`` when not finite."""
    result = _from_float(value)
    if result is None:
        return None
    return min(max(result, _ZERO_6), _ONE_6)


def _require_finite(value: Decimal | None, what: str) -> Decimal:
    """Return a library result that validated input guarantees to be finite.

    Raises:
        ArithmeticError: The library returned NaN or infinity for validated input, which
            would otherwise surface as a silently missing statistic.
    """
    if value is None:
        raise ArithmeticError(f"{what} returned a non-finite value for valid input")
    return value


def _power_at(analysis: NormalIndPower, effect: float, nobs: int, alpha: float) -> float:
    """Power of the two-sided two-sample normal test at ``nobs`` per group."""
    return float(
        analysis.power(
            effect_size=effect, nobs1=nobs, alpha=alpha, ratio=1.0, alternative=_TWO_SIDED
        )
    )


def _per_group_n(rate_1: float, rate_2: float, *, alpha: float, power: float) -> int | None:
    """Smallest whole sample per group that detects ``rate_1`` vs ``rate_2``.

    Cohen's h (``proportion_effectsize``) feeds ``NormalIndPower``, the two-sided two-sample
    normal test with equal groups (``ratio=1``). The solver brackets the sample size from 2
    upwards and fails (NaN plus a ConvergenceWarning) when fewer than 2 observations per
    group suffice, which happens for very large effects (h above about 2.8 at alpha 0.05 and
    power 0.8). The power function settles those cases directly: 1 when one observation per
    group attains ``power``, otherwise 2 when two do. Larger samples come from the solver,
    rounded up.

    Returns:
        The per-group sample size, or ``None`` when the rates give no effect in floating
        point or the solver cannot reach a finite answer (effects so small that the sample
        exceeds anything a campaign delivers).
    """
    effect = abs(float(proportion_effectsize(rate_2, rate_1)))
    if not math.isfinite(effect) or effect == 0.0:
        return None
    analysis = NormalIndPower()
    if _power_at(analysis, effect, _SMALLEST_SAMPLES[1], alpha) >= power:
        smallest, second = _SMALLEST_SAMPLES
        return smallest if _power_at(analysis, effect, smallest, alpha) >= power else second
    solved = float(
        analysis.solve_power(
            effect_size=effect,
            nobs1=None,
            alpha=alpha,
            power=power,
            ratio=1.0,
            alternative=_TWO_SIDED,
        )
    )
    if not math.isfinite(solved) or solved <= 0.0:
        return None
    return math.ceil(solved)


# --- CR1: pairwise comparison, Holm correction and the sample still needed -----------------


def compare_proportions(
    x_a: int,
    n_a: int,
    x_b: int,
    n_b: int,
    *,
    fisher_min_expected: Decimal | int = FISHER_MIN_EXPECTED,
) -> ProportionTest | None:
    """Compare two observed proportions with a two-sided test (CR1).

    CR1 compares the end-to-end rate of two creative variants (leads / impressions, or
    clicks / impressions for non-lead-gen campaigns). The 2x2 table is
    ``[[x_a, n_a - x_a], [x_b, n_b - x_b]]`` and its expected cells under independence are
    ``row total * column total / grand total``, computed exactly:

    * any expected cell below ``fisher_min_expected`` (``cr.fisher_min_expected``, 5): Fisher's
      exact test, ``scipy.stats.fisher_exact(table, alternative="two-sided")``;
    * otherwise: the pooled two-proportion z-test, statsmodels' ``proportions_ztest``.
      Identical rates give p = 1 without calling the test.

    The p-value is unadjusted. CR1 calls a pair distinguishable only after :func:`holm`
    across the campaign's pairs.

    Args:
        x_a: Successes of variant A (leads or clicks).
        n_a: Trials of variant A (impressions).
        x_b: Successes of variant B.
        n_b: Trials of variant B.
        fisher_min_expected: Expected-cell count below which Fisher's exact test is used.

    Returns:
        The test result, or ``None`` ("not estimable") when either variant has zero trials.

    Raises:
        TypeError: A count is not an ``int``, or the threshold is not a ``Decimal``/``int``.
        ValueError: A count is negative, successes exceed non-zero trials, or the threshold is
            negative or not finite.

    Example:
        >>> compare_proportions(60, 1000, 90, 1000).method
        'two_proportion_z'
    """
    successes_a, trials_a = _require_successes("x_a", x_a, "n_a", n_a)
    successes_b, trials_b = _require_successes("x_b", x_b, "n_b", n_b)
    threshold = _require_decimal("fisher_min_expected", fisher_min_expected)
    if threshold < _ZERO:
        raise ValueError(f"fisher_min_expected must be >= 0, got {threshold}")
    if trials_a == 0 or trials_b == 0:
        return None

    total = trials_a + trials_b
    successes = successes_a + successes_b
    # The smallest expected cell pairs the smaller row total with the smaller column total.
    expected_min = Fraction(min(trials_a, trials_b) * min(successes, total - successes), total)

    method: ProportionMethod
    raw_p: SupportsFloat
    if expected_min < Fraction(threshold):
        method = "fisher_exact"
        table = [[successes_a, trials_a - successes_a], [successes_b, trials_b - successes_b]]
        raw_p = fisher_exact(table, alternative=_TWO_SIDED).pvalue
    else:
        method = "two_proportion_z"
        if successes_a * trials_b == successes_b * trials_a:
            raw_p = 1.0
        else:
            _, raw_p = proportions_ztest(
                [successes_a, successes_b], [trials_a, trials_b], alternative=_TWO_SIDED
            )

    p_value = _probability_from_float(raw_p)
    if p_value is None:
        return None
    shown_expected = _context().divide(
        Decimal(expected_min.numerator), Decimal(expected_min.denominator)
    )
    return ProportionTest(
        method=method,
        p_value=p_value,
        rate_a=_ratio(successes_a, trials_a),
        rate_b=_ratio(successes_b, trials_b),
        expected_min=_quantize(shown_expected, rounding=ROUND_FLOOR),
    )


def holm(p_values: Sequence[Decimal], alpha: Decimal = DEFAULT_ALPHA) -> list[Decimal]:
    """Holm step-down adjusted p-values, in input order (CR1).

    CR1 adjusts every pairwise p-value of one campaign together and calls a pair
    distinguishable only when its adjusted p-value is below ``cr.alpha``. Holm controls the
    family-wise error rate without assuming independence between the pairs. The adjustment
    is ``statsmodels.stats.multitest.multipletests(method="holm")``'s ``pvals_corrected``;
    ``alpha`` only sets the library's reject flags, which this function does not return, so
    the adjusted values do not depend on it.

    Args:
        p_values: Unadjusted p-values, each in [0, 1].
        alpha: Family-wise significance level in (0, 1).

    Returns:
        The adjusted p-values, capped at 1, in the order given; ``[]`` for no input.

    Raises:
        TypeError: A p-value or ``alpha`` is not a ``Decimal``.
        ValueError: A p-value lies outside [0, 1] or ``alpha`` outside (0, 1).
        ArithmeticError: The library returned a non-finite adjusted value.

    Example:
        >>> holm([Decimal("0.04"), Decimal("0.03")])  # both below 0.05 before adjustment
        [Decimal('0.060000'), Decimal('0.060000')]
    """
    level = _require_level("alpha", alpha)
    checked = [_require_probability(f"p_values[{i}]", p) for i, p in enumerate(p_values)]
    if not checked:
        return []
    _, corrected, _, _ = multipletests(
        [float(p) for p in checked], alpha=float(level), method="holm"
    )
    return [
        _require_finite(_probability_from_float(value), "multipletests(method='holm')")
        for value in corrected
    ]


def additional_n_for_difference(
    x_a: int,
    n_a: int,
    x_b: int,
    n_b: int,
    *,
    alpha: Decimal,
    power: Decimal,
) -> int | None:
    """Extra trials per variant needed to distinguish the observed rates (CR1).

    Feeds the CR1 label "about {n} more impressions per variant needed at the observed
    difference": the per-group sample that detects the observed lower rate against the
    observed higher rate (Cohen's h and ``NormalIndPower``, two-sided, as in
    :func:`required_n`) minus the larger of the two samples already observed, floored at 0.

    Unlike :func:`required_n`, a lower rate of 0 is estimable here: a variant with
    impressions and no leads still has an observed rate, and CR1 calls the figure not
    estimable only for identical rates or a variant without impressions.

    Args:
        x_a: Successes of variant A.
        n_a: Trials of variant A.
        x_b: Successes of variant B.
        n_b: Trials of variant B.
        alpha: Two-sided significance level in (0, 1) (``cr.alpha``).
        power: Target power in (alpha, 1) (``cr.power``).

    Returns:
        The additional trials per variant (0 when the samples already suffice), or ``None``
        ("not estimable") when either variant has zero trials, the observed rates are
        identical ("no observed difference"), or the difference is too small for the power
        solver to size.

    Raises:
        TypeError: A count is not an ``int``, or a level is not a ``Decimal``.
        ValueError: Invalid counts, levels outside (0, 1), or power not above alpha.
    """
    successes_a, trials_a = _require_successes("x_a", x_a, "n_a", n_a)
    successes_b, trials_b = _require_successes("x_b", x_b, "n_b", n_b)
    level, target_power = _require_power_levels(alpha, power)
    if trials_a == 0 or trials_b == 0:
        return None
    if successes_a * trials_b == successes_b * trials_a:
        return None
    # The power analysis runs on floats; int / int is correctly rounded true division.
    low, high = sorted((successes_a / trials_a, successes_b / trials_b))
    needed = _per_group_n(low, high, alpha=level, power=target_power)
    if needed is None:
        return None
    return max(0, needed - max(trials_a, trials_b))


# --- CR2: stage-rate uncertainty --------------------------------------------------------------


def wilson(
    successes: int, trials: int, *, confidence: Decimal = DEFAULT_CONFIDENCE
) -> Interval | None:
    """Wilson score interval of an observed rate (CR2).

    CR2 calls a funnel stage weak when the upper bound of its interval lies below the
    account baseline. The Wilson score interval (statsmodels' ``proportion_confint`` with
    ``method="wilson"``) stays valid at small counts and at rates near 0 or 1, where the
    normal (Wald) interval collapses to a point or leaves [0, 1].

    Args:
        successes: Stage successes (clicks, form opens or leads).
        trials: The stage's own denominator (impressions, or form opens for completion).
        confidence: Confidence level in (0, 1) (``cr.ci_level``).

    Returns:
        The rate with its bounds, ``low <= point <= high`` within [0, 1], or ``None``
        ("not estimable") when ``trials`` is 0.

    Raises:
        TypeError: A count is not an ``int``, or ``confidence`` is not a ``Decimal``.
        ValueError: A negative count, successes above non-zero trials, or a level outside (0, 1).
        ArithmeticError: The library returned a non-finite bound.

    Example:
        >>> wilson(0, 50)
        Interval(point=Decimal('0.000000'), low=Decimal('0.000000'), high=Decimal('0.071348'))
    """
    x, n = _require_successes("successes", successes, "trials", trials)
    level = _require_level("confidence", confidence)
    if n == 0:
        return None
    low_raw, high_raw = proportion_confint(
        x, n, alpha=float(_context().subtract(_ONE, level)), method="wilson"
    )
    low = _require_finite(_probability_from_float(low_raw), "proportion_confint")
    high = _require_finite(_probability_from_float(high_raw), "proportion_confint")
    point = _ratio(x, n)
    # Rounding is monotone, so this only absorbs floating-point noise at the bounds.
    return Interval(point=point, low=min(low, point), high=max(high, point))


# --- CR3: effect size and the sample a test needs ---------------------------------------------


def cohens_h(p1: Decimal, p2: Decimal) -> Decimal:
    """Cohen's h effect size between two proportions (CR1, CR3).

    ``h = 2 * asin(sqrt(p1)) - 2 * asin(sqrt(p2))`` via statsmodels'
    ``proportion_effectsize(p1, p2)``. The arcsine transform makes equal values of h
    equally detectable at any base rate, which is why the sample-size estimates use it.

    Args:
        p1: First proportion, in [0, 1].
        p2: Second proportion, in [0, 1].

    Returns:
        The signed effect size in [-pi, pi]: positive when ``p1 > p2``.

    Raises:
        TypeError: A proportion is not a ``Decimal``.
        ValueError: A proportion lies outside [0, 1].
        ArithmeticError: The library returned a non-finite value.

    Example:
        >>> cohens_h(Decimal("0.024"), Decimal("0.02"))
        Decimal('0.027297')
    """
    first = _require_probability("p1", p1)
    second = _require_probability("p2", p2)
    effect = proportion_effectsize(float(first), float(second))
    return _require_finite(_from_float(effect), "proportion_effectsize")


def required_n(p_base: Decimal, p_target: Decimal, *, alpha: Decimal, power: Decimal) -> int | None:
    """Per-variant sample needed to detect ``p_base -> p_target`` (CR3).

    CR3 sizes the next two-variant test to detect a relative lift (``cr.target_lift``,
    20%) of the tested stage's baseline rate: ``p_target = p_base * (1 + lift)``. The
    estimate is Cohen's h (:func:`cohens_h`) solved for the per-group sample of the
    two-sided two-sample normal test with equal groups (statsmodels' ``NormalIndPower``,
    ``ratio=1``), rounded up to a whole number.

    The result counts the tested stage's own denominator: impressions for click and open
    rate, form opens for completion rate. The caller names the unit and, for completion
    rate, projects it to impressions through the observed open rate.

    Args:
        p_base: Baseline rate of the tested stage.
        p_target: Rate the test must detect.
        alpha: Two-sided significance level in (0, 1) (``cr.alpha``).
        power: Target power in (alpha, 1) (``cr.power``).

    Returns:
        Trials per variant, or ``None`` ("not estimable") when the baseline rate is 0 or
        below, the target is 0 or below or above 100% (baseline x 1.2 > 1), the baseline
        is not a proportion (above 1), the target equals the baseline, or the difference is
        too small for the power solver to size.

    Raises:
        TypeError: A rate or level is not a ``Decimal``.
        ValueError: A rate is not finite, a level lies outside (0, 1), or power is not
            above alpha.

    Example:
        >>> required_n(Decimal("0.9"), Decimal("1.08"), alpha=Decimal("0.05"), power=Decimal("0.8"))
        >>> required_n(Decimal("0.1"), Decimal("0.12"), alpha=Decimal("0.05"), power=Decimal("0.8"))
        3835
    """
    base = _require_decimal("p_base", p_base)
    target = _require_decimal("p_target", p_target)
    level, target_power = _require_power_levels(alpha, power)
    if not _ZERO < base <= _ONE or not _ZERO < target <= _ONE or base == target:
        return None
    return _per_group_n(float(base), float(target), alpha=level, power=target_power)


# --- B4: weekday / weekend rate test ----------------------------------------------------------


def poisson_rate_test(
    count_a: int, exposure_a: Decimal, count_b: int, exposure_b: Decimal
) -> RateTest | None:
    """Two-sample Poisson rate test of outcomes per unit of spend (B4).

    B4 compares weekend with weekday efficiency: ``count_*`` are outcomes and
    ``exposure_*`` the spend on each side. The test is statsmodels'
    ``test_poisson_2indep(count_a, exposure_a, count_b, exposure_b, method="score")`` with
    ``compare="ratio"``: the two-sided score test of a rate ratio of 1, which uses the
    variance under the null and behaves well at the small counts B4 admits (from 10
    outcomes in total).

    Valid only for unweighted event counts. A Poisson count is a whole number of events;
    fractional or data-driven conversions violate the count assumption even when their
    daily totals happen to be whole. Counts must therefore be ``int``: callers pass LinkedIn
    leads or website conversions, or Google conversions only under a verified last-click
    counting contract, and must never round weighted conversions into this test (B4
    withholds the pattern instead). A ``float`` or ``Decimal`` count raises ``TypeError``.

    Args:
        count_a: Outcomes on side A (for example weekend days), an unweighted event count.
        exposure_a: Spend on side A.
        count_b: Outcomes on side B (for example weekdays), an unweighted event count.
        exposure_b: Spend on side B.

    Returns:
        The test result, or ``None`` ("no exposure" / not estimable) when either side's
        exposure is 0 or below, or when neither side has an outcome, so no rate exists to
        compare.

    Raises:
        TypeError: A count is not an ``int`` or an exposure is not a ``Decimal``.
        ValueError: A count is negative or an exposure is not finite.

    Example:
        >>> poisson_rate_test(30, Decimal(1000), 10, Decimal(1000)).p_value
        Decimal('0.001565')
    """
    events_a = _require_count("count_a", count_a)
    events_b = _require_count("count_b", count_b)
    spend_a = _require_decimal("exposure_a", exposure_a)
    spend_b = _require_decimal("exposure_b", exposure_b)
    if spend_a <= _ZERO or spend_b <= _ZERO:
        return None
    if events_a + events_b == 0:
        return None
    result = _poisson_2indep(
        events_a,
        float(spend_a),
        events_b,
        float(spend_b),
        value=1.0,
        method="score",
        compare="ratio",
        alternative=_TWO_SIDED,
    )
    p_value = _probability_from_float(result.pvalue)
    if p_value is None:
        return None
    return RateTest(
        method=POISSON_RATE_METHOD,
        p_value=p_value,
        rate_a=_ratio(events_a, spend_a),
        rate_b=_ratio(events_b, spend_b),
    )


# --- B3: spend percentile ---------------------------------------------------------------------


def percentile(values: Sequence[Decimal], q: Decimal) -> Decimal | None:
    """Linear-interpolation percentile of ``values``, exact in ``Decimal`` (B3).

    B3 proposes a lower daily budget of the 90th percentile of daily spend over the
    known-budget active days, times ``b3.headroom``. The method is NumPy's default
    ``"linear"`` (Hyndman and Fan type 7): with the values sorted ascending and the virtual
    index ``i = (len(values) - 1) * q``, the result is
    ``v[floor(i)] + (i - floor(i)) * (v[floor(i) + 1] - v[floor(i)])``. It is computed in
    ``Decimal`` so money keeps its exact value up to the 6-place output scale.

    Args:
        values: The observations, in any order.
        q: The percentile as a fraction in [0, 1]: 0.9 for the 90th percentile. A percent
            such as ``b3.spend_percentile: 90`` is divided by 100 first.

    Returns:
        The percentile rounded to 6 decimal places, or ``None`` for no values.

    Raises:
        TypeError: A value or ``q`` is not a ``Decimal``.
        ValueError: A value is not finite, or ``q`` lies outside [0, 1].

    Example:
        >>> percentile([Decimal(40), Decimal(10), Decimal(30), Decimal(20)], Decimal("0.5"))
        Decimal('25.000000')
    """
    fraction = _require_decimal("q", q)
    if not _ZERO <= fraction <= _ONE:
        raise ValueError(
            f"q must be a fraction in [0, 1] (0.9 for the 90th percentile), got {fraction}"
        )
    ordered = sorted(_require_decimal(f"values[{i}]", value) for i, value in enumerate(values))
    if not ordered:
        return None
    context = _context()
    position = context.multiply(Decimal(len(ordered) - 1), fraction)
    lower = int(position)  # position >= 0, so truncation is the floor
    upper = min(lower + 1, len(ordered) - 1)
    weight = context.subtract(position, Decimal(lower))
    step = context.multiply(weight, context.subtract(ordered[upper], ordered[lower]))
    return _quantize(context.add(ordered[lower], step))
