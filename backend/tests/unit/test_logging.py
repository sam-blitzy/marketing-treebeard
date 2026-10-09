"""Behavioural tests for ``ads_engine.logging``: JSON lines with secret and email redaction.

Every test runs against an environment stripped of each variable ``Settings`` reads, so
host-exported credentials never reach a test, and the autouse fixture restores the global
state :func:`configure_logging` replaces: root and third-party handlers and levels, the
structlog configuration, the active redactor and the structlog context. No test performs
network or database I/O. Every secret is an obvious test literal and every address uses a
reserved example domain.
"""

import copy
import dataclasses
import datetime as dt
import io
import json
import logging
import numbers
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from collections import UserList, deque
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Final, cast
from urllib.parse import quote, unquote

import pytest
import structlog
from pydantic import SecretBytes, SecretStr

import ads_engine.logging as ads_logging
from ads_engine.logging import (
    EMAIL_RE,
    REDACTED,
    REDACTED_EMAIL,
    SENSITIVE_KEYS,
    URL_CREDENTIALS_RE,
    configure_logging,
    get_logger,
    install_excepthook,
    logged_as,
    mark_logged,
    redact,
    redact_processor,
    redact_secret_matches,
)
from ads_engine.settings import Settings, get_settings

N8N_SECRET = "test-n8n-secret-value"
# A 7-character n8n secret, as a webhook echoed it back in a 403 body.
SHORT_N8N_SECRET = "Zq7#k2p"
SESSION_SECRET = "test-session-secret-value"
BEARER = "Bearer test-bearer-token-value"
HEADER_VALUE = "test-header-value"
LEAD_EMAIL = "lead@example.com"
WEBHOOK_URL = "https://n8n.example.test/webhook/test-webhook-path"
WEBHOOK_PATH = "/webhook/test-webhook-path"
DB_URL = "postgresql+psycopg://ads:test-db-password@db:5432/ads_engine"
DB_URL_REDACTED = "postgresql+psycopg://ads:[REDACTED]@db:5432/ads_engine"
DEFAULT_DB_URL = "postgresql+psycopg://ads:ads@db:5432/ads_engine"
DEFAULT_DB_URL_REDACTED = "postgresql+psycopg://ads:[REDACTED]@db:5432/ads_engine"

# Settings reads exactly its field names (upper-cased) from the environment, no aliases.
SETTINGS_ENV_NAMES = frozenset(Settings.model_fields)

# Loggers configure_logging changes besides the root logger.
CONFIGURED_LOGGERS = (*ads_logging._QUIET_LOGGERS, *ads_logging._ROUTED_LOGGERS)


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the settings environment and restore the global logging state afterwards.

    The root handlers saved here include pytest's own session handlers, which
    ``configure_logging`` removes; they are re-attached, and the handler it installed is
    removed and closed, so other test modules see logging exactly as before. The
    ``logging.lastResort`` handler and ``sys.excepthook`` it replaces are restored too, and
    so are the stream and formatter of a handler installed before the test, which it
    reconfigures in place.
    """
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    # configure_logging installs its sys.excepthook; the hook and the one it replaced are put
    # back afterwards.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(
        ads_logging._STATE, "previous_excepthook", ads_logging._STATE.previous_excepthook
    )

    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    named: dict[str, tuple[int, list[logging.Handler], bool]] = {}
    for name in CONFIGURED_LOGGERS:
        logger = logging.getLogger(name)
        named[name] = (logger.level, list(logger.handlers), logger.propagate)
    structlog_was_configured = structlog.is_configured()
    structlog_config = structlog.get_config()
    state = ads_logging._STATE
    saved_redactor, saved_handler = state.redactor, state.handler
    saved_last_resort = logging.lastResort
    saved_output = (
        (saved_handler.stream, saved_handler.formatter) if saved_handler is not None else None
    )

    yield

    structlog.contextvars.clear_contextvars()
    logging.lastResort = saved_last_resort
    if saved_handler is not None and saved_output is not None:
        saved_handler.setStream(saved_output[0])
        saved_handler.setFormatter(saved_output[1])
    installed = state.handler
    if installed is not None and installed is not saved_handler:
        root.removeHandler(installed)
        installed.close()
    for handler in root_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(root_level)
    for name, (level, handlers, propagate) in named.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                logger.removeHandler(handler)
        for handler in handlers:
            if handler not in logger.handlers:
                logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
    if structlog_was_configured:
        structlog.configure(**structlog_config)
    else:
        structlog.reset_defaults()
    state.redactor, state.handler = saved_redactor, saved_handler
    get_settings.cache_clear()


def _configure(settings: Settings | None = None, *, level: str = "INFO") -> io.StringIO:
    """Configure logging into a fresh buffer, with ``settings`` or the defaults."""
    stream = io.StringIO()
    configure_logging(
        stream=stream, level=level, settings=settings if settings is not None else Settings()
    )
    return stream


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse the buffer as JSON lines, asserting each line is exactly one JSON object."""
    text = stream.getvalue()
    assert text.endswith("\n")
    records = [json.loads(line) for line in text.splitlines()]
    assert all(isinstance(record, dict) for record in records)
    return records


def _nest(value: object, levels: int) -> object:
    """Wrap ``value`` in ``levels`` single-item lists."""
    for _ in range(levels):
        value = [value]
    return value


class _Hooked:
    """An object that renders itself through structlog's ``__structlog__`` hook."""

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __structlog__(self) -> object:
        return self.payload

    def __repr__(self) -> str:
        return f"<_Hooked for {LEAD_EMAIL}>"


class _Opaque:
    """An object JSON cannot render, whose ``repr`` carries an email and a secret."""

    def __repr__(self) -> str:
        return f"<_Opaque {LEAD_EMAIL} {N8N_SECRET}>"


# ---------------------------------------------------------------------------
# redact(): sensitive keys and header pairs
# ---------------------------------------------------------------------------


def test_required_header_names_are_sensitive_keys() -> None:
    assert {"authorization", "cookie", "x_webhook_secret", "n8n_webhook_secret"} <= SENSITIVE_KEYS


@pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
def test_every_sensitive_key_loses_its_whole_value(key: str) -> None:
    assert redact({key: {"nested": "value"}, "kept": 1}) == {key: REDACTED, "kept": 1}


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "  authorization ",
        "Proxy-Authorization",
        "Set-Cookie",
        "X-Webhook-Secret",
        "X-API-Key",
        "accessToken",
        "refreshToken",
        "clientSecret",
        "idToken",
        "sessionSecret",
        "passwordHash",
    ],
)
def test_key_names_are_normalised_before_matching(key: str) -> None:
    assert redact({key: HEADER_VALUE}) == {key: REDACTED}


@pytest.mark.parametrize(
    "key",
    [
        "AuThOrIzAtIoN",
        "AUTHORIZATION",
        "PROXY-AUTHORIZATION",
        "pRoXy-AuThOrIzAtIoN",
        "CooKie",
        "Set-COOKIE",
        "X-WeBhOoK-SeCrEt",
        "ACCESS_TOKEN",
        "X-Webhook-TOKEN",
    ],
)
def test_mixed_case_header_names_are_sensitive(key: str) -> None:
    assert redact({key: HEADER_VALUE}) == {key: REDACTED}
    assert redact([(key, HEADER_VALUE)]) == [(key, REDACTED)]
    assert redact([(key.encode(), HEADER_VALUE.encode())]) == [(key, REDACTED)]


@pytest.mark.parametrize(
    "key",
    [
        "accesstoken",
        "ACCESSTOKEN",
        "accessTokeN",
        "refreshtoken",
        "rEfReShToKeN",
        "clientsecret",
        "CLIENTSECRET",
        "apikey",
        "XAPIKEY",
        "proxyauthorization",
        "PROXYAUTHORIZATION",
        "setcookie",
        "SetcooKie",
        "idtoken",
        "hubspotaccesstoken",
        "sessionsecret",
        "APPADMINPASSWORDHASH",
        "n8nwebhooksecret",
        "XWEBHOOKSECRET",
        "XWEBHOOKTOKEN",
        "linkedinclientsecret",
        "dbpassword",
        "hubspotapikey",
    ],
)
def test_separator_free_key_names_are_sensitive(key: str) -> None:
    assert redact({key: HEADER_VALUE}) == {key: REDACTED}
    assert redact({key.encode(): HEADER_VALUE}) == {key: REDACTED}
    assert redact([(key, HEADER_VALUE)]) == [(key, REDACTED)]
    assert redact([(key.encode(), HEADER_VALUE.encode())]) == [(key, REDACTED)]
    assert redact(f"{key}={HEADER_VALUE}&limit=1") == f"{key}={REDACTED}&limit=1"


@pytest.mark.parametrize(
    "key",
    [
        "tokencount",
        "TOKENCOUNT",
        "maxtokens",
        "tokens",
        "secretary",
        "apikeys",
        "passwordsseen",
        "pagesize",
        "idproperty",
        "confighash",
        "withheld",
        "xcustomauth",
    ],
)
def test_separator_free_ordinary_keys_keep_their_values(key: str) -> None:
    assert redact({key: HEADER_VALUE}) == {key: HEADER_VALUE}
    assert redact([(key, HEADER_VALUE)]) == [(key, HEADER_VALUE)]
    assert redact(f"{key}={HEADER_VALUE}") == f"{key}={HEADER_VALUE}"


@pytest.mark.parametrize("key", ["XCUSTOMAUTH", "xcustomauth", "XCustomAuth", b"XCUSTOMAUTH"])
def test_configured_header_name_matches_without_separators(key: str | bytes) -> None:
    name = key.decode() if isinstance(key, bytes) else key
    assert redact({key: HEADER_VALUE}, header_names=["X-Custom-Auth"]) == {name: REDACTED}
    assert redact([(key, HEADER_VALUE)], header_names=["X-Custom-Auth"]) == [(name, REDACTED)]
    assert redact({key: HEADER_VALUE}) == {name: HEADER_VALUE}


class _BadBufferBytes(bytes):
    """A ``bytes`` name whose buffer cannot be read, so normalising it raises."""

    def __buffer__(self, flags: int) -> memoryview:
        raise RuntimeError(f"buffer failed for {HEADER_VALUE}")


def test_a_name_that_cannot_be_read_fails_closed() -> None:
    redacted = redact({_BadBufferBytes(b"limit"): HEADER_VALUE})
    assert isinstance(redacted, dict)
    assert list(redacted.values()) == [REDACTED]


@pytest.mark.parametrize(
    "key", ["x-CuStOm-AuTh", "X-CUSTOM-AUTH", "x_custom_auth", b"x-CuStOm-AuTh"]
)
def test_configured_header_name_matches_in_any_letter_case(key: str | bytes) -> None:
    name = key.decode() if isinstance(key, bytes) else key
    assert redact({key: HEADER_VALUE}, header_names=["X-Custom-Auth"]) == {name: REDACTED}
    assert redact([(key, HEADER_VALUE)], header_names=["X-Custom-Auth"]) == [(name, REDACTED)]
    assert redact({key: HEADER_VALUE}) == {name: HEADER_VALUE}


@pytest.mark.parametrize(
    "key",
    [
        "hubspot_access_token",
        "HUBSPOT_ACCESS_TOKEN",
        "linkedin_client_secret",
        "linkedinClientSecret",
        "X-Webhook-Token",
        "csrfToken",
        "db_password",
        "hubspot_api_key",
        b"x-webhook-token",
    ],
)
def test_sensitive_suffixes_widen_the_key_rule(key: str | bytes) -> None:
    assert redact({key: HEADER_VALUE}) == {
        key.decode() if isinstance(key, bytes) else key: REDACTED
    }


@pytest.mark.parametrize(
    "key", ["event", "token_count", "tokens", "secretary", "api_keys", "Accept", "passwords_seen"]
)
def test_ordinary_keys_keep_their_values(key: str) -> None:
    assert redact({key: HEADER_VALUE}) == {key: HEADER_VALUE}


def test_non_string_keys_are_never_sensitive() -> None:
    assert redact({1: HEADER_VALUE, None: HEADER_VALUE}) == {1: HEADER_VALUE, None: HEADER_VALUE}


def test_header_names_extend_the_sensitive_keys() -> None:
    value = {"x-hook-auth": HEADER_VALUE, "X_Hook_Auth": HEADER_VALUE, "X-Other": HEADER_VALUE}
    assert redact(value, header_names=["X-Hook-Auth", "  "]) == {
        "x-hook-auth": REDACTED,
        "X_Hook_Auth": REDACTED,
        "X-Other": HEADER_VALUE,
    }
    assert redact(value) == value


def test_header_pairs_lose_their_values() -> None:
    headers = [
        ("Authorization", BEARER),
        ["Cookie", "session=test-cookie-value"],
        (b"x-webhook-secret", N8N_SECRET.encode()),
        ("Accept", "application/json"),
        ("Authorization", "three", "items"),
        (1, 2),
    ]
    assert redact(headers) == [
        ("Authorization", REDACTED),
        ["Cookie", REDACTED],
        ("x-webhook-secret", REDACTED),
        ("Accept", "application/json"),
        ("Authorization", "three", "items"),
        (1, 2),
    ]


def test_header_pair_at_the_depth_limit_is_replaced_whole() -> None:
    assert redact(_nest(("Authorization", BEARER), 7)) == _nest(("Authorization", REDACTED), 7)
    assert redact(_nest(("Authorization", BEARER), 8)) == _nest(REDACTED, 8)


# ---------------------------------------------------------------------------
# redact(): values inside strings
# ---------------------------------------------------------------------------


def test_secret_types_are_redacted() -> None:
    value = [SecretStr(N8N_SECRET), SecretBytes(N8N_SECRET.encode()), {"v": SecretStr("x")}]
    assert redact(value) == [REDACTED, REDACTED, {"v": REDACTED}]
    assert redact(SecretStr(N8N_SECRET)) == REDACTED


def test_configured_secrets_are_replaced_inside_strings() -> None:
    text = f"calling with {N8N_SECRET} and again {N8N_SECRET}."
    assert redact(text, secrets=[N8N_SECRET]) == "calling with [REDACTED] and again [REDACTED]."
    assert redact({"detail": text}, secrets=(N8N_SECRET,)) == {
        "detail": "calling with [REDACTED] and again [REDACTED]."
    }


def test_secrets_of_any_length_are_redacted() -> None:
    secrets = (SHORT_N8N_SECRET, "eight8ch", "ads")
    assert (
        redact(f"{SHORT_N8N_SECRET} eight8ch ads_engine", secrets=secrets)
        == "[REDACTED] [REDACTED] [REDACTED]_engine"
    )
    assert redact("wrong secret Zq7%23k2p", secrets=secrets) == "wrong secret [REDACTED]"


def test_an_empty_secret_matches_nothing() -> None:
    assert redact("nothing to hide", secrets=("",)) == "nothing to hide"


@pytest.mark.parametrize(
    ("text", "secrets", "expected"),
    [
        pytest.param("[REDACTED] ACT", ("ACT",), "[REDACTED] [REDACTED]", id="inside-a-marker"),
        pytest.param("[REDACTED] D] end", ("D]",), "[REDACTED] [REDACTED] end", id="marker-tail"),
        pytest.param(
            "[REDACTED_EMAIL] [UNRENDERABLE] EMAIL ]",
            ("EMAIL", "[", "]"),
            "[REDACTED_EMAIL] [UNRENDERABLE] [REDACTED] [REDACTED]",
            id="every-marker",
        ),
        pytest.param(
            "Authorization: Bearer test-a", ("ACT", "ED]"), "Authorization: [REDACTED]", id="key"
        ),
        pytest.param("to lead@example.com", ("EMAIL",), "to [REDACTED_EMAIL]", id="email"),
        pytest.param("[REDACTED]x and ]x", ("]x",), "[REDACTED] and [REDACTED]", id="straddle"),
    ],
)
def test_short_secrets_never_match_inside_a_redaction_marker(
    text: str, secrets: tuple[str, ...], expected: str
) -> None:
    once = redact(text, secrets=secrets)
    assert once == expected
    assert redact(once, secrets=secrets) == once


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        pytest.param("[REDACTED]x", "]x", id="last-character"),
        pytest.param("seen [REDACTED]abc", "D]abc", id="longer-tail"),
        pytest.param("lead [REDACTED]]x", "]]x", id="doubled-bracket"),
        pytest.param("[REDACTED_EMAIL]abc", "L]abc", id="email-marker"),
        pytest.param("[UNRENDERABLE]!", "ABLE]!", id="unrenderable-marker"),
        pytest.param("[REDACTED]x", "[REDACTED]x", id="whole-marker"),
    ],
)
def test_a_secret_running_past_a_marker_end_is_redacted_with_it(text: str, secret: str) -> None:
    once = redact(text, secrets=[secret])
    assert secret not in once
    assert once.endswith(REDACTED)
    assert redact(once, secrets=[secret]) == once


@pytest.mark.parametrize(
    ("text", "secrets", "expected"),
    [
        pytest.param("contact jane2024@example.com", ("2024",), "contact [REDACTED]", id="inside"),
        pytest.param(
            "to 2024ann@example.com, 2024", ("2024",), "to [REDACTED], [REDACTED]", id="start"
        ),
        pytest.param(
            "email jane%402024x.io sent", ("2024",), "email [REDACTED] sent", id="encoded"
        ),
        pytest.param("e jane%25402024x.io", ("2024",), "e [REDACTED]", id="double-encoded"),
        pytest.param(
            "2024 and lead@example.com", ("2024",), "[REDACTED] and [REDACTED_EMAIL]", id="apart"
        ),
        pytest.param(
            "x!abc@def.gh then abc@def.gh!x",
            ("x!abc@def.gh", "abc@def.gh!x"),
            "[REDACTED] then [REDACTED]",
            id="secret-holding-an-address",
        ),
    ],
)
def test_a_secret_takes_every_email_address_it_overlaps_with_it(
    text: str, secrets: tuple[str, ...], expected: str
) -> None:
    once = redact(text, secrets=secrets)
    assert once == expected
    for fragment in ("jane", "ann", "lead@", "example.com", "x.io", "def.gh"):
        assert fragment not in once
    assert redact(once, secrets=secrets) == once


def test_redact_secret_matches_keeps_marker_matches_and_widens_over_addresses() -> None:
    assert (
        redact_secret_matches("jane2024@x.io and 2024", re.compile("2024"))
        == "[REDACTED] and [REDACTED]"
    )
    pattern = re.compile(r"(?P<marker>\[REDACTED\])|ACT")
    assert redact_secret_matches("[REDACTED] ACT", pattern) == "[REDACTED] [REDACTED]"
    assert redact_secret_matches("nothing here", pattern) == "nothing here"


def test_redact_secret_matches_widens_over_encoded_runs_with_a_valid_escape() -> None:
    assert (
        redact_secret_matches("GET access_token%3Dtok 100% 3D", re.compile("3D"))
        == "GET [REDACTED] 100% [REDACTED]"
    )
    assert redact_secret_matches("x%ZZ y", re.compile("ZZ")) == "x%[REDACTED] y"


@pytest.mark.parametrize(
    ("text", "secret", "expected", "credential"),
    [
        pytest.param(
            "sent access_token%3Dtest-refreshed-token now",
            "3D",
            "sent [REDACTED] now",
            "test-refreshed-token",
            id="inside-an-escape",
        ),
        pytest.param(
            "sent access_token%3Dtok now", "%", "sent [REDACTED] now", "tok", id="percent-sign"
        ),
        pytest.param("auth Bearer%20tok end", "%", "auth [REDACTED] end", "tok", id="bearer"),
        pytest.param(
            "url postgresql%3A%2F%2Fu%3Apw%40db end",
            "%",
            "url [REDACTED] end",
            "pw",
            id="url-password",
        ),
        pytest.param(
            "auth Bearer%20test-bearer end", "20", "auth [REDACTED] end", "test-bearer", id="space"
        ),
        pytest.param(
            "h Authorization%3A%20Bearer%20test-a end",
            "3A",
            "h [REDACTED] end",
            "test-a",
            id="authorization",
        ),
        pytest.param(
            "sent access_token%3Dabc now", "token", "sent [REDACTED] now", "abc", id="encoded-key"
        ),
        pytest.param(
            "x access_token%253Dtest-refreshed-token y",
            "3D",
            "x [REDACTED] y",
            "test-refreshed-token",
            id="double-encoded",
        ),
    ],
)
def test_a_secret_takes_every_encoded_run_it_overlaps_with_it(
    text: str, secret: str, expected: str, credential: str
) -> None:
    once = redact(text, secrets=[secret])
    assert once == expected
    assert credential not in once
    assert secret not in once
    assert "%[REDACTED]" not in once
    assert redact(once, secrets=[secret]) == once


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        pytest.param("100% done", "100", "[REDACTED]% done", id="dangling-percent"),
        pytest.param("100% done", "%", "100[REDACTED] done", id="percent-sign"),
        pytest.param("got %ZZ here", "ZZ", "got %[REDACTED] here", id="invalid-escape"),
    ],
)
def test_a_run_without_a_valid_escape_keeps_its_other_text(
    text: str, secret: str, expected: str
) -> None:
    once = redact(text, secrets=[secret])
    assert once == expected
    assert redact(once, secrets=[secret]) == once


def test_longest_secret_is_redacted_first() -> None:
    secrets = ("test-secret-value", "test-secret-value-extended")
    assert redact("use test-secret-value-extended now", secrets=secrets) == "use [REDACTED] now"
    assert redact("use test-secret-value now", secrets=secrets) == "use [REDACTED] now"


def test_secret_metacharacters_match_literally() -> None:
    secrets = ("test.secret*value",)
    assert redact("a test.secret*value b", secrets=secrets) == "a [REDACTED] b"
    assert redact("a testXsecretvalue b", secrets=secrets) == "a testXsecretvalue b"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (DB_URL, DB_URL_REDACTED),
        (
            "see https://svc:p%40ss-test@host.example.test/x?y=1",
            "see https://svc:[REDACTED]@host.example.test/x?y=1",
        ),
        ("redis://:test-redis-password@cache:6379/0", "redis://:[REDACTED]@cache:6379/0"),
        ("https://host.example.test/path", "https://host.example.test/path"),
    ],
)
def test_url_passwords_are_redacted_and_users_kept(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_exported_patterns_match_their_documented_shapes() -> None:
    match = URL_CREDENTIALS_RE.search(DB_URL)
    assert match is not None
    assert match.group("password") == "test-db-password"
    assert EMAIL_RE.fullmatch(LEAD_EMAIL) is not None
    assert EMAIL_RE.search(DB_URL_REDACTED) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"contact {LEAD_EMAIL}", "contact [REDACTED_EMAIL]"),
        ("First.Last+tag@sub.example.test replied", "[REDACTED_EMAIL] replied"),
        ("lead sim-0001@sim.invalid", "lead [REDACTED_EMAIL]"),
        ("no address here", "no address here"),
    ],
)
def test_email_addresses_are_redacted(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize("text", ["a@b.c+d@e.f", "a@b.c_d@e.f", "a@b.c..d@e.f", "a@b.c%d@e.f"])
def test_glued_email_addresses_leave_no_address_behind(text: str) -> None:
    assert redact(text) == REDACTED_EMAIL
    assert EMAIL_RE.sub(REDACTED_EMAIL, f"to {text}.") == f"to {REDACTED_EMAIL}."


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * 131072 + "@x", id="long-local-part"),
        pytest.param("a." * 50000 + "@x", id="dotted-local-part"),
        pytest.param("a@" + "b-" * 50000, id="long-domain-label"),
        pytest.param("a@b" + ".c" * 50000 + "!", id="many-domain-labels"),
        pytest.param("http://" + "u" * 100000 + "@x", id="url-without-password"),
        pytest.param("token=" * 20000 + "Bearer " * 20000, id="assignments-and-bearer"),
        pytest.param("%41" * 40000 + " " + "('a', '" * 10000, id="encoded-and-pairs"),
    ],
)
def test_malformed_input_is_redacted_in_linear_time(text: str) -> None:
    # Backtracking patterns took seconds to minutes on these; a linear scan takes ms.
    started = time.perf_counter()
    redact(text, secrets=[N8N_SECRET])
    assert time.perf_counter() - started < 1.0


def test_long_text_keeps_its_length_and_loses_only_the_address() -> None:
    text = "x" * 50000 + " " + LEAD_EMAIL + " " + "-" * 50000
    assert redact(text) == "x" * 50000 + " " + REDACTED_EMAIL + " " + "-" * 50000


# ---------------------------------------------------------------------------
# redact(): credentials inside text, whatever secrets are configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Authorization: Bearer test-unknown-token",
            "Authorization: [REDACTED]",
            id="header-text",
        ),
        pytest.param(
            "Proxy-Authorization: Basic dGVzdDp0ZXN0\r\nAccept: json",
            "Proxy-Authorization: [REDACTED]\r\nAccept: json",
            id="proxy-header-text",
        ),
        pytest.param(
            "Cookie: session=test-cookie; theme=dark", "Cookie: [REDACTED]", id="cookie-header"
        ),
        pytest.param(
            "AUTHORIZATION:Bearer test-unknown-token, next\nAccept: json",
            "AUTHORIZATION:[REDACTED]\nAccept: json",
            id="mixed-case-header-text",
        ),
        pytest.param(
            "sent Bearer test-unknown-token upstream",
            "sent Bearer [REDACTED] upstream",
            id="standalone-bearer",
        ),
        pytest.param(
            "https://api.example.test/x?access_token=test-a&refresh_token=test-r&limit=5",
            "https://api.example.test/x?access_token=[REDACTED]&refresh_token=[REDACTED]&limit=5",
            id="url-query",
        ),
        pytest.param(
            "grant_type=refresh_token&refresh_token=test-r&client_secret=test-c",
            "grant_type=refresh_token&refresh_token=[REDACTED]&client_secret=[REDACTED]",
            id="form-body",
        ),
        pytest.param(
            '{"access_token": "test-a", "expires_in": 5184000}',
            '{"access_token": "[REDACTED]", "expires_in": 5184000}',
            id="json-body",
        ),
        pytest.param(
            '{\\"refresh_token\\": \\"test-r\\"}',
            '{\\"refresh_token\\": \\"[REDACTED]\\"}',
            id="escaped-json-body",
        ),
        pytest.param(
            "body={'refresh_token': 'test-r', 'n': 1}",
            "body={'refresh_token': '[REDACTED]', 'n': 1}",
            id="dict-repr",
        ),
        pytest.param(
            "{b'authorization': b'Bearer test-a'}",
            "{b'authorization': b'[REDACTED]'}",
            id="bytes-dict-repr",
        ),
        pytest.param(
            "[('Authorization', 'Bearer test-a'), (b'Cookie', b's=test-c'), ['X-Api-Key', 'k']]",
            "[('Authorization', '[REDACTED]'), (b'Cookie', b'[REDACTED]'), "
            "['X-Api-Key', '[REDACTED]']]",
            id="pair-reprs",
        ),
        pytest.param(
            "settings.access_token=test-a accessToken=test-b refreshToken: test-c",
            "settings.access_token=[REDACTED] accessToken=[REDACTED] refreshToken: [REDACTED]",
            id="dotted-and-camel-case-keys",
        ),
        pytest.param(
            "authorization=Bearer test-a&x=1\nnext",
            "authorization=[REDACTED]\nnext",
            id="scheme-after-=",
        ),
        pytest.param("password='test-cut-off", "password='[REDACTED]", id="unterminated-quote"),
    ],
)
def test_textual_credentials_are_redacted_without_configured_secrets(
    text: str, expected: str
) -> None:
    assert redact(text) == expected


ROTATED = "test-rotated-token"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            f'Cookie: session="{ROTATED}"; theme=dark\nAccept: json',
            "Cookie: [REDACTED]\nAccept: json",
            id="cookie-with-quoted-values",
        ),
        pytest.param(f"Cookie: '{ROTATED}'", "Cookie: [REDACTED]", id="quoted-header-value"),
        pytest.param(
            f"Cookie: session=b'{ROTATED}'", "Cookie: [REDACTED]", id="bytes-header-value"
        ),
        pytest.param(
            f'Authorization: Digest username="u", realm="r", nonce="{ROTATED}", '
            f'response="{ROTATED}"',
            "Authorization: [REDACTED]",
            id="digest-parameters",
        ),
        pytest.param(
            f"Set-Cookie: sid={ROTATED}; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Path=/",
            "Set-Cookie: [REDACTED]",
            id="set-cookie-with-expiry",
        ),
        pytest.param(
            f'Hello, Authorization: Digest nonce="{ROTATED}", response="{ROTATED}"',
            "Hello, Authorization: [REDACTED]",
            id="scheme-after-a-comma",
        ),
        pytest.param(
            f'{{"refresh_token": ["{ROTATED}"], "n": 1}}',
            '{"refresh_token": [REDACTED], "n": 1}',
            id="json-list-value",
        ),
        pytest.param(
            f"{{'access_token': {{'value': '{ROTATED}'}}, 'n': 1}}",
            "{'access_token': [REDACTED], 'n': 1}",
            id="dict-repr-nested-value",
        ),
        pytest.param(
            f"{{'token': Token(value='{ROTATED}'), 'x': 1}}",
            "{'token': [REDACTED], 'x': 1}",
            id="object-repr-value",
        ),
        pytest.param('{"token": null, "x": 1}', '{"token": [REDACTED], "x": 1}', id="json-null"),
        pytest.param(
            f"Client(access_token={ROTATED}, retries=3)",
            "Client(access_token=[REDACTED], retries=3)",
            id="keyword-argument-repr",
        ),
        pytest.param(
            f'{{"body": "{{\\"refresh_token\\": [\\"{ROTATED}\\"], \\"n\\": 1}}"}}',
            '{"body": "{\\"refresh_token\\": [REDACTED], \\"n\\": 1}"}',
            id="escaped-json-list-value",
        ),
        pytest.param(
            f"[('Authorization', ['Bearer {ROTATED}']), ('Accept', 'json')]",
            "[('Authorization', [REDACTED]), ('Accept', 'json')]",
            id="pair-repr-list-value",
        ),
        pytest.param(
            f"token=Token(value='{ROTATED}') sent",
            "token=[REDACTED] sent",
            id="token-with-group-after-=",
        ),
        pytest.param(f"token=({ROTATED} x)&n=1", "token=[REDACTED]&n=1", id="group-after-="),
        pytest.param(
            f'authorization=Digest username="u", nonce="{ROTATED}"\nnext',
            "authorization=[REDACTED]\nnext",
            id="digest-after-=",
        ),
        pytest.param(
            f"token: {{\n  'value': '{ROTATED}'\n}} tail\nnext",
            "token: [REDACTED]\nnext",
            id="header-group-across-lines",
        ),
        pytest.param(
            f"token: [{ROTATED}\nmore text", "token: [REDACTED]", id="unclosed-group-fails-closed"
        ),
    ],
)
def test_sensitive_values_are_replaced_whole_however_quoted_or_nested(
    text: str, expected: str
) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "{'token': [",
        "{'token': [REDACTED], ",
        "(token=",
        ", token: ",
        "token: (",
        "token: [REDACTED]\n",
        "token=a(",
        "token: {'",
        "[('token', [",
    ],
)
def test_value_extent_scan_runs_in_linear_time(text: str) -> None:
    started = time.perf_counter()
    once = redact(text * (100_000 // len(text)))
    assert time.perf_counter() - started < 1.0
    assert redact(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "sync_job_finished rows=120 token_count=3 the token bucket is full",
        "path=/crm/v3/objects limit=100 at 12:30:00 status: ok",
        "token: ",
        "password=",
        "Accept: application/json, Content-Type: text/plain",
        "SELECT campaign.id FROM campaign WHERE campaign.status = 'ENABLED' LIMIT 10",
        "{'status': 'ok', 'rows': [1, 2], 'next': None}",
    ],
)
def test_ordinary_text_is_left_unchanged(text: str) -> None:
    assert redact(text) == text


def test_assignment_after_an_ordinary_key_is_still_found() -> None:
    text = "error: request failed access_token=test-a, detail: Authorization: Bearer test-b"
    assert (
        redact(text)
        == "error: request failed access_token=[REDACTED], detail: Authorization: [REDACTED]"
    )


@pytest.mark.parametrize(
    "text",
    [
        "access_token=[REDACTED]&limit=1",
        "postgresql+psycopg://ads:[REDACTED]@db:5432/ads_engine",
        "/crm/x?hapikey=[REDACTED]&email=[REDACTED_EMAIL]",
        "Authorization: [REDACTED]",
        "Bearer [REDACTED]",
        "{'refresh_token': '[REDACTED]'}",
        "[('Cookie', '[REDACTED]')]",
        "value [UNRENDERABLE]",
        "{'access_token': [REDACTED], 'n': 1}",
        "[('Authorization', [REDACTED])]",
        "token=[REDACTED] sent",
    ],
)
def test_redacted_text_survives_a_second_pass_unchanged(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer test-a lead@example.com https://h.example.test/?token=t&e=lead%40x.io",
        "{'password': 'test-p', 'headers': [('Cookie', 's=1')]} via redis://:test-pw@cache:6379",
        "x=%2541%40example.com Bearer abc token%3Dtest-q",
        "token: [abc] tail, {'refresh_token': {'v': 'x'}} Cookie: a=\"b\"",
    ],
)
def test_redaction_is_idempotent_on(text: str) -> None:
    once = redact(text, secrets=[N8N_SECRET])
    assert redact(once, secrets=[N8N_SECRET]) == once


# ---------------------------------------------------------------------------
# redact(): percent-encoded values
# ---------------------------------------------------------------------------

ENCODED_SECRET = "%74%65%73%74%2D%6E%38%6E-secret-value"  # N8N_SECRET, partly encoded
ENCODED_SECRET_LOWER = ENCODED_SECRET.lower()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "https://api.example.test/x?email=lead%40example.com",
            "https://api.example.test/x?email=[REDACTED_EMAIL]",
            id="encoded-email",
        ),
        pytest.param(
            "https://api.example.test/x?email=lead%2540example.com",
            "https://api.example.test/x?email=[REDACTED_EMAIL]",
            id="double-encoded-email",
        ),
        pytest.param(
            "q=lead%40Example.COM and LEAD%40example.com",
            "q=[REDACTED_EMAIL] and [REDACTED_EMAIL]",
            id="mixed-case-email",
        ),
        pytest.param(
            "email=lead%2Btag%40example.com",
            "email=[REDACTED_EMAIL]",
            id="encoded-plus-tag",
        ),
        pytest.param(
            f"https://x.example.test/?s={ENCODED_SECRET}&email=lead%40example.com",
            "https://x.example.test/?s=[REDACTED]&email=[REDACTED_EMAIL]",
            id="encoded-secret-and-email",
        ),
        pytest.param(f"v={ENCODED_SECRET_LOWER}", "v=[REDACTED]", id="lowercase-escapes"),
        pytest.param(
            "x?access_token%3Dtest-a&y=1", "x?access_token=[REDACTED]&y=1", id="encoded-assignment"
        ),
        pytest.param(
            "Authorization%3A%20Bearer%20test-a", "Authorization: [REDACTED]", id="encoded-header"
        ),
        pytest.param(
            "a%0Alead%40example.com%0D%E2%80%A8",
            "a%0A[REDACTED_EMAIL]%0D%E2%80%A8",
            id="controls-stay-encoded",
        ),
    ],
)
def test_percent_encoded_values_are_redacted(text: str, expected: str) -> None:
    assert redact(text, secrets=[N8N_SECRET]) == expected


@pytest.mark.parametrize(
    "text",
    [
        "path=/crm/v3/objects%2Fcontacts limit=100 100%25 done",
        "%ZZ not an escape, lead%40x is no address",
        "q=%E2%9C%93%20ok",
    ],
)
def test_percent_encoded_text_without_findings_is_kept_byte_for_byte(text: str) -> None:
    assert redact(text, secrets=[N8N_SECRET]) == text


def test_foreign_records_with_encoded_values_are_redacted() -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    logging.getLogger("httpx").warning(
        "HTTP Request: GET https://x.example.test/?email=lead%%40example.com"
        "&access_token=test-rotated-access&s=%s",
        ENCODED_SECRET,
    )

    output = stream.getvalue()
    for plaintext in ("lead", "test-rotated-access", "secret-value"):
        assert plaintext not in output
    (record,) = _lines(stream)
    assert record["event"] == (
        "HTTP Request: GET https://x.example.test/?email=[REDACTED_EMAIL]"
        "&access_token=[REDACTED]&s=[REDACTED]"
    )


def _layered(text: str, layers: int) -> str:
    """Return ``email=<LEAD_EMAIL> s=<N8N_SECRET> <text>``, both values encoded ``layers`` times."""
    email, secret = "lead%40example.com", "%74est-n8n-secret-value"
    for _ in range(layers - 1):
        email, secret = email.replace("%", "%25"), secret.replace("%", "%25")
    return f"email={email} s={secret} {text}"


def _fully_decoded(text: str) -> str:
    """Percent-decode ``text`` until it no longer changes."""
    while (decoded := unquote(text)) != text:
        text = decoded
    return text


@pytest.mark.parametrize("layers", [1, 2, 3, 4])
def test_values_encoded_up_to_the_decode_limit_are_redacted(layers: int) -> None:
    text = _layered("rejected", layers)
    assert redact(text, secrets=[N8N_SECRET]) == "email=[REDACTED_EMAIL] s=[REDACTED] rejected"


@pytest.mark.parametrize("layers", [5, 6])
def test_values_encoded_beyond_the_decode_limit_fail_closed(layers: int) -> None:
    result = str(redact(_layered("rejected", layers), secrets=[N8N_SECRET]))
    assert result == "[REDACTED] [REDACTED] rejected"
    decoded = _fully_decoded(result)
    assert LEAD_EMAIL not in decoded
    assert N8N_SECRET not in decoded


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        pytest.param(4, "email=[REDACTED_EMAIL] s=[REDACTED] rejected", id="at-the-limit"),
        pytest.param(5, "[REDACTED] [REDACTED] rejected", id="one-beyond"),
        pytest.param(6, "[REDACTED] [REDACTED] rejected", id="two-beyond"),
    ],
)
def test_deeply_encoded_values_in_foreign_records_and_exception_text_never_leak(
    layers: int, expected: str
) -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    text = _layered("rejected", layers)
    logging.getLogger("tests.third_party").warning("lookup failed: %s", text)
    try:
        raise RuntimeError(text)
    except RuntimeError:
        get_logger("tests.logging.encoded").exception("call_failed")

    decoded = _fully_decoded(stream.getvalue())
    assert LEAD_EMAIL not in decoded
    assert N8N_SECRET not in decoded
    foreign, native = _lines(stream)
    assert foreign["event"] == f"lookup failed: {expected}"
    assert native["exception"][0]["exc_value"] == expected


def test_bytes_are_decoded_and_redacted() -> None:
    raw = f"to {LEAD_EMAIL}".encode()
    assert redact(raw) == "to [REDACTED_EMAIL]"
    assert redact(bytearray(raw)) == "to [REDACTED_EMAIL]"
    assert redact(memoryview(raw)) == "to [REDACTED_EMAIL]"
    assert redact(b"\xff " + raw) == "\\xff to [REDACTED_EMAIL]"


@pytest.mark.parametrize(
    "value",
    [
        42,
        2.5,
        True,
        Decimal("1.25"),
        dt.date(2026, 10, 1),
        dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC),
        dt.time(9, 30),
        dt.timedelta(seconds=5),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        None,
    ],
)
def test_safe_scalars_are_returned_unchanged(value: object) -> None:
    assert redact(value) is value


# ---------------------------------------------------------------------------
# redact(): containers, depth, keys and objects
# ---------------------------------------------------------------------------


def test_input_is_copied_and_never_mutated() -> None:
    original = {"headers": {"Authorization": BEARER}, "emails": [LEAD_EMAIL], "pair": ("a", 1)}
    snapshot = copy.deepcopy(original)
    result = redact(original)
    assert result == {
        "headers": {"Authorization": REDACTED},
        "emails": [REDACTED_EMAIL],
        "pair": ("a", 1),
    }
    assert original == snapshot


def test_container_types_are_preserved_or_normalised() -> None:
    redacted_tuple = redact((LEAD_EMAIL, 1))
    assert redacted_tuple == (REDACTED_EMAIL, 1)
    assert type(redacted_tuple) is tuple
    for sequence in (UserList([LEAD_EMAIL]), deque([LEAD_EMAIL]), [LEAD_EMAIL]):
        redacted_list = redact(sequence)
        assert redacted_list == [REDACTED_EMAIL]
        assert type(redacted_list) is list
    assert redact(range(3)) == [0, 1, 2]
    redacted_mapping = redact(MappingProxyType({"contact": LEAD_EMAIL}))
    assert redacted_mapping == {"contact": REDACTED_EMAIL}
    assert type(redacted_mapping) is dict


def test_sets_keep_their_kind_unless_a_member_becomes_unhashable() -> None:
    redacted_frozen = redact(frozenset({LEAD_EMAIL, "ok"}))
    assert redacted_frozen == frozenset({REDACTED_EMAIL, "ok"})
    assert type(redacted_frozen) is frozenset
    redacted_set = redact({LEAD_EMAIL, "ok"})
    assert redacted_set == {REDACTED_EMAIL, "ok"}
    assert type(redacted_set) is set
    # A range is hashable, but its redacted copy is a list, so the set becomes a list.
    assert redact({range(2)}) == [[0, 1]]


def test_containers_deeper_than_eight_levels_are_replaced() -> None:
    assert redact(_nest(LEAD_EMAIL, 8)) == _nest(REDACTED_EMAIL, 8)
    assert redact(_nest(LEAD_EMAIL, 9)) == _nest(REDACTED, 8)
    deep_mapping: object = {"leaf": 1}
    for _ in range(8):
        deep_mapping = {"child": deep_mapping}
    expected: object = REDACTED
    for _ in range(8):
        expected = {"child": expected}
    assert redact(deep_mapping) == expected


def test_mapping_keys_are_redacted_and_collisions_suffixed() -> None:
    value = {"a@example.com": 1, "b@example.com": 2, "c@example.com": 3}
    assert redact(value) == {
        "[REDACTED_EMAIL]": 1,
        "[REDACTED_EMAIL]#2": 2,
        "[REDACTED_EMAIL]#3": 3,
    }


def test_non_string_keys_are_redacted_and_collisions_use_their_repr() -> None:
    value = {("a@example.com", 1): "x", ("b@example.com", 1): "y", 7: "z"}
    assert redact(value) == {
        ("[REDACTED_EMAIL]", 1): "x",
        "('[REDACTED_EMAIL]', 1)#2": "y",
        7: "z",
    }


def test_key_whose_redacted_copy_is_unhashable_becomes_text() -> None:
    assert redact({range(3): "v"}) == {"range(0, 3)": "v"}


def test_objects_render_through_their_structlog_hook() -> None:
    hooked = _Hooked({"Authorization": BEARER, "contact": LEAD_EMAIL})
    assert redact(hooked) == {"Authorization": REDACTED, "contact": REDACTED_EMAIL}
    # At the depth limit the hook is not called; the redacted repr is used instead.
    assert redact(_nest(hooked, 8)) == _nest("<_Hooked for [REDACTED_EMAIL]>", 8)


def test_other_objects_become_their_redacted_repr() -> None:
    assert redact(_Opaque(), secrets=[N8N_SECRET]) == "<_Opaque [REDACTED_EMAIL] [REDACTED]>"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"secrets": N8N_SECRET},
        {"secrets": N8N_SECRET.encode()},
        {"header_names": "X-Hook-Auth"},
    ],
)
def test_single_string_arguments_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="must be a collection of strings, not a single string"):
        redact("value", **kwargs)


# ---------------------------------------------------------------------------
# configure_logging(): JSON lines, levels and routing
# ---------------------------------------------------------------------------


def test_events_are_written_as_one_json_object_per_line() -> None:
    stream = _configure()
    log = get_logger("tests.logging.lines")
    log.info("sync_job_finished", source="google", rows=120, duration_ms=840)
    log.warning("multi_line_event", detail="first line\nsecond line")
    log.info("with_stack", stack_info=True)

    records = _lines(stream)
    assert len(records) == 3
    first, second, third = records
    assert first["event"] == "sync_job_finished"
    assert first["source"] == "google"
    assert first["rows"] == 120
    assert first["level"] == "info"
    assert first["logger"] == "tests.logging.lines"
    timestamp = dt.datetime.fromisoformat(first["timestamp"])
    assert timestamp.utcoffset() == dt.timedelta(0)
    assert first["timestamp"].endswith("Z")
    assert second["level"] == "warning"
    assert second["detail"] == "first line\nsecond line"
    assert "\n" in third["stack"]


def test_structlog_events_are_redacted() -> None:
    settings = Settings(
        n8n_webhook_secret=SecretStr(N8N_SECRET),
        n8n_google_ads_webhook_url=WEBHOOK_URL,
        session_secret=SecretStr(SESSION_SECRET),
    )
    stream = _configure(settings)
    get_logger("tests.logging.redaction").info(
        "request_sent",
        headers={"Authorization": BEARER, "X-Webhook-Secret": N8N_SECRET, "Accept": "json"},
        raw_headers=[(b"authorization", BEARER.encode()), (b"accept", b"json")],
        hubspot_access_token="test-token-under-suffixed-key",
        detail=f"secret {N8N_SECRET} session {SESSION_SECRET} for {LEAD_EMAIL}",
        url=WEBHOOK_URL,
        path=WEBHOOK_PATH,
        database=DB_URL,
        wrapped=SecretStr(HEADER_VALUE),
    )

    output = stream.getvalue()
    for plaintext in (
        BEARER,
        N8N_SECRET,
        SESSION_SECRET,
        LEAD_EMAIL,
        WEBHOOK_URL,
        WEBHOOK_PATH,
        "test-db-password",
        "test-token-under-suffixed-key",
        HEADER_VALUE,
    ):
        assert plaintext not in output
    (record,) = _lines(stream)
    assert record["headers"] == {
        "Authorization": REDACTED,
        "X-Webhook-Secret": REDACTED,
        "Accept": "json",
    }
    assert record["raw_headers"] == [["authorization", REDACTED], ["accept", "json"]]
    assert record["hubspot_access_token"] == REDACTED
    assert record["detail"] == "secret [REDACTED] session [REDACTED] for [REDACTED_EMAIL]"
    assert record["url"] == REDACTED
    assert record["path"] == REDACTED
    assert record["database"] == DB_URL_REDACTED
    assert record["wrapped"] == REDACTED


def test_configured_secret_header_name_is_redacted() -> None:
    stream = _configure(Settings(n8n_webhook_secret_header="X-Hook-Auth"))
    get_logger("tests.logging.header").info(
        "webhook_called", headers=[("X-Hook-Auth", HEADER_VALUE)], **{"x-hook-auth": HEADER_VALUE}
    )
    (record,) = _lines(stream)
    assert record["headers"] == [["X-Hook-Auth", REDACTED]]
    assert record["x-hook-auth"] == REDACTED


def test_separator_free_names_are_redacted_in_json_output() -> None:
    stream = _configure(Settings(n8n_webhook_secret_header="X-Custom-Auth"))
    get_logger("tests.logging.separator_free").info(
        "webhook_called",
        headers=[("XCUSTOMAUTH", HEADER_VALUE), (b"PROXYAUTHORIZATION", HEADER_VALUE.encode())],
        body={"rEfReShToKeN": HEADER_VALUE, "maxtokens": 5},
        detail=f"retrying with accesstoken={HEADER_VALUE}&limit=1",
        xcustomauth=HEADER_VALUE,
        hubspotaccesstoken=HEADER_VALUE,
        tokencount=3,
    )
    assert HEADER_VALUE not in stream.getvalue()
    (record,) = _lines(stream)
    assert record["headers"] == [["XCUSTOMAUTH", REDACTED], ["PROXYAUTHORIZATION", REDACTED]]
    assert record["body"] == {"rEfReShToKeN": REDACTED, "maxtokens": 5}
    assert record["detail"] == "retrying with accesstoken=[REDACTED]&limit=1"
    assert record["xcustomauth"] == REDACTED
    assert record["hubspotaccesstoken"] == REDACTED
    assert record["tokencount"] == 3


def test_url_secret_passwords_are_redacted_raw_and_percent_decoded() -> None:
    database_url = "postgresql+psycopg://ads:test%2Fdb%40password@db:5432/ads_engine"
    stream = _configure(Settings(database_url=SecretStr(database_url)))
    log = get_logger("tests.logging.database")
    log.error("db_connect_failed", detail="password authentication failed: test/db@password")
    log.error("db_connect_failed", detail="raw test%2Fdb%40password", url=database_url)

    output = stream.getvalue()
    assert "test/db@password" not in output
    assert "test%2Fdb%40password" not in output
    first, second = _lines(stream)
    assert first["detail"] == "password authentication failed: [REDACTED]"
    assert second["detail"] == "raw [REDACTED]"
    assert second["url"] == DEFAULT_DB_URL_REDACTED


def test_default_database_url_keeps_its_readable_form() -> None:
    stream = _configure()
    get_logger("tests.logging.default_db").info("ads_engine_started", url=DEFAULT_DB_URL)
    (record,) = _lines(stream)
    # The default password `ads` is too short to be a substring rule; the URL rule covers it.
    assert record["event"] == "ads_engine_started"
    assert record["url"] == DEFAULT_DB_URL_REDACTED


def test_a_short_configured_secret_is_redacted_plain_and_percent_encoded() -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(SHORT_N8N_SECRET)))
    log = get_logger("tests.logging.short_secret")
    head = "google search Q-G1: HTTP 403: wrong secret"
    log.warning("connector.error", reason=f"{head} {SHORT_N8N_SECRET}")
    log.warning("connector.error", reason=f"{head} {quote(SHORT_N8N_SECRET, safe='')}")

    output = stream.getvalue()
    assert SHORT_N8N_SECRET not in output
    assert "Zq7%23k2p" not in output
    assert [record["reason"] for record in _lines(stream)] == [f"{head} [REDACTED]"] * 2


def test_a_short_configured_secret_never_breaks_an_encoded_credential_apart() -> None:
    # Replacing `3D` alone would log `access_token%[REDACTED]test-refreshed-token`, which no
    # longer decodes to an assignment, so the token would be written in plaintext.
    stream = _configure(Settings(n8n_webhook_secret=SecretStr("3D")))
    log = get_logger("tests.logging.short_secret")
    head = "google search Q-G1: HTTP 403:"
    log.warning("connector.error", reason=f"{head} access_token%3Dtest-refreshed-token")

    output = stream.getvalue()
    for fragment in ("test-refreshed-token", "%[REDACTED]", "3D"):
        assert fragment not in output
    (record,) = _lines(stream)
    assert record["reason"] == f"{head} [REDACTED]"


def test_short_url_passwords_are_not_substring_rules_but_short_secrets_are() -> None:
    webhook_url = "https://n8n.example.test/w"
    settings = Settings(
        n8n_webhook_secret=SecretStr(SHORT_N8N_SECRET), n8n_google_ads_webhook_url=webhook_url
    )
    secrets = ads_logging._settings_secrets(settings)
    assert {SHORT_N8N_SECRET, webhook_url, "/w", DEFAULT_DB_URL} <= set(secrets)
    assert "ads" not in secrets

    stream = _configure(settings)
    get_logger("ads_engine.sync").info("ads_engine_started", url=DEFAULT_DB_URL, note="ads up")
    (record,) = _lines(stream)
    assert record["logger"] == "ads_engine.sync"
    assert record["event"] == "ads_engine_started"
    assert record["url"] == DEFAULT_DB_URL_REDACTED
    assert record["note"] == "ads up"


def test_secret_that_is_not_a_parseable_url_is_still_redacted() -> None:
    unparseable = "http://[test-session-secret"
    stream = _configure(Settings(session_secret=SecretStr(unparseable)))
    get_logger("tests.logging.unparseable").info("session", detail=f"key {unparseable}")
    (record,) = _lines(stream)
    assert record["detail"] == "key [REDACTED]"


def test_webhook_url_without_a_path_redacts_only_the_url() -> None:
    stream = _configure(Settings(n8n_google_ads_webhook_url="https://n8n.example.test/"))
    get_logger("tests.logging.webhook").info("call", detail="POST https://n8n.example.test/ /")
    (record,) = _lines(stream)
    assert record["detail"] == "POST [REDACTED] /"


def test_unsplittable_webhook_url_is_redacted_whole() -> None:
    # Validation rejects such a URL; model_construct exercises the defensive fallback.
    unsplittable = "https://[n8n.example.test/webhook/test-webhook-path"
    settings = Settings.model_construct(n8n_google_ads_webhook_url=unsplittable)
    stream = _configure(settings)
    get_logger("tests.logging.webhook").info("call", detail=f"to {unsplittable}")
    (record,) = _lines(stream)
    assert record["detail"] == "to [REDACTED]"


def test_level_names_are_case_and_space_insensitive() -> None:
    stream = _configure(level=" warning ")
    log = get_logger("tests.logging.level")
    log.info("dropped_info")
    log.warning("kept_warning")
    logging.getLogger("tests.third_party").info("dropped stdlib info")
    logging.getLogger("tests.third_party").error("kept stdlib error")

    records = _lines(stream)
    assert [record["event"] for record in records] == ["kept_warning", "kept stdlib error"]
    assert logging.getLogger().level == logging.WARNING


def test_debug_level_keeps_noisy_libraries_at_warning() -> None:
    stream = _configure(level="debug")
    get_logger("tests.logging.debug").debug("kept_debug")
    for name in ("httpx", "httpcore", "sqlalchemy.engine"):
        assert logging.getLogger(name).level == logging.WARNING
    logging.getLogger("httpx").info("HTTP Request: GET https://api.example.test/x")
    logging.getLogger("httpx").warning("kept httpx warning")

    records = _lines(stream)
    assert [record["event"] for record in records] == ["kept_debug", "kept httpx warning"]
    assert records[0]["level"] == "debug"


@pytest.mark.parametrize("level", ["VERBOSE", "NOTSET", "", "  "])
def test_unknown_level_is_rejected_before_any_change(level: str) -> None:
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    redactor_before = ads_logging._STATE.redactor
    stream = io.StringIO()
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging(stream=stream, level=level, settings=Settings())
    assert root.handlers == handlers_before
    assert ads_logging._STATE.redactor is redactor_before
    assert stream.getvalue() == ""


def test_standard_library_records_are_rendered_and_redacted() -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    logging.getLogger("tests.third_party").warning(
        "token %s rejected for %s", N8N_SECRET, LEAD_EMAIL, extra={"authorization": BEARER}
    )

    assert BEARER not in stream.getvalue()
    (record,) = _lines(stream)
    assert record["event"] == "token [REDACTED] rejected for [REDACTED_EMAIL]"
    assert record["level"] == "warning"
    assert record["logger"] == "tests.third_party"
    assert "timestamp" in record


def test_uvicorn_loggers_are_routed_through_redaction() -> None:
    private = io.StringIO()
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler(private))
    access.propagate = False

    stream = _configure()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    access.info('%s - "%s" %d', "127.0.0.1", f"GET /actions?email={LEAD_EMAIL} HTTP/1.1", 200)

    assert private.getvalue() == ""
    (record,) = _lines(stream)
    assert record["event"] == '127.0.0.1 - "GET /actions?email=[REDACTED_EMAIL] HTTP/1.1" 200'
    assert record["logger"] == "uvicorn.access"


@dataclasses.dataclass(frozen=True)
class _FrozenError(Exception):
    """An exception whose ``__setattr__`` refuses every assignment."""

    code: int


@dataclasses.dataclass(frozen=True, slots=True)
class _FrozenSlotsError(Exception):
    """A frozen dataclass exception with slots: its instance dict comes from BaseException."""

    code: int


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), _FrozenError(7), _FrozenSlotsError(8), KeyboardInterrupt()],
    ids=["plain", "frozen-dataclass", "frozen-slots-dataclass", "base-exception"],
)
def test_mark_logged_records_the_owning_event(error: BaseException) -> None:
    assert logged_as(error) is None
    mark_logged(error, "analysis_run_failed")
    assert logged_as(error) == "analysis_run_failed"
    # A later owner replaces the event; other exceptions stay unmarked.
    mark_logged(error, "api.unhandled_error")
    assert logged_as(error) == "api.unhandled_error"
    assert logged_as(RuntimeError("boom")) is None


def test_mark_logged_needs_an_event_name() -> None:
    error = RuntimeError("boom")
    with pytest.raises(ValueError, match="event must name the log event"):
        mark_logged(error, "")
    assert logged_as(error) is None


def test_logged_as_ignores_a_marker_that_is_not_an_event_name() -> None:
    error = RuntimeError("boom")
    vars(error)[ads_logging._LOGGED_AS_KEY] = 3
    assert logged_as(error) is None


def _raised(message: str) -> RuntimeError:
    """Return a ``RuntimeError`` that was raised, so it carries a traceback."""
    try:
        raise RuntimeError(message)
    except RuntimeError as caught:
        return caught


def test_uvicorn_error_drops_a_record_of_an_exception_already_logged() -> None:
    stream = _configure()
    uvicorn_error = logging.getLogger("uvicorn.error")
    owned = _raised("logged by its owner")
    mark_logged(owned, "api.unhandled_error")
    unowned = _raised("logged nowhere else")

    uvicorn_error.error("Exception in ASGI application\n", exc_info=owned)
    uvicorn_error.error("Exception in ASGI application\n", exc_info=unowned)
    uvicorn_error.info("Application startup complete.")

    records = _lines(stream)
    assert [record["event"] for record in records] == [
        "Exception in ASGI application\n",
        "Application startup complete.",
    ]
    (exception,) = records[0]["exception"]
    assert exception["exc_value"] == "logged nowhere else"
    # Reconfiguring attaches the one filter instance once.
    _configure()
    assert uvicorn_error.filters.count(ads_logging._OWNED_EXCEPTION_FILTER) == 1


def test_owned_exception_filter_passes_records_without_a_real_exception() -> None:
    owned_filter = ads_logging._OWNED_EXCEPTION_FILTER
    plain = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "msg", None, None)
    assert owned_filter.filter(plain) is True
    cleared = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "msg", None, (None, None, None)
    )
    assert owned_filter.filter(cleared) is True
    odd = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "msg", None, None)
    odd.exc_info = cast(Any, ("not", "an", "exc_info", "tuple"))
    assert owned_filter.filter(odd) is True


def _raise_with_secret_local() -> None:
    """Raise while a local variable holds a value no redaction rule knows."""
    unlisted_local = "test-unlisted-local-value"
    raise RuntimeError(f"failed with {N8N_SECRET} for {LEAD_EMAIL} ({len(unlisted_local)})")


def test_structlog_exceptions_render_without_locals() -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    log = get_logger("tests.logging.exception")
    try:
        _raise_with_secret_local()
    except RuntimeError:
        log.exception("call_failed")

    output = stream.getvalue()
    for plaintext in ("test-unlisted-local-value", N8N_SECRET, LEAD_EMAIL):
        assert plaintext not in output
    (record,) = _lines(stream)
    assert record["level"] == "error"
    (exception,) = record["exception"]
    assert exception["exc_type"] == "RuntimeError"
    assert exception["exc_value"] == "failed with [REDACTED] for [REDACTED_EMAIL] (25)"
    assert exception["frames"]
    for frame in exception["frames"]:
        assert "locals" not in frame
    assert exception["frames"][-1]["name"] == "_raise_with_secret_local"


def test_standard_library_exceptions_are_rendered_and_redacted() -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    try:
        _raise_with_secret_local()
    except RuntimeError:
        logging.getLogger("tests.third_party").exception("library call failed")

    output = stream.getvalue()
    for plaintext in ("test-unlisted-local-value", N8N_SECRET, LEAD_EMAIL):
        assert plaintext not in output
    (record,) = _lines(stream)
    assert record["event"] == "library call failed"
    (exception,) = record["exception"]
    assert exception["exc_value"] == "failed with [REDACTED] for [REDACTED_EMAIL] (25)"
    for frame in exception["frames"]:
        assert "locals" not in frame


def test_exception_arguments_are_redacted_as_data() -> None:
    stream = _configure()
    log = get_logger("tests.logging.exception_args")
    errors = (
        RuntimeError({"Authorization": "Bearer test-rotated-access", "refresh_token": "test-r"}),
        RuntimeError({"refresh_token": {"v": "test-nested-value"}}),
        RuntimeError("Authorization", "Bearer test-pair-value"),
        RuntimeError(),
    )
    for error in errors:
        try:
            raise error
        except RuntimeError:
            log.exception("call_failed")

    output = stream.getvalue()
    for plaintext in ("test-rotated-access", "test-r'", "test-nested-value", "test-pair-value"):
        assert plaintext not in output
    assert [record["exception"][0]["exc_value"] for record in _lines(stream)] == [
        "{'Authorization': '[REDACTED]', 'refresh_token': '[REDACTED]'}",
        "{'refresh_token': '[REDACTED]'}",
        "('Authorization', '[REDACTED]')",
        "",
    ]


def test_chained_and_grouped_exception_arguments_are_redacted() -> None:
    stream = _configure()
    try:
        try:
            raise KeyError({"token": "test-inner-token"})
        except KeyError as inner:
            members = [RuntimeError({"access_token": "test-member-token"})]
            raise ExceptionGroup("batch failed", members) from inner
    except ExceptionGroup:
        logging.getLogger("tests.third_party").exception("batch")

    output = stream.getvalue()
    assert "test-inner-token" not in output
    assert "test-member-token" not in output
    (record,) = _lines(stream)
    group, cause = record["exception"]
    assert group["exc_type"] == "ExceptionGroup"
    assert group["exceptions"][0][0]["exc_value"] == "{'access_token': '[REDACTED]'}"
    assert cause["exc_type"] == "KeyError"
    assert cause["exc_value"] == "{'token': '[REDACTED]'}"


class _KeyLookupError(KeyError):
    """A user-defined ``KeyError`` that keeps the built-in ``KeyError.__str__``."""


class _CustomStrError(Exception):
    """An exception with a ``__str__`` of its own, left to the text rules."""

    def __str__(self) -> str:
        return f"custom {self.args[0]!r}"


def _unicode_error_with_replaced_args() -> UnicodeDecodeError:
    """A ``UnicodeDecodeError`` whose arguments its constructor would reject."""
    error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    error.args = ({"token": {"v": ROTATED}},)
    return error


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            KeyError({"access_token": {"value": ROTATED}}),
            "{'access_token': '[REDACTED]'}",
            id="key-error-nested-mapping",
        ),
        pytest.param(KeyError("campaign-123"), "'campaign-123'", id="key-error-plain-string"),
        pytest.param(
            KeyError("token", ROTATED.encode()), "('token', '[REDACTED]')", id="key-error-pair"
        ),
        pytest.param(
            _KeyLookupError({"refresh_token": [ROTATED]}),
            "{'refresh_token': '[REDACTED]'}",
            id="key-error-subclass",
        ),
        pytest.param(
            OSError(13, f"denied access_token={ROTATED}"),
            "[Errno 13] denied access_token=[REDACTED]",
            id="os-error-strerror",
        ),
        pytest.param(
            FileNotFoundError(2, "No such file or directory", f"/data/token={ROTATED}"),
            "[Errno 2] No such file or directory: '/data/token=[REDACTED]'",
            id="os-error-filename",
        ),
        pytest.param(
            OSError({"token": {"v": ROTATED}}), "{'token': '[REDACTED]'}", id="os-error-mapping"
        ),
        pytest.param(
            ExceptionGroup("batch failed", [ValueError("member")]),
            "batch failed (1 sub-exception)",
            id="exception-group",
        ),
        pytest.param(
            UnicodeDecodeError("utf-8", b"\xff to " + LEAD_EMAIL.encode(), 0, 1, "invalid"),
            "'utf-8' codec can't decode byte 0xff in position 0: invalid",
            id="unicode-decode-error",
        ),
        pytest.param(
            _unicode_error_with_replaced_args(),
            "{'token': '[REDACTED]'}",
            id="unbuildable-clone-falls-back-to-redacted-arguments",
        ),
        pytest.param(
            _CustomStrError({"token": {"v": ROTATED}}),
            "custom {'token': [REDACTED]}",
            id="custom-str-uses-text-rules",
        ),
    ],
)
def test_builtin_exception_text_is_rendered_from_redacted_arguments(
    error: BaseException, expected: str
) -> None:
    stream = _configure()
    try:
        raise error
    except KeyError, OSError, ExceptionGroup, UnicodeDecodeError, _CustomStrError:
        get_logger("tests.logging.builtin_str").exception("call_failed")

    output = stream.getvalue()
    assert ROTATED not in output
    assert LEAD_EMAIL not in output
    (record,) = _lines(stream)
    assert record["exception"][0]["exc_value"] == expected


def test_exception_values_are_patched_only_when_the_walk_lines_up() -> None:
    aligned = ads_logging._aligned_stacks
    error = RuntimeError({"token": "test-t"})
    assert aligned("not a list", RuntimeError, error, set()) is None
    assert aligned([], RuntimeError, error, set()) is None
    assert aligned([{"exc_type": "ValueError"}], RuntimeError, error, set()) is None
    two = [{"exc_type": "RuntimeError"}, {"exc_type": "RuntimeError"}]
    assert aligned(two, RuntimeError, error, set()) is None
    group = ExceptionGroup("g", [ValueError("v")])
    assert aligned([{"exc_type": "ExceptionGroup"}], ExceptionGroup, group, set()) is None
    stack = {"exc_type": "RuntimeError"}
    assert aligned([stack], RuntimeError, error, set()) == [(stack, error)]


def test_redact_processor_renders_and_redacts_exc_info() -> None:
    _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    try:
        raise RuntimeError(f"rejected {N8N_SECRET}")
    except RuntimeError as exc:
        event: dict[str, Any] = {"event": "call_failed", "exc_info": exc}

    result = redact_processor(logging.getLogger("tests.processor"), "error", event)
    assert "exc_info" not in result
    assert result["exception"][0]["exc_value"] == "rejected [REDACTED]"
    assert result["event"] == "call_failed"
    assert "exc_info" in event


def test_redact_processor_uses_the_latest_configuration() -> None:
    logger = logging.getLogger("tests.processor")
    event: dict[Any, Any] = {"event": f"uses {N8N_SECRET}", 7: LEAD_EMAIL}
    _configure()
    assert redact_processor(logger, "info", event) == {
        "event": f"uses {N8N_SECRET}",
        "7": REDACTED_EMAIL,
    }
    _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    assert redact_processor(logger, "info", event) == {
        "event": "uses [REDACTED]",
        "7": REDACTED_EMAIL,
    }
    assert event == {"event": f"uses {N8N_SECRET}", 7: LEAD_EMAIL}


def test_reconfiguring_replaces_the_handler_and_applies_to_cached_loggers() -> None:
    log = get_logger("tests.logging.cached")
    first = _configure()
    log.info("first_event", detail=N8N_SECRET)
    second = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    log.info("second_event", detail=N8N_SECRET)

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert cast(logging.StreamHandler[io.StringIO], root.handlers[0]).stream is second
    assert [record["event"] for record in _lines(first)] == ["first_event"]
    (record,) = _lines(second)
    assert record["event"] == "second_event"
    assert record["detail"] == REDACTED


def test_reconfiguring_keeps_the_attached_handler_and_swaps_its_output() -> None:
    first = _configure()
    (handler,) = logging.getLogger().handlers
    second = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    assert logging.getLogger().handlers == [handler]
    assert ads_logging._STATE.handler is handler
    logging.getLogger("tests.third_party").warning("uses %s", N8N_SECRET)
    assert first.getvalue() == ""
    (record,) = _lines(second)
    assert record["event"] == "uses [REDACTED]"


def test_a_new_handler_is_attached_before_any_other_is_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    root.addHandler(foreign)
    counts: list[int] = []
    original = root.removeHandler

    def remove_and_count(handler: logging.Handler) -> None:
        original(handler)
        counts.append(len(root.handlers))

    monkeypatch.setattr(root, "removeHandler", remove_and_count)
    stream = _configure()
    assert counts
    assert min(counts) >= 1
    assert root.handlers == [ads_logging._STATE.handler]
    assert foreign not in root.handlers
    logging.getLogger("tests.third_party").warning("kept")
    assert [record["event"] for record in _lines(stream)] == ["kept"]


def test_a_detached_previous_handler_is_closed_after_the_new_one_is_attached() -> None:
    _configure()
    previous = ads_logging._STATE.handler
    assert previous is not None
    logging.getLogger().removeHandler(previous)
    stream = _configure()
    assert ads_logging._STATE.handler is not previous
    assert logging.getLogger().handlers == [ads_logging._STATE.handler]
    logging.getLogger("tests.third_party").warning("after")
    assert [record["event"] for record in _lines(stream)] == ["after"]


def test_uvicorn_loggers_propagate_before_their_handlers_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler(io.StringIO()))
    access.propagate = False
    seen: list[bool] = []
    original = access.removeHandler

    def remove_and_record(handler: logging.Handler) -> None:
        seen.append(access.propagate)
        original(handler)

    monkeypatch.setattr(access, "removeHandler", remove_and_record)
    _configure()
    assert seen == [True]
    assert access.handlers == []


def test_concurrent_reconfiguration_never_leaks_a_raw_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    streams = [_configure()]
    stop = threading.Event()
    handler_counts: set[int] = set()

    def emit() -> None:
        foreign = logging.getLogger("tests.race")
        native = get_logger("tests.race.native")
        while not stop.is_set():
            foreign.warning("Authorization: Bearer %s for %s", RAW_TOKEN, LEAD_EMAIL)
            native.warning("race", access_token=RAW_TOKEN, detail=f"Bearer {RAW_TOKEN}")
            time.sleep(0)

    def sample() -> None:
        while not stop.is_set():
            handler_counts.add(len(logging.getLogger().handlers))
            time.sleep(0)

    threads = [threading.Thread(target=emit) for _ in range(3)]
    threads.append(threading.Thread(target=sample))
    for thread in threads:
        thread.start()
    try:
        for _ in range(150):
            streams.append(_configure())
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    captured = capsys.readouterr()
    assert RAW_TOKEN not in captured.err
    assert LEAD_EMAIL not in captured.err
    assert handler_counts == {1}
    written = 0
    for stream in streams:
        for line in stream.getvalue().splitlines():
            assert RAW_TOKEN not in line
            assert LEAD_EMAIL not in line
            assert isinstance(_strict_json(line), dict)
            written += 1
    assert written > 0


def test_last_resort_renders_redacted_json_on_the_current_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    last_resort = logging.lastResort
    assert isinstance(last_resort, ads_logging._SafeLastResortHandler)
    assert last_resort.level == logging.WARNING
    orphan = logging.getLogger("tests.orphan")
    monkeypatch.setattr(orphan, "propagate", False)
    orphan.warning("Authorization: Bearer %s for %s", RAW_TOKEN, LEAD_EMAIL)
    orphan.info("below the last-resort level")
    record = logging.LogRecord(
        "tests.orphan", logging.ERROR, __file__, 1, "token=%s", (RAW_TOKEN,), None
    )
    last_resort.handle(record)

    err = capsys.readouterr().err
    assert RAW_TOKEN not in err
    assert LEAD_EMAIL not in err
    first, second = (_strict_json(line) for line in err.splitlines())
    assert isinstance(first, dict)
    assert first["event"] == "Authorization: [REDACTED]"
    assert first["logger"] == "tests.orphan"
    assert isinstance(second, dict)
    assert second["event"] == "token=[REDACTED]"


def test_last_resort_reports_a_failed_write_without_the_record(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    last_resort = logging.lastResort
    assert last_resort is not None
    real_stderr = sys.stderr
    monkeypatch.setattr("sys.stderr", _FailingStream())
    record = logging.LogRecord("tests.orphan", logging.ERROR, __file__, 1, RAW_TOKEN, (), None)
    last_resort.handle(record)
    monkeypatch.setattr("sys.stderr", real_stderr)
    assert RAW_TOKEN not in capsys.readouterr().err


def test_native_records_reach_other_handlers_already_redacted() -> None:
    seen: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    stream = _configure()
    child = logging.getLogger("tests.logging.child")
    capture = _Capture()
    child.addHandler(capture)
    try:
        get_logger("tests.logging.child").warning(
            "native", access_token=RAW_TOKEN, contact=LEAD_EMAIL, headers=[("Cookie", "s=1")]
        )
    finally:
        child.removeHandler(capture)

    (record,) = seen
    message = record.getMessage()
    assert RAW_TOKEN not in message
    assert LEAD_EMAIL not in message
    assert record.msg == {
        "access_token": REDACTED,
        "contact": REDACTED_EMAIL,
        "headers": [("Cookie", REDACTED)],
        "event": "native",
        "level": "warning",
        "logger": "tests.logging.child",
        "timestamp": cast(dict[str, Any], record.msg)["timestamp"],
    }
    (line,) = _lines(stream)
    assert line["access_token"] == REDACTED
    assert line["headers"] == [["Cookie", REDACTED]]


def test_default_stream_is_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(settings=Settings())
    get_logger("tests.logging.stdout").info("to_stdout", contact=LEAD_EMAIL)

    captured = capsys.readouterr()
    (line,) = captured.out.splitlines()
    record = json.loads(line)
    assert record["event"] == "to_stdout"
    assert record["contact"] == REDACTED_EMAIL
    assert captured.err == ""


def test_context_variables_are_merged_and_redacted() -> None:
    stream = _configure()
    structlog.contextvars.bind_contextvars(
        request_id="req-1", session_secret=SESSION_SECRET, contact=LEAD_EMAIL
    )
    get_logger("tests.logging.context").info("login_failed")

    (record,) = _lines(stream)
    assert record["request_id"] == "req-1"
    assert record["session_secret"] == REDACTED
    assert record["contact"] == REDACTED_EMAIL


def test_event_keys_that_collide_after_redaction_are_suffixed() -> None:
    stream = _configure()
    get_logger("tests.logging.collision").info("seen", **{"a@example.com": 1, "b@example.com": 2})

    (record,) = _lines(stream)
    assert record["[REDACTED_EMAIL]"] == 1
    assert record["[REDACTED_EMAIL]#2"] == 2


def test_values_json_cannot_encode_render_through_the_json_default() -> None:
    stream = _configure()
    get_logger("tests.logging.values").info(
        "values",
        amount=Decimal("12.50"),
        run_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        day=dt.date(2026, 10, 1),
        at=dt.datetime(2026, 10, 1, 9, 30, tzinfo=dt.UTC),
        slot=dt.time(9, 30),
        wait=dt.timedelta(minutes=1, seconds=30),
        tags={"beta", "alpha"},
        frozen=frozenset({2, 1}),
        opaque=_Opaque(),
    )

    (record,) = _lines(stream)
    assert record["amount"] == "12.50"
    assert record["run_id"] == "12345678-1234-5678-1234-567812345678"
    assert record["day"] == "2026-10-01"
    assert record["at"] == "2026-10-01T09:30:00+00:00"
    assert record["slot"] == "09:30:00"
    assert record["wait"] == 90.0
    assert record["tags"] == ["alpha", "beta"]
    assert record["frozen"] == [1, 2]
    assert record["opaque"] == f"<_Opaque [REDACTED_EMAIL] {N8N_SECRET}>"


def _strict_json(line: str) -> object:
    """Parse ``line`` as strict JSON, rejecting the NaN and Infinity extensions."""

    def reject(constant: str) -> object:
        raise ValueError(f"non-standard JSON constant {constant}")

    return json.loads(line, parse_constant=reject)


class _Float(float):
    """A float subclass, as numpy's ``float64`` is."""


def test_non_finite_numbers_are_logged_as_null() -> None:
    stream = _configure()
    get_logger("tests.logging.numbers").info(
        "numbers",
        sample=float("nan"),
        exposure=float("inf"),
        nested={"a": [float("-inf"), 1.5], "b": (_Float("nan"),)},
        subclass=_Float("inf"),
        amount=Decimal("NaN"),
        finite=2.5,
    )

    (line,) = stream.getvalue().splitlines()
    record = _strict_json(line)
    assert isinstance(record, dict)
    assert record["sample"] is None
    assert record["exposure"] is None
    assert record["nested"] == {"a": [None, 1.5], "b": [None]}
    assert record["subclass"] is None
    assert record["amount"] == "NaN"
    assert record["finite"] == 2.5
    assert redact([float("nan"), {"x": _Float("-inf")}]) == [None, {"x": None}]


def test_json_default_fallbacks() -> None:
    json_default = ads_logging._json_default
    assert json_default(Decimal("0.1")) == "0.1"
    assert json_default(uuid.UUID(int=1)) == "00000000-0000-0000-0000-000000000001"
    assert json_default(dt.date(2026, 1, 2)) == "2026-01-02"
    assert json_default(dt.timedelta(milliseconds=1500)) == 1.5
    assert json_default({3, 1, 2}) == [1, 2, 3]
    assert json_default(_Hooked("hooked-form")) == "hooked-form"
    assert json_default(_Opaque()) == f"<_Opaque {LEAD_EMAIL} {N8N_SECRET}>"


# ---------------------------------------------------------------------------
# configure_logging(): failures fail closed
# ---------------------------------------------------------------------------

RAW_TOKEN = "test-raw-token-value"


class _BadHook:
    """An object whose ``__structlog__`` hook raises with the token in the error text."""

    def __structlog__(self) -> object:
        raise RuntimeError(f"hook failed with {RAW_TOKEN}")

    def __repr__(self) -> str:
        return "<_BadHook>"


class _BadRepr:
    """An object whose ``repr`` raises with the token in the error text."""

    def __repr__(self) -> str:
        raise ValueError(f"repr failed with {RAW_TOKEN}")


class _BadMapping(Mapping[str, str]):
    """A mapping whose iteration raises."""

    def __getitem__(self, key: str) -> str:
        return RAW_TOKEN

    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError(f"iteration failed with {RAW_TOKEN}")


class _BadLengthList(list[object]):
    """A list whose ``len`` raises, so header-pair detection fails on it."""

    def __len__(self) -> int:
        raise RuntimeError(f"len failed with {RAW_TOKEN}")


class _BadStr(str):
    """A str subclass whose methods raise; redaction reads its plain characters."""

    __slots__ = ()

    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError(f"strip failed with {RAW_TOKEN}")

    def __contains__(self, key: object) -> bool:
        raise RuntimeError(f"contains failed with {RAW_TOKEN}")


class _BadNumber(numbers.Number):
    """A number that passes redaction unchanged and that JSON rendering cannot print."""

    __hash__ = object.__hash__

    def __repr__(self) -> str:
        raise ValueError(f"repr failed with {RAW_TOKEN}")


class _BadStrError(Exception):
    """An exception whose ``__str__`` raises with the token in the error text."""

    def __str__(self) -> str:
        raise RuntimeError(f"str failed with {RAW_TOKEN}")


class _BadNotesError(Exception):
    """An exception whose notes cannot be read or written, so traceback rendering raises."""

    @property
    def __notes__(self) -> list[str]:
        raise RuntimeError(f"notes failed with {RAW_TOKEN}")

    @__notes__.setter
    def __notes__(self, notes: list[str]) -> None:
        raise RuntimeError(f"notes failed with {RAW_TOKEN}")


def _assert_clean(stream: io.StringIO, capsys: pytest.CaptureFixture[str]) -> list[Any]:
    """Assert no output carries the raw token and every line is strict JSON; return them."""
    captured = capsys.readouterr()
    for text in (stream.getvalue(), captured.out, captured.err):
        assert RAW_TOKEN not in text
    assert captured.err == ""
    return [_strict_json(line) for line in stream.getvalue().splitlines()]


def test_values_that_cannot_be_rendered_fail_closed(capsys: pytest.CaptureFixture[str]) -> None:
    stream = _configure()
    get_logger("tests.logging.unrenderable").warning(
        "bad_values",
        access_token=RAW_TOKEN,
        hook=_BadHook(),
        shown=_BadRepr(),
        mapping=_BadMapping(),
        nested=[_BadLengthList([1, 2]), "kept", {_BadRepr(): 1}],
        text=_BadStr(f"contact {LEAD_EMAIL}"),
        **{_BadStr("Authorization"): RAW_TOKEN},
    )

    (record,) = _assert_clean(stream, capsys)
    assert record["access_token"] == REDACTED
    assert record["hook"] == "[UNRENDERABLE]"
    assert record["shown"] == "[UNRENDERABLE]"
    assert record["mapping"] == "[UNRENDERABLE]"
    assert record["nested"] == ["[UNRENDERABLE]", "kept", {"[UNRENDERABLE]": 1}]
    assert record["text"] == "contact [REDACTED_EMAIL]"
    assert record["Authorization"] == REDACTED
    assert record["event"] == "bad_values"


def test_unrenderable_values_are_marked_by_redact() -> None:
    assert redact({"a": _BadHook(), "b": [_BadRepr(), 1]}) == {
        "a": "[UNRENDERABLE]",
        "b": ["[UNRENDERABLE]", 1],
    }
    event: dict[Any, Any] = {_BadRepr(): 1, "event": "e"}
    assert ads_logging._STATE.redactor.event(event) == {"[UNRENDERABLE]": 1, "event": "e"}


def test_exceptions_that_cannot_be_rendered_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _configure()
    log = get_logger("tests.logging.bad_exception")
    for error_type in (_BadStrError, _BadNotesError):
        try:
            raise error_type(RAW_TOKEN)
        except Exception:
            log.exception("call_failed", access_token=RAW_TOKEN)

    first, second = _assert_clean(stream, capsys)
    assert first["exception"][0]["exc_type"] == "_BadStrError"
    assert first["exception"][0]["exc_value"] == "[UNRENDERABLE]"
    assert first["exception"][0]["frames"]
    assert second["exception"] == [{"exc_type": "_BadNotesError", "exc_value": "[UNRENDERABLE]"}]
    assert second["access_token"] == REDACTED


def test_values_json_cannot_encode_fall_back_to_a_minimal_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _configure()
    get_logger("tests.logging.json").error("bad_json", value=_BadNumber(), token=RAW_TOKEN)

    (record,) = _assert_clean(stream, capsys)
    assert set(record) == {"event", "level", "logger", "timestamp", "error_type"}
    assert record["event"] == "log_record_unrenderable"
    assert record["level"] == "error"
    assert record["logger"] == "tests.logging.json"
    assert record["error_type"] == "ValueError"
    assert record["timestamp"].endswith("Z")
    assert dt.datetime.fromisoformat(record["timestamp"]).utcoffset() == dt.timedelta(0)


def test_foreign_records_with_bad_arguments_fall_back_to_a_minimal_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _configure()
    logging.getLogger("tests.third_party").warning("token %s %d", RAW_TOKEN, "not-a-number")
    logging.getLogger(f"odd name {RAW_TOKEN}").error("%d", "x")

    first, second = _assert_clean(stream, capsys)
    assert first["event"] == "log_record_unrenderable"
    assert first["level"] == "warning"
    assert first["logger"] == "tests.third_party"
    assert first["error_type"] == "TypeError"
    assert second["logger"] == "unknown"
    assert second["level"] == "error"


def test_formatter_failures_including_recursion_never_escape() -> None:
    def overflow(logger: object, method_name: str, event_dict: object) -> str:
        raise RecursionError(RAW_TOKEN)

    formatter = ads_logging._SafeProcessorFormatter(processors=[overflow])
    record = logging.LogRecord("tests.formatter", logging.INFO, __file__, 1, RAW_TOKEN, (), None)
    line = formatter.format(record)
    assert RAW_TOKEN not in line
    parsed = _strict_json(line)
    assert isinstance(parsed, dict)
    assert parsed["error_type"] == "RecursionError"
    assert parsed["level"] == "info"


class _FailingStream(io.StringIO):
    """A stream whose writes fail, as a closed pipe's do."""

    def write(self, text: str) -> int:
        raise OSError(f"write failed for {RAW_TOKEN}")


def test_stream_write_failures_report_only_a_minimal_diagnostic(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_logging(stream=_FailingStream(), settings=Settings())
    get_logger("tests.logging.write").warning("lost", access_token=RAW_TOKEN, detail=RAW_TOKEN)
    logging.getLogger("tests.third_party").error("lost %s", RAW_TOKEN)

    captured = capsys.readouterr()
    assert RAW_TOKEN not in captured.err
    assert "Traceback" not in captured.err
    lines = [_strict_json(line) for line in captured.err.splitlines()]
    assert lines == [{"event": "log_handler_error", "error_type": "OSError"}] * 2

    monkeypatch.setattr(logging, "raiseExceptions", False)
    get_logger("tests.logging.write").warning("lost_quietly", access_token=RAW_TOKEN)
    assert capsys.readouterr().err == ""


def test_handler_error_report_survives_a_broken_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_logging(stream=_FailingStream(), settings=Settings())
    broken = _FailingStream()
    monkeypatch.setattr("sys.stderr", broken)
    get_logger("tests.logging.write").warning("lost", access_token=RAW_TOKEN)
    monkeypatch.setattr("sys.stderr", None)
    get_logger("tests.logging.write").warning("lost", access_token=RAW_TOKEN)
    assert broken.getvalue() == ""


# The names a fallback line keeps (logger, level, error class) pass the configured redaction.
# KNOWN_PLAINTEXT is configured as the n8n webhook secret; no name here pairs a credential
# word with a literal, so the generic secret-scan rule has nothing to flag.
KNOWN_PLAINTEXT = "DummyKnownSecret123"


class _PlaintextNamedError(Exception):
    """An exception whose class name is renamed to the configured secret below."""


_PlaintextNamedError.__name__ = KNOWN_PLAINTEXT


class _PlaintextNamedNotesError(_BadNotesError):
    """An exception named after the secret whose traceback cannot be rendered."""


_PlaintextNamedNotesError.__name__ = KNOWN_PLAINTEXT


class _PlaintextNamedStrHook:
    """A ``%s`` argument whose ``__str__`` hook raises the secret-named exception."""

    def __str__(self) -> str:
        raise _PlaintextNamedError(RAW_TOKEN)


class _PlaintextNamedFailingStream(io.StringIO):
    """A stream whose writes raise the secret-named exception."""

    def write(self, text: str) -> int:
        raise _PlaintextNamedError(RAW_TOKEN)


def _configure_fallback_secret(secret: str = KNOWN_PLAINTEXT) -> io.StringIO:
    """Configure logging into a fresh buffer with ``secret`` as the n8n webhook secret."""
    return _configure(Settings(n8n_webhook_secret=SecretStr(secret)))


def _assert_no_fallback_secret(
    stream: io.StringIO, capsys: pytest.CaptureFixture[str], secret: str = KNOWN_PLAINTEXT
) -> list[Any]:
    """Assert no output holds the secret in any letter case, the token or the address.

    Returns:
        The strict-JSON lines of ``stream`` followed by those of the captured stderr.
    """
    captured = capsys.readouterr()
    for text in (stream.getvalue(), captured.out, captured.err):
        assert secret.casefold() not in text.casefold()
        assert RAW_TOKEN not in text
        assert LEAD_EMAIL not in text
    assert captured.out == ""
    lines = (*stream.getvalue().splitlines(), *captured.err.splitlines())
    return [_strict_json(line) for line in lines]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (KNOWN_PLAINTEXT, "unknown"),
        (f"probe.{KNOWN_PLAINTEXT}", "unknown"),
        (LEAD_EMAIL, "unknown"),
        ("ads_engine.sync", "ads_engine.sync"),
    ],
)
def test_fallback_line_keeps_a_logger_name_only_when_redaction_leaves_it_unchanged(
    name: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _configure_fallback_secret()
    logging.getLogger(name).warning("%d", RAW_TOKEN)

    (record,) = _assert_no_fallback_secret(stream, capsys)
    assert set(record) == {"event", "level", "logger", "timestamp", "error_type"}
    assert record["event"] == "log_record_unrenderable"
    assert record["logger"] == expected
    assert record["level"] == "warning"
    assert record["error_type"] == "TypeError"


def test_fallback_line_drops_an_error_class_named_after_a_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _configure_fallback_secret()
    logging.getLogger("tests.fallback.hook").warning("%s", _PlaintextNamedStrHook())

    (record,) = _assert_no_fallback_secret(stream, capsys)
    assert record["event"] == "log_record_unrenderable"
    assert record["logger"] == "tests.fallback.hook"
    assert record["error_type"] == "unknown"


@pytest.mark.parametrize(
    ("secret", "level_name"),
    [
        (KNOWN_PLAINTEXT, KNOWN_PLAINTEXT),
        # Written lower-cased, an upper-cased level name would spell a lower-case secret.
        (KNOWN_PLAINTEXT.lower(), KNOWN_PLAINTEXT.upper()),
    ],
)
def test_fallback_line_drops_a_level_name_the_redaction_would_change(
    secret: str,
    level_name: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # addLevelName writes these module-level maps; copies keep its change to this test, and
    # monkeypatch restores the originals however the test ends.
    monkeypatch.setattr(logging, "_levelToName", dict(logging._levelToName))
    monkeypatch.setattr(logging, "_nameToLevel", dict(logging._nameToLevel))
    stream = _configure_fallback_secret(secret)
    level = logging.WARNING + 5
    logging.addLevelName(level, level_name)
    logging.getLogger("tests.fallback.level").log(level, "%d", RAW_TOKEN)

    (record,) = _assert_no_fallback_secret(stream, capsys, secret)
    assert record["event"] == "log_record_unrenderable"
    assert record["level"] == "unknown"
    assert record["logger"] == "tests.fallback.level"


def test_exception_marker_drops_a_class_name_the_redaction_would_change(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _configure_fallback_secret()
    try:
        raise _PlaintextNamedNotesError(RAW_TOKEN)
    except _PlaintextNamedNotesError:
        get_logger("tests.fallback.marker").exception("call_failed")

    (record,) = _assert_no_fallback_secret(stream, capsys)
    assert record["event"] == "call_failed"
    assert record["exception"] == [{"exc_type": "unknown", "exc_value": "[UNRENDERABLE]"}]


def test_handler_error_report_drops_an_error_class_named_after_a_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = _PlaintextNamedFailingStream()
    configure_logging(
        stream=stream, settings=Settings(n8n_webhook_secret=SecretStr(KNOWN_PLAINTEXT))
    )
    get_logger("tests.fallback.write").warning("lost", access_token=RAW_TOKEN)
    logging.getLogger("tests.third_party").error("lost %s", RAW_TOKEN)

    lines = _assert_no_fallback_secret(stream, capsys)
    assert lines == [{"event": "log_handler_error", "error_type": "unknown"}] * 2


def test_safe_name_fails_closed_when_redaction_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingRedactor:
        def text(self, text: str) -> str:
            raise RuntimeError(RAW_TOKEN)

    assert ads_logging._safe_name("ads_engine.sync") == "ads_engine.sync"
    monkeypatch.setattr(ads_logging._STATE, "redactor", _RaisingRedactor())
    assert ads_logging._safe_name("ads_engine.sync") == "unknown"
    assert ads_logging._type_name(ValueError()) == "unknown"
    assert ads_logging._exc_info_type_name(ValueError()) == "unknown"


# ---------------------------------------------------------------------------
# install_excepthook(): an exception that ends the process
# ---------------------------------------------------------------------------

type _HookCall = tuple[type[BaseException], BaseException, TracebackType | None]


class _RecordingHook:
    """A stand-in for the interpreter's excepthook that records every call."""

    def __init__(self) -> None:
        self.calls: list[_HookCall] = []

    def __call__(
        self, exc_type: type[BaseException], value: BaseException, tb: TracebackType | None
    ) -> None:
        self.calls.append((exc_type, value, tb))


@pytest.fixture
def previous_hook(monkeypatch: pytest.MonkeyPatch) -> _RecordingHook:
    """Make a recording hook ``sys.excepthook``, as if no hook had been installed yet."""
    recorder = _RecordingHook()
    monkeypatch.setattr(sys, "excepthook", recorder)
    monkeypatch.setattr(ads_logging._STATE, "previous_excepthook", None)
    return recorder


def _uncaught(exc: BaseException) -> None:
    """Hand ``exc`` to ``sys.excepthook`` as the interpreter does when it ends the process."""
    sys.excepthook(type(exc), exc, exc.__traceback__)


def test_configure_logging_installs_the_excepthook_once(previous_hook: _RecordingHook) -> None:
    _configure()
    assert sys.excepthook is ads_logging._log_uncaught_exception
    assert ads_logging._STATE.previous_excepthook is previous_hook

    # Repeating is a no-op: the hook never chains onto itself.
    _configure()
    install_excepthook()
    assert sys.excepthook is ads_logging._log_uncaught_exception
    assert ads_logging._STATE.previous_excepthook is previous_hook


def test_a_replaced_excepthook_is_reinstalled_in_front_of_the_original(
    monkeypatch: pytest.MonkeyPatch, previous_hook: _RecordingHook
) -> None:
    install_excepthook()
    later = _RecordingHook()
    monkeypatch.setattr(sys, "excepthook", later)

    install_excepthook()
    assert sys.excepthook is ads_logging._log_uncaught_exception
    # The remembered hook is still the one present before the first install.
    assert ads_logging._STATE.previous_excepthook is previous_hook


def test_an_uncaught_exception_its_owner_logged_writes_nothing(
    previous_hook: _RecordingHook, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _configure()
    error = _raised(f"refused for {LEAD_EMAIL}")
    mark_logged(error, "api.startup_failed")

    _uncaught(error)

    assert stream.getvalue() == ""
    assert capsys.readouterr() == ("", "")
    assert previous_hook.calls == []


def test_an_unowned_uncaught_exception_is_one_redacted_critical_line(
    previous_hook: _RecordingHook, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    with pytest.raises(RuntimeError) as excinfo:
        _raise_with_secret_local()

    _uncaught(excinfo.value)

    output = stream.getvalue()
    (record,) = _lines(stream)
    assert record["event"] == "uncaught_exception"
    assert record["level"] == "critical"
    assert record["logger"] == "ads_engine.logging"
    assert record["error_type"] == "RuntimeError"
    (exception,) = record["exception"]
    assert exception["exc_type"] == "RuntimeError"
    assert exception["exc_value"] == f"failed with {REDACTED} for {REDACTED_EMAIL} (25)"
    assert exception["frames"]
    assert all("locals" not in frame for frame in exception["frames"])
    for value in (N8N_SECRET, LEAD_EMAIL, "test-unlisted-local-value"):
        assert value not in output
    assert capsys.readouterr() == ("", "")
    assert previous_hook.calls == []


def test_keyboard_interrupt_goes_to_the_previous_excepthook(
    previous_hook: _RecordingHook,
) -> None:
    stream = _configure()
    interrupt = KeyboardInterrupt()

    _uncaught(interrupt)

    assert previous_hook.calls == [(KeyboardInterrupt, interrupt, None)]
    assert stream.getvalue() == ""


def test_keyboard_interrupt_falls_back_to_the_interpreter_hook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Called before any install, the hook hands Ctrl-C to ``sys.__excepthook__``."""
    monkeypatch.setattr(ads_logging._STATE, "previous_excepthook", None)
    ads_logging._log_uncaught_exception(KeyboardInterrupt, KeyboardInterrupt(), None)
    assert capsys.readouterr().err == "KeyboardInterrupt\n"


@pytest.mark.parametrize(
    "error",
    [GeneratorExit(), BaseExceptionGroup("group", [KeyboardInterrupt()])],
    ids=["generator-exit", "base-exception-group"],
)
def test_other_base_exceptions_are_logged_not_handed_on(
    previous_hook: _RecordingHook, error: BaseException
) -> None:
    stream = _configure()

    _uncaught(error)

    (record,) = _lines(stream)
    assert record["event"] == "uncaught_exception"
    assert record["level"] == "critical"
    assert record["error_type"] == type(error).__name__
    assert previous_hook.calls == []


class _FailingLogger:
    """A logger whose every call raises, with a secret in the error's text."""

    def critical(self, event: str, **values: object) -> None:
        raise RecursionError(f"logging failed for {N8N_SECRET}")


def test_an_uncaught_exception_that_cannot_be_logged_writes_a_minimal_line(
    monkeypatch: pytest.MonkeyPatch,
    previous_hook: _RecordingHook,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The fallback names both classes only; the previous hook would print the text raw."""
    _configure(Settings(n8n_webhook_secret=SecretStr(N8N_SECRET)))
    monkeypatch.setattr(ads_logging, "get_logger", lambda name=None: _FailingLogger())

    _uncaught(_raised(f"failed with {N8N_SECRET} for {LEAD_EMAIL}"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("\n")
    (line,) = captured.err.splitlines()
    record = json.loads(line)
    assert set(record) == {
        "event",
        "level",
        "logger",
        "timestamp",
        "error_type",
        "logging_error_type",
    }
    assert record["event"] == "uncaught_exception"
    assert record["level"] == "critical"
    assert record["logger"] == "ads_engine.logging"
    assert record["error_type"] == "RuntimeError"
    assert record["logging_error_type"] == "RecursionError"
    assert N8N_SECRET not in captured.err
    assert LEAD_EMAIL not in captured.err
    assert previous_hook.calls == []


class _BrokenStderr:
    """A ``sys.stderr`` whose writes fail."""

    def write(self, text: str) -> int:
        raise OSError("stderr is closed")

    def flush(self) -> None:
        raise OSError("stderr is closed")


def test_the_minimal_line_is_dropped_quietly_when_stderr_is_broken(
    monkeypatch: pytest.MonkeyPatch, previous_hook: _RecordingHook
) -> None:
    _configure()
    monkeypatch.setattr(ads_logging, "get_logger", lambda name=None: _FailingLogger())
    monkeypatch.setattr(sys, "stderr", _BrokenStderr())

    _uncaught(_raised("nothing can be written"))

    assert previous_hook.calls == []


def test_the_minimal_line_withholds_a_class_named_after_a_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(Settings(n8n_webhook_secret=SecretStr("SecretClassName0451")))
    secret_class = type("SecretClassName0451", (RuntimeError,), {})
    monkeypatch.setattr(ads_logging, "get_logger", lambda name=None: _FailingLogger())

    ads_logging._log_uncaught_exception(secret_class, secret_class("x"), None)

    record = json.loads(capsys.readouterr().err)
    assert record["error_type"] == "unknown"
    assert record["logging_error_type"] == "RecursionError"


_UNCAUGHT_PROGRAM: Final = textwrap.dedent(
    f"""
    import sys

    from ads_engine.logging import configure_logging, mark_logged

    configure_logging()
    error = RuntimeError("uncaught with {N8N_SECRET} for {LEAD_EMAIL}")
    if sys.argv[1] == "owned":
        mark_logged(error, "owner_event")
    raise error
    """
)
"""Configures logging in a fresh interpreter, then ends it with an uncaught exception."""


@pytest.mark.parametrize("ownership", ["unowned", "owned"])
def test_an_uncaught_exception_ends_a_real_process_as_json_only(
    monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    """The interpreter still exits 1, and writes no traceback text of its own."""
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", N8N_SECRET)
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and program, no user input
        [sys.executable, "-c", _UNCAUGHT_PROGRAM, ownership],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout
    for value in (N8N_SECRET, LEAD_EMAIL):
        assert value not in completed.stdout
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    if ownership == "owned":
        assert records == []
    else:
        (record,) = records
        assert record["event"] == "uncaught_exception"
        assert record["level"] == "critical"
        assert record["error_type"] == "RuntimeError"
        assert record["exception"][0]["exc_value"] == (
            f"uncaught with {REDACTED} for {REDACTED_EMAIL}"
        )


# ---------------------------------------------------------------------------
# configure_logging(): settings loaded from the environment
# ---------------------------------------------------------------------------


def test_settings_are_loaded_from_the_environment_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", N8N_SECRET)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET_HEADER", "X-Hook-Auth")
    get_settings.cache_clear()
    stream = io.StringIO()
    configure_logging(stream=stream)
    get_logger("tests.logging.env").info(
        "sent", detail=f"secret {N8N_SECRET}", **{"X-Hook-Auth": HEADER_VALUE}
    )

    (record,) = _lines(stream)
    assert record["event"] == "sent"
    assert record["detail"] == "secret [REDACTED]"
    assert record["X-Hook-Auth"] == REDACTED


def test_invalid_settings_still_configure_logging_and_warn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_url = "not-a-webhook-url-test-value"
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", invalid_url)
    monkeypatch.setenv("BACKFILL_DAYS", "0")
    get_settings.cache_clear()
    stream = io.StringIO()
    configure_logging(stream=stream)
    get_logger("tests.logging.invalid").info("hook", **{"X-Webhook-Secret": HEADER_VALUE})

    output = stream.getvalue()
    assert invalid_url not in output
    assert HEADER_VALUE not in output
    warning, event = _lines(stream)
    assert warning["event"] == "logging_configured_without_settings"
    assert warning["level"] == "warning"
    assert warning["logger"] == "ads_engine.logging"
    assert warning["error_count"] == 2
    assert warning["fields"] == ["backfill_days", "n8n_google_ads_webhook_url"]
    # Redaction falls back to no secrets and the Settings field's default header name.
    assert ads_logging._DEFAULT_SECRET_HEADER == "X-Webhook-Secret"
    assert ads_logging._STATE.redactor is ads_logging._build_redactor(
        (), frozenset({ads_logging._DEFAULT_SECRET_HEADER})
    )
    assert event["X-Webhook-Secret"] == REDACTED


# ---------------------------------------------------------------------------
# get_logger()
# ---------------------------------------------------------------------------


def test_get_logger_without_a_name_uses_the_calling_module() -> None:
    stream = _configure()
    get_logger().info("unnamed")
    get_logger(None).info("explicit_none")

    records = _lines(stream)
    assert [record["logger"] for record in records] == [__name__, __name__]


def test_get_logger_with_a_name_uses_that_name() -> None:
    stream = _configure()
    get_logger("ads_engine.sync.runner").info("named")

    (record,) = _lines(stream)
    assert record["logger"] == "ads_engine.sync.runner"
