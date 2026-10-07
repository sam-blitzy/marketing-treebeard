"""Behavioural tests for ``ads_engine.connectors.http``: client factory, redaction, retries.

Covers :func:`build_client` (base URL, timeouts, default headers, redirects, the injected
transport and the redacting event hooks), :func:`redact_url`, :class:`RetryPolicy`,
:class:`JsonResponse` and :func:`send_json`: retry with backoff and ``Retry-After``, the
error kind raised per response, exact-decimal JSON decoding, the ``before_attempt`` call
budget hook and transport failures.

No test touches the network: every client sends through an ``httpx.MockTransport``, and
every retry policy records its waits instead of sleeping. Logs are captured with
``structlog.testing.capture_logs`` as raw event dicts, before the redaction processors of
``ads_engine.logging`` run, so the assertions prove the HTTP layer itself never emits a
header, body, secret, webhook path or email. Every test runs against an environment
stripped of each variable ``Settings`` reads, because the host exports real credentials;
every secret is an obvious test literal and every address uses a reserved example domain.
"""

import math
import os
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from typing import Any, cast

import httpx
import pytest
from pydantic import AliasChoices, SecretStr, ValidationError
from structlog.testing import capture_logs
from structlog.typing import EventDict
from tenacity import AsyncRetrying, RetryCallState

from ads_engine import __version__
from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    ConnectorError,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.http import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_RETRY,
    SOURCE_BASE_URLS,
    SOURCE_TIMEOUTS,
    STARTED_EXTENSION,
    USER_AGENT,
    JsonResponse,
    RetryPolicy,
    _failed_with,
    build_client,
    redact_url,
    send_json,
)
from ads_engine.logging import REDACTED, REDACTED_EMAIL
from ads_engine.settings import SOURCES, Settings, Source, get_settings

pytestmark = pytest.mark.anyio

N8N_SECRET = "test-n8n-secret-value"
LINKEDIN_TOKEN = "test-linkedin-access-token"
HUBSPOT_TOKEN = "test-hubspot-access-token"
LEAD_EMAIL = "lead@example.com"
ENCODED_EMAIL = "lead%40example.com"
WEBHOOK_HOST = "n8n.example.test"
WEBHOOK_PATH = "/webhook/test-webhook-path"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"
REDACTED_WEBHOOK = f"https://{WEBHOOK_HOST}/<redacted>"
CUSTOMER_ID = "1234567890"

ENDPOINTS: Mapping[Source, str] = {
    "google": "search Q-G2",
    "linkedin": "ad_analytics",
    "hubspot": "objects_batch_read contacts",
}
GOOGLE_ADS_FAILURE = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
SEARCH_BODY: Mapping[str, object] = {
    "results": [{"campaign": {"resourceName": "customers/1234567890/campaigns/1", "id": "1"}}],
    "fieldMask": "campaign.id",
    "queryResourceConsumption": "1",
}

# Backoff windows of the default policy: 2 s then 4 s, each plus up to 1 s of jitter.
FIRST_BACKOFF = (2.0, 3.0)
SECOND_BACKOFF = (4.0, 5.0)


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

    The host exports real credentials. ``build_client`` reads the configured secrets, and
    every connector error sanitises its message against them, so no test may read the
    host environment.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.fixture
def captured() -> Iterator[list[EventDict]]:
    """Capture every structlog event as its raw event dict, before any redaction."""
    with capture_logs() as entries:
        yield entries


def _events(entries: list[EventDict], name: str) -> list[EventDict]:
    """Return the captured events called ``name``, in order."""
    return [entry for entry in entries if entry.get("event") == name]


def _assert_logs_exclude(entries: list[EventDict], *forbidden: str) -> None:
    """Assert no captured event renders any of ``forbidden``, compared case-insensitively."""
    rendered = repr(entries).lower()
    for text in forbidden:
        assert text.lower() not in rendered, text


def _json(status: int, body: object, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
    """Build a JSON response with ``status`` and ``body``."""
    return httpx.Response(status, json=body, headers=headers)


def _google_error(
    code: int,
    status: str,
    message: str,
    error_type: str,
    value: str,
    **error_extra: object,
) -> dict[str, object]:
    """Render Google's documented REST error: a google.rpc Status with a GoogleAdsFailure."""
    ads_error = {"errorCode": {error_type: value}, "message": message, **error_extra}
    failure = {"@type": GOOGLE_ADS_FAILURE, "errors": [ads_error], "requestId": "test-request"}
    return {"error": {"code": code, "message": message, "status": status, "details": [failure]}}


class Harness:
    """Drives :func:`send_json` through a scripted ``httpx.MockTransport``.

    Each request the transport receives is answered with the next scripted outcome: a
    response is returned, an exception is raised. A request beyond the script fails the
    test. ``order`` records ``build`` and ``send`` steps (and whatever a test's
    ``before_attempt`` appends), and ``waits`` the seconds the retry policy would sleep.
    """

    def __init__(self, *outcomes: httpx.Response | Exception) -> None:
        self._outcomes = list(outcomes)
        self.built: list[httpx.Request] = []
        self.sent: list[httpx.Request] = []
        self.order: list[str] = []
        self.waits: list[float] = []

    def transport(self) -> httpx.MockTransport:
        """Return a mock transport answering from the script."""
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.order.append("send")
        self.sent.append(request)
        if not self._outcomes:
            raise AssertionError(f"unexpected request number {len(self.sent)}")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def sleep(self, seconds: float) -> None:
        """Record a wait instead of sleeping."""
        self.waits.append(seconds)

    def policy(self, **overrides: Any) -> RetryPolicy:
        """Return a retry policy that records its waits, with ``overrides`` applied."""
        return RetryPolicy(sleep=self.sleep, **overrides)

    def builder(self, client: httpx.AsyncClient, source: Source) -> Callable[[], httpx.Request]:
        """Return a request builder for ``source`` carrying that source's credential."""

        def build() -> httpx.Request:
            if source == "google":
                request = client.build_request(
                    "POST",
                    WEBHOOK_URL,
                    json={
                        "customer_id": CUSTOMER_ID,
                        "query": "SELECT campaign.id FROM campaign",
                        "page_token": None,
                    },
                    headers={"X-Webhook-Secret": N8N_SECRET},
                )
            elif source == "linkedin":
                request = client.build_request(
                    "GET",
                    "adAnalytics?q=analytics&pivot=CREATIVE&timeGranularity=DAILY",
                    headers={
                        "Authorization": f"Bearer {LINKEDIN_TOKEN}",
                        "Linkedin-Version": "202609",
                    },
                )
            else:
                request = client.build_request(
                    "POST",
                    "/crm/objects/2026-09/contacts/batch/read",
                    json={"idProperty": "email", "inputs": [{"id": LEAD_EMAIL}]},
                    headers={"Authorization": f"Bearer {HUBSPOT_TOKEN}"},
                )
            self.order.append("build")
            self.built.append(request)
            return request

        return build

    async def call(
        self,
        *,
        source: Source = "google",
        endpoint: str | None = None,
        policy: RetryPolicy | None = None,
        before_attempt: Callable[[], Awaitable[None]] | None = None,
        accept_statuses: Collection[int] = (200,),
    ) -> JsonResponse:
        """Send one logical call to ``source`` through :func:`send_json`."""
        async with build_client(source, self.transport(), settings=Settings()) as client:
            return await send_json(
                client,
                self.builder(client, source),
                source=source,
                endpoint=endpoint if endpoint is not None else ENDPOINTS[source],
                policy=policy if policy is not None else self.policy(),
                before_attempt=before_attempt,
                accept_statuses=accept_statuses,
            )


async def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request expected, got {request.method}")


async def _send_one(source: Source, target: str, *, settings: Settings | None) -> httpx.Response:
    """GET ``target`` through a ``source`` client answering HTTP 200.

    ``settings=None`` leaves the client to load the settings itself.
    """
    harness = Harness(_json(200, {}))
    if settings is None:
        client = build_client(source, harness.transport())
    else:
        client = build_client(source, harness.transport(), settings=settings)
    async with client:
        return await client.send(client.build_request("GET", target))


# ---------------------------------------------------------------------------
# Source tables
# ---------------------------------------------------------------------------


def test_every_source_has_a_base_url_and_a_timeout() -> None:
    assert set(SOURCE_BASE_URLS) == set(SOURCES) == set(SOURCE_TIMEOUTS)
    assert dict(SOURCE_BASE_URLS) == {
        "google": "",
        "linkedin": "https://api.linkedin.com/rest/",
        "hubspot": "https://api.hubapi.com",
    }
    assert dict(SOURCE_TIMEOUTS) == {"google": 120.0, "linkedin": 60.0, "hubspot": 60.0}
    assert CONNECT_TIMEOUT_SECONDS == 10.0
    assert f"ads-engine/{__version__}" == USER_AGENT


def test_source_tables_are_read_only() -> None:
    with pytest.raises(TypeError):
        cast("dict[str, str]", SOURCE_BASE_URLS)["google"] = "https://elsewhere.example.test"
    with pytest.raises(TypeError):
        cast("dict[str, float]", SOURCE_TIMEOUTS)["google"] = 1.0


# ---------------------------------------------------------------------------
# build_client()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "base_url", "read_timeout"),
    [
        ("google", "", 120.0),
        ("linkedin", "https://api.linkedin.com/rest/", 60.0),
        ("hubspot", "https://api.hubapi.com", 60.0),
    ],
)
async def test_build_client_configures_each_source(
    source: Source, base_url: str, read_timeout: float
) -> None:
    async with build_client(
        source, httpx.MockTransport(_never_called), settings=Settings()
    ) as client:
        assert client.base_url == httpx.URL(base_url)
        assert client.timeout == httpx.Timeout(read_timeout, connect=10.0)
        assert (client.timeout.read, client.timeout.write, client.timeout.pool) == (
            read_timeout,
            read_timeout,
            read_timeout,
        )
        assert client.timeout.connect == CONNECT_TIMEOUT_SECONDS
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.headers["Accept"] == "application/json"
        assert "Authorization" not in client.headers
        assert client.follow_redirects is False
        assert len(client.event_hooks["request"]) == 1
        assert len(client.event_hooks["response"]) == 1


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("google", WEBHOOK_URL, WEBHOOK_URL),
        (
            "linkedin",
            "adAnalytics?q=analytics&pivot=CREATIVE",
            "https://api.linkedin.com/rest/adAnalytics?q=analytics&pivot=CREATIVE",
        ),
        (
            "linkedin",
            "https://www.linkedin.com/oauth/v2/accessToken",
            "https://www.linkedin.com/oauth/v2/accessToken",
        ),
        (
            "hubspot",
            "/crm/objects/2026-09/contacts/batch/read",
            "https://api.hubapi.com/crm/objects/2026-09/contacts/batch/read",
        ),
    ],
)
async def test_requests_resolve_under_the_base_url_and_use_the_given_transport(
    source: Source, target: str, expected: str
) -> None:
    harness = Harness(_json(200, {"ok": True}))
    async with build_client(source, harness.transport(), settings=Settings()) as client:
        response = await client.send(client.build_request("GET", target))
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    (request,) = harness.sent
    assert str(request.url) == expected
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.headers["Accept"] == "application/json"
    assert isinstance(request.extensions[STARTED_EXTENSION], float)


async def test_redirects_are_not_followed() -> None:
    harness = Harness(httpx.Response(302, headers={"Location": "https://elsewhere.example.test/"}))
    async with build_client("hubspot", harness.transport(), settings=Settings()) as client:
        response = await client.send(client.build_request("GET", "/crm/x"))
    assert response.status_code == 302
    assert len(harness.sent) == 1


@pytest.mark.parametrize("source", ["bing", "Google", ""])
def test_build_client_rejects_an_unknown_source(source: str) -> None:
    with pytest.raises(ValueError, match="unknown source"):
        build_client(cast("Source", source), httpx.MockTransport(_never_called))


# ---------------------------------------------------------------------------
# redact_url()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WEBHOOK_URL, REDACTED_WEBHOOK),
        (f"{WEBHOOK_URL}?token=abc&customer_id={CUSTOMER_ID}", REDACTED_WEBHOOK),
        ("https://acme.app.n8n.cloud/webhook/f00d", "https://acme.app.n8n.cloud/<redacted>"),
        (WEBHOOK_PATH, "/<redacted>"),
    ],
)
def test_google_urls_keep_only_scheme_and_host(url: str, expected: str) -> None:
    assert redact_url(httpx.URL(url), source="google") == expected


@pytest.mark.parametrize(
    ("source", "url", "expected"),
    [
        (
            "hubspot",
            "https://api.hubapi.com/crm/x?hapikey=abc&limit=1",
            f"/crm/x?hapikey={REDACTED}&limit=1",
        ),
        (
            "linkedin",
            "https://www.linkedin.com/oauth/v2/accessToken"
            "?grant_type=refresh_token&refresh_token=abc&client_id=id&client_secret=xyz",
            "/oauth/v2/accessToken?grant_type=refresh_token"
            f"&refresh_token={REDACTED}&client_id=id&client_secret={REDACTED}",
        ),
        (
            "linkedin",
            "https://www.linkedin.com/oauth/v2/authorization?code=AQTcode&state=s1",
            f"/oauth/v2/authorization?code={REDACTED}&state=s1",
        ),
        (
            "linkedin",
            "https://api.linkedin.com/rest/me?access_token=abc&token=def&oauth2_access_token=ghi",
            f"/rest/me?access_token={REDACTED}&token={REDACTED}&oauth2_access_token={REDACTED}",
        ),
        (
            "linkedin",
            "https://api.linkedin.com/rest/adAccounts/1/adCampaigns"
            "?q=search&pageSize=100&pageToken=cursor-1",
            f"/rest/adAccounts/1/adCampaigns?q=search&pageSize=100&pageToken={REDACTED}",
        ),
        (
            "hubspot",
            "https://api.hubapi.com/crm/x?next_page_token=a&clientSecret=b&X-Api-Key=c"
            "&db_password=d",
            f"/crm/x?next_page_token={REDACTED}&clientSecret={REDACTED}&X-Api-Key={REDACTED}"
            f"&db_password={REDACTED}",
        ),
        (
            "hubspot",
            "https://api.hubapi.com/crm/x?access%5Ftoken=abc&limit=1",
            f"/crm/x?access%5Ftoken={REDACTED}&limit=1",
        ),
        (
            "hubspot",
            "https://api.hubapi.com/crm/x?tokens=5&count=10&after=abc",
            "/crm/x?tokens=5&count=10&after=abc",
        ),
        (
            "hubspot",
            "https://api.hubapi.com/crm/x?archived&access_token&limit=1",
            "/crm/x?archived&access_token&limit=1",
        ),
        ("hubspot", "https://api.hubapi.com/crm/x/", "/crm/x/"),
    ],
)
def test_credential_query_values_are_redacted(source: Source, url: str, expected: str) -> None:
    assert redact_url(httpx.URL(url), source=source) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://api.hubapi.com/crm/objects/2026-09/contacts/{ENCODED_EMAIL}?idProperty=email",
            f"/crm/objects/2026-09/contacts/{REDACTED_EMAIL}?idProperty=email",
        ),
        (
            f"https://api.hubapi.com/crm/x?email={ENCODED_EMAIL}&limit=1",
            f"/crm/x?email={REDACTED_EMAIL}&limit=1",
        ),
        (
            f"https://api.hubapi.com/crm/x?email={LEAD_EMAIL}",
            f"/crm/x?email={REDACTED_EMAIL}",
        ),
        (
            f"https://api.hubapi.com/crm/x?q=from%3A{ENCODED_EMAIL}%20today",
            f"/crm/x?q=from:{REDACTED_EMAIL} today",
        ),
        (
            f"https://api.hubapi.com/crm/x?{ENCODED_EMAIL}=1",
            f"/crm/x?{REDACTED_EMAIL}=1",
        ),
    ],
)
def test_percent_encoded_and_plain_emails_are_redacted(url: str, expected: str) -> None:
    redacted = redact_url(httpx.URL(url), source="hubspot")
    assert redacted == expected
    assert "lead" not in redacted


def test_configured_secrets_are_redacted_from_path_and_query() -> None:
    secret = "test-configured-secret"
    url = httpx.URL(f"https://api.hubapi.com/crm/{secret}/x?properties={secret}&limit=1")
    assert redact_url(url, source="hubspot", secrets=(secret,)) == (
        f"/crm/{REDACTED}/x?properties={REDACTED}&limit=1"
    )
    assert redact_url(url, source="hubspot") == f"/crm/{secret}/x?properties={secret}&limit=1"


def test_a_secret_holding_percent_escapes_is_matched_in_the_raw_url() -> None:
    secret = "test-secret%2Fvalue"
    url = httpx.URL(f"https://api.hubapi.com/crm/{secret}?v={secret}")
    assert redact_url(url, source="hubspot", secrets=(secret,)) == f"/crm/{REDACTED}?v={REDACTED}"


def test_clean_components_keep_their_raw_bytes() -> None:
    url = httpx.URL("https://api.linkedin.com/rest/adAnalytics?accounts=List(urn%3Ali%3A1)&x=a%20b")
    assert redact_url(url, source="linkedin", secrets=("unrelated-secret",)) == (
        "/rest/adAnalytics?accounts=List(urn%3Ali%3A1)&x=a%20b"
    )


# ---------------------------------------------------------------------------
# Logging hooks
# ---------------------------------------------------------------------------


async def test_linkedin_logs_carry_no_header_token_cursor_or_email(
    captured: list[EventDict],
) -> None:
    settings = Settings(linkedin_access_token=SecretStr(LINKEDIN_TOKEN))
    harness = Harness(_json(200, {"elements": [{"email": LEAD_EMAIL}]}))
    async with build_client("linkedin", harness.transport(), settings=settings) as client:
        request = client.build_request(
            "GET",
            f"leadFormResponses/{LINKEDIN_TOKEN}?q=owner&email={ENCODED_EMAIL}&pageToken=cursor-1",
            headers={"Authorization": f"Bearer {LINKEDIN_TOKEN}", "Linkedin-Version": "202609"},
        )
        response = await client.send(request)
    assert response.status_code == 200
    expected_url = (
        f"/rest/leadFormResponses/{REDACTED}?q=owner&email={REDACTED_EMAIL}&pageToken={REDACTED}"
    )
    (logged_request,) = _events(captured, "http.request")
    assert logged_request == {
        "event": "http.request",
        "log_level": "debug",
        "source": "linkedin",
        "method": "GET",
        "url": expected_url,
    }
    (logged_response,) = _events(captured, "http.response")
    elapsed_ms = logged_response.pop("elapsed_ms")
    assert isinstance(elapsed_ms, float)
    assert elapsed_ms >= 0.0
    assert logged_response == {
        "event": "http.response",
        "log_level": "info",
        "source": "linkedin",
        "method": "GET",
        "url": expected_url,
        "status": 200,
    }
    _assert_logs_exclude(
        captured,
        LINKEDIN_TOKEN,
        LEAD_EMAIL,
        ENCODED_EMAIL,
        "cursor-1",
        "authorization",
        "bearer",
        "linkedin-version",
        "elements",
    )


async def test_google_logs_carry_no_webhook_path_secret_header_or_body(
    captured: list[EventDict],
) -> None:
    settings = Settings(
        n8n_webhook_secret=SecretStr(N8N_SECRET), n8n_google_ads_webhook_url=WEBHOOK_URL
    )
    harness = Harness(_json(200, SEARCH_BODY))
    async with build_client("google", harness.transport(), settings=settings) as client:
        request = client.build_request(
            "POST",
            WEBHOOK_URL,
            json={"customer_id": CUSTOMER_ID, "query": "SELECT customer.id FROM customer"},
            headers={"X-Webhook-Secret": N8N_SECRET},
        )
        await client.send(request)
    assert [event["url"] for event in captured] == [REDACTED_WEBHOOK, REDACTED_WEBHOOK]
    assert [event["event"] for event in captured] == ["http.request", "http.response"]
    _assert_logs_exclude(
        captured,
        WEBHOOK_PATH,
        "test-webhook-path",
        N8N_SECRET,
        "x-webhook-secret",
        CUSTOMER_ID,
        "customer_id",
        "fieldMask",
    )


async def test_hubspot_logs_strip_configured_secrets_longest_first(
    captured: list[EventDict],
) -> None:
    # HUBSPOT_TOKEN is a prefix of the session secret, so only longest-first leaves nothing
    # of the longer one; the 5-character n8n secret is below the 8-character floor and kept.
    settings = Settings(
        hubspot_access_token=SecretStr(HUBSPOT_TOKEN),
        session_secret=SecretStr(f"{HUBSPOT_TOKEN}-extended"),
        n8n_webhook_secret=SecretStr("short"),
    )
    target = f"/crm/{HUBSPOT_TOKEN}-extended/{HUBSPOT_TOKEN}/short?properties={HUBSPOT_TOKEN}"
    await _send_one("hubspot", target, settings=settings)
    (logged_request,) = _events(captured, "http.request")
    assert logged_request["url"] == f"/crm/{REDACTED}/{REDACTED}/short?properties={REDACTED}"
    _assert_logs_exclude(captured, HUBSPOT_TOKEN, "extended")


async def test_hooks_default_to_the_settings_loaded_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", HUBSPOT_TOKEN)
    get_settings.cache_clear()
    await _send_one("hubspot", f"/crm/objects/2026-09/{HUBSPOT_TOKEN}", settings=None)
    (logged_request,) = _events(captured, "http.request")
    assert logged_request["url"] == f"/crm/objects/2026-09/{REDACTED}"
    _assert_logs_exclude(captured, HUBSPOT_TOKEN)


async def test_invalid_settings_still_give_a_client_with_url_redaction(
    monkeypatch: pytest.MonkeyPatch, captured: list[EventDict]
) -> None:
    monkeypatch.setenv("MOCK_SEED", "not-a-number")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
    response = await _send_one(
        "hubspot",
        f"/crm/objects/2026-09/contacts/{ENCODED_EMAIL}?hapikey=abc&limit=1",
        settings=None,
    )
    assert response.status_code == 200
    (logged_request,) = _events(captured, "http.request")
    assert logged_request["url"] == (
        f"/crm/objects/2026-09/contacts/{REDACTED_EMAIL}?hapikey={REDACTED}&limit=1"
    )


async def test_response_hook_logs_no_elapsed_time_without_a_start_stamp(
    captured: list[EventDict],
) -> None:
    async with build_client(
        "hubspot", httpx.MockTransport(_never_called), settings=Settings()
    ) as client:
        (log_response,) = client.event_hooks["response"]
        request = httpx.Request("GET", "https://api.hubapi.com/crm/x")
        await log_response(httpx.Response(204, request=request))
    (logged_response,) = _events(captured, "http.response")
    assert logged_response["elapsed_ms"] is None
    assert logged_response["status"] == 204
    assert logged_response["url"] == "/crm/x"


# ---------------------------------------------------------------------------
# RetryPolicy and JsonResponse
# ---------------------------------------------------------------------------


def test_default_retry_is_the_plan_policy() -> None:
    assert (
        DEFAULT_RETRY.attempts,
        DEFAULT_RETRY.base_seconds,
        DEFAULT_RETRY.max_seconds,
        DEFAULT_RETRY.jitter_seconds,
        DEFAULT_RETRY.max_retry_after_seconds,
    ) == (3, 2.0, 30.0, 1.0, 120.0)
    assert RetryPolicy() == DEFAULT_RETRY


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"attempts": 0}, "attempts must be at least 1"),
        ({"attempts": -2}, "attempts must be at least 1"),
        ({"attempts": True}, "attempts must be an integer"),
        ({"attempts": 2.0}, "attempts must be an integer"),
        ({"attempts": "3"}, "attempts must be an integer"),
        ({"base_seconds": -1.0}, "base_seconds must be a finite, non-negative number"),
        ({"max_seconds": math.inf}, "max_seconds must be a finite, non-negative number"),
        ({"jitter_seconds": math.nan}, "jitter_seconds must be a finite, non-negative number"),
        (
            {"max_retry_after_seconds": -0.5},
            "max_retry_after_seconds must be a finite, non-negative number",
        ),
    ],
)
def test_retry_policy_rejects_invalid_values(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RetryPolicy(**overrides)


def test_retry_policy_accepts_zero_durations_and_one_attempt() -> None:
    policy = RetryPolicy(
        attempts=1, base_seconds=0, max_seconds=0, jitter_seconds=0, max_retry_after_seconds=0
    )
    assert policy.attempts == 1


def test_json_response_repr_hides_body_and_headers() -> None:
    response = JsonResponse(
        status=200,
        body={"email": LEAD_EMAIL},
        headers=httpx.Headers({"Set-Cookie": "session=test-cookie-value"}),
    )
    assert repr(response) == "JsonResponse(status=200)"
    assert str(response) == "JsonResponse(status=200)"


# ---------------------------------------------------------------------------
# send_json(): accepted responses and decoding
# ---------------------------------------------------------------------------


async def test_google_success_is_returned_after_one_send() -> None:
    harness = Harness(_json(200, SEARCH_BODY, headers={"X-Request-Id": "req-1"}))
    response = await harness.call()
    assert response.status == 200
    assert response.body == SEARCH_BODY
    assert response.headers["X-Request-Id"] == "req-1"
    assert len(harness.sent) == 1
    assert harness.waits == []
    (request,) = harness.sent
    assert request.headers["X-Webhook-Secret"] == N8N_SECRET


async def test_json_numbers_with_a_fraction_or_exponent_decode_as_decimals() -> None:
    content = b'{"conversions": 0.123456789, "clicks": 3, "costMicros": "1500000", "r": 1e-3}'
    harness = Harness(httpx.Response(200, content=content))
    response = await harness.call(source="linkedin")
    assert response.body == {
        "conversions": Decimal("0.123456789"),
        "clicks": 3,
        "costMicros": "1500000",
        "r": Decimal("0.001"),
    }
    assert isinstance(response.body["conversions"], Decimal)
    assert type(response.body["clicks"]) is int


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_non_standard_json_constants_are_a_defect(constant: str) -> None:
    harness = Harness(httpx.Response(200, content=f'{{"cost": {constant}}}'.encode()))
    with pytest.raises(QueryDefect, match="response is not JSON") as excinfo:
        await harness.call(source="hubspot")
    assert excinfo.value.code == "validation"
    assert excinfo.value.http_status == 200
    assert len(harness.sent) == 1
    assert harness.waits == []


@pytest.mark.parametrize("content", [b"", b"  \r\n\t"])
async def test_an_empty_body_decodes_to_none(content: bytes) -> None:
    harness = Harness(httpx.Response(200, content=content))
    response = await harness.call(source="linkedin")
    assert response.status == 200
    assert response.body is None


@pytest.mark.parametrize(
    "content",
    [b"<html>ok</html>", b"\xff\xfe\xfa", b"[" * 100_000 + b"]" * 100_000],
    ids=["html", "undecodable", "too-deep"],
)
async def test_an_accepted_status_with_a_non_json_body_is_a_defect(content: bytes) -> None:
    harness = Harness(httpx.Response(200, content=content))
    with pytest.raises(QueryDefect, match="response is not JSON") as excinfo:
        await harness.call()
    assert excinfo.value.code == "validation"
    assert excinfo.value.query_id == "Q-G2"
    assert len(harness.sent) == 1


async def test_accepted_statuses_return_multi_status_batches() -> None:
    body = {"status": "COMPLETE", "results": [], "numErrors": 1, "errors": []}
    harness = Harness(_json(207, body))
    response = await harness.call(source="hubspot", accept_statuses=(200, 207))
    assert response.status == 207
    assert response.body == body


async def test_accepted_statuses_return_a_not_found_as_data() -> None:
    body = {"status": "error", "message": "Property does not exist", "category": "OBJECT_NOT_FOUND"}
    harness = Harness(_json(404, body))
    response = await harness.call(
        source="hubspot", endpoint="properties contacts", accept_statuses=(200, 404)
    )
    assert response.status == 404
    assert response.body == body


async def test_a_not_found_outside_the_accepted_statuses_is_a_defect() -> None:
    harness = Harness(_json(404, {"status": "error", "message": "Not found"}))
    with pytest.raises(QueryDefect) as excinfo:
        await harness.call(source="hubspot")
    assert excinfo.value.http_status == 404
    assert len(harness.sent) == 1


async def test_non_google_bodies_are_not_classified_when_accepted() -> None:
    body = {"message": "PERMISSION_DENIED", "status": 403}
    harness = Harness(_json(200, body))
    response = await harness.call(source="linkedin")
    assert response.body == body


async def test_google_rows_naming_an_error_token_are_not_misread() -> None:
    body = {
        "results": [{"campaign": {"id": "1", "name": "PERMISSION_DENIED test"}}],
        "fieldMask": "campaign.id,campaign.name",
    }
    harness = Harness(_json(200, body))
    response = await harness.call()
    assert response.body == body


@pytest.mark.parametrize(
    ("status", "headers"),
    [(207, {}), (302, {"Location": "https://elsewhere.example.test/"}), (204, {})],
)
async def test_an_unexpected_status_without_an_error_signal_is_a_defect(
    status: int, headers: dict[str, str]
) -> None:
    harness = Harness(httpx.Response(status, headers=headers, content=b"moved or partial"))
    with pytest.raises(QueryDefect, match=f"unexpected HTTP {status} response") as excinfo:
        await harness.call(source="hubspot")
    assert excinfo.value.code == "unexpected_status"
    assert excinfo.value.http_status == status
    assert excinfo.value.query_id == "contacts"
    assert len(harness.sent) == 1
    assert harness.waits == []


# ---------------------------------------------------------------------------
# send_json(): retries
# ---------------------------------------------------------------------------


async def test_a_503_then_200_returns_after_one_backoff_wait(captured: list[EventDict]) -> None:
    harness = Harness(httpx.Response(503), _json(200, SEARCH_BODY))
    response = await harness.call()
    assert response.body == SEARCH_BODY
    assert len(harness.sent) == 2
    (wait,) = harness.waits
    assert FIRST_BACKOFF[0] <= wait <= FIRST_BACKOFF[1]
    (logged_error,) = _events(captured, "connector.error")
    assert logged_error == {
        "event": "connector.error",
        "log_level": "warning",
        "source": "google",
        "endpoint": "search Q-G2",
        "kind": "transient",
        "code": None,
        "http_status": 503,
        "attempt": 1,
        "retryable": True,
    }
    (logged_retry,) = _events(captured, "http.retry")
    assert logged_retry == {
        "event": "http.retry",
        "log_level": "info",
        "source": "google",
        "endpoint": "search Q-G2",
        "attempt": 1,
        "wait_seconds": round(wait, 3),
        "kind": "transient",
        "code": None,
        "http_status": 503,
        "retry_after": None,
    }


async def test_three_429s_raise_the_last_transient_error_after_three_sends(
    captured: list[EventDict],
) -> None:
    harness = Harness(
        *(_json(429, {"status": 429, "message": f"throttled {n}"}) for n in ("one", "two", "three"))
    )
    with pytest.raises(TransientError) as excinfo:
        await harness.call(source="linkedin")
    assert excinfo.value.http_status == 429
    assert excinfo.value.message == "throttled three"
    assert len(harness.sent) == 3
    first, second = harness.waits
    assert FIRST_BACKOFF[0] <= first <= FIRST_BACKOFF[1]
    assert SECOND_BACKOFF[0] <= second <= SECOND_BACKOFF[1]
    assert [event["attempt"] for event in _events(captured, "connector.error")] == [1, 2, 3]
    assert [event["attempt"] for event in _events(captured, "http.retry")] == [1, 2]


async def test_each_attempt_builds_a_fresh_request() -> None:
    harness = Harness(httpx.Response(500), httpx.Response(502), _json(200, {}))
    await harness.call(source="hubspot")
    assert len(harness.built) == 3
    assert len({id(request) for request in harness.built}) == 3
    assert all(sent is built for sent, built in zip(harness.sent, harness.built, strict=True))


async def test_backoff_doubles_from_the_base_and_is_capped() -> None:
    harness = Harness(*(httpx.Response(503) for _ in range(4)))
    policy = harness.policy(attempts=4, base_seconds=2.0, max_seconds=5.0, jitter_seconds=0.0)
    with pytest.raises(TransientError):
        await harness.call(source="hubspot", policy=policy)
    assert harness.waits == [2.0, 4.0, 5.0]
    assert len(harness.sent) == 4


async def test_a_single_attempt_policy_never_retries() -> None:
    harness = Harness(httpx.Response(503))
    with pytest.raises(TransientError):
        await harness.call(policy=harness.policy(attempts=1))
    assert len(harness.sent) == 1
    assert harness.waits == []


@pytest.mark.parametrize(
    ("retry_after", "cap", "expected"),
    [
        ("5", None, 5.0),
        ("0.5", None, 0.5),
        ("600", None, 120.0),
        ("600", 10.0, 10.0),
        (format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True), None, 120.0),
    ],
    ids=["seconds", "fraction", "capped-default", "capped-policy", "http-date"],
)
async def test_retry_after_replaces_the_backoff_up_to_the_cap(
    retry_after: str, cap: float | None, expected: float, captured: list[EventDict]
) -> None:
    harness = Harness(
        _json(
            429,
            {"status": 429, "message": "Too many requests"},
            headers={"Retry-After": retry_after},
        ),
        _json(200, {"elements": []}),
    )
    policy = harness.policy() if cap is None else harness.policy(max_retry_after_seconds=cap)
    response = await harness.call(source="linkedin", policy=policy)
    assert response.body == {"elements": []}
    assert harness.waits == [expected]
    (logged_retry,) = _events(captured, "http.retry")
    assert logged_retry["wait_seconds"] == expected
    assert logged_retry["retry_after"] is not None


async def test_an_unparsable_retry_after_falls_back_to_the_backoff() -> None:
    harness = Harness(httpx.Response(429, headers={"Retry-After": "soon"}), _json(200, {}))
    await harness.call(source="hubspot")
    (wait,) = harness.waits
    assert FIRST_BACKOFF[0] <= wait <= FIRST_BACKOFF[1]


async def test_google_quota_in_http_200_is_retried_after_its_retry_delay() -> None:
    quota = _google_error(
        429,
        "RESOURCE_EXHAUSTED",
        "Too many requests",
        "quotaError",
        "RESOURCE_EXHAUSTED",
        details={"quotaErrorDetails": {"rateScope": "DEVELOPER", "retryDelay": "30s"}},
    )
    harness = Harness(_json(200, quota), _json(200, SEARCH_BODY))
    response = await harness.call()
    assert response.body == SEARCH_BODY
    assert harness.waits == [30.0]
    assert len(harness.sent) == 2


async def test_n8n_workflow_errors_are_transient_and_retried() -> None:
    harness = Harness(*(_json(500, {"message": "Error in workflow"}) for _ in range(3)))
    with pytest.raises(TransientError) as excinfo:
        await harness.call()
    assert excinfo.value.http_status == 500
    assert len(harness.sent) == 3
    assert len(harness.waits) == 2


# ---------------------------------------------------------------------------
# send_json(): errors raised on their first occurrence
# ---------------------------------------------------------------------------


async def test_a_400_is_sent_once_and_raised_as_a_query_defect() -> None:
    body = _google_error(400, "INVALID_ARGUMENT", "Unrecognized field", "queryError", "BAD_FIELD")
    harness = Harness(_json(400, body))
    with pytest.raises(QueryDefect) as excinfo:
        await harness.call()
    assert excinfo.value.http_status == 400
    assert excinfo.value.code == "BAD_FIELD"
    assert excinfo.value.query_id == "Q-G2"
    assert len(harness.sent) == 1
    assert harness.waits == []


async def test_a_403_is_sent_once_and_raised_as_access_denied() -> None:
    body = {"status": "error", "message": "Missing scopes", "category": "MISSING_SCOPES"}
    harness = Harness(_json(403, body))
    with pytest.raises(AccessDenied) as excinfo:
        await harness.call(source="hubspot")
    assert excinfo.value.code == "MISSING_SCOPES"
    assert excinfo.value.http_status == 403
    assert len(harness.sent) == 1
    assert harness.waits == []


async def test_a_linkedin_401_is_access_denied_marked_unauthorized() -> None:
    harness = Harness(_json(401, {"status": 401, "code": "EXPIRED_ACCESS_TOKEN", "message": "x"}))
    with pytest.raises(AccessDenied) as excinfo:
        await harness.call(source="linkedin")
    assert excinfo.value.is_unauthorized
    assert len(harness.sent) == 1


@pytest.mark.parametrize("wrapped", [False, True], ids=["object", "n8n-item-array"])
async def test_google_permission_denied_in_http_200_is_access_denied(
    wrapped: bool, captured: list[EventDict]
) -> None:
    denial = _google_error(
        403,
        "PERMISSION_DENIED",
        "The caller does not have permission",
        "authorizationError",
        "USER_PERMISSION_DENIED",
    )
    harness = Harness(_json(200, [denial] if wrapped else denial))
    with pytest.raises(AccessDenied) as excinfo:
        await harness.call()
    assert excinfo.value.http_status == 200
    assert excinfo.value.code == "USER_PERMISSION_DENIED"
    assert len(harness.sent) == 1
    assert harness.waits == []
    (logged_error,) = _events(captured, "connector.error")
    assert logged_error["kind"] == "access_denied"
    assert logged_error["retryable"] is False


async def test_google_query_error_in_http_200_is_a_defect() -> None:
    body = _google_error(400, "INVALID_ARGUMENT", "Unrecognized field", "queryError", "BAD_FIELD")
    harness = Harness(_json(200, body))
    with pytest.raises(QueryDefect) as excinfo:
        await harness.call()
    assert excinfo.value.http_status == 200
    assert excinfo.value.query_id == "Q-G2"
    assert len(harness.sent) == 1


@pytest.mark.parametrize(
    ("source", "status", "text", "kind", "sends"),
    [
        ("hubspot", 502, "<html><body>Bad Gateway</body></html>", TransientError, 3),
        ("linkedin", 400, "Bad Request", QueryDefect, 1),
        ("google", 500, "PERMISSION_DENIED: The caller does not have permission", AccessDenied, 1),
    ],
)
async def test_non_json_error_bodies_are_classified_from_their_text(
    source: Source, status: int, text: str, kind: type[ConnectorError], sends: int
) -> None:
    harness = Harness(*(httpx.Response(status, text=text) for _ in range(sends)))
    with pytest.raises(kind) as excinfo:
        await harness.call(source=source)
    assert excinfo.value.http_status == status
    assert len(harness.sent) == sends


async def test_error_logs_and_messages_carry_no_body_or_email(captured: list[EventDict]) -> None:
    body = {
        "status": "error",
        "message": f"Property values were not valid for {LEAD_EMAIL}",
        "category": "VALIDATION_ERROR",
    }
    harness = Harness(_json(400, body))
    with pytest.raises(QueryDefect) as excinfo:
        await harness.call(source="hubspot")
    assert len(harness.sent) == 1
    assert LEAD_EMAIL not in str(excinfo.value)
    assert REDACTED_EMAIL in excinfo.value.message
    (logged_error,) = _events(captured, "connector.error")
    assert logged_error["code"] == "VALIDATION_ERROR"
    _assert_logs_exclude(captured, LEAD_EMAIL, "property values", HUBSPOT_TOKEN, "authorization")


# ---------------------------------------------------------------------------
# send_json(): transport failures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make_error", "kind", "code", "sends"),
    [
        (lambda: httpx.ConnectError("connection refused"), TransientError, "NETWORK", 3),
        (lambda: httpx.RemoteProtocolError("peer closed"), TransientError, "NETWORK", 3),
        (lambda: httpx.ReadTimeout("timed out"), TransientError, "TIMEOUT", 3),
        (lambda: TimeoutError("timed out"), TransientError, "TIMEOUT", 3),
        (lambda: httpx.InvalidURL("invalid host"), QueryDefect, "CLIENT", 1),
        (lambda: httpx.UnsupportedProtocol("unsupported scheme"), QueryDefect, "CLIENT", 1),
        (lambda: httpx.StreamConsumed(), QueryDefect, "CLIENT", 1),
    ],
    ids=[
        "connect",
        "remote-protocol",
        "read-timeout",
        "builtin-timeout",
        "invalid-url",
        "protocol",
        "stream",
    ],
)
async def test_transport_failures_are_classified(
    make_error: Callable[[], Exception],
    kind: type[ConnectorError],
    code: str,
    sends: int,
    captured: list[EventDict],
) -> None:
    errors = [make_error() for _ in range(sends)]
    harness = Harness(*errors)
    with pytest.raises(kind) as excinfo:
        await harness.call()
    assert type(excinfo.value) is kind
    assert excinfo.value.code == code
    assert excinfo.value.http_status is None
    assert excinfo.value.__cause__ is errors[-1]
    assert len(harness.sent) == sends
    assert len(harness.waits) == sends - 1
    assert [event["attempt"] for event in _events(captured, "connector.error")] == list(
        range(1, sends + 1)
    )


async def test_a_connector_error_from_the_transport_is_reraised_unchanged(
    captured: list[EventDict],
) -> None:
    denial = AccessDenied(
        "simulated denial", source="google", endpoint="search Q-G2", code="PERMISSION_DENIED"
    )
    harness = Harness(denial)
    with pytest.raises(AccessDenied) as excinfo:
        await harness.call()
    assert excinfo.value is denial
    assert len(harness.sent) == 1
    (logged_error,) = _events(captured, "connector.error")
    assert logged_error["code"] == "PERMISSION_DENIED"


async def test_a_transient_error_from_the_transport_is_retried() -> None:
    errors = [
        TransientError("injected fault", source="google", endpoint="search Q-G2", http_status=503)
        for _ in range(3)
    ]
    harness = Harness(*errors)
    with pytest.raises(TransientError) as excinfo:
        await harness.call()
    assert excinfo.value is errors[-1]
    assert len(harness.sent) == 3


# ---------------------------------------------------------------------------
# send_json(): before_attempt and argument checks
# ---------------------------------------------------------------------------


async def test_before_attempt_runs_before_every_send() -> None:
    harness = Harness(httpx.Response(503), httpx.Response(503), _json(200, SEARCH_BODY))

    async def acquire() -> None:
        harness.order.append("acquire")

    await harness.call(before_attempt=acquire)
    assert harness.order == ["acquire", "build", "send"] * 3


async def test_an_exhausted_call_budget_sends_nothing_and_is_not_retried(
    captured: list[EventDict],
) -> None:
    harness = Harness(_json(200, SEARCH_BODY))

    async def exhausted() -> None:
        raise CallBudgetExhausted()

    with pytest.raises(CallBudgetExhausted) as excinfo:
        await harness.call(before_attempt=exhausted)
    # Transient by kind, so the datasets turn stale, but deferred rather than retried.
    assert excinfo.value.kind == "transient"
    assert excinfo.value.code == "CALL_BUDGET_EXHAUSTED"
    assert harness.built == []
    assert harness.sent == []
    assert harness.waits == []
    assert _events(captured, "http.retry") == []


async def test_a_budget_exhausted_before_a_retry_stops_the_retries() -> None:
    harness = Harness(httpx.Response(503))
    acquired: list[int] = []

    async def acquire() -> None:
        acquired.append(len(acquired) + 1)
        if len(acquired) == 2:
            raise CallBudgetExhausted()

    with pytest.raises(CallBudgetExhausted):
        await harness.call(before_attempt=acquire)
    assert acquired == [1, 2]
    assert len(harness.sent) == 1
    assert len(harness.waits) == 1


async def test_send_json_rejects_an_unknown_source() -> None:
    harness = Harness()
    async with build_client("hubspot", harness.transport(), settings=Settings()) as client:
        with pytest.raises(ValueError, match="unknown source 'bing'"):
            await send_json(
                client,
                harness.builder(client, "hubspot"),
                source=cast("Source", "bing"),
                endpoint="crm_search",
                policy=harness.policy(),
            )
    assert harness.built == []
    assert harness.sent == []


async def test_send_json_rejects_empty_accept_statuses() -> None:
    harness = Harness()
    with pytest.raises(ValueError, match="accept_statuses must name at least one HTTP status"):
        await harness.call(source="hubspot", accept_statuses=())
    assert harness.built == []
    assert harness.sent == []


def test_failed_with_reads_only_failed_outcomes() -> None:
    state = RetryCallState(retry_object=AsyncRetrying(), fn=None, args=(), kwargs={})
    assert _failed_with(state) is None
    state.set_result({"ok": True})
    assert _failed_with(state) is None
    error = TransientError("x", source="google", endpoint="search")
    state.set_exception((TransientError, error, None))
    assert _failed_with(state) is error
