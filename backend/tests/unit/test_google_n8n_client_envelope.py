"""Unit tests for the response projection check of ``GoogleAdsWebhookClient``.

A successful ``googleAds:search`` response carries ``fieldMask``, the comma-joined camelCase
paths of the selected fields (``backend/tests/contracts/google/search.json``). Google omits a
zero or unset field from a row and the parser reads an omitted metric as zero, so a response
whose mask is not the requested projection must fail as a ``QueryDefect`` (code
``validation``, carrying the query id, no retry) instead of becoming zero-valued facts.

Covered: a correct mask in any order is accepted for every template; truncated Q-G3, Q-G5 and
Q-G6 masks, an extra path, a wrong path, an empty entry and an empty-string mask are rejected
after exactly one POST; the settled envelope rules still hold (a ``null`` or absent mask with
results is accepted, a body with neither is a defect, n8n's one-element array is unwrapped, a
zero-row body with the correct mask is an empty page); ``search_page`` derives the projection
from its GAQL text and rejects text without a readable ``SELECT ... FROM`` before sending; no
error or log carries GAQL text or relay-supplied mask entries; and the client's projection of
every template equals the ``request.gaql.select`` of its ``row_*.json`` contract.

Every call runs through the production client and ``send_json`` over an
``httpx.MockTransport`` built by ``connectors.http.build_client``; nothing touches the network.
"""

import json
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.google_n8n.client import (
    SIMULATED_SECRET,
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    GoogleAdsWebhookClient,
    _prepare,
)
from ads_engine.connectors.google_n8n.queries import (
    CAMPAIGN_HI,
    CAMPAIGN_LO,
    END,
    IDS,
    INT64_MAX,
    INT64_MIN_ID,
    LAST_ID,
    QUERIES,
    START,
    rest_path,
)
from ads_engine.connectors.http import RetryPolicy, build_client

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts" / "google"

CUSTOMER = "1234567890"
SECRET_HEADER = "X-Webhook-Secret"
DAY = date(2026, 9, 1)

# A valid scope for every template, so each one can be read through run_query.
SCOPES: Mapping[str, Mapping[str, object]] = {
    "Q-G1": {},
    "Q-G2": {LAST_ID: 0},
    "Q-G2b": {IDS: (111, 222)},
    "Q-G3": {START: DAY, END: DAY},
    "Q-G4": {LAST_ID: 0},
    "Q-G5": {START: DAY, END: DAY},
    "Q-G6": {START: DAY, END: DAY},
    "Q-G7": {},
    "Q-G8": {},
}

# The selected fields of the dated templates that are not metrics: what a relay that dropped
# every metric from the projection would still report.
NON_METRIC_FIELDS: Mapping[str, tuple[str, ...]] = {
    query_id: tuple(name for name in QUERIES[query_id].select if not name.startswith("metrics."))
    for query_id in ("Q-G3", "Q-G5", "Q-G6")
}

# Q-G3 rows as Google returns them for campaigns without traffic: every zero metric omitted.
METRICLESS_QG3_ROWS: tuple[dict[str, Any], ...] = tuple(
    {
        "campaign": {"resourceName": f"customers/{CUSTOMER}/campaigns/{cid}", "id": str(cid)},
        "segments": {"date": DAY.isoformat()},
    }
    for cid in (111, 222)
)
# A Q-G3 row with metrics, some of them omitted because they are zero.
QG3_ROW: dict[str, Any] = {
    "campaign": {"resourceName": f"customers/{CUSTOMER}/campaigns/333", "id": "333"},
    "segments": {"date": DAY.isoformat()},
    "metrics": {"costMicros": "1250000", "impressions": "40", "clicks": "3"},
}

# Relay-supplied mask text that must never surface in an error or a log line.
RELAY_MARKER = "relayInjected.leakMarker"


async def _no_sleep(seconds: float) -> None:
    """Skip the backoff between retries; an attempt would still be counted."""


RETRY = RetryPolicy(sleep=_no_sleep)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@cache
def _contract_select(row_surface: str) -> tuple[str, ...]:
    """Return ``request.gaql.select`` of ``row_{surface}.json``, in snake_case GAQL names."""
    contract = json.loads((CONTRACTS_DIR / f"{row_surface}.json").read_text(encoding="utf-8"))
    return tuple(contract["request"]["gaql"]["select"])


def _paths(fields: Iterable[str]) -> list[str]:
    """Return the camelCase REST paths of snake_case GAQL fields, in the given order."""
    return [".".join(rest_path(name)) for name in fields]


def _mask(fields: Iterable[str]) -> str:
    """Return the ``fieldMask`` Google sends for ``fields``: comma-joined camelCase paths."""
    return ",".join(_paths(fields))


def _shuffled_mask(query_id: str) -> str:
    """Return the template's correct mask in a different order, with spaces around entries."""
    paths = _paths(QUERIES[query_id].select)
    reordered = paths[1::2][::-1] + paths[0::2]
    return " , ".join(reordered)


class Relay:
    """Plays the n8n webhook: answers every POST with the next scripted JSON body.

    The last body is repeated once the script runs out, so a retry would be answered too and
    show up in :attr:`requests`.
    """

    def __init__(self, *bodies: object) -> None:
        self.bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request body and answer with HTTP 200 and the next scripted body."""
        self.requests.append(json.loads(request.content))
        index = min(len(self.requests), len(self.bodies)) - 1
        return httpx.Response(200, json=self.bodies[index])


@asynccontextmanager
async def _client(relay: Relay) -> AsyncIterator[GoogleAdsWebhookClient]:
    """Yield the production client posting to ``relay`` through ``build_client("google")``."""
    async with build_client("google", httpx.MockTransport(relay)) as http:
        yield GoogleAdsWebhookClient(
            http=http,
            budget=UNMETERED,
            webhook_url=SIMULATED_WEBHOOK_URL,
            secret_header=SECRET_HEADER,
            secret=SecretStr(SIMULATED_SECRET),
            customer_id=CUSTOMER,
            retry=RETRY,
        )


def _envelope(mask: object, rows: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Return the specified n8n envelope with ``mask``; rows only when given."""
    body: dict[str, Any] = {"fieldMask": mask, "queryResourceConsumption": "1"}
    if rows is not None:
        body["results"] = list(rows)
    return body


async def _expect_defect(
    query_id: str, body: object, *, scope: Mapping[str, object] | None = None
) -> tuple[QueryDefect, Relay, list[dict[str, Any]]]:
    """Run ``query_id`` against a relay answering ``body``; return the defect, relay and logs.

    Asserts the defect's shape: code ``validation``, the query id, and exactly one POST with
    no retry, both on the relay and on the client's counter.
    """
    relay = Relay(body)
    with capture_logs() as logs:
        async with _client(relay) as client:
            with pytest.raises(QueryDefect) as caught:
                await client.run_query(query_id, SCOPES[query_id] if scope is None else scope)
            calls_made = client.calls_made
    defect = caught.value
    assert defect.query_id == query_id
    assert defect.code == "validation"
    assert defect.endpoint == f"search {query_id}"
    assert len(relay.requests) == 1
    assert calls_made == 1
    rejected = [entry for entry in logs if entry["event"] == "google.search_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["query_id"] == query_id
    assert rejected[0]["error_code"] == "validation"
    return defect, relay, logs


def _assert_never_echoed(texts: Iterable[str], defect: QueryDefect, logs: Sequence[Any]) -> None:
    """Assert that none of ``texts`` appears in the defect or in any captured log entry."""
    rendered = [str(defect), defect.message, defect.reason, *(repr(entry) for entry in logs)]
    for text in texts:
        for line in rendered:
            assert text not in line


# ---------------------------------------------------------------------------------------------
# Correct projections are accepted
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("query_id", list(QUERIES))
async def test_a_correct_mask_in_any_order_is_accepted(query_id: str) -> None:
    relay = Relay(_envelope(_shuffled_mask(query_id)))
    async with _client(relay) as client:
        result = await client.run_query(query_id, SCOPES[query_id])
    assert (result.rows, result.pages, result.calls, result.capped) == ((), 1, 1, False)
    assert relay.requests[0]["query"] == QUERIES[query_id].render(**SCOPES[query_id])


@pytest.mark.anyio
async def test_rows_omitting_zero_metrics_under_the_correct_mask_are_kept_as_returned() -> None:
    rows = [*METRICLESS_QG3_ROWS, QG3_ROW]
    relay = Relay(_envelope(_shuffled_mask("Q-G3"), rows))
    async with _client(relay) as client:
        result = await client.run_query("Q-G3", SCOPES["Q-G3"])
    assert list(result.rows) == rows
    assert (result.pages, result.calls) == (1, 1)


@pytest.mark.anyio
async def test_a_zero_row_body_with_the_correct_mask_is_an_empty_page() -> None:
    mask = _mask(QUERIES["Q-G3"].select)
    relay = Relay(_envelope(mask))
    async with _client(relay) as client:
        page = await client.search_page("Q-G3", QUERIES["Q-G3"].render(**SCOPES["Q-G3"]))
    assert page.rows == ()
    assert page.next_page_token is None
    assert page.field_mask == mask
    assert not page.capped


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"results": [QG3_ROW], "fieldMask": None}, id="null-mask"),
        pytest.param({"results": [QG3_ROW]}, id="absent-mask"),
    ],
)
async def test_results_without_a_mask_are_accepted_as_settled(body: dict[str, Any]) -> None:
    relay = Relay(body)
    async with _client(relay) as client:
        page = await client.search_page("Q-G3", QUERIES["Q-G3"].render(**SCOPES["Q-G3"]))
    assert page.rows == (QG3_ROW,)
    assert page.field_mask is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"queryResourceConsumption": "1"}, id="neither"),
        pytest.param({"fieldMask": None, "results": None}, id="both-null"),
    ],
)
async def test_a_body_with_neither_results_nor_mask_is_still_a_defect(
    body: dict[str, Any],
) -> None:
    defect, _, _ = await _expect_defect("Q-G3", body)
    assert "neither results nor fieldMask" in defect.message


@pytest.mark.anyio
async def test_a_one_element_array_envelope_with_the_correct_mask_is_unwrapped() -> None:
    relay = Relay([_envelope(_shuffled_mask("Q-G3"), [QG3_ROW])])
    async with _client(relay) as client:
        result = await client.run_query("Q-G3", SCOPES["Q-G3"])
    assert result.rows == (QG3_ROW,)


@pytest.mark.anyio
async def test_a_one_element_array_envelope_with_a_truncated_mask_is_rejected() -> None:
    body = [_envelope(_mask(NON_METRIC_FIELDS["Q-G3"]), METRICLESS_QG3_ROWS)]
    defect, _, _ = await _expect_defect("Q-G3", body)
    assert "7 of 9 selected fields missing" in defect.message


# ---------------------------------------------------------------------------------------------
# Wrong projections are defects, after one POST and without retry
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_reported_qg3_mask_with_metricless_rows_is_a_defect_not_zero_facts() -> None:
    body = _envelope("campaign.id,segments.date", METRICLESS_QG3_ROWS)
    defect, _, logs = await _expect_defect("Q-G3", body)
    assert defect.message == (
        "webhook response fieldMask does not list exactly the selected fields: 7 of 9 selected "
        "fields missing (metrics.clicks, metrics.conversions, metrics.conversionsValue, "
        "metrics.costMicros, metrics.impressions and 2 more)"
    )
    assert defect.http_status == 200
    _assert_never_echoed(
        [str(SCOPES["Q-G3"][START]), "SELECT", "segments.date BETWEEN"], defect, logs
    )


@pytest.mark.anyio
@pytest.mark.parametrize("query_id", ["Q-G3", "Q-G5", "Q-G6"])
async def test_a_mask_without_the_metrics_is_a_defect(query_id: str) -> None:
    fields = NON_METRIC_FIELDS[query_id]
    metrics = len(QUERIES[query_id].select) - len(fields)
    defect, _, _ = await _expect_defect(query_id, _envelope(_mask(fields), [{}]))
    total = len(QUERIES[query_id].select)
    assert f"{metrics} of {total} selected fields missing" in defect.message
    assert "unselected" not in defect.message
    assert "empty" not in defect.message


@pytest.mark.anyio
@pytest.mark.parametrize("query_id", ["Q-G3", "Q-G5", "Q-G6"])
async def test_a_mask_missing_its_last_field_is_a_defect(query_id: str) -> None:
    select = QUERIES[query_id].select
    defect, _, _ = await _expect_defect(query_id, _envelope(_mask(select[:-1])))
    missing = ".".join(rest_path(select[-1]))
    assert f"1 of {len(select)} selected fields missing ({missing})" in defect.message


@pytest.mark.anyio
async def test_an_extra_path_is_a_defect_counted_but_not_echoed() -> None:
    mask = _mask(QUERIES["Q-G3"].select) + "," + RELAY_MARKER
    defect, _, logs = await _expect_defect("Q-G3", _envelope(mask, [QG3_ROW]))
    assert defect.message.endswith("selected fields: 1 unselected entry")
    _assert_never_echoed([RELAY_MARKER, "relayInjected"], defect, logs)


@pytest.mark.anyio
async def test_a_wrong_path_is_a_defect_naming_the_missing_selected_path() -> None:
    # The snake_case GAQL name in place of the camelCase REST path Google returns.
    paths = _paths(QUERIES["Q-G3"].select)
    mask = ",".join("metrics.cost_micros" if p == "metrics.costMicros" else p for p in paths)
    defect, _, logs = await _expect_defect("Q-G3", _envelope(mask, [QG3_ROW]))
    assert defect.message.endswith(
        "1 of 9 selected fields missing (metrics.costMicros); 1 unselected entry"
    )
    _assert_never_echoed(["metrics.cost_micros"], defect, logs)


@pytest.mark.anyio
async def test_an_empty_entry_is_a_defect() -> None:
    paths = _paths(QUERIES["Q-G3"].select)
    mask = ",".join(paths[:4]) + ",," + ",".join(paths[4:])
    defect, _, _ = await _expect_defect("Q-G3", _envelope(mask, [QG3_ROW]))
    assert defect.message.endswith("selected fields: 1 empty entry")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "rows",
    [pytest.param(None, id="zero-rows"), pytest.param([QG3_ROW], id="with-results")],
)
async def test_an_empty_string_mask_is_a_defect(rows: list[dict[str, Any]] | None) -> None:
    defect, _, _ = await _expect_defect("Q-G3", _envelope("", rows))
    assert "9 of 9 selected fields missing" in defect.message
    assert defect.message.endswith("; 1 empty entry")


@pytest.mark.anyio
async def test_missing_paths_beyond_five_are_summarised_as_a_count() -> None:
    defect, _, _ = await _expect_defect("Q-G6", _envelope("campaign.id", [{}]))
    assert "12 of 13 selected fields missing" in defect.message
    assert defect.message.endswith(" and 7 more)")
    named = defect.message.split("(", 1)[1].split(" and ", 1)[0].split(", ")
    assert len(named) == 5


@pytest.mark.anyio
async def test_relay_entries_never_reach_the_error_or_the_logs() -> None:
    leaked = ["lead@example.com", "customers/1234567890", RELAY_MARKER]
    mask = ",".join([*_paths(NON_METRIC_FIELDS["Q-G6"]), *leaked, ""])
    defect, _, logs = await _expect_defect("Q-G6", _envelope(mask, [{}]))
    assert "3 unselected entries; 1 empty entry" in defect.message
    _assert_never_echoed([*leaked, "REDACTED"], defect, logs)


@pytest.mark.anyio
async def test_a_wrong_mask_on_a_next_page_fails_the_query_without_retry() -> None:
    first = {**_envelope(_mask(QUERIES["Q-G3"].select), [QG3_ROW]), "nextPageToken": "page-2"}
    second = _envelope(_mask(NON_METRIC_FIELDS["Q-G3"]), METRICLESS_QG3_ROWS)
    relay = Relay(first, second)
    async with _client(relay) as client:
        with pytest.raises(QueryDefect) as caught:
            await client.run_query("Q-G3", SCOPES["Q-G3"])
        assert client.calls_made == 2
    assert caught.value.query_id == "Q-G3"
    assert [request["page_token"] for request in relay.requests] == [None, "page-2"]


@pytest.mark.anyio
async def test_streaming_search_yields_no_row_of_a_wrongly_projected_page() -> None:
    relay = Relay(_envelope(_mask(NON_METRIC_FIELDS["Q-G3"]), METRICLESS_QG3_ROWS))
    yielded: list[Mapping[str, Any]] = []

    async def drain(client: GoogleAdsWebhookClient) -> None:
        async for row in client.search("Q-G3", SCOPES["Q-G3"]):
            yielded.append(row)

    async with _client(relay) as client:
        with pytest.raises(QueryDefect) as caught:
            await drain(client)
    assert yielded == []
    assert caught.value.code == "validation"
    assert len(relay.requests) == 1


@pytest.mark.anyio
async def test_a_dated_qg3_read_as_partitioning_issues_it_surfaces_the_defect() -> None:
    # The full-range dated form partitioning sends for one window: a wrong projection must
    # raise instead of yielding a result that could be accepted as complete.
    scope = {START: DAY, END: date(2026, 9, 28), CAMPAIGN_LO: INT64_MIN_ID, CAMPAIGN_HI: INT64_MAX}
    body = _envelope("campaign.id,segments.date", METRICLESS_QG3_ROWS)
    defect, relay, _ = await _expect_defect("Q-G3", body, scope=scope)
    assert relay.requests[0]["query"] == QUERIES["Q-G3"].render(**scope)
    assert defect.query_id == "Q-G3"


# ---------------------------------------------------------------------------------------------
# search_page derives the projection from its GAQL text
# ---------------------------------------------------------------------------------------------

CUSTOM_GAQL = (
    "select  campaign.id ,\n  metrics.clicks\n  FROM campaign "
    "WHERE campaign.id IN (987654321, 876543219)"
)


@pytest.mark.anyio
async def test_search_page_accepts_the_mask_of_its_own_select_list() -> None:
    relay = Relay(_envelope("metrics.clicks,campaign.id", [QG3_ROW]))
    async with _client(relay) as client:
        page = await client.search_page("custom-1", CUSTOM_GAQL)
    assert page.rows == (QG3_ROW,)
    assert relay.requests[0]["query"] == CUSTOM_GAQL


@pytest.mark.anyio
async def test_search_page_rejects_a_mask_that_differs_from_its_select_list() -> None:
    relay = Relay(_envelope(_mask(QUERIES["Q-G3"].select), [QG3_ROW]))
    with capture_logs() as logs:
        async with _client(relay) as client:
            with pytest.raises(QueryDefect) as caught:
                await client.search_page("custom-1", CUSTOM_GAQL)
            assert client.calls_made == 1
    defect = caught.value
    assert (defect.query_id, defect.code) == ("custom-1", "validation")
    assert defect.message.endswith("selected fields: 7 unselected entries")
    assert len(relay.requests) == 1
    _assert_never_echoed(["987654321", "876543219", "IN (", "FROM campaign"], defect, logs)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n ", id="blank"),
        pytest.param("campaign.id FROM campaign WHERE campaign.id = 987654321", id="no-select"),
        pytest.param("SELECT campaign.id WHERE campaign.id = 987654321", id="no-from"),
        pytest.param("SELECT FROM campaign WHERE campaign.id = 987654321", id="no-fields"),
        pytest.param("SELECT campaign.id, FROM campaign", id="trailing-comma"),
        pytest.param("SELECT campaign.id,, metrics.clicks FROM campaign", id="empty-field"),
        pytest.param("SELECT Campaign.ID FROM campaign", id="not-snake-case"),
        pytest.param("SELECT campaign.id FROM", id="no-resource"),
        pytest.param("WITH x SELECT campaign.id FROM campaign", id="not-leading"),
    ],
)
async def test_search_page_rejects_text_without_a_readable_select_before_sending(
    query: str,
) -> None:
    relay = Relay(_envelope("campaign.id"))
    async with _client(relay) as client:
        with pytest.raises(ValueError, match="SELECT") as caught:
            await client.search_page("custom-1", query)
        assert client.calls_made == 0
    assert relay.requests == []
    message = str(caught.value)
    for fragment in ("987654321", "Campaign.ID", "WITH x"):
        assert fragment not in message


# ---------------------------------------------------------------------------------------------
# The expected projection is the row contracts' select list
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query_id", list(QUERIES))
def test_the_expected_projection_is_the_row_contract_select(query_id: str) -> None:
    template = QUERIES[query_id]
    _, context = _prepare(query_id, SCOPES[query_id])
    assert context.selected == frozenset(_paths(_contract_select(template.row_surface)))
    assert context.query_id == query_id


@pytest.mark.anyio
@pytest.mark.parametrize("query_id", list(QUERIES))
async def test_a_mask_built_from_the_row_contract_select_is_accepted(query_id: str) -> None:
    select = _contract_select(QUERIES[query_id].row_surface)
    relay = Relay(_envelope(_mask(reversed(select))))
    async with _client(relay) as client:
        result = await client.run_query(query_id, SCOPES[query_id])
    assert (result.rows, result.calls) == ((), 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "dropped"),
    [
        pytest.param(query_id, name, id=f"{query_id}-{name}")
        for query_id, template in QUERIES.items()
        for name in _contract_select(template.row_surface)
    ],
)
async def test_a_contract_mask_missing_any_single_path_is_rejected(
    query_id: str, dropped: str
) -> None:
    select = _contract_select(QUERIES[query_id].row_surface)
    mask = _mask(name for name in select if name != dropped)
    defect, _, _ = await _expect_defect(query_id, _envelope(mask, [{}]))
    assert f"missing ({'.'.join(rest_path(dropped))})" in defect.message
