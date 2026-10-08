"""Integration tests of the metrics and segment jobs' fact reconcile and once-per-job id maps.

AAP 0.1.1 ("Restated platform data overwrites earlier values"; "Upserts are idempotent"),
0.3.2 (fetch policy: daily facts are fetched incrementally and restated; segment evidence
exists only for exactly matching fetched windows) and 0.3.3 ("Budget", the transient row:
deferred or interrupted reads keep the last good data; a Q-G6 finest partition at the limit
is stored as returned and marked incomplete). A proven-complete re-fetch of a scope must leave
exactly what the platform now reports for it, so a stored key the re-fetch no longer returns
is deleted, while an incomplete, deferred or interrupted fetch deletes nothing.

Covered, on the PostgreSQL 18 test database through the conftest ``db`` fixture, with the
production jobs on simulated clients (``SourceModeController.open_clients(..., "simulated")``
and :meth:`JobContext.for_dataset`), each re-fetch edited at the connector seam:

* **Segments** (``fact_segment_window``): a value dropped from one complete (account, pivot,
  window) listing is deleted and every other row kept; an empty complete listing empties
  exactly its scope; an incomplete or interrupted listing deletes nothing; rows of another
  window (an earlier end date), account, facet or dataset are never touched;
* **LinkedIn daily** (``fact_creative_daily``): a creative-day the complete listing no longer
  returns is deleted inside the fetched range and kept outside it; an incomplete or
  interrupted listing keeps it;
* **Google** (Q-G3 ``fact_campaign_daily``, Q-G5 ``fact_keyword_daily``, Q-G6
  ``fact_search_term_daily``): absent keys inside the fetched range are deleted when the fetch
  is complete, an empty complete answer included, and kept outside the range or when the
  fetch is deferred, interrupted or capped at its finest partition;
* **Bounded statements**: a reconcile of more than ``_ID_CHUNK`` rows deletes in statements
  of at most ``_ID_CHUNK`` ids and selects its scope with a fixed number of parameters;
* **Idempotence**: a repeated complete fetch removes nothing more;
* **Once-per-job maps** (PERF-F04): the segment job reads the campaign table once and the
  LinkedIn metrics job the creative table once, however many batches they store.
"""

import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ads_engine.connectors.errors import CallBudgetExhausted, TransientError
from ads_engine.connectors.google_n8n.partition import (
    REASON_BUDGET,
    REASON_FINEST_PARTITION,
    FetchOutcome,
    PartitionedFetcher,
)
from ads_engine.connectors.linkedin.client import LinkedInClient, Listing
from ads_engine.connectors.linkedin.parser import (
    SEGMENT_FACETS,
    CreativeDailyRow,
    SegmentRow,
)
from ads_engine.db import repositories
from ads_engine.db.models.accounts import AdAccount, AdGroup, Campaign, Creative, Keyword
from ads_engine.db.models.facts import (
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    FactSegmentWindow,
)
from ads_engine.settings import Source, get_settings
from ads_engine.sync import freshness
from ads_engine.sync.jobs import (
    _ID_CHUNK,
    EntitySyncJob,
    JobContext,
    JobResult,
    MetricsSyncJob,
    SegmentSyncJob,
    default_window_end,
)
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type JobFactory = Callable[[JobContext], EntitySyncJob | MetricsSyncJob | SegmentSyncJob]
type SegmentKey = tuple[int, str, str, date, date]
"""``(campaign_id, facet, value, window_start, window_end)``."""
type SegmentEdit = Callable[[str, str, tuple[date, date], Listing[SegmentRow]], Listing[SegmentRow]]
type DailyEdit = Callable[[str, Listing[CreativeDailyRow]], Listing[CreativeDailyRow]]
type FetchEdit = Callable[[str, FetchOutcome], FetchOutcome]

NOW: Final = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
"""The instant of every job run: one clock, so every run reads the same native days."""

PIVOT: Final = "MEMBER_SENIORITY"
"""The pivot whose listings the segment tests edit."""

FACET: Final = SEGMENT_FACETS[PIVOT]
"""``seniority``, the facet :data:`PIVOT` stores."""

OTHER_PIVOT: Final = "MEMBER_JOB_FUNCTION"
OTHER_FACET: Final = SEGMENT_FACETS[OTHER_PIVOT]

PRESET_DAYS: Final = 28
"""The preset window the segment tests edit (``window.presets`` holds 14, 28, 56 and 90)."""

ABSENT_CRITERION: Final = "987654321"
"""A keyword criterion id the simulated Google account never reports."""

ABSENT_CAMPAIGN: Final = "9876543210"
"""A Google campaign id the simulated account never reports (a stand-in, as Q-G3 creates)."""

ABSENT_CREATIVE: Final = "urn:li:sponsoredCreative:987654321"
"""A LinkedIn creative URN the simulated accounts never report."""

ABSENT_TERM: Final = "reconcile test absent term"
"""A search term the simulated Google account never reports."""


@dataclass(frozen=True, slots=True)
class World:
    """A simulated dataset whose entities an entity sync stored."""

    db: Sessions
    controller: SourceModeController
    dataset_id: int
    accounts: tuple[str, ...]


# ---------------------------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------------------------


def _controller(db: Sessions) -> SourceModeController:
    """A controller on the test database with the fixed clock (its world shares it)."""
    return SourceModeController(get_settings(), sessionmaker=db, clock=lambda: NOW)


async def _run(
    world: World, job: JobFactory, *, accounts: tuple[str, ...] | None = None
) -> JobResult:
    """Run one production job on ``world``'s dataset with simulated clients.

    ``accounts`` narrows the LinkedIn accounts the job reads (the simulated world has two).
    """
    source: Source = "linkedin" if world.accounts else "google"
    async with world.controller.open_clients(source, "simulated") as clients:
        if accounts is not None:
            clients = replace(clients, linkedin_account_ids=accounts)
        ctx = await JobContext.for_dataset(world.controller, world.dataset_id, clients, now=NOW)
        return await job(ctx).run()


async def _world(db: Sessions, source: Source) -> World:
    """Begin a simulated dataset of ``source`` and store its entities."""
    controller = _controller(db)
    dataset_id = await controller.datasets.begin(source, "simulated", reason="reconcile test")
    accounts = controller.world.linkedin_account_ids if source == "linkedin" else ()
    world = World(db, controller, dataset_id, accounts)
    result = await _run(world, EntitySyncJob)
    assert result.status == "succeeded", result.error
    return world


@pytest.fixture
async def linkedin(db: Sessions) -> World:
    """A simulated LinkedIn dataset with its accounts, campaigns and creatives stored."""
    return await _world(db, "linkedin")


@pytest.fixture
async def google(db: Sessions) -> World:
    """A simulated Google dataset with its customer, campaigns, ad groups and keywords."""
    return await _world(db, "google")


def _incremental(ctx: JobContext) -> MetricsSyncJob:
    """The 6-hourly metrics job: the trailing 3 native days."""
    return MetricsSyncJob(ctx, "incremental")


@contextmanager
def _statements(db: Sessions) -> Iterator[list[tuple[str, int]]]:
    """Record ``(sql, parameter count)`` for every statement the engine sends."""
    engine = db.kw["bind"]
    sent: list[tuple[str, int]] = []

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: Any,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, context
        if executemany:
            count = max((len(parameter_set) for parameter_set in parameters), default=0)
            sent.extend((statement, count) for _ in parameters)
        else:
            sent.append((statement, len(parameters) if parameters else 0))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _reading(sent: Sequence[tuple[str, int]], table: str) -> list[str]:
    """The statements among ``sent`` that read ``table`` (in a ``FROM`` or ``JOIN``)."""
    pattern = re.compile(rf"\b(?:FROM|JOIN) {table}\b")
    return [sql for sql, _ in sent if pattern.search(sql)]


async def _account_campaigns(world: World) -> dict[str, frozenset[int]]:
    """The internal campaign ids of each ad account external id."""
    async with world.db() as session:
        result = await session.execute(
            select(AdAccount.external_id, Campaign.id)
            .join(AdAccount, AdAccount.id == Campaign.account_id)
            .where(Campaign.dataset_id == world.dataset_id)
        )
        grouped: dict[str, set[int]] = {}
        for account, campaign_id in result:
            grouped.setdefault(account, set()).add(campaign_id)
    return {account: frozenset(ids) for account, ids in grouped.items()}


# ---------------------------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------------------------


async def _segments(db: Sessions, dataset_id: int) -> dict[SegmentKey, tuple[Any, ...]]:
    """Every stored segment row of a dataset: natural key -> metrics."""
    async with db() as session:
        result = await session.execute(
            select(
                FactSegmentWindow.campaign_id,
                FactSegmentWindow.facet,
                FactSegmentWindow.value,
                FactSegmentWindow.window_start,
                FactSegmentWindow.window_end,
                FactSegmentWindow.impressions,
                FactSegmentWindow.clicks,
                FactSegmentWindow.cost,
                FactSegmentWindow.leads,
            ).where(FactSegmentWindow.dataset_id == dataset_id)
        )
        return {
            (campaign_id, facet, value, start, end): (impressions, clicks, cost, leads)
            for campaign_id, facet, value, start, end, impressions, clicks, cost, leads in result
        }


async def _preset_window(world: World, days: int = PRESET_DAYS) -> tuple[date, date]:
    """The ``days`` preset window ending on the default window end, as the job reads it."""
    async with world.db() as session:
        end = await default_window_end(session, now=NOW, thresholds=world.controller.thresholds)
    return end - timedelta(days=days - 1), end


def _patch_segments(monkeypatch: pytest.MonkeyPatch, edit: SegmentEdit) -> None:
    """Pass every segment listing through ``edit(account, pivot, window, listing)``."""
    original = LinkedInClient.analytics_segments

    async def edited(
        self: LinkedInClient, account_id: str, pivot: str, window: tuple[date, date]
    ) -> Listing[SegmentRow]:
        return edit(account_id, pivot, window, await original(self, account_id, pivot, window))

    monkeypatch.setattr(LinkedInClient, "analytics_segments", edited)


def _in_scope(
    key: SegmentKey, campaigns: frozenset[int], facet: str, window: tuple[date, date]
) -> bool:
    """Whether a stored segment key lies in one (account, facet, window) scope."""
    campaign_id, key_facet, _, start, end = key
    return campaign_id in campaigns and key_facet == facet and (start, end) == window


async def _inject_segments(
    db: Sessions, dataset_id: int, keys: Sequence[SegmentKey]
) -> list[SegmentKey]:
    """Store segment rows the simulator never returns; return their keys."""
    rows = [
        {
            "campaign_id": campaign_id,
            "facet": facet,
            "value": value,
            "window_start": start,
            "window_end": end,
            "impressions": 1000,
            "clicks": 10,
            "cost": Decimal("12.500000"),
            "leads": 2,
        }
        for campaign_id, facet, value, start, end in keys
    ]
    async with db() as session, session.begin():
        await repositories.upsert_facts(FactSegmentWindow, rows, dataset_id, session=session)
    return list(keys)


async def test_segment_value_a_complete_refetch_drops_is_deleted_and_the_rest_kept(
    linkedin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One value dropped from one complete listing: that row goes, every other row stays."""
    assert (await _run(linkedin, SegmentSyncJob)).status == "succeeded"
    before = await _segments(linkedin.db, linkedin.dataset_id)
    account = linkedin.accounts[0]
    window = await _preset_window(linkedin)
    scope = (await _account_campaigns(linkedin))[account]
    in_scope = sorted(key for key in before if _in_scope(key, scope, FACET, window))
    assert len(in_scope) >= 2, "the edited scope needs a value to drop and one to keep"
    dropped = in_scope[0]
    dropped_campaign = (await _campaign_externals(linkedin))[dropped[0]]

    def drop_one(
        account_id: str, pivot: str, fetched: tuple[date, date], listing: Listing[SegmentRow]
    ) -> Listing[SegmentRow]:
        if (account_id, pivot, fetched) != (account, PIVOT, window):
            return listing
        kept = tuple(
            row
            for row in listing.rows
            if (_urn_id(row.campaign_urn), row.value) != (dropped_campaign, dropped[2])
        )
        assert len(kept) == len(listing.rows) - 1, "the dropped value was not returned"
        return replace(listing, rows=kept)

    _patch_segments(monkeypatch, drop_one)
    with capture_logs() as logs:
        result = await _run(linkedin, SegmentSyncJob)

    assert result.status == "succeeded", result.error
    after = await _segments(linkedin.db, linkedin.dataset_id)
    assert dropped not in after
    assert after == {key: values for key, values in before.items() if key != dropped}
    removals = [entry for entry in logs if entry["event"] == "sync.segments.rows_removed"]
    assert [(entry["facet"], entry["rows"]) for entry in removals] == [(FACET, 1)]
    assert removals[0]["window_start"] == window[0].isoformat()
    assert removals[0]["window_end"] == window[1].isoformat()

    again = await _run(linkedin, SegmentSyncJob)
    assert again.status == "succeeded"
    assert await _segments(linkedin.db, linkedin.dataset_id) == after, "not idempotent"


def _urn_id(urn: str) -> str:
    """The id after a URN's last colon, as stored in ``external_id``."""
    return urn.rsplit(":", 1)[-1]


async def _campaign_externals(world: World) -> dict[int, str]:
    """Each stored campaign's external id by internal id."""
    async with world.db() as session:
        result = await session.execute(
            select(Campaign.id, Campaign.external_id).where(Campaign.dataset_id == world.dataset_id)
        )
        return dict(result.all())


async def test_empty_complete_segment_listing_empties_exactly_its_scope(
    linkedin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete listing with no value deletes its whole scope and nothing else."""
    assert (await _run(linkedin, SegmentSyncJob)).status == "succeeded"
    before = await _segments(linkedin.db, linkedin.dataset_id)
    account = linkedin.accounts[0]
    window = await _preset_window(linkedin)
    scope = (await _account_campaigns(linkedin))[account]
    in_scope = {key for key in before if _in_scope(key, scope, FACET, window)}
    assert in_scope, "the edited scope must hold rows to remove"

    def empty(
        account_id: str, pivot: str, fetched: tuple[date, date], listing: Listing[SegmentRow]
    ) -> Listing[SegmentRow]:
        if (account_id, pivot, fetched) == (account, PIVOT, window):
            return replace(listing, rows=())
        return listing

    _patch_segments(monkeypatch, empty)
    result = await _run(linkedin, SegmentSyncJob)

    assert result.status == "succeeded", result.error
    after = await _segments(linkedin.db, linkedin.dataset_id)
    assert after == {key: values for key, values in before.items() if key not in in_scope}
    # Neighbours of the emptied scope keep every row: the account's other facets and
    # windows, and the other account's rows of the same facet and window.
    assert any(key[1] == FACET and key[3:] != window for key in after)
    assert any(key[1] != FACET and key[3:] == window and key[0] in scope for key in after)
    assert any(key[1] == FACET and key[3:] == window and key[0] not in scope for key in after)


@pytest.mark.parametrize("shortfall", ["incomplete", "interrupted"])
async def test_incomplete_or_interrupted_segment_listing_deletes_nothing(
    linkedin: World, monkeypatch: pytest.MonkeyPatch, shortfall: str
) -> None:
    """A listing that did not read its whole scope never removes a stored value."""
    assert (await _run(linkedin, SegmentSyncJob)).status == "succeeded"
    before = await _segments(linkedin.db, linkedin.dataset_id)
    account = linkedin.accounts[0]
    window = await _preset_window(linkedin)

    def short(
        account_id: str, pivot: str, fetched: tuple[date, date], listing: Listing[SegmentRow]
    ) -> Listing[SegmentRow]:
        if (account_id, pivot, fetched) != (account, PIVOT, window):
            return listing
        if shortfall == "incomplete":
            return replace(listing, rows=(), complete=False, reason="result limit reached")
        error = TransientError(
            "simulated outage",
            source="linkedin",
            endpoint="ad_analytics_statistics",
            http_status=503,
        )
        return replace(listing, rows=(), complete=False, reason=error.reason, interrupted_by=error)

    _patch_segments(monkeypatch, short)
    result = await _run(linkedin, SegmentSyncJob)

    after = await _segments(linkedin.db, linkedin.dataset_id)
    assert after == before
    segments = result.freshness["linkedin.segments"]
    if shortfall == "incomplete":
        assert result.status == "succeeded", result.error
        assert segments.completeness == "incomplete"
    else:
        assert result.status == "failed"
        assert not segments.succeeded


async def test_other_windows_accounts_facets_and_datasets_are_never_touched(
    db: Sessions, linkedin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the exact (account, facet, window) scope of a complete listing is reconciled."""
    assert (await _run(linkedin, SegmentSyncJob)).status == "succeeded"
    before = await _segments(linkedin.db, linkedin.dataset_id)
    first, second = linkedin.accounts[:2]
    campaigns = await _account_campaigns(linkedin)
    window = await _preset_window(linkedin)
    earlier = (window[0] - timedelta(days=1), window[1] - timedelta(days=1))
    mine = min(campaigns[first])
    theirs = min(campaigns[second])
    removed = await _inject_segments(
        db, linkedin.dataset_id, [(mine, OTHER_FACET, "urn:li:function:9999", *window)]
    )
    kept = await _inject_segments(
        db,
        linkedin.dataset_id,
        [
            # The other account's scope is not fetched in this run.
            (theirs, OTHER_FACET, "urn:li:function:9999", *window),
            # Another window of the same campaign and facet: an earlier default end.
            (mine, OTHER_FACET, "urn:li:function:9999", *earlier),
            # Another facet of the same campaign and window, whose listing is incomplete.
            (mine, FACET, "urn:li:seniority:9999", *window),
        ],
    )
    other = await _other_linkedin_dataset(db, window)

    def capped(
        account_id: str, pivot: str, fetched: tuple[date, date], listing: Listing[SegmentRow]
    ) -> Listing[SegmentRow]:
        if pivot == PIVOT:
            return replace(listing, complete=False, reason="result limit reached")
        return listing

    _patch_segments(monkeypatch, capped)
    result = await _run(linkedin, SegmentSyncJob, accounts=(first,))

    assert result.status == "succeeded", result.error
    after = await _segments(linkedin.db, linkedin.dataset_id)
    assert not set(removed) & set(after)
    assert set(kept) <= set(after)
    assert set(before) <= set(after), "a value the simulator still returns was deleted"
    assert set(await _segments(db, other[0])) == {other[1]}


async def _other_linkedin_dataset(
    db: Sessions, window: tuple[date, date]
) -> tuple[int, SegmentKey]:
    """Another LinkedIn dataset holding one segment row of ``window``; its id and key."""
    controller = _controller(db)
    dataset_id = await controller.datasets.begin("linkedin", "simulated", reason="other dataset")
    async with db() as session, session.begin():
        accounts = await repositories.upsert_entities(
            session,
            AdAccount,
            [{"external_id": "990000001", "name": "Other", "currency": "USD", "timezone": "UTC"}],
            dataset_id,
        )
        campaigns = await repositories.upsert_entities(
            session,
            Campaign,
            [
                {
                    "account_id": next(iter(accounts.values())),
                    "external_id": "1",
                    "name": "Other campaign",
                    "status": "active",
                    "raw_status": "ACTIVE",
                }
            ],
            dataset_id,
        )
    key: SegmentKey = (next(iter(campaigns.values())), OTHER_FACET, "urn:li:function:1", *window)
    await _inject_segments(db, dataset_id, [key])
    return dataset_id, key


async def test_reconcile_of_more_than_an_id_chunk_deletes_in_bounded_statements(
    db: Sessions, linkedin: World
) -> None:
    """2,500 withdrawn values go in three DELETEs of at most ``_ID_CHUNK`` ids each."""
    assert (await _run(linkedin, SegmentSyncJob)).status == "succeeded"
    before = await _segments(linkedin.db, linkedin.dataset_id)
    account = linkedin.accounts[0]
    campaign = min((await _account_campaigns(linkedin))[account])
    window = await _preset_window(linkedin)
    injected = await _inject_segments(
        db,
        linkedin.dataset_id,
        [
            (campaign, FACET, f"urn:li:seniority:{90000 + index}", *window)
            for index in range(2 * _ID_CHUNK + 500)
        ],
    )

    with _statements(db) as sent:
        result = await _run(linkedin, SegmentSyncJob, accounts=(account,))

    assert result.status == "succeeded", result.error
    after = await _segments(linkedin.db, linkedin.dataset_id)
    assert not set(injected) & set(after)
    assert after == before
    deletes = [count for sql, count in sent if sql.startswith("DELETE FROM fact_segment_window ")]
    assert len(deletes) == 3
    assert all(count <= _ID_CHUNK + 1 for count in deletes), deletes
    scope_reads = [count for sql, count in sent if sql.startswith("SELECT fact_segment_window.id")]
    assert scope_reads, "every complete listing reads its scope"
    assert max(scope_reads) <= 5, "the scope read binds a fixed number of parameters"


async def test_segment_job_reads_the_campaign_table_once(linkedin: World) -> None:
    """PERF-F04: the campaign map is read once per job, not once per stored batch."""
    with _statements(linkedin.db) as sent:
        result = await _run(linkedin, SegmentSyncJob)

    assert result.status == "succeeded", result.error
    batches = [sql for sql, _ in sent if sql.startswith("INSERT INTO fact_segment_window ")]
    assert len(batches) > len(linkedin.accounts), "the run stores several batches"
    assert len(_reading(sent, "campaign")) == 1, _reading(sent, "campaign")


# ---------------------------------------------------------------------------------------------
# LinkedIn daily creative analytics
# ---------------------------------------------------------------------------------------------


async def _creative_days(db: Sessions, dataset_id: int) -> dict[tuple[int, date], Any]:
    """Every stored creative-day of a dataset: (creative, day) -> impressions."""
    async with db() as session:
        result = await session.execute(
            select(
                FactCreativeDaily.creative_id,
                FactCreativeDaily.date,
                FactCreativeDaily.impressions,
            ).where(FactCreativeDaily.dataset_id == dataset_id)
        )
        return {(creative, day): impressions for creative, day, impressions in result}


async def _covered(world: World, name: str) -> tuple[date, date]:
    """The native range a dataset's freshness row records as read."""
    async with world.db() as session:
        row = (await freshness.load_freshness(session, world.dataset_id))[name]
    assert row.covered_from is not None
    assert row.covered_through is not None
    return row.covered_from, row.covered_through


async def _account_creatives(world: World) -> dict[str, dict[str, int]]:
    """Each ad account's creatives: URN -> internal id."""
    async with world.db() as session:
        result = await session.execute(
            select(AdAccount.external_id, Creative.urn, Creative.id)
            .join(Campaign, Campaign.id == Creative.campaign_id)
            .join(AdAccount, AdAccount.id == Campaign.account_id)
            .where(Creative.dataset_id == world.dataset_id)
        )
        grouped: dict[str, dict[str, int]] = {}
        for account, urn, creative_id in result:
            grouped.setdefault(account, {})[urn] = creative_id
    return grouped


async def _inject_creative(world: World, account: str, days: Sequence[date]) -> dict[date, int]:
    """Store a creative the simulator never reports, with a fact on each of ``days``."""
    campaign = min((await _account_campaigns(world))[account])
    async with world.db() as session, session.begin():
        stored = await repositories.upsert_entities(
            session, Creative, [{"campaign_id": campaign, "urn": ABSENT_CREATIVE}], world.dataset_id
        )
        creative_id = next(iter(stored.values()))
        await repositories.upsert_facts(
            FactCreativeDaily,
            [
                {"creative_id": creative_id, "date": day, "impressions": 500, "clicks": 5}
                for day in days
            ],
            world.dataset_id,
            session=session,
        )
    return dict.fromkeys(days, creative_id)


def _patch_daily(monkeypatch: pytest.MonkeyPatch, edit: DailyEdit) -> None:
    """Pass every daily analytics listing through ``edit(account, listing)``."""
    original = LinkedInClient.analytics_daily

    async def edited(
        self: LinkedInClient,
        account_id: str,
        date_range: tuple[date, date],
        *,
        expected_creatives: int | None = None,
    ) -> Listing[CreativeDailyRow]:
        listing = await original(
            self, account_id, date_range, expected_creatives=expected_creatives
        )
        return edit(account_id, listing)

    monkeypatch.setattr(LinkedInClient, "analytics_daily", edited)


async def test_linkedin_daily_absent_key_is_deleted_inside_the_range_only(
    linkedin: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creative-day the complete listing drops goes; outside the range it stays."""
    assert (await _run(linkedin, _incremental)).status == "succeeded"
    start, end = await _covered(linkedin, "linkedin.analytics_daily")
    before = await _creative_days(linkedin.db, linkedin.dataset_id)
    account = linkedin.accounts[0]
    creatives = (await _account_creatives(linkedin))[account]
    reported = sorted(key for key in before if key[0] in set(creatives.values()))
    assert reported, "the account reports creative days"
    dropped = reported[0]
    urn = next(urn for urn, creative_id in creatives.items() if creative_id == dropped[0])
    injected = await _inject_creative(linkedin, account, [end, start - timedelta(days=1)])

    def drop_one(account_id: str, listing: Listing[CreativeDailyRow]) -> Listing[CreativeDailyRow]:
        if account_id != account:
            return listing
        rows = tuple(
            row for row in listing.rows if (row.creative_urn, row.date) != (urn, dropped[1])
        )
        return replace(listing, rows=rows)

    _patch_daily(monkeypatch, drop_one)
    with capture_logs() as logs:
        result = await _run(linkedin, _incremental)

    assert result.status == "succeeded", result.error
    after = await _creative_days(linkedin.db, linkedin.dataset_id)
    outside = (injected[start - timedelta(days=1)], start - timedelta(days=1))
    assert set(after) == (set(before) - {dropped}) | {outside}
    removals = [entry for entry in logs if entry["event"] == "sync.metrics.rows_removed"]
    assert [(entry["source"], entry["rows"]) for entry in removals] == [("linkedin", 2)]


@pytest.mark.parametrize("shortfall", ["incomplete", "interrupted"])
async def test_incomplete_or_interrupted_daily_listing_keeps_every_row(
    linkedin: World, monkeypatch: pytest.MonkeyPatch, shortfall: str
) -> None:
    """A daily listing that did not finish never removes a stored creative-day."""
    assert (await _run(linkedin, _incremental)).status == "succeeded"
    _, end = await _covered(linkedin, "linkedin.analytics_daily")
    account = linkedin.accounts[0]
    await _inject_creative(linkedin, account, [end])
    before = await _creative_days(linkedin.db, linkedin.dataset_id)

    def short(account_id: str, listing: Listing[CreativeDailyRow]) -> Listing[CreativeDailyRow]:
        if account_id != account:
            return listing
        if shortfall == "incomplete":
            return replace(
                listing,
                rows=listing.rows[1:],
                complete=False,
                reason="result limit reached at finest partition (one day)",
            )
        error = TransientError(
            "simulated outage", source="linkedin", endpoint="ad_analytics", http_status=503
        )
        return replace(
            listing,
            rows=listing.rows[1:],
            complete=False,
            reason=error.reason,
            interrupted_by=error,
        )

    _patch_daily(monkeypatch, short)
    result = await _run(linkedin, _incremental)

    assert set(await _creative_days(linkedin.db, linkedin.dataset_id)) == set(before)
    analytics = result.freshness["linkedin.analytics_daily"]
    if shortfall == "incomplete":
        assert result.status == "succeeded", result.error
        assert analytics.completeness == "incomplete"
    else:
        assert result.status == "failed"
        assert not analytics.succeeded


async def test_linkedin_metrics_job_reads_the_creative_table_once(linkedin: World) -> None:
    """PERF-F04: the creative map is read once per job, not once per account."""
    with _statements(linkedin.db) as sent:
        result = await _run(linkedin, _incremental)

    assert result.status == "succeeded", result.error
    stored = [sql for sql, _ in sent if sql.startswith("INSERT INTO fact_creative_daily ")]
    assert len(linkedin.accounts) > 1
    assert stored, "the run stores creative days"
    assert len(_reading(sent, "creative")) == 1, _reading(sent, "creative")


# ---------------------------------------------------------------------------------------------
# Google Q-G3, Q-G5 and Q-G6
# ---------------------------------------------------------------------------------------------


async def _google_facts(db: Sessions, dataset_id: int) -> dict[str, set[tuple[Any, ...]]]:
    """Every stored Google daily key of a dataset, by query id."""
    async with db() as session:
        campaign = await session.execute(
            select(FactCampaignDaily.campaign_id, FactCampaignDaily.date).where(
                FactCampaignDaily.dataset_id == dataset_id
            )
        )
        keyword = await session.execute(
            select(FactKeywordDaily.keyword_id, FactKeywordDaily.date).where(
                FactKeywordDaily.dataset_id == dataset_id
            )
        )
        term = await session.execute(
            select(
                FactSearchTermDaily.ad_group_id,
                FactSearchTermDaily.matched_criterion_id,
                FactSearchTermDaily.search_term,
                FactSearchTermDaily.date,
            ).where(FactSearchTermDaily.dataset_id == dataset_id)
        )
        return {
            "Q-G3": set(campaign.all()),
            "Q-G5": set(keyword.all()),
            "Q-G6": set(term.all()),
        }


async def _inject_google(world: World) -> dict[str, set[tuple[Any, ...]]]:
    """Store one absent key per query on its range's end and on the day before the range.

    Returns:
        The keys stored inside (``<query>:in``) and outside (``<query>:out``) each query's
        fetched range.
    """
    metrics_start, metrics_end = await _covered(world, "google.metrics_daily")
    terms_start, terms_end = await _covered(world, "google.search_terms")
    async with world.db() as session, session.begin():
        account = (
            await session.scalars(
                select(AdAccount.id).where(AdAccount.dataset_id == world.dataset_id)
            )
        ).one()
        group = (
            await session.execute(
                select(AdGroup.id, AdGroup.campaign_id)
                .where(AdGroup.dataset_id == world.dataset_id)
                .order_by(AdGroup.id)
                .limit(1)
            )
        ).one()
        campaign_id = next(
            iter(
                (
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
                        world.dataset_id,
                    )
                ).values()
            )
        )
        keyword_id = next(
            iter(
                (
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
                        world.dataset_id,
                    )
                ).values()
            )
        )
        metric_days = (metrics_end, metrics_start - timedelta(days=1))
        term_days = (terms_end, terms_start - timedelta(days=1))
        money = {"cost": Decimal("3.000000"), "impressions": 30, "clicks": 3}
        await repositories.upsert_facts(
            FactCampaignDaily,
            [{"campaign_id": campaign_id, "date": day, **money} for day in metric_days],
            world.dataset_id,
            session=session,
        )
        await repositories.upsert_facts(
            FactKeywordDaily,
            [{"keyword_id": keyword_id, "date": day, **money} for day in metric_days],
            world.dataset_id,
            session=session,
        )
        await repositories.upsert_facts(
            FactSearchTermDaily,
            [
                {
                    "ad_group_id": group.id,
                    "campaign_id": group.campaign_id,
                    "date": day,
                    "search_term": ABSENT_TERM,
                    "term_status": "NONE",
                    "matched_criterion_id": ABSENT_CRITERION,
                    **money,
                }
                for day in term_days
            ],
            world.dataset_id,
            session=session,
        )
    return {
        "Q-G3:in": {(campaign_id, metric_days[0])},
        "Q-G3:out": {(campaign_id, metric_days[1])},
        "Q-G5:in": {(keyword_id, metric_days[0])},
        "Q-G5:out": {(keyword_id, metric_days[1])},
        "Q-G6:in": {(group.id, ABSENT_CRITERION, ABSENT_TERM, term_days[0])},
        "Q-G6:out": {(group.id, ABSENT_CRITERION, ABSENT_TERM, term_days[1])},
    }


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, edit: FetchEdit) -> None:
    """Pass every Google partitioned fetch through ``edit(query_id, outcome)``."""
    original = PartitionedFetcher.partitioned_fetch

    async def edited(self: PartitionedFetcher, query_id: str, scope: Any = None) -> FetchOutcome:
        return edit(query_id, await original(self, query_id, scope))

    monkeypatch.setattr(PartitionedFetcher, "partitioned_fetch", edited)


@pytest.fixture
async def google_synced(google: World) -> World:
    """The Google dataset after one complete incremental metrics run."""
    result = await _run(google, _incremental)
    assert result.status == "succeeded", result.error
    assert result.completeness == "complete"
    return google


async def test_google_complete_refetch_deletes_absent_keys_inside_each_range(
    google_synced: World,
) -> None:
    """Q-G3, Q-G5 and Q-G6: absent keys go inside the fetched range and stay outside it."""
    before = await _google_facts(google_synced.db, google_synced.dataset_id)
    assert all(before.values()), "every query stored rows"
    keys = await _inject_google(google_synced)

    with capture_logs() as logs:
        result = await _run(google_synced, _incremental)

    assert result.status == "succeeded", result.error
    after = await _google_facts(google_synced.db, google_synced.dataset_id)
    for query_id in ("Q-G3", "Q-G5", "Q-G6"):
        assert after[query_id] == before[query_id] | keys[f"{query_id}:out"], query_id
    removals = [entry for entry in logs if entry["event"] == "sync.metrics.rows_removed"]
    assert sorted((entry["query_id"], entry["rows"]) for entry in removals) == [
        ("Q-G3", 1),
        ("Q-G5", 1),
        ("Q-G6", 1),
    ]


@pytest.mark.parametrize("query_id", ["Q-G3", "Q-G5", "Q-G6"])
async def test_google_empty_complete_answer_empties_the_range(
    google_synced: World, monkeypatch: pytest.MonkeyPatch, query_id: str
) -> None:
    """A complete fetch with no row deletes every row of its range, none outside it."""
    before = await _google_facts(google_synced.db, google_synced.dataset_id)
    keys = await _inject_google(google_synced)

    def empty(fetched: str, outcome: FetchOutcome) -> FetchOutcome:
        return replace(outcome, rows=()) if fetched == query_id else outcome

    _patch_fetch(monkeypatch, empty)
    result = await _run(google_synced, _incremental)

    assert result.status == "succeeded", result.error
    after = await _google_facts(google_synced.db, google_synced.dataset_id)
    assert after[query_id] == keys[f"{query_id}:out"]
    for other in {"Q-G3", "Q-G5", "Q-G6"} - {query_id}:
        assert after[other] == before[other] | keys[f"{other}:out"], other


@pytest.mark.parametrize(
    ("query_id", "shortfall"),
    [
        ("Q-G3", "deferred"),
        ("Q-G5", "deferred"),
        ("Q-G6", "deferred"),
        ("Q-G6", "finest_partition"),
        ("Q-G3", "interrupted"),
    ],
)
async def test_google_incomplete_fetch_keeps_absent_keys(
    google_synced: World, monkeypatch: pytest.MonkeyPatch, query_id: str, shortfall: str
) -> None:
    """A deferred, interrupted or capped fetch deletes nothing; complete queries still do."""
    before = await _google_facts(google_synced.db, google_synced.dataset_id)
    keys = await _inject_google(google_synced)

    def short(fetched: str, outcome: FetchOutcome) -> FetchOutcome:
        if fetched != query_id:
            return outcome
        rows = outcome.rows[1:]
        match shortfall:
            case "deferred":
                return replace(
                    outcome,
                    rows=rows,
                    completeness="incomplete",
                    reason=REASON_BUDGET,
                    interrupted_by=CallBudgetExhausted(),
                )
            case "finest_partition":
                return replace(
                    outcome, rows=rows, completeness="incomplete", reason=REASON_FINEST_PARTITION
                )
            case _:
                error = TransientError(
                    "simulated outage", source="google", endpoint="search", http_status=503
                )
                return replace(
                    outcome,
                    rows=rows,
                    completeness="incomplete",
                    reason=error.reason,
                    interrupted_by=error,
                )

    _patch_fetch(monkeypatch, short)
    await _run(google_synced, _incremental)

    after = await _google_facts(google_synced.db, google_synced.dataset_id)
    assert after[query_id] == before[query_id] | keys[f"{query_id}:in"] | keys[f"{query_id}:out"]
    for other in {"Q-G3", "Q-G5", "Q-G6"} - {query_id}:
        assert after[other] == before[other] | keys[f"{other}:out"], other
