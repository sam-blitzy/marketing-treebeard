"""Contract tests for ``ads_engine.connectors.linkedin.parser``.

Every payload here is traceable to ``backend/tests/contracts/linkedin/*.json``. Each documented
sample runs through its parser verbatim; the only substitutions are the two the parser
docstrings name: the ``ad_analytics`` sample's organization pivot value is replaced by a
creative URN, and the ``ad_analytics_statistics`` sample gains the demographic value its
single-pivot form lacks. Every hand-built element starts from a documented sample, uses only
fields its contract defines, and is checked against that contract's response schema before it
is parsed, so an element carrying an undeclared field fails the test. Deliberately malformed
inputs for the defect tests are the one exception.

Identifiers are clearly fake (``99000…`` ids), and lead emails are ``example.com`` addresses
that exist in memory only. Everything is pure and synchronous: no database and no network.
"""

import copy
import dataclasses
import functools
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from typing import Any, cast

import pytest

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.linkedin import parser
from ads_engine.connectors.linkedin.parser import (
    CAMPAIGN_GROUP_SEARCH_STATUSES,
    CAMPAIGN_GROUP_STATUS_MAP,
    CAMPAIGN_SEARCH_STATUSES,
    CAMPAIGN_STATUS_MAP,
    CREATIVE_INTENDED_STATUSES,
    CREATIVE_STATUS_MAP,
    PREFIX_ACCOUNT,
    PREFIX_CAMPAIGN,
    PREFIX_CAMPAIGN_GROUP,
    PREFIX_CREATIVE,
    PREFIX_LEAD_FORM,
    REASON_FORM_MAP_MISMATCH,
    REASON_NO_EMAIL_ANSWER,
    REASON_NO_EMAIL_QUESTION,
    REASON_UNKNOWN_FORM_VERSION,
    SEGMENT_FACETS,
    SEGMENT_PIVOTS,
    AdAccountRow,
    CampaignGroupRow,
    CampaignRow,
    CreativeDailyRow,
    CreativeRow,
    DecodedEmail,
    FormVersionMap,
    LeadFormRow,
    Money,
    ParsedLead,
    PostRow,
    SegmentRow,
    canonical_form_urn,
    decode_email,
    form_map_from_decoration,
    parse_ad_account,
    parse_analytics_daily,
    parse_campaign,
    parse_campaign_group,
    parse_creative,
    parse_lead,
    parse_lead_form,
    parse_post,
    parse_segment,
    parse_versioned_form_urn,
    sponsored_account_urn,
    sponsored_campaign_urn,
    urn_id,
)

pytestmark = pytest.mark.contract

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "linkedin"

# ---------------------------------------------------------------------------------------------
# Contract files: loading, element schemas and the conformance check
# ---------------------------------------------------------------------------------------------

# Where each parsed surface's element schema sits inside its contract's response body schema.
_ELEMENT_SCHEMA_PATHS: Mapping[str, tuple[str, ...]] = {
    "ad_account": (),
    "ad_campaign_groups": ("properties", "elements", "items"),
    "ad_campaigns": ("properties", "elements", "items"),
    "creatives": ("properties", "elements", "items"),
    "posts": ("properties", "results", "additionalProperties"),
    "ad_analytics": ("properties", "elements", "items"),
    "ad_analytics_statistics": ("properties", "elements", "items"),
    "lead_forms": ("properties", "elements", "items"),
    "lead_form_responses": ("properties", "elements", "items"),
    "lead_form_responses_batch": ("properties", "results", "additionalProperties"),
}
PARSED_SURFACES = tuple(_ELEMENT_SCHEMA_PATHS)

# The JSON Schema keywords the LinkedIn contracts use. Any other keyword fails the check
# rather than being silently ignored.
_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}
)
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


@functools.cache
def _contract(surface: str) -> Mapping[str, Any]:
    """Load a contract file the way ``connectors/http.py`` decodes bodies (floats as Decimal)."""
    text = (CONTRACTS / f"{surface}.json").read_text(encoding="utf-8")
    loaded: Mapping[str, Any] = json.loads(text, parse_float=Decimal)
    return loaded


def _sample(surface: str) -> Any:
    """Return a private deep copy of the contract's documented sample response."""
    sample = _contract(surface)["response"]["documented_sample"]
    assert sample is not None, f"{surface} has no documented sample"
    return copy.deepcopy(sample)


def _sample_element(surface: str, index: int = 0) -> Any:
    """Return one element of a documented ``elements`` sample."""
    return _sample(surface)["elements"][index]


def _element_schema(surface: str) -> Mapping[str, Any]:
    """Return the schema one parsed element of ``surface`` must satisfy."""
    schema: Mapping[str, Any] = _contract(surface)["response"]["body"]
    for key in _ELEMENT_SCHEMA_PATHS[surface]:
        schema = schema[key]
    return schema


def _schema_errors(value: object, schema: Mapping[str, Any], path: str) -> list[str]:
    """Validate ``value`` against a contract schema; objects are closed unless declared open."""
    unknown = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unknown:
        return [f"{path}: unsupported schema keywords {unknown}"]
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_JSON_TYPES[name](value) for name in names):
            return [f"{path}: {type(value).__name__} is not {names}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not a declared enum value")
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        errors.extend(
            f"{path}: required field {key!r} is missing"
            for key in schema.get("required", ())
            if key not in value
        )
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_errors(item, properties[key], f"{path}.{key}"))
            elif isinstance(additional, Mapping):
                errors.extend(_schema_errors(item, additional, f"{path}.{key}"))
            elif additional is not True:
                errors.append(f"{path}: field {key!r} is not defined by the contract")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def _conforming[T](surface: str, element: T) -> T:
    """Return ``element`` after asserting it satisfies its contract's element schema."""
    errors = _schema_errors(element, _element_schema(surface), surface)
    assert errors == [], errors
    return element


def _enum(surface: str, field_name: str) -> list[str]:
    """Return the enum the contract declares for a top-level element field."""
    values: list[str] = _element_schema(surface)["properties"][field_name]["enum"]
    return values


def _request_list(surface: str, pattern: str) -> tuple[str, ...]:
    """Return the Rest.li ``List(...)`` the contract's request URL sends after ``pattern``."""
    url: str = _contract(surface)["request"]["url"]
    match = re.search(re.escape(pattern) + r"List\(([A-Z_,]+)\)", url)
    assert match is not None, url
    return tuple(match.group(1).split(","))


def _expect_defect(call: Callable[[], object], surface: str, fragment: str) -> QueryDefect:
    """Assert ``call`` raises the parser's validation defect for ``surface`` naming ``fragment``."""
    with pytest.raises(QueryDefect) as caught:
        call()
    error = caught.value
    assert error.code == "validation"
    assert error.source == "linkedin"
    assert error.endpoint == surface
    assert error.message.startswith(f"{surface}: ")
    assert fragment in error.message, error.message
    return error


# ---------------------------------------------------------------------------------------------
# Fake identifiers and lead-element builders
# ---------------------------------------------------------------------------------------------

ACCOUNT_URN = "urn:li:sponsoredAccount:990000001"
GROUP_URN = "urn:li:sponsoredCampaignGroup:990000201"
CAMPAIGN_URN = "urn:li:sponsoredCampaign:990000101"
OTHER_CAMPAIGN_URN = "urn:li:sponsoredCampaign:990000102"
CREATIVE_URN = "urn:li:sponsoredCreative:990001101"
OTHER_CREATIVE_URN = "urn:li:sponsoredCreative:990001102"
FORM_ID = 990002001
FORM_URN = "urn:li:leadGenForm:990002001"
FORM_VERSION = 3
VERSIONED_FORM_URN = "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990002001,3)"
LEAD_ID = "ffffeeee-0000-dddd-1111-cccc2222bbbb-9"

FIRST_NAME_QID = 10
EMAIL_QID = 20
WORK_EMAIL_QID = 30
CUSTOM_QID = 40
UNDECORATED_QID = 77

RAW_EMAIL = "  Lead.One@Example.COM "
EMAIL = "lead.one@example.com"
OTHER_EMAIL = "lead.two@example.com"
EMAIL_SHA256 = hashlib.sha256(EMAIL.encode("utf-8")).hexdigest()
OTHER_EMAIL_SHA256 = hashlib.sha256(OTHER_EMAIL.encode("utf-8")).hexdigest()

# The documented lead sample (lead_form_responses.json and lead_form_responses_batch.json).
SAMPLE_LEAD_ID = "aaaabbbb-0000-cccc-1111-dddd2222eeee-5"
SAMPLE_FORM_URN = "urn:li:leadGenForm:3162"
SAMPLE_ANSWER = "Hello World-testing purpose"


def _question(question_id: int, predefined: str | None = None) -> dict[str, Any]:
    """A form question with the contract's ``questionId``, ``name`` and ``predefinedField``."""
    question: dict[str, Any] = {"questionId": question_id, "name": f"question{question_id}"}
    if predefined is not None:
        question["predefinedField"] = predefined
    return question


def _standard_questions() -> list[dict[str, Any]]:
    """First name, EMAIL, WORK_EMAIL and a custom question without a predefined field."""
    return [
        _question(FIRST_NAME_QID, "FIRST_NAME"),
        _question(EMAIL_QID, "EMAIL"),
        _question(WORK_EMAIL_QID, "WORK_EMAIL"),
        _question(CUSTOM_QID),
    ]


def _text_answer(question_id: int, text: str) -> dict[str, Any]:
    """A text answer, shaped as the documented sample's first answer."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": text}}}


def _choice_answer(question_id: int) -> dict[str, Any]:
    """A multiple-choice answer, shaped as the documented sample's second answer."""
    return {"questionId": question_id, "answerDetails": {"multipleChoiceAnswer": {"options": [1]}}}


def _lead(
    answers: Sequence[Mapping[str, Any]],
    *,
    questions: Sequence[Mapping[str, Any]] | None = None,
    decorated: bool = True,
    versioned_urn: str = VERSIONED_FORM_URN,
    test_lead: bool = False,
) -> dict[str, Any]:
    """A projected (``LEAD_FIELDS``) lead response built from the documented sample.

    Under the projection LinkedIn omits ``submitter``, ``associatedEntity`` and
    ``leadMetadata`` and adds the ``form`` decoration (the contract's KEY RISK note), so those
    keys are dropped and the decoration added. Every other key is the sample's own, with fake
    ids. ``questions`` defaults to :func:`_standard_questions`.
    """
    element: dict[str, Any] = _sample_element("lead_form_responses")
    for key in ("submitter", "associatedEntity", "leadMetadata"):
        del element[key]
    element["id"] = LEAD_ID
    element["testLead"] = test_lead
    element["owner"]["sponsoredAccount"] = ACCOUNT_URN
    element["versionedLeadGenFormUrn"] = versioned_urn
    element["leadMetadataInfo"]["sponsoredLeadMetadataInfo"]["campaign"]["id"] = CAMPAIGN_URN
    element["associatedEntityInfo"]["associatedCreative"]["id"] = CREATIVE_URN
    element["formResponse"]["answers"] = [dict(answer) for answer in answers]
    if decorated:
        element["form"] = {
            "id": FORM_ID,
            "name": "Fake white paper form",
            "content": {
                "questions": [
                    dict(question)
                    for question in (_standard_questions() if questions is None else questions)
                ]
            },
        }
    return _conforming("lead_form_responses", element)


def _stored(
    version: int = FORM_VERSION,
    email_ids: Sequence[str] = (str(EMAIL_QID),),
) -> dict[tuple[str, int], FormVersionMap]:
    """Known versions holding one map rebuilt from a ``lead_form_version`` row."""
    stored = FormVersionMap.from_storage(FORM_URN, version, email_ids)
    return {(FORM_URN, version): stored}


# ---------------------------------------------------------------------------------------------
# Contracts and module surface
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("surface", PARSED_SURFACES)
def test_documented_sample_conforms_to_its_contract(surface: str) -> None:
    contract = _contract(surface)
    assert contract["source"] == "linkedin"
    assert contract["api_version"] == "202609"
    assert contract["reference_url"].startswith("https://learn.microsoft.com/")
    assert _schema_errors(_sample(surface), contract["response"]["body"], surface) == []


# The cursor-paged search surfaces. Their official pages document that metadata.nextPageToken
# is absent or null on the last page, so both terminal shapes must validate.
CURSOR_SURFACES = ("ad_campaign_groups", "ad_campaigns", "creatives")


@pytest.mark.parametrize("surface", CURSOR_SURFACES)
@pytest.mark.parametrize("metadata", [{"nextPageToken": None}, {}], ids=["null", "absent"])
def test_cursor_contract_admits_a_terminal_page(surface: str, metadata: dict[str, Any]) -> None:
    body = {"elements": [], "metadata": metadata}
    assert _schema_errors(body, _contract(surface)["response"]["body"], surface) == []


@pytest.mark.parametrize("surface", CURSOR_SURFACES)
def test_cursor_contract_rejects_a_non_string_page_token(surface: str) -> None:
    body = {"elements": [], "metadata": {"nextPageToken": 5}}
    errors = _schema_errors(body, _contract(surface)["response"]["body"], surface)
    assert errors == [f"{surface}.metadata.nextPageToken: int is not ['string', 'null']"]


def test_contract_check_rejects_an_undeclared_field() -> None:
    element = _sample("ad_account")
    element["notInTheContract"] = True
    errors = _schema_errors(element, _element_schema("ad_account"), "ad_account")
    assert errors == ["ad_account: field 'notInTheContract' is not defined by the contract"]


def test_contract_check_rejects_a_wrong_type_and_a_missing_required_field() -> None:
    element = _sample("ad_account")
    element["id"] = "123456"
    del element["name"]
    errors = _schema_errors(element, _element_schema("ad_account"), "ad_account")
    assert errors == [
        "ad_account: required field 'name' is missing",
        "ad_account.id: str is not ['integer']",
    ]


def test_every_exported_name_exists() -> None:
    missing = [name for name in parser.__all__ if not hasattr(parser, name)]
    assert missing == []


# ---------------------------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [123, "123", "urn:li:sponsoredAccount:123", 0])
def test_sponsored_account_urn_accepts_ids_and_urns(value: str | int) -> None:
    expected = "urn:li:sponsoredAccount:0" if value == 0 else "urn:li:sponsoredAccount:123"
    assert sponsored_account_urn(value) == expected


@pytest.mark.parametrize("value", [123, "123", "urn:li:sponsoredCampaign:123"])
def test_sponsored_campaign_urn_accepts_ids_and_urns(value: str | int) -> None:
    assert sponsored_campaign_urn(value) == "urn:li:sponsoredCampaign:123"


# Neither a str nor an int: the helpers raise their documented ValueError, never a TypeError,
# and never build a URN such as ``urn:li:sponsoredAccount:1.5``.
_NON_INTEGER_IDS: tuple[object, ...] = (
    1.5,
    5.0,
    Decimal("1.5"),
    Decimal("5"),
    None,
    object(),
    b"1",
)


@pytest.mark.parametrize(
    "value",
    [
        True,
        -1,
        "",
        "abc",
        "12a",
        "urn:li:sponsoredCampaign:123",
        "urn:li:sponsoredAccount:",
        *_NON_INTEGER_IDS,
    ],
)
def test_sponsored_account_urn_rejects_malformed_values(value: Any) -> None:
    with pytest.raises(ValueError, match=r"account_id must be a numeric id or a"):
        sponsored_account_urn(value)


@pytest.mark.parametrize(
    "value", [False, -5, "urn:li:sponsoredAccount:123", "x1", *_NON_INTEGER_IDS]
)
def test_sponsored_campaign_urn_rejects_malformed_values(value: Any) -> None:
    with pytest.raises(ValueError, match=r"campaign_id must be a numeric id or a"):
        sponsored_campaign_urn(value)


def test_urn_id_returns_the_numeric_tail() -> None:
    assert urn_id("urn:li:sponsoredAccount:506223315", PREFIX_ACCOUNT) == "506223315"
    assert urn_id(CREATIVE_URN, PREFIX_CREATIVE) == "990001101"
    assert urn_id(FORM_URN, PREFIX_LEAD_FORM) == "990002001"
    assert urn_id(GROUP_URN, PREFIX_CAMPAIGN_GROUP) == "990000201"
    assert urn_id(sponsored_campaign_urn(990000101), PREFIX_CAMPAIGN) == "990000101"


@pytest.mark.parametrize(
    ("urn", "prefix", "message"),
    [
        (CAMPAIGN_URN, PREFIX_ACCOUNT, r"URN does not start with"),
        (CAMPAIGN_URN, "", r"URN does not start with"),
        ("urn:li:sponsoredCampaign:", PREFIX_CAMPAIGN, r"is not numeric"),
        ("urn:li:sponsoredCampaign:12a", PREFIX_CAMPAIGN, r"is not numeric"),
        ("urn:li:sponsoredCampaignGroup:1", PREFIX_CAMPAIGN, r"URN does not start with"),
        ("urn:li:sponsoredCampaignGroup:1a", PREFIX_CAMPAIGN_GROUP, r"is not numeric"),
    ],
)
def test_urn_id_rejects_a_wrong_prefix_or_tail(urn: str, prefix: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        urn_id(urn, prefix)


# ---------------------------------------------------------------------------------------------
# Request lists, status maps and segment constants
# ---------------------------------------------------------------------------------------------


def test_search_status_lists_equal_the_contract_requests() -> None:
    assert _request_list("ad_campaigns", "values:") == CAMPAIGN_SEARCH_STATUSES
    assert _request_list("ad_campaign_groups", "values:") == CAMPAIGN_GROUP_SEARCH_STATUSES
    assert _request_list("creatives", "intendedStatuses=") == CREATIVE_INTENDED_STATUSES


def test_search_status_lists_hold_the_documented_values_in_order() -> None:
    assert CAMPAIGN_SEARCH_STATUSES == (
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "COMPLETED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "REMOVED",
    )
    # COMPLETED and the CANCELLED spelling are group statuses, never group search values.
    assert CAMPAIGN_GROUP_SEARCH_STATUSES == (
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "REMOVED",
    )
    assert CREATIVE_INTENDED_STATUSES == (
        "ACTIVE",
        "PAUSED",
        "DRAFT",
        "ARCHIVED",
        "CANCELED",
        "PENDING_DELETION",
        "REMOVED",
    )
    # Every searched status is mapped, so a searched entity is never rejected as unmapped.
    assert set(CAMPAIGN_SEARCH_STATUSES) <= set(CAMPAIGN_STATUS_MAP)
    assert set(CAMPAIGN_GROUP_SEARCH_STATUSES) <= set(CAMPAIGN_GROUP_STATUS_MAP)
    assert set(CREATIVE_INTENDED_STATUSES) <= set(CREATIVE_STATUS_MAP)


EXPECTED_CAMPAIGN_STATUS = {
    "ACTIVE": "active",
    "PAUSED": "paused",
    "DRAFT": "paused",
    "ARCHIVED": "ended",
    "COMPLETED": "ended",
    "CANCELED": "ended",
    "PENDING_DELETION": "ended",
    "REMOVED": "ended",
}
EXPECTED_GROUP_STATUS = {
    "ACTIVE": "active",
    "PAUSED": "paused",
    "DRAFT": "paused",
    "ARCHIVED": "ended",
    "CANCELED": "ended",
    "CANCELLED": "ended",
    "COMPLETED": "ended",
    "PENDING_DELETION": "ended",
    "REMOVED": "ended",
}
EXPECTED_CREATIVE_STATUS = {
    "ACTIVE": "active",
    "PAUSED": "paused",
    "DRAFT": "paused",
    "ARCHIVED": "ended",
    "CANCELED": "ended",
    "PENDING_DELETION": "ended",
    "REMOVED": "ended",
}


def test_status_maps_cover_exactly_the_contract_enums() -> None:
    assert dict(CAMPAIGN_STATUS_MAP) == EXPECTED_CAMPAIGN_STATUS
    assert dict(CAMPAIGN_GROUP_STATUS_MAP) == EXPECTED_GROUP_STATUS
    assert dict(CREATIVE_STATUS_MAP) == EXPECTED_CREATIVE_STATUS
    assert set(CAMPAIGN_STATUS_MAP) == set(_enum("ad_campaigns", "status"))
    assert set(CAMPAIGN_GROUP_STATUS_MAP) == set(_enum("ad_campaign_groups", "status"))
    assert set(CREATIVE_STATUS_MAP) == set(_enum("creatives", "intendedStatus"))


@pytest.mark.parametrize(
    "mapping", [CAMPAIGN_STATUS_MAP, CAMPAIGN_GROUP_STATUS_MAP, CREATIVE_STATUS_MAP, SEGMENT_FACETS]
)
def test_constant_maps_are_read_only(mapping: Mapping[str, str]) -> None:
    with pytest.raises(TypeError):
        cast(Any, mapping)["NEW_VALUE"] = "ended"


def test_segment_pivots_and_facets() -> None:
    assert SEGMENT_PIVOTS == ("MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY")
    assert dict(SEGMENT_FACETS) == {
        "MEMBER_JOB_FUNCTION": "job_function",
        "MEMBER_SENIORITY": "seniority",
        "MEMBER_INDUSTRY": "industry",
    }
    # The statistics contract's URL template names the pivot slot these values fill.
    assert (
        "pivots=List(CAMPAIGN,{member_pivot})"
        in _contract("ad_analytics_statistics")["request"]["url"]
    )


# ---------------------------------------------------------------------------------------------
# Ad account
# ---------------------------------------------------------------------------------------------


def test_parse_ad_account_documented_sample() -> None:
    assert parse_ad_account(_sample("ad_account")) == AdAccountRow(
        external_id="123456",
        urn="urn:li:sponsoredAccount:123456",
        name="Company A",
        currency="USD",
        raw_status="ACTIVE",
        test=False,
    )


def test_parse_ad_account_defaults_test_to_false_and_reads_true() -> None:
    absent = _sample("ad_account")
    del absent["test"]
    assert parse_ad_account(_conforming("ad_account", absent)).test is False
    present = _sample("ad_account")
    present["test"] = True
    assert parse_ad_account(_conforming("ad_account", present)).test is True


@pytest.mark.parametrize("value", ["123456", Decimal(123456), Decimal("123456.0")])
def test_parse_ad_account_accepts_integral_id_forms(value: object) -> None:
    # Off the contract's integer type on purpose: the parser also reads the digit-string and
    # integral-Decimal forms of an id that connectors/http.py can hand it.
    element = _sample("ad_account")
    element["id"] = value
    assert parse_ad_account(element).external_id == "123456"


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("id", None, "required field 'id' is missing"),
        ("id", "12a", "field 'id' must be an integer"),
        ("id", True, "field 'id' must be an integer, not a boolean"),
        ("id", -1, "field 'id' must not be negative"),
        ("id", Decimal("1.5"), "field 'id' must be an integer"),
        ("id", Decimal("NaN"), "field 'id' must be an integer"),
        ("id", 1.5, "field 'id' must be an integer"),
        ("name", 7, "field 'name' must be a string"),
        ("name", None, "required field 'name' is missing"),
        ("currency", "usd", "field 'currency' must be an ISO 4217 code"),
        ("currency", "US", "field 'currency' must be an ISO 4217 code"),
        ("currency", None, "required field 'currency' is missing"),
        ("status", 1, "field 'status' must be a string"),
        ("test", "false", "field 'test' must be a boolean"),
    ],
)
def test_parse_ad_account_rejects_malformed_fields(key: str, value: object, fragment: str) -> None:
    element = _sample("ad_account")
    element[key] = value
    _expect_defect(lambda: parse_ad_account(element), "ad_account", fragment)


@pytest.mark.parametrize("element", [[], "ad account", None, 123456])
def test_parse_ad_account_rejects_a_non_object(element: object) -> None:
    _expect_defect(lambda: parse_ad_account(element), "ad_account", "must be a JSON object")


# ---------------------------------------------------------------------------------------------
# Campaign groups
# ---------------------------------------------------------------------------------------------


def test_parse_campaign_group_documented_sample() -> None:
    first, second = _sample("ad_campaign_groups")["elements"]
    assert parse_campaign_group(first) == CampaignGroupRow(
        external_id="603407684",
        urn="urn:li:sponsoredCampaignGroup:603407684",
        account_urn="urn:li:sponsoredAccount:506223315",
        name="Refresh group",
        status="active",
        raw_status="ACTIVE",
        daily_budget=None,
        total_budget=None,
        run_start=datetime(2009, 2, 13, 23, 31, 30, 987000, tzinfo=UTC),
        run_end=datetime(2282, 12, 22, 20, 13, 30, 123000, tzinfo=UTC),
    )
    row = parse_campaign_group(second, account_currency="USD")
    assert row.external_id == "504293601"
    assert row.name == "New Campaign Group"
    assert row.run_start == datetime(2017, 4, 26, 17, 48, 17, tzinfo=UTC)
    assert row.run_end is None  # an open-ended schedule
    assert row.daily_budget is None
    assert row.total_budget is None


def _group(**fields: Any) -> dict[str, Any]:
    """The first documented group with contract-defined fields replaced."""
    element: dict[str, Any] = _sample_element("ad_campaign_groups")
    element.update(fields)
    return _conforming("ad_campaign_groups", element)


def test_parse_campaign_group_bare_daily_budget_takes_the_account_currency() -> None:
    row = parse_campaign_group(_group(dailyBudget="250.75"), account_currency="EUR")
    assert row.daily_budget == Money(amount=Decimal("250.75"), currency="EUR")
    assert str(row.daily_budget.amount) == "250.75"  # never rounded


def test_parse_campaign_group_bare_daily_budget_needs_the_account_currency() -> None:
    element = _group(dailyBudget="250.75")
    _expect_defect(
        lambda: parse_campaign_group(element),
        "ad_campaign_groups",
        "field 'dailyBudget' is a bare amount; the ad account currency must be supplied",
    )


def test_parse_campaign_group_total_budget_object() -> None:
    element = _group(totalBudget={"amount": "5000.125", "currencyCode": "USD"})
    for currency in (None, "USD"):
        row = parse_campaign_group(element, account_currency=currency)
        assert row.total_budget == Money(amount=Decimal("5000.125"), currency="USD")
        assert row.daily_budget is None


def test_parse_campaign_group_rejects_a_budget_in_another_currency() -> None:
    element = _group(totalBudget={"amount": "5000", "currencyCode": "USD"})
    _expect_defect(
        lambda: parse_campaign_group(element, account_currency="EUR"),
        "ad_campaign_groups",
        "field 'totalBudget' is not in the ad account currency",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("250", "250"),
        ("0.5", "0.5"),
        (".5", "0.5"),
        ("1e2", "1E+2"),
        ("+3.25", "3.25"),
        ("-0.0", "0.0"),
        # connectors/http.py decodes a JSON number as int or Decimal, never float.
        (250, "250"),
        (Decimal("19.91833"), "19.91833"),
    ],
)
def test_parse_campaign_group_decimal_forms(value: object, expected: str) -> None:
    element = _sample_element("ad_campaign_groups")
    element["dailyBudget"] = value
    amount = parse_campaign_group(element, account_currency="USD").daily_budget
    assert amount is not None
    assert str(amount.amount) == expected
    assert not amount.amount.is_signed()


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("dailyBudget", "-1", "field 'dailyBudget' must not be negative"),
        ("dailyBudget", "abc", "field 'dailyBudget' must be a decimal string"),
        ("dailyBudget", "1,5", "field 'dailyBudget' must be a decimal string"),
        ("dailyBudget", "NaN", "field 'dailyBudget' must be a decimal string"),
        ("dailyBudget", 2.5, "field 'dailyBudget' must be a decimal string"),
        ("dailyBudget", True, "field 'dailyBudget' must be a decimal, not a boolean"),
        ("dailyBudget", Decimal("Infinity"), "field 'dailyBudget' must be finite"),
        ("dailyBudget", Decimal("-2"), "field 'dailyBudget' must not be negative"),
        ("totalBudget", "5000", "field 'totalBudget' must be an amount and currency object"),
        ("totalBudget", {"currencyCode": "USD"}, "required field 'amount' is missing"),
        ("totalBudget", {"amount": "5"}, "required field 'currencyCode' is missing"),
        (
            "totalBudget",
            {"amount": "5", "currencyCode": "usd"},
            "field 'totalBudget.currencyCode' must be an ISO 4217 code",
        ),
        ("totalBudget", {"amount": "x", "currencyCode": "USD"}, "field 'amount' must be a"),
        ("runSchedule", {"end": 1493228897000}, "required field 'start' is missing"),
        ("runSchedule", [1493228897000], "field 'runSchedule' must be a JSON object"),
        ("runSchedule", {"start": "soon"}, "field 'start' must be an integer"),
        (
            "runSchedule",
            {"start": 2**63 - 1},
            "field 'start' is outside the representable date range",
        ),
        (
            "runSchedule",
            {"start": 1493228897000, "end": 10**16},
            "field 'end' is outside the representable date range",
        ),
        ("account", "urn:li:sponsoredAccount:abc", "field 'account' must be a"),
        ("account", "urn:li:organization:5509810", "field 'account' must be a"),
        ("account", None, "required field 'account' is missing"),
        ("status", None, "required field 'status' is missing"),
        ("status", "SUSPENDED", "unmapped status 'SUSPENDED' in field 'status'"),
        ("id", None, "required field 'id' is missing"),
    ],
)
def test_parse_campaign_group_rejects_malformed_fields(
    key: str, value: object, fragment: str
) -> None:
    element = _sample_element("ad_campaign_groups")
    element[key] = value
    _expect_defect(
        lambda: parse_campaign_group(element, account_currency="USD"),
        "ad_campaign_groups",
        fragment,
    )


@pytest.mark.parametrize("status", _enum("ad_campaign_groups", "status"))
def test_parse_campaign_group_normalises_every_contract_status(status: str) -> None:
    row = parse_campaign_group(_group(status=status))
    assert row.raw_status == status
    assert row.status == EXPECTED_GROUP_STATUS[status]


def test_cancelled_and_completed_groups_have_ended() -> None:
    assert parse_campaign_group(_group(status="CANCELLED")).status == "ended"
    assert parse_campaign_group(_group(status="COMPLETED")).status == "ended"


@pytest.mark.parametrize("currency", ["usd", "US", "EURO", ""])
def test_parse_campaign_group_rejects_a_malformed_account_currency(currency: str) -> None:
    with pytest.raises(ValueError, match=r"account_currency must be a three-letter"):
        parse_campaign_group(_sample_element("ad_campaign_groups"), account_currency=currency)


# ---------------------------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------------------------


def test_parse_campaign_documented_sample() -> None:
    expected = CampaignRow(
        external_id="145282384",
        urn="urn:li:sponsoredCampaign:145282384",
        account_urn="urn:li:sponsoredAccount:506333826",
        name="Campaign Sponsored update B",
        status="active",
        raw_status="ACTIVE",
        campaign_group_urn="urn:li:sponsoredCampaignGroup:603030884",
        daily_budget=Money(amount=Decimal(18), currency="USD"),
        total_budget=None,
        objective_type=None,
        run_start=datetime(2009, 2, 13, 23, 31, 30, 987000, tzinfo=UTC),
        run_end=datetime(2282, 12, 22, 20, 13, 30, 123000, tzinfo=UTC),
    )
    assert parse_campaign(_sample_element("ad_campaigns")) == expected
    assert parse_campaign(_sample_element("ad_campaigns"), account_currency="USD") == expected


def _campaign(**fields: Any) -> dict[str, Any]:
    """The documented campaign with contract-defined fields replaced."""
    element: dict[str, Any] = _sample_element("ad_campaigns")
    element.update(fields)
    return _conforming("ad_campaigns", element)


def test_parse_campaign_lifetime_budget_objective_and_no_schedule() -> None:
    element = _campaign(
        id=990000101,
        account=ACCOUNT_URN,
        campaignGroup=GROUP_URN,
        status="PAUSED",
        objectiveType="LEAD_GENERATION",
        totalBudget={"amount": "1200.50", "currencyCode": "USD"},
    )
    del element["dailyBudget"]
    del element["runSchedule"]
    row = parse_campaign(_conforming("ad_campaigns", element), account_currency="USD")
    assert row.urn == CAMPAIGN_URN
    assert row.account_urn == ACCOUNT_URN
    assert row.campaign_group_urn == GROUP_URN
    assert row.status == "paused"
    assert row.objective_type == "LEAD_GENERATION"
    assert row.daily_budget is None  # absent, never invented
    assert row.total_budget == Money(amount=Decimal("1200.50"), currency="USD")
    assert row.run_start is None
    assert row.run_end is None


@pytest.mark.parametrize("status", _enum("ad_campaigns", "status"))
def test_parse_campaign_normalises_every_contract_status(status: str) -> None:
    row = parse_campaign(_campaign(status=status))
    assert row.raw_status == status
    assert row.status == EXPECTED_CAMPAIGN_STATUS[status]


def test_parse_campaign_rejects_a_budget_in_another_currency() -> None:
    _expect_defect(
        lambda: parse_campaign(_sample_element("ad_campaigns"), account_currency="GBP"),
        "ad_campaigns",
        "field 'dailyBudget' is not in the ad account currency",
    )


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("dailyBudget", "18", "field 'dailyBudget' must be an amount and currency object"),
        ("campaignGroup", "urn:li:sponsoredCampaign:1", "field 'campaignGroup' must be a"),
        ("campaignGroup", 603030884, "field 'campaignGroup' must be a string"),
        ("objectiveType", ["LEAD_GENERATION"], "field 'objectiveType' must be a string"),
        ("status", "CANCELLED", "unmapped status 'CANCELLED' in field 'status'"),
        ("name", None, "required field 'name' is missing"),
    ],
)
def test_parse_campaign_rejects_malformed_fields(key: str, value: object, fragment: str) -> None:
    element = _sample_element("ad_campaigns")
    element[key] = value
    _expect_defect(lambda: parse_campaign(element), "ad_campaigns", fragment)


def test_unmapped_free_text_status_is_never_echoed() -> None:
    element = _sample_element("ad_campaigns")
    element["status"] = SAMPLE_ANSWER
    error = _expect_defect(
        lambda: parse_campaign(element), "ad_campaigns", "unmapped status in field 'status'"
    )
    assert SAMPLE_ANSWER not in str(error)
    assert SAMPLE_ANSWER not in error.reason


def test_account_currency_and_campaign_urn_are_keyword_only() -> None:
    group: Any = parse_campaign_group
    campaign: Any = parse_campaign
    segment: Any = parse_segment
    with pytest.raises(TypeError):
        group(_sample_element("ad_campaign_groups"), "USD")
    with pytest.raises(TypeError):
        campaign(_sample_element("ad_campaigns"), "USD")
    with pytest.raises(TypeError):
        segment(_segment(["urn:li:seniority:7"]), "MEMBER_SENIORITY", SAMPLE_SEGMENT_CAMPAIGN)


def test_parse_campaign_rejects_a_non_object() -> None:
    _expect_defect(lambda: parse_campaign([]), "ad_campaigns", "a campaign must be a JSON object")


def test_parse_campaign_rejects_a_malformed_account_currency() -> None:
    with pytest.raises(ValueError, match=r"ISO 4217"):
        parse_campaign(_sample_element("ad_campaigns"), account_currency="dollars")


# ---------------------------------------------------------------------------------------------
# Creatives
# ---------------------------------------------------------------------------------------------


def test_parse_creative_documented_sample() -> None:
    assert parse_creative(_sample_element("creatives")) == CreativeRow(
        urn="urn:li:sponsoredCreative:119962155",
        campaign_urn="urn:li:sponsoredCampaign:360035215",
        account_urn="urn:li:sponsoredAccount:520866471",
        name=None,
        intended_status="DRAFT",
        status="paused",
        is_serving=False,
        post_urn="urn:li:share:6778045555198214144",
        lead_form_urn=None,
        cta_label=None,
    )


def _creative(**fields: Any) -> dict[str, Any]:
    """The documented creative with contract-defined fields replaced."""
    element: dict[str, Any] = _sample_element("creatives")
    element.update(fields)
    return _conforming("creatives", element)


@pytest.mark.parametrize(
    "destination",
    [
        "urn:li:adForm:990002001",
        "urn:li:leadGenForm:990002001",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990002001,3)",
    ],
)
def test_parse_creative_lead_gen_call_to_action(destination: str) -> None:
    element = _creative(
        id=CREATIVE_URN,
        campaign=CAMPAIGN_URN,
        account=ACCOUNT_URN,
        name="Fake white paper creative",
        intendedStatus="ACTIVE",
        isServing=True,
        content={"reference": "urn:li:ugcPost:990003001"},
        leadgenCallToAction={"destination": destination, "label": "DOWNLOAD"},
    )
    assert parse_creative(element) == CreativeRow(
        urn=CREATIVE_URN,
        campaign_urn=CAMPAIGN_URN,
        account_urn=ACCOUNT_URN,
        name="Fake white paper creative",
        intended_status="ACTIVE",
        status="active",
        is_serving=True,
        post_urn="urn:li:ugcPost:990003001",
        lead_form_urn=FORM_URN,
        cta_label="DOWNLOAD",
    )


def test_parse_creative_without_a_post_account_or_serving_flag() -> None:
    element = _creative(content={"reference": "urn:li:adInMailContent:990003002"})
    del element["account"]
    del element["isServing"]
    row = parse_creative(_conforming("creatives", element))
    assert row.post_urn is None  # message ads reference no post
    assert row.account_urn is None
    assert row.is_serving is None
    no_reference = parse_creative(_creative(content={}))
    assert no_reference.post_urn is None
    no_content = _sample_element("creatives")
    del no_content["content"]
    assert parse_creative(_conforming("creatives", no_content)).post_urn is None


def test_parse_creative_accepts_a_bare_numeric_id() -> None:
    assert parse_creative(_creative(id="990001101")).urn == CREATIVE_URN


@pytest.mark.parametrize("status", _enum("creatives", "intendedStatus"))
def test_parse_creative_normalises_every_contract_intended_status(status: str) -> None:
    row = parse_creative(_creative(intendedStatus=status))
    assert row.intended_status == status
    assert row.status == EXPECTED_CREATIVE_STATUS[status]


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("id", "urn:li:sponsoredCreative:abc", "field 'id' must be a urn:li:sponsoredCreative:"),
        ("id", "creative-1", "field 'id' must be an integer"),
        ("id", None, "required field 'id' is missing"),
        ("campaign", "urn:li:sponsoredCampaignGroup:1", "field 'campaign' must be a"),
        ("campaign", None, "required field 'campaign' is missing"),
        ("intendedStatus", None, "required field 'intendedStatus' is missing"),
        ("intendedStatus", "COMPLETED", "unmapped status 'COMPLETED' in field 'intendedStatus'"),
        ("isServing", "yes", "field 'isServing' must be a boolean"),
        ("content", "urn:li:share:1", "field 'content' must be a JSON object"),
        ("content", {"reference": 1}, "field 'reference' must be a string"),
        ("leadgenCallToAction", {"label": "DOWNLOAD"}, "required field 'destination'"),
        (
            "leadgenCallToAction",
            {"destination": "urn:li:adForm:990002001"},
            "required field 'label' is missing",
        ),
    ],
)
def test_parse_creative_rejects_malformed_fields(key: str, value: object, fragment: str) -> None:
    element = _sample_element("creatives")
    element[key] = value
    _expect_defect(lambda: parse_creative(element), "creatives", fragment)


def test_parse_creative_rejects_a_malformed_destination() -> None:
    element = _sample_element("creatives")
    element["leadgenCallToAction"] = {"destination": "urn:li:adForm:abc", "label": "DOWNLOAD"}
    _expect_defect(
        lambda: parse_creative(element),
        "creatives",
        "a lead-form reference is not a leadGenForm, adForm or versioned URN",
    )


@pytest.mark.parametrize(
    ("reference", "prefix"),
    [
        ("urn:li:share:not-a-number", "urn:li:share:"),
        ("urn:li:share:", "urn:li:share:"),
        ("urn:li:share:12 ", "urn:li:share:"),
        ("urn:li:ugcPost:12a", "urn:li:ugcPost:"),
        ("urn:li:ugcPost:-1", "urn:li:ugcPost:"),
    ],
)
def test_parse_creative_rejects_a_post_reference_with_a_malformed_id(
    reference: str, prefix: str
) -> None:
    element = _creative(content={"reference": reference})
    error = _expect_defect(
        lambda: parse_creative(element), "creatives", "field 'content.reference' must be a"
    )
    # The message names the field and the expected shape only, never the malformed value.
    assert error.message == f"creatives: field 'content.reference' must be a {prefix}{{id}} URN"


# ---------------------------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------------------------


def test_parse_post_documented_sample() -> None:
    results = _sample("posts")["results"]
    assert [parse_post(urn, element) for urn, element in results.items()] == [
        PostRow(
            urn="urn:li:share:6844785523593134080",
            headline=None,  # the sample's content is empty
            intro_text="Test Org commentary-Updated Config",
        )
    ]


def _post(**fields: Any) -> dict[str, Any]:
    """The documented post with contract-defined fields replaced."""
    element: dict[str, Any] = _sample("posts")["results"]["urn:li:share:6844785523593134080"]
    element.update(fields)
    return _conforming("posts", element)


def test_parse_post_headline_from_media_title() -> None:
    element = _post(
        content={"media": {"id": "urn:li:image:990004001", "title": "  Fake headline  "}},
        commentary="  Fake intro text\n",
    )
    row = parse_post("urn:li:ugcPost:990003001", element)
    assert row == PostRow(
        urn="urn:li:ugcPost:990003001", headline="Fake headline", intro_text="Fake intro text"
    )


def test_parse_post_headline_from_article_title() -> None:
    element = _post(
        content={"article": {"source": "https://example.com/white-paper", "title": "Fake paper"}}
    )
    assert parse_post("urn:li:share:990003003", element).headline == "Fake paper"


def test_parse_post_prefers_the_media_title_and_skips_a_blank_one() -> None:
    both = _post(content={"media": {"title": "Media title"}, "article": {"title": "Article"}})
    assert parse_post("urn:li:share:1", both).headline == "Media title"
    blank_media = _post(content={"media": {"title": "   "}, "article": {"title": "Article"}})
    assert parse_post("urn:li:share:1", blank_media).headline == "Article"
    untitled = _post(content={"media": {"id": "urn:li:image:990004001"}})
    assert parse_post("urn:li:share:1", untitled).headline is None


def test_parse_post_blank_or_absent_commentary_is_none() -> None:
    assert parse_post("urn:li:share:1", _post(commentary=" \t ")).intro_text is None
    element = _post()
    del element["commentary"]
    assert parse_post("urn:li:share:1", _conforming("posts", element)).intro_text is None


@pytest.mark.parametrize(
    "urn",
    [
        "urn:li:sponsoredCreative:1",
        "urn:li:share:",
        "urn:li:share:abc",
        "6844785523593134080",
        "urn:li:ugcPost:12a",
        6844785523593134080,
        None,
    ],
)
def test_parse_post_rejects_a_key_that_is_not_a_post_urn(urn: object) -> None:
    error = _expect_defect(
        lambda: parse_post(urn, _post()), "posts", "the batch-result key must be a urn:li:share:"
    )
    # The key is LinkedIn's data, so the message states the expected shape and never the key.
    assert error.message == (
        "posts: the batch-result key must be a urn:li:share:{id} or urn:li:ugcPost:{id} URN"
    )


@pytest.mark.parametrize(
    ("element", "fragment"),
    [
        ([], "a post must be a JSON object"),
        ({"content": "media"}, "field 'content' must be a JSON object"),
        ({"content": {"media": ["x"]}}, "field 'media' must be a JSON object"),
        ({"content": {"article": {"title": 1}}}, "field 'title' must be a string"),
        ({"commentary": 5}, "field 'commentary' must be a string"),
    ],
)
def test_parse_post_rejects_malformed_fields(element: object, fragment: str) -> None:
    _expect_defect(lambda: parse_post("urn:li:share:1", element), "posts", fragment)


# ---------------------------------------------------------------------------------------------
# Daily creative analytics (LinkedIn days are UTC)
# ---------------------------------------------------------------------------------------------


def _daily(index: int = 0, **fields: Any) -> dict[str, Any]:
    """A documented analytics element with its pivot value replaced by a creative URN.

    The parser docstring names this substitution: the sample pivots by MEMBER_COMPANY.
    """
    element: dict[str, Any] = _sample_element("ad_analytics", index)
    element["pivotValues"] = [CREATIVE_URN]
    element.update(fields)
    return _conforming("ad_analytics", element)


def test_documented_analytics_sample_verbatim_is_not_a_creative_pivot() -> None:
    for element in _sample("ad_analytics")["elements"]:
        _expect_defect(
            lambda element=element: parse_analytics_daily(element),
            "ad_analytics",
            "field 'pivotValues' must be a urn:li:sponsoredCreative:{id} URN",
        )


def test_parse_analytics_daily_documented_sample_with_a_creative_pivot() -> None:
    first = parse_analytics_daily(_daily(0))
    assert first == CreativeDailyRow(
        creative_urn=CREATIVE_URN,
        date=date(2024, 5, 28),
        cost=Decimal("0.0"),
        impressions=6,
        clicks=None,
        landing_page_clicks=0,
        form_opens=None,
        leads=None,
        website_conversions=0,
    )
    second = parse_analytics_daily(_daily(1))
    assert second.date == date(2024, 5, 29)
    assert second.cost == Decimal("19.91833")
    assert str(second.cost) == "19.91833"  # exact, never rounded
    assert second.impressions == 165
    assert second.landing_page_clicks == 11


def test_parse_analytics_daily_types_and_absent_metrics() -> None:
    row = parse_analytics_daily(_daily(0))
    assert isinstance(row.cost, Decimal)
    assert type(row.impressions) is int
    assert type(row.date) is date  # a UTC calendar day, not an instant
    # Requested metrics the element omits are unavailable, never zero.
    assert row.clicks is None
    assert row.form_opens is None
    assert row.leads is None
    # A returned zero stays a zero.
    assert row.landing_page_clicks == 0
    assert row.website_conversions == 0


def test_parse_analytics_daily_every_requested_metric() -> None:
    element = _daily(
        1,
        clicks=14,
        oneClickLeadFormOpens=9,
        oneClickLeads=4,
        externalWebsiteConversions=2,
        costInLocalCurrency="31.07",
    )
    row = parse_analytics_daily(element)
    assert row == CreativeDailyRow(
        creative_urn=CREATIVE_URN,
        date=date(2024, 5, 29),
        cost=Decimal("31.07"),
        impressions=165,
        clicks=14,
        landing_page_clicks=11,
        form_opens=9,
        leads=4,
        website_conversions=2,
    )


def test_parse_analytics_daily_without_a_date_range_end() -> None:
    element = _daily(0)
    del element["dateRange"]["end"]
    assert parse_analytics_daily(_conforming("ad_analytics", element)).date == date(2024, 5, 28)


def test_parse_analytics_daily_accepts_integral_decimal_counts() -> None:
    # Off the contract's integer type on purpose: connectors/http.py decodes a JSON number
    # written as 6.0 to Decimal("6.0"), which still names a whole count.
    element = _daily(0)
    element["impressions"] = Decimal("6.0")
    row = parse_analytics_daily(element)
    assert row.impressions == 6
    assert type(row.impressions) is int


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"pivotValues": []}, "field 'pivotValues' must hold exactly one creative URN"),
        (
            {"pivotValues": [CREATIVE_URN, OTHER_CREATIVE_URN]},
            "field 'pivotValues' must hold exactly one creative URN",
        ),
        ({"pivotValues": [990001101]}, "field 'pivotValues' must hold strings"),
        ({"pivotValues": CREATIVE_URN}, "field 'pivotValues' must be a JSON array"),
        ({"pivotValues": None}, "required field 'pivotValues' is missing"),
        ({"dateRange": None}, "required field 'dateRange' is missing"),
        ({"dateRange": {}}, "required field 'start' is missing"),
        (
            {"dateRange": {"start": {"year": 2024, "month": 5}}},
            "required field 'day' is missing",
        ),
        (
            {"dateRange": {"start": {"year": 2024, "month": 2, "day": 30}}},
            "field 'start' is not a valid calendar date",
        ),
        (
            {"dateRange": {"start": {"year": 2024, "month": 13, "day": 1}}},
            "field 'start' is not a valid calendar date",
        ),
        (
            {
                "dateRange": {
                    "start": {"year": 2024, "month": 5, "day": 28},
                    "end": {"year": 2024, "month": 5, "day": 29},
                }
            },
            "a DAILY element must have dateRange.end equal to its start",
        ),
        (
            {
                "dateRange": {
                    "start": {"year": 2024, "month": 5, "day": 28},
                    "end": {"year": 2024, "month": 0, "day": 1},
                }
            },
            "field 'end' is not a valid calendar date",
        ),
        ({"impressions": -6}, "field 'impressions' must not be negative"),
        ({"impressions": "six"}, "field 'impressions' must be an integer"),
        ({"clicks": Decimal("1.5")}, "field 'clicks' must be an integer"),
        ({"oneClickLeads": False}, "field 'oneClickLeads' must be an integer, not a boolean"),
        ({"costInLocalCurrency": "-0.01"}, "field 'costInLocalCurrency' must not be negative"),
        ({"costInLocalCurrency": "1.0.0"}, "field 'costInLocalCurrency' must be a decimal"),
    ],
)
def test_parse_analytics_daily_rejects_malformed_fields(
    fields: dict[str, Any], fragment: str
) -> None:
    element: dict[str, Any] = _sample_element("ad_analytics")
    element["pivotValues"] = [CREATIVE_URN]
    element.update(fields)
    _expect_defect(lambda: parse_analytics_daily(element), "ad_analytics", fragment)


def test_parse_analytics_daily_rejects_a_non_object() -> None:
    _expect_defect(
        lambda: parse_analytics_daily("row"),
        "ad_analytics",
        "an analytics element must be a JSON object",
    )


# ---------------------------------------------------------------------------------------------
# Numeric bounds: an oversized numeral is a validation defect, never a raw conversion error
# ---------------------------------------------------------------------------------------------
#
# Rest.li ids, counts, epoch milliseconds, date parts and form versions are 64-bit longs, and
# amounts are stored as numeric(18, 6): below 10**12, with no nonzero digit beyond the sixth
# decimal place. Without the bounds, int() raises ValueError past 4,300 digits, Decimal()
# raises InvalidOperation on an exponent beyond its limits, date() raises OverflowError on a
# year beyond a C int, and storage overflows on a larger amount or rounds a finer one. The
# values below are deliberately malformed inputs, apart from the boundary values accepted.

INT64_MAX = 2**63 - 1
OUT_OF_INT64 = "is out of the 64-bit integer range"


def _assert_not_echoed(error: QueryDefect, *raw: str) -> None:
    """Assert no text of ``error`` carries any of the ``raw`` numeral fragments."""
    for text in (str(error), error.message, error.reason, repr(error.args)):
        for fragment in raw:
            assert fragment not in text, text


@pytest.mark.parametrize(
    "value",
    [
        "9" * 5000,  # past int()'s 4,300-digit string limit
        "9" * 20,
        str(2**63),
        2**63,
        2**70,
        Decimal(2**63),
        Decimal("1E+30"),
        Decimal("-1E+30"),
        Decimal("1E+999999999"),  # int() would build a billion-digit integer
    ],
)
def test_parse_ad_account_rejects_an_id_beyond_64_bits(value: object) -> None:
    element = _sample("ad_account")
    element["id"] = value
    error = _expect_defect(
        lambda: parse_ad_account(element), "ad_account", f"field 'id' {OUT_OF_INT64}"
    )
    _assert_not_echoed(error, str(value)[:8])


@pytest.mark.parametrize("value", [INT64_MAX, str(INT64_MAX), Decimal(INT64_MAX)])
def test_parse_ad_account_accepts_the_largest_64_bit_id(value: object) -> None:
    element = _sample("ad_account")
    element["id"] = value
    row = parse_ad_account(element)
    assert row.external_id == str(INT64_MAX)
    assert row.urn == PREFIX_ACCOUNT + str(INT64_MAX)


@pytest.mark.parametrize("value", [2**63, "9" * 5000, Decimal("1E+30")])
def test_parse_analytics_daily_rejects_a_count_beyond_64_bits(value: object) -> None:
    element = _daily(0)
    element["impressions"] = value
    error = _expect_defect(
        lambda: parse_analytics_daily(element),
        "ad_analytics",
        f"field 'impressions' {OUT_OF_INT64}",
    )
    _assert_not_echoed(error, str(value)[:8])


def test_epoch_milliseconds_beyond_64_bits_are_a_defect() -> None:
    group = _sample_element("ad_campaign_groups")
    group["runSchedule"] = {"start": 2**63}
    _expect_defect(
        lambda: parse_campaign_group(group, account_currency="USD"),
        "ad_campaign_groups",
        f"field 'start' {OUT_OF_INT64}",
    )
    lead = _mutate(("submittedAt",), "9" * 5000)
    error = _expect_defect(
        lambda: parse_lead(lead, {}), "lead_form_responses", f"field 'submittedAt' {OUT_OF_INT64}"
    )
    _assert_not_echoed(error, "9999")


@pytest.mark.parametrize(
    ("date_range", "fragment", "raw"),
    [
        (
            {"start": {"year": 10**12, "month": 5, "day": 28}},
            "field 'start' is not a valid calendar date",
            "10000000",
        ),
        (
            {"start": {"year": 2**62, "month": 5, "day": 28}},
            "field 'start' is not a valid calendar date",
            "46116860",
        ),
        (
            {"start": {"year": INT64_MAX, "month": 5, "day": 28}},
            "field 'start' is not a valid calendar date",
            "92233720",
        ),
        (
            {"start": {"year": 2024, "month": 2**62, "day": 28}},
            "field 'start' is not a valid calendar date",
            "46116860",
        ),
        (
            {"start": {"year": 2024, "month": 5, "day": 2**62}},
            "field 'start' is not a valid calendar date",
            "46116860",
        ),
        (
            {
                "start": {"year": 2024, "month": 5, "day": 28},
                "end": {"year": 10**12, "month": 5, "day": 28},
            },
            "field 'end' is not a valid calendar date",
            "10000000",
        ),
        (
            {"start": {"year": 2**63, "month": 5, "day": 28}},
            f"field 'year' {OUT_OF_INT64}",
            "92233720",
        ),
        (
            {"start": {"year": "9" * 5000, "month": 5, "day": 28}},
            f"field 'year' {OUT_OF_INT64}",
            "9999",
        ),
    ],
)
def test_parse_analytics_daily_rejects_a_date_part_beyond_the_calendar(
    date_range: dict[str, Any], fragment: str, raw: str
) -> None:
    element = _daily(0)
    element["dateRange"] = date_range
    error = _expect_defect(lambda: parse_analytics_daily(element), "ad_analytics", fragment)
    _assert_not_echoed(error, raw)


@pytest.mark.parametrize(
    ("value", "fragment", "raw"),
    [
        ("1e" + "9" * 25, "is out of range", "9999"),  # Decimal() raises InvalidOperation
        ("1e-" + "9" * 25, "is out of range", "9999"),
        ("1" + "0" * 64, "is longer than 64 characters", "0000"),
        ("0." + "0" * 4997 + "1", "is longer than 64 characters", "0000"),
        ("9" * 5000, "is longer than 64 characters", "9999"),
        ("1e30", "is out of range", "1e30"),
        ("1E+19", "is out of range", "1E+19"),
        (str(10**19), "is out of range", "10000000"),
        (Decimal("1E+30"), "is out of range", "1E+30"),
        (Decimal("1E+999999999999999999"), "is out of range", "1E+99999"),
        (10**19, "is out of range", "10000000"),
        (2**70, "is out of range", "11805916"),
    ],
)
def test_parse_analytics_daily_rejects_a_cost_beyond_the_decimal_bounds(
    value: object, fragment: str, raw: str
) -> None:
    element = _daily(1)
    element["costInLocalCurrency"] = value
    error = _expect_defect(
        lambda: parse_analytics_daily(element),
        "ad_analytics",
        f"field 'costInLocalCurrency' {fragment}",
    )
    _assert_not_echoed(error, raw)


def _analytics_cost(value: object) -> dict[str, Any]:
    """The second documented analytics element, ``costInLocalCurrency`` set to ``value``."""
    element = _daily(1)
    element["costInLocalCurrency"] = value
    return element


def _segment_cost(value: object) -> dict[str, Any]:
    """The documented statistics element, ``costInLocalCurrency`` set to ``value``."""
    element = _segment([SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"])
    element["costInLocalCurrency"] = value
    return element


def _group_daily_budget(value: object) -> dict[str, Any]:
    """The first documented campaign group with the bare ``dailyBudget`` set to ``value``."""
    element: dict[str, Any] = _sample_element("ad_campaign_groups")
    element["dailyBudget"] = value
    return element


def _group_total_budget(value: object) -> dict[str, Any]:
    """The first documented campaign group with ``totalBudget.amount`` set to ``value``."""
    element: dict[str, Any] = _sample_element("ad_campaign_groups")
    element["totalBudget"] = {"amount": value, "currencyCode": "USD"}
    return element


def _campaign_daily_budget(value: object) -> dict[str, Any]:
    """The documented campaign with ``dailyBudget.amount`` set to ``value``."""
    element: dict[str, Any] = _sample_element("ad_campaigns")
    element["dailyBudget"]["amount"] = value
    return element


def _campaign_total_budget(value: object) -> dict[str, Any]:
    """The documented campaign with a lifetime budget of ``value`` in place of its daily one."""
    element: dict[str, Any] = _sample_element("ad_campaigns")
    del element["dailyBudget"]
    element["totalBudget"] = {"amount": value, "currencyCode": "USD"}
    return element


def _amount_of(budget: Money | None) -> Decimal | None:
    """The amount of a parsed budget, or ``None`` when the budget is absent."""
    return None if budget is None else budget.amount


# Every amount the parser reads is stored as numeric(18, 6): (surface, the field a defect names,
# a documented element carrying the amount, the parsed amount). Reported spend is rounded
# half-even to six decimal places; budgets are exact, so a finer digit there is a defect.
COST_SITES = [
    pytest.param(
        "ad_analytics",
        "costInLocalCurrency",
        _analytics_cost,
        lambda element: parse_analytics_daily(element).cost,
        id="analytics-cost",
    ),
    pytest.param(
        "ad_analytics_statistics",
        "costInLocalCurrency",
        _segment_cost,
        lambda element: parse_segment(element, "MEMBER_SENIORITY").cost,
        id="segment-cost",
    ),
]
BUDGET_SITES = [
    pytest.param(
        "ad_campaign_groups",
        "dailyBudget",
        _group_daily_budget,
        lambda element: _amount_of(
            parse_campaign_group(element, account_currency="USD").daily_budget
        ),
        id="group-daily-budget",
    ),
    pytest.param(
        "ad_campaign_groups",
        "amount",
        _group_total_budget,
        lambda element: _amount_of(
            parse_campaign_group(element, account_currency="USD").total_budget
        ),
        id="group-total-budget",
    ),
    pytest.param(
        "ad_campaigns",
        "amount",
        _campaign_daily_budget,
        lambda element: _amount_of(parse_campaign(element, account_currency="USD").daily_budget),
        id="campaign-daily-budget",
    ),
    pytest.param(
        "ad_campaigns",
        "amount",
        _campaign_total_budget,
        lambda element: _amount_of(parse_campaign(element, account_currency="USD").total_budget),
        id="campaign-total-budget",
    ),
]
AMOUNT_SITES = [*COST_SITES, *BUDGET_SITES]
AMOUNT_SITE_ARGS = ("surface", "field_name", "place", "read")

OUT_OF_RANGE = "is out of range"
TOO_PRECISE = "has more than 6 decimal places"

# Amounts of 10**12 or more, which overflow numeric(18, 6) at every amount field, each with a
# fragment no message may echo.
OUT_OF_RANGE_AMOUNTS: list[tuple[str | int | Decimal, str]] = [
    ("1000000000000", "1000000000000"),  # 10**12, thirteen integral digits
    ("1000000000000.000000", "1000000000000"),
    ("1000000000000.0000001", "1000000000000"),  # out of range before any rounding
    ("1e12", "1e12"),
    (Decimal("1E+12"), "1E+12"),
    (10**12, "1000000000000"),
    ("9999999999999999999.99", "99999999"),  # just below the old 10**19 bound
    (Decimal("9.999E+18"), "9.999E+18"),
    (10**19 - 1, "99999999"),
]

# Amounts below 10**12 with a nonzero digit beyond the sixth decimal place, each with the cost
# it rounds to half-even and a fragment no budget defect may echo.
EXCESS_PLACE_AMOUNTS: list[tuple[str | Decimal, str, str]] = [
    ("0.0000001", "0.000000", "0000001"),
    ("0.00000010", "0.000000", "0000001"),
    ("1e-7", "0.000000", "1e-7"),
    (Decimal("1E-7"), "0.000000", "1E-7"),
    ("12.3456789", "12.345679", "3456789"),
    ("0." + "1" * 62, "0.111111", "1111111"),  # exactly 64 characters
    # 64 characters with a 1 in the 62nd place: rounding to the 28-digit context would hide it.
    ("1." + "0" * 61 + "1", "1.000000", "0000001"),
    # Live 202609 float residue: costInLocalCurrency carries up to 17 decimal places.
    ("19.91833000000000123", "19.918330", "91833"),
    ("19.918330000000001", "19.918330", "91833"),
    (Decimal("19.91833000000000123"), "19.918330", "91833"),
    ("1.991833000000000123E+1", "19.918330", "991833"),
    ("0.00000000000000355", "0.000000", "355"),  # residue of a zero cost: an unsigned zero
    # Ties round to the even sixth digit; anything above a tie rounds up.
    ("0.0000005", "0.000000", "0000005"),
    ("0.0000015", "0.000002", "0000015"),
    ("0.0000025", "0.000002", "0000025"),
    ("0.00000050000000000000001", "0.000001", "00000050"),
    ("999999999999.9999994", "999999999999.999999", "9999994"),  # the largest still in range
]

# Amounts below 10**12 that round half-even to 10**12, which numeric(18, 6) cannot hold.
ROUNDING_OUT_OF_RANGE_AMOUNTS: list[tuple[str | Decimal, str]] = [
    ("999999999999.9999995", "9999995"),  # a tie, and the sixth digit 9 is odd: rounds up
    ("999999999999.9999999", "9999999"),
    (Decimal("999999999999.99999950"), "99999950"),
]

# Amounts numeric(18, 6) holds exactly, at both bounds, each with Decimal's string form of it.
# Trailing zeros beyond the sixth place change no value, so they are accepted.
STORABLE_AMOUNTS: list[tuple[str | int | Decimal, str]] = [
    ("999999999999.999999", "999999999999.999999"),  # the largest storable amount
    (Decimal("999999999999.999999"), "999999999999.999999"),
    (999999999999, "999999999999"),
    ("999999999999.9999990000", "999999999999.9999990000"),
    ("1e11", "1E+11"),
    ("0.000001", "0.000001"),  # the smallest nonzero storable amount
    (Decimal("1E-6"), "0.000001"),
    ("100.500000000", "100.500000000"),
    (Decimal("1.0000000"), "1.0000000"),
    ("0.0000000", "0E-7"),
    ("1." + "0" * 62, "1." + "0" * 62),  # exactly 64 characters
]


@pytest.mark.parametrize(AMOUNT_SITE_ARGS, AMOUNT_SITES)
@pytest.mark.parametrize(("value", "raw"), OUT_OF_RANGE_AMOUNTS)
def test_an_amount_numeric_18_6_cannot_hold_is_a_defect_at_every_amount_field(
    surface: str,
    field_name: str,
    place: Callable[[object], dict[str, Any]],
    read: Callable[[dict[str, Any]], Decimal | None],
    value: str | int | Decimal,
    raw: str,
) -> None:
    element = place(value)
    if isinstance(value, str):
        _conforming(surface, element)  # a contract-valid BigDecimal string
    error = _expect_defect(lambda: read(element), surface, f"field '{field_name}' {OUT_OF_RANGE}")
    _assert_not_echoed(error, raw)


@pytest.mark.parametrize(AMOUNT_SITE_ARGS, BUDGET_SITES)
@pytest.mark.parametrize(
    ("value", "raw"),
    [
        *((value, raw) for value, _rounded, raw in EXCESS_PLACE_AMOUNTS),
        *ROUNDING_OUT_OF_RANGE_AMOUNTS,
    ],
)
def test_a_budget_with_a_digit_beyond_the_sixth_place_is_a_defect_never_rounded(
    surface: str,
    field_name: str,
    place: Callable[[object], dict[str, Any]],
    read: Callable[[dict[str, Any]], Decimal | None],
    value: str | Decimal,
    raw: str,
) -> None:
    """A budget numeric(18, 6) would round is rejected: C0 compares budgets at the minor unit."""
    element = place(value)
    if isinstance(value, str):
        _conforming(surface, element)  # a contract-valid BigDecimal string
    error = _expect_defect(lambda: read(element), surface, f"field '{field_name}' {TOO_PRECISE}")
    _assert_not_echoed(error, raw)


@pytest.mark.parametrize(AMOUNT_SITE_ARGS, COST_SITES)
@pytest.mark.parametrize(
    ("value", "rounded"), [(value, rounded) for value, rounded, _raw in EXCESS_PLACE_AMOUNTS]
)
def test_a_cost_with_a_digit_beyond_the_sixth_place_is_rounded_half_even_to_six_places(
    surface: str,
    field_name: str,
    place: Callable[[object], dict[str, Any]],
    read: Callable[[dict[str, Any]], Decimal | None],
    value: str | Decimal,
    rounded: str,
) -> None:
    """Live 202609 costs carry up to 17 decimal places; each is stored at numeric(18, 6)."""
    del field_name  # no defect is raised, so no field is named
    element = place(value)
    if isinstance(value, str):
        _conforming(surface, element)  # a contract-valid BigDecimal string
    cost = read(element)
    assert cost is not None
    assert str(cost) == rounded
    assert cost.as_tuple().exponent == -6  # exactly six places
    assert not cost.is_signed()


@pytest.mark.parametrize(AMOUNT_SITE_ARGS, COST_SITES)
@pytest.mark.parametrize(("value", "raw"), ROUNDING_OUT_OF_RANGE_AMOUNTS)
def test_a_cost_that_rounds_to_10_12_is_out_of_range(
    surface: str,
    field_name: str,
    place: Callable[[object], dict[str, Any]],
    read: Callable[[dict[str, Any]], Decimal | None],
    value: str | Decimal,
    raw: str,
) -> None:
    """The range is checked after rounding: a cost rounding to 10**12 overflows the column."""
    element = place(value)
    if isinstance(value, str):
        _conforming(surface, element)  # a contract-valid BigDecimal string
    error = _expect_defect(lambda: read(element), surface, f"field '{field_name}' {OUT_OF_RANGE}")
    _assert_not_echoed(error, raw)


def test_cost_rounding_is_independent_of_the_callers_decimal_context() -> None:
    """A narrow, truncating, trapping caller context changes neither the result nor the flow."""
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        daily = parse_analytics_daily(_analytics_cost("19.91833000000000123")).cost
        segment = parse_segment(_segment_cost("0.0000015"), "MEMBER_SENIORITY").cost
    assert str(daily) == "19.918330"
    assert str(segment) == "0.000002"


def test_parse_analytics_daily_rounds_a_live_cost_and_keeps_every_other_metric() -> None:
    """A live-shaped element (17-decimal float residue) parses like the documented one."""
    documented = parse_analytics_daily(_daily(1))
    live = parse_analytics_daily(_daily(1, costInLocalCurrency="19.91833000000000123"))
    assert documented.cost is not None
    assert str(documented.cost) == "19.91833"  # within six places: returned as sent
    assert live.cost is not None
    assert str(live.cost) == "19.918330"
    assert dataclasses.replace(live, cost=None) == dataclasses.replace(documented, cost=None)


@pytest.mark.parametrize(AMOUNT_SITE_ARGS, AMOUNT_SITES)
@pytest.mark.parametrize(("value", "expected"), STORABLE_AMOUNTS)
def test_an_amount_numeric_18_6_holds_is_returned_unchanged_at_every_amount_field(
    surface: str,
    field_name: str,
    place: Callable[[object], dict[str, Any]],
    read: Callable[[dict[str, Any]], Decimal | None],
    value: str | int | Decimal,
    expected: str,
) -> None:
    element = place(value)
    if isinstance(value, str):
        _conforming(surface, element)  # a contract-valid BigDecimal string
    amount = read(element)
    assert amount is not None
    assert amount == Decimal(value)
    # The same sign, digits and exponent: never rounded, rescaled or normalised.
    assert amount.as_tuple() == Decimal(value).as_tuple()
    assert str(amount) == expected


# A zero fits numeric(18, 6) whatever its exponent, although Decimal("0E+19").adjusted() is 19
# and Decimal("0E-30") has thirty decimal places, and a signed zero is returned unsigned.
ZERO_NUMERALS = ["0E+19", "-0E+19", "0e999", "0E-30"]
ZERO_AMOUNTS: list[object] = [*ZERO_NUMERALS, Decimal("0E+19"), Decimal("0E-30")]


@pytest.mark.parametrize("value", ZERO_AMOUNTS)
def test_parse_analytics_daily_accepts_a_zero_cost_with_a_large_exponent(value: object) -> None:
    element = _daily(1)
    element["costInLocalCurrency"] = value
    if isinstance(value, str):
        _conforming("ad_analytics", element)  # a contract-valid BigDecimal string
    cost = parse_analytics_daily(element).cost
    assert cost is not None
    assert cost == Decimal(0)
    assert not cost.is_signed()


@pytest.mark.parametrize("value", ZERO_NUMERALS)
def test_parse_segment_accepts_a_zero_cost_with_a_large_exponent(value: str) -> None:
    element = _segment([SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"], costInLocalCurrency=value)
    cost = parse_segment(element, "MEMBER_SENIORITY").cost
    assert cost is not None
    assert cost == Decimal(0)
    assert not cost.is_signed()


@pytest.mark.parametrize("value", ZERO_AMOUNTS)
def test_parse_campaign_group_accepts_a_zero_budget_with_a_large_exponent(value: object) -> None:
    element = _sample_element("ad_campaign_groups")
    element["dailyBudget"] = value
    element["totalBudget"] = {"amount": value, "currencyCode": "USD"}
    if isinstance(value, str):
        _conforming("ad_campaign_groups", element)  # contract-valid BigDecimal strings
    row = parse_campaign_group(element, account_currency="USD")
    for budget in (row.daily_budget, row.total_budget):
        assert budget is not None
        assert budget.currency == "USD"
        assert budget.amount == Decimal(0)
        assert not budget.amount.is_signed()


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("dailyBudget", "1e30", "field 'dailyBudget' is out of range"),
        ("dailyBudget", "1e" + "9" * 25, "field 'dailyBudget' is out of range"),
        ("dailyBudget", Decimal("1E+30"), "field 'dailyBudget' is out of range"),
        ("dailyBudget", "1" * 65, "field 'dailyBudget' is longer than 64 characters"),
        (
            "totalBudget",
            {"amount": "1e30", "currencyCode": "USD"},
            "field 'amount' is out of range",
        ),
        (
            "totalBudget",
            {"amount": "1e" + "9" * 25, "currencyCode": "USD"},
            "field 'amount' is out of range",
        ),
    ],
)
def test_parse_campaign_group_rejects_a_budget_beyond_the_decimal_bounds(
    key: str, value: object, fragment: str
) -> None:
    element = _sample_element("ad_campaign_groups")
    element[key] = value
    error = _expect_defect(
        lambda: parse_campaign_group(element, account_currency="USD"),
        "ad_campaign_groups",
        fragment,
    )
    _assert_not_echoed(error, "1e30", "1E+30", "9999", "1111")


@pytest.mark.parametrize("version", ["9" * 5000, "9" * 20, str(2**63)])
def test_parse_versioned_form_urn_rejects_a_version_beyond_64_bits(version: str) -> None:
    urn = f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{FORM_ID},{version})"
    for surface in ("lead_form_responses", "lead_form_responses_batch"):
        error = _expect_defect(
            lambda surface=surface: parse_versioned_form_urn(urn, surface=surface),
            surface,
            f"the form version {OUT_OF_INT64}",
        )
        _assert_not_echoed(error, version[:8])
    error = _expect_defect(
        lambda: canonical_form_urn(urn), "lead_forms", f"the form version {OUT_OF_INT64}"
    )
    _assert_not_echoed(error, version[:8])


def test_parse_versioned_form_urn_accepts_the_largest_64_bit_version() -> None:
    urn = f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{FORM_ID},{INT64_MAX})"
    assert parse_versioned_form_urn(urn) == (FORM_URN, INT64_MAX)


def test_a_lead_with_a_version_beyond_64_bits_is_a_defect_on_ingest_and_re_fetch() -> None:
    urn = f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{FORM_ID},{'9' * 5000})"
    element = _mutate(("versionedLeadGenFormUrn",), urn)
    for error in (
        _expect_defect(
            lambda: parse_lead(element, {}),
            "lead_form_responses",
            f"the form version {OUT_OF_INT64}",
        ),
        _expect_defect(
            lambda: decode_email(element, {}),
            "lead_form_responses_batch",
            f"the form version {OUT_OF_INT64}",
        ),
    ):
        _assert_not_echoed(error, "9999", "lead.one", "example.com")


# ---------------------------------------------------------------------------------------------
# Demographic segments
# ---------------------------------------------------------------------------------------------

# Demographic value URNs from the Targeting Criteria Facet URNs reference the statistics
# contract cites for pivotValues.
SEGMENT_CASES = [
    ("MEMBER_JOB_FUNCTION", "urn:li:function:22", "job_function"),
    ("MEMBER_SENIORITY", "urn:li:seniority:7", "seniority"),
    ("MEMBER_INDUSTRY", "urn:li:industry:9", "industry"),
]
SAMPLE_SEGMENT_CAMPAIGN = "urn:li:sponsoredCampaign:1234567"


def _segment(pivot_values: Sequence[str], **fields: Any) -> dict[str, Any]:
    """The documented statistics element with ``pivotValues`` replaced."""
    element: dict[str, Any] = _sample_element("ad_analytics_statistics")
    element["pivotValues"] = list(pivot_values)
    element.update(fields)
    return _conforming("ad_analytics_statistics", element)


def test_documented_statistics_sample_verbatim_lacks_a_demographic_value() -> None:
    element = _sample_element("ad_analytics_statistics")
    _expect_defect(
        lambda: parse_segment(element, "MEMBER_SENIORITY"),
        "ad_analytics_statistics",
        "field 'pivotValues' must hold exactly one demographic value",
    )


@pytest.mark.parametrize(("pivot", "value", "facet"), SEGMENT_CASES)
def test_parse_segment_documented_sample_with_a_demographic_value(
    pivot: str, value: str, facet: str
) -> None:
    element = _sample_element("ad_analytics_statistics")
    element["pivotValues"].append(value)
    row = parse_segment(_conforming("ad_analytics_statistics", element), pivot)
    assert row == SegmentRow(
        campaign_urn=SAMPLE_SEGMENT_CAMPAIGN,
        facet=facet,
        value=value,
        impressions=54494,
        clicks=177,
        # Metrics the element does not return are unavailable, never zero.
        cost=None,
        leads=None,
        website_conversions=None,
    )
    assert SEGMENT_FACETS[pivot] == facet


@pytest.mark.parametrize(
    ("pivot_values", "surface", "campaign_urn"),
    [
        pytest.param(
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"],
            "ad_analytics_statistics",
            None,
            id="statistics-finder",
        ),
        pytest.param(
            ["urn:li:seniority:7"],
            "ad_analytics_member_segments",
            SAMPLE_SEGMENT_CAMPAIGN,
            id="analytics-finder-fallback",
        ),
    ],
)
def test_parse_segment_rounds_a_live_cost_with_17_decimal_places(
    pivot_values: list[str], surface: str, campaign_urn: str | None
) -> None:
    """Both segment forms round a live-shaped cost half-even to six places."""
    element = _segment(pivot_values, costInLocalCurrency="4.25000000000000355")
    row = parse_segment(element, "MEMBER_SENIORITY", campaign_urn=campaign_urn, surface=surface)
    assert row.cost is not None
    assert str(row.cost) == "4.250000"
    assert (row.campaign_urn, row.value) == (SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7")
    assert (row.impressions, row.clicks) == (54494, 177)


@pytest.mark.parametrize(("pivot", "value", "facet"), SEGMENT_CASES)
def test_parse_segment_value_first_order(pivot: str, value: str, facet: str) -> None:
    row = parse_segment(_segment([value, SAMPLE_SEGMENT_CAMPAIGN]), pivot)
    assert row.campaign_urn == SAMPLE_SEGMENT_CAMPAIGN
    assert row.facet == facet
    assert row.value == value


@pytest.mark.parametrize("requested", [1234567, "1234567", SAMPLE_SEGMENT_CAMPAIGN])
def test_parse_segment_analytics_finder_fallback_takes_the_requested_campaign(
    requested: str | int,
) -> None:
    row = parse_segment(
        _segment(["urn:li:seniority:7"]), "MEMBER_SENIORITY", campaign_urn=requested
    )
    assert row.campaign_urn == SAMPLE_SEGMENT_CAMPAIGN
    assert row.facet == "seniority"


def test_parse_segment_agreeing_campaigns_and_every_metric() -> None:
    element = _segment(
        [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:industry:9"],
        costInLocalCurrency="88.40",
        oneClickLeads=6,
        externalWebsiteConversions=1,
    )
    row = parse_segment(element, "MEMBER_INDUSTRY", campaign_urn=SAMPLE_SEGMENT_CAMPAIGN)
    assert row.cost == Decimal("88.40")
    assert row.leads == 6
    assert row.website_conversions == 1


@pytest.mark.parametrize(
    ("pivot_values", "pivot", "requested", "fragment"),
    [
        (
            ["urn:li:seniority:7"],
            "MEMBER_SENIORITY",
            None,
            "field 'pivotValues' holds no campaign and none was requested",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"],
            "MEMBER_SENIORITY",
            OTHER_CAMPAIGN_URN,
            "field 'pivotValues' names a campaign other than requested",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, OTHER_CAMPAIGN_URN, "urn:li:seniority:7"],
            "MEMBER_SENIORITY",
            None,
            "field 'pivotValues' holds more than one campaign",
        ),
        (
            ["urn:li:sponsoredCampaign:abc", "urn:li:seniority:7"],
            "MEMBER_SENIORITY",
            None,
            "field 'pivotValues' must be a urn:li:sponsoredCampaign:{id} URN",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7", "urn:li:seniority:8"],
            "MEMBER_SENIORITY",
            None,
            "field 'pivotValues' must hold exactly one demographic value",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"],
            "MEMBER_INDUSTRY",
            None,
            "the demographic value must be a urn:li:industry:{id} URN",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:function:22"],
            "MEMBER_SENIORITY",
            None,
            "the demographic value must be a urn:li:seniority:{id} URN",
        ),
        (
            [SAMPLE_SEGMENT_CAMPAIGN, "urn:li:function:abc"],
            "MEMBER_JOB_FUNCTION",
            None,
            "the demographic value must be a urn:li:function:{id} URN",
        ),
    ],
)
def test_parse_segment_rejects_inconsistent_pivot_values(
    pivot_values: list[str], pivot: str, requested: str | None, fragment: str
) -> None:
    element: dict[str, Any] = _sample_element("ad_analytics_statistics")
    element["pivotValues"] = pivot_values
    _expect_defect(
        lambda: parse_segment(element, pivot, campaign_urn=requested),
        "ad_analytics_statistics",
        fragment,
    )


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"impressions": "many"}, "field 'impressions' must be an integer"),
        ({"clicks": -1}, "field 'clicks' must not be negative"),
        ({"costInLocalCurrency": 1.5}, "field 'costInLocalCurrency' must be a decimal string"),
        ({"oneClickLeads": True}, "field 'oneClickLeads' must be an integer, not a boolean"),
        ({"pivotValues": [7]}, "field 'pivotValues' must hold strings"),
    ],
)
def test_parse_segment_rejects_malformed_metrics(fields: dict[str, Any], fragment: str) -> None:
    element: dict[str, Any] = _sample_element("ad_analytics_statistics")
    element["pivotValues"].append("urn:li:seniority:7")
    element.update(fields)
    _expect_defect(
        lambda: parse_segment(element, "MEMBER_SENIORITY"), "ad_analytics_statistics", fragment
    )


@pytest.mark.parametrize("pivot", ["MEMBER_COMPANY", "CAMPAIGN", "member_seniority", ""])
def test_parse_segment_rejects_an_unknown_pivot(pivot: str) -> None:
    with pytest.raises(ValueError, match=r"pivot must be one of MEMBER_JOB_FUNCTION"):
        parse_segment(_segment([SAMPLE_SEGMENT_CAMPAIGN, "urn:li:seniority:7"]), pivot)


def test_parse_segment_rejects_a_malformed_requested_campaign() -> None:
    with pytest.raises(ValueError, match=r"campaign_id must be a numeric id"):
        parse_segment(
            _segment(["urn:li:seniority:7"]), "MEMBER_SENIORITY", campaign_urn=CREATIVE_URN
        )


def test_parse_segment_rejects_a_non_object() -> None:
    _expect_defect(
        lambda: parse_segment(None, "MEMBER_SENIORITY"),
        "ad_analytics_statistics",
        "a segment element must be a JSON object",
    )


# ---------------------------------------------------------------------------------------------
# Lead-form identity
# ---------------------------------------------------------------------------------------------


def test_parse_versioned_form_urn_documented_sample() -> None:
    urn = _sample_element("lead_form_responses")["versionedLeadGenFormUrn"]
    assert parse_versioned_form_urn(urn) == (SAMPLE_FORM_URN, 1)
    assert parse_versioned_form_urn(VERSIONED_FORM_URN) == (FORM_URN, FORM_VERSION)
    assert parse_versioned_form_urn(
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:6755260984438374400,12)"
    ) == ("urn:li:leadGenForm:6755260984438374400", 12)


@pytest.mark.parametrize(
    "urn",
    [
        "",
        SAMPLE_FORM_URN,
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:abc,1)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,-1)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162, 1)",
        " urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,1)",
        "urn:li:versionedLeadGenForm:(urn:li:adForm:3162,1)",
    ],
)
def test_parse_versioned_form_urn_rejects_malformed_urns(urn: str) -> None:
    _expect_defect(
        lambda: parse_versioned_form_urn(urn),
        "lead_form_responses",
        "a versioned lead-form URN is malformed",
    )


def test_parse_versioned_form_urn_names_the_given_surface() -> None:
    _expect_defect(
        lambda: parse_versioned_form_urn("bad", surface="lead_form_responses_batch"),
        "lead_form_responses_batch",
        "a versioned lead-form URN is malformed",
    )
    with pytest.raises(ValueError, match=r"unknown LinkedIn contract surface"):
        parse_versioned_form_urn(VERSIONED_FORM_URN, surface="leadFormResponses")


@pytest.mark.parametrize(
    "value",
    [FORM_URN, "urn:li:adForm:990002001", VERSIONED_FORM_URN],
)
def test_canonical_form_urn_accepts_every_reference_form(value: str) -> None:
    assert canonical_form_urn(value) == FORM_URN


@pytest.mark.parametrize(
    "value",
    [
        "",
        "990002001",
        "urn:li:leadGenForm:",
        "urn:li:leadGenForm:abc",
        "urn:li:adForm:abc",
        "urn:li:sponsoredCreative:990002001",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990002001)",
        990002001,
        None,
    ],
)
def test_canonical_form_urn_rejects_malformed_references(value: Any) -> None:
    _expect_defect(
        lambda: canonical_form_urn(value),
        "lead_forms",
        "a lead-form reference is not a leadGenForm, adForm or versioned URN",
    )


def test_canonical_form_urn_names_the_given_surface() -> None:
    _expect_defect(
        lambda: canonical_form_urn("urn:li:adForm:", surface="creatives"),
        "creatives",
        "a lead-form reference is not",
    )
    with pytest.raises(ValueError, match=r"unknown LinkedIn contract surface"):
        canonical_form_urn(FORM_URN, surface="access_token")


# ---------------------------------------------------------------------------------------------
# Form version maps
# ---------------------------------------------------------------------------------------------


def test_form_version_map_storage_round_trip() -> None:
    learned = FormVersionMap(
        canonical_urn=FORM_URN,
        version=FORM_VERSION,
        email_question_ids=(EMAIL_QID, WORK_EMAIL_QID),
        question_ids=frozenset({FIRST_NAME_QID, EMAIL_QID, WORK_EMAIL_QID}),
    )
    stored = learned.stored_email_question_ids
    assert stored == ["20", "30"]  # preference order survives text[] storage
    rebuilt = FormVersionMap.from_storage(learned.canonical_urn, learned.version, stored)
    assert rebuilt == FormVersionMap(
        canonical_urn=FORM_URN,
        version=FORM_VERSION,
        email_question_ids=(EMAIL_QID, WORK_EMAIL_QID),
        question_ids=None,  # only email ids are persisted
    )
    assert rebuilt.stored_email_question_ids == stored
    empty = FormVersionMap.from_storage(FORM_URN, 0, [])
    assert empty.email_question_ids == ()
    assert empty.stored_email_question_ids == []


@pytest.mark.parametrize("stored", [["20", "x"], ["-20"], [""], ["2 0"], ["20.0"]])
def test_form_version_map_from_storage_rejects_non_digit_ids(stored: list[str]) -> None:
    with pytest.raises(ValueError, match=r"stored email question ids must be digit strings"):
        FormVersionMap.from_storage(FORM_URN, FORM_VERSION, stored)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"canonical_urn": VERSIONED_FORM_URN}, r"canonical_urn must be a urn:li:leadGenForm"),
        ({"canonical_urn": "urn:li:adForm:990002001"}, r"canonical_urn must be a"),
        ({"canonical_urn": "urn:li:leadGenForm:"}, r"canonical_urn must be a"),
        ({"version": -1}, r"version must be a non-negative integer"),
        ({"version": True}, r"version must be a non-negative integer"),
        ({"email_question_ids": (EMAIL_QID, EMAIL_QID)}, r"must not repeat an id"),
        (
            {"email_question_ids": (EMAIL_QID,), "question_ids": frozenset({FIRST_NAME_QID})},
            r"email_question_ids must be among question_ids",
        ),
    ],
)
def test_form_version_map_validates_on_construction(fields: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {
        "canonical_urn": FORM_URN,
        "version": FORM_VERSION,
        "email_question_ids": (),
        "question_ids": None,
    }
    arguments.update(fields)
    with pytest.raises(ValueError, match=message):
        FormVersionMap(**arguments)


def test_form_version_map_rejects_a_malformed_stored_identity() -> None:
    with pytest.raises(ValueError, match=r"canonical_urn must be a"):
        FormVersionMap.from_storage("urn:li:leadGenForm:x", FORM_VERSION, ["20"])


def test_form_version_map_is_frozen() -> None:
    version_map = FormVersionMap(FORM_URN, FORM_VERSION, (EMAIL_QID,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cast(Any, version_map).version = 4


def test_form_map_from_decoration_orders_email_questions() -> None:
    questions = [
        _question(WORK_EMAIL_QID, "WORK_EMAIL"),
        _question(25, "EMAIL"),
        _question(FIRST_NAME_QID, "FIRST_NAME"),
        _question(EMAIL_QID, "EMAIL"),
        _question(CUSTOM_QID),
        _question(35, "WORK_EMAIL"),
    ]
    form = _lead([], questions=questions)["form"]
    version_map = form_map_from_decoration(FORM_URN, FORM_VERSION, form)
    assert version_map == FormVersionMap(
        canonical_urn=FORM_URN,
        version=FORM_VERSION,
        # EMAIL questions first, then WORK_EMAIL, each ascending.
        email_question_ids=(EMAIL_QID, 25, WORK_EMAIL_QID, 35),
        question_ids=frozenset({WORK_EMAIL_QID, 25, FIRST_NAME_QID, EMAIL_QID, CUSTOM_QID, 35}),
    )


REPEATED_QUESTION = "field 'questionId' repeats a question of the form version"
REPEATED_ANSWER = "field 'questionId' repeats an answered question"

# Two definitions of one question id: conflicting predefined fields, or the same one twice.
REPEATED_QUESTION_CASES = [
    [_question(EMAIL_QID, "EMAIL"), _question(EMAIL_QID, "WORK_EMAIL")],
    [_question(EMAIL_QID, "EMAIL"), _question(FIRST_NAME_QID), _question(EMAIL_QID, "FIRST_NAME")],
    [_question(WORK_EMAIL_QID, "WORK_EMAIL"), _question(WORK_EMAIL_QID, "WORK_EMAIL")],
]


def _assert_carries_no_email(error: QueryDefect) -> None:
    """Assert a defect names fields only: no lead address, raw or normalised, in any text."""
    for text in (str(error), error.message, error.reason, repr(error.args)):
        for address in (EMAIL, OTHER_EMAIL):
            assert address not in text.lower()
        assert "example.com" not in text.lower()


@pytest.mark.parametrize("questions", REPEATED_QUESTION_CASES)
@pytest.mark.parametrize("surface", ["lead_form_responses", "lead_form_responses_batch"])
def test_form_map_from_decoration_rejects_a_repeated_question_id(
    questions: list[dict[str, Any]], surface: str
) -> None:
    form = _lead([], questions=questions)["form"]
    error = _expect_defect(
        lambda: form_map_from_decoration(FORM_URN, FORM_VERSION, form, surface=surface),
        surface,
        REPEATED_QUESTION,
    )
    # The message names the field only, never the repeated id.
    assert error.message == f"{surface}: {REPEATED_QUESTION}"


@pytest.mark.parametrize(
    ("form", "fragment"),
    [
        ([], "the form decoration must be a JSON object"),
        ({"id": FORM_ID}, "required field 'content' is missing"),
        ({"content": {}}, "required field 'questions' is missing"),
        ({"content": {"questions": {}}}, "field 'questions' must be a JSON array"),
        ({"content": {"questions": [7]}}, "a form question must be a JSON object"),
        ({"content": {"questions": [{"name": "email"}]}}, "required field 'questionId'"),
        (
            {"content": {"questions": [{"questionId": "20"}, {"questionId": "x"}]}},
            "field 'questionId' must be an integer",
        ),
        (
            {"content": {"questions": [{"questionId": 20, "predefinedField": 1}]}},
            "field 'predefinedField' must be a string",
        ),
    ],
)
def test_form_map_from_decoration_rejects_malformed_decorations(form: Any, fragment: str) -> None:
    _expect_defect(
        lambda: form_map_from_decoration(FORM_URN, FORM_VERSION, form),
        "lead_form_responses",
        fragment,
    )


def test_form_map_from_decoration_checks_its_arguments() -> None:
    form = _lead([])["form"]
    with pytest.raises(ValueError, match=r"unknown LinkedIn contract surface"):
        form_map_from_decoration(FORM_URN, FORM_VERSION, form, surface="forms")
    with pytest.raises(ValueError, match=r"canonical_urn must be a"):
        form_map_from_decoration(VERSIONED_FORM_URN, FORM_VERSION, form)
    batch = form_map_from_decoration(
        FORM_URN, FORM_VERSION, form, surface="lead_form_responses_batch"
    )
    assert batch.email_question_ids == (EMAIL_QID, WORK_EMAIL_QID)


# ---------------------------------------------------------------------------------------------
# Lead forms
# ---------------------------------------------------------------------------------------------


def test_parse_lead_form_documented_sample() -> None:
    canonical = "urn:li:leadGenForm:6755260984438374400"
    assert parse_lead_form(_sample_element("lead_forms")) == LeadFormRow(
        canonical_urn=canonical,
        external_id="6755260984438374400",  # the 64-bit id survives exactly
        name="LeadGen form for Nimbus 2000",
        current_version=1,
        version_map=FormVersionMap(
            canonical_urn=canonical,
            version=1,
            email_question_ids=(),  # the sample asks for first and last name only
            question_ids=frozenset({10548, 10540}),
        ),
    )


def test_parse_lead_form_current_version_email_questions() -> None:
    element: dict[str, Any] = _sample_element("lead_forms")
    element["id"] = FORM_ID
    element["versionId"] = FORM_VERSION
    element["content"]["questions"].extend(
        [_question(WORK_EMAIL_QID, "WORK_EMAIL"), _question(EMAIL_QID, "EMAIL")]
    )
    del element["name"]
    row = parse_lead_form(_conforming("lead_forms", element))
    assert row.canonical_urn == FORM_URN
    assert row.external_id == "990002001"
    assert row.name is None
    assert row.current_version == FORM_VERSION
    assert row.version_map.canonical_urn == FORM_URN
    assert row.version_map.version == FORM_VERSION
    assert row.version_map.email_question_ids == (EMAIL_QID, WORK_EMAIL_QID)
    assert row.version_map.question_ids == frozenset({10548, 10540, EMAIL_QID, WORK_EMAIL_QID})
    stored = row.version_map.stored_email_question_ids
    assert FormVersionMap.from_storage(FORM_URN, FORM_VERSION, stored).email_question_ids == (
        EMAIL_QID,
        WORK_EMAIL_QID,
    )


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("id", None, "required field 'id' is missing"),
        ("id", "form-1", "field 'id' must be an integer"),
        ("versionId", None, "required field 'versionId' is missing"),
        ("versionId", -1, "field 'versionId' must not be negative"),
        ("content", None, "required field 'content' is missing"),
        ("content", [], "field 'content' must be a JSON object"),
        ("content", {"questions": None}, "required field 'questions' is missing"),
        ("name", 5, "field 'name' must be a string"),
    ],
)
def test_parse_lead_form_rejects_malformed_fields(key: str, value: object, fragment: str) -> None:
    element = _sample_element("lead_forms")
    element[key] = value
    _expect_defect(lambda: parse_lead_form(element), "lead_forms", fragment)


def test_parse_lead_form_rejects_a_non_object() -> None:
    _expect_defect(lambda: parse_lead_form(1), "lead_forms", "a lead form must be a JSON object")


@pytest.mark.parametrize("questions", REPEATED_QUESTION_CASES)
def test_parse_lead_form_rejects_a_repeated_question_id(questions: list[dict[str, Any]]) -> None:
    element: dict[str, Any] = _sample_element("lead_forms")
    element["content"]["questions"].extend(questions)
    error = _expect_defect(
        lambda: parse_lead_form(_conforming("lead_forms", element)), "lead_forms", REPEATED_QUESTION
    )
    assert error.message == f"lead_forms: {REPEATED_QUESTION}"


def test_parse_lead_form_rejects_a_repeat_of_the_documented_question() -> None:
    element: dict[str, Any] = _sample_element("lead_forms")
    element["content"]["questions"].append(copy.deepcopy(element["content"]["questions"][0]))
    _expect_defect(
        lambda: parse_lead_form(_conforming("lead_forms", element)), "lead_forms", REPEATED_QUESTION
    )


# ---------------------------------------------------------------------------------------------
# Lead responses: documented samples
# ---------------------------------------------------------------------------------------------

SAMPLE_LEAD = ParsedLead(
    external_id=SAMPLE_LEAD_ID,
    submitted_at=datetime(2023, 6, 7, 23, 59, 18, 881000, tzinfo=UTC),
    account_urn="urn:li:sponsoredAccount:522529623",
    campaign_urn="urn:li:sponsoredCampaign:367378525",
    creative_urn="urn:li:sponsoredCreative:121608865",
    lead_form_urn=SAMPLE_FORM_URN,
    lead_form_version=1,
    email_sha256=None,
    email_status="no_email",
    email_reason=REASON_UNKNOWN_FORM_VERSION,
    version_map=None,
)


def test_parse_lead_documented_sample_without_a_known_version() -> None:
    # The unprojected sample carries no form decoration, and no map is stored for its version.
    assert parse_lead(_sample_element("lead_form_responses"), {}) == SAMPLE_LEAD


def _batch_results() -> dict[str, Any]:
    """The documented batch ``results`` map without its non-id keys.

    The contract records that the documented sample nests empty ``statuses`` and ``errors``
    maps inside ``results`` beside the id key, and that they are skipped as non-id keys.
    """
    results: dict[str, Any] = _sample("lead_form_responses_batch")["results"]
    assert results.pop("statuses") == {}
    assert results.pop("errors") == {}
    return results


def test_parse_lead_documented_batch_sample() -> None:
    results = _batch_results()
    assert list(results) == [SAMPLE_LEAD_ID]
    for lead_id, element in results.items():
        lead = parse_lead(element, {}, surface="lead_form_responses_batch")
        assert lead == SAMPLE_LEAD
        assert lead is not None
        assert lead.external_id == lead_id
    decoded = decode_email(results[SAMPLE_LEAD_ID], {})
    assert decoded == DecodedEmail(
        email=None, sha256=None, status="no_email", reason=REASON_UNKNOWN_FORM_VERSION
    )


@pytest.mark.parametrize(
    ("version_map", "reason"),
    [
        # Question 1 is the email question, but its answer is free text, not an address.
        (FormVersionMap(SAMPLE_FORM_URN, 1, (1,), frozenset({1, 2})), REASON_NO_EMAIL_ANSWER),
        (FormVersionMap.from_storage(SAMPLE_FORM_URN, 1, ["1"]), REASON_NO_EMAIL_ANSWER),
        (FormVersionMap(SAMPLE_FORM_URN, 1, (), frozenset({1, 2})), REASON_NO_EMAIL_QUESTION),
        # Answered question 2 is not among the stored version's questions.
        (FormVersionMap(SAMPLE_FORM_URN, 1, (1,), frozenset({1})), REASON_FORM_MAP_MISMATCH),
    ],
)
def test_parse_lead_documented_sample_with_a_stored_map(
    version_map: FormVersionMap, reason: str
) -> None:
    known = {(SAMPLE_FORM_URN, 1): version_map}
    lead = parse_lead(_sample_element("lead_form_responses"), known)
    assert lead == dataclasses.replace(SAMPLE_LEAD, email_reason=reason)


# ---------------------------------------------------------------------------------------------
# Lead responses: email decoding per form version
# ---------------------------------------------------------------------------------------------


def test_parse_lead_hashes_the_trimmed_lowercased_email() -> None:
    element = _lead([_text_answer(FIRST_NAME_QID, "Lead"), _text_answer(EMAIL_QID, RAW_EMAIL)])
    lead = parse_lead(element, {})
    assert lead == ParsedLead(
        external_id=LEAD_ID,
        submitted_at=datetime(2023, 6, 7, 23, 59, 18, 881000, tzinfo=UTC),
        account_urn=ACCOUNT_URN,
        campaign_urn=CAMPAIGN_URN,
        creative_urn=CREATIVE_URN,
        lead_form_urn=FORM_URN,
        lead_form_version=FORM_VERSION,
        email_sha256=EMAIL_SHA256,
        email_status="ok",
        email_reason=None,
        version_map=FormVersionMap(
            canonical_urn=FORM_URN,
            version=FORM_VERSION,
            email_question_ids=(EMAIL_QID, WORK_EMAIL_QID),
            question_ids=frozenset({FIRST_NAME_QID, EMAIL_QID, WORK_EMAIL_QID, CUSTOM_QID}),
        ),
    )
    assert lead is not None
    assert lead.email_sha256 is not None
    assert re.fullmatch(r"[0-9a-f]{64}", lead.email_sha256)


def test_parse_lead_learned_map_round_trips_through_storage() -> None:
    lead = parse_lead(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)]), {})
    assert lead is not None
    assert lead.version_map is not None
    learned = lead.version_map
    known = {
        (learned.canonical_urn, learned.version): FormVersionMap.from_storage(
            learned.canonical_urn, learned.version, learned.stored_email_question_ids
        )
    }
    undecorated = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], decorated=False)
    again = parse_lead(undecorated, known)
    assert again is not None
    assert again.email_sha256 == lead.email_sha256


def test_parse_lead_reads_a_work_email_only_form() -> None:
    element = _lead(
        [_text_answer(WORK_EMAIL_QID, RAW_EMAIL)],
        questions=[
            _question(FIRST_NAME_QID, "FIRST_NAME"),
            _question(WORK_EMAIL_QID, "WORK_EMAIL"),
        ],
    )
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_sha256 == EMAIL_SHA256


def test_parse_lead_prefers_email_over_work_email() -> None:
    element = _lead([_text_answer(WORK_EMAIL_QID, OTHER_EMAIL), _text_answer(EMAIL_QID, RAW_EMAIL)])
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_sha256 == EMAIL_SHA256


@pytest.mark.parametrize("unusable", ["n/a", "", "   ", "@example.com", "lead@", "a@b@example.com"])
def test_parse_lead_falls_back_to_work_email_when_email_is_unusable(unusable: str) -> None:
    element = _lead(
        [_text_answer(EMAIL_QID, unusable), _text_answer(WORK_EMAIL_QID, f" {OTHER_EMAIL} ")]
    )
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_sha256 == OTHER_EMAIL_SHA256


# An address carrying an escaped unpaired surrogate, decoded the way the HTTP layer decodes JSON.
# The string exists but UTF-8 cannot encode it, so it can never be hashed.
SURROGATE_EMAIL: str = json.loads('"lead\\ud800.one@example.com"')


def test_parse_lead_and_decode_email_treat_an_unencodable_answer_as_no_email() -> None:
    element = _lead([_text_answer(EMAIL_QID, SURROGATE_EMAIL)])
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_NO_EMAIL_ANSWER
    assert lead.email_sha256 is None
    assert lead.version_map is not None  # the decoration passed the check and is still learned
    decoded = decode_email(element, {})
    assert decoded == DecodedEmail(
        email=None, sha256=None, status="no_email", reason=REASON_NO_EMAIL_ANSWER
    )
    for text in (repr(lead), repr(decoded)):
        assert "example.com" not in text.lower()


def test_parse_lead_falls_back_to_work_email_when_email_is_unencodable() -> None:
    element = _lead(
        [_text_answer(EMAIL_QID, SURROGATE_EMAIL), _text_answer(WORK_EMAIL_QID, OTHER_EMAIL)]
    )
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_sha256 == OTHER_EMAIL_SHA256
    decoded = decode_email(element, {})
    assert decoded == DecodedEmail(
        email=OTHER_EMAIL, sha256=OTHER_EMAIL_SHA256, status="ok", reason=None
    )


def test_parse_lead_accepts_a_non_ascii_address_that_utf8_encodes() -> None:
    element = _lead([_text_answer(EMAIL_QID, " Léa.Ünal@Exämple.COM ")])
    expected = "léa.ünal@exämple.com"
    expected_sha256 = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_sha256 == expected_sha256
    decoded = decode_email(element, {})
    assert decoded == DecodedEmail(email=expected, sha256=expected_sha256, status="ok", reason=None)


@pytest.mark.parametrize(
    ("answers", "decorated"),
    [
        # Two answers to the email question: neither address may be chosen.
        ([_text_answer(EMAIL_QID, RAW_EMAIL), _text_answer(EMAIL_QID, OTHER_EMAIL)], True),
        ([_text_answer(EMAIL_QID, RAW_EMAIL), _text_answer(EMAIL_QID, OTHER_EMAIL)], False),
        # A repeated non-email answer is the same defect, before any map is consulted.
        (
            [
                _text_answer(FIRST_NAME_QID, "Lead"),
                _text_answer(EMAIL_QID, RAW_EMAIL),
                _text_answer(FIRST_NAME_QID, "Lead"),
            ],
            True,
        ),
    ],
)
@pytest.mark.parametrize("surface", ["lead_form_responses", "lead_form_responses_batch"])
def test_parse_lead_and_decode_email_reject_a_repeated_answer(
    answers: list[dict[str, Any]], decorated: bool, surface: str
) -> None:
    element = _lead(answers, decorated=decorated)
    for error in (
        _expect_defect(
            lambda: parse_lead(element, _stored(), surface=surface), surface, REPEATED_ANSWER
        ),
        _expect_defect(
            lambda: decode_email(element, _stored(), surface=surface), surface, REPEATED_ANSWER
        ),
    ):
        assert error.message == f"{surface}: {REPEATED_ANSWER}"
        _assert_carries_no_email(error)


@pytest.mark.parametrize("questions", REPEATED_QUESTION_CASES)
@pytest.mark.parametrize("surface", ["lead_form_responses", "lead_form_responses_batch"])
def test_parse_lead_and_decode_email_reject_a_repeated_decorated_question(
    questions: list[dict[str, Any]], surface: str
) -> None:
    # The decoration defines one question twice, so its version's meaning is ambiguous: the
    # response is rejected, never decoded, even with a stored map for the same version.
    element = _lead(
        [_text_answer(EMAIL_QID, RAW_EMAIL), _text_answer(WORK_EMAIL_QID, OTHER_EMAIL)],
        questions=questions,
    )
    for error in (
        _expect_defect(
            lambda: parse_lead(element, _stored(), surface=surface), surface, REPEATED_QUESTION
        ),
        _expect_defect(
            lambda: decode_email(element, _stored(), surface=surface), surface, REPEATED_QUESTION
        ),
    ):
        assert error.message == f"{surface}: {REPEATED_QUESTION}"
        _assert_carries_no_email(error)


def test_parse_lead_form_map_mismatch_never_uses_another_versions_map() -> None:
    # Version 2's stored map knows question 77 as the email question; the response is
    # version 3, whose own decoration does not list 77.
    element = _lead(
        [_text_answer(EMAIL_QID, OTHER_EMAIL), _text_answer(UNDECORATED_QID, RAW_EMAIL)]
    )
    known = {
        (FORM_URN, 2): FormVersionMap(
            FORM_URN, 2, (UNDECORATED_QID,), frozenset({UNDECORATED_QID, EMAIL_QID})
        )
    }
    lead = parse_lead(element, known)
    assert lead is not None
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_FORM_MAP_MISMATCH
    assert lead.email_sha256 is None
    assert lead.version_map is None  # a decoration that failed the check is never learned
    assert lead.lead_form_version == FORM_VERSION
    decoded = decode_email(element, known)
    assert decoded.email is None
    assert decoded.reason == REASON_FORM_MAP_MISMATCH


def test_parse_lead_form_map_mismatch_against_the_stored_map_of_its_version() -> None:
    element = _lead([_text_answer(UNDECORATED_QID, RAW_EMAIL)], decorated=False)
    known = {
        (FORM_URN, FORM_VERSION): FormVersionMap(
            FORM_URN, FORM_VERSION, (EMAIL_QID,), frozenset({EMAIL_QID})
        ),
        (FORM_URN, 2): FormVersionMap(FORM_URN, 2, (UNDECORATED_QID,)),
    }
    lead = parse_lead(element, known)
    assert lead is not None
    assert lead.email_reason == REASON_FORM_MAP_MISMATCH
    assert lead.email_sha256 is None


@pytest.mark.parametrize(
    "known",
    [
        {},
        # Another version of the same form.
        _stored(version=2),
        # The same version number of another form.
        {
            ("urn:li:leadGenForm:1", FORM_VERSION): FormVersionMap(
                "urn:li:leadGenForm:1", FORM_VERSION, (EMAIL_QID,)
            )
        },
    ],
)
def test_parse_lead_unknown_form_version(known: dict[tuple[str, int], FormVersionMap]) -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], decorated=False)
    lead = parse_lead(element, known)
    assert lead is not None
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_UNKNOWN_FORM_VERSION
    assert lead.email_sha256 is None
    assert lead.version_map is None
    assert lead.lead_form_urn == FORM_URN
    assert lead.lead_form_version == FORM_VERSION


def test_parse_lead_decodes_with_the_stored_map_without_a_decoration() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], decorated=False)
    lead = parse_lead(element, _stored())
    assert lead is not None
    assert lead.email_status == "ok"
    assert lead.email_reason is None
    assert lead.email_sha256 == EMAIL_SHA256
    assert lead.version_map is None  # nothing new was learned


def test_parse_lead_stored_map_with_known_questions_passes_the_check() -> None:
    element = _lead(
        [_text_answer(FIRST_NAME_QID, "Lead"), _text_answer(WORK_EMAIL_QID, RAW_EMAIL)],
        decorated=False,
    )
    known = {
        (FORM_URN, FORM_VERSION): FormVersionMap(
            FORM_URN, FORM_VERSION, (WORK_EMAIL_QID,), frozenset({FIRST_NAME_QID, WORK_EMAIL_QID})
        )
    }
    lead = parse_lead(element, known)
    assert lead is not None
    assert lead.email_sha256 == EMAIL_SHA256


def test_parse_lead_decoration_wins_over_a_stored_map_of_the_same_version() -> None:
    # The stored map would read question 30; the response's own decoration says 20.
    element = _lead(
        [_text_answer(EMAIL_QID, RAW_EMAIL), _text_answer(WORK_EMAIL_QID, OTHER_EMAIL)],
        questions=[_question(EMAIL_QID, "EMAIL"), _question(WORK_EMAIL_QID)],
    )
    lead = parse_lead(element, _stored(email_ids=[str(WORK_EMAIL_QID)]))
    assert lead is not None
    assert lead.email_sha256 == EMAIL_SHA256
    assert lead.version_map is not None
    assert lead.version_map.email_question_ids == (EMAIL_QID,)


def test_parse_lead_form_without_an_email_question() -> None:
    element = _lead(
        [_text_answer(FIRST_NAME_QID, "Lead"), _text_answer(CUSTOM_QID, RAW_EMAIL)],
        questions=[_question(FIRST_NAME_QID, "FIRST_NAME"), _question(CUSTOM_QID)],
    )
    lead = parse_lead(element, {})
    assert lead is not None
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_NO_EMAIL_QUESTION
    assert lead.email_sha256 is None
    # The decoration passed the check, so its map is still learned for storage.
    assert lead.version_map == FormVersionMap(
        FORM_URN, FORM_VERSION, (), frozenset({FIRST_NAME_QID, CUSTOM_QID})
    )


@pytest.mark.parametrize(
    "answers",
    [
        [],
        [_text_answer(FIRST_NAME_QID, "Lead")],
        [_choice_answer(EMAIL_QID)],
        [{"questionId": EMAIL_QID, "answerDetails": {"textQuestionAnswer": {}}}],
        [_text_answer(EMAIL_QID, "lead at example dot com")],
        [_text_answer(EMAIL_QID, "lead@"), _text_answer(WORK_EMAIL_QID, "@example.com")],
    ],
)
def test_parse_lead_without_a_usable_email_answer(answers: list[dict[str, Any]]) -> None:
    lead = parse_lead(_lead(answers), {})
    assert lead is not None
    assert lead.email_status == "no_email"
    assert lead.email_reason == REASON_NO_EMAIL_ANSWER
    assert lead.email_sha256 is None
    assert lead.version_map is not None


def test_parse_lead_never_reads_consent_responses() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    element["formResponse"]["consentResponses"] = [{"consentId": 990, "accepted": False}]
    lead = parse_lead(_conforming("lead_form_responses", element), {})
    assert lead is not None
    assert lead.email_status == "ok"


# ---------------------------------------------------------------------------------------------
# Lead responses: test leads, instants, owner, campaign and creative links
# ---------------------------------------------------------------------------------------------


def test_parse_lead_drops_test_leads() -> None:
    sample = _sample_element("lead_form_responses")
    sample["testLead"] = True
    assert parse_lead(_conforming("lead_form_responses", sample), {}) is None
    assert parse_lead(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)], test_lead=True), {}) is None


def test_parse_lead_checks_test_lead_before_anything_else() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], test_lead=True)
    element["id"] = " "
    del element["versionedLeadGenFormUrn"]
    del element["leadMetadataInfo"]
    assert parse_lead(element, {}) is None


def test_parse_lead_submitted_at_is_a_utc_instant() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    element["submittedAt"] = 1_700_000_000_123
    lead = parse_lead(_conforming("lead_form_responses", element), {})
    assert lead is not None
    assert lead.submitted_at == datetime(2023, 11, 14, 22, 13, 20, 123000, tzinfo=UTC)
    assert lead.submitted_at.tzinfo is UTC
    assert lead.submitted_at.utcoffset() == timedelta(0)
    element["submittedAt"] = 0
    epoch = parse_lead(_conforming("lead_form_responses", element), {})
    assert epoch is not None
    assert epoch.submitted_at == datetime(1970, 1, 1, tzinfo=UTC)


def test_parse_lead_form_identity_comes_only_from_the_versioned_urn() -> None:
    # form.id is never read: the decoration carries no version, and the lead_forms sample
    # shows the id as a rounded JSON number.
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    element["form"]["id"] = 6755260984438374400
    lead = parse_lead(_conforming("lead_form_responses", element), {})
    assert lead is not None
    assert lead.lead_form_urn == FORM_URN
    assert lead.lead_form_version == FORM_VERSION
    assert lead.version_map is not None
    assert lead.version_map.canonical_urn == FORM_URN
    assert lead.version_map.version == FORM_VERSION


def test_parse_lead_owner_without_a_sponsored_account() -> None:
    organization = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    organization["owner"] = {"organization": "urn:li:organization:5509810"}
    lead = parse_lead(_conforming("lead_form_responses", organization), {})
    assert lead is not None
    assert lead.account_urn is None


def test_parse_lead_reads_the_unprojected_links() -> None:
    # The documented sample's unprojected paths alone, as an element without the decoration.
    element = _sample_element("lead_form_responses")
    del element["leadMetadataInfo"]
    del element["associatedEntityInfo"]
    lead = parse_lead(_conforming("lead_form_responses", element), {})
    assert lead is not None
    assert lead.campaign_urn == "urn:li:sponsoredCampaign:367378525"
    assert lead.creative_urn == "urn:li:sponsoredCreative:121608865"


def test_parse_lead_reads_the_projected_links() -> None:
    lead = parse_lead(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)]), {})
    assert lead is not None
    assert lead.campaign_urn == CAMPAIGN_URN
    assert lead.creative_urn == CREATIVE_URN


def test_parse_lead_falls_back_when_a_projected_link_has_no_id() -> None:
    element = _sample_element("lead_form_responses")
    del element["leadMetadataInfo"]["sponsoredLeadMetadataInfo"]["campaign"]["id"]
    element["associatedEntityInfo"] = {}
    lead = parse_lead(_conforming("lead_form_responses", element), {})
    assert lead is not None
    assert lead.campaign_urn == "urn:li:sponsoredCampaign:367378525"
    assert lead.creative_urn == "urn:li:sponsoredCreative:121608865"


def test_parse_lead_unknown_creative_is_none() -> None:
    null_creative = _sample_element("lead_form_responses")
    del null_creative["associatedEntityInfo"]
    null_creative["associatedEntity"]["associatedCreative"] = None
    lead = parse_lead(_conforming("lead_form_responses", null_creative), {})
    assert lead is not None
    assert lead.creative_urn is None
    no_creative = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    del no_creative["associatedEntityInfo"]
    lead = parse_lead(no_creative, {})
    assert lead is not None
    assert lead.creative_urn is None


def test_parse_lead_rejects_disagreeing_campaign_links() -> None:
    element = _sample_element("lead_form_responses")
    element["leadMetadata"]["sponsoredLeadMetadata"]["campaign"] = OTHER_CAMPAIGN_URN
    _expect_defect(
        lambda: parse_lead(element, {}),
        "lead_form_responses",
        "the projected and unprojected campaign links disagree",
    )


def test_parse_lead_rejects_disagreeing_creative_links() -> None:
    element = _sample_element("lead_form_responses")
    element["associatedEntity"]["associatedCreative"] = OTHER_CREATIVE_URN
    _expect_defect(
        lambda: parse_lead(element, {}),
        "lead_form_responses",
        "the projected and unprojected creative links disagree",
    )


def test_parse_lead_requires_a_campaign() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    del element["leadMetadataInfo"]
    _expect_defect(
        lambda: parse_lead(element, {}),
        "lead_form_responses",
        "required field 'campaign' is missing from a sponsored lead",
    )


def _mutate(path: Sequence[str], value: object) -> dict[str, Any]:
    """A decorated lead element with the value at ``path`` replaced (``None`` deletes it)."""
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    node: Any = element
    for key in path[:-1]:
        node = node[key]
    if value is None:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return element


LEAD_DEFECTS = [
    (("testLead",), None, "required field 'testLead' is missing"),
    (("testLead",), "false", "field 'testLead' must be a boolean"),
    (("id",), None, "required field 'id' is missing"),
    (("id",), 5, "field 'id' must be a string"),
    (("id",), "  ", "field 'id' must not be blank"),
    (("submittedAt",), None, "required field 'submittedAt' is missing"),
    (("submittedAt",), -1, "field 'submittedAt' must not be negative"),
    (("submittedAt",), "2023-06-07", "field 'submittedAt' must be an integer"),
    (("submittedAt",), 10**16, "field 'submittedAt' is outside the representable date range"),
    (("owner",), "urn:li:sponsoredAccount:1", "field 'owner' must be a JSON object"),
    (("owner", "sponsoredAccount"), "urn:li:organization:1", "field 'sponsoredAccount' must be"),
    (
        ("leadMetadataInfo", "sponsoredLeadMetadataInfo", "campaign", "id"),
        CREATIVE_URN,
        "field 'id' must be a urn:li:sponsoredCampaign:{id} URN",
    ),
    (
        ("leadMetadataInfo", "sponsoredLeadMetadataInfo"),
        "campaign",
        "field 'sponsoredLeadMetadataInfo' must be a JSON object",
    ),
    (
        ("associatedEntityInfo", "associatedCreative", "id"),
        CAMPAIGN_URN,
        "field 'id' must be a urn:li:sponsoredCreative:{id} URN",
    ),
    (("associatedEntity",), "creative", "field 'associatedEntity' must be a JSON object"),
    (("versionedLeadGenFormUrn",), None, "required field 'versionedLeadGenFormUrn' is missing"),
    (("versionedLeadGenFormUrn",), FORM_URN, "a versioned lead-form URN is malformed"),
    (("formResponse",), None, "required field 'formResponse' is missing"),
    (("formResponse", "answers"), None, "required field 'answers' is missing"),
    (("formResponse", "answers"), {"20": RAW_EMAIL}, "field 'answers' must be a JSON array"),
    (("formResponse", "answers"), [RAW_EMAIL], "an answer must be a JSON object"),
    (("formResponse", "answers"), [{"answerDetails": {}}], "required field 'questionId'"),
    (("form",), "decoration", "field 'form' must be a JSON object"),
    (("form", "content"), None, "required field 'content' is missing"),
    (
        ("formResponse", "answers"),
        [{"questionId": EMAIL_QID, "answerDetails": {"textQuestionAnswer": {"answer": [EMAIL]}}}],
        "field 'answer' must be a string",
    ),
    (
        ("formResponse", "answers"),
        [{"questionId": EMAIL_QID, "answerDetails": {"textQuestionAnswer": RAW_EMAIL}}],
        "field 'textQuestionAnswer' must be a JSON object",
    ),
    (
        ("formResponse", "answers"),
        [{"questionId": EMAIL_QID, "answerDetails": RAW_EMAIL}],
        "field 'answerDetails' must be a JSON object",
    ),
]


@pytest.mark.parametrize(("path", "value", "fragment"), LEAD_DEFECTS)
def test_parse_lead_rejects_malformed_fields(
    path: tuple[str, ...], value: object, fragment: str
) -> None:
    element = _mutate(path, value)
    error = _expect_defect(lambda: parse_lead(element, {}), "lead_form_responses", fragment)
    # Defect messages name fields only, never a lead's answers or email.
    for text in (str(error), error.message, error.reason, repr(error.args)):
        assert "lead.one" not in text.lower()
        assert "example.com" not in text.lower()


@pytest.mark.parametrize(("path", "value", "fragment"), LEAD_DEFECTS)
def test_lead_defects_on_the_batch_surface(
    path: tuple[str, ...], value: object, fragment: str
) -> None:
    element = _mutate(path, value)
    _expect_defect(
        lambda: parse_lead(element, {}, surface="lead_form_responses_batch"),
        "lead_form_responses_batch",
        fragment,
    )


def test_defect_messages_never_carry_free_text_answers() -> None:
    element = _sample_element("lead_form_responses")
    element["formResponse"]["answers"].append({"questionId": "three", "answerDetails": {}})
    error = _expect_defect(
        lambda: parse_lead(element, {}),
        "lead_form_responses",
        "field 'questionId' must be an integer",
    )
    assert SAMPLE_ANSWER not in str(error)
    assert SAMPLE_ANSWER not in error.reason


@pytest.mark.parametrize("element", [[], "lead", None, 5])
def test_parse_lead_rejects_a_non_object(element: object) -> None:
    _expect_defect(
        lambda: parse_lead(element, {}),
        "lead_form_responses",
        "a lead form response must be a JSON object",
    )


@pytest.mark.parametrize("surface", ["lead_forms", "leadFormResponses", "", "posts"])
def test_parse_lead_and_decode_email_reject_other_surfaces(surface: str) -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    with pytest.raises(ValueError, match=r"surface must be lead_form_responses or"):
        parse_lead(element, {}, surface=surface)
    with pytest.raises(ValueError, match=r"surface must be lead_form_responses or"):
        decode_email(element, {}, surface=surface)


# ---------------------------------------------------------------------------------------------
# Storage-bound text: a NUL or an unencodable string is a defect, valid text is unchanged
# ---------------------------------------------------------------------------------------------
#
# JSON escapes decode to strings that PostgreSQL text cannot store: an escaped NUL to "A\x00B",
# and an escaped unpaired surrogate to "A\ud800B", which UTF-8 cannot encode. The parser rejects
# both before a row reaches the database driver. They are deliberately malformed inputs, decoded
# the way connectors/http.py decodes JSON.

NUL_TEXT: str = json.loads('"A\\u0000B"')
SURROGATE_TEXT: str = json.loads('"A\\ud800B"')
UNSTORABLE_TEXTS = [
    pytest.param(NUL_TEXT, "must not contain a NUL character", id="nul"),
    pytest.param(SURROGATE_TEXT, "must be valid UTF-8 text", id="surrogate"),
]
# Valid text, non-ASCII characters, an emoji and inner whitespace included.
STORABLE_TEXTS = ["Caf\u00e9 \u2013 Q4 \U0001f680", "Z\u00fcrich\tleads \u65e5\u672c\nQ4"]


def _assert_text_not_echoed(error: QueryDefect, text: str) -> None:
    """Assert the defect names the field only: no raw or escaped ``text`` and no chained error.

    A chained ``UnicodeEncodeError`` would carry the raw string in its ``object`` attribute.
    """
    _assert_not_echoed(error, text, ascii(text)[1:-1], "\x00", "\ud800", "\\x00", "\\ud800")
    assert error.__cause__ is None
    assert error.__context__ is None


def _account_with(key: str, text: str) -> AdAccountRow:
    """Parse the documented ad account with ``key`` set to ``text``."""
    element = _sample("ad_account")
    element[key] = text
    return parse_ad_account(element)


def _with_value(surface: str, key: str, value: object) -> Any:
    """The first documented element of ``surface`` with ``key`` set to ``value``.

    Used for fields the contract declares as an enum, so the element is not checked against it.
    """
    element: dict[str, Any] = _sample_element(surface)
    element[key] = value
    return element


def _creative_with_label(text: str) -> CreativeRow:
    """Parse a lead-gen creative whose call-to-action label (an enum) is ``text``."""
    cta = {"destination": "urn:li:adForm:990002001", "label": text}
    return parse_creative(_with_value("creatives", "leadgenCallToAction", cta))


def _lead_form_with_name(text: str) -> LeadFormRow:
    """Parse the documented lead form named ``text``."""
    element: dict[str, Any] = _sample_element("lead_forms")
    element["name"] = text
    return parse_lead_form(_conforming("lead_forms", element))


def _lead_with_id(surface: str, text: str) -> ParsedLead | None:
    """Parse a decorated lead whose response ``id`` is ``text``, on ``surface``."""
    return parse_lead(_mutate(("id",), text), {}, surface=surface)


# Every text field a row hands to storage: (surface, field, parse an element carrying the text,
# read the parsed text back). ``read`` is None for a field the contract declares as an enum.
STORED_TEXT_FIELDS = [
    pytest.param(
        "ad_account",
        "name",
        functools.partial(_account_with, "name"),
        lambda row: row.name,
        id="ad_account.name",
    ),
    pytest.param(
        "ad_account",
        "status",
        functools.partial(_account_with, "status"),
        None,
        id="ad_account.status",
    ),
    pytest.param(
        "ad_campaign_groups",
        "name",
        lambda text: parse_campaign_group(_group(name=text)),
        lambda row: row.name,
        id="ad_campaign_groups.name",
    ),
    pytest.param(
        "ad_campaign_groups",
        "status",
        lambda text: parse_campaign_group(_with_value("ad_campaign_groups", "status", text)),
        None,
        id="ad_campaign_groups.status",
    ),
    pytest.param(
        "ad_campaigns",
        "name",
        lambda text: parse_campaign(_campaign(name=text)),
        lambda row: row.name,
        id="ad_campaigns.name",
    ),
    pytest.param(
        "ad_campaigns",
        "status",
        lambda text: parse_campaign(_with_value("ad_campaigns", "status", text)),
        None,
        id="ad_campaigns.status",
    ),
    pytest.param(
        "ad_campaigns",
        "objectiveType",
        lambda text: parse_campaign(_campaign(objectiveType=text)),
        lambda row: row.objective_type,
        id="ad_campaigns.objectiveType",
    ),
    pytest.param(
        "creatives",
        "name",
        lambda text: parse_creative(_creative(name=text)),
        lambda row: row.name,
        id="creatives.name",
    ),
    pytest.param(
        "creatives",
        "intendedStatus",
        lambda text: parse_creative(_with_value("creatives", "intendedStatus", text)),
        None,
        id="creatives.intendedStatus",
    ),
    pytest.param(
        "creatives",
        "label",
        _creative_with_label,
        None,
        id="creatives.leadgenCallToAction.label",
    ),
    pytest.param(
        "posts",
        "commentary",
        lambda text: parse_post("urn:li:share:1", _post(commentary=text)),
        lambda row: row.intro_text,
        id="posts.commentary",
    ),
    pytest.param(
        "posts",
        "title",
        lambda text: parse_post("urn:li:share:1", _post(content={"media": {"title": text}})),
        lambda row: row.headline,
        id="posts.media.title",
    ),
    pytest.param(
        "posts",
        "title",
        lambda text: parse_post("urn:li:share:1", _post(content={"article": {"title": text}})),
        lambda row: row.headline,
        id="posts.article.title",
    ),
    pytest.param(
        "lead_forms",
        "name",
        _lead_form_with_name,
        lambda row: row.name,
        id="lead_forms.name",
    ),
    pytest.param(
        "lead_form_responses",
        "id",
        functools.partial(_lead_with_id, "lead_form_responses"),
        lambda row: row.external_id,
        id="lead_form_responses.id",
    ),
    pytest.param(
        "lead_form_responses_batch",
        "id",
        functools.partial(_lead_with_id, "lead_form_responses_batch"),
        lambda row: row.external_id,
        id="lead_form_responses_batch.id",
    ),
]
FREE_TEXT_FIELDS = [field for field in STORED_TEXT_FIELDS if field.values[3] is not None]


@pytest.mark.parametrize(("text", "problem"), UNSTORABLE_TEXTS)
@pytest.mark.parametrize(("surface", "key", "parse", "read"), STORED_TEXT_FIELDS)
def test_stored_text_with_a_nul_or_an_unencodable_character_is_a_defect(
    surface: str,
    key: str,
    parse: Callable[[str], object],
    read: Callable[[Any], object] | None,
    text: str,
    problem: str,
) -> None:
    error = _expect_defect(lambda: parse(text), surface, f"field '{key}' {problem}")
    assert error.message == f"{surface}: field '{key}' {problem}"
    _assert_text_not_echoed(error, text)
    _assert_not_echoed(error, "lead.one", "example.com")


@pytest.mark.parametrize("text", STORABLE_TEXTS)
@pytest.mark.parametrize(("surface", "key", "parse", "read"), FREE_TEXT_FIELDS)
def test_stored_text_that_utf8_encodes_is_returned_unchanged(
    surface: str,
    key: str,
    parse: Callable[[str], object],
    read: Callable[[Any], object],
    text: str,
) -> None:
    row = parse(text)
    assert row is not None, (surface, key)
    assert read(row) == text


@pytest.mark.parametrize(("text", "problem"), UNSTORABLE_TEXTS)
def test_a_predefined_field_with_a_nul_or_an_unencodable_character_is_a_defect(
    text: str, problem: str
) -> None:
    # predefinedField is compared, never stored, but a NUL or a lone surrogate is no documented
    # field name: the decoration breaks its contract, so the lead is a defect, not no_email.
    questions = [*_standard_questions(), _question(UNDECORATED_QID, f"EMAIL{text}")]
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], questions=questions)
    fragment = f"field 'predefinedField' {problem}"
    for call, surface in (
        (lambda: parse_lead(element, {}), "lead_form_responses"),
        (lambda: decode_email(element, {}), "lead_form_responses_batch"),
    ):
        error = _expect_defect(call, surface, fragment)
        _assert_text_not_echoed(error, text)
        _assert_not_echoed(error, "lead.one", "example.com")
    form: dict[str, Any] = _sample_element("lead_forms")
    form["content"]["questions"].append(_question(UNDECORATED_QID, f"EMAIL{text}"))
    error = _expect_defect(lambda: parse_lead_form(form), "lead_forms", fragment)
    _assert_text_not_echoed(error, text)


NUL_EMAIL: str = json.loads('"lead\\u0000.one@example.com"')
NUL_EMAIL_SHA256 = hashlib.sha256(NUL_EMAIL.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # UTF-8 encodes a NUL, so the address is decoded and hashed: the answer is transient,
        # never stored, and only its SHA-256 reaches the lead row.
        pytest.param(
            NUL_EMAIL,
            DecodedEmail(email=NUL_EMAIL, sha256=NUL_EMAIL_SHA256, status="ok", reason=None),
            id="nul-address",
        ),
        pytest.param(
            NUL_TEXT,
            DecodedEmail(email=None, sha256=None, status="no_email", reason=REASON_NO_EMAIL_ANSWER),
            id="nul-text",
        ),
        # UTF-8 cannot encode a lone surrogate, so the answer is no address, never a defect.
        pytest.param(
            SURROGATE_EMAIL,
            DecodedEmail(email=None, sha256=None, status="no_email", reason=REASON_NO_EMAIL_ANSWER),
            id="surrogate-address",
        ),
        pytest.param(
            SURROGATE_TEXT,
            DecodedEmail(email=None, sha256=None, status="no_email", reason=REASON_NO_EMAIL_ANSWER),
            id="surrogate-text",
        ),
    ],
)
@pytest.mark.parametrize("surface", ["lead_form_responses", "lead_form_responses_batch"])
def test_email_answers_are_decoded_without_the_storage_text_checks(
    surface: str, answer: str, expected: DecodedEmail
) -> None:
    element = _lead([_text_answer(EMAIL_QID, answer)])
    assert decode_email(element, {}, surface=surface) == expected
    lead = parse_lead(element, {}, surface=surface)
    assert lead is not None
    assert lead.email_status == expected.status
    assert lead.email_reason == expected.reason
    assert lead.email_sha256 == expected.sha256
    assert lead.version_map is not None  # the decoration passed the check and is still learned
    for text in (repr(lead), str(dataclasses.asdict(lead))):
        assert "example.com" not in text


# ---------------------------------------------------------------------------------------------
# Personal data: no plaintext outside decode_email
# ---------------------------------------------------------------------------------------------


def test_parsed_lead_has_no_field_that_can_hold_an_email() -> None:
    assert {item.name for item in dataclasses.fields(ParsedLead)} == {
        "external_id",
        "submitted_at",
        "account_urn",
        "campaign_urn",
        "creative_urn",
        "lead_form_urn",
        "lead_form_version",
        "email_sha256",
        "email_status",
        "email_reason",
        "version_map",
    }
    lead = parse_lead(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)]), {})
    assert lead is not None
    assert not hasattr(lead, "__dict__")  # slots: nothing can be attached to a stored row
    for text in (repr(lead), str(dataclasses.asdict(lead))):
        assert EMAIL not in text.lower()
        assert RAW_EMAIL.strip() not in text
        assert "example.com" not in text.lower()
        assert EMAIL_SHA256 in text


def test_decode_email_returns_the_plaintext_with_the_stored_hash() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)])
    decoded = decode_email(element, {})
    assert decoded == DecodedEmail(email=EMAIL, sha256=EMAIL_SHA256, status="ok", reason=None)
    lead = parse_lead(element, {})
    assert lead is not None
    assert decoded.sha256 == lead.email_sha256
    # The page surface decodes identically.
    assert decode_email(element, {}, surface="lead_form_responses") == decoded


def test_decode_email_repr_never_shows_the_address() -> None:
    decoded = decode_email(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)]), {})
    assert decoded.email == EMAIL
    assert EMAIL not in repr(decoded)
    assert "example.com" not in repr(decoded)
    assert EMAIL_SHA256 in repr(decoded)


def test_decode_email_from_a_stored_map_matches_parse_lead() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], decorated=False)
    decoded = decode_email(element, _stored())
    lead = parse_lead(element, _stored())
    assert lead is not None
    assert decoded.email == EMAIL
    assert decoded.sha256 == lead.email_sha256 == EMAIL_SHA256


@pytest.mark.parametrize(
    ("known", "reason"),
    [({}, REASON_UNKNOWN_FORM_VERSION), (_stored(email_ids=()), REASON_NO_EMAIL_QUESTION)],
)
def test_decode_email_reports_no_email(
    known: dict[tuple[str, int], FormVersionMap], reason: str
) -> None:
    decoded = decode_email(_lead([_text_answer(EMAIL_QID, RAW_EMAIL)], decorated=False), known)
    assert decoded == DecodedEmail(email=None, sha256=None, status="no_email", reason=reason)


def test_decode_email_defects_name_the_batch_surface_by_default() -> None:
    element = _lead([_text_answer(EMAIL_QID, RAW_EMAIL)], versioned_urn=SAMPLE_FORM_URN)
    error = _expect_defect(
        lambda: decode_email(element, {}),
        "lead_form_responses_batch",
        "a versioned lead-form URN is malformed",
    )
    assert "example.com" not in str(error).lower()
    _expect_defect(
        lambda: decode_email([], {}),
        "lead_form_responses_batch",
        "a lead form response must be a JSON object",
    )
