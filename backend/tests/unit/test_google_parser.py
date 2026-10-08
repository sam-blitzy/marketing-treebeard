"""Google row parsing on verified contracts: ``ads_engine.connectors.google_n8n.parser``.

Every payload here is traceable to a contract in ``backend/tests/contracts/google/`` (AAP 0.11.2
"External fixtures are traceable to verified contracts"):

* each row contract's documented sample, when it publishes one, is parsed verbatim;
* every row of the simulator's committed snapshots ``backend/tests/fixtures/google__q_g*.json``
  (listed by ``fixtures/manifest.json``) is validated with ``payloads.validate_google_rows`` and
  then parsed with ``PARSERS[QUERIES[query_id].row_surface]``, the query id read back from the
  request's own GAQL text;
* every redacted live capture under ``contracts/google/captured/`` goes through the same path,
  and none being recorded yet is a valid state, not a skip;
* every hand-built row first passes ``payloads.validate_response`` against its ``row_*``
  contract (the :func:`on_contract` fixture), so no test encodes a field, type or enum value the
  contract does not define.

Rows are parsed in the form ``connectors/http.py`` hands them to the parser: JSON decoded with
``parse_float=Decimal``, so DOUBLE metrics arrive as exact decimals and INT64 values as strings.
Two kinds of row deliberately leave the contract, and each test that builds one says so: wire
encodings the parser tolerates although the contract types the field otherwise (a numeric
string for a DOUBLE, a JSON integer for an INT64 id), and malformed rows for defect branches.

``Decimal`` results are compared with exact decimals, never floats. Everything is pure and
synchronous: no database and no network.
"""

import copy
import json
from collections.abc import Callable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ads_engine.connectors import errors
from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.google_n8n.parser import (
    CAMPAIGN_STATUS_MAP,
    PARSERS,
    AdGroupRow,
    CampaignDailyRow,
    CampaignRow,
    ConversionActionRow,
    CustomerRow,
    GoogleAdsRow,
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
from ads_engine.connectors.google_n8n.queries import INT64_MAX, QUERIES, query_id_for, rest_path
from ads_engine.simulation import payloads
from ads_engine.simulation.payloads import Contract, ContractViolation

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------------------------
# Surfaces, fixtures on disk and hand-built rows
# ---------------------------------------------------------------------------------------------

ROW_SURFACES: tuple[str, ...] = tuple(sorted({t.row_surface for t in QUERIES.values()}))
"""The eight ``row_*`` contracts the GAQL templates name."""

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
"""The dataclass each surface's parser returns."""

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
"""``backend/tests/fixtures/``: simulator snapshots rendered from the contracts (seed 42)."""


def _google_fixture_files() -> tuple[str, ...]:
    """Return the file names of the manifest's Google snapshots, in manifest order."""
    manifest = json.loads((FIXTURES_DIR / "manifest.json").read_text(encoding="utf-8"))
    return tuple(entry["file"] for entry in manifest["entries"] if entry["source"] == "google")


GOOGLE_FIXTURES = _google_fixture_files()

CUSTOMER_ID = "1234567890"
CUSTOMER_RN = f"customers/{CUSTOMER_ID}"
CAMPAIGN_ID = "111"
BUDGET_ID = "222"
AD_GROUP_ID = "333"
CRITERION_ID = "444"
ACTION_ID = "555"
CRITERION_RN = f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP_ID}~{CRITERION_ID}"
CAMPAIGN_REF = {"resourceName": f"{CUSTOMER_RN}/campaigns/{CAMPAIGN_ID}", "id": CAMPAIGN_ID}
AD_GROUP_REF = {"resourceName": f"{CUSTOMER_RN}/adGroups/{AD_GROUP_ID}", "id": AD_GROUP_ID}

# One conforming row per surface, built only from keys, types and enum values its contract
# defines. Tests take a deep copy through _base() and change one thing at a time.
BASE_ROWS: Mapping[str, dict[str, Any]] = {
    "row_customer": {
        "customer": {
            "resourceName": CUSTOMER_RN,
            "id": CUSTOMER_ID,
            "descriptiveName": "Acme Analytics",
            "currencyCode": "EUR",
            "timeZone": "Europe/Berlin",
        }
    },
    "row_campaign": {
        "campaign": {
            **CAMPAIGN_REF,
            "name": "Brand - Search",
            "status": "ENABLED",
            "advertisingChannelType": "SEARCH",
        },
        "campaignBudget": {
            "resourceName": f"{CUSTOMER_RN}/campaignBudgets/{BUDGET_ID}",
            "id": BUDGET_ID,
            "amountMicros": "12345678",
            "period": "DAILY",
            "explicitlyShared": True,
        },
    },
    "row_campaign_daily": {
        "campaign": dict(CAMPAIGN_REF),
        "segments": {"date": "2026-09-04"},
        "metrics": {
            "costMicros": "1500000",
            "impressions": "340",
            "clicks": "12",
            "conversions": 0.3,
            "conversionsValue": 45.5,
            "searchBudgetLostImpressionShare": 0.12,
            "searchImpressionShare": 0.6,
        },
    },
    "row_ad_group": {
        "adGroup": {**AD_GROUP_REF, "name": "Brand terms", "status": "ENABLED"},
        "campaign": dict(CAMPAIGN_REF),
    },
    "row_keyword_view": {
        "keywordView": {"resourceName": f"{CUSTOMER_RN}/keywordViews/{AD_GROUP_ID}~{CRITERION_ID}"},
        "adGroupCriterion": {"resourceName": CRITERION_RN, "criterionId": CRITERION_ID},
        "adGroup": dict(AD_GROUP_REF),
        "segments": {"date": "2026-09-04"},
        "metrics": {
            "historicalQualityScore": "7",
            "averageCpc": 2500000,
            "costMicros": "30000000",
            "clicks": "12",
            "impressions": "400",
            "conversions": 1.5,
        },
    },
    "row_search_term_view": {
        "searchTermView": {
            "resourceName": f"{CUSTOMER_RN}/searchTermViews/{CAMPAIGN_ID}~{AD_GROUP_ID}~YWNtZQ",
            "searchTerm": "acme analytics pricing",
            "status": "NONE",
        },
        "segments": {
            "date": "2026-09-04",
            "keyword": {
                "adGroupCriterion": CRITERION_RN,
                "info": {"text": "acme analytics", "matchType": "PHRASE"},
            },
        },
        "adGroup": dict(AD_GROUP_REF),
        "campaign": dict(CAMPAIGN_REF),
        "metrics": {
            "costMicros": "4200000",
            "clicks": "3",
            "impressions": "50",
            "conversions": 2.0,
            "conversionsValue": 180.25,
        },
    },
    "row_ad_group_criterion": {
        "adGroupCriterion": {
            "resourceName": CRITERION_RN,
            "criterionId": CRITERION_ID,
            "status": "ENABLED",
            "keyword": {"text": "acme analytics", "matchType": "EXACT"},
            "qualityInfo": {"qualityScore": 8},
        },
        "adGroup": dict(AD_GROUP_REF),
        "campaign": dict(CAMPAIGN_REF),
    },
    "row_conversion_action": {
        "conversionAction": {
            "resourceName": f"{CUSTOMER_RN}/conversionActions/{ACTION_ID}",
            "id": ACTION_ID,
            "name": "Demo request",
            "status": "ENABLED",
            "includeInConversionsMetric": True,
            "countingType": "ONE_PER_CLICK",
            "attributionModelSettings": {"attributionModel": "GOOGLE_ADS_LAST_CLICK"},
        }
    },
}

RowCheck = Callable[[str, dict[str, Any]], dict[str, Any]]
"""``on_contract(surface, row)``: validate a JSON-native row, return it decoded as on the wire."""


def _base(surface: str) -> dict[str, Any]:
    """Return a fresh deep copy of the conforming row of ``surface``."""
    return copy.deepcopy(BASE_ROWS[surface])


def _wire(value: object) -> Any:
    """Re-decode a JSON value exactly as ``connectors/http.py`` decodes a webhook body.

    JSON numbers with a fraction or exponent become :class:`~decimal.Decimal`, integers stay
    ``int`` and int64 strings stay strings, which is the form ``client.py`` hands to the parser.
    """
    return json.loads(json.dumps(value), parse_float=Decimal)


def _set(row: dict[str, Any], gaql_field: str, value: object) -> dict[str, Any]:
    """Set the value at a GAQL field's REST key path, creating objects on the way."""
    *parents, leaf = rest_path(gaql_field)
    node = row
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value
    return row


def _drop(row: dict[str, Any], gaql_field: str) -> dict[str, Any]:
    """Remove the key at a GAQL field's REST key path (REST JSON's omission)."""
    *parents, leaf = rest_path(gaql_field)
    node = row
    for key in parents:
        node = node[key]
    del node[leaf]
    return row


def _results(envelope: Mapping[str, Any]) -> list[GoogleAdsRow]:
    """Return an envelope's rows; an absent ``results`` is zero rows (search.json)."""
    rows = envelope.get("results", [])
    assert isinstance(rows, list)
    return rows


def _parse_exchange(query: str, body: object) -> tuple[str, list[object]]:
    """Validate and parse one webhook response the way ``client.py`` reads it.

    The query id is read back from the request's GAQL text, the n8n one-element array wrapper
    is removed, every row is checked against its ``row_*`` contract and the ``fieldMask``
    against the template's fields, and each row is parsed in its wire decoding.

    Args:
        query: The ``query`` of the webhook request body.
        body: The JSON-native response body, possibly wrapped in a one-element array.

    Returns:
        The row surface and the parsed rows.
    """
    query_id = query_id_for(query)
    assert query_id is not None, "the request query is not a rendering of any QUERIES template"
    envelope: Any = errors.unwrap_single_item(body)
    payloads.validate_google_rows(query, envelope)
    assert isinstance(envelope, dict)
    surface = QUERIES[query_id].row_surface
    return surface, [PARSERS[surface](row) for row in _results(_wire(envelope))]


@pytest.fixture
def on_contract(contract: Callable[..., Contract]) -> RowCheck:
    """Return ``on_contract(surface, row)``: the gate every hand-built row passes first.

    The row is validated as the JSON-native value it is with ``payloads.validate_response``
    against ``contracts/google/{surface}.json`` (status 200), then returned re-decoded by
    :func:`_wire`, ready for its parser. A field, type or enum value the contract does not
    define fails the test before the parser is reached.
    """

    def _check(surface: str, row: dict[str, Any]) -> dict[str, Any]:
        payloads.validate_response(contract("google", surface), row, status=200)
        decoded: dict[str, Any] = _wire(row)
        return decoded

    return _check


def _assert_validation_defect(defect: QueryDefect, surface: str, gaql_field: str) -> None:
    """Assert ``defect`` is the parser's validation defect for ``gaql_field`` of ``surface``."""
    assert defect.code == "validation"
    assert defect.source == "google"
    assert defect.endpoint == f"parse {surface}"
    assert defect.message.startswith(f"{gaql_field}: expected ")


# ---------------------------------------------------------------------------------------------
# Phase 1: contracts, snapshots and captures (traceability)
# ---------------------------------------------------------------------------------------------


def test_parsers_cover_exactly_the_row_surfaces_of_queries() -> None:
    assert set(PARSERS) == {template.row_surface for template in QUERIES.values()}
    assert set(PARSERS) == set(ROW_SURFACES) == set(ROW_TYPES)
    assert len(PARSERS) == 8


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_row_contract_describes_the_fields_its_template_selects(
    surface: str, contract: Callable[..., Contract]
) -> None:
    gaql = contract("google", surface).request.gaql
    assert gaql is not None
    template = QUERIES[gaql.query_id]
    assert template.row_surface == surface
    assert template.resource == gaql.resource
    assert template.select == gaql.select


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_conforming_row_parses_into_its_row_type(surface: str, on_contract: RowCheck) -> None:
    parsed = PARSERS[surface](on_contract(surface, _base(surface)))
    assert type(parsed) is ROW_TYPES[surface]


def test_contract_gate_rejects_a_field_the_contract_does_not_define(
    on_contract: RowCheck,
) -> None:
    row = _set(_base("row_campaign"), "campaign.start_date", "2026-09-01")
    with pytest.raises(ContractViolation, match="unknown field"):
        on_contract("row_campaign", row)


@pytest.mark.parametrize("surface", ROW_SURFACES)
def test_documented_sample_parses_verbatim(surface: str, contract: Callable[..., Contract]) -> None:
    loaded = contract("google", surface)
    payloads.validate_documented_sample(loaded)
    sample = loaded.response.documented_sample
    if sample is None:
        # The v25 reference publishes no sample row for this resource; the contract records
        # that as null, and the conforming hand-built row stands in for it above.
        return
    if isinstance(sample, dict) and "results" in sample:
        rows: list[Any] = _results(sample)
    else:
        rows = sample if isinstance(sample, list) else [sample]
    assert rows, "a documented sample must hold at least one row"
    for row in rows:
        assert type(PARSERS[surface](row)) is ROW_TYPES[surface]


def test_manifest_lists_one_snapshot_per_google_template() -> None:
    on_disk = {path.name for path in FIXTURES_DIR.glob("google__q_g*.json")}
    assert set(GOOGLE_FIXTURES) == on_disk
    query_ids = set()
    for file_name in GOOGLE_FIXTURES:
        snapshot = json.loads((FIXTURES_DIR / file_name).read_text(encoding="utf-8"))
        query_ids.add(query_id_for(snapshot["request"]["body"]["query"]))
    assert query_ids == set(QUERIES)


@pytest.mark.parametrize("file_name", GOOGLE_FIXTURES)
def test_snapshot_rows_parse_with_the_parser_of_their_query(file_name: str) -> None:
    snapshot = json.loads((FIXTURES_DIR / file_name).read_text(encoding="utf-8"))
    assert snapshot["source"] == "google"
    assert snapshot["surface"] == "google.search"
    response = snapshot["response"]
    assert response["status"] == 200
    query = snapshot["request"]["body"]["query"]
    surface, parsed = _parse_exchange(query, response["body"])
    assert parsed, f"{file_name}: a snapshot must carry at least one row"
    assert all(type(row) is ROW_TYPES[surface] for row in parsed)
    raw_rows = _results(response["body"])
    assert len(parsed) == len(raw_rows)
    for raw, row in zip(raw_rows, parsed, strict=True):
        if not isinstance(row, CampaignDailyRow | KeywordViewRow | SearchTermRow):
            continue
        # Money is the exact micros divided by 1,000,000, never a rounded float, and
        # conversions are the returned decimal, never rounded to a whole count.
        assert row.cost_micros == int(str(get_field(raw, "metrics.cost_micros") or 0))
        assert row.cost * 1_000_000 == row.cost_micros
        wire = get_field(raw, "metrics.conversions")
        assert row.conversions == (Decimal(0) if wire is None else Decimal(str(wire)))


def test_snapshot_wrapped_in_a_one_element_array_parses_the_same() -> None:
    snapshot = json.loads((FIXTURES_DIR / "google__q_g6.json").read_text(encoding="utf-8"))
    query = snapshot["request"]["body"]["query"]
    body = snapshot["response"]["body"]
    assert _parse_exchange(query, [body]) == _parse_exchange(query, body)


def test_captured_google_rows_parse_with_the_parser_of_their_query(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
) -> None:
    # Zero captures is a valid state until `ads-engine capture-fixtures` has run with live
    # credentials; every capture recorded since then must parse like the snapshots do.
    for capture in captured_payloads("google"):
        response = capture["response"]
        if capture["surface"] != "google.search" or not 200 <= response["status"] < 300:
            continue
        request_body = capture["request"]["body"]
        if isinstance(request_body, str):
            request_body = json.loads(request_body)
        surface, parsed = _parse_exchange(request_body["query"], response["body"])
        assert all(type(row) is ROW_TYPES[surface] for row in parsed)


# ---------------------------------------------------------------------------------------------
# Phase 2: value conversions
# ---------------------------------------------------------------------------------------------


def test_daily_budget_keeps_an_exact_amount_beside_its_native_micros(
    on_contract: RowCheck,
) -> None:
    campaign = parse_campaign(on_contract("row_campaign", _base("row_campaign")))
    assert campaign.campaign_id == int(CAMPAIGN_ID)
    assert campaign.name == "Brand - Search"
    assert campaign.advertising_channel_type == "SEARCH"
    assert campaign.budget_id == int(BUDGET_ID)
    assert type(campaign.budget_id) is int
    assert campaign.budget_period == "DAILY"
    assert campaign.budget_amount_micros == 12345678
    assert type(campaign.budget_amount_micros) is int
    assert campaign.budget_amount == Decimal("12.345678")
    assert campaign.daily_budget == Decimal("12.345678")
    assert campaign.total_budget is None
    assert campaign.budget_explicitly_shared is True


def test_omitted_explicitly_shared_is_a_budget_of_the_campaign_alone(
    on_contract: RowCheck,
) -> None:
    # proto3 JSON omits an implicit-presence bool that is false.
    row = _drop(_base("row_campaign"), "campaign_budget.explicitly_shared")
    campaign = parse_campaign(on_contract("row_campaign", row))
    assert campaign.budget_explicitly_shared is False


def test_custom_period_budget_reads_its_lifetime_total_amount(on_contract: RowCheck) -> None:
    row = _base("row_campaign")
    _drop(row, "campaign_budget.amount_micros")
    _drop(row, "campaign_budget.explicitly_shared")
    _set(row, "campaign_budget.period", "CUSTOM_PERIOD")
    _set(row, "campaign_budget.total_amount_micros", "500000000")
    campaign = parse_campaign(on_contract("row_campaign", row))
    assert campaign.budget_period == "CUSTOM_PERIOD"
    assert campaign.budget_amount_micros == 500000000
    assert campaign.total_budget == Decimal("500")
    assert campaign.daily_budget is None
    assert campaign.budget_explicitly_shared is False


def test_daily_budget_without_its_amount_is_unknown_not_zero(on_contract: RowCheck) -> None:
    row = _drop(_base("row_campaign"), "campaign_budget.amount_micros")
    campaign = parse_campaign(on_contract("row_campaign", row))
    assert campaign.budget_id == int(BUDGET_ID)
    assert campaign.budget_amount_micros is None
    assert campaign.budget_amount is None
    assert campaign.daily_budget is None


def test_campaign_without_a_budget_has_no_budget_fields(on_contract: RowCheck) -> None:
    row = _base("row_campaign")
    del row["campaignBudget"]
    campaign = parse_campaign(on_contract("row_campaign", row))
    assert campaign.budget_id is None
    assert campaign.budget_period is None
    assert campaign.budget_amount_micros is None
    assert campaign.budget_amount is None
    assert campaign.daily_budget is None
    assert campaign.total_budget is None
    assert campaign.budget_explicitly_shared is False


def test_campaign_status_map_is_enabled_paused_removed() -> None:
    assert dict(CAMPAIGN_STATUS_MAP) == {
        "ENABLED": "active",
        "PAUSED": "paused",
        "REMOVED": "ended",
    }


STATUS_FIELDS = (
    ("row_campaign", "campaign.status"),
    ("row_ad_group", "ad_group.status"),
    ("row_ad_group_criterion", "ad_group_criterion.status"),
)


@pytest.mark.parametrize(("surface", "status_field"), STATUS_FIELDS)
@pytest.mark.parametrize(
    ("raw", "normalised"), [("ENABLED", "active"), ("PAUSED", "paused"), ("REMOVED", "ended")]
)
def test_status_is_normalised_and_kept_raw(
    surface: str, status_field: str, raw: str, normalised: str, on_contract: RowCheck
) -> None:
    row = on_contract(surface, _set(_base(surface), status_field, raw))
    parsed = PARSERS[surface](row)
    assert isinstance(parsed, CampaignRow | AdGroupRow | KeywordRow)
    assert parsed.status == normalised
    assert parsed.raw_status == raw


@pytest.mark.parametrize(("surface", "status_field"), STATUS_FIELDS)
@pytest.mark.parametrize("raw", ["UNKNOWN", "UNSPECIFIED"])
def test_status_the_contract_allows_but_the_map_lacks_is_a_defect(
    surface: str, status_field: str, raw: str, on_contract: RowCheck
) -> None:
    # Both values are in the contract's enum, so the row is on contract; the parser refuses to
    # guess a normalised status for them.
    row = on_contract(surface, _set(_base(surface), status_field, raw))
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](row)
    _assert_validation_defect(caught.value, surface, status_field)


def test_campaign_daily_converts_money_counts_shares_and_date(on_contract: RowCheck) -> None:
    daily = parse_campaign_daily(on_contract("row_campaign_daily", _base("row_campaign_daily")))
    assert daily.campaign_id == int(CAMPAIGN_ID)
    assert daily.cost == Decimal("1.5")
    assert daily.cost_micros == 1500000
    assert daily.clicks == 12
    assert type(daily.clicks) is int
    assert daily.impressions == 340
    assert daily.conversions == Decimal("0.3")
    assert daily.conversions_value == Decimal("45.5")
    assert daily.search_budget_lost_impression_share == Decimal("0.12")
    assert daily.search_impression_share == Decimal("0.6")
    assert daily.date == date(2026, 9, 4)


CONVERSION_SURFACES = ("row_campaign_daily", "row_keyword_view", "row_search_term_view")


@pytest.mark.parametrize("surface", CONVERSION_SURFACES)
@pytest.mark.parametrize(
    "wire", [Decimal("0.3"), 0.3, "0.3"], ids=["decimal", "float", "numeric_string"]
)
def test_fractional_conversions_are_kept_exact_whatever_their_encoding(
    surface: str, wire: object, on_contract: RowCheck
) -> None:
    # The row is validated with conversions as a JSON number; the decimal is what http.py
    # decodes that number to, the float what a plain json.loads gives, and the numeric string
    # an encoding the contract does not define but the parser tolerates.
    row = on_contract(surface, _set(_base(surface), "metrics.conversions", 0.3))
    _set(row, "metrics.conversions", wire)
    parsed = PARSERS[surface](row)
    assert isinstance(parsed, CampaignDailyRow | KeywordViewRow | SearchTermRow)
    assert parsed.conversions == Decimal("0.3")
    assert str(parsed.conversions) == "0.3"
    # Never rounded and never assumed integral: a fraction still counts as converting.
    assert parsed.conversions != parsed.conversions.to_integral_value()


def test_conversions_decoded_from_wire_text_never_pass_through_a_binary_float(
    on_contract: RowCheck,
) -> None:
    row = on_contract(
        "row_campaign_daily", _set(_base("row_campaign_daily"), "metrics.conversions", 0.1)
    )
    assert row["metrics"]["conversions"] == Decimal("0.1")
    daily = parse_campaign_daily(row)
    assert daily.conversions == Decimal("0.1")
    assert daily.conversions != Decimal.from_float(0.1)


def test_omitted_additive_metrics_are_zero_and_omitted_shares_unavailable(
    on_contract: RowCheck,
) -> None:
    # REST JSON omits zero-valued fields, so an absent count is zero; an absent share is
    # unknown, never zero.
    row = _base("row_campaign_daily")
    for gaql_field in (
        "metrics.conversions",
        "metrics.clicks",
        "metrics.search_budget_lost_impression_share",
        "metrics.search_impression_share",
    ):
        _drop(row, gaql_field)
    daily = parse_campaign_daily(on_contract("row_campaign_daily", row))
    assert daily.conversions == Decimal(0)
    assert daily.clicks == 0
    assert daily.search_budget_lost_impression_share is None
    assert daily.search_impression_share is None
    assert daily.cost == Decimal("1.5")
    assert daily.impressions == 340


def test_impression_share_finer_than_six_places_is_rounded_toward_zero(
    on_contract: RowCheck,
) -> None:
    # Google returns shares as full doubles; the stored six places never cross a threshold
    # such as the 10% capped-day test.
    row = _set(_base("row_campaign_daily"), "metrics.search_impression_share", 0.09999999)
    daily = parse_campaign_daily(on_contract("row_campaign_daily", row))
    assert daily.search_impression_share == Decimal("0.099999")


def test_campaign_day_without_metrics_has_zero_cost_and_counts(on_contract: RowCheck) -> None:
    row = _base("row_campaign_daily")
    del row["metrics"]
    daily = parse_campaign_daily(on_contract("row_campaign_daily", row))
    assert daily.cost == Decimal(0)
    assert daily.cost_micros == 0
    assert (daily.impressions, daily.clicks) == (0, 0)
    assert daily.conversions == Decimal(0)
    assert daily.conversions_value == Decimal(0)
    assert daily.search_budget_lost_impression_share is None
    assert daily.search_impression_share is None


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("2026-09-04", date(2026, 9, 4)),
        ("2026-12-31", date(2026, 12, 31)),
        ("2028-02-29", date(2028, 2, 29)),
    ],
)
def test_segments_date_is_the_native_day_as_written(
    wire: str, expected: date, on_contract: RowCheck
) -> None:
    row = _set(_base("row_campaign_daily"), "segments.date", wire)
    assert parse_campaign_daily(on_contract("row_campaign_daily", row)).date == expected


def test_ad_group_reads_its_campaign_name_and_status(on_contract: RowCheck) -> None:
    ad_group = parse_ad_group(on_contract("row_ad_group", _base("row_ad_group")))
    assert ad_group.ad_group_id == int(AD_GROUP_ID)
    assert ad_group.campaign_id == int(CAMPAIGN_ID)
    assert ad_group.name == "Brand terms"
    assert (ad_group.status, ad_group.raw_status) == ("active", "ENABLED")


def test_keyword_view_converts_score_cpc_and_metrics(on_contract: RowCheck) -> None:
    view = parse_keyword_view(on_contract("row_keyword_view", _base("row_keyword_view")))
    assert view.ad_group_id == int(AD_GROUP_ID)
    assert view.criterion_id == int(CRITERION_ID)
    assert view.date == date(2026, 9, 4)
    assert view.historical_quality_score == 7
    assert type(view.historical_quality_score) is int
    assert view.average_cpc == Decimal("2.5")
    assert view.cost == Decimal("30")
    assert view.cost_micros == 30000000
    assert (view.clicks, view.impressions) == (12, 400)
    assert view.conversions == Decimal("1.5")


def test_keyword_day_without_a_historical_quality_score_has_none(on_contract: RowCheck) -> None:
    row = _drop(_base("row_keyword_view"), "metrics.historical_quality_score")
    view = parse_keyword_view(on_contract("row_keyword_view", row))
    assert view.historical_quality_score is None
    assert view.average_cpc == Decimal("2.5")


@pytest.mark.parametrize(
    "wire", [2500000, 2500000.0, "2500000"], ids=["json_integer", "json_number", "numeric_string"]
)
def test_average_cpc_micros_become_an_amount(wire: object, on_contract: RowCheck) -> None:
    # averageCpc is a DOUBLE in micros. The contract types it as a JSON number (an integer or a
    # number with a fraction, decoded as Decimal); a numeric string is tolerated off contract.
    row = on_contract("row_keyword_view", _base("row_keyword_view"))
    _set(row, "metrics.average_cpc", _wire(wire))
    assert parse_keyword_view(row).average_cpc == Decimal("2.5")


def test_keyword_day_without_clicks_has_no_average_cpc(on_contract: RowCheck) -> None:
    row = _base("row_keyword_view")
    for gaql_field in ("metrics.average_cpc", "metrics.clicks", "metrics.cost_micros"):
        _drop(row, gaql_field)
    view = parse_keyword_view(on_contract("row_keyword_view", row))
    assert view.average_cpc is None
    assert view.clicks == 0
    assert view.cost == Decimal(0)


def test_search_term_takes_the_matched_criterion_from_its_resource_name(
    on_contract: RowCheck,
) -> None:
    term = parse_search_term(on_contract("row_search_term_view", _base("row_search_term_view")))
    assert CRITERION_RN == "customers/1234567890/adGroupCriteria/333~444"
    assert term.matched_criterion_id == "444"
    assert term.matched_keyword_text == "acme analytics"
    assert term.matched_match_type == "PHRASE"
    assert term.search_term == "acme analytics pricing"
    assert term.campaign_id == int(CAMPAIGN_ID)
    assert term.ad_group_id == int(AD_GROUP_ID)
    assert term.date == date(2026, 9, 4)
    assert term.cost == Decimal("4.2")
    assert term.cost_micros == 4200000
    assert (term.clicks, term.impressions) == (3, 50)
    assert term.conversions == Decimal("2")
    assert term.conversions_value == Decimal("180.25")


def test_search_term_without_a_keyword_segment_matched_no_criterion(
    on_contract: RowCheck,
) -> None:
    row = _drop(_base("row_search_term_view"), "segments.keyword")
    term = parse_search_term(on_contract("row_search_term_view", row))
    assert term.matched_criterion_id == ""
    assert term.matched_keyword_text is None
    assert term.matched_match_type is None


def test_matched_criterion_of_another_ad_group_is_a_defect(on_contract: RowCheck) -> None:
    # A criterion id is unique only within its ad group, so the resource name must name the
    # row's own ad group.
    other = f"{CUSTOMER_RN}/adGroupCriteria/999~{CRITERION_ID}"
    row = _set(_base("row_search_term_view"), "segments.keyword.ad_group_criterion", other)
    with pytest.raises(QueryDefect) as caught:
        parse_search_term(on_contract("row_search_term_view", row))
    _assert_validation_defect(
        caught.value, "row_search_term_view", "segments.keyword.ad_group_criterion"
    )


@pytest.mark.parametrize("status", ["ADDED", "EXCLUDED", "ADDED_EXCLUDED", "NONE"])
def test_search_term_status_passes_through_raw(status: str, on_contract: RowCheck) -> None:
    row = _set(_base("row_search_term_view"), "search_term_view.status", status)
    assert parse_search_term(on_contract("row_search_term_view", row)).term_status == status


def test_keyword_reads_text_match_type_and_quality_score(on_contract: RowCheck) -> None:
    keyword = parse_keyword(on_contract("row_ad_group_criterion", _base("row_ad_group_criterion")))
    assert keyword.ad_group_id == int(AD_GROUP_ID)
    assert keyword.criterion_id == int(CRITERION_ID)
    assert keyword.campaign_id == int(CAMPAIGN_ID)
    assert keyword.text == "acme analytics"
    assert keyword.match_type == "EXACT"
    assert keyword.quality_score == 8
    assert type(keyword.quality_score) is int


def test_keyword_without_quality_info_has_an_unavailable_score(on_contract: RowCheck) -> None:
    # Keywords with little or no traffic carry no quality info; that is unknown, never zero.
    row = _drop(_base("row_ad_group_criterion"), "ad_group_criterion.quality_info")
    keyword = parse_keyword(on_contract("row_ad_group_criterion", row))
    assert keyword.quality_score is None


@pytest.mark.parametrize(
    ("counting_type", "attribution_model", "included"),
    [
        ("ONE_PER_CLICK", "GOOGLE_ADS_LAST_CLICK", True),
        ("MANY_PER_CLICK", "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN", False),
    ],
)
def test_conversion_action_keeps_its_counting_contract_raw(
    counting_type: str, attribution_model: str, included: bool, on_contract: RowCheck
) -> None:
    row = _base("row_conversion_action")
    _set(row, "conversion_action.counting_type", counting_type)
    _set(row, "conversion_action.attribution_model_settings.attribution_model", attribution_model)
    _set(row, "conversion_action.include_in_conversions_metric", included)
    action = parse_conversion_action(on_contract("row_conversion_action", row))
    assert action.conversion_action_id == int(ACTION_ID)
    assert action.name == "Demo request"
    assert action.status == "ENABLED"
    assert action.counting_type == counting_type
    assert action.attribution_model == attribution_model
    assert action.include_in_conversions_metric is included


def test_conversion_action_without_settings_has_an_unknown_model(on_contract: RowCheck) -> None:
    # B4 reads a missing attribution model as an unknown counting contract, never last click.
    row = _base("row_conversion_action")
    _drop(row, "conversion_action.attribution_model_settings")
    _drop(row, "conversion_action.include_in_conversions_metric")
    action = parse_conversion_action(on_contract("row_conversion_action", row))
    assert action.attribution_model is None
    assert action.include_in_conversions_metric is False


def test_customer_gives_its_id_currency_and_time_zone(on_contract: RowCheck) -> None:
    customer = parse_customer(on_contract("row_customer", _base("row_customer")))
    assert customer.customer_id == int(CUSTOMER_ID)
    assert type(customer.customer_id) is int
    assert customer.currency_code == "EUR"
    assert customer.time_zone == "Europe/Berlin"
    assert customer.descriptive_name == "Acme Analytics"


def test_int64_id_at_its_maximum_stays_exact(on_contract: RowCheck) -> None:
    row = _set(_base("row_campaign"), "campaign.id", "9223372036854775807")
    campaign = parse_campaign(on_contract("row_campaign", row))
    assert campaign.campaign_id == 9223372036854775807 == INT64_MAX


@pytest.mark.parametrize(
    "wire",
    [111, Decimal("111"), Decimal("111.0")],
    ids=["json_integer", "decimal", "decimal_with_zero_fraction"],
)
def test_int64_id_given_as_a_json_integer_or_integral_decimal_is_accepted(
    wire: object, on_contract: RowCheck
) -> None:
    # proto3 JSON writes INT64 as a string, as the contract says; parsers also accept the
    # number form, so an integer or an integral decimal is tolerated off contract.
    row = on_contract("row_campaign", _base("row_campaign"))
    _set(row, "campaign.id", wire)
    assert parse_campaign(row).campaign_id == 111


# ---------------------------------------------------------------------------------------------
# Phase 3: helpers and the n8n envelope
# ---------------------------------------------------------------------------------------------


def test_rest_path_walks_each_snake_case_segment_as_lower_camel_case() -> None:
    assert rest_path("campaign_budget.amount_micros") == ("campaignBudget", "amountMicros")
    assert rest_path("segments.keyword.info.match_type") == (
        "segments",
        "keyword",
        "info",
        "matchType",
    )


def test_get_field_reads_the_nested_raw_value(on_contract: RowCheck) -> None:
    row = on_contract("row_campaign_daily", _base("row_campaign_daily"))
    # int64 values are still the wire strings; the typed parsers convert them.
    assert get_field(row, "metrics.cost_micros") == "1500000"
    assert get_field(row, "segments.date") == "2026-09-04"
    assert get_field(row, "metrics.conversions") == Decimal("0.3")
    assert get_field(row, "campaign_budget.id") is None


def test_get_field_rejects_an_explicit_json_null() -> None:
    with pytest.raises(QueryDefect) as caught:
        get_field({"metrics": None}, "metrics.cost_micros")
    assert caught.value.code == "validation"


def test_unwrap_single_item_returns_the_envelope_inside_n8ns_array(
    contract: Callable[..., Contract],
) -> None:
    snapshot = json.loads((FIXTURES_DIR / "google__q_g1.json").read_text(encoding="utf-8"))
    envelope = snapshot["response"]["body"]
    # n8n's "all incoming items" response mode wraps the Google response in a one-element array.
    unwrapped: Any = errors.unwrap_single_item([envelope])
    assert unwrapped is envelope
    assert errors.unwrap_single_item(envelope) is envelope
    payloads.validate_response(contract("google", "search"), unwrapped, status=200)
    (customer,) = (parse_customer(row) for row in _results(unwrapped))
    assert customer.customer_id == 9900000001
    assert customer.currency_code == "USD"
    assert customer.time_zone == "America/New_York"


# ---------------------------------------------------------------------------------------------
# Phase 4: malformed rows (deliberately off contract)
# ---------------------------------------------------------------------------------------------

CAMPAIGN_ID_SURFACES = (
    "row_campaign",
    "row_campaign_daily",
    "row_ad_group",
    "row_search_term_view",
    "row_ad_group_criterion",
)


@pytest.mark.parametrize("surface", CAMPAIGN_ID_SURFACES)
def test_row_without_campaign_id_is_a_validation_defect(surface: str) -> None:
    row = _drop(_base(surface), "campaign.id")
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](_wire(row))
    _assert_validation_defect(caught.value, surface, "campaign.id")


@pytest.mark.parametrize(
    ("surface", "gaql_field", "wire"),
    [
        ("row_campaign", "campaign.id", "12a"),
        ("row_campaign", "campaign_budget.amount_micros", "12.5"),
        ("row_campaign_daily", "metrics.clicks", "twelve"),
        ("row_campaign_daily", "metrics.cost_micros", "1e6"),
        ("row_keyword_view", "ad_group_criterion.criterion_id", ""),
        ("row_customer", "customer.id", "92233720368547758070"),
    ],
)
def test_non_numeric_int64_string_is_a_validation_defect(
    surface: str, gaql_field: str, wire: str
) -> None:
    row = _set(_base(surface), gaql_field, wire)
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](_wire(row))
    _assert_validation_defect(caught.value, surface, gaql_field)
    # Messages name the field and the expected type only, never the row value.
    if wire:
        assert wire not in str(caught.value)


def test_int64_id_beyond_the_range_is_a_validation_defect() -> None:
    row = _set(_base("row_campaign"), "campaign.id", str(INT64_MAX + 1))
    with pytest.raises(QueryDefect) as caught:
        parse_campaign(_wire(row))
    _assert_validation_defect(caught.value, "row_campaign", "campaign.id")


@pytest.mark.parametrize("surface", ROW_SURFACES)
@pytest.mark.parametrize("row", [[], "row", 7, None], ids=["array", "string", "number", "null"])
def test_row_that_is_not_an_object_is_a_validation_defect(surface: str, row: object) -> None:
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](row)  # type: ignore[arg-type]
    defect = caught.value
    assert defect.code == "validation"
    assert defect.source == "google"
    assert defect.endpoint == f"parse {surface}"
    assert "a JSON object at every level of its key path" in defect.message


@pytest.mark.parametrize(
    ("surface", "gaql_field", "wire"),
    [
        ("row_campaign", "campaign.name", 7),
        ("row_campaign", "campaign.name", "Brand\x00Search"),
        ("row_campaign", "campaign.name", "Marke\ud800"),
        ("row_campaign", "campaign_budget.explicitly_shared", "true"),
        ("row_campaign", "campaign_budget.period", "WEEKLY"),
        ("row_campaign", "campaign_budget.total_amount_micros", "500000000"),
        ("row_campaign_daily", "segments.date", "2026/09/04"),
        ("row_campaign_daily", "segments.date", "2026-02-30"),
        ("row_campaign_daily", "metrics.conversions", "three"),
        ("row_campaign_daily", "metrics.conversions", "NaN"),
        ("row_campaign_daily", "metrics.conversions", [1]),
        ("row_campaign_daily", "metrics.conversions", Decimal("-1")),
        ("row_campaign_daily", "metrics.conversions", Decimal("1E+12")),
        ("row_campaign_daily", "metrics.conversions", Decimal("2.3333333")),
        ("row_campaign_daily", "metrics.search_impression_share", Decimal("1.5")),
        ("row_campaign_daily", "metrics.search_impression_share", Decimal("0.0000001")),
        ("row_keyword_view", "metrics.average_cpc", Decimal("1E+18")),
        ("row_keyword_view", "metrics.average_cpc", Decimal("999999999999999999.6")),
        ("row_keyword_view", "metrics.average_cpc", Decimal("0.4")),
        ("row_ad_group_criterion", "ad_group_criterion.keyword.match_type", "FUZZY"),
        ("row_customer", "customer.currency_code", "eur"),
        ("row_customer", "customer.time_zone", "Mars/Olympus_Mons"),
        ("row_search_term_view", "search_term_view.status", "PENDING"),
        (
            "row_search_term_view",
            "segments.keyword.ad_group_criterion",
            f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP_ID}",
        ),
        (
            "row_search_term_view",
            "segments.keyword.ad_group_criterion",
            f"{CUSTOMER_RN}/adGroupCriteria/{AD_GROUP_ID}~9999999999999999999",
        ),
    ],
)
def test_malformed_field_value_is_a_validation_defect_naming_only_the_field(
    surface: str, gaql_field: str, wire: object
) -> None:
    # The value is set on the wire-decoded row, so decimals and lone surrogates reach the
    # parser exactly as given. Each one is a value the parser cannot store faithfully: the
    # wrong type, text PostgreSQL cannot hold, an amount outside numeric(18, 6), an enum name
    # outside the contract, or an amount field of the other budget period.
    row = _set(_wire(_base(surface)), gaql_field, wire)
    with pytest.raises(QueryDefect) as caught:
        PARSERS[surface](row)
    _assert_validation_defect(caught.value, surface, gaql_field)
    assert str(wire) not in caught.value.message.removeprefix(f"{gaql_field}: expected ")


def test_explicitly_shared_lifetime_budget_is_a_validation_defect(on_contract: RowCheck) -> None:
    # The row contract types each key on its own; Google never shares a CUSTOM_PERIOD budget.
    row = _base("row_campaign")
    _drop(row, "campaign_budget.amount_micros")
    _set(row, "campaign_budget.period", "CUSTOM_PERIOD")
    _set(row, "campaign_budget.total_amount_micros", "500000000")
    with pytest.raises(QueryDefect) as caught:
        parse_campaign(on_contract("row_campaign", row))
    _assert_validation_defect(caught.value, "row_campaign", "campaign_budget.explicitly_shared")


def test_get_field_rejects_a_name_that_is_not_a_gaql_field() -> None:
    with pytest.raises(QueryDefect) as caught:
        get_field({"campaign": {"id": CAMPAIGN_ID}}, "Campaign.Id")
    assert caught.value.code == "validation"
    assert caught.value.endpoint == "parse google_ads_row"
