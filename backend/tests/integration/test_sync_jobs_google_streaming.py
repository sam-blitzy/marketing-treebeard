"""Bounded Google metrics writes behind an n8n relay that ignores page tokens.

The live n8n workflow answers a follow-up call with page 1 again under a fresh
``nextPageToken``. The webhook client abandons such a token and reports the partition capped,
``PartitionedFetcher`` narrows dates and id ranges until no partition needs a second page, and
:class:`~ads_engine.sync.jobs.MetricsSyncJob` streams the accepted rows into a write buffer per
freshness dataset, which it upserts in a short transaction of its own whenever it holds
``_GOOGLE_WRITE_BUFFER_ROWS`` rows; for an active dataset that transaction also queues the
``analysis_request`` its rows need, and its rows count toward the job once it commits. The
recording transaction writes the rest, sweeps the ranges proven complete and publishes the
checkpoint, the outcome, the ``sync_run`` close and its own ``analysis_request``. Proven on the
PostgreSQL test database (``db`` of ``tests/conftest.py``) with the production jobs, the
simulated Google account behind :class:`IgnoringRelay`, and the buffer bound shrunk to
:data:`BOUND` so the simulated volumes flush many times:

* a backfill stores exactly the facts a plain read stored, every row rewritten once, completes,
  flushes in several transactions that each hold at least the bound and less than the bound
  plus one partition, and publishes each dataset's outcome with its ``sync_run`` close;
* a complete restatement deletes the absent rows inside its range only, flushes included, and
  leaves every row before its range as stored;
* a defect raised after a flush leaves the one ``analysis_request`` the flush queued with its
  rows and records the failed attempt with none, while the same defect before any flush queues
  none;
* a cancellation after a flush leaves the flushed upserts and their request but no freshness or
  checkpoint change;
* a sync cancelled, or failing unexpectedly, after a flush of changed rows keeps those rows
  with exactly one request, publishes no freshness or checkpoint, and reports the flushed rows
  in its failed ``sync_run`` rows, ``sync.job.finish`` and ``sync.job.cancelled``;
* a large run counts each row it writes exactly once, flushed or recorded;
* a building dataset's backfill flushes without queueing any request.

Every clock is fixed (:data:`T0` builds the dataset, :data:`T1` runs the job under test),
retries never wait in real time, and no test sleeps.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, NamedTuple

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.google_n8n.client import (
    SIMULATED_SECRET,
    SIMULATED_WEBHOOK_URL,
    UNMETERED,
    GoogleAdsWebhookClient,
)
from ads_engine.connectors.google_n8n.queries import parse_query
from ads_engine.connectors.http import RetryPolicy
from ads_engine.db import repositories
from ads_engine.db.base import utcnow
from ads_engine.db.models import (
    AnalysisRequest,
    DatasetFreshness,
    FactCampaignDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    SyncRun,
)
from ads_engine.db.models.accounts import AdAccount, AdGroup, Campaign, Keyword
from ads_engine.settings import get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_NOW, fixed_clock
from ads_engine.simulation.transport import mock_transport
from ads_engine.sync import jobs
from ads_engine.sync.mode import SourceClients, SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Hook = Callable[[int], Awaitable[httpx.Response | None]]

T0: Final = FIXED_NOW
"""The instant the dataset is built and activated at."""

T1: Final = FIXED_NOW + timedelta(hours=1)
"""The instant the job under test runs at."""

BACKFILL_DAYS: Final = 35
"""``BACKFILL_DAYS`` of every build: longer than Google's 30-day restatement."""

PAGE_ROWS: Final = 15
"""The relay's page size: below a simulated day of search terms, above any (day, ad group)."""

BOUND: Final = 40
"""``_GOOGLE_WRITE_BUFFER_ROWS`` while the job under test runs."""

METRICS: Final = "google.metrics_daily"
TERMS: Final = "google.search_terms"

FACT_MODELS: Final = (FactCampaignDaily, FactKeywordDaily, FactSearchTermDaily)

ABSENT_CAMPAIGN: Final = "9876543210"
ABSENT_CRITERION: Final = "987654321"
ABSENT_TERM: Final = "streaming test absent term"
"""Ids and a search term the simulated Google account never reports."""

INVALID_ARGUMENT: Final = {
    "error": {
        "code": 400,
        "message": "Request contains an invalid argument.",
        "status": "INVALID_ARGUMENT",
    }
}
"""n8n relaying a Google request error, which the client raises as a ``QueryDefect``."""


async def _no_sleep(seconds: float) -> None:
    """Skip the backoff between retries; every attempt still happens and is counted."""


RETRY: Final = RetryPolicy(sleep=_no_sleep)


# ---------------------------------------------------------------------------------------------
# The relay and the rig
# ---------------------------------------------------------------------------------------------


class IgnoringRelay(httpx.AsyncBaseTransport):
    """The live n8n relay's paging in front of the simulated Google account.

    Every query without ``ORDER BY`` is answered with its first :data:`PAGE_ROWS` rows and,
    while more rows match, a fresh ``nextPageToken``; a token sent back is ignored and
    answered the same way, page 1 again under a new token. A keyset page (``ORDER BY ...
    LIMIT 9999``) fits the live relay's 10,000-row page, so it passes as the account answers.

    Attributes:
        calls: Requests received.
        tokens_issued: Tokens handed out.
        tokens_received: Requests that sent a token back.
        hook: Awaited with each request's 1-based ordinal before it is answered; a response it
            returns is sent instead, and it may wait to stop the job at that call.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.calls = 0
        self.tokens_issued = 0
        self.tokens_received = 0
        self.hook: Hook | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Answer one webhook POST as the live relay pages it."""
        self.calls += 1
        if self.hook is not None:
            replaced = await self.hook(self.calls)
            if replaced is not None:
                return replaced
        body = json.loads(request.content)
        if body["page_token"] is not None:
            self.tokens_received += 1
            body["page_token"] = None
        headers = {key: value for key, value in request.headers.items() if key != "content-length"}
        response = await self._inner.handle_async_request(
            httpx.Request(request.method, request.url, headers=headers, json=body)
        )
        if response.status_code != 200 or parse_query(body["query"]).order_by is not None:
            return response
        await response.aread()
        envelope = json.loads(response.content)
        rows = envelope.get("results", [])
        if len(rows) > PAGE_ROWS:
            self.tokens_issued += 1
            envelope["results"] = rows[:PAGE_ROWS]
            envelope["nextPageToken"] = f"relay-token-{self.tokens_issued}"
        return httpx.Response(200, json=envelope)


@dataclass(slots=True)
class Rig:
    """A simulated Google world on the test database, and the flushes the job under test made.

    Attributes:
        sessions: The application's session factory on the emptied test database.
        world: The world the simulated account answers from.
        flushes: ``(dataset, rows held)`` of every flush, in order, once instrumented.
    """

    sessions: Sessions
    world: SimulatedWorld
    flushes: list[tuple[str, int]] = field(default_factory=list)

    def controller(self, at: datetime) -> SourceModeController:
        """A controller whose clock stands at ``at`` and whose clients never wait."""
        return SourceModeController(
            get_settings(),
            sessionmaker=self.sessions,
            world=self.world,
            clock=lambda: at,
            retry=RETRY,
        )

    async def build(self) -> int:
        """Build, backfill and activate a simulated Google primary at :data:`T0`."""
        outcome = await self.controller(T0).build_dataset(
            "google", "simulated", reason="credentials not configured"
        )
        assert outcome.activated, outcome
        assert outcome.dataset_id is not None
        return outcome.dataset_id

    def instrument(self, monkeypatch: pytest.MonkeyPatch, bound: int = BOUND) -> None:
        """Shrink the buffer bound to ``bound`` and record each flush with the rows it held."""
        monkeypatch.setattr(jobs, "_GOOGLE_WRITE_BUFFER_ROWS", bound)
        original = jobs.MetricsSyncJob._flush_google

        async def recorded(job: jobs.MetricsSyncJob, writes: jobs._DatasetWrites) -> None:
            self.flushes.append((writes.name, writes.held))
            await original(job, writes)

        monkeypatch.setattr(jobs.MetricsSyncJob, "_flush_google", recorded)

    def relay(self) -> IgnoringRelay:
        """A token-ignoring relay in front of the world's simulated Google account."""
        return IgnoringRelay(mock_transport("google", self.world, settings=get_settings()))

    @asynccontextmanager
    async def context(
        self, dataset_id: int, relay: IgnoringRelay
    ) -> AsyncIterator[jobs.JobContext]:
        """The context of ``dataset_id`` at :data:`T1`, its Google client behind ``relay``."""
        settings = get_settings()
        async with httpx.AsyncClient(transport=relay) as http:
            client = GoogleAdsWebhookClient(
                http=http,
                budget=UNMETERED,
                webhook_url=SIMULATED_WEBHOOK_URL,
                secret_header=settings.n8n_webhook_secret_header,
                secret=SecretStr(SIMULATED_SECRET),
                customer_id=self.world.google_customer_id,
                retry=RETRY,
            )
            clients = SourceClients("google", "simulated", google=client)
            yield await jobs.JobContext.for_dataset(
                self.controller(T1), dataset_id, clients, now=T1
            )

    def held(self, name: str) -> list[int]:
        """The rows each flush of dataset ``name`` held, in order."""
        return [rows for flushed, rows in self.flushes if flushed == name]


@pytest.fixture
def rig(
    db: Sessions, make_world: Callable[..., SimulatedWorld], set_env: Callable[..., None]
) -> Rig:
    """The rig on the emptied test database and the default world at the fixed clock."""
    set_env(BACKFILL_DAYS=str(BACKFILL_DAYS))
    return Rig(sessions=db, world=make_world("default", clock=fixed_clock))


class LogRecorder:
    """Stand-in for ``jobs._log``: records every ``info``, ``warning`` and ``error`` event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event_name: str, **fields: Any) -> None:
        """Record an ``info`` event."""
        self.events.append(("info", event_name, fields))

    def warning(self, event_name: str, **fields: Any) -> None:
        """Record a ``warning`` event."""
        self.events.append(("warning", event_name, fields))

    def error(self, event_name: str, **fields: Any) -> None:
        """Record an ``error`` event."""
        self.events.append(("error", event_name, fields))

    def named(self, event_name: str) -> list[dict[str, Any]]:
        """The fields of every recorded ``event_name`` event, in order."""
        return [fields for _level, name, fields in self.events if name == event_name]

    def flushed(self) -> dict[str, int]:
        """Rows each freshness dataset's flushes wrote, from ``sync.metrics.google_flushed``."""
        rows: dict[str, int] = {METRICS: 0, TERMS: 0}
        for fields in self.named("sync.metrics.google_flushed"):
            rows[fields["dataset"]] += fields["rows"]
        return rows


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> LogRecorder:
    """Replace the jobs module's logger with a recorder for the test."""
    recorder = LogRecorder()
    monkeypatch.setattr(jobs, "_log", recorder)
    return recorder


# ---------------------------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------------------------


class Fact(NamedTuple):
    """One stored fact row: its values but ``id`` and ``fetched_at``, those two, and ``xmin``."""

    values: tuple[Any, ...]
    fetched_at: datetime
    xmin: str


type Facts = dict[str, dict[tuple[Any, ...], Fact]]


async def stored(rig: Rig, dataset_id: int) -> Facts:
    """Every Google fact row of a dataset: per table, natural key -> :class:`Fact`."""
    found: Facts = {}
    async with rig.sessions() as session:
        for model in FACT_MODELS:
            table = model.__table__
            keys = [table.c[name] for name in model.__natural_key__]
            others = [column for column in table.columns if column.name not in {"id", "fetched_at"}]
            result = await session.execute(
                select(literal_column("xmin::text").label("xmin"), *table.columns).where(
                    table.c.dataset_id == dataset_id
                )
            )
            found[table.name] = {
                tuple(row._mapping[column] for column in keys): Fact(
                    values=tuple(row._mapping[column] for column in others),
                    fetched_at=row._mapping[table.c.fetched_at],
                    xmin=str(row._mapping["xmin"]),
                )
                for row in result
            }
    return found


def values_of(facts: Facts) -> dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]:
    """The stored values only, without when or by which transaction they were written."""
    return {
        table: {key: fact.values for key, fact in rows.items()} for table, rows in facts.items()
    }


async def freshness_rows(rig: Rig, dataset_id: int) -> dict[str, tuple[Any, ...]]:
    """Every column of each freshness row of a dataset, and its ``xmin``, by dataset name."""
    table = DatasetFreshness.__table__
    async with rig.sessions() as session:
        result = await session.execute(
            select(literal_column("xmin::text").label("xmin"), *table.columns).where(
                table.c.dataset_id == dataset_id
            )
        )
        return {row._mapping[table.c.dataset]: tuple(row) for row in result}


async def requests(rig: Rig) -> dict[int, str]:
    """Every ``analysis_request`` a sync job queued: its id and ``xmin``."""
    async with rig.sessions() as session:
        result = await session.execute(
            select(AnalysisRequest.id, literal_column("xmin::text")).where(
                AnalysisRequest.reason.startswith("sync:")
            )
        )
        return {int(request_id): str(xid) for request_id, xid in result}


async def run_closings(rig: Rig, name: str) -> set[tuple[str, str]]:
    """The status and ``xmin`` of every ``sync_run`` row of dataset ``name`` started at T1."""
    async with rig.sessions() as session:
        result = await session.execute(
            select(SyncRun.status, literal_column("xmin::text")).where(
                SyncRun.dataset == name, SyncRun.started_at == T1
            )
        )
        return {(str(status), str(xid)) for status, xid in result}


async def run_rows(rig: Rig, name: str) -> list[tuple[str, int | None]]:
    """The status and ``rows`` of every ``sync_run`` row of dataset ``name`` started at T1."""
    async with rig.sessions() as session:
        result = await session.execute(
            select(SyncRun.status, SyncRun.rows)
            .where(SyncRun.dataset == name, SyncRun.started_at == T1)
            .order_by(SyncRun.id)
        )
        return [(str(status), rows) for status, rows in result]


def written_by(facts: Facts, xid: str) -> set[tuple[str, tuple[Any, ...]]]:
    """The ``(table, natural key)`` of every fact row transaction ``xid`` last wrote."""
    return {
        (table, key)
        for table, rows in facts.items()
        for key, fact in rows.items()
        if fact.xmin == xid
    }


def rewritten_since(facts: Facts, since: datetime) -> dict[str, int]:
    """Per freshness dataset, the fact rows written at or after ``since``."""
    tables = {
        METRICS: (FactCampaignDaily.__tablename__, FactKeywordDaily.__tablename__),
        TERMS: (FactSearchTermDaily.__tablename__,),
    }
    return {
        name: sum(fact.fetched_at >= since for table in names for fact in facts[table].values())
        for name, names in tables.items()
    }


async def shift_costs(rig: Rig, dataset_id: int) -> None:
    """Add one to the stored cost of every Google fact of the dataset, so a re-read changes it."""
    async with rig.sessions() as session, session.begin():
        for model in FACT_MODELS:
            await session.execute(
                update(model)
                .where(model.dataset_id == dataset_id)
                .values(cost=model.cost + Decimal("1"))
            )


async def stored_days(rig: Rig, dataset_id: int) -> tuple[date, date]:
    """The first and last native day of the dataset's stored campaign days."""
    async with rig.sessions() as session:
        first, last = (
            await session.execute(
                select(func.min(FactCampaignDaily.date), func.max(FactCampaignDaily.date)).where(
                    FactCampaignDaily.dataset_id == dataset_id
                )
            )
        ).one()
    return first, last


async def inject_absent(rig: Rig, dataset_id: int, days: tuple[date, date]) -> None:
    """Store one fact the account never reports per table on each of ``days``."""
    money = {"cost": Decimal("3.000000"), "impressions": 30, "clicks": 3}
    async with rig.sessions() as session, session.begin():
        account = (
            await session.scalars(select(AdAccount.id).where(AdAccount.dataset_id == dataset_id))
        ).one()
        group = (
            await session.execute(
                select(AdGroup.id, AdGroup.campaign_id)
                .where(AdGroup.dataset_id == dataset_id)
                .order_by(AdGroup.id)
                .limit(1)
            )
        ).one()
        (campaign_id,) = (
            await repositories.upsert_entities(
                session,
                Campaign,
                [
                    {
                        "account_id": account,
                        "external_id": ABSENT_CAMPAIGN,
                        "name": f"Campaign {ABSENT_CAMPAIGN}",
                        "status": "ended",
                        "raw_status": "UNKNOWN",
                    }
                ],
                dataset_id,
            )
        ).values()
        (keyword_id,) = (
            await repositories.upsert_entities(
                session,
                Keyword,
                [
                    {
                        "ad_group_id": group.id,
                        "criterion_id": ABSENT_CRITERION,
                        "text": "absent keyword",
                        "match_type": "EXACT",
                        "status": "ENABLED",
                    }
                ],
                dataset_id,
            )
        ).values()
        rows: dict[type[Any], list[dict[str, Any]]] = {
            FactCampaignDaily: [{"campaign_id": campaign_id, "date": day, **money} for day in days],
            FactKeywordDaily: [{"keyword_id": keyword_id, "date": day, **money} for day in days],
            FactSearchTermDaily: [
                {
                    "ad_group_id": group.id,
                    "campaign_id": group.campaign_id,
                    "date": day,
                    "search_term": ABSENT_TERM,
                    "term_status": "NONE",
                    "matched_criterion_id": ABSENT_CRITERION,
                    **money,
                }
                for day in days
            ],
        }
        for model, facts in rows.items():
            await repositories.upsert_facts(model, facts, dataset_id, session=session)


async def inject(
    rig: Rig, dataset_id: int, days: tuple[date, date]
) -> dict[str, tuple[tuple[Any, ...], tuple[Any, ...]]]:
    """Inject absent facts on ``days`` (inside, outside) and return each table's two keys."""
    existing = await stored(rig, dataset_id)
    await inject_absent(rig, dataset_id, days)
    keys: dict[str, tuple[tuple[Any, ...], tuple[Any, ...]]] = {}
    for table, rows in (await stored(rig, dataset_id)).items():
        by_day = {key[-1]: key for key in rows if key not in existing[table]}
        assert set(by_day) == set(days), table
        keys[table] = (by_day[days[0]], by_day[days[1]])
    return keys


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


async def test_a_backfill_behind_a_relay_ignoring_page_tokens_stores_every_row_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row is read by partitioning, written in bounded flushes, and published once.

    The relay's first token is sent back once, proves ignored and is abandoned; no later
    token is followed. The backfill ends ``complete`` with exactly the facts the plain build
    stored, each rewritten by this run. Each flush held at least :data:`BOUND` rows and less
    than one more partition, and each dataset's outcome commits with its ``sync_run`` close.
    """
    dataset_id = await rig.build()
    before = await stored(rig, dataset_id)
    assert all(before.values()), "the build stored facts in every table"
    rig.instrument(monkeypatch)
    relay = rig.relay()
    started = utcnow()

    async with rig.context(dataset_id, relay) as ctx:
        result = await jobs.MetricsSyncJob(ctx, "backfill").run()

    assert result.status == "succeeded", result.error
    assert {name: outcome.completeness for name, outcome in result.freshness.items()} == {
        METRICS: "complete",
        TERMS: "complete",
    }
    assert relay.tokens_issued > 1
    assert relay.tokens_received == 1, "the first ignored token teaches the client"
    after = await stored(rig, dataset_id)
    assert values_of(after) == values_of(before)
    assert all(fact.fetched_at >= started for rows in after.values() for fact in rows.values())
    for name in (METRICS, TERMS):
        held = rig.held(name)
        assert len(held) >= 2, f"{name}: the read outgrew the buffer several times"
        assert all(BOUND <= rows < BOUND + PAGE_ROWS for rows in held), (name, held)
    writers = {fact.xmin for rows in after.values() for fact in rows.values()}
    assert len(writers) > len(FACT_MODELS), "the facts were committed by several transactions"
    published = await freshness_rows(rig, dataset_id)
    for name in (METRICS, TERMS):
        xmin = published[name][0]
        assert await run_closings(rig, name) == {("succeeded", xmin)}, name


async def test_a_complete_restatement_deletes_absent_rows_only_inside_its_range(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flushed or not, a complete re-read sweeps exactly the absent rows of its own range.

    One fact per table that the account never reports is stored on the last day of the build
    and on its first day, before the 30-day restatement range. The restatement over the
    relay flushes several times; the first injected row of each table is gone, the second
    stays, and every row dated before the range stands exactly as stored.
    """
    dataset_id = await rig.build()
    first, last = await stored_days(rig, dataset_id)
    injected = await inject(rig, dataset_id, (last, first))
    before = await stored(rig, dataset_id)
    rig.instrument(monkeypatch)

    async with rig.context(dataset_id, rig.relay()) as ctx:
        result = await jobs.MetricsSyncJob(ctx, "restatement").run()

    assert result.status == "succeeded", result.error
    assert all(outcome.completeness == "complete" for outcome in result.freshness.values())
    assert len(rig.held(METRICS)) >= 2
    assert len(rig.held(TERMS)) >= 2
    after = await stored(rig, dataset_id)
    # The restatement reads the 30 native days ending on the build's last day (today).
    start = last - timedelta(days=jobs.RESTATEMENT_DAYS["google"] - 1)
    assert first < start, "the build reaches before the restatement range"
    for table, (inside, outside) in injected.items():
        assert inside not in after[table], f"{table}: the absent row inside the range is gone"
        assert after[table][outside] == before[table][outside], f"{table}: the outside row stays"
        older = {key: fact for key, fact in before[table].items() if key[-1] < start}
        assert older, f"{table}: the build stored days before the range"
        assert {key: after[table][key] for key in older} == older, table


@pytest.mark.parametrize("flushed", [True, False], ids=["after-a-flush", "before-any-flush"])
async def test_a_defect_queues_the_request_only_for_rows_a_flush_wrote(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, flushed: bool
) -> None:
    """A defect after a flush leaves the one request the flush queued; before any, none.

    The relay answers the first call after a flush (or the very first call) with HTTP 400,
    which the client raises as a ``QueryDefect``. The job records the failed attempt of every
    dataset not yet recorded. The flush queued the ``analysis_request`` its rows need in the
    transaction that upserted them, so the failed attempt, which writes no row, queues none.
    """
    dataset_id = await rig.build()
    rig.instrument(monkeypatch, BOUND if flushed else jobs._GOOGLE_WRITE_BUFFER_ROWS)
    relay = rig.relay()

    async def defect(_call: int) -> httpx.Response | None:
        due = bool(rig.flushes) or not flushed
        return httpx.Response(400, json=INVALID_ARGUMENT) if due else None

    relay.hook = defect
    queued = await requests(rig)

    async with rig.context(dataset_id, relay) as ctx:
        result = await jobs.MetricsSyncJob(ctx, "backfill").run()

    assert result.status == "failed"
    assert result.disposition == "defect"
    failed = {name for name, outcome in result.freshness.items() if not outcome.succeeded}
    assert failed, "the defect failed the datasets not yet recorded"
    new = {
        request_id: xid
        for request_id, xid in (await requests(rig)).items()
        if request_id not in queued
    }
    if not flushed:
        assert rig.flushes == []
        assert new == {}, "a defect that wrote nothing queues nothing"
        return
    assert {name for name, _rows in rig.flushes} & failed, "a failed dataset holds flushed rows"
    assert len(new) == 1
    (xid,) = new.values()
    assert written_by(await stored(rig, dataset_id), xid), "queued with the flushed upserts"
    published = await freshness_rows(rig, dataset_id)
    assert xid not in {published[name][0] for name in failed}, "the failed attempt queues none"


async def test_a_cancellation_after_a_flush_publishes_only_the_flushed_upserts_and_their_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled after a flush: those upserts and their request stand; freshness does not move.

    The relay stops the job at the first call after a flush. The flushed rows are restated
    platform values, equal to those stored; no fact row is deleted, no freshness row (its
    checkpoint included) changes, the one request queued is the flush's own and no
    ``sync_run`` row succeeds.
    """
    dataset_id = await rig.build()
    before = await stored(rig, dataset_id)
    fresh_before = await freshness_rows(rig, dataset_id)
    queued = await requests(rig)
    rig.instrument(monkeypatch)
    relay = rig.relay()
    reached = asyncio.Event()

    async def stall(_call: int) -> httpx.Response | None:
        if rig.flushes:
            reached.set()
            await asyncio.get_running_loop().create_future()
        return None

    relay.hook = stall

    async with rig.context(dataset_id, relay) as ctx:
        task = asyncio.create_task(jobs.MetricsSyncJob(ctx, "backfill").run())
        waiting = asyncio.create_task(reached.wait())
        try:
            await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiting.cancel()
        assert not task.done(), f"the job ended before the stalled call: {task.result()}"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    after = await stored(rig, dataset_id)
    assert values_of(after) == values_of(before), "only restated, equal values were upserted"
    rewritten = sum(
        after[table][key].xmin != fact.xmin
        for table, rows in before.items()
        for key, fact in rows.items()
    )
    assert rewritten >= BOUND, "the flushed upserts stand"
    assert await freshness_rows(rig, dataset_id) == fresh_before
    new = {key: xid for key, xid in (await requests(rig)).items() if key not in queued}
    assert len(new) == 1, "the flush queued its request"
    (xid,) = new.values()
    assert len(written_by(after, xid)) >= BOUND, "queued with the flushed upserts"
    for name in (METRICS, TERMS):
        assert all(status != "succeeded" for status, _xid in await run_closings(rig, name)), name


@pytest.mark.parametrize("stop", ["cancelled", "raised"])
async def test_a_sync_stopped_after_a_flush_counts_its_changed_rows_and_leaves_their_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, log: LogRecorder, stop: str
) -> None:
    """Stopped after a flush of changed rows: they stand, counted, followed by one request.

    Every stored cost of the active dataset is shifted by one, so each row the backfill
    re-reads restates a changed value. The relay stops the job at the first call after the
    first flush, by stalling until the task is cancelled or by raising an unexpected
    ``RuntimeError``. The flushed rows hold the platform's values again, the flush's own
    transaction queued the one new ``analysis_request``, no other fact row changed or was
    deleted, every freshness row (its checkpoint included) stands, and each ``sync_run`` row of
    the job closes failed with its dataset's flushed rows, which ``sync.job.finish`` and
    ``sync.job.cancelled`` report as the job's rows.
    """
    dataset_id = await rig.build()
    platform = await stored(rig, dataset_id)
    await shift_costs(rig, dataset_id)
    before = await stored(rig, dataset_id)
    assert all(
        before[table][key].values != fact.values
        for table, rows in platform.items()
        for key, fact in rows.items()
    ), "every stored value differs from the platform's"
    fresh_before = await freshness_rows(rig, dataset_id)
    queued = await requests(rig)
    rig.instrument(monkeypatch)
    relay = rig.relay()
    reached = asyncio.Event()

    async def stop_after_a_flush(_call: int) -> httpx.Response | None:
        if rig.flushes:
            if stop == "raised":
                raise RuntimeError("the relay failed unexpectedly")
            reached.set()
            await asyncio.get_running_loop().create_future()
        return None

    relay.hook = stop_after_a_flush
    log.events.clear()  # The build's own jobs logged too.

    async with rig.context(dataset_id, relay) as ctx:
        job = jobs.MetricsSyncJob(ctx, "backfill")
        if stop == "raised":
            with pytest.raises(RuntimeError, match="the relay failed unexpectedly"):
                await job.run()
        else:
            task = asyncio.create_task(job.run())
            waiting = asyncio.create_task(reached.wait())
            try:
                await asyncio.wait({task, waiting}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiting.cancel()
            assert not task.done(), f"the job ended before the stalled call: {task.result()}"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(rig.flushes) == 1
    flushed = log.flushed()
    total = sum(flushed.values())
    assert total >= BOUND
    after = await stored(rig, dataset_id)
    assert {table: set(rows) for table, rows in after.items()} == {
        table: set(rows) for table, rows in before.items()
    }, "no fact row was deleted or added"
    new = {key: xid for key, xid in (await requests(rig)).items() if key not in queued}
    assert len(new) == 1, "exactly one request follows the flushed rows"
    (xid,) = new.values()
    rewritten = written_by(after, xid)
    assert len(rewritten) == total, "the request was queued by the flush that wrote them"
    for table, rows in after.items():
        for key, fact in rows.items():
            if (table, key) in rewritten:
                assert fact.values == platform[table][key].values, (table, key)
            else:
                assert fact == before[table][key], (table, key)
    assert await freshness_rows(rig, dataset_id) == fresh_before
    for name in (METRICS, TERMS):
        closed = await run_rows(rig, name)
        assert len(closed) == 2, f"{name}: a backfill holds a metrics and a restatement row"
        assert set(closed) == {("failed", flushed[name])}, name
    [finish] = log.named("sync.job.finish")
    assert finish["status"] == ("cancelled" if stop == "cancelled" else "failed")
    assert finish["rows"] == total
    if stop == "cancelled":
        [cancelled] = log.named("sync.job.cancelled")
        assert cancelled["rows"] == total


async def test_a_large_run_counts_every_row_it_writes_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, log: LogRecorder
) -> None:
    """Flushed or written by the recording transaction, every row counts exactly once.

    A backfill over the relay restates every changed cost of the active dataset in several
    flushes and two recording transactions. The job's rows, its ``sync.job.finish`` and each
    ``sync_run`` row equal the fact rows the run wrote, per dataset and in total, and every
    flush queued its own request besides the two the recorded successes queued.
    """
    dataset_id = await rig.build()
    platform = await stored(rig, dataset_id)
    await shift_costs(rig, dataset_id)
    queued = await requests(rig)
    rig.instrument(monkeypatch)
    log.events.clear()  # The build's own jobs logged too.
    started = utcnow()

    async with rig.context(dataset_id, rig.relay()) as ctx:
        result = await jobs.MetricsSyncJob(ctx, "backfill").run()

    assert result.status == "succeeded", result.error
    after = await stored(rig, dataset_id)
    assert values_of(after) == values_of(platform), "every changed value was restated"
    written = rewritten_since(after, started)
    assert written == {
        METRICS: len(after[FactCampaignDaily.__tablename__])
        + len(after[FactKeywordDaily.__tablename__]),
        TERMS: len(after[FactSearchTermDaily.__tablename__]),
    }, "the run rewrote every fact row"
    flushed = log.flushed()
    flushes = log.named("sync.metrics.google_flushed")
    assert len(rig.held(METRICS)) >= 2
    assert len(rig.held(TERMS)) >= 2
    assert all(fields["rows"] > 0 for fields in flushes)
    assert all(0 < flushed[name] <= written[name] for name in written), (flushed, written)
    assert result.rows == sum(written.values())
    for name, rows in written.items():
        closed = await run_rows(rig, name)
        assert len(closed) == 2, f"{name}: a backfill holds a metrics and a restatement row"
        assert set(closed) == {("succeeded", rows)}, name
    [finish] = log.named("sync.job.finish")
    assert finish["rows"] == result.rows
    new = [key for key in await requests(rig) if key not in queued]
    assert len(new) == len(flushes) + 2, "one request per flush and per recorded success"


async def test_a_building_backfill_flushes_without_queueing_a_request(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, log: LogRecorder
) -> None:
    """A building dataset's backfill flushes as any large read does but queues no request.

    With the buffer bound shrunk before the build, the backfill of the building dataset
    flushes both freshness datasets; no sync job queues an ``analysis_request`` for it, and
    the activation queues the rerun instead.
    """
    rig.instrument(monkeypatch)
    dataset_id = await rig.build()

    assert rig.held(METRICS), "the backfill outgrew the buffer"
    assert rig.held(TERMS), "the backfill outgrew the buffer"
    flushed = log.flushed()
    assert all(rows > 0 for rows in flushed.values()), f"every flush committed rows: {flushed}"
    facts = await stored(rig, dataset_id)
    assert 0 < sum(flushed.values()) <= sum(len(rows) for rows in facts.values())
    assert await requests(rig) == {}, "no sync job queued a request for the building dataset"
    async with rig.sessions() as session:
        reasons = set((await session.scalars(select(AnalysisRequest.reason))).all())
    assert "dataset_activated:google" in reasons, reasons
