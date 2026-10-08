"""Unit tests for ``ads_engine.connectors.google_n8n.partition``: completeness partitioning.

Covers :meth:`PartitionedFetcher.partitioned_fetch` for every template strategy: the Q-G2 and
Q-G4 keyset scans (``id >`` the last id, ``ORDER BY id LIMIT 9999``, a full page costing one more
call), the Q-G2b ``IN`` chunks of at most 1,000 ids with no status filter, the dated partitions
of Q-G3, Q-G5 and Q-G6 (date halving, then numeric ``campaign.id`` and ``ad_group.id`` ranges
from ``[0, INT64_MAX]``, then the Q-G5 criterion keyset or the Q-G6 finest partition), the Q-G7
ad-group range halving with its criterion keyset, page tokens, call-budget and transient
interruption with deferred scopes, the contract defects, and scope validation. It also covers
the ``partitioned_fetch(query_id, scope)`` signature with the client bound at construction, and
the safe-absence verdicts of :func:`enumerate_campaigns` and :func:`enumerate_entities`.

Every call runs through the production ``GoogleAdsWebhookClient`` and ``send_json`` on an
``httpx.MockTransport``. :class:`FakeGoogle` plays the n8n webhook in front of a small Google Ads
account: it checks each request against ``backend/tests/contracts/google/search.json``, decodes
the GAQL with ``queries.parse_query`` and honours its date range, id ranges, ``id >`` keyset,
``IN`` list, ``REMOVED`` exclusion, ``ORDER BY`` and ``LIMIT``. It answers with the specified
envelope ``{results, fieldMask, queryResourceConsumption}`` and no page token, at most 10,000
rows per response, so a larger match comes back capped exactly as the live relay returns it.
Rows carry camelCase keys and INT64 values as strings, use only the fields their
``row_*.json`` contract defines, and the first and last row of every response are checked
against that contract. Rows are rendered from compact :class:`Cell` runs of keys, and identical
responses are serialised once, so a 63-level id-range descent stays fast. Deliberately
misbehaving relays (an ignored filter, an ignored ``LIMIT``, a malformed row, a repeated token)
are opt-in and named where used. No test touches the network, a database or a real sleep.
"""

import functools
import inspect
import itertools
import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from structlog.typing import EventDict

from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.google_n8n.client import (
    SIMULATED_SECRET,
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    CallBudget,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.google_n8n.partition import (
    REASON_BUDGET,
    REASON_FINEST_PARTITION,
    REASON_INTERRUPTED,
    REASON_RESULT_LIMIT,
    CampaignEnumeration,
    EntityEnumeration,
    FetchOutcome,
    PartitionedFetcher,
    enumerate_campaigns,
    enumerate_entities,
)
from ads_engine.connectors.google_n8n.queries import (
    AD_GROUP_HI,
    AD_GROUP_ID,
    AD_GROUP_LO,
    CAMPAIGN_HI,
    CAMPAIGN_LO,
    END,
    IDS,
    INT64_MAX,
    INT64_MIN_ID,
    KEYSET_LIMIT,
    LAST_ID,
    MAX_IDS_PER_IN,
    PAGE_SIZE,
    QUERIES,
    START,
    ParsedQuery,
    QueryTemplate,
    parse_query,
    rest_path,
)
from ads_engine.connectors.http import RetryPolicy

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts" / "google"

CUSTOMER = "1234567890"
SECRET_HEADER = "X-Webhook-Token"
DAY1, DAY2, DAY3, DAY4 = (date(2026, 9, day) for day in range(1, 5))

# The first split of the full id range, by the module's rule lo + (hi - lo) // 2.
FULL_MID = INT64_MIN_ID + (INT64_MAX - INT64_MIN_ID) // 2
# Halvings that narrow [0, INT64_MAX] (2**63 ids) down to a single id.
HALVINGS = (INT64_MAX - INT64_MIN_ID).bit_length()

# n8n's observed reply to a workflow that failed (HTTP 500, no Google error body).
N8N_WORKFLOW_ERROR = {"message": "Error in workflow"}
PERMISSION_DENIED = {
    "error": {
        "code": 403,
        "message": "The caller does not have permission",
        "status": "PERMISSION_DENIED",
    }
}
INVALID_ARGUMENT = {
    "error": {
        "code": 400,
        "message": "Request contains an invalid argument.",
        "status": "INVALID_ARGUMENT",
    }
}

REMOVED = "REMOVED"
# Q-G2, Q-G2b and Q-G3 rows are keyed by campaign id, Q-G4 rows by ad-group id.
CAMPAIGN_KEYED = frozenset({"Q-G2", "Q-G2b", "Q-G3"})


async def _no_sleep(seconds: float) -> None:
    """Skip the backoff between retries; the attempts still happen and are counted."""


RETRY = RetryPolicy(sleep=_no_sleep)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict."""
    with capture_logs() as entries:
        yield entries


# --- contracts and the conformance check ------------------------------------------------------


@functools.cache
def _contract(surface: str) -> Mapping[str, Any]:
    """Load ``contracts/google/{surface}.json``."""
    loaded: Mapping[str, Any] = json.loads(
        (CONTRACTS_DIR / f"{surface}.json").read_text(encoding="utf-8")
    )
    return loaded


_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
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


# --- the fake account -------------------------------------------------------------------------


class Cell(NamedTuple):
    """A run of rows the fake account holds for one query, in ascending key order.

    ``keys`` are the rows' own ids: the campaign id for Q-G2, Q-G2b and Q-G3, the ad-group id
    for Q-G4, the criterion id for Q-G5 and Q-G7, a search-term ordinal for Q-G6 and the
    conversion action id for Q-G8. ``campaign`` and ``ad_group`` are the parents of rows keyed
    by something else; ``day`` is ``segments.date`` of dated rows.
    """

    keys: Sequence[int]
    day: date | None = None
    campaign: int = 0
    ad_group: int = 0
    status: str = "ENABLED"


Segment = tuple[Cell, Sequence[int]]
"""Some of one cell's keys, in order: a slice of the rows a response carries."""


def _window(segments: Sequence[Segment], start: int, stop: int) -> list[Segment]:
    """Return the rows at positions ``start`` to ``stop - 1`` of a selection, as segments."""
    window: list[Segment] = []
    position = 0
    for cell, keys in segments:
        lo, hi = max(start - position, 0), min(stop - position, len(keys))
        if lo < hi:
            window.append((cell, keys[lo:hi]))
        position += len(keys)
        if position >= stop:
            break
    return window


class Call(NamedTuple):
    """One webhook request the fake received."""

    parsed: ParsedQuery
    query: str
    page_token: str | None


def _rn(*parts: object) -> str:
    """Return a resource name under the test customer, such as ``customers/1/campaigns/7``."""
    return "/".join(("customers", CUSTOMER, *(str(part) for part in parts)))


def _campaign_ref(campaign: int) -> dict[str, str]:
    """Return the attributed ``campaign`` object of a row."""
    return {"resourceName": _rn("campaigns", campaign), "id": str(campaign)}


def _ad_group_ref(ad_group: int) -> dict[str, str]:
    """Return the attributed ``adGroup`` object of a row."""
    return {"resourceName": _rn("adGroups", ad_group), "id": str(ad_group)}


def _segments(cell: Cell) -> dict[str, str]:
    """Return the ``segments`` object of a dated row."""
    assert cell.day is not None, "a dated query's cell needs a day"
    return {"date": cell.day.isoformat()}


def _customer_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G1 row (row_customer.json)."""
    return {
        "customer": {
            "resourceName": _rn(),
            "id": CUSTOMER,
            "currencyCode": "USD",
            "timeZone": "America/New_York",
        }
    }


def _campaign_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G2 or Q-G2b row (row_campaign.json); ``key`` is the campaign id."""
    return {
        "campaign": {
            "resourceName": _rn("campaigns", key),
            "id": str(key),
            "name": f"Campaign {key}",
            "status": cell.status,
            "advertisingChannelType": "SEARCH",
        }
    }


def _campaign_daily_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G3 row (row_campaign_daily.json); ``key`` is the campaign id."""
    return {"campaign": _campaign_ref(key), "segments": _segments(cell)}


def _ad_group_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G4 row (row_ad_group.json); ``key`` is the ad-group id."""
    return {
        "adGroup": {
            "resourceName": _rn("adGroups", key),
            "id": str(key),
            "name": f"Ad group {key}",
            "status": cell.status,
        },
        "campaign": _campaign_ref(cell.campaign),
    }


def _keyword_view_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G5 row (row_keyword_view.json); ``key`` is the criterion id."""
    criterion = f"{cell.ad_group}~{key}"
    return {
        "keywordView": {"resourceName": _rn("keywordViews", criterion)},
        "adGroupCriterion": {
            "resourceName": _rn("adGroupCriteria", criterion),
            "criterionId": str(key),
        },
        "adGroup": _ad_group_ref(cell.ad_group),
        "segments": _segments(cell),
    }


def _search_term_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G6 row that matched no keyword (row_search_term_view.json); ``key`` names the term."""
    return {
        "searchTermView": {
            "resourceName": _rn("searchTermViews", f"{cell.campaign}~{cell.ad_group}~t{key}"),
            "searchTerm": f"term {key}",
            "status": "NONE",
        },
        "segments": _segments(cell),
        "adGroup": _ad_group_ref(cell.ad_group),
        "campaign": _campaign_ref(cell.campaign),
    }


def _keyword_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G7 row with no quality score (row_ad_group_criterion.json); ``key`` is the criterion."""
    return {
        "adGroupCriterion": {
            "resourceName": _rn("adGroupCriteria", f"{cell.ad_group}~{key}"),
            "criterionId": str(key),
            "status": cell.status,
            "keyword": {"text": f"keyword {key}", "matchType": "EXACT"},
        },
        "adGroup": _ad_group_ref(cell.ad_group),
        "campaign": _campaign_ref(cell.campaign),
    }


def _conversion_action_row(cell: Cell, key: int) -> dict[str, Any]:
    """A Q-G8 row (row_conversion_action.json); ``key`` is the action id."""
    return {
        "conversionAction": {
            "resourceName": _rn("conversionActions", key),
            "id": str(key),
            "name": f"Action {key}",
            "status": cell.status,
            "countingType": "ONE_PER_CLICK",
        }
    }


ROW_BUILDERS: Mapping[str, Callable[[Cell, int], dict[str, Any]]] = {
    "row_customer": _customer_row,
    "row_campaign": _campaign_row,
    "row_campaign_daily": _campaign_daily_row,
    "row_ad_group": _ad_group_row,
    "row_keyword_view": _keyword_view_row,
    "row_search_term_view": _search_term_row,
    "row_ad_group_criterion": _keyword_row,
    "row_conversion_action": _conversion_action_row,
}


def _field_mask(template: QueryTemplate) -> str:
    """Return the ``fieldMask`` Google sends: the camelCase paths of the selected fields."""
    return ",".join(".".join(rest_path(name)) for name in template.select)


def _excludes_removed(template: QueryTemplate) -> bool:
    """Whether the template's literal conditions exclude ``REMOVED`` rows (Q-G2, Q-G4, Q-G7)."""
    return any(condition.endswith(f"!= '{REMOVED}'") for condition in template.fixed_conditions)


def _cell_in_scope(query_id: str, cell: Cell, scope: Mapping[str, Any]) -> bool:
    """Whether a cell's day and parents satisfy the scope's date range and parent filters."""
    if START in scope and (cell.day is None or not scope[START] <= cell.day <= scope[END]):
        return False
    if (
        CAMPAIGN_LO in scope
        and query_id not in CAMPAIGN_KEYED
        and not scope[CAMPAIGN_LO] <= cell.campaign <= scope[CAMPAIGN_HI]
    ):
        return False
    if AD_GROUP_LO in scope and not scope[AD_GROUP_LO] <= cell.ad_group <= scope[AD_GROUP_HI]:
        return False
    return not (AD_GROUP_ID in scope and cell.ad_group != scope[AD_GROUP_ID])


def _keys_in_scope(query_id: str, keys: Sequence[int], scope: Mapping[str, Any]) -> Sequence[int]:
    """Return a cell's keys that satisfy the scope's key range, ``id >`` keyset and ``IN`` list."""
    lo, hi = INT64_MIN_ID, INT64_MAX
    if query_id in CAMPAIGN_KEYED and CAMPAIGN_LO in scope:
        lo, hi = scope[CAMPAIGN_LO], scope[CAMPAIGN_HI]
    if LAST_ID in scope:
        lo = max(lo, scope[LAST_ID] + 1)
    within = keys[bisect_left(keys, lo) : bisect_right(keys, hi)]
    if IDS in scope:
        wanted = frozenset(scope[IDS])
        return [key for key in within if key in wanted]
    return within


@dataclass
class FakeGoogle:
    """The n8n webhook relaying GAQL to a fake Google Ads account.

    Attributes:
        world: The account's cells per query id, in Google's natural row order.
        ignored: Scope keys whose condition a misbehaving relay drops (``START`` drops the
            date range, ``CAMPAIGN_LO`` or ``AD_GROUP_LO`` a range, ``LAST_ID`` the keyset
            filter, ``AD_GROUP_ID`` the single ad group, ``IDS`` the ``IN`` list).
        honour_limit: ``False`` models a relay that ignores ``LIMIT 9999`` on a keyset page and
            returns up to a full 10,000-row page.
        mangle: Applied to every row after its contract check, to model a malformed row.
        fault: Returns the ``(status, body)`` to answer a request with instead of rows.
        page_rows: When set, results come in pages of this many rows chained by
            ``nextPageToken``. The specified n8n response carries no token.
        repeat_token: Whether every page carries the same token, as a broken relay might.
        calls: Every request received, in order.
    """

    world: Mapping[str, Sequence[Cell]]
    ignored: frozenset[str] = frozenset()
    honour_limit: bool = True
    mangle: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    fault: Callable[[ParsedQuery], tuple[int, object] | None] | None = None
    page_rows: int | None = None
    repeat_token: bool = False
    calls: list[Call] = field(default_factory=list)
    _tokens: dict[str, tuple[str, int]] = field(default_factory=dict)
    _bodies: dict[object, bytes] = field(default_factory=dict)

    @property
    def scopes(self) -> list[dict[str, object]]:
        """The scope of every first-page request, in order."""
        return [dict(call.parsed.scope) for call in self.calls if call.page_token is None]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one webhook POST."""
        body = _checked_request(request)
        query, token = body["query"], body["page_token"]
        try:
            parsed = parse_query(query)
        except ValueError as exc:
            pytest.fail(f"the client sent GAQL that no template renders: {exc}")
        self.calls.append(Call(parsed, query, token))
        if self.fault is not None:
            reply = self.fault(parsed)
            if reply is not None:
                status, payload = reply
                return httpx.Response(status, json=payload)
        offset = 0
        if token is not None:
            assert token in self._tokens, "the client sent a token the relay never issued"
            issued_for, offset = self._tokens[token]
            assert issued_for == query, "a page token was sent back with another query"
        selected = self._select(parsed)
        if self.page_rows is None:
            page, next_token = _window(selected, 0, PAGE_SIZE), None
        else:
            end = offset + self.page_rows
            page = _window(selected, offset, end)
            next_token = None
            if end < sum(len(keys) for _, keys in selected):
                next_token = "again" if self.repeat_token else f"page-{end}"
                self._tokens[next_token] = (query, end)
        return httpx.Response(
            200,
            content=self._body(parsed.template, page, next_token),
            headers={"Content-Type": "application/json"},
        )

    def _select(self, parsed: ParsedQuery) -> list[Segment]:
        """Return the rows the query matches as segments of cells, ordered and limited.

        Without ``ORDER BY`` the rows come in the account's natural order. A keyset form sorts
        them by key and applies its ``LIMIT``, unless the relay ignores it.
        """
        query_id = parsed.query_id
        scope = {key: value for key, value in parsed.scope.items() if key not in self.ignored}
        excludes_removed = _excludes_removed(parsed.template)
        segments: list[Segment] = []
        for cell in self.world.get(query_id, ()):
            if excludes_removed and cell.status == REMOVED:
                continue
            if not _cell_in_scope(query_id, cell, scope):
                continue
            keys = _keys_in_scope(query_id, cell.keys, scope)
            if keys:
                segments.append((cell, keys))
        if parsed.order_by is None:
            return segments
        limit = parsed.limit if self.honour_limit else PAGE_SIZE
        ordered = sorted(
            ((cell, key) for cell, keys in segments for key in keys), key=lambda pair: pair[1]
        )[:limit]
        runs = (list(run) for _, run in itertools.groupby(ordered, key=lambda pair: id(pair[0])))
        return [(run[0][0], [key for _, key in run]) for run in runs]

    def _body(self, template: QueryTemplate, page: Sequence[Segment], token: str | None) -> bytes:
        """Render and serialise one response, once per distinct page."""
        cache_key = (
            template.query_id,
            token,
            tuple(
                (id(cell), keys if isinstance(keys, range) else tuple(keys)) for cell, keys in page
            ),
        )
        if self.mangle is None and cache_key in self._bodies:
            return self._bodies[cache_key]
        build = ROW_BUILDERS[template.row_surface]
        rows = [build(cell, key) for cell, keys in page for key in keys]
        row_schema = _contract(template.row_surface)["response"]["body"]
        for row in rows[:1] + rows[-1:]:
            assert _violations(row, row_schema) == []
        if self.mangle is not None:
            rows = [self.mangle(row) for row in rows]
        envelope: dict[str, Any] = {"fieldMask": _field_mask(template)}
        if rows:
            # Google omits an empty repeated field, so an empty page carries no results.
            envelope["results"] = rows
        envelope["queryResourceConsumption"] = str(max(1, len(rows)))
        if token is not None:
            envelope["nextPageToken"] = token
        sample = {**envelope, "results": rows[:1]} if rows else envelope
        assert _violations(sample, _contract("search")["response"]["body"]) == []
        content = json.dumps(envelope).encode("utf-8")
        if self.mangle is None:
            self._bodies[cache_key] = content
        return content


def _checked_request(request: httpx.Request) -> dict[str, Any]:
    """Assert that a webhook request follows search.json, and return its decoded body."""
    contract = _contract("search")["request"]
    assert request.method == contract["method"]
    assert str(request.url) == SIMULATED_WEBHOOK_URL
    assert request.headers[SECRET_HEADER] == SIMULATED_SECRET
    assert request.headers["Content-Type"] == "application/json"
    body: dict[str, Any] = json.loads(request.content)
    assert list(body) == ["customer_id", "query", "page_token"]
    assert _violations(body, contract["body"]) == []
    assert body["customer_id"] == CUSTOMER
    return body


class _Budget:
    """A call budget that admits ``allowed`` POST attempts and refuses every later one."""

    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.admitted = 0
        self.refused = 0

    async def acquire(self) -> None:
        """Admit one attempt, or raise ``CallBudgetExhausted`` once the allowance is used."""
        if self.admitted >= self.allowed:
            self.refused += 1
            raise CallBudgetExhausted()
        self.admitted += 1


@asynccontextmanager
async def _client(
    fake: FakeGoogle, budget: CallBudget = UNMETERED
) -> AsyncIterator[GoogleAdsWebhookClient]:
    """Yield the production client posting to ``fake``, retrying without sleeping."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
        yield GoogleAdsWebhookClient(
            http=http,
            budget=budget,
            webhook_url=SIMULATED_WEBHOOK_URL,
            secret_header=SECRET_HEADER,
            secret=SecretStr(SIMULATED_SECRET),
            customer_id=CUSTOMER,
            retry=RETRY,
        )


async def _fetch(
    fake: FakeGoogle,
    query_id: str,
    scope: Mapping[str, object] | None = None,
    budget: CallBudget = UNMETERED,
) -> FetchOutcome:
    """Run one :meth:`PartitionedFetcher.partitioned_fetch` against ``fake``."""
    async with _client(fake, budget) as client:
        return await PartitionedFetcher(client).partitioned_fetch(query_id, scope)


def _value(row: Mapping[str, Any], gaql_field: str) -> Any:
    """Return the raw value of one GAQL field of a row."""
    node: Any = row
    for key in rest_path(gaql_field):
        node = node[key]
    return node


def _ids(rows: Sequence[Mapping[str, Any]], gaql_field: str) -> list[int]:
    """Return one INT64 field of every row, as integers."""
    return [int(_value(row, gaql_field)) for row in rows]


_MISSING = object()


def _set_field(gaql_field: str, value: object) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a mangle that sets one field of every row to ``value``, or deletes it."""
    *parents, leaf = rest_path(gaql_field)

    def mangle(row: dict[str, Any]) -> dict[str, Any]:
        """Rewrite one rendered row."""
        node = row
        for key in parents:
            node = node[key]
        if value is _MISSING:
            del node[leaf]
        else:
            node[leaf] = value
        return row

    return mangle


def _fail_when(
    predicate: Callable[[ParsedQuery], bool], status: int, body: object
) -> Callable[[ParsedQuery], tuple[int, object] | None]:
    """Return a fault answering every request matching ``predicate`` with ``status``."""

    def fault(parsed: ParsedQuery) -> tuple[int, object] | None:
        """Answer a matching request with the scripted failure."""
        return (status, body) if predicate(parsed) else None

    return fault


def _events(entries: Sequence[EventDict], name: str) -> list[EventDict]:
    """Return the captured events called ``name``."""
    return [entry for entry in entries if entry["event"] == name]


# --- constants --------------------------------------------------------------------------------


def test_partition_limits_are_the_limits_the_search_contract_records() -> None:
    """PAGE_SIZE, KEYSET_LIMIT and MAX_IDS_PER_IN equal search.json's request limits."""
    limits = _contract("search")["request"]["limits"]

    recorded = (limits["page_size"], limits["keyset_limit"], limits["in_list_max"])

    assert recorded == (PAGE_SIZE, KEYSET_LIMIT, MAX_IDS_PER_IN)
    assert isinstance(_Budget(1), CallBudget)


# --- keyset scans: Q-G2 and Q-G4 --------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "key_field"), [("Q-G2", "campaign.id"), ("Q-G4", "ad_group.id")]
)
async def test_keyset_scan_continues_from_the_largest_id_of_each_full_page(
    query_id: str, key_field: str
) -> None:
    """Q-G2 and Q-G4 page by ``id > last ORDER BY id LIMIT 9999`` until a short page."""
    keys = range(7, 7 + 3 * (KEYSET_LIMIT + 1), 3)
    removed = Cell(range(8, 9), campaign=5, status=REMOVED)
    fake = FakeGoogle({query_id: [Cell(keys, campaign=5), removed]})

    outcome = await _fetch(fake, query_id)

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: keys[KEYSET_LIMIT - 1]}]
    assert all(call.parsed.limit == KEYSET_LIMIT for call in fake.calls)
    assert all(call.parsed.order_by == key_field for call in fake.calls)
    assert _ids(outcome.rows, key_field) == list(keys)
    assert outcome.query_id == query_id
    assert outcome.complete
    assert (outcome.completeness, outcome.reason) == ("complete", None)
    assert (outcome.calls, outcome.partitions_read) == (2, 2)
    assert (outcome.deferred, outcome.interrupted_by) == ((), None)


@pytest.mark.anyio
async def test_a_page_of_exactly_the_keyset_limit_costs_one_more_call() -> None:
    """A full 9,999-row page cannot prove the end, so one more call reads the empty page."""
    fake = FakeGoogle({"Q-G2": [Cell(range(1, KEYSET_LIMIT + 1))]})

    outcome = await _fetch(fake, "Q-G2")

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: KEYSET_LIMIT}]
    assert len(outcome.rows) == KEYSET_LIMIT
    assert outcome.complete
    assert outcome.calls == 2


@pytest.mark.anyio
async def test_keyset_scan_resumes_after_a_given_last_id() -> None:
    """A deferred keyset scope ``{last_id: n}`` reads only the ids above n."""
    fake = FakeGoogle({"Q-G4": [Cell(range(10, 15), campaign=3)]})

    outcome = await _fetch(fake, "Q-G4", {LAST_ID: 12})

    assert fake.scopes == [{LAST_ID: 12}]
    assert _ids(outcome.rows, "ad_group.id") == [13, 14]
    assert outcome.complete


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [1, 1.0], ids=["json-integer", "integral-number"])
async def test_an_id_sent_as_a_json_number_is_read_as_its_integer(raw: object) -> None:
    """Off-contract but tolerated: an integral JSON number is an INT64 id like its string."""
    fake = FakeGoogle({"Q-G2": [Cell(range(1, 2))]}, mangle=_set_field("campaign.id", raw))

    outcome = await _fetch(fake, "Q-G2")

    assert [_value(row, "campaign.id") for row in outcome.rows] == [1]
    assert outcome.complete


# --- by ids: Q-G2b ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_by_ids_reads_sorted_distinct_chunks_with_no_status_filter() -> None:
    """Q-G2b sends at most 1,000 sorted, distinct ids per IN list and returns REMOVED rows."""
    requested = [*range(MAX_IDS_PER_IN + 1, 0, -1), 5]
    fake = FakeGoogle(
        {
            "Q-G2b": [
                Cell(range(1, 4), status=REMOVED),
                Cell(range(MAX_IDS_PER_IN + 1, MAX_IDS_PER_IN + 2)),
            ]
        }
    )

    outcome = await _fetch(fake, "Q-G2b", {IDS: requested})

    assert fake.scopes == [
        {IDS: tuple(range(1, MAX_IDS_PER_IN + 1))},
        {IDS: (MAX_IDS_PER_IN + 1,)},
    ]
    assert QUERIES["Q-G2b"].fixed_conditions == ()
    assert all("status" not in call.query.split(" WHERE ", 1)[1] for call in fake.calls)
    assert _ids(outcome.rows, "campaign.id") == [1, 2, 3, MAX_IDS_PER_IN + 1]
    statuses = [_value(row, "campaign.status") for row in outcome.rows]
    assert statuses == [REMOVED, REMOVED, REMOVED, "ENABLED"]
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (2, 2)


@pytest.mark.anyio
async def test_by_ids_with_no_ids_is_complete_without_a_call() -> None:
    """An empty Q-G2b id collection reads nothing and sends nothing."""
    fake = FakeGoogle({})

    outcome = await _fetch(fake, "Q-G2b", {IDS: []})

    assert fake.calls == []
    assert outcome.complete
    assert (outcome.rows, outcome.calls, outcome.partitions_read) == ((), 0, 0)


# --- dated partitions: Q-G3, Q-G5, Q-G6 -------------------------------------------------------


@pytest.mark.anyio
async def test_dated_response_below_the_page_size_completes_its_partition() -> None:
    """9,999 rows with no token are complete: one call, no partitioning."""
    fake = FakeGoogle({"Q-G3": [Cell(range(1, PAGE_SIZE), day=DAY1)]})

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY2})

    assert fake.scopes == [{START: DAY1, END: DAY2}]
    assert len(outcome.rows) == PAGE_SIZE - 1
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (1, 1)


def _four_day_world() -> dict[str, list[Cell]]:
    """Q-G3 rows over four days: days 1 and 2 hold exactly 10,000, days 1 to 4 hold more."""
    return {
        "Q-G3": [
            Cell(range(1, PAGE_SIZE), day=DAY1),
            Cell(range(1, 2), day=DAY2),
            Cell(range(1, 2), day=DAY3),
            Cell(range(1, 2), day=DAY4),
        ]
    }


@pytest.mark.anyio
async def test_capped_date_range_is_halved_down_to_single_days() -> None:
    """Exactly 10,000 rows with no token halve the dates; capped rows are re-read, not kept."""
    fake = FakeGoogle(_four_day_world())

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY4})

    assert fake.scopes == [
        {START: DAY1, END: DAY4},
        {START: DAY1, END: DAY2},
        {START: DAY1, END: DAY1},
        {START: DAY2, END: DAY2},
        {START: DAY3, END: DAY4},
    ]
    read = [(_value(row, "segments.date"), _value(row, "campaign.id")) for row in outcome.rows]
    assert len(read) == len(set(read)) == PAGE_SIZE + 2
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (5, 5)


@pytest.mark.anyio
async def test_capped_day_splits_numeric_campaign_ranges_from_the_full_int64_range() -> None:
    """At one day, a capped Q-G3 splits [0, INT64_MAX] by campaign.id, covering any id."""
    low = Cell(range(1, PAGE_SIZE), day=DAY1)
    # No entity list holds this id; a numeric range still covers it.
    high = Cell(range(INT64_MAX, INT64_MAX + 1), day=DAY1)
    fake = FakeGoogle({"Q-G3": [low, high]})

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY1})

    day = {START: DAY1, END: DAY1}
    assert fake.scopes == [
        day,
        {**day, CAMPAIGN_LO: INT64_MIN_ID, CAMPAIGN_HI: FULL_MID},
        {**day, CAMPAIGN_LO: FULL_MID + 1, CAMPAIGN_HI: INT64_MAX},
    ]
    assert _ids(outcome.rows, "campaign.id") == [*low.keys, INT64_MAX]
    assert outcome.complete


HEAVY_CAMPAIGN = 7_000_000_001
OTHER_CAMPAIGN = FULL_MID + 5
HEAVY_AD_GROUP = 9_000_000_002
SIBLING_AD_GROUP = HEAVY_AD_GROUP + 1


@pytest.mark.anyio
async def test_q_g5_descends_dates_campaigns_ad_groups_then_a_criterion_keyset() -> None:
    """Q-G5 halves dates, then campaign.id and ad_group.id from [0, INT64_MAX], then keysets."""
    heavy = Cell(
        range(1, PAGE_SIZE + 1), day=DAY1, campaign=HEAVY_CAMPAIGN, ad_group=HEAVY_AD_GROUP
    )
    world = [
        heavy,
        Cell(range(1, 3), day=DAY1, campaign=HEAVY_CAMPAIGN, ad_group=SIBLING_AD_GROUP),
        Cell(range(1, 2), day=DAY1, campaign=OTHER_CAMPAIGN, ad_group=5),
        Cell(range(1, 3), day=DAY2, campaign=HEAVY_CAMPAIGN, ad_group=HEAVY_AD_GROUP),
    ]
    fake = FakeGoogle({"Q-G5": world})

    outcome = await _fetch(fake, "Q-G5", {START: DAY1, END: DAY2})

    day = {START: DAY1, END: DAY1}
    one_campaign = {**day, CAMPAIGN_LO: HEAVY_CAMPAIGN, CAMPAIGN_HI: HEAVY_CAMPAIGN}
    keyset = {**one_campaign, AD_GROUP_ID: HEAVY_AD_GROUP}
    scopes = fake.scopes
    assert scopes[:3] == [
        {START: DAY1, END: DAY2},
        day,
        {**day, CAMPAIGN_LO: INT64_MIN_ID, CAMPAIGN_HI: FULL_MID},
    ]
    assert one_campaign in scopes
    assert {**one_campaign, AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: FULL_MID} in scopes
    assert {**one_campaign, AD_GROUP_LO: HEAVY_AD_GROUP, AD_GROUP_HI: HEAVY_AD_GROUP} in scopes
    assert {**keyset, LAST_ID: INT64_MIN_ID} in scopes
    assert {**keyset, LAST_ID: KEYSET_LIMIT} in scopes
    assert scopes[-1] == {START: DAY2, END: DAY2}
    # Two date calls, two calls per id halving in each dimension, two keyset pages, day 2.
    assert len(scopes) == outcome.calls == outcome.partitions_read == 2 + 4 * HALVINGS + 3
    read = [
        (
            _value(row, "segments.date"),
            _value(row, "ad_group.id"),
            _value(row, "ad_group_criterion.criterion_id"),
        )
        for row in outcome.rows
    ]
    assert len(read) == len(set(read)) == sum(len(cell.keys) for cell in world)
    assert outcome.complete


@pytest.mark.anyio
async def test_q_g5_criterion_keyset_resumes_on_a_single_day() -> None:
    """A deferred Q-G5 keyset scope reads its ad group's criteria above ``last_id``."""
    fake = FakeGoogle({"Q-G5": [Cell(range(1, 4), day=DAY1, campaign=2, ad_group=9)]})

    outcome = await _fetch(fake, "Q-G5", {START: DAY1, END: DAY1, AD_GROUP_ID: 9, LAST_ID: 1})

    assert fake.scopes == [{START: DAY1, END: DAY1, AD_GROUP_ID: 9, LAST_ID: 1}]
    assert _ids(outcome.rows, "ad_group_criterion.criterion_id") == [2, 3]
    assert outcome.complete


def _finest_scope(first_ad_group: int, last_ad_group: int) -> dict[str, object]:
    """A Q-G6 scope of one day and one campaign over an ad-group range."""
    return {
        START: DAY1,
        END: DAY1,
        CAMPAIGN_LO: HEAVY_CAMPAIGN,
        CAMPAIGN_HI: HEAVY_CAMPAIGN,
        AD_GROUP_LO: first_ad_group,
        AD_GROUP_HI: last_ad_group,
    }


def _heavy_search_terms(*ad_groups: int) -> dict[str, list[Cell]]:
    """Q-G6 rows: 10,000 search terms on day 1 in each given ad group of the heavy campaign."""
    return {
        "Q-G6": [
            Cell(range(1, PAGE_SIZE + 1), day=DAY1, campaign=HEAVY_CAMPAIGN, ad_group=ad_group)
            for ad_group in ad_groups
        ]
    }


@pytest.mark.anyio
async def test_q_g6_finest_partition_at_the_limit_is_kept_and_incomplete() -> None:
    """A single (day, ad group) of Q-G6 returning 10,000 rows is stored as returned."""
    fake = FakeGoogle(_heavy_search_terms(HEAVY_AD_GROUP))

    outcome = await _fetch(fake, "Q-G6", _finest_scope(HEAVY_AD_GROUP, HEAVY_AD_GROUP))

    assert len(outcome.rows) == PAGE_SIZE
    assert not outcome.complete
    assert outcome.completeness == "incomplete"
    assert outcome.reason == (
        f"{REASON_FINEST_PARTITION} ({DAY1.isoformat()}, ad group {HEAVY_AD_GROUP})"
    )
    assert (outcome.deferred, outcome.interrupted_by) == ((), None)
    assert (outcome.calls, outcome.partitions_read) == (1, 1)


@pytest.mark.anyio
async def test_q_g6_reason_names_three_finest_partitions_then_counts_the_rest() -> None:
    """Halving reaches each capped ad group; the reason names three and counts the others."""
    groups = range(HEAVY_AD_GROUP, HEAVY_AD_GROUP + 4)
    fake = FakeGoogle(_heavy_search_terms(*groups))

    outcome = await _fetch(fake, "Q-G6", _finest_scope(groups[0], groups[-1]))

    assert [scope[AD_GROUP_LO] for scope in fake.scopes] == [
        groups[0],
        groups[0],
        groups[0],
        groups[1],
        groups[2],
        groups[2],
        groups[3],
    ]
    named = "; ".join(f"{DAY1.isoformat()}, ad group {group}" for group in groups[:3])
    assert outcome.reason == f"{REASON_FINEST_PARTITION} ({named}; and 1 more)"
    assert sorted(set(_ids(outcome.rows, "ad_group.id"))) == list(groups)
    assert len(outcome.rows) == len(groups) * PAGE_SIZE


@pytest.mark.anyio
async def test_a_repairable_reason_comes_before_the_finest_partition_reason() -> None:
    """Budget deferral and a finest-partition overflow are both reported, deferral first."""
    fake = FakeGoogle(_heavy_search_terms(HEAVY_AD_GROUP, SIBLING_AD_GROUP))

    outcome = await _fetch(
        fake, "Q-G6", _finest_scope(HEAVY_AD_GROUP, SIBLING_AD_GROUP), budget=_Budget(2)
    )

    assert outcome.reason == (
        f"{REASON_BUDGET}; 1 partition deferred; "
        f"{REASON_FINEST_PARTITION} ({DAY1.isoformat()}, ad group {HEAVY_AD_GROUP})"
    )
    assert outcome.deferred == (_finest_scope(SIBLING_AD_GROUP, SIBLING_AD_GROUP),)
    assert len(outcome.rows) == PAGE_SIZE


# --- ad-group ranges: Q-G7 --------------------------------------------------------------------


@pytest.mark.anyio
async def test_q_g7_halves_ad_group_ranges_then_keysets_a_capped_ad_group() -> None:
    """A capped Q-G7 range is halved; a capped single ad group is read by criterion keyset."""
    first = HEAVY_AD_GROUP
    world = [
        Cell(range(1, PAGE_SIZE + 1), campaign=HEAVY_CAMPAIGN, ad_group=first),
        Cell(range(1, 3), campaign=HEAVY_CAMPAIGN, ad_group=first + 1),
        Cell(range(5, 6), campaign=HEAVY_CAMPAIGN, ad_group=first + 2),
    ]
    fake = FakeGoogle({"Q-G7": world})

    outcome = await _fetch(fake, "Q-G7", {AD_GROUP_LO: first, AD_GROUP_HI: first + 2})

    assert fake.scopes == [
        {AD_GROUP_LO: first, AD_GROUP_HI: first + 2},
        {AD_GROUP_LO: first, AD_GROUP_HI: first + 1},
        {AD_GROUP_LO: first, AD_GROUP_HI: first},
        {AD_GROUP_ID: first, LAST_ID: INT64_MIN_ID},
        {AD_GROUP_ID: first, LAST_ID: KEYSET_LIMIT},
        {AD_GROUP_LO: first + 1, AD_GROUP_HI: first + 1},
        {AD_GROUP_LO: first + 2, AD_GROUP_HI: first + 2},
    ]
    keywords = list(
        zip(
            _ids(outcome.rows, "ad_group.id"),
            _ids(outcome.rows, "ad_group_criterion.criterion_id"),
            strict=True,
        )
    )
    assert len(keywords) == len(set(keywords)) == sum(len(cell.keys) for cell in world)
    assert outcome.complete
    assert outcome.calls == 7


@pytest.mark.anyio
async def test_q_g7_criterion_keyset_resumes_from_a_deferred_scope() -> None:
    """A deferred Q-G7 keyset scope reads one ad group's criteria above ``last_id``."""
    fake = FakeGoogle(
        {
            "Q-G7": [
                Cell(range(1, 4), campaign=2, ad_group=9),
                Cell(range(1, 4), campaign=2, ad_group=10),
            ]
        }
    )

    outcome = await _fetch(fake, "Q-G7", {AD_GROUP_ID: 9, LAST_ID: 1})

    assert fake.scopes == [{AD_GROUP_ID: 9, LAST_ID: 1}]
    assert _ids(outcome.rows, "ad_group_criterion.criterion_id") == [2, 3]
    assert outcome.complete


# --- single queries: Q-G1 and Q-G8 ------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "count", "reason"),
    [("Q-G1", 1, None), ("Q-G8", PAGE_SIZE, REASON_RESULT_LIMIT)],
)
async def test_single_query_is_one_call_and_a_capped_one_is_kept_incomplete(
    query_id: str, count: int, reason: str | None
) -> None:
    """Q-G1 and Q-G8 run once; a capped answer is kept and reported as the result limit."""
    fake = FakeGoogle({query_id: [Cell(range(1, count + 1))]})

    outcome = await _fetch(fake, query_id)

    assert fake.scopes == [{}]
    assert len(outcome.rows) == count
    assert outcome.reason == reason
    assert outcome.complete is (reason is None)
    assert (outcome.calls, outcome.deferred) == (1, ())


# --- page tokens ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_page_tokens_are_followed_within_one_partition() -> None:
    """Each nextPageToken is sent back at once with the same query; the pages are one read."""
    fake = FakeGoogle({"Q-G3": [Cell(range(1, 6), day=DAY1)]}, page_rows=2)

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY1})

    assert [call.page_token for call in fake.calls] == [None, "page-2", "page-4"]
    assert len({call.query for call in fake.calls}) == 1
    assert _ids(outcome.rows, "campaign.id") == [1, 2, 3, 4, 5]
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (3, 1)


@pytest.mark.anyio
async def test_a_full_page_carrying_a_token_is_followed_not_partitioned() -> None:
    """10,000 rows with a token are not capped: the next page completes the partition."""
    fake = FakeGoogle({"Q-G3": [Cell(range(1, PAGE_SIZE + 2), day=DAY1)]}, page_rows=PAGE_SIZE)

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY1})

    assert fake.scopes == [{START: DAY1, END: DAY1}]
    assert len(outcome.rows) == PAGE_SIZE + 1
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (2, 1)


@pytest.mark.anyio
async def test_a_repeated_page_token_is_a_query_defect() -> None:
    """A relay repeating a token fails the fetch as a QueryDefect instead of looping."""
    fake = FakeGoogle({"Q-G3": [Cell(range(1, 6), day=DAY1)]}, page_rows=2, repeat_token=True)

    with pytest.raises(QueryDefect) as caught:
        await _fetch(fake, "Q-G3", {START: DAY1, END: DAY1})

    assert caught.value.message == "page token repeated"
    assert caught.value.query_id == "Q-G3"
    assert [call.page_token for call in fake.calls] == [None, "again"]


# --- interruption: call budget and transient failures -----------------------------------------


@pytest.mark.anyio
async def test_budget_exhaustion_mid_scan_defers_the_next_page_and_keeps_the_rows(
    captured: list[EventDict],
) -> None:
    """A refused keyset page is deferred as a resumable scope; rows read so far are kept."""
    keys = range(1, KEYSET_LIMIT + 2)
    fake = FakeGoogle({"Q-G2": [Cell(keys)]})
    budget = _Budget(1)

    outcome = await _fetch(fake, "Q-G2", budget=budget)

    assert not outcome.complete
    assert _ids(outcome.rows, "campaign.id") == list(keys[:KEYSET_LIMIT])
    assert outcome.deferred == ({LAST_ID: KEYSET_LIMIT},)
    assert isinstance(outcome.deferred[0], MappingProxyType)
    with pytest.raises(TypeError):
        outcome.deferred[0][LAST_ID] = INT64_MIN_ID  # type: ignore[index]
    assert isinstance(outcome.interrupted_by, CallBudgetExhausted)
    assert outcome.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert (outcome.calls, outcome.partitions_read) == (1, 1)
    assert (budget.admitted, budget.refused, len(fake.calls)) == (1, 1, 1)

    resumed = await _fetch(fake, "Q-G2", outcome.deferred[0])

    assert _ids(resumed.rows, "campaign.id") == [keys[-1]]
    assert resumed.complete
    interrupted, finished = _events(captured, "google.partitioned_fetch")
    assert interrupted["log_level"] == "warning"
    assert interrupted["interrupted_by"] == "CALL_BUDGET_EXHAUSTED"
    assert (interrupted["calls"], interrupted["rows"], interrupted["deferred"]) == (
        1,
        KEYSET_LIMIT,
        1,
    )
    assert (finished["log_level"], finished["completeness"]) == ("info", "complete")
    assert "interrupted_by" not in finished
    assert not any("SELECT" in str(value) for value in interrupted.values())


@pytest.mark.anyio
async def test_budget_exhaustion_mid_partition_defers_every_unread_partition_in_order() -> None:
    """The refused partition and every partition still pending are deferred in reading order."""
    fake = FakeGoogle(_four_day_world())

    outcome = await _fetch(fake, "Q-G3", {START: DAY1, END: DAY4}, budget=_Budget(2))

    assert outcome.deferred == (
        {START: DAY1, END: DAY1},
        {START: DAY2, END: DAY2},
        {START: DAY3, END: DAY4},
    )
    assert outcome.rows == ()
    assert outcome.reason == f"{REASON_BUDGET}; 3 partitions deferred"
    assert (outcome.calls, outcome.partitions_read) == (2, 2)

    resumed = [await _fetch(fake, "Q-G3", scope) for scope in outcome.deferred]

    assert all(part.complete for part in resumed)
    assert sum(len(part.rows) for part in resumed) == PAGE_SIZE + 2


@pytest.mark.anyio
async def test_a_transient_failure_after_every_retry_interrupts_the_fetch() -> None:
    """A 5xx surviving its retries defers the failed chunk; every attempt is counted."""
    last = MAX_IDS_PER_IN + 1
    fake = FakeGoogle(
        {"Q-G2b": [Cell(range(1, 3))]},
        fault=_fail_when(lambda parsed: parsed.scope[IDS] == (last,), 500, N8N_WORKFLOW_ERROR),
    )

    outcome = await _fetch(fake, "Q-G2b", {IDS: range(1, last + 1)})

    error = outcome.interrupted_by
    assert isinstance(error, TransientError)
    assert not isinstance(error, CallBudgetExhausted)
    assert error.http_status == 500
    assert outcome.reason == f"{REASON_INTERRUPTED}: {error.reason}; 1 partition deferred"
    assert outcome.deferred == ({IDS: (last,)},)
    assert _ids(outcome.rows, "campaign.id") == [1, 2]
    assert (outcome.calls, outcome.partitions_read) == (1 + RETRY.attempts, 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [(403, PERMISSION_DENIED, AccessDenied), (400, INVALID_ARGUMENT, QueryDefect)],
)
async def test_access_denied_and_query_defect_propagate_unchanged(
    status: int, body: object, error: type[Exception], captured: list[EventDict]
) -> None:
    """AccessDenied and QueryDefect stop the fetch and propagate; the failure is logged."""
    fake = FakeGoogle(
        {"Q-G2b": [Cell(range(1, 3))]},
        fault=_fail_when(lambda parsed: parsed.scope[IDS] == (MAX_IDS_PER_IN + 1,), status, body),
    )

    with pytest.raises(error) as caught:
        await _fetch(fake, "Q-G2b", {IDS: range(1, MAX_IDS_PER_IN + 2)})

    assert getattr(caught.value, "http_status", None) == status
    assert len(fake.calls) == 2
    (event,) = _events(captured, "google.partitioned_fetch_failed")
    assert (event["query_id"], event["partitions_read"], event["calls"], event["rows"]) == (
        "Q-G2b",
        1,
        2,
        2,
    )


# --- contract defects -------------------------------------------------------------------------


def _defect_cases() -> list[Any]:
    """Responses that break a partition's contract, and the defect each one raises."""
    one_day = {START: DAY1, END: DAY1}
    return [
        pytest.param(
            "Q-G2",
            {LAST_ID: 3},
            functools.partial(
                FakeGoogle, {"Q-G2": [Cell(range(1, 6))]}, ignored=frozenset({LAST_ID})
            ),
            "keyset did not advance",
            id="keyset-not-advancing",
        ),
        pytest.param(
            "Q-G2",
            None,
            functools.partial(FakeGoogle, {"Q-G2": [Cell(range(1, 4)), Cell(range(2, 3))]}),
            "keyset page repeated an id",
            id="keyset-repeated-id",
        ),
        pytest.param(
            "Q-G4",
            None,
            functools.partial(
                FakeGoogle, {"Q-G4": [Cell(range(1, PAGE_SIZE + 1))]}, honour_limit=False
            ),
            "keyset page over limit",
            id="keyset-over-limit",
        ),
        pytest.param(
            "Q-G2b",
            {IDS: [1]},
            functools.partial(
                FakeGoogle, {"Q-G2b": [Cell(range(1, PAGE_SIZE + 1))]}, ignored=frozenset({IDS})
            ),
            f"by-ids chunk returned a full page of {PAGE_SIZE} rows with no page token",
            id="by-ids-capped",
        ),
        pytest.param(
            "Q-G2b",
            {IDS: [2]},
            functools.partial(FakeGoogle, {"Q-G2b": [Cell(range(1, 4))]}, ignored=frozenset({IDS})),
            "by-ids chunk returned an unrequested or repeated campaign",
            id="by-ids-unrequested",
        ),
        pytest.param(
            "Q-G2b",
            {IDS: [2]},
            functools.partial(FakeGoogle, {"Q-G2b": [Cell(range(2, 3)), Cell(range(2, 3))]}),
            "by-ids chunk returned an unrequested or repeated campaign",
            id="by-ids-repeated",
        ),
        pytest.param(
            "Q-G3",
            {**one_day, CAMPAIGN_LO: 4, CAMPAIGN_HI: 4},
            functools.partial(FakeGoogle, {"Q-G3": [Cell([4] * PAGE_SIZE, day=DAY1)]}),
            f"a single campaign-day returned a full page of {PAGE_SIZE} rows; the query has "
            "one row per campaign per day",
            id="q-g3-capped-campaign-day",
        ),
        pytest.param(
            "Q-G3",
            {START: DAY1, END: DAY2},
            functools.partial(
                FakeGoogle,
                {"Q-G3": [Cell(range(1, 2), day=DAY1), Cell(range(1, 2), day=DAY3)]},
                ignored=frozenset({START}),
            ),
            "row outside the requested partition (segments.date)",
            id="outside-date",
        ),
        pytest.param(
            "Q-G3",
            {**one_day, CAMPAIGN_LO: 2, CAMPAIGN_HI: 2},
            functools.partial(
                FakeGoogle,
                {"Q-G3": [Cell(range(1, 4), day=DAY1)]},
                ignored=frozenset({CAMPAIGN_LO}),
            ),
            "row outside the requested partition (campaign.id)",
            id="outside-campaign-range",
        ),
        pytest.param(
            "Q-G6",
            {**one_day, AD_GROUP_LO: 7, AD_GROUP_HI: 7},
            functools.partial(
                FakeGoogle,
                {
                    "Q-G6": [
                        Cell(range(1, 2), day=DAY1, campaign=5, ad_group=7),
                        Cell(range(1, 2), day=DAY1, campaign=5, ad_group=9),
                    ]
                },
                ignored=frozenset({AD_GROUP_LO}),
            ),
            "row outside the requested partition (ad_group.id)",
            id="outside-ad-group-range",
        ),
        pytest.param(
            "Q-G5",
            {**one_day, AD_GROUP_ID: 7, LAST_ID: INT64_MIN_ID},
            functools.partial(
                FakeGoogle,
                {
                    "Q-G5": [
                        Cell(range(1, 3), day=DAY1, campaign=5, ad_group=7),
                        Cell(range(3, 5), day=DAY1, campaign=5, ad_group=9),
                    ]
                },
                ignored=frozenset({AD_GROUP_ID}),
            ),
            "row outside the requested partition (ad_group.id)",
            id="outside-keyset-ad-group",
        ),
        pytest.param(
            "Q-G7",
            {AD_GROUP_LO: 7, AD_GROUP_HI: 7},
            functools.partial(
                FakeGoogle,
                {
                    "Q-G7": [
                        Cell(range(1, 2), campaign=5, ad_group=7),
                        Cell(range(1, 2), campaign=5, ad_group=9),
                    ]
                },
                ignored=frozenset({AD_GROUP_LO}),
            ),
            "row outside the requested partition (ad_group.id)",
            id="outside-q-g7-range",
        ),
        *(
            pytest.param(
                "Q-G3",
                one_day,
                functools.partial(
                    FakeGoogle,
                    {"Q-G3": [Cell(range(1, 2), day=DAY1)]},
                    mangle=_set_field("segments.date", text),
                ),
                "segments.date: expected a YYYY-MM-DD date on every row",
                id=f"date-{text}",
            )
            for text in ("2026-02-30", "2026-9-1")
        ),
        *(
            pytest.param(
                "Q-G2",
                None,
                functools.partial(
                    FakeGoogle, {"Q-G2": [Cell(range(1, 2))]}, mangle=_set_field("campaign.id", raw)
                ),
                "campaign.id: expected an INT64 id on every row",
                id=f"id-{label}",
            )
            for label, raw in (
                ("negative", "-1"),
                ("exponent", "1e3"),
                ("empty", ""),
                ("overflow", str(INT64_MAX + 1)),
                ("boolean", True),
                ("fraction", 1.5),
                ("absent", _MISSING),
            )
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("query_id", "scope", "make", "message"), _defect_cases())
async def test_a_response_that_breaks_the_partition_contract_is_a_query_defect(
    query_id: str,
    scope: Mapping[str, object] | None,
    make: Callable[[], FakeGoogle],
    message: str,
) -> None:
    """Rows outside their partition, a stuck keyset or a malformed row raise QueryDefect."""
    fake = make()

    with pytest.raises(QueryDefect) as caught:
        await _fetch(fake, query_id, scope)

    defect = caught.value
    assert defect.message == message
    assert (defect.code, defect.source) == ("validation", "google")
    assert (defect.endpoint, defect.query_id) == (f"search {query_id}", query_id)
    assert len(fake.calls) == 1


# --- scope validation -------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "scope", "message"),
    [
        ("Q-G9", None, "unknown Google Ads query id 'Q-G9'"),
        (
            "SELECT campaign.id FROM campaign",
            None,
            "unknown Google Ads query id (not an identifier)",
        ),
        ("Q-G2b", None, "Q-G2b: scope key 'ids' is required"),
        ("Q-G2b", {IDS: [1], LAST_ID: 0}, "Q-G2b: scope key 'last_id' is not accepted here"),
        ("Q-G2b", {IDS: "12"}, "Q-G2b: scope key 'ids' must be a collection of int ids"),
        ("Q-G2b", {IDS: {1: 1}}, "Q-G2b: scope key 'ids' must be a collection of int ids"),
        ("Q-G2b", {IDS: 12}, "Q-G2b: scope key 'ids' must be a collection of int ids"),
        ("Q-G2b", {IDS: [True]}, "Q-G2b: ids must hold int ids, not bool"),
        ("Q-G2b", {IDS: [INT64_MAX + 1]}, "Q-G2b: ids must hold ids between 0 and INT64_MAX"),
        ("Q-G3", {START: DAY1}, "Q-G3: a dated query needs scope keys 'start' and 'end'"),
        ("Q-G3", {START: DAY2, END: DAY1}, "Q-G3: scope key 'start' must not follow 'end'"),
        ("Q-G2", {START: DAY1}, "Q-G2: scope key 'start' is not accepted here"),
        (
            "Q-G5",
            {START: DAY1, END: DAY2, AD_GROUP_ID: 7, LAST_ID: 0},
            "Q-G5: a criterion keyset ('ad_group_id' with 'last_id') needs a single day",
        ),
        ("Q-G7", {LAST_ID: 0}, "Q-G7: scope key 'ad_group_id' is required with 'last_id'"),
    ],
)
async def test_an_unknown_query_id_or_invalid_scope_is_rejected_before_any_call(
    query_id: str, scope: Mapping[str, object] | None, message: str
) -> None:
    """Programming errors raise ValueError and send nothing."""
    fake = FakeGoogle({})

    with pytest.raises(ValueError, match=re.escape(message)):
        await _fetch(fake, query_id, scope)

    assert fake.calls == []


# --- safe absence: enumerate_campaigns --------------------------------------------------------


@pytest.mark.anyio
async def test_enumerate_campaigns_with_nothing_absent_skips_q_g2b() -> None:
    """A complete scan returning every tracked id is complete with nothing removed or missing."""
    fake = FakeGoogle({"Q-G2": [Cell(range(1, 4))]})

    async with _client(fake) as client:
        enumeration = await enumerate_campaigns(client, [1, 2])

    assert [call.parsed.query_id for call in fake.calls] == ["Q-G2"]
    assert _ids(enumeration.present, "campaign.id") == [1, 2, 3]
    assert (enumeration.removed, enumeration.missing_ids) == ((), frozenset())
    assert enumeration.complete
    assert (enumeration.reason, enumeration.calls, enumeration.interrupted_by) == (None, 1, None)


@pytest.mark.anyio
async def test_enumerate_campaigns_classifies_absent_ids_after_complete_reads(
    captured: list[EventDict],
) -> None:
    """Q-G2b re-reads absent tracked ids: REMOVED is removed, unreturned is missing."""
    fake = FakeGoogle(
        {
            "Q-G2": [Cell(range(1, 3)), Cell(range(5, 6), status=REMOVED)],
            # Campaign 7 was not in the scan; it is returned un-removed on the re-read.
            "Q-G2b": [
                Cell(range(1, 3)),
                Cell(range(5, 6), status=REMOVED),
                Cell(range(7, 8), status="PAUSED"),
            ],
        }
    )

    async with _client(fake) as client:
        enumeration = await enumerate_campaigns(client, {9, 7, 5, 1})

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {IDS: (5, 7, 9)}]
    assert _ids(enumeration.present, "campaign.id") == [1, 2, 7]
    assert _ids(enumeration.removed, "campaign.id") == [5]
    assert enumeration.missing_ids == frozenset({9})
    assert enumeration.complete
    assert (enumeration.reason, enumeration.calls, enumeration.interrupted_by) == (None, 2, None)
    (event,) = _events(captured, "google.enumerate_campaigns")
    assert (event["tracked"], event["present"], event["removed"], event["missing"]) == (4, 3, 1, 1)
    assert event["complete"] is True
    with pytest.raises(FrozenInstanceError):
        enumeration.complete = False  # type: ignore[misc]


@pytest.mark.anyio
async def test_enumerate_campaigns_infers_nothing_after_an_interrupted_scan() -> None:
    """A deferred Q-G2 page leaves the verdict incomplete and Q-G2b unsent."""
    keys = range(1, KEYSET_LIMIT + 2)
    fake = FakeGoogle({"Q-G2": [Cell(keys)]})

    async with _client(fake, _Budget(1)) as client:
        enumeration = await enumerate_campaigns(client, [keys[-1], INT64_MAX])

    assert [call.parsed.query_id for call in fake.calls] == ["Q-G2"]
    assert len(enumeration.present) == KEYSET_LIMIT
    assert (enumeration.removed, enumeration.missing_ids) == ((), frozenset())
    assert not enumeration.complete
    assert enumeration.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert isinstance(enumeration.interrupted_by, CallBudgetExhausted)
    assert enumeration.calls == 1


@pytest.mark.anyio
async def test_enumerate_campaigns_infers_nothing_when_a_q_g2b_chunk_fails() -> None:
    """A failed Q-G2b chunk discards every removal and disappearance of the sync."""
    absent = range(2, MAX_IDS_PER_IN + 3)
    fake = FakeGoogle(
        {
            "Q-G2": [Cell(range(1, 2))],
            "Q-G2b": [Cell(range(2, 4), status=REMOVED), Cell(range(4, 5), status="PAUSED")],
        },
        fault=_fail_when(
            lambda parsed: parsed.query_id == "Q-G2b" and parsed.scope[IDS] == (absent[-1],),
            500,
            N8N_WORKFLOW_ERROR,
        ),
    )

    async with _client(fake) as client:
        enumeration = await enumerate_campaigns(client, [1, *absent])

    sent = [call.parsed.query_id for call in fake.calls]
    assert sent == ["Q-G2", "Q-G2b", *["Q-G2b"] * RETRY.attempts]
    # The first chunk's rows (2 and 3 removed, 4 paused) wait in the progress for the
    # verdict, which only a re-read whose every chunk succeeded draws.
    assert _ids(enumeration.present, "campaign.id") == [1]
    assert enumeration.progress is not None
    assert [dict(scope) for scope in enumeration.progress.pending] == [{IDS: (absent[-1],)}]
    assert _ids(enumeration.progress.reread_rows, "campaign.id") == [2, 3, 4]
    assert (enumeration.removed, enumeration.missing_ids) == ((), frozenset())
    assert not enumeration.complete
    assert isinstance(enumeration.interrupted_by, TransientError)
    assert enumeration.reason is not None
    assert enumeration.reason.startswith(f"{REASON_INTERRUPTED}: ")
    assert enumeration.calls == 2 + RETRY.attempts


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tracked", "message"),
    [
        (["12"], "Q-G2: tracked keys must hold int ids, not str"),
        ([True], "Q-G2: tracked keys must hold int ids, not bool"),
        ([-1], "Q-G2: tracked keys must hold ids between 0 and INT64_MAX"),
    ],
)
async def test_enumerate_campaigns_rejects_an_invalid_tracked_id_before_any_call(
    tracked: list[object], message: str
) -> None:
    """A tracked id that could never match a row is rejected instead of reported missing."""
    fake = FakeGoogle({})

    async with _client(fake) as client:
        with pytest.raises(ValueError, match=re.escape(message)):
            await enumerate_campaigns(client, tracked)  # type: ignore[arg-type]

    assert fake.calls == []


# --- safe absence: enumerate_entities ---------------------------------------------------------


@pytest.mark.anyio
async def test_enumerate_entities_q_g4_reports_absent_ad_groups_after_a_complete_scan(
    captured: list[EventDict],
) -> None:
    """Q-G4 keys rows by ad_group.id; tracked ids not returned are absent."""
    fake = FakeGoogle(
        {"Q-G4": [Cell(range(10, 12), campaign=1), Cell(range(30, 31), campaign=1, status=REMOVED)]}
    )

    async with _client(fake) as client:
        enumeration = await enumerate_entities(client, "Q-G4", [10, 30, 40])

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}]
    assert _ids(enumeration.rows, "ad_group.id") == [10, 11]
    assert enumeration.absent_keys == frozenset({30, 40})
    assert enumeration.complete
    assert (enumeration.reason, enumeration.calls, enumeration.interrupted_by) == (None, 1, None)
    (event,) = _events(captured, "google.enumerate_entities")
    assert (event["query_id"], event["tracked"], event["absent"]) == ("Q-G4", 3, 2)


@pytest.mark.anyio
async def test_enumerate_entities_q_g7_reports_absent_keyword_pairs() -> None:
    """Q-G7 reads the full ad-group range and keys rows by (ad group, criterion)."""
    fake = FakeGoogle(
        {
            "Q-G7": [
                Cell(range(1, 3), campaign=1, ad_group=10),
                Cell(range(1, 2), campaign=1, ad_group=20),
                Cell(range(2, 3), campaign=1, ad_group=20, status=REMOVED),
            ]
        }
    )

    async with _client(fake) as client:
        enumeration = await enumerate_entities(client, "Q-G7", [(10, 1), (20, 1), (20, 2)])

    assert fake.scopes == [{AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: INT64_MAX}]
    assert len(enumeration.rows) == 3
    assert enumeration.absent_keys == frozenset({(20, 2)})
    assert enumeration.complete
    assert isinstance(enumeration, EntityEnumeration)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "tracked"), [("Q-G4", [10]), ("Q-G7", [(10, 1)])], ids=["Q-G4", "Q-G7"]
)
async def test_enumerate_entities_infers_nothing_from_an_incomplete_read(
    query_id: Any, tracked: list[Any]
) -> None:
    """An interrupted enumeration reports no absent key, whatever was tracked."""
    fake = FakeGoogle({}, fault=_fail_when(lambda parsed: True, 500, N8N_WORKFLOW_ERROR))

    async with _client(fake) as client:
        enumeration = await enumerate_entities(client, query_id, tracked)

    assert (enumeration.rows, enumeration.absent_keys) == ((), frozenset())
    assert not enumeration.complete
    assert enumeration.reason is not None
    assert enumeration.reason.startswith(f"{REASON_INTERRUPTED}: ")
    assert isinstance(enumeration.interrupted_by, TransientError)
    assert enumeration.calls == RETRY.attempts


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query_id", "tracked", "message"),
    [
        ("Q-G2", [1], "enumerate_entities reads 'Q-G4' or 'Q-G7' only"),
        ("Q-G4", ["10"], "Q-G4: tracked keys must hold int ids, not str"),
        ("Q-G7", [(10,)], "Q-G7: tracked keys must be (ad_group_id, criterion_id) tuples"),
        ("Q-G7", [[10, 1]], "Q-G7: tracked keys must be (ad_group_id, criterion_id) tuples"),
        ("Q-G7", [(10, "1")], "Q-G7: tracked keys must hold int ids, not str"),
    ],
)
async def test_enumerate_entities_rejects_invalid_arguments_before_any_call(
    query_id: Any, tracked: list[Any], message: str
) -> None:
    """A wrong query id or a tracked key of the wrong shape raises ValueError and sends nothing."""
    fake = FakeGoogle({})

    async with _client(fake) as client:
        with pytest.raises(ValueError, match=re.escape(message)):
            await enumerate_entities(client, query_id, tracked)

    assert fake.calls == []


# --- the bound fetcher: partitioned_fetch(query_id, scope) ------------------------------------


@pytest.mark.anyio
async def test_the_bound_fetcher_reads_a_query_id_and_scope_passed_positionally() -> None:
    """``partitioned_fetch("Q-G2", scope)`` and ``partitioned_fetch("Q-G2")`` read in full."""
    keys = range(1, 4)
    fake = FakeGoogle({"Q-G2": [Cell(keys)]})

    async with _client(fake) as client:
        fetcher = PartitionedFetcher(client)
        with_scope = await fetcher.partitioned_fetch("Q-G2", {"last_id": 0})
        without_scope = await fetcher.partitioned_fetch("Q-G2")

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: INT64_MIN_ID}]
    for outcome in (with_scope, without_scope):
        assert isinstance(outcome, FetchOutcome)
        assert outcome.query_id == "Q-G2"
        assert _ids(outcome.rows, "campaign.id") == list(keys)
        assert (outcome.completeness, outcome.reason) == ("complete", None)
        assert (outcome.calls, outcome.partitions_read, outcome.deferred) == (1, 1, ())
    assert client.calls_made == 2


def test_partitioned_fetch_takes_exactly_a_query_id_and_an_optional_scope() -> None:
    """The method's parameters are ``self``, ``query_id`` and ``scope=None``, in that order."""
    parameters = inspect.signature(PartitionedFetcher.partitioned_fetch).parameters

    assert list(parameters) == ["self", "query_id", "scope"]
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in parameters.values()
    )
    assert parameters["query_id"].default is inspect.Parameter.empty
    assert parameters["scope"].default is None
    assert inspect.iscoroutinefunction(PartitionedFetcher.partitioned_fetch)


@pytest.mark.anyio
async def test_a_deferred_scope_resumes_through_the_same_bound_fetcher() -> None:
    """Every deferred scope of an interrupted fetch completes on the fetcher that deferred it."""
    fake = FakeGoogle(_four_day_world())
    budget = _Budget(2)

    async with _client(fake, budget) as client:
        fetcher = PartitionedFetcher(client)
        interrupted = await fetcher.partitioned_fetch("Q-G3", {START: DAY1, END: DAY4})
        # A later 24 h window admits one call per deferred partition.
        budget.allowed += len(interrupted.deferred)
        resumed = [await fetcher.partitioned_fetch("Q-G3", scope) for scope in interrupted.deferred]

    assert not interrupted.complete
    assert interrupted.reason == f"{REASON_BUDGET}; 3 partitions deferred"
    assert fake.scopes[2:] == [dict(scope) for scope in interrupted.deferred]
    assert all(part.complete for part in resumed)
    assert [part.calls for part in resumed] == [1, 1, 1]
    read = [
        (_value(row, "segments.date"), _value(row, "campaign.id"))
        for part in resumed
        for row in part.rows
    ]
    assert len(read) == len(set(read)) == PAGE_SIZE + 2
    assert (client.calls_made, budget.refused) == (5, 1)


@pytest.mark.anyio
async def test_the_fetcher_exposes_its_bound_client_read_only_and_never_its_secrets() -> None:
    """``client`` is the bound client itself; no other attribute exists and nothing is sent."""
    fake = FakeGoogle({})

    async with _client(fake) as client:
        fetcher = PartitionedFetcher(client)

        assert fetcher.client is client
        with pytest.raises(AttributeError):
            fetcher.client = client  # type: ignore[misc]
        with pytest.raises(AttributeError):
            fetcher.budget = UNMETERED  # type: ignore[attr-defined]
        assert repr(fetcher) == f"PartitionedFetcher(client={client!r})"
        assert all(
            secret not in repr(fetcher) for secret in (SIMULATED_SECRET, SIMULATED_WEBHOOK_URL)
        )
        assert CUSTOMER not in repr(fetcher)

    assert fake.calls == []


# --- result types -----------------------------------------------------------------------------


def test_result_types_are_frozen() -> None:
    """FetchOutcome, CampaignEnumeration and EntityEnumeration cannot be modified."""
    outcome = FetchOutcome(
        query_id="Q-G1",
        rows=(),
        completeness="complete",
        reason=None,
        calls=0,
        partitions_read=0,
        deferred=(),
        interrupted_by=None,
    )
    campaigns = CampaignEnumeration(
        present=(),
        removed=(),
        missing_ids=frozenset(),
        complete=True,
        reason=None,
        calls=0,
        interrupted_by=None,
    )
    entities = EntityEnumeration(
        rows=(), absent_keys=frozenset(), complete=True, reason=None, calls=0, interrupted_by=None
    )

    assert outcome.complete
    assert not replace(outcome, completeness="incomplete").complete
    for result, name in ((outcome, "rows"), (campaigns, "present"), (entities, "rows")):
        with pytest.raises(FrozenInstanceError):
            setattr(result, name, ())
