"""Tests for C0 resolutions that adopt the platform state, through the real detection path.

Covered (AAP 0.5.4.4 "C0 resolutions by kind and subject type", AAP 0.5.2 shapes):

* **Shape transitions**: a campaign that moved onto or off an explicitly shared budget
  while its decided status differs from the platform's. ``platform_correct`` on the
  ``status`` discrepancy records the platform status in the new shape and closes it, and
  the next entity sync raises no new ``status`` discrepancy; on the ``budget``
  discrepancy it records the new assignment or amount with the decided status.
* **An own-budget amount Google omitted**: ``adopt_platform`` refuses it with 409
  ``platform_state_unknown`` and writes nothing until a sync reports the amount, while a
  ``status`` ``platform_correct`` adopts only the platform status and keeps the decided
  amount.
* **An own budget whose period changed**: a patch over a decided daily budget of a
  campaign whose budget is now ``CUSTOM_PERIOD`` is refused with 422 ``budget_changed``
  and writes nothing; resolving the budget discrepancy makes it decidable again.
* **An intended state's amounts**: ``record_intended`` on an untracked own-budget campaign
  and an untracked shared budget stores the decided amounts at the ``numeric(18, 6)``
  scale of the platform side, whatever exponent the submitted text had, and keeps that
  text through the next sync's refresh until the platform matches.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. Each test builds its own active Google
dataset, retiring the previous one. An unreachable database fails the database tests; it
never skips them.
"""

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.decisions import (
    DecisionPatch,
    PatchRejected,
    PlatformStateUnknown,
    Resolution,
    SubjectRef,
    record_decision,
    resolve_discrepancy,
)
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
type Step = Callable[[AsyncSession], Awaitable[None]]

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first entity sync; later instants are whole hours after it."""

RAW_STATUS = {"active": "ENABLED", "paused": "PAUSED", "ended": "REMOVED"}
"""The Google status each normalised status is observed as."""


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
            "for the decision resolution tests"
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
    schema = f"resolutions_test_{uuid.uuid4().hex}"
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


@dataclass(frozen=True)
class Funding:
    """A Google ``campaign_budget`` as one entity sync observes it."""

    id: int
    period: str
    micros: int
    shared: bool


@dataclass(frozen=True)
class GoogleWorld:
    """An active Google dataset: an own-budget campaign and a member of a shared budget.

    ``own_budget`` (A) and ``spare_budget`` are not explicitly shared; ``shared_budget``
    (B) is, so it is a decision subject of its own.
    """

    dataset_id: int
    account_id: int
    own_budget: Funding
    shared_budget: Funding
    spare_budget: Funding
    own: int
    member: int

    @property
    def budgets(self) -> tuple[Funding, ...]:
        """Every budget of the world as created."""
        return (self.own_budget, self.shared_budget, self.spare_budget)


async def _budget(
    session: AsyncSession, dataset_id: int, external_id: str, micros: int, *, shared: bool
) -> Funding:
    """Insert a Google DAILY budget and return it as observed."""
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
    return Funding(id=row.id, period="DAILY", micros=micros, shared=shared)


async def _campaign(
    session: AsyncSession, dataset_id: int, account_id: int, external_id: str, budget: Funding
) -> int:
    """Insert an active Google campaign drawing from ``budget`` and return its id."""
    row = Campaign(
        source="google",
        dataset_id=dataset_id,
        account_id=account_id,
        external_id=external_id,
        name=f"Campaign {external_id}",
        status="active",
        raw_status="ENABLED",
        objective_or_channel="SEARCH",
        budget_id=budget.id,
        daily_budget=None if budget.shared else Decimal(budget.micros) / Decimal(1_000_000),
        budget_period=budget.period,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _google_world(sessions: Sessions) -> GoogleWorld:
    """Commit an active Google dataset with its account, three budgets and two campaigns."""
    async with sessions() as session, session.begin():
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
        account = AdAccount(
            source="google",
            dataset_id=dataset.id,
            external_id="1234567890",
            name="Acme",
            currency="USD",
            timezone="America/New_York",
        )
        session.add(account)
        await session.flush()
        own_budget = await _budget(session, dataset.id, "501", 100_000_000, shared=False)
        shared_budget = await _budget(session, dataset.id, "502", 50_000_000, shared=True)
        spare_budget = await _budget(session, dataset.id, "504", 80_000_000, shared=False)
        own = await _campaign(session, dataset.id, account.id, "11", own_budget)
        member = await _campaign(session, dataset.id, account.id, "12", shared_budget)
        return GoogleWorld(
            dataset_id=dataset.id,
            account_id=account.id,
            own_budget=own_budget,
            shared_budget=shared_budget,
            spare_budget=spare_budget,
            own=own,
            member=member,
        )


def _rows(
    campaigns: Mapping[int, tuple[str, Funding]],
    budgets: Sequence[Funding],
    *,
    unreported: frozenset[int] = frozenset(),
) -> list[SnapshotRow]:
    """Return one sync's snapshot rows: each campaign's status and budget, then each budget.

    Google omits the amount of every budget whose id is in ``unreported``, on the budget's
    own row and on the row of each campaign drawing from it.
    """
    rows = [
        SnapshotRow.campaign(
            source="google",
            entity_id=campaign_id,
            status=status,
            raw_status=RAW_STATUS[status],
            budget_id=funding.id,
            budget_period=funding.period,
            budget_amount_micros=None if funding.id in unreported else funding.micros,
            explicitly_shared=funding.shared,
        )
        for campaign_id, (status, funding) in sorted(campaigns.items())
    ]
    rows.extend(
        SnapshotRow.budget(
            entity_id=funding.id,
            period=funding.period,
            amount_micros=None if funding.id in unreported else funding.micros,
            explicitly_shared=funding.shared,
        )
        for funding in budgets
    )
    return rows


@dataclass(frozen=True)
class SyncResult:
    """What one entity sync detected, and the snapshot it wrote of each entity."""

    report: DetectionReport
    snapshot_ids: dict[tuple[str, int], int]


async def _sync(
    sessions: Sessions,
    world: GoogleWorld,
    campaigns: Mapping[int, tuple[str, Funding]],
    *,
    at: datetime,
    budgets: Sequence[Funding] | None = None,
    before_writes: Step | None = None,
    detected_at: datetime | None = None,
    unreported: frozenset[int] = frozenset(),
) -> SyncResult:
    """Run one complete entity sync that read the platform at ``at``.

    ``before_writes`` runs in the sync's transaction before its snapshots are written: a
    decision recorded while the sync was reading the platform, and the sync's upsert of
    the entity rows. Detection runs at ``detected_at`` (default ``at``). Google omits the
    amount of every budget in ``unreported`` (budget ids).
    """
    async with sessions() as session, session.begin():
        sealed = await baseline_sealed(session, world.dataset_id)
        if before_writes is not None:
            await before_writes(session)
        observed = world.budgets if budgets is None else budgets
        rows = _rows(campaigns, observed, unreported=unreported)
        ids = await write_snapshots(session, world.dataset_id, rows, captured_at=at)
        await ensure_baseline(session, world.dataset_id, captured_at=at, sealed=sealed)
        enumeration = EnumerationResult(
            source="google",
            complete=True,
            present_campaign_ids=frozenset(campaigns),
            present_budget_ids=frozenset(funding.id for funding in observed),
        )
        report = await detect_discrepancies(
            session,
            world.dataset_id,
            enumeration,
            ids,
            sealed=sealed,
            now=at if detected_at is None else detected_at,
        )
        await record_freshness(
            session,
            world.dataset_id,
            "google.entities",
            FreshnessOutcome.success(at, covered_from=None, covered_through=None, complete=True),
        )
    return SyncResult(report=report, snapshot_ids=ids)


def _steps(*steps: Step) -> Step:
    """Return one step running ``steps`` in order in the same transaction."""

    async def step(session: AsyncSession) -> None:
        for each in steps:
            await each(session)

    return step


def _upsert_budget(funding: Funding) -> Step:
    """Return the entity upsert that gives a budget row ``funding``'s period and amount."""

    async def step(session: AsyncSession) -> None:
        await session.execute(
            update(Budget)
            .where(Budget.id == funding.id)
            .values(
                period=funding.period,
                amount_micros=funding.micros,
                amount=Decimal(funding.micros) / Decimal(1_000_000),
            )
        )

    return step


def _move(campaign_id: int, funding: Funding) -> Step:
    """Return the entity upsert that moves a campaign onto ``funding``."""

    async def step(session: AsyncSession) -> None:
        await session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(
                budget_id=funding.id,
                budget_period=funding.period,
                daily_budget=None
                if funding.shared or funding.period != "DAILY"
                else Decimal(funding.micros) / Decimal(1_000_000),
                total_budget=None
                if funding.shared or funding.period != "CUSTOM_PERIOD"
                else Decimal(funding.micros) / Decimal(1_000_000),
            )
        )

    return step


async def _decide(sessions: Sessions, ref: SubjectRef, patch: DecisionPatch, at: datetime) -> None:
    """Record a user decision in a transaction of its own."""
    async with sessions() as session, session.begin():
        await record_decision(
            ref, patch, "user", session=session, decided_by="ops@example.com", now=at
        )


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


async def _platform_correct(sessions: Sessions, discrepancy_id: int, at: datetime) -> Discrepancy:
    """Resolve a discrepancy with ``platform_correct`` and commit."""
    async with sessions() as session, session.begin():
        return await resolve_discrepancy(
            discrepancy_id,
            "platform_correct",
            session=session,
            decided_by="ops@example.com",
            now=at,
        )


def _placements(world: GoogleWorld) -> dict[int, tuple[str, Funding]]:
    """Return the platform state the world was created in: both campaigns active."""
    return {world.own: ("active", world.own_budget), world.member: ("active", world.shared_budget)}


# ---------------------------------------------------------------------------------------
# Shape transitions (A2-F01, BE2-F02)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """A campaign whose decided status is paused, moved to another budget while active."""

    campaign: int
    target: Funding
    shape: str


def _transition(world: GoogleWorld, direction: str) -> Transition:
    """Return the move of one direction: onto the shared budget, or off it."""
    if direction == "onto-shared":
        return Transition(campaign=world.own, target=world.shared_budget, shape="shared_member")
    return Transition(campaign=world.member, target=world.spare_budget, shape="own_budget")


async def _moved_while_paused(
    sessions: Sessions, direction: str
) -> tuple[GoogleWorld, Transition, dict[int, tuple[str, Funding]]]:
    """Baseline the world, decide the campaign paused, then sync it active on its new budget.

    Returns the world, the transition and the platform placements of the later syncs; the
    campaign's ``status`` and ``budget`` discrepancies are open.
    """
    world = await _google_world(sessions)
    await _sync(sessions, world, _placements(world), at=_at(0))
    move = _transition(world, direction)
    await _decide(
        sessions,
        SubjectRef(type="campaign", id=move.campaign),
        DecisionPatch(status="paused"),
        _at(1),
    )
    placements = {**_placements(world), move.campaign: ("active", move.target)}
    moved = await _sync(
        sessions, world, placements, at=_at(3), before_writes=_move(move.campaign, move.target)
    )
    assert dict(moved.report.raised) == {"status": 1, "budget": 1, "untracked": 0, "missing": 0}
    assert set(await _open(sessions, move.campaign)) == {"status", "budget"}
    return world, move, placements


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("direction", ["onto-shared", "off-shared"])
async def test_status_correction_across_a_shape_change_adopts_the_platform_status(
    sessions: Sessions, direction: str
) -> None:
    world, move, placements = await _moved_while_paused(sessions, direction)
    status = (await _open(sessions, move.campaign))["status"]

    closed = await _platform_correct(sessions, status.id, _at(4))

    assert (closed.state, closed.resolution) == ("resolved", "platform_correct")
    written = (await _decisions(sessions, move.campaign))[-1]
    assert (written.origin, written.shape, written.status) == ("resolution", move.shape, "active")
    if move.shape == "shared_member":
        assert (written.budget_id, written.daily_budget) == (world.shared_budget.id, None)
    else:
        assert (written.budget_id, written.daily_budget) == (None, Decimal(80))
    # The next sync finds the decided status and assignment on the platform: no status
    # discrepancy is raised again, and the assignment discrepancy closes on its own.
    after = (await _sync(sessions, world, placements, at=_at(6))).report
    assert dict(after.raised) == {"status": 0, "budget": 0, "untracked": 0, "missing": 0}
    assert after.auto_resolved == 1
    assert await _open(sessions, move.campaign) == {}


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("direction", ["onto-shared", "off-shared"])
async def test_budget_correction_across_a_shape_change_keeps_the_decided_status(
    sessions: Sessions, direction: str
) -> None:
    world, move, placements = await _moved_while_paused(sessions, direction)
    budget = (await _open(sessions, move.campaign))["budget"]

    await _platform_correct(sessions, budget.id, _at(4))

    written = (await _decisions(sessions, move.campaign))[-1]
    assert (written.shape, written.status) == (move.shape, "paused")
    # The status mismatch is still the user's to resolve; nothing about the budget recurs.
    after = (await _sync(sessions, world, placements, at=_at(6))).report
    assert dict(after.raised) == {"status": 0, "budget": 0, "untracked": 0, "missing": 0}
    assert after.refreshed == 1
    status = await _open(sessions, move.campaign)
    assert set(status) == {"status"}
    await _platform_correct(sessions, status["status"].id, _at(7))
    latest = (await _decisions(sessions, move.campaign))[-1]
    assert (latest.shape, latest.status) == (move.shape, "active")
    final = (await _sync(sessions, world, placements, at=_at(9))).report
    assert final.total_raised == 0
    assert await _open(sessions, move.campaign) == {}


# ---------------------------------------------------------------------------------------
# A compared snapshot from before a shape or period change (BE2-F11)
# ---------------------------------------------------------------------------------------


async def _resolve(
    sessions: Sessions,
    discrepancy_id: int,
    resolution: Resolution,
    at: datetime,
    intended_state: Mapping[str, object] | None = None,
) -> Discrepancy:
    """Apply a resolution and commit."""
    async with sessions() as session, session.begin():
        return await resolve_discrepancy(
            discrepancy_id,
            resolution,
            intended_state,
            session=session,
            decided_by="ops@example.com",
            now=at,
        )


async def _refused(
    sessions: Sessions, discrepancy_id: int, resolution: Resolution, at: datetime
) -> PlatformStateUnknown:
    """Apply a resolution that must be refused as stale, and return the refusal."""
    with pytest.raises(PlatformStateUnknown) as refused:
        await _resolve(sessions, discrepancy_id, resolution, at)
    error = refused.value
    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details["discrepancy_id"] == discrepancy_id
    assert "next entity sync" in error.message
    return error


def _decided_mid_sync(ref: SubjectRef, patch: DecisionPatch, at: datetime) -> Step:
    """Return a user decision recorded while a sync reads the platform, before its writes."""

    async def step(session: AsyncSession) -> None:
        await record_decision(
            ref, patch, "user", session=session, decided_by="ops@example.com", now=at
        )

    return step


@pytest.mark.anyio
@pytest.mark.db
async def test_a_status_snapshot_from_before_a_move_onto_a_shared_budget_is_refused(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    own = SubjectRef(type="campaign", id=world.own)
    await _sync(sessions, world, _placements(world), at=_at(0))
    paused = {**_placements(world), world.own: ("paused", world.own_budget)}
    first = await _sync(sessions, world, paused, at=_at(3))
    status = (await _open(sessions, world.own))["status"]
    compared = first.snapshot_ids[("campaign", world.own)]
    assert status.snapshot_id == compared
    # Google moves the campaign onto shared budget B. The sync that reads it is not judged:
    # the user records a budget decision while it runs, so its snapshot predates it.
    moved = {**_placements(world), world.own: ("paused", world.shared_budget)}
    skipped = await _sync(
        sessions,
        world,
        moved,
        at=_at(6),
        before_writes=_steps(
            _decided_mid_sync(own, DecisionPatch(daily_budget=Decimal(110)), _at(6.5)),
            _move(world.own, world.shared_budget),
        ),
        detected_at=_at(6.6),
    )
    assert (skipped.report.skipped, skipped.report.total_raised) == (1, 0)
    still = await _open(sessions, world.own)
    assert set(still) == {"status"}
    assert still["status"].snapshot_id == compared
    decided = await _decisions(sessions, world.own)

    error = await _refused(sessions, status.id, "platform_correct", _at(7))

    assert error.details["snapshot_id"] == compared
    assert (error.details["observed_shape"], error.details["observed_budget_period"]) == (
        "own_budget",
        "DAILY",
    )
    assert (error.details["current_shape"], error.details["current_budget_id"]) == (
        "shared_member",
        world.shared_budget.id,
    )
    # Nothing was written: own budget A was never recorded as a shared assignment.
    assert [row.id for row in await _decisions(sessions, world.own)] == [row.id for row in decided]
    assert (await _open(sessions, world.own))["status"].state == "open"

    # The next sync compares the campaign again and refreshes the discrepancy.
    refreshed = await _sync(sessions, world, moved, at=_at(9))
    rows = await _open(sessions, world.own)
    assert rows["status"].id == status.id
    assert rows["status"].snapshot_id == refreshed.snapshot_ids[("campaign", world.own)]
    assert set(rows) == {"status", "budget"}
    await _resolve(sessions, status.id, "platform_correct", _at(10))
    written = (await _decisions(sessions, world.own))[-1]
    assert (written.shape, written.status, written.budget_id) == (
        "shared_member",
        "paused",
        world.shared_budget.id,
    )
    assert (written.daily_budget, written.total_budget) == (None, None)
    final = await _sync(sessions, world, moved, at=_at(12))
    assert final.report.total_raised == 0
    assert await _open(sessions, world.own) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_adopting_an_untracked_snapshot_from_before_a_move_is_refused(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world, _placements(world), at=_at(0))
    async with sessions() as session, session.begin():
        newcomer = await _campaign(
            session, world.dataset_id, world.account_id, "13", world.spare_budget
        )
    on_spare = {**_placements(world), newcomer: ("active", world.spare_budget)}
    seen = await _sync(sessions, world, on_spare, at=_at(3))
    assert seen.report.raised["untracked"] == 1
    untracked = (await _open(sessions, newcomer))["untracked"]
    compared = seen.snapshot_ids[("campaign", newcomer)]
    assert untracked.snapshot_id == compared

    async def intend(session: AsyncSession) -> None:
        await resolve_discrepancy(
            untracked.id,
            "record_intended",
            {"status": "active", "daily_budget": "80"},
            session=session,
            decided_by="ops@example.com",
            now=_at(6.5),
        )

    # The user records the intended state while the sync that sees the campaign move onto
    # shared budget B runs, so that sync's snapshot is not judged.
    on_shared = {**_placements(world), newcomer: ("active", world.shared_budget)}
    skipped = await _sync(
        sessions,
        world,
        on_shared,
        at=_at(6),
        before_writes=_steps(intend, _move(newcomer, world.shared_budget)),
        detected_at=_at(6.6),
    )
    assert skipped.report.skipped == 1
    waiting = (await _open(sessions, newcomer))["untracked"]
    assert (waiting.state, waiting.snapshot_id) == ("awaiting_platform", compared)
    decided = await _decisions(sessions, newcomer)
    assert [(row.shape, row.daily_budget) for row in decided] == [("own_budget", Decimal(80))]

    error = await _refused(sessions, untracked.id, "adopt_platform", _at(7))

    assert (error.details["observed_shape"], error.details["current_shape"]) == (
        "own_budget",
        "shared_member",
    )
    assert [row.id for row in await _decisions(sessions, newcomer)] == [row.id for row in decided]
    assert (await _open(sessions, newcomer))["untracked"].state == "awaiting_platform"

    # The next sync's assignment mismatch refreshes the waiting row with its snapshot.
    refreshed = await _sync(sessions, world, on_shared, at=_at(9))
    rows = await _open(sessions, newcomer)
    assert set(rows) == {"untracked"}
    assert rows["untracked"].snapshot_id == refreshed.snapshot_ids[("campaign", newcomer)]
    adopted = await _resolve(sessions, untracked.id, "adopt_platform", _at(10))
    assert (adopted.state, adopted.resolution) == ("resolved", "adopt_platform")
    written = (await _decisions(sessions, newcomer))[-1]
    assert (written.shape, written.status, written.budget_id) == (
        "shared_member",
        "active",
        world.shared_budget.id,
    )
    final = await _sync(sessions, world, on_shared, at=_at(12))
    assert final.report.total_raised == 0
    assert await _open(sessions, newcomer) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_a_daily_budget_snapshot_is_not_adopted_after_a_period_change(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    own = SubjectRef(type="campaign", id=world.own)
    await _sync(sessions, world, _placements(world), at=_at(0))
    raised_a = replace(world.own_budget, micros=120_000_000)
    first = await _sync(
        sessions,
        world,
        {**_placements(world), world.own: ("active", raised_a)},
        at=_at(3),
        budgets=(raised_a, world.shared_budget, world.spare_budget),
    )
    budget = (await _open(sessions, world.own))["budget"]
    compared = first.snapshot_ids[("campaign", world.own)]
    assert budget.snapshot_id == compared
    # Budget A becomes a custom-period budget of 900 while a decision is being recorded.
    custom_a = Funding(
        id=world.own_budget.id, period="CUSTOM_PERIOD", micros=900_000_000, shared=False
    )
    custom = {**_placements(world), world.own: ("paused", custom_a)}
    custom_budgets = (custom_a, world.shared_budget, world.spare_budget)
    skipped = await _sync(
        sessions,
        world,
        custom,
        at=_at(6),
        budgets=custom_budgets,
        before_writes=_steps(
            _decided_mid_sync(own, DecisionPatch(status="paused"), _at(6.5)),
            _upsert_budget(custom_a),
            _move(world.own, custom_a),
        ),
        detected_at=_at(6.6),
    )
    assert skipped.report.skipped == 1
    assert (await _open(sessions, world.own))["budget"].snapshot_id == compared
    decided = await _decisions(sessions, world.own)

    error = await _refused(sessions, budget.id, "platform_correct", _at(7))

    assert (error.details["observed_shape"], error.details["current_shape"]) == (
        "own_budget",
        "own_budget",
    )
    assert (error.details["observed_budget_period"], error.details["current_budget_period"]) == (
        "DAILY",
        "CUSTOM_PERIOD",
    )
    # The daily 120 was not relabelled as a total budget.
    assert [row.id for row in await _decisions(sessions, world.own)] == [row.id for row in decided]

    refreshed = await _sync(sessions, world, custom, at=_at(9), budgets=custom_budgets)
    rows = await _open(sessions, world.own)
    assert set(rows) == {"budget"}
    assert rows["budget"].snapshot_id == refreshed.snapshot_ids[("campaign", world.own)]
    await _resolve(sessions, budget.id, "platform_correct", _at(10))
    written = (await _decisions(sessions, world.own))[-1]
    assert (written.shape, written.status, written.daily_budget, written.total_budget) == (
        "own_budget",
        "paused",
        None,
        Decimal(900),
    )
    final = await _sync(sessions, world, custom, at=_at(12), budgets=custom_budgets)
    assert final.report.total_raised == 0
    assert await _open(sessions, world.own) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_acknowledging_removal_without_a_decision_refuses_a_stale_snapshot(
    sessions: Sessions,
) -> None:
    # Detection raises ``missing`` only for a tracked subject, so the row is inserted here to
    # reach the fallback that materialises the subject's newest snapshot.
    world = await _google_world(sessions)
    await _sync(sessions, world, _placements(world), at=_at(0))
    async with sessions() as session, session.begin():
        newcomer = await _campaign(
            session, world.dataset_id, world.account_id, "13", world.spare_budget
        )
    seen = await _sync(
        sessions, world, {**_placements(world), newcomer: ("active", world.spare_budget)}, at=_at(3)
    )
    async with sessions() as session, session.begin():
        await _move(newcomer, world.shared_budget)(session)
        missing = Discrepancy(
            source="google",
            dataset_id=world.dataset_id,
            subject_type="campaign",
            subject_id=newcomer,
            kind="missing",
            decided_value=None,
            platform_value=None,
            snapshot_id=None,
            decision_id=None,
            detected_at=_at(4),
            state="open",
        )
        session.add(missing)
        await session.flush()
        missing_id = missing.id

    error = await _refused(sessions, missing_id, "acknowledge_removal", _at(5))

    assert error.details["snapshot_id"] == seen.snapshot_ids[("campaign", newcomer)]
    assert (error.details["observed_shape"], error.details["current_shape"]) == (
        "own_budget",
        "shared_member",
    )
    assert await _decisions(sessions, newcomer) == []


# ---------------------------------------------------------------------------------------
# An own-budget amount Google omitted
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_adopting_an_own_budget_whose_amount_google_omitted_is_refused(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    await _sync(sessions, world, _placements(world), at=_at(0))
    async with sessions() as session, session.begin():
        newcomer = await _campaign(
            session, world.dataset_id, world.account_id, "13", world.spare_budget
        )
    on_spare = {**_placements(world), newcomer: ("active", world.spare_budget)}
    seen = await _sync(
        sessions, world, on_spare, at=_at(3), unreported=frozenset({world.spare_budget.id})
    )
    assert seen.report.raised["untracked"] == 1
    untracked = (await _open(sessions, newcomer))["untracked"]
    compared = seen.snapshot_ids[("campaign", newcomer)]
    assert untracked.snapshot_id == compared

    with pytest.raises(PlatformStateUnknown) as refused:
        await _resolve(sessions, untracked.id, "adopt_platform", _at(4))

    error = refused.value
    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details == {"snapshot_id": compared}
    assert "did not report the amount or period of the campaign's budget" in error.message
    # Nothing was written: the unknown amount was not recorded as no budget.
    assert await _decisions(sessions, newcomer) == []
    assert (await _open(sessions, newcomer))["untracked"].state == "open"

    # The next sync reports the amount and refreshes the row, which then adopts it.
    known = await _sync(sessions, world, on_spare, at=_at(6))
    assert (await _open(sessions, newcomer))["untracked"].snapshot_id == known.snapshot_ids[
        ("campaign", newcomer)
    ]
    adopted = await _resolve(sessions, untracked.id, "adopt_platform", _at(7))
    assert (adopted.state, adopted.resolution) == ("resolved", "adopt_platform")
    written = (await _decisions(sessions, newcomer))[-1]
    assert (written.shape, written.status, written.daily_budget, written.total_budget) == (
        "own_budget",
        "active",
        Decimal(80),
        None,
    )
    final = await _sync(sessions, world, on_spare, at=_at(9))
    assert final.report.total_raised == 0
    assert await _open(sessions, newcomer) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_a_status_correction_needs_no_own_budget_amount_google_omitted(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    own = SubjectRef(type="campaign", id=world.own)
    await _sync(sessions, world, _placements(world), at=_at(0))
    await _decide(sessions, own, DecisionPatch(daily_budget=Decimal(120)), _at(1))
    paused = {**_placements(world), world.own: ("paused", world.own_budget)}
    blind = await _sync(
        sessions, world, paused, at=_at(3), unreported=frozenset({world.own_budget.id})
    )
    # The status differs; the amount is unknown, so no budget discrepancy is raised.
    assert dict(blind.report.raised) == {"status": 1, "budget": 0, "untracked": 0, "missing": 0}
    status = (await _open(sessions, world.own))["status"]
    assert status.snapshot_id == blind.snapshot_ids[("campaign", world.own)]

    closed = await _platform_correct(sessions, status.id, _at(4))

    assert (closed.state, closed.resolution) == ("resolved", "platform_correct")
    written = (await _decisions(sessions, world.own))[-1]
    assert (
        written.origin,
        written.shape,
        written.status,
        written.daily_budget,
        written.total_budget,
    ) == ("resolution", "own_budget", "paused", Decimal(120), None)
    # The decided 120 is still the expectation: the platform reporting it raises nothing.
    applied = replace(world.own_budget, micros=120_000_000)
    after = await _sync(
        sessions,
        world,
        {**_placements(world), world.own: ("paused", applied)},
        at=_at(6),
        budgets=(applied, world.shared_budget, world.spare_budget),
    )
    assert after.report.total_raised == 0
    assert await _open(sessions, world.own) == {}


# ---------------------------------------------------------------------------------------
# A patch over an own budget whose period changed
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_a_patch_over_a_daily_decision_of_a_now_custom_period_budget_is_refused(
    sessions: Sessions,
) -> None:
    world = await _google_world(sessions)
    own = SubjectRef(type="campaign", id=world.own)
    await _sync(sessions, world, _placements(world), at=_at(0))
    # Budget A becomes a custom-period budget of 900; the decision still holds daily 100.
    custom_a = Funding(
        id=world.own_budget.id, period="CUSTOM_PERIOD", micros=900_000_000, shared=False
    )
    custom = {**_placements(world), world.own: ("active", custom_a)}
    moved = await _sync(
        sessions,
        world,
        custom,
        at=_at(3),
        budgets=(custom_a, world.shared_budget, world.spare_budget),
        before_writes=_steps(_upsert_budget(custom_a), _move(world.own, custom_a)),
    )
    assert dict(moved.report.raised) == {"status": 0, "budget": 1, "untracked": 0, "missing": 0}
    decided = [row.id for row in await _decisions(sessions, world.own)]

    for patch, decided_fields in (
        (DecisionPatch(total_budget=Decimal(200)), ["daily_budget", "total_budget"]),
        (DecisionPatch(status="paused"), ["daily_budget"]),
    ):
        with pytest.raises(PatchRejected) as refused:
            await _decide(sessions, own, patch, _at(4))
        error = refused.value
        assert (error.status_code, error.code) == (422, "budget_changed")
        assert error.details == {
            "decided_fields": decided_fields,
            "current_fields": ["total_budget"],
            "current_budget_period": "CUSTOM_PERIOD",
        }
        assert "Resolve its budget discrepancy first." in error.message

    # Nothing was written: no row holds the obsolete daily 100, alone or beside a total.
    assert [row.id for row in await _decisions(sessions, world.own)] == decided
    # Resolving the budget discrepancy brings the decision to the new period, after which
    # the total budget is decided as usual.
    budget = (await _open(sessions, world.own))["budget"]
    await _platform_correct(sessions, budget.id, _at(5))
    corrected = (await _decisions(sessions, world.own))[-1]
    assert (corrected.status, corrected.daily_budget, corrected.total_budget) == (
        "active",
        None,
        Decimal(900),
    )
    await _decide(sessions, own, DecisionPatch(total_budget=Decimal(200)), _at(6))
    latest = (await _decisions(sessions, world.own))[-1]
    assert (latest.origin, latest.status, latest.daily_budget, latest.total_budget) == (
        "user",
        "active",
        None,
        Decimal(200),
    )


# ---------------------------------------------------------------------------------------
# An intended state's amounts at the stored money scale (QAPI2-F02)
# ---------------------------------------------------------------------------------------


async def _untracked(sessions: Sessions, ref: SubjectRef) -> Discrepancy:
    """Return the subject's one ``untracked`` discrepancy, whatever its state."""
    async with sessions() as session:
        (row,) = await session.scalars(
            select(Discrepancy).where(
                Discrepancy.subject_type == ref.type,
                Discrepancy.subject_id == ref.id,
                Discrepancy.kind == "untracked",
            )
        )
        return row


def _google_money(amount: str, micros: int) -> dict[str, object]:
    """Return a Google daily amount as a discrepancy value stores it."""
    return {
        "daily_budget": amount,
        "total_budget": None,
        "currency": "USD",
        "amount_micros": micros,
    }


@pytest.mark.anyio
@pytest.mark.db
async def test_an_intended_state_is_stored_at_the_scale_of_the_platform_side(
    sessions: Sessions,
) -> None:
    # AAP 0.5.2: money is numeric(18, 6). record_intended stores the submitted amounts
    # ("125", "2.6E+2") at the six places the platform side shows, and the next sync's
    # refresh from the stored decisions keeps exactly that text.
    world = await _google_world(sessions)
    await _sync(sessions, world, _placements(world), at=_at(0))
    async with sessions() as session, session.begin():
        newcomer = await _campaign(
            session, world.dataset_id, world.account_id, "13", world.spare_budget
        )
        pool = await _budget(session, world.dataset_id, "505", 250_000_000, shared=True)
    campaign = SubjectRef(type="campaign", id=newcomer)
    budget = SubjectRef(type="budget", id=pool.id)
    placements = {**_placements(world), newcomer: ("active", world.spare_budget)}
    budgets = (*world.budgets, pool)
    seen = await _sync(sessions, world, placements, at=_at(3), budgets=budgets)
    assert seen.report.raised["untracked"] == 2
    on_campaign = await _untracked(sessions, campaign)
    on_budget = await _untracked(sessions, budget)
    assert on_campaign.platform_value == {
        "status": "active",
        **_google_money("80.000000", 80_000_000),
    }
    assert on_budget.platform_value == _google_money("250.000000", 250_000_000)

    intended_campaign = await _resolve(
        sessions,
        on_campaign.id,
        "record_intended",
        _at(4),
        {"status": "active", "daily_budget": "125"},
    )
    intended_budget = await _resolve(
        sessions, on_budget.id, "record_intended", _at(4), {"daily_budget": "2.6E+2"}
    )

    campaign_decided = {"status": "active", **_google_money("125.000000", 125_000_000)}
    budget_decided = _google_money("260.000000", 260_000_000)
    assert intended_campaign.decided_value == campaign_decided
    assert intended_budget.decided_value == budget_decided
    for ref, decided in ((campaign, campaign_decided), (budget, budget_decided)):
        stored = await _untracked(sessions, ref)
        assert (stored.state, stored.decided_value) == ("awaiting_platform", decided)

    # The platform has not changed yet: the waiting rows are refreshed from the stored
    # decisions, and their decided sides keep the same text.
    await _sync(sessions, world, placements, at=_at(6), budgets=budgets)
    for ref, decided in ((campaign, campaign_decided), (budget, budget_decided)):
        refreshed = await _untracked(sessions, ref)
        assert (refreshed.state, refreshed.decided_value) == ("awaiting_platform", decided)

    # The platform reaches both intended amounts and each row closes.
    applied_spare = replace(world.spare_budget, micros=125_000_000)
    applied_pool = replace(pool, micros=260_000_000)
    await _sync(
        sessions,
        world,
        {**_placements(world), newcomer: ("active", applied_spare)},
        at=_at(9),
        budgets=(world.own_budget, world.shared_budget, applied_spare, applied_pool),
    )
    for ref in (campaign, budget):
        closed = await _untracked(sessions, ref)
        assert (closed.state, closed.resolution) == ("resolved", "record_intended")
