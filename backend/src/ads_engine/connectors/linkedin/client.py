"""Read-only LinkedIn Marketing API client (``Linkedin-Version: 202609``, Rest.li 2.0).

:class:`LinkedInClient` issues every LinkedIn call the product makes (AAP 0.3.3, "LinkedIn
Marketing API"): the ad account probe, campaign groups, campaigns and creatives in every
status, the posts creatives reference, daily creative analytics, preset-window demographic
segments, lead forms, lead form responses and the transient batch re-fetch of responses by
id. It turns each response into the typed rows of :mod:`ads_engine.connectors.linkedin.parser`
and never writes to LinkedIn (AAP 0.7.2).

Request construction:
    Every URL is the path relative to ``https://api.linkedin.com/rest/`` (no leading slash)
    plus a query built by :func:`restli_query`. Rest.li 2.0 values are assembled from
    :func:`encode_urn`, :func:`restli_list`, :func:`restli_date` and
    :func:`restli_date_range`: the structural characters ``( ) : ,`` stay literal and only
    the URNs, ids and page tokens inside them are percent-encoded, exactly once. httpx keeps
    already-valid query bytes unchanged, so ``request.url.query`` equals the built string
    byte for byte.

Contract fidelity (``backend/tests/contracts/linkedin/*.json``):
    Each request satisfies its contract file: path, required query keys, ``fields``
    projection and headers. The simulation's mock transport validates requests against the
    same files, so live and simulated calls take one code path and this module never
    branches on the data mode. Where a contract records live 202609 behaviour that differs
    from the plan text, the contract is followed and the call site says so. The Lead Sync
    projection :data:`LEAD_FIELDS` is no literal of this module:
    :mod:`ads_engine.connectors.linkedin.projections` generates it from the ``request.fields``
    of the ``lead_form_responses`` and ``lead_form_responses_batch`` contracts into the
    packaged ``_projections_generated`` module, and a unit test fails while that module is
    stale. Importing this module therefore reads no file and works from any working directory.

Errors:
    Every call goes through :func:`ads_engine.connectors.http.send_json`, which owns the
    retry policy (3 attempts, 2 s exponential base, jitter, ``Retry-After``), classification
    and redacted request logging. This module raises only
    :class:`~ads_engine.connectors.errors.AccessDenied`,
    :class:`~ads_engine.connectors.errors.TransientError` and
    :class:`~ads_engine.connectors.errors.QueryDefect` (plus ``ValueError`` for an invalid
    argument), and ``ads_engine.sync.mode`` alone decides what follows from them. A
    ``TransientError`` that survives the retries inside a listing ends that listing early:
    the rows already read are returned with ``complete=False`` and ``interrupted_by`` set.

Personal data:
    Lead response bodies carry emails. This module never logs a body, element, id list,
    email or token, nor places one in an exception message; :class:`LeadBatch` hides its
    elements from ``repr``; its log events carry counts only. Request URLs are logged only
    by the ``connectors/http.py`` hooks, through ``redact_url``.

Example:
    The worker owns the HTTP client and builds one LinkedIn client per job::

        async with build_client("linkedin") as http:
            client = LinkedInClient(http, LinkedInTokenProvider(get_settings(), http))
            account = await client.get_ad_account("123456")
            campaigns = await client.list_campaigns("123456", account_currency=account.currency)
            if campaigns.complete:
                ...  # only a complete listing may drive `missing` discrepancies
"""

import re
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Final
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import SecretStr

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.http import DEFAULT_RETRY, JsonResponse, RetryPolicy, send_json
from ads_engine.connectors.linkedin._projections_generated import (
    LEAD_FIELDS as _GENERATED_LEAD_FIELDS,
)
from ads_engine.connectors.linkedin.auth import HoldsSecrets, TokenSource
from ads_engine.connectors.linkedin.parser import (
    CAMPAIGN_GROUP_SEARCH_STATUSES,
    CAMPAIGN_SEARCH_STATUSES,
    CREATIVE_INTENDED_STATUSES,
    PREFIX_ACCOUNT,
    PREFIX_CAMPAIGN,
    SEGMENT_PIVOTS,
    AdAccountRow,
    CampaignGroupRow,
    CampaignRow,
    CreativeDailyRow,
    CreativeRow,
    FormVersionMap,
    KnownVersions,
    LeadFormRow,
    ParsedLead,
    PostRow,
    SegmentRow,
    parse_ad_account,
    parse_analytics_daily,
    parse_campaign,
    parse_campaign_group,
    parse_creative,
    parse_lead,
    parse_lead_form,
    parse_post,
    parse_segment,
    sponsored_account_urn,
    sponsored_campaign_urn,
    urn_id,
)
from ads_engine.logging import get_logger

__all__ = [
    "CREATIVE_CAMPAIGN_CHUNK",
    "DAILY_FIELDS",
    "ENTITY_PAGE_SIZE",
    "LEAD_BATCH_SIZE",
    "LEAD_FIELDS",
    "LEAD_PAGE_SIZE",
    "LINKEDIN_VERSION",
    "MAX_ANALYTICS_ELEMENTS",
    "POST_BATCH_SIZE",
    "RESTLI_PROTOCOL_VERSION",
    "SEGMENT_DISCOVERY_FIELDS",
    "SEGMENT_FIELDS",
    "SOURCE",
    "LeadBatch",
    "LeadListing",
    "LinkedInClient",
    "Listing",
    "PostBatch",
    "encode_urn",
    "restli_date",
    "restli_date_range",
    "restli_list",
    "restli_query",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LINKEDIN_VERSION: Final = "202609"
"""Marketing API version sent as ``Linkedin-Version`` on every request (AAP 0.1.3)."""

RESTLI_PROTOCOL_VERSION: Final = "2.0.0"
"""Rest.li protocol sent as ``X-Restli-Protocol-Version``; it fixes the query syntax used here."""

SOURCE: Final = "linkedin"
"""Source name of every call, in errors and logs."""

ENTITY_PAGE_SIZE: Final = 100
"""``pageSize`` of campaign-group, campaign and creative searches (creatives allow at most 100)."""

CREATIVE_CAMPAIGN_CHUNK: Final = 100
"""Most campaign URNs sent in one ``creatives?q=criteria&campaigns=List(...)`` request."""

POST_BATCH_SIZE: Final = 50
"""Most post URNs per ``posts?ids=List(...)`` batch get.

The Posts API documents no maximum; 50 encoded URNs keep the request URL well below common
URL-length limits, so the call never needs query tunnelling.
"""

LEAD_PAGE_SIZE: Final = 100
"""``count`` of lead-form and lead-response pages, the documented page size."""

LEAD_BATCH_SIZE: Final = 100
"""Most response ids per batch re-fetch. LinkedIn documents 1,000; AAP 0.3.3 chooses 100."""

MAX_ANALYTICS_ELEMENTS: Final = 15_000
"""Most elements one ``adAnalytics`` response returns; a response at the cap may be truncated."""

LEAD_FIELDS: Final[str] = _GENERATED_LEAD_FIELDS
"""Lead Sync response projection, generated from the lead-response contracts.

It is the ``request.fields`` that ``lead_form_responses.json`` and
``lead_form_responses_batch.json`` both record: the projection of LinkedIn's Lead Sync API
migration guide, verbatim (AAP 0.2.4, 0.3.3). It decorates each response with the ``form``
built from the response's own ``versionedLeadGenFormUrn``.
:mod:`ads_engine.connectors.linkedin.projections` writes it into the packaged
``_projections_generated`` module, so importing this module reads no file. The simulator
validates requests against the same contracts, and a unit test fails while the generated value
differs from them, so live and simulated calls send one projection. Its commas, colon and
parentheses are sent raw: see :data:`_LEAD_FIELDS_QUERY_VALUE`.
"""

DAILY_FIELDS: Final = (
    "pivotValues,dateRange,costInLocalCurrency,impressions,clicks,landingPageClicks,"
    "oneClickLeadFormOpens,oneClickLeads,externalWebsiteConversions"
)
"""Projection of daily creative analytics: 9 fields, under the 20-field limit; sent raw."""

SEGMENT_FIELDS: Final = (
    "pivotValues,costInLocalCurrency,impressions,clicks,oneClickLeads,externalWebsiteConversions"
)
"""Projection of demographic segment statistics; sent raw."""

SEGMENT_DISCOVERY_FIELDS: Final = "pivotValues,impressions"
"""Projection of the segment fallback's campaign-discovery call (see ``analytics_segments``)."""

# The lead_form_responses and lead_form_responses_batch contracts send the projection raw, not
# percent-encoded as one value: RFC 3986 allows ',', ':', '(' and ')' in a query, so the raw
# value already is its single URL encoding (decoding it once returns LEAD_FIELDS exactly), and
# live 202609 rejects the fully encoded value with HTTP 400 "Projected field ... not present in
# schema". The contract wins over the plan text "quote(LEAD_FIELDS, safe='')".
_LEAD_FIELDS_QUERY_VALUE: Final = LEAD_FIELDS

_BASE_PATH: Final = "/rest/"
_API_HOST: Final = "api.linkedin.com"

# Contract surface names (backend/tests/contracts/linkedin/{surface}.json).
_SURFACE_AD_ACCOUNT: Final = "ad_account"
_SURFACE_CAMPAIGN_GROUPS: Final = "ad_campaign_groups"
_SURFACE_CAMPAIGNS: Final = "ad_campaigns"
_SURFACE_CREATIVES: Final = "creatives"
_SURFACE_POSTS: Final = "posts"
_SURFACE_ANALYTICS: Final = "ad_analytics"
_SURFACE_STATISTICS: Final = "ad_analytics_statistics"
_SURFACE_CAMPAIGN_DELIVERY: Final = "ad_analytics_campaign_delivery"
_SURFACE_MEMBER_SEGMENTS: Final = "ad_analytics_member_segments"
_SURFACE_LEAD_FORMS: Final = "lead_forms"
_SURFACE_LEAD_RESPONSES: Final = "lead_form_responses"
_SURFACE_LEAD_RESPONSES_BATCH: Final = "lead_form_responses_batch"

_POST_PREFIXES: Final = ("urn:li:share:", "urn:li:ugcPost:")

# Characters allowed in a key or value of restli_query: RFC 3986 unreserved characters, the
# Rest.li structural characters, '*' and '%' (only as the start of a %XX escape).
_QUERY_TEXT_RE: Final = re.compile(r"[A-Za-z0-9\-._~():,*%]+")
_PERCENT_ESCAPE_RE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DIGITS_RE: Final = re.compile(r"[0-9]+")
# A next-link path relative to the client base: plain path segments, never '..'.
_RELATIVE_PATH_RE: Final = re.compile(r"[A-Za-z0-9\-._~]+(?:/[A-Za-z0-9\-._~]+)*")

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_DAY: Final = timedelta(days=1)
_ONE_MS: Final = timedelta(milliseconds=1)

# Defensive bounds on one listing's page count. They stop a misbehaving endpoint that keeps
# returning cursors or full pages; real accounts stay orders of magnitude below them.
_MAX_CURSOR_PAGES: Final = 10_000
_MAX_OFFSET_PAGES: Final = 10_000
_MAX_NEXT_LINKS: Final = 1_000

_REASON_PAGE_LIMIT: Final = "page limit"
_REASON_ONE_DAY_CAP: Final = "result limit reached at finest partition (one day)"
_REASON_CAP: Final = "result limit reached"

# Distinct bearer tokens a client keeps in memory, oldest dropped first, only to strip them
# from the errors of its later calls. The same bound as the token provider's retired tokens
# in ``auth.py``, so the client holds as many past tokens as the provider does.
_USED_TOKENS_MAX: Final = 8


# ---------------------------------------------------------------------------
# Rest.li 2.0 helpers
# ---------------------------------------------------------------------------


def encode_urn(urn: str) -> str:
    """Percent-encode a URN for use inside a Rest.li query value.

    Example::

        >>> encode_urn("urn:li:sponsoredAccount:123")
        'urn%3Ali%3AsponsoredAccount%3A123'

    Args:
        urn: A raw URN such as ``urn:li:sponsoredCampaign:42``.

    Returns:
        ``quote(urn, safe="")``.

    Raises:
        ValueError: When ``urn`` is blank or already holds a ``%``, which would mean it is
            encoded already and would be double-encoded (``%253A``).
    """
    if not urn or not urn.strip():
        raise ValueError("a URN must not be blank")
    if "%" in urn:
        raise ValueError("a URN must be passed raw; it already holds a percent escape")
    return quote(urn, safe="")


def restli_list(items: Iterable[str]) -> str:
    """Return the Rest.li 2.0 list ``List(a,b,...)`` of already-encoded items.

    Args:
        items: Encoded items, for example from :func:`encode_urn`; they are joined as given.

    Returns:
        ``"List(" + ",".join(items) + ")"``.
    """
    return "List(" + ",".join(items) + ")"


def restli_date(day: date) -> str:
    """Return the Rest.li 2.0 date ``(year:Y,month:M,day:D)``, with no zero padding.

    Example::

        >>> restli_date(date(2026, 9, 4))
        '(year:2026,month:9,day:4)'
    """
    return f"(year:{day.year},month:{day.month},day:{day.day})"


def restli_date_range(start: date, end: date) -> str:
    """Return the Rest.li 2.0 ``dateRange`` value; both dates are inclusive.

    Example::

        >>> restli_date_range(date(2026, 9, 1), date(2026, 9, 28))
        '(start:(year:2026,month:9,day:1),end:(year:2026,month:9,day:28))'

    Raises:
        ValueError: When ``start`` is after ``end``.
    """
    if start > end:
        raise ValueError("a date range must not start after it ends")
    return f"(start:{restli_date(start)},end:{restli_date(end)})"


def restli_query(pairs: Sequence[tuple[str, str]]) -> str:
    """Join pre-assembled Rest.li ``key=value`` pairs with ``&``.

    Values are complete Rest.li 2.0 strings whose structural characters ``( ) : ,`` stay
    literal and whose URNs, ids and page tokens are already percent-encoded once. Every key
    and value is checked, so nothing httpx would re-encode, and nothing that would split the
    query, ever reaches the URL.

    Example::

        >>> restli_query([("q", "search"), ("pageSize", "100")])
        'q=search&pageSize=100'

    Args:
        pairs: ``(key, value)`` pairs in request order.

    Returns:
        The query string, without a leading ``?``.

    Raises:
        ValueError: When a key or value is empty, holds a character outside
            ``[A-Za-z0-9-._~():,*%]`` (a raw ``"``, space, ``&``, ``#``, ``+`` or ``=``), or
            holds a ``%`` that does not start a two-digit hex escape.
    """
    parts: list[str] = []
    for key, value in pairs:
        # Only a key that is itself valid is named in the error; a value is never echoed,
        # because it can be an id or a page token.
        label = repr(key) if _QUERY_TEXT_RE.fullmatch(key) else "a query key"
        for what, text in (("key", key), ("value", value)):
            if not _QUERY_TEXT_RE.fullmatch(text):
                raise ValueError(f"the {what} of {label} is empty or holds a raw character")
            if _PERCENT_ESCAPE_RE.search(text):
                raise ValueError(f"the {what} of {label} holds a malformed percent escape")
        parts.append(f"{key}={value}")
    return "&".join(parts)


def _epoch_ms(instant: datetime, what: str) -> int:
    """Return a timezone-aware instant as epoch milliseconds, with integer arithmetic."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return (instant - _EPOCH) // _ONE_MS


def _restli_time_range(start: datetime, end: datetime) -> str:
    """Return the Rest.li ``submittedAtTimeRange`` value ``(start:{ms},end:{ms})``."""
    start_ms = _epoch_ms(start, "the range start")
    end_ms = _epoch_ms(end, "the range end")
    if start_ms > end_ms:
        raise ValueError("a time range must not start after it ends")
    return f"(start:{start_ms},end:{end_ms})"


def _owner(account_urn: str) -> str:
    """Return the Lead Sync owner selector ``(sponsoredAccount:{encoded account URN})``."""
    return f"(sponsoredAccount:{encode_urn(account_urn)})"


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    """Split ``items`` into consecutive chunks of at most ``size``."""
    return [items[index : index + size] for index in range(0, len(items), size)]


def _distinct(items: Iterable[str]) -> list[str]:
    """Return ``items`` without repeats, in first-seen order."""
    return list(dict.fromkeys(items))


def _check_page_size(page_size: int, max_pages: int | None) -> None:
    """Validate the offset paging arguments of the Lead Sync listings."""
    if isinstance(page_size, bool) or not 1 <= page_size <= LEAD_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {LEAD_PAGE_SIZE}")
    if max_pages is not None and (isinstance(max_pages, bool) or max_pages < 1):
        raise ValueError("max_pages must be at least 1 when given")


def _check_limit(name: str, value: int | None) -> None:
    """Validate an optional request bound: ``None`` (no bound) or an integer of at least 1."""
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError(f"{name} must be at least 1 when given")


def _check_date_range(date_range: tuple[date, date]) -> tuple[date, date]:
    """Return an inclusive ``(start, end)`` date range, rejecting a start after its end."""
    start, end = date_range
    if start > end:
        raise ValueError("a date range must not start after it ends")
    return start, end


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Listing[T]:
    """The rows of one LinkedIn listing and whether the enumeration finished.

    Mirrors the Google connector's partitioned fetch outcome: a ``TransientError`` that
    survives :func:`~ads_engine.connectors.http.send_json`'s retries stops the listing, the
    rows already read are kept, ``complete`` is ``False``, ``reason`` is the error's reason
    and ``interrupted_by`` holds it. ``AccessDenied`` and ``QueryDefect`` always propagate.
    ``sync`` never infers a removal or a ``missing`` discrepancy from an incomplete listing,
    and the ``sync/mode.py`` probe treats ``interrupted_by`` as a transient failure.

    Attributes:
        rows: The parsed rows, de-duplicated on their natural key, in response order.
        complete: Whether every page was read and no response hit a result limit.
        reason: Why the listing is incomplete, or ``None`` when it is complete.
        calls: LinkedIn calls made, each retry sequence counted once.
        interrupted_by: The transient error that ended the listing early, or ``None``.
    """

    rows: tuple[T, ...]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None = None


@dataclass(frozen=True, slots=True)
class LeadListing:
    """The sponsored lead responses of one owner listing; test leads are dropped.

    Attributes:
        leads: Parsed leads, de-duplicated on response id (the first is kept). They carry
            only the SHA-256 of the email, never the plaintext.
        versions: The distinct form-version maps learned from the responses' ``form``
            decorations, for ``sync`` to persist as ``lead_form_version``.
        test_leads_skipped: Responses with ``testLead`` true, counted and never returned.
        complete: Whether every page was read.
        reason: Why the listing is incomplete, or ``None``.
        calls: LinkedIn calls made.
        interrupted_by: The transient error that ended the listing early, or ``None``.
    """

    leads: tuple[ParsedLead, ...]
    versions: tuple[FormVersionMap, ...]
    test_leads_skipped: int
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None = None


@dataclass(frozen=True, slots=True)
class LeadBatch:
    """The transient batch re-fetch of lead responses by id (the email-matching path).

    The caller passes each found element to ``parser.decode_email``, holds the plaintext only
    for one HubSpot ``contacts_by_email`` call and then drops it. Nothing here is stored or
    logged, and ``found`` is excluded from ``repr`` because its elements hold emails.

    Attributes:
        found: Each re-fetched response element, keyed by its plain response id. An element
            is here only when its own ``id`` is that id, its ``owner.sponsoredAccount`` the
            requested account, its ``leadType`` ``SPONSORED`` and its ``testLead`` ``false``.
        failed: Requested ids that were not re-fetched: present in ``errors``, absent from
            ``results``, answered by an element whose identity fields do not match the
            request (or are missing), or not yet read when a transient error ended the
            batch. ``sync/lead_match.py`` records each as a failed re-fetch.
        interrupted_by: The transient error that ended the batch early, or ``None``.
        calls: LinkedIn calls made.
    """

    # Hidden from repr: every element carries the lead's email answer.
    found: Mapping[str, Mapping[str, object]] = field(repr=False)
    failed: frozenset[str]
    interrupted_by: TransientError | None = None
    calls: int = 0


@dataclass(frozen=True, slots=True)
class PostBatch:
    """Headline and intro text of the posts creatives reference.

    Attributes:
        posts: Parsed posts keyed by post URN.
        failed: Requested post URNs not read: present in ``errors`` (an unreadable post comes
            back as a per-key 403 inside HTTP 200), absent from ``results``, or not yet read
            when a transient error ended the batch. Their creatives keep a null headline.
        complete: Whether every batch was read.
        reason: Why the batch is incomplete, or ``None``.
        calls: LinkedIn calls made.
        interrupted_by: The transient error that ended the batch early, or ``None``.
    """

    posts: Mapping[str, PostRow]
    failed: frozenset[str]
    complete: bool
    reason: str | None
    calls: int
    interrupted_by: TransientError | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

type _Element = Mapping[str, object]


@dataclass(slots=True)
class _Tally:
    """Calls made by one client operation; each ``send_json`` retry sequence counts once."""

    calls: int = 0


@dataclass(slots=True)
class _Pages:
    """The elements of one request and the next links it led to.

    Attributes:
        elements: Every element read, in response order.
        capped: Whether the last response held at least :data:`MAX_ANALYTICS_ELEMENTS`
            elements with no ``next`` link, so it may be truncated.
        limited: Whether a ``max_pages`` bound stopped the chain with a ``next`` link unread.
        interrupted_by: The transient error that stopped the next-link chain, or ``None``.
    """

    elements: list[_Element] = field(default_factory=list)
    capped: bool = False
    limited: bool = False
    interrupted_by: TransientError | None = None


def _defect(message: str, surface: str, *, code: str = "validation") -> QueryDefect:
    """Build the ``QueryDefect`` of a response that breaks its contract.

    ``message`` names the surface and the field only, never a value from a response.
    """
    return QueryDefect(message, source=SOURCE, endpoint=surface, code=code)


def _body_object(response: JsonResponse, surface: str) -> _Element:
    """Return the response body as a JSON object, or raise a ``validation`` defect."""
    body: object = response.body
    if not isinstance(body, Mapping):
        raise _defect("the response body is not a JSON object", surface)
    return body


def _elements(body: _Element, surface: str) -> list[_Element]:
    """Return the ``elements`` list of a collection response.

    An unrecognised body is a defect, never an empty listing: an empty campaign listing
    would make every tracked campaign look ``missing``.
    """
    elements = body.get("elements")
    if not isinstance(elements, list):
        raise _defect("the response has no 'elements' list", surface)
    checked: list[_Element] = []
    for element in elements:
        if not isinstance(element, Mapping):
            raise _defect("an item of 'elements' is not a JSON object", surface)
        checked.append(element)
    return checked


def _relative_href(href: str, surface: str) -> str:
    """Turn a ``next`` link into a URL relative to the client base, query bytes untouched.

    LinkedIn returns links as ``/rest/...`` paths or absolute ``https://api.linkedin.com``
    URLs. The ``/rest/`` prefix is stripped because the client's base URL already ends in
    ``/rest/`` and httpx's base-URL merge would otherwise double it.

    Only visible ASCII characters reach LinkedIn byte for byte, so a link holding any other
    character is rejected before it is parsed: ``urlsplit`` would silently drop a tab or a
    line break, and a non-ASCII host could lower-case to ``api.linkedin.com``. An absolute
    link must name exactly ``https://api.linkedin.com``; user information or any port, even
    ``:443``, makes it another authority.

    Raises:
        QueryDefect: A ``validation`` defect when the link is not a valid URL, points
            outside ``https://api.linkedin.com`` or ``/rest/``, or has a malformed path. The
            message never quotes the link.
    """
    if any(not "!" <= char <= "~" for char in href):
        raise _defect("a 'next' link is not a valid URL", surface)
    try:
        parts = urlsplit(href)
    except ValueError:
        # An unbalanced or invalid bracketed host. urlsplit's message quotes the link, so
        # it is not chained onto the defect.
        raise _defect("a 'next' link is not a valid URL", surface) from None
    if (parts.scheme or parts.netloc) and (
        parts.scheme != "https" or parts.netloc.lower() != _API_HOST
    ):
        raise _defect("a 'next' link points outside https://api.linkedin.com", surface)
    path = parts.path
    if path.startswith(_BASE_PATH):
        relative = path[len(_BASE_PATH) :]
    elif path.startswith("/"):
        raise _defect("a 'next' link is outside the versioned REST base /rest/", surface)
    else:
        relative = path
    if not _RELATIVE_PATH_RE.fullmatch(relative) or ".." in relative.split("/"):
        raise _defect("a 'next' link has a malformed path", surface)
    return f"{relative}?{parts.query}" if parts.query else relative


def _next_href(body: _Element, surface: str) -> str | None:
    """Return the ``rel=next`` link of ``paging.links`` as a relative URL, or ``None``."""
    paging = body.get("paging")
    if paging is None:
        return None
    if not isinstance(paging, Mapping):
        raise _defect("field 'paging' is not a JSON object", surface)
    links = paging.get("links")
    if links is None:
        return None
    if not isinstance(links, list):
        raise _defect("field 'paging.links' is not a list", surface)
    for link in links:
        if not isinstance(link, Mapping):
            raise _defect("an item of 'paging.links' is not a JSON object", surface)
        if link.get("rel") != "next":
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href.strip():
            raise _defect("a 'next' link has no href", surface)
        return _relative_href(href.strip(), surface)
    return None


def _cursor_token(body: _Element, surface: str) -> str | None:
    """Return ``metadata.nextPageToken``; absent, null or empty ends the enumeration."""
    metadata = body.get("metadata")
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise _defect("field 'metadata' is not a JSON object", surface)
    token = metadata.get("nextPageToken")
    if token is None:
        return None
    if not isinstance(token, str):
        raise _defect("field 'metadata.nextPageToken' is not a string", surface)
    return token or None


def _batch_key(key: str) -> str:
    """Normalise a batch-result key: percent-decode it, then strip one pair of quotes."""
    text = unquote(key)
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text


def _batch_maps(body: _Element, surface: str) -> tuple[dict[str, _Element], frozenset[str]]:
    """Read a Rest.li batch-get envelope ``{results, statuses, errors}``.

    Keys are normalised with :func:`_batch_key`. ``statuses`` and ``errors`` are read both at
    the top level and nested inside ``results``, where LinkedIn's documented Lead Sync sample
    places them; a key listed in ``errors``, or with a non-2xx status, is failed.

    Returns:
        ``(results by key, failed keys)``. A non-object value under a key that is not
        ``statuses`` or ``errors`` is a defect.
    """
    results = body.get("results")
    if not isinstance(results, Mapping):
        raise _defect("the batch response has no 'results' object", surface)
    found: dict[str, _Element] = {}
    failed: set[str] = set()
    error_maps: list[object] = [body.get("errors"), results.get("errors")]
    status_maps: list[object] = [body.get("statuses"), results.get("statuses")]
    for raw_key, value in results.items():
        if raw_key in ("statuses", "errors"):
            continue
        if not isinstance(value, Mapping):
            raise _defect("a batch result is not a JSON object", surface)
        found[_batch_key(str(raw_key))] = value
    for errors in error_maps:
        if errors is None:
            continue
        if not isinstance(errors, Mapping):
            raise _defect("field 'errors' is not a JSON object", surface)
        failed.update(_batch_key(str(key)) for key in errors)
    for statuses in status_maps:
        if statuses is None:
            continue
        if not isinstance(statuses, Mapping):
            raise _defect("field 'statuses' is not a JSON object", surface)
        for key, status in statuses.items():
            is_status = isinstance(status, int) and not isinstance(status, bool)
            if is_status and not 200 <= status <= 299:
                failed.add(_batch_key(str(key)))
    return found, frozenset(failed)


def _distinct_rows[R](rows: Iterable[R], key: Callable[[R], Hashable]) -> tuple[list[R], int]:
    """Keep the first row per natural key; return the rows and the number of repeats dropped.

    A repeated key inside one upsert batch would make PostgreSQL's ``ON CONFLICT DO UPDATE``
    affect a row twice, so listings never return one.
    """
    seen: set[Hashable] = set()
    kept: list[R] = []
    dropped = 0
    for row in rows:
        row_key = key(row)
        if row_key in seen:
            dropped += 1
            continue
        seen.add(row_key)
        kept.append(row)
    return kept, dropped


def _urn_key(row: CampaignGroupRow | CampaignRow | CreativeRow) -> str:
    """Natural key of an entity row: its URN."""
    return row.urn


def _daily_key(row: CreativeDailyRow) -> tuple[str, date]:
    """Natural key of a daily creative row: the creative and its UTC day."""
    return row.creative_urn, row.date


def _form_key(row: LeadFormRow) -> str:
    """Natural key of a lead form: its canonical URN."""
    return row.canonical_urn


def _segment_key(row: SegmentRow) -> tuple[str, str, str]:
    """Natural key of a segment row within one window: campaign, facet and value."""
    return row.campaign_urn, row.facet, row.value


def _paging_integer(paging: _Element, key: str, surface: str) -> int | None:
    """Return the integer ``paging.{key}``, or ``None`` when the key is absent.

    A present value that is not an integer (``null``, a string or a boolean included) is a
    ``validation`` defect.
    """
    if key not in paging:
        return None
    value = paging[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise _defect(f"field 'paging.{key}' is not an integer", surface)
    return value


def _checked_offset_paging(body: _Element, surface: str, *, start: int, count: int) -> bool:
    """Validate the ``paging`` envelope of one Lead Sync offset page; report a ``next`` link.

    The lead_forms and lead_form_responses contracts require ``paging`` as an object with
    integer ``start`` and ``count``; ``total`` (an integer, page-local for responses) and
    ``links`` are optional. ``start`` must echo the requested offset and ``count`` the
    requested page size, as both contracts' documented samples do for their documented
    requests, so a page answering another offset or size is never read as this one. Each
    ``links`` item needs a string ``rel`` and ``href`` and, when present, a string ``type``. A
    ``rel=next`` href must stay on ``https://api.linkedin.com`` under ``/rest/``
    (:func:`_relative_href`) although offset listings never follow it: it reorders the
    projection and the time range, so only its presence is used.

    Args:
        body: The page's response body.
        surface: The contract surface, for errors.
        start: The ``start`` the page was requested with.
        count: The ``count`` the page was requested with.

    Returns:
        Whether ``paging.links`` holds a ``rel=next`` link.

    Raises:
        QueryDefect: When the envelope breaks its contract or answers another page.
    """
    if "paging" not in body:
        raise _defect("the response has no 'paging' object", surface)
    paging = body["paging"]
    if not isinstance(paging, Mapping):
        raise _defect("field 'paging' is not a JSON object", surface)
    for key, requested in (("start", start), ("count", count)):
        value = _paging_integer(paging, key, surface)
        if value is None:
            raise _defect(f"field 'paging.{key}' is missing", surface)
        if value != requested:
            raise _defect(f"field 'paging.{key}' does not match the requested {key}", surface)
    total = _paging_integer(paging, "total", surface)
    if total is not None and total < 0:
        raise _defect("field 'paging.total' is negative", surface)
    if "links" not in paging:
        return False
    links = paging["links"]
    if not isinstance(links, list):
        raise _defect("field 'paging.links' is not a list", surface)
    has_next = False
    for link in links:
        if not isinstance(link, Mapping):
            raise _defect("an item of 'paging.links' is not a JSON object", surface)
        rel = link.get("rel")
        href = link.get("href")
        if not isinstance(rel, str):
            raise _defect("an item of 'paging.links' has no string 'rel'", surface)
        if not isinstance(href, str):
            raise _defect("an item of 'paging.links' has no string 'href'", surface)
        if "type" in link and not isinstance(link["type"], str):
            raise _defect("field 'type' of an item of 'paging.links' is not a string", surface)
        if rel == "next":
            if not href.strip():
                raise _defect("a 'next' link has no href", surface)
            _relative_href(href.strip(), surface)
            has_next = True
    return has_next


def _discovered_campaign(element: _Element, surface: str) -> str:
    """Read the single campaign URN of a ``pivot=CAMPAIGN`` analytics element."""
    values = element.get("pivotValues")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        raise _defect("field 'pivotValues' must hold exactly one campaign URN", surface)
    value = values[0]
    tail = value.removeprefix(PREFIX_CAMPAIGN)
    if tail == value or not _DIGITS_RE.fullmatch(tail):
        raise _defect("field 'pivotValues' must hold a urn:li:sponsoredCampaign:{id} URN", surface)
    return value


def _is_post_urn(urn: str) -> bool:
    """Whether ``urn`` is a ``urn:li:share:{id}`` or ``urn:li:ugcPost:{id}`` URN."""
    for prefix in _POST_PREFIXES:
        if urn.startswith(prefix):
            return _DIGITS_RE.fullmatch(urn[len(prefix) :]) is not None
    return False


def _checked_lead_id(lead_id: str) -> str:
    """Validate one plain lead response id for the batch re-fetch."""
    if not isinstance(lead_id, str) or not lead_id.strip():
        raise ValueError("a lead response id must be a non-blank string")
    if lead_id.startswith("urn:"):
        raise ValueError("lead response ids are plain ids, not URNs")
    if '"' in lead_id:
        raise ValueError("a lead response id must not hold a double quote")
    return lead_id


def _is_requested_lead(element: _Element, lead_id: str, account_urn: str) -> bool:
    """Whether a batch re-fetch element is the requested sponsored response of the account.

    A ``results`` map key alone never binds an element to a request: the element's own
    ``id`` must be the requested plain id, ``owner.sponsoredAccount`` the requested account
    URN, ``leadType`` ``SPONSORED`` (the request's ``leadType`` selector) and ``testLead``
    the boolean ``false`` (test leads are dropped at ingest, so no stored lead re-fetches as
    one). A missing or mistyped field fails the check, so the email of another response,
    another owner or a test lead is never decoded for the requested lead.
    """
    owner = element.get("owner")
    response_id = element.get("id")
    return (
        isinstance(response_id, str)
        and response_id == lead_id
        and isinstance(owner, Mapping)
        and owner.get("sponsoredAccount") == account_urn
        and element.get("leadType") == "SPONSORED"
        and element.get("testLead") is False
    )


def _log_outcome(
    surface: str,
    *,
    rows: int,
    calls: int,
    complete: bool,
    reason: str | None,
    **counts: int,
) -> None:
    """Log one ``linkedin.listing`` event: counts only, never ids, bodies or emails."""
    fields: dict[str, object] = {
        "surface": surface,
        "rows": rows,
        "calls": calls,
        "complete": complete,
        "reason": reason,
        **counts,
    }
    if complete:
        log.info("linkedin.listing", **fields)
    else:
        log.warning("linkedin.listing", **fields)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LinkedInClient:
    """Read-only client of the LinkedIn Marketing API, version ``202609``.

    Every request goes to ``https://api.linkedin.com/rest/`` (the base URL of
    ``build_client("linkedin")``) with ``Authorization: Bearer <token>``,
    ``Linkedin-Version: 202609``, ``X-Restli-Protocol-Version: 2.0.0`` and
    ``Accept: application/json``. The client only reads: it sends ``GET`` requests and never
    creates, updates or deletes anything on LinkedIn (AAP 0.7.2).

    Scopes (read-only, granted once through the Developer Portal token generator):

    * ``r_ads``: ad accounts, campaign groups, campaigns and creatives;
    * ``r_ads_reporting``: ``adAnalytics``;
    * ``r_marketing_leadgen_automation``: lead forms and lead responses (the Lead Sync API
      product; ``r_ads`` cannot read leads).

    Plan gap: :meth:`get_posts` reads the Posts API, which needs ``r_organization_social``
    (granted with the Advertising API product) and is not among the planned scopes. An
    HTTP 403 on the whole posts call is ``AccessDenied`` and, like any LinkedIn denial,
    switches all of LinkedIn to simulated (AAP 0.3.3); it is never swallowed here. The
    recovery probe does not read posts, so a missing scope makes the live build fail and the
    source stays simulated rather than flipping back and forth. ``docs/runbook.md`` lists the
    scope. An unreadable single post comes back as a per-key 403 inside HTTP 200 and is only
    a failed read for that creative.

    Errors: only ``AccessDenied``, ``TransientError`` and ``QueryDefect`` leave the client
    (and ``ValueError`` for an invalid argument). On a 401 the client calls
    ``tokens.invalidate()`` and retries once with a fresh token when ``tokens.can_refresh``
    is true; a second 401, or a 401 with a static token, propagates as ``AccessDenied``.
    LinkedIn's daily per-application and per-member limits are unpublished and reset at
    midnight UTC; a 429 is transient and is retried by ``send_json``.

    Tokens in errors: the client keeps, in memory only and as ``SecretStr``, the last
    :data:`_USED_TOKENS_MAX` distinct bearer tokens it obtained from ``tokens``, and every
    call holds all of them through ``send_json``'s ``secrets``. An error echoing a token
    replaced by a refresh, on any later call, therefore never carries it, nor does any log.
    No REST request sends a refresh token or the client secret, yet a platform error can
    still echo one, so the client fails closed: when ``tokens`` also
    :class:`~ads_engine.connectors.linkedin.auth.HoldsSecrets`, as ``LinkedInTokenProvider``
    and ``StaticTokenProvider`` do, every call holds what ``tokens.held_secrets()`` returns
    when it is sent (the client secret, the current and rotated refresh tokens, and the
    access tokens the provider cached or retired). The client copies none of them; a
    rotation between two calls is covered by the later one.

    Days: LinkedIn reports daily buckets on UTC days, and rows keep the dates as returned.

    Recovery probe (AAP 0.3.3, used by ``sync/mode.py``), one call each:

    * ``get_ad_account(acct)``;
    * ``analytics_daily(acct, (d, d), expected_creatives=1)``;
    * ``list_lead_forms(acct, page_size=1, max_pages=1)``;
    * ``list_lead_responses(acct, (t0, t1), known_versions={}, page_size=1, max_pages=1)``.

    A probe listing whose ``interrupted_by`` is set failed transiently; ``complete=False``
    with reason ``"page limit"`` only means the probe stopped after its one page.

    Per-account context: the two-argument forms ``analytics_daily(acct, range)`` and
    ``list_lead_responses(acct, range)`` use what this client learned earlier for the
    account. :meth:`list_creatives` records the creative count that sizes the daily
    analytics chunks (one chunk for the whole range when no count is known; a chunk at the
    response cap is split either way). :meth:`list_lead_forms` and
    :meth:`list_lead_responses` record form-version maps by ``(canonical URN, version)``
    for decoding later responses of exactly that version. An explicit
    ``expected_creatives`` or ``known_versions`` replaces that context for the call.

    Example::

        async with build_client("linkedin", transport=mock_transport("linkedin", world)) as http:
            client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
            forms = await client.list_lead_forms("123456")
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        *,
        retry: RetryPolicy = DEFAULT_RETRY,
    ) -> None:
        """Bind the client to an HTTP client and a token source.

        Args:
            http: A client from ``build_client("linkedin", transport)``; the caller owns
                its lifecycle.
            tokens: The bearer token source: ``LinkedInTokenProvider`` live,
                ``StaticTokenProvider(SIMULATED_TOKEN)`` simulated.
            retry: The retry policy passed to every ``send_json`` call.
        """
        self._http = http
        self._tokens = tokens
        self._retry = retry
        # Ad account currencies learned by get_ad_account, keyed by account URN; a currency
        # never changes for an account, and bare campaign-group budgets need it.
        self._currencies: dict[str, str] = {}
        # Set once LinkedIn rejects the Statistics Finder segment request with HTTP 400, so
        # later segment calls of this client go straight to the Analytics Finder fallback.
        self._statistics_finder_rejected = False
        # Campaigns with delivery and the discovery's incompleteness reason per (account URN,
        # window start, window end, max_pages), found by the segment fallback's discovery call
        # and shared by the three demographic pivots. A bounded discovery is cached under its
        # own bound, so it never stands in for a full one.
        self._delivering_campaigns: dict[
            tuple[str, date, date, int | None], tuple[tuple[str, ...], str | None]
        ] = {}
        # Creatives per account URN, from the de-duplicated rows of this client's most recent
        # list_creatives of that account; analytics_daily sizes its date chunks with it when
        # the caller passes no expected_creatives.
        self._creative_counts: dict[str, int] = {}
        # Form-version maps per account URN, keyed by (canonical URN, version): every current
        # version list_lead_forms read and every decoration list_lead_responses learned for
        # that account. list_lead_responses decodes with them when the caller passes no
        # known_versions; each map only ever decodes responses of its own version.
        self._form_versions: dict[str, dict[tuple[str, int], FormVersionMap]] = {}
        # Distinct bearer tokens obtained from the token source, oldest first, kept in
        # memory only so no error or log of a later call carries one.
        self._used_tokens: deque[SecretStr] = deque(maxlen=_USED_TOKENS_MAX)

    def __repr__(self) -> str:
        """Return the class name only; the token source is never shown."""
        return f"{type(self).__name__}()"

    # -- request core -------------------------------------------------------------------

    async def _send(
        self,
        surface: str,
        url: str,
        token: str,
        extra_headers: Mapping[str, str] | None,
        tally: _Tally,
        *,
        held: Sequence[str] = (),
    ) -> JsonResponse:
        """Send one ``GET`` through ``send_json``; one retry sequence counts as one call.

        No error or log of the call may carry a token this client obtained from its token
        source (``_used_tokens``), nor one of ``held``, tokens beyond ``token`` such as the
        one a 401 refresh replaced, nor, when the token source
        :class:`~ads_engine.connectors.linkedin.auth.HoldsSecrets`, any secret its
        ``held_secrets()`` returns: its client secret, current and rotated refresh tokens
        and cached or retired access tokens. Those are read for each call, so a refresh
        token rotated since an earlier call is held too. ``send_json`` holds them all as its
        ``secrets``, with the bearer token the request itself carries, so a token from
        before a refresh stays out of the errors of every later call too.

        Raises:
            TypeError: When ``held`` is a single string, which would otherwise be read as
                one-character tokens.
        """
        if isinstance(held, str):
            raise TypeError("held must be a sequence of strings, not a single string")
        provider_held = (
            self._tokens.held_secrets() if isinstance(self._tokens, HoldsSecrets) else ()
        )
        secrets = (
            *(used.get_secret_value() for used in self._used_tokens),
            *held,
            *provider_held,
        )
        headers: dict[str, str] = dict(extra_headers or {})
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": LINKEDIN_VERSION,
                "X-Restli-Protocol-Version": RESTLI_PROTOCOL_VERSION,
                "Accept": "application/json",
            }
        )

        def build() -> httpx.Request:
            """Build a fresh request for each attempt."""
            return self._http.build_request("GET", url, headers=headers)

        tally.calls += 1
        return await send_json(
            self._http, build, source=SOURCE, endpoint=surface, policy=self._retry, secrets=secrets
        )

    async def _get_url(
        self,
        surface: str,
        url: str,
        *,
        tally: _Tally,
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        """``GET`` a URL relative to the base, refreshing the token once after a 401.

        AAP 0.3.3 classifies LinkedIn's "401 after one token refresh" as access denied: the
        first 401 of a refreshable token source invalidates the cached token and the call is
        repeated once; a 403 or a second 401 propagates as ``AccessDenied``. Each token
        obtained from the token source, before the first send and after the refresh, is
        retained at once (:meth:`_retain`), so this call and every later one hold it; the
        repeated call also names the rejected token, so an error echoing it never carries it.
        """
        token = await self._tokens.get()
        self._retain(token)
        try:
            return await self._send(surface, url, token, extra_headers, tally)
        except AccessDenied as denied:
            if not (denied.is_unauthorized and self._tokens.can_refresh):
                raise
        rejected = token
        self._tokens.invalidate()
        log.info("linkedin.token.retry_after_401", surface=surface)
        token = await self._tokens.get()
        self._retain(token)
        return await self._send(surface, url, token, extra_headers, tally, held=(rejected,))

    def _retain(self, token: str) -> None:
        """Keep ``token``, obtained from the token source, in memory as a ``SecretStr``.

        Only the last :data:`_USED_TOKENS_MAX` distinct tokens are kept, the oldest dropped
        first; a blank or already kept token is not added again. The kept tokens are never
        logged, persisted or shown by ``repr``; :meth:`_send` holds them on every call.
        """
        if not token.strip():
            return
        if any(used.get_secret_value() == token for used in self._used_tokens):
            return
        self._used_tokens.append(SecretStr(token))

    async def _get(
        self,
        surface: str,
        path: str,
        pairs: Sequence[tuple[str, str]],
        *,
        tally: _Tally,
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonResponse:
        """``GET`` ``path?restli_query(pairs)``; ``path`` is relative to ``/rest/``."""
        url = f"{path}?{restli_query(pairs)}" if pairs else path
        return await self._get_url(surface, url, tally=tally, extra_headers=extra_headers)

    async def _follow(
        self, surface: str, url: str, tally: _Tally, *, max_pages: int | None = None
    ) -> _Pages:
        """Read a request and every ``rel=next`` link it leads to.

        ``adAnalytics`` documents no pagination, but live responses can carry a ``next``
        link, which is followed at once, from an empty page too: only a response without a
        ``next`` link ends the chain, so no row behind an empty page is lost. A link back to
        any request already read is a defect, and so is a chain of more than
        :data:`_MAX_NEXT_LINKS` links, so the chain always terminates. A transient failure
        stops the chain, keeps the elements already read and sets ``interrupted_by``, so the
        caller reports the listing incomplete. With ``max_pages``, at most that many
        responses are read (the request and its ``next`` links); a ``next`` link left unread
        sets ``limited``, so the caller reports the listing incomplete too.
        """
        pages = _Pages()
        seen: set[str] = {url}
        current = url
        for read in range(1, _MAX_NEXT_LINKS + 2):
            try:
                response = await self._get_url(surface, current, tally=tally)
            except TransientError as exc:
                pages.interrupted_by = exc
                return pages
            body = _body_object(response, surface)
            elements = _elements(body, surface)
            pages.elements.extend(elements)
            href = _next_href(body, surface)
            if href is None:
                pages.capped = len(elements) >= MAX_ANALYTICS_ELEMENTS
                return pages
            if max_pages is not None and read >= max_pages:
                pages.limited = True
                return pages
            if href in seen:
                raise _defect("a 'next' link repeats an earlier request", surface)
            seen.add(href)
            current = href
        raise _defect(f"more than {_MAX_NEXT_LINKS} 'next' links in one chain", surface)

    async def _cursor_pages[T](
        self,
        surface: str,
        path: str,
        pairs: Sequence[tuple[str, str]],
        parse: Callable[[_Element], T],
        tally: _Tally,
        *,
        max_pages: int | None = None,
    ) -> tuple[list[T], TransientError | None, str | None, int]:
        """Page a search by ``pageToken`` until ``metadata.nextPageToken`` is absent.

        Each page sends ``pageSize=100`` and, after the first, the previous response's
        ``metadata.nextPageToken`` percent-encoded once as ``pageToken``. The legacy
        ``paging`` block that live responses also carry is ignored: its links can hold a
        stale ``next`` href. A token that repeats is a defect, so enumeration terminates.
        With ``max_pages``, paging stops after that many pages; when the last page read
        still carries a token, the search is incomplete with reason "page limit".

        Returns:
            ``(rows, interrupted_by, reason, pages)``: the parsed rows, the transient error
            that ended paging early or ``None``, "page limit" when ``max_pages`` left a page
            unread or ``None``, and the number of pages read.
        """
        rows: list[T] = []
        token: str | None = None
        seen: set[str] = set()
        for page in range(1, _MAX_CURSOR_PAGES + 1):
            page_pairs = [*pairs, ("pageSize", str(ENTITY_PAGE_SIZE))]
            if token is not None:
                page_pairs.append(("pageToken", quote(token, safe="")))
            try:
                response = await self._get(surface, path, page_pairs, tally=tally)
            except TransientError as exc:
                return rows, exc, None, page - 1
            body = _body_object(response, surface)
            rows.extend(parse(element) for element in _elements(body, surface))
            next_token = _cursor_token(body, surface)
            if next_token is None:
                return rows, None, None, page
            if max_pages is not None and page >= max_pages:
                return rows, None, _REASON_PAGE_LIMIT, page
            if next_token in seen:
                raise _defect("page token did not advance", surface)
            seen.add(next_token)
            token = next_token
        raise _defect(f"more than {_MAX_CURSOR_PAGES} pages in one search", surface)

    async def _offset_pages(
        self,
        surface: str,
        path: str,
        head: Sequence[tuple[str, str]],
        tail: Sequence[tuple[str, str]],
        *,
        page_size: int,
        max_pages: int | None,
        tally: _Tally,
        consume: Callable[[list[_Element]], None],
    ) -> tuple[bool, str | None, TransientError | None]:
        """Page a Lead Sync ``q=owner`` finder by ``count`` and ``start``.

        Every page is rebuilt from the request template as ``head``, ``count``, ``start``,
        ``tail``, with ``start`` advanced by ``page_size``; the ``next`` href is never
        followed, because it reorders the projection and the time range.

        Stop rule (lead_form_responses contract): ``paging.total`` counts only the elements
        of the current page, so ``start + count >= total`` would stop after page 1. Paging
        stops on a page with fewer than ``page_size`` elements and no ``next`` link, and on
        an empty page. The lead_forms contract's ``start + count >= total`` rule is not used
        for forms either: Lead Sync pages forms the same way, and under an overall total the
        rule used here costs at most one extra, empty page instead of a missed one.

        Envelope (both contracts' ``#/response/body/required``): each page's ``paging`` block
        is validated by :func:`_checked_offset_paging` before its elements are consumed or
        the stop rule is applied. A page without ``paging``, with a non-integer ``start`` or
        ``count``, one echoing another offset or page size, or with a malformed link is a
        ``QueryDefect``, never the end of the listing.

        Args:
            surface: The contract surface, for errors and logs.
            path: The finder path relative to ``/rest/``.
            head: Query pairs sent before ``count`` and ``start``.
            tail: Query pairs sent after them (the ``fields`` projection).
            page_size: ``count`` of every page.
            max_pages: The most pages to read, or ``None`` for the defensive bound.
            tally: The operation's call counter.
            consume: Called with each page's elements as soon as the page arrives, so lead
                responses (which carry emails) are parsed and dropped page by page.

        Returns:
            ``(complete, reason, interrupted_by)``.
        """
        limit = _MAX_OFFSET_PAGES if max_pages is None else max_pages
        start = 0
        for pages in range(1, limit + 1):
            page_pairs = [*head, ("count", str(page_size)), ("start", str(start)), *tail]
            try:
                response = await self._get(surface, path, page_pairs, tally=tally)
            except TransientError as exc:
                return False, exc.reason, exc
            body = _body_object(response, surface)
            elements = _elements(body, surface)
            has_next = _checked_offset_paging(body, surface, start=start, count=page_size)
            consume(elements)
            more = bool(elements) and (len(elements) >= page_size or has_next)
            if not more:
                return True, None, None
            if pages == limit:
                break
            start += page_size
        if max_pages is None:
            raise _defect(f"more than {_MAX_OFFSET_PAGES} pages in one listing", surface)
        return False, _REASON_PAGE_LIMIT, None

    # -- entities -----------------------------------------------------------------------

    async def _fetch_account(self, account_urn: str, tally: _Tally) -> AdAccountRow:
        """Read ``GET adAccounts/{id}`` and remember the account's currency."""
        numeric_id = urn_id(account_urn, PREFIX_ACCOUNT)
        response = await self._get(_SURFACE_AD_ACCOUNT, f"adAccounts/{numeric_id}", (), tally=tally)
        row = parse_ad_account(response.body)
        if row.external_id != numeric_id:
            raise _defect("the ad account response names another account", _SURFACE_AD_ACCOUNT)
        self._currencies[account_urn] = row.currency
        return row

    async def get_ad_account(self, account_id: str) -> AdAccountRow:
        """Read one ad account: the source probe and the currency of its amounts.

        ``GET adAccounts/{id}`` (contract ``ad_account``). The currency is remembered, so
        later campaign-group and campaign listings of the account check their budgets
        against it without another call.

        Args:
            account_id: The numeric id from ``LINKEDIN_AD_ACCOUNT_IDS`` or its
                ``urn:li:sponsoredAccount:{id}`` URN.

        Returns:
            The parsed account.

        Raises:
            AccessDenied: The credentials cannot read the account.
            TransientError: The call failed transiently on every attempt.
            QueryDefect: The response breaks the contract or names another account.
            ValueError: When ``account_id`` is neither a numeric id nor an account URN.
        """
        account_urn = sponsored_account_urn(account_id)
        tally = _Tally()
        row = await self._fetch_account(account_urn, tally)
        _log_outcome(_SURFACE_AD_ACCOUNT, rows=1, calls=tally.calls, complete=True, reason=None)
        return row

    async def list_campaign_groups(
        self,
        account_id: str,
        *,
        account_currency: str | None = None,
        max_pages: int | None = None,
    ) -> Listing[CampaignGroupRow]:
        """List the account's campaign groups in every status (contract ``ad_campaign_groups``).

        ``GET adAccounts/{id}/adCampaignGroups?q=search&search=(status:(values:List(ACTIVE,
        PAUSED,ARCHIVED,CANCELED,DRAFT,PENDING_DELETION,REMOVED)))&pageSize=100``, paged by
        ``pageToken``. The seven values are ``parser.CAMPAIGN_GROUP_SEARCH_STATUSES``,
        exactly as AAP 0.3.3 writes them. Each group carries status, ``totalBudget``,
        ``dailyBudget`` and the run schedule.

        A dynamic group's ``dailyBudget`` is a bare amount in the ad account's currency, so
        the listing needs that currency: ``account_currency`` when given, else the one
        learned by :meth:`get_ad_account`, else one ``adAccounts/{id}`` call made here.

        Args:
            account_id: The numeric ad account id or its URN.
            account_currency: The ad account's ISO 4217 currency, when already known.
            max_pages: Stop after this many pages; ``complete`` is then ``False`` with reason
                "page limit" when another page exists. ``None`` (every sync) reads every
                page; ``capture-fixtures`` sends 1.

        Returns:
            The groups; ``complete`` is ``False`` when a page failed transiently or
            ``max_pages`` left a page unread.

        Raises:
            ValueError: When ``max_pages`` is given and is not an integer of at least 1.
        """
        _check_limit("max_pages", max_pages)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_CAMPAIGN_GROUPS
        tally = _Tally()
        currency = account_currency or self._currencies.get(account_urn)
        if currency is None:
            try:
                currency = (await self._fetch_account(account_urn, tally)).currency
            except TransientError as exc:
                unread: list[CampaignGroupRow] = []
                return self._listing(surface, unread, tally, exc, key=_urn_key)
        known_currency: str = currency
        pairs = [
            ("q", "search"),
            ("search", f"(status:(values:{restli_list(CAMPAIGN_GROUP_SEARCH_STATUSES)}))"),
        ]
        path = f"adAccounts/{urn_id(account_urn, PREFIX_ACCOUNT)}/adCampaignGroups"
        rows, interrupted, reason, _pages = await self._cursor_pages(
            surface,
            path,
            pairs,
            lambda element: parse_campaign_group(element, account_currency=known_currency),
            tally,
            max_pages=max_pages,
        )
        return self._listing(surface, rows, tally, interrupted, key=_urn_key, reason=reason)

    async def list_campaigns(
        self,
        account_id: str,
        *,
        account_currency: str | None = None,
        max_pages: int | None = None,
    ) -> Listing[CampaignRow]:
        """List the account's campaigns in every status (contract ``ad_campaigns``).

        ``GET adAccounts/{id}/adCampaigns?q=search&search=(status:(values:List(...)))
        &pageSize=100``, paged by ``pageToken``. Each campaign carries status,
        ``dailyBudget``, ``totalBudget``, ``campaignGroup`` and ``objectiveType``.

        Plan correction: AAP 0.3.3 calls this "the same all-status search" as campaign
        groups, but campaigns have one more status, ``COMPLETED`` (a budgetary or
        chronological limit reached), documented in LinkedIn's 2026 campaign reference and
        in ``contracts/linkedin/ad_campaigns.json``. The list sent is
        ``parser.CAMPAIGN_SEARCH_STATUSES``, all eight statuses, so the tracked set can be
        diffed against the full list; omitting ``COMPLETED`` would raise false ``missing``
        discrepancies for finished campaigns.

        A listing cut short (any page failed, or ``max_pages`` left a page unread) has
        ``complete=False``, and ``sync`` never infers ``missing`` from it.

        Args:
            account_id: The numeric ad account id or its URN.
            account_currency: The ad account's currency; when given, or learned earlier by
                :meth:`get_ad_account`, a budget in another currency is a defect.
            max_pages: Stop after this many pages; ``complete`` is then ``False`` with reason
                "page limit" when another page exists. ``None`` (every sync) reads every
                page; ``capture-fixtures`` sends 1.

        Returns:
            The campaigns.

        Raises:
            ValueError: When ``max_pages`` is given and is not an integer of at least 1.
        """
        _check_limit("max_pages", max_pages)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_CAMPAIGNS
        tally = _Tally()
        currency = account_currency or self._currencies.get(account_urn)
        pairs = [
            ("q", "search"),
            ("search", f"(status:(values:{restli_list(CAMPAIGN_SEARCH_STATUSES)}))"),
        ]
        path = f"adAccounts/{urn_id(account_urn, PREFIX_ACCOUNT)}/adCampaigns"
        rows, interrupted, reason, _pages = await self._cursor_pages(
            surface,
            path,
            pairs,
            lambda element: parse_campaign(element, account_currency=currency),
            tally,
            max_pages=max_pages,
        )
        return self._listing(surface, rows, tally, interrupted, key=_urn_key, reason=reason)

    async def list_creatives(
        self, account_id: str, campaigns: Sequence[str], *, max_pages: int | None = None
    ) -> Listing[CreativeRow]:
        """List the creatives of the given campaigns in every status (contract ``creatives``).

        ``GET adAccounts/{id}/creatives?q=criteria&campaigns=List(urn%3Ali%3A
        sponsoredCampaign%3A{cid},...)&intendedStatuses=List(ACTIVE,PAUSED,DRAFT,ARCHIVED,
        CANCELED,PENDING_DELETION,REMOVED)&pageSize=100``, at most 100 campaigns per
        request, each chunk paged by ``pageToken``. Each creative carries its campaign,
        ``intendedStatus``, the referenced post and the lead-gen call-to-action form and
        label.

        Addition to AAP 0.3.3: ``intendedStatuses`` (``parser.CREATIVE_INTENDED_STATUSES``)
        is sent because LinkedIn hides ``CANCELED`` creatives from a campaign query unless
        they are requested, and entity state is synced in all statuses (AAP 0.3.2).

        An empty ``campaigns`` returns an empty, complete listing without a call: LinkedIn
        reads an empty ``campaigns=List()`` as "no filter".

        The number of creatives returned is remembered for the account, and
        :meth:`analytics_daily` sizes its date chunks with it when called without
        ``expected_creatives``.

        Args:
            account_id: The numeric ad account id or its URN.
            campaigns: Campaign ids or ``urn:li:sponsoredCampaign:{id}`` URNs; repeats are
                sent once.
            max_pages: Stop after this many pages in total, across the campaign chunks;
                ``complete`` is then ``False`` with reason "page limit" when another page or
                chunk remains. ``None`` (every sync) reads every page of every chunk;
                ``capture-fixtures`` sends 1.

        Returns:
            The creatives; complete only when every chunk completed.

        Raises:
            ValueError: When an id is neither numeric nor a campaign URN, or ``max_pages``
                is given and is not an integer of at least 1.
        """
        _check_limit("max_pages", max_pages)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_CREATIVES
        tally = _Tally()
        campaign_urns = _distinct(sponsored_campaign_urn(campaign) for campaign in campaigns)
        path = f"adAccounts/{urn_id(account_urn, PREFIX_ACCOUNT)}/creatives"
        statuses = restli_list(CREATIVE_INTENDED_STATUSES)
        rows: list[CreativeRow] = []
        interrupted: TransientError | None = None
        reason: str | None = None
        remaining = max_pages
        for chunk in _chunks(campaign_urns, CREATIVE_CAMPAIGN_CHUNK):
            if remaining == 0:
                reason = _REASON_PAGE_LIMIT
                break
            pairs = [
                ("q", "criteria"),
                ("campaigns", restli_list(encode_urn(urn) for urn in chunk)),
                ("intendedStatuses", statuses),
            ]
            chunk_rows, interrupted, reason, read = await self._cursor_pages(
                surface, path, pairs, parse_creative, tally, max_pages=remaining
            )
            rows.extend(chunk_rows)
            if interrupted is not None or reason is not None:
                break
            if remaining is not None:
                remaining -= read
        listing = self._listing(surface, rows, tally, interrupted, key=_urn_key, reason=reason)
        self._creative_counts[account_urn] = len(listing.rows)
        return listing

    async def get_posts(self, urns: Sequence[str]) -> PostBatch:
        """Read the headline and intro text of the posts creatives reference (contract ``posts``).

        ``GET posts?ids=List({encoded urn},...)`` with ``X-RestLi-Method: BATCH_GET``, the
        commas inside ``List(...)`` raw. Only ``urn:li:share:`` and ``urn:li:ugcPost:``
        URNs with a numeric id are requested (other content references have no post); they
        are de-duplicated and sent at most :data:`POST_BATCH_SIZE` per call. The response is
        ``{results: {urn: post}, statuses, errors}``; each result key is percent-decoded
        and parsed with ``parse_post``. A URN present in ``errors`` or absent from
        ``results`` is failed. The headline is ``content.media.title`` or
        ``content.article.title`` and the intro text is ``commentary`` (parser).

        Plan gap: see the class docstring. The Posts API needs ``r_organization_social``,
        which AAP 0.3.3 does not list; an HTTP 403 on the call propagates as
        ``AccessDenied`` and is not swallowed.

        Args:
            urns: Post URNs, typically ``CreativeRow.post_urn`` values.

        Returns:
            The posts read and the URNs that could not be read.
        """
        surface = _SURFACE_POSTS
        tally = _Tally()
        wanted = _distinct(urn for urn in urns if _is_post_urn(urn))
        skipped = sum(1 for urn in urns if not _is_post_urn(urn))
        posts: dict[str, PostRow] = {}
        failed: set[str] = set()
        interrupted: TransientError | None = None
        chunks = _chunks(wanted, POST_BATCH_SIZE)
        for index, chunk in enumerate(chunks):
            pairs = [("ids", restli_list(encode_urn(urn) for urn in chunk))]
            try:
                response = await self._get(
                    surface,
                    "posts",
                    pairs,
                    tally=tally,
                    extra_headers={"X-RestLi-Method": "BATCH_GET"},
                )
            except TransientError as exc:
                interrupted = exc
                for unread in chunks[index:]:
                    failed.update(unread)
                break
            found, failed_keys = _batch_maps(_body_object(response, surface), surface)
            for urn in chunk:
                element = found.get(urn)
                if urn in failed_keys or element is None:
                    failed.add(urn)
                else:
                    posts[urn] = parse_post(urn, element)
        complete = interrupted is None
        reason = None if interrupted is None else interrupted.reason
        _log_outcome(
            surface,
            rows=len(posts),
            calls=tally.calls,
            complete=complete,
            reason=reason,
            failed=len(failed),
            skipped=skipped,
        )
        return PostBatch(
            posts=posts,
            failed=frozenset(failed),
            complete=complete,
            reason=reason,
            calls=tally.calls,
            interrupted_by=interrupted,
        )

    def _listing[R](
        self,
        surface: str,
        rows: Iterable[R],
        tally: _Tally,
        interrupted: TransientError | None,
        *,
        key: Callable[[R], Hashable],
        reason: str | None = None,
    ) -> Listing[R]:
        """Build, log and return a :class:`Listing` with rows de-duplicated on ``key``.

        ``reason`` names a result-limit incompleteness; an interruption's reason wins.
        """
        kept, repeats = _distinct_rows(rows, key)
        if interrupted is not None:
            reason = interrupted.reason
        complete = reason is None
        _log_outcome(
            surface,
            rows=len(kept),
            calls=tally.calls,
            complete=complete,
            reason=reason,
            repeats_dropped=repeats,
        )
        return Listing(
            rows=tuple(kept),
            complete=complete,
            reason=reason,
            calls=tally.calls,
            interrupted_by=interrupted,
        )

    # -- analytics ----------------------------------------------------------------------

    async def analytics_daily(
        self,
        account_id: str,
        date_range: tuple[date, date],
        *,
        expected_creatives: int | None = None,
        max_pages: int | None = None,
    ) -> Listing[CreativeDailyRow]:
        """Read daily per-creative performance on UTC days (contract ``ad_analytics``).

        ``GET adAnalytics?q=analytics&pivot=CREATIVE&timeGranularity=DAILY&dateRange=(start:
        (year:Y,month:M,day:D),end:(year:Y,month:M,day:D))&accounts=List({encoded account
        URN})&fields={DAILY_FIELDS}``. ``dateRange`` is both a required request parameter,
        selecting the chunk's days, and a response field selected in ``fields``, which gives
        each element's day. ``pivotValues`` is a response field only: it is requested through
        ``fields`` and never sent as a request parameter (contract ``ad_analytics``,
        ``#/request/url`` and ``#/request/required_query``).

        Partitioning, so no stored day is ever silently truncated:

        * the range is cut into chunks of ``max(1, 14,999 // max(n, 1))`` days, where ``n`` is
          ``expected_creatives`` when given, else the creative count of this client's most
          recent :meth:`list_creatives` of the account, so creatives times days stays below
          the 15,000-element response cap; with neither, the creative count is unknown and
          the whole range is one chunk. Chunks are read in ascending date order;
        * a ``next`` link is followed at once until none remains;
        * a response with at least 15,000 elements and no ``next`` link may be truncated: its
          rows are discarded and its range is split at the midpoint into ``[start, mid]``
          and ``[mid + 1, end]``, read in that order;
        * a single day at the cap keeps its rows and makes the listing incomplete with the
          reason "result limit reached at finest partition (one day)";
        * with ``max_pages``, each chunk's request reads at most that many responses (the
          request and its ``next`` links); a ``next`` link left unread keeps the rows read
          and makes the listing incomplete with the reason "page limit".

        The creative count only sizes the first chunks; completeness never depends on it,
        because any chunk at the cap is split as above.

        The caller caps ``date_range`` at 180 days, LinkedIn's 6-month daily retention (the
        backfill and ``window.max_days``); this method does not enforce it.

        Args:
            account_id: The numeric ad account id or its URN.
            date_range: Inclusive ``(start, end)`` UTC dates.
            expected_creatives: The creatives expected to report, for chunk sizing, such as
                the creative count of the account's entity sync. ``None`` (the default) uses
                the count from this client's most recent :meth:`list_creatives` of the
                account, or one chunk for the whole range when there is none.
            max_pages: The most responses each chunk's request reads. ``None`` (every sync)
                follows every ``next`` link; ``capture-fixtures`` sends 1.

        Returns:
            One row per creative and UTC day, de-duplicated on that pair.

        Raises:
            ValueError: When the range starts after it ends, ``expected_creatives`` is
                given and is negative or a ``bool``, or ``max_pages`` is given and is not an
                integer of at least 1.
        """
        start, end = _check_date_range(date_range)
        if expected_creatives is not None and (
            isinstance(expected_creatives, bool) or expected_creatives < 0
        ):
            raise ValueError("expected_creatives must be a non-negative integer")
        _check_limit("max_pages", max_pages)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_ANALYTICS
        tally = _Tally()
        accounts = restli_list([encode_urn(account_urn)])
        creatives = (
            self._creative_counts.get(account_urn)
            if expected_creatives is None
            else expected_creatives
        )
        if creatives is None:
            span_days = (end - start).days + 1
        else:
            span_days = max(1, (MAX_ANALYTICS_ELEMENTS - 1) // max(creatives, 1))
        chunks: list[tuple[date, date]] = []
        cursor = start
        while True:
            remaining = (end - cursor).days
            chunk_end = cursor + timedelta(days=min(span_days - 1, remaining))
            chunks.append((cursor, chunk_end))
            if chunk_end >= end:
                break
            cursor = chunk_end + _ONE_DAY
        stack = list(reversed(chunks))
        rows: list[CreativeDailyRow] = []
        reason: str | None = None
        interrupted: TransientError | None = None
        while stack:
            chunk_start, chunk_end = stack.pop()
            pairs = [
                ("q", "analytics"),
                ("pivot", "CREATIVE"),
                ("timeGranularity", "DAILY"),
                ("dateRange", restli_date_range(chunk_start, chunk_end)),
                ("accounts", accounts),
                ("fields", DAILY_FIELDS),
            ]
            pages = await self._follow(
                surface, f"adAnalytics?{restli_query(pairs)}", tally, max_pages=max_pages
            )
            if pages.interrupted_by is not None:
                rows.extend(parse_analytics_daily(element) for element in pages.elements)
                interrupted = pages.interrupted_by
                break
            if pages.limited:
                reason = _REASON_PAGE_LIMIT
            if pages.capped:
                if chunk_start < chunk_end:
                    mid = chunk_start + timedelta(days=(chunk_end - chunk_start).days // 2)
                    stack.append((mid + _ONE_DAY, chunk_end))
                    stack.append((chunk_start, mid))
                    log.info(
                        "linkedin.analytics.split",
                        surface=surface,
                        days=(chunk_end - chunk_start).days + 1,
                    )
                    continue
                reason = _REASON_ONE_DAY_CAP
            rows.extend(parse_analytics_daily(element) for element in pages.elements)
        return self._listing(surface, rows, tally, interrupted, key=_daily_key, reason=reason)

    async def analytics_segments(
        self,
        account_id: str,
        pivot: str,
        window: tuple[date, date],
        *,
        max_pages: int | None = None,
        max_campaigns: int | None = None,
    ) -> Listing[SegmentRow]:
        """Read one demographic pivot per campaign over one exact preset window.

        Contracted request (``ad_analytics_statistics``, AAP 0.3.3): ``GET adAnalytics?
        q=statistics&pivots=List(CAMPAIGN,{pivot})&timeGranularity=ALL&dateRange=({window})
        &accounts=List({encoded account URN})&fields={SEGMENT_FIELDS}``, one request per
        pivot and preset window; ``SegmentSyncJob`` iterates ``window.presets`` ending on the
        default window end (AAP 0.3.2). ``next`` links are followed.

        Live fallback: LinkedIn 202609 rejects ``MEMBER_*`` values in the Statistics Finder
        ``pivots`` with HTTP 400 ("is not an enum symbol"; recorded as a plan risk in the
        contract and observed live on 2026-10-07). A 400 is a defect, which would fail every
        live segment fetch and with it every live LinkedIn dataset build, keeping LinkedIn
        simulated for good. On that 400 the client therefore switches, for the rest of its
        life, to the Analytics Finder form the contract documents as the verified fallback:
        one ``q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&accounts=List(...)`` call
        (contract ``ad_analytics_campaign_delivery``) finds the campaigns with delivery in the
        window, then ``q=analytics&pivot={pivot}&timeGranularity=ALL&dateRange=({window})
        &campaigns=List({one campaign URN})&fields={SEGMENT_FIELDS}`` (contract
        ``ad_analytics_member_segments``) is read per campaign, the campaign attributed from the
        request (``parse_segment(..., campaign_urn=...)``). The simulator answers the
        contracted form, so simulated runs take the fallback only when a world fault rejects
        that form with HTTP 400; a defect of the fallback itself propagates, chained to the
        original 400.

        Segment values are approximate and privacy-thresholded: LinkedIn returns at most the
        top 100 values per pivot and omits values reached by too few members, so a window's
        values are never summed from daily rows. A metric the pivot does not return is
        ``None`` (parser). A response at the 15,000 cap without a ``next`` link makes the
        listing incomplete with reason "result limit reached": the window must stay exact,
        so it cannot be split by date.

        Capture bounds (``capture-fixtures`` sends 1 for both; every sync sends neither):
        ``max_pages`` caps the responses each request reads (the request and its ``next``
        links), and ``max_campaigns`` caps the delivering campaigns the fallback reads, one
        request each in ascending id order. A ``next`` link or campaign left unread keeps
        the rows read and makes the listing incomplete with the reason "page limit".

        Args:
            account_id: The numeric ad account id or its URN.
            pivot: One of ``parser.SEGMENT_PIVOTS``.
            window: The inclusive ``(start, end)`` UTC dates of one preset window.
            max_pages: The most responses each request reads; ``None`` follows every link.
            max_campaigns: The most delivering campaigns the fallback reads; ``None`` reads
                every one.

        Returns:
            One row per campaign and demographic value, de-duplicated on that pair.

        Raises:
            ValueError: When ``pivot`` is not a segment pivot, the window starts after it
                ends, or a bound is given and is not an integer of at least 1.
        """
        if pivot not in SEGMENT_PIVOTS:
            raise ValueError(f"pivot must be one of {', '.join(SEGMENT_PIVOTS)}")
        start, end = _check_date_range(window)
        _check_limit("max_pages", max_pages)
        _check_limit("max_campaigns", max_campaigns)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_STATISTICS
        tally = _Tally()
        if self._statistics_finder_rejected:
            return await self._segments_by_campaign(
                account_urn,
                pivot,
                start,
                end,
                tally,
                max_pages=max_pages,
                max_campaigns=max_campaigns,
            )
        pairs = [
            ("q", "statistics"),
            ("pivots", restli_list(("CAMPAIGN", pivot))),
            ("timeGranularity", "ALL"),
            ("dateRange", restli_date_range(start, end)),
            ("accounts", restli_list([encode_urn(account_urn)])),
            ("fields", SEGMENT_FIELDS),
        ]
        try:
            pages = await self._follow(
                surface, f"adAnalytics?{restli_query(pairs)}", tally, max_pages=max_pages
            )
        except QueryDefect as rejected:
            if rejected.http_status != 400:
                raise
            self._statistics_finder_rejected = True
            log.warning(
                "linkedin.segments.statistics_finder_rejected",
                surface=surface,
                http_status=rejected.http_status,
                error_code=rejected.code,
            )
            try:
                return await self._segments_by_campaign(
                    account_urn,
                    pivot,
                    start,
                    end,
                    tally,
                    max_pages=max_pages,
                    max_campaigns=max_campaigns,
                )
            except QueryDefect as fallback_defect:
                raise fallback_defect from rejected
        rows = [parse_segment(element, pivot) for element in pages.elements]
        reason = _REASON_CAP if pages.capped else _REASON_PAGE_LIMIT if pages.limited else None
        return self._listing(
            surface, rows, tally, pages.interrupted_by, key=_segment_key, reason=reason
        )

    async def _delivering(
        self,
        account_urn: str,
        start: date,
        end: date,
        tally: _Tally,
        *,
        max_pages: int | None,
    ) -> tuple[tuple[str, ...], str | None, TransientError | None]:
        """Find the account's campaigns with delivery in a window (segment fallback).

        ``GET adAnalytics?q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&dateRange=(...)
        &accounts=List(...)&fields=pivotValues,impressions`` (contract
        ``ad_analytics_campaign_delivery``). A campaign reported with zero
        impressions has no demographic values and is left out. The result is cached per
        account, window and ``max_pages``, so the three pivots of one preset share one
        discovery call; an interrupted discovery is not cached.

        Returns:
            ``(campaign URNs in ascending id order, reason, interrupted_by)``, where
            ``reason`` is ``None`` for a complete discovery, else "result limit reached" or
            "page limit".
        """
        cache_key = (account_urn, start, end, max_pages)
        cached = self._delivering_campaigns.get(cache_key)
        if cached is not None:
            return cached[0], cached[1], None
        surface = _SURFACE_CAMPAIGN_DELIVERY
        pairs = [
            ("q", "analytics"),
            ("pivot", "CAMPAIGN"),
            ("timeGranularity", "ALL"),
            ("dateRange", restli_date_range(start, end)),
            ("accounts", restli_list([encode_urn(account_urn)])),
            ("fields", SEGMENT_DISCOVERY_FIELDS),
        ]
        pages = await self._follow(
            surface, f"adAnalytics?{restli_query(pairs)}", tally, max_pages=max_pages
        )
        if pages.interrupted_by is not None:
            return (), None, pages.interrupted_by
        delivering: list[str] = []
        for element in pages.elements:
            campaign = _discovered_campaign(element, surface)
            impressions = element.get("impressions")
            if impressions == 0 and not isinstance(impressions, bool):
                continue
            delivering.append(campaign)
        campaigns = tuple(
            sorted(_distinct(delivering), key=lambda urn: int(urn.removeprefix(PREFIX_CAMPAIGN)))
        )
        reason = _REASON_CAP if pages.capped else _REASON_PAGE_LIMIT if pages.limited else None
        self._delivering_campaigns[cache_key] = (campaigns, reason)
        return campaigns, reason, None

    async def _segments_by_campaign(
        self,
        account_urn: str,
        pivot: str,
        start: date,
        end: date,
        tally: _Tally,
        *,
        max_pages: int | None,
        max_campaigns: int | None,
    ) -> Listing[SegmentRow]:
        """Read one pivot per delivering campaign with the Analytics Finder (live fallback).

        Each request is the ``ad_analytics_member_segments`` contract's form and its rows are
        parsed and logged under that surface. ``max_campaigns`` reads only the first
        delivering campaigns in ascending id order; the rest make the listing incomplete
        with the reason "page limit".
        """
        surface = _SURFACE_MEMBER_SEGMENTS
        campaigns, reason, interrupted = await self._delivering(
            account_urn, start, end, tally, max_pages=max_pages
        )
        if max_campaigns is not None and len(campaigns) > max_campaigns:
            campaigns = campaigns[:max_campaigns]
            reason = reason or _REASON_PAGE_LIMIT
        rows: list[SegmentRow] = []
        if interrupted is None:
            date_range = restli_date_range(start, end)
            for campaign in campaigns:
                pairs = [
                    ("q", "analytics"),
                    ("pivot", pivot),
                    ("timeGranularity", "ALL"),
                    ("dateRange", date_range),
                    ("campaigns", restli_list([encode_urn(campaign)])),
                    ("fields", SEGMENT_FIELDS),
                ]
                pages = await self._follow(
                    surface, f"adAnalytics?{restli_query(pairs)}", tally, max_pages=max_pages
                )
                rows.extend(
                    parse_segment(element, pivot, campaign_urn=campaign, surface=surface)
                    for element in pages.elements
                )
                if pages.interrupted_by is not None:
                    interrupted = pages.interrupted_by
                    break
                if pages.capped:
                    reason = _REASON_CAP
                elif pages.limited:
                    reason = _REASON_PAGE_LIMIT
        return self._listing(surface, rows, tally, interrupted, key=_segment_key, reason=reason)

    # -- Lead Sync ----------------------------------------------------------------------

    async def list_lead_forms(
        self,
        account_id: str,
        *,
        page_size: int = LEAD_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> Listing[LeadFormRow]:
        """List the account's lead forms, each as its current version (contract ``lead_forms``).

        ``GET leadForms?q=owner&owner=(sponsoredAccount:{encoded account URN})&count={n}
        &start={k}``, paged by ``start += count`` (stop rule in :meth:`_offset_pages`). Each
        element yields the canonical ``urn:li:leadGenForm:{id}``, the current ``versionId``
        and that version's question map.

        Each returned form's version map is remembered for the account under its
        ``(canonical URN, version)``, and :meth:`list_lead_responses` decodes undecorated
        responses of that exact version with it when called without ``known_versions``.

        Args:
            account_id: The numeric ad account id or its URN.
            page_size: ``count`` per page, 1 to 100; the recovery probe sends 1.
            max_pages: Stop after this many pages; ``complete`` is then ``False`` with reason
                "page limit" when more pages may exist.

        Returns:
            The forms, de-duplicated on canonical URN.
        """
        _check_page_size(page_size, max_pages)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_LEAD_FORMS
        tally = _Tally()
        rows: list[LeadFormRow] = []

        def consume(elements: list[_Element]) -> None:
            """Parse one page of forms."""
            rows.extend(parse_lead_form(element) for element in elements)

        complete, reason, interrupted = await self._offset_pages(
            surface,
            "leadForms",
            [("q", "owner"), ("owner", _owner(account_urn))],
            (),
            page_size=page_size,
            max_pages=max_pages,
            tally=tally,
            consume=consume,
        )
        listing = self._listing(
            surface,
            rows,
            tally,
            interrupted,
            key=_form_key,
            reason=None if complete else reason,
        )
        bound = self._form_versions.setdefault(account_urn, {})
        for form in listing.rows:
            bound[(form.version_map.canonical_urn, form.version_map.version)] = form.version_map
        return listing

    async def list_lead_responses(
        self,
        account_id: str,
        submitted_range: tuple[datetime, datetime],
        *,
        known_versions: KnownVersions | None = None,
        page_size: int = LEAD_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> LeadListing:
        """List the sponsored lead responses submitted in a time range (``lead_form_responses``).

        ``GET leadFormResponses?q=owner&owner=(sponsoredAccount:{encoded account URN})
        &leadType=(leadType:SPONSORED)&limitedToTestLeads=false&submittedAtTimeRange=(start:
        {epoch ms},end:{epoch ms})&count={n}&start={k}&fields={LEAD_FIELDS}``, paged like
        :meth:`list_lead_forms`. ``LeadSyncJob`` passes the range "since the last cursor
        minus 1 h overlap" (AAP 0.3.2). The projection is sent raw, as the contract
        requires; decoding it once still returns :data:`LEAD_FIELDS` exactly.

        Each element is parsed with ``parse_lead`` against the starting maps extended by
        every map learned from an earlier element's ``form`` decoration, so a later response
        of the same version decodes even without a decoration. The starting maps are
        ``known_versions`` when given, used as passed; otherwise the maps this client holds
        for the account: every current version its :meth:`list_lead_forms` read and every
        decoration an earlier call of this method learned. Either way, each decoration
        learned here is remembered for the account. Decoding stays version-specific: a
        response's own decoration wins, a map is looked up only under the response's
        ``(canonical URN, version)``, and a version with no map ends ``no_email`` with the
        reason "unknown form version". ``limitedToTestLeads=false``
        returns test and real leads together (AAP 0.2.4): test leads are counted in
        ``test_leads_skipped`` and never returned. Responses repeated within the listing
        (offset paging can shift while new leads arrive) are kept once, the first.

        Plaintext emails never leave this method: each page is parsed as it arrives, and
        ``ParsedLead`` carries only ``email_sha256``.

        Args:
            account_id: The numeric ad account id or its URN.
            submitted_range: Timezone-aware ``(start, end)`` instants.
            known_versions: Stored form-version maps keyed by ``(canonical URN, version)``,
                used instead of the client's maps for the account; the recovery probe passes
                ``{}``. ``None`` (the default) uses the maps this client holds for the
                account.
            page_size: ``count`` per page, 1 to 100; the recovery probe sends 1.
            max_pages: Stop after this many pages (reason "page limit").

        Returns:
            The leads, the learned version maps and the test-lead count.

        Raises:
            ValueError: When an instant is naive, the range starts after it ends, or a
                paging argument is out of range.
        """
        _check_page_size(page_size, max_pages)
        time_range = _restli_time_range(*submitted_range)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_LEAD_RESPONSES
        tally = _Tally()
        bound = self._form_versions.setdefault(account_urn, {})
        known: dict[tuple[str, int], FormVersionMap] = dict(
            bound if known_versions is None else known_versions
        )
        learned: dict[tuple[str, int], FormVersionMap] = {}
        leads: list[ParsedLead] = []
        seen_ids: set[str] = set()
        counts = {"test_leads_skipped": 0, "repeats_dropped": 0}

        def consume(elements: list[_Element]) -> None:
            """Parse one page of responses, learning version maps in response order."""
            for element in elements:
                lead = parse_lead(element, known, surface=surface)
                if lead is None:
                    counts["test_leads_skipped"] += 1
                    continue
                if lead.version_map is not None:
                    version_key = (lead.version_map.canonical_urn, lead.version_map.version)
                    known[version_key] = lead.version_map
                    bound[version_key] = lead.version_map
                    learned.setdefault(version_key, lead.version_map)
                if lead.external_id in seen_ids:
                    counts["repeats_dropped"] += 1
                    continue
                seen_ids.add(lead.external_id)
                leads.append(lead)

        complete, reason, interrupted = await self._offset_pages(
            surface,
            "leadFormResponses",
            [
                ("q", "owner"),
                ("owner", _owner(account_urn)),
                ("leadType", "(leadType:SPONSORED)"),
                ("limitedToTestLeads", "false"),
                ("submittedAtTimeRange", time_range),
            ],
            [("fields", _LEAD_FIELDS_QUERY_VALUE)],
            page_size=page_size,
            max_pages=max_pages,
            tally=tally,
            consume=consume,
        )
        reason = None if complete else reason
        _log_outcome(
            surface,
            rows=len(leads),
            calls=tally.calls,
            complete=complete,
            reason=reason,
            versions_learned=len(learned),
            **counts,
        )
        return LeadListing(
            leads=tuple(leads),
            versions=tuple(learned.values()),
            test_leads_skipped=counts["test_leads_skipped"],
            complete=complete,
            reason=reason,
            calls=tally.calls,
            interrupted_by=interrupted,
        )

    async def get_lead_responses(self, account_id: str, ids: Sequence[str]) -> LeadBatch:
        """Re-fetch lead responses by id, transiently, for HubSpot matching.

        ``GET leadFormResponses?ids=List({id1},{id2},...)&owner=(sponsoredAccount:{encoded
        account URN})&leadType=(leadType:SPONSORED)&fields={LEAD_FIELDS}``, at most
        :data:`LEAD_BATCH_SIZE` ids per call and no method header (only the Rest.li 2.0
        header is required).

        Ids are sent unquoted, each percent-encoded once, following the
        ``lead_form_responses_batch`` contract rather than the plan text's quoted
        ``List("{id}")``: live 202609 answers quoted ids with HTTP 200 and empty ``results``
        and ``errors``, which would end every lead as "email no longer retrievable". The
        projection is sent raw, as for :meth:`list_lead_responses`.

        The response is ``{results: {"<id>": element}, statuses, errors}``; keys are
        normalised by percent-decoding and stripping surrounding quotes. An id present in
        ``errors``, or absent from ``results`` (live returns an unknown id as absent with
        empty ``errors``), is failed: ``sync/lead_match.py`` records a failed re-fetch and
        eventually ``error`` with reason "email no longer retrievable". A transient error
        stops the batch, and every id not yet read is failed.

        Identity: an element is found only when its own fields bind it to the request, never
        by its map key alone. Its ``id`` must equal the requested plain id,
        ``owner.sponsoredAccount`` the requested account URN, ``leadType`` ``SPONSORED`` and
        ``testLead`` the boolean ``false``. An element missing any of these, or holding
        another response, owner, lead type or a test lead, is failed the same way, so its
        email is never decoded or matched for the requested lead; the ``linkedin.listing``
        event counts these as ``identity_rejected``.

        Personal data: this is the transient path only. The caller passes each found
        element to ``parser.decode_email``, holds the plaintext only for one HubSpot
        ``contacts_by_email`` call and drops it; nothing is stored or logged, and ``found``
        is hidden from ``repr``.

        Args:
            account_id: The numeric ad account id or its URN.
            ids: Plain response ids (``ParsedLead.external_id``), never URNs; repeats are
                requested once.

        Returns:
            The found elements and the failed ids.

        Raises:
            ValueError: When an id is blank, starts with ``urn:`` or holds a ``"``.
        """
        wanted = _distinct(_checked_lead_id(lead_id) for lead_id in ids)
        account_urn = sponsored_account_urn(account_id)
        surface = _SURFACE_LEAD_RESPONSES_BATCH
        tally = _Tally()
        found: dict[str, Mapping[str, object]] = {}
        failed: set[str] = set()
        identity_rejected = 0
        interrupted: TransientError | None = None
        chunks = _chunks(wanted, LEAD_BATCH_SIZE)
        for index, chunk in enumerate(chunks):
            pairs = [
                ("ids", restli_list(quote(lead_id, safe="") for lead_id in chunk)),
                ("owner", _owner(account_urn)),
                ("leadType", "(leadType:SPONSORED)"),
                ("fields", _LEAD_FIELDS_QUERY_VALUE),
            ]
            try:
                response = await self._get(surface, "leadFormResponses", pairs, tally=tally)
            except TransientError as exc:
                interrupted = exc
                for unread in chunks[index:]:
                    failed.update(unread)
                break
            results, failed_keys = _batch_maps(_body_object(response, surface), surface)
            for lead_id in chunk:
                element = results.get(lead_id)
                if lead_id in failed_keys or element is None:
                    failed.add(lead_id)
                elif not _is_requested_lead(element, lead_id, account_urn):
                    identity_rejected += 1
                    failed.add(lead_id)
                else:
                    found[lead_id] = element
        _log_outcome(
            surface,
            rows=len(found),
            calls=tally.calls,
            complete=interrupted is None,
            reason=None if interrupted is None else interrupted.reason,
            failed=len(failed),
            identity_rejected=identity_rejected,
        )
        return LeadBatch(
            found=found,
            failed=frozenset(failed),
            interrupted_by=interrupted,
            calls=tally.calls,
        )
