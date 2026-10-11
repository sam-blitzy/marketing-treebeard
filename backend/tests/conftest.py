"""Root pytest configuration and shared fixtures of the Ads Action Engine backend suite.

This file is loaded before any test module. It provides:

* **An isolated environment.** At import, before any ``ads_engine`` module is imported, the
  process environment is reset to a fixed test baseline: ``DATABASE_URL`` points at the test
  database, ``DATA_MODE`` is ``auto``, the session secret and admin identity are test
  literals, and every source credential the settings could read is removed (in any letter
  case, because the settings read names case-insensitively). ``_isolated_env`` re-applies the
  same baseline before every test, so a credential exported by the host shell can never turn a
  source live. A test that needs other values uses :func:`set_env` or :func:`live_credentials`.
* **A network guard.** ``_no_real_network`` makes every real ``httpx`` transport raise, so a
  test never reaches a real API. ``httpx.MockTransport`` and ``httpx.ASGITransport`` are other
  classes and keep working; the PostgreSQL driver (libpq) is unaffected.
* **The PostgreSQL 18 test database.** ``_database_schema`` resets schema ``public`` of
  ``TEST_DATABASE_URL`` once per session and migrates it with the project's Alembic revision;
  ``db`` truncates every table before each test and hands out the application's own session
  factory. An unreachable database **fails** every test that needs it with a message naming the
  URL (password hidden): no test is ever skipped for lack of a database, so no guarantee and
  no coverage disappears silently.
* **API clients** on the real application through ``httpx.ASGITransport``, logged in as the
  test admin with the CSRF header set.
* **Contracts, transports and scenarios**: contract loading, the simulator's mock transport,
  request recording, contract-checked hand-built responses (AAP 0.11.2: no fixture may carry a
  field its contract does not define), and builders for the scenario catalogue's worlds and
  analysis contexts.
* **Configuration, logs and the CLI**: the repository thresholds, overridden copies written to
  ``tmp_path``, captured JSON log lines, and the ``ads-engine`` CLI run in a subprocess.
* **Coverage gates** (AAP 0.11.2): at session end, ``[tool.ads_engine.coverage_gates]`` in
  ``backend/pyproject.toml`` is enforced through the coverage API, because coverage.py has no
  per-path ``fail_under``. The 80% overall target is decided there too, on the unrounded total:
  pytest-cov's own check still runs, but it compares the total rounded to the report precision.

Test modules run in ``--import-mode=importlib`` with no ``__init__.py`` under ``tests/``, so they
can never import this file. Everything shared is therefore a fixture, and a fixture may return
a class or a function (for example :func:`recording_transport` or :func:`make_world`).

The guarantee this file upholds: a test never reaches a real API and never silently skips for
lack of a database.
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, cast

import httpx
import pytest
import structlog
import yaml
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

if TYPE_CHECKING:
    from coverage import Coverage
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ads_engine.analysis.context import AnalysisContext
    from ads_engine.analysis.thresholds import Thresholds
    from ads_engine.db.models.ops import AppUser
    from ads_engine.settings import Settings, Source
    from ads_engine.simulation.generator import SimulatedWorld
    from ads_engine.simulation.payloads import Contract
    from ads_engine.simulation.scenarios import Scenario
    from ads_engine.simulation.transport import JournalEntry

# ---------------------------------------------------------------------------------------------
# Paths and test constants
# ---------------------------------------------------------------------------------------------

BACKEND_DIR: Final = Path(__file__).resolve().parent.parent
"""``backend/``: the working directory of every test (see :func:`pytest_configure`)."""

REPO_ROOT: Final = BACKEND_DIR.parent
"""The repository root, which holds ``config/``."""

TESTS_DIR: Final = BACKEND_DIR / "tests"
"""``backend/tests/``."""

CONTRACTS_DIR: Final = TESTS_DIR / "contracts"
"""``backend/tests/contracts/``: one contract file per external surface, plus ``captured/``."""

CONFIG_DIR: Final = REPO_ROOT / "config"
"""The repository's ``config/`` with ``thresholds.yaml``, ``content_assets.yaml`` and
``hubspot_mapping.yaml``."""

CONTRACT_SOURCES: Final[tuple[str, ...]] = ("google", "linkedin", "hubspot")
"""The contract directories under :data:`CONTRACTS_DIR`, in display order."""

# Read only here, never by settings.py. The default is the root Makefile's disposable
# postgres:18.6 on TEST_DB_PORT 55432; CI points it at its postgres:18 service on 5432.
TEST_DATABASE_URL: Final = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ads:ads@localhost:55432/ads_engine_test"
)
"""The PostgreSQL 18 database every database-backed test runs against."""

TEST_ADMIN_EMAIL: Final = "admin@example.com"
"""The admin login of the tests: the documented ``APP_ADMIN_EMAIL`` default."""

TEST_ADMIN_PASSWORD: Final = "test-admin-password"
"""The admin password of the tests: an obvious test literal, hashed at runtime."""

TEST_SESSION_SECRET: Final = "test-session-secret-not-for-production"
"""``SESSION_SECRET`` of the tests: an obvious test literal that signs session cookies."""

CSRF_HEADERS: Final[Mapping[str, str]] = MappingProxyType({"X-Requested-With": "ads-engine"})
"""The header every mutating API request must carry (AAP 0.3.2, 0.5.3)."""

SESSION_COOKIE: Final = "ads_engine_session"
"""The name of the signed session cookie the API sets on login."""

SOURCE_CREDENTIAL_VARS: Final[tuple[str, ...]] = (
    "N8N_GOOGLE_ADS_WEBHOOK_URL",
    "N8N_WEBHOOK_SECRET",
    "GOOGLE_ADS_CUSTOMER_ID",
    "LINKEDIN_AD_ACCOUNT_IDS",
    "LINKEDIN_CLIENT_ID",
    "LINKEDIN_CLIENT_SECRET",
    "LINKEDIN_REFRESH_TOKEN",
    "LINKEDIN_ACCESS_TOKEN",
    "HUBSPOT_ACCESS_TOKEN",
    "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY",
    "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY",
)
"""Every variable that makes up a source's credentials or CRM link (AAP 0.6.2)."""

# Variables whose documented defaults the tests rely on. A host may export them (for example
# GOOGLE_DAILY_CALL_BUDGET=50 for a live run), so they are removed rather than left alone.
_DEFAULTED_VARS: Final[tuple[str, ...]] = (
    "N8N_WEBHOOK_SECRET_HEADER",
    "GOOGLE_DAILY_CALL_BUDGET",
    "BACKFILL_DAYS",
    "DISPLAY_TIMEZONE",
    "MOCK_SCENARIO",
)

NETWORK_GUARD_MESSAGE: Final = "real network I/O attempted in a test; inject a MockTransport"
"""The ``RuntimeError`` message of the network guard."""

# The exact program `run_cli` executes: the console script's entry point, in a fresh process.
_CLI_PROGRAM: Final = (
    "import sys; from ads_engine.cli import main; raise SystemExit(main(sys.argv[1:]))"
)

# Fixtures whose tests need the PostgreSQL test database; their items get the `db` marker.
_DB_FIXTURES: Final = frozenset(
    {
        "_database_schema",
        "db",
        "session",
        "sync_engine",
        "admin_user",
        "api_client",
        "authed_client",
    }
)

# Plain PostgreSQL schemes that carry no driver; the tests pin them to psycopg 3 as the
# application does, so `postgresql://…` in TEST_DATABASE_URL never selects psycopg2.
_PLAIN_SCHEMES: Final = frozenset({"postgresql", "postgres"})
_DRIVERNAME: Final = "postgresql+psycopg"

# How long the per-test TRUNCATE waits for a table lock before failing with a clear message
# instead of hanging until the pytest timeout.
_TRUNCATE_LOCK_TIMEOUT: Final = "15s"


def _baseline_environment(password_hash: str | None) -> dict[str, str | None]:
    """Return the test environment baseline: each name mapped to its value, ``None`` = unset.

    Args:
        password_hash: ``APP_ADMIN_PASSWORD_HASH``; ``None`` at import time, before the
            session's argon2id hash of :data:`TEST_ADMIN_PASSWORD` exists.

    Returns:
        The variables to set and, mapped to ``None``, the variables to remove.
    """
    baseline: dict[str, str | None] = {
        "DATABASE_URL": TEST_DATABASE_URL,
        "DATA_MODE": "auto",
        "MOCK_SEED": "42",
        "APP_BASE_URL": "http://localhost:8080",
        "SESSION_SECRET": TEST_SESSION_SECRET,
        "APP_ADMIN_EMAIL": TEST_ADMIN_EMAIL,
        "APP_ADMIN_PASSWORD_HASH": password_hash,
    }
    for name in (*SOURCE_CREDENTIAL_VARS, *_DEFAULTED_VARS):
        baseline[name] = None
    return baseline


def _case_variants(name: str) -> list[str]:
    """Return the environment keys other than ``name`` that equal it ignoring case.

    The settings read names case-insensitively, so ``linkedin_access_token`` exported by a host
    would be read as ``LINKEDIN_ACCESS_TOKEN``; the baseline removes such variants too.
    """
    upper = name.upper()
    return [key for key in os.environ if key.upper() == upper and key != name]


def _apply_to_process(baseline: Mapping[str, str | None]) -> None:
    """Write ``baseline`` into ``os.environ`` directly (import time, before any fixture)."""
    for name, value in baseline.items():
        for variant in _case_variants(name):
            del os.environ[variant]
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _set_variable(monkeypatch: pytest.MonkeyPatch, name: str, value: str | None) -> None:
    """Set (or, for ``None``, remove) one variable for the current test, case variants removed."""
    for variant in _case_variants(name):
        monkeypatch.delenv(variant, raising=False)
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


# The baseline must be in place before `ads_engine.settings` is imported, so no import-time
# reader (and no cached Settings) ever sees a host credential or the host's DATABASE_URL.
_apply_to_process(_baseline_environment(None))

from ads_engine.settings import get_settings  # noqa: E402 - must follow the baseline above

get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--update-fixtures``, read through the :func:`update_fixtures` fixture.

    The snapshots in ``backend/tests/fixtures/`` are rendered by
    ``ads_engine.simulation.payloads.render`` with seed 42; the determinism test compares them
    by default and rewrites them when this flag is given.
    """
    parser.addoption(
        "--update-fixtures",
        action="store_true",
        default=False,
        dest="update_fixtures",
        help="Re-render backend/tests/fixtures snapshots from the simulator instead of comparing",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Run every test from ``backend/``, whatever directory pytest was launched from.

    Every runtime path the product resolves is relative to the working directory:
    ``simulation/payloads.py`` reads ``./tests/contracts``, ``ads-engine migrate`` reads
    ``./alembic.ini`` and ``analysis/thresholds.resolve_config_dir()`` falls back to
    ``../config``. Changing directory here makes ``pytest -c backend/pyproject.toml
    --rootdir backend`` from the repository root behave exactly like ``pytest`` in ``backend/``.
    """
    del config  # The hook's signature is fixed; the directory does not depend on it.
    os.chdir(BACKEND_DIR)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark collected tests by location and by the database fixtures they use.

    ``acceptance`` marks tests under ``tests/acceptance/`` and ``integration`` those under
    ``tests/integration/``; ``db`` marks every test that requests a database fixture, so
    ``-m "not db"`` selects the tests that run without PostgreSQL. ``contract`` is applied by
    the connector and parser modules themselves. Only the four markers registered in
    ``pyproject.toml`` are used (``--strict-markers``).
    """
    del config  # The hook's signature is fixed; marking depends only on the items.
    for item in items:
        if item.path.is_relative_to(TESTS_DIR):
            parts = item.path.relative_to(TESTS_DIR).parts
            top = parts[0] if len(parts) > 1 else ""
            if top in {"acceptance", "integration"} and item.get_closest_marker(top) is None:
                item.add_marker(top)
        fixturenames = cast("tuple[str, ...]", getattr(item, "fixturenames", ()))
        if _DB_FIXTURES.intersection(fixturenames) and item.get_closest_marker("db") is None:
            item.add_marker("db")


def _start_gate_report(config: pytest.Config) -> None:
    """Open the gate report on a fresh line under a ``coverage gates`` separator.

    The hook runs right after the last progress marker (``[100%]``), which ends no line.
    """
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_sep("-", "coverage gates")


def _write_report_line(config: pytest.Config, line: str, *, failed: bool) -> None:
    """Write one line through the terminal reporter (stderr when it is not loaded)."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line, red=failed, bold=failed, green=not failed)
    else:
        sys.stderr.write(f"{line}\n")


def _coverage_gates(pyproject: Path) -> dict[str, float] | str:
    """Read ``[tool.ads_engine.coverage_gates]``: include pattern -> minimum percent.

    Returns:
        The gates, or an error message when the table is missing or malformed.
    """
    with pyproject.open("rb") as handle:
        document = tomllib.load(handle)
    table = document.get("tool", {}).get("ads_engine", {}).get("coverage_gates")
    if not isinstance(table, dict) or not table:
        return f"coverage gates: [tool.ads_engine.coverage_gates] is missing from {pyproject}"
    gates: dict[str, float] = {}
    for pattern, minimum in table.items():
        if isinstance(minimum, bool) or not isinstance(minimum, int | float):
            return f"coverage gates: {pattern!r} must map to a percentage, found {minimum!r}"
        gates[str(pattern)] = float(minimum)
    return gates


def _gate_line(label: str, percent: float, minimum: float, precision: int) -> tuple[str, bool]:
    """Decide one coverage gate on the unrounded percentage and format its report line.

    ``label`` names the gate in the line: its include pattern, or ``overall`` for the total.
    The gate is short when ``percent < minimum``: rounding never lets 89.96% meet a 90% floor.
    The percentage is shown at the report precision, with one more digit at a time only while
    the shown value would sit on the other side of the minimum from the verdict, so a short
    89.96% reads ``89.96%`` (never ``90.0%``) and a passing 90.0% reads ``90.0%``.

    Returns:
        The report line, and whether the gate is short.
    """
    short = percent < minimum
    digits = max(precision, 0)
    shown = f"{percent:.{digits}f}"
    # Terminates: with enough digits the shown text parses back to ``percent`` itself.
    while (float(shown) < minimum) != short:
        digits += 1
        shown = f"{percent:.{digits}f}"
    verdict = " FAILED" if short else ""
    return f"coverage gate {label}: {shown}% (min {minimum:g}){verdict}", short


def _check_gate(
    config: pytest.Config,
    cov: Coverage,
    label: str,
    include: list[str] | None,
    minimum: float,
    precision: int,
) -> bool:
    """Measure one coverage gate, print its line and return whether it is short.

    ``include`` limits the report to the gate's files. ``None`` reports every measured file that
    ``[tool.coverage.run]`` and ``[tool.coverage.report]`` select, which is pytest-cov's total.
    No measured file counts as 0%, and a report that fails counts as a short gate.
    """
    from coverage.exceptions import CoverageException, NoDataError

    try:
        percent = cov.report(include=include, file=io.StringIO())
    except NoDataError:
        percent = 0.0
    except CoverageException as exc:
        _write_report_line(config, f"coverage gate {label}: report failed ({exc})", failed=True)
        return True
    line, short = _gate_line(label, percent, minimum, precision)
    _write_report_line(config, line, failed=short)
    return short


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Enforce the coverage gates of AAP 0.11.2 once pytest-cov has saved its data.

    The per-path gates (``*/ads_engine/analysis/*`` and ``*/ads_engine/connectors/*/parser.py``,
    at least 90% line coverage each) live in ``[tool.ads_engine.coverage_gates]`` of
    ``backend/pyproject.toml``. The overall gate (backend overall at least 80%) takes
    pytest-cov's minimum: ``--cov-fail-under`` when given, else ``[tool.coverage.report]
    fail_under``; a minimum of 0 means no overall gate. pytest-cov's own overall check still
    runs, but it compares the total rounded to the report precision, so a measured 79.96% passes
    it; this hook closes that window. pytest-cov saves and combines its data at the end of
    ``pytest_runtestloop``, before this hook, so the data read here is complete; it is only
    read, never combined or erased.

    Nothing happens when coverage is inactive: pytest-cov is not loaded, ``--no-cov`` was
    given, the run only collected tests, or no data file exists. Otherwise one line per gate is
    printed, the overall gate last, and a short gate turns an otherwise passing run into
    ``TESTS_FAILED``. A pattern that matches no measured file counts as 0%, because the plan
    requires those modules to be measured.

    Each gate is decided on the unrounded percentage, so 89.96% is short of 90% and 79.96% of
    80%. The line shows it at the report precision (``[tool.coverage.report] precision``), with
    extra digits only where rounding would cross the minimum: ``89.96% (min 90) FAILED``, never
    ``90.0%``.
    """
    del exitstatus  # session.exitstatus is the authoritative, writable status.
    config = session.config
    if not config.pluginmanager.hasplugin("_cov") or config.getoption("no_cov", default=False):
        return
    if config.getoption("collectonly", default=False):
        return

    import coverage

    pyproject = BACKEND_DIR / "pyproject.toml"
    # pytest-cov fixes its data file when it starts, before pytest_configure changes directory,
    # so a relative data_file (".coverage" or COVERAGE_FILE) is relative to the launch directory.
    probe = coverage.Coverage(config_file=str(pyproject))
    data_file = Path(str(probe.get_option("run:data_file")))
    if not data_file.is_absolute():
        data_file = config.invocation_params.dir / data_file
    if not data_file.is_file():
        return

    gates = _coverage_gates(pyproject)
    any_short = False
    _start_gate_report(config)
    if isinstance(gates, str):
        _write_report_line(config, gates, failed=True)
        any_short = True
        gates = {}

    cov = coverage.Coverage(data_file=str(data_file), config_file=str(pyproject))
    cov.load()
    precision = int(cast("int", cov.get_option("report:precision")) or 0)
    for pattern, minimum in gates.items():
        short = _check_gate(config, cov, pattern, [pattern], minimum, precision)
        any_short = any_short or short

    # The overall minimum is pytest-cov's: --cov-fail-under when given, else fail_under.
    fail_under = config.getoption("cov_fail_under", default=None)
    if fail_under is None:
        fail_under = cov.get_option("report:fail_under")
    overall_minimum = float(cast("float | None", fail_under) or 0)
    if overall_minimum > 0:
        short = _check_gate(config, cov, "overall", None, overall_minimum, precision)
        any_short = any_short or short
    if any_short and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# ---------------------------------------------------------------------------------------------
# Session options, the anyio backend and the admin password hash
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def update_fixtures(pytestconfig: pytest.Config) -> bool:
    """Whether ``--update-fixtures`` was given: re-render the fixture snapshots, never compare."""
    return bool(pytestconfig.getoption("update_fixtures"))


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run every ``@pytest.mark.anyio`` test and async fixture on asyncio.

    SQLAlchemy's async engine on psycopg 3 and the application itself are asyncio-only, so the
    suite pins the backend instead of inheriting anyio's parametrisation over every installed
    backend. A module may still override this fixture.
    """
    return "asyncio"


@pytest.fixture(scope="session")
def admin_password_hash() -> str:
    """The argon2id hash of :data:`TEST_ADMIN_PASSWORD`, computed once per session.

    It is computed at runtime, never committed (the secret scan reports any argon2 hash), with
    the library defaults the API's hasher uses, so a login against it needs no rehash.
    """
    from argon2 import PasswordHasher

    return PasswordHasher().hash(TEST_ADMIN_PASSWORD)


# ---------------------------------------------------------------------------------------------
# Environment isolation and the network guard (autouse)
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, admin_password_hash: str) -> Iterator[None]:
    """Re-apply the environment baseline for every test and reset the settings cache.

    The baseline is the one written at import, with ``APP_ADMIN_PASSWORD_HASH`` now set to the
    session's hash so ``ensure-admin`` and first-start tests see a valid value. Every change is
    undone by ``monkeypatch`` after the test. A test that needs other values calls
    :func:`set_env` or :func:`live_credentials`.
    """
    for name, value in _baseline_environment(admin_password_hash).items():
        _set_variable(monkeypatch, name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _refuse_sync_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    """Replacement of ``httpx.HTTPTransport.handle_request``: always refuses."""
    raise RuntimeError(NETWORK_GUARD_MESSAGE)


async def _refuse_async_request(
    self: httpx.AsyncHTTPTransport, request: httpx.Request
) -> httpx.Response:
    """Replacement of ``httpx.AsyncHTTPTransport.handle_async_request``: always refuses."""
    raise RuntimeError(NETWORK_GUARD_MESSAGE)


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every real ``httpx`` transport raise ``RuntimeError`` for the current test.

    Every client in the suite must be built on an ``httpx.MockTransport`` (the simulator's, a
    recording or a contract-checked one) or an ``httpx.ASGITransport``; those are separate
    classes and are unaffected. The database driver (libpq) does not use httpx.
    """
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse_sync_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_async_request)


# ---------------------------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------------------------


def _test_database_url() -> URL:
    """Parse :data:`TEST_DATABASE_URL`, pinning a driverless scheme to psycopg 3.

    Raises:
        pytest.fail.Exception: When the text is not a SQLAlchemy URL. The URL is not echoed,
            because it carries the database password.
    """
    url: URL | None
    try:
        url = make_url(TEST_DATABASE_URL)
    except ArgumentError, ValueError:
        url = None
    # Failed outside the handler, so the parse error (which can quote part of the URL) is
    # not chained into the report.
    if url is None:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL", pytrace=False)
    if url.drivername in _PLAIN_SCHEMES:
        url = url.set(drivername=_DRIVERNAME)
    return url


@pytest.fixture(scope="session")
def _database_schema() -> Iterator[Engine]:
    """Reset and migrate schema ``public`` of the test database once per session.

    Steps: build a synchronous psycopg 3 engine (UTC session time zone, 5 s connect timeout);
    probe it with ``SELECT 1``; drop and recreate ``public`` so no earlier run's state leaks;
    run ``alembic upgrade head`` on one shared connection. ``migrations/env.py`` migrates
    through ``Config.attributes["connection"]`` when given (joining the caller's transaction,
    which ``engine.begin()`` commits) and leaves logging alone when ``configure_logger`` is
    false. Requested only by the database fixtures, so a run of pure unit tests never connects.

    Yields:
        The synchronous engine, disposed at session end.

    Raises:
        pytest.fail.Exception: When the database is unreachable. Never a skip: a skipped
            database test would hide its guarantee and its coverage.
    """
    from alembic import command
    from alembic.config import Config

    url = _test_database_url()
    hidden = url.render_as_string(hide_password=True)
    engine = create_engine(url, connect_args={"connect_timeout": 5, "options": "-c TimeZone=UTC"})
    try:
        unreachable: str | None = None
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            unreachable = str(getattr(exc, "orig", None) or exc)
        # Failed outside the handler, so the report shows this message, not the driver chain.
        if unreachable is not None:
            pytest.fail(
                f"Test database unreachable at {hidden}: {unreachable}. "
                "Start it with `make test` or set TEST_DATABASE_URL.",
                pytrace=False,
            )
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        config = Config(str(BACKEND_DIR / "alembic.ini"))
        config.attributes["configure_logger"] = False
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def sync_engine(_database_schema: Engine) -> Engine:
    """The synchronous engine on the migrated test database, for raw SQL assertions.

    For example the foreign-key graph through ``sqlalchemy.inspect``, ``compare_metadata``
    and the no-plaintext-email table scan. Rows are those the current test wrote: tables are
    truncated by :func:`db`, not by this fixture.
    """
    return _database_schema


def _truncate_all_tables(engine: Engine) -> None:
    """Empty every table of the application schema; ``alembic_version`` is kept.

    Raises:
        pytest.fail.Exception: When a table lock cannot be taken within
            :data:`_TRUNCATE_LOCK_TIMEOUT`, which means a connection from an earlier test is
            still open.
    """
    # Importing the models package registers every table on Base.metadata.
    importlib.import_module("ads_engine.db.models")
    from ads_engine.db.base import Base

    quote = engine.dialect.identifier_preparer.quote
    tables = ", ".join(quote(table.name) for table in Base.metadata.sorted_tables)
    failure: str | None = None
    try:
        with engine.begin() as connection:
            connection.execute(text(f"SET LOCAL lock_timeout = '{_TRUNCATE_LOCK_TIMEOUT}'"))
            connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    except SQLAlchemyError as exc:
        failure = str(getattr(exc, "orig", None) or exc)
    if failure is not None:
        pytest.fail(
            f"Could not truncate the test tables ({failure}); a connection from an earlier "
            "test probably still holds a lock.",
            pytrace=False,
        )


@pytest.fixture
async def db(
    anyio_backend: str, _database_schema: Engine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Empty tables and the application's own session factory on the test database.

    Before the test every table is truncated (identities restarted), ``DATABASE_URL`` points at
    :data:`TEST_DATABASE_URL`, and the cached settings and engine are forgotten, so the
    application builds its async engine inside this test's event loop. After the test the
    engine is disposed, which closes every pooled connection and so releases any session-level
    advisory lock (such as the ``analysis`` lock), then forgotten again.

    Truncation rather than a rolled-back outer transaction is required: the code under test
    commits through its own ``session_scope()``, holds advisory locks on dedicated
    connections, claims rows with ``FOR UPDATE SKIP LOCKED`` and runs the CLI in subprocesses.
    Engines are per test because a pooled async connection must never cross event loops.

    Yields:
        ``ads_engine.db.session.get_sessionmaker()``.
    """
    del anyio_backend  # Requested so anyio runs this fixture; the backend is always asyncio.
    from ads_engine.db import session as db_session

    _truncate_all_tables(_database_schema)
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    db_session.reset_engine()
    try:
        yield db_session.get_sessionmaker()
    finally:
        await db_session.dispose_engine()
        db_session.reset_engine()


@pytest.fixture
async def session(db: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One application session on the emptied test database, for ORM setup and assertions.

    The session does not commit by itself: a test commits explicitly what other code must see.
    """
    async with db() as opened:
        yield opened


@pytest.fixture
async def admin_user(session: AsyncSession, admin_password_hash: str) -> AppUser:
    """The committed ``app_user`` row of the test admin (:data:`TEST_ADMIN_EMAIL`)."""
    from ads_engine.db.models import AppUser

    user = AppUser(email=TEST_ADMIN_EMAIL, password_hash=admin_password_hash)
    session.add(user)
    await session.commit()
    return user


# ---------------------------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    """The identity and request conventions of the test admin, for tests that cannot import us.

    Attributes:
        email: :data:`TEST_ADMIN_EMAIL`.
        password: :data:`TEST_ADMIN_PASSWORD`.
        csrf_headers: :data:`CSRF_HEADERS`, read-only.
        session_cookie: The session cookie name, ``ads_engine_session``.
    """

    email: str
    password: str
    csrf_headers: Mapping[str, str]
    session_cookie: str


@pytest.fixture(scope="session")
def test_identity() -> AdminIdentity:
    """The test admin's email and password, the CSRF header and the session cookie name."""
    return AdminIdentity(
        email=TEST_ADMIN_EMAIL,
        password=TEST_ADMIN_PASSWORD,
        csrf_headers=CSRF_HEADERS,
        session_cookie=SESSION_COOKIE,
    )


@pytest.fixture
async def api_app(db: async_sessionmaker[AsyncSession]) -> AsyncIterator[FastAPI]:
    """A fresh application on the emptied test database: the one :func:`api_client` serves.

    A fresh ``create_app()`` per test means a fresh in-memory login rate limiter. The app is
    built after the environment baseline is in place, so it reads the test settings.
    ``create_app()`` configures logging, so it runs with structlog's logger caching switched
    off afterwards (:func:`_without_logger_caching`) and logging is restored to its pre-test
    state when the app is torn down (:func:`_restored_logging`).

    The API reads the time only through ``ads_engine.api.deps.get_now``. A test whose
    assertions depend on the request time fixes it with
    ``api_app.dependency_overrides[get_now] = lambda: instant`` before logging in, so the
    session's issue time, feedback ``until`` and the read-time overlay all follow that clock.
    """
    del db  # Requested for the emptied database and the per-test engine lifecycle.
    from ads_engine.api.app import create_app

    with _restored_logging():
        app = create_app()
        _without_logger_caching()
        yield app


@pytest.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An unauthenticated client on :func:`api_app`, served in process.

    ``ASGITransport`` runs no lifespan; the engine is disposed by :func:`db`. Tests of 401 and
    403 responses use this client directly. A test that fixes the request clock sets
    ``api_app.dependency_overrides[get_now]`` before logging this client in.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://localhost"
    ) as client:
        yield client


type LoginFunction = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def login() -> LoginFunction:
    """Return ``async login(client, email=..., password=...) -> httpx.Response``.

    It posts ``/api/v1/auth/login`` with the CSRF header and fails the test, naming the status
    and the error body, unless the ``ads_engine_session`` cookie is then held by the client.
    The cookie is not Secure because ``APP_BASE_URL`` is ``http://localhost:8080`` (AAP 0.3.2).
    """

    async def _login(
        client: httpx.AsyncClient,
        email: str = TEST_ADMIN_EMAIL,
        password: str = TEST_ADMIN_PASSWORD,
    ) -> httpx.Response:
        """Log ``client`` in and return the login response."""
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers=dict(CSRF_HEADERS),
        )
        if SESSION_COOKIE not in client.cookies:
            pytest.fail(
                f"login did not set the {SESSION_COOKIE} cookie: HTTP {response.status_code} "
                f"{response.text}",
                pytrace=False,
            )
        return response

    return _login


@pytest.fixture
async def authed_client(
    api_client: httpx.AsyncClient, admin_user: AppUser, login: LoginFunction
) -> httpx.AsyncClient:
    """:func:`api_client` logged in as the test admin, with the CSRF header on every request."""
    del admin_user  # Requested so the admin row exists before logging in.
    await login(api_client)
    api_client.headers.update(CSRF_HEADERS)
    return api_client


# ---------------------------------------------------------------------------------------------
# Contracts and transports
# ---------------------------------------------------------------------------------------------

type SyncHandler = Callable[[httpx.Request], httpx.Response]
type AsyncHandler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
type Handler = SyncHandler | AsyncHandler


def _is_async_handler(handler: object) -> bool:
    """Whether ``handler`` (a function or a callable object) is a coroutine function."""
    return inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)  # noqa: B004 - the bound __call__ of a callable object
    )


class RecordingTransport(httpx.MockTransport):
    """An ``httpx.MockTransport`` that records every request it answers, in order.

    For assertions on exact request bodies and headers: the n8n webhook body and secret
    header, the LinkedIn version headers, ``fields=LEAD_FIELDS``. Bodies are read before the
    handler runs, so ``request.content`` is always available afterwards.

    Example::

        transport = recording_transport(lambda request: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as http:
            await http.get("https://api.linkedin.com/rest/adAccounts/1")
        assert transport.last.headers["Linkedin-Version"] == "202609"

    Args:
        handler: The responder, sync or async; or another ``MockTransport`` (such as the
            simulator's), whose handler is then wrapped.
    """

    def __init__(self, handler: Handler | httpx.MockTransport) -> None:
        inner = handler.handler if isinstance(handler, httpx.MockTransport) else handler
        super().__init__(inner)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Record ``request``, then answer it (sync clients)."""
        self.requests.append(request)
        return super().handle_request(request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record ``request``, then answer it (async clients)."""
        self.requests.append(request)
        return await super().handle_async_request(request)

    @property
    def last(self) -> httpx.Request:
        """The most recent request; an ``AssertionError`` when none was sent."""
        if not self.requests:
            raise AssertionError("no request was sent through the recording transport")
        return self.requests[-1]


@pytest.fixture
def recording_transport() -> type[RecordingTransport]:
    """The :class:`RecordingTransport` class: ``recording_transport(handler)`` builds one."""
    return RecordingTransport


@pytest.fixture
def contract() -> Callable[..., Contract]:
    """``ads_engine.simulation.payloads.load_contract``: ``contract(source, name)``.

    Reads ``./tests/contracts/{source}/{name}.json`` (the working directory is ``backend/``).
    """
    from ads_engine.simulation.payloads import load_contract

    return load_contract


@pytest.fixture
def iter_contract_paths() -> Callable[[], Iterator[Path]]:
    """Return a function yielding every contract file, ``captured/`` excluded.

    Files come source by source (google, linkedin, hubspot), sorted by name within each.
    """

    def _iter_contract_paths() -> Iterator[Path]:
        """Yield ``tests/contracts/{google,linkedin,hubspot}/*.json``."""
        for source in CONTRACT_SOURCES:
            yield from sorted((CONTRACTS_DIR / source).glob("*.json"))

    return _iter_contract_paths


_CAPTURE_KEYS: Final = ("surface", "captured_at", "request", "response")


@pytest.fixture
def captured_payloads() -> Callable[[str], list[dict[str, Any]]]:
    """Return ``captured_payloads(source)``: the redacted live captures of a source.

    Each file in ``tests/contracts/{source}/captured/`` written by ``ads-engine
    capture-fixtures`` is one ``{surface, captured_at, request, response}`` object. The list
    is empty while the directory holds only ``.gitkeep``; connector tests run against every
    capture once credentials have produced some (AAP 0.11.2).
    """

    def _captured_payloads(source: str) -> list[dict[str, Any]]:
        """Load every capture of ``source``, sorted by file name."""
        if source not in CONTRACT_SOURCES:
            raise ValueError(f"unknown source {source!r}; expected one of {CONTRACT_SOURCES}")
        captures: list[dict[str, Any]] = []
        for path in sorted((CONTRACTS_DIR / source / "captured").glob("*.json")):
            shown = path.relative_to(BACKEND_DIR)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                pytest.fail(f"{shown}: not valid JSON ({exc.msg})", pytrace=False)
            if not isinstance(raw, dict) or any(key not in raw for key in _CAPTURE_KEYS):
                pytest.fail(
                    f"{shown}: a capture must be an object with keys {', '.join(_CAPTURE_KEYS)}",
                    pytrace=False,
                )
            captures.append(raw)
        return captures

    return _captured_payloads


def _check_request(source: Source, request: httpx.Request) -> Contract:
    """Identify the surface ``request`` addresses and validate it against its contract.

    Raises:
        AssertionError: Naming the surface (or the source, when no surface matches) and every
            violation.
    """
    from ads_engine.simulation.payloads import (
        ContractViolation,
        UnknownSurface,
        identify_surface,
        load_contract,
        validate_request,
    )

    try:
        surface = identify_surface(source, request)
    except UnknownSurface as exc:
        raise AssertionError(f"{source}: the request matches no contract surface ({exc})") from exc
    loaded = load_contract(source, surface)
    try:
        validate_request(loaded, request)
    except ContractViolation as exc:
        raise AssertionError(f"request breaks the {loaded.surface} contract: {exc}") from exc
    return loaded


def _check_response(loaded: Contract, request: httpx.Request, response: httpx.Response) -> None:
    """Validate a hand-built 2xx response against its contract, as the simulator does.

    Error responses (non-2xx) have no body schema in the contracts, and the simulator itself
    validates only success bodies, so they pass; their request was still validated. Google
    ``search`` rows are validated against their ``row_*`` contracts as well.

    Raises:
        AssertionError: Naming the surface and every violation.
    """
    from ads_engine.simulation.payloads import (
        ContractViolation,
        UnknownSurface,
        validate_google_rows,
        validate_response,
    )

    status = response.status_code
    if not 200 <= status < 300:
        return
    try:
        body = response.json() if response.content else None
    except ValueError as exc:
        raise AssertionError(f"response to {loaded.surface} is not JSON ({exc})") from exc
    try:
        validate_response(loaded, body, status=status)
        if loaded.source == "google" and loaded.name == "search":
            query = json.loads(request.content)["query"]
            validate_google_rows(query, body)
    except (ContractViolation, UnknownSurface) as exc:
        raise AssertionError(f"response breaks the {loaded.surface} contract: {exc}") from exc


@pytest.fixture
def contract_checked_transport() -> Callable[[Source, Handler], httpx.MockTransport]:
    """Return ``contract_checked_transport(source, handler) -> httpx.MockTransport``.

    For tests that hand-build responses (for example the 25,000-campaign keyset scan): every
    request is identified (``identify_surface``) and validated (``validate_request``) before the
    handler runs, and every 2xx response is validated (``validate_response``, plus
    ``validate_google_rows`` for Google ``search``) before the client sees it. A violation
    raises ``AssertionError`` naming the surface, so no fixture can carry a field its contract
    does not define (AAP 0.11.2). Error bodies inside HTTP 200 (an n8n ``PERMISSION_DENIED``
    relay) have no success schema: build those with :func:`recording_transport`.

    The handler may be sync or async; a sync handler serves sync and async clients alike.
    """

    def _contract_checked_transport(source: Source, handler: Handler) -> httpx.MockTransport:
        """Wrap ``handler`` in the request and response contract checks of ``source``."""
        if _is_async_handler(handler):
            respond_async = cast("AsyncHandler", handler)

            async def checked_async(request: httpx.Request) -> httpx.Response:
                """Check the request, await the handler, check the response."""
                loaded = _check_request(source, request)
                response = await respond_async(request)
                await response.aread()
                _check_response(loaded, request, response)
                return response

            return httpx.MockTransport(checked_async)

        respond = cast("SyncHandler", handler)

        def checked(request: httpx.Request) -> httpx.Response:
            """Check the request, call the handler, check the response."""
            loaded = _check_request(source, request)
            response = respond(request)
            if not isinstance(response, httpx.Response):
                raise TypeError(
                    f"a synchronous handler must return an httpx.Response, not "
                    f"{type(response).__name__}; declare it with `async def` to await it"
                )
            response.read()
            _check_response(loaded, request, response)
            return response

        return httpx.MockTransport(checked)

    return _contract_checked_transport


# ---------------------------------------------------------------------------------------------
# Scenarios, worlds and analysis contexts
# ---------------------------------------------------------------------------------------------


def _scenario(scenario: str | Scenario) -> Scenario:
    """Return the catalogue entry named ``scenario`` (a ``Scenario`` passes through).

    Raises:
        pytest.fail.Exception: For an unknown name, listing every available name, sorted.
    """
    from ads_engine.simulation.scenarios import SCENARIOS

    if not isinstance(scenario, str):
        return scenario
    try:
        return SCENARIOS[scenario]
    except KeyError:
        pytest.fail(
            f"unknown scenario {scenario!r}; available: {', '.join(sorted(SCENARIOS))}",
            pytrace=False,
        )


type WorldFactory = Callable[..., SimulatedWorld]


@pytest.fixture
def make_world() -> WorldFactory:
    """Return ``make_world(scenario="default", seed=42, *, clock=None) -> SimulatedWorld``.

    Delegates to the catalogue's own builder, ``Scenario.world``, which constructs
    ``SimulatedWorld(seed, scenario)`` with the scenario's fixed clock and white paper
    registry; no scenario logic is repeated here. ``default`` runs on the real clock unless a
    clock is given.
    """

    def _make_world(
        scenario: str | Scenario = "default",
        seed: int = 42,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> SimulatedWorld:
        """Build the seeded world of ``scenario``."""
        return _scenario(scenario).world(seed, clock=clock)

    return _make_world


@pytest.fixture
def make_context() -> Callable[..., AnalysisContext]:
    """Return ``make_context(scenario, *, thresholds=None, seed=42, now=None)``.

    Without ``thresholds`` it is ``Scenario.build_context``: the context the sync jobs and
    ``load_context`` would produce, under the scenario's own configuration (the repository
    files plus the scenario's threshold overrides and registry, which is exactly the
    :func:`thresholds` fixture for every scenario without overrides). With ``thresholds`` the
    same world, clock and database additions are converted by ``context_from_world`` under the
    given configuration instead, for example one from :func:`make_thresholds`.
    """
    from ads_engine.simulation.scenarios import FIXED_NOW, context_from_world

    def _make_context(
        scenario: str | Scenario,
        *,
        thresholds: Thresholds | None = None,
        seed: int = 42,
        now: Any = None,
    ) -> AnalysisContext:
        """Build the analysis context of ``scenario``."""
        entry = _scenario(scenario)
        if thresholds is None:
            # The scenario's own overrides must apply (P5 runs with trusted_min_days 21, for
            # example), so the default is its configuration, not the plain repository files.
            return entry.build_context(seed=seed, now=now)
        instant = now if now is not None else (FIXED_NOW if entry.fixed_time else None)
        clock = None if instant is None else (lambda: instant)
        world = entry.world(seed, clock=clock)
        return context_from_world(world, entry.context, thresholds, now=instant)

    return _make_context


@pytest.fixture
def mock_transport_for(make_world: WorldFactory) -> Callable[..., httpx.MockTransport]:
    """Return ``mock_transport_for(source, world=None, *, strict=False, journal=None)``.

    The simulator's ``httpx.MockTransport`` for one source
    (``ads_engine.simulation.transport.mock_transport``), answering from ``world`` or, when
    none is given, from the ``default`` scenario's world with seed 42. ``strict`` raises on a
    request that breaks its contract instead of answering it with the platform's HTTP 400.
    """
    from ads_engine.simulation.transport import mock_transport

    def _mock_transport_for(
        source: Source,
        world: SimulatedWorld | None = None,
        *,
        strict: bool = False,
        journal: list[JournalEntry] | None = None,
    ) -> httpx.MockTransport:
        """Build the simulator transport of ``source``."""
        served = world if world is not None else make_world()
        return mock_transport(source, served, strict=strict, journal=journal)

    return _mock_transport_for


# ---------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def thresholds() -> Thresholds:
    """The repository configuration, ``Thresholds.load`` of ``config/`` (``../config``)."""
    from ads_engine.analysis.thresholds import Thresholds

    return Thresholds.load(CONFIG_DIR)


def _has_path(tree: object, parts: list[str]) -> bool:
    """Whether the dotted path ``parts`` names an existing key of the parsed YAML ``tree``."""
    node = tree
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _set_path(tree: dict[str, Any], parts: list[str], value: object) -> None:
    """Replace the value at the dotted path ``parts`` of ``tree``, which :func:`_has_path` found."""
    node = tree
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


class ThresholdsFactory:
    """Writes overridden copies of ``config/`` under ``tmp_path`` and loads them.

    ``factory(overrides)`` copies every ``config/*.yaml`` into a fresh directory (``config``,
    then ``config-2``, ...), applies the dotted-key overrides, writes the edited files with
    ``yaml.safe_dump`` and returns ``Thresholds.load`` of that directory. A key goes to the
    file that defines it: ``thresholds.yaml`` first, then ``hubspot_mapping.yaml`` (``mql.*``,
    ``ql.*``, ``crm.booking_sources``, ``crm.meeting_outcome_exclusions``). A key neither file
    defines raises ``ConfigError`` before anything is loaded, exactly as
    ``Thresholds.load(overrides=...)`` rejects a misspelt key, so a typo can never leave a test
    running on the unchanged value. Values pass through ``override_value``, so
    ``Decimal("0.90")`` is written as ``0.9`` and tuples as lists. Load errors
    (``ConfigError``) propagate, for tests such as "a default missing from ``window.presets``
    fails config load".

    Example::

        loaded = make_thresholds({"window.trusted_min_days": 21})
        assert loaded.value("window.trusted_min_days") == 21
        assert (make_thresholds.directory / "thresholds.yaml").is_file()

    Attributes:
        directory: The directory the latest call wrote, set before it was loaded; ``None``
            before the first call.
    """

    def __init__(self, base: Path) -> None:
        self._base = base
        self._count = 0
        self.directory: Path | None = None

    def write(self, overrides: Mapping[str, Any] | None = None) -> Path:
        """Write an overridden copy of ``config/`` and return its directory, without loading.

        For tests that hand the directory to another process, such as the CLI.

        Raises:
            ConfigError: When an override names a key no config file defines.
        """
        from ads_engine.analysis.thresholds import (
            HUBSPOT_MAPPING_FILE,
            THRESHOLDS_FILE,
            ConfigError,
            override_value,
        )

        self._count += 1
        name = "config" if self._count == 1 else f"config-{self._count}"
        directory = self._base / name
        directory.mkdir(parents=True)
        for source in sorted(CONFIG_DIR.glob("*.yaml")):
            shutil.copyfile(source, directory / source.name)
        self.directory = directory
        if not overrides:
            return directory
        editable = (THRESHOLDS_FILE, HUBSPOT_MAPPING_FILE)
        trees: dict[str, dict[str, Any]] = {}
        for file in editable:
            loaded = yaml.safe_load((directory / file).read_text(encoding="utf-8"))
            trees[file] = loaded if isinstance(loaded, dict) else {}
        edited: set[str] = set()
        for key, value in overrides.items():
            parts = key.split(".")
            target = next(
                (candidate for candidate in editable if _has_path(trees[candidate], parts)),
                None,
            )
            if target is None:
                raise ConfigError(THRESHOLDS_FILE, key, "threshold override names a missing key")
            _set_path(trees[target], parts, override_value(value))
            edited.add(target)
        for file in sorted(edited):
            (directory / file).write_text(
                yaml.safe_dump(trees[file], sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        return directory

    def __call__(self, overrides: Mapping[str, Any] | None = None) -> Thresholds:
        """Write an overridden copy of ``config/`` and load it.

        Raises:
            ConfigError: When the resulting configuration fails to load.
        """
        from ads_engine.analysis.thresholds import Thresholds

        return Thresholds.load(self.write(overrides))


@pytest.fixture
def make_thresholds(tmp_path: Path) -> ThresholdsFactory:
    """A :class:`ThresholdsFactory` writing under this test's ``tmp_path``."""
    return ThresholdsFactory(tmp_path)


# ---------------------------------------------------------------------------------------------
# Environment and credentials
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def set_env(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Return ``set_env(**variables)``: set each for this test, ``None`` removes it.

    Case variants of each name are removed as well, and the settings cache is cleared, so the
    next ``get_settings()`` reads the new values. Everything is undone after the test.
    """

    def _set_env(**variables: str | None) -> None:
        """Apply ``variables`` and clear the settings cache."""
        for name, value in variables.items():
            _set_variable(monkeypatch, name, value)
        get_settings.cache_clear()

    return _set_env


# Syntactically valid, obviously fake credentials: the settings accept them, so the source is
# configured live, while every request still goes through a MockTransport (the network guard
# refuses anything else). The LinkedIn account is the simulated world's own reserved id.
_FAKE_CREDENTIALS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "google": MappingProxyType(
            {
                "N8N_GOOGLE_ADS_WEBHOOK_URL": "https://n8n.example.test/webhook/google-ads",
                "N8N_WEBHOOK_SECRET": "test-n8n-secret",
                "GOOGLE_ADS_CUSTOMER_ID": "1234567890",
            }
        ),
        "linkedin": MappingProxyType(
            {
                "LINKEDIN_AD_ACCOUNT_IDS": "990000001",
                "LINKEDIN_ACCESS_TOKEN": "test-linkedin-token",
            }
        ),
        "hubspot": MappingProxyType({"HUBSPOT_ACCESS_TOKEN": "test-hubspot-token"}),
    }
)


@pytest.fixture
def live_credentials(set_env: Callable[..., None]) -> Callable[[Source], Mapping[str, str]]:
    """Return ``live_credentials(source)``: configure fake credentials for one source.

    Afterwards ``get_settings().has_credentials(source)`` is true, so the source resolves live
    under ``DATA_MODE=auto`` (the baseline), while all HTTP still goes through a MockTransport.
    ``DATA_MODE`` is left as it is, so a test that forces ``simulated`` keeps it. Returns the
    variables it set.
    """

    def _live_credentials(source: Source) -> Mapping[str, str]:
        """Set the fake credentials of ``source`` and return them."""
        try:
            values = _FAKE_CREDENTIALS[source]
        except KeyError:
            raise ValueError(
                f"unknown source {source!r}; expected one of {', '.join(_FAKE_CREDENTIALS)}"
            ) from None
        set_env(**values)
        settings = get_settings()
        if not settings.has_credentials(source):
            pytest.fail(
                f"fake {source} credentials are incomplete: missing "
                f"{', '.join(settings.missing_credentials(source))}",
                pytrace=False,
            )
        return values

    return _live_credentials


# ---------------------------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------------------------


@contextmanager
def _restored_logging() -> Iterator[None]:
    """Put back, on exit, the global logging state ``configure_logging`` replaces.

    Saved on entry and restored on exit, whatever happens inside, with the same steps as the
    autouse ``isolated_logging`` fixtures of ``tests/unit/test_logging.py`` and
    ``tests/unit/test_cli.py``: the root handlers and level; the level, handlers and
    propagation of the quiet and routed third-party loggers; the structlog configuration (or
    its defaults, when it was never configured); the active redactor and installed handler,
    and that handler's stream and formatter, which ``configure_logging`` swaps in place; and
    ``logging.lastResort``. The structlog context is cleared, pytest's own root handlers are
    re-attached, and a handler installed inside is removed and closed. Later tests therefore
    log exactly as before, and no root handler is left writing to a stream pytest has closed.
    """
    import ads_engine.logging as ads_logging

    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    named: dict[str, tuple[int, list[logging.Handler], bool]] = {}
    for name in (*ads_logging._QUIET_LOGGERS, *ads_logging._ROUTED_LOGGERS):
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
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        logging.lastResort = saved_last_resort
        if saved_handler is not None and saved_output is not None:
            saved_stream, saved_formatter = saved_output
            try:
                saved_handler.setStream(saved_stream)
            except OSError, ValueError:
                # The stream used meanwhile is closed and cannot be flushed; it is replaced
                # anyway, as configure_logging itself does.
                saved_handler.stream = saved_stream
            saved_handler.setFormatter(saved_formatter)
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


def _without_logger_caching() -> None:
    """Switch structlog's logger caching off, keeping the rest of its configuration.

    ``configure_logging`` turns caching on, so a module-level logger first used under it would
    keep that configuration's processors for the rest of the session and bypass every later
    configuration, including ``structlog.testing.capture_logs`` in other test modules, which
    would then record nothing from it. Uncached, a logger reads the current configuration on
    every call, so a configuration made for one test ends with :func:`_restored_logging`.
    """
    structlog.configure(cache_logger_on_first_use=False)


class CapturedLogs:
    """The JSON log lines written while the fixture is active.

    ``configure_logging`` reconfigures in place (it swaps the stream of the root handler it
    installed and removes every other root handler), so pointing it at this buffer captures
    every structlog event and standard-library record, redacted exactly as in production.
    ``create_app()`` reconfigures logging to stdout; a test that builds an application after
    this fixture calls :meth:`configure` again. Each configuration leaves structlog's logger
    caching off, so no module logger keeps this test's processors after the test.

    Example::

        captured_logs.configure(settings=Settings(n8n_webhook_secret=SecretStr("...")))
        get_logger("t").info("event", header="...")
        assert all("[REDACTED]" in str(line) for line in captured_logs())
    """

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def configure(self, *, settings: Settings | None = None, level: str = "INFO") -> None:
        """(Re)configure logging onto the buffer; ``settings`` name the secrets to redact.

        structlog's logger caching is switched off right after (see
        :func:`_without_logger_caching`), so the configuration ends with the fixture.
        """
        from ads_engine.logging import configure_logging

        configure_logging(stream=self.stream, level=level, settings=settings)
        _without_logger_caching()

    @property
    def text(self) -> str:
        """Everything written so far, raw."""
        return self.stream.getvalue()

    def __call__(self) -> list[dict[str, Any]]:
        """Return every line written so far, parsed.

        Raises:
            AssertionError: When a line is not a JSON object.
        """
        records: list[dict[str, Any]] = []
        for number, line in enumerate(self.text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(f"log line {number} is not JSON: {line!r}") from exc
            if not isinstance(record, dict):
                raise AssertionError(f"log line {number} is not a JSON object: {line!r}")
            records.append(record)
        return records

    def clear(self) -> None:
        """Forget everything written so far."""
        self.stream.seek(0)
        self.stream.truncate()


@pytest.fixture
def captured_logs() -> Iterator[CapturedLogs]:
    """Capture the product's JSON log lines; logging is restored to its pre-test state afterwards.

    Configured with the current (isolated) settings; call ``.configure(settings=...)`` to
    redact other secrets. Used by the redaction checks of the API and connector tests.
    Every configuration runs with structlog's logger caching off, so no module logger first
    used during the test keeps its processors; on teardown :func:`_restored_logging` puts the
    previous handlers, levels, structlog configuration and redactor back, so later tests'
    ``structlog.testing.capture_logs`` and logging fixtures see logging as it was.
    """
    with _restored_logging():
        captured = CapturedLogs()
        captured.configure()
        yield captured


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def run_cli() -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return ``run_cli(*argv, input=None, env=None, timeout=120)``: run ``ads-engine``.

    The CLI runs as ``python -c "...main(sys.argv[1:])"`` in a fresh process in ``backend/``,
    with the test's isolated environment plus ``env`` (a ``None`` value removes a variable).
    A subprocess keeps the CLI's own ``asyncio.run`` out of the test's running event loop and
    gives the real exit code (for example 3 when the analysis lock is held). ``input`` is
    written to stdin (``hash-password``); without it stdin is empty.

    Returns:
        The completed process, with text ``stdout`` and ``stderr``; never raises on a non-zero
        exit code.
    """

    def _run_cli(
        *argv: str,
        input: str | None = None,
        env: Mapping[str, str | None] | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        """Run the CLI with ``argv`` and return the completed process."""
        environment = dict(os.environ)
        for name, value in (env or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        return subprocess.run(  # noqa: S603 - fixed interpreter and program; argv from the test
            [sys.executable, "-c", _CLI_PROGRAM, *argv],
            cwd=BACKEND_DIR,
            env=environment,
            input=input,
            stdin=subprocess.DEVNULL if input is None else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    return _run_cli
