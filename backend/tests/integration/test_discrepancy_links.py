"""Tests for the observation links :func:`ads_engine.decisions.raise_discrepancy` stores.

A discrepancy names the snapshot and the decision its comparison read (AAP 0.5.2
``discrepancy``; the ``Discrepancy`` model contract). ``snapshot_id`` is ``None`` exactly for
``missing``, where the platform no longer returns the entity, and ``decision_id`` is ``None``
exactly for ``untracked``, which has no decision. Every other link names a row of the
subject itself, in the subject's dataset and source. The dataset-qualified foreign keys
check only the dataset, so the writer checks the rest before it inserts or refreshes a row:
on the insert path, on an open row of the kind, on the ``untracked`` row waiting after
``record_intended`` and on the row a concurrent writer inserted first. A refused raise
leaves every stored row as it was and queues nothing.

The checks reuse the records the caller preloaded, so detection after an entity sync reads
nothing while it raises, refreshes or folds discrepancies.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds every table
of the model metadata in it, points every connection's ``search_path`` at it alone, and drops
it with ``CASCADE`` at teardown, so the ``public`` schema and every other schema stay
untouched. Each test builds its own datasets; an active one first retires the source's
previous active primary. An unreachable database fails the tests; it never skips them.
"""

import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    DiscrepancyKind,
    SubjectRef,
    SubjectState,
    load_subject_states,
    raise_discrepancy,
    resolve_discrepancy,
)
from ads_engine.sync import snapshots
from ads_engine.sync.freshness import FreshnessOutcome, record_freshness
from ads_engine.sync.snapshots import (
    DetectionReport,
    EnumerationResult,
    SnapshotRow,
    baseline_sealed,
    detect_discrepancies,
    ensure_baseline,
    write_snapshots,
)

type Sessions = async_sessionmaker[AsyncSession]

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first sync; later syncs are whole hours after it."""

BEFORE_T0 = T0 - timedelta(days=1)
"""When the contrived rows of another dataset or source were observed or decided: before
every row of the world, so none is ever a subject's newest snapshot or current decision."""

OWN_MICROS = 100_000_000
SHARED_MICROS = 50_000_000
LATE_MICROS = 80_000_000


def _at(hour: float) -> datetime:
    """Return the instant ``hour`` hours after :data:`T0`."""
    return T0 + timedelta(hours=hour)


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
            "for the discrepancy link tests"
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
    schema = f"discrepancy_links_test_{uuid.uuid4().hex}"
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
    """Yield a session factory on an engine created and disposed inside this test's loop."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


async def _dataset(session: AsyncSession, source: str, state: str = "active") -> int:
    """Insert a primary dataset; an active one first retires the source's previous one."""
    if state == "active":
        await session.execute(
            update(SourceDataset)
            .where(
                SourceDataset.source == source,
                SourceDataset.state == "active",
                SourceDataset.role == "primary",
            )
            .values(state="retired", retired_at=T0)
        )
    dataset = SourceDataset(source=source, mode="simulated", state=state, role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


@dataclass(frozen=True)
class GoogleWorld:
    """A Google dataset: one own-budget campaign and two members of one shared budget."""

    dataset_id: int
    account_id: int
    own_budget: int
    shared_budget: int
    other_shared: int
    own: int
    member_a: int
    member_b: int


async def _budget(
    session: AsyncSession, dataset_id: int, external_id: str, micros: int | None, *, shared: bool
) -> int:
    """Insert a Google DAILY budget and return its id."""
    row = Budget(
        source="google",
        dataset_id=dataset_id,
        external_id=external_id,
        amount=None if micros is None else Decimal(micros) / Decimal(1_000_000),
        amount_micros=micros,
        period="DAILY",
        explicitly_shared=shared,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _google_campaign(
    session: AsyncSession,
    dataset_id: int,
    account_id: int,
    external_id: str,
    budget_id: int,
    *,
    own_micros: int | None = None,
) -> int:
    """Insert a Google campaign on a DAILY budget and return its id."""
    row = Campaign(
        source="google",
        dataset_id=dataset_id,
        account_id=account_id,
        external_id=external_id,
        name=f"Campaign {external_id}",
        status="active",
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        budget_id=budget_id,
        daily_budget=None if own_micros is None else Decimal(own_micros) / Decimal(1_000_000),
        budget_period="DAILY",
    )
    session.add(row)
    await session.flush()
    return row.id


async def _google_world(sessions: Sessions) -> GoogleWorld:
    """Commit an active Google dataset with its account, three budgets and three campaigns."""
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session, "google")
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
        own_budget = await _budget(session, dataset_id, "501", OWN_MICROS, shared=False)
        shared_budget = await _budget(session, dataset_id, "502", SHARED_MICROS, shared=True)
        other_shared = await _budget(session, dataset_id, "503", 70_000_000, shared=True)
        own = await _google_campaign(
            session, dataset_id, account.id, "11", own_budget, own_micros=OWN_MICROS
        )
        member_a = await _google_campaign(session, dataset_id, account.id, "12", shared_budget)
        member_b = await _google_campaign(session, dataset_id, account.id, "13", shared_budget)
        return GoogleWorld(
            dataset_id=dataset_id,
            account_id=account.id,
            own_budget=own_budget,
            shared_budget=shared_budget,
            other_shared=other_shared,
            own=own,
            member_a=member_a,
            member_b=member_b,
        )


def _google_rows(
    world: GoogleWorld,
    *,
    own_status: str = "active",
    shared_micros: int = SHARED_MICROS,
    without: frozenset[int] = frozenset(),
) -> list[SnapshotRow]:
    """Return one sync's Google snapshot rows for the world's platform state."""
    rows = [
        SnapshotRow.campaign(
            source="google",
            entity_id=world.own,
            status=own_status,
            raw_status="ENABLED" if own_status == "active" else "PAUSED",
            budget_id=world.own_budget,
            budget_period="DAILY",
            budget_amount_micros=OWN_MICROS,
        ),
        *(
            SnapshotRow.campaign(
                source="google",
                entity_id=member,
                status="active",
                raw_status="ENABLED",
                budget_id=world.shared_budget,
                budget_period="DAILY",
                budget_amount_micros=shared_micros,
                explicitly_shared=True,
            )
            for member in (world.member_a, world.member_b)
        ),
        SnapshotRow.budget(
            entity_id=world.own_budget,
            period="DAILY",
            amount_micros=OWN_MICROS,
            explicitly_shared=False,
        ),
        SnapshotRow.budget(
            entity_id=world.shared_budget,
            period="DAILY",
            amount_micros=shared_micros,
            explicitly_shared=True,
        ),
        SnapshotRow.budget(
            entity_id=world.other_shared,
            period="DAILY",
            amount_micros=70_000_000,
            explicitly_shared=True,
        ),
    ]
    return [row for row in rows if row.entity_id not in without or row.entity_type == "budget"]


async def _late_campaign(
    sessions: Sessions, world: GoogleWorld, external_id: str
) -> tuple[int, int]:
    """Commit a campaign on its own new budget, first seen after the baseline.

    A sync whose detection first observes it raises its ``untracked`` discrepancy; a
    snapshot written without detection makes later syncs treat its baseline as deferred.

    Returns:
        The campaign's id and its budget's id.
    """
    async with sessions() as session, session.begin():
        budget = await _budget(
            session, world.dataset_id, f"5{external_id}", LATE_MICROS, shared=False
        )
        campaign = await _google_campaign(
            session, world.dataset_id, world.account_id, external_id, budget, own_micros=LATE_MICROS
        )
    return campaign, budget


def _late_rows(campaign: int, budget: int, status: str = "active") -> list[SnapshotRow]:
    """Return the late campaign's and its own budget's snapshot rows."""
    return [
        SnapshotRow.campaign(
            source="google",
            entity_id=campaign,
            status=status,
            raw_status="ENABLED" if status == "active" else "PAUSED",
            budget_id=budget,
            budget_period="DAILY",
            budget_amount_micros=LATE_MICROS,
        ),
        SnapshotRow.budget(
            entity_id=budget, period="DAILY", amount_micros=LATE_MICROS, explicitly_shared=False
        ),
    ]


@dataclass(frozen=True)
class SyncResult:
    """What one simulated entity sync did."""

    report: DetectionReport
    snapshot_ids: dict[tuple[str, int], int]


async def _sync(
    sessions: Sessions,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    *,
    at: datetime,
    missing_campaigns: frozenset[int] = frozenset(),
) -> SyncResult:
    """Run the snapshot, baseline, detection and freshness steps of one complete sync."""
    async with sessions() as session, session.begin():
        sealed = await baseline_sealed(session, dataset_id)
        ids = await write_snapshots(session, dataset_id, rows, captured_at=at)
        await ensure_baseline(session, dataset_id, captured_at=at, sealed=sealed)
        enumeration = EnumerationResult(
            source="google",
            complete=True,
            present_campaign_ids=frozenset(
                row.entity_id for row in rows if row.entity_type == "campaign"
            ),
            present_budget_ids=frozenset(
                row.entity_id for row in rows if row.entity_type == "budget"
            ),
            missing_campaign_ids=missing_campaigns,
        )
        report = await detect_discrepancies(
            session, dataset_id, enumeration, ids, sealed=sealed, now=at
        )
        await record_freshness(
            session,
            dataset_id,
            "google.entities",
            FreshnessOutcome.success(at, covered_from=None, covered_through=None, complete=True),
        )
    return SyncResult(report=report, snapshot_ids=ids)


async def _requests(session: AsyncSession) -> Counter[str]:
    """Return the number of ``analysis_request`` rows per reason."""
    result = await session.execute(
        select(AnalysisRequest.reason, func.count()).group_by(AnalysisRequest.reason)
    )
    counts: Counter[str] = Counter()
    for reason, total in result:
        counts[reason] = total
    return counts


type StoredRow = tuple[Any, ...]


async def _stored(sessions: Sessions, dataset_id: int) -> tuple[list[StoredRow], Counter[str]]:
    """Return every discrepancy of the dataset, as committed, and the queued requests."""
    async with sessions() as session:
        result = await session.scalars(
            select(Discrepancy).where(Discrepancy.dataset_id == dataset_id).order_by(Discrepancy.id)
        )
        rows = [
            (
                row.id,
                row.subject_type,
                row.subject_id,
                row.kind,
                row.state,
                row.resolution,
                row.snapshot_id,
                row.decision_id,
                row.decided_value,
                row.platform_value,
                row.detected_at,
            )
            for row in result
        ]
        return rows, await _requests(session)


@dataclass(frozen=True)
class Links:
    """A sealed Google world with an untracked late campaign, and the rows a comparison reads.

    Attributes:
        world: The Google world, baselined at ``_at(0)`` and re-synced at ``_at(1)``.
        late: A campaign first seen after the seal, with a snapshot and no decision.
        snapshots: The newest snapshot of every entity, by ``(entity_type, entity_id)``.
        first_snapshots: The baseline sync's snapshots, older than ``snapshots``.
        decisions: The current decision of every tracked subject.
    """

    world: GoogleWorld
    late: int
    snapshots: Mapping[tuple[str, int], int]
    first_snapshots: Mapping[tuple[str, int], int]
    decisions: Mapping[SubjectRef, int]

    def snapshot(self, ref: SubjectRef) -> int:
        """Return the newest snapshot of ``ref``."""
        return self.snapshots[(ref.type, ref.id)]

    def decision(self, ref: SubjectRef) -> int:
        """Return the current decision of ``ref``."""
        return self.decisions[ref]


def _campaign(campaign_id: int) -> SubjectRef:
    """Return the campaign subject ``campaign_id``."""
    return SubjectRef(type="campaign", id=campaign_id)


def _shared(budget_id: int) -> SubjectRef:
    """Return the shared-budget subject ``budget_id``."""
    return SubjectRef(type="budget", id=budget_id)


async def _links(sessions: Sessions) -> Links:
    """Build a baselined, sealed world in which nothing is open, plus an untracked campaign.

    The late campaign's snapshot is written without a detection pass, so no ``untracked``
    row exists for it yet.
    """
    world = await _google_world(sessions)
    first = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(0))
    second = await _sync(sessions, world.dataset_id, _google_rows(world), at=_at(1))
    assert (first.report.total_raised, second.report.total_raised) == (0, 0)
    late, late_budget = await _late_campaign(sessions, world, "17")
    async with sessions() as session, session.begin():
        late_ids = await write_snapshots(
            session, world.dataset_id, _late_rows(late, late_budget), captured_at=_at(2)
        )
    tracked = [
        _campaign(world.own),
        _campaign(world.member_a),
        _campaign(world.member_b),
        _shared(world.shared_budget),
        _shared(world.other_shared),
    ]
    async with sessions() as session:
        loaded = await load_subject_states(session, tracked)
        decisions: dict[SubjectRef, int] = {}
        for ref in tracked:
            decision = loaded.require(ref).current_decision
            assert decision is not None
            decisions[ref] = decision.id
    rows, _ = await _stored(sessions, world.dataset_id)
    assert rows == []
    return Links(
        world=world,
        late=late,
        snapshots={**second.snapshot_ids, **late_ids},
        first_snapshots=first.snapshot_ids,
        decisions=decisions,
    )


@dataclass(frozen=True)
class Contrived:
    """Rows that name the own-budget campaign but belong elsewhere.

    Attributes:
        foreign_snapshot: A snapshot of the campaign's id in another Google dataset.
        foreign_decision: A decision of the campaign's id in another Google dataset.
        linkedin_snapshot: A snapshot of the campaign in its own dataset, labelled LinkedIn.
        absent: An id no snapshot or decision has.
    """

    foreign_snapshot: int
    foreign_decision: int
    linkedin_snapshot: int
    absent: int


def _contrived_snapshot(source: str, dataset_id: int, campaign_id: int) -> EntitySnapshot:
    """Return an own-budget campaign snapshot observed before every row of the world."""
    return EntitySnapshot(
        source=source,
        dataset_id=dataset_id,
        entity_type="campaign",
        entity_id=campaign_id,
        captured_at=BEFORE_T0,
        status="active",
        budget_daily=Decimal(100),
        budget_amount_micros=OWN_MICROS,
        payload={},
    )


async def _contrived(sessions: Sessions, links: Links) -> Contrived:
    """Commit the rows of :class:`Contrived`, dated before the world so none becomes current."""
    own = links.world.own
    async with sessions() as session, session.begin():
        foreign = await _dataset(session, "google", state="building")
        foreign_snapshot = _contrived_snapshot("google", foreign, own)
        linkedin_snapshot = _contrived_snapshot("linkedin", links.world.dataset_id, own)
        foreign_decision = Decision(
            source="google",
            dataset_id=foreign,
            subject_type="campaign",
            subject_id=own,
            shape="own_budget",
            status="active",
            daily_budget=Decimal(100),
            tombstone=False,
            decided_at=BEFORE_T0,
            origin="baseline",
            patch={},
        )
        session.add_all([foreign_snapshot, linkedin_snapshot, foreign_decision])
        await session.flush()
        highest = max(
            await session.scalar(select(func.max(EntitySnapshot.id))) or 0,
            await session.scalar(select(func.max(Decision.id))) or 0,
        )
        return Contrived(
            foreign_snapshot=foreign_snapshot.id,
            foreign_decision=foreign_decision.id,
            linkedin_snapshot=linkedin_snapshot.id,
            absent=highest + 1_000,
        )


def _values(kind: DiscrepancyKind) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return a decided and a platform side of the shape ``kind`` stores."""
    if kind == "status":
        return {"status": "active"}, {"status": "paused"}
    if kind == "budget":
        return {"daily_budget": "50.000000"}, {"daily_budget": "60.000000"}
    if kind == "untracked":
        return None, {"status": "active", "daily_budget": "80.000000"}
    return {"status": "active"}, None


async def _raise(
    session: AsyncSession,
    ref: SubjectRef,
    kind: DiscrepancyKind,
    *,
    snapshot_id: int | None,
    decision_id: int | None,
    locked: bool,
    at: datetime,
    unresolved: Sequence[Discrepancy] | None = None,
) -> Discrepancy:
    """Raise ``kind`` on ``ref`` as detection does (``locked``) or as a standalone caller.

    Locked, the subject is loaded with :func:`load_subject_states` and ``lock`` first and its
    state is passed with ``locked=True``; otherwise nothing is preloaded.
    """
    state: SubjectState | None = None
    if locked:
        state = (await load_subject_states(session, [ref], lock=True)).require(ref)
    decided, platform = _values(kind)
    return await raise_discrepancy(
        session,
        ref,
        kind,
        decided_value=decided,
        platform_value=platform,
        snapshot_id=snapshot_id,
        decision_id=decision_id,
        detected_at=at,
        state=state,
        locked=locked,
        unresolved=unresolved,
    )


async def _refused(
    sessions: Sessions,
    dataset_id: int,
    ref: SubjectRef,
    kind: DiscrepancyKind,
    *,
    snapshot_id: int | None,
    decision_id: int | None,
    locked: bool,
    match: str,
    unresolved_empty: bool = False,
) -> None:
    """Assert that the raise is refused with ``ValueError`` and changes nothing.

    The refused call's transaction is committed, so anything it changed in the session would
    be written; the dataset's discrepancies and the queued requests must be as before.
    ``unresolved_empty`` passes ``unresolved=[]``, as a caller that read before a concurrent
    writer inserted the subject's row would, which sends the raise down the
    ``IntegrityError`` fallback when a row of the kind exists.
    """
    before = await _stored(sessions, dataset_id)
    async with sessions() as session, session.begin():
        with pytest.raises(ValueError, match=match):
            await _raise(
                session,
                ref,
                kind,
                snapshot_id=snapshot_id,
                decision_id=decision_id,
                locked=locked,
                at=_at(5),
                unresolved=[] if unresolved_empty else None,
            )
    assert await _stored(sessions, dataset_id) == before


# ---------------------------------------------------------------------------------------
# The snapshot link
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("kind", ["status", "budget", "untracked"])
async def test_a_comparison_kind_without_its_snapshot_is_refused(
    sessions: Sessions, kind: DiscrepancyKind, *, locked: bool
) -> None:
    links = await _links(sessions)
    world = links.world
    subject = {
        "status": _campaign(world.own),
        "budget": _shared(world.shared_budget),
        "untracked": _campaign(links.late),
    }[kind]
    decision_id = None if kind == "untracked" else links.decision(subject)
    await _refused(
        sessions,
        world.dataset_id,
        subject,
        kind,
        snapshot_id=None,
        decision_id=decision_id,
        locked=locked,
        match=f"a {kind} discrepancy needs the snapshot it compared",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_a_missing_discrepancy_with_a_snapshot_is_refused(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    member = _campaign(links.world.member_a)
    await _refused(
        sessions,
        links.world.dataset_id,
        member,
        "missing",
        snapshot_id=links.snapshot(member),
        decision_id=links.decision(member),
        locked=locked,
        match="a missing discrepancy has no snapshot",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_a_snapshot_of_anything_but_the_subject_is_refused(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    contrived = await _contrived(sessions, links)
    world = links.world
    own = _campaign(world.own)
    shared = _shared(world.shared_budget)
    cases: list[tuple[SubjectRef, DiscrepancyKind, int, str]] = [
        # Another campaign of the same dataset passes the dataset-qualified foreign key.
        (own, "status", links.snapshot(_campaign(world.member_a)), "describes campaign"),
        (shared, "budget", links.snapshot(own), "describes campaign"),
        (_campaign(links.late), "untracked", links.snapshot(own), "describes campaign"),
        (own, "budget", links.snapshot(shared), "describes budget"),
        (own, "status", contrived.foreign_snapshot, "belongs to google dataset"),
        (own, "status", contrived.linkedin_snapshot, "belongs to linkedin dataset"),
        (own, "status", contrived.absent, f"snapshot {contrived.absent} does not exist"),
    ]
    for ref, kind, snapshot_id, match in cases:
        await _refused(
            sessions,
            world.dataset_id,
            ref,
            kind,
            snapshot_id=snapshot_id,
            decision_id=None if kind == "untracked" else links.decision(ref),
            locked=locked,
            match=match,
        )


# ---------------------------------------------------------------------------------------
# The decision link
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_an_untracked_discrepancy_with_a_decision_is_refused(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    own = _campaign(links.world.own)
    late = _campaign(links.late)
    for ref, decision_id in ((own, links.decision(own)), (late, links.decision(own))):
        await _refused(
            sessions,
            links.world.dataset_id,
            ref,
            "untracked",
            snapshot_id=links.snapshot(ref),
            decision_id=decision_id,
            locked=locked,
            match="an untracked discrepancy has no decision",
        )


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("kind", ["status", "budget", "missing"])
async def test_a_tracked_kind_without_its_decision_is_refused(
    sessions: Sessions, kind: DiscrepancyKind, *, locked: bool
) -> None:
    links = await _links(sessions)
    world = links.world
    subject = {
        "status": _campaign(world.own),
        "budget": _shared(world.shared_budget),
        "missing": _campaign(world.member_a),
    }[kind]
    await _refused(
        sessions,
        world.dataset_id,
        subject,
        kind,
        snapshot_id=None if kind == "missing" else links.snapshot(subject),
        decision_id=None,
        locked=locked,
        match=f"a {kind} discrepancy needs the decision it compared",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_a_decision_of_anything_but_the_subject_is_refused(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    contrived = await _contrived(sessions, links)
    world = links.world
    own = _campaign(world.own)
    member = _campaign(world.member_a)
    shared = _shared(world.shared_budget)
    cases: list[tuple[SubjectRef, DiscrepancyKind, int, str]] = [
        (
            own,
            "status",
            links.decision(member),
            f"belongs to campaign {world.member_a}, not campaign {world.own}",
        ),
        (
            shared,
            "budget",
            links.decision(_shared(world.other_shared)),
            f"belongs to budget {world.other_shared}, not budget {world.shared_budget}",
        ),
        (
            member,
            "missing",
            links.decision(own),
            f"belongs to campaign {world.own}, not campaign {world.member_a}",
        ),
        (
            own,
            "budget",
            links.decision(shared),
            f"belongs to budget {world.shared_budget}, not campaign {world.own}",
        ),
        (own, "status", contrived.foreign_decision, "belongs to google dataset"),
        (own, "missing", contrived.absent, f"decision {contrived.absent} does not exist"),
    ]
    for ref, kind, decision_id, match in cases:
        await _refused(
            sessions,
            world.dataset_id,
            ref,
            kind,
            snapshot_id=None if kind == "missing" else links.snapshot(ref),
            decision_id=decision_id,
            locked=locked,
            match=match,
        )


# ---------------------------------------------------------------------------------------
# The refresh paths
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_an_open_row_of_the_kind_is_not_refreshed_with_bad_links(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    world = links.world
    own = _campaign(world.own)
    shared = _shared(world.shared_budget)
    changed = await _sync(
        sessions,
        world.dataset_id,
        _google_rows(world, own_status="paused", shared_micros=SHARED_MICROS + 1),
        at=_at(3),
    )
    assert (changed.report.raised["status"], changed.report.raised["budget"]) == (1, 1)
    snapshot_of = changed.snapshot_ids
    member = _campaign(world.member_a)
    cases: list[tuple[SubjectRef, DiscrepancyKind, int | None, int | None, str]] = [
        (own, "status", None, links.decision(own), "needs the snapshot"),
        (
            own,
            "status",
            snapshot_of[("campaign", world.member_a)],
            links.decision(own),
            "describes",
        ),
        (own, "status", snapshot_of[("campaign", world.own)], None, "needs the decision"),
        (own, "status", snapshot_of[("campaign", world.own)], links.decision(member), "belongs"),
        (shared, "budget", None, links.decision(shared), "needs the snapshot"),
        (
            shared,
            "budget",
            snapshot_of[("budget", world.shared_budget)],
            None,
            "needs the decision",
        ),
    ]
    for ref, kind, snapshot_id, decision_id, match in cases:
        # Through the subject's open row, and through the fallback that refreshes the row a
        # concurrent writer inserted first (the caller's preloaded rows predate it).
        for unresolved_empty in (False, True):
            await _refused(
                sessions,
                world.dataset_id,
                ref,
                kind,
                snapshot_id=snapshot_id,
                decision_id=decision_id,
                locked=locked,
                match=match,
                unresolved_empty=unresolved_empty,
            )

    # Valid links still refresh the row, also through the concurrent-insert fallback.
    rows, _ = await _stored(sessions, world.dataset_id)
    (status_id,) = [row[0] for row in rows if row[3] == "status"]
    for snapshot_id, unresolved in (
        (links.first_snapshots[("campaign", world.own)], None),
        (snapshot_of[("campaign", world.own)], []),
    ):
        async with sessions() as session, session.begin():
            refreshed = await _raise(
                session,
                own,
                "status",
                snapshot_id=snapshot_id,
                decision_id=links.decision(own),
                locked=locked,
                at=_at(5),
                unresolved=unresolved,
            )
            assert (refreshed.id, refreshed.snapshot_id, refreshed.decision_id) == (
                status_id,
                snapshot_id,
                links.decision(own),
            )
            assert (refreshed.state, refreshed.detected_at) == ("open", _at(3))


@dataclass(frozen=True)
class Waiting:
    """A campaign whose ``untracked`` row waits after ``record_intended``.

    Attributes:
        ref: The campaign, first observed by the sync that raised its ``untracked`` row.
        discrepancy_id: The waiting row.
        seen: The snapshot the ``untracked`` row first compared.
        latest: The newer snapshot a later sync folded into the waiting row.
        intended: The decision ``record_intended`` wrote.
    """

    ref: SubjectRef
    discrepancy_id: int
    seen: int
    latest: int
    intended: int


async def _waiting(sessions: Sessions, world: GoogleWorld) -> Waiting:
    """Raise a new campaign's ``untracked`` row, wait on it, then fold a mismatch into it."""
    fresh, fresh_budget = await _late_campaign(sessions, world, "18")
    rows = [*_google_rows(world), *_late_rows(fresh, fresh_budget)]
    seen = await _sync(sessions, world.dataset_id, rows, at=_at(3))
    assert seen.report.raised["untracked"] == 1
    stored, _ = await _stored(sessions, world.dataset_id)
    (untracked_id,) = [row[0] for row in stored if row[3] == "untracked"]
    async with sessions() as session, session.begin():
        waiting = await resolve_discrepancy(
            untracked_id,
            "record_intended",
            {"status": "paused", "daily_budget": "80"},
            session=session,
            now=_at(4),
        )
    assert (waiting.state, waiting.resolution) == ("awaiting_platform", "record_intended")
    intended = waiting.decision_id
    assert intended is not None
    # The platform still shows the campaign active: the mismatch folds into the waiting row.
    again = await _sync(sessions, world.dataset_id, rows, at=_at(5))
    assert (again.report.total_raised, again.report.refreshed) == (0, 1)
    latest = again.snapshot_ids[("campaign", fresh)]
    stored, _ = await _stored(sessions, world.dataset_id)
    assert [(row[0], row[4], row[6], row[7]) for row in stored] == [
        (untracked_id, "awaiting_platform", latest, intended)
    ]
    return Waiting(
        ref=_campaign(fresh),
        discrepancy_id=untracked_id,
        seen=seen.snapshot_ids[("campaign", fresh)],
        latest=latest,
        intended=intended,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_the_row_waiting_after_record_intended_is_not_refreshed_with_bad_links(
    sessions: Sessions, *, locked: bool
) -> None:
    links = await _links(sessions)
    world = links.world
    waiting = await _waiting(sessions, world)
    ref, intended, latest = waiting.ref, waiting.intended, waiting.latest
    own = _campaign(world.own)
    cases: list[tuple[DiscrepancyKind, int | None, int | None, str]] = [
        ("status", None, intended, "a status discrepancy needs the snapshot"),
        ("budget", None, intended, "a budget discrepancy needs the snapshot"),
        ("status", links.snapshot(own), intended, f"describes campaign {world.own}, not"),
        ("status", latest, None, "a status discrepancy needs the decision"),
        (
            "status",
            latest,
            links.decision(own),
            f"belongs to campaign {world.own}, not campaign {ref.id}",
        ),
        (
            "budget",
            latest,
            links.decision(_shared(world.shared_budget)),
            f"belongs to budget {world.shared_budget}, not campaign {ref.id}",
        ),
    ]
    for kind, snapshot_id, decision_id, match in cases:
        await _refused(
            sessions,
            world.dataset_id,
            ref,
            kind,
            snapshot_id=snapshot_id,
            decision_id=decision_id,
            locked=locked,
            match=match,
        )

    # A valid comparison folds into the waiting row, its values describing the whole subject
    # as the named snapshot observed it and the named decision decided it.
    async with sessions() as session, session.begin():
        folded = await _raise(
            session,
            ref,
            "status",
            snapshot_id=waiting.seen,
            decision_id=intended,
            locked=locked,
            at=_at(6),
        )
        assert (folded.id, folded.kind, folded.state, folded.detected_at) == (
            waiting.discrepancy_id,
            "untracked",
            "awaiting_platform",
            _at(3),
        )
        assert (folded.snapshot_id, folded.decision_id) == (waiting.seen, intended)
        assert folded.platform_value is not None
        assert folded.decided_value is not None
        assert (folded.platform_value["status"], folded.decided_value["status"]) == (
            "active",
            "paused",
        )
        assert folded.platform_value["amount_micros"] == LATE_MICROS
        assert folded.decided_value["amount_micros"] == LATE_MICROS


# ---------------------------------------------------------------------------------------
# Valid links
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("locked", [False, True])
async def test_valid_links_open_a_row_of_every_kind(sessions: Sessions, *, locked: bool) -> None:
    links = await _links(sessions)
    world = links.world
    own = _campaign(world.own)
    member = _campaign(world.member_a)
    shared = _shared(world.shared_budget)
    late = _campaign(links.late)
    expected: list[tuple[SubjectRef, DiscrepancyKind, int | None, int | None]] = [
        (own, "status", links.snapshot(own), links.decision(own)),
        # An older snapshot of the subject than the one the state preloaded.
        (own, "budget", links.first_snapshots[("campaign", world.own)], links.decision(own)),
        (shared, "budget", links.snapshot(shared), links.decision(shared)),
        (late, "untracked", links.snapshot(late), None),
        (member, "missing", None, links.decision(member)),
    ]
    async with sessions() as session, session.begin():
        before = await _requests(session)
        for ref, kind, snapshot_id, decision_id in expected:
            row = await _raise(
                session,
                ref,
                kind,
                snapshot_id=snapshot_id,
                decision_id=decision_id,
                locked=locked,
                at=_at(5),
            )
            assert (row.subject_type, row.subject_id, row.kind, row.state) == (
                ref.type,
                ref.id,
                kind,
                "open",
            )
            assert (row.snapshot_id, row.decision_id) == (snapshot_id, decision_id)
            assert (row.source, row.dataset_id, row.detected_at) == (
                "google",
                world.dataset_id,
                _at(5),
            )
        queued = await _requests(session) - before
    assert queued["discrepancy_created"] == len(expected)
    rows, _ = await _stored(sessions, world.dataset_id)
    assert [(row[1], row[2], row[3], row[6], row[7]) for row in rows] == [
        (ref.type, ref.id, kind, snapshot_id, decision_id)
        for ref, kind, snapshot_id, decision_id in expected
    ]


# ---------------------------------------------------------------------------------------
# Detection reads nothing more to check the links
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CountedSync:
    """One entity sync with the statements detection's discrepancy writes sent, by verb."""

    result: SyncResult
    raising: Counter[str]


async def _counted_sync(
    sessions: Sessions,
    monkeypatch: pytest.MonkeyPatch,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    *,
    at: datetime,
    missing_campaigns: frozenset[int] = frozenset(),
) -> CountedSync:
    """Run one complete sync, counting the statements sent inside ``raise_discrepancy``.

    Detection reaches :func:`raise_discrepancy` through :mod:`ads_engine.sync.snapshots`, so
    the name is wrapped there; each statement sent while a call runs is counted under its
    leading SQL verb.
    """
    engine = sessions.kw["bind"]
    inside: list[bool] = [False]
    verbs: Counter[str] = Counter()

    def count(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if inside[0]:
            verbs[statement.lstrip().split(None, 1)[0].upper()] += 1

    async def counted(*args: Any, **kwargs: Any) -> Discrepancy:
        inside[0] = True
        try:
            return await raise_discrepancy(*args, **kwargs)
        finally:
            inside[0] = False

    monkeypatch.setattr(snapshots, "raise_discrepancy", counted)
    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        result = await _sync(sessions, dataset_id, rows, at=at, missing_campaigns=missing_campaigns)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
        monkeypatch.undo()
    return CountedSync(result=result, raising=verbs)


@pytest.mark.anyio
async def test_detection_checks_the_links_on_the_rows_it_preloaded(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    links = await _links(sessions)
    world = links.world
    fresh, fresh_budget = await _late_campaign(sessions, world, "18")
    gone = frozenset({world.member_b})
    rows = [
        *_google_rows(world, own_status="paused", shared_micros=SHARED_MICROS + 1, without=gone),
        *_late_rows(fresh, fresh_budget),
    ]
    raised = await _counted_sync(
        sessions, monkeypatch, world.dataset_id, rows, at=_at(3), missing_campaigns=gone
    )
    assert dict(raised.result.report.raised) == {
        "status": 1,
        "budget": 1,
        "untracked": 1,
        "missing": 1,
    }
    refreshed = await _counted_sync(
        sessions, monkeypatch, world.dataset_id, rows, at=_at(6), missing_campaigns=gone
    )
    assert (refreshed.result.report.total_raised, refreshed.result.report.refreshed) == (0, 4)

    # The new campaign's intended state differs from the platform: its status mismatch
    # folds into the waiting untracked row.
    stored, _ = await _stored(sessions, world.dataset_id)
    (untracked_id,) = [row[0] for row in stored if row[3] == "untracked"]
    async with sessions() as session, session.begin():
        await resolve_discrepancy(
            untracked_id,
            "record_intended",
            {"status": "paused", "daily_budget": "80"},
            session=session,
            now=_at(7),
        )
    folded = await _counted_sync(
        sessions, monkeypatch, world.dataset_id, rows, at=_at(9), missing_campaigns=gone
    )
    assert (folded.result.report.total_raised, folded.result.report.refreshed) == (0, 4)
    stored, _ = await _stored(sessions, world.dataset_id)
    (waiting,) = [row for row in stored if row[3] == "untracked"]
    assert (waiting[4], waiting[6]) == (
        "awaiting_platform",
        folded.result.snapshot_ids[("campaign", fresh)],
    )

    # Every call wrote and none read: the links were checked on the preloaded rows.
    for run in (raised, refreshed, folded):
        assert run.raising["SELECT"] == 0, run.raising
        assert sum(run.raising.values()) > 0
