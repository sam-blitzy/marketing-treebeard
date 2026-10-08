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
  discards the building dataset; frozen runs still render after a purge.
* **Cross-dataset purge**: purging a retired HubSpot dataset deletes only its own ``lead_match``
  rows; purging a retired LinkedIn dataset removes its leads and every match referencing them;
  a companion is purged with its parent; no foreign-key error at any step.
* **Companion CRM dataset**: LinkedIn simulated while HubSpot is live reads the companion; the
  Google-to-CRM link reads the live primary; the companion activates and retires with its parent.
* **Isolation**: simulated HubSpot contacts never collide with live ids and leave every
  computation when the live dataset activates.
* **Transient and defects**: 429/5xx/timeout retry three times, then stale, never mock; a 400
  fails the job; the Google call budget defers what it cannot pay for.

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
``BuildOutcome``, ``JobResult``), never on log text.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import Table, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis import metrics, runner
from ads_engine.connectors.errors import AccessDenied
from ads_engine.connectors.http import RetryPolicy
from ads_engine.db import repositories
from ads_engine.db.models import (
    Budget,
    Campaign,
    CrmContact,
    DatasetFreshness,
    Decision,
    FactCampaignDaily,
    FactCreativeDaily,
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
from ads_engine.simulation.scenarios import FIXED_CLOCK, FIXED_NOW, SCENARIOS, Scenario
from ads_engine.simulation.transport import JournalEntry
from ads_engine.sync import freshness, jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.datasets import RETENTION, DatasetManager
from ads_engine.sync.mode import (
    REASON_FORCED,
    REASON_LIVE,
    REASON_NO_CREDENTIALS,
    REASON_RECOVERED,
    BuildOutcome,
    ProbeCheck,
    SourceModeController,
    bootstrap_sources,
)

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
        return self.make_world(entry, seed, clock=FIXED_CLOCK)

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
        simulated=make_world(BASE, clock=FIXED_CLOCK),
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
    ],
)
async def test_build_failure_discards_building_dataset(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, fault: Fault, reason: str
) -> None:
    """Recovery: a build failure discards the building dataset and leaves the simulated one active.

    AAP 0.3.3 "Recovery readiness": the probe (Q-G1) passes, so a live dataset is built; its
    metrics backfill (Q-G3) then keeps failing (a persistent 5xx) or is refused (a 403). The
    building dataset is discarded with every row it wrote, the simulated dataset stays the
    active one, nothing of the discarded build is visible through the active-dataset filter,
    and the source stays in automatic fallback for the next recovery probe.

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
    # A persistent 5xx leaves the metrics freshness unrecorded, so the build is not ready and
    # enters fallback as a failed live build; a 403 is recorded as the denial itself.
    assert connection.reason is not None
    assert reason in connection.reason
    assert outcome.reason is not None
    assert reason in outcome.reason
    assert await recovering.in_fallback("google") is True
    assert [row.id for row in await rig.datasets("google")] == [simulated_id]


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
    ``stale``, the source stays live and no simulated dataset is built.
    """
    fault = _on_linkedin_analytics(scenario)
    attempts = SCENARIOS[scenario].expect.values["attempts"]
    dataset_id = await _live_linkedin(rig)
    before = await rig.freshness_row(dataset_id, "linkedin.analytics_daily")
    assert before is not None
    assert before.last_success_at == T0
    facts_before = await rig.count(FactCreativeDaily, FactCreativeDaily.dataset_id == dataset_id)
    assert facts_before, "the live backfill stored daily creative facts"
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


async def test_query_defect_fails_job_without_retry(rig: Rig) -> None:
    """Defects: a 400 fails the job with its query id, with no retry and no mock.

    AAP 0.3.3 fallback table row 4: HTTP 400 (a GAQL ``QueryError``) is a defect in the
    product. The job fails and its ``sync_run`` rows record the query id; the request is sent
    exactly once; the source stays live (its status ``error``) and no simulated data appears.
    """
    query_id = SCENARIOS[DEFECT_400].expect.values["query_id"]
    assert isinstance(query_id, str)
    dataset_id = await _live_google(rig)
    facts_before = await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
    assert facts_before
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


async def test_call_budget_deferral(rig: Rig) -> None:
    """Transient: calls beyond GOOGLE_DAILY_CALL_BUDGET are deferred; the datasets stay incomplete.

    AAP 0.3.3 "Timeouts and budget": the product's own cap counts every webhook call in the
    sliding 24 h. With the cap one call above what the live build already spent, the next
    metrics run makes that one call, defers the rest and records the affected datasets
    ``incomplete`` with a reason that starts with the deferral prefix, so the next window
    finishes them. The source stays live, the facts already stored are kept, and
    ``source_connection.calls_24h`` shows the cap reached.
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
    datasets_before = [(row.id, row.state) for row in await rig.datasets()]
    journal: list[JournalEntry] = []
    controller = rig.controller({"google": rig.live("google", journal=journal)}, at=T1)
    assert controller.settings.google_daily_call_budget == cap

    report = await jobs.run_source_jobs(
        "google", trigger="manual", jobs=[jobs.JOB_METRICS], controller=controller
    )

    (result,) = report.results
    assert result.completeness == "incomplete"
    assert len(journal) == 1, "only the call the cap still allowed was sent"
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
        assert freshness.needs_repair(row), "the next run widens to finish the deferred pages"
    connection = await rig.connection("google")
    assert (connection.mode, connection.active_dataset_id) == ("live", dataset_id)
    assert connection.calls_24h == cap
    assert [(row.id, row.state) for row in await rig.datasets()] == datasets_before
    assert (
        await rig.count(FactCampaignDaily, FactCampaignDaily.dataset_id == dataset_id)
        == facts_before
    )
    # A deferral is a transient shortfall: the run succeeded with what it could pay for, and
    # the connection reports the source stale until the deferred pages are finished.
    assert (result.status, result.disposition, connection.status) == (
        "succeeded",
        "stale",
        "stale",
    )
