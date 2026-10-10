"""Connector error taxonomy and response classification.

Every connector failure is raised as exactly one of three kinds, and
``ads_engine.sync.mode`` alone decides what each kind means for a source:

* :class:`AccessDenied` (``access_denied``): HTTP 401/403, an OAuth credential rejection,
  or a Google body carrying ``PERMISSION_DENIED``, ``UNAUTHENTICATED``,
  ``AuthorizationError`` or ``AuthenticationError``, even under HTTP 200 from n8n. The
  source switches to simulated data, with :attr:`ConnectorError.reason` as the reason.
* :class:`TransientError` (``transient``): HTTP 429, 5xx, timeouts, network failures,
  Google ``RESOURCE_EXHAUSTED``, and the product's own call budget
  (:class:`CallBudgetExhausted`). ``connectors/http.py`` retries it with backoff, honouring
  :attr:`TransientError.retry_after`; afterwards the affected datasets turn stale and keep
  their last good data. A transient error never switches a source to mock data.
* :class:`QueryDefect` (``defect``): HTTP 400 and every other 4xx, a GAQL ``QueryError``,
  a validation error, or a client-side misconfiguration. The job fails and is logged with
  its query id; there is no retry and no mock.

Google errors reach n8n's caller in two body shapes, both read here: the REST error
envelope ``{"error": {code: <HTTP status>, message, status: <google.rpc code name>,
details[]}}``, and a bare google.rpc Status ``{code: <numeric google.rpc code>, message,
details[]}`` relayed as the whole body. A bare Status code counts as its canonical name,
so 7 is ``PERMISSION_DENIED``, 16 ``UNAUTHENTICATED`` and 8 ``RESOURCE_EXHAUSTED``; every
other code classifies like the HTTP status ``google.rpc.Code`` maps it to.

Precedence is *access denied > transient > defect*, and body signals are read before the
HTTP status. Classification is pure: :func:`classify` and :func:`classify_exception`
perform no I/O, log nothing and never raise.

Every error message passes through :func:`sanitize_message` when the error is built, so
an exception's text never carries an email address, a configured secret, the n8n webhook
URL, a credential assigned to a sensitive key or a ``Bearer`` token, whether written
plainly or percent-encoded. That matters because :attr:`ConnectorError.reason` is
persisted as the source-mode reason, and HubSpot errors can echo the email that was
looked up. Secrets that exist only in memory, such as a LinkedIn access token from a
refresh exchange or a refresh token LinkedIn rotated, are unknown to the settings; code
that holds them builds its errors inside :func:`holding_secrets`, which strips them just
as thoroughly, even when a platform echoes one in ordinary prose.

Example:
    A connector decodes the body, classifies the response and raises what it gets::

        try:
            response = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise classify_exception(exc, source="google", endpoint="search Q-G2") from exc
        body = response.json()
        error = classify(response, body, source="google", endpoint="search Q-G2")
        if error is not None:
            raise error

This module is the leaf every connector imports. It depends only on the standard
library, httpx, pydantic's ``ValidationError``, :mod:`ads_engine.settings` and
:mod:`ads_engine.logging`.
"""

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, Final, Literal, NamedTuple
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from ads_engine.logging import REDACTED, redact, redact_emails
from ads_engine.settings import ConfigurationError, Source, get_settings

__all__ = [
    "AccessDenied",
    "CallBudgetExhausted",
    "ConnectorError",
    "ErrorKind",
    "QueryDefect",
    "TransientError",
    "classify",
    "classify_exception",
    "holding_secrets",
    "parse_retry_after",
    "sanitize_message",
    "unwrap_single_item",
]

ErrorKind = Literal["access_denied", "transient", "defect"]
"""The three consequences ``sync/mode.py`` distinguishes (the source fallback table)."""

_KIND_PRECEDENCE: Final[tuple[ErrorKind, ...]] = ("access_denied", "transient", "defect")

_MESSAGE_MAX: Final = 300
_REASON_MAX: Final = 500
_CODE_MAX: Final = 100
_ELLIPSIS: Final = "…"
_UNKNOWN_ENDPOINT: Final = "unknown"
_BUDGET_MESSAGE: Final = (
    "the product's own Google Ads webhook call budget (GOOGLE_DAILY_CALL_BUDGET per "
    "sliding 24 h) is used up; remaining calls are deferred"
)

# The plaintexts `holding_secrets` holds, in memory only. A ContextVar is task-local, so
# concurrent calls never strip, or learn, each other's secrets.
_HELD_SECRETS: Final[ContextVar[frozenset[str]]] = ContextVar(
    "ads_engine_held_secrets", default=frozenset()
)
# The shortest start of a held secret replaced at the end of text that was already cut;
# a shorter run is as likely to be ordinary text as a fragment of the secret.
_HELD_FRAGMENT_MIN: Final = 4

# Google body tokens, keyed by their normalised form (lowercase, `_` removed), with the
# kind they signal and the canonical code reported when no specific errorCode value exists.
# GoogleAdsFailure errorCode keys are camelCase in REST JSON (`authorizationError`), the
# google.rpc statuses are upper snake case (`PERMISSION_DENIED`); both normalise alike.
_GOOGLE_TOKENS: Final[Mapping[str, tuple[ErrorKind, str]]] = {
    "permissiondenied": ("access_denied", "PERMISSION_DENIED"),
    "unauthenticated": ("access_denied", "UNAUTHENTICATED"),
    "authorizationerror": ("access_denied", "AuthorizationError"),
    "authenticationerror": ("access_denied", "AuthenticationError"),
    "resourceexhausted": ("transient", "RESOURCE_EXHAUSTED"),
    # Beyond the listed tokens: QuotaError's temporary variant is the same retryable quota.
    "resourcetemporarilyexhausted": ("transient", "RESOURCE_TEMPORARILY_EXHAUSTED"),
    "queryerror": ("defect", "QueryError"),
    "invalidargument": ("defect", "INVALID_ARGUMENT"),
    "requesterror": ("defect", "RequestError"),
    "fielderror": ("defect", "FieldError"),
}

# The same tokens found inside free text such as "PERMISSION_DENIED: The caller does not
# have permission". Case-sensitive on purpose: prose like "unauthenticated user" is not a
# Google status. `\b` treats `_` as a word character, so USER_PERMISSION_DENIED (an
# errorCode value, classified through its key) does not match PERMISSION_DENIED here.
_GOOGLE_TOKEN_RE: Final = re.compile(
    r"\b(?:PERMISSION_DENIED|UNAUTHENTICATED|[Aa]uthorizationError|[Aa]uthenticationError"
    r"|RESOURCE_(?:TEMPORARILY_)?EXHAUSTED|INVALID_ARGUMENT|[Qq]ueryError|[Rr]equestError"
    r"|[Ff]ieldError)\b"
)

# google.rpc statuses that are transient only when they are exactly `error.status`;
# `INTERNAL` and `UNAVAILABLE` are ordinary words elsewhere.
_GOOGLE_TRANSIENT_STATUSES: Final = frozenset({"UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL"})

# google.rpc.Code (google/rpc/code.proto): each numeric error code with its canonical name
# and the HTTP status code.proto maps it to. A bare google.rpc Status that n8n relays as
# the whole body carries this number as its top-level `code`; 0 (OK) is not an error.
_GOOGLE_RPC_CODES: Final[Mapping[int, tuple[str, int]]] = {
    1: ("CANCELLED", 499),
    2: ("UNKNOWN", 500),
    3: ("INVALID_ARGUMENT", 400),
    4: ("DEADLINE_EXCEEDED", 504),
    5: ("NOT_FOUND", 404),
    6: ("ALREADY_EXISTS", 409),
    7: ("PERMISSION_DENIED", 403),
    8: ("RESOURCE_EXHAUSTED", 429),
    9: ("FAILED_PRECONDITION", 400),
    10: ("ABORTED", 409),
    11: ("OUT_OF_RANGE", 400),
    12: ("UNIMPLEMENTED", 501),
    13: ("INTERNAL", 500),
    14: ("UNAVAILABLE", 503),
    15: ("DATA_LOSS", 500),
    16: ("UNAUTHENTICATED", 401),
}

# The error-bearing keys of a body that also carries `results` or `fieldMask`: Google's
# `error`/`errors` and the keys of n8n's error envelope. Row payloads are never scanned, so
# a campaign named "PERMISSION_DENIED test" cannot be misread as a denial.
_GOOGLE_ERROR_KEYS: Final[tuple[str, ...]] = (
    "error",
    "errors",
    "message",
    "description",
    "httpCode",
    "code",
    "status",
    "cause",
    "details",
)
_GOOGLE_SUCCESS_KEYS: Final[tuple[str, ...]] = ("results", "fieldMask")

# Bounds on the body walk, so classification stays cheap whatever arrives.
_MAX_SCAN_DEPTH: Final = 12
_MAX_SCAN_NODES: Final = 5000
_MAX_SCAN_TEXT: Final = 4000

# OAuth 2.0 token-endpoint errors (RFC 6749 section 5.2) that reject the credential itself.
_OAUTH_DENIED_ERRORS: Final = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})
# LinkedIn reports an expired or revoked refresh token as 400 `invalid_request` with
# "...is invalid, expired or revoked"; that is a credential rejection, not a defect.
_OAUTH_REVOKED_RE: Final = re.compile(r"\b(?:expired|revoked)\b", re.IGNORECASE)

# Retry-After delta-seconds: an integer or a decimal, never signed.
_DELTA_SECONDS_RE: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# GoogleAdsFailure quotaErrorDetails.retryDelay, a protobuf Duration such as "30s".
_DURATION_RE: Final = re.compile(r"([0-9]+(?:\.[0-9]+)?)s")


class ConnectorError(Exception):
    """Base of every error a connector raises; instantiate a subclass.

    The message is sanitised on construction, so neither ``str(error)`` nor
    :attr:`reason` can carry an email address, a configured secret, a secret held by
    :func:`holding_secrets` while the error is built, or the webhook URL.

    Attributes:
        kind: The consequence class, set per subclass.
        retryable: Whether ``connectors/http.py`` retries the error with backoff.
        message: The sanitised platform message, at most 300 characters.
        source: The source whose call failed.
        endpoint: The contract surface name of the call, such as ``search``,
            ``ad_campaigns``, ``lead_form_responses``, ``associations_batch_read``,
            ``properties`` or ``access_token``, optionally followed by a query id
            (``search Q-G2``). The names match ``backend/tests/contracts/{source}/*.json``.
        code: The platform's error code (``USER_PERMISSION_DENIED``, ``MISSING_SCOPES``,
            ``RATE_LIMIT``, ``TIMEOUT``), or ``None`` when the platform sent none.
        http_status: The HTTP status of the response, or ``None`` when no response
            arrived.
    """

    kind: ClassVar[ErrorKind]
    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        source: Source,
        endpoint: str,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if not hasattr(type(self), "kind"):
            raise TypeError(
                "ConnectorError is abstract; raise AccessDenied, TransientError or QueryDefect"
            )
        self.message: str = sanitize_message(message)
        self.source: Source = source
        self.endpoint: str = " ".join(endpoint.split()) or _UNKNOWN_ENDPOINT
        self.code: str | None = _clean_code(code)
        self.http_status: int | None = http_status
        super().__init__(self.message)

    @property
    def reason(self) -> str:
        """One line naming the source, the failing endpoint, the code and the message.

        Shaped ``"{source} {endpoint}: {code or 'HTTP ' + status}: {message}"`` and at most
        500 characters. ``sync/mode.py`` stores it as the source-mode reason, for example
        ``"linkedin lead_form_responses: ACCESS_DENIED: Not enough permissions"``.
        """
        if self.code:
            label: str | None = self.code
        elif self.http_status is not None:
            label = f"HTTP {self.http_status}"
        else:
            label = None
        head = f"{self.source} {self.endpoint}"
        text = f"{head}: {label}: {self.message}" if label else f"{head}: {self.message}"
        return _truncate(text, _REASON_MAX)

    @property
    def is_unauthorized(self) -> bool:
        """Whether the response was HTTP 401.

        The LinkedIn client refreshes its access token once on a 401 and retries, before
        letting a second 401 propagate as access denied.
        """
        return self.http_status == 401

    @property
    def log_fields(self) -> dict[str, object]:
        """Structured logging fields for the error; they contain no personal data.

        Example::

            log.warning("sync_call_failed", job="metrics", **error.log_fields)
        """
        return {
            "error_kind": self.kind,
            "source": self.source,
            "endpoint": self.endpoint,
            "error_code": self.code,
            "http_status": self.http_status,
            "reason": self.reason,
        }

    def _init_kwargs(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "source": self.source,
            "endpoint": self.endpoint,
            "code": self.code,
            "http_status": self.http_status,
        }

    def __reduce__(self) -> tuple[Any, ...]:
        """Support pickling and copying despite the keyword-only constructor."""
        return (_restore_error, (type(self), self._init_kwargs()))

    def __str__(self) -> str:
        """Return :attr:`reason`."""
        return self.reason

    def __repr__(self) -> str:
        """Return the class name and :attr:`reason`."""
        return f"{type(self).__name__}({self.reason!r})"


# AccessDenied, CallBudgetExhausted and QueryDefect keep the names every connector, the
# source-mode controller and the tests import, so pep8-naming's N818 rule (an "Error"
# suffix on exception names) is waived for exactly these three classes.
class AccessDenied(ConnectorError):  # noqa: N818 - name fixed by the connector contract
    """The platform refused the credentials or their permissions.

    ``sync/mode.py`` switches the whole source to simulated data and records
    :attr:`reason`, which names the failing endpoint and the platform's error code.
    """

    kind: ClassVar[ErrorKind] = "access_denied"


class TransientError(ConnectorError):
    """A failure expected to clear on its own: rate limits, 5xx, timeouts, the network.

    Attributes:
        retry_after: Seconds the platform asked the client to wait (``Retry-After`` or a
            Google ``retryDelay``), or ``None``. Negative or non-finite values are
            dropped.
    """

    kind: ClassVar[ErrorKind] = "transient"
    retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        source: Source,
        endpoint: str,
        code: str | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message, source=source, endpoint=endpoint, code=code, http_status=http_status
        )
        self.retry_after: float | None = _clean_seconds(retry_after)

    @property
    def log_fields(self) -> dict[str, object]:
        """Structured logging fields, including ``retry_after``."""
        return {**super().log_fields, "retry_after": self.retry_after}

    def _init_kwargs(self) -> dict[str, Any]:
        return {**super()._init_kwargs(), "retry_after": self.retry_after}


class CallBudgetExhausted(TransientError):  # noqa: N818 - name fixed by the connector contract
    """The product's own Google webhook call cap is used up.

    Raised by ``sync/budget_guard.GoogleCallBudget.acquire()`` before a call is made,
    when ``GOOGLE_DAILY_CALL_BUDGET`` calls (1,000 by default) have been made in the
    sliding 24 h. The cap is separate from, and below, the developer token's quota, which
    other n8n workflows share. ``connectors/http.py`` never retries it: the remaining
    pages are deferred, and the affected datasets stay incomplete until they finish.
    """

    retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str = _BUDGET_MESSAGE,
        *,
        source: Source = "google",
        endpoint: str = "search",
        code: str | None = "CALL_BUDGET_EXHAUSTED",
        http_status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            source=source,
            endpoint=endpoint,
            code=code,
            http_status=http_status,
            retry_after=retry_after,
        )


class QueryDefect(ConnectorError):  # noqa: N818 - name fixed by the connector contract
    """A request the platform will never accept as sent: a defect in the product.

    The job fails and is logged with the query id; there is no retry and no mock.

    Attributes:
        query_id: The GAQL query id (``Q-G2``) or other request id, when known.
    """

    kind: ClassVar[ErrorKind] = "defect"

    def __init__(
        self,
        message: str,
        *,
        source: Source,
        endpoint: str,
        code: str | None = None,
        http_status: int | None = None,
        query_id: str | None = None,
    ) -> None:
        super().__init__(
            message, source=source, endpoint=endpoint, code=code, http_status=http_status
        )
        cleaned_query_id = " ".join(query_id.split()) if query_id else ""
        self.query_id: str | None = cleaned_query_id or None

    @property
    def log_fields(self) -> dict[str, object]:
        """Structured logging fields, including ``query_id``."""
        return {**super().log_fields, "query_id": self.query_id}

    def _init_kwargs(self) -> dict[str, Any]:
        return {**super()._init_kwargs(), "query_id": self.query_id}


def _restore_error(cls: type[ConnectorError], kwargs: dict[str, Any]) -> ConnectorError:
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, marking a cut with an ellipsis."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _clean_code(code: str | None) -> str | None:
    """Sanitise a platform error code to one short line; blank becomes ``None``."""
    if code is None:
        return None
    cleaned = _truncate(sanitize_message(code), _CODE_MAX)
    return cleaned or None


def _clean_seconds(value: float | None) -> float | None:
    """Return ``value`` as seconds when it is finite and not negative, else ``None``."""
    if value is None:
        return None
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _configured_secrets() -> tuple[str, ...]:
    """Collect the plaintexts to strip from messages, from the loaded settings.

    That is every value of ``Settings.credential_values()``, each set ``SecretStr`` and the
    LinkedIn client id, and beyond them the n8n webhook URL and its path: the URL is a
    capability URL that ``ads_engine.logging`` never logs either. When the settings cannot
    load, nothing is collected, and only the rules that need no configured secret apply:
    emails, URL passwords, sensitive assignments and ``Bearer`` tokens, plain or
    percent-encoded.
    """
    try:
        settings = get_settings()
    except ValidationError, ConfigurationError:
        return ()
    values = list(settings.credential_values())
    webhook_url = settings.n8n_google_ads_webhook_url
    if webhook_url:
        values.append(webhook_url)
        try:
            path = urlsplit(webhook_url).path
        except ValueError:
            path = ""
        if path.strip("/"):
            values.append(path)
    return tuple(dict.fromkeys(values))


@contextmanager
def holding_secrets(values: Iterable[str]) -> Iterator[None]:
    """Strip ``values`` from every error message built inside the block.

    Some secrets exist only in memory, so the settings :func:`sanitize_message` reads do
    not know them: an access token LinkedIn issued in a refresh exchange, a refresh token
    it rotated, the credentials a request carries. While the block runs,
    :func:`sanitize_message`, and with it every :class:`ConnectorError` constructor,
    :func:`classify` and :func:`classify_exception`, removes each held value exactly as it
    removes a configured secret: whatever its length, plain or percent-encoded, and before
    the message is cut, so a long token is never left half-matched.

    Blocks nest: an inner block holds its values together with those of the blocks around
    it, and every block restores what was held before it when it ends, also when it
    raises. The held set is task-local (a :class:`contextvars.ContextVar`), so concurrent
    tasks never see each other's secrets, and it lives in memory only. Blank values are
    ignored.

    Example::

        with holding_secrets([access_token]):
            error = classify(response, body, source="linkedin", endpoint="ad_account")

    Args:
        values: The plaintexts to hold.

    Yields:
        Nothing; the values stay held until the block ends.

    Raises:
        TypeError: When ``values`` is a single string, which would otherwise be read as
            one-character secrets, or holds anything but strings.
    """
    if isinstance(values, str | bytes):
        raise TypeError("values must be a collection of strings, not a single string")
    plaintexts = list(values)
    if not all(isinstance(value, str) for value in plaintexts):
        raise TypeError("values must hold strings only")
    added = frozenset(value for value in plaintexts if value.strip())
    previous = _HELD_SECRETS.set(_HELD_SECRETS.get() | added)
    try:
        yield
    finally:
        _HELD_SECRETS.reset(previous)


def _redact_held_fragment(text: str, held: frozenset[str]) -> str:
    """Replace the start of a held secret that ends already-cut ``text`` with ``[REDACTED]``.

    Text ending in the ellipsis :func:`_truncate` appends was cut before, for example the
    message of an error built before a secret was held and rebuilt inside
    :func:`holding_secrets`. That cut may have gone through the secret, leaving a start of
    it that no longer matches it whole. The longest such start right before the ellipsis,
    of at least :data:`_HELD_FRAGMENT_MIN` characters, is replaced.
    """
    if not held or not text.endswith(_ELLIPSIS):
        return text
    body = text[: -len(_ELLIPSIS)]
    longest = 0
    for secret in held:
        floor = max(longest, _HELD_FRAGMENT_MIN - 1)
        for size in range(min(len(secret), len(body)), floor, -1):
            if body.endswith(secret[:size]):
                longest = size
                break
    if not longest:
        return text
    return f"{body[: len(body) - longest]}{REDACTED}{_ELLIPSIS}"


def sanitize_message(text: str) -> str:
    r"""Make platform or exception text safe to persist and log.

    Email addresses in any script, their local parts read fail-closed (``用户!@example.com``
    whole), and values assigned to keys that name one (``email=...``) become
    ``[REDACTED_EMAIL]``; URL passwords, every configured secret and every secret held by
    :func:`holding_secrets` (whatever its length), values assigned to sensitive keys
    (``access_token=...``, ``Authorization: ...``, ``{"refresh_token": "..."}``) and
    ``Bearer`` tokens become ``[REDACTED]``, exactly as
    :func:`ads_engine.logging.redact` does for log lines; the n8n webhook URL is removed
    too. Percent-encoded forms are covered as well, also when encoded twice:
    ``lead%40example.com`` and ``lead%2540example.com`` become ``[REDACTED_EMAIL]``.
    Whitespace runs collapse to one space, and the result is cut to 300 characters.
    Redaction runs before the collapse, so a header value still ends at its line, and the
    email scanner runs again after it, so no quoted local part that the collapse joins
    across a line break (``"john\ndoe"@example.com``) survives; both run before the cut, so
    a secret is never left half-matched. Text that was already cut, and so ends in ``…``,
    also loses a start of a held secret right before that ellipsis, of 4 characters or
    more: what an earlier cut through the secret left.

    Example::

        >>> sanitize_message("No contact with email lead@example.com\n found")
        'No contact with email [REDACTED_EMAIL] found'

    Args:
        text: Raw text, for example a HubSpot error message.

    Returns:
        The sanitised single-line text.
    """
    held = _HELD_SECRETS.get()
    secrets = (*_configured_secrets(), *sorted(held))
    collapsed = " ".join(str(redact(text, secrets=secrets)).split())
    # A quoted local part may hold blanks but no line break, so collapsing can join the two
    # halves of one that a line break kept apart (`"john` and `doe"@example.com`): the
    # single-line text is scanned for addresses again.
    redacted = redact_emails(collapsed)
    return _truncate(_redact_held_fragment(redacted, held), _MESSAGE_MAX)


def unwrap_single_item(body: object) -> object:
    """Unwrap n8n's "all incoming items" response shape.

    n8n can answer with a one-element JSON array wrapping the Google response object;
    the Google client and :func:`classify` both read the object inside it.

    Args:
        body: The decoded JSON body.

    Returns:
        The single element when ``body`` is a list of exactly one element; otherwise
        ``body`` unchanged.
    """
    if isinstance(body, list) and len(body) == 1:
        return body[0]
    return body


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` value into seconds.

    Example::

        >>> parse_retry_after("7")
        7.0
        >>> parse_retry_after(
        ...     "Wed, 07 Oct 2026 12:00:30 GMT",
        ...     now=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        ... )
        30.0

    Args:
        value: The header value: delta-seconds (an integer or decimal) or an HTTP-date.
        now: The reference instant for an HTTP-date; the current UTC time when ``None``.
            A naive value is read as UTC.

    Returns:
        The delay in seconds, or ``None`` when the value is absent, unparsable, signed,
        not finite, or a date in the past.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if _DELTA_SECONDS_RE.fullmatch(text):
        return _clean_seconds(float(text))
    try:
        when = parsedate_to_datetime(text)
    except TypeError, ValueError, IndexError, OverflowError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    reference = now if now is not None else datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return _clean_seconds((when - reference).total_seconds())


# ---------------------------------------------------------------------------
# Body inspection
# ---------------------------------------------------------------------------


class _Signal(NamedTuple):
    """One error token found in a body.

    ``specific`` marks a GoogleAdsFailure ``errorCode`` value such as
    ``USER_PERMISSION_DENIED``, preferred as the reported code over a bare token.
    """

    kind: ErrorKind
    code: str
    specific: bool


def _normalise_token(text: str) -> str:
    """Normalise a key or value for token comparison: lowercase, ``_`` removed."""
    return text.strip().lower().replace("_", "")


def _walk(roots: Sequence[object]) -> Iterator[tuple[str | None, object]]:
    """Yield ``(key, node)`` for every node under ``roots``, in document order.

    ``key`` is the mapping key the node sits under, or ``None`` for roots and list items.
    The walk stops at :data:`_MAX_SCAN_DEPTH` levels and :data:`_MAX_SCAN_NODES` nodes.
    """
    stack: list[tuple[str | None, object, int]] = [(None, root, 0) for root in reversed(roots)]
    visited = 0
    while stack and visited < _MAX_SCAN_NODES:
        key, node, depth = stack.pop()
        visited += 1
        yield key, node
        if depth >= _MAX_SCAN_DEPTH:
            continue
        if isinstance(node, Mapping):
            entries = [
                (child_key if isinstance(child_key, str) else None, child, depth + 1)
                for child_key, child in node.items()
            ]
            stack.extend(reversed(entries))
        elif isinstance(node, list | tuple):
            stack.extend((None, child, depth + 1) for child in reversed(node))


def _google_scan_roots(body: object, *, depth: int = 0) -> list[object]:
    """Return the error-bearing parts of a Google (n8n) body.

    A mapping carrying ``results`` or ``fieldMask`` is a success payload: only its
    ``error``/``errors`` subtrees and n8n error keys are returned, never its rows. Any
    other mapping, a string, or the items of a multi-item n8n list are returned whole.
    """
    item = unwrap_single_item(body) if depth == 0 else body
    if isinstance(item, Mapping):
        if any(key in item for key in _GOOGLE_SUCCESS_KEYS):
            return [item[key] for key in _GOOGLE_ERROR_KEYS if key in item]
        return [item]
    if isinstance(item, str):
        return [item]
    if isinstance(item, list) and depth < 2:
        roots: list[object] = []
        for element in item:
            roots.extend(_google_scan_roots(element, depth=depth + 1))
        return roots
    return []


def _text_signals(text: str) -> list[_Signal]:
    """Find Google tokens in a string value: the whole value, else tokens inside it."""
    token = _GOOGLE_TOKENS.get(_normalise_token(text))
    if token is not None:
        return [_Signal(token[0], token[1], specific=False)]
    signals: list[_Signal] = []
    for match in _GOOGLE_TOKEN_RE.finditer(text, 0, _MAX_SCAN_TEXT):
        found = _GOOGLE_TOKENS.get(_normalise_token(match.group(0)))
        if found is not None:
            signals.append(_Signal(found[0], found[1], specific=False))
    return signals


def _google_rpc_status_signal(item: object) -> _Signal | None:
    """Read the code of a bare google.rpc Status ``{code, message, details[]}``.

    n8n can relay a Google error as a bare Status forming the whole body, under any HTTP
    status. ``item`` is the unwrapped body; it is a bare Status when it is a mapping with
    neither ``results`` nor ``fieldMask`` whose top-level ``code`` is a google.rpc.Code
    from 1 to 16, as an int or a digit string. The signal reports the code's canonical
    name with the kind of the HTTP status code.proto maps it to: 7 ``PERMISSION_DENIED``
    (403) and 16 ``UNAUTHENTICATED`` (401) deny access, 8 ``RESOURCE_EXHAUSTED`` (429) and
    14 ``UNAVAILABLE`` (503) are transient, 3 ``INVALID_ARGUMENT`` (400) is a defect.

    Returns ``None`` for a success payload, whose ``code`` is never read, for 0 (``OK``),
    and for any other value, such as the HTTP status in n8n's own
    ``{"code": 403, "message": ...}`` envelope.
    """
    if not isinstance(item, Mapping) or any(key in item for key in _GOOGLE_SUCCESS_KEYS):
        return None
    number = _as_int(item.get("code"))
    rpc_code = _GOOGLE_RPC_CODES.get(number) if number is not None else None
    if rpc_code is None:
        return None
    name, http_status = rpc_code
    kind = _kind_for_status(http_status)
    return _Signal(kind, name, specific=False) if kind is not None else None


def _google_signals(item: object, roots: Sequence[object]) -> list[_Signal]:
    """Collect every Google error signal of a body, in reporting order.

    First a bare google.rpc Status code, the Status's own code; then every error token in
    ``roots`` in document order; then an exact transient ``error.status``.
    """
    signals: list[_Signal] = []
    rpc_status = _google_rpc_status_signal(item)
    if rpc_status is not None:
        signals.append(rpc_status)
    for key, node in _walk(roots):
        if key is not None:
            token = _GOOGLE_TOKENS.get(_normalise_token(key))
            if token is not None:
                # A GoogleAdsFailure errorCode entry: {"authorizationError": "USER_..."}.
                value = node.strip() if isinstance(node, str) else ""
                signals.append(_Signal(token[0], value or token[1], specific=bool(value)))
        if isinstance(node, str):
            signals.extend(_text_signals(node))
    error = item.get("error") if isinstance(item, Mapping) else None
    status = error.get("status") if isinstance(error, Mapping) else None
    if isinstance(status, str) and status.strip() in _GOOGLE_TRANSIENT_STATUSES:
        signals.append(_Signal("transient", status.strip(), specific=False))
    return signals


def _select_signal(signals: Sequence[_Signal]) -> tuple[ErrorKind, str] | None:
    """Pick the highest-precedence kind, reporting its most specific code."""
    for kind in _KIND_PRECEDENCE:
        of_kind = [signal for signal in signals if signal.kind == kind]
        if of_kind:
            chosen = next((signal for signal in of_kind if signal.specific), of_kind[0])
            return kind, chosen.code
    return None


def _as_int(value: object) -> int | None:
    """Read an integer from a body field: an int (never a bool) or an unsigned digit string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            # isdigit() also accepts superscript digits and digit runs longer than the
            # interpreter's int conversion limit, which int() rejects; neither is a number.
            return None
    return None


def _as_status(value: object) -> int | None:
    """Read an HTTP status from a body field: an int or a digit string in 100..599."""
    status = _as_int(value)
    return status if status is not None and 100 <= status <= 599 else None


def _google_body_status(item: object) -> int | None:
    """Read the status an n8n or Google error envelope carries in its body.

    Checked in order: ``error.code`` (the HTTP code of a google.rpc Status in REST JSON),
    ``error.httpCode`` and n8n's top-level ``httpCode``.
    """
    if not isinstance(item, Mapping):
        return None
    error = item.get("error")
    candidates: list[object] = []
    if isinstance(error, Mapping):
        candidates.extend((error.get("code"), error.get("httpCode")))
    candidates.append(item.get("httpCode"))
    for candidate in candidates:
        status = _as_status(candidate)
        if status is not None:
            return status
    return None


def _google_retry_delay(roots: Sequence[object]) -> float | None:
    """Read GoogleAdsFailure ``quotaErrorDetails.retryDelay`` (``"30s"``) as seconds."""
    for key, node in _walk(roots):
        if key == "retryDelay" and isinstance(node, str):
            match = _DURATION_RE.fullmatch(node.strip())
            if match is not None:
                return _clean_seconds(float(match.group(1)))
    return None


def _oauth_denial_code(item: object) -> str | None:
    """Return the OAuth 2.0 error code when the body rejects the credential itself.

    ``invalid_grant``, ``invalid_client`` and ``unauthorized_client`` always do; so does
    ``invalid_request`` whose description says the grant or token is expired or revoked,
    which is how LinkedIn reports a revoked refresh token.
    """
    if not isinstance(item, Mapping):
        return None
    error = item.get("error")
    if not isinstance(error, str) or not error.strip():
        return None
    code = error.strip()
    if code.lower() in _OAUTH_DENIED_ERRORS:
        return code
    description = item.get("error_description")
    if (
        code.lower() == "invalid_request"
        and isinstance(description, str)
        and _OAUTH_REVOKED_RE.search(description) is not None
    ):
        return code
    return None


def _first_text(candidates: Sequence[object]) -> str | None:
    """Return the first non-blank string among ``candidates``."""
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


def _platform_code(item: object, source: Source) -> str | None:
    """Read the platform's error code from a decoded error body.

    OAuth bodies give ``error``; Google gives ``error.status``; LinkedIn gives ``code``,
    else ``serviceErrorCode``; HubSpot gives ``category``, else ``errorType`` or
    ``subCategory``.
    """
    if not isinstance(item, Mapping):
        return None
    error = item.get("error")
    if isinstance(error, str) and error.strip():
        return error
    if source == "google":
        status = error.get("status") if isinstance(error, Mapping) else None
        return _first_text([status])
    if source == "linkedin":
        code = _first_text([item.get("code")])
        if code is not None:
            return code
        service_code = item.get("serviceErrorCode")
        if isinstance(service_code, int) and not isinstance(service_code, bool):
            return str(service_code)
        return _first_text([service_code])
    return _first_text([item.get("category"), item.get("errorType"), item.get("subCategory")])


def _platform_message(item: object) -> str | None:
    """Read the platform's error message from a decoded body or short text body."""
    if isinstance(item, str):
        return item if item.strip() else None
    if not isinstance(item, Mapping):
        return None
    candidates: list[object] = []
    error = item.get("error")
    if isinstance(error, Mapping):
        candidates.append(error.get("message"))
    candidates.extend((item.get("error_description"), item.get("message"), item.get("description")))
    errors = item.get("errors")
    if isinstance(errors, list):
        candidates.extend(entry.get("message") for entry in errors if isinstance(entry, Mapping))
    return _first_text(candidates)


def _default_message(response: httpx.Response) -> str:
    """Describe a response whose body carried no message."""
    phrase = response.reason_phrase.strip()
    if response.status_code >= 400:
        return phrase or "no error message in the response"
    return "error reported in the response body"


def _kind_for_status(status: int) -> ErrorKind | None:
    """Map an HTTP status to its kind: 401/403 denied, 429/5xx transient, 4xx defect."""
    if status in (401, 403):
        return "access_denied"
    if status == 429 or 500 <= status <= 599:
        return "transient"
    if 400 <= status <= 499:
        return "defect"
    return None


def _query_id(endpoint: str) -> str | None:
    """Return the query id suffix of an endpoint such as ``search Q-G2``."""
    parts = endpoint.split(maxsplit=1)
    return parts[1] if len(parts) == 2 else None


def _build(
    kind: ErrorKind,
    *,
    response: httpx.Response,
    item: object,
    roots: Sequence[object],
    source: Source,
    endpoint: str,
    code: str | None,
) -> ConnectorError:
    """Build the error of ``kind`` for ``response``."""
    message = _platform_message(item) or _default_message(response)
    status = response.status_code
    if kind == "access_denied":
        return AccessDenied(
            message, source=source, endpoint=endpoint, code=code, http_status=status
        )
    if kind == "transient":
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is None and roots:
            retry_after = _google_retry_delay(roots)
        return TransientError(
            message,
            source=source,
            endpoint=endpoint,
            code=code,
            http_status=status,
            retry_after=retry_after,
        )
    return QueryDefect(
        message,
        source=source,
        endpoint=endpoint,
        code=code,
        http_status=status,
        query_id=_query_id(endpoint),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    response: httpx.Response, body: object, *, source: Source, endpoint: str
) -> ConnectorError | None:
    """Classify a received response as access denied, transient, defect or no error.

    Rules, in order (precedence *access denied > transient > defect*, body before status):

    1. OAuth credential rejection in the body (``invalid_grant``, ``invalid_client``,
       ``unauthorized_client``, or ``invalid_request`` for an expired or revoked token):
       access denied. Read on any 4xx, and on every Google response, because n8n relays
       errors under any status.
    2. Google only, on the error-bearing parts of the unwrapped body: denial tokens
       (``PERMISSION_DENIED``, ``UNAUTHENTICATED``, ``AuthorizationError``,
       ``AuthenticationError``) even under HTTP 200; then transient (``RESOURCE_EXHAUSTED``,
       or ``error.status`` exactly ``UNAVAILABLE``, ``DEADLINE_EXCEEDED`` or ``INTERNAL``);
       then defect (``QueryError``, ``INVALID_ARGUMENT``, ``requestError``,
       ``fieldError``). A successful payload's rows are never read.

       A bare google.rpc Status joins those signals: an unwrapped body with neither
       ``results`` nor ``fieldMask`` whose top-level ``code`` is a google.rpc.Code from 1
       to 16 (an int or a digit string). It reports the code's canonical name with the
       kind of the HTTP status code.proto maps it to: 7 ``PERMISSION_DENIED`` (403) and 16
       ``UNAUTHENTICATED`` (401) access denied, even under HTTP 200; 8
       ``RESOURCE_EXHAUSTED`` (429) and the 5xx codes 2, 4, 12, 13, 14 and 15 transient;
       3 ``INVALID_ARGUMENT`` (400) and the other 4xx codes 1, 5, 6, 9, 10 and 11 a
       defect. A GoogleAdsFailure ``errorCode`` value of the chosen kind is still the
       reported code. Any other top-level ``code`` (0 ``OK``, n8n's HTTP
       ``{"code": 403, ...}``) is no signal.
    3. Google only: a numeric ``error.code`` / ``error.httpCode`` / ``httpCode`` in the
       body maps like an HTTP status.
    4. The HTTP status: 401/403 access denied; 429 and 5xx transient, with
       ``Retry-After`` honoured; 400 and every other 4xx a defect.

    A 1xx, 2xx or 3xx response with no body signal gives ``None``; the caller decides
    whether an unexpected success status (a 207 it does not accept, a redirect) is a
    defect. A LinkedIn 401 is returned as access denied with ``is_unauthorized`` set; the
    LinkedIn client refreshes its token once before letting a second 401 propagate.

    Example::

        >>> response = httpx.Response(429, headers={"Retry-After": "7"})
        >>> error = classify(response, None, source="hubspot", endpoint="crm_search")
        >>> (error.kind, error.retry_after)
        ('transient', 7.0)

    Args:
        response: The received response; only its status and headers are read.
        body: The decoded JSON body, ``None``, or a short text when it was not JSON.
        source: The source called.
        endpoint: The contract surface name, optionally with a query id (``search Q-G6``).

    Returns:
        The error to raise, or ``None`` when the response is not an error.
    """
    status = response.status_code
    item = unwrap_single_item(body)
    is_google = source == "google"
    roots = _google_scan_roots(body) if is_google else []

    if status >= 400 or is_google:
        oauth_code = _oauth_denial_code(item)
        if oauth_code is not None:
            return _build(
                "access_denied",
                response=response,
                item=item,
                roots=roots,
                source=source,
                endpoint=endpoint,
                code=oauth_code,
            )

    if is_google:
        selected = _select_signal(_google_signals(item, roots))
        if selected is not None:
            kind, code = selected
            return _build(
                kind,
                response=response,
                item=item,
                roots=roots,
                source=source,
                endpoint=endpoint,
                code=code,
            )
        body_status = _google_body_status(item)
        body_kind = _kind_for_status(body_status) if body_status is not None else None
        if body_kind is not None:
            return _build(
                body_kind,
                response=response,
                item=item,
                roots=roots,
                source=source,
                endpoint=endpoint,
                code=_platform_code(item, source),
            )

    status_kind = _kind_for_status(status)
    if status_kind is None:
        return None
    return _build(
        status_kind,
        response=response,
        item=item,
        roots=roots,
        source=source,
        endpoint=endpoint,
        code=_platform_code(item, source),
    )


def classify_exception(exc: Exception, *, source: Source, endpoint: str) -> ConnectorError:
    """Classify an exception raised before a usable response arrived.

    * :class:`httpx.TimeoutException` (and the built-in :class:`TimeoutError`):
      transient, code ``TIMEOUT``.
    * :class:`httpx.UnsupportedProtocol` and :class:`httpx.LocalProtocolError`: a
      defect, code ``CLIENT``. httpx files both under ``TransportError``, but a wrong URL
      scheme or a request the client itself cannot encode never clears on retry.
    * Every other :class:`httpx.TransportError` (connect, read, write, remote protocol,
      proxy) and :class:`httpx.DecodingError`: transient, code ``NETWORK``.
    * :class:`httpx.HTTPStatusError`: classified from its response status, as
      :func:`classify` does with no body.
    * Anything else (:class:`httpx.InvalidURL`, :class:`httpx.TooManyRedirects`, a stream
      misuse, a non-httpx exception): a defect, code ``CLIENT``.
    * A :class:`ConnectorError` is returned unchanged.

    The message is the exception class name plus its sanitised text; request headers
    and bodies are never included, and an ``HTTPStatusError``'s URL is left out.

    Args:
        exc: The exception the HTTP call raised.
        source: The source called.
        endpoint: The contract surface name, optionally with a query id.

    Returns:
        The error to raise in place of ``exc`` (``raise error from exc``).
    """
    if isinstance(exc, ConnectorError):
        return exc
    name = type(exc).__name__
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        classified = classify(response, None, source=source, endpoint=endpoint)
        if classified is not None:
            return classified
        message = f"{name}: HTTP {response.status_code} {response.reason_phrase}"
        return QueryDefect(
            message,
            source=source,
            endpoint=endpoint,
            code="CLIENT",
            http_status=response.status_code,
            query_id=_query_id(endpoint),
        )
    detail = str(exc)
    message = f"{name}: {detail}" if detail.strip() else name
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return TransientError(message, source=source, endpoint=endpoint, code="TIMEOUT")
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.LocalProtocolError):
        return QueryDefect(
            message, source=source, endpoint=endpoint, code="CLIENT", query_id=_query_id(endpoint)
        )
    if isinstance(exc, httpx.TransportError | httpx.DecodingError):
        return TransientError(message, source=source, endpoint=endpoint, code="NETWORK")
    return QueryDefect(
        message, source=source, endpoint=endpoint, code="CLIENT", query_id=_query_id(endpoint)
    )
