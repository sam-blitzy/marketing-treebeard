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
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    json_value,
    objective_group,
    search_term_external_id,
    segment_external_id,
    term_cluster_external_id,
    unverified_message,
)
from ads_engine.analysis.thresholds import CrmBindings, Thresholds
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
    assert extended.extra == {"a": 1, "depends_on": [{"type": "campaign", "id": 7}]}
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
    assert candidate.sort_key == ((Decimal("-5"),), "Brand", "google:campaign:123")
    assert candidate.spend_targets == frozenset({("campaign", 7), ("budget", 2)})
    assert not candidate.is_discrepancy
    held = dataclasses.replace(candidate, held=True, held_reason="Resolve discrepancy first")
    assert held.held
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.held = True  # type: ignore[misc]


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
    assert evidence.window == window.to_json()
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


def test_module_doctests() -> None:
    result = doctest.testmod(ctx_module, optionflags=doctest.ELLIPSIS)
    assert result.failed == 0
    assert result.attempted > 0


def test_every_dataset_name_resolves(thresholds: Thresholds, window: AnalysisWindow) -> None:
    context = _context(thresholds, window)
    for name in DATASETS:
        assert context.dataset_id_for(name, crm_scope="linkedin") is not None
