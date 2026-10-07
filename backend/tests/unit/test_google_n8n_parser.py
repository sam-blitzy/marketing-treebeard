"""Unit tests for ``ads_engine.connectors.google_n8n.parser``: REST ``GoogleAdsRow`` parsing.

Fixtures are traceable to ``backend/tests/contracts/google/*.json``. The contracts are read with
``json`` exactly as ``connectors/http.py`` decodes a webhook body (``parse_float=Decimal``), and
every hand-built row uses only fields its contract defines: camelCase REST keys, INT64 values as
decimal strings, DOUBLE values as the ``Decimal`` or ``int`` a JSON number decodes to.
:func:`_parse` checks each row against its contract's ``response.body`` schema before parsing it,
so a fixture that carries an undefined field, a wrong type or an off-enum value fails. Two kinds
of row are exempt and named as such: rows the parser tolerates although the contract requires
the field (:func:`_parse_off_contract`), and deliberately malformed rows for defect branches.
Each contract's ``response.documented_sample`` is parsed verbatim when the contract publishes
one.

Everything here is pure and synchronous: no database and no network.
"""

import copy
import dataclasses
import functools
import importlib.util
import json
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.google_n8n import parser, queries
from ads_engine.connectors.google_n8n.parser import (
    CAMPAIGN_STATUS_MAP,
    PARSERS,
    SEARCH_TERM_STATUSES,
    AdGroupRow,
    CampaignDailyRow,
    CampaignRow,
    ConversionActionRow,
    CustomerRow,
    KeywordRow,
    KeywordViewRow,
    SearchTermRow,
    get_field,
    parse_ad_group,
    parse_campaign,
    parse_campaign_daily,
    parse_conversion_action,
    parse_customer,
    parse_keyword,
    parse_keyword_view,
    parse_search_term,
)
from ads_engine.connectors.google_n8n.queries import INT64_MAX, QUERIES, rest_path

pytestmark = pytest.mark.contract

CONTRACTS_DIR = Path(__file__).parents[1] / "contracts" / "google"
ROW_SURFACES = tuple(sorted({template.row_surface for template in QUERIES.values()}))

ROW_TYPES: Mapping[str, type] = {
    "row_customer": CustomerRow,
    "row_campaign": CampaignRow,
    "row_campaign_daily": CampaignDailyRow,
    "row_ad_group": AdGroupRow,
    "row_keyword_view": KeywordViewRow,
    "row_search_term_view": SearchTermRow,
    "row_ad_group_criterion": KeywordRow,
    "row_conversion_action": ConversionActionRow,
}

# Query id a defect names: row_campaign serves both Q-G2 and Q-G2b, so its defects name none.
QUERY_ID_OF_SURFACE: Mapping[str, str | None] = {
    "row_customer": "Q-G1",
    "row_campaign": None,
    "row_campaign_daily": "Q-G3",
    "row_ad_group": "Q-G4",
    "row_keyword_view": "Q-G5",
    "row_search_term_view": "Q-G6",
    "row_ad_group_criterion": "Q-G7",
    "row_conversion_action": "Q-G8",
}

INT64_MAX_TEXT = str(INT64_MAX)
INT64_OVERFLOW_TEXT = str(INT64_MAX + 1)
INT64_RANGE = f"an integer from 0 to {INT64_MAX}"
NOT_INT64 = "an INT64 decimal string or a JSON integer"
NOT_NUMBER = "a finite JSON number"
NOT_DATE = "a YYYY-MM-DD date"
NOT_OBJECT_PATH = "a JSON object at every level of its key path"
NOT_NULL_PATH = "a non-null value at every level of its key path"
NOT_NON_NEGATIVE = "a non-negative finite JSON number"
NOT_FRACTION = "a fraction from 0 to 1"
# numeric(18, 6), the column of every Decimal row field, holds at most 999999999999.999999.
NUMERIC_MAX_TEXT = "999999999999.999999"
AMOUNT_MICROS_MAX_TEXT = "999999999999999999"
AMOUNT_MICROS_RANGE = f"an integer from 0 to {AMOUNT_MICROS_MAX_TEXT}"
NUMERIC_RANGE = f"a value of at most {NUMERIC_MAX_TEXT}, the numeric(18, 6) range"
NUMERIC_SCALE = "a value with at most six decimal places, which numeric(18, 6) stores exactly"
FRACTION_POSITIVE = (
    "zero or a fraction of at least 0.000001, which six decimal places keep above zero"
)
CPC_RANGE = (
    "an amount in micros that, divided by 1000000 and rounded half-even to six decimal places, "
    f"is at most {NUMERIC_MAX_TEXT}, the numeric(18, 6) range"
)
CPC_POSITIVE = "zero or a value above 0.5 micros, which six decimal places keep above zero"
CRITERION_FORM = (
    "an adGroupCriteria resource name customers/{customer}/adGroupCriteria/{ad_group}~{criterion}"
)

# Identifiers of the hand-built rows; resource names follow the forms the contracts document.
CUSTOMER = "1234567890"
CAMPAIGN = "111"
BUDGET = "222"
AD_GROUP = "444"
CRITERION = "555"
ACTION = "666"
CUSTOMER_RN = f"customers/{CUSTOMER}"
CAMPAIGN_RN = f"{CUSTOMER_RN}/campaigns/{CAMPAIGN}"
BUDGET_RN = f"{CUSTOMER_RN}/campaignBudgets/{BUDGET}"
AD_GROUP_RN = f"{CUSTOMER_RN}/adGroups/{AD_GROUP}"
CRITERION_RN = f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~{CRITERION}"


# --- contracts and the conformance check -----------------------------------------------------


@functools.cache
def _contract(surface: str) -> dict[str, Any]:
    """Load ``contracts/google/{surface}.json`` with JSON numbers decoded as on the wire."""
    text = (CONTRACTS_DIR / f"{surface}.json").read_text(encoding="utf-8")
    contract: dict[str, Any] = json.loads(text, parse_float=Decimal)
    return contract


def _body_schema(surface: str) -> Mapping[str, Any]:
    """Return the ``response.body`` schema of one row contract."""
    schema: Mapping[str, Any] = _contract(surface)["response"]["body"]
    return schema


def _enum(surface: str, rest_keys: str) -> frozenset[str]:
    """Return the ``enum`` of the property at the dotted REST key path of one row contract."""
    node = _body_schema(surface)
    for key in rest_keys.split("."):
        node = node["properties"][key]
    return frozenset(node["enum"])


_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    # JSON numbers decode to int or Decimal (parse_float=Decimal); a float never comes off the
    # wire, so a float in a fixture is off-contract.
    "number": lambda value: isinstance(value, int | Decimal) and not isinstance(value, bool),
    "null": lambda value: value is None,
}
_SCHEMA_KEYWORDS = frozenset(
    {"type", "description", "properties", "required", "additionalProperties", "enum", "items"}
)


def _violations(value: object, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """List every way ``value`` departs from a contract schema, or ``[]`` when it conforms.

    Only the JSON Schema keywords the Google contracts use are understood; any other keyword
    fails the test, so the check can never pass by ignoring a constraint.
    """
    unknown = set(schema) - _SCHEMA_KEYWORDS
    if unknown:
        pytest.fail(f"{path}: contract keywords {sorted(unknown)} are not checked here")
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_JSON_TYPES[name](value) for name in names):
            return [f"{path}: expected {' or '.join(names)}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: not one of the contract's enum values")
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties", {})
        found.extend(
            f"{path}.{key}: required" for key in schema.get("required", ()) if key not in value
        )
        for key, item in value.items():
            if key in properties:
                found.extend(_violations(item, properties[key], f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                found.append(f"{path}.{key}: not defined by the contract")
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            found.extend(_violations(item, schema["items"], f"{path}[{index}]"))
    return found


def _parse(surface: str, row: Mapping[str, Any]) -> Any:
    """Assert that ``row`` conforms to its contract, then parse it with ``PARSERS[surface]``."""
    assert _violations(row, _body_schema(surface)) == []
    parsed = PARSERS[surface](row)
    assert isinstance(parsed, ROW_TYPES[surface])
    return parsed


def _parse_off_contract(surface: str, row: Mapping[str, Any]) -> Any:
    """Parse a row the parser tolerates although it departs from its contract.

    The departure is asserted, so a row named off-contract cannot silently conform.
    """
    assert _violations(row, _body_schema(surface)) != []
    parsed = PARSERS[surface](row)
    assert isinstance(parsed, ROW_TYPES[surface])
    return parsed


def _defect(surface: str, row: Any) -> QueryDefect:
    """Parse a malformed row and return the validation defect it raises."""
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](row)
    return caught.value


def _assert_defect(error: QueryDefect, surface: str, message: str) -> None:
    """Assert the defect's message and the fixed metadata every parse defect carries."""
    assert error.message == message
    assert error.source == "google"
    assert error.endpoint == f"parse {surface}"
    assert error.code == "validation"
    assert error.kind == "defect"
    assert error.query_id == QUERY_ID_OF_SURFACE[surface]


def _with(row: Mapping[str, Any], rest_keys: str, value: object) -> dict[str, Any]:
    """Return a deep copy of ``row`` with the dotted REST key path set to ``value``."""
    result = copy.deepcopy(dict(row))
    *parents, leaf = rest_keys.split(".")
    node = result
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value
    return result


def _without(row: Mapping[str, Any], *rest_keys: str) -> dict[str, Any]:
    """Return a deep copy of ``row`` without each dotted REST key path."""
    result = copy.deepcopy(dict(row))
    for path in rest_keys:
        *parents, leaf = path.split(".")
        node = result
        for key in parents:
            node = node[key]
        del node[leaf]
    return result


# --- contract-conformant base rows, one per surface ------------------------------------------


def customer_row() -> dict[str, Any]:
    """A Q-G1 row (row_customer.json)."""
    return {
        "customer": {
            "resourceName": CUSTOMER_RN,
            "id": CUSTOMER,
            "descriptiveName": "Example Advertiser",
            "currencyCode": "USD",
            "timeZone": "America/New_York",
        }
    }


def campaign_row() -> dict[str, Any]:
    """A Q-G2 row with a DAILY budget (row_campaign.json)."""
    return {
        "campaign": {
            "resourceName": CAMPAIGN_RN,
            "id": CAMPAIGN,
            "name": "Brand - Search",
            "status": "ENABLED",
            "advertisingChannelType": "SEARCH",
        },
        "campaignBudget": {
            "resourceName": BUDGET_RN,
            "id": BUDGET,
            "amountMicros": "50000000",
            "period": "DAILY",
            "explicitlyShared": False,
        },
    }


def lifetime_campaign_row() -> dict[str, Any]:
    """A Q-G2 row with a CUSTOM_PERIOD budget, which cannot be explicitly shared."""
    return {
        "campaign": {
            "resourceName": CAMPAIGN_RN,
            "id": CAMPAIGN,
            "name": "Launch - Display",
            "status": "PAUSED",
            "advertisingChannelType": "DISPLAY",
        },
        "campaignBudget": {
            "resourceName": BUDGET_RN,
            "id": BUDGET,
            "totalAmountMicros": "1500000000",
            "period": "CUSTOM_PERIOD",
        },
    }


def campaign_daily_row() -> dict[str, Any]:
    """A Q-G3 row with every metric present (row_campaign_daily.json)."""
    return {
        "campaign": {"resourceName": CAMPAIGN_RN, "id": CAMPAIGN},
        "segments": {"date": "2026-09-01"},
        "metrics": {
            "costMicros": "12345678",
            "impressions": "1000",
            "clicks": "50",
            "conversions": Decimal("2.5"),
            "conversionsValue": Decimal("125.75"),
            "searchBudgetLostImpressionShare": Decimal("0.9001"),
            "searchImpressionShare": Decimal("0.0999"),
        },
    }


def ad_group_row() -> dict[str, Any]:
    """A Q-G4 row (row_ad_group.json)."""
    return {
        "adGroup": {
            "resourceName": AD_GROUP_RN,
            "id": AD_GROUP,
            "name": "CRM software",
            "status": "PAUSED",
        },
        "campaign": {"resourceName": CAMPAIGN_RN, "id": CAMPAIGN},
    }


def keyword_view_row() -> dict[str, Any]:
    """A Q-G5 row; averageCpc equals costMicros / clicks (row_keyword_view.json)."""
    return {
        "keywordView": {"resourceName": f"{CUSTOMER_RN}/keywordViews/{AD_GROUP}~{CRITERION}"},
        "adGroupCriterion": {"resourceName": CRITERION_RN, "criterionId": CRITERION},
        "adGroup": {"resourceName": AD_GROUP_RN, "id": AD_GROUP},
        "segments": {"date": "2026-09-02"},
        "metrics": {
            "historicalQualityScore": "7",
            "averageCpc": Decimal("1234567.5"),
            "costMicros": "24691350",
            "clicks": "20",
            "impressions": "400",
            "conversions": Decimal("1.5"),
        },
    }


def search_term_row() -> dict[str, Any]:
    """A Q-G6 row that matched a keyword criterion (row_search_term_view.json)."""
    return {
        "searchTermView": {
            "resourceName": f"{CUSTOMER_RN}/searchTermViews/{CAMPAIGN}~{AD_GROUP}~Y3JtIHByaWNpbmc",
            "searchTerm": "crm pricing",
            "status": "NONE",
        },
        "segments": {
            "date": "2026-09-03",
            "keyword": {
                "adGroupCriterion": CRITERION_RN,
                "info": {"text": "crm software", "matchType": "PHRASE"},
            },
        },
        "adGroup": {"resourceName": AD_GROUP_RN, "id": AD_GROUP},
        "campaign": {"resourceName": CAMPAIGN_RN, "id": CAMPAIGN},
        "metrics": {
            "costMicros": "3500000",
            "clicks": "7",
            "impressions": "90",
            "conversions": Decimal("0.3"),
            "conversionsValue": 15,
        },
    }


def keyword_row() -> dict[str, Any]:
    """A Q-G7 row with a populated quality score (row_ad_group_criterion.json)."""
    return {
        "adGroupCriterion": {
            "resourceName": CRITERION_RN,
            "criterionId": CRITERION,
            "status": "ENABLED",
            "keyword": {"text": "crm software", "matchType": "EXACT"},
            "qualityInfo": {"qualityScore": 8},
        },
        "adGroup": {"resourceName": AD_GROUP_RN, "id": AD_GROUP},
        "campaign": {"resourceName": CAMPAIGN_RN, "id": CAMPAIGN},
    }


def conversion_action_row() -> dict[str, Any]:
    """A Q-G8 row with a last-click counting contract (row_conversion_action.json)."""
    return {
        "conversionAction": {
            "resourceName": f"{CUSTOMER_RN}/conversionActions/{ACTION}",
            "id": ACTION,
            "name": "Demo request",
            "status": "ENABLED",
            "includeInConversionsMetric": True,
            "countingType": "ONE_PER_CLICK",
            "attributionModelSettings": {"attributionModel": "GOOGLE_ADS_LAST_CLICK"},
        }
    }


BASE_ROWS: Mapping[str, Callable[[], dict[str, Any]]] = {
    "row_customer": customer_row,
    "row_campaign": campaign_row,
    "row_campaign_daily": campaign_daily_row,
    "row_ad_group": ad_group_row,
    "row_keyword_view": keyword_view_row,
    "row_search_term_view": search_term_row,
    "row_ad_group_criterion": keyword_row,
    "row_conversion_action": conversion_action_row,
}

# The required INT64 id of each surface, as a GAQL field and its REST key path.
ID_FIELD_OF_SURFACE: Mapping[str, tuple[str, str]] = {
    "row_customer": ("customer.id", "customer.id"),
    "row_campaign": ("campaign.id", "campaign.id"),
    "row_campaign_daily": ("campaign.id", "campaign.id"),
    "row_ad_group": ("ad_group.id", "adGroup.id"),
    "row_keyword_view": ("ad_group_criterion.criterion_id", "adGroupCriterion.criterionId"),
    "row_search_term_view": ("campaign.id", "campaign.id"),
    "row_ad_group_criterion": ("campaign.id", "campaign.id"),
    "row_conversion_action": ("conversion_action.id", "conversionAction.id"),
}


# --- registry, contracts and documented samples ----------------------------------------------


def test_parsers_cover_exactly_the_row_surfaces_of_queries() -> None:
    assert set(PARSERS) == {template.row_surface for template in QUERIES.values()}
    assert set(PARSERS) == set(ROW_TYPES) == set(BASE_ROWS) == set(QUERY_ID_OF_SURFACE)
    assert {path.stem for path in CONTRACTS_DIR.glob("row_*.json")} == set(PARSERS)
    assert PARSERS["row_customer"] is parse_customer
    assert PARSERS["row_campaign"] is parse_campaign
    assert PARSERS["row_campaign_daily"] is parse_campaign_daily
    assert PARSERS["row_ad_group"] is parse_ad_group
    assert PARSERS["row_keyword_view"] is parse_keyword_view
    assert PARSERS["row_search_term_view"] is parse_search_term
    assert PARSERS["row_ad_group_criterion"] is parse_keyword
    assert PARSERS["row_conversion_action"] is parse_conversion_action


def test_parsers_is_read_only() -> None:
    writable: Any = PARSERS
    with pytest.raises(TypeError):
        writable["row_extra"] = parse_customer


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_contract_describes_the_rows_of_its_template(surface: str) -> None:
    contract = _contract(surface)
    gaql = contract["request"]["gaql"]
    template = QUERIES[gaql["query_id"]]
    assert contract["surface"] == f"google.{surface}"
    assert contract["source"] == "google"
    assert contract["api_version"] == "v25"
    assert contract["response"]["status"] == [200]
    assert template.row_surface == surface
    assert gaql["resource"] == template.resource
    assert tuple(gaql["select"]) == template.select


@pytest.mark.parametrize("query_id", sorted(QUERIES))
def test_every_selected_field_has_a_contract_key_path(query_id: str) -> None:
    template = QUERIES[query_id]
    for gaql_field in template.select:
        node = _body_schema(template.row_surface)
        for key in rest_path(gaql_field):
            assert key in node.get("properties", {}), f"{gaql_field}: no contract key {key}"
            node = node["properties"][key]


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_documented_sample_parses_verbatim(surface: str) -> None:
    sample = _contract(surface)["response"]["documented_sample"]
    if sample is None:
        pytest.skip(f"contracts/google/{surface}.json publishes no documented sample")
    _parse(surface, sample)


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_base_row_conforms_and_parses(surface: str) -> None:
    _parse(surface, BASE_ROWS[surface]())


def test_conformance_check_rejects_fields_the_contract_does_not_define() -> None:
    schema = _body_schema("row_campaign")
    assert _violations(_with(campaign_row(), "campaign.biddingStrategyType", "MANUAL_CPC"), schema)
    assert _violations(_with(campaign_row(), "campaign.id", 111), schema)
    assert _violations(_with(campaign_row(), "campaign.status", "ACTIVE"), schema)
    assert _violations(_without(campaign_row(), "campaign.name"), schema)
    daily_schema = _body_schema("row_campaign_daily")
    assert _violations(_with(campaign_daily_row(), "metrics.conversions", 0.3), daily_schema)
    assert _violations(_with(campaign_daily_row(), "metrics.conversions", "0.3"), daily_schema)
    assert _violations(_with(campaign_daily_row(), "metrics.clicks", 50), daily_schema)


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_parsed_rows_are_frozen(surface: str) -> None:
    parsed = _parse(surface, BASE_ROWS[surface]())
    field_name = dataclasses.fields(parsed)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(parsed, field_name, None)


def test_import_fails_when_a_template_names_a_surface_without_a_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orphan = dataclasses.replace(QUERIES["Q-G1"], query_id="Q-G9", row_surface="row_orphan")
    monkeypatch.setattr(queries, "QUERIES", MappingProxyType({**QUERIES, "Q-G9": orphan}))
    spec = importlib.util.spec_from_file_location("google_n8n_parser_probe", parser.__file__)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    with pytest.raises(RuntimeError, match="PARSERS must cover exactly the row surfaces"):
        spec.loader.exec_module(probe)


# --- defect metadata shared by every surface --------------------------------------------------


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_absent_required_id_is_a_validation_defect(surface: str) -> None:
    gaql_field, rest_keys = ID_FIELD_OF_SURFACE[surface]
    error = _defect(surface, _without(BASE_ROWS[surface](), rest_keys))
    _assert_defect(error, surface, f"{gaql_field}: expected an INT64 id")


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_row_that_is_not_an_object_is_a_defect(surface: str) -> None:
    error = _defect(surface, ["not", "an", "object"])
    assert error.message.endswith(f"expected {NOT_OBJECT_PATH}")
    assert error.endpoint == f"parse {surface}"
    assert error.query_id == QUERY_ID_OF_SURFACE[surface]


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_resource_that_is_not_an_object_is_a_defect(surface: str) -> None:
    gaql_field, rest_keys = ID_FIELD_OF_SURFACE[surface]
    resource = rest_keys.split(".")[0]
    error = _defect(surface, _with(BASE_ROWS[surface](), resource, "not an object"))
    assert error.message.startswith(gaql_field.split(".")[0])
    assert error.message.endswith(f"expected {NOT_OBJECT_PATH}")


def test_defect_messages_never_carry_row_values() -> None:
    row = _with(search_term_row(), "searchTermView.searchTerm", "zyxwv confidential term")
    error = _defect("row_search_term_view", _with(row, "searchTermView.status", "QWERTY"))
    assert "zyxwv" not in error.message
    assert "QWERTY" not in error.message
    assert "zyxwv" not in error.reason
    error = _defect("row_customer", _with(customer_row(), "customer.timeZone", "Qwerty/Zone"))
    assert "Qwerty" not in error.message
    error = _defect("row_campaign", _with(campaign_row(), "campaign.id", "98765abc"))
    assert "98765" not in error.message


# --- strings PostgreSQL text can store ---------------------------------------------------------
# PostgreSQL text cannot store U+0000 or an unpaired surrogate, so every string the parser reads
# (names, search terms, keyword texts, enum names, resource names) rejects both with a defect
# that names the field only, and keeps every other code point exactly as returned.

NOT_STORABLE_TEXT = "a string without U+0000 or unpaired surrogates (PostgreSQL text)"

# Every field the parser reads through _RowReader.text: surface, REST path, GAQL field.
STRING_FIELDS: list[tuple[str, str, str]] = [
    ("row_customer", "customer.descriptiveName", "customer.descriptive_name"),
    ("row_customer", "customer.currencyCode", "customer.currency_code"),
    ("row_customer", "customer.timeZone", "customer.time_zone"),
    ("row_campaign", "campaign.name", "campaign.name"),
    ("row_campaign", "campaign.status", "campaign.status"),
    ("row_campaign", "campaign.advertisingChannelType", "campaign.advertising_channel_type"),
    ("row_campaign", "campaignBudget.period", "campaign_budget.period"),
    ("row_ad_group", "adGroup.name", "ad_group.name"),
    ("row_ad_group", "adGroup.status", "ad_group.status"),
    ("row_search_term_view", "searchTermView.searchTerm", "search_term_view.search_term"),
    ("row_search_term_view", "searchTermView.status", "search_term_view.status"),
    (
        "row_search_term_view",
        "segments.keyword.adGroupCriterion",
        "segments.keyword.ad_group_criterion",
    ),
    ("row_search_term_view", "segments.keyword.info.text", "segments.keyword.info.text"),
    (
        "row_search_term_view",
        "segments.keyword.info.matchType",
        "segments.keyword.info.match_type",
    ),
    ("row_ad_group_criterion", "adGroupCriterion.status", "ad_group_criterion.status"),
    ("row_ad_group_criterion", "adGroupCriterion.keyword.text", "ad_group_criterion.keyword.text"),
    (
        "row_ad_group_criterion",
        "adGroupCriterion.keyword.matchType",
        "ad_group_criterion.keyword.match_type",
    ),
    ("row_conversion_action", "conversionAction.name", "conversion_action.name"),
    ("row_conversion_action", "conversionAction.status", "conversion_action.status"),
    ("row_conversion_action", "conversionAction.countingType", "conversion_action.counting_type"),
    (
        "row_conversion_action",
        "conversionAction.attributionModelSettings.attributionModel",
        "conversion_action.attribution_model_settings.attribution_model",
    ),
]

# Free-text fields kept as returned: surface, REST path, parsed attribute.
FREE_TEXT_FIELDS: list[tuple[str, str, str]] = [
    ("row_customer", "customer.descriptiveName", "descriptive_name"),
    ("row_campaign", "campaign.name", "name"),
    ("row_ad_group", "adGroup.name", "name"),
    ("row_search_term_view", "searchTermView.searchTerm", "search_term"),
    ("row_search_term_view", "segments.keyword.info.text", "matched_keyword_text"),
    ("row_ad_group_criterion", "adGroupCriterion.keyword.text", "text"),
    ("row_conversion_action", "conversionAction.name", "name"),
]

# Each carries the sentinels "crm" and "tool"; the non-ASCII ones exercise the regex path.
UNSTORABLE_STRINGS = [
    pytest.param("crm\x00tool", id="nul-inside"),
    pytest.param("\x00crm tool", id="nul-first"),
    pytest.param("crm tool\x00", id="nul-last"),
    pytest.param("caf\u00e9 crm\x00tool", id="nul-in-non-ascii"),
    pytest.param("crm\ud800tool", id="unpaired-high-surrogate"),
    pytest.param("crm tool\udfff", id="unpaired-low-surrogate"),
    pytest.param("crm \ude00\ud83d tool", id="pair-in-reverse-order"),
]

STORABLE_STRINGS = [
    pytest.param("caf\u00e9 \u2615 \U0001f600 \u6771\u4eac\ttab\nnew line", id="unicode"),
    pytest.param("\x01\x1f\x7f crm tool", id="other-control-characters"),
    pytest.param("\ud7ff\ue000\ufffd\U0010ffff", id="code-points-beside-the-surrogate-range"),
]


def test_string_fields_are_every_field_read_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    read: set[tuple[str, str]] = set()
    text = parser._RowReader.text

    def recording_text(reader: Any, gaql_field: str) -> str:
        read.add((reader._surface, gaql_field))
        return text(reader, gaql_field)

    monkeypatch.setattr(parser._RowReader, "text", recording_text)
    for surface in ROW_SURFACES:
        _parse(surface, BASE_ROWS[surface]())
    assert read == {(surface, gaql_field) for surface, _, gaql_field in STRING_FIELDS}


@pytest.mark.parametrize("value", UNSTORABLE_STRINGS)
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field"), STRING_FIELDS)
def test_string_postgresql_text_cannot_store_is_a_defect_naming_the_field_only(
    surface: str, rest_keys: str, gaql_field: str, value: str
) -> None:
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, value))
    _assert_defect(error, surface, f"{gaql_field}: expected {NOT_STORABLE_TEXT}")
    for echoed in (error.message, error.reason):
        assert echoed.isascii()
        assert "\x00" not in echoed
        assert "crm" not in echoed
        assert "tool" not in echoed


@pytest.mark.parametrize("value", STORABLE_STRINGS)
@pytest.mark.parametrize(("surface", "rest_keys", "attribute"), FREE_TEXT_FIELDS)
def test_free_text_postgresql_text_can_store_is_kept_unchanged(
    surface: str, rest_keys: str, attribute: str, value: str
) -> None:
    parsed = _parse(surface, _with(BASE_ROWS[surface](), rest_keys, value))
    assert getattr(parsed, attribute) == value


def test_json_surrogate_pair_is_kept_but_lone_surrogate_or_nul_escape_is_a_defect() -> None:
    # connectors/http.py decodes the body with json.loads: a surrogate-pair escape becomes one
    # astral code point, while a lone surrogate escape and \u0000 stay as they are.
    paired = json.loads('"crm \\ud83d\\ude00 tool"')
    assert paired == "crm \U0001f600 tool"
    row = _with(search_term_row(), "searchTermView.searchTerm", paired)
    assert _parse("row_search_term_view", row).search_term == paired
    for escaped in ('"crm \\ud83d tool"', '"crm\\u0000tool"'):
        row = _with(search_term_row(), "searchTermView.searchTerm", json.loads(escaped))
        error = _defect("row_search_term_view", row)
        _assert_defect(
            error,
            "row_search_term_view",
            f"search_term_view.search_term: expected {NOT_STORABLE_TEXT}",
        )


# --- absent keys versus explicit JSON null -----------------------------------------------------
# REST JSON omits zero-valued and unset fields, so an absent key keeps the meaning its contract
# gives it: zero for an additive metric, unavailable for a rate, score or budget amount, false for
# a flag. No row contract declares a null type, so a key present with null is a defect at any
# level of the path and is never read as that absence.

ABSENT_MEANING: list[tuple[str, str, str, str, object]] = [
    ("row_campaign_daily", "metrics.costMicros", "metrics.cost_micros", "cost_micros", 0),
    ("row_campaign_daily", "metrics.impressions", "metrics.impressions", "impressions", 0),
    ("row_campaign_daily", "metrics.clicks", "metrics.clicks", "clicks", 0),
    ("row_campaign_daily", "metrics.conversions", "metrics.conversions", "conversions", 0),
    (
        "row_campaign_daily",
        "metrics.conversionsValue",
        "metrics.conversions_value",
        "conversions_value",
        0,
    ),
    (
        "row_campaign_daily",
        "metrics.searchBudgetLostImpressionShare",
        "metrics.search_budget_lost_impression_share",
        "search_budget_lost_impression_share",
        None,
    ),
    (
        "row_campaign_daily",
        "metrics.searchImpressionShare",
        "metrics.search_impression_share",
        "search_impression_share",
        None,
    ),
    ("row_keyword_view", "metrics.costMicros", "metrics.cost_micros", "cost_micros", 0),
    ("row_keyword_view", "metrics.conversions", "metrics.conversions", "conversions", 0),
    ("row_keyword_view", "metrics.averageCpc", "metrics.average_cpc", "average_cpc", None),
    (
        "row_keyword_view",
        "metrics.historicalQualityScore",
        "metrics.historical_quality_score",
        "historical_quality_score",
        None,
    ),
    ("row_search_term_view", "metrics.costMicros", "metrics.cost_micros", "cost_micros", 0),
    ("row_search_term_view", "metrics.conversions", "metrics.conversions", "conversions", 0),
    (
        "row_search_term_view",
        "metrics.conversionsValue",
        "metrics.conversions_value",
        "conversions_value",
        0,
    ),
    (
        "row_search_term_view",
        "segments.keyword.adGroupCriterion",
        "segments.keyword.ad_group_criterion",
        "matched_criterion_id",
        "",
    ),
    (
        "row_campaign",
        "campaignBudget.amountMicros",
        "campaign_budget.amount_micros",
        "budget_amount_micros",
        None,
    ),
    (
        "row_campaign",
        "campaignBudget.explicitlyShared",
        "campaign_budget.explicitly_shared",
        "budget_explicitly_shared",
        False,
    ),
    (
        "row_ad_group_criterion",
        "adGroupCriterion.qualityInfo.qualityScore",
        "ad_group_criterion.quality_info.quality_score",
        "quality_score",
        None,
    ),
    (
        "row_conversion_action",
        "conversionAction.includeInConversionsMetric",
        "conversion_action.include_in_conversions_metric",
        "include_in_conversions_metric",
        False,
    ),
    (
        "row_customer",
        "customer.descriptiveName",
        "customer.descriptive_name",
        "descriptive_name",
        None,
    ),
]


@pytest.mark.parametrize(
    ("surface", "rest_keys", "gaql_field", "attribute", "absent_value"), ABSENT_MEANING
)
def test_absent_key_keeps_its_contract_meaning_but_explicit_null_is_a_defect(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, absent_value: object
) -> None:
    value = getattr(_parse(surface, _without(BASE_ROWS[surface](), rest_keys)), attribute)
    if absent_value is None or isinstance(absent_value, bool):
        assert value is absent_value
    else:
        assert value == absent_value
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, None))
    _assert_defect(error, surface, f"{gaql_field}: expected {NOT_NULL_PATH}")


@pytest.mark.parametrize(
    ("surface", "rest_keys", "gaql_field"),
    [
        *[(surface, rest, gaql) for surface, (gaql, rest) in ID_FIELD_OF_SURFACE.items()],
        ("row_customer", "customer.timeZone", "customer.time_zone"),
        ("row_campaign", "campaign.status", "campaign.status"),
        ("row_campaign", "campaign.advertisingChannelType", "campaign.advertising_channel_type"),
        ("row_campaign", "campaignBudget.period", "campaign_budget.period"),
        ("row_campaign_daily", "segments.date", "segments.date"),
        ("row_ad_group", "adGroup.name", "ad_group.name"),
        ("row_search_term_view", "searchTermView.searchTerm", "search_term_view.search_term"),
        ("row_search_term_view", "segments.keyword.info.text", "segments.keyword.info.text"),
        (
            "row_search_term_view",
            "segments.keyword.info.matchType",
            "segments.keyword.info.match_type",
        ),
        (
            "row_ad_group_criterion",
            "adGroupCriterion.keyword.matchType",
            "ad_group_criterion.keyword.match_type",
        ),
        (
            "row_conversion_action",
            "conversionAction.countingType",
            "conversion_action.counting_type",
        ),
        (
            "row_conversion_action",
            "conversionAction.attributionModelSettings.attributionModel",
            "conversion_action.attribution_model_settings.attribution_model",
        ),
    ],
)
def test_explicit_json_null_field_is_a_validation_defect(
    surface: str, rest_keys: str, gaql_field: str
) -> None:
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, None))
    _assert_defect(error, surface, f"{gaql_field}: expected {NOT_NULL_PATH}")


@pytest.mark.parametrize(
    ("surface", "rest_keys", "gaql_prefix"),
    [
        *[
            (surface, rest.split(".")[0], gaql.split(".")[0] + ".")
            for surface, (gaql, rest) in ID_FIELD_OF_SURFACE.items()
        ],
        ("row_campaign_daily", "metrics", "metrics."),
        ("row_campaign_daily", "segments", "segments."),
        ("row_keyword_view", "metrics", "metrics."),
        ("row_search_term_view", "metrics", "metrics."),
        ("row_search_term_view", "segments.keyword", "segments.keyword."),
        ("row_search_term_view", "segments.keyword.info", "segments.keyword.info."),
        ("row_campaign", "campaignBudget", "campaign_budget"),
        (
            "row_ad_group_criterion",
            "adGroupCriterion.qualityInfo",
            "ad_group_criterion.quality_info.",
        ),
        ("row_ad_group_criterion", "adGroupCriterion.keyword", "ad_group_criterion.keyword."),
        (
            "row_conversion_action",
            "conversionAction.attributionModelSettings",
            "conversion_action.attribution_model_settings.",
        ),
    ],
)
def test_explicit_json_null_object_is_a_validation_defect(
    surface: str, rest_keys: str, gaql_prefix: str
) -> None:
    # A null object never stands for its omitted fields: "metrics": null is not zero metrics.
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, None))
    assert error.message.startswith(gaql_prefix)
    assert error.message.endswith(f": expected {NOT_NULL_PATH}")
    _assert_defect(error, surface, error.message)


# --- non-negative DOUBLE metrics -----------------------------------------------------------------
# Conversions, conversion value and average CPC are counts, amounts or cost divided by clicks;
# the fact tables carry non-negative checks on each of them.

NON_NEGATIVE_DOUBLES: list[tuple[str, str, str, str]] = [
    ("row_campaign_daily", "metrics.conversions", "metrics.conversions", "conversions"),
    (
        "row_campaign_daily",
        "metrics.conversionsValue",
        "metrics.conversions_value",
        "conversions_value",
    ),
    ("row_keyword_view", "metrics.conversions", "metrics.conversions", "conversions"),
    ("row_keyword_view", "metrics.averageCpc", "metrics.average_cpc", "average_cpc"),
    ("row_search_term_view", "metrics.conversions", "metrics.conversions", "conversions"),
    (
        "row_search_term_view",
        "metrics.conversionsValue",
        "metrics.conversions_value",
        "conversions_value",
    ),
]


@pytest.mark.parametrize("wire", [Decimal("-0.3"), Decimal("-0.000001"), Decimal("-125.75"), -1])
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), NON_NEGATIVE_DOUBLES)
def test_negative_double_metric_is_a_validation_defect(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: object
) -> None:
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, wire))
    _assert_defect(error, surface, f"{gaql_field}: expected {NOT_NON_NEGATIVE}")


@pytest.mark.parametrize("wire", [Decimal("-0"), Decimal("-0.000"), Decimal(0), 0])
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), NON_NEGATIVE_DOUBLES)
def test_zero_double_metric_negative_zero_included_parses_as_unsigned_zero(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: object
) -> None:
    value = getattr(_parse(surface, _with(BASE_ROWS[surface](), rest_keys, wire)), attribute)
    assert value == 0
    assert isinstance(value, Decimal)
    assert not value.is_signed()


# --- numeric(18, 6) storage --------------------------------------------------------------------
# Every Decimal row field is stored as numeric(18, 6): at most 999999999999.999999, six decimal
# places. Rows carry only values that column stores unchanged: amount micros are bounded,
# conversions and conversion value are kept exactly or rejected (never rounded), impression
# shares finer than six places are rounded toward zero, and a value that would overflow the
# column, change in storage or turn a positive share into zero is a defect naming only the field
# and its bounds.

# Each micros field whose amount a row emits: surface, base row, REST path, GAQL field, then the
# attributes holding the micros and the amount.
AMOUNT_MICROS_FIELDS = [
    pytest.param(
        "row_campaign_daily",
        campaign_daily_row,
        "metrics.costMicros",
        "metrics.cost_micros",
        "cost_micros",
        "cost",
        id="campaign-daily-cost",
    ),
    pytest.param(
        "row_keyword_view",
        keyword_view_row,
        "metrics.costMicros",
        "metrics.cost_micros",
        "cost_micros",
        "cost",
        id="keyword-view-cost",
    ),
    pytest.param(
        "row_search_term_view",
        search_term_row,
        "metrics.costMicros",
        "metrics.cost_micros",
        "cost_micros",
        "cost",
        id="search-term-cost",
    ),
    pytest.param(
        "row_campaign",
        campaign_row,
        "campaignBudget.amountMicros",
        "campaign_budget.amount_micros",
        "budget_amount_micros",
        "budget_amount",
        id="daily-budget",
    ),
    pytest.param(
        "row_campaign",
        lifetime_campaign_row,
        "campaignBudget.totalAmountMicros",
        "campaign_budget.total_amount_micros",
        "budget_amount_micros",
        "budget_amount",
        id="custom-period-budget",
    ),
]
AMOUNT_MICROS_PARAMS = (
    "surface",
    "base",
    "rest_keys",
    "gaql_field",
    "micros_attribute",
    "amount_attribute",
)


@pytest.mark.parametrize(AMOUNT_MICROS_PARAMS, AMOUNT_MICROS_FIELDS)
def test_amount_micros_at_the_numeric_maximum_are_kept_exact(
    surface: str,
    base: Callable[[], dict[str, Any]],
    rest_keys: str,
    gaql_field: str,
    micros_attribute: str,
    amount_attribute: str,
) -> None:
    parsed = _parse(surface, _with(base(), rest_keys, AMOUNT_MICROS_MAX_TEXT))
    micros = getattr(parsed, micros_attribute)
    amount = getattr(parsed, amount_attribute)
    assert type(micros) is int
    assert micros == 999_999_999_999_999_999
    assert isinstance(amount, Decimal)
    assert amount.as_tuple() == Decimal(NUMERIC_MAX_TEXT).as_tuple()


@pytest.mark.parametrize(
    "micros", ["1000000000000000000", INT64_MAX_TEXT, INT64_OVERFLOW_TEXT, "-1"]
)
@pytest.mark.parametrize(AMOUNT_MICROS_PARAMS, AMOUNT_MICROS_FIELDS)
def test_amount_micros_beyond_the_numeric_range_are_a_defect_never_clamped(
    surface: str,
    base: Callable[[], dict[str, Any]],
    rest_keys: str,
    gaql_field: str,
    micros_attribute: str,
    amount_attribute: str,
    micros: str,
) -> None:
    error = _defect(surface, _with(base(), rest_keys, micros))
    _assert_defect(error, surface, f"{gaql_field}: expected {AMOUNT_MICROS_RANGE}")
    assert micros not in error.message


# The DOUBLE conversion metrics of each surface; average CPC, in micros, is checked separately.
CONVERSION_DOUBLES = [entry for entry in NON_NEGATIVE_DOUBLES if entry[3] != "average_cpc"]


# Google returns conversions and conversion values with at most six decimal places (observed
# live, 2026-10-07), so a finer value is one numeric(18, 6) would change, and it is rejected,
# never rounded: rounding 1.0000001 or 2.9999996 would make a fractional count whole (B4 withholds
# fractional conversions), and 0.0000003 would store a converting day as zero (K1 counts any
# fraction as converting).
CONVERSIONS_FINER_THAN_SIX_PLACES = [
    pytest.param(Decimal("1.0000001"), id="just-above-a-whole-count"),
    pytest.param(Decimal("2.9999996"), id="just-below-a-whole-count"),
    pytest.param(Decimal("2.3333333333333335"), id="full-double"),
    pytest.param(Decimal("5.0467620000001"), id="six-places-then-a-digit"),
    pytest.param(Decimal("0.0000025"), id="half-even-tie"),
    pytest.param(Decimal("0.0000015"), id="half-even-tie-upwards"),
    # Positive values below one millionth, which six places would store as zero.
    pytest.param(Decimal("0.0000003"), id="below-a-millionth"),
    pytest.param(Decimal("0.0000005"), id="half-a-millionth"),
    pytest.param(Decimal("0.00000099"), id="just-below-a-millionth"),
    pytest.param(Decimal("1E-30"), id="tiny-positive"),
    # Below 10**12, yet no six-place value equals them.
    pytest.param(Decimal("999999999999.9999994"), id="just-above-the-maximum"),
    pytest.param(Decimal("999999999999.9999995"), id="tie-above-the-maximum"),
    pytest.param(Decimal("999999999999.9999996"), id="just-below-the-limit"),
]


@pytest.mark.parametrize("wire", CONVERSIONS_FINER_THAN_SIX_PLACES)
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), CONVERSION_DOUBLES)
def test_conversion_doubles_finer_than_six_places_are_a_defect_never_rounded(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: Decimal
) -> None:
    # The row conforms to its contract (a JSON number), so _parse checks it before parsing.
    row = _with(BASE_ROWS[surface](), rest_keys, wire)
    with pytest.raises(QueryDefect) as caught:
        _parse(surface, row)
    _assert_defect(caught.value, surface, f"{gaql_field}: expected {NUMERIC_SCALE}")


@pytest.mark.parametrize(
    "wire",
    [
        # str gives 0.30000000000000004, which numeric(18, 6) cannot hold exactly.
        pytest.param(0.1 + 0.2, id="float-sum"),
        pytest.param(3e-7, id="float-below-a-millionth"),
        pytest.param("2.9999996", id="numeric-string"),
    ],
)
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), CONVERSION_DOUBLES)
def test_off_contract_conversions_finer_than_six_places_are_a_defect_never_rounded(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: object
) -> None:
    # A float or a numeric string is tolerated off-contract as an encoding, never as a value
    # finer than six places.
    row = _with(BASE_ROWS[surface](), rest_keys, wire)
    with pytest.raises(QueryDefect) as caught:
        _parse_off_contract(surface, row)
    _assert_defect(caught.value, surface, f"{gaql_field}: expected {NUMERIC_SCALE}")


@pytest.mark.parametrize(
    "wire",
    [
        Decimal(NUMERIC_MAX_TEXT),
        Decimal("0.000001"),
        Decimal("123.456789"),
        Decimal("1E+11"),
        Decimal("2.5"),
        Decimal("0.3"),
        7,
        # Values observed live (2026-10-07).
        Decimal("5.046762"),
        Decimal("0.952596"),
        # Trailing zeros past the sixth place change no value, so the exponent is kept too.
        Decimal("1.0000000"),
        Decimal("0.95259600000"),
    ],
)
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), CONVERSION_DOUBLES)
def test_conversion_doubles_within_six_places_are_kept_as_returned(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: int | Decimal
) -> None:
    value = getattr(_parse(surface, _with(BASE_ROWS[surface](), rest_keys, wire)), attribute)
    assert isinstance(value, Decimal)
    assert value.as_tuple() == Decimal(wire).as_tuple()


@pytest.mark.parametrize(
    "wire",
    [
        Decimal("1E+12"),
        Decimal("1000000000000"),
        Decimal("1000000000000.000000"),
        Decimal("1000000000000.0000001"),
        Decimal("12345678901234.5"),
        # Six places of 1E+40 need 47 digits; the range is checked before any six-place step.
        Decimal("1E+40"),
    ],
)
@pytest.mark.parametrize(("surface", "rest_keys", "gaql_field", "attribute"), CONVERSION_DOUBLES)
def test_conversion_doubles_beyond_the_numeric_range_are_a_defect(
    surface: str, rest_keys: str, gaql_field: str, attribute: str, wire: Decimal
) -> None:
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, wire))
    _assert_defect(error, surface, f"{gaql_field}: expected {NUMERIC_RANGE}")


SHARE_FIELDS: list[tuple[str, str, str]] = [
    (
        "metrics.searchBudgetLostImpressionShare",
        "metrics.search_budget_lost_impression_share",
        "search_budget_lost_impression_share",
    ),
    ("metrics.searchImpressionShare", "metrics.search_impression_share", "search_impression_share"),
]


# The capped-day threshold on the budget-lost share: 10%, a six-place value.
CAPPED_DAY_SHARE = Decimal("0.1")


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        # Full doubles observed live (2026-10-07).
        (Decimal("0.5407821229050279"), Decimal("0.540782")),
        (Decimal("0.0011627906976744186"), Decimal("0.001162")),
        (Decimal("0.2273449920508744"), Decimal("0.227344")),
        (Decimal("0.123456789"), Decimal("0.123456")),
        (Decimal("0.0000025"), Decimal("0.000002")),
        (Decimal("0.90010000001"), Decimal("0.900100")),
        # Toward zero, never up to a whole share or across 10% in either direction.
        (Decimal("0.99999999"), Decimal("0.999999")),
        (Decimal("0.09999999"), Decimal("0.099999")),
        (Decimal("0.10000009"), Decimal("0.100000")),
    ],
)
@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), SHARE_FIELDS)
def test_impression_shares_finer_than_six_places_are_rounded_toward_zero(
    rest_keys: str, gaql_field: str, attribute: str, wire: Decimal, expected: Decimal
) -> None:
    row = _with(campaign_daily_row(), rest_keys, wire)
    value = getattr(_parse("row_campaign_daily", row), attribute)
    assert value.as_tuple() == expected.as_tuple()
    assert value <= wire
    # The stored share and the share returned fall on the same side of the 10% threshold.
    assert (value >= CAPPED_DAY_SHARE) is (wire >= CAPPED_DAY_SHARE)


@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), SHARE_FIELDS)
def test_share_just_below_ten_percent_stays_below_it_once_stored(
    rest_keys: str, gaql_field: str, attribute: str
) -> None:
    below = _with(campaign_daily_row(), rest_keys, Decimal("0.09999999"))
    value = getattr(_parse("row_campaign_daily", below), attribute)
    assert value == Decimal("0.099999")
    assert value < CAPPED_DAY_SHARE
    at = _with(campaign_daily_row(), rest_keys, Decimal("0.10000009"))
    assert getattr(_parse("row_campaign_daily", at), attribute) >= CAPPED_DAY_SHARE


@pytest.mark.parametrize(
    "wire",
    [
        Decimal("0.0000003"),
        Decimal("0.0000005"),
        # Half-even would keep it as 0.000001; toward zero it would be stored as zero.
        Decimal("0.00000099"),
        Decimal("1E-9"),
    ],
)
@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), SHARE_FIELDS)
def test_positive_impression_share_that_would_round_to_zero_is_a_defect(
    rest_keys: str, gaql_field: str, attribute: str, wire: Decimal
) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), rest_keys, wire))
    _assert_defect(error, "row_campaign_daily", f"{gaql_field}: expected {FRACTION_POSITIVE}")


@pytest.mark.parametrize(
    ("average_cpc", "expected"),
    [
        (Decimal(AMOUNT_MICROS_MAX_TEXT), Decimal(NUMERIC_MAX_TEXT)),
        (Decimal("999999999999999999.4"), Decimal(NUMERIC_MAX_TEXT)),
        (Decimal("0.6"), Decimal("0.000001")),
        (Decimal("0.5000001"), Decimal("0.000001")),
        (Decimal("1.5"), Decimal("0.000002")),
        (Decimal("2.5"), Decimal("0.000002")),
    ],
)
def test_average_cpc_up_to_the_numeric_maximum_converts_half_even(
    average_cpc: Decimal, expected: Decimal
) -> None:
    row = _with(keyword_view_row(), "metrics.averageCpc", average_cpc)
    value = _parse("row_keyword_view", row).average_cpc
    assert value is not None
    assert value.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize("average_cpc", [Decimal("0.4"), Decimal("0.5"), Decimal("1E-20")])
def test_positive_average_cpc_below_half_a_micro_is_a_defect(average_cpc: Decimal) -> None:
    # Half a micro is a tie that half-even rounds to zero; a CPC with clicks is never zero.
    row = _with(keyword_view_row(), "metrics.averageCpc", average_cpc)
    error = _defect("row_keyword_view", row)
    _assert_defect(error, "row_keyword_view", f"metrics.average_cpc: expected {CPC_POSITIVE}")


@pytest.mark.parametrize(
    ("surface", "rest_keys", "wire", "fingerprint"),
    [
        ("row_campaign_daily", "metrics.costMicros", "4567891234567891234", "4567891234"),
        ("row_campaign", "campaignBudget.amountMicros", "4567891234567891234", "4567891234"),
        ("row_campaign_daily", "metrics.conversions", Decimal("4567891234567.25"), "4567891234"),
        ("row_keyword_view", "metrics.conversions", Decimal("0.00000031"), "31"),
        ("row_search_term_view", "metrics.conversions", Decimal("2.3333333333333335"), "3333"),
        ("row_campaign_daily", "metrics.conversionsValue", Decimal("5.0467621"), "0467"),
        ("row_keyword_view", "metrics.conversions", Decimal("999999999999.9999994"), "9994"),
        ("row_search_term_view", "metrics.conversionsValue", Decimal("4567891234567.5"), "4567"),
        ("row_search_term_view", "metrics.conversionsValue", 0.1 + 0.2, "30000000000000004"),
        ("row_campaign_daily", "metrics.searchImpressionShare", Decimal("0.00000047"), "47"),
        (
            "row_campaign_daily",
            "metrics.searchBudgetLostImpressionShare",
            Decimal("0.00000083"),
            "83",
        ),
        ("row_keyword_view", "metrics.averageCpc", Decimal("4567891234567891234.5"), "4567"),
        ("row_keyword_view", "metrics.averageCpc", Decimal("0.37"), "37"),
    ],
)
def test_numeric_storage_defect_messages_never_carry_row_values(
    surface: str, rest_keys: str, wire: object, fingerprint: str
) -> None:
    error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, wire))
    assert error.code == "validation"
    assert fingerprint not in error.message
    assert fingerprint not in error.reason
    assert str(wire) not in error.message
    assert len(error.message) <= 300
    assert "…" not in error.message


# --- int64 identifiers --------------------------------------------------------------------------


@pytest.mark.parametrize(("wire", "expected"), [("0", 0), ("7", 7), (INT64_MAX_TEXT, INT64_MAX)])
def test_int64_id_strings_cover_the_whole_id_range(wire: str, expected: int) -> None:
    parsed = _parse("row_campaign", _with(campaign_row(), "campaign.id", wire))
    assert parsed.campaign_id == expected
    assert type(parsed.campaign_id) is int


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (INT64_OVERFLOW_TEXT, INT64_RANGE),
        ("-1", INT64_RANGE),
        ("99999999999999999999", NOT_INT64),
        ("12a", NOT_INT64),
        ("", NOT_INT64),
        (" 7", NOT_INT64),
        ("7\n", NOT_INT64),
        ("1.5", NOT_INT64),
        ("1e3", NOT_INT64),
        (True, NOT_INT64),
        (Decimal("7.5"), NOT_INT64),
        (Decimal("NaN"), NOT_INT64),
        (Decimal("Infinity"), NOT_INT64),
        (7.0, NOT_INT64),
        (["7"], NOT_INT64),
    ],
)
def test_malformed_int64_id_is_a_defect(wire: object, expected: str) -> None:
    error = _defect("row_campaign", _with(campaign_row(), "campaign.id", wire))
    _assert_defect(error, "row_campaign", f"campaign.id: expected {expected}")


@pytest.mark.parametrize(
    ("wire", "expected"),
    [(7, 7), (Decimal("7"), 7), (Decimal("7.0"), 7), (Decimal("7E+2"), 700)],
)
def test_int64_id_accepts_json_integers_and_integral_decimals(wire: object, expected: int) -> None:
    parsed = _parse_off_contract("row_campaign", _with(campaign_row(), "campaign.id", wire))
    assert parsed.campaign_id == expected
    assert type(parsed.campaign_id) is int


@pytest.mark.parametrize("wire", ["+7", "007", "-0"])
def test_signed_or_zero_padded_id_strings_are_accepted(wire: str) -> None:
    # Strings conform to the contract's string type; the parser reads them as decimal INT64.
    parsed = _parse("row_ad_group", _with(ad_group_row(), "adGroup.id", wire))
    assert parsed.ad_group_id == int(wire)


# --- get_field and rest_path --------------------------------------------------------------------


def test_rest_path_converts_each_snake_case_segment_to_lower_camel_case() -> None:
    assert rest_path("campaign.id") == ("campaign", "id")
    assert rest_path("campaign_budget.amount_micros") == ("campaignBudget", "amountMicros")
    assert rest_path("segments.keyword.info.match_type") == (
        "segments",
        "keyword",
        "info",
        "matchType",
    )
    assert rest_path("ad_group_criterion.quality_info.quality_score") == (
        "adGroupCriterion",
        "qualityInfo",
        "qualityScore",
    )
    assert rest_path("metrics.search_budget_lost_impression_share") == (
        "metrics",
        "searchBudgetLostImpressionShare",
    )


def test_get_field_returns_raw_values_with_int64_still_strings() -> None:
    row = campaign_row()
    assert get_field(row, "campaign.id") == CAMPAIGN
    assert get_field(row, "campaign_budget.amount_micros") == "50000000"
    assert get_field(row, "campaign_budget.explicitly_shared") is False
    assert get_field(row, "campaign") == row["campaign"]
    assert get_field(keyword_row(), "ad_group_criterion.quality_info.quality_score") == 8


def test_get_field_returns_none_for_any_absent_key() -> None:
    row = campaign_row()
    assert get_field(row, "campaign_budget.total_amount_micros") is None
    assert get_field(row, "ad_group.id") is None
    assert get_field(_without(row, "campaignBudget"), "campaign_budget.id") is None
    assert get_field({"campaign": {}}, "campaign.id") is None
    assert get_field({}, "segments.keyword.info.text") is None


@pytest.mark.parametrize(
    ("row", "gaql_field"),
    [
        ({"campaign": None}, "campaign.id"),
        ({"campaign": None}, "campaign"),
        ({"campaign": {"id": None}}, "campaign.id"),
        ({"segments": {"keyword": None}}, "segments.keyword.info.text"),
        ({"segments": {"keyword": {"info": {"text": None}}}}, "segments.keyword.info.text"),
    ],
)
def test_get_field_rejects_json_null_at_any_level_of_the_path(
    row: dict[str, Any], gaql_field: str
) -> None:
    # No row contract declares a null type, so an explicit null is never read as absence.
    with pytest.raises(QueryDefect) as caught:
        get_field(row, gaql_field)
    error = caught.value
    assert error.message == f"{gaql_field}: expected {NOT_NULL_PATH}"
    assert error.endpoint == "parse google_ads_row"
    assert error.code == "validation"
    assert error.query_id is None


@pytest.mark.parametrize(
    "gaql_field", ["Campaign.id", "campaign..id", "", "campaign.id;", "campaignBudget.id", "a b"]
)
def test_get_field_rejects_names_that_are_not_gaql_fields(gaql_field: str) -> None:
    with pytest.raises(QueryDefect) as caught:
        get_field(campaign_row(), gaql_field)
    error = caught.value
    assert error.message == f"{gaql_field}: expected a GAQL field name"
    assert error.endpoint == "parse google_ads_row"
    assert error.code == "validation"
    assert error.query_id is None


def test_get_field_rejects_a_value_that_is_not_an_object_along_the_path() -> None:
    with pytest.raises(QueryDefect) as caught:
        get_field({"campaign": "7"}, "campaign.id")
    assert caught.value.message == f"campaign.id: expected {NOT_OBJECT_PATH}"
    assert caught.value.endpoint == "parse google_ads_row"
    with pytest.raises(QueryDefect) as caught:
        get_field({"campaign": {"id": "7"}}, "campaign.id.value")
    assert caught.value.message == f"campaign.id.value: expected {NOT_OBJECT_PATH}"


def test_get_field_names_the_surface_it_is_given() -> None:
    row: Any = ["not", "a", "row"]
    with pytest.raises(QueryDefect) as caught:
        get_field(row, "ad_group.id", surface="row_keyword_view")
    error = caught.value
    assert error.message == f"ad_group.id: expected {NOT_OBJECT_PATH}"
    assert error.endpoint == "parse row_keyword_view"
    assert error.query_id == "Q-G5"
    with pytest.raises(QueryDefect) as caught:
        get_field(row, "campaign.id", surface="row_campaign")
    assert caught.value.endpoint == "parse row_campaign"
    assert caught.value.query_id is None


# --- Q-G1 customer --------------------------------------------------------------------------


def test_customer_row_gives_currency_and_time_zone() -> None:
    parsed = _parse("row_customer", customer_row())
    assert parsed == CustomerRow(
        customer_id=1234567890,
        descriptive_name="Example Advertiser",
        currency_code="USD",
        time_zone="America/New_York",
    )


def test_customer_without_descriptive_name_has_none() -> None:
    parsed = _parse("row_customer", _without(customer_row(), "customer.descriptiveName"))
    assert parsed.descriptive_name is None


@pytest.mark.parametrize("currency", ["EUR", "GBP", "JPY"])
def test_customer_accepts_any_iso_4217_shaped_currency(currency: str) -> None:
    parsed = _parse("row_customer", _with(customer_row(), "customer.currencyCode", currency))
    assert parsed.currency_code == currency


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "U5D", "", "US D"])
def test_customer_rejects_a_currency_that_is_not_three_upper_case_letters(currency: str) -> None:
    error = _defect("row_customer", _with(customer_row(), "customer.currencyCode", currency))
    _assert_defect(
        error, "row_customer", "customer.currency_code: expected an ISO 4217 currency code"
    )


def test_customer_rejects_a_currency_that_is_not_a_string() -> None:
    error = _defect("row_customer", _with(customer_row(), "customer.currencyCode", 840))
    _assert_defect(error, "row_customer", "customer.currency_code: expected a string")
    error = _defect("row_customer", _without(customer_row(), "customer.currencyCode"))
    _assert_defect(error, "row_customer", "customer.currency_code: expected a string")


@pytest.mark.parametrize("time_zone", ["Europe/London", "Asia/Kolkata", "UTC"])
def test_customer_accepts_iana_time_zones(time_zone: str) -> None:
    parsed = _parse("row_customer", _with(customer_row(), "customer.timeZone", time_zone))
    assert parsed.time_zone == time_zone


@pytest.mark.parametrize(
    "time_zone", ["Mars/Olympus_Mons", "../etc/passwd", "/etc/localtime", "", "EST5EDT/x"]
)
def test_customer_rejects_a_time_zone_that_is_not_an_iana_id(time_zone: str) -> None:
    error = _defect("row_customer", _with(customer_row(), "customer.timeZone", time_zone))
    _assert_defect(error, "row_customer", "customer.time_zone: expected an IANA time-zone id")


def test_customer_rejects_a_time_zone_that_is_not_a_string() -> None:
    error = _defect("row_customer", _with(customer_row(), "customer.timeZone", -5))
    _assert_defect(error, "row_customer", "customer.time_zone: expected a string")


def test_customer_rejects_a_descriptive_name_that_is_not_a_string() -> None:
    error = _defect("row_customer", _with(customer_row(), "customer.descriptiveName", 42))
    _assert_defect(error, "row_customer", "customer.descriptive_name: expected a string")


# --- Q-G2 / Q-G2b campaigns and budgets -------------------------------------------------------


def test_daily_budget_keeps_micros_and_an_exact_amount() -> None:
    parsed = _parse("row_campaign", campaign_row())
    assert parsed == CampaignRow(
        campaign_id=111,
        name="Brand - Search",
        status="active",
        raw_status="ENABLED",
        advertising_channel_type="SEARCH",
        budget_id=222,
        budget_period="DAILY",
        budget_amount_micros=50_000_000,
        budget_amount=Decimal("50.000000"),
        budget_explicitly_shared=False,
    )
    assert type(parsed.budget_amount_micros) is int
    assert isinstance(parsed.budget_amount, Decimal)
    assert parsed.budget_amount.as_tuple().exponent == -6
    assert parsed.daily_budget == Decimal("50.000000")
    assert parsed.total_budget is None


def test_custom_period_budget_reads_total_amount_micros() -> None:
    parsed = _parse("row_campaign", lifetime_campaign_row())
    assert parsed.status == "paused"
    assert parsed.raw_status == "PAUSED"
    assert parsed.advertising_channel_type == "DISPLAY"
    assert parsed.budget_id == 222
    assert parsed.budget_period == "CUSTOM_PERIOD"
    assert parsed.budget_amount_micros == 1_500_000_000
    assert parsed.budget_amount == Decimal("1500.000000")
    assert parsed.budget_amount.as_tuple().exponent == -6
    assert parsed.total_budget == Decimal("1500.000000")
    assert parsed.daily_budget is None
    assert parsed.budget_explicitly_shared is False


def test_budget_without_its_own_period_amount_is_unknown_not_zero() -> None:
    # A CUSTOM_PERIOD budget whose total is unset is unknown, never its average daily amount.
    parsed = _parse(
        "row_campaign", _without(lifetime_campaign_row(), "campaignBudget.totalAmountMicros")
    )
    assert parsed.budget_period == "CUSTOM_PERIOD"
    assert parsed.budget_amount_micros is None
    assert parsed.budget_amount is None
    assert parsed.total_budget is None
    assert parsed.daily_budget is None
    parsed = _parse("row_campaign", _without(campaign_row(), "campaignBudget.amountMicros"))
    assert parsed.budget_period == "DAILY"
    assert parsed.budget_amount_micros is None
    assert parsed.daily_budget is None


# The amount field of the other period, and the period that owns it.
DAILY_AMOUNT = ("campaignBudget.amountMicros", "campaign_budget.amount_micros", "DAILY")
LIFETIME_AMOUNT = (
    "campaignBudget.totalAmountMicros",
    "campaign_budget.total_amount_micros",
    "CUSTOM_PERIOD",
)


@pytest.mark.parametrize(
    ("base", "removed", "foreign"),
    [
        # Both amount fields at once: the period's own field never excuses the other one.
        pytest.param(campaign_row, None, LIFETIME_AMOUNT, id="daily-with-both"),
        pytest.param(lifetime_campaign_row, None, DAILY_AMOUNT, id="custom-period-with-both"),
        # Only the other period's field.
        pytest.param(campaign_row, "campaignBudget.amountMicros", LIFETIME_AMOUNT, id="daily"),
        pytest.param(
            lifetime_campaign_row,
            "campaignBudget.totalAmountMicros",
            DAILY_AMOUNT,
            id="custom-period",
        ),
    ],
)
def test_amount_field_of_the_other_budget_period_is_a_defect(
    base: Callable[[], dict[str, Any]], removed: str | None, foreign: tuple[str, str, str]
) -> None:
    rest_keys, gaql_field, owner = foreign
    row = _with(base(), rest_keys, "20000000")
    if removed is not None:
        row = _without(row, removed)
    error = _defect("row_campaign", row)
    expected = f"absent unless campaign_budget.period is {owner}"
    _assert_defect(error, "row_campaign", f"{gaql_field}: expected {expected}")
    assert "20000000" not in error.message


def test_budget_without_a_period_rejects_the_custom_period_amount() -> None:
    # An absent period is Google's default DAILY, so a lifetime amount does not belong to it.
    row = _without(campaign_row(), "campaignBudget.period", "campaignBudget.amountMicros")
    error = _defect("row_campaign", _with(row, "campaignBudget.totalAmountMicros", "900000000"))
    _assert_defect(
        error,
        "row_campaign",
        "campaign_budget.total_amount_micros: expected absent unless campaign_budget.period is "
        "CUSTOM_PERIOD",
    )


@pytest.mark.parametrize("micros", ["0", "-1", "1500.5"])
def test_other_period_amount_is_rejected_whatever_its_value(micros: str) -> None:
    error = _defect(
        "row_campaign", _with(campaign_row(), "campaignBudget.totalAmountMicros", micros)
    )
    _assert_defect(
        error,
        "row_campaign",
        "campaign_budget.total_amount_micros: expected absent unless campaign_budget.period is "
        "CUSTOM_PERIOD",
    )


def test_explicitly_shared_custom_period_budget_is_a_defect() -> None:
    row = _with(lifetime_campaign_row(), "campaignBudget.explicitlyShared", True)
    error = _defect("row_campaign", row)
    _assert_defect(
        error,
        "row_campaign",
        "campaign_budget.explicitly_shared: expected false or absent when "
        "campaign_budget.period is CUSTOM_PERIOD",
    )


@pytest.mark.parametrize("shared", [False, None])
def test_custom_period_budget_that_is_not_shared_is_accepted(shared: bool | None) -> None:
    row = lifetime_campaign_row()
    if shared is not None:
        row = _with(row, "campaignBudget.explicitlyShared", shared)
    parsed = _parse("row_campaign", row)
    assert parsed.budget_period == "CUSTOM_PERIOD"
    assert parsed.budget_explicitly_shared is False
    assert parsed.total_budget == Decimal("1500.000000")


def test_budget_amount_is_exact_at_native_precision() -> None:
    parsed = _parse("row_campaign", _with(campaign_row(), "campaignBudget.amountMicros", "1"))
    assert parsed.budget_amount == Decimal("0.000001")
    row = _with(campaign_row(), "campaignBudget.amountMicros", "123456789")
    assert _parse("row_campaign", row).budget_amount == Decimal("123.456789")
    row = _with(campaign_row(), "campaignBudget.amountMicros", "0")
    parsed = _parse("row_campaign", row)
    assert parsed.budget_amount_micros == 0
    assert parsed.budget_amount == Decimal(0)


def test_budget_amount_at_int64_max_micros_is_a_defect() -> None:
    # INT64_MAX micros is 9223372036854.775807, 13 integral digits: numeric(18, 6) holds 12.
    row = _with(campaign_row(), "campaignBudget.amountMicros", INT64_MAX_TEXT)
    error = _defect("row_campaign", row)
    _assert_defect(
        error, "row_campaign", f"campaign_budget.amount_micros: expected {AMOUNT_MICROS_RANGE}"
    )


@pytest.mark.parametrize("micros", ["-1", INT64_OVERFLOW_TEXT])
def test_budget_amount_outside_the_int64_range_is_a_defect(micros: str) -> None:
    error = _defect("row_campaign", _with(campaign_row(), "campaignBudget.amountMicros", micros))
    _assert_defect(
        error, "row_campaign", f"campaign_budget.amount_micros: expected {AMOUNT_MICROS_RANGE}"
    )


def test_budget_amount_that_is_not_an_int64_is_a_defect() -> None:
    row = _with(lifetime_campaign_row(), "campaignBudget.totalAmountMicros", "1500.5")
    error = _defect("row_campaign", row)
    _assert_defect(
        error, "row_campaign", f"campaign_budget.total_amount_micros: expected {NOT_INT64}"
    )


def test_explicitly_shared_budget_is_flagged() -> None:
    row = _with(campaign_row(), "campaignBudget.explicitlyShared", True)
    assert _parse("row_campaign", row).budget_explicitly_shared is True


def test_absent_explicitly_shared_means_not_shared() -> None:
    parsed = _parse("row_campaign", _without(campaign_row(), "campaignBudget.explicitlyShared"))
    assert parsed.budget_explicitly_shared is False


@pytest.mark.parametrize("flag", ["true", 1, Decimal(1)])
def test_explicitly_shared_that_is_not_a_boolean_is_a_defect(flag: object) -> None:
    error = _defect("row_campaign", _with(campaign_row(), "campaignBudget.explicitlyShared", flag))
    _assert_defect(
        error, "row_campaign", "campaign_budget.explicitly_shared: expected a JSON boolean"
    )


def test_campaign_without_a_budget_has_no_budget_fields() -> None:
    parsed = _parse("row_campaign", _without(campaign_row(), "campaignBudget"))
    assert parsed.budget_id is None
    assert parsed.budget_period is None
    assert parsed.budget_amount_micros is None
    assert parsed.budget_amount is None
    assert parsed.budget_explicitly_shared is False
    assert parsed.daily_budget is None
    assert parsed.total_budget is None


@pytest.mark.parametrize("budget", [{}, {"resourceName": BUDGET_RN, "period": "DAILY"}])
def test_campaign_budget_without_an_id_is_a_defect(budget: dict[str, Any]) -> None:
    error = _defect("row_campaign", _with(campaign_row(), "campaignBudget", budget))
    _assert_defect(error, "row_campaign", "campaign_budget.id: expected an INT64 id")


def test_campaign_budget_that_is_not_an_object_is_a_defect() -> None:
    error = _defect("row_campaign", _with(campaign_row(), "campaignBudget", "222"))
    _assert_defect(error, "row_campaign", f"campaign_budget.id: expected {NOT_OBJECT_PATH}")


def test_budget_without_a_period_takes_googles_default_daily() -> None:
    # Off-contract: the contract requires period; Google documents DAILY as its default.
    parsed = _parse_off_contract("row_campaign", _without(campaign_row(), "campaignBudget.period"))
    assert parsed.budget_period == "DAILY"
    assert parsed.budget_amount == Decimal("50.000000")
    assert parsed.daily_budget == Decimal("50.000000")


def test_budget_periods_outside_daily_and_custom_period_are_defects() -> None:
    unmapped = _enum("row_campaign", "campaignBudget.period") - {"DAILY", "CUSTOM_PERIOD"}
    assert unmapped == {"UNKNOWN", "UNSPECIFIED"}
    for period in [*sorted(unmapped), "LIFETIME", "daily"]:
        error = _defect("row_campaign", _with(campaign_row(), "campaignBudget.period", period))
        _assert_defect(
            error, "row_campaign", "campaign_budget.period: expected one of DAILY, CUSTOM_PERIOD"
        )
    error = _defect("row_campaign", _with(campaign_row(), "campaignBudget.period", 2))
    _assert_defect(error, "row_campaign", "campaign_budget.period: expected a string")


def test_campaign_status_map_is_enabled_paused_removed() -> None:
    assert dict(CAMPAIGN_STATUS_MAP) == {
        "ENABLED": "active",
        "PAUSED": "paused",
        "REMOVED": "ended",
    }
    writable: Any = CAMPAIGN_STATUS_MAP
    with pytest.raises(TypeError):
        writable["UNKNOWN"] = "active"


@pytest.mark.parametrize(
    ("raw", "normalised"), [("ENABLED", "active"), ("PAUSED", "paused"), ("REMOVED", "ended")]
)
def test_campaign_status_is_normalised_and_kept_raw(raw: str, normalised: str) -> None:
    # REMOVED arrives only from Q-G2b, which has no status filter; it is snapshotted as ended.
    parsed = _parse("row_campaign", _with(campaign_row(), "campaign.status", raw))
    assert parsed.status == normalised
    assert parsed.raw_status == raw


@pytest.mark.parametrize(
    ("surface", "status_keys", "gaql_field"),
    [
        ("row_campaign", "campaign.status", "campaign.status"),
        ("row_ad_group", "adGroup.status", "ad_group.status"),
        ("row_ad_group_criterion", "adGroupCriterion.status", "ad_group_criterion.status"),
    ],
)
def test_unmapped_status_values_of_the_contract_are_defects(
    surface: str, status_keys: str, gaql_field: str
) -> None:
    unmapped = _enum(surface, status_keys) - set(CAMPAIGN_STATUS_MAP)
    assert unmapped == {"UNKNOWN", "UNSPECIFIED"}
    for raw in [*sorted(unmapped), "enabled", "ARCHIVED"]:
        error = _defect(surface, _with(BASE_ROWS[surface](), status_keys, raw))
        _assert_defect(error, surface, f"{gaql_field}: expected one of ENABLED, PAUSED, REMOVED")
    error = _defect(surface, _without(BASE_ROWS[surface](), status_keys))
    _assert_defect(error, surface, f"{gaql_field}: expected a string")


def test_campaign_without_a_channel_type_keeps_it_unknown() -> None:
    # Off-contract: the contract always returns advertisingChannelType; absence stays None.
    row = _without(campaign_row(), "campaign.advertisingChannelType")
    assert _parse_off_contract("row_campaign", row).advertising_channel_type is None


def _one_of(surface: str, rest_keys: str) -> str:
    """The defect expectation for an enum field: its contract's enum names, sorted."""
    return "one of " + ", ".join(sorted(_enum(surface, rest_keys)))


def test_advertising_channel_types_equal_the_contract_enum() -> None:
    channel_types = _enum("row_campaign", "campaign.advertisingChannelType")
    assert channel_types == parser._ADVERTISING_CHANNEL_TYPES
    assert len(channel_types) == 14
    assert {"UNKNOWN", "UNSPECIFIED"} <= channel_types


@pytest.mark.parametrize("channel_type", sorted(parser._ADVERTISING_CHANNEL_TYPES))
def test_campaign_channel_type_is_kept_raw(channel_type: str) -> None:
    row = _with(campaign_row(), "campaign.advertisingChannelType", channel_type)
    assert _parse("row_campaign", row).advertising_channel_type == channel_type


@pytest.mark.parametrize("channel_type", ["", "BOGUS", "search", "SEARCH "])
def test_campaign_channel_type_outside_the_contract_enum_is_a_defect(channel_type: str) -> None:
    row = _with(campaign_row(), "campaign.advertisingChannelType", channel_type)
    error = _defect("row_campaign", row)
    expected = _one_of("row_campaign", "campaign.advertisingChannelType")
    _assert_defect(error, "row_campaign", f"campaign.advertising_channel_type: expected {expected}")
    assert expected == (
        "one of DEMAND_GEN, DISPLAY, HOTEL, LOCAL, LOCAL_SERVICES, MULTI_CHANNEL, "
        "PERFORMANCE_MAX, SEARCH, SHOPPING, SMART, TRAVEL, UNKNOWN, UNSPECIFIED, VIDEO"
    )


def test_campaign_channel_type_that_is_not_a_string_is_a_defect() -> None:
    row = _with(campaign_row(), "campaign.advertisingChannelType", 2)
    error = _defect("row_campaign", row)
    _assert_defect(error, "row_campaign", "campaign.advertising_channel_type: expected a string")


def test_campaign_without_a_name_is_a_defect() -> None:
    error = _defect("row_campaign", _without(campaign_row(), "campaign.name"))
    _assert_defect(error, "row_campaign", "campaign.name: expected a string")


# --- Q-G3 daily campaign metrics -------------------------------------------------------------


def test_campaign_daily_row_parses_every_metric_exactly() -> None:
    parsed = _parse("row_campaign_daily", campaign_daily_row())
    assert parsed == CampaignDailyRow(
        campaign_id=111,
        date=date(2026, 9, 1),
        cost=Decimal("12.345678"),
        cost_micros=12_345_678,
        impressions=1000,
        clicks=50,
        conversions=Decimal("2.5"),
        conversions_value=Decimal("125.75"),
        search_budget_lost_impression_share=Decimal("0.9001"),
        search_impression_share=Decimal("0.0999"),
    )
    assert parsed.cost.as_tuple().exponent == -6
    assert type(parsed.date) is date
    assert isinstance(parsed.conversions, Decimal)


def test_campaign_daily_without_metrics_has_zero_counts_and_unavailable_shares() -> None:
    parsed = _parse("row_campaign_daily", _without(campaign_daily_row(), "metrics"))
    assert parsed.cost == Decimal(0)
    assert parsed.cost_micros == 0
    assert parsed.impressions == 0
    assert parsed.clicks == 0
    assert parsed.conversions == Decimal(0)
    assert parsed.conversions_value == Decimal(0)
    assert parsed.search_budget_lost_impression_share is None
    assert parsed.search_impression_share is None


def test_campaign_daily_with_empty_metrics_matches_absent_metrics() -> None:
    empty = _parse("row_campaign_daily", _with(campaign_daily_row(), "metrics", {}))
    absent = _parse("row_campaign_daily", _without(campaign_daily_row(), "metrics"))
    assert empty == absent


def test_campaign_daily_absent_impression_shares_are_unavailable_not_zero() -> None:
    row = _without(
        campaign_daily_row(),
        "metrics.searchBudgetLostImpressionShare",
        "metrics.searchImpressionShare",
    )
    parsed = _parse("row_campaign_daily", row)
    assert parsed.search_budget_lost_impression_share is None
    assert parsed.search_impression_share is None
    assert parsed.cost_micros == 12_345_678


def test_campaign_daily_whole_json_numbers_become_decimals() -> None:
    row = _with(campaign_daily_row(), "metrics.conversions", 3)
    row = _with(row, "metrics.conversionsValue", 0)
    row = _with(row, "metrics.searchImpressionShare", 1)
    parsed = _parse("row_campaign_daily", row)
    assert parsed.conversions == Decimal(3)
    assert isinstance(parsed.conversions, Decimal)
    assert parsed.conversions_value == Decimal(0)
    assert parsed.search_impression_share == Decimal(1)


def test_campaign_daily_decodes_wire_json_without_binary_floats() -> None:
    text = (
        '{"campaign": {"resourceName": "customers/1234567890/campaigns/111", "id": "111"},'
        ' "segments": {"date": "2026-09-30"},'
        ' "metrics": {"costMicros": "1500000", "clicks": "3", "conversions": 0.3,'
        ' "conversionsValue": 15, "searchImpressionShare": 0.0999}}'
    )
    row = json.loads(text, parse_float=Decimal)
    parsed = _parse("row_campaign_daily", row)
    assert parsed.date == date(2026, 9, 30)
    assert parsed.cost == Decimal("1.500000")
    assert parsed.clicks == 3
    assert parsed.impressions == 0
    assert parsed.conversions == Decimal("0.3")
    assert parsed.conversions_value == Decimal(15)
    assert parsed.search_impression_share == Decimal("0.0999")
    assert parsed.search_budget_lost_impression_share is None


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (Decimal("0.3"), Decimal("0.3")),
        (Decimal("0.000001"), Decimal("0.000001")),
        (Decimal("12345678.123456"), Decimal("12345678.123456")),
        (0.3, Decimal("0.3")),
        ("0.3", Decimal("0.3")),
        ("2", Decimal(2)),
    ],
)
def test_conversions_are_kept_exact_whatever_their_encoding(
    wire: object, expected: Decimal
) -> None:
    # Decimal is the wire encoding; a float or a numeric string is tolerated off-contract and
    # converted through str, never through Decimal(float).
    row = _with(campaign_daily_row(), "metrics.conversions", wire)
    parse = _parse if isinstance(wire, Decimal) else _parse_off_contract
    parsed = parse("row_campaign_daily", row)
    assert parsed.conversions == expected
    assert isinstance(parsed.conversions, Decimal)
    if isinstance(wire, float):
        assert parsed.conversions != Decimal(wire)


@pytest.mark.parametrize(
    "wire",
    [
        "abc",
        "",
        "1,5",
        True,
        False,
        [],
        {"value": 1},
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        float("inf"),
        float("nan"),
        "NaN",
        "Infinity",
    ],
)
def test_conversions_that_are_not_a_finite_number_are_a_defect(wire: object) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), "metrics.conversions", wire))
    _assert_defect(error, "row_campaign_daily", f"metrics.conversions: expected {NOT_NUMBER}")


@pytest.mark.parametrize(
    ("rest_keys", "gaql_field"),
    [
        ("metrics.conversionsValue", "metrics.conversions_value"),
        ("metrics.searchBudgetLostImpressionShare", "metrics.search_budget_lost_impression_share"),
        ("metrics.searchImpressionShare", "metrics.search_impression_share"),
    ],
)
def test_other_doubles_that_are_not_numbers_are_defects(rest_keys: str, gaql_field: str) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), rest_keys, "n/a"))
    _assert_defect(error, "row_campaign_daily", f"{gaql_field}: expected {NOT_NUMBER}")


IMPRESSION_SHARES: list[tuple[str, str, str]] = [
    (
        "metrics.searchBudgetLostImpressionShare",
        "metrics.search_budget_lost_impression_share",
        "search_budget_lost_impression_share",
    ),
    ("metrics.searchImpressionShare", "metrics.search_impression_share", "search_impression_share"),
]


@pytest.mark.parametrize(
    "wire",
    [
        0,
        1,
        Decimal(0),
        Decimal("0.0"),
        Decimal("0.5"),
        Decimal("1.0"),
        # Google's reporting sentinels: "> 90%" lost to budget and "< 10%" impression share.
        Decimal("0.9001"),
        Decimal("0.0999"),
        # The smallest positive share six places hold, and trailing zeros past the sixth place.
        Decimal("0.000001"),
        Decimal("0.10000000"),
    ],
)
@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), IMPRESSION_SHARES)
def test_impression_shares_from_zero_to_one_are_kept_as_returned(
    rest_keys: str, gaql_field: str, attribute: str, wire: int | Decimal
) -> None:
    row = _with(campaign_daily_row(), rest_keys, wire)
    value = getattr(_parse("row_campaign_daily", row), attribute)
    assert isinstance(value, Decimal)
    assert value.as_tuple() == Decimal(wire).as_tuple()


@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), IMPRESSION_SHARES)
def test_negative_zero_impression_share_is_zero_without_its_sign(
    rest_keys: str, gaql_field: str, attribute: str
) -> None:
    row = _with(campaign_daily_row(), rest_keys, Decimal("-0.0"))
    value = getattr(_parse("row_campaign_daily", row), attribute)
    assert value == 0
    assert not value.is_signed()


@pytest.mark.parametrize(
    "wire",
    [Decimal("-0.1"), Decimal("-0.0001"), Decimal("1.1"), Decimal("1.0001"), Decimal("9E+1"), 2],
)
@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), IMPRESSION_SHARES)
def test_impression_share_outside_zero_to_one_is_a_defect(
    rest_keys: str, gaql_field: str, attribute: str, wire: object
) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), rest_keys, wire))
    _assert_defect(error, "row_campaign_daily", f"{gaql_field}: expected {NOT_FRACTION}")


@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute"), IMPRESSION_SHARES)
def test_impression_share_tolerated_as_a_float_is_still_bounded(
    rest_keys: str, gaql_field: str, attribute: str
) -> None:
    row = _with(campaign_daily_row(), rest_keys, 0.25)
    assert getattr(_parse_off_contract("row_campaign_daily", row), attribute) == Decimal("0.25")
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), rest_keys, 1.5))
    _assert_defect(error, "row_campaign_daily", f"{gaql_field}: expected {NOT_FRACTION}")


def test_cost_micros_at_int64_max_is_a_defect() -> None:
    # INT64_MAX micros is 9223372036854.775807, 13 integral digits: numeric(18, 6) holds 12.
    row = _with(campaign_daily_row(), "metrics.costMicros", INT64_MAX_TEXT)
    error = _defect("row_campaign_daily", row)
    _assert_defect(
        error, "row_campaign_daily", f"metrics.cost_micros: expected {AMOUNT_MICROS_RANGE}"
    )


def test_cost_micros_as_a_json_integer_is_tolerated() -> None:
    row = _with(campaign_daily_row(), "metrics.costMicros", 1_500_000)
    parsed = _parse_off_contract("row_campaign_daily", row)
    assert parsed.cost_micros == 1_500_000
    assert parsed.cost == Decimal("1.500000")


@pytest.mark.parametrize(
    ("rest_keys", "gaql_field", "wire", "expected"),
    [
        ("metrics.costMicros", "metrics.cost_micros", "-1", AMOUNT_MICROS_RANGE),
        ("metrics.costMicros", "metrics.cost_micros", INT64_OVERFLOW_TEXT, AMOUNT_MICROS_RANGE),
        ("metrics.costMicros", "metrics.cost_micros", "1.5", NOT_INT64),
        ("metrics.impressions", "metrics.impressions", "-10", INT64_RANGE),
        ("metrics.impressions", "metrics.impressions", "ten", NOT_INT64),
        ("metrics.clicks", "metrics.clicks", "-1", INT64_RANGE),
        ("metrics.clicks", "metrics.clicks", True, NOT_INT64),
    ],
)
def test_negative_or_malformed_counts_are_defects(
    rest_keys: str, gaql_field: str, wire: object, expected: str
) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), rest_keys, wire))
    _assert_defect(error, "row_campaign_daily", f"{gaql_field}: expected {expected}")


@pytest.mark.parametrize("day", ["2024-02-29", "2026-12-31", "2026-01-01"])
def test_segments_date_is_parsed_as_the_native_day(day: str) -> None:
    parsed = _parse("row_campaign_daily", _with(campaign_daily_row(), "segments.date", day))
    assert parsed.date == date.fromisoformat(day)


@pytest.mark.parametrize(
    "day",
    [
        "2026-9-1",
        "2026-02-30",
        "2025-02-29",
        "2026-13-01",
        "2026-09-01T00:00:00",
        "20260901",
        "2026/09/01",
        " 2026-09-01",
        "",
        20260901,
        ["2026-09-01"],
    ],
)
def test_segments_date_that_is_not_a_valid_iso_day_is_a_defect(day: object) -> None:
    error = _defect("row_campaign_daily", _with(campaign_daily_row(), "segments.date", day))
    _assert_defect(error, "row_campaign_daily", f"segments.date: expected {NOT_DATE}")


def test_absent_segments_date_is_a_defect() -> None:
    error = _defect("row_campaign_daily", _without(campaign_daily_row(), "segments.date"))
    _assert_defect(error, "row_campaign_daily", f"segments.date: expected {NOT_DATE}")
    error = _defect("row_campaign_daily", _without(campaign_daily_row(), "segments"))
    _assert_defect(error, "row_campaign_daily", f"segments.date: expected {NOT_DATE}")


# --- Q-G4 ad groups ---------------------------------------------------------------------------


def test_ad_group_row_parses_with_its_campaign() -> None:
    parsed = _parse("row_ad_group", ad_group_row())
    assert parsed == AdGroupRow(
        ad_group_id=444, campaign_id=111, name="CRM software", status="paused", raw_status="PAUSED"
    )


def test_ad_group_without_its_campaign_is_a_defect() -> None:
    error = _defect("row_ad_group", _without(ad_group_row(), "campaign"))
    _assert_defect(error, "row_ad_group", "campaign.id: expected an INT64 id")


def test_ad_group_without_a_name_is_a_defect() -> None:
    error = _defect("row_ad_group", _without(ad_group_row(), "adGroup.name"))
    _assert_defect(error, "row_ad_group", "ad_group.name: expected a string")


# --- Q-G5 keyword view --------------------------------------------------------------------------


def test_keyword_view_row_parses_scores_cpc_and_metrics() -> None:
    parsed = _parse("row_keyword_view", keyword_view_row())
    assert parsed == KeywordViewRow(
        ad_group_id=444,
        criterion_id=555,
        date=date(2026, 9, 2),
        historical_quality_score=7,
        # 1,234,567.5 micros is 1.2345675, a tie rounded half-even to 1.234568.
        average_cpc=Decimal("1.234568"),
        cost=Decimal("24.691350"),
        cost_micros=24_691_350,
        clicks=20,
        impressions=400,
        conversions=Decimal("1.5"),
    )
    assert parsed.average_cpc is not None
    assert parsed.average_cpc.as_tuple().exponent == -6


@pytest.mark.parametrize(
    ("cost_micros", "clicks", "average_cpc", "expected"),
    [
        ("2469133", "2", Decimal("1234566.5"), Decimal("1.234566")),
        ("2469135", "2", Decimal("1234567.5"), Decimal("1.234568")),
        ("10000000", "3", Decimal("3333333.3333333335"), Decimal("3.333333")),
        ("4000000", "2", 2000000, Decimal("2.000000")),
        ("1", "1", 1, Decimal("0.000001")),
    ],
)
def test_average_cpc_converts_micros_rounding_half_even(
    cost_micros: str, clicks: str, average_cpc: object, expected: Decimal
) -> None:
    row = _with(keyword_view_row(), "metrics.costMicros", cost_micros)
    row = _with(row, "metrics.clicks", clicks)
    row = _with(row, "metrics.averageCpc", average_cpc)
    parsed = _parse("row_keyword_view", row)
    assert parsed.average_cpc == expected
    assert parsed.average_cpc is not None
    assert parsed.average_cpc.as_tuple().exponent == -6


def test_keyword_view_without_metrics_has_unavailable_score_and_cpc() -> None:
    parsed = _parse("row_keyword_view", _without(keyword_view_row(), "metrics"))
    assert parsed.historical_quality_score is None
    assert parsed.average_cpc is None
    assert parsed.cost == Decimal(0)
    assert parsed.cost_micros == 0
    assert parsed.clicks == 0
    assert parsed.impressions == 0
    assert parsed.conversions == Decimal(0)


def test_keyword_view_day_without_a_score_keeps_its_other_metrics() -> None:
    row = _without(keyword_view_row(), "metrics.historicalQualityScore")
    parsed = _parse("row_keyword_view", row)
    assert parsed.historical_quality_score is None
    assert parsed.clicks == 20


@pytest.mark.parametrize(("wire", "expected"), [("1", 1), ("5", 5), ("10", 10)])
def test_historical_quality_score_spans_one_to_ten(wire: str, expected: int) -> None:
    row = _with(keyword_view_row(), "metrics.historicalQualityScore", wire)
    assert _parse("row_keyword_view", row).historical_quality_score == expected


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("0", "an integer from 1 to 10"),
        ("11", "an integer from 1 to 10"),
        ("-3", "an integer from 1 to 10"),
        ("7.5", NOT_INT64),
        ("seven", NOT_INT64),
    ],
)
def test_historical_quality_score_outside_one_to_ten_is_a_defect(wire: str, expected: str) -> None:
    row = _with(keyword_view_row(), "metrics.historicalQualityScore", wire)
    error = _defect("row_keyword_view", row)
    _assert_defect(
        error, "row_keyword_view", f"metrics.historical_quality_score: expected {expected}"
    )


@pytest.mark.parametrize(
    "average_cpc",
    # 999999999999999999.5 micros is a tie that half-even rounds up to 10**18 micros: 10**12.
    [Decimal("1E+40"), Decimal("1E+38"), Decimal("1E+18"), Decimal("999999999999999999.5"), 10**18],
)
def test_average_cpc_beyond_numeric_range_is_a_defect(average_cpc: object) -> None:
    row = _with(keyword_view_row(), "metrics.averageCpc", average_cpc)
    error = _defect("row_keyword_view", row)
    _assert_defect(error, "row_keyword_view", f"metrics.average_cpc: expected {CPC_RANGE}")


@pytest.mark.parametrize(
    "average_cpc",
    # -0.4 micros would round to zero; the sign is checked before rounding.
    [Decimal("-1234567.5"), Decimal("-1"), Decimal("-0.4"), Decimal("-1E+40"), -2000000],
)
def test_negative_average_cpc_is_a_defect(average_cpc: object) -> None:
    # Average CPC is cost divided by clicks, neither of which is ever negative.
    row = _with(keyword_view_row(), "metrics.averageCpc", average_cpc)
    error = _defect("row_keyword_view", row)
    _assert_defect(error, "row_keyword_view", f"metrics.average_cpc: expected {NOT_NON_NEGATIVE}")


def test_average_cpc_as_a_numeric_string_is_tolerated() -> None:
    row = _with(keyword_view_row(), "metrics.averageCpc", "1234567.5")
    assert _parse_off_contract("row_keyword_view", row).average_cpc == Decimal("1.234568")


def test_average_cpc_that_is_not_a_number_is_a_defect() -> None:
    row = _with(keyword_view_row(), "metrics.averageCpc", "n/a")
    error = _defect("row_keyword_view", row)
    _assert_defect(error, "row_keyword_view", f"metrics.average_cpc: expected {NOT_NUMBER}")


def test_keyword_view_without_its_criterion_id_is_a_defect() -> None:
    row = _without(keyword_view_row(), "adGroupCriterion.criterionId")
    error = _defect("row_keyword_view", row)
    _assert_defect(
        error, "row_keyword_view", "ad_group_criterion.criterion_id: expected an INT64 id"
    )
    error = _defect("row_keyword_view", _without(keyword_view_row(), "adGroup"))
    _assert_defect(error, "row_keyword_view", "ad_group.id: expected an INT64 id")


# --- Q-G6 search terms ------------------------------------------------------------------------


def test_search_term_row_takes_the_matched_criterion_from_its_resource_name() -> None:
    parsed = _parse("row_search_term_view", search_term_row())
    assert parsed == SearchTermRow(
        campaign_id=111,
        ad_group_id=444,
        date=date(2026, 9, 3),
        search_term="crm pricing",
        term_status="NONE",
        matched_criterion_id="555",
        matched_keyword_text="crm software",
        matched_match_type="PHRASE",
        cost=Decimal("3.500000"),
        cost_micros=3_500_000,
        clicks=7,
        impressions=90,
        conversions=Decimal("0.3"),
        conversions_value=Decimal(15),
    )


def test_search_term_without_a_keyword_segment_matched_no_criterion() -> None:
    parsed = _parse("row_search_term_view", _without(search_term_row(), "segments.keyword"))
    assert parsed.matched_criterion_id == ""
    assert parsed.matched_keyword_text is None
    assert parsed.matched_match_type is None
    assert parsed.search_term == "crm pricing"


def test_search_term_keyword_segment_parts_are_each_optional() -> None:
    row = _without(search_term_row(), "segments.keyword.info")
    parsed = _parse("row_search_term_view", row)
    assert parsed.matched_criterion_id == "555"
    assert parsed.matched_keyword_text is None
    assert parsed.matched_match_type is None
    row = _without(search_term_row(), "segments.keyword.adGroupCriterion")
    parsed = _parse("row_search_term_view", row)
    assert parsed.matched_criterion_id == ""
    assert parsed.matched_keyword_text == "crm software"
    assert parsed.matched_match_type == "PHRASE"
    parsed = _parse("row_search_term_view", _with(search_term_row(), "segments.keyword", {}))
    assert parsed.matched_criterion_id == ""


@pytest.mark.parametrize(
    ("resource_name", "expected"),
    [
        (f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~0", "0"),
        (f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~000555", "555"),
        (f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~{INT64_MAX}", INT64_MAX_TEXT),
    ],
)
def test_matched_criterion_id_is_returned_in_canonical_decimal_form(
    resource_name: str, expected: str
) -> None:
    row = _with(search_term_row(), "segments.keyword.adGroupCriterion", resource_name)
    assert _parse("row_search_term_view", row).matched_criterion_id == expected


@pytest.mark.parametrize(
    "resource_name",
    [
        AD_GROUP_RN,
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}",
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~",
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~abc",
        f"{CUSTOMER_RN}/adGroupCriteria/~{CRITERION}",
        f"adGroupCriteria/{AD_GROUP}~{CRITERION}",
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~{CRITERION}/extra",
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~99999999999999999999",
        CRITERION,
        "",
    ],
)
def test_matched_criterion_that_is_not_an_ad_group_criteria_name_is_a_defect(
    resource_name: str,
) -> None:
    row = _with(search_term_row(), "segments.keyword.adGroupCriterion", resource_name)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error,
        "row_search_term_view",
        f"segments.keyword.ad_group_criterion: expected {CRITERION_FORM}",
    )


def test_matched_criterion_of_the_rows_own_ad_group_gives_its_criterion_id() -> None:
    row = _with(search_term_row(), "adGroup.id", "111")
    row = _with(row, "adGroup.resourceName", f"{CUSTOMER_RN}/adGroups/111")
    resource_name = f"{CUSTOMER_RN}/adGroupCriteria/111~333"
    parsed = _parse(
        "row_search_term_view", _with(row, "segments.keyword.adGroupCriterion", resource_name)
    )
    assert parsed.ad_group_id == 111
    assert parsed.matched_criterion_id == "333"
    # Ad-group ids compare as numbers, so zero padding on either side is the same ad group.
    resource_name = f"{CUSTOMER_RN}/adGroupCriteria/000111~333"
    row = _with(row, "segments.keyword.adGroupCriterion", resource_name)
    assert _parse("row_search_term_view", row).matched_criterion_id == "333"
    row = _with(row, "adGroup.id", "0111")
    assert _parse("row_search_term_view", row).matched_criterion_id == "333"


@pytest.mark.parametrize("resource_ad_group", ["222", "0", "1111", "11", INT64_MAX_TEXT])
def test_matched_criterion_of_another_ad_group_is_a_defect(resource_ad_group: str) -> None:
    # A criterion id is unique only within its ad group: 222~333 is not criterion 333 of 111.
    row = _with(search_term_row(), "adGroup.id", "111")
    resource_name = f"{CUSTOMER_RN}/adGroupCriteria/{resource_ad_group}~333"
    row = _with(row, "segments.keyword.adGroupCriterion", resource_name)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error,
        "row_search_term_view",
        "segments.keyword.ad_group_criterion: expected an adGroupCriteria resource name of the "
        "row's ad group (ad_group.id)",
    )
    assert "111" not in error.message
    assert "333" not in error.message


@pytest.mark.parametrize(
    "resource_name",
    [
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~{INT64_OVERFLOW_TEXT}",
        f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP}~9999999999999999999",
        f"{CUSTOMER_RN}/adGroupCriteria/{INT64_OVERFLOW_TEXT}~{CRITERION}",
        f"customers/{INT64_OVERFLOW_TEXT}/adGroupCriteria/{AD_GROUP}~{CRITERION}",
    ],
)
def test_matched_criterion_with_an_id_beyond_int64_is_a_defect(resource_name: str) -> None:
    row = _with(search_term_row(), "segments.keyword.adGroupCriterion", resource_name)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error,
        "row_search_term_view",
        "segments.keyword.ad_group_criterion: expected an adGroupCriteria resource name whose "
        f"ids are integers from 0 to {INT64_MAX}",
    )


def test_matched_criterion_that_is_not_a_string_is_a_defect() -> None:
    row = _with(search_term_row(), "segments.keyword.adGroupCriterion", 555)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error, "row_search_term_view", "segments.keyword.ad_group_criterion: expected a string"
    )


def test_search_term_statuses_equal_the_contract_enum() -> None:
    assert _enum("row_search_term_view", "searchTermView.status") == SEARCH_TERM_STATUSES


@pytest.mark.parametrize("status", sorted(SEARCH_TERM_STATUSES))
def test_every_search_term_status_is_stored_raw(status: str) -> None:
    # UNKNOWN and UNSPECIFIED are accepted wire values that no rule acts on.
    row = _with(search_term_row(), "searchTermView.status", status)
    assert _parse("row_search_term_view", row).term_status == status


@pytest.mark.parametrize("status", ["TARGETED", "none", "Added", ""])
def test_search_term_status_outside_the_contract_enum_is_a_defect(status: str) -> None:
    error = _defect(
        "row_search_term_view", _with(search_term_row(), "searchTermView.status", status)
    )
    _assert_defect(
        error,
        "row_search_term_view",
        "search_term_view.status: expected one of "
        "ADDED, ADDED_EXCLUDED, EXCLUDED, NONE, UNKNOWN, UNSPECIFIED",
    )


def test_search_term_status_that_is_not_a_string_is_a_defect() -> None:
    error = _defect("row_search_term_view", _without(search_term_row(), "searchTermView.status"))
    _assert_defect(error, "row_search_term_view", "search_term_view.status: expected a string")


def test_search_term_without_its_text_is_a_defect() -> None:
    row = _without(search_term_row(), "searchTermView.searchTerm")
    error = _defect("row_search_term_view", row)
    _assert_defect(error, "row_search_term_view", "search_term_view.search_term: expected a string")


def test_search_term_match_types_equal_the_contract_enum() -> None:
    match_types = _enum("row_search_term_view", "segments.keyword.info.matchType")
    assert match_types == parser._KEYWORD_MATCH_TYPES
    assert match_types == {"BROAD", "EXACT", "PHRASE", "UNKNOWN", "UNSPECIFIED"}


@pytest.mark.parametrize("match_type", sorted(parser._KEYWORD_MATCH_TYPES))
def test_search_term_match_type_is_kept_raw(match_type: str) -> None:
    row = _with(search_term_row(), "segments.keyword.info.matchType", match_type)
    assert _parse("row_search_term_view", row).matched_match_type == match_type


@pytest.mark.parametrize("match_type", ["", "BOGUS", "phrase", "BROAD_MATCH"])
def test_search_term_match_type_outside_the_contract_enum_is_a_defect(match_type: str) -> None:
    row = _with(search_term_row(), "segments.keyword.info.matchType", match_type)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error,
        "row_search_term_view",
        "segments.keyword.info.match_type: expected "
        + _one_of("row_search_term_view", "segments.keyword.info.matchType"),
    )


def test_search_term_without_a_match_type_keeps_it_unknown() -> None:
    # Off-contract: info requires matchType; an absent match type stays None, never a default.
    row = _without(search_term_row(), "segments.keyword.info.matchType")
    parsed = _parse_off_contract("row_search_term_view", row)
    assert parsed.matched_match_type is None
    assert parsed.matched_keyword_text == "crm software"
    row = _with(search_term_row(), "segments.keyword.info.matchType", 2)
    error = _defect("row_search_term_view", row)
    _assert_defect(
        error, "row_search_term_view", "segments.keyword.info.match_type: expected a string"
    )


def test_search_term_without_metrics_has_zero_additive_metrics() -> None:
    parsed = _parse("row_search_term_view", _without(search_term_row(), "metrics"))
    assert parsed.cost == Decimal(0)
    assert parsed.cost_micros == 0
    assert parsed.clicks == 0
    assert parsed.impressions == 0
    assert parsed.conversions == Decimal(0)
    assert parsed.conversions_value == Decimal(0)


# --- Q-G7 keywords and quality info -----------------------------------------------------------


def test_keyword_row_parses_text_match_type_and_quality_score() -> None:
    parsed = _parse("row_ad_group_criterion", keyword_row())
    assert parsed == KeywordRow(
        ad_group_id=444,
        criterion_id=555,
        campaign_id=111,
        text="crm software",
        match_type="EXACT",
        status="active",
        raw_status="ENABLED",
        quality_score=8,
    )


def test_keyword_without_quality_info_has_an_unavailable_score() -> None:
    parsed = _parse(
        "row_ad_group_criterion", _without(keyword_row(), "adGroupCriterion.qualityInfo")
    )
    assert parsed.quality_score is None
    row = _with(keyword_row(), "adGroupCriterion.qualityInfo", {})
    assert _parse("row_ad_group_criterion", row).quality_score is None


@pytest.mark.parametrize("score", [1, 10])
def test_quality_score_bounds_are_inclusive(score: int) -> None:
    row = _with(keyword_row(), "adGroupCriterion.qualityInfo.qualityScore", score)
    assert _parse("row_ad_group_criterion", row).quality_score == score


@pytest.mark.parametrize(
    ("wire", "expected"),
    [("7", 7), (Decimal("7"), 7), (Decimal("7.0"), 7), (Decimal("1E+1"), 10)],
)
def test_quality_score_tolerates_int_strings_and_integral_decimals(
    wire: object, expected: int
) -> None:
    row = _with(keyword_row(), "adGroupCriterion.qualityInfo.qualityScore", wire)
    parsed = _parse_off_contract("row_ad_group_criterion", row)
    assert parsed.quality_score == expected
    assert type(parsed.quality_score) is int


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (0, "an integer from 1 to 10"),
        (11, "an integer from 1 to 10"),
        (-1, "an integer from 1 to 10"),
        (Decimal("7.5"), NOT_INT64),
        (True, NOT_INT64),
        ("high", NOT_INT64),
    ],
)
def test_quality_score_outside_one_to_ten_is_a_defect(wire: object, expected: str) -> None:
    row = _with(keyword_row(), "adGroupCriterion.qualityInfo.qualityScore", wire)
    error = _defect("row_ad_group_criterion", row)
    _assert_defect(
        error,
        "row_ad_group_criterion",
        f"ad_group_criterion.quality_info.quality_score: expected {expected}",
    )


def test_keyword_match_types_equal_the_contract_enum() -> None:
    match_types = _enum("row_ad_group_criterion", "adGroupCriterion.keyword.matchType")
    assert match_types == parser._KEYWORD_MATCH_TYPES


@pytest.mark.parametrize("match_type", sorted(parser._KEYWORD_MATCH_TYPES))
def test_keyword_match_type_is_kept_raw(match_type: str) -> None:
    row = _with(keyword_row(), "adGroupCriterion.keyword.matchType", match_type)
    assert _parse("row_ad_group_criterion", row).match_type == match_type


@pytest.mark.parametrize("match_type", ["", "BOGUS", "exact", "NEAR_EXACT"])
def test_keyword_match_type_outside_the_contract_enum_is_a_defect(match_type: str) -> None:
    row = _with(keyword_row(), "adGroupCriterion.keyword.matchType", match_type)
    error = _defect("row_ad_group_criterion", row)
    _assert_defect(
        error,
        "row_ad_group_criterion",
        "ad_group_criterion.keyword.match_type: expected "
        + _one_of("row_ad_group_criterion", "adGroupCriterion.keyword.matchType"),
    )


def test_keyword_paused_status_is_normalised() -> None:
    row = _with(keyword_row(), "adGroupCriterion.status", "PAUSED")
    parsed = _parse("row_ad_group_criterion", row)
    assert parsed.status == "paused"
    assert parsed.raw_status == "PAUSED"


def test_keyword_without_text_or_match_type_is_a_defect() -> None:
    error = _defect("row_ad_group_criterion", _without(keyword_row(), "adGroupCriterion.keyword"))
    _assert_defect(
        error, "row_ad_group_criterion", "ad_group_criterion.keyword.text: expected a string"
    )
    row = _without(keyword_row(), "adGroupCriterion.keyword.matchType")
    error = _defect("row_ad_group_criterion", row)
    _assert_defect(
        error, "row_ad_group_criterion", "ad_group_criterion.keyword.match_type: expected a string"
    )
    row = _with(keyword_row(), "adGroupCriterion.keyword", "crm software")
    error = _defect("row_ad_group_criterion", row)
    _assert_defect(
        error,
        "row_ad_group_criterion",
        f"ad_group_criterion.keyword.text: expected {NOT_OBJECT_PATH}",
    )


def test_keyword_without_its_ad_group_or_criterion_id_is_a_defect() -> None:
    error = _defect("row_ad_group_criterion", _without(keyword_row(), "adGroup.id"))
    _assert_defect(error, "row_ad_group_criterion", "ad_group.id: expected an INT64 id")
    row = _with(keyword_row(), "adGroupCriterion.criterionId", INT64_OVERFLOW_TEXT)
    error = _defect("row_ad_group_criterion", row)
    _assert_defect(
        error, "row_ad_group_criterion", f"ad_group_criterion.criterion_id: expected {INT64_RANGE}"
    )


# --- Q-G8 conversion actions ------------------------------------------------------------------


def test_conversion_action_row_keeps_its_counting_contract_raw() -> None:
    parsed = _parse("row_conversion_action", conversion_action_row())
    assert parsed == ConversionActionRow(
        conversion_action_id=666,
        name="Demo request",
        status="ENABLED",
        include_in_conversions_metric=True,
        counting_type="ONE_PER_CLICK",
        attribution_model="GOOGLE_ADS_LAST_CLICK",
    )


def test_conversion_action_with_weighted_many_per_click_counting_is_kept_raw() -> None:
    row = _with(conversion_action_row(), "conversionAction.countingType", "MANY_PER_CLICK")
    row = _with(
        row,
        "conversionAction.attributionModelSettings.attributionModel",
        "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
    )
    parsed = _parse("row_conversion_action", row)
    assert parsed.counting_type == "MANY_PER_CLICK"
    assert parsed.attribution_model == "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"


@pytest.mark.parametrize("status", ["ENABLED", "HIDDEN", "REMOVED", "UNKNOWN", "UNSPECIFIED"])
def test_conversion_action_status_is_kept_raw_never_normalised(status: str) -> None:
    row = _with(conversion_action_row(), "conversionAction.status", status)
    assert _parse("row_conversion_action", row).status == status


# Each Q-G8 enum field: its REST key path, GAQL field, parsed attribute and parser constant.
ACTION_ENUMS = [
    pytest.param(
        "conversionAction.status",
        "conversion_action.status",
        "status",
        parser._CONVERSION_ACTION_STATUSES,
        id="status",
    ),
    pytest.param(
        "conversionAction.countingType",
        "conversion_action.counting_type",
        "counting_type",
        parser._COUNTING_TYPES,
        id="counting_type",
    ),
    pytest.param(
        "conversionAction.attributionModelSettings.attributionModel",
        "conversion_action.attribution_model_settings.attribution_model",
        "attribution_model",
        parser._ATTRIBUTION_MODELS,
        id="attribution_model",
    ),
]


@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute", "values"), ACTION_ENUMS)
def test_conversion_action_enums_equal_the_contract_enum(
    rest_keys: str, gaql_field: str, attribute: str, values: frozenset[str]
) -> None:
    assert _enum("row_conversion_action", rest_keys) == values
    assert {"UNKNOWN", "UNSPECIFIED"} <= values
    assert gaql_field.endswith(attribute)


def test_attribution_models_keep_the_retired_rule_based_models() -> None:
    assert len(parser._ATTRIBUTION_MODELS) == 9
    assert {
        "GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK",
        "GOOGLE_SEARCH_ATTRIBUTION_LINEAR",
        "GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED",
        "GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY",
    } <= parser._ATTRIBUTION_MODELS


@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute", "values"), ACTION_ENUMS)
def test_every_conversion_action_enum_value_is_kept_raw(
    rest_keys: str, gaql_field: str, attribute: str, values: frozenset[str]
) -> None:
    for value in sorted(values):
        row = _with(conversion_action_row(), rest_keys, value)
        assert getattr(_parse("row_conversion_action", row), attribute) == value


@pytest.mark.parametrize("wire", ["", "BOGUS", "last_click", "ENABLED "])
@pytest.mark.parametrize(("rest_keys", "gaql_field", "attribute", "values"), ACTION_ENUMS)
def test_conversion_action_enum_outside_the_contract_is_a_defect(
    rest_keys: str, gaql_field: str, attribute: str, values: frozenset[str], wire: str
) -> None:
    error = _defect("row_conversion_action", _with(conversion_action_row(), rest_keys, wire))
    if attribute == "attribution_model":
        # Listing the nine attribution model names would overflow QueryDefect's 300-character
        # message cap, so the defect names the enum and where its names are listed instead.
        message = (
            "conversion_action.attribution_model_settings.attribution_model: expected "
            "a v25 AttributionModelEnum name listed in the row_conversion_action contract"
        )
    else:
        message = f"{gaql_field}: expected {_one_of('row_conversion_action', rest_keys)}"
    _assert_defect(error, "row_conversion_action", message)
    assert len(error.message) <= 300
    assert "…" not in error.message
    assert wire == "" or wire not in error.message


# Each field the parser reads with _RowReader.enum or optional_enum: surface, REST path, constant.
ENUM_FIELDS = [
    ("row_campaign", "campaign.advertisingChannelType", parser._ADVERTISING_CHANNEL_TYPES),
    ("row_search_term_view", "segments.keyword.info.matchType", parser._KEYWORD_MATCH_TYPES),
    ("row_ad_group_criterion", "adGroupCriterion.keyword.matchType", parser._KEYWORD_MATCH_TYPES),
    ("row_conversion_action", "conversionAction.status", parser._CONVERSION_ACTION_STATUSES),
    ("row_conversion_action", "conversionAction.countingType", parser._COUNTING_TYPES),
    (
        "row_conversion_action",
        "conversionAction.attributionModelSettings.attributionModel",
        parser._ATTRIBUTION_MODELS,
    ),
]


def test_every_enum_defect_message_is_complete_within_the_message_cap() -> None:
    assert len({values for _, _, values in ENUM_FIELDS}) == 5
    for surface, rest_keys, values in ENUM_FIELDS:
        assert _enum(surface, rest_keys) == values
        error = _defect(surface, _with(BASE_ROWS[surface](), rest_keys, "BOGUS"))
        assert error.code == "validation"
        assert len(error.message) <= 300, rest_keys
        assert "…" not in error.message, rest_keys


def test_conversion_action_with_empty_attribution_settings_has_an_unknown_model() -> None:
    # Off-contract: attributionModelSettings requires attributionModel when it is present.
    row = _with(conversion_action_row(), "conversionAction.attributionModelSettings", {})
    assert _parse_off_contract("row_conversion_action", row).attribution_model is None


def test_conversion_action_include_flag_false_or_absent_is_false() -> None:
    row = _with(conversion_action_row(), "conversionAction.includeInConversionsMetric", False)
    assert _parse("row_conversion_action", row).include_in_conversions_metric is False
    row = _without(conversion_action_row(), "conversionAction.includeInConversionsMetric")
    assert _parse("row_conversion_action", row).include_in_conversions_metric is False


def test_conversion_action_without_attribution_settings_has_an_unknown_model() -> None:
    row = _without(conversion_action_row(), "conversionAction.attributionModelSettings")
    parsed = _parse("row_conversion_action", row)
    assert parsed.attribution_model is None
    assert parsed.counting_type == "ONE_PER_CLICK"


def test_conversion_action_without_a_counting_type_keeps_it_unknown() -> None:
    # Off-contract: the contract always returns countingType; B4 reads None as unknown.
    row = _without(conversion_action_row(), "conversionAction.countingType")
    assert _parse_off_contract("row_conversion_action", row).counting_type is None


def test_conversion_action_include_flag_that_is_not_a_boolean_is_a_defect() -> None:
    row = _with(conversion_action_row(), "conversionAction.includeInConversionsMetric", "true")
    error = _defect("row_conversion_action", row)
    _assert_defect(
        error,
        "row_conversion_action",
        "conversion_action.include_in_conversions_metric: expected a JSON boolean",
    )


def test_conversion_action_without_a_name_or_status_is_a_defect() -> None:
    row = _without(conversion_action_row(), "conversionAction.name")
    error = _defect("row_conversion_action", row)
    _assert_defect(error, "row_conversion_action", "conversion_action.name: expected a string")
    row = _with(conversion_action_row(), "conversionAction.status", 2)
    error = _defect("row_conversion_action", row)
    _assert_defect(error, "row_conversion_action", "conversion_action.status: expected a string")


def test_conversion_action_attribution_settings_that_are_not_an_object_are_a_defect() -> None:
    row = _with(conversion_action_row(), "conversionAction.attributionModelSettings", "LAST_CLICK")
    error = _defect("row_conversion_action", row)
    _assert_defect(
        error,
        "row_conversion_action",
        "conversion_action.attribution_model_settings.attribution_model: "
        f"expected {NOT_OBJECT_PATH}",
    )
