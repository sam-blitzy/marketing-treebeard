"""Behavioural tests of ``ads_engine.simulation.transport.mock_transport``.

AAP 0.3.3 ("Contract fidelity", the error-classification table) and FR-11: a simulated source
runs its live client unchanged, and only the transport differs. Each test sends requests through
``mock_transport`` and checks the guarantees its module docstring lists:

* Google (``SIMULATED_WEBHOOK_URL`` and a configured live webhook URL), LinkedIn
  (``api.linkedin.com/rest`` and the OAuth ``accessToken`` endpoint) and HubSpot
  (``api.hubapi.com/crm``) are answered from the seeded world, with no network I/O;
* contract violations and unknown surfaces get HTTP 400 in the platform's own error shape, and
  raise ``ContractViolation``, ``UnknownSurface`` or ``SimulatedTransportError`` when strict;
* the webhook secret header is required under the name the transport's settings give (the
  settings the client was built with), and ``render(source, request)`` answers from the
  product's world, seeded by ``MOCK_SEED`` and built once per settings instance;
* injected 429/5xx faults carry ``Retry-After`` when the fault sets one, and injected timeouts
  raise ``httpx.ReadTimeout``;
* the journal records source, surface, method, path, status and the applied fault label, and
  never a header, query string or body, also under concurrent requests on one client.

Every payload is rendered by ``simulation.payloads`` from the contract files in
``backend/tests/contracts/``; no test encodes a platform payload of its own. A socket guard
fails any test that tries to open a network connection.
"""

import asyncio
import dataclasses
import functools
import os
import re
import socket
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import AliasChoices, SecretStr

from ads_engine.analysis.thresholds import StageBinding, Thresholds
from ads_engine.connectors.errors import QueryDefect, TransientError
from ads_engine.connectors.google_n8n.client import (
    SIMULATED_SECRET,
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.google_n8n.queries import QUERIES
from ads_engine.connectors.http import RetryPolicy, build_client, send_json
from ads_engine.connectors.hubspot.client import (
    HubSpotClient,
    RateLimiter,
    contact_property_map,
)
from ads_engine.connectors.hubspot.parser import parse_instant
from ads_engine.connectors.linkedin.auth import (
    SIMULATED_TOKEN,
    TOKEN_URL,
    LinkedInTokenProvider,
    StaticTokenProvider,
)
from ads_engine.connectors.linkedin.client import (
    DAILY_FIELDS,
    SEGMENT_DISCOVERY_FIELDS,
    SEGMENT_FIELDS,
    LinkedInClient,
)
from ads_engine.connectors.linkedin.parser import decode_email
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.simulation import SIM_GOOGLE_CUSTOMER_ID
from ads_engine.simulation import transport as transport_module
from ads_engine.simulation.generator import (
    DEFAULT_SPEC,
    ContactRecord,
    Fault,
    SearchFilter,
    SimulatedWorld,
)
from ads_engine.simulation.payloads import (
    ContractViolation,
    RenderedResponse,
    RequestShape,
    UnknownSurface,
    identify_surface,
    load_contract,
    render,
    validate_request,
    validate_response,
)
from ads_engine.simulation.transport import (
    UNKNOWN_SURFACE,
    JournalEntry,
    SimulatedTransportError,
    mock_transport,
)
from ads_engine.sync.mode import SourceModeController

REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
SPEC = DEFAULT_SPEC.with_(anchor=date(2026, 10, 2))

CUSTOMER = SIM_GOOGLE_CUSTOMER_ID
NA_ACCOUNT = "990000001"
LIVE_WEBHOOK_URL = "https://n8n.example.com/webhook/ads-engine-live"
LIVE_SECRET_HEADER = "X-Webhook-Token"
CUSTOM_SECRET_HEADER = "X-Custom-Secret"
LINKEDIN_REST = "https://api.linkedin.com/rest"
HUBSPOT_CRM = "https://api.hubapi.com/crm"
AD_ACCOUNT_URL = f"{LINKEDIN_REST}/adAccounts/{NA_ACCOUNT}"
PROPERTY_PATH = "/crm/properties/2026-09/contacts/lifecyclestage"
PROPERTY_URL = f"https://api.hubapi.com{PROPERTY_PATH}"
DEALS_BATCH_PATH = "/crm/objects/2026-09/deals/batch/read"
LINKEDIN_HEADERS = {
    "Authorization": f"Bearer {SIMULATED_TOKEN}",
    "Linkedin-Version": "202609",
    "X-Restli-Protocol-Version": "2.0.0",
}
HUBSPOT_HEADERS = {"Authorization": f"Bearer {SIMULATED_TOKEN}"}
GOOGLE_FAILURE_TYPE = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Marks values sent in query strings, headers and bodies that must never reach the journal.
LEAK_MARK = "leak-check"
# A 28-day preset window ending the day before NOW, as the segment job requests it.
SEGMENT_WINDOW = (date(2026, 9, 4), date(2026, 10, 1))
SEGMENT_RANGE = "(start:(year:2026,month:9,day:4),end:(year:2026,month:10,day:1))"


def _campaign_urn(campaign_id: int) -> str:
    """A campaign URN percent-encoded once, as it sits inside a Rest.li ``List(...)``."""
    return f"urn%3Ali%3AsponsoredCampaign%3A{campaign_id}"


@functools.cache
def _config() -> Thresholds:
    """The shipped config files, read from the repository so the cwd never matters."""
    return Thresholds.load(REPO_ROOT / "config")


def _world(*faults: Fault) -> SimulatedWorld:
    """A default world at :data:`NOW`, with ``faults`` added to its spec."""
    config = _config()
    return SimulatedWorld(
        42,
        SPEC.adding(faults=faults) if faults else SPEC,
        clock=lambda: NOW,
        content_assets=config.content_assets,
        mapping=config.mapping,
    )


def _google_body(query_id: str = "Q-G1") -> dict[str, object]:
    """The webhook body the Google client sends for one first page."""
    return {"customer_id": CUSTOMER, "query": QUERIES[query_id].render(), "page_token": None}


async def _post_google(
    http: httpx.AsyncClient,
    url: str = SIMULATED_WEBHOOK_URL,
    *,
    secret: str | None = SIMULATED_SECRET,
    query_id: str = "Q-G1",
) -> httpx.Response:
    """POST one GAQL query to a webhook URL, with the configured secret header unless ``None``."""
    headers = {} if secret is None else {get_settings().n8n_webhook_secret_header: secret}
    return await http.post(url, headers=headers, json=_google_body(query_id))


def _google_client(http: httpx.AsyncClient, *, retry: RetryPolicy) -> GoogleAdsWebhookClient:
    """The simulated Google client, as ``sync/mode.py`` builds it."""
    return GoogleAdsWebhookClient(
        http=http,
        budget=UNMETERED,
        webhook_url=SIMULATED_WEBHOOK_URL,
        secret_header=get_settings().n8n_webhook_secret_header,
        secret=SecretStr(SIMULATED_SECRET),
        customer_id=CUSTOMER,
        retry=retry,
    )


def _hubspot_client(http: httpx.AsyncClient, *, retry: RetryPolicy) -> HubSpotClient:
    """The simulated HubSpot client, throttled on a fake clock so no test sleeps."""
    now = [0.0]

    async def advance(seconds: float) -> None:
        now[0] += seconds

    limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=advance)
    return HubSpotClient(http, SecretStr(SIMULATED_TOKEN), live=False, retry=retry, limiter=limiter)


def _recording_policy(waits: list[float]) -> RetryPolicy:
    """The plan's retry policy, recording each wait instead of sleeping."""

    async def record(seconds: float) -> None:
        waits.append(seconds)

    return RetryPolicy(sleep=record)


def _platform_error_message(source: Source, url: str, body: Any) -> str:
    """Assert that ``body`` is the platform's own HTTP 400 error shape; return its message."""
    if source == "google":
        assert set(body) == {"error"}
        error = body["error"]
        assert set(error) == {"code", "message", "status", "details"}
        assert (error["code"], error["status"]) == (400, "INVALID_ARGUMENT")
        (detail,) = error["details"]
        assert detail["@type"] == GOOGLE_FAILURE_TYPE
        assert re.fullmatch(r"[0-9a-f]{22}", detail["requestId"])
        assert [item["message"] for item in detail["errors"]] == [error["message"]]
        return cast(str, error["message"])
    if source == "linkedin" and httpx.URL(url).host == "www.linkedin.com":
        assert set(body) == {"error", "error_description"}
        assert body["error"] == "invalid_request"
        return cast(str, body["error_description"])
    if source == "linkedin":
        assert set(body) == {"status", "serviceErrorCode", "code", "message"}
        assert (body["status"], body["serviceErrorCode"], body["code"]) == (
            400,
            100,
            "ILLEGAL_ARGUMENT",
        )
        return cast(str, body["message"])
    assert set(body) == {"status", "message", "correlationId", "category"}
    assert (body["status"], body["category"]) == ("error", "VALIDATION_ERROR")
    assert UUID_RE.fullmatch(body["correlationId"])
    return cast(str, body["message"])


@dataclasses.dataclass
class _LogRecorder:
    """Stands in for the transport's logger and keeps every debug event it receives."""

    events: list[tuple[str, dict[str, object]]] = dataclasses.field(default_factory=list)

    def debug(self, event: str, **fields: object) -> None:
        self.events.append((event, fields))


def _patch_render(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[RenderedResponse], RenderedResponse]
) -> None:
    """Make the transport serve ``change`` applied to the real rendered response.

    Stands in for a simulator bug: ``payloads.render`` itself never returns such answers.
    """
    real = transport_module.render

    def changed(
        source: Source,
        request: RequestShape,
        world: SimulatedWorld | None = None,
        *,
        settings: Settings | None = None,
    ) -> RenderedResponse:
        return change(real(source, request, world, settings=settings))

    monkeypatch.setattr(transport_module, "render", changed)


def _settings_env_names() -> frozenset[str]:
    """Return, lowercased, every environment name a ``Settings`` field can be read from."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias)
            elif isinstance(alias, AliasChoices):
                names.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(name.lower() for name in names)


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads, in any letter case, and reset the cache.

    The host exports real credentials; request validation reads the configured webhook secret
    header and ``build_client`` reads the configured secrets, so no test reads the host.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test on any attempt to resolve a host or open a connection."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the simulated transport attempted network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


# ---------------------------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------------------------


def test_mock_transport_rejects_an_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown source 'bing'; expected one of google"):
        mock_transport(cast(Source, "bing"), _world())


def test_mock_transport_needs_a_simulated_world() -> None:
    with pytest.raises(TypeError, match="world must be a SimulatedWorld, not dict"):
        mock_transport("google", cast(SimulatedWorld, {}))


def test_mock_transport_needs_a_list_journal() -> None:
    with pytest.raises(TypeError, match="journal must be a list or None, not tuple"):
        mock_transport("google", _world(), journal=cast(list[JournalEntry], ()))


def test_mock_transport_needs_settings_or_none() -> None:
    with pytest.raises(TypeError, match="settings must be a Settings or None, not dict"):
        mock_transport("google", _world(), settings=cast(Settings, {}))


def test_mock_transport_is_an_httpx_mock_transport() -> None:
    assert isinstance(mock_transport("hubspot", _world()), httpx.MockTransport)


def test_journal_entries_hold_only_the_documented_fields_and_are_immutable() -> None:
    names = [field.name for field in dataclasses.fields(JournalEntry)]
    assert names == ["source", "surface", "method", "path", "status", "fault"]
    entry = JournalEntry("google", "search", "POST", "/webhook/google-ads", 200, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.status = 500  # type: ignore[misc]


# ---------------------------------------------------------------------------------------------
# Each source's surfaces, answered from the world through the live clients
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_google_search_on_the_simulated_webhook_url_is_the_live_envelope() -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await _post_google(http)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"results", "fieldMask", "queryResourceConsumption"}
    (row,) = body["results"]
    assert row["customer"]["id"] == CUSTOMER
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", 200, None)]

    # The same seed renders the same bytes.
    async with httpx.AsyncClient(transport=mock_transport("google", _world())) as http:
        again = await _post_google(http)
    assert again.content == response.content


@pytest.mark.anyio
async def test_the_google_client_reads_the_simulated_webhook_through_build_client() -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), journal=journal)
    async with build_client("google", transport=transport) as http:
        client = _google_client(http, retry=_recording_policy([]))
        page = await client.search_page("Q-G1", QUERIES["Q-G1"].render())
    assert len(page.rows) == 1
    assert page.next_page_token is None
    assert client.calls_made == 1
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", 200, None)]


@pytest.mark.anyio
async def test_the_google_client_reads_the_configured_live_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", LIVE_WEBHOOK_URL)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET_HEADER", LIVE_SECRET_HEADER)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", SIMULATED_SECRET)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", CUSTOMER)
    get_settings.cache_clear()
    settings = get_settings()
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), journal=journal)
    async with build_client("google", transport=transport, settings=settings) as http:
        client = GoogleAdsWebhookClient.from_settings(
            settings, http, UNMETERED, retry=_recording_policy([])
        )
        result = await client.run_query("Q-G1")
    # A 200 means the request passed validation: the contract's secret-header placeholder
    # resolved to the configured X-Webhook-Token, the header the client sent.
    assert (len(result.rows), result.pages, result.calls) == (1, 1, 1)
    assert journal == [
        JournalEntry("google", "search", "POST", "/webhook/ads-engine-live", 200, None)
    ]


@pytest.mark.anyio
async def test_the_linkedin_client_reads_rest_surfaces_through_build_client() -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport("linkedin", _world(), journal=journal)
    async with build_client("linkedin", transport=transport) as http:
        client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
        account = await client.get_ad_account(NA_ACCOUNT)
        forms = await client.list_lead_forms(NA_ACCOUNT)
    assert (account.external_id, account.currency) == (NA_ACCOUNT, "USD")
    assert forms.complete
    assert forms.rows
    # One entry per page; the journal records the path, never the owner finder's query string.
    assert len(journal) == 1 + forms.calls
    assert journal == [
        JournalEntry("linkedin", "ad_account", "GET", f"/rest/adAccounts/{NA_ACCOUNT}", 200, None),
        *[JournalEntry("linkedin", "lead_forms", "GET", "/rest/leadForms", 200, None)]
        * forms.calls,
    ]


@pytest.mark.anyio
async def test_the_linkedin_client_re_fetches_simulated_leads_by_id() -> None:
    """Simulated batch elements carry the identity the client binds each re-fetch to.

    Every real lead re-fetched by plain id is found and decodes to an email; test leads, which
    ingest never stores, come back with ``testLead`` true and are failed.
    """
    world = _world()
    start_ms = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()) * 1000
    end_ms = int(NOW.timestamp()) * 1000
    responses = world.linkedin_lead_responses(NA_ACCOUNT, start_ms, end_ms)
    real = [response.id for response in responses if not response.test_lead]
    tests = [response.id for response in responses if response.test_lead]
    assert real
    assert tests
    async with build_client("linkedin", transport=mock_transport("linkedin", world)) as http:
        client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
        batch = await client.get_lead_responses(NA_ACCOUNT, [*real, *tests])
    assert set(batch.found) == set(real)
    assert batch.failed == frozenset(tests)
    assert batch.interrupted_by is None
    assert all(decode_email(element, {}).status == "ok" for element in batch.found.values())


@pytest.mark.anyio
async def test_the_linkedin_refresh_exchange_is_answered_on_the_oauth_host() -> None:
    settings = Settings(
        linkedin_client_id="simulated-client",
        linkedin_client_secret=SecretStr(SIMULATED_SECRET),
        linkedin_refresh_token=SecretStr(SIMULATED_TOKEN),
    )
    journal: list[JournalEntry] = []
    transport = mock_transport("linkedin", _world(), journal=journal)
    async with build_client("linkedin", transport=transport, settings=settings) as http:
        tokens = LinkedInTokenProvider(settings, http, clock=lambda: 0.0)
        bearer = await tokens.get()
        account = await LinkedInClient(http, tokens).get_ad_account(NA_ACCOUNT)
    assert tokens.mode == "refresh"
    assert bearer.startswith("sim-token-")
    assert account.external_id == NA_ACCOUNT
    assert journal == [
        JournalEntry("linkedin", "access_token", "POST", httpx.URL(TOKEN_URL).path, 200, None),
        JournalEntry("linkedin", "ad_account", "GET", f"/rest/adAccounts/{NA_ACCOUNT}", 200, None),
    ]


@pytest.mark.anyio
async def test_the_hubspot_client_reads_crm_surfaces_through_build_client() -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport("hubspot", _world(), journal=journal)
    async with build_client("hubspot", transport=transport) as http:
        definition = await _hubspot_client(http, retry=_recording_policy([])).property(
            "contacts", "lifecyclestage"
        )
    assert definition is not None
    assert definition.name == "lifecyclestage"
    assert "Qualified Lead" in {option.label for option in definition.options}
    assert journal == [JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, 200, None)]


@pytest.mark.anyio
async def test_a_property_bound_stage_keeps_its_instant_through_the_unchanged_parser() -> None:
    """AAP 0.5.4.7: a property-mode MQL is a datetime, compared with the touch as an instant.

    A same-day event must order correctly against a touch an hour earlier, on the event's
    instant and an hour later, so it is neither counted before its touch nor dropped after it.
    """
    booked = "date_first_meeting_was_booked"
    config = _config()
    mapping = dataclasses.replace(config.mapping, mql=StageBinding("property", (), booked))
    world = SimulatedWorld(
        42, SPEC, clock=lambda: NOW, content_assets=config.content_assets, mapping=mapping
    )
    contacts = cast(
        tuple[ContactRecord, ...],
        world.hubspot_search("contacts", [SearchFilter(booked, "HAS_PROPERTY")]),
    )
    # Events at least an hour from either UTC midnight, so all three touches share their day.
    truths = {
        contact.id: contact.bound_dates[booked]
        for contact in contacts
        if time(1) <= contact.bound_dates[booked].time() < time(23)
    }
    assert truths
    first = min(truths)
    settings = get_settings()
    async with build_client("hubspot", transport=mock_transport("hubspot", world)) as http:
        client = _hubspot_client(http, retry=_recording_policy([]))
        bindings = await client.verify_bindings(mapping, settings)
        batch = await client.contacts_by_id(
            sorted(truths), contact_property_map(bindings, settings)
        )
        raw = await http.post(
            f"{HUBSPOT_CRM}/objects/2026-09/contacts/batch/read",
            headers=HUBSPOT_HEADERS,
            json={"inputs": [{"id": first}], "properties": [booked]},
        )
    assert (bindings.mql.available, bindings.mql.property) == (True, booked)
    assert batch.read_status == dict.fromkeys(sorted(truths), "ok")
    (record,) = raw.json()["results"]
    wire = record["properties"][booked]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", wire)
    assert parse_instant(wire) == batch.records[first].mql_at
    hour = timedelta(hours=1)
    for hs_id, truth in truths.items():
        event = truth.replace(microsecond=truth.microsecond // 1000 * 1000)
        parsed = batch.records[hs_id].mql_at
        assert parsed is not None
        assert parsed == event
        assert parsed.time() != time(0)
        # Counted only when the bound stage instant is at or after the touch.
        for touch, counted in ((event - hour, True), (event, True), (event + hour, False)):
            assert touch.date() == event.date()
            assert (parsed >= touch) is counted


# ---------------------------------------------------------------------------------------------
# Contracts bind in simulated mode too
# ---------------------------------------------------------------------------------------------

VIOLATIONS = [
    pytest.param(
        "google",
        "POST",
        SIMULATED_WEBHOOK_URL,
        {},
        "search",
        "header {secret_header}: missing or empty",
        id="google-missing-webhook-secret-header",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{LINKEDIN_REST}/leadForms?q=owner&count=10&start=0",
        LINKEDIN_HEADERS,
        "lead_forms",
        "query parameter owner: missing (required)",
        id="linkedin-missing-owner-selector",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{AD_ACCOUNT_URL}?colour={LEAK_MARK}",
        LINKEDIN_HEADERS,
        "ad_account",
        "query parameter colour: unknown to the contract",
        id="linkedin-unknown-query-parameter",
    ),
    pytest.param(
        "linkedin",
        "POST",
        TOKEN_URL,
        {},
        "access_token",
        "body $.client_id: missing required field",
        id="linkedin-oauth-incomplete-grant",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{LINKEDIN_REST}/adAnalytics?q=analytics&pivot=CAMPAIGN&timeGranularity=ALL"
        f"&dateRange={SEGMENT_RANGE}&fields={SEGMENT_DISCOVERY_FIELDS}",
        LINKEDIN_HEADERS,
        "ad_analytics_campaign_delivery",
        "query parameter accounts: missing (required)",
        id="linkedin-segment-discovery-without-accounts",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{LINKEDIN_REST}/adAnalytics?q=analytics&pivot=MEMBER_SENIORITY&timeGranularity=ALL"
        f"&dateRange={SEGMENT_RANGE}&fields={SEGMENT_FIELDS}",
        LINKEDIN_HEADERS,
        "ad_analytics_member_segments",
        "query parameter campaigns: missing (required)",
        id="linkedin-member-segments-without-campaigns",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{LINKEDIN_REST}/adAnalytics?q=analytics&pivot=MEMBER_SENIORITY&timeGranularity=ALL"
        f"&dateRange={SEGMENT_RANGE}&campaigns=List({_campaign_urn(990010000)},"
        f"{_campaign_urn(990010001)})&fields={SEGMENT_FIELDS}",
        LINKEDIN_HEADERS,
        "ad_analytics_member_segments",
        "query parameter campaigns: must name exactly one campaign",
        id="linkedin-member-segments-for-two-campaigns",
    ),
    pytest.param(
        "hubspot",
        "POST",
        f"https://api.hubapi.com{DEALS_BATCH_PATH}",
        HUBSPOT_HEADERS,
        "objects_batch_read",
        "body $.properties: missing required field",
        id="hubspot-batch-read-without-properties",
    ),
]


def _violating_request(source: Source, method: str, headers: dict[str, str]) -> Any:
    """The request arguments of one :data:`VIOLATIONS` case."""
    if source == "google":
        return {"json": _google_body()}
    if source == "hubspot":
        return {"headers": headers, "json": {"inputs": [{"id": "1001"}]}}
    if method == "POST":
        return {"data": {"grant_type": "refresh_token"}}
    return {"headers": headers}


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "headers", "surface", "problem"), VIOLATIONS)
async def test_a_contract_violation_is_answered_with_http_400_in_the_platform_shape(
    source: Source, method: str, url: str, headers: dict[str, str], surface: str, problem: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.request(method, url, **_violating_request(source, method, headers))
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    message = _platform_error_message(source, url, response.json())
    expected = problem.format(secret_header=get_settings().n8n_webhook_secret_header)
    assert expected in message
    assert LEAK_MARK not in message
    assert journal == [JournalEntry(source, surface, method, httpx.URL(url).path, 400, None)]


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "headers", "surface", "problem"), VIOLATIONS)
async def test_strict_mode_raises_a_contract_violation_instead(
    source: Source, method: str, url: str, headers: dict[str, str], surface: str, problem: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), strict=True, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ContractViolation) as caught:
            await http.request(method, url, **_violating_request(source, method, headers))
    assert caught.value.surface == f"{source}.{surface}"
    expected = problem.format(secret_header=get_settings().n8n_webhook_secret_header)
    assert any(expected in error for error in caught.value.errors)
    # No response was returned, so the entry carries no status.
    assert journal == [JournalEntry(source, surface, method, httpx.URL(url).path, None, None)]


@pytest.mark.anyio
async def test_strict_mode_answers_a_valid_request() -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), strict=True, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await _post_google(http)
    assert response.status_code == 200
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", 200, None)]


@pytest.mark.anyio
async def test_the_live_client_treats_a_simulated_violation_as_a_defect_and_never_retries() -> None:
    journal: list[JournalEntry] = []
    waits: list[float] = []
    url = f"{LINKEDIN_REST}/leadForms?q=owner&count=10&start=0"
    transport = mock_transport("linkedin", _world(), journal=journal)
    async with build_client("linkedin", transport=transport) as http:
        with pytest.raises(QueryDefect) as caught:
            await send_json(
                http,
                lambda: http.build_request("GET", url, headers=LINKEDIN_HEADERS),
                source="linkedin",
                endpoint="lead_forms",
                policy=_recording_policy(waits),
            )
    assert caught.value.http_status == 400
    assert waits == []
    assert journal == [JournalEntry("linkedin", "lead_forms", "GET", "/rest/leadForms", 400, None)]


# ---------------------------------------------------------------------------------------------
# The settings the client was built with name the webhook secret header
# ---------------------------------------------------------------------------------------------


def _custom_header_settings() -> Settings:
    """Settings naming :data:`CUSTOM_SECRET_HEADER`, while the process keeps the default."""
    settings = Settings(data_mode="simulated", n8n_webhook_secret_header=CUSTOM_SECRET_HEADER)
    assert get_settings().n8n_webhook_secret_header == "X-Webhook-Secret"
    return settings


@pytest.mark.anyio
@pytest.mark.parametrize("strict", [False, True])
async def test_a_transport_validates_the_secret_header_its_settings_name(strict: bool) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(
        "google", _world(), strict=strict, journal=journal, settings=_custom_header_settings()
    )
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.post(
            SIMULATED_WEBHOOK_URL,
            headers={CUSTOM_SECRET_HEADER: SIMULATED_SECRET},
            json=_google_body(),
        )
    assert response.status_code == 200
    assert response.json()["results"][0]["customer"]["id"] == CUSTOMER
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", 200, None)]


@pytest.mark.anyio
async def test_a_transport_rejects_the_process_header_when_its_settings_name_another() -> None:
    settings = _custom_header_settings()
    headers = {get_settings().n8n_webhook_secret_header: SIMULATED_SECRET}
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), journal=journal, settings=settings)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.post(SIMULATED_WEBHOOK_URL, headers=headers, json=_google_body())
    assert response.status_code == 400
    message = _platform_error_message("google", SIMULATED_WEBHOOK_URL, response.json())
    assert f"header {CUSTOM_SECRET_HEADER}: missing or empty" in message
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", 400, None)]

    strict = mock_transport("google", _world(), strict=True, settings=settings)
    async with httpx.AsyncClient(transport=strict) as http:
        with pytest.raises(ContractViolation) as caught:
            await http.post(SIMULATED_WEBHOOK_URL, headers=headers, json=_google_body())
    assert f"header {CUSTOM_SECRET_HEADER}: missing or empty" in caught.value.errors


@pytest.mark.anyio
async def test_a_controller_with_a_custom_secret_header_reads_simulated_google_rows() -> None:
    controller = SourceModeController(_custom_header_settings())
    async with controller.open_clients("google", "simulated") as clients:
        google = clients.require_google()
        rows = [row async for row in google.search("Q-G1")]
    assert [row["customer"]["id"] for row in rows] == [controller.world.google_customer_id]
    assert google.calls_made == 1


# ---------------------------------------------------------------------------------------------
# render(source, request): the product's world
# ---------------------------------------------------------------------------------------------


def _webhook_request(header: str) -> httpx.Request:
    """The first-page Q-G1 webhook request, carrying the secret under ``header``."""
    return httpx.Request(
        "POST", SIMULATED_WEBHOOK_URL, headers={header: SIMULATED_SECRET}, json=_google_body()
    )


def _record_world_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[Settings, SimulatedWorld]]:
    """Record every ``SimulatedWorld.from_settings`` call: its settings and the built world."""
    built: list[tuple[Settings, SimulatedWorld]] = []
    real = SimulatedWorld.from_settings

    def recording(settings: Settings, *args: Any, **kwargs: Any) -> SimulatedWorld:
        world = real(settings, *args, **kwargs)
        built.append((settings, world))
        return world

    monkeypatch.setattr(SimulatedWorld, "from_settings", staticmethod(recording))
    return built


def test_two_argument_render_answers_from_the_world_mock_seed_seeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_SEED", "7")
    get_settings.cache_clear()
    built = _record_world_builds(monkeypatch)
    request = _webhook_request(get_settings().n8n_webhook_secret_header)
    rendered = render("google", request)
    assert (rendered.status, rendered.surface) == (200, "search")
    ((settings, world),) = built
    assert settings is get_settings()
    assert world.seed == 7
    explicit = render("google", request, SimulatedWorld.from_settings(get_settings()))
    assert rendered.body == explicit.body


def test_two_argument_render_builds_one_world_per_settings_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _record_world_builds(monkeypatch)
    request = _webhook_request(get_settings().n8n_webhook_secret_header)
    process = get_settings()
    assert render("google", request).status == 200
    assert render("google", request).status == 200
    first, second = Settings(data_mode="simulated"), Settings(data_mode="simulated")
    answers = [render("google", request, settings=s) for s in (first, first, second, first)]
    assert [answer.status for answer in answers] == [200] * 4
    # One slot keyed on identity (equal settings are distinct instances): reused while the
    # same instance asks, rebuilt for another.
    expected = (process, first, second, first)
    assert len(built) == len(expected)
    assert all(settings is want for (settings, _), want in zip(built, expected, strict=True))
    get_settings.cache_clear()
    assert render("google", request).status == 200
    assert len(built) == len(expected) + 1
    assert built[-1][0] is get_settings()
    assert built[-1][0] is not process


def test_two_argument_render_validates_the_header_its_settings_name() -> None:
    settings = _custom_header_settings()
    accepted = render("google", _webhook_request(CUSTOM_SECRET_HEADER), settings=settings)
    assert (accepted.status, accepted.surface) == (200, "search")
    rejected = render(
        "google", _webhook_request(get_settings().n8n_webhook_secret_header), settings=settings
    )
    assert rejected.status == 400
    message = _platform_error_message("google", SIMULATED_WEBHOOK_URL, rejected.body)
    assert f"header {CUSTOM_SECRET_HEADER}: missing or empty" in message


# ---------------------------------------------------------------------------------------------
# Requests that match no surface of the transport's source
# ---------------------------------------------------------------------------------------------

UNMATCHED = [
    pytest.param(
        "google",
        "GET",
        SIMULATED_WEBHOOK_URL,
        "google: only POSTs carrying a GAQL query reach the webhook",
        id="google-get-on-the-webhook",
    ),
    pytest.param(
        "linkedin",
        "GET",
        f"{LINKEDIN_REST}/adTargetingFacets?access_token={LEAK_MARK}",
        "linkedin: no surface serves GET /rest/adTargetingFacets",
        id="linkedin-rest-unknown-resource",
    ),
    pytest.param(
        "linkedin",
        "POST",
        "https://www.linkedin.com/oauth/v2/authorization",
        "linkedin: no surface serves POST /oauth/v2/authorization",
        id="linkedin-oauth-unknown-endpoint",
    ),
    pytest.param(
        "hubspot",
        "GET",
        f"{HUBSPOT_CRM}/v3/objects/contacts?hapikey={LEAK_MARK}",
        "hubspot: no 2026-09 surface serves GET /crm/v3/objects/contacts",
        id="hubspot-numbered-api-version",
    ),
]

FOREIGN = [
    pytest.param("google", "GET", AD_ACCOUNT_URL, None, "linkedin.ad_account", id="linkedin-call"),
    pytest.param(
        "linkedin",
        "POST",
        f"{HUBSPOT_CRM}/objects/2026-09/deals/search",
        {"limit": 1},
        "hubspot.crm_search",
        id="hubspot-call",
    ),
    pytest.param(
        "hubspot", "POST", SIMULATED_WEBHOOK_URL, _google_body(), "google.search", id="google-call"
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "reason"), UNMATCHED)
async def test_a_request_matching_no_surface_gets_http_400_in_the_platform_shape(
    source: Source, method: str, url: str, reason: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.request(method, url)
        again = await http.request(method, url)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    # The message names the method and path only, never the query string.
    message = _platform_error_message(source, url, response.json())
    assert message == f"Simulated transport: {reason}"
    # Deterministic: the same request gets the same body, request and correlation ids included.
    assert again.content == response.content
    path = httpx.URL(url).path
    assert journal == [JournalEntry(source, UNKNOWN_SURFACE, method, path, 400, None)] * 2


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "body", "addressed"), FOREIGN)
async def test_a_request_meant_for_another_source_gets_http_400_naming_that_source(
    source: Source, method: str, url: str, body: object, addressed: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.request(method, url, json=body)
    assert response.status_code == 400
    message = _platform_error_message(source, url, response.json())
    assert message.startswith(f"Simulated transport: {source}: ")
    assert message.endswith(
        f" (the request addresses {addressed}; each source has its own transport)"
    )
    assert journal == [
        JournalEntry(source, UNKNOWN_SURFACE, method, httpx.URL(url).path, 400, None)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "reason"), UNMATCHED)
async def test_strict_mode_raises_unknown_surface_for_a_request_matching_no_surface(
    source: Source, method: str, url: str, reason: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), strict=True, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(UnknownSurface, match=f"^{re.escape(reason)}$"):
            await http.request(method, url)
    assert journal == [
        JournalEntry(source, UNKNOWN_SURFACE, method, httpx.URL(url).path, None, None)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("source", "method", "url", "body", "addressed"), FOREIGN)
async def test_strict_mode_raises_simulated_transport_error_for_another_source(
    source: Source, method: str, url: str, body: object, addressed: str
) -> None:
    journal: list[JournalEntry] = []
    transport = mock_transport(source, _world(), strict=True, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(SimulatedTransportError) as caught:
            await http.request(method, url, json=body)
    error = caught.value
    assert (error.source, error.addressed) == (source, addressed)
    assert str(error) == (
        f"the {source} transport received a request for {addressed}; "
        "each source's client needs its own mock_transport"
    )
    assert isinstance(error.__cause__, UnknownSurface)
    assert journal == [
        JournalEntry(source, UNKNOWN_SURFACE, method, httpx.URL(url).path, None, None)
    ]


# ---------------------------------------------------------------------------------------------
# Injected faults
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_injected_google_429_and_5xx_carry_retry_after_only_when_the_fault_sets_one() -> None:
    world = _world(
        Fault("google", "search", "http_status", status=429, retry_after=30, times=1),
        Fault("google", "search", "http_status", status=500, match={"query_id": "Q-G8"}, times=1),
    )
    journal: list[JournalEntry] = []
    async with httpx.AsyncClient(
        transport=mock_transport("google", world, journal=journal)
    ) as http:
        limited = await _post_google(http)
        failed = await _post_google(http, query_id="Q-G8")
        recovered = await _post_google(http)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    assert limited.json()["error"]["status"] == "RESOURCE_EXHAUSTED"
    # A Google 5xx without a Google code is n8n's own workflow failure, with no Retry-After.
    assert failed.status_code == 500
    assert "Retry-After" not in failed.headers
    assert failed.json() == {"message": "Error in workflow"}
    assert recovered.status_code == 200
    assert [(entry.status, entry.fault) for entry in journal] == [
        (429, "http_status"),
        (500, "http_status"),
        (200, None),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("retry_after", "expected"), [(7, "7"), (None, None)])
async def test_an_injected_hubspot_503_carries_retry_after_only_when_set(
    retry_after: int | None, expected: str | None
) -> None:
    world = _world(
        Fault("hubspot", "properties", "http_status", status=503, retry_after=retry_after, times=1)
    )
    journal: list[JournalEntry] = []
    async with httpx.AsyncClient(
        transport=mock_transport("hubspot", world, journal=journal)
    ) as http:
        unavailable = await http.get(PROPERTY_URL, headers=HUBSPOT_HEADERS)
        recovered = await http.get(PROPERTY_URL, headers=HUBSPOT_HEADERS)
    assert unavailable.status_code == 503
    assert unavailable.headers.get("Retry-After") == expected
    assert set(unavailable.json()) == {"status", "message", "correlationId", "category"}
    assert recovered.status_code == 200
    assert journal == [
        JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, 503, "http_status"),
        JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, 200, None),
    ]


@pytest.mark.anyio
async def test_the_google_client_waits_the_injected_retry_after_then_reads_the_page() -> None:
    world = _world(Fault("google", "search", "http_status", status=429, retry_after=3, times=1))
    journal: list[JournalEntry] = []
    waits: list[float] = []
    async with build_client(
        "google", transport=mock_transport("google", world, journal=journal)
    ) as http:
        client = _google_client(http, retry=_recording_policy(waits))
        page = await client.search_page("Q-G1", QUERIES["Q-G1"].render())
    assert len(page.rows) == 1
    assert waits == [3.0]
    assert client.calls_made == 2
    assert [(entry.status, entry.fault) for entry in journal] == [
        (429, "http_status"),
        (200, None),
    ]


@pytest.mark.anyio
async def test_an_injected_timeout_raises_read_timeout_at_once() -> None:
    world = _world(Fault("linkedin", "ad_account", "timeout", times=1))
    journal: list[JournalEntry] = []
    transport = mock_transport("linkedin", world, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.ReadTimeout, match="simulated timeout") as caught:
            await http.get(AD_ACCOUNT_URL, headers=LINKEDIN_HEADERS)
        recovered = await http.get(AD_ACCOUNT_URL, headers=LINKEDIN_HEADERS)
    assert caught.value.request.url == httpx.URL(AD_ACCOUNT_URL)
    assert recovered.status_code == 200
    path = f"/rest/adAccounts/{NA_ACCOUNT}"
    assert journal == [
        JournalEntry("linkedin", "ad_account", "GET", path, None, "timeout"),
        JournalEntry("linkedin", "ad_account", "GET", path, 200, None),
    ]


@pytest.mark.anyio
async def test_the_hubspot_client_retries_an_injected_timeout_then_reports_it_transient() -> None:
    world = _world(Fault("hubspot", "properties", "timeout"))
    journal: list[JournalEntry] = []
    waits: list[float] = []
    async with build_client(
        "hubspot", transport=mock_transport("hubspot", world, journal=journal)
    ) as http:
        client = _hubspot_client(http, retry=_recording_policy(waits))
        with pytest.raises(TransientError) as caught:
            await client.property("contacts", "lifecyclestage")
    assert caught.value.http_status is None
    assert len(waits) == 2
    assert (
        journal
        == [JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, None, "timeout")] * 3
    )


@pytest.mark.anyio
async def test_id_level_faults_are_journaled_together_in_spec_order() -> None:
    world = _world(
        Fault("hubspot", "objects_batch_read", "omit_ids", ids=("1001",)),
        Fault(
            "hubspot", "objects_batch_read", "batch_errors", ids=("1002",), category="RATE_LIMITS"
        ),
    )
    journal: list[JournalEntry] = []
    body = {"inputs": [{"id": "1001"}, {"id": "1002"}, {"id": "1003"}], "properties": ["dealname"]}
    async with httpx.AsyncClient(
        transport=mock_transport("hubspot", world, journal=journal)
    ) as http:
        response = await http.post(
            f"https://api.hubapi.com{DEALS_BATCH_PATH}", headers=HUBSPOT_HEADERS, json=body
        )
    assert response.status_code == 207
    errors = {error["category"]: error["context"]["ids"] for error in response.json()["errors"]}
    assert errors == {"OBJECT_NOT_FOUND": ["1003"], "RATE_LIMITS": ["1002"]}
    assert journal == [
        JournalEntry(
            "hubspot", "objects_batch_read", "POST", DEALS_BATCH_PATH, 207, "omit_ids,batch_errors"
        )
    ]


# ---------------------------------------------------------------------------------------------
# Privacy and concurrency
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_journal_and_debug_log_never_record_headers_query_strings_or_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(transport_module, "_LOG", recorder)
    world = _world()
    journal: list[JournalEntry] = []
    email = f"{LEAK_MARK}@sim.invalid"
    async with httpx.AsyncClient(
        transport=mock_transport("google", world, journal=journal)
    ) as http:
        await _post_google(http, secret=f"{LEAK_MARK}-secret")
    async with build_client(
        "linkedin", transport=mock_transport("linkedin", world, journal=journal)
    ) as http:
        await LinkedInClient(http, StaticTokenProvider(f"{LEAK_MARK}-bearer")).list_lead_forms(
            NA_ACCOUNT
        )
    async with httpx.AsyncClient(
        transport=mock_transport("hubspot", world, journal=journal)
    ) as http:
        await http.post(
            f"{HUBSPOT_CRM}/objects/2026-09/contacts/batch/read",
            headers=HUBSPOT_HEADERS,
            json={"idProperty": "email", "inputs": [{"id": email}], "properties": ["email"]},
        )
    assert [entry.surface for entry in journal][:2] == ["search", "lead_forms"]
    assert journal[-1].surface == "objects_batch_read"
    recorded = repr(journal) + repr(recorder.events)
    for secretive in (LEAK_MARK, "sim.invalid", "SELECT", "owner", "count=", "?", "Bearer"):
        assert secretive not in recorded
    assert [event for event, _fields in recorder.events] == ["simulation.request"] * len(journal)
    assert [fields for _event, fields in recorder.events] == [
        {
            "source": entry.source,
            "surface": entry.surface,
            "status": entry.status,
            "fault": entry.fault,
        }
        for entry in journal
    ]


@pytest.mark.anyio
async def test_the_journal_and_log_take_the_fault_label_from_the_rendered_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(transport_module, "_LOG", recorder)
    _patch_render(monkeypatch, lambda rendered: dataclasses.replace(rendered, fault="omit_ids"))
    journal: list[JournalEntry] = []
    async with httpx.AsyncClient(
        transport=mock_transport("hubspot", _world(), journal=journal)
    ) as http:
        response = await http.get(PROPERTY_URL, headers=HUBSPOT_HEADERS)
    assert response.status_code == 200
    assert journal == [
        JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, 200, "omit_ids"),
    ]
    assert recorder.events == [
        (
            "simulation.request",
            {"source": "hubspot", "surface": "properties", "status": 200, "fault": "omit_ids"},
        )
    ]


@pytest.mark.anyio
async def test_a_transport_without_a_journal_still_logs_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(transport_module, "_LOG", recorder)
    async with httpx.AsyncClient(transport=mock_transport("google", _world())) as http:
        response = await _post_google(http)
    assert response.status_code == 200
    assert recorder.events == [
        (
            "simulation.request",
            {"source": "google", "surface": "search", "status": 200, "fault": None},
        )
    ]


@pytest.mark.anyio
async def test_concurrent_requests_on_one_client_are_each_journaled_once() -> None:
    world = _world(Fault("google", "search", "http_status", status=429, retry_after=1, times=5))
    journal: list[JournalEntry] = []
    query_ids = ["Q-G1", "Q-G8"] * 12
    async with httpx.AsyncClient(
        transport=mock_transport("google", world, journal=journal)
    ) as http:
        responses = await asyncio.gather(
            *(_post_google(http, query_id=query_id) for query_id in query_ids)
        )
    assert Counter(response.status_code for response in responses) == Counter({200: 19, 429: 5})
    assert all(
        response.headers["Retry-After"] == "1"
        for response in responses
        if response.status_code == 429
    )
    assert len(journal) == len(query_ids)
    assert Counter((entry.status, entry.fault) for entry in journal) == Counter(
        {(200, None): 19, (429, "http_status"): 5}
    )
    assert {(entry.source, entry.surface, entry.method, entry.path) for entry in journal} == {
        ("google", "search", "POST", "/webhook/google-ads")
    }


# ---------------------------------------------------------------------------------------------
# Serialisation and simulator bugs
# ---------------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    ("value", "error_type"), [(Decimal("1.5"), "TypeError"), (float("nan"), "ValueError")]
)
async def test_a_rendered_body_that_is_not_strict_json_raises_in_both_modes(
    monkeypatch: pytest.MonkeyPatch, strict: bool, value: object, error_type: str
) -> None:
    _patch_render(monkeypatch, lambda rendered: dataclasses.replace(rendered, body={"x": value}))
    journal: list[JournalEntry] = []
    transport = mock_transport("google", _world(), strict=strict, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(
            ContractViolation, match="the rendered body is not strict JSON"
        ) as caught:
            await _post_google(http)
    assert caught.value.surface == "google.search"
    assert f"({error_type}: " in caught.value.errors[0]
    assert journal == [JournalEntry("google", "search", "POST", "/webhook/google-ads", None, None)]


@pytest.mark.anyio
@pytest.mark.parametrize("strict", [False, True])
async def test_a_rendered_contract_violation_is_raised_never_disguised_as_a_platform_error(
    monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    bug = ContractViolation("hubspot.properties", ("$.options[0].colour: unknown field",))

    def broken(
        source: Source,
        request: RequestShape,
        world: SimulatedWorld | None = None,
        *,
        settings: Settings | None = None,
    ) -> RenderedResponse:
        raise bug

    monkeypatch.setattr(transport_module, "render", broken)
    journal: list[JournalEntry] = []
    transport = mock_transport("hubspot", _world(), strict=strict, journal=journal)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ContractViolation) as caught:
            await http.get(PROPERTY_URL, headers=HUBSPOT_HEADERS)
    assert caught.value is bug
    assert journal == [JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, None, None)]


@pytest.mark.anyio
async def test_a_rendered_empty_body_is_sent_without_content_and_keeps_its_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_render(
        monkeypatch,
        lambda rendered: dataclasses.replace(
            rendered, status=503, body=None, headers=(("Retry-After", "5"),)
        ),
    )
    journal: list[JournalEntry] = []
    async with httpx.AsyncClient(
        transport=mock_transport("hubspot", _world(), journal=journal)
    ) as http:
        response = await http.get(PROPERTY_URL, headers=HUBSPOT_HEADERS)
    assert response.status_code == 503
    assert response.content == b""
    assert "content-type" not in response.headers
    assert response.headers["Retry-After"] == "5"
    assert journal == [JournalEntry("hubspot", "properties", "GET", PROPERTY_PATH, 503, None)]


@pytest.mark.anyio
async def test_a_rendered_content_type_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    media = "application/json; charset=utf-8"
    _patch_render(
        monkeypatch,
        lambda rendered: dataclasses.replace(rendered, headers=(("Content-Type", media),)),
    )
    async with httpx.AsyncClient(transport=mock_transport("google", _world())) as http:
        response = await _post_google(http)
    assert response.status_code == 200
    assert response.headers.get_list("content-type") == [media]
    assert response.json()["results"][0]["customer"]["id"] == CUSTOMER


# ---------------------------------------------------------------------------------------------
# The segment fallback's two request forms
# ---------------------------------------------------------------------------------------------

# LinkedIn 202609's answer to the contracted Statistics Finder request with a MEMBER_* pivot.
STATISTICS_REJECTED = Fault(
    "linkedin",
    "ad_analytics_statistics",
    "http_status",
    status=400,
    code="FIELD_INVALID",
    message='"MEMBER_SENIORITY" is not an enum symbol',
)


def _segment_key(row: Any) -> tuple[str, str, str]:
    """A segment row's natural key: campaign, facet and value."""
    return row.campaign_urn, row.facet, row.value


@pytest.mark.anyio
async def test_the_segment_fallback_runs_on_the_simulator_under_its_own_contracts() -> None:
    """A Statistics Finder 400 sends the unchanged client down the fallback, contract-checked.

    The strict transport raises on any request that breaks its contract and every 2xx body is
    validated against the contract that answered, so the discovery call and each per-campaign
    request are checked against their own files. The rows equal the statistics form's on the
    same world: both are drawn from one set of campaign totals.
    """
    journal: list[JournalEntry] = []
    rejecting = mock_transport(
        "linkedin", _world(STATISTICS_REJECTED), strict=True, journal=journal
    )
    async with build_client("linkedin", transport=rejecting) as http:
        client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
        fallback = await client.analytics_segments(NA_ACCOUNT, "MEMBER_SENIORITY", SEGMENT_WINDOW)
        again = await client.analytics_segments(NA_ACCOUNT, "MEMBER_INDUSTRY", SEGMENT_WINDOW)
    async with build_client(
        "linkedin", transport=mock_transport("linkedin", _world(), strict=True)
    ) as http:
        client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
        statistics = await client.analytics_segments(NA_ACCOUNT, "MEMBER_SENIORITY", SEGMENT_WINDOW)

    delivering = _world().linkedin_campaign_delivery(NA_ACCOUNT, *SEGMENT_WINDOW)
    assert delivering
    assert [(entry.surface, entry.status, entry.fault) for entry in journal] == [
        ("ad_analytics_statistics", 400, "http_status"),
        ("ad_analytics_campaign_delivery", 200, None),
        *[("ad_analytics_member_segments", 200, None)] * (2 * len(delivering)),
    ]
    assert fallback.complete
    assert again.complete
    assert statistics.complete
    assert fallback.rows
    assert sorted(fallback.rows, key=_segment_key) == sorted(statistics.rows, key=_segment_key)
    assert {row.facet for row in again.rows} == {"industry"}


@pytest.mark.parametrize(
    ("query", "surface", "value_prefix"),
    [
        pytest.param(
            f"q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&dateRange={SEGMENT_RANGE}"
            f"&accounts=List(urn%3Ali%3AsponsoredAccount%3A{NA_ACCOUNT})"
            f"&fields={SEGMENT_DISCOVERY_FIELDS}",
            "ad_analytics_campaign_delivery",
            "urn:li:sponsoredCampaign:",
            id="campaign-delivery",
        ),
        pytest.param(
            f"q=analytics&pivot=MEMBER_JOB_FUNCTION&timeGranularity=ALL&dateRange={SEGMENT_RANGE}"
            f"&campaigns=List({_campaign_urn(990010000)})&fields={SEGMENT_FIELDS}",
            "ad_analytics_member_segments",
            "urn:li:function:",
            id="member-segments",
        ),
    ],
)
def test_each_segment_fallback_form_renders_a_body_its_contract_accepts(
    query: str, surface: str, value_prefix: str
) -> None:
    """Each form is identified as its own surface and rendered in its contract's shape.

    Discovery elements name one campaign with its impressions in the window; per-campaign
    elements name only the demographic value, the campaign being the request's.
    """
    request = httpx.Request("GET", f"{LINKEDIN_REST}/adAnalytics?{query}", headers=LINKEDIN_HEADERS)
    assert identify_surface("linkedin", request) == surface
    response = render("linkedin", request, _world())
    assert (response.status, response.surface) == (200, surface)
    validate_response(load_contract("linkedin", surface), response.body)
    body = cast(dict[str, Any], response.body)
    elements = body["elements"]
    assert elements
    for element in elements:
        (value,) = element["pivotValues"]
        assert value.startswith(value_prefix)
        assert set(element) <= set(_top_level(load_contract("linkedin", surface).request.fields))
    if surface == "ad_analytics_campaign_delivery":
        expected = _world().linkedin_campaign_delivery(NA_ACCOUNT, *SEGMENT_WINDOW)
        assert [(element["pivotValues"][0], element["impressions"]) for element in elements] == [
            (f"urn:li:sponsoredCampaign:{record.campaign_id}", record.impressions)
            for record in expected
        ]


def test_the_member_segments_form_for_an_unknown_campaign_has_no_elements() -> None:
    """A campaign the world does not hold reports nothing, as LinkedIn omits inactive entities."""
    request = httpx.Request(
        "GET",
        f"{LINKEDIN_REST}/adAnalytics?q=analytics&pivot=MEMBER_SENIORITY&timeGranularity=ALL"
        f"&dateRange={SEGMENT_RANGE}&campaigns=List({_campaign_urn(990019999)})"
        f"&fields={SEGMENT_FIELDS}",
        headers=LINKEDIN_HEADERS,
    )
    response = render("linkedin", request, _world())
    assert response.status == 200
    assert cast(dict[str, Any], response.body)["elements"] == []


# ---------------------------------------------------------------------------------------------
# Live 202609 repeat pages on the timeGranularity=ALL surfaces (analytics_repeat_pages)
# ---------------------------------------------------------------------------------------------

DAILY_RANGE = "(start:(year:2026,month:9,day:28),end:(year:2026,month:10,day:1))"
ACCOUNT_FACET = f"accounts=List(urn%3Ali%3AsponsoredAccount%3A{NA_ACCOUNT})"
STATISTICS_QUERY = (
    f"q=statistics&pivots=List(CAMPAIGN,MEMBER_SENIORITY)&timeGranularity=ALL"
    f"&dateRange={SEGMENT_RANGE}&{ACCOUNT_FACET}&fields={SEGMENT_FIELDS}"
)
DELIVERY_QUERY = (
    f"q=analytics&pivot=CAMPAIGN&timeGranularity=ALL&dateRange={SEGMENT_RANGE}"
    f"&{ACCOUNT_FACET}&fields={SEGMENT_DISCOVERY_FIELDS}"
)
MEMBER_QUERY = (
    f"q=analytics&pivot=MEMBER_SENIORITY&timeGranularity=ALL&dateRange={SEGMENT_RANGE}"
    f"&campaigns=List({_campaign_urn(990010000)})&fields={SEGMENT_FIELDS}"
)


def _repeat_world() -> SimulatedWorld:
    """The default world at :data:`NOW`, paging its ``ALL`` analytics as live 202609 does."""
    config = _config()
    return SimulatedWorld(
        42,
        SPEC.with_linkedin(analytics_repeat_pages=True),
        clock=lambda: NOW,
        content_assets=config.content_assets,
        mapping=config.mapping,
    )


def _analytics_request(query: str) -> httpx.Request:
    """A LinkedIn ``adAnalytics`` GET with ``query`` and the versioned headers."""
    return httpx.Request("GET", f"{LINKEDIN_REST}/adAnalytics?{query}", headers=LINKEDIN_HEADERS)


def _next_link(query: str, start: int, count: int) -> list[dict[str, str]]:
    """The ``paging.links`` of a page whose ``next`` link continues ``query`` at ``start``."""
    href = f"/rest/adAnalytics?{query}&start={start}&count={count}"
    return [{"rel": "next", "href": href, "type": "application/json"}]


@pytest.mark.parametrize(
    ("query", "surface"),
    [
        pytest.param(STATISTICS_QUERY, "ad_analytics_statistics", id="statistics"),
        pytest.param(DELIVERY_QUERY, "ad_analytics_campaign_delivery", id="campaign-delivery"),
        pytest.param(MEMBER_QUERY, "ad_analytics_member_segments", id="member-segments"),
    ],
)
def test_repeat_pages_ignore_start_and_link_on_without_a_total(query: str, surface: str) -> None:
    """Every page of an ``ALL`` surface repeats the first, as live 202609 answered on 2026-10-10.

    The first request carries no ``start`` or ``count``: its page echoes start 0 and
    LinkedIn's default count 10, and links ``next`` only when it holds at least 10 elements.
    Each continuation, whatever its ``start``, returns the same elements, echoes its own
    ``start`` and ``count``, links on to ``start + count`` and never carries a ``total``;
    every page satisfies its contract.
    """
    world = _repeat_world()
    contract = load_contract("linkedin", surface)
    first = render("linkedin", _analytics_request(query), world)
    assert (first.status, first.surface) == (200, surface)
    validate_response(contract, first.body)
    body = cast(dict[str, Any], first.body)
    elements = body["elements"]
    assert elements
    assert body["paging"] == {
        "count": 10,
        "start": 0,
        "links": _next_link(query, 10, 10) if len(elements) >= 10 else [],
    }
    count = len(elements)
    for start in (count, 7 * count, 15_000):
        again = render(
            "linkedin", _analytics_request(f"{query}&start={start}&count={count}"), world
        )
        assert (again.status, again.surface) == (200, surface)
        validate_response(contract, again.body)
        repeated = cast(dict[str, Any], again.body)
        assert repeated["elements"] == elements
        assert repeated["paging"] == {
            "count": count,
            "start": start,
            "links": _next_link(query, start + count, count),
        }
        assert "total" not in repeated["paging"]


def test_repeat_pages_link_on_from_a_full_default_page_only() -> None:
    """A page shorter than its ``count`` has no ``next`` link, as Rest.li pages without a total."""
    world = _repeat_world()
    first = render("linkedin", _analytics_request(MEMBER_QUERY), world)
    elements = cast(dict[str, Any], first.body)["elements"]
    assert 0 < len(elements) < 10
    assert cast(dict[str, Any], first.body)["paging"]["links"] == []
    count = len(elements) + 1
    longer = render("linkedin", _analytics_request(f"{MEMBER_QUERY}&count={count}"), world)
    assert cast(dict[str, Any], longer.body) == {
        "elements": elements,
        "paging": {"count": count, "start": 0, "links": []},
    }


def test_repeat_pages_reject_a_count_below_one() -> None:
    """``count=0`` would link a page to itself: the request is rejected with HTTP 400."""
    request = _analytics_request(f"{MEMBER_QUERY}&start=0&count=0")
    response = render("linkedin", request, _repeat_world())
    assert response.status == 400
    assert "count" in cast(dict[str, Any], response.body)["message"]


def test_repeat_pages_leave_the_daily_surface_unchanged() -> None:
    """``ad_analytics`` (``DAILY``) pages by ``start`` as before: the flag does not reach it."""
    query = (
        f"q=analytics&pivot=CREATIVE&timeGranularity=DAILY&dateRange={DAILY_RANGE}"
        f"&{ACCOUNT_FACET}&fields={DAILY_FIELDS}"
    )
    for suffix in ("", "&start=10&count=10"):
        request = _analytics_request(f"{query}{suffix}")
        assert render("linkedin", request, _repeat_world()) == render("linkedin", request, _world())
    first = cast(dict[str, Any], render("linkedin", _analytics_request(query), _world()).body)
    assert first["elements"]
    assert first["paging"] == {"count": 15_000, "start": 0, "links": []}


def test_analytics_forms_outside_the_four_contracts_are_routed_to_the_daily_contract() -> None:
    """Only ``ALL`` with a CAMPAIGN or MEMBER_* pivot is a fallback form; the rest is rejected.

    A ``DAILY`` campaign request and an ``ALL`` creative request fall to ``ad_analytics``,
    whose contract fixes ``pivot=CREATIVE`` and ``timeGranularity=DAILY``.
    """
    for query, problem in (
        ("q=analytics&pivot=CAMPAIGN&timeGranularity=DAILY", "pivot: must be CREATIVE"),
        ("q=analytics&pivot=CREATIVE&timeGranularity=ALL", "timeGranularity: must be DAILY"),
        ("q=analytics&pivot=MEMBER_SENIORITY&timeGranularity=DAILY", "pivot: must be CREATIVE"),
    ):
        request = httpx.Request(
            "GET",
            f"{LINKEDIN_REST}/adAnalytics?{query}&dateRange={SEGMENT_RANGE}"
            f"&accounts=List(urn%3Ali%3AsponsoredAccount%3A{NA_ACCOUNT})&fields=pivotValues",
            headers=LINKEDIN_HEADERS,
        )
        assert identify_surface("linkedin", request) == "ad_analytics"
        with pytest.raises(ContractViolation) as caught:
            validate_request(load_contract("linkedin", "ad_analytics"), request)
        assert any(problem in error for error in caught.value.errors), caught.value.errors


def _top_level(projection: str | None) -> list[str]:
    """The top-level field names of a flat ``fields`` projection."""
    assert projection is not None
    return projection.split(",")
