"""Structured JSON logging with secret, token and email redaction.

Every process (``api``, ``worker``, the ``ads-engine`` CLI) calls
:func:`configure_logging` once at startup. After that, structlog events and records from
third-party standard-library loggers (``uvicorn``, ``uvicorn.access``, ``sqlalchemy``,
``alembic``, ``httpx``) are written as one JSON object per line, and every line passes
through the same redaction before it is rendered:

* values under sensitive keys (``Authorization``, cookies, passwords, tokens, client
  secrets, the n8n secret header, and the configured ``N8N_WEBHOOK_SECRET_HEADER``,
  matched in any letter case, with or without ``_``, ``-`` and camelCase separators)
  become ``[REDACTED]``, including ``(name, value)`` header pairs and ``bytes`` names;
* :class:`pydantic.SecretStr` values become ``[REDACTED]``;
* inside any string, whatever secrets are configured: ``scheme://user:password@host``
  URLs keep the user and lose the password; values assigned to sensitive keys
  (``Authorization: Bearer ...``, ``?access_token=...``, ``{"refresh_token": "..."}``)
  and of sensitive pair reprs, and standalone ``Bearer`` tokens, become ``[REDACTED]``,
  each value whole: a header's to the end of its line (``Cookie: a="b"; c=d``), a
  container member's up to the next member (``{'token': {'value': '...'}, 'n': 1}``);
* the plaintext of every configured secret, whatever its length, is replaced inside any
  string, longest first, together with any email address and any percent-encoded run
  holding a ``%XX`` escape that it overlaps, so a short secret never breaks an address,
  an escape or an encoded key apart (``3D`` in ``access_token%3Dtok``); a secret is
  never matched inside a redaction marker, and one that overlaps a marker's end (``]x``
  in ``[REDACTED]x``) is replaced together with it;
* email addresses become ``[REDACTED_EMAIL]``;
* the same rules apply to percent-encoded text (``lead%40example.com``, encoded up to
  four times or in mixed case), so foreign records and exception text are covered too;
  text encoded more deeply than that becomes ``[REDACTED]`` whole;
* an exception that a built-in ``__str__`` renders from its arguments (``KeyError``'s
  repr of its key, ``OSError``'s ``[Errno n] ...`` and the like) is rendered again from
  those arguments redacted as data, before its traceback text is redacted, and frame
  locals are never rendered;
* NaN and infinite floats become ``null``, so every line is strict JSON.

Every rule runs in linear time, and redacting redacted text changes nothing. Rendering
fails closed: a value whose hook or ``repr`` raises becomes ``[UNRENDERABLE]``, a record
that cannot be rendered becomes a minimal line naming only the error class, and a failed
write is reported without the record. No path writes an unredacted record, including
``logging.lastResort`` and handlers elsewhere in the logger tree.

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
import json
import logging
import math
import numbers
import os
import re
import sys
import threading
import unicodedata
import uuid
from collections.abc import Collection, Mapping, Sequence
from collections.abc import Set as AbstractSet
from decimal import Decimal
from typing import Any, Final, TextIO, override
from urllib.parse import unquote, urlsplit

import structlog
from pydantic import SecretBytes, SecretStr, ValidationError
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, ExcInfo, Processor, WrappedLogger

from ads_engine.settings import Settings, get_settings

__all__ = [
    "EMAIL_RE",
    "REDACTED",
    "REDACTED_EMAIL",
    "SENSITIVE_KEYS",
    "URL_CREDENTIALS_RE",
    "configure_logging",
    "get_logger",
    "logged_as",
    "mark_logged",
    "redact",
    "redact_processor",
    "redact_secret_matches",
]

REDACTED: Final = "[REDACTED]"
"""Replacement for a secret value, a sensitive key's value or a URL password."""

REDACTED_EMAIL: Final = "[REDACTED_EMAIL]"
"""Replacement for an email address found inside any logged string."""

EMAIL_RE: Final = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)++"
    r"(?:[A-Za-z0-9._%+-]++@[A-Za-z0-9-]++(?:\.[A-Za-z0-9-]++)++)*+"
)
"""A conservative email pattern: ``local@domain.tld``, including ``@sim.invalid`` addresses.

Matching runs in linear time on any input (CWE-1333): a match starts only where a run of
local-part characters starts, and every quantifier is possessive, so a long malformed
address such as ``"a" * 100_000 + "@x"`` is rejected in one pass instead of being retried
from every position. Addresses glued directly after one another (``a@b.c+d@e.f``) are
consumed by the same match, so none is left behind. It is applied after
:data:`URL_CREDENTIALS_RE`, so the ``user:password@host`` part of a database URL is handled
by the URL rule rather than read as an address. ``cli.py`` reuses it to redact captured
live payloads.
"""

URL_CREDENTIALS_RE: Final = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9+.-])[0-9+.-]*+[A-Za-z][A-Za-z0-9+.-]*+://[^\s:/?#@]*+:)"
    r"(?P<password>[^\s/?#@]++)@"
)
"""The password of a ``scheme://user:password@host`` URL, for example ``DATABASE_URL``.

The substitution keeps the scheme and user: ``postgresql+psycopg://ads:pw@db:5432/x``
becomes ``postgresql+psycopg://ads:[REDACTED]@db:5432/x``. Like :data:`EMAIL_RE`, it runs
in linear time: a match starts only where a run of scheme characters starts, and every
quantifier is possessive.
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

Each key is compared in three normalised forms (see :func:`_key_forms`), surrounding
whitespace stripped and ``-`` read as ``_`` in all three: case-folded as written
(``AuThOrIzAtIoN`` becomes ``authorization``, ``X-Webhook-Secret`` becomes
``x_webhook_secret``); case-folded after splitting camelCase (``accessToken`` becomes
``access_token``); and case-folded as written with every ``_`` removed (``ACCESSTOKEN``,
``rEfReShToKeN`` and ``X-API-KEY`` become ``accesstoken``, ``refreshtoken`` and
``xapikey``). A key is sensitive when its first or second form is listed here or ends with
``_token``, ``_secret``, ``_password`` or ``_api_key``, or when its third form is a listed
name with ``_`` removed or ends with ``token``, ``secret``, ``password`` or ``apikey``.
``bytes`` names are read as Latin-1. The configured ``N8N_WEBHOOK_SECRET_HEADER`` is added,
in all three forms, at :func:`configure_logging` time.
"""

# Fail-closed widening of the key rule (logs never contain secrets or tokens): any key
# ending in one of these is sensitive too, so names such as `hubspot_access_token`,
# `linkedin_client_secret` or a renamed n8n header like `X-Webhook-Token` are covered
# without listing each one.
_SENSITIVE_KEY_SUFFIXES: Final[tuple[str, ...]] = ("_token", "_secret", "_password", "_api_key")

# The listed names and suffixes with `_` removed, compared with the separator-free form of
# a key, so a name written without `_`, `-` or a camelCase boundary (`accesstoken`,
# `XAPIKEY`, `proxyauthorization`, `hubspotaccesstoken`) is sensitive too.
_SEPARATOR_FREE_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    key.replace("_", "") for key in SENSITIVE_KEYS
)
_SEPARATOR_FREE_KEY_SUFFIXES: Final[tuple[str, ...]] = tuple(
    suffix.replace("_", "") for suffix in _SENSITIVE_KEY_SUFFIXES
)

# A URL password taken out of a URL-shaped secret becomes a substring rule of its own only
# from this length: a shorter one (the local default database password `ads`) would mangle
# ordinary text such as `ads_engine`, and the URL-credential rule already removes it from
# its URL. A configured secret itself is always a substring rule, whatever its length.
_MIN_SECRET_LENGTH: Final = 8

# Containers nested deeper than this are replaced wholesale rather than walked.
_MAX_DEPTH: Final = 8

# Replacement for a logged value whose hook, repr or container operation raised. The
# exception's own text is never rendered: it may quote the value it failed on.
_UNRENDERABLE: Final = "[UNRENDERABLE]"

# A value that an earlier pass already replaced; it is left as it is, so redacting
# redacted text changes nothing.
_REDACTION_MARKER_RE: Final = re.compile(r"\[(?:REDACTED|REDACTED_EMAIL|UNRENDERABLE)\]")
# The texts `_REDACTION_MARKER_RE` matches. A configured secret can overlap one of them, so
# the secret pattern is built around them (see `_compile_secret_pattern`).
_REDACTION_MARKERS: Final[tuple[str, ...]] = (REDACTED, REDACTED_EMAIL, _UNRENDERABLE)

_URL_REPLACEMENT: Final = r"\g<prefix>" + REDACTED + "@"
_CAMEL_BOUNDARY_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Textual credential rules. They apply inside every string whatever secrets are configured,
# so a refreshed or never-configured token in a header line, URL query, form or JSON body,
# container repr or exception text is still removed. Every pattern runs in linear time: a
# match starts only where its run of name characters starts, and quantifiers are
# possessive.
#
# `key=value` and `key: value`, the key optionally quoted (JSON, Python repr), the quote
# optionally backslash-escaped (JSON inside a JSON string). Only the key and separator are
# matched; the value's extent is read afterwards for sensitive keys alone, so an ordinary
# key never swallows a sensitive assignment that follows it.
_ASSIGNMENT_RE: Final = re.compile(
    r"(?P<kq>\\?[\"'])?(?<![A-Za-z0-9_-])(?P<key>[A-Za-z0-9_-]++)(?(kq)(?P=kq))"
    r"[ \t]*+(?P<sep>[:=])[ \t]*+"
)
# A `('name', 'value')`, `(b'name', b'value')`, `['name', 'value']` or
# `('name', ['value'])` pair repr, up to the opening quote or bracket of its value.
_PAIR_RE: Final = re.compile(
    r"[(\[][ \t]*+b?(?P<nq>[\"'])(?P<key>[A-Za-z0-9_-]++)(?P=nq)[ \t]*+,[ \t]*+"
    r"(?=b?[\"']|[(\[{])"
)
# A quoted value; its content is replaced and its quotes kept. An unterminated string runs
# to the end of the line, so a cut-off token is still removed.
_QUOTED_VALUE_RE: Final = re.compile(
    r"b?(?P<q>\\?[\"'])(?P<content>(?:(?!(?P=q))(?:[^\\\r\n]|\\[^\r\n]))*+)(?P=q)?"
)
# A line break: a header value such as `Cookie: a="b"; c=d` or `Authorization: Digest
# username="u", nonce="n"` runs up to it, whatever quotes, brackets or commas it holds.
_LINE_BREAK_RE: Final = re.compile(r"[\r\n]")
# An authentication scheme followed by its credentials (`Bearer <token>`, `Digest
# username="u", nonce="n"`); a value that starts with one runs to the end of the line.
_AUTH_SCHEME_RE: Final = re.compile(
    r"(?i:bearer|basic|digest|negotiate|ntlm|token|dpop|hoba|mutual|aws4-hmac-sha256)"
    r"[ \t]++(?=\S)"
)
# One token of a `key=value` text, URL query or form body.
_TOKEN_RUN_RE: Final = re.compile(r"[^\s&;,\"'<>(){}\[\]]*+")
# Each opening bracket and the closer that ends its group.
_BRACKETS: Final[Mapping[str, str]] = {"(": ")", "[": "]", "{": "}"}
_CLOSERS: Final = ")]}"
_QUOTES: Final = "'\""
# A key directly inside one of these (blanks skipped) is a container member: a JSON or
# dict entry, a list item or a keyword argument of a repr.
_CONTAINER_STARTS: Final = "{[(,"
# Where an unquoted container member's value ends at nesting depth 0, outside strings.
_CONTAINER_STOPS: Final = ",)]}\r\n"
# A standalone `Bearer <token>` credential anywhere in the text.
_BEARER_RE: Final = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])(?i:bearer)[ \t]++)[^\s,;\"'<>(){}\[\]]++"
)
_BEARER_REPLACEMENT: Final = r"\g<prefix>" + REDACTED

# A maximal run of characters other than blanks, quotes and angle brackets that holds a
# `%`: a URL, query or form value that may hide a percent-encoded address, secret or
# credential assignment. A match starts only where such a run starts.
_ENCODED_RUN_RE: Final = re.compile(r"(?<![^\s\"'<>])[^\s\"'<>%]*+%[^\s\"'<>]*+")
_PERCENT_ESCAPE_RE: Final = re.compile(r"%[0-9A-Fa-f]{2}")
# Decoding stops after this many rounds, enough for a value encoded and then re-encoded
# by every layer between a source and the log; a run that would still decode further is
# replaced whole, so the bound never lets an encoded value through.
_MAX_DECODE_ROUNDS: Final = 4
# Unicode categories percent-encoded again when a decoded run is written back, so decoding
# never introduces control, format or line-separator characters into a log line.
_ESCAPED_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Zl", "Zp"})

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


def _key_forms(name: str) -> tuple[str, str, str]:
    """Normalise a key or header name for comparison with :data:`SENSITIVE_KEYS`.

    Header names are case-insensitive, so the raw name is case-folded as written; the
    camelCase form is an additional alias. Splitting camelCase alone would turn a
    mixed-case ``AuThOrIzAtIoN`` into ``au_th_or_iz_at_io_n`` and miss it. Neither form
    separates a name written without ``_``, ``-`` or a camelCase boundary
    (``accesstoken``, ``ACCESSTOKEN``, ``rEfReShToKeN``), so the separator-free form is
    compared with the sensitive names and suffixes that have ``_`` removed.

    Args:
        name: A mapping key, header name or ``(name, value)`` pair name.

    Returns:
        The raw form (stripped, case-folded, ``-`` replaced by ``_``), the camelCase
        form (stripped, camelCase boundaries split by ``_``, then case-folded with ``-``
        replaced by ``_``) and the separator-free form (the raw form with every ``_``
        removed). For example ``X-Webhook-Secret`` gives ``x_webhook_secret`` twice and
        then ``xwebhooksecret``, ``refreshToken`` gives ``refreshtoken``,
        ``refresh_token`` and ``refreshtoken``, and ``rEfReShToKeN`` gives
        ``refreshtoken``, ``r_ef_re_sh_to_ke_n`` and ``refreshtoken``.
    """
    stripped = name.strip()
    raw = stripped.casefold().replace("-", "_")
    camel = _CAMEL_BOUNDARY_RE.sub("_", stripped).casefold().replace("-", "_")
    return raw, camel, raw.replace("_", "")


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


def _scan_value(text: str, start: int, stops: str | None) -> int:
    """Return the index just past the value that starts at ``text[start]``.

    One forward pass over the text, tracking a stack of open brackets and the open
    string. ``(``, ``[`` and ``{`` open a group that runs, across lines, to its matching
    closer; a closer that does not match the innermost open bracket is part of the
    value. ``'`` and ``"`` open a string that runs to the same quote, and a backslash
    escapes the character after it, so brackets, quotes and stops inside a string or an
    escape do not count.

    Args:
        text: The text being redacted.
        start: Index of the value's first character.
        stops: Characters that end the value at nesting depth 0 outside a string, where
            an unmatched closer ends it too. ``None`` scans one group: ``text[start]`` is
            an opening bracket and the value ends just after its matching closer.

    Returns:
        The end of the value; ``len(text)`` when a group or string is never closed, so
        an unterminated value fails closed.
    """
    stack: list[str] = []
    quote = ""
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in _BRACKETS:
            stack.append(_BRACKETS[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack and stops is None:
                return index + 1
        elif char in _QUOTES:
            quote = char
        elif not stack and stops is not None and (char in stops or char in _CLOSERS):
            return index
        index += 1
    return length


def _line_end(text: str, index: int) -> int:
    """Return the index of the first line break at or after ``index``, else ``len(text)``."""
    match = _LINE_BREAK_RE.search(text, index)
    return match.start() if match is not None else len(text)


def _token_end(text: str, start: int) -> int:
    """Return the end of the ``key=value`` token at ``text[start]``.

    The token is a run of characters other than blanks, quotes, angle brackets and
    ``&;,``; each bracketed group glued to it is taken whole, with the run that follows.
    """
    end = start
    while True:
        token = _TOKEN_RUN_RE.match(text, end)
        if token is not None:
            end = token.end()
        if end >= len(text) or text[end] not in _BRACKETS:
            return end
        end = _scan_value(text, end, None)


def _in_container(text: str, key_start: int) -> bool:
    """Whether the key that starts at ``text[key_start]`` is a member of a container.

    ``key_start`` is the index of the key's opening quote, or of its ``b`` prefix, when
    it is quoted. The key is a member when the character before it, blanks skipped, opens
    a group or separates members: ``{``, ``[``, ``(`` or ``,``.
    """
    index = key_start
    while index > 0 and text[index - 1] in " \t":
        index -= 1
    return index > 0 and text[index - 1] in _CONTAINER_STARTS


def _value_span(
    text: str, start: int, separator: str | None, *, container: bool
) -> tuple[int, int]:
    """Return the span of the credential value that starts at ``text[start]``.

    The extent depends on where the key sits, so that no part of a credential is left
    behind, however it is quoted, nested or split:

    * container members (``{"refresh_token": ["x"]}``, ``{'token': Token(value='x'), 'n':
      1}``, ``f(access_token=x, n=1)``) and pair reprs: a quoted value's content, else the
      text up to the next ``,``, ``)``, ``]``, ``}`` or line break outside brackets and
      strings, so a nested value is taken whole and the following members are kept;
    * header or prose text after ``:`` (``Cookie: a="b"; c=d``, ``Authorization: Digest
      username="u", nonce="n"``): the rest of the line, and a value that opens a bracket
      runs at least to its matching closer, even on a later line;
    * text after ``=`` (URL query, form body): a quoted value's content, else one token,
      including each bracketed group glued to it (``token=Token(value='x')``).

    An unquoted value that starts with an authentication scheme (``Bearer``, ``Digest``
    and the like) runs to the end of the line wherever it sits, so scheme parameters
    separated by commas go with it.

    Args:
        text: The text being redacted.
        start: Index just after the key, its separator and any blanks.
        separator: ``":"`` or ``"="`` for an assignment; ``None`` for a pair repr.
        container: Whether the key is a container member (see :func:`_in_container`);
            pair reprs always are.

    Returns:
        ``(start, end)`` of the value to replace, trailing blanks excluded. An unclosed
        bracket or string extends it to the end of the text.
    """
    if start >= len(text):
        return start, start
    if separator == ":" and not container:
        group_end = _scan_value(text, start, None) if text[start] in _BRACKETS else start
        end = _line_end(text, group_end)
    else:
        quoted = _QUOTED_VALUE_RE.match(text, start)
        if quoted is not None:
            return quoted.span("content")
        if _AUTH_SCHEME_RE.match(text, start) is not None:
            end = _line_end(text, start)
        elif container:
            end = _scan_value(text, start, _CONTAINER_STOPS)
        else:
            end = _token_end(text, start)
    return start, start + len(text[start:end].rstrip(" \t"))


def _escape_controls(text: str) -> str:
    """Percent-encode every control, format and line or paragraph separator in ``text``."""
    return "".join(
        "".join(f"%{byte:02X}" for byte in char.encode("utf-8", "surrogatepass"))
        if unicodedata.category(char) in _ESCAPED_CATEGORIES
        else char
        for char in text
    )


def _compile_secret_pattern(plaintexts: Sequence[str]) -> re.Pattern[str]:
    """Compile the one pattern that finds every secret in ``plaintexts`` and no marker.

    Secrets of any length are matched, so a short one can occur inside a redaction marker
    (``ACT`` or ``D]`` in ``[REDACTED]``). Alternatives are tried in order at each position,
    and every one is a literal text, so matching stays linear:

    1. a marker run on by a secret that starts with the marker's tail, or with the whole
       marker, and extends past it (``[REDACTED]x`` for the secret ``]x``): marker and
       secret are replaced together, since keeping the marker would leave the secret
       behind;
    2. a bare marker, named ``marker``, which :func:`redact_secret_matches` keeps as it
       is, so no secret is ever matched inside one;
    3. the secrets themselves.

    Each group is ordered longest first, so a secret that contains another secret is
    redacted as a whole.

    Args:
        plaintexts: Non-empty secrets, longest first.

    Returns:
        The compiled pattern, applied through :func:`redact_secret_matches`.
    """
    straddles = sorted(
        {
            marker[:cut] + secret
            for marker in _REDACTION_MARKERS
            for secret in plaintexts
            for cut in range(len(marker))
            if len(secret) > len(marker) - cut and secret.startswith(marker[cut:])
        },
        key=lambda text: (-len(text), text),
    )
    alternatives = [re.escape(text) for text in straddles]
    alternatives.append(f"(?P<marker>{_REDACTION_MARKER_RE.pattern})")
    alternatives.extend(re.escape(secret) for secret in plaintexts)
    return re.compile("|".join(alternatives))


def redact_secret_matches(text: str, pattern: re.Pattern[str]) -> str:
    """Replace each secret ``pattern`` finds in ``text``, with every whole span it overlaps.

    A secret replaced on its own would cut what contains it, or what it runs into, into
    pieces the later rules no longer recognise, and the rest would stay behind: an email
    address :data:`EMAIL_RE` no longer matches (``jane[REDACTED]@x.io`` for the secret
    ``2024`` in ``jane2024@x.io``), or a percent-encoded run whose escape or key the
    decode pass can no longer read (``access_token%[REDACTED]tok`` for the secret ``3D``
    in ``access_token%3Dtok``). So each match, widened over every email address and every
    percent-encoded run holding a ``%XX`` escape that it overlaps (see
    :func:`_whole_spans`), becomes one ``[REDACTED]``, and matches joined by such a span
    become one too. Spans that overlap no secret are left for the email rule and the
    decode pass, which run afterwards. A match of a group named ``marker`` (see
    :func:`_compile_secret_pattern`) is kept as it is. ``pattern``, :data:`EMAIL_RE` and
    :data:`_ENCODED_RUN_RE` each scan ``text`` once, so the work stays linear.

    Example::

        >>> redact_secret_matches("jane2024@x.io and 2024", re.compile("2024"))
        '[REDACTED] and [REDACTED]'
        >>> redact_secret_matches("GET access_token%3Dtok 100% 3D", re.compile("3D"))
        'GET [REDACTED] 100% [REDACTED]'

    Args:
        text: The text to redact.
        pattern: An alternation of escaped secrets, optionally with a ``marker`` group.

    Returns:
        ``text`` with every secret, and every email address and encoded run one overlaps,
        replaced.
    """
    secrets = [match.span() for match in pattern.finditer(text) if match.lastgroup != "marker"]
    if not secrets:
        return text
    wholes = _whole_spans(text)
    pieces: list[str] = []
    cursor = 0
    region_start = region_end = -1
    whole = 0
    for start, end in secrets:
        while whole < len(wholes) and wholes[whole][1] <= start:
            whole += 1
        while whole < len(wholes) and wholes[whole][0] < end:
            start = min(start, wholes[whole][0])
            end = max(end, wholes[whole][1])
            whole += 1
        if start < region_end:
            region_end = max(region_end, end)
            continue
        if region_end >= 0:
            pieces.extend((text[cursor:region_start], REDACTED))
            cursor = region_end
        region_start, region_end = start, end
    pieces.extend((text[cursor:region_start], REDACTED, text[region_end:]))
    return "".join(pieces)


def _whole_spans(text: str) -> list[tuple[int, int]]:
    """Return the sorted, disjoint spans of ``text`` that a secret match takes whole.

    That is every :data:`EMAIL_RE` match, and every percent-encoded run
    (:data:`_ENCODED_RUN_RE`) holding at least one valid ``%XX`` escape
    (:data:`_PERCENT_ESCAPE_RE`): exactly the runs the decode pass of
    :meth:`_Redactor.text` decodes, whatever they decode to. A run without a valid escape
    (``100%``, ``%ZZ``) is never decoded, so it is not included. Overlapping spans are
    joined.
    """
    spans = [match.span() for match in EMAIL_RE.finditer(text)] if "@" in text else []
    if "%" in text:
        spans.extend(
            run.span()
            for run in _ENCODED_RUN_RE.finditer(text)
            if _PERCENT_ESCAPE_RE.search(text, run.start(), run.end()) is not None
        )
        spans.sort()
    joined: list[tuple[int, int]] = []
    for start, end in spans:
        if joined and start < joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


class _Redactor:
    """Compiled redaction rules: the secret pattern plus the sensitive key names.

    Instances are immutable after construction and shared between threads; build them
    through :func:`_build_redactor`, which caches them.
    """

    __slots__ = ("_secret_pattern", "_sensitive_keys", "_separator_free_keys")

    def __init__(self, secrets: Sequence[str], header_names: Collection[str]) -> None:
        # Every secret counts whatever its length; an empty one would match everywhere.
        plaintexts = sorted(
            {secret for secret in secrets if secret}, key=lambda secret: (-len(secret), secret)
        )
        self._secret_pattern: re.Pattern[str] | None = (
            _compile_secret_pattern(plaintexts) if plaintexts else None
        )
        # Each configured header name is added in all three forms of `_key_forms`: its raw
        # and camelCase forms beside SENSITIVE_KEYS, its separator-free form beside theirs,
        # so a configured `X-Custom-Auth` also matches `x_custom_auth` and `XCUSTOMAUTH`.
        aliases = [_key_forms(name) for name in header_names if name.strip()]
        self._sensitive_keys: frozenset[str] = SENSITIVE_KEYS | {
            form for raw, camel, _ in aliases for form in (raw, camel)
        }
        self._separator_free_keys: frozenset[str] = _SEPARATOR_FREE_SENSITIVE_KEYS | {
            separator_free for _, _, separator_free in aliases
        }

    def is_sensitive_key(self, key: object) -> bool:
        """Whether ``key`` names a value that must be redacted wholesale.

        Args:
            key: A mapping key or pair name; ``str`` and ``bytes`` names are checked and
                any other type is never sensitive.

        Returns:
            ``True`` when the raw or camelCase form of the name (see :func:`_key_forms`
            and :data:`SENSITIVE_KEYS`) is a sensitive key or ends with a sensitive
            suffix (``Authorization``, ``accessToken``, ``hubspot_access_token``), when
            its separator-free form is a sensitive key or ends with a sensitive suffix,
            each with ``_`` removed (``ACCESSTOKEN``, ``rEfReShToKeN``, ``XAPIKEY``,
            ``proxyauthorization``), and when reading the name raises, so its value fails
            closed.
        """
        try:
            if isinstance(key, str):
                name = str.__str__(key)
            elif isinstance(key, bytes | bytearray):
                name = bytes(memoryview(key)).decode("latin-1")
            else:
                return False
            raw, camel, separator_free = _key_forms(name)
            return (
                any(
                    form in self._sensitive_keys or form.endswith(_SENSITIVE_KEY_SUFFIXES)
                    for form in (raw, camel)
                )
                or separator_free in self._separator_free_keys
                or separator_free.endswith(_SEPARATOR_FREE_KEY_SUFFIXES)
            )
        # No-blind-except yields: a str or bytes subclass can raise anything; fail closed.
        except Exception:  # noqa: BLE001
            return True

    def text(self, text: str) -> str:
        """Redact every credential, configured secret and email address in ``text``.

        The rules run in this order: URL passwords; values assigned to sensitive keys
        (``key=value``, ``key: value``, quoted JSON and repr keys); values of sensitive
        ``(name, value)`` pair reprs; standalone ``Bearer`` tokens; configured secrets;
        email addresses. A sensitive value is replaced whole, however it is quoted or
        nested: a header's value to the end of its line, a container member's value up
        to the next member, a bracketed value to its matching closer (see
        :func:`_value_span`). URL credentials go first, so a full ``DATABASE_URL`` that is
        itself a configured secret keeps its readable ``scheme://user:[REDACTED]@host/db``
        form. Emails go last, after the URL rule has consumed any ``user:password@host``
        part.

        Then every run of URL-like text holding a ``%XX`` escape is percent-decoded, up to
        four times, and the same rules run on each decoded form. Where they find something
        (``lead%40example.com``, an encoded secret, ``access_token%3D...``), the decoded,
        redacted run replaces the raw one, with control characters encoded again;
        otherwise the raw run is kept byte for byte. A run that would still decode after
        the fourth round is replaced by ``[REDACTED]`` whole. Text that is already
        redacted is returned unchanged.
        """
        # A str subclass is read as its plain characters, so none of its methods runs.
        redacted = self._plain(str.__str__(text))
        if "%" in redacted:
            redacted = _ENCODED_RUN_RE.sub(self._decoded_run, redacted)
        return redacted

    def _decoded_run(self, match: re.Match[str]) -> str:
        """Return a percent-encoded run redacted in its decoded form, or the raw run.

        A run that would still decode after :data:`_MAX_DECODE_ROUNDS` rounds is encoded
        more deeply than any rule inspects, so it is replaced by ``[REDACTED]`` whole:
        nothing recoverable by decoding it further is left behind.
        """
        run = match.group()
        if _PERCENT_ESCAPE_RE.search(run) is None:
            return run
        current = run
        changed = False
        for _ in range(_MAX_DECODE_ROUNDS):
            decoded = unquote(current)
            if decoded == current:
                break
            current = self._plain(decoded)
            changed = changed or current != decoded
        else:
            if unquote(current) != current:
                return REDACTED
        return _escape_controls(current) if changed else run

    def _plain(self, text: str) -> str:
        """Apply the plaintext rules of :meth:`text`, in its order, to ``text``."""
        has_at = "@" in text
        redacted = URL_CREDENTIALS_RE.sub(_URL_REPLACEMENT, text) if has_at else text
        if ":" in redacted or "=" in redacted:
            redacted = self._assigned_values(redacted, _ASSIGNMENT_RE)
        if "," in redacted and ("'" in redacted or '"' in redacted):
            redacted = self._assigned_values(redacted, _PAIR_RE)
        redacted = _BEARER_RE.sub(_BEARER_REPLACEMENT, redacted)
        if self._secret_pattern is not None:
            redacted = redact_secret_matches(redacted, self._secret_pattern)
        return EMAIL_RE.sub(REDACTED_EMAIL, redacted) if has_at else redacted

    def _assigned_values(self, text: str, head: re.Pattern[str]) -> str:
        """Replace the value after each sensitive key that ``head`` finds in ``text``.

        ``head`` matches a key and its separator only. The value's extent
        (:func:`_value_span`) is read for sensitive keys alone, and matches inside a
        replaced value are skipped, so the scan stays linear and an ordinary key never
        hides the assignment that follows it.
        """
        pieces: list[str] = []
        cursor = 0
        for match in head.finditer(text):
            if match.start() < cursor or not self.is_sensitive_key(match.group("key")):
                continue
            groups = match.groupdict()
            separator = groups.get("sep")
            key_start = match.start()
            if groups.get("kq") and text[key_start - 1 : key_start] == "b":
                key_start -= 1
            container = separator is None or _in_container(text, key_start)
            start, end = _value_span(text, match.end(), separator, container=container)
            value = text[start:end]
            if not value or _REDACTION_MARKER_RE.fullmatch(value) is not None:
                continue
            pieces.append(text[cursor:start])
            pieces.append(REDACTED)
            cursor = end
        if not pieces:
            return text
        pieces.append(text[cursor:])
        return "".join(pieces)

    def value(self, value: object, depth: int) -> object:
        """Return a redacted copy of ``value``; the input is never mutated.

        Args:
            value: Any logged value.
            depth: Nesting level of ``value``; containers at :data:`_MAX_DEPTH` or deeper
                are replaced by :data:`REDACTED` instead of being walked.

        Returns:
            Strings redacted; ``SecretStr``/``SecretBytes`` replaced; dicts, mappings,
            lists, tuples, sets and other sequences copied with redacted contents; NaN and
            infinite floats replaced by ``None``, which strict JSON renders as ``null``;
            other numbers, dates, UUIDs and ``None`` unchanged; bytes decoded and redacted;
            any other object replaced by the redacted form of what JSON rendering would
            print for it (its ``__structlog__()`` result, else its ``repr``). A value whose
            hook, ``repr``, iteration or other operation raises is replaced by
            ``[UNRENDERABLE]``, without the exception's text; its siblings are kept.
        """
        try:
            return self._value(value, depth)
        # No-blind-except yields: a logged object's hook or repr can raise anything; fail closed.
        except Exception:  # noqa: BLE001
            return _UNRENDERABLE

    def _value(self, value: object, depth: int) -> object:
        """Redact ``value`` as :meth:`value` describes, letting any exception propagate."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, float) and not math.isfinite(value):
            # Strict JSON has no NaN or Infinity; they are logged as null.
            return None
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
        its values is walked from depth 0. Keys become strings: a non-string key is
        rendered through its redacted ``repr``, and a key that cannot be rendered becomes
        ``[UNRENDERABLE]``. It never raises.
        """
        result: dict[str, Any] = {}
        try:
            for key, item in event_dict.items():
                try:
                    rendered = self.text(key if isinstance(key, str) else repr(key))
                # No-blind-except yields: a key's repr can raise anything; fail closed.
                except Exception:  # noqa: BLE001
                    rendered = _UNRENDERABLE
                new_key = _unique_key(result, rendered)
                result[new_key] = REDACTED if self.is_sensitive_key(key) else self.value(item, 0)
        # No-blind-except yields: a mapping's iteration can raise anything; fail closed.
        except Exception:  # noqa: BLE001
            result.setdefault("event", _UNRENDERABLE)
        return result

    def _key(self, key: object, depth: int) -> object:
        """Redact a mapping key, keeping it hashable; ``[UNRENDERABLE]`` when that fails."""
        try:
            if isinstance(key, str):
                return self.text(key)
            redacted = self._value(key, depth + 1)
            try:
                hash(redacted)
            except TypeError:
                # A hashable key whose redacted copy is not (an exotic hashable sequence
                # copied as a list) falls back to its redacted text.
                return self.text(repr(key))
            return redacted
        # No-blind-except yields: a key's hash, hook or repr can raise anything; fail closed.
        except Exception:  # noqa: BLE001
            return _UNRENDERABLE

    def _mapping(self, value: Mapping[Any, Any], depth: int) -> dict[Any, object]:
        """Copy a mapping into a dict, redacting keys and values."""
        result: dict[Any, object] = {}
        for key, item in value.items():
            try:
                new_key = _unique_key(result, self._key(key, depth))
            # No-blind-except yields: a redacted key's __eq__ can raise anything; fail closed.
            except Exception:  # noqa: BLE001
                new_key = _unique_key(result, _UNRENDERABLE)
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
            try:
                name = item[0] if isinstance(item, tuple | list) and len(item) == 2 else None
            # No-blind-except yields: a list subclass's len or [] can raise anything; fail closed.
            except Exception:  # noqa: BLE001
                items.append(_UNRENDERABLE)
                continue
            if name is not None and self.is_sensitive_key(name) and depth + 1 < _MAX_DEPTH:
                pair = (self.value(name, depth + 2), REDACTED)
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
        # No-blind-except yields: a redacted member's hash can raise anything; keep a list.
        except Exception:  # noqa: BLE001
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
    plus ``header_names``, compared after normalisation in any letter case) and sensitive
    ``(name, value)`` pairs lose their values; ``SecretStr`` values are replaced; inside
    every string, plain or percent-encoded, URL passwords, values assigned to sensitive
    keys, sensitive pair reprs and ``Bearer`` tokens, each secret whatever its length
    (longest first, with any email address and any percent-encoded run holding a ``%XX``
    escape that it overlaps) and email addresses are replaced. NaN and infinite floats
    become ``None``, and a value that cannot be rendered becomes ``[UNRENDERABLE]``.
    Containers are walked to a depth of 8. Redacting the result again changes nothing.

    Example::

        >>> redact({"headers": {"Authorization": "Bearer abc", "X-Webhook-Secret": "zzz"}})
        {'headers': {'Authorization': '[REDACTED]', 'X-Webhook-Secret': '[REDACTED]'}}
        >>> redact("contact lead@example.com via postgresql://ads:hunter22@db/x")
        'contact [REDACTED_EMAIL] via postgresql://ads:[REDACTED]@db/x'
        >>> redact("GET /x?access_token=abc&email=lead%40example.com")
        'GET /x?access_token=[REDACTED]&email=[REDACTED_EMAIL]'

    Args:
        value: Any value: a string, a container, a secret or another object.
        secrets: Plaintext secrets to remove from strings, whatever their length; empty
            strings are ignored. A secret is never matched inside a redaction marker.
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
    handler this module installed, which a later :func:`configure_logging` reconfigures
    in place while it is still attached.
    """

    __slots__ = ("handler", "lock", "redactor")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.redactor = _build_redactor((), frozenset({_DEFAULT_SECRET_HEADER}))
        self.handler: _SafeStreamHandler | None = None


_STATE: Final = _LoggingState()


def _aligned_stacks(
    stacks: object,
    exc_type: type[BaseException],
    exc_value: BaseException,
    seen: set[int],
) -> list[tuple[dict[str, Any], BaseException]] | None:
    """Pair each rendered stack dict with the exception it was rendered from.

    The walk is the one :func:`structlog.tracebacks.extract` performs: an exception, the
    members of an exception group (rendered under ``"exceptions"``), then its
    ``__cause__`` when that has a traceback, else its unsuppressed ``__context__`` when
    that has one, never visiting an exception twice.

    Returns:
        The pairs in rendering order, or ``None`` when the rendered list does not line up
        with the walk, in which case nothing may be patched.
    """
    if not isinstance(stacks, list):
        return None
    pairs: list[tuple[dict[str, Any], BaseException]] = []
    index = 0
    current, current_type = exc_value, exc_type
    while id(current) not in seen:
        seen.add(id(current))
        if index >= len(stacks):
            return None
        stack = stacks[index]
        if not isinstance(stack, dict) or stack.get("exc_type") != current_type.__name__:
            return None
        pairs.append((stack, current))
        index += 1
        if isinstance(current, BaseExceptionGroup):
            members = stack.get("exceptions")
            if not isinstance(members, list) or len(members) != len(current.exceptions):
                return None
            for member_stacks, member in zip(members, current.exceptions, strict=True):
                member_pairs = _aligned_stacks(member_stacks, type(member), member, seen)
                if member_pairs is None:
                    return None
                pairs.extend(member_pairs)
        cause = current.__cause__
        if cause and cause.__traceback__:
            current, current_type = cause, cause.__class__
            continue
        context = current.__context__
        if context and context.__traceback__ and not current.__suppress_context__:
            current, current_type = context, context.__class__
            continue
        break
    return pairs if index == len(stacks) else None


def _str_owner(exc_type: type[BaseException]) -> type[BaseException]:
    """Return the class in ``exc_type``'s MRO whose ``__str__`` renders its instances."""
    for klass in exc_type.__mro__:
        if "__str__" in vars(klass) and issubclass(klass, BaseException):
            return klass
    return BaseException


def _redacted_bytes(value: bytes | bytearray, redactor: _Redactor) -> bytes:
    """Redact ``value`` as text and return it as bytes, every other byte kept as it was."""
    text = bytes(value).decode("utf-8", "surrogateescape")
    return redactor.text(text).encode("utf-8", "surrogateescape")


def _constructor_value(original: object, redacted: object, redactor: _Redactor) -> object:
    """Return a redacted form of ``original`` of the type a built-in exception expects.

    A sequence of exceptions (an exception group's members) is kept: a group's text only
    counts its members, and each member's own text is rendered and redacted in its own
    traceback entry. Bytes stay bytes, redacted as text, so a ``UnicodeDecodeError`` can
    be rebuilt. Everything else takes its structurally redacted form ``redacted``.
    """
    if (
        isinstance(original, list | tuple)
        and original
        and all(isinstance(member, BaseException) for member in original)
    ):
        return original
    if isinstance(original, bytes | bytearray):
        return _redacted_bytes(original, redactor)
    return redacted


def _redacted_filename(value: object, redactor: _Redactor) -> object:
    """Return an ``OSError`` file name (``str``, ``bytes``, path or descriptor) redacted.

    A path object is kept when redaction leaves its text unchanged, so it renders as it
    did; otherwise its redacted text takes its place.
    """
    if isinstance(value, os.PathLike):
        path = os.fspath(value)
        redacted = _redacted_filename(path, redactor)
        return value if redacted == path else redacted
    if isinstance(value, bytes | bytearray):
        return _redacted_bytes(value, redactor)
    return redactor.value(value, 1)


def _builtin_text(
    owner: type[BaseException],
    exc: BaseException,
    originals: tuple[object, ...],
    redacted: tuple[object, ...],
    redactor: _Redactor,
) -> str:
    """Render ``exc`` with the built-in ``owner.__str__`` over its redacted arguments.

    A fresh ``owner`` instance is built from the redacted arguments (and, for an
    ``OSError``, its redacted file names), and ``owner.__str__`` renders it, so the text
    keeps its built-in form (``KeyError``'s repr, ``[Errno 2] ...: 'name'``, ``message (2
    sub-exceptions)``) with every sensitive value redacted. When ``owner`` rejects the
    structurally redacted arguments, the forms of :func:`_constructor_value` are tried;
    when neither can be built or rendered, the redacted arguments are rendered as
    ``BaseException`` renders arguments. The original text is never used.

    Args:
        owner: The built-in class whose ``__str__`` renders ``exc``.
        exc: The exception being rendered.
        originals: Its arguments, a sensitive pair's value already replaced.
        redacted: The same arguments, structurally redacted.
        redactor: The active redaction rules.
    """
    candidates = [redacted]
    try:
        typed = tuple(
            _constructor_value(original, value, redactor)
            for original, value in zip(originals, redacted, strict=True)
        )
    # No-blind-except yields: an argument's type checks can raise anything; skip that form.
    except Exception:  # noqa: BLE001
        typed = redacted
    if any(left is not right for left, right in zip(typed, redacted, strict=True)):
        candidates.append(typed)
    for clone_args in candidates:
        text = _clone_text(owner, exc, clone_args, redactor)
        if text is not None:
            return text
    return str(redacted[0]) if len(redacted) == 1 else str(redacted)


def _clone_text(
    owner: type[BaseException],
    exc: BaseException,
    clone_args: tuple[object, ...],
    redactor: _Redactor,
) -> str | None:
    """Return ``owner.__str__`` of a fresh ``owner(*clone_args)``, or ``None`` if that raises.

    For an ``OSError``, the redacted file names of ``exc`` are copied onto the instance.
    """
    try:
        clone = owner(*clone_args)
        if isinstance(exc, OSError) and isinstance(clone, OSError):
            # Only names that are set are copied: OSError renders a name set to None.
            if exc.filename is not None:
                clone.filename = _redacted_filename(exc.filename, redactor)
            if exc.filename2 is not None:
                clone.filename2 = _redacted_filename(exc.filename2, redactor)
        return owner.__str__(clone)
    # No-blind-except yields: a built-in constructor rejects arguments with anything.
    except Exception:  # noqa: BLE001
        return None


def _exception_text(exc: BaseException, redactor: _Redactor) -> str | None:
    """Return the redacted ``exc_value`` text of ``exc``, or ``None`` to keep the rendered one.

    An exception whose ``__str__`` is a built-in one (``BaseException``'s, ``KeyError``'s,
    ``OSError``'s and the like) renders its arguments, so those are redacted as data
    first: a dict argument loses the values of its sensitive keys however they are
    nested, and so does ``KeyError``'s repr of its key. The text is then rendered by that
    ``__str__`` from the redacted arguments (:func:`_builtin_text`). A single string
    argument, and every exception with a ``__str__`` of its own, is left to the text
    rules. An exception whose ``__str__`` raises renders as the marker, never as the error
    text structlog would quote.
    """
    try:
        owner = _str_owner(type(exc))
        if owner.__module__ == "builtins":
            args = exc.args
            if not args or (len(args) == 1 and isinstance(args[0], str)):
                return None
            redacted = redactor.value(args, 0)
            if not isinstance(redacted, tuple):
                return str(redacted)
            originals = tuple(args)
            if len(args) == 2 and redactor.is_sensitive_key(args[0]):
                redacted = (redacted[0], REDACTED)
                originals = (args[0], REDACTED)
            return _builtin_text(owner, exc, originals, redacted, redactor)
        str(exc)
    # No-blind-except yields: __str__ or an argument's repr can raise anything; fail closed.
    except Exception:  # noqa: BLE001
        return _UNRENDERABLE
    return None


class _ExceptionTransformer:
    """Structured tracebacks without frame locals, whose exception arguments are redacted.

    structlog's ``dict_tracebacks`` shows locals truncated to 80 characters, and a
    truncated secret or email can no longer be matched by the substring and email rules,
    so locals are never rendered. Each ``exc_value`` is then replaced by the text of
    :func:`_exception_text` where that differs, using the active redactor; the text rules
    still apply on top when the event is redacted.
    """

    __slots__ = ("_transformer",)

    def __init__(self) -> None:
        self._transformer = ExceptionDictTransformer(show_locals=False)

    def __call__(self, exc_info: ExcInfo) -> list[dict[str, Any]]:
        """Render ``exc_info`` as a list of stack dicts with redacted exception values."""
        stacks = self._transformer(exc_info)
        pairs = _aligned_stacks(stacks, exc_info[0], exc_info[1], set())
        if pairs is not None:
            redactor = _STATE.redactor
            for stack, exc in pairs:
                text = _exception_text(exc, redactor)
                if text is not None:
                    stack["exc_value"] = text
        return stacks


_EXCEPTION_RENDERER: Final = structlog.processors.ExceptionRenderer(_ExceptionTransformer())

# A logger, level or exception class name that a fallback line may carry as it is.
_SAFE_NAME_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,200}")


def _safe_name(value: object) -> str:
    """Return ``value`` when it is a plain name that is safe to log, else ``"unknown"``.

    A name is kept only when it is a plain ``str`` of 1 to 200 letters, digits, ``_``,
    ``.`` or ``-``, and the configured redaction (the active redactor's text rules)
    leaves it unchanged. A name that is, holds or hides a configured secret, an email
    address or a credential is therefore never written, not even partly redacted, and a
    redactor that raises yields ``"unknown"`` as well.
    """
    try:
        if (
            type(value) is str
            and _SAFE_NAME_RE.fullmatch(value) is not None
            and _STATE.redactor.text(value) == value
        ):
            return value
    # No-blind-except yields: a redaction rule can raise anything; fail closed.
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


def _type_name(value: object) -> str:
    """Return the class name of ``value`` as :func:`_safe_name` keeps it, or ``"unknown"``.

    The name is kept only when it passes the safe-name pattern and the configured
    redaction leaves it unchanged.
    """
    try:
        return _safe_name(type(value).__name__)
    # No-blind-except yields: a metaclass's __name__ can raise anything; fail closed.
    except Exception:  # noqa: BLE001
        return "unknown"


def _exc_info_type_name(exc_info: object) -> str:
    """Return the class name of the exception an ``exc_info`` value refers to.

    The name is kept, as :func:`_safe_name` keeps it, only when it passes the safe-name
    pattern and the configured redaction leaves it unchanged; otherwise it is
    ``"unknown"``, so the failure marker of :func:`_render_exception` never carries a
    secret class name, raw or partly redacted.
    """
    try:
        if isinstance(exc_info, BaseException):
            exc_type: object = type(exc_info)
        elif isinstance(exc_info, tuple) and len(exc_info) == 3:
            exc_type = exc_info[0]
        else:
            exc_type = sys.exc_info()[0]
        return _safe_name(getattr(exc_type, "__name__", None))
    # No-blind-except yields: a metaclass's __name__ can raise anything; fail closed.
    except Exception:  # noqa: BLE001
        return "unknown"


def _render_exception(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Render ``exc_info`` as a structured, redacted traceback (a structlog processor).

    When rendering raises, ``exc_info`` is dropped and ``exception`` holds only the logged
    exception's class name and ``[UNRENDERABLE]``: never the error's text, which may quote
    the exception's arguments.
    """
    if "exc_info" not in event_dict:
        return event_dict
    exc_info = event_dict["exc_info"]
    if not isinstance(exc_info, BaseException | tuple):
        # `exc_info=True` names the exception being handled now; a rendering error raised
        # below would take its place in sys.exc_info().
        exc_info = sys.exc_info()
    try:
        return _EXCEPTION_RENDERER(logger, method_name, event_dict)
    # No-blind-except yields: an exception's notes or traceback can raise anything; fail closed.
    except Exception:  # noqa: BLE001
        event_dict.pop("exc_info", None)
        event_dict["exception"] = [
            {"exc_type": _exc_info_type_name(exc_info), "exc_value": _UNRENDERABLE}
        ]
        return event_dict


def _unrenderable_record_line(record: logging.LogRecord, error: BaseException) -> str:
    """Return the JSON line logged in place of a record that could not be rendered.

    It carries the level, the logger name, a UTC timestamp and the error's class name,
    and never the record's message, arguments, values or the error's text. Each name is
    checked against the configured redaction by :func:`_safe_name` and is ``"unknown"``
    unless that leaves it unchanged; the level is checked both as named and lower-cased,
    as it is written.
    """
    try:
        # Secret matching is case-sensitive, so the lower-cased level is checked again.
        level = _safe_name(_safe_name(record.levelname).lower())
        logger = _safe_name(record.name)
    # No-blind-except yields: a LogRecord subclass's attributes can raise anything.
    except Exception:  # noqa: BLE001
        level = logger = "unknown"
    line = {
        "event": "log_record_unrenderable",
        "level": level,
        "logger": logger,
        "timestamp": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "error_type": _type_name(error),
    }
    return json.dumps(line, allow_nan=False)


def _report_handler_error() -> None:
    """Report a failed log write on ``sys.stderr`` as one JSON line naming the error class.

    It replaces :meth:`logging.Handler.handleError`, which prints the record's message
    and arguments with a traceback, unredacted. The error class name is checked against
    the configured redaction by :func:`_type_name` and is ``"unknown"`` unless that leaves
    it unchanged. Like the replaced method, it writes only while
    :data:`logging.raiseExceptions` is true.
    """
    if not logging.raiseExceptions:
        return
    line = {"event": "log_handler_error", "error_type": _type_name(sys.exc_info()[1])}
    try:
        sys.stderr.write(json.dumps(line, allow_nan=False) + "\n")
        sys.stderr.flush()
    except OSError, ValueError, AttributeError:
        # stderr itself is closed, broken or absent; the stdlib drops its report likewise.
        return


class _SafeProcessorFormatter(structlog.stdlib.ProcessorFormatter):
    """A :class:`~structlog.stdlib.ProcessorFormatter` that fails closed.

    Any exception while rendering (a foreign record with bad ``%`` arguments, a value
    JSON cannot encode, a recursion overflow) yields :func:`_unrenderable_record_line`
    instead of reaching the handler, whose stdlib error report would print the raw record.
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as one redacted JSON line, or as the minimal fallback line."""
        try:
            return super().format(record)
        # No-blind-except yields: a record's values can raise anything; the line fails closed.
        except Exception as error:  # noqa: BLE001
            return _unrenderable_record_line(record, error)


class _SafeStreamHandler(logging.StreamHandler[TextIO]):
    """A stream handler whose error report never echoes the record it failed on."""

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Report a failed write as one JSON line on ``sys.stderr``, without the record."""
        _report_handler_error()


class _SafeLastResortHandler(logging.Handler):
    """``logging.lastResort``: a record that no handler takes, as redacted JSON on stderr.

    The standard library's own last resort writes such a record's raw message to stderr.
    This one renders it with the configured formatter instead, and, like the original,
    looks ``sys.stderr`` up when it writes and passes on ``WARNING`` and above only.
    """

    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__(logging.WARNING)
        self.setFormatter(formatter)

    @override
    def emit(self, record: logging.LogRecord) -> None:
        """Write ``record`` as one redacted JSON line to the current ``sys.stderr``."""
        try:
            line = self.format(record)
            stream = sys.stderr
            stream.write(line + "\n")
            stream.flush()
        except RecursionError:
            # As in logging.StreamHandler.emit: never report a recursion overflow by logging.
            raise
        # No-blind-except yields: stderr can raise anything; report without the record.
        except Exception:  # noqa: BLE001
            self.handleError(record)

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Report a failed write as one JSON line on ``sys.stderr``, without the record."""
        _report_handler_error()


def _redact_native_event(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Redact a structlog event before it becomes a standard-library record.

    ``wrap_for_formatter`` hands the event dict to the standard library as
    ``record.msg``, and every handler the record reaches sees it: a handler on a child
    logger, ``logging.lastResort`` or pytest's ``caplog`` may print it without this
    module's formatter. Redacting here leaves no raw value on any record; the formatter's
    :func:`redact_processor` runs again for every record, which changes nothing here and
    covers foreign records. Like :meth:`_Redactor.event`, it never raises.
    """
    return _STATE.redactor.event(event_dict)


def redact_processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Redact every key and value of a structlog event dict (a structlog processor).

    It uses the secrets and secret-header name captured by the latest
    :func:`configure_logging` call. An ``exc_info`` still present is first rendered as a
    structured traceback, with exception arguments redacted as data, so the traceback
    text is redacted too. The input dict is not modified; a redacted copy is returned.
    Redacting an already redacted event changes nothing, and it never raises: a value
    that cannot be rendered becomes ``[UNRENDERABLE]``, and a traceback that cannot be
    rendered keeps only its exception class name.

    Args:
        logger: The wrapped logger (unused beyond traceback rendering).
        method_name: The log method name (unused beyond traceback rendering).
        event_dict: The event to redact.

    Returns:
        A new, redacted event dict.
    """
    if "exc_info" in event_dict:
        event_dict = _render_exception(logger, method_name, dict(event_dict))
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

    That is every value of :meth:`Settings.secret_values` and the n8n webhook URL with its
    path, whatever their length: the webhook URL is not a ``SecretStr``, but it is a
    capability URL that is never logged. The password of each URL-shaped secret, raw and
    percent-decoded, is added too when it has at least :data:`_MIN_SECRET_LENGTH`
    characters; a shorter one, such as the local default database password ``ads``, is
    left to the URL-credential rule, which removes it from its URL.
    """
    values: list[str] = []
    for plaintext in settings.secret_values():
        values.append(plaintext)
        values.extend(
            part for part in _url_secret_parts(plaintext) if len(part) >= _MIN_SECRET_LENGTH
        )
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


# The instance-dict key under which mark_logged records the event that logged an exception.
_LOGGED_AS_KEY: Final[str] = "_ads_engine_logged_as"


def mark_logged(exc: BaseException, event: str) -> None:
    """Record that the owner of a failure's consequence logged ``exc`` in full as ``event``.

    The owner logs the failure once, traceback included; a caller that receives the same
    exception afterwards reads :func:`logged_as` and reports its own outcome without a
    second traceback. The marker is written to the exception's instance dict, which every
    :class:`BaseException` has, so it works for exceptions whose ``__setattr__`` refuses
    (frozen dataclasses) as well. A later call replaces the recorded event.

    Example::

        _log.error("analysis_run_failed", exc_info=exc, run_id=run_id)
        mark_logged(exc, "analysis_run_failed")

    Args:
        exc: The exception that was logged.
        event: The name of the event that logged it.

    Raises:
        ValueError: When ``event`` is empty.
    """
    if not event:
        raise ValueError("event must name the log event that recorded the exception")
    vars(exc)[_LOGGED_AS_KEY] = event


def logged_as(exc: BaseException) -> str | None:
    """Return the event that already logged ``exc`` in full, or ``None``.

    Example::

        owner = logged_as(exc)
        if owner is None:
            _log.error("scheduler.step_failed", exc_info=exc)
        else:
            _log.warning("scheduler.step_failed", logged_as=owner)

    Args:
        exc: An exception, logged or not.

    Returns:
        The event name :func:`mark_logged` recorded for ``exc``, or ``None`` when no owner
        logged it.
    """
    marker = vars(exc).get(_LOGGED_AS_KEY)
    return marker if isinstance(marker, str) else None


class _OwnedExceptionFilter(logging.Filter):
    """Drop a record whose exception an owner already logged in full (:func:`mark_logged`).

    Starlette's ``ServerErrorMiddleware`` re-raises an exception after the API's catch-all
    handler has answered and logged it, and uvicorn then logs it on ``uvicorn.error`` as
    "Exception in ASGI application" with the same traceback. This filter drops that copy.
    """

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        """Return ``False`` when ``record`` carries an exception :func:`logged_as` names."""
        exc_info: object = record.exc_info
        # A record built by hand may hold anything here; only a real exc_info tuple counts.
        if not isinstance(exc_info, tuple) or len(exc_info) != 3:
            return True
        exc: object = exc_info[1]
        return not isinstance(exc, BaseException) or logged_as(exc) is None


_OWNED_EXCEPTION_FILTER: Final = _OwnedExceptionFilter()
"""The one filter instance attached to ``uvicorn.error``, so attaching it again is a no-op."""


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

    Redaction follows them, so rendered tracebacks are redacted as well: for structlog
    events at the end of the native chain and again in the formatter, for foreign records
    in the formatter. JSON rendering comes last, in the formatter.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _render_exception,
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
    structlog events are redacted once more before they become standard-library records,
    so a handler elsewhere in the logger tree never sees a raw value either. A record
    that cannot be rendered, or whose write fails, yields a minimal JSON line naming only
    the error class, never the record. ``logging.lastResort`` is replaced by a handler
    that writes redacted JSON to ``sys.stderr``.

    ``uvicorn.error`` gets a filter that drops a record whose exception was already logged
    in full (:func:`mark_logged`): uvicorn logs every exception that leaves the application
    as "Exception in ASGI application", including one the API's catch-all handler has just
    logged with the same traceback, so each failure keeps one traceback.

    Calling it again reconfigures in place without duplicate handlers: while the handler
    it installed is still attached, its formatter and stream are swapped under the
    handler's lock; otherwise the new handler is attached before any other root handler
    is removed. The root logger is never without a handler, so a record logged
    concurrently is redacted by the old or the new configuration. The new secrets and
    stream apply to loggers created before the call as well.

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
    formatter = _SafeProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            redact_processor,
            structlog.processors.JSONRenderer(default=_json_default, allow_nan=False),
        ],
    )
    target = stream or sys.stdout

    with _STATE.lock:
        _STATE.redactor = _build_redactor(secrets, frozenset({header_name}))
        root = logging.getLogger()
        previous = _STATE.handler
        if previous is not None and previous in root.handlers:
            handler = previous
            # emit() runs under the handler's lock, so a record logged meanwhile is written
            # whole with the old or the new formatter and stream.
            handler.acquire()
            try:
                handler.setFormatter(formatter)
                try:
                    handler.setStream(target)
                except OSError, ValueError:
                    # The old stream is closed and cannot be flushed; it is replaced anyway.
                    handler.stream = target
            finally:
                handler.release()
        else:
            handler = _SafeStreamHandler(target)
            handler.setFormatter(formatter)
            root.addHandler(handler)
        # Other handlers go only once this one is attached: the root logger is never left
        # without a handler, so no record falls through to the last-resort sink meanwhile.
        for existing in list(root.handlers):
            if existing is not handler:
                root.removeHandler(existing)
        if previous is not None and previous is not handler:
            previous.close()
        _STATE.handler = handler
        root.setLevel(root_level)
        logging.lastResort = _SafeLastResortHandler(formatter)
        for name in _QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        for name in _ROUTED_LOGGERS:
            routed = logging.getLogger(name)
            # Propagation first, so a record logged meanwhile still reaches the root handler.
            routed.propagate = True
            for existing in list(routed.handlers):
                routed.removeHandler(existing)
        # The same instance every time, so a repeated configuration attaches it only once.
        logging.getLogger("uvicorn.error").addFilter(_OWNED_EXCEPTION_FILTER)
        structlog.configure(
            processors=[
                # Drop disabled levels before any processor runs.
                structlog.stdlib.filter_by_level,
                *pre_chain,
                # Records leave structlog redacted, whichever handler then sees them.
                _redact_native_event,
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
