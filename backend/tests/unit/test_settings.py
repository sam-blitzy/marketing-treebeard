"""Behavioural tests for ``ads_engine.settings``.

Every test runs against an environment stripped of each variable ``Settings`` reads, so
host-exported credentials never reach a test; each test sets the fake values it needs.
No test performs network or database I/O.
"""

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import cast, get_args
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import AliasChoices, SecretStr, ValidationError

from ads_engine.settings import (
    SOURCES,
    ConfigurationError,
    DataMode,
    Settings,
    Source,
    get_settings,
)

WEBHOOK_URL = "https://n8n.example.test/webhook/google-ads"
CUSTOMER_ID = "1234567890"
N8N_SECRET = "test-n8n-webhook-secret"
LINKEDIN_ACCOUNT_ID = "500000001"
LINKEDIN_CLIENT_ID = "test-linkedin-client-id"
LINKEDIN_CLIENT_SECRET = "test-linkedin-client-secret"
LINKEDIN_REFRESH_TOKEN = "test-linkedin-refresh-token"
LINKEDIN_ACCESS_TOKEN = "test-linkedin-token"
HUBSPOT_TOKEN = "test-hubspot-token"
SESSION_SECRET = "test-session-secret"
PASSWORD_HASH = "test-argon2-password-hash"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ads:ads@db:5432/ads_engine"

GOOGLE_ENV: Mapping[str, str] = {
    "N8N_GOOGLE_ADS_WEBHOOK_URL": WEBHOOK_URL,
    "N8N_WEBHOOK_SECRET": N8N_SECRET,
    "GOOGLE_ADS_CUSTOMER_ID": CUSTOMER_ID,
}
LINKEDIN_TRIO_ENV: Mapping[str, str] = {
    "LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID,
    "LINKEDIN_CLIENT_SECRET": LINKEDIN_CLIENT_SECRET,
    "LINKEDIN_REFRESH_TOKEN": LINKEDIN_REFRESH_TOKEN,
}
SECRET_ENV: Mapping[str, str] = {
    "N8N_WEBHOOK_SECRET": N8N_SECRET,
    "LINKEDIN_CLIENT_SECRET": LINKEDIN_CLIENT_SECRET,
    "LINKEDIN_REFRESH_TOKEN": LINKEDIN_REFRESH_TOKEN,
    "LINKEDIN_ACCESS_TOKEN": LINKEDIN_ACCESS_TOKEN,
    "HUBSPOT_ACCESS_TOKEN": HUBSPOT_TOKEN,
    "APP_ADMIN_PASSWORD_HASH": PASSWORD_HASH,
    "SESSION_SECRET": SESSION_SECRET,
}


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


def _set_env(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> None:
    """Export each variable in ``env`` for the current test only."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads, in any letter case, and reset the cache.

    pydantic-settings matches environment names case-insensitively here, so a host
    variable such as ``linkedin_access_token`` would be read as well.
    """
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


def test_sources_lists_every_source_in_display_order() -> None:
    assert SOURCES == ("google", "linkedin", "hubspot")
    assert get_args(Source) == SOURCES


def test_data_mode_allows_exactly_auto_and_simulated() -> None:
    assert get_args(DataMode) == ("auto", "simulated")


def test_configuration_error_is_a_runtime_error() -> None:
    assert issubclass(ConfigurationError, RuntimeError)


def test_configuration_error_names_its_fields_only_when_given() -> None:
    plain = ConfigurationError("refused")
    named = ConfigurationError("refused", fields=("app_base_url",))

    assert (str(plain), plain.fields) == ("refused", ())
    assert (str(named), named.fields) == ("refused", ("app_base_url",))


# ---------------------------------------------------------------------------
# Defaults and environment reading
# ---------------------------------------------------------------------------


def test_defaults_are_the_documented_safe_local_values() -> None:
    settings = Settings()

    assert settings.database_url.get_secret_value() == DEFAULT_DATABASE_URL
    assert settings.app_base_url == "http://localhost:8080"
    assert settings.display_timezone == "UTC"
    assert settings.data_mode == "auto"
    assert settings.mock_seed == 42
    assert settings.backfill_days == 180
    assert settings.n8n_google_ads_webhook_url is None
    assert settings.n8n_webhook_secret_header == "X-Webhook-Secret"
    assert settings.n8n_webhook_secret is None
    assert settings.google_ads_customer_id is None
    assert settings.google_daily_call_budget == 1000
    assert settings.linkedin_ad_account_ids == []
    assert settings.linkedin_client_id is None
    assert settings.linkedin_client_secret is None
    assert settings.linkedin_refresh_token is None
    assert settings.linkedin_access_token is None
    assert settings.hubspot_access_token is None
    assert settings.hubspot_google_campaign_property is None
    assert settings.hubspot_google_touch_date_property is None
    assert settings.app_admin_email == "admin@example.com"
    assert settings.app_admin_password_hash is None
    assert settings.session_secret is None


def test_defaults_leave_every_source_without_credentials() -> None:
    settings = Settings()

    assert not settings.secure_cookies
    assert settings.display_tz == ZoneInfo("UTC")
    assert not settings.linkedin_uses_refresh_token
    assert not settings.google_crm_link_configured
    for source in SOURCES:
        assert not settings.has_credentials(source)


def test_reads_every_variable_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        {
            "DATABASE_URL": "postgresql+psycopg://ads:ads@127.0.0.1:55432/ads_engine_test",
            "APP_BASE_URL": "https://ads.example.test",
            "DISPLAY_TIMEZONE": "America/New_York",
            "DATA_MODE": "simulated",
            "MOCK_SEED": "7",
            "BACKFILL_DAYS": "90",
            "N8N_GOOGLE_ADS_WEBHOOK_URL": WEBHOOK_URL,
            "N8N_WEBHOOK_SECRET_HEADER": "X-Webhook-Token",
            "N8N_WEBHOOK_SECRET": N8N_SECRET,
            "GOOGLE_ADS_CUSTOMER_ID": CUSTOMER_ID,
            "GOOGLE_DAILY_CALL_BUDGET": "25",
            "LINKEDIN_AD_ACCOUNT_IDS": LINKEDIN_ACCOUNT_ID,
            "LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID,
            "LINKEDIN_CLIENT_SECRET": LINKEDIN_CLIENT_SECRET,
            "LINKEDIN_REFRESH_TOKEN": LINKEDIN_REFRESH_TOKEN,
            "LINKEDIN_ACCESS_TOKEN": LINKEDIN_ACCESS_TOKEN,
            "HUBSPOT_ACCESS_TOKEN": HUBSPOT_TOKEN,
            "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": "google_campaign_id",
            "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": "google_touch_date",
            "APP_ADMIN_EMAIL": "ops@example.test",
            "APP_ADMIN_PASSWORD_HASH": PASSWORD_HASH,
            "SESSION_SECRET": SESSION_SECRET,
        },
    )

    settings = Settings()

    assert settings.database_url.get_secret_value() == (
        "postgresql+psycopg://ads:ads@127.0.0.1:55432/ads_engine_test"
    )
    assert settings.app_base_url == "https://ads.example.test"
    assert settings.display_timezone == "America/New_York"
    assert settings.display_tz == ZoneInfo("America/New_York")
    assert settings.data_mode == "simulated"
    assert settings.mock_seed == 7
    assert settings.backfill_days == 90
    assert settings.n8n_google_ads_webhook_url == WEBHOOK_URL
    assert settings.n8n_webhook_secret_header == "X-Webhook-Token"
    assert settings.n8n_webhook_secret == SecretStr(N8N_SECRET)
    assert settings.google_ads_customer_id == CUSTOMER_ID
    assert settings.google_daily_call_budget == 25
    assert settings.linkedin_ad_account_ids == [LINKEDIN_ACCOUNT_ID]
    assert settings.linkedin_client_id == LINKEDIN_CLIENT_ID
    assert settings.linkedin_client_secret == SecretStr(LINKEDIN_CLIENT_SECRET)
    assert settings.linkedin_refresh_token == SecretStr(LINKEDIN_REFRESH_TOKEN)
    assert settings.linkedin_access_token == SecretStr(LINKEDIN_ACCESS_TOKEN)
    assert settings.hubspot_access_token == SecretStr(HUBSPOT_TOKEN)
    assert settings.hubspot_google_campaign_property == "google_campaign_id"
    assert settings.hubspot_google_touch_date_property == "google_touch_date"
    assert settings.app_admin_email == "ops@example.test"
    assert settings.app_admin_password_hash == SecretStr(PASSWORD_HASH)
    assert settings.session_secret == SecretStr(SESSION_SECRET)
    assert settings.secure_cookies
    assert settings.google_crm_link_configured
    for source in SOURCES:
        assert settings.has_credentials(source)


def test_environment_names_match_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("data_mode", "simulated")
    monkeypatch.setenv("Session_Secret", SESSION_SECRET)

    settings = Settings()

    assert settings.data_mode == "simulated"
    assert settings.session_secret == SecretStr(SESSION_SECRET)


@pytest.mark.parametrize(
    ("host_name", "field_name"),
    [
        ("HUBSPOT_PRIVATE_APP_TOKEN", "hubspot_access_token"),
        ("N8N_GOOGLE_ADS_WEBHOOK_TOKEN", "n8n_webhook_secret"),
        ("LINKEDIN_AD_ACCOUNT_ID", "linkedin_ad_account_ids"),
    ],
)
def test_undocumented_alternative_names_are_never_read(
    monkeypatch: pytest.MonkeyPatch, host_name: str, field_name: str
) -> None:
    monkeypatch.setenv(host_name, LINKEDIN_ACCOUNT_ID)

    settings = Settings()

    assert getattr(settings, field_name) == Settings.model_fields[field_name].get_default(
        call_default_factory=True
    )


def test_dotenv_file_in_working_directory_is_not_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        f"SESSION_SECRET={SESSION_SECRET}\nDATA_MODE=simulated\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.session_secret is None
    assert settings.data_mode == "auto"


def test_settings_are_frozen() -> None:
    settings = Settings()

    with pytest.raises(ValidationError, match="frozen"):
        settings.data_mode = "simulated"  # type: ignore[misc]  # the runtime guard under test


# ---------------------------------------------------------------------------
# Blank values count as unset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t \n"])
@pytest.mark.parametrize("field_name", sorted(Settings.model_fields))
def test_blank_environment_value_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, field_name: str, blank: str
) -> None:
    monkeypatch.setenv(field_name.upper(), blank)

    settings = Settings()

    assert getattr(settings, field_name) == Settings.model_fields[field_name].get_default(
        call_default_factory=True
    )


def test_blank_constructor_arguments_fall_back_to_the_defaults() -> None:
    settings = Settings(
        mock_seed=cast(int, "  "),
        app_base_url=" ",
        session_secret=SecretStr("   "),
        linkedin_access_token=SecretStr(""),
    )

    assert settings.mock_seed == 42
    assert settings.app_base_url == "http://localhost:8080"
    assert settings.session_secret is None
    assert settings.linkedin_access_token is None


def test_blank_secret_counts_as_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, GOOGLE_ENV)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", "   ")

    settings = Settings()

    assert settings.n8n_webhook_secret is None
    assert settings.missing_credentials("google") == ["N8N_WEBHOOK_SECRET"]
    assert not settings.has_credentials("google")


@pytest.mark.parametrize(
    "variable",
    [
        "LINKEDIN_CLIENT_ID",
        "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY",
        "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY",
    ],
)
def test_identifiers_are_stripped(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    monkeypatch.setenv(variable, "  test-identifier \t")

    settings = Settings()

    assert getattr(settings, variable.lower()) == "test-identifier"


# ---------------------------------------------------------------------------
# DATA_MODE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auto", "auto"),
        ("AUTO", "auto"),
        ("simulated", "simulated"),
        ("Simulated", "simulated"),
        ("  SIMULATED  ", "simulated"),
    ],
)
def test_data_mode_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: DataMode
) -> None:
    monkeypatch.setenv("DATA_MODE", raw)

    assert Settings().data_mode == expected


@pytest.mark.parametrize("raw", ["live", "mock", "simulate", "auto,simulated"])
def test_unknown_data_mode_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DATA_MODE", raw)

    with pytest.raises(ValidationError, match="data_mode"):
        Settings()


# ---------------------------------------------------------------------------
# Numeric settings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "raw"),
    [
        ("BACKFILL_DAYS", "0"),
        ("BACKFILL_DAYS", "-1"),
        ("GOOGLE_DAILY_CALL_BUDGET", "0"),
        ("GOOGLE_DAILY_CALL_BUDGET", "-25"),
        ("MOCK_SEED", "forty-two"),
        ("BACKFILL_DAYS", "1.5"),
    ],
)
def test_invalid_numbers_are_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, raw: str
) -> None:
    monkeypatch.setenv(variable, raw)

    with pytest.raises(ValidationError, match=variable.lower()):
        Settings()


def test_minimum_positive_numbers_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKFILL_DAYS", "1")
    monkeypatch.setenv("GOOGLE_DAILY_CALL_BUDGET", "1")
    monkeypatch.setenv("MOCK_SEED", "0")

    settings = Settings()

    assert settings.backfill_days == 1
    assert settings.google_daily_call_budget == 1
    assert settings.mock_seed == 0


# ---------------------------------------------------------------------------
# GOOGLE_ADS_CUSTOMER_ID
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["1234567890", "123-456-7890", "  123-456-7890  ", "12-34-56-78-90"],
)
def test_customer_id_is_normalised_to_ten_digits(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", raw)

    assert Settings().google_ads_customer_id == CUSTOMER_ID


@pytest.mark.parametrize(
    "raw",
    [
        "123456789",
        "12345678901",
        "123-456-789a",
        "123 456 7890",
        "123_456_7890",
        "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10",
    ],
)
def test_customer_id_that_is_not_ten_digits_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", raw)

    with pytest.raises(ValidationError, match="GOOGLE_ADS_CUSTOMER_ID must be 10 digits"):
        Settings()


def test_non_string_customer_id_is_left_to_type_validation() -> None:
    with pytest.raises(ValidationError, match="valid string"):
        Settings(google_ads_customer_id=cast(str, 1234567890))


# ---------------------------------------------------------------------------
# LINKEDIN_AD_ACCOUNT_IDS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("500000001", ["500000001"]),
        ("urn:li:sponsoredAccount:500000001", ["500000001"]),
        (" 500000001 , 500000002 ", ["500000001", "500000002"]),
        ("500000002,urn:li:sponsoredAccount:500000001", ["500000002", "500000001"]),
        ("500000002,500000001,500000002", ["500000002", "500000001"]),
        ("urn:li:sponsoredAccount:500000001,500000001", ["500000001"]),
        (",500000001,,", ["500000001"]),
        (",, ,", []),
    ],
)
def test_linkedin_account_ids_are_normalised_and_deduplicated(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", raw)

    assert Settings().linkedin_ad_account_ids == expected


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "500000001,12a",
        "urn:li:sponsoredAccount:",
        "urn:li:organization:500000001",
        "urn:li:sponsoredAccount:500 000001",
        "-500000001",
    ],
)
def test_linkedin_account_id_that_is_not_numeric_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", raw)

    with pytest.raises(ValidationError, match="LINKEDIN_AD_ACCOUNT_IDS entries must be numeric"):
        Settings()


def test_linkedin_account_ids_accept_a_list_or_tuple() -> None:
    from_list = Settings(
        linkedin_ad_account_ids=["urn:li:sponsoredAccount:500000001", " 500000002 ", ""]
    )
    from_tuple = Settings(
        linkedin_ad_account_ids=cast(list[str], ("500000003", 500000003, 500000004))
    )
    from_none = Settings(linkedin_ad_account_ids=cast(list[str], None))

    assert from_list.linkedin_ad_account_ids == ["500000001", "500000002"]
    assert from_tuple.linkedin_ad_account_ids == ["500000003", "500000004"]
    assert from_none.linkedin_ad_account_ids == []


def test_linkedin_account_ids_of_another_type_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must be a comma-separated list"):
        Settings(linkedin_ad_account_ids=cast(list[str], 500000001))


# ---------------------------------------------------------------------------
# DISPLAY_TIMEZONE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("UTC", "UTC"),
        ("America/New_York", "America/New_York"),
        (" Europe/London ", "Europe/London"),
    ],
)
def test_display_timezone_accepts_iana_names(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", raw)

    settings = Settings()

    assert settings.display_timezone == expected
    assert settings.display_tz == ZoneInfo(expected)


@pytest.mark.parametrize("raw", ["Mars/Olympus_Mons", "../etc/passwd", "/", "UTC/../UTC"])
def test_display_timezone_that_is_not_an_iana_name_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("DISPLAY_TIMEZONE", raw)

    with pytest.raises(ValidationError, match="DISPLAY_TIMEZONE must be an IANA time zone name"):
        Settings()


# ---------------------------------------------------------------------------
# APP_BASE_URL, Secure cookies and the API's base-URL safety check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8080", "http://localhost:8080"),
        ("http://localhost:8080/", "http://localhost:8080"),
        ("  https://ads.example.test/app/  ", "https://ads.example.test/app"),
        ("HTTPS://ads.example.test///", "https://ads.example.test"),
        ("Http://127.0.0.1:20002", "http://127.0.0.1:20002"),
    ],
)
def test_app_base_url_is_normalised(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("APP_BASE_URL", raw)

    assert Settings().app_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "localhost:8080",
        "ads.example.test",
        "//ads.example.test",
        "ftp://ads.example.test",
        "http://",
        "https:///path",
        "http://localhost:0",
        "http://localhost:notaport",
        "http://localhost:70000",
    ],
)
def test_app_base_url_that_is_not_absolute_http_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("APP_BASE_URL", raw)

    with pytest.raises(ValidationError, match="APP_BASE_URL must be an absolute http"):
        Settings()


@pytest.mark.parametrize(
    ("base_url", "secure"),
    [
        ("https://ads.example.test", True),
        ("HTTPS://ads.example.test", True),
        ("https://localhost:8443", True),
        ("http://localhost:8080", False),
        ("http://127.0.0.1:20002", False),
    ],
)
def test_session_cookies_are_secure_exactly_for_https(
    monkeypatch: pytest.MonkeyPatch, base_url: str, secure: bool
) -> None:
    monkeypatch.setenv("APP_BASE_URL", base_url)

    assert Settings().secure_cookies is secure


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8080",
        "http://localhost",
        "http://LOCALHOST:8080",
        "http://127.0.0.1:20002",
        "http://localhost:8080/app/",
        "https://ads.example.test",
        "https://203.0.113.10:8443",
    ],
)
def test_safe_base_urls_pass(monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    monkeypatch.setenv("APP_BASE_URL", base_url)

    Settings().assert_safe_base_url()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://ads.example.test",
        "http://0.0.0.0:8080",
        "http://localhost.example.test",
        "http://127.0.0.2:8080",
        "http://[::1]:8080",
        "http://192.0.2.10",
    ],
)
def test_plain_http_on_a_non_local_host_is_refused(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    monkeypatch.setenv("APP_BASE_URL", base_url)
    settings = Settings()

    with pytest.raises(ConfigurationError, match=r"APP_BASE_URL must use https://"):
        settings.assert_safe_base_url()


# ---------------------------------------------------------------------------
# check_api_startup
# ---------------------------------------------------------------------------


def test_api_startup_passes_on_local_http_with_a_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)

    Settings().check_api_startup()


def test_api_startup_passes_on_https_with_a_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_BASE_URL", "https://ads.example.test")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)

    Settings().check_api_startup()


@pytest.mark.parametrize("session_secret", [None, "", "   "])
def test_api_startup_refuses_without_a_session_secret(
    monkeypatch: pytest.MonkeyPatch, session_secret: str | None
) -> None:
    if session_secret is not None:
        monkeypatch.setenv("SESSION_SECRET", session_secret)
    settings = Settings()

    with pytest.raises(ConfigurationError, match="SESSION_SECRET is not set; run make init"):
        settings.check_api_startup()


def test_api_startup_refuses_an_unsafe_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BASE_URL", "http://ads.example.test")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    settings = Settings()

    with pytest.raises(ConfigurationError, match="terminate TLS in front of nginx"):
        settings.check_api_startup()


def test_api_startup_reports_the_base_url_before_the_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_BASE_URL", "http://ads.example.test")
    settings = Settings()

    with pytest.raises(ConfigurationError, match="APP_BASE_URL"):
        settings.check_api_startup()


@pytest.mark.parametrize(
    ("base_url", "session_secret", "fields"),
    [
        ("http://ads.example.test", SESSION_SECRET, ("app_base_url",)),
        ("http://ads.example.test", None, ("app_base_url",)),
        ("https://ads.example.test", None, ("session_secret",)),
    ],
    ids=["unsafe-base-url", "both-base-url-first", "no-session-secret"],
)
def test_api_startup_refusal_names_the_field_at_fault(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    session_secret: str | None,
    fields: tuple[str, ...],
) -> None:
    monkeypatch.setenv("APP_BASE_URL", base_url)
    if session_secret is not None:
        monkeypatch.setenv("SESSION_SECRET", session_secret)
    settings = Settings()

    with pytest.raises(ConfigurationError) as refused:
        settings.check_api_startup()

    assert refused.value.fields == fields


# ---------------------------------------------------------------------------
# N8N_GOOGLE_ADS_WEBHOOK_URL and N8N_WEBHOOK_SECRET_HEADER
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (WEBHOOK_URL, WEBHOOK_URL),
        (f"  {WEBHOOK_URL}  ", WEBHOOK_URL),
        ("http://localhost:5678/webhook/google-ads", "http://localhost:5678/webhook/google-ads"),
        ("http://127.0.0.1:5678/webhook/x", "http://127.0.0.1:5678/webhook/x"),
        ("HTTP://LOCALHOST:5678/webhook/google-ads", "HTTP://LOCALHOST:5678/webhook/google-ads"),
    ],
)
def test_webhook_url_accepts_https_and_local_http_urls(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", raw)

    assert Settings().n8n_google_ads_webhook_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://n8n.example.test/webhook/test-plain-http-path",
        "HTTP://n8n.example.test/webhook/test-plain-http-path",
        "http://127.0.0.2:5678/webhook/test-plain-http-path",
        "http://0.0.0.0:5678/webhook/test-plain-http-path",
        "http://[::1]:5678/webhook/test-plain-http-path",
        "http://localhost.example.test/webhook/test-plain-http-path",
        "http://192.0.2.10/webhook/test-plain-http-path",
    ],
)
def test_plain_http_webhook_url_on_a_non_local_host_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", raw)
    host = urlsplit(raw).hostname
    assert host

    with pytest.raises(
        ValidationError, match="N8N_GOOGLE_ADS_WEBHOOK_URL must use https://"
    ) as excinfo:
        Settings()

    rendered = f"{excinfo.value}\n{excinfo.value!r}"
    assert "test-plain-http-path" not in rendered
    assert host not in rendered


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://n8n.example.test/webhook/test-workflow-path",
        "n8n.example.test/webhook/test-workflow-path",
        "https:///webhook/test-workflow-path",
        "https://n8n.example.test:0/webhook/test-workflow-path",
    ],
)
def test_invalid_webhook_url_is_rejected_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", raw)

    with pytest.raises(ValidationError, match="N8N_GOOGLE_ADS_WEBHOOK_URL must be") as excinfo:
        Settings()

    rendered = f"{excinfo.value}\n{excinfo.value!r}"
    assert "test-workflow-path" not in rendered
    assert "n8n.example.test" not in rendered


def test_non_string_webhook_url_is_left_to_type_validation() -> None:
    with pytest.raises(ValidationError, match="valid string"):
        Settings(n8n_google_ads_webhook_url=cast(str, 8080))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("X-Webhook-Secret", "X-Webhook-Secret"),
        ("X-Webhook-Token", "X-Webhook-Token"),
        ("  X-Webhook-Token  ", "X-Webhook-Token"),
        ("x_custom.auth~1", "x_custom.auth~1"),
    ],
)
def test_secret_header_accepts_http_token_names(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("N8N_WEBHOOK_SECRET_HEADER", raw)

    assert Settings().n8n_webhook_secret_header == expected


@pytest.mark.parametrize(
    "raw",
    ["X Webhook Secret", "X-Webhook:Secret", "X-Webhook-Secret\r\nX-Injected", "X-W\u00e9bhook"],
)
def test_secret_header_that_is_not_an_http_token_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("N8N_WEBHOOK_SECRET_HEADER", raw)

    with pytest.raises(ValidationError, match="N8N_WEBHOOK_SECRET_HEADER must be an HTTP header"):
        Settings()


# ---------------------------------------------------------------------------
# APP_ADMIN_EMAIL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("admin@example.com", "admin@example.com"),
        ("  Ops.Admin@Example.TEST ", "ops.admin@example.test"),
    ],
)
def test_admin_email_is_stripped_and_lowercased(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv("APP_ADMIN_EMAIL", raw)

    assert Settings().app_admin_email == expected


@pytest.mark.parametrize(
    "raw", ["admin", "@example.com", "admin@", "ad min@example.com", "admin@exa\tmple.com"]
)
def test_admin_email_without_an_address_shape_is_rejected(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("APP_ADMIN_EMAIL", raw)

    with pytest.raises(ValidationError, match="APP_ADMIN_EMAIL must be an email address"):
        Settings()


# ---------------------------------------------------------------------------
# Credentials per source
# ---------------------------------------------------------------------------


def test_google_has_credentials_with_url_secret_and_customer_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, GOOGLE_ENV)

    settings = Settings()

    assert settings.missing_credentials("google") == []
    assert settings.has_credentials("google")


@pytest.mark.parametrize("absent", sorted(GOOGLE_ENV))
def test_google_lacks_credentials_when_any_part_is_missing(
    monkeypatch: pytest.MonkeyPatch, absent: str
) -> None:
    _set_env(monkeypatch, {name: value for name, value in GOOGLE_ENV.items() if name != absent})

    settings = Settings()

    assert settings.missing_credentials("google") == [absent]
    assert not settings.has_credentials("google")


def test_google_missing_credentials_are_listed_in_a_stable_order() -> None:
    assert Settings().missing_credentials("google") == [
        "N8N_GOOGLE_ADS_WEBHOOK_URL",
        "N8N_WEBHOOK_SECRET",
        "GOOGLE_ADS_CUSTOMER_ID",
    ]


def test_linkedin_has_credentials_with_account_ids_and_the_refresh_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", LINKEDIN_ACCOUNT_ID)
    _set_env(monkeypatch, LINKEDIN_TRIO_ENV)

    settings = Settings()

    assert settings.missing_credentials("linkedin") == []
    assert settings.has_credentials("linkedin")
    assert settings.linkedin_uses_refresh_token


def test_linkedin_has_credentials_with_account_ids_and_an_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", LINKEDIN_ACCOUNT_ID)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)

    settings = Settings()

    assert settings.missing_credentials("linkedin") == []
    assert settings.has_credentials("linkedin")
    assert not settings.linkedin_uses_refresh_token


def test_linkedin_refresh_trio_wins_over_a_static_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", LINKEDIN_ACCOUNT_ID)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)
    _set_env(monkeypatch, LINKEDIN_TRIO_ENV)

    settings = Settings()

    assert settings.has_credentials("linkedin")
    assert settings.linkedin_uses_refresh_token


def test_linkedin_without_account_ids_lacks_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)
    _set_env(monkeypatch, LINKEDIN_TRIO_ENV)

    settings = Settings()

    assert settings.missing_credentials("linkedin") == ["LINKEDIN_AD_ACCOUNT_IDS"]
    assert not settings.has_credentials("linkedin")


def test_linkedin_with_only_blank_account_entries_lacks_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", " , ,")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)

    assert Settings().missing_credentials("linkedin") == ["LINKEDIN_AD_ACCOUNT_IDS"]


@pytest.mark.parametrize("absent", sorted(LINKEDIN_TRIO_ENV))
def test_linkedin_partial_trio_without_access_token_lacks_credentials(
    monkeypatch: pytest.MonkeyPatch, absent: str
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", LINKEDIN_ACCOUNT_ID)
    _set_env(
        monkeypatch, {name: value for name, value in LINKEDIN_TRIO_ENV.items() if name != absent}
    )

    settings = Settings()

    assert settings.missing_credentials("linkedin") == [absent, "LINKEDIN_ACCESS_TOKEN"]
    assert not settings.has_credentials("linkedin")
    assert not settings.linkedin_uses_refresh_token


def test_linkedin_partial_trio_with_access_token_has_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", LINKEDIN_ACCOUNT_ID)
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", LINKEDIN_CLIENT_ID)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)

    settings = Settings()

    assert settings.has_credentials("linkedin")
    assert not settings.linkedin_uses_refresh_token


def test_linkedin_missing_credentials_are_listed_in_a_stable_order() -> None:
    assert Settings().missing_credentials("linkedin") == [
        "LINKEDIN_AD_ACCOUNT_IDS",
        "LINKEDIN_CLIENT_ID",
        "LINKEDIN_CLIENT_SECRET",
        "LINKEDIN_REFRESH_TOKEN",
        "LINKEDIN_ACCESS_TOKEN",
    ]


def test_hubspot_credentials_are_its_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().missing_credentials("hubspot") == ["HUBSPOT_ACCESS_TOKEN"]
    assert not Settings().has_credentials("hubspot")

    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", HUBSPOT_TOKEN)

    assert Settings().missing_credentials("hubspot") == []
    assert Settings().has_credentials("hubspot")


def test_each_source_is_judged_only_by_its_own_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, GOOGLE_ENV)

    settings = Settings()

    assert settings.has_credentials("google")
    assert not settings.has_credentials("linkedin")
    assert not settings.has_credentials("hubspot")


def test_unknown_source_fails_loudly() -> None:
    with pytest.raises(AssertionError, match="unreachable"):
        Settings().missing_credentials(cast(Source, "bing"))


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ({}, False),
        ({"HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": "google_campaign_id"}, False),
        ({"HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": "google_touch_date"}, False),
        (
            {
                "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": "google_campaign_id",
                "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": "google_touch_date",
            },
            True,
        ),
        (
            {
                "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": "google_campaign_id",
                "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": "   ",
            },
            False,
        ),
    ],
)
def test_google_crm_link_needs_both_contact_properties(
    monkeypatch: pytest.MonkeyPatch, configured: Mapping[str, str], expected: bool
) -> None:
    _set_env(monkeypatch, configured)

    assert Settings().google_crm_link_configured is expected


# ---------------------------------------------------------------------------
# Secrets never print
# ---------------------------------------------------------------------------


def test_secrets_never_appear_in_repr_str_or_serialised_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, SECRET_ENV)
    database_password = "test-database-password"
    monkeypatch.setenv(
        "DATABASE_URL", f"postgresql+psycopg://ads:{database_password}@127.0.0.1:55432/ads"
    )

    settings = Settings()
    rendered = "\n".join(
        [
            repr(settings),
            str(settings),
            f"{settings}",
            settings.model_dump_json(),
            str(settings.model_dump()),
        ]
    )

    for secret in [*SECRET_ENV.values(), database_password]:
        assert secret not in rendered
    assert "**********" in rendered
    assert settings.n8n_webhook_secret is not None
    assert settings.n8n_webhook_secret.get_secret_value() == N8N_SECRET
    assert database_password in settings.database_url.get_secret_value()


def test_validation_errors_never_echo_the_input(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, SECRET_ENV)
    monkeypatch.setenv("MOCK_SEED", "test-seed-input-value")

    with pytest.raises(ValidationError, match="mock_seed") as excinfo:
        Settings()

    rendered = f"{excinfo.value}\n{excinfo.value!r}"
    assert "test-seed-input-value" not in rendered
    assert "input_value" not in rendered
    for secret in SECRET_ENV.values():
        assert secret not in rendered


def test_validation_error_on_a_secret_field_never_echoes_it() -> None:
    with pytest.raises(ValidationError, match="session_secret") as excinfo:
        Settings(session_secret=cast(SecretStr, ["test-session-secret-input"]))

    rendered = f"{excinfo.value}\n{excinfo.value!r}"
    assert "test-session-secret-input" not in rendered


def test_secret_values_lists_each_set_secret_once_in_field_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", N8N_SECRET)
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", N8N_SECRET)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_ACCESS_TOKEN)

    assert Settings().secret_values() == (
        DEFAULT_DATABASE_URL,
        N8N_SECRET,
        LINKEDIN_ACCESS_TOKEN,
        SESSION_SECRET,
    )


def test_secret_values_skips_unset_secrets() -> None:
    assert Settings().secret_values() == (DEFAULT_DATABASE_URL,)


# ---------------------------------------------------------------------------
# get_settings
# ---------------------------------------------------------------------------


def test_get_settings_returns_one_cached_instance_until_cache_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_MODE", "simulated")
    first = get_settings()

    assert get_settings() is first

    monkeypatch.setenv("DATA_MODE", "auto")

    assert get_settings() is first
    assert get_settings().data_mode == "simulated"

    get_settings.cache_clear()
    refreshed = get_settings()

    assert refreshed is not first
    assert refreshed.data_mode == "auto"


def test_get_settings_raises_on_an_invalid_environment_without_caching_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKFILL_DAYS", "0")

    with pytest.raises(ValidationError, match="backfill_days"):
        get_settings()

    monkeypatch.setenv("BACKFILL_DAYS", "90")

    assert get_settings().backfill_days == 90
