"""Unit tests for ``ads_engine.analysis.stats``, the statistics behind CR1-CR3, B3 and B4.

They pin each wrapper to the library method it names, with every reference value computed here
from SciPy or statsmodels (the Holm and Wilson boundary values are also checked by hand):

* CR1: Fisher's exact test when any expected cell is below ``fisher_min_expected`` (5), the
  pooled two-proportion z-test otherwise, the Holm step-down correction, and the extra sample
  a pair needs at its observed difference;
* CR2: Wilson score intervals; CR3: Cohen's h and the per-variant sample of a two-variant test,
  including samples of 1 and 2 per group, which the library solver cannot size, and tiny
  levels and effects, where it fails;
* B4: the two-sample Poisson score test on unweighted counts; B3: the linear-interpolation
  percentile of daily spend, exact in ``Decimal``.

Degenerate input returns ``None`` ("not estimable"), invalid input raises, and no result
depends on the caller's decimal context. Everything here is pure and synchronous: no database
and no network.
"""

import ast
import doctest
import inspect
import math
import sys
import warnings
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, localcontext
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, SupportsFloat

import numpy as np
import pytest
from scipy.stats import fisher_exact, norm
from scipy.stats.contingency import expected_freq
from statsmodels.stats.multitest import multipletests
from statsmodels.stats.power import NormalIndPower
from statsmodels.stats.proportion import (
    proportion_confint,
    proportion_effectsize,
    proportions_ztest,
)
from statsmodels.stats.rates import test_poisson_2indep as poisson_2indep_reference
from statsmodels.tools.rootfinding import brentq_expanding
from statsmodels.tools.sm_exceptions import ConvergenceWarning

from ads_engine.analysis import stats
from ads_engine.analysis.stats import (
    DEFAULT_ALPHA,
    DEFAULT_CONFIDENCE,
    FISHER_MIN_EXPECTED,
    POISSON_RATE_METHOD,
    Interval,
    ProportionTest,
    RateTest,
    additional_n_for_difference,
    cohens_h,
    compare_proportions,
    holm,
    percentile,
    poisson_rate_test,
    required_n,
    wilson,
)

QUANTUM = Decimal("0.000001")
ZERO = Decimal("0.000000")
ONE = Decimal("1.000000")
ALPHA = Decimal("0.05")
POWER = Decimal("0.8")
HALF = Decimal("0.5")
TWO_SIDED = "two-sided"

ZERO_RATE_RATIO_WARNING = pytest.mark.filterwarnings(
    "ignore:divide by zero encountered in scalar divide:RuntimeWarning:statsmodels"
)

# Rates that differ exactly (1/3 and 10**20 / (3 * 10**20 - 1)) but are equal as floats, so
# the power analysis sees no effect at all.
FLOAT_EQUAL_RATES = (1, 3, 10**20, 3 * 10**20 - 1)


# --- reference helpers ----------------------------------------------------------------------


def six(value: SupportsFloat) -> Decimal:
    """Reference conversion of a library float: its repr, rounded half-even to 6 places."""
    return Decimal(repr(float(value))).quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def probability(value: SupportsFloat) -> Decimal:
    """Reference conversion of a library probability, clamped to [0, 1]."""
    return min(max(six(value), ZERO), ONE)


def exact_ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    """Reference ``numerator / denominator`` at 6 places, at a precision no ratio here exceeds."""
    with localcontext(Context(prec=50)):
        quotient = Decimal(numerator) / Decimal(denominator)
        return quotient.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def floor_six(value: Fraction) -> Decimal:
    """``value`` rounded down to 6 places, exactly."""
    return Decimal(math.floor(value * 1_000_000)) / Decimal(1_000_000)


def table_of(x_a: int, n_a: int, x_b: int, n_b: int) -> list[list[int]]:
    """The CR1 2x2 table: successes and failures of each variant."""
    return [[x_a, n_a - x_a], [x_b, n_b - x_b]]


def expected_min_reference(table: list[list[int]]) -> Fraction:
    """Smallest expected cell under independence, row total * column total / grand total."""
    rows = [sum(row) for row in table]
    columns = [sum(column) for column in zip(*table, strict=True)]
    total = sum(rows)
    return min(Fraction(row * column, total) for row in rows for column in columns)


def fisher_reference(x_a: int, n_a: int, x_b: int, n_b: int) -> Decimal:
    """SciPy's two-sided Fisher exact p-value of the CR1 table."""
    return probability(fisher_exact(table_of(x_a, n_a, x_b, n_b), alternative=TWO_SIDED).pvalue)


def z_reference(x_a: int, n_a: int, x_b: int, n_b: int) -> Decimal:
    """statsmodels' two-sided pooled two-proportion z-test p-value."""
    _, p_value = proportions_ztest([x_a, x_b], [n_a, n_b], alternative=TWO_SIDED)
    return probability(p_value)


def solver_n(rate_1: float, rate_2: float, *, alpha: float = 0.05, power: float = 0.8) -> int:
    """statsmodels' per-group sample for Cohen's h, two-sided, equal groups, rounded up."""
    effect = abs(proportion_effectsize(rate_2, rate_1))
    solved = NormalIndPower().solve_power(
        effect_size=effect, nobs1=None, alpha=alpha, power=power, ratio=1.0, alternative=TWO_SIDED
    )
    return math.ceil(float(solved))


def power_at(
    rate_1: float, rate_2: float, nobs: int, *, alpha: float = 0.05, ratio: float = 1.0
) -> float:
    """statsmodels' power of the two-sided two-sample normal test, ``nobs`` and ``ratio * nobs``."""
    effect = abs(proportion_effectsize(rate_2, rate_1))
    return float(
        NormalIndPower().power(
            effect_size=effect, nobs1=nobs, alpha=alpha, ratio=ratio, alternative=TWO_SIDED
        )
    )


def common_increment_power(x_a: int, n_a: int, x_b: int, n_b: int, extra: int) -> float:
    """Power at the observed rates once each variant has ``extra`` more trials (unequal groups)."""
    low, high = sorted((x_a / n_a, x_b / n_b))
    smaller, larger = sorted((n_a, n_b))
    grown = smaller + extra
    return power_at(low, high, grown, ratio=(larger + extra) / grown)


def library_solver_fails(rate_1: float, rate_2: float, *, alpha: float) -> bool:
    """Whether statsmodels' ``solve_power`` returns NaN with a ConvergenceWarning (power 0.8)."""
    effect = abs(proportion_effectsize(rate_2, rate_1))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        solved = NormalIndPower().solve_power(
            effect_size=effect, nobs1=None, alpha=alpha, power=0.8, ratio=1.0, alternative=TWO_SIDED
        )
    warned = any(issubclass(w.category, ConvergenceWarning) for w in caught)
    return math.isnan(float(solved)) and warned


def poisson_reference(count_a: int, spend_a: Decimal, count_b: int, spend_b: Decimal) -> Decimal:
    """statsmodels' two-sided score test of a rate ratio of 1."""
    result = poisson_2indep_reference(
        count_a,
        float(spend_a),
        count_b,
        float(spend_b),
        value=1.0,
        method="score",
        compare="ratio",
        alternative=TWO_SIDED,
    )
    return probability(result.pvalue)


def type7_reference(values: Sequence[Decimal | int], q: Decimal) -> Decimal:
    """Hyndman and Fan type 7 (NumPy ``"linear"``) percentile, exact, then 6 places."""
    ordered = sorted(Fraction(value) for value in values)
    position = (len(ordered) - 1) * Fraction(q)
    lower = math.floor(position)
    upper = min(lower + 1, len(ordered) - 1)
    exact = ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])
    return exact_ratio(Decimal(exact.numerator), Decimal(exact.denominator))


# --- compare_proportions: Fisher or z-test (CR1) ------------------------------------------------


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b", "method"),
    [
        (1, 20, 4, 20, "fisher_exact"),  # expected 2.5
        (4, 10, 5, 10, "fisher_exact"),  # expected 4.5
        (5, 999, 5, 1000, "fisher_exact"),  # expected 9990/1999, just below 5
        (48, 50, 50, 50, "fisher_exact"),  # the failure column is the small one
        (4, 10, 6, 10, "two_proportion_z"),  # expected exactly 5: not below the threshold
        (10, 10, 0, 10, "two_proportion_z"),  # expected exactly 5, rates 1 and 0
        (0, 1000, 12, 1000, "two_proportion_z"),  # expected 6, one variant without successes
        (3, 1500, 9, 1200, "two_proportion_z"),  # expected 1200 * 12 / 2700
        (60, 1000, 90, 1000, "two_proportion_z"),  # expected 75
    ],
)
def test_compare_proportions_chooses_fisher_below_five_expected_and_z_otherwise(
    x_a: int, n_a: int, x_b: int, n_b: int, method: str
) -> None:
    result = compare_proportions(x_a, n_a, x_b, n_b)

    expected = expected_min_reference(table_of(x_a, n_a, x_b, n_b))
    assert result is not None
    assert result.method == method
    assert (expected < FISHER_MIN_EXPECTED) == (method == "fisher_exact")
    reference = fisher_reference if method == "fisher_exact" else z_reference
    assert result.p_value == reference(x_a, n_a, x_b, n_b)
    assert result.rate_a == exact_ratio(x_a, n_a)
    assert result.rate_b == exact_ratio(x_b, n_b)
    assert result.expected_min == floor_six(expected)
    assert math.isclose(
        float(expected), float(expected_freq(np.array(table_of(x_a, n_a, x_b, n_b))).min())
    )


def test_compare_proportions_docstring_case() -> None:
    assert compare_proportions(1, 20, 4, 20) == ProportionTest(
        method="fisher_exact",
        p_value=fisher_reference(1, 20, 4, 20),
        rate_a=Decimal("0.050000"),
        rate_b=Decimal("0.200000"),
        expected_min=Decimal("2.500000"),
    )


def test_expected_min_is_rounded_down_so_it_never_shows_the_threshold() -> None:
    result = compare_proportions(5, 999, 5, 1000)

    # 9990 / 1999 = 4.99749874...: half-up rounding at 6 places would still be below 5, but
    # rounding down is what guarantees the shown value never crosses the threshold.
    assert result is not None
    assert result.method == "fisher_exact"
    assert result.expected_min == Decimal("4.997498")
    assert result.expected_min < FISHER_MIN_EXPECTED


@pytest.mark.parametrize(
    ("threshold", "method"),
    [
        (100, "fisher_exact"),  # expected 75 < 100
        (Decimal("75.000001"), "fisher_exact"),
        (Decimal(75), "two_proportion_z"),  # 75 is not below 75
        (Decimal("74.5"), "two_proportion_z"),
        (0, "two_proportion_z"),
    ],
)
def test_fisher_min_expected_is_configurable(threshold: Decimal | int, method: str) -> None:
    result = compare_proportions(60, 1000, 90, 1000, fisher_min_expected=threshold)

    assert result is not None
    assert result.method == method
    reference = fisher_reference if method == "fisher_exact" else z_reference
    assert result.p_value == reference(60, 1000, 90, 1000)
    assert result.expected_min == Decimal("75.000000")


def test_default_fisher_threshold_is_five() -> None:
    assert FISHER_MIN_EXPECTED == 5
    for case in [(4, 10, 5, 10), (4, 10, 6, 10), (60, 1000, 90, 1000)]:
        assert compare_proportions(*case) == compare_proportions(*case, fisher_min_expected=5)


def test_identical_rates_under_the_z_test_give_p_one_without_calling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("proportions_ztest must not run for identical rates")

    monkeypatch.setattr(stats, "proportions_ztest", forbidden)

    result = compare_proportions(50, 1000, 100, 2000)
    zero_successes = compare_proportions(0, 10, 0, 10, fisher_min_expected=0)

    assert result is not None
    assert result.method == "two_proportion_z"
    assert result.p_value == ONE
    assert result.rate_a == result.rate_b == Decimal("0.050000")
    assert zero_successes is not None
    assert zero_successes.method == "two_proportion_z"
    assert zero_successes.p_value == ONE
    assert zero_successes.expected_min == ZERO


def test_identical_rates_with_a_small_expected_cell_use_fisher() -> None:
    result = compare_proportions(0, 10, 0, 10)

    assert result is not None
    assert result.method == "fisher_exact"
    assert result.p_value == fisher_reference(0, 10, 0, 10) == ONE
    assert result.expected_min == ZERO


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [(1, 20, 4, 20), (5, 999, 5, 1000), (60, 1000, 90, 1000), (3, 1500, 9, 1200)],
)
def test_compare_proportions_is_symmetric_in_the_variants(
    x_a: int, n_a: int, x_b: int, n_b: int
) -> None:
    forward = compare_proportions(x_a, n_a, x_b, n_b)
    backward = compare_proportions(x_b, n_b, x_a, n_a)

    assert forward is not None
    assert backward is not None
    assert (forward.method, forward.p_value, forward.expected_min) == (
        backward.method,
        backward.p_value,
        backward.expected_min,
    )
    assert (forward.rate_a, forward.rate_b) == (backward.rate_b, backward.rate_a)


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [(0, 0, 5, 10), (5, 10, 0, 0), (0, 0, 0, 0), (3, 0, 5, 10)],
)
def test_compare_proportions_without_trials_is_not_estimable(
    x_a: int, n_a: int, x_b: int, n_b: int
) -> None:
    assert compare_proportions(x_a, n_a, x_b, n_b) is None


def test_compare_proportions_with_a_non_finite_library_p_value_is_not_estimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nan_fisher(table: object, alternative: str) -> SimpleNamespace:
        return SimpleNamespace(pvalue=math.nan)

    monkeypatch.setattr(stats, "fisher_exact", nan_fisher)

    assert compare_proportions(1, 20, 4, 20) is None


@pytest.mark.parametrize(
    ("args", "kwargs", "error", "match"),
    [
        ((1.0, 20, 4, 20), {}, TypeError, "x_a must be an int count, got float"),
        ((1, 20.0, 4, 20), {}, TypeError, "n_a must be an int count, got float"),
        ((1, 20, True, 20), {}, TypeError, "x_b must be an int count, got bool"),
        ((1, 20, 4, Decimal(20)), {}, TypeError, "n_b must be an int count, got Decimal"),
        ((1, 20, 4, 20), {"fisher_min_expected": 5.0}, TypeError, "must be a Decimal"),
        ((1, 20, 4, 20), {"fisher_min_expected": True}, TypeError, "must be a Decimal"),
        ((-1, 20, 4, 20), {}, ValueError, r"x_a must be >= 0, got -1"),
        ((1, 20, 4, -5), {}, ValueError, r"n_b must be >= 0, got -5"),
        ((21, 20, 4, 20), {}, ValueError, r"x_a \(21\) must not exceed n_a \(20\)"),
        ((1, 20, 11, 10), {}, ValueError, r"x_b \(11\) must not exceed n_b \(10\)"),
        ((1, 20, 4, 20), {"fisher_min_expected": -1}, ValueError, "must be >= 0"),
        ((1, 20, 4, 20), {"fisher_min_expected": Decimal("NaN")}, ValueError, "finite"),
        ((1, 20, 4, 20), {"fisher_min_expected": Decimal("Infinity")}, ValueError, "finite"),
        # Invalid input raises even where the result would be "not estimable".
        ((1, 0, 4, 20), {"fisher_min_expected": -1}, ValueError, "must be >= 0"),
    ],
)
def test_compare_proportions_rejects_invalid_input(
    args: tuple[Any, ...], kwargs: dict[str, Any], error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        compare_proportions(*args, **kwargs)


# --- holm (CR1) ----------------------------------------------------------------------------


def test_holm_hand_checked_step_down() -> None:
    # Sorted 0.01, 0.03, 0.04 times 3, 2, 1 gives 0.03, 0.06, 0.04; the running maximum makes
    # the last 0.06. Results come back in input order.
    assert holm([Decimal("0.01"), Decimal("0.04"), Decimal("0.03")]) == [
        Decimal("0.030000"),
        Decimal("0.060000"),
        Decimal("0.060000"),
    ]


def test_holm_flips_two_raw_significant_pairs_to_not_distinguishable() -> None:
    adjusted = holm([Decimal("0.04"), Decimal("0.03")])

    assert adjusted == [Decimal("0.060000"), Decimal("0.060000")]
    assert all(p >= DEFAULT_ALPHA for p in adjusted)


def test_holm_caps_adjusted_values_at_one() -> None:
    assert holm([Decimal("0.5"), Decimal("0.6"), Decimal("0.9")]) == [ONE, ONE, ONE]


def test_holm_ties_and_single_values() -> None:
    assert holm([Decimal("0.02"), Decimal("0.02")]) == [Decimal("0.040000"), Decimal("0.040000")]
    assert holm([Decimal("0.0123")]) == [Decimal("0.012300")]
    assert holm([0, 1]) == [ZERO, ONE]


@pytest.mark.parametrize(
    "p_values",
    [
        ["0.001", "0.2", "0.013", "0.04", "0.5"],
        ["0.0004", "0.0004", "0.049", "0.051", "0.9", "1"],
        ["0.31415926", "0.0271828", "0.000001"],
    ],
)
def test_holm_matches_statsmodels(p_values: list[str]) -> None:
    raw = [Decimal(p) for p in p_values]

    _, corrected, _, _ = multipletests([float(p) for p in raw], alpha=0.05, method="holm")

    assert holm(raw) == [probability(value) for value in corrected]


def test_holm_adjusted_values_do_not_depend_on_alpha() -> None:
    raw = [Decimal("0.001"), Decimal("0.2"), Decimal("0.013"), Decimal("0.04")]

    assert holm(raw, Decimal("0.01")) == holm(raw) == holm(raw, DEFAULT_ALPHA)
    assert Decimal("0.05") == DEFAULT_ALPHA


def test_holm_of_nothing_is_empty() -> None:
    assert holm([]) == []


@pytest.mark.parametrize(
    ("p_values", "alpha", "error", "match"),
    [
        ([Decimal("0.1"), 0.2], ALPHA, TypeError, r"p_values\[1\] must be a Decimal, got float"),
        ([True], ALPHA, TypeError, r"p_values\[0\] must be a Decimal, got bool"),
        ([Decimal("1.01")], ALPHA, ValueError, r"p_values\[0\] must be in \[0, 1\]"),
        ([Decimal("-0.01")], ALPHA, ValueError, r"p_values\[0\] must be in \[0, 1\]"),
        ([Decimal("NaN")], ALPHA, ValueError, "must be finite"),
        ([Decimal("0.1")], 0.05, TypeError, "alpha must be a Decimal, got float"),
        ([Decimal("0.1")], Decimal(0), ValueError, r"alpha must be in \(0, 1\)"),
        ([Decimal("0.1")], Decimal(1), ValueError, r"alpha must be in \(0, 1\)"),
        ([], Decimal(1), ValueError, r"alpha must be in \(0, 1\)"),
        (
            [Decimal("0.1")],
            Decimal("1e-1000"),
            ValueError,
            r"alpha \(1E-1000\) is not representable strictly inside \(0, 1\) as a float",
        ),
        ([], Decimal("0.99999999999999999999"), ValueError, r"alpha \(0.99999999999999999999\)"),
    ],
)
def test_holm_rejects_invalid_input(
    p_values: list[Any], alpha: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        holm(p_values, alpha)


# --- wilson (CR2) ----------------------------------------------------------------------------


def test_wilson_hand_checked_bounds_at_zero_and_full_rates() -> None:
    z_squared = float(norm.ppf(0.975)) ** 2

    none_of_fifty = wilson(0, 50)
    all_of_fifty = wilson(50, 50)

    # Wilson at x = 0: [0, z^2 / (n + z^2)]; at x = n: [n / (n + z^2), 1].
    assert none_of_fifty == Interval(point=ZERO, low=ZERO, high=Decimal("0.071348"))
    assert none_of_fifty.high == six(z_squared / (50 + z_squared))
    assert all_of_fifty == Interval(point=ONE, low=Decimal("0.928652"), high=ONE)
    assert all_of_fifty.low == six(50 / (50 + z_squared))


@pytest.mark.parametrize(
    ("successes", "trials"),
    [(7, 120), (1, 3), (1, 2), (3, 1000), (412, 1000), (1, 1_000_000), (999_999, 1_000_000)],
)
def test_wilson_matches_statsmodels_and_the_closed_form(successes: int, trials: int) -> None:
    result = wilson(successes, trials)

    low, high = proportion_confint(successes, trials, alpha=0.05, method="wilson")
    assert result == Interval(
        point=exact_ratio(successes, trials), low=probability(low), high=probability(high)
    )
    z = float(norm.ppf(0.975))
    share = successes / trials
    centre = (share + z * z / (2 * trials)) / (1 + z * z / trials)
    half = (
        z / (1 + z * z / trials) * math.sqrt(share * (1 - share) / trials + z * z / trials**2 / 4)
    )
    assert abs(result.low - Decimal(repr(centre - half))) <= QUANTUM
    assert abs(result.high - Decimal(repr(centre + half))) <= QUANTUM


@pytest.mark.parametrize(
    ("successes", "trials"),
    [(0, 1), (1, 1), (0, 1_000_000), (1, 1_000_000), (2, 7), (999_999, 1_000_000), (5, 5)],
)
def test_wilson_bounds_stay_ordered_within_zero_and_one(successes: int, trials: int) -> None:
    result = wilson(successes, trials)

    assert result is not None
    assert ZERO <= result.low <= result.point <= result.high <= ONE
    for value in (result.point, result.low, result.high):
        assert value.as_tuple().exponent == -6


def test_wilson_default_confidence_is_ninety_five_percent() -> None:
    assert Decimal("0.95") == DEFAULT_CONFIDENCE
    assert wilson(7, 120) == wilson(7, 120, confidence=Decimal("0.95"))


def test_wilson_confidence_level_widens_the_interval() -> None:
    narrow = wilson(7, 120)
    wide = wilson(7, 120, confidence=Decimal("0.99"))

    low, high = proportion_confint(7, 120, alpha=0.01, method="wilson")
    assert narrow is not None
    assert wide is not None
    assert (wide.low, wide.high) == (probability(low), probability(high))
    assert wide.low < narrow.low <= narrow.point <= narrow.high < wide.high


@pytest.mark.parametrize(("successes", "trials"), [(0, 0), (5, 0)])
def test_wilson_without_trials_is_not_estimable(successes: int, trials: int) -> None:
    assert wilson(successes, trials) is None


def test_wilson_interval_is_immutable() -> None:
    result = wilson(7, 120)

    assert result is not None
    with pytest.raises(FrozenInstanceError):
        result.low = ZERO  # type: ignore[misc]


@pytest.mark.parametrize(
    ("successes", "trials", "confidence", "error", "match"),
    [
        (1.0, 10, DEFAULT_CONFIDENCE, TypeError, "successes must be an int count, got float"),
        (1, 10.0, DEFAULT_CONFIDENCE, TypeError, "trials must be an int count, got float"),
        (False, 10, DEFAULT_CONFIDENCE, TypeError, "successes must be an int count, got bool"),
        (-1, 10, DEFAULT_CONFIDENCE, ValueError, "successes must be >= 0"),
        (1, -10, DEFAULT_CONFIDENCE, ValueError, "trials must be >= 0"),
        (11, 10, DEFAULT_CONFIDENCE, ValueError, r"successes \(11\) must not exceed trials"),
        (1, 10, 0.95, TypeError, "confidence must be a Decimal, got float"),
        (1, 10, Decimal(0), ValueError, r"confidence must be in \(0, 1\)"),
        (1, 10, Decimal(1), ValueError, r"confidence must be in \(0, 1\)"),
        (1, 10, Decimal("Infinity"), ValueError, "confidence must be finite"),
        # The level is validated even when there are no trials.
        (0, 0, Decimal("1.5"), ValueError, r"confidence must be in \(0, 1\)"),
        # Inside (0, 1), but 1 - confidence is no float strictly inside it.
        (
            1,
            10,
            Decimal("1e-20"),
            ValueError,
            r"1 - confidence \(0.99999999999999999999\) is not representable strictly inside",
        ),
        (
            1,
            10,
            Decimal("0." + "9" * 1000),
            ValueError,
            r"1 - confidence \(1E-1000\) is not representable strictly inside \(0, 1\)",
        ),
        (0, 0, Decimal("1e-20"), ValueError, r"1 - confidence \(0.99999999999999999999\)"),
    ],
)
def test_wilson_rejects_invalid_input(
    successes: Any, trials: Any, confidence: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        wilson(successes, trials, confidence=confidence)


# --- cohens_h (CR1, CR3) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p1", "p2"),
    [("0.024", "0.02"), ("0.02", "0.024"), ("0.5", "0.1"), ("0.999", "0.001"), ("0.3", "0.3")],
)
def test_cohens_h_matches_statsmodels_and_the_arcsine_formula(p1: str, p2: str) -> None:
    result = cohens_h(Decimal(p1), Decimal(p2))

    assert result == six(proportion_effectsize(float(p1), float(p2)))
    formula = 2 * math.asin(math.sqrt(float(p1))) - 2 * math.asin(math.sqrt(float(p2)))
    assert abs(result - Decimal(repr(formula))) <= QUANTUM


def test_cohens_h_docstring_case_and_sign() -> None:
    reference = six(proportion_effectsize(0.024, 0.02))
    assert cohens_h(Decimal("0.024"), Decimal("0.02")) == reference == Decimal("0.027297")
    assert cohens_h(Decimal("0.02"), Decimal("0.024")) == Decimal("-0.027297")


def test_cohens_h_extremes_are_plus_and_minus_pi() -> None:
    assert cohens_h(Decimal(1), Decimal(0)) == six(math.pi) == Decimal("3.141593")
    assert cohens_h(0, 1) == Decimal("-3.141593")


def test_cohens_h_never_returns_a_negative_zero() -> None:
    tiny_negative = cohens_h(Decimal("0.3"), Decimal("0.3000000001"))

    assert proportion_effectsize(0.3, 0.3000000001) < 0
    assert str(tiny_negative) == "0.000000"
    assert str(cohens_h(Decimal("0.3"), Decimal("0.3"))) == "0.000000"


@pytest.mark.parametrize(
    ("p1", "p2", "error", "match"),
    [
        (0.5, Decimal("0.1"), TypeError, "p1 must be a Decimal, got float"),
        (Decimal("0.5"), True, TypeError, "p2 must be a Decimal, got bool"),
        (Decimal("1.1"), Decimal("0.1"), ValueError, r"p1 must be in \[0, 1\]"),
        (Decimal("0.5"), Decimal("-0.1"), ValueError, r"p2 must be in \[0, 1\]"),
        (Decimal("NaN"), Decimal("0.1"), ValueError, "p1 must be finite"),
    ],
)
def test_cohens_h_rejects_invalid_input(
    p1: Any, p2: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        cohens_h(p1, p2)


# --- required_n (CR3) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p_base", "p_target"),
    [("0.02", "0.024"), ("0.1", "0.12"), ("0.3", "0.36"), ("0.05", "0.06"), ("0.5", "1")],
)
def test_required_n_matches_the_statsmodels_power_solver(p_base: str, p_target: str) -> None:
    result = required_n(Decimal(p_base), Decimal(p_target), alpha=ALPHA, power=POWER)

    expected = solver_n(float(p_base), float(p_target))
    assert result == expected
    # The smallest whole sample that reaches the target power.
    assert power_at(float(p_base), float(p_target), expected) >= 0.8
    assert power_at(float(p_base), float(p_target), expected - 1) < 0.8


def test_required_n_docstring_cases() -> None:
    two_percent = required_n(Decimal("0.02"), Decimal("0.024"), alpha=ALPHA, power=POWER)
    ten_percent = required_n(Decimal("0.1"), Decimal("0.12"), alpha=ALPHA, power=POWER)

    assert two_percent == solver_n(0.02, 0.024) == 21067
    assert ten_percent == solver_n(0.1, 0.12) == 3835


def test_required_n_uses_the_given_alpha_and_power() -> None:
    result = required_n(
        Decimal("0.1"), Decimal("0.12"), alpha=Decimal("0.01"), power=Decimal("0.9")
    )

    assert result == solver_n(0.1, 0.12, alpha=0.01, power=0.9)
    assert result is not None
    assert result > solver_n(0.1, 0.12)


def test_required_n_does_not_depend_on_the_direction_of_the_change() -> None:
    up = required_n(Decimal("0.1"), Decimal("0.12"), alpha=ALPHA, power=POWER)
    down = required_n(Decimal("0.12"), Decimal("0.1"), alpha=ALPHA, power=POWER)

    assert up == down == solver_n(0.1, 0.12)


def test_samples_of_one_and_two_per_group_are_sized_before_the_solver_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effect = abs(proportion_effectsize(1.0, 0.0001))
    # The library solver brackets from 2 upwards and cannot size these effects at all.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        unsolved = NormalIndPower().solve_power(
            effect_size=effect, nobs1=None, alpha=0.05, power=0.8, ratio=1.0, alternative=TWO_SIDED
        )
    assert math.isnan(float(unsolved))
    assert any(issubclass(w.category, ConvergenceWarning) for w in caught)
    # One observation per group reaches power 0.5 but not 0.8; two reach 0.8.
    assert power_at(0.0001, 1.0, 1) >= 0.5
    assert power_at(0.0001, 1.0, 1) < 0.8 <= power_at(0.0001, 1.0, 2)

    def no_root_finder(*args: object, **kwargs: object) -> object:
        raise AssertionError("the root finder must not run for samples of 1 or 2 per group")

    monkeypatch.setattr(stats, "brentq_expanding", no_root_finder)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        one = required_n(Decimal("0.0001"), Decimal(1), alpha=ALPHA, power=Decimal("0.5"))
        two = required_n(Decimal("0.0001"), Decimal(1), alpha=ALPHA, power=POWER)
        two_down = required_n(Decimal(1), Decimal("0.0001"), alpha=ALPHA, power=POWER)

    assert (one, two, two_down) == (1, 2, 2)


def test_three_per_group_comes_from_the_solver() -> None:
    result = required_n(Decimal("0.05"), Decimal(1), alpha=ALPHA, power=POWER)

    assert result == solver_n(0.05, 1.0) == 3


@pytest.mark.parametrize(
    ("p_base", "p_target"),
    [
        ("0", "0.1"),  # baseline rate of 0
        ("-0.1", "0.1"),
        ("0.9", "1.08"),  # baseline x 1.2 above 100%
        ("0.1", "0"),
        ("0.1", "-0.2"),
        ("1.5", "0.5"),  # baseline is not a proportion
        ("0.1", "0.1"),  # no difference to detect
        ("0.1", "0.10000000000000000001"),  # different, but equal as floats: no effect
    ],
)
def test_required_n_not_estimable(p_base: str, p_target: str) -> None:
    assert required_n(Decimal(p_base), Decimal(p_target), alpha=ALPHA, power=POWER) is None


def test_required_n_allows_a_target_of_exactly_one_and_a_baseline_of_one() -> None:
    assert required_n(Decimal("0.5"), Decimal(1), alpha=ALPHA, power=POWER) == solver_n(0.5, 1.0)
    assert required_n(Decimal(1), Decimal("0.8"), alpha=ALPHA, power=POWER) == solver_n(1.0, 0.8)


@pytest.mark.parametrize("alpha", [Decimal("1e-30"), Decimal("1e-300")])
def test_required_n_at_a_tiny_alpha_is_sized_where_the_library_solver_fails(
    alpha: Decimal,
) -> None:
    # solve_power infers the direction from the power at 2 and at 50, equal here in floats.
    assert library_solver_fails(0.1, 0.12, alpha=float(alpha))

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = required_n(Decimal("0.1"), Decimal("0.12"), alpha=alpha, power=POWER)

    assert result is not None
    assert power_at(0.1, 0.12, result, alpha=float(alpha)) >= 0.8
    assert power_at(0.1, 0.12, result - 1, alpha=float(alpha)) < 0.8


def test_required_n_for_a_tiny_effect_is_sized_where_the_library_solver_fails() -> None:
    # h is about 2e-11, below what the power at 2 and at 50 can tell apart in floats.
    assert library_solver_fails(0.5, 0.50000000001, alpha=0.05)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = required_n(Decimal("0.5"), Decimal("0.50000000001"), alpha=ALPHA, power=POWER)

    assert result is not None
    assert result > 10**22
    # Whole samples this large differ by less than float resolution; a millionth less fails.
    assert power_at(0.5, 0.50000000001, result) >= 0.8
    assert power_at(0.5, 0.50000000001, result - result // 1_000_000) < 0.8


def test_required_n_for_an_unreachable_power_is_not_estimable_without_a_warning() -> None:
    # h is about 8e-101: the power stays below 0.8 up to the root finder's 5e102 per group.
    assert power_at(1e-200, 2e-200, 5 * 10**102) < 0.8

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = required_n(Decimal("1e-200"), Decimal("2e-200"), alpha=ALPHA, power=POWER)
        extra = additional_n_for_difference(1, 10**200, 2, 10**200, alpha=ALPHA, power=POWER)

    assert result is None
    assert extra is None


@pytest.mark.parametrize(
    "outcome",
    [
        ValueError("f(a) and f(b) must have different signs"),
        RuntimeError("Failed to converge after 100 iterations"),
        (3834.2, SimpleNamespace(converged=False)),
        (math.inf, SimpleNamespace(converged=True)),
        (-1.0, SimpleNamespace(converged=True)),
    ],
)
def test_required_n_without_a_usable_root_is_not_estimable(
    monkeypatch: pytest.MonkeyPatch, outcome: Exception | tuple[float, SimpleNamespace]
) -> None:
    def root_finder(*args: object, **kwargs: object) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(stats, "brentq_expanding", root_finder)

    assert required_n(Decimal("0.1"), Decimal("0.12"), alpha=ALPHA, power=POWER) is None
    assert additional_n_for_difference(10, 100, 12, 100, alpha=ALPHA, power=POWER) is None


@pytest.mark.parametrize(
    ("p_base", "p_target", "alpha", "power", "error", "match"),
    [
        (0.1, Decimal("0.12"), ALPHA, POWER, TypeError, "p_base must be a Decimal, got float"),
        (Decimal("0.1"), 0.12, ALPHA, POWER, TypeError, "p_target must be a Decimal, got float"),
        (Decimal("0.1"), Decimal("0.12"), 0.05, POWER, TypeError, "alpha must be a Decimal"),
        (Decimal("0.1"), Decimal("0.12"), ALPHA, True, TypeError, "power must be a Decimal"),
        (Decimal("NaN"), Decimal("0.12"), ALPHA, POWER, ValueError, "p_base must be finite"),
        (Decimal("0.1"), Decimal("-Infinity"), ALPHA, POWER, ValueError, "p_target must be"),
        (Decimal("0.1"), Decimal("0.12"), Decimal(0), POWER, ValueError, r"alpha must be in"),
        (Decimal("0.1"), Decimal("0.12"), Decimal(1), POWER, ValueError, r"alpha must be in"),
        (Decimal("0.1"), Decimal("0.12"), ALPHA, Decimal(1), ValueError, r"power must be in"),
        (Decimal("0.1"), Decimal("0.12"), ALPHA, ALPHA, ValueError, r"power \(0.05\) must exceed"),
        (Decimal("0.1"), Decimal("0.12"), Decimal("0.5"), Decimal("0.4"), ValueError, "exceed"),
        # A configuration error is never masked as "not estimable".
        (Decimal(0), Decimal("0.1"), ALPHA, ALPHA, ValueError, "must exceed alpha"),
        # Levels inside (0, 1) as Decimals, but not as the floats the power analysis takes.
        (
            Decimal("0.1"),
            Decimal("0.12"),
            Decimal("1e-1000"),
            POWER,
            ValueError,
            r"alpha \(1E-1000\) is not representable strictly inside \(0, 1\) as a float",
        ),
        (
            Decimal("0.1"),
            Decimal("0.12"),
            ALPHA,
            Decimal("0.99999999999999999999"),
            ValueError,
            r"power \(0.99999999999999999999\) is not representable strictly inside \(0, 1\)",
        ),
        (
            Decimal("0.1"),
            Decimal("0.12"),
            Decimal("0.3"),
            Decimal("0.30000000000000000001"),
            ValueError,
            r"must exceed alpha \(0.3\) as a float; both convert to 0.3",
        ),
    ],
)
def test_required_n_rejects_invalid_input(
    p_base: Any, p_target: Any, alpha: Any, power: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        required_n(p_base, p_target, alpha=alpha, power=power)


# --- additional_n_for_difference (CR1) -----------------------------------------------------


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [
        (20, 1000, 30, 1000),
        (1, 1000, 2, 1000),
        (0, 50, 1, 50),  # a variant without leads still has an observed rate of 0
        (12, 800, 30, 1500),  # unequal samples: each variant grows by the same increment
        (45, 3000, 40, 2000),
        (20, 1000, 3000, 100000),  # the larger sample alone exceeds the equal-group sample
    ],
)
def test_additional_n_is_the_smallest_common_increment_that_reaches_the_power(
    x_a: int, n_a: int, x_b: int, n_b: int
) -> None:
    result = additional_n_for_difference(x_a, n_a, x_b, n_b, alpha=ALPHA, power=POWER)

    assert result is not None
    assert result > 0
    assert common_increment_power(x_a, n_a, x_b, n_b, result) >= 0.8
    assert common_increment_power(x_a, n_a, x_b, n_b, result - 1) < 0.8
    # Growing both variants to the equal-group sample always suffices.
    low, high = sorted((x_a / n_a, x_b / n_b))
    assert result <= solver_n(low, high) - min(n_a, n_b)
    assert additional_n_for_difference(x_b, n_b, x_a, n_a, alpha=ALPHA, power=POWER) == result


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [(20, 1000, 30, 1000), (1, 1000, 2, 1000), (0, 50, 1, 50), (2, 1000, 9, 1000)],
)
def test_additional_n_for_equal_samples_is_the_solver_sample_minus_the_observed_one(
    x_a: int, n_a: int, x_b: int, n_b: int
) -> None:
    result = additional_n_for_difference(x_a, n_a, x_b, n_b, alpha=ALPHA, power=POWER)

    low, high = sorted((x_a / n_a, x_b / n_b))
    assert result == solver_n(low, high) - n_a
    assert result is not None
    assert result > 0


def test_imbalanced_delivery_still_needs_more_impressions_per_variant() -> None:
    # 2% on 1,000 impressions against 3% on 100,000: the larger variant alone holds far more
    # than the equal-group sample, yet the pair is not distinguishable.
    comparison = compare_proportions(20, 1000, 3000, 100000)
    result = additional_n_for_difference(20, 1000, 3000, 100000, alpha=ALPHA, power=POWER)

    assert comparison is not None
    assert comparison.p_value > Decimal("0.05")
    assert solver_n(0.02, 0.03) == 3789
    assert result == 931
    assert power_at(0.02, 0.03, 1931, ratio=100931 / 1931) >= 0.8
    assert power_at(0.02, 0.03, 1930, ratio=100930 / 1930) < 0.8


def test_additional_n_uses_the_given_alpha_and_power() -> None:
    result = additional_n_for_difference(
        20, 1000, 30, 1000, alpha=Decimal("0.01"), power=Decimal("0.9")
    )

    assert result == solver_n(0.02, 0.03, alpha=0.01, power=0.9) - 1000


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [
        (0, 10, 10, 10),
        (0, 1000, 5, 1000),
        (300, 1000, 30, 1000),
        (90, 100, 10, 100),
        # The smaller variant holds fewer than the equal-group 3,789; the larger makes up for it.
        (40, 2000, 30000, 1000000),
    ],
)
def test_additional_n_is_zero_when_the_samples_already_suffice(
    x_a: int, n_a: int, x_b: int, n_b: int
) -> None:
    # Power grows with either variant's sample, so observed samples that already reach the
    # target power need no common increment, and the result is never negative.
    assert common_increment_power(x_a, n_a, x_b, n_b, 0) >= 0.8

    assert additional_n_for_difference(x_a, n_a, x_b, n_b, alpha=ALPHA, power=POWER) == 0


def test_a_larger_variant_can_make_up_for_a_smaller_one() -> None:
    # The smaller variant alone falls short of the equal-group sample at 2% against 3%.
    assert solver_n(0.02, 0.03) > 2000
    assert additional_n_for_difference(40, 2000, 30000, 1000000, alpha=ALPHA, power=POWER) == 0
    assert additional_n_for_difference(30000, 1000000, 40, 2000, alpha=ALPHA, power=POWER) == 0


@pytest.mark.parametrize(
    "outcome",
    [
        ValueError("f(a) and f(b) must have different signs"),
        RuntimeError("Failed to converge after 100 iterations"),
        (931.5, SimpleNamespace(converged=False)),
        (math.nan, SimpleNamespace(converged=True)),
        # A root no whole increment next to it confirms, as floating-point noise could give.
        (0.5, SimpleNamespace(converged=True)),
    ],
)
def test_additional_n_falls_back_to_the_equal_group_bound_when_the_root_is_unusable(
    monkeypatch: pytest.MonkeyPatch, outcome: Exception | tuple[float, SimpleNamespace]
) -> None:
    def root_finder(function: Any, **kwargs: Any) -> Any:
        if kwargs["upp"] is None:  # the equal-group sample, sized by the real root finder
            return brentq_expanding(function, **kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(stats, "brentq_expanding", root_finder)

    result = additional_n_for_difference(20, 1000, 3000, 100000, alpha=ALPHA, power=POWER)

    # Both variants at the equal-group sample: always enough, if far more than needed.
    assert result == solver_n(0.02, 0.03) - 1000 == 2789
    assert common_increment_power(20, 1000, 3000, 100000, 2789) >= 0.8


@pytest.mark.parametrize(
    ("x_a", "n_a", "x_b", "n_b"),
    [
        (5, 100, 10, 200),  # identical rates: no observed difference
        (0, 100, 0, 200),
        (0, 0, 5, 10),  # a variant without impressions
        (5, 10, 0, 0),
        (3, 0, 5, 10),
        FLOAT_EQUAL_RATES,  # exactly different, but no effect in floating point
    ],
)
def test_additional_n_not_estimable(x_a: int, n_a: int, x_b: int, n_b: int) -> None:
    assert additional_n_for_difference(x_a, n_a, x_b, n_b, alpha=ALPHA, power=POWER) is None


def test_float_equal_rates_really_differ_exactly() -> None:
    x_a, n_a, x_b, n_b = FLOAT_EQUAL_RATES

    assert Fraction(x_a, n_a) != Fraction(x_b, n_b)
    assert x_a / n_a == x_b / n_b


@pytest.mark.parametrize(
    ("args", "alpha", "power", "error", "match"),
    [
        ((1.0, 10, 2, 10), ALPHA, POWER, TypeError, "x_a must be an int count, got float"),
        ((1, 10, 2, True), ALPHA, POWER, TypeError, "n_b must be an int count, got bool"),
        ((11, 10, 2, 10), ALPHA, POWER, ValueError, r"x_a \(11\) must not exceed n_a \(10\)"),
        ((1, 10, -2, 10), ALPHA, POWER, ValueError, "x_b must be >= 0"),
        ((1, 10, 2, 10), 0.05, POWER, TypeError, "alpha must be a Decimal, got float"),
        ((1, 10, 2, 10), ALPHA, Decimal("1.2"), ValueError, r"power must be in \(0, 1\)"),
        ((1, 10, 2, 10), Decimal("0.2"), Decimal("0.1"), ValueError, "must exceed alpha"),
        ((1, 0, 2, 10), ALPHA, ALPHA, ValueError, "must exceed alpha"),
        ((1, 10, 2, 10), Decimal("1e-1000"), POWER, ValueError, r"alpha \(1E-1000\) is not"),
        (
            (1, 10, 2, 10),
            ALPHA,
            Decimal("0.99999999999999999999"),
            ValueError,
            r"power \(0.99999999999999999999\) is not representable strictly inside \(0, 1\)",
        ),
        (
            (1, 0, 2, 10),
            Decimal("0.3"),
            Decimal("0.30000000000000000001"),
            ValueError,
            r"must exceed alpha \(0.3\) as a float",
        ),
    ],
)
def test_additional_n_rejects_invalid_input(
    args: tuple[Any, ...], alpha: Any, power: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        additional_n_for_difference(*args, alpha=alpha, power=power)


# --- poisson_rate_test (B4) ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("count_a", "spend_a", "count_b", "spend_b"),
    [
        (30, Decimal(1000), 10, Decimal(1000)),
        (4, Decimal("812.40"), 15, Decimal("2950.15")),
        # One side without outcomes is still a rate. statsmodels' own ratio estimate then
        # divides by a zero rate, which never reaches the score test's p-value.
        pytest.param(0, Decimal(500), 12, Decimal(1500), marks=ZERO_RATE_RATIO_WARNING),
        pytest.param(12, Decimal(1500), 0, Decimal(500), marks=ZERO_RATE_RATIO_WARNING),
        (10, Decimal(1000), 20, Decimal(2000)),  # equal rates
        (3, Decimal("0.75"), 17, Decimal("123456.789012")),
    ],
)
def test_poisson_rate_test_matches_the_statsmodels_score_test(
    count_a: int, spend_a: Decimal, count_b: int, spend_b: Decimal
) -> None:
    result = poisson_rate_test(count_a, spend_a, count_b, spend_b)

    assert result == RateTest(
        method=POISSON_RATE_METHOD,
        p_value=poisson_reference(count_a, spend_a, count_b, spend_b),
        rate_a=exact_ratio(count_a, spend_a),
        rate_b=exact_ratio(count_b, spend_b),
    )
    swapped = poisson_rate_test(count_b, spend_b, count_a, spend_a)
    assert swapped is not None
    assert swapped.p_value == result.p_value


def test_poisson_rate_test_docstring_case_and_equal_rates() -> None:
    result = poisson_rate_test(30, Decimal(1000), 10, Decimal(1000))
    equal = poisson_rate_test(10, Decimal(1000), 20, Decimal(2000))

    assert result is not None
    assert result.method == POISSON_RATE_METHOD == "two_sample_poisson_score"
    assert result.p_value == poisson_reference(30, Decimal(1000), 10, Decimal(1000))
    assert result.p_value == Decimal("0.001565")
    assert (result.rate_a, result.rate_b) == (Decimal("0.030000"), Decimal("0.010000"))
    assert result.p_value < Decimal("0.05")
    assert equal is not None
    assert equal.p_value == ONE


def test_poisson_rate_test_accepts_int_exposure() -> None:
    assert poisson_rate_test(30, 1000, 10, 1000) == poisson_rate_test(
        30, Decimal(1000), 10, Decimal(1000)
    )


@pytest.mark.parametrize(
    ("count_a", "spend_a", "count_b", "spend_b"),
    [
        (0, Decimal(1000), 0, Decimal(1000)),  # no outcome on either side
        (5, Decimal(0), 5, Decimal(1000)),  # no exposure
        (5, Decimal(1000), 5, Decimal("0.00")),
        (5, Decimal(1000), 5, Decimal("-1")),
        (0, Decimal(0), 0, Decimal(0)),
    ],
)
def test_poisson_rate_test_not_estimable(
    count_a: int, spend_a: Decimal, count_b: int, spend_b: Decimal
) -> None:
    assert poisson_rate_test(count_a, spend_a, count_b, spend_b) is None


def test_poisson_rate_test_with_a_non_finite_library_p_value_is_not_estimable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nan_test(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(pvalue=math.nan)

    monkeypatch.setattr(stats, "_poisson_2indep", nan_test)

    assert poisson_rate_test(30, Decimal(1000), 10, Decimal(1000)) is None


@pytest.mark.parametrize(
    ("count_a", "spend_a", "count_b", "spend_b", "error", "match"),
    [
        # Weighted conversions are never rounded into the test: non-int counts are rejected.
        (3.0, Decimal(1000), 10, Decimal(1000), TypeError, "count_a must be an int count"),
        (3, Decimal(1000), Decimal(10), Decimal(1000), TypeError, "count_b must be an int count"),
        (True, Decimal(1000), 10, Decimal(1000), TypeError, "count_a must be an int count"),
        (3, 1000.0, 10, Decimal(1000), TypeError, "exposure_a must be a Decimal, got float"),
        (3, Decimal(1000), 10, False, TypeError, "exposure_b must be a Decimal, got bool"),
        (-3, Decimal(1000), 10, Decimal(1000), ValueError, "count_a must be >= 0"),
        (3, Decimal("NaN"), 10, Decimal(1000), ValueError, "exposure_a must be finite"),
        (3, Decimal(1000), 10, Decimal("Infinity"), ValueError, "exposure_b must be finite"),
        # Invalid input raises even where the result would be "no exposure".
        (3.5, Decimal(0), 10, Decimal(1000), TypeError, "count_a must be an int count"),
    ],
)
def test_poisson_rate_test_rejects_invalid_input(
    count_a: Any, spend_a: Any, count_b: Any, spend_b: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        poisson_rate_test(count_a, spend_a, count_b, spend_b)


# --- percentile (B3) -----------------------------------------------------------------------

SPEND_DAYS = [
    [Decimal("12.34"), Decimal("56.78"), Decimal("90.12"), Decimal("3.45"), Decimal("67.89")],
    [Decimal(10), Decimal(20), Decimal(30), Decimal(40)],
    [Decimal("7.5"), Decimal("7.5"), Decimal("7.5"), Decimal("0"), Decimal("100.01")],
    [Decimal("0.1"), Decimal("0.2"), Decimal("0.3"), Decimal("0.4"), Decimal("0.5"), Decimal(0)],
    [Decimal("42.42")],
]


@pytest.mark.parametrize("values", SPEND_DAYS)
@pytest.mark.parametrize("q", ["0", "0.25", "0.5", "0.9", "0.95", "1"])
def test_percentile_is_numpys_linear_method_exactly(values: list[Decimal], q: str) -> None:
    result = percentile(values, Decimal(q))

    assert result == type7_reference(values, Decimal(q))
    assert result is not None
    numpy_value = np.quantile([float(v) for v in values], float(q), method="linear")
    assert abs(result - Decimal(repr(float(numpy_value)))) <= QUANTUM
    assert result.as_tuple().exponent == -6


def test_percentile_docstring_cases() -> None:
    four_days = [Decimal(40), Decimal(10), Decimal(30), Decimal(20)]

    assert percentile(four_days, Decimal("0.5")) == Decimal("25.000000")
    assert percentile(sorted(four_days), Decimal("0.9")) == Decimal("37.000000")


def test_percentile_ends_are_the_minimum_and_maximum() -> None:
    values = [Decimal("5.5"), Decimal("-1.25"), Decimal("3")]

    assert percentile(values, Decimal(0)) == Decimal("-1.250000")
    assert percentile(values, Decimal(1)) == Decimal("5.500000")
    assert percentile([Decimal("42.42")], Decimal("0.37")) == Decimal("42.420000")


def test_percentile_keeps_money_exact_beyond_float_precision() -> None:
    values = [Decimal("1000000000.000001"), Decimal("1000000000.000003")]

    assert percentile(values, Decimal("0.5")) == Decimal("1000000000.000002")
    assert float(values[0]) == float(Decimal("1000000000.000001")) != float(values[1])


def test_percentile_rounds_half_to_even_at_six_places() -> None:
    assert percentile([Decimal(0), Decimal("0.000001")], Decimal("0.5")) == ZERO
    assert percentile([Decimal(0), Decimal("0.000003")], Decimal("0.5")) == Decimal("0.000002")


def test_percentile_accepts_ints_and_leaves_its_input_unchanged() -> None:
    values: list[Any] = [Decimal(30), 10, Decimal(20)]
    whole_q: Any = 1

    assert percentile(values, Decimal("0.9")) == type7_reference(values, Decimal("0.9"))
    assert values == [Decimal(30), 10, Decimal(20)]
    assert percentile((Decimal(1), Decimal(3)), whole_q) == Decimal("3.000000")


def test_percentile_of_no_values_is_none() -> None:
    assert percentile([], Decimal("0.9")) is None


@pytest.mark.parametrize(
    ("values", "q", "error", "match"),
    [
        ([Decimal(1)], Decimal(90), ValueError, r"q must be a fraction in \[0, 1\]"),
        ([Decimal(1)], Decimal("-0.1"), ValueError, r"q must be a fraction in \[0, 1\]"),
        ([Decimal(1)], Decimal("1.000001"), ValueError, r"q must be a fraction in \[0, 1\]"),
        ([], Decimal(90), ValueError, r"q must be a fraction in \[0, 1\]"),
        ([Decimal(1)], 0.9, TypeError, "q must be a Decimal, got float"),
        ([Decimal(1)], Decimal("NaN"), ValueError, "q must be finite"),
        ([Decimal(1), 2.5], Decimal("0.9"), TypeError, r"values\[1\] must be a Decimal"),
        ([True], Decimal("0.9"), TypeError, r"values\[0\] must be a Decimal, got bool"),
        ([Decimal(1), Decimal("Infinity")], Decimal("0.9"), ValueError, r"values\[1\] must be"),
    ],
)
def test_percentile_rejects_invalid_input(
    values: list[Any], q: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        percentile(values, q)


# --- conversion helpers --------------------------------------------------------------------


def test_library_floats_convert_through_repr_with_numpy_scalars_unwrapped() -> None:
    assert stats._from_float(np.float64(0.25)) == Decimal("0.250000")
    assert stats._from_float(0.0000005) == ZERO  # half-even at the sixth place
    assert stats._from_float(0.0000015) == Decimal("0.000002")
    assert str(stats._from_float(-0.0000001)) == "0.000000"
    assert stats._from_float(math.nan) is None
    assert stats._from_float(math.inf) is None
    assert stats._from_float(-math.inf) is None


def test_library_probabilities_are_clamped_to_zero_and_one() -> None:
    assert stats._probability_from_float(1.0000000000000002) == ONE
    assert stats._probability_from_float(1.5) == ONE
    assert stats._probability_from_float(-0.2) == ZERO
    assert stats._probability_from_float(math.nan) is None


def test_a_non_finite_result_for_valid_input_raises() -> None:
    assert stats._require_finite(Decimal("0.5"), "library") == Decimal("0.5")
    with pytest.raises(ArithmeticError, match="library returned a non-finite value"):
        stats._require_finite(None, "library")


@pytest.mark.parametrize(
    ("function", "target", "replacement"),
    [
        (lambda: wilson(7, 120), "proportion_confint", lambda *a, **k: (math.nan, 0.1)),
        (lambda: holm([Decimal("0.1")]), "multipletests", lambda *a, **k: (0, [math.inf], 0, 0)),
        (
            lambda: cohens_h(Decimal("0.2"), Decimal("0.1")),
            "proportion_effectsize",
            lambda *a, **k: math.nan,
        ),
    ],
)
def test_non_finite_library_results_raise_rather_than_vanish(
    monkeypatch: pytest.MonkeyPatch, function: Any, target: str, replacement: Any
) -> None:
    monkeypatch.setattr(stats, target, replacement)

    with pytest.raises(ArithmeticError, match="non-finite value for valid input"):
        function()


def test_per_group_sample_without_a_finite_effect_is_not_estimable() -> None:
    assert stats._per_group_n(math.nan, 0.5, alpha=0.05, power=0.8) is None
    assert stats._per_group_n(0.5, 0.5, alpha=0.05, power=0.8) is None


# --- module contract ----------------------------------------------------------------------


def test_results_do_not_depend_on_the_callers_decimal_context() -> None:
    def every_result() -> list[object]:
        return [
            compare_proportions(5, 999, 5, 1000),
            compare_proportions(60, 1000, 90, 1000, fisher_min_expected=Decimal("74.5")),
            holm([Decimal("0.001"), Decimal("0.2"), Decimal("0.013")]),
            wilson(7, 120),
            cohens_h(Decimal("0.024"), Decimal("0.02")),
            required_n(Decimal("0.02"), Decimal("0.024"), alpha=ALPHA, power=POWER),
            additional_n_for_difference(12, 800, 30, 1500, alpha=ALPHA, power=POWER),
            poisson_rate_test(3, Decimal("0.75"), 17, Decimal("123456.789012")),
            percentile([Decimal("1000000000.000001"), Decimal("1000000000.000003")], HALF),
        ]

    baseline = every_result()
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        constrained = every_result()

    assert constrained == baseline


def test_results_are_immutable() -> None:
    proportion = compare_proportions(1, 20, 4, 20)
    rate = poisson_rate_test(30, Decimal(1000), 10, Decimal(1000))

    assert proportion is not None
    assert rate is not None
    with pytest.raises(FrozenInstanceError):
        proportion.p_value = ZERO  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        rate.p_value = ZERO  # type: ignore[misc]


def test_docstring_examples_hold() -> None:
    results = doctest.testmod(stats, report=False)

    assert results.attempted > 0
    assert results.failed == 0


def test_module_is_pure_and_uses_only_the_standard_library_scipy_and_statsmodels() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(stats))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module)

    assert {"scipy.stats", "statsmodels.stats.rates"} <= imported
    for module in imported:
        root = module.partition(".")[0]
        assert root in sys.stdlib_module_names or root in {"scipy", "statsmodels"}, module
    assert "logging" not in imported


def test_module_namespace_holds_no_collectable_test_name() -> None:
    assert [name for name in vars(stats) if name.startswith("test")] == []


def test_public_names_are_exactly_the_documented_api() -> None:
    assert sorted(stats.__all__) == [
        "DEFAULT_ALPHA",
        "DEFAULT_CONFIDENCE",
        "FISHER_MIN_EXPECTED",
        "Interval",
        "POISSON_RATE_METHOD",
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
    for name in stats.__all__:
        assert hasattr(stats, name), name
