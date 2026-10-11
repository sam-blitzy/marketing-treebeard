"""Unit tests for the money text of the rules' reason sentences, one minor-unit rule everywhere.

Every rule that words an amount in its reason or evidence text (``white_papers``,
``campaigns``, ``budget``, ``keywords``) prints it as "{ISO code} {figure}", the figure grouped
in thousands and rounded to the currency's ISO 4217 minor unit from
:func:`ads_engine.decisions.minor_unit_exponent`: the unit the API validates decided amounts to
(AAP 0.5.2), so ``IQD`` has three places and ``AFN`` and ``ALL`` two, where CLDR gives none.
Every one rounds with :func:`ads_engine.decisions.round_to_minor_unit` (half up: a tie rounds
away from zero), as the web client's ``formatMoney`` rounds a stored amount in the cards' cells,
so one amount reads alike in a reason, in the cell beside it and on every other card (AAP 0.5.5:
numbers in the account currency). ``white_papers``, ``campaigns`` and ``budget`` round a stored
amount when they word it. ``keywords`` rounds each amount to the minor unit once, before its
evidence stores it and its reason words it, so its reason and its cell word one already-rounded
value, and it decides a tie on the exact amount, a ``Fraction`` included. Pure: no database and
no network.
"""

from collections.abc import Callable
from decimal import Decimal
from fractions import Fraction

import pytest

from ads_engine.analysis import budget, campaigns, keywords, white_papers
from ads_engine.analysis.attribution import CostValue
from ads_engine.decisions import minor_unit_exponent

# web/src/lib/format.test.ts asserts formatMoney on these amounts with the same literal strings,
# with a no-break space (U+00A0) where these have a plain space. Change both sides together.
ROUNDED_PARITY: tuple[tuple[str, str, str], ...] = (
    ("517.85", "USD", "USD 517.85"),
    ("40", "EUR", "EUR 40.00"),
    ("24.6", "JPY", "JPY 25"),
    ("2.3456", "KWD", "KWD 2.346"),
    ("50", "IQD", "IQD 50.000"),
    ("50.00", "AFN", "AFN 50.00"),
    ("12.3456", "ALL", "ALL 12.35"),
    ("5", "XYZ", "XYZ 5.00"),
    ("89.225000", "USD", "USD 89.23"),
    ("-89.225", "USD", "USD -89.23"),
)
"""``(amount, currency, money text)``: common currencies, those whose ISO 4217 minor unit
differs from CLDR's, a well-formed unknown code, which has two places, and a tie on each side of
zero, which rounds half up, away from zero, at the minor unit."""

GROUPED_PARITY: tuple[tuple[str, str, str], ...] = (
    ("1400", "USD", "USD 1,400.00"),
    ("2766", "USD", "USD 2,766.00"),
    ("1234.5", "IQD", "IQD 1,234.500"),
    ("5000", "JPY", "JPY 5,000"),
)
"""Amounts of 1,000 and more, which every renderer prints with thousands separators."""


def _keywords_money(amount: Decimal, currency: str) -> str:
    """K1-K3 round to the minor unit first, then render the rounded value."""
    return keywords._money_text(keywords._money(amount, currency), currency)


Renderer = Callable[[Decimal, str], str]

RENDER_TIME_RENDERERS: tuple[tuple[str, Renderer], ...] = (
    ("white_papers", white_papers._money),
    ("campaigns", campaigns._money),
    ("budget", budget._money),
)
"""Renderers that round a stored amount when they word it, as the web client's ``formatMoney``
rounds it in the cell."""

ALL_RENDERERS: tuple[tuple[str, Renderer], ...] = (
    *RENDER_TIME_RENDERERS,
    ("keywords", _keywords_money),
)


def _cases(
    renderers: tuple[tuple[str, Renderer], ...], amounts: tuple[tuple[str, str, str], ...]
) -> list[object]:
    return [
        pytest.param(render, amount, currency, expected, id=f"{name}-{currency}-{amount}")
        for name, render in renderers
        for amount, currency, expected in amounts
    ]


@pytest.mark.parametrize(
    ("render", "amount", "currency", "expected"), _cases(ALL_RENDERERS, ROUNDED_PARITY)
)
def test_every_reason_renderer_rounds_to_the_iso_minor_unit(
    render: Renderer, amount: str, currency: str, expected: str
) -> None:
    assert render(Decimal(amount), currency) == expected


@pytest.mark.parametrize(
    ("render", "amount", "currency", "expected"), _cases(ALL_RENDERERS, GROUPED_PARITY)
)
def test_every_reason_renderer_groups_thousands_at_the_iso_minor_unit(
    render: Renderer, amount: str, currency: str, expected: str
) -> None:
    assert render(Decimal(amount), currency) == expected


def test_keyword_money_is_rounded_once_before_its_reason_and_metric_cite_it() -> None:
    """K1-K3 store the minor-unit value and word that stored value, never the raw amount.

    The evidence metric and the reason therefore cite one figure, and the web cell, formatting
    the stored value, has no tie left to round. The tie itself rounds half up, as every other
    rule rounds it: 89.225 is stored as 89.23 and worded "USD 89.23" on every card.
    """
    stored = keywords._money(Decimal("89.225"), "USD")
    assert stored == Decimal("89.23")
    assert keywords._money_text(stored, "USD") == "USD 89.23"
    assert keywords._money_text(stored, "USD") == campaigns._money(Decimal("89.225"), "USD")


TINY = Fraction(1, 10**70)
"""Far below the 60 significant digits a decimal quotient keeps."""


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        pytest.param(Fraction(89_225, 1_000), "USD", Decimal("89.23"), id="usd-tie"),
        pytest.param(Fraction(89_225, 1_000) - TINY, "USD", Decimal("89.22"), id="usd-below-tie"),
        pytest.param(Fraction(89_225, 1_000) + TINY, "USD", Decimal("89.23"), id="usd-above-tie"),
        pytest.param(-Fraction(89_225, 1_000), "USD", Decimal("-89.23"), id="usd-negative-tie"),
        pytest.param(
            TINY - Fraction(89_225, 1_000), "USD", Decimal("-89.22"), id="usd-negative-inside"
        ),
        pytest.param(Fraction(2, 3), "USD", Decimal("0.67"), id="usd-repeating"),
        pytest.param(Fraction(5, 2), "JPY", Decimal("3"), id="jpy-tie"),
        pytest.param(Fraction(5, 2) - TINY, "JPY", Decimal("2"), id="jpy-below-tie"),
        pytest.param(Fraction(1, 1_600), "KWD", Decimal("0.001"), id="kwd-above-half"),
        pytest.param(Fraction(1, 2_000), "KWD", Decimal("0.001"), id="kwd-tie"),
        pytest.param(Fraction(1, 2_000) - TINY, "KWD", Decimal("0.000"), id="kwd-below-tie"),
    ],
)
def test_keyword_money_decides_a_tie_on_the_exact_amount(
    amount: Fraction, currency: str, expected: Decimal
) -> None:
    """A ``Fraction`` (a cost/conversion, a scaled floor) rounds as its exact value does.

    A quotient within half a unit of its 60th significant digit of a tie is not the tie: it
    rounds to the nearer minor unit, and only an exact tie rounds half up, away from zero.
    """
    shown = keywords._money(amount, currency)
    assert shown == expected
    assert shown.as_tuple().exponent == -minor_unit_exponent(currency)


@pytest.mark.parametrize(
    ("currency", "places"),
    [
        ("USD", 2),
        ("EUR", 2),
        ("JPY", 0),
        ("KWD", 3),
        ("IQD", 3),
        ("AFN", 2),
        ("ALL", 2),
        ("XYZ", 2),
    ],
)
def test_white_paper_money_has_the_places_decisions_are_validated_to(
    currency: str, places: int
) -> None:
    assert minor_unit_exponent(currency) == places
    figure = white_papers._money(Decimal("30"), currency).removeprefix(f"{currency} ")
    shown = figure.partition(".")[2]
    assert len(shown) == places


@pytest.mark.parametrize(
    ("cost", "currency", "expected"),
    [
        pytest.param(CostValue(Decimal("12.3456")), "USD", "USD 12.35", id="usd"),
        pytest.param(CostValue(Decimal("12.3456")), "JPY", "JPY 12", id="jpy"),
        pytest.param(CostValue(Decimal("12.3456"), ceiling=True), "KWD", "≤ KWD 12.346", id="kwd"),
        pytest.param(CostValue(Decimal("12.3456")), "IQD", "IQD 12.346", id="iqd"),
        pytest.param(CostValue(Decimal("12.3456")), "AFN", "AFN 12.35", id="afn"),
        pytest.param(CostValue(Decimal("89.225")), "USD", "USD 89.23", id="usd-tie"),
        pytest.param(
            CostValue(Decimal("2766"), ceiling=True), "USD", "≤ USD 2,766.00", id="usd-grouped"
        ),
        pytest.param(CostValue(None, note="no QL yet"), "IQD", "— (no QL yet)", id="no-ql"),
        pytest.param(CostValue(None, note="x", unavailable=True), "IQD", "unavailable", id="na"),
    ],
)
def test_white_paper_cost_text_uses_the_currency_minor_unit(
    cost: CostValue, currency: str, expected: str
) -> None:
    assert white_papers._cost_text(cost, currency) == expected


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        ("0.125", "USD", "USD 0.13"),
        ("0.0125", "IQD", "IQD 0.013"),
        ("2.5", "JPY", "JPY 3"),
        ("-2.5", "JPY", "JPY -3"),
    ],
)
def test_white_paper_money_rounds_half_up_at_the_minor_unit_as_campaigns_and_budget_do(
    amount: str, currency: str, expected: str
) -> None:
    value = Decimal(amount)
    assert white_papers._money(value, currency) == expected
    assert campaigns._money(value, currency) == expected
    assert budget._money(value, currency) == expected
