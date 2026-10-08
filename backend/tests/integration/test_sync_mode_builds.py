"""Tests for ``ads_engine.sync.mode`` builds: activation against the current lead/CRM pair.

Covered (AAP 0.3.3, "Staged matching on a CRM dataset change" and "Recovery readiness"):

* A LinkedIn dataset that activates while a HubSpot build settles has its leads staged in
  the build under the activation lock, and attempted there, before the build activates.
* A HubSpot primary that activates while a live LinkedIn build settles gets the build's
  leads staged and attempted before the LinkedIn dataset activates.
* Rows staged in the build's own CRM dataset after its backfill mark that dataset's
  ``hubspot.contacts`` and ``hubspot.associations`` freshness ``unknown`` and rerun the
  backfill; the build activates only once the rerun records them again, never when the
  rerun only fails.
* Rows matched there by another matcher after the backfill, whose contacts have no
  association read, do the same, whether matched before the settle or before the
  activation lock; a build whose matched contacts still lack association reads after
  every settle round is discarded.
* A live LinkedIn build's leads matched in HubSpot's active primary after its backfill (by
  the late matching pass in a primary activated meanwhile, or by another matcher before
  the activation lock) get that primary's CRM sync before the LinkedIn dataset activates,
  with the primary's freshness left as it is; a build whose matches there are never
  hydrated is discarded after every settle round.
* A denial or fallback entry recorded by another controller voids a live build bound to
  the earlier access generation: after its backfill, under the activation lock, and when it
  lands while a recovery probe runs. A denial that lands while a source's first activation
  runs waits for it, because the activation holds the source's newly created row.
* A live LinkedIn dataset with leads waits for an effective CRM dataset; one with no lead
  needs none.
* A simulated LinkedIn build stages every lead its backfill stored, one submitted before
  the backfill range included, in its companion, whose CRM backfill reruns and attempts
  them before the pair activates.

``ads_engine.sync.jobs`` is replaced by :class:`Backfill`, which records the freshness a
backfill records, attempts the ``lead_match`` rows its CRM passes attempt and reads their
matched contacts' associations, and which runs another process's activations or denials at
chosen points. No connector client is opened:
CRM-binding verification, the live matching pass and the CRM hydration pass (or the CRM
sync it runs) are replaced, so no request leaves the process. The database tests run
against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``) in a private schema built from the
model metadata and dropped at teardown, as in ``test_sync_datasets.py``. Every clock stands
at :data:`T0`.
"""

import hashlib
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import anyio
import pytest
from pydantic import SecretStr
from sqlalchemy import and_, create_engine, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from structlog.testing import capture_logs

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.connectors.errors import AccessDenied
from ads_engine.db import repositories
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.crm import CrmAssociationFetch, CrmContact, Lead, LeadMatch
from ads_engine.db.models.ops import DatasetFreshness, SourceConnection, SourceDataset
from ads_engine.settings import Settings
from ads_engine.sync import lead_match, mode
from ads_engine.sync.datasets import DatasetManager
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness
from ads_engine.sync.mode import ProbeCheck, ProbeReport, SourceModeController

type Sessions = async_sessionmaker[AsyncSession]
type Hook = Callable[[], Awaitable[object]]
type Fetch = tuple[str, str | None] | None
"""One association fetch as ``(status, target_read_status)``; ``None`` when none is stored."""

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The instant every clock in this module returns."""

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
"""A staging bound before every lead."""

HUBSPOT_NAMES = ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search")
"""The freshness rows a HubSpot dataset records."""

CRM_RERUN = "{} staged after the CRM backfill; matching, contact refresh and associations re-run"
"""The reason a rerun's ``hubspot.contacts`` and ``hubspot.associations`` rows carry."""

CRM_HYDRATION = (
    "{} without association reads after the CRM backfill; matching, contact refresh and "
    "associations re-run"
)
"""The same reason for contacts matched after the CRM backfill whose associations are unread."""

ASSOCIATED_TYPES = ("meeting", "deal")
"""The target types of a contact's association fetches."""


def _clock() -> datetime:
    """Return :data:`T0`: the fixed clock of every controller and manager here."""
    return T0


def _credential(source: str) -> SecretStr:
    """Return a fake credential assembled at run time; it authenticates nowhere."""
    return SecretStr("-".join(("test", source, uuid.uuid4().hex)))


def _denial() -> AccessDenied:
    """Return the HTTP 403 a HubSpot job of another controller reports."""
    return AccessDenied(
        "Forbidden", source="hubspot", endpoint="crm/objects/contacts", http_status=403
    )


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the source-mode build tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str) -> dict[str, str]:
    """Pin every connection to ``schema`` alone, in UTC like the application's sessions."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"mode_builds_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on empty dataset tables of the private schema."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("TRUNCATE source_dataset, source_connection, analysis_request CASCADE")
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    """Return ``DATA_MODE=auto`` settings with LinkedIn and HubSpot credentials present."""
    return Settings(
        data_mode="auto",
        backfill_days=90,
        hubspot_access_token=_credential("hubspot"),
        linkedin_access_token=_credential("linkedin"),
        linkedin_ad_account_ids=["5080001"],
    )


@pytest.fixture
def manager(sessions: Sessions) -> DatasetManager:
    """Return the dataset manager "another process" uses, on the same database and clock."""
    return DatasetManager(sessions, clock=_clock)


def _controller(settings: Settings, sessions: Sessions) -> SourceModeController:
    """Return a controller on the private schema and the fixed clock."""
    return SourceModeController(settings, sessionmaker=sessions, clock=_clock)


@pytest.fixture
def matching_passes(monkeypatch: pytest.MonkeyPatch, sessions: Sessions) -> list[mode._MatchPass]:
    """Keep every build offline; return the live matching passes builds asked for.

    CRM-binding verification would call the portal's properties API and the matching pass
    LinkedIn and HubSpot, so both are replaced: verification stores nothing, and a matching
    pass attempts its pending rows as a pass that finds no contact does.
    """
    passes: list[mode._MatchPass] = []

    async def verify_nothing(self: SourceModeController, dataset_id: int) -> None:
        """Skip verification: no binding is read by the steps under test."""

    async def attempt_pass(self: SourceModeController, match_pass: mode._MatchPass) -> None:
        """Attempt the pass's pending rows once, recording the pass."""
        passes.append(match_pass)
        await _attempt(sessions, match_pass.crm_dataset_id, match_pass.lead_dataset_id)

    monkeypatch.setattr(SourceModeController, "verify_crm_bindings", verify_nothing)
    monkeypatch.setattr(SourceModeController, "_matching_pass", attempt_pass)
    return passes


@pytest.fixture
def hydration_passes(
    monkeypatch: pytest.MonkeyPatch, sessions: Sessions
) -> list[tuple[int, int, int]]:
    """Keep a live LinkedIn build's CRM hydration offline; return the passes it ran.

    The hydration pass runs HubSpot's CRM sync job in the primary the build's leads were
    matched in, which would call HubSpot. It is replaced by the association pass that job
    records (:func:`_hydrate`). Each pass is recorded as ``(crm_dataset_id,
    lead_dataset_id, unhydrated)``.
    """
    passes: list[tuple[int, int, int]] = []

    async def hydrate(
        self: SourceModeController, crm_dataset_id: int, lead_dataset_id: int, unhydrated: int
    ) -> None:
        """Read the associations of the CRM dataset's matched contacts, recording the pass."""
        passes.append((crm_dataset_id, lead_dataset_id, unhydrated))
        await _hydrate(sessions, crm_dataset_id)

    monkeypatch.setattr(SourceModeController, "_hydration_pass", hydrate)
    return passes


class Backfill:
    """Stands in for ``ads_engine.sync.jobs.backfill_dataset(controller, dataset_id, *, now)``.

    Like the backfill it records every freshness row the dataset requires and attempts the
    ``lead_match`` rows its CRM passes own:

    * HubSpot (the CRM sync job with ``backfill=True``): attempts the dataset's pending rows,
      reads the contact-to-meeting and contact-to-deal associations of every matched
      contact that has none read yet (:func:`_hydrate`), then records ``hubspot.*``. With
      :attr:`failing_reruns` a second backfill of the same dataset reads no association and
      records only failures, as if contact refresh and associations kept failing.
    * Live LinkedIn: stores :attr:`linkedin_leads` leads, stages and attempts them in
      HubSpot's active primary (the lead-match job), then records ``linkedin.*``.
    * Simulated LinkedIn: on its first backfill stores one lead per age in
      :attr:`simulated_leads`, submitted that long before :data:`T0`, with no row in its
      companion (a rerun stores none: the real backfill upserts the same leads). Then runs
      the same CRM pass on its companion (attempt, associations, ``hubspot.*``) and records
      its own ``linkedin.*``.

    :attr:`after` maps a call number (1-based, over every call) to a hook run at the end of
    that call: another process acting while the build's backfill runs.

    Attributes:
        calls: The dataset of each call, in order.
        seen: Per HubSpot dataset, its freshness at the start of each of its backfills.
        states: Per dataset, its state at the start of each of its backfills.
    """

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions
        self.calls: list[int] = []
        self.linkedin_leads = 0
        self.simulated_leads: list[timedelta] = []
        self.failing_reruns = False
        self.after: dict[int, Hook] = {}
        self.seen: dict[int, list[dict[str, tuple[str, str | None]]]] = {}
        self.states: dict[int, list[str]] = {}

    async def __call__(
        self, controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> None:
        """Backfill one building dataset (see the class docstring)."""
        self.calls.append(dataset_id)
        number = len(self.calls)
        dataset = await controller.datasets.get(dataset_id)
        self.states.setdefault(dataset_id, []).append(dataset.state)
        if dataset.source == "hubspot":
            self.seen.setdefault(dataset_id, []).append(await _freshness(self.sessions, dataset_id))
            failed = self.failing_reruns and self.calls.count(dataset_id) > 1
            await _attempt(self.sessions, dataset_id)
            if not failed:
                await _hydrate(self.sessions, dataset_id)
            await _record(controller.datasets, self.sessions, dataset_id, failed=failed)
        elif dataset.source == "linkedin" and dataset.mode == "live":
            await _leads(self.sessions, dataset_id, self.linkedin_leads)
            await _match_in_active_hubspot(self.sessions, dataset_id)
            await _record(controller.datasets, self.sessions, dataset_id)
        else:
            if dataset.source == "linkedin":
                if self.calls.count(dataset_id) == 1:
                    await _aged_leads(self.sessions, dataset_id, self.simulated_leads)
                companion = await controller.datasets.companion_of(dataset_id)
                if companion is not None:
                    await _attempt(self.sessions, companion)
                    await _hydrate(self.sessions, companion)
                    await _record(controller.datasets, self.sessions, companion)
            await _record(controller.datasets, self.sessions, dataset_id)
        hook = self.after.pop(number, None)
        if hook is not None:
            await hook()


@pytest.fixture
def backfill(monkeypatch: pytest.MonkeyPatch, sessions: Sessions) -> Backfill:
    """Install :class:`Backfill` as ``ads_engine.sync.jobs`` for this test."""
    stub = Backfill(sessions)
    jobs = types.ModuleType("ads_engine.sync.jobs")
    jobs.backfill_dataset = stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ads_engine.sync.jobs", jobs)
    return stub


def _after_first_settle(monkeypatch: pytest.MonkeyPatch, hook: Hook) -> None:
    """Run ``hook`` once, right after a build first settles and before it takes the lock."""
    original = SourceModeController._settle_matching
    pending = [hook]

    async def settle(self: SourceModeController, *args: Any, **kwargs: Any) -> Any:
        """Settle as usual, then let another process act."""
        readiness = await original(self, *args, **kwargs)
        if pending:
            await pending.pop()()
        return readiness

    monkeypatch.setattr(SourceModeController, "_settle_matching", settle)


# ---------------------------------------------------------------------------------------
# Writers and readers
# ---------------------------------------------------------------------------------------


async def _record(
    datasets: DatasetManager, sessions: Sessions, dataset_id: int, *, failed: bool = False
) -> None:
    """Record every required freshness row of a dataset: a complete success, or a failure."""
    names = await datasets.required_freshness(dataset_id)
    async with sessions() as session, session.begin():
        for name in names:
            if failed:
                outcome = FreshnessOutcome.failure(T0, reason="HTTP 503 after 3 attempts")
            else:
                outcome = FreshnessOutcome.success(
                    T0, covered_from=None, covered_through=None, complete=True
                )
            await record_freshness(session, dataset_id, name, outcome)


async def _leads(sessions: Sessions, dataset_id: int, count: int) -> list[int]:
    """Commit ``count`` leads of one LinkedIn campaign in a dataset; return their ids.

    The lead at index ``i`` was submitted ``i + 1`` days before :data:`T0`.
    """
    return await _aged_leads(
        sessions, dataset_id, [timedelta(days=index + 1) for index in range(count)]
    )


async def _aged_leads(sessions: Sessions, dataset_id: int, ages: Sequence[timedelta]) -> list[int]:
    """Commit one lead of one LinkedIn campaign per age, submitted that long before T0.

    Returns:
        The leads' ids, in the order of ``ages``.
    """
    if not ages:
        return []
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=f"5080{dataset_id}",
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id=f"61{dataset_id}",
            name="White paper A",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("25.00"),
        )
        session.add(campaign)
        await session.flush()
        leads = [
            Lead(
                source="linkedin",
                dataset_id=dataset_id,
                external_id=f"{dataset_id}-{index}",
                submitted_at=T0 - age,
                campaign_id=campaign.id,
                creative_id=None,
                lead_form_urn="urn:li:leadGenForm:7",
                lead_form_version=1,
                email_sha256=hashlib.sha256(
                    f"lead{dataset_id}.{index}@x.test".encode()
                ).hexdigest(),
            )
            for index, age in enumerate(ages)
        ]
        session.add_all(leads)
        await session.flush()
        return [lead.id for lead in leads]


async def _attempt(
    sessions: Sessions, crm_dataset_id: int, lead_dataset_id: int | None = None
) -> None:
    """Attempt the pending rows of a CRM dataset once, as a pass that finds no contact.

    Rows of leads in a retired LinkedIn dataset are left alone, as matching leaves them.
    """
    leads = (
        select(Lead.id)
        .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
        .where(SourceDataset.state != "retired")
    )
    if lead_dataset_id is not None:
        leads = leads.where(Lead.dataset_id == lead_dataset_id)
    async with sessions() as session, session.begin():
        await session.execute(
            update(LeadMatch)
            .where(
                LeadMatch.crm_dataset_id == crm_dataset_id,
                LeadMatch.status == "pending",
                LeadMatch.lead_id.in_(leads),
            )
            .values(status="no_match", attempts=LeadMatch.attempts + 1, last_checked_at=T0)
        )


async def _hydrate(sessions: Sessions, crm_dataset_id: int) -> None:
    """Read the associations of every matched contact of a CRM dataset that has none read.

    As the CRM sync job's association pass records them: one contact-to-meeting and one
    contact-to-deal fetch per contact, here ``ok_empty`` (a genuine zero, nothing to read).
    A fetch already recorded is left as it is.
    """
    async with sessions() as session, session.begin():
        hs_ids = list(
            await session.scalars(
                select(CrmContact.hs_id)
                .join(
                    LeadMatch,
                    and_(
                        LeadMatch.contact_id == CrmContact.id,
                        LeadMatch.crm_dataset_id == CrmContact.dataset_id,
                    ),
                )
                .where(CrmContact.dataset_id == crm_dataset_id, LeadMatch.status == "matched")
                .distinct()
            )
        )
        if not hs_ids:
            return
        await session.execute(
            insert(CrmAssociationFetch)
            .values(
                [
                    {
                        "source": "hubspot",
                        "dataset_id": crm_dataset_id,
                        "object_type": "contact",
                        "object_id": hs_id,
                        "to_type": to_type,
                        "status": "ok_empty",
                        "target_read_status": None,
                        "fetched_at": T0,
                    }
                    for hs_id in hs_ids
                    for to_type in ASSOCIATED_TYPES
                ]
            )
            .on_conflict_do_nothing(index_elements=list(CrmAssociationFetch.__natural_key__))
        )


async def _match_elsewhere(sessions: Sessions, crm_dataset_id: int, lead_ids: list[int]) -> None:
    """Match leads' rows in a CRM dataset as another process's matcher does.

    Each lead's row finds a new contact, read ``ok`` by the email batch read; matching reads
    none of the contact's associations.
    """
    async with sessions() as session, session.begin():
        for lead_id in lead_ids:
            contact = CrmContact(
                source="hubspot",
                dataset_id=crm_dataset_id,
                hs_id=f"7{lead_id:08d}",
                lifecycle_stage="lead",
            )
            session.add(contact)
            await session.flush()
            await session.execute(
                update(LeadMatch)
                .where(LeadMatch.crm_dataset_id == crm_dataset_id, LeadMatch.lead_id == lead_id)
                .values(
                    status="matched",
                    attempts=LeadMatch.attempts + 1,
                    last_checked_at=T0,
                    next_attempt_at=None,
                    contact_id=contact.id,
                    contact_read_status="ok",
                )
            )


async def _pending_leads(
    sessions: Sessions, crm_dataset_id: int, lead_dataset_id: int
) -> list[int]:
    """Return the ids of a LinkedIn dataset's leads whose row in a CRM dataset is pending."""
    async with sessions() as session:
        return list(
            await session.scalars(
                select(LeadMatch.lead_id)
                .join(Lead, Lead.id == LeadMatch.lead_id)
                .where(
                    LeadMatch.crm_dataset_id == crm_dataset_id,
                    LeadMatch.status == "pending",
                    Lead.dataset_id == lead_dataset_id,
                )
                .order_by(LeadMatch.lead_id)
            )
        )


async def _match_in_active_hubspot(sessions: Sessions, lead_dataset_id: int) -> None:
    """Stage and attempt a LinkedIn dataset's leads in HubSpot's active primary, if any."""
    async with sessions() as session, session.begin():
        crm_dataset_id = await repositories.active_dataset_id(session, "hubspot")
        if crm_dataset_id is not None:
            await lead_match.stage_rows(
                session, crm_dataset_id, lead_dataset_id, since=EPOCH, now=T0
            )
    if crm_dataset_id is not None:
        await _attempt(sessions, crm_dataset_id, lead_dataset_id)


async def _activate_hubspot(manager: DatasetManager, sessions: Sessions, mode_: str) -> int:
    """Build and activate a HubSpot primary as another process would; return its id."""
    dataset_id = await manager.begin("hubspot", mode_, reason="another process")
    await _record(manager, sessions, dataset_id)
    await manager.activate(dataset_id)
    return dataset_id


async def _activate_live_linkedin(
    manager: DatasetManager, sessions: Sessions, leads: int
) -> tuple[int, list[int]]:
    """Build and activate a live LinkedIn dataset as another process would.

    Its leads are staged and attempted in HubSpot's active primary first, so it is ready.

    Returns:
        The dataset's id and its leads' ids.
    """
    dataset_id = await manager.begin("linkedin", "live", reason="another process")
    lead_ids = await _leads(sessions, dataset_id, leads)
    await _match_in_active_hubspot(sessions, dataset_id)
    await _record(manager, sessions, dataset_id)
    await manager.activate(dataset_id)
    return dataset_id, lead_ids


async def _activate_simulated_linkedin(manager: DatasetManager, sessions: Sessions) -> int:
    """Build and activate a simulated LinkedIn dataset with its companion; return its id."""
    dataset_id = await manager.begin("linkedin", "simulated", reason="credentials not configured")
    companion = await manager.begin("hubspot", "simulated", "companion", dataset_id)
    await _record(manager, sessions, companion)
    await _record(manager, sessions, dataset_id)
    await manager.activate(dataset_id)
    return dataset_id


async def _state(sessions: Sessions, dataset_id: int) -> str | None:
    """Return a dataset's state, or ``None`` when it does not exist (discarded)."""
    async with sessions() as session:
        return await session.scalar(
            select(SourceDataset.state).where(SourceDataset.id == dataset_id)
        )


async def _connection(sessions: Sessions, source: str) -> SourceConnection:
    """Return a source's connection row as committed now."""
    async with sessions() as session:
        row = await session.scalar(
            select(SourceConnection).where(SourceConnection.source == source)
        )
    assert row is not None
    return row


async def _lead_ids(sessions: Sessions, dataset_id: int) -> list[int]:
    """Return the ids of a LinkedIn dataset's leads, ascending."""
    async with sessions() as session:
        return list(
            await session.scalars(
                select(Lead.id).where(Lead.dataset_id == dataset_id).order_by(Lead.id)
            )
        )


async def _matches(sessions: Sessions, crm_dataset_id: int, lead_ids: list[int]) -> dict[int, str]:
    """Return the status of each given lead's row in a CRM dataset, by lead id."""
    async with sessions() as session:
        rows = await session.execute(
            select(LeadMatch.lead_id, LeadMatch.status).where(
                LeadMatch.crm_dataset_id == crm_dataset_id, LeadMatch.lead_id.in_(lead_ids)
            )
        )
        return {row.lead_id: row.status for row in rows}


async def _freshness(sessions: Sessions, dataset_id: int) -> dict[str, tuple[str, str | None]]:
    """Return each freshness row of a dataset as ``(completeness, reason)``, by name."""
    async with sessions() as session:
        rows = await session.scalars(
            select(DatasetFreshness).where(DatasetFreshness.dataset_id == dataset_id)
        )
        return {row.dataset: (row.completeness, row.reason) for row in rows}


async def _matched_contacts(sessions: Sessions, crm_dataset_id: int) -> set[str]:
    """Return the HubSpot ids of a CRM dataset's contacts that a ``matched`` row names."""
    async with sessions() as session:
        return set(
            await session.scalars(
                select(CrmContact.hs_id)
                .join(
                    LeadMatch,
                    and_(
                        LeadMatch.contact_id == CrmContact.id,
                        LeadMatch.crm_dataset_id == CrmContact.dataset_id,
                    ),
                )
                .where(CrmContact.dataset_id == crm_dataset_id, LeadMatch.status == "matched")
            )
        )


async def _contact_fetches(sessions: Sessions, crm_dataset_id: int) -> dict[str, set[str]]:
    """Return the target types of each contact's association fetches in a CRM dataset."""
    async with sessions() as session:
        rows = await session.execute(
            select(CrmAssociationFetch.object_id, CrmAssociationFetch.to_type).where(
                CrmAssociationFetch.dataset_id == crm_dataset_id,
                CrmAssociationFetch.object_type == "contact",
            )
        )
        fetches: dict[str, set[str]] = {}
        for row in rows:
            fetches.setdefault(row.object_id, set()).add(row.to_type)
        return fetches


async def _lock_waiters(sessions: Sessions) -> int:
    """Count the sessions of this database waiting for a lock in a ``source_connection`` query."""
    async with sessions() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND wait_event_type = 'Lock' AND query ILIKE '%source_connection%'"
            )
        )
    return int(count or 0)


def _events(entries: list[Any], event: str) -> list[Any]:
    """Return the captured log entries of one event."""
    return [entry for entry in entries if entry.get("event") == event]


# ---------------------------------------------------------------------------------------
# BE5-F01: the effective lead/CRM pair at activation
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_hubspot_build_stages_a_linkedin_dataset_activated_before_its_lock(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    first_linkedin, first_leads = await _activate_live_linkedin(manager, sessions, 2)
    activated: list[tuple[int, list[int]]] = []

    async def linkedin_activates() -> None:
        """LinkedIn activates a new live dataset after the build settled, before its lock."""
        activated.append(await _activate_live_linkedin(manager, sessions, 3))

    _after_first_settle(monkeypatch, linkedin_activates)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    [(new_linkedin, new_leads)] = activated
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode, outcome.retired) == (True, "live", (previous,))
    assert await _state(sessions, first_linkedin) == "retired"
    assert await _state(sessions, new_linkedin) == "active"
    # The activation lock staged the new dataset's leads, sending the build back to
    # settle: its CRM backfill reran for them before it could activate.
    assert backfill.calls == [building, building]
    assert await _matches(sessions, building, new_leads) == dict.fromkeys(new_leads, "no_match")
    assert await _matches(sessions, building, first_leads) == dict.fromkeys(first_leads, "no_match")
    assert matching_passes == []
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        building,
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_build_is_matched_in_a_hubspot_primary_activated_before_its_lock(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced = await _activate_hubspot(manager, sessions, "live")
    previous = await _activate_simulated_linkedin(manager, sessions)
    backfill.linkedin_leads = 2
    activated: list[int] = []

    async def hubspot_activates() -> None:
        """HubSpot activates a new primary after the LinkedIn build settled."""
        activated.append(await _activate_hubspot(manager, sessions, "live"))

    _after_first_settle(monkeypatch, hubspot_activates)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    [current] = activated
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode) == (True, "live")
    assert previous in outcome.retired
    assert await _state(sessions, replaced) == "retired"
    leads = await _lead_ids(sessions, building)
    assert len(leads) == 2
    # Matched in the replaced primary by the backfill, then staged in the new one under the
    # activation lock and attempted there by one more matching pass before activation.
    assert await _matches(sessions, replaced, leads) == dict.fromkeys(leads, "no_match")
    assert await _matches(sessions, current, leads) == dict.fromkeys(leads, "no_match")
    assert matching_passes == [mode._MatchPass(current, "live", "live", building)]
    assert backfill.calls == [building]
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        building,
    )


# ---------------------------------------------------------------------------------------
# BE5-F02: rows staged in the build's own CRM dataset after its backfill
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_rows_staged_after_the_crm_backfill_rerun_it_before_activation(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    await _activate_live_linkedin(manager, sessions, 2)
    activated: list[tuple[int, list[int]]] = []

    async def linkedin_activates() -> None:
        """LinkedIn activates a new live dataset while the CRM backfill runs."""
        activated.append(await _activate_live_linkedin(manager, sessions, 1))

    backfill.after[1] = linkedin_activates
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    [(_, new_leads)] = activated
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.retired) == (True, (previous,))
    assert backfill.calls == [building, building]
    # The rerun started with the contact and association rows withdrawn, while the build was
    # still building and the previous dataset active; only its success made it ready.
    rerun = backfill.seen[building][1]
    withdrawn = ("unknown", CRM_RERUN.format("1 lead match"))
    assert rerun["hubspot.contacts"] == withdrawn
    assert rerun["hubspot.associations"] == withdrawn
    assert rerun["hubspot.deal_search"] == ("complete", None)
    assert backfill.states[building] == ["building", "building"]
    assert await _freshness(sessions, building) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    assert await _matches(sessions, building, new_leads) == dict.fromkeys(new_leads, "no_match")
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_a_lead_staged_during_the_crm_rerun_is_settled_in_another_round(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    await _activate_live_linkedin(manager, sessions, 2)
    activated: list[tuple[int, list[int]]] = []
    late: list[int] = []

    async def linkedin_activates() -> None:
        """LinkedIn activates a new live dataset while the CRM backfill runs."""
        activated.append(await _activate_live_linkedin(manager, sessions, 1))

    async def lead_sync_stages_a_new_lead() -> None:
        """A lead sync stores a new lead and stages it in the building CRM dataset."""
        [(linkedin, _)] = activated
        building_id = backfill.calls[-1]
        async with sessions() as session, session.begin():
            campaign_id = await session.scalar(
                select(Campaign.id).where(Campaign.dataset_id == linkedin)
            )
            assert campaign_id is not None
            lead = Lead(
                source="linkedin",
                dataset_id=linkedin,
                external_id=f"{linkedin}-late",
                submitted_at=T0 - timedelta(hours=1),
                campaign_id=campaign_id,
                creative_id=None,
                lead_form_urn="urn:li:leadGenForm:7",
                lead_form_version=1,
                email_sha256=hashlib.sha256(b"late.lead@x.test").hexdigest(),
            )
            session.add(lead)
            await session.flush()
            await lead_match.create_pending(session, [lead.id], building_id, now=T0)
            late.append(lead.id)

    backfill.after[1] = linkedin_activates
    backfill.after[2] = lead_sync_stages_a_new_lead
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.retired) == (True, (previous,))
    # The lead staged while the first rerun ran was still pending after it, so the build
    # settled again: a second rerun attempted it and recorded the CRM evidence once more.
    assert backfill.calls == [building, building, building]
    assert backfill.seen[building][2]["hubspot.contacts"] == (
        "unknown",
        CRM_RERUN.format("1 lead match"),
    )
    assert await _matches(sessions, building, late) == dict.fromkeys(late, "no_match")
    assert await _freshness(sessions, building) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_a_lead_staged_just_before_the_activation_writes_is_settled_in_another_round(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    linkedin, _ = await _activate_live_linkedin(manager, sessions, 1)
    original = DatasetManager.activate
    late: list[int] = []

    async def lead_sync_commits_first(self: DatasetManager, dataset_id: int, **kwargs: Any) -> Any:
        """A lead sync commits a new lead, staged in both HubSpot datasets, just before."""
        if not late:
            async with sessions() as session, session.begin():
                campaign_id = await session.scalar(
                    select(Campaign.id).where(Campaign.dataset_id == linkedin)
                )
                assert campaign_id is not None
                lead = Lead(
                    source="linkedin",
                    dataset_id=linkedin,
                    external_id=f"{linkedin}-concurrent",
                    submitted_at=T0 - timedelta(hours=1),
                    campaign_id=campaign_id,
                    creative_id=None,
                    lead_form_urn="urn:li:leadGenForm:7",
                    lead_form_version=1,
                    email_sha256=hashlib.sha256(b"concurrent.lead@x.test").hexdigest(),
                )
                session.add(lead)
                await session.flush()
                late.append(lead.id)
                await lead_match.create_pending(session, late, previous, now=T0)
                await lead_match.create_pending(session, late, dataset_id, now=T0)
        return await original(self, dataset_id, **kwargs)

    monkeypatch.setattr(DatasetManager, "activate", lead_sync_commits_first)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    # DatasetManager.activate refused the build under the lock; nothing was written, so the
    # build settled again (its CRM backfill reran for the new row) and then activated.
    assert (outcome.activated, outcome.retired) == (True, (previous,))
    assert backfill.calls == [building, building]
    assert await _matches(sessions, building, late) == dict.fromkeys(late, "no_match")
    assert await _state(sessions, previous) == "retired"


@pytest.mark.anyio
@pytest.mark.db
async def test_a_failed_crm_rerun_leaves_the_evidence_unknown_and_the_build_unactivated(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    await _activate_live_linkedin(manager, sessions, 2)

    async def linkedin_activates() -> None:
        """LinkedIn activates a new live dataset while the CRM backfill runs."""
        await _activate_live_linkedin(manager, sessions, 2)

    backfill.after[1] = linkedin_activates
    backfill.failing_reruns = True
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    not_ready = "not ready: freshness unknown: hubspot.contacts, hubspot.associations"
    assert (outcome.activated, outcome.reason) == (False, not_ready)
    assert backfill.calls == [building, building]
    rerun = backfill.seen[building][1]
    assert rerun["hubspot.contacts"] == ("unknown", CRM_RERUN.format("2 lead matches"))
    # Discarded; the simulated dataset stays active and recovery probes take over.
    assert await _state(sessions, building) is None
    assert await _state(sessions, previous) == "active"
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        previous,
    )
    assert connection.reason == f"live build failed: {not_ready}"
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_rows_matched_elsewhere_after_the_crm_backfill_still_rerun_it(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    await _activate_live_linkedin(manager, sessions, 2)
    activated: list[tuple[int, list[int]]] = []

    async def linkedin_activates() -> None:
        """LinkedIn activates a new live dataset while the CRM backfill runs."""
        activated.append(await _activate_live_linkedin(manager, sessions, 1))

    original = SourceModeController._restage
    matched: list[int] = []

    async def restage_then_matched_elsewhere(
        self: SourceModeController,
        source: Any,
        mode_: Any,
        dataset_id: int,
        companion_id: int | None,
    ) -> Any:
        """Stage as usual; after the backfill, another matcher matches the new rows at once."""
        match_pass = await original(self, source, mode_, dataset_id, companion_id)
        if len(backfill.calls) == 1 and not matched:
            [(_, new_leads)] = activated
            await _match_elsewhere(sessions, dataset_id, new_leads)
            matched.extend(new_leads)
        return match_pass

    backfill.after[1] = linkedin_activates
    monkeypatch.setattr(SourceModeController, "_restage", restage_then_matched_elsewhere)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.retired) == (True, (previous,))
    assert len(matched) == 1
    assert await _matches(sessions, building, matched) == dict.fromkeys(matched, "matched")
    # No row was pending when the settle read readiness, yet the matched contact's evidence
    # was withdrawn and the CRM backfill reran, while the build was still building.
    assert backfill.calls == [building, building]
    assert backfill.states[building] == ["building", "building"]
    rerun = backfill.seen[building][1]
    withdrawn = ("unknown", CRM_HYDRATION.format("1 matched lead"))
    assert rerun["hubspot.contacts"] == withdrawn
    assert rerun["hubspot.associations"] == withdrawn
    assert rerun["hubspot.deal_search"] == ("complete", None)
    assert await _freshness(sessions, building) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    # Every matched contact of the activated dataset has both association fetches.
    contacts = await _matched_contacts(sessions, building)
    assert len(contacts) == 1
    assert await _contact_fetches(sessions, building) == {
        contact: set(ASSOCIATED_TYPES) for contact in contacts
    }
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_contacts_matched_before_the_lock_hold_activation_until_their_associations_are_read(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    _, leads = await _activate_live_linkedin(manager, sessions, 2)
    found = leads[:1]

    async def matched_elsewhere() -> None:
        """Another matcher's retry finds a contact for an attempted lead, before the lock."""
        await _match_elsewhere(sessions, backfill.calls[-1], found)

    _after_first_settle(monkeypatch, matched_elsewhere)
    with capture_logs() as entries, anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.retired) == (True, (previous,))
    # Refused under the activation lock, then the next round reran the CRM backfill for the
    # contact before the build activated.
    [refused] = _events(entries, "sync.build.unhydrated")
    assert (refused["dataset_id"], refused["crm_dataset_id"], refused["unhydrated"]) == (
        building,
        building,
        1,
    )
    assert backfill.calls == [building, building]
    rerun = backfill.seen[building][1]
    assert rerun["hubspot.contacts"] == ("unknown", CRM_HYDRATION.format("1 matched lead"))
    assert rerun["hubspot.associations"] == ("unknown", CRM_HYDRATION.format("1 matched lead"))
    assert await _matches(sessions, building, leads) == {
        found[0]: "matched",
        leads[1]: "no_match",
    }
    contacts = await _matched_contacts(sessions, building)
    assert len(contacts) == 1
    assert await _contact_fetches(sessions, building) == {
        contact: set(ASSOCIATED_TYPES) for contact in contacts
    }
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        building,
    )
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_contacts_still_without_association_reads_after_every_round_fail_the_build(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = await _activate_hubspot(manager, sessions, "simulated")
    _, leads = await _activate_live_linkedin(manager, sessions, 3)
    original = SourceModeController._settle_matching
    remaining = list(leads)

    async def settle_then_matched_elsewhere(
        self: SourceModeController, *args: Any, **kwargs: Any
    ) -> Any:
        """Settle as usual; then another matcher matches one more lead before the lock."""
        readiness = await original(self, *args, **kwargs)
        await _match_elsewhere(sessions, backfill.calls[-1], [remaining.pop(0)])
        return readiness

    monkeypatch.setattr(SourceModeController, "_settle_matching", settle_then_matched_elsewhere)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    lacking = (
        "matched contacts still lack association reads after 3 rounds: 1 matched lead in CRM "
        f"dataset {building}"
    )
    assert (outcome.activated, outcome.reason) == (False, lacking)
    assert remaining == []
    # Rounds two and three each reran the CRM backfill for the contact matched before.
    assert backfill.calls == [building, building, building]
    assert await _state(sessions, building) is None
    assert await _state(sessions, previous) == "active"
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        previous,
    )
    assert connection.reason == f"live build failed: {lacking}"
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_unhydrated_matches_counts_matched_contacts_without_association_reads(
    sessions: Sessions, manager: DatasetManager
) -> None:
    other_crm = await _activate_hubspot(manager, sessions, "simulated")
    retired_linkedin, retired_leads = await _activate_live_linkedin(manager, sessions, 1)
    crm = await manager.begin("hubspot", "live", reason="recovered")
    async with sessions() as session, session.begin():
        await lead_match.stage_rows(session, crm, retired_linkedin, since=EPOCH, now=T0)
    # Matched with no association read, but its LinkedIn dataset retires below: not counted.
    await _match_elsewhere(sessions, crm, retired_leads)
    linkedin, leads = await _activate_live_linkedin(manager, sessions, 7)
    assert await _state(sessions, retired_linkedin) == "retired"
    async with sessions() as session, session.begin():
        await lead_match.stage_rows(session, crm, linkedin, since=EPOCH, now=T0)
    # Leads 0-5 are matched; lead 6 stays pending.
    await _match_elsewhere(sessions, crm, leads[:6])

    fetched: dict[int, tuple[Fetch, Fetch]] = {
        # (meeting fetch, deal fetch) as (status, target_read_status); None: no fetch row.
        0: (("ok_empty", None), ("ok_empty", None)),  # both read: hydrated
        1: (("ok", "ok"), ("error", None)),  # targets read, failure recorded: hydrated
        2: (("partial", "partial"), None),  # no deal fetch: counted
        3: (("ok", None), ("ok_empty", None)),  # meeting targets not read: counted
        4: (None, None),  # nothing read in this dataset: counted
        5: (None, None),  # contact refresh failed: not counted
    }
    async with sessions() as session, session.begin():
        rows = await session.execute(
            select(LeadMatch.lead_id, CrmContact.hs_id)
            .join(CrmContact, CrmContact.id == LeadMatch.contact_id)
            .where(LeadMatch.crm_dataset_id == crm)
        )
        hs_ids = {row.lead_id: row.hs_id for row in rows}
        for index, outcomes in fetched.items():
            for to_type, outcome in zip(ASSOCIATED_TYPES, outcomes, strict=True):
                if outcome is not None:
                    session.add(
                        CrmAssociationFetch(
                            source="hubspot",
                            dataset_id=crm,
                            object_type="contact",
                            object_id=hs_ids[leads[index]],
                            to_type=to_type,
                            status=outcome[0],
                            target_read_status=outcome[1],
                            fetched_at=T0,
                        )
                    )
        # Lead 4's contact id read in another CRM dataset does not hydrate it in this one.
        for to_type in ASSOCIATED_TYPES:
            session.add(
                CrmAssociationFetch(
                    source="hubspot",
                    dataset_id=other_crm,
                    object_type="contact",
                    object_id=hs_ids[leads[4]],
                    to_type=to_type,
                    status="ok_empty",
                    target_read_status=None,
                    fetched_at=T0,
                )
            )
        await session.execute(
            update(LeadMatch)
            .where(LeadMatch.crm_dataset_id == crm, LeadMatch.lead_id == leads[5])
            .values(contact_read_status="error")
        )

    assert await manager.unhydrated_matches(crm) == 3
    async with sessions() as session:
        assert await manager.unhydrated_matches(crm, session=session) == 3
    assert await manager.unhydrated_matches(other_crm) == 0
    # Readiness counts attempted rows and freshness only: the pending lead, no fetch.
    readiness = await manager.readiness(crm)
    assert readiness.pending_matches == 1
    assert readiness.reasons == (
        f"freshness not yet recorded: {', '.join(HUBSPOT_NAMES)}",
        f"1 lead match not yet attempted in CRM dataset {crm}",
    )


# ---------------------------------------------------------------------------------------
# AAP2-F02: a live LinkedIn build's matches in HubSpot's active primary
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_matches_in_a_hubspot_primary_activated_during_a_live_linkedin_build_are_hydrated(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    hydration_passes: list[tuple[int, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced = await _activate_hubspot(manager, sessions, "live")
    previous = await _activate_simulated_linkedin(manager, sessions)
    backfill.linkedin_leads = 2
    activated: list[int] = []

    async def hubspot_activates() -> None:
        """HubSpot activates a new primary after the LinkedIn build settled."""
        activated.append(await _activate_hubspot(manager, sessions, "live"))

    async def pass_finds_contacts(self: SourceModeController, match_pass: mode._MatchPass) -> None:
        """Attempt the pass's pending rows once, each finding a contact; record the pass."""
        matching_passes.append(match_pass)
        assert match_pass.lead_dataset_id is not None
        found = await _pending_leads(
            sessions, match_pass.crm_dataset_id, match_pass.lead_dataset_id
        )
        await _match_elsewhere(sessions, match_pass.crm_dataset_id, found)

    monkeypatch.setattr(SourceModeController, "_matching_pass", pass_finds_contacts)
    _after_first_settle(monkeypatch, hubspot_activates)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    [current] = activated
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode) == (True, "live")
    assert previous in outcome.retired
    assert await _state(sessions, replaced) == "retired"
    leads = await _lead_ids(sessions, building)
    assert len(leads) == 2
    # Staged in the new primary under the activation lock and matched there by the late
    # matching pass; then that primary's CRM sync read every new match's associations.
    assert await _matches(sessions, current, leads) == dict.fromkeys(leads, "matched")
    assert matching_passes == [mode._MatchPass(current, "live", "live", building)]
    assert hydration_passes == [(current, building, 2)]
    contacts = await _matched_contacts(sessions, current)
    assert len(contacts) == 2
    assert await _contact_fetches(sessions, current) == {
        contact: set(ASSOCIATED_TYPES) for contact in contacts
    }
    assert await manager.unhydrated_matches(current, lead_dataset_id=building) == 0
    # The primary is in use: its evidence is never withdrawn, and no backfill reruns.
    assert await _freshness(sessions, current) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    assert backfill.calls == [building]
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        building,
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_contacts_matched_before_the_lock_hold_activation_until_hydrated(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    hydration_passes: list[tuple[int, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm = await _activate_hubspot(manager, sessions, "live")
    await _activate_simulated_linkedin(manager, sessions)
    backfill.linkedin_leads = 2
    found: list[int] = []

    async def matched_elsewhere() -> None:
        """Another matcher's retry finds a contact for one of the build's leads, before the lock."""
        found.extend((await _lead_ids(sessions, backfill.calls[-1]))[:1])
        await _match_elsewhere(sessions, crm, found)

    _after_first_settle(monkeypatch, matched_elsewhere)
    with capture_logs() as entries, anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode) == (True, "live")
    # Refused under the activation lock for the match in HubSpot's primary, then the next
    # round ran that primary's CRM sync for it before the build activated.
    [refused] = _events(entries, "sync.build.unhydrated")
    assert (
        refused["dataset_id"],
        refused["crm_dataset_id"],
        refused["lead_dataset_id"],
        refused["unhydrated"],
    ) == (building, crm, building, 1)
    assert hydration_passes == [(crm, building, 1)]
    leads = await _lead_ids(sessions, building)
    assert await _matches(sessions, crm, leads) == {found[0]: "matched", leads[1]: "no_match"}
    contacts = await _matched_contacts(sessions, crm)
    assert len(contacts) == 1
    assert await _contact_fetches(sessions, crm) == {
        contact: set(ASSOCIATED_TYPES) for contact in contacts
    }
    assert await _freshness(sessions, crm) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    assert backfill.calls == [building]
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_matches_never_hydrated_in_the_hubspot_primary_fail_the_build(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crm = await _activate_hubspot(manager, sessions, "live")
    previous = await _activate_simulated_linkedin(manager, sessions)
    backfill.linkedin_leads = 2
    refreshes: list[int] = []

    async def matched_elsewhere() -> None:
        """Another matcher finds a contact for every lead of the build, before the lock."""
        await _match_elsewhere(sessions, crm, await _lead_ids(sessions, backfill.calls[-1]))

    async def refresh_skipped(
        controller: SourceModeController, crm_dataset_id: int, *, now: datetime | None = None
    ) -> None:
        """The CRM sync cannot run: no HubSpot client may be opened, nothing is read."""
        refreshes.append(crm_dataset_id)

    # The real hydration pass runs, against a CRM sync that never reads an association.
    monkeypatch.setattr(
        sys.modules["ads_engine.sync.jobs"], "refresh_crm_dataset", refresh_skipped, raising=False
    )
    _after_first_settle(monkeypatch, matched_elsewhere)
    with capture_logs() as entries, anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    building = outcome.dataset_id
    assert building is not None
    lacking = (
        "matched contacts still lack association reads after 3 rounds: 2 matched leads in CRM "
        f"dataset {crm}"
    )
    assert (outcome.activated, outcome.reason) == (False, lacking)
    # Rounds one to three were each refused under the lock; rounds two and three each ran
    # the CRM sync of HubSpot's primary first.
    refused = _events(entries, "sync.build.unhydrated")
    assert [(entry["crm_dataset_id"], entry["lead_dataset_id"]) for entry in refused] == [
        (crm, building)
    ] * 3
    assert refreshes == [crm, crm]
    hydrations = _events(entries, "sync.build.crm_hydration")
    assert [
        (entry["crm_dataset_id"], entry["lead_dataset_id"], entry["unhydrated"], entry["status"])
        for entry in hydrations
    ] == [(crm, building, 2, "skipped")] * 2
    assert await _state(sessions, building) is None
    assert await _state(sessions, previous) == "active"
    assert await _state(sessions, crm) == "active"
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        previous,
    )
    assert connection.reason == f"live build failed: {lacking}"
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_unhydrated_matches_counts_only_the_given_linkedin_datasets_leads(
    sessions: Sessions, manager: DatasetManager
) -> None:
    crm = await _activate_hubspot(manager, sessions, "live")
    active, active_leads = await _activate_live_linkedin(manager, sessions, 2)
    building = await manager.begin("linkedin", "live", reason="recovered")
    building_leads = await _leads(sessions, building, 3)
    unstaged = await manager.begin("linkedin", "live", reason="another build")
    await _leads(sessions, unstaged, 1)
    async with sessions() as session, session.begin():
        await lead_match.stage_rows(session, crm, building, since=EPOCH, now=T0)
    # One active lead and two of the building dataset's are matched with no association read.
    await _match_elsewhere(sessions, crm, active_leads[:1] + building_leads[:2])

    assert await manager.unhydrated_matches(crm) == 3
    assert await manager.unhydrated_matches(crm, lead_dataset_id=building) == 2
    assert await manager.unhydrated_matches(crm, lead_dataset_id=active) == 1
    assert await manager.unhydrated_matches(crm, lead_dataset_id=unstaged) == 0
    async with sessions() as session:
        assert await manager.unhydrated_matches(crm, lead_dataset_id=building, session=session) == 2
    await _hydrate(sessions, crm)
    assert await manager.unhydrated_matches(crm, lead_dataset_id=building) == 0
    assert await manager.unhydrated_matches(crm) == 0


# ---------------------------------------------------------------------------------------
# BE5-F08: the durable access generation
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_a_denial_by_another_controller_voids_a_live_build_after_its_backfill(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    live = await _activate_hubspot(manager, sessions, "live")
    other = _controller(settings, sessions)
    seen: list[tuple[str, mode.BuildOutcome | None]] = []

    async def other_controller_is_denied() -> None:
        """Controller B's job is denied on the live data and B activates simulated data."""
        disposition = await other.on_error("hubspot", _denial())
        seen.append((disposition, await other.ensure_dataset("hubspot")))

    backfill.after[1] = other_controller_is_denied
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    [(disposition, fallback)] = seen
    assert disposition == "switched_to_simulated"
    assert fallback is not None
    assert (fallback.activated, fallback.mode, fallback.retired) == (True, "simulated", (live,))
    simulated = fallback.dataset_id
    assert simulated is not None
    building = outcome.dataset_id
    assert building is not None
    # A's earlier live build neither activates nor retires B's fallback dataset.
    assert (outcome.activated, outcome.mode, outcome.reason) == (False, "live", _denial().reason)
    assert backfill.calls == [building, simulated]
    assert await _state(sessions, building) is None
    assert await _state(sessions, simulated) == "active"
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        simulated,
    )
    assert connection.access_generation == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_a_stale_generation_is_refused_under_the_activation_lock(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = await _activate_hubspot(manager, sessions, "live")
    other = _controller(settings, sessions)

    async def other_controller_is_denied() -> None:
        """A denial lands after the build's post-backfill check, before its lock."""
        assert await other.on_error("hubspot", _denial()) == "switched_to_simulated"

    _after_first_settle(monkeypatch, other_controller_is_denied)
    with capture_logs() as entries, anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "hubspot", "live", reason="recovered"
        )

    [voided] = _events(entries, "sync.build.voided")
    building = voided["dataset_id"]
    assert (voided["bound_generation"], voided["access_generation"]) == (0, 1)
    assert await _state(sessions, building) is None
    # The build is void, not failed: no fallback of its own, and the simulated dataset the
    # denial calls for is built and activated with the denial marker kept.
    assert (outcome.activated, outcome.mode, outcome.retired) == (True, "simulated", (live,))
    assert backfill.calls == [building, outcome.dataset_id]
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.reason) == (
        "simulated",
        "denied",
        _denial().reason,
    )
    assert connection.active_dataset_id == outcome.dataset_id
    assert connection.access_generation == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_recovery_is_void_when_a_fallback_entry_lands_during_its_probe(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _activate_hubspot(manager, sessions, "live")
    other = _controller(settings, sessions)
    assert await other.on_error("hubspot", _denial()) == "switched_to_simulated"
    fallback = await other.ensure_dataset("hubspot")
    assert fallback is not None
    assert fallback.activated

    async def probe_while_other_fails(self: SourceModeController, source: str) -> ProbeReport:
        """Pass the probe while controller B's own live build fails into fallback."""
        await other._enter_fallback("hubspot", "live build failed: HTTP 503")
        return ProbeReport.of("hubspot", (ProbeCheck("crm", True),))

    monkeypatch.setattr(SourceModeController, "probe", probe_while_other_fails)
    controller = _controller(settings, sessions)
    assert await controller.in_fallback("hubspot")
    with anyio.fail_after(60):
        outcome = await controller.recover("hubspot")

    assert outcome is not None
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode) == (False, "live")
    assert backfill.calls == [fallback.dataset_id, building]
    assert await _state(sessions, building) is None
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status) == ("simulated", "denied")
    assert connection.active_dataset_id == fallback.dataset_id
    assert connection.access_generation == 2


@pytest.mark.anyio
@pytest.mark.db
async def test_a_denial_during_a_first_activation_waits_for_it_then_takes_effect(
    settings: Settings,
    sessions: Sessions,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HubSpot has no source_connection row yet: this is its first activation.
    other = _controller(settings, sessions)
    original = DatasetManager.activate
    denials: list[str] = []
    inside: list[tuple[bool, int]] = []

    async def denied() -> None:
        """Controller B's HubSpot job is denied on live data while A's activation runs."""
        denials.append(await other.on_error("hubspot", _denial()))

    with capture_logs() as entries, anyio.fail_after(60):
        async with anyio.create_task_group() as group:

            async def activate(self: DatasetManager, dataset_id: int, **kwargs: Any) -> Any:
                """Start B's denial as a concurrent task and let it reach the row first."""
                if not inside:
                    group.start_soon(denied)
                    # The denial's wait is a server-side lock wait, so it is polled for.
                    waiting = 0
                    with anyio.fail_after(30):
                        while not denials and not waiting:
                            await anyio.sleep(0.02)
                            waiting = await _lock_waiters(sessions)
                    inside.append((bool(denials), waiting))
                return await original(self, dataset_id, **kwargs)

            monkeypatch.setattr(DatasetManager, "activate", activate)
            outcome = await _controller(settings, sessions).build_dataset(
                "hubspot", "live", reason="initial"
            )

    # The denial could not interleave: it waited on the row A's activation held, and was
    # recorded only once that activation had committed.
    assert inside == [(False, 1)]
    assert denials == ["switched_to_simulated"]
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.mode, outcome.retired) == (True, "live", ())
    changes = _events(entries, "sync.mode.change")
    first = [entry for entry in changes if entry["to_mode"] == "live"]
    assert [(entry["from_mode"], entry["dataset_id"]) for entry in first] == [(None, building)]
    denial = [entry for entry in changes if entry["to_mode"] == "simulated"]
    assert [(entry["from_mode"], entry["status"]) for entry in denial] == [("live", "denied")]
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.access_generation) == (
        "simulated",
        "denied",
        1,
    )
    assert connection.active_dataset_id == building
    # Only the build's own row was created; the counterpart's is left to LinkedIn.
    async with sessions() as session:
        assert (
            await session.scalar(
                select(SourceConnection.source).where(SourceConnection.source == "linkedin")
            )
            is None
        )
    # The denial calls for simulated data, which retires the live dataset of generation 0.
    with anyio.fail_after(60):
        fallback = await other.ensure_dataset("hubspot")
    assert fallback is not None
    assert (fallback.activated, fallback.mode, fallback.retired) == (True, "simulated", (building,))
    assert await _state(sessions, building) == "retired"
    connection = await _connection(sessions, "hubspot")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        fallback.dataset_id,
    )
    assert connection.access_generation == 1


# ---------------------------------------------------------------------------------------
# BE5-F09: a live LinkedIn build needs an effective CRM dataset for its leads
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_leads_without_a_hubspot_primary_hold_the_build(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    previous = await _activate_simulated_linkedin(manager, sessions)
    backfill.linkedin_leads = 2
    with capture_logs() as entries, anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    no_crm = (
        "not ready: no effective CRM dataset: HubSpot has no active primary dataset to match "
        "2 leads against"
    )
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.activated, outcome.reason) == (False, no_crm)
    assert [entry["dataset_id"] for entry in _events(entries, "sync.build.no_crm")] == [building]
    assert await _state(sessions, building) is None
    assert await _state(sessions, previous) == "active"
    # Automatic fallback: the 30-minute recovery probe retries once HubSpot is ready.
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "denied",
        previous,
    )
    assert connection.reason == f"live build failed: {no_crm}"
    assert matching_passes == []


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_build_without_leads_activates_without_a_hubspot_primary(
    settings: Settings,
    sessions: Sessions,
    manager: DatasetManager,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    previous = await _activate_simulated_linkedin(manager, sessions)
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "live", reason="recovered"
        )

    assert (outcome.activated, outcome.mode) == (True, "live")
    assert previous in outcome.retired
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "live",
        "ok",
        outcome.dataset_id,
    )
    assert matching_passes == []


# ---------------------------------------------------------------------------------------
# BD2-F02: a simulated LinkedIn build stages every lead in its companion
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_simulated_linkedin_build_stages_every_backfilled_lead_in_its_companion(
    settings: Settings,
    sessions: Sessions,
    backfill: Backfill,
    matching_passes: list[mode._MatchPass],
) -> None:
    # One lead inside the 90-day backfill range and one submitted before it, neither with a
    # row in the companion after the backfill.
    backfill.simulated_leads = [timedelta(days=1), timedelta(days=120)]
    with anyio.fail_after(60):
        outcome = await _controller(settings, sessions).build_dataset(
            "linkedin", "simulated", reason="credentials not configured"
        )

    building = outcome.dataset_id
    companion = outcome.companion_id
    assert building is not None
    assert companion is not None
    assert (outcome.activated, outcome.mode, outcome.retired) == (True, "simulated", ())
    assert await _state(sessions, building) == "active"
    assert await _state(sessions, companion) == "active"
    leads = await _lead_ids(sessions, building)
    assert len(leads) == 2
    # The settle staged both leads in the companion, the old one included; the companion's
    # CRM backfill then reran and attempted them before the pair activated.
    assert await _matches(sessions, companion, leads) == dict.fromkeys(leads, "no_match")
    assert backfill.calls == [building, building]
    assert await _freshness(sessions, companion) == dict.fromkeys(HUBSPOT_NAMES, ("complete", None))
    assert matching_passes == []
    connection = await _connection(sessions, "linkedin")
    assert (connection.mode, connection.status, connection.active_dataset_id) == (
        "simulated",
        "ok",
        building,
    )
