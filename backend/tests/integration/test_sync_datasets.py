"""Tests for ``ads_engine.sync.datasets``: the dataset lifecycle of AAP 0.3.3.

Covered: building datasets for every source and mode; the companion CRM dataset of a
simulated LinkedIn dataset; the freshness each dataset must record; the staged-matching
readiness test (no ``pending`` ``lead_match`` row, no ``unknown`` or reason-less
``incomplete`` freshness row); atomic activation that retires the previous active primary
with its companions and points ``source_connection`` at the new dataset; discarding a
failed build; the cascade purge of retired datasets after the retention period; the
effective CRM dataset; the CRM bindings stored per HubSpot dataset; and the import-time
vocabulary guard.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test starts from empty dataset tables in
that schema. An unreachable database fails the database tests; it never skips them.
"""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import ColumnElement, create_engine, func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding, ResolvedBinding
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.crm import CrmContact, Lead, LeadMatch
from ads_engine.db.models.ops import DatasetFreshness, SourceConnection, SourceDataset
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.sync import datasets
from ads_engine.sync.datasets import (
    RETENTION,
    Activation,
    DatasetManager,
    DatasetNotFoundError,
    DatasetStateError,
    Readiness,
)
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The clock's starting instant; tests advance it explicitly."""

MISSING_ID = 987_654_321
"""A ``source_dataset`` id no test creates."""

GOOGLE_NAMES = ("google.entities", "google.metrics_daily", "google.search_terms")
LINKEDIN_NAMES = (
    "linkedin.entities",
    "linkedin.analytics_daily",
    "linkedin.segments",
    "linkedin.leads",
)
HUBSPOT_NAMES = ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search")


class Clock:
    """A settable, timezone-aware clock for :class:`DatasetManager`."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        """Move the clock forward by ``delta`` and return the new instant."""
        self.now += delta
        return self.now


# ---------------------------------------------------------------------------------------
# Pure: constants, value objects and the vocabulary guard
# ---------------------------------------------------------------------------------------


def test_retention_is_thirty_days() -> None:
    assert timedelta(days=30) == RETENTION


def test_value_objects_are_frozen() -> None:
    readiness = Readiness(
        dataset_id=1,
        ready=True,
        pending_matches=0,
        missing_freshness=(),
        unknown_freshness=(),
        incomplete_without_reason=(),
    )
    assert readiness.companion is None
    assert readiness.reasons == ()
    with pytest.raises(FrozenInstanceError):
        readiness.ready = False  # type: ignore[misc]
    activation = Activation(source="google", activated=(1,), retired=(), mode="live")
    with pytest.raises(FrozenInstanceError):
        activation.mode = "simulated"  # type: ignore[misc]


def test_errors_carry_their_dataset_and_readiness() -> None:
    readiness = Readiness(
        dataset_id=4,
        ready=False,
        pending_matches=2,
        missing_freshness=(),
        unknown_freshness=(),
        incomplete_without_reason=(),
        reasons=("2 lead matches not yet attempted in CRM dataset 4",),
    )
    error = DatasetStateError("not ready", dataset_id=4, readiness=readiness)
    assert (str(error), error.dataset_id, error.readiness) == ("not ready", 4, readiness)
    bare = DatasetStateError("illegal")
    assert (bare.dataset_id, bare.readiness) == (None, None)
    missing = DatasetNotFoundError(7)
    assert isinstance(missing, DatasetStateError)
    assert isinstance(missing, LookupError)
    assert str(missing) == "source_dataset 7 does not exist"
    assert (missing.dataset_id, missing.readiness) == (7, None)


def test_vocabulary_matches_the_schema() -> None:
    datasets._check_vocabulary()


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("DATASET_STATES", ("building", "active"), "dataset states"),
        ("DATASET_ROLES", ("primary",), "dataset roles"),
        ("MODES", ("live",), "modes"),
        ("LEAD_MATCH_STATUSES", ("matched", "no_match"), "lead_match statuses"),
        ("SOURCE_DATASETS", {"hubspot": HUBSPOT_NAMES}, "not a HubSpot freshness dataset"),
    ],
)
def test_vocabulary_guard_fails_on_schema_drift(
    monkeypatch: pytest.MonkeyPatch, name: str, value: object, match: str
) -> None:
    monkeypatch.setattr(datasets, name, value)
    with pytest.raises(RuntimeError, match=match):
        datasets._check_vocabulary()


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
            "for the dataset lifecycle tests"
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
    schema = f"datasets_test_{uuid.uuid4().hex}"
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
    """Yield a session factory on empty dataset tables of the private schema.

    The engine is created and disposed inside this test's loop. Truncating
    ``source_dataset`` cascades to every dataset-scoped table, ``lead_match`` and
    ``source_connection``; ``analysis_request`` is emptied with them, so each test sees
    only the datasets it builds.
    """
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
def clock() -> Clock:
    """Return a clock standing at :data:`T0`."""
    return Clock(T0)


@pytest.fixture
def manager(sessions: Sessions, clock: Clock) -> DatasetManager:
    """Return a manager on the private schema's session factory and the test clock."""
    return DatasetManager(sessions, clock=clock)


# ---------------------------------------------------------------------------------------
# Scenario builders and readers
# ---------------------------------------------------------------------------------------


async def _row(sessions: Sessions, dataset_id: int) -> SourceDataset | None:
    """Return a dataset row as committed now, or ``None`` when it does not exist."""
    async with sessions() as session:
        return await session.get(SourceDataset, dataset_id)


async def _state(sessions: Sessions, dataset_id: int) -> str | None:
    """Return a dataset's state, or ``None`` when the row does not exist."""
    row = await _row(sessions, dataset_id)
    return None if row is None else row.state


async def _record(
    sessions: Sessions,
    dataset_id: int,
    names: Sequence[str],
    *,
    at: datetime = T0,
    complete: bool = True,
    reason: str | None = None,
    failed: bool = False,
) -> None:
    """Record one freshness outcome for each name: a success, or a failure."""
    async with sessions() as session, session.begin():
        for name in names:
            if failed:
                outcome = FreshnessOutcome.failure(at, reason="HTTP 503 after 3 attempts")
            else:
                outcome = FreshnessOutcome.success(
                    at, covered_from=None, covered_through=None, complete=complete, reason=reason
                )
            await record_freshness(session, dataset_id, name, outcome)


async def _make_fresh(manager: DatasetManager, sessions: Sessions, dataset_id: int) -> None:
    """Record every freshness row the dataset requires as complete."""
    await _record(sessions, dataset_id, await manager.required_freshness(dataset_id))


async def _activated(
    manager: DatasetManager,
    sessions: Sessions,
    source: str,
    mode: str,
    *,
    reason: str = "credentials not configured",
) -> tuple[int, int | None]:
    """Begin, freshen and activate a primary; a simulated LinkedIn one gets its companion.

    Returns:
        The primary's id and its companion's id (``None`` without one).
    """
    dataset_id = await manager.begin(source, mode, reason=reason)
    companion_id: int | None = None
    if source == "linkedin" and mode == "simulated":
        companion_id = await manager.begin("hubspot", "simulated", "companion", dataset_id)
        await _make_fresh(manager, sessions, companion_id)
    await _make_fresh(manager, sessions, dataset_id)
    await manager.activate(dataset_id)
    return dataset_id, companion_id


async def _leads(sessions: Sessions, dataset_id: int, count: int) -> list[int]:
    """Commit ``count`` leads of one LinkedIn campaign in a dataset; return their ids."""
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
                submitted_at=T0 - timedelta(days=index + 1),
                campaign_id=campaign.id,
                creative_id=None,
                lead_form_urn="urn:li:leadGenForm:7",
                lead_form_version=1,
                email_sha256=hashlib.sha256(
                    f"lead{dataset_id}.{index}@sim.invalid".encode()
                ).hexdigest(),
            )
            for index in range(count)
        ]
        session.add_all(leads)
        await session.flush()
        return [lead.id for lead in leads]


async def _contact(sessions: Sessions, crm_dataset_id: int, hs_id: str) -> int:
    """Commit one CRM contact in a HubSpot dataset; return its id."""
    async with sessions() as session, session.begin():
        contact = CrmContact(source="hubspot", dataset_id=crm_dataset_id, hs_id=hs_id)
        session.add(contact)
        await session.flush()
        return contact.id


async def _stage(
    sessions: Sessions, lead_ids: Sequence[int], crm_dataset_id: int, status: str = "pending"
) -> None:
    """Commit one ``lead_match`` row per lead in a CRM dataset, never matched."""
    attempted = status != "pending"
    async with sessions() as session, session.begin():
        session.add_all(
            LeadMatch(
                lead_id=lead_id,
                crm_dataset_id=crm_dataset_id,
                status=status,
                attempts=1 if attempted else 0,
                last_checked_at=T0 if attempted else None,
            )
            for lead_id in lead_ids
        )


async def _match(sessions: Sessions, lead_id: int, crm_dataset_id: int, contact_id: int) -> None:
    """Commit a ``matched`` row linking a lead to a contact of the CRM dataset."""
    async with sessions() as session, session.begin():
        session.add(
            LeadMatch(
                lead_id=lead_id,
                crm_dataset_id=crm_dataset_id,
                status="matched",
                attempts=1,
                last_checked_at=T0,
                contact_id=contact_id,
                contact_read_status="ok",
            )
        )


async def _attempt(
    sessions: Sessions, crm_dataset_id: int, lead_ids: Sequence[int], status: str = "no_match"
) -> None:
    """Mark the given leads' rows in a CRM dataset as attempted with ``status``."""
    async with sessions() as session, session.begin():
        await session.execute(
            update(LeadMatch)
            .where(LeadMatch.crm_dataset_id == crm_dataset_id, LeadMatch.lead_id.in_(lead_ids))
            .values(status=status, attempts=1, last_checked_at=T0)
        )


async def _count(sessions: Sessions, model: type[Base], *criteria: ColumnElement[bool]) -> int:
    """Count the committed rows of ``model`` matching ``criteria``."""
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(model).where(*criteria))
        return int(count or 0)


async def _connection(sessions: Sessions, source: str) -> SourceConnection | None:
    """Return a source's ``source_connection`` row, if any."""
    async with sessions() as session:
        return await session.scalar(
            select(SourceConnection).where(SourceConnection.source == source)
        )


async def _requests(sessions: Sessions) -> list[str]:
    """Return the reasons of every queued analysis request, oldest first."""
    async with sessions() as session:
        return list(
            await session.scalars(select(AnalysisRequest.reason).order_by(AnalysisRequest.id))
        )


def _bindings(*, google: bool) -> CrmBindings:
    """Return verified CRM bindings, with or without the Google-to-CRM link."""
    link = (
        GoogleCrmBinding(
            available=True,
            campaign_property="google_campaign_id",
            touch_date_property="google_touch_date",
        )
        if google
        else GoogleCrmBinding(
            available=False, reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"
        )
    )
    return CrmBindings(
        mql=ResolvedBinding(available=True, property="date_first_meeting_was_booked"),
        ql=ResolvedBinding(
            available=True,
            property="hs_v2_date_entered_1406943076",
            stage_id="1406943076",
            label="Qualified Lead",
        ),
        google=link,
        verified_at=T0,
    )


# ---------------------------------------------------------------------------------------
# Construction and the process-wide session factory
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_manager_without_a_factory_uses_the_process_wide_one(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(datasets, "get_sessionmaker", lambda: sessions)
    manager = DatasetManager()
    assert repr(manager) == "DatasetManager()"
    before = datetime.now(UTC)
    dataset_id = await manager.begin("google", "simulated", reason="credentials not configured")
    row = await manager.get(dataset_id)
    assert row.state == "building"
    assert row.created_at >= before


@pytest.mark.anyio
@pytest.mark.db
async def test_naive_clock_is_refused(sessions: Sessions) -> None:
    manager = DatasetManager(sessions, clock=lambda: T0.replace(tzinfo=None))
    with pytest.raises(ValueError, match="timezone-aware"):
        await manager.begin("google", "live")
    with pytest.raises(ValueError, match="timezone-aware"):
        await manager.purge_retired()
    assert await _count(sessions, SourceDataset) == 0


# ---------------------------------------------------------------------------------------
# begin
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("source", ["google", "linkedin", "hubspot"])
@pytest.mark.parametrize("mode", ["live", "simulated"])
async def test_begin_inserts_a_building_primary(
    manager: DatasetManager, sessions: Sessions, clock: Clock, source: str, mode: str
) -> None:
    dataset_id = await manager.begin(source, mode, reason="recovered")
    row = await manager.get(dataset_id)
    assert (row.id, row.source, row.mode, row.state, row.role) == (
        dataset_id,
        source,
        mode,
        "building",
        "primary",
    )
    assert row.parent_dataset_id is None
    assert row.reason == "recovered"
    assert row.created_at == clock.now
    assert (row.activated_at, row.retired_at, row.crm_bindings) == (None, None, None)
    # A building dataset reads nothing yet: the source has no active primary.
    assert await _connection(sessions, source) is None
    assert await _requests(sessions) == []


@pytest.mark.anyio
@pytest.mark.db
async def test_begin_without_reason_records_none(manager: DatasetManager) -> None:
    dataset_id = await manager.begin("google", "live")
    assert (await manager.get(dataset_id)).reason is None


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("args", "kwargs", "match"),
    [
        (("bing", "live"), {}, "unknown source 'bing'"),
        (("google", "mock"), {}, "unknown mode 'mock'"),
        (("google", "live", "secondary"), {}, "unknown dataset role 'secondary'"),
        (("google", "live"), {"reason": ""}, "must not be blank"),
        (("google", "live"), {"reason": "   "}, "must not be blank"),
        (("google", "live", "primary", 1), {}, "a primary dataset has no parent"),
        (("hubspot", "live", "companion", 1), {}, "always simulated HubSpot, not live hubspot"),
        (
            ("linkedin", "simulated", "companion", 1),
            {},
            "always simulated HubSpot, not simulated linkedin",
        ),
        (("hubspot", "simulated", "companion"), {}, "needs its LinkedIn parent"),
    ],
)
async def test_begin_rejects_invalid_arguments(
    manager: DatasetManager,
    sessions: Sessions,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        await manager.begin(*args, **kwargs)  # type: ignore[arg-type]
    assert await _count(sessions, SourceDataset) == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_begin_joins_the_callers_transaction(
    manager: DatasetManager, sessions: Sessions
) -> None:
    async with sessions() as session:
        async with session.begin():
            kept = await manager.begin("linkedin", "simulated", session=session)
            await manager.begin("hubspot", "simulated", "companion", kept, session=session)
        transaction = await session.begin()
        rolled_back = await manager.begin("google", "live", session=session)
        assert (await manager.get(rolled_back, session=session)).state == "building"
        await transaction.rollback()
    assert await _state(sessions, kept) == "building"
    assert await _row(sessions, rolled_back) is None
    assert await _count(sessions, SourceDataset) == 2


# ---------------------------------------------------------------------------------------
# Companions
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_simulated_linkedin_dataset_takes_one_companion(
    manager: DatasetManager, clock: Clock
) -> None:
    parent = await manager.begin("linkedin", "simulated", reason="credentials not configured")
    assert await manager.companion_of(parent) is None
    assert await manager.effective_crm_dataset(parent) is None

    companion = await manager.begin("hubspot", "simulated", role="companion", parent=parent)
    row = await manager.get(companion)
    assert (row.source, row.mode, row.state, row.role, row.parent_dataset_id) == (
        "hubspot",
        "simulated",
        "building",
        "companion",
        parent,
    )
    assert row.reason == f"companion CRM of simulated LinkedIn dataset {parent}"
    assert row.created_at == clock.now
    assert await manager.companion_of(parent) == companion
    assert await manager.effective_crm_dataset(parent) == companion

    with pytest.raises(DatasetStateError, match=f"already has companion {companion}") as excinfo:
        await manager.begin("hubspot", "simulated", "companion", parent)
    assert excinfo.value.dataset_id == parent

    other = await manager.begin("linkedin", "simulated")
    explicit = await manager.begin(
        "hubspot", "simulated", "companion", other, reason="seeded CRM for LinkedIn"
    )
    assert (await manager.get(explicit)).reason == "seeded CRM for LinkedIn"


@pytest.mark.anyio
@pytest.mark.db
async def test_companion_parent_must_be_a_building_simulated_linkedin_primary(
    manager: DatasetManager, sessions: Sessions
) -> None:
    google = await manager.begin("google", "simulated")
    live = await manager.begin("linkedin", "live")
    parent = await manager.begin("linkedin", "simulated")
    companion = await manager.begin("hubspot", "simulated", "companion", parent)
    active, _ = await _activated(manager, sessions, "linkedin", "simulated")
    before = await _count(sessions, SourceDataset)

    with pytest.raises(DatasetNotFoundError) as missing:
        await manager.begin("hubspot", "simulated", "companion", MISSING_ID)
    assert missing.value.dataset_id == MISSING_ID
    with pytest.raises(ValueError, match="is a google primary dataset, not a LinkedIn primary"):
        await manager.begin("hubspot", "simulated", "companion", google)
    with pytest.raises(ValueError, match="is a hubspot companion dataset, not a LinkedIn primary"):
        await manager.begin("hubspot", "simulated", "companion", companion)
    with pytest.raises(ValueError, match=f"source_dataset {live} is live"):
        await manager.begin("hubspot", "simulated", "companion", live)
    with pytest.raises(DatasetStateError, match="is active; a companion is begun only") as state:
        await manager.begin("hubspot", "simulated", "companion", active)
    assert state.value.dataset_id == active
    assert await _count(sessions, SourceDataset) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_companion_of_reads_only_linkedin_primaries(manager: DatasetManager) -> None:
    live = await manager.begin("linkedin", "live")
    google = await manager.begin("google", "live")
    parent = await manager.begin("linkedin", "simulated")
    companion = await manager.begin("hubspot", "simulated", "companion", parent)
    assert await manager.companion_of(live) is None
    with pytest.raises(ValueError, match="not a LinkedIn primary"):
        await manager.companion_of(google)
    with pytest.raises(ValueError, match="not a LinkedIn primary"):
        await manager.companion_of(companion)
    with pytest.raises(DatasetNotFoundError):
        await manager.companion_of(MISSING_ID)


# ---------------------------------------------------------------------------------------
# Effective CRM dataset
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_effective_crm_dataset_follows_the_linkedin_mode(
    manager: DatasetManager, sessions: Sessions
) -> None:
    live = await manager.begin("linkedin", "live")
    # A live LinkedIn dataset reads HubSpot's active primary, and there is none yet.
    assert await manager.effective_crm_dataset(live) is None
    hubspot, _ = await _activated(manager, sessions, "hubspot", "live", reason="recovered")
    assert await manager.effective_crm_dataset(live) == hubspot

    simulated, companion = await _activated(manager, sessions, "linkedin", "simulated")
    # A simulated one reads its companion, never the live HubSpot primary.
    assert companion is not None
    assert await manager.effective_crm_dataset(simulated) == companion
    async with sessions() as session:
        assert await manager.effective_crm_dataset(simulated, session=session) == companion

    google = await manager.begin("google", "live")
    with pytest.raises(ValueError, match="not 'linkedin'"):
        await manager.effective_crm_dataset(google)
    with pytest.raises(ValueError, match="does not exist"):
        await manager.effective_crm_dataset(MISSING_ID)


# ---------------------------------------------------------------------------------------
# Required freshness and CRM bindings
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_required_freshness_per_source_and_role(manager: DatasetManager) -> None:
    google = await manager.begin("google", "live")
    linkedin = await manager.begin("linkedin", "simulated")
    companion = await manager.begin("hubspot", "simulated", "companion", linkedin)
    hubspot = await manager.begin("hubspot", "live")
    assert await manager.required_freshness(google) == GOOGLE_NAMES
    assert await manager.required_freshness(linkedin) == LINKEDIN_NAMES
    assert await manager.required_freshness(hubspot) == HUBSPOT_NAMES
    assert await manager.required_freshness(companion) == HUBSPOT_NAMES

    # Google-touch discovery runs only for a HubSpot primary whose Google link is verified.
    await manager.store_crm_bindings(hubspot, _bindings(google=False))
    assert await manager.required_freshness(hubspot) == HUBSPOT_NAMES
    await manager.store_crm_bindings(hubspot, _bindings(google=True))
    assert await manager.required_freshness(hubspot) == (
        *HUBSPOT_NAMES,
        "hubspot.google_contacts",
    )
    await manager.store_crm_bindings(companion, _bindings(google=True))
    assert await manager.required_freshness(companion) == HUBSPOT_NAMES

    with pytest.raises(DatasetNotFoundError):
        await manager.required_freshness(MISSING_ID)


@pytest.mark.anyio
@pytest.mark.db
async def test_crm_bindings_are_stored_per_hubspot_dataset(
    manager: DatasetManager, sessions: Sessions
) -> None:
    hubspot = await manager.begin("hubspot", "live")
    assert await manager.load_crm_bindings(hubspot) is None

    first = _bindings(google=False)
    await manager.store_crm_bindings(hubspot, first)
    assert await manager.load_crm_bindings(hubspot) == first
    newer = _bindings(google=True)
    await manager.store_crm_bindings(hubspot, newer)
    assert await manager.load_crm_bindings(hubspot) == newer
    assert (await manager.get(hubspot)).crm_bindings == newer.to_json()

    async with sessions() as session:
        async with session.begin():
            await manager.store_crm_bindings(hubspot, first, session=session)
            assert await manager.load_crm_bindings(hubspot, session=session) == first
        await session.begin()
        await manager.store_crm_bindings(hubspot, newer, session=session)
        await session.rollback()
    assert await manager.load_crm_bindings(hubspot) == first

    google = await manager.begin("google", "live")
    with pytest.raises(ValueError, match="CRM bindings belong to HubSpot datasets"):
        await manager.store_crm_bindings(google, first)
    with pytest.raises(ValueError, match="CRM bindings belong to HubSpot datasets"):
        await manager.load_crm_bindings(google)
    with pytest.raises(DatasetNotWritable) as absent:
        await manager.store_crm_bindings(MISSING_ID, first)
    assert absent.value.state is None
    with pytest.raises(DatasetNotFoundError):
        await manager.load_crm_bindings(MISSING_ID)

    # Retired datasets are read-only: their bindings can still be read, never replaced.
    old, _ = await _activated(manager, sessions, "hubspot", "simulated")
    await _activated(manager, sessions, "hubspot", "live", reason="recovered")
    assert await _state(sessions, old) == "retired"
    with pytest.raises(DatasetNotWritable) as retired:
        await manager.store_crm_bindings(old, first)
    assert retired.value.state == "retired"
    assert await manager.load_crm_bindings(old) is None


# ---------------------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_google_readiness_reads_freshness_only(
    manager: DatasetManager, sessions: Sessions
) -> None:
    google = await manager.begin("google", "live", reason="recovered")
    readiness = await manager.readiness(google)
    assert readiness == Readiness(
        dataset_id=google,
        ready=False,
        pending_matches=0,
        missing_freshness=GOOGLE_NAMES,
        unknown_freshness=(),
        incomplete_without_reason=(),
        companion=None,
        reasons=(f"freshness not yet recorded: {', '.join(GOOGLE_NAMES)}",),
    )

    await _record(sessions, google, ["google.entities"], failed=True)
    await _record(sessions, google, ["google.metrics_daily"])
    async with sessions() as session, session.begin():
        session.add(
            DatasetFreshness(
                source="google",
                dataset_id=google,
                dataset="google.search_terms",
                completeness="incomplete",
                reason="   ",
            )
        )
    readiness = await manager.readiness(google)
    assert not readiness.ready
    assert readiness.missing_freshness == ()
    assert readiness.unknown_freshness == ("google.entities",)
    assert readiness.incomplete_without_reason == ("google.search_terms",)
    assert readiness.reasons == (
        "freshness unknown: google.entities",
        "incomplete with no recorded reason: google.search_terms",
    )

    # incomplete with a recorded reason is accepted; unknown never is.
    await _record(sessions, google, ["google.entities"], at=T0 + timedelta(hours=1))
    async with sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(
                DatasetFreshness.dataset_id == google,
                DatasetFreshness.dataset == "google.search_terms",
            )
            .values(reason="result limit reached at finest partition")
        )
    readiness = await manager.readiness(google)
    assert readiness.ready
    assert readiness.reasons == ()
    assert (readiness.pending_matches, readiness.companion) == (0, None)

    async with sessions() as session:
        assert (await manager.readiness(google, session=session)).ready
    with pytest.raises(DatasetNotFoundError):
        await manager.readiness(MISSING_ID)


@pytest.mark.anyio
@pytest.mark.db
async def test_hubspot_build_is_refused_until_staged_matches_and_freshness_are_attempted(
    manager: DatasetManager, sessions: Sessions
) -> None:
    linkedin, _ = await _activated(manager, sessions, "linkedin", "live")
    leads = await _leads(sessions, linkedin, 3)
    simulated, _ = await _activated(manager, sessions, "hubspot", "simulated")
    await _stage(sessions, leads, simulated, "no_match")
    requests_before = await _requests(sessions)

    building = await manager.begin("hubspot", "live", reason="recovered")
    await _stage(sessions, leads, building)
    await _record(sessions, building, ["hubspot.contacts"], failed=True)
    await _record(sessions, building, ["hubspot.associations", "hubspot.deal_search"])

    with pytest.raises(DatasetStateError, match="is not ready") as excinfo:
        await manager.activate(building)
    refused = excinfo.value.readiness
    assert excinfo.value.dataset_id == building
    assert refused is not None
    assert not refused.ready
    assert refused.pending_matches == 3
    assert refused.unknown_freshness == ("hubspot.contacts",)
    assert refused.reasons == (
        "freshness unknown: hubspot.contacts",
        f"3 lead matches not yet attempted in CRM dataset {building}",
    )
    # The refusal changed nothing: the simulated dataset stays active and in use.
    assert await _state(sessions, building) == "building"
    assert await _state(sessions, simulated) == "active"
    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert (connection.active_dataset_id, connection.mode) == (simulated, "simulated")
    assert await _requests(sessions) == requests_before

    await _attempt(sessions, building, leads[:2])
    readiness = await manager.readiness(building)
    assert readiness.pending_matches == 1
    assert f"1 lead match not yet attempted in CRM dataset {building}" in readiness.reasons

    await _record(sessions, building, ["hubspot.contacts"], at=T0 + timedelta(hours=1))
    await _attempt(sessions, building, leads[2:], "error")
    assert (await manager.readiness(building)).ready
    activation = await manager.activate(building)
    assert activation == Activation(
        source="hubspot", activated=(building,), retired=(simulated,), mode="live"
    )
    # Matching state moved with the datasets: nothing was reset to pending.
    assert await _count(sessions, LeadMatch, LeadMatch.status == "pending") == 0
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == simulated) == 3


@pytest.mark.anyio
@pytest.mark.db
async def test_simulated_linkedin_readiness_includes_its_companion(
    manager: DatasetManager, sessions: Sessions
) -> None:
    parent = await manager.begin("linkedin", "simulated", reason="credentials not configured")
    await _make_fresh(manager, sessions, parent)
    readiness = await manager.readiness(parent)
    assert not readiness.ready
    assert readiness.companion is None
    assert readiness.reasons == ("companion CRM dataset not begun",)
    with pytest.raises(DatasetStateError, match="companion CRM dataset not begun"):
        await manager.activate(parent)

    companion = await manager.begin("hubspot", "simulated", "companion", parent)
    leads = await _leads(sessions, parent, 2)
    await _stage(sessions, leads, companion)
    readiness = await manager.readiness(parent)
    assert not readiness.ready
    assert readiness.pending_matches == 2
    assert readiness.companion is not None
    assert readiness.companion.dataset_id == companion
    assert readiness.companion.missing_freshness == HUBSPOT_NAMES
    assert readiness.companion.pending_matches == 2
    companion_reasons = (
        f"freshness not yet recorded: {', '.join(HUBSPOT_NAMES)}",
        f"2 lead matches not yet attempted in CRM dataset {companion}",
    )
    assert readiness.companion.reasons == companion_reasons
    # The companion's pending rows are reported once, among its own reasons.
    assert readiness.reasons == (
        f"companion CRM dataset {companion} not ready: " + "; ".join(companion_reasons),
    )
    assert (await manager.readiness(companion)).reasons == companion_reasons

    await _make_fresh(manager, sessions, companion)
    await _attempt(sessions, companion, leads, "no_match")
    readiness = await manager.readiness(parent)
    assert readiness.ready
    assert readiness.pending_matches == 0
    assert readiness.companion is not None
    assert readiness.companion.ready


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_readiness_counts_its_leads_in_hubspots_active_primary(
    manager: DatasetManager, sessions: Sessions
) -> None:
    linkedin = await manager.begin("linkedin", "live", reason="recovered")
    await _make_fresh(manager, sessions, linkedin)
    leads = await _leads(sessions, linkedin, 2)
    # No HubSpot dataset is active, so the leads have no CRM dataset to be matched in: the
    # dataset is held, and its previous dataset stays active, until HubSpot has a primary.
    readiness = await manager.readiness(linkedin)
    assert not readiness.ready
    assert (readiness.pending_matches, readiness.unstaged_leads) == (0, 2)
    assert readiness.reasons == (
        "no effective CRM dataset: HubSpot has no active primary dataset to match 2 leads against",
    )
    with pytest.raises(DatasetStateError, match="no effective CRM dataset"):
        await manager.activate(linkedin)
    assert await _state(sessions, linkedin) == "building"

    hubspot, _ = await _activated(manager, sessions, "hubspot", "live")
    other = await manager.begin("hubspot", "live")
    await _stage(sessions, leads, hubspot)
    await _stage(sessions, leads, other)
    readiness = await manager.readiness(linkedin)
    assert not readiness.ready
    assert readiness.pending_matches == 2
    assert readiness.companion is None
    assert readiness.reasons == (
        f"2 lead matches of this dataset's leads not yet attempted in CRM dataset {hubspot}",
    )

    # Only HubSpot's active primary counts; the other building dataset's rows do not.
    await _attempt(sessions, hubspot, leads)
    assert (await manager.readiness(linkedin)).ready
    assert (await manager.readiness(other)).pending_matches == 2


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_readiness_detects_a_replaced_hubspot_primary(
    manager: DatasetManager, sessions: Sessions
) -> None:
    first, _ = await _activated(manager, sessions, "hubspot", "live")
    linkedin = await manager.begin("linkedin", "live", reason="recovered")
    await _make_fresh(manager, sessions, linkedin)
    leads = await _leads(sessions, linkedin, 2)
    await _stage(sessions, leads, first, "no_match")
    assert (await manager.readiness(linkedin)).ready

    # HubSpot activates a new primary while the LinkedIn build runs: the rows attempted in
    # the replaced one no longer count, and readiness says which leads still need staging.
    second, _ = await _activated(manager, sessions, "hubspot", "live")
    readiness = await manager.readiness(linkedin)
    assert not readiness.ready
    assert (readiness.pending_matches, readiness.unstaged_leads) == (0, 2)
    assert readiness.reasons == (f"2 leads of this dataset not yet staged in CRM dataset {second}",)

    await _stage(sessions, leads[:1], second)
    readiness = await manager.readiness(linkedin)
    assert (readiness.pending_matches, readiness.unstaged_leads) == (1, 1)
    assert readiness.reasons == (
        f"1 lead match of this dataset's leads not yet attempted in CRM dataset {second}",
        f"1 lead of this dataset not yet staged in CRM dataset {second}",
    )

    await _stage(sessions, leads[1:], second)
    await _attempt(sessions, second, leads)
    assert (await manager.readiness(linkedin)).ready


@pytest.mark.anyio
@pytest.mark.db
async def test_hubspot_readiness_ignores_pending_rows_of_a_retired_linkedin_dataset(
    manager: DatasetManager, sessions: Sessions
) -> None:
    old_linkedin, _ = await _activated(manager, sessions, "linkedin", "live")
    old_leads = await _leads(sessions, old_linkedin, 2)
    building = await manager.begin("hubspot", "live", reason="recovered")
    await _make_fresh(manager, sessions, building)
    await _stage(sessions, old_leads, building)
    assert (await manager.readiness(building)).pending_matches == 2

    # LinkedIn activates a new dataset: its predecessor's rows are never attempted again,
    # so they no longer hold the HubSpot build; rows of the active dataset's leads still do.
    new_linkedin, _ = await _activated(manager, sessions, "linkedin", "live")
    assert await _state(sessions, old_linkedin) == "retired"
    assert (await manager.readiness(building)).ready
    new_leads = await _leads(sessions, new_linkedin, 1)
    await _stage(sessions, new_leads, building)
    readiness = await manager.readiness(building)
    assert readiness.pending_matches == 1
    assert readiness.reasons == (f"1 lead match not yet attempted in CRM dataset {building}",)


@pytest.mark.anyio
@pytest.mark.db
async def test_live_linkedin_without_leads_needs_no_crm_dataset(
    manager: DatasetManager, sessions: Sessions
) -> None:
    linkedin = await manager.begin("linkedin", "live", reason="recovered")
    await _make_fresh(manager, sessions, linkedin)
    # No lead and no HubSpot primary: nothing is to be matched, so nothing holds it.
    readiness = await manager.readiness(linkedin)
    assert readiness.ready
    assert (readiness.pending_matches, readiness.unstaged_leads, readiness.reasons) == (0, 0, ())
    activation = await manager.activate(linkedin)
    assert activation == Activation(
        source="linkedin", activated=(linkedin,), retired=(), mode="live"
    )


# ---------------------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_first_activation_creates_the_source_connection(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    google = await manager.begin("google", "simulated", reason="credentials not configured")
    await _make_fresh(manager, sessions, google)
    at = clock.advance(timedelta(hours=2))
    activation = await manager.activate(google)
    assert activation == Activation(
        source="google", activated=(google,), retired=(), mode="simulated"
    )
    row = await manager.get(google)
    assert (row.state, row.activated_at, row.retired_at) == ("active", at, None)
    connection = await _connection(sessions, "google")
    assert connection is not None
    assert (
        connection.mode,
        connection.status,
        connection.reason,
        connection.active_dataset_id,
    ) == ("simulated", "ok", "credentials not configured", google)
    assert connection.calls_24h == 0
    assert await _requests(sessions) == ["dataset_activated:google"]


@pytest.mark.anyio
@pytest.mark.db
async def test_activation_retires_the_previous_primary_and_updates_the_connection(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    old, _ = await _activated(manager, sessions, "google", "simulated")
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceConnection)
            .where(SourceConnection.source == "google")
            .values(status="denied", reason="PERMISSION_DENIED on googleAds:search", calls_24h=7)
        )
    new = await manager.begin("google", "live", reason="probe passed")
    await _make_fresh(manager, sessions, new)
    at = clock.advance(timedelta(days=1))
    activation = await manager.activate(new, reason="recovered")
    assert activation == Activation(source="google", activated=(new,), retired=(old,), mode="live")

    old_row = await manager.get(old)
    new_row = await manager.get(new)
    assert (old_row.state, old_row.retired_at, old_row.activated_at) == ("retired", at, T0)
    assert (new_row.state, new_row.activated_at, new_row.retired_at) == ("active", at, None)
    connection = await _connection(sessions, "google")
    assert connection is not None
    assert (
        connection.mode,
        connection.status,
        connection.reason,
        connection.active_dataset_id,
    ) == ("live", "ok", "recovered", new)
    # The call budget is not the activation's to reset.
    assert connection.calls_24h == 7
    assert await _requests(sessions) == ["dataset_activated:google"] * 2
    async with sessions() as session:
        active = await session.scalars(
            select(SourceDataset.id).where(
                SourceDataset.source == "google", SourceDataset.state == "active"
            )
        )
        assert list(active) == [new]


@pytest.mark.anyio
@pytest.mark.db
async def test_companion_activates_and_retires_with_its_parent(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    hubspot, _ = await _activated(manager, sessions, "hubspot", "live")
    first = await manager.begin("linkedin", "simulated", reason="credentials not configured")
    first_companion = await manager.begin("hubspot", "simulated", "companion", first)
    await _make_fresh(manager, sessions, first)
    await _make_fresh(manager, sessions, first_companion)
    first_at = clock.advance(timedelta(hours=1))
    activation = await manager.activate(first)
    assert activation == Activation(
        source="linkedin", activated=(first, first_companion), retired=(), mode="simulated"
    )
    companion_row = await manager.get(first_companion)
    assert (companion_row.state, companion_row.activated_at) == ("active", first_at)

    second = await manager.begin("linkedin", "simulated")
    second_companion = await manager.begin("hubspot", "simulated", "companion", second)
    await _make_fresh(manager, sessions, second)
    await _make_fresh(manager, sessions, second_companion)
    second_at = clock.advance(timedelta(hours=1))
    activation = await manager.activate(second)
    assert activation.activated == (second, second_companion)
    assert activation.retired == (first, first_companion)
    for retired in (first, first_companion):
        row = await manager.get(retired)
        assert (row.state, row.retired_at) == ("retired", second_at)
    for active in (second, second_companion):
        assert await _state(sessions, active) == "active"

    live = await manager.begin("linkedin", "live", reason="recovered")
    await _make_fresh(manager, sessions, live)
    activation = await manager.activate(live)
    assert activation == Activation(
        source="linkedin", activated=(live,), retired=(second, second_companion), mode="live"
    )
    assert await manager.effective_crm_dataset(live) == hubspot
    # HubSpot's primary never moves with LinkedIn's companions.
    assert await _state(sessions, hubspot) == "active"
    connection = await _connection(sessions, "hubspot")
    assert connection is not None
    assert connection.active_dataset_id == hubspot
    linkedin = await _connection(sessions, "linkedin")
    assert linkedin is not None
    assert (linkedin.mode, linkedin.reason, linkedin.active_dataset_id) == (
        "live",
        "recovered",
        live,
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_activation_refuses_illegal_transitions(
    manager: DatasetManager, sessions: Sessions
) -> None:
    active, companion = await _activated(manager, sessions, "linkedin", "simulated")
    assert companion is not None
    old, _ = await _activated(manager, sessions, "google", "simulated")
    await _activated(manager, sessions, "google", "live")
    requests_before = await _requests(sessions)

    with pytest.raises(DatasetStateError, match="is a companion; it activates only") as error:
        await manager.activate(companion)
    assert error.value.dataset_id == companion
    assert error.value.readiness is None
    with pytest.raises(DatasetStateError, match="is active; only a building dataset"):
        await manager.activate(active)
    with pytest.raises(DatasetStateError, match="is retired; only a building dataset"):
        await manager.activate(old)
    with pytest.raises(DatasetNotFoundError) as missing:
        await manager.activate(MISSING_ID)
    assert missing.value.dataset_id == MISSING_ID
    with pytest.raises(ValueError, match="must not be blank"):
        await manager.activate(active, reason=" ")

    building = await manager.begin("linkedin", "simulated")
    stray = await manager.begin("hubspot", "simulated", "companion", building)
    await _make_fresh(manager, sessions, building)
    await _make_fresh(manager, sessions, stray)
    async with sessions() as session, session.begin():
        await session.execute(
            update(SourceDataset).where(SourceDataset.id == stray).values(state="retired")
        )
    with pytest.raises(DatasetStateError, match=f"companion {stray} .* is retired"):
        await manager.activate(building)
    assert await _state(sessions, building) == "building"
    assert await _state(sessions, active) == "active"
    assert await _requests(sessions) == requests_before


@pytest.mark.anyio
@pytest.mark.db
async def test_activation_joins_the_callers_transaction(
    manager: DatasetManager, sessions: Sessions
) -> None:
    old, _ = await _activated(manager, sessions, "google", "simulated")
    new = await manager.begin("google", "live")
    await _make_fresh(manager, sessions, new)
    async with sessions() as session:
        await session.begin()
        activation = await manager.activate(new, reason="recovered", session=session)
        assert activation.retired == (old,)
        await session.rollback()
    assert await _state(sessions, old) == "active"
    assert await _state(sessions, new) == "building"
    connection = await _connection(sessions, "google")
    assert connection is not None
    assert connection.active_dataset_id == old

    async with sessions() as session, session.begin():
        await manager.activate(new, session=session)
    assert await _state(sessions, new) == "active"
    assert await _state(sessions, old) == "retired"


# ---------------------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_discard_deletes_a_failed_build_with_its_companion_and_rows(
    manager: DatasetManager, sessions: Sessions
) -> None:
    active, active_companion = await _activated(manager, sessions, "linkedin", "simulated")
    assert active_companion is not None
    active_leads = await _leads(sessions, active, 1)
    await _stage(sessions, active_leads, active_companion, "no_match")

    building = await manager.begin("linkedin", "simulated")
    companion = await manager.begin("hubspot", "simulated", "companion", building)
    leads = await _leads(sessions, building, 2)
    contact = await _contact(sessions, companion, "9000000001")
    await _match(sessions, leads[0], companion, contact)
    await _stage(sessions, leads[1:], companion)
    await _make_fresh(manager, sessions, building)
    await _make_fresh(manager, sessions, companion)

    await manager.discard(building)
    assert await _row(sessions, building) is None
    assert await _row(sessions, companion) is None
    assert await _count(sessions, Lead, Lead.dataset_id == building) == 0
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == companion) == 0
    assert await _count(sessions, CrmContact, CrmContact.dataset_id == companion) == 0
    assert (
        await _count(
            sessions, DatasetFreshness, DatasetFreshness.dataset_id.in_([building, companion])
        )
        == 0
    )
    # The active dataset is untouched and stays in use.
    assert await _state(sessions, active) == "active"
    assert await _state(sessions, active_companion) == "active"
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == active_companion) == 1
    with pytest.raises(DatasetNotFoundError):
        await manager.discard(building)


@pytest.mark.anyio
@pytest.mark.db
async def test_discarding_a_hubspot_build_keeps_the_active_matches(
    manager: DatasetManager, sessions: Sessions
) -> None:
    linkedin, _ = await _activated(manager, sessions, "linkedin", "live")
    leads = await _leads(sessions, linkedin, 2)
    hubspot, _ = await _activated(manager, sessions, "hubspot", "simulated")
    await _stage(sessions, leads, hubspot, "no_match")
    building = await manager.begin("hubspot", "live", reason="recovered")
    await _stage(sessions, leads, building)

    async with sessions() as session, session.begin():
        await manager.discard(building, session=session)
    assert await _row(sessions, building) is None
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == building) == 0
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == hubspot) == 2
    assert await _count(sessions, Lead, Lead.dataset_id == linkedin) == 2
    assert await _state(sessions, hubspot) == "active"


@pytest.mark.anyio
@pytest.mark.db
async def test_discard_refuses_companions_and_non_building_datasets(
    manager: DatasetManager, sessions: Sessions
) -> None:
    active, companion = await _activated(manager, sessions, "linkedin", "simulated")
    assert companion is not None
    old, _ = await _activated(manager, sessions, "google", "simulated")
    await _activated(manager, sessions, "google", "live")
    building = await manager.begin("linkedin", "simulated")
    building_companion = await manager.begin("hubspot", "simulated", "companion", building)

    with pytest.raises(DatasetStateError, match="is a companion; it is discarded with") as error:
        await manager.discard(building_companion)
    assert error.value.dataset_id == building_companion
    with pytest.raises(DatasetStateError, match="is active; only a building dataset"):
        await manager.discard(active)
    with pytest.raises(DatasetStateError, match="is retired; only a building dataset"):
        await manager.discard(old)
    with pytest.raises(DatasetNotFoundError):
        await manager.discard(MISSING_ID)
    for kept in (active, companion, old, building, building_companion):
        assert await _row(sessions, kept) is not None


# ---------------------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_purge_deletes_only_retired_primaries_after_the_retention_period(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    # HubSpot H1 live and Google G1 active at T0.
    h1, _ = await _activated(manager, sessions, "hubspot", "live")
    h1_contact = await _contact(sessions, h1, "301")
    g1, _ = await _activated(manager, sessions, "google", "simulated")

    # Simulated LinkedIn L1 with its companion C1, whose generated CRM rows it reads.
    l1 = await manager.begin("linkedin", "simulated", reason="credentials not configured")
    c1 = await manager.begin("hubspot", "simulated", "companion", l1)
    l1_leads = await _leads(sessions, l1, 2)
    c1_contact = await _contact(sessions, c1, "9000000001")
    await _match(sessions, l1_leads[0], c1, c1_contact)
    await _stage(sessions, l1_leads[1:], c1, "no_match")
    await _make_fresh(manager, sessions, l1)
    await _make_fresh(manager, sessions, c1)
    await manager.activate(l1)

    # Live LinkedIn L2 replaces L1 one hour later; its leads are staged against H1.
    l2 = await manager.begin("linkedin", "live", reason="recovered")
    l2_leads = await _leads(sessions, l2, 2)
    await _match(sessions, l2_leads[0], h1, h1_contact)
    await _stage(sessions, l2_leads[1:], h1, "no_match")
    await _make_fresh(manager, sessions, l2)
    l1_retired_at = clock.advance(timedelta(hours=1))
    assert (await manager.activate(l2)).retired == (l1, c1)

    # HubSpot H2 replaces H1 another hour later, with its own staged matches.
    h2 = await manager.begin("hubspot", "live", reason="recovered")
    h2_contact = await _contact(sessions, h2, "301")
    await _match(sessions, l2_leads[0], h2, h2_contact)
    await _stage(sessions, l2_leads[1:], h2, "no_match")
    await _make_fresh(manager, sessions, h2)
    h1_retired_at = clock.advance(timedelta(hours=1))
    assert (await manager.activate(h2)).retired == (h1,)

    building = await manager.begin("google", "live", reason="probe passed")

    # Both retired primaries are younger than the retention period.
    clock.now = l1_retired_at + RETENTION
    assert await manager.purge_retired() == []
    assert await _state(sessions, l1) == "retired"

    # Past L1's retention only: L1 goes, its companion and every row of both with it.
    clock.now = h1_retired_at + RETENTION - timedelta(minutes=1)
    assert await manager.purge_retired() == [l1]
    for purged in (l1, c1):
        assert await _row(sessions, purged) is None
    assert await _count(sessions, Lead, Lead.dataset_id == l1) == 0
    assert await _count(sessions, LeadMatch, LeadMatch.lead_id.in_(l1_leads)) == 0
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == c1) == 0
    assert await _count(sessions, CrmContact, CrmContact.dataset_id == c1) == 0
    assert await _count(sessions, DatasetFreshness, DatasetFreshness.dataset_id == c1) == 0
    assert await _state(sessions, h1) == "retired"

    # Past H1's: only H1's own matching state goes; the active leads keep H2's rows.
    clock.advance(timedelta(minutes=2))
    assert await manager.purge_retired() == [h1]
    assert await _row(sessions, h1) is None
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == h1) == 0
    assert await _count(sessions, CrmContact, CrmContact.dataset_id == h1) == 0
    assert await _count(sessions, Lead, Lead.dataset_id == l2) == 2
    assert await _count(sessions, LeadMatch, LeadMatch.crm_dataset_id == h2) == 2
    assert await _count(sessions, LeadMatch, LeadMatch.status == "pending") == 0
    assert await _count(sessions, CrmContact, CrmContact.dataset_id == h2) == 1

    # Active and building datasets are never purged, however old.
    clock.advance(timedelta(days=365))
    assert await manager.purge_retired(timedelta(0)) == []
    for kept, state in ((g1, "active"), (l2, "active"), (h2, "active"), (building, "building")):
        assert await _state(sessions, kept) == state
    for source, active in (("google", g1), ("linkedin", l2), ("hubspot", h2)):
        connection = await _connection(sessions, source)
        assert connection is not None
        assert connection.active_dataset_id == active


@pytest.mark.anyio
@pytest.mark.db
async def test_purge_with_zero_retention_purges_every_retired_dataset(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    first, _ = await _activated(manager, sessions, "google", "simulated")
    clock.advance(timedelta(seconds=1))
    second, _ = await _activated(manager, sessions, "google", "live")
    clock.advance(timedelta(seconds=1))
    await _activated(manager, sessions, "google", "simulated")
    clock.advance(timedelta(seconds=1))
    assert await manager.purge_retired(older_than=timedelta(0)) == [first, second]
    with pytest.raises(ValueError, match="must not be negative"):
        await manager.purge_retired(timedelta(seconds=-1))


async def _wait_until_blocked(sessions: Sessions, blocker_pid: int) -> None:
    """Wait until some backend waits on a lock held by ``blocker_pid``, or fail."""
    async with sessions() as session:
        for _ in range(400):
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": blocker_pid},
            )
            await session.rollback()
            if waiting:
                return
            await asyncio.sleep(0.025)
    pytest.fail("the purge never waited on the locked source_dataset row")


@pytest.mark.anyio
@pytest.mark.db
async def test_purge_skips_a_dataset_changed_after_it_was_selected(
    manager: DatasetManager, sessions: Sessions, clock: Clock
) -> None:
    first, _ = await _activated(manager, sessions, "google", "simulated")
    second, _ = await _activated(manager, sessions, "google", "live")
    await _activated(manager, sessions, "google", "simulated")
    clock.advance(RETENTION + timedelta(days=1))

    # Another process holds the first candidate's row and re-stamps its retirement before
    # the purge's DELETE can run; the DELETE re-checks its conditions and skips the row.
    async with sessions() as blocker:
        await blocker.execute(
            select(SourceDataset.id).where(SourceDataset.id == first).with_for_update()
        )
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        assert isinstance(pid, int)
        purge = asyncio.create_task(manager.purge_retired())
        try:
            await _wait_until_blocked(sessions, pid)
            await blocker.execute(
                update(SourceDataset).where(SourceDataset.id == first).values(retired_at=clock.now)
            )
            await blocker.commit()
        finally:
            await blocker.rollback()
            purged = await purge
    assert purged == [second]
    assert await _state(sessions, first) == "retired"
    assert await _row(sessions, second) is None


# ---------------------------------------------------------------------------------------
# Unknown datasets
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    "call",
    [
        "get",
        "companion_of",
        "load_crm_bindings",
        "required_freshness",
        "readiness",
        "activate",
        "discard",
    ],
)
async def test_unknown_dataset_ids_raise_not_found(manager: DatasetManager, call: str) -> None:
    with pytest.raises(DatasetNotFoundError, match=f"source_dataset {MISSING_ID} does not exist"):
        await getattr(manager, call)(MISSING_ID)
