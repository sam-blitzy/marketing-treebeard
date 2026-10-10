"""A sync job publishes a dataset's data and its outcome in one commit, or nothing at all.

AAP 0.3.2 ("Freshness": a successful sync never hides a failed refresh; "Events": a completed
sync queues a default-window ``analysis_request``) and 0.3.3 (the transient row: after its
retries the dataset keeps its last good data and turns stale, and what was read is still
written). Every job reads its whole scope with no transaction open, then writes the rows read
(upserts and complete-scope deletions), the Google checkpoint, the freshness outcome, the
closed ``sync_run`` row and the rerun request in one transaction. Proven on the PostgreSQL
test database (``db`` of ``tests/conftest.py``) with the production jobs on simulated clients,
each failure placed at the connector seam:

* **Nothing before the commit** (``i``, ``ii``, ``iv``): a Google metrics refresh, a LinkedIn
  metrics refresh or a segment refresh cancelled, or failing with an unexpected error, after
  its first read changed rows leaves every fact row version, every freshness row and the
  request queue exactly as they were; so does a CRM sync stopped after its Google-touch
  discovery read contacts, before the contact refresh committed.
* **One commit for a failed attempt** (``iii``, ``iv``, ``v``): a transient interruption of a
  later read, of the CRM discovery, contact refresh, deal search or association read, or a
  lead listing (transient, or with a lead that cannot be placed) stores the rows read, records
  the failed attempt (``last_attempt_at`` moves; ``last_success_at``, the coverage and the
  completeness stand), closes its ``sync_run`` row ``failed`` and queues an
  ``analysis_request``, all committed by one transaction: PostgreSQL stamps every row version
  with the id of the transaction that wrote it (``xmin``), so equal ids mean one commit.

Every clock is fixed (:data:`T0` builds the datasets, :data:`T1` runs the job under test),
retries never wait in real time, and no test sleeps.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import pytest
from sqlalchemy import Table, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.errors import TransientError
from ads_engine.connectors.google_n8n.partition import (
    FetchOutcome,
    PartitionedFetcher,
    interruption_reason,
)
from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import (
    AssociationFetch,
    ContactBatch,
    HubSpotClient,
    RateLimiter,
)
from ads_engine.connectors.hubspot.search import SearchOutcome
from ads_engine.connectors.linkedin.client import LeadListing, LinkedInClient, Listing
from ads_engine.connectors.linkedin.parser import PREFIX_ACCOUNT, PREFIX_CAMPAIGN
from ads_engine.db.models import (
    AnalysisRequest,
    CrmAssociation,
    CrmAssociationFetch,
    CrmContact,
    CrmDeal,
    CrmMeeting,
    DatasetFreshness,
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    FactSegmentWindow,
    Lead,
    SyncRun,
)
from ads_engine.settings import Source, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_NOW, fixed_clock
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Then = Literal["cancel", "raise"]
type JobFactory = Callable[[jobs.JobContext], jobs._JobBase]

SCENARIO: Final = "fallback.missing_credentials.case"
"""The catalogue's clean every-source world: Google search, LinkedIn lead gen, a CRM funnel."""

FIRST_START: Final = "default"
"""The first-start world: two LinkedIn ad accounts, each listed on its own, and CRM deals."""

DISCOVERY: Final = "hubspot.google_discovery.case"
"""The catalogue's world whose HubSpot portal holds Google-touch contacts to discover."""

CAMPAIGN_PROPERTY: Final = "google_campaign_id"
TOUCH_PROPERTY: Final = "google_ads_touch_at"
"""The Google properties :data:`DISCOVERY`'s portal defines, configured for discovery."""

T0: Final = FIXED_NOW
"""The instant every dataset is built and activated at."""

T1: Final = FIXED_NOW + timedelta(hours=1)
"""The instant the job under test runs at."""

BACKFILL_DAYS: Final = 35
"""``BACKFILL_DAYS`` of every build: the default 28-day window and every lead of the world."""

BUG: Final = "a bug after the first read"
"""The text of the unexpected error a broken read raises."""

UNKNOWN_ID: Final = "987654321"
"""A LinkedIn campaign and ad account id the simulated world never reports."""

GOOGLE_FACTS: Final[tuple[Table, ...]] = (
    FactCampaignDaily.__table__,
    FactKeywordDaily.__table__,
    FactSearchTermDaily.__table__,
)
CRM_TABLES: Final[tuple[Table, ...]] = (
    CrmContact.__table__,
    CrmDeal.__table__,
    CrmMeeting.__table__,
    CrmAssociation.__table__,
    CrmAssociationFetch.__table__,
)
FRESHNESS: Final[Table] = DatasetFreshness.__table__


def _outage(source: Source, endpoint: str) -> TransientError:
    """A transient error that outlived its retries."""
    return TransientError("simulated outage", source=source, endpoint=endpoint, http_status=503)


async def _no_sleep(_seconds: float) -> None:
    """The retry policy's sleep: returns at once."""


def _comparable(value: object) -> object:
    """A column value as a hashable value: ``jsonb`` and arrays as their canonical JSON."""
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return value


# ---------------------------------------------------------------------------------------------
# The lab: datasets, contexts and database reads
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class Lab:
    """Controllers on the test database, simulating every source from one world.

    Attributes:
        sessions: The application's session factory on the emptied test database.
        world: The world every simulated client answers from.
    """

    sessions: Sessions
    world: SimulatedWorld

    def controller(self, at: datetime) -> SourceModeController:
        """A controller whose clock stands at ``at`` and whose clients never wait."""
        settings = get_settings()
        tick = [0.0]

        def clock() -> datetime:
            return at

        async def advance(seconds: float) -> None:
            tick[0] += seconds

        return SourceModeController(
            settings,
            sessionmaker=self.sessions,
            world=self.world,
            clock=clock,
            budget=GoogleCallBudget(settings, sessionmaker=self.sessions, clock=clock),
            retry=RetryPolicy(sleep=_no_sleep),
            hubspot_limiter=RateLimiter(4.0, clock=lambda: tick[0], sleep=advance),
        )

    async def build(self, source: Source) -> int:
        """Build, backfill and activate a simulated primary of ``source`` at :data:`T0`."""
        outcome = await self.controller(T0).build_dataset(
            source, "simulated", reason="credentials not configured"
        )
        assert outcome.activated, outcome
        assert outcome.dataset_id is not None
        return outcome.dataset_id

    @asynccontextmanager
    async def context(self, source: Source, dataset_id: int) -> AsyncIterator[jobs.JobContext]:
        """The active context of ``dataset_id`` at :data:`T1` on simulated clients."""
        controller = self.controller(T1)
        async with controller.open_clients(source, "simulated") as clients:
            yield await jobs.JobContext.for_dataset(controller, dataset_id, clients, now=T1)

    async def versions(self, dataset_id: int, *tables: Table) -> dict[str, set[tuple[Any, ...]]]:
        """Every row version of ``tables`` in a dataset: its ``xmin`` and every column."""
        found: dict[str, set[tuple[Any, ...]]] = {}
        async with self.sessions() as session:
            for table in tables:
                result = await session.execute(
                    select(literal_column("xmin::text"), *table.columns).where(
                        table.c.dataset_id == dataset_id
                    )
                )
                found[table.name] = {tuple(_comparable(value) for value in row) for row in result}
        return found

    async def written_by(self, xid: str, dataset_id: int, *tables: Table) -> dict[str, int]:
        """How many rows of each table in a dataset transaction ``xid`` wrote."""
        versions = await self.versions(dataset_id, *tables)
        return {name: sum(1 for row in rows if row[0] == xid) for name, rows in versions.items()}

    async def ids(self, dataset_id: int, table: Table) -> set[int]:
        """The ids of every row of ``table`` in a dataset."""
        async with self.sessions() as session:
            result = await session.scalars(
                select(table.c.id).where(table.c.dataset_id == dataset_id)
            )
            return set(result)

    async def requests(self) -> dict[int, str]:
        """Every ``analysis_request`` a sync job queued: its id and ``xmin``."""
        async with self.sessions() as session:
            result = await session.execute(
                select(AnalysisRequest.id, literal_column("xmin::text")).where(
                    AnalysisRequest.reason.startswith("sync:")
                )
            )
            return {int(request_id): str(xid) for request_id, xid in result}

    async def freshness(self, dataset_id: int) -> dict[str, DatasetFreshness]:
        """The dataset's freshness rows, by dataset name."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(DatasetFreshness).where(DatasetFreshness.dataset_id == dataset_id)
            )
            return {row.dataset: row for row in rows}

    async def closing(self, job: str, name: str) -> tuple[str, str]:
        """The status and ``xmin`` of the one ``sync_run`` row of ``job``/``name`` at T1."""
        async with self.sessions() as session:
            result = await session.execute(
                select(SyncRun.status, literal_column("xmin::text")).where(
                    SyncRun.job == job, SyncRun.dataset == name, SyncRun.started_at == T1
                )
            )
            (row,) = list(result)
        return str(row[0]), str(row[1])


@pytest.fixture
def labs(
    db: Sessions,
    make_world: Callable[..., SimulatedWorld],
    set_env: Callable[..., None],
) -> Callable[[str], Lab]:
    """Return ``labs(scenario)``: the lab on the emptied test database and that world."""
    set_env(BACKFILL_DAYS=str(BACKFILL_DAYS))

    def make(scenario: str) -> Lab:
        return Lab(sessions=db, world=make_world(scenario, clock=fixed_clock))

    return make


@pytest.fixture
def lab(labs: Callable[[str], Lab]) -> Lab:
    """The lab on the clean every-source world."""
    return labs(SCENARIO)


def _configure_discovery(set_env: Callable[..., None]) -> None:
    """Configure both Google properties of :data:`DISCOVERY`'s portal, so discovery runs."""
    set_env(
        HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY=CAMPAIGN_PROPERTY,
        HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY=TOUCH_PROPERTY,
    )


@pytest.fixture
def discovery_lab(labs: Callable[[str], Lab], set_env: Callable[..., None]) -> Lab:
    """The lab on the Google-touch discovery world, with both Google properties configured."""
    _configure_discovery(set_env)
    return labs(DISCOVERY)


# ---------------------------------------------------------------------------------------------
# Connector seams and assertions
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class Seam:
    """One connector method whose answers the test edits, by call number (1-based).

    Attributes:
        edits: Per call number, what the call does instead of answering as read: a function
            of the answer and the call's arguments, ``"raise"`` (an unexpected error) or
            ``"cancel"`` (waits until the job is cancelled).
        calls: Calls made so far.
        reached: Set when a ``"cancel"`` call starts waiting.
    """

    edits: Mapping[int, Callable[[Any, tuple[Any, ...]], Any] | Then]
    calls: int = 0
    reached: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)

    def patch(self, monkeypatch: pytest.MonkeyPatch, owner: type, method: str) -> None:
        """Route every call of ``owner.method`` through :attr:`edits`."""
        original: Callable[..., Awaitable[Any]] = getattr(owner, method)

        async def edited(client: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            edit = self.edits.get(self.calls)
            if edit == "raise":
                raise RuntimeError(BUG)
            if edit == "cancel":
                self.reached.set()
                await asyncio.get_running_loop().create_future()
            answer = await original(client, *args, **kwargs)
            return answer if edit is None or isinstance(edit, str) else edit(answer, args)

        monkeypatch.setattr(owner, method, edited)


async def _stopped(job: jobs._JobBase, then: Then, seam: Seam) -> None:
    """Run ``job`` until the seam stops it: cancelled while it waits, or by the raised bug."""
    if then == "raise":
        with pytest.raises(RuntimeError, match=BUG):
            await job.run()
        return
    task = asyncio.create_task(job.run())
    reached = asyncio.create_task(seam.reached.wait())
    try:
        await asyncio.wait({task, reached}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        reached.cancel()
    assert not task.done(), f"the job ended before the cancelled read: {task.result()}"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _fewer_rows(answer: Any, _args: tuple[Any, ...]) -> Any:
    """A complete answer without its first row: a complete-scope reconcile deletes it."""
    assert answer.rows, "the edited read returns rows"
    return dataclasses.replace(answer, rows=answer.rows[1:])


def _interrupted_fetch(outcome: FetchOutcome, args: tuple[Any, ...]) -> FetchOutcome:
    """A Google fetch that read its rows, then failed after its retries."""
    error = _outage("google", "search")
    return dataclasses.replace(
        outcome,
        completeness="incomplete",
        reason=interruption_reason(error, 1),
        deferred=(args[1],),
        interrupted_by=error,
    )


def _interrupted_listing(listing: Listing[Any], _args: tuple[Any, ...]) -> Listing[Any]:
    """A LinkedIn listing that read its rows, then failed after its retries."""
    error = _outage("linkedin", "ad_analytics")
    return dataclasses.replace(listing, complete=False, reason=error.reason, interrupted_by=error)


def assert_failed_attempt(before: DatasetFreshness, after: DatasetFreshness) -> None:
    """A failed attempt at T1: ``last_attempt_at`` moved, the last good standing stands."""
    assert after.last_attempt_at == T1, after.dataset
    assert after.last_success_at == before.last_success_at, after.dataset
    assert (after.covered_from, after.covered_through) == (
        before.covered_from,
        before.covered_through,
    ), after.dataset
    assert after.completeness == before.completeness, after.dataset
    assert after.reason is not None, after.dataset


async def assert_one_commit(
    lab: Lab,
    dataset_id: int,
    *,
    job: str,
    name: str,
    queued_before: Mapping[int, str],
    tables: Sequence[Table],
) -> dict[str, int]:
    """The failed attempt of ``name`` committed with its rows and exactly one new request.

    Returns:
        The rows of each table the failed attempt's transaction wrote.
    """
    status, xid = await lab.closing(job, name)
    assert status == "failed", name
    queued = await lab.requests()
    new = [x for request_id, x in queued.items() if request_id not in queued_before]
    assert new.count(xid) == 1, f"{name}: one request committed with the failed attempt"
    written = await lab.written_by(xid, dataset_id, *tables)
    assert any(written.values()), f"{name}: the rows read were written with the failed attempt"
    return written


# ---------------------------------------------------------------------------------------------
# Metrics and segments: (i) and (ii) nothing before the commit, (iii) one commit
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Refresh:
    """A metrics or segment refresh whose reads the tests break.

    Attributes:
        source: The dataset's source.
        owner: The connector class whose method reads one scope.
        method: That method.
        job: The job under test.
        job_name: Its ``sync_run.job``.
        name: The freshness dataset a broken read fails.
        tables: The fact tables the job writes.
        interrupt: Turns one read's answer into an interrupted one.
        scenario: The world the dataset is built from; it has a second read to break.
    """

    source: Source
    owner: type
    method: str
    job: JobFactory
    job_name: str
    name: str
    tables: tuple[Table, ...]
    interrupt: Callable[[Any, tuple[Any, ...]], Any]
    scenario: str = SCENARIO


REFRESHES: Final = [
    pytest.param(
        Refresh(
            "google",
            PartitionedFetcher,
            "partitioned_fetch",
            jobs.MetricsSyncJob,
            jobs.JOB_METRICS,
            "google.metrics_daily",
            GOOGLE_FACTS,
            _interrupted_fetch,
        ),
        id="google-metrics",
    ),
    pytest.param(
        Refresh(
            "linkedin",
            LinkedInClient,
            "analytics_daily",
            jobs.MetricsSyncJob,
            jobs.JOB_METRICS,
            "linkedin.analytics_daily",
            (FactCreativeDaily.__table__,),
            _interrupted_listing,
            # One listing per ad account: the second account's is the second read.
            scenario=FIRST_START,
        ),
        id="linkedin-metrics",
    ),
    pytest.param(
        Refresh(
            "linkedin",
            LinkedInClient,
            "analytics_segments",
            jobs.SegmentSyncJob,
            jobs.JOB_SEGMENTS,
            "linkedin.segments",
            (FactSegmentWindow.__table__,),
            _interrupted_listing,
        ),
        id="linkedin-segments",
    ),
]


@pytest.mark.parametrize("then", ["cancel", "raise"])
@pytest.mark.parametrize("refresh", REFRESHES)
async def test_a_refresh_stopped_after_its_first_read_changes_nothing(
    labs: Callable[[str], Lab], monkeypatch: pytest.MonkeyPatch, refresh: Refresh, then: Then
) -> None:
    """Nothing before the commit: a cancelled or raising refresh leaves every row as it was.

    The first read returns all but one row of a complete scope, which a per-read commit would
    upsert and reconcile (deleting the row left out); the second read is cancelled or raises.
    Every fact row version (``xmin`` included) and every freshness row stands, and no request
    is queued.
    """
    lab = labs(refresh.scenario)
    dataset_id = await lab.build(refresh.source)
    tables = (*refresh.tables, FRESHNESS)
    before = await lab.versions(dataset_id, *tables)
    assert all(before[table.name] for table in refresh.tables), "the build stored facts"
    queued = await lab.requests()
    seam = Seam({1: _fewer_rows, 2: then})
    seam.patch(monkeypatch, refresh.owner, refresh.method)

    async with lab.context(refresh.source, dataset_id) as ctx:
        await _stopped(refresh.job(ctx), then, seam)

    assert seam.calls == 2, "stopped at the second read"
    assert await lab.versions(dataset_id, *tables) == before
    assert await lab.requests() == queued


@pytest.mark.parametrize("refresh", REFRESHES)
async def test_a_transient_interruption_commits_the_rows_read_with_the_failed_attempt(
    labs: Callable[[str], Lab], monkeypatch: pytest.MonkeyPatch, refresh: Refresh
) -> None:
    """One commit: rows read, reconcile deletions, failed attempt and request together.

    The first read returns all but one row of a complete scope; the second read fails after
    its retries with the rows it read. One transaction writes both reads' rows, deletes the
    row the complete first read left out, records the failed attempt (the last good standing
    stands) and queues the rerun.
    """
    lab = labs(refresh.scenario)
    dataset_id = await lab.build(refresh.source)
    first = refresh.tables[0]
    ids_before = await lab.ids(dataset_id, first)
    rows_before = await lab.freshness(dataset_id)
    queued = await lab.requests()
    seam = Seam({1: _fewer_rows, 2: refresh.interrupt})
    seam.patch(monkeypatch, refresh.owner, refresh.method)

    async with lab.context(refresh.source, dataset_id) as ctx:
        result = await refresh.job(ctx).run()

    assert result.status == "failed"
    assert not result.freshness[refresh.name].succeeded
    assert_failed_attempt(
        rows_before[refresh.name], (await lab.freshness(dataset_id))[refresh.name]
    )
    await assert_one_commit(
        lab,
        dataset_id,
        job=refresh.job_name,
        name=refresh.name,
        queued_before=queued,
        tables=refresh.tables,
    )
    assert ids_before - await lab.ids(dataset_id, first), "the complete first read reconciled"


# ---------------------------------------------------------------------------------------------
# CRM: (iv)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("then", ["cancel", "raise"])
async def test_a_crm_sync_stopped_after_discovery_commits_no_discovered_contact(
    discovery_lab: Lab, monkeypatch: pytest.MonkeyPatch, then: Then
) -> None:
    """Nothing before the commit: discovered contacts wait for the contact refresh's commit.

    Discovery reads the Google-touch contacts; the matching pass after it is cancelled or
    raises. No contact row version, no other CRM row and no freshness row changes, and no
    request is queued.
    """
    lab = discovery_lab
    dataset_id = await lab.build("hubspot")
    tables = (*CRM_TABLES, FRESHNESS)
    before = await lab.versions(dataset_id, *tables)
    assert before[CrmContact.__tablename__], "the build discovered contacts"
    queued = await lab.requests()
    discovered: list[jobs._Discovery] = []
    discover = jobs.CrmSyncJob._discover
    seam = Seam({1: then})

    async def recorded(job: jobs.CrmSyncJob, *args: Any, **kwargs: Any) -> jobs._Discovery:
        discovery = await discover(job, *args, **kwargs)
        discovered.append(discovery)
        return discovery

    async def match(job: jobs.CrmSyncJob, _props: Any) -> None:
        seam.calls += 1
        if then == "raise":
            raise RuntimeError(BUG)
        seam.reached.set()
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(jobs.CrmSyncJob, "_discover", recorded)
    monkeypatch.setattr(jobs.CrmSyncJob, "_match", match)

    async with lab.context("hubspot", dataset_id) as ctx:
        await _stopped(jobs.CrmSyncJob(ctx), then, seam)

    (discovery,) = discovered
    assert discovery.found, "discovery read contacts"
    assert await lab.versions(dataset_id, *tables) == before
    assert await lab.requests() == queued


def _interrupted_search(outcome: SearchOutcome, _args: tuple[Any, ...]) -> SearchOutcome:
    """A CRM search that read its rows, then failed after its retries."""
    error = _outage("hubspot", "crm_search")
    assert outcome.rows, "the interrupted search read rows"
    return dataclasses.replace(
        outcome, completeness="incomplete", reason=error.reason, interrupted_by=error
    )


def _interrupted_batch(batch: ContactBatch, _args: tuple[Any, ...]) -> ContactBatch:
    """A contact refresh by id that read its first contact, then failed after its retries."""
    first, *unread = sorted(batch.records)
    assert unread, "the interrupted refresh reads more than one contact"
    return dataclasses.replace(
        batch,
        records={first: batch.records[first]},
        read_status={first: "ok", **dict.fromkeys(unread, "error")},
        interrupted_by=_outage("hubspot", "objects_batch_read"),
    )


def _interrupted_associations(fetch: AssociationFetch, _args: tuple[Any, ...]) -> AssociationFetch:
    """An association read that read its inputs, then failed after its retries."""
    assert fetch.per_input, "the interrupted association read had inputs"
    return dataclasses.replace(fetch, interrupted_by=_outage("hubspot", "associations_batch_read"))


@dataclasses.dataclass(frozen=True, slots=True)
class CrmBreak:
    """A CRM read the test interrupts.

    Attributes:
        method: The ``HubSpotClient`` method.
        call: Which of its calls is interrupted (1-based); the others answer as read.
        edit: Turns that call's answer into an interrupted one.
        name: The freshness dataset the interruption fails.
        tables: The tables its transaction writes the rows read to.
        companion: A further dataset the same transaction fails, if any.
        scenario: The world the HubSpot dataset is built from; discovery runs in
            :data:`DISCOVERY` alone, whose Google properties are then configured.
    """

    method: str
    call: int
    edit: Callable[[Any, tuple[Any, ...]], Any]
    name: str
    tables: tuple[Table, ...]
    companion: str | None = None
    scenario: str = DISCOVERY


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param(
            # The contact search is discovery's; the deal search runs later (step 4).
            CrmBreak(
                "search_partitioned",
                1,
                _interrupted_search,
                "hubspot.google_contacts",
                (CrmContact.__table__,),
            ),
            id="discovery",
        ),
        pytest.param(
            CrmBreak(
                "contacts_by_id",
                1,
                _interrupted_batch,
                "hubspot.contacts",
                (CrmContact.__table__,),
                companion="hubspot.google_contacts",
            ),
            id="contact-refresh",
        ),
        pytest.param(
            # The first-start world's funnel has deals to search; it runs no discovery.
            CrmBreak(
                "search_partitioned",
                1,
                _interrupted_search,
                "hubspot.deal_search",
                (CrmDeal.__table__,),
                scenario=FIRST_START,
            ),
            id="deal-search",
        ),
        pytest.param(
            CrmBreak(
                "associations",
                1,
                _interrupted_associations,
                "hubspot.associations",
                (
                    CrmAssociationFetch.__table__,
                    CrmAssociation.__table__,
                    CrmMeeting.__table__,
                    CrmDeal.__table__,
                    CrmContact.__table__,
                ),
            ),
            id="associations",
        ),
    ],
)
async def test_a_crm_interruption_commits_the_rows_read_with_the_failed_attempt(
    labs: Callable[[str], Lab],
    set_env: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    broken: CrmBreak,
) -> None:
    """One commit: a CRM step's rows read, failed attempt and request together.

    The step's read fails after its retries with what it read. Its transaction stores the
    rows read, records the failed attempt of its dataset (and of the Google-touch dataset
    when a contact refresh cost a Google-touch read), closes the ``sync_run`` row ``failed``
    and queues the rerun; every other dataset records its success.
    """
    if broken.scenario == DISCOVERY:
        _configure_discovery(set_env)
    lab = labs(broken.scenario)
    dataset_id = await lab.build("hubspot")
    rows_before = await lab.freshness(dataset_id)
    queued = await lab.requests()
    seam = Seam({broken.call: broken.edit})
    seam.patch(monkeypatch, HubSpotClient, broken.method)

    async with lab.context("hubspot", dataset_id) as ctx:
        # A backfill searches deals and Google-touch contacts over the whole staging range.
        result = await jobs.CrmSyncJob(ctx, backfill=True).run()

    assert seam.calls >= broken.call
    assert result.status == "failed"
    failed = {broken.name} | ({broken.companion} if broken.companion else set())
    assert {name for name, outcome in result.freshness.items() if not outcome.succeeded} == failed
    rows_after = await lab.freshness(dataset_id)
    for name in failed:
        assert_failed_attempt(rows_before[name], rows_after[name])
    for name in set(result.freshness) - failed:
        assert rows_after[name].last_success_at == T1, name
    await assert_one_commit(
        lab,
        dataset_id,
        job=jobs.JOB_CRM,
        name=broken.name,
        queued_before=queued,
        tables=broken.tables,
    )
    if broken.companion is not None:
        _, xid = await lab.closing(jobs.JOB_CRM, broken.name)
        assert await lab.closing(jobs.JOB_CRM, broken.companion) == ("failed", xid)


# ---------------------------------------------------------------------------------------------
# Leads: (v)
# ---------------------------------------------------------------------------------------------


def _interrupted_leads(listing: LeadListing, _args: tuple[Any, ...]) -> LeadListing:
    """A lead listing that read its leads, then failed after its retries."""
    assert listing.leads, "the interrupted listing read leads"
    error = _outage("linkedin", "lead_form_responses")
    return dataclasses.replace(listing, complete=False, reason=error.reason, interrupted_by=error)


def _unplaceable_lead(listing: LeadListing, _args: tuple[Any, ...]) -> LeadListing:
    """A lead listing whose first lead names a campaign and ad account nobody stored."""
    assert listing.leads, "the listing read leads"
    first, *rest = listing.leads
    unplaceable = dataclasses.replace(
        first,
        campaign_urn=f"{PREFIX_CAMPAIGN}{UNKNOWN_ID}",
        account_urn=f"{PREFIX_ACCOUNT}{UNKNOWN_ID}",
    )
    return dataclasses.replace(listing, leads=(unplaceable, *rest))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(_interrupted_leads, id="transient"),
        pytest.param(_unplaceable_lead, id="unplaced"),
    ],
)
async def test_a_failed_lead_attempt_queues_the_request_its_stored_leads_need(
    lab: Lab, monkeypatch: pytest.MonkeyPatch, edit: Callable[[Any, tuple[Any, ...]], Any]
) -> None:
    """One commit: the leads stored by a failed attempt, the attempt and its request together.

    A transient interruption of the first account's listing, or a lead that cannot be
    placed, fails ``linkedin.leads`` (the cursor stands), and the transaction that stored the
    attempt's leads records it and queues the rerun.
    """
    dataset_id = await lab.build("linkedin")
    rows_before = await lab.freshness(dataset_id)
    queued = await lab.requests()
    seam = Seam({1: edit})
    seam.patch(monkeypatch, LinkedInClient, "list_lead_responses")

    async with lab.context("linkedin", dataset_id) as ctx:
        result = await jobs.LeadSyncJob(ctx, backfill=True).run()

    assert result.status == "failed"
    assert not result.freshness["linkedin.leads"].succeeded
    assert_failed_attempt(
        rows_before["linkedin.leads"], (await lab.freshness(dataset_id))["linkedin.leads"]
    )
    written = await assert_one_commit(
        lab,
        dataset_id,
        job=jobs.JOB_LEADS,
        name="linkedin.leads",
        queued_before=queued,
        tables=(Lead.__table__,),
    )
    assert written[Lead.__tablename__] > 0
