"""``GoogleAdsWebhookClient``: one n8n webhook call per GAQL query, metered by the call budget.

Google Ads is reached only through the existing n8n Cloud webhook, exactly as the user prescribed.
Each call is one ``POST {N8N_GOOGLE_ADS_WEBHOOK_URL}`` with the shared-secret header
``{N8N_WEBHOOK_SECRET_HEADER}: {N8N_WEBHOOK_SECRET}``, ``Content-Type: application/json`` and
exactly this body, three keys in this order and no others::

    {"customer_id": "<10 digits, no dashes>", "query": "<GAQL>", "page_token": null}

n8n's already-authenticated Google Ads node runs the query as ``googleAds:search`` v25 and
answers synchronously with ``{results[], fieldMask, queryResourceConsumption}``: REST
``GoogleAdsRow`` objects with camelCase keys and int64 values as strings. The request and the
response follow ``backend/tests/contracts/google/search.json``; the simulator validates requests
against the same file, so a malformed request fails in simulated mode too.

What this module does, and what it leaves to others:

* **One call** (:meth:`GoogleAdsWebhookClient.search_page`): builds the body, sends it through
  :func:`ads_engine.connectors.http.send_json` and validates the envelope. ``send_json`` owns the
  retry policy (3 attempts, 2 s exponential base, jitter, ``Retry-After`` honoured) and the
  error classification, including Google denials, quota exhaustion and query errors that n8n
  relays inside HTTP 200. This module never re-classifies; it only adds envelope validation, so
  a body that is neither a result page nor an error is a ``QueryDefect``, never an empty page.
* **Paging** (:meth:`GoogleAdsWebhookClient.run_query`, :meth:`GoogleAdsWebhookClient.search`):
  renders one template of :data:`~ads_engine.connectors.google_n8n.queries.QUERIES` and follows
  a ``nextPageToken`` at once when one is returned, because page tokens expire after a few
  hours. The specified n8n response carries no token, and nothing here depends on one.
* **Completeness** is ``partition.py``'s job. A last page of exactly
  :data:`~ads_engine.connectors.google_n8n.queries.PAGE_SIZE` (10,000) rows with no token may be
  truncated; :attr:`QueryResult.capped` reports it and ``partition.partitioned_fetch`` narrows
  the query until no partition is capped.
* **Call budget**: every POST attempt (first pages, next pages, partition calls and retries)
  first awaits :meth:`CallBudget.acquire`. ``sync`` injects
  ``ads_engine.sync.budget_guard.GoogleCallBudget`` for live calls and :data:`UNMETERED` for
  simulated ones, so this module never imports ``sync`` and the dependency stays one-way.

Errors leave only as ``AccessDenied``, ``TransientError`` (including ``CallBudgetExhausted``) or
``QueryDefect`` from :mod:`ads_engine.connectors.errors`, plus ``ValueError`` for a programming
error caught before any call (an unknown query id, an invalid scope or configuration).
``ads_engine.sync.mode`` alone decides the consequence of each kind.

The product holds only the webhook URL, the header name and the secret. Google's own client
credentials stay in n8n's credential store, the connector never writes to Google Ads, and no
streaming search variant is used. Logs never carry the secret, the header value, the webhook URL
or its path, the customer id, the GAQL text or an ``IN`` id list (only its length).

Example:
    ``sync`` owns the HTTP client and the budget, and reads one query to its last page::

        async with build_client("google") as http:
            budget = GoogleCallBudget(settings)
            client = GoogleAdsWebhookClient.from_settings(settings, http, budget)
            result = await client.run_query("Q-G1")
            if result.capped:
                ...  # never for Q-G1; partition.py narrows capped queries
            async for row in client.search("Q-G4", {"last_id": 0}):
                ...
"""

import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final, Protocol, Self, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ads_engine.connectors.errors import QueryDefect, unwrap_single_item
from ads_engine.connectors.google_n8n.parser import GoogleAdsRow
from ads_engine.connectors.google_n8n.queries import IDS, PAGE_SIZE, QUERIES
from ads_engine.connectors.http import DEFAULT_RETRY, JsonResponse, RetryPolicy, send_json
from ads_engine.logging import get_logger
from ads_engine.settings import Settings, Source

__all__ = [
    "SIMULATED_SECRET",
    "SIMULATED_WEBHOOK_URL",
    "UNMETERED",
    "CallBudget",
    "GoogleAdsRow",
    "GoogleAdsWebhookClient",
    "QueryResult",
    "SearchPage",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Simulated identity
# ---------------------------------------------------------------------------------------------

SIMULATED_WEBHOOK_URL: Final = "https://n8n.sim.invalid/webhook/google-ads"
"""Webhook URL the client is given when Google is simulated and no live URL is configured.

``sync/mode.py`` passes it with :data:`SIMULATED_SECRET` and :data:`UNMETERED`; the simulation's
``httpx.MockTransport`` answers it without opening a socket. The ``.invalid`` top-level domain is
reserved (RFC 2606), so the URL can never reach a real host."""

# A fixed, publicly documented placeholder sent only to the simulated transport; it unlocks
# nothing, which is why the hardcoded-password rule (S105) does not apply.
SIMULATED_SECRET: Final = "simulated"  # noqa: S105 - simulated identity, not a credential
"""Shared-secret header value sent to the simulated webhook, which checks only its presence."""

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

_SOURCE: Final[Source] = "google"
# The contract surface name of every call (backend/tests/contracts/google/search.json); errors
# and logs name a call as "search {query_id}", for example "search Q-G2".
_SURFACE: Final = "search"
_JSON_CONTENT_TYPE: Final = "application/json"
_HTTP_SCHEMES: Final = frozenset({"http", "https"})

# GOOGLE_ADS_CUSTOMER_ID as the webhook body needs it: exactly 10 ASCII digits, no dashes.
_CUSTOMER_ID_RE: Final = re.compile(r"[0-9]{10}")
# RFC 7230 "token" characters of a header field name, matched with fullmatch.
_HEADER_TOKEN_RE: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Headers the request itself sets or that frame the message; the secret header may not replace
# them. Authorization stays allowed: an n8n Header Auth credential may well be named that.
_RESERVED_HEADERS: Final = frozenset(
    {"content-type", "content-length", "transfer-encoding", "host", "connection"}
)
# A header value that survives transmission unchanged: visible ASCII with inner spaces allowed,
# no control characters (no CR/LF header injection) and no surrounding whitespace, which HTTP
# strips and n8n would then never match.
_SECRET_VALUE_RE: Final = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]*[\x21-\x7e])?")
# A query id labels a call in errors and logs. Restricting it to a short identifier keeps GAQL
# text, which can carry id lists, out of both even when a caller passes the wrong argument.
_QUERY_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# queryResourceConsumption is an int64, sent as a decimal string (proto3 JSON) or a number.
_INT64_TEXT_RE: Final = re.compile(r"[0-9]{1,19}")

# Defensive bound on next-page calls of one query: 1,000 pages is 10 million rows, far beyond
# any query the product issues (partition.py keeps every partition below one page). It stops a
# misbehaving relay that keeps returning fresh tokens, even under the unmetered budget.
_MAX_PAGES_PER_QUERY: Final = 1_000


# ---------------------------------------------------------------------------------------------
# Call budget
# ---------------------------------------------------------------------------------------------


@runtime_checkable
class CallBudget(Protocol):
    """The product's own cap on n8n webhook calls, consulted before every POST attempt.

    ``ads_engine.sync.budget_guard.GoogleCallBudget`` satisfies this protocol structurally and is
    injected by ``sync`` for live calls; :data:`UNMETERED` is injected for simulated calls and in
    tests. Declaring the protocol here, rather than importing ``budget_guard``, keeps the
    dependency one-way: ``sync`` imports connectors, never the reverse.
    """

    async def acquire(self) -> None:
        """Count one webhook POST attempt against the cap, or refuse it.

        The cap is ``GOOGLE_DAILY_CALL_BUDGET`` (1,000 webhook calls per sliding 24 h by
        default), the product's own limit below the Google quota that every n8n workflow using
        the same credential shares. Every attempt counts: first pages, next pages, partition
        calls and retries.

        Raises:
            ads_engine.connectors.errors.CallBudgetExhausted: When the cap is used up. No POST
                is sent, and ``connectors/http.py`` never retries the refusal; the remaining
                pages are deferred to a later window.
        """


class _Unmetered:
    """A :class:`CallBudget` that admits every call and counts nothing.

    Used when Google is simulated, because a simulated n8n call is not a live Google call and
    must never consume ``GOOGLE_DAILY_CALL_BUDGET``, and by tests.
    """

    __slots__ = ()

    async def acquire(self) -> None:
        """Admit the call: an unmetered budget never refuses and records nothing."""
        return

    def __repr__(self) -> str:
        """Return ``UNMETERED``, the name the instance is exported under."""
        return "UNMETERED"


UNMETERED: Final[CallBudget] = _Unmetered()
"""The call budget of simulated runs and tests: every call is admitted and nothing is counted."""


# ---------------------------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One webhook response: one page of one GAQL query.

    Attributes:
        rows: The page's REST ``GoogleAdsRow`` objects, in the order returned; at most
            :data:`~ads_engine.connectors.google_n8n.queries.PAGE_SIZE` (10,000). Empty when the
            response carried ``fieldMask`` without ``results`` (Google omits an empty repeated
            field).
        next_page_token: The ``nextPageToken`` to send back at once as ``page_token``, or
            ``None`` when the response carried none (always, for the specified n8n response).
        field_mask: The comma-joined camelCase paths of the selected fields, or ``None`` when
            the response omitted them.
    """

    rows: tuple[GoogleAdsRow, ...]
    next_page_token: str | None
    field_mask: str | None

    @property
    def capped(self) -> bool:
        """Whether the page may be truncated: exactly ``PAGE_SIZE`` rows and no page token."""
        return self.next_page_token is None and len(self.rows) == PAGE_SIZE


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Every page of one GAQL query, read to its last page.

    Attributes:
        query_id: The template id, such as ``"Q-G2"``.
        rows: The rows of every page, in page order and, within a page, in the order returned.
        pages: Pages read, the first included; at least 1.
        calls: POST attempts made for this query: one per page plus every retry. Each was
            counted against the call budget.
        capped: Whether the last page held exactly ``PAGE_SIZE`` (10,000) rows and carried no
            page token. Such a response may be truncated, and the caller must partition the
            query (``partition.partitioned_fetch``) instead of trusting it as complete.
    """

    query_id: str
    rows: tuple[GoogleAdsRow, ...]
    pages: int
    calls: int
    capped: bool


@dataclass(slots=True)
class _CallContext:
    """Mutable bookkeeping of one query's webhook calls.

    One context per query keeps per-query counts exact when several queries share one client
    concurrently; the client's lifetime total is kept separately.

    Attributes:
        query_id: The template id that labels the calls in errors and logs.
        page: The ordinal of the page being read, the first being 1; ``None`` for a single
            :meth:`GoogleAdsWebhookClient.search_page` continuation whose ordinal only the
            caller knows.
        ids_count: Length of the scope's ``IN`` id list, logged instead of the ids themselves.
        attempts: POST attempts made so far for this query, retries included.
    """

    query_id: str
    page: int | None
    ids_count: int | None = None
    attempts: int = 0


# ---------------------------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------------------------


class GoogleAdsWebhookClient:
    """Reads Google Ads through the n8n webhook: one POST per GAQL query page.

    The client is stateless apart from its lifetime call count, so one instance may serve
    concurrent queries. It never opens or closes its ``httpx.AsyncClient``: the caller builds it
    with ``connectors.http.build_client("google", transport)`` (no base URL, the 120 s per-call
    deadline, redacting logging hooks) and owns its lifecycle. The transport is the only thing
    that differs between live and simulated runs.

    Example:
        A simulated client, as ``sync/mode.py`` builds it::

            client = GoogleAdsWebhookClient(
                http=http,
                budget=UNMETERED,
                webhook_url=SIMULATED_WEBHOOK_URL,
                secret_header=settings.n8n_webhook_secret_header,
                secret=SecretStr(SIMULATED_SECRET),
                customer_id="1234567890",
            )
            page = await client.search_page("Q-G1", QUERIES["Q-G1"].render())
    """

    __slots__ = (
        "_budget",
        "_calls_made",
        "_customer_id",
        "_http",
        "_retry",
        "_secret",
        "_secret_header",
        "_webhook_url",
    )

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        budget: CallBudget,
        webhook_url: str,
        secret_header: str,
        secret: SecretStr,
        customer_id: str,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        """Configure the client; nothing is sent until the first query.

        Args:
            http: The Google client from ``connectors.http.build_client("google", ...)``.
            budget: Consulted before every POST attempt: ``GoogleCallBudget`` for live calls,
                :data:`UNMETERED` for simulated calls and tests.
            webhook_url: The absolute ``http://`` or ``https://`` n8n webhook URL
                (``N8N_GOOGLE_ADS_WEBHOOK_URL``, or :data:`SIMULATED_WEBHOOK_URL`). Surrounding
                whitespace is ignored.
            secret_header: The header name n8n's Header Auth credential expects
                (``N8N_WEBHOOK_SECRET_HEADER``). Surrounding whitespace is ignored.
            secret: The shared secret sent in that header (``N8N_WEBHOOK_SECRET``).
            customer_id: The Google Ads customer id, exactly 10 digits with no dashes
                (``GOOGLE_ADS_CUSTOMER_ID``, already normalised by settings).
            retry: The retry policy of every call; the plan's policy by default.

        Raises:
            ValueError: When ``customer_id`` is not 10 digits, ``webhook_url`` is not an
                absolute ``http(s)`` URL with a host, ``secret_header`` is not an HTTP header
                name or names a header the request itself sets (``Content-Type``,
                ``Content-Length``, ``Transfer-Encoding``, ``Host``, ``Connection``), or
                ``secret`` is empty, holds control or non-ASCII characters, or has surrounding
                whitespace. The message never contains the URL, the secret or the customer id.
        """
        self._http: httpx.AsyncClient = http
        self._budget: CallBudget = budget
        self._webhook_url: str = _checked_webhook_url(webhook_url)
        self._secret_header: str = _checked_secret_header(secret_header)
        self._secret: SecretStr = _checked_secret(secret)
        self._customer_id: str = _checked_customer_id(customer_id)
        self._retry: RetryPolicy = retry
        self._calls_made: int = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient,
        budget: CallBudget,
        *,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> Self:
        """Build the live client from the configured webhook credentials.

        Reads ``N8N_GOOGLE_ADS_WEBHOOK_URL``, ``N8N_WEBHOOK_SECRET_HEADER`` (the plan's default
        is ``X-Webhook-Secret``, because the name on n8n's Header Auth credential is not
        stated), ``N8N_WEBHOOK_SECRET`` and ``GOOGLE_ADS_CUSTOMER_ID``. Mode resolution sends a
        source without them to simulated data first, so a missing value here is a programming
        error rather than a fallback.

        Args:
            settings: The process settings.
            http: The Google client from ``connectors.http.build_client("google")``.
            budget: The live call budget, ``GoogleCallBudget(settings)``.
            retry: The retry policy of every call; the plan's policy by default.

        Returns:
            A client posting to the configured webhook.

        Raises:
            ValueError: When the webhook URL, the secret or the customer id is unset (the
                message names the missing environment variables, never a value), or a value
                fails the checks of :meth:`__init__`.
        """
        webhook_url = settings.n8n_google_ads_webhook_url
        secret = settings.n8n_webhook_secret
        customer_id = settings.google_ads_customer_id
        if webhook_url is None or secret is None or customer_id is None:
            missing = [
                name
                for name, value in (
                    ("N8N_GOOGLE_ADS_WEBHOOK_URL", webhook_url),
                    ("N8N_WEBHOOK_SECRET", secret),
                    ("GOOGLE_ADS_CUSTOMER_ID", customer_id),
                )
                if value is None
            ]
            raise ValueError("the Google Ads webhook is not configured; set " + ", ".join(missing))
        return cls(
            http=http,
            budget=budget,
            webhook_url=webhook_url,
            secret_header=settings.n8n_webhook_secret_header,
            secret=secret,
            customer_id=customer_id,
            retry=retry,
        )

    @property
    def calls_made(self) -> int:
        """POST attempts made over the client's lifetime, retries and next pages included.

        Every attempt was admitted by the call budget first. ``sync`` logs the total, and
        ``partition.partitioned_fetch`` derives a fetch's calls from the difference before and
        after it.
        """
        return self._calls_made

    def __repr__(self) -> str:
        """Return the class name and the call count; never the URL, secret or customer id."""
        return f"{type(self).__name__}(calls_made={self._calls_made})"

    # -- one webhook call ---------------------------------------------------------------------

    async def search_page(
        self, query_id: str, query: str, page_token: str | None = None
    ) -> SearchPage:
        """Send one webhook call: one page of one GAQL query.

        The body is exactly ``{"customer_id", "query", "page_token"}`` in that order, serialised
        once with :func:`json.dumps` and sent as raw content, so its bytes are deterministic.
        Before every attempt, retries included, the call budget's ``acquire`` is awaited and
        the call counters are incremented. Classification of the response, including Google
        errors that n8n relays inside HTTP 200, is ``send_json``'s.

        Args:
            query_id: The template id that labels the call in errors and logs, such as
                ``"Q-G2"``: a short identifier, never GAQL text.
            query: The GAQL text, normally ``QUERIES[query_id].render(...)``; never logged.
            page_token: ``None`` for a first page; otherwise the previous page's
                ``nextPageToken``, sent back at once because tokens expire after a few hours.

        Returns:
            The validated page.

        Raises:
            AccessDenied: The webhook or Google refused the credentials, including a Google
                ``PERMISSION_DENIED``, ``UNAUTHENTICATED``, ``AuthorizationError`` or
                ``AuthenticationError`` inside an HTTP 200 from n8n.
            TransientError: HTTP 429 or 5xx, a timeout, a network failure or Google
                ``RESOURCE_EXHAUSTED`` that survived every retry.
            CallBudgetExhausted: The call budget refused an attempt; that attempt sent no POST.
            QueryDefect: The request was rejected as malformed (HTTP 400, a GAQL
                ``QueryError``), or the response is not a valid result envelope: not a JSON
                object, neither ``results`` nor ``fieldMask``, ``results`` not a list of
                objects, more than 10,000 rows, or a non-string ``fieldMask`` or
                ``nextPageToken``. It always carries ``query_id``.
            ValueError: When ``query_id`` is not a short identifier, ``query`` is blank or
                ``page_token`` is an empty string; nothing is sent.
        """
        context = _CallContext(
            query_id=_checked_query_id(query_id), page=1 if page_token is None else None
        )
        return await self._fetch_page(query, page_token, context)

    async def _fetch_page(
        self, query: str, page_token: str | None, context: _CallContext
    ) -> SearchPage:
        """Send one webhook call for ``context`` and validate its envelope.

        Args:
            query: The GAQL text.
            page_token: ``None`` for a first page, else the token to send back.
            context: The query's bookkeeping; its ``attempts`` grows by every POST attempt.

        Returns:
            The validated page.

        Raises:
            AccessDenied: As :meth:`search_page`.
            TransientError: As :meth:`search_page`.
            CallBudgetExhausted: As :meth:`search_page`.
            QueryDefect: As :meth:`search_page`.
            ValueError: When ``query`` is blank or ``page_token`` is an empty string.
        """
        if not query.strip():
            raise ValueError("query must be a non-empty GAQL string")
        if page_token is not None and not page_token:
            raise ValueError("page_token must be None or a non-empty nextPageToken")
        endpoint = f"{_SURFACE} {context.query_id}"
        # Serialised once: every attempt sends the same bytes, keys in the contract's order.
        content = json.dumps(
            {"customer_id": self._customer_id, "query": query, "page_token": page_token}
        ).encode("utf-8")
        attempts_before = context.attempts

        def build_request() -> httpx.Request:
            """Build the POST of one attempt; the secret is read only here."""
            return self._http.build_request(
                "POST",
                self._webhook_url,
                content=content,
                headers={
                    self._secret_header: self._secret.get_secret_value(),
                    "Content-Type": _JSON_CONTENT_TYPE,
                },
            )

        async def before_attempt() -> None:
            """Admit the attempt through the call budget, then count it."""
            await self._budget.acquire()
            self._calls_made += 1
            context.attempts += 1

        try:
            response: JsonResponse = await send_json(
                self._http,
                build_request,
                source=_SOURCE,
                endpoint=endpoint,
                policy=self._retry,
                before_attempt=before_attempt,
            )
        except QueryDefect as defect:
            if defect.query_id is not None:
                raise
            # Every defect leaving the client names its query, so the failed job is logged
            # with the query id even when the endpoint label could not supply it.
            raise QueryDefect(
                defect.message,
                source=defect.source,
                endpoint=defect.endpoint,
                code=defect.code,
                http_status=defect.http_status,
                query_id=context.query_id,
            ) from defect

        page, resource_units = _read_envelope(
            response.body, query_id=context.query_id, http_status=response.status
        )
        fields: dict[str, object] = {
            "source": _SOURCE,
            "query_id": context.query_id,
            "page": context.page,
            "rows": len(page.rows),
            "capped": page.capped,
            "has_next_page": page.next_page_token is not None,
            "resource_consumption": resource_units,
            "attempts": context.attempts - attempts_before,
            "calls_made": self._calls_made,
        }
        if context.ids_count is not None:
            fields["ids_count"] = context.ids_count
        log.info("google.search", **fields)
        return page

    # -- paging -------------------------------------------------------------------------------

    async def run_query(
        self, query_id: str, scope: Mapping[str, object] | None = None
    ) -> QueryResult:
        """Read one GAQL query to its last page.

        Renders ``QUERIES[query_id].render(**scope)``, sends the first page with
        ``page_token=None`` and, while a page returns a ``nextPageToken``, sends it back at once.
        The rows of a query that fails are discarded: the caller re-reads the whole query,
        because page tokens expire. Completeness of a capped result is the caller's to
        establish, through ``partition.partitioned_fetch``.

        Args:
            query_id: A key of :data:`~ads_engine.connectors.google_n8n.queries.QUERIES`, such
                as ``"Q-G2"``.
            scope: Narrowing values for the template's ``render``, keyed by the scope-key
                constants of ``queries.py`` (``last_id``, ``ids``, ``start``, ``end``, id
                ranges); ``None`` for none.

        Returns:
            Every row in page order, the page count, the POST attempts made and whether the
            last page was capped.

        Raises:
            AccessDenied: As :meth:`search_page`, unchanged.
            TransientError: As :meth:`search_page`, unchanged.
            CallBudgetExhausted: As :meth:`search_page`, unchanged.
            QueryDefect: As :meth:`search_page`; also when a page token is returned twice in
                one query, or the query runs past 1,000 pages.
            ValueError: When ``query_id`` is unknown or ``scope`` is invalid for its template;
                nothing is sent.
        """
        query, context = _prepare(query_id, scope)
        rows: list[GoogleAdsRow] = []
        pages = 0
        capped = False
        async with aclosing(self._pages(query, context)) as stream:
            async for page in stream:
                rows.extend(page.rows)
                pages += 1
                capped = page.capped
        return QueryResult(
            query_id=context.query_id,
            rows=tuple(rows),
            pages=pages,
            calls=context.attempts,
            capped=capped,
        )

    async def search(
        self, query_id: str, scope: Mapping[str, object] | None = None
    ) -> AsyncIterator[GoogleAdsRow]:
        """Stream the rows of one GAQL query, page by page.

        The same paging loop as :meth:`run_query`, yielding each page's rows as soon as the page
        is validated. It does not partition and reports no completeness;
        ``partition.partitioned_fetch`` is the completeness-aware entry point that ``sync``
        uses. Validation and the first call happen on the first iteration.

        Args:
            query_id: A key of :data:`~ads_engine.connectors.google_n8n.queries.QUERIES`.
            scope: Narrowing values for the template's ``render``; ``None`` for none.

        Yields:
            Each REST ``GoogleAdsRow``, in page order.

        Raises:
            AccessDenied: As :meth:`run_query`.
            TransientError: As :meth:`run_query`.
            CallBudgetExhausted: As :meth:`run_query`.
            QueryDefect: As :meth:`run_query`.
            ValueError: As :meth:`run_query`.
        """
        query, context = _prepare(query_id, scope)
        async with aclosing(self._pages(query, context)) as stream:
            async for page in stream:
                for row in page.rows:
                    yield row

    async def _pages(self, query: str, context: _CallContext) -> AsyncGenerator[SearchPage]:
        """Yield every page of ``query``, following each returned token at once.

        A page's token is checked before the page is yielded, so a relay that repeats a token
        fails the query before any row of the looping page is handed out.

        Args:
            query: The rendered GAQL text.
            context: The query's bookkeeping; ``page`` is set to each page's ordinal.

        Yields:
            Each validated page, the first included.

        Raises:
            QueryDefect: When a token repeats or the query runs past 1,000 pages, besides every
                error of :meth:`search_page`.
        """
        seen_tokens: set[str] = set()
        page_token: str | None = None
        page_number = 0
        while True:
            if page_number >= _MAX_PAGES_PER_QUERY:
                raise _defect(
                    f"query returned page tokens beyond {_MAX_PAGES_PER_QUERY} pages",
                    query_id=context.query_id,
                    http_status=None,
                )
            page_number += 1
            context.page = page_number
            page = await self._fetch_page(query, page_token, context)
            next_token = page.next_page_token
            if next_token is not None:
                if next_token in seen_tokens:
                    raise _defect(
                        "page token repeated", query_id=context.query_id, http_status=None
                    )
                seen_tokens.add(next_token)
            yield page
            if next_token is None:
                return
            page_token = next_token


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _prepare(query_id: str, scope: Mapping[str, object] | None) -> tuple[str, _CallContext]:
    """Render the template of ``query_id`` for ``scope`` and open the query's bookkeeping.

    Raises:
        ValueError: When ``query_id`` is not a template id or ``scope`` fails its ``render``.
    """
    try:
        template = QUERIES[query_id]
    except KeyError:
        # Echo the id only when it looks like one: a caller passing GAQL text by mistake must
        # not put that text, and the id lists it can carry, into an error message.
        shown = repr(query_id) if _QUERY_ID_RE.fullmatch(query_id) else "(not an identifier)"
        raise ValueError(
            f"unknown Google Ads query id {shown}; expected one of {', '.join(QUERIES)}"
        ) from None
    narrowing: Mapping[str, object] = scope if scope is not None else {}
    query = template.render(**narrowing)
    return query, _CallContext(
        query_id=template.query_id, page=None, ids_count=_ids_count(narrowing)
    )


def _ids_count(scope: Mapping[str, object]) -> int | None:
    """Return the length of the scope's ``IN`` id list, or ``None`` without one."""
    ids = scope.get(IDS)
    if isinstance(ids, Collection) and not isinstance(ids, str | bytes):
        return len(ids)
    return None


def _defect(message: str, *, query_id: str, http_status: int | None) -> QueryDefect:
    """Build the validation ``QueryDefect`` of a call labelled ``query_id``, and log it.

    ``send_json`` logs the errors it raises; envelope and paging defects are raised here, after
    it returned, so they are logged here, with the same sanitised fields.
    """
    defect = QueryDefect(
        message,
        source=_SOURCE,
        endpoint=f"{_SURFACE} {query_id}",
        code="validation",
        http_status=http_status,
        query_id=query_id,
    )
    log.warning("google.search_rejected", **defect.log_fields)
    return defect


def _read_envelope(
    body: object, *, query_id: str, http_status: int
) -> tuple[SearchPage, int | None]:
    """Validate a webhook response body and turn it into a page.

    ``send_json`` has already raised for every error body it recognises, so what reaches this
    function is either a result envelope or something unrecognised. An unrecognised body is a
    defect, never an empty page: an empty Q-G2 page would let ``sync`` infer false removals.

    Args:
        body: The decoded JSON body, possibly n8n's one-element array wrapping the envelope.
        query_id: The template id, for errors.
        http_status: The accepted HTTP status, for errors.

    Returns:
        The page, and the ``queryResourceConsumption`` units (``None`` when absent or not an
        int64), which are only logged.

    Raises:
        QueryDefect: With code ``validation`` when the body is not a JSON object, carries
            neither ``results`` nor ``fieldMask``, has ``results`` that is not a list of objects
            or holds more than 10,000 rows, or has a ``fieldMask`` or ``nextPageToken`` that is
            not a string. A ``null`` value counts as absent.
    """
    envelope = unwrap_single_item(body)
    if not isinstance(envelope, Mapping):
        raise _defect(
            "webhook response is not a JSON object", query_id=query_id, http_status=http_status
        )
    raw_results = envelope.get("results")
    raw_mask = envelope.get("fieldMask")
    if raw_results is None and raw_mask is None:
        raise _defect(
            "webhook response carries neither results nor fieldMask",
            query_id=query_id,
            http_status=http_status,
        )
    if raw_mask is not None and not isinstance(raw_mask, str):
        raise _defect(
            "webhook response fieldMask is not a string", query_id=query_id, http_status=http_status
        )
    rows: list[GoogleAdsRow] = []
    if raw_results is not None:
        if not isinstance(raw_results, list):
            raise _defect(
                "webhook response results is not a JSON array",
                query_id=query_id,
                http_status=http_status,
            )
        if len(raw_results) > PAGE_SIZE:
            raise _defect(
                f"webhook response holds {len(raw_results)} rows, more than the "
                f"{PAGE_SIZE}-row page size",
                query_id=query_id,
                http_status=http_status,
            )
        for index, row in enumerate(raw_results):
            if not isinstance(row, dict):
                raise _defect(
                    f"webhook response results[{index}] is not a JSON object",
                    query_id=query_id,
                    http_status=http_status,
                )
            rows.append(row)
    raw_token = envelope.get("nextPageToken")
    if raw_token is not None and not isinstance(raw_token, str):
        raise _defect(
            "webhook response nextPageToken is not a string",
            query_id=query_id,
            http_status=http_status,
        )
    page = SearchPage(rows=tuple(rows), next_page_token=raw_token or None, field_mask=raw_mask)
    return page, _resource_units(envelope.get("queryResourceConsumption"))


def _resource_units(value: object) -> int | None:
    """Read ``queryResourceConsumption``: an int64 as a decimal string or a JSON integer."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and _INT64_TEXT_RE.fullmatch(value):
        return int(value)
    return None


def _checked_query_id(query_id: str) -> str:
    """Return ``query_id`` when it is a short identifier such as ``Q-G2``.

    Raises:
        ValueError: Otherwise; the value is not echoed, because it could be GAQL text.
    """
    if _QUERY_ID_RE.fullmatch(query_id) is None:
        raise ValueError(
            "query_id must be a short identifier such as 'Q-G2' (letters, digits, '_', '.', "
            "'-'; at most 64 characters), never GAQL text"
        )
    return query_id


def _checked_customer_id(customer_id: str) -> str:
    """Return ``customer_id`` when it is exactly 10 ASCII digits.

    Raises:
        ValueError: Otherwise; the value is not echoed.
    """
    if _CUSTOMER_ID_RE.fullmatch(customer_id) is None:
        raise ValueError("customer_id must be exactly 10 digits with no dashes")
    return customer_id


def _checked_webhook_url(webhook_url: str) -> str:
    """Return the stripped ``webhook_url`` when it is an absolute ``http(s)`` URL with a host.

    Raises:
        ValueError: Otherwise. The message never contains the URL, and the parser's own error,
            which could quote part of it, is suppressed rather than chained.
    """
    message = "webhook_url must be an absolute http:// or https:// URL with a host"
    candidate = webhook_url.strip()
    if not candidate or any(char.isspace() or not char.isprintable() for char in candidate):
        raise ValueError(message)
    try:
        parts = urlsplit(candidate)
        port = parts.port  # reading .port raises ValueError for a malformed port
        httpx.URL(candidate)
    except ValueError, httpx.InvalidURL:
        raise ValueError(message) from None
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname or port == 0:
        raise ValueError(message)
    return candidate


def _checked_secret_header(secret_header: str) -> str:
    """Return the stripped header name when it is a token the request does not set itself.

    Raises:
        ValueError: When the name has characters outside the RFC 7230 token set, or is one of
            ``Content-Type``, ``Content-Length``, ``Transfer-Encoding``, ``Host`` or
            ``Connection`` (in any case).
    """
    name = secret_header.strip()
    if _HEADER_TOKEN_RE.fullmatch(name) is None:
        raise ValueError(
            "secret_header must be an HTTP header name made of RFC 7230 token characters, "
            "such as X-Webhook-Secret"
        )
    if name.lower() in _RESERVED_HEADERS:
        raise ValueError(
            "secret_header must not be Content-Type, Content-Length, Transfer-Encoding, Host "
            "or Connection, which the request itself sets"
        )
    return name


def _checked_secret(secret: SecretStr) -> SecretStr:
    """Return ``secret`` when its value can be sent unchanged as a header value.

    Raises:
        ValueError: When the value is empty, holds a control or non-ASCII character, or starts
            or ends with whitespace. The value is never echoed.
    """
    if _SECRET_VALUE_RE.fullmatch(secret.get_secret_value()) is None:
        raise ValueError(
            "secret must be non-empty visible ASCII text without control characters or "
            "surrounding whitespace"
        )
    return secret
