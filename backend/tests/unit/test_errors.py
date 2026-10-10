"""Connector error classification, row by row of the source fallback table (AAP 0.3.3).

Every connector failure is one of three kinds, and ``sync/mode.py`` alone decides what each
kind means for a source (AAP 0.11.1):

* **Access denied** (HTTP 401/403, LinkedIn 401 after one token refresh, a Google body
  carrying ``PERMISSION_DENIED``, ``UNAUTHENTICATED``, ``AuthorizationError`` or
  ``AuthenticationError`` even under HTTP 200 from n8n): :class:`AccessDenied`; the source
  switches to simulated data, with the error's ``reason`` as the recorded reason.
* **Transient** (HTTP 429, 5xx, timeouts, ``RESOURCE_EXHAUSTED``, the call budget):
  :class:`TransientError`; retried with backoff, then the datasets turn stale. Never mock.
* **Defect** (HTTP 400, a GAQL ``QueryError``, a validation error): :class:`QueryDefect`;
  the job fails with its query id. No mock, no retry.

Responses are built in memory with an attached request and are never sent; no test touches
the database or the network. Google error bodies use the two shapes the classifier and
``tests/contracts/google/search.json`` document: the REST error envelope
``{"error": {code, message, status, details[]}}`` and a bare google.rpc Status
``{code, message, details[]}``, each optionally carrying a GoogleAdsFailure detail, also
inside n8n's one-element array. The root ``conftest.py`` strips every source credential
from the environment before each test, so message sanitisation never reads a host secret.
"""

import json
import typing
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote

import httpx
import pytest

from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    ConnectorError,
    ErrorKind,
    QueryDefect,
    TransientError,
    classify,
    classify_exception,
    parse_retry_after,
    sanitize_message,
    unwrap_single_item,
)

SourceName = Literal["google", "linkedin", "hubspot"]

SOURCES: tuple[SourceName, ...] = ("google", "linkedin", "hubspot")

SEARCH_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "google" / "search.json"

# One representative URL per source; the request is attached to each response, never sent.
REQUEST_URLS: Mapping[SourceName, str] = {
    "google": "https://n8n.example.test/webhook/google-ads",
    "linkedin": "https://api.linkedin.com/rest/adAccounts/500000001/adCampaigns",
    "hubspot": "https://api.hubapi.com/crm/objects/2026-09/contacts/batch/read",
}
ENDPOINTS: Mapping[SourceName, str] = {
    "google": "search Q-G2",
    "linkedin": "ad_campaigns",
    "hubspot": "contacts_batch_read",
}

GOOGLE_ADS_FAILURE_TYPE = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"

# A successful googleAds:search response as n8n relays it (contract google.search).
SEARCH_ENVELOPE: Mapping[str, object] = {
    "results": [],
    "fieldMask": "campaign.id",
    "queryResourceConsumption": "1",
}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _response(
    status: int,
    *,
    source: SourceName = "google",
    headers: Mapping[str, str] | None = None,
    request_headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Build a received response for ``source`` with an attached request that is never sent."""
    method = "GET" if source == "linkedin" else "POST"
    request = httpx.Request(method, REQUEST_URLS[source], headers=request_headers)
    return httpx.Response(status, headers=headers, request=request)


def _classify(
    status: int,
    body: object,
    *,
    source: SourceName = "google",
    endpoint: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> ConnectorError | None:
    """Classify an in-memory response carrying ``status``, ``headers`` and decoded ``body``."""
    response = _response(status, source=source, headers=headers)
    return classify(response, body, source=source, endpoint=endpoint or ENDPOINTS[source])


def _ads_failure(*errors: Mapping[str, object]) -> dict[str, object]:
    """Render a GoogleAdsFailure detail holding the GoogleAdsError entries ``errors``."""
    return {"@type": GOOGLE_ADS_FAILURE_TYPE, "errors": list(errors), "requestId": "req-test-1"}


def _ads_error(error_type: str, value: str, message: str, **extra: object) -> dict[str, object]:
    """Render one GoogleAdsError: ``errorCode {<camelCase type>: <enum>}`` plus its message."""
    return {"errorCode": {error_type: value}, "message": message, **extra}


def _google_error(
    code: int, status: str, message: str, *errors: Mapping[str, object]
) -> dict[str, object]:
    """Render Google's REST error envelope, with a GoogleAdsFailure when ``errors`` are given."""
    error: dict[str, object] = {"code": code, "message": message, "status": status}
    if errors:
        error["details"] = [_ads_failure(*errors)]
    return {"error": error}


def _bare_status(code: int, message: str) -> dict[str, object]:
    """Render a bare google.rpc Status relayed by n8n as the whole body."""
    return {"code": code, "message": message, "details": []}


def _wrapped(body: object, *, wrap: bool) -> object:
    """Return ``body``, or n8n's "all incoming items" one-element array around it."""
    return [body] if wrap else body


def test_fixtures_follow_the_google_search_contract() -> None:
    """The success envelope and error shapes used here are the ones the contract documents.

    AAP 0.11.2: a fixture may carry no field its contract does not define. The contract's
    description is also the source of the GoogleAdsFailure ``@type`` and the error tokens.
    """
    contract = json.loads(SEARCH_CONTRACT.read_text(encoding="utf-8"))
    body_schema = contract["response"]["body"]
    assert contract["api_version"] == "v25"
    assert set(SEARCH_ENVELOPE) <= set(body_schema["properties"])
    assert set(body_schema["required"]) <= set(SEARCH_ENVELOPE)
    description = body_schema["description"]
    assert GOOGLE_ADS_FAILURE_TYPE in description
    for token in ("PERMISSION_DENIED", "UNAUTHENTICATED", "RESOURCE_EXHAUSTED", "QueryError"):
        assert token in description
    for documented_key in ("errorCode", "quotaErrorDetails", "retryDelay"):
        assert documented_key in description


# ---------------------------------------------------------------------------
# Phase 1: access denied (the source switches to simulated)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    ("status", "unauthorized"),
    [pytest.param(401, True, id="http-401"), pytest.param(403, False, id="http-403")],
)
def test_http_401_and_403_are_access_denied_for_every_source(
    source: SourceName, status: int, unauthorized: bool
) -> None:
    """AAP 0.3.3 row "Access denied": HTTP 401/403 from any source is :class:`AccessDenied`."""
    error = _classify(status, None, source=source)

    assert isinstance(error, AccessDenied)
    assert error.kind == "access_denied"
    assert error.http_status == status
    assert error.is_unauthorized is unauthorized
    assert error.source == source
    assert error.endpoint == ENDPOINTS[source]
    assert not error.retryable
    assert error.reason.startswith(f"{source} {ENDPOINTS[source]}: HTTP {status}: ")


@pytest.mark.parametrize(
    ("source", "status", "body", "code"),
    [
        pytest.param(
            "linkedin",
            401,
            {
                "status": 401,
                "serviceErrorCode": 65601,
                "code": "REVOKED_ACCESS_TOKEN",
                "message": "The token used in the request has been revoked by the user",
            },
            "REVOKED_ACCESS_TOKEN",
            id="linkedin-401-revoked-token",
        ),
        pytest.param(
            "linkedin",
            403,
            {
                "status": 403,
                "serviceErrorCode": 100,
                "code": "ACCESS_DENIED",
                "message": "Not enough permissions to access: leadFormResponses.FINDER-owner",
            },
            "ACCESS_DENIED",
            id="linkedin-403-lead-sync-scope",
        ),
        pytest.param(
            "hubspot",
            401,
            {
                "status": "error",
                "message": "Authentication credentials not found.",
                "category": "INVALID_AUTHENTICATION",
                "correlationId": "4b1c0f1e-0000-4000-8000-000000000001",
            },
            "INVALID_AUTHENTICATION",
            id="hubspot-401-invalid-authentication",
        ),
        pytest.param(
            "hubspot",
            403,
            {
                "status": "error",
                "message": "This app hasn't been granted all required scopes.",
                "category": "MISSING_SCOPES",
            },
            "MISSING_SCOPES",
            id="hubspot-403-missing-scopes",
        ),
    ],
)
def test_platform_error_code_and_endpoint_name_the_denial(
    source: SourceName, status: int, body: Mapping[str, object], code: str
) -> None:
    """AAP 0.3.3 row "Access denied": the platform's code and the failing endpoint are kept.

    ``reason`` becomes the source-mode reason, so it names both.
    """
    error = _classify(status, body, source=source)

    assert isinstance(error, AccessDenied)
    assert error.code == code
    assert error.endpoint == ENDPOINTS[source]
    assert error.reason.startswith(f"{source} {ENDPOINTS[source]}: {code}: ")
    assert str(error) == error.reason


def test_linkedin_401_stays_access_denied_after_the_single_token_refresh() -> None:
    """AAP 0.3.3 row "Access denied": LinkedIn 401 after one token refresh.

    Classification is stateless: the first 401 and the 401 that follows the client's single
    refresh are both :class:`AccessDenied` flagged ``is_unauthorized``, so the client can
    tell a refreshable 401 from a 403 and the second 401 still switches the source.
    """
    body = {"status": 401, "serviceErrorCode": 65600, "code": "INVALID_ACCESS_TOKEN"}
    first = _classify(401, body, source="linkedin", endpoint="lead_form_responses")
    after_refresh = _classify(401, body, source="linkedin", endpoint="lead_form_responses")

    for error in (first, after_refresh):
        assert isinstance(error, AccessDenied)
        assert error.is_unauthorized
        assert error.code == "INVALID_ACCESS_TOKEN"
        assert error.reason.startswith("linkedin lead_form_responses: INVALID_ACCESS_TOKEN: ")


def test_linkedin_refresh_token_rejection_is_access_denied() -> None:
    """AAP 0.3.3 row "Access denied": a refresh that the token endpoint rejects.

    LinkedIn answers a revoked refresh token with an OAuth 2.0 ``invalid_grant`` error,
    which is a credential rejection rather than a defect, even though its status is 400.
    """
    body = {"error": "invalid_grant", "error_description": "The provided authorization grant"}
    error = _classify(400, body, source="linkedin", endpoint="access_token")

    assert isinstance(error, AccessDenied)
    assert error.code == "invalid_grant"
    assert error.is_unauthorized is False
    assert error.reason.startswith("linkedin access_token: invalid_grant: ")


GOOGLE_DENIAL_BODIES = [
    pytest.param(
        _google_error(403, "PERMISSION_DENIED", "The caller does not have permission"),
        "PERMISSION_DENIED",
        id="rest-envelope-PERMISSION_DENIED",
    ),
    pytest.param(
        _google_error(401, "UNAUTHENTICATED", "Request is missing required authentication."),
        "UNAUTHENTICATED",
        id="rest-envelope-UNAUTHENTICATED",
    ),
    pytest.param(
        _bare_status(7, "The caller does not have permission"),
        "PERMISSION_DENIED",
        id="bare-status-code-7",
    ),
    pytest.param(
        _bare_status(16, "Request had invalid authentication credentials."),
        "UNAUTHENTICATED",
        id="bare-status-code-16",
    ),
    pytest.param(
        _google_error(
            403,
            "PERMISSION_DENIED",
            "The caller does not have permission",
            _ads_error(
                "authorizationError",
                "USER_PERMISSION_DENIED",
                "User doesn't have permission to access customer.",
            ),
        ),
        "USER_PERMISSION_DENIED",
        id="authorizationError",
    ),
    pytest.param(
        _google_error(
            401,
            "UNAUTHENTICATED",
            "Request had invalid authentication credentials.",
            _ads_error("authenticationError", "NOT_ADS_USER", "User is not an Ads user."),
        ),
        "NOT_ADS_USER",
        id="authenticationError",
    ),
]


@pytest.mark.parametrize("wrap", [False, True], ids=["object", "one-element-array"])
@pytest.mark.parametrize(("body", "code"), GOOGLE_DENIAL_BODIES)
def test_google_denial_under_http_200_from_n8n_is_access_denied(
    body: Mapping[str, object], code: str, wrap: bool
) -> None:
    """AAP 0.3.3 row "Access denied": a Google denial body is read even under HTTP 200.

    n8n relays Google's error with HTTP 200, in the plain object or its one-element array.
    A GoogleAdsFailure ``errorCode`` value is reported in preference to the rpc status.
    """
    error = _classify(200, _wrapped(body, wrap=wrap), endpoint="search Q-G1")

    assert isinstance(error, AccessDenied)
    assert error.code == code
    assert error.http_status == 200
    assert error.endpoint == "search Q-G1"
    assert error.is_unauthorized is False
    assert "search Q-G1" in error.reason
    assert code in error.reason


# ---------------------------------------------------------------------------
# Phase 2: transient (retry with backoff, then stale; never mock)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_http_429_is_transient_and_honours_retry_after(source: SourceName) -> None:
    """AAP 0.3.3 row "Transient": HTTP 429 with ``Retry-After: 7`` waits 7 seconds."""
    error = _classify(429, None, source=source, headers={"Retry-After": "7"})

    assert isinstance(error, TransientError)
    assert not isinstance(error, AccessDenied)
    assert error.kind == "transient"
    assert error.retryable
    assert error.retry_after == 7.0
    assert error.http_status == 429


def test_hubspot_rate_limit_body_reports_its_error_type() -> None:
    """AAP 0.3.3 row "Transient": HubSpot's 429 ``RATE_LIMIT`` body names the limit hit."""
    body = {
        "status": "error",
        "message": "You have reached your secondly limit.",
        "errorType": "RATE_LIMIT",
        "policyName": "SECONDLY",
    }
    error = _classify(429, body, source="hubspot", headers={"Retry-After": "1"})

    assert isinstance(error, TransientError)
    assert error.code == "RATE_LIMIT"
    assert error.retry_after == 1.0
    assert error.reason.startswith("hubspot contacts_batch_read: RATE_LIMIT: ")


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("status", [500, 502, 503, 504], ids=lambda status: f"http-{status}")
def test_http_5xx_is_transient(source: SourceName, status: int) -> None:
    """AAP 0.3.3 row "Transient": every 5xx is retried, never treated as denied or a defect."""
    error = _classify(status, None, source=source)

    assert isinstance(error, TransientError)
    assert error.http_status == status
    assert error.retry_after is None


def test_n8n_workflow_error_500_is_transient() -> None:
    """AAP 0.3.3 row "Transient": n8n's ``{"message": "Error in workflow"}`` under HTTP 500.

    The contract records that n8n answers this way with no Google body, so at the HTTP level
    it is a transient 5xx: retried, then stale, and never a switch to mock data.
    """
    error = _classify(500, {"message": "Error in workflow"}, endpoint="search Q-G3")

    assert isinstance(error, TransientError)
    assert error.message == "Error in workflow"


RESOURCE_EXHAUSTED_BODY = _google_error(
    429,
    "RESOURCE_EXHAUSTED",
    "Resource has been exhausted (e.g. check quota).",
    _ads_error(
        "quotaError",
        "RESOURCE_EXHAUSTED",
        "Too many requests. Retry in 30 seconds.",
        details={
            "quotaErrorDetails": {
                "rateScope": "DEVELOPER",
                "rateName": "Number of operations for basic access",
                "retryDelay": "30s",
            }
        },
    ),
)


@pytest.mark.parametrize("wrap", [False, True], ids=["object", "one-element-array"])
def test_google_resource_exhausted_under_http_200_is_transient(wrap: bool) -> None:
    """AAP 0.3.3 row "Transient": ``RESOURCE_EXHAUSTED`` in an HTTP 200 body from n8n.

    With no ``Retry-After`` header, the GoogleAdsFailure ``retryDelay`` gives the wait.
    """
    error = _classify(200, _wrapped(RESOURCE_EXHAUSTED_BODY, wrap=wrap), endpoint="search Q-G5")

    assert isinstance(error, TransientError)
    assert not isinstance(error, AccessDenied)
    assert error.code == "RESOURCE_EXHAUSTED"
    assert error.http_status == 200
    assert error.retry_after == 30.0
    assert error.reason.startswith("google search Q-G5: RESOURCE_EXHAUSTED: ")


def test_google_bare_status_code_8_is_transient() -> None:
    """AAP 0.3.3 row "Transient": a bare google.rpc Status with code 8 (RESOURCE_EXHAUSTED)."""
    error = _classify(200, _bare_status(8, "Quota exceeded."), endpoint="search Q-G6")

    assert isinstance(error, TransientError)
    assert error.code == "RESOURCE_EXHAUSTED"


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        pytest.param(
            httpx.ReadTimeout("The read operation timed out"), "TIMEOUT", id="read-timeout"
        ),
        pytest.param(httpx.ConnectTimeout("timed out"), "TIMEOUT", id="connect-timeout"),
        pytest.param(httpx.ConnectError("[Errno 111] Connection refused"), "NETWORK", id="connect"),
        pytest.param(
            httpx.RemoteProtocolError("Server disconnected"), "NETWORK", id="remote-close"
        ),
    ],
)
@pytest.mark.parametrize("source", SOURCES)
def test_timeouts_and_network_failures_are_transient(
    source: SourceName, exc: httpx.TransportError, code: str
) -> None:
    """AAP 0.3.3 row "Transient": a timeout or network failure before any response arrived."""
    error = classify_exception(exc, source=source, endpoint=ENDPOINTS[source])

    assert isinstance(error, TransientError)
    assert error.code == code
    assert error.http_status is None
    assert error.is_unauthorized is False
    assert error.source == source
    assert type(exc).__name__ in error.message


def test_call_budget_exhausted_is_transient_for_google_and_never_access_denied() -> None:
    """AAP 0.3.3 row "Transient": the product's own call budget defers, it never mocks.

    ``CallBudgetExhausted`` is the 1,000-calls-per-24-h cap (``GOOGLE_DAILY_CALL_BUDGET``),
    raised before a call is made; the affected datasets keep their data and turn stale.
    """
    error = CallBudgetExhausted()

    assert isinstance(error, TransientError)
    assert not isinstance(error, AccessDenied)
    assert not isinstance(error, QueryDefect)
    assert error.kind == "transient"
    assert error.source == "google"
    assert error.code == "CALL_BUDGET_EXHAUSTED"
    assert error.http_status is None
    assert error.reason.startswith("google search: CALL_BUDGET_EXHAUSTED: ")
    assert classify_exception(error, source="google", endpoint="search") is error


# ---------------------------------------------------------------------------
# Phase 3: defects (the job fails; no mock, no retry)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_http_400_is_a_query_defect(source: SourceName) -> None:
    """AAP 0.3.3 row "Defect": HTTP 400 from any source is a :class:`QueryDefect`."""
    error = _classify(400, None, source=source)

    assert isinstance(error, QueryDefect)
    assert error.kind == "defect"
    assert not error.retryable
    assert error.http_status == 400


@pytest.mark.parametrize("wrap", [False, True], ids=["object", "one-element-array"])
def test_google_query_error_under_http_200_is_a_defect_with_its_query_id(wrap: bool) -> None:
    """AAP 0.3.3 row "Defect": a GAQL ``QueryError`` in an HTTP 200 body from n8n.

    The query id is taken from the endpoint (``search Q-G6``), so the failed job is logged
    with it; the GoogleAdsFailure ``errorCode`` value is the reported code.
    """
    body = _google_error(
        400,
        "INVALID_ARGUMENT",
        "Request contains an invalid argument.",
        _ads_error("queryError", "UNRECOGNIZED_FIELD", "Unrecognized field in the query."),
    )
    error = _classify(200, _wrapped(body, wrap=wrap), endpoint="search Q-G6")

    assert isinstance(error, QueryDefect)
    assert error.code == "UNRECOGNIZED_FIELD"
    assert error.query_id == "Q-G6"
    assert error.http_status == 200
    assert error.reason.startswith("google search Q-G6: UNRECOGNIZED_FIELD: ")


@pytest.mark.parametrize(
    ("source", "status", "body", "code"),
    [
        pytest.param(
            "google",
            200,
            _google_error(
                400,
                "INVALID_ARGUMENT",
                "Request contains an invalid argument.",
                _ads_error("requestError", "INVALID_PAGE_TOKEN", "Page token is invalid."),
            ),
            "INVALID_PAGE_TOKEN",
            id="google-requestError",
        ),
        pytest.param(
            "google",
            200,
            _google_error(
                400,
                "INVALID_ARGUMENT",
                "Request contains an invalid argument.",
                _ads_error("fieldError", "REQUIRED", "The required field was not present."),
            ),
            "REQUIRED",
            id="google-fieldError",
        ),
        pytest.param(
            "google",
            200,
            _bare_status(3, "Request contains an invalid argument."),
            "INVALID_ARGUMENT",
            id="google-bare-status-code-3",
        ),
        pytest.param(
            "hubspot",
            400,
            {
                "status": "error",
                "message": "Invalid input JSON on line 1",
                "category": "VALIDATION_ERROR",
            },
            "VALIDATION_ERROR",
            id="hubspot-VALIDATION_ERROR",
        ),
        pytest.param(
            "linkedin",
            422,
            {"status": 422, "code": "UNPROCESSABLE_ENTITY", "message": "Invalid dateRange"},
            "UNPROCESSABLE_ENTITY",
            id="linkedin-422",
        ),
    ],
)
def test_validation_failures_are_query_defects(
    source: SourceName, status: int, body: Mapping[str, object], code: str
) -> None:
    """AAP 0.3.3 row "Defect": validation errors the classifier documents never retry.

    Google ``requestError``, ``fieldError`` and ``INVALID_ARGUMENT``, HubSpot
    ``VALIDATION_ERROR`` and any other 4xx are defects in the request the product sent.
    """
    error = _classify(status, body, source=source)

    assert isinstance(error, QueryDefect)
    assert error.code == code
    assert not error.retryable


@pytest.mark.parametrize(
    ("endpoint", "query_id"),
    [
        pytest.param("search Q-G2b", "Q-G2b", id="gaql-query-id"),
        pytest.param("search", None, id="no-query-id"),
    ],
)
def test_query_defect_carries_the_query_id_when_given(endpoint: str, query_id: str | None) -> None:
    """AAP 0.3.3 row "Defect": the job is logged with its query id when the call had one."""
    error = _classify(400, None, endpoint=endpoint)

    assert isinstance(error, QueryDefect)
    assert error.query_id == query_id
    assert error.log_fields["query_id"] == query_id


def test_query_defect_constructor_keeps_an_explicit_query_id() -> None:
    """A connector raising a defect itself passes the query id it ran."""
    error = QueryDefect(
        "a body with neither results nor fieldMask",
        source="google",
        endpoint="search",
        code="MALFORMED_RESPONSE",
        query_id="Q-G3",
    )

    assert error.query_id == "Q-G3"
    assert error.kind == "defect"
    assert error.reason == (
        "google search: MALFORMED_RESPONSE: a body with neither results nor fieldMask"
    )


# ---------------------------------------------------------------------------
# Phase 4: no error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(SEARCH_ENVELOPE, id="search-envelope"),
        pytest.param([SEARCH_ENVELOPE], id="search-envelope-one-element-array"),
        pytest.param(
            {"fieldMask": "campaign.id", "queryResourceConsumption": "1"}, id="zero-row-envelope"
        ),
        pytest.param(
            {
                "results": [{"campaign": {"id": "11", "name": "PERMISSION_DENIED test"}}],
                "fieldMask": "campaign.id,campaign.name",
            },
            id="token-inside-a-row-is-data",
        ),
    ],
)
def test_successful_google_search_response_is_no_error(body: object) -> None:
    """AAP 0.3.3: a normal n8n search envelope is not an error; its rows are never scanned."""
    assert _classify(200, body, endpoint="search Q-G2") is None


def test_hubspot_207_multi_status_is_left_to_the_client() -> None:
    """AAP 0.2.4: a 207 reports failed inputs in ``errors[]``; the client records them.

    The response itself is not a call failure, so :func:`classify` returns ``None``.
    """
    body = {
        "status": "COMPLETE",
        "results": [
            {
                "from": {"id": "101"},
                "to": [{"toObjectId": 9001, "associationTypes": []}],
            }
        ],
        "numErrors": 1,
        "errors": [
            {
                "status": "error",
                "category": "OBJECT_NOT_FOUND",
                "message": "No contact with id 102",
                "context": {"ids": ["102"]},
            }
        ],
        "startedAt": "2026-10-07T12:00:00.000Z",
        "completedAt": "2026-10-07T12:00:00.250Z",
    }
    assert _classify(207, body, source="hubspot", endpoint="associations_batch_read") is None


@pytest.mark.parametrize("source", ["linkedin", "hubspot"])
def test_ordinary_success_is_no_error(source: SourceName) -> None:
    """A 200 from LinkedIn or HubSpot carries no error, whatever its payload."""
    assert _classify(200, {"elements": [], "results": []}, source=source) is None


# ---------------------------------------------------------------------------
# Phase 5: helpers, sanitisation and kinds
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("120", 120.0, id="delta-seconds"),
        pytest.param("7", 7.0, id="delta-seconds-short"),
        pytest.param("Wed, 07 Oct 2026 12:02:00 GMT", 120.0, id="http-date-2-min-ahead"),
        pytest.param("Wed, 07 Oct 2026 12:00:00 GMT", 0.0, id="http-date-now"),
    ],
)
def test_parse_retry_after_reads_seconds_and_http_dates(value: str, expected: float) -> None:
    """``Retry-After`` is delta-seconds or an HTTP-date measured from the injected ``now``."""
    assert parse_retry_after(value, now=NOW) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("soon", id="garbage"),
        pytest.param("-5", id="negative"),
        pytest.param("Wed, 07 Oct 2026 11:59:00 GMT", id="http-date-in-the-past"),
    ],
)
def test_parse_retry_after_gives_none_for_unusable_values(value: str | None) -> None:
    """An absent, unparsable, signed or past value gives no wait."""
    assert parse_retry_after(value, now=NOW) is None


def test_garbage_retry_after_leaves_the_429_transient_without_a_wait() -> None:
    """A 429 stays transient when its ``Retry-After`` cannot be read; backoff then applies."""
    error = _classify(429, None, source="linkedin", headers={"Retry-After": "later"})

    assert isinstance(error, TransientError)
    assert error.retry_after is None


def test_unwrap_single_item_returns_the_only_element() -> None:
    """n8n's one-element array is unwrapped to the object it holds."""
    envelope = dict(SEARCH_ENVELOPE)
    assert unwrap_single_item([envelope]) is envelope


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(dict(SEARCH_ENVELOPE), id="plain-object"),
        pytest.param([dict(SEARCH_ENVELOPE), dict(SEARCH_ENVELOPE)], id="multi-element-list"),
        pytest.param([], id="empty-list"),
        pytest.param(None, id="none"),
    ],
)
def test_unwrap_single_item_leaves_everything_else_unchanged(body: object) -> None:
    """A plain object, or a list of any other length, is returned as it is."""
    assert unwrap_single_item(body) is body


def test_sanitize_message_removes_bearer_tokens_and_emails() -> None:
    """Messages are persisted as source-mode reasons, so they carry no token or email."""
    cleaned = sanitize_message("Bearer abc123 for alice@example.com")

    assert "abc123" not in cleaned
    assert "alice@example.com" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned


def test_sanitize_message_collapses_to_one_line() -> None:
    """Whitespace runs, line breaks included, collapse to single spaces."""
    assert sanitize_message("No contact with email lead@example.com\n  found") == (
        "No contact with email [REDACTED_EMAIL] found"
    )


def test_error_built_from_an_echoing_body_carries_no_email_or_token() -> None:
    """HubSpot can echo the email looked up, and a platform can echo the bearer token."""
    body = {
        "status": "error",
        "message": "Contact bob@example.com rejected Authorization: Bearer hs-echoed-token-1",
        "category": "INVALID_AUTHENTICATION",
    }
    error = _classify(401, body, source="hubspot")

    assert isinstance(error, AccessDenied)
    for rendered in (error.message, error.reason, str(error), repr(error)):
        assert "bob@example.com" not in rendered
        assert "hs-echoed-token-1" not in rendered


# Synthetic addresses in other scripts and their identifying fragments: none may survive.
UNICODE_ADDRESSES: Mapping[str, tuple[str, ...]] = {
    "用户@例子.公司": ("用户", "例子", "公司"),
    "lead@例子.公司": ("lead", "例子"),
    "jöe.smith@example.com": ("jö", "smith"),
    "josé@example.com": ("josé", "example.com"),
    "jo\u0301se@example.com": ("jo\u0301", "example.com"),
    "राम@उदाहरण.भारत": ("राम", "उदाहरण"),
}


@pytest.mark.parametrize("address", sorted(UNICODE_ADDRESSES))
def test_sanitize_message_removes_addresses_in_any_script(address: str) -> None:
    """HubSpot echoes the address it looked up; a non-ASCII one is redacted whole too."""
    cleaned = sanitize_message(f"No contact with email {address}\n  found")

    assert cleaned == "No contact with email [REDACTED_EMAIL] found"


@pytest.mark.parametrize(
    "encoded",
    [
        pytest.param(quote("用户@例子.公司", safe=""), id="utf-8-cjk"),
        pytest.param(quote("jöe.smith@example.com", safe=""), id="utf-8-mixed-script"),
        pytest.param(quote(quote("josé@example.com", safe=""), safe=""), id="utf-8-twice"),
        pytest.param("lead%40example.com", id="encoded-at"),
        pytest.param("lead%2540example.com", id="encoded-at-twice"),
    ],
)
def test_sanitize_message_removes_percent_encoded_addresses(encoded: str) -> None:
    """A URL echoed in error prose carries the address percent-encoded, in UTF-8."""
    cleaned = sanitize_message(f"GET /crm/contacts?email={encoded} returned 404")

    assert cleaned == "GET /crm/contacts?email=[REDACTED_EMAIL] returned 404"


@pytest.mark.parametrize("address", sorted(UNICODE_ADDRESSES))
def test_error_built_from_a_unicode_echo_carries_no_address(address: str) -> None:
    """The reason ``sync/mode.py`` persists and the log fields carry no trace of the address."""
    body = {
        "status": "error",
        "message": f"No contact with email {address} found",
        "category": "OBJECT_NOT_FOUND",
    }
    error = _classify(400, body, source="hubspot")

    assert isinstance(error, QueryDefect)
    assert "[REDACTED_EMAIL]" in error.reason
    rendered = (error.message, error.reason, str(error), repr(error), str(error.log_fields))
    for text in rendered:
        for fragment in UNICODE_ADDRESSES[address]:
            assert fragment not in text, (fragment, text)


# Addresses whose local parts hold RFC 5322 `atext` specials, symbols or emoji, beside letters
# in any script, and their identifying fragments: none may survive.
FAIL_CLOSED_ADDRESSES: Mapping[str, tuple[str, ...]] = {
    "用户!@example.com": ("用户!", "用户"),
    "用户😀@example.com": ("用户", "😀"),
    "用户!a@example.com": ("用户!", "用户"),
    "o'brien@example.com": ("o'", "brien"),
    "patrick.o'brien@example.com": ("patrick", "brien"),
    "a=b@x.com": ("a=b", "a="),
    "{user}@x.com": ("{user}", "user}"),
    "😀@example.com": ("😀",),
    # Quoted local parts and domain labels holding symbols or emoji.
    '"用户"@example.com': ('"用户"', "用户"),
    '"john doe"@example.com': ("john doe", "john"),
    '"a\\"b"@example.com': ('a\\"b', '\\"b'),
    "用户@例子😀.com": ("用户", "例子", "😀"),
    "user@i❤.ws": ("user@", "i❤"),
}


def _spelled(address: str, spelling: str) -> str:
    """``address`` as error prose may echo it: plain, or percent-encoded once or twice."""
    return {
        "plain": address,
        "utf-8": quote(address, safe=""),
        "at-sign": address.replace("@", "%40"),
        "at-sign-twice": address.replace("@", "%2540"),
    }[spelling]


@pytest.mark.parametrize("spelling", ["plain", "utf-8", "at-sign", "at-sign-twice"])
@pytest.mark.parametrize("address", sorted(FAIL_CLOSED_ADDRESSES))
def test_sanitize_message_removes_fail_closed_local_parts_whole(
    address: str, spelling: str
) -> None:
    """No piece of an address survives, whatever ``atext`` special or symbol it holds."""
    cleaned = sanitize_message(f"No contact with {_spelled(address, spelling)}\n  found")

    assert cleaned == "No contact with [REDACTED_EMAIL] found"
    for fragment in FAIL_CLOSED_ADDRESSES[address]:
        assert fragment not in unquote(unquote(cleaned)), fragment


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('No contact with "john\ndoe"@example.com found', id="line-feed"),
        pytest.param('No contact with "john\r\n doe"@example.com found', id="folded"),
        pytest.param('No contact with "用户\\\n"@example.com found', id="escaped-line-feed"),
    ],
)
def test_sanitize_message_removes_a_quoted_local_part_its_collapse_joins(text: str) -> None:
    """A quoted local part holds no line break, but collapsing whitespace joins its halves
    into one; the single-line text the sync persists still loses the whole address."""
    cleaned = sanitize_message(text)

    assert cleaned == "No contact with [REDACTED_EMAIL] found"
    assert sanitize_message(cleaned) == cleaned


@pytest.mark.parametrize("address", sorted(FAIL_CLOSED_ADDRESSES))
def test_error_built_from_a_fail_closed_echo_carries_no_address(address: str) -> None:
    """The reason the sync persists and the log fields hold no piece of the address."""
    body = {
        "status": "error",
        "message": f"No contact with email {address} found; email={address}",
        "category": "OBJECT_NOT_FOUND",
    }
    error = _classify(400, body, source="hubspot")

    assert isinstance(error, QueryDefect)
    assert error.reason.endswith(
        "No contact with email [REDACTED_EMAIL] found; email=[REDACTED_EMAIL]"
    )
    rendered = (error.message, error.reason, str(error), repr(error), str(error.log_fields))
    for text in rendered:
        for fragment in FAIL_CLOSED_ADDRESSES[address]:
            assert fragment not in text, (fragment, text)


@pytest.mark.parametrize("source", SOURCES)
def test_authorization_header_value_never_reaches_the_error(source: SourceName) -> None:
    """Request and response headers are never copied into an error's text.

    Only the status and ``Retry-After`` are read, so an ``Authorization`` value present on
    the request or echoed on the response cannot reach ``str()``, ``reason`` or the logs.
    """
    response = _response(
        401,
        source=source,
        headers={
            "Authorization": "Bearer response-header-token-9",
            "WWW-Authenticate": 'Bearer realm="api", error="invalid_token"',
        },
        request_headers={"Authorization": "Bearer request-header-token-9"},
    )
    error = classify(response, None, source=source, endpoint=ENDPOINTS[source])

    assert isinstance(error, AccessDenied)
    rendered = (str(error), error.reason, repr(error), str(error.log_fields))
    for text in rendered:
        assert "response-header-token-9" not in text
        assert "request-header-token-9" not in text


def test_network_error_text_is_sanitised() -> None:
    """An exception's own text is sanitised before it becomes the error message."""
    exc = httpx.ConnectError("refused for carol@example.com with Bearer net-token-7")
    error = classify_exception(exc, source="hubspot", endpoint="contacts_batch_read")

    assert isinstance(error, TransientError)
    assert "carol@example.com" not in error.reason
    assert "net-token-7" not in error.reason


def test_error_kind_lists_the_three_consequences() -> None:
    """``ErrorKind`` is exactly the three consequences ``sync/mode.py`` distinguishes."""
    assert typing.get_args(ErrorKind) == ("access_denied", "transient", "defect")


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        pytest.param(
            AccessDenied("denied", source="linkedin", endpoint="lead_forms"),
            "access_denied",
            False,
            id="AccessDenied",
        ),
        pytest.param(
            TransientError("busy", source="hubspot", endpoint="crm_search"),
            "transient",
            True,
            id="TransientError",
        ),
        pytest.param(CallBudgetExhausted(), "transient", False, id="CallBudgetExhausted"),
        pytest.param(
            QueryDefect("bad query", source="google", endpoint="search Q-G4"),
            "defect",
            False,
            id="QueryDefect",
        ),
    ],
)
def test_kind_matches_the_class(error: ConnectorError, kind: ErrorKind, retryable: bool) -> None:
    """Each subclass declares its kind; the budget cap is transient but deferred, not retried."""
    assert error.kind == kind
    assert type(error).kind == kind
    assert error.retryable is retryable
    assert isinstance(error, ConnectorError)


def test_connector_error_base_is_abstract() -> None:
    """Connectors raise only the three subclasses, never the base itself (AAP 0.11.1)."""
    with pytest.raises(TypeError, match="abstract"):
        ConnectorError("anything", source="google", endpoint="search")
