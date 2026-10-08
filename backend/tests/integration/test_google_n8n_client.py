"""Integration tests of the Google Ads connector: the n8n webhook, partitioning and entity sync.

AAP 0.11.2 row ``integration/test_google_n8n_client.py`` proves six guarantees of the access path
AAP 0.3.3 prescribes. Each call is one ``POST {N8N_GOOGLE_ADS_WEBHOOK_URL}``, carrying exactly
``{customer_id, query, page_token}`` and the shared-secret header, for one GAQL query run as
``googleAds:search`` v25:

* **Request**: the exact body and headers, a returned page token followed at once, n8n's
  one-element array unwrapped, and errors relayed inside HTTP 200 classified;
* **Keyset enumeration**: Q-G2 and Q-G4 keyset scans, a deferred page leaving
  ``google.entities`` incomplete, and Q-G2b ``IN`` chunks of 1,000 ids;
* **Dated partitions**: date halving, then numeric ``campaign.id`` and ``ad_group.id`` ranges, the
  Q-G5 criterion keyset and the incomplete Q-G6 finest partition;
* **Q-G7**: ``ad_group.id`` range halving, then a criterion keyset in a 15,000-keyword ad group;
* **Budget**: exhaustion mid-scan or mid-partition is ``incomplete``, and every POST counts;
* **Entity coverage**: removals and disappearances only from complete reads, keyword rows and
  quality-score snapshots for keywords with no traffic, and one snapshot per conversion action.

They also prove that the Google fixtures trace to their contracts (documented samples and
redacted live captures parse; AAP 0.11.2) and that logs never carry the secret or the webhook
path (AAP 0.3.2).

Every request and every success response goes through ``contract_checked_transport("google",
...)``, which validates them against ``tests/contracts/google/search.json`` and each served row
against its ``row_*.json`` contract (AAP 0.11.2: no fixture carries a field its contract does not
define). Bodies that are not success envelopes cannot pass that schema: Google errors relayed
inside HTTP 200, n8n's one-element array and unrecognised bodies. Those tests serve through
``recording_transport`` and validate each request against the same contract explicitly.

:class:`_FakeGoogleAds` plays n8n in front of an in-memory Google Ads account. It decodes each
query with ``queries.parse_query``, the exact inverse of ``QueryTemplate.render``. It honours
date ranges, numeric id ranges, the ``REMOVED`` exclusions the templates declare, ``IN`` lists and
``ORDER BY ... LIMIT 9999`` keysets. It truncates every answer to 10,000 rows with no
``nextPageToken``, as the specified n8n response does. Rows carry camelCase keys and INT64 values
as strings.

Tests that write entity state run the production ``EntitySyncJob`` on the PostgreSQL 18 test
database through the live-transport seam of ``SourceModeController``. Retries never sleep, and
no request leaves the process (the conftest network guard refuses any real transport). The
product never writes to Google Ads and holds no Google OAuth client id, client secret, refresh
token or developer token; these tests assert that absence and never exercise such a path.
"""

import base64
import json
import re
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any, Final, Protocol

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.google_n8n.client import (
    UNMETERED,
    CallBudget,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.google_n8n.parser import PARSERS, parse_campaign
from ads_engine.connectors.google_n8n.partition import (
    REASON_BUDGET,
    REASON_FINEST_PARTITION,
    REASON_INTERRUPTED,
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
    query_id_for,
    rest_path,
)
from ads_engine.connectors.http import RetryPolicy, build_client
from ads_engine.db.models import (
    AdGroup,
    Campaign,
    ConversionAction,
    DatasetFreshness,
    Discrepancy,
    EntitySnapshot,
    Keyword,
    SourceConnection,
)
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.simulation.payloads import (
    Contract,
    load_contract,
    validate_capture,
    validate_documented_sample,
    validate_google_rows,
    validate_request,
    validate_response,
)
from ads_engine.sync import freshness
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.jobs import EntitySyncJob, JobContext, JobResult
from ads_engine.sync.mode import SourceModeController

pytestmark = [pytest.mark.anyio, pytest.mark.contract]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

CUSTOMER_ID: Final = "1234567890"
"""The fake ``GOOGLE_ADS_CUSTOMER_ID`` that conftest's ``live_credentials("google")`` sets."""

DAY1, DAY2, DAY3, DAY4 = (date(2026, 9, day) for day in range(1, 5))

T0: Final = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
"""The first instant of the entity-sync tests' clock."""

FULL_MID: Final = INT64_MIN_ID + (INT64_MAX - INT64_MIN_ID) // 2
"""The first split point of the full id range ``[0, INT64_MAX]``."""

HALVINGS: Final = (INT64_MAX - INT64_MIN_ID).bit_length()
"""Halvings that narrow ``[0, INT64_MAX]`` (2**63 ids) to a single id: 63."""

SEARCH_CONTRACT: Final = "search"
ROW_CONTRACTS: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(template.row_surface for template in QUERIES.values())
)
"""Every ``row_*`` contract a Google template's rows follow, in template order."""

N8N_WORKFLOW_ERROR: Final[Mapping[str, str]] = {"message": "Error in workflow"}
"""n8n's observed reply when its workflow fails: HTTP 500 with no Google error body."""

GOOGLE_FAILURE_TYPE: Final = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"

# The webhook body is exactly these three keys, in this order (AAP 0.3.3 "Request body").
BODY_KEYS: Final = ["customer_id", "query", "page_token"]
_CUSTOMER_ID_RE: Final = re.compile(r"[0-9]{10}")

# Headers a direct Google Ads API call would need; the webhook path must never send them.
GOOGLE_API_HEADERS: Final = ("Authorization", "developer-token", "login-customer-id")

# Each template's rows come from one in-memory table of the fake account.
_TABLE_OF: Final[Mapping[str, str]] = {
    "Q-G1": "customer",
    "Q-G2": "campaigns",
    "Q-G2b": "campaigns",
    "Q-G3": "campaign_days",
    "Q-G4": "ad_groups",
    "Q-G5": "keyword_days",
    "Q-G6": "search_terms",
    "Q-G7": "criteria",
    "Q-G8": "actions",
}
_DATED_TABLES: Final = frozenset({"campaign_days", "keyword_days", "search_terms"})
_ENCODED_CACHE_SIZE: Final = 8


async def _no_sleep(seconds: float) -> None:
    """Stand in for ``asyncio.sleep`` between retries, so no test ever waits."""
    del seconds


RETRY: Final = RetryPolicy(sleep=_no_sleep)
"""The plan's retry policy (3 attempts) with an instant sleep."""

type Handler = Callable[[httpx.Request], httpx.Response]


class LogCapture(Protocol):
    """The ``captured_logs`` fixture: the product's JSON log lines, redacted as in production."""

    @property
    def text(self) -> str:
        """Everything written so far, raw."""
        ...

    def configure(self, *, settings: Settings | None = None, level: str = "INFO") -> None:
        """Reconfigure logging onto the buffer; ``settings`` name the secrets to redact."""
        ...

    def __call__(self) -> list[dict[str, Any]]:
        """Every line written so far, parsed."""
        ...


type CheckedTransport = Callable[[Source, Handler], httpx.MockTransport]
type RecordingFactory = Callable[[Handler], httpx.MockTransport]
type Sessions = async_sessionmaker[AsyncSession]
type JsonRow = Mapping[str, Any]

# ---------------------------------------------------------------------------------------------
# Rows in the REST GoogleAdsRow shapes of tests/contracts/google/row_*.json
# ---------------------------------------------------------------------------------------------


def _resource(collection: str, key: object) -> str:
    """Return the resource name ``customers/{customer}/{collection}/{key}``."""
    return f"customers/{CUSTOMER_ID}/{collection}/{key}"


def _ref(collection: str, entity_id: int) -> dict[str, str]:
    """Return an attributed resource object ``{resourceName, id}``, the id as an INT64 string."""
    return {"resourceName": _resource(collection, entity_id), "id": str(entity_id)}


def customer_row() -> dict[str, Any]:
    """A Q-G1 row (``row_customer``): the account's currency and time zone."""
    return {
        "customer": {
            "resourceName": f"customers/{CUSTOMER_ID}",
            "id": CUSTOMER_ID,
            "descriptiveName": "Contract Test Account",
            "currencyCode": "USD",
            "timeZone": "America/New_York",
        }
    }


def campaign_row(campaign_id: int, *, status: str, budget: bool) -> dict[str, Any]:
    """A Q-G2 / Q-G2b row (``row_campaign``), optionally with an own daily budget."""
    row: dict[str, Any] = {
        "campaign": {
            "resourceName": _resource("campaigns", campaign_id),
            "id": str(campaign_id),
            "name": f"Campaign {campaign_id}",
            "status": status,
            "advertisingChannelType": "SEARCH",
        }
    }
    if budget:
        budget_id = campaign_id + 500_000
        row["campaignBudget"] = {
            "resourceName": _resource("campaignBudgets", budget_id),
            "id": str(budget_id),
            "amountMicros": "50000000",
            "period": "DAILY",
        }
    return row


def campaign_day_row(day: date, campaign_id: int) -> dict[str, Any]:
    """A Q-G3 row (``row_campaign_daily``) with spend, clicks and impressions."""
    return {
        "campaign": _ref("campaigns", campaign_id),
        "segments": {"date": day.isoformat()},
        "metrics": {"costMicros": "1250000", "clicks": "3", "impressions": "60"},
    }


def ad_group_row(ad_group_id: int, campaign_id: int, *, status: str) -> dict[str, Any]:
    """A Q-G4 row (``row_ad_group``)."""
    return {
        "adGroup": {
            "resourceName": _resource("adGroups", ad_group_id),
            "id": str(ad_group_id),
            "name": f"Ad group {ad_group_id}",
            "status": status,
        },
        "campaign": _ref("campaigns", campaign_id),
    }


def keyword_day_row(day: date, ad_group_id: int, criterion_id: int) -> dict[str, Any]:
    """A Q-G5 row (``row_keyword_view``): one keyword on one native day."""
    key = f"{ad_group_id}~{criterion_id}"
    return {
        "keywordView": {"resourceName": _resource("keywordViews", key)},
        "adGroupCriterion": {
            "resourceName": _resource("adGroupCriteria", key),
            "criterionId": str(criterion_id),
        },
        "adGroup": _ref("adGroups", ad_group_id),
        "segments": {"date": day.isoformat()},
    }


def search_term_row(day: date, campaign_id: int, ad_group_id: int, term: str) -> dict[str, Any]:
    """A Q-G6 row (``row_search_term_view``); the resource name embeds the URL-base64 term."""
    encoded = base64.urlsafe_b64encode(term.encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "searchTermView": {
            "resourceName": _resource("searchTermViews", f"{campaign_id}~{ad_group_id}~{encoded}"),
            "searchTerm": term,
            "status": "NONE",
        },
        "segments": {"date": day.isoformat()},
        "adGroup": _ref("adGroups", ad_group_id),
        "campaign": _ref("campaigns", campaign_id),
    }


def criterion_row(
    ad_group_id: int,
    campaign_id: int,
    criterion_id: int,
    *,
    status: str,
    quality_score: int | None,
) -> dict[str, Any]:
    """A Q-G7 row (``row_ad_group_criterion``); ``qualityInfo`` is omitted when unpopulated."""
    criterion: dict[str, Any] = {
        "resourceName": _resource("adGroupCriteria", f"{ad_group_id}~{criterion_id}"),
        "criterionId": str(criterion_id),
        "status": status,
        "keyword": {"text": f"keyword {criterion_id}", "matchType": "PHRASE"},
    }
    if quality_score is not None:
        criterion["qualityInfo"] = {"qualityScore": quality_score}
    return {
        "adGroupCriterion": criterion,
        "adGroup": _ref("adGroups", ad_group_id),
        "campaign": _ref("campaigns", campaign_id),
    }


def conversion_action_row(
    action_id: int,
    *,
    status: str,
    include: bool | None,
    counting_type: str,
    attribution_model: str | None,
) -> dict[str, Any]:
    """A Q-G8 row (``row_conversion_action``).

    ``include_in_conversions_metric`` has explicit presence, so ``None`` omits the key (unset);
    an action type without attribution settings omits ``attributionModelSettings``.
    """
    action: dict[str, Any] = {
        "resourceName": _resource("conversionActions", action_id),
        "id": str(action_id),
        "name": f"Conversion action {action_id}",
        "status": status,
        "countingType": counting_type,
    }
    if include is not None:
        action["includeInConversionsMetric"] = include
    if attribution_model is not None:
        action["attributionModelSettings"] = {"attributionModel": attribution_model}
    return {"conversionAction": action}


def field_mask(template: QueryTemplate) -> str:
    """The envelope's ``fieldMask``: the template's selected fields as camelCase REST paths."""
    return ",".join(".".join(rest_path(name)) for name in template.select)


# ---------------------------------------------------------------------------------------------
# The fake n8n webhook in front of an in-memory Google Ads account
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Rec:
    """One stored row and the values the fake filters it by.

    Attributes:
        row: The REST ``GoogleAdsRow`` served as is.
        key: The keyset id: ``campaign.id`` (Q-G2, Q-G2b), ``ad_group.id`` (Q-G4) or
            ``ad_group_criterion.criterion_id`` (Q-G5, Q-G7).
        day: The native date of a daily row.
        campaign: The row's ``campaign.id``, also for Q-G5 rows, which do not select it.
        ad_group: The row's ``ad_group.id``.
        removed: Whether the entity's status is ``REMOVED``.
    """

    row: JsonRow
    key: int | None = None
    day: date | None = None
    campaign: int | None = None
    ad_group: int | None = None
    removed: bool = False


@dataclass(frozen=True, slots=True)
class _Received:
    """One webhook call as the fake received it.

    Attributes:
        number: The call's ordinal, the first being 1.
        request: The request.
        body: Its decoded JSON body.
        parsed: The GAQL template and scope ``parse_query`` recovered from it.
    """

    number: int
    request: httpx.Request
    body: Mapping[str, Any]
    parsed: ParsedQuery

    @property
    def query_id(self) -> str:
        """The template id, such as ``Q-G2``."""
        return self.parsed.query_id

    @property
    def scope(self) -> dict[str, object]:
        """The scope the query was rendered with, as a plain dict."""
        return dict(self.parsed.scope)

    @property
    def text(self) -> str:
        """The GAQL text."""
        return str(self.body["query"])

    @property
    def where(self) -> str:
        """The GAQL text after ``WHERE``; empty without a WHERE clause."""
        return self.text.partition(" WHERE ")[2]

    @property
    def page_token(self) -> str | None:
        """The ``page_token`` sent: ``None`` on a first page."""
        token = self.body["page_token"]
        return token if isinstance(token, str) else None

    @property
    def last_id(self) -> int:
        """The keyset position ``last_id`` of a keyset call."""
        value = self.parsed.scope[LAST_ID]
        assert isinstance(value, int), "a keyset call carries an int last_id"
        return value

    @property
    def ids(self) -> tuple[int, ...]:
        """The ascending ``IN`` id list of a Q-G2b call."""
        value = self.parsed.scope[IDS]
        assert isinstance(value, tuple), "a Q-G2b scope carries its ids as a tuple"
        return tuple(item for item in value if isinstance(item, int))


type ResponseHook = Callable[[_Received], httpx.Response | None]


def _scope_int(scope: Mapping[str, object], key: str) -> int | None:
    """Return an id-valued scope entry, or ``None`` when the scope has none."""
    value = scope.get(key)
    if value is None:
        return None
    assert isinstance(value, int), f"scope {key} must be an int"
    return value


def _scope_date(scope: Mapping[str, object], key: str) -> date | None:
    """Return a date-valued scope entry, or ``None`` when the scope has none."""
    value = scope.get(key)
    if value is None:
        return None
    assert isinstance(value, date), f"scope {key} must be a date"
    return value


def _scope_range(scope: Mapping[str, object], lo_key: str, hi_key: str) -> tuple[int, int] | None:
    """Return an inclusive id range of a scope, or ``None`` when the scope has none."""
    lo, hi = _scope_int(scope, lo_key), _scope_int(scope, hi_key)
    if lo is None or hi is None:
        assert lo is None, f"{lo_key} comes with {hi_key}"
        assert hi is None, f"{hi_key} comes with {lo_key}"
        return None
    return lo, hi


def _within(value: int | None, bounds: tuple[int, int]) -> bool:
    """Whether ``value`` lies in the inclusive ``bounds``; a row without the value never does."""
    return value is not None and bounds[0] <= value <= bounds[1]


class _FakeGoogleAds:
    """The n8n webhook relaying ``googleAds:search`` v25 for one in-memory Google Ads account.

    :meth:`handler` is the ``httpx.MockTransport`` handler. Each call must carry exactly the
    three body keys and a 10-digit customer id; its GAQL is decoded with ``parse_query`` and
    answered from the account's tables:

    * the ``REMOVED`` exclusion applies exactly where the template declares one in its fixed
      conditions (Q-G2, Q-G4, Q-G7), so metrics queries and Q-G2b return removed entities;
    * date ranges, numeric campaign and ad-group ranges, ``ad_group.id = X`` and ``IN`` lists
      filter the rows; a keyset form orders by its keyset id, keeps ids above ``last_id`` and
      applies ``LIMIT 9999``;
    * every answer holds at most 10,000 rows and no ``nextPageToken``, exactly the specified n8n
      response, so a larger match comes back capped and silently truncated;
    * an empty answer omits ``results`` and keeps ``fieldMask``, as Google does.

    Knobs: :attr:`tokens` makes a given call return a page of a chosen size with a
    ``nextPageToken`` (a continuation resumes from it); :attr:`wrap_in_array` answers with n8n's
    one-element array; :attr:`hook` may answer or raise instead of the data. Every call is
    recorded in :attr:`received`, and the rows each data answer served in :attr:`served`.
    Identical answers are serialised once, so a 63-level id-range descent stays fast.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[_Rec]] = {name: [] for name in set(_TABLE_OF.values())}
        self._by_day: dict[str, dict[date, list[_Rec]]] = {name: {} for name in _DATED_TABLES}
        self._continuations: dict[str, tuple[str, int]] = {}
        self._encoded: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
        self.received: list[_Received] = []
        self.served: dict[int, int] = {}
        self.tokens: dict[int, tuple[str, int]] = {}
        self.wrap_in_array = False
        self.hook: ResponseHook | None = None

    # -- the account --------------------------------------------------------------------------

    def _add(self, table: str, records: Iterable[_Rec]) -> None:
        """Append records to a table, indexing daily rows by their native date."""
        added = list(records)
        self._tables[table].extend(added)
        if table in _DATED_TABLES:
            index = self._by_day[table]
            for rec in added:
                assert rec.day is not None, "a daily row needs its date"
                index.setdefault(rec.day, []).append(rec)

    def add_customer(self) -> None:
        """Add the account's Q-G1 row."""
        self._add("customer", [_Rec(customer_row())])

    def add_campaigns(
        self, ids: Iterable[int], *, status: str = "ENABLED", budgets: bool = False
    ) -> None:
        """Add campaigns, each with an own daily budget when ``budgets``."""
        self._add(
            "campaigns",
            (
                _Rec(
                    campaign_row(campaign_id, status=status, budget=budgets),
                    key=campaign_id,
                    campaign=campaign_id,
                    removed=status == "REMOVED",
                )
                for campaign_id in ids
            ),
        )

    def add_ad_groups(
        self, ids: Iterable[int], *, campaign_id: int, status: str = "ENABLED"
    ) -> None:
        """Add ad groups of one campaign."""
        self._add(
            "ad_groups",
            (
                _Rec(
                    ad_group_row(ad_group_id, campaign_id, status=status),
                    key=ad_group_id,
                    campaign=campaign_id,
                    ad_group=ad_group_id,
                    removed=status == "REMOVED",
                )
                for ad_group_id in ids
            ),
        )

    def add_criteria(
        self,
        ad_group_id: int,
        campaign_id: int,
        criterion_ids: Iterable[int],
        *,
        status: str = "ENABLED",
        quality_score: int | None = 7,
    ) -> None:
        """Add keyword criteria of one ad group (Q-G7's current keyword list)."""
        self._add(
            "criteria",
            (
                _Rec(
                    criterion_row(
                        ad_group_id,
                        campaign_id,
                        criterion_id,
                        status=status,
                        quality_score=quality_score,
                    ),
                    key=criterion_id,
                    campaign=campaign_id,
                    ad_group=ad_group_id,
                    removed=status == "REMOVED",
                )
                for criterion_id in criterion_ids
            ),
        )

    def add_campaign_days(self, day: date, campaign_ids: Iterable[int]) -> None:
        """Add one Q-G3 row per campaign on ``day``."""
        self._add(
            "campaign_days",
            (
                _Rec(campaign_day_row(day, campaign_id), day=day, campaign=campaign_id)
                for campaign_id in campaign_ids
            ),
        )

    def add_keyword_days(
        self, day: date, campaign_id: int, ad_group_id: int, criterion_ids: Iterable[int]
    ) -> None:
        """Add one Q-G5 row per keyword of one ad group on ``day``."""
        self._add(
            "keyword_days",
            (
                _Rec(
                    keyword_day_row(day, ad_group_id, criterion_id),
                    key=criterion_id,
                    day=day,
                    campaign=campaign_id,
                    ad_group=ad_group_id,
                )
                for criterion_id in criterion_ids
            ),
        )

    def add_search_terms(self, day: date, campaign_id: int, ad_group_id: int, count: int) -> None:
        """Add ``count`` distinct Q-G6 search terms of one ad group on ``day``."""
        self._add(
            "search_terms",
            (
                _Rec(
                    search_term_row(
                        day, campaign_id, ad_group_id, f"term {campaign_id} {ad_group_id} {index}"
                    ),
                    day=day,
                    campaign=campaign_id,
                    ad_group=ad_group_id,
                )
                for index in range(count)
            ),
        )

    def add_conversion_action(
        self,
        action_id: int,
        *,
        status: str,
        include: bool | None,
        counting_type: str,
        attribution_model: str | None,
    ) -> None:
        """Add one Q-G8 conversion action."""
        self._add(
            "actions",
            [
                _Rec(
                    conversion_action_row(
                        action_id,
                        status=status,
                        include=include,
                        counting_type=counting_type,
                        attribution_model=attribution_model,
                    ),
                    key=action_id,
                )
            ],
        )

    def rows(self, table: str) -> list[JsonRow]:
        """Every stored row of ``table``, in storage order."""
        return [rec.row for rec in self._tables[table]]

    def queries(self, query_id: str) -> list[_Received]:
        """The received calls of one template, in order."""
        return [call for call in self.received if call.query_id == query_id]

    # -- the webhook ----------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one webhook POST: check the body, record the call, answer from the account."""
        assert request.method == "POST"
        body = json.loads(request.content)
        assert isinstance(body, dict), "the webhook body is a JSON object"
        assert list(body) == BODY_KEYS, f"the body keys must be exactly {BODY_KEYS}"
        customer = body["customer_id"]
        assert isinstance(customer, str), "customer_id is a string"
        assert _CUSTOMER_ID_RE.fullmatch(customer), "customer_id is 10 digits without dashes"
        assert isinstance(body["query"], str), "query is GAQL text"
        received = _Received(len(self.received) + 1, request, body, parse_query(body["query"]))
        self.received.append(received)
        if self.hook is not None:
            answer = self.hook(received)
            if answer is not None:
                return answer
        return self._answer(received)

    def match(self, parsed: ParsedQuery) -> list[JsonRow]:
        """Return the rows a query selects, before the 10,000-row truncation."""
        template = parsed.template
        scope = parsed.scope
        table = _TABLE_OF[parsed.query_id]
        exclude_removed = any(
            condition.endswith("!= 'REMOVED'") for condition in template.fixed_conditions
        )
        start, end = _scope_date(scope, START), _scope_date(scope, END)
        campaigns = _scope_range(scope, CAMPAIGN_LO, CAMPAIGN_HI)
        ad_groups = _scope_range(scope, AD_GROUP_LO, AD_GROUP_HI)
        ad_group_id = _scope_int(scope, AD_GROUP_ID)
        last_id = _scope_int(scope, LAST_ID)
        raw_ids = scope.get(IDS)
        wanted = frozenset(raw_ids) if isinstance(raw_ids, tuple) else None
        selected: list[_Rec] = []
        for rec in self._candidates(table, start, end):
            if exclude_removed and rec.removed:
                continue
            if campaigns is not None and not _within(rec.campaign, campaigns):
                continue
            if ad_groups is not None and not _within(rec.ad_group, ad_groups):
                continue
            if ad_group_id is not None and rec.ad_group != ad_group_id:
                continue
            if wanted is not None and rec.key not in wanted:
                continue
            if last_id is not None and not _within(rec.key, (last_id + 1, INT64_MAX)):
                continue
            selected.append(rec)
        if last_id is not None:
            selected.sort(key=lambda rec: rec.key if rec.key is not None else -1)
            limit = parsed.limit
            assert limit == KEYSET_LIMIT, "every keyset form ends in LIMIT 9999"
            selected = selected[:limit]
        return [rec.row for rec in selected]

    def _candidates(self, table: str, start: date | None, end: date | None) -> Iterator[_Rec]:
        """Yield a table's rows, only those of the requested days for a daily table."""
        if start is None or end is None or table not in _DATED_TABLES:
            yield from self._tables[table]
            return
        index = self._by_day[table]
        for day in sorted(index):
            if start <= day <= end:
                yield from index[day]

    def _answer(self, received: _Received) -> httpx.Response:
        """Answer from the account: one page, at most 10,000 rows, a token only when scripted."""
        matched = self.match(received.parsed)
        offset = 0
        token = received.page_token
        if token is not None:
            assert token in self._continuations, "the page token must be one the fake returned"
            text, offset = self._continuations[token]
            assert text == received.text, "a page token comes back with its own query"
        next_token: str | None = None
        size = PAGE_SIZE
        if received.number in self.tokens:
            next_token, size = self.tokens[received.number]
        page = matched[offset : offset + size]
        if next_token is not None:
            self._continuations[next_token] = (received.text, offset + size)
        self.served[received.number] = len(page)
        return self._respond(received.parsed.template, page, next_token)

    def _respond(
        self, template: QueryTemplate, rows: Sequence[JsonRow], token: str | None
    ) -> httpx.Response:
        """Serialise the n8n envelope ``{results, fieldMask, queryResourceConsumption}``."""
        key: tuple[object, ...] = (
            template.query_id,
            token,
            self.wrap_in_array,
            tuple(id(row) for row in rows),
        )
        content = self._encoded.get(key)
        if content is None:
            envelope: dict[str, object] = {}
            if rows:
                envelope["results"] = list(rows)
            envelope["fieldMask"] = field_mask(template)
            envelope["queryResourceConsumption"] = str(len(rows) + 1)
            if token is not None:
                envelope["nextPageToken"] = token
            payload: object = [envelope] if self.wrap_in_array else envelope
            content = json.dumps(payload).encode("utf-8")
            self._encoded[key] = content
            while len(self._encoded) > _ENCODED_CACHE_SIZE:
                self._encoded.popitem(last=False)
        else:
            self._encoded.move_to_end(key)
        return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})


# ---------------------------------------------------------------------------------------------
# Call budget, clients, clock and fixtures
# ---------------------------------------------------------------------------------------------


class _CountingBudget:
    """A :class:`CallBudget` that admits ``allow`` POST attempts, then refuses every one.

    It satisfies the protocol structurally, as ``GoogleCallBudget`` does, and needs no
    database. ``allow=None`` admits every call.
    """

    def __init__(self, allow: int | None) -> None:
        self.allow = allow
        self.acquired = 0
        self.refused = 0

    async def acquire(self) -> None:
        """Count one attempt, or raise ``CallBudgetExhausted`` once ``allow`` are used."""
        if self.allow is not None and self.acquired >= self.allow:
            self.refused += 1
            raise CallBudgetExhausted()
        self.acquired += 1


class _Clock:
    """A settable timezone-aware clock shared by a test's controllers and datasets."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        """Return the current instant."""
        return self.now

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by ``delta``."""
        self.now += delta


@asynccontextmanager
async def webhook_client(
    transport: httpx.AsyncBaseTransport,
    *,
    budget: CallBudget = UNMETERED,
    retry: RetryPolicy = RETRY,
) -> AsyncIterator[GoogleAdsWebhookClient]:
    """Open the live webhook client exactly as ``sync`` builds it, on a mocked transport.

    The HTTP client comes from ``connectors.http.build_client("google", transport)`` (the 120 s
    deadline and the redacting logging hooks) and the webhook client from
    ``GoogleAdsWebhookClient.from_settings``, so the configured URL, secret header, secret and
    customer id are the ones under test. The HTTP client is closed on exit.
    """
    settings = get_settings()
    async with build_client("google", transport, settings=settings) as http:
        yield GoogleAdsWebhookClient.from_settings(settings, http, budget, retry=retry)


def search_contract() -> Contract:
    """The ``google.search`` contract: the webhook request and the response envelope."""
    return load_contract("google", SEARCH_CONTRACT)


def check_requests(fake: _FakeGoogleAds) -> None:
    """Validate every request the fake received against ``search.json``.

    For tests that serve error or wrapped bodies through ``recording_transport``, whose
    responses no success schema describes: their requests are still held to the contract.
    """
    contract = search_contract()
    for call in fake.received:
        validate_request(contract, call.request)


def halving_preorder(lo: int, hi: int, heavy: int) -> list[tuple[int, int]]:
    """Return the id ranges an id-range descent reads below ``(lo, hi)``, root included.

    AAP 0.3.3's halving rule, written out independently of ``partition.py``: every range that
    holds the ``heavy`` id returns a capped page and, while wider than one id, is replaced by its
    halves ``[lo, mid]`` and ``[mid + 1, hi]`` with ``mid = lo + (hi - lo) // 2``, the lower half
    read first and in full before the upper one; any other range is read once.
    """
    ranges: list[tuple[int, int]] = []
    stack = [(lo, hi)]
    while stack:
        low, high = stack.pop()
        ranges.append((low, high))
        if low <= heavy <= high and low < high:
            mid = low + (high - low) // 2
            stack.append((mid + 1, high))
            stack.append((low, mid))
    return ranges


def row_id(row: JsonRow, resource: str) -> int:
    """Return the INT64 ``id`` of a row's resource object (``campaign``, ``adGroup``, ...)."""
    return int(row[resource]["id"])


def criterion_key(row: JsonRow) -> tuple[int, int]:
    """Return a keyword's ``(ad_group.id, ad_group_criterion.criterion_id)`` key."""
    return row_id(row, "adGroup"), int(row["adGroupCriterion"]["criterionId"])


def search_term_key(row: JsonRow) -> tuple[str, str]:
    """Return a daily search-term row's ``(resourceName, segments.date)`` key."""
    return str(row["searchTermView"]["resourceName"]), str(row["segments"]["date"])


@pytest.fixture
def google_credentials(
    live_credentials: Callable[[Source], Mapping[str, str]],
) -> Mapping[str, str]:
    """Configure conftest's fake webhook credentials: Google resolves live, HTTP stays mocked."""
    return live_credentials("google")


async def run_entity_sync(
    db: Sessions,
    dataset_id: int,
    transport: httpx.AsyncBaseTransport,
    *,
    clock: _Clock,
    budget: CallBudget,
) -> JobResult:
    """Run the production Google ``EntitySyncJob`` once on ``transport``.

    The seam is ``SourceModeController(transports={"google": ...})``: ``open_clients("google",
    "live")`` builds the live webhook client from the settings on that transport, drawing on the
    injected call budget, so the job runs exactly as the worker runs it.
    """
    controller = SourceModeController(
        get_settings(),
        sessionmaker=db,
        clock=clock,
        budget=budget,
        transports={"google": transport},
        retry=RETRY,
    )
    async with controller.open_clients("google", "live") as clients:
        context = await JobContext.for_dataset(controller, dataset_id, clients)
        return await EntitySyncJob(context).run()


async def begin_live_dataset(db: Sessions, clock: _Clock) -> int:
    """Begin a ``building`` live Google dataset and return its id."""
    controller = SourceModeController(get_settings(), sessionmaker=db, clock=clock, retry=RETRY)
    return await controller.datasets.begin(
        "google", "live", reason="recovered: webhook served by a contract-checked transport"
    )


async def activate_dataset(db: Sessions, dataset_id: int, clock: _Clock) -> None:
    """Activate a built Google dataset, recording the freshness rows its build would have.

    Readiness requires every required freshness row (AAP 0.3.3); the entity sync recorded
    ``google.entities``, and the metrics rows a backfill would record are written here.
    """
    controller = SourceModeController(get_settings(), sessionmaker=db, clock=clock, retry=RETRY)
    required = await controller.datasets.required_freshness(dataset_id)
    async with db() as session, session.begin():
        recorded = await freshness.load_freshness(session, dataset_id)
        for name in required:
            if name not in recorded:
                await freshness.record_freshness(
                    session,
                    dataset_id,
                    name,
                    freshness.FreshnessOutcome.success(
                        clock(), covered_from=DAY1, covered_through=DAY4, complete=True
                    ),
                )
    await controller.datasets.activate(dataset_id)


async def entities_freshness(db: Sessions, dataset_id: int) -> DatasetFreshness:
    """Return the dataset's ``google.entities`` freshness row."""
    async with db() as session:
        row = (await freshness.load_freshness(session, dataset_id)).get("google.entities")
    assert row is not None, "the entity sync records google.entities"
    return row


async def campaign_ids(db: Sessions, dataset_id: int) -> dict[str, int]:
    """Map each stored campaign's external id to its internal id."""
    async with db() as session:
        result = await session.execute(
            select(Campaign.external_id, Campaign.id).where(Campaign.dataset_id == dataset_id)
        )
        return dict(result.all())


async def discrepancy_kinds(db: Sessions, dataset_id: int) -> list[tuple[int, str]]:
    """Return every discrepancy of the dataset as ``(subject_id, kind)``, sorted."""
    async with db() as session:
        result = await session.execute(
            select(Discrepancy.subject_id, Discrepancy.kind).where(
                Discrepancy.dataset_id == dataset_id
            )
        )
        return sorted((subject, kind) for subject, kind in result)


async def campaign_snapshots(db: Sessions, dataset_id: int) -> list[tuple[int, str | None]]:
    """Return every campaign snapshot of the dataset as ``(entity_id, status)``, in id order."""
    async with db() as session:
        result = await session.execute(
            select(EntitySnapshot.entity_id, EntitySnapshot.status)
            .where(
                EntitySnapshot.dataset_id == dataset_id,
                EntitySnapshot.entity_type == "campaign",
            )
            .order_by(EntitySnapshot.id)
        )
        return [(entity, status) for entity, status in result]


# ---------------------------------------------------------------------------------------------
# Request (AAP 0.3.3 "Request body", "Headers", "Response", "Pagination and completeness")
# ---------------------------------------------------------------------------------------------


async def test_request_body_and_secret_header(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Request: one POST to the webhook with exactly ``{customer_id, query, page_token}``.

    The secret travels only in the ``X-Webhook-Secret`` header (the documented default name),
    with ``Content-Type: application/json``. No Google Ads API credential header is sent and
    nothing names ``searchStream`` (AAP 0.3.3; AAP 0.7.2 keeps direct Google access out).
    """
    fake = _FakeGoogleAds()
    fake.add_customer()
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        result = await client.run_query("Q-G1")

    assert (len(result.rows), result.pages, result.calls, result.capped) == (1, 1, 1, False)
    assert PARSERS["row_customer"](result.rows[0]) == PARSERS["row_customer"](customer_row())
    [call] = fake.received
    request = call.request
    secret = google_credentials["N8N_WEBHOOK_SECRET"]
    assert request.method == "POST"
    assert str(request.url) == google_credentials["N8N_GOOGLE_ADS_WEBHOOK_URL"]
    expected = {"customer_id": CUSTOMER_ID, "query": QUERIES["Q-G1"].render(), "page_token": None}
    assert request.content == json.dumps(expected).encode("utf-8")
    assert list(json.loads(request.content)) == BODY_KEYS
    assert request.headers["X-Webhook-Secret"] == secret
    assert request.headers["Content-Type"] == "application/json"
    for name in GOOGLE_API_HEADERS:
        assert name not in request.headers
    assert [name for name, value in request.headers.items() if secret in value] == [
        "x-webhook-secret"
    ]
    assert secret.encode("utf-8") not in request.content
    assert secret not in str(request.url)
    assert "searchstream" not in str(request.url).lower()
    assert b"searchstream" not in request.content.lower()


async def test_custom_secret_header_name(
    google_credentials: Mapping[str, str],
    set_env: Callable[..., None],
    contract_checked_transport: CheckedTransport,
) -> None:
    """Request: ``N8N_WEBHOOK_SECRET_HEADER`` names the header the secret travels in, and only it.

    n8n's Header Auth credential names its own header (AAP 0.3.3), so a configured name replaces
    the ``X-Webhook-Secret`` default entirely.
    """
    set_env(N8N_WEBHOOK_SECRET_HEADER="X-N8N-Auth")
    fake = _FakeGoogleAds()
    fake.add_customer()
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        await client.run_query("Q-G1")

    [call] = fake.received
    secret = google_credentials["N8N_WEBHOOK_SECRET"]
    assert call.request.headers["X-N8N-Auth"] == secret
    assert "X-Webhook-Secret" not in call.request.headers
    assert [name for name, value in call.request.headers.items() if secret in value] == [
        "x-n8n-auth"
    ]


def _add_actions(fake: _FakeGoogleAds, count: int) -> list[int]:
    """Add ``count`` enabled last-click conversion actions and return their ids, in order."""
    ids = [9_000 + index for index in range(count)]
    for action_id in ids:
        fake.add_conversion_action(
            action_id,
            status="ENABLED",
            include=True,
            counting_type="ONE_PER_CLICK",
            attribution_model="GOOGLE_ADS_LAST_CLICK",
        )
    return ids


async def test_next_page_token_followed_at_once(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Request: a returned ``nextPageToken`` is sent back at once as ``page_token``.

    The continuation repeats the identical query, the pages concatenate in order and both
    calls are counted. A relay that returns the same token twice fails the query as a
    ``QueryDefect`` instead of looping (AAP 0.3.3 "Pagination and completeness").
    """
    fake = _FakeGoogleAds()
    ids = _add_actions(fake, 15)
    fake.tokens = {1: ("tok-1", 7)}
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        result = await client.run_query("Q-G8")

    assert [row_id(row, "conversionAction") for row in result.rows] == ids
    assert (result.pages, result.calls, result.capped) == (2, 2, False)
    first, second = fake.received
    assert (first.page_token, second.page_token) == (None, "tok-1")
    assert second.text == first.text == QUERIES["Q-G8"].render()
    assert (fake.served[1], fake.served[2]) == (7, 8)

    looping = _FakeGoogleAds()
    _add_actions(looping, 15)
    looping.tokens = {1: ("tok-1", 7), 2: ("tok-1", 3)}
    async with webhook_client(contract_checked_transport("google", looping.handler)) as client:
        with pytest.raises(QueryDefect, match="page token repeated") as raised:
            await client.run_query("Q-G8")
    assert raised.value.query_id == "Q-G8"
    assert [call.page_token for call in looping.received] == [None, "tok-1"]


async def test_one_element_array_unwrapped(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
    recording_transport: RecordingFactory,
) -> None:
    """Request: n8n's ``[{results, fieldMask, queryResourceConsumption}]`` parses like the object.

    The wrapped envelope inside the array is held to ``search.json`` and its rows to
    ``row_conversion_action.json`` explicitly, because the array itself is no success schema.
    """
    bare = _FakeGoogleAds()
    _add_actions(bare, 4)
    async with webhook_client(contract_checked_transport("google", bare.handler)) as client:
        expected = await client.run_query("Q-G8")

    wrapped = _FakeGoogleAds()
    _add_actions(wrapped, 4)
    wrapped.wrap_in_array = True
    served: list[httpx.Response] = []

    def serve(request: httpx.Request) -> httpx.Response:
        """Answer through the wrapping fake and keep the response for inspection."""
        response = wrapped.handler(request)
        served.append(response)
        return response

    async with webhook_client(recording_transport(serve)) as client:
        result = await client.run_query("Q-G8")

    assert result == expected
    assert [PARSERS["row_conversion_action"](row) for row in result.rows] == [
        PARSERS["row_conversion_action"](row) for row in expected.rows
    ]
    check_requests(wrapped)
    [response] = served
    body = json.loads(response.content)
    assert isinstance(body, list)
    assert len(body) == 1
    validate_response(search_contract(), body[0])
    validate_google_rows(QUERIES["Q-G8"].render(), body[0])


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="empty-array"),
        pytest.param({}, id="empty-object"),
        pytest.param({"queryResourceConsumption": "0"}, id="no-results-no-fieldmask"),
        pytest.param([{}, {}], id="two-item-array"),
    ],
)
async def test_unrecognised_body_is_a_defect(
    google_credentials: Mapping[str, str],
    recording_transport: RecordingFactory,
    contract_checked_transport: CheckedTransport,
    body: object,
) -> None:
    """Request: a body with neither ``results`` nor ``fieldMask`` is a ``QueryDefect``.

    It is never read as zero rows, which on Q-G2 would let a sync infer false removals, and it
    is not retried. A ``fieldMask`` without ``results`` is Google's empty result: zero rows.
    """
    fake = _FakeGoogleAds()
    fake.hook = lambda call: httpx.Response(200, json=body)
    async with webhook_client(recording_transport(fake.handler)) as client:
        with pytest.raises(QueryDefect) as raised:
            await client.run_query("Q-G2", {LAST_ID: 0})
    assert raised.value.query_id == "Q-G2"
    assert len(fake.received) == 1
    check_requests(fake)

    empty = _FakeGoogleAds()
    async with webhook_client(contract_checked_transport("google", empty.handler)) as client:
        result = await client.run_query("Q-G2", {LAST_ID: 0})
    assert (result.rows, result.pages, result.calls, result.capped) == ((), 1, 1, False)


def _google_failure(error_type: str, code: str, message: str) -> dict[str, object]:
    """A bare GoogleAdsFailure ``{errors[], requestId}`` naming one Google Ads error type."""
    return {
        "@type": GOOGLE_FAILURE_TYPE,
        "errors": [{"errorCode": {error_type: code}, "message": message}],
        "requestId": "req-0001",
    }


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(
            {
                "error": {
                    "code": 403,
                    "message": "The caller does not have permission",
                    "status": "PERMISSION_DENIED",
                }
            },
            "PERMISSION_DENIED",
            id="permission-denied",
        ),
        pytest.param(
            [
                {
                    "error": {
                        "code": 403,
                        "message": "The caller does not have permission",
                        "status": "PERMISSION_DENIED",
                    }
                }
            ],
            "PERMISSION_DENIED",
            id="permission-denied-in-array",
        ),
        pytest.param(
            _google_failure(
                "authenticationError",
                "OAUTH_TOKEN_INVALID",
                "Oauth token in the header is not valid.",
            ),
            "OAUTH_TOKEN_INVALID",
            id="authentication-error",
        ),
        pytest.param(
            _google_failure(
                "authorizationError",
                "USER_PERMISSION_DENIED",
                "User doesn't have permission to access customer.",
            ),
            "USER_PERMISSION_DENIED",
            id="authorization-error",
        ),
    ],
)
async def test_denied_inside_http_200(
    google_credentials: Mapping[str, str],
    recording_transport: RecordingFactory,
    body: object,
    code: str,
) -> None:
    """Request: a Google denial n8n relays inside HTTP 200 is ``AccessDenied``, never retried.

    ``PERMISSION_DENIED``, ``AuthenticationError`` and ``AuthorizationError`` switch the source
    to simulated data (AAP 0.3.3 fallback table), whatever HTTP status n8n answered with.
    """
    fake = _FakeGoogleAds()
    fake.hook = lambda call: httpx.Response(200, json=body)
    async with webhook_client(recording_transport(fake.handler)) as client:
        with pytest.raises(AccessDenied) as raised:
            await client.run_query("Q-G1")
    assert raised.value.http_status == 200
    assert raised.value.code == code
    assert len(fake.received) == 1
    assert google_credentials["N8N_WEBHOOK_SECRET"] not in str(raised.value)
    check_requests(fake)


def _resource_exhausted(call: _Received) -> httpx.Response:
    """Google ``RESOURCE_EXHAUSTED`` relayed by n8n inside HTTP 200."""
    del call
    return httpx.Response(
        200,
        json={
            "error": {
                "code": 429,
                "message": "Resource has been exhausted (e.g. check quota).",
                "status": "RESOURCE_EXHAUSTED",
            }
        },
    )


def _read_timeout(call: _Received) -> httpx.Response:
    """A webhook call that times out reading the response."""
    raise httpx.ReadTimeout("timed out reading the webhook response", request=call.request)


def _workflow_error(call: _Received) -> httpx.Response:
    """n8n's HTTP 500 for a failed workflow."""
    del call
    return httpx.Response(500, json=dict(N8N_WORKFLOW_ERROR))


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param(_resource_exhausted, id="resource-exhausted-in-200"),
        pytest.param(_read_timeout, id="read-timeout"),
        pytest.param(_workflow_error, id="http-500"),
    ],
)
async def test_transient_retried_three_times(
    google_credentials: Mapping[str, str],
    recording_transport: RecordingFactory,
    hook: ResponseHook,
) -> None:
    """Request: ``RESOURCE_EXHAUSTED``, timeouts and 5xx are ``TransientError`` after 3 attempts.

    Every attempt is a separate POST counted against the call budget; the error is never the
    product's own ``CallBudgetExhausted`` and never mock data (AAP 0.3.3 fallback table).
    """
    fake = _FakeGoogleAds()
    fake.hook = hook
    budget = _CountingBudget(allow=None)
    async with webhook_client(recording_transport(fake.handler), budget=budget) as client:
        with pytest.raises(TransientError) as raised:
            await client.run_query("Q-G1")
        assert client.calls_made == 3
    assert not isinstance(raised.value, CallBudgetExhausted)
    assert len(fake.received) == 3
    assert budget.acquired == 3
    check_requests(fake)


# ---------------------------------------------------------------------------------------------
# Keyset enumeration (AAP 0.3.3 "Entity enumeration by keyset", "Safe absence")
# ---------------------------------------------------------------------------------------------


# AAP 0.11.2 states the Q-G2 guarantee as "25,000 campaigns ... pages of 9,999, 9,999 and 7,002
# rows", which no keyset can satisfy: pages of 9,999 leave 5,002 of 25,000, and a last page of
# 7,002 needs 27,000. Both readings are proven, so the guarantee holds whichever number the plan
# meant: three keyset calls with strictly increasing campaign.id filters and a complete union.
@pytest.mark.parametrize(
    ("campaigns", "pages"),
    [
        pytest.param(25_000, (9_999, 9_999, 5_002), id="25000-campaigns"),
        pytest.param(27_000, (9_999, 9_999, 7_002), id="27000-campaigns"),
    ],
)
async def test_qg2_keyset_scan_25000_campaigns(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
    campaigns: int,
    pages: tuple[int, int, int],
) -> None:
    """Keyset enumeration: Q-G2 reads every non-removed campaign in pages of at most 9,999.

    Each page is ``campaign.status != 'REMOVED' AND campaign.id > {last} ORDER BY campaign.id
    LIMIT 9999``, below the 10,000-row page size, so no page can be silently truncated; the
    scan continues from the last id of a full page and ends on the first shorter one.
    """
    fake = _FakeGoogleAds()
    ids = [1_000 + index for index in range(campaigns)]
    fake.add_campaigns(ids)
    removed = [ids[-1] + 1 + index for index in range(5)]
    fake.add_campaigns(removed, status="REMOVED")
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch("Q-G2")

    calls = fake.queries("Q-G2")
    assert [fake.served[call.number] for call in calls] == list(pages)
    last_ids = [call.last_id for call in calls]
    assert last_ids == [0, ids[9_998], ids[19_997]]
    assert all(earlier < later for earlier, later in pairwise(last_ids))
    for call in calls:
        assert call.where.startswith("campaign.status != 'REMOVED' AND campaign.id > ")
        assert call.text.endswith(f"ORDER BY campaign.id LIMIT {KEYSET_LIMIT}")
        assert call.page_token is None
    returned = [row_id(row, "campaign") for row in outcome.rows]
    assert len(returned) == len(set(returned)) == campaigns
    assert set(returned) == set(ids)
    assert (outcome.completeness, outcome.reason, outcome.calls) == ("complete", None, 3)
    assert outcome.deferred == ()
    assert outcome.interrupted_by is None


async def test_qg4_keyset_scan_ad_groups(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Keyset enumeration: Q-G4 scans ``ad_group.id`` exactly as Q-G2 scans ``campaign.id``.

    25,000 ad groups read as pages of 9,999, 9,999 and 5,002 with strictly increasing
    ``ad_group.id >`` filters. A tracked ad group the complete scan no longer returns (it is
    ``REMOVED``) is reported absent, from the complete read only.
    """
    fake = _FakeGoogleAds()
    ids = [50_000 + index for index in range(25_000)]
    for campaign_index in range(5):
        fake.add_ad_groups(ids[campaign_index::5], campaign_id=7_000 + campaign_index)
    gone = 40_000
    fake.add_ad_groups([gone], campaign_id=7_000, status="REMOVED")
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        enumeration = await enumerate_entities(client, "Q-G4", {ids[0], ids[-1], gone})

    calls = fake.queries("Q-G4")
    assert [fake.served[call.number] for call in calls] == [9_999, 9_999, 5_002]
    last_ids = [call.last_id for call in calls]
    assert last_ids == [0, ids[9_998], ids[19_997]]
    for call in calls:
        assert call.where.startswith("ad_group.status != 'REMOVED' AND ad_group.id > ")
        assert call.text.endswith(f"ORDER BY ad_group.id LIMIT {KEYSET_LIMIT}")
    returned = [row_id(row, "adGroup") for row in enumeration.rows]
    assert len(returned) == len(set(returned)) == 25_000
    assert set(returned) == set(ids)
    assert enumeration.complete
    assert enumeration.absent_keys == frozenset({gone})
    assert enumeration.calls == 3


def _entity_account(fake: _FakeGoogleAds, campaigns: Mapping[int, str]) -> None:
    """Give the fake an account with campaigns in the given statuses, each with its budget."""
    fake.add_customer()
    for campaign_id, status in campaigns.items():
        fake.add_campaigns([campaign_id], status=status, budgets=True)


async def test_keyset_page_deferred_marks_entities_incomplete(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
    db: Sessions,
) -> None:
    """Keyset enumeration: a deferred keyset page leaves ``google.entities`` incomplete.

    At the connector, the call budget refusing page 2 of the Q-G2 scan keeps page 1's rows,
    reports ``incomplete`` with the resumable scope in ``deferred`` and resumes to the complete
    set. Through the production ``EntitySyncJob`` on a live dataset (begun, built and activated
    as a recovery would), the same deferral records ``google.entities`` ``incomplete`` with the
    repairable reason ``REASON_DEFERRED`` and infers nothing: no ``missing`` discrepancy and no
    ``ended`` snapshot, although every tracked campaign is absent from the partial read. A
    later complete sync does infer them, which proves the incomplete read was what held them.
    """
    fake = _FakeGoogleAds()
    ids = [1_000 + index for index in range(25_000)]
    fake.add_campaigns(ids)
    budget = _CountingBudget(allow=1)
    async with webhook_client(
        contract_checked_transport("google", fake.handler), budget=budget
    ) as client:
        fetcher = PartitionedFetcher(client)
        outcome = await fetcher.partitioned_fetch("Q-G2")
    assert outcome.completeness == "incomplete"
    assert [row_id(row, "campaign") for row in outcome.rows] == ids[:KEYSET_LIMIT]
    assert [dict(scope) for scope in outcome.deferred] == [{LAST_ID: ids[9_998]}]
    assert isinstance(outcome.interrupted_by, CallBudgetExhausted)
    assert outcome.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert REASON_BUDGET == freshness.REASON_DEFERRED
    assert freshness.is_repairable_reason(outcome.reason)
    assert (outcome.calls, budget.acquired, budget.refused, len(fake.received)) == (1, 1, 1, 1)
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        resumed = await PartitionedFetcher(client).partitioned_fetch("Q-G2", outcome.deferred[0])
    assert resumed.complete
    union = [row_id(row, "campaign") for row in (*outcome.rows, *resumed.rows)]
    assert union == ids

    clock = _Clock(T0)
    dataset_id = await begin_live_dataset(db, clock)
    first = _FakeGoogleAds()
    _entity_account(first, {101: "ENABLED", 102: "ENABLED", 103: "ENABLED"})
    built = await run_entity_sync(
        db,
        dataset_id,
        contract_checked_transport("google", first.handler),
        clock=clock,
        budget=_CountingBudget(allow=None),
    )
    assert built.status == "succeeded"
    assert (await entities_freshness(db, dataset_id)).completeness == "complete"
    await activate_dataset(db, dataset_id, clock)
    internal = await campaign_ids(db, dataset_id)
    snapshots_before = await campaign_snapshots(db, dataset_id)
    assert sorted(entity for entity, _status in snapshots_before) == sorted(internal.values())

    # 101 is now REMOVED and 102 gone, but the budget admits only Q-G1: Q-G2 is deferred.
    clock.advance(timedelta(hours=3))
    second = _FakeGoogleAds()
    _entity_account(second, {101: "REMOVED", 103: "ENABLED"})
    deferred = await run_entity_sync(
        db,
        dataset_id,
        contract_checked_transport("google", second.handler),
        clock=clock,
        budget=_CountingBudget(allow=1),
    )
    assert deferred.status == "succeeded"
    assert [call.query_id for call in second.received] == ["Q-G1"]
    row = await entities_freshness(db, dataset_id)
    assert row.completeness == "incomplete"
    assert row.reason is not None
    assert row.reason.startswith(freshness.REASON_DEFERRED)
    assert await discrepancy_kinds(db, dataset_id) == []
    assert await campaign_snapshots(db, dataset_id) == snapshots_before

    clock.advance(timedelta(hours=3))
    third = _FakeGoogleAds()
    _entity_account(third, {101: "REMOVED", 103: "ENABLED"})
    complete = await run_entity_sync(
        db,
        dataset_id,
        contract_checked_transport("google", third.handler),
        clock=clock,
        budget=_CountingBudget(allow=None),
    )
    assert complete.status == "succeeded"
    assert (await entities_freshness(db, dataset_id)).completeness == "complete"
    [reread] = third.queries("Q-G2b")
    assert reread.ids == (101, 102)
    kinds = await discrepancy_kinds(db, dataset_id)
    assert (internal["102"], "missing") in kinds
    assert (internal["101"], "missing") not in kinds
    latest = await campaign_snapshots(db, dataset_id)
    assert (internal["101"], "ended") in latest[len(snapshots_before) :]
    assert all(entity != internal["102"] for entity, _status in latest[len(snapshots_before) :])


async def test_qg2b_chunks_of_1000(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Keyset enumeration: Q-G2b re-reads absent tracked campaigns in ``IN`` lists of 1,000.

    2,500 tracked campaigns are absent from a complete Q-G2 scan, so Q-G2b runs three chunks
    of 1,000, 1,000 and 500 ascending ids (``MAX_IDS_PER_IN``), none with a status filter, and
    the removed campaigns it returns are told apart from the ones it does not return.
    """
    fake = _FakeGoogleAds()
    present = [1_000 + index for index in range(10)]
    fake.add_campaigns(present)
    absent = [100_000 + index for index in range(2_500)]
    removed = absent[::10]
    fake.add_campaigns(removed, status="REMOVED")
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        enumeration = await enumerate_campaigns(client, {*present, *absent})

    chunks = fake.queries("Q-G2b")
    assert [len(call.ids) for call in chunks] == [MAX_IDS_PER_IN, MAX_IDS_PER_IN, 500]
    assert [campaign for call in chunks for campaign in call.ids] == absent
    assert QUERIES["Q-G2b"].fixed_conditions == ()
    for call in chunks:
        assert call.where.startswith("campaign.id IN (")
        assert "status" not in call.where
    assert enumeration.complete
    assert sorted(row_id(row, "campaign") for row in enumeration.removed) == removed
    assert enumeration.missing_ids == frozenset(absent) - frozenset(removed)
    assert sorted(row_id(row, "campaign") for row in enumeration.present) == present
    assert enumeration.calls == 4


# ---------------------------------------------------------------------------------------------
# Dated partitions (AAP 0.3.3 "Dated queries")
# ---------------------------------------------------------------------------------------------


async def test_dated_9999_rows_complete(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Dated partitions: a range returning fewer than 10,000 rows is complete in one call."""
    fake = _FakeGoogleAds()
    campaigns = [20_000 + index for index in range(3_333)]
    for day in (DAY1, DAY2, DAY3):
        fake.add_campaign_days(day, campaigns)
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch(
            "Q-G3", {START: DAY1, END: DAY3}
        )

    [call] = fake.received
    assert call.scope == {START: DAY1, END: DAY3}
    assert fake.served[call.number] == 9_999
    assert len(outcome.rows) == 9_999
    assert (outcome.completeness, outcome.calls, outcome.partitions_read) == ("complete", 1, 1)


async def test_dated_10000_halves_date_then_campaign_ranges_then_ad_group_ranges(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Dated partitions: a capped range is halved by date, then campaign range, then ad group.

    Q-G6 over four days returns exactly 10,000 rows with no token, so the range is halved down
    to single days; the capped day is split by numeric ``campaign.id >= lo AND campaign.id <=
    hi`` ranges from ``[0, INT64_MAX]`` down to its one heavy campaign, never by ``IN`` lists
    of known ids; that campaign-day is split by ``ad_group.id`` ranges. A campaign that is in no
    entity list (removed mid-window) is still covered by the numeric ranges. Capped responses
    are discarded and re-read by their halves, so the union is the data set with no duplicate.
    """
    heavy, other, removed = 1_000, 1_001, INT64_MAX - 100
    low_group, high_group = 5, INT64_MAX - 5
    fake = _FakeGoogleAds()
    fake.add_campaigns([heavy, other])
    fake.add_search_terms(DAY2, heavy, low_group, 6_000)
    fake.add_search_terms(DAY2, heavy, high_group, 6_000)
    fake.add_search_terms(DAY2, removed, 77, 40)
    fake.add_search_terms(DAY1, other, 31, 50)
    fake.add_search_terms(DAY3, other, 31, 30)
    fake.add_search_terms(DAY4, other, 31, 20)
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch(
            "Q-G6", {START: DAY1, END: DAY4}
        )

    single_day: dict[str, object] = {START: DAY2, END: DAY2}
    expected: list[dict[str, object]] = [
        {START: DAY1, END: DAY4},
        {START: DAY1, END: DAY2},
        {START: DAY1, END: DAY1},
        single_day,
    ]
    for lo, hi in halving_preorder(INT64_MIN_ID, INT64_MAX, heavy)[1:]:
        expected.append({**single_day, CAMPAIGN_LO: lo, CAMPAIGN_HI: hi})
        if (lo, hi) == (heavy, heavy):
            for group_lo, group_hi in ((INT64_MIN_ID, FULL_MID), (FULL_MID + 1, INT64_MAX)):
                expected.append(
                    {
                        **single_day,
                        CAMPAIGN_LO: heavy,
                        CAMPAIGN_HI: heavy,
                        AD_GROUP_LO: group_lo,
                        AD_GROUP_HI: group_hi,
                    }
                )
    expected.append({START: DAY3, END: DAY4})
    calls = fake.queries("Q-G6")
    assert [call.scope for call in calls] == expected
    assert expected[4] == {**single_day, CAMPAIGN_LO: INT64_MIN_ID, CAMPAIGN_HI: FULL_MID}
    capped = [call for call in calls if fake.served[call.number] == PAGE_SIZE]
    assert len(capped) == 3 + HALVINGS
    for call in calls:
        assert call.where.startswith("segments.date BETWEEN '")
        assert " IN (" not in call.text
        assert call.page_token is None

    assert removed not in {row_id(row, "campaign") for row in fake.rows("campaigns")}
    assert sum(1 for row in outcome.rows if row_id(row, "campaign") == removed) == 40
    # A search-term view's resource name is the same on every day, so a daily row is keyed by
    # its resource name and native date.
    returned = [search_term_key(row) for row in outcome.rows]
    stored = [search_term_key(row) for row in fake.rows("search_terms")]
    assert len(returned) == len(set(returned)) == len(stored)
    assert set(returned) == set(stored)
    assert (outcome.completeness, outcome.reason, outcome.deferred) == ("complete", None, ())
    assert outcome.calls == outcome.partitions_read == len(expected)


async def test_qg3_no_status_filter(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Dated partitions: Q-G3 is bounded only by ``segments.date BETWEEN``, with no status filter.

    Spend by a campaign removed during the window still counts (AAP 0.3.3 "Queries issued"):
    the daily rows of ``REMOVED`` campaigns are read, here across a date split.
    """
    fake = _FakeGoogleAds()
    campaigns = [20_000 + index for index in range(3_400)]
    fake.add_campaigns(campaigns[:100], status="REMOVED")
    fake.add_campaigns(campaigns[100:])
    for day in (DAY1, DAY2, DAY3):
        fake.add_campaign_days(day, campaigns)
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch(
            "Q-G3", {START: DAY1, END: DAY3}
        )

    calls = fake.queries("Q-G3")
    assert [call.scope for call in calls] == [
        {START: DAY1, END: DAY3},
        {START: DAY1, END: DAY2},
        {START: DAY3, END: DAY3},
    ]
    assert [fake.served[call.number] for call in calls] == [PAGE_SIZE, 6_800, 3_400]
    assert QUERIES["Q-G3"].fixed_conditions == ()
    for call in calls:
        assert call.where.startswith("segments.date BETWEEN '")
        assert "status" not in call.text
    assert len(outcome.rows) == 10_200
    removed = set(campaigns[:100])
    assert sum(1 for row in outcome.rows if row_id(row, "campaign") in removed) == 300
    assert outcome.complete


async def test_qg5_single_ad_group_day_uses_criterion_keyset(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Dated partitions: a capped Q-G5 (day, ad group) is read by keyset on ``criterion_id``.

    Criterion ids are unique only within an ad group, so at one day and one ad group the read
    switches to ``ad_group.id = G AND ad_group_criterion.criterion_id > {last} ORDER BY
    ad_group_criterion.criterion_id LIMIT 9999``: 15,000 keyword rows read as pages of 9,999
    and 5,001. The fetch starts at the finest (day, campaign, ad group) partition; the descent
    to it from ``[0, INT64_MAX]`` is proven by the Q-G6 descent test, and repeating its 126
    capped calls here would add nothing but time.
    """
    campaign, group = 4_000, 7_000
    criteria = [10 + index for index in range(15_000)]
    fake = _FakeGoogleAds()
    fake.add_keyword_days(DAY2, campaign, group, criteria)
    fake.add_keyword_days(DAY1, campaign, group, criteria[:30])
    fake.add_keyword_days(DAY2, campaign, group + 1, criteria[:20])
    finest: dict[str, object] = {
        START: DAY2,
        END: DAY2,
        CAMPAIGN_LO: campaign,
        CAMPAIGN_HI: campaign,
        AD_GROUP_LO: group,
        AD_GROUP_HI: group,
    }
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch("Q-G5", finest)

    keyset: dict[str, object] = {
        START: DAY2,
        END: DAY2,
        CAMPAIGN_LO: campaign,
        CAMPAIGN_HI: campaign,
        AD_GROUP_ID: group,
    }
    calls = fake.queries("Q-G5")
    assert [call.scope for call in calls] == [
        finest,
        {**keyset, LAST_ID: INT64_MIN_ID},
        {**keyset, LAST_ID: criteria[9_998]},
    ]
    assert [fake.served[call.number] for call in calls] == [PAGE_SIZE, 9_999, 5_001]
    for call in calls[1:]:
        last = call.last_id
        assert f"ad_group.id = {group} AND ad_group_criterion.criterion_id > {last}" in call.text
        assert call.text.endswith(f"ORDER BY ad_group_criterion.criterion_id LIMIT {KEYSET_LIMIT}")
    returned = [criterion_key(row) for row in outcome.rows]
    assert returned == [(group, criterion) for criterion in criteria]
    assert all(row["segments"]["date"] == DAY2.isoformat() for row in outcome.rows)
    assert (outcome.completeness, outcome.calls) == ("complete", 3)


async def test_qg6_finest_partition_incomplete(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Dated partitions: a capped Q-G6 (day, ad group) is kept as returned and ``incomplete``.

    Search-term rows have no unique sortable id, so a single (day, ad group) that still returns
    10,000 rows is the finest partition: exactly those rows are kept, and the fetch reports the
    permanent reason "result limit reached at finest partition" that ``google.search_terms``
    records (AAP 0.3.3), naming the partition. The fetch starts at that partition, as in the
    Q-G5 keyset test.
    """
    campaign, group = 4_100, 7_100
    fake = _FakeGoogleAds()
    fake.add_search_terms(DAY2, campaign, group, 12_000)
    fake.add_search_terms(DAY2, campaign, group + 1, 15)
    finest: dict[str, object] = {
        START: DAY2,
        END: DAY2,
        CAMPAIGN_LO: campaign,
        CAMPAIGN_HI: campaign,
        AD_GROUP_LO: group,
        AD_GROUP_HI: group,
    }
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch("Q-G6", finest)

    [call] = fake.received
    assert call.scope == finest
    first_page = [row for row in fake.rows("search_terms") if row_id(row, "adGroup") == group]
    assert list(outcome.rows) == first_page[:PAGE_SIZE]
    assert outcome.completeness == "incomplete"
    assert outcome.reason == f"{REASON_FINEST_PARTITION} ({DAY2.isoformat()}, ad group {group})"
    assert REASON_FINEST_PARTITION == freshness.REASON_FINEST_PARTITION
    assert not freshness.is_repairable_reason(outcome.reason)
    assert (outcome.deferred, outcome.interrupted_by) == ((), None)


# ---------------------------------------------------------------------------------------------
# Q-G7 and the call budget (AAP 0.3.3 "Keywords", "Budget", "Timeouts and budget")
# ---------------------------------------------------------------------------------------------


async def test_qg7_range_halving_then_criterion_keyset(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Q-G7: ``ad_group.id`` ranges halve from ``[0, INT64_MAX]``, then a criterion keyset.

    One ad group holds 15,000 keywords, so every range holding it is capped and halved until
    it stands alone; that ad group is then read by keyset on ``criterion_id`` in pages of 9,999
    and 5,001. Every query keeps ``ad_group_criterion.type = 'KEYWORD' AND
    ad_group_criterion.status != 'REMOVED'``, every keyword of every ad group is returned once,
    and the tracked keyword that is now ``REMOVED`` is reported absent from the complete read.
    """
    heavy, near, nearer, far = 2_000, 2_001, 2_002, INT64_MAX - 7
    campaign = 3_000
    criteria = [1 + index for index in range(15_000)]
    fake = _FakeGoogleAds()
    fake.add_criteria(heavy, campaign, criteria)
    fake.add_criteria(near, campaign, range(1, 21), quality_score=None)
    fake.add_criteria(near, campaign, [999], status="REMOVED")
    fake.add_criteria(nearer, campaign, range(1, 21))
    fake.add_criteria(far, campaign, range(1, 11))
    tracked = {(heavy, 1), (far, 10), (near, 999)}
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        enumeration = await enumerate_entities(client, "Q-G7", tracked)

    expected: list[dict[str, object]] = []
    for lo, hi in halving_preorder(INT64_MIN_ID, INT64_MAX, heavy):
        expected.append({AD_GROUP_LO: lo, AD_GROUP_HI: hi})
        if (lo, hi) == (heavy, heavy):
            expected.append({AD_GROUP_ID: heavy, LAST_ID: INT64_MIN_ID})
            expected.append({AD_GROUP_ID: heavy, LAST_ID: criteria[9_998]})
    calls = fake.queries("Q-G7")
    assert [call.scope for call in calls] == expected
    assert expected[0] == {AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: INT64_MAX}
    keyset_calls = [call for call in calls if AD_GROUP_ID in call.scope]
    assert [fake.served[call.number] for call in keyset_calls] == [9_999, 5_001]
    capped = [call for call in calls if fake.served[call.number] == PAGE_SIZE]
    assert len(capped) == 1 + HALVINGS
    for call in calls:
        assert call.where.startswith(
            "ad_group_criterion.type = 'KEYWORD' AND ad_group_criterion.status != 'REMOVED' AND "
        )
    for call in keyset_calls:
        assert call.text.endswith(f"ORDER BY ad_group_criterion.criterion_id LIMIT {KEYSET_LIMIT}")

    returned = [criterion_key(row) for row in enumeration.rows]
    stored = {
        criterion_key(row)
        for row in fake.rows("criteria")
        if row["adGroupCriterion"]["status"] != "REMOVED"
    }
    assert len(returned) == len(set(returned)) == len(stored) == 15_000 + 20 + 20 + 10
    assert set(returned) == stored
    assert enumeration.complete
    assert enumeration.absent_keys == frozenset({(near, 999)})
    assert enumeration.calls == len(expected)


def _three_day_campaigns(fake: _FakeGoogleAds) -> None:
    """Give the fake 3,400 campaigns with one Q-G3 row on each of three days (10,200 rows)."""
    campaigns = [20_000 + index for index in range(3_400)]
    for day in (DAY1, DAY2, DAY3):
        fake.add_campaign_days(day, campaigns)


async def test_budget_exhaustion_mid_partition_incomplete(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Budget: the call budget running out during dated partitioning leaves it ``incomplete``.

    The rows already read are kept, the unread partition is returned in ``deferred`` as a
    scope the fetcher accepts, the reason is the repairable ``REASON_DEFERRED`` that keeps the
    dataset ``incomplete`` until a later window finishes it, and resuming it completes the set.
    """
    fake = _FakeGoogleAds()
    _three_day_campaigns(fake)
    budget = _CountingBudget(allow=2)
    async with webhook_client(
        contract_checked_transport("google", fake.handler), budget=budget
    ) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch(
            "Q-G3", {START: DAY1, END: DAY3}
        )

    assert [call.scope for call in fake.received] == [
        {START: DAY1, END: DAY3},
        {START: DAY1, END: DAY2},
    ]
    assert outcome.completeness == "incomplete"
    assert len(outcome.rows) == 6_800
    assert {row["segments"]["date"] for row in outcome.rows} == {
        DAY1.isoformat(),
        DAY2.isoformat(),
    }
    assert [dict(scope) for scope in outcome.deferred] == [{START: DAY3, END: DAY3}]
    assert isinstance(outcome.interrupted_by, CallBudgetExhausted)
    assert outcome.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert outcome.reason.startswith(freshness.REASON_DEFERRED)
    assert (outcome.calls, budget.acquired, budget.refused) == (2, 2, 1)

    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        resumed = await PartitionedFetcher(client).partitioned_fetch("Q-G3", outcome.deferred[0])
    assert resumed.complete
    assert len(outcome.rows) + len(resumed.rows) == 10_200


async def calls_24h(db: Sessions) -> int | None:
    """Return ``source_connection.calls_24h`` of Google, or ``None`` when the row is absent."""
    async with db() as session:
        return await session.scalar(
            select(SourceConnection.calls_24h).where(SourceConnection.source == "google")
        )


async def test_call_budget_counts_every_call(
    google_credentials: Mapping[str, str],
    set_env: Callable[..., None],
    contract_checked_transport: CheckedTransport,
    db: Sessions,
) -> None:
    """Budget: ``GoogleCallBudget`` counts every webhook POST and defers the call past its cap.

    With ``GOOGLE_DAILY_CALL_BUDGET=3`` a Q-G6 fetch makes a first page, its next page (the
    relay returned a token, then a capped page) and one date partition; the fourth call is
    refused before any POST, deferring the last partition. ``source_connection.calls_24h``
    equals the calls made, and a retried call (HTTP 500, then 200) counts both attempts.
    """
    set_env(GOOGLE_DAILY_CALL_BUDGET="3")
    fake = _FakeGoogleAds()
    fake.add_search_terms(DAY1, 1_000, 10, 6_000)
    fake.add_search_terms(DAY2, 1_000, 10, 15_000)
    fake.tokens = {1: ("tok-1", PAGE_SIZE)}
    budget = GoogleCallBudget(get_settings(), sessionmaker=db)
    async with webhook_client(
        contract_checked_transport("google", fake.handler), budget=budget
    ) as client:
        outcome = await PartitionedFetcher(client).partitioned_fetch(
            "Q-G6", {START: DAY1, END: DAY2}
        )

    assert [(call.scope, call.page_token) for call in fake.received] == [
        ({START: DAY1, END: DAY2}, None),
        ({START: DAY1, END: DAY2}, "tok-1"),
        ({START: DAY1, END: DAY1}, None),
    ]
    assert [fake.served[call.number] for call in fake.received] == [PAGE_SIZE, PAGE_SIZE, 6_000]
    assert outcome.completeness == "incomplete"
    assert [dict(scope) for scope in outcome.deferred] == [{START: DAY2, END: DAY2}]
    assert isinstance(outcome.interrupted_by, CallBudgetExhausted)
    assert outcome.calls == 3
    assert len(outcome.rows) == 6_000
    assert await calls_24h(db) == 3
    assert await budget.used_24h() == 3

    set_env(GOOGLE_DAILY_CALL_BUDGET="10")
    retried = _FakeGoogleAds()
    retried.add_customer()
    retried.hook = lambda call: _workflow_error(call) if call.number == 1 else None
    widened = GoogleCallBudget(get_settings(), sessionmaker=db)
    async with webhook_client(
        contract_checked_transport("google", retried.handler), budget=widened
    ) as client:
        result = await client.run_query("Q-G1")
    assert (result.calls, len(result.rows), len(retried.received)) == (2, 1, 2)
    assert await calls_24h(db) == 5
    assert await widened.used_24h() == 5


# ---------------------------------------------------------------------------------------------
# Entity coverage and safe absence (AAP 0.3.3 "Safe absence", "Queries issued")
# ---------------------------------------------------------------------------------------------


async def test_qg2b_removed_and_missing_only_after_complete_scan(
    google_credentials: Mapping[str, str], contract_checked_transport: CheckedTransport
) -> None:
    """Entity coverage: after a complete Q-G2 scan, Q-G2b tells removed from missing campaigns.

    A tracked campaign Q-G2b returns as ``REMOVED`` is ``removed`` (snapshotted as ``ended``);
    one that neither query returns is ``missing``. Q-G2b reads exactly the tracked ids the
    complete scan no longer returned, with no status filter.
    """
    fake = _FakeGoogleAds()
    present = [1_000, 1_001]
    fake.add_campaigns(present)
    removed_id, missing_id = 1_500, 1_600
    fake.add_campaigns([removed_id], status="REMOVED")
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        enumeration = await enumerate_campaigns(client, {1_000, removed_id, missing_id})

    [scan] = fake.queries("Q-G2")
    assert scan.scope == {LAST_ID: INT64_MIN_ID}
    [reread] = fake.queries("Q-G2b")
    assert reread.ids == (removed_id, missing_id)
    assert "status" not in reread.where
    assert enumeration.complete
    assert [row_id(row, "campaign") for row in enumeration.present] == present
    [removed] = enumeration.removed
    parsed = parse_campaign(removed)
    assert (parsed.campaign_id, parsed.status, parsed.raw_status) == (
        removed_id,
        "ended",
        "REMOVED",
    )
    assert enumeration.missing_ids == frozenset({missing_id})
    assert (enumeration.reason, enumeration.interrupted_by) == (None, None)


async def test_incomplete_scan_infers_nothing(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
) -> None:
    """Entity coverage: an incomplete enumeration infers no removal and no disappearance.

    A deferred Q-G2 keyset page leaves the tracked campaigns of the unread pages unknown, so
    Q-G2b is not run; a Q-G2b chunk that keeps failing (HTTP 500 through every retry) leaves
    its ids unknown. Either way ``complete`` is false and ``removed`` and ``missing_ids`` are
    empty, so the previous snapshots stand (AAP 0.3.3 "Safe absence").
    """
    deferred = _FakeGoogleAds()
    ids = [1_000 + index for index in range(12_000)]
    deferred.add_campaigns(ids)
    deferred.add_campaigns([50_000], status="REMOVED")
    budget = _CountingBudget(allow=1)
    async with webhook_client(
        contract_checked_transport("google", deferred.handler), budget=budget
    ) as client:
        cut_short = await enumerate_campaigns(client, {ids[11_000], 50_000, 60_000})
    assert not cut_short.complete
    assert (cut_short.removed, cut_short.missing_ids) == ((), frozenset())
    assert cut_short.reason == f"{REASON_BUDGET}; 1 partition deferred"
    assert isinstance(cut_short.interrupted_by, CallBudgetExhausted)
    assert len(cut_short.present) == KEYSET_LIMIT
    assert deferred.queries("Q-G2b") == []

    failing = _FakeGoogleAds()
    failing.add_campaigns([1_000 + index for index in range(5)])
    failing.add_campaigns([50_000], status="REMOVED")
    failing.hook = lambda call: _workflow_error(call) if call.query_id == "Q-G2b" else None
    async with webhook_client(contract_checked_transport("google", failing.handler)) as client:
        failed = await enumerate_campaigns(client, {1_000, 50_000, 60_000})
    assert not failed.complete
    assert (failed.removed, failed.missing_ids) == ((), frozenset())
    assert failed.reason is not None
    assert failed.reason.startswith(REASON_INTERRUPTED)
    assert isinstance(failed.interrupted_by, TransientError)
    assert len(failing.queries("Q-G2b")) == RETRY.attempts
    assert [row_id(row, "campaign") for row in failed.present] == list(range(1_000, 1_005))


async def _keywords(db: Sessions, dataset_id: int) -> dict[tuple[str, str], int | None]:
    """Map each stored keyword's ``(ad group external id, criterion id)`` to its quality score."""
    async with db() as session:
        result = await session.execute(
            select(AdGroup.external_id, Keyword.criterion_id, Keyword.quality_score)
            .join(AdGroup, AdGroup.id == Keyword.ad_group_id)
            .where(Keyword.dataset_id == dataset_id)
        )
        return {(group, criterion): score for group, criterion, score in result}


async def test_qg7_keywords_without_metrics_get_rows_and_qs_snapshots(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
    db: Sessions,
) -> None:
    """Entity coverage: Q-G7 keywords with no traffic get keyword rows and quality-score snapshots.

    The keywords appear in Q-G7 and in no Q-G5 row: the entity sync reads no metrics, and the
    account has no keyword-view rows. Each still gets a ``keyword`` row and one
    ``entity_snapshot`` (``entity_type='keyword'``) with its quality score, ``None`` where the
    row omits ``qualityInfo``. The ``REMOVED`` keyword Q-G7 excludes is never stored.
    """
    clock = _Clock(T0)
    dataset_id = await begin_live_dataset(db, clock)
    fake = _FakeGoogleAds()
    _entity_account(fake, {300: "ENABLED"})
    fake.add_ad_groups([3_001, 3_002], campaign_id=300)
    fake.add_criteria(3_001, 300, [11], quality_score=7)
    fake.add_criteria(3_001, 300, [12], quality_score=None)
    fake.add_criteria(3_002, 300, [21], quality_score=3)
    fake.add_criteria(3_002, 300, [22], status="REMOVED", quality_score=5)
    result = await run_entity_sync(
        db,
        dataset_id,
        contract_checked_transport("google", fake.handler),
        clock=clock,
        budget=_CountingBudget(allow=None),
    )

    assert result.status == "succeeded"
    assert (await entities_freshness(db, dataset_id)).completeness == "complete"
    assert fake.queries("Q-G5") == []
    assert fake.rows("keyword_days") == []
    expected = {("3001", "11"): 7, ("3001", "12"): None, ("3002", "21"): 3}
    assert await _keywords(db, dataset_id) == expected
    async with db() as session:
        snapshots = await session.execute(
            select(
                AdGroup.external_id,
                Keyword.criterion_id,
                EntitySnapshot.quality_score,
                EntitySnapshot.payload,
            )
            .join(Keyword, Keyword.id == EntitySnapshot.entity_id)
            .join(AdGroup, AdGroup.id == Keyword.ad_group_id)
            .where(
                EntitySnapshot.dataset_id == dataset_id,
                EntitySnapshot.entity_type == "keyword",
            )
        )
        rows = [tuple(row) for row in snapshots]
    assert len(rows) == len(expected)
    assert {(group, criterion): score for group, criterion, score, _payload in rows} == expected
    for _group, _criterion, _score, payload in rows:
        assert (payload["status"], payload["match_type"]) == ("ENABLED", "PHRASE")


async def test_qg8_snapshots_every_conversion_action(
    google_credentials: Mapping[str, str],
    contract_checked_transport: CheckedTransport,
    db: Sessions,
) -> None:
    """Entity coverage: Q-G8 snapshots every conversion action, enabled, hidden and removed.

    Q-G8 has no status filter, because a removed or hidden action may still have contributed to
    past days; each action gets a ``conversion_action`` row and one ``entity_snapshot``
    (``entity_type='conversion_action'``) whose payload carries the counting contract B4 reads:
    ``status``, ``include_in_conversions_metric``, ``counting_type`` and ``attribution_model``,
    an unreported model kept ``None`` (unknown, never last-click).
    """
    clock = _Clock(T0)
    dataset_id = await begin_live_dataset(db, clock)
    fake = _FakeGoogleAds()
    _entity_account(fake, {400: "ENABLED"})
    fake.add_conversion_action(
        701,
        status="ENABLED",
        include=True,
        counting_type="ONE_PER_CLICK",
        attribution_model="GOOGLE_ADS_LAST_CLICK",
    )
    fake.add_conversion_action(
        702,
        status="HIDDEN",
        include=False,
        counting_type="MANY_PER_CLICK",
        attribution_model="GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
    )
    fake.add_conversion_action(
        703, status="REMOVED", include=None, counting_type="ONE_PER_CLICK", attribution_model=None
    )
    result = await run_entity_sync(
        db,
        dataset_id,
        contract_checked_transport("google", fake.handler),
        clock=clock,
        budget=_CountingBudget(allow=None),
    )

    assert result.status == "succeeded"
    [call] = fake.queries("Q-G8")
    assert call.text == QUERIES["Q-G8"].render()
    assert (call.where, QUERIES["Q-G8"].fixed_conditions) == ("", ())
    expected = {
        "701": ("ENABLED", True, "ONE_PER_CLICK", "GOOGLE_ADS_LAST_CLICK"),
        "702": ("HIDDEN", False, "MANY_PER_CLICK", "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"),
        "703": ("REMOVED", False, "ONE_PER_CLICK", None),
    }
    async with db() as session:
        stored = await session.execute(
            select(
                ConversionAction.external_id,
                ConversionAction.status,
                ConversionAction.include_in_conversions_metric,
                ConversionAction.counting_type,
                ConversionAction.attribution_model,
            ).where(ConversionAction.dataset_id == dataset_id)
        )
        actions = {external: tuple(values) for external, *values in stored}
        snapshots = await session.execute(
            select(ConversionAction.external_id, EntitySnapshot.status, EntitySnapshot.payload)
            .join(ConversionAction, ConversionAction.id == EntitySnapshot.entity_id)
            .where(
                EntitySnapshot.dataset_id == dataset_id,
                EntitySnapshot.entity_type == "conversion_action",
            )
        )
        snapshot_rows = [tuple(row) for row in snapshots]
    assert actions == expected
    assert len(snapshot_rows) == len(expected)
    for external, status, payload in snapshot_rows:
        contract_values = (
            payload["status"],
            payload["include_in_conversions_metric"],
            payload["counting_type"],
            payload["attribution_model"],
        )
        assert contract_values == expected[external]
        assert status == expected[external][0]


# ---------------------------------------------------------------------------------------------
# Traceability and redaction (AAP 0.11.2 "External fixtures are traceable"; AAP 0.3.2)
# ---------------------------------------------------------------------------------------------


def _minimal_scope(template: QueryTemplate) -> dict[str, object]:
    """Return the narrowest valid scope of a template: the one its first call would carry."""
    match template.strategy:
        case "keyset":
            return {LAST_ID: INT64_MIN_ID}
        case "by_ids":
            return {IDS: (1,)}
        case "ad_group_ranges":
            return {AD_GROUP_LO: INT64_MIN_ID, AD_GROUP_HI: INT64_MAX}
        case "dated":
            return {START: DAY1, END: DAY1}
        case "single":
            return {}


def _template_for_mask(envelope: object) -> QueryTemplate:
    """Return the first template whose selected fields an envelope's ``fieldMask`` lists."""
    unwrapped = envelope[0] if isinstance(envelope, list) and len(envelope) == 1 else envelope
    assert isinstance(unwrapped, dict), "a documented search sample is an envelope object"
    mask = unwrapped.get("fieldMask")
    assert isinstance(mask, str), "a documented search sample carries its fieldMask"
    for template in QUERIES.values():
        if set(mask.split(",")) == set(field_mask(template).split(",")):
            return template
    raise AssertionError("the documented sample's fieldMask matches no Google template")


def _serve(status: int, body: object, sent: list[httpx.Request]) -> Handler:
    """Return a handler answering every call with ``body`` verbatim and recording the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer it with the fixed body."""
        sent.append(request)
        return httpx.Response(status, json=body)

    return handler


async def test_documented_sample_parses(
    google_credentials: Mapping[str, str],
    contract: Callable[..., Contract],
    contract_checked_transport: CheckedTransport,
) -> None:
    """Traceability: the contracts cite their v25 reference, and documented samples parse.

    ``search.json`` names the official ``GoogleAdsService/Search`` v25 reference, and each row
    contract's GAQL is its template's resource and SELECT list. Every documented sample the
    official pages publish (``search.json`` and each ``row_*.json``) satisfies its own schema
    and is served verbatim through the client and parsed; none is published today, so each
    sample slot is checked to be ``null`` rather than silently skipped.
    """
    search = contract("google", SEARCH_CONTRACT)
    assert (search.source, search.api_version) == ("google", "v25")
    assert "/v25/GoogleAdsService/Search" in search.reference_url
    validate_documented_sample(search)
    samples: list[tuple[QueryTemplate, object]] = []
    if search.response.documented_sample is not None:
        envelope = search.response.documented_sample
        samples.append((_template_for_mask(envelope), envelope))
    absent = 0 if search.response.documented_sample is not None else 1
    for template in QUERIES.values():
        rows = contract("google", template.row_surface)
        validate_documented_sample(rows)
        gaql = rows.request.gaql
        assert gaql is not None, f"{rows.surface} names its GAQL query"
        if gaql.query_id != template.query_id:
            # Q-G2b shares row_campaign with Q-G2, whose GAQL the contract records.
            assert QUERIES[gaql.query_id].select == template.select
            continue
        assert (gaql.resource, gaql.select) == (template.resource, template.select)
        sample = rows.response.documented_sample
        if sample is None:
            absent += 1
            continue
        samples.append(
            (
                template,
                {"results": [sample], "fieldMask": field_mask(template)},
            )
        )
    assert absent + len(samples) == 1 + len(ROW_CONTRACTS)

    for template, body in samples:
        sent: list[httpx.Request] = []
        transport = contract_checked_transport("google", _serve(200, body, sent))
        async with webhook_client(transport) as client:
            page = await client.search_page(
                template.query_id, template.render(**_minimal_scope(template))
            )
        assert len(sent) == 1
        parse = PARSERS[template.row_surface]
        assert [parse(row) for row in page.rows]


async def test_captured_payloads_parse(
    google_credentials: Mapping[str, str],
    captured_payloads: Callable[[str], list[dict[str, Any]]],
    recording_transport: RecordingFactory,
) -> None:
    """Traceability: every redacted live capture validates and replays through the client.

    ``ads-engine capture-fixtures`` writes ``tests/contracts/google/captured/*.json`` once
    credentials work. Each capture is validated by ``payloads.validate_capture`` (request and
    response against ``search.json`` and its row contract); its response is replayed verbatim
    through the client for its captured GAQL, and every row is parsed. No capture exists until
    that command runs, and an empty directory is a pass, not a skip.
    """
    captures = captured_payloads("google")
    replayed = 0
    for capture in captures:
        validate_capture(capture)
        request = capture["request"]
        body = request["body"]
        decoded = json.loads(body) if isinstance(body, str) else body
        assert isinstance(decoded, dict), "a captured webhook body is a JSON object"
        query = decoded["query"]
        assert isinstance(query, str)
        query_id = query_id_for(query)
        assert query_id is not None, "a captured query is one of the GAQL templates"
        response = capture["response"]
        sent: list[httpx.Request] = []
        transport = recording_transport(_serve(int(response["status"]), response["body"], sent))
        async with webhook_client(transport) as client:
            page = await client.search_page(query_id, query)
        contract_used = search_contract()
        for outgoing in sent:
            validate_request(contract_used, outgoing)
        parse = PARSERS[QUERIES[query_id].row_surface]
        for row in page.rows:
            parse(row)
        replayed += 1
    assert replayed == len(captures)


async def test_logs_redact_secret(
    google_credentials: Mapping[str, str],
    captured_logs: LogCapture,
    contract_checked_transport: CheckedTransport,
) -> None:
    """Redaction: no log line carries the secret, the webhook path, the customer id or GAQL.

    One successful call and one refused call, whose HTTP 403 body echoes the secret and the
    webhook URL, are logged as in production (``http.response``, ``google.search``,
    ``connector.error``); the secret and the capability URL's path appear nowhere, nor in the
    raised error (AAP 0.3.2 "Log redaction"; AAP 0.3.3 "Secrets handling").
    """
    captured_logs.configure(settings=get_settings())
    secret = google_credentials["N8N_WEBHOOK_SECRET"]
    url = google_credentials["N8N_GOOGLE_ADS_WEBHOOK_URL"]
    path = httpx.URL(url).path
    echo = {"message": f"Header value {secret} is not accepted at {url}"}
    fake = _FakeGoogleAds()
    fake.add_customer()
    fake.hook = lambda call: httpx.Response(403, json=echo) if call.number == 2 else None
    async with webhook_client(contract_checked_transport("google", fake.handler)) as client:
        await client.run_query("Q-G1")
        with pytest.raises(AccessDenied) as raised:
            await client.run_query("Q-G1")

    assert raised.value.http_status == 403
    assert secret not in str(raised.value)
    assert path not in str(raised.value)
    records = captured_logs()
    events = {record.get("event") for record in records}
    assert {"http.response", "google.search", "connector.error"} <= events
    text = captured_logs.text
    assert secret not in text
    assert path not in text
    assert CUSTOMER_ID not in text
    assert "SELECT " not in text
