"""Tests for the write-path locks: dataset lifecycle guard, subject serialisation, call forms.

Covered:

* **Subject serialisation**: two writers of one subject, ordinary decisions or
  resolutions of different discrepancy kinds, apply one after the other, and the later one
  applies its change over the earlier one's committed decision (AAP 0.5.2 patch model).
* **Dataset lifecycle guard**: a writer already holding the guard makes an activation of
  the replacement dataset wait until it commits, and a writer arriving while a retirement
  is uncommitted waits for it and is then refused: decisions and resolutions with 409
  ``subject_retired``, fact, snapshot and freshness writes with ``DatasetNotWritable``
  (AAP 0.3.2, 0.3.3). Companion datasets are guarded the same way.
* **Detection on a retired dataset**: discrepancy detection, and ``auto_resolve`` given a
  caller's state, refuse a retired dataset, already retired or retired while detection
  waited, with 409 ``subject_retired``: no discrepancy closes and no ``analysis_request``
  is queued. A pass that judges no subject writes nothing, and ``building`` and ``active``
  datasets still close discrepancies whose platform state matches again.
* **Standalone detection writers**: ``write_baseline``, ``raise_discrepancy`` and
  ``auto_resolve_absent`` called outside a detection pass take the dataset and subject
  locks themselves, also when given a state, so a state read before an activation
  retired its dataset is refused with 409 ``subject_retired`` and nothing is written or
  queued. Told ``locked``, they trust a state the caller loaded with
  ``load_subject_states(..., lock=True)``: a retired one is refused, and otherwise they
  send nothing but their own writes. ``auto_resolve_absent`` refuses a state of another
  subject and judges no tracked (non-tombstone) subject, before any lock or read.
* **Locked bulk load**: ``load_subject_states(..., lock=True)`` holds the dataset and
  subject locks until commit; a decision of the subject and an activation retiring its
  dataset wait for it, then both proceed.
* **Lock order**: a transient-failure record locks ``source_connection`` before the
  dataset, and lead staging locks the LinkedIn dataset before its companion, the order
  activation uses, so neither can deadlock against it.
* **AAP 0.5.1 call forms**: ``record_decision(subject, patch, origin)``,
  ``resolve_discrepancy(id, resolution, intended_state)``, ``upsert_facts(table, rows,
  dataset_id)`` and ``append_snapshots(rows)`` commit a transaction of their own without
  ``session=`` and join the caller's transaction with it; concurrent CRM-binding writers
  still serialise without deadlock.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test starts from empty dataset tables.
Blocking is observed, never assumed: a waiting writer is seen in ``pg_stat_activity`` as
blocked by the holder's backend (``pg_blocking_pids``) before the holder commits. An
unreachable database fails the database tests; it never skips them.
"""

import asyncio
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding, ResolvedBinding
from ads_engine.connectors.errors import TransientError
from ads_engine.db import session as db_session
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.facts import FactCampaignDaily
from ads_engine.db.models.ops import SourceConnection, SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.db.repositories import (
    DatasetNotWritable,
    Row,
    append_snapshots,
    ensure_writable,
    lock_dataset,
    upsert_facts,
)
from ads_engine.decisions import (
    DecisionPatch,
    DiscrepancyClosed,
    PatchRejected,
    SubjectRef,
    SubjectRetired,
    SubjectState,
    auto_resolve,
    auto_resolve_absent,
    compare,
    load_subject_state,
    load_subject_states,
    raise_discrepancy,
    record_decision,
    resolve_discrepancy,
    write_baseline,
)
from ads_engine.settings import Settings
from ads_engine.sync import lead_match
from ads_engine.sync.datasets import Activation, DatasetManager
from ads_engine.sync.freshness import FreshnessOutcome, load_freshness, record_freshness
from ads_engine.sync.mode import SourceModeController
from ads_engine.sync.snapshots import (
    DetectionReport,
    EnumerationResult,
    SnapshotRow,
    detect_discrepancies,
    ensure_baseline,
    write_snapshots,
)

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first entity sync; later instants are whole hours after it."""

OWN_MICROS = 100_000_000
"""The own campaign budget the baseline records: 100.000000 a day."""

WAIT_LIMIT = 30.0
"""Seconds a released writer may take to finish before the test fails."""


def _at(hour: float) -> datetime:
    """Return the instant ``hour`` hours after :data:`T0`."""
    return T0 + timedelta(hours=hour)


class Clock:
    """A settable, timezone-aware clock for :class:`DatasetManager`."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


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
            "for the decision write-guard tests"
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
    schema = f"write_guards_test_{uuid.uuid4().hex}"
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
    ``source_dataset`` cascades to every dataset-scoped table and ``lead_match``;
    ``source_connection`` and ``analysis_request`` are emptied with it.
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
def process_sessions(sessions: Sessions, monkeypatch: pytest.MonkeyPatch) -> Sessions:
    """Make the process-wide session factory this test's, for calls made without a session."""
    monkeypatch.setattr(db_session, "_sessionmaker", sessions)
    return sessions


@pytest.fixture
def manager(sessions: Sessions) -> DatasetManager:
    """Return a dataset manager on the private schema and a clock standing at ``T0``."""
    return DatasetManager(sessions, clock=Clock(T0))


# ---------------------------------------------------------------------------------------
# Lock observation
# ---------------------------------------------------------------------------------------


async def _backend_pid(session: AsyncSession) -> int:
    """Return the PostgreSQL backend pid of ``session``'s connection (begins its transaction)."""
    pid = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(pid, int)
    return pid


async def _wait_until_blocked(sessions: Sessions, blocker_pid: int, count: int = 1) -> None:
    """Wait until ``count`` backends wait on a lock held by ``blocker_pid``, or fail."""
    async with sessions() as session:
        for _ in range(800):
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": blocker_pid},
            )
            await session.rollback()
            if waiting is not None and waiting >= count:
                return
            await asyncio.sleep(0.025)
    pytest.fail(f"{count} writer(s) never waited on the locks of backend {blocker_pid}")


async def _settle[T](task: asyncio.Task[T]) -> T:
    """Await a released writer, failing the test if it does not finish in time."""
    return await asyncio.wait_for(task, timeout=WAIT_LIMIT)


@asynccontextmanager
async def _statements(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Collect the SQL statements ``session``'s connection sends inside the block, in order.

    A writer told that its caller already holds the locks (``locked``) must lock and read
    nothing more, so what it sends is observed rather than assumed.
    """
    connection = (await session.connection()).sync_connection
    assert connection is not None
    sent: list[str] = []

    def record(_connection: object, _cursor: object, statement: str, *_: object) -> None:
        sent.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(connection, "before_cursor_execute", record)


# ---------------------------------------------------------------------------------------
# Scenario builders and readers
# ---------------------------------------------------------------------------------------


async def _make_fresh(manager: DatasetManager, sessions: Sessions, dataset_id: int) -> None:
    """Record every freshness row the dataset requires as a complete success at ``T0``."""
    outcome = FreshnessOutcome.success(T0, covered_from=None, covered_through=None, complete=True)
    async with sessions() as session, session.begin():
        for name in await manager.required_freshness(dataset_id, session=session):
            await record_freshness(session, dataset_id, name, outcome)


async def _ready_build(
    manager: DatasetManager, sessions: Sessions, source: str, mode: str = "simulated"
) -> tuple[int, int | None]:
    """Begin and freshen a primary (and a simulated LinkedIn one's companion), unactivated."""
    dataset_id = await manager.begin(source, mode, reason="credentials not configured")
    companion_id: int | None = None
    if source == "linkedin" and mode == "simulated":
        companion_id = await manager.begin("hubspot", "simulated", "companion", dataset_id)
        await _make_fresh(manager, sessions, companion_id)
    await _make_fresh(manager, sessions, dataset_id)
    return dataset_id, companion_id


async def _activated(
    manager: DatasetManager, sessions: Sessions, source: str, mode: str = "simulated"
) -> tuple[int, int | None]:
    """Begin, freshen and activate a primary; return it and its companion (or ``None``)."""
    dataset_id, companion_id = await _ready_build(manager, sessions, source, mode)
    await manager.activate(dataset_id)
    return dataset_id, companion_id


@dataclass(frozen=True)
class Subject:
    """A Google own-budget campaign with its baseline decision of ``active`` / 100.00."""

    dataset_id: int
    campaign_id: int
    budget_id: int

    @property
    def ref(self) -> SubjectRef:
        """The campaign as a decision subject."""
        return SubjectRef(type="campaign", id=self.campaign_id)


def _rows(subject: Subject, *, status: str, micros: int) -> list[SnapshotRow]:
    """Return one entity sync's snapshot rows of the campaign and its own daily budget."""
    return [
        SnapshotRow.campaign(
            source="google",
            entity_id=subject.campaign_id,
            status=status,
            raw_status="ENABLED" if status == "active" else "PAUSED",
            budget_id=subject.budget_id,
            budget_period="DAILY",
            budget_amount_micros=micros,
        ),
        SnapshotRow.budget(
            entity_id=subject.budget_id,
            period="DAILY",
            amount_micros=micros,
            explicitly_shared=False,
        ),
    ]


async def _google_subject(sessions: Sessions, dataset_id: int, *, baseline: bool = True) -> Subject:
    """Commit an account, an own daily budget and a campaign with their ``T0`` snapshots.

    Their baseline is synced too unless ``baseline`` is off, which leaves the campaign
    without a decision.
    """
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="google",
            dataset_id=dataset_id,
            external_id="1234567890",
            name="Acme",
            currency="USD",
            timezone="America/New_York",
        )
        session.add(account)
        await session.flush()
        budget = Budget(
            source="google",
            dataset_id=dataset_id,
            external_id="501",
            amount=Decimal(OWN_MICROS) / Decimal(1_000_000),
            amount_micros=OWN_MICROS,
            period="DAILY",
            explicitly_shared=False,
        )
        session.add(budget)
        await session.flush()
        campaign = Campaign(
            source="google",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id="11",
            name="Campaign 11",
            status="active",
            raw_status="ENABLED",
            objective_or_channel="SEARCH",
            budget_id=budget.id,
            daily_budget=Decimal(OWN_MICROS) / Decimal(1_000_000),
            budget_period="DAILY",
        )
        session.add(campaign)
        await session.flush()
        subject = Subject(dataset_id=dataset_id, campaign_id=campaign.id, budget_id=budget.id)
        await write_snapshots(
            session, dataset_id, _rows(subject, status="active", micros=OWN_MICROS), captured_at=T0
        )
        if baseline:
            assert await ensure_baseline(session, dataset_id, captured_at=T0, sealed=False) == 1
    return subject


async def _observe(sessions: Sessions, subject: Subject, *, status: str, micros: int) -> None:
    """Run a later entity sync's snapshot and discrepancy steps at ``_at(1)``."""
    async with sessions() as session, session.begin():
        ids = await write_snapshots(
            session,
            subject.dataset_id,
            _rows(subject, status=status, micros=micros),
            captured_at=_at(1),
        )
        enumeration = EnumerationResult(
            source="google",
            complete=True,
            present_campaign_ids=frozenset({subject.campaign_id}),
            present_budget_ids=frozenset({subject.budget_id}),
        )
        await detect_discrepancies(
            session, subject.dataset_id, enumeration, ids, sealed=True, now=_at(1)
        )


async def _discrepant(sessions: Sessions) -> tuple[Subject, dict[str, Discrepancy]]:
    """Return an active-dataset subject whose platform moved to paused / 120.00.

    Its ``status`` and ``budget`` discrepancies are open, keyed by kind.
    """
    dataset_id, _ = await _activated_google(sessions)
    subject = await _google_subject(sessions, dataset_id)
    await _observe(sessions, subject, status="paused", micros=120_000_000)
    rows = await _open(sessions, subject.campaign_id)
    assert set(rows) == {"status", "budget"}
    return subject, rows


async def _activated_google(sessions: Sessions) -> tuple[int, int | None]:
    """Activate a simulated Google dataset through a manager of its own."""
    return await _activated(DatasetManager(sessions, clock=Clock(T0)), sessions, "google")


async def _open(sessions: Sessions, campaign_id: int) -> dict[str, Discrepancy]:
    """Return the campaign's non-resolved discrepancies keyed by kind."""
    async with sessions() as session:
        rows = await session.scalars(
            select(Discrepancy).where(
                Discrepancy.subject_type == "campaign",
                Discrepancy.subject_id == campaign_id,
                Discrepancy.state != "resolved",
            )
        )
        return {row.kind: row for row in rows}


async def _decisions(sessions: Sessions, campaign_id: int) -> list[Decision]:
    """Return the campaign's decisions, latest last."""
    async with sessions() as session:
        rows = await session.scalars(
            select(Decision)
            .where(Decision.subject_type == "campaign", Decision.subject_id == campaign_id)
            .order_by(Decision.decided_at, Decision.id)
        )
        return list(rows)


async def _count(
    sessions: Sessions, model: type[FactCampaignDaily] | type[EntitySnapshot], dataset_id: int
) -> int:
    """Count the fact or snapshot rows of one dataset."""
    async with sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(model).where(model.dataset_id == dataset_id)
        )
    return int(count or 0)


def _fact(subject: Subject, day: int = 30) -> Row:
    """Return one ``fact_campaign_daily`` row of the subject's campaign."""
    return {
        "campaign_id": subject.campaign_id,
        "date": date(2026, 9, day),
        "cost": Decimal("12.500000"),
        "impressions": 400,
        "clicks": 9,
        "conversions": Decimal("1.000000"),
        "conversions_value": Decimal("0.000000"),
    }


def _snapshot(subject: Subject, at: datetime) -> Row:
    """Return one ``entity_snapshot`` row of the subject's campaign."""
    (campaign, _) = _rows(subject, status="active", micros=OWN_MICROS)
    return campaign.to_row(subject.dataset_id, at)


def _failure(hour: float) -> FreshnessOutcome:
    """Return a failed attempt at ``_at(hour)``."""
    return FreshnessOutcome.failure(_at(hour), reason="HTTP 503 after 3 attempts")


async def _freshen(sessions: Sessions, dataset_id: int, name: str, hour: float) -> None:
    """Record a failed attempt of one freshness dataset in a transaction of its own."""
    async with sessions() as session, session.begin():
        await record_freshness(session, dataset_id, name, _failure(hour))


async def _guard(sessions: Sessions, dataset_id: int) -> None:
    """Take and release the dataset guard in a transaction of its own."""
    async with sessions() as session, session.begin():
        await ensure_writable(session, dataset_id)


async def _last_attempt(sessions: Sessions, dataset_id: int, name: str) -> datetime | None:
    """Return a freshness row's ``last_attempt_at``."""
    async with sessions() as session:
        rows = await load_freshness(session, dataset_id)
    return rows[name].last_attempt_at


async def _requests(sessions: Sessions) -> Counter[str]:
    """Count the queued ``analysis_request`` rows by reason."""
    async with sessions() as session:
        reasons = await session.scalars(select(AnalysisRequest.reason))
        return Counter(reasons)


async def _rematched(sessions: Sessions, subject: Subject) -> dict[tuple[str, int], int]:
    """Commit an entity sync's snapshots at ``_at(2)`` matching the baseline again."""
    async with sessions() as session, session.begin():
        return await write_snapshots(
            session,
            subject.dataset_id,
            _rows(subject, status="active", micros=OWN_MICROS),
            captured_at=_at(2),
        )


async def _detect(
    sessions: Sessions, subject: Subject, snapshot_ids: Mapping[tuple[str, int], int], hour: float
) -> DetectionReport:
    """Run detection over ``snapshot_ids`` after a complete enumeration, then commit."""
    enumeration = EnumerationResult(
        source="google",
        complete=True,
        present_campaign_ids=frozenset({subject.campaign_id}),
        present_budget_ids=frozenset({subject.budget_id}),
    )
    async with sessions() as session, session.begin():
        return await detect_discrepancies(
            session, subject.dataset_id, enumeration, snapshot_ids, sealed=True, now=_at(hour)
        )


async def _auto_resolve(
    session: AsyncSession, subject: Subject, state: SubjectState
) -> list[Discrepancy]:
    """Call ``auto_resolve`` with ``state`` and its newest snapshot, which matches its decision."""
    snapshot, decision = state.latest_snapshot, state.current_decision
    assert snapshot is not None
    assert decision is not None
    # The platform matches the decision again, so only the lifecycle guard can refuse.
    assert compare(snapshot, decision, state=state) == []
    return await auto_resolve(
        session,
        subject.ref,
        snapshot,
        decision,
        now=_at(3),
        state=state,
        enumeration_complete=True,
    )


def _untouched(rows: Mapping[str, Discrepancy]) -> dict[str, tuple[int, str, None]]:
    """Return ``rows`` as (id, state, resolved_at) keyed by kind, still open and unresolved."""
    return {kind: (row.id, "open", None) for kind, row in rows.items()}


async def _current(sessions: Sessions, campaign_id: int) -> dict[str, tuple[int, str, Any]]:
    """Return the campaign's discrepancies as (id, state, resolved_at) keyed by kind."""
    async with sessions() as session:
        rows = await session.scalars(
            select(Discrepancy).where(
                Discrepancy.subject_type == "campaign", Discrepancy.subject_id == campaign_id
            )
        )
        return {row.kind: (row.id, row.state, row.resolved_at) for row in rows}


async def _campaign_snapshot(session: AsyncSession, subject: Subject) -> EntitySnapshot:
    """Return the campaign's newest snapshot, read without a lock."""
    snapshot = await session.scalar(
        select(EntitySnapshot)
        .where(
            EntitySnapshot.entity_type == "campaign",
            EntitySnapshot.entity_id == subject.campaign_id,
        )
        .order_by(EntitySnapshot.captured_at.desc(), EntitySnapshot.id.desc())
        .limit(1)
    )
    assert snapshot is not None
    return snapshot


async def _tombstoned(sessions: Sessions, manager: DatasetManager) -> tuple[Subject, Discrepancy]:
    """Return an active-dataset subject whose acknowledged removal was followed by its return.

    A complete enumeration at ``_at(1)`` misses the campaign, whose ``missing``
    discrepancy ``acknowledge_removal`` resolves at ``_at(2)`` with a tombstone decision;
    a complete enumeration at ``_at(3)`` returns it again, which opens the ``status``
    discrepancy against the tombstone returned here. That row is exactly what the
    subject's next absence closes (``auto_resolve_absent``).
    """
    dataset_id, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, dataset_id)
    absent = EnumerationResult(
        source="google", complete=True, missing_campaign_ids=frozenset({subject.campaign_id})
    )
    async with sessions() as session, session.begin():
        report = await detect_discrepancies(
            session, dataset_id, absent, {}, sealed=True, now=_at(1)
        )
    assert report.raised["missing"] == 1
    missing = (await _open(sessions, subject.campaign_id))["missing"]
    async with sessions() as session, session.begin():
        await resolve_discrepancy(missing.id, "acknowledge_removal", session=session, now=_at(2))
    async with sessions() as session, session.begin():
        returned = _rows(subject, status="active", micros=OWN_MICROS)
        ids = await write_snapshots(session, dataset_id, returned, captured_at=_at(3))
    assert (await _detect(sessions, subject, ids, 3)).raised["status"] == 1
    rows = await _open(sessions, subject.campaign_id)
    assert set(rows) == {"status"}
    return subject, rows["status"]


async def _tombstone(session: AsyncSession, subject: Subject) -> Decision:
    """Return the subject's current decision, read without a lock; it must be a tombstone."""
    decision = (await load_subject_state(session, subject.ref)).current_decision
    assert decision is not None
    assert decision.tombstone is True
    return decision


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


def _start[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Start a concurrent writer."""
    return asyncio.create_task(coroutine)


async def _held_off(
    sessions: Sessions,
    manager: DatasetManager,
    holder: AsyncSession,
    subject: Subject,
    replacement: int,
) -> tuple[Decision, Activation]:
    """Observe a decision and a retiring activation wait on ``holder``'s locks, then commit.

    The status decision of ``subject`` (``record_decision`` in a transaction of its own)
    starts first. Its ``FOR SHARE`` on the dataset is compatible with the holder's, so only
    the holder's subject row lock can stop it. The activation of ``replacement``, which
    retires the subject's dataset, starts once the decision is seen waiting, and only the
    holder's dataset lock can stop it. With both seen blocked by the holder's backend
    (``pg_blocking_pids``), the holder commits and both are awaited.

    Returns:
        The decision written and the activation, once both proceeded.
    """
    holder_pid = await _backend_pid(holder)
    writers: list[asyncio.Task[Any]] = [
        _start(record_decision(subject.ref, DecisionPatch(status="paused"), "user", now=_at(2)))
    ]
    try:
        await _wait_until_blocked(sessions, holder_pid)
        writers.append(_start(manager.activate(replacement)))
        await _wait_until_blocked(sessions, holder_pid, count=2)
        assert not any(task.done() for task in writers)
        await holder.commit()
    finally:
        await holder.rollback()
        settled = await asyncio.wait_for(
            asyncio.gather(*writers, return_exceptions=True), timeout=WAIT_LIMIT
        )
    decision, activation = settled
    assert isinstance(decision, Decision), decision
    assert isinstance(activation, Activation), activation
    return decision, activation


async def _raise_status(
    session: AsyncSession, subject: Subject, state: SubjectState, *, locked: bool = False
) -> Discrepancy:
    """Raise a ``status`` discrepancy (platform ``paused``) on ``state``'s snapshot and decision."""
    snapshot, decision = state.latest_snapshot, state.current_decision
    assert snapshot is not None
    assert decision is not None
    return await raise_discrepancy(
        session,
        subject.ref,
        "status",
        decided_value={"status": "active"},
        platform_value={"status": "paused"},
        snapshot_id=snapshot.id,
        decision_id=decision.id,
        detected_at=_at(2),
        state=state,
        locked=locked,
    )


# ---------------------------------------------------------------------------------------
# Subject serialisation (DB-F01, BE2-F01)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_concurrent_disjoint_patches_both_reach_the_latest_decision(
    process_sessions: Sessions,
) -> None:
    sessions = process_sessions
    dataset_id, _ = await _activated_google(sessions)
    subject = await _google_subject(sessions, dataset_id)

    async with sessions() as first:
        holder = await _backend_pid(first)
        paused = await record_decision(
            subject.ref, DecisionPatch(status="paused"), "user", session=first, now=_at(2)
        )
        budget = _start(
            record_decision(
                subject.ref, DecisionPatch(daily_budget=Decimal("120")), "user", now=_at(3)
            )
        )
        try:
            await _wait_until_blocked(sessions, holder)
            assert not budget.done()
            await first.commit()
        finally:
            await first.rollback()
            raised = await _settle(budget)

    assert (paused.status, paused.daily_budget) == ("paused", Decimal(100))
    assert (raised.status, raised.daily_budget) == ("paused", Decimal(120))
    latest = (await _decisions(sessions, subject.campaign_id))[-1]
    assert latest.id == raised.id
    assert (latest.status, latest.daily_budget) == ("paused", Decimal(120))
    # The row records only the submitted field; the status came from the first writer.
    assert set(latest.patch) == {"daily_budget"}


@pytest.mark.anyio
@pytest.mark.db
async def test_resolutions_of_two_kinds_on_one_subject_serialise(
    process_sessions: Sessions,
) -> None:
    sessions = process_sessions
    subject, rows = await _discrepant(sessions)

    async with sessions() as first:
        holder = await _backend_pid(first)
        await resolve_discrepancy(rows["status"].id, "platform_correct", session=first, now=_at(2))
        budget = _start(resolve_discrepancy(rows["budget"].id, "platform_correct", now=_at(3)))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not budget.done()
            await first.commit()
        finally:
            await first.rollback()
            closed = await _settle(budget)

    assert (closed.state, closed.resolution) == ("resolved", "platform_correct")
    assert await _open(sessions, subject.campaign_id) == {}
    decisions = await _decisions(sessions, subject.campaign_id)
    assert [row.origin for row in decisions] == ["baseline", "resolution", "resolution"]
    status_fix, budget_fix = decisions[1:]
    assert (status_fix.status, status_fix.daily_budget) == ("paused", Decimal(100))
    # The budget resolution waited and applied its amount over the status resolution.
    assert (budget_fix.status, budget_fix.daily_budget) == ("paused", Decimal(120))


@pytest.mark.anyio
@pytest.mark.db
async def test_a_decision_waits_for_a_resolution_of_its_subject(
    process_sessions: Sessions,
) -> None:
    sessions = process_sessions
    subject, rows = await _discrepant(sessions)

    async with sessions() as first:
        holder = await _backend_pid(first)
        await resolve_discrepancy(rows["status"].id, "platform_correct", session=first, now=_at(2))
        decision = _start(
            record_decision(
                subject.ref, DecisionPatch(daily_budget=Decimal("130")), "user", now=_at(3)
            )
        )
        try:
            await _wait_until_blocked(sessions, holder)
            assert not decision.done()
            await first.commit()
        finally:
            await first.rollback()
            written = await _settle(decision)

    assert (written.status, written.daily_budget) == ("paused", Decimal(130))


@pytest.mark.anyio
@pytest.mark.db
async def test_a_resolution_closed_while_waiting_is_refused(process_sessions: Sessions) -> None:
    sessions = process_sessions
    _, rows = await _discrepant(sessions)
    target = rows["status"].id

    async with sessions() as first:
        holder = await _backend_pid(first)
        await resolve_discrepancy(target, "platform_correct", session=first, now=_at(2))
        second = _start(resolve_discrepancy(target, "decision_correct", now=_at(3)))
        try:
            await _wait_until_blocked(sessions, holder)
            await first.commit()
        finally:
            await first.rollback()
            with pytest.raises(DiscrepancyClosed) as refused:
                await _settle(second)

    assert (refused.value.status_code, refused.value.code) == (409, "discrepancy_resolved")


# ---------------------------------------------------------------------------------------
# Dataset lifecycle guard (DB-F02, BE2-F01)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("writer", ["decision", "facts", "snapshots", "freshness"])
async def test_an_authorised_writer_makes_the_retiring_activation_wait(
    process_sessions: Sessions, manager: DatasetManager, writer: str
) -> None:
    sessions = process_sessions
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old)
    replacement, _ = await _ready_build(manager, sessions, "google")

    async with sessions() as first:
        holder = await _backend_pid(first)
        if writer == "decision":
            await record_decision(
                subject.ref, DecisionPatch(status="paused"), "user", session=first, now=_at(2)
            )
        elif writer == "facts":
            assert await upsert_facts(FactCampaignDaily, [_fact(subject)], old, session=first) == 1
        elif writer == "snapshots":
            await append_snapshots([_snapshot(subject, _at(2))], session=first)
        else:
            await record_freshness(first, old, "google.metrics_daily", _failure(2))
        activation = _start(manager.activate(replacement))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not activation.done()
            await first.commit()
        finally:
            await first.rollback()
            activated = await _settle(activation)

    assert (activated.activated, activated.retired) == ((replacement,), (old,))
    # The write committed before the retirement, so it stands in the now-retired dataset.
    if writer == "decision":
        assert (await _decisions(sessions, subject.campaign_id))[-1].status == "paused"
    elif writer == "facts":
        assert await _count(sessions, FactCampaignDaily, old) == 1
    elif writer == "snapshots":
        assert await _count(sessions, EntitySnapshot, old) == 3
    else:
        assert await _last_attempt(sessions, old, "google.metrics_daily") == _at(2)


@pytest.mark.anyio
@pytest.mark.db
async def test_writes_waiting_on_an_uncommitted_retirement_are_refused(
    process_sessions: Sessions, manager: DatasetManager
) -> None:
    sessions = process_sessions
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old)
    await _observe(sessions, subject, status="paused", micros=120_000_000)
    rows = await _open(sessions, subject.campaign_id)
    replacement, _ = await _ready_build(manager, sessions, "google")
    snapshots_before = await _count(sessions, EntitySnapshot, old)

    async with sessions() as retiring:
        holder = await _backend_pid(retiring)
        activation = await manager.activate(replacement, session=retiring)
        assert activation.retired == (old,)
        writers: dict[str, asyncio.Task[object]] = {
            "decision": _start(
                record_decision(subject.ref, DecisionPatch(status="ended"), "user", now=_at(2))
            ),
            "resolution": _start(
                resolve_discrepancy(rows["status"].id, "decision_correct", now=_at(2))
            ),
            "facts": _start(upsert_facts(FactCampaignDaily, [_fact(subject)], old)),
            "snapshots": _start(append_snapshots([_snapshot(subject, _at(2))])),
            "freshness": _start(_freshen(sessions, old, "google.metrics_daily", 2)),
        }
        try:
            await _wait_until_blocked(sessions, holder, count=len(writers))
            assert not any(task.done() for task in writers.values())
            await retiring.commit()
        finally:
            await retiring.rollback()
            settled = await asyncio.wait_for(
                asyncio.gather(*writers.values(), return_exceptions=True), timeout=WAIT_LIMIT
            )
    outcomes: Mapping[str, object] = dict(zip(writers, settled, strict=True))

    for name in ("decision", "resolution"):
        refused = outcomes[name]
        assert isinstance(refused, SubjectRetired), name
        assert (refused.status_code, refused.code) == (409, "subject_retired")
    for name in ("facts", "snapshots", "freshness"):
        refused = outcomes[name]
        assert isinstance(refused, DatasetNotWritable), name
        assert (refused.dataset_id, refused.state) == (old, "retired")
    assert [row.origin for row in await _decisions(sessions, subject.campaign_id)] == ["baseline"]
    assert (await _open(sessions, subject.campaign_id))["status"].state == "open"
    assert await _count(sessions, FactCampaignDaily, old) == 0
    assert await _count(sessions, EntitySnapshot, old) == snapshots_before
    assert await _last_attempt(sessions, old, "google.metrics_daily") == T0

    # A request that begins after the retirement is refused at once.
    with pytest.raises(SubjectRetired):
        await record_decision(subject.ref, DecisionPatch(status="ended"), "user", now=_at(3))


@pytest.mark.anyio
@pytest.mark.db
async def test_a_companion_is_guarded_like_its_parent(
    sessions: Sessions, manager: DatasetManager
) -> None:
    first, first_companion = await _activated(manager, sessions, "linkedin")
    second, second_companion = await _ready_build(manager, sessions, "linkedin")
    assert first_companion is not None
    assert second_companion is not None

    # A companion writer already holding the guard: the retiring activation waits for it.
    async with sessions() as writer:
        holder = await _backend_pid(writer)
        await record_freshness(writer, first_companion, "hubspot.contacts", _failure(2))
        activation = _start(manager.activate(second))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not activation.done()
            await writer.commit()
        finally:
            await writer.rollback()
            activated = await _settle(activation)
    assert activated.retired == (first, first_companion)
    assert await _last_attempt(sessions, first_companion, "hubspot.contacts") == _at(2)

    # An uncommitted retirement of a companion: its writers wait, then are refused.
    third, _ = await _ready_build(manager, sessions, "linkedin")
    async with sessions() as retiring:
        holder = await _backend_pid(retiring)
        retired = await manager.activate(third, session=retiring)
        assert retired.retired == (second, second_companion)
        writers = [
            _start(_freshen(sessions, second_companion, "hubspot.contacts", 3)),
            _start(_guard(sessions, second_companion)),
        ]
        try:
            await _wait_until_blocked(sessions, holder, count=len(writers))
            await retiring.commit()
        finally:
            await retiring.rollback()
            settled = await asyncio.wait_for(
                asyncio.gather(*writers, return_exceptions=True), timeout=WAIT_LIMIT
            )
    for refused in settled:
        assert isinstance(refused, DatasetNotWritable)
        assert (refused.dataset_id, refused.state) == (second_companion, "retired")
    assert await _last_attempt(sessions, second_companion, "hubspot.contacts") == T0


@pytest.mark.anyio
@pytest.mark.db
async def test_the_guard_lets_writers_of_one_dataset_and_key_share_checks_through(
    sessions: Sessions, manager: DatasetManager
) -> None:
    dataset_id, _ = await _activated(manager, sessions, "google")

    async with sessions() as first:
        await ensure_writable(first, dataset_id)
        # Shared guards never serialise writers of one dataset.
        await asyncio.wait_for(_guard(sessions, dataset_id), timeout=WAIT_LIMIT)
        await first.rollback()

    async with sessions() as binder:
        locked = await lock_dataset(binder, dataset_id, exclusive=True)
        assert locked is not None
        assert locked.state == "active"
        # FOR NO KEY UPDATE still admits the FOR KEY SHARE check of a foreign-key insert.
        async with sessions() as inserter:
            inserter.add(
                AdAccount(
                    source="google",
                    dataset_id=dataset_id,
                    external_id="9876543210",
                    name="Other",
                    currency="EUR",
                    timezone="Europe/Berlin",
                )
            )
            await asyncio.wait_for(inserter.flush(), timeout=WAIT_LIMIT)
            await inserter.rollback()
        await binder.rollback()

    async with sessions() as session:
        assert await lock_dataset(session, 987_654_321) is None
        with pytest.raises(DatasetNotWritable) as missing:
            await ensure_writable(session, 987_654_321)
    assert missing.value.state is None


# ---------------------------------------------------------------------------------------
# Detection and auto-resolution on a retired dataset (DB-F02)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_detection_on_a_retired_dataset_is_refused_and_closes_nothing(
    sessions: Sessions, manager: DatasetManager
) -> None:
    subject, opened = await _discrepant(sessions)
    ids = await _rematched(sessions, subject)
    replacement, _ = await _ready_build(manager, sessions, "google")
    assert (await manager.activate(replacement)).retired == (subject.dataset_id,)
    before = await _requests(sessions)

    with pytest.raises(SubjectRetired) as refused:
        await _detect(sessions, subject, ids, 3)

    assert (refused.value.status_code, refused.value.code) == (409, "subject_retired")
    assert refused.value.details == {"dataset_id": subject.dataset_id}
    assert await _current(sessions, subject.campaign_id) == _untouched(opened)
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_detection_waiting_on_an_uncommitted_retirement_is_refused(
    sessions: Sessions, manager: DatasetManager
) -> None:
    subject, opened = await _discrepant(sessions)
    ids = await _rematched(sessions, subject)
    replacement, _ = await _ready_build(manager, sessions, "google")
    before = await _requests(sessions)

    async with sessions() as retiring:
        holder = await _backend_pid(retiring)
        activation = await manager.activate(replacement, session=retiring)
        assert activation.retired == (subject.dataset_id,)
        detection = _start(_detect(sessions, subject, ids, 3))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not detection.done()
            await retiring.commit()
        finally:
            await retiring.rollback()
            (outcome,) = await asyncio.wait_for(
                asyncio.gather(detection, return_exceptions=True), timeout=WAIT_LIMIT
            )

    assert isinstance(outcome, SubjectRetired)
    assert (outcome.status_code, outcome.code) == (409, "subject_retired")
    assert await _current(sessions, subject.campaign_id) == _untouched(opened)
    # The activation queued its own rerun; the refused detection queued nothing.
    assert await _requests(sessions) - before == Counter({"dataset_activated:google": 1})


@pytest.mark.anyio
@pytest.mark.db
async def test_auto_resolve_refuses_a_retired_dataset_whatever_state_it_is_given(
    sessions: Sessions, manager: DatasetManager
) -> None:
    subject, opened = await _discrepant(sessions)
    await _rematched(sessions, subject)
    replacement, _ = await _ready_build(manager, sessions, "google")

    async with sessions() as stale:
        # Read without the locks while the dataset is active, then retired underneath.
        unlocked = await load_subject_state(stale, subject.ref)
        assert unlocked.dataset_state == "active"
        assert (await manager.activate(replacement)).retired == (subject.dataset_id,)
        before = await _requests(sessions)
        with pytest.raises(SubjectRetired):
            await _auto_resolve(stale, subject, unlocked)
        await stale.commit()

    async with sessions() as locked:
        state = await load_subject_state(locked, subject.ref, lock=True)
        assert state.dataset_state == "retired"
        with pytest.raises(SubjectRetired) as refused:
            await _auto_resolve(locked, subject, state)
        await locked.commit()

    assert (refused.value.status_code, refused.value.code) == (409, "subject_retired")
    assert await _current(sessions, subject.campaign_id) == _untouched(opened)
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_a_detection_pass_judging_no_subject_of_a_retired_dataset_writes_nothing(
    process_sessions: Sessions, manager: DatasetManager
) -> None:
    sessions = process_sessions
    subject, opened = await _discrepant(sessions)
    ids = await _rematched(sessions, subject)
    # A decision recorded after the _at(2) observation: that snapshot cannot judge it.
    await record_decision(subject.ref, DecisionPatch(status="ended"), "user", now=_at(3))
    replacement, _ = await _ready_build(manager, sessions, "google")
    assert (await manager.activate(replacement)).retired == (subject.dataset_id,)
    before = await _requests(sessions)

    report = await _detect(sessions, subject, ids, 4)

    assert (report.checked, report.skipped, report.total_raised) == (1, 1, 0)
    assert (report.refreshed, report.auto_resolved) == (0, 0)
    assert await _current(sessions, subject.campaign_id) == _untouched(opened)
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("dataset_state", ["active", "building"])
@pytest.mark.parametrize("caller", ["auto_resolve", "detection"])
async def test_a_writable_dataset_still_closes_matching_discrepancies(
    sessions: Sessions, manager: DatasetManager, dataset_state: str, caller: str
) -> None:
    if dataset_state == "active":
        dataset_id, _ = await _activated(manager, sessions, "google")
    else:
        dataset_id, _ = await _ready_build(manager, sessions, "google")
    subject = await _google_subject(sessions, dataset_id)
    await _observe(sessions, subject, status="paused", micros=120_000_000)
    opened = await _open(sessions, subject.campaign_id)
    assert set(opened) == {"status", "budget"}
    ids = await _rematched(sessions, subject)
    before = await _requests(sessions)

    if caller == "auto_resolve":
        async with sessions() as session, session.begin():
            state = await load_subject_state(session, subject.ref, lock=True)
            assert state.dataset_state == dataset_state
            closed = await _auto_resolve(session, subject, state)
        assert sorted(row.id for row in closed) == sorted(row.id for row in opened.values())
    else:
        report = await _detect(sessions, subject, ids, 3)
        assert (report.checked, report.auto_resolved, report.total_raised) == (1, 2, 0)

    assert await _current(sessions, subject.campaign_id) == {
        kind: (row.id, "resolved", _at(3)) for kind, row in opened.items()
    }
    assert await _requests(sessions) - before == Counter({"discrepancy_resolved": 1})


# ---------------------------------------------------------------------------------------
# Standalone detection writers and the locked bulk load (merged C0 detection and locks)
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_write_baseline_without_a_state_writes_under_the_dataset_and_subject_locks(
    process_sessions: Sessions, manager: DatasetManager
) -> None:
    sessions = process_sessions
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old, baseline=False)
    replacement, _ = await _ready_build(manager, sessions, "google")

    async with sessions() as holder:
        snapshot = await _campaign_snapshot(holder, subject)
        baseline = await write_baseline(holder, subject.ref, snapshot, now=T0)
        assert (baseline.origin, baseline.status, baseline.daily_budget) == (
            "baseline",
            "active",
            Decimal(100),
        )
        decision, activation = await _held_off(sessions, manager, holder, subject, replacement)

    # The decision waited for the baseline to commit, then applied its patch over it.
    assert (decision.status, decision.daily_budget) == ("paused", Decimal(100))
    decisions = await _decisions(sessions, subject.campaign_id)
    assert [(row.id, row.origin) for row in decisions] == [
        (baseline.id, "baseline"),
        (decision.id, "user"),
    ]
    assert (activation.activated, activation.retired) == ((replacement,), (old,))


@pytest.mark.anyio
@pytest.mark.db
async def test_write_baseline_refuses_a_retired_dataset_whatever_state_it_is_given(
    sessions: Sessions, manager: DatasetManager
) -> None:
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old, baseline=False)
    replacement, _ = await _ready_build(manager, sessions, "google")
    refusals: list[SubjectRetired] = []

    async with sessions() as stale:
        # Read without the locks while the dataset is active, then retired underneath.
        unlocked = await load_subject_state(stale, subject.ref)
        assert (unlocked.dataset_state, unlocked.current_decision) == ("active", None)
        snapshot = unlocked.latest_snapshot
        assert snapshot is not None
        assert (await manager.activate(replacement)).retired == (old,)
        with pytest.raises(SubjectRetired) as refused:
            await write_baseline(stale, subject.ref, snapshot, now=T0, state=unlocked)
        refusals.append(refused.value)
        await stale.commit()

    async with sessions() as session:
        snapshot = await _campaign_snapshot(session, subject)
        with pytest.raises(SubjectRetired) as refused:
            await write_baseline(session, subject.ref, snapshot, now=T0)
        refusals.append(refused.value)
        await session.commit()

    async with sessions() as locked:
        state = (await load_subject_states(locked, [subject.ref], lock=True)).require(subject.ref)
        assert state.dataset_state == "retired"
        snapshot = await _campaign_snapshot(locked, subject)
        with pytest.raises(SubjectRetired) as refused:
            await write_baseline(locked, subject.ref, snapshot, now=T0, state=state, locked=True)
        refusals.append(refused.value)
        await locked.commit()

    for refusal in refusals:
        assert (refusal.status_code, refusal.code) == (409, "subject_retired")
        assert refusal.details == {"dataset_id": old}
    assert await _decisions(sessions, subject.campaign_id) == []


@pytest.mark.anyio
@pytest.mark.db
async def test_write_baseline_trusts_a_locked_state_and_sends_nothing_but_its_insert(
    sessions: Sessions, manager: DatasetManager
) -> None:
    dataset_id, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, dataset_id, baseline=False)

    async with sessions() as session, session.begin():
        state = (await load_subject_states(session, [subject.ref], lock=True)).require(subject.ref)
        assert (state.dataset_state, state.current_decision) == ("active", None)
        snapshot = state.latest_snapshot
        assert snapshot is not None
        async with _statements(session) as sent:
            baseline = await write_baseline(
                session, subject.ref, snapshot, now=T0, state=state, locked=True
            )
        # Nothing is locked or read again: the new row's flush is all that is sent.
        assert len(sent) == 1, sent
        assert sent[0].startswith("INSERT INTO decision "), sent
    assert (baseline.origin, baseline.status, baseline.daily_budget) == (
        "baseline",
        "active",
        Decimal(100),
    )

    async with sessions() as session, session.begin():
        state = (await load_subject_states(session, [subject.ref], lock=True)).require(subject.ref)
        assert state.current_decision is not None
        snapshot = await _campaign_snapshot(session, subject)
        async with _statements(session) as sent:
            existing = await write_baseline(
                session, subject.ref, snapshot, now=_at(1), state=state, locked=True
            )
        assert sent == []
        assert existing is state.current_decision
        # Locking and reading again itself, it still returns that decision unchanged.
        again = await write_baseline(session, subject.ref, snapshot, now=_at(1))
        assert again.id == baseline.id

    decisions = await _decisions(sessions, subject.campaign_id)
    assert [(row.id, row.origin, row.decided_at) for row in decisions] == [
        (baseline.id, "baseline", T0)
    ]


@pytest.mark.anyio
@pytest.mark.db
async def test_raise_discrepancy_refuses_a_retired_dataset_whatever_state_it_is_given(
    sessions: Sessions, manager: DatasetManager
) -> None:
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old)
    replacement, _ = await _ready_build(manager, sessions, "google")
    refusals: list[SubjectRetired] = []

    async with sessions() as stale:
        # Read without the locks while the dataset is active, then retired underneath.
        unlocked = await load_subject_state(stale, subject.ref)
        assert unlocked.dataset_state == "active"
        assert (await manager.activate(replacement)).retired == (old,)
        before = await _requests(sessions)
        with pytest.raises(SubjectRetired) as refused:
            await _raise_status(stale, subject, unlocked)
        refusals.append(refused.value)
        await stale.commit()

    async with sessions() as locked:
        state = (await load_subject_states(locked, [subject.ref], lock=True)).require(subject.ref)
        assert state.dataset_state == "retired"
        with pytest.raises(SubjectRetired) as refused:
            await _raise_status(locked, subject, state, locked=True)
        refusals.append(refused.value)
        await locked.commit()

    for refusal in refusals:
        assert (refusal.status_code, refusal.code) == (409, "subject_retired")
        assert refusal.details == {"dataset_id": old}
    # No row was inserted and no discrepancy_created request was queued.
    assert await _current(sessions, subject.campaign_id) == {}
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_auto_resolve_absent_refuses_a_retired_dataset_whatever_state_it_is_given(
    sessions: Sessions, manager: DatasetManager
) -> None:
    subject, reappeared = await _tombstoned(sessions, manager)
    replacement, _ = await _ready_build(manager, sessions, "google")
    refusals: list[SubjectRetired] = []

    async with sessions() as stale:
        # Read without the locks while the dataset is active, then retired underneath.
        unlocked = await load_subject_state(stale, subject.ref)
        assert unlocked.dataset_state == "active"
        tombstone = unlocked.current_decision
        assert tombstone is not None
        assert tombstone.tombstone is True
        assert (await manager.activate(replacement)).retired == (subject.dataset_id,)
        before = await _requests(sessions)
        with pytest.raises(SubjectRetired) as refused:
            await auto_resolve_absent(stale, subject.ref, tombstone, now=_at(4), state=unlocked)
        refusals.append(refused.value)
        await stale.commit()

    async with sessions() as session:
        tombstone = await _tombstone(session, subject)
        with pytest.raises(SubjectRetired) as refused:
            await auto_resolve_absent(session, subject.ref, tombstone, now=_at(4))
        refusals.append(refused.value)
        await session.commit()

    async with sessions() as locked:
        state = (await load_subject_states(locked, [subject.ref], lock=True)).require(subject.ref)
        assert state.dataset_state == "retired"
        tombstone = state.current_decision
        assert tombstone is not None
        with pytest.raises(SubjectRetired) as refused:
            await auto_resolve_absent(
                locked, subject.ref, tombstone, now=_at(4), state=state, locked=True
            )
        refusals.append(refused.value)
        await locked.commit()

    for refusal in refusals:
        assert (refusal.status_code, refusal.code) == (409, "subject_retired")
        assert refusal.details == {"dataset_id": subject.dataset_id}
    # The reappearance row stays open and no discrepancy_resolved request was queued.
    assert (await _current(sessions, subject.campaign_id))["status"] == (
        reappeared.id,
        "open",
        None,
    )
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_auto_resolve_absent_refuses_a_state_describing_another_subject(
    sessions: Sessions, manager: DatasetManager
) -> None:
    subject, reappeared = await _tombstoned(sessions, manager)
    before = await _requests(sessions)

    async with sessions() as session, session.begin():
        state = (await load_subject_states(session, [subject.ref], lock=True)).require(subject.ref)
        tombstone = state.current_decision
        assert tombstone is not None
        assert tombstone.tombstone is True
        other = replace(state, ref=SubjectRef(type="campaign", id=subject.campaign_id + 1))
        for locked in (False, True):
            with pytest.raises(ValueError, match="state describes"):
                await auto_resolve_absent(
                    session, subject.ref, tombstone, now=_at(4), state=other, locked=locked
                )
        # The subject's own locked state closes the reappearance row, so only the check
        # stopped the calls above.
        closed = await auto_resolve_absent(
            session, subject.ref, tombstone, now=_at(4), state=state, locked=True
        )
        assert [row.id for row in closed] == [reappeared.id]

    assert (await _current(sessions, subject.campaign_id))["status"] == (
        reappeared.id,
        "resolved",
        _at(4),
    )
    assert await _requests(sessions) - before == Counter({"discrepancy_resolved": 1})


@pytest.mark.anyio
@pytest.mark.db
async def test_auto_resolve_absent_closes_nothing_for_a_tracked_subject_before_any_read(
    sessions: Sessions,
) -> None:
    subject, opened = await _discrepant(sessions)
    before = await _requests(sessions)

    async with sessions() as session, session.begin():
        state = await load_subject_state(session, subject.ref)
        decision = state.current_decision
        assert decision is not None
        assert decision.tombstone is False
        # A tracked subject's absence is a missing discrepancy, never a match: its open
        # status and budget rows stay open, and nothing is locked or read to say so.
        async with _statements(session) as sent:
            for given in (None, state):
                closed = await auto_resolve_absent(
                    session, subject.ref, decision, now=_at(2), state=given
                )
                assert closed == []
        assert sent == []

    assert await _current(sessions, subject.campaign_id) == _untouched(opened)
    assert await _requests(sessions) == before


@pytest.mark.anyio
@pytest.mark.db
async def test_a_locked_bulk_load_holds_the_dataset_and_subject_locks_until_commit(
    process_sessions: Sessions, manager: DatasetManager
) -> None:
    sessions = process_sessions
    old, _ = await _activated(manager, sessions, "google")
    subject = await _google_subject(sessions, old)
    replacement, _ = await _ready_build(manager, sessions, "google")

    async with sessions() as holder:
        loaded = await load_subject_states(holder, [subject.ref], lock=True)
        assert loaded.require(subject.ref).dataset_state == "active"
        decision, activation = await _held_off(sessions, manager, holder, subject, replacement)

    # Both proceeded after the commit: the decision over the baseline, and then the
    # retirement, which also waited for the decision's own dataset lock.
    assert (decision.status, decision.daily_budget) == ("paused", Decimal(100))
    assert [row.origin for row in await _decisions(sessions, subject.campaign_id)] == [
        "baseline",
        "user",
    ]
    assert (activation.activated, activation.retired) == ((replacement,), (old,))


# ---------------------------------------------------------------------------------------
# Lock order against activation
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_a_transient_failure_locks_the_connection_before_the_dataset(
    sessions: Sessions, manager: DatasetManager
) -> None:
    old, _ = await _activated(manager, sessions, "google")
    controller = SourceModeController(
        Settings(data_mode="simulated"),
        datasets=manager,
        sessionmaker=sessions,
        clock=Clock(_at(2)),
    )
    error = TransientError("HTTP 503 after 3 attempts", source="google", endpoint="search")

    async with sessions() as activation:
        holder = await _backend_pid(activation)
        # Activation's first step: lock the source's connection row.
        await activation.execute(
            select(SourceConnection.source)
            .where(SourceConnection.source == "google")
            .with_for_update()
        )
        failure = _start(
            controller.on_error("google", error, dataset_id=old, datasets=("google.metrics_daily",))
        )
        try:
            await _wait_until_blocked(sessions, holder)
            # The failure recorder waits on the connection holding no dataset lock, so the
            # activation's next step, locking the dataset it retires, never waits on it.
            await activation.execute(
                select(SourceDataset.id).where(SourceDataset.id == old).with_for_update(nowait=True)
            )
            await activation.execute(
                update(SourceDataset)
                .where(SourceDataset.id == old)
                .values(state="retired", retired_at=_at(2))
            )
            await activation.commit()
        finally:
            await activation.rollback()
            disposition = await _settle(failure)

    assert disposition == "stale"
    async with sessions() as session:
        connection = await session.scalar(
            select(SourceConnection).where(SourceConnection.source == "google")
        )
        assert connection is not None
        assert (connection.status, connection.last_attempt_at) == ("stale", _at(2))
    # The dataset retired first, so its freshness row is read-only history.
    assert await _last_attempt(sessions, old, "google.metrics_daily") == T0


@pytest.mark.anyio
@pytest.mark.db
async def test_staging_locks_the_linkedin_dataset_before_its_companion(
    sessions: Sessions, manager: DatasetManager
) -> None:
    parent = await manager.begin("linkedin", "simulated")
    companion = await manager.begin("hubspot", "simulated", "companion", parent)

    async def stage() -> int:
        async with sessions() as session, session.begin():
            return await lead_match.stage_rows(
                session, companion, parent, since=T0 - timedelta(days=30), now=T0
            )

    async with sessions() as activation:
        holder = await _backend_pid(activation)
        # Activation locks a primary before its companion.
        await activation.execute(
            select(SourceDataset.id).where(SourceDataset.id == parent).with_for_update()
        )
        staging = _start(stage())
        try:
            await _wait_until_blocked(sessions, holder)
            await activation.execute(
                select(SourceDataset.id)
                .where(SourceDataset.id == companion)
                .with_for_update(nowait=True)
            )
            await activation.commit()
        finally:
            await activation.rollback()
            staged = await _settle(staging)
    assert staged == 0


# ---------------------------------------------------------------------------------------
# AAP 0.5.1 call forms (A2-F09) and CRM bindings
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_aap_call_forms_join_a_given_session_without_committing(
    process_sessions: Sessions,
) -> None:
    sessions = process_sessions
    subject, rows = await _discrepant(sessions)
    snapshots_before = await _count(sessions, EntitySnapshot, subject.dataset_id)

    async with sessions() as session:
        decision = await record_decision(
            subject.ref, DecisionPatch(status="paused"), "user", session=session, now=_at(2)
        )
        assert decision.status == "paused"
        waiting = await resolve_discrepancy(
            rows["budget"].id, "decision_correct", None, session=session, now=_at(2)
        )
        assert waiting.state == "awaiting_platform"
        assert (
            await upsert_facts(
                FactCampaignDaily, [_fact(subject)], subject.dataset_id, session=session
            )
            == 1
        )
        assert len(await append_snapshots([_snapshot(subject, _at(2))], session=session)) == 1
        await session.rollback()

    assert [row.origin for row in await _decisions(sessions, subject.campaign_id)] == ["baseline"]
    assert (await _open(sessions, subject.campaign_id))["budget"].state == "open"
    assert await _count(sessions, FactCampaignDaily, subject.dataset_id) == 0
    assert await _count(sessions, EntitySnapshot, subject.dataset_id) == snapshots_before


@pytest.mark.anyio
@pytest.mark.db
async def test_aap_call_forms_commit_a_transaction_of_their_own(
    process_sessions: Sessions,
) -> None:
    sessions = process_sessions
    subject, rows = await _discrepant(sessions)
    snapshots_before = await _count(sessions, EntitySnapshot, subject.dataset_id)

    decision = await record_decision(
        subject.ref, DecisionPatch(status="paused"), "user", now=_at(2)
    )
    waiting = await resolve_discrepancy(rows["budget"].id, "decision_correct", None, now=_at(2))
    assert await upsert_facts(FactCampaignDaily, [_fact(subject)], subject.dataset_id) == 1
    (snapshot_id,) = await append_snapshots([_snapshot(subject, _at(2))])
    assert await upsert_facts(FactCampaignDaily, [], subject.dataset_id) == 0
    assert await append_snapshots([]) == []

    latest = (await _decisions(sessions, subject.campaign_id))[-1]
    assert (latest.id, latest.status, latest.origin) == (decision.id, "paused", "user")
    open_rows = await _open(sessions, subject.campaign_id)
    assert open_rows["budget"].id == waiting.id
    assert (open_rows["budget"].state, open_rows["budget"].resolution) == (
        "awaiting_platform",
        "decision_correct",
    )
    assert await _count(sessions, FactCampaignDaily, subject.dataset_id) == 1
    assert await _count(sessions, EntitySnapshot, subject.dataset_id) == snapshots_before + 1
    async with sessions() as session:
        assert await session.get(EntitySnapshot, snapshot_id) is not None

    # A refused call of its own unit of work rolls back and leaves nothing behind.
    with pytest.raises(PatchRejected):
        await record_decision(subject.ref, DecisionPatch(status="paused"), "user", now=_at(3))
    assert (await _decisions(sessions, subject.campaign_id))[-1].id == decision.id


@pytest.mark.anyio
@pytest.mark.db
async def test_concurrent_crm_binders_serialise_without_deadlock(
    sessions: Sessions, manager: DatasetManager
) -> None:
    hubspot, _ = await _activated(manager, sessions, "hubspot")
    first_bindings, second_bindings = _bindings(google=False), _bindings(google=True)

    async with sessions() as first:
        holder = await _backend_pid(first)
        await manager.store_crm_bindings(hubspot, first_bindings, session=first)
        second = _start(manager.store_crm_bindings(hubspot, second_bindings))
        try:
            await _wait_until_blocked(sessions, holder)
            assert not second.done()
            await first.commit()
        finally:
            await first.rollback()
            await _settle(second)

    assert await manager.load_crm_bindings(hubspot) == second_bindings
    # An active dataset's shared writers still proceed beside the stored bindings.
    await _freshen(sessions, hubspot, "hubspot.contacts", 4)
    assert await _last_attempt(sessions, hubspot, "hubspot.contacts") == _at(4)
