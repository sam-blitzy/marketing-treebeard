"""Tests for set-based subject loading and ``GET /discrepancies`` (AAP 0.5.3, 0.5.4.4).

:func:`ads_engine.decisions.load_subject_states` loads many decision subjects with a number
of statements that does not grow with their count; :func:`~ads_engine.decisions.load_subject_state`
is that function for one subject. The tests pin both to the documented state of every
subject shape (a Google own-budget campaign, a Google shared member whose decision assigns
another shared budget, a Google shared budget whose currency comes from its members or from
the dataset's Google account, a LinkedIn campaign) and to the exact ``SubjectNotFound`` of
every unloadable subject, check that loading many subjects at once, in one round or in
several, gives each subject the state it gets alone, and that entity rows are re-read after
a Core upsert.

The route tests serve ``GET /api/v1/discrepancies`` from a stand-in FastAPI app that mounts
the real router, with ``require_user`` and ``get_session`` overridden onto this module's
test schema. They check the active-dataset and state filters, the oldest-first order, the
response shape, the omission (logged once per subject) of a discrepancy whose subject the
domain cannot load, and that the route's statement count is the same for 10 and 200
subjects.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``), in a private schema created and
dropped by this module, so no other schema is touched. An unreachable database fails them.
"""

import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema
from structlog.testing import capture_logs

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine import decisions
from ads_engine.api.deps import CurrentUser, get_session, require_user
from ads_engine.api.routes import discrepancies as routes
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Budget, Campaign
from ads_engine.db.models.decisions import Decision, Discrepancy
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    SubjectNotFound,
    SubjectRef,
    SubjectState,
    load_subject_state,
    load_subject_states,
)

type Sessions = async_sessionmaker[AsyncSession]

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""The first observation; later instants are whole hours after it."""


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
        pytest.fail("TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database")
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
    schema = f"discrepancy_list_test_{uuid.uuid4().hex}"
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
async def engine(database: tuple[URL, str]) -> AsyncIterator[AsyncEngine]:
    """Yield an engine created and disposed inside this test's event loop."""
    url, schema = database
    created = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    """Return a session factory on this test's engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


class StatementCounter:
    """Count the SQL statements an engine sends while :attr:`active` is set."""

    def __init__(self) -> None:
        """Start inactive with no statements counted."""
        self.active = False
        self.count = 0

    def __call__(self, *_: object) -> None:
        """``before_cursor_execute`` listener: count one statement when active."""
        if self.active:
            self.count += 1


@pytest.fixture
def counter(engine: AsyncEngine) -> Iterator[StatementCounter]:
    """Attach a statement counter to the test's engine."""
    listener = StatementCounter()
    event.listen(engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield listener
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", listener)


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


async def _retire_active(session: AsyncSession, source: str) -> None:
    """Retire the source's active primary dataset, if any (at most one may be active)."""
    await session.execute(
        update(SourceDataset)
        .where(
            SourceDataset.source == source,
            SourceDataset.state == "active",
            SourceDataset.role == "primary",
        )
        .values(state="retired", retired_at=T0)
    )


async def _dataset(session: AsyncSession, source: str, state: str = "active") -> int:
    """Insert a primary dataset; an active one first retires the source's previous one."""
    if state == "active":
        await _retire_active(session, source)
    dataset = SourceDataset(source=source, mode="simulated", state=state, role="primary")
    session.add(dataset)
    await session.flush()
    return dataset.id


async def _account(
    session: AsyncSession, dataset_id: int, source: str, external_id: str, currency: str
) -> int:
    """Insert an ad account and return its id."""
    row = AdAccount(
        source=source,
        dataset_id=dataset_id,
        external_id=external_id,
        name=f"Account {external_id}",
        currency=currency,
        timezone="UTC",
    )
    session.add(row)
    await session.flush()
    return row.id


async def _budget(
    session: AsyncSession,
    dataset_id: int,
    external_id: str,
    *,
    shared: bool,
    period: str = "DAILY",
    micros: int = 50_000_000,
) -> int:
    """Insert a Google budget and return its id."""
    row = Budget(
        source="google",
        dataset_id=dataset_id,
        external_id=external_id,
        amount=Decimal(micros) / Decimal(1_000_000),
        amount_micros=micros,
        period=period,
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
    *,
    source: str = "google",
    budget_id: int | None = None,
    budget_period: str | None = "DAILY",
    daily: Decimal | None = None,
    total: Decimal | None = None,
) -> int:
    """Insert a campaign and return its id."""
    row = Campaign(
        source=source,
        dataset_id=dataset_id,
        account_id=account_id,
        external_id=external_id,
        name=f"Campaign {external_id}",
        status="active",
        raw_status="ENABLED" if source == "google" else "ACTIVE",
        objective_or_channel="SEARCH" if source == "google" else "LEAD_GENERATION",
        budget_id=budget_id,
        daily_budget=daily,
        total_budget=total,
        budget_period=budget_period if source == "google" else None,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _snapshot(
    session: AsyncSession,
    dataset_id: int,
    ref: SubjectRef,
    at: datetime,
    *,
    source: str = "google",
    budget_id: int | None = None,
) -> int:
    """Insert an ``entity_snapshot`` row directly and return its id."""
    row = EntitySnapshot(
        source=source,
        dataset_id=dataset_id,
        entity_type=ref.type,
        entity_id=ref.id,
        captured_at=at,
        status="active" if ref.type == "campaign" else None,
        budget_id=budget_id,
        payload={},
    )
    session.add(row)
    await session.flush()
    return row.id


async def _decision(
    session: AsyncSession,
    dataset_id: int,
    ref: SubjectRef,
    at: datetime,
    *,
    shape: str,
    source: str = "google",
    budget_id: int | None = None,
    daily: Decimal | None = None,
) -> int:
    """Insert a ``decision`` row directly and return its id."""
    row = Decision(
        source=source,
        dataset_id=dataset_id,
        subject_type=ref.type,
        subject_id=ref.id,
        shape=shape,
        status=None if shape == "budget" else "active",
        daily_budget=daily,
        total_budget=None,
        budget_id=budget_id,
        tombstone=False,
        decided_at=at,
        decided_by=None,
        origin="baseline",
        recommendation_id=None,
        patch={},
        note=None,
    )
    session.add(row)
    await session.flush()
    return row.id


@dataclass(frozen=True)
class World:
    """Every subject shape and every unloadable subject, with the rows they should load."""

    google: int
    linkedin: int
    own: SubjectRef
    member_a: SubjectRef
    member_b: SubjectRef
    unbudgeted: SubjectRef
    shared: SubjectRef
    memberless: SubjectRef
    li_campaign: SubjectRef
    own_budget: SubjectRef
    absent_campaign: SubjectRef
    absent_budget: SubjectRef
    ambiguous: SubjectRef
    accountless: SubjectRef
    shared_external: str
    old_shared_external: str
    ambiguous_external: str
    accountless_external: str
    own_snapshot: int
    own_decision: int
    member_b_snapshot: int
    member_b_decision: int
    shared_snapshot: int
    shared_decision: int


async def _world(sessions: Sessions) -> World:
    """Commit the subjects :class:`World` describes."""
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        usd = await _account(session, google, "google", "1001", "USD")
        own_budget = await _budget(session, google, "501", shared=False, micros=100_000_000)
        shared = await _budget(session, google, "502", shared=True)
        old_shared = await _budget(session, google, "509", shared=True)
        memberless = await _budget(session, google, "503", shared=True, period="CUSTOM_PERIOD")
        own = SubjectRef(
            "campaign", await _campaign(session, google, usd, "11", budget_id=own_budget)
        )
        member_a = SubjectRef(
            "campaign", await _campaign(session, google, usd, "12", budget_id=shared)
        )
        member_b = SubjectRef(
            "campaign", await _campaign(session, google, usd, "13", budget_id=shared)
        )
        unbudgeted = SubjectRef(
            "campaign", await _campaign(session, google, usd, "14", budget_period=None)
        )
        # Snapshots: the newest in the subject's own dataset wins, ties broken by id.
        await _snapshot(session, google, own, _at(0), budget_id=own_budget)
        await _snapshot(session, google, own, _at(3), budget_id=own_budget)
        own_snapshot = await _snapshot(session, google, own, _at(3), budget_id=own_budget)
        member_b_snapshot = await _snapshot(session, google, member_b, _at(3), budget_id=shared)
        shared_ref = SubjectRef("budget", shared)
        await _snapshot(session, google, shared_ref, _at(0))
        shared_snapshot = await _snapshot(session, google, shared_ref, _at(3))
        # Decisions: the latest wins, ties broken by id.
        await _decision(session, google, own, _at(0), shape="own_budget", daily=Decimal(100))
        await _decision(session, google, own, _at(4), shape="own_budget", daily=Decimal(90))
        own_decision = await _decision(
            session, google, own, _at(4), shape="own_budget", daily=Decimal(80)
        )
        await _decision(session, google, member_a, _at(0), shape="shared_member", budget_id=shared)
        member_b_decision = await _decision(
            session, google, member_b, _at(0), shape="shared_member", budget_id=old_shared
        )
        shared_decision = await _decision(
            session, google, shared_ref, _at(0), shape="budget", daily=Decimal(50)
        )

        # Rows of another dataset never count for these subjects, even when newer. That
        # dataset's snapshot draws from its own copy of budget 501, as a snapshot's budget
        # must belong to the snapshot's dataset.
        other = await _dataset(session, "google", "building")
        other_budget = await _budget(session, other, "501", shared=False, micros=100_000_000)
        await _snapshot(session, other, own, _at(9), budget_id=other_budget)
        await _decision(session, other, own, _at(9), shape="own_budget", daily=Decimal(70))

        # A shared budget whose members bill in two currencies, and one in a dataset with
        # no Google account: neither currency can be determined.
        mixed = await _dataset(session, "google", "building")
        mixed_usd = await _account(session, mixed, "google", "2001", "USD")
        mixed_eur = await _account(session, mixed, "google", "2002", "EUR")
        ambiguous = await _budget(session, mixed, "601", shared=True)
        await _campaign(session, mixed, mixed_usd, "21", budget_id=ambiguous)
        await _campaign(session, mixed, mixed_eur, "22", budget_id=ambiguous)
        bare = await _dataset(session, "google", "building")
        accountless = await _budget(session, bare, "701", shared=True)

        linkedin = await _dataset(session, "linkedin")
        gbp = await _account(session, linkedin, "linkedin", "508", "GBP")
        li_campaign = SubjectRef(
            "campaign",
            await _campaign(
                session,
                linkedin,
                gbp,
                "urn:li:sponsoredCampaign:61",
                source="linkedin",
                daily=Decimal("25.00"),
                total=Decimal("900.00"),
            ),
        )
        return World(
            google=google,
            linkedin=linkedin,
            own=own,
            member_a=member_a,
            member_b=member_b,
            unbudgeted=unbudgeted,
            shared=shared_ref,
            memberless=SubjectRef("budget", memberless),
            li_campaign=li_campaign,
            own_budget=SubjectRef("budget", own_budget),
            absent_campaign=SubjectRef("campaign", 987_654_321),
            absent_budget=SubjectRef("budget", 987_654_321),
            ambiguous=SubjectRef("budget", ambiguous),
            accountless=SubjectRef("budget", accountless),
            shared_external="502",
            old_shared_external="509",
            ambiguous_external="601",
            accountless_external="701",
            own_snapshot=own_snapshot,
            own_decision=own_decision,
            member_b_snapshot=member_b_snapshot,
            member_b_decision=member_b_decision,
            shared_snapshot=shared_snapshot,
            shared_decision=shared_decision,
        )


def _loadable(world: World) -> list[SubjectRef]:
    """The world's loadable subjects."""
    return [
        world.own,
        world.member_a,
        world.member_b,
        world.unbudgeted,
        world.shared,
        world.memberless,
        world.li_campaign,
    ]


def _unloadable(world: World) -> dict[SubjectRef, tuple[str, dict[str, object]]]:
    """The world's unloadable subjects, with the message and details each must report."""
    not_shared = "is not an explicitly shared budget; only shared budgets are decision subjects."
    return {
        world.absent_campaign: ("Campaign 987654321 does not exist.", {}),
        world.own_budget: (f"Budget {world.own_budget.id} {not_shared}", {}),
        world.absent_budget: (f"Budget 987654321 {not_shared}", {}),
        world.ambiguous: (
            f"The currency of shared budget {world.ambiguous_external} cannot be determined.",
            {"budget_id": world.ambiguous.id},
        ),
        world.accountless: (
            f"The currency of shared budget {world.accountless_external} cannot be determined.",
            {"budget_id": world.accountless.id},
        ),
    }


def _summary(state: SubjectState) -> dict[str, Any]:
    """Return the scalar facts of a state, with its snapshot and decision as ids."""
    return {
        "dataset_state": state.dataset_state,
        "source": state.source,
        "currency": state.currency,
        "name": state.name,
        "shape": state.shape,
        "has_daily_budget": state.has_daily_budget,
        "has_total_budget": state.has_total_budget,
        "budget_period": state.budget_period,
        "budget_id": state.budget_id,
        "budget_external_id": state.budget_external_id,
        "snapshot": None if state.latest_snapshot is None else state.latest_snapshot.id,
        "decision": None if state.current_decision is None else state.current_decision.id,
        "decided_budget_external_id": state.decided_budget_external_id,
    }


# ---------------------------------------------------------------------------------------
# load_subject_states
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_every_shape_loads_its_documented_state(sessions: Sessions) -> None:
    world = await _world(sessions)
    async with sessions() as session:
        loaded = await load_subject_states(session, _loadable(world))
    assert loaded.not_found == {}
    google = {"dataset_state": "active", "source": "google", "currency": "USD"}
    assert _summary(loaded.states[world.own]) == {
        **google,
        "name": "Campaign 11",
        "shape": "own_budget",
        "has_daily_budget": True,
        "has_total_budget": False,
        "budget_period": "DAILY",
        "budget_id": None,
        "budget_external_id": None,
        "snapshot": world.own_snapshot,
        "decision": world.own_decision,
        "decided_budget_external_id": None,
    }
    member = {
        **google,
        "shape": "shared_member",
        "has_daily_budget": False,
        "has_total_budget": False,
        "budget_period": "DAILY",
        "budget_id": world.shared.id,
        "budget_external_id": world.shared_external,
    }
    assert _summary(loaded.states[world.member_b]) == {
        **member,
        "name": "Campaign 13",
        "snapshot": world.member_b_snapshot,
        "decision": world.member_b_decision,
        "decided_budget_external_id": world.old_shared_external,
    }
    member_a = _summary(loaded.states[world.member_a])
    assert (member_a["shape"], member_a["snapshot"], member_a["decided_budget_external_id"]) == (
        "shared_member",
        None,
        world.shared_external,
    )
    assert _summary(loaded.states[world.unbudgeted]) == {
        **google,
        "name": "Campaign 14",
        "shape": "own_budget",
        "has_daily_budget": False,
        "has_total_budget": False,
        "budget_period": None,
        "budget_id": None,
        "budget_external_id": None,
        "snapshot": None,
        "decision": None,
        "decided_budget_external_id": None,
    }
    assert _summary(loaded.states[world.shared]) == {
        **google,
        "name": f"Shared budget {world.shared_external}",
        "shape": "budget",
        "has_daily_budget": True,
        "has_total_budget": False,
        "budget_period": "DAILY",
        "budget_id": None,
        "budget_external_id": None,
        "snapshot": world.shared_snapshot,
        "decision": world.shared_decision,
        "decided_budget_external_id": None,
    }
    memberless = _summary(loaded.states[world.memberless])
    assert (
        memberless["currency"],
        memberless["has_daily_budget"],
        memberless["has_total_budget"],
    ) == (
        "USD",
        False,
        True,
    )
    assert memberless["budget_period"] == "CUSTOM_PERIOD"
    assert _summary(loaded.states[world.li_campaign]) == {
        "dataset_state": "active",
        "source": "linkedin",
        "currency": "GBP",
        "name": "Campaign urn:li:sponsoredCampaign:61",
        "shape": "own_budget",
        "has_daily_budget": True,
        "has_total_budget": True,
        "budget_period": None,
        "budget_id": None,
        "budget_external_id": None,
        "snapshot": None,
        "decision": None,
        "decided_budget_external_id": None,
    }


@pytest.mark.anyio
async def test_unloadable_subjects_report_the_error_load_subject_state_raises(
    sessions: Sessions,
) -> None:
    world = await _world(sessions)
    expected = _unloadable(world)
    async with sessions() as session:
        loaded = await load_subject_states(session, [*expected, world.own])
        assert set(loaded.states) == {world.own}
        for ref, (message, details) in expected.items():
            error = loaded.not_found[ref]
            assert (error.code, error.message, error.details, error.status_code) == (
                "not_found",
                message,
                details,
                404,
            )
            with pytest.raises(SubjectNotFound) as raised:
                loaded.require(ref)
            assert (raised.value.code, raised.value.message, raised.value.details) == (
                "not_found",
                message,
                details,
            )
            with pytest.raises(SubjectNotFound) as single:
                await load_subject_state(session, ref)
            assert (single.value.code, single.value.message, single.value.details) == (
                "not_found",
                message,
                details,
            )
        with pytest.raises(KeyError):
            loaded.require(world.member_a)
        with pytest.raises(TypeError):
            await load_subject_states(session, [("campaign", world.own.id)])  # type: ignore[list-item]


@pytest.mark.anyio
async def test_many_subjects_load_as_each_loads_alone_in_one_round_or_several(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(sessions)
    refs = [*_loadable(world), *_unloadable(world)]
    async with sessions() as session:
        alone: dict[SubjectRef, SubjectState | str] = {}
        for ref in refs:
            try:
                alone[ref] = await load_subject_state(session, ref)
            except SubjectNotFound as exc:
                alone[ref] = exc.message
        together = await load_subject_states(session, [*refs, *reversed(refs)])
        monkeypatch.setattr(decisions, "_STATE_CHUNK", 2)
        rounds = await load_subject_states(session, refs)
    for loaded in (together, rounds):
        assert set(loaded.states) | set(loaded.not_found) == set(refs)
        assert not set(loaded.states) & set(loaded.not_found)
        for ref in refs:
            got: SubjectState | str = (
                loaded.states[ref] if ref in loaded.states else loaded.not_found[ref].message
            )
            assert got == alone[ref]


@pytest.mark.anyio
async def test_entity_rows_are_re_read_after_a_core_upsert(sessions: Sessions) -> None:
    world = await _world(sessions)
    async with sessions() as session:
        before = await load_subject_states(session, [world.own, world.shared])
        assert before.states[world.own].name == "Campaign 11"
        assert before.states[world.shared].has_daily_budget is True
        await session.execute(
            update(Campaign).where(Campaign.id == world.own.id).values(name="Renamed")
        )
        await session.execute(
            update(Budget).where(Budget.id == world.shared.id).values(period="CUSTOM_PERIOD")
        )
        after = await load_subject_states(session, [world.own, world.shared])
        assert after.states[world.own].name == "Renamed"
        assert (
            after.states[world.shared].has_daily_budget,
            after.states[world.shared].has_total_budget,
        ) == (False, True)
        await session.rollback()


# ---------------------------------------------------------------------------------------
# GET /discrepancies
# ---------------------------------------------------------------------------------------


def _app(sessions: Sessions) -> FastAPI:
    """Return a stand-in app serving the real discrepancy router on the test schema."""
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")

    async def session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[require_user] = lambda: CurrentUser(id=1, email="ops@example.com")
    return app


async def _get(sessions: Sessions, query: str = "") -> httpx.Response:
    """Serve ``GET /api/v1/discrepancies{query}``."""
    transport = httpx.ASGITransport(app=_app(sessions))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/api/v1/discrepancies{query}")


def _discrepancy(
    dataset_id: int,
    ref: SubjectRef,
    kind: str,
    at: datetime,
    *,
    state: str = "open",
    source: str = "google",
) -> Discrepancy:
    """Return a ``discrepancy`` row for direct insertion."""
    return Discrepancy(
        source=source,
        dataset_id=dataset_id,
        subject_type=ref.type,
        subject_id=ref.id,
        kind=kind,
        decided_value=None if kind == "untracked" else {"status": "active"},
        platform_value=None if kind == "missing" else {"status": "paused"},
        snapshot_id=None,
        decision_id=None,
        detected_at=at,
        state=state,
        resolved_at=at if state == "resolved" else None,
        resolution=None,
    )


@pytest.mark.anyio
async def test_list_filters_orders_and_omits_unloadable_subjects_once(sessions: Sessions) -> None:
    world = await _world(sessions)
    async with sessions() as session, session.begin():
        building = await _dataset(session, "google", "building")
        rows = [
            _discrepancy(world.google, world.own, "status", _at(6)),
            _discrepancy(world.google, world.shared, "budget", _at(5)),
            _discrepancy(world.google, world.member_b, "budget", _at(6), state="awaiting_platform"),
            _discrepancy(world.google, world.own_budget, "budget", _at(4)),
            _discrepancy(world.google, world.own_budget, "missing", _at(7)),
            _discrepancy(world.google, world.own, "budget", _at(1), state="resolved"),
            _discrepancy(world.linkedin, world.li_campaign, "status", _at(8), source="linkedin"),
            _discrepancy(building, world.unbudgeted, "status", _at(2)),
        ]
        session.add_all(rows)
        await session.flush()
        ids = [row.id for row in rows]
    with capture_logs() as entries:
        response = await _get(sessions)
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [ids[1], ids[0], ids[2], ids[6]]
    first = body[0]
    assert first == {
        "id": ids[1],
        "subject": {
            "type": "budget",
            "id": world.shared.id,
            "name": f"Shared budget {world.shared_external}",
            "source": "google",
            "currency": "USD",
        },
        "shape": "budget",
        "kind": "budget",
        "decided_value": {"status": "active"},
        "platform_value": {"status": "paused"},
        "detected_at": "2026-10-01T11:00:00Z",
        "state": "open",
        "resolved_at": None,
        "resolution": None,
        "resolutions": ["platform_correct", "decision_correct"],
    }
    assert [(item["subject"]["id"], item["shape"]) for item in body[1:]] == [
        (world.own.id, "own_budget"),
        (world.member_b.id, "shared_member"),
        (world.li_campaign.id, "own_budget"),
    ]
    assert body[3]["subject"]["currency"] == "GBP"
    unavailable = [
        (entry["log_level"], entry["discrepancy_id"], entry["subject_type"], entry["subject_id"])
        for entry in entries
        if entry["event"] == "discrepancies.subject_unavailable"
    ]
    assert unavailable == [("warning", ids[3], "budget", world.own_budget.id)]
    resolved = (await _get(sessions, "?state=resolved")).json()
    assert [item["id"] for item in resolved] == [ids[5]]
    both = (await _get(sessions, "?state=open&state=resolved")).json()
    assert [item["id"] for item in both] == [ids[5], ids[1], ids[0], ids[6]]


async def _many(sessions: Sessions, count: int) -> None:
    """Commit an active Google dataset with ``count`` campaigns, each with an open status
    discrepancy, plus one shared budget with an open budget discrepancy."""
    async with sessions() as session, session.begin():
        dataset_id = await _dataset(session, "google")
        account = await _account(session, dataset_id, "google", "3001", "USD")
        shared = await _budget(session, dataset_id, "801", shared=True)
        rows = [_discrepancy(dataset_id, SubjectRef("budget", shared), "budget", _at(0))]
        for index in range(count):
            budget = await _budget(session, dataset_id, f"9{index}", shared=False)
            campaign = SubjectRef(
                "campaign",
                await _campaign(session, dataset_id, account, f"c{index}", budget_id=budget),
            )
            await _snapshot(session, dataset_id, campaign, _at(0), budget_id=budget)
            await _decision(session, dataset_id, campaign, _at(0), shape="own_budget")
            rows.append(_discrepancy(dataset_id, campaign, "status", _at(1)))
        session.add_all(rows)


@pytest.mark.anyio
async def test_list_statement_count_does_not_grow_with_the_subjects(
    sessions: Sessions, counter: StatementCounter
) -> None:
    async with sessions() as session, session.begin():
        await _retire_active(session, "linkedin")
    counts: Counter[int] = Counter()
    for count in (10, 200):
        await _many(sessions, count)
        counter.count = 0
        counter.active = True
        # The largest page holds every row of both sizes, so one request lists them all.
        response = await _get(sessions, "?limit=500")
        counter.active = False
        assert response.status_code == 200
        assert len(response.json()) == count + 1
        assert "Link" not in response.headers
        counts[count] = counter.count
    assert counts[10] == counts[200]
    assert counts[200] <= 12
