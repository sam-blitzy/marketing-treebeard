"""Shared HTTP layer of every connector: client factory, retry policy and redacted logging.

The Google (n8n webhook), LinkedIn and HubSpot connectors all talk to their platform
through the two entry points of this module:

* :func:`build_client` returns the ``httpx.AsyncClient`` of a source, with that source's
  base URL, per-phase timeouts (:func:`send_json` also bounds each attempt as a whole:
  Google 120 s per call), default headers and redacting event hooks.
  Its ``transport`` argument is the single injection point for the simulation's
  ``httpx.MockTransport``, the CLI's recording transport and test transports, so the live
  clients and parsers run unchanged in simulated mode.
* :func:`send_json` sends one logical call: it retries transient failures (3 attempts,
  exponential backoff from a 2 s base with jitter, ``Retry-After`` honoured in full; a
  requested wait too long to sleep through ends the call instead), decodes the JSON body
  with exact decimals, and raises exactly one of the error kinds of
  :mod:`ads_engine.connectors.errors`. ``AccessDenied``, ``QueryDefect`` and
  ``CallBudgetExhausted`` are never retried: a denial sends the source to simulated data,
  a defect fails the job, and an exhausted call budget defers the remaining calls.
  ``ads_engine.sync.mode`` alone decides those consequences.

Logging never carries a request or response body or header. Bodies hold personal data
(HubSpot email batch reads, LinkedIn lead responses) and requests carry the n8n secret
header and ``Authorization``. URLs are logged only through :func:`redact_url`, which keeps
nothing of the n8n capability URL but its host. Error reasons never carry a credential of
the call either, configured or not: :func:`send_json` holds every credential its requests
carry, and those the caller passes as ``secrets`` (an exchanged LinkedIn access token, a
rotated refresh token), with :func:`~ads_engine.connectors.errors.holding_secrets` while
each error is built, so a platform echoing one in ordinary prose never reaches a log. The
redaction processors of :mod:`ads_engine.logging` are the second line of defence, not the
first.

Example:
    A connector owns its client and sends each call through :func:`send_json`::

        async with build_client("hubspot") as http:

            def build() -> httpx.Request:
                return http.build_request(
                    "POST",
                    "/crm/objects/2026-09/contacts/batch/read",
                    json={"inputs": [{"id": "101"}]},
                    headers={"Authorization": f"Bearer {token.get_secret_value()}"},
                )

            response = await send_json(
                http,
                build,
                source="hubspot",
                endpoint="objects_batch_read contacts",
                accept_statuses=(200, 207),
            )
"""

import asyncio
import copy
import functools
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Context, Decimal, DecimalException, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, NoReturn
from urllib.parse import parse_qsl, unquote

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ads_engine import __version__
from ads_engine.connectors.errors import (
    AccessDenied,
    CallBudgetExhausted,
    ConnectorError,
    QueryDefect,
    TransientError,
    classify,
    classify_exception,
    holding_secrets,
    sanitize_message,
)
from ads_engine.db.models.ops import SOURCES
from ads_engine.logging import (
    REDACTED,
    get_logger,
    redact,
    redact_emails,
    redact_secret_matches,
)
from ads_engine.settings import Settings, Source, get_settings

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_RETRY",
    "SOURCE_BASE_URLS",
    "SOURCE_TIMEOUTS",
    "STARTED_EXTENSION",
    "USER_AGENT",
    "JsonResponse",
    "RetryPolicy",
    "build_client",
    "redact_url",
    "send_json",
]

SOURCE_BASE_URLS: Final[Mapping[Source, str]] = MappingProxyType(
    {
        # The n8n webhook URL (N8N_GOOGLE_ADS_WEBHOOK_URL) is absolute and posted to directly.
        "google": "",
        # The trailing slash makes relative paths such as `adAnalytics?...` resolve under
        # /rest/; LinkedIn's OAuth token URL is absolute and overrides it.
        "linkedin": "https://api.linkedin.com/rest/",
        "hubspot": "https://api.hubapi.com",
    }
)
"""Base URL of each source's client; connectors send paths relative to it."""

SOURCE_TIMEOUTS: Final[Mapping[Source, float]] = MappingProxyType(
    {
        # Each n8n webhook call runs one GAQL query synchronously: a 120 s timeout per call.
        "google": 120.0,
        # The plan fixes only Google's timeout; 60 s covers LinkedIn analytics and HubSpot
        # batch reads with room to spare while still failing a hung call well inside a job.
        "linkedin": 60.0,
        "hubspot": 60.0,
    }
)
"""Per-call deadline of each source, in seconds; also its client's read, write and pool timeout.

:func:`send_json` bounds each attempt, from sending the request to reading the last byte
of the response body, by this deadline. The client's httpx timeouts of the same value bound
each network phase and each read only, so a slowly dripping body never trips them alone.
"""

CONNECT_TIMEOUT_SECONDS: Final = 10.0
"""Connect timeout of every source's client, in seconds."""

USER_AGENT: Final = f"ads-engine/{__version__}"
"""``User-Agent`` sent on every request."""

STARTED_EXTENSION: Final = "ads_engine_started"
"""Request extension holding the ``time.monotonic()`` instant the request hook ran."""

_REDACTED_PATH: Final = "<redacted>"

# Query parameters whose whole value is a credential. A name matches when one of the three
# forms of `_key_forms` does: its raw form, case-folded as written, or its camelCase-split
# form is listed here or ends with a suffix below; or its separator-free form, the raw form
# with every `_` removed (`ACCESSTOKEN`, `rEfReShToKeN` and `X-API-KEY` give `accesstoken`,
# `refreshtoken` and `xapikey`), is a listed name or ends with a suffix, each with `_`
# removed.
_SENSITIVE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {"access_token", "token", "client_id", "client_secret", "refresh_token", "hapikey", "code"}
)
# Fail-closed widening, matching the key rule of ads_engine.logging: any parameter whose
# name ends like a credential in any form is redacted too. That includes LinkedIn's
# `pageToken` cursor, which carries no credential but loses nothing by being hidden.
_SENSITIVE_QUERY_SUFFIXES: Final[tuple[str, ...]] = ("_token", "_secret", "_password", "_api_key")
# The listed names and suffixes with `_` removed, compared with the separator-free form.
_SEPARATOR_FREE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    key.replace("_", "") for key in _SENSITIVE_QUERY_KEYS
)
_SEPARATOR_FREE_QUERY_SUFFIXES: Final[tuple[str, ...]] = tuple(
    suffix.replace("_", "") for suffix in _SENSITIVE_QUERY_SUFFIXES
)
_CAMEL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Headers whose whole value, and the credential after its scheme, authenticate a request.
_AUTHORIZATION_HEADERS: Final = frozenset({"authorization", "proxy-authorization"})
_FORM_CONTENT_TYPE: Final = "application/x-www-form-urlencoded"
# The credential every simulated client sends: LinkedIn's SIMULATED_TOKEN, Google's
# SIMULATED_SECRET and the simulated HubSpot token of ads_engine.sync.mode all equal it. It
# is a public placeholder, not a secret, and holding it would strip the word from
# simulated-mode reasons such as "ReadTimeout: simulated timeout", so it is never held.
_SIMULATED_PLACEHOLDER: Final = "simulated"

# Longest non-JSON error body, in characters, that is sanitised whole and then used for
# classification and the error message. Redaction runs on the complete text before any
# cut, so no secret or address is ever split into a fragment it no longer recognises.
# The limit bounds the work spent on an error body and the text kept from it: a longer
# body is never redacted, only classified from a window of its first this many
# characters, and the error raised is described by the body's size, keeping no text.
_ERROR_TEXT_SCAN_MAX: Final = 16_384

# Decimal context of JSON number decoding. Only its traps matter: the constructor keeps
# every digit whatever the precision, but an exponent beyond the decimal implementation's
# limits signals InvalidOperation, which an ambient context with that trap off turns into
# a silent NaN. The flags it accumulates are never read.
_JSON_DECIMAL_CONTEXT: Final = Context(traps=[InvalidOperation])

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def build_client(
    source: Source,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    settings: Settings | None = None,
) -> httpx.AsyncClient:
    """Build the ``httpx.AsyncClient`` every call to ``source`` goes through.

    The client has the source's base URL (:data:`SOURCE_BASE_URLS`), read, write and pool
    timeouts of :data:`SOURCE_TIMEOUTS` with a 10 s connect timeout, ``User-Agent`` and
    ``Accept: application/json`` default headers, no redirect following (a redirect is
    an unexpected status, hence a defect), and two event hooks that log each request at
    debug (``http.request``) and each response at info (``http.response``, with
    ``status`` and ``duration_ms`` to the response headers). Hooks log only the method and
    :func:`redact_url` of the URL.

    httpx applies those timeouts per network phase and per read, never to a call as a
    whole; :func:`send_json` adds that bound, the per-attempt deadline of
    :data:`SOURCE_TIMEOUTS`.

    No authentication is attached: each connector adds its own headers per request (the
    n8n secret header, LinkedIn's Bearer and version headers, HubSpot's Bearer), so no
    secret ever sits in the client's default headers.

    The caller owns the client's lifecycle::

        async with build_client("linkedin") as http:
            client = LinkedInClient(http, tokens)

    Args:
        source: The source the client calls.
        transport: The transport to send through: the simulation's
            ``mock_transport(source, world)``, the CLI's recording transport or a test's
            ``httpx.MockTransport``. ``None`` uses httpx's default network transport. It
            is passed through untouched, so live and simulated calls take one code path.
        settings: Source of the configured secrets that the logging hooks strip from
            logged URLs; :func:`ads_engine.settings.get_settings` when ``None``. When
            settings cannot be loaded, the URL rules of :func:`redact_url` still apply.

    Returns:
        A new, unopened ``httpx.AsyncClient``.

    Raises:
        ValueError: When ``source`` is not one of ``google``, ``linkedin``, ``hubspot``.
    """
    _require_source(source)
    log_request, log_response = _logging_hooks(source, _url_secrets(settings))
    return httpx.AsyncClient(
        base_url=SOURCE_BASE_URLS[source],
        timeout=httpx.Timeout(SOURCE_TIMEOUTS[source], connect=CONNECT_TIMEOUT_SECONDS),
        transport=transport,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
        event_hooks={"request": [log_request], "response": [log_response]},
    )


def _require_source(source: object) -> None:
    """Raise ``ValueError`` unless ``source`` is one of the database's allowed sources."""
    if source not in SOURCES or source not in SOURCE_BASE_URLS:
        raise ValueError(f"unknown source {source!r}; expected one of {', '.join(SOURCES)}")


def _url_secrets(settings: Settings | None) -> tuple[str, ...]:
    """Collect the configured credential plaintexts to strip from logged URLs, longest first.

    Every non-empty value of ``Settings.credential_values()``, each set secret and the
    LinkedIn client id, is collected, whatever its length. Connectors never put a
    credential in a URL; this guards against one that arrives there anyway, for example a
    token pasted into a configured URL.
    """
    if settings is None:
        try:
            settings = get_settings()
        except ValueError:
            # pydantic's ValidationError is a ValueError. The client still works; its hooks
            # keep the key and email rules, and the logging processors keep theirs.
            return ()
    plaintexts = {plaintext for plaintext in settings.credential_values() if plaintext}
    return tuple(sorted(plaintexts, key=lambda plaintext: (-len(plaintext), plaintext)))


# ---------------------------------------------------------------------------
# Redacted request logging
# ---------------------------------------------------------------------------


def redact_url(url: httpx.URL, *, source: Source, secrets: Collection[str] = ()) -> str:
    """Render a request URL for logging, with nothing sensitive left in it.

    * ``google``: only ``"{scheme}://{host}/<redacted>"``. The n8n webhook path is a
      capability URL: knowing it is enough to call the workflow.
    * ``linkedin`` and ``hubspot``: the raw path and query, with the value of every
      credential parameter replaced by ``[REDACTED]`` and its name kept as sent. A
      credential parameter's name, percent-decoded and in any letter case, is
      ``access_token``, ``token``, ``client_id``, ``client_secret``, ``refresh_token``,
      ``hapikey`` or ``code``, or ends in ``_token``, ``_secret``, ``_password`` or
      ``_api_key``. The name is compared case-folded as written, with camelCase split,
      and with every ``_`` and ``-`` removed against those names and suffixes with ``_``
      removed, so ``ACCESS_TOKEN``, ``AcCeSs_ToKeN``, ``access%5Ftoken``, ``pageToken``,
      ``clientId``, ``ACCESSTOKEN``, ``rEfReShToKeN`` and ``XAPIKEY`` all match.
      Beyond those names, a path segment, query name or query value that holds an email
      address in any script (:func:`ads_engine.logging.email_spans`) or one of
      ``secrets`` has ``[REDACTED_EMAIL]`` or ``[REDACTED]`` in its place. It is checked
      percent-decoded, again while decoding still changes it, up to four times, so
      ``lead%40example.com``, ``lead%2540example.com`` and the UTF-8 escapes of
      ``用户@例子.公司`` are caught and the component is rendered decoded; one encoded more
      deeply than that becomes ``[REDACTED]`` whole, and a secret that itself holds ``%``
      escapes is matched as written.

    This is the first line of defence: the HTTP layer redacts the percent-encoded forms
    itself, decoding each name and component, rather than leaving them to the log. The
    redaction processors of :mod:`ads_engine.logging` are the second line.

    Example::

        >>> redact_url(
        ...     httpx.URL("https://api.hubapi.com/crm/x?hapikey=abc&limit=1"), source="hubspot"
        ... )
        '/crm/x?hapikey=[REDACTED]&limit=1'
        >>> redact_url(httpx.URL("https://acme.app.n8n.cloud/webhook/f00d"), source="google")
        'https://acme.app.n8n.cloud/<redacted>'

    Args:
        url: The request URL.
        source: The source the request goes to.
        secrets: Plaintext secrets to strip from path segments and query values, whatever
            their length, all in one pass and longest first, in any order given;
            :func:`build_client` passes the configured secrets.

    Returns:
        The loggable form of the URL.
    """
    if source == "google":
        host = url.host
        return f"{url.scheme}://{host}/{_REDACTED_PATH}" if host else f"/{_REDACTED_PATH}"
    raw = url.raw_path.decode("ascii", errors="replace")
    path, separator, query = raw.partition("?")
    safe_path = "/".join(_scrub_component(segment, secrets) for segment in path.split("/"))
    if not separator:
        return safe_path
    return f"{safe_path}?{_redact_query(query, secrets)}"


def _key_forms(name: str) -> tuple[str, str, str]:
    """Normalise a query parameter name into the three forms compared with the credential names.

    All three forms are percent-decoded and stripped, with ``-`` replaced by ``_``. The raw
    form is case-folded as written; the camelCase form splits each lowercase-to-uppercase
    boundary with ``_`` before lowercasing; the separator-free form is the raw form with
    every ``_`` removed. Splitting alone would turn a mixed-case ``AcCeSs_ToKeN`` into
    ``ac_ce_ss_to_ke_n`` and miss it, and neither of the first two forms separates a name
    written without ``_``, ``-`` or a camelCase boundary. So ``AcCeSs_ToKeN`` gives
    ``access_token``, ``ac_ce_ss_to_ke_n`` and ``accesstoken``; ``pageToken`` gives
    ``pagetoken``, ``page_token`` and ``pagetoken``; and ``rEfReShToKeN`` gives
    ``refreshtoken``, ``r_ef_re_sh_to_ke_n`` and ``refreshtoken``.
    """
    decoded = unquote(name).strip()
    raw = decoded.casefold().replace("-", "_")
    camel = _CAMEL_BOUNDARY_RE.sub("_", decoded).lower().replace("-", "_")
    return raw, camel, raw.replace("_", "")


def _is_sensitive_key(name: str) -> bool:
    """Whether the query parameter, header or form field ``name`` carries a credential.

    In any letter case, its raw or camelCase form is a credential name or ends with a
    credential suffix (``ACCESS_TOKEN``, ``pageToken``, ``X-Webhook-Secret``), or its
    separator-free form is one of those names or ends with one of those suffixes, each
    with ``_`` removed (``ACCESSTOKEN``, ``XAPIKEY``).
    """
    raw, camel, separator_free = _key_forms(name)
    return (
        any(
            form in _SENSITIVE_QUERY_KEYS or form.endswith(_SENSITIVE_QUERY_SUFFIXES)
            for form in (raw, camel)
        )
        or separator_free in _SEPARATOR_FREE_QUERY_KEYS
        or separator_free.endswith(_SEPARATOR_FREE_QUERY_SUFFIXES)
    )


def _redact_query(query: str, secrets: Collection[str]) -> str:
    """Redact a raw query string pair by pair, keeping every other byte as sent."""
    pairs: list[str] = []
    for pair in query.split("&"):
        key, separator, value = pair.partition("=")
        if separator and _is_sensitive_key(key):
            pairs.append(f"{key}={REDACTED}")
        else:
            pairs.append(
                f"{_scrub_component(key, secrets)}{separator}{_scrub_component(value, secrets)}"
            )
    return "&".join(pairs)


@functools.lru_cache(maxsize=32)
def _secrets_pattern(secrets: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile one alternation of the non-empty ``secrets``, longest first.

    Alternatives are tried in order, so a secret that contains another one is replaced
    whole. Every alternative is an escaped literal, so matching stays linear.

    Returns:
        The pattern, or ``None`` when no secret is set.
    """
    plaintexts = sorted(
        {secret for secret in secrets if secret}, key=lambda secret: (-len(secret), secret)
    )
    if not plaintexts:
        return None
    return re.compile("|".join(re.escape(secret) for secret in plaintexts))


def _scrub_text(text: str, secrets: Collection[str]) -> str:
    """Replace every secret, then each email address in any script, inside ``text``.

    All secrets are replaced in one pass, so a short secret is never matched inside the
    ``[REDACTED]`` that took a longer one's place, and each takes every email address and
    every percent-encoded run holding a ``%XX`` escape that it overlaps with it
    (:func:`ads_engine.logging.redact_secret_matches`), so no part of an address, and no
    broken escape or encoded credential, that holds a secret is left behind. Addresses are
    found by the logging module's shared scanner (:func:`ads_engine.logging.email_spans`),
    so ``用户@例子.公司`` and ``jöe.smith@example.com`` become ``[REDACTED_EMAIL]`` whole.
    """
    pattern = _secrets_pattern(tuple(secrets))
    if pattern is not None:
        text = redact_secret_matches(text, pattern)
    return redact_emails(text)


# Decoding rounds of one URL component, as many as the logging redaction decodes: enough for
# a value encoded and then re-encoded by every layer up to the request. A component that
# still decodes after the last round is replaced whole, so the bound lets nothing through.
_MAX_COMPONENT_DECODES: Final = 4


def _scrub_component(component: str, secrets: Collection[str]) -> str:
    """Return a path segment or query name or value, scrubbed of emails and secrets.

    The component is percent-decoded, and decoded again while that still changes it, up
    to :data:`_MAX_COMPONENT_DECODES` times, so an address encoded once or more becomes
    visible (``lead%40example.com``, ``lead%2540example.com``, the UTF-8 escapes of
    ``用户@例子.公司``). The first decoded form in which a secret or an address is found is
    returned scrubbed. A component that would still decode after the last round becomes
    ``[REDACTED]`` whole. Otherwise the raw form is checked as well, for a secret that
    itself contains ``%`` escapes, and the raw bytes are returned unchanged when it is
    clean.
    """
    decoded = component
    for _ in range(_MAX_COMPONENT_DECODES):
        previous, decoded = decoded, unquote(decoded)
        if decoded == previous:
            break
        scrubbed = _scrub_text(decoded, secrets)
        if scrubbed != decoded:
            return scrubbed
    else:
        if unquote(decoded) != decoded:
            return REDACTED
    return _scrub_text(component, secrets)


def _logging_hooks(
    source: Source, secrets: tuple[str, ...]
) -> tuple[
    Callable[[httpx.Request], Awaitable[None]],
    Callable[[httpx.Response], Awaitable[None]],
]:
    """Build the request and response event hooks of a source's client.

    Args:
        source: The source the client calls.
        secrets: Plaintext secrets to strip from logged URLs.

    Returns:
        The request hook and the response hook.
    """

    async def log_request(request: httpx.Request) -> None:
        """Stamp the request's start instant and log it at debug: method and URL only."""
        request.extensions[STARTED_EXTENSION] = time.monotonic()
        _log.debug(
            "http.request",
            source=source,
            method=request.method,
            url=redact_url(request.url, source=source, secrets=secrets),
        )

    async def log_response(response: httpx.Response) -> None:
        """Log a response at info: method, URL, status and ``duration_ms`` to its headers.

        ``duration_ms`` is the milliseconds from the request's start stamp to the response
        headers, to one decimal; ``None`` for a request the request hook never stamped.
        """
        request = response.request
        started = request.extensions.get(STARTED_EXTENSION)
        duration_ms = (
            round((time.monotonic() - started) * 1000.0, 1) if isinstance(started, float) else None
        )
        _log.info(
            "http.response",
            source=source,
            method=request.method,
            url=redact_url(request.url, source=source, secrets=secrets),
            status=response.status_code,
            duration_ms=duration_ms,
        )

    return log_request, log_response


# ---------------------------------------------------------------------------
# Retry policy and JSON calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How :func:`send_json` retries a transient failure.

    The default is the plan's policy: 3 attempts in total, exponential backoff from a 2 s
    base (2 s, then 4 s, each plus up to 1 s of jitter, capped at 30 s), and a platform's
    ``Retry-After`` (or Google ``retryDelay``) honoured in place of the backoff. A
    requested wait is never shortened: :func:`send_json` sleeps through one of up to
    ``max_retry_after_seconds`` (120 s by default) in full, and raises the
    ``TransientError`` of a longer one at once, its ``retry_after`` intact, so the caller
    defers the work instead of calling before the platform allows it.

    Example:
        Tests record waits instead of sleeping::

            waits: list[float] = []


            async def record(seconds: float) -> None:
                waits.append(seconds)


            policy = RetryPolicy(sleep=record)

    Attributes:
        attempts: Attempts in total, the first one included; at least 1.
        base_seconds: Backoff before the second attempt; doubled for each later one.
        max_seconds: Upper bound of one backoff wait.
        jitter_seconds: Upper bound of the uniform random jitter added to each backoff.
        max_retry_after_seconds: The longest platform-requested wait :func:`send_json`
            sleeps through before retrying. A longer one ends the call with its
            ``TransientError``, whose ``retry_after`` keeps the full delay.
        sleep: Awaitable sleep used between attempts.
    """

    attempts: int = 3
    base_seconds: float = 2.0
    max_seconds: float = 30.0
    jitter_seconds: float = 1.0
    max_retry_after_seconds: float = 120.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __post_init__(self) -> None:
        """Validate the policy.

        Raises:
            ValueError: When ``attempts`` is not a positive integer, or a duration is
                negative or not finite.
        """
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise ValueError("RetryPolicy.attempts must be an integer")
        if self.attempts < 1:
            raise ValueError("RetryPolicy.attempts must be at least 1")
        durations = {
            "base_seconds": self.base_seconds,
            "max_seconds": self.max_seconds,
            "jitter_seconds": self.jitter_seconds,
            "max_retry_after_seconds": self.max_retry_after_seconds,
        }
        for name, seconds in durations.items():
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError(f"RetryPolicy.{name} must be a finite, non-negative number")


DEFAULT_RETRY: Final = RetryPolicy()
"""The plan's retry policy: 3 attempts, 2 s exponential base, jitter, ``Retry-After`` honoured."""


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """A response :func:`send_json` accepted, with its decoded JSON body.

    ``body`` and ``headers`` are left out of ``repr``, because a body can hold personal
    data (lead responses, CRM contacts) and a ``repr`` ends up in tracebacks and logs.

    Attributes:
        status: The HTTP status, one of the call's ``accept_statuses``.
        body: The decoded JSON, with every JSON number that has a fraction or exponent as
            an exact, finite :class:`~decimal.Decimal`; ``None`` for an empty body.
        headers: The response headers.
    """

    status: int
    # Hidden from repr: response bodies carry emails and lead answers.
    body: Any = field(repr=False)
    headers: httpx.Headers = field(repr=False)


async def send_json(
    client: httpx.AsyncClient,
    build_request: Callable[[], httpx.Request],
    *,
    source: Source,
    endpoint: str,
    policy: RetryPolicy = DEFAULT_RETRY,
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    accept_statuses: Collection[int] = (200,),
    secrets: Collection[str] = (),
) -> JsonResponse:
    """Send one logical call, retrying transient failures, and decode its JSON response.

    Each attempt, in order:

    1. awaits ``before_attempt`` when given. The Google client passes its call-budget
       acquire, so every webhook POST (retries, partition and next-page calls included)
       counts against the product's own cap; a ``CallBudgetExhausted`` raised here means
       no request is sent and nothing is retried;
    2. builds a fresh request with ``build_request``, so a refreshed token or a new body
       is used. A request that cannot be built (an invalid URL, a body that does not
       serialise, a header that does not encode) is a ``QueryDefect`` with code
       ``CLIENT``, raised without a send and never retried; a ``ConnectorError`` the
       builder raises itself is raised unchanged, unless it carries a held secret (below);
    3. sends it and reads the whole response body within the source's
       :data:`SOURCE_TIMEOUTS` deadline (Google: 120 s). An expired deadline is a
       ``TransientError`` with code ``TIMEOUT``; any other httpx failure (timeout,
       network, protocol) is classified with
       :func:`~ads_engine.connectors.errors.classify_exception`;
    4. decodes the body as JSON with exact, finite decimals, so no binary float reaches a
       money or conversion value; an empty body decodes to ``None``. A body that is not
       JSON, or holds ``NaN``, ``Infinity`` or an exponent beyond the decimal limits, does
       not decode, whatever the ambient decimal context: under an accepted status it is a
       ``QueryDefect`` (``validation``);
    5. returns a :class:`JsonResponse` when the status is in ``accept_statuses``. For
       ``google`` the body is classified first, because n8n relays Google denials,
       quota exhaustion and query errors inside HTTP 200;
    6. otherwise raises the :func:`~ads_engine.connectors.errors.classify` result for the
       response, or a ``QueryDefect`` (``unexpected_status``) when the status signals no
       error by itself (an unaccepted 2xx, or a redirect). A body that step 4 could not
       decode is classified from its text, sanitised whole before it is cut; one longer
       than 16,384 characters is classified from its first 16,384 characters and
       described by its size.

    Only ``TransientError`` (429, 5xx, timeouts, network, ``RESOURCE_EXHAUSTED``) is
    retried, waiting the full ``Retry-After`` (or Google ``retryDelay``) when the
    platform sent one and the policy's exponential backoff otherwise. A requested wait
    longer than ``policy.max_retry_after_seconds`` is never shortened: that error is
    raised at once, its ``retry_after`` intact, so no request is sent before the platform
    allows it. After the last attempt the last error is raised.
    ``AccessDenied``, ``QueryDefect`` and ``CallBudgetExhausted`` are raised on their
    first occurrence. Every classified error is logged as ``connector.error`` (source,
    endpoint, kind, code, HTTP status, attempt, retryability, the sanitised reason,
    ``retry_after``, ``query_id`` and ``will_retry``) before it is raised; headers and
    bodies never are. An attempt that will be retried is logged at debug level and its
    retry as ``http.retry`` at warning level; a raised ``TransientError`` is logged at
    warning level, and a raised ``AccessDenied`` or ``QueryDefect`` at info level, because
    the owner of its consequence logs the terminal record (:func:`_log_error`).

    No error of the call carries one of its credentials, even one the settings do not
    know. Each attempt runs inside :func:`~ads_engine.connectors.errors.holding_secrets`,
    holding ``secrets`` and the credentials of every request the call has built so far:
    the ``Authorization`` value and the credential after its scheme, and each header,
    query parameter and ``application/x-www-form-urlencoded`` body field with a credential
    name (:func:`redact_url`'s names, which cover the n8n secret header, ``refresh_token``
    and ``client_secret``). A token from an earlier attempt therefore stays held after a
    retry. Every error built during the attempt, here, in
    :mod:`~ads_engine.connectors.errors` or by a transport, loses them before its message
    is cut, and so does the reason logged. A logged traceback renders the text, the
    arguments and the notes of the error raised and of every exception linked to it, and
    the log redaction does not know a held secret. So a ``ConnectorError`` built before the
    call that the builder or transport raises carrying one, in its message, its code, a
    note or a linked exception, is replaced by a copy rebuilt without them, with no
    notes, cause or context; and an exception quoting one in any of those texts, plain or
    percent-encoded as deeply as the log redaction decodes, is raised for with neither
    cause nor context. Blank values and the public ``simulated`` placeholder are never
    held.

    Args:
        client: A client from :func:`build_client`.
        build_request: Builds the request of one attempt, normally with
            ``client.build_request``.
        source: The source called; ``google`` turns on body classification of accepted
            responses.
        endpoint: The contract surface name, optionally followed by a query id, such as
            ``search Q-G2``; it names the call in errors and logs.
        policy: The retry policy.
        before_attempt: Awaited before every attempt, for example a call budget or a
            rate limiter.
        accept_statuses: Statuses returned as data instead of raised, for example
            ``(200, 207)`` for HubSpot batch reads or ``(200, 404)`` for a property read.
        secrets: Plaintexts held only in memory that no error or log of the call may
            carry, beyond the credentials its requests carry: for example the LinkedIn
            token provider's exchanged, rotated and retired tokens, or the access token a
            refresh replaced.

    Returns:
        The accepted response with its decoded body.

    Raises:
        AccessDenied: The platform refused the credentials or their permissions.
        TransientError: A transient failure survived every attempt, or asked for a wait
            longer than ``policy.max_retry_after_seconds``.
        CallBudgetExhausted: ``before_attempt`` reported the call budget used up.
        QueryDefect: The request is wrong, the response is not JSON or holds an
            unrepresentable number, or its status was unexpected.
        TypeError: When ``secrets`` is a single string, which would otherwise be read as
            one-character secrets, or holds anything but strings.
        ValueError: When ``source`` is unknown or ``accept_statuses`` is empty.
    """
    _require_source(source)
    accepted = frozenset(accept_statuses)
    if not accepted:
        raise ValueError("accept_statuses must name at least one HTTP status")
    if isinstance(secrets, str | bytes):
        raise TypeError("secrets must be a collection of strings, not a single string")
    if not all(isinstance(secret, str) for secret in secrets):
        raise TypeError("secrets must hold strings only")
    # Grows with the credentials of each attempt's request and is held by every later
    # attempt, so a token replaced between attempts stays out of their errors too.
    held = _holdable(secrets)
    retrying = AsyncRetrying(
        stop=stop_after_attempt(policy.attempts),
        wait=_RetryWait(policy),
        retry=retry_if_exception(_retry_predicate(policy)),
        sleep=policy.sleep,
        reraise=True,
        before_sleep=_retry_logger(source, endpoint),
    )
    async for attempt in retrying:
        with attempt:
            return await _send_once(
                client,
                build_request,
                source=source,
                endpoint=endpoint,
                accepted=accepted,
                before_attempt=before_attempt,
                attempt_number=attempt.retry_state.attempt_number,
                held=held,
                policy=policy,
            )
    # tenacity either returns from inside the loop or raises the last error (reraise=True).
    raise RuntimeError("retry loop ended without an outcome")  # pragma: no cover


def _is_retryable(error: BaseException) -> bool:
    """Whether ``error`` is of a retryable kind: a ``TransientError`` other than the call budget."""
    return (
        isinstance(error, TransientError)
        and not isinstance(error, CallBudgetExhausted)
        and error.retryable
    )


def _retry_predicate(policy: RetryPolicy) -> Callable[[BaseException], bool]:
    """Build tenacity's ``retry`` test of :func:`send_json` for ``policy``.

    An error is retried when it is retryable (:func:`_is_retryable`) and the wait the
    platform asked for, if any, is at most ``policy.max_retry_after_seconds``. A longer
    requested wait is never shortened: the error is raised at once, its ``retry_after``
    intact, so no request is sent before the platform allows it and the caller defers
    the work instead.
    """

    def should_retry(error: BaseException) -> bool:
        """Whether tenacity retries ``error`` under ``policy``."""
        if not _is_retryable(error):
            return False
        retry_after = error.retry_after if isinstance(error, TransientError) else None
        return retry_after is None or retry_after <= policy.max_retry_after_seconds

    return should_retry


def _failed_with(retry_state: RetryCallState) -> BaseException | None:
    """Return the exception the attempt in ``retry_state`` failed with, if any."""
    outcome = retry_state.outcome
    if outcome is None or not outcome.failed:
        return None
    return outcome.exception()


class _RetryWait:
    """tenacity wait strategy: the platform's full ``Retry-After``, else exponential jitter.

    A platform-requested delay is never shortened. The policy's ``max_seconds`` caps the
    backoff only; a requested delay beyond ``max_retry_after_seconds`` never reaches this
    strategy, because the retry predicate of :func:`send_json` stops the call first.
    """

    __slots__ = ("_backoff",)

    def __init__(self, policy: RetryPolicy) -> None:
        self._backoff = wait_exponential_jitter(
            initial=policy.base_seconds,
            max=policy.max_seconds,
            jitter=policy.jitter_seconds,
        )

    def __call__(self, retry_state: RetryCallState) -> float:
        """Return the seconds to wait before the next attempt."""
        error = _failed_with(retry_state)
        if isinstance(error, TransientError) and error.retry_after is not None:
            return error.retry_after
        return float(self._backoff(retry_state))


def _retry_logger(source: Source, endpoint: str) -> Callable[[RetryCallState], None]:
    """Build tenacity's ``before_sleep`` hook, which logs ``http.retry`` at warning level.

    Each retry is a transient failure the call has not yet given up on, so it is a warning.
    The failed attempt's own ``connector.error`` stays at debug level (:func:`_log_error`),
    which leaves this event its one record above debug.
    """

    def log_retry(retry_state: RetryCallState) -> None:
        """Log the failed attempt and the wait before the next one; never a body."""
        error = _failed_with(retry_state)
        connector_error = error if isinstance(error, ConnectorError) else None
        _log.warning(
            "http.retry",
            source=source,
            endpoint=endpoint,
            attempt=retry_state.attempt_number,
            wait_seconds=round(retry_state.upcoming_sleep, 3),
            kind=connector_error.kind if connector_error else None,
            code=connector_error.code if connector_error else None,
            http_status=connector_error.http_status if connector_error else None,
            retry_after=error.retry_after if isinstance(error, TransientError) else None,
        )

    return log_retry


def _construction_defect(exc: Exception, *, source: Source, endpoint: str) -> QueryDefect:
    """Classify a failure to build a request as a ``QueryDefect`` with code ``CLIENT``.

    Building a request is deterministic, so the failure never clears on retry. That holds
    even for an ``httpx.TransportError`` subclass, which
    :func:`~ads_engine.connectors.errors.classify_exception` files as transient because a
    transport raising it may succeed on the next attempt.
    """
    classified = classify_exception(exc, source=source, endpoint=endpoint)
    if isinstance(classified, QueryDefect):
        return classified
    return QueryDefect(
        classified.message,
        source=source,
        endpoint=endpoint,
        code="CLIENT",
        query_id=_query_id(endpoint),
    )


def _holdable(values: Iterable[str]) -> set[str]:
    """Return the ``values`` worth holding: neither blank nor the simulated placeholder."""
    return {value for value in values if value.strip() and value.strip() != _SIMULATED_PLACEHOLDER}


def _request_secrets(request: httpx.Request) -> set[str]:
    """Return the credentials ``request`` carries, for :func:`send_json` to hold.

    They are the whole value of an ``Authorization`` or ``Proxy-Authorization`` header and
    the credential after its scheme (the token of ``Bearer <token>``), and the value of
    every header, query parameter and ``application/x-www-form-urlencoded`` body field
    whose name :func:`_is_sensitive_key` reads as a credential: the n8n secret header
    (``X-Webhook-Secret``, ``X-Webhook-Token``), ``refresh_token``, ``client_id``,
    ``client_secret``. A streamed body that was never read is not inspected. Blank values
    and the simulated placeholder are left out (:func:`_holdable`).
    """
    values: list[str] = []
    for name, value in request.headers.multi_items():
        if name.lower() in _AUTHORIZATION_HEADERS:
            credential = value.strip().partition(" ")[2].strip()
            if credential != _SIMULATED_PLACEHOLDER:
                values.extend((value, credential))
        elif _is_sensitive_key(name):
            values.append(value)
    values.extend(
        value for name, value in request.url.params.multi_items() if _is_sensitive_key(name)
    )
    media_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if media_type == _FORM_CONTENT_TYPE:
        try:
            content = request.content
        except httpx.RequestNotRead:
            content = b""
        fields = parse_qsl(content.decode("utf-8", errors="replace"), keep_blank_values=True)
        values.extend(value for name, value in fields if _is_sensitive_key(name))
    return _holdable(values)


def _rendered_texts(exc: BaseException) -> list[str] | None:
    """Return every text a logged traceback renders for ``exc`` itself, its links aside.

    They are its ``str``; the ``repr`` of its arguments, from which the log re-renders an
    exception whose ``__str__`` is a built-in one; each of its notes (PEP 678
    ``add_note``), as ``str`` renders it whatever its type; and, for a ``SyntaxError``, its
    message, file name and source line.

    Returns:
        The texts, or ``None`` when one cannot be rendered (a ``__str__`` or ``repr`` that
        raises) or the notes are not a list or tuple, which ``add_note`` never leaves.
    """
    try:
        notes = getattr(exc, "__notes__", ())
        if not isinstance(notes, list | tuple):
            return None
        texts = [str(exc), repr(exc.args), *(str(note) for note in notes)]
        if isinstance(exc, SyntaxError):
            parts = (exc.msg, exc.filename, exc.text)
            texts.extend(str(part) for part in parts if part is not None)
    # No-blind-except yields: an exception's __str__, an argument's repr or a note's __str__
    # can raise anything; the caller fails closed.
    except Exception:  # noqa: BLE001
        return None
    return texts


def _quotes_held(error: BaseException, held: Collection[str]) -> bool:
    """Whether a logged traceback of ``error`` would render a held secret.

    Every text the traceback renderer emits (:func:`_rendered_texts`: the text, the
    arguments and the notes) is read for ``error`` and for every exception linked to it,
    however deep: each cause, each context and each member of an exception group. A text
    quotes a held secret when its log redaction (:func:`ads_engine.logging.redact`)
    changes once the held secrets are redacted as well. That finds a secret plain or
    percent-encoded, in either letter case of the escapes, through the four decoding
    rounds the redaction applies; a run encoded more deeply is replaced whole by the log
    redaction anyway. Such an exception is never chained to the error raised for it,
    because the log redaction does not know a held secret. Text that cannot be rendered
    counts as quoting one.
    """
    secrets = tuple(sorted({secret for secret in held if secret}))
    if not secrets:
        return False
    pending: list[BaseException] = [error]
    seen: list[BaseException] = []
    while pending:
        current = pending.pop()
        if any(current is known for known in seen):
            continue
        seen.append(current)
        texts = _rendered_texts(current)
        if texts is None or any(redact(text, secrets=secrets) != redact(text) for text in texts):
            return True
        linked: list[BaseException | None] = [current.__cause__, current.__context__]
        if isinstance(current, BaseExceptionGroup):
            linked.extend(current.exceptions)
        pending.extend(link for link in linked if link is not None)
    return False


def _without_held(error: ConnectorError, held: Collection[str]) -> ConnectorError:
    """Return ``error`` when it quotes no held secret, else a copy rebuilt without them.

    Called while ``held`` is held, for an error a builder or transport raised. One built
    before the secrets were held can still carry one in its message or code, in a note
    added to it, or in an exception linked to it (:func:`_quotes_held`). The original is
    kept only when rebuilding leaves its message and code unchanged and none of those
    quotes a held secret. ``copy.copy`` rebuilds the error through its constructor
    (``ConnectorError.__reduce__``), whose sanitising strips them now, also a start of
    one that an earlier cut left, and the copy has no traceback, notes, cause or context.
    """
    rebuilt = copy.copy(error)
    unchanged = (rebuilt.message, rebuilt.code) == (error.message, error.code)
    if unchanged and not _quotes_held(error, held):
        return error
    return rebuilt


async def _send_once(
    client: httpx.AsyncClient,
    build_request: Callable[[], httpx.Request],
    *,
    source: Source,
    endpoint: str,
    accepted: frozenset[int],
    before_attempt: Callable[[], Awaitable[None]] | None,
    attempt_number: int,
    held: set[str],
    policy: RetryPolicy,
) -> JsonResponse:
    """Run one attempt of :func:`send_json`: steps 1 to 6 of its docstring.

    Every step runs while ``held`` is held. Once the request is built, ``held`` gains the
    credentials it carries, and the call keeps them for its later attempts.
    """
    replacement: ConnectorError | None = None
    with holding_secrets(held):
        if before_attempt is not None:
            await before_attempt()
        try:
            request = build_request()
        except ConnectorError as raised:
            # A builder may raise a classified error itself; it keeps its kind.
            replacement = _without_held(raised, held)
            _log_error(replacement, attempt_number, policy)
            if replacement is raised:
                raise
        except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TypeError, ValueError) as exc:
            # The request cannot be built: an invalid URL, a JSON body that does not serialise,
            # a header that does not encode. The same failure recurs on every attempt.
            defect = _construction_defect(exc, source=source, endpoint=endpoint)
            _log_error(defect, attempt_number, policy)
            if not _quotes_held(exc, held):
                raise defect from exc
            replacement = defect
    if replacement is not None:
        # Raised outside the handler, so the error or exception that quoted a held secret
        # is neither its cause nor its context.
        raise replacement
    held.update(_request_secrets(request))
    with holding_secrets(held):
        return await _send_request(
            client,
            request,
            source=source,
            endpoint=endpoint,
            accepted=accepted,
            attempt_number=attempt_number,
            held=held,
            policy=policy,
        )


async def _send_request(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    source: Source,
    endpoint: str,
    accepted: frozenset[int],
    attempt_number: int,
    held: Collection[str],
    policy: RetryPolicy,
) -> JsonResponse:
    """Run steps 3 to 6 of a :func:`send_json` attempt for its built ``request``.

    Called while ``held``, the request's own credentials included, is held. ``policy``
    decides whether each ``connector.error`` it logs reports a retry (:func:`_log_error`).
    """
    # httpx's timeouts bound each network phase and each read, not the call: a body that
    # drips in faster than the read timeout would hold the call open indefinitely. The
    # deadline bounds the whole attempt, body included, since client.send reads the body.
    deadline_seconds = SOURCE_TIMEOUTS[source]
    deadline = asyncio.timeout(deadline_seconds)
    replacement: ConnectorError | None = None
    try:
        async with deadline:
            response = await client.send(request)
    except ConnectorError as raised:
        # A transport may raise a classified error itself, as the simulation's mock
        # transport does for an injected fault or a request that breaks its contract.
        replacement = _without_held(raised, held)
        _log_error(replacement, attempt_number, policy)
        if replacement is raised:
            raise
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TimeoutError) as exc:
        # Beyond httpx.HTTPError: InvalidURL, StreamError and the built-in TimeoutError do
        # not derive from it, and classify_exception maps each of them to its kind. An
        # expired deadline raises the built-in TimeoutError, or whatever the cancelled send
        # raised in its place, and is told apart from an httpx timeout by expired().
        failure: ConnectorError
        if deadline.expired():
            failure = TransientError(
                f"call exceeded the {deadline_seconds:g} s per-call deadline",
                source=source,
                endpoint=endpoint,
                code="TIMEOUT",
            )
        else:
            failure = classify_exception(exc, source=source, endpoint=endpoint)
        _log_error(failure, attempt_number, policy)
        if not _quotes_held(exc, held):
            raise failure from exc
        replacement = failure
    if replacement is not None:
        # Raised outside the handler, so the error or exception that quoted a held secret
        # is neither its cause nor its context.
        raise replacement

    status = response.status_code
    body, is_json = _decode_json(response.content)
    if status in accepted:
        if not is_json:
            _raise_logged(
                QueryDefect(
                    "response is not JSON or holds an unrepresentable number",
                    source=source,
                    endpoint=endpoint,
                    code="validation",
                    http_status=status,
                    query_id=_query_id(endpoint),
                ),
                attempt_number,
                policy,
            )
        if source == "google":
            # n8n answers HTTP 200 for Google denials and quota errors; the body decides.
            relayed = classify(response, body, source=source, endpoint=endpoint)
            if relayed is not None:
                _raise_logged(relayed, attempt_number, policy)
        return JsonResponse(status=status, body=body, headers=response.headers)

    if is_json:
        classified = classify(response, body, source=source, endpoint=endpoint)
    else:
        classified = _classify_text(response, source=source, endpoint=endpoint)
    if classified is None:
        classified = QueryDefect(
            f"unexpected HTTP {status} response",
            source=source,
            endpoint=endpoint,
            code="unexpected_status",
            http_status=status,
            query_id=_query_id(endpoint),
        )
    _raise_logged(classified, attempt_number, policy)


def _classify_text(
    response: httpx.Response, *, source: Source, endpoint: str
) -> ConnectorError | None:
    """Classify a response whose body is not JSON from its text, keeping no raw fragment.

    A body of at most ``_ERROR_TEXT_SCAN_MAX`` characters is sanitised whole with
    :func:`~ads_engine.connectors.errors.sanitize_message`, which redacts emails, URL
    passwords and every configured secret before it cuts the result to 300 characters;
    that safe text is both the input of :func:`classify` and the error's message.

    A longer body is classified from its first ``_ERROR_TEXT_SCAN_MAX`` characters, so a
    Google token at its start (``PERMISSION_DENIED``, ``RESOURCE_EXHAUSTED``,
    ``QueryError``) decides its kind as it would in a shorter body. A secret or address
    the window cuts in two can no longer be recognised for redaction, so no text of the
    window is kept: the error raised is a new one of the same class, described by the
    body's size, that carries over only the code, HTTP status, ``retry_after`` and query id.
    The error classified from the window is discarded, never raised, chained or logged.

    Returns:
        The error to raise, or ``None`` when the status and text signal no error.
    """
    text = response.text
    if len(text) <= _ERROR_TEXT_SCAN_MAX:
        return classify(response, sanitize_message(text), source=source, endpoint=endpoint)
    probe = classify(response, text[:_ERROR_TEXT_SCAN_MAX], source=source, endpoint=endpoint)
    description = f"non-JSON error body of {len(text)} characters"
    if isinstance(probe, AccessDenied):
        return AccessDenied(
            description,
            source=source,
            endpoint=endpoint,
            code=probe.code,
            http_status=probe.http_status,
        )
    if isinstance(probe, TransientError):
        return TransientError(
            description,
            source=source,
            endpoint=endpoint,
            code=probe.code,
            http_status=probe.http_status,
            retry_after=probe.retry_after,
        )
    if isinstance(probe, QueryDefect):
        return QueryDefect(
            description,
            source=source,
            endpoint=endpoint,
            code=probe.code,
            http_status=probe.http_status,
            query_id=probe.query_id,
        )
    # Reached only when classify found no error, since it builds no other class: the caller
    # then raises its unexpected-status defect, which keeps no text of the body either.
    return None


def _decode_json(content: bytes) -> tuple[Any, bool]:
    """Decode a response body as JSON with exact, finite decimals.

    Every number with a fraction or exponent becomes a :class:`~decimal.Decimal` through
    :func:`_parse_decimal`, whatever the ambient decimal context; ``NaN`` and ``Infinity``
    are refused by :func:`_reject_constant`.

    Returns:
        ``(body, True)`` for JSON, ``(None, True)`` for an empty or blank body, and
        ``(None, False)`` for anything else: a body that is not JSON, nests beyond the
        parser's depth, or holds a number no finite ``Decimal`` represents.
    """
    if not content.strip():
        return None, True
    try:
        body = json.loads(content, parse_float=_parse_decimal, parse_constant=_reject_constant)
    except ValueError, RecursionError, DecimalException:
        # ValueError covers JSONDecodeError, UnicodeDecodeError, an integer beyond the
        # interpreter's digit limit and the numbers rejected below; RecursionError a body
        # nested beyond the parser's depth; DecimalException an exponent beyond the decimal
        # implementation's limits.
        return None, False
    return body, True


def _parse_decimal(text: str) -> Decimal:
    """Decode a JSON number with a fraction or exponent as an exact, finite ``Decimal``.

    The strict context makes an exponent beyond the decimal implementation's limits raise
    even when the ambient context would return ``NaN``; every digit is kept, unrounded.

    Raises:
        decimal.InvalidOperation: When the exponent is beyond those limits.
        ValueError: When the result is not finite.
    """
    number = Decimal(text, context=_JSON_DECIMAL_CONTEXT)
    if not number.is_finite():
        raise ValueError("JSON number is not a finite decimal")
    return number


def _reject_constant(name: str) -> NoReturn:
    """Refuse ``NaN``, ``Infinity`` and ``-Infinity``.

    They are not JSON, and Python's parser would otherwise turn them into binary floats
    in a money or conversion value.

    Raises:
        ValueError: Always.
    """
    raise ValueError(f"non-standard JSON constant {name}")


def _query_id(endpoint: str) -> str | None:
    """Return the query id suffix of an endpoint such as ``search Q-G2``, as errors.py does."""
    parts = endpoint.split(maxsplit=1)
    return parts[1] if len(parts) == 2 else None


def _log_error(error: ConnectorError, attempt_number: int, policy: RetryPolicy) -> None:
    """Log a classified error as ``connector.error``, with its reason but never a body.

    The event carries source, endpoint, kind, code, HTTP status, attempt, retryability and
    :attr:`~ads_engine.connectors.errors.ConnectorError.reason`, which keeps failures that
    share every other field distinguishable. It also carries ``retry_after``, the wait a
    ``TransientError`` asked for, and ``query_id``, a ``QueryDefect``'s query id; each is
    ``None`` for other errors. The reason is already sanitised of emails, configured
    secrets, the webhook URL and every secret :func:`send_json` holds for the call (its
    ``secrets`` and its requests' credentials); headers and bodies are never logged.

    ``will_retry`` says whether :func:`send_json` sends another attempt, by the retry test
    and attempt limit its retry loop applies under ``policy``. The level follows from it:

    * an attempt that will be retried is logged at debug level, because the ``http.retry``
      warning that follows already reports it;
    * a ``TransientError`` :func:`send_json` raises (its attempts ran out, or the platform
      asked for a wait beyond ``policy.max_retry_after_seconds``) is logged at warning
      level, as the transient failure that ended the call;
    * any other error :func:`send_json` raises, an ``AccessDenied`` or a ``QueryDefect``, is
      logged at info level: the owner of its consequence (``ads_engine.sync.mode``'s
      ``on_error``, which logs ``sync.classification`` at warning or error level, or the
      connector or CLI code that catches it) logs the failure's terminal record, so one
      failure never yields two records above info.
    """
    will_retry = attempt_number < policy.attempts and _retry_predicate(policy)(error)
    if will_retry:
        log = _log.debug
    elif isinstance(error, TransientError):
        log = _log.warning
    else:
        log = _log.info
    log(
        "connector.error",
        source=error.source,
        endpoint=error.endpoint,
        kind=error.kind,
        code=error.code,
        http_status=error.http_status,
        attempt=attempt_number,
        retryable=_is_retryable(error),
        reason=error.reason,
        retry_after=error.retry_after if isinstance(error, TransientError) else None,
        query_id=error.query_id if isinstance(error, QueryDefect) else None,
        will_retry=will_retry,
    )


def _raise_logged(error: ConnectorError, attempt_number: int, policy: RetryPolicy) -> NoReturn:
    """Log ``error`` as ``connector.error`` and raise it."""
    _log_error(error, attempt_number, policy)
    raise error
