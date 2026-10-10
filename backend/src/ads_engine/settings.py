"""Typed environment settings for the Ads Action Engine.

Every process (``api``, ``worker``, the ``ads-engine`` CLI and Alembic) reads its
configuration through :func:`get_settings`. Values come only from the process
environment: ``.env`` is never read here, because Docker Compose injects it into the
``migrate``, ``api`` and ``worker`` containers. The ``db`` container receives only the
``POSTGRES_*`` values, which this module never reads.

Conventions:

* A field's name is the lowercase form of its environment variable, so ``database_url``
  reads ``DATABASE_URL``. Other modules depend on these attribute names, for example
  ``migrations/env.py`` calls ``get_settings().database_url.get_secret_value()``.
* Only the documented variable names are read, with no prefix and no alternative
  aliases. A host that exports credentials under other names (for example
  ``HUBSPOT_PRIVATE_APP_TOKEN``) maps them into ``.env`` under the names below, so a
  process, and in particular a test that removes exactly these names, can never pick
  up a credential it did not ask for.
* A blank value (``GOOGLE_ADS_CUSTOMER_ID=`` or whitespace only) counts as unset, so a
  typed field falls back to its documented default instead of failing to parse.
* Secrets are :class:`pydantic.SecretStr`, so ``repr()``, ``str()`` and validation
  errors never show them. No value here is ever shipped to the browser bundle.
* Loading performs no network or database I/O, and importing this module reads no
  environment variable.

API versions:
    :data:`GOOGLE_ADS_API_VERSION`, :data:`LINKEDIN_API_VERSION` and
    :data:`HUBSPOT_API_VERSION` pin each source's external API version. They are code
    constants, not environment variables and not :class:`Settings` fields: the documented
    variables include no version, and only those variables are read. A version move is a
    code change that ships with the contract files in ``backend/tests/contracts/``.

Example:
    A process entry point loads the cached settings once and checks what it needs::

        settings = get_settings()
        settings.check_api_startup()  # the API refuses to start on unsafe config
        reason = None if settings.has_credentials("google") else "credentials not configured"
"""

import functools
import re
from typing import Annotated, Final, Literal, assert_never
from urllib.parse import SplitResult, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

__all__ = [
    "GOOGLE_ADS_API_VERSION",
    "HUBSPOT_API_VERSION",
    "LINKEDIN_API_VERSION",
    "SOURCES",
    "VALIDATOR_MESSAGES",
    "ConfigurationError",
    "DataMode",
    "Settings",
    "Source",
    "get_settings",
]

Source = Literal["google", "linkedin", "hubspot"]
"""An external data source. Must equal the values of ``ads_engine.db.models.ops.SOURCES``.

This module never imports ``ads_engine.db``, because ``db/session.py`` imports settings;
the equality is therefore kept by convention and asserted by the tests.
"""

SOURCES: Final[tuple[Source, ...]] = ("google", "linkedin", "hubspot")
"""Every source, in display order. Must equal ``ads_engine.db.models.ops.SOURCES``."""

GOOGLE_ADS_API_VERSION: Final = "v25"
"""The Google Ads API version of the n8n relay contract, as declarative metadata only.

n8n's Google Ads node runs each query as ``googleAds:search`` on this version. The product
never sends it: the webhook body is exactly ``{customer_id, query, page_token}``, and the
product holds no Google credential and runs no Google OAuth.
"""

LINKEDIN_API_VERSION: Final = "202609"
"""The LinkedIn Marketing API version, sent as ``Linkedin-Version`` on every request."""

HUBSPOT_API_VERSION: Final = "2026-09"
"""The HubSpot CRM API version, the dated path segment of every surface the product calls.

For example ``/crm/objects/2026-09/contacts/search``.
"""

DataMode = Literal["auto", "simulated"]
"""``DATA_MODE``: ``auto`` falls back per source; ``simulated`` forces every source."""

# Field names whose environment variables make up each source's credentials
# (the "Access unavailable" row of the fallback table). The env name is the upper-case
# field name.
_GOOGLE_CREDENTIAL_FIELDS: Final[tuple[str, ...]] = (
    "n8n_google_ads_webhook_url",
    "n8n_webhook_secret",
    "google_ads_customer_id",
)
_LINKEDIN_REFRESH_FIELDS: Final[tuple[str, ...]] = (
    "linkedin_client_id",
    "linkedin_client_secret",
    "linkedin_refresh_token",
)
_LINKEDIN_STATIC_FIELD: Final = "linkedin_access_token"
_HUBSPOT_CREDENTIAL_FIELDS: Final[tuple[str, ...]] = ("hubspot_access_token",)

_HTTP_SCHEMES: Final = frozenset({"http", "https"})
# The only hosts on which APP_BASE_URL (local default http://localhost:8080) and
# N8N_GOOGLE_ADS_WEBHOOK_URL may use plain http; every other host requires https.
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
_LINKEDIN_ACCOUNT_URN_PREFIX: Final = "urn:li:sponsoredAccount:"
_DIGITS_RE: Final = re.compile(r"[0-9]+")
_CUSTOMER_ID_RE: Final = re.compile(r"[0-9]{10}")
# RFC 7230 "token" (field-name) characters: ^[!#$%&'*+.^_`|~0-9A-Za-z-]+$, used with fullmatch.
_HEADER_TOKEN_RE: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

_UNSAFE_BASE_URL_MESSAGE: Final = (
    "APP_BASE_URL must use https:// unless it is a localhost URL; "
    "terminate TLS in front of nginx (see docs/runbook.md)"
)
_SESSION_KEY_UNSET_MESSAGE: Final = "SESSION_SECRET is not set; run make init"

# The messages the Settings validators raise. Each is fixed text that never quotes the value
# it rejects: a failed load is reported before the environment's secrets are known to the
# log redactor, so a value written into a message would reach the log unredacted.
_ACCOUNT_IDS_TYPE_MESSAGE: Final = (
    "LINKEDIN_AD_ACCOUNT_IDS must be a comma-separated list of ad account ids"
)
_ACCOUNT_ID_ENTRY_MESSAGE: Final = (
    "LINKEDIN_AD_ACCOUNT_IDS entries must be numeric ad account ids or "
    f"{_LINKEDIN_ACCOUNT_URN_PREFIX}<digits>"
)
_CUSTOMER_ID_MESSAGE: Final = "GOOGLE_ADS_CUSTOMER_ID must be 10 digits"
_DISPLAY_TIMEZONE_MESSAGE: Final = (
    "DISPLAY_TIMEZONE must be an IANA time zone name such as UTC or America/New_York"
)
_APP_BASE_URL_MESSAGE: Final = (
    "APP_BASE_URL must be an absolute http:// or https:// URL with a host, "
    "such as http://localhost:8080"
)
_WEBHOOK_URL_FORM_MESSAGE: Final = (
    "N8N_GOOGLE_ADS_WEBHOOK_URL must be an absolute https:// URL "
    "(plain http:// only on localhost or 127.0.0.1)"
)
_WEBHOOK_URL_PLAIN_HTTP_MESSAGE: Final = (
    "N8N_GOOGLE_ADS_WEBHOOK_URL must use https:// unless it is a localhost or 127.0.0.1 URL"
)
_WEBHOOK_HEADER_NAME_MESSAGE: Final = (
    "N8N_WEBHOOK_SECRET_HEADER must be an HTTP header name made of RFC 7230 "
    "token characters, such as X-Webhook-Secret"
)
_ADMIN_EMAIL_MESSAGE: Final = "APP_ADMIN_EMAIL must be an email address such as admin@example.com"

VALIDATOR_MESSAGES: Final[frozenset[str]] = frozenset(
    {
        _ACCOUNT_IDS_TYPE_MESSAGE,
        _ACCOUNT_ID_ENTRY_MESSAGE,
        _CUSTOMER_ID_MESSAGE,
        _DISPLAY_TIMEZONE_MESSAGE,
        _APP_BASE_URL_MESSAGE,
        _WEBHOOK_URL_FORM_MESSAGE,
        _WEBHOOK_URL_PLAIN_HTTP_MESSAGE,
        _WEBHOOK_HEADER_NAME_MESSAGE,
        _ADMIN_EMAIL_MESSAGE,
    }
)
"""The closed set of messages the :class:`Settings` validators raise.

None holds the value it rejects: each names the environment variable and the accepted form,
with no part of the input. A process that reports a :class:`pydantic.ValidationError` from
:class:`Settings` may therefore show a ``value_error`` whose message is in this set, and
withhold any other, which did not come from these validators and may quote its input.

Example::

    message = str(error["ctx"]["error"])  # one entry of ValidationError.errors()
    shown = message if message in VALIDATOR_MESSAGES else "message withheld"
"""


class ConfigurationError(RuntimeError):
    """Configuration that loads but is unsafe or incomplete for the process using it.

    Raised by :meth:`Settings.assert_safe_base_url` and :meth:`Settings.check_api_startup`.
    Parse-level problems (a malformed customer id, an unknown time zone) surface earlier,
    as :class:`pydantic.ValidationError` from :class:`Settings`.

    Example::

        try:
            settings.check_api_startup()
        except ConfigurationError as exc:
            exc.fields  # ("session_secret",) when SESSION_SECRET is unset

    Attributes:
        fields: The :class:`Settings` fields at fault, by name and never by value, in the
            form of a :class:`pydantic.ValidationError` ``loc``; each field reads the
            environment variable of its upper-case name. Empty when the raiser names none.
    """

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()) -> None:
        """Create the error.

        Args:
            message: What is wrong and how to fix it.
            fields: The names of the :class:`Settings` fields at fault.
        """
        super().__init__(message)
        self.fields = fields


def _split_http_url(value: str) -> SplitResult | None:
    """Split an absolute ``http://`` or ``https://`` URL.

    Args:
        value: The candidate URL, already stripped of surrounding whitespace.

    Returns:
        The URL's parts, or ``None`` when the value has another scheme, no host, or a
        malformed or zero port.
    """
    try:
        parts = urlsplit(value)
        port = parts.port  # reading .port raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme not in _HTTP_SCHEMES or not parts.hostname or port == 0:
        return None
    return parts


class Settings(BaseSettings):
    """Process configuration read from environment variables.

    Each attribute reads the environment variable of the same name in upper case.
    Defaults are the documented safe local values; every secret defaults to unset.

    Attributes:
        database_url: ``DATABASE_URL``, the SQLAlchemy URL (``postgresql+psycopg://``).
            Secret, because it carries the database password.
        app_base_url: ``APP_BASE_URL``, the public base URL, stored without a trailing
            ``/``. It decides whether session cookies are Secure.
        display_timezone: ``DISPLAY_TIMEZONE``, the IANA zone used only to format
            instants. It never moves a source's day boundary.
        data_mode: ``DATA_MODE``, ``auto`` or ``simulated``.
        mock_seed: ``MOCK_SEED``, the seed of the simulated world.
        backfill_days: ``BACKFILL_DAYS``, the initial backfill length per dataset; the
            sync jobs cap it at 180 days (LinkedIn's 6-month daily retention) for every
            source.
        n8n_google_ads_webhook_url: ``N8N_GOOGLE_ADS_WEBHOOK_URL``, the n8n webhook that
            relays ``googleAds:search``. Must be ``https://``; plain ``http://`` loads only
            for a ``localhost`` or ``127.0.0.1`` host. Never logged.
        n8n_webhook_secret_header: ``N8N_WEBHOOK_SECRET_HEADER``, the header name the
            n8n Header Auth credential expects.
        n8n_webhook_secret: ``N8N_WEBHOOK_SECRET``, the shared secret sent in that header.
        google_ads_customer_id: ``GOOGLE_ADS_CUSTOMER_ID``, normalised to 10 digits with
            no dashes, as the webhook body's ``customer_id`` requires.
        google_daily_call_budget: ``GOOGLE_DAILY_CALL_BUDGET``, the product's own cap on
            webhook calls per sliding 24 h.
        linkedin_ad_account_ids: ``LINKEDIN_AD_ACCOUNT_IDS``, a comma-separated list of
            ad account ids or ``urn:li:sponsoredAccount:{id}`` URNs, normalised to ids.
        linkedin_client_id: ``LINKEDIN_CLIENT_ID`` of the refresh-token trio.
        linkedin_client_secret: ``LINKEDIN_CLIENT_SECRET`` of the refresh-token trio.
        linkedin_refresh_token: ``LINKEDIN_REFRESH_TOKEN`` of the refresh-token trio.
        linkedin_access_token: ``LINKEDIN_ACCESS_TOKEN``, a static alternative to the trio.
        hubspot_access_token: ``HUBSPOT_ACCESS_TOKEN``, a Service Key or private-app token.
        hubspot_google_campaign_property: ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY``, the contact
            property holding a Google campaign id.
        hubspot_google_touch_date_property: ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY``, the
            contact datetime property holding when that Google touch happened.
        app_admin_email: ``APP_ADMIN_EMAIL``, the bootstrap admin identity, lowercased.
        app_admin_password_hash: ``APP_ADMIN_PASSWORD_HASH``, the argon2 hash written by
            ``make init``.
        session_secret: ``SESSION_SECRET``, the key that signs session cookies.
    """

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        env_file=None,
        extra="ignore",
        case_sensitive=False,
        frozen=True,
        # Beyond the documented options: a validation error never echoes the input, so
        # a malformed secret or the webhook URL cannot reach a log through the error.
        hide_input_in_errors=True,
    )

    # Runtime
    database_url: SecretStr = SecretStr("postgresql+psycopg://ads:ads@db:5432/ads_engine")
    app_base_url: str = "http://localhost:8080"
    display_timezone: str = "UTC"
    data_mode: DataMode = "auto"
    mock_seed: int = 42
    backfill_days: int = Field(default=180, ge=1)

    # Google Ads, reached only through the n8n webhook
    n8n_google_ads_webhook_url: str | None = None
    # The plan's documented header name. A deployment whose n8n Header Auth credential
    # uses another name (for example X-Webhook-Token) sets N8N_WEBHOOK_SECRET_HEADER.
    n8n_webhook_secret_header: str = "X-Webhook-Secret"  # noqa: S105 - a header name, not a secret
    n8n_webhook_secret: SecretStr | None = None
    google_ads_customer_id: str | None = None
    google_daily_call_budget: int = Field(default=1000, ge=1)

    # LinkedIn Marketing API
    linkedin_ad_account_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    linkedin_client_id: str | None = None
    linkedin_client_secret: SecretStr | None = None
    linkedin_refresh_token: SecretStr | None = None
    linkedin_access_token: SecretStr | None = None

    # HubSpot CRM
    hubspot_access_token: SecretStr | None = None
    hubspot_google_campaign_property: str | None = None
    hubspot_google_touch_date_property: str | None = None

    # Admin and session
    app_admin_email: str = "admin@example.com"
    app_admin_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat a blank value as unset, so the field takes its documented default.

        ``env_ignore_empty`` already drops empty environment values; this also covers
        whitespace-only values and blank constructor arguments.

        Raises:
            PydanticUseDefault: When the value is a blank string or a blank secret.
        """
        if isinstance(value, str) and not value.strip():
            raise PydanticUseDefault
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            raise PydanticUseDefault
        return value

    @field_validator(
        "linkedin_client_id",
        "hubspot_google_campaign_property",
        "hubspot_google_touch_date_property",
        mode="before",
    )
    @classmethod
    def _strip_identifier(cls, value: object) -> object:
        """Strip surrounding whitespace from identifiers and property names."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("data_mode", mode="before")
    @classmethod
    def _normalise_data_mode(cls, value: object) -> object:
        """Accept ``DATA_MODE`` case-insensitively, for example ``Simulated``."""
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("linkedin_ad_account_ids", mode="before")
    @classmethod
    def _parse_linkedin_account_ids(cls, value: object) -> list[str]:
        """Parse ``LINKEDIN_AD_ACCOUNT_IDS`` into de-duplicated numeric account ids.

        Accepts a comma-separated string (the environment form) or a list. Each entry is
        a numeric id or ``urn:li:sponsoredAccount:{id}``; blank entries are dropped and
        the first occurrence of each id keeps its position.

        Raises:
            ValueError: When an entry is neither form, or the value is not a string or list.
        """
        if value is None:
            return []
        if isinstance(value, str):
            raw_items: list[str] = value.split(",")
        elif isinstance(value, list | tuple):
            raw_items = [str(item) for item in value]
        else:
            raise ValueError(_ACCOUNT_IDS_TYPE_MESSAGE)
        account_ids: list[str] = []
        for raw in raw_items:
            entry = raw.strip()
            if not entry:
                continue
            account_id = entry.removeprefix(_LINKEDIN_ACCOUNT_URN_PREFIX)
            if not _DIGITS_RE.fullmatch(account_id):
                # The entry is not quoted: a mis-pasted token would reach the log with it.
                raise ValueError(_ACCOUNT_ID_ENTRY_MESSAGE)
            if account_id not in account_ids:
                account_ids.append(account_id)
        return account_ids

    @field_validator("google_ads_customer_id", mode="before")
    @classmethod
    def _normalise_customer_id(cls, value: object) -> object:
        """Normalise ``GOOGLE_ADS_CUSTOMER_ID`` to exactly 10 digits with no dashes.

        Raises:
            ValueError: When the value is not 10 digits after removing dashes.
        """
        if not isinstance(value, str) or not value.strip():
            return value  # blank: left to _blank_is_unset
        customer_id = value.strip().replace("-", "")
        if not _CUSTOMER_ID_RE.fullmatch(customer_id):
            raise ValueError(_CUSTOMER_ID_MESSAGE)
        return customer_id

    @field_validator("display_timezone", mode="after")
    @classmethod
    def _validate_display_timezone(cls, value: str) -> str:
        """Require ``DISPLAY_TIMEZONE`` to name a zone the tz database can load.

        Raises:
            ValueError: When :class:`zoneinfo.ZoneInfo` cannot load the name.
        """
        name = value.strip()
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError, OSError:
            # The name is not quoted: a value pasted into the wrong variable would reach the log.
            raise ValueError(_DISPLAY_TIMEZONE_MESSAGE) from None
        return name

    @field_validator("app_base_url", mode="after")
    @classmethod
    def _validate_app_base_url(cls, value: str) -> str:
        """Require an absolute ``http(s)`` ``APP_BASE_URL`` and drop trailing slashes.

        The localhost-only rule for plain ``http`` is enforced by
        :meth:`Settings.assert_safe_base_url`, so the CLI and worker load on any URL and
        only the API refuses to start.

        Raises:
            ValueError: When the scheme is not ``http``/``https`` or the host is missing.
        """
        candidate = value.strip().rstrip("/")
        parts = _split_http_url(candidate)
        if parts is None:
            raise ValueError(_APP_BASE_URL_MESSAGE)
        # urlsplit lowercases the scheme; store it lowercased so prefix checks hold.
        return parts.scheme + candidate[len(parts.scheme) :]

    @field_validator("n8n_google_ads_webhook_url", mode="before")
    @classmethod
    def _validate_webhook_url(cls, value: object) -> object:
        """Require an absolute ``https://`` webhook URL when one is set.

        The request carries the n8n shared-secret header, so plain ``http://`` is
        accepted only when the host is exactly ``localhost`` or ``127.0.0.1``. The error
        message never contains the URL, which identifies the workflow.

        Raises:
            ValueError: When the value is not an absolute ``http(s)`` URL with a host, or
                is plain ``http://`` on a host other than ``localhost`` or ``127.0.0.1``.
        """
        if not isinstance(value, str) or not value.strip():
            return value  # blank: left to _blank_is_unset
        candidate = value.strip()
        parts = _split_http_url(candidate)
        if parts is None:
            raise ValueError(_WEBHOOK_URL_FORM_MESSAGE)
        # urlsplit lowercases the scheme and host, so HTTP://LOCALHOST counts as local.
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            raise ValueError(_WEBHOOK_URL_PLAIN_HTTP_MESSAGE)
        return candidate

    @field_validator("n8n_webhook_secret_header", mode="after")
    @classmethod
    def _validate_secret_header(cls, value: str) -> str:
        """Require ``N8N_WEBHOOK_SECRET_HEADER`` to be a valid HTTP header name.

        Raises:
            ValueError: When the name has characters outside the RFC 7230 token set.
        """
        name = value.strip()
        if not _HEADER_TOKEN_RE.fullmatch(name):
            raise ValueError(_WEBHOOK_HEADER_NAME_MESSAGE)
        return name

    @field_validator("app_admin_email", mode="after")
    @classmethod
    def _normalise_admin_email(cls, value: str) -> str:
        """Strip and lowercase ``APP_ADMIN_EMAIL`` and require an address shape.

        Raises:
            ValueError: When the value has no ``local@domain`` shape or holds whitespace.
        """
        email = value.strip().lower()
        local, separator, domain = email.rpartition("@")
        if not separator or not local or not domain or any(ch.isspace() for ch in email):
            raise ValueError(_ADMIN_EMAIL_MESSAGE)
        return email

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    @property
    def display_tz(self) -> ZoneInfo:
        """The :class:`zoneinfo.ZoneInfo` of ``DISPLAY_TIMEZONE``.

        It only formats instants for display; it never moves a source's native day
        boundary (Google: the customer account's zone; LinkedIn: UTC).
        """
        return ZoneInfo(self.display_timezone)

    @property
    def secure_cookies(self) -> bool:
        """Whether session cookies carry ``Secure``: true for an ``https://`` base URL."""
        return self.app_base_url.startswith("https://")

    @property
    def linkedin_uses_refresh_token(self) -> bool:
        """Whether LinkedIn authenticates through the refresh-token trio.

        True when ``LINKEDIN_CLIENT_ID``, ``LINKEDIN_CLIENT_SECRET`` and
        ``LINKEDIN_REFRESH_TOKEN`` are all set. When the static ``LINKEDIN_ACCESS_TOKEN``
        is also set, the trio wins, because the token provider can refresh it.
        """
        return not self._missing_env_names(_LINKEDIN_REFRESH_FIELDS)

    @property
    def google_crm_link_configured(self) -> bool:
        """Whether both Google CRM-link contact properties are configured.

        True only when ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and
        ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` are both set. Their existence in the
        portal is verified against HubSpot's properties API elsewhere, at startup.
        """
        return not self._missing_env_names(
            ("hubspot_google_campaign_property", "hubspot_google_touch_date_property")
        )

    # ------------------------------------------------------------------
    # Credentials
    # ------------------------------------------------------------------

    def missing_credentials(self, source: Source) -> list[str]:
        """List the unset environment variables that keep ``source`` from running live.

        Follows the "credentials not configured" row of the source fallback table:

        * ``google``: ``N8N_GOOGLE_ADS_WEBHOOK_URL``, ``N8N_WEBHOOK_SECRET`` and
          ``GOOGLE_ADS_CUSTOMER_ID``.
        * ``linkedin``: ``LINKEDIN_AD_ACCOUNT_IDS``, plus either the refresh-token trio
          or ``LINKEDIN_ACCESS_TOKEN``. When neither alternative is complete, the trio's
          missing names are listed, followed by ``LINKEDIN_ACCESS_TOKEN``.
        * ``hubspot``: ``HUBSPOT_ACCESS_TOKEN``.

        A secret whose value is empty counts as missing.

        Args:
            source: The source to check.

        Returns:
            The missing variable names in a stable order; empty when the source is
            fully configured.
        """
        match source:
            case "google":
                return self._missing_env_names(_GOOGLE_CREDENTIAL_FIELDS)
            case "linkedin":
                missing: list[str] = []
                if not self.linkedin_ad_account_ids:
                    missing.append("LINKEDIN_AD_ACCOUNT_IDS")
                missing_trio = self._missing_env_names(_LINKEDIN_REFRESH_FIELDS)
                missing_static = self._missing_env_names((_LINKEDIN_STATIC_FIELD,))
                if missing_trio and missing_static:
                    missing.extend(missing_trio)
                    missing.extend(missing_static)
                return missing
            case "hubspot":
                return self._missing_env_names(_HUBSPOT_CREDENTIAL_FIELDS)
            case _:
                assert_never(source)

    def has_credentials(self, source: Source) -> bool:
        """Whether every credential ``source`` needs to run live is set.

        Args:
            source: The source to check.

        Returns:
            ``True`` when :meth:`missing_credentials` reports nothing.
        """
        return not self.missing_credentials(source)

    def secret_values(self) -> tuple[str, ...]:
        """Return the plaintext of every set secret, for log redaction.

        Returns:
            Each non-empty :class:`~pydantic.SecretStr` value once, in field order.
        """
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                plaintext = value.get_secret_value()
                if plaintext and plaintext not in values:
                    values.append(plaintext)
        return tuple(values)

    def _missing_env_names(self, field_names: tuple[str, ...]) -> list[str]:
        """Return the environment names of the fields in ``field_names`` that are unset.

        A field is unset when it is ``None`` or a blank string or secret.
        """
        missing: list[str] = []
        for name in field_names:
            value: object = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name.upper())
        return missing

    # ------------------------------------------------------------------
    # Startup checks
    # ------------------------------------------------------------------

    def assert_safe_base_url(self) -> None:
        """Refuse a plain-``http`` ``APP_BASE_URL`` on any host other than localhost.

        ``https://`` passes for every host. ``http://`` passes only when the host is
        exactly ``localhost`` or ``127.0.0.1``, the local default; any other deployment
        must terminate TLS in front of nginx.

        Raises:
            ConfigurationError: When the URL is plain ``http`` on a non-local host, with
                ``fields`` ``("app_base_url",)``.
        """
        parts = urlsplit(self.app_base_url)
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            raise ConfigurationError(_UNSAFE_BASE_URL_MESSAGE, fields=("app_base_url",))

    def check_api_startup(self) -> None:
        """Check what the API needs before it serves a request.

        ``api/app.py:create_app()`` calls this, so ``uvicorn`` refuses to start on an
        unsafe base URL or without a session-signing key.

        Raises:
            ConfigurationError: When :meth:`assert_safe_base_url` fails, or
                ``SESSION_SECRET`` is not set (``fields`` ``("session_secret",)``).
        """
        self.assert_safe_base_url()
        if self._missing_env_names(("session_secret",)):
            raise ConfigurationError(_SESSION_KEY_UNSET_MESSAGE, fields=("session_secret",))


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded from the environment on first call.

    The result is cached; tests that change the environment call
    ``get_settings.cache_clear()`` before the next call.

    Returns:
        The loaded :class:`Settings`.

    Raises:
        pydantic.ValidationError: When an environment value cannot be parsed.
    """
    return Settings()
