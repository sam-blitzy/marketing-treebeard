"""Unit tests for ``ads_engine.analysis.context``: the rule contract and its shared types.

They pin what every rule, ``precedence.py``, ``runner.py``, ``overlay.py`` and the simulation
scenarios rely on: the constants equal the model enumerations and the AAP 0.5.3 codes; every
output type renders exactly the AAP 0.5.3 JSON (Decimals as fixed-point strings, dates as ISO
strings); validation rejects contract violations at construction; ``RuleResult.merge`` sums
suppression counts; and ``AnalysisContext.make_evidence`` records the window, thresholds,
dataset statuses and frozen provenance per the effective CRM dataset. Pure: no database and
no network.
"""

import dataclasses
import doctest
import itertools
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis import context as ctx_module
from ads_engine.analysis.context import (
    ALERT_BANNERS,
    BANNER_ORDER,
    BANNER_TONES,
    C0,
    C1,
    C1B,
    C3A,
    C3B,
    CAMPAIGNS,
    FEEDBACK_SUPPRESSION,
    FINDING_CODES,
    FLAG_CODES,
    FLAG_NO_CRM_LINK,
    FULL_STATE_DATASETS,
    GROUP_ORDER,
    GROUP_TITLES,
    K1,
    KEYWORDS,
    RULE_IDS,
    SECTION_ORDER,
    SECTION_TITLES,
    SUPPRESSION_REASONS,
    TONES,
    AccountInfo,
    AnalysisContext,
    AssociationFetchInfo,
    Baseline,
    BudgetInfo,
    CampaignInfo,
    Candidate,
    ContactInfo,
    CrmData,
    DatasetRef,
    DatasetStatus,
    DayFact,
    DecisionSubject,
    DiscrepancyInfo,
    EntityRef,
    Evidence,
    FeedbackInfo,
    Finding,
    Flag,
    Freshness,
    LeadInfo,
    Metric,
    Observation,
    Projection,
    ProvenanceEntry,
    RateBaseline,
    RuleResult,
    Sample,
    TestResult,
    W,
    Withheld,
    cohort_group,
    cohort_group_currency,
    content_asset_external_id,
    creative_group,
    creative_group_campaign,
    creative_pair_external_id,
    google_link_gap,
    json_value,
    keyword_external_id,
    no_crm_link_flag,
    objective_group,
    search_term_external_id,
    segment_external_id,
    term_cluster_external_id,
    unverified_message,
)
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding, Thresholds
from ads_engine.analysis.window import AnalysisWindow, SourceDayInfo
from ads_engine.db.models.analysis import DATA_STATUSES, FEEDBACK_KINDS, RECOMMENDATION_SECTIONS
from ads_engine.db.models.ops import DATASETS

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
GOOGLE = DatasetRef(11, "google", "live")
LINKEDIN_SIM = DatasetRef(12, "linkedin", "simulated")
HUBSPOT = DatasetRef(13, "hubspot", "live")
COMPANION = DatasetRef(14, "hubspot", "simulated", "companion")


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


@pytest.fixture(scope="module")
def window(thresholds: Thresholds) -> AnalysisWindow:
    return AnalysisWindow.default(thresholds, SourceDayInfo("America/New_York"), now=NOW)


def _bindings() -> CrmBindings:
    return CrmBindings.unavailable("CRM stage bindings not verified", at=NOW)


def _evidence(**extra: Any) -> Evidence:
    return Evidence(
        window={"start": "2026-09-07", "end": "2026-10-04", "days": 28, "source_days": []},
        metrics=(Metric("spend", "Spend", Decimal("12.340000"), "currency", currency="USD"),),
        extra=extra,
    )


def _entity(name: str = "Brand") -> EntityRef:
    return EntityRef("google", "campaign", 7, "123", name)


def _candidate(**changes: Any) -> Candidate:
    fields: dict[str, Any] = {
        "rule_id": C1,
        "section": CAMPAIGNS,
        "group": "pause",
        "entity_ref": _entity(),
        "action": "Pause",
        "reason": "Spent 12.34 USD on 20 of 28 days with 0 conversions",
        "evidence": _evidence(),
        "rank_key": (Decimal("-12.34"),),
        "flags": (),
        "required_datasets": ("google.metrics_daily",),
        "increases_spend_on": (),
        "decision_subject": DecisionSubject("campaign", 7),
    }
    fields.update(changes)
    return Candidate(**fields)


def _context(thresholds: Thresholds, window: AnalysisWindow, **changes: Any) -> AnalysisContext:
    fields: dict[str, Any] = {
        "window": window,
        "thresholds": thresholds,
        "now": NOW,
        "datasets": {"google": GOOGLE, "linkedin": LINKEDIN_SIM, "hubspot": HUBSPOT},
        "linkedin_crm": CrmData(COMPANION, _bindings()),
        "google_crm": CrmData(HUBSPOT, _bindings()),
    }
    fields.update(changes)
    return AnalysisContext(**fields)


# -- constants -------------------------------------------------------------------------------


def test_section_order_matches_the_model_and_the_aap_order() -> None:
    assert SECTION_ORDER == ("keywords", "campaigns", "budget", "creative", "white_papers")
    assert SECTION_ORDER == RECOMMENDATION_SECTIONS
    assert tuple(SECTION_TITLES) == SECTION_ORDER
    assert tuple(GROUP_ORDER) == SECTION_ORDER
    assert tuple(GROUP_TITLES) == SECTION_ORDER
    for section, groups in GROUP_ORDER.items():
        assert tuple(GROUP_TITLES[section]) == groups
    assert GROUP_ORDER[CAMPAIGNS] == ("discrepancies", "pause", "scale", "launch")
    assert SECTION_TITLES["creative"] == "Copy / creative changes"


def test_status_and_feedback_constants_equal_the_model_enumerations() -> None:
    assert ctx_module.DATA_STATUS_VALUES == DATA_STATUSES
    assert tuple(FEEDBACK_SUPPRESSION) == FEEDBACK_KINDS
    assert set(FEEDBACK_SUPPRESSION.values()) <= set(SUPPRESSION_REASONS)


def test_rule_ids_are_unique_ordered_and_match_threshold_namespaces(
    thresholds: Thresholds,
) -> None:
    assert len(set(RULE_IDS)) == len(RULE_IDS) == 20
    assert RULE_IDS[:6] == ("K1", "K2", "K3", "C0", "C1", "C1b")
    assert RULE_IDS[-3:] == ("W2", "W", "G1")
    assert (C1B, C3A, C3B) == ("C1b", "C3a", "C3b")
    for rule in ("K1", "K2", "K3", "C1", "C1b", "C3a", "C3b", "B1", "B2", "B3", "B4"):
        assert rule.lower() in thresholds.raw


def test_flag_finding_and_banner_codes() -> None:
    assert len(set(FLAG_CODES)) == len(FLAG_CODES) == 15
    assert "shared_budget" in FLAG_CODES
    assert len(set(FINDING_CODES)) == len(FINDING_CODES) == 8
    assert BANNER_ORDER[0] == "open_discrepancies"
    assert BANNER_ORDER[-1] == "short_window"
    assert set(BANNER_TONES) == set(BANNER_ORDER)
    assert set(BANNER_TONES.values()) <= set(TONES)
    assert set(ALERT_BANNERS) == {"open_discrepancies"}
    assert BANNER_TONES["open_discrepancies"] == "critical"
    assert unverified_message(3) == "Unverified while 3 discrepancies are open"


def test_group_keys_round_trip() -> None:
    assert creative_group(42) == "campaign:42"
    assert creative_group_campaign("campaign:42") == 42
    assert creative_group_campaign("pause") is None
    assert creative_group_campaign("campaign:x") is None
    assert cohort_group(" usd ") == "cohort:USD"
    assert cohort_group_currency("cohort:USD") == "USD"
    assert cohort_group_currency("campaign:1") is None
    with pytest.raises(TypeError):
        creative_group(True)
    with pytest.raises(ValueError, match="currency"):
        cohort_group(" ")


def test_objective_groups() -> None:
    assert objective_group("google", "SEARCH") == "google"
    assert objective_group("linkedin", "LEAD_GENERATION") == "linkedin_lead_gen"
    assert objective_group("linkedin", " lead_generation ") == "linkedin_lead_gen"
    assert objective_group("linkedin", "WEBSITE_CONVERSION") == "linkedin_other"
    assert objective_group("linkedin", None) == "linkedin_other"
    with pytest.raises(ValueError, match="hubspot"):
        objective_group("hubspot", None)


def test_composite_entity_keys() -> None:
    assert keyword_external_id("55", "77") == "55|77"
    assert keyword_external_id("55", 77) == "55|77"
    # A criterion id is unique only within its ad group: the same criterion in two ad groups
    # gives two keys, so feedback on one keyword never matches the other.
    assert keyword_external_id("55", "77") != keyword_external_id("56", "77")
    first = EntityRef("google", "keyword", 1, keyword_external_id("55", "77"), "crm demo")
    second = EntityRef("google", "keyword", 2, keyword_external_id("56", "77"), "crm demo")
    assert (first.key, second.key) == ("google:keyword:55|77", "google:keyword:56|77")
    assert search_term_external_id("55", "901", "crm software") == "55|901|crm software"
    assert search_term_external_id("55", None, "crm") == "55||crm"
    assert content_asset_external_id("my-paper", "eur") == "my-paper|EUR"
    assert segment_external_id("9", "seniority", "Director") == "9|seniority|Director"
    assert term_cluster_external_id(["software", "crm", "crm"]) == "crm software"
    assert creative_pair_external_id("urn:b", "urn:a") == "urn:a|urn:b"
    with pytest.raises(ValueError, match="head token"):
        term_cluster_external_id([])
    with pytest.raises(ValueError, match="two different"):
        creative_pair_external_id("urn:a", "urn:a")


# -- JSON rendering --------------------------------------------------------------------------


def test_json_value_renders_decimals_dates_and_collections() -> None:
    assert json_value(Decimal("12.340000")) == "12.340000"
    assert json_value(Decimal("1E+2")) == "100"
    assert json_value(date(2026, 10, 1)) == "2026-10-01"
    instant = datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + timedelta(hours=1)
    assert json_value(instant) == "2026-10-01T13:00:00+00:00"
    assert json_value({"a": (1, Decimal("2.5")), 3: None}) == {"a": [1, "2.5"], "3": None}
    assert json_value(frozenset({"b", "a"})) == ["a", "b"]
    assert json_value(_entity()) == {
        "source": "google",
        "type": "campaign",
        "id": 7,
        "name": "Brand",
    }
    assert json_value(0.5) == 0.5


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (Decimal("NaN"), ValueError),
        (Decimal("Infinity"), ValueError),
        (float("nan"), ValueError),
        (datetime(2026, 10, 1), ValueError),  # noqa: DTZ001 - a naive instant is the point
        ({(1, 2): 3}, TypeError),
        (object(), TypeError),
    ],
)
def test_json_value_rejects_unrenderable_values(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        json_value(value)


# -- output types ----------------------------------------------------------------------------


def test_entity_ref_key_json_and_blank_name() -> None:
    ref = _entity()
    assert ref.key == "google:campaign:123"
    assert ref.to_json() == {"source": "google", "type": "campaign", "id": 7, "name": "Brand"}
    assert _entity(name="  ").name == "123"
    with pytest.raises(ValueError, match="type"):
        EntityRef("google", "ad", None, "1", "x")
    with pytest.raises(ValueError, match="source"):
        EntityRef("bing", "campaign", None, "1", "x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EntityRef("google", "campaign", True, "1", "x")


def test_decision_subject_key_and_json() -> None:
    subject = DecisionSubject("budget", 4)
    assert subject.key == ("budget", 4)
    assert DecisionSubject.of(("budget", 4)) == subject
    assert subject.to_json() == {"type": "budget", "id": 4}
    with pytest.raises(ValueError, match="type"):
        DecisionSubject("keyword", 1)  # type: ignore[arg-type]


def test_flag_metric_baseline_sample_and_test_rendering() -> None:
    assert Flag("stale", "Data as of 2026-10-01").to_json() == {
        "code": "stale",
        "message": "Data as of 2026-10-01",
    }
    with pytest.raises(ValueError, match=r"Flag\.code"):
        Flag("unknown_code", "x")
    unavailable = Metric("ql", "Qualified Lead", None, "contacts", floor=True)
    assert unavailable.unavailable
    assert unavailable.to_json() == {
        "key": "ql",
        "label": "Qualified Lead",
        "value": None,
        "unit": "contacts",
        "floor": True,
        "unavailable": True,
    }
    with pytest.raises(TypeError):
        Metric("rate", "Rate", 0.5, "ratio")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Metric("flag", "Flag", True, "bool")
    assert Baseline("cpa", "Account CPA", Decimal("40.5")).to_json() == {
        "key": "cpa",
        "label": "Account CPA",
        "value": "40.5",
    }
    sample = Sample(
        1200, 3000, "form opens", Projection(Decimal("15000"), "impressions", "open rate")
    )
    assert not sample.met
    assert Sample(5, 5, "days").met
    assert sample.to_json() == {
        "observed": 1200,
        "required": 3000,
        "unit": "form opens",
        "projected": {"value": "15000", "unit": "impressions", "basis": "open rate"},
    }
    test = TestResult("Fisher's exact test", Decimal("0.01"), None, Decimal("0.05"))
    assert test.to_json() == {"name": "Fisher's exact test", "p_value": "0.01", "alpha": "0.05"}
    assert TestResult.__test__ is False
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        TestResult("z", Decimal("1.5"), None, Decimal("0.05"))
    with pytest.raises(TypeError):
        TestResult("z", 0.01, None, Decimal("0.05"))  # type: ignore[arg-type]


def test_evidence_to_json_is_exact_and_json_serialisable() -> None:
    evidence = Evidence(
        window={"start": date(2026, 9, 7), "end": date(2026, 10, 4), "days": 28},
        metrics=(Metric("spend", "Spend", Decimal("12.340000"), "currency", currency="USD"),),
        sample=Sample(10, 7, "days"),
        test=TestResult("z", Decimal("0.2"), Decimal("0.4"), Decimal("0.05")),
        thresholds={"c1.min_spend_day_share": 0.7},
        datasets=(DatasetStatus("google.metrics_daily", "complete", date(2026, 10, 4)),),
        provenance=(ProvenanceEntry("google", 11, "live", "primary"),),
        extra={"rows": [{"spend": Decimal("1.50")}]},
    )
    rendered = evidence.to_json()
    assert list(rendered) == [
        "window",
        "metrics",
        "baselines",
        "sample",
        "test",
        "thresholds",
        "datasets",
        "provenance",
        "extra",
    ]
    assert rendered["metrics"][0]["value"] == "12.340000"
    assert rendered["window"]["start"] == "2026-09-07"
    assert rendered["datasets"] == [
        {
            "dataset": "google.metrics_daily",
            "completeness": "complete",
            "covered_through": "2026-10-04",
        }
    ]
    assert rendered["provenance"] == [
        {"source": "google", "dataset_id": 11, "mode": "live", "role": "primary"}
    ]
    assert rendered["extra"] == {"rows": [{"spend": "1.50"}]}
    assert json.loads(json.dumps(rendered)) == rendered
    minimal = _evidence().to_json()
    assert "sample" not in minimal
    assert "test" not in minimal
    assert "extra" not in minimal


def test_evidence_lookup_copy_and_immutability() -> None:
    extra: dict[str, Any] = {"a": 1}
    evidence = _evidence(**extra)
    extra["a"] = 2
    assert evidence.extra == {"a": 1}
    assert evidence.metric("spend").value == Decimal("12.340000")
    with pytest.raises(KeyError):
        evidence.metric("clicks")
    extended = evidence.with_extra(depends_on=[{"type": "campaign", "id": 7}])
    assert extended.extra == {"a": 1, "depends_on": ({"type": "campaign", "id": 7},)}
    assert evidence.extra == {"a": 1}
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.window = {}  # type: ignore[misc]
    with pytest.raises(ValueError, match="repeats"):
        Evidence(window={}, metrics=(Metric("x", "X", 1, "n"), Metric("x", "X", 2, "n")))
    with pytest.raises(TypeError):
        Evidence(window={}, metrics=("spend",))  # type: ignore[arg-type]


def test_candidate_normalises_and_exposes_keys() -> None:
    stale = Flag("stale", "Data as of 2026-10-01")
    candidate = _candidate(
        flags=[stale, Flag("stale", "second message")],
        required_datasets=["google.metrics_daily", "google.metrics_daily", "google.entities"],
        increases_spend_on=(DecisionSubject("campaign", 7), DecisionSubject("budget", 2)),
        rank_key=[Decimal("-5")],
    )
    assert candidate.flags == (stale,)
    assert candidate.required_datasets == ("google.metrics_daily", "google.entities")
    assert candidate.rank_key == (Decimal("-5"),)
    assert candidate.flag_codes == ("stale",)
    assert candidate.has_flag("stale")
    assert candidate.with_flags(
        Flag("stale", "again"), Flag("simulated", "Simulated")
    ).flag_codes == (
        "stale",
        "simulated",
    )
    assert candidate.feedback_key == ("C1", "campaign", "google:campaign:123")
    assert candidate.sort_key == (
        (Decimal("-5"),),
        "Brand",
        (False, 7),
        "google:campaign:123",
    )
    assert candidate.spend_targets == frozenset({("campaign", 7), ("budget", 2)})
    assert not candidate.is_discrepancy
    held = dataclasses.replace(candidate, held=True, held_reason="Resolve discrepancy first")
    assert held.held
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.held = True  # type: ignore[misc]


def _ranked_keys(candidates: list[Candidate] | tuple[Candidate, ...]) -> list[str]:
    """Sort ``candidates`` by :attr:`Candidate.sort_key` and return their entity keys."""
    ranked = sorted(candidates, key=lambda candidate: candidate.sort_key)
    return [candidate.entity_ref.key for candidate in ranked]


def test_sort_key_breaks_name_ties_by_entity_id_not_external_id() -> None:
    low = _candidate(entity_ref=EntityRef("google", "campaign", 2, "9", "Brand"))
    high = _candidate(entity_ref=EntityRef("google", "campaign", 10, "10", "Brand"))
    # As text the external ids sort the other way, so a key tie-break would reverse them.
    assert high.entity_ref.key < low.entity_ref.key
    assert low.sort_key[2] == (False, 2)
    assert high.sort_key[2] == (False, 10)
    expected = ["google:campaign:9", "google:campaign:10"]
    assert _ranked_keys([high, low]) == expected
    assert _ranked_keys([low, high]) == expected


def test_sort_key_orders_id_less_entities_of_one_name_by_key() -> None:
    def term(ad_group: str) -> Candidate:
        external_id = search_term_external_id(ad_group, 7, "free crm")
        ref = EntityRef("google", "search_term", None, external_id, "free crm")
        return _candidate(rule_id=K1, section=KEYWORDS, group="block", entity_ref=ref)

    first, second = term("200"), term("31")
    assert first.sort_key[2] == second.sort_key[2] == (True, 0)
    expected = ["google:search_term:200|7|free crm", "google:search_term:31|7|free crm"]
    assert _ranked_keys([second, first]) == expected
    assert _ranked_keys([first, second]) == expected


def test_sort_key_puts_an_entity_with_an_id_before_an_id_less_one_of_the_same_name() -> None:
    creative = EntityRef("linkedin", "creative", 4, "urn:li:sponsoredCreative:4", "Guide")
    asset = EntityRef(
        "linkedin", "content_asset", None, content_asset_external_id("guide", "usd"), "Guide"
    )
    with_id = _candidate(entity_ref=creative)
    without_id = _candidate(entity_ref=asset)
    # The id slot decides: the key alone would put the content asset first.
    assert asset.key < creative.key
    expected = [creative.key, asset.key]
    assert _ranked_keys([without_id, with_id]) == expected
    assert _ranked_keys([with_id, without_id]) == expected


def test_sort_key_separates_entity_types_sharing_a_name_and_an_id_by_key() -> None:
    campaign = _candidate(entity_ref=EntityRef("google", "campaign", 5, "5", "Brand"))
    budget = _candidate(entity_ref=EntityRef("google", "budget", 5, "5", "Brand"))
    assert campaign.sort_key[:3] == budget.sort_key[:3]
    expected = ["google:budget:5", "google:campaign:5"]
    assert _ranked_keys([campaign, budget]) == expected
    assert _ranked_keys([budget, campaign]) == expected


def test_sort_key_order_is_total_and_independent_of_input_order() -> None:
    refs = [
        EntityRef("google", "campaign", 10, "10", "Brand"),
        EntityRef("google", "search_term", None, search_term_external_id("31", 7, "x"), "Brand"),
        EntityRef("google", "campaign", 2, "9", "Brand"),
        EntityRef("google", "search_term", None, search_term_external_id("200", 7, "x"), "Brand"),
        EntityRef("google", "budget", 2, "9", "Brand"),
        EntityRef("google", "campaign", 1, "1", "Alpha"),
    ]
    candidates = [_candidate(entity_ref=ref) for ref in refs]
    candidates.append(
        _candidate(
            entity_ref=EntityRef("google", "campaign", 3, "3", "Zulu"), rank_key=(Decimal("-99"),)
        )
    )
    expected = [
        "google:campaign:3",
        "google:campaign:1",
        "google:budget:9",
        "google:campaign:9",
        "google:campaign:10",
        "google:search_term:200|7|x",
        "google:search_term:31|7|x",
    ]
    for permutation in itertools.permutations(candidates):
        assert _ranked_keys(permutation) == expected


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"rule_id": "K9"}, ValueError),
        ({"section": "dashboard"}, ValueError),
        ({"group": " "}, ValueError),
        ({"action": ""}, ValueError),
        ({"required_datasets": ("google.facts",)}, ValueError),
        ({"held": True}, ValueError),
        ({"held_reason": "Resolve discrepancy first"}, ValueError),
        ({"data_status": "fresh"}, ValueError),
        ({"pinned": True}, ValueError),
        ({"entity_ref": "campaign"}, TypeError),
        ({"evidence": {}}, TypeError),
        ({"flags": ("stale",)}, TypeError),
        ({"decision_subject": ("campaign", 7)}, TypeError),
    ],
)
def test_candidate_rejects_contract_violations(
    changes: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _candidate(**changes)


def test_c0_candidates_may_be_pinned() -> None:
    pinned = _candidate(rule_id=C0, group="discrepancies", pinned=True, data_status="live")
    assert pinned.pinned
    assert pinned.is_discrepancy


def test_finding_and_withheld_rendering() -> None:
    finding = Finding("k1_no_baseline", KEYWORDS, None, "Fewer than 10 baseline conversions")
    assert finding.to_json() == {
        "code": "k1_no_baseline",
        "section": "keywords",
        "message": "Fewer than 10 baseline conversions",
        "evidence": {},
    }
    with_entity = Finding("skipped_stage", "white_papers", _entity(), "2 contacts", {"n": 2})
    assert with_entity.to_json()["entity"]["name"] == "Brand"
    with pytest.raises(ValueError, match=r"Finding\.code"):
        Finding("bogus", KEYWORDS, None, "x")
    withheld = Withheld(
        "B3", "budget", _entity(), "spending days 3 of 5 required", 3, 5, "spending days"
    )
    assert withheld.to_json() == {
        "rule_id": "B3",
        "entity": {"source": "google", "type": "campaign", "id": 7, "name": "Brand"},
        "reason": "spending days 3 of 5 required",
        "sample": {"observed": 3, "required": 5, "unit": "spending days"},
    }
    account_wide = Withheld(K1, KEYWORDS, None, "no baseline", Decimal("9"), 10, "conversions")
    assert account_wide.to_json()["entity"] is None
    assert account_wide.to_json()["sample"]["observed"] == "9"


def test_rule_result_merge_sums_suppressed_in_every_call_style() -> None:
    first = RuleResult(candidates=(_candidate(),), suppressed={"g1_blocked": 1})
    second = RuleResult(
        findings=(Finding("k1_no_baseline", KEYWORDS, None, "x"),),
        suppressed={"g1_blocked": 2, "tracking_absent": 1},
    )
    third = RuleResult(withheld=(Withheld(W, "white_papers", None, "few QL", 2, 3, "QL"),))
    for merged in (
        first.merge(second, third),
        RuleResult.merge(first, second, third),
        RuleResult().merge(first, second, third),
    ):
        assert merged.suppressed == {"g1_blocked": 3, "tracking_absent": 1}
        assert len(merged.candidates) == 1
        assert len(merged.findings) == 1
        assert len(merged.withheld) == 1
    assert first.suppressed == {"g1_blocked": 1}
    assert RuleResult().suppressed == {}
    with pytest.raises(ValueError, match="non-negative"):
        RuleResult(suppressed={"g1_blocked": -1})


# -- immutability of frozen evidence (AAP 0.5.4.8) ----------------------------------------------


def _source_days() -> list[dict[str, Any]]:
    return [
        {
            "source": "google",
            "start": "2026-09-07",
            "end": "2026-10-04",
            "boundary": "America/New_York",
        }
    ]


@dataclasses.dataclass
class _MutableRow:
    spend: Decimal


def test_evidence_keeps_its_values_when_the_caller_mutates_its_objects() -> None:
    source_days = _source_days()
    window: dict[str, Any] = {
        "start": "2026-09-07",
        "end": "2026-10-04",
        "days": 28,
        "source_days": source_days,
    }
    thresholds: dict[str, Any] = {"k2.adjacent_max_terms": 5, "window.presets": [14, 28]}
    rows: list[dict[str, Any]] = [{"spend": Decimal("1.50"), "terms": ["crm"]}]
    entity = _entity()
    evidence = Evidence(
        window=window,
        thresholds=thresholds,
        extra={"rows": rows, "tokens": {"crm"}, "entity": entity},
    )
    source_days[0]["boundary"] = "UTC"
    window["days"] = 1
    thresholds["window.presets"].append(90)
    rows[0]["spend"] = Decimal("9")
    rows[0]["terms"].append("erp")
    rows.append({"spend": Decimal("2")})
    assert evidence.window["days"] == 28
    assert evidence.window["source_days"] == (
        {
            "source": "google",
            "start": "2026-09-07",
            "end": "2026-10-04",
            "boundary": "America/New_York",
        },
    )
    assert evidence.thresholds == {"k2.adjacent_max_terms": 5, "window.presets": (14, 28)}
    assert evidence.extra["rows"] == ({"spend": Decimal("1.50"), "terms": ("crm",)},)
    assert evidence.extra["tokens"] == frozenset({"crm"})
    assert evidence.extra["entity"] is entity


def test_evidence_nested_values_cannot_be_changed_after_construction() -> None:
    evidence = Evidence(
        window={
            "start": "2026-09-07",
            "end": "2026-10-04",
            "days": 28,
            "source_days": _source_days(),
        },
        thresholds={"c1.min_spend_day_share": 0.7},
        extra={"rows": [{"spend": Decimal("1.50")}], "cohort": {"currency": "USD"}},
    )
    with pytest.raises(TypeError):
        evidence.window["source_days"][0]["boundary"] = "x"
    with pytest.raises(AttributeError):
        evidence.extra["rows"].append({"spend": Decimal("2")})
    with pytest.raises(TypeError):
        evidence.extra["cohort"]["currency"] = "EUR"
    with pytest.raises(TypeError):
        evidence.extra["added"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        evidence.window["days"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        evidence.thresholds["c1.min_spend_day_share"] = 0.1  # type: ignore[index]
    assert evidence.window["source_days"][0]["boundary"] == "America/New_York"
    assert evidence.extra["rows"] == ({"spend": Decimal("1.50")},)
    assert evidence.extra["cohort"] == {"currency": "USD"}


def test_with_extra_returns_an_independent_frozen_tree() -> None:
    evidence = _evidence(rows=[{"spend": Decimal("1.50")}])
    depends_on: list[dict[str, Any]] = [{"type": "campaign", "id": 7}]
    extended = evidence.with_extra(depends_on=depends_on)
    depends_on[0]["id"] = 8
    depends_on.append({"type": "budget", "id": 3})
    assert extended.extra == {
        "rows": ({"spend": Decimal("1.50")},),
        "depends_on": ({"type": "campaign", "id": 7},),
    }
    assert evidence.extra == {"rows": ({"spend": Decimal("1.50")},)}
    assert extended.extra is not evidence.extra
    assert extended.extra["rows"][0] is not evidence.extra["rows"][0]
    assert extended.window is not evidence.window
    with pytest.raises(TypeError):
        extended.extra["depends_on"][0]["id"] = 9
    with pytest.raises(TypeError):
        extended.extra["rows"][0]["spend"] = Decimal(0)
    assert extended.to_json()["extra"] == {
        "rows": [{"spend": "1.50"}],
        "depends_on": [{"type": "campaign", "id": 7}],
    }
    assert evidence.to_json()["extra"] == {"rows": [{"spend": "1.50"}]}


def test_frozen_evidence_renders_the_json_its_input_renders() -> None:
    window: dict[str, Any] = {
        "start": "2026-09-07",
        "end": "2026-10-04",
        "days": 28,
        "source_days": _source_days(),
    }
    thresholds: dict[str, Any] = {"c1.min_spend_day_share": 0.7, "window.presets": [14, 28]}
    extra: dict[str, Any] = {
        "rows": [{"spend": Decimal("1.50"), "day": date(2026, 10, 1)}],
        "head_tokens": ("alternative", "hubspot"),
        "tokens": {"b", "a"},
        "nested": {"k": [1, [2, 3]], "at": NOW},
        "entity": _entity(),
    }
    evidence = Evidence(window=window, thresholds=thresholds, extra=extra)
    rendered = evidence.to_json()
    assert json.dumps(rendered["window"]) == json.dumps(json_value(window))
    assert json.dumps(rendered["thresholds"]) == json.dumps(json_value(thresholds))
    assert json.dumps(rendered["extra"]) == json.dumps(json_value(extra))
    assert type(rendered["window"]) is dict
    assert type(rendered["window"]["source_days"]) is list
    assert type(rendered["window"]["source_days"][0]) is dict
    assert type(rendered["extra"]["rows"]) is list
    assert type(rendered["extra"]["head_tokens"]) is list
    assert rendered["extra"]["tokens"] == ["a", "b"]
    assert rendered["extra"]["nested"] == {"k": [1, [2, 3]], "at": "2026-10-06T02:00:00+00:00"}
    assert json.loads(json.dumps(rendered)) == rendered


def test_values_that_cannot_be_frozen_are_rejected_naming_the_field() -> None:
    with pytest.raises(TypeError, match=r"Evidence\.extra\['rows'\]\[0\]\['raw'\]"):
        _evidence(rows=[{"raw": bytearray(b"x")}])
    with pytest.raises(TypeError, match=r"Evidence\.extra\['row'\].*_MutableRow"):
        _evidence(row=_MutableRow(Decimal(1)))
    with pytest.raises(TypeError, match=r"Evidence\.window\['days'\]"):
        Evidence(window={"days": object()})
    with pytest.raises(TypeError, match=r"Evidence\.thresholds keys must be strings"):
        Evidence(window={}, thresholds={1: 2})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match=r"Finding\.evidence\['blob'\]"):
        Finding("k1_no_baseline", KEYWORDS, None, "x", {"blob": bytearray()})
    with pytest.raises(TypeError, match=r"Observation\.payload\['x'\]"):
        Observation(NOW, payload={"x": [bytearray()]})
    with pytest.raises(TypeError, match=r"Observation\.payload must be a mapping"):
        Observation(NOW, payload=["x"])  # type: ignore[arg-type]


@dataclasses.dataclass(frozen=True)
class _DictRecord:
    data: dict[str, list[int]]

    def to_json(self) -> dict[str, list[int]]:
        return self.data


@dataclasses.dataclass(frozen=True)
class _ListRecord:
    values: list[int]


@dataclasses.dataclass(frozen=True)
class _ProxyRecord:
    data: Mapping[str, int]


@dataclasses.dataclass(frozen=True)
class _AnyTupleRecord:
    values: tuple[Any, ...]

    def to_json(self) -> dict[str, Any]:
        return {"values": self.values}


@dataclasses.dataclass(frozen=True)
class _AnySetRecord:
    tags: frozenset[Any]


@dataclasses.dataclass(frozen=True)
class _OuterRecord:
    label: str
    inner: _DictRecord


@dataclasses.dataclass(frozen=True)
class _MutableFieldRecord:
    row: _MutableRow


@dataclasses.dataclass(frozen=True)
class _ClassRecord:
    kind: type


@dataclasses.dataclass(frozen=True)
class _ImmutableRecord:
    label: str
    values: tuple[int, ...]
    tags: frozenset[str]
    entity: EntityRef
    metric: Metric
    day: date
    at: datetime
    spend: Decimal | None

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "values": list(self.values),
            "tags": sorted(self.tags),
            "entity": self.entity.to_json(),
            "metric": self.metric.to_json(),
            "day": self.day,
            "at": self.at,
            "spend": self.spend,
        }


def _discrepancy(decided: Any, platform: Any) -> DiscrepancyInfo:
    return DiscrepancyInfo(
        1,
        "budget",
        3,
        "Shared budget 777",
        "google",
        "budget",
        "budget",
        decided,
        platform,
        NOW,
        "open",
    )


_DICT_FIELD = r"is a frozen dataclass whose field 'data' holds a dict, so it is not immutable"


def test_frozen_record_holding_a_dict_is_rejected_wherever_it_is_stored() -> None:
    payload: dict[str, list[int]] = {"values": [1]}
    row = _DictRecord(payload)
    with pytest.raises(TypeError, match=r"Evidence\.extra\['row'\] " + _DICT_FIELD):
        _evidence(row=row)
    with pytest.raises(TypeError, match=r"Evidence\.extra\['rows'\]\[1\] " + _DICT_FIELD):
        _evidence(rows=[_entity(), row])
    with pytest.raises(TypeError, match=r"Finding\.evidence\['row'\] " + _DICT_FIELD):
        Finding("pipeline_unavailable", CAMPAIGNS, None, "Pipeline unavailable", {"row": row})
    with pytest.raises(TypeError, match=r"Observation\.payload\['row'\] " + _DICT_FIELD):
        Observation(NOW, payload={"row": row})
    with pytest.raises(TypeError, match=r"DiscrepancyInfo\.decided_value\['row'\] " + _DICT_FIELD):
        _discrepancy({"row": row}, None)
    with pytest.raises(TypeError, match=r"DiscrepancyInfo\.platform_value\['row'\] " + _DICT_FIELD):
        _discrepancy(None, {"nested": [1], "row": row})
    evidence = _evidence(note="kept", rows=[{"spend": Decimal("1.50")}])
    before = evidence.to_json()
    with pytest.raises(TypeError, match=r"Evidence\.extra\['row'\] " + _DICT_FIELD):
        evidence.with_extra(row=row)
    assert evidence.to_json() == before
    assert evidence.extra == {"note": "kept", "rows": ({"spend": Decimal("1.50")},)}
    payload["values"].append(2)
    assert evidence.to_json() == before


def test_frozen_records_with_mutable_fields_at_any_depth_are_rejected() -> None:
    with pytest.raises(
        TypeError,
        match=(
            r"Evidence\.extra\['row'\] is a frozen dataclass whose field 'values' holds a list, "
            r"so it is not immutable"
        ),
    ):
        _evidence(row=_ListRecord([1]))
    with pytest.raises(TypeError, match=r"whose field 'data' holds a mappingproxy"):
        _evidence(row=_ProxyRecord(MappingProxyType({"a": 1})))
    with pytest.raises(TypeError, match=r"whose field 'inner\.data' holds a dict"):
        _evidence(row=_OuterRecord("outer", _DictRecord({"values": [1]})))
    with pytest.raises(TypeError, match=r"whose field 'values\[1\]' holds a list"):
        _evidence(row=_AnyTupleRecord((1, [2])))
    with pytest.raises(TypeError, match=r"whose field 'values\[0\]\[0\]\.data' holds a dict"):
        _evidence(row=_AnyTupleRecord(((_DictRecord({}),),)))
    with pytest.raises(TypeError, match=r"whose field 'tags item' holds an object"):
        _evidence(row=_AnySetRecord(frozenset({"a", object()})))
    with pytest.raises(
        TypeError,
        match=r"whose field 'row' holds a _MutableRow \(a dataclass that is not frozen\)",
    ):
        Finding(
            "pipeline_unavailable",
            CAMPAIGNS,
            None,
            "Pipeline unavailable",
            {"row": _MutableFieldRecord(_MutableRow(Decimal(1)))},
        )
    with pytest.raises(TypeError, match=r"whose field 'kind' holds a type"):
        Observation(NOW, payload={"row": _AnyTupleRecord((1,)), "bad": _ClassRecord(dict)})
    with pytest.raises(TypeError, match=r"Evidence\.extra\['row'\].*_MutableRow"):
        _evidence(row=_MutableRow(Decimal(1)))


def test_deeply_immutable_frozen_records_are_stored_as_the_same_object() -> None:
    entity = _entity()
    metric = Metric("spend", "Spend", Decimal("12.340000"), "currency", currency="USD")
    record = _ImmutableRecord(
        "row",
        (1, 2),
        frozenset({"b", "a"}),
        entity,
        metric,
        date(2026, 10, 1),
        NOW,
        None,
    )
    nested = _AnyTupleRecord((record, (entity, None), frozenset({metric})))
    evidence = _evidence(entity=entity, metric=metric, record=record, nested=nested)
    assert evidence.extra["entity"] is entity
    assert evidence.extra["metric"] is metric
    assert evidence.extra["record"] is record
    assert evidence.extra["nested"] is nested
    assert evidence.with_extra(more=record).extra["more"] is record
    assert evidence.to_json()["extra"]["record"] == {
        "label": "row",
        "values": [1, 2],
        "tags": ["a", "b"],
        "entity": entity.to_json(),
        "metric": metric.to_json(),
        "day": "2026-10-01",
        "at": "2026-10-06T02:00:00+00:00",
        "spend": None,
    }
    assert evidence.to_json()["extra"]["entity"] == entity.to_json()
    assert evidence.to_json()["extra"]["metric"] == metric.to_json()
    assert evidence.to_json()["extra"]["nested"] == {
        "values": [
            evidence.to_json()["extra"]["record"],
            [entity.to_json(), None],
            [metric.to_json()],
        ]
    }
    finding = Finding(
        "pipeline_unavailable", CAMPAIGNS, None, "Pipeline unavailable", {"r": record}
    )
    assert finding.evidence["r"] is record
    assert finding.to_json()["evidence"]["r"]["values"] == [1, 2]
    observation = Observation(NOW, payload={"entity": entity, "record": record})
    assert observation.payload["record"] is record
    assert json_value(observation.payload)["entity"] == entity.to_json()
    discrepancy = _discrepancy({"entity": entity}, {"record": record})
    assert discrepancy.decided_value is not None
    assert discrepancy.decided_value["entity"] is entity
    assert discrepancy.platform_value is not None
    assert discrepancy.platform_value["record"] is record


def test_finding_evidence_is_deep_copied_and_read_only() -> None:
    flags = ["pipeline_unavailable"]
    evidence: dict[str, Any] = {"flags": flags, "window": {"start": date(2026, 9, 7)}}
    finding = Finding("pipeline_unavailable", CAMPAIGNS, None, "Pipeline unavailable", evidence)
    flags.append("attribution_floor")
    evidence["window"]["start"] = date(2026, 1, 1)
    evidence["added"] = 1
    assert finding.evidence == {
        "flags": ("pipeline_unavailable",),
        "window": {"start": date(2026, 9, 7)},
    }
    with pytest.raises(AttributeError):
        finding.evidence["flags"].append("x")
    with pytest.raises(TypeError):
        finding.evidence["window"]["start"] = date(2026, 1, 2)
    with pytest.raises(TypeError):
        finding.evidence["added"] = 1  # type: ignore[index]
    assert finding.to_json()["evidence"] == {
        "flags": ["pipeline_unavailable"],
        "window": {"start": "2026-09-07"},
    }


def test_rule_result_suppressed_is_read_only_and_merge_still_sums() -> None:
    counts = {"g1_blocked": 1}
    result = RuleResult(suppressed=counts)
    counts["g1_blocked"] = 5
    counts["tracking_absent"] = 1
    assert result.suppressed == {"g1_blocked": 1}
    with pytest.raises(TypeError):
        result.suppressed["g1_blocked"] = 2  # type: ignore[index]
    merged = result.merge(RuleResult(suppressed={"g1_blocked": 2, "tracking_absent": 1}))
    assert merged.suppressed == {"g1_blocked": 3, "tracking_absent": 1}
    with pytest.raises(TypeError):
        merged.suppressed["g1_blocked"] = 0  # type: ignore[index]
    assert result.suppressed == {"g1_blocked": 1}
    assert RuleResult().merge(result, result).suppressed == {"g1_blocked": 2}
    with pytest.raises(TypeError, match="keys must be strings"):
        RuleResult(suppressed={1: 1})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="must be a mapping"):
        RuleResult(suppressed=[("g1_blocked", 1)])  # type: ignore[arg-type]


# -- input types -----------------------------------------------------------------------------


def test_dataset_ref_and_provenance() -> None:
    assert COMPANION.provenance() == ProvenanceEntry("hubspot", 14, "simulated", "companion")
    entry = ProvenanceEntry.from_json(COMPANION.provenance().to_json())
    assert entry == COMPANION.provenance()
    with pytest.raises(ValueError, match="companion"):
        DatasetRef(1, "linkedin", "simulated", "companion")
    with pytest.raises(ValueError, match="companion"):
        DatasetRef(1, "hubspot", "live", "companion")
    with pytest.raises(ValueError, match="mode"):
        DatasetRef(1, "google", "mock")  # type: ignore[arg-type]


def test_freshness_staleness_and_status() -> None:
    row = Freshness(
        11,
        "google.metrics_daily",
        NOW,
        NOW - timedelta(hours=2),
        date(2026, 4, 1),
        date(2026, 10, 4),
        "complete",
    )
    day = timedelta(hours=24)
    assert not row.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 4))
    assert row.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 5))
    assert row.is_stale(
        now=NOW + timedelta(hours=23), stale_after=day, window_end=date(2026, 10, 4)
    )
    never = dataclasses.replace(row, last_success_at=None, completeness="unknown")
    assert never.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 1))
    assert row.is_complete
    assert not never.is_complete
    assert row.status() == DatasetStatus("google.metrics_daily", "complete", date(2026, 10, 4))
    with pytest.raises(ValueError, match="dataset"):
        Freshness(11, "google.facts", None, None, None, None, "unknown")
    with pytest.raises(TypeError):
        Freshness(11, "google.entities", None, None, None, NOW, "unknown")


NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("success", "now", "stale_after_hours", "stale"),
    [
        # Fall back: Oct 31 noon EDT to Nov 1 noon EST is 25 real hours, 24 by the wall clock.
        (
            datetime(2026, 10, 31, 12, tzinfo=NEW_YORK),
            datetime(2026, 11, 1, 12, tzinfo=NEW_YORK),
            24,
            True,
        ),
        # Exactly 25 elapsed hours is not more than 25.
        (
            datetime(2026, 10, 31, 12, tzinfo=NEW_YORK),
            datetime(2026, 11, 1, 12, tzinfo=NEW_YORK),
            25,
            False,
        ),
        # Spring forward: Mar 7 noon EST to Mar 8 noon EDT is 23 real hours, 24 by the wall clock.
        (
            datetime(2026, 3, 7, 12, tzinfo=NEW_YORK),
            datetime(2026, 3, 8, 12, tzinfo=NEW_YORK),
            23,
            False,
        ),
        (
            datetime(2026, 3, 7, 12, tzinfo=NEW_YORK),
            datetime(2026, 3, 8, 12, tzinfo=NEW_YORK),
            22,
            True,
        ),
    ],
)
def test_freshness_age_is_elapsed_time_across_dst(
    success: datetime, now: datetime, stale_after_hours: int, stale: bool
) -> None:
    row = Freshness(11, "google.metrics_daily", success, success, None, now.date(), "complete")
    stale_after = timedelta(hours=stale_after_hours)
    assert row.is_stale(now=now, stale_after=stale_after, window_end=now.date()) is stale


def test_freshness_stores_its_instants_in_utc() -> None:
    attempt = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NEW_YORK)  # 06:30 UTC (EST)
    success = datetime(2026, 11, 1, 1, 10, fold=0, tzinfo=NEW_YORK)  # 05:10 UTC (EDT)
    row = Freshness(11, "google.entities", attempt, success, None, None, "unknown")
    assert row.last_attempt_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert row.last_success_at == datetime(2026, 11, 1, 5, 10, tzinfo=UTC)
    assert row.last_attempt_at is not None
    assert row.last_success_at is not None
    assert row.last_attempt_at.tzinfo is UTC
    assert row.last_success_at.tzinfo is UTC
    assert row.last_attempt_at.timestamp() == attempt.timestamp()
    assert row.last_success_at.timestamp() == success.timestamp()
    assert min(row.last_attempt_at, row.last_success_at) == row.last_success_at
    plain = datetime(2026, 10, 31, 12, tzinfo=NEW_YORK)
    moved = dataclasses.replace(row, last_attempt_at=plain, last_success_at=None)
    assert moved.last_attempt_at == plain
    assert moved.last_attempt_at is not None
    assert moved.last_attempt_at.tzinfo is UTC
    assert moved.last_success_at is None


def test_freshness_and_feedback_require_an_aware_now() -> None:
    naive = datetime(2026, 10, 6, 2, 0)  # noqa: DTZ001 - a naive instant is the point
    day = timedelta(hours=24)
    row = Freshness(11, "google.metrics_daily", NOW, NOW, None, date(2026, 10, 4), "complete")
    never = dataclasses.replace(row, last_success_at=None)
    for target in (row, never):
        with pytest.raises(ValueError, match=r"Freshness\.is_stale now must be tz-aware"):
            target.is_stale(now=naive, stale_after=day, window_end=date(2026, 10, 4))
        with pytest.raises(TypeError, match=r"Freshness\.is_stale now must be a tz-aware"):
            target.is_stale(
                now=date(2026, 10, 6),  # type: ignore[arg-type]
                stale_after=day,
                window_end=date(2026, 10, 4),
            )
    feedback = FeedbackInfo("C1", "campaign", "google:campaign:123", "snooze", NOW)
    with pytest.raises(ValueError, match=r"FeedbackInfo\.active_at now must be tz-aware"):
        feedback.active_at(naive)
    with pytest.raises(TypeError, match=r"FeedbackInfo\.active_at now must be a tz-aware"):
        feedback.active_at("2026-10-06T02:00:00+00:00")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match=r"FeedbackInfo\.until"):
        FeedbackInfo("C1", "campaign", "k", "snooze", date(2026, 10, 6))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"FeedbackInfo\.until"):
        FeedbackInfo("C1", "campaign", "k", "snooze", naive)


def test_full_state_datasets_are_the_entity_syncs() -> None:
    assert frozenset({"google.entities", "linkedin.entities"}) == FULL_STATE_DATASETS
    assert set(DATASETS) >= FULL_STATE_DATASETS
    assert "FULL_STATE_DATASETS" in ctx_module.__all__


@pytest.mark.parametrize(
    ("dataset", "completeness", "covered_from", "covered_through", "expected"),
    [
        # The review's reproduction: complete over 2026-09-20..2026-10-01, but the window
        # starts on 2026-09-04, so 16 requested days are not covered.
        ("google.metrics_daily", "complete", date(2026, 9, 20), date(2026, 10, 1), False),
        ("google.metrics_daily", "complete", date(2026, 9, 5), date(2026, 10, 1), False),
        ("google.metrics_daily", "complete", date(2026, 9, 4), date(2026, 10, 1), True),
        ("google.metrics_daily", "complete", date(2026, 4, 1), date(2026, 10, 1), True),
        ("google.metrics_daily", "complete", date(2026, 4, 1), date(2026, 10, 5), True),
        # Coverage that stops before the window end is not complete over the window.
        ("google.metrics_daily", "complete", date(2026, 4, 1), date(2026, 9, 30), False),
        ("google.search_terms", "complete", date(2026, 9, 4), date(2026, 9, 4), False),
        # Short at both ends.
        ("google.metrics_daily", "complete", date(2026, 9, 5), date(2026, 9, 30), False),
        # Half a range is no range.
        ("google.metrics_daily", "complete", date(2026, 4, 1), None, False),
        ("google.metrics_daily", "complete", None, date(2026, 10, 1), False),
        ("linkedin.leads", "complete", None, None, False),
        ("google.metrics_daily", "incomplete", date(2026, 4, 1), date(2026, 10, 1), False),
        ("hubspot.contacts", "unknown", date(2026, 4, 1), date(2026, 10, 1), False),
        # A full-state entity read has no date range: its completeness alone decides.
        ("google.entities", "complete", None, None, True),
        ("linkedin.entities", "complete", date(2026, 9, 20), date(2026, 9, 21), True),
        ("google.entities", "incomplete", None, None, False),
        ("linkedin.entities", "unknown", None, None, False),
    ],
)
def test_freshness_is_complete_over_requires_coverage_of_the_whole_window(
    dataset: str,
    completeness: str,
    covered_from: date | None,
    covered_through: date | None,
    expected: bool,
) -> None:
    row = Freshness(
        3,
        dataset,
        NOW,
        NOW - timedelta(hours=1),
        covered_from,
        covered_through,
        completeness,  # type: ignore[arg-type]
    )
    assert (
        row.is_complete_over(window_start=date(2026, 9, 4), window_end=date(2026, 10, 1))
        is expected
    )
    # The covered-range verdict other callers read is unchanged.
    assert row.is_complete is (completeness == "complete")


def test_a_required_range_wider_than_the_window_needs_coverage_from_its_own_start() -> None:
    # A K1 item's google.metrics_daily must cover its 90-day baseline, not only the window.
    row = Freshness(
        3,
        "google.metrics_daily",
        NOW,
        NOW - timedelta(hours=1),
        date(2026, 9, 4),
        date(2026, 10, 1),
        "complete",
    )
    assert row.is_complete_over(window_start=date(2026, 9, 4), window_end=date(2026, 10, 1))
    assert not row.is_complete_over(window_start=date(2026, 7, 4), window_end=date(2026, 10, 1))
    baseline = dataclasses.replace(row, covered_from=date(2026, 7, 4))
    assert baseline.is_complete_over(window_start=date(2026, 7, 4), window_end=date(2026, 10, 1))


def test_coverage_short_of_the_window_end_is_both_stale_and_incomplete() -> None:
    row = Freshness(
        3,
        "google.search_terms",
        NOW,
        NOW - timedelta(hours=1),
        date(2026, 4, 1),
        date(2026, 9, 30),
        "complete",
    )
    day = timedelta(hours=24)
    # AAP 0.3.2: stale when it covers less than the window end, and incomplete when it is not
    # complete over the window, which coverage ending a day early is not.
    assert row.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 1))
    assert not row.is_complete_over(window_start=date(2026, 9, 4), window_end=date(2026, 10, 1))
    # Coverage reaching the window end is neither.
    covered = dataclasses.replace(row, covered_through=date(2026, 10, 1))
    assert not covered.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 1))
    assert covered.is_complete_over(window_start=date(2026, 9, 4), window_end=date(2026, 10, 1))


@pytest.mark.parametrize("dataset", sorted(FULL_STATE_DATASETS))
def test_full_state_datasets_are_stale_by_age_alone(dataset: str) -> None:
    day = timedelta(hours=24)
    fresh = Freshness(3, dataset, NOW, NOW - timedelta(hours=2), None, None, "complete")
    for window_end in (date(2026, 9, 1), date(2026, 10, 5), date(2026, 12, 31)):
        assert not fresh.is_stale(now=NOW, stale_after=day, window_end=window_end)
    covered = dataclasses.replace(
        fresh, covered_from=date(2026, 4, 1), covered_through=date(2026, 9, 1)
    )
    assert not covered.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 5))
    old = dataclasses.replace(fresh, last_success_at=NOW - timedelta(hours=25))
    assert old.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 5))
    never = dataclasses.replace(fresh, last_success_at=None)
    assert never.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 5))
    # A dated dataset with the same row is stale: it covers no date of the window.
    dated = dataclasses.replace(fresh, dataset="google.metrics_daily")
    assert dated.is_stale(now=NOW, stale_after=day, window_end=date(2026, 10, 5))


def test_entity_infos_validate_and_default() -> None:
    campaign = CampaignInfo(
        id=1,
        source="linkedin",
        account_id=1,
        external_id="500",
        name="Lead gen",
        status="active",
        raw_status="ACTIVE",
        objective_or_channel="LEAD_GENERATION",
        objective_group="linkedin_lead_gen",
        campaign_group_id=None,
        budget_id=None,
        daily_budget=Decimal("100"),
        total_budget=None,
        budget_period=None,
        currency="USD",
    )
    assert campaign.objective_group == "linkedin_lead_gen"
    with pytest.raises(ValueError, match="objective_group"):
        dataclasses.replace(campaign, objective_group="linkedin_other")
    with pytest.raises(ValueError, match="status"):
        dataclasses.replace(campaign, status="enabled")  # type: ignore[arg-type]
    shared = BudgetInfo(3, "777", "", Decimal("50"), 50_000_000, "DAILY", True, "USD")
    assert shared.name == "Shared budget 777"
    own = BudgetInfo(4, "778", "", None, None, "CUSTOM_PERIOD", False, "USD")
    assert own.name == "Budget 778"
    with pytest.raises(ValueError, match="source"):
        AccountInfo(1, "hubspot", "x", "x", "USD")


def test_facts_observations_and_leads() -> None:
    fact = DayFact(date(2026, 10, 1), Decimal("5"), 100, 4, conversions=Decimal("0.5"), leads=2)
    assert fact.outcome("google") == Decimal("0.5")
    assert fact.outcome("linkedin_lead_gen") == Decimal(2)
    assert fact.outcome("linkedin_other") is None
    with pytest.raises(KeyError):
        fact.outcome("bing")
    with pytest.raises(TypeError, match="native day"):
        DayFact(NOW, Decimal("5"), 100, 4)
    assert RateBaseline(Decimal("1.4"), 1, date(2026, 7, 7), date(2026, 10, 4)).rate == Decimal(
        "1.4"
    )
    assert RateBaseline(Decimal(0), 0, date(2026, 7, 7), date(2026, 10, 4)).rate is None
    with pytest.raises(ValueError, match="tz-aware"):
        Observation(datetime(2026, 10, 1))  # noqa: DTZ001 - a naive instant is the point
    lead = LeadInfo(1, "abc", 1, None, None, "urn:li:leadGenForm:1", 2, NOW, "matched", "901")
    assert lead.matched
    assert not lead.unresolved
    no_row = LeadInfo(2, "abd", 1, None, None, "urn:li:leadGenForm:1", 2, NOW)
    assert no_row.unresolved
    with pytest.raises(ValueError, match="match_status"):
        LeadInfo(3, "x", 1, None, None, "urn", 1, NOW, "found")  # type: ignore[arg-type]


def test_day_fact_reported_floors_stand_only_for_unavailable_metrics() -> None:
    day = date(2026, 10, 1)
    floors = (("leads", 3), ("conversions_value", Decimal("1.5")))
    fact = DayFact(day, Decimal("5"), 100, 4, form_opens=12, reported_floors=floors)
    # Stored in field order, so the same floors given in any order compare and hash equal.
    assert fact.reported_floors == (("conversions_value", Decimal("1.5")), ("leads", 3))
    same = DayFact(
        day,
        Decimal("5"),
        100,
        4,
        form_opens=12,
        reported_floors=[("conversions_value", Decimal("1.5")), ["leads", 3]],  # type: ignore[list-item]
    )
    assert (same, hash(same)) == (fact, hash(fact))
    # Counts and rates read the metric itself: unavailable, never the floor.
    assert (fact.leads, fact.outcome("linkedin_lead_gen")) == (None, None)
    # P2 reads what was recorded: the complete value, else the floor, else nothing.
    assert fact.recorded_outcome("linkedin_lead_gen") == Decimal(3)
    assert fact.recorded_outcome("linkedin_other") is None
    assert fact.recorded_outcome("google") is None
    complete = DayFact(day, Decimal("5"), 100, 4, conversions=Decimal("0.5"), website_conversions=2)
    assert complete.reported_floors == ()
    assert complete.recorded_outcome("linkedin_other") == Decimal(2)
    assert complete.recorded_outcome("google") == Decimal("0.5")
    zero_floor = DayFact(day, Decimal("5"), 100, 4, reported_floors=(("leads", 0),))
    assert zero_floor.recorded_outcome("linkedin_lead_gen") == Decimal(0)
    with pytest.raises(KeyError):
        fact.recorded_outcome("bing")
    # Reporting the metric later leaves no room for its floor.
    with pytest.raises(ValueError, match="leads is reported"):
        dataclasses.replace(fact, leads=3)


@pytest.mark.parametrize(
    ("floors", "error", "match"),
    [
        pytest.param("leads", TypeError, "must be a tuple of pairs", id="not-a-sequence"),
        pytest.param((("leads",),), TypeError, r"not a \(name, value\) pair", id="short-pair"),
        pytest.param((("leads", 3, 4),), TypeError, r"not a \(name, value\) pair", id="triple"),
        pytest.param(("leads",), TypeError, r"not a \(name, value\) pair", id="bare-name"),
        pytest.param(
            (("cost", Decimal(1)),), ValueError, "reported_floors name must be one of", id="cost"
        ),
        pytest.param(
            (("budget_lost_is", Decimal("0.1")),),
            ValueError,
            "reported_floors name must be one of",
            id="share",
        ),
        pytest.param((("leads", 1), ("leads", 2)), ValueError, "given twice", id="duplicate"),
        pytest.param((("form_opens", 1),), ValueError, "form_opens is reported", id="reported"),
        pytest.param((("leads", Decimal(3)),), TypeError, "must be an int count", id="decimal"),
        pytest.param((("leads", True),), TypeError, "must be an int count", id="bool-count"),
        pytest.param(
            (("conversions", 1.5),), TypeError, "must be a Decimal or int", id="float-amount"
        ),
        pytest.param(
            (("conversions", False),), TypeError, "must be a Decimal or int", id="bool-amount"
        ),
        pytest.param((("conversions", Decimal("NaN")),), ValueError, "must be finite", id="nan"),
        pytest.param(
            (("conversions_value", Decimal("Infinity")),),
            ValueError,
            "must be finite",
            id="infinite",
        ),
        pytest.param((("leads", -1),), ValueError, "must not be negative", id="negative"),
        pytest.param(
            (("conversions_value", Decimal("-0.5")),),
            ValueError,
            "must not be negative",
            id="negative-amount",
        ),
    ],
)
def test_day_fact_rejects_malformed_reported_floors(
    floors: object, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        DayFact(
            date(2026, 10, 1),
            Decimal("5"),
            100,
            4,
            form_opens=12,
            reported_floors=floors,  # type: ignore[arg-type]
        )


def test_association_fetch_knowledge_and_crm_lookups() -> None:
    assert AssociationFetchInfo("contact", "1", "deal", "ok_empty").known
    assert AssociationFetchInfo("contact", "1", "deal", "ok", "ok").known
    assert not AssociationFetchInfo("contact", "1", "deal", "ok", "partial", ("9",)).known
    assert not AssociationFetchInfo("contact", "1", "deal", "partial", "ok").known
    assert not AssociationFetchInfo("contact", "1", "deal", "error").known
    with pytest.raises(ValueError, match="object_type"):
        AssociationFetchInfo("company", "1", "deal", "ok")
    crm = CrmData(
        HUBSPOT,
        _bindings(),
        contacts={"1": ContactInfo("1")},
        associations={("contact", "1", "deal"): ("9",)},
        fetches={
            ("contact", "1", "deal"): AssociationFetchInfo("contact", "1", "deal", "ok", "ok")
        },
    )
    assert crm.targets("contact", "1", "deal") == ("9",)
    assert crm.targets("contact", "1", "meeting") == ()
    assert crm.association_known("contact", "1", "deal")
    assert not crm.association_known("contact", "1", "meeting")
    assert crm.fetch("contact", "2", "deal") is None
    with pytest.raises(ValueError, match="HubSpot"):
        CrmData(GOOGLE, _bindings())


def test_feedback_matches_candidates_but_never_c0() -> None:
    feedback = FeedbackInfo("C1", "campaign", "google:campaign:123", "snooze", NOW)
    assert feedback.matches(_candidate())
    assert not feedback.matches(_candidate(rule_id=C0, group="discrepancies"))
    assert feedback.suppression_reason == "feedback_snooze"
    assert feedback.active_at(NOW - timedelta(seconds=1))
    assert not feedback.active_at(NOW)
    with pytest.raises(ValueError, match="kind"):
        FeedbackInfo("C1", "campaign", "k", "hide", NOW)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("until_fold", "now_fold", "active"),
    [
        # until 01:10 EST (06:10 UTC) is 40 minutes after now 01:30 EDT (05:30 UTC).
        (1, 0, True),
        # until 01:10 EDT (05:10 UTC) is 80 minutes before now 01:30 EST (06:30 UTC).
        (0, 1, False),
    ],
)
def test_feedback_expiry_compares_instants_in_a_dst_fold(
    until_fold: int, now_fold: int, active: bool
) -> None:
    until = datetime(2026, 11, 1, 1, 10, fold=until_fold, tzinfo=NEW_YORK)
    now = datetime(2026, 11, 1, 1, 30, fold=now_fold, tzinfo=NEW_YORK)
    feedback = FeedbackInfo("C1", "campaign", "google:campaign:123", "snooze", until)
    assert feedback.active_at(now) is active
    assert feedback.until.tzinfo is UTC
    assert feedback.until.timestamp() == until.timestamp()


def test_feedback_stores_until_in_utc() -> None:
    until = datetime(2026, 11, 3, 9, 0, tzinfo=NEW_YORK)
    feedback = FeedbackInfo("C1", "campaign", "google:campaign:123", "dismiss", until)
    assert feedback.until == until
    assert feedback.until == datetime(2026, 11, 3, 14, 0, tzinfo=UTC)
    assert feedback.until.tzinfo is UTC
    assert feedback.active_at(datetime(2026, 11, 3, 8, 59, tzinfo=NEW_YORK))
    assert not feedback.active_at(datetime(2026, 11, 3, 14, 0, tzinfo=UTC))


# -- the analysis context --------------------------------------------------------------------


def test_context_defaults_lookups_and_cache(thresholds: Thresholds, window: AnalysisWindow) -> None:
    bare = AnalysisContext(window, thresholds, NOW)
    assert bare.run_kind == "default"
    assert bare.campaigns == {}
    assert bare.dataset_id_for("google.entities", crm_scope=None) is None
    context = _context(
        thresholds,
        window,
        accounts={1: AccountInfo(1, "google", "1234567890", "Main", "USD", "America/New_York")},
        campaigns={
            5: CampaignInfo(
                5,
                "google",
                1,
                "123",
                "Brand",
                "active",
                "ENABLED",
                "SEARCH",
                "google",
                None,
                3,
                None,
                None,
                None,
                "USD",
            )
        },
        budgets={3: BudgetInfo(3, "777", "", Decimal("50"), 50_000_000, "DAILY", True, "USD")},
        discrepancies=(
            DiscrepancyInfo(
                1,
                "budget",
                3,
                "Shared budget 777",
                "google",
                "budget",
                "budget",
                {"daily_budget": "50"},
                {"daily_budget": "60"},
                NOW,
                "open",
            ),
        ),
    )
    assert context.crm("linkedin") is context.linkedin_crm
    assert context.crm("google") is context.google_crm
    assert context.dataset("google") == GOOGLE
    assert context.dataset_id_for("google.metrics_daily", crm_scope=None) == 11
    assert context.dataset_id_for("hubspot.contacts", crm_scope="linkedin") == 14
    assert context.dataset_id_for("hubspot.contacts", crm_scope="google") == 13
    assert context.dataset_id_for("hubspot.contacts", crm_scope=None) == 13
    with pytest.raises(ValueError, match="dataset name"):
        context.dataset_id_for("google.facts", crm_scope=None)
    assert context.stale_after == timedelta(hours=24)
    assert context.account_currency(5) == "USD"
    with pytest.raises(KeyError):
        context.account_currency(6)
    assert context.campaign_name(5) == "Brand"
    assert context.campaign_name(6) == "Campaign #6"
    assert context.subject_name(("budget", 3)) == "Shared budget 777"
    assert context.subject_name(("budget", 9)) == "Budget #9"
    assert context.discrepant_subjects == frozenset({("budget", 3)})
    asset = context.resolve_asset(creative_urn=None, form_urn="urn:li:leadGenForm:990000101")
    assert asset is not None
    assert asset.slug == "b2b-pipeline-report-2026"
    context.cache["history"] = 1
    assert context == dataclasses.replace(context, cache={})


def test_context_enforces_dataset_isolation(thresholds: Thresholds, window: AnalysisWindow) -> None:
    with pytest.raises(ValueError, match="companion CRM dataset while LinkedIn is simulated"):
        _context(thresholds, window, linkedin_crm=CrmData(HUBSPOT, _bindings()))
    with pytest.raises(ValueError, match="primary CRM dataset while LinkedIn is live"):
        _context(
            thresholds,
            window,
            datasets={"linkedin": DatasetRef(12, "linkedin", "live")},
            linkedin_crm=CrmData(COMPANION, _bindings()),
        )
    with pytest.raises(ValueError, match="never a companion"):
        _context(thresholds, window, google_crm=CrmData(COMPANION, _bindings()))
    with pytest.raises(ValueError, match="primary"):
        _context(thresholds, window, datasets={"google": LINKEDIN_SIM})
    with pytest.raises(ValueError, match="tz-aware"):
        _context(thresholds, window, now=datetime(2026, 10, 6))  # noqa: DTZ001
    with pytest.raises(TypeError, match="window"):
        _context(thresholds, None)  # type: ignore[arg-type]


def test_observation_and_discrepancy_values_are_deep_copied_and_read_only() -> None:
    payload: dict[str, Any] = {"attribution_model": "GOOGLE_ADS_LAST_CLICK", "labels": ["a"]}
    observation = Observation(NOW, payload=payload)
    payload["attribution_model"] = "DATA_DRIVEN"
    payload["labels"].append("b")
    assert observation.payload == {"attribution_model": "GOOGLE_ADS_LAST_CLICK", "labels": ("a",)}
    with pytest.raises(TypeError):
        observation.payload["counting_type"] = "ONE_PER_CLICK"  # type: ignore[index]
    with pytest.raises(AttributeError):
        observation.payload["labels"].append("c")
    assert Observation(NOW).payload == {}
    decided: dict[str, Any] = {"daily_budget": "50", "history": [{"at": "2026-10-01"}]}
    item = DiscrepancyInfo(
        1,
        "budget",
        3,
        "Shared budget 777",
        "google",
        "budget",
        "budget",
        decided,
        None,
        NOW,
        "open",
    )
    decided["daily_budget"] = "60"
    decided["history"][0]["at"] = "2026-10-02"
    assert item.decided_value == {"daily_budget": "50", "history": ({"at": "2026-10-01"},)}
    assert item.platform_value is None
    with pytest.raises(TypeError):
        item.decided_value["daily_budget"] = "70"  # type: ignore[index]
    with pytest.raises(TypeError, match=r"DiscrepancyInfo\.platform_value must be a mapping"):
        dataclasses.replace(item, platform_value="60")  # type: ignore[arg-type]


def test_crm_data_copies_its_collections_read_only() -> None:
    contacts: dict[str, Any] = {"1": ContactInfo("1")}
    associations: dict[Any, Any] = {("contact", "1", "deal"): ["9"]}
    fetches: dict[Any, Any] = {
        ("contact", "1", "deal"): AssociationFetchInfo("contact", "1", "deal", "ok", "ok")
    }
    deal_ids = ["9"]
    crm = CrmData(
        HUBSPOT,
        _bindings(),
        contacts=contacts,
        associations=associations,
        fetches=fetches,
        window_deal_ids=tuple(deal_ids),
    )
    contacts["2"] = ContactInfo("2")
    associations[("contact", "1", "deal")].append("10")
    associations[("contact", "1", "meeting")] = ["5"]
    fetches.clear()
    assert set(crm.contacts) == {"1"}
    assert crm.targets("contact", "1", "deal") == ("9",)
    assert type(crm.targets("contact", "1", "deal")) is tuple
    assert crm.targets("contact", "1", "meeting") == ()
    assert crm.association_known("contact", "1", "deal")
    with pytest.raises(TypeError):
        crm.contacts["3"] = ContactInfo("3")  # type: ignore[index]
    with pytest.raises(TypeError):
        crm.associations[("contact", "1", "deal")] = ("x",)  # type: ignore[index]
    with pytest.raises(TypeError):
        crm.fetches[("contact", "1", "deal")] = None  # type: ignore[index]
    with pytest.raises(TypeError, match=r"CrmData\.associations\[\('contact', '1', 'deal'\)\]"):
        CrmData(HUBSPOT, _bindings(), associations={("contact", "1", "deal"): "9"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match=r"CrmData\.deals must be a mapping"):
        CrmData(HUBSPOT, _bindings(), deals=["9"])  # type: ignore[arg-type]


def test_context_copies_its_collections_read_only_but_keeps_the_cache_writable(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    account = AccountInfo(1, "google", "1234567890", "Main", "USD", "America/New_York")
    first_day = DayFact(date(2026, 10, 1), Decimal("5"), 100, 4)
    snapshot = Observation(NOW, status="active", payload={"labels": ["a"]})
    lead = LeadInfo(1, "abc", 1, None, None, "urn:li:leadGenForm:1", 2, NOW, "matched", "901")
    datasets: dict[str, Any] = {"google": GOOGLE, "linkedin": LINKEDIN_SIM, "hubspot": HUBSPOT}
    accounts: dict[int, Any] = {1: account}
    campaign_days: dict[int, Any] = {5: [first_day]}
    observations: dict[Any, Any] = {("campaign", 5): [snapshot]}
    leads = [lead]
    segment_windows: list[Any] = [[date(2026, 9, 7), date(2026, 10, 4)]]
    context = _context(
        thresholds,
        window,
        datasets=datasets,
        accounts=accounts,
        campaign_days=campaign_days,
        observations=observations,
        leads=leads,
        segment_windows=segment_windows,
    )
    datasets.pop("google")
    accounts[2] = AccountInfo(2, "google", "2234567890", "Other", "EUR")
    campaign_days[5].append(DayFact(date(2026, 10, 2), Decimal("6"), 90, 3))
    campaign_days[6] = []
    observations[("campaign", 5)].append(Observation(NOW))
    leads.append(dataclasses.replace(lead, id=2))
    segment_windows.append([date(2026, 9, 21), date(2026, 10, 4)])
    assert context.dataset("google") == GOOGLE
    assert context.accounts == {1: account}
    assert context.campaign_days == {5: (first_day,)}
    assert context.observations == {("campaign", 5): (snapshot,)}
    assert context.leads == (lead,)
    assert context.segment_windows == ((date(2026, 9, 7), date(2026, 10, 4)),)
    assert (window.start, window.end) in context.segment_windows
    for name in ("search_terms", "segments", "discrepancies", "feedback"):
        assert getattr(context, name) == ()
    with pytest.raises(TypeError):
        context.accounts[3] = account  # type: ignore[index]
    with pytest.raises(TypeError):
        context.campaign_days[7] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        context.datasets["google"] = GOOGLE  # type: ignore[index]
    with pytest.raises(AttributeError):
        context.campaign_days[5].append(first_day)  # type: ignore[attr-defined]
    context.cache["memo"] = 1
    assert context.cache == {"memo": 1}
    assert type(context.cache) is dict
    assert context == dataclasses.replace(context, cache={})
    with pytest.raises(TypeError, match=r"AnalysisContext\.campaign_days\[5\] must be a tuple"):
        _context(thresholds, window, campaign_days={5: first_day})
    with pytest.raises(TypeError, match=r"AnalysisContext\.accounts must be a mapping"):
        _context(thresholds, window, accounts=[account])
    with pytest.raises(TypeError, match=r"AnalysisContext\.leads must be a tuple"):
        _context(thresholds, window, leads=None)


def test_context_copies_latest_observations_read_only(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    # B1, B2 and B3 size their proposals from the newest stored snapshot, so a change to the
    # caller's mapping after construction must not resize them.
    current = Observation(NOW, status="active", budget_daily=Decimal("100.00"))
    raised = dataclasses.replace(current, budget_daily=Decimal("200.00"))
    latest: dict[Any, Any] = {("campaign", 5): current, ("budget", 9): current}
    context = _context(thresholds, window, latest_observations=latest)
    latest[("campaign", 5)] = raised
    latest[("budget", 10)] = raised
    del latest[("budget", 9)]
    assert context.latest_observations == {("campaign", 5): current, ("budget", 9): current}
    assert list(context.latest_observations) == [("campaign", 5), ("budget", 9)]
    assert type(context.latest_observations) is MappingProxyType
    with pytest.raises(TypeError):
        context.latest_observations[("campaign", 5)] = raised  # type: ignore[index]
    with pytest.raises(TypeError):
        del context.latest_observations[("budget", 9)]  # type: ignore[attr-defined]
    # A read-only view over the caller's dict is copied too: the dict behind it stays theirs.
    proxied = _context(thresholds, window, latest_observations=MappingProxyType(latest))
    latest.clear()
    assert proxied.latest_observations == {("campaign", 5): raised, ("budget", 10): raised}
    replaced = dataclasses.replace(context, latest_observations=latest)
    latest[("campaign", 5)] = current
    assert replaced.latest_observations == {}
    assert context.latest_observations[("campaign", 5)] is current
    with pytest.raises(TypeError, match=r"AnalysisContext\.latest_observations must be a mapping"):
        _context(thresholds, window, latest_observations=[current])


def test_every_context_collection_except_the_cache_is_copied_on_construction(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    # A collection field added to AnalysisContext must join one of the copy lists, or the
    # caller's later changes would reach the run's inputs.
    hints = get_type_hints(AnalysisContext)
    mappings = {name for name, hint in hints.items() if get_origin(hint) is Mapping}
    series = {name for name in mappings if get_origin(get_args(hints[name])[1]) is tuple}
    sequences = {name for name, hint in hints.items() if get_origin(hint) is tuple}
    records = ctx_module._CONTEXT_RECORD_MAPPINGS
    assert len(set(records)) == len(records)
    assert set(records) == mappings - series
    assert sorted(ctx_module._CONTEXT_SEQUENCE_MAPPINGS) == sorted(series)
    assert sorted((*ctx_module._CONTEXT_SEQUENCES, "segment_windows")) == sorted(sequences)
    assert {name for name, hint in hints.items() if get_origin(hint) in (dict, list, set)} == {
        "cache"
    }
    passed: dict[str, Any] = {name: {} for name in mappings}
    passed.update({name: [] for name in sequences})
    context = _context(thresholds, window, **passed)
    for name in mappings:
        assert type(getattr(context, name)) is MappingProxyType, name
    for name in sequences:
        assert type(getattr(context, name)) is tuple, name
    assert type(context.cache) is dict


def test_make_evidence_records_window_thresholds_datasets_and_provenance(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    covered = date(2026, 10, 4)
    freshness = {
        (12, "linkedin.leads"): Freshness(
            12, "linkedin.leads", NOW, NOW, None, covered, "complete"
        ),
        (14, "hubspot.contacts"): Freshness(
            14, "hubspot.contacts", NOW, NOW, None, covered, "incomplete", "partition limit"
        ),
    }
    context = _context(thresholds, window, freshness=freshness)
    evidence = context.make_evidence(
        required_datasets=(
            "linkedin.leads",
            "hubspot.contacts",
            "hubspot.associations",
            "linkedin.leads",
        ),
        crm_scope="linkedin",
        metrics=(Metric("leads", "Leads", 25, "leads"),),
        baselines=(Baseline("ql_rate", "Account QL rate", Decimal("0.1")),),
        sample=Sample(25, 20, "eligible contacts"),
        threshold_keys=("crm.veto_min_leads", "attribution.floor_share"),
        extra={"matured": 25},
    )
    assert json_value(evidence.window) == window.to_json()
    assert evidence.thresholds == {"crm.veto_min_leads": 20, "attribution.floor_share": 0.1}
    assert evidence.datasets == (
        DatasetStatus("linkedin.leads", "complete", covered),
        DatasetStatus("hubspot.contacts", "incomplete", covered),
        DatasetStatus("hubspot.associations", "unknown", None),
    )
    assert evidence.provenance == (
        ProvenanceEntry("linkedin", 12, "simulated", "primary"),
        ProvenanceEntry("hubspot", 14, "simulated", "companion"),
    )
    rendered = evidence.to_json()
    assert rendered["window"]["source_days"][0]["boundary"] == "America/New_York"
    assert rendered["sample"] == {"observed": 25, "required": 20, "unit": "eligible contacts"}
    assert json.loads(json.dumps(rendered)) == rendered
    google = context.make_evidence(
        required_datasets=("google.metrics_daily", "hubspot.contacts"),
        crm_scope="google",
        metrics=(),
    )
    assert [entry.dataset_id for entry in google.provenance] == [11, 13]
    missing = _context(thresholds, window, datasets={}).make_evidence(
        required_datasets=("google.entities",), crm_scope=None, metrics=()
    )
    assert missing.datasets == (DatasetStatus("google.entities", "unknown", None),)
    assert missing.provenance == ()
    with pytest.raises(ValueError, match="dataset name"):
        context.make_evidence(required_datasets=("google.facts",), crm_scope=None, metrics=())


# -- the Google-to-CRM link (AAP 0.3.3, 0.5.4.1) ---------------------------------------------


def test_google_link_gap_names_why_google_items_have_no_crm_link(
    thresholds: Thresholds, window: AnalysisWindow
) -> None:
    """No Google CRM dataset, an unverified link and a verified link, decided in one place."""
    assert "google_link_gap" in ctx_module.__all__
    without = _context(thresholds, window, google_crm=None)
    assert google_link_gap(without) == "no active HubSpot dataset"
    # An unavailable link names its own reason: here every binding is unverified.
    assert google_link_gap(_context(thresholds, window)) == "CRM stage bindings not verified"
    not_configured = GoogleCrmBinding(
        available=False, reason="HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured"
    )
    unverified = CrmData(HUBSPOT, dataclasses.replace(_bindings(), google=not_configured))
    assert google_link_gap(_context(thresholds, window, google_crm=unverified)) == (
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured"
    )
    linked = GoogleCrmBinding(
        available=True,
        campaign_property="google_campaign_id",
        touch_date_property="google_touch_at",
    )
    verified = CrmData(HUBSPOT, dataclasses.replace(_bindings(), google=linked))
    assert google_link_gap(_context(thresholds, window, google_crm=verified)) is None
    # A LinkedIn-derived CRM dataset never stands in for the Google link, even when verified.
    companion = CrmData(COMPANION, dataclasses.replace(_bindings(), google=linked))
    stand_in = _context(thresholds, window, linkedin_crm=companion, google_crm=None)
    assert google_link_gap(stand_in) == "no active HubSpot dataset"


def test_no_crm_link_flag_renders_the_gap_and_rejects_a_blank_reason() -> None:
    """Every Google item's no_crm_link badge reads "No CRM link for Google campaigns: {gap}"."""
    assert "no_crm_link_flag" in ctx_module.__all__
    flag = no_crm_link_flag("no active HubSpot dataset")
    message = "No CRM link for Google campaigns: no active HubSpot dataset"
    assert flag == Flag(FLAG_NO_CRM_LINK, message)
    assert flag.to_json() == {
        "code": "no_crm_link",
        "message": message,
    }
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="no_crm_link reason must be a non-blank string"):
            no_crm_link_flag(blank)


def test_module_doctests() -> None:
    result = doctest.testmod(ctx_module, optionflags=doctest.ELLIPSIS)
    assert result.failed == 0
    assert result.attempted > 0


def test_every_dataset_name_resolves(thresholds: Thresholds, window: AnalysisWindow) -> None:
    context = _context(thresholds, window)
    for name in DATASETS:
        assert context.dataset_id_for(name, crm_scope="linkedin") is not None
