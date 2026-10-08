"""Per-surface contract tests of the LinkedIn connector (AAP 0.11.2 ``test_linkedin_client.py``).

Every guarantee of the AAP 0.11.2 row is a named test below:

* **Headers**: every request carries ``Authorization: Bearer``, ``Linkedin-Version: 202609``
  and ``X-Restli-Protocol-Version: 2.0.0``; Rest.li structure stays raw and only URNs, ids and
  tokens are percent-encoded, once.
* **Lead requests**: ``leadForms`` and ``leadFormResponses`` carry the owner selector, the lead
  type, ``count``/``start`` and ``submittedAtTimeRange``; a request without its owner selector
  fails contract validation, in the contract checks and in the simulator alike.
* **Analytics**: ``adAnalytics`` carries ``dateRange``, ``timeGranularity`` and an ``accounts``
  facet, with ``pivotValues`` only in ``fields``; ``q=statistics`` with
  ``pivots=List(CAMPAIGN,...)``; the 15,000-element split; a ``next`` link followed. The live
  segment fallback's campaign-delivery and member-segments forms are requested and validated
  under their own contracts against a world that rejects ``q=statistics`` with HTTP 400.
* **Lead projection**: ``fields`` equals ``LEAD_FIELDS``; decoding reads
  ``form.content.questions[].predefinedField`` and takes the version only from
  ``versionedLeadGenFormUrn``.
* **Re-fetch**: ``ids=List(...)`` of plain ids with owner, lead type and the same ``fields``, at
  most 100 ids per call; the ``results`` map is decoded; an id in ``errors`` or missing from
  ``results`` is a failed re-fetch.
* **Forms**: a versioned form URN is parsed into canonical URN plus version; entities are listed
  in every status so the tracked set can be diffed, and a listing cut short is incomplete.
* **Traceability**: every request and parse is checked against
  ``backend/tests/contracts/linkedin/*.json``, the documented samples parse, and the redacted
  live captures under ``captured/`` are replayed when present.

Two kinds of responder serve the client, both behind the conftest's
``contract_checked_transport``, which validates every request against its contract and every
2xx body against the contract's closed response schema before the client sees it:

1. the simulator (``mock_transport_for("linkedin", make_world("default"), strict=True)``),
   for realistic live wire shapes;
2. hand-written handlers for the edge cases (cap split, ``next`` links, ``errors`` maps, 401s,
   cut-short listings), whose bodies start from the contract's documented sample or use only
   fields the contract declares.

Where the contract files record verified live 202609 behaviour that differs from the plan
text, the assertions follow the contract and the connector, and say so where they do. No test
touches the network, the database or a real credential; retries record their waits instead of
sleeping.
"""

import copy
import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from secrets import token_hex
from typing import Any, Final, cast
from urllib.parse import parse_qs, quote, unquote

import httpx
import pytest
from pydantic import SecretStr

from ads_engine.connectors.errors import AccessDenied, QueryDefect, TransientError
from ads_engine.connectors.http import RetryPolicy, build_client, send_json
from ads_engine.connectors.linkedin.auth import (
    EXPIRY_MARGIN_SECONDS,
    TOKEN_URL,
    LinkedInTokenProvider,
    StaticTokenProvider,
    TokenSource,
)
from ads_engine.connectors.linkedin.client import (
    CREATIVE_CAMPAIGN_CHUNK,
    DAILY_FIELDS,
    ENTITY_PAGE_SIZE,
    LEAD_BATCH_SIZE,
    LEAD_FIELDS,
    LEAD_PAGE_SIZE,
    LINKEDIN_VERSION,
    MAX_ANALYTICS_ELEMENTS,
    RESTLI_PROTOCOL_VERSION,
    SEGMENT_FIELDS,
    LinkedInClient,
    encode_urn,
    restli_date_range,
    restli_list,
)
from ads_engine.connectors.linkedin.parser import (
    CAMPAIGN_GROUP_SEARCH_STATUSES,
    CAMPAIGN_SEARCH_STATUSES,
    CREATIVE_INTENDED_STATUSES,
    REASON_FORM_MAP_MISMATCH,
    REASON_UNKNOWN_FORM_VERSION,
    SEGMENT_FACETS,
    SEGMENT_PIVOTS,
    FormVersionMap,
    canonical_form_urn,
    decode_email,
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
    sponsored_campaign_urn,
)
from ads_engine.settings import Settings, get_settings
from ads_engine.simulation import SIM_EMAIL_DOMAIN
from ads_engine.simulation.generator import Fault, SimulatedWorld
from ads_engine.simulation.payloads import (
    Contract,
    ContractViolation,
    RequestShape,
    identify_surface,
    parse_restli,
    validate_capture,
    validate_request,
)
from ads_engine.simulation.transport import JournalEntry

pytestmark = [pytest.mark.anyio, pytest.mark.contract]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

NOW: Final = datetime(2026, 10, 2, 12, tzinfo=UTC)
"""The simulated world's clock: noon UTC, so every simulated day up to yesterday is complete."""

TOKEN: Final = "test-token"
"""The static bearer value of every test that is not about token refresh."""

WINDOW: Final = (date(2026, 9, 4), date(2026, 10, 1))
"""A 28-day preset window ending yesterday (UTC) at :data:`NOW`."""

LEADS_FROM: Final = datetime(2026, 9, 1, tzinfo=UTC)
"""Start of the lead-response range the simulator is read over."""

LINKEDIN_SURFACES: Final = (
    "access_token",
    "ad_account",
    "ad_campaign_groups",
    "ad_campaigns",
    "creatives",
    "posts",
    "ad_analytics",
    "ad_analytics_statistics",
    "ad_analytics_campaign_delivery",
    "ad_analytics_member_segments",
    "lead_forms",
    "lead_form_responses",
    "lead_form_responses_batch",
)
"""The thirteen LinkedIn contract surfaces, one file each in ``tests/contracts/linkedin/``."""

REST_SURFACES: Final = frozenset(LINKEDIN_SURFACES) - {"access_token"}
"""The twelve surfaces under ``https://api.linkedin.com/rest/`` that carry the version headers."""

FALLBACK_SURFACES: Final = frozenset(
    {"ad_analytics_campaign_delivery", "ad_analytics_member_segments"}
)
"""The segment fallback's two surfaces: requested only after a 400 on ``q=statistics``."""

STATISTICS_REJECTED: Final = Fault(
    "linkedin",
    "ad_analytics_statistics",
    "http_status",
    status=400,
    code="FIELD_INVALID",
    message='"MEMBER_INDUSTRY" is not an enum symbol',
)
"""The live 202609 rejection of ``MEMBER_*`` values in the Statistics Finder ``pivots``."""

SAMPLE_CAMPAIGN: Final = "urn:li:sponsoredCampaign:1234567"
"""The campaign the fallback samples are attributed to (the statistics sample's campaign)."""

LEAD_FIELDS_LITERAL: Final = (
    "ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,versionedLeadGenFormUrn,"
    "id,submittedAt,testLead,formResponse,form:(hiddenFields,creationLocale,name,id,content)"
)
"""The Lead Sync migration guide's response-decoration projection, verbatim (AAP 0.2.4)."""

SAMPLE_LEAD_ACCOUNT: Final = "522529623"
"""The sponsored account of the Lead Sync documented samples."""

SAMPLE_LEAD_ID: Final = "aaaabbbb-0000-cccc-1111-dddd2222eeee-5"
"""The plain response id of the Lead Sync documented samples."""

SAMPLE_FORM_URN: Final = "urn:li:leadGenForm:3162"
"""The canonical form of the Lead Sync documented samples (version 1)."""

DEMOGRAPHIC_VALUES: Final[Mapping[str, str]] = {
    "MEMBER_JOB_FUNCTION": "urn:li:function:15",
    "MEMBER_SENIORITY": "urn:li:seniority:5",
    "MEMBER_INDUSTRY": "urn:li:industry:4",
}
"""One value URN per demographic pivot (LinkedIn's targeting facet URN forms)."""

ENCODED_URN_RE: Final = re.compile(r"urn%3Ali%3A[A-Za-z]+%3A[0-9]+")
"""A URN percent-encoded exactly once, as it appears inside Rest.li query values."""

REFRESH_CLIENT_ID: Final = "test-linkedin-client-id"
REFRESH_CLIENT_SECRET: Final = "test-linkedin-client-secret-value"
REFRESH_TOKEN: Final = "test-linkedin-refresh-token-value"
ROTATED_REFRESH_TOKEN: Final = "test-linkedin-rotated-refresh-token"
"""Obviously fake refresh-token trio and a rotated refresh token; never sent anywhere real."""

# ---------------------------------------------------------------------------------------------
# Transports, clients and request helpers
# ---------------------------------------------------------------------------------------------

type SyncResponder = Callable[[httpx.Request], httpx.Response]
type AsyncResponder = Callable[[httpx.Request], Awaitable[httpx.Response]]
type Responder = SyncResponder | AsyncResponder


@dataclass(frozen=True, slots=True)
class Served:
    """A contract-checked, recording transport and the requests it has answered, in order.

    Attributes:
        transport: The conftest ``RecordingTransport`` around ``contract_checked_transport``.
        requests: Every request the transport received, retries included.
    """

    transport: httpx.AsyncBaseTransport
    requests: list[httpx.Request]


async def _skip_sleep(seconds: float) -> None:
    """Retry sleep of every test: returns at once, so no test waits on a backoff."""
    del seconds


NO_SLEEP: Final = RetryPolicy(sleep=_skip_sleep)
"""The production retry policy (3 attempts) without real sleeps."""


def recording_policy(waits: list[float]) -> RetryPolicy:
    """Return the production retry policy recording each backoff wait instead of sleeping."""

    async def record(seconds: float) -> None:
        """Record one wait."""
        waits.append(seconds)

    return RetryPolicy(sleep=record)


@asynccontextmanager
async def open_client(
    served: Served, *, tokens: TokenSource | None = None, retry: RetryPolicy = NO_SLEEP
) -> AsyncIterator[LinkedInClient]:
    """Open ``build_client("linkedin")`` on ``served`` and yield a LinkedIn client on it."""
    async with build_client("linkedin", transport=served.transport) as http:
        yield LinkedInClient(
            http, tokens if tokens is not None else StaticTokenProvider(TOKEN), retry=retry
        )


def query_pairs(request: httpx.Request) -> list[tuple[str, str]]:
    """Split the raw query of ``request`` into ``(name, raw value)`` pairs, bytes untouched."""
    pairs: list[tuple[str, str]] = []
    for part in request.url.query.decode("ascii").split("&"):
        if part:
            name, _separator, value = part.partition("=")
            pairs.append((name, value))
    return pairs


def query_value(request: httpx.Request, name: str) -> str | None:
    """The raw (still percent-encoded) value of the query parameter ``name``, or ``None``."""
    for key, value in query_pairs(request):
        if key == name:
            return value
    return None


def surface_of(request: httpx.Request) -> str:
    """The LinkedIn contract surface ``request`` addresses."""
    return identify_surface("linkedin", request)


def of_surface(requests: Sequence[httpx.Request], surface: str) -> list[httpx.Request]:
    """The requests that address ``surface``, in order."""
    return [request for request in requests if surface_of(request) == surface]


def requested_range(request: httpx.Request) -> tuple[date, date]:
    """The inclusive ``dateRange`` of an ``adAnalytics`` request."""
    raw = query_value(request, "dateRange")
    assert raw is not None, "adAnalytics request without dateRange"
    parsed = cast("dict[str, dict[str, str]]", parse_restli(raw))
    start, end = parsed["start"], parsed["end"]
    return (
        date(int(start["year"]), int(start["month"]), int(start["day"])),
        date(int(end["year"]), int(end["month"]), int(end["day"])),
    )


def ok(body: Mapping[str, Any]) -> httpx.Response:
    """A 200 JSON response."""
    return httpx.Response(200, json=body)


def linkedin_error(status: int, code: str, message: str) -> httpx.Response:
    """LinkedIn's REST error envelope ``{status, serviceErrorCode, code, message}``."""
    return httpx.Response(
        status,
        json={"status": status, "serviceErrorCode": 100, "code": code, "message": message},
    )


def day_stamp(day: date) -> dict[str, int]:
    """A Rest.li response date ``{year, month, day}``."""
    return {"year": day.year, "month": day.month, "day": day.day}


def creative_day(creative_id: int, day: date, *, impressions: int = 100) -> dict[str, Any]:
    """One DAILY ``pivot=CREATIVE`` analytics element with the contract's declared fields."""
    return {
        "pivotValues": [f"urn:li:sponsoredCreative:{creative_id}"],
        "dateRange": {"start": day_stamp(day), "end": day_stamp(day)},
        "costInLocalCurrency": "12.5",
        "impressions": impressions,
        "clicks": 3,
        "landingPageClicks": 2,
        "oneClickLeadFormOpens": 2,
        "oneClickLeads": 1,
        "externalWebsiteConversions": 0,
    }


def lead_element(
    base: Mapping[str, Any],
    *,
    lead_id: str,
    account_id: str,
    form_id: int,
    version: int,
    answers: Sequence[tuple[int, str]],
    questions: Sequence[tuple[int, str | None]] | None,
    test_lead: bool = False,
    decoration_id: int | None = None,
) -> dict[str, Any]:
    """A lead response built on the documented sample element, with only declared fields.

    Args:
        base: The documented sample element (campaign, creative and owner decorations).
        lead_id: The plain response id.
        account_id: The owning sponsored account id.
        form_id: The lead form id inside ``versionedLeadGenFormUrn``.
        version: The form version inside ``versionedLeadGenFormUrn``.
        answers: ``(questionId, text answer)`` pairs.
        questions: The ``form`` decoration's ``(questionId, predefinedField)`` pairs, or
            ``None`` for an element without a decoration.
        test_lead: The ``testLead`` flag.
        decoration_id: ``form.id`` of the decoration; ``form_id`` when ``None``. A different
            value proves identity and version never come from the decoration.
    """
    element = copy.deepcopy(dict(base))
    element["id"] = lead_id
    element["owner"] = {"sponsoredAccount": f"urn:li:sponsoredAccount:{account_id}"}
    element["versionedLeadGenFormUrn"] = (
        f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{form_id},{version})"
    )
    element["testLead"] = test_lead
    element["formResponse"] = {
        "answers": [
            {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": text}}}
            for question_id, text in answers
        ]
    }
    element.pop("form", None)
    if questions is not None:
        element["form"] = {
            "id": form_id if decoration_id is None else decoration_id,
            "name": "White paper download form",
            "content": {
                "questions": [
                    {"questionId": question_id, "name": f"question{question_id}"}
                    | ({} if predefined is None else {"predefinedField": predefined})
                    for question_id, predefined in questions
                ]
            },
        }
    return element


def offset_page(elements: Sequence[Mapping[str, Any]], *, start: int, count: int) -> dict[str, Any]:
    """A Lead Sync ``count``/``start`` page; ``total`` is page-local, as live 202609 returns it."""
    return {
        "elements": list(elements),
        "paging": {"start": start, "count": count, "total": len(elements), "links": []},
    }


@pytest.fixture
def world(make_world: Callable[..., SimulatedWorld]) -> SimulatedWorld:
    """The ``default`` scenario's seeded world (seed 42) on a fixed clock, :data:`NOW`."""
    return make_world("default", clock=lambda: NOW)


@pytest.fixture
def serve(
    contract_checked_transport: Callable[[str, Responder], httpx.MockTransport],
    recording_transport: Callable[[httpx.MockTransport], Any],
) -> Callable[[Responder], Served]:
    """Return ``serve(handler)``: the handler behind the contract checks, recorded."""

    def _serve(handler: Responder) -> Served:
        """Wrap ``handler`` in the LinkedIn contract checks and record every request."""
        recorder = recording_transport(contract_checked_transport("linkedin", handler))
        return Served(transport=recorder, requests=recorder.requests)

    return _serve


@pytest.fixture
def simulated(
    world: SimulatedWorld,
    mock_transport_for: Callable[..., httpx.MockTransport],
    serve: Callable[[Responder], Served],
) -> Served:
    """The strict simulator for :func:`world`, behind the contract checks, recorded."""
    simulator = mock_transport_for("linkedin", world, strict=True)
    return serve(cast("Responder", simulator.handler))


@pytest.fixture
def fallback(
    world: SimulatedWorld,
    mock_transport_for: Callable[..., httpx.MockTransport],
    serve: Callable[[Responder], Served],
) -> Served:
    """The strict simulator for :func:`world` with the Statistics Finder rejected (HTTP 400).

    Every segment read therefore takes the live fallback: one campaign-delivery discovery,
    then one member-segments request per delivering campaign, each under its own contract.
    """
    rejecting = SimulatedWorld(
        world.seed,
        world.spec.adding(faults=(STATISTICS_REJECTED,)),
        clock=lambda: NOW,
        content_assets=world.content_assets,
    )
    simulator = mock_transport_for("linkedin", rejecting, strict=True)
    return serve(cast("Responder", simulator.handler))


@pytest.fixture
def sample(contract: Callable[[str, str], Contract]) -> Callable[[str], dict[str, Any]]:
    """Return ``sample(name)``: a private deep copy of a LinkedIn contract's documented sample."""

    def _sample(name: str) -> dict[str, Any]:
        """Copy the documented sample of ``linkedin/{name}.json``."""
        documented = contract("linkedin", name).response.documented_sample
        assert isinstance(documented, dict), f"linkedin.{name} has no documented sample object"
        return copy.deepcopy(cast("dict[str, Any]", documented))

    return _sample


async def exercise_every_rest_surface(client: LinkedInClient, account_id: str) -> None:
    """Call every client method once, so each contracted REST surface is requested.

    The order follows the entity sync, the analytics jobs and the lead job: the account, its
    groups, campaigns and their creatives, the creatives' posts, daily analytics, one
    demographic pivot, lead forms, lead responses and the batch re-fetch of five of them.
    """
    await client.get_ad_account(account_id)
    await client.list_campaign_groups(account_id)
    campaigns = await client.list_campaigns(account_id)
    creatives = await client.list_creatives(account_id, [row.urn for row in campaigns.rows])
    await client.get_posts([row.post_urn for row in creatives.rows if row.post_urn is not None])
    await client.analytics_daily(account_id, WINDOW)
    await client.analytics_segments(account_id, "MEMBER_JOB_FUNCTION", WINDOW)
    await client.list_lead_forms(account_id)
    leads = await client.list_lead_responses(account_id, (LEADS_FROM, NOW))
    await client.get_lead_responses(account_id, [lead.external_id for lead in leads.leads[:5]])


# ---------------------------------------------------------------------------------------------
# Headers and Rest.li encoding (AAP 0.3.3 "Headers")
# ---------------------------------------------------------------------------------------------


async def test_every_request_carries_version_headers(
    simulated: Served, fallback: Served, world: SimulatedWorld
) -> None:
    """Headers: every REST call carries the Bearer token, ``202609`` and Rest.li ``2.0.0``.

    Every client method is called once against the simulator, and one segment read against a
    world whose Statistics Finder is rejected; all twelve REST surfaces are requested (the
    segment fallback's two only in the second run), every request is a read-only ``GET``
    under ``https://api.linkedin.com/rest/``, and each passed its contract's
    ``required_headers`` check on the way.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        await exercise_every_rest_surface(client, account_id)
    async with open_client(fallback) as client:
        await client.analytics_segments(account_id, "MEMBER_JOB_FUNCTION", WINDOW)

    assert LINKEDIN_VERSION == "202609"
    assert RESTLI_PROTOCOL_VERSION == "2.0.0"
    contracted = {surface_of(request) for request in simulated.requests}
    assert contracted == REST_SURFACES - FALLBACK_SURFACES
    assert {surface_of(request) for request in fallback.requests} == {
        "ad_analytics_statistics",
        *FALLBACK_SURFACES,
    }
    for request in [*simulated.requests, *fallback.requests]:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.linkedin.com"
        assert request.url.path.startswith("/rest/")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.headers["Linkedin-Version"] == LINKEDIN_VERSION
        assert request.headers["X-Restli-Protocol-Version"] == RESTLI_PROTOCOL_VERSION
    posts = of_surface(simulated.requests, "posts")
    assert posts
    assert all(request.headers["X-RestLi-Method"] == "BATCH_GET" for request in posts)


async def test_restli_urns_encoded_structure_raw(simulated: Served, world: SimulatedWorld) -> None:
    """Headers: URNs are percent-encoded once inside raw Rest.li structure, never twice.

    In the raw query bytes of every request: each URN inside ``List(...)`` and inside
    ``owner=(sponsoredAccount:...)`` reads ``urn%3Ali%3A...``; ``(``, ``)``, ``,`` and ``:`` of
    the Rest.li structure stay literal (no ``%28``, ``%29`` or ``%2C``, and ``%3A`` only inside
    an encoded URN); no raw ``urn:`` reaches the URL; and no ``%25`` appears anywhere.
    """
    account_id = world.linkedin_account_ids[0]
    account_urn = f"urn:li:sponsoredAccount:{account_id}"
    async with open_client(simulated) as client:
        await exercise_every_rest_surface(client, account_id)

    encoded_account = encode_urn(account_urn)
    assert encoded_account == f"urn%3Ali%3AsponsoredAccount%3A{account_id}"
    seen_lists: set[str] = set()
    owners = 0
    for request in simulated.requests:
        surface = surface_of(request)
        raw = request.url.query.decode("ascii")
        assert "%25" not in raw, f"{surface}: a value is percent-encoded twice"
        assert "urn:" not in raw, f"{surface}: a raw URN reached the query"
        for structural in ("%28", "%29", "%2C", "%2c"):
            assert structural not in raw, f"{surface}: Rest.li structure encoded"
        assert "%3A" not in ENCODED_URN_RE.sub("", raw), f"{surface}: ':' encoded outside a URN"
        for name, value in query_pairs(request):
            # The batch re-fetch's ids are plain response ids, checked in the re-fetch tests.
            if name in ("accounts", "campaigns", "ids") and surface != "lead_form_responses_batch":
                seen_lists.add(name)
                assert value.startswith("List(")
                assert value.endswith(")")
                items = value[len("List(") : -1].split(",")
                assert items
                assert all(ENCODED_URN_RE.fullmatch(item) for item in items)
                # Decoding once yields the raw URNs: each item was encoded exactly once.
                assert all(unquote(item).startswith("urn:li:") for item in items)
            if name == "owner":
                owners += 1
                assert value == f"(sponsoredAccount:{encoded_account})"
    assert seen_lists == {"accounts", "campaigns", "ids"}
    lead_surfaces = ("lead_forms", "lead_form_responses", "lead_form_responses_batch")
    assert owners == sum(len(of_surface(simulated.requests, name)) for name in lead_surfaces)
    assert all(of_surface(simulated.requests, name) for name in lead_surfaces)


# ---------------------------------------------------------------------------------------------
# Entities in every status (AAP 0.3.3 "Calls")
# ---------------------------------------------------------------------------------------------


async def test_campaign_groups_all_statuses_and_page_token_paging(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    contract: Callable[[str, str], Contract],
) -> None:
    """Forms (all-status diffing): groups are searched in all seven statuses, paged by token.

    Page 1 is the contract's documented sample, whose ``metadata.nextPageToken`` makes the
    client send page 2 with ``pageToken`` (the token percent-encoded once). Page 2 carries no
    token, so paging stops there. Both requests equal the contract's literal URL.
    """
    first = sample("ad_campaign_groups")
    token = first["metadata"]["nextPageToken"]
    removed = copy.deepcopy(first["elements"][0])
    removed.update({"id": 603407685, "name": "Retired group", "status": "REMOVED"})

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve the documented page, then a last page without a token."""
        if query_value(request, "pageToken") is None:
            return ok(first)
        assert query_value(request, "pageToken") == quote(token, safe="")
        return ok({"elements": [removed]})

    served = serve(handler)
    async with open_client(served) as client:
        listing = await client.list_campaign_groups("506223315", account_currency="USD")

    assert CAMPAIGN_GROUP_SEARCH_STATUSES == (
        "ACTIVE",
        "PAUSED",
        "ARCHIVED",
        "CANCELED",
        "DRAFT",
        "PENDING_DELETION",
        "REMOVED",
    )
    literal_query = contract("linkedin", "ad_campaign_groups").request.url.split("?", 1)[1]
    assert [request.url.path for request in served.requests] == [
        "/rest/adAccounts/506223315/adCampaignGroups"
    ] * 2
    first_query, second_query = (request.url.query.decode() for request in served.requests)
    assert first_query == literal_query
    assert second_query == f"{literal_query}&pageToken={quote(token, safe='')}"
    assert query_value(served.requests[0], "search") == (
        f"(status:(values:{restli_list(CAMPAIGN_GROUP_SEARCH_STATUSES)}))"
    )
    assert query_value(served.requests[0], "pageSize") == str(ENTITY_PAGE_SIZE) == "100"
    assert listing.complete
    assert listing.reason is None
    assert listing.calls == 2
    assert [row.external_id for row in listing.rows] == ["603407684", "504293601", "603407685"]
    assert listing.rows[-1].raw_status == "REMOVED"
    assert listing.rows[-1].status == "ended"


async def test_campaigns_request_every_status_including_completed(
    simulated: Served, world: SimulatedWorld, contract: Callable[[str, str], Contract]
) -> None:
    """Forms (all-status diffing): the campaign search lists all eight statuses.

    Plan correction recorded by the connector: campaigns also have ``COMPLETED``. Omitting it
    would drop finished campaigns from the tracked set and raise false ``missing``
    discrepancies, so the request carries ``parser.CAMPAIGN_SEARCH_STATUSES`` exactly, as the
    contract's literal URL does, and the simulator's completed campaign comes back.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        listing = await client.list_campaigns(account_id)

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
    (request,) = simulated.requests
    assert request.url.path == f"/rest/adAccounts/{account_id}/adCampaigns"
    literal_query = contract("linkedin", "ad_campaigns").request.url.split("?", 1)[1]
    assert request.url.query.decode() == literal_query
    assert query_value(request, "search") == (
        f"(status:(values:{restli_list(CAMPAIGN_SEARCH_STATUSES)}))"
    )
    assert listing.complete
    raw_statuses = {row.raw_status for row in listing.rows}
    assert "COMPLETED" in raw_statuses
    assert all(row.status == "ended" for row in listing.rows if row.raw_status == "COMPLETED")


async def test_listing_cut_short_is_incomplete(
    serve: Callable[[Responder], Served], sample: Callable[[str], dict[str, Any]]
) -> None:
    """Forms: a search cut short by a transient failure is incomplete, so no ``missing``.

    Page 1 (the documented sample) carries a page token; page 2 fails with HTTP 500 on every
    one of the three attempts. The listing keeps page 1's rows and reports ``complete=False``
    with the error's reason and the error itself, which is what keeps ``sync`` from inferring
    a removal (AAP 0.11.2 "LinkedIn's all-status search cut short → no ``missing``"). Retries
    wait through the policy's backoff without sleeping.
    """
    first = sample("ad_campaigns")

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve page 1; fail every attempt at page 2."""
        if query_value(request, "pageToken") is None:
            return ok(first)
        return linkedin_error(500, "INTERNAL_SERVER_ERROR", "Internal Server Error")

    served = serve(handler)
    waits: list[float] = []
    async with open_client(served, retry=recording_policy(waits)) as client:
        listing = await client.list_campaigns("506333826", account_currency="USD")

    assert not listing.complete
    assert isinstance(listing.interrupted_by, TransientError)
    assert listing.interrupted_by.http_status == 500
    assert listing.reason == listing.interrupted_by.reason
    assert listing.reason is not None
    assert "ad_campaigns" in listing.reason
    assert [row.external_id for row in listing.rows] == ["145282384"]
    # Page 1 once, page 2 three times; each retry sequence counts as one call.
    assert len(served.requests) == 4
    assert listing.calls == 2
    assert len(waits) == 2


async def test_creatives_by_campaign_list(
    serve: Callable[[Responder], Served], sample: Callable[[str], dict[str, Any]]
) -> None:
    """Forms: creatives are read by ``campaigns=List(...)``, at most 100 campaigns per request.

    150 campaigns become two requests of 100 and 50 encoded campaign URNs, each with
    ``q=criteria``, every intended status and ``pageSize=100``; repeated campaigns are sent once.
    Each chunk's creative is kept, so the listing merges both chunks.
    """
    template = sample("creatives")["elements"][0]
    campaign_ids = [990010000 + index for index in range(150)]

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer each chunk with one creative of its first campaign and no page token."""
        listed = query_value(request, "campaigns")
        assert listed is not None
        first_campaign = unquote(listed[len("List(") : -1].split(",")[0])
        creative = copy.deepcopy(template)
        creative["campaign"] = first_campaign
        creative["id"] = f"urn:li:sponsoredCreative:{first_campaign.rsplit(':', 1)[1]}"
        creative["account"] = "urn:li:sponsoredAccount:520866471"
        return ok({"elements": [creative]})

    served = serve(handler)
    async with open_client(served) as client:
        listing = await client.list_creatives(
            "520866471", [*map(str, campaign_ids), f"urn:li:sponsoredCampaign:{campaign_ids[0]}"]
        )

    assert CREATIVE_CAMPAIGN_CHUNK == 100
    assert len(served.requests) == 2
    chunks: list[list[str]] = []
    for request in served.requests:
        assert request.url.path == "/rest/adAccounts/520866471/creatives"
        assert query_value(request, "q") == "criteria"
        assert query_value(request, "pageSize") == "100"
        assert query_value(request, "intendedStatuses") == restli_list(CREATIVE_INTENDED_STATUSES)
        listed = query_value(request, "campaigns")
        assert listed is not None
        assert listed.startswith("List(")
        chunks.append(listed[len("List(") : -1].split(","))
    assert [len(chunk) for chunk in chunks] == [100, 50]
    expected = [encode_urn(f"urn:li:sponsoredCampaign:{campaign}") for campaign in campaign_ids]
    assert [item for chunk in chunks for item in chunk] == expected
    assert listing.complete
    assert [row.campaign_urn for row in listing.rows] == [
        "urn:li:sponsoredCampaign:990010000",
        "urn:li:sponsoredCampaign:990010100",
    ]


# ---------------------------------------------------------------------------------------------
# Analytics (AAP 0.3.3 "Daily creative performance", "Demographic segments"; AAP 0.2.4 limits)
# ---------------------------------------------------------------------------------------------


async def test_analytics_daily_request_shape(
    simulated: Served, world: SimulatedWorld, contract: Callable[[str, str], Contract]
) -> None:
    """Analytics: the daily request is ``q=analytics&pivot=CREATIVE&timeGranularity=DAILY``.

    It carries ``dateRange=(start:(...),end:(...))``, an ``accounts=List(...)`` facet and the
    daily ``fields`` projection, exactly the contract's required parameters in order.
    ``pivotValues`` and ``dateRange`` are response fields selected through ``fields``;
    ``pivotValues`` is never a request filter, and the projection stays within 20 fields.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        listing = await client.analytics_daily(account_id, WINDOW)

    loaded = contract("linkedin", "ad_analytics")
    (request,) = simulated.requests
    assert request.url.path == "/rest/adAnalytics"
    pairs = query_pairs(request)
    assert [name for name, _value in pairs] == list(loaded.request.required_query)
    values = dict(pairs)
    assert values["q"] == "analytics"
    assert values["pivot"] == "CREATIVE"
    assert values["timeGranularity"] == "DAILY"
    assert values["dateRange"] == restli_date_range(*WINDOW)
    assert values["dateRange"] == "(start:(year:2026,month:9,day:4),end:(year:2026,month:10,day:1))"
    assert values["accounts"] == f"List(urn%3Ali%3AsponsoredAccount%3A{account_id})"
    assert values["fields"] == DAILY_FIELDS == loaded.request.fields
    assert DAILY_FIELDS == (
        "pivotValues,dateRange,costInLocalCurrency,impressions,clicks,landingPageClicks,"
        "oneClickLeadFormOpens,oneClickLeads,externalWebsiteConversions"
    )
    for name, value in pairs:
        if name != "fields":
            assert "pivotValues" not in name
            assert "pivotValues" not in value
    assert [name for name, _value in pairs].count("dateRange") == 1
    max_fields = loaded.request.limit("max_fields")
    assert max_fields == 20
    assert len(DAILY_FIELDS.split(",")) <= max_fields
    assert listing.complete
    assert listing.rows
    assert all(WINDOW[0] <= row.date <= WINDOW[1] for row in listing.rows)


async def test_analytics_daily_chunks_below_15000(simulated: Served, world: SimulatedWorld) -> None:
    """Analytics: creatives times days stays below 15,000 elements in every request.

    180 days for 500 expected creatives would be 90,000 elements in one response, above
    ``MAX_ANALYTICS_ELEMENTS``. The client cuts the range into contiguous date chunks of
    ``14,999 // 500 = 29`` days, read in ascending order, each below the cap.
    """
    account_id = world.linkedin_account_ids[0]
    end = WINDOW[1]
    start = end - timedelta(days=179)
    creatives = 500
    assert creatives * 180 > MAX_ANALYTICS_ELEMENTS
    async with open_client(simulated) as client:
        listing = await client.analytics_daily(
            account_id, (start, end), expected_creatives=creatives
        )

    ranges = [requested_range(request) for request in simulated.requests]
    assert len(ranges) == 7
    assert ranges[0][0] == start
    assert ranges[-1][1] == end
    for (_previous_start, previous_end), (next_start, _next_end) in pairwise(ranges):
        assert next_start == previous_end + timedelta(days=1)
    for chunk_start, chunk_end in ranges:
        days = (chunk_end - chunk_start).days + 1
        assert days <= (MAX_ANALYTICS_ELEMENTS - 1) // creatives
        assert creatives * days < MAX_ANALYTICS_ELEMENTS
    assert listing.complete
    assert listing.calls == len(ranges)
    assert all(start <= row.date <= end for row in listing.rows)


async def test_analytics_response_at_cap_is_split(serve: Callable[[Responder], Served]) -> None:
    """Analytics: a response at the 15,000-element cap is split; a single day is incomplete.

    A multi-day response holding exactly 15,000 elements and no ``next`` link may be
    truncated: its rows are discarded and the two halves are requested, in order, so the
    final rows are their union and the listing is complete. One day still at the cap keeps its
    rows and makes the listing incomplete, because a day cannot be split further.
    """
    full = (date(2026, 9, 1), date(2026, 9, 4))
    halves = [(date(2026, 9, 1), date(2026, 9, 2)), (date(2026, 9, 3), date(2026, 9, 4))]
    creatives = (990001000, 990001001)

    def half_elements(chunk_start: date, chunk_end: date) -> list[dict[str, Any]]:
        """Two creatives on each day of a half."""
        days = (chunk_end - chunk_start).days + 1
        return [
            creative_day(creative, chunk_start + timedelta(days=offset))
            for creative in creatives
            for offset in range(days)
        ]

    def splitting(request: httpx.Request) -> httpx.Response:
        """Return the cap for the full range and a few rows for each half."""
        chunk = requested_range(request)
        if chunk == full:
            capped = creative_day(creatives[0], full[0])
            return ok({"elements": [capped] * MAX_ANALYTICS_ELEMENTS})
        assert chunk in halves
        return ok({"elements": half_elements(*chunk)})

    served = serve(splitting)
    async with open_client(served) as client:
        listing = await client.analytics_daily("990000001", full)

    assert [requested_range(request) for request in served.requests] == [full, *halves]
    expected = {
        (f"urn:li:sponsoredCreative:{creative}", day)
        for chunk_start, chunk_end in halves
        for creative in creatives
        for day in (chunk_start, chunk_end)
    }
    assert {(row.creative_urn, row.date) for row in listing.rows} == expected
    assert len(listing.rows) == len(expected) == 8
    assert listing.complete
    assert listing.reason is None
    assert listing.calls == 3

    one_day = (date(2026, 9, 1), date(2026, 9, 1))

    def single_day_cap(request: httpx.Request) -> httpx.Response:
        """Return 15,000 distinct creatives on one day."""
        assert requested_range(request) == one_day
        return ok(
            {
                "elements": [
                    creative_day(990100000 + index, one_day[0])
                    for index in range(MAX_ANALYTICS_ELEMENTS)
                ]
            }
        )

    capped_day = serve(single_day_cap)
    async with open_client(capped_day) as client:
        at_cap = await client.analytics_daily("990000001", one_day)

    assert len(capped_day.requests) == 1
    assert len(at_cap.rows) == MAX_ANALYTICS_ELEMENTS
    assert not at_cap.complete
    assert at_cap.reason == "result limit reached at finest partition (one day)"
    assert at_cap.interrupted_by is None


async def test_analytics_next_link_followed(serve: Callable[[Responder], Served]) -> None:
    """Analytics: a ``next`` paging link is followed at once and its rows are merged.

    The first response carries ``paging.links`` with ``rel=next`` (a ``/rest/`` href adding
    ``start`` and ``count``, the continuation parameters the contract admits); the client
    requests it and stops on the page without a ``next`` link.
    """
    chunk = (date(2026, 9, 1), date(2026, 9, 2))
    first_page = [creative_day(990001000, chunk[0]), creative_day(990001000, chunk[1])]
    second_page = [creative_day(990001001, chunk[0]), creative_day(990001001, chunk[1])]

    def paged(request: httpx.Request) -> httpx.Response:
        """Serve page 1 with a next link to page 2."""
        if query_value(request, "start") is None:
            href = f"/rest/adAnalytics?{request.url.query.decode()}&start=2&count=2"
            return ok(
                {
                    "elements": first_page,
                    "paging": {
                        "start": 0,
                        "count": 2,
                        "links": [{"rel": "next", "href": href, "type": "application/json"}],
                    },
                }
            )
        assert query_value(request, "start") == "2"
        return ok({"elements": second_page, "paging": {"start": 2, "count": 2, "links": []}})

    served = serve(paged)
    async with open_client(served) as client:
        listing = await client.analytics_daily("990000001", chunk)

    assert len(served.requests) == 2
    first_query = served.requests[0].url.query.decode()
    assert served.requests[1].url.path == "/rest/adAnalytics"
    assert served.requests[1].url.query.decode() == f"{first_query}&start=2&count=2"
    assert listing.complete
    assert listing.calls == 2
    assert {(row.creative_urn, row.date) for row in listing.rows} == {
        (f"urn:li:sponsoredCreative:{creative}", day)
        for creative in (990001000, 990001001)
        for day in chunk
    }


@pytest.mark.parametrize("pivot", SEGMENT_PIVOTS)
async def test_statistics_finder_for_segments(
    pivot: str,
    simulated: Served,
    world: SimulatedWorld,
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    contract: Callable[[str, str], Contract],
) -> None:
    """Analytics: segments use ``q=statistics`` with ``pivots=List(CAMPAIGN,{pivot})``.

    Each demographic pivot is one ``timeGranularity=ALL`` request over the exact preset window,
    with the ``accounts`` facet and the segment projection. A metric the response omits is
    unavailable (``None``), never 0: the documented sample, given the demographic value its
    campaign-only ``pivotValues`` lacks, returns no cost, leads or website conversions.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        listing = await client.analytics_segments(account_id, pivot, WINDOW)

    loaded = contract("linkedin", "ad_analytics_statistics")
    (request,) = simulated.requests
    assert surface_of(request) == "ad_analytics_statistics"
    pairs = query_pairs(request)
    assert [name for name, _value in pairs] == list(loaded.request.required_query)
    values = dict(pairs)
    assert values["q"] == "statistics"
    assert values["pivots"] == f"List(CAMPAIGN,{pivot})"
    assert values["timeGranularity"] == "ALL"
    assert values["dateRange"] == restli_date_range(*WINDOW)
    assert values["accounts"] == f"List({encode_urn(f'urn:li:sponsoredAccount:{account_id}')})"
    assert values["fields"] == SEGMENT_FIELDS == loaded.request.fields
    max_pivots = loaded.request.limit("max_pivots")
    assert max_pivots is not None
    assert len(values["pivots"][len("List(") : -1].split(",")) <= max_pivots
    assert listing.complete
    assert listing.rows
    prefix = DEMOGRAPHIC_VALUES[pivot].rsplit(":", 1)[0] + ":"
    for row in listing.rows:
        assert row.facet == SEGMENT_FACETS[pivot]
        assert row.value.startswith(prefix)
        assert row.campaign_urn.startswith("urn:li:sponsoredCampaign:")

    documented = sample("ad_analytics_statistics")
    for element in documented["elements"]:
        element["pivotValues"] = [*element["pivotValues"], DEMOGRAPHIC_VALUES[pivot]]
    served = serve(lambda _request: ok(documented))
    async with open_client(served) as client:
        from_sample = await client.analytics_segments("1234567", pivot, WINDOW)

    (row,) = from_sample.rows
    assert row.campaign_urn == "urn:li:sponsoredCampaign:1234567"
    assert row.value == DEMOGRAPHIC_VALUES[pivot]
    assert (row.impressions, row.clicks) == (54494, 177)
    assert row.cost is None
    assert row.leads is None
    assert row.website_conversions is None


async def test_daily_rows_are_utc_dates(
    serve: Callable[[Responder], Served], sample: Callable[[str], dict[str, Any]]
) -> None:
    """Analytics: a daily row's ``date`` is its ``dateRange.start``, a UTC calendar date.

    The documented sample is served with its ``MEMBER_COMPANY`` pivot value replaced by a
    creative URN, because daily rows key ``fact_creative_daily`` and the parser accepts only a
    creative pivot. The dates come back exactly as returned (LinkedIn days are UTC, never
    rebucketed), and metrics the elements omit are ``None``.
    """
    documented = sample("ad_analytics")
    creative = "urn:li:sponsoredCreative:121608865"
    for element in documented["elements"]:
        element["pivotValues"] = [creative]
    served = serve(lambda _request: ok(documented))
    requested = (date(2024, 5, 28), date(2024, 5, 29))
    async with open_client(served) as client:
        listing = await client.analytics_daily("990000001", requested)

    assert requested_range(served.requests[0]) == requested
    assert [row.date for row in listing.rows] == [date(2024, 5, 28), date(2024, 5, 29)]
    assert all(type(row.date) is date for row in listing.rows)
    assert all(row.creative_urn == creative for row in listing.rows)
    assert [row.cost for row in listing.rows] == [Decimal("0.0"), Decimal("19.91833")]
    assert [row.impressions for row in listing.rows] == [6, 165]
    assert [row.landing_page_clicks for row in listing.rows] == [0, 11]
    assert [row.website_conversions for row in listing.rows] == [0, 0]
    assert all(row.clicks is None for row in listing.rows)
    assert all(row.leads is None and row.form_opens is None for row in listing.rows)
    assert listing.complete


# ---------------------------------------------------------------------------------------------
# Lead Sync (AAP 0.3.3 "Lead forms", "Lead responses", "Email re-fetch", "Form identity")
# ---------------------------------------------------------------------------------------------


def epoch_ms(instant: datetime) -> int:
    """Epoch milliseconds of an aware instant, with integer arithmetic."""
    return (instant - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


async def test_lead_forms_owner_and_count_start_paging(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    contract: Callable[[str, str], Contract],
) -> None:
    """Lead requests: ``leadForms?q=owner`` pages by ``count`` and ``start``.

    The first page is ``start=0``, the next ``start=100``, each with the owner selector
    ``(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{id})``. Plan correction recorded in
    the ``lead_forms`` and ``lead_form_responses`` contracts: ``paging.total`` counts only the
    elements of the current page (live 202609), so paging stops on a short page without a
    ``next`` link, not on ``start + count >= total``. Page 1 below is full with a page-local
    ``total`` of 100, which that plan rule would have read as the end, losing page 2.
    """
    template = sample("lead_forms")["elements"][0]

    def form(index: int) -> dict[str, Any]:
        """One form element built on the documented sample."""
        element = copy.deepcopy(template)
        element["id"] = 990000100 + index
        element["versionId"] = 1 + index % 3
        element["name"] = f"White paper form {index}"
        return element

    def handler(request: httpx.Request) -> httpx.Response:
        """A full first page, then a short last page."""
        start = int(query_value(request, "start") or "-1")
        count = int(query_value(request, "count") or "-1")
        if start == 0:
            # A page-local total of 100: start + count >= total here, yet a page follows. The
            # contract's stop rule (short page, no next link) applies, not the plan's.
            return ok(offset_page([form(index) for index in range(100)], start=0, count=count))
        assert start == 100
        return ok(offset_page([form(index) for index in range(100, 120)], start=100, count=count))

    served = serve(handler)
    async with open_client(served) as client:
        listing = await client.list_lead_forms("990000001")

    owner = "(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A990000001)"
    assert [request.url.query.decode() for request in served.requests] == [
        f"q=owner&owner={owner}&count={LEAD_PAGE_SIZE}&start=0",
        f"q=owner&owner={owner}&count={LEAD_PAGE_SIZE}&start=100",
    ]
    assert [name for name, _value in query_pairs(served.requests[0])] == list(
        contract("linkedin", "lead_forms").request.required_query
    )
    assert all(request.url.path == "/rest/leadForms" for request in served.requests)
    assert listing.complete
    assert listing.calls == 2
    assert len(listing.rows) == 120
    assert listing.rows[0].canonical_urn == "urn:li:leadGenForm:990000100"
    assert [row.current_version for row in listing.rows[:3]] == [1, 2, 3]
    assert all(row.version_map.version == row.current_version for row in listing.rows)


async def test_lead_responses_selectors(
    simulated: Served, world: SimulatedWorld, contract: Callable[[str, str], Contract]
) -> None:
    """Lead requests: responses carry owner, lead type, test-lead flag, range, paging, fields.

    Every page is ``q=owner&owner=(sponsoredAccount:...)&leadType=(leadType:SPONSORED)
    &limitedToTestLeads=false&submittedAtTimeRange=(start:{ms},end:{ms})&count=100&start={k}
    &fields=...``, the range in epoch milliseconds of the requested instants and ``start``
    advancing by 100. ``limitedToTestLeads=false`` returns test leads with real ones; they are
    counted and dropped, never returned.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        listing = await client.list_lead_responses(account_id, (LEADS_FROM, NOW))

    loaded = contract("linkedin", "lead_form_responses")
    requests = simulated.requests
    assert len(requests) >= 2
    assert all(surface_of(request) == "lead_form_responses" for request in requests)
    expected_range = f"(start:{epoch_ms(LEADS_FROM)},end:{epoch_ms(NOW)})"
    for page, request in enumerate(requests):
        pairs = query_pairs(request)
        assert [name for name, _value in pairs] == list(loaded.request.required_query)
        assert pairs == [
            ("q", "owner"),
            ("owner", f"(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{account_id})"),
            ("leadType", "(leadType:SPONSORED)"),
            ("limitedToTestLeads", "false"),
            ("submittedAtTimeRange", expected_range),
            ("count", str(LEAD_PAGE_SIZE)),
            ("start", str(page * LEAD_PAGE_SIZE)),
            ("fields", LEAD_FIELDS),
        ]
    assert listing.complete
    assert listing.calls == len(requests)
    submitted = world.linkedin_lead_responses(account_id, epoch_ms(LEADS_FROM), epoch_ms(NOW))
    real = {response.id for response in submitted if not response.test_lead}
    assert listing.test_leads_skipped == len(submitted) - len(real) > 0
    assert {lead.external_id for lead in listing.leads} == real
    assert len(listing.leads) == len(real)
    assert all(LEADS_FROM <= lead.submitted_at <= NOW for lead in listing.leads)


async def test_request_without_owner_fails_contract_validation(
    contract: Callable[[str, str], Contract],
    contract_checked_transport: Callable[[str, Responder], httpx.MockTransport],
    mock_transport_for: Callable[..., httpx.MockTransport],
    world: SimulatedWorld,
) -> None:
    """Lead requests: a ``leadFormResponses`` request without ``owner`` breaks its contract.

    It fails ``validate_request`` and the conftest's contract-checked transport before any
    handler runs. In simulated mode it fails too (AAP 0.3.3 "a request missing a required
    selector fails in simulated mode too"): the strict simulator raises the same
    ``ContractViolation``, and the default simulator answers LinkedIn's HTTP 400, which
    ``send_json`` raises as a defect, once, never retried.
    """
    url = (
        "https://api.linkedin.com/rest/leadFormResponses?q=owner&leadType=(leadType:SPONSORED)"
        f"&limitedToTestLeads=false&submittedAtTimeRange=(start:{epoch_ms(LEADS_FROM)},"
        f"end:{epoch_ms(NOW)})&count=100&start=0&fields={LEAD_FIELDS}"
    )
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Linkedin-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": RESTLI_PROTOCOL_VERSION,
    }
    request = httpx.Request("GET", url, headers=headers)
    assert surface_of(request) == "lead_form_responses"

    with pytest.raises(ContractViolation) as violation:
        validate_request(contract("linkedin", "lead_form_responses"), request)
    assert violation.value.surface == "linkedin.lead_form_responses"
    assert "query parameter owner: missing (required)" in violation.value.errors

    handled: list[httpx.Request] = []

    def never(inner: httpx.Request) -> httpx.Response:
        """Must not run: the request is rejected before the handler."""
        handled.append(inner)
        return ok({})

    async with httpx.AsyncClient(transport=contract_checked_transport("linkedin", never)) as http:
        with pytest.raises(AssertionError, match="owner"):
            await http.send(request)
    assert handled == []

    strict = mock_transport_for("linkedin", world, strict=True)
    async with httpx.AsyncClient(transport=strict) as http:
        with pytest.raises(ContractViolation) as simulated_violation:
            await http.send(request)
    assert "query parameter owner: missing (required)" in simulated_violation.value.errors

    journal: list[JournalEntry] = []
    waits: list[float] = []
    lenient = mock_transport_for("linkedin", world, journal=journal)
    async with build_client("linkedin", transport=lenient) as http:
        with pytest.raises(QueryDefect) as defect:
            await send_json(
                http,
                lambda: http.build_request("GET", url, headers=headers),
                source="linkedin",
                endpoint="lead_form_responses",
                policy=recording_policy(waits),
            )
    assert defect.value.http_status == 400
    assert journal == [
        JournalEntry("linkedin", "lead_form_responses", "GET", "/rest/leadFormResponses", 400, None)
    ]
    assert waits == []


async def test_lead_fields_projection_verbatim_encoded_once(
    simulated: Served, world: SimulatedWorld, contract: Callable[[str, str], Contract]
) -> None:
    """Lead projection: ``fields`` is ``LEAD_FIELDS`` verbatim, in its single URL encoding.

    Both Lead Sync requests (owner finder and batch re-fetch) send the migration guide's
    projection; decoding the raw query value once yields it exactly, and it equals both lead
    contracts' ``request.fields``.

    Contract over plan text: the plan writes the value as ``quote(LEAD_FIELDS, safe="")``.
    The contracts record that live 202609 rejects that fully encoded value with HTTP 400
    ("Projected field ... not present in schema"), while RFC 3986 allows ``,``, ``:``, ``(``
    and ``)`` in a query, so the raw value already is its one URL encoding. The test asserts
    the contract's form and proves the plan's form fails contract validation.
    """
    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        page = await client.list_lead_responses(
            account_id, (LEADS_FROM, NOW), page_size=1, max_pages=1
        )
        await client.get_lead_responses(account_id, [lead.external_id for lead in page.leads])

    assert LEAD_FIELDS == LEAD_FIELDS_LITERAL
    for name in ("lead_form_responses", "lead_form_responses_batch"):
        assert contract("linkedin", name).request.fields == LEAD_FIELDS_LITERAL
    finder = of_surface(simulated.requests, "lead_form_responses")
    batch = of_surface(simulated.requests, "lead_form_responses_batch")
    assert len(finder) == 1
    assert len(batch) == 1
    for request in (*finder, *batch):
        raw = query_value(request, "fields")
        # The contract's raw form, not the plan's quote(LEAD_FIELDS, safe=""): live 202609
        # rejects the fully encoded projection, and the raw value is its single URL encoding.
        assert raw == LEAD_FIELDS_LITERAL
        assert unquote(raw) == LEAD_FIELDS_LITERAL
        assert "%" not in raw

    encoded = quote(LEAD_FIELDS, safe="")
    assert unquote(encoded) == LEAD_FIELDS
    plan_form = httpx.Request(
        "GET",
        str(finder[0].url).replace(f"fields={LEAD_FIELDS}", f"fields={encoded}"),
        headers=dict(finder[0].headers),
    )
    with pytest.raises(ContractViolation) as violation:
        validate_request(contract("linkedin", "lead_form_responses"), plan_form)
    assert any("must be sent raw" in error for error in violation.value.errors)


async def test_documented_sample_decodes(
    serve: Callable[[Responder], Served], sample: Callable[[str], dict[str, Any]]
) -> None:
    """Lead projection: the documented sample decodes; email ids come from the decoration.

    Served verbatim, the Lead Sync sample yields its lead with the creative and campaign it
    names and the canonical form and version parsed from ``versionedLeadGenFormUrn``; it has
    no ``form`` decoration and no stored map, so its email is ``no_email`` "unknown form
    version". A copy decorated with ``form.content.questions[].predefinedField == "EMAIL"``
    decodes the email answer, hashed only. The decoration's ``form.id`` is deliberately
    another number and a second copy names version 3 in its URN: identity and version come
    only from the URN. A ``testLead: true`` copy is dropped, and no plaintext email remains on
    any parsed lead.
    """
    documented = sample("lead_form_responses")
    base = documented["elements"][0]
    window = (datetime(2023, 6, 1, tzinfo=UTC), datetime(2023, 7, 1, tzinfo=UTC))
    verbatim = serve(lambda _request: ok(documented))
    async with open_client(verbatim) as client:
        listing = await client.list_lead_responses(
            SAMPLE_LEAD_ACCOUNT, window, known_versions={}, page_size=10
        )

    (lead,) = listing.leads
    assert lead.external_id == SAMPLE_LEAD_ID
    assert lead.account_urn == f"urn:li:sponsoredAccount:{SAMPLE_LEAD_ACCOUNT}"
    assert lead.campaign_urn == "urn:li:sponsoredCampaign:367378525"
    assert lead.creative_urn == "urn:li:sponsoredCreative:121608865"
    assert lead.submitted_at == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        milliseconds=1686182358881
    )
    assert (lead.lead_form_urn, lead.lead_form_version) == (SAMPLE_FORM_URN, 1)
    assert (lead.email_status, lead.email_reason) == ("no_email", REASON_UNKNOWN_FORM_VERSION)
    assert lead.email_sha256 is None
    assert listing.complete
    assert listing.test_leads_skipped == 0

    email = "  Jordan.Lee@Example.COM "
    normalised = "jordan.lee@example.com"
    shared = {
        "base": base,
        "account_id": SAMPLE_LEAD_ACCOUNT,
        "form_id": 3162,
        "answers": [(1, email), (2, "Bracknell")],
        "questions": [(1, "EMAIL"), (2, None)],
        "decoration_id": 31629999,
    }
    decorated = lead_element(lead_id=SAMPLE_LEAD_ID, version=1, **shared)
    later_version = lead_element(
        lead_id="aaaabbbb-0000-cccc-1111-dddd2222eeee-6", version=3, **shared
    )
    test_lead = lead_element(
        lead_id="aaaabbbb-0000-cccc-1111-dddd2222eeee-7", version=1, test_lead=True, **shared
    )
    page = offset_page([decorated, later_version, test_lead], start=0, count=10)
    served = serve(lambda _request: ok(page))
    async with open_client(served) as client:
        decoded = await client.list_lead_responses(
            SAMPLE_LEAD_ACCOUNT, window, known_versions={}, page_size=10
        )

    assert decoded.test_leads_skipped == 1
    assert [lead.external_id for lead in decoded.leads] == [
        SAMPLE_LEAD_ID,
        "aaaabbbb-0000-cccc-1111-dddd2222eeee-6",
    ]
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
    for parsed, version in zip(decoded.leads, (1, 3), strict=True):
        assert parsed.lead_form_urn == SAMPLE_FORM_URN
        assert parsed.lead_form_version == version
        assert (parsed.email_status, parsed.email_reason) == ("ok", None)
        assert parsed.email_sha256 == digest
        assert parsed.version_map == FormVersionMap(
            canonical_urn=SAMPLE_FORM_URN,
            version=version,
            email_question_ids=(1,),
            question_ids=frozenset({1, 2}),
        )
        for value in (repr(parsed), *(str(getattr(parsed, name)) for name in parsed.__slots__)):
            assert "jordan.lee" not in value.lower()
            assert "@" not in value
    assert {(m.canonical_urn, m.version) for m in decoded.versions} == {
        (SAMPLE_FORM_URN, 1),
        (SAMPLE_FORM_URN, 3),
    }
    assert "jordan.lee" not in repr(decoded).lower()


async def test_versioned_form_urn_parsed(simulated: Served, world: SimulatedWorld) -> None:
    """Forms: a versioned form URN splits into the canonical URN and the integer version.

    Every form reference maps to one canonical ``urn:li:leadGenForm:{id}``, so all versions of
    a form join one content asset; a malformed URN is a ``validation`` defect. Through the
    client, every simulated lead's form and version equal the world's record.
    """
    versioned = "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101,3)"
    assert parse_versioned_form_urn(versioned) == ("urn:li:leadGenForm:990000101", 3)
    for reference in (versioned, "urn:li:adForm:990000101", "urn:li:leadGenForm:990000101"):
        assert canonical_form_urn(reference) == "urn:li:leadGenForm:990000101"
    for malformed in (
        "",
        "urn:li:leadGenForm:990000101",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:form,3)",
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101,-1)",
        "urn:li:versionedLeadGenForm:(urn:li:adForm:990000101,3)",
    ):
        with pytest.raises(QueryDefect) as defect:
            parse_versioned_form_urn(malformed)
        assert (defect.value.code, defect.value.source) == ("validation", "linkedin")

    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        listing = await client.list_lead_responses(account_id, (LEADS_FROM, NOW))
    records = {
        response.id: response
        for response in world.linkedin_lead_responses(
            account_id, epoch_ms(LEADS_FROM), epoch_ms(NOW)
        )
    }
    assert listing.leads
    for lead in listing.leads:
        record = records[lead.external_id]
        assert lead.lead_form_urn == f"urn:li:leadGenForm:{record.form_id}"
        assert lead.lead_form_version == record.form_version


async def test_form_map_mismatch_and_unknown_version(
    serve: Callable[[Responder], Served], sample: Callable[[str], dict[str, Any]]
) -> None:
    """Forms: a response is decoded only with its own version's question map.

    * a decoration that lacks an answered ``questionId`` does not describe the response's
      version: ``no_email`` "form map mismatch", and no map is learned from it;
    * a stored map whose questions lack an answered id is a mismatch the same way;
    * a version with no decoration and no stored map is ``no_email`` "unknown form version",
      even though a map of another version of the same form is known;
    * an undecorated response of a known version decodes with that version's stored map.
    """
    base = sample("lead_form_responses")["elements"][0]
    known = {
        (SAMPLE_FORM_URN, 1): FormVersionMap(
            canonical_urn=SAMPLE_FORM_URN,
            version=1,
            email_question_ids=(1,),
            question_ids=frozenset({1, 2}),
        )
    }
    common = {"base": base, "account_id": SAMPLE_LEAD_ACCOUNT, "form_id": 3162}
    elements = [
        lead_element(
            lead_id="lead-map-mismatch-1",
            version=2,
            answers=[(1, "ana@example.com"), (7, "not on this version")],
            questions=[(1, "EMAIL")],
            **common,
        ),
        lead_element(
            lead_id="lead-stored-mismatch-2",
            version=1,
            answers=[(1, "ben@example.com"), (9, "unknown question")],
            questions=None,
            **common,
        ),
        lead_element(
            lead_id="lead-unknown-version-3",
            version=7,
            answers=[(1, "cleo@example.com")],
            questions=None,
            **common,
        ),
        lead_element(
            lead_id="lead-known-version-4",
            version=1,
            answers=[(1, "dev@example.com"), (2, "Leeds")],
            questions=None,
            **common,
        ),
    ]
    served = serve(lambda _request: ok(offset_page(elements, start=0, count=10)))
    window = (datetime(2023, 6, 1, tzinfo=UTC), datetime(2023, 7, 1, tzinfo=UTC))
    async with open_client(served) as client:
        listing = await client.list_lead_responses(
            SAMPLE_LEAD_ACCOUNT, window, known_versions=known, page_size=10
        )

    outcomes = {
        lead.external_id: (lead.lead_form_version, lead.email_status, lead.email_reason)
        for lead in listing.leads
    }
    assert REASON_FORM_MAP_MISMATCH == "form map mismatch"
    assert REASON_UNKNOWN_FORM_VERSION == "unknown form version"
    assert outcomes == {
        "lead-map-mismatch-1": (2, "no_email", REASON_FORM_MAP_MISMATCH),
        "lead-stored-mismatch-2": (1, "no_email", REASON_FORM_MAP_MISMATCH),
        "lead-unknown-version-3": (7, "no_email", REASON_UNKNOWN_FORM_VERSION),
        "lead-known-version-4": (1, "ok", None),
    }
    by_id = {lead.external_id: lead for lead in listing.leads}
    assert by_id["lead-known-version-4"].email_sha256 == (
        hashlib.sha256(b"dev@example.com").hexdigest()
    )
    assert all(
        by_id[lead_id].email_sha256 is None and by_id[lead_id].version_map is None
        for lead_id in ("lead-map-mismatch-1", "lead-stored-mismatch-2", "lead-unknown-version-3")
    )
    assert listing.versions == ()


async def test_batch_refetch_plain_ids_and_limits(
    serve: Callable[[Responder], Served], contract: Callable[[str, str], Contract]
) -> None:
    """Re-fetch: ``ids=List(...)`` of plain ids, at most 100 per call, owner, type and fields.

    250 ids become three calls of 100, 100 and 50, each also carrying the owner selector,
    ``leadType=(leadType:SPONSORED)`` and ``fields`` equal to ``LEAD_FIELDS``. Every id is a
    plain response id, never a URN, percent-encoded once inside raw ``List(...)`` structure.
    Ids absent from ``results`` are failed re-fetches.

    Contract over plan text: the plan quotes each id (``List("{id}")``). The
    ``lead_form_responses_batch`` contract records that live 202609 answers quoted ids with
    HTTP 200 and empty ``results`` and ``errors``, which would end every lead as "email no
    longer retrievable", so ids are sent unquoted and no ``%22`` may appear.
    """
    ids = [f"aaaabbbb-0000-cccc-1111-dddd2222{index:04x}-5" for index in range(250)]
    served = serve(lambda _request: ok({"results": {}, "statuses": {}, "errors": {}}))
    async with open_client(served) as client:
        batch = await client.get_lead_responses(SAMPLE_LEAD_ACCOUNT, [*ids, ids[0]])

    loaded = contract("linkedin", "lead_form_responses_batch")
    assert LEAD_BATCH_SIZE == loaded.request.limit("max_ids") == 100
    assert len(served.requests) == 3
    sent: list[str] = []
    for request in served.requests:
        assert surface_of(request) == "lead_form_responses_batch"
        pairs = query_pairs(request)
        assert [name for name, _value in pairs] == list(loaded.request.required_query)
        values = dict(pairs)
        assert values["owner"] == (
            f"(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{SAMPLE_LEAD_ACCOUNT})"
        )
        assert values["leadType"] == "(leadType:SPONSORED)"
        assert values["fields"] == LEAD_FIELDS
        listed = values["ids"]
        assert listed.startswith("List(")
        assert listed.endswith(")")
        # Unquoted ids, per the batch contract rather than the plan's List("{id}"): live
        # 202609 answers quoted ids with empty results, failing every re-fetch.
        assert "%22" not in listed
        assert '"' not in listed
        items = listed[len("List(") : -1].split(",")
        assert all(not unquote(item).startswith("urn:") for item in items)
        sent.extend(unquote(item) for item in items)
        assert all(item == quote(unquote(item), safe="") for item in items)
    assert [len(chunk) for chunk in (sent[:100], sent[100:200], sent[200:])] == [100, 100, 50]
    assert sent == ids
    assert batch.found == {}
    assert batch.failed == frozenset(ids)
    assert batch.interrupted_by is None
    assert batch.calls == 3

    async with open_client(served) as client:
        for invalid in ("urn:li:leadFormResponse:1", '"quoted-id"', " "):
            with pytest.raises(ValueError, match="lead response id"):
                await client.get_lead_responses(SAMPLE_LEAD_ACCOUNT, [invalid])
    assert len(served.requests) == 3


async def test_batch_refetch_results_and_errors(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    simulated: Served,
    world: SimulatedWorld,
) -> None:
    """Re-fetch: the ``results`` map is decoded; ids in ``errors`` or absent are failed.

    The documented batch sample, served verbatim, yields its one response (``statuses`` and
    ``errors`` nested inside ``results`` as the sample places them). A hand-built envelope
    with ``id1`` found, ``id2`` in ``errors``, ``id3`` absent and ``id4`` answered by another
    owner's response returns ``found={id1}`` and ``failed={id2, id3, id4}``; ``id1``'s email
    decodes transiently with its decoration. Against the simulator, every re-fetched real lead
    decodes to the ``sim.invalid`` address whose hash the lead job stored.
    """
    documented = sample("lead_form_responses_batch")
    verbatim = serve(lambda _request: ok(documented))
    async with open_client(verbatim) as client:
        from_sample = await client.get_lead_responses(SAMPLE_LEAD_ACCOUNT, [SAMPLE_LEAD_ID])
    assert set(from_sample.found) == {SAMPLE_LEAD_ID}
    assert from_sample.failed == frozenset()
    parsed = parse_lead(from_sample.found[SAMPLE_LEAD_ID], {}, surface="lead_form_responses_batch")
    assert parsed is not None
    assert (parsed.external_id, parsed.lead_form_urn, parsed.lead_form_version) == (
        SAMPLE_LEAD_ID,
        SAMPLE_FORM_URN,
        1,
    )
    undecorated = decode_email(from_sample.found[SAMPLE_LEAD_ID], {})
    assert (undecorated.status, undecorated.reason) == ("no_email", REASON_UNKNOWN_FORM_VERSION)

    base = documented["results"][SAMPLE_LEAD_ID]
    id1, id2, id3, id4 = (f"aaaabbbb-0000-cccc-1111-dddd2222eeee-{n}" for n in range(1, 5))
    common = {"form_id": 3162, "version": 1, "questions": [(1, "EMAIL"), (2, None)]}
    envelope = {
        "results": {
            id1: lead_element(
                base,
                lead_id=id1,
                account_id=SAMPLE_LEAD_ACCOUNT,
                answers=[(1, "Erin.Shaw@Example.com"), (2, "Bath")],
                **common,
            ),
            id4: lead_element(
                base, lead_id=id4, account_id="522529624", answers=[(1, "x@example.com")], **common
            ),
        },
        "statuses": {id2: 404},
        "errors": {id2: {"status": 404, "code": "NOT_FOUND", "message": "Not found"}},
    }
    served = serve(lambda _request: ok(envelope))
    async with open_client(served) as client:
        batch = await client.get_lead_responses(SAMPLE_LEAD_ACCOUNT, [id1, id2, id3, id4])
    assert set(batch.found) == {id1}
    assert batch.failed == frozenset({id2, id3, id4})
    assert "Erin" not in repr(batch)
    email = decode_email(batch.found[id1], {})
    assert (email.status, email.reason) == ("ok", None)
    assert email.email == "erin.shaw@example.com"
    assert email.sha256 == hashlib.sha256(b"erin.shaw@example.com").hexdigest()
    assert "erin" not in repr(email).lower()

    account_id = world.linkedin_account_ids[0]
    async with open_client(simulated) as client:
        leads = await client.list_lead_responses(account_id, (LEADS_FROM, NOW))
        chosen = leads.leads[: LEAD_BATCH_SIZE + 5]
        refetched = await client.get_lead_responses(
            account_id, [lead.external_id for lead in chosen]
        )
    assert len(of_surface(simulated.requests, "lead_form_responses_batch")) == 2
    assert set(refetched.found) == {lead.external_id for lead in chosen}
    assert refetched.failed == frozenset()
    for lead in chosen:
        decoded = decode_email(refetched.found[lead.external_id], {})
        assert decoded.status == "ok"
        assert decoded.email is not None
        assert decoded.email.endswith(f"@{SIM_EMAIL_DOMAIN}")
        assert decoded.sha256 == lead.email_sha256


# ---------------------------------------------------------------------------------------------
# Tokens (AAP 0.3.3 "Credentials"; fallback table "401 after one token refresh")
# ---------------------------------------------------------------------------------------------


class ManualClock:
    """A monotonic clock the test advances by hand, injected into the token provider."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        """The current reading, in seconds."""
        return self.now


def refresh_settings() -> Settings:
    """Settings holding only the fake refresh-token trio, built without the environment."""
    return Settings(
        linkedin_ad_account_ids=["990000001"],
        linkedin_client_id=REFRESH_CLIENT_ID,
        linkedin_client_secret=SecretStr(REFRESH_CLIENT_SECRET),
        linkedin_refresh_token=SecretStr(REFRESH_TOKEN),
    )


@dataclass(slots=True)
class TokenServer:
    """A hand-written LinkedIn: the OAuth exchange plus ``GET adAccounts/{id}``.

    Attributes:
        account: The ``ad_account`` body served on success (the documented sample).
        unauthorized: How many REST calls are answered HTTP 401 before the first 200.
        expires_in: ``expires_in`` of every issued token.
        issued: Every access token issued, in order.
        exchanges: Every token request received.
        api: Every REST request received.
    """

    account: Mapping[str, Any]
    unauthorized: int = 0
    expires_in: int = 3600
    issued: list[str] = field(default_factory=list)
    exchanges: list[httpx.Request] = field(default_factory=list)
    api: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request."""
        if surface_of(request) == "access_token":
            self.exchanges.append(request)
            token = f"test-exchanged-access-token-{len(self.issued) + 1}"
            self.issued.append(token)
            return ok(
                {
                    "access_token": token,
                    "expires_in": self.expires_in,
                    "refresh_token": ROTATED_REFRESH_TOKEN,
                    "refresh_token_expires_in": 31_536_000,
                    "scope": "r_ads,r_ads_reporting,r_marketing_leadgen_automation",
                }
            )
        self.api.append(request)
        if len(self.api) <= self.unauthorized:
            return linkedin_error(401, "INVALID_ACCESS_TOKEN", "Invalid access token")
        return ok(self.account)


async def test_refresh_token_exchange(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    set_env: Callable[..., None],
) -> None:
    """Credentials: the refresh token is exchanged once and cached until 300 s before expiry.

    With the refresh-token trio configured, ``get()`` posts exactly the contract's four form
    fields to ``https://www.linkedin.com/oauth/v2/accessToken`` without any REST header. A
    second ``get()`` within ``expires_in - 300`` seconds (injected clock) sends nothing; at
    that instant the token is exchanged again. ``live_credentials("linkedin")`` configures the
    static-token alternative, so the trio is set here directly.
    """
    set_env(
        LINKEDIN_AD_ACCOUNT_IDS="990000001",
        LINKEDIN_CLIENT_ID=REFRESH_CLIENT_ID,
        LINKEDIN_CLIENT_SECRET=REFRESH_CLIENT_SECRET,
        LINKEDIN_REFRESH_TOKEN=REFRESH_TOKEN,
    )
    settings = get_settings()
    assert settings.linkedin_uses_refresh_token
    assert settings.has_credentials("linkedin")
    server = TokenServer(account=sample("ad_account"))
    served = serve(server)
    clock = ManualClock(1_000.0)
    async with build_client("linkedin", transport=served.transport, settings=settings) as http:
        tokens = LinkedInTokenProvider(settings, http, retry=NO_SLEEP, clock=clock)
        assert (tokens.mode, tokens.can_refresh) == ("refresh", True)
        first = await tokens.get()
        clock.now = 1_000.0 + server.expires_in - EXPIRY_MARGIN_SECONDS - 1
        cached = await tokens.get()
        assert len(server.exchanges) == 1
        clock.now = 1_000.0 + server.expires_in - EXPIRY_MARGIN_SECONDS
        refreshed = await tokens.get()

    assert server.issued == ["test-exchanged-access-token-1", "test-exchanged-access-token-2"]
    assert first == cached == server.issued[0]
    assert refreshed == server.issued[1]
    assert EXPIRY_MARGIN_SECONDS == 300
    assert TOKEN_URL == "https://www.linkedin.com/oauth/v2/accessToken"
    assert len(served.requests) == 2
    # The first exchange presents the configured refresh token; the response rotated it, so
    # the second presents the rotated one, held in memory only.
    presented = (REFRESH_TOKEN, ROTATED_REFRESH_TOKEN)
    for request, refresh_token in zip(served.requests, presented, strict=True):
        assert request.method == "POST"
        assert str(request.url) == TOKEN_URL
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        for forbidden in ("Authorization", "Linkedin-Version", "X-Restli-Protocol-Version"):
            assert forbidden not in request.headers
        form = parse_qs(request.content.decode("ascii"), strict_parsing=True)
        assert form == {
            "grant_type": ["refresh_token"],
            "refresh_token": [refresh_token],
            "client_id": [REFRESH_CLIENT_ID],
            "client_secret": [REFRESH_CLIENT_SECRET],
        }


async def test_401_refreshes_once_then_denies(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    live_credentials: Callable[[str], Mapping[str, str]],
) -> None:
    """Fallback table: a 401 refreshes the token once; a second 401 is access denied.

    * one 401, then success: the cached token is invalidated, a new one exchanged and the
      request repeated once with it;
    * 401 on both attempts: ``AccessDenied`` with ``is_unauthorized``, after exactly two
      REST attempts and two exchanges (LinkedIn's "401 after one token refresh");
    * a static ``LINKEDIN_ACCESS_TOKEN`` cannot be refreshed, so its first 401 is denied at
      once, with no exchange.
    """
    account = sample("ad_account")

    once = TokenServer(account=account, unauthorized=1)
    served = serve(once)
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        row = await LinkedInClient(http, tokens, retry=NO_SLEEP).get_ad_account("123456")
    assert (row.external_id, row.currency) == ("123456", "USD")
    assert len(once.exchanges) == 2
    assert [request.headers["Authorization"] for request in once.api] == [
        f"Bearer {token}" for token in once.issued
    ]

    always = TokenServer(account=account, unauthorized=99)
    served = serve(always)
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        with pytest.raises(AccessDenied) as denied:
            await LinkedInClient(http, tokens, retry=NO_SLEEP).get_ad_account("123456")
    assert denied.value.is_unauthorized
    assert denied.value.http_status == 401
    assert denied.value.endpoint == "ad_account"
    assert len(always.api) == 2
    assert len(always.exchanges) == 2

    static = live_credentials("linkedin")
    settings = get_settings()
    rejected = TokenServer(account=account, unauthorized=99)
    served = serve(rejected)
    async with build_client("linkedin", transport=served.transport, settings=settings) as http:
        tokens = LinkedInTokenProvider(settings, http, retry=NO_SLEEP)
        assert (tokens.mode, tokens.can_refresh) == ("static", False)
        with pytest.raises(AccessDenied) as static_denied:
            await LinkedInClient(http, tokens, retry=NO_SLEEP).get_ad_account("123456")
    assert static_denied.value.is_unauthorized
    assert [request.headers["Authorization"] for request in rejected.api] == [
        f"Bearer {static['LINKEDIN_ACCESS_TOKEN']}"
    ]
    assert rejected.exchanges == []


@pytest.mark.parametrize(
    ("status", "error", "description"),
    [
        pytest.param(
            400,
            "invalid_grant",
            f"The refresh token {REFRESH_TOKEN} is invalid",
            id="invalid-grant",
        ),
        pytest.param(
            400,
            "invalid_request",
            "The provided authorization grant or refresh token is invalid, expired or revoked",
            id="revoked-refresh-token",
        ),
        pytest.param(401, "invalid_client", "Client authentication failed", id="invalid-client"),
    ],
)
async def test_invalid_grant_is_access_denied(
    status: int, error: str, description: str, serve: Callable[[Responder], Served]
) -> None:
    """Fallback table: a rejected refresh grant is access denied, never a defect or a retry.

    ``invalid_grant`` (and LinkedIn's revoked-token ``invalid_request``, and a 401
    ``invalid_client``) refuse the credentials themselves, so LinkedIn falls back to simulated
    data with the error as the reason. No REST call is sent without a token, and a refresh
    token echoed in the error description never reaches the error text.
    """
    exchanges: list[httpx.Request] = []

    def rejecting(request: httpx.Request) -> httpx.Response:
        """Reject every exchange with an OAuth 2.0 error body; no REST call is expected."""
        assert surface_of(request) == "access_token"
        exchanges.append(request)
        return httpx.Response(status, json={"error": error, "error_description": description})

    served = serve(rejecting)
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        with pytest.raises(AccessDenied) as denied:
            await tokens.get()
        with pytest.raises(AccessDenied):
            await LinkedInClient(http, tokens, retry=NO_SLEEP).get_ad_account("123456")

    assert denied.value.source == "linkedin"
    assert denied.value.endpoint == "access_token"
    assert denied.value.code == error
    assert denied.value.http_status == status
    for secret in (REFRESH_TOKEN, REFRESH_CLIENT_SECRET):
        assert secret not in str(denied.value)
        assert secret not in denied.value.reason
    assert len(exchanges) == 2
    assert all(surface_of(request) == "access_token" for request in served.requests)


async def test_tokens_never_logged(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    captured_logs: Any,
) -> None:
    """Credentials: no access token, refresh token or client secret is ever logged.

    Logging captures every level, with redaction configured from the isolated environment,
    which holds none of these values, so absence proves they were never written rather than
    masked. The run exchanges a token, retries a 401 with a fresh token, rotates the refresh
    token and finally fails a refresh whose error echoes the refresh token.
    """
    captured_logs.configure(level="DEBUG")
    server = TokenServer(account=sample("ad_account"), unauthorized=1)
    served = serve(server)
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        client = LinkedInClient(http, tokens, retry=NO_SLEEP)
        await client.get_ad_account("123456")

    def rejecting(request: httpx.Request) -> httpx.Response:
        """Reject the exchange, echoing the presented refresh token in the description."""
        presented = parse_qs(request.content.decode("ascii"))["refresh_token"][0]
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": f"{presented} is revoked"},
        )

    failing = serve(rejecting)
    async with build_client("linkedin", transport=failing.transport) as http:
        with pytest.raises(AccessDenied):
            await LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP).get()

    events = {record.get("event") for record in captured_logs()}
    assert {
        "http.request",
        "http.response",
        "linkedin.token.refreshed",
        "linkedin.token.retry_after_401",
        "linkedin.token.failed",
        "connector.error",
    } <= events
    text = captured_logs.text
    assert len(server.issued) == 2
    for secret in (
        *server.issued,
        REFRESH_TOKEN,
        ROTATED_REFRESH_TOKEN,
        REFRESH_CLIENT_SECRET,
    ):
        assert secret not in text
    assert "Bearer" not in text


def volatile_token(label: str) -> str:
    """A 384-character token no setting knows, as long as a LinkedIn access token.

    Derived at run time, so the secret scan never reads a test value as a credential.
    """
    return hashlib.sha256(f"test-volatile-{label}".encode()).hexdigest() * 6


@dataclass(slots=True)
class EchoingLinkedIn:
    """A hand-written LinkedIn whose every failure echoes tokens in ordinary prose.

    Each exchange answers the next entry of ``exchanges``, each REST call the next entry
    of ``api``: an ``httpx.Response``, or a callable building one from the request.

    Attributes:
        exchanges: The scripted token-endpoint answers, in order.
        api: The scripted REST answers, in order.
        seen: Every request received, exchanges included.
    """

    exchanges: list[httpx.Response | Callable[[httpx.Request], httpx.Response]]
    api: list[httpx.Response | Callable[[httpx.Request], httpx.Response]]
    seen: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one request from its script."""
        self.seen.append(request)
        script = self.exchanges if surface_of(request) == "access_token" else self.api
        assert script, f"unexpected {surface_of(request)} request"
        answer = script.pop(0)
        return answer(request) if callable(answer) else answer


async def test_exchanged_and_rotated_tokens_never_reach_an_error_or_a_log(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    captured_logs: Any,
) -> None:
    """Logs never contain tokens, even ones only the provider holds, echoed in plain prose.

    The exchange issues an access token and rotates the refresh token, neither of which any
    setting knows. REST calls then fail with a 403 and three 503 text bodies echoing the
    access token without any ``Bearer`` or assignment marker; a 401 makes the client
    refresh, and the repeated call's 403 echoes both the rejected and the new token. Later
    exchanges fail with 500 three times, then a 400 ``invalid_grant``, each echoing the
    refresh tokens rotated away and the access tokens dropped. No error text, and no
    logged event (``connector.error``, ``linkedin.token.failed``, ``http.retry``),
    carries any of them.
    """
    captured_logs.configure(level="DEBUG")
    first_access, second_access = volatile_token("access-1"), volatile_token("access-2")
    first_rotated, second_rotated = volatile_token("refresh-1"), volatile_token("refresh-2")

    def issued(access: str, refresh: str) -> httpx.Response:
        """A successful exchange issuing ``access`` and rotating to ``refresh``."""
        return ok(
            {
                "access_token": access,
                "expires_in": 3600,
                "refresh_token": refresh,
                "refresh_token_expires_in": 31_536_000,
                "scope": "r_ads,r_ads_reporting,r_marketing_leadgen_automation",
            }
        )

    def echo_retired(request: httpx.Request) -> httpx.Response:
        """Fail the exchange, quoting every token the provider stopped using."""
        presented = parse_qs(request.content.decode("ascii"))["refresh_token"][0]
        assert presented == second_rotated
        return httpx.Response(
            500,
            json={
                "message": f"Refresh {first_rotated} was superseded; {REFRESH_TOKEN} too. "
                f"Access {first_access} and {second_access} were dropped."
            },
        )

    server = EchoingLinkedIn(
        exchanges=[
            issued(first_access, first_rotated),
            issued(second_access, second_rotated),
            echo_retired,
            echo_retired,
            echo_retired,
            httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": f"{first_rotated} and {second_rotated} are revoked; "
                    f"so is {first_access}",
                },
            ),
        ],
        api=[
            linkedin_error(403, "ACCESS_DENIED", f"Member {first_access} lacks r_ads"),
            *(
                httpx.Response(503, text=f"Upstream unavailable while checking {first_access}")
                for _ in range(3)
            ),
            linkedin_error(401, "INVALID_ACCESS_TOKEN", f"Token {first_access} expired"),
            linkedin_error(
                403, "ACCESS_DENIED", f"Neither {first_access} nor {second_access} may read it"
            ),
        ],
    )
    served = serve(server)
    errors: list[Exception] = []
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        client = LinkedInClient(http, tokens, retry=NO_SLEEP)
        with pytest.raises(AccessDenied) as denied:
            await client.get_ad_account("123456")
        errors.append(denied.value)
        with pytest.raises(TransientError) as unavailable:
            await client.get_ad_account("123456")
        errors.append(unavailable.value)
        with pytest.raises(AccessDenied) as refreshed_denied:
            await client.get_ad_account("123456")
        errors.append(refreshed_denied.value)
        tokens.invalidate()
        with pytest.raises(TransientError) as exchange_failed:
            await tokens.get()
        errors.append(exchange_failed.value)
        with pytest.raises(AccessDenied) as exchange_rejected:
            await tokens.get()
        errors.append(exchange_rejected.value)

    assert server.exchanges == []
    assert server.api == []
    assert denied.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: Member [REDACTED] lacks r_ads"
    )
    assert unavailable.value.reason == (
        "linkedin ad_account: HTTP 503: Upstream unavailable while checking [REDACTED]"
    )
    assert refreshed_denied.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: Neither [REDACTED] nor [REDACTED] may read it"
    )
    assert exchange_failed.value.http_status == 500
    assert (exchange_rejected.value.code, exchange_rejected.value.http_status) == (
        "invalid_grant",
        400,
    )
    secrets = (
        first_access,
        second_access,
        first_rotated,
        second_rotated,
        REFRESH_TOKEN,
        REFRESH_CLIENT_SECRET,
    )
    for error in errors:
        assert isinstance(error, AccessDenied | TransientError)
        rendered = (error.message, error.reason, str(error), repr(error), str(error.log_fields))
        for shown in rendered:
            for secret in secrets:
                assert secret not in shown
                assert secret[:16] not in shown
    events = [record.get("event") for record in captured_logs()]
    # 403, three 503s, 401 and 403 on REST; three 500s on the exchange. The 400 is read by
    # the provider itself and logged as linkedin.token.failed.
    assert events.count("connector.error") == 9
    assert {"http.retry", "linkedin.token.failed", "linkedin.token.refreshed"} <= set(events)
    text = captured_logs.text
    for secret in secrets:
        assert secret not in text
        assert secret[:16] not in text


async def test_a_token_replaced_by_a_refresh_never_reaches_a_later_call_error_or_log(
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
    captured_logs: Any,
) -> None:
    """Logs never contain a token the client stopped using, echoed by a later call.

    The first exchange issues a random 420-character access token; ``GET adAccounts``
    answers 401, so the client invalidates it, the second exchange issues another, and the
    repeated call succeeds. A new call, whose request carries only the second token, then
    gets a 403 whose message opens with the first one in plain prose, so a 300-character
    cut of an unstripped reason would keep its start. Neither token, nor any 16-character
    fragment of either, reaches the raised error or any logged event.
    """
    captured_logs.configure(level="DEBUG")
    first_access, second_access = token_hex(210), token_hex(210)

    def issued(access: str) -> httpx.Response:
        """A successful exchange issuing ``access``."""
        return ok(
            {
                "access_token": access,
                "expires_in": 3600,
                "scope": "r_ads,r_ads_reporting,r_marketing_leadgen_automation",
            }
        )

    server = EchoingLinkedIn(
        exchanges=[issued(first_access), issued(second_access)],
        api=[
            linkedin_error(401, "INVALID_ACCESS_TOKEN", "Invalid access token"),
            ok(sample("ad_account")),
            linkedin_error(403, "ACCESS_DENIED", f"{first_access} may not read this account"),
        ],
    )
    served = serve(server)
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        client = LinkedInClient(http, tokens, retry=NO_SLEEP)
        row = await client.get_ad_account("123456")
        with pytest.raises(AccessDenied) as denied:
            await client.get_ad_account("123456")

    assert row.external_id == "123456"
    assert server.exchanges == []
    assert server.api == []
    assert len(first_access) == len(second_access) == 420
    rest = [request for request in server.seen if surface_of(request) != "access_token"]
    assert [request.headers["Authorization"] for request in rest] == [
        f"Bearer {first_access}",
        f"Bearer {second_access}",
        f"Bearer {second_access}",
    ]
    error = denied.value
    assert (error.http_status, error.code) == (403, "ACCESS_DENIED")
    assert error.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] may not read this account"
    )
    fragments = {
        token[start : start + 16]
        for token in (first_access, second_access)
        for start in range(len(token) - 15)
    }
    rendered = (
        error.message,
        error.reason,
        str(error),
        repr(error),
        str(error.code),
        str(error.log_fields),
    )
    for shown in rendered:
        assert not any(fragment in shown for fragment in fragments)
    events = [record.get("event") for record in captured_logs()]
    # The 401 that made the client refresh, then the later call's 403.
    assert events.count("connector.error") == 2
    assert {"linkedin.token.refreshed", "linkedin.token.retry_after_401"} <= set(events)
    text = captured_logs.text
    assert not any(fragment in text for fragment in fragments)


async def test_provider_held_refresh_tokens_never_reach_a_rest_error_or_a_log(
    serve: Callable[[Responder], Served],
    captured_logs: Any,
) -> None:
    """Logs never contain a secret only the token provider holds, echoed by a REST call.

    The first exchange issues a random access token and rotates the configured refresh
    token to a random 400-character one that no setting knows and no REST request carries.
    ``GET adAccounts`` then answers 403 with a message opening with that rotated token in
    plain prose, so a 300-character cut of an unstripped reason would keep its start. A
    second exchange, after an invalidation, rotates it again: the next call's 403 opens with
    the refresh token now retired, and a third call's quotes the configured refresh token,
    the client secret and the current rotated token. No 16-character fragment of any of
    them, nor of the access tokens, reaches a raised error or any logged event.
    """
    captured_logs.configure(level="DEBUG")
    first_access, second_access = token_hex(200), token_hex(200)
    first_rotated, second_rotated = token_hex(200), token_hex(200)

    def issued(access: str, refresh: str) -> httpx.Response:
        """A successful exchange issuing ``access`` and rotating to ``refresh``."""
        return ok(
            {
                "access_token": access,
                "expires_in": 3600,
                "refresh_token": refresh,
                "refresh_token_expires_in": 31_536_000,
                "scope": "r_ads,r_ads_reporting,r_marketing_leadgen_automation",
            }
        )

    server = EchoingLinkedIn(
        exchanges=[issued(first_access, first_rotated), issued(second_access, second_rotated)],
        api=[
            linkedin_error(403, "ACCESS_DENIED", f"{first_rotated} grants no r_ads"),
            linkedin_error(403, "ACCESS_DENIED", f"{first_rotated} was revoked"),
            linkedin_error(
                403,
                "ACCESS_DENIED",
                f"{REFRESH_TOKEN} and {REFRESH_CLIENT_SECRET} were revoked; so is {second_rotated}",
            ),
        ],
    )
    served = serve(server)
    errors: list[AccessDenied] = []
    async with build_client("linkedin", transport=served.transport) as http:
        tokens = LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP)
        client = LinkedInClient(http, tokens, retry=NO_SLEEP)
        with pytest.raises(AccessDenied) as current_denied:
            await client.get_ad_account("123456")
        errors.append(current_denied.value)
        tokens.invalidate()
        with pytest.raises(AccessDenied) as retired_denied:
            await client.get_ad_account("123456")
        errors.append(retired_denied.value)
        with pytest.raises(AccessDenied) as configured_denied:
            await client.get_ad_account("123456")
        errors.append(configured_denied.value)
        shown_objects = (repr(tokens), str(tokens), repr(client), str(client))

    assert server.exchanges == []
    assert server.api == []
    assert all(len(token) == 400 for token in (first_rotated, second_rotated))
    exchanges = [request for request in server.seen if surface_of(request) == "access_token"]
    presented = [parse_qs(request.content.decode("ascii")) for request in exchanges]
    assert [form["refresh_token"] for form in presented] == [[REFRESH_TOKEN], [first_rotated]]
    rest = [request for request in server.seen if surface_of(request) != "access_token"]
    # No REST request carries a refresh token or the client secret: only the bearer token.
    assert [request.headers["Authorization"] for request in rest] == [
        f"Bearer {first_access}",
        f"Bearer {second_access}",
        f"Bearer {second_access}",
    ]
    assert current_denied.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] grants no r_ads"
    )
    assert retired_denied.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] was revoked"
    )
    assert configured_denied.value.reason == (
        "linkedin ad_account: ACCESS_DENIED: [REDACTED] and [REDACTED] were revoked; "
        "so is [REDACTED]"
    )
    fragments = {
        secret[start : start + 16]
        for secret in (
            first_access,
            second_access,
            first_rotated,
            second_rotated,
            REFRESH_TOKEN,
            REFRESH_CLIENT_SECRET,
        )
        for start in range(len(secret) - 15)
    }
    for error in errors:
        assert (error.http_status, error.code) == (403, "ACCESS_DENIED")
        rendered = (
            error.message,
            error.reason,
            str(error),
            repr(error),
            str(error.code),
            str(error.log_fields),
        )
        for shown in rendered:
            assert not any(fragment in shown for fragment in fragments)
    assert shown_objects == (
        "LinkedInTokenProvider(mode='refresh')",
        "LinkedInTokenProvider(mode='refresh')",
        "LinkedInClient()",
        "LinkedInClient()",
    )
    events = [record.get("event") for record in captured_logs()]
    # The three REST 403s; both exchanges succeeded.
    assert events.count("connector.error") == 3
    assert events.count("linkedin.token.refreshed") == 2
    text = captured_logs.text
    assert not any(fragment in text for fragment in fragments)


# ---------------------------------------------------------------------------------------------
# Traceability (AAP 0.11.2 "External fixtures are traceable to verified contracts")
# ---------------------------------------------------------------------------------------------


async def call_surface(surface: str, http: httpx.AsyncClient, account_id: str) -> None:
    """Make the client call that requests ``surface``, after any call it depends on.

    Creatives need campaign URNs and posts need creatives, so those listings run first; the
    target surface is requested last, except the fallback's campaign-delivery discovery, which
    precedes the per-campaign member-segments requests it selects. The two fallback surfaces
    are requested only when ``http`` rejects the Statistics Finder with HTTP 400.
    """
    client = LinkedInClient(http, StaticTokenProvider(TOKEN), retry=NO_SLEEP)
    match surface:
        case "access_token":
            await LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP).get()
        case "ad_account":
            await client.get_ad_account(account_id)
        case "ad_campaign_groups":
            await client.list_campaign_groups(account_id, account_currency="USD")
        case "ad_campaigns":
            await client.list_campaigns(account_id, account_currency="USD")
        case "creatives" | "posts":
            campaigns = await client.list_campaigns(account_id, account_currency="USD")
            creatives = await client.list_creatives(account_id, [row.urn for row in campaigns.rows])
            if surface == "posts":
                posts = [row.post_urn for row in creatives.rows if row.post_urn is not None]
                await client.get_posts(posts)
        case "ad_analytics":
            await client.analytics_daily(account_id, WINDOW)
        case (
            "ad_analytics_statistics"
            | "ad_analytics_campaign_delivery"
            | "ad_analytics_member_segments"
        ):
            await client.analytics_segments(account_id, "MEMBER_INDUSTRY", WINDOW)
        case "lead_forms":
            await client.list_lead_forms(account_id)
        case "lead_form_responses":
            await client.list_lead_responses(account_id, (LEADS_FROM, NOW))
        case "lead_form_responses_batch":
            submitted = await client.list_lead_responses(
                account_id, (LEADS_FROM, NOW), page_size=10, max_pages=1
            )
            await client.get_lead_responses(
                account_id, [lead.external_id for lead in submitted.leads]
            )
        case _:
            raise AssertionError(f"no client call requests linkedin.{surface}")


@pytest.mark.parametrize("surface", LINKEDIN_SURFACES)
async def test_every_surface_request_validates(
    surface: str,
    simulated: Served,
    fallback: Served,
    world: SimulatedWorld,
    contract: Callable[[str, str], Contract],
) -> None:
    """Traceability: each surface's request matches its contract file and is identified as it.

    The client's call for the surface goes through the contract-checked simulator (for the
    segment fallback's two surfaces, one rejecting the Statistics Finder); every request it
    sent is identified by ``payloads.identify_surface`` and validated again against the
    contract that names the reference URL and version it was written from.
    """
    served = fallback if surface in FALLBACK_SURFACES else simulated
    async with build_client("linkedin", transport=served.transport) as http:
        await call_surface(surface, http, world.linkedin_account_ids[0])

    requested = [surface_of(request) for request in served.requests]
    assert requested
    if surface == "ad_analytics_campaign_delivery":
        assert requested[:2] == ["ad_analytics_statistics", surface]
        assert set(requested[2:]) == {"ad_analytics_member_segments"}
    else:
        assert requested[-1] == surface
    for request in served.requests:
        loaded = contract("linkedin", surface_of(request))
        assert loaded.api_version == "202609"
        assert loaded.reference_url.startswith("https://")
        validate_request(loaded, request)


def adapted_sample(surface: str, documented: dict[str, Any]) -> dict[str, Any]:
    """The documented sample as the client path can parse it, with documented adaptations.

    Every sample is used verbatim except those whose pivot does not match the request the
    product sends: the Analytics Finder sample (shared by ``ad_analytics`` and the segment
    fallback's two contracts) pivots by ``MEMBER_COMPANY`` and gets a creative URN (daily rows
    key creatives), a distinct campaign URN per element (campaign-delivery discovery) or a
    distinct ``MEMBER_SENIORITY`` value per element (member segments of one campaign); the
    ``ad_analytics_statistics`` sample pivots by campaign only and gets the requested
    demographic value beside it.
    """
    if surface == "ad_analytics":
        for element in documented["elements"]:
            element["pivotValues"] = ["urn:li:sponsoredCreative:121608865"]
    elif surface == "ad_analytics_campaign_delivery":
        for index, element in enumerate(documented["elements"]):
            element["pivotValues"] = [f"urn:li:sponsoredCampaign:{1234567 + index}"]
    elif surface == "ad_analytics_member_segments":
        for index, element in enumerate(documented["elements"]):
            element["pivotValues"] = [f"urn:li:seniority:{5 + index}"]
    elif surface == "ad_analytics_statistics":
        for element in documented["elements"]:
            element["pivotValues"] = [
                *element["pivotValues"],
                DEMOGRAPHIC_VALUES["MEMBER_SENIORITY"],
            ]
    return documented


def url_pairs(url: httpx.URL) -> dict[str, str]:
    """The raw query parameters of ``url`` by name."""
    pairs: dict[str, str] = {}
    for part in url.query.decode("ascii").split("&"):
        if part:
            name, _separator, value = part.partition("=")
            pairs.setdefault(name, value)
    return pairs


def parse_body(
    surface: str,
    body: Mapping[str, Any],
    *,
    pivot: str | None = None,
    campaign_urn: str | None = None,
    account_currency: str | None = None,
) -> int:
    """Parse a 2xx body of ``surface`` with the connector's parser; return the records read.

    Args:
        surface: The LinkedIn contract surface.
        body: The decoded response body.
        pivot: The demographic pivot of a segment request.
        campaign_urn: The campaign of an Analytics Finder segment request, when it names one.
        account_currency: The ad account's currency, for bare campaign-group budgets.
    """
    batch_keys = ("statuses", "errors")
    match surface:
        case "access_token":
            assert isinstance(body["access_token"], str)
            assert body["access_token"].strip()
            assert isinstance(body["expires_in"], int)
            assert body["expires_in"] > 0
            return 1
        case "ad_account":
            parse_ad_account(body)
            return 1
        case "ad_campaign_groups":
            return len(
                [
                    parse_campaign_group(element, account_currency=account_currency)
                    for element in body["elements"]
                ]
            )
        case "ad_campaigns":
            return len([parse_campaign(element) for element in body["elements"]])
        case "creatives":
            return len([parse_creative(element) for element in body["elements"]])
        case "posts":
            return len(
                [
                    parse_post(unquote(key), value)
                    for key, value in body["results"].items()
                    if key not in batch_keys
                ]
            )
        case "ad_analytics":
            return len([parse_analytics_daily(element) for element in body["elements"]])
        case "ad_analytics_campaign_delivery":
            campaigns = [element["pivotValues"] for element in body["elements"]]
            assert all(len(values) == 1 for values in campaigns)
            assert all(sponsored_campaign_urn(values[0]) == values[0] for values in campaigns)
            return len(campaigns)
        case "ad_analytics_statistics" | "ad_analytics_member_segments":
            assert pivot is not None, "a segment body needs its request's pivot"
            if surface == "ad_analytics_member_segments":
                assert campaign_urn is not None, "a member-segments body needs its campaign"
            return len(
                [
                    parse_segment(element, pivot, campaign_urn=campaign_urn, surface=surface)
                    for element in body["elements"]
                ]
            )
        case "lead_forms":
            return len([parse_lead_form(element) for element in body["elements"]])
        case "lead_form_responses":
            return len([parse_lead(element, {}) for element in body["elements"]])
        case "lead_form_responses_batch":
            return len(
                [
                    parse_lead(value, {}, surface="lead_form_responses_batch")
                    for key, value in body["results"].items()
                    if key not in batch_keys
                ]
            )
        case _:
            raise AssertionError(f"no parser for linkedin.{surface}")


def segment_selector(url: httpx.URL) -> tuple[str, str | None]:
    """The demographic pivot and, for the Analytics Finder form, the campaign of a request."""
    pairs = url_pairs(url)
    if "pivots" in pairs:
        pivots = cast("list[str]", parse_restli(pairs["pivots"]))
        return pivots[-1], None
    campaigns = cast("list[str]", parse_restli(pairs["campaigns"]))
    return pairs["pivot"], campaigns[0]


async def read_sample(surface: str, http: httpx.AsyncClient, documented: Mapping[str, Any]) -> int:
    """Read ``surface`` through the client against its served sample; return the rows read."""
    client = LinkedInClient(http, StaticTokenProvider(TOKEN), retry=NO_SLEEP)
    lead_window = (datetime(2023, 6, 1, tzinfo=UTC), datetime(2023, 7, 1, tzinfo=UTC))
    match surface:
        case "access_token":
            token = await LinkedInTokenProvider(refresh_settings(), http, retry=NO_SLEEP).get()
            assert token == documented["access_token"]
            return 1
        case "ad_account":
            account = await client.get_ad_account("123456")
            assert (account.external_id, account.currency) == ("123456", "USD")
            return 1
        case "ad_campaign_groups":
            groups = await client.list_campaign_groups("506223315", account_currency="USD")
            return len(groups.rows)
        case "ad_campaigns":
            campaigns = await client.list_campaigns("506333826", account_currency="USD")
            return len(campaigns.rows)
        case "creatives":
            creatives = await client.list_creatives("520866471", ["360035215"])
            return len(creatives.rows)
        case "posts":
            posts = await client.get_posts(["urn:li:share:6844785523593134080"])
            assert posts.failed == frozenset()
            return len(posts.posts)
        case "ad_analytics":
            daily = await client.analytics_daily(
                "990000001", (date(2024, 5, 28), date(2024, 5, 29))
            )
            return len(daily.rows)
        case (
            "ad_analytics_statistics"
            | "ad_analytics_campaign_delivery"
            | "ad_analytics_member_segments"
        ):
            segments = await client.analytics_segments("990000001", "MEMBER_SENIORITY", WINDOW)
            return len(segments.rows)
        case "lead_forms":
            forms = await client.list_lead_forms("990000001", page_size=1)
            return len(forms.rows)
        case "lead_form_responses":
            leads = await client.list_lead_responses(
                SAMPLE_LEAD_ACCOUNT, lead_window, known_versions={}, page_size=10
            )
            return len(leads.leads)
        case "lead_form_responses_batch":
            batch = await client.get_lead_responses(SAMPLE_LEAD_ACCOUNT, [SAMPLE_LEAD_ID])
            return len(batch.found)
        case _:
            raise AssertionError(f"no client call reads linkedin.{surface}")


@pytest.mark.parametrize("surface", LINKEDIN_SURFACES)
async def test_documented_samples_parse(
    surface: str,
    serve: Callable[[Responder], Served],
    sample: Callable[[str], dict[str, Any]],
) -> None:
    """Traceability: every LinkedIn documented sample parses, through the client and parser.

    Each contract's sample from the official reference page is served (adapted only as
    :func:`adapted_sample` documents) behind the contract checks, read through the client
    call of that surface, and parsed again directly by the matching parser. A cursor search's
    second page and the forms' second offset page are empty, so paging ends. For the segment
    fallback's two surfaces the Statistics Finder is rejected with LinkedIn's 400, and the
    step that is not under test answers with one hand-built element: one delivering campaign
    (:data:`SAMPLE_CAMPAIGN`) for the member-segments sample, and one demographic value per
    discovered campaign for the campaign-delivery sample, so each discovered campaign is one
    row read.
    """
    documented = adapted_sample(surface, sample(surface))
    seniority = DEMOGRAPHIC_VALUES["MEMBER_SENIORITY"]

    def fallback_step(requested: str) -> httpx.Response:
        """Answer the fallback request that is not the surface under test."""
        if requested == "ad_analytics_statistics":
            return linkedin_error(400, "FIELD_INVALID", '"MEMBER_SENIORITY" is not an enum symbol')
        if requested == "ad_analytics_campaign_delivery":
            return ok({"elements": [{"pivotValues": [SAMPLE_CAMPAIGN], "impressions": 1}]})
        assert requested == "ad_analytics_member_segments"
        return ok({"elements": [{"pivotValues": [seniority], "impressions": 1}]})

    def handler(request: httpx.Request) -> httpx.Response:
        """Serve the sample on the first page and an empty page after it."""
        requested = surface_of(request)
        if surface in FALLBACK_SURFACES and requested != surface:
            return fallback_step(requested)
        assert requested == surface
        if query_value(request, "pageToken") is not None:
            return ok({"elements": []})
        start = query_value(request, "start")
        if surface == "lead_forms" and start != "0":
            return ok(offset_page([], start=int(start or "0"), count=1))
        return ok(documented)

    served = serve(handler)
    async with build_client("linkedin", transport=served.transport) as http:
        read = await read_sample(surface, http, documented)

    assert read >= 1
    campaign = SAMPLE_CAMPAIGN if surface == "ad_analytics_member_segments" else None
    assert parse_body(surface, documented, pivot="MEMBER_SENIORITY", campaign_urn=campaign) == read
    assert served.requests


async def test_captured_payloads_parse(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
) -> None:
    """Traceability: every redacted live capture validates and parses; none is a pass.

    Each ``captured/`` file written by ``ads-engine capture-fixtures`` is checked by
    ``payloads.validate_capture`` (surface identification, request and response contracts)
    and its 2xx body is parsed with the connector's parser. A segment body (Statistics Finder
    or the fallback's member segments) is parsed with the pivot (and, for the Analytics Finder
    form, the campaign) of its own request, and a campaign-group body with the currency of a
    captured ad account when there is one.
    """
    captures = captured_payloads("linkedin")
    assert isinstance(captures, list)
    currencies: dict[str, str] = {}
    for capture in captures:
        response = capture["response"]
        if capture["surface"] == "linkedin.ad_account" and 200 <= response["status"] < 300:
            account = parse_ad_account(response["body"])
            currencies[account.external_id] = account.currency
    for capture in captures:
        loaded = validate_capture(capture)
        assert loaded.source == "linkedin"
        assert loaded.name in LINKEDIN_SURFACES
        response = capture["response"]
        if not 200 <= response["status"] < 300:
            continue
        url = httpx.URL(RequestShape.from_capture(capture["request"]).url)
        pivot, campaign = (
            segment_selector(url)
            if loaded.name in {"ad_analytics_statistics", "ad_analytics_member_segments"}
            else (None, None)
        )
        segments = url.path.split("/")
        account_id = segments[3] if len(segments) > 3 and segments[2] == "adAccounts" else ""
        parse_body(
            loaded.name,
            response["body"],
            pivot=pivot,
            campaign_urn=campaign,
            account_currency=currencies.get(account_id),
        )
