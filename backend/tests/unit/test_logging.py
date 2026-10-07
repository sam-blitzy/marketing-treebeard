"""Behavioural tests for ``ads_engine.logging``: JSON lines with secret and email redaction.

Every test runs against an environment stripped of each variable ``Settings`` reads, so
host-exported credentials never reach a test, and the autouse fixture restores the global
state :func:`configure_logging` replaces: root and third-party handlers and levels, the
structlog configuration, the active redactor and the structlog context. No test performs
network or database I/O. Every secret is an obvious test literal and every address uses a
reserved example domain.
"""

import copy
import datetime as dt
import io
import json
import logging
import os
import uuid
from collections import UserList, deque
from collections.abc import Iterator
from decimal import Decimal
from types import MappingProxyType
from typing import Any, cast

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
    redact,
    redact_processor,
)
from ads_engine.settings import Settings, get_settings

N8N_SECRET = "test-n8n-secret-value"
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
    removed and closed, so other test modules see logging exactly as before.
    """
    for key in list(os.environ):
        if key.lower() in SETTINGS_ENV_NAMES:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

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

    yield

    structlog.contextvars.clear_contextvars()
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


def test_secrets_shorter_than_eight_characters_are_ignored() -> None:
    secrets = ("short7c", "eight8ch", "ads")
    assert redact("short7c eight8ch ads_engine", secrets=secrets) == "short7c [REDACTED] ads_engine"


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
