"""Unit tests for Google Ads completeness partitioning and safe absence (AAP 0.3.3, 0.11.2).

``googleAds:search`` returns at most ``PAGE_SIZE`` rows per page and the n8n response carries no
page token, so a page of exactly ``PAGE_SIZE`` rows has unknown completeness. These tests prove,
on the smallest inputs that exercise each branch, that
``ads_engine.connectors.google_n8n.partition`` reaches completeness only through narrowing forms
whose completeness can be proven, and infers a removal or disappearance only from complete
reads:

* keyset scans (Q-G2, Q-G4): ``id > {last} ORDER BY id LIMIT 9999`` pages, continued only while
  a page is full; a relay that does not advance the keyset is a ``QueryDefect``;
* Q-G2b: ``IN`` lists of at most ``MAX_IDS_PER_IN`` ids and no status filter;
* dated queries (Q-G3, Q-G5, Q-G6): date halving to single days, then numeric ``campaign.id``
  and ``ad_group.id`` ranges halved from ``[0, INT64_MAX]``, then the Q-G5 criterion keyset or
  the Q-G6 finest partition kept as returned and reported incomplete; Q-G3 never goes below one
  campaign-day;
* Q-G7: ``ad_group.id`` range halving, then a criterion keyset inside one capped ad group;
* page tokens followed at once, call-budget interruption with deferred scopes, and
  ``AccessDenied`` and ``QueryDefect`` propagating unchanged;
* safe absence: :func:`enumerate_campaigns` and :func:`enumerate_entities` report ``removed``,
  ``missing`` and absent keys only after complete reads.

Every request goes through the production ``GoogleAdsWebhookClient`` and ``send_json`` on the
root fixture ``contract_checked_transport("google", ...)``, which validates each request against
``tests/contracts/google/search.json`` (body exactly ``{customer_id, query, page_token}``, the
secret header present) and each 2xx response against the envelope and its ``row_*`` contract.
:class:`FakeGoogle` plays the n8n webhook in front of a small account: it decodes the GAQL with
``queries.parse_query`` and honours date ranges, id ranges, ``id >`` keysets, ``IN`` lists, the
``REMOVED`` exclusion, ``ORDER BY`` and ``LIMIT``, answering at most ``PAGE_SIZE`` rows with no
page token, exactly as the live relay does.

Deep branches start from a narrowed scope (a single day, campaign or ad group), which
``partitioned_fetch`` accepts, so no test pays for a 63-level descent from ``[0, INT64_MAX]``;
the large-scale proofs (a 25,000-campaign scan, a 15,000-keyword ad group, full-range descents)
live in ``integration/test_google_n8n_client.py`` and ``unit/test_google_n8n_partition.py``.
No test touches the network, a database or a real sleep: the retry sleep is injected.

The plan names the entry point ``partitioned_fetch(client, query_id, scope)``; the module binds
the client at construction instead, so the tests call
``PartitionedFetcher(client).partitioned_fetch(query_id, scope)``.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.google_n8n.client import (
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    CallBudget,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.google_n8n.partition import (
    REASON_BUDGET,
    REASON_FINEST_PARTITION,
    REASON_INTERRUPTED,
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

pytestmark = [pytest.mark.anyio, pytest.mark.contract]

CUSTOMER = "1234567890"
SECRET_HEADER = "X-Webhook-Secret"
SECRET = "test-secret"
DAY1, DAY2, DAY3, DAY4 = (date(2026, 9, 14) + timedelta(days=offset) for offset in range(4))

# The first split of the full id range, by the module's rule lo + (hi - lo) // 2.
FULL_MID = INT64_MIN_ID + (INT64_MAX - INT64_MIN_ID) // 2

ENABLED = "ENABLED"
PAUSED = "PAUSED"
REMOVED = "REMOVED"

# Decoy ids and terms fill a capped response the fake returns by policy. They never collide
# with a real id of any test, so a decoy surviving into an outcome is detected.
DECOY_BASE = 700_000_000
DECOY_TERM = "decoy term"

# The fake's world per row contract: Q-G2 and Q-G2b read the same campaign rows.
CAMPAIGNS = "row_campaign"
CAMPAIGN_DAYS = "row_campaign_daily"
AD_GROUPS = "row_ad_group"
KEYWORD_DAYS = "row_keyword_view"
SEARCH_TERMS = "row_search_term_view"
KEYWORDS = "row_ad_group_criterion"


async def _no_sleep(seconds: float) -> None:
    """Skip the backoff between retries; every attempt still happens and is counted."""


RETRY = RetryPolicy(sleep=_no_sleep)

# n8n relaying Google errors with their HTTP status.
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
UNAVAILABLE = {
    "error": {
        "code": 503,
        "message": "The service is currently unavailable.",
        "status": "UNAVAILABLE",
    }
}


# --- rows shaped by the row_*.json contracts (required keys only) ------------------------------


def _ref(kind: str, *ids: int) -> str:
    """Return a Google Ads resource name: ``customers/{customer}/{kind}/{id}~{id}``."""
    return f"customers/{CUSTOMER}/{kind}/{'~'.join(str(part) for part in ids)}"


def _campaign_ref(campaign: int) -> dict[str, str]:
    """The ``campaign`` object every row that selects ``campaign.id`` carries."""
    return {"resourceName": _ref("campaigns", campaign), "id": str(campaign)}


def _ad_group_ref(ad_group: int) -> dict[str, str]:
    """The ``adGroup`` object every row that selects ``ad_group.id`` carries."""
    return {"resourceName": _ref("adGroups", ad_group), "id": str(ad_group)}


def campaign_row(campaign: int, status: str = ENABLED) -> dict[str, Any]:
    """A Q-G2 / Q-G2b row (``row_campaign``)."""
    return {
        "campaign": {
            **_campaign_ref(campaign),
            "name": f"Campaign {campaign}",
            "status": status,
            "advertisingChannelType": "SEARCH",
        }
    }


def campaign_day_row(campaign: int, day: date) -> dict[str, Any]:
    """A Q-G3 row (``row_campaign_daily``): one campaign on one native day."""
    return {
        "campaign": _campaign_ref(campaign),
        "segments": {"date": day.isoformat()},
        "metrics": {"costMicros": "1250000", "clicks": "3"},
    }


def ad_group_row(ad_group: int, campaign: int, status: str = ENABLED) -> dict[str, Any]:
    """A Q-G4 row (``row_ad_group``)."""
    return {
        "adGroup": {**_ad_group_ref(ad_group), "name": f"Ad group {ad_group}", "status": status},
        "campaign": _campaign_ref(campaign),
    }


def keyword_day_row(criterion: int, ad_group: int, day: date) -> dict[str, Any]:
    """A Q-G5 row (``row_keyword_view``): one keyword on one native day."""
    return {
        "keywordView": {"resourceName": _ref("keywordViews", ad_group, criterion)},
        "adGroupCriterion": {
            "resourceName": _ref("adGroupCriteria", ad_group, criterion),
            "criterionId": str(criterion),
        },
        "adGroup": _ad_group_ref(ad_group),
        "segments": {"date": day.isoformat()},
    }


def search_term_row(term: str, ad_group: int, campaign: int, day: date) -> dict[str, Any]:
    """A Q-G6 row (``row_search_term_view``): one search term on one native day."""
    return {
        "searchTermView": {
            "resourceName": _ref("searchTermViews", campaign, ad_group) + f"~{term}",
            "searchTerm": term,
            "status": "NONE",
        },
        "segments": {"date": day.isoformat()},
        "adGroup": _ad_group_ref(ad_group),
        "campaign": _campaign_ref(campaign),
    }


def keyword_row(
    criterion: int, ad_group: int, campaign: int, status: str = ENABLED
) -> dict[str, Any]:
    """A Q-G7 row (``row_ad_group_criterion``): the current state of one keyword."""
    return {
        "adGroupCriterion": {
            "resourceName": _ref("adGroupCriteria", ad_group, criterion),
            "criterionId": str(criterion),
            "status": status,
            "keyword": {"text": f"keyword {criterion}", "matchType": "PHRASE"},
        },
        "adGroup": _ad_group_ref(ad_group),
        "campaign": _campaign_ref(campaign),
    }


def _value(row: Mapping[str, Any], gaql_field: str) -> Any:
    """Return the raw REST value of one GAQL field of a row."""
    node: Any = row
    for key in rest_path(gaql_field):
        node = node[key]
    return node


def ids_of(rows: Iterable[Mapping[str, Any]], gaql_field: str) -> list[int]:
    """Return one INT64 field of every row, as integers, in row order."""
    return [int(_value(row, gaql_field)) for row in rows]


def field_mask(template: QueryTemplate) -> str:
    """Return the ``fieldMask`` Google sends: the selected fields as camelCase REST paths."""
    return ",".join(".".join(rest_path(name)) for name in template.select)


# --- the fake account -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fact:
    """One row of the fake account and the ids and day the relay filters it by.

    Attributes:
        row: The REST ``GoogleAdsRow`` the relay returns.
        campaign: The row's campaign, also for Q-G5 rows, which do not select ``campaign.id``
            but are still filtered by a ``campaign.id`` range.
        ad_group: The row's ad group, or ``None`` for campaign rows.
        criterion: The row's criterion id, or ``None``.
        day: The native date of a daily row, or ``None`` for an entity row.
        status: The entity's raw status; templates that exclude ``REMOVED`` never return it.
    """

    row: Mapping[str, Any]
    campaign: int
    ad_group: int | None = None
    criterion: int | None = None
    day: date | None = None
    status: str = ENABLED

    def key(self, keyset_field: str) -> int:
        """Return the id a keyset form orders and filters this row by."""
        value = {
            "campaign.id": self.campaign,
            "ad_group.id": self.ad_group,
            "ad_group_criterion.criterion_id": self.criterion,
        }[keyset_field]
        assert value is not None, f"a row without {keyset_field} reached a keyset"
        return value


def campaign_fact(campaign: int, status: str = ENABLED) -> Fact:
    """A campaign as Q-G2 and Q-G2b read it."""
    return Fact(campaign_row(campaign, status), campaign=campaign, status=status)


def ad_group_fact(ad_group: int, campaign: int = 1, status: str = ENABLED) -> Fact:
    """An ad group as Q-G4 reads it."""
    return Fact(ad_group_row(ad_group, campaign, status), campaign, ad_group, status=status)


def campaign_day_fact(campaign: int, day: date) -> Fact:
    """One campaign-day of Q-G3."""
    return Fact(campaign_day_row(campaign, day), campaign=campaign, day=day)


def keyword_day_fact(criterion: int, ad_group: int, campaign: int, day: date) -> Fact:
    """One keyword-day of Q-G5."""
    return Fact(keyword_day_row(criterion, ad_group, day), campaign, ad_group, criterion, day)


def search_term_fact(term: str, ad_group: int, campaign: int, day: date) -> Fact:
    """One search-term-day of Q-G6."""
    return Fact(search_term_row(term, ad_group, campaign, day), campaign, ad_group, day=day)


def keyword_fact(criterion: int, ad_group: int, campaign: int = 1, status: str = ENABLED) -> Fact:
    """One keyword as Q-G7 reads it."""
    row = keyword_row(criterion, ad_group, campaign, status)
    return Fact(row, campaign, ad_group, criterion, status=status)


def _scope_int(scope: Mapping[str, object], key: str) -> int:
    """Read an id-valued key of a parsed scope."""
    value = scope[key]
    assert isinstance(value, int)
    return value


def _scope_day(scope: Mapping[str, object], key: str) -> date:
    """Read a date-valued key of a parsed scope."""
    value = scope[key]
    assert isinstance(value, date)
    return value


def _scope_ids(scope: Mapping[str, object]) -> frozenset[int]:
    """Read the ``IN`` list of a parsed Q-G2b scope."""
    value = scope[IDS]
    assert isinstance(value, tuple)
    return frozenset(_scope_int({IDS: item}, IDS) for item in value)


def _excludes_removed(template: QueryTemplate) -> bool:
    """Whether the template's literal conditions drop ``REMOVED`` entities (Q-G2, Q-G4, Q-G7)."""
    return any(condition.endswith(f"!= '{REMOVED}'") for condition in template.fixed_conditions)


def _in_scope(fact: Fact, parsed: ParsedQuery, *, honour_keyset: bool) -> bool:
    """Whether one row satisfies the date range, id ranges, ad group and keyset of a query."""
    scope = parsed.scope
    if START in scope and (
        fact.day is None or not _scope_day(scope, START) <= fact.day <= _scope_day(scope, END)
    ):
        return False
    if CAMPAIGN_LO in scope and not (
        _scope_int(scope, CAMPAIGN_LO) <= fact.campaign <= _scope_int(scope, CAMPAIGN_HI)
    ):
        return False
    if AD_GROUP_LO in scope and (
        fact.ad_group is None
        or not _scope_int(scope, AD_GROUP_LO) <= fact.ad_group <= _scope_int(scope, AD_GROUP_HI)
    ):
        return False
    if AD_GROUP_ID in scope and fact.ad_group != _scope_int(scope, AD_GROUP_ID):
        return False
    keyset_field = parsed.order_by
    return not (
        honour_keyset
        and keyset_field is not None
        and fact.key(keyset_field) <= _scope_int(scope, LAST_ID)
    )


def _decoys(parsed: ParsedQuery) -> list[dict[str, Any]]:
    """Return a full page of rows inside the partition, for a response capped by policy.

    The rows carry decoy ids or terms, so a test detects any of them surviving the split.
    """
    scope = parsed.scope
    day = _scope_day(scope, START)
    surface = parsed.template.row_surface
    if surface == CAMPAIGN_DAYS:
        if CAMPAIGN_LO in scope:
            # A capped campaign-day: one campaign returning a full page breaks Q-G3's contract.
            return [campaign_day_row(_scope_int(scope, CAMPAIGN_LO), day)] * PAGE_SIZE
        return [campaign_day_row(DECOY_BASE + index, day) for index in range(PAGE_SIZE)]
    if surface == SEARCH_TERMS:
        campaign = _scope_int(scope, CAMPAIGN_LO) if CAMPAIGN_LO in scope else DECOY_BASE
        ad_group = _scope_int(scope, AD_GROUP_LO) if AD_GROUP_LO in scope else DECOY_BASE
        return [
            search_term_row(f"{DECOY_TERM} {index}", ad_group, campaign, day)
            for index in range(PAGE_SIZE)
        ]
    pytest.fail(f"no policy decoys are defined for {surface}")


@dataclass(slots=True)
class Call:
    """One webhook POST the fake received.

    Attributes:
        parsed: The template id and scope ``parse_query`` read from the GAQL.
        query: The GAQL text as sent.
        page_token: The ``page_token`` of the body.
        returned: Rows answered, or ``None`` when a fault answered the call.
    """

    parsed: ParsedQuery
    query: str
    page_token: str | None
    returned: int | None = None

    @property
    def query_id(self) -> str:
        """The template id of the call."""
        return self.parsed.query_id

    @property
    def scope(self) -> dict[str, object]:
        """The call's scope as a plain dict."""
        return dict(self.parsed.scope)


def in_list(call: Call) -> tuple[int, ...]:
    """Return the ``IN`` list of a Q-G2b call, in the order it was rendered."""
    value = call.parsed.scope[IDS]
    assert isinstance(value, tuple)
    return tuple(_scope_int({IDS: item}, IDS) for item in value)


@dataclass
class FakeGoogle:
    """The n8n webhook relaying GAQL to a fake Google Ads account.

    Without options it behaves like the specified relay: rows matching the query's filters, in
    the account's natural order (keyset forms sorted by their id and cut at ``LIMIT``), at
    most ``PAGE_SIZE`` per response and no page token.

    Attributes:
        world: The account's rows per ``row_*`` contract name.
        cap: Answers every query it returns ``True`` for with a full page of decoy rows and no
            token, whatever the account holds: "capped whenever ...".
        honour_keyset: ``False`` models a relay that drops the ``id > {last_id}`` filter.
        first_page_rows: When set, every query is answered in two pages: this many rows with
            ``nextPageToken`` :attr:`token`, then the rest.
        token: The page token the relay issues.
        repeat_token: Whether the second page carries the same token again, as a broken relay
            might.
        fault: Called with each call and its 1-based ordinal; a response it returns is sent
            instead of rows (an HTTP 403, 400 or 503).
        calls: Every request received, in order.
    """

    world: Mapping[str, Sequence[Fact]]
    cap: Callable[[ParsedQuery], bool] | None = None
    honour_keyset: bool = True
    first_page_rows: int | None = None
    token: str = "t1"
    repeat_token: bool = False
    fault: Callable[[Call, int], httpx.Response | None] | None = None
    calls: list[Call] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one webhook POST; the contract-checked transport has validated it already."""
        assert request.headers[SECRET_HEADER] == SECRET
        body = json.loads(request.content)
        assert list(body) == ["customer_id", "query", "page_token"]
        assert body["customer_id"] == CUSTOMER
        call = Call(parse_query(body["query"]), body["query"], body["page_token"])
        self.calls.append(call)
        if self.fault is not None:
            reply = self.fault(call, len(self.calls))
            if reply is not None:
                return reply
        rows, next_token = self._answer(call)
        call.returned = len(rows)
        envelope: dict[str, Any] = {"fieldMask": field_mask(call.parsed.template)}
        if rows:
            # Google omits an empty repeated field: a zero-row page carries no results key.
            envelope["results"] = rows
        envelope["queryResourceConsumption"] = "1"
        if next_token is not None:
            envelope["nextPageToken"] = next_token
        return httpx.Response(200, json=envelope)

    def _answer(self, call: Call) -> tuple[list[Mapping[str, Any]], str | None]:
        """Return the rows of one page and the token to send with it."""
        parsed = call.parsed
        if self.cap is not None and self.cap(parsed):
            assert call.page_token is None
            return list(_decoys(parsed)), None
        rows = [fact.row for fact in self._select(parsed)]
        if self.first_page_rows is None:
            assert call.page_token is None, "the client sent a token the relay never issued"
            return rows[:PAGE_SIZE], None
        if call.page_token is None:
            return rows[: self.first_page_rows], self.token
        assert call.page_token == self.token, "the client sent a token the relay never issued"
        return rows[self.first_page_rows :], self.token if self.repeat_token else None

    def _select(self, parsed: ParsedQuery) -> list[Fact]:
        """Return the account rows the query matches, ordered and limited as GAQL would."""
        template = parsed.template
        drop_removed = _excludes_removed(template)
        wanted = _scope_ids(parsed.scope) if IDS in parsed.scope else None
        matches = [
            fact
            for fact in self.world.get(template.row_surface, ())
            if not (drop_removed and fact.status == REMOVED)
            and (wanted is None or fact.campaign in wanted)
            and _in_scope(fact, parsed, honour_keyset=self.honour_keyset)
        ]
        keyset_field, limit = parsed.order_by, parsed.limit
        if keyset_field is not None and limit is not None:
            matches.sort(key=lambda fact: fact.key(keyset_field))
            matches = matches[:limit]
        return matches

    @property
    def scopes(self) -> list[dict[str, object]]:
        """The scope of every first-page request, in order."""
        return [call.scope for call in self.calls if call.page_token is None]

    @property
    def returned(self) -> list[int | None]:
        """The rows answered by every request, in order."""
        return [call.returned for call in self.calls]

    def calls_for(self, query_id: str) -> list[Call]:
        """The requests of one template, in order."""
        return [call for call in self.calls if call.query_id == query_id]


def answer_with(
    status: int, body: object, *, when: Callable[[Call, int], bool]
) -> Callable[[Call, int], httpx.Response | None]:
    """Return a :attr:`FakeGoogle.fault` answering the matching calls with ``status``."""

    def fault(call: Call, ordinal: int) -> httpx.Response | None:
        """Answer a matching call with the scripted failure, any other with rows."""
        return httpx.Response(status, json=body) if when(call, ordinal) else None

    return fault


class CountingBudget:
    """A :class:`CallBudget` that admits ``allowed`` POST attempts and refuses every later one.

    Attributes:
        allowed: Attempts admitted before the first refusal.
        admitted: Attempts admitted so far.
        refusals: Every ``CallBudgetExhausted`` raised, in order.
    """

    def __init__(self, allowed: int) -> None:
        self.allowed = allowed
        self.admitted = 0
        self.refusals: list[CallBudgetExhausted] = []

    async def acquire(self) -> None:
        """Admit one attempt, or raise ``CallBudgetExhausted`` once the allowance is used."""
        if self.admitted >= self.allowed:
            refusal = CallBudgetExhausted()
            self.refusals.append(refusal)
            raise refusal
        self.admitted += 1


type Connect = Callable[..., AbstractAsyncContextManager[GoogleAdsWebhookClient]]


@pytest.fixture
def connect(
    contract_checked_transport: Callable[
        [str, Callable[[httpx.Request], httpx.Response]], httpx.MockTransport
    ],
) -> Connect:
    """Return ``connect(fake, budget=UNMETERED)``: the production client posting to ``fake``.

    Every request and 2xx response passes the root contract checks of the Google ``search``
    surface; retries do not sleep.
    """

    @asynccontextmanager
    async def _connect(
        fake: FakeGoogle, budget: CallBudget = UNMETERED
    ) -> AsyncIterator[GoogleAdsWebhookClient]:
        """Open the HTTP client on the contract-checked transport and yield the webhook client."""
        transport = contract_checked_transport("google", fake)
        async with httpx.AsyncClient(transport=transport) as http:
            yield GoogleAdsWebhookClient(
                http=http,
                budget=budget,
                webhook_url=SIMULATED_WEBHOOK_URL,
                secret_header=SECRET_HEADER,
                secret=SecretStr(SECRET),
                customer_id=CUSTOMER,
                retry=RETRY,
            )

    return _connect


async def fetch(
    connect: Connect,
    fake: FakeGoogle,
    query_id: str,
    scope: Mapping[str, object] | None = None,
    budget: CallBudget = UNMETERED,
) -> FetchOutcome:
    """Run ``PartitionedFetcher(client).partitioned_fetch(query_id, scope)`` against ``fake``."""
    async with connect(fake, budget) as client:
        return await PartitionedFetcher(client).partitioned_fetch(query_id, scope)


def day_scope(
    start: date, end: date, narrowing: Mapping[str, int] | None = None
) -> dict[str, object]:
    """Return a dated scope ``{start, end}`` plus id-range or keyset narrowing keys."""
    return {START: start, END: end, **(narrowing or {})}


def multi_day(parsed: ParsedQuery) -> bool:
    """Cap policy: every partition whose date range spans more than one day is capped."""
    return _scope_day(parsed.scope, START) < _scope_day(parsed.scope, END)


def two_terms_a_day(days: Iterable[date], ad_group: int = 21, campaign: int = 20) -> list[Fact]:
    """Two search terms on each day, in day order: a small real account behind a cap policy."""
    return [
        search_term_fact(f"term {day.isoformat()} {index}", ad_group, campaign, day)
        for day in days
        for index in range(2)
    ]


def terms_of(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return the search term of every Q-G6 row, in row order."""
    return [_value(row, "search_term_view.search_term") for row in rows]


FOUR_DAYS = (DAY1, DAY2, DAY3, DAY4)
CAMPAIGN_LOW_HALF = {CAMPAIGN_LO: INT64_MIN_ID, CAMPAIGN_HI: FULL_MID}
CAMPAIGN_HIGH_HALF = {CAMPAIGN_LO: FULL_MID + 1, CAMPAIGN_HI: INT64_MAX}
AD_GROUP_LOW_HALF = {AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: FULL_MID}
AD_GROUP_HIGH_HALF = {AD_GROUP_LO: FULL_MID + 1, AD_GROUP_HI: INT64_MAX}


def test_partition_limits_are_the_limits_the_search_contract_records(contract: Any) -> None:
    """The constants the partitioning relies on equal ``search.json``'s recorded request limits."""
    search = contract("google", "search").request

    recorded = tuple(search.limit(name) for name in ("page_size", "keyset_limit", "in_list_max"))

    assert recorded == (PAGE_SIZE, KEYSET_LIMIT, MAX_IDS_PER_IN)
    assert isinstance(CountingBudget(1), CallBudget)


# --- Phase 1: keyset scans (Q-G2, Q-G4) -------------------------------------------------------

KEYSET_SCANS = [
    pytest.param("Q-G2", "campaign.id", "campaign", id="Q-G2 campaigns"),
    pytest.param("Q-G4", "ad_group.id", "ad_group", id="Q-G4 ad groups"),
]


def keyset_world(
    query_id: str, ids: Iterable[int], removed: Iterable[int] = ()
) -> dict[str, list[Fact]]:
    """Non-removed entities ``ids`` and ``REMOVED`` entities of a Q-G2 or Q-G4 scan."""
    if query_id == "Q-G2":
        facts = [campaign_fact(key) for key in ids]
        return {CAMPAIGNS: facts + [campaign_fact(key, REMOVED) for key in removed]}
    facts = [ad_group_fact(key) for key in ids]
    return {AD_GROUPS: facts + [ad_group_fact(key, status=REMOVED) for key in removed]}


@pytest.mark.parametrize(("query_id", "keyset_field", "resource"), KEYSET_SCANS)
async def test_keyset_scan_continues_from_the_largest_id_while_a_page_is_full(
    connect: Connect, query_id: str, keyset_field: str, resource: str
) -> None:
    """AAP 0.3.3 keyset enumeration: 10,050 entities read as pages of 9,999 and 51 rows.

    The first page filters ``id > 0``, the second ``id >`` the largest id of the first; the
    scan ends on the first short page, every id is returned once and in order, and the
    ``REMOVED`` exclusion is part of every page's query.
    """
    ids = list(range(5, 5 + 3 * (KEYSET_LIMIT + 51), 3))
    # Stored out of order, so only the query's ORDER BY sorts the pages; one removed id sits
    # between two live ones and must never be returned.
    fake = FakeGoogle(keyset_world(query_id, reversed(ids), removed=[ids[100] + 1]))

    outcome = await fetch(connect, fake, query_id)

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: ids[KEYSET_LIMIT - 1]}]
    assert fake.returned == [KEYSET_LIMIT, 51]
    assert f"AND {keyset_field} > {INT64_MIN_ID} ORDER BY" in fake.calls[0].query
    assert f"AND {keyset_field} > {ids[KEYSET_LIMIT - 1]} ORDER BY" in fake.calls[1].query
    for call in fake.calls:
        assert f"{resource}.status != '{REMOVED}'" in call.query
        assert call.query.endswith(f"ORDER BY {keyset_field} LIMIT {KEYSET_LIMIT}")
    assert ids_of(outcome.rows, keyset_field) == ids
    assert (outcome.query_id, outcome.completeness, outcome.reason) == (query_id, "complete", None)
    assert (outcome.calls, outcome.partitions_read) == (2, 2)
    assert (outcome.deferred, outcome.interrupted_by) == ((), None)


@pytest.mark.parametrize(("query_id", "keyset_field", "resource"), KEYSET_SCANS)
async def test_a_full_last_keyset_page_costs_one_more_call_that_returns_no_rows(
    connect: Connect, query_id: str, keyset_field: str, resource: str
) -> None:
    """A page of exactly 9,999 rows means "continue": only a shorter page ends the scan."""
    ids = list(range(1, KEYSET_LIMIT + 1))
    fake = FakeGoogle(keyset_world(query_id, ids))

    outcome = await fetch(connect, fake, query_id)

    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: ids[-1]}]
    assert fake.returned == [KEYSET_LIMIT, 0]
    assert ids_of(outcome.rows, keyset_field) == ids
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (2, 2)
    assert all(call.parsed.template.resource == resource for call in fake.calls)


@pytest.mark.parametrize(("query_id", "keyset_field", "resource"), KEYSET_SCANS)
async def test_a_relay_that_does_not_advance_the_keyset_is_a_query_defect(
    connect: Connect, query_id: str, keyset_field: str, resource: str
) -> None:
    """A relay ignoring ``id > {last_id}`` repeats the first page; it fails, never loops."""
    ids = list(range(1, KEYSET_LIMIT + 2))
    fake = FakeGoogle(keyset_world(query_id, ids), honour_keyset=False)

    with pytest.raises(QueryDefect, match="keyset did not advance") as raised:
        await fetch(connect, fake, query_id)

    assert raised.value.query_id == query_id
    assert fake.scopes == [{LAST_ID: INT64_MIN_ID}, {LAST_ID: ids[KEYSET_LIMIT - 1]}]
    assert all(keyset_field in call.query and resource in call.query for call in fake.calls)


# --- Phase 2: Q-G2b by ids --------------------------------------------------------------------


async def test_q_g2b_reads_in_lists_of_at_most_1000_ids_with_no_status_filter(
    connect: Connect,
) -> None:
    """AAP 0.3.3 Q-G2b: 2,500 tracked ids are three ``IN`` lists of 1,000, 1,000 and 500 ids.

    Ids are sorted and de-duplicated before chunking, and the query carries no status filter,
    so a ``REMOVED`` campaign is returned and can be snapshotted as ``ended``.
    """
    ids = list(range(100, 100 + 2 * MAX_IDS_PER_IN + MAX_IDS_PER_IN // 2))
    world = {CAMPAIGNS: [campaign_fact(key, REMOVED if key % 10 == 0 else ENABLED) for key in ids]}
    fake = FakeGoogle(world)
    requested = [*reversed(ids), ids[0], ids[-1]]

    outcome = await fetch(connect, fake, "Q-G2b", {IDS: requested})

    chunks = [in_list(call) for call in fake.calls]
    assert [len(chunk) for chunk in chunks] == [MAX_IDS_PER_IN, MAX_IDS_PER_IN, MAX_IDS_PER_IN // 2]
    assert [key for chunk in chunks for key in chunk] == ids
    assert QUERIES["Q-G2b"].fixed_conditions == ()
    for call in fake.calls:
        where = call.query.split(" WHERE ", 1)[1]
        assert where.startswith("campaign.id IN (")
        assert "status" not in where
    assert sorted(ids_of(outcome.rows, "campaign.id")) == ids
    assert {_value(row, "campaign.status") for row in outcome.rows} == {ENABLED, REMOVED}
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (3, 3)


# --- Phase 3: dated partitions (Q-G3, Q-G5, Q-G6) ---------------------------------------------


async def test_a_dated_response_below_the_page_size_completes_in_one_call(connect: Connect) -> None:
    """9,999 rows over four days are one response below the page size: complete as returned."""
    total = PAGE_SIZE - 1
    per_day = [total // 4 + (1 if index < total % 4 else 0) for index in range(4)]
    world = {
        SEARCH_TERMS: [
            search_term_fact(f"term {index}", 12, 11, day)
            for day, count in zip(FOUR_DAYS, per_day, strict=True)
            for index in range(count)
        ]
    }
    fake = FakeGoogle(world)

    outcome = await fetch(connect, fake, "Q-G6", day_scope(DAY1, DAY4))

    assert fake.scopes == [day_scope(DAY1, DAY4)]
    assert fake.returned == [total]
    assert len(outcome.rows) == total
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (1, 1)


async def test_capped_multi_day_ranges_are_halved_to_single_days_in_ascending_order(
    connect: Connect,
) -> None:
    """AAP 0.3.3 dated queries: a capped range is halved by date down to single days.

    Partitions are read depth first with dates ascending. A capped response's rows are
    discarded when it is split, so only the leaf partitions' rows are returned.
    """
    fake = FakeGoogle({SEARCH_TERMS: two_terms_a_day(FOUR_DAYS)}, cap=multi_day)

    outcome = await fetch(connect, fake, "Q-G6", day_scope(DAY1, DAY4))

    assert fake.scopes == [
        day_scope(DAY1, DAY4),
        day_scope(DAY1, DAY2),
        day_scope(DAY1, DAY1),
        day_scope(DAY2, DAY2),
        day_scope(DAY3, DAY4),
        day_scope(DAY3, DAY3),
        day_scope(DAY4, DAY4),
    ]
    assert fake.returned == [PAGE_SIZE, PAGE_SIZE, 2, 2, PAGE_SIZE, 2, 2]
    assert terms_of(outcome.rows) == terms_of(fact.row for fact in two_terms_a_day(FOUR_DAYS))
    assert not any(term.startswith(DECOY_TERM) for term in terms_of(outcome.rows))
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (7, 7)


async def test_a_capped_single_day_halves_numeric_campaign_ranges_from_the_full_int64_range(
    connect: Connect,
) -> None:
    """AAP 0.3.3: at one day a capped partition splits ``[0, INT64_MAX]`` by ``campaign.id``.

    The split is at ``lo + (hi - lo) // 2``. Ranges are numeric, never lists of known ids:
    campaign 4,242 exists only in the account's metrics (it was removed during the window and
    is on no entity list) and is still read.
    """
    removed_mid_window = 4_242
    low = range(1, PAGE_SIZE // 2 + 1)
    high = range(INT64_MAX - PAGE_SIZE // 2, INT64_MAX + 1)
    assert removed_mid_window in low
    fake = FakeGoogle({CAMPAIGN_DAYS: [campaign_day_fact(key, DAY2) for key in (*low, *high)]})

    outcome = await fetch(connect, fake, "Q-G3", day_scope(DAY2, DAY2))

    assert fake.scopes == [
        day_scope(DAY2, DAY2),
        day_scope(DAY2, DAY2, CAMPAIGN_LOW_HALF),
        day_scope(DAY2, DAY2, CAMPAIGN_HIGH_HALF),
    ]
    assert fake.returned == [PAGE_SIZE, len(low), len(high)]
    assert not any(" IN (" in call.query for call in fake.calls)
    campaigns = ids_of(outcome.rows, "campaign.id")
    assert campaigns == [*low, *high]
    assert removed_mid_window in campaigns
    assert outcome.complete
    assert outcome.calls == 3


async def test_a_capped_campaign_day_halves_numeric_ad_group_ranges_and_keeps_the_campaign(
    connect: Connect,
) -> None:
    """At one day and one campaign a capped partition splits ``[0, INT64_MAX]`` by ad group."""
    campaign, other_campaign = 31, 32
    low_ad_group, high_ad_group = 41, INT64_MAX - 41
    world = {
        SEARCH_TERMS: [
            *(
                search_term_fact(f"low {index}", low_ad_group, campaign, DAY3)
                for index in range(PAGE_SIZE // 2 + 1)
            ),
            *(
                search_term_fact(f"high {index}", high_ad_group, campaign, DAY3)
                for index in range(PAGE_SIZE // 2)
            ),
            *(
                search_term_fact(f"other {index}", low_ad_group, other_campaign, DAY3)
                for index in range(50)
            ),
        ]
    }
    fake = FakeGoogle(world)
    scope = day_scope(DAY3, DAY3, {CAMPAIGN_LO: campaign, CAMPAIGN_HI: campaign})

    outcome = await fetch(connect, fake, "Q-G6", scope)

    assert fake.scopes == [
        scope,
        {**scope, **AD_GROUP_LOW_HALF},
        {**scope, **AD_GROUP_HIGH_HALF},
    ]
    assert fake.returned == [PAGE_SIZE, PAGE_SIZE // 2 + 1, PAGE_SIZE // 2]
    assert set(ids_of(outcome.rows, "campaign.id")) == {campaign}
    ad_groups = ids_of(outcome.rows, "ad_group.id")
    assert (ad_groups.count(low_ad_group), ad_groups.count(high_ad_group)) == (
        PAGE_SIZE // 2 + 1,
        PAGE_SIZE // 2,
    )
    assert outcome.complete
    assert outcome.calls == 3


async def test_q_g5_at_a_capped_day_and_ad_group_switches_to_the_criterion_keyset(
    connect: Connect,
) -> None:
    """AAP 0.3.3: Q-G5 at one (day, ad group) is read by keyset on the criterion id.

    ``ad_group_criterion.criterion_id`` is unique only within its ad group, so the keyset
    pins ``ad_group.id = {ad group}`` and keeps the campaign range; its pages hold 9,999 rows
    and continue from the largest criterion id.
    """
    campaign, ad_group = 51, 52
    criteria = list(range(1_000, 1_000 + 2 * (KEYSET_LIMIT + 501), 2))
    world = {
        KEYWORD_DAYS: [
            *(keyword_day_fact(key, ad_group, campaign, DAY1) for key in reversed(criteria)),
            keyword_day_fact(7, ad_group + 1, campaign, DAY1),
            keyword_day_fact(9, ad_group, campaign, DAY2),
        ]
    }
    fake = FakeGoogle(world)
    pinned = {CAMPAIGN_LO: campaign, CAMPAIGN_HI: campaign}
    scope = day_scope(DAY1, DAY1, {**pinned, AD_GROUP_LO: ad_group, AD_GROUP_HI: ad_group})

    outcome = await fetch(connect, fake, "Q-G5", scope)

    keyset = day_scope(DAY1, DAY1, {**pinned, AD_GROUP_ID: ad_group, LAST_ID: INT64_MIN_ID})
    assert fake.scopes == [scope, keyset, {**keyset, LAST_ID: criteria[KEYSET_LIMIT - 1]}]
    assert fake.returned == [PAGE_SIZE, KEYSET_LIMIT, 501]
    first_keyset_page = fake.calls[1].query
    assert (
        f"ad_group.id = {ad_group} AND ad_group_criterion.criterion_id > {INT64_MIN_ID} "
        in first_keyset_page
    )
    assert first_keyset_page.endswith(
        f"ORDER BY ad_group_criterion.criterion_id LIMIT {KEYSET_LIMIT}"
    )
    assert ids_of(outcome.rows, "ad_group_criterion.criterion_id") == criteria
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (3, 3)


async def test_q_g6_at_a_capped_finest_partition_keeps_its_rows_and_is_incomplete(
    connect: Connect,
) -> None:
    """AAP 0.3.3: Q-G6 rows have no unique sortable id, so a capped (day, ad group) is final.

    Its 10,000 rows are stored as returned and the fetch is ``incomplete`` with the permanent
    reason "result limit reached at finest partition", which names the partition.
    """
    campaign, ad_group = 61, 62
    world = {
        SEARCH_TERMS: [
            search_term_fact(f"term {index}", ad_group, campaign, DAY4)
            for index in range(PAGE_SIZE + 25)
        ]
    }
    fake = FakeGoogle(world)
    scope = day_scope(
        DAY4,
        DAY4,
        {
            CAMPAIGN_LO: campaign,
            CAMPAIGN_HI: campaign,
            AD_GROUP_LO: ad_group,
            AD_GROUP_HI: ad_group,
        },
    )

    outcome = await fetch(connect, fake, "Q-G6", scope)

    assert fake.scopes == [scope]
    assert terms_of(outcome.rows) == [f"term {index}" for index in range(PAGE_SIZE)]
    assert outcome.completeness == "incomplete"
    assert outcome.reason is not None
    assert outcome.reason.startswith(REASON_FINEST_PARTITION)
    # The reason names the partition after the stable prefix that dataset freshness reads.
    assert outcome.reason == f"{REASON_FINEST_PARTITION} ({DAY4.isoformat()}, ad group {ad_group})"
    assert (outcome.deferred, outcome.interrupted_by) == ((), None)
    assert outcome.calls == 1


def coarser_than_a_campaign_day(parsed: ParsedQuery) -> bool:
    """Cap policy: capped while the range spans several days or has no campaign range yet."""
    return multi_day(parsed) or CAMPAIGN_LO not in parsed.scope


async def test_q_g3_partitions_end_at_campaign_days_with_no_ad_group_dimension(
    connect: Connect,
) -> None:
    """AAP 0.3.3: Q-G3 has one row per campaign per day, so it never splits below that.

    The relay caps every partition coarser than a campaign range on one day. The fetch halves
    the dates, then the campaign range, and every partition it sends uses only Q-G3's own
    scope keys: dates and a campaign range, never an ad-group range or keyset.
    """
    campaigns = (3, INT64_MAX - 3)
    world = {
        CAMPAIGN_DAYS: [campaign_day_fact(key, day) for day in (DAY1, DAY2) for key in campaigns]
    }
    fake = FakeGoogle(world, cap=coarser_than_a_campaign_day)

    outcome = await fetch(connect, fake, "Q-G3", day_scope(DAY1, DAY2))

    assert fake.scopes == [
        day_scope(DAY1, DAY2),
        day_scope(DAY1, DAY1),
        day_scope(DAY1, DAY1, CAMPAIGN_LOW_HALF),
        day_scope(DAY1, DAY1, CAMPAIGN_HIGH_HALF),
        day_scope(DAY2, DAY2),
        day_scope(DAY2, DAY2, CAMPAIGN_LOW_HALF),
        day_scope(DAY2, DAY2, CAMPAIGN_HIGH_HALF),
    ]
    q_g3_keys = {START, END, CAMPAIGN_LO, CAMPAIGN_HI}
    assert QUERIES["Q-G3"].allowed_scope_keys == q_g3_keys
    assert all(call.scope.keys() <= q_g3_keys for call in fake.calls)
    read = [(int(_value(row, "campaign.id")), _value(row, "segments.date")) for row in outcome.rows]
    assert read == [(key, day.isoformat()) for day in (DAY1, DAY2) for key in campaigns]
    assert outcome.complete
    assert outcome.calls == 7


async def test_a_capped_q_g3_campaign_day_is_a_query_defect_not_a_finer_partition(
    connect: Connect,
) -> None:
    """A single Q-G3 campaign-day returning a full page breaks its contract: no finer split."""
    campaign = 71
    fake = FakeGoogle({CAMPAIGN_DAYS: []}, cap=lambda parsed: True)
    scope = day_scope(DAY1, DAY1, {CAMPAIGN_LO: campaign, CAMPAIGN_HI: campaign})

    with pytest.raises(QueryDefect, match="campaign-day") as raised:
        await fetch(connect, fake, "Q-G3", scope)

    assert raised.value.query_id == "Q-G3"
    assert fake.scopes == [scope]


# --- Phase 4: Q-G7 keywords -------------------------------------------------------------------


def keyword_pairs(rows: Iterable[Mapping[str, Any]]) -> list[tuple[int, int]]:
    """Return the ``(ad_group.id, criterion_id)`` key of every Q-G7 row, in row order."""
    rows = list(rows)
    return list(
        zip(
            ids_of(rows, "ad_group.id"),
            ids_of(rows, "ad_group_criterion.criterion_id"),
            strict=True,
        )
    )


async def test_q_g7_capped_full_range_is_halved_by_ad_group_id(connect: Connect) -> None:
    """AAP 0.3.3 Q-G7: the keyword list is read by ``ad_group.id`` ranges from ``[0, INT64_MAX]``.

    Criterion ids repeat across ad groups, so keywords are keyed by (ad group, criterion); a
    ``REMOVED`` keyword is excluded by the query itself.
    """
    low_ad_group, high_ad_group = 81, INT64_MAX - 81
    low = [(low_ad_group, key) for key in range(1, PAGE_SIZE // 2 + 2)]
    high = [(high_ad_group, key) for key in range(1, PAGE_SIZE // 2 + 1)]
    world = {
        KEYWORDS: [
            *(keyword_fact(key, ad_group) for ad_group, key in (*low, *high)),
            keyword_fact(PAGE_SIZE, low_ad_group, status=REMOVED),
        ]
    }
    fake = FakeGoogle(world)

    outcome = await fetch(connect, fake, "Q-G7")

    full_range = {AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: INT64_MAX}
    assert fake.scopes == [full_range, AD_GROUP_LOW_HALF, AD_GROUP_HIGH_HALF]
    assert fake.returned == [PAGE_SIZE, len(low), len(high)]
    assert all(f"ad_group_criterion.status != '{REMOVED}'" in call.query for call in fake.calls)
    pairs = keyword_pairs(outcome.rows)
    assert pairs == [*low, *high]
    assert len(set(pairs)) == len(pairs)
    assert outcome.complete
    assert outcome.calls == 3


async def test_q_g7_a_capped_single_ad_group_is_read_by_criterion_keyset(
    connect: Connect,
) -> None:
    """A single capped ad group of 10,500 keywords is read as keyset pages of 9,999 and 501.

    The light ad group reuses the heavy one's criterion ids: every (ad group, criterion) pair
    is still returned exactly once.
    """
    heavy, light = 90, 91
    heavy_criteria = list(range(1, KEYSET_LIMIT + 501 + 1))
    light_criteria = [1, 3, 5]
    world = {
        KEYWORDS: [
            *(keyword_fact(key, heavy) for key in reversed(heavy_criteria)),
            *(keyword_fact(key, light) for key in light_criteria),
        ]
    }
    fake = FakeGoogle(world)
    scope = {AD_GROUP_LO: heavy, AD_GROUP_HI: light}

    outcome = await fetch(connect, fake, "Q-G7", scope)

    keyset = {AD_GROUP_ID: heavy, LAST_ID: INT64_MIN_ID}
    assert fake.scopes == [
        scope,
        {AD_GROUP_LO: heavy, AD_GROUP_HI: heavy},
        keyset,
        {**keyset, LAST_ID: heavy_criteria[KEYSET_LIMIT - 1]},
        {AD_GROUP_LO: light, AD_GROUP_HI: light},
    ]
    assert fake.returned == [PAGE_SIZE, PAGE_SIZE, KEYSET_LIMIT, 501, len(light_criteria)]
    assert fake.calls[2].query.endswith(
        f"ad_group.id = {heavy} AND ad_group_criterion.criterion_id > {INT64_MIN_ID} "
        f"ORDER BY ad_group_criterion.criterion_id LIMIT {KEYSET_LIMIT}"
    )
    pairs = keyword_pairs(outcome.rows)
    assert pairs == [(heavy, key) for key in heavy_criteria] + [
        (light, key) for key in light_criteria
    ]
    assert len(set(pairs)) == len(pairs)
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (5, 5)


# --- Phase 5: page tokens, call budget and errors ---------------------------------------------


async def test_a_returned_page_token_is_sent_back_at_once_with_the_same_query(
    connect: Connect,
) -> None:
    """AAP 0.3.3: a response carrying ``nextPageToken`` is followed at once (tokens expire)."""
    fake = FakeGoogle({CAMPAIGNS: [campaign_fact(key) for key in (4, 5, 6)]}, first_page_rows=2)

    outcome = await fetch(connect, fake, "Q-G2")

    assert [call.page_token for call in fake.calls] == [None, "t1"]
    assert fake.calls[1].query == fake.calls[0].query
    assert fake.returned == [2, 1]
    assert ids_of(outcome.rows, "campaign.id") == [4, 5, 6]
    assert outcome.complete
    assert (outcome.calls, outcome.partitions_read) == (2, 1)


async def test_a_repeated_page_token_is_a_query_defect(connect: Connect) -> None:
    """A relay that hands out the same token twice fails the query instead of looping."""
    fake = FakeGoogle(
        {CAMPAIGNS: [campaign_fact(key) for key in (4, 5, 6, 7)]},
        first_page_rows=1,
        repeat_token=True,
    )

    with pytest.raises(QueryDefect, match="page token repeated") as raised:
        await fetch(connect, fake, "Q-G2")

    assert raised.value.query_id == "Q-G2"
    assert [call.page_token for call in fake.calls] == [None, "t1"]


async def test_budget_exhaustion_on_the_third_keyset_call_defers_the_rest_and_keeps_rows(
    connect: Connect,
) -> None:
    """AAP 0.3.3 call budget: a refused call ends the fetch ``incomplete`` with work deferred.

    Rows of the two admitted calls are kept, the unread keyset page is deferred as a scope
    the same fetcher resumes, and ``calls`` counts only the POSTs actually made.
    """
    ids = list(range(1, 2 * KEYSET_LIMIT + 6))
    fake = FakeGoogle({CAMPAIGNS: [campaign_fact(key) for key in ids]})
    budget = CountingBudget(allowed=2)

    outcome = await fetch(connect, fake, "Q-G2", budget=budget)

    assert outcome.completeness == "incomplete"
    assert outcome.reason is not None
    assert outcome.reason.startswith("call budget exhausted")
    assert outcome.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert [dict(scope) for scope in outcome.deferred] == [{LAST_ID: ids[2 * KEYSET_LIMIT - 1]}]
    assert len(budget.refusals) == 1
    assert outcome.interrupted_by is budget.refusals[0]
    assert ids_of(outcome.rows, "campaign.id") == ids[: 2 * KEYSET_LIMIT]
    assert outcome.calls == len(fake.calls) == budget.admitted == 2

    async with connect(fake) as client:
        resumed = await PartitionedFetcher(client).partitioned_fetch("Q-G2", outcome.deferred[0])

    assert ids_of(resumed.rows, "campaign.id") == ids[2 * KEYSET_LIMIT :]
    assert resumed.complete


async def test_budget_exhaustion_mid_dated_partition_defers_unread_partitions_in_order(
    connect: Connect,
) -> None:
    """A refusal inside a date descent keeps the leaves read and defers the rest, in order."""
    fake = FakeGoogle({SEARCH_TERMS: two_terms_a_day(FOUR_DAYS)}, cap=multi_day)
    budget = CountingBudget(allowed=3)

    outcome = await fetch(connect, fake, "Q-G6", day_scope(DAY1, DAY4), budget)

    assert fake.scopes == [day_scope(DAY1, DAY4), day_scope(DAY1, DAY2), day_scope(DAY1, DAY1)]
    assert [dict(scope) for scope in outcome.deferred] == [
        day_scope(DAY2, DAY2),
        day_scope(DAY3, DAY4),
    ]
    assert terms_of(outcome.rows) == terms_of(fact.row for fact in two_terms_a_day([DAY1]))
    assert outcome.completeness == "incomplete"
    assert outcome.reason == f"{REASON_BUDGET}; 2 partitions deferred"
    assert outcome.interrupted_by is budget.refusals[0]
    assert (outcome.calls, outcome.partitions_read) == (3, 3)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        pytest.param(403, PERMISSION_DENIED, AccessDenied, id="403 access denied"),
        pytest.param(400, INVALID_ARGUMENT, QueryDefect, id="400 query defect"),
    ],
)
async def test_access_denied_and_query_defects_propagate_unchanged_mid_fetch(
    connect: Connect, status: int, body: object, error: type[Exception]
) -> None:
    """AAP 0.3.3: denial and defects are never retried and never become an incomplete outcome.

    ``sync.mode`` decides their consequence (simulated data, or a failed job), so the fetch
    raises them at the third partition instead of returning the rows read so far.
    """
    fault = answer_with(status, body, when=lambda call, ordinal: ordinal == 3)
    fake = FakeGoogle({SEARCH_TERMS: two_terms_a_day(FOUR_DAYS)}, cap=multi_day, fault=fault)

    with pytest.raises(error) as raised:
        await fetch(connect, fake, "Q-G6", day_scope(DAY1, DAY4))

    assert isinstance(raised.value, AccessDenied | QueryDefect)
    assert raised.value.http_status == status
    assert len(fake.calls) == 3


# --- Phase 6: safe absence (AAP 0.3.3) --------------------------------------------------------


def tracked_campaign_world() -> dict[str, list[Fact]]:
    """Campaigns 1 (enabled), 2 (paused), 9 (removed) and 30 (untracked); 10 no longer exists."""
    return {
        CAMPAIGNS: [
            campaign_fact(1),
            campaign_fact(2, PAUSED),
            campaign_fact(9, REMOVED),
            campaign_fact(30),
        ]
    }


async def test_enumerate_campaigns_reports_removed_and_missing_after_complete_reads(
    connect: Connect,
) -> None:
    """A complete Q-G2 scan and a complete Q-G2b re-read decide removed and missing campaigns.

    Q-G2 returns 1, 2 and 30; Q-G2b re-reads the tracked 9 and 10 with no status filter. 9
    comes back ``REMOVED`` (snapshotted as ended); 10 is returned by neither (``missing``).
    """
    fake = FakeGoogle(tracked_campaign_world())

    async with connect(fake) as client:
        enumeration = await enumerate_campaigns(client, tracked_ids={1, 2, 9, 10})

    assert [(call.query_id, call.scope) for call in fake.calls] == [
        ("Q-G2", {LAST_ID: INT64_MIN_ID}),
        ("Q-G2b", {IDS: (9, 10)}),
    ]
    assert enumeration.complete is True
    assert (enumeration.reason, enumeration.interrupted_by) == (None, None)
    assert ids_of(enumeration.present, "campaign.id") == [1, 2, 30]
    assert ids_of(enumeration.removed, "campaign.id") == [9]
    assert _value(enumeration.removed[0], "campaign.status") == REMOVED
    assert enumeration.missing_ids == frozenset({10})
    assert enumeration.calls == 2


async def test_enumerate_campaigns_infers_nothing_after_a_budget_interrupted_scan(
    connect: Connect,
) -> None:
    """A deferred keyset page leaves the scan incomplete: nothing is removed or missing.

    Campaign 1 is on the first page, the last campaign on the deferred page, and 1,000,000
    exists nowhere; none of them is inferred absent, Q-G2b never runs, and
    ``google.entities`` stays incomplete.
    """
    ids = list(range(1, KEYSET_LIMIT + 4))
    fake = FakeGoogle({CAMPAIGNS: [campaign_fact(key) for key in ids]})
    budget = CountingBudget(allowed=1)

    async with connect(fake, budget) as client:
        enumeration = await enumerate_campaigns(client, tracked_ids={1, ids[-1], 1_000_000})

    assert [call.query_id for call in fake.calls] == ["Q-G2"]
    assert enumeration.complete is False
    assert enumeration.removed == ()
    assert enumeration.missing_ids == frozenset()
    assert enumeration.reason is not None
    assert enumeration.reason.startswith(REASON_BUDGET)
    assert isinstance(enumeration.interrupted_by, CallBudgetExhausted)
    assert ids_of(enumeration.present, "campaign.id") == ids[:KEYSET_LIMIT]
    assert enumeration.calls == 1


async def test_enumerate_campaigns_infers_nothing_when_a_q_g2b_chunk_keeps_failing(
    connect: Connect,
) -> None:
    """A Q-G2b chunk answering 503 on every retry leaves no removal or disappearance inferred."""
    fault = answer_with(503, UNAVAILABLE, when=lambda call, ordinal: call.query_id == "Q-G2b")
    fake = FakeGoogle(tracked_campaign_world(), fault=fault)

    async with connect(fake) as client:
        enumeration = await enumerate_campaigns(client, tracked_ids={1, 2, 9, 10})

    assert len(fake.calls_for("Q-G2b")) == RETRY.attempts
    assert enumeration.complete is False
    assert enumeration.removed == ()
    assert enumeration.missing_ids == frozenset()
    assert enumeration.reason is not None
    assert enumeration.reason.startswith(REASON_INTERRUPTED)
    assert isinstance(enumeration.interrupted_by, TransientError)
    assert not isinstance(enumeration.interrupted_by, CallBudgetExhausted)
    assert enumeration.interrupted_by.http_status == 503
    assert ids_of(enumeration.present, "campaign.id") == [1, 2, 30]
    assert enumeration.calls == 1 + RETRY.attempts


async def test_enumerate_entities_reports_absent_ad_groups_only_after_a_complete_scan(
    connect: Connect,
) -> None:
    """Q-G4 absent keys come only from a complete scan; an interrupted one reports none."""
    world = {
        AD_GROUPS: [*(ad_group_fact(key) for key in (5, 6, 8)), ad_group_fact(7, status=REMOVED)]
    }

    async with connect(FakeGoogle(world)) as client:
        complete = await enumerate_entities(client, "Q-G4", {5, 7, 8})

    assert complete.complete is True
    assert ids_of(complete.rows, "ad_group.id") == [5, 6, 8]
    assert complete.absent_keys == frozenset({7})
    assert (complete.reason, complete.interrupted_by) == (None, None)

    ids = list(range(1, KEYSET_LIMIT + 3))
    interrupted_fake = FakeGoogle({AD_GROUPS: [ad_group_fact(key) for key in ids]})
    async with connect(interrupted_fake, CountingBudget(allowed=1)) as client:
        interrupted = await enumerate_entities(client, "Q-G4", {1, ids[-1], 1_000_000})

    assert interrupted_fake.scopes == [{LAST_ID: INT64_MIN_ID}]
    assert interrupted.complete is False
    assert interrupted.absent_keys == frozenset()
    assert interrupted.reason is not None
    assert interrupted.reason.startswith(REASON_BUDGET)
    assert isinstance(interrupted.interrupted_by, CallBudgetExhausted)
    assert ids_of(interrupted.rows, "ad_group.id") == ids[:KEYSET_LIMIT]


async def test_enumerate_entities_keys_keywords_by_ad_group_and_criterion(
    connect: Connect,
) -> None:
    """Q-G7 absent keys are (ad group, criterion) pairs: criterion ids repeat across groups."""
    world = {
        KEYWORDS: [
            keyword_fact(1, 30),
            keyword_fact(1, 31),
            keyword_fact(3, 31),
            keyword_fact(9, 31, status=REMOVED),
        ]
    }

    async with connect(FakeGoogle(world)) as client:
        enumeration = await enumerate_entities(client, "Q-G7", {(30, 1), (31, 1), (31, 9), (30, 3)})

    assert enumeration.complete is True
    assert keyword_pairs(enumeration.rows) == [(30, 1), (31, 1), (31, 3)]
    assert enumeration.absent_keys == frozenset({(31, 9), (30, 3)})
