"""Tests of ``ads_engine.cli``, the ``ads-engine`` command line, without a database.

AAP 0.5.1 ("Worker entry and CLI"), 0.3.3 ("Live captures"), 0.5.3 (exit code 3 while the
analysis lock is held) and 0.11.2 ("First start"). Covered here:

* the parser: seven commands, ``--help``/``--version`` exit 0, usage errors exit 2 and never
  repeat the supplied text;
* ``hash-password``: only the argon2id hash on stdout, one trailing newline dropped, an empty
  password a usage error;
* ``migrate``, ``ensure-admin``, ``sync``, ``analyze`` and ``seed-simulated`` with their
  collaborators replaced, so exit codes and JSON reports are checked without PostgreSQL
  (``tests/integration/test_first_start.py`` and ``test_runs_and_decisions.py`` run them
  against the database);
* error mapping: expected domain errors print one redacted line and exit 1, anything else is
  logged as ``cli_failed`` and exits 1;
* ``capture-fixtures``: its refusals, the redaction policy, the recording transport, and
  complete captures of every surface of all three sources, recorded from the simulator acting
  as the live platform and checked with ``payloads.validate_capture`` and for leaks;
* the capture bounds: on accounts where every listing has another page, each LinkedIn surface
  costs one request, and after a Statistics Finder 400 the LinkedIn segment fallback costs one
  discovery and one per-campaign request, each captured under its own surface; HubSpot's
  ``crm_search``, ``associations_batch_read`` (the initial call only, never a continuation)
  and ``objects_batch_read`` cost one request each, and ``properties`` costs the
  ``lifecyclestage`` read plus the fixed property reads of CRM binding verification;
* the Google capture's call budget: every webhook call is reserved in the shared
  ``GoogleCallBudget`` on the test database (the ``db`` fixture) and counted in
  ``source_connection.calls_24h``; a full window sends nothing; a database that cannot meter
  the calls refuses the capture before any request, or stops it at the failed reservation;
* the Google capture's scopes: Q-G3 reads every campaign for 30 days, Q-G5 every campaign on
  the delivering campaign's latest day with impressions, Q-G6 and Q-G7 the ad group whose
  keywords delivered most; a query that answers with no rows is reported as not captured,
  with its scope in words, and the capture still exits 0;
* the capture output directory: overlapping and failed writes leave complete files and no
  temporaries, symbolic links are replaced or refused rather than followed, and a redirected
  or unwritable ``captured/`` is refused before any request is sent.

No test opens a network connection: live transports are replaced by the simulator or an
``httpx.MockTransport``. Google captures meter their calls in the test database.
"""

import argparse
import asyncio
import errno
import io
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn
from urllib.parse import quote, unquote

import httpx
import pytest
import structlog
from argon2 import PasswordHasher, Type
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine import cli
from ads_engine import logging as ads_logging
from ads_engine.analysis import runner
from ads_engine.analysis.lock import RunInProgress
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.analysis.window import InvalidWindow
from ads_engine.connectors.errors import AccessDenied, QueryDefect
from ads_engine.connectors.google_n8n.queries import (
    AD_GROUP_HI,
    AD_GROUP_LO,
    CAMPAIGN_HI,
    CAMPAIGN_LO,
    END,
    QUERIES,
    START,
    ParsedQuery,
    parse_query,
)
from ads_engine.connectors.hubspot.client import STAGE_DATE_PROPERTY_PREFIX
from ads_engine.db import session as db_session
from ads_engine.decisions import SubjectRetired
from ads_engine.settings import ConfigurationError, Settings, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.payloads import canonical_json, validate_capture
from ads_engine.simulation.scenarios import FIXED_CLOCK
from ads_engine.simulation.transport import mock_transport
from ads_engine.sync import budget_guard, jobs, mode

BACKEND = Path(__file__).resolve().parents[2]
CONTRACTS = BACKEND / "tests" / "contracts"
CONFIG = BACKEND.parent / "config"

# Fake credentials, never real.
WEBHOOK_URL = "https://example-workspace.app.n8n.cloud/webhook/0f3c2a9b-test"
WEBHOOK_SECRET = "test-webhook-secret-0123456789"
HUBSPOT_TOKEN = "test-hubspot-token-abcdefghijkl"
LINKEDIN_REFRESH = "test-linkedin-refresh-zyxwvutsrq"
LINKEDIN_CLIENT_SECRET = "test-linkedin-client-secret-qwerty"
LINKEDIN_CLIENT_ID = "test-linkedin-client-id"
LINKEDIN_STATIC = "test-linkedin-static-token-123456"

CREDENTIAL_ENV = (
    "N8N_GOOGLE_ADS_WEBHOOK_URL",
    "N8N_WEBHOOK_SECRET",
    "N8N_WEBHOOK_SECRET_HEADER",
    "GOOGLE_ADS_CUSTOMER_ID",
    "LINKEDIN_AD_ACCOUNT_IDS",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_REFRESH_TOKEN",
    "LINKEDIN_ACCESS_TOKEN",
    "HUBSPOT_ACCESS_TOKEN",
    "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY",
    "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY",
    "APP_ADMIN_PASSWORD_HASH",
    "APP_ADMIN_EMAIL",
)


# Loggers configure_logging changes besides the root logger.
CONFIGURED_LOGGERS = (*ads_logging._QUIET_LOGGERS, *ads_logging._ROUTED_LOGGERS)


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip credentials and reload settings around every test."""
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_MODE", "simulated")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore the global logging state ``main`` replaces through ``configure_logging``.

    ``main`` points the root handler at the test's captured stderr, which pytest closes after
    the test; the handlers, levels, structlog configuration, redactor and last-resort handler
    are put back, as ``tests/unit/test_logging.py`` does, so later modules log as before.
    ``main``'s configuration also runs with structlog's logger caching off: a module logger
    first used under it would otherwise keep its processors for the rest of the session and
    bypass ``structlog.testing.capture_logs`` in later test modules.
    """
    real_configure = cli.configure_logging

    def configure_without_cache(**kwargs: Any) -> None:
        real_configure(**kwargs)
        structlog.configure(cache_logger_on_first_use=False)

    monkeypatch.setattr(cli, "configure_logging", configure_without_cache)
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


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Replace stdin with a non-interactive stream holding ``text``."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(text))


# ---------------------------------------------------------------------------
# Parser, help and usage errors
# ---------------------------------------------------------------------------


def test_help_lists_the_seven_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    for command in (
        "hash-password",
        "migrate",
        "ensure-admin",
        "sync",
        "analyze",
        "seed-simulated",
        "capture-fixtures",
    ):
        assert command in out


def test_version_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    assert (cli.EXIT_OK, cli.EXIT_FAILURE, cli.EXIT_USAGE, cli.EXIT_LOCK_HELD) == (0, 1, 2, 3)
    assert cli.main(["--version"]) == cli.EXIT_OK
    assert capsys.readouterr().out.strip() == "ads-engine 0.1.0"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["no-such-command"],
        ["analyze", "--start", "bad", "--end", "2026-09-28"],
        ["analyze", "--start", "2026-02-30", "--end", "2026-03-01"],
        ["analyze", "--start", "2026-09-01"],
        ["sync", "--source", "bing"],
        ["capture-fixtures"],
        ["--log-level", "loud", "migrate"],
    ],
)
def test_usage_errors_exit_2(argv: list[str]) -> None:
    assert cli.main(argv) == cli.EXIT_USAGE


# argparse writes usage errors before logging and its redaction are configured, so they must
# never repeat what was typed. The credential-like value is assembled at run time so the secret
# scan does not flag it.
LEAKED_EMAIL = "admin-leak" + "@" + "example.com"
PASTED_SECRET = "pasted-" + "secret-" + "value"
CONTROL_TEXT = "\x1b[31m" + PASTED_SECRET + "\x07\r"
COMMANDS_CHOICE = (
    "argument COMMAND: invalid choice (choose from hash-password, migrate, ensure-admin, sync, "
    "analyze, seed-simulated, capture-fixtures)"
)
SOURCE_CHOICE = "argument --source: invalid choice (choose from google, linkedin, hubspot)"


def _assert_redacted(err: str, supplied: tuple[str, ...]) -> None:
    """Assert stderr holds none of the supplied values, raw or quoted, and no control byte."""
    folded = err.casefold()
    for value in supplied:
        assert value.casefold() not in folded
        assert repr(value).casefold() not in folded
    assert all(char == "\n" or char.isprintable() for char in err), repr(err)


@pytest.mark.parametrize(
    ("argv", "supplied", "diagnostic"),
    [
        pytest.param(
            ["analyze", "--start", LEAKED_EMAIL, "--end", "2026-09-28"],
            (LEAKED_EMAIL,),
            "ads-engine analyze: error: argument --start: not a date in YYYY-MM-DD form",
            id="date-email",
        ),
        pytest.param(
            ["analyze", "--start", "2026-09-01", f"--end={PASTED_SECRET}"],
            (PASTED_SECRET,),
            "ads-engine analyze: error: argument --end: not a date in YYYY-MM-DD form",
            id="date-credential",
        ),
        pytest.param(
            ["sync", "--source", LEAKED_EMAIL],
            (LEAKED_EMAIL,),
            f"ads-engine sync: error: {SOURCE_CHOICE}",
            id="source-email",
        ),
        pytest.param(
            ["capture-fixtures", "--source", PASTED_SECRET],
            (PASTED_SECRET,),
            f"ads-engine capture-fixtures: error: {SOURCE_CHOICE}",
            id="source-credential",
        ),
        pytest.param(
            ["--log-level", PASTED_SECRET, "migrate"],
            (PASTED_SECRET,),
            "ads-engine: error: argument --log-level: invalid choice "
            "(choose from DEBUG, INFO, WARNING, ERROR, CRITICAL)",
            id="log-level-choice",
        ),
        pytest.param(
            ["migrate", f"--password={PASTED_SECRET}"],
            (PASTED_SECRET, "--password"),
            "ads-engine: error: unrecognized arguments (1)",
            id="unknown-option-value",
        ),
        pytest.param(
            ["migrate", CONTROL_TEXT, LEAKED_EMAIL],
            (CONTROL_TEXT, PASTED_SECRET, LEAKED_EMAIL),
            "ads-engine: error: unrecognized arguments (2)",
            id="unknown-positional-controls",
        ),
        pytest.param(
            [f"--help={PASTED_SECRET}"],
            (PASTED_SECRET,),
            "ads-engine: error: argument -h/--help: ignored explicit argument",
            id="help-explicit-argument",
        ),
        pytest.param(
            [f"--version={PASTED_SECRET}"],
            (PASTED_SECRET,),
            "ads-engine: error: argument --version: ignored explicit argument",
            id="version-explicit-argument",
        ),
        pytest.param(
            [LEAKED_EMAIL],
            (LEAKED_EMAIL,),
            f"ads-engine: error: {COMMANDS_CHOICE}",
            id="unknown-command-email",
        ),
        pytest.param(
            [f"--={PASTED_SECRET}", "migrate"],
            (PASTED_SECRET,),
            "ads-engine: error: ambiguous option (could match --help, --version, --log-level)",
            id="ambiguous-option",
        ),
        pytest.param(
            ["analyze", "--start", "2026-09-01"],
            (),
            "ads-engine analyze: error: the following arguments are required: --end",
            id="required-passes-unchanged",
        ),
    ],
)
def test_usage_errors_never_repeat_supplied_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    supplied: tuple[str, ...],
    diagnostic: str,
) -> None:
    # Usage colouring is argparse's own; keep it off so only the diagnostic's text is checked.
    monkeypatch.setenv("PYTHON_COLORS", "0")
    assert cli.main(argv) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("usage: ")
    assert captured.err.endswith(f"\n{diagnostic}\n")
    _assert_redacted(captured.err, supplied)


@pytest.mark.parametrize(
    ("message", "diagnostic"),
    [
        pytest.param(
            f"can't open '{LEAKED_EMAIL}': [Errno 2] No such file or directory",
            "invalid usage; run 'ads-engine --help'",
            id="unknown-form",
        ),
        pytest.param(
            f"argument --log-level: {LEAKED_EMAIL} is not acceptable",
            "argument --log-level: invalid usage; run 'ads-engine --help'",
            id="unknown-reason-keeps-argument-name",
        ),
        pytest.param(
            f"argument --{PASTED_SECRET}: invalid choice: 'x' (choose from 'x')",
            "invalid usage; run 'ads-engine --help'",
            id="argument-name-not-vocabulary",
        ),
        pytest.param(
            f"the following arguments are required: COMMAND, {LEAKED_EMAIL}",
            "invalid usage; run 'ads-engine --help'",
            id="required-name-not-vocabulary",
        ),
        pytest.param(
            f"ambiguous option: --{PASTED_SECRET} could match --{PASTED_SECRET}",
            "ambiguous option",
            id="ambiguous-match-not-vocabulary",
        ),
        pytest.param(
            f"unrecognized arguments: {PASTED_SECRET} {LEAKED_EMAIL}",
            "unrecognized arguments",
            id="uncounted-unrecognized",
        ),
        pytest.param(
            f"argument COMMAND: unknown parser '{LEAKED_EMAIL}' (choices: migrate)",
            COMMANDS_CHOICE,
            id="unknown-parser",
        ),
        pytest.param(
            f"argument --log-level: invalid choice: '{PASTED_SECRET}', maybe you meant 'DEBUG'? "
            "(choose from 'DEBUG')",
            "argument --log-level: invalid choice "
            "(choose from DEBUG, INFO, WARNING, ERROR, CRITICAL)",
            id="invalid-choice-suggestion",
        ),
        pytest.param(
            f"argument --log-level: invalid upper value: '{PASTED_SECRET}'",
            "argument --log-level: invalid upper value",
            id="invalid-type-value",
        ),
        pytest.param(
            "argument --log-level: expected 2 arguments",
            "argument --log-level: expected 2 arguments",
            id="value-free-passes-unchanged",
        ),
    ],
)
def test_parser_error_rewrites_or_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
    diagnostic: str,
) -> None:
    monkeypatch.setenv("PYTHON_COLORS", "0")
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exited:
        parser.error(message)
    assert exited.value.code == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert err.endswith(f"\nads-engine: error: {diagnostic}\n")
    _assert_redacted(err, (LEAKED_EMAIL, PASTED_SECRET))


def test_sub_parsers_inherit_the_redacting_parser() -> None:
    parser = cli.build_parser()
    commands = next(action for action in parser._actions if action.dest == "command")
    assert isinstance(commands.choices, dict)
    assert len(commands.choices) == 7
    assert {type(sub) for sub in commands.choices.values()} == {type(parser)}


def test_unrecognized_arguments_raise_counted_without_exit_on_error() -> None:
    parser = cli._RedactingArgumentParser(prog="ads-engine", exit_on_error=False)
    with pytest.raises(argparse.ArgumentError) as raised:
        parser.parse_args([LEAKED_EMAIL])
    assert str(raised.value) == "unrecognized arguments (1)"


# ---------------------------------------------------------------------------
# hash-password
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "password"), [("pw", "pw"), ("pw\n", "pw"), ("pw\r\n", "pw")])
def test_hash_password_prints_only_the_hash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str, password: str
) -> None:
    _stdin(monkeypatch, raw)
    assert cli.main(["hash-password"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert out.endswith("\n")
    assert lines[0].startswith("$argon2id$")
    assert PasswordHasher().verify(lines[0], password)


def test_hash_password_keeps_inner_newlines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "a\n\n")
    assert cli.main(["hash-password"]) == cli.EXIT_OK
    assert PasswordHasher().verify(capsys.readouterr().out.strip(), "a\n")


@pytest.mark.parametrize("raw", ["", "\n", "\r\n"])
def test_hash_password_empty_is_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    _stdin(monkeypatch, raw)
    assert cli.main(["hash-password"]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "the password read from stdin is empty" in captured.err


def test_hash_password_reads_no_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An environment that cannot be parsed into settings does not matter to hash-password.
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", "not-ten-digits")
    get_settings.cache_clear()
    _stdin(monkeypatch, "pw")
    assert cli.main(["hash-password"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.startswith("$argon2id$")
    assert "logging_configured_without_settings" not in captured.err


def test_hash_password_uses_getpass_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdin", Terminal(""))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "typed")
    assert cli.main(["hash-password"]) == cli.EXIT_OK
    assert PasswordHasher().verify(capsys.readouterr().out.strip(), "typed")


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_domain_error_messages() -> None:
    assert (
        cli._domain_error_message(ConfigurationError("SESSION_SECRET is not set"))
        == "ConfigurationError: SESSION_SECRET is not set"
    )
    denied = AccessDenied("no scope", source="hubspot", endpoint="properties", http_status=403)
    assert cli._domain_error_message(denied) == f"AccessDenied: {denied.reason}"
    defect = QueryDefect("bad", source="google", endpoint="search Q-G1")
    assert str(cli._domain_error_message(defect)).startswith("QueryDefect: google search Q-G1")
    retired = SubjectRetired()
    assert cli._domain_error_message(retired) == f"SubjectRetired: {retired.message}"
    assert cli._domain_error_message(RuntimeError("boom")) is None


def test_settings_validation_error_names_fields_only() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(google_ads_customer_id="secret-value-12")
    message = cli._domain_error_message(caught.value)
    assert message is not None
    assert message.startswith("invalid settings: google_ads_customer_id (1 error)")
    assert "secret-value-12" not in message


def test_other_validation_errors_are_unexpected() -> None:
    class Other(BaseModel):
        x: int

    with pytest.raises(ValidationError) as caught:
        Other(x="no")  # type: ignore[arg-type]
    assert cli._domain_error_message(caught.value) is None


def test_main_maps_domain_and_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def domain(_args: object) -> int:
        raise ConfigurationError("unsafe base URL for owner@example.com")

    def unexpected(_args: object) -> int:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "_cmd_migrate", domain)
    assert cli.main(["migrate"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "ads-engine: migrate: ConfigurationError: unsafe base URL for [REDACTED_EMAIL]" in err
    assert "owner@example.com" not in err

    monkeypatch.setattr(cli, "_cmd_migrate", unexpected)
    assert cli.main(["migrate"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert '"event": "cli_failed"' in err
    assert '"command": "migrate"' in err
    assert "ads-engine: migrate failed (RuntimeError)" in err


def _cli_failed_records(err: str) -> list[dict[str, Any]]:
    """The ``cli_failed`` JSON lines among the CLI's stderr lines."""
    records = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    return [record for record in records if record.get("event") == "cli_failed"]


def test_main_reports_an_already_logged_failure_without_a_second_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def owned(_args: object) -> int:
        error = RuntimeError("rule exploded")
        ads_logging.mark_logged(error, "analysis_run_failed")
        raise error

    def unowned(_args: object) -> int:
        raise RuntimeError("rule exploded")

    monkeypatch.setattr(cli, "_cmd_migrate", owned)
    assert cli.main(["migrate"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    (record,) = _cli_failed_records(err)
    assert record["level"] == "warning"
    assert (record["command"], record["error"]) == ("migrate", "RuntimeError")
    assert record["logged_as"] == "analysis_run_failed"
    assert "exception" not in record
    assert "ads-engine: migrate failed (RuntimeError)" in err

    monkeypatch.setattr(cli, "_cmd_migrate", unowned)
    assert cli.main(["migrate"]) == cli.EXIT_FAILURE
    (record,) = _cli_failed_records(capsys.readouterr().err)
    assert record["level"] == "error"
    assert "logged_as" not in record
    (exception,) = record["exception"]
    assert exception["exc_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# migrate and ensure-admin
# ---------------------------------------------------------------------------


def test_migrate_needs_alembic_ini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["migrate"]) == cli.EXIT_USAGE
    assert "run from backend/ (or /app in the image)" in capsys.readouterr().err


def test_migrate_upgrades_to_head_without_ini_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command

    calls: list[tuple[Any, str]] = []
    monkeypatch.chdir(BACKEND)
    monkeypatch.setattr(
        command, "upgrade", lambda config, revision: calls.append((config, revision))
    )
    assert cli.main(["migrate"]) == cli.EXIT_OK
    assert cli.main(["migrate"]) == cli.EXIT_OK
    assert [revision for _config, revision in calls] == ["head", "head"]
    config = calls[0][0]
    assert config.attributes["configure_logger"] is False
    assert config.get_main_option("sqlalchemy.url") is None
    assert Path(config.config_file_name) == BACKEND / "alembic.ini"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "APP_ADMIN_PASSWORD_HASH is not set; run `make init ADMIN_PASSWORD=…`"),
        ("plain-text-password", "is not an argon2 hash"),
        ("$argon2id$v=19$garbage", "is not a valid argon2 hash"),
    ],
)
def test_ensure_admin_rejects_a_missing_or_bad_hash(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
    message: str,
) -> None:
    if value is not None:
        monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", value)
    get_settings.cache_clear()
    assert cli.main(["ensure-admin"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert message in err
    if value is not None:
        assert value not in err


def _edited_hash(**fields: str) -> str:
    """Return a fresh argon2id hash of a test password with some of its PHC fields replaced.

    The fields are ``version`` (``v=19``), ``params`` (``m=…,t=…,p=…``), ``salt`` and
    ``digest``. The hash is computed at run time, so no argon2 hash literal is committed (the
    secret scan reports every one).
    """
    empty, kind, version, params, salt, digest = PasswordHasher().hash("pw").split("$")
    parts = {"version": version, "params": params, "salt": salt, "digest": digest} | fields
    return "$".join(
        [empty, kind, parts["version"], parts["params"], parts["salt"], parts["digest"]]
    )


_OUT_OF_RANGE = "has argon2 parameters outside the supported range: "
_UNREADABLE = "is not a valid argon2 hash (it cannot be read as an argon2 PHC string)"
_UNDECODABLE = "cannot be verified by argon2 (Decoding failed)"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        pytest.param(
            "$argon2id$v=19$m=0,t=0,p=0$$",
            f"{_OUT_OF_RANGE}the salt must be at least 8 bytes",
            id="all-zero-empty-salt-and-digest",
        ),
        pytest.param({"salt": "!" * 22}, _UNDECODABLE, id="salt-not-base64"),
        pytest.param(
            {"salt": "A" * 10},
            f"{_OUT_OF_RANGE}the salt must be at least 8 bytes",
            id="salt-7-bytes",
        ),
        pytest.param(
            {"digest": "A" * 4},
            f"{_OUT_OF_RANGE}the digest must be at least 4 bytes",
            id="digest-3-bytes",
        ),
        pytest.param(
            {"params": "m=65536,t=0,p=4"},
            f"{_OUT_OF_RANGE}the time cost must be 1 to 10",
            id="time-cost-0",
        ),
        pytest.param(
            {"params": "m=65536,t=11,p=4"},
            f"{_OUT_OF_RANGE}the time cost must be 1 to 10",
            id="time-cost-11",
        ),
        pytest.param(
            {"params": "m=65536,t=3,p=0"},
            f"{_OUT_OF_RANGE}the parallelism must be 1 to 16",
            id="parallelism-0",
        ),
        pytest.param(
            {"params": "m=65536,t=3,p=17"},
            f"{_OUT_OF_RANGE}the parallelism must be 1 to 16",
            id="parallelism-17",
        ),
        pytest.param(
            {"params": "m=31,t=3,p=4"},
            f"{_OUT_OF_RANGE}the memory cost must be at least 8 KiB per lane",
            id="memory-below-8-kib-per-lane",
        ),
        pytest.param(
            {"params": "m=-1,t=3,p=4"},
            f"{_OUT_OF_RANGE}the memory cost must be at least 8 KiB per lane",
            id="memory-negative",
        ),
        pytest.param(
            {"params": "m=2097153,t=3,p=4"},
            f"{_OUT_OF_RANGE}the memory cost must be at most 2097152 KiB (2 GiB)",
            id="memory-above-2-gib",
        ),
        pytest.param({"params": "m=65536,t=\u0663,p=4"}, _UNREADABLE, id="non-ascii-digit"),
        pytest.param({"salt": "A" * 11 + "\x1b" + "A" * 10}, _UNREADABLE, id="control-character"),
        pytest.param({"params": "m=65_536,t=3,p=4"}, _UNDECODABLE, id="underscore-in-number"),
        pytest.param({"version": "v=+19"}, _UNDECODABLE, id="signed-version"),
    ],
)
def test_ensure_admin_rejects_a_hash_argon2_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    edit: str | dict[str, str],
    message: str,
) -> None:
    """A hash argon2 parses but cannot verify fails before the transaction, never echoed.

    ``argon2.extract_parameters`` accepts every one of these values; stored, each would make
    every sign-in fail and lock the single admin out.
    """
    opened: list[str] = []

    def session_scope() -> None:
        opened.append("session_scope")
        raise AssertionError("ensure-admin opened its transaction for an invalid hash")

    monkeypatch.setattr(db_session, "session_scope", session_scope)
    value = edit if isinstance(edit, str) else _edited_hash(**edit)
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", value)
    get_settings.cache_clear()

    assert cli.main(["ensure-admin"]) == cli.EXIT_FAILURE
    captured = capsys.readouterr()
    assert opened == []
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert len(lines) == 1, captured.err
    assert lines[0].startswith("ads-engine: APP_ADMIN_PASSWORD_HASH ")
    assert message in lines[0]
    assert lines[0].endswith("; write it again with `ads-engine hash-password`")
    assert value not in captured.err
    for part in value.split("$"):
        if len(part) >= 8:
            assert part not in captured.err


class _Result:
    """A stand-in for a SQLAlchemy result with the two reads ensure-admin makes."""

    def __init__(self, rows: list[int]) -> None:
        self._rows = rows

    def scalar_one(self) -> int:
        return self._rows[0]

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[int]:
        return self._rows


def test_ensure_admin_upserts_by_email_and_removes_other_logins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password_hash = PasswordHasher().hash("pw")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", password_hash)
    monkeypatch.setenv("APP_ADMIN_EMAIL", "Admin@Example.com")
    get_settings.cache_clear()
    statements: list[str] = []

    class Session:
        async def execute(self, statement: Any) -> _Result:
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return _Result([7] if len(statements) == 1 else [3, 4])

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(db_session, "session_scope", scope)
    assert cli.main(["ensure-admin"]) == cli.EXIT_OK
    upsert, removal = statements
    assert upsert.startswith("INSERT INTO app_user (email, password_hash)")
    assert "ON CONFLICT (email) DO UPDATE SET password_hash = excluded.password_hash" in upsert
    assert "RETURNING app_user.id" in upsert
    assert removal.startswith("DELETE FROM app_user WHERE app_user.email != ")


@pytest.mark.parametrize(
    "hasher",
    [
        pytest.param(PasswordHasher(), id="hash-password-defaults"),
        pytest.param(
            PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1),
            id="argon2id-t2-m19456-p1",
        ),
        pytest.param(PasswordHasher(type=Type.I), id="argon2i"),
        pytest.param(PasswordHasher(type=Type.D), id="argon2d"),
        pytest.param(
            PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, hash_len=4, salt_len=8),
            id="libargon2-minimums",
        ),
        pytest.param(
            PasswordHasher(time_cost=10, memory_cost=128, parallelism=16), id="time-and-lane-caps"
        ),
    ],
)
def test_ensure_admin_accepts_every_hash_argon2_verifies(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], hasher: PasswordHasher
) -> None:
    """Any argon2id, argon2i or argon2d hash within the supported range reaches the upsert.

    ``make init`` writes the ``hash-password`` defaults, and ``api/security.py`` verifies every
    argon2 type, so none of these may be refused.
    """
    password_hash = hasher.hash("pw")
    monkeypatch.setenv("APP_ADMIN_PASSWORD_HASH", password_hash)
    get_settings.cache_clear()
    written: list[dict[str, Any]] = []

    class Session:
        async def execute(self, statement: Any) -> _Result:
            written.append(statement.compile(dialect=postgresql.dialect()).params)
            return _Result([7] if len(written) == 1 else [])

    @asynccontextmanager
    async def scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(db_session, "session_scope", scope)
    assert cli.main(["ensure-admin"]) == cli.EXIT_OK
    assert written[0]["password_hash"] == password_hash
    assert password_hash not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# sync, analyze and seed-simulated
# ---------------------------------------------------------------------------


def _job(disposition: str | None) -> jobs.JobResult:
    return jobs.JobResult(
        job="entities",
        source="google",
        dataset_id=1,
        status="succeeded" if disposition is None else "failed",
        rows=4,
        calls=1,
        freshness={},
        disposition=disposition,  # type: ignore[arg-type]
        error=None if disposition is None else "google search Q-G1: HTTP 400: bad query",
        duration_ms=5,
    )


@pytest.mark.parametrize(("disposition", "code"), [(None, 0), ("stale", 0), ("defect", 1)])
def test_sync_prints_the_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    disposition: str | None,
    code: int,
) -> None:
    seen: list[tuple[str, str]] = []

    async def run_source_jobs(source: str, *, trigger: str) -> jobs.SourceSyncReport:
        seen.append((source, trigger))
        return jobs.SourceSyncReport(
            source="google",
            trigger="manual",
            results=(_job(disposition),),
            dataset_id=1,
            mode="simulated",
        )

    monkeypatch.setattr(jobs, "run_source_jobs", run_source_jobs)
    assert cli.main(["sync", "--source", "google"]) == code
    assert seen == [("google", "manual")]
    report = json.loads(capsys.readouterr().out)
    assert report["source"] == "google"
    assert report["mode"] == "simulated"
    assert report["jobs"][0]["job"] == "entities"


def test_sync_exits_failure_on_a_data_integrity_defect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    violation = jobs.IntegrityFailure(
        kind="foreign_key_violation",
        sqlstate="23503",
        table="fact_campaign_daily",
        constraint="fk_fact_campaign_daily_campaign_id_campaign",
    )
    failed = jobs.JobResult(
        job="metrics",
        source="google",
        dataset_id=1,
        status="failed",
        rows=0,
        calls=3,
        freshness={},
        disposition=None,
        error=violation.reason,
        duration_ms=5,
        integrity=violation,
    )

    async def run_source_jobs(source: str, *, trigger: str) -> jobs.SourceSyncReport:
        return jobs.SourceSyncReport(
            source="google", trigger="manual", results=(failed,), dataset_id=1, mode="simulated"
        )

    monkeypatch.setattr(jobs, "run_source_jobs", run_source_jobs)
    assert cli.main(["sync", "--source", "google"]) == cli.EXIT_FAILURE
    captured = capsys.readouterr()
    (job,) = json.loads(captured.out)["jobs"]
    assert job["integrity"] == violation.to_json()
    assert job["error"] == violation.reason
    assert "a job failed on a defect (query or data integrity)" in captured.err


def _fake_run(kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=41,
        kind=kind,
        status="succeeded",
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 28),
    )


def test_analyze_runs_a_custom_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[Any, ...]] = []

    async def custom_window(start: date, end: date, **kwargs: Any) -> str:
        calls.append(("window", start, end, kwargs["thresholds"] is not None))
        return "window"

    async def run_analysis(window: str, kind: str, trigger: str, **kwargs: Any) -> Any:
        calls.append(("run", window, kind, trigger))
        return _fake_run(kind)

    monkeypatch.setattr(runner, "custom_window", custom_window)
    monkeypatch.setattr(runner, "run_analysis", run_analysis)
    assert cli.main(["analyze", "--start", "2026-09-01", "--end", "2026-09-28"]) == cli.EXIT_OK
    assert calls == [
        ("window", date(2026, 9, 1), date(2026, 9, 28), True),
        ("run", "window", "custom", "manual"),
    ]
    assert json.loads(capsys.readouterr().out) == {
        "run_id": 41,
        "kind": "custom",
        "status": "succeeded",
        "window_start": "2026-09-01",
        "window_end": "2026-09-28",
    }


def test_analyze_invalid_window_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def custom_window(start: date, end: date, **kwargs: Any) -> str:
        raise InvalidWindow("too_long", "Window of 393 days exceeds the 180-day maximum")

    monkeypatch.setattr(runner, "custom_window", custom_window)
    assert cli.main(["analyze", "--start", "2025-09-01", "--end", "2026-09-28"]) == cli.EXIT_USAGE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid window (too_long): Window of 393 days" in captured.err


def test_analyze_lock_held_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def custom_window(start: date, end: date, **kwargs: Any) -> str:
        return "window"

    async def run_analysis(window: str, kind: str, trigger: str, **kwargs: Any) -> Any:
        raise RunInProgress

    monkeypatch.setattr(runner, "custom_window", custom_window)
    monkeypatch.setattr(runner, "run_analysis", run_analysis)
    code = cli.main(["analyze", "--start", "2026-09-01", "--end", "2026-09-28"])
    assert code == cli.EXIT_LOCK_HELD
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "an analysis run is in progress" in captured.err


def _bootstrap_report(failures: dict[str, str]) -> mode.BootstrapReport:
    modes = {
        source: mode.ResolvedMode(source, "simulated", "DATA_MODE=simulated")
        for source in ("google", "hubspot", "linkedin")
    }
    active = {
        "google": mode.ActiveDataset(1, "simulated", "DATA_MODE=simulated"),
        "hubspot": None if "hubspot" in failures else mode.ActiveDataset(2, "simulated", None),
        "linkedin": mode.ActiveDataset(3, "simulated", None, companion_id=4),
    }
    return mode.BootstrapReport(
        outcomes={},
        modes=modes,
        active=active,
        failures=failures,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("failures", "lock_held", "code"),
    [({}, False, 0), ({"hubspot": "ConfigError: no config"}, False, 1), ({}, True, 3)],
)
def test_seed_simulated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failures: dict[str, str],
    lock_held: bool,
    code: int,
) -> None:
    forced: list[bool] = []

    async def bootstrap_sources(settings: Settings, *, force_simulated: bool) -> Any:
        forced.append(force_simulated)
        return _bootstrap_report(failures)

    async def default_window(**kwargs: Any) -> str:
        return "default-window"

    async def run_analysis(window: str, kind: str, trigger: str, **kwargs: Any) -> Any:
        assert (window, kind, trigger) == ("default-window", "default", "manual")
        if lock_held:
            raise RunInProgress
        return _fake_run(kind)

    monkeypatch.setattr(mode, "bootstrap_sources", bootstrap_sources)
    monkeypatch.setattr(runner, "default_window", default_window)
    monkeypatch.setattr(runner, "run_analysis", run_analysis)
    assert cli.main(["seed-simulated"]) == code
    assert forced == [True]
    output = json.loads(capsys.readouterr().out)
    assert output["sources"]["linkedin"] == {
        "mode": "simulated",
        "reason": "DATA_MODE=simulated",
        "dataset_id": 3,
        "dataset_mode": "simulated",
        "companion_id": 4,
        "failure": None,
    }
    assert output["sources"]["hubspot"]["failure"] == failures.get("hubspot")
    assert (output["run"] is None) == lock_held


# ---------------------------------------------------------------------------
# capture-fixtures: refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["google", "linkedin", "hubspot"])
def test_capture_refused_under_simulated_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    monkeypatch.chdir(BACKEND)
    before = sorted(CONTRACTS.glob("*/captured/*"))
    assert cli.main(["capture-fixtures", "--source", source]) == cli.EXIT_FAILURE
    assert "capture-fixtures records live traffic only" in capsys.readouterr().err
    assert sorted(CONTRACTS.glob("*/captured/*")) == before


@pytest.mark.parametrize(
    ("source", "names"),
    [
        ("google", "N8N_GOOGLE_ADS_WEBHOOK_URL, N8N_WEBHOOK_SECRET, GOOGLE_ADS_CUSTOMER_ID"),
        ("hubspot", "HUBSPOT_ACCESS_TOKEN"),
        (
            "linkedin",
            "LINKEDIN_AD_ACCOUNT_IDS and either LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET and "
            "LINKEDIN_REFRESH_TOKEN, or LINKEDIN_ACCESS_TOKEN",
        ),
    ],
)
def test_capture_refused_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], source: str, names: str
) -> None:
    monkeypatch.chdir(BACKEND)
    monkeypatch.setenv("DATA_MODE", "auto")
    get_settings.cache_clear()
    before = sorted(CONTRACTS.glob("*/captured/*"))
    assert cli.main(["capture-fixtures", "--source", source]) == cli.EXIT_FAILURE
    assert (
        f"ads-engine: capture-fixtures {source}: live credentials are not configured; set {names}"
    ) in capsys.readouterr().err.splitlines()
    assert sorted(CONTRACTS.glob("*/captured/*")) == before


@pytest.mark.parametrize(
    ("configured", "names"),
    [
        pytest.param(
            {},
            "LINKEDIN_AD_ACCOUNT_IDS and either LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET and "
            "LINKEDIN_REFRESH_TOKEN, or LINKEDIN_ACCESS_TOKEN",
            id="nothing-set",
        ),
        pytest.param(
            {"LINKEDIN_AD_ACCOUNT_IDS": "507404993"},
            "either LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN, "
            "or LINKEDIN_ACCESS_TOKEN",
            id="account-ids-only",
        ),
        pytest.param(
            {"LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID},
            "LINKEDIN_AD_ACCOUNT_IDS and either LINKEDIN_CLIENT_SECRET and LINKEDIN_REFRESH_TOKEN "
            "(to complete the refresh-token trio), or LINKEDIN_ACCESS_TOKEN",
            id="partial-trio",
        ),
        pytest.param(
            {
                "LINKEDIN_AD_ACCOUNT_IDS": "507404993",
                "LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID,
                "LINKEDIN_CLIENT_SECRET": LINKEDIN_CLIENT_SECRET,
            },
            "either LINKEDIN_REFRESH_TOKEN (to complete the refresh-token trio), "
            "or LINKEDIN_ACCESS_TOKEN",
            id="account-ids-and-partial-trio",
        ),
        pytest.param(
            {
                "LINKEDIN_CLIENT_ID": LINKEDIN_CLIENT_ID,
                "LINKEDIN_CLIENT_SECRET": LINKEDIN_CLIENT_SECRET,
                "LINKEDIN_REFRESH_TOKEN": LINKEDIN_REFRESH,
            },
            "LINKEDIN_AD_ACCOUNT_IDS",
            id="refresh-trio-without-account-ids",
        ),
        pytest.param(
            {"LINKEDIN_ACCESS_TOKEN": LINKEDIN_STATIC},
            "LINKEDIN_AD_ACCOUNT_IDS",
            id="access-token-without-account-ids",
        ),
    ],
)
def test_capture_refusal_names_the_linkedin_alternatives(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: dict[str, str],
    names: str,
) -> None:
    """LinkedIn needs its account ids plus either the refresh-token trio or an access token."""
    monkeypatch.chdir(BACKEND)
    monkeypatch.setenv("DATA_MODE", "auto")
    for name, value in configured.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    before = sorted(CONTRACTS.glob("*/captured/*"))
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert (
        f"ads-engine: capture-fixtures linkedin: live credentials are not configured; set {names}"
    ) in err.splitlines()
    for value in configured.values():
        assert value not in err
    assert sorted(CONTRACTS.glob("*/captured/*")) == before


def test_capture_needs_the_contracts_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_MODE", "auto")
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", HUBSPOT_TOKEN)
    get_settings.cache_clear()
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_USAGE
    assert "tests/contracts/hubspot not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# capture-fixtures: redaction policy
# ---------------------------------------------------------------------------


def _policy() -> Any:
    return cli._RedactionPolicy.from_settings(
        Settings(
            n8n_google_ads_webhook_url=WEBHOOK_URL,
            n8n_webhook_secret=WEBHOOK_SECRET,
            n8n_webhook_secret_header="X-Webhook-Token",
            google_ads_customer_id="1234567890",
            linkedin_ad_account_ids="512345678",
            hubspot_access_token=HUBSPOT_TOKEN,
            linkedin_access_token="short",
        )
    )


def test_policy_text_redacts_secrets_ids_and_emails() -> None:
    policy = _policy()
    assert policy.text("lead jane.doe@example.com here") == "lead <redacted> here"
    assert (
        policy.text("customers/1234567890/campaigns/12345678901")
        == "customers/0000000000/campaigns/12345678901"
    )
    assert policy.text("urn:li:sponsoredAccount:512345678") == "urn:li:sponsoredAccount:000000000"
    assert policy.text("51234567899") == "51234567899"
    assert policy.text(f"Bearer {HUBSPOT_TOKEN}!") == "Bearer <redacted>!"
    assert policy.text(f"see {WEBHOOK_URL}") == "see <redacted>"
    # A short secret is replaced wherever it occurs, as a long one is.
    assert policy.text("short") == "<redacted>"
    assert policy.text("shortcut") == "<redacted>cut"
    assert policy.value(512345678, frozenset()) == 0
    assert policy.value(True, frozenset()) is True


def test_policy_value_blanks_people_credentials_and_answers() -> None:
    policy = _policy()
    body = {
        "properties": {
            "email": "a@b.co",
            "firstname": "Jane",
            "phone": 5551234,
            "dealstage": "won",
        },
        "access_token": "abc",
        "code": "ERR_CODE",
        "formResponse": {
            "answers": [
                {
                    "questionId": 7,
                    "answerDetails": {"textQuestionAnswer": {"answer": "free text"}, "n": 3},
                }
            ]
        },
        "results": {"a@b.co": {"id": "1"}},
        "archived": False,
        "localized": {"firstName": {"localized": {"en_US": "Jane"}}},
    }
    redacted = policy.value(body, cli._CREDENTIAL_KEYS | cli._PERSON_KEYS)
    assert redacted == {
        "properties": {
            "email": "<redacted>",
            "firstname": "<redacted>",
            "phone": "<redacted>",
            "dealstage": "won",
        },
        "access_token": "<redacted>",
        "code": "ERR_CODE",
        "formResponse": {
            "answers": [
                {
                    "questionId": 7,
                    "answerDetails": {"textQuestionAnswer": {"answer": "<redacted>"}, "n": 3},
                }
            ]
        },
        "results": {"<redacted>": {"id": "1"}},
        "archived": False,
        "localized": {"firstName": {"localized": {"en_US": "<redacted>"}}},
    }
    request = policy.value(
        {"code": "auth-code", "inputs": [{"id": "x@y.org"}]},
        cli._REQUEST_CREDENTIAL_KEYS | cli._PERSON_KEYS,
    )
    assert request == {"code": "<redacted>", "inputs": [{"id": "<redacted>"}]}


def test_policy_url_headers_and_form() -> None:
    policy = _policy()
    assert policy.url(WEBHOOK_URL, "google") == cli._REDACTED_WEBHOOK_URL
    url = (
        "https://api.linkedin.com/rest/adAccounts/512345678/adCampaigns?q=search"
        "&access_token=zzz&search=(status:(values:List(ACTIVE)))"
    )
    assert policy.url(url, "linkedin") == (
        "https://api.linkedin.com/rest/adAccounts/000000000/adCampaigns?q=search"
        "&access_token=%3Credacted%3E&search=(status:(values:List(ACTIVE)))"
    )
    assert policy.url("https://api.hubapi.com/crm/x", "hubspot") == "https://api.hubapi.com/crm/x"
    headers = policy.headers(
        [
            ("Authorization", "Bearer live-token"),
            ("X-Webhook-Token", WEBHOOK_SECRET),
            ("Host", "example-workspace.app.n8n.cloud"),
            ("Content-Length", "10"),
            ("Linkedin-Version", "202609"),
        ]
    )
    assert headers == {
        "Authorization": "Bearer <redacted>",
        "X-Webhook-Token": "<redacted>",
        "Linkedin-Version": "202609",
    }
    assert policy.headers([("authorization", "opaque")]) == {"authorization": "<redacted>"}
    assert policy.form(
        "grant_type=refresh_token&refresh_token=r1&client_id=c1&client_secret=s"
    ) == (
        "grant_type=refresh_token&refresh_token=%3Credacted%3E&client_id=%3Credacted%3E"
        "&client_secret=%3Credacted%3E"
    )


# Organization ids no setting holds, as LinkedIn's documented samples write them; one has
# four digits, so a bare number equal to it is an ordinary metric.
ORGANIZATION_ID = "2414183"
EMPLOYER_ID = "1035"


def test_policy_zeroes_organization_urn_ids_in_every_string() -> None:
    """Each organization URN id becomes same-length zeros; the prefix stays as written."""
    policy = _policy()
    assert policy.text(f"urn:li:organization:{ORGANIZATION_ID}") == "urn:li:organization:0000000"
    assert (
        policy.text(f"urn:li:organizationBrand:{ORGANIZATION_ID}")
        == "urn:li:organizationBrand:0000000"
    )
    assert policy.text(f"urn:li:company:{EMPLOYER_ID}") == "urn:li:company:0000"
    assert (
        policy.text(f"by urn:li:organization:{ORGANIZATION_ID}, urn:li:company:{EMPLOYER_ID}.")
        == "by urn:li:organization:0000000, urn:li:company:0000."
    )
    body = {
        "reference": f"urn:li:organization:{ORGANIZATION_ID}",
        "beneficiaryReference": {"organization": f"urn:li:organization:{ORGANIZATION_ID}"},
        "targetingCriteria": {
            "exclude": {
                "or": {
                    "urn:li:adTargetingFacet:employers": [
                        f"urn:li:organization:{EMPLOYER_ID}",
                        "urn:li:organization:77001234",
                    ]
                }
            }
        },
        "results": {f"urn:li:organization:{ORGANIZATION_ID}": {"vanityName": "acme"}},
    }
    assert policy.value(body, cli._CREDENTIAL_KEYS | cli._PERSON_KEYS) == {
        "reference": "urn:li:organization:0000000",
        "beneficiaryReference": {"organization": "urn:li:organization:0000000"},
        "targetingCriteria": {
            "exclude": {
                "or": {
                    "urn:li:adTargetingFacet:employers": [
                        "urn:li:organization:0000",
                        "urn:li:organization:00000000",
                    ]
                }
            }
        },
        "results": {"urn:li:organization:0000000": {"vanityName": "acme"}},
    }
    assert policy.headers([("X-Context", f"urn:li:organization:{ORGANIZATION_ID}")]) == {
        "X-Context": "urn:li:organization:0000000"
    }


def test_policy_zeroes_percent_encoded_organization_urn_ids() -> None:
    """Encoded colons, in either hex case and encoded more than once, are matched too."""
    policy = _policy()
    url = (
        "https://api.linkedin.com/rest/posts?q=author"
        f"&author=urn%3Ali%3Aorganization%3A{ORGANIZATION_ID}"
        f"&ids=List(urn%3ali%3acompany%3a{EMPLOYER_ID},"
        f"urn%3Ali%3AorganizationBrand%3a{ORGANIZATION_ID})"
        f"&next=urn%253Ali%253Aorganization%253A{ORGANIZATION_ID}"
    )
    assert policy.url(url, "linkedin") == (
        "https://api.linkedin.com/rest/posts?q=author"
        "&author=urn%3Ali%3Aorganization%3A0000000"
        "&ids=List(urn%3ali%3acompany%3a0000,urn%3Ali%3AorganizationBrand%3a0000000)"
        "&next=urn%253Ali%253Aorganization%253A0000000"
    )
    assert unquote(policy.text(quote(f"urn:li:organization:{ORGANIZATION_ID}", safe=""))) == (
        "urn:li:organization:0000000"
    )


def test_policy_keeps_other_urns_and_bare_numbers() -> None:
    """Only the organization URN context is zeroed; account URNs follow the configured ids."""
    policy = _policy()
    kept = (
        "urn:li:sponsoredCampaign:123456",
        "urn:li:sponsoredCampaignGroup:654321",
        "urn:li:share:6778045555198214144",
        "urn:li:person:Ab3dEf",
        "urn:li:geo:103644278",
        "urn:li:adTargetingFacet:employers",
        "urn:li:organization:",
        ORGANIZATION_ID,
        f"{EMPLOYER_ID} impressions",
    )
    for value in kept:
        assert policy.text(value) == value
    assert policy.text("urn:li:sponsoredAccount:512345678") == "urn:li:sponsoredAccount:000000000"
    assert policy.value(int(ORGANIZATION_ID), frozenset()) == int(ORGANIZATION_ID)
    assert policy.value({"impressions": int(EMPLOYER_ID)}, frozenset()) == {
        "impressions": int(EMPLOYER_ID)
    }


# A configured secret far shorter than any real token: settings accept it, so capture
# redaction must remove it wherever it occurs.
SHORT_SECRET = "Qz7"


def _short_policy(**secrets: str) -> Any:
    """A policy whose only configured credentials are ``secrets``."""
    return cli._RedactionPolicy.from_settings(Settings(**secrets))


def test_policy_redacts_an_embedded_short_secret() -> None:
    policy = _short_policy(n8n_webhook_secret="abc")
    assert policy.text("credential abc denied") == "credential <redacted> denied"
    body = {"message": "credential abc denied", "abc": {"xabcx": 1}, "status": "COMPLETE"}
    redacted = policy.value(body, cli._CREDENTIAL_KEYS | cli._PERSON_KEYS)
    assert redacted == {
        "message": "credential <redacted> denied",
        "<redacted>": {"x<redacted>x": 1},
        "status": "COMPLETE",
    }
    assert "abc" not in json.dumps(redacted)
    assert not policy.holds_secret(redacted)
    assert policy.holds_secret(body)


def test_policy_never_rescans_the_marker_it_wrote() -> None:
    policy = _short_policy(linkedin_access_token="red")
    assert policy.text("red alert: reds") == "<redacted> alert: <redacted>s"
    assert policy.text("<redacted>") == "<<redacted>acted>"
    assert policy.text("credential") == "c<redacted>ential"
    redacted = policy.value({"red": ["red", "redacted"]}, frozenset())
    assert redacted == {"<redacted>": ["<redacted>", "<redacted>acted"]}
    assert not policy.holds_secret(redacted)
    assert not policy.holds_secret(cli._REDACTED_WEBHOOK_URL)
    assert policy.holds_secret("<redacted> red")


def test_policy_redacts_percent_encoded_secrets() -> None:
    policy = _short_policy(hubspot_access_token="a/b+c", linkedin_client_secret="p w")
    assert policy.text("a/b+c") == "<redacted>"
    assert policy.text("token=a%2Fb%2Bc&x=1") == "token=%3Credacted%3E&x=1"
    assert policy.text("s=p%20w&t=p+w") == "s=%3Credacted%3E&t=%3Credacted%3E"
    url = "https://api.hubapi.com/crm/x?after=a%2Fb%2Bc"
    assert policy.url(url, "hubspot") == "https://api.hubapi.com/crm/x?after=%3Credacted%3E"
    assert policy.form("grant_type=a%2Fb%2Bc") == "grant_type=%3Credacted%3E"
    assert policy.holds_secret({"url": url})
    assert not policy.holds_secret({"url": policy.url(url, "hubspot")})


@pytest.mark.parametrize(
    ("secret", "spelling"),
    [
        pytest.param("a/b+c", "a%2fb%2bc", id="lowercase-escapes"),
        pytest.param("abc", "%61%62%63", id="encoded-unreserved"),
        pytest.param("abc", "a%62c", id="mixed"),
        pytest.param("ABC", "%41BC", id="mixed-upper"),
        pytest.param("p w", "p+w", id="plus-for-space"),
        pytest.param("p w", "p%20w", id="escaped-space"),
        pytest.param("pä€", quote("pä€", safe=""), id="utf-8-upper"),
        pytest.param("pä€", quote("pä€", safe="").lower(), id="utf-8-lower"),
        pytest.param("pä€", "p%c3%a4€", id="utf-8-mixed"),
    ],
)
def test_policy_redacts_every_percent_encoded_spelling(secret: str, spelling: str) -> None:
    policy = _short_policy(hubspot_access_token=secret)
    text = f"q=1&token={spelling}&x=2"
    assert policy.text(text) == "q=1&token=%3Credacted%3E&x=2"
    assert policy.text(f"credential {spelling} accepted") == "credential %3Credacted%3E accepted"
    assert policy.text(secret) == "<redacted>"
    document = {"url": text, spelling: [f"{spelling}!"]}
    assert policy.holds_secret(document)
    assert not policy.holds_secret(policy.value(document, frozenset()))


def test_policy_matches_plain_characters_case_sensitively() -> None:
    policy = _short_policy(hubspot_access_token="ABC")
    # Hex digits match in either case; the plain characters, and what escapes decode to, do not.
    assert policy.text("%41BC %41bc abc %61BC") == "%3Credacted%3E %41bc abc %61BC"
    assert not policy.holds_secret("%41bc abc %61BC")


@pytest.mark.parametrize(
    ("secret", "spelling"),
    [
        pytest.param("abc", "%2561%2562%2563", id="double-encoded"),
        pytest.param("abc", "%252561bc", id="triple-encoded"),
        pytest.param("p w", "p%2520w", id="double-encoded-space"),
        pytest.param("p w", "p%252Bw", id="double-encoded-plus"),
        # No cap on the rounds: a fourth or sixth level is decoded too.
        pytest.param("abc", "%25252561%25252562%25252563", id="4-level"),
        pytest.param("abc", "%252525252561%252525252562%252525252563", id="6-level"),
        pytest.param("p w", "p%25252520w", id="4-level-space"),
        pytest.param("p w", "p%252525252520w", id="6-level-space"),
        pytest.param("p w", "p%25252Bw", id="4-level-plus"),
        pytest.param("p w", "p%252525252Bw", id="6-level-plus"),
    ],
)
def test_policy_finds_a_secret_encoded_more_than_once(secret: str, spelling: str) -> None:
    policy = _short_policy(hubspot_access_token=secret)
    # Redaction matches one level of encoding only; the leak check decodes further.
    assert policy.text(spelling) == spelling
    document = {"response": {"status": 200, "body": {"message": f"credential {spelling}"}}}
    assert policy.holds_secret(policy.value(document, frozenset()))
    assert policy.holds_secret({f"x{spelling}": None})
    assert not _short_policy(hubspot_access_token="xyz").holds_secret(document)


def test_policy_decodes_deep_nesting_to_the_end() -> None:
    policy = _short_policy(hubspot_access_token="abc")
    # "x" and "abc" percent-encoded 2,000 times; each round of decoding strips one level.
    harmless, nested = "%78", "%61bc"
    for _level in range(1999):
        harmless, nested = quote(harmless, safe=""), quote(nested, safe="")
    assert harmless == "%" + "25" * 1999 + "78"
    assert (_fully_decoded(harmless), _fully_decoded(nested)) == ("x", "abc")
    # Decoded to its end, a harmless string this deep is found clean.
    assert policy.text(harmless) == harmless
    assert not policy.holds_secret({"message": f"credential {harmless}", harmless: [harmless]})
    # The same depth hiding the secret is found: decoding runs until the text stops changing.
    assert policy.text(nested) == nested
    assert policy.holds_secret({"message": f"credential {nested}"})


def test_policy_blanks_a_number_equal_to_a_digits_only_secret() -> None:
    policy = _short_policy(n8n_webhook_secret="4242", linkedin_ad_account_ids="512345678")
    body = {"pin": 4242, "n": 42, "on": True, "id": 512345678, "note": "x4242y"}
    redacted = policy.value(body, frozenset())
    assert redacted == {"pin": "<redacted>", "n": 42, "on": True, "id": 0, "note": "x<redacted>y"}
    assert not policy.holds_secret(redacted)
    # A number that only contains the digits is kept by redaction, and fails closed.
    assert policy.value(14242, frozenset()) == 14242
    assert policy.holds_secret({"total": 14242})


def test_policy_finds_a_secret_where_redaction_does_not_reach() -> None:
    policy = _short_policy(hubspot_access_token="abc")
    document = {
        "surface": "hubspot.properties",
        "captured_at": "2026-09-01T00:00:00+00:00",
        "request": {"headers": {"Authorization": "Bearer <redacted>"}, "body": None},
        "response": {"status": 200, "body": {"labels": ["<redacted>"]}},
    }
    assert not policy.holds_secret(document)
    assert policy.holds_secret({**document, "captured_at": "abc"})
    assert policy.holds_secret({**document, "request": {"headers": {"X-abc": "1"}}})
    assert not _short_policy().holds_secret({**document, "captured_at": "abc"})


def test_policy_redacts_the_linkedin_client_id_wherever_it_occurs() -> None:
    # A plain setting rather than a secret value, yet a LinkedIn OAuth credential.
    policy = _short_policy(linkedin_client_id=" LIC-xyz ")
    # A blank client id is unset and joins nothing; a set one joins stripped.
    blank = _short_policy(linkedin_client_id="   ")
    assert set(policy.secrets) - set(blank.secrets) == {"LIC-xyz"}
    body = {"message": "client LIC-xyz not allowed", "status": 401}
    redacted = policy.value(body, cli._CREDENTIAL_KEYS | cli._PERSON_KEYS)
    assert redacted == {"message": "client <redacted> not allowed", "status": 401}
    url = "https://api.linkedin.com/rest/apps/LIC-xyz/adAccounts?q=search&app=LIC-xyz"
    assert policy.url(url, "linkedin") == (
        "https://api.linkedin.com/rest/apps/<redacted>/adAccounts?q=search&app=<redacted>"
    )
    document = {
        "request": {"url": policy.url(url, "linkedin"), "headers": {"X-Client": "1"}},
        "response": {"status": 401, "body": redacted},
    }
    assert not policy.holds_secret(document)
    # Header names are kept as sent, so the fail-closed check catches the id in one.
    assert policy.holds_secret({**document, "request": {"headers": {"X-LIC-xyz": "1"}}})
    assert policy.holds_secret(body)
    assert not blank.holds_secret(body)


def test_build_captures_leaves_out_a_capture_that_still_holds_a_secret() -> None:
    policy = _short_policy(hubspot_access_token="abc")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "COMPLETE", "results": []})

    recorder = cli._RecordingTransport(httpx.MockTransport(handler))
    objects = "https://api.hubapi.com/crm/objects/2026-09/contacts/batch/read"
    associations = "https://api.hubapi.com/crm/associations/2026-09/deals/contacts/batch/read"

    async def calls() -> None:
        async with httpx.AsyncClient(transport=recorder) as http:
            auth = {"Authorization": "Bearer abc"}
            # The header name is kept as sent, so the secret in it survives redaction.
            await http.post(objects, json={"inputs": [{"id": "1"}]}, headers={**auth, "X-abc": "1"})
            await http.post(objects, json={"inputs": [{"id": "2"}]}, headers=auth)
            await http.post(associations, json={"inputs": [{"id": "abc"}]}, headers=auth)

    asyncio.run(calls())
    run = cli._CaptureRun(source="hubspot", recorder=recorder)
    documents = cli._build_captures(run, policy)
    # The surface is decided by its first exchange: the later clean one is not taken instead.
    assert set(documents) == {"associations_batch_read"}
    assert run.failures == [
        "objects_batch_read: not written: a configured secret survived redaction"
    ]
    request = documents["associations_batch_read"]["request"]
    headers = {name.lower(): value for name, value in request["headers"].items()}
    assert headers["authorization"] == "Bearer <redacted>"
    assert request["body"] == {"inputs": [{"id": "<redacted>"}]}


def _embed_short_secret(
    inner: httpx.AsyncBaseTransport, spelling: str = SHORT_SECRET
) -> httpx.MockTransport:
    """Answer from ``inner``, with ``spelling`` inside the 2xx properties description."""

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await inner.handle_async_request(request)
        if "/crm/properties/" not in request.url.path or response.status_code != 200:
            return response
        body = json.loads(await response.aread())
        body["description"] = f"Lifecycle stage; credential {spelling} accepted"
        return httpx.Response(response.status_code, json=body)

    return httpx.MockTransport(handler)


def _fully_decoded(text: str) -> str:
    """``text`` percent-decoded until decoding no longer changes it."""
    while (decoded := unquote(text)) != text:
        text = decoded
    return text


def test_capture_writes_no_short_secret(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", SHORT_SECRET)
    get_settings.cache_clear()
    settings = get_settings()
    inner = mock_transport("hubspot", _capture_world(settings), settings=settings)
    _live(monkeypatch, _embed_short_secret(inner))
    code = cli.main(["capture-fixtures", "--source", "hubspot"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    paths = _written(out)
    assert {Path(path).stem for path in paths} == EXPECTED_SURFACES["hubspot"]
    for path in paths:
        assert SHORT_SECRET not in (capture_dir / path).read_text(encoding="utf-8"), path
    captured = capture_dir / "tests" / "contracts" / "hubspot" / "captured"
    properties = json.loads((captured / "properties.json").read_text(encoding="utf-8"))
    assert properties["response"]["body"]["description"] == (
        "Lifecycle stage; credential <redacted> accepted"
    )
    assert SHORT_SECRET not in out + err


def test_capture_fails_closed_when_a_secret_survives_redaction(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", SHORT_SECRET)
    get_settings.cache_clear()
    settings = get_settings()
    _live(monkeypatch, mock_transport("hubspot", _capture_world(settings), settings=settings))
    redact_capture = cli._redact_capture

    def leaky(capture: dict[str, Any], policy: Any, source: Any) -> dict[str, Any]:
        """Redact as usual, then let the secret through where redaction does not reach."""
        document = redact_capture(capture, policy, source)
        if document["surface"] == "hubspot.properties":
            document["captured_at"] = f"{document['captured_at']} {SHORT_SECRET}"
        return document

    monkeypatch.setattr(cli, "_redact_capture", leaky)
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["hubspot"] - {
        "properties"
    }
    assert (
        "capture-fixtures hubspot: properties: not written: a configured secret survived "
        "redaction" in err
    )
    captured = capture_dir / "tests" / "contracts" / "hubspot" / "captured"
    assert not (captured / "properties.json").exists()
    for path in captured.iterdir():
        assert SHORT_SECRET not in path.read_text(encoding="utf-8"), path
    assert SHORT_SECRET not in out + err


@pytest.mark.parametrize(
    ("secret", "spelling"),
    [
        pytest.param("Q/z+7", "Q%2fz%2b7", id="lowercase-escapes"),
        pytest.param(SHORT_SECRET, "%51%7A%37", id="encoded-unreserved"),
        pytest.param(SHORT_SECRET, "Q%7a7", id="mixed"),
    ],
)
def test_capture_writes_no_percent_encoded_short_secret(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secret: str,
    spelling: str,
) -> None:
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", secret)
    get_settings.cache_clear()
    settings = get_settings()
    inner = mock_transport("hubspot", _capture_world(settings), settings=settings)
    _live(monkeypatch, _embed_short_secret(inner, spelling))
    code = cli.main(["capture-fixtures", "--source", "hubspot"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    paths = _written(out)
    assert {Path(path).stem for path in paths} == EXPECTED_SURFACES["hubspot"]
    for path in paths:
        text = (capture_dir / path).read_text(encoding="utf-8")
        assert secret not in _fully_decoded(text), path
    captured = capture_dir / "tests" / "contracts" / "hubspot" / "captured"
    properties = json.loads((captured / "properties.json").read_text(encoding="utf-8"))
    assert properties["response"]["body"]["description"] == (
        "Lifecycle stage; credential %3Credacted%3E accepted"
    )
    assert secret not in _fully_decoded(out + err)


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("%2551%257A%2537", id="double-encoded"),
        pytest.param("%25252551%2525257A%25252537", id="4-level"),
    ],
)
def test_capture_fails_closed_on_a_short_secret_encoded_more_than_once(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spelling: str,
) -> None:
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", SHORT_SECRET)
    get_settings.cache_clear()
    settings = get_settings()
    inner = mock_transport("hubspot", _capture_world(settings), settings=settings)
    # Redaction matches one level of encoding; these spellings decode to the secret only after
    # two and four rounds.
    _live(monkeypatch, _embed_short_secret(inner, spelling))
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["hubspot"] - {
        "properties"
    }
    assert (
        "capture-fixtures hubspot: properties: not written: a configured secret survived "
        "redaction" in err
    )
    captured = capture_dir / "tests" / "contracts" / "hubspot" / "captured"
    assert not (captured / "properties.json").exists()
    for path in captured.iterdir():
        assert SHORT_SECRET not in _fully_decoded(path.read_text(encoding="utf-8")), path
    assert SHORT_SECRET not in _fully_decoded(out + err)


# ---------------------------------------------------------------------------
# capture-fixtures: recording transport
# ---------------------------------------------------------------------------


def test_recording_transport_records_and_replays() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(207, json={"status": "COMPLETE", "results": []}, headers={"X-T": "1"})

    recorder = cli._RecordingTransport(httpx.MockTransport(handler))

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(transport=recorder) as http:
            return await http.post(
                "https://api.hubapi.com/crm/objects/2026-09/contacts/batch/read",
                json={"inputs": [{"id": "1"}]},
                headers={"Authorization": "Bearer t"},
            )

    response = asyncio.run(call())
    assert response.status_code == 207
    assert response.json() == {"status": "COMPLETE", "results": []}
    assert response.headers["x-t"] == "1"
    (exchange,) = recorder.exchanges
    assert exchange.ok
    assert exchange.method == "POST"
    assert exchange.status == 207
    assert json.loads(exchange.body) == {"inputs": [{"id": "1"}]}
    assert exchange.header("AUTHORIZATION") == "Bearer t"
    assert exchange.header("x-missing") is None


# ---------------------------------------------------------------------------
# capture-fixtures: complete captures, with the simulator as the live platform
# ---------------------------------------------------------------------------

EXPECTED_SURFACES = {
    "google": {
        "search",
        "row_customer",
        "row_campaign",
        "row_campaign_daily",
        "row_ad_group",
        "row_keyword_view",
        "row_search_term_view",
        "row_ad_group_criterion",
        "row_conversion_action",
    },
    "linkedin": {
        "access_token",
        "ad_account",
        "ad_campaign_groups",
        "ad_campaigns",
        "creatives",
        "posts",
        "ad_analytics",
        "ad_analytics_statistics",
        "lead_forms",
        "lead_form_responses",
        "lead_form_responses_batch",
    },
    "hubspot": {"properties", "crm_search", "associations_batch_read", "objects_batch_read"},
}


@pytest.fixture
def capture_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with the contracts and config, and live (fake) credentials.

    The capture's request windows end at :func:`FIXED_CLOCK`, the clock of every world
    :func:`_capture_world` builds, so each window selects the same records at any host time.
    """
    monkeypatch.setattr(cli, "_capture_now", FIXED_CLOCK)
    shutil.copytree(CONTRACTS, tmp_path / "tests" / "contracts")
    for stale in (tmp_path / "tests" / "contracts").glob("*/captured/*.json"):
        stale.unlink()
    shutil.copytree(CONFIG, tmp_path / "config")
    monkeypatch.chdir(tmp_path)
    world = SimulatedWorld(42)
    monkeypatch.setenv("DATA_MODE", "auto")
    monkeypatch.setenv("N8N_GOOGLE_ADS_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("N8N_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("GOOGLE_ADS_CUSTOMER_ID", world.google_customer_id)
    monkeypatch.setenv("LINKEDIN_AD_ACCOUNT_IDS", ",".join(world.linkedin_account_ids))
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", LINKEDIN_CLIENT_ID)
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", LINKEDIN_CLIENT_SECRET)
    monkeypatch.setenv("LINKEDIN_REFRESH_TOKEN", LINKEDIN_REFRESH)
    monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", HUBSPOT_TOKEN)
    get_settings.cache_clear()
    return tmp_path


def _capture_world(settings: Settings) -> SimulatedWorld:
    """The simulated platform a capture reads, on the clock ``capture_dir`` gives the capture."""
    return SimulatedWorld.from_settings(settings, clock=FIXED_CLOCK)


@pytest.fixture
def google_calls_24h(
    db: async_sessionmaker[AsyncSession], sync_engine: Engine
) -> Callable[[], int | None]:
    """The shared Google call budget on the emptied test database, and a reader of its count.

    ``db`` empties every table and points the CLI's engine at the test database, so the real
    ``GoogleCallBudget`` meters a Google capture there.

    Returns:
        Reads ``source_connection.calls_24h`` of the ``google`` row: ``None`` while no Google
        call was ever metered (no row).
    """
    del db  # Requested for the emptied, migrated database the CLI connects to.

    def calls_24h() -> int | None:
        with sync_engine.connect() as connection:
            value = connection.execute(
                text("SELECT calls_24h FROM source_connection WHERE source = 'google'")
            ).scalar_one_or_none()
        return None if value is None else int(value)

    return calls_24h


def _live(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    """Make the CLI's network transport answer from ``transport``."""
    monkeypatch.setattr(cli.httpx, "AsyncHTTPTransport", lambda: transport)


def _counted_google(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Serve the CLI's Google calls from the simulator, and list every request that arrives.

    Returns:
        The requests that reached the transport, in arrival order; it grows as the CLI runs.
    """
    settings = get_settings()
    inner = mock_transport("google", _capture_world(settings), settings=settings)
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await inner.handle_async_request(request)

    _live(monkeypatch, httpx.MockTransport(handler))
    return seen


def _google_captured(directory: Path) -> set[str]:
    """The surfaces written under ``tests/contracts/google/captured/`` of ``directory``."""
    captured = directory / "tests" / "contracts" / "google" / "captured"
    return {path.stem for path in captured.glob("*.json")}


def _written(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.endswith(".json")]


@pytest.mark.parametrize("source", ["google", "linkedin", "hubspot"])
def test_capture_writes_every_surface_redacted(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    settings = get_settings()
    world = _capture_world(settings)
    _live(monkeypatch, mock_transport(source, world, settings=settings))
    code = cli.main(["capture-fixtures", "--source", source])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    paths = _written(out)
    assert {Path(path).stem for path in paths} == EXPECTED_SURFACES[source]
    leaks = (
        WEBHOOK_URL,
        WEBHOOK_SECRET,
        HUBSPOT_TOKEN,
        LINKEDIN_REFRESH,
        LINKEDIN_CLIENT_SECRET,
        LINKEDIN_CLIENT_ID,
        world.google_customer_id,
        *world.linkedin_account_ids,
    )
    for path in paths:
        assert path.startswith(f"tests/contracts/{source}/captured/")
        text = (capture_dir / path).read_text(encoding="utf-8")
        assert text.endswith("\n")
        document = json.loads(text)
        assert set(document) == {"surface", "captured_at", "request", "response"}
        assert document["surface"] == f"{source}.{Path(path).stem}"
        assert document["captured_at"].endswith("+00:00")
        assert text == json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        validate_capture(document, root=capture_dir / "tests" / "contracts")
        assert cli.EMAIL_RE.search(text) is None, path
        for secret in leaks:
            assert secret not in text, (path, secret)
        headers = {name.lower(): value for name, value in document["request"]["headers"].items()}
        assert "host" not in headers
        if "authorization" in headers:
            assert headers["authorization"] == "Bearer <redacted>"
        if source == "google":
            assert headers["x-webhook-secret"] == "<redacted>"
            assert document["request"]["url"] == cli._REDACTED_WEBHOOK_URL
    if source == "google":
        # One call per template, each one counted in the shared budget.
        assert google_calls_24h() == len(QUERIES)
        assert "capture_call_budget_unavailable" not in err
    if source == "linkedin":
        captured = capture_dir / "tests" / "contracts" / "linkedin" / "captured"
        token = json.loads((captured / "access_token.json").read_text(encoding="utf-8"))
        assert token["response"]["body"]["access_token"] == "<redacted>"
        assert "refresh_token=%3Credacted%3E" in token["request"]["body"]
        leads = json.loads((captured / "lead_form_responses.json").read_text(encoding="utf-8"))
        answers = [
            answer
            for element in leads["response"]["body"]["elements"]
            for answer in element["formResponse"]["answers"]
        ]
        assert answers
        for answer in answers:
            assert "<redacted>" in json.dumps(answer["answerDetails"])


# Where the live account's organization id was found in LinkedIn captures.
ORGANIZATION_SURFACES = frozenset({"ad_account", "ad_campaign_groups", "ad_campaigns", "posts"})
# Every organization URN id in a capture's JSON text, its colons literal or percent-encoded.
ORGANIZATION_URN_ID = re.compile(
    r"urn(?::|%(?:25)*3[Aa])li(?::|%(?:25)*3[Aa])(?:organization(?:Brand)?|company)"
    r"(?::|%(?:25)*3[Aa])([0-9]+)"
)


def _organization_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the CLI from the simulator, with organization URNs where live LinkedIn has them.

    The simulator authors posts as the (configured, so already zeroed) ad account. This puts
    organization ids no setting holds in the account's ``reference``, each campaign group's
    ``beneficiaryReference``, each campaign's ``associatedEntity`` and employer exclusions,
    and each post's ``author``, as a live account returns them.
    """
    from ads_engine.simulation.payloads import identify_surface

    settings = get_settings()
    inner = mock_transport("linkedin", _capture_world(settings), settings=settings)
    organization = f"urn:li:organization:{ORGANIZATION_ID}"

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await inner.handle_async_request(request)
        surface = identify_surface("linkedin", request)
        if response.status_code != 200 or surface not in ORGANIZATION_SURFACES:
            return response
        body = json.loads(await response.aread())
        if surface == "ad_account":
            body["reference"] = organization
        elif surface == "ad_campaign_groups":
            for element in body["elements"]:
                element["beneficiaryReference"] = {"organization": organization}
        elif surface == "ad_campaigns":
            for element in body["elements"]:
                element["associatedEntity"] = organization
                element["targetingCriteria"] = {
                    "include": {
                        "and": [
                            {"or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:103644278"]}}
                        ]
                    },
                    "exclude": {
                        "or": {
                            "urn:li:adTargetingFacet:employers": [
                                f"urn:li:organization:{EMPLOYER_ID}",
                                "urn:li:organization:77001234",
                            ]
                        }
                    },
                }
        else:
            for post in body["results"].values():
                post["author"] = organization
        return httpx.Response(response.status_code, json=body)

    _live(monkeypatch, httpx.MockTransport(handler))


def test_capture_linkedin_zeroes_every_organization_id(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No organization id survives in a written capture, and each still meets its contract."""
    _organization_live(monkeypatch)
    code = cli.main(["capture-fixtures", "--source", "linkedin"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    documents: dict[str, dict[str, Any]] = {}
    zeroed = 0
    for path in _written(out):
        text = (capture_dir / path).read_text(encoding="utf-8")
        for organization_id in ORGANIZATION_URN_ID.findall(text):
            assert set(organization_id) == {"0"}, (path, organization_id)
            zeroed += 1
        document = json.loads(text)
        validate_capture(document, root=capture_dir / "tests" / "contracts")
        documents[Path(path).stem] = document
    assert set(documents) == EXPECTED_SURFACES["linkedin"]
    organization = "urn:li:organization:0000000"
    assert documents["ad_account"]["response"]["body"]["reference"] == organization
    groups = documents["ad_campaign_groups"]["response"]["body"]["elements"]
    campaigns = documents["ad_campaigns"]["response"]["body"]["elements"]
    posts = documents["posts"]["response"]["body"]["results"]
    assert groups
    assert campaigns
    assert posts
    for group in groups:
        assert group["beneficiaryReference"] == {"organization": organization}
    for campaign in campaigns:
        assert campaign["associatedEntity"] == organization
        targeting = campaign["targetingCriteria"]
        assert targeting["exclude"]["or"]["urn:li:adTargetingFacet:employers"] == [
            "urn:li:organization:0000",
            "urn:li:organization:00000000",
        ]
        locations = targeting["include"]["and"][0]["or"]["urn:li:adTargetingFacet:locations"]
        assert locations == ["urn:li:geo:103644278"]
    for post in posts.values():
        assert post["author"] == organization
    assert zeroed == 1 + len(groups) + 3 * len(campaigns) + len(posts)


# ---------------------------------------------------------------------------
# capture-fixtures: one request per surface on accounts with more pages
# ---------------------------------------------------------------------------

STATISTICS_REJECTION = {
    "status": 400,
    "serviceErrorCode": 100,
    "code": "FIELD_INVALID",
    "message": '"MEMBER_SENIORITY" is not an enum symbol',
}
"""LinkedIn 202609's live answer to ``MEMBER_*`` values in the Statistics Finder ``pivots``."""

LINKEDIN_CURSOR_SURFACES = frozenset({"ad_campaign_groups", "ad_campaigns", "creatives"})
LINKEDIN_LINKED_SURFACES = frozenset(
    {
        "ad_analytics",
        "ad_analytics_statistics",
        "ad_analytics_campaign_delivery",
        "ad_analytics_member_segments",
    }
)
SEGMENT_FALLBACK_SURFACES = frozenset(
    {"ad_analytics_campaign_delivery", "ad_analytics_member_segments"}
)


def _multipage_live(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    *,
    reject_statistics: bool = False,
    seen: list[httpx.Request] | None = None,
) -> dict[str, int]:
    """Serve the CLI from the simulator as an account where every listing has another page.

    Each 2xx response that can continue is given a continuation the simulator would not add:
    a ``metadata.nextPageToken`` on LinkedIn cursor searches, a ``rel=next`` link on
    ``adAnalytics`` responses and ``paging.next.after`` on every HubSpot association result.
    With ``reject_statistics`` the Statistics Finder is answered with LinkedIn's live 400, so
    the segment call takes the fallback. Every request that arrives is appended to ``seen``
    when it is given.

    Returns:
        Requests per contract surface (``payloads.identify_surface``); it grows as the CLI runs.
    """
    from ads_engine.simulation.payloads import identify_surface

    settings = get_settings()
    inner = mock_transport(source, _capture_world(settings), settings=settings)
    counts: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        surface = identify_surface(source, request)
        counts[surface] = counts.get(surface, 0) + 1
        if seen is not None:
            seen.append(request)
        if reject_statistics and surface == "ad_analytics_statistics":
            return httpx.Response(400, json=STATISTICS_REJECTION)
        response = await inner.handle_async_request(request)
        if response.status_code != 200:
            return response
        body = json.loads(await response.aread())
        if surface in LINKEDIN_CURSOR_SURFACES:
            body["metadata"] = {"nextPageToken": "capture-page-2"}
        elif surface in LINKEDIN_LINKED_SURFACES:
            link = {"rel": "next", "href": "/rest/adAnalytics?start=10&count=10", "type": "GET"}
            body["paging"] = {"start": 0, "count": 10, "links": [link]}
        elif surface == "associations_batch_read":
            for result in body.get("results", []):
                result["paging"] = {"next": {"after": "capture-page-2"}}
        return httpx.Response(response.status_code, json=body)

    _live(monkeypatch, httpx.MockTransport(handler))
    return counts


def _captured_documents(directory: Path, source: str) -> dict[str, dict[str, Any]]:
    """Every capture written under ``tests/contracts/{source}/captured/``, by surface."""
    captured = directory / "tests" / "contracts" / source / "captured"
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(captured.glob("*.json"))
    }


def test_capture_linkedin_sends_one_request_per_surface_on_a_multipage_account(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No page token or ``next`` link is followed: every surface costs exactly one request."""
    counts = _multipage_live(monkeypatch, "linkedin")
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert counts == dict.fromkeys(EXPECTED_SURFACES["linkedin"], 1), err[-2000:]
    documents = _captured_documents(capture_dir, "linkedin")
    assert {Path(path).stem for path in _written(out)} == set(documents)
    assert set(documents) == EXPECTED_SURFACES["linkedin"]
    for surface in LINKEDIN_CURSOR_SURFACES:
        body = documents[surface]["response"]["body"]
        assert body["metadata"] == {"nextPageToken": "capture-page-2"}
    for document in documents.values():
        validate_capture(document, root=capture_dir / "tests" / "contracts")


def test_capture_linkedin_segment_fallback_costs_one_request_per_fallback_surface(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After the Statistics Finder's 400: one discovery and one per-campaign request.

    Both fallback exchanges are captured under their own surfaces and satisfy their own
    contracts; the statistics surface, whose only answer was the 400, is not captured. The
    discovery names more than one delivering campaign, so the single per-campaign request
    shows the campaign bound at work.
    """
    counts = _multipage_live(monkeypatch, "linkedin", reject_statistics=True)
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    expected = (EXPECTED_SURFACES["linkedin"] - {"ad_analytics_statistics"}) | (
        SEGMENT_FALLBACK_SURFACES
    )
    assert counts == {**dict.fromkeys(expected, 1), "ad_analytics_statistics": 1}, err[-2000:]
    documents = _captured_documents(capture_dir, "linkedin")
    assert {Path(path).stem for path in _written(out)} == set(documents) == expected
    for document in documents.values():
        validate_capture(document, root=capture_dir / "tests" / "contracts")
    discovery = documents["ad_analytics_campaign_delivery"]
    assert "pivot=CAMPAIGN" in discovery["request"]["url"]
    assert len(discovery["response"]["body"]["elements"]) > 1
    segments = documents["ad_analytics_member_segments"]
    assert "pivot=MEMBER_SENIORITY" in segments["request"]["url"]
    assert "campaigns=List(" in segments["request"]["url"]


def test_capture_hubspot_never_sends_an_association_continuation(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The associations read is the initial call only, though its result carries a cursor.

    ``properties`` is the ``lifecyclestage`` read, then binding verification's own reads: with
    the shipped mapping (MQL and QL in ``stage_label`` mode) and no Google link properties,
    ``lifecyclestage`` again and the ``hs_v2_date_entered_{stage id}`` property of the stage
    each binding matches, four requests in all.
    """
    seen: list[httpx.Request] = []
    counts = _multipage_live(monkeypatch, "hubspot", seen=seen)
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert counts["associations_batch_read"] == 1, err[-2000:]
    assert (counts["crm_search"], counts["objects_batch_read"]) == (1, 1)
    documents = _captured_documents(capture_dir, "hubspot")
    assert {Path(path).stem for path in _written(out)} == set(documents)
    assert set(documents) == EXPECTED_SURFACES["hubspot"]
    associations = documents["associations_batch_read"]
    assert all("after" not in item for item in associations["request"]["body"]["inputs"])
    results = associations["response"]["body"]["results"]
    assert results
    assert all(result["paging"]["next"]["after"] == "capture-page-2" for result in results)
    for document in documents.values():
        validate_capture(document, root=capture_dir / "tests" / "contracts")
    # The stage date each binding needs: the first of its labels among the captured options.
    contact_properties = "/crm/properties/2026-09/contacts/"
    lifecycle = documents["properties"]
    assert httpx.URL(lifecycle["request"]["url"]).path == f"{contact_properties}lifecyclestage"
    options = lifecycle["response"]["body"]["options"]
    stage_ids = {option["label"]: option["value"] for option in options}
    mapping = Thresholds.load(capture_dir / "config").mapping
    stage_dates: list[str] = []
    for binding in (mapping.mql, mapping.ql):
        assert binding.mode == "stage_label"
        matched = [label for label in binding.stage_labels if label in stage_ids]
        assert matched, binding.stage_labels
        stage_dates.append(f"{STAGE_DATE_PROPERTY_PREFIX}{stage_ids[matched[0]]}")
    expected = ["lifecyclestage", "lifecyclestage", *stage_dates]
    properties = [request.url.path for request in seen if "/crm/properties/" in request.url.path]
    assert properties == [f"{contact_properties}{name}" for name in expected]
    # Four, as docs/integrations/hubspot.md states for the shipped mapping.
    assert counts["properties"] == len(expected) == 4


# ---------------------------------------------------------------------------
# capture-fixtures: the Google call budget
# ---------------------------------------------------------------------------

# Nothing listens on port 1, so the connection is refused at once; the URL holds no credential.
UNREACHABLE_DATABASE_URL = "postgresql+psycopg://127.0.0.1:1/unreachable"
BUDGET_UNAVAILABLE = "the shared Google call budget in the database is unavailable"


def test_capture_google_counts_every_call_in_the_shared_budget(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = _counted_google(monkeypatch)
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_OK
    first = len(seen)
    assert first == len(QUERIES)
    assert {(request.method, str(request.url)) for request in seen} == {("POST", WEBHOOK_URL)}
    assert google_calls_24h() == first
    # A second capture counts on top of the first: the window is shared, never reset.
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_OK
    assert len(seen) == 2 * first
    assert google_calls_24h() == len(seen)
    assert "capture_call_budget_unavailable" not in capsys.readouterr().err


@pytest.mark.parametrize("cap", [len(QUERIES), 5])
def test_capture_google_sends_nothing_once_the_window_is_full(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cap: int,
) -> None:
    monkeypatch.setenv("GOOGLE_DAILY_CALL_BUDGET", str(cap))
    get_settings.cache_clear()
    seen = _counted_google(monkeypatch)
    expected = cli.EXIT_OK if cap >= len(QUERIES) else cli.EXIT_FAILURE
    assert cli.main(["capture-fixtures", "--source", "google"]) == expected
    # The first capture fills the window: it sends exactly the calls the cap admits.
    assert len(seen) == cap
    assert google_calls_24h() == cap
    capsys.readouterr()
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert len(seen) == cap
    assert google_calls_24h() == cap
    assert _written(out) == []
    assert f"Google call budget exhausted: {cap} of {cap} calls in the last 24 h" in err
    assert "no surface could be captured" in err


def test_capture_google_with_a_zero_cap_sends_nothing(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _counted_google(monkeypatch)
    monkeypatch.setenv("GOOGLE_DAILY_CALL_BUDGET", "0")
    get_settings.cache_clear()
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert seen == []
    assert _written(out) == []
    assert _google_captured(capture_dir) == set()
    assert "invalid settings: google_daily_call_budget" in err


@pytest.mark.parametrize("failure", ["OperationalError", "ConnectionRefusedError"])
def test_capture_google_is_refused_when_the_budget_cannot_be_read(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    if failure == "OperationalError":
        # A real connection attempt, refused by the server address.
        monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    else:

        async def remaining(_self: object) -> int:
            raise ConnectionRefusedError("connection refused")

        monkeypatch.setattr(budget_guard.GoogleCallBudget, "remaining", remaining)
    get_settings.cache_clear()
    db_session.reset_engine()
    seen = _counted_google(monkeypatch)
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert seen == []
    assert _written(out) == []
    assert _google_captured(capture_dir) == set()
    assert (
        f"ads-engine: capture-fixtures google: {BUDGET_UNAVAILABLE} ({failure}); "
        "no webhook call was sent"
    ) in err
    assert "capture_call_budget_unavailable" in err
    for secret in (WEBHOOK_URL, WEBHOOK_SECRET):
        assert secret not in err


@pytest.mark.parametrize(
    ("failing", "surfaces"),
    [(1, set()), (4, {"search", "row_customer", "row_campaign"})],
)
def test_capture_google_stops_at_a_reservation_the_database_cannot_make(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: int,
    surfaces: set[str],
) -> None:
    real_acquire = budget_guard.GoogleCallBudget.acquire
    reservations: list[int] = []

    async def acquire(self: budget_guard.GoogleCallBudget) -> None:
        reservations.append(len(reservations) + 1)
        if len(reservations) >= failing:
            raise OperationalError("UPDATE source_connection", {}, OSError("connection lost"))
        await real_acquire(self)

    monkeypatch.setattr(budget_guard.GoogleCallBudget, "acquire", acquire)
    seen = _counted_google(monkeypatch)
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    # The failed reservation is the last one attempted, and no request follows it.
    assert reservations == list(range(1, failing + 1))
    assert len(seen) == failing - 1
    assert google_calls_24h() == (failing - 1 if failing > 1 else None)
    query_ids = list(QUERIES)
    assert (
        f"capture-fixtures google: search {query_ids[failing - 1]}: {BUDGET_UNAVAILABLE} "
        "(OperationalError); no further webhook call was sent"
    ) in err
    for query_id in query_ids[failing:]:
        assert (
            f"not captured: search {query_id}: not sent, the shared call budget is unavailable"
        ) in err
    # The calls made before the failure were counted, so their captures are kept.
    assert {Path(path).stem for path in _written(out)} == surfaces
    assert _google_captured(capture_dir) == surfaces
    assert ("no surface could be captured" in err) == (not surfaces)


# ---------------------------------------------------------------------------
# capture-fixtures: Google queries pointed at data that delivered
# ---------------------------------------------------------------------------

YESTERDAY = FIXED_CLOCK().date() - timedelta(days=1)
"""The day every Google capture window ends on."""

GoogleAnswers = dict[str, tuple[Any, list[dict[str, Any]]]]
"""Per query id: the scope it was sent with and the rows the CLI received."""


def _google_answered(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[tuple[ParsedQuery, dict[str, Any]]]:
    """Serve the CLI's Google calls from the simulator, each 200 body edited by ``answer``.

    ``answer`` receives the query id and the simulator's envelope, and edits it in place.

    Returns:
        ``(parsed query, envelope the CLI received)`` per request, in arrival order; it
        grows as the CLI runs.
    """
    settings = get_settings()
    inner = mock_transport("google", _capture_world(settings), settings=settings)
    seen: list[tuple[ParsedQuery, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        parsed = parse_query(json.loads(request.content)["query"])
        response = await inner.handle_async_request(request)
        body = json.loads(await response.aread())
        if answer is not None and response.status_code == 200:
            answer(parsed.query_id, body)
        seen.append((parsed, body))
        return httpx.Response(response.status_code, json=body)

    _live(monkeypatch, httpx.MockTransport(handler))
    return seen


def _keep_rows(body: dict[str, Any], keep: Callable[[dict[str, Any]], bool]) -> None:
    """Keep the envelope rows ``keep`` accepts, omitting ``results`` as Google does when empty."""
    rows = [row for row in body.pop("results", []) if keep(row)]
    if rows:
        body["results"] = rows


def _google_answers(seen: list[tuple[ParsedQuery, dict[str, Any]]]) -> GoogleAnswers:
    """The scope and the received rows of each query, by query id."""
    return {parsed.query_id: (dict(parsed.scope), body.get("results", [])) for parsed, body in seen}


def _impressions_by(rows: list[dict[str, Any]], resource: str) -> dict[int, int]:
    """Impressions summed per ``{resource}.id`` over GoogleAdsRows."""
    totals: dict[int, int] = {}
    for row in rows:
        key = int(row[resource]["id"])
        totals[key] = totals.get(key, 0) + int(row["metrics"]["impressions"])
    return totals


def _latest_delivery(rows: list[dict[str, Any]], campaign_id: int) -> date:
    """The latest ``segments.date`` with impressions of ``campaign_id`` in Q-G3 rows."""
    return max(
        date.fromisoformat(row["segments"]["date"])
        for row in rows
        if int(row["campaign"]["id"]) == campaign_id and int(row["metrics"]["impressions"]) > 0
    )


def _finished_record(err: str) -> dict[str, Any]:
    """The one ``capture_fixtures_finished`` JSON line among the CLI's stderr lines."""
    records = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    (record,) = [record for record in records if record.get("event") == "capture_fixtures_finished"]
    return record


def test_capture_google_reads_where_a_later_search_campaign_delivered(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first campaign and ad group never delivered; a later Search campaign did.

    Q-G3 answers only for the later campaign and Q-G5/Q-G6 never for the first ad group, so
    scoping to the first ids would capture neither. Q-G3 reads every campaign for 30 days,
    Q-G5 every campaign on the later campaign's latest day with impressions, and Q-G6 and
    Q-G7 that campaign's ad group, although another campaign's ad group had more keyword
    impressions that day. Every surface is captured with one call per template.
    """
    world = _capture_world(get_settings())
    campaigns = sorted(world.google_campaigns(world.now()), key=lambda record: record.id)
    ad_groups = sorted(world.google_ad_groups(world.now()), key=lambda record: record.id)
    (later,) = [record for record in campaigns if record.name == "Competitor - Conquest"]
    (later_group,) = [record for record in ad_groups if record.campaign_id == later.id]
    assert later.channel == "SEARCH"
    assert later.id != campaigns[0].id
    assert later_group.id != ad_groups[0].id

    def answer(query_id: str, body: dict[str, Any]) -> None:
        if query_id == "Q-G3":
            _keep_rows(body, lambda row: int(row["campaign"]["id"]) == later.id)
        elif query_id in ("Q-G5", "Q-G6"):
            _keep_rows(body, lambda row: int(row["adGroup"]["id"]) != ad_groups[0].id)

    seen = _google_answered(monkeypatch, answer)
    code = cli.main(["capture-fixtures", "--source", "google"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    assert [parsed.query_id for parsed, _body in seen] == list(QUERIES)
    assert google_calls_24h() == len(QUERIES)
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["google"]
    assert "not captured" not in err
    answers = _google_answers(seen)
    assert answers["Q-G3"][0] == {START: YESTERDAY - timedelta(days=29), END: YESTERDAY}
    day = _latest_delivery(answers["Q-G3"][1], later.id)
    assert answers["Q-G5"][0] == {START: day, END: day}
    keyword_impressions = _impressions_by(answers["Q-G5"][1], "adGroup")
    assert 0 < keyword_impressions[later_group.id] < max(keyword_impressions.values())
    group = {AD_GROUP_LO: later_group.id, AD_GROUP_HI: later_group.id}
    assert answers["Q-G6"][0] == {START: day, END: day, **group}
    assert answers["Q-G7"][0] == group
    assert answers["Q-G6"][1]
    assert answers["Q-G7"][1]
    # Q-G2b still looks up the first Q-G2 campaign.
    assert answers["Q-G2b"][0] == {"ids": (campaigns[0].id,)}


def test_capture_google_takes_keywords_from_a_search_ad_group_when_the_top_campaign_has_none(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Search campaign with the most impressions has no keyword traffic that day.

    Q-G5 still reads every campaign on that campaign's latest day with impressions, so Q-G6
    and Q-G7 read the Search ad group with the most keyword impressions in Q-G5's rows.
    """
    seen = _google_answered(monkeypatch)
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["google"], err
    answers = _google_answers(seen)
    channels = {
        int(row["campaign"]["id"]): row["campaign"]["advertisingChannelType"]
        for row in answers["Q-G2"][1]
    }
    owners = {int(row["adGroup"]["id"]): int(row["campaign"]["id"]) for row in answers["Q-G4"][1]}
    delivery = _impressions_by(answers["Q-G3"][1], "campaign")
    top = max(
        (campaign_id for campaign_id in delivery if channels.get(campaign_id) == "SEARCH"),
        key=lambda campaign_id: delivery[campaign_id],
    )
    day = _latest_delivery(answers["Q-G3"][1], top)
    assert answers["Q-G5"][0] == {START: day, END: day}
    keyword_impressions = _impressions_by(answers["Q-G5"][1], "adGroup")
    assert top not in {owners[ad_group_id] for ad_group_id in keyword_impressions}
    chosen = max(keyword_impressions, key=lambda ad_group_id: keyword_impressions[ad_group_id])
    assert channels[owners[chosen]] == "SEARCH"
    group = {AD_GROUP_LO: chosen, AD_GROUP_HI: chosen}
    assert answers["Q-G6"][0] == {START: day, END: day, **group}
    assert answers["Q-G7"][0] == group


EMPTY_DELIVERY_LINES = (
    "capture-fixtures google: not captured: row_campaign_daily: search Q-G3 returned no rows "
    "(every campaign, the 30 days ending yesterday)",
    "capture-fixtures google: not captured: row_keyword_view: search Q-G5 returned no rows "
    "(the first ad group listed, yesterday)",
    "capture-fixtures google: not captured: row_search_term_view: search Q-G6 returned no "
    "rows (the first ad group listed, yesterday)",
    "capture-fixtures google: not captured: row_ad_group_criterion: search Q-G7 returned no "
    "rows (the first ad group listed)",
)
TOP_CAMPAIGN = "the Search campaign with the most impressions in the last 30 days"
EMPTY_KEYWORD_LINES = (
    "capture-fixtures google: not captured: row_keyword_view: search Q-G5 returned no rows "
    f"(every campaign, the latest day with impressions of {TOP_CAMPAIGN})",
    "capture-fixtures google: not captured: row_search_term_view: search Q-G6 returned no "
    f"rows ({TOP_CAMPAIGN}, its latest day with impressions)",
    "capture-fixtures google: not captured: row_ad_group_criterion: search Q-G7 returned no "
    f"rows (the first ad group listed for {TOP_CAMPAIGN})",
)


@pytest.mark.parametrize(
    ("emptied", "lines"),
    [
        (("Q-G3", "Q-G5", "Q-G6", "Q-G7"), EMPTY_DELIVERY_LINES),
        (("Q-G5", "Q-G6", "Q-G7"), EMPTY_KEYWORD_LINES),
    ],
    ids=["no-delivery", "no-keyword-rows"],
)
def test_capture_google_reports_each_query_that_answered_with_no_rows(
    capture_dir: Path,
    google_calls_24h: Callable[[], int | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    emptied: tuple[str, ...],
    lines: tuple[str, ...],
) -> None:
    """A query that answers with no rows leaves its surface reported, not silently missing.

    Each such surface gets one "not captured" line naming its query and the scope in words,
    never an id, and counts in ``skipped``. Nothing failed, so the capture still exits 0
    after one call per template. Without a delivering campaign the reads keep yesterday and
    the first ad group listed; with one but no keyword rows, Q-G6 reads the delivering
    campaign and Q-G7 its first ad group.
    """
    world = _capture_world(get_settings())

    def answer(query_id: str, body: dict[str, Any]) -> None:
        if query_id in emptied:
            body.pop("results", None)

    seen = _google_answered(monkeypatch, answer)
    code = cli.main(["capture-fixtures", "--source", "google"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    assert [parsed.query_id for parsed, _body in seen] == list(QUERIES)
    assert google_calls_24h() == len(QUERIES)
    missing = {QUERIES[query_id].row_surface for query_id in emptied}
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["google"] - missing
    assert _google_captured(capture_dir) == EXPECTED_SURFACES["google"] - missing
    reported = [line for line in err.splitlines() if "not captured" in line]
    assert reported == [f"ads-engine: {line}" for line in lines]
    record = _finished_record(err)
    assert record["skipped"] == len(lines)
    assert record["failed"] == 0
    assert record["written"] == len(EXPECTED_SURFACES["google"]) - len(missing)
    assert world.google_customer_id not in err
    answers = _google_answers(seen)
    ids = {row["campaign"]["id"] for row in answers["Q-G2"][1]} | {
        row["adGroup"]["id"] for row in answers["Q-G4"][1]
    }
    for line in reported:
        assert not any(value in line for value in ids), line
    first_group = int(answers["Q-G4"][1][0]["adGroup"]["id"])
    if "Q-G3" in emptied:
        group = {AD_GROUP_LO: first_group, AD_GROUP_HI: first_group}
        assert answers["Q-G5"][0] == {START: YESTERDAY, END: YESTERDAY, **group}
        assert answers["Q-G6"][0] == {START: YESTERDAY, END: YESTERDAY, **group}
        assert answers["Q-G7"][0] == group
    else:
        (campaign_id,) = {answers["Q-G6"][0][CAMPAIGN_LO], answers["Q-G6"][0][CAMPAIGN_HI]}
        owners = [
            int(row["adGroup"]["id"])
            for row in answers["Q-G4"][1]
            if int(row["campaign"]["id"]) == campaign_id
        ]
        assert answers["Q-G7"][0] == {AD_GROUP_LO: owners[0], AD_GROUP_HI: owners[0]}
        day = _latest_delivery(answers["Q-G3"][1], campaign_id)
        assert answers["Q-G5"][0] == {START: day, END: day}
        assert answers["Q-G6"][0][START] == answers["Q-G6"][0][END] == day


def test_capture_continues_past_a_forbidden_surface(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = get_settings()
    inner = mock_transport("linkedin", _capture_world(settings), settings=settings)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            return httpx.Response(
                403,
                json={"status": 403, "code": "ACCESS_DENIED", "message": "Not enough permissions"},
            )
        return await inner.handle_async_request(request)

    _live(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert {Path(path).stem for path in _written(out)} == EXPECTED_SURFACES["linkedin"] - {"posts"}
    assert "capture-fixtures linkedin: posts: linkedin posts: ACCESS_DENIED" in err


NO_SPONSORED_LEAD = "lead_form_responses_batch: no sponsored lead in the last 30 days"


def _linkedin_lead_responses_answered(
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[httpx.Response], Awaitable[httpx.Response]],
) -> dict[str, int]:
    """Serve the CLI's LinkedIn calls from the simulator, with ``lead_form_responses`` re-answered.

    ``answer`` receives the simulator's response to the responses listing and returns the one
    the CLI gets.

    Returns:
        Requests per contract surface (``payloads.identify_surface``); it grows as the CLI runs.
    """
    from ads_engine.simulation.payloads import identify_surface

    settings = get_settings()
    inner = mock_transport("linkedin", _capture_world(settings), settings=settings)
    counts: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        surface = identify_surface("linkedin", request)
        counts[surface] = counts.get(surface, 0) + 1
        response = await inner.handle_async_request(request)
        return await answer(response) if surface == "lead_form_responses" else response

    _live(monkeypatch, httpx.MockTransport(handler))
    return counts


def test_capture_linkedin_says_when_the_one_response_read_is_a_test_lead(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A test lead read with ``count=1`` is not re-fetched, and is not called an absent lead.

    The responses listing still costs one request and is captured; no batch request is sent.
    """

    async def as_test_lead(response: httpx.Response) -> httpx.Response:
        body = json.loads(await response.aread())
        for element in body["elements"]:
            element["testLead"] = True
        return httpx.Response(response.status_code, json=body)

    counts = _linkedin_lead_responses_answered(monkeypatch, as_test_lead)
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    expected = EXPECTED_SURFACES["linkedin"] - {"lead_form_responses_batch"}
    assert counts == dict.fromkeys(expected, 1), err[-2000:]
    assert {Path(path).stem for path in _written(out)} == expected
    documents = _captured_documents(capture_dir, "linkedin")
    (element,) = documents["lead_form_responses"]["response"]["body"]["elements"]
    assert element["testLead"] is True
    validate_capture(documents["lead_form_responses"], root=capture_dir / "tests" / "contracts")
    assert (
        "capture-fixtures linkedin: not captured: lead_form_responses_batch: the one lead "
        "response read was a test lead, which is never re-fetched"
    ) in err
    assert NO_SPONSORED_LEAD not in err


def test_capture_linkedin_says_when_no_lead_response_was_read(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A responses listing cut short on its one page fails, and is not called an absent lead.

    The 429 asks for a wait longer than the client sleeps through, so the call ends at once.
    """

    async def throttled(response: httpx.Response) -> httpx.Response:
        await response.aread()
        return httpx.Response(429, json={"status": 429}, headers={"Retry-After": "3600"})

    counts = _linkedin_lead_responses_answered(monkeypatch, throttled)
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    expected = EXPECTED_SURFACES["linkedin"] - {"lead_form_responses_batch"}
    assert counts == dict.fromkeys(expected, 1), err[-2000:]
    assert {Path(path).stem for path in _written(out)} == expected - {"lead_form_responses"}
    assert "capture-fixtures linkedin: lead_form_responses: " in err
    assert (
        "capture-fixtures linkedin: not captured: lead_form_responses_batch: "
        "no lead response was read"
    ) in err
    assert NO_SPONSORED_LEAD not in err


def test_capture_stops_when_the_credentials_are_refused(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LINKEDIN_REFRESH_TOKEN")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", LINKEDIN_STATIC)
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": 401, "code": "REVOKED_ACCESS_TOKEN"})

    _live(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert _written(out) == []
    assert "skipped after the credentials were refused" in err
    assert "no surface could be captured" in err
    assert not list((capture_dir / "tests" / "contracts" / "linkedin" / "captured").glob("*.json"))


def test_capture_stops_when_the_token_exchange_fails(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})

    _live(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["capture-fixtures", "--source", "linkedin"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert "capture-fixtures linkedin: access_token:" in err
    assert LINKEDIN_REFRESH not in err
    assert LINKEDIN_CLIENT_SECRET not in err


def test_capture_reports_a_capture_that_breaks_its_contract(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = get_settings()
    inner = mock_transport("hubspot", _capture_world(settings), settings=settings)

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await inner.handle_async_request(request)
        if "/crm/properties/" not in request.url.path:
            return response
        body = json.loads(await response.aread())
        body["unexpectedField"] = "drift"
        return httpx.Response(response.status_code, json=body)

    _live(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert "properties" in {Path(path).stem for path in _written(out)}
    assert "capture breaks its contract: properties:" in err


def test_capture_hubspot_keeps_the_live_null_association_object_type_ids(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live 2026-09 association types carry ``fromObjectTypeId`` and ``toObjectTypeId`` null.

    The capture records them as sent and still satisfies its contract, so the command exits 0.
    """
    settings = get_settings()
    inner = mock_transport("hubspot", _capture_world(settings), settings=settings)

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await inner.handle_async_request(request)
        if "/crm/associations/" not in request.url.path or not response.is_success:
            return response
        body = json.loads(await response.aread())
        for result in body["results"]:
            for target in result["to"]:
                for spec in target["associationTypes"]:
                    spec.update(fromObjectTypeId=None, toObjectTypeId=None)
        return httpx.Response(response.status_code, json=body)

    _live(monkeypatch, httpx.MockTransport(handler))
    assert cli.main(["capture-fixtures", "--source", "hubspot"]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert "capture breaks its contract" not in err, err[-2000:]
    documents = _captured_documents(capture_dir, "hubspot")
    assert {Path(path).stem for path in _written(out)} == set(documents)
    assert set(documents) == EXPECTED_SURFACES["hubspot"]
    body = documents["associations_batch_read"]["response"]["body"]
    specs = [
        spec
        for result in body["results"]
        for target in result["to"]
        for spec in target["associationTypes"]
    ]
    assert specs
    assert all(spec["fromObjectTypeId"] is None for spec in specs)
    assert all(spec["toObjectTypeId"] is None for spec in specs)
    for document in documents.values():
        validate_capture(document, root=capture_dir / "tests" / "contracts")


# ---------------------------------------------------------------------------
# capture-fixtures: the output directory
# ---------------------------------------------------------------------------


def _captured(capture_dir: Path, source: str) -> Path:
    return capture_dir / "tests" / "contracts" / source / "captured"


def _open_output(capture_dir: Path, source: str) -> Any:
    return cli._CaptureOutput.open(capture_dir / "tests" / "contracts", source)


def _temporaries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".tmp"))


def _counting_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    """A live transport that records every request it receives and answers 500."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    return httpx.MockTransport(handler)


def test_overlapping_writes_to_one_surface_never_fail(capture_dir: Path) -> None:
    captured = _captured(capture_dir, "google")
    writers, rounds = 8, 40
    documents: list[Any] = [
        {"surface": "google.search", "writer": writer, "padding": "x" * 8192}
        for writer in range(writers)
    ]
    start = threading.Barrier(writers, timeout=30)

    def write(writer: int) -> None:
        # One output handle per writer, as separate capture commands would hold.
        output = _open_output(capture_dir, "google")
        try:
            start.wait()
            for _ in range(rounds):
                cli._write_captures(output, {"search": documents[writer]})
        finally:
            output.close()

    with ThreadPoolExecutor(max_workers=writers) as pool:
        for future in [pool.submit(write, writer) for writer in range(writers)]:
            future.result()
    final = captured / "search.json"
    assert not final.is_symlink()
    assert final.read_text(encoding="utf-8") in {canonical_json(document) for document in documents}
    assert _temporaries(captured) == []


@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_a_failed_write_leaves_no_temporary_and_propagates(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    captured = _captured(capture_dir, "google")
    (captured / "search.json").write_text("previous\n", encoding="utf-8")
    output = _open_output(capture_dir, "google")

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(cli.os, step, fail)
    try:
        with pytest.raises(OSError, match=os.strerror(errno.ENOSPC)):
            cli._write_captures(output, {"search": {"surface": "google.search"}})
    finally:
        output.close()
    assert _temporaries(captured) == []
    assert (captured / "search.json").read_text(encoding="utf-8") == "previous\n"


def test_a_symbolic_link_at_the_final_or_former_temporary_name_is_not_followed(
    capture_dir: Path,
) -> None:
    captured = _captured(capture_dir, "google")
    victim = capture_dir / "victim.json"
    victim.write_text("victim\n", encoding="utf-8")
    (captured / "search.json").symlink_to(victim)
    # The fixed temporary name earlier versions wrote through.
    former = captured / ".search.json.tmp"
    former.symlink_to(victim)
    document: Any = {"surface": "google.search", "request": {}, "response": {}}
    output = _open_output(capture_dir, "google")
    try:
        written = cli._write_captures(output, {"search": document})
    finally:
        output.close()
    final = captured / "search.json"
    assert written == [final]
    assert victim.read_text(encoding="utf-8") == "victim\n"
    assert not final.is_symlink()
    assert final.read_text(encoding="utf-8") == canonical_json(document)
    # Someone else's entry is left as it was.
    assert former.is_symlink()


def test_a_symbolic_link_at_the_temporary_name_is_never_written_through(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _captured(capture_dir, "google")
    victim = capture_dir / "victim.json"
    victim.write_text("victim\n", encoding="utf-8")
    output = _open_output(capture_dir, "google")
    monkeypatch.setattr(cli, "token_hex", lambda _nbytes: "0" * 16)
    planted = captured / f".search.json.{'0' * 16}.tmp"
    planted.symlink_to(victim)
    try:
        with pytest.raises(FileExistsError):
            cli._write_captures(output, {"search": {"surface": "google.search"}})
    finally:
        output.close()
    assert victim.read_text(encoding="utf-8") == "victim\n"
    assert planted.is_symlink()
    assert not (captured / "search.json").exists()


@pytest.mark.parametrize(
    ("layout", "reason"),
    [
        ("captured_symlink", "tests/contracts/google/captured is a symbolic link"),
        ("captured_file", "tests/contracts/google/captured is not a directory"),
        ("source_symlink", "tests/contracts/google is a symbolic link"),
    ],
)
def test_capture_refuses_a_redirected_output_directory_before_any_request(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layout: str,
    reason: str,
) -> None:
    source_dir = capture_dir / "tests" / "contracts" / "google"
    outside = capture_dir / "outside"
    if layout == "source_symlink":
        source_dir.rename(outside)
        source_dir.symlink_to(outside, target_is_directory=True)
    else:
        outside.mkdir()
        shutil.rmtree(source_dir / "captured")
        if layout == "captured_symlink":
            (source_dir / "captured").symlink_to(outside, target_is_directory=True)
        else:
            (source_dir / "captured").write_text("", encoding="utf-8")
    before = sorted(outside.rglob("*"))
    calls: list[httpx.Request] = []
    _live(monkeypatch, _counting_transport(calls))
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_FAILURE
    out, err = capsys.readouterr()
    assert out == ""
    assert f"capture-fixtures google: {reason}" in err
    assert err.rstrip().endswith("; nothing was sent")
    assert calls == []
    assert sorted(outside.rglob("*")) == before


@pytest.mark.parametrize(
    ("how", "reason"),
    [
        ("chmod", "tests/contracts/hubspot/captured is not writable by this user (uid "),
        ("probe_denied", "tests/contracts/hubspot/captured is not writable by this user (uid "),
        ("probe_read_only", "tests/contracts/hubspot/captured is on a read-only file system"),
        (
            "mkdir_denied",
            "tests/contracts/hubspot/captured is missing and cannot be created: "
            "tests/contracts/hubspot is not writable by this user (uid ",
        ),
    ],
)
def test_capture_refuses_an_unwritable_output_directory_before_any_request(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    how: str,
    reason: str,
) -> None:
    captured = _captured(capture_dir, "hubspot")
    before = sorted(captured.parent.rglob("*"))

    def denied(*_args: object, **_kwargs: object) -> NoReturn:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    def read_only(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(errno.EROFS, os.strerror(errno.EROFS))

    match how:
        case "chmod":
            if os.geteuid() == 0:
                pytest.skip("root ignores directory permission bits; probe_denied covers it")
            captured.chmod(0o555)
        case "probe_denied":
            monkeypatch.setattr(cli, "_create_temporary", denied)
        case "probe_read_only":
            monkeypatch.setattr(cli, "_create_temporary", read_only)
        case _:
            shutil.rmtree(captured)
            before = sorted(captured.parent.rglob("*"))
            monkeypatch.setattr(cli.os, "mkdir", denied)
    calls: list[httpx.Request] = []
    _live(monkeypatch, _counting_transport(calls))
    try:
        code = cli.main(["capture-fixtures", "--source", "hubspot"])
    finally:
        if captured.is_dir():
            captured.chmod(0o755)
    out, err = capsys.readouterr()
    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert f"capture-fixtures hubspot: {reason}" in err
    if how != "probe_read_only":
        assert "in the backend image the runtime user owns only tests/contracts/*/captured/" in err
    assert err.rstrip().endswith("; nothing was sent")
    assert calls == []
    assert sorted(captured.parent.rglob("*")) == before


def test_capture_creates_a_missing_output_directory(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = _captured(capture_dir, "hubspot")
    shutil.rmtree(captured)
    settings = get_settings()
    _live(monkeypatch, mock_transport("hubspot", _capture_world(settings), settings=settings))
    code = cli.main(["capture-fixtures", "--source", "hubspot"])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_OK, err[-2000:]
    assert captured.is_dir()
    assert not captured.is_symlink()
    expected = sorted(f"{surface}.json" for surface in EXPECTED_SURFACES["hubspot"])
    # Only the captures: no temporary and no write probe is left behind.
    assert sorted(path.name for path in captured.iterdir()) == expected
    prefix = "tests/contracts/hubspot/captured/"
    assert sorted(_written(out)) == [prefix + name for name in expected]
