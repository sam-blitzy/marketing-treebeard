"""Behavioural tests for the classification half of ``ads_engine.connectors.errors``.

Covers :func:`classify`, :func:`classify_exception` and the body-inspection helpers behind
them: the HTTP status per source; Google bodies that n8n relays under any status, also in
n8n's one-element array shape; numeric google.rpc codes of a bare Status; OAuth credential
rejections; ``Retry-After`` and Google ``retryDelay``; query ids on defects; the scan
limits; and the precedence between conflicting signals.

Error bodies follow the shapes the contract files document: Google's REST error envelope
and bare google.rpc Status, each optionally wrapping a GoogleAdsFailure
(``tests/contracts/google/search.json``), LinkedIn's OAuth
errors (``tests/contracts/linkedin/access_token.json``) and HubSpot's 207 batch response,
whose documented sample is loaded verbatim. Responses are built in memory with an attached
request and are never sent. Every test runs against an environment stripped of each
variable ``Settings`` reads, because building an error sanitises its message against the
configured secrets.
"""

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest
from pydantic import AliasChoices

from ads_engine.connectors.errors import (
    _GOOGLE_ERROR_KEYS,
    _GOOGLE_RPC_CODES,
    _MAX_SCAN_DEPTH,
    _MAX_SCAN_NODES,
    _MAX_SCAN_TEXT,
    AccessDenied,
    CallBudgetExhausted,
    ConnectorError,
    QueryDefect,
    TransientError,
    _as_int,
    _as_status,
    _google_body_status,
    _google_rpc_status_signal,
    _google_scan_roots,
    _kind_for_status,
    _platform_code,
    _platform_message,
    _query_id,
    _select_signal,
    _Signal,
    _walk,
    classify,
    classify_exception,
)
from ads_engine.settings import Settings, Source, get_settings

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

WEBHOOK_URL = "https://n8n.example.test/webhook/google-ads"
REQUEST_URLS: Mapping[Source, str] = {
    "google": WEBHOOK_URL,
    "linkedin": "https://api.linkedin.com/rest/adAccounts/500000001",
    "hubspot": "https://api.hubapi.com/crm/associations/2026-09/contacts/meetings/batch/read",
}
GOOGLE_ENDPOINT = "search Q-G2"

GOOGLE_ADS_FAILURE = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
SEARCH_ENVELOPE: Mapping[str, object] = {
    "results": [],
    "fieldMask": "campaign.id",
    "queryResourceConsumption": "1",
}
N8N_WORKFLOW_ERROR: Mapping[str, object] = {"message": "Error in workflow"}
NO_BODY_MESSAGE = "error reported in the response body"
TOKEN = "PERMISSION_DENIED"


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

    The host exports real credentials, and every built error sanitises its message
    against the loaded settings, so no test may read the host environment.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _response(
    status: int, *, source: Source = "google", headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """Build a received response for ``source`` with an attached request that is never sent."""
    method = "GET" if source == "linkedin" else "POST"
    request = httpx.Request(method, REQUEST_URLS[source])
    return httpx.Response(status, headers=headers, request=request)


def _classify(
    status: int,
    body: object,
    *,
    source: Source = "google",
    endpoint: str = GOOGLE_ENDPOINT,
    headers: Mapping[str, str] | None = None,
) -> ConnectorError | None:
    """Classify an in-memory response with ``status``, ``headers`` and decoded ``body``."""
    response = _response(status, source=source, headers=headers)
    return classify(response, body, source=source, endpoint=endpoint)


def _google_error(
    code: int,
    status: str,
    message: str,
    *errors: Mapping[str, object],
) -> dict[str, object]:
    """Render Google's documented REST error: a google.rpc Status with a GoogleAdsFailure."""
    failure = _ads_failure(*errors)
    return {"error": {"code": code, "message": message, "status": status, "details": [failure]}}


def _ads_failure(*errors: Mapping[str, object]) -> dict[str, object]:
    """Render a GoogleAdsFailure detail carrying the GoogleAdsErrors ``errors``."""
    return {"@type": GOOGLE_ADS_FAILURE, "errors": list(errors), "requestId": "test-request"}


def _bare_status(code: object, message: str, *details: Mapping[str, object]) -> dict[str, object]:
    """Render a bare google.rpc Status ``{code, message, details[]}``, relayed as the body."""
    return {"code": code, "message": message, "details": list(details)}


def _ads_error(error_type: str, value: str, message: str, **extra: object) -> dict[str, object]:
    """Render one GoogleAdsError: its ``errorCode`` entry, message and extra fields."""
    return {"errorCode": {error_type: value}, "message": message, **extra}


def _nested(value: object, levels: int) -> object:
    """Wrap ``value`` in ``levels`` single-key mappings, so it sits at depth ``levels``."""
    for _ in range(levels):
        value = {"wrap": value}
    return value


PERMISSION_DENIED_BODY = _google_error(
    403,
    "PERMISSION_DENIED",
    "The caller does not have permission",
    _ads_error(
        "authorizationError",
        "USER_PERMISSION_DENIED",
        "User doesn't have permission to access customer.",
    ),
)
UNAUTHENTICATED_BODY = _google_error(
    401,
    "UNAUTHENTICATED",
    "Request is missing required authentication credential.",
    _ads_error("authenticationError", "OAUTH_TOKEN_REVOKED", "The OAuth token was revoked."),
)
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
                "rateScope": "ACCOUNT",
                "rateName": "Requests per service per method",
                "retryDelay": "30s",
            }
        },
    ),
)
QUERY_ERROR_BODY = _google_error(
    400,
    "INVALID_ARGUMENT",
    "Request contains an invalid argument.",
    _ads_error("queryError", "UNRECOGNIZED_FIELD", "Unrecognized field in the query."),
)


def _with_retry_delay(delay: str) -> dict[str, object]:
    """Render a ``RESOURCE_EXHAUSTED`` body whose ``quotaErrorDetails`` carry ``delay``."""
    return _google_error(
        429,
        "RESOURCE_EXHAUSTED",
        "Resource has been exhausted (e.g. check quota).",
        _ads_error(
            "quotaError",
            "RESOURCE_EXHAUSTED",
            "Too many requests.",
            details={"quotaErrorDetails": {"retryDelay": delay}},
        ),
    )


ACCESS_REFUSED = "Access refused"
USER_PERMISSION_DENIED_FAILURE = _ads_failure(
    _ads_error(
        "authorizationError",
        "USER_PERMISSION_DENIED",
        "User doesn't have permission to access customer.",
    )
)
QUOTA_STATUS = _bare_status(
    8,
    "Resource has been exhausted (e.g. check quota).",
    _ads_failure(
        _ads_error(
            "quotaError",
            "RESOURCE_EXHAUSTED",
            "Too many requests. Retry in 30 seconds.",
            details={
                "quotaErrorDetails": {
                    "rateScope": "ACCOUNT",
                    "rateName": "Requests per service per method",
                    "retryDelay": "30s",
                }
            },
        )
    ),
)
INVALID_ARGUMENT_MESSAGE = "Request contains an invalid argument."

# google.rpc.Code as google/rpc/code.proto defines it: each code, its canonical name, the
# HTTP status the proto maps it to, and the error that HTTP status classifies as.
RPC_CODE_CASES: tuple[tuple[int, str, int, type[ConnectorError]], ...] = (
    (1, "CANCELLED", 499, QueryDefect),
    (2, "UNKNOWN", 500, TransientError),
    (3, "INVALID_ARGUMENT", 400, QueryDefect),
    (4, "DEADLINE_EXCEEDED", 504, TransientError),
    (5, "NOT_FOUND", 404, QueryDefect),
    (6, "ALREADY_EXISTS", 409, QueryDefect),
    (7, "PERMISSION_DENIED", 403, AccessDenied),
    (8, "RESOURCE_EXHAUSTED", 429, TransientError),
    (9, "FAILED_PRECONDITION", 400, QueryDefect),
    (10, "ABORTED", 409, QueryDefect),
    (11, "OUT_OF_RANGE", 400, QueryDefect),
    (12, "UNIMPLEMENTED", 501, TransientError),
    (13, "INTERNAL", 500, TransientError),
    (14, "UNAVAILABLE", 503, TransientError),
    (15, "DATA_LOSS", 500, TransientError),
    (16, "UNAUTHENTICATED", 401, AccessDenied),
)


def _hubspot_207_sample() -> object:
    """Load the HubSpot associations 207 response documented in its contract, verbatim."""
    contract = json.loads((CONTRACTS / "hubspot" / "associations_batch_read.json").read_text())
    return contract["response"]["documented_sample"]


# ---------------------------------------------------------------------------
# classify: HTTP status, per source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "status", "body", "code", "message"),
    [
        pytest.param("google", 401, None, None, "Unauthorized", id="google-401-no-body"),
        pytest.param(
            "google",
            403,
            {"code": 403, "message": "Authorization data is wrong!"},
            None,
            "Authorization data is wrong!",
            id="google-403-n8n-header-auth",
        ),
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
            "The token used in the request has been revoked by the user",
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
            "Not enough permissions to access: leadFormResponses.FINDER-owner",
            id="linkedin-403-access-denied",
        ),
        pytest.param(
            "hubspot",
            401,
            {
                "status": "error",
                "message": "Authentication credentials not found.",
                "correlationId": "00000000-0000-0000-0000-000000000001",
                "category": "INVALID_AUTHENTICATION",
            },
            "INVALID_AUTHENTICATION",
            "Authentication credentials not found.",
            id="hubspot-401-invalid-authentication",
        ),
        pytest.param(
            "hubspot",
            403,
            {
                "status": "error",
                "message": "This app hasn't been granted all required scopes to make this call.",
                "correlationId": "00000000-0000-0000-0000-000000000002",
                "category": "MISSING_SCOPES",
                "errors": [{"message": "One or more of the following scopes are required."}],
            },
            "MISSING_SCOPES",
            "This app hasn't been granted all required scopes to make this call.",
            id="hubspot-403-missing-scopes",
        ),
    ],
)
def test_401_and_403_are_access_denied_for_every_source(
    source: Source, status: int, body: object, code: str | None, message: str
) -> None:
    """HTTP 401/403 is access denied with the platform code; only 401 is ``is_unauthorized``."""
    endpoint = "ad_account" if source == "linkedin" else "search"
    error = _classify(status, body, source=source, endpoint=endpoint)

    assert isinstance(error, AccessDenied)
    assert error.kind == "access_denied"
    assert error.retryable is False
    assert error.source == source
    assert error.endpoint == endpoint
    assert error.code == code
    assert error.http_status == status
    assert error.message == message
    assert error.is_unauthorized is (status == 401)


@pytest.mark.parametrize(
    ("source", "status", "headers", "body", "code", "retry_after", "message"),
    [
        pytest.param(
            "google",
            429,
            {"Retry-After": "7"},
            None,
            None,
            7.0,
            "Too Many Requests",
            id="google-429-retry-after-seconds",
        ),
        pytest.param(
            "linkedin",
            429,
            {"Retry-After": "2.5"},
            {"status": 429, "code": "TOO_MANY_REQUESTS", "message": "Resource level throttle"},
            "TOO_MANY_REQUESTS",
            2.5,
            "Resource level throttle",
            id="linkedin-429-decimal-retry-after",
        ),
        pytest.param(
            "hubspot",
            429,
            None,
            {
                "status": "error",
                "message": "You have reached your secondly limit.",
                "errorType": "RATE_LIMIT",
                "correlationId": "00000000-0000-0000-0000-000000000003",
                "policyName": "SECONDLY",
            },
            "RATE_LIMIT",
            None,
            "You have reached your secondly limit.",
            id="hubspot-429-error-type",
        ),
        pytest.param(
            "linkedin",
            503,
            {"Retry-After": "soon"},
            None,
            None,
            None,
            "Service Unavailable",
            id="linkedin-503-unparsable-retry-after",
        ),
        pytest.param("hubspot", 502, None, None, None, None, "Bad Gateway", id="hubspot-502"),
        pytest.param(
            "linkedin",
            599,
            None,
            None,
            None,
            None,
            "no error message in the response",
            id="5xx-without-reason-phrase",
        ),
        pytest.param(
            "google",
            500,
            None,
            N8N_WORKFLOW_ERROR,
            None,
            None,
            "Error in workflow",
            id="n8n-500-error-in-workflow",
        ),
    ],
)
def test_429_and_5xx_are_transient_with_retry_after(
    source: Source,
    status: int,
    headers: Mapping[str, str] | None,
    body: object,
    code: str | None,
    retry_after: float | None,
    message: str,
) -> None:
    """HTTP 429 and 5xx are transient, and a parsable ``Retry-After`` becomes the delay."""
    error = _classify(status, body, source=source, endpoint="search", headers=headers)

    assert isinstance(error, TransientError)
    assert error.kind == "transient"
    assert error.retryable is True
    assert error.code == code
    assert error.http_status == status
    assert error.retry_after == retry_after
    assert error.message == message


def test_n8n_live_workflow_error_reads_as_a_transient_5xx() -> None:
    """n8n's invalid-GAQL answer, HTTP 500 ``Error in workflow``, has no token: transient."""
    error = _classify(500, N8N_WORKFLOW_ERROR)

    assert isinstance(error, TransientError)
    assert error.reason == "google search Q-G2: HTTP 500: Error in workflow"
    assert error.retry_after is None


@pytest.mark.parametrize(
    ("source", "status", "endpoint", "body", "code", "query_id", "message"),
    [
        pytest.param(
            "google", 400, "search Q-G6", None, None, "Q-G6", "Bad Request", id="google-400"
        ),
        pytest.param(
            "linkedin",
            400,
            "ad_analytics",
            {"status": 400, "code": "PARAM_INVALID", "message": "Invalid param"},
            "PARAM_INVALID",
            None,
            "Invalid param",
            id="linkedin-400-param-invalid",
        ),
        pytest.param(
            "linkedin",
            422,
            "lead_form_responses",
            {"status": 422, "serviceErrorCode": "422001", "message": "Unprocessable"},
            "422001",
            None,
            "Unprocessable",
            id="linkedin-422-string-service-code",
        ),
        pytest.param(
            "hubspot",
            400,
            "crm_search deals",
            {
                "status": "error",
                "message": "Invalid input JSON on line 1",
                "category": "VALIDATION_ERROR",
            },
            "VALIDATION_ERROR",
            "deals",
            "Invalid input JSON on line 1",
            id="hubspot-400-validation-error",
        ),
        pytest.param(
            "hubspot",
            404,
            "properties",
            {"status": "error", "message": "Not found", "subCategory": "PROPERTY_NOT_FOUND"},
            "PROPERTY_NOT_FOUND",
            None,
            "Not found",
            id="hubspot-404-sub-category",
        ),
    ],
)
def test_400_and_other_4xx_are_query_defects(
    source: Source,
    status: int,
    endpoint: str,
    body: object,
    code: str | None,
    query_id: str | None,
    message: str,
) -> None:
    """HTTP 400 and every other 4xx is a defect carrying the endpoint's query id."""
    error = _classify(status, body, source=source, endpoint=endpoint)

    assert isinstance(error, QueryDefect)
    assert error.kind == "defect"
    assert error.retryable is False
    assert error.code == code
    assert error.http_status == status
    assert error.query_id == query_id
    assert error.message == message


@pytest.mark.parametrize(
    ("source", "status", "body"),
    [
        pytest.param("google", 200, SEARCH_ENVELOPE, id="google-envelope"),
        pytest.param("google", 200, [SEARCH_ENVELOPE], id="google-envelope-in-n8n-array"),
        pytest.param(
            "google",
            200,
            {"fieldMask": "campaign.id", "queryResourceConsumption": "1"},
            id="google-zero-rows",
        ),
        pytest.param(
            "google",
            200,
            {
                "results": [
                    {
                        "campaign": {
                            "resourceName": "customers/1234567890/campaigns/111",
                            "id": "111",
                            "name": "PERMISSION_DENIED RESOURCE_EXHAUSTED QueryError test",
                            "status": "UNAUTHENTICATED",
                        }
                    }
                ],
                "fieldMask": "campaign.id,campaign.name,campaign.status",
            },
            id="google-rows-are-never-scanned",
        ),
        pytest.param("google", 200, {}, id="google-empty-object"),
        pytest.param("google", 200, None, id="google-no-body"),
        pytest.param(
            "google", 200, {"message": "request from an unauthenticated user"}, id="google-prose"
        ),
        pytest.param("google", 200, {"message": "UNAVAILABLE"}, id="google-status-word-in-text"),
        pytest.param("google", 200, {"httpCode": "200"}, id="google-body-status-2xx"),
        pytest.param("google", 302, None, id="google-redirect"),
        pytest.param("hubspot", 207, _hubspot_207_sample(), id="hubspot-207-documented-sample"),
        pytest.param(
            "hubspot",
            207,
            {
                "status": "COMPLETE",
                "results": [],
                "numErrors": 1,
                "errors": [
                    {
                        "status": "error",
                        "category": "OBJECT_NOT_FOUND",
                        "subCategory": "crm.associations.NO_ASSOCIATIONS_FOUND",
                        "message": "No associations found",
                    }
                ],
            },
            id="hubspot-207-no-associations",
        ),
        pytest.param("hubspot", 204, None, id="hubspot-204"),
        pytest.param("linkedin", 200, {"elements": []}, id="linkedin-empty-page"),
        pytest.param("linkedin", 200, {"message": TOKEN}, id="linkedin-google-token-ignored"),
        pytest.param(
            "linkedin", 200, {"error": "invalid_grant"}, id="linkedin-oauth-body-under-200"
        ),
    ],
)
def test_success_responses_without_a_body_signal_are_not_errors(
    source: Source, status: int, body: object
) -> None:
    """A 1xx/2xx/3xx with no body signal gives ``None``; the caller judges the status."""
    assert _classify(status, body, source=source, endpoint="search") is None


# ---------------------------------------------------------------------------
# classify: Google bodies relayed by n8n
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        pytest.param(
            PERMISSION_DENIED_BODY,
            "USER_PERMISSION_DENIED",
            "The caller does not have permission",
            id="permission-denied",
        ),
        pytest.param(
            [PERMISSION_DENIED_BODY],
            "USER_PERMISSION_DENIED",
            "The caller does not have permission",
            id="permission-denied-in-n8n-array",
        ),
        pytest.param(
            UNAUTHENTICATED_BODY,
            "OAUTH_TOKEN_REVOKED",
            "Request is missing required authentication credential.",
            id="unauthenticated",
        ),
        pytest.param(
            [UNAUTHENTICATED_BODY],
            "OAUTH_TOKEN_REVOKED",
            "Request is missing required authentication credential.",
            id="unauthenticated-in-n8n-array",
        ),
        pytest.param(
            {
                "errors": [
                    _ads_error(
                        "authorizationError",
                        "DEVELOPER_TOKEN_NOT_APPROVED",
                        "The developer token is not approved.",
                    )
                ]
            },
            "DEVELOPER_TOKEN_NOT_APPROVED",
            "The developer token is not approved.",
            id="authorization-error-code",
        ),
        pytest.param(
            [{"errors": [{"errorCode": {"authenticationError": ""}}]}],
            "AuthenticationError",
            NO_BODY_MESSAGE,
            id="authentication-error-key-without-value-in-n8n-array",
        ),
        pytest.param(
            {
                "message": "Forbidden - perhaps check your credentials?",
                "description": "PERMISSION_DENIED: The caller does not have permission",
                "httpCode": "403",
            },
            "PERMISSION_DENIED",
            "Forbidden - perhaps check your credentials?",
            id="n8n-envelope-token-in-description",
        ),
        pytest.param(
            {"message": "AuthorizationError.USER_PERMISSION_DENIED from the Google Ads node"},
            "AuthorizationError",
            "AuthorizationError.USER_PERMISSION_DENIED from the Google Ads node",
            id="authorization-error-in-prose",
        ),
        pytest.param(
            "UNAUTHENTICATED: Request had invalid authentication credentials.",
            "UNAUTHENTICATED",
            "UNAUTHENTICATED: Request had invalid authentication credentials.",
            id="text-body",
        ),
        pytest.param(
            {"error": {"status": " permission_denied "}},
            "PERMISSION_DENIED",
            NO_BODY_MESSAGE,
            id="whole-value-token-any-case",
        ),
        pytest.param(
            {
                "results": [],
                "fieldMask": "campaign.id",
                "errors": [{"message": "PERMISSION_DENIED: customer not accessible"}],
            },
            "PERMISSION_DENIED",
            "PERMISSION_DENIED: customer not accessible",
            id="error-subtree-of-success-payload",
        ),
        pytest.param(
            [SEARCH_ENVELOPE, {"error": {"status": "UNAUTHENTICATED"}}],
            "UNAUTHENTICATED",
            NO_BODY_MESSAGE,
            id="multi-item-n8n-list",
        ),
    ],
)
def test_google_denial_in_a_200_body_is_access_denied(
    body: object, code: str, message: str
) -> None:
    """n8n's HTTP 200 carrying a Google denial token is access denied, wrapped or not."""
    error = _classify(200, body)

    assert isinstance(error, AccessDenied)
    assert error.code == code
    assert error.message == message
    assert error.http_status == 200
    assert error.is_unauthorized is False


@pytest.mark.parametrize(
    ("status", "body", "headers", "code", "retry_after"),
    [
        pytest.param(200, RESOURCE_EXHAUSTED_BODY, None, "RESOURCE_EXHAUSTED", 30.0, id="200"),
        pytest.param(
            200, [RESOURCE_EXHAUSTED_BODY], None, "RESOURCE_EXHAUSTED", 30.0, id="200-in-array"
        ),
        pytest.param(
            429,
            RESOURCE_EXHAUSTED_BODY,
            {"Retry-After": "7"},
            "RESOURCE_EXHAUSTED",
            7.0,
            id="retry-after-header-wins-over-retry-delay",
        ),
        pytest.param(
            429,
            RESOURCE_EXHAUSTED_BODY,
            {"Retry-After": "later"},
            "RESOURCE_EXHAUSTED",
            30.0,
            id="unparsable-retry-after-falls-back-to-retry-delay",
        ),
        pytest.param(
            200, _with_retry_delay("1.5s"), None, "RESOURCE_EXHAUSTED", 1.5, id="decimal-delay"
        ),
        pytest.param(
            200, _with_retry_delay(" 0s "), None, "RESOURCE_EXHAUSTED", 0.0, id="zero-delay"
        ),
        pytest.param(
            200, _with_retry_delay("30"), None, "RESOURCE_EXHAUSTED", None, id="delay-without-unit"
        ),
        pytest.param(
            200, _with_retry_delay("-5s"), None, "RESOURCE_EXHAUSTED", None, id="negative-delay"
        ),
        pytest.param(
            200,
            _with_retry_delay("9" * 400 + "s"),
            None,
            "RESOURCE_EXHAUSTED",
            None,
            id="non-finite-delay",
        ),
        pytest.param(
            200,
            {"errors": [{"errorCode": {"quotaError": "RESOURCE_TEMPORARILY_EXHAUSTED"}}]},
            None,
            "RESOURCE_TEMPORARILY_EXHAUSTED",
            None,
            id="temporarily-exhausted",
        ),
        pytest.param(
            200,
            {"message": "Too many requests", "description": "RESOURCE_EXHAUSTED: quota"},
            None,
            "RESOURCE_EXHAUSTED",
            None,
            id="n8n-envelope-token-in-description",
        ),
        pytest.param(
            200,
            {
                "error": {
                    "code": 503,
                    "message": "The service is unavailable.",
                    "status": "UNAVAILABLE",
                }
            },
            None,
            "UNAVAILABLE",
            None,
            id="rpc-status-unavailable",
        ),
        pytest.param(
            200,
            {"error": {"status": "DEADLINE_EXCEEDED"}},
            None,
            "DEADLINE_EXCEEDED",
            None,
            id="rpc-status-deadline-exceeded",
        ),
        pytest.param(
            200, {"error": {"status": " INTERNAL "}}, None, "INTERNAL", None, id="rpc-internal"
        ),
        pytest.param(
            200,
            {"error": {"code": 503, "message": "The service is unavailable."}},
            None,
            None,
            None,
            id="body-error-code-503",
        ),
        pytest.param(
            200,
            {"message": "Too many requests", "httpCode": "429"},
            None,
            None,
            None,
            id="n8n-body-http-code-429",
        ),
    ],
)
def test_google_transient_body_is_transient_with_retry_delay(
    status: int,
    body: object,
    headers: Mapping[str, str] | None,
    code: str | None,
    retry_after: float | None,
) -> None:
    """``RESOURCE_EXHAUSTED``, an exact transient ``error.status`` or a 429/5xx body code."""
    error = _classify(status, body, headers=headers)

    assert isinstance(error, TransientError)
    assert error.code == code
    assert error.http_status == status
    assert error.retry_after == retry_after


@pytest.mark.parametrize(
    ("status", "body", "code", "message"),
    [
        pytest.param(
            400,
            QUERY_ERROR_BODY,
            "UNRECOGNIZED_FIELD",
            "Request contains an invalid argument.",
            id="400-query-error",
        ),
        pytest.param(
            200,
            [QUERY_ERROR_BODY],
            "UNRECOGNIZED_FIELD",
            "Request contains an invalid argument.",
            id="200-query-error-in-n8n-array",
        ),
        pytest.param(
            200,
            {
                "message": "Bad request - please check your parameters",
                "description": "QueryError.UNRECOGNIZED_FIELD: Unrecognized field",
                "httpCode": "400",
            },
            "QueryError",
            "Bad request - please check your parameters",
            id="n8n-envelope-query-error-in-description",
        ),
        pytest.param(
            200,
            {"errors": [{"errorCode": {"requestError": "INVALID_CUSTOMER_ID"}}]},
            "INVALID_CUSTOMER_ID",
            NO_BODY_MESSAGE,
            id="request-error",
        ),
        pytest.param(
            200,
            {"errors": [{"errorCode": {"fieldError": "REQUIRED"}, "message": "Required."}]},
            "REQUIRED",
            "Required.",
            id="field-error",
        ),
        pytest.param(
            200,
            {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}},
            "INVALID_ARGUMENT",
            "bad",
            id="invalid-argument",
        ),
        pytest.param(
            200,
            {"message": "Bad request - please check your parameters", "httpCode": "400"},
            None,
            "Bad request - please check your parameters",
            id="n8n-body-http-code-400",
        ),
    ],
)
def test_google_query_error_is_a_defect_with_its_query_id(
    status: int, body: object, code: str | None, message: str
) -> None:
    """GAQL ``QueryError`` and Google defect tokens are defects with the endpoint query id."""
    error = _classify(status, body, endpoint="search Q-G6")

    assert isinstance(error, QueryDefect)
    assert error.code == code
    assert error.message == message
    assert error.http_status == status
    assert error.query_id == "Q-G6"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param({"message": "Unauthorized", "httpCode": "401"}, AccessDenied, id="401"),
        pytest.param({"error": {"code": 403, "message": "Forbidden"}}, AccessDenied, id="403"),
        pytest.param({"error": {"httpCode": 502}}, TransientError, id="error-http-code-502"),
        pytest.param({"error": {"code": 404}, "httpCode": 500}, QueryDefect, id="code-first"),
    ],
)
def test_google_body_status_maps_like_an_http_status(
    body: object, expected: type[ConnectorError]
) -> None:
    """A numeric ``error.code``/``error.httpCode``/``httpCode`` maps like the HTTP status."""
    error = _classify(200, body)

    assert type(error) is expected
    assert error.http_status == 200


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("\u00b2", id="superscript-digit"),
        pytest.param("1" * 5000, id="digit-run-beyond-int-conversion-limit"),
        pytest.param(True, id="boolean"),
        pytest.param("4o3", id="not-a-number"),
        pytest.param("999", id="out-of-range"),
    ],
)
def test_unusable_body_status_never_raises_and_is_ignored(value: object) -> None:
    """A body status that is not a 100..599 integer is ignored; classification never raises."""
    assert _classify(200, {"httpCode": value}) is None
    error = _classify(503, {"httpCode": value})
    assert isinstance(error, TransientError)


# ---------------------------------------------------------------------------
# classify: bare google.rpc Status relayed by n8n
# ---------------------------------------------------------------------------


def test_the_search_contract_documents_both_google_error_shapes() -> None:
    """The REST envelope and bare Status bodies rendered here are those ``search.json`` names."""
    contract = json.loads((CONTRACTS / "google" / "search.json").read_text())
    description = contract["response"]["body"]["description"]

    assert (
        '{"error": {code: <HTTP status>, message, status: <google.rpc code name>, details[]}}'
        in description
    )
    assert (
        "bare google.rpc Status {code: <numeric google.rpc code>, message, details[]}"
        in description
    )


def test_the_rpc_code_table_is_google_rpc_code() -> None:
    """Codes 1..16 carry code.proto's names and HTTP mappings; 0 (``OK``) is not an error."""
    assert dict(_GOOGLE_RPC_CODES) == {
        code: (name, http_status) for code, name, http_status, _ in RPC_CODE_CASES
    }


@pytest.mark.parametrize(
    ("rpc_code", "name", "http_status", "expected"),
    [pytest.param(*case, id=f"{case[0]}-{case[1].lower()}") for case in RPC_CODE_CASES],
)
def test_every_bare_rpc_code_classifies_like_its_http_status(
    rpc_code: int, name: str, http_status: int, expected: type[ConnectorError]
) -> None:
    """A bare Status code under HTTP 200 reports its name, classified like its HTTP status."""
    error = _classify(200, _bare_status(rpc_code, "Relayed Google status."))

    assert type(error) is expected
    assert type(_classify(http_status, None)) is expected
    assert error.code == name
    assert error.message == "Relayed Google status."
    assert error.http_status == 200


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(_bare_status(7, ACCESS_REFUSED), "PERMISSION_DENIED", id="7"),
        pytest.param(_bare_status(16, ACCESS_REFUSED), "UNAUTHENTICATED", id="16"),
        pytest.param([_bare_status(7, ACCESS_REFUSED)], "PERMISSION_DENIED", id="7-in-n8n-array"),
        pytest.param([_bare_status(16, ACCESS_REFUSED)], "UNAUTHENTICATED", id="16-in-n8n-array"),
        pytest.param(_bare_status("7", ACCESS_REFUSED), "PERMISSION_DENIED", id="digit-string"),
        pytest.param(_bare_status(" 16 ", ACCESS_REFUSED), "UNAUTHENTICATED", id="padded-string"),
        pytest.param(
            {"code": 7, "message": ACCESS_REFUSED}, "PERMISSION_DENIED", id="without-details"
        ),
        pytest.param(
            _bare_status(7, ACCESS_REFUSED, USER_PERMISSION_DENIED_FAILURE),
            "USER_PERMISSION_DENIED",
            id="error-code-value-wins",
        ),
    ],
)
def test_google_bare_rpc_status_denial_in_a_200_body_is_access_denied(
    body: object, code: str
) -> None:
    """A bare Status with code 7 or 16 under n8n's HTTP 200 is access denied, wrapped or not."""
    error = _classify(200, body)

    assert isinstance(error, AccessDenied)
    assert error.code == code
    assert error.message == ACCESS_REFUSED
    assert error.http_status == 200
    assert error.is_unauthorized is False
    assert error.reason == f"google search Q-G2: {code}: {ACCESS_REFUSED}"


@pytest.mark.parametrize(
    ("status", "body", "headers", "code", "retry_after"),
    [
        pytest.param(200, QUOTA_STATUS, None, "RESOURCE_EXHAUSTED", 30.0, id="8-retry-delay"),
        pytest.param(200, [QUOTA_STATUS], None, "RESOURCE_EXHAUSTED", 30.0, id="8-in-n8n-array"),
        pytest.param(
            200,
            _bare_status(8, "Resource has been exhausted (e.g. check quota)."),
            None,
            "RESOURCE_EXHAUSTED",
            None,
            id="8-without-details",
        ),
        pytest.param(
            429,
            QUOTA_STATUS,
            {"Retry-After": "7"},
            "RESOURCE_EXHAUSTED",
            7.0,
            id="8-retry-after-header-wins-over-retry-delay",
        ),
        pytest.param(
            200,
            _bare_status(14, "The service is unavailable."),
            None,
            "UNAVAILABLE",
            None,
            id="14-unavailable",
        ),
        pytest.param(
            200,
            _bare_status(4, "Deadline exceeded."),
            None,
            "DEADLINE_EXCEEDED",
            None,
            id="4-deadline-exceeded",
        ),
        pytest.param(
            200,
            _bare_status("13", "Internal error encountered."),
            None,
            "INTERNAL",
            None,
            id="13-internal-digit-string",
        ),
    ],
)
def test_google_bare_rpc_status_transient_is_transient_with_retry_delay(
    status: int,
    body: object,
    headers: Mapping[str, str] | None,
    code: str,
    retry_after: float | None,
) -> None:
    """Code 8 and the codes mapped to 5xx are transient; ``retryDelay`` gives the wait."""
    error = _classify(status, body, headers=headers)

    assert isinstance(error, TransientError)
    assert error.code == code
    assert error.http_status == status
    assert error.retry_after == retry_after


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        pytest.param(
            _bare_status(3, INVALID_ARGUMENT_MESSAGE),
            "INVALID_ARGUMENT",
            INVALID_ARGUMENT_MESSAGE,
            id="3-invalid-argument",
        ),
        pytest.param(
            [_bare_status(3, INVALID_ARGUMENT_MESSAGE)],
            "INVALID_ARGUMENT",
            INVALID_ARGUMENT_MESSAGE,
            id="3-in-n8n-array",
        ),
        pytest.param(
            _bare_status(
                3,
                INVALID_ARGUMENT_MESSAGE,
                _ads_failure(_ads_error("queryError", "UNRECOGNIZED_FIELD", "Unrecognized field.")),
            ),
            "UNRECOGNIZED_FIELD",
            INVALID_ARGUMENT_MESSAGE,
            id="3-query-error-code-value-wins",
        ),
        pytest.param(
            _bare_status(9, "Precondition check failed."),
            "FAILED_PRECONDITION",
            "Precondition check failed.",
            id="9-failed-precondition",
        ),
    ],
)
def test_google_bare_rpc_status_defect_is_a_defect_with_its_query_id(
    body: object, code: str, message: str
) -> None:
    """Code 3 and the codes mapped to other 4xx are defects with the endpoint query id."""
    error = _classify(200, body, endpoint="search Q-G6")

    assert isinstance(error, QueryDefect)
    assert error.code == code
    assert error.message == message
    assert error.http_status == 200
    assert error.query_id == "Q-G6"


@pytest.mark.parametrize(
    ("status", "body", "expected", "code"),
    [
        pytest.param(
            200,
            _bare_status(8, "Quota.", USER_PERMISSION_DENIED_FAILURE),
            AccessDenied,
            "USER_PERMISSION_DENIED",
            id="denial-token-before-transient-code",
        ),
        pytest.param(
            200,
            _bare_status(7, "RESOURCE_EXHAUSTED: quota"),
            AccessDenied,
            "PERMISSION_DENIED",
            id="denial-code-before-transient-token",
        ),
        pytest.param(
            200,
            _bare_status(3, "RESOURCE_EXHAUSTED: quota"),
            TransientError,
            "RESOURCE_EXHAUSTED",
            id="transient-token-before-defect-code",
        ),
        pytest.param(
            200,
            _bare_status(7, "UNAUTHENTICATED: missing credentials"),
            AccessDenied,
            "PERMISSION_DENIED",
            id="status-code-reported-before-a-token-of-its-kind",
        ),
        pytest.param(
            500,
            _bare_status(7, ACCESS_REFUSED),
            AccessDenied,
            "PERMISSION_DENIED",
            id="before-5xx",
        ),
        pytest.param(
            400,
            _bare_status(14, "The service is unavailable."),
            TransientError,
            "UNAVAILABLE",
            id="before-400",
        ),
        pytest.param(
            429,
            _bare_status(3, INVALID_ARGUMENT_MESSAGE),
            QueryDefect,
            "INVALID_ARGUMENT",
            id="before-429",
        ),
    ],
)
def test_google_bare_rpc_status_precedence(
    status: int, body: object, expected: type[ConnectorError], code: str
) -> None:
    """The Status code joins the body tokens: denied, transient, defect; body before status."""
    error = _classify(status, body)

    assert type(error) is expected
    assert error.code == code
    assert error.http_status == status


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_bare_status(True, ACCESS_REFUSED), id="boolean"),
        pytest.param(_bare_status(None, ACCESS_REFUSED), id="null"),
        pytest.param(_bare_status(0, "OK"), id="0-ok"),
        pytest.param(_bare_status(17, ACCESS_REFUSED), id="17-beyond-google-rpc-code"),
        pytest.param(_bare_status(-1, ACCESS_REFUSED), id="negative"),
        pytest.param(_bare_status("-7", ACCESS_REFUSED), id="signed-string"),
        pytest.param(_bare_status(7.0, ACCESS_REFUSED), id="float"),
        pytest.param(_bare_status("\u2077", ACCESS_REFUSED), id="superscript-digit"),
        pytest.param({"code": 403, "message": "Authorization data is wrong!"}, id="n8n-403"),
        pytest.param({"code": 404, "message": "The webhook is not registered."}, id="n8n-404"),
        pytest.param({**SEARCH_ENVELOPE, "code": 7}, id="success-payload-code-never-read"),
        pytest.param({"fieldMask": "campaign.id", "code": "16"}, id="zero-row-payload-code"),
        pytest.param([{**SEARCH_ENVELOPE, "code": 8}], id="success-payload-in-n8n-array"),
    ],
)
def test_other_top_level_codes_are_not_google_rpc_codes(body: object) -> None:
    """Only a code from 1 to 16 outside a success payload counts; the HTTP status rules."""
    assert _classify(200, body) is None
    error = _classify(503, body)
    assert isinstance(error, TransientError)
    assert error.code is None


# ---------------------------------------------------------------------------
# classify: OAuth credential rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "status", "body", "code"),
    [
        pytest.param(
            "linkedin",
            400,
            {
                "error": "invalid_request",
                "error_description": (
                    "The provided authorization grant or refresh token is invalid, "
                    "expired or revoked"
                ),
            },
            "invalid_request",
            id="linkedin-400-revoked-refresh-token",
        ),
        pytest.param(
            "linkedin",
            401,
            {"error": "invalid_client", "error_description": "Client authentication failed"},
            "invalid_client",
            id="linkedin-401-invalid-client",
        ),
        pytest.param(
            "linkedin",
            400,
            {"error": "UNAUTHORIZED_CLIENT", "error_description": "Not allowed"},
            "UNAUTHORIZED_CLIENT",
            id="linkedin-400-code-in-any-case",
        ),
        pytest.param(
            "hubspot",
            400,
            {"error": "invalid_grant", "error_description": "refresh token is invalid"},
            "invalid_grant",
            id="hubspot-400-invalid-grant",
        ),
        pytest.param(
            "google",
            200,
            {"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
            "invalid_grant",
            id="google-200-relayed-by-n8n",
        ),
    ],
)
def test_oauth_credential_rejection_is_access_denied(
    source: Source, status: int, body: Mapping[str, str], code: str
) -> None:
    """``invalid_grant``/``invalid_client``/``unauthorized_client`` and a revoked token."""
    error = _classify(status, body, source=source, endpoint="access_token")

    assert isinstance(error, AccessDenied)
    assert error.code == code
    assert error.message == body["error_description"]
    assert error.http_status == status
    assert error.is_unauthorized is (status == 401)


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        pytest.param(
            {
                "error": "invalid_request",
                "error_description": 'A required parameter "grant_type" is missing',
            },
            "invalid_request",
            'A required parameter "grant_type" is missing',
            id="missing-parameter",
        ),
        pytest.param(
            {"error": "invalid_request"}, "invalid_request", "Bad Request", id="no-description"
        ),
        pytest.param({"error": "   "}, None, "Bad Request", id="blank-error"),
        pytest.param({"error": {"message": "nested"}}, None, "nested", id="error-not-a-string"),
    ],
)
def test_other_oauth_400_is_a_defect(body: object, code: str | None, message: str) -> None:
    """An ``invalid_request`` that is not an expired or revoked grant is a defect."""
    error = _classify(400, body, source="linkedin", endpoint="access_token")

    assert isinstance(error, QueryDefect)
    assert error.code == code
    assert error.message == message
    assert error.query_id is None


# ---------------------------------------------------------------------------
# classify: precedence between conflicting signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected", "code"),
    [
        pytest.param(
            200,
            {"error": "invalid_grant", "message": "RESOURCE_EXHAUSTED"},
            AccessDenied,
            "invalid_grant",
            id="oauth-before-google-tokens",
        ),
        pytest.param(
            200,
            {"errors": [RESOURCE_EXHAUSTED_BODY, QUERY_ERROR_BODY, PERMISSION_DENIED_BODY]},
            AccessDenied,
            "USER_PERMISSION_DENIED",
            id="denied-before-transient-and-defect",
        ),
        pytest.param(
            200,
            {"errors": [QUERY_ERROR_BODY, RESOURCE_EXHAUSTED_BODY]},
            TransientError,
            "RESOURCE_EXHAUSTED",
            id="transient-before-defect",
        ),
        pytest.param(
            200,
            {"error": {"code": 400, "status": "RESOURCE_EXHAUSTED"}},
            TransientError,
            "RESOURCE_EXHAUSTED",
            id="body-token-before-body-status",
        ),
        pytest.param(
            500, QUERY_ERROR_BODY, QueryDefect, "UNRECOGNIZED_FIELD", id="body-token-before-5xx"
        ),
        pytest.param(
            429, PERMISSION_DENIED_BODY, AccessDenied, "USER_PERMISSION_DENIED", id="before-429"
        ),
        pytest.param(
            400, RESOURCE_EXHAUSTED_BODY, TransientError, "RESOURCE_EXHAUSTED", id="before-400"
        ),
        pytest.param(
            500,
            {"message": "Forbidden", "httpCode": "403"},
            AccessDenied,
            None,
            id="body-status-before-http-status",
        ),
    ],
)
def test_precedence_is_denied_then_transient_then_defect_and_body_before_status(
    status: int, body: object, expected: type[ConnectorError], code: str | None
) -> None:
    """OAuth, then Google tokens by kind, then the body status, then the HTTP status."""
    error = _classify(status, body)

    assert type(error) is expected
    assert error.code == code
    assert error.http_status == status


@pytest.mark.parametrize(
    ("body", "code"),
    [
        pytest.param(PERMISSION_DENIED_BODY, "USER_PERMISSION_DENIED", id="error-code-value-wins"),
        pytest.param(
            {"error": {"status": "UNAUTHENTICATED", "message": "PERMISSION_DENIED: no access"}},
            "UNAUTHENTICATED",
            id="first-token-in-document-order",
        ),
    ],
)
def test_reported_code_prefers_a_specific_error_code_then_document_order(
    body: object, code: str
) -> None:
    """A GoogleAdsFailure ``errorCode`` value beats a bare token; else the first one found."""
    error = _classify(200, body)

    assert isinstance(error, AccessDenied)
    assert error.code == code


def test_non_google_bodies_are_classified_by_status_only() -> None:
    """Google tokens in a LinkedIn or HubSpot body do not change the status' kind."""
    linkedin = _classify(500, {"message": "QueryError"}, source="linkedin", endpoint="posts")
    hubspot = _classify(400, {"message": TOKEN}, source="hubspot", endpoint="properties")

    assert isinstance(linkedin, TransientError)
    assert isinstance(hubspot, QueryDefect)


# ---------------------------------------------------------------------------
# classify: scan limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("levels", "found"),
    [
        pytest.param(_MAX_SCAN_DEPTH, True, id="at-depth-limit"),
        pytest.param(_MAX_SCAN_DEPTH + 1, False, id="beyond-depth-limit"),
    ],
)
def test_body_walk_stops_at_the_depth_limit(levels: int, found: bool) -> None:
    """A token nested deeper than the scan depth is not read."""
    error = _classify(200, _nested(TOKEN, levels))

    assert isinstance(error, AccessDenied) is found


@pytest.mark.parametrize(
    ("fillers", "found"),
    [
        # The walk visits the body, its list, the fillers, then the token.
        pytest.param(_MAX_SCAN_NODES - 3, True, id="token-is-the-last-node-read"),
        pytest.param(_MAX_SCAN_NODES - 2, False, id="token-beyond-node-limit"),
    ],
)
def test_body_walk_stops_at_the_node_limit(fillers: int, found: bool) -> None:
    """A token after the first ``_MAX_SCAN_NODES`` nodes is not read."""
    body = {"errors": ["filler"] * fillers + [TOKEN]}

    assert isinstance(_classify(200, body), AccessDenied) is found


@pytest.mark.parametrize(
    ("padding", "found"),
    [
        pytest.param(_MAX_SCAN_TEXT - len(TOKEN), True, id="token-ends-at-text-limit"),
        pytest.param(_MAX_SCAN_TEXT - len(TOKEN) + 1, False, id="token-crosses-text-limit"),
    ],
)
def test_text_scan_stops_at_the_text_limit(padding: int, found: bool) -> None:
    """Tokens inside a string are searched only within its first ``_MAX_SCAN_TEXT`` chars."""
    text = " " * padding + TOKEN + " in a long n8n description"
    error = _classify(200, {"description": text})

    assert isinstance(error, AccessDenied) is found


def test_walk_yields_document_order_and_stops_at_the_node_limit() -> None:
    """``_walk`` is depth-first in document order, keyed by str keys, and bounded."""
    nodes = list(_walk([{"a": [1, (2, 3)], 4: "x"}, "y"]))
    assert nodes == [
        (None, {"a": [1, (2, 3)], 4: "x"}),
        ("a", [1, (2, 3)]),
        (None, 1),
        (None, (2, 3)),
        (None, 2),
        (None, 3),
        (None, "x"),
        (None, "y"),
    ]

    assert len(list(_walk([list(range(2 * _MAX_SCAN_NODES))]))) == _MAX_SCAN_NODES


# ---------------------------------------------------------------------------
# Body-inspection helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "roots"),
    [
        pytest.param(None, [], id="none"),
        pytest.param(7, [], id="number"),
        pytest.param("text", ["text"], id="text"),
        pytest.param({"message": "m"}, [{"message": "m"}], id="error-mapping"),
        pytest.param(["only"], ["only"], id="one-element-array-unwrapped"),
        pytest.param(
            {"results": [{"campaign": {}}], "fieldMask": "f", "error": "e", "status": "s"},
            ["e", "s"],
            id="success-payload-error-keys-only",
        ),
        pytest.param(SEARCH_ENVELOPE, [], id="success-payload-without-error-keys"),
        pytest.param([["a", "b"], "c"], ["a", "b", "c"], id="nested-list-one-level"),
        pytest.param([[["deep"], "b"], "c"], ["b", "c"], id="lists-two-levels-deep-dropped"),
    ],
)
def test_google_scan_roots(body: object, roots: list[object]) -> None:
    """Only the error-bearing parts of a Google body are scanned."""
    assert _google_scan_roots(body) == roots


def test_google_scan_roots_follow_the_error_key_order() -> None:
    """A success payload's error keys come back in the declared key order."""
    body: dict[str, object] = {key: key for key in reversed(_GOOGLE_ERROR_KEYS)} | {"results": []}

    assert _google_scan_roots(body) == list(_GOOGLE_ERROR_KEYS)


def test_select_signal() -> None:
    """Kinds are chosen by precedence and a specific code is preferred within the kind."""
    defect = _Signal("defect", "QueryError", specific=False)
    transient = _Signal("transient", "RESOURCE_EXHAUSTED", specific=False)
    bare = _Signal("access_denied", "PERMISSION_DENIED", specific=False)
    specific = _Signal("access_denied", "USER_PERMISSION_DENIED", specific=True)

    assert _select_signal([]) is None
    assert _select_signal([defect, transient]) == ("transient", "RESOURCE_EXHAUSTED")
    assert _select_signal([defect, bare, specific]) == ("access_denied", "USER_PERMISSION_DENIED")
    assert _select_signal([defect, bare]) == ("access_denied", "PERMISSION_DENIED")


@pytest.mark.parametrize(
    ("value", "status"),
    [
        pytest.param(403, 403, id="int"),
        pytest.param(" 429 ", 429, id="digit-string"),
        pytest.param(100, 100, id="lowest"),
        pytest.param(599, 599, id="highest"),
        pytest.param(99, None, id="below-range"),
        pytest.param("600", None, id="above-range"),
        pytest.param(True, None, id="bool"),
        pytest.param(403.0, None, id="float"),
        pytest.param("4o3", None, id="not-digits"),
        pytest.param("\u00b2", None, id="superscript"),
        pytest.param("9" * 5000, None, id="over-long"),
        pytest.param(None, None, id="none"),
    ],
)
def test_as_status(value: object, status: int | None) -> None:
    """A body status is an int or digit string in 100..599; anything else is ``None``."""
    assert _as_status(value) == status


@pytest.mark.parametrize(
    ("item", "status"),
    [
        pytest.param("text", None, id="not-a-mapping"),
        pytest.param({"error": {"code": 404, "httpCode": 500}, "httpCode": 429}, 404, id="code"),
        pytest.param({"error": {"code": 7, "httpCode": "503"}}, 503, id="error-http-code"),
        pytest.param({"error": "boom", "httpCode": "429"}, 429, id="top-level-http-code"),
        pytest.param({"error": {"code": "x"}}, None, id="none-usable"),
    ],
)
def test_google_body_status(item: object, status: int | None) -> None:
    """``error.code``, then ``error.httpCode``, then n8n's top-level ``httpCode``."""
    assert _google_body_status(item) == status


@pytest.mark.parametrize(
    ("value", "number"),
    [
        pytest.param(7, 7, id="int"),
        pytest.param(-1, -1, id="negative-int"),
        pytest.param(" 16 ", 16, id="digit-string"),
        pytest.param("-7", None, id="signed-string"),
        pytest.param(True, None, id="bool"),
        pytest.param(7.0, None, id="float"),
        pytest.param("\u2077", None, id="superscript"),
        pytest.param("9" * 5000, None, id="over-long"),
        pytest.param(None, None, id="none"),
    ],
)
def test_as_int(value: object, number: int | None) -> None:
    """An int that is not a bool, or an unsigned digit string; anything else is ``None``."""
    assert _as_int(value) == number


@pytest.mark.parametrize(
    ("item", "signal"),
    [
        pytest.param(
            {"code": 7}, _Signal("access_denied", "PERMISSION_DENIED", specific=False), id="7"
        ),
        pytest.param(
            {"code": 16}, _Signal("access_denied", "UNAUTHENTICATED", specific=False), id="16"
        ),
        pytest.param(
            {"code": "8"}, _Signal("transient", "RESOURCE_EXHAUSTED", specific=False), id="8"
        ),
        pytest.param({"code": 1}, _Signal("defect", "CANCELLED", specific=False), id="1"),
        pytest.param({"code": 0}, None, id="0-ok"),
        pytest.param({"code": 17}, None, id="17"),
        pytest.param({}, None, id="no-code"),
        pytest.param({"results": [], "code": 7}, None, id="results"),
        pytest.param({"fieldMask": "campaign.id", "code": 7}, None, id="field-mask"),
        pytest.param({"error": {"code": 7}}, None, id="rest-envelope-code"),
        pytest.param([{"code": 7}], None, id="not-unwrapped"),
        pytest.param("7", None, id="not-a-mapping"),
    ],
)
def test_google_rpc_status_signal(item: object, signal: _Signal | None) -> None:
    """Only the top-level google.rpc code 1..16 of a non-success mapping is a signal."""
    assert _google_rpc_status_signal(item) == signal


@pytest.mark.parametrize(
    ("item", "source", "code"),
    [
        pytest.param(None, "google", None, id="not-a-mapping"),
        pytest.param({"error": "invalid_grant"}, "linkedin", "invalid_grant", id="oauth"),
        pytest.param({"error": {"status": "NOT_FOUND"}}, "google", "NOT_FOUND", id="google"),
        pytest.param({"error": {"code": 404}}, "google", None, id="google-without-status"),
        pytest.param({"code": "X", "serviceErrorCode": 1}, "linkedin", "X", id="linkedin-code"),
        pytest.param({"serviceErrorCode": 100}, "linkedin", "100", id="linkedin-service-int"),
        pytest.param({"serviceErrorCode": True}, "linkedin", None, id="linkedin-service-bool"),
        pytest.param({"category": "C", "errorType": "E"}, "hubspot", "C", id="hubspot-category"),
        pytest.param({"errorType": "E", "subCategory": "S"}, "hubspot", "E", id="hubspot-type"),
        pytest.param({"category": " ", "subCategory": "S"}, "hubspot", "S", id="hubspot-sub"),
    ],
)
def test_platform_code(item: object, source: Source, code: str | None) -> None:
    """Each platform's error code field is read in its documented order."""
    assert _platform_code(item, source) == code


@pytest.mark.parametrize(
    ("item", "message"),
    [
        pytest.param("plain text", "plain text", id="text"),
        pytest.param("  ", None, id="blank-text"),
        pytest.param(42, None, id="not-a-mapping"),
        pytest.param(
            {"error": {"message": "a"}, "error_description": "b", "message": "c"},
            "a",
            id="error-message-first",
        ),
        pytest.param({"error_description": "b", "message": "c"}, "b", id="oauth-description"),
        pytest.param({"message": " ", "description": "d"}, "d", id="n8n-description"),
        pytest.param(
            {"errors": ["x", {"message": ""}, {"message": "e"}]}, "e", id="errors-list-message"
        ),
        pytest.param({"errors": "not-a-list"}, None, id="none"),
    ],
)
def test_platform_message(item: object, message: str | None) -> None:
    """The first non-blank message, in the documented order."""
    assert _platform_message(item) == message


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        pytest.param(401, "access_denied", id="401"),
        pytest.param(403, "access_denied", id="403"),
        pytest.param(429, "transient", id="429"),
        pytest.param(500, "transient", id="500"),
        pytest.param(599, "transient", id="599"),
        pytest.param(400, "defect", id="400"),
        pytest.param(404, "defect", id="404"),
        pytest.param(499, "defect", id="499"),
        pytest.param(200, None, id="200"),
        pytest.param(207, None, id="207"),
        pytest.param(302, None, id="302"),
        pytest.param(600, None, id="600"),
    ],
)
def test_kind_for_status(status: int, kind: str | None) -> None:
    """401/403 denied, 429/5xx transient, other 4xx defect, anything else no error."""
    assert _kind_for_status(status) == kind


@pytest.mark.parametrize(
    ("endpoint", "query_id"),
    [
        pytest.param("search Q-G2", "Q-G2", id="with-query-id"),
        pytest.param("search", None, id="without-query-id"),
        pytest.param("", None, id="empty"),
    ],
)
def test_query_id(endpoint: str, query_id: str | None) -> None:
    """The query id is the endpoint's suffix after the surface name."""
    assert _query_id(endpoint) == query_id


# ---------------------------------------------------------------------------
# Sanitisation of classified messages
# ---------------------------------------------------------------------------


def test_classified_message_never_carries_the_webhook_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An n8n message echoing the configured webhook URL is redacted in the error."""
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", WEBHOOK_URL)
    get_settings.cache_clear()
    body = {"message": f"The requested webhook {WEBHOOK_URL} is not registered."}

    error = _classify(404, body)

    assert isinstance(error, QueryDefect)
    assert WEBHOOK_URL not in error.reason
    assert "/webhook/google-ads" not in error.reason
    assert "[REDACTED]" in error.message
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# classify_exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected", "code", "message"),
    [
        pytest.param(
            httpx.ReadTimeout("timed out"),
            TransientError,
            "TIMEOUT",
            "ReadTimeout: timed out",
            id="read-timeout",
        ),
        pytest.param(
            httpx.ConnectTimeout("timed out"),
            TransientError,
            "TIMEOUT",
            "ConnectTimeout: timed out",
            id="connect-timeout",
        ),
        pytest.param(
            httpx.WriteTimeout("timed out"),
            TransientError,
            "TIMEOUT",
            "WriteTimeout: timed out",
            id="write-timeout",
        ),
        pytest.param(
            httpx.PoolTimeout("no connection available"),
            TransientError,
            "TIMEOUT",
            "PoolTimeout: no connection available",
            id="pool-timeout",
        ),
        pytest.param(TimeoutError(), TransientError, "TIMEOUT", "TimeoutError", id="builtin"),
        pytest.param(
            httpx.ConnectError("connection refused"),
            TransientError,
            "NETWORK",
            "ConnectError: connection refused",
            id="connect-error",
        ),
        pytest.param(
            httpx.ReadError("connection reset"),
            TransientError,
            "NETWORK",
            "ReadError: connection reset",
            id="read-error",
        ),
        pytest.param(
            httpx.WriteError("broken pipe"),
            TransientError,
            "NETWORK",
            "WriteError: broken pipe",
            id="write-error",
        ),
        pytest.param(
            httpx.RemoteProtocolError("peer closed connection"),
            TransientError,
            "NETWORK",
            "RemoteProtocolError: peer closed connection",
            id="remote-protocol-error",
        ),
        pytest.param(
            httpx.ProxyError("proxy refused"),
            TransientError,
            "NETWORK",
            "ProxyError: proxy refused",
            id="proxy-error",
        ),
        pytest.param(
            httpx.DecodingError("malformed gzip"),
            TransientError,
            "NETWORK",
            "DecodingError: malformed gzip",
            id="decoding-error",
        ),
        pytest.param(
            httpx.UnsupportedProtocol("missing an 'http://' or 'https://' protocol"),
            QueryDefect,
            "CLIENT",
            "UnsupportedProtocol: missing an 'http://' or 'https://' protocol",
            id="unsupported-protocol",
        ),
        pytest.param(
            httpx.LocalProtocolError("illegal header value"),
            QueryDefect,
            "CLIENT",
            "LocalProtocolError: illegal header value",
            id="local-protocol-error",
        ),
        pytest.param(
            httpx.InvalidURL("invalid port"),
            QueryDefect,
            "CLIENT",
            "InvalidURL: invalid port",
            id="invalid-url",
        ),
        pytest.param(
            httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
            QueryDefect,
            "CLIENT",
            "TooManyRedirects: Exceeded maximum allowed redirects.",
            id="too-many-redirects",
        ),
        pytest.param(
            ValueError("unexpected payload"),
            QueryDefect,
            "CLIENT",
            "ValueError: unexpected payload",
            id="non-httpx-exception",
        ),
        pytest.param(RuntimeError("  "), QueryDefect, "CLIENT", "RuntimeError", id="blank-text"),
        pytest.param(
            httpx.ConnectError("cannot reach lead@example.com"),
            TransientError,
            "NETWORK",
            "ConnectError: cannot reach [REDACTED_EMAIL]",
            id="text-is-sanitised",
        ),
    ],
)
def test_classify_exception_by_exception_type(
    exc: Exception, expected: type[ConnectorError], code: str, message: str
) -> None:
    """Timeouts and network failures are transient; client-side failures are defects."""
    error = classify_exception(exc, source="google", endpoint="search Q-G5")

    assert type(error) is expected
    assert error.code == code
    assert error.message == message
    assert error.http_status is None
    assert error.source == "google"
    assert error.endpoint == "search Q-G5"
    if isinstance(error, QueryDefect):
        assert error.query_id == "Q-G5"
    else:
        assert isinstance(error, TransientError)
        assert error.retry_after is None


def test_classify_exception_handles_a_stream_misuse_as_a_client_defect() -> None:
    """An httpx stream misuse is neither a transport nor a status error: a client defect."""
    error = classify_exception(httpx.StreamConsumed(), source="hubspot", endpoint="crm_search")

    assert isinstance(error, QueryDefect)
    assert error.code == "CLIENT"
    assert error.message.startswith("StreamConsumed: ")
    assert error.query_id is None


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(
            AccessDenied("denied", source="linkedin", endpoint="ad_account"), id="access-denied"
        ),
        pytest.param(CallBudgetExhausted(), id="call-budget-exhausted"),
        pytest.param(
            QueryDefect("bad", source="google", endpoint="search Q-G3", query_id="Q-G3"),
            id="query-defect",
        ),
    ],
)
def test_classify_exception_returns_a_connector_error_unchanged(
    original: ConnectorError,
) -> None:
    """A connector error raised inside the call passes through as the same object."""
    assert classify_exception(original, source="hubspot", endpoint="properties") is original


@pytest.mark.parametrize(
    ("status", "headers", "expected", "code", "message"),
    [
        pytest.param(401, None, AccessDenied, None, "Unauthorized", id="401"),
        pytest.param(403, None, AccessDenied, None, "Forbidden", id="403"),
        pytest.param(
            429, {"Retry-After": "3"}, TransientError, None, "Too Many Requests", id="429"
        ),
        pytest.param(503, None, TransientError, None, "Service Unavailable", id="503"),
        pytest.param(404, None, QueryDefect, None, "Not Found", id="404"),
        pytest.param(302, None, QueryDefect, "CLIENT", "HTTPStatusError: HTTP 302 Found", id="302"),
    ],
)
def test_classify_exception_classifies_an_http_status_error_from_its_status(
    status: int,
    headers: Mapping[str, str] | None,
    expected: type[ConnectorError],
    code: str | None,
    message: str,
) -> None:
    """``HTTPStatusError`` maps as its status with no body; its URL never enters the text."""
    response = _response(status, source="hubspot", headers=headers)
    exc = httpx.HTTPStatusError(
        f"Error for url '{response.request.url}'", request=response.request, response=response
    )

    error = classify_exception(exc, source="hubspot", endpoint="crm_search deals")

    assert type(error) is expected
    assert error.code == code
    assert error.message == message
    assert error.http_status == status
    assert error.is_unauthorized is (status == 401)
    assert "api.hubapi.com" not in error.reason
    if isinstance(error, TransientError):
        assert error.retry_after == (3.0 if status == 429 else None)
    if isinstance(error, QueryDefect):
        assert error.query_id == "deals"
