"""Tests that concurrent entity syncs of one dataset never deadlock in ``sync.snapshots``.

An entity sync runs :func:`~ads_engine.sync.snapshots.ensure_baseline` and then
:func:`~ads_engine.sync.snapshots.detect_discrepancies` in one transaction. Each pass first
locks the dataset ``FOR SHARE`` and then every decision subject of the dataset ``FOR NO KEY
UPDATE`` (campaigns, then explicitly shared budgets, each in ``id`` order), so the second
pass of a sync never waits for a subject lock that another sync holds while that sync waits
for one the first pass took.

Covered:

* **No cross-pass cycle**: sync A started while the baseline was open and baselines a
  campaign that is new to the dataset; sync B started after another sync sealed the
  baseline, so its baseline pass writes nothing and it goes on to detection. Had each pass
  locked only the subjects it judges, A's baseline would hold the new campaign alone, B's
  detection would lock the existing campaigns and wait for the new one, and A's detection
  would wait for an existing one: a cycle PostgreSQL aborts with SQLSTATE ``40P01``. Here B
  waits on A already in its baseline pass, before it holds any subject lock; A detects and
  commits, and B completes against A's committed baseline. Neither is aborted.
* **The whole lock set on every path**: a baseline pass that writes nothing (baseline open
  or sealed) and a detection pass that judges no subject hold the dataset, every campaign
  and every explicitly shared budget until the transaction ends. A budget that is not
  shared is not a decision subject and stays unlocked.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test builds its own active Google
dataset, retiring the previous one. Blocking is observed, never assumed: a waiting sync is
seen in ``pg_stat_activity`` as blocked by the holder's backend (``pg_blocking_pids``), and
a held lock is seen through a ``NOWAIT`` probe that fails with SQLSTATE ``55P03``. An
unreachable database fails the database tests; it never skips them.
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.sync.freshness import REASON_INTERRUPTED, FreshnessOutcome, record_freshness
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

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first entity sync; later instants are whole hours after it."""

OWN_MICROS = 100_000_000
SHARED_MICROS = 50_000_000

WAIT_LIMIT = 30.0
"""Seconds a released sync may take to finish before the test fails."""

DEADLOCK_DETECTED = "40P01"
LOCK_NOT_AVAILABLE = "55P03"

INCOMPLETE = f"{REASON_INTERRUPTED}: page deferred"


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
            "for the snapshot lock-order tests"
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
    schema = f"snapshot_lock_order_test_{uuid.uuid4().hex}"
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
    pytest.fail(f"{count} sync(s) never waited on the locks of backend {blocker_pid}")


async def _blocked_by(sessions: Sessions, blocker_pid: int) -> list[tuple[int, str, str]]:
    """Return ``(pid, wait_event_type, query)`` of every backend ``blocker_pid`` blocks."""
    async with sessions() as session:
        result = await session.execute(
            text(
                "SELECT pid, wait_event_type, query FROM pg_stat_activity "
                "WHERE :pid = ANY(pg_blocking_pids(pid)) ORDER BY pid"
            ),
            {"pid": blocker_pid},
        )
        return [(row.pid, row.wait_event_type, row.query) for row in result]


def _start[T](coroutine: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    """Start a concurrent sync."""
    return asyncio.create_task(coroutine)


def _sqlstate(outcome: object) -> str | None:
    """Return the SQLSTATE of a database error, or ``None`` for anything else."""
    if isinstance(outcome, DBAPIError):
        state = getattr(outcome.orig, "sqlstate", None)
        return state if isinstance(state, str) else None
    return None


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """An active Google dataset whose first entity sync recorded the baseline.

    Attributes:
        dataset_id: The dataset.
        existing: An own-budget campaign with its baseline decision.
        existing_budget: Its budget, not shared, so not a decision subject.
        shared_budget: An explicitly shared budget with its baseline decision.
        member: A campaign drawing from ``shared_budget``, with its baseline decision.
        new: An own-budget campaign added after the first sync, with no decision.
        new_budget: Its budget, not shared.
    """

    dataset_id: int
    existing: int
    existing_budget: int
    shared_budget: int
    member: int
    new: int
    new_budget: int


async def _dataset(session: AsyncSession) -> int:
    """Insert an active Google primary dataset, retiring the previous active one."""
    await session.execute(
        update(SourceDataset)
        .where(
            SourceDataset.source == "google",
            SourceDataset.state == "active",
            SourceDataset.role == "primary",
        )
        .values(state="retired", retired_at=T0)
    )
    dataset = SourceDataset(source="google", mode="simulated", state="active", role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


async def _budget(session: AsyncSession, dataset_id: int, external_id: str, *, shared: bool) -> int:
    """Insert a Google DAILY budget and return its id."""
    micros = SHARED_MICROS if shared else OWN_MICROS
    row = Budget(
        source="google",
        dataset_id=dataset_id,
        external_id=external_id,
        amount=Decimal(micros) / Decimal(1_000_000),
        amount_micros=micros,
        period="DAILY",
        explicitly_shared=shared,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _campaign(
    session: AsyncSession,
    dataset_id: int,
    account_id: int,
    external_id: str,
    budget_id: int,
    *,
    shared: bool,
) -> int:
    """Insert an active Google campaign on a DAILY budget and return its id.

    A campaign on a budget that is not shared carries that budget's amount itself.
    """
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
        daily_budget=None if shared else Decimal(OWN_MICROS) / Decimal(1_000_000),
        budget_period="DAILY",
    )
    session.add(row)
    await session.flush()
    return row.id


def _campaign_row(campaign_id: int, budget_id: int, *, shared: bool) -> SnapshotRow:
    """Return an active Google campaign's snapshot row on its DAILY budget."""
    return SnapshotRow.campaign(
        source="google",
        entity_id=campaign_id,
        status="active",
        raw_status="ENABLED",
        budget_id=budget_id,
        budget_period="DAILY",
        budget_amount_micros=SHARED_MICROS if shared else OWN_MICROS,
        explicitly_shared=shared,
    )


def _budget_row(budget_id: int, *, shared: bool) -> SnapshotRow:
    """Return a Google DAILY budget's snapshot row."""
    return SnapshotRow.budget(
        entity_id=budget_id,
        period="DAILY",
        amount_micros=SHARED_MICROS if shared else OWN_MICROS,
        explicitly_shared=shared,
    )


def _enumeration(rows: Sequence[SnapshotRow], *, complete: bool = True) -> EnumerationResult:
    """Return the enumeration of a sync that returned exactly the entities of ``rows``."""
    return EnumerationResult(
        source="google",
        complete=complete,
        present_campaign_ids=frozenset(r.entity_id for r in rows if r.entity_type == "campaign"),
        present_budget_ids=frozenset(r.entity_id for r in rows if r.entity_type == "budget"),
        reason=None if complete else INCOMPLETE,
    )


async def _world(sessions: Sessions) -> World:
    """Commit the dataset, run its first entity sync, then add a campaign it never saw.

    The first sync's enumeration is incomplete, so its freshness leaves the baseline open.
    """
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session)
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
        account_id = account.id
        existing_budget = await _budget(session, dataset_id, "501", shared=False)
        shared_budget = await _budget(session, dataset_id, "502", shared=True)
        existing = await _campaign(
            session, dataset_id, account_id, "11", existing_budget, shared=False
        )
        member = await _campaign(session, dataset_id, account_id, "12", shared_budget, shared=True)
    first = [
        _campaign_row(existing, existing_budget, shared=False),
        _campaign_row(member, shared_budget, shared=True),
        _budget_row(existing_budget, shared=False),
        _budget_row(shared_budget, shared=True),
    ]
    async with sessions() as session, session.begin():
        sealed = await baseline_sealed(session, dataset_id)
        ids = await write_snapshots(session, dataset_id, first, captured_at=T0)
        baselines = await ensure_baseline(session, dataset_id, captured_at=T0, sealed=sealed)
        report = await detect_discrepancies(
            session, dataset_id, _enumeration(first, complete=False), ids, sealed=sealed, now=T0
        )
        await record_freshness(
            session,
            dataset_id,
            "google.entities",
            FreshnessOutcome.success(
                T0, covered_from=None, covered_through=None, complete=False, reason=INCOMPLETE
            ),
        )
    assert (sealed, baselines, report.total_raised) == (False, 3, 0)
    async with sessions() as session, session.begin():
        new_budget = await _budget(session, dataset_id, "503", shared=False)
        new = await _campaign(session, dataset_id, account_id, "13", new_budget, shared=False)
    return World(
        dataset_id=dataset_id,
        existing=existing,
        existing_budget=existing_budget,
        shared_budget=shared_budget,
        member=member,
        new=new,
        new_budget=new_budget,
    )


def _rows(world: World) -> list[SnapshotRow]:
    """Return a later sync's snapshot rows: every entity, platform state unchanged."""
    return [
        _campaign_row(world.existing, world.existing_budget, shared=False),
        _campaign_row(world.member, world.shared_budget, shared=True),
        _campaign_row(world.new, world.new_budget, shared=False),
        _budget_row(world.existing_budget, shared=False),
        _budget_row(world.shared_budget, shared=True),
        _budget_row(world.new_budget, shared=False),
    ]


async def _seal(sessions: Sessions, dataset_id: int, *, at: datetime) -> None:
    """Commit what another, complete entity sync records last: ``google.entities`` complete."""
    async with sessions() as session, session.begin():
        await record_freshness(
            session,
            dataset_id,
            "google.entities",
            FreshnessOutcome.success(at, covered_from=None, covered_through=None, complete=True),
        )


@dataclass
class Progress:
    """Where a concurrent sync is: its backend pid and the step it has entered."""

    pid: int | None = None
    step: str = "starting"


@dataclass(frozen=True)
class SyncOutcome:
    """What one complete entity sync did."""

    sealed: bool
    baselines: int
    report: DetectionReport


async def _entity_sync(
    sessions: Sessions,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    *,
    at: datetime,
    progress: Progress,
) -> SyncOutcome:
    """Run an entity sync's snapshot, baseline and detection steps in one transaction."""
    async with sessions() as session, session.begin():
        progress.pid = await _backend_pid(session)
        sealed = await baseline_sealed(session, dataset_id)
        progress.step = "snapshots"
        ids = await write_snapshots(session, dataset_id, rows, captured_at=at)
        progress.step = "baseline"
        baselines = await ensure_baseline(session, dataset_id, captured_at=at, sealed=sealed)
        progress.step = "detection"
        report = await detect_discrepancies(
            session, dataset_id, _enumeration(rows), ids, sealed=sealed, now=at
        )
        progress.step = "commit"
    progress.step = "committed"
    return SyncOutcome(sealed=sealed, baselines=baselines, report=report)


async def _detect_and_commit(
    session: AsyncSession,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    snapshot_ids: Mapping[tuple[str, int], int],
    *,
    sealed: bool,
    at: datetime,
) -> DetectionReport:
    """Finish a paused sync: run its detection step, then commit its transaction."""
    report = await detect_discrepancies(
        session, dataset_id, _enumeration(rows), snapshot_ids, sealed=sealed, now=at
    )
    await session.commit()
    return report


def _nothing_raised(report: DetectionReport, *, checked: int) -> None:
    """Assert a detection pass judged ``checked`` subjects and found every one matching."""
    assert dict(report.raised) == {"status": 0, "budget": 0, "untracked": 0, "missing": 0}
    assert (report.refreshed, report.auto_resolved, report.checked, report.skipped) == (
        0,
        0,
        checked,
        0,
    )


# ---------------------------------------------------------------------------------------
# No cross-pass cycle between two entity syncs
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_concurrent_entity_syncs_wait_on_each_other_instead_of_deadlocking(
    sessions: Sessions,
) -> None:
    world = await _world(sessions)
    rows = _rows(world)
    progress = Progress()
    tasks: list[asyncio.Task[Any]] = []
    async with sessions() as holder:
        holder_pid = await _backend_pid(holder)
        sealed = await baseline_sealed(holder, world.dataset_id)
        # Another entity sync of the dataset completes and seals the baseline after this
        # one started, so the next sync starts with the baseline sealed.
        await _seal(sessions, world.dataset_id, at=_at(1))
        snapshot_ids = await write_snapshots(holder, world.dataset_id, rows, captured_at=_at(2))
        baselines = await ensure_baseline(
            holder, world.dataset_id, captured_at=_at(2), sealed=sealed
        )
        tasks.append(
            _start(_entity_sync(sessions, world.dataset_id, rows, at=_at(3), progress=progress))
        )
        try:
            await _wait_until_blocked(sessions, holder_pid)
            blocked = await _blocked_by(sessions, holder_pid)
            blocked_step = progress.step
            tasks.append(
                _start(
                    _detect_and_commit(
                        holder, world.dataset_id, rows, snapshot_ids, sealed=sealed, at=_at(2)
                    )
                )
            )
        finally:
            if len(tasks) == 1:
                await holder.rollback()
            settled = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=WAIT_LIMIT
            )
            await holder.rollback()

    deadlocked = [outcome for outcome in settled if _sqlstate(outcome) == DEADLOCK_DETECTED]
    assert not deadlocked, f"the concurrent entity syncs deadlocked: {deadlocked!r}"
    other, holder_report = settled
    assert isinstance(other, SyncOutcome), other
    assert isinstance(holder_report, DetectionReport), holder_report
    # The later sync waited on the holder in its baseline pass, on a subject row lock,
    # before it held any subject lock the holder's detection would need.
    assert blocked_step == "baseline"
    ((blocked_pid, wait_type, query),) = blocked
    assert (blocked_pid, wait_type) == (progress.pid, "Lock")
    assert "FOR NO KEY UPDATE" in query
    # The holder started with the baseline open and baselined the new campaign.
    assert (sealed, baselines) == (False, 1)
    _nothing_raised(holder_report, checked=4)
    # The later sync started sealed and found the holder's committed baseline.
    assert (other.sealed, other.baselines) == (True, 0)
    _nothing_raised(other.report, checked=4)
    async with sessions() as session:
        decisions = (
            await session.execute(
                select(Decision.subject_type, Decision.subject_id, Decision.decided_at)
                .where(Decision.dataset_id == world.dataset_id, Decision.origin == "baseline")
                .order_by(Decision.subject_type, Decision.subject_id)
            )
        ).all()
        unresolved = await session.scalar(
            select(Discrepancy.id)
            .where(Discrepancy.dataset_id == world.dataset_id, Discrepancy.state != "resolved")
            .limit(1)
        )
    assert [tuple(row) for row in decisions] == [
        ("budget", world.shared_budget, T0),
        ("campaign", world.existing, T0),
        ("campaign", world.member, T0),
        ("campaign", world.new, _at(2)),
    ]
    assert unresolved is None


# ---------------------------------------------------------------------------------------
# The whole lock set, on every path of both passes
# ---------------------------------------------------------------------------------------


async def _probe(sessions: Sessions, world: World) -> dict[str, bool]:
    """Return, per row of the world, whether another transaction finds it locked.

    Each row is probed in a transaction of its own with ``FOR NO KEY UPDATE NOWAIT``, which
    conflicts with the dataset's ``FOR SHARE`` and a subject's ``FOR NO KEY UPDATE`` but
    not with the ``FOR KEY SHARE`` of a foreign-key check.

    Raises:
        DBAPIError: If a probe fails for any reason other than a held lock.
    """
    targets: dict[str, tuple[type[SourceDataset] | type[Campaign] | type[Budget], int]] = {
        "dataset": (SourceDataset, world.dataset_id),
        "existing": (Campaign, world.existing),
        "member": (Campaign, world.member),
        "new": (Campaign, world.new),
        "shared_budget": (Budget, world.shared_budget),
        "existing_budget": (Budget, world.existing_budget),
        "new_budget": (Budget, world.new_budget),
    }
    locked: dict[str, bool] = {}
    for name, (model, row_id) in targets.items():
        async with sessions() as session:
            try:
                await session.scalar(
                    select(model.id)
                    .where(model.id == row_id)
                    .with_for_update(key_share=True, nowait=True)
                )
            except DBAPIError as exc:
                if _sqlstate(exc) != LOCK_NOT_AVAILABLE:
                    raise
                locked[name] = True
            else:
                locked[name] = False
            finally:
                await session.rollback()
    return locked


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("step", ["baseline_open", "baseline_sealed", "detection"])
async def test_each_pass_holds_the_dataset_and_every_subject_until_commit(
    sessions: Sessions, step: str
) -> None:
    world = await _world(sessions)
    async with sessions() as holder:
        if step == "detection":
            report = await detect_discrepancies(
                holder,
                world.dataset_id,
                EnumerationResult(source="google", complete=False, reason=INCOMPLETE),
                {},
                sealed=True,
                now=_at(2),
            )
            assert (report.checked, report.total_raised) == (0, 0)
        else:
            written = await ensure_baseline(
                holder, world.dataset_id, captured_at=_at(2), sealed=step == "baseline_sealed"
            )
            assert written == 0
        try:
            held = await _probe(sessions, world)
        finally:
            await holder.rollback()
    assert held == {
        "dataset": True,
        "existing": True,
        "member": True,
        "new": True,
        "shared_budget": True,
        "existing_budget": False,
        "new_budget": False,
    }
    assert not any((await _probe(sessions, world)).values())
