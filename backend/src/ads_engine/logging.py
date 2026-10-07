"""Structured JSON logging with secret, token and email redaction.

Every process (``api``, ``worker``, the ``ads-engine`` CLI) calls
:func:`configure_logging` once at startup. After that, structlog events and records from
third-party standard-library loggers (``uvicorn``, ``uvicorn.access``, ``sqlalchemy``,
``alembic``, ``httpx``) are written as one JSON object per line, and every line passes
through the same redaction before it is rendered:

* values under sensitive keys (``Authorization``, cookies, passwords, tokens, client
  secrets, the n8n secret header, and the configured ``N8N_WEBHOOK_SECRET_HEADER``)
  become ``[REDACTED]``, including ``(name, value)`` header pairs;
* :class:`pydantic.SecretStr` values become ``[REDACTED]``;
* the plaintext of every configured secret of at least 8 characters is replaced inside
  any string, longest first;
* ``scheme://user:password@host`` URLs keep the user and lose the password;
* email addresses become ``[REDACTED_EMAIL]``.

Call sites log structured key-value events and never interpolate personal data into the
message::

    from ads_engine.logging import get_logger

    log = get_logger(__name__)
    log.info("sync_job_finished", source="google", job="metrics", rows=120, duration_ms=840)

This module is ``ads_engine.logging``. Absolute imports make ``import logging`` below
resolve to the standard library; the file must never be run as a script, which would put
its own directory first on ``sys.path``.
"""

import datetime as dt
import functools
import logging
import numbers
import re
import sys
import threading
import uuid
from collections.abc import Collection, Mapping, Sequence
from collections.abc import Set as AbstractSet
from decimal import Decimal
from typing import Any, Final, TextIO
from urllib.parse import unquote, urlsplit

import structlog
from pydantic import SecretBytes, SecretStr, ValidationError
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, Processor, WrappedLogger

from ads_engine.settings import Settings, get_settings

__all__ = [
    "EMAIL_RE",
    "REDACTED",
    "REDACTED_EMAIL",
    "SENSITIVE_KEYS",
    "URL_CREDENTIALS_RE",
    "configure_logging",
    "get_logger",
    "redact",
    "redact_processor",
]

REDACTED: Final = "[REDACTED]"
"""Replacement for a secret value, a sensitive key's value or a URL password."""

REDACTED_EMAIL: Final = "[REDACTED_EMAIL]"
"""Replacement for an email address found inside any logged string."""

EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
"""A conservative email pattern: ``local@domain.tld``, including ``@sim.invalid`` addresses.

It is applied after :data:`URL_CREDENTIALS_RE`, so the ``user:password@host`` part of a
database URL is handled by the URL rule rather than read as an address. ``cli.py`` reuses it
to redact captured live payloads.
"""

URL_CREDENTIALS_RE: Final = re.compile(
    r"(?P<prefix>\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/?#@]*:)(?P<password>[^\s/?#@]+)@"
)
"""The password of a ``scheme://user:password@host`` URL, for example ``DATABASE_URL``.

The substitution keeps the scheme and user: ``postgresql+psycopg://ads:pw@db:5432/x``
becomes ``postgresql+psycopg://ads:[REDACTED]@db:5432/x``.
"""

SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "password",
        "password_hash",
        "app_admin_password_hash",
        "session_secret",
        "secret",
        "client_secret",
        "access_token",
        "refresh_token",
        "token",
        "id_token",
        "n8n_webhook_secret",
        "x_webhook_secret",
        # Beyond the required set: API-key parameter names (HubSpot's legacy `hapikey`).
        "api_key",
        "x_api_key",
        "hapikey",
    }
)
"""Normalised key names whose whole value is redacted, whatever its type.

Keys are compared after normalisation: surrounding whitespace stripped, camelCase split
(``accessToken`` becomes ``access_token``), lowercased, and ``-`` read as ``_``, so the
header ``X-Webhook-Secret`` matches ``x_webhook_secret``. The configured
``N8N_WEBHOOK_SECRET_HEADER`` is added at :func:`configure_logging` time.
"""

# Fail-closed widening of the key rule (logs never contain secrets or tokens): any key
# ending in one of these is sensitive too, so names such as `hubspot_access_token`,
# `linkedin_client_secret` or a renamed n8n header like `X-Webhook-Token` are covered
# without listing each one.
_SENSITIVE_KEY_SUFFIXES: Final[tuple[str, ...]] = ("_token", "_secret", "_password", "_api_key")

# Shorter secrets (the local default database password `ads`) would mangle ordinary text
# such as `ads_engine`; they stay covered by the key and URL-credential rules.
_MIN_SECRET_LENGTH: Final = 8

# Containers nested deeper than this are replaced wholesale rather than walked.
_MAX_DEPTH: Final = 8

_URL_REPLACEMENT: Final = r"\g<prefix>" + REDACTED + "@"
_CAMEL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Values that cannot carry a secret and that JSON rendering handles without `repr`.
_SAFE_SCALARS: Final[tuple[type, ...]] = (numbers.Number, dt.date, dt.time, dt.timedelta, uuid.UUID)

# The header name used when settings cannot be loaded: the Settings field's own default.
_DEFAULT_SECRET_HEADER: Final[str] = str(Settings.model_fields["n8n_webhook_secret_header"].default)

# Third-party loggers whose request lines could carry identifiers, or whose statement
# echo could carry data, are kept at WARNING.
_QUIET_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "sqlalchemy.engine")

# uvicorn installs its own handlers with propagate=False; they are removed and the records
# propagate to the root handler, so they are rendered and redacted like every other line.
_ROUTED_LOGGERS: Final[tuple[str, ...]] = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Structured tracebacks without frame locals. structlog's `dict_tracebacks` shows locals
# truncated to 80 characters, and a truncated secret or email can no longer be matched by
# the substring and email rules, so locals are never rendered.
_EXCEPTION_RENDERER: Final = structlog.processors.ExceptionRenderer(
    ExceptionDictTransformer(show_locals=False)
)


def _normalise_key(name: str) -> str:
    """Normalise a key or header name for comparison with :data:`SENSITIVE_KEYS`.

    Args:
        name: A mapping key, header name or ``(name, value)`` pair name.

    Returns:
        The name stripped, with camelCase boundaries split by ``_``, lowercased and with
        ``-`` replaced by ``_``. For example ``X-Webhook-Secret`` gives
        ``x_webhook_secret`` and ``refreshToken`` gives ``refresh_token``.
    """
    return _CAMEL_BOUNDARY_RE.sub("_", name.strip()).lower().replace("-", "_")


def _unique_key(existing: Mapping[Any, object], key: Any) -> Any:
    """Return ``key``, suffixed with ``#n`` when redaction made it collide with another key.

    Two keys can become equal after redaction, for example two email addresses that both
    render as ``[REDACTED_EMAIL]``. The suffix keeps both values instead of silently
    dropping one.
    """
    if key not in existing:
        return key
    base = key if isinstance(key, str) else repr(key)
    counter = 2
    while f"{base}#{counter}" in existing:
        counter += 1
    return f"{base}#{counter}"


class _Redactor:
    """Compiled redaction rules: the secret pattern plus the sensitive key names.

    Instances are immutable after construction and shared between threads; build them
    through :func:`_build_redactor`, which caches them.
    """

    __slots__ = ("_secret_pattern", "_sensitive_keys")

    def __init__(self, secrets: Sequence[str], header_names: Collection[str]) -> None:
        plaintexts = sorted(
            {secret for secret in secrets if len(secret) >= _MIN_SECRET_LENGTH},
            key=lambda secret: (-len(secret), secret),
        )
        # Alternatives are tried in order, so sorting longest first makes a secret that
        # contains another secret redact as a whole.
        self._secret_pattern: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(secret) for secret in plaintexts)) if plaintexts else None
        )
        self._sensitive_keys: frozenset[str] = SENSITIVE_KEYS | {
            _normalise_key(name) for name in header_names if name.strip()
        }

    def is_sensitive_key(self, key: object) -> bool:
        """Whether ``key`` names a value that must be redacted wholesale.

        Args:
            key: A mapping key or pair name; ``str`` and ``bytes`` names are checked and
                any other type is never sensitive.

        Returns:
            ``True`` when the normalised name is a sensitive key or ends with a sensitive
            suffix.
        """
        if isinstance(key, str):
            name = key
        elif isinstance(key, bytes | bytearray):
            name = bytes(key).decode("latin-1")
        else:
            return False
        normalised = _normalise_key(name)
        return normalised in self._sensitive_keys or normalised.endswith(_SENSITIVE_KEY_SUFFIXES)

    def text(self, text: str) -> str:
        """Redact URL passwords, then configured secrets, then email addresses in ``text``.

        URL credentials go first, so a full ``DATABASE_URL`` that is itself a configured
        secret keeps its readable ``scheme://user:[REDACTED]@host/db`` form. Emails go
        last, after the URL rule has consumed any ``user:password@host`` part.
        """
        has_at = "@" in text
        redacted = URL_CREDENTIALS_RE.sub(_URL_REPLACEMENT, text) if has_at else text
        if self._secret_pattern is not None:
            redacted = self._secret_pattern.sub(REDACTED, redacted)
        return EMAIL_RE.sub(REDACTED_EMAIL, redacted) if has_at else redacted

    def value(self, value: object, depth: int) -> object:
        """Return a redacted copy of ``value``; the input is never mutated.

        Args:
            value: Any logged value.
            depth: Nesting level of ``value``; containers at :data:`_MAX_DEPTH` or deeper
                are replaced by :data:`REDACTED` instead of being walked.

        Returns:
            Strings redacted; ``SecretStr``/``SecretBytes`` replaced; dicts, mappings,
            lists, tuples, sets and other sequences copied with redacted contents; numbers,
            dates, UUIDs and ``None`` unchanged; bytes decoded and redacted; any other
            object replaced by the redacted form of what JSON rendering would print for it
            (its ``__structlog__()`` result, else its ``repr``).
        """
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, _SAFE_SCALARS):
            return value
        if isinstance(value, SecretStr | SecretBytes):
            return REDACTED
        if isinstance(value, bytes | bytearray | memoryview):
            return self.text(bytes(value).decode("utf-8", "backslashreplace"))
        if isinstance(value, Mapping | Sequence | AbstractSet):
            if depth >= _MAX_DEPTH:
                return REDACTED
            if isinstance(value, Mapping):
                return self._mapping(value, depth)
            if isinstance(value, AbstractSet):
                return self._set(value, depth)
            return self._sequence(value, depth)
        hook = getattr(value, "__structlog__", None)
        if callable(hook) and depth < _MAX_DEPTH:
            return self.value(hook(), depth + 1)
        return self.text(repr(value))

    def event(self, event_dict: Mapping[str, Any]) -> dict[str, Any]:
        """Redact every key and value of a structlog event dict.

        The event dict's own level does not count towards the nesting limit, so each of
        its values is walked from depth 0.
        """
        result: dict[str, Any] = {}
        for key, item in event_dict.items():
            new_key = _unique_key(result, self.text(key) if isinstance(key, str) else repr(key))
            result[new_key] = REDACTED if self.is_sensitive_key(key) else self.value(item, 0)
        return result

    def _key(self, key: object, depth: int) -> object:
        """Redact a mapping key, keeping it hashable."""
        if isinstance(key, str):
            return self.text(key)
        redacted = self.value(key, depth + 1)
        try:
            hash(redacted)
        except TypeError:
            # A hashable key whose redacted copy is not (an exotic hashable sequence
            # copied as a list) falls back to its redacted text.
            return self.text(repr(key))
        return redacted

    def _mapping(self, value: Mapping[Any, Any], depth: int) -> dict[Any, object]:
        """Copy a mapping into a dict, redacting keys and values."""
        result: dict[Any, object] = {}
        for key, item in value.items():
            new_key = _unique_key(result, self._key(key, depth))
            result[new_key] = (
                REDACTED if self.is_sensitive_key(key) else self.value(item, depth + 1)
            )
        return result

    def _sequence(self, value: Sequence[Any], depth: int) -> list[object] | tuple[object, ...]:
        """Copy a sequence, redacting items and sensitive ``(name, value)`` header pairs.

        Tuples stay tuples; lists and every other sequence become lists.
        """
        items: list[object] = []
        for item in value:
            if (
                isinstance(item, tuple | list)
                and len(item) == 2
                and self.is_sensitive_key(item[0])
                and depth + 1 < _MAX_DEPTH
            ):
                pair = (self.value(item[0], depth + 2), REDACTED)
                items.append(pair if isinstance(item, tuple) else list(pair))
            else:
                items.append(self.value(item, depth + 1))
        return tuple(items) if isinstance(value, tuple) else items

    def _set(
        self, value: AbstractSet[Any], depth: int
    ) -> set[object] | frozenset[object] | list[object]:
        """Copy a set, redacting its members.

        A ``frozenset`` stays a ``frozenset`` and any other set becomes a ``set``; if a
        redacted member is unhashable, the copy is a list instead.
        """
        members = [self.value(item, depth + 1) for item in value]
        try:
            return frozenset(members) if isinstance(value, frozenset) else set(members)
        except TypeError:
            return members


@functools.lru_cache(maxsize=32)
def _build_redactor(secrets: tuple[str, ...], header_names: frozenset[str]) -> _Redactor:
    """Return the cached :class:`_Redactor` for these secrets and header names."""
    return _Redactor(secrets, header_names)


def _as_names(value: Sequence[str] | Collection[str], parameter: str) -> tuple[str, ...]:
    """Materialise a ``secrets`` or ``header_names`` argument as a tuple of strings.

    Raises:
        TypeError: When a single string is passed, which would otherwise be read as a
            sequence of one-character names and silently redact nothing.
    """
    if isinstance(value, str | bytes):
        raise TypeError(f"{parameter} must be a collection of strings, not a single string")
    return tuple(value)


def redact(
    value: object,
    *,
    secrets: Sequence[str] = (),
    header_names: Collection[str] = (),
) -> object:
    """Return a redacted copy of ``value``, never mutating it.

    The rules are those of the module docstring: sensitive keys (:data:`SENSITIVE_KEYS`
    plus ``header_names``, compared after normalisation) and sensitive ``(name, value)``
    pairs lose their values; ``SecretStr`` values are replaced; inside every string, URL
    passwords, each secret of at least 8 characters (longest first) and email addresses
    are replaced. Containers are walked to a depth of 8.

    Example::

        >>> redact({"headers": {"Authorization": "Bearer abc", "X-Webhook-Secret": "zzz"}})
        {'headers': {'Authorization': '[REDACTED]', 'X-Webhook-Secret': '[REDACTED]'}}
        >>> redact("contact lead@example.com via postgresql://ads:hunter22@db/x")
        'contact [REDACTED_EMAIL] via postgresql://ads:[REDACTED]@db/x'

    Args:
        value: Any value: a string, a container, a secret or another object.
        secrets: Plaintext secrets to remove from strings; those shorter than 8
            characters are ignored.
        header_names: Extra key or header names whose values are redacted, for example
            the configured ``N8N_WEBHOOK_SECRET_HEADER``; compared case-insensitively.

    Returns:
        The redacted copy. Containers are new objects; unchanged scalars are returned
        as they are.

    Raises:
        TypeError: When ``secrets`` or ``header_names`` is a single string.
    """
    names = frozenset(_as_names(header_names, "header_names"))
    redactor = _build_redactor(_as_names(secrets, "secrets"), names)
    return redactor.value(value, 0)


class _LoggingState:
    """Process-wide logging state, guarded by ``lock``.

    ``redactor`` is read by :func:`redact_processor` on every event, so reconfiguration
    takes effect even for loggers that structlog cached before it. ``handler`` is the root
    handler this module installed, which a later :func:`configure_logging` replaces.
    """

    __slots__ = ("handler", "lock", "redactor")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.redactor = _build_redactor((), frozenset({_DEFAULT_SECRET_HEADER}))
        self.handler: logging.Handler | None = None


_STATE: Final = _LoggingState()


def redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Redact every key and value of a structlog event dict (a structlog processor).

    It uses the secrets and secret-header name captured by the latest
    :func:`configure_logging` call. An ``exc_info`` still present is first rendered as a
    structured traceback, so the traceback text is redacted too. The input dict is not
    modified; a redacted copy is returned.

    Args:
        logger: The wrapped logger (unused beyond traceback rendering).
        method_name: The log method name (unused beyond traceback rendering).
        event_dict: The event to redact.

    Returns:
        A new, redacted event dict.
    """
    if "exc_info" in event_dict:
        event_dict = _EXCEPTION_RENDERER(logger, method_name, dict(event_dict))
    return _STATE.redactor.event(event_dict)


def _json_default(obj: object) -> object:
    """JSON fallback for values the redactor leaves as objects.

    Runs after :func:`redact_processor`, so only safe scalars and redacted sets reach it:
    decimals and UUIDs as strings, dates and times in ISO 8601, durations in seconds,
    sets as sorted lists. Anything else follows structlog's own fallback
    (``__structlog__()``, else ``repr``).
    """
    if isinstance(obj, Decimal | uuid.UUID):
        return str(obj)
    if isinstance(obj, dt.date | dt.time):
        return obj.isoformat()
    if isinstance(obj, dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, AbstractSet):
        return sorted(obj, key=repr)
    hook = getattr(obj, "__structlog__", None)
    if callable(hook):
        return hook()
    return repr(obj)


def _url_secret_parts(value: str) -> tuple[str, ...]:
    """Return the password of a URL-shaped secret, raw and percent-decoded.

    A secret such as ``DATABASE_URL`` is mostly harmless text; its password is the part
    that must never appear, for example in a driver error that prints it on its own.
    """
    try:
        password = urlsplit(value).password
    except ValueError:
        return ()
    if not password:
        return ()
    return (password, unquote(password))


def _settings_secrets(settings: Settings) -> tuple[str, ...]:
    """Collect every plaintext to remove from log strings for ``settings``.

    That is :meth:`Settings.secret_values`, the password of each URL-shaped secret, and
    the n8n webhook URL with its path: the webhook URL is not a ``SecretStr``, but it is
    a capability URL that is never logged.
    """
    values: list[str] = []
    for plaintext in settings.secret_values():
        values.append(plaintext)
        values.extend(_url_secret_parts(plaintext))
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


def _resolve_level(level: str) -> int:
    """Translate a level name such as ``"INFO"`` into its ``logging`` number.

    Raises:
        ValueError: When the name is not a standard ``logging`` level.
    """
    levels = logging.getLevelNamesMapping()
    name = level.strip().upper()
    if name not in levels or name == "NOTSET":
        raise ValueError(
            f"unknown log level {level!r}; use DEBUG, INFO, WARNING, ERROR or CRITICAL"
        )
    return levels[name]


def _pre_render_processors() -> list[Processor]:
    """The processors shared by structlog events and foreign standard-library records.

    Redaction and rendering follow in the formatter, after traceback rendering, so
    tracebacks are redacted as well.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _EXCEPTION_RENDERER,
    ]


def configure_logging(
    *,
    stream: TextIO | None = None,
    level: str = "INFO",
    settings: Settings | None = None,
) -> None:
    """Configure structlog and the standard library to write redacted JSON lines.

    Installs one :class:`logging.StreamHandler` on the root logger, replacing every root
    handler present, with a :class:`structlog.stdlib.ProcessorFormatter`. structlog events
    and third-party records then share one pipeline: context variables, level, logger
    name, ISO-8601 UTC ``timestamp``, stack and traceback rendering, redaction and JSON.
    Calling it again reconfigures in place without duplicate handlers; the new secrets
    and stream apply to loggers created before the call as well.

    The api and worker log to stdout (the container convention). ``cli.py`` passes
    ``stream=sys.stderr``, so stdout carries only command output such as the
    ``hash-password`` hash.

    Example::

        configure_logging()  # api / worker: JSON lines on stdout
        configure_logging(stream=sys.stderr)  # CLI: stdout stays clean

    Args:
        stream: Destination of the JSON lines; ``sys.stdout`` when ``None``.
        level: Root level name, for example ``"INFO"`` or ``"DEBUG"``. ``httpx``,
            ``httpcore`` and ``sqlalchemy.engine`` are held at ``WARNING`` regardless.
        settings: Source of the secrets and the secret-header name to redact;
            :func:`get_settings` when ``None``. If loading settings fails validation,
            logging is still configured, with no secret values and the default header
            name, and a warning naming the invalid fields (never their values) is logged;
            the error is not raised here.

    Raises:
        ValueError: When ``level`` is not a standard level name.
    """
    root_level = _resolve_level(level)
    settings_error: ValidationError | None = None
    if settings is None:
        try:
            settings = get_settings()
        except ValidationError as exc:
            settings_error = exc
    if settings is not None:
        secrets = _settings_secrets(settings)
        header_name = settings.n8n_webhook_secret_header
    else:
        secrets = ()
        header_name = _DEFAULT_SECRET_HEADER

    pre_chain = _pre_render_processors()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_processor,
            structlog.processors.JSONRenderer(default=_json_default),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    with _STATE.lock:
        _STATE.redactor = _build_redactor(secrets, frozenset({header_name}))
        root = logging.getLogger()
        for existing in list(root.handlers):
            root.removeHandler(existing)
            if existing is _STATE.handler:
                existing.close()
        root.addHandler(handler)
        root.setLevel(root_level)
        _STATE.handler = handler
        for name in _QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        for name in _ROUTED_LOGGERS:
            routed = logging.getLogger(name)
            for existing in list(routed.handlers):
                routed.removeHandler(existing)
            routed.propagate = True
        structlog.configure(
            processors=[
                # Drop disabled levels before any processor runs.
                structlog.stdlib.filter_by_level,
                *pre_chain,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(ignore_frame_names=[__name__]),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

    if settings_error is not None:
        get_logger(__name__).warning(
            "logging_configured_without_settings",
            error_count=settings_error.error_count(),
            fields=sorted(
                {
                    ".".join(str(part) for part in error["loc"])
                    for error in settings_error.errors(include_url=False, include_input=False)
                }
            ),
        )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the standard-library logger ``name``.

    Without a name, the logger is named after the calling module (frames inside
    structlog and this module are skipped), rather than after the root logger.

    Example::

        log = get_logger(__name__)
        log.info("analysis_run_finished", run_id=12, items=37, held=2)

    Args:
        name: The logger name, normally ``__name__`` of the calling module.

    Returns:
        A lazily bound :class:`structlog.stdlib.BoundLogger`.
    """
    if name is None:
        return structlog.stdlib.get_logger()
    return structlog.stdlib.get_logger(name)
