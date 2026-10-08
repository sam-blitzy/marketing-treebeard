"""Unit tests for W2 Retire or rework against a zero cohort median cost per Qualified Lead.

AAP 0.5.4.7 W2 fires only when QL = 0 and spend / median cost per QL >= 3. A cohort whose assets
with QL had no window spend has a median cost per QL of 0, so that ratio is undefined: the
comparison W2 needs is unavailable, and "spend >= 3 x 0" must never retire a paid zero-QL row.
These cases pin the zero-median row to "No change warranted" with one W2 withheld line (observed
unavailable, required ``w.retire_spend_multiple``), no fabricated ratio or retirement threshold
and no failed sample on the item, and keep W2 firing / not firing on the catalogue's
``w2.cohort`` pair (spend 10 x / 2 x a median of 30).

Contexts come from the scenario catalogue: the ``w2.cohort`` pair, and the same four-asset
cohort with Guide A-C at zero window spend (5 QL each) and Guide D at the given spend with no QL.
Pure: no database and no network.
"""

from decimal import Decimal
from typing import Any, Final

import pytest

from ads_engine.analysis import white_papers
from ads_engine.analysis.context import (
    W2,
    AnalysisContext,
    Candidate,
    RuleResult,
    W,
    Withheld,
    json_value,
)
from ads_engine.analysis.white_papers import ACTION_NO_CHANGE, ACTION_RETIRE
from ads_engine.simulation import scenarios as sc
from ads_engine.simulation.scenarios import SCENARIOS, Scenario

_UNAVAILABLE: Final = "spend-to-median comparison unavailable"
_UNIT: Final = "x cohort median cost per Qualified Lead"
_TARGET: Final = "guide-d"
_LONG_WINDOW_DAYS: Final = 60
"""A ``w.retire_min_window_days`` above the catalogue's 28-day window, failing W2's guard."""


def _zero_median_context(d_cost: str, **overrides: object) -> AnalysisContext:
    """Guide A-C with no window spend and 5 QL each (median 0); Guide D at ``d_cost``, no QL."""
    five = dict.fromkeys(sc._offsets(-28, -24), 1)
    papers = (
        sc._Paper("cr-a", sc._PAPER_FORMS["guide-a"], "0", five),
        sc._Paper("cr-b", sc._PAPER_FORMS["guide-b"], "0", five),
        sc._Paper("cr-c", sc._PAPER_FORMS["guide-c"], "0", five),
        sc._Paper("cr-d", sc._PAPER_FORMS[_TARGET], d_cost, five),
    )
    context = sc._COHORT_CONTEXT
    if overrides:
        context = sc._papers_context(
            sc._ql_after("cr-a"),
            sc._ql_after("cr-b"),
            sc._ql_after("cr-c"),
            assets=sc._PAPERS,
            threshold_overrides=overrides,
        )
    scenario = Scenario(
        "w2.zero_median.case",
        sc.MODULE_WHITE_PAPERS,
        "W2 is withheld, never fired, when the cohort median cost per Qualified Lead is 0",
        "case",
        None,
        sc._papers_world("w.cohort", papers),
        context,
        SCENARIOS["w2.cohort.fires"].expect,
    )
    return scenario.build_context()


def _item(result: RuleResult, slug: str) -> Candidate:
    """Return the single item of the asset ``slug``."""
    items = [item for item in result.candidates if item.evidence.extra["asset"]["slug"] == slug]
    assert len(items) == 1
    return items[0]


def _w2_withheld(result: RuleResult, item: Candidate) -> list[Withheld]:
    """Return the W2 lines withheld for ``item``'s asset."""
    return [
        line for line in result.withheld if line.rule_id == W2 and line.entity == item.entity_ref
    ]


def _metrics(item: Candidate) -> dict[str, Any]:
    """Return the item's metric values by key."""
    return {metric.key: metric.value for metric in item.evidence.metrics}


def _retire_multiple(ctx: AnalysisContext) -> Decimal:
    """Return the configured ``w.retire_spend_multiple``."""
    return ctx.thresholds.ratio("w.retire_spend_multiple")


def test_zero_median_paid_row_withholds_w2_as_comparison_unavailable() -> None:
    ctx = _zero_median_context("1")
    result = white_papers.evaluate(ctx)

    assert [item.rule_id for item in result.candidates if item.rule_id == W2] == []
    item = _item(result, _TARGET)
    assert item.rule_id == W
    assert item.action == ACTION_NO_CHANGE
    assert item.evidence.sample is None
    extra = item.evidence.extra
    assert extra["spend_to_median"] is None
    cohort = extra["cohort"]
    assert cohort["median_cost_per_ql"] == 0
    assert cohort["retire_spend_floor"] is None
    assert item.reason == (
        "No Qualified Lead yet after USD 1.00 of spend, but W2 is withheld: "
        f"{_UNAVAILABLE}: the USD cohort median cost per Qualified Lead is USD 0.00."
    )
    # No retirement threshold of "multiple x 0" is shown anywhere.
    assert "retires at" not in item.reason

    lines = _w2_withheld(result, item)
    assert len(lines) == 1
    line = lines[0]
    assert line.observed is None
    assert line.required == _retire_multiple(ctx)
    assert line.unit == _UNIT
    assert line.reason.startswith(_UNAVAILABLE)
    assert extra["withheld_reasons"] == (line.reason,)
    assert line.to_json()["sample"] == {
        "observed": None,
        "required": json_value(_retire_multiple(ctx)),
        "unit": _UNIT,
    }

    # The row stays readable: its numbers are unchanged by the withholding.
    metrics = _metrics(item)
    assert metrics["spend"] == Decimal(1)
    assert metrics["ql"] == 0
    assert metrics["cost_per_ql"] == "—"


def test_zero_median_skips_the_w2_guards() -> None:
    """A failing window guard is not reported: the comparison it would guard does not exist."""
    ctx = _zero_median_context("1", **{"w.retire_min_window_days": _LONG_WINDOW_DAYS})
    result = white_papers.evaluate(ctx)

    item = _item(result, _TARGET)
    assert item.rule_id == W
    lines = _w2_withheld(result, item)
    assert [line.reason.startswith(_UNAVAILABLE) for line in lines] == [True]
    assert "window of" not in item.reason


def test_positive_median_still_reports_a_failing_w2_guard() -> None:
    """The twin of the guard case: with a median of 30 the same override withholds W2."""
    scenario = SCENARIOS["w2.cohort.fires"]
    context = sc._papers_context(
        sc._ql_after("cr-a"),
        sc._ql_after("cr-b"),
        sc._ql_after("cr-c"),
        assets=sc._PAPERS,
        threshold_overrides={"w.retire_min_window_days": _LONG_WINDOW_DAYS},
    )
    ctx = Scenario(
        "w2.cohort.guarded",
        scenario.module,
        scenario.guarantee,
        "case",
        None,
        scenario.spec,
        context,
        scenario.expect,
    ).build_context()
    result = white_papers.evaluate(ctx)

    item = _item(result, _TARGET)
    assert item.rule_id == W
    lines = _w2_withheld(result, item)
    assert [line.reason for line in lines] == [
        f"window of 28 days is below the {_LONG_WINDOW_DAYS} days W2 needs"
    ]
    assert item.evidence.extra["spend_to_median"] == 10


def test_zero_median_zero_spend_row_has_no_spend_reason() -> None:
    result = white_papers.evaluate(_zero_median_context("0"))

    assert [item.rule_id for item in result.candidates if item.rule_id == W2] == []
    item = _item(result, _TARGET)
    assert item.rule_id == W
    assert item.action == ACTION_NO_CHANGE
    assert item.reason == "No spend and no Qualified Lead in the window."
    assert item.evidence.sample is None
    assert item.evidence.extra["spend_to_median"] is None
    assert item.evidence.extra["cohort"]["retire_spend_floor"] is None
    assert _w2_withheld(result, item) == []


@pytest.mark.parametrize(
    ("name", "rule_id", "action", "ratio"),
    [
        ("w2.cohort.fires", W2, ACTION_RETIRE, 10),
        ("w2.cohort.does_not_fire", W, ACTION_NO_CHANGE, 2),
    ],
)
def test_positive_median_w2_fires_at_10x_and_not_at_2x(
    name: str, rule_id: str, action: str, ratio: int
) -> None:
    ctx = SCENARIOS[name].build_context()
    result = white_papers.evaluate(ctx)

    item = _item(result, _TARGET)
    assert item.rule_id == rule_id
    assert item.action == action
    extra = item.evidence.extra
    assert extra["spend_to_median"] == ratio
    median = extra["cohort"]["median_cost_per_ql"]
    assert median == 30
    assert extra["cohort"]["retire_spend_floor"] == _retire_multiple(ctx) * median
    assert _w2_withheld(result, item) == []
    assert _UNAVAILABLE not in item.reason
