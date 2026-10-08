"""Unit tests for ``ads_engine.api.schemas``: the ``/api/v1`` request and response contract.

Each test asserts behaviour that AAP 0.5.3 or the module's docstrings set:

* shared scalars: :data:`Money` refuses floats, booleans, non-finite and over-precise
  amounts, serialises as a fixed-point decimal string, and publishes a request schema of
  decimal strings or whole integers only; :data:`MetricValue` renders a
  ``Decimal`` exactly as ``analysis.context.json_value`` freezes it and keeps ``None``
  ("unavailable") as ``null``, never ``0``;
* vocabularies: every ``Literal`` holds exactly the AAP values and its database or analysis
  tuple, and :func:`_check_vocabularies` raises on drift in values, count or section order;
* base classes: responses ignore unknown keys and validate from ORM attributes, requests
  forbid unknown keys, and no field is aliased, so the wire names are the snake_case field
  names; a response emits every ``None`` default as ``null`` and publishes every field as
  required, while a defaulted request field stays optional;
* requests: :class:`LoginRequest`, :class:`CreateRunRequest` (whose dates are exactly
  ``YYYY-MM-DD`` strings or plain ``date`` objects, :data:`CalendarDate`, never timestamps or
  datetimes, and whose window bounds ``AnalysisWindow.custom`` enforces),
  :class:`FeedbackRequest`, :class:`DecisionRequest`,
  :class:`DecisionPatchIn`, :class:`IntendedStateIn` and :class:`ResolveRequest`, whose ids
  stop at :data:`BIGINT_MAX` and whose email and notes refuse U+0000 and text UTF-8 cannot
  encode (:data:`StorableText`), while the memory-only password takes any character;
* responses: every ``from_row`` / ``from_thresholds`` / ``from_asset`` builder, the
  recommendations page with its five-section rule, evidence exactly as the runner freezes
  it, sources, configuration, health and the error shape, each against the AAP 0.5.3 keys.

No test reads settings, the database or the network: ORM rows are transient instances and
the configuration is the shipped ``config/`` directory.
"""

import copy
import doctest
import json
import re
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeAliasType, get_args
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import BaseModel, ValidationError

import ads_engine.api.schemas as schemas
from ads_engine.analysis import context
from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CONTENT_ASSETS_FILE,
    HUBSPOT_MAPPING_FILE,
    THRESHOLDS_FILE,
    ContentAsset,
    CrmBindings,
    GoogleCrmBinding,
    ResolvedBinding,
    StageBinding,
    Thresholds,
)
from ads_engine.analysis.window import AnalysisWindow, InvalidWindow, SourceDayInfo
from ads_engine.api.schemas import (
    ERROR_RESPONSES,
    NOTE_MAX_LENGTH,
    Banner,
    Baseline,
    ConfigView,
    ContentAssetView,
    CreateRunRequest,
    CrmBindingsView,
    DatasetFreshnessView,
    DatasetView,
    Decision,
    DecisionPatchIn,
    DecisionRequest,
    Discrepancy,
    EntityView,
    ErrorDetail,
    ErrorResponse,
    Evidence,
    EvidenceWindow,
    Feedback,
    FeedbackRequest,
    Finding,
    Flag,
    GoogleCrmBindingView,
    Group,
    Health,
    HubSpotMappingView,
    IntendedStateIn,
    Item,
    ItemOverlay,
    LoginRequest,
    Me,
    Metric,
    Overlay,
    Projection,
    RecommendationsPage,
    ResolvedBindingView,
    ResolveRequest,
    Run,
    Sample,
    Section,
    Source,
    StageBindingView,
    StatTest,
    SubjectView,
    SyncRequestView,
    SyncRunView,
    WindowConfig,
    Withheld,
    WithheldSample,
)
from ads_engine.db.models.analysis import (
    AnalysisRun,
    RecommendationFeedback,
    RunFinding,
    RunWithheld,
)
from ads_engine.db.models.decisions import DISCREPANCY_RESOLUTIONS
from ads_engine.db.models.decisions import Decision as DecisionRow
from ads_engine.db.models.decisions import Discrepancy as DiscrepancyRow
from ads_engine.db.models.ops import (
    AppUser,
    DatasetFreshness,
    SourceDataset,
    SyncRequest,
    SyncRun,
)
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    RESOLUTIONS,
    DecisionPatch,
    ExpectedState,
    SubjectRef,
    SubjectState,
    compare,
    describe_decision,
    describe_expected,
    describe_snapshot,
    is_valid_amount,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
"""08:00 in New York: yesterday is 2026-10-05 in both day sources, the default window end."""
DEFAULT_END = date(2026, 10, 5)
NEW_YORK = SourceDayInfo("America/New_York")

SECTION_KEYS = ("keywords", "campaigns", "budget", "creative", "white_papers")
"""AAP 0.5.3: "Sections appear in the fixed order ``keywords``, ``campaigns``, ``budget``,
``creative``, ``white_papers``"."""


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def thresholds() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


def _thresholds_with(**window: int) -> Thresholds:
    """The shipped configuration with ``window.*`` keys replaced."""
    trees = {
        name: yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in CONFIG_FILES
    }
    edited = copy.deepcopy(trees[THRESHOLDS_FILE])
    edited["window"].update(window)
    return Thresholds.from_trees(
        thresholds=edited,
        content_assets=trees[CONTENT_ASSETS_FILE],
        hubspot_mapping=trees[HUBSPOT_MAPPING_FILE],
        source_dir=CONFIG_DIR,
    )


def _drop_none(value: Any) -> Any:
    """Remove ``None``-valued keys from every mapping, recursively."""
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value


def _source_days() -> list[dict[str, str]]:
    return [
        {"source": "google", "start": "2026-09-08", "end": "2026-10-05", "boundary": "UTC"},
        {"source": "linkedin", "start": "2026-09-08", "end": "2026-10-05", "boundary": "UTC"},
    ]


def _run_row(**overrides: Any) -> AnalysisRun:
    values: dict[str, Any] = {
        "id": 41,
        "kind": "default",
        "window_start": date(2026, 9, 8),
        "window_end": DEFAULT_END,
        "source_day_boundaries": _source_days(),
        "trigger": "scheduled",
        "data_modes": {"google": "simulated", "linkedin": "simulated", "hubspot": "live"},
        "dataset_ids": {"google": 1, "linkedin": 2, "hubspot": 3},
        "config_hash": "c" * 64,
        "status": "succeeded",
        "started_at": NOW - timedelta(minutes=5),
        "finished_at": NOW,
    }
    values.update(overrides)
    return AnalysisRun(**values)


def _run(thresholds: Thresholds) -> Run:
    return Run.from_row(_run_row(), thresholds=thresholds, display_timezone="America/New_York")


def _evidence() -> dict[str, Any]:
    return {
        "window": {
            "start": "2026-09-08",
            "end": "2026-10-05",
            "days": 28,
            "source_days": _source_days(),
        },
        "metrics": [
            {"key": "spend", "label": "Spend", "value": "412.500000", "unit": "currency"},
        ],
        "baselines": [],
        "thresholds": {"k1.alpha": "0.05"},
        "datasets": [
            {
                "dataset": "google.search_terms",
                "completeness": "complete",
                "covered_through": "2026-10-05",
            }
        ],
        "provenance": [
            {"source": "google", "dataset_id": 1, "mode": "simulated", "role": "primary"},
        ],
    }


def _overlay_now() -> dict[str, Any]:
    return {
        "held_now": False,
        "historical": False,
        "data_status_now": "simulated",
        "flags_now": [],
    }


def _item(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 501,
        "rank": 1,
        "rule_id": "K1",
        "action": "Add 'free trial' as a negative keyword",
        "reason": "240 clicks and 0 conversions against a 1.2% baseline",
        "entity": {"source": "google", "type": "search_term", "id": 77, "name": "free trial"},
        "evidence": _evidence(),
        "flags": [{"code": "simulated", "message": "Simulated data"}],
        "data_status": "simulated",
        "held": False,
        "current": _overlay_now(),
    }
    values.update(overrides)
    return values


def _section(key: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "key": key,
        "title": context.SECTION_TITLES[key],
        "groups": [],
        "findings": [],
        "withheld": [],
        "suppressed": {},
    }
    values.update(overrides)
    return values


def _page(run: Run, keys: tuple[str, ...] = SECTION_KEYS) -> dict[str, Any]:
    return {
        "run": run.model_dump(),
        "overlay": {
            "open_discrepancies": 2,
            "source_modes": {"google": "simulated", "linkedin": "simulated", "hubspot": "live"},
            "stale_datasets": [],
            "incomplete_datasets": ["google.search_terms"],
            "computed_at": NOW,
        },
        "banners": [
            {
                "code": "open_discrepancies",
                "tone": "critical",
                "message": "2 unresolved discrepancies",
                "count": 2,
            }
        ],
        "sections": [_section(key) for key in keys],
    }


def _subject(**overrides: Any) -> SubjectView:
    values: dict[str, Any] = {
        "type": "campaign",
        "id": 12,
        "name": "Brand - US",
        "source": "google",
        "currency": "USD",
    }
    values.update(overrides)
    return SubjectView.model_validate(values)


def _decision_row(**overrides: Any) -> DecisionRow:
    values: dict[str, Any] = {
        "id": 900,
        "source": "google",
        "dataset_id": 1,
        "subject_type": "campaign",
        "subject_id": 12,
        "shape": "own_budget",
        "status": "active",
        "daily_budget": Decimal("120.500000"),
        "total_budget": None,
        "budget_id": None,
        "tombstone": False,
        "decided_at": NOW,
        "decided_by": "admin@example.com",
        "origin": "user",
        "recommendation_id": 501,
        "patch": {"daily_budget": "120.50"},
        "note": "Raise after the B1 card",
    }
    values.update(overrides)
    return DecisionRow(**values)


def _discrepancy_row(**overrides: Any) -> DiscrepancyRow:
    values: dict[str, Any] = {
        "id": 300,
        "source": "google",
        "dataset_id": 1,
        "subject_type": "campaign",
        "subject_id": 12,
        "kind": "status",
        "decided_value": {"status": "active"},
        "platform_value": {"status": "paused"},
        "snapshot_id": 88,
        "detected_at": NOW,
        "state": "open",
        "resolved_at": None,
        "resolution": None,
    }
    values.update(overrides)
    return DiscrepancyRow(**values)


def _error_types(error: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    return [item["type"] for item in error.value.errors()]


# ---------------------------------------------------------------------------
# Money and metric values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [120.5, 0.1, 120.0, True, False])
def test_money_input_refuses_floats_and_booleans(value: object) -> None:
    with pytest.raises(ValueError, match=r'send amounts as decimal strings such as "120\.50"'):
        schemas._money_input(value)
    with pytest.raises(ValidationError, match=r"send amounts as decimal strings") as error:
        DecisionPatchIn.model_validate({"daily_budget": value})
    assert error.value.errors()[0]["loc"] == ("daily_budget",)


@pytest.mark.parametrize("value", ["120.50", 120, Decimal("0.000001"), None])
def test_money_input_passes_strings_integers_and_decimals_through(value: object) -> None:
    assert schemas._money_input(value) is value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120.50", Decimal("120.50")),
        (" 12.5 ", Decimal("12.5")),
        (120, Decimal(120)),
        (Decimal("0.000001"), Decimal("0.000001")),
        ("999999999999.999999", Decimal("999999999999.999999")),
    ],
)
def test_money_accepts_decimal_strings_whole_integers_and_decimals(
    value: object, expected: Decimal
) -> None:
    amount = DecisionPatchIn.model_validate({"total_budget": value}).total_budget
    assert isinstance(amount, Decimal)
    assert amount == expected


def test_money_on_the_json_wire_accepts_strings_and_integers_but_not_fractional_numbers() -> None:
    assert DecisionPatchIn.model_validate_json('{"daily_budget": "120.50"}').daily_budget == (
        Decimal("120.50")
    )
    assert DecisionPatchIn.model_validate_json('{"daily_budget": 120}').daily_budget == 120
    with pytest.raises(ValidationError, match=r"not JSON numbers"):
        DecisionPatchIn.model_validate_json('{"daily_budget": 120.5}')


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        ("NaN", "finite_number"),
        ("Infinity", "finite_number"),
        ("-Infinity", "finite_number"),
        ("1.1234567", "decimal_max_places"),
        (10**18, "decimal_max_digits"),
        ("1000000000000.000001", "decimal_max_digits"),
        ("twelve", "decimal_parsing"),
    ],
)
def test_money_refuses_non_finite_over_precise_and_unparsable_amounts(
    value: object, error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        DecisionPatchIn.model_validate({"daily_budget": value})
    assert _error_types(error) == [error_type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120.50", "120.50"),
        ("1E+2", "100"),
        ("2.50E+1", "25.0"),
        ("0.0000000", "0.0000000"),
    ],
)
def test_money_serialises_as_fixed_point_text_in_json_and_stays_a_decimal_in_python(
    value: str, expected: str
) -> None:
    patch = DecisionPatchIn.model_validate({"daily_budget": value})
    assert patch.model_dump(mode="json")["daily_budget"] == expected
    assert f'"daily_budget":"{expected}"' in patch.model_dump_json()
    python = patch.model_dump()["daily_budget"]
    assert isinstance(python, Decimal)
    assert python == Decimal(value)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("12.340000"),
        Decimal("1E+2"),
        Decimal("-0.5"),
        Decimal("0E-6"),
        Decimal("1.5E-5"),
    ],
)
def test_metric_input_renders_a_decimal_exactly_as_frozen_evidence_does(value: Decimal) -> None:
    rendered = schemas._metric_input(value)
    assert rendered == context.json_value(value) == format(value, "f")
    assert isinstance(rendered, str)
    assert "E" not in rendered


@pytest.mark.parametrize("value", [None, 0, 240, "n/a", "12.340000", True, 0.25])
def test_metric_input_passes_every_other_value_through(value: object) -> None:
    assert schemas._metric_input(value) is value


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_metric_input_refuses_a_non_finite_decimal(value: Decimal) -> None:
    with pytest.raises(ValueError, match=r"metric values must be finite"):
        schemas._metric_input(value)
    with pytest.raises(ValidationError, match=r"metric values must be finite"):
        Metric(key="spend", label="Spend", value=value, unit="currency")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("412.500000"), "412.500000"),
        (Decimal("1E+2"), "100"),
        (240, 240),
        (0, 0),
        ("n/a", "n/a"),
        (True, True),
        (0.25, 0.25),
        (None, None),
    ],
)
def test_metric_value_keeps_one_json_type_per_value(value: object, expected: object) -> None:
    metric = Metric(key="spend", label="Spend", value=value, unit="currency")
    dumped = metric.model_dump(mode="json")["value"]
    assert dumped == expected
    assert type(dumped) is type(expected)


def test_a_missing_number_stays_null_and_never_becomes_zero() -> None:
    models: list[BaseModel] = [
        Metric(key="cpa", label="Cost per conversion", value=None, unit="currency"),
        Baseline(key="account_cost_per_outcome", label="Account CPA", value=None),
        Sample(observed=None, required=None, unit="clicks"),
        StatTest(name="Fisher's exact test", p_value=None, alpha=Decimal("0.05")),
        WithheldSample(observed=None, required=None, unit=None),
        Projection(value=None, unit="impressions", basis="projected through the open rate"),
    ]
    for model in models:
        dumped = model.model_dump(mode="json")
        nulls = {key for key, value in dumped.items() if value is None}
        assert nulls, type(model).__name__
        assert 0 not in dumped.values(), type(model).__name__
    assert StatTest(name="z", p_value="0.01", alpha="0.05").p_adjusted is None


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

AAP_VOCABULARIES: list[tuple[object, tuple[str, ...]]] = [
    (schemas.SourceName, ("google", "linkedin", "hubspot")),
    (schemas.Mode, ("live", "simulated")),
    (schemas.DatasetRole, ("primary", "companion")),
    (schemas.DatasetState, ("building", "active", "retired")),
    (schemas.Completeness, ("complete", "incomplete", "unknown")),
    (schemas.ConnectionStatus, ("ok", "stale", "denied", "error")),
    (schemas.SyncRunStatus, ("running", "succeeded", "failed")),
    (schemas.RunKind, ("default", "custom")),
    (schemas.RunTrigger, ("scheduled", "event", "manual")),
    (schemas.RunStatus, ("running", "succeeded", "failed")),
    (schemas.DataStatus, ("live", "simulated", "stale", "mixed")),
    (schemas.FeedbackKind, ("dismiss", "snooze", "done")),
    (schemas.SubjectType, ("campaign", "budget")),
    (schemas.DecisionShape, ("own_budget", "shared_member", "budget")),
    (schemas.DecisionOrigin, ("baseline", "user", "recommendation", "resolution")),
    (schemas.CampaignStatus, ("active", "paused", "ended")),
    (schemas.DiscrepancyKind, ("status", "budget", "untracked", "missing")),
    (schemas.DiscrepancyState, ("open", "awaiting_platform", "resolved")),
    (
        schemas.ResolutionKey,
        (
            "platform_correct",
            "decision_correct",
            "adopt_platform",
            "record_intended",
            "acknowledge_removal",
        ),
    ),
    (
        schemas.FlagCode,
        (
            "simulated",
            "stale",
            "incomplete",
            "short_window",
            "unverified_discrepancies",
            "attribution_floor",
            "creative_floor",
            "deal_floor",
            "unknown_crm_evidence",
            "meeting_time_proxy",
            "pipeline_unavailable",
            "no_crm_link",
            "not_distinguishable",
            "search_term_coverage",
            "shared_budget",
        ),
    ),
    (
        schemas.FindingCode,
        (
            "no_crm_link",
            "search_term_coverage",
            "k1_no_baseline",
            "segment_window_unavailable",
            "pipeline_unavailable",
            "skipped_stage",
            "conversion_tracking_absent",
            "incomplete_dataset",
        ),
    ),
    (schemas.BannerTone, tuple(context.TONES)),
]


@pytest.mark.parametrize(("alias", "values"), AAP_VOCABULARIES)
def test_each_vocabulary_holds_exactly_the_aap_values(
    alias: object, values: tuple[str, ...]
) -> None:
    literal_values = get_args(alias)
    assert len(literal_values) == len(set(literal_values)) == len(values)
    assert set(literal_values) == set(values)


def test_section_key_keeps_the_fixed_page_order() -> None:
    assert get_args(schemas.SectionKey) == SECTION_KEYS == tuple(context.SECTION_ORDER)


def test_vocabularies_match_their_tuples_on_the_shipped_code() -> None:
    assert schemas._check_vocabularies() is None


@pytest.mark.parametrize(
    ("attribute", "value", "alias"),
    [
        ("SOURCES", ("google", "linkedin"), "SourceName"),
        ("MODES", ("live", "simulated", "replayed"), "Mode"),
        ("COMPLETENESS", ("complete", "incomplete"), "Completeness"),
        ("RECOMMENDATION_SECTIONS", SECTION_KEYS[:4], "SectionKey"),
        ("DATA_STATUSES", ("live", "simulated", "stale"), "DataStatus"),
        ("DATA_STATUS_VALUES", ("live", "simulated", "stale", "partial"), "DataStatus"),
        ("DECISION_SHAPES", ("own_budget", "budget"), "DecisionShape"),
        ("CAMPAIGN_STATUSES", ("active", "paused", "ended", "removed"), "CampaignStatus"),
        ("DISCREPANCY_RESOLUTIONS", DISCREPANCY_RESOLUTIONS[:4], "ResolutionKey"),
        ("TONES", ("neutral", "info"), "BannerTone"),
        ("FLAG_CODES", (*context.FLAG_CODES, "new_flag"), "FlagCode"),
        ("FINDING_CODES", context.FINDING_CODES[1:], "FindingCode"),
    ],
)
def test_check_vocabularies_raises_when_a_tuple_drifts(
    monkeypatch: pytest.MonkeyPatch, attribute: str, value: tuple[str, ...], alias: str
) -> None:
    monkeypatch.setattr(schemas, attribute, value)
    with pytest.raises(RuntimeError, match=rf"^api\.schemas\.{alias} \[.*\] does not match "):
        schemas._check_vocabularies()


def test_check_vocabularies_raises_when_a_tuple_repeats_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas, "SOURCES", ("google", "linkedin", "hubspot", "google"))
    with pytest.raises(RuntimeError, match=r"db\.models\.ops\.SOURCES"):
        schemas._check_vocabularies()


def test_check_vocabularies_raises_when_the_section_order_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schemas, "SECTION_ORDER", tuple(reversed(SECTION_KEYS)))
    with pytest.raises(
        RuntimeError,
        match=r"^api\.schemas\.SectionKey order \['keywords', .*\] does not match "
        r"analysis\.context\.SECTION_ORDER \['white_papers', ",
    ):
        schemas._check_vocabularies()


# ---------------------------------------------------------------------------
# Base classes, field names and the error shape
# ---------------------------------------------------------------------------


def _public_models() -> Iterator[type[BaseModel]]:
    for name in schemas.__all__:
        value = getattr(schemas, name)
        if isinstance(value, type) and issubclass(value, BaseModel):
            yield value


def test_every_response_model_ignores_and_every_request_model_forbids_unknown_keys() -> None:
    models = list(_public_models())
    assert models
    for model in models:
        if issubclass(model, schemas.RequestModel):
            assert model.model_config.get("extra") == "forbid", model.__name__
        else:
            assert issubclass(model, schemas.ApiModel), model.__name__
            assert model.model_config.get("extra") == "ignore", model.__name__
            assert model.model_config.get("from_attributes") is True, model.__name__


def test_no_field_is_aliased_so_the_wire_names_are_the_snake_case_field_names() -> None:
    for model in _public_models():
        for name, field in model.model_fields.items():
            assert field.alias is None, f"{model.__name__}.{name}"
            assert field.validation_alias is None, f"{model.__name__}.{name}"
            assert field.serialization_alias is None, f"{model.__name__}.{name}"
            assert name == name.lower(), f"{model.__name__}.{name}"
        assert model.model_config.get("alias_generator") is None, model.__name__


def test_a_response_model_drops_keys_the_contract_does_not_define() -> None:
    payload = {**_evidence(), "computed_by": "runner v2"}
    payload["metrics"] = [{**payload["metrics"][0], "trend": "up"}]
    evidence = Evidence.model_validate(payload)
    dumped = evidence.model_dump()
    assert "computed_by" not in dumped
    assert "trend" not in dumped["metrics"][0]


def test_a_response_model_validates_from_orm_attributes() -> None:
    user = AppUser(id=1, email="admin@example.com", password_hash="argon2-hash-placeholder")
    assert Me.model_validate(user).model_dump() == {"email": "admin@example.com"}


@pytest.mark.parametrize(
    ("model", "payload", "location"),
    [
        (LoginRequest, {"email": "a@example.com", "password": "pw", "remember": True}, "remember"),
        (CreateRunRequest, {"window_start": "2026-09-08", "window_end": "2026-10-05", "x": 1}, "x"),
        (FeedbackRequest, {"kind": "dismiss", "until": "2026-11-01"}, "until"),
        (
            DecisionRequest,
            {"subject": {"type": "campaign", "id": 1}, "patch": {}, "origin": "user"},
            "origin",
        ),
        (
            DecisionRequest,
            {"subject": {"type": "campaign", "id": 1, "name": "Brand"}, "patch": {}},
            "subject.name",
        ),
        (
            DecisionRequest,
            {"subject": {"type": "campaign", "id": 1}, "patch": {"budget_id": 4}},
            "patch.budget_id",
        ),
        (ResolveRequest, {"resolution": "platform_correct", "force": True}, "force"),
        (
            ResolveRequest,
            {"resolution": "record_intended", "intended_state": {"budget_id": 4}},
            "intended_state.budget_id",
        ),
    ],
)
def test_a_request_model_refuses_an_unknown_field_with_422(
    model: type[BaseModel], payload: dict[str, Any], location: str
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    errors = error.value.errors()
    assert [item["type"] for item in errors] == ["extra_forbidden"]
    assert ".".join(str(part) for part in errors[0]["loc"]) == location


def test_the_error_response_has_the_aap_error_shape() -> None:
    body = {"error": {"code": "subject_retired", "message": "The dataset is no longer active"}}
    response = ErrorResponse(error=ErrorDetail(**body["error"]))
    assert response.model_dump() == body
    assert ErrorResponse.model_validate(body) == response
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate({"error": {"code": "run_in_progress"}})


def test_error_responses_document_every_aap_status() -> None:
    assert set(ERROR_RESPONSES) == {401, 403, 404, 409, 422, 429}
    for status, entry in ERROR_RESPONSES.items():
        assert entry["model"] is ErrorResponse, status
        assert entry["description"].strip(), status


def test_the_module_docstring_example_holds() -> None:
    result = doctest.testmod(schemas, verbose=False)
    assert result.attempted >= 1
    assert result.failed == 0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_login_request_trims_and_lowercases_the_email() -> None:
    request = LoginRequest.model_validate({"email": "  Admin@Example.COM\t", "password": "secret"})
    assert request.email == "admin@example.com"


def test_login_request_keeps_the_password_exactly_as_typed() -> None:
    password = "  Pa ss\tWORD  "
    assert LoginRequest(email="admin@example.com", password=password).password == password


def test_login_request_checks_the_email_length_after_trimming() -> None:
    longest = "a" * 310 + "@example.c"
    assert len(longest) == 320
    assert LoginRequest(email=f"   {longest}   ", password="pw").email == longest
    with pytest.raises(ValidationError) as error:
        LoginRequest(email=longest + "o", password="pw")
    assert _error_types(error) == ["string_too_long"]
    with pytest.raises(ValidationError) as error:
        LoginRequest(email="   ", password="pw")
    assert _error_types(error) == ["string_too_short"]


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"email": 5, "password": "pw"}, "string_type"),
        ({"email": None, "password": "pw"}, "string_type"),
        ({"email": "admin@example.com", "password": ""}, "string_too_short"),
        ({"email": "admin@example.com", "password": "x" * 1025}, "string_too_long"),
        ({"email": "admin@example.com"}, "missing"),
        ({"password": "pw"}, "missing"),
    ],
)
def test_login_request_refuses_bad_types_and_lengths(
    payload: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        LoginRequest.model_validate(payload)
    assert _error_types(error) == [error_type]


def test_login_request_accepts_the_longest_password() -> None:
    assert len(LoginRequest(email="a@example.com", password="x" * 1024).password) == 1024


def test_me_is_the_email_only() -> None:
    assert Me(email="admin@example.com").model_dump(mode="json") == {"email": "admin@example.com"}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

RUN_KEYS = {
    "id",
    "kind",
    "window_start",
    "window_end",
    "days",
    "source_days",
    "display_timezone",
    "short_window",
    "default_days",
    "trusted_min_days",
    "trigger",
    "data_modes",
    "config_hash",
    "status",
    "finished_at",
}
"""AAP 0.5.3 ``Run``."""


def test_run_from_row_reproduces_the_aap_run_shape(thresholds: Thresholds) -> None:
    run = Run.from_row(
        _run_row(
            source_day_boundaries=list(reversed(_source_days())),
            data_modes={"google": "simulated", "linkedin": "live"},
        ),
        thresholds=thresholds,
        display_timezone="Europe/London",
    )
    dumped = run.model_dump(mode="json")
    assert set(dumped) == RUN_KEYS
    assert dumped["id"] == 41
    assert dumped["kind"] == "default"
    assert (dumped["window_start"], dumped["window_end"]) == ("2026-09-08", "2026-10-05")
    assert dumped["days"] == 28
    assert [day["source"] for day in dumped["source_days"]] == ["google", "linkedin"]
    assert set(dumped["source_days"][0]) == {"source", "start", "end", "boundary"}
    assert dumped["display_timezone"] == "Europe/London"
    assert dumped["short_window"] is False
    assert (dumped["default_days"], dumped["trusted_min_days"]) == (28, 14)
    assert dumped["trigger"] == "scheduled"
    assert dumped["data_modes"] == {"google": "simulated", "linkedin": "live", "hubspot": None}
    assert dumped["config_hash"] == "c" * 64
    assert dumped["status"] == "succeeded"
    assert dumped["finished_at"] == "2026-10-06T12:00:00Z"


@pytest.mark.parametrize(
    ("trusted_min_days", "days", "short"),
    [(None, 13, True), (None, 14, False), (21, 20, True), (21, 21, False)],
)
def test_run_short_window_uses_the_configured_minimum(
    thresholds: Thresholds, trusted_min_days: int | None, days: int, short: bool
) -> None:
    config = (
        thresholds
        if trusted_min_days is None
        else _thresholds_with(trusted_min_days=trusted_min_days)
    )
    run = Run.from_row(
        _run_row(window_start=DEFAULT_END - timedelta(days=days - 1)),
        thresholds=config,
        display_timezone="UTC",
    )
    assert run.days == days
    assert run.short_window is short
    assert run.trusted_min_days == config.trusted_min_days


def test_run_from_row_handles_a_running_row_without_modes_or_boundaries(
    thresholds: Thresholds,
) -> None:
    run = Run.from_row(
        _run_row(
            kind="custom",
            trigger="manual",
            status="running",
            finished_at=None,
            data_modes=None,
            source_day_boundaries=None,
            window_start=DEFAULT_END,
        ),
        thresholds=thresholds,
        display_timezone="UTC",
    )
    assert (run.kind, run.trigger, run.status, run.finished_at) == (
        "custom",
        "manual",
        "running",
        None,
    )
    assert run.days == 1
    assert run.source_days == []
    assert run.data_modes.model_dump() == {"google": None, "linkedin": None, "hubspot": None}


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"kind": "adhoc"}, "literal_error"),
        ({"status": "cancelled"}, "literal_error"),
        ({"trigger": "cron"}, "literal_error"),
        ({"finished_at": datetime(2026, 10, 6, 12, 0)}, "timezone_aware"),  # noqa: DTZ001
        ({"data_modes": {"google": "mocked"}}, "literal_error"),
        (
            {"source_day_boundaries": [{**_source_days()[0], "source": "bing"}]},
            "literal_error",
        ),
    ],
)
def test_run_from_row_rejects_a_stored_value_outside_the_contract(
    thresholds: Thresholds, overrides: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Run.from_row(_run_row(**overrides), thresholds=thresholds, display_timezone="UTC")
    assert _error_types(error) == [error_type]


@pytest.mark.parametrize(
    ("entry", "rank"),
    [
        ({"source": "google"}, 0),
        ({"source": "linkedin"}, 1),
        ({"source": "hubspot"}, 2),
        ({"source": "bing"}, 3),
        ({}, 3),
        ("google", 3),
        (None, 3),
    ],
)
def test_source_rank_orders_by_sources_and_puts_anything_else_last(
    entry: object, rank: int
) -> None:
    assert schemas._source_rank(entry) == rank


def test_run_and_evidence_windows_match_the_analysis_window(thresholds: Thresholds) -> None:
    window = AnalysisWindow.custom(
        date(2026, 9, 1), DEFAULT_END, config=thresholds, sources=NEW_YORK, now=NOW
    )
    run = Run.from_row(
        _run_row(
            window_start=window.start,
            window_end=window.end,
            source_day_boundaries=list(reversed(window.source_days_json())),
        ),
        thresholds=thresholds,
        display_timezone="UTC",
    )
    assert run.days == window.days == 35
    assert [day.model_dump(mode="json") for day in run.source_days] == window.source_days_json()
    assert EvidenceWindow.model_validate(window.to_json()).model_dump(mode="json") == (
        window.to_json()
    )


def test_create_run_request_parses_calendar_dates() -> None:
    request = CreateRunRequest.model_validate_json(
        '{"window_start": "2026-09-08", "window_end": "2026-10-05"}'
    )
    assert (request.window_start, request.window_end) == (date(2026, 9, 8), DEFAULT_END)


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"window_start": "2026-09-08T10:30:00", "window_end": "2026-10-05"}, "value_error"),
        ({"window_start": "last month", "window_end": "2026-10-05"}, "value_error"),
        ({"window_start": "2026-09-08"}, "missing"),
    ],
)
def test_create_run_request_refuses_anything_but_two_calendar_dates(
    payload: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        CreateRunRequest.model_validate(payload)
    assert _error_types(error) == [error_type]


_REFUSED_DATE_FORMS: list[object] = [
    1679616000,
    1679616000.0,
    True,
    "1679616000",
    "2023-03-24T00:00:00",
    "2023-03-24T00:00:00Z",
    "2023-03-24T00:00:00+00:00",
    " 2023-03-24",
    "2023-03-24 ",
    "2023-03-24\n",
    "2023-3-24",
    "20230324",
    "\uff12\uff10\uff12\uff13-\uff10\uff13-\uff12\uff14",
    b"2023-03-24",
    None,
    datetime(2023, 3, 24, tzinfo=UTC).replace(tzinfo=None),
    datetime(2023, 3, 24, tzinfo=UTC),
]
"""Request dates the API refuses: UNIX timestamps (number, float, numeric string), a boolean,
datetime strings and objects (an instant, even at midnight), whitespace, a trailing newline,
unpadded, undelimited or non-ASCII digits, bytes and ``null``. Lax Pydantic ``date``
validation turns the timestamps and the midnight datetimes into 2023-03-24."""


@pytest.mark.parametrize("value", ["2026-09-01", date(2026, 9, 1)])
def test_calendar_date_input_admits_a_yyyy_mm_dd_string_and_a_plain_date(value: object) -> None:
    assert schemas._calendar_date_input(value) is value


@pytest.mark.parametrize("value", _REFUSED_DATE_FORMS)
def test_calendar_date_input_refuses_every_other_form_without_echoing_it(value: object) -> None:
    with pytest.raises(ValueError, match=r'send a calendar date as a "YYYY-MM-DD" string') as error:
        schemas._calendar_date_input(value)
    message = str(error.value)
    assert repr(value) not in message
    assert str(value).strip() not in message


def test_calendar_date_input_leaves_an_impossible_date_to_the_date_parser() -> None:
    assert schemas._calendar_date_input("2026-02-30") == "2026-02-30"


def test_create_run_request_accepts_a_yyyy_mm_dd_string_and_a_date() -> None:
    from_json = CreateRunRequest.model_validate_json(
        '{"window_start": "2026-09-01", "window_end": "2026-09-30"}'
    )
    from_python = CreateRunRequest.model_validate(
        {"window_start": "2026-09-01", "window_end": date(2026, 9, 30)}
    )
    built = CreateRunRequest(window_start=date(2026, 9, 1), window_end=date(2026, 9, 30))
    for request in (from_json, from_python, built):
        assert (request.window_start, request.window_end) == (date(2026, 9, 1), date(2026, 9, 30))
        assert type(request.window_start) is date
        assert type(request.window_end) is date


@pytest.mark.parametrize("field", ["window_start", "window_end"])
@pytest.mark.parametrize("value", _REFUSED_DATE_FORMS)
def test_create_run_request_refuses_timestamps_datetimes_and_loose_strings(
    field: str, value: object
) -> None:
    payload: dict[str, object] = {"window_start": "2026-09-01", "window_end": "2026-09-30"}
    payload[field] = value
    with pytest.raises(ValidationError) as error:
        CreateRunRequest.model_validate(payload)
    errors = error.value.errors()
    assert [(item["type"], item["loc"]) for item in errors] == [("value_error", (field,))]
    assert 'send a calendar date as a "YYYY-MM-DD" string' in errors[0]["msg"]


@pytest.mark.parametrize(
    "literal",
    [
        "1679616000",
        "1679616000.0",
        "true",
        '"1679616000"',
        '"2023-03-24T00:00:00"',
        '"2023-03-24T00:00:00Z"',
        '" 2023-03-24"',
        '"2023-03-24\\n"',
        '"2023-3-24"',
        '"20230324"',
        "null",
    ],
)
def test_create_run_request_refuses_them_on_the_json_wire_too(literal: str) -> None:
    with pytest.raises(ValidationError) as error:
        CreateRunRequest.model_validate_json(
            f'{{"window_start": {literal}, "window_end": "2026-09-30"}}'
        )
    errors = error.value.errors()
    assert [(item["type"], item["loc"]) for item in errors] == [("value_error", ("window_start",))]


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "2026-00-10", "0000-01-01"])
def test_create_run_request_refuses_an_impossible_yyyy_mm_dd_date(value: str) -> None:
    with pytest.raises(ValidationError) as error:
        CreateRunRequest.model_validate({"window_start": value, "window_end": "2026-09-30"})
    errors = error.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ("window_start",)
    assert errors[0]["type"].startswith("date_")


def test_create_run_request_publishes_both_dates_as_json_schema_date_strings() -> None:
    properties = CreateRunRequest.model_json_schema(mode="validation")["properties"]
    for field in ("window_start", "window_end"):
        published = {key: value for key, value in properties[field].items() if key != "title"}
        assert published == {"type": "string", "format": "date"}, field


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (DEFAULT_END, DEFAULT_END),
        (DEFAULT_END - timedelta(days=179), DEFAULT_END),
        (date(2026, 9, 8), date(2026, 9, 30)),
    ],
)
def test_create_run_request_windows_within_the_bounds_build_a_custom_window(
    thresholds: Thresholds, start: date, end: date
) -> None:
    request = CreateRunRequest(window_start=start, window_end=end)
    window = AnalysisWindow.custom(
        request.window_start, request.window_end, config=thresholds, sources=NEW_YORK, now=NOW
    )
    assert (window.start, window.end) == (start, end)
    assert 1 <= window.days <= thresholds.max_days


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (date(2026, 10, 2), date(2026, 10, 1), "end_before_start"),
        (DEFAULT_END - timedelta(days=180), DEFAULT_END, "too_long"),
        (date(2026, 9, 8), DEFAULT_END + timedelta(days=1), "end_after_default_end"),
    ],
)
def test_create_run_request_leaves_the_window_bounds_to_analysis_window_custom(
    thresholds: Thresholds, start: date, end: date, code: str
) -> None:
    request = CreateRunRequest(window_start=start, window_end=end)
    with pytest.raises(InvalidWindow) as error:
        AnalysisWindow.custom(
            request.window_start, request.window_end, config=thresholds, sources=NEW_YORK, now=NOW
        )
    assert error.value.code == code


# ---------------------------------------------------------------------------
# Entity, evidence and items
# ---------------------------------------------------------------------------


def test_entity_view_renders_the_stored_integer_id_as_a_decimal_string() -> None:
    ref = context.EntityRef(
        source="google", type="campaign", id=42, external_id="2233445566", name="Brand - US"
    )
    entity = EntityView.model_validate(ref.to_json())
    assert entity.id == "42"
    assert entity.model_dump(mode="json") == {
        "source": "google",
        "type": "campaign",
        "id": "42",
        "name": "Brand - US",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, "7"), (Decimal(7), "7"), ("urn:li:sponsoredCreative:9", "urn:li:sponsoredCreative:9")],
)
def test_entity_view_id_is_always_a_string(value: object, expected: str) -> None:
    entity = EntityView.model_validate(
        {"source": "linkedin", "type": "creative", "id": value, "name": "Ad 9"}
    )
    assert entity.id == expected


def test_entity_view_id_may_be_null_for_an_account_wide_entity() -> None:
    ref = context.EntityRef(
        source="google", type="account", id=None, external_id="1234567890", name=""
    )
    entity = EntityView.model_validate(ref.to_json())
    assert entity.id is None
    assert entity.name == "1234567890"


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"source": "google", "type": "campaign", "id": True, "name": "x"}, "string_type"),
        ({"source": "bing", "type": "campaign", "id": "1", "name": "x"}, "literal_error"),
        ({"source": "google", "type": "campaign", "id": "1"}, "missing"),
    ],
)
def test_entity_view_refuses_a_boolean_id_an_unknown_source_and_a_missing_key(
    payload: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        EntityView.model_validate(payload)
    assert _error_types(error) == [error_type]


EVIDENCE_KEYS = {
    "window",
    "metrics",
    "baselines",
    "sample",
    "test",
    "thresholds",
    "datasets",
    "provenance",
    "extra",
}
"""AAP 0.5.3 ``Evidence``."""


def _frozen_evidence(thresholds: Thresholds) -> context.Evidence:
    window = AnalysisWindow.custom(
        date(2026, 9, 8), DEFAULT_END, config=thresholds, sources=NEW_YORK, now=NOW
    )
    return context.Evidence(
        window=window.to_json(),
        metrics=(
            context.Metric("spend", "Spend", Decimal("412.500000"), "currency", currency="USD"),
            context.Metric("clicks", "Clicks", 240, "clicks"),
            context.Metric("contacts", "Contacts", 12, "contacts", floor=True),
            context.Metric("utilization", "Utilization", "n/a", "ratio"),
            context.Metric("ql", "Qualified Leads", None, "contacts"),
        ),
        baselines=(context.Baseline("account_cpa", "Account CPA", Decimal("38.20")),),
        sample=context.Sample(
            observed=310,
            required=1000,
            unit="form opens",
            projected=context.Projection(Decimal("2.5E+4"), "impressions", "open rate"),
        ),
        test=context.TestResult(
            name="Fisher's exact test",
            p_value=Decimal("0.012"),
            p_adjusted=Decimal("0.036"),
            alpha=Decimal("0.05"),
        ),
        thresholds={"k1.alpha": Decimal("0.05"), "k1.min_clicks": 10},
        datasets=(
            context.DatasetStatus("google.search_terms", "complete", DEFAULT_END),
            context.DatasetStatus("google.metrics_daily", "unknown", None),
        ),
        provenance=(
            context.ProvenanceEntry("google", 1, "simulated", "primary"),
            context.ProvenanceEntry("hubspot", 4, "simulated", "companion"),
        ),
        extra={
            "rows": [{"creative": "urn:li:sponsoredCreative:9", "mql": 3}],
            "adjacent_terms": ["free trial software"],
        },
    )


def test_evidence_round_trips_exactly_what_the_runner_freezes(thresholds: Thresholds) -> None:
    stored = _frozen_evidence(thresholds).to_json()
    evidence = Evidence.model_validate(stored)
    dumped = evidence.model_dump(mode="json")
    assert set(dumped) == EVIDENCE_KEYS
    assert _drop_none(dumped) == _drop_none(stored)
    assert set(dumped["metrics"][0]) == {
        "key",
        "label",
        "value",
        "unit",
        "currency",
        "floor",
        "unavailable",
    }
    assert set(dumped["baselines"][0]) == {"key", "label", "value"}
    assert set(dumped["sample"]) == {"observed", "required", "unit", "projected"}
    assert set(dumped["sample"]["projected"]) == {"value", "unit", "basis"}
    assert set(dumped["test"]) == {"name", "p_value", "p_adjusted", "alpha"}
    assert set(dumped["datasets"][0]) == {"dataset", "completeness", "covered_through"}
    assert set(dumped["provenance"][0]) == {"source", "dataset_id", "mode", "role"}
    assert dumped["sample"]["projected"]["value"] == "25000"
    assert dumped["extra"]["rows"] == [{"creative": "urn:li:sponsoredCreative:9", "mql": 3}]
    assert dumped["extra"]["adjacent_terms"] == ["free trial software"]


def test_evidence_keeps_an_unavailable_metric_null_and_flagged(thresholds: Thresholds) -> None:
    evidence = Evidence.model_validate(_frozen_evidence(thresholds).to_json())
    ql = next(metric for metric in evidence.metrics if metric.key == "ql")
    assert (ql.value, ql.unavailable) == (None, True)
    contacts = next(metric for metric in evidence.metrics if metric.key == "contacts")
    assert (contacts.value, contacts.floor, contacts.unavailable) == (12, True, None)
    assert evidence.datasets[1].covered_through is None


def test_evidence_without_sample_test_or_extra_leaves_them_null() -> None:
    evidence = Evidence.model_validate(_evidence())
    assert (evidence.sample, evidence.test, evidence.extra) == (None, None, None)


@pytest.mark.parametrize("missing", ["window", "metrics", "thresholds", "datasets", "provenance"])
def test_evidence_requires_each_frozen_part(missing: str) -> None:
    payload = _evidence()
    del payload[missing]
    with pytest.raises(ValidationError) as error:
        Evidence.model_validate(payload)
    assert _error_types(error) == ["missing"]


@pytest.mark.parametrize(
    ("entry", "error_type"),
    [
        ({"source": "google", "dataset_id": 1, "mode": "mock", "role": "primary"}, "literal_error"),
        ({"source": "google", "dataset_id": 1, "mode": "live", "role": "parent"}, "literal_error"),
        (
            {"source": "google", "dataset_id": "one", "mode": "live", "role": "primary"},
            "int_parsing",
        ),
    ],
)
def test_evidence_provenance_holds_only_contract_values(
    entry: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Evidence.model_validate({**_evidence(), "provenance": [entry]})
    assert _error_types(error) == [error_type]


ITEM_KEYS = {
    "id",
    "rank",
    "rule_id",
    "action",
    "reason",
    "entity",
    "evidence",
    "flags",
    "data_status",
    "held",
    "held_reason",
    "decision_subject",
    "current",
}
"""AAP 0.5.3 ``Item``."""

ITEM_OVERLAY_KEYS = {
    "held_now",
    "held_reason_now",
    "historical",
    "data_status_now",
    "flags_now",
    "hidden_by",
}
"""AAP 0.5.3 ``ItemOverlay``."""


def test_item_reproduces_the_aap_item_shape() -> None:
    item = Item.model_validate(
        _item(
            held=True,
            held_reason="Open discrepancy on Brand - US",
            decision_subject={"type": "campaign", "id": 12},
            current={
                "held_now": True,
                "held_reason_now": "Open discrepancy on Brand - US",
                "historical": True,
                "data_status_now": "live",
                "flags_now": [{"code": "unverified_discrepancies", "message": "1 unverified"}],
                "hidden_by": "snooze",
            },
        )
    )
    dumped = item.model_dump(mode="json")
    assert set(dumped) == ITEM_KEYS
    assert set(dumped["current"]) == ITEM_OVERLAY_KEYS
    assert set(dumped["entity"]) == {"source", "type", "id", "name"}
    assert dumped["entity"]["id"] == "77"
    assert dumped["decision_subject"] == {"type": "campaign", "id": 12}
    assert dumped["flags"] == [{"code": "simulated", "message": "Simulated data"}]
    assert dumped["current"]["hidden_by"] == "snooze"
    assert dumped["current"]["data_status_now"] == "live"
    assert dumped["evidence"]["provenance"][0]["mode"] == "simulated"


def test_item_optional_parts_default_to_null() -> None:
    item = Item.model_validate(_item())
    assert isinstance(item.current, ItemOverlay)
    assert (item.held_reason, item.decision_subject) == (None, None)
    assert (item.current.held_reason_now, item.current.hidden_by) == (None, None)


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"data_status": "partial"}, "literal_error"),
        ({"flags": [{"code": "winner", "message": "Winner"}]}, "literal_error"),
        ({"current": {**_overlay_now(), "hidden_by": "archive"}}, "literal_error"),
        ({"current": {**_overlay_now(), "data_status_now": "unknown"}}, "literal_error"),
        ({"decision_subject": {"type": "ad_group", "id": 3}}, "literal_error"),
        ({"current": {"held_now": False}}, "missing"),
    ],
)
def test_item_refuses_values_outside_the_contract(
    overrides: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Item.model_validate(_item(**overrides))
    assert set(_error_types(error)) == {error_type}


def test_flag_holds_a_code_and_a_message() -> None:
    flag = context.Flag("shared_budget", "Shared budget: Brand pool")
    assert Flag.model_validate(flag.to_json()).model_dump() == flag.to_json()


# ---------------------------------------------------------------------------
# Sections, overlay, banners and the page
# ---------------------------------------------------------------------------


def test_recommendations_page_returns_the_five_sections_in_the_fixed_order(
    thresholds: Thresholds,
) -> None:
    payload = _page(_run(thresholds))
    payload["sections"][0]["groups"] = [
        {"key": "block", "title": "Block", "items": [_item()]},
    ]
    payload["sections"][1]["suppressed"] = {"feedback_dismiss": 2, "g1_blocked": 1}
    page = RecommendationsPage.model_validate(payload)
    dumped = page.model_dump(mode="json")
    assert set(dumped) == {"run", "overlay", "banners", "sections"}
    assert [section["key"] for section in dumped["sections"]] == list(SECTION_KEYS)
    assert set(dumped["sections"][0]) == {
        "key",
        "title",
        "groups",
        "findings",
        "withheld",
        "suppressed",
    }
    assert set(dumped["sections"][0]["groups"][0]) == {"key", "title", "items"}
    assert dumped["sections"][1]["suppressed"] == {"feedback_dismiss": 2, "g1_blocked": 1}
    assert set(dumped["overlay"]) == {
        "open_discrepancies",
        "source_modes",
        "stale_datasets",
        "incomplete_datasets",
        "computed_at",
    }
    assert set(dumped["overlay"]["source_modes"]) == {"google", "linkedin", "hubspot"}
    assert set(dumped["banners"][0]) == {"code", "tone", "message", "count"}
    assert dumped["run"] == _run(thresholds).model_dump(mode="json")


@pytest.mark.parametrize(
    "keys",
    [
        SECTION_KEYS[:4],
        tuple(reversed(SECTION_KEYS)),
        (*SECTION_KEYS, "keywords"),
        ("campaigns", "keywords", "budget", "creative", "white_papers"),
        (),
    ],
)
def test_recommendations_page_refuses_any_other_section_list(
    thresholds: Thresholds, keys: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError, match=r"sections must be exactly \['keywords'"):
        RecommendationsPage.model_validate(_page(_run(thresholds), keys))


def test_section_refuses_an_unknown_key_and_a_non_integer_suppression_count() -> None:
    with pytest.raises(ValidationError) as error:
        Section.model_validate({**_section("keywords"), "key": "launch"})
    assert _error_types(error) == ["literal_error"]
    with pytest.raises(ValidationError) as error:
        Section.model_validate(_section("budget", suppressed={"g1_blocked": "two"}))
    assert _error_types(error) == ["int_parsing"]


def test_group_holds_its_items_in_rank_order() -> None:
    group = Group.model_validate(
        {"key": "pause", "title": "Pause", "items": [_item(rank=1), _item(id=502, rank=2)]}
    )
    assert [item.rank for item in group.items] == [1, 2]


def test_overlay_defaults_absent_source_modes_to_null_and_needs_an_aware_instant() -> None:
    payload = {
        "open_discrepancies": 0,
        "source_modes": {"google": "live"},
        "stale_datasets": ["linkedin.leads"],
        "incomplete_datasets": [],
        "computed_at": NOW,
    }
    overlay = Overlay.model_validate(payload)
    assert overlay.source_modes.model_dump() == {
        "google": "live",
        "linkedin": None,
        "hubspot": None,
    }
    with pytest.raises(ValidationError) as error:
        Overlay.model_validate({**payload, "computed_at": datetime(2026, 10, 6)})  # noqa: DTZ001
    assert _error_types(error) == ["timezone_aware"]


def test_banner_count_is_optional_and_its_tone_comes_from_the_design_system() -> None:
    banner = Banner(code="short_window", tone="warning", message="13-day window")
    assert banner.model_dump() == {
        "code": "short_window",
        "tone": "warning",
        "message": "13-day window",
        "count": None,
    }
    with pytest.raises(ValidationError) as error:
        Banner(code="short_window", tone="alert", message="13-day window")
    assert _error_types(error) == ["literal_error"]


# ---------------------------------------------------------------------------
# Findings and withheld patterns
# ---------------------------------------------------------------------------


def test_finding_from_row_returns_the_stored_finding() -> None:
    ref = context.EntityRef("google", "campaign", 42, "2233445566", "Brand - US")
    row = RunFinding(
        run_id=41,
        section="keywords",
        code="k1_no_baseline",
        entity=ref.to_json(),
        message="K1 withheld: 9 baseline conversions of 10 required",
        evidence={"baseline_conversions": 9, "required": 10},
    )
    finding = Finding.from_row(row)
    assert finding.model_dump(mode="json") == {
        "code": "k1_no_baseline",
        "section": "keywords",
        "entity": {"source": "google", "type": "campaign", "id": "42", "name": "Brand - US"},
        "message": "K1 withheld: 9 baseline conversions of 10 required",
        "evidence": {"baseline_conversions": 9, "required": 10},
    }


def test_finding_from_row_without_entity_or_evidence() -> None:
    row = RunFinding(
        run_id=41,
        section="campaigns",
        code="conversion_tracking_absent",
        entity=None,
        message="Google spent with no recorded conversions",
        evidence=None,
    )
    finding = Finding.from_row(row)
    assert (finding.entity, finding.evidence) == (None, {})


def test_finding_accepts_the_analysis_finding_with_its_entity_omitted() -> None:
    stored = context.Finding(
        code="search_term_coverage",
        section="keywords",
        entity=None,
        message="Search terms cover 72% of keyword spend",
        evidence={"coverage": "0.72"},
    ).to_json()
    assert "entity" not in stored
    assert Finding.model_validate(stored).entity is None


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [({"code": "winner_found"}, "literal_error"), ({"section": "launch"}, "literal_error")],
)
def test_finding_from_row_rejects_a_stored_value_outside_the_contract(
    overrides: dict[str, Any], error_type: str
) -> None:
    values = {
        "run_id": 41,
        "section": "keywords",
        "code": "k1_no_baseline",
        "entity": None,
        "message": "m",
        "evidence": {},
    }
    with pytest.raises(ValidationError) as error:
        Finding.from_row(RunFinding(**{**values, **overrides}))
    assert _error_types(error) == [error_type]


def test_withheld_from_row_renders_numeric_samples_as_fixed_point_strings() -> None:
    ref = context.EntityRef("google", "account", 3, "1234567890", "Main account")
    row = RunWithheld(
        run_id=41,
        section="budget",
        rule_id="B4",
        entity=ref.to_json(),
        reason="3 weekend days of 4 required",
        observed=Decimal("3"),
        required=Decimal("4.000000"),
        unit="weekend days",
    )
    withheld = Withheld.from_row(row)
    assert withheld.model_dump(mode="json") == {
        "rule_id": "B4",
        "entity": {"source": "google", "type": "account", "id": "3", "name": "Main account"},
        "reason": "3 weekend days of 4 required",
        "sample": {"observed": "3", "required": "4.000000", "unit": "weekend days"},
    }


def test_withheld_from_row_without_entity_or_sample() -> None:
    row = RunWithheld(
        run_id=41,
        section="keywords",
        rule_id="K1",
        entity=None,
        reason="No usable baseline",
        observed=None,
        required=None,
        unit=None,
    )
    withheld = Withheld.from_row(row)
    assert withheld.entity is None
    assert withheld.sample.model_dump() == {"observed": None, "required": None, "unit": None}


def test_withheld_accepts_the_analysis_withheld_shape() -> None:
    stored = context.Withheld(
        rule_id="B2",
        section="budget",
        entity=None,
        reason="spending days 4 of 5",
        observed=4,
        required=5,
        unit="days",
    ).to_json()
    withheld = Withheld.model_validate(stored)
    assert withheld.model_dump(mode="json") == stored


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["dismiss", "snooze", "done"])
def test_feedback_request_accepts_each_kind_with_an_optional_note(kind: str) -> None:
    assert FeedbackRequest.model_validate({"kind": kind}).model_dump() == {
        "kind": kind,
        "note": None,
    }
    assert FeedbackRequest(kind=kind, note="n" * NOTE_MAX_LENGTH).note == "n" * NOTE_MAX_LENGTH


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"kind": "archive"}, "literal_error"),
        ({"kind": "dismiss", "note": "n" * (NOTE_MAX_LENGTH + 1)}, "string_too_long"),
        ({}, "missing"),
    ],
)
def test_feedback_request_refuses_an_unknown_kind_and_an_overlong_note(
    payload: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        FeedbackRequest.model_validate(payload)
    assert _error_types(error) == [error_type]


def _feedback_row(**overrides: Any) -> RecommendationFeedback:
    ref = context.EntityRef("google", "search_term", None, "111|222|free trial", "free trial")
    values: dict[str, Any] = {
        "id": 61,
        "recommendation_id": 501,
        "rule_id": "K1",
        "entity_type": "search_term",
        "entity_id": ref.key,
        "kind": "snooze",
        "until": NOW + timedelta(days=7),
        "note": "Revisit after the promo",
        "created_at": NOW,
        "created_by": "admin@example.com",
    }
    values.update(overrides)
    return RecommendationFeedback(**values)


def test_feedback_from_row_keeps_the_text_entity_key() -> None:
    feedback = Feedback.from_row(_feedback_row())
    dumped = feedback.model_dump(mode="json")
    assert dumped["entity_id"] == "google:search_term:111|222|free trial"
    assert dumped == {
        "id": 61,
        "recommendation_id": 501,
        "rule_id": "K1",
        "entity_type": "search_term",
        "entity_id": "google:search_term:111|222|free trial",
        "kind": "snooze",
        "until": "2026-10-13T12:00:00Z",
        "note": "Revisit after the promo",
        "created_at": "2026-10-06T12:00:00Z",
        "created_by": "admin@example.com",
    }


def test_feedback_from_row_without_entity_note_or_author() -> None:
    feedback = Feedback.from_row(
        _feedback_row(entity_type=None, entity_id=None, note=None, created_by=None)
    )
    assert (feedback.entity_type, feedback.entity_id, feedback.note, feedback.created_by) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"kind": "hide"}, "literal_error"),
        ({"until": datetime(2026, 10, 13)}, "timezone_aware"),  # noqa: DTZ001
    ],
)
def test_feedback_from_row_rejects_a_stored_value_outside_the_contract(
    overrides: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Feedback.from_row(_feedback_row(**overrides))
    assert _error_types(error) == [error_type]


# ---------------------------------------------------------------------------
# Decision requests (AAP 0.5.2 patch model)
# ---------------------------------------------------------------------------


def test_decision_request_parses_a_patch_on_a_subject() -> None:
    request = DecisionRequest.model_validate_json(
        '{"subject": {"type": "campaign", "id": 12},'
        ' "patch": {"daily_budget": "120.50", "status": "paused"},'
        ' "recommendation_id": 501, "note": "From the B1 card"}'
    )
    assert request.subject.model_dump() == {"type": "campaign", "id": 12}
    assert request.patch.present_fields() == {
        "status": "paused",
        "daily_budget": Decimal("120.50"),
    }
    assert (request.recommendation_id, request.note) == (501, "From the B1 card")


def test_decision_request_optional_fields_default_to_null() -> None:
    request = DecisionRequest.model_validate(
        {"subject": {"type": "budget", "id": 4}, "patch": {"daily_budget": "300"}}
    )
    assert (request.recommendation_id, request.note) == (None, None)
    assert request.subject.type == "budget"


@pytest.mark.parametrize(
    ("payload", "location", "error_type"),
    [
        (
            {"subject": {"type": "ad_group", "id": 1}, "patch": {}},
            ("subject", "type"),
            "literal_error",
        ),
        (
            {"subject": {"type": "campaign", "id": 0}, "patch": {}},
            ("subject", "id"),
            "greater_than",
        ),
        (
            {"subject": {"type": "campaign", "id": "twelve"}, "patch": {}},
            ("subject", "id"),
            "int_parsing",
        ),
        (
            {"subject": {"type": "campaign", "id": 1}, "patch": {}, "recommendation_id": 0},
            ("recommendation_id",),
            "greater_than",
        ),
        (
            {"subject": {"type": "campaign", "id": 1}, "patch": {}, "note": "n" * 2001},
            ("note",),
            "string_too_long",
        ),
        ({"subject": {"type": "campaign", "id": 1}}, ("patch",), "missing"),
        ({"patch": {}}, ("subject",), "missing"),
    ],
)
def test_decision_request_refuses_bad_subjects_references_and_notes(
    payload: dict[str, Any], location: tuple[str, ...], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        DecisionRequest.model_validate(payload)
    errors = error.value.errors()
    assert [(item["loc"], item["type"]) for item in errors] == [(location, error_type)]


def test_bigint_max_is_the_largest_postgresql_bigint() -> None:
    assert schemas.BIGINT_MAX == 9_223_372_036_854_775_807 == 2**63 - 1
    assert {"BIGINT_MAX", "PositiveBigInt"} <= set(schemas.__all__)


def _decision_payload(
    *, subject_id: int = 1, recommendation_id: int | None = None
) -> dict[str, Any]:
    """A minimal ``POST /decisions`` body naming ``subject_id`` and, if given, the item."""
    payload: dict[str, Any] = {"subject": {"type": "campaign", "id": subject_id}, "patch": {}}
    if recommendation_id is not None:
        payload["recommendation_id"] = recommendation_id
    return payload


def test_decision_request_accepts_ids_up_to_the_largest_bigint() -> None:
    request = DecisionRequest.model_validate(
        _decision_payload(subject_id=schemas.BIGINT_MAX, recommendation_id=schemas.BIGINT_MAX)
    )
    assert (request.subject.id, request.recommendation_id) == (
        schemas.BIGINT_MAX,
        schemas.BIGINT_MAX,
    )


@pytest.mark.parametrize(
    ("payload", "location"),
    [
        (_decision_payload(subject_id=2**63), ("subject", "id")),
        (_decision_payload(subject_id=10**30), ("subject", "id")),
        (_decision_payload(recommendation_id=2**63), ("recommendation_id",)),
    ],
    ids=["subject-2**63", "subject-10**30", "recommendation-2**63"],
)
def test_decision_request_refuses_an_id_beyond_the_largest_bigint(
    payload: dict[str, Any], location: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError) as error:
        DecisionRequest.model_validate(payload)
    errors = error.value.errors()
    assert [(item["loc"], item["type"]) for item in errors] == [(location, "less_than_equal")]
    assert errors[0]["ctx"] == {"le": schemas.BIGINT_MAX}


def test_decision_request_refuses_an_oversize_id_sent_on_the_json_wire() -> None:
    with pytest.raises(ValidationError) as error:
        DecisionRequest.model_validate_json(
            '{"subject": {"type": "campaign", "id": 9223372036854775808}, "patch": {},'
            ' "recommendation_id": 9223372036854775808}'
        )
    assert sorted(item["loc"] for item in error.value.errors()) == [
        ("recommendation_id",),
        ("subject", "id"),
    ]
    assert _error_types(error) == ["less_than_equal", "less_than_equal"]


def test_request_ids_document_the_bigint_bounds_in_the_json_schema() -> None:
    bounds = {"type": "integer", "exclusiveMinimum": 0, "maximum": schemas.BIGINT_MAX}
    subject = schemas.SubjectIn.model_json_schema()["properties"]["id"]
    assert bounds.items() <= subject.items()
    reference = DecisionRequest.model_json_schema()["properties"]["recommendation_id"]
    assert reference["default"] is None
    assert reference["anyOf"] == [bounds, {"type": "null"}]


def test_patch_present_fields_returns_only_the_sent_fields_in_declaration_order() -> None:
    patch = DecisionPatchIn.model_validate({"total_budget": "5000", "status": "active"})
    present = patch.present_fields()
    assert list(present) == ["status", "total_budget"]
    assert present == {"status": "active", "total_budget": Decimal(5000)}


def test_an_empty_patch_parses_and_is_left_to_the_decision_rules() -> None:
    assert DecisionPatchIn.model_validate({}).present_fields() == {}


@pytest.mark.parametrize("field", ["status", "daily_budget", "total_budget"])
def test_patch_refuses_a_field_sent_as_null(field: str) -> None:
    with pytest.raises(ValidationError, match=rf"omit the field instead of sending null: {field}"):
        DecisionPatchIn.model_validate({field: None})


def test_patch_names_every_field_sent_as_null() -> None:
    with pytest.raises(
        ValidationError, match=r"omit the field instead of sending null: status, total_budget"
    ):
        DecisionPatchIn.model_validate({"total_budget": None, "status": None, "daily_budget": "1"})


@pytest.mark.parametrize("status", ["archived", "ACTIVE", "removed", ""])
def test_patch_refuses_a_status_outside_the_normalised_statuses(status: str) -> None:
    with pytest.raises(ValidationError) as error:
        DecisionPatchIn.model_validate({"status": status})
    assert _error_types(error) == ["literal_error"]


# ---------------------------------------------------------------------------
# Resolutions (AAP 0.5.4.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("intended", "present"),
    [
        (
            {"status": "active", "daily_budget": "50.00"},
            {"status": "active", "daily_budget": "50.00"},
        ),
        ({"status": "paused"}, {"status": "paused"}),
        ({"daily_budget": "300"}, {"daily_budget": "300"}),
        ({"total_budget": "1200.00"}, {"total_budget": "1200.00"}),
    ],
)
def test_intended_state_carries_the_fields_of_each_shape(
    intended: dict[str, str], present: dict[str, str]
) -> None:
    request = ResolveRequest.model_validate(
        {"resolution": "record_intended", "intended_state": intended, "note": "Planned launch"}
    )
    assert request.intended_state is not None
    assert {
        key: value if isinstance(value, str) else format(value, "f")
        for key, value in request.intended_state.present_fields().items()
    } == present
    assert request.note == "Planned launch"


def test_intended_state_refuses_null_fields_floats_and_unknown_statuses() -> None:
    with pytest.raises(ValidationError, match=r"omit the field instead of sending null: status"):
        IntendedStateIn.model_validate({"status": None, "daily_budget": "10"})
    with pytest.raises(ValidationError, match=r"not JSON numbers"):
        IntendedStateIn.model_validate({"status": "active", "daily_budget": 10.5})
    with pytest.raises(ValidationError) as error:
        IntendedStateIn.model_validate({"status": "archived"})
    assert _error_types(error) == ["literal_error"]


@pytest.mark.parametrize("resolution", DISCREPANCY_RESOLUTIONS)
def test_resolve_request_accepts_every_resolution_key(resolution: str) -> None:
    request = ResolveRequest.model_validate({"resolution": resolution})
    assert request.model_dump() == {"resolution": resolution, "intended_state": None, "note": None}


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"resolution": "ignore"}, "literal_error"),
        ({}, "missing"),
        ({"resolution": "platform_correct", "note": "n" * 2001}, "string_too_long"),
        ({"resolution": "record_intended", "intended_state": "active"}, "model_type"),
    ],
)
def test_resolve_request_refuses_an_unknown_resolution_and_a_malformed_body(
    payload: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        ResolveRequest.model_validate(payload)
    assert _error_types(error) == [error_type]


# ---------------------------------------------------------------------------
# Storable text: no U+0000 and nothing UTF-8 cannot encode reaches SQL
# ---------------------------------------------------------------------------

STORABLE_FIELDS: list[tuple[type[BaseModel], str, Callable[[str], dict[str, Any]]]] = [
    (LoginRequest, "email", lambda text: {"email": text, "password": "pw"}),
    (FeedbackRequest, "note", lambda text: {"kind": "dismiss", "note": text}),
    (
        DecisionRequest,
        "note",
        lambda text: {"subject": {"type": "campaign", "id": 1}, "patch": {}, "note": text},
    ),
    (ResolveRequest, "note", lambda text: {"resolution": "platform_correct", "note": text}),
]
"""Every request field bound for a PostgreSQL ``text`` column or comparison, with a builder
of a body that is valid apart from that field."""

STORABLE_IDS = [f"{model.__name__}.{field}" for model, field, _ in STORABLE_FIELDS]


@pytest.mark.parametrize(("model", "field", "build"), STORABLE_FIELDS, ids=STORABLE_IDS)
@pytest.mark.parametrize("text", ["adm\x00in@example.com", "\x00", "trailing\x00"])
def test_storable_text_refuses_a_nul_character_without_echoing_the_value(
    model: type[BaseModel], field: str, build: Callable[[str], dict[str, Any]], text: str
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(build(text))
    errors = error.value.errors()
    assert [(item["loc"], item["type"]) for item in errors] == [((field,), "value_error")]
    message = errors[0]["msg"]
    assert message == "Value error, must not contain a NUL (U+0000) character"
    assert text not in message
    assert "\x00" not in message


@pytest.mark.parametrize(("model", "field", "build"), STORABLE_FIELDS, ids=STORABLE_IDS)
@pytest.mark.parametrize("text", ["\ud800", "note \udfff end", "\ud83d\ude00"])
def test_storable_text_refuses_an_unpaired_surrogate_without_echoing_the_value(
    model: type[BaseModel], field: str, build: Callable[[str], dict[str, Any]], text: str
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(build(text))
    errors = error.value.errors()
    assert [(item["loc"], item["type"]) for item in errors] == [((field,), "string_unicode")]
    assert text not in errors[0]["msg"]


@pytest.mark.parametrize(("model", "field", "build"), STORABLE_FIELDS, ids=STORABLE_IDS)
@pytest.mark.parametrize(
    "text",
    [
        "paused for review — café",
        "café ✓",
        "josé@exämple.com",
        "日本語のメモ",
        "launch 😀 next week",
        "line one\nline two\ttabbed",
    ],
)
def test_storable_text_passes_ordinary_and_non_ascii_text_unchanged(
    model: type[BaseModel], field: str, build: Callable[[str], dict[str, Any]], text: str
) -> None:
    assert getattr(model.model_validate(build(text)), field) == text


def test_storable_text_checks_the_email_after_trimming_and_lowercasing() -> None:
    with pytest.raises(ValidationError) as error:
        LoginRequest.model_validate({"email": "  ADM\x00IN@Example.com ", "password": "pw"})
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("email",), "value_error")
    ]
    request = LoginRequest.model_validate({"email": "  José@Exämple.COM ", "password": "pw"})
    assert request.email == "josé@exämple.com"


@pytest.mark.parametrize(("model", "field", "build"), STORABLE_FIELDS[1:], ids=STORABLE_IDS[1:])
def test_an_overlong_note_is_too_long_whatever_it_contains(
    model: type[BaseModel], field: str, build: Callable[[str], dict[str, Any]]
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(build("\x00" * (NOTE_MAX_LENGTH + 1)))
    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        ((field,), "string_too_long")
    ]
    assert getattr(model.model_validate(build("n" * NOTE_MAX_LENGTH)), field) == (
        "n" * NOTE_MAX_LENGTH
    )


def test_an_overlong_email_is_too_long_whatever_it_contains() -> None:
    with pytest.raises(ValidationError) as error:
        LoginRequest.model_validate({"email": "a\x00" * 161, "password": "pw"})
    assert _error_types(error) == ["string_too_long"]


@pytest.mark.parametrize(("model", "field", "build"), STORABLE_FIELDS[1:], ids=STORABLE_IDS[1:])
def test_storable_notes_keep_their_optional_bounded_wire_contract(
    model: type[BaseModel], field: str, build: Callable[[str], dict[str, Any]]
) -> None:
    note = model.model_json_schema()["properties"][field]
    assert note["default"] is None
    assert note["anyOf"] == [{"maxLength": NOTE_MAX_LENGTH, "type": "string"}, {"type": "null"}]
    assert field not in model.model_json_schema().get("required", [])


def test_the_login_password_is_memory_only_and_may_hold_any_character() -> None:
    request = LoginRequest.model_validate({"email": "admin@example.com", "password": "pa\x00ss"})
    assert request.password == "pa\x00ss"


def test_storable_text_validator_refuses_unencodable_text_without_chaining_it() -> None:
    assert schemas._storable_text("café ✓") == "café ✓"
    with pytest.raises(ValueError, match=r"^must not contain a NUL \(U\+0000\) character$"):
        schemas._storable_text("a\x00b")
    with pytest.raises(ValueError, match=r"^must be valid Unicode text, without unpaired") as error:
        schemas._storable_text("a\ud800b")
    assert "a\ud800b" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_storable_aliases_are_exported() -> None:
    assert {"StorableNote", "StorableText"} <= set(schemas.__all__)


# ---------------------------------------------------------------------------
# Decision and discrepancy views
# ---------------------------------------------------------------------------

DECISION_KEYS = {
    "id",
    "subject",
    "shape",
    "status",
    "daily_budget",
    "total_budget",
    "budget_id",
    "tombstone",
    "decided_at",
    "decided_by",
    "origin",
    "recommendation_id",
    "patch",
    "note",
}


def test_decision_from_row_carries_the_shape_and_the_complete_expected_state() -> None:
    decision = Decision.from_row(_decision_row(), subject=_subject())
    dumped = decision.model_dump(mode="json")
    assert set(dumped) == DECISION_KEYS
    assert dumped["subject"] == {
        "type": "campaign",
        "id": 12,
        "name": "Brand - US",
        "source": "google",
        "currency": "USD",
    }
    assert (dumped["shape"], dumped["status"]) == ("own_budget", "active")
    assert (dumped["daily_budget"], dumped["total_budget"]) == ("120.500000", None)
    assert (dumped["budget_id"], dumped["tombstone"]) == (None, False)
    assert dumped["decided_at"] == "2026-10-06T12:00:00Z"
    assert (dumped["decided_by"], dumped["origin"]) == ("admin@example.com", "user")
    assert (dumped["recommendation_id"], dumped["patch"]) == (501, {"daily_budget": "120.50"})
    assert dumped["note"] == "Raise after the B1 card"
    assert decision.daily_budget == Decimal("120.500000")


def test_decision_from_row_for_a_shared_member_holds_its_budget_assignment_only() -> None:
    decision = Decision.from_row(
        _decision_row(
            shape="shared_member",
            status="paused",
            daily_budget=None,
            budget_id=9,
            patch={"status": "paused"},
        ),
        subject=_subject(),
    )
    assert (decision.shape, decision.status, decision.budget_id) == ("shared_member", "paused", 9)
    assert (decision.daily_budget, decision.total_budget) == (None, None)


def test_decision_from_row_for_a_budget_subject_has_no_status() -> None:
    decision = Decision.from_row(
        _decision_row(
            subject_type="budget",
            subject_id=4,
            shape="budget",
            status=None,
            daily_budget=None,
            total_budget=Decimal("1200.000000"),
            origin="baseline",
            decided_by=None,
            recommendation_id=None,
            patch=None,
            note=None,
        ),
        subject=_subject(type="budget", id=4, name="Brand pool"),
    )
    dumped = decision.model_dump(mode="json")
    assert (dumped["status"], dumped["total_budget"], dumped["patch"]) == (None, "1200.000000", {})
    assert (dumped["subject"]["type"], dumped["origin"]) == ("budget", "baseline")


def test_decision_tombstone_records_an_acknowledged_removal() -> None:
    decision = Decision.from_row(
        _decision_row(status="ended", tombstone=True, origin="resolution", patch={}),
        subject=_subject(),
    )
    assert (decision.status, decision.tombstone, decision.origin) == ("ended", True, "resolution")


@pytest.mark.parametrize(
    ("daily_budget", "total_budget", "expected"),
    [
        (Decimal("1E+2"), None, ("100", None)),
        (None, Decimal("1.5E+3"), (None, "1500")),
        (Decimal("120.50"), None, ("120.50", None)),
        (Decimal("0.000000"), None, ("0.000000", None)),
    ],
)
def test_decision_amounts_serialise_as_fixed_point_strings_never_exponent_notation(
    daily_budget: Decimal | None, total_budget: Decimal | None, expected: tuple[str | None, ...]
) -> None:
    decision = Decision.from_row(
        _decision_row(daily_budget=daily_budget, total_budget=total_budget),
        subject=_subject(),
    )
    dumped = decision.model_dump(mode="json")
    assert (dumped["daily_budget"], dumped["total_budget"]) == expected
    assert "E+" not in decision.model_dump_json()
    python = decision.model_dump()
    assert (python["daily_budget"], python["total_budget"]) == (daily_budget, total_budget)


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"shape": "group"}, "literal_error"),
        ({"origin": "import"}, "literal_error"),
        ({"status": "removed"}, "literal_error"),
        ({"decided_at": datetime(2026, 10, 6)}, "timezone_aware"),  # noqa: DTZ001
        ({"daily_budget": Decimal("0.0000001")}, "decimal_max_places"),
    ],
)
def test_decision_from_row_rejects_a_stored_value_outside_the_contract(
    overrides: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Decision.from_row(_decision_row(**overrides), subject=_subject())
    assert _error_types(error) == [error_type]


DISCREPANCY_KEYS = {
    "id",
    "subject",
    "shape",
    "kind",
    "decided_value",
    "platform_value",
    "detected_at",
    "state",
    "resolved_at",
    "resolution",
    "resolutions",
}

AAP_RESOLUTIONS_BY_KIND = {
    "status": {"platform_correct", "decision_correct"},
    "budget": {"platform_correct", "decision_correct"},
    "untracked": {"adopt_platform", "record_intended"},
    "missing": {"acknowledge_removal", "decision_correct"},
}
"""AAP 0.5.4.4 "C0 resolutions by kind and subject type"."""


@pytest.mark.parametrize(("pair", "valid"), sorted(RESOLUTIONS.items()))
def test_discrepancy_from_row_offers_the_valid_resolutions_in_their_fixed_order(
    pair: tuple[str, str], valid: frozenset[str]
) -> None:
    kind, shape = pair
    assert set(valid) == AAP_RESOLUTIONS_BY_KIND[kind]
    discrepancy = Discrepancy.from_row(
        _discrepancy_row(kind=kind),
        subject=_subject(),
        shape=shape,
        resolutions=sorted(valid, reverse=True),
    )
    assert discrepancy.resolutions == [key for key in DISCREPANCY_RESOLUTIONS if key in valid]
    assert (discrepancy.kind, discrepancy.shape) == (kind, shape)


def test_discrepancy_from_row_reproduces_the_resolver_shape() -> None:
    discrepancy = Discrepancy.from_row(
        _discrepancy_row(),
        subject=_subject(),
        shape="own_budget",
        resolutions=["decision_correct", "platform_correct"],
    )
    dumped = discrepancy.model_dump(mode="json")
    assert set(dumped) == DISCREPANCY_KEYS
    assert dumped["decided_value"] == {"status": "active"}
    assert dumped["platform_value"] == {"status": "paused"}
    assert dumped["detected_at"] == "2026-10-06T12:00:00Z"
    assert (dumped["state"], dumped["resolved_at"], dumped["resolution"]) == ("open", None, None)
    assert dumped["resolutions"] == ["platform_correct", "decision_correct"]


def test_discrepancy_from_row_accepts_any_iterable_and_collapses_repeats() -> None:
    offered = (key for key in ["record_intended", "adopt_platform", "record_intended"])
    discrepancy = Discrepancy.from_row(
        _discrepancy_row(kind="untracked", decided_value=None),
        subject=_subject(),
        shape="shared_member",
        resolutions=offered,
    )
    assert discrepancy.resolutions == ["adopt_platform", "record_intended"]
    assert discrepancy.decided_value is None


def test_discrepancy_from_row_refuses_an_unknown_resolution() -> None:
    with pytest.raises(ValueError, match=r"unknown discrepancy resolutions \['ignore', 'snooze'\]"):
        Discrepancy.from_row(
            _discrepancy_row(),
            subject=_subject(),
            shape="own_budget",
            resolutions=["platform_correct", "snooze", "ignore"],
        )


def test_a_resolved_missing_discrepancy_has_no_platform_side() -> None:
    discrepancy = Discrepancy.from_row(
        _discrepancy_row(
            kind="missing",
            platform_value=None,
            snapshot_id=None,
            state="resolved",
            resolved_at=NOW + timedelta(hours=1),
            resolution="acknowledge_removal",
        ),
        subject=_subject(type="budget", id=4, name="Brand pool"),
        shape="budget",
        resolutions=RESOLUTIONS[("missing", "budget")],
    )
    assert discrepancy.platform_value is None
    assert (discrepancy.state, discrepancy.resolution) == ("resolved", "acknowledge_removal")
    assert discrepancy.resolutions == ["decision_correct", "acknowledge_removal"]


@pytest.mark.parametrize(
    ("overrides", "shape", "error_type"),
    [
        ({"kind": "drift"}, "own_budget", "literal_error"),
        ({"state": "closed"}, "own_budget", "literal_error"),
        ({}, "group", "literal_error"),
        ({"detected_at": datetime(2026, 10, 6)}, "own_budget", "timezone_aware"),  # noqa: DTZ001
    ],
)
def test_discrepancy_from_row_rejects_a_stored_value_outside_the_contract(
    overrides: dict[str, Any], shape: str, error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Discrepancy.from_row(
            _discrepancy_row(**overrides), subject=_subject(), shape=shape, resolutions=[]
        )
    assert _error_types(error) == [error_type]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _dataset_row(**overrides: Any) -> SourceDataset:
    values: dict[str, Any] = {
        "id": 3,
        "source": "hubspot",
        "mode": "simulated",
        "state": "active",
        "role": "primary",
        "parent_dataset_id": None,
        "reason": "credentials not configured",
        "created_at": NOW - timedelta(days=2),
        "activated_at": NOW - timedelta(days=1),
        "retired_at": None,
    }
    values.update(overrides)
    return SourceDataset(**values)


def _freshness_row(**overrides: Any) -> DatasetFreshness:
    values: dict[str, Any] = {
        "source": "google",
        "dataset_id": 1,
        "dataset": "google.metrics_daily",
        "last_attempt_at": NOW,
        "last_success_at": NOW - timedelta(hours=1),
        "covered_from": date(2026, 4, 9),
        "covered_through": DEFAULT_END,
        "completeness": "complete",
        "reason": None,
    }
    values.update(overrides)
    return DatasetFreshness(**values)


def _sync_run_row(**overrides: Any) -> SyncRun:
    values: dict[str, Any] = {
        "id": 70,
        "source": "google",
        "job": "metrics_daily",
        "dataset": "google.metrics_daily",
        "started_at": NOW - timedelta(minutes=3),
        "finished_at": NOW,
        "status": "succeeded",
        "rows": 1240,
        "completeness": "complete",
        "error": None,
    }
    values.update(overrides)
    return SyncRun(**values)


def test_dataset_view_from_row() -> None:
    assert DatasetView.from_row(_dataset_row()).model_dump(mode="json") == {
        "id": 3,
        "mode": "simulated",
        "role": "primary",
        "state": "active",
        "reason": "credentials not configured",
        "created_at": "2026-10-04T12:00:00Z",
        "activated_at": "2026-10-05T12:00:00Z",
    }
    building = DatasetView.from_row(
        _dataset_row(state="building", role="companion", activated_at=None, reason=None)
    )
    assert (building.state, building.role, building.activated_at, building.reason) == (
        "building",
        "companion",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [({"state": "purged"}, "literal_error"), ({"mode": "mock"}, "literal_error")],
)
def test_dataset_view_rejects_a_stored_value_outside_the_contract(
    overrides: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        DatasetView.from_row(_dataset_row(**overrides))
    assert _error_types(error) == [error_type]


@pytest.mark.parametrize(
    ("completeness", "incomplete"),
    [("complete", False), ("incomplete", True), ("unknown", True)],
)
def test_dataset_freshness_view_derives_incomplete_from_completeness(
    completeness: str, incomplete: bool
) -> None:
    view = DatasetFreshnessView.from_row(_freshness_row(completeness=completeness), stale=False)
    assert view.incomplete is incomplete
    assert view.model_dump(mode="json")["incomplete"] is incomplete


def test_dataset_freshness_view_from_row() -> None:
    view = DatasetFreshnessView.from_row(
        _freshness_row(
            completeness="incomplete",
            reason="call budget exhausted",
            last_success_at=None,
            covered_from=None,
            covered_through=None,
        ),
        stale=True,
    )
    assert view.model_dump(mode="json") == {
        "dataset": "google.metrics_daily",
        "last_attempt_at": "2026-10-06T12:00:00Z",
        "last_success_at": None,
        "covered_from": None,
        "covered_through": None,
        "completeness": "incomplete",
        "reason": "call budget exhausted",
        "stale": True,
        "incomplete": True,
    }


def test_sync_run_view_from_row() -> None:
    assert SyncRunView.from_row(_sync_run_row()).model_dump(mode="json") == {
        "id": 70,
        "job": "metrics_daily",
        "dataset": "google.metrics_daily",
        "started_at": "2026-10-06T11:57:00Z",
        "finished_at": "2026-10-06T12:00:00Z",
        "status": "succeeded",
        "rows": 1240,
        "completeness": "complete",
        "error": None,
    }
    running = SyncRunView.from_row(
        _sync_run_row(status="running", finished_at=None, rows=None, completeness=None)
    )
    assert (running.status, running.finished_at, running.rows, running.completeness) == (
        "running",
        None,
        None,
        None,
    )


def test_sync_run_view_rejects_a_stored_value_outside_the_contract() -> None:
    with pytest.raises(ValidationError) as error:
        SyncRunView.from_row(_sync_run_row(status="queued"))
    assert _error_types(error) == ["literal_error"]


def test_sync_request_view_from_row() -> None:
    pending = SyncRequestView.from_row(
        SyncRequest(id=5, source="linkedin", requested_at=NOW, picked_at=None)
    )
    assert pending.model_dump(mode="json") == {
        "id": 5,
        "source": "linkedin",
        "requested_at": "2026-10-06T12:00:00Z",
        "picked_at": None,
    }
    picked = SyncRequestView.from_row(
        SyncRequest(id=6, source="google", requested_at=NOW, picked_at=NOW + timedelta(seconds=30))
    )
    assert picked.picked_at == NOW + timedelta(seconds=30)
    with pytest.raises(ValidationError) as error:
        SyncRequestView.from_row(SyncRequest(id=7, source="bing", requested_at=NOW))
    assert _error_types(error) == ["literal_error"]


SOURCE_KEYS = {
    "source",
    "mode",
    "status",
    "reason",
    "active_dataset",
    "companion_dataset",
    "datasets",
    "companion_datasets",
    "last_attempt_at",
    "calls_24h",
    "call_cap",
    "sync_pending",
    "recent_runs",
}


def test_source_carries_health_mode_active_dataset_and_freshness() -> None:
    source = Source(
        source="google",
        mode="simulated",
        status="ok",
        reason="credentials not configured",
        active_dataset=DatasetView.from_row(_dataset_row(id=1, source="google")),
        companion_dataset=None,
        datasets=[DatasetFreshnessView.from_row(_freshness_row(), stale=False)],
        companion_datasets=[],
        last_attempt_at=NOW,
        calls_24h=12,
        call_cap=25,
        sync_pending=True,
        recent_runs=[SyncRunView.from_row(_sync_run_row())],
    )
    dumped = source.model_dump(mode="json")
    assert set(dumped) == SOURCE_KEYS
    assert dumped["active_dataset"]["id"] == 1
    assert dumped["datasets"][0]["dataset"] == "google.metrics_daily"
    assert (dumped["calls_24h"], dumped["call_cap"], dumped["sync_pending"]) == (12, 25, True)
    assert Source.model_validate(dumped) == source


def test_source_before_the_first_attempt_has_no_mode_status_or_datasets() -> None:
    source = Source.model_validate(
        {
            "source": "hubspot",
            "mode": None,
            "status": None,
            "reason": None,
            "active_dataset": None,
            "companion_dataset": DatasetView.from_row(_dataset_row(id=8, role="companion")),
            "datasets": [],
            "companion_datasets": [],
            "last_attempt_at": None,
            "calls_24h": None,
            "call_cap": None,
            "sync_pending": False,
            "recent_runs": [],
        }
    )
    assert (source.mode, source.status, source.calls_24h) == (None, None, None)
    assert source.companion_dataset is not None
    assert source.companion_dataset.role == "companion"
    with pytest.raises(ValidationError) as error:
        Source.model_validate({**source.model_dump(), "status": "throttled"})
    assert _error_types(error) == ["literal_error"]


# ---------------------------------------------------------------------------
# Config and health
# ---------------------------------------------------------------------------


def test_window_config_reads_the_loaded_configuration(thresholds: Thresholds) -> None:
    window = WindowConfig.from_thresholds(thresholds)
    assert window.model_dump() == {
        "default_days": thresholds.default_days,
        "trusted_min_days": thresholds.trusted_min_days,
        "presets": list(thresholds.presets),
        "max_days": thresholds.max_days,
        "sustained_min_days": thresholds.sustained_min_days,
    }
    assert window.default_days in window.presets


def test_window_config_follows_a_changed_configuration() -> None:
    window = WindowConfig.from_thresholds(_thresholds_with(default_days=14, trusted_min_days=10))
    assert (window.default_days, window.trusted_min_days) == (14, 10)


def test_content_asset_view_uses_the_registry_key_names() -> None:
    asset = ContentAsset(
        slug="b2b-benchmarks",
        title="B2B Benchmarks 2026",
        lead_form_urns=("urn:li:leadGenForm:990000201",),
        creative_urns=("urn:li:sponsoredCreative:990001301",),
    )
    view = ContentAssetView.from_asset(asset)
    assert view.model_dump() == asset.to_json()
    assert view.model_dump() == {
        "slug": "b2b-benchmarks",
        "title": "B2B Benchmarks 2026",
        "lead_forms": ["urn:li:leadGenForm:990000201"],
        "creative_overrides": ["urn:li:sponsoredCreative:990001301"],
    }
    assert (
        ContentAssetView.from_asset(
            ContentAsset(slug="playbook", title="Playbook", lead_form_urns=())
        ).creative_overrides
        == []
    )


def test_config_view_carries_thresholds_window_mapping_registry_and_hash(
    thresholds: Thresholds,
) -> None:
    view = ConfigView(
        thresholds=context.json_value(thresholds.raw),
        window=WindowConfig.from_thresholds(thresholds),
        hubspot_mapping=HubSpotMappingView.from_mapping(thresholds.mapping),
        crm_bindings=None,
        companion_crm_bindings=None,
        content_assets=[ContentAssetView.from_asset(a) for a in thresholds.content_assets],
        config_hash=thresholds.config_hash,
    )
    dumped = view.model_dump(mode="json")
    assert set(dumped) == {
        "thresholds",
        "window",
        "hubspot_mapping",
        "crm_bindings",
        "companion_crm_bindings",
        "content_assets",
        "config_hash",
    }
    assert dumped["thresholds"]["window"]["default_days"] == thresholds.default_days
    assert dumped["thresholds"]["window"]["trusted_min_days"] == thresholds.trusted_min_days
    assert dumped["thresholds"]["window"]["presets"] == list(thresholds.presets)
    assert [asset["slug"] for asset in dumped["content_assets"]] == [
        asset.slug for asset in thresholds.content_assets
    ]
    assert dumped["config_hash"] == thresholds.config_hash
    assert (dumped["crm_bindings"], dumped["companion_crm_bindings"]) == (None, None)


def test_health_is_status_ok_only() -> None:
    assert Health(status="ok").model_dump() == {"status": "ok"}
    assert Health.model_validate_json('{"status": "ok"}') == Health(status="ok")
    with pytest.raises(ValidationError) as error:
        Health.model_validate({"status": "degraded"})
    assert _error_types(error) == ["literal_error"]


# ---------------------------------------------------------------------------
# Response-key presence: a None default is emitted as null and published as required
# ---------------------------------------------------------------------------

_DEFAULTED_RESPONSE_KEYS: tuple[
    tuple[type[schemas.ApiModel], Callable[[], dict[str, Any]], tuple[str, ...]], ...
] = (
    (schemas.DataModes, dict, ("google", "linkedin", "hubspot")),
    (schemas.SourceModes, dict, ("google", "linkedin", "hubspot")),
    (
        Metric,
        lambda: {"key": "spend", "label": "Spend", "value": "412.500000", "unit": "currency"},
        ("currency", "floor", "unavailable"),
    ),
    (Sample, lambda: {"observed": 240, "required": 300, "unit": "clicks"}, ("projected",)),
    (
        StatTest,
        lambda: {"name": "Fisher's exact test", "p_value": "0.012", "alpha": "0.05"},
        ("p_adjusted",),
    ),
    (Evidence, _evidence, ("sample", "test", "extra")),
    (ItemOverlay, _overlay_now, ("held_reason_now", "hidden_by")),
    (Item, _item, ("held_reason", "decision_subject")),
    (
        Finding,
        lambda: {
            "code": "search_term_coverage",
            "section": "keywords",
            "message": "Search terms cover 72% of spend",
            "evidence": {},
        },
        ("entity",),
    ),
    (
        Banner,
        lambda: {"code": "short_window", "tone": "warning", "message": "Short window"},
        ("count",),
    ),
    (
        SyncRequestView,
        lambda: {"id": 7, "source": "google", "requested_at": NOW},
        ("picked_at",),
    ),
)
"""Every response model with a defaulted field: a minimal payload and its ``None``-default
keys, which ``web/src/api/types.ts`` declares as required ``X | null`` properties."""

_DEFAULTED_REQUEST_KEYS: tuple[tuple[type[schemas.RequestModel], tuple[str, ...]], ...] = (
    (DecisionPatchIn, ("status", "daily_budget", "total_budget")),
    (IntendedStateIn, ("status", "daily_budget", "total_budget")),
    (DecisionRequest, ("recommendation_id", "note")),
    (ResolveRequest, ("intended_state", "note")),
    (FeedbackRequest, ("note",)),
)
"""Every request model with a defaulted field and the keys a client may omit (``?`` in TS)."""


def test_the_defaulted_response_keys_cover_every_defaulted_response_field() -> None:
    listed = {model: set(keys) for model, _, keys in _DEFAULTED_RESPONSE_KEYS}
    assert len(listed) == 11
    assert sum(len(keys) for keys in listed.values()) == 21
    for model, keys in listed.items():
        defaulted = {name for name, field in model.model_fields.items() if not field.is_required()}
        assert defaulted == keys, model.__name__
        assert all(model.model_fields[key].default is None for key in keys), model.__name__


@pytest.mark.parametrize(
    ("model", "payload", "keys"),
    _DEFAULTED_RESPONSE_KEYS,
    ids=[model.__name__ for model, _, _ in _DEFAULTED_RESPONSE_KEYS],
)
def test_a_response_model_emits_every_none_default_as_null(
    model: type[schemas.ApiModel],
    payload: Callable[[], dict[str, Any]],
    keys: tuple[str, ...],
) -> None:
    sent = payload()
    assert not set(keys) & set(sent)
    dumped = model.model_validate(sent).model_dump(mode="json")
    for key in keys:
        assert key in dumped, f"{model.__name__}.{key}"
        assert dumped[key] is None, f"{model.__name__}.{key}"


@pytest.mark.parametrize(
    ("model", "keys"),
    [(model, keys) for model, _, keys in _DEFAULTED_RESPONSE_KEYS],
    ids=[model.__name__ for model, _, _ in _DEFAULTED_RESPONSE_KEYS],
)
def test_a_response_schema_publishes_every_field_as_required(
    model: type[schemas.ApiModel], keys: tuple[str, ...]
) -> None:
    serialization = model.model_json_schema(mode="serialization")
    assert set(keys) <= set(serialization["required"]), model.__name__
    assert set(serialization["required"]) == set(serialization["properties"]), model.__name__
    # Only the published response schema changes: validation still fills the defaults.
    validation = model.model_json_schema(mode="validation")
    assert not set(keys) & set(validation.get("required", [])), model.__name__


@pytest.mark.parametrize(
    ("model", "keys"),
    _DEFAULTED_REQUEST_KEYS,
    ids=[model.__name__ for model, _ in _DEFAULTED_REQUEST_KEYS],
)
def test_a_request_schema_keeps_its_defaulted_fields_optional(
    model: type[schemas.RequestModel], keys: tuple[str, ...]
) -> None:
    validation = model.model_json_schema(mode="validation")
    assert set(keys) <= set(validation["properties"]), model.__name__
    assert not set(keys) & set(validation.get("required", [])), model.__name__
    assert model.model_config.get("json_schema_serialization_defaults_required") is None


# ---------------------------------------------------------------------------
# Published request schema: Money input and never-null decision fields
# ---------------------------------------------------------------------------

_EXPECTED_FIELD_MODELS: tuple[type[schemas.RequestModel], ...] = (DecisionPatchIn, IntendedStateIn)
"""The request models built on ``_ExpectedFieldsIn``: optional fields, never ``null``."""

_MONEY_FIELDS = ("daily_budget", "total_budget")


def _schema_types(fragment: object) -> set[str]:
    """Every JSON-schema ``type`` named anywhere in ``fragment``, nested branches included."""
    if isinstance(fragment, dict):
        found = {fragment["type"]} if isinstance(fragment.get("type"), str) else set()
        for value in fragment.values():
            found |= _schema_types(value)
        return found
    if isinstance(fragment, list):
        found = set()
        for item in fragment:
            found |= _schema_types(item)
        return found
    return set()


def _money_request_schema(model: type[BaseModel], field: str) -> dict[str, Any]:
    """The published validation-mode schema of one money field of ``model``."""
    schema: dict[str, Any] = model.model_json_schema(mode="validation")["properties"][field]
    return schema


def _published_money_accepts(schema: dict[str, Any], value: object) -> bool:
    """Whether a JSON value satisfies a published money schema.

    A Python ``float`` stands for a JSON number written with a decimal point and ``bool`` for
    ``true``/``false``; neither is a JSON-schema ``integer`` here. The checker implements
    exactly the keywords the schema uses, and the assertion keeps it that way.
    """
    accepted = False
    for branch in schema["anyOf"]:
        assert set(branch) <= {"type", "pattern", "minimum", "maximum"}, branch
        if branch["type"] == "string" and isinstance(value, str):
            accepted |= re.search(branch["pattern"], value) is not None
        elif branch["type"] == "integer" and type(value) is int:
            accepted |= branch["minimum"] <= value <= branch["maximum"]
    return accepted


@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_decision_fields_publish_optional_properties_with_no_null_branch_or_null_default(
    model: type[schemas.RequestModel],
) -> None:
    schema = model.model_json_schema(mode="validation")
    properties = schema["properties"]
    assert list(properties) == ["status", "daily_budget", "total_budget"]
    assert "required" not in schema
    for name, field in properties.items():
        assert "null" not in _schema_types(field), f"{model.__name__}.{name}"
        assert "default" not in field, f"{model.__name__}.{name}"
    assert properties["status"] == {
        "enum": ["active", "paused", "ended"],
        "title": "Status",
        "type": "string",
    }


@pytest.mark.parametrize("field", _MONEY_FIELDS)
@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_money_request_schema_is_a_decimal_string_or_a_whole_integer_never_a_number(
    model: type[schemas.RequestModel], field: str
) -> None:
    schema = _money_request_schema(model, field)
    assert [branch["type"] for branch in schema["anyOf"]] == ["string", "integer"]
    assert _schema_types(schema) == {"string", "integer"}
    assert not {"max_digits", "decimal_places"} & set(schema)
    string, integer = schema["anyOf"]
    assert set(string) == {"type", "pattern"}
    assert integer == {"type": "integer", "minimum": 1, "maximum": 10**12 - 1}
    description = schema["description"]
    for phrase in ("positive", "below 10^12", "minor unit", "422", "120.0"):
        assert phrase in description, phrase


def test_the_request_bodies_reference_the_null_free_decision_fields() -> None:
    decision = DecisionRequest.model_json_schema(mode="validation")
    assert decision["properties"]["patch"] == {"$ref": "#/$defs/DecisionPatchIn"}
    assert "patch" in decision["required"]
    assert decision["$defs"]["DecisionPatchIn"] == DecisionPatchIn.model_json_schema()
    resolve = ResolveRequest.model_json_schema(mode="validation")
    # ``intended_state`` itself may be omitted or null: only ``record_intended`` needs it.
    assert {"$ref": "#/$defs/IntendedStateIn"} in resolve["properties"]["intended_state"]["anyOf"]
    assert resolve["$defs"]["IntendedStateIn"] == IntendedStateIn.model_json_schema()
    for body in (decision["$defs"]["DecisionPatchIn"], resolve["$defs"]["IntendedStateIn"]):
        assert "null" not in _schema_types(body["properties"])
        assert "number" not in _schema_types(body["properties"])


@pytest.mark.parametrize("field", _MONEY_FIELDS)
def test_money_response_schema_stays_a_string(field: str) -> None:
    schema = Decision.model_json_schema(mode="serialization")["properties"][field]
    branches = [branch for branch in schema["anyOf"] if branch != {"type": "null"}]
    assert [branch["type"] for branch in branches] == ["string"]
    assert _schema_types(schema) == {"string", "null"}
    assert "pattern" not in branches[0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("120.50", Decimal("120.50")),
        ("12", Decimal(12)),
        ("0.5", Decimal("0.5")),
        ("0.000001", Decimal("0.000001")),
        ("999999999999.999999", Decimal("999999999999.999999")),
        (12, Decimal(12)),
        (1, Decimal(1)),
        (10**12 - 1, Decimal(10**12 - 1)),
    ],
)
@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_every_published_money_example_is_accepted_by_the_validators_and_the_decision_rules(
    model: type[schemas.RequestModel], value: object, expected: Decimal
) -> None:
    assert _published_money_accepts(_money_request_schema(model, "daily_budget"), value)
    amount = model.model_validate({"daily_budget": value}).present_fields()["daily_budget"]
    assert isinstance(amount, Decimal)
    assert amount == expected
    assert is_valid_amount(amount, source="google", currency="USD")


@pytest.mark.parametrize(
    "value",
    [
        120.5,
        120.0,
        0.1,
        True,
        None,
        "NaN",
        "Infinity",
        "1.1234567",
        "1000000000000",
        10**12,
        "0",
        "0.000000",
        0,
        "-5",
        -5,
        "",
        "twelve",
    ],
)
@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_the_published_money_schema_refuses_what_the_validators_or_decision_rules_refuse(
    model: type[schemas.RequestModel], value: object
) -> None:
    assert not _published_money_accepts(_money_request_schema(model, "total_budget"), value)
    try:
        amount = model.model_validate({"total_budget": value}).present_fields()["total_budget"]
    except ValidationError:
        return
    # Zero and negative amounts parse; ``ads_engine.decisions`` refuses them with a 422.
    assert not is_valid_amount(amount, source="google", currency="USD"), value


@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_the_decision_field_validators_still_refuse_null_and_floats(
    model: type[schemas.RequestModel],
) -> None:
    for field in ("status", *_MONEY_FIELDS):
        with pytest.raises(
            ValidationError, match=rf"omit the field instead of sending null: {field}"
        ):
            model.model_validate_json(f'{{"{field}": null}}')
    for number in ("120.5", "120.0"):
        with pytest.raises(ValidationError, match=r"not JSON numbers") as error:
            model.model_validate_json(f'{{"daily_budget": {number}}}')
        assert error.value.errors()[0]["loc"] == ("daily_budget",)


@pytest.mark.parametrize("model", _EXPECTED_FIELD_MODELS, ids=lambda model: model.__name__)
def test_the_decision_field_validators_still_accept_a_decimal_string_and_a_whole_integer(
    model: type[schemas.RequestModel],
) -> None:
    parsed = model.model_validate_json('{"daily_budget": "120.50", "total_budget": 12}')
    assert parsed.present_fields() == {
        "daily_budget": Decimal("120.50"),
        "total_budget": Decimal(12),
    }
    assert model.model_validate_json("{}").present_fields() == {}


# ---------------------------------------------------------------------------
# Config: the HubSpot mapping and CRM binding views of ``GET /config``
# ---------------------------------------------------------------------------

_AVAILABLE_BINDINGS = CrmBindings(
    mql=ResolvedBinding(
        available=True,
        property="hs_v2_date_entered_1093456",
        stage_id="1093456",
        label="First meeting booked",
    ),
    ql=ResolvedBinding(
        available=True,
        property="hs_v2_date_entered_salesqualifiedlead",
        stage_id="salesqualifiedlead",
        label="Sales Qualified Lead",
    ),
    google=GoogleCrmBinding(
        available=True,
        campaign_property="google_ads_campaign_id",
        touch_date_property="google_ads_touch_at",
    ),
    verified_at=datetime(2026, 10, 6, 2, 30, 15, 250000, tzinfo=ZoneInfo("America/New_York")),
)
"""Bindings verified at 06:30:15.25 UTC, stamped in a zone other than UTC."""

_MAPPING_JSON: dict[str, Any] = {
    "mql": {"mode": "stage_label", "stage_labels": ["First meeting booked"], "property": None},
    "ql": {
        "mode": "stage_label",
        "stage_labels": ["Qualified Lead", "Sales Qualified Lead"],
        "property": None,
    },
    "booking_sources": ["MEETINGS_PUBLIC"],
    "meeting_outcome_exclusions": ["CANCELED"],
}
"""``HubSpotMapping.to_json`` of the shipped ``config/hubspot_mapping.yaml`` (AAP 0.5.4.7)."""

_BINDING_VIEWS: tuple[type[schemas.ApiModel], ...] = (
    StageBindingView,
    HubSpotMappingView,
    ResolvedBindingView,
    GoogleCrmBindingView,
    CrmBindingsView,
)


def _without_instant(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "verified_at"}


def test_binding_mode_holds_exactly_the_mapping_modes() -> None:
    assert get_args(schemas.BindingMode) == ("stage_label", "property")
    assert get_args(schemas.BindingMode) == get_args(schemas.ConfigBindingMode.__value__)


def test_check_vocabularies_raises_when_the_binding_modes_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drifted = TypeAliasType("BindingMode", Literal["stage_label", "property", "formula"])
    monkeypatch.setattr(schemas, "ConfigBindingMode", drifted)
    with pytest.raises(
        RuntimeError,
        match=r"^api\.schemas\.BindingMode \[.*\] does not match "
        r"analysis\.thresholds\.BindingMode \['formula', ",
    ):
        schemas._check_vocabularies()


def test_hubspot_mapping_view_serialises_exactly_the_shipped_mapping(
    thresholds: Thresholds,
) -> None:
    mapping = thresholds.mapping
    view = HubSpotMappingView.from_mapping(mapping)
    assert view.model_dump(mode="json") == mapping.to_json() == _MAPPING_JSON
    assert HubSpotMappingView.model_validate(mapping.to_json()) == view
    assert HubSpotMappingView.model_validate_json(view.model_dump_json()) == view
    assert isinstance(view.mql, StageBindingView)
    assert isinstance(view.ql, StageBindingView)


def test_stage_binding_view_keeps_a_property_mode_binding() -> None:
    binding = StageBinding(
        mode="property",
        stage_labels=("First meeting booked",),
        property="date_first_meeting_was_booked",
    )
    view = StageBindingView.from_binding(binding)
    assert view.model_dump(mode="json") == binding.to_json()
    assert view.model_dump(mode="json") == {
        "mode": "property",
        "stage_labels": ["First meeting booked"],
        "property": "date_first_meeting_was_booked",
    }


def test_config_view_validates_the_shipped_mapping_and_serialises_it_unchanged(
    thresholds: Thresholds,
) -> None:
    payload = {
        "thresholds": thresholds.as_view(),
        "window": WindowConfig.from_thresholds(thresholds).model_dump(),
        "hubspot_mapping": thresholds.mapping.to_json(),
        "crm_bindings": _AVAILABLE_BINDINGS.to_json(),
        "companion_crm_bindings": None,
        "content_assets": [asset.to_json() for asset in thresholds.content_assets],
        "config_hash": thresholds.config_hash,
    }
    view = ConfigView.model_validate(payload)
    assert view.hubspot_mapping == HubSpotMappingView.from_mapping(thresholds.mapping)
    assert view.crm_bindings == CrmBindingsView.from_bindings(_AVAILABLE_BINDINGS)
    assert view.companion_crm_bindings is None
    dumped = view.model_dump(mode="json")
    assert dumped["hubspot_mapping"] == thresholds.mapping.to_json()
    assert _without_instant(dumped["crm_bindings"]) == _without_instant(
        _AVAILABLE_BINDINGS.to_json()
    )
    assert ConfigView.model_validate_json(view.model_dump_json()) == view


def test_crm_bindings_view_round_trips_available_bindings() -> None:
    view = CrmBindingsView.from_bindings(_AVAILABLE_BINDINGS)
    dumped = view.model_dump(mode="json")
    stored = _AVAILABLE_BINDINGS.to_json()
    assert list(dumped) == list(stored) == ["mql", "ql", "google", "verified_at"]
    assert _without_instant(dumped) == _without_instant(stored)
    assert stored["verified_at"] == "2026-10-06T06:30:15.250000+00:00"
    assert dumped["verified_at"] == "2026-10-06T06:30:15.250000Z"
    assert datetime.fromisoformat(dumped["verified_at"]) == _AVAILABLE_BINDINGS.verified_at
    assert CrmBindings.from_json(dumped) == _AVAILABLE_BINDINGS
    assert CrmBindingsView.model_validate(stored) == view


def test_crm_bindings_view_keeps_every_null_key_of_unavailable_bindings() -> None:
    reason = "CRM bindings not yet verified"
    bindings = CrmBindings.unavailable(reason, at=NOW)
    dumped = CrmBindingsView.from_bindings(bindings).model_dump(mode="json")
    unavailable = {
        "available": False,
        "property": None,
        "stage_id": None,
        "label": None,
        "reason": reason,
    }
    assert dumped == {
        "mql": unavailable,
        "ql": unavailable,
        "google": {
            "available": False,
            "campaign_property": None,
            "touch_date_property": None,
            "reason": reason,
        },
        "verified_at": "2026-10-06T12:00:00Z",
    }
    assert _without_instant(dumped) == _without_instant(bindings.to_json())
    assert CrmBindings.from_json(dumped) == bindings


@pytest.mark.parametrize(
    ("model", "payload", "error_type", "location"),
    [
        (
            StageBindingView,
            {**_MAPPING_JSON["mql"], "mode": "stage"},
            "literal_error",
            ("mode",),
        ),
        (
            StageBindingView,
            {"mode": "stage_label", "stage_labels": ["First meeting booked"]},
            "missing",
            ("property",),
        ),
        (
            HubSpotMappingView,
            {key: value for key, value in _MAPPING_JSON.items() if key != "ql"},
            "missing",
            ("ql",),
        ),
        (
            HubSpotMappingView,
            {**_MAPPING_JSON, "mql": {**_MAPPING_JSON["mql"], "mode": "stage"}},
            "literal_error",
            ("mql", "mode"),
        ),
        (
            ResolvedBindingView,
            {"available": True, "property": "hs_v2_date_entered_1", "label": None, "reason": None},
            "missing",
            ("stage_id",),
        ),
        (
            GoogleCrmBindingView,
            {"available": False, "campaign_property": None, "touch_date_property": None},
            "missing",
            ("reason",),
        ),
        (
            CrmBindingsView,
            _without_instant(_AVAILABLE_BINDINGS.to_json()),
            "missing",
            ("verified_at",),
        ),
        (
            CrmBindingsView,
            {**_AVAILABLE_BINDINGS.to_json(), "verified_at": "2026-10-06T06:30:15"},
            "timezone_aware",
            ("verified_at",),
        ),
    ],
)
def test_binding_views_refuse_an_unknown_mode_a_missing_key_and_a_naive_instant(
    model: type[BaseModel], payload: dict[str, Any], error_type: str, location: tuple[str, ...]
) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert [(item["type"], item["loc"]) for item in error.value.errors()] == [
        (error_type, location)
    ]


def test_config_view_refuses_an_open_mapping_document(thresholds: Thresholds) -> None:
    with pytest.raises(ValidationError) as error:
        ConfigView.model_validate(
            {
                "thresholds": thresholds.as_view(),
                "window": WindowConfig.from_thresholds(thresholds),
                "hubspot_mapping": {"mql": {"mode": "stage", "stage": "marketingqualifiedlead"}},
                "crm_bindings": None,
                "companion_crm_bindings": None,
                "content_assets": [],
                "config_hash": thresholds.config_hash,
            }
        )
    assert {item["loc"][0] for item in error.value.errors()} == {"hubspot_mapping"}


def test_config_view_serialisation_schema_references_the_binding_components() -> None:
    schema = ConfigView.model_json_schema(mode="serialization")
    definitions = schema["$defs"]
    properties = schema["properties"]
    assert properties["hubspot_mapping"] == {"$ref": "#/$defs/HubSpotMappingView"}
    for name in ("crm_bindings", "companion_crm_bindings"):
        assert properties[name]["anyOf"] == [
            {"$ref": "#/$defs/CrmBindingsView"},
            {"type": "null"},
        ], name
    assert schema["required"] == list(ConfigView.model_fields)
    for model in _BINDING_VIEWS:
        definition = definitions[model.__name__]
        assert definition["type"] == "object", model.__name__
        assert definition["required"] == list(model.model_fields), model.__name__
        assert "additionalProperties" not in definition, model.__name__
    mapping = definitions["HubSpotMappingView"]["properties"]
    assert mapping["mql"] == mapping["ql"] == {"$ref": "#/$defs/StageBindingView"}
    stage = definitions["StageBindingView"]["properties"]
    assert stage["mode"]["enum"] == ["stage_label", "property"]
    assert stage["stage_labels"]["items"] == {"type": "string"}
    crm = definitions["CrmBindingsView"]["properties"]
    assert crm["mql"] == crm["ql"] == {"$ref": "#/$defs/ResolvedBindingView"}
    assert crm["google"] == {"$ref": "#/$defs/GoogleCrmBindingView"}
    assert crm["verified_at"]["format"] == "date-time"
    resolved = definitions["ResolvedBindingView"]["properties"]
    for name in ("property", "stage_id", "label", "reason"):
        assert resolved[name]["anyOf"] == [{"type": "string"}, {"type": "null"}], name
    google = definitions["GoogleCrmBindingView"]["properties"]
    for name in ("campaign_property", "touch_date_property", "reason"):
        assert google[name]["anyOf"] == [{"type": "string"}, {"type": "null"}], name


# ---------------------------------------------------------------------------
# Decision patches and discrepancy values: stored jsonb returned key for key
# ---------------------------------------------------------------------------

C0_CAMPAIGN = SubjectRef(type="campaign", id=12)
"""The campaign subject of :func:`_decision_row` and :func:`_discrepancy_row`."""

C0_BUDGET = SubjectRef(type="budget", id=4)
"""A Google explicitly shared budget subject."""

MEMBER_BUDGET = 7
"""The explicitly shared budget a member campaign was decided onto."""

MOVED_BUDGET = 8
"""Another explicitly shared budget, which the member draws from after a move."""

GOOGLE_MONEY_KEYS = {"daily_budget", "total_budget", "currency", "amount_micros"}
LINKEDIN_MONEY_KEYS = {"daily_budget", "total_budget", "currency"}
ASSIGNMENT_KEYS = {"budget_id", "budget_external_id"}


def _as_stored(value: dict[str, Any] | None) -> Any:
    """Return ``value`` as a ``jsonb`` column stores and returns it."""
    return json.loads(json.dumps(value))


def _json_text(value: object) -> str:
    """Render ``value`` canonically, so ``True`` never passes for ``1``, nor ``100.0`` for 100."""
    return json.dumps(value, sort_keys=True)


def _external_id(budget_id: int) -> str:
    """Return the Google id of a shared budget."""
    return str(5_550_000 + budget_id)


def _c0_state(
    *,
    ref: SubjectRef = C0_CAMPAIGN,
    source: Literal["google", "linkedin"] = "google",
    budget_id: int | None = None,
    decision: DecisionRow | None = None,
) -> SubjectState:
    """Return a subject's current state; Google budgets are ``DAILY``, LinkedIn's daily.

    ``budget_id`` makes a Google campaign a shared member of that budget now.
    """
    shape: Literal["own_budget", "shared_member", "budget"]
    if ref.type == "budget":
        shape = "budget"
    else:
        shape = "own_budget" if budget_id is None else "shared_member"
    decided_budget = None if decision is None else decision.budget_id
    return SubjectState(
        ref=ref,
        dataset_id=1,
        dataset_state="active",
        source=source,
        currency="EUR" if source == "linkedin" else "USD",
        name="Brand - US" if ref.type == "campaign" else f"Shared budget {ref.id}",
        shape=shape,
        has_daily_budget=shape != "shared_member",
        has_total_budget=False,
        budget_period=None if source == "linkedin" else "DAILY",
        budget_id=budget_id,
        budget_external_id=None if budget_id is None else _external_id(budget_id),
        latest_snapshot=None,
        current_decision=decision,
        decided_budget_external_id=None if decided_budget is None else _external_id(decided_budget),
    )


def _c0_snapshot(ref: SubjectRef = C0_CAMPAIGN, **columns: object) -> EntitySnapshot:
    """Return the subject's newest snapshot, captured at ``NOW``."""
    values: dict[str, object] = {
        "id": 88,
        "dataset_id": 1,
        "source": "google",
        "entity_type": ref.type,
        "entity_id": ref.id,
        "captured_at": NOW,
        "status": None if ref.type == "budget" else "active",
        "budget_daily": None,
        "budget_total": None,
        "budget_amount_micros": None,
        "budget_id": None,
    }
    values.update(columns)
    return EntitySnapshot(**values)


def _own_decision(**overrides: Any) -> DecisionRow:
    """Return a Google ``own_budget`` decision: active, 100 a day."""
    values: dict[str, Any] = {"daily_budget": Decimal("100.000000"), **overrides}
    return _decision_row(**values)


def _member_decision(budget_id: int = MEMBER_BUDGET, **overrides: Any) -> DecisionRow:
    """Return a ``shared_member`` decision assigning ``budget_id``."""
    values: dict[str, Any] = {
        "shape": "shared_member",
        "daily_budget": None,
        "budget_id": budget_id,
        "patch": {},
        **overrides,
    }
    return _decision_row(**values)


def _budget_decision(**overrides: Any) -> DecisionRow:
    """Return a ``budget`` decision on :data:`C0_BUDGET`: 50 a day, no status."""
    values: dict[str, Any] = {
        "subject_type": "budget",
        "subject_id": C0_BUDGET.id,
        "shape": "budget",
        "status": None,
        "daily_budget": Decimal("50.000000"),
        "origin": "baseline",
        "decided_by": None,
        "recommendation_id": None,
        "patch": {},
        **overrides,
    }
    return _decision_row(**values)


DAY_BEFORE = {"decided_at": NOW - timedelta(days=1)}
"""A tombstone written before the snapshot that reports the entity again."""

C0Case = tuple[str, SubjectState, dict[str, Any] | None, dict[str, Any] | None]
"""A discrepancy's kind, its subject's state and both values, as ``decisions`` built them."""


def _compared(state: SubjectState, snapshot: EntitySnapshot, decision: DecisionRow) -> C0Case:
    """Return the one mismatch ``decisions.compare`` finds, as a case."""
    [mismatch] = compare(snapshot, decision, state=state)
    return mismatch.kind, state, mismatch.decided_value, mismatch.platform_value


def _status_case() -> C0Case:
    return _compared(_c0_state(), _c0_snapshot(status="paused"), _own_decision())


def _campaign_tombstone_case(status: str | None) -> C0Case:
    decision = _own_decision(status="ended", tombstone=True, origin="resolution", **DAY_BEFORE)
    return _compared(_c0_state(), _c0_snapshot(status=status), decision)


def _google_money_case() -> C0Case:
    snapshot = _c0_snapshot(budget_amount_micros=120_500_000)
    return _compared(_c0_state(), snapshot, _own_decision())


def _linkedin_money_case() -> C0Case:
    snapshot = _c0_snapshot(source="linkedin", budget_daily=Decimal("90.00"))
    decision = _own_decision(source="linkedin", daily_budget=Decimal("120.50"))
    return _compared(_c0_state(source="linkedin"), snapshot, decision)


def _budget_money_case() -> C0Case:
    snapshot = _c0_snapshot(C0_BUDGET, budget_amount_micros=60_000_000)
    return _compared(_c0_state(ref=C0_BUDGET), snapshot, _budget_decision())


def _budget_tombstone_case() -> C0Case:
    decision = _budget_decision(tombstone=True, origin="resolution", **DAY_BEFORE)
    snapshot = _c0_snapshot(C0_BUDGET, budget_amount_micros=50_000_000)
    return _compared(_c0_state(ref=C0_BUDGET), snapshot, decision)


def _moved_onto_shared_case() -> C0Case:
    snapshot = _c0_snapshot(budget_id=MEMBER_BUDGET)
    return _compared(_c0_state(budget_id=MEMBER_BUDGET), snapshot, _own_decision())


def _member_moved_case() -> C0Case:
    decision = _member_decision()
    state = _c0_state(budget_id=MOVED_BUDGET, decision=decision)
    return _compared(state, _c0_snapshot(budget_id=MOVED_BUDGET), decision)


def _own_untracked_case() -> C0Case:
    state = _c0_state()
    snapshot = _c0_snapshot(status="paused", budget_amount_micros=75_250_000)
    return "untracked", state, None, describe_snapshot(snapshot, state=state)


def _linkedin_untracked_unreported_case() -> C0Case:
    state = _c0_state(source="linkedin")
    snapshot = _c0_snapshot(source="linkedin", status=None, budget_total=Decimal("900.00"))
    return "untracked", state, None, describe_snapshot(snapshot, state=state)


def _member_untracked_case() -> C0Case:
    state = _c0_state(budget_id=MEMBER_BUDGET)
    snapshot = _c0_snapshot(status="paused", budget_id=MEMBER_BUDGET)
    return "untracked", state, None, describe_snapshot(snapshot, state=state)


def _budget_intended_case() -> C0Case:
    state = _c0_state(ref=C0_BUDGET)
    intended = ExpectedState(shape="budget", daily_budget=Decimal("65.5"))
    snapshot = _c0_snapshot(C0_BUDGET, budget_amount_micros=60_000_000)
    return (
        "untracked",
        state,
        describe_expected(intended, state=state),
        describe_snapshot(snapshot, state=state),
    )


def _own_missing_case() -> C0Case:
    state = _c0_state()
    return "missing", state, describe_decision(_own_decision(), state=state), None


def _member_missing_case() -> C0Case:
    state = _c0_state(budget_id=MEMBER_BUDGET)
    return "missing", state, describe_decision(_member_decision(), state=state), None


def _tombstone_state_case() -> C0Case:
    state = _c0_state()
    decision = _own_decision(status="ended", tombstone=True, origin="resolution")
    return "missing", state, describe_decision(decision, state=state), None


C0_CASES: list[tuple[Callable[[], C0Case], set[str] | None, set[str] | None]] = [
    pytest.param(_status_case, {"status"}, {"status"}, id="status"),
    pytest.param(
        lambda: _campaign_tombstone_case("active"),
        {"status", "tombstone"},
        {"status"},
        id="campaign-tombstone-reappears",
    ),
    pytest.param(
        lambda: _campaign_tombstone_case(None),
        {"status", "tombstone"},
        {"status"},
        id="campaign-tombstone-reappears-without-status",
    ),
    pytest.param(_google_money_case, GOOGLE_MONEY_KEYS, GOOGLE_MONEY_KEYS, id="google-money"),
    pytest.param(
        _linkedin_money_case, LINKEDIN_MONEY_KEYS, LINKEDIN_MONEY_KEYS, id="linkedin-money"
    ),
    pytest.param(_budget_money_case, GOOGLE_MONEY_KEYS, GOOGLE_MONEY_KEYS, id="budget-money"),
    pytest.param(
        _budget_tombstone_case,
        GOOGLE_MONEY_KEYS | {"tombstone"},
        GOOGLE_MONEY_KEYS,
        id="budget-tombstone-reappears",
    ),
    pytest.param(_moved_onto_shared_case, ASSIGNMENT_KEYS, ASSIGNMENT_KEYS, id="moved-onto-shared"),
    pytest.param(_member_moved_case, ASSIGNMENT_KEYS, ASSIGNMENT_KEYS, id="assignment"),
    pytest.param(
        _own_untracked_case, None, {"status"} | GOOGLE_MONEY_KEYS, id="own-budget-untracked"
    ),
    pytest.param(
        _linkedin_untracked_unreported_case,
        None,
        {"status"} | LINKEDIN_MONEY_KEYS,
        id="linkedin-untracked-unreported-status",
    ),
    pytest.param(_member_untracked_case, None, {"status"} | ASSIGNMENT_KEYS, id="member-untracked"),
    pytest.param(
        _budget_intended_case,
        GOOGLE_MONEY_KEYS,
        GOOGLE_MONEY_KEYS,
        id="budget-record-intended",
    ),
    pytest.param(_own_missing_case, {"status"} | GOOGLE_MONEY_KEYS, None, id="own-budget-missing"),
    pytest.param(
        _member_missing_case, {"status"} | ASSIGNMENT_KEYS, None, id="shared-member-missing"
    ),
    pytest.param(
        _tombstone_state_case,
        {"status", "tombstone"} | GOOGLE_MONEY_KEYS,
        None,
        id="tombstone-complete-state",
    ),
]
"""Every value variant the ``decisions`` writers store, built with those writers."""


@pytest.mark.parametrize(("case", "decided_keys", "platform_keys"), C0_CASES)
def test_discrepancy_values_return_every_stored_variant_key_for_key(
    case: Callable[[], C0Case], decided_keys: set[str] | None, platform_keys: set[str] | None
) -> None:
    kind, state, decided, platform = case()
    stored_decided, stored_platform = _as_stored(decided), _as_stored(platform)
    assert (None if stored_decided is None else set(stored_decided)) == decided_keys
    assert (None if stored_platform is None else set(stored_platform)) == platform_keys
    row = _discrepancy_row(
        source=state.source,
        subject_type=state.ref.type,
        subject_id=state.ref.id,
        kind=kind,
        decided_value=stored_decided,
        platform_value=stored_platform,
    )
    discrepancy = Discrepancy.from_row(
        row,
        subject=_subject(type=state.ref.type, id=state.ref.id, source=state.source),
        shape=state.shape,
        resolutions=RESOLUTIONS[(kind, state.shape)],
    )
    dumped = discrepancy.model_dump(mode="json")
    wire = json.loads(discrepancy.model_dump_json())
    for side, stored in (("decided_value", stored_decided), ("platform_value", stored_platform)):
        assert _json_text(dumped[side]) == _json_text(stored), side
        assert _json_text(wire[side]) == _json_text(stored), side


def test_discrepancy_amounts_keep_their_stored_decimal_text_and_integer_micros() -> None:
    for case, decided, platform in (
        (
            _google_money_case,
            {
                "daily_budget": "100.000000",
                "total_budget": None,
                "currency": "USD",
                "amount_micros": 100_000_000,
            },
            {
                "daily_budget": "120.500000",
                "total_budget": None,
                "currency": "USD",
                "amount_micros": 120_500_000,
            },
        ),
        (
            _linkedin_money_case,
            {"daily_budget": "120.50", "total_budget": None, "currency": "EUR"},
            {"daily_budget": "90.00", "total_budget": None, "currency": "EUR"},
        ),
    ):
        kind, state, decided_value, platform_value = case()
        discrepancy = Discrepancy.from_row(
            _discrepancy_row(
                kind=kind,
                decided_value=_as_stored(decided_value),
                platform_value=_as_stored(platform_value),
            ),
            subject=_subject(source=state.source),
            shape=state.shape,
            resolutions=RESOLUTIONS[(kind, state.shape)],
        )
        wire = json.loads(discrepancy.model_dump_json())
        assert _json_text(wire["decided_value"]) == _json_text(decided)
        assert _json_text(wire["platform_value"]) == _json_text(platform)


@pytest.mark.parametrize(
    ("submitted", "stored"),
    [
        (
            {"status": "paused", "daily_budget": "120.50"},
            {"status": "paused", "daily_budget": "120.50"},
        ),
        ({"total_budget": "100.000000"}, {"total_budget": "100.000000"}),
        ({"daily_budget": "1E+2"}, {"daily_budget": "100"}),
        ({"status": "ended"}, {"status": "ended"}),
    ],
)
def test_a_user_decision_returns_the_patch_it_stored_key_for_key(
    submitted: dict[str, str], stored: dict[str, str]
) -> None:
    patch = _as_stored(DecisionPatch.from_mapping(submitted).as_json())
    assert _json_text(patch) == _json_text(stored)
    decision = Decision.from_row(_decision_row(patch=patch), subject=_subject())
    assert _json_text(decision.model_dump(mode="json")["patch"]) == _json_text(stored)
    assert _json_text(json.loads(decision.model_dump_json())["patch"]) == _json_text(stored)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"origin": "baseline", "decided_by": None}, id="baseline"),
        pytest.param({"origin": "resolution"}, id="resolution"),
        pytest.param(
            {"origin": "resolution", "status": "ended", "tombstone": True}, id="tombstone"
        ),
    ],
)
def test_baseline_and_resolution_decisions_return_an_empty_patch(overrides: dict[str, Any]) -> None:
    decision = Decision.from_row(
        _decision_row(recommendation_id=None, patch={}, **overrides), subject=_subject()
    )
    assert decision.model_dump(mode="json")["patch"] == {}
    assert json.loads(decision.model_dump_json())["patch"] == {}


def test_stored_decision_json_drops_unknown_keys_as_every_response_model_does() -> None:
    decision = Decision.from_row(
        _decision_row(patch={"daily_budget": "120.50", "budget_id": 4}), subject=_subject()
    )
    assert decision.model_dump(mode="json")["patch"] == {"daily_budget": "120.50"}
    discrepancy = Discrepancy.from_row(
        _discrepancy_row(decided_value={"status": "active", "legacy": 1}),
        subject=_subject(),
        shape="own_budget",
        resolutions=[],
    )
    assert discrepancy.model_dump(mode="json")["decided_value"] == {"status": "active"}
    for view in (schemas.DecisionPatchView, schemas.DiscrepancyValue):
        assert view.__pydantic_config__["extra"] == "ignore", view.__name__


@pytest.mark.parametrize(
    ("patch", "error_type"),
    [
        ({"status": "removed"}, "literal_error"),
        ({"daily_budget": None}, "decimal_type"),
        ({"total_budget": 12.5}, "value_error"),
        ({"daily_budget": "0.0000001"}, "decimal_max_places"),
    ],
)
def test_decision_patch_rejects_a_stored_value_outside_the_contract(
    patch: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Decision.from_row(_decision_row(patch=patch), subject=_subject())
    assert _error_types(error) == [error_type]


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        ({"status": "removed"}, "literal_error"),
        ({"amount_micros": "many"}, "int_parsing"),
        ({"tombstone": "maybe"}, "bool_parsing"),
        ({"daily_budget": 12.5}, "value_error"),
    ],
)
def test_discrepancy_value_rejects_a_stored_value_outside_the_contract(
    value: dict[str, Any], error_type: str
) -> None:
    with pytest.raises(ValidationError) as error:
        Discrepancy.from_row(
            _discrepancy_row(platform_value=value),
            subject=_subject(),
            shape="own_budget",
            resolutions=[],
        )
    assert _error_types(error) == [error_type]


def test_the_response_schemas_publish_the_named_patch_and_value_properties() -> None:
    decision = Decision.model_json_schema(mode="serialization")
    assert decision["properties"]["patch"] == {"$ref": "#/$defs/DecisionPatchView"}
    patch = decision["$defs"]["DecisionPatchView"]
    assert patch["type"] == "object"
    assert "required" not in patch
    assert list(patch["properties"]) == ["status", "daily_budget", "total_budget"]
    assert patch["properties"]["status"]["enum"] == list(get_args(schemas.CampaignStatus))
    for name in ("daily_budget", "total_budget"):
        assert patch["properties"][name]["type"] == "string", name

    discrepancy = Discrepancy.model_json_schema(mode="serialization")
    for name in ("decided_value", "platform_value"):
        assert discrepancy["properties"][name]["anyOf"] == [
            {"$ref": "#/$defs/DiscrepancyValue"},
            {"type": "null"},
        ], name
    value = discrepancy["$defs"]["DiscrepancyValue"]
    assert value["type"] == "object"
    assert "required" not in value
    assert list(value["properties"]) == [
        "status",
        "daily_budget",
        "total_budget",
        "currency",
        "amount_micros",
        "budget_id",
        "budget_external_id",
        "tombstone",
    ]
    for name, prop in value["properties"].items():
        if name == "tombstone":
            assert prop["type"] == "boolean"
        else:
            assert prop["anyOf"][-1] == {"type": "null"}, name
    assert value["properties"]["status"]["anyOf"][0]["enum"] == list(
        get_args(schemas.CampaignStatus)
    )
    for name in ("daily_budget", "total_budget"):
        assert value["properties"][name]["anyOf"][0]["type"] == "string", name


def test_the_stored_json_views_are_exported() -> None:
    assert {"DecisionPatchView", "DiscrepancyValue"} <= set(schemas.__all__)
