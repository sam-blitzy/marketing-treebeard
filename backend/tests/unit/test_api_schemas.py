"""Unit tests for ``ads_engine.api.schemas``: the ``/api/v1`` request and response contract.

Each test asserts behaviour that AAP 0.5.3 or the module's docstrings set:

* shared scalars: :data:`Money` refuses floats, booleans, non-finite and over-precise
  amounts and serialises as a fixed-point decimal string; :data:`MetricValue` renders a
  ``Decimal`` exactly as ``analysis.context.json_value`` freezes it and keeps ``None``
  ("unavailable") as ``null``, never ``0``;
* vocabularies: every ``Literal`` holds exactly the AAP values and its database or analysis
  tuple, and :func:`_check_vocabularies` raises on drift in values, count or section order;
* base classes: responses ignore unknown keys and validate from ORM attributes, requests
  forbid unknown keys, and no field is aliased, so the wire names are the snake_case field
  names;
* requests: :class:`LoginRequest`, :class:`CreateRunRequest` (whose window bounds
  ``AnalysisWindow.custom`` enforces), :class:`FeedbackRequest`, :class:`DecisionRequest`,
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
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

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
    Group,
    Health,
    IntendedStateIn,
    Item,
    ItemOverlay,
    LoginRequest,
    Me,
    Metric,
    Overlay,
    Projection,
    RecommendationsPage,
    ResolveRequest,
    Run,
    Sample,
    Section,
    Source,
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
from ads_engine.decisions import RESOLUTIONS

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
        ({"window_start": "2026-09-08T10:30:00", "window_end": "2026-10-05"}, None),
        ({"window_start": "last month", "window_end": "2026-10-05"}, None),
        ({"window_start": "2026-09-08"}, "missing"),
    ],
)
def test_create_run_request_refuses_anything_but_two_calendar_dates(
    payload: dict[str, Any], error_type: str | None
) -> None:
    with pytest.raises(ValidationError) as error:
        CreateRunRequest.model_validate(payload)
    types = _error_types(error)
    assert len(types) == 1
    if error_type is None:
        assert types[0].startswith("date_")
    else:
        assert types == [error_type]


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
        hubspot_mapping={"mql": {"mode": "stage", "stage": "marketingqualifiedlead"}},
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
