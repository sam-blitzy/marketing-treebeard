"""Unit tests for the samples and thresholds in the evidence of white-paper items.

AAP 0.5.4.2 P6: a pattern below its minimum is listed under ``withheld`` with its observed and
required sizes and never becomes an item, so no white-paper candidate carries an
``evidence.sample`` it falls short of. The rows AAP 0.5.4.7 requires stay. A row with fewer
Qualified Leads than ``w.min_ql`` is "No change warranted", with its QL shortfall in
``extra.w1_sample``. It also gets a W1 withheld line when its cost per QL is within the
double-down ceiling. Every row of a cohort under ``w.cohort_min_total_ql`` is "Insufficient
data", with the cohort shortfall in ``extra.cohort_sample`` beside the cohort-wide withheld line.

AAP 0.5.4 and 0.5.4.8: an item's ``evidence.thresholds`` holds the values it applied. W1 runs G1
(:func:`~ads_engine.analysis.metrics.spend_increase_blocked`) over its contributing campaigns.
The W1 item, and the row whose W1 G1 withholds, therefore also carry
:data:`~ads_engine.analysis.metrics.G1_THRESHOLD_KEYS`. A row that never ran G1 carries exactly
:data:`~ads_engine.analysis.white_papers.THRESHOLD_KEYS`.

Contexts come from the scenario catalogue (``SCENARIOS[name].build_context()``) and from worlds
built with its white-paper helpers. Threshold values are read from ``ctx.thresholds``. Pure: no
database and no network.
"""

from collections.abc import Callable, Mapping
from decimal import Decimal
from functools import cache
from typing import Final

import pytest

from ads_engine.analysis import white_papers
from ads_engine.analysis.attribution import attribution_for, bounded_decimal
from ads_engine.analysis.context import (
    W1,
    AnalysisContext,
    Candidate,
    RuleResult,
    W,
    Withheld,
)
from ads_engine.analysis.metrics import (
    B2_THRESHOLD_KEYS,
    BUDGET_THRESHOLD_KEYS,
    G1_THRESHOLD_KEYS,
    spend_increase_blocked,
)
from ads_engine.analysis.white_papers import (
    ACTION_INSUFFICIENT_DATA,
    ACTION_NO_CHANGE,
    THRESHOLD_KEYS,
    W1_ALL_BLOCKED_REASON,
)
from ads_engine.simulation import scenarios as sc
from ads_engine.simulation.scenarios import SCENARIOS, Scenario

LOW_QL: Final = "w.low_ql_within_ceiling.case"
"""Guide A-C at cost/QL 10, 30 and 50 (5 QL each); Guide D with 1 QL at cost/QL 5 (median 20)."""

LOW_QL_ABOVE: Final = "w.low_ql_above_ceiling.case"
"""Guide A-C as in :data:`LOW_QL`; Guide D with 2 QL at cost/QL 50 (median 40, ceiling 32)."""

INSUFFICIENT: Final = "w.insufficient_cohort.case"
"""Guide A with 2 QL and Guide B with none: 2 Qualified Leads across the cohort."""

ALL_BLOCKED: Final = "w1.all_blocked.case"
"""Guide A's only campaign is the high-utilization, poor-efficiency one G1 blocks."""

NO_CAMPAIGN: Final = "w1.no_campaign.case"
"""Guide A has 5 QL and no window spend, so no campaign feeds it."""

CATALOGUE: Final = (
    "w1.unblocked_only.case",
    "w1.cohort.fires",
    "w1.cohort.does_not_fire",
    "w2.cohort.fires",
    "w2.cohort.does_not_fire",
)
CUSTOM: Final = (LOW_QL, LOW_QL_ABOVE, INSUFFICIENT, ALL_BLOCKED, NO_CAMPAIGN)

_FIVE: Final = dict.fromkeys(sc._offsets(-28, -24), 1)
"""Five leads, one a day from -28 to -24."""

_WITH_G1: Final = (*THRESHOLD_KEYS, *G1_THRESHOLD_KEYS)
"""The threshold keys of an item whose decision ran G1, in evidence order."""


def _paper(key: str, slug: str, cost: str, leads: Mapping[int, int] = _FIVE) -> sc._Paper:
    """A white paper creative on the catalogue's default campaign."""
    return sc._Paper(key, sc._PAPER_FORMS[slug], cost, leads)


def _custom(name: str) -> Scenario:
    """Return the custom scenario ``name`` (one of :data:`CUSTOM`)."""
    world: tuple[sc._Paper, ...]
    ql = ("cr-a", "cr-b", "cr-c", "cr-d")
    assets = sc._PAPERS
    hours: dict[str, int] = {}
    if name == LOW_QL:
        world = (
            _paper("cr-a", "guide-a", "50"),
            _paper("cr-b", "guide-b", "150"),
            _paper("cr-c", "guide-c", "250"),
            _paper("cr-d", "guide-d", "5", {-28: 1}),
        )
    elif name == LOW_QL_ABOVE:
        world = (
            _paper("cr-a", "guide-a", "50"),
            _paper("cr-b", "guide-b", "150"),
            _paper("cr-c", "guide-c", "250"),
            _paper("cr-d", "guide-d", "100", {-28: 1, -27: 1}),
        )
    elif name == INSUFFICIENT:
        world = (
            _paper("cr-a", "guide-a", "50", {-28: 1, -27: 1}),
            _paper("cr-b", "guide-b", "150", {-28: 1}),
        )
        ql = ("cr-a",)
        assets = sc._PAPERS[:2]
    elif name == ALL_BLOCKED:
        # The w1.unblocked_only world without Guide A's unblocked creative: the cheap campaign's
        # leads make the account baseline low, so the capped "Hot" campaign is poor-efficiency.
        world = (
            sc._Paper(
                "cr-a-hot",
                sc._PAPER_FORMS["guide-a"],
                "2716",
                dict.fromkeys(sc._offsets(-28, -3), 1),
                campaign="li-hot",
            ),
            _paper("cr-b", "guide-b", "750"),
            _paper("cr-c", "guide-c", "1250"),
            sc._Paper(
                "cr-cheap",
                sc._CHEAP_FORM,
                "500",
                dict.fromkeys(sc._WINDOW, 9) | {-1: 7},
                campaign="li-cheap",
            ),
        )
        ql = ("cr-a-hot", "cr-b", "cr-c")
        hours = {"cr-a-hot": 24}
        assets = sc._PAPERS[:3]
    elif name == NO_CAMPAIGN:
        world = (
            _paper("cr-a", "guide-a", "0"),
            _paper("cr-b", "guide-b", "150"),
            _paper("cr-c", "guide-c", "250"),
            _paper("cr-d", "guide-d", "300"),
        )
        ql = ("cr-a", "cr-b", "cr-c")
    else:
        raise KeyError(name)
    patches = tuple(sc._ql_after(key, hours.get(key, 48)) for key in ql)
    return Scenario(
        name,
        sc.MODULE_WHITE_PAPERS,
        "White-paper evidence: samples only for passed gates, G1 keys where G1 ran",
        "case",
        None,
        sc._papers_world(name, world),
        sc._papers_context(*patches, assets=assets),
        SCENARIOS["w2.cohort.fires"].expect,
    )


@cache
def _evaluated(name: str) -> tuple[AnalysisContext, RuleResult]:
    """Build the context of ``name`` (catalogue or custom) and evaluate the W rules on it."""
    scenario = SCENARIOS[name] if name in SCENARIOS else _custom(name)
    ctx = scenario.build_context()
    return ctx, white_papers.evaluate(ctx)


def _item(result: RuleResult, slug: str) -> Candidate:
    """Return the single item of the asset ``slug``."""
    items = [item for item in result.candidates if item.evidence.extra["asset"]["slug"] == slug]
    assert len(items) == 1
    return items[0]


def _withheld_for(result: RuleResult, item: Candidate) -> list[Withheld]:
    """Return the withheld lines about ``item``'s asset."""
    return [line for line in result.withheld if line.entity == item.entity_ref]


def _cost_per_ql(item: Candidate) -> Decimal:
    """Return the item's known cost per Qualified Lead."""
    value = next(metric.value for metric in item.evidence.metrics if metric.key == "cost_per_ql")
    assert isinstance(value, Decimal)
    return value


@pytest.mark.parametrize("name", [*CATALOGUE, *CUSTOM])
def test_no_candidate_carries_a_failed_sample(name: str) -> None:
    """P6: every item's ``evidence.sample`` is a gate it passed, or absent."""
    _, result = _evaluated(name)
    assert result.candidates
    failed = [
        (item.entity_ref.name, item.evidence.sample)
        for item in result.candidates
        if item.evidence.sample is not None
        and item.evidence.sample.observed < item.evidence.sample.required
    ]
    assert failed == []


@pytest.mark.parametrize("name", [*CATALOGUE, *CUSTOM])
def test_no_white_paper_row_names_a_headline_metric(name: str) -> None:
    """White-paper items are table rows with fixed columns, not cards: no headline number."""
    _, result = _evaluated(name)
    assert result.candidates
    assert all("headline_metric" not in item.evidence.extra for item in result.candidates)


@pytest.mark.parametrize("name", [*CATALOGUE, *CUSTOM])
def test_every_item_records_the_configured_values_it_applied(name: str) -> None:
    """``evidence.thresholds`` is the configured value of every key it lists, the W keys first."""
    ctx, result = _evaluated(name)
    for item in result.candidates:
        thresholds = item.evidence.thresholds
        assert tuple(thresholds)[: len(THRESHOLD_KEYS)] == THRESHOLD_KEYS
        assert item.evidence.to_json()["thresholds"] == ctx.thresholds.subset(*thresholds)


def test_low_ql_row_within_the_ceiling_withholds_w1_with_its_shortfall() -> None:
    """1 QL at a cost per QL within the ceiling: no change, no sample, a W1 withheld line."""
    ctx, result = _evaluated(LOW_QL)
    min_ql = ctx.thresholds.int("w.min_ql")
    item = _item(result, "guide-d")
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.evidence.sample is None
    assert item.evidence.extra["w1_sample"] == {
        "observed": 1,
        "required": min_ql,
        "unit": "Qualified Leads",
    }
    assert _cost_per_ql(item) <= item.evidence.extra["cohort"]["double_down_ceiling"]
    lines = _withheld_for(result, item)
    assert [(line.rule_id, line.observed, line.required, line.unit) for line in lines] == [
        (W1, 1, min_ql, "Qualified Leads")
    ]
    assert lines[0].reason == f"1 Qualified Lead of the {min_ql} W1 needs"
    assert item.evidence.extra["withheld_reasons"] == (lines[0].reason,)
    assert tuple(item.evidence.thresholds) == THRESHOLD_KEYS


def test_low_ql_row_above_the_ceiling_withholds_nothing() -> None:
    """2 QL at a cost per QL above the ceiling: no change, no sample and no W1 withheld line."""
    ctx, result = _evaluated(LOW_QL_ABOVE)
    item = _item(result, "guide-d")
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.evidence.sample is None
    assert item.evidence.extra["w1_sample"] == {
        "observed": 2,
        "required": ctx.thresholds.int("w.min_ql"),
        "unit": "Qualified Leads",
    }
    assert _cost_per_ql(item) > item.evidence.extra["cohort"]["double_down_ceiling"]
    assert _withheld_for(result, item) == []
    assert "withheld_reasons" not in item.evidence.extra


def test_insufficient_cohort_rows_carry_the_shortfall_without_a_sample() -> None:
    """Cohort QL 2 of 3: every row "Insufficient data", no sample, one cohort withheld line."""
    ctx, result = _evaluated(INSUFFICIENT)
    required = ctx.thresholds.int("w.cohort_min_total_ql")
    assert len(result.candidates) == 2
    for item in result.candidates:
        assert (item.rule_id, item.action) == (W, ACTION_INSUFFICIENT_DATA)
        assert item.evidence.sample is None
        assert item.evidence.extra["cohort_sample"] == {
            "observed": 2,
            "required": required,
            "unit": "Qualified Leads across the cohort's assets",
        }
        assert tuple(item.evidence.thresholds) == THRESHOLD_KEYS
    cohort_lines = [line for line in result.withheld if line.entity is None]
    assert [(line.rule_id, line.observed, line.required, line.unit) for line in cohort_lines] == [
        (W, 2, required, "Qualified Leads")
    ]
    assert [line for line in result.withheld if line.entity is not None] == []


def test_g1_key_contract_is_the_budget_gate_and_b2_keys() -> None:
    """G1 applies the budget-history gate keys and the B2 efficiency keys, and nothing else."""
    assert (*BUDGET_THRESHOLD_KEYS, *B2_THRESHOLD_KEYS) == G1_THRESHOLD_KEYS
    assert {"b2.poor_efficiency_ratio", "b2.min_expected_outcomes"} <= set(G1_THRESHOLD_KEYS)
    assert all(key.startswith(("budget.", "b2.")) for key in G1_THRESHOLD_KEYS)


@pytest.mark.parametrize("name", ["w1.unblocked_only.case", "w1.cohort.fires", LOW_QL])
def test_w1_item_records_the_g1_keys(name: str) -> None:
    """A W1 double down records the values of every key G1 applied to its campaigns."""
    ctx, result = _evaluated(name)
    items = [item for item in result.candidates if item.rule_id == W1]
    assert items
    for item in items:
        thresholds = item.evidence.thresholds
        assert set(G1_THRESHOLD_KEYS) <= set(thresholds)
        assert {"b2.poor_efficiency_ratio", "b2.min_expected_outcomes"} <= set(thresholds)
        assert set(BUDGET_THRESHOLD_KEYS) <= set(thresholds)
        assert tuple(thresholds) == _WITH_G1
        assert item.evidence.to_json()["thresholds"] == ctx.thresholds.subset(*_WITH_G1)


def test_w1_left_out_campaign_is_explained_by_recorded_g1_keys() -> None:
    """The campaign W1 leaves out is G1-blocked, and the W1 item records the G1 keys."""
    ctx, result = _evaluated("w1.unblocked_only.case")
    item = _item(result, "guide-a")
    assert item.rule_id == W1
    left_out = item.evidence.extra["blocked_campaigns"]
    assert [campaign["name"] for campaign in left_out] == ["Hot - Lead gen"]
    blocked = spend_increase_blocked(ctx)
    assert all(("campaign", campaign["id"]) in blocked for campaign in left_out)
    assert tuple(item.evidence.thresholds) == _WITH_G1


def test_all_blocked_no_change_records_the_g1_keys() -> None:
    """W1 withheld because G1 blocks every contributing campaign: the G1 keys are recorded."""
    ctx, result = _evaluated(ALL_BLOCKED)
    item = _item(result, "guide-a")
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    assert item.evidence.extra["double_down_campaigns"] == ()
    left_out = item.evidence.extra["blocked_campaigns"]
    assert [campaign["name"] for campaign in left_out] == ["Hot - Lead gen"]
    assert all(("campaign", campaign["id"]) in spend_increase_blocked(ctx) for campaign in left_out)
    lines = _withheld_for(result, item)
    assert [(line.rule_id, line.reason) for line in lines] == [(W1, W1_ALL_BLOCKED_REASON)]
    sample = item.evidence.sample
    assert sample is not None
    assert sample.observed >= sample.required
    assert tuple(item.evidence.thresholds) == _WITH_G1
    assert item.evidence.to_json()["thresholds"] == ctx.thresholds.subset(*_WITH_G1)


_NEVER_RAN_G1: Final[tuple[tuple[str, str, Callable[[Candidate], bool]], ...]] = (
    (LOW_QL, "guide-d", lambda item: "w1_sample" in item.evidence.extra),
    (LOW_QL_ABOVE, "guide-d", lambda item: "w1_sample" in item.evidence.extra),
    ("w1.cohort.does_not_fire", "guide-a", lambda item: item.action == ACTION_NO_CHANGE),
    ("w1.cohort.fires", "guide-b", lambda item: item.action == ACTION_NO_CHANGE),
    ("w1.cohort.fires", "guide-d", lambda item: item.rule_id != W1),
    ("w2.cohort.does_not_fire", "guide-d", lambda item: item.action == ACTION_NO_CHANGE),
    (INSUFFICIENT, "guide-a", lambda item: item.action == ACTION_INSUFFICIENT_DATA),
    (INSUFFICIENT, "guide-b", lambda item: item.action == ACTION_INSUFFICIENT_DATA),
    (NO_CAMPAIGN, "guide-a", lambda item: item.evidence.extra["campaigns"] == ()),
)
"""Rows whose decision never ran G1, with a check that the row is the intended case.

QL below ``w.min_ql`` (within and above the ceiling), cost per QL above the ceiling, W2 and its
zero-QL "No change warranted", every "Insufficient data" row, and a W1 pattern with no
contributing campaign.
"""


@pytest.mark.parametrize(("name", "slug", "is_case"), _NEVER_RAN_G1)
def test_rows_that_never_ran_g1_carry_exactly_the_w_keys(
    name: str, slug: str, is_case: Callable[[Candidate], bool]
) -> None:
    """A row G1 never judged records exactly :data:`THRESHOLD_KEYS`, no ``budget.*`` or ``b2.*``."""
    ctx, result = _evaluated(name)
    item = _item(result, slug)
    assert is_case(item)
    assert item.rule_id != W1
    assert tuple(item.evidence.thresholds) == THRESHOLD_KEYS
    assert item.evidence.to_json()["thresholds"] == ctx.thresholds.subset(*THRESHOLD_KEYS)


def test_no_contributing_campaign_withholds_w1_without_g1_keys() -> None:
    """A W1 pattern fed by no campaign is withheld; G1 judged no campaign, so no G1 keys."""
    _, result = _evaluated(NO_CAMPAIGN)
    item = _item(result, "guide-a")
    assert (item.rule_id, item.action) == (W, ACTION_NO_CHANGE)
    lines = _withheld_for(result, item)
    assert [(line.rule_id, line.reason) for line in lines] == [
        (W1, "no campaign with window spend feeds this white paper")
    ]
    assert not set(G1_THRESHOLD_KEYS) & set(item.evidence.thresholds)


def _places(text: str) -> int:
    """Return the number of decimals a serialised amount or share carries."""
    exponent = Decimal(text).as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


def _cited(value: Decimal | None) -> str | None:
    """Return the serialised form of an exact value as every evidence field cites it."""
    return None if value is None else format(bounded_decimal(value), "f")


@pytest.mark.parametrize("name", [*CATALOGUE, *CUSTOM])
def test_every_cited_cost_and_share_carries_one_value_at_six_decimals(name: str) -> None:
    """AAP 0.5.4.8: a number cited in a metric and in ``extra`` is one value (``numeric(18, 6)``).

    The cost metrics equal ``extra.costs``; each ``extra.rows`` cost, each completeness share and
    the orphan-deal share is its exact value bounded by
    :func:`~ads_engine.analysis.attribution.bounded_decimal`, so none carries a 28-digit
    quotient.
    """
    ctx, result = _evaluated(name)
    table = attribution_for(ctx)
    rows = {row.external_id: row for row in table.rows()}
    for item in result.candidates:
        rendered = item.evidence.to_json()
        metrics = {metric["key"]: metric["value"] for metric in rendered["metrics"]}
        extra = rendered["extra"]
        row = rows[item.entity_ref.external_id]
        for key, cost in (
            ("cost_per_ql", row.cost_per_ql),
            ("cost_per_meeting", row.cost_per_meeting),
        ):
            cited = extra["costs"][key]["value"]
            assert cited == _cited(cost.value)
            if cost.value is not None:
                assert metrics[key] == cited
        assert len(extra["rows"]) == len(row.creative_rows)
        for credit, serialised in zip(row.creative_rows, extra["rows"], strict=True):
            assert serialised["cost_per_ql"]["value"] == _cited(credit.cost_per_ql.value)
            assert serialised["cost_per_meeting"]["value"] == _cited(credit.cost_per_meeting.value)
        assert extra["orphan_deals"]["share"] == _cited(table.orphan_deals.share)
        numbers = [
            *(extra["costs"][key]["value"] for key in ("cost_per_ql", "cost_per_meeting")),
            *(
                credit[key]["value"]
                for credit in extra["rows"]
                for key in ("cost_per_ql", "cost_per_meeting")
            ),
            *(share["share"] for share in extra["shares"].values()),
            extra["orphan_deals"]["share"],
        ]
        assert all(_places(text) <= 6 for text in numbers if text is not None)


def test_a_non_terminating_cost_is_cited_at_one_precision() -> None:
    """Guide A: USD 2,766 over 31 QL is cited as 89.225806 in the metric and in ``extra``.

    Its second creative's USD 2,716 over 26 QL is cited as 104.461538, never as the exact
    quotient; the display and every comparison keep the exact value.
    """
    ctx, result = _evaluated("w1.unblocked_only.case")
    item = _item(result, "guide-a")
    rendered = item.evidence.to_json()
    metric = next(m for m in rendered["metrics"] if m["key"] == "cost_per_ql")
    assert _cost_per_ql(item) == Decimal("89.225806")
    assert metric["value"] == rendered["extra"]["costs"]["cost_per_ql"]["value"] == "89.225806"
    assert [credit["cost_per_ql"]["value"] for credit in rendered["extra"]["rows"]] == [
        "10.00",
        "104.461538",
    ]
    (row,) = (row for row in attribution_for(ctx).rows() if row.slug == "guide-a")
    assert row.cost_per_ql.value == Decimal(2766) / Decimal(31)
    assert row.cost_per_ql.render() == "89.23"
