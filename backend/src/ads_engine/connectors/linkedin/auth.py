"""In-memory LinkedIn access-token providers.

Every LinkedIn Marketing API request carries ``Authorization: Bearer <access token>``. This
module supplies that value through the :class:`TokenSource` protocol, in one of two ways:

* :class:`LinkedInTokenProvider` reads the server-side credentials from
  :class:`~ads_engine.settings.Settings`. With ``LINKEDIN_CLIENT_ID``,
  ``LINKEDIN_CLIENT_SECRET`` and ``LINKEDIN_REFRESH_TOKEN`` all set it exchanges the
  refresh token for an access token at LinkedIn's OAuth endpoint (:data:`TOKEN_URL`) and
  caches the result in memory until shortly before it expires. Otherwise it serves the
  static ``LINKEDIN_ACCESS_TOKEN``.
* :class:`StaticTokenProvider` serves a fixed value. ``ads_engine.sync`` uses
  ``StaticTokenProvider(SIMULATED_TOKEN)`` while LinkedIn is simulated, where the
  simulation's mock transport answers every call.

The product has no OAuth consent UI and runs no authorization-code flow. The refresh token
(or the static access token) is bootstrapped once with the LinkedIn Developer Portal token
generator, as ``docs/runbook.md`` describes, and granted these read-only scopes:

* ``r_ads``: ad accounts, campaign groups, campaigns and creatives;
* ``r_ads_reporting``: ``adAnalytics``;
* ``r_marketing_leadgen_automation``: lead forms and lead-form responses (the Lead Sync
  API product; ``r_ads`` cannot read leads).

The client's ``get_posts`` also needs post read access for creative copy (see
``connectors/linkedin/client.py``).

Tokens are never persisted, logged or returned anywhere but :meth:`TokenSource.get`.
Secrets stay :class:`~pydantic.SecretStr` and are revealed only while the token request is
built. A refresh token that LinkedIn rotates in an exchange response replaces the
configured one in memory only, so a restarted process starts again from
``LINKEDIN_REFRESH_TOKEN``.

Error handling follows the source fallback table. The token endpoint answers OAuth 2.0
errors (``{"error": ..., "error_description": ...}``) under HTTP 400 or 401, and this
module reads them itself:

* ``invalid_grant``, ``invalid_client``, ``unauthorized_client``, any 401, and LinkedIn's
  400 ``invalid_request`` whose description says the grant or token is expired or revoked
  are :class:`~ads_engine.connectors.errors.AccessDenied`: revoked or expired credentials
  send LinkedIn to simulated data instead of failing every job as a defect;
* every other 400, and a success body without a usable token, is a
  :class:`~ads_engine.connectors.errors.QueryDefect`;
* 403, 429, 5xx, timeouts and network failures are classified by
  :func:`~ads_engine.connectors.http.send_json`; the transient ones are retried with the
  shared backoff policy and end as ``TransientError``.

The "401 after one token refresh" rule belongs to ``LinkedInClient``: on a 401 it calls
:meth:`TokenSource.invalidate` when :attr:`TokenSource.can_refresh` is true, retries once
with a fresh token, and lets a second 401 propagate as access denied.

Example:
    The worker builds one provider per LinkedIn client and shares its HTTP client::

        async with build_client("linkedin") as http:
            tokens = LinkedInTokenProvider(get_settings(), http)
            client = LinkedInClient(http, tokens)
"""

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import httpx
from pydantic import SecretStr

from ads_engine.connectors.errors import AccessDenied, QueryDefect, sanitize_message
from ads_engine.connectors.http import DEFAULT_RETRY, JsonResponse, RetryPolicy, send_json
from ads_engine.logging import REDACTED, get_logger
from ads_engine.settings import Settings

__all__ = [
    "ACCESS_DENIED_OAUTH_ERRORS",
    "EXPIRY_MARGIN_SECONDS",
    "SIMULATED_TOKEN",
    "TOKEN_SURFACE",
    "TOKEN_URL",
    "LinkedInTokenProvider",
    "StaticTokenProvider",
    "TokenMode",
    "TokenSource",
]

# The names below hold a URL, a contract surface name and a placeholder bearer value for
# simulated mode, never a credential; S105 matches them only by the word "token".
SIMULATED_TOKEN: Final = "simulated"  # noqa: S105 - placeholder bearer value, not a secret
"""Bearer value used while LinkedIn is simulated; the mock transport ignores it."""

TOKEN_URL: Final = "https://www.linkedin.com/oauth/v2/accessToken"  # noqa: S105 - a URL
"""LinkedIn's OAuth 2.0 token endpoint.

It is absolute, so it overrides the ``https://api.linkedin.com/rest/`` base URL of the
LinkedIn client from :func:`~ads_engine.connectors.http.build_client`. It is unversioned and
takes no ``Linkedin-Version`` or ``X-Restli-Protocol-Version`` header.
"""

TOKEN_SURFACE: Final = "access_token"  # noqa: S105 - a contract surface name
"""Endpoint name of the exchange in errors and logs.

It matches the contract file ``backend/tests/contracts/linkedin/access_token.json``.
"""

EXPIRY_MARGIN_SECONDS: Final = 300
"""Seconds before an access token's expiry at which the cached token is refreshed.

A token whose ``expires_in`` is at most this margin is returned without being cached.
"""

ACCESS_DENIED_OAUTH_ERRORS: Final = frozenset(
    {"invalid_grant", "invalid_client", "unauthorized_client"}
)
"""OAuth 2.0 error codes (RFC 6749 section 5.2) that reject the credentials themselves."""

TokenMode = Literal["refresh", "static"]
"""How a :class:`LinkedInTokenProvider` obtains its token."""

# LinkedIn reports an expired or revoked refresh token as 400 `invalid_request` with "The
# provided authorization grant or refresh token is invalid, expired or revoked"; the
# contract file classifies that as a credential rejection, as connectors/errors.py does.
_REVOKED_ERROR: Final = "invalid_request"
_REVOKED_DESCRIPTION_RE: Final = re.compile(r"\b(?:expired|revoked)\b", re.IGNORECASE)

# Headers of the REST surfaces that must never reach the OAuth endpoint, whatever default
# headers the injected client carries: the exchange authenticates with the client secret
# in the form body, and the endpoint is unversioned.
_FORBIDDEN_TOKEN_HEADERS: Final[tuple[str, ...]] = (
    "Authorization",
    "Linkedin-Version",
    "X-Restli-Protocol-Version",
)

# Statuses whose OAuth error body this module reads itself; send_json classifies the rest.
_ACCEPTED_STATUSES: Final[tuple[int, ...]] = (200, 400, 401)

_log = get_logger(__name__)


@dataclass(slots=True, repr=False, eq=False)
class _RefreshCredentials:
    """The refresh-token trio, held in memory only.

    ``refresh_token`` is replaced when an exchange response rotates it. ``repr`` is the
    default object form, so no field can reach a traceback or a log.
    """

    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr


class TokenSource(Protocol):
    """Supplies the bearer value of LinkedIn requests.

    ``LinkedInClient`` depends only on this protocol, so live, static and simulated
    credentials take one code path. On a 401 the client calls :meth:`invalidate` and
    retries once with :meth:`get` when :attr:`can_refresh` is true; otherwise, or on a
    second 401, the 401 propagates as access denied.
    """

    @property
    def can_refresh(self) -> bool:
        """Whether :meth:`get` can obtain a new token after :meth:`invalidate`."""

    async def get(self) -> str:
        """Return the bearer value to send as ``Authorization: Bearer <value>``.

        Raises:
            AccessDenied: LinkedIn rejected the credentials while refreshing.
            TransientError: A refresh failed transiently on every attempt.
            QueryDefect: A refresh request or response was malformed.
        """

    def invalidate(self) -> None:
        """Drop any cached access token, so the next :meth:`get` refreshes it."""


class StaticTokenProvider:
    """A :class:`TokenSource` serving one fixed bearer value.

    It never refreshes: :attr:`can_refresh` is ``False`` and :meth:`invalidate` does
    nothing, so a 401 propagates at once as access denied. Its ``repr`` never shows the
    value.

    Example::

        tokens = StaticTokenProvider(SIMULATED_TOKEN)
        client = LinkedInClient(http, tokens)  # http sends through the mock transport
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        """Hold ``token`` in memory as a secret.

        Args:
            token: The bearer value, such as :data:`SIMULATED_TOKEN`.

        Raises:
            ValueError: When ``token`` is blank.
        """
        if not token.strip():
            raise ValueError("a static LinkedIn token must not be blank")
        self._token = SecretStr(token)

    @property
    def can_refresh(self) -> bool:
        """Always ``False``: a static token cannot be refreshed."""
        return False

    async def get(self) -> str:
        """Return the fixed bearer value."""
        return self._token.get_secret_value()

    def invalidate(self) -> None:
        """Do nothing: there is no cached token to drop."""

    def __repr__(self) -> str:
        """Return the class name only; the token is never shown."""
        return f"{type(self).__name__}()"


class LinkedInTokenProvider:
    """The live :class:`TokenSource`: a refresh-token exchange or a static access token.

    **Refresh mode** applies when ``LINKEDIN_CLIENT_ID``, ``LINKEDIN_CLIENT_SECRET`` and
    ``LINKEDIN_REFRESH_TOKEN`` are all set (``Settings.linkedin_uses_refresh_token``; the
    trio wins over a static token also set). :meth:`get` exchanges the refresh token for an
    access token with one form-encoded POST to :data:`TOKEN_URL` and caches it in memory
    until :data:`EXPIRY_MARGIN_SECONDS` before it expires, measured on ``clock``. An
    :class:`asyncio.Lock` serialises exchanges, so concurrent callers that find no valid
    token share one exchange. :meth:`invalidate` drops the cached token.

    **Static mode** applies otherwise, when ``LINKEDIN_ACCESS_TOKEN`` is set: :meth:`get`
    returns it without any network call, and :attr:`can_refresh` is ``False``.

    Credentials are bootstrapped once with the Developer Portal token generator
    (``docs/runbook.md``); there is no consent UI. The provider is meant for one event
    loop, the worker's or the CLI's; it is not thread-safe.

    Example::

        async with build_client("linkedin") as http:
            tokens = LinkedInTokenProvider(settings, http)
            bearer = await tokens.get()
    """

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        retry: RetryPolicy = DEFAULT_RETRY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Read the LinkedIn credentials from ``settings`` and choose the mode.

        Args:
            settings: The process settings holding the LinkedIn credentials.
            http: The ``build_client("linkedin", transport)`` client the exchange is sent
                through; :data:`TOKEN_URL` is absolute and overrides its base URL. The
                caller owns its lifecycle.
            retry: Retry policy of the exchange, the shared policy by default.
            clock: Monotonic clock, in seconds, that measures token expiry.

        Raises:
            ValueError: When neither the refresh-token trio nor a static access token is
                configured. Source-mode resolution has already sent such a source to
                simulated data with the reason "credentials not configured".
        """
        self._http = http
        self._retry = retry
        self._clock = clock
        self._lock = asyncio.Lock()
        # The cached access token and the clock() instant from which it must be refreshed.
        self._access_token: SecretStr | None = None
        self._refresh_at: float | None = None
        # Refresh mode holds the trio; static mode holds the configured access token.
        self._credentials: _RefreshCredentials | SecretStr

        client_id = settings.linkedin_client_id
        client_secret = settings.linkedin_client_secret
        refresh_token = settings.linkedin_refresh_token
        if (
            settings.linkedin_uses_refresh_token
            and client_id
            and client_secret is not None
            and refresh_token is not None
        ):
            self._credentials = _RefreshCredentials(client_id, client_secret, refresh_token)
        elif settings.linkedin_access_token is not None:
            self._credentials = settings.linkedin_access_token
        else:
            raise ValueError("LinkedIn credentials not configured")

    @property
    def mode(self) -> TokenMode:
        """``refresh`` for the refresh-token exchange, ``static`` for a static token."""
        return "static" if isinstance(self._credentials, SecretStr) else "refresh"

    @property
    def can_refresh(self) -> bool:
        """Whether :meth:`get` exchanges the refresh token again after :meth:`invalidate`."""
        return self.mode == "refresh"

    async def get(self) -> str:
        """Return a valid access token, exchanging the refresh token when needed.

        In static mode the configured token is returned with no network call. In refresh
        mode the cached token is returned while ``clock()`` is before its refresh instant;
        otherwise one caller exchanges the refresh token under the lock and every caller
        waiting on it reuses the result.

        Returns:
            The bearer value.

        Raises:
            AccessDenied: LinkedIn rejected the credentials (``invalid_grant``,
                ``invalid_client``, ``unauthorized_client``, an expired or revoked refresh
                token, HTTP 401 or 403).
            TransientError: The exchange failed with 429, 5xx, a timeout or a network
                failure on every attempt.
            QueryDefect: LinkedIn rejected the request as malformed (another HTTP 400), or
                the success body carried no usable ``access_token`` or ``expires_in``.
        """
        credentials = self._credentials
        if isinstance(credentials, SecretStr):
            return credentials.get_secret_value()
        cached = self._cached_token()
        if cached is not None:
            return cached
        async with self._lock:
            # Another caller may have refreshed while this one waited for the lock.
            cached = self._cached_token()
            if cached is not None:
                return cached
            return await self._exchange(credentials)

    def invalidate(self) -> None:
        """Drop the cached access token; the next :meth:`get` exchanges again.

        A no-op in static mode, which caches nothing and has nothing to refresh.
        """
        self._access_token = None
        self._refresh_at = None

    def __repr__(self) -> str:
        """Return the mode only; no credential or token is ever shown."""
        return f"{type(self).__name__}(mode={self.mode!r})"

    # ------------------------------------------------------------------
    # Exchange
    # ------------------------------------------------------------------

    def _cached_token(self) -> str | None:
        """Return the cached access token before its refresh instant, else ``None``."""
        token = self._access_token
        refresh_at = self._refresh_at
        if token is None or refresh_at is None or self._clock() >= refresh_at:
            return None
        return token.get_secret_value()

    def _held_secrets(self, credentials: _RefreshCredentials) -> tuple[str, ...]:
        """Return the plaintexts held in memory that an error text must never carry.

        ``sanitize_message`` strips the configured secrets; a rotated refresh token and an
        exchanged access token exist only here, so they are stripped explicitly as well.
        """
        held = [
            credentials.client_secret.get_secret_value(),
            credentials.refresh_token.get_secret_value(),
        ]
        if self._access_token is not None:
            held.append(self._access_token.get_secret_value())
        return tuple(held)

    async def _exchange(self, credentials: _RefreshCredentials) -> str:
        """Exchange the refresh token for an access token, cache it and return it.

        The request is built per attempt, so a retry uses the current refresh token. The
        expiry is measured from the instant before the first attempt is sent, never from
        the response: the token was issued no earlier than that, so the cached expiry can
        only fall before the real one.

        A 401 whose body is not JSON reaches this method as ``send_json``'s ``validation``
        defect, because 401 is an accepted status; any 401 rejects the credentials, so it
        is raised as :class:`AccessDenied` instead.
        """

        def build() -> httpx.Request:
            """Build one exchange request; the secrets are revealed only here.

            The body is exactly the contract's four form fields, which httpx form-encodes
            as ``application/x-www-form-urlencoded``. No REST header is sent, whatever
            default headers the injected client carries.
            """
            request = self._http.build_request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": credentials.refresh_token.get_secret_value(),
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret.get_secret_value(),
                },
            )
            for name in _FORBIDDEN_TOKEN_HEADERS:
                request.headers.pop(name, None)
            return request

        requested_at = self._clock()
        try:
            response = await send_json(
                self._http,
                build,
                source="linkedin",
                endpoint=TOKEN_SURFACE,
                policy=self._retry,
                accept_statuses=_ACCEPTED_STATUSES,
            )
        except QueryDefect as defect:
            if defect.http_status != 401:
                raise
            raise _logged(
                AccessDenied(
                    "LinkedIn token refresh rejected: HTTP 401 with a non-JSON body",
                    source="linkedin",
                    endpoint=TOKEN_SURFACE,
                    http_status=401,
                )
            ) from defect
        if response.status != 200:
            raise _rejection(response, secrets=self._held_secrets(credentials))
        return self._accept(response.body, credentials, requested_at)

    def _accept(self, body: object, credentials: _RefreshCredentials, requested_at: float) -> str:
        """Validate a 200 exchange body, cache its token and adopt a rotated refresh token.

        Args:
            body: The decoded success body.
            credentials: The trio whose refresh token a rotation replaces.
            requested_at: ``clock()`` just before the exchange was sent.

        Returns:
            The new access token.

        Raises:
            QueryDefect: With code ``validation`` when ``access_token`` is not a non-empty
                string or ``expires_in`` is not a positive integer.
        """
        if not isinstance(body, Mapping):
            raise _invalid_success("the token response is not a JSON object")
        token = _text_field(body, "access_token")
        if token is None:
            raise _invalid_success("the token response has no non-empty access_token")
        expires_in = _positive_int(body.get("expires_in"))
        if expires_in is None:
            raise _invalid_success("the token response has no positive integer expires_in")

        cached = expires_in > EXPIRY_MARGIN_SECONDS
        if cached:
            self._access_token = SecretStr(token)
            self._refresh_at = requested_at + expires_in - EXPIRY_MARGIN_SECONDS
        else:
            # A token this short-lived is returned once and never served from the cache;
            # LinkedIn's access tokens normally last about 60 days.
            self._access_token = None
            self._refresh_at = None

        rotated = _text_field(body, "refresh_token")
        refresh_rotated = (
            rotated is not None and rotated != credentials.refresh_token.get_secret_value()
        )
        if rotated is not None and refresh_rotated:
            # In memory only: never persisted, logged or returned.
            credentials.refresh_token = SecretStr(rotated)

        # Numbers and flags only, so operators see when to re-bootstrap (docs/runbook.md).
        _log.info(
            "linkedin.token.refreshed",
            expires_in=expires_in,
            refresh_token_expires_in=_positive_int(body.get("refresh_token_expires_in")),
            cached=cached,
            refresh_token_rotated=refresh_rotated,
        )
        return token


def _logged[E: AccessDenied | QueryDefect](error: E) -> E:
    """Log an error this module raises itself, as ``linkedin.token.failed``, and return it.

    ``send_json`` logs the errors it raises; these are the ones built from an accepted
    response. ``log_fields`` carry the kind, code, HTTP status and sanitised reason, never
    a body, a header or a secret.
    """
    _log.warning("linkedin.token.failed", **error.log_fields)
    return error


def _positive_int(value: object) -> int | None:
    """Return ``value`` when it is a positive JSON integer (never a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _invalid_success(message: str) -> QueryDefect:
    """Build and log the ``validation`` defect of a 200 body without a usable token."""
    return _logged(
        QueryDefect(
            message,
            source="linkedin",
            endpoint=TOKEN_SURFACE,
            code="validation",
            http_status=200,
        )
    )


def _text_field(body: object, key: str) -> str | None:
    """Return the stripped, non-blank string at ``body[key]``, else ``None``."""
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _error_code(body: object) -> str | None:
    """Read the error code of a rejected exchange.

    The OAuth 2.0 ``error`` comes first. A 401 from LinkedIn's gateway can instead carry
    its REST error shape, whose code is ``code`` or the numeric ``serviceErrorCode``.
    """
    code = _text_field(body, "error") or _text_field(body, "code")
    if code is not None:
        return code
    if isinstance(body, Mapping):
        service_code = body.get("serviceErrorCode")
        if isinstance(service_code, int) and not isinstance(service_code, bool):
            return str(service_code)
    return None


def _denies_credentials(status: int, code: str | None, description: str | None) -> bool:
    """Whether a rejected exchange refuses the credentials themselves.

    True for any 401, an ``error`` in :data:`ACCESS_DENIED_OAUTH_ERRORS`, and LinkedIn's
    ``invalid_request`` whose description says the grant or token is expired or revoked.
    """
    if status == 401:
        return True
    if code is None:
        return False
    normalised = code.lower()
    if normalised in ACCESS_DENIED_OAUTH_ERRORS:
        return True
    return (
        normalised == _REVOKED_ERROR
        and description is not None
        and _REVOKED_DESCRIPTION_RE.search(description) is not None
    )


def _strip_held(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every occurrence of the in-memory ``secrets`` in ``text`` with ``[REDACTED]``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def _rejection(response: JsonResponse, *, secrets: tuple[str, ...]) -> AccessDenied | QueryDefect:
    """Classify a 400 or 401 exchange response and log it.

    Credential rejections (:func:`_denies_credentials`) are :class:`AccessDenied`, so
    ``sync/mode.py`` sends LinkedIn to simulated data with the reason, for example
    ``"linkedin access_token: invalid_grant: LinkedIn token refresh rejected: invalid_grant"``.
    Every other 400 is a :class:`QueryDefect` carrying the OAuth ``error`` as its code.
    The code and ``error_description`` lose the held ``secrets`` first, then the
    description passes through :func:`~ads_engine.connectors.errors.sanitize_message`
    before it joins the message (the error's constructor sanitises the code and message
    once more).

    Args:
        response: The accepted 400 or 401 response.
        secrets: Plaintexts held only in memory (see ``_held_secrets``).
    """
    body = response.body
    status = response.status
    raw_code = _error_code(body)
    raw_description = _text_field(body, "error_description") or _text_field(body, "message")
    denied = _denies_credentials(status, raw_code, raw_description)
    code = _strip_held(raw_code, secrets) if raw_code is not None else None
    description = (
        sanitize_message(_strip_held(raw_description, secrets))
        if raw_description is not None
        else None
    )
    detail = code or f"HTTP {status}"
    error: AccessDenied | QueryDefect
    if denied:
        message = f"LinkedIn token refresh rejected: {detail}"
        if description:
            message = f"{message}: {description}"
        error = AccessDenied(
            message,
            source="linkedin",
            endpoint=TOKEN_SURFACE,
            code=code,
            http_status=status,
        )
    else:
        message = f"LinkedIn token refresh failed: {detail}"
        if description:
            message = f"{message}: {description}"
        error = QueryDefect(
            message,
            source="linkedin",
            endpoint=TOKEN_SURFACE,
            code=code,
            http_status=status,
        )
    return _logged(error)
