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
nothing of the n8n capability URL but its host. The redaction processors of
:mod:`ads_engine.logging` are the second line of defence, not the first.

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
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from decimal import Context, Decimal, DecimalException, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, NoReturn
from urllib.parse import unquote

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
    sanitize_message,
)
from ads_engine.db.models.ops import SOURCES
from ads_engine.logging import EMAIL_RE, REDACTED, REDACTED_EMAIL, get_logger
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

# Query parameters whose whole value is a credential. Names are compared after the
# normalisation of `_normalise_key`.
_SENSITIVE_QUERY_KEYS: Final[frozenset[str]] = frozenset(
    {"access_token", "token", "client_secret", "refresh_token", "hapikey", "code"}
)
# Fail-closed widening, matching the key rule of ads_engine.logging: any parameter whose
# normalised name ends like a credential is redacted too. That includes LinkedIn's
# `pageToken` cursor, which carries no credential but loses nothing by being hidden.
_SENSITIVE_QUERY_SUFFIXES: Final[tuple[str, ...]] = ("_token", "_secret", "_password", "_api_key")
_CAMEL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Configured secrets shorter than this would mangle ordinary URL text (the local database
# password `ads` appears in `ads_engine`); ads_engine.logging applies the same floor.
_MIN_SECRET_LENGTH: Final = 8

# Longest non-JSON error body, in characters, that is sanitised whole and then used for
# classification and the error message. Redaction runs on the complete text before any
# cut, so no secret or address is ever split into a fragment it no longer recognises.
# The email pattern's cost grows quadratically with long address-like runs, so a longer
# body is never redacted whole: it is classified from a window of its first this many
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
    ``status`` and ``elapsed_ms`` to the response headers). Hooks log only the method and
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
    """Collect the configured secret plaintexts to strip from logged URLs, longest first.

    Connectors never put a credential in a URL; this guards against one that arrives
    there anyway, for example a token pasted into a configured URL.
    """
    if settings is None:
        try:
            settings = get_settings()
        except ValueError:
            # pydantic's ValidationError is a ValueError. The client still works; its hooks
            # keep the key and email rules, and the logging processors keep theirs.
            return ()
    plaintexts = {
        plaintext for plaintext in settings.secret_values() if len(plaintext) >= _MIN_SECRET_LENGTH
    }
    return tuple(sorted(plaintexts, key=lambda plaintext: (-len(plaintext), plaintext)))


# ---------------------------------------------------------------------------
# Redacted request logging
# ---------------------------------------------------------------------------


def redact_url(url: httpx.URL, *, source: Source, secrets: Collection[str] = ()) -> str:
    """Render a request URL for logging, with nothing sensitive left in it.

    * ``google``: only ``"{scheme}://{host}/<redacted>"``. The n8n webhook path is a
      capability URL: knowing it is enough to call the workflow.
    * ``linkedin`` and ``hubspot``: the raw path and query, with the value of every
      credential parameter (``access_token``, ``token``, ``client_secret``,
      ``refresh_token``, ``hapikey``, ``code``, and any name ending in ``_token``,
      ``_secret``, ``_password`` or ``_api_key`` once camelCase is split) replaced by
      ``[REDACTED]``. Beyond those names, a path segment or query value that holds an
      email address or one of ``secrets`` once percent-decoded is rendered decoded with
      ``[REDACTED_EMAIL]`` or ``[REDACTED]`` in its place, because the logging processors
      match plaintext only and would miss ``lead%40example.com``.

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
        secrets: Plaintext secrets to strip from path segments and query values, longest
            first; :func:`build_client` passes the configured secrets.

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


def _normalise_key(name: str) -> str:
    """Normalise a query parameter name: percent-decoded, camelCase split, lowercased."""
    decoded = unquote(name).strip()
    return _CAMEL_BOUNDARY_RE.sub("_", decoded).lower().replace("-", "_")


def _is_sensitive_key(name: str) -> bool:
    """Whether the query parameter ``name`` carries a credential."""
    normalised = _normalise_key(name)
    return normalised in _SENSITIVE_QUERY_KEYS or normalised.endswith(_SENSITIVE_QUERY_SUFFIXES)


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


def _scrub_text(text: str, secrets: Collection[str]) -> str:
    """Replace each secret, then each email address, inside ``text``."""
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    if "@" in text:
        text = EMAIL_RE.sub(REDACTED_EMAIL, text)
    return text


def _scrub_component(component: str, secrets: Collection[str]) -> str:
    """Return a path segment or query value, scrubbed of emails and secrets.

    The component is checked percent-decoded, where an encoded address becomes visible;
    when that finds something the decoded, scrubbed form is returned. Otherwise the raw
    form is checked as well, for a secret that itself contains ``%`` escapes, and the raw
    bytes are returned unchanged when it is clean.
    """
    decoded = unquote(component)
    scrubbed = _scrub_text(decoded, secrets)
    if scrubbed != decoded:
        return scrubbed
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
        """Log a response at info: method, URL, status and milliseconds to its headers."""
        request = response.request
        started = request.extensions.get(STARTED_EXTENSION)
        elapsed_ms = (
            round((time.monotonic() - started) * 1000.0, 1) if isinstance(started, float) else None
        )
        _log.info(
            "http.response",
            source=source,
            method=request.method,
            url=redact_url(request.url, source=source, secrets=secrets),
            status=response.status_code,
            elapsed_ms=elapsed_ms,
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
       builder raises itself is raised unchanged;
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
    ``retry_after`` and ``query_id``) before it is raised; headers and bodies never are.

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

    Returns:
        The accepted response with its decoded body.

    Raises:
        AccessDenied: The platform refused the credentials or their permissions.
        TransientError: A transient failure survived every attempt, or asked for a wait
            longer than ``policy.max_retry_after_seconds``.
        CallBudgetExhausted: ``before_attempt`` reported the call budget used up.
        QueryDefect: The request is wrong, the response is not JSON or holds an
            unrepresentable number, or its status was unexpected.
        ValueError: When ``source`` is unknown or ``accept_statuses`` is empty.
    """
    _require_source(source)
    accepted = frozenset(accept_statuses)
    if not accepted:
        raise ValueError("accept_statuses must name at least one HTTP status")
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
    """Build tenacity's ``before_sleep`` hook, which logs ``http.retry``."""

    def log_retry(retry_state: RetryCallState) -> None:
        """Log the failed attempt and the wait before the next one; never a body."""
        error = _failed_with(retry_state)
        connector_error = error if isinstance(error, ConnectorError) else None
        _log.info(
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


async def _send_once(
    client: httpx.AsyncClient,
    build_request: Callable[[], httpx.Request],
    *,
    source: Source,
    endpoint: str,
    accepted: frozenset[int],
    before_attempt: Callable[[], Awaitable[None]] | None,
    attempt_number: int,
) -> JsonResponse:
    """Run one attempt of :func:`send_json`: steps 1 to 6 of its docstring."""
    if before_attempt is not None:
        await before_attempt()
    try:
        request = build_request()
    except ConnectorError as raised:
        # A builder may raise a classified error itself; it keeps its kind.
        _log_error(raised, attempt_number)
        raise
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, TypeError, ValueError) as exc:
        # The request cannot be built: an invalid URL, a JSON body that does not serialise,
        # a header that does not encode. The same failure recurs on every attempt.
        defect = _construction_defect(exc, source=source, endpoint=endpoint)
        _log_error(defect, attempt_number)
        raise defect from exc
    # httpx's timeouts bound each network phase and each read, not the call: a body that
    # drips in faster than the read timeout would hold the call open indefinitely. The
    # deadline bounds the whole attempt, body included, since client.send reads the body.
    deadline_seconds = SOURCE_TIMEOUTS[source]
    deadline = asyncio.timeout(deadline_seconds)
    try:
        async with deadline:
            response = await client.send(request)
    except ConnectorError as raised:
        # A transport may raise a classified error itself, as the simulation's mock
        # transport does for an injected fault or a request that breaks its contract.
        _log_error(raised, attempt_number)
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
        _log_error(failure, attempt_number)
        raise failure from exc

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
            )
        if source == "google":
            # n8n answers HTTP 200 for Google denials and quota errors; the body decides.
            relayed = classify(response, body, source=source, endpoint=endpoint)
            if relayed is not None:
                _raise_logged(relayed, attempt_number)
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
    _raise_logged(classified, attempt_number)


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


def _log_error(error: ConnectorError, attempt_number: int) -> None:
    """Log a classified error as ``connector.error``, with its reason but never a body.

    The event carries source, endpoint, kind, code, HTTP status, attempt, retryability and
    :attr:`~ads_engine.connectors.errors.ConnectorError.reason`, which keeps failures that
    share every other field distinguishable. It also carries ``retry_after``, the wait a
    ``TransientError`` asked for, and ``query_id``, a ``QueryDefect``'s query id; each is
    ``None`` for other errors. The reason is already sanitised of emails, configured
    secrets and the webhook URL; headers and bodies are never logged.
    """
    _log.warning(
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
    )


def _raise_logged(error: ConnectorError, attempt_number: int) -> NoReturn:
    """Log ``error`` as ``connector.error`` and raise it."""
    _log_error(error, attempt_number)
    raise error
