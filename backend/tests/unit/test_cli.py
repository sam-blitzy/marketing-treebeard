"""Tests of ``ads_engine.cli``, the ``ads-engine`` command line, without a database.

AAP 0.5.1 ("Worker entry and CLI"), 0.3.3 ("Live captures"), 0.5.3 (exit code 3 while the
analysis lock is held) and 0.11.2 ("First start"). Covered here:

* the parser: seven commands, ``--help``/``--version`` exit 0, usage errors exit 2;
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
  as the live platform and checked with ``payloads.validate_capture`` and for leaks.

No test opens a network connection: live transports are replaced by the simulator or an
``httpx.MockTransport``, and the Google call budget's database is replaced.
"""

import asyncio
import io
import json
import logging
import shutil
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog
from argon2 import PasswordHasher
from pydantic import BaseModel, ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from ads_engine import cli
from ads_engine import logging as ads_logging
from ads_engine.analysis import runner
from ads_engine.analysis.lock import RunInProgress
from ads_engine.analysis.window import InvalidWindow
from ads_engine.connectors.errors import AccessDenied, CallBudgetExhausted, QueryDefect
from ads_engine.db import session as db_session
from ads_engine.decisions import SubjectRetired
from ads_engine.settings import ConfigurationError, Settings, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.payloads import validate_capture
from ads_engine.simulation.transport import mock_transport
from ads_engine.sync import budget_guard, jobs, mode

BACKEND = Path(__file__).resolve().parents[2]
CONTRACTS = BACKEND / "tests" / "contracts"
CONFIG = BACKEND.parent / "config"

# Fake credentials: long enough to be redacted wherever they occur, and never real.
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
        ("linkedin", "LINKEDIN_AD_ACCOUNT_IDS"),
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
    assert f"live credentials are not configured; set {names}" in capsys.readouterr().err
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
    # A short secret is replaced only where it is the whole string.
    assert policy.text("short") == "<redacted>"
    assert policy.text("shortcut") == "shortcut"
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


# ---------------------------------------------------------------------------
# capture-fixtures: recording transport and local budget
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


def test_local_budget_refuses_past_its_cap() -> None:
    budget = cli._LocalCallBudget(2)

    async def three_calls() -> None:
        await budget.acquire()
        await budget.acquire()
        await budget.acquire()

    with pytest.raises(CallBudgetExhausted, match="local cap of 2 webhook calls"):
        asyncio.run(three_calls())


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
    """A working directory with the contracts and config, and live (fake) credentials."""
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

    async def unreachable(_self: object) -> int:
        raise OperationalError("SELECT 1", {}, OSError("connection refused"))

    monkeypatch.setattr(budget_guard.GoogleCallBudget, "remaining", unreachable)
    return tmp_path


def _live(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    """Make the CLI's network transport answer from ``transport``."""
    monkeypatch.setattr(cli.httpx, "AsyncHTTPTransport", lambda: transport)


def _written(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.endswith(".json")]


@pytest.mark.parametrize("source", ["google", "linkedin", "hubspot"])
def test_capture_writes_every_surface_redacted(
    capture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    settings = get_settings()
    world = SimulatedWorld.from_settings(settings)
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
        assert "capture_call_budget_unrecorded" in err
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


def test_capture_google_uses_the_shared_budget_when_the_database_answers(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    acquired: list[int] = []

    async def remaining(_self: object) -> int:
        return 500

    async def acquire(_self: object) -> None:
        acquired.append(1)

    monkeypatch.setattr(budget_guard.GoogleCallBudget, "remaining", remaining)
    monkeypatch.setattr(budget_guard.GoogleCallBudget, "acquire", acquire)
    settings = get_settings()
    _live(
        monkeypatch,
        mock_transport("google", SimulatedWorld.from_settings(settings), settings=settings),
    )
    assert cli.main(["capture-fixtures", "--source", "google"]) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "capture_call_budget_unrecorded" not in err
    assert len(acquired) == len(EXPECTED_SURFACES["google"])


def test_capture_continues_past_a_forbidden_surface(
    capture_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = get_settings()
    inner = mock_transport("linkedin", SimulatedWorld.from_settings(settings), settings=settings)

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
    inner = mock_transport("hubspot", SimulatedWorld.from_settings(settings), settings=settings)

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
