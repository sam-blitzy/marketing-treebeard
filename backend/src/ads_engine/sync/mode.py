"""Source modes: live or simulated per source, connector clients, probes, fallback and builds.

AAP 0.3.3 ("Error classification and mock-data fallback", "Modes", "Granularity",
"Datasets and provenance", "Cross-source joins with simulation", "Recovery readiness"), AAP
0.5.1 ("Source mode and datasets") and AAP 0.11.2 ("First start"). Connectors raise only
``AccessDenied``, ``TransientError`` or ``QueryDefect`` (AAP 0.11.1); this module alone decides
what each one means:

==================  ================================================================
Classification      Consequence (:meth:`SourceModeController.on_error`)
==================  ================================================================
Access unavailable  Required credentials missing: the source is simulated with the
                    reason ``credentials not configured``.
Access denied       401/403 (LinkedIn: 401 after one token refresh), or a Google
                    denial inside an n8n HTTP 200: the whole source switches to
                    simulated data; the platform code and failing endpoint are the
                    reason.
Transient           429, 5xx, timeout, ``RESOURCE_EXHAUSTED``, call budget: the
                    affected datasets turn stale and keep their last good data. Never
                    mock.
Defect              400, GAQL ``QueryError``, validation: the job fails. No mock, no
                    retry.
==================  ================================================================

Modes (``DATA_MODE``):

* ``simulated`` forces every source to simulated data; it never probes and never promotes
  a source to live, even when the probes would pass.
* ``auto`` (default) applies the table per source. A source whose access is unavailable or
  denied is always simulated, never left without data.

Granularity: a source's active dataset is entirely live or entirely simulated, so a 403 on
any LinkedIn endpoint, Lead Sync included, switches all of LinkedIn. A simulated LinkedIn
dataset is built with a companion CRM dataset (simulated HubSpot, ``role=companion``) that
activates and retires with it, so simulated leads never join live contacts and
``@sim.invalid`` emails never reach a live portal.

State of record. ``source_connection`` holds each source's resolved mode, health status
(``ok``, ``stale``, ``denied`` or ``error``) and reason. **Automatic fallback** is the row
``mode='simulated', status='denied'`` under ``DATA_MODE=auto`` with credentials present:
only such a source gets recovery probes (every 30 minutes, driven by the scheduler), and the
``denied`` status is sticky while it lasts (activation of the fallback dataset and transient
errors on simulated data do not clear it). ``source_dataset`` holds the data itself:
switching mode always means building, backfilling and atomically activating a new dataset
(``sync/datasets.py``); until then the previous dataset stays active and labelled.

Import direction: ``sync/jobs.py`` imports this module, so the backfill is reached through
one deferred import inside :meth:`SourceModeController.build_dataset`.

Example:
    The worker's startup and ``ads-engine seed-simulated`` share one entry point::

        report = await bootstrap_sources(get_settings())
        for source, active in report.active.items():
            print(source, report.modes[source].mode, active)
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal

import httpx
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.thresholds import ConfigError, CrmBindings, Thresholds
from ads_engine.connectors.errors import AccessDenied, ConnectorError, QueryDefect, TransientError
from ads_engine.connectors.google_n8n.client import (
    SIMULATED_SECRET,
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    CallBudget,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.http import DEFAULT_RETRY, RetryPolicy, build_client
from ads_engine.connectors.hubspot.client import HubSpotClient, RateLimiter
from ads_engine.connectors.linkedin.auth import (
    SIMULATED_TOKEN,
    LinkedInTokenProvider,
    StaticTokenProvider,
)
from ads_engine.connectors.linkedin.client import LinkedInClient
from ads_engine.db import repositories
from ads_engine.db.models.ops import (
    CONNECTION_STATUSES,
    DATASET_ROLES,
    DATASET_STATES,
    SourceConnection,
    SourceDataset,
)
from ads_engine.db.session import get_sessionmaker
from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Settings, Source, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.transport import mock_transport
from ads_engine.sync import freshness, lead_match
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.datasets import Activation, DatasetManager, DatasetStateError, Mode, Readiness

__all__ = [
    "ABANDONED_BUILD_AFTER",
    "BOOTSTRAP_ORDER",
    "CONNECTION_STATUSES",
    "LINKEDIN_RETENTION_DAYS",
    "REASON_FORCED",
    "REASON_LIVE",
    "REASON_NO_CREDENTIALS",
    "REASON_RECOVERED",
    "REASON_SEEDED",
    "ActiveDataset",
    "BootstrapReport",
    "BuildOutcome",
    "ErrorDisposition",
    "ProbeCheck",
    "ProbeReport",
    "ResolvedMode",
    "SourceClients",
    "SourceModeController",
    "bootstrap_sources",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

REASON_NO_CREDENTIALS: Final = "credentials not configured"
"""Mode reason of a source simulated because its required environment variables are unset."""

REASON_FORCED: Final = "DATA_MODE=simulated"
"""Mode reason of every source while ``DATA_MODE=simulated`` forces simulated data."""

REASON_SEEDED: Final = "simulated data seeded on request"
"""Dataset reason of a simulated build forced by ``ads-engine seed-simulated`` for a source
that would otherwise resolve to live; the worker later replaces it through recovery readiness."""

REASON_LIVE: Final = "live access verified"
"""Dataset reason of a live build that followed a passing probe (first start with credentials,
or credentials added before a restart)."""

REASON_RECOVERED: Final = "recovered"
"""Dataset reason of a live build made by :meth:`SourceModeController.recover`."""

BOOTSTRAP_ORDER: Final[tuple[Source, ...]] = ("google", "hubspot", "linkedin")
"""Bootstrap order: HubSpot's primary must be active before a live LinkedIn build stages its
leads against it (AAP 0.3.3, "Staged matching")."""

ABANDONED_BUILD_AFTER: Final = timedelta(hours=6)
"""Age after which a ``building`` primary is treated as abandoned by a crashed build and
discarded. Builds run to completion inside one :meth:`SourceModeController.build_dataset`
call, so a younger one may still be in progress in another process (the CLI's
``seed-simulated`` while the worker runs)."""

LINKEDIN_RETENTION_DAYS: Final = 180
"""LinkedIn's 6-month daily retention, the cap on any LinkedIn backfill (AAP 0.2.4)."""

ErrorDisposition = Literal["switched_to_simulated", "stale", "defect", "ignored"]
"""What :meth:`SourceModeController.on_error` decided for one classified connector error."""

_STATUS_OK: Final = "ok"
_STATUS_STALE: Final = "stale"
_STATUS_DENIED: Final = "denied"
_STATUS_ERROR: Final = "error"
if tuple(CONNECTION_STATUSES) != (_STATUS_OK, _STATUS_STALE, _STATUS_DENIED, _STATUS_ERROR):
    # A renamed status would otherwise surface only as a CHECK violation at run time.
    raise RuntimeError(f"connection statuses {CONNECTION_STATUSES!r} no longer match sync/mode.py")

_ROLE_PRIMARY: Final = "primary"
_STATE_BUILDING: Final = "building"
if _ROLE_PRIMARY not in DATASET_ROLES or _STATE_BUILDING not in DATASET_STATES:
    raise RuntimeError("dataset roles or states no longer match sync/mode.py")

_MODES: Final[tuple[Mode, ...]] = ("live", "simulated")
_REASON_MAX: Final = 500
_SIMULATED_HUBSPOT_TOKEN: Final = SecretStr("simulated")
_LOCK_TIMEOUT: Final = text("SET LOCAL lock_timeout = '30s'")
_ELLIPSIS: Final = "…"


class _Keep(Enum):
    """Sentinel for a ``source_connection`` field left as stored."""

    KEEP = "keep"


_KEEP: Final = _Keep.KEEP


# ---------------------------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedMode:
    """The mode a source should read now, and why.

    Attributes:
        source: The source resolved.
        mode: ``live`` or ``simulated``.
        reason: Why the source is simulated (``credentials not configured``,
            ``DATA_MODE=simulated``, or the recorded denial); ``None`` for live.
        fallback: Whether the source is simulated because live access was denied or failed
            (automatic fallback); only such a source gets recovery probes.
        needs_probe: Live is desired but the active primary dataset is simulated or absent,
            so the full capability probe must pass before a live dataset is built.
    """

    source: Source
    mode: Mode
    reason: str | None
    fallback: bool = False
    needs_probe: bool = False


@dataclass(frozen=True, slots=True)
class ProbeCheck:
    """One capability check of a recovery probe.

    Attributes:
        name: What was checked, for example ``Q-G1`` or ``leadFormResponses 123``.
        ok: Whether the call succeeded.
        reason: The classified error's reason when it failed; ``None`` when it passed.
    """

    name: str
    ok: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """The result of one probe of a source's full capability set (AAP 0.3.3 "Probe set").

    Attributes:
        source: The source probed.
        passed: True exactly when there is at least one check and every check passed.
        checks: Each check, in the order run.

    Raises:
        ValueError: On construction, when ``passed`` disagrees with ``checks``.
    """

    source: Source
    passed: bool
    checks: tuple[ProbeCheck, ...]

    def __post_init__(self) -> None:
        """Reject a report whose verdict does not follow from its checks."""
        expected = bool(self.checks) and all(check.ok for check in self.checks)
        if self.passed != expected:
            raise ValueError("ProbeReport.passed must be true exactly when every check passed")

    @classmethod
    def of(cls, source: Source, checks: tuple[ProbeCheck, ...]) -> ProbeReport:
        """Build the report of ``checks``, deriving the verdict.

        Args:
            source: The source probed.
            checks: The checks run.

        Returns:
            The report; ``passed`` only when every check passed.
        """
        return cls(
            source=source,
            passed=bool(checks) and all(check.ok for check in checks),
            checks=checks,
        )

    @property
    def failed(self) -> tuple[ProbeCheck, ...]:
        """The checks that failed, in the order run."""
        return tuple(check for check in self.checks if not check.ok)

    @property
    def reason(self) -> str | None:
        """The first failing check as ``"<name>: <reason>"``, or ``None`` when it passed."""
        for check in self.checks:
            if not check.ok:
                return f"{check.name}: {check.reason}" if check.reason else check.name
        return None


@dataclass(frozen=True, slots=True)
class SourceClients:
    """The connector client of one source in one mode.

    :meth:`SourceModeController.open_clients` yields it with only the field of ``source``
    set; the ``require_*`` accessors return that client typed as non-optional.

    Attributes:
        source: The source the client calls.
        mode: ``live`` (real network, or a test's injected transport) or ``simulated`` (the
            simulation's mock transport).
        google: The n8n webhook client, for Google.
        linkedin: The Marketing API client, for LinkedIn.
        linkedin_account_ids: The ad accounts to read: ``LINKEDIN_AD_ACCOUNT_IDS`` live, the
            simulated world's accounts simulated.
        hubspot: The CRM client, for HubSpot.
    """

    source: Source
    mode: Mode
    google: GoogleAdsWebhookClient | None = None
    # The built-in gitleaks rule linkedin-client-id reads the 14-letter type name after
    # "linkedin:" as a client id; the line holds no credential.
    linkedin: LinkedInClient | None = None  # gitleaks:allow
    linkedin_account_ids: tuple[str, ...] = ()
    hubspot: HubSpotClient | None = None

    def require_google(self) -> GoogleAdsWebhookClient:
        """Return the Google client.

        Raises:
            RuntimeError: When these are not Google's clients.
        """
        if self.google is None:
            raise RuntimeError(f"no Google client: these are {self.source}'s clients")
        return self.google

    def require_linkedin(self) -> LinkedInClient:
        """Return the LinkedIn client.

        Raises:
            RuntimeError: When these are not LinkedIn's clients.
        """
        if self.linkedin is None:
            raise RuntimeError(f"no LinkedIn client: these are {self.source}'s clients")
        return self.linkedin

    def require_hubspot(self) -> HubSpotClient:
        """Return the HubSpot client.

        Raises:
            RuntimeError: When these are not HubSpot's clients.
        """
        if self.hubspot is None:
            raise RuntimeError(f"no HubSpot client: these are {self.source}'s clients")
        return self.hubspot


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """What one dataset build ended with.

    Attributes:
        source: The source built.
        mode: The mode of the dataset built.
        dataset_id: The dataset built; ``None`` when none could be begun. A dataset that was
            not activated has been discarded, so the id no longer exists then.
        activated: Whether the dataset was activated (and the previous one retired).
        reason: The dataset's reason when activated; why the build failed otherwise.
        companion_id: The companion CRM dataset of a simulated LinkedIn build.
        retired: The datasets the activation retired (previous primary and its companion).
        duration_ms: Wall-clock duration of the build.
    """

    source: Source
    mode: Mode
    dataset_id: int | None
    activated: bool
    reason: str | None
    companion_id: int | None = None
    retired: tuple[int, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class ActiveDataset:
    """A source's active primary dataset, as a bootstrap report shows it.

    Attributes:
        dataset_id: The active primary dataset.
        mode: Its mode.
        reason: Its reason, for example ``credentials not configured``.
        companion_id: The active companion CRM dataset of a simulated LinkedIn dataset.
    """

    dataset_id: int
    mode: Mode
    reason: str | None
    companion_id: int | None = None


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    """What :func:`bootstrap_sources` did for each source; no credentials are ever included.

    Attributes:
        outcomes: The build each source needed, or ``None`` when its active dataset was
            already of the resolved mode.
        modes: Each source's resolution after the bootstrap.
        active: Each source's active primary dataset after the bootstrap, or ``None``.
        failures: The error that stopped a source's bootstrap, by source; one source's
            failure never stops the others.
    """

    outcomes: Mapping[Source, BuildOutcome | None]
    modes: Mapping[Source, ResolvedMode]
    active: Mapping[Source, ActiveDataset | None] = field(default_factory=dict)
    failures: Mapping[Source, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _DatasetRef:
    """The identifying columns of a ``source_dataset`` row, safe to use after its session."""

    id: int
    source: Source
    mode: Mode
    state: str
    role: str
    reason: str | None

    @classmethod
    def of(cls, row: SourceDataset) -> _DatasetRef:
        """Copy the columns of a loaded row."""
        return cls(
            id=row.id,
            source=_as_source(row.source),
            mode=_as_mode(row.mode),
            state=row.state,
            role=row.role,
            reason=row.reason,
        )


@dataclass(frozen=True, slots=True)
class _MatchPass:
    """One matching pass a build runs: a CRM dataset and the leads it attempts.

    Attributes:
        crm_dataset_id: The HubSpot dataset (primary or companion) whose rows are attempted.
        crm_mode: Its mode, which selects the HubSpot client.
        lead_mode: The mode of every lead in the pass, which selects the LinkedIn client.
        lead_dataset_id: Restrict the pass to one LinkedIn dataset's leads; ``None`` for all.
    """

    crm_dataset_id: int
    crm_mode: Mode
    lead_mode: Mode
    lead_dataset_id: int | None


@dataclass(frozen=True, slots=True)
class _StepResult:
    """How the steps of one build ended, before the build is settled."""

    companion_id: int | None = None
    activation: Activation | None = None
    readiness: Readiness | None = None
    error: ConnectorError | None = None
    denied: bool = False
    message: str | None = None


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _as_source(value: str) -> Source:
    """Return ``value`` as a :data:`~ads_engine.settings.Source`.

    Raises:
        ValueError: When ``value`` is not ``google``, ``linkedin`` or ``hubspot``.
    """
    for source in SOURCES:
        if value == source:
            return source
    raise ValueError(f"unknown source {value!r}; expected one of {', '.join(SOURCES)}")


def _as_mode(value: str) -> Mode:
    """Return ``value`` as a :data:`~ads_engine.sync.datasets.Mode`.

    Raises:
        ValueError: When ``value`` is not ``live`` or ``simulated``.
    """
    for mode in _MODES:
        if value == mode:
            return mode
    raise ValueError(f"unknown mode {value!r}; expected live or simulated")


def _truncate(reason: str) -> str:
    """Cut a reason to the length stored and shown, marking a cut."""
    if len(reason) <= _REASON_MAX:
        return reason
    return reason[: _REASON_MAX - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def _error_source(error: ConnectorError, default: Source) -> Source:
    """Return the source whose call raised ``error``, or ``default`` when it names none."""
    for source in SOURCES:
        if error.source == source:
            return source
    return default


def _utcnow() -> datetime:
    """Return the current instant in UTC."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------------------------


class SourceModeController:
    """Decides, per source, between live and simulated data and carries out every switch.

    One controller serves a process: the worker's scheduler, the CLI or a test. It holds no
    mode state of its own beyond a per-source count of the denials it recorded, which lets a
    build notice a denial its own backfill jobs reported; every decision is read from
    ``source_connection`` and ``source_dataset``, so several controllers (worker and CLI)
    stay consistent.

    Example:
        A test drives a "live" Google through the simulator, with no network and no sleeps::

            controller = SourceModeController(
                settings,
                sessionmaker=sessionmaker,
                clock=lambda: FIXED_CLOCK,
                transports={"google": mock_transport("google", world, settings=settings)},
                retry=RetryPolicy(sleep=no_sleep),
            )
            outcome = await controller.ensure_dataset("google")
    """

    __slots__ = (
        "_budget",
        "_clock",
        "_datasets",
        "_denials",
        "_hubspot_limiter",
        "_retry",
        "_sessionmaker",
        "_settings",
        "_thresholds",
        "_transports",
        "_world",
    )

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        datasets: DatasetManager | None = None,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        world: SimulatedWorld | None = None,
        clock: Callable[[], datetime] | None = None,
        budget: CallBudget | None = None,
        transports: Mapping[Source, httpx.AsyncBaseTransport] | None = None,
        retry: RetryPolicy = DEFAULT_RETRY,
        hubspot_limiter: RateLimiter | None = None,
        thresholds: Thresholds | None = None,
    ) -> None:
        """Configure the controller; nothing is read, built or sent until the first call.

        Args:
            settings: The process settings; :func:`~ads_engine.settings.get_settings` when
                omitted.
            datasets: The dataset manager; one on ``sessionmaker`` and ``clock`` when omitted.
            sessionmaker: The session factory; the process-wide one, looked up per call, when
                omitted.
            world: The simulated world every simulated transport answers from; built lazily
                from the settings (``MOCK_SEED``) on first use and shared by all sources, so
                runs reproduce exactly.
            clock: Returns the current instant, timezone-aware; ``datetime.now(UTC)`` when
                omitted. It also drives the lazily built world and call budget.
            budget: The live Google call budget; ``GoogleCallBudget`` on the same session
                factory and clock when omitted. Simulated calls are never metered.
            transports: Transport per source for **live** clients; the network when a source
                is absent. Tests pass the simulator (or a failing handler) here to act as the
                live platform.
            retry: The retry policy of every client, live and simulated.
            hubspot_limiter: The HubSpot throttle; the process-wide 4 req/s limiter when
                omitted.
            thresholds: The configuration whose HubSpot mapping bindings are verified
                against; ``Thresholds.load()`` on first use when omitted.
        """
        self._settings: Settings = settings if settings is not None else get_settings()
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._datasets = (
            datasets if datasets is not None else DatasetManager(sessionmaker, clock=clock)
        )
        self._world = world
        self._budget = budget
        self._transports: Mapping[Source, httpx.AsyncBaseTransport] = MappingProxyType(
            dict(transports or {})
        )
        self._retry = retry
        self._hubspot_limiter = hubspot_limiter
        self._thresholds = thresholds
        self._denials: Counter[Source] = Counter()

    def __repr__(self) -> str:
        """Return the data mode only; settings carry secrets and are never shown."""
        return f"{type(self).__name__}(data_mode={self._settings.data_mode!r})"

    # -- accessors ---------------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        """The process settings."""
        return self._settings

    @property
    def datasets(self) -> DatasetManager:
        """The dataset manager every build goes through."""
        return self._datasets

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The injected session factory, or the process-wide one."""
        return self._sessionmaker if self._sessionmaker is not None else get_sessionmaker()

    @property
    def retry(self) -> RetryPolicy:
        """The retry policy of every client this controller opens."""
        return self._retry

    @property
    def world(self) -> SimulatedWorld:
        """The simulated world, built from the settings on first use and then shared."""
        if self._world is None:
            self._world = SimulatedWorld.from_settings(self._settings, clock=self._clock)
        return self._world

    @property
    def thresholds(self) -> Thresholds:
        """The configuration, loaded on first use.

        Raises:
            ConfigError: When a configuration file is missing or invalid.
        """
        if self._thresholds is None:
            self._thresholds = Thresholds.load()
        return self._thresholds

    @property
    def budget(self) -> CallBudget:
        """The live Google call budget, built on first use."""
        if self._budget is None:
            self._budget = GoogleCallBudget(
                self._settings, sessionmaker=self._sessionmaker, clock=self._clock
            )
        return self._budget

    def now(self) -> datetime:
        """Read the clock once.

        Returns:
            The current instant, timezone-aware.

        Raises:
            ValueError: When the injected clock returns a naive datetime.
        """
        current = self._clock() if self._clock is not None else _utcnow()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("the source-mode clock must return a timezone-aware datetime")
        return current

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        """Yield a session inside its own transaction, committed when the block exits."""
        async with self.sessionmaker() as session, session.begin():
            yield session

    def _fallback_possible(self, source: Source) -> bool:
        """Whether ``source`` can be in automatic fallback: ``auto`` with credentials."""
        return self._settings.data_mode == "auto" and self._settings.has_credentials(source)

    # -- mode resolution ---------------------------------------------------------------------

    async def resolve(self, source: Source) -> ResolvedMode:
        """Resolve the mode ``source`` should read now (AAP 0.3.3 "Modes").

        * ``DATA_MODE=simulated``: simulated, :data:`REASON_FORCED`; never probes.
        * ``auto`` without the source's credentials: simulated, :data:`REASON_NO_CREDENTIALS`
          (Google needs webhook URL, secret and customer id; LinkedIn account ids plus the
          refresh-token trio or an access token; HubSpot its token).
        * ``auto`` in automatic fallback: simulated with the recorded denial.
        * Otherwise live; ``needs_probe`` when the active primary is simulated or absent, so
          credentials added before a restart go live only through recovery readiness.

        The ``source_connection`` row is created when missing, and the simulated
        resolutions record their mode and reason on it (a stale ``denied`` status is
        cleared, since a source without credentials cannot be in fallback).

        Args:
            source: The source to resolve.

        Returns:
            The resolution.

        Raises:
            ValueError: When ``source`` is unknown.
        """
        checked = _as_source(source)
        settings = self._settings
        if settings.data_mode == "simulated":
            await self._record_connection(
                checked, mode="simulated", reason=REASON_FORCED, clear_denied=True
            )
            return ResolvedMode(checked, "simulated", REASON_FORCED)
        if not settings.has_credentials(checked):
            missing = settings.missing_credentials(checked)
            await self._record_connection(
                checked,
                mode="simulated",
                reason=REASON_NO_CREDENTIALS,
                clear_denied=True,
                missing=missing,
            )
            return ResolvedMode(checked, "simulated", REASON_NO_CREDENTIALS)

        async with self._transaction() as session:
            connection, _ = await self._lock_connection(session, checked, insert_mode="live")
            fallback = connection.mode == "simulated" and connection.status == _STATUS_DENIED
            stored_reason = connection.reason
            active = await self._active_in(session, checked)
        if fallback:
            return ResolvedMode(
                checked,
                "simulated",
                stored_reason if stored_reason else "access denied",
                fallback=True,
            )
        return ResolvedMode(
            checked,
            "live",
            None,
            needs_probe=active is None or active.mode != "live",
        )

    async def in_fallback(self, source: Source) -> bool:
        """Whether ``source`` is in automatic fallback and so due recovery probes.

        True only under ``DATA_MODE=auto`` with the source's credentials present, when the
        source is simulated because live access was denied or a live build failed.

        Args:
            source: The source to check.

        Returns:
            Whether recovery probes apply.
        """
        checked = _as_source(source)
        if not self._fallback_possible(checked):
            return False
        async with self._transaction() as session:
            row = await session.scalar(
                select(SourceConnection).where(SourceConnection.source == checked)
            )
            return row is not None and row.mode == "simulated" and row.status == _STATUS_DENIED

    # -- source_connection -------------------------------------------------------------------

    async def _lock_connection(
        self, session: AsyncSession, source: Source, *, insert_mode: Mode
    ) -> tuple[SourceConnection, bool]:
        """Create ``source``'s ``source_connection`` row when missing and lock it.

        The call counters are left to their server defaults on insert and never written
        here: ``sync/budget_guard.py`` owns them.

        Returns:
            The locked row, refreshed from the database, and whether it was just created.
        """
        created = await session.scalar(
            insert(SourceConnection)
            .values(source=source, mode=insert_mode, status=_STATUS_OK, reason=None)
            .on_conflict_do_nothing(index_elements=["source"])
            .returning(SourceConnection.source)
        )
        row = (
            await session.scalars(
                select(SourceConnection)
                .where(SourceConnection.source == source)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one()
        return row, created is not None

    async def _record_connection(
        self,
        source: Source,
        *,
        mode: Mode | _Keep = _KEEP,
        status: str | _Keep = _KEEP,
        reason: str | _Keep | None = _KEEP,
        attempted_at: datetime | None = None,
        clear_denied: bool = False,
        sticky_denied: bool = False,
        missing: Sequence[str] = (),
        session: AsyncSession | None = None,
    ) -> None:
        """Insert or update ``source``'s ``source_connection`` row; log a mode change.

        Args:
            source: The source.
            mode: The resolved mode to store; kept when omitted.
            status: The health status to store (``ok``, ``stale``, ``denied``, ``error``);
                kept when omitted.
            reason: The reason to store; kept when omitted.
            attempted_at: Stored as ``last_attempt_at`` when given.
            clear_denied: Replace a stored ``denied`` with ``ok``; used when the source
                cannot be in fallback (forced simulation, no credentials).
            sticky_denied: Keep a stored ``denied`` of a simulated source instead of the new
                ``stale`` or ``error``: a transient or defective call on fallback data must not
                end the fallback, whose recovery probes rely on the marker.
            missing: Names (never values) of the unset credential variables, for the log.
            session: Join the caller's transaction instead of committing one here.
        """
        if session is None:
            async with self._transaction() as owned:
                await self._apply_connection(
                    owned,
                    source,
                    mode=mode,
                    status=status,
                    reason=reason,
                    attempted_at=attempted_at,
                    clear_denied=clear_denied,
                    sticky_denied=sticky_denied,
                    missing=missing,
                )
            return
        await self._apply_connection(
            session,
            source,
            mode=mode,
            status=status,
            reason=reason,
            attempted_at=attempted_at,
            clear_denied=clear_denied,
            sticky_denied=sticky_denied,
            missing=missing,
        )

    async def _apply_connection(
        self,
        session: AsyncSession,
        source: Source,
        *,
        mode: Mode | _Keep,
        status: str | _Keep,
        reason: str | _Keep | None,
        attempted_at: datetime | None,
        clear_denied: bool,
        sticky_denied: bool,
        missing: Sequence[str],
    ) -> None:
        """Apply :meth:`_record_connection` inside ``session``."""
        if not isinstance(status, _Keep) and status not in CONNECTION_STATUSES:
            raise ValueError(f"unknown connection status {status!r}")
        insert_mode: Mode = mode if not isinstance(mode, _Keep) else self._default_mode(source)
        row, created = await self._lock_connection(session, source, insert_mode=insert_mode)
        previous_mode = None if created else row.mode
        previous_status = row.status

        new_mode = row.mode if isinstance(mode, _Keep) else mode
        new_status = row.status if isinstance(status, _Keep) else status
        if clear_denied and new_status == _STATUS_DENIED:
            new_status = _STATUS_OK
        if (
            sticky_denied
            and previous_status == _STATUS_DENIED
            and row.mode == "simulated"
            and new_status in (_STATUS_STALE, _STATUS_ERROR)
        ):
            new_status = _STATUS_DENIED
        new_reason = row.reason if isinstance(reason, _Keep) else reason
        if new_reason is not None:
            new_reason = _truncate(new_reason)

        changed = (row.mode, row.status, row.reason) != (new_mode, new_status, new_reason)
        row.mode = new_mode
        row.status = new_status
        row.reason = new_reason
        if attempted_at is not None:
            row.last_attempt_at = attempted_at
        await session.flush()

        if previous_mode != new_mode:
            _log.info(
                "sync.mode.change",
                source=source,
                from_mode=previous_mode,
                to_mode=new_mode,
                status=new_status,
                reason=new_reason,
                missing_credentials=list(missing),
            )
        elif changed:
            _log.info(
                "sync.connection",
                source=source,
                mode=new_mode,
                from_status=previous_status,
                status=new_status,
                reason=new_reason,
            )

    def _default_mode(self, source: Source) -> Mode:
        """The mode a newly created connection row starts with, before anything is resolved."""
        return "live" if self._fallback_possible(source) else "simulated"

    async def _connection_reason(self, source: Source) -> str | None:
        """Return the stored reason of ``source``'s connection row, or ``None``."""
        async with self._transaction() as session:
            return await session.scalar(
                select(SourceConnection.reason).where(SourceConnection.source == source)
            )

    async def _enter_fallback(self, source: Source, reason: str) -> None:
        """Put ``source`` into automatic fallback: simulated, ``denied``, with ``reason``.

        Recovery probes then retry live access every 30 minutes (scheduler), instead of a
        live rebuild on every 30-second wake.
        """
        await self._record_connection(
            source,
            mode="simulated",
            status=_STATUS_DENIED,
            reason=reason,
            attempted_at=self.now(),
        )
        _log.warning("sync.fallback", source=source, reason=_truncate(reason))

    async def _reconcile_status(self, source: Source, active: _DatasetRef) -> None:
        """Return a ``stale`` or ``error`` connection to ``ok`` once its data has caught up.

        AAP 0.5.2 keeps source health current on every attempt. A transient or defective
        call marks the connection when it fails (:meth:`on_error`); this clears the mark
        once every freshness row of the active primary has succeeded at or after that
        failure and no later attempt of it has failed. ``denied`` is never cleared here.
        """
        async with self._transaction() as session:
            row = await session.scalar(
                select(SourceConnection).where(SourceConnection.source == source).with_for_update()
            )
            if row is None or row.status not in (_STATUS_STALE, _STATUS_ERROR):
                return
            rows = await freshness.load_freshness(session, active.id)
            if not rows:
                return
            failed_at = row.last_attempt_at
            for entry in rows.values():
                succeeded = entry.last_success_at
                if succeeded is None:
                    return
                if entry.last_attempt_at is not None and succeeded < entry.last_attempt_at:
                    return
                if failed_at is not None and succeeded < failed_at:
                    return
            previous = row.status
            row.status = _STATUS_OK
            if row.mode == active.mode:
                row.reason = active.reason
            await session.flush()
        _log.info("sync.connection", source=source, from_status=previous, status=_STATUS_OK)

    # -- datasets ----------------------------------------------------------------------------

    @staticmethod
    async def _active_in(session: AsyncSession, source: Source) -> _DatasetRef | None:
        """Return ``source``'s active primary dataset read in ``session``."""
        row = (await repositories.active_datasets(session)).get(source)
        return None if row is None else _DatasetRef.of(row)

    async def _active(self, source: Source) -> _DatasetRef | None:
        """Return ``source``'s active primary dataset."""
        async with self._transaction() as session:
            return await self._active_in(session, source)

    async def _dataset(self, dataset_id: int) -> _DatasetRef | None:
        """Return a dataset's identifying columns, or ``None`` when it does not exist."""
        async with self._transaction() as session:
            row = await session.get(SourceDataset, dataset_id, populate_existing=True)
            return None if row is None else _DatasetRef.of(row)

    # -- clients -----------------------------------------------------------------------------

    @asynccontextmanager
    async def open_clients(self, source: Source, mode: str) -> AsyncIterator[SourceClients]:
        """Open ``source``'s connector client in ``mode``; its HTTP client closes on exit.

        * **Simulated**: every request goes through the simulation's mock transport
          (``mock_transport(source, self.world, settings=self.settings)``), so the live clients
          and parsers run unchanged. Google gets :data:`SIMULATED_WEBHOOK_URL` with
          :data:`SIMULATED_SECRET` under the configured secret header name, which the
          transport validates against the same settings, and is never metered against the
          live call budget; LinkedIn a static simulated token and the world's ad accounts;
          HubSpot a client marked not live, which never sends simulated emails to a portal.
        * **Live**: the network, or the transport injected for the source. Google reads the
          n8n webhook settings and draws on the product's call budget (no Google OAuth,
          client id, secret, refresh token or developer token exists here); LinkedIn
          exchanges the refresh token (or uses the static access token) for the configured
          ad accounts; HubSpot uses ``HUBSPOT_ACCESS_TOKEN``.

        Args:
            source: The source to open.
            mode: ``live`` or ``simulated``, normally the mode of the dataset being written.

        Yields:
            The clients, with only ``source``'s field set.

        Raises:
            ValueError: When ``source`` or ``mode`` is unknown, or a live client is
                requested for a source without credentials.
        """
        checked_source = _as_source(source)
        checked_mode = _as_mode(mode)
        transport: httpx.AsyncBaseTransport | None
        if checked_mode == "simulated":
            transport = mock_transport(checked_source, self.world, settings=self._settings)
        else:
            if not self._settings.has_credentials(checked_source):
                missing = ", ".join(self._settings.missing_credentials(checked_source))
                raise ValueError(f"no live {checked_source} client without credentials: {missing}")
            transport = self._transports.get(checked_source)
        async with build_client(checked_source, transport, settings=self._settings) as http:
            yield self._clients(checked_source, checked_mode, http)

    def _clients(self, source: Source, mode: Mode, http: httpx.AsyncClient) -> SourceClients:
        """Build ``source``'s connector client in ``mode`` on ``http``."""
        settings = self._settings
        simulated = mode == "simulated"
        match source:
            case "google":
                if simulated:
                    google = GoogleAdsWebhookClient(
                        http=http,
                        budget=UNMETERED,
                        webhook_url=SIMULATED_WEBHOOK_URL,
                        secret_header=settings.n8n_webhook_secret_header,
                        secret=SecretStr(SIMULATED_SECRET),
                        customer_id=self.world.google_customer_id,
                        retry=self._retry,
                    )
                else:
                    google = GoogleAdsWebhookClient.from_settings(
                        settings, http, self.budget, retry=self._retry
                    )
                return SourceClients(source, mode, google=google)
            case "linkedin":
                if simulated:
                    linkedin = LinkedInClient(
                        http, StaticTokenProvider(SIMULATED_TOKEN), retry=self._retry
                    )
                    accounts = self.world.linkedin_account_ids
                else:
                    tokens = LinkedInTokenProvider(settings, http, retry=self._retry)
                    linkedin = LinkedInClient(http, tokens, retry=self._retry)
                    accounts = tuple(settings.linkedin_ad_account_ids)
                return SourceClients(source, mode, linkedin=linkedin, linkedin_account_ids=accounts)
            case "hubspot":
                if simulated:
                    hubspot = HubSpotClient(
                        http,
                        _SIMULATED_HUBSPOT_TOKEN,
                        live=False,
                        retry=self._retry,
                        limiter=self._hubspot_limiter,
                    )
                else:
                    hubspot = HubSpotClient.from_settings(
                        settings,
                        http,
                        live=True,
                        retry=self._retry,
                        limiter=self._hubspot_limiter,
                    )
                return SourceClients(source, mode, hubspot=hubspot)

    # -- probes ------------------------------------------------------------------------------

    async def probe(self, source: Source) -> ProbeReport:
        """Run ``source``'s full capability set against the live platform.

        AAP 0.3.3 ("Probe set"):

        * Google: Q-G1 through the webhook.
        * LinkedIn, per configured ad account: ``adAccounts/{id}``, a one-day ``adAnalytics``
          (yesterday UTC), ``leadForms`` with ``count=1`` and ``leadFormResponses`` with
          ``count=1``.
        * HubSpot: a one-email contacts batch read, a one-result deals search and a
          one-input associations read (``HubSpotClient.probe``).

        Each check catches only classified connector errors, and a LinkedIn listing cut short
        by a transient error fails its check. Partial success (for example LinkedIn ads
        readable while Lead Sync returns 403) is a failed probe and changes nothing, so a
        source never flips back and forth. Under ``DATA_MODE=simulated``, or without
        credentials, no request is sent and the report fails with that reason.

        Args:
            source: The source to probe.

        Returns:
            The report; ``passed`` only when every check passed.
        """
        checked = _as_source(source)
        if self._settings.data_mode == "simulated":
            checks: tuple[ProbeCheck, ...] = (ProbeCheck("data_mode", False, REASON_FORCED),)
        elif not self._settings.has_credentials(checked):
            checks = (ProbeCheck("credentials", False, REASON_NO_CREDENTIALS),)
        else:
            async with self.open_clients(checked, "live") as clients:
                match checked:
                    case "google":
                        checks = await self._probe_google(clients)
                    case "linkedin":
                        checks = await self._probe_linkedin(clients)
                    case "hubspot":
                        checks = await self._probe_hubspot(clients)
        report = ProbeReport.of(checked, checks)
        _log.info(
            "sync.probe",
            source=checked,
            passed=report.passed,
            checks=len(report.checks),
            failed=[check.name for check in report.failed],
            reason=report.reason,
        )
        return report

    @staticmethod
    async def _probe_google(clients: SourceClients) -> tuple[ProbeCheck, ...]:
        """Probe Google with Q-G1 (customer id, currency, time zone)."""
        google = clients.require_google()
        try:
            await google.run_query("Q-G1")
        except ConnectorError as exc:
            return (ProbeCheck("Q-G1", False, exc.reason),)
        return (ProbeCheck("Q-G1", True),)

    async def _probe_linkedin(self, clients: SourceClients) -> tuple[ProbeCheck, ...]:
        """Probe every configured LinkedIn ad account with the four Lead Sync and ads calls."""
        linkedin = clients.require_linkedin()
        now = self.now().astimezone(UTC)
        yesterday = now.date() - timedelta(days=1)
        submitted = (now - timedelta(days=1), now)
        checks: list[ProbeCheck] = []
        for account in clients.linkedin_account_ids:
            name = f"adAccounts/{account}"
            try:
                await linkedin.get_ad_account(account)
            except ConnectorError as exc:
                checks.append(ProbeCheck(name, False, exc.reason))
            else:
                checks.append(ProbeCheck(name, True))

            name = f"adAnalytics {account}"
            try:
                analytics = await linkedin.analytics_daily(
                    account, (yesterday, yesterday), expected_creatives=1
                )
            except ConnectorError as exc:
                checks.append(ProbeCheck(name, False, exc.reason))
            else:
                checks.append(_listing_check(name, analytics.interrupted_by))

            name = f"leadForms {account}"
            try:
                forms = await linkedin.list_lead_forms(account, page_size=1, max_pages=1)
            except ConnectorError as exc:
                checks.append(ProbeCheck(name, False, exc.reason))
            else:
                checks.append(_listing_check(name, forms.interrupted_by))

            name = f"leadFormResponses {account}"
            try:
                responses = await linkedin.list_lead_responses(
                    account, submitted, known_versions={}, page_size=1, max_pages=1
                )
            except ConnectorError as exc:
                checks.append(ProbeCheck(name, False, exc.reason))
            else:
                checks.append(_listing_check(name, responses.interrupted_by))
        if not checks:
            checks.append(ProbeCheck("adAccounts", False, "no LinkedIn ad account configured"))
        return tuple(checks)

    @staticmethod
    async def _probe_hubspot(clients: SourceClients) -> tuple[ProbeCheck, ...]:
        """Probe HubSpot's contacts batch read, deals search and associations read."""
        hubspot = clients.require_hubspot()
        try:
            await hubspot.probe()
        except ConnectorError as exc:
            return (ProbeCheck("crm", False, exc.reason),)
        return (ProbeCheck("crm", True),)

    # -- error consequences ------------------------------------------------------------------

    async def on_error(
        self,
        source: Source,
        exc: ConnectorError,
        *,
        dataset_id: int | None = None,
        datasets: Sequence[str] = (),
    ) -> ErrorDisposition:
        """Apply the AAP 0.3.3 fallback table to one classified connector error.

        Callers hand over every ``ConnectorError`` a job or build lets out, after the
        connector's own retries; this method alone decides what follows.

        * ``AccessDenied``: when the data in play is live (the given dataset, else the
          source's active primary), the source switches to simulated: ``source_connection``
          records ``mode='simulated'``, ``status='denied'`` and the error's reason (platform
          code and failing endpoint). The simulated dataset is built by
          :meth:`ensure_dataset` (the next scheduler wake, or at once when a build was
          denied); until it activates the previous dataset stays active and labelled.
          Returns ``"switched_to_simulated"``. Under ``DATA_MODE=simulated``, or for a
          denial raised on simulated data (a simulated fault), it only logs and returns
          ``"ignored"``.
        * ``TransientError`` (429, 5xx, timeout, ``RESOURCE_EXHAUSTED``, call budget): never
          mock. Each named freshness dataset of the dataset records the failure, so it turns
          stale while its last good data is kept, and the connection's status becomes
          ``stale``. Returns ``"stale"``.
        * ``QueryDefect`` (400, GAQL ``QueryError``, validation): the connection's status
          becomes ``error`` and the query id is logged; no mock, no retry. Returns
          ``"defect"``, and the job marks its ``sync_run`` failed.

        A stale or erroneous call on fallback data leaves the ``denied`` marker in place,
        and the reason of a simulated source's connection keeps its mode reason.

        Args:
            source: The source whose dataset the failed call was writing.
            exc: The classified error.
            dataset_id: The dataset the call was writing; the source's active primary when
                omitted.
            datasets: The freshness dataset names (for example ``linkedin.analytics_daily``)
                whose refresh the error interrupted.

        Returns:
            The disposition.
        """
        checked = _as_source(source)
        now = self.now()
        fields: dict[str, object] = {
            **exc.log_fields,
            "source": checked,
            "error_source": exc.source,
            "dataset_id": dataset_id,
            "datasets": list(datasets),
        }

        if isinstance(exc, AccessDenied):
            if self._settings.data_mode == "simulated":
                _log.warning("sync.classification", disposition="ignored", **fields)
                return "ignored"
            if await self._context_mode(checked, dataset_id) == "simulated":
                _log.warning("sync.classification", disposition="ignored", **fields)
                return "ignored"
            await self._record_connection(
                checked,
                mode="simulated",
                status=_STATUS_DENIED,
                reason=exc.reason,
                attempted_at=now,
            )
            self._denials[checked] += 1
            _log.warning("sync.classification", disposition="switched_to_simulated", **fields)
            return "switched_to_simulated"

        if isinstance(exc, TransientError):
            await self._record_transient(checked, exc, dataset_id, datasets, now)
            _log.warning("sync.classification", disposition="stale", **fields)
            return "stale"

        # QueryDefect, and defensively any other connector error: a defect is never retried
        # or mocked, so treating an unknown kind as one is the conservative consequence.
        await self._record_connection(
            checked,
            status=_STATUS_ERROR,
            reason=await self._failure_reason(checked, exc),
            attempted_at=now,
            sticky_denied=True,
        )
        # ``fields`` carries the query id of a ``QueryDefect`` (``query_id``) when it has one.
        _log.error(
            "sync.classification",
            disposition="defect",
            unexpected_kind=not isinstance(exc, QueryDefect),
            **fields,
        )
        return "defect"

    async def _context_mode(self, source: Source, dataset_id: int | None) -> Mode:
        """The mode of the data a failed call was serving.

        The given dataset's mode; else the source's active primary's; else the mode a call
        would be made in now (live only with credentials under ``auto``).
        """
        if dataset_id is not None:
            dataset = await self._dataset(dataset_id)
            if dataset is not None:
                return dataset.mode
        active = await self._active(source)
        if active is not None:
            return active.mode
        return self._default_mode(source)

    async def _failure_reason(self, source: Source, exc: ConnectorError) -> str | _Keep | None:
        """The connection reason after a transient or defective call.

        A live connection shows the failure; a simulated one keeps its mode reason, which is
        what the simulated-source banner explains (``credentials not configured``, a denial).
        """
        async with self._transaction() as session:
            mode = await session.scalar(
                select(SourceConnection.mode).where(SourceConnection.source == source)
            )
        return exc.reason if mode == "live" else _KEEP

    async def _record_transient(
        self,
        source: Source,
        exc: TransientError,
        dataset_id: int | None,
        names: Sequence[str],
        now: datetime,
    ) -> None:
        """Record a transient failure on the named freshness rows and the connection."""
        reason = await self._failure_reason(source, exc)
        async with self._transaction() as session:
            # Lock order: source_connection before the source_dataset guard the freshness
            # write takes, the order DatasetManager.activate locks them in; the reverse
            # order could deadlock against a concurrent activation of this source.
            await self._record_connection(
                source,
                status=_STATUS_STALE,
                reason=reason,
                attempted_at=now,
                sticky_denied=True,
                session=session,
            )
            target = dataset_id
            if target is None:
                target = await repositories.active_dataset_id(session, source)
            if target is not None and names:
                await self._record_failed_freshness(session, target, names, exc, now)

    @staticmethod
    async def _record_failed_freshness(
        session: AsyncSession,
        dataset_id: int,
        names: Sequence[str],
        exc: TransientError,
        now: datetime,
    ) -> None:
        """Fold a failed attempt into each named freshness row of a writable dataset."""
        try:
            dataset = await repositories.ensure_writable(session, dataset_id)
        except repositories.DatasetNotWritable:
            # Retired (or discarded) while the job ran: its rows are read-only history.
            _log.info("sync.freshness.skipped", dataset_id=dataset_id, reason="not writable")
            return
        owned = freshness.SOURCE_DATASETS[_as_source(dataset.source)]
        outcome = freshness.FreshnessOutcome.failure(now, reason=exc.reason)
        for name in dict.fromkeys(names):
            if name not in owned:
                _log.warning(
                    "sync.freshness.skipped",
                    dataset_id=dataset_id,
                    dataset=name,
                    reason=f"not a {dataset.source} dataset",
                )
                continue
            await freshness.record_freshness(session, dataset_id, name, outcome)

    # -- CRM bindings ------------------------------------------------------------------------

    async def verify_crm_bindings(self, dataset_id: int) -> CrmBindings:
        """Verify the MQL, QL and Google bindings against a HubSpot dataset's portal and store them.

        AAP 0.3.3 ("Startup property verification"). The client of the dataset's own mode is
        used: the live portal for a live dataset, the simulated one for a simulated primary
        or a companion, since the two resolve different stage ids. An unresolved binding is
        stored as unavailable (never counted as 0).

        Args:
            dataset_id: A HubSpot dataset (primary or companion), ``building`` or ``active``.

        Returns:
            The verified bindings, as stored.

        Raises:
            ValueError: When the dataset is not HubSpot's, or a live dataset's credentials
                are no longer configured.
            ads_engine.sync.datasets.DatasetNotFoundError: When the dataset does not exist.
            ads_engine.db.repositories.DatasetNotWritable: When it is retired.
            AccessDenied: When the portal refuses the token or a scope.
            TransientError: When a properties read keeps failing; stored bindings stand.
            QueryDefect: When a properties response breaks its contract.
            ConfigError: When the HubSpot mapping configuration is invalid.
        """
        dataset = await self.datasets.get(dataset_id)
        if dataset.source != "hubspot":
            raise ValueError(
                f"source_dataset {dataset_id} is {dataset.source}; CRM bindings are HubSpot's"
            )
        mode = _as_mode(dataset.mode)
        mapping = self.thresholds.mapping
        async with self.open_clients("hubspot", mode) as clients:
            bindings = await clients.require_hubspot().verify_bindings(mapping, self._settings)
        await self.datasets.store_crm_bindings(dataset_id, bindings)
        _log.info(
            "sync.crm_bindings.verified",
            dataset_id=dataset_id,
            mode=mode,
            role=dataset.role,
            mql_available=bindings.mql.available,
            ql_available=bindings.ql.available,
            google_link_available=bindings.google.available,
        )
        return bindings

    async def verify_active_bindings(
        self, source: Source, *, skip: frozenset[int] = frozenset()
    ) -> None:
        """Re-verify the CRM bindings of the active HubSpot dataset ``source`` relies on.

        Called by :func:`bootstrap_sources` at worker startup: for ``hubspot`` the active
        primary, for ``linkedin`` the companion of a simulated active dataset. A classified
        error goes through :meth:`on_error`; a denial on live data switches HubSpot to
        simulated at once, and a transient failure keeps the stored bindings.

        Args:
            source: ``hubspot`` or ``linkedin``; Google has no CRM dataset.
            skip: Datasets verified moments ago by a build in the same bootstrap.
        """
        checked = _as_source(source)
        target: int | None = None
        mode: Mode = "simulated"
        if checked == "hubspot":
            active = await self._active("hubspot")
            if active is not None:
                target, mode = active.id, active.mode
        elif checked == "linkedin":
            active = await self._active("linkedin")
            if active is not None and active.mode == "simulated":
                target = await self.datasets.companion_of(active.id)
        if target is None or target in skip:
            return
        if not self._can_open("hubspot", mode):
            _log.info("sync.crm_bindings.skipped", dataset_id=target, mode=mode)
            return
        try:
            await self.verify_crm_bindings(target)
        except ConnectorError as exc:
            disposition = await self.on_error("hubspot", exc, dataset_id=target)
            if disposition == "switched_to_simulated":
                await self.ensure_dataset("hubspot")

    def _can_open(self, source: Source, mode: Mode) -> bool:
        """Whether a ``mode`` client of ``source`` may be opened now."""
        if mode == "simulated":
            return True
        return self._settings.data_mode == "auto" and self._settings.has_credentials(source)

    # -- builds ------------------------------------------------------------------------------

    async def build_dataset(self, source: Source, mode: str, *, reason: str | None) -> BuildOutcome:
        """Build, backfill and, once ready, atomically activate a new primary dataset.

        AAP 0.3.3 ("Transitions", "Staged matching", "Recovery readiness"):

        1. Begin a ``building`` dataset; a simulated LinkedIn one also begins its companion
           CRM dataset (simulated HubSpot, ``role=companion``).
        2. HubSpot primary or companion: verify the CRM bindings with the client of its mode.
        3. HubSpot primary: when LinkedIn's active primary is live, stage a ``lead_match`` row
           for each of its leads in the backfill range (simulated leads are never staged
           against a primary).
        4. Backfill (``sync/jobs.py``; ``BACKFILL_DAYS``, LinkedIn capped at 180 days). The
           first entity sync writes the baseline decisions.
        5. Re-stage (LinkedIn may have activated a new dataset meanwhile), run one more
           matching pass when rows are still pending, then assess readiness.
        6. Ready: activate in one transaction with the companion, retiring the previous
           dataset and its matching state. Otherwise discard; the previous dataset stays
           active.

        A live build that does not activate (denied, transient failure or not ready) leaves
        the source in automatic fallback with the failure as reason, then makes sure a
        simulated dataset is active, so no source is left without data and live access is
        retried by the 30-minute recovery probe.

        Args:
            source: The source to build.
            mode: ``live`` or ``simulated``.
            reason: The dataset's reason, recorded as the connection's mode reason on
                activation (for example ``credentials not configured`` or ``recovered``).

        Returns:
            The build's outcome; for a failed live build, the outcome of the simulated build
            that followed when one was needed.

        Raises:
            ValueError: When ``source``, ``mode`` or ``reason`` is invalid.
        """
        # Deferred import: sync/jobs.py imports this module at its top (controller, clients,
        # on_error), so a module-level import here would be a cycle. The ignore covers
        # sync/jobs.py not existing yet and is inert once it does.
        from ads_engine.sync.jobs import (  # type: ignore[import-not-found, import-untyped, unused-ignore]
            backfill_dataset,
        )

        checked_source = _as_source(source)
        checked_mode = _as_mode(mode)
        started = time.perf_counter()
        denials = self._denials[checked_source]
        dataset_id = await self.datasets.begin(checked_source, checked_mode, reason=reason)
        _log.info(
            "sync.build.start",
            source=checked_source,
            mode=checked_mode,
            dataset_id=dataset_id,
            reason=reason,
        )
        settled = False
        try:
            result = await self._build_steps(
                checked_source, checked_mode, dataset_id, reason, denials, backfill_dataset
            )
            settled = True
        finally:
            if not settled:
                # An unexpected exception or a cancellation ended the build: drop it rather
                # than leave a building dataset behind.
                await self._discard_quietly(dataset_id, best_effort=True)

        duration_ms = int((time.perf_counter() - started) * 1000)
        if result.activation is not None:
            outcome = BuildOutcome(
                source=checked_source,
                mode=checked_mode,
                dataset_id=dataset_id,
                activated=True,
                reason=reason,
                companion_id=result.companion_id,
                retired=result.activation.retired,
                duration_ms=duration_ms,
            )
            _log.info(
                "sync.build",
                source=checked_source,
                mode=checked_mode,
                dataset_id=dataset_id,
                companion_id=result.companion_id,
                outcome="activated",
                retired=list(outcome.retired),
                duration_ms=duration_ms,
            )
            return outcome
        return await self._settle_failure(
            checked_source, checked_mode, dataset_id, result, duration_ms
        )

    async def _build_steps(
        self,
        source: Source,
        mode: Mode,
        dataset_id: int,
        reason: str | None,
        denials: int,
        backfill: Callable[[SourceModeController, int], Awaitable[object]],
    ) -> _StepResult:
        """Run steps 1-6 of :meth:`build_dataset` after the dataset was begun."""
        companion_id: int | None = None
        try:
            if source == "linkedin" and mode == "simulated":
                companion_id = await self.datasets.begin(
                    "hubspot", "simulated", role="companion", parent=dataset_id
                )
            if source == "hubspot":
                await self.verify_crm_bindings(dataset_id)
                await self._restage(source, mode, dataset_id, None)
            if companion_id is not None:
                await self.verify_crm_bindings(companion_id)
            await backfill(self, dataset_id)
            if self._denials[source] != denials:
                # A backfill job reported a denial through on_error: the build is void.
                return _StepResult(companion_id=companion_id, denied=True)
            readiness = await self._settle_matching(source, mode, dataset_id, companion_id)
            if not readiness.ready:
                return _StepResult(companion_id=companion_id, readiness=readiness)
            activation = await self._activate(source, mode, dataset_id, reason)
        except ConnectorError as exc:
            return _StepResult(companion_id=companion_id, error=exc)
        except (DatasetStateError, repositories.DatasetNotWritable) as exc:
            # Another process activated, discarded or retired a dataset this build relies on,
            # or activation found the dataset not ready under its lock.
            return _StepResult(companion_id=companion_id, message=str(exc))
        return _StepResult(companion_id=companion_id, readiness=readiness, activation=activation)

    async def _settle_failure(
        self,
        source: Source,
        mode: Mode,
        dataset_id: int,
        result: _StepResult,
        duration_ms: int,
    ) -> BuildOutcome:
        """Classify a failed build, discard it, and for a live build fall back to simulated."""
        denied = result.denied
        if result.error is not None:
            failed_source = _error_source(result.error, source)
            # Classify before discarding: on_error reads the building dataset's mode.
            disposition = await self.on_error(
                failed_source,
                result.error,
                dataset_id=dataset_id if failed_source == source else None,
            )
            denied = failed_source == source and disposition == "switched_to_simulated"
            reason = result.error.reason
        elif result.denied:
            reason = await self._connection_reason(source) or "access denied during backfill"
        elif result.readiness is not None:
            reason = "not ready: " + "; ".join(result.readiness.reasons)
        else:
            reason = result.message or "build failed"
        reason = _truncate(reason)

        await self._discard_quietly(dataset_id)
        outcome = BuildOutcome(
            source=source,
            mode=mode,
            dataset_id=dataset_id,
            activated=False,
            reason=reason,
            companion_id=result.companion_id,
            duration_ms=duration_ms,
        )
        _log.warning(
            "sync.build",
            source=source,
            mode=mode,
            dataset_id=dataset_id,
            companion_id=result.companion_id,
            outcome="discarded",
            reason=reason,
            duration_ms=duration_ms,
        )
        if mode != "live":
            # The previous dataset stays active; the next wake retries the simulated build.
            return outcome
        if not denied:
            # Decision: any live build that fails (transient, defect, not ready) enters
            # automatic fallback, so live access is retried by the 30-minute recovery probe
            # instead of a full live backfill on every 30-second wake, which would burn the
            # shared Google call budget, and a source with no active dataset gets simulated
            # data meanwhile.
            await self._enter_fallback(source, f"live build failed: {reason}")
        follow = await self._ensure(source, force_simulated=False, allow_live=False)
        return follow if follow is not None else outcome

    async def _activate(
        self, source: Source, mode: Mode, dataset_id: int, reason: str | None
    ) -> Activation:
        """Activate a ready build, keeping the automatic-fallback marker of a fallback build.

        ``DatasetManager.activate`` records ``status='ok'``; a simulated dataset activated
        while the source is in automatic fallback must keep ``denied``, or recovery probes
        would stop. Both writes share one transaction, so no reader sees the gap.
        """
        async with self._transaction() as session:
            await session.execute(_LOCK_TIMEOUT)
            prior = (
                await session.execute(
                    select(SourceConnection.mode, SourceConnection.status)
                    .where(SourceConnection.source == source)
                    .with_for_update()
                )
            ).one_or_none()
            activation = await self.datasets.activate(dataset_id, reason=reason, session=session)
            if (
                mode == "simulated"
                and prior is not None
                and prior.status == _STATUS_DENIED
                and self._fallback_possible(source)
            ):
                await session.execute(
                    update(SourceConnection)
                    .where(SourceConnection.source == source)
                    .values(status=_STATUS_DENIED)
                )
        prior_mode = None if prior is None else prior.mode
        if prior_mode != mode:
            _log.info(
                "sync.mode.change",
                source=source,
                from_mode=prior_mode,
                to_mode=mode,
                reason=reason,
                dataset_id=dataset_id,
            )
        return activation

    async def _settle_matching(
        self, source: Source, mode: Mode, dataset_id: int, companion_id: int | None
    ) -> Readiness:
        """Re-stage, run one more matching pass when rows are pending, and assess readiness.

        The extra pass calls ``lead_match.run_matching`` for the rows still pending rather than
        repeating the backfill: only rows staged after the backfill's own matching (a LinkedIn
        dataset activated meanwhile) need it, and a second backfill would re-read every source.
        """
        match_pass = await self._restage(source, mode, dataset_id, companion_id)
        readiness = await self.datasets.readiness(dataset_id)
        pending = readiness.pending_matches
        if readiness.companion is not None:
            pending += readiness.companion.pending_matches
        if pending and match_pass is not None:
            await self._matching_pass(match_pass)
            readiness = await self.datasets.readiness(dataset_id)
        return readiness

    async def _restage(
        self, source: Source, mode: Mode, dataset_id: int, companion_id: int | None
    ) -> _MatchPass | None:
        """Stage the ``lead_match`` rows a build must attempt; return the pass that does so.

        * HubSpot primary: every lead of LinkedIn's active primary, when that is live, in the
          backfill range. Simulated leads are matched only in their own companion.
        * Live LinkedIn: every lead of the build against HubSpot's active primary.
        * Simulated LinkedIn: every lead of the build against its companion.

        Staging is idempotent: a lead with a row in the CRM dataset is skipped.
        """
        now = self.now()
        span = min(self._settings.backfill_days, LINKEDIN_RETENTION_DAYS)
        since = now - timedelta(days=span)
        async with self._transaction() as session:
            if source == "hubspot":
                leads = await self._active_in(session, "linkedin")
                if leads is None or leads.mode != "live":
                    return None
                crm_dataset_id, crm_mode, lead_dataset_id = dataset_id, mode, leads.id
                match_pass = _MatchPass(dataset_id, mode, "live", None)
            elif source == "linkedin" and mode == "live":
                crm = await self._active_in(session, "hubspot")
                if crm is None:
                    return None
                crm_dataset_id, crm_mode, lead_dataset_id = crm.id, crm.mode, dataset_id
                match_pass = _MatchPass(crm.id, crm.mode, "live", dataset_id)
            elif source == "linkedin" and companion_id is not None:
                crm_dataset_id, crm_mode, lead_dataset_id = companion_id, "simulated", dataset_id
                match_pass = _MatchPass(companion_id, "simulated", "simulated", dataset_id)
            else:
                return None
            staged = await lead_match.stage_rows(
                session, crm_dataset_id, lead_dataset_id, since=since, now=now
            )
        _log.info(
            "sync.build.staged",
            source=source,
            dataset_id=dataset_id,
            crm_dataset_id=crm_dataset_id,
            crm_mode=crm_mode,
            lead_dataset_id=lead_dataset_id,
            staged=staged,
        )
        return match_pass

    async def _matching_pass(self, match_pass: _MatchPass) -> None:
        """Attempt the pending ``lead_match`` rows of one CRM dataset once."""
        if not (
            self._can_open("linkedin", match_pass.lead_mode)
            and self._can_open("hubspot", match_pass.crm_mode)
        ):
            _log.warning(
                "sync.build.matching_skipped",
                crm_dataset_id=match_pass.crm_dataset_id,
                reason="live credentials not configured",
            )
            return
        bindings = await self.datasets.load_crm_bindings(match_pass.crm_dataset_id)
        if bindings is None:
            bindings = await self.verify_crm_bindings(match_pass.crm_dataset_id)
        properties = HubSpotClient.contact_property_map(bindings, self._settings)
        async with (
            self.open_clients("linkedin", match_pass.lead_mode) as lead_clients,
            self.open_clients("hubspot", match_pass.crm_mode) as crm_clients,
        ):
            linkedin = lead_clients.require_linkedin()
            clients = lead_match.MatchClients(
                # Every lead of one pass is of one mode (the isolation rule of stage_rows),
                # so a single client re-fetches them all.
                linkedin_for=lambda _lead_dataset_id: linkedin,
                hubspot=crm_clients.require_hubspot(),
                contact_properties=properties,
            )
            report = await lead_match.run_matching(
                self._sessionmaker,
                match_pass.crm_dataset_id,
                clients,
                now=self.now(),
                lead_dataset_id=match_pass.lead_dataset_id,
            )
        _log.info(
            "sync.build.matching",
            crm_dataset_id=match_pass.crm_dataset_id,
            attempted=report.attempted,
            matched=report.matched,
            pending_left=report.pending_left,
        )

    async def _discard_quietly(self, dataset_id: int, *, best_effort: bool = False) -> None:
        """Discard a building dataset, tolerating one already gone or activated elsewhere.

        Args:
            dataset_id: The building primary to discard.
            best_effort: Also tolerate a database error, when the build is already ending
                with another exception; the abandoned-build sweep removes it later.
        """
        try:
            await self.datasets.discard(dataset_id)
        except DatasetStateError as exc:
            _log.info("sync.build.discard_skipped", dataset_id=dataset_id, reason=str(exc))
        except SQLAlchemyError as exc:
            if not best_effort:
                raise
            _log.warning(
                "sync.build.discard_failed", dataset_id=dataset_id, error=type(exc).__name__
            )

    async def _discard_abandoned_builds(self, source: Source) -> None:
        """Discard ``source``'s building primaries left behind by a crashed build.

        Decision: nothing else removes them (``purge_retired`` purges only retired
        datasets), and a stale build would hold staged ``lead_match`` rows forever. A build
        younger than :data:`ABANDONED_BUILD_AFTER` may still be running in another process.
        """
        cutoff = self.now() - ABANDONED_BUILD_AFTER
        async with self._transaction() as session:
            abandoned = list(
                await session.scalars(
                    select(SourceDataset.id)
                    .where(
                        SourceDataset.source == source,
                        SourceDataset.role == _ROLE_PRIMARY,
                        SourceDataset.state == _STATE_BUILDING,
                        SourceDataset.created_at < cutoff,
                    )
                    .order_by(SourceDataset.id)
                )
            )
        for dataset_id in abandoned:
            _log.warning("sync.build.abandoned", source=source, dataset_id=dataset_id)
            await self._discard_quietly(dataset_id)

    # -- ensuring, recovery ------------------------------------------------------------------

    async def ensure_dataset(
        self, source: Source, *, force_simulated: bool = False
    ) -> BuildOutcome | None:
        """Make sure ``source``'s active primary dataset is of the mode it resolves to.

        Called on every scheduler wake and by :func:`bootstrap_sources`. It performs a
        pending live-to-simulated switch after a denial, first builds, and the first live
        build once credentials are present:

        * an active primary of the resolved mode: nothing to build (``None``);
        * simulated resolved: build simulated with the resolution's reason;
        * live resolved: probe the full capability set first and build live only when it
          passes. A failed probe enters automatic fallback with the failing check as reason
          and builds simulated data when none is active.

        Building primaries abandoned by a crashed build are discarded first, and a
        ``stale``/``error`` connection whose data has caught up returns to ``ok``.

        Args:
            source: The source.
            force_simulated: Build simulated data even where live would be resolved
                (``ads-engine seed-simulated``); an active simulated dataset is kept.

        Returns:
            The build's outcome, or ``None`` when no build was needed.
        """
        return await self._ensure(
            _as_source(source), force_simulated=force_simulated, allow_live=True
        )

    async def _ensure(
        self, source: Source, *, force_simulated: bool, allow_live: bool
    ) -> BuildOutcome | None:
        """Run :meth:`ensure_dataset`; ``allow_live=False`` after a failed live build."""
        await self._discard_abandoned_builds(source)
        resolved = await self.resolve(source)
        if force_simulated and resolved.mode == "live":
            resolved = ResolvedMode(source, "simulated", REASON_SEEDED)
        active = await self._active(source)
        if active is not None and active.mode == resolved.mode:
            await self._reconcile_status(source, active)
            return None
        if resolved.mode == "simulated":
            return await self.build_dataset(source, "simulated", reason=resolved.reason)
        if not allow_live:
            # Unreachable after _enter_fallback (the source then resolves simulated); kept so
            # a failed live build can never recurse into another one.
            return None
        report = await self.probe(source)
        if not report.passed:
            await self._enter_fallback(source, report.reason or "live probe failed")
            if active is None:
                return await self._ensure(source, force_simulated=False, allow_live=False)
            return None
        return await self.build_dataset(source, "live", reason=REASON_LIVE)

    async def recover(self, source: Source) -> BuildOutcome | None:
        """Try to bring a source in automatic fallback back to live data.

        AAP 0.3.3 ("Recovery readiness"). Only a source :meth:`in_fallback` is probed; the
        scheduler calls this at most every 30 minutes. When every capability passes, a live
        dataset is built and activated only after its backfill completes (and, for HubSpot,
        staged-matching readiness passes); a failed build is discarded and the simulated
        dataset stays active. A partial success changes nothing.

        Args:
            source: The source.

        Returns:
            The live build's outcome, or ``None`` when the source is not in fallback or the
            probe did not pass.
        """
        checked = _as_source(source)
        if not await self.in_fallback(checked):
            return None
        report = await self.probe(checked)
        if not report.passed:
            _log.info("sync.recover", source=checked, recovered=False, reason=report.reason)
            return None
        outcome = await self.build_dataset(checked, "live", reason=REASON_RECOVERED)
        _log.info(
            "sync.recover",
            source=checked,
            recovered=outcome.activated and outcome.mode == "live",
            dataset_id=outcome.dataset_id,
            reason=outcome.reason,
        )
        return outcome

    async def active_datasets(self) -> Mapping[Source, ActiveDataset | None]:
        """Return each source's active primary dataset, with a simulated LinkedIn's companion.

        Returns:
            The active datasets by source, ``None`` for a source with none.
        """
        result: dict[Source, ActiveDataset | None] = {}
        async with self._transaction() as session:
            active = await repositories.active_datasets(session)
            for source in SOURCES:
                row = active.get(source)
                if row is None:
                    result[source] = None
                    continue
                companion_id: int | None = None
                if source == "linkedin":
                    companion_id = await self.datasets.companion_of(row.id, session=session)
                result[source] = ActiveDataset(
                    dataset_id=row.id,
                    mode=_as_mode(row.mode),
                    reason=row.reason,
                    companion_id=companion_id,
                )
        return MappingProxyType(result)


def _listing_check(name: str, interrupted_by: TransientError | None) -> ProbeCheck:
    """Turn a probe listing into a check: a listing cut short by a transient error fails.

    A one-page probe listing is normally incomplete ("page limit"); that is a pass.
    """
    if interrupted_by is not None:
        return ProbeCheck(name, False, interrupted_by.reason)
    return ProbeCheck(name, True)


# ---------------------------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------------------------

_BOOTSTRAP_FAILURES: Final = (
    ConnectorError,
    DatasetStateError,
    repositories.DatasetNotWritable,
    ConfigError,
    httpx.HTTPError,
)
"""Failures that stop one source's bootstrap but never the others'. A database outage is not
among them: it stops every source alike and propagates."""


async def bootstrap_sources(
    settings: Settings,
    *,
    force_simulated: bool = False,
    controller: SourceModeController | None = None,
) -> BootstrapReport:
    """Give every source an active primary dataset of its resolved mode.

    Shared by ``worker/main.py`` startup (after the scheduler lock is held, before the first
    tick) and ``ads-engine seed-simulated``. Sources run in :data:`BOOTSTRAP_ORDER`
    (``google``, ``hubspot``, ``linkedin``), so HubSpot's primary is active before a live
    LinkedIn build stages its leads against it. Per source: create the ``source_connection``
    row, resolve, build when needed (:meth:`SourceModeController.ensure_dataset`), and
    re-verify the CRM bindings of active HubSpot datasets (primary and companion) that this
    bootstrap did not just build. Baselines are written by the first entity sync inside
    each backfill.

    With no credentials every source ends simulated with ``credentials not configured``,
    built, backfilled and activated (AAP 0.11.2 "First start"). Each source is independent:
    one source's failure is recorded in the report and never stops the others.

    Args:
        settings: The process settings.
        force_simulated: Build simulated data even where live would be resolved; an active
            simulated dataset is kept.
        controller: The controller to use; one on ``settings`` when omitted.

    Returns:
        The report: build outcomes, final modes, active datasets and failures, by source.
    """
    ctl = controller if controller is not None else SourceModeController(settings)
    outcomes: dict[Source, BuildOutcome | None] = {}
    failures: dict[Source, str] = {}
    for source in BOOTSTRAP_ORDER:
        try:
            outcome = await ctl.ensure_dataset(source, force_simulated=force_simulated)
            outcomes[source] = outcome
            if source in ("hubspot", "linkedin"):
                await ctl.verify_active_bindings(source, skip=_built_ids(outcome))
        except _BOOTSTRAP_FAILURES as exc:
            outcomes.setdefault(source, None)
            failures[source] = _truncate(f"{type(exc).__name__}: {exc}")
            _log.error(
                "sync.bootstrap.source_failed",
                source=source,
                error=type(exc).__name__,
                message=failures[source],
            )

    active = await ctl.active_datasets()
    modes: dict[Source, ResolvedMode] = {}
    for source in BOOTSTRAP_ORDER:
        resolved = await ctl.resolve(source)
        current = active.get(source)
        if (
            force_simulated
            and resolved.mode == "live"
            and current is not None
            and current.mode == "simulated"
        ):
            resolved = ResolvedMode(source, "simulated", current.reason or REASON_SEEDED)
        modes[source] = resolved

    for source in BOOTSTRAP_ORDER:
        current = active.get(source)
        _log.info(
            "sync.bootstrap",
            source=source,
            mode=modes[source].mode,
            reason=modes[source].reason,
            fallback=modes[source].fallback,
            dataset_id=None if current is None else current.dataset_id,
            dataset_mode=None if current is None else current.mode,
            companion_id=None if current is None else current.companion_id,
            built=outcomes.get(source) is not None,
            failed=source in failures,
        )
    return BootstrapReport(
        outcomes=MappingProxyType(outcomes),
        modes=MappingProxyType(modes),
        active=active,
        failures=MappingProxyType(failures),
    )


def _built_ids(outcome: BuildOutcome | None) -> frozenset[int]:
    """The datasets an activated build just verified, so bootstrap does not verify them twice."""
    if outcome is None or not outcome.activated or outcome.dataset_id is None:
        return frozenset()
    ids = {outcome.dataset_id}
    if outcome.companion_id is not None:
        ids.add(outcome.companion_id)
    return frozenset(ids)
