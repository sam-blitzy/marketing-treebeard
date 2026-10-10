"""Behavioural tests for the error types and helpers of ``ads_engine.connectors.errors``.

Covers the error kinds and their precedence; the ``ConnectorError`` family (sanitised
messages, reasons, length limits, endpoint and code cleaning, ``retry_after`` and
``query_id``, logging fields, pickling and copying); and the helpers ``sanitize_message``,
``unwrap_single_item`` and ``parse_retry_after``. Response classification is tested in
``test_connector_errors_classify.py``.

Every test runs against an environment stripped of each variable ``Settings`` reads, so
host-exported credentials never reach a test; a test that needs configured secrets exports
obvious test literals. No test performs network or database I/O.
"""

import contextvars
import copy
import math
import os
import pickle
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any, get_args
from urllib.parse import unquote

import pytest
from pydantic import AliasChoices

from ads_engine.connectors import errors
from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    ConnectorError,
    ErrorKind,
    QueryDefect,
    TransientError,
    holding_secrets,
    parse_retry_after,
    sanitize_message,
    unwrap_single_item,
)
from ads_engine.settings import ConfigurationError, Settings, get_settings

WEBHOOK_URL = "https://n8n.example.test/webhook/test-google-ads"
WEBHOOK_PATH = "/webhook/test-google-ads"
N8N_SECRET = "test-n8n-secret"
# A 7-character n8n secret, as a webhook echoed it back in a 403 body.
SHORT_N8N_SECRET = "Zq7#k2p"
LINKEDIN_TOKEN = "test-linkedin-access-token"
HUBSPOT_TOKEN = "test-hubspot-token"
LINKEDIN_CLIENT_ID = "test-linkedin-client-id"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ads:ads@db:5432/ads_engine"
EMAIL = "alice@example.com"
ELLIPSIS = errors._ELLIPSIS
# Tokens that exist only in memory, as an exchanged LinkedIn access token or a rotated
# refresh token does: no setting holds them. Assembled at run time, so the secret scan
# never reads these test values as credentials. The long one is 400 characters, as long as
# a LinkedIn access token, and every 8-character run of it occurs in it only once.
HELD_TOKEN = "-".join(("test", "held", "access", "token", "0123456789"))
OTHER_HELD_TOKEN = "-".join(("test", "held", "rotated", "refresh", "token"))
LONG_HELD_TOKEN = "".join(f"{index:04d}tokn" for index in range(50))

# The reference instant of the HTTP-date cases: the docstring example's 12:00 UTC.
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

ERROR_TYPES = [
    pytest.param(AccessDenied, id="access-denied"),
    pytest.param(TransientError, id="transient"),
    pytest.param(CallBudgetExhausted, id="call-budget-exhausted"),
    pytest.param(QueryDefect, id="query-defect"),
]


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


SETTINGS_ENV_NAMES = _settings_env_names()


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads, in any letter case, and reset its cache.

    Building an error loads the settings to collect the secrets to redact, so no test may
    see the host's real credentials.
    """
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> None:
    """Export ``env`` for the current test only and drop the cached settings."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Kinds and the class hierarchy
# ---------------------------------------------------------------------------


def test_error_kind_names_three_consequences_in_precedence_order() -> None:
    """``ErrorKind`` has three values, ranked access denied > transient > defect."""
    assert get_args(ErrorKind) == ("access_denied", "transient", "defect")
    assert errors._KIND_PRECEDENCE == ("access_denied", "transient", "defect")


@pytest.mark.parametrize(
    ("error_type", "kind", "retryable"),
    [
        pytest.param(AccessDenied, "access_denied", False, id="access-denied"),
        pytest.param(TransientError, "transient", True, id="transient"),
        pytest.param(CallBudgetExhausted, "transient", False, id="call-budget-exhausted"),
        pytest.param(QueryDefect, "defect", False, id="query-defect"),
    ],
)
def test_each_error_type_declares_its_kind_and_retry_policy(
    error_type: type[ConnectorError], kind: ErrorKind, retryable: bool
) -> None:
    """Each type is a ``ConnectorError``; only a plain transient error is retried."""
    assert issubclass(error_type, ConnectorError)
    assert error_type.kind == kind
    assert error_type.retryable is retryable


def test_call_budget_exhausted_is_caught_as_a_transient_error() -> None:
    """The call budget is a transient condition, so a handler of transients catches it."""
    with pytest.raises(TransientError) as caught:
        raise CallBudgetExhausted
    assert isinstance(caught.value, CallBudgetExhausted)
    assert isinstance(caught.value, ConnectorError)


def test_connector_error_itself_cannot_be_instantiated() -> None:
    """The base class has no kind; a subclass must be raised instead."""
    with pytest.raises(TypeError, match="abstract"):
        ConnectorError("boom", source="google", endpoint="search")


# ---------------------------------------------------------------------------
# ConnectorError construction, reason, str and repr
# ---------------------------------------------------------------------------


def test_constructor_keeps_the_call_details() -> None:
    """Source, endpoint, code and HTTP status are kept as given when already clean."""
    error = AccessDenied(
        "Not enough permissions",
        source="linkedin",
        endpoint="lead_form_responses",
        code="ACCESS_DENIED",
        http_status=403,
    )
    assert error.message == "Not enough permissions"
    assert error.source == "linkedin"
    assert error.endpoint == "lead_form_responses"
    assert error.code == "ACCESS_DENIED"
    assert error.http_status == 403


@pytest.mark.parametrize(
    ("code", "http_status", "expected"),
    [
        pytest.param(
            "ACCESS_DENIED",
            403,
            "linkedin lead_form_responses: ACCESS_DENIED: Not enough permissions",
            id="code-preferred-over-status",
        ),
        pytest.param(
            None,
            403,
            "linkedin lead_form_responses: HTTP 403: Not enough permissions",
            id="status-without-code",
        ),
        pytest.param(
            " \n ",
            403,
            "linkedin lead_form_responses: HTTP 403: Not enough permissions",
            id="blank-code-falls-back-to-status",
        ),
        pytest.param(
            None,
            None,
            "linkedin lead_form_responses: Not enough permissions",
            id="neither-code-nor-status",
        ),
    ],
)
def test_reason_names_source_endpoint_label_and_message(
    code: str | None, http_status: int | None, expected: str
) -> None:
    """The reason reads ``{source} {endpoint}: {code or 'HTTP ' + status}: {message}``."""
    error = AccessDenied(
        "Not enough permissions",
        source="linkedin",
        endpoint="lead_form_responses",
        code=code,
        http_status=http_status,
    )
    assert error.reason == expected


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        pytest.param("search Q-G2", "search Q-G2", id="clean"),
        pytest.param("  search\n\tQ-G2  ", "search Q-G2", id="whitespace-collapsed"),
        pytest.param("", errors._UNKNOWN_ENDPOINT, id="empty-falls-back"),
        pytest.param(" \n\t ", errors._UNKNOWN_ENDPOINT, id="blank-falls-back"),
    ],
)
def test_endpoint_is_one_line_and_never_blank(endpoint: str, expected: str) -> None:
    """The endpoint collapses to one line, and a blank one is replaced by a fallback name."""
    error = TransientError("Bad gateway", source="google", endpoint=endpoint, http_status=502)
    assert error.endpoint == expected
    assert error.reason == f"google {expected}: HTTP 502: Bad gateway"


def test_str_is_the_reason_and_repr_names_the_class() -> None:
    """``str()`` returns the reason; ``repr()`` wraps it in the class name."""
    error = QueryDefect(
        "Unrecognized field",
        source="google",
        endpoint="search Q-G5",
        code="QueryError",
        http_status=400,
        query_id="Q-G5",
    )
    assert str(error) == "google search Q-G5: QueryError: Unrecognized field"
    assert repr(error) == "QueryDefect('google search Q-G5: QueryError: Unrecognized field')"


@pytest.mark.parametrize(
    ("http_status", "expected"),
    [
        pytest.param(401, True, id="401"),
        pytest.param(403, False, id="403"),
        pytest.param(200, False, id="200"),
        pytest.param(None, False, id="no-response"),
    ],
)
def test_is_unauthorized_only_for_http_401(http_status: int | None, expected: bool) -> None:
    """Only a 401 marks the error unauthorized, which triggers LinkedIn's token refresh."""
    error = AccessDenied(
        "Unauthorized", source="linkedin", endpoint="ad_campaigns", http_status=http_status
    )
    assert error.is_unauthorized is expected


@pytest.mark.parametrize("error_type", ERROR_TYPES)
def test_message_never_carries_an_email_secret_or_webhook_url(
    monkeypatch: pytest.MonkeyPatch, error_type: type[ConnectorError]
) -> None:
    """Every error type sanitises its message on construction, so no text it renders leaks."""
    _configure(
        monkeypatch,
        {"N8N_GOOGLE_ADS_WEBHOOK_URL": WEBHOOK_URL, "LINKEDIN_ACCESS_TOKEN": LINKEDIN_TOKEN},
    )
    raw = f"POST {WEBHOOK_URL} for {EMAIL}\n  with Bearer {LINKEDIN_TOKEN}"
    error = error_type(raw, source="google", endpoint="search")
    assert error.message == "POST [REDACTED] for [REDACTED_EMAIL] with Bearer [REDACTED]"
    for text in (error.message, error.reason, str(error), repr(error), str(error.log_fields)):
        for leaked in (EMAIL, WEBHOOK_URL, WEBHOOK_PATH, LINKEDIN_TOKEN):
            assert leaked not in text


# ---------------------------------------------------------------------------
# Length limits
# ---------------------------------------------------------------------------


def test_message_is_cut_to_300_characters() -> None:
    """A long platform message keeps its first 299 characters plus an ellipsis."""
    error = TransientError("x" * 1000, source="google", endpoint="search")
    assert errors._MESSAGE_MAX == 300
    assert error.message == "x" * 299 + ELLIPSIS


def test_message_of_exactly_300_characters_is_kept_whole() -> None:
    """A message at the limit is not cut."""
    error = TransientError("x" * 300, source="google", endpoint="search")
    assert error.message == "x" * 300


def test_reason_is_cut_to_500_characters() -> None:
    """The reason never exceeds 500 characters, even with a long endpoint and message."""
    error = TransientError("y" * 300, source="google", endpoint="e" * 400, code="TIMEOUT")
    assert errors._REASON_MAX == 500
    assert len(error.reason) == 500
    assert error.reason.startswith("google eee")
    assert error.reason.endswith(ELLIPSIS)
    assert str(error) == error.reason


def test_long_code_is_cut_to_the_code_limit() -> None:
    """A platform code is kept to one short line, marked with an ellipsis when cut."""
    error = QueryDefect("bad", source="hubspot", endpoint="search", code="E" * 150)
    assert error.code == "E" * (errors._CODE_MAX - 1) + ELLIPSIS


# ---------------------------------------------------------------------------
# Logging fields
# ---------------------------------------------------------------------------


def test_log_fields_describe_the_error() -> None:
    """The base fields name the kind, source, endpoint, code, status and reason."""
    error = AccessDenied(
        f"No contact {EMAIL}",
        source="hubspot",
        endpoint="contacts_batch_read",
        code="MISSING_SCOPES",
        http_status=403,
    )
    assert error.log_fields == {
        "error_kind": "access_denied",
        "source": "hubspot",
        "endpoint": "contacts_batch_read",
        "error_code": "MISSING_SCOPES",
        "http_status": 403,
        "reason": "hubspot contacts_batch_read: MISSING_SCOPES: No contact [REDACTED_EMAIL]",
    }


def test_log_fields_of_a_transient_error_add_retry_after() -> None:
    """A transient error also logs the delay the platform asked for."""
    error = TransientError(
        "Too many requests",
        source="hubspot",
        endpoint="search",
        code="RATE_LIMIT",
        http_status=429,
        retry_after=10,
    )
    assert error.log_fields == {
        "error_kind": "transient",
        "source": "hubspot",
        "endpoint": "search",
        "error_code": "RATE_LIMIT",
        "http_status": 429,
        "reason": "hubspot search: RATE_LIMIT: Too many requests",
        "retry_after": 10.0,
    }


def test_log_fields_of_a_query_defect_add_query_id() -> None:
    """A defect also logs its query id, so the failed job names the query."""
    error = QueryDefect(
        "Unrecognized field",
        source="google",
        endpoint="search Q-G5",
        code="QueryError",
        http_status=400,
        query_id="Q-G5",
    )
    assert error.log_fields == {
        "error_kind": "defect",
        "source": "google",
        "endpoint": "search Q-G5",
        "error_code": "QueryError",
        "http_status": 400,
        "reason": "google search Q-G5: QueryError: Unrecognized field",
        "query_id": "Q-G5",
    }


# ---------------------------------------------------------------------------
# TransientError.retry_after and _clean_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param(0, 0.0, id="zero"),
        pytest.param(30, 30.0, id="integer"),
        pytest.param(1.5, 1.5, id="fraction"),
        pytest.param(-1.0, None, id="negative"),
        pytest.param(-0.001, None, id="slightly-negative"),
        pytest.param(math.nan, None, id="nan"),
        pytest.param(math.inf, None, id="inf"),
        pytest.param(-math.inf, None, id="negative-inf"),
    ],
)
def test_retry_after_keeps_only_finite_non_negative_seconds(
    retry_after: float | None, expected: float | None
) -> None:
    """Negative and non-finite delays are dropped; any other delay becomes float seconds."""
    error = TransientError(
        "Too many requests",
        source="linkedin",
        endpoint="ad_analytics",
        http_status=429,
        retry_after=retry_after,
    )
    assert error.retry_after == expected
    assert type(error.retry_after) is type(expected)
    assert errors._clean_seconds(retry_after) == expected


# ---------------------------------------------------------------------------
# CallBudgetExhausted
# ---------------------------------------------------------------------------


def test_call_budget_exhausted_defaults_to_the_google_budget() -> None:
    """With no arguments it names Google's search endpoint and the product's own budget."""
    error = CallBudgetExhausted()
    assert error.source == "google"
    assert error.endpoint == "search"
    assert error.code == "CALL_BUDGET_EXHAUSTED"
    assert error.http_status is None
    assert error.retry_after is None
    assert error.message == errors._BUDGET_MESSAGE
    assert "GOOGLE_DAILY_CALL_BUDGET" in error.message
    assert error.reason == f"google search: CALL_BUDGET_EXHAUSTED: {errors._BUDGET_MESSAGE}"
    assert error.kind == "transient"
    assert error.retryable is False


def test_call_budget_exhausted_accepts_a_query_endpoint_and_delay() -> None:
    """The endpoint can name the deferred query, and a delay is cleaned like any transient."""
    error = CallBudgetExhausted(endpoint="search Q-G5", retry_after=3600)
    assert error.endpoint == "search Q-G5"
    assert error.retry_after == 3600.0
    assert error.reason.startswith("google search Q-G5: CALL_BUDGET_EXHAUSTED: ")


# ---------------------------------------------------------------------------
# QueryDefect.query_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query_id", "expected"),
    [
        pytest.param("Q-G2", "Q-G2", id="clean"),
        pytest.param("  Q-G2\n", "Q-G2", id="trimmed"),
        pytest.param("Q-G2  page\t3", "Q-G2 page 3", id="whitespace-collapsed"),
        pytest.param("", None, id="empty"),
        pytest.param(" \n ", None, id="blank"),
        pytest.param(None, None, id="absent"),
    ],
)
def test_query_id_is_one_line_or_none(query_id: str | None, expected: str | None) -> None:
    """The query id collapses to one line; a blank id is unknown."""
    error = QueryDefect("Unrecognized field", source="google", endpoint="search", query_id=query_id)
    assert error.query_id == expected


# ---------------------------------------------------------------------------
# Pickling and copying
# ---------------------------------------------------------------------------

ROUND_TRIP_CASES = [
    pytest.param(
        AccessDenied,
        {
            "message": "Not enough permissions",
            "source": "linkedin",
            "endpoint": "lead_form_responses",
            "code": "ACCESS_DENIED",
            "http_status": 403,
        },
        id="access-denied",
    ),
    pytest.param(
        TransientError,
        {
            "message": f"Too many requests for {EMAIL} " + "z" * 400,
            "source": "hubspot",
            "endpoint": "contacts_batch_read",
            "code": "RATE_LIMIT",
            "http_status": 429,
            "retry_after": 12.5,
        },
        id="transient-with-sanitised-and-cut-message",
    ),
    pytest.param(
        CallBudgetExhausted,
        {"endpoint": "search Q-G5", "retry_after": 60.0},
        id="call-budget-exhausted",
    ),
    pytest.param(
        QueryDefect,
        {
            "message": "Unrecognized field",
            "source": "google",
            "endpoint": "search Q-G2",
            "code": "QueryError",
            "http_status": 400,
            "query_id": "Q-G2",
        },
        id="query-defect",
    ),
]


@pytest.mark.parametrize(("error_type", "kwargs"), ROUND_TRIP_CASES)
def test_copies_rebuild_an_equal_error(
    error_type: type[ConnectorError], kwargs: dict[str, Any]
) -> None:
    """``copy.copy`` and ``copy.deepcopy`` rebuild the error despite keyword-only arguments."""
    original = error_type(**kwargs)
    for clone in (copy.copy(original), copy.deepcopy(original)):
        assert clone is not original
        assert type(clone) is error_type
        assert vars(clone) == vars(original)
        assert clone.args == original.args
        assert clone.reason == original.reason


@pytest.mark.parametrize(("error_type", "kwargs"), ROUND_TRIP_CASES)
def test_pickling_reduces_to_the_restore_function(
    error_type: type[ConnectorError], kwargs: dict[str, Any]
) -> None:
    """Pickle records ``_restore_error`` with the arguments that rebuild an equal error.

    ``pickle.dumps`` resolves the restore function and the class by name; calling the
    reduction is exactly what unpickling does.
    """
    original = error_type(**kwargs)
    payload = pickle.dumps(original)
    assert b"_restore_error" in payload
    restore, arguments = original.__reduce__()
    assert restore is errors._restore_error
    clone = restore(*arguments)
    assert type(clone) is error_type
    assert vars(clone) == vars(original)
    assert clone.reason == original.reason


def test_restore_error_builds_the_given_class_from_keyword_arguments() -> None:
    """``_restore_error`` calls the class with the recorded constructor arguments."""
    error = errors._restore_error(
        QueryDefect,
        {"message": "bad query", "source": "google", "endpoint": "search", "query_id": "Q-G1"},
    )
    assert isinstance(error, QueryDefect)
    assert error.query_id == "Q-G1"
    assert error.reason == "google search: bad query"


# ---------------------------------------------------------------------------
# _truncate and _clean_code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        pytest.param("", 5, id="empty"),
        pytest.param("abc", 5, id="shorter"),
        pytest.param("abcde", 5, id="exact"),
    ],
)
def test_truncate_keeps_text_within_the_limit(text: str, limit: int) -> None:
    """Text at or under the limit is returned unchanged."""
    assert errors._truncate(text, limit) == text


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        pytest.param("abcdef", 5, "abcd" + ELLIPSIS, id="one-over"),
        pytest.param("a" * 50, 10, "a" * 9 + ELLIPSIS, id="far-over"),
    ],
)
def test_truncate_marks_a_cut_with_an_ellipsis(text: str, limit: int, expected: str) -> None:
    """Longer text is cut so that it and its ellipsis fit the limit."""
    result = errors._truncate(text, limit)
    assert result == expected
    assert len(result) == limit


def test_truncate_result_never_exceeds_the_limit_when_cut_at_whitespace() -> None:
    """A cut that lands on whitespace still yields a marked prefix within the limit."""
    text = "ab   cdefgh"
    result = errors._truncate(text, 5)
    assert len(result) <= 5
    assert result.endswith(ELLIPSIS)
    assert text.startswith(result.removesuffix(ELLIPSIS))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param("", None, id="empty"),
        pytest.param("  \n ", None, id="blank"),
        pytest.param("RATE_LIMIT", "RATE_LIMIT", id="clean"),
        pytest.param(" MISSING_SCOPES\n", "MISSING_SCOPES", id="trimmed"),
        pytest.param("INVALID\n  REQUEST", "INVALID REQUEST", id="multi-line"),
        pytest.param(f"NOT_FOUND {EMAIL}", "NOT_FOUND [REDACTED_EMAIL]", id="email-redacted"),
    ],
)
def test_clean_code_is_one_sanitised_line_and_blank_is_none(
    code: str | None, expected: str | None
) -> None:
    """A code is sanitised like a message; a blank code becomes ``None``, on errors too."""
    assert errors._clean_code(code) == expected
    error = QueryDefect("bad", source="hubspot", endpoint="search", code=code)
    assert error.code == expected


# ---------------------------------------------------------------------------
# _configured_secrets
# ---------------------------------------------------------------------------


def test_configured_secrets_without_credentials_hold_only_the_default_database_url() -> None:
    """The database URL is a secret with a default value; nothing else is set."""
    assert set(errors._configured_secrets()) == {DEFAULT_DATABASE_URL}


def test_configured_secrets_hold_every_secret_and_the_webhook_url_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every set secret is collected, plus the n8n webhook URL and its path."""
    _configure(
        monkeypatch,
        {
            "N8N_GOOGLE_ADS_WEBHOOK_URL": WEBHOOK_URL,
            "N8N_WEBHOOK_SECRET": N8N_SECRET,
            "LINKEDIN_ACCESS_TOKEN": LINKEDIN_TOKEN,
            "HUBSPOT_ACCESS_TOKEN": HUBSPOT_TOKEN,
        },
    )
    assert set(errors._configured_secrets()) == {
        DEFAULT_DATABASE_URL,
        N8N_SECRET,
        LINKEDIN_TOKEN,
        HUBSPOT_TOKEN,
        WEBHOOK_URL,
        WEBHOOK_PATH,
    }


def test_configured_secrets_hold_the_linkedin_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client id is a plain setting, yet an OAuth credential: it is collected, stripped."""
    _configure(monkeypatch, {"LINKEDIN_CLIENT_ID": f"  {LINKEDIN_CLIENT_ID} "})
    assert set(errors._configured_secrets()) == {DEFAULT_DATABASE_URL, LINKEDIN_CLIENT_ID}


@pytest.mark.parametrize(
    "webhook_url",
    [
        pytest.param("https://n8n.example.test", id="no-path"),
        pytest.param("https://n8n.example.test/", id="root-path"),
    ],
)
def test_configured_secrets_skip_an_empty_webhook_path(
    monkeypatch: pytest.MonkeyPatch, webhook_url: str
) -> None:
    """A webhook URL without a path adds only the URL, never a blank or ``/`` entry."""
    _configure(monkeypatch, {"N8N_GOOGLE_ADS_WEBHOOK_URL": webhook_url})
    assert set(errors._configured_secrets()) == {DEFAULT_DATABASE_URL, webhook_url}


def test_configured_secrets_keep_a_webhook_url_whose_path_cannot_be_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL ``urlsplit`` rejects is still collected whole, without a path entry."""
    broken_url = "https://[n8n.example.test/webhook/test-google-ads"
    settings = Settings.model_construct(n8n_google_ads_webhook_url=broken_url)
    monkeypatch.setattr(errors, "get_settings", lambda: settings)
    assert set(errors._configured_secrets()) == {DEFAULT_DATABASE_URL, broken_url}


def test_configured_secrets_are_empty_when_the_environment_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings that fail validation contribute nothing."""
    _configure(monkeypatch, {"GOOGLE_ADS_CUSTOMER_ID": "12345", "N8N_WEBHOOK_SECRET": N8N_SECRET})
    assert errors._configured_secrets() == ()


def test_configured_secrets_are_empty_on_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Settings that raise ``ConfigurationError`` contribute nothing."""

    def unsafe_settings() -> Settings:
        """Stand in for settings that load but are refused."""
        raise ConfigurationError("test configuration refused")

    monkeypatch.setattr(errors, "get_settings", unsafe_settings)
    assert errors._configured_secrets() == ()


# ---------------------------------------------------------------------------
# sanitize_message
# ---------------------------------------------------------------------------


def test_sanitize_message_docstring_example() -> None:
    """Emails are redacted and the line break collapses to one space."""
    assert (
        sanitize_message("No contact with email lead@example.com\n found")
        == "No contact with email [REDACTED_EMAIL] found"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f"Bearer {LINKEDIN_TOKEN} was rejected",
            "Bearer [REDACTED] was rejected",
            id="bearer-token",
        ),
        pytest.param(
            f"header value {N8N_SECRET} refused",
            "header value [REDACTED] refused",
            id="n8n-secret",
        ),
        pytest.param(
            f"private app {HUBSPOT_TOKEN} expired",
            "private app [REDACTED] expired",
            id="hubspot-token",
        ),
        pytest.param(
            f"POST {WEBHOOK_URL} returned 404",
            "POST [REDACTED] returned 404",
            id="webhook-url",
        ),
        pytest.param(
            f"no workflow listens on {WEBHOOK_PATH}",
            "no workflow listens on [REDACTED]",
            id="webhook-path",
        ),
        pytest.param(
            f"contact {EMAIL} and bob@example.org not found",
            "contact [REDACTED_EMAIL] and [REDACTED_EMAIL] not found",
            id="emails",
        ),
        pytest.param(
            "connect to postgresql://ads:test-db-password@db/x failed",
            "connect to postgresql://ads:[REDACTED]@db/x failed",
            id="url-password",
        ),
    ],
)
def test_sanitize_message_removes_emails_and_configured_secrets(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    """Configured secrets, the webhook URL, URL passwords and emails are all redacted."""
    _configure(
        monkeypatch,
        {
            "N8N_GOOGLE_ADS_WEBHOOK_URL": WEBHOOK_URL,
            "N8N_WEBHOOK_SECRET": N8N_SECRET,
            "LINKEDIN_ACCESS_TOKEN": LINKEDIN_TOKEN,
            "HUBSPOT_ACCESS_TOKEN": HUBSPOT_TOKEN,
        },
    )
    assert sanitize_message(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "No contact ?email=alice%40example.com found",
            "No contact ?email=[REDACTED_EMAIL] found",
            id="encoded-email",
        ),
        pytest.param(
            "email=alice%2540example.com rejected",
            "email=[REDACTED_EMAIL] rejected",
            id="double-encoded-email",
        ),
        pytest.param(
            "No contact alice%40Example.COM",
            "No contact [REDACTED_EMAIL]",
            id="mixed-case-encoded-email",
        ),
        pytest.param(
            "bad token %74%65%73%74%2d%6e%38%6e-secret",
            "bad token [REDACTED]",
            id="encoded-configured-secret",
        ),
        pytest.param(
            "Authorization: Bearer test-rotated-token\nrejected",
            "Authorization: [REDACTED] rejected",
            id="authorization-header-text",
        ),
        pytest.param(
            "sent Bearer test-rotated-token upstream",
            "sent Bearer [REDACTED] upstream",
            id="unconfigured-bearer-token",
        ),
        pytest.param(
            "https://api.example.test/x?access_token=test-rotated-token&limit=1",
            "https://api.example.test/x?access_token=[REDACTED]&limit=1",
            id="query-credential",
        ),
        pytest.param(
            "path /crm/v3/objects%2Fcontacts at 100%25",
            "path /crm/v3/objects%2Fcontacts at 100%25",
            id="ordinary-encoded-text-kept",
        ),
    ],
)
def test_sanitize_message_redacts_encoded_values_and_textual_credentials(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    """Percent-encoded emails and secrets, and unconfigured credentials, never persist."""
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": N8N_SECRET})
    assert sanitize_message(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f"Client {LINKEDIN_CLIENT_ID} is not registered",
            "Client [REDACTED] is not registered",
            id="client-id-in-prose",
        ),
        pytest.param(
            "Client %74%65%73%74-linkedin-client-id refused",
            "Client [REDACTED] refused",
            id="percent-encoded-client-id",
        ),
        pytest.param(
            "grant_type=refresh_token&client_id=test-other-client&scope=r_ads",
            "grant_type=refresh_token&client_id=[REDACTED]&scope=r_ads",
            id="unconfigured-client-id-assignment",
        ),
        pytest.param(
            '{"clientId": "test-other-client", "status": 400}',
            '{"clientId": "[REDACTED]", "status": 400}',
            id="unconfigured-client-id-json-key",
        ),
    ],
)
def test_sanitize_message_removes_the_configured_and_assigned_client_id(
    monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    """``LINKEDIN_CLIENT_ID`` never reaches an error text or stored reason, nor any client id.

    The configured one is stripped wherever it occurs, plain or percent-encoded; any value
    assigned to a ``client_id`` key, in any spelling, is stripped whole.
    """
    _configure(monkeypatch, {"LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID})
    assert sanitize_message(text) == expected
    error = AccessDenied(text, source="linkedin", endpoint="access_token")
    assert error.message == expected
    assert LINKEDIN_CLIENT_ID not in error.reason


def _layered(layers: int) -> str:
    """Return a provider message whose email and ``N8N_SECRET`` are encoded ``layers`` times."""
    email, secret = "alice%40example.com", "%74est-n8n-secret"
    for _ in range(layers - 1):
        email, secret = email.replace("%", "%25"), secret.replace("%", "%25")
    return f"email={email} s={secret} rejected"


def _fully_decoded(text: str) -> str:
    """Percent-decode ``text`` until it no longer changes."""
    while (decoded := unquote(text)) != text:
        text = decoded
    return text


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        pytest.param(4, "email=[REDACTED_EMAIL] s=[REDACTED] rejected", id="at-the-limit"),
        # The email key's value is replaced whatever it holds, before the decode pass.
        pytest.param(5, "email=[REDACTED_EMAIL] [REDACTED] rejected", id="one-beyond"),
        pytest.param(6, "email=[REDACTED_EMAIL] [REDACTED] rejected", id="two-beyond"),
    ],
)
def test_sanitize_message_leaves_no_deeply_encoded_email_or_secret(
    monkeypatch: pytest.MonkeyPatch, layers: int, expected: str
) -> None:
    """An email or secret encoded at or beyond the decode limit is never recoverable."""
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": N8N_SECRET})
    reason = sanitize_message(_layered(layers))
    assert reason == expected
    decoded = _fully_decoded(reason)
    assert EMAIL not in decoded
    assert N8N_SECRET not in decoded


def test_sanitize_message_redacts_secrets_shorter_than_8_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured secret is matched inside text whatever its length."""
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": "short7x"})
    assert sanitize_message("value short7x sent") == "value [REDACTED] sent"


def test_access_denied_never_carries_a_short_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 7-character n8n secret echoed in a 403 body leaves neither message nor reason."""
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": SHORT_N8N_SECRET})
    error = AccessDenied(
        f"wrong secret {SHORT_N8N_SECRET}",
        source="google",
        endpoint="search Q-G1",
        http_status=403,
    )
    assert error.message == "wrong secret [REDACTED]"
    assert error.reason == "google search Q-G1: HTTP 403: wrong secret [REDACTED]"
    for text in (error.message, error.reason, str(error), repr(error), str(error.log_fields)):
        assert SHORT_N8N_SECRET not in text


@pytest.mark.parametrize("secret", ["3D", "3A"])
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("access_token%3Dtest-refreshed-token", id="encoded-assignment"),
        pytest.param("Authorization%3A%20test-refreshed-token", id="encoded-authorization"),
        pytest.param(
            "Authorization%3A%20Bearer%20test-refreshed-token", id="encoded-authorization-bearer"
        ),
    ],
)
def test_sanitize_message_never_lets_a_short_secret_break_an_encoded_credential(
    monkeypatch: pytest.MonkeyPatch, secret: str, text: str
) -> None:
    """A short secret inside a ``%XX`` escape takes the whole encoded run with it.

    Replacing ``3D`` alone would leave ``access_token%[REDACTED]test-refreshed-token``,
    which no longer decodes to an assignment, so the token would stay in the reason.
    """
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": secret})
    message = f"HTTP 403: {text} rejected"
    sanitized = sanitize_message(message)
    for fragment in ("test-refreshed-token", "%[REDACTED]"):
        assert fragment not in sanitized
    assert "test-refreshed-token" not in _fully_decoded(sanitized)
    assert sanitized.startswith("HTTP 403: ")
    assert sanitized.endswith(" rejected")


def test_sanitize_message_still_redacts_emails_and_url_passwords_without_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the settings cannot load, emails and URL passwords are still redacted."""
    _configure(monkeypatch, {"GOOGLE_ADS_CUSTOMER_ID": "12345"})
    assert (
        sanitize_message(f"{EMAIL} via postgresql://ads:test-db-password@db/x")
        == "[REDACTED_EMAIL] via postgresql://ads:[REDACTED]@db/x"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", "", id="empty"),
        pytest.param("   \n\t ", "", id="blank"),
        pytest.param("  line one\n\n\tline   two  ", "line one line two", id="whitespace-runs"),
        pytest.param("x" * 300, "x" * 300, id="exactly-300"),
        pytest.param("x" * 301, "x" * 299 + ELLIPSIS, id="over-300"),
    ],
)
def test_sanitize_message_returns_one_line_of_at_most_300_characters(
    text: str, expected: str
) -> None:
    """Whitespace runs collapse to one space and the result is cut to 300 characters."""
    assert sanitize_message(text) == expected


def test_sanitize_message_redacts_before_cutting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A secret straddling the cut is redacted whole, so no part of it survives."""
    _configure(monkeypatch, {"N8N_WEBHOOK_SECRET": N8N_SECRET})
    result = sanitize_message("x" * 290 + " " + N8N_SECRET)
    assert len(result) == 300
    assert result.endswith(ELLIPSIS)
    assert "test-n8n" not in result
    assert result.startswith("x" * 290 + " [RE")


# ---------------------------------------------------------------------------
# holding_secrets
# ---------------------------------------------------------------------------


def _fragments(value: str, size: int = 8) -> list[str]:
    """Return every ``size``-character substring of ``value``, its prefixes included."""
    return [value[start : start + size] for start in range(len(value) - size + 1)]


def test_holding_secrets_strips_a_token_no_setting_knows_from_every_error_text() -> None:
    """A held token echoed in ordinary prose leaves message, code, reason, str and repr."""
    text = f"Member token {HELD_TOKEN} lacks the r_ads scope"
    assert sanitize_message(text) == text
    with holding_secrets([HELD_TOKEN]):
        assert sanitize_message(text) == "Member token [REDACTED] lacks the r_ads scope"
        error = AccessDenied(
            text, source="linkedin", endpoint="ad_account", code=HELD_TOKEN, http_status=403
        )
    assert error.message == "Member token [REDACTED] lacks the r_ads scope"
    assert error.code == "[REDACTED]"
    assert error.reason == (
        "linkedin ad_account: [REDACTED]: Member token [REDACTED] lacks the r_ads scope"
    )
    for rendered in (error.message, error.reason, str(error), repr(error), str(error.log_fields)):
        assert HELD_TOKEN not in rendered
    # The block ended: the token is no longer held.
    assert sanitize_message(text) == text


def test_holding_secrets_strips_percent_encoded_held_tokens() -> None:
    """A held token echoed percent-encoded is stripped like a configured secret."""
    encoded = HELD_TOKEN.replace("-", "%2D")
    with holding_secrets([HELD_TOKEN]):
        sanitized = sanitize_message(f"rejected {encoded} as stale")
    assert sanitized == "rejected [REDACTED] as stale"


def test_holding_secrets_nests_and_restores_the_outer_set_on_exit_and_on_error() -> None:
    """An inner block adds its values to the outer ones; each exit restores the outer set."""
    text = f"{HELD_TOKEN} and {OTHER_HELD_TOKEN}"
    with holding_secrets([HELD_TOKEN]):
        assert sanitize_message(text) == f"[REDACTED] and {OTHER_HELD_TOKEN}"
        with holding_secrets([OTHER_HELD_TOKEN]):
            assert sanitize_message(text) == "[REDACTED] and [REDACTED]"
        assert sanitize_message(text) == f"[REDACTED] and {OTHER_HELD_TOKEN}"
        with (
            pytest.raises(RuntimeError, match="inner failure"),
            holding_secrets([OTHER_HELD_TOKEN]),
        ):
            raise RuntimeError("inner failure")
        assert sanitize_message(text) == f"[REDACTED] and {OTHER_HELD_TOKEN}"
    assert sanitize_message(text) == text


def test_held_secrets_are_local_to_their_context() -> None:
    """The held set is a context variable: code running in another context never sees it."""
    text = f"echo {HELD_TOKEN}"
    with holding_secrets([HELD_TOKEN]):
        assert sanitize_message(text) == "echo [REDACTED]"
        elsewhere = contextvars.Context().run(sanitize_message, text)
    assert elsewhere == text


def test_holding_secrets_ignores_blank_values() -> None:
    """Empty and blank values are never held, so they never match ordinary text."""
    with holding_secrets(["", "   ", HELD_TOKEN]):
        assert sanitize_message("a b  c") == "a b c"
        assert sanitize_message(HELD_TOKEN) == "[REDACTED]"


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(HELD_TOKEN, id="single-string"),
        pytest.param(HELD_TOKEN.encode(), id="single-bytes"),
        pytest.param([HELD_TOKEN, 7], id="non-string-item"),
    ],
)
def test_holding_secrets_rejects_anything_but_a_collection_of_strings(values: Any) -> None:
    """A single string would be read as one-character secrets, so it is refused."""
    with pytest.raises(TypeError), holding_secrets(values):
        pytest.fail("the block must not run")
    assert sanitize_message("a b") == "a b"


def test_a_long_held_token_straddling_the_cut_leaves_no_fragment() -> None:
    """A 400-character held token across character 300 is stripped whole before the cut."""
    filler = "Upstream refused the call. " * 10
    assert len(filler) < 300 < len(filler) + len(LONG_HELD_TOKEN)
    text = f"{filler}{LONG_HELD_TOKEN} again"
    # Not held, the cut keeps the token's first 29 characters.
    unheld = sanitize_message(text)
    assert any(fragment in unheld for fragment in _fragments(LONG_HELD_TOKEN))
    with holding_secrets([LONG_HELD_TOKEN]):
        sanitized = sanitize_message(text)
        error = TransientError(text, source="linkedin", endpoint="ad_account", http_status=503)
    assert sanitized == f"{' '.join(filler.split())} [REDACTED] again"
    for rendered in (sanitized, error.message, error.reason, str(error), repr(error)):
        for fragment in _fragments(LONG_HELD_TOKEN):
            assert fragment not in rendered, fragment


@pytest.mark.parametrize(
    ("kept", "replaced"),
    [
        pytest.param(29, True, id="long-fragment"),
        pytest.param(4, True, id="shortest-replaced-fragment"),
        pytest.param(3, False, id="shorter-run-kept"),
    ],
)
def test_a_rebuilt_error_loses_the_start_of_a_held_token_an_earlier_cut_left(
    kept: int, replaced: bool
) -> None:
    """An error cut through a token before it was held, rebuilt inside the hold, keeps none.

    ``copy.copy`` rebuilds the error through its constructor, so its message is sanitised
    again; the earlier cut left only a start of the token, which no longer matches it
    whole, and that start is replaced from 4 characters on.
    """
    filler = "x" * (299 - kept - 1) + " "
    original = AccessDenied(
        f"{filler}{LONG_HELD_TOKEN}", source="linkedin", endpoint="ad_account", http_status=403
    )
    assert original.message == f"{filler}{LONG_HELD_TOKEN[:kept]}{ELLIPSIS}"
    with holding_secrets([LONG_HELD_TOKEN]):
        rebuilt = copy.copy(original)
    if replaced:
        # The marker may itself be cut again when it makes the text longer than 300.
        assert rebuilt.message == errors._truncate(f"{filler}[REDACTED]{ELLIPSIS}", 300)
        assert LONG_HELD_TOKEN[:kept] not in rebuilt.message
    else:
        assert rebuilt.message == original.message
    assert len(rebuilt.message) <= 300


# ---------------------------------------------------------------------------
# unwrap_single_item
# ---------------------------------------------------------------------------


def test_unwrap_single_item_returns_the_only_element() -> None:
    """n8n's one-element array is unwrapped to the Google response object it holds."""
    response = {"results": [], "fieldMask": "campaign.id"}
    assert unwrap_single_item([response]) is response


def test_unwrap_single_item_unwraps_only_one_level() -> None:
    """A nested one-element list loses only its outer list."""
    inner: list[dict[str, list[object]]] = [{"results": []}]
    assert unwrap_single_item([inner]) is inner


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"results": [], "fieldMask": "campaign.id"}, id="plain-object"),
        pytest.param([], id="empty-list"),
        pytest.param([{"results": []}, {"results": []}], id="two-elements"),
        pytest.param(({"results": []},), id="one-element-tuple"),
        pytest.param("Error in workflow", id="string"),
        pytest.param(None, id="none"),
        pytest.param(7, id="number"),
    ],
)
def test_unwrap_single_item_passes_everything_else_through(body: object) -> None:
    """Anything but a list of exactly one element is returned unchanged."""
    assert unwrap_single_item(body) is body


# ---------------------------------------------------------------------------
# parse_retry_after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("7", 7.0, id="delta-integer"),
        pytest.param("0", 0.0, id="delta-zero"),
        pytest.param("1.5", 1.5, id="delta-decimal"),
        pytest.param("  12 ", 12.0, id="delta-padded"),
        pytest.param("Wed, 07 Oct 2026 12:00:30 GMT", 30.0, id="http-date-gmt"),
        pytest.param("Wed, 07 Oct 2026 14:00:30 +0200", 30.0, id="http-date-offset"),
        pytest.param("Wed, 07 Oct 2026 12:00:30 -0000", 30.0, id="http-date-without-zone"),
        pytest.param("Wed, 07 Oct 2026 12:00:00 GMT", 0.0, id="http-date-now"),
    ],
)
def test_parse_retry_after_reads_delta_seconds_and_http_dates(value: str, expected: float) -> None:
    """Delta seconds are read as given; an HTTP-date is measured from ``now``."""
    assert parse_retry_after(value, now=NOW) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="absent"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param("-5", id="negative"),
        pytest.param("+5", id="signed"),
        pytest.param("soon", id="garbage"),
        pytest.param("1e3", id="exponent"),
        pytest.param("1.", id="bare-decimal-point"),
        pytest.param("inf", id="inf"),
        pytest.param("nan", id="nan"),
        pytest.param("9" * 400, id="overflows-to-infinity"),
        pytest.param("Wed, 07 Oct 2026 11:59:59 GMT", id="http-date-past"),
        pytest.param("Thu, 01 Jan 1970 00:00:00 GMT", id="http-date-epoch"),
        pytest.param("Wed, 32 Oct 2026 12:00:30 GMT", id="http-date-invalid-day"),
        pytest.param("Wed, 07 Oct 99999 12:00:30 GMT", id="http-date-year-out-of-range"),
    ],
)
def test_parse_retry_after_rejects_absent_unparsable_signed_infinite_and_past_values(
    value: str | None,
) -> None:
    """Values that do not give a finite, non-negative future delay yield ``None``."""
    assert parse_retry_after(value, now=NOW) is None


def test_parse_retry_after_reads_a_naive_now_as_utc() -> None:
    """A naive reference instant is taken to be UTC."""
    naive_now = NOW.replace(tzinfo=None)
    assert parse_retry_after("Wed, 07 Oct 2026 12:00:30 GMT", now=naive_now) == 30.0


def test_parse_retry_after_measures_from_the_current_time_by_default() -> None:
    """Without ``now``, an HTTP-date is measured from the current UTC time."""
    target = datetime(2100, 1, 1, tzinfo=UTC)
    before = datetime.now(UTC)
    delay = parse_retry_after("Fri, 01 Jan 2100 00:00:00 GMT")
    after = datetime.now(UTC)
    assert delay is not None
    assert (target - after).total_seconds() <= delay <= (target - before).total_seconds()
    assert parse_retry_after("Thu, 01 Jan 1970 00:00:00 GMT") is None
