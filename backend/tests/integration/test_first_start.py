"""First start: the CLI bootstrap, environment defaults, worker startup and the admin's first page.

AAP 0.11.2, "Definition of done — First start": ``make init`` writes ``.env`` from
``.env.example`` (a random ``SESSION_SECRET`` and the ``ads-engine hash-password`` hash); Compose
runs the one-shot ``migrate`` service (``ads-engine migrate``, then ``ads-engine ensure-admin``);
with no source credentials the worker resolves every source to simulated with the reason
``credentials not configured``, builds and backfills the simulated datasets and computes the
default analysis before its first scheduled cycle; the admin then signs in on local HTTP and
sees all five sections populated. ``web/tests/e2e/first-start.spec.ts`` proves the same path in
a browser; this module proves the backend half in process:

* **CLI bootstrap** (AAP 0.5.1 "Worker entry and CLI"): ``hash-password`` prints only the
  argon2id hash of stdin less its trailing newline; ``migrate`` is idempotent at revision
  ``0001``; ``ensure-admin`` creates, then updates, the single admin login and fails clearly,
  naming the variable, without a hash.
* **Environment defaults** (AAP 0.6.2; AAP 0.3.3 "A blank value is read as unset"): blank
  values take the documented defaults and blank secrets count as unset, and ``.env.example``
  lists exactly the variables ``Settings`` reads, with empty secrets and the code defaults.
* **Worker startup with no credentials**: every source simulated with an active dataset (and
  the companion CRM dataset of simulated LinkedIn) whose freshness is recorded; one baseline
  decision per subject in its shape and no discrepancy; the content-asset tables equal to
  ``config/content_assets.yaml``; a succeeded default run before any scheduler tick; a second
  startup that changes nothing; and a ``config_changed`` rerun when the configuration hash
  differs from the latest default run's (AAP 0.3.2).
* **The admin's first page**: ``GET /api/v1/recommendations`` returns the five sections in the
  user's order, each with items, under the banner naming every simulated source; the session
  cookie on the local default base URL is not Secure (AAP 0.3.2).

Every test runs on the real PostgreSQL 18 test database of ``tests/conftest.py`` (truncated per
test by its ``db`` fixture) with no network: conftest removes every source credential, so all
three sources are simulated and every request goes through the simulator's ``MockTransport``.
The worker's ``startup`` is called directly, exactly as ``Scheduler.run_forever`` awaits it once
it holds the scheduler lock, so no scheduler tick and no scheduler-loop sleep runs. Connector
throttling still applies: its HubSpot clients share the process-wide limiter of 4 requests per
second (:data:`~ads_engine.connectors.hubspot.client.SHARED_LIMITER`), which waits in real time
on the simulated calls too and takes most of each startup's duration. ``startup`` accepts no
clock, so the simulated world is generated relative to the real clock; ``BACKFILL_DAYS`` is set
to :data:`BACKFILL_DAYS` to keep each startup to seconds rather than minutes.
"""

from __future__ import annotations

import subprocess
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, get_args

import httpx
import pytest
import structlog
import yaml
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from pydantic import SecretStr
from sqlalchemy import Engine, Select, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.runner import latest_default_run
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.api import app as app_module
from ads_engine.db.models import (
    DATASETS,
    AnalysisRequest,
    AnalysisRun,
    AppUser,
    Budget,
    Campaign,
    ContentAsset,
    ContentAssetCreative,
    ContentAssetForm,
    DatasetFreshness,
    Decision,
    Discrepancy,
    EntitySnapshot,
    SourceConnection,
    SourceDataset,
)
from ads_engine.settings import SOURCES, Settings, get_settings
from ads_engine.sync.mode import REASON_NO_CREDENTIALS
from ads_engine.worker.main import REASON_CONFIG_CHANGED, startup

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type RunCli = Callable[..., subprocess.CompletedProcess[str]]
type SetEnv = Callable[..., None]
type Login = Callable[..., Awaitable[httpx.Response]]


class Identity(Protocol):
    """The shape of conftest's ``test_identity`` fixture: the test admin and request conventions."""

    @property
    def email(self) -> str:
        """The admin's login email."""
        ...

    @property
    def password(self) -> str:
        """The admin's plaintext test password."""
        ...

    @property
    def csrf_headers(self) -> Mapping[str, str]:
        """The header every mutating request must carry."""
        ...

    @property
    def session_cookie(self) -> str:
        """The name of the signed session cookie."""
        ...


# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

BACKEND_DIR: Final = Path(__file__).resolve().parents[2]
"""``backend/``, the working directory of every test and of the CLI subprocess."""

ENV_EXAMPLE: Final = BACKEND_DIR.parent / ".env.example"
"""The root environment template ``make init`` copies to ``.env``."""

CONTENT_ASSETS_FILE: Final = BACKEND_DIR.parent / "config" / "content_assets.yaml"
"""The white paper registry the worker's startup loads into the content-asset tables."""

BACKFILL_DAYS: Final = "35"
"""``BACKFILL_DAYS`` of every startup here: the 28-day default window plus a week of margin, so
the window is covered whichever native day boundary (Google's account zone or UTC) ends it."""

MIGRATION_HEAD: Final = "0001"
"""The single Alembic revision of the schema, ``0001_initial_schema``."""

SECTION_ORDER: Final[tuple[str, ...]] = (
    "keywords",
    "campaigns",
    "budget",
    "creative",
    "white_papers",
)
"""The five sections of ``/actions``, in the user's order (AAP 0.5.3, 0.5.5)."""

SOURCE_LABELS: Final[dict[str, str]] = {
    "google": "Google",
    "linkedin": "LinkedIn",
    "hubspot": "HubSpot",
}
"""How the simulated-sources banner names each source."""

SIMULATED_BANNER: Final = "simulated_sources"
"""The banner code naming every simulated source and its reason (AAP 0.5.4.2 P3)."""

STALE_CONFIG_HASH: Final = "0" * 64
"""A SHA-256-shaped digest no configuration produces, standing for an earlier configuration."""

DOCUMENTED_DEFAULTS: Final[dict[str, object]] = {
    "DATABASE_URL": "postgresql+psycopg://ads:ads@db:5432/ads_engine",
    "APP_BASE_URL": "http://localhost:8080",
    "DISPLAY_TIMEZONE": "UTC",
    "DATA_MODE": "auto",
    "MOCK_SEED": 42,
    "BACKFILL_DAYS": 180,
    "N8N_WEBHOOK_SECRET_HEADER": "X-Webhook-Secret",
    "GOOGLE_DAILY_CALL_BUDGET": 1000,
    "APP_ADMIN_EMAIL": "admin@example.com",
}
"""The non-secret defaults AAP 0.6.2 documents, written out here rather than read from
``Settings`` so a changed code default fails this module."""

# The template's header name is the one deliberate departure from the code defaults. The
# existing n8n Header Auth credential expects X-Webhook-Token and rejects X-Webhook-Secret with
# 403, so `.env.example` ships X-Webhook-Token, while `settings.py` keeps the documented
# X-Webhook-Secret as the default a blank value falls back to. Both values are pinned here so
# neither file can drift silently.
TEMPLATE_VALUES_BEYOND_DEFAULTS: Final[dict[str, str]] = {
    "N8N_WEBHOOK_SECRET_HEADER": "X-Webhook-Token",
}
"""Template values that intentionally differ from the ``Settings`` default of their variable."""

TEMPLATE_SECRETS: Final[frozenset[str]] = frozenset(
    {
        "N8N_WEBHOOK_SECRET",
        "LINKEDIN_CLIENT_SECRET",
        "LINKEDIN_REFRESH_TOKEN",
        "LINKEDIN_ACCESS_TOKEN",
        "HUBSPOT_ACCESS_TOKEN",
        "SESSION_SECRET",
        "APP_ADMIN_PASSWORD_HASH",
    }
)
"""Secrets that must be empty in ``.env.example`` (AAP 0.3.3 "Secrets handling")."""

SOURCE_SETTINGS: Final[tuple[str, ...]] = (
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
"""Every variable that makes up a source's credentials or the Google CRM link (AAP 0.6.2)."""

# hubspot.google_contacts is written only when both Google CRM properties are configured and
# verified (AAP 0.3.3), which a first start without credentials never has.
_UNCONFIGURED_DATASETS: Final[frozenset[str]] = frozenset({"hubspot.google_contacts"})

# ---------------------------------------------------------------------------------------------
# Logging isolation
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _logging_left_as_found(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Leave the process-wide logging configuration, and structlog's logger cache, as found.

    Logging is global to the test session, and no test here asserts on it, so this module
    must not change it for the modules that do:

    * ``create_app()`` calls ``configure_logging()``, which binds the root handler to the
      ``sys.stdout`` of the moment (a pytest capture stream that is later closed) and leaves
      it installed; ``tests/unit/test_cli.py`` and ``test_logging.py`` then fail restoring a
      handler they did not install. The app's call is therefore replaced by a no-op here: the
      API runs unchanged and logs through whatever configuration the session already has.
    * Under ``cache_logger_on_first_use=True``, every module-level logger first used during a
      startup (nearly every sync, simulation and connector logger) would stay bound to the
      processor chain of that moment, so a later ``structlog.testing.capture_logs()``, which
      edits the then-current chain, would see none of its events. Logger caching is off while
      a test here runs, and structlog's configuration is restored after it.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()

    def keep_logging_configuration(**kwargs: Any) -> None:
        """Stand in for ``configure_logging(settings=...)`` inside ``create_app()``."""
        del kwargs  # The session's logging configuration stays as it is.

    monkeypatch.setattr(app_module, "configure_logging", keep_logging_configuration)
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


async def _first_start(set_env: SetEnv) -> Settings:
    """Run the worker's startup once, as on a first ``docker compose up`` with no credentials.

    Returns:
        The settings the startup ran with.
    """
    set_env(BACKFILL_DAYS=BACKFILL_DAYS)
    settings = get_settings()
    configured = [source for source in SOURCES if settings.has_credentials(source)]
    assert configured == [], f"credentials leaked into the test environment for {configured}"
    await startup(settings)
    return settings


async def _all[T](db: Sessions, statement: Select[T]) -> list[T]:
    """Return every row of a single-entity ``statement`` from a fresh session."""
    async with db() as session:
        return list((await session.scalars(statement)).all())


async def _active_primaries(db: Sessions) -> dict[str, SourceDataset]:
    """Return each source's active primary dataset, failing unless there is exactly one."""
    rows = await _all(
        db,
        select(SourceDataset).where(
            SourceDataset.state == "active", SourceDataset.role == "primary"
        ),
    )
    by_source: dict[str, list[SourceDataset]] = defaultdict(list)
    for row in rows:
        by_source[row.source].append(row)
    assert sorted(by_source) == sorted(SOURCES), f"active primaries: {dict(by_source)}"
    for source, found in by_source.items():
        assert len(found) == 1, f"{source} has {len(found)} active primary datasets"
    return {source: found[0] for source, found in by_source.items()}


async def _active_companion(db: Sessions) -> SourceDataset:
    """Return the single active companion CRM dataset."""
    rows = await _all(
        db,
        select(SourceDataset).where(
            SourceDataset.state == "active", SourceDataset.role == "companion"
        ),
    )
    assert len(rows) == 1, f"expected one active companion dataset, found {len(rows)}"
    return rows[0]


def _expected_freshness(source: str) -> set[str]:
    """The freshness rows a dataset of ``source`` records on a first start."""
    return {
        name
        for name in DATASETS
        if name.startswith(f"{source}.") and name not in _UNCONFIGURED_DATASETS
    }


def _plain(value: object) -> object:
    """Unwrap a ``SecretStr`` so a setting can be compared with its documented text."""
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def _template_text(value: object) -> str:
    """Render a ``Settings`` default as it is written in ``.env.example``."""
    plain = _plain(value)
    if plain is None:
        return ""
    if isinstance(plain, list):
        return ",".join(str(item) for item in plain)
    return str(plain)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; comments and blank lines are skipped, keys must not repeat."""
    entries: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"{path.name}:{number} is not a KEY=VALUE line: {raw!r}"
        key = key.strip()
        assert key not in entries, f"{path.name}:{number} repeats {key}"
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        entries[key] = value
    return entries


def _registry() -> dict[str, tuple[str, list[str], list[str]]]:
    """Read ``config/content_assets.yaml`` directly: slug -> (title, forms, overrides), sorted."""
    document = yaml.safe_load(CONTENT_ASSETS_FILE.read_text(encoding="utf-8"))
    registry: dict[str, tuple[str, list[str], list[str]]] = {}
    for asset in document["assets"]:
        registry[asset["slug"]] = (
            asset["title"],
            sorted(asset["lead_forms"]),
            sorted(asset.get("creative_overrides") or []),
        )
    return registry


# ---------------------------------------------------------------------------------------------
# Phase 1: the CLI bootstrap commands of `make init` and the `migrate` service
# ---------------------------------------------------------------------------------------------


def test_hash_password_reads_stdin_prints_only_hash(run_cli: RunCli) -> None:
    """``hash-password`` prints exactly the argon2id hash of stdin less its trailing newline.

    ``make init`` captures stdout into ``APP_ADMIN_PASSWORD_HASH``, so stdout must be the hash
    and nothing else, the newline ``echo`` adds must not be hashed, and the password must
    appear on neither stream.
    """
    password = "correct horse"
    result = run_cli("hash-password", input=f"{password}\n")

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1, f"stdout must be one line, found {len(lines)}"
    assert result.stdout == f"{lines[0]}\n"
    digest = lines[0]
    assert digest.startswith("$argon2id$")
    hasher = PasswordHasher()
    assert hasher.verify(digest, password)
    with pytest.raises(VerifyMismatchError):
        hasher.verify(digest, f"{password}\n")
    assert password not in result.stdout
    assert password not in result.stderr


def test_migrate_is_idempotent(run_cli: RunCli, sync_engine: Engine) -> None:
    """``ads-engine migrate`` succeeds on a database already at head and changes nothing.

    The ``migrate`` service runs on every ``docker compose up``, so a second run must leave
    the schema at revision ``0001`` with the same tables.
    """
    tables_before = set(inspect(sync_engine).get_table_names())

    for attempt in (1, 2):
        result = run_cli("migrate")
        assert result.returncode == 0, f"migrate run {attempt} failed: {result.stderr}"

    with sync_engine.connect() as connection:
        versions = list(
            connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
        )
    assert versions == [MIGRATION_HEAD]
    assert set(inspect(sync_engine).get_table_names()) == tables_before


async def test_ensure_admin_creates_then_updates(
    db: Sessions, run_cli: RunCli, admin_password_hash: str
) -> None:
    """``ensure-admin`` creates the single admin, is idempotent, and updates the hash in place.

    The admin comes from ``APP_ADMIN_EMAIL`` and ``APP_ADMIN_PASSWORD_HASH``; another login
    (an admin whose email was changed) is removed, a rerun keeps the one row, and a new hash
    replaces the old one on the same row.
    """
    async with db() as session:
        session.add(AppUser(email="previous-admin@example.com", password_hash=admin_password_hash))
        await session.commit()

    created = run_cli("ensure-admin")
    assert created.returncode == 0, created.stderr
    users = await _all(db, select(AppUser))
    assert [(user.email, user.password_hash) for user in users] == [
        ("admin@example.com", admin_password_hash)
    ]
    admin_id = users[0].id

    again = run_cli("ensure-admin")
    assert again.returncode == 0, again.stderr
    users = await _all(db, select(AppUser))
    assert [(user.id, user.password_hash) for user in users] == [(admin_id, admin_password_hash)]

    hashed = run_cli("hash-password", input="a different password\n")
    assert hashed.returncode == 0, hashed.stderr
    new_hash = hashed.stdout.strip()
    updated = run_cli("ensure-admin", env={"APP_ADMIN_PASSWORD_HASH": new_hash})
    assert updated.returncode == 0, updated.stderr
    users = await _all(db, select(AppUser))
    assert [(user.id, user.email, user.password_hash) for user in users] == [
        (admin_id, "admin@example.com", new_hash)
    ]
    assert PasswordHasher().verify(users[0].password_hash, "a different password")


@pytest.mark.parametrize("form", ["all-zero", "salt-not-base64", "memory-above-cap"])
async def test_ensure_admin_keeps_the_admin_when_the_hash_cannot_be_verified(
    db: Sessions,
    run_cli: RunCli,
    admin_password_hash: str,
    api_client: httpx.AsyncClient,
    login: Login,
    test_identity: Identity,
    form: str,
) -> None:
    """An argon2 hash that parses but cannot verify fails without touching the stored admin.

    Each value passes ``argon2.extract_parameters``; stored, it would make every sign-in fail
    and lock the single admin out. ``ensure-admin`` exits 1 before its transaction, names the
    problem without echoing the value, and the admin keeps its row and can still sign in. The
    invalid values are built at run time from the session's hash, so no argon2 hash literal
    is committed.
    """
    created = run_cli("ensure-admin")
    assert created.returncode == 0, created.stderr
    before = [(user.id, user.email, user.password_hash) for user in await _all(db, select(AppUser))]
    assert [(email, password_hash) for _id, email, password_hash in before] == [
        ("admin@example.com", admin_password_hash)
    ]

    empty, kind, version, params, salt, digest = admin_password_hash.split("$")
    invalid = {
        "all-zero": "$argon2id$v=19$m=0,t=0,p=0$$",
        "salt-not-base64": "$".join([empty, kind, version, params, "!" * len(salt), digest]),
        "memory-above-cap": "$".join([empty, kind, version, "m=2097153,t=3,p=4", salt, digest]),
    }[form]
    rejected = run_cli("ensure-admin", env={"APP_ADMIN_PASSWORD_HASH": invalid})

    assert rejected.returncode == 1, rejected.stdout
    assert rejected.stdout == ""
    assert "APP_ADMIN_PASSWORD_HASH" in rejected.stderr
    assert "`ads-engine hash-password`" in rejected.stderr
    assert invalid not in rejected.stderr
    for part in (params, salt, digest, "m=0,t=0,p=0", "m=2097153"):
        assert part not in rejected.stderr
    after = [(user.id, user.email, user.password_hash) for user in await _all(db, select(AppUser))]
    assert after == before
    assert PasswordHasher().verify(after[0][2], test_identity.password)
    signed_in = await login(api_client, email=test_identity.email, password=test_identity.password)
    assert signed_in.status_code == 200, signed_in.text


@pytest.mark.parametrize("value", ["", "   ", None], ids=["empty", "whitespace", "unset"])
async def test_ensure_admin_without_hash_fails_clearly(
    db: Sessions, run_cli: RunCli, value: str | None
) -> None:
    """Without ``APP_ADMIN_PASSWORD_HASH``, ``ensure-admin`` exits 1 naming the variable.

    A blank value is read as unset (AAP 0.3.3), so it fails like a missing one, and no login
    is created that nobody could sign in with.
    """
    result = run_cli("ensure-admin", env={"APP_ADMIN_PASSWORD_HASH": value})

    assert result.returncode == 1, result.stdout
    assert "APP_ADMIN_PASSWORD_HASH" in result.stderr
    assert result.stdout == ""
    assert await _all(db, select(AppUser)) == []


# ---------------------------------------------------------------------------------------------
# Phase 2: environment defaults and the `.env.example` template
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", " \t "], ids=["empty", "whitespace"])
def test_blank_values_fall_back_to_defaults(set_env: SetEnv, blank: str) -> None:
    """Every blank variable reads as unset: its documented default applies, a secret is unset.

    ``make init`` copies the template's empty lines into ``.env``, so a first start must load
    with them: typed settings take their AAP 0.6.2 defaults instead of failing to parse, and
    no source counts as having credentials.
    """
    secrets = (*SOURCE_SETTINGS, "APP_ADMIN_PASSWORD_HASH", "SESSION_SECRET")
    set_env(**dict.fromkeys((*DOCUMENTED_DEFAULTS, *secrets), blank))

    settings = get_settings()

    for name, expected in DOCUMENTED_DEFAULTS.items():
        assert _plain(getattr(settings, name.lower())) == expected, name
    for name in secrets:
        value = getattr(settings, name.lower())
        assert value in (None, []), f"{name} must be unset when blank, found a value"
    for source in SOURCES:
        assert not settings.has_credentials(source)
        assert settings.missing_credentials(source), source
    assert not settings.google_crm_link_configured
    assert not settings.secure_cookies


def test_env_example_matches_settings(set_env: SetEnv) -> None:
    """``.env.example`` lists exactly the variables ``Settings`` reads, with safe values.

    Every key but the ``POSTGRES_*`` values of the ``db`` service is a ``Settings`` field and
    every field is listed; each non-empty value is the code default (except the pinned n8n
    header name); every secret is empty; ``DATABASE_URL`` matches the ``POSTGRES_*`` values;
    and the template alone loads into settings with no source credentials, which is the first
    start of AAP 0.11.2.
    """
    entries = _parse_env_file(ENV_EXAMPLE)
    postgres = {key: value for key, value in entries.items() if key.startswith("POSTGRES_")}
    template = {key: value for key, value in entries.items() if key not in postgres}
    fields = Settings.model_fields
    env_names = {name.upper() for name in fields}

    assert sorted(template.keys() - env_names) == [], "template keys Settings does not read"
    assert sorted(env_names - template.keys()) == [], "Settings fields missing from the template"
    assert sorted(postgres) == ["POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER"]

    for key, value in template.items():
        if not value:
            continue
        default = _template_text(fields[key.lower()].get_default(call_default_factory=True))
        expected = TEMPLATE_VALUES_BEYOND_DEFAULTS.get(key, default)
        assert value == expected, f"{key}: template {value!r}, expected {expected!r}"
    for key in TEMPLATE_VALUES_BEYOND_DEFAULTS:
        default = fields[key.lower()].get_default(call_default_factory=True)
        assert default == DOCUMENTED_DEFAULTS[key], f"{key}: code default changed"

    # The secrets with no default; DATABASE_URL is a secret too, but its default is local only.
    unset_secrets = {
        name.upper()
        for name, field in fields.items()
        if (field.annotation is SecretStr or SecretStr in get_args(field.annotation))
        and field.get_default(call_default_factory=True) is None
    }
    assert unset_secrets == TEMPLATE_SECRETS
    for key in sorted(TEMPLATE_SECRETS):
        assert template[key] == "", f"{key} must be empty in the template"

    assert template["DATABASE_URL"] == (
        f"postgresql+psycopg://{postgres['POSTGRES_USER']}:{postgres['POSTGRES_PASSWORD']}"
        f"@db:5432/{postgres['POSTGRES_DB']}"
    )

    set_env(**template)
    loaded = get_settings()
    assert loaded.data_mode == "auto"
    assert (
        loaded.n8n_webhook_secret_header
        == TEMPLATE_VALUES_BEYOND_DEFAULTS["N8N_WEBHOOK_SECRET_HEADER"]
    )
    assert [source for source in SOURCES if loaded.has_credentials(source)] == []
    assert loaded.session_secret is None
    assert loaded.app_admin_password_hash is None


# ---------------------------------------------------------------------------------------------
# Phase 3: the worker's first start with no source credentials
# ---------------------------------------------------------------------------------------------


async def test_worker_startup_simulates_every_source(db: Sessions, set_env: SetEnv) -> None:
    """With no credentials, startup leaves every source simulated with a backfilled dataset.

    For Google, LinkedIn and HubSpot: ``source_connection`` is simulated with the reason
    ``credentials not configured`` and points at the source's single active primary dataset,
    itself simulated with that reason; simulated LinkedIn has its active companion CRM
    dataset; no build is left behind; and every freshness row of the active datasets has
    been recorded (none ``unknown``).
    """
    await _first_start(set_env)

    primaries = await _active_primaries(db)
    connections = {row.source: row for row in await _all(db, select(SourceConnection))}
    assert sorted(connections) == sorted(SOURCES)
    for source in SOURCES:
        dataset = primaries[source]
        assert (dataset.mode, dataset.reason) == ("simulated", REASON_NO_CREDENTIALS), source
        assert dataset.activated_at is not None, source
        connection = connections[source]
        assert (connection.mode, connection.status, connection.reason) == (
            "simulated",
            "ok",
            REASON_NO_CREDENTIALS,
        ), source
        assert connection.active_dataset_id == dataset.id, source

    companion = await _active_companion(db)
    assert (companion.source, companion.mode) == ("hubspot", "simulated")
    assert companion.parent_dataset_id == primaries["linkedin"].id

    every_dataset = await _all(db, select(SourceDataset))
    assert sorted(row.state for row in every_dataset) == ["active"] * 4, "a build was left over"

    freshness: dict[int, dict[str, DatasetFreshness]] = defaultdict(dict)
    for row in await _all(db, select(DatasetFreshness)):
        freshness[row.dataset_id][row.dataset] = row
    for dataset in (*primaries.values(), companion):
        rows = freshness[dataset.id]
        assert set(rows) == _expected_freshness(dataset.source), dataset.source
        for name, row in rows.items():
            assert row.completeness != "unknown", f"{name} of dataset {dataset.id} is unknown"
            assert row.last_success_at is not None, f"{name} of dataset {dataset.id}"


async def test_worker_startup_writes_baselines_without_discrepancies(
    db: Sessions, set_env: SetEnv
) -> None:
    """The first entity sync baselines every subject once, in its shape, and raises nothing.

    Every campaign the first entity sync of the active Google and LinkedIn datasets observed
    (snapshotted) and every explicitly shared Google budget has exactly one
    ``origin='baseline'`` decision of its own dataset: a shared budget in shape ``budget`` (no
    status), a Google campaign drawing from one as a ``shared_member`` holding that budget,
    every other campaign as ``own_budget``, each with the platform's status. A campaign known
    only from metrics rows (removed before the first sync, so never enumerated) has no
    platform state to baseline and gets none. So no discrepancy is raised on day one
    (AAP 0.5.2).
    """
    await _first_start(set_env)

    primaries = await _active_primaries(db)
    ad_datasets = [primaries["google"].id, primaries["linkedin"].id]
    snapshots = await _all(
        db, select(EntitySnapshot).where(EntitySnapshot.dataset_id.in_(ad_datasets))
    )
    observed = {
        entity_type: {row.entity_id for row in snapshots if row.entity_type == entity_type}
        for entity_type in ("campaign", "budget")
    }
    campaigns = [
        campaign
        for campaign in await _all(db, select(Campaign).where(Campaign.dataset_id.in_(ad_datasets)))
        if campaign.id in observed["campaign"]
    ]
    budgets = await _all(db, select(Budget).where(Budget.dataset_id == primaries["google"].id))
    shared = {
        budget.id: budget
        for budget in budgets
        if budget.explicitly_shared and budget.id in observed["budget"]
    }
    decisions = await _all(db, select(Decision).order_by(Decision.id))

    assert {campaign.source for campaign in campaigns} == {"google", "linkedin"}
    assert shared, "the simulated world must hold an explicitly shared budget"
    counts = Counter((decision.subject_type, decision.subject_id) for decision in decisions)
    expected_subjects = {("campaign", campaign.id) for campaign in campaigns} | {
        ("budget", budget_id) for budget_id in shared
    }
    assert set(counts) == expected_subjects
    assert [subject for subject, count in counts.items() if count != 1] == []
    assert {decision.origin for decision in decisions} == {"baseline"}
    assert not any(decision.tombstone for decision in decisions)

    by_subject = {(decision.subject_type, decision.subject_id): decision for decision in decisions}
    shapes: Counter[str] = Counter()
    for campaign in campaigns:
        decision = by_subject["campaign", campaign.id]
        member = campaign.source == "google" and campaign.budget_id in shared
        expected_shape = "shared_member" if member else "own_budget"
        assert decision.shape == expected_shape, campaign.name
        assert decision.dataset_id == campaign.dataset_id, campaign.name
        assert decision.status == campaign.status, campaign.name
        if member:
            assert decision.budget_id == campaign.budget_id, campaign.name
            assert (decision.daily_budget, decision.total_budget) == (None, None), campaign.name
        shapes[decision.shape] += 1
    for budget_id, budget in shared.items():
        decision = by_subject["budget", budget_id]
        assert (decision.shape, decision.status) == ("budget", None), budget.external_id
        assert decision.dataset_id == budget.dataset_id, budget.external_id
        shapes[decision.shape] += 1
    assert set(shapes) == {"own_budget", "shared_member", "budget"}, shapes

    discrepancies = await _all(db, select(Discrepancy))
    assert [(row.kind, row.state) for row in discrepancies if row.state != "resolved"] == []


async def test_worker_startup_syncs_content_assets(db: Sessions, set_env: SetEnv) -> None:
    """Startup makes the content-asset tables equal ``config/content_assets.yaml``.

    Every listed slug with its title, canonical lead-form URNs and creative overrides is
    stored, and an asset no longer listed is removed with its form mapping, so the White
    papers section resolves exactly the registry the operator ships.
    """
    async with db() as session:
        retired = ContentAsset(slug="retired-white-paper", title="A retired white paper")
        session.add(retired)
        await session.flush()
        session.add(ContentAssetForm(asset_id=retired.id, lead_form_urn="urn:li:leadGenForm:1"))
        await session.commit()

    await _first_start(set_env)

    assets = {asset.id: asset for asset in await _all(db, select(ContentAsset))}
    forms: dict[int, list[str]] = defaultdict(list)
    for form in await _all(db, select(ContentAssetForm)):
        forms[form.asset_id].append(form.lead_form_urn)
    overrides: dict[int, list[str]] = defaultdict(list)
    for override in await _all(db, select(ContentAssetCreative)):
        overrides[override.asset_id].append(override.creative_urn)
    stored = {
        asset.slug: (asset.title, sorted(forms[asset_id]), sorted(overrides[asset_id]))
        for asset_id, asset in assets.items()
    }

    registry = _registry()
    assert registry, "config/content_assets.yaml lists no asset"
    assert stored == registry
    assert set(forms) <= set(assets)
    assert set(overrides) <= set(assets)


async def test_default_run_exists_before_first_cycle(db: Sessions, set_env: SetEnv) -> None:
    """Startup computes the first default run itself, before any scheduler tick.

    One ``analysis_run`` exists: ``kind='default'``, ``status='succeeded'``,
    ``trigger='scheduled'``, every data mode simulated, the dataset ids of the active
    datasets (the companion as LinkedIn's effective CRM dataset) and the ``config_hash`` of
    the loaded configuration. Its window is the configured default, ends before today and
    lies inside the backfilled daily data, and it served every queued request, so the first
    tick has nothing left to recompute.
    """
    await _first_start(set_env)
    thresholds = Thresholds.load()

    runs = await _all(db, select(AnalysisRun))
    assert len(runs) == 1, f"expected exactly one run, found {len(runs)}"
    run = runs[0]
    assert (run.kind, run.status, run.trigger) == ("default", "succeeded", "scheduled")
    assert run.finished_at is not None
    assert run.data_modes == dict.fromkeys(SOURCES, "simulated")
    assert run.config_hash == thresholds.config_hash

    primaries = await _active_primaries(db)
    companion = await _active_companion(db)
    for source in SOURCES:
        assert run.dataset_ids[source] == primaries[source].id, source
    assert run.dataset_ids["hubspot_linkedin_effective"] == companion.id

    assert (run.window_end - run.window_start).days + 1 == thresholds.int("window.default_days")
    assert run.window_end < datetime.now(UTC).date()
    coverage = {
        row.dataset: row
        for row in await _all(
            db,
            select(DatasetFreshness).where(
                DatasetFreshness.dataset_id.in_([primaries["google"].id, primaries["linkedin"].id])
            ),
        )
    }
    for name in ("google.metrics_daily", "linkedin.analytics_daily"):
        row = coverage[name]
        assert row.covered_from is not None, name
        assert row.covered_through is not None, name
        assert row.covered_from <= run.window_start, name
        assert row.covered_through >= run.window_end, name

    async with db() as session:
        latest = await latest_default_run(session)
    assert latest is not None
    assert latest.id == run.id

    requests = await _all(db, select(AnalysisRequest))
    assert requests, "the dataset activations must have queued analysis requests"
    assert [request.id for request in requests if request.run_id != run.id] == []
    assert all(request.picked_at is not None for request in requests)
    assert REASON_CONFIG_CHANGED not in {request.reason for request in requests}


async def test_worker_startup_is_idempotent(db: Sessions, set_env: SetEnv) -> None:
    """A second startup reuses everything the first built and enqueues no ``config_changed``.

    A restarted worker finds every source's active dataset already of its resolved mode, so
    it builds nothing, writes no second baseline, keeps the connections and content-asset ids,
    and, with the configuration unchanged, its run carries the same hash and datasets.
    """
    settings = await _first_start(set_env)

    def dataset_keys(rows: Sequence[SourceDataset]) -> list[tuple[Any, ...]]:
        return sorted(
            (row.id, row.source, row.mode, row.state, row.role, row.parent_dataset_id)
            for row in rows
        )

    def connection_keys(rows: Sequence[SourceConnection]) -> list[tuple[Any, ...]]:
        return sorted(
            (row.source, row.mode, row.status, row.reason, row.active_dataset_id) for row in rows
        )

    datasets_before = dataset_keys(await _all(db, select(SourceDataset)))
    connections_before = connection_keys(await _all(db, select(SourceConnection)))
    decisions_before = [row.id for row in await _all(db, select(Decision).order_by(Decision.id))]
    assets_before = sorted((row.id, row.slug) for row in await _all(db, select(ContentAsset)))

    await startup(settings)

    assert dataset_keys(await _all(db, select(SourceDataset))) == datasets_before
    assert connection_keys(await _all(db, select(SourceConnection))) == connections_before
    decisions_after = [row.id for row in await _all(db, select(Decision).order_by(Decision.id))]
    assert decisions_after == decisions_before
    assert sorted((row.id, row.slug) for row in await _all(db, select(ContentAsset))) == (
        assets_before
    )
    reasons = {row.reason for row in await _all(db, select(AnalysisRequest))}
    assert REASON_CONFIG_CHANGED not in reasons

    runs = await _all(db, select(AnalysisRun).order_by(AnalysisRun.id))
    assert [(run.kind, run.status) for run in runs] == [("default", "succeeded")] * 2
    first, second = runs
    assert second.config_hash == first.config_hash
    assert second.dataset_ids == first.dataset_ids


async def test_changed_config_hash_enqueues_rerun(db: Sessions, set_env: SetEnv) -> None:
    """A configuration hash that differs from the latest default run's queues a rerun.

    AAP 0.3.2 lists "a changed threshold file detected at startup" among the rerun triggers:
    with the latest run recorded under an earlier configuration, the next startup queues one
    ``config_changed`` request, and its own default run, computed under the current hash,
    serves it.
    """
    settings = await _first_start(set_env)
    current_hash = Thresholds.load().config_hash
    async with db() as session:
        first = await latest_default_run(session)
        assert first is not None
        first_id = first.id
        await session.execute(
            update(AnalysisRun)
            .where(AnalysisRun.id == first_id)
            .values(config_hash=STALE_CONFIG_HASH)
        )
        await session.commit()

    await startup(settings)

    changed = await _all(
        db, select(AnalysisRequest).where(AnalysisRequest.reason == REASON_CONFIG_CHANGED)
    )
    assert len(changed) == 1, f"expected one config_changed request, found {len(changed)}"
    async with db() as session:
        latest = await latest_default_run(session)
    assert latest is not None
    assert latest.id != first_id
    assert latest.config_hash == current_hash
    assert changed[0].run_id == latest.id
    assert changed[0].picked_at is not None


# ---------------------------------------------------------------------------------------------
# Phase 4: the admin signs in and sees the five sections
# ---------------------------------------------------------------------------------------------


def _cookie_attributes(header: str) -> dict[str, str]:
    """Parse the attributes of one ``Set-Cookie`` header: lowercase name -> value."""
    attributes: dict[str, str] = {}
    for part in header.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    return attributes


def _session_cookie(response: httpx.Response, name: str) -> str:
    """Return the single ``Set-Cookie`` header that sets the session cookie ``name``."""
    headers = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith(f"{name}=")
    ]
    assert len(headers) == 1, f"expected one {name} cookie, found {headers}"
    return headers[0]


async def test_admin_logs_in_and_sees_five_populated_sections(
    db: Sessions,
    set_env: SetEnv,
    run_cli: RunCli,
    api_client: httpx.AsyncClient,
    login: Login,
    test_identity: Identity,
) -> None:
    """After ``ensure-admin`` and the worker's startup, the admin's first page is populated.

    The admin created by ``ensure-admin`` signs in with the CSRF header; ``GET
    /api/v1/recommendations`` serves the startup's default run with the sections
    ``keywords, campaigns, budget, creative, white_papers`` in that order, each holding at
    least one item computed from simulated data, under the banner that names Google,
    LinkedIn and HubSpot with ``credentials not configured``.
    """
    admin = run_cli("ensure-admin")
    assert admin.returncode == 0, admin.stderr
    await _first_start(set_env)

    signed_in = await login(api_client, email=test_identity.email, password=test_identity.password)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["email"] == test_identity.email

    response = await api_client.get("/api/v1/recommendations")
    assert response.status_code == 200, response.text
    page = response.json()

    async with db() as session:
        run = await latest_default_run(session)
    assert run is not None
    assert page["run"]["id"] == run.id
    assert page["run"]["kind"] == "default"
    assert page["run"]["data_modes"] == dict.fromkeys(SOURCES, "simulated")
    assert page["overlay"]["source_modes"] == dict.fromkeys(SOURCES, "simulated")

    assert [section["key"] for section in page["sections"]] == list(SECTION_ORDER)
    for section in page["sections"]:
        items = [item for group in section["groups"] for item in group["items"]]
        assert items, f"section {section['key']} has no item"
        assert {item["data_status"] for item in items} == {"simulated"}, section["key"]

    banners = [banner for banner in page["banners"] if banner["code"] == SIMULATED_BANNER]
    assert len(banners) == 1, page["banners"]
    banner = banners[0]
    assert banner["count"] == len(SOURCES)
    for source in SOURCES:
        assert f"{SOURCE_LABELS[source]} ({REASON_NO_CREDENTIALS})" in banner["message"], source


async def test_local_http_cookie_not_secure(
    admin_user: AppUser,
    api_client: httpx.AsyncClient,
    login: Login,
    set_env: SetEnv,
    test_identity: Identity,
) -> None:
    """On the local default ``http://localhost:8080`` the session cookie is not Secure.

    Plain HTTP is accepted only on localhost (AAP 0.3.2), so the cookie still is HttpOnly
    and ``SameSite=Lax``; the same login under an ``https://`` base URL does carry
    ``Secure``, so the flag follows the base URL rather than being absent altogether.
    """
    del admin_user  # Requested so the admin row exists before signing in.
    settings = get_settings()
    assert settings.app_base_url == DOCUMENTED_DEFAULTS["APP_BASE_URL"]
    assert not settings.secure_cookies

    local = await login(api_client)
    attributes = _cookie_attributes(_session_cookie(local, test_identity.session_cookie))
    assert "secure" not in attributes
    assert "httponly" in attributes
    assert attributes.get("samesite", "").lower() == "lax"

    set_env(APP_BASE_URL="https://ads.example.test")
    secure_app = app_module.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=secure_app), base_url="https://ads.example.test"
    ) as client:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": test_identity.email, "password": test_identity.password},
            headers=dict(test_identity.csrf_headers),
        )
    assert response.status_code == 200, response.text
    secure = _cookie_attributes(_session_cookie(response, test_identity.session_cookie))
    assert "secure" in secure
