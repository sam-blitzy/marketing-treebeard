"""Source fallback, recovery readiness, dataset transitions and the cross-dataset purge.

AAP 0.11.2, row ``integration/test_sync_fallback.py``; every test's docstring names the
guarantee of that row it proves:

* **Access unavailable**: missing environment makes a source simulated under ``auto``.
* **Access denied**: HTTP 401/403, or HTTP 200 from n8n carrying ``PERMISSION_DENIED``,
  switches the whole source to simulated data with the platform's code as the reason.
* **Forced simulation**: ``DATA_MODE=simulated`` never probes or promotes, even when the probes
  would pass.
* **Partial permissions**: LinkedIn ads readable while Lead Sync returns 403 stays simulated,
  with no dataset flip.
* **Recovery**: a live dataset is built and activated only after its backfill; a build failure
  discards the building dataset; frozen runs still render after a purge; a live LinkedIn
  build's late matches in HubSpot's active primary get that primary's CRM sync, and every
  completed CRM sync of that primary queues its rerun, even when the build is voided.
* **Cross-dataset purge**: purging a retired HubSpot dataset deletes only its own ``lead_match``
  rows; purging a retired LinkedIn dataset removes its leads and every match referencing them;
  a companion is purged with its parent; no foreign-key error at any step.
* **Companion CRM dataset**: LinkedIn simulated while HubSpot is live reads the companion; the
  Google-to-CRM link reads the live primary, whose later CRM sync commits its Google-touch
  discovery with the contact refresh and queues one rerun per publication transaction; the
  companion activates and retires with its parent.
* **Isolation**: simulated HubSpot contacts never collide with live ids and leave every
  computation when the live dataset activates.
* **Transient and defects**: 429/5xx/timeout retry three times, then stale, never mock; a 400
  fails the job; a first-start probe that meets a defect (a Q-G1 400, an ``adAnalytics``
  validation error with every endpoint HTTP 200) and no denial is an ``error`` with no
  fallback and no mock, logged with its query id, while a denial in the same probe still
  switches the source; a probe that meets only transient failures (a Q-G1 503, a ``leadForms``
  listing a 503 cut short) leaves the source ``stale`` with no fallback and no mock, an active
  simulated dataset as it was, and its next probe 30 minutes later, while a denial in the same
  probe, or one recorded while it ran, still switches the source; the Google call budget
  defers what it cannot pay for, and the next runs read the deferred scopes first (metrics)
  or only the outstanding reads (entities); a read that a transient error or the budget cut
  short (Google or LinkedIn entities, Google metrics) keeps the dataset's last success, so the
  dataset turns stale.

How "live" is exercised without a network (AAP 0.3.3 "Contract fidelity"): each source's live
client is built exactly as in production, through
``SourceModeController(transports={source: ...})``, on the simulator's
``httpx.MockTransport`` acting as the live platform. A fault added to that world (403, 401,
``PERMISSION_DENIED`` in an HTTP 200, 429 with ``Retry-After``, 503, a timeout, a 400) makes the
"platform" misbehave in its own wire shape, so the live clients, retries and error
classification run unchanged. The conftest network guard refuses every real socket.

Every clock is fixed (:data:`ads_engine.simulation.scenarios.FIXED_NOW`, or a fixed later
instant for a later step); retries record their waits instead of sleeping, and the HubSpot
throttle grants every start at once, so no test sleeps. Dataset state is asserted on database
rows and on the report objects the product returns (``BootstrapReport``, ``ProbeReport``,
``BuildOutcome``, ``JobResult``), never on log text; the one logging guarantee checked, a
failed probe's classification (a defect with its query id, a transient failure) logged with
its check, is read from the structured event (``structlog.testing.capture_logs``).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ads_engine.analysis import metrics, runner
from ads_engine.analysis.context import Freshness
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.connectors.errors import AccessDenied, CallBudgetExhausted
from ads_engine.connectors.google_n8n.partition import FetchOutcome, scope_from_json
from ads_engine.connectors.google_n8n.queries import END, INT64_MAX, START, parse_query
from ads_engine.connectors.http import RetryPolicy
from ads_engine.db import repositories
from ads_engine.db.models import (
    AnalysisRequest,
    Budget,
    Campaign,
    CrmAssociationFetch,
    CrmContact,
    DatasetFreshness,
    Decision,
    Discrepancy,
    EntitySnapshot,
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    Keyword,
    Lead,
    LeadMatch,
    Recommendation,
    SourceConnection,
    SourceDataset,
    SyncRun,
)
from ads_engine.settings import SOURCES, Source, get_settings
from ads_engine.simulation import (
    HUBSPOT_HASHED_CONTACT_IDS,
    HUBSPOT_ID_RANGE,
    SIM_EMAIL_DOMAIN,
    is_simulated_id,
)
from ads_engine.simulation.generator import Fault, SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_NOW, SCENARIOS, Scenario, fixed_clock
from ads_engine.simulation.transport import JournalEntry
from ads_engine.sync import freshness, jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.datasets import RETENTION, DatasetManager
from ads_engine.sync.mode import (
    JOB_PROBE,
    PROBE_INTERVAL,
    REASON_FORCED,
    REASON_LIVE,
    REASON_NO_CREDENTIALS,
    REASON_RECOVERED,
    BuildOutcome,
    ProbeCheck,
    ProbeReport,
    SourceModeController,
    bootstrap_sources,
)
from ads_engine.sync.scheduler import Scheduler

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Scenarios, instants and expected texts
# ---------------------------------------------------------------------------------------------

BASE: Final = "fallback.missing_credentials.case"
"""The catalogue's clean every-source world (the K1 search campaign, the capped LinkedIn lead-gen
campaign with ten leads, a flat CRM funnel): the simulated data of every controller here, and,
without a fault, the live platform once credentials work."""

HTTP_403: Final = "fallback.http_403.case"
"""LinkedIn ``adAccounts`` answers 403 (the catalogue's access-denied case)."""

N8N_DENIED: Final = "fallback.n8n_200_permission_denied.case"
"""The n8n webhook answers HTTP 200 with a Google ``PERMISSION_DENIED`` body."""

FORCED: Final = "fallback.forced_simulated.case"
"""The forced-simulation case: a world whose probes would all pass."""

LEADSYNC_403: Final = "fallback.leadsync_403_only.case"
"""LinkedIn ads readable while Lead Sync (``leadFormResponses``) answers 403."""

RECOVERY: Final = "fallback.recovery_all_pass.case"
"""The recovery case: every capability passes once access is back."""

DEFECT_400: Final = "fallback.defect_400.case"
"""Q-G3 answers HTTP 400, a defect."""

COMPANION: Final = "fallback.companion.case"
"""LinkedIn simulated while HubSpot is live."""

ISOLATION: Final = "fallback.isolation.case"
"""HubSpot simulated while LinkedIn is live."""

TRANSIENT_429: Final = "fallback.transient_429.case"
TRANSIENT_5XX: Final = "fallback.transient_5xx.case"
TIMEOUT: Final = "fallback.timeout.case"
"""The catalogue's transient faults: 429 with ``Retry-After``, 503 and a timeout."""

DEFAULT: Final = "default"
"""The first-start world in which every section populates; used where a run needs LinkedIn
items with entity keys (CR1, CR3, C3a, B4) to show what a purge does to run history."""

T0: Final = FIXED_NOW
"""The first instant of every test: the scenarios' fixed read time."""

T1: Final = FIXED_NOW + timedelta(hours=1)
"""A later step's instant (a recovery, a denial, a failing job)."""

T2: Final = FIXED_NOW + timedelta(hours=2)
"""A third step's instant."""

T3: Final = FIXED_NOW + timedelta(hours=3)
"""A fourth step's instant."""

DAY_AFTER: Final = FIXED_NOW + timedelta(hours=24, minutes=30)
"""Half an hour past the shipped 24 h ``sources.stale_after_hours`` after :data:`T0`, the
live build's instant, but 23.5 h after :data:`T1`: a dataset whose last success is the
build's is stale then, one that succeeded at :data:`T1` or later is not."""

BACKFILL_DAYS: Final = 35
"""``BACKFILL_DAYS`` of every test: covers the default 28-day window and every lead of the
catalogue world (day -28 onwards) while keeping backfills small."""

HISTORICAL_REASON: Final = "Computed from data that has since been replaced"
"""``current.held_reason_now`` of a historical item, verbatim from AAP 0.3.2."""

GOOGLE_CAMPAIGN_PROPERTY: Final = "google_campaign_id"
GOOGLE_TOUCH_PROPERTY: Final = "google_ads_touch_at"
"""The Google-to-CRM contact properties of the catalogue's Google discovery case."""

LEAD_SYNC_SURFACE: Final = "lead_form_responses"
REAL_EMAIL_DOMAIN: Final = "example.com"
"""The domain the live LinkedIn stand-in gives its leads: real-shaped, not ``sim.invalid``."""


def _after_retention(retired_at: datetime) -> Callable[[], datetime]:
    """Return a fixed clock one day past :data:`RETENTION` after ``retired_at``."""
    instant = retired_at + RETENTION + timedelta(days=1)
    return lambda: instant


# ---------------------------------------------------------------------------------------------
# Clock, retry waits, throttle and the live LinkedIn stand-in
# ---------------------------------------------------------------------------------------------


class _Clock:
    """A clock fixed at one instant: every controller of a step reads the same time."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        """Return the fixed instant."""
        return self.at


class _Waits:
    """The retry policy's sleep: records every wait instead of sleeping."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record one backoff or ``Retry-After`` wait."""
        self.seconds.append(seconds)


class _InstantLimiter:
    """A HubSpot throttle that grants every request start at once.

    Decision: the 4 requests/s throttle is the HubSpot client's own guarantee, proved by the
    HubSpot client tests; here it would only make every CRM build sleep in real time. The
    client calls nothing but ``acquire()`` on its limiter, so this structural stand-in is passed
    as ``hubspot_limiter`` and counts the starts it grants.
    """

    def __init__(self) -> None:
        self.grants = 0

    async def acquire(self) -> None:
        """Grant one start without waiting."""
        self.grants += 1


class _RealLeadEmails(httpx.AsyncBaseTransport):
    """The live LinkedIn stand-in whose lead emails are real-shaped.

    The simulator gives every lead an ``@sim.invalid`` address, which a live HubSpot portal never
    receives and a simulated one answers as a simulated person. A live LinkedIn returns real
    addresses, so this transport rewrites the domain of every answer to
    :data:`REAL_EMAIL_DOMAIN`; the two domains have the same length, so nothing else in the body
    moves. Responses (lead responses and their batch re-fetch alike) stay consistent, so the
    email hash stored at ingest matches every later re-fetch.
    """

    _SIMULATED: Final = f"@{SIM_EMAIL_DOMAIN}".encode()
    _REAL: Final = f"@{REAL_EMAIL_DOMAIN}".encode()

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Answer from the simulator, with simulated email domains made real."""
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        if self._SIMULATED not in body:
            return response
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() != "content-length"
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=body.replace(self._SIMULATED, self._REAL),
            request=request,
        )


# ---------------------------------------------------------------------------------------------
# The rig: worlds, live transports, controllers and database reads
# ---------------------------------------------------------------------------------------------

type Sessions = async_sessionmaker[AsyncSession]
type Transports = Mapping[Source, httpx.AsyncBaseTransport]


@dataclasses.dataclass(slots=True)
class Rig:
    """Builds worlds, live transports and controllers, and reads dataset state back.

    Attributes:
        sessions: The application's session factory on the emptied test database (``db``).
        make_world: The conftest world builder (``Scenario.world``).
        transport_for: The conftest simulator transport builder (``mock_transport_for``).
        recording: The conftest :class:`RecordingTransport` class.
        set_env: The conftest environment setter.
        live_credentials: The conftest fake-credential setter.
        waits: Every retry wait any controller of the test asked for.
        limiter: The HubSpot throttle every controller shares.
        simulated: The world every controller simulates from (:data:`BASE` by default).
    """

    sessions: Sessions
    make_world: Callable[..., SimulatedWorld]
    transport_for: Callable[..., httpx.MockTransport]
    recording: Callable[[httpx.MockTransport], Any]
    set_env: Callable[..., None]
    live_credentials: Callable[[Source], Mapping[str, str]]
    waits: _Waits
    limiter: _InstantLimiter
    simulated: SimulatedWorld

    # -- worlds, transports, credentials and controllers -----------------------------------

    def world(
        self,
        scenario: str = BASE,
        *,
        faults: Sequence[Fault] = (),
        seed: int = 42,
        **hubspot: Any,
    ) -> SimulatedWorld:
        """Build a catalogue world on the fixed clock, with extra faults or HubSpot changes.

        Args:
            scenario: The catalogue entry whose world is built.
            faults: Faults appended to the scenario's own.
            seed: The world seed.
            **hubspot: ``HubSpotSpec`` changes, for example ``live_lead_match_share``.

        Returns:
            The world; built through the conftest ``make_world``.
        """
        entry: Scenario = SCENARIOS[scenario]
        spec = entry.spec
        if faults:
            spec = spec.adding(faults=faults)
        if hubspot:
            spec = spec.with_hubspot(**hubspot)
        if spec is not entry.spec:
            entry = dataclasses.replace(entry, spec=spec)
        return self.make_world(entry, seed, clock=fixed_clock)

    def live(
        self,
        source: Source,
        world: SimulatedWorld | None = None,
        *,
        journal: list[JournalEntry] | None = None,
    ) -> httpx.MockTransport:
        """The simulator transport of ``source`` acting as its live platform.

        Args:
            source: The source served.
            world: The platform's world; a fault-free :data:`BASE` world when omitted.
            journal: Receives one entry per request when given.
        """
        return self.transport_for(
            source, world if world is not None else self.world(), journal=journal
        )

    def go_live(self, *sources: Source) -> None:
        """Configure fake live credentials for ``sources``.

        Google's customer id is the stand-in world's own, which the simulated webhook requires.
        """
        for source in sources:
            self.live_credentials(source)
        if "google" in sources:
            self.set_env(GOOGLE_ADS_CUSTOMER_ID=self.simulated.google_customer_id)

    def controller(
        self,
        live: Transports | None = None,
        *,
        at: datetime = T0,
        world: SimulatedWorld | None = None,
    ) -> SourceModeController:
        """A controller on the current settings, as one process step at ``at`` would run it.

        Args:
            live: The live platform transport per source; absent sources would use the
                network, which the guard refuses.
            at: The step's fixed instant; it also stamps dataset activations and retirements.
            world: The simulated world; :attr:`simulated` when omitted.

        Returns:
            The controller. It holds no state of its own, so a later step simply builds
            another one with other transports or another instant.
        """
        clock = _Clock(at)
        settings = get_settings()
        return SourceModeController(
            settings,
            sessionmaker=self.sessions,
            world=world if world is not None else self.simulated,
            clock=clock,
            budget=GoogleCallBudget(settings, sessionmaker=self.sessions, clock=clock),
            transports=dict(live or {}),
            retry=RetryPolicy(sleep=self.waits),
            # Structural stand-in for the client's RateLimiter (see _InstantLimiter).
            hubspot_limiter=self.limiter,  # type: ignore[arg-type]
        )

    # -- reads ----------------------------------------------------------------------------

    async def datasets(self, source: Source | None = None) -> list[SourceDataset]:
        """Every ``source_dataset`` row (of one source), by id."""
        async with self.sessions() as session:
            statement = select(SourceDataset).order_by(SourceDataset.id)
            if source is not None:
                statement = statement.where(SourceDataset.source == source)
            return list(await session.scalars(statement))

    async def dataset(self, dataset_id: int | None) -> SourceDataset | None:
        """One ``source_dataset`` row, or ``None`` when it does not exist (discarded, purged)."""
        if dataset_id is None:
            return None
        async with self.sessions() as session:
            return await session.get(SourceDataset, dataset_id)

    async def active(self, source: Source) -> SourceDataset | None:
        """The source's active primary dataset, read through the repository's active filter."""
        async with self.sessions() as session:
            return (await repositories.active_datasets(session)).get(source)

    async def connection(self, source: Source) -> SourceConnection:
        """The source's ``source_connection`` row; it must exist."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(SourceConnection).where(SourceConnection.source == source)
            )
        assert row is not None, f"no source_connection row for {source}"
        return row

    async def freshness_row(self, dataset_id: int, name: str) -> DatasetFreshness | None:
        """One ``dataset_freshness`` row."""
        async with self.sessions() as session:
            return await session.scalar(
                select(DatasetFreshness).where(
                    DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == name
                )
            )

    async def freshness_names(self, dataset_id: int) -> set[str]:
        """The freshness dataset names a dataset has rows for."""
        async with self.sessions() as session:
            return set(
                await session.scalars(
                    select(DatasetFreshness.dataset).where(
                        DatasetFreshness.dataset_id == dataset_id
                    )
                )
            )

    async def count(self, model: Any, *where: Any) -> int:
        """``count(*)`` of ``model`` rows matching ``where``."""
        async with self.sessions() as session:
            return int(
                await session.scalar(select(func.count()).select_from(model).where(*where)) or 0
            )

    async def all(self, model: Any, *where: Any, order_by: Sequence[Any] = ()) -> list[Any]:
        """Every ``model`` row matching ``where``, ordered by ``order_by`` (default: its id)."""
        async with self.sessions() as session:
            statement = select(model).where(*where).order_by(*(order_by or (model.id,)))
            return list(await session.scalars(statement))

    async def facts(self, model: Any, dataset_id: int) -> dict[tuple[Any, ...], dict[str, Any]]:
        """Every column of one fact table's rows in ``dataset_id``, by natural key.

        Args:
            model: The fact model; its ``__natural_key__`` names the key columns.
            dataset_id: The dataset whose rows are read.

        Returns:
            Each row's whole column mapping, read from the model's table (``id``, the native
            date, money, impressions, clicks, outcome and value metrics, ``fetched_at``), keyed
            by its natural-key tuple; comparing two of them catches a rewritten value, a moved
            date or a re-stamped ``fetched_at`` that an equal row count would hide.
        """
        table = _table(model)
        key: tuple[str, ...] = model.__natural_key__
        async with self.sessions() as session:
            result = await session.execute(select(table).where(table.c.dataset_id == dataset_id))
            return {tuple(row[column] for column in key): dict(row) for row in result.mappings()}

    async def matches(self, *where: Any) -> list[LeadMatch]:
        """``lead_match`` rows matching ``where``, by lead and CRM dataset."""
        return await self.all(
            LeadMatch, *where, order_by=(LeadMatch.lead_id, LeadMatch.crm_dataset_id)
        )

    async def rows_of(self, dataset_ids: Sequence[int]) -> dict[str, int]:
        """Rows that still belong to ``dataset_ids``, per table; tables with none are omitted.

        Every dataset-scoped table is counted on ``dataset_id``, ``lead_match`` also on
        ``crm_dataset_id``, and ``source_dataset`` on its own id and its companions' parent.
        """
        ids = list(dataset_ids)
        found: dict[str, int] = {}
        async with self.sessions() as session:
            for table in _tables():
                column = table.c.get("dataset_id")
                if column is not None:
                    rows = await _count_where(session, table, column.in_(ids))
                    if rows:
                        found[table.name] = rows
            crm_rows = await _count_where(
                session, _table(LeadMatch), LeadMatch.crm_dataset_id.in_(ids)
            )
            if crm_rows:
                found["lead_match.crm_dataset_id"] = crm_rows
            datasets = await _count_where(
                session,
                _table(SourceDataset),
                SourceDataset.id.in_(ids) | SourceDataset.parent_dataset_id.in_(ids),
            )
            if datasets:
                found["source_dataset"] = datasets
        return found

    async def orphans(self) -> dict[str, int]:
        """Rows whose dataset, lead, contact or parent no longer exists, per reference.

        Counted over every dataset-scoped table (``dataset_id``), ``lead_match`` (its lead, its
        CRM dataset, its contact) and ``source_dataset`` (a companion's parent). The foreign
        keys make any such row impossible; the purge tests assert the count is zero anyway.
        """
        existing = select(SourceDataset.id)
        found: dict[str, int] = {}
        async with self.sessions() as session:
            references: list[tuple[str, Table, Any]] = []
            for table in _tables():
                column = table.c.get("dataset_id")
                if column is not None:
                    references.append((f"{table.name}.dataset_id", table, column))
            match = _table(LeadMatch)
            references.extend(
                [
                    ("lead_match.crm_dataset_id", match, LeadMatch.crm_dataset_id),
                    ("source_dataset.parent_dataset_id", _table(SourceDataset), None),
                ]
            )
            for name, table, column in references:
                if column is None:
                    condition = SourceDataset.parent_dataset_id.is_not(None) & (
                        SourceDataset.parent_dataset_id.not_in(existing)
                    )
                else:
                    condition = column.is_not(None) & column.not_in(existing)
                rows = await _count_where(session, table, condition)
                if rows:
                    found[name] = rows
            lead_orphans = await _count_where(
                session, match, LeadMatch.lead_id.not_in(select(Lead.id))
            )
            contact_orphans = await _count_where(
                session,
                match,
                LeadMatch.contact_id.is_not(None)
                & LeadMatch.contact_id.not_in(select(CrmContact.id)),
            )
        if lead_orphans:
            found["lead_match.lead_id"] = lead_orphans
        if contact_orphans:
            found["lead_match.contact_id"] = contact_orphans
        return found


def _tables() -> list[Table]:
    """Every table of the application schema, in dependency order."""
    return list(_table(SourceDataset).metadata.sorted_tables)


def _table(model: Any) -> Table:
    """The ``Table`` of an ORM model."""
    table = model.__table__
    assert isinstance(table, Table)
    return table


async def _count_where(session: AsyncSession, table: Table, condition: Any) -> int:
    """``count(*)`` of ``table`` rows matching ``condition``."""
    return int(await session.scalar(select(func.count()).select_from(table).where(condition)) or 0)


@pytest.fixture
def rig(
    db: Sessions,
    make_world: Callable[..., SimulatedWorld],
    mock_transport_for: Callable[..., httpx.MockTransport],
    recording_transport: Callable[[httpx.MockTransport], Any],
    set_env: Callable[..., None],
    live_credentials: Callable[[Source], Mapping[str, str]],
) -> Rig:
    """The rig on the emptied test database, with ``BACKFILL_DAYS`` set to a short span."""
    set_env(BACKFILL_DAYS=str(BACKFILL_DAYS))
    return Rig(
        sessions=db,
        make_world=make_world,
        transport_for=mock_transport_for,
        recording=recording_transport,
        set_env=set_env,
        live_credentials=live_credentials,
        waits=_Waits(),
        limiter=_InstantLimiter(),
        simulated=make_world(BASE, clock=fixed_clock),
    )


def _activated(outcome: BuildOutcome | None, mode: str) -> BuildOutcome:
    """Return a build outcome that must have activated a dataset of ``mode``."""
    assert outcome is not None, "a build was expected"
    assert outcome.activated, f"the {outcome.source} build did not activate: {outcome.reason}"
    assert outcome.mode == mode
    assert outcome.dataset_id is not None
    return outcome


async def _connection_states(rig: Rig) -> dict[str, tuple[str, str, str | None]]:
    """``(mode, status, reason)`` of every ``source_connection`` row, by source."""
    rows = await rig.all(SourceConnection)
    return {row.source: (row.mode, row.status, row.reason) for row in rows}


def _stale_at(row: DatasetFreshness, at: datetime) -> bool:
    """Whether items reading ``row``'s dataset are flagged stale at ``at`` (P4, AAP 0.3.2).

    The analysis's own rule (:meth:`Freshness.is_stale`) with the shipped
    ``sources.stale_after_hours``. The window ends where the row's coverage does (a
    full-state entity row has none), so only the age of its last success can make it stale.
    """
    thresholds = Thresholds.load()
    view = Freshness(
        dataset_id=row.dataset_id,
        dataset=row.dataset,
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at,
        covered_from=row.covered_from,
        covered_through=row.covered_through,
        completeness=freshness._as_completeness(row.completeness),
        reason=row.reason,
    )
    window_end = row.covered_through if row.covered_through is not None else at.date()
    return view.is_stale(
        now=at,
        stale_after=timedelta(hours=thresholds.int("sources.stale_after_hours")),
        window_end=window_end,
    )


# ---------------------------------------------------------------------------------------------
# Access unavailable and access denied (AAP 0.3.3 fallback table, rows 1 and 2)
# ---------------------------------------------------------------------------------------------


async def test_missing_credentials_resolve_simulated(rig: Rig) -> None:
    """Access unavailable: missing environment switches every source to simulated under auto.

    AAP 0.3.3: required environment variables missing for a source is "access unavailable":
    the source switches to simulated data with the reason ``credentials not configured``, is
    never probed, and is never left without data.
    """
    controller = rig.controller()
    assert controller.settings.data_mode == "auto"
    for source in SOURCES:
        assert controller.settings.missing_credentials(source), source
        resolved = await controller.resolve(source)
        assert (resolved.mode, resolved.reason, resolved.fallback, resolved.needs_probe) == (
            "simulated",
            REASON_NO_CREDENTIALS,
            False,
            False,
        ), source
        probe = await controller.probe(source)
        assert probe.passed is False
        assert probe.checks == (ProbeCheck("credentials", False, REASON_NO_CREDENTIALS),)
        assert await controller.in_fallback(source) is False
    assert await _connection_states(rig) == dict.fromkeys(
        SOURCES, ("simulated", "ok", REASON_NO_CREDENTIALS)
    )

    report = await bootstrap_sources(controller.settings, controller=controller)

    assert report.failures == {}
    for source in SOURCES:
        assert report.modes[source].mode == "simulated"
        assert report.modes[source].reason == REASON_NO_CREDENTIALS
        active = report.active[source]
        assert active is not None, f"{source} was left without data"
        assert (active.mode, active.reason) == ("simulated", REASON_NO_CREDENTIALS)
        connection = await rig.connection(source)
        assert (connection.mode, connection.reason) == ("simulated", REASON_NO_CREDENTIALS)
        assert connection.active_dataset_id == active.dataset_id
    linkedin = report.active["linkedin"]
    assert linkedin is not None
    assert linkedin.companion_id is not None, "simulated leads get their companion CRM"
    datasets = await rig.datasets()
    assert sorted((row.source, row.mode, row.state, row.role) for row in datasets) == [
        ("google", "simulated", "active", "primary"),
        ("hubspot", "simulated", "active", "companion"),
        ("hubspot", "simulated", "active", "primary"),
        ("linkedin", "simulated", "active", "primary"),
    ]
    assert all(row.reason for row in datasets)


@pytest.mark.parametrize(
    ("source", "changes", "missing"),
    [
        pytest.param(
            "google",
            {"GOOGLE_ADS_CUSTOMER_ID": None},
            ("GOOGLE_ADS_CUSTOMER_ID",),
            id="google-url-and-secret-without-customer-id",
        ),
        pytest.param(
            "google",
            {"N8N_WEBHOOK_SECRET": None},
            ("N8N_WEBHOOK_SECRET",),
            id="google-without-webhook-secret",
        ),
        pytest.param(
            "linkedin",
            {"LINKEDIN_ACCESS_TOKEN": None},
            (
                "LINKEDIN_CLIENT_ID",
                "LINKEDIN_CLIENT_SECRET",
                "LINKEDIN_REFRESH_TOKEN",
                "LINKEDIN_ACCESS_TOKEN",
            ),
            id="linkedin-account-ids-without-any-token",
        ),
        pytest.param(
            "linkedin",
            {
                "LINKEDIN_ACCESS_TOKEN": None,
                "LINKEDIN_CLIENT_ID": "test-client-id",
                "LINKEDIN_CLIENT_SECRET": "test-client-secret",
            },
            ("LINKEDIN_REFRESH_TOKEN", "LINKEDIN_ACCESS_TOKEN"),
            id="linkedin-refresh-trio-without-refresh-token",
        ),
        pytest.param(
            "linkedin",
            {"LINKEDIN_AD_ACCOUNT_IDS": None},
            ("LINKEDIN_AD_ACCOUNT_IDS",),
            id="linkedin-token-without-account-ids",
        ),
        pytest.param(
            "hubspot",
            {
                "HUBSPOT_ACCESS_TOKEN": None,
                "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY": GOOGLE_CAMPAIGN_PROPERTY,
                "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY": GOOGLE_TOUCH_PROPERTY,
            },
            ("HUBSPOT_ACCESS_TOKEN",),
            id="hubspot-crm-link-without-token",
        ),
        pytest.param("linkedin", {}, (), id="linkedin-access-token-and-account-ids-is-live"),
    ],
)
async def test_partial_credentials_count_as_missing(
    rig: Rig, source: Source, changes: Mapping[str, str | None], missing: tuple[str, ...]
) -> None:
    """Access unavailable: a source with only part of its credentials is simulated.

    AAP 0.3.3 fallback table row 1: Google needs webhook URL, secret and customer id; LinkedIn
    account ids plus either the refresh-token trio or an access token; HubSpot its token. Any
    gap is "credentials not configured"; LinkedIn with account ids and only
    ``LINKEDIN_ACCESS_TOKEN`` is complete and resolves live (behind a probe).
    """
    rig.live_credentials(source)
    rig.set_env(**changes)
    controller = rig.controller()
    assert controller.settings.missing_credentials(source) == list(missing)

    resolved = await controller.resolve(source)

    connection = await rig.connection(source)
    if missing:
        assert (resolved.mode, resolved.reason, resolved.fallback) == (
            "simulated",
            REASON_NO_CREDENTIALS,
            False,
        )
        assert (connection.mode, connection.status, connection.reason) == (
            "simulated",
            "ok",
            REASON_NO_CREDENTIALS,
        )
    else:
        # Live is wanted, but only a passing probe may build the live dataset.
        assert (resolved.mode, resolved.reason, resolved.fallback, resolved.needs_probe) == (
            "live",
            None,
            False,
            True,
        )
        assert (connection.mode, connection.status, connection.reason) == ("live", "ok", None)
    assert await rig.datasets() == []


@dataclasses.dataclass(frozen=True, slots=True)
class _Denial:
    """A live source's job that the platform refuses.

    Attributes:
        source: The live source.
        job: The job that meets the refusal.
        fault: The refusal, in the platform's wire shape.
    """

    source: Source
    job: str
    fault: Fault


@pytest.mark.parametrize(
    "denial",
    [
        pytest.param(
            _Denial(
                "linkedin",
                jobs.JOB_ENTITIES,
                dataclasses.replace(SCENARIOS[HTTP_403].spec.faults[0], code="ACCESS_DENIED"),
            ),
            id="linkedin-403",
        ),
        pytest.param(
            _Denial(
                "hubspot",
                jobs.JOB_CRM,
                Fault(
                    "hubspot",
                    "crm_search",
                    "http_status",
                    status=401,
                    code="INVALID_AUTHENTICATION",
                ),
            ),
            id="hubspot-401",
        ),
    ],
)
async def test_http_401_403_switch_to_simulated(rig: Rig, denial: _Denial) -> None:
    """Access denied: a 401/403 switches the whole source to simulated with the platform's code.

    AAP 0.3.3 fallback table row 2 and "Transitions": ``on_error`` records the denial (the
    platform's error code and the failing endpoint) and the source resolves simulated at once;
    the live dataset stays active and labelled until the simulated dataset, built and
    backfilled, activates in one transaction and retires it. The source is never data-less.
    """
    source = denial.source
    rig.go_live(source)
    healthy = rig.controller({source: rig.live(source)})
    if source == "linkedin":
        # A live LinkedIn dataset's leads need an effective CRM dataset to be staged against.
        await healthy.ensure_dataset("hubspot")
    built = _activated(await healthy.ensure_dataset(source), "live")
    assert built.reason == REASON_LIVE, "first start with credentials goes live behind a probe"
    live_id = built.dataset_id
    journal: list[JournalEntry] = []
    refusing = rig.live(source, rig.world(faults=(denial.fault,)), journal=journal)
    denied = rig.controller({source: refusing}, at=T1)

    report = await jobs.run_source_jobs(
        source, trigger="manual", jobs=[denial.job], controller=denied
    )

    (result,) = report.results
    assert (result.status, result.disposition) == ("failed", "switched_to_simulated")
    assert any(entry.status == denial.fault.status for entry in journal)
    reason = result.error
    assert reason is not None
    assert f"{source} {denial.fault.surface}" in reason, "the failing endpoint is named"
    assert denial.fault.code is not None
    assert denial.fault.code in reason, "the platform's code"
    connection = await rig.connection(source)
    assert (connection.mode, connection.status, connection.reason) == (
        "simulated",
        "denied",
        reason,
    )
    resolved = await denied.resolve(source)
    assert (resolved.mode, resolved.reason, resolved.fallback) == ("simulated", reason, True)
    # Until the simulated dataset activates, the live one stays active: never data-less.
    still = await rig.active(source)
    assert still is not None
    assert still.id == live_id

    outcome = _activated(await denied.ensure_dataset(source), "simulated")

    assert outcome.reason == reason
    assert outcome.retired == (live_id,)
    active = await rig.active(source)
    assert active is not None
    assert (active.id, active.mode, active.reason) == (outcome.dataset_id, "simulated", reason)
    retired = await rig.dataset(live_id)
    assert retired is not None
    assert (retired.state, retired.retired_at) == ("retired", T1)
    connection = await rig.connection(source)
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert connection.active_dataset_id == outcome.dataset_id
    assert await denied.in_fallback(source) is True
    if source == "linkedin":
        assert outcome.companion_id is not None
        companion = await rig.dataset(outcome.companion_id)
        assert companion is not None
        assert companion.state == "active"


@pytest.mark.parametrize("path", ["first-start-probe", "live-dataset-job"])
async def test_permission_denied_inside_http_200(rig: Rig, path: str) -> None:
    """Access denied: HTTP 200 from n8n carrying PERMISSION_DENIED switches Google to simulated.

    AAP 0.3.3 fallback table row 2: a body carrying Google ``PERMISSION_DENIED`` is a denial even
    though n8n answered HTTP 200. Whether it meets the first-start probe (Q-G1) or a job on the
    live dataset, Google ends simulated with that code in the reason.
    """
    rig.go_live("google")
    journal: list[JournalEntry] = []
    refusing = rig.live("google", rig.world(N8N_DENIED), journal=journal)
    retired: tuple[int, ...] = ()
    if path == "live-dataset-job":
        healthy = rig.controller({"google": rig.live("google")})
        live_id = _activated(await healthy.ensure_dataset("google"), "live").dataset_id
        assert live_id is not None
        retired = (live_id,)
        denied = rig.controller({"google": refusing}, at=T1)
        report = await jobs.run_source_jobs(
            "google", trigger="manual", jobs=[jobs.JOB_ENTITIES], controller=denied
        )
        (result,) = report.results
        assert (result.status, result.disposition) == ("failed", "switched_to_simulated")
    else:
        denied = rig.controller({"google": refusing})

    outcome = _activated(await denied.ensure_dataset("google"), "simulated")

    assert journal, "the webhook was called"
    assert {entry.status for entry in journal} == {200}, "every refusal arrived as HTTP 200"
    connection = await rig.connection("google")
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert connection.reason is not None
    assert "PERMISSION_DENIED" in connection.reason
    assert "google search" in connection.reason
    assert outcome.reason is not None
    assert "PERMISSION_DENIED" in outcome.reason
    assert outcome.retired == retired
    active = await rig.active("google")
    assert active is not None
    assert (active.id, active.mode) == (outcome.dataset_id, "simulated")
    assert [row.mode for row in await rig.datasets("google") if row.state == "active"] == [
        "simulated"
    ]


# ---------------------------------------------------------------------------------------------
# Forced simulation and partial permissions (AAP 0.3.3 "Modes", "Recovery readiness")
# ---------------------------------------------------------------------------------------------


async def test_data_mode_simulated_never_probes_or_promotes(rig: Rig) -> None:
    """Forced simulation: DATA_MODE=simulated never probes or promotes, even when probes pass.

    AAP 0.3.3 "Modes": ``simulated`` forces every source to simulated data; it never probes
    and never promotes a source to live. Every source here has working credentials and a live
    platform that answers every probe, yet no request reaches it and no live dataset is built.
    """
    rig.set_env(DATA_MODE="simulated")
    rig.go_live(*SOURCES)
    platform = rig.world(FORCED)
    recorders = {source: rig.recording(rig.live(source, platform)) for source in SOURCES}
    controller = rig.controller(recorders)
    assert all(controller.settings.has_credentials(source) for source in SOURCES)

    for source in SOURCES:
        resolved = await controller.resolve(source)
        assert (resolved.mode, resolved.reason, resolved.fallback) == (
            "simulated",
            REASON_FORCED,
            False,
        )
        probe = await controller.probe(source)
        assert probe.checks == (ProbeCheck("data_mode", False, REASON_FORCED),)
    report = await bootstrap_sources(controller.settings, controller=controller)
    for source in SOURCES:
        assert await controller.in_fallback(source) is False
        assert await controller.recover(source) is None
        assert await controller.ensure_dataset(source) is None, "an active dataset is kept"
    denial = AccessDenied("Forbidden", source="google", endpoint="search Q-G1", http_status=403)
    assert await controller.on_error("google", denial) == "ignored"

    assert report.failures == {}
    for source in SOURCES:
        active = report.active[source]
        assert active is not None
        assert (active.mode, active.reason) == ("simulated", REASON_FORCED)
        assert report.modes[source].mode == "simulated"
        assert recorders[source].requests == [], f"{source} was called live"
    assert await rig.count(SourceDataset, SourceDataset.mode == "live") == 0
    assert {row.mode for row in await rig.all(SourceConnection)} == {"simulated"}

    # The same platforms pass every probe once DATA_MODE=auto allows probing at all.
    rig.set_env(DATA_MODE="auto")
    auto = rig.controller(recorders)
    for source in SOURCES:
        assert (await auto.probe(source)).passed, source
        assert recorders[source].requests, source


async def test_lead_sync_403_keeps_linkedin_simulated(rig: Rig) -> None:
    """Partial permissions: LinkedIn ads readable while Lead Sync returns 403 stay simulated.

    AAP 0.3.3 "Granularity" and "Recovery readiness": a 403 on Lead Sync alone (no
    ``r_marketing_leadgen_automation``) keeps all of LinkedIn simulated, because simulated
    leads cannot reference real creatives. The recovery probe passes ``adAccounts``, the one-day
    ``adAnalytics`` and ``leadForms`` but fails ``leadFormResponses``: a partial success
    changes nothing, so repeated probes never build a dataset and never flip the source.
    """
    rig.go_live("linkedin")
    (account,) = get_settings().linkedin_ad_account_ids
    journal: list[JournalEntry] = []
    platform = rig.live("linkedin", rig.world(LEADSYNC_403), journal=journal)
    controller = rig.controller({"linkedin": platform})

    first = _activated(await controller.ensure_dataset("linkedin"), "simulated")

    assert first.reason is not None
    assert f"leadFormResponses {account}" in first.reason
    assert first.companion_id is not None
    assert await controller.in_fallback("linkedin") is True
    simulated_id = first.dataset_id
    before = [(row.id, row.state) for row in await rig.datasets()]

    for step in range(3):
        later = rig.controller({"linkedin": platform}, at=T0 + timedelta(minutes=30 * (step + 1)))
        report = await later.probe("linkedin")
        assert report.passed is False
        assert [(check.name, check.ok) for check in report.checks] == [
            (f"adAccounts/{account}", True),
            (f"adAnalytics {account}", True),
            (f"leadForms {account}", True),
            (f"leadFormResponses {account}", False),
        ]
        (failed,) = report.failed
        assert failed.reason is not None
        assert f"linkedin {LEAD_SYNC_SURFACE}" in failed.reason
        assert await later.recover("linkedin") is None
        assert await later.ensure_dataset("linkedin") is None

    assert [(row.id, row.state) for row in await rig.datasets()] == before
    assert await rig.count(SourceDataset, SourceDataset.state == "building") == 0
    active = await rig.active("linkedin")
    assert active is not None
    assert (active.id, active.mode) == (simulated_id, "simulated")
    connection = await rig.connection("linkedin")
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert connection.active_dataset_id == simulated_id
    lead_sync_calls = [entry for entry in journal if entry.surface == LEAD_SYNC_SURFACE]
    assert lead_sync_calls
    assert {entry.status for entry in lead_sync_calls} == {403}


# ---------------------------------------------------------------------------------------------
# Recovery and build failure (AAP 0.3.3 "Recovery readiness", "Transitions")
# ---------------------------------------------------------------------------------------------


async def _google_in_fallback(rig: Rig) -> int:
    """Put Google into automatic fallback at :data:`T0` and return its simulated dataset.

    Credentials are configured, but the webhook refuses Q-G1 (``PERMISSION_DENIED`` in an HTTP
    200), so the first-start probe fails and a simulated dataset is built in its place.
    """
    rig.go_live("google")
    refusing = rig.controller({"google": rig.live("google", rig.world(N8N_DENIED))})
    simulated = _activated(await refusing.ensure_dataset("google"), "simulated")
    assert await refusing.in_fallback("google") is True
    assert simulated.dataset_id is not None
    return simulated.dataset_id


@dataclasses.dataclass(slots=True)
class _BackfillProbe:
    """What the build looked like while ``jobs.backfill_dataset`` ran, read from the database.

    Attributes:
        dataset_id: The dataset being backfilled.
        state: Its state when the backfill started.
        mode: Its mode.
        active_id: The source's active primary when the backfill started.
        connection_dataset_id: ``source_connection.active_dataset_id`` at that moment.
        connection_mode: ``source_connection.mode`` at that moment.
        rows_written: The build's rows once the backfill returned, per table.
    """

    dataset_id: int
    state: str
    mode: str
    active_id: int | None
    connection_dataset_id: int | None
    connection_mode: str
    rows_written: dict[str, int] = dataclasses.field(default_factory=dict)


def _observe_backfill(
    monkeypatch: pytest.MonkeyPatch, rig: Rig, source: Source
) -> list[_BackfillProbe]:
    """Wrap ``jobs.backfill_dataset`` so every build records its state mid-build.

    ``sync/mode.py`` imports the backfill inside each build, so patching the module attribute
    reaches every build of the test. The real backfill runs unchanged.
    """
    observed: list[_BackfillProbe] = []
    backfill = jobs.backfill_dataset

    async def observing(
        controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> jobs.SourceSyncReport:
        """Record the build's state, run the real backfill, then record what it wrote."""
        dataset = await rig.dataset(dataset_id)
        assert dataset is not None
        active = await rig.active(source)
        connection = await rig.connection(source)
        probe = _BackfillProbe(
            dataset_id=dataset_id,
            state=dataset.state,
            mode=dataset.mode,
            active_id=None if active is None else active.id,
            connection_dataset_id=connection.active_dataset_id,
            connection_mode=connection.mode,
        )
        observed.append(probe)
        report = await backfill(controller, dataset_id, now=now)
        probe.rows_written = await rig.rows_of([dataset_id])
        return report

    monkeypatch.setattr(jobs, "backfill_dataset", observing)
    return observed


async def test_recovery_builds_live_dataset_activated_after_backfill(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery: when every capability passes, a live dataset is activated only after its backfill.

    AAP 0.3.3 "Recovery readiness" and "Transitions": the recovery probe of a source in
    automatic fallback passes, a ``building`` live dataset is created and backfilled while the
    simulated dataset stays active (and is what ``source_connection`` points at), and one
    activation then makes the live dataset active and retires the simulated one. The first
    entity sync of the new dataset writes the baseline decision of every campaign and shared
    budget.
    """
    simulated_id = await _google_in_fallback(rig)
    observed = _observe_backfill(monkeypatch, rig, "google")
    platform = rig.live("google", rig.world(RECOVERY))
    recovering = rig.controller({"google": platform}, at=T1)

    outcome = _activated(await recovering.recover("google"), "live")

    (during,) = observed
    assert (during.dataset_id, during.state, during.mode) == (
        outcome.dataset_id,
        "building",
        "live",
    )
    assert during.active_id == simulated_id, "the simulated dataset stays active mid-build"
    assert (during.connection_dataset_id, during.connection_mode) == (simulated_id, "simulated")
    assert during.rows_written, "the backfill wrote the live dataset before it activated"
    assert outcome.reason == REASON_RECOVERED
    assert outcome.retired == (simulated_id,)
    live = await rig.dataset(outcome.dataset_id)
    old = await rig.dataset(simulated_id)
    assert live is not None
    assert old is not None
    assert (live.state, live.mode, live.reason, live.activated_at) == (
        "active",
        "live",
        REASON_RECOVERED,
        T1,
    )
    assert (old.state, old.retired_at) == ("retired", live.activated_at), "one activation"
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        outcome.dataset_id,
    )
    assert await recovering.in_fallback("google") is False

    campaigns = {row.id for row in await rig.all(Campaign, Campaign.dataset_id == live.id)}
    shared = {
        row.id
        for row in await rig.all(
            Budget, Budget.dataset_id == live.id, Budget.explicitly_shared.is_(True)
        )
    }
    assert campaigns, "the live backfill stored the campaigns"
    baselines = await rig.all(
        Decision, Decision.dataset_id == live.id, Decision.origin == "baseline"
    )
    assert {row.subject_id for row in baselines if row.subject_type == "campaign"} == campaigns
    assert {row.subject_id for row in baselines if row.subject_type == "budget"} == shared


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        pytest.param(
            Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G3"}),
            "not ready: freshness unknown: google.metrics_daily",
            id="persistent-5xx",
        ),
        pytest.param(
            Fault("google", "search", "http_status", status=403, match={"query_id": "Q-G3"}),
            "google search Q-G3",
            id="403-mid-backfill",
        ),
        pytest.param(
            Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G4"}),
            "not ready: backfill incomplete: google.entities (interrupted: ",
            id="interrupted-entity-backfill",
        ),
    ],
)
async def test_build_failure_discards_building_dataset(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, fault: Fault, reason: str
) -> None:
    """Recovery: a build failure discards the building dataset and leaves the simulated one active.

    AAP 0.3.3 "Recovery readiness": the probe (Q-G1) passes, so a live dataset is built; its
    metrics backfill (Q-G3) then keeps failing (a persistent 5xx) or is refused (a 403), or
    its entity backfill's ad-group scan (Q-G4) keeps failing, so the backfill never completes
    although every other query succeeds. The building dataset is discarded with every row it
    wrote, the simulated dataset stays the active one and no live dataset is activated,
    nothing of the discarded build is visible through the active-dataset filter, and the
    source stays in automatic fallback for the next recovery probe.

    The catalogue's LinkedIn build-failure world (``fallback.build_failure``) fails
    ``adAnalytics``, which the LinkedIn recovery probe also calls, so it can never reach a build;
    the same failure is therefore placed on a Google backfill query the probe does not run.
    """
    simulated_id = await _google_in_fallback(rig)
    observed = _observe_backfill(monkeypatch, rig, "google")
    journal: list[JournalEntry] = []
    platform = rig.live("google", rig.world(faults=(fault,)), journal=journal)
    recovering = rig.controller({"google": platform}, at=T1)

    outcome = await recovering.recover("google")

    assert outcome is not None
    assert (outcome.mode, outcome.activated) == ("live", False)
    (during,) = observed
    assert (during.dataset_id, during.state) == (outcome.dataset_id, "building")
    assert during.rows_written, "the build had stored entities before it failed"
    assert await rig.dataset(outcome.dataset_id) is None, "the building dataset was discarded"
    assert await rig.rows_of([during.dataset_id]) == {}, "with every row it wrote"
    failed_calls = [entry for entry in journal if entry.status == fault.status]
    assert len(failed_calls) == (3 if fault.status == 503 else 1), "retries only the transient"
    async with rig.sessions() as session:
        active = await repositories.active_datasets(session)
        assert active["google"].id == simulated_id
        assert await repositories.active_dataset_id(session, "google") == simulated_id
    survivor = await rig.dataset(simulated_id)
    assert survivor is not None
    assert (survivor.state, survivor.mode) == ("active", "simulated")
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        simulated_id,
    )
    # A persistent 5xx leaves the metrics freshness unrecorded, and a Q-G4 scan cut short
    # leaves google.entities incomplete with its unread work, so the build is not ready and
    # enters fallback as a failed live build; a 403 is recorded as the denial itself.
    assert connection.reason is not None
    assert reason in connection.reason
    assert outcome.reason is not None
    assert reason in outcome.reason
    assert await recovering.in_fallback("google") is True
    assert [row.id for row in await rig.datasets("google")] == [simulated_id]


async def _probe_rows(rig: Rig, source: Source = "google") -> list[SyncRun]:
    """Every ``probe`` ``sync_run`` row of ``source``, oldest first."""
    return await rig.all(
        SyncRun,
        SyncRun.source == source,
        SyncRun.job == JOB_PROBE,
        order_by=(SyncRun.started_at, SyncRun.id),
    )


async def test_bootstrap_probe_is_recorded_and_spaces_the_first_recovery_probe(rig: Rig) -> None:
    """Recovery: the first recovery probe follows a bootstrap probe's fallback 30 minutes later.

    AAP 0.3.2 ("Source-mode recovery probe: every 30 min while a source is in automatic
    fallback") and AAP 0.2.4 (every rejected Google call still spends the developer-token
    quota other n8n workflows share). The worker's bootstrap probes Google once (Q-G1, refused
    with ``PERMISSION_DENIED`` in an HTTP 200) and enters automatic fallback; that probe is a
    ``probe`` ``sync_run`` started at the bootstrap and closed ``failed`` with the denial, so
    the scheduler's next recovery probe is not due 5 minutes later and is due 31 minutes
    later.
    """
    rig.go_live("google")
    journal: list[JournalEntry] = []
    refusing = rig.live("google", rig.world(N8N_DENIED), journal=journal)
    controller = rig.controller({"google": refusing})

    report = await bootstrap_sources(controller.settings, controller=controller)

    assert report.failures == {}
    assert report.modes["google"].fallback is True
    assert [entry.surface for entry in journal] == ["search"], "one Q-G1 request"
    (row,) = await _probe_rows(rig)
    assert (row.started_at, row.finished_at, row.status, row.dataset) == (T0, T0, "failed", None)
    assert row.error is not None
    assert row.error.startswith("Q-G1: google search Q-G1: ")
    assert "PERMISSION_DENIED" in row.error
    for source in ("hubspot", "linkedin"):
        assert await _probe_rows(rig, source) == [], f"{source} has no credentials: no probe"
    for after, due in ((timedelta(minutes=5), False), (timedelta(minutes=31), True)):
        at = T0 + after
        scheduler = Scheduler(
            controller=rig.controller({"google": refusing}, at=at), clock=_Clock(at)
        )
        assert await scheduler._probe_due("google", at) is due, after
    assert len(journal) == 1, "deciding due-ness sends nothing"


async def test_probe_in_the_defect_state_runs_once_per_interval_and_records_each_outcome(
    rig: Rig,
) -> None:
    """Recovery: a source left in the defect state is probed again only every 30 minutes.

    AAP 0.3.2 (probe cadence) and AAP 0.3.3 (a defect is never retried or mocked). A Q-G1 400
    leaves Google live with status ``error`` and no data, so it resolves live with a probe
    needed on every wake. Its first probe's ``probe`` row spaces the next: a wake 5 minutes and
    another 29 min 59 s later send nothing and change nothing; 30 minutes later Google is
    probed again. Once the platform answers, the next due probe passes, the live dataset
    activates, and that probe's row ends ``succeeded``.
    """
    rig.go_live("google")
    journal: list[JournalEntry] = []
    defect = Fault("google", "search", "http_status", status=400, match={"query_id": "Q-G1"})
    defective = rig.live("google", rig.world(faults=(defect,)), journal=journal)
    assert await rig.controller({"google": defective}).ensure_dataset("google") is None
    connection = await rig.connection("google")
    assert connection.status == "error"
    attempted = (connection.status, connection.reason, connection.last_attempt_at)

    for after in (timedelta(minutes=5), PROBE_INTERVAL - timedelta(seconds=1)):
        spaced = rig.controller({"google": defective}, at=T0 + after)
        assert await spaced.ensure_dataset("google") is None
    assert len(journal) == 1, "no probe inside the interval"
    connection = await rig.connection("google")
    assert (connection.status, connection.reason, connection.last_attempt_at) == attempted
    assert [row.started_at for row in await _probe_rows(rig)] == [T0]

    again = T0 + PROBE_INTERVAL
    assert await rig.controller({"google": defective}, at=again).ensure_dataset("google") is None
    assert [entry.status for entry in journal] == [400, 400]
    assert (await rig.connection("google")).last_attempt_at == again
    assert await rig.datasets() == [], "still no mock"

    healed = T0 + 2 * PROBE_INTERVAL
    healthy = rig.controller({"google": rig.live("google")}, at=healed)
    live = _activated(await healthy.ensure_dataset("google"), "live")

    assert live.reason == REASON_LIVE
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        live.dataset_id,
    )
    rows = await _probe_rows(rig)
    assert [(row.started_at, row.status) for row in rows] == [
        (T0, "failed"),
        (again, "failed"),
        (healed, "succeeded"),
    ]
    assert rows[-1].error is None
    assert all(row.error is not None and "Q-G1" in row.error for row in rows[:-1])


async def test_first_live_probe_row_fails_with_the_failed_builds_reason(rig: Rig) -> None:
    """Recovery: a first-start probe that passes ends its row ``failed`` when the build fails.

    AAP 0.3.3 "Recovery readiness" ("a failure during the build discards the building
    dataset"): Q-G1 passes but every metrics read (Q-G3) answers 503, so the live build is not
    ready, is discarded, and Google falls back. The probe's row is ``succeeded`` only for a
    live dataset that activated, so it ends ``failed`` with the build's reason.
    """
    rig.go_live("google")
    fault = Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G3"})
    controller = rig.controller({"google": rig.live("google", rig.world(faults=(fault,)))})

    outcome = _activated(await controller.ensure_dataset("google"), "simulated")

    assert await controller.in_fallback("google") is True
    (row,) = await _probe_rows(rig)
    assert (row.started_at, row.status) == (T0, "failed")
    assert row.error is not None
    assert outcome.reason is not None
    assert row.error == outcome.reason
    assert row.error.startswith("live build failed: not ready: ")


@pytest.mark.parametrize("raised", [RuntimeError, asyncio.CancelledError])
async def test_first_live_probe_row_closes_failed_when_its_build_raises(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, raised: type[BaseException]
) -> None:
    """Recovery: a probe whose live build raises, or is cancelled, leaves no ``running`` row.

    The ``probe`` row is committed before the probe; an error out of the live build (a broken
    backfill here) closes it ``failed`` with the error's class name, a cancellation with
    ``probe cancelled``, and the error then propagates unchanged; the build is discarded.
    """
    rig.go_live("google")
    controller = rig.controller({"google": rig.live("google")})

    async def broken(
        controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> jobs.SourceSyncReport:
        """Fail the backfill the way an unexpected error or a cancellation would."""
        del controller, dataset_id, now  # Nothing is backfilled.
        raise raised()

    monkeypatch.setattr(jobs, "backfill_dataset", broken)

    with pytest.raises(raised):
        await controller.ensure_dataset("google")

    (row,) = await _probe_rows(rig)
    expected = "probe cancelled" if raised is asyncio.CancelledError else raised.__name__
    assert (row.started_at, row.finished_at, row.status, row.error) == (
        T0,
        T0,
        "failed",
        expected,
    )
    assert await rig.datasets() == [], "the building dataset was discarded"


async def test_frozen_run_renders_after_purge(rig: Rig, authed_client: httpx.AsyncClient) -> None:
    """Recovery: frozen runs still render after a purge, as historical-only items.

    AAP 0.3.2 "Items from replaced data are historical-only" and AAP 0.3.3 "Run history": a run
    computed on simulated LinkedIn data keeps its evidence after LinkedIn recovers to live and
    the retired simulated dataset (with its companion) is purged. Its entity keys are set to
    NULL (``ON DELETE SET NULL``), its provenance still says ``simulated``, and the API serves
    every such item as historical and held, so every action on it is disabled. Items that read
    only still-active datasets stay current.
    """
    world = rig.world(DEFAULT)
    accounts = ",".join(world.linkedin_account_ids)
    rig.go_live("linkedin")
    rig.set_env(LINKEDIN_AD_ACCOUNT_IDS=accounts)
    lead_sync_refused = rig.world(DEFAULT, faults=SCENARIOS[LEADSYNC_403].spec.faults)
    first = rig.controller({"linkedin": rig.live("linkedin", lead_sync_refused)}, world=world)
    report = await bootstrap_sources(first.settings, controller=first)
    assert report.failures == {}
    simulated = report.active["linkedin"]
    assert simulated is not None
    assert simulated.mode == "simulated"
    assert simulated.companion_id is not None
    replaced = {simulated.dataset_id, simulated.companion_id}

    window = await runner.default_window(now=T0)
    run = await runner.run_analysis(window, "default", "manual", now=T0)
    stored = {row.id: row for row in await rig.all(Recommendation, Recommendation.run_id == run.id)}

    def reads_replaced(evidence: Mapping[str, Any]) -> bool:
        """Whether an item's frozen provenance names the simulated LinkedIn data."""
        return any(entry["dataset_id"] in replaced for entry in evidence["provenance"])

    linkedin_items = {key for key, row in stored.items() if reads_replaced(row.evidence)}
    assert linkedin_items, "the run holds items computed from simulated LinkedIn data"
    assert any(stored[key].campaign_id is not None for key in linkedin_items)
    assert len(linkedin_items) < len(stored), "and items from other sources"

    recovering = rig.controller({"linkedin": rig.live("linkedin", world)}, at=T1, world=world)
    recovered = _activated(await recovering.recover("linkedin"), "live")
    assert set(recovered.retired) == replaced
    purged = await DatasetManager(rig.sessions, clock=_after_retention(T1)).purge_retired()
    assert purged == [simulated.dataset_id]
    assert await rig.dataset(simulated.companion_id) is None, "the companion went with it"

    after = {row.id: row for row in await rig.all(Recommendation, Recommendation.run_id == run.id)}
    assert after.keys() == stored.keys(), "run history is never purged"
    for key in linkedin_items:
        row = after[key]
        keys = (row.campaign_id, row.budget_id, row.ad_group_id, row.keyword_id, row.creative_id)
        assert keys == (None, None, None, None, None)
        assert row.evidence == stored[key].evidence, "frozen evidence is untouched"

    response = await authed_client.get("/api/v1/recommendations", params={"run_id": run.id})

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["run"]["id"] == run.id
    items = [
        item
        for section in page["sections"]
        for group in section["groups"]
        for item in group["items"]
    ]
    assert {item["id"] for item in items} == set(stored)
    for item in items:
        evidence = item["evidence"]
        current = item["current"]
        if item["id"] in linkedin_items:
            frozen = stored[item["id"]].evidence
            assert evidence["provenance"] == frozen["provenance"]
            assert evidence["window"]["start"] == frozen["window"]["start"]
            assert evidence["window"]["end"] == frozen["window"]["end"]
            assert [metric["key"] for metric in evidence["metrics"]] == [
                metric["key"] for metric in frozen["metrics"]
            ]
            linkedin = [entry for entry in evidence["provenance"] if entry["source"] == "linkedin"]
            assert [entry["mode"] for entry in linkedin] == ["simulated"]
            assert item["data_status"] == "simulated"
            assert current["historical"] is True
            assert current["held_now"] is True
            assert current["held_reason_now"] == HISTORICAL_REASON
        else:
            assert current["historical"] is False, item["rule_id"]


# ---------------------------------------------------------------------------------------------
# Cross-dataset purge (AAP 0.3.3 "Retirement and purge"; AAP 0.5.2 foreign-key actions)
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class _LiveLeads:
    """A live LinkedIn dataset whose leads were matched in two HubSpot datasets.

    Attributes:
        simulated_crm: The simulated HubSpot primary the leads were matched in first; retired
            at :data:`T1`.
        live_crm: The live HubSpot primary that replaced it, matching the same leads; active.
        leads: The live LinkedIn primary; active.
        lead_ids: Its leads.
        real_emails: The live LinkedIn stand-in, for later steps.
        portal: The live HubSpot stand-in, for later steps.
    """

    simulated_crm: int
    live_crm: int
    leads: int
    lead_ids: frozenset[int]
    real_emails: httpx.AsyncBaseTransport
    portal: httpx.AsyncBaseTransport


def _portal(rig: Rig, scenario: str = BASE) -> httpx.MockTransport:
    """The live HubSpot stand-in: another seed, and a contact for every real lead email.

    A different seed makes the portal's contact attributes differ from the simulated portal's,
    so a read can tell the two datasets apart even where both answer the same seeded id.
    """
    return rig.live("hubspot", rig.world(scenario, seed=7, live_lead_match_share=Decimal(1)))


async def _live_leads_on_two_crm_datasets(rig: Rig) -> _LiveLeads:
    """Build :class:`_LiveLeads` through the product's own flows.

    At :data:`T0` HubSpot has no credentials (simulated primary) and LinkedIn goes live; its
    leads are staged and matched in the simulated HubSpot primary. At :data:`T1` HubSpot gets
    credentials: its live build stages the same leads again, matches them in the live portal
    and activates, retiring the simulated primary with its matching state.
    """
    rig.go_live("linkedin")
    real_emails = _RealLeadEmails(rig.live("linkedin"))
    first = rig.controller({"linkedin": real_emails})
    simulated_crm = _activated(await first.ensure_dataset("hubspot"), "simulated")
    leads = _activated(await first.ensure_dataset("linkedin"), "live")
    assert simulated_crm.dataset_id is not None
    assert leads.dataset_id is not None
    rig.go_live("hubspot")
    portal = _portal(rig)
    second = rig.controller({"linkedin": real_emails, "hubspot": portal}, at=T1)
    live_crm = _activated(await second.ensure_dataset("hubspot"), "live")
    assert live_crm.dataset_id is not None
    assert live_crm.retired == (simulated_crm.dataset_id,)
    lead_ids = frozenset(row.id for row in await rig.all(Lead, Lead.dataset_id == leads.dataset_id))
    assert lead_ids, "the live LinkedIn backfill stored leads"
    return _LiveLeads(
        simulated_crm=simulated_crm.dataset_id,
        live_crm=live_crm.dataset_id,
        leads=leads.dataset_id,
        lead_ids=lead_ids,
        real_emails=real_emails,
        portal=portal,
    )


def _match_states(rows: Sequence[LeadMatch]) -> list[tuple[object, ...]]:
    """Every column of each ``lead_match`` row, for before/after comparison."""
    return [
        (
            row.lead_id,
            row.crm_dataset_id,
            row.status,
            row.attempts,
            row.next_attempt_at,
            row.last_checked_at,
            row.contact_id,
            row.contact_read_status,
            row.reason,
        )
        for row in rows
    ]


async def test_purge_retired_hubspot_deletes_only_its_matches(rig: Rig) -> None:
    """Cross-dataset purge: purging a retired HubSpot dataset deletes only its own lead matches.

    AAP 0.3.3 "Retirement and purge": the one ``DELETE FROM source_dataset`` of a retired
    HubSpot dataset cascades to its contacts, meetings, deals, associations, association
    fetches, freshness and ``lead_match`` rows, and to nothing else. The active LinkedIn leads
    keep the rows staged for the active CRM dataset, unchanged and never requeued as pending; no
    foreign-key error is raised and no orphan row remains.
    """
    setup = await _live_leads_on_two_crm_datasets(rig)
    retired_crm = await rig.rows_of([setup.simulated_crm])
    for table in (
        "crm_contact",
        "crm_association_fetch",
        "dataset_freshness",
        "lead_match.crm_dataset_id",
    ):
        assert retired_crm.get(table), f"the retired HubSpot dataset holds {table} rows"
    retired_matches = await rig.matches(LeadMatch.crm_dataset_id == setup.simulated_crm)
    active_matches = await rig.matches(LeadMatch.crm_dataset_id == setup.live_crm)
    assert {row.lead_id for row in retired_matches} == setup.lead_ids
    assert {row.lead_id for row in active_matches} == setup.lead_ids
    assert any(row.status == "matched" for row in active_matches)
    assert all(row.status != "pending" for row in active_matches)
    active_before = _match_states(active_matches)
    kept_before = {key: await rig.rows_of([key]) for key in (setup.live_crm, setup.leads)}

    purged = await DatasetManager(rig.sessions, clock=_after_retention(T1)).purge_retired()

    assert purged == [setup.simulated_crm]
    assert await rig.rows_of([setup.simulated_crm]) == {}
    assert _match_states(await rig.matches(LeadMatch.crm_dataset_id == setup.live_crm)) == (
        active_before
    )
    assert {key: await rig.rows_of([key]) for key in kept_before} == kept_before
    assert await rig.count(LeadMatch, LeadMatch.status == "pending") == 0
    assert await rig.orphans() == {}


async def test_purge_retired_linkedin_removes_leads_and_all_their_matches(rig: Rig) -> None:
    """Cross-dataset purge: purging a retired LinkedIn dataset removes its leads and their matches.

    AAP 0.3.3 "Retirement and purge": ``lead_match`` is the only table linking two datasets, and
    its keys to ``lead`` are ``ON DELETE CASCADE``. Leads matched in two CRM datasets (a retired
    simulated HubSpot primary and the active live one) lose every match when their retired
    LinkedIn dataset is purged, while the active CRM dataset keeps its contacts, meetings, deals
    and associations. No foreign-key error is raised and no orphan row remains.
    """
    setup = await _live_leads_on_two_crm_datasets(rig)
    # LinkedIn loses access at T2: a refused entity sync, then simulated data replaces the
    # live dataset, which retires with its leads.
    refusing = rig.live("linkedin", rig.world(HTTP_403))
    denied = rig.controller({"linkedin": refusing, "hubspot": setup.portal}, at=T2)
    report = await jobs.run_source_jobs(
        "linkedin", trigger="manual", jobs=[jobs.JOB_ENTITIES], controller=denied
    )
    assert report.results[0].disposition == "switched_to_simulated"
    replacement = _activated(await denied.ensure_dataset("linkedin"), "simulated")
    assert replacement.retired == (setup.leads,)
    assert replacement.dataset_id is not None
    assert replacement.companion_id is not None
    matches = await rig.matches(LeadMatch.lead_id.in_(setup.lead_ids))
    assert {row.crm_dataset_id for row in matches} == {setup.simulated_crm, setup.live_crm}
    live_crm_before = await rig.rows_of([setup.live_crm])
    contacts_before = [
        (row.id, row.hs_id, row.email_sha256, row.lifecycle_stage, row.mql_at, row.ql_at)
        for row in await rig.all(CrmContact, CrmContact.dataset_id == setup.live_crm)
    ]
    assert contacts_before, "the live CRM dataset holds the matched contacts"
    replacement_before = await rig.rows_of([replacement.dataset_id, replacement.companion_id])

    purged = await DatasetManager(rig.sessions, clock=_after_retention(T2)).purge_retired()

    assert purged == sorted([setup.simulated_crm, setup.leads])
    assert await rig.count(Lead, Lead.id.in_(setup.lead_ids)) == 0
    assert await rig.matches(LeadMatch.lead_id.in_(setup.lead_ids)) == []
    assert await rig.rows_of([setup.leads, setup.simulated_crm]) == {}
    # The live CRM dataset lost only the match rows of the purged leads.
    expected = {
        table: rows
        for table, rows in live_crm_before.items()
        if table != "lead_match.crm_dataset_id"
    }
    assert await rig.rows_of([setup.live_crm]) == expected
    contacts_after = [
        (row.id, row.hs_id, row.email_sha256, row.lifecycle_stage, row.mql_at, row.ql_at)
        for row in await rig.all(CrmContact, CrmContact.dataset_id == setup.live_crm)
    ]
    assert contacts_after == contacts_before
    after = await rig.rows_of([replacement.dataset_id, replacement.companion_id])
    assert after == replacement_before
    assert await rig.orphans() == {}


async def test_companion_purged_with_parent(rig: Rig) -> None:
    """Cross-dataset purge: a companion is purged with its parent.

    AAP 0.3.3 "Companions": ``parent_dataset_id`` is ``ON DELETE CASCADE``, so one purge of a
    retired simulated LinkedIn dataset removes its companion CRM dataset with every row the
    companion holds (contacts, associations, fetches, freshness, ``lead_match``), while the
    HubSpot primary and the live LinkedIn dataset that replaced the pair stay as they were.
    """
    rig.go_live("linkedin")
    first = rig.controller({"linkedin": rig.live("linkedin", rig.world(LEADSYNC_403))})
    crm = _activated(await first.ensure_dataset("hubspot"), "simulated")
    parent = _activated(await first.ensure_dataset("linkedin"), "simulated")
    assert parent.dataset_id is not None
    assert parent.companion_id is not None
    recovering = rig.controller({"linkedin": rig.live("linkedin", rig.world(RECOVERY))}, at=T1)
    live = _activated(await recovering.recover("linkedin"), "live")
    assert sorted(live.retired) == sorted([parent.dataset_id, parent.companion_id])
    held = await rig.rows_of([parent.companion_id])
    for table in ("crm_contact", "dataset_freshness", "lead_match.crm_dataset_id"):
        assert held.get(table), f"the companion holds {table} rows"
    assert crm.dataset_id is not None
    assert live.dataset_id is not None
    kept_before = {key: await rig.rows_of([key]) for key in (crm.dataset_id, live.dataset_id)}

    purged = await DatasetManager(rig.sessions, clock=_after_retention(T1)).purge_retired()

    assert purged == [parent.dataset_id]
    assert await rig.dataset(parent.dataset_id) is None
    assert await rig.dataset(parent.companion_id) is None
    assert await rig.rows_of([parent.dataset_id, parent.companion_id]) == {}
    assert {key: await rig.rows_of([key]) for key in kept_before} == kept_before
    assert await rig.orphans() == {}


# ---------------------------------------------------------------------------------------------
# Companion CRM dataset and isolation (AAP 0.3.3 "Cross-source joins with simulation")
# ---------------------------------------------------------------------------------------------


async def test_companion_crm_when_linkedin_simulated_hubspot_live(rig: Rig) -> None:
    """Companion CRM dataset: LinkedIn simulated while HubSpot is live reads the companion.

    AAP 0.3.3: building a simulated LinkedIn dataset also builds a companion
    ``source_dataset(source=hubspot, mode=simulated, role=companion, parent=<LinkedIn>)`` holding
    the generated contacts and ``lead_match`` rows and its own ``hubspot.contacts``,
    ``hubspot.associations`` and ``hubspot.deal_search`` freshness. LinkedIn-attributed
    evidence reads the companion; no simulated lead joins a live contact, and no
    ``@sim.invalid`` address is ever sent to the live portal.
    """
    rig.go_live("hubspot")
    portal = rig.recording(rig.live("hubspot", rig.world(COMPANION)))
    controller = rig.controller({"hubspot": portal})

    report = await bootstrap_sources(controller.settings, controller=controller)

    assert report.failures == {}
    crm, linkedin = report.active["hubspot"], report.active["linkedin"]
    assert crm is not None
    assert crm.mode == "live"
    assert linkedin is not None
    assert linkedin.mode == "simulated"
    assert linkedin.companion_id is not None
    companion = await rig.dataset(linkedin.companion_id)
    assert companion is not None
    assert (
        companion.source,
        companion.mode,
        companion.role,
        companion.parent_dataset_id,
        companion.state,
    ) == ("hubspot", "simulated", "companion", linkedin.dataset_id, "active")
    assert await controller.datasets.effective_crm_dataset(linkedin.dataset_id) == companion.id
    async with rig.sessions() as session:
        assert await repositories.effective_crm_dataset_id(session, linkedin.dataset_id) == (
            companion.id
        )
        assert await repositories.active_dataset_id(session, "hubspot") == crm.dataset_id

    lead_ids = {row.id for row in await rig.all(Lead, Lead.dataset_id == linkedin.dataset_id)}
    assert lead_ids, "the simulated LinkedIn backfill stored leads"
    matches = await rig.matches(LeadMatch.lead_id.in_(lead_ids))
    assert {row.lead_id for row in matches} == lead_ids, "every simulated lead was staged"
    assert {row.crm_dataset_id for row in matches} == {companion.id}
    companion_contacts = {
        row.id for row in await rig.all(CrmContact, CrmContact.dataset_id == companion.id)
    }
    matched = [row for row in matches if row.status == "matched"]
    assert matched, "simulated leads matched simulated contacts"
    assert all(row.contact_id in companion_contacts for row in matched)
    assert await rig.count(LeadMatch, LeadMatch.crm_dataset_id == crm.dataset_id) == 0
    assert {"hubspot.contacts", "hubspot.associations", "hubspot.deal_search"} <= (
        await rig.freshness_names(companion.id)
    )

    assert portal.requests, "the live portal was probed and backfilled"
    assert rig.limiter.grants >= len(portal.requests), "every live request passed the throttle"
    simulated_domain = f"@{SIM_EMAIL_DOMAIN}"
    for request in portal.requests:
        assert simulated_domain not in request.content.decode(), request.url.path
        assert simulated_domain not in str(request.url), request.url.path


async def test_google_to_crm_reads_live_primary(rig: Rig) -> None:
    """Companion CRM dataset: the Google-to-CRM link reads the live primary, not the companion.

    AAP 0.3.3 "Google-to-CRM link" and "Cross-source joins with simulation": with both Google
    contact properties configured and verified on the live portal, Google-touch contacts are
    discovered into HubSpot's live primary dataset only; the companion of the simulated
    LinkedIn dataset never runs that discovery, even though its simulated portal knows the
    properties too. The analysis context reads Google-attributed CRM evidence from the live
    primary and LinkedIn-attributed evidence from the companion.
    """
    rig.set_env(
        HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=GOOGLE_CAMPAIGN_PROPERTY,
        HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=GOOGLE_TOUCH_PROPERTY,
    )
    rig.go_live("hubspot")
    link: dict[str, Any] = {
        "google_crm_properties": (GOOGLE_CAMPAIGN_PROPERTY, GOOGLE_TOUCH_PROPERTY),
        "google_touch_contacts": 20,
    }
    rig.simulated = rig.world(COMPANION, **link)
    portal = rig.live("hubspot", rig.world(COMPANION, **link))
    controller = rig.controller({"hubspot": portal})

    report = await bootstrap_sources(controller.settings, controller=controller)

    assert report.failures == {}
    crm, linkedin = report.active["hubspot"], report.active["linkedin"]
    assert crm is not None
    assert crm.mode == "live"
    assert linkedin is not None
    assert linkedin.companion_id is not None
    companion_id = linkedin.companion_id
    bindings = await controller.datasets.load_crm_bindings(crm.dataset_id)
    assert bindings is not None
    assert bindings.google.available, "both properties verified"
    discovered = await rig.all(
        CrmContact, CrmContact.dataset_id == crm.dataset_id, CrmContact.google_touch_at.is_not(None)
    )
    assert discovered, "Google-touch contacts were discovered in the live primary"
    assert all(row.google_campaign_ref for row in discovered)
    assert (
        await rig.count(
            CrmContact,
            CrmContact.dataset_id == companion_id,
            CrmContact.google_touch_at.is_not(None),
        )
        == 0
    )
    assert "hubspot.google_contacts" in await rig.freshness_names(crm.dataset_id)
    assert "hubspot.google_contacts" not in await rig.freshness_names(companion_id)

    async with rig.sessions() as session:
        window = await runner.default_window(now=T0, session=session)
        context = await metrics.load_context(
            session, window, controller.thresholds, kind="default", now=T0
        )
    assert context.google_crm is not None
    assert context.linkedin_crm is not None
    google_crm = context.google_crm.dataset
    assert (google_crm.dataset_id, google_crm.mode, google_crm.role) == (
        crm.dataset_id,
        "live",
        "primary",
    )
    linkedin_crm = context.linkedin_crm.dataset
    assert (linkedin_crm.dataset_id, linkedin_crm.role) == (companion_id, "companion")
    google_contacts = {contact.hs_id for contact in context.google_crm.google_contacts}
    assert google_contacts, "the window holds Google-touch contacts"
    assert google_contacts <= {row.hs_id for row in discovered}


async def test_google_discovery_commits_with_the_contact_refresh_one_rerun_per_transaction(
    rig: Rig,
) -> None:
    """Companion CRM dataset: the live primary's later CRM sync commits its Google discovery.

    AAP 0.3.3 "Google-to-CRM link" and 0.3.2 "Events that trigger a rerun": with both Google
    contact properties configured and verified, a later CRM sync of HubSpot's active live
    primary runs Google-touch discovery again and records ``hubspot.google_contacts`` as a
    success together with contacts, the deal search and associations. Discovery's contacts
    and outcome commit in the contact refresh's transaction, so the four datasets are
    published in three transactions, and the sync queues three pending default-window
    requests, one per transaction (:func:`_crm_requests`), which the worker coalesces into
    one run.
    """
    rig.set_env(
        HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=GOOGLE_CAMPAIGN_PROPERTY,
        HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=GOOGLE_TOUCH_PROPERTY,
    )
    rig.go_live("hubspot")
    link: dict[str, Any] = {
        "google_crm_properties": (GOOGLE_CAMPAIGN_PROPERTY, GOOGLE_TOUCH_PROPERTY),
        "google_touch_contacts": 20,
    }
    rig.simulated = rig.world(COMPANION, **link)
    controller = rig.controller({"hubspot": rig.live("hubspot", rig.world(COMPANION, **link))})
    report = await bootstrap_sources(controller.settings, controller=controller)
    assert report.failures == {}
    crm = report.active["hubspot"]
    assert crm is not None
    primary = await rig.dataset(crm.dataset_id)
    assert primary is not None
    assert (primary.mode, primary.role, primary.state) == ("live", "primary", "active")
    bindings = await controller.datasets.load_crm_bindings(crm.dataset_id)
    assert bindings is not None
    assert bindings.google.available, "both properties verified"
    earlier = {row.id for row in await rig.all(AnalysisRequest)}
    later = rig.controller({"hubspot": rig.live("hubspot", rig.world(COMPANION, **link))}, at=T1)

    result = await jobs.refresh_crm_dataset(later, crm.dataset_id)

    assert result is not None
    assert (result.job, result.dataset_id, result.status) == (
        jobs.JOB_CRM,
        crm.dataset_id,
        "succeeded",
    )
    succeeded = {name for name, outcome in result.freshness.items() if outcome.succeeded}
    assert succeeded == {
        "hubspot.contacts",
        "hubspot.google_contacts",
        "hubspot.deal_search",
        "hubspot.associations",
    }
    for name in sorted(succeeded):
        row = await rig.freshness_row(crm.dataset_id, name)
        assert row is not None, name
        assert row.last_success_at == T1, name
    requests = await _new_requests(rig, earlier)
    assert requests == _crm_requests(result)
    # Four datasets succeeded in three publication transactions: three requests, not four.
    assert requests == [(f"sync:hubspot.{jobs.JOB_CRM}", None, None)] * 3


async def test_companion_activates_and_retires_with_parent(rig: Rig) -> None:
    """Companion CRM dataset: the companion activates and retires with its parent.

    AAP 0.3.3 "Companion dataset": the companion activates and retires in the same transaction
    as its simulated LinkedIn parent. It is activated at the parent's instant, and when LinkedIn
    recovers to live, the one activation of the live dataset retires parent and companion at
    the same instant.
    """
    rig.go_live("linkedin")
    first = rig.controller({"linkedin": rig.live("linkedin", rig.world(LEADSYNC_403))})
    _activated(await first.ensure_dataset("hubspot"), "simulated")
    built = _activated(await first.ensure_dataset("linkedin"), "simulated")
    parent, companion = await rig.dataset(built.dataset_id), await rig.dataset(built.companion_id)
    assert parent is not None
    assert companion is not None
    assert companion.parent_dataset_id == parent.id
    assert (parent.state, companion.state) == ("active", "active")
    assert parent.activated_at == companion.activated_at == T0

    recovering = rig.controller({"linkedin": rig.live("linkedin", rig.world(RECOVERY))}, at=T1)
    live = _activated(await recovering.recover("linkedin"), "live")

    assert sorted(live.retired) == sorted([parent.id, companion.id])
    parent, companion = await rig.dataset(parent.id), await rig.dataset(companion.id)
    replacement = await rig.dataset(live.dataset_id)
    assert parent is not None
    assert companion is not None
    assert replacement is not None
    assert (parent.state, companion.state) == ("retired", "retired")
    assert parent.retired_at == companion.retired_at == replacement.activated_at == T1
    assert parent.activated_at == companion.activated_at == T0


async def test_simulated_hubspot_ids_never_collide_and_leave_on_activation(
    rig: Rig, contract: Callable[..., Any]
) -> None:
    """Isolation: simulated HubSpot contacts never collide with live ids and leave on activation.

    AAP 0.3.3 "HubSpot simulated while LinkedIn is live": the simulated portal answers a
    deterministic share of real lead emails with seeded contacts whose ids come from the
    reserved block (``HUBSPOT_ID_RANGE``, orders of magnitude above live HubSpot ids, such as
    those of HubSpot's own documented sample), keyed by email hash. When a live HubSpot dataset
    activates, the simulated one retires with them, the effective CRM dataset is the live one,
    and the analysis context holds no contact, match or stage date of the simulated dataset.

    The live portal here is itself a simulator stand-in (another seed), so it answers from the
    same reserved block; what keeps the two apart is the dataset, which this test reads.
    """
    rig.go_live("linkedin")
    real_emails = _RealLeadEmails(rig.live("linkedin", rig.world(ISOLATION)))
    first = rig.controller({"linkedin": real_emails})
    simulated_crm = _activated(await first.ensure_dataset("hubspot"), "simulated").dataset_id
    leads_built = _activated(await first.ensure_dataset("linkedin"), "live")
    assert leads_built.dataset_id is not None
    leads = leads_built.dataset_id
    lead_rows = await rig.all(Lead, Lead.dataset_id == leads)
    assert lead_rows
    assert all(row.email_sha256 for row in lead_rows)

    simulated_contacts = {
        row.id: row for row in await rig.all(CrmContact, CrmContact.dataset_id == simulated_crm)
    }
    assert simulated_contacts
    for row in simulated_contacts.values():
        assert is_simulated_id("hubspot", row.hs_id)
        assert HUBSPOT_ID_RANGE.contains(row.hs_id)
    documented = contract("hubspot", "crm_search").response.documented_sample["results"]
    live_ids = {str(result["id"]) for result in documented}
    assert live_ids
    assert not any(is_simulated_id("hubspot", value) for value in live_ids)
    assert live_ids.isdisjoint(row.hs_id for row in simulated_contacts.values())

    # A deterministic share of the real lead emails has a seeded contact in the simulated portal.
    expected: dict[int, str] = {}
    for lead in lead_rows:
        record = rig.simulated.hashed_contact_for(str(lead.email_sha256))
        if record is not None:
            assert HUBSPOT_HASHED_CONTACT_IDS.contains(record.id)
            expected[lead.id] = record.id
    assert 0 < len(expected) < len(lead_rows), "a share, neither none nor all"
    first_matches = await rig.matches(LeadMatch.crm_dataset_id == simulated_crm)
    matched = {
        row.lead_id: simulated_contacts[row.contact_id].hs_id
        for row in first_matches
        if row.status == "matched" and row.contact_id is not None
    }
    assert matched == expected

    rig.go_live("hubspot")
    portal = _portal(rig, ISOLATION)
    second = rig.controller({"linkedin": real_emails, "hubspot": portal}, at=T1)
    live_crm = _activated(await second.ensure_dataset("hubspot"), "live")

    assert live_crm.retired == (simulated_crm,)
    retired = await rig.dataset(simulated_crm)
    assert retired is not None
    assert retired.state == "retired"
    live_contacts = {
        row.hs_id: row
        for row in await rig.all(CrmContact, CrmContact.dataset_id == live_crm.dataset_id)
    }
    live_matches = {
        row.lead_id: row
        for row in await rig.matches(LeadMatch.crm_dataset_id == live_crm.dataset_id)
    }
    async with rig.sessions() as session:
        assert await repositories.effective_crm_dataset_id(session, leads) == live_crm.dataset_id
        window = await runner.default_window(now=T1, session=session)
        context = await metrics.load_context(
            session, window, second.thresholds, kind="default", now=T1
        )
    assert context.linkedin_crm is not None
    assert context.linkedin_crm.dataset.dataset_id == live_crm.dataset_id
    assert context.linkedin_crm.dataset.mode == "live"
    assert context.google_crm is not None
    assert context.google_crm.dataset.dataset_id == live_crm.dataset_id
    assert context.linkedin_crm.contacts, "the live portal's contacts are read"
    for hs_id, contact in context.linkedin_crm.contacts.items():
        stored = live_contacts[hs_id]
        assert (contact.lifecycle_stage, contact.mql_at, contact.ql_at) == (
            stored.lifecycle_stage,
            stored.mql_at,
            stored.ql_at,
        )
    assert context.leads
    for lead in context.leads:
        row = live_matches[lead.id]
        assert lead.match_status == row.status
        expected_hs_id = None
        if row.contact_id is not None:
            expected_hs_id = next(
                hs_id for hs_id, stored in live_contacts.items() if stored.id == row.contact_id
            )
        assert lead.contact_hs_id == expected_hs_id
    # The simulated portal matched only a share; the context's matches are the live portal's.
    window_leads = {lead.id for lead in context.leads}
    in_context = {lead.id for lead in context.leads if lead.match_status == "matched"}
    assert in_context != {lead_id for lead_id in expected if lead_id in window_leads}


async def _new_requests(
    rig: Rig, earlier: set[int]
) -> list[tuple[str, datetime | None, int | None]]:
    """``(reason, picked_at, run_id)`` of every ``analysis_request`` not in ``earlier``, by id.

    A pending default-window request has neither ``picked_at`` nor ``run_id``.
    """
    return [
        (row.reason, row.picked_at, row.run_id)
        for row in await rig.all(AnalysisRequest)
        if row.id not in earlier
    ]


_CRM_PUBLICATIONS: Final[tuple[tuple[str, ...], ...]] = (
    ("hubspot.contacts", "hubspot.google_contacts"),
    ("hubspot.deal_search",),
    ("hubspot.associations",),
)
"""The CRM sync's publication transactions, in step order, by the freshness datasets each
records: the contact refresh (with Google-touch discovery's outcome when discovery runs), the
deal search, and the associations with their target reads."""


def _crm_requests(result: jobs.JobResult) -> list[tuple[str, datetime | None, int | None]]:
    """The pending ``sync:hubspot.crm`` requests a CRM sync of an active primary queued.

    AAP 0.3.2 "Events that trigger a rerun": a completed CRM sync queues a default-window
    request, and the worker coalesces the requests that arrive within 60 s into one run. The
    sync publishes in three transactions (:data:`_CRM_PUBLICATIONS`): the contact refresh
    records ``hubspot.contacts`` and, when Google-touch discovery runs,
    ``hubspot.google_contacts`` in the same commit; the deal search records
    ``hubspot.deal_search``; the associations record ``hubspot.associations``. Each
    transaction that records a success queues one request with its write, and the matching
    step queues none. A success reaches ``result.freshness`` only once its transaction
    commits, so the requests count the publication transactions with a success there, never
    the datasets: four datasets but three requests when discovery runs. The helper serves
    syncs that refreshed contacts, the deal search and associations (asserted), so each of
    the three transactions recorded a success.
    """
    succeeded = {name for name, outcome in result.freshness.items() if outcome.succeeded}
    assert {"hubspot.contacts", "hubspot.associations", "hubspot.deal_search"} <= succeeded
    assert succeeded <= {name for group in _CRM_PUBLICATIONS for name in group}, succeeded
    published = [group for group in _CRM_PUBLICATIONS if succeeded.intersection(group)]
    return [(f"sync:hubspot.{jobs.JOB_CRM}", None, None)] * len(published)


async def test_crm_refresh_hydrates_a_live_linkedin_datasets_matches_in_the_active_primary(
    rig: Rig,
) -> None:
    """Recovery: a live LinkedIn build's late matches get the active primary's CRM sync.

    AAP 0.3.3 "Staged matching on a CRM dataset change": when a live LinkedIn build's leads
    are matched in HubSpot's active primary after its backfill, ``jobs.refresh_crm_dataset``
    runs that primary's CRM sync, so the matched contacts get their contact refresh,
    associations and target reads before the LinkedIn dataset activates. The primary is
    active, so the completed sync queues its default-window analysis requests, one per
    publication transaction it commits (AAP 0.3.2 "Events that trigger a rerun";
    :func:`_crm_requests`); a building HubSpot dataset's sync queues none. A live HubSpot
    dataset that may not be synced now (no HubSpot credentials) is left alone, and a dataset
    of another source is refused.
    """
    rig.go_live("linkedin")
    real_emails = _RealLeadEmails(rig.live("linkedin", rig.world(ISOLATION)))
    controller = rig.controller({"linkedin": real_emails})
    crm = _activated(await controller.ensure_dataset("hubspot"), "simulated").dataset_id
    leads = _activated(await controller.ensure_dataset("linkedin"), "live").dataset_id
    assert crm is not None
    assert leads is not None
    assert await controller.datasets.unhydrated_matches(crm, lead_dataset_id=leads) == 0
    # As a matching pass alone leaves them: matched, with no association read of the contacts.
    async with rig.sessions() as session, session.begin():
        await session.execute(
            delete(CrmAssociationFetch).where(
                CrmAssociationFetch.dataset_id == crm,
                CrmAssociationFetch.object_type == "contact",
            )
        )
    assert await controller.datasets.unhydrated_matches(crm, lead_dataset_id=leads) > 0
    earlier = {row.id for row in await rig.all(AnalysisRequest)}

    result = await jobs.refresh_crm_dataset(controller, crm)

    assert result is not None
    assert (result.job, result.source, result.dataset_id, result.status) == (
        jobs.JOB_CRM,
        "hubspot",
        crm,
        "succeeded",
    )
    assert await controller.datasets.unhydrated_matches(crm, lead_dataset_id=leads) == 0
    assert await _new_requests(rig, earlier) == _crm_requests(result)
    earlier = {row.id for row in await rig.all(AnalysisRequest)}

    building = await controller.datasets.begin("hubspot", "simulated", reason=REASON_FORCED)
    built = await jobs.refresh_crm_dataset(controller, building)
    assert built is not None
    assert (built.dataset_id, built.status) == (building, "succeeded")
    live = await controller.datasets.begin("hubspot", "live", reason=REASON_RECOVERED)
    runs = await rig.count(SyncRun)
    assert await jobs.refresh_crm_dataset(controller, live) is None
    assert await rig.count(SyncRun) == runs
    with pytest.raises(ValueError, match="the CRM sync runs in a HubSpot dataset"):
        await jobs.refresh_crm_dataset(controller, leads)
    assert await _new_requests(rig, earlier) == [], "a building dataset's sync queues none"


@pytest.mark.parametrize("step", ["backfill", "late-refresh"])
async def test_active_crm_sync_queues_its_rerun_when_the_linkedin_recovery_build_is_voided(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Recovery: a live LinkedIn build's CRM sync of the active primary queues its own rerun.

    AAP 0.3.2 "Events that trigger a rerun" and 0.3.3 "Recovery readiness": LinkedIn's
    recovery build runs a CRM sync of HubSpot's active primary, the CRM data in use: in its
    backfill (``backfill``), or, when HubSpot replaced its primary after that backfill, as
    the refresh that hydrates the build's late matches there (``late-refresh``). A LinkedIn
    denial recorded right after that sync voids the build: it is discarded, the previous
    simulated LinkedIn dataset stays active and the connection's mode does not change, so
    neither an activation nor a mode change queues a rerun. The completed CRM sync has
    queued its own pending default-window ``analysis_request`` rows, one per publication
    transaction it committed (:func:`_crm_requests`), so the current recommendations do not
    keep the CRM evidence it replaced. The build's matching passes queue none.
    """
    rig.go_live("linkedin")
    refusing = rig.controller({"linkedin": rig.live("linkedin", rig.world(LEADSYNC_403))})
    _activated(await refusing.ensure_dataset("hubspot"), "simulated")
    previous = _activated(await refusing.ensure_dataset("linkedin"), "simulated").dataset_id
    assert await refusing.in_fallback("linkedin") is True
    recovering = rig.controller(
        {"linkedin": _RealLeadEmails(rig.live("linkedin", rig.world(ISOLATION)))}, at=T1
    )
    replacing = rig.controller(at=T1)
    backfill = jobs.backfill_dataset
    refresh = jobs.refresh_crm_dataset
    builds: list[int] = []
    replaced: list[int] = []
    synced: list[jobs.JobResult] = []
    earlier: set[int] = set()
    before_build: set[int] = set()

    async def deny(controller: SourceModeController) -> None:
        """Record a LinkedIn denial against the build, as another LinkedIn call would."""
        denial = AccessDenied(
            "Denied during recovery",
            source="linkedin",
            endpoint=LEAD_SYNC_SURFACE,
            http_status=403,
        )
        disposition = await controller.on_error("linkedin", denial, dataset_id=builds[0])
        assert disposition == "switched_to_simulated"

    async def backfilling(
        controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> jobs.SourceSyncReport:
        """Run the real backfill; deny after it, or have HubSpot replace its primary."""
        dataset = await rig.dataset(dataset_id)
        assert dataset is not None
        if (dataset.source, dataset.mode) != ("linkedin", "live"):
            return await backfill(controller, dataset_id, now=now)
        builds.append(dataset_id)
        before_build.update(row.id for row in await rig.all(AnalysisRequest))
        if step == "backfill":
            earlier.update(before_build)
        report = await backfill(controller, dataset_id, now=now)
        if step == "backfill":
            synced.extend(result for result in report.results if result.job == jobs.JOB_CRM)
            await deny(controller)
        else:
            replacement = _activated(
                await replacing.build_dataset("hubspot", "simulated", reason=REASON_NO_CREDENTIALS),
                "simulated",
            )
            assert replacement.dataset_id is not None
            replaced.append(replacement.dataset_id)
        return report

    async def refreshing(
        controller: SourceModeController, crm_dataset_id: int, *, now: datetime | None = None
    ) -> jobs.JobResult | None:
        """Run the real CRM refresh, then deny."""
        earlier.update(row.id for row in await rig.all(AnalysisRequest))
        result = await refresh(controller, crm_dataset_id, now=now)
        assert result is not None
        synced.append(result)
        await deny(controller)
        return result

    monkeypatch.setattr(jobs, "backfill_dataset", backfilling)
    monkeypatch.setattr(jobs, "refresh_crm_dataset", refreshing)

    outcome = await recovering.recover("linkedin")

    assert outcome is not None
    assert (outcome.source, outcome.mode, outcome.activated) == ("linkedin", "live", False)
    assert [outcome.dataset_id] == builds
    assert await rig.dataset(outcome.dataset_id) is None, "the voided build was discarded"
    active = await rig.active("linkedin")
    assert active is not None
    assert (active.id, active.mode, active.state) == (previous, "simulated", "active")
    connection = await rig.connection("linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        previous,
    )
    crm = await rig.active("hubspot")
    assert crm is not None
    assert replaced == ([crm.id] if step == "late-refresh" else [])
    [result] = synced
    assert (result.source, result.dataset_id, result.status) == ("hubspot", crm.id, "succeeded")
    # In both steps the window holds only the requests of the one CRM sync in ``synced``: in
    # ``late-refresh`` it opens at the refresh, after the backfill's own CRM sync of the
    # replaced primary and the replacement's activation queued theirs.
    assert await _new_requests(rig, earlier) == _crm_requests(result)
    # The build's matching passes (its backfill's inactive LeadMatchJob, and the late-restaged
    # pass in the replacement primary) queue no request of their own.
    since_build = {reason for reason, _, _ in await _new_requests(rig, before_build)}
    assert f"sync:hubspot.{jobs.JOB_LEAD_MATCH}" not in since_build


# ---------------------------------------------------------------------------------------------
# Transient errors, defects and the call budget (AAP 0.3.3 fallback table, rows 3 and 4)
# ---------------------------------------------------------------------------------------------


def _on_linkedin_analytics(scenario: str) -> Fault:
    """The catalogue scenario's transient fault, placed on LinkedIn's daily ``adAnalytics``.

    Decision: the catalogue spreads its three transient faults over three surfaces; the
    guarantee "the affected datasets turn stale and keep their last good data" is observable as
    such on a dated dataset, so each fault (429 with its ``Retry-After``, 503, timeout) is
    replayed on the daily creative analytics a LinkedIn metrics job reads.
    """
    (fault,) = SCENARIOS[scenario].spec.faults
    return dataclasses.replace(fault, source="linkedin", surface="ad_analytics")


async def _live_linkedin(rig: Rig) -> int:
    """Make LinkedIn live at :data:`T0` (HubSpot simulated for its leads) and return its dataset."""
    rig.go_live("linkedin")
    healthy = rig.controller({"linkedin": rig.live("linkedin")})
    _activated(await healthy.ensure_dataset("hubspot"), "simulated")
    dataset_id = _activated(await healthy.ensure_dataset("linkedin"), "live").dataset_id
    assert dataset_id is not None
    return dataset_id


async def _live_google(rig: Rig) -> int:
    """Make Google live at :data:`T0` and return its dataset."""
    rig.go_live("google")
    healthy = rig.controller({"google": rig.live("google")})
    dataset_id = _activated(await healthy.ensure_dataset("google"), "live").dataset_id
    assert dataset_id is not None
    return dataset_id


@pytest.mark.parametrize("scenario", [TRANSIENT_429, TRANSIENT_5XX, TIMEOUT])
async def test_transient_errors_retry_then_stale_never_mock(rig: Rig, scenario: str) -> None:
    """Transient: 429, 5xx and timeouts retry three times with backoff, then stale; never mock.

    AAP 0.3.3 fallback table row 3: a transient failure gets 3 attempts with exponential
    backoff (``Retry-After`` honoured), then the affected dataset turns stale: its last success,
    coverage and facts stay, the failed attempt and its reason are recorded, the connection is
    ``stale``, the source stays live and no simulated dataset is built. "Facts stay" is proved
    on values, not counts: every stored creative-day row keeps its key, id, spend,
    impressions, clicks, lead and conversion outcomes and ``fetched_at``, so nothing was
    rewritten.
    """
    fault = _on_linkedin_analytics(scenario)
    attempts = SCENARIOS[scenario].expect.values["attempts"]
    dataset_id = await _live_linkedin(rig)
    before = await rig.freshness_row(dataset_id, "linkedin.analytics_daily")
    assert before is not None
    assert before.last_success_at == T0
    facts_before = await rig.count(FactCreativeDaily, FactCreativeDaily.dataset_id == dataset_id)
    assert facts_before, "the live backfill stored daily creative facts"
    stored_before = await rig.facts(FactCreativeDaily, dataset_id)
    assert len(stored_before) == facts_before
    assert any(row["cost"] for row in stored_before.values()), "spend to keep"
    assert any(row["leads"] for row in stored_before.values()), "lead outcomes to keep"
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    rig.waits.seconds.clear()
    journal: list[JournalEntry] = []
    failing = rig.live("linkedin", rig.world(faults=(fault,)), journal=journal)
    controller = rig.controller({"linkedin": failing}, at=T1)

    report = await jobs.run_source_jobs(
        "linkedin", trigger="manual", jobs=[jobs.JOB_METRICS], controller=controller
    )

    (result,) = report.results
    assert (result.status, result.disposition) == ("failed", "stale")
    assert result.error is not None
    assert "linkedin ad_analytics" in result.error
    analytics_calls = [entry for entry in journal if entry.surface == "ad_analytics"]
    assert len(analytics_calls) == attempts == 3
    assert {entry.status for entry in analytics_calls} == {fault.status}
    assert len(rig.waits.seconds) == attempts - 1, "a backoff wait before each retry"
    if fault.retry_after is not None:
        # The platform's Retry-After replaces the backoff.
        assert rig.waits.seconds == [float(fault.retry_after)] * (attempts - 1)
    else:
        # Exponential backoff from the policy's base, plus at most its jitter.
        policy = RetryPolicy()
        for retry, waited in enumerate(rig.waits.seconds):
            backoff = policy.base_seconds * 2**retry
            assert backoff <= waited <= backoff + policy.jitter_seconds
    after = await rig.freshness_row(dataset_id, "linkedin.analytics_daily")
    assert after is not None
    assert after.last_success_at == before.last_success_at, "the last good data stands"
    assert after.last_attempt_at == T1
    assert (after.covered_from, after.covered_through, after.completeness) == (
        before.covered_from,
        before.covered_through,
        before.completeness,
    )
    assert after.reason is not None
    assert "linkedin ad_analytics" in after.reason
    connection = await rig.connection("linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "stale",
        dataset_id,
    )
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before, "never mock"
    assert (
        await rig.count(FactCreativeDaily, FactCreativeDaily.dataset_id == dataset_id)
        == facts_before
    )
    assert await rig.facts(FactCreativeDaily, dataset_id) == stored_before, (
        "the last good creative facts stand, every key and value unchanged"
    )


_GOOGLE_ENTITY_QUERIES: Final = ("Q-G1", "Q-G2", "Q-G4", "Q-G7", "Q-G8")
"""The queries an entity sync of a dataset with a stored account sends; Q-G2b only follows a
complete Q-G2 scan, so a failing scan sends none."""


async def test_a_google_entity_sync_whose_every_read_fails_turns_stale(rig: Rig) -> None:
    """Transient: an entity sync whose every read fails keeps its last success and turns stale.

    AAP 0.3.3 fallback table row 3 ("then the affected datasets turn stale ... Last good data
    is kept. Never mock") with "Safe absence": Q-G1, Q-G2, Q-G4, Q-G7 and Q-G8 each answer 503
    on all three attempts. The sync infers nothing (no snapshot, no ``missing`` discrepancy)
    and records ``google.entities`` ``incomplete`` with the interruption's repairable reason.
    It read nothing, so it is no successful refresh: ``last_success_at`` stays the build's and
    only ``last_attempt_at`` moves, which makes the dataset stale 24 h after the build
    although the failed sync ran an hour later (AAP 0.3.2). The source stays live with a
    ``stale`` connection and no simulated dataset; the next healthy sync reads the
    outstanding work and moves ``last_success_at`` again.
    """
    assert Thresholds.load().int("sources.stale_after_hours") == 24, "DAY_AFTER assumes 24 h"
    dataset_id = await _live_google(rig)
    before = await rig.freshness_row(dataset_id, "google.entities")
    assert before is not None
    assert (before.last_success_at, before.completeness) == (T0, "complete")
    discrepancies_before = await rig.count(Discrepancy)
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    faults = tuple(
        Fault(
            source="google",
            surface="search",
            kind="http_status",
            status=503,
            match={"query_id": query_id},
        )
        for query_id in _GOOGLE_ENTITY_QUERIES
    )
    journal: list[JournalEntry] = []
    failing = rig.live("google", rig.world(faults=faults), journal=journal)

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"google": failing}, at=T1),
    )

    (result,) = report.results
    assert (result.status, result.completeness, result.disposition) == (
        "succeeded",
        "incomplete",
        "stale",
    )
    assert len(journal) == 3 * len(_GOOGLE_ENTITY_QUERIES), "three attempts per query"
    assert {entry.status for entry in journal} == {503}
    row = await rig.freshness_row(dataset_id, "google.entities")
    assert row is not None
    assert (row.last_attempt_at, row.last_success_at) == (T1, T0), "the last success stands"
    assert row.completeness == "incomplete"
    assert row.reason is not None
    assert row.reason.startswith(freshness.REASON_INTERRUPTED)
    assert freshness.needs_repair(row)
    assert not _stale_at(row, T1)
    assert _stale_at(row, DAY_AFTER), "stale 24 h after the last success, not the attempt"
    assert await _snapshots_at(rig, dataset_id, "campaign", T1) == 0, "nothing was read"
    assert await rig.count(Discrepancy) == discrepancies_before, "nothing was inferred"
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "stale",
        dataset_id,
    )
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before, "never mock"

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"google": rig.live("google")}, at=T2),
    )

    (recovered,) = report.results
    assert (recovered.status, recovered.completeness) == ("succeeded", "complete")
    row = await rig.freshness_row(dataset_id, "google.entities")
    assert row is not None
    assert (row.last_attempt_at, row.last_success_at, row.completeness, row.checkpoint) == (
        T2,
        T2,
        "complete",
        None,
    )
    assert not _stale_at(row, DAY_AFTER)


async def test_a_linkedin_entity_sync_cut_short_by_lead_sync_turns_stale(rig: Rig) -> None:
    """Transient: a Lead Sync 503 cuts the LinkedIn entity sync short; its last success stands.

    AAP 0.3.3 fallback table row 3: ``leadForms`` answering 503 on all three attempts stops
    the lead-form listing. What was read is stored, and ``linkedin.entities`` is
    ``incomplete`` with the platform error's own reason, which, unlike Google's, carries no
    repairable prefix. The read is still no successful refresh: ``last_success_at`` stays
    the build's, so the dataset is stale 24 h after it (AAP 0.3.2). The source stays live
    with a ``stale`` connection and no simulated dataset; the next healthy entity sync moves
    ``last_success_at`` again.
    """
    dataset_id = await _live_linkedin(rig)
    before = await rig.freshness_row(dataset_id, "linkedin.entities")
    assert before is not None
    assert (before.last_success_at, before.completeness) == (T0, "complete")
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    lead_forms_down = Fault(source="linkedin", surface="lead_forms", kind="http_status", status=503)
    journal: list[JournalEntry] = []
    failing = rig.live("linkedin", rig.world(faults=(lead_forms_down,)), journal=journal)

    report = await jobs.run_source_jobs(
        "linkedin",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"linkedin": failing}, at=T1),
    )

    (result,) = report.results
    assert (result.status, result.completeness, result.disposition) == (
        "succeeded",
        "incomplete",
        "stale",
    )
    form_calls = [entry for entry in journal if entry.surface == "lead_forms"]
    assert form_calls, "the lead-form listing was requested"
    assert len(form_calls) % 3 == 0, "three attempts per account"
    assert {entry.status for entry in form_calls} == {503}
    row = await rig.freshness_row(dataset_id, "linkedin.entities")
    assert row is not None
    assert (row.last_attempt_at, row.last_success_at) == (T1, T0), "the last success stands"
    assert row.completeness == "incomplete"
    assert row.reason is not None
    assert "lead_forms" in row.reason
    assert not freshness.is_repairable_reason(row.reason), "LinkedIn states the error itself"
    assert not _stale_at(row, T1)
    assert _stale_at(row, DAY_AFTER), "stale 24 h after the last success, not the attempt"
    connection = await rig.connection("linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "stale",
        dataset_id,
    )
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before, "never mock"

    report = await jobs.run_source_jobs(
        "linkedin",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"linkedin": rig.live("linkedin")}, at=T2),
    )

    (recovered,) = report.results
    assert (recovered.status, recovered.completeness) == ("succeeded", "complete")
    row = await rig.freshness_row(dataset_id, "linkedin.entities")
    assert row is not None
    assert (row.last_attempt_at, row.last_success_at, row.completeness) == (T2, T2, "complete")
    assert not _stale_at(row, DAY_AFTER)


async def test_query_defect_fails_job_without_retry(rig: Rig) -> None:
    """Defects: a 400 fails the job with its query id, with no retry and no mock.

    AAP 0.3.3 fallback table row 4: HTTP 400 (a GAQL ``QueryError``) is a defect in the
    product. The job fails and its ``sync_run`` rows record the query id; the request is sent
    exactly once; the source stays live (its status ``error``) and no simulated data appears.
    The defect fails every metrics dataset of the run, so every dated fact table it writes
    (campaign, keyword and search-term days) keeps each stored row's key, id, spend,
    impressions, clicks, conversion outcomes and ``fetched_at`` exactly.
    """
    query_id = SCENARIOS[DEFECT_400].expect.values["query_id"]
    assert isinstance(query_id, str)
    dataset_id = await _live_google(rig)
    facts_before = await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
    assert facts_before
    stored_before = {
        model: await rig.facts(model, dataset_id)
        for model in (FactCampaignDaily, FactKeywordDaily, FactSearchTermDaily)
    }
    assert len(stored_before[FactCampaignDaily]) == facts_before
    for model, rows in stored_before.items():
        assert rows, f"the live build stored {model.__tablename__} rows"
        assert any(row["cost"] for row in rows.values()), f"{model.__tablename__} spend to keep"
    assert any(row["conversions"] for row in stored_before[FactCampaignDaily].values())
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    rig.waits.seconds.clear()
    journal: list[JournalEntry] = []
    defective = rig.live("google", rig.world(DEFECT_400), journal=journal)
    controller = rig.controller({"google": defective}, at=T1)

    report = await jobs.run_source_jobs(
        "google", trigger="manual", jobs=[jobs.JOB_METRICS], controller=controller
    )

    (result,) = report.results
    assert (result.status, result.disposition) == ("failed", "defect")
    assert result.error is not None
    assert query_id in result.error
    assert report.has_defect
    assert [entry.status for entry in journal].count(400) == 1, "never retried"
    assert result.calls == len(journal) == 1, "the defective call is the job's call"
    assert rig.waits.seconds == []
    runs = await rig.all(SyncRun, SyncRun.job == jobs.JOB_METRICS, SyncRun.started_at == T1)
    assert runs, "the job recorded its sync_run rows"
    for run in runs:
        assert run.status == "failed"
        assert run.error is not None
        assert query_id in run.error
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "error",
        dataset_id,
    )
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before, "no mock"
    assert (
        await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
        == facts_before
    )
    for model, rows in stored_before.items():
        assert await rig.facts(model, dataset_id) == rows, (
            f"{model.__tablename__}: every key and value of the last good facts unchanged"
        )


UNICODE_ECHO: Final = "No contact with email 用户@例子.公司 or jöe.smith@example.com found"
"""Platform error prose echoing two synthetic addresses: one in CJK script, one mixed-script."""

UNICODE_ECHO_FRAGMENTS: Final = ("用户", "例子", "公司", "jö", "smith")
"""Pieces of :data:`UNICODE_ECHO`'s addresses; none may reach a stored reason or a log."""

UNICODE_ECHOES: Final = (
    pytest.param(UNICODE_ECHO, UNICODE_ECHO_FRAGMENTS, id="cjk-and-mixed-script"),
    pytest.param(
        "No contact with email 用户!@example.com found", ("用户!", "用户"), id="cjk-and-bang"
    ),
    pytest.param(
        "No contact with email 用户😀@example.com found", ("用户", "😀"), id="cjk-and-emoji"
    ),
    pytest.param(
        "No contact with email 用户!a@example.com found", ("用户!", "用户"), id="cjk-bang-letter"
    ),
    pytest.param(
        'No contact with email "用户"@example.com found',
        ('"用户"', "用户", "example.com"),
        id="quoted-local-part",
    ),
    pytest.param(
        "No contact with email 用户@例子😀.com found",
        ("用户", "例子😀", "例子", "😀"),
        id="emoji-domain-label",
    ),
)
"""Echoes whose addresses hold letters in any script, ``atext`` specials, emoji, a quoted
local part or a domain label with an emoji, each with the pieces of its address that may
reach no stored reason or log."""


@pytest.mark.parametrize(("echo", "fragments"), UNICODE_ECHOES)
@pytest.mark.parametrize(
    ("source", "fault", "disposition"),
    [
        pytest.param(
            "linkedin",
            Fault("linkedin", "ad_analytics", "http_status", status=503, message=UNICODE_ECHO),
            "stale",
            id="linkedin-transient",
        ),
        pytest.param(
            "google",
            Fault(
                "google",
                "search",
                "http_status",
                status=400,
                message=UNICODE_ECHO,
                match={"query_id": "Q-G3"},
            ),
            "defect",
            id="google-defect",
        ),
    ],
)
async def test_a_failure_echoing_unicode_emails_stores_and_logs_no_address(
    rig: Rig,
    source: Source,
    fault: Fault,
    disposition: str,
    echo: str,
    fragments: tuple[str, ...],
) -> None:
    """Transient and defects: error prose that echoes an address in any script reaches no sink.

    AAP 0.3.2 (lead emails persisted only as SHA-256 hashes) and 0.11.1 (logs never contain
    emails): the platform's error message names synthetic addresses in CJK and mixed script,
    with ``atext`` specials or emoji in their local parts, with a quoted local part and with
    an emoji in a domain label. The failure is classified and recorded as usual (stale, or a
    defect), yet the job result, ``sync_run.error``, ``dataset_freshness.reason``,
    ``source_connection.reason`` and every structured log event of the run carry
    ``[REDACTED_EMAIL]`` in their place and no piece of any address, such as the ``用户!``
    before a ``!a@example.com`` a narrower scanner would have cut off.
    """
    dataset_id = await (_live_linkedin(rig) if source == "linkedin" else _live_google(rig))
    failing = rig.live(source, rig.world(faults=(dataclasses.replace(fault, message=echo),)))

    with capture_logs() as logs:
        report = await jobs.run_source_jobs(
            source,
            trigger="manual",
            jobs=[jobs.JOB_METRICS],
            controller=rig.controller({source: failing}, at=T1),
        )

    (result,) = report.results
    assert (result.status, result.disposition) == ("failed", disposition)
    connection = await rig.connection(source)
    runs = await rig.all(SyncRun, SyncRun.source == source, SyncRun.started_at == T1)
    attempts = await rig.all(
        DatasetFreshness,
        DatasetFreshness.dataset_id == dataset_id,
        DatasetFreshness.last_attempt_at == T1,
    )
    run_errors = [run.error for run in runs if run.error is not None]
    freshness_reasons = [row.reason for row in attempts if row.reason is not None]
    assert result.error is not None
    assert connection.reason is not None
    assert run_errors, "the failed job recorded its sync_run rows"
    if disposition == "stale":
        assert freshness_reasons, "the interrupted datasets recorded the failure"
    for stored in (result.error, connection.reason, *run_errors, *freshness_reasons):
        assert "[REDACTED_EMAIL]" in stored, stored
        for fragment in fragments:
            assert fragment not in stored, (fragment, stored)
    classified = [entry for entry in logs if entry.get("event") == "sync.classification"]
    assert classified, "the failure was classified and logged"
    rendered = repr(logs)
    for fragment in fragments:
        assert fragment not in rendered, fragment


@pytest.mark.parametrize(
    ("source", "job", "fault"),
    [
        pytest.param(
            "google",
            jobs.JOB_METRICS,
            Fault("google", "search", "http_status", status=400, match={"query_id": "Q-G6"}),
            id="google-metrics-q-g6",
        ),
        pytest.param(
            "linkedin",
            jobs.JOB_ENTITIES,
            Fault("linkedin", "creatives", "http_status", status=400),
            id="linkedin-entities-creatives",
        ),
    ],
)
async def test_defect_mid_job_counts_every_call_it_made(
    rig: Rig, source: Source, job: str, fault: Fault
) -> None:
    """Defects: a job a defect fails mid-run reports every call it made, the defect's included.

    AAP 0.11.1 logging points (sync job finish "with source, job, rows, calls and duration"):
    Google's metrics job meets the 400 on Q-G6 after Q-G3 and Q-G5 answered, and LinkedIn's
    entity sync on its creatives after the account, campaign groups and campaigns answered.
    The job's result and the report's totals count exactly the calls the platform received
    (LinkedIn's token exchange is no API call of the job), not only those of the reads that
    returned.
    """
    if source == "google":
        await _live_google(rig)
    else:
        await _live_linkedin(rig)
    journal: list[JournalEntry] = []
    defective = rig.live(source, rig.world(faults=(fault,)), journal=journal)
    controller = rig.controller({source: defective}, at=T1)

    report = await jobs.run_source_jobs(source, trigger="manual", jobs=[job], controller=controller)

    (result,) = report.results
    assert (result.job, result.status, result.disposition) == (job, "failed", "defect")
    received = [entry for entry in journal if entry.surface != "access_token"]
    assert [entry.status for entry in received].count(400) == 1, "the defect, never retried"
    assert len(received) > 1, "calls answered before the defect"
    assert result.calls == len(received)
    assert report.to_json()["jobs"][0]["calls"] == result.calls


@dataclasses.dataclass(frozen=True, slots=True)
class _ProbeFault:
    """How the live platform answers Google's first-start probe (Q-G1), and what follows.

    Attributes:
        status: The HTTP status Q-G1 answers with, in Google's error body.
        attempts: The Q-G1 requests the probe sends (a transient error is retried).
        connection_status: The ``source_connection`` status the failure leaves: ``denied``
            (automatic fallback with simulated data), ``error`` (a defect) or ``stale``
            (transient failures); only ``denied`` falls back.
        error_kind: The failed check's classification.
        disposition: The ``disposition`` of the probe's ``sync.classification`` event;
            ``None`` for a denial, whose fallback is no job classification.
        log_level: The level of that event.
        query_id: The ``query_id`` that event carries (a defect's only).
    """

    status: int
    attempts: int
    connection_status: str
    error_kind: str
    disposition: str | None = None
    log_level: str | None = None
    query_id: str | None = None

    @property
    def fallback(self) -> bool:
        """Whether the failure enters automatic fallback with simulated data."""
        return self.connection_status == "denied"


@pytest.mark.parametrize(
    "fault",
    [
        pytest.param(
            _ProbeFault(400, 1, "error", "defect", "defect", "error", "Q-G1"), id="defect-400"
        ),
        pytest.param(_ProbeFault(403, 1, "denied", "access_denied"), id="denied-403"),
        pytest.param(
            _ProbeFault(503, 3, "stale", "transient", "stale", "warning"), id="transient-503"
        ),
    ],
)
async def test_first_start_probe_failure_follows_its_classification(
    rig: Rig, fault: _ProbeFault
) -> None:
    """Defects and transients: a probe failing with either builds no mock; a denial falls back.

    AAP 0.3.3 fallback table: HTTP 400 (like a GAQL ``QueryError``) is a Defect, "the job fails
    and is logged with the query id. No mock, no retry"; HTTP 503 is Transient, "3 attempts
    ... then the affected datasets turn stale ... Last good data is kept. Never mock"; only a
    denial (401/403, a permission body) switches the source to simulated data. So a first-start
    probe whose Q-G1 is refused with a 400 is sent once and leaves ``source_connection`` live
    with status ``error``; one answered 503 is sent three times and leaves it live with status
    ``stale``. Either way the failing check (``Q-G1``, its endpoint ``search Q-G1``) is the
    reason, no access generation is advanced, no dataset is built, nothing is put in automatic
    fallback, the ``probe`` row ends ``failed`` with that reason, and ``sync.classification``
    is logged (``defect`` at error level with the query id, ``stale`` at warning level) for job
    ``probe``. A 403 on the same probe still enters automatic fallback with simulated data.
    """
    rig.go_live("google")
    journal: list[JournalEntry] = []
    probe_fault = Fault(
        "google", "search", "http_status", status=fault.status, match={"query_id": "Q-G1"}
    )
    platform = rig.live("google", rig.world(faults=(probe_fault,)), journal=journal)
    controller = rig.controller({"google": platform})

    with capture_logs() as entries:
        outcome = await controller.ensure_dataset("google")

    assert [entry.status for entry in journal] == [fault.status] * fault.attempts
    connection = await rig.connection("google")
    assert connection.reason is not None
    assert connection.reason.startswith("Q-G1: google search Q-G1: ")
    assert connection.last_attempt_at == T0
    (probe_row,) = await _probe_rows(rig)
    assert (probe_row.started_at, probe_row.status, probe_row.error) == (
        T0,
        "failed",
        connection.reason,
    )
    classified = [entry for entry in entries if entry.get("event") == "sync.classification"]
    if not fault.fallback:
        assert outcome is None, "no dataset is built, simulated data included"
        assert (connection.mode, connection.status, connection.access_generation) == (
            "live",
            fault.connection_status,
            0,
        )
        assert await rig.datasets() == [], "no mock"
        assert await controller.in_fallback("google") is False
        resolved = await controller.resolve("google")
        assert (resolved.mode, resolved.fallback, resolved.needs_probe) == ("live", False, True)
        (classification,) = classified
        assert (
            classification["disposition"],
            classification["log_level"],
            classification["query_id"],
        ) == (fault.disposition, fault.log_level, fault.query_id)
        assert (
            classification["source"],
            classification["endpoint"],
            classification["error_kind"],
            classification["check"],
            classification["job"],
        ) == ("google", "search Q-G1", fault.error_kind, "Q-G1", JOB_PROBE)
        assert not [entry for entry in entries if entry.get("event") == "sync.fallback"]
        return
    simulated = _activated(outcome, "simulated")
    assert (connection.mode, connection.status, connection.access_generation) == (
        "simulated",
        "denied",
        1,
    )
    assert simulated.reason == connection.reason
    assert "PERMISSION_DENIED" in connection.reason, "the platform's code is the reason"
    assert await controller.in_fallback("google") is True
    assert classified == [], "the probe's fallback is no job classification"


def _q_g1_unavailable() -> Fault:
    """Q-G1, Google's whole probe, answering HTTP 503: a transient failure on every attempt."""
    return Fault("google", "search", "http_status", status=503, match={"query_id": "Q-G1"})


def _freshness_state(rows: Sequence[DatasetFreshness]) -> list[tuple[object, ...]]:
    """Every column of ``rows`` a freshness write could change, by dataset name."""
    return sorted(
        (
            row.dataset,
            row.last_attempt_at,
            row.last_success_at,
            row.covered_from,
            row.covered_through,
            row.completeness,
            row.reason,
        )
        for row in rows
    )


async def test_a_transient_probe_keeps_the_active_simulated_dataset_and_builds_nothing(
    rig: Rig,
) -> None:
    """Transient: a probe that cannot reach the platform leaves the simulated data as it was.

    AAP 0.3.3 fallback table row 3 (Transient: "Last good data is kept. Never mock") and
    "Transitions" (until a new dataset activates, the previous one stays active and labelled).
    Google runs on simulated data while its credentials are missing. Once they are configured,
    the first probe's Q-G1 answers 503 on all three attempts: the simulated dataset stays the
    active one with its facts and freshness rows unchanged, no dataset is built, and the
    connection keeps its mode, its dataset and its access generation while its status turns
    ``stale`` with the failing check as reason. Google is not in automatic fallback: it still
    resolves live, due a probe of its own.
    """
    unconfigured = rig.controller()
    simulated_id = _activated(await unconfigured.ensure_dataset("google"), "simulated").dataset_id
    assert simulated_id is not None
    datasets_before = [(row.id, row.mode, row.state, row.reason) for row in await rig.datasets()]
    freshness_before = _freshness_state(
        await rig.all(DatasetFreshness, DatasetFreshness.dataset_id == simulated_id)
    )
    assert freshness_before, "the simulated backfill recorded its freshness"
    facts_before = await rig.facts(FactCampaignDaily, simulated_id)
    assert facts_before, "the simulated backfill stored daily campaign facts"
    before = await rig.connection("google")
    assert (before.mode, before.status, before.reason) == ("simulated", "ok", REASON_NO_CREDENTIALS)

    rig.go_live("google")
    journal: list[JournalEntry] = []
    platform = rig.live("google", rig.world(faults=(_q_g1_unavailable(),)), journal=journal)
    controller = rig.controller({"google": platform}, at=T1)

    assert await controller.ensure_dataset("google") is None

    assert [entry.status for entry in journal] == [503] * 3, "3 attempts, then stale"
    datasets_after = [(row.id, row.mode, row.state, row.reason) for row in await rig.datasets()]
    assert datasets_after == datasets_before, "never mock, nothing built"
    active = await rig.active("google")
    assert active is not None
    assert active.id == simulated_id, "the last good data stays active"
    assert (
        _freshness_state(
            await rig.all(DatasetFreshness, DatasetFreshness.dataset_id == simulated_id)
        )
        == freshness_before
    )
    assert await rig.facts(FactCampaignDaily, simulated_id) == facts_before
    connection = await rig.connection("google")
    assert (
        connection.mode,
        connection.status,
        connection.access_generation,
        connection.active_dataset_id,
        connection.last_attempt_at,
    ) == ("simulated", "stale", before.access_generation, simulated_id, T1)
    assert connection.reason is not None
    assert connection.reason.startswith("Q-G1: google search Q-G1: ")
    assert await controller.in_fallback("google") is False
    resolved = await controller.resolve("google")
    assert (resolved.mode, resolved.fallback, resolved.needs_probe) == ("live", False, True)
    (row,) = await _probe_rows(rig)
    assert (row.started_at, row.status, row.error) == (T1, "failed", connection.reason)


async def test_a_transient_probe_is_repeated_one_interval_later_and_then_goes_live(
    rig: Rig,
) -> None:
    """Transient: the source left stale by a probe is probed again after 30 minutes, then live.

    AAP 0.3.3 fallback table row 3 (Transient: no mock, the source is not switched) and AAP
    0.3.2 (probes 30 minutes apart; every probe spends the developer-token quota other n8n
    workflows share, AAP 0.2.4). A first-start Q-G1 answering 503 leaves Google live and
    ``stale``, outside automatic fallback, so the scheduler owes it no recovery probe. Its own
    ``probe`` row spaces the next probe: wakes 5 minutes and 29 min 59 s later send nothing and
    change nothing. 30 minutes later the platform answers, the probe passes, the live dataset
    is built and activated, and that probe's row ends ``succeeded``.
    """
    rig.go_live("google")
    journal: list[JournalEntry] = []
    unavailable = rig.live("google", rig.world(faults=(_q_g1_unavailable(),)), journal=journal)
    assert await rig.controller({"google": unavailable}).ensure_dataset("google") is None
    connection = await rig.connection("google")
    assert (connection.mode, connection.status) == ("live", "stale")
    attempted = (
        connection.mode,
        connection.status,
        connection.reason,
        connection.last_attempt_at,
        connection.access_generation,
    )

    for after in (timedelta(minutes=5), PROBE_INTERVAL - timedelta(seconds=1)):
        at = T0 + after
        spaced = rig.controller({"google": unavailable}, at=at)
        assert await spaced.ensure_dataset("google") is None
        scheduler = Scheduler(controller=spaced, clock=_Clock(at))
        assert await scheduler._probe_due("google", at) is False, "not in automatic fallback"
    assert len(journal) == 3, "no probe inside the interval"
    connection = await rig.connection("google")
    assert (
        connection.mode,
        connection.status,
        connection.reason,
        connection.last_attempt_at,
        connection.access_generation,
    ) == attempted
    assert [row.started_at for row in await _probe_rows(rig)] == [T0]
    assert await rig.datasets() == [], "still no mock"

    again = T0 + PROBE_INTERVAL
    healthy = rig.controller({"google": rig.live("google")}, at=again)
    live = _activated(await healthy.ensure_dataset("google"), "live")

    assert live.reason == REASON_LIVE
    connection = await rig.connection("google")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        live.dataset_id,
    )
    assert [(row.started_at, row.status) for row in await _probe_rows(rig)] == [
        (T0, "failed"),
        (again, "succeeded"),
    ]


async def test_a_transient_probe_keeps_a_denial_recorded_while_it_ran(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient: a probe's transient verdict never ends a fallback that a denial began meanwhile.

    AAP 0.3.3 fallback table rows 2 and 3, and the sticky ``denied`` marker of automatic
    fallback, which the recovery probes rely on. While the first-start probe's Q-G1 answers
    503, another controller's live call is denied (``on_error`` enters automatic fallback).
    The probe's transient verdict then keeps the ``denied`` status and the denial's reason
    instead of writing ``stale`` and its own check, so Google stays in automatic fallback and
    its next wake builds simulated data with the denial as reason.
    """
    rig.go_live("google")
    platform = rig.live("google", rig.world(faults=(_q_g1_unavailable(),)))
    other = rig.controller({"google": platform})
    denial = AccessDenied(
        "The caller does not have permission",
        source="google",
        endpoint="search Q-G3",
        code="PERMISSION_DENIED",
        http_status=403,
    )
    probe = SourceModeController.probe
    denied: list[Source] = []

    async def probe_while_denied(self: SourceModeController, source: Source) -> ProbeReport:
        """Run the real probe, then let the other controller record its denial, once."""
        report = await probe(self, source)
        if not denied:
            denied.append(source)
            assert await other.on_error(source, denial) == "switched_to_simulated"
        return report

    monkeypatch.setattr(SourceModeController, "probe", probe_while_denied)

    assert await rig.controller({"google": platform}).ensure_dataset("google") is None

    assert denied == ["google"]
    connection = await rig.connection("google")
    assert (
        connection.mode,
        connection.status,
        connection.reason,
        connection.access_generation,
    ) == ("simulated", "denied", denial.reason, 1)
    assert await other.in_fallback("google") is True
    assert await rig.datasets() == [], "the probe itself builds nothing"
    (row,) = await _probe_rows(rig)
    assert row.status == "failed"
    assert row.error is not None
    assert row.error.startswith("Q-G1: google search Q-G1: ")

    simulated = _activated(
        await rig.controller({"google": platform}).ensure_dataset("google"), "simulated"
    )
    assert simulated.reason == denial.reason


class _BrokenAnalytics(httpx.AsyncBaseTransport):
    """The live LinkedIn stand-in whose ``adAnalytics`` answers HTTP 200 off its contract.

    Every other request is the simulator's. ``adAnalytics`` answers HTTP 200 with a body
    holding no ``elements`` list, which the parser rejects as a ``validation`` defect, as it
    rejected the live analytics in the QA run (every endpoint HTTP 200).
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.analytics_calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Answer ``adAnalytics`` with a contract-breaking body, anything else as simulated."""
        if request.url.path.endswith("/adAnalytics"):
            self.analytics_calls += 1
            return httpx.Response(200, json={"paging": {"start": 0, "count": 1}}, request=request)
        return await self._inner.handle_async_request(request)


@pytest.mark.parametrize("lead_sync_denied", [False, True], ids=["defect-only", "defect-and-403"])
async def test_linkedin_validation_defect_in_probe_is_an_error_unless_a_check_is_denied(
    rig: Rig, lead_sync_denied: bool
) -> None:
    """Defects: an ``adAnalytics`` validation defect in the probe is no denial and builds no mock.

    AAP 0.3.3 fallback table row 4 ("validation error": Defect, no mock) and "Granularity" (a
    403 on any LinkedIn endpoint switches all of LinkedIn). Every endpoint answers HTTP 200,
    but the probe's one-day ``adAnalytics`` breaks its contract: LinkedIn stays live with
    status ``error`` and the failing check as reason, and neither a LinkedIn dataset nor its
    companion CRM dataset is built. When Lead Sync also answers 403, the denial decides:
    all of LinkedIn switches to simulated data with the denied check, not the defect, as
    reason.
    """
    rig.go_live("linkedin")
    (account,) = get_settings().linkedin_ad_account_ids
    scenario = LEADSYNC_403 if lead_sync_denied else BASE
    platform = _BrokenAnalytics(rig.live("linkedin", rig.world(scenario)))
    controller = rig.controller({"linkedin": platform})

    outcome = await controller.ensure_dataset("linkedin")

    assert platform.analytics_calls == 1, "a defect is never retried"
    connection = await rig.connection("linkedin")
    assert connection.reason is not None
    if not lead_sync_denied:
        assert outcome is None
        assert (connection.mode, connection.status) == ("live", "error")
        assert connection.reason.startswith(f"adAnalytics {account}: linkedin ad_analytics: ")
        assert "validation" in connection.reason
        assert await rig.datasets() == [], "no LinkedIn dataset and no companion"
        assert await controller.in_fallback("linkedin") is False
        return
    simulated = _activated(outcome, "simulated")
    assert simulated.companion_id is not None
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert connection.reason.startswith(f"leadFormResponses {account}: ")
    assert f"linkedin {LEAD_SYNC_SURFACE}" in connection.reason
    assert await controller.in_fallback("linkedin") is True


@pytest.mark.parametrize(
    "lead_sync_denied", [False, True], ids=["transient-only", "transient-and-403"]
)
async def test_linkedin_probe_cut_short_by_a_transient_error_is_stale_unless_a_check_is_denied(
    rig: Rig, lead_sync_denied: bool
) -> None:
    """Transient: a probe listing cut short by a 503 is no denial and builds no mock.

    AAP 0.3.3 fallback table row 3 (Transient: "Never mock") and "Granularity" (a 403 on any
    LinkedIn endpoint switches all of LinkedIn). The probe's one-page ``leadForms`` answers 503
    on all three attempts, so the listing is cut short and its check fails as transient:
    LinkedIn stays live with status ``stale`` and that check as reason, its access generation
    stays, and neither a LinkedIn dataset nor its companion CRM dataset is built. When Lead
    Sync also answers 403, the denial decides although the transient check failed first: all
    of LinkedIn switches to simulated data with the denied check, not the transient one, as
    reason.
    """
    rig.go_live("linkedin")
    (account,) = get_settings().linkedin_ad_account_ids
    scenario = LEADSYNC_403 if lead_sync_denied else BASE
    outage = Fault("linkedin", "lead_forms", "http_status", status=503)
    journal: list[JournalEntry] = []
    platform = rig.live("linkedin", rig.world(scenario, faults=(outage,)), journal=journal)
    controller = rig.controller({"linkedin": platform})

    with capture_logs() as entries:
        outcome = await controller.ensure_dataset("linkedin")

    form_calls = [entry for entry in journal if entry.surface == "lead_forms"]
    assert [entry.status for entry in form_calls] == [503] * 3, "3 attempts, then cut short"
    connection = await rig.connection("linkedin")
    assert connection.reason is not None
    (probe_row,) = await _probe_rows(rig, "linkedin")
    assert (probe_row.status, probe_row.error) == ("failed", connection.reason)
    classified = [entry for entry in entries if entry.get("event") == "sync.classification"]
    if not lead_sync_denied:
        assert outcome is None
        assert (connection.mode, connection.status, connection.access_generation) == (
            "live",
            "stale",
            0,
        )
        assert connection.reason.startswith(f"leadForms {account}: linkedin lead_forms: ")
        assert await rig.datasets() == [], "no LinkedIn dataset and no companion"
        assert await controller.in_fallback("linkedin") is False
        (classification,) = classified
        assert (
            classification["disposition"],
            classification["log_level"],
            classification["error_kind"],
            classification["check"],
            classification["job"],
        ) == ("stale", "warning", "transient", f"leadForms {account}", JOB_PROBE)
        return
    simulated = _activated(outcome, "simulated")
    assert simulated.companion_id is not None
    assert (connection.mode, connection.status, connection.access_generation) == (
        "simulated",
        "denied",
        1,
    )
    assert connection.reason.startswith(f"leadFormResponses {account}: ")
    assert f"linkedin {LEAD_SYNC_SURFACE}" in connection.reason
    assert simulated.reason == connection.reason
    assert await controller.in_fallback("linkedin") is True
    assert classified == [], "the probe's fallback is no job classification"


async def test_call_budget_deferral(rig: Rig) -> None:
    """Transient: calls beyond GOOGLE_DAILY_CALL_BUDGET are deferred; the datasets stay incomplete.

    AAP 0.3.3 "Timeouts and budget": the product's own cap counts every webhook call in the
    sliding 24 h. With the cap one call above what the live build already spent, the next
    metrics run makes that one call, defers the rest and records the affected datasets
    ``incomplete`` with a reason that starts with the deferral prefix, so the next window
    finishes them. A deferred read is no successful refresh ("The affected datasets keep their
    last good data and turn stale"): each deferred dataset's ``last_success_at`` stays the
    build's, so it is stale 24 h after the build. The source stays live and
    ``source_connection.calls_24h`` shows the cap reached. The facts already stored are kept,
    proved on values: the one call is the
    incremental Q-G3 read, so only the campaign days inside its range are rewritten, with
    their ids and the platform's same values under a later ``fetched_at``; every other stored
    campaign day, and every keyword and search-term day the deferred Q-G5 and Q-G6 would have
    refreshed, stays exactly as stored, ``fetched_at`` included.
    """
    dataset_id = await _live_google(rig)
    spent = await GoogleCallBudget(
        get_settings(), sessionmaker=rig.sessions, clock=_Clock(T1)
    ).used_24h()
    assert spent > 0, "the live build's webhook calls were counted"
    cap = spent + 1
    rig.set_env(GOOGLE_DAILY_CALL_BUDGET=str(cap))
    facts_before = await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
    assert facts_before
    campaign_days = await rig.facts(FactCampaignDaily, dataset_id)
    assert len(campaign_days) == facts_before
    unread_before = {
        model: await rig.facts(model, dataset_id)
        for model in (FactKeywordDaily, FactSearchTermDaily)
    }
    for model, rows in unread_before.items():
        assert rows, f"the live build stored {model.__tablename__} rows"
        assert any(row["cost"] for row in rows.values()), f"{model.__tablename__} spend to keep"
    built_daily = await rig.freshness_row(dataset_id, "google.metrics_daily")
    assert built_daily is not None
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    journal: list[JournalEntry] = []
    capped = rig.recording(rig.live("google", journal=journal))
    controller = rig.controller({"google": capped}, at=T1)
    assert controller.settings.google_daily_call_budget == cap

    report = await jobs.run_source_jobs(
        "google", trigger="manual", jobs=[jobs.JOB_METRICS], controller=controller
    )

    (result,) = report.results
    assert result.completeness == "incomplete"
    assert len(journal) == 1, "only the call the cap still allowed was sent"
    # The one paid call is Q-G3 over the incremental range, whole native days with no
    # campaign bounds: the only facts this run may rewrite are the campaign days it read.
    ((query_id, read),) = _google_reads(capped)
    assert query_id == "Q-G3"
    assert set(read) == {START, END}
    first_day, last_day = read[START], read[END]
    assert isinstance(first_day, date)
    assert isinstance(last_day, date)
    assert last_day == built_daily.covered_through, "the native day the build covered through"
    assert last_day - first_day == timedelta(days=jobs.INCREMENTAL_DAYS - 1)
    deferred = {
        name: outcome
        for name, outcome in result.freshness.items()
        if outcome.reason is not None and outcome.reason.startswith(freshness.REASON_DEFERRED)
    }
    assert deferred, "a dataset recorded the deferral"
    for name in deferred:
        row = await rig.freshness_row(dataset_id, name)
        assert row is not None
        assert row.completeness == "incomplete"
        assert row.reason is not None
        assert row.reason.startswith(freshness.REASON_DEFERRED)
        assert freshness.needs_repair(row), "the shortfall is repairable by a later read"
        assert row.checkpoint is not None, "the deferred scopes are kept for the next run"
        # "The affected datasets keep their last good data and turn stale": a deferred read is
        # no successful refresh, so only the attempt moves.
        assert (row.last_attempt_at, row.last_success_at) == (T1, T0), name
        assert _stale_at(row, DAY_AFTER), name
    connection = await rig.connection("google")
    assert (connection.mode, connection.active_dataset_id) == ("live", dataset_id)
    assert connection.calls_24h == cap
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before
    assert (
        await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
        == facts_before
    )
    campaign_days_after = await rig.facts(FactCampaignDaily, dataset_id)
    assert campaign_days_after.keys() == campaign_days.keys(), "no campaign day added or removed"
    refreshed = {key for key, row in campaign_days.items() if first_day <= row["date"] <= last_day}
    untouched = campaign_days.keys() - refreshed
    assert refreshed, "stored campaign days lie inside the paid read"
    assert any(campaign_days[key]["cost"] for key in refreshed), "spend inside the paid read"
    assert untouched, "stored campaign days lie outside the paid read"
    for key in refreshed:
        was, now = campaign_days[key], campaign_days_after[key]
        assert now["fetched_at"] > was["fetched_at"], f"{key}: rewritten by the paid read"
        assert {**now, "fetched_at": was["fetched_at"]} == was, (
            f"{key}: the same row, holding the platform's same values"
        )
    assert {key: campaign_days_after[key] for key in untouched} == {
        key: campaign_days[key] for key in untouched
    }, "campaign days outside the paid read are untouched, fetched_at included"
    for model, rows in unread_before.items():
        assert await rig.facts(model, dataset_id) == rows, (
            f"{model.__tablename__}: its deferred read rewrote nothing"
        )
    # A deferral is a transient shortfall: the run succeeded with what it could pay for, and
    # the connection reports the source stale until the deferred pages are finished.
    assert (result.status, result.disposition, connection.status) == (
        "succeeded",
        "stale",
        "stale",
    )


async def _cap_google_calls(rig: Rig, at: datetime, allowed: int) -> None:
    """Set ``GOOGLE_DAILY_CALL_BUDGET`` to admit ``allowed`` more webhook calls at ``at``."""
    spent = await GoogleCallBudget(
        get_settings(), sessionmaker=rig.sessions, clock=_Clock(at)
    ).used_24h()
    rig.set_env(GOOGLE_DAILY_CALL_BUDGET=str(spent + allowed))


def _google_reads(recorder: Any) -> list[tuple[str, dict[str, object]]]:
    """The template id and scope of every first-page webhook request, decoded from its GAQL."""
    reads: list[tuple[str, dict[str, object]]] = []
    for request in recorder.requests:
        body = json.loads(request.content)
        if body["page_token"] is None:
            parsed = parse_query(body["query"])
            reads.append((parsed.query_id, dict(parsed.scope)))
    return reads


async def _checkpoint_is_sql_null(rig: Rig, dataset_id: int, name: str) -> bool:
    """Whether a freshness row's checkpoint ``IS NULL`` in SQL, not merely the JSON ``null``."""
    async with rig.sessions() as session:
        is_null = await session.scalar(
            select(DatasetFreshness.checkpoint.is_(None)).where(
                DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == name
            )
        )
    assert is_null is not None, f"no dataset_freshness row for {name}"
    return bool(is_null)


async def _snapshots_at(rig: Rig, dataset_id: int, entity_type: str, at: datetime) -> int:
    """How many snapshots of one entity type the sync at ``at`` wrote."""
    return await rig.count(
        EntitySnapshot,
        EntitySnapshot.dataset_id == dataset_id,
        EntitySnapshot.entity_type == entity_type,
        EntitySnapshot.captured_at == at,
    )


async def test_call_budget_deferral_is_read_first_by_the_next_metrics_run(rig: Rig) -> None:
    """Transient: the scopes the cap deferred are the next metrics run's first requests.

    AAP 0.3.3 "Budget": calls beyond the cap move to a later 24 h window and the dataset stays
    ``incomplete`` until they finish. A 30-day restatement with the cap one call above what
    the build spent reads Q-G3 and keeps Q-G5 and Q-G6 as unread scopes in each dataset's
    checkpoint, tagged with the run's instant, so nothing restarts from the same prefix. The
    deferred run is no successful refresh of either dataset ("The affected datasets keep
    their last good data and turn stale"): both keep the build's ``last_success_at``,
    ``google.search_terms`` although it got no call at all. The next incremental run, under
    a raised cap, first reads both stored scopes as stored (``google.metrics_daily``'s
    before ``google.search_terms``', both deferred at T1), then its own range without the
    native days a stored scope of the same query covered: Q-G3 in full, nothing more of Q-G5
    or Q-G6. Both datasets end ``complete`` with the checkpoint cleared, over the stored
    range, and that resume moves their ``last_success_at``.
    """
    dataset_id = await _live_google(rig)
    built = {
        name: await rig.freshness_row(dataset_id, name)
        for name in ("google.metrics_daily", "google.search_terms")
    }
    await _cap_google_calls(rig, T1, allowed=1)
    capped = rig.recording(rig.live("google"))

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_RESTATEMENT],
        controller=rig.controller({"google": capped}, at=T1),
    )

    (deferral,) = report.results
    assert (deferral.status, deferral.completeness) == ("succeeded", "incomplete")
    assert [query_id for query_id, _scope in _google_reads(capped)] == ["Q-G3"]
    daily = await rig.freshness_row(dataset_id, "google.metrics_daily")
    terms = await rig.freshness_row(dataset_id, "google.search_terms")
    assert daily is not None
    assert terms is not None
    assert daily.covered_through is not None
    today = daily.covered_through
    first_day = today - timedelta(days=jobs.RESTATEMENT_DAYS["google"] - 1)
    restated = {"start": first_day.isoformat(), "end": today.isoformat()}
    for row, query_id in ((daily, "Q-G5"), (terms, "Q-G6")):
        assert row.checkpoint == {
            "version": 1,
            "pending": {query_id: [{"scope": restated, "deferred_at": T1.isoformat()}]},
            "covered_from": first_day.isoformat(),
            "reasons": [],
        }
        assert row.completeness == "incomplete"
        assert row.reason == f"{freshness.REASON_DEFERRED}; 1 partition deferred"
        before = built[row.dataset]
        assert before is not None
        # Deferred (Q-G6 got no call at all): no successful refresh, so only the attempt moves.
        assert (row.last_attempt_at, row.last_success_at) == (T1, before.last_success_at)
        assert _stale_at(row, DAY_AFTER), row.dataset

    await _cap_google_calls(rig, T2, allowed=100)
    resumed = rig.recording(rig.live("google"))
    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_METRICS],
        controller=rig.controller({"google": resumed}, at=T2),
    )

    (result,) = report.results
    assert (result.status, result.completeness) == ("succeeded", "complete")
    daily = await rig.freshness_row(dataset_id, "google.metrics_daily")
    assert daily is not None
    assert daily.covered_through == today, "T1 and T2 share the account's native day"
    own_start = daily.covered_through - timedelta(days=jobs.INCREMENTAL_DAYS - 1)
    own = {START: own_start, END: daily.covered_through}
    stored = {START: first_day, END: today}
    assert _google_reads(resumed) == [
        ("Q-G5", stored),
        ("Q-G6", stored),
        ("Q-G3", own),
    ], "the stored scopes come first as stored; the own range skips the days they cover"
    for name, before in built.items():
        row = await rig.freshness_row(dataset_id, name)
        assert row is not None
        assert before is not None
        assert (row.completeness, row.reason, row.checkpoint) == ("complete", None, None)
        assert await _checkpoint_is_sql_null(rig, dataset_id, name), "drained to SQL NULL"
        assert (row.covered_from, row.covered_through) == (
            before.covered_from,
            daily.covered_through,
        ), "the drained checkpoint vouches for the whole stored range"
        assert not freshness.needs_repair(row)
        assert (row.last_attempt_at, row.last_success_at) == (T2, T2), "the resume refreshed it"
        assert not _stale_at(row, DAY_AFTER), name


async def test_call_budget_deferral_resumes_the_entity_cycle_with_only_outstanding_reads(
    rig: Rig,
) -> None:
    """Transient: an entity sync the cap interrupts is finished by later syncs, read by read.

    AAP 0.3.3 "Budget" and "Safe absence": the first sync can pay for Q-G1 only, so the
    campaign, ad-group and keyword enumerations and Q-G8 stay outstanding in the
    ``google.entities`` checkpoint. The second reads only Q-G2 and Q-G4, the third only Q-G7
    and Q-G8; nothing is read twice, and no snapshot, removal or ``missing`` discrepancy is
    inferred from a read that did not happen. The dataset stays ``incomplete`` with the total
    still outstanding, and turns ``complete`` with the checkpoint cleared once the cycle drains.
    Until then no sync is a successful refresh ("The affected datasets keep their last good
    data and turn stale"): ``last_success_at`` stays the build's through both deferred syncs
    and moves only with the sync that drains the cycle.
    """
    dataset_id = await _live_google(rig)
    discrepancies_before = await rig.count(Discrepancy)
    removed_keywords = await rig.count(
        Keyword, Keyword.dataset_id == dataset_id, Keyword.status == "REMOVED"
    )
    await _cap_google_calls(rig, T1, allowed=1)
    first_sync = rig.recording(rig.live("google"))

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"google": first_sync}, at=T1),
    )

    (first,) = report.results
    assert (first.status, first.completeness) == ("succeeded", "incomplete")
    assert [query_id for query_id, _scope in _google_reads(first_sync)] == ["Q-G1"]
    row = await rig.freshness_row(dataset_id, "google.entities")
    assert row is not None
    assert (row.completeness, row.reason) == (
        "incomplete",
        f"{freshness.REASON_DEFERRED}; 4 partitions deferred",
    )
    assert (row.last_attempt_at, row.last_success_at) == (T1, T0), "deferred: no refresh"

    def unread(query_id: str, scope: dict[str, int]) -> dict[str, object]:
        return {
            "query_id": query_id,
            "pending": [scope],
            "absent": [],
            "rereading": False,
            "reread_rows": [],
        }

    keywords_unread = unread("Q-G7", {"ad_group_lo": 0, "ad_group_hi": INT64_MAX})
    assert row.checkpoint == {
        "version": 1,
        "started_at": T1.isoformat(),
        "pending": {"Q-G8": [{}]},
        "enumerations": {
            "Q-G2": unread("Q-G2", {"last_id": 0}),
            "Q-G4": unread("Q-G4", {"last_id": 0}),
            "Q-G7": keywords_unread,
        },
        "reasons": [],
    }
    assert await _snapshots_at(rig, dataset_id, "campaign", T1) == 0

    await _cap_google_calls(rig, T2, allowed=2)
    second_sync = rig.recording(rig.live("google"))
    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"google": second_sync}, at=T2),
    )

    (second,) = report.results
    assert (second.status, second.completeness) == ("succeeded", "incomplete")
    assert _google_reads(second_sync) == [("Q-G2", {"last_id": 0}), ("Q-G4", {"last_id": 0})]
    row = await rig.freshness_row(dataset_id, "google.entities")
    assert row is not None
    assert (row.completeness, row.reason) == (
        "incomplete",
        f"{freshness.REASON_DEFERRED}; 2 partitions deferred",
    )
    assert (row.last_attempt_at, row.last_success_at) == (T2, T0), "still deferred: no refresh"
    assert _stale_at(row, DAY_AFTER)
    assert row.checkpoint == {
        "version": 1,
        "started_at": T1.isoformat(),
        "pending": {"Q-G8": [{}]},
        "enumerations": {"Q-G7": keywords_unread},
        "reasons": [],
    }
    assert await _snapshots_at(rig, dataset_id, "campaign", T2) > 0
    assert await _snapshots_at(rig, dataset_id, "keyword", T2) == 0
    assert (
        await rig.count(Keyword, Keyword.dataset_id == dataset_id, Keyword.status == "REMOVED")
        == removed_keywords
    ), "no keyword is removed while Q-G7 is outstanding"

    await _cap_google_calls(rig, T3, allowed=100)
    third_sync = rig.recording(rig.live("google"))
    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_ENTITIES],
        controller=rig.controller({"google": third_sync}, at=T3),
    )

    (third,) = report.results
    assert (third.status, third.completeness) == ("succeeded", "complete")
    assert [query_id for query_id, _scope in _google_reads(third_sync)] == ["Q-G7", "Q-G8"]
    row = await rig.freshness_row(dataset_id, "google.entities")
    assert row is not None
    assert (row.completeness, row.reason, row.checkpoint) == ("complete", None, None)
    assert (row.last_attempt_at, row.last_success_at) == (T3, T3), "the drained cycle refreshed"
    assert await _checkpoint_is_sql_null(rig, dataset_id, "google.entities"), "drained to SQL NULL"
    assert await _snapshots_at(rig, dataset_id, "keyword", T3) > 0
    assert await _snapshots_at(rig, dataset_id, "campaign", T3) == 0
    assert await rig.count(Discrepancy) == discrepancies_before, "nothing was inferred"


async def test_a_transient_failure_keeps_its_unread_scopes_for_the_next_run(rig: Rig) -> None:
    """Transient: a refresh a 503 fails is stale now and resumed from its unread scopes next.

    AAP 0.3.3 fallback table row 3: Q-G6 answering 503 on every retry fails the
    ``google.search_terms`` refresh, which keeps its last good data and turns stale, while
    ``google.metrics_daily`` completes. The unread restatement scope is still kept in the
    checkpoint, so the next run reads it as stored before any own range, and its own Q-G6
    range, wholly inside the stored scope's days, adds no read; the dataset completes.
    """
    dataset_id = await _live_google(rig)
    built = await rig.freshness_row(dataset_id, "google.search_terms")
    assert built is not None
    q_g6_down = Fault(
        source="google",
        surface="search",
        kind="http_status",
        status=503,
        match={"query_id": "Q-G6"},
    )
    failing = rig.recording(rig.live("google", rig.world(faults=(q_g6_down,))))

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_RESTATEMENT],
        controller=rig.controller({"google": failing}, at=T1),
    )

    (failed,) = report.results
    assert (failed.status, failed.disposition) == ("failed", "stale")
    daily = await rig.freshness_row(dataset_id, "google.metrics_daily")
    terms = await rig.freshness_row(dataset_id, "google.search_terms")
    assert daily is not None
    assert terms is not None
    assert (daily.completeness, daily.checkpoint) == ("complete", None)
    assert daily.covered_through is not None
    first_day = daily.covered_through - timedelta(days=jobs.RESTATEMENT_DAYS["google"] - 1)
    restated = {"start": first_day.isoformat(), "end": daily.covered_through.isoformat()}
    assert terms.checkpoint == {
        "version": 1,
        "pending": {"Q-G6": [{"scope": restated, "deferred_at": T1.isoformat()}]},
        "covered_from": first_day.isoformat(),
        "reasons": [],
    }
    assert (terms.last_success_at, terms.completeness) == (built.last_success_at, "complete")
    assert terms.last_attempt_at == T1
    assert terms.reason is not None

    healthy = rig.recording(rig.live("google"))
    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_METRICS],
        controller=rig.controller({"google": healthy}, at=T2),
    )

    (result,) = report.results
    assert (result.status, result.completeness) == ("succeeded", "complete")
    terms = await rig.freshness_row(dataset_id, "google.search_terms")
    assert terms is not None
    assert terms.covered_through == daily.covered_through, "T1 and T2 share the native day"
    search_reads = [scope for query_id, scope in _google_reads(healthy) if query_id == "Q-G6"]
    assert search_reads == [{START: first_day, END: terms.covered_through}]
    assert _google_reads(healthy)[0][0] == "Q-G6", "the stored scope is read before any own range"
    assert (terms.completeness, terms.checkpoint, terms.last_success_at) == ("complete", None, T2)


async def test_an_unusable_checkpoint_is_ignored_and_the_run_falls_back_to_the_wide_read(
    rig: Rig,
) -> None:
    """A checkpoint the job cannot read is never trusted: the deferred row is re-read whole.

    The row records a call-budget deferral, but its checkpoint is of an unknown version, so
    the run widens to the recorded range (``needs_repair``), completes the dataset and
    replaces the unusable checkpoint with none.
    """
    dataset_id = await _live_google(rig)
    async with rig.sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(
                DatasetFreshness.dataset_id == dataset_id,
                DatasetFreshness.dataset == "google.metrics_daily",
            )
            .values(
                completeness="incomplete",
                reason=f"{freshness.REASON_DEFERRED}; 1 partition deferred",
                checkpoint={"version": 99, "pending": {"Q-G5": [{"start": "x"}]}},
            )
        )
    stored = await rig.freshness_row(dataset_id, "google.metrics_daily")
    assert stored is not None
    assert freshness.needs_repair(stored)
    recorder = rig.recording(rig.live("google"))

    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_METRICS],
        controller=rig.controller({"google": recorder}, at=T1),
    )

    (result,) = report.results
    assert (result.status, result.completeness) == ("succeeded", "complete")
    row = await rig.freshness_row(dataset_id, "google.metrics_daily")
    assert row is not None
    assert row.covered_through is not None
    widened = {START: stored.covered_from, END: row.covered_through}
    daily_reads = [
        (query_id, scope)
        for query_id, scope in _google_reads(recorder)
        if query_id in {"Q-G3", "Q-G5"}
    ]
    assert daily_reads == [("Q-G3", widened), ("Q-G5", widened)]
    assert (row.completeness, row.reason, row.checkpoint) == ("complete", None, None)


async def test_a_drained_checkpoint_still_reports_a_permanent_shortfall(rig: Rig) -> None:
    """A result-limit shortfall of a deferred cycle survives the drain of its checkpoint.

    AAP 0.3.3: a Q-G6 (day, ad group) kept at the result limit leaves ``google.search_terms``
    ``incomplete``, and re-reading cannot remove it. Draining a checkpoint lets the run vouch
    for the whole stored range, so the shortfall must outlive the deferral: whether the
    checkpoint carries it (met earlier in the cycle) or the stored row does (met before the
    deferral), the drained dataset stays ``incomplete`` with that reason, not ``complete``.
    """
    dataset_id = await _live_google(rig)
    built = await rig.freshness_row(dataset_id, "google.search_terms")
    assert built is not None
    assert built.covered_from is not None
    first_day = built.covered_from
    shortfall = f"{freshness.REASON_FINEST_PARTITION} ({first_day.isoformat()}, ad group 1)"
    unread_day = {"start": first_day.isoformat(), "end": first_day.isoformat()}

    async def store(reason: str, reasons: list[str]) -> None:
        async with rig.sessions() as session, session.begin():
            await session.execute(
                update(DatasetFreshness)
                .where(
                    DatasetFreshness.dataset_id == dataset_id,
                    DatasetFreshness.dataset == "google.search_terms",
                )
                .values(
                    completeness="incomplete",
                    reason=reason,
                    checkpoint={
                        "version": 1,
                        "pending": {"Q-G6": [{"scope": unread_day, "deferred_at": T0.isoformat()}]},
                        "covered_from": first_day.isoformat(),
                        "reasons": reasons,
                    },
                )
            )

    async def drain(at: datetime) -> DatasetFreshness:
        report = await jobs.run_source_jobs(
            "google",
            trigger="manual",
            jobs=[jobs.JOB_METRICS],
            controller=rig.controller({"google": rig.live("google")}, at=at),
        )
        (result,) = report.results
        assert result.status == "succeeded"
        row = await rig.freshness_row(dataset_id, "google.search_terms")
        assert row is not None
        return row

    await store(f"{freshness.REASON_DEFERRED}; 1 partition deferred", [shortfall])
    carried_by_checkpoint = await drain(T1)
    assert (
        carried_by_checkpoint.completeness,
        carried_by_checkpoint.reason,
        carried_by_checkpoint.checkpoint,
    ) == ("incomplete", shortfall, None)
    assert carried_by_checkpoint.covered_from == first_day

    await store(shortfall, [])
    carried_by_row = await drain(T2)
    assert (carried_by_row.completeness, carried_by_row.reason, carried_by_row.checkpoint) == (
        "incomplete",
        shortfall,
        None,
    )
    assert carried_by_row.covered_from == first_day


_METRICS_QUERIES: Final[Mapping[str, tuple[str, ...]]] = {
    "google.metrics_daily": ("Q-G3", "Q-G5"),
    "google.search_terms": ("Q-G6",),
}
"""The Google metrics datasets and their dated queries, in the job's dataset and query order."""


def _stored_reading_order(
    checkpoints: Mapping[str, Mapping[str, Any] | None],
) -> list[tuple[str, dict[str, object]]]:
    """Every pending metrics scope of both checkpoints, in the order the next run reads them.

    Oldest ``deferred_at`` first; ties by dataset (``google.metrics_daily``, then
    ``google.search_terms``), then query (Q-G3, Q-G5; Q-G6), then position in the stored list.
    """
    ordered: list[tuple[tuple[datetime, int, int, int], str, dict[str, object]]] = []
    for dataset_index, (name, queries) in enumerate(_METRICS_QUERIES.items()):
        checkpoint = checkpoints.get(name)
        if checkpoint is None:
            continue
        for query_index, query_id in enumerate(queries):
            for position, entry in enumerate(checkpoint["pending"].get(query_id, ())):
                key = (
                    datetime.fromisoformat(entry["deferred_at"]),
                    dataset_index,
                    query_index,
                    position,
                )
                ordered.append((key, query_id, dict(scope_from_json(query_id, entry["scope"]))))
    ordered.sort(key=lambda item: item[0])
    return [(query_id, scope) for _key, query_id, scope in ordered]


async def _metrics_window(
    rig: Rig, dataset_id: int, at: datetime, *, allowed: int
) -> tuple[list[tuple[str, dict[str, object]]], dict[str, DatasetFreshness]]:
    """Run one incremental Google metrics job at ``at`` with ``allowed`` webhook calls left.

    Returns the first-page reads in the order they were sent and both freshness rows after.
    """
    await _cap_google_calls(rig, at, allowed=allowed)
    recorder = rig.recording(rig.live("google"))
    report = await jobs.run_source_jobs(
        "google",
        trigger="manual",
        jobs=[jobs.JOB_METRICS],
        controller=rig.controller({"google": recorder}, at=at),
    )
    (result,) = report.results
    assert result.status == "succeeded", "a call-budget deferral is an incomplete success"
    rows: dict[str, DatasetFreshness] = {}
    for name in _METRICS_QUERIES:
        row = await rig.freshness_row(dataset_id, name)
        assert row is not None
        rows[name] = row
    return _google_reads(recorder), rows


def _pending_total(checkpoints: Mapping[str, Mapping[str, Any] | None]) -> int:
    """The number of scopes both metrics checkpoints still hold."""
    return sum(
        len(entries)
        for checkpoint in checkpoints.values()
        if checkpoint is not None
        for entries in checkpoint["pending"].values()
    )


async def test_small_replenished_windows_read_the_oldest_deferred_work_first_and_drain(
    rig: Rig,
) -> None:
    """Budget: no metrics dataset's stored work starves behind another's fresh range.

    AAP 0.3.3 "Budget": deferred pages are read in later windows and the dataset stays
    ``incomplete`` until they finish. Three daily windows of two calls each, while each run's
    own ranges need three (Q-G3, Q-G5, Q-G6): every window starts with the oldest stored
    scopes, whichever dataset holds them, so ``google.search_terms``' Q-G6, deferred in the
    first window, is the first read of the second instead of waiting behind
    ``google.metrics_daily``'s fresh ranges. Then, within one native day, repeated windows
    of five calls, fewer than the stored and own work together: each window still starts
    with the oldest stored work and stays within its calls, and the backlog drains over
    several windows until both datasets are ``complete`` with no checkpoint left.
    """
    dataset_id = await _live_google(rig)
    checkpoints: dict[str, Mapping[str, Any] | None] = dict.fromkeys(_METRICS_QUERIES)
    for step in range(3):
        at = T1 + timedelta(days=step)
        expected = _stored_reading_order(checkpoints)
        reads, rows = await _metrics_window(rig, dataset_id, at, allowed=2)
        first = min(len(expected), 2)
        assert len(reads) == 2, f"window {step} spends exactly its two calls"
        assert reads[:first] == expected[:first], f"window {step} starts with the oldest work"
        checkpoints = {name: row.checkpoint for name, row in rows.items()}
        if step == 0:
            assert [query_id for query_id, _scope in reads] == ["Q-G3", "Q-G5"]
            terms = checkpoints["google.search_terms"]
            assert terms is not None
            assert [entry["deferred_at"] for entry in terms["pending"]["Q-G6"]] == [at.isoformat()]
        elif step == 1:
            assert reads[0][0] == "Q-G6", "the first window's deferred Q-G6 is read first"
        for name, row in rows.items():
            if row.checkpoint is None:
                assert (row.completeness, row.reason) == ("complete", None), name
            else:
                assert row.completeness == "incomplete", name
                assert row.reason is not None
                assert row.reason.startswith(freshness.REASON_DEFERRED), name

    allowed = 5
    day = T1 + timedelta(days=3)
    windows = 0
    while _pending_total(checkpoints):
        assert windows < 6, f"the backlog did not drain: {checkpoints}"
        expected = _stored_reading_order(checkpoints)
        reads, rows = await _metrics_window(
            rig, dataset_id, day + timedelta(minutes=windows), allowed=allowed
        )
        first = min(len(expected), allowed)
        assert len(reads) <= allowed
        assert reads[:first] == expected[:first], f"drain window {windows} starts oldest first"
        checkpoints = {name: row.checkpoint for name, row in rows.items()}
        windows += 1
    assert windows > 1, "the backlog took more than one window"
    for name, row in rows.items():
        assert (row.completeness, row.reason, row.checkpoint) == ("complete", None, None), name
        assert await _checkpoint_is_sql_null(rig, dataset_id, name), name


async def test_a_stored_criterion_keyset_is_read_as_stored_before_any_own_range(
    rig: Rig,
) -> None:
    """Budget: a stored Q-G5 keyset keeps its progress and is never re-partitioned from its day.

    A narrowed scope that lies inside the run's own range is neither dropped nor trimmed:
    the checkpoint holds a Q-G5 criterion keyset (ad group 123 from criterion 9,999) on the
    last native day of the own range, deferred at T0, and a window of one call reads exactly
    that keyset. That run's own Q-G5 range leaves the keyset's day out instead of
    re-partitioning it from scratch, and the own ranges it deferred carry its instant.
    Repeated windows of three calls (the own ranges alone need three) then read the oldest
    stored work first, never the keyset again, until both datasets are ``complete`` with no
    checkpoint left.
    """
    dataset_id = await _live_google(rig)
    before = await rig.freshness_row(dataset_id, "google.metrics_daily")
    assert before is not None
    assert before.covered_from is not None
    assert before.covered_through is not None
    day = before.covered_through
    keyset = {"start": day.isoformat(), "end": day.isoformat(), "ad_group_id": 123, "last_id": 9999}
    async with rig.sessions() as session, session.begin():
        await freshness.write_checkpoint(
            session,
            dataset_id,
            "google.metrics_daily",
            {
                "version": 1,
                "pending": {"Q-G5": [{"scope": keyset, "deferred_at": T0.isoformat()}]},
                "covered_from": day.isoformat(),
                "reasons": [],
            },
        )

    reads, rows = await _metrics_window(rig, dataset_id, T1, allowed=1)

    keyset_read = ("Q-G5", {START: day, END: day, "ad_group_id": 123, "last_id": 9999})
    assert reads == [keyset_read], "the stored keyset is the one call, read as stored"
    own_start = day - timedelta(days=jobs.INCREMENTAL_DAYS - 1)
    own = {"start": own_start.isoformat(), "end": day.isoformat()}
    before_day = {"start": own_start.isoformat(), "end": (day - timedelta(days=1)).isoformat()}
    assert rows["google.metrics_daily"].checkpoint == {
        "version": 1,
        "pending": {
            "Q-G3": [{"scope": own, "deferred_at": T1.isoformat()}],
            "Q-G5": [{"scope": before_day, "deferred_at": T1.isoformat()}],
        },
        "covered_from": before.covered_from.isoformat(),
        "reasons": [],
    }, "the keyset is done; the own Q-G5 range left its day out"
    assert rows["google.search_terms"].checkpoint == {
        "version": 1,
        "pending": {"Q-G6": [{"scope": own, "deferred_at": T1.isoformat()}]},
        "covered_from": own_start.isoformat(),
        "reasons": [],
    }

    checkpoints = {name: row.checkpoint for name, row in rows.items()}
    windows = 0
    while _pending_total(checkpoints):
        assert windows < 6, f"the backlog did not drain: {checkpoints}"
        expected = _stored_reading_order(checkpoints)
        reads, rows = await _metrics_window(
            rig, dataset_id, T1 + timedelta(minutes=1 + windows), allowed=3
        )
        first = min(len(expected), 3)
        assert len(reads) <= 3
        assert reads[:first] == expected[:first], f"window {windows} starts oldest first"
        assert keyset_read not in reads, "the keyset is never read again"
        checkpoints = {name: row.checkpoint for name, row in rows.items()}
        windows += 1
    for name, row in rows.items():
        assert (row.completeness, row.reason, row.checkpoint) == ("complete", None, None), name


class _InterruptedFetcher:
    """A fetcher whose one fetch is cut short by the call budget after reading nothing.

    Attributes:
        deferred: The partitions the interrupted fetch leaves unread.
        fetched: Every ``(query_id, scope)`` fetched, in order.
    """

    def __init__(self, deferred: tuple[Mapping[str, object], ...]) -> None:
        self.deferred = deferred
        self.fetched: list[tuple[str, Mapping[str, object]]] = []

    async def partitioned_fetch(self, query_id: str, scope: Mapping[str, object]) -> FetchOutcome:
        """Return an interrupted outcome that defers :attr:`deferred`."""
        self.fetched.append((query_id, scope))
        return FetchOutcome(
            query_id=query_id,
            rows=(),
            completeness="incomplete",
            reason=f"{freshness.REASON_DEFERRED}; {len(self.deferred)} partitions deferred",
            calls=1,
            partitions_read=0,
            deferred=self.deferred,
            interrupted_by=CallBudgetExhausted(),
        )


async def test_metrics_deferral_instants_survive_interruption_merging_and_storage() -> None:
    """Deferral instants are kept by the partitions, merges and JSON that carry the work on.

    A stored scope deferred at T0 whose fetch the budget interrupts leaves its two unread
    partitions tagged T0, and the own range the run did not reach is tagged with the run's
    instant. A concurrent run's checkpoint is merged by scope identity alone (query id and
    scope, whatever the instant): a scope this run loaded and read is not brought back, one
    both hold keeps the earlier instant, and the result is ordered by instant. The JSON form
    rebuilds equal, and a stored entry of another shape is refused.
    """
    queries = _METRICS_QUERIES["google.metrics_daily"]
    day = date(2026, 9, 14)

    def days(first: int, last: int) -> dict[str, date]:
        return {START: day + timedelta(days=first), END: day + timedelta(days=last)}

    written: list[tuple[date, date] | None] = []

    async def write(
        _session: AsyncSession, _outcome: FetchOutcome, covered: tuple[date, date] | None
    ) -> jobs._Written:
        written.append(covered)
        return jobs._Written()

    stored_scope, own_scope = days(0, 3), days(10, 12)
    fetcher = _InterruptedFetcher(deferred=(days(0, 1), days(2, 3)))
    fetched: list[jobs._Fetched] = []
    reads = jobs._QueryReads("Q-G5", write, fetched)
    await reads.read(fetcher, stored_scope, T0)  # type: ignore[arg-type]
    await reads.read(fetcher, own_scope, T2)  # type: ignore[arg-type]
    assert fetcher.fetched == [("Q-G5", stored_scope)], "an interrupted query reads no more"
    assert [(entry.query_id, entry.covered) for entry in fetched] == [
        ("Q-G5", (day, day + timedelta(days=3)))
    ], "the read's rows are kept for the dataset's transaction"
    assert written == [], "reading writes nothing; the dataset's transaction does"
    assert [(dict(entry.scope), entry.deferred_at) for entry in reads.pending] == [
        (days(0, 1), T0),
        (days(2, 3), T0),
        (own_scope, T2),
    ]

    def entry(scope: Mapping[str, date], at: datetime) -> dict[str, object]:
        return {
            "scope": {key: value.isoformat() for key, value in scope.items()},
            "deferred_at": at.isoformat(),
        }

    def checkpoint(pending: Mapping[str, list[dict[str, object]]]) -> dict[str, object]:
        return {"version": 1, "pending": pending, "covered_from": "2026-09-14", "reasons": []}

    loaded = jobs._MetricsCheckpoint.parse(checkpoint({"Q-G5": [entry(days(0, 0), T0)]}), queries)
    current = jobs._MetricsCheckpoint.parse(
        checkpoint(
            {
                "Q-G3": [entry(days(5, 5), T0)],
                "Q-G5": [entry(days(0, 0), T0), entry(days(1, 1), T1)],
            }
        ),
        queries,
    )
    mine = jobs._MetricsCheckpoint.parse(
        checkpoint({"Q-G5": [entry(days(2, 2), T2), entry(days(1, 1), T3)]}), queries
    )
    merged = mine.merged_with(current, loaded)
    assert merged.to_json()["pending"] == {
        "Q-G5": [entry(days(1, 1), T1), entry(days(2, 2), T2)],
        "Q-G3": [entry(days(5, 5), T0)],
    }
    assert jobs._MetricsCheckpoint.parse(merged.to_json(), queries) == merged

    one_day = {START: "2026-09-14", END: "2026-09-14"}
    refused: list[tuple[dict[str, list[dict[str, object]]], str]] = [
        ({"Q-G5": [one_day]}, "must hold exactly"),  # a bare scope, without its instant
        ({"Q-G5": [{"scope": one_day}]}, "must hold exactly"),
        ({"Q-G5": [{"scope": one_day, "deferred_at": "2026-09-14T06:00:00"}]}, "UTC offset"),
        ({"Q-G5": [entry(days(0, 0), T0), entry(days(0, 0), T1)]}, "pending twice"),
        (
            {
                "Q-G5": [
                    {
                        "scope": {
                            START: "2026-09-14",
                            END: "2026-09-15",
                            "campaign_lo": 0,
                            "campaign_hi": 7,
                        },
                        "deferred_at": T0.isoformat(),
                    }
                ]
            },
            "narrowed stored scope covers a single day",
        ),
        ({"Q-G6": [entry(days(0, 0), T0)]}, "does not read"),
    ]
    for pending, error in refused:
        with pytest.raises(ValueError, match=error):
            jobs._MetricsCheckpoint.parse(checkpoint(pending), queries)
