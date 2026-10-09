"""Integration tests: a pending ``analysis_request`` backlog of any size is served by one run.

AAP 0.3.2 (the worker coalesces requests that arrive within 60 s into one run) and 0.5.3
(lock unavailable: the worker leaves its ``analysis_request`` pending; one ``analysis`` lock for
every entry point). The backlog is read as one bounded summary
(``analysis.runner.pending_requests``) by both of its readers, the scheduler's coalescing step
and the worker's startup run, and served through the ``serve_through`` watermark, which the
runner claims with one set-based update after its ``running`` row is committed and before
its context snapshot opens. These tests prove, against the real PostgreSQL test database:

* a backlog larger than PostgreSQL's 65,535 bind parameters is served by exactly one default
  run, from the scheduler and from the worker startup, and no statement touching
  ``analysis_request`` carries more than a handful of parameters;
* explicit ``request_ids`` beyond that limit are stamped without a bind error;
* a request above the watermark, or queued after the backlog was read, stays pending for the
  next run, while every claimed request is already stamped inside the run's snapshot;
* behind a held ``analysis`` lock the whole backlog stays pending, a failed claim leaves it
  pending, and a run that lost its lock queues a catch-up request;
* ``pending_requests`` returns ``None`` on an empty queue and a bounded reason list on a large
  one.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis import metrics, precedence, runner
from ads_engine.analysis.context import AnalysisContext
from ads_engine.analysis.lock import RunInProgress, analysis_lock
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.db.models.accounts import AdAccount
from ads_engine.db.models.analysis import AnalysisRequest, AnalysisRun
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.session import get_engine
from ads_engine.sync import scheduler as scheduler_module
from ads_engine.sync.scheduler import COALESCE_WINDOW, Scheduler
from ads_engine.worker import main as worker_main

pytestmark = pytest.mark.anyio

Sessions = async_sessionmaker[AsyncSession]

AT = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The scheduler's clock in these tests; yesterday is 2026-10-06 in New York and UTC."""

BIND_LIMIT = 65_535
"""PostgreSQL's most bind parameters in one statement (the wire protocol's int16 count)."""

BACKLOG = 70_000
"""A backlog larger than :data:`BIND_LIMIT`, so one parameter per request cannot work."""

MOST_REQUEST_BINDS = 4
"""The most parameters any statement touching ``analysis_request`` may carry."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


async def _seed(db: Sessions) -> None:
    """Seed the active datasets a default run reads (Google live with an account in New York).

    LinkedIn is simulated with its companion CRM dataset and HubSpot live, as in the runner's
    own tests, so the default window and the context load run on a real configuration.
    """
    async with db() as session, session.begin():

        async def dataset(source: str, mode: str, parent: int | None = None) -> int:
            row = SourceDataset(
                source=source,
                mode=mode,
                state="active",
                role="primary" if parent is None else "companion",
                parent_dataset_id=parent,
                reason="credentials not configured" if mode == "simulated" else None,
                activated_at=AT - timedelta(days=30),
                retired_at=None,
                crm_bindings=None,
            )
            session.add(row)
            await session.flush()
            return row.id

        google = await dataset("google", "live")
        linkedin = await dataset("linkedin", "simulated")
        await dataset("hubspot", "simulated", parent=linkedin)
        await dataset("hubspot", "live")
        session.add(
            AdAccount(
                source="google",
                dataset_id=google,
                external_id="1234567890",
                name="Main",
                currency="USD",
                timezone="America/New_York",
            )
        )


async def _backlog(db: Sessions, count: int, *, requested_at: datetime, reasons: int = 1) -> None:
    """Queue ``count`` pending requests in one ``INSERT … SELECT generate_series``.

    Request ``g`` (1-based) carries the reason ``reason-{g % reasons}``.
    """
    async with db() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO analysis_request (requested_at, reason) "
                "SELECT :at, 'reason-' || (g % :reasons) FROM generate_series(1, :count) AS g"
            ),
            {"at": requested_at, "reasons": reasons, "count": count},
        )


async def _request(db: Sessions, *, requested_at: datetime, reason: str = "decision") -> int:
    """Queue one pending request and return its id."""
    async with db() as session, session.begin():
        row = AnalysisRequest(reason=reason, requested_at=requested_at)
        session.add(row)
        await session.flush()
        return row.id


@dataclass(frozen=True)
class _Queue:
    """The state of every ``analysis_request`` row."""

    total: int
    pending: int
    unserved: int
    run_ids: tuple[int, ...]


async def _queue(db: Sessions) -> _Queue:
    """Count all, pending (``picked_at`` NULL) and unserved (``run_id`` NULL) requests."""
    async with db() as session:
        total, pending, unserved, run_ids = (
            await session.execute(
                text(
                    "SELECT count(*), count(*) FILTER (WHERE picked_at IS NULL), "
                    "count(*) FILTER (WHERE run_id IS NULL), "
                    "coalesce(array_agg(DISTINCT run_id) FILTER (WHERE run_id IS NOT NULL), "
                    "'{}') FROM analysis_request"
                )
            )
        ).one()
    return _Queue(int(total), int(pending), int(unserved), tuple(sorted(run_ids)))


async def _runs(db: Sessions) -> list[AnalysisRun]:
    """Every ``analysis_run`` row, by id."""
    async with db() as session:
        return list(await session.scalars(select(AnalysisRun).order_by(AnalysisRun.id)))


async def _stamps(db: Sessions, ids: list[int]) -> dict[int, tuple[bool, int | None]]:
    """``{id: (picked, run_id)}`` for the given request ids."""
    async with db() as session:
        rows = await session.execute(
            select(AnalysisRequest.id, AnalysisRequest.picked_at, AnalysisRequest.run_id).where(
                AnalysisRequest.id.in_(ids)
            )
        )
        return {int(row_id): (picked is not None, run_id) for row_id, picked, run_id in rows}


def _bind_count(parameters: object) -> int:
    """How many parameters one cursor execution sends (the largest set of an executemany)."""
    if isinstance(parameters, Mapping):
        return len(parameters)
    if isinstance(parameters, list | tuple):
        sets = [item for item in parameters if isinstance(item, Mapping | list | tuple)]
        if sets and len(sets) == len(parameters):
            return max(len(item) for item in sets)
        return len(parameters)
    return 0


@contextmanager
def _request_binds() -> Iterator[list[int]]:
    """Record the parameter count of every statement that touches ``analysis_request``."""
    engine = get_engine().sync_engine
    seen: list[int] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "analysis_request" in statement:
            seen.append(_bind_count(parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _coalesce(scheduler: Scheduler, at: datetime) -> Any:
    """Run the scheduler's analysis-coalescing step alone on a fresh tick at ``at``."""
    tick = scheduler_module._Tick(at)
    await scheduler._coalesce_analysis(tick)
    return tick


def _events(records: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The captured log records of event ``name``."""
    return [record for record in records if record.get("event") == name]


# ---------------------------------------------------------------------------------------------
# pending_requests: the one bounded backlog reader
# ---------------------------------------------------------------------------------------------


async def test_pending_requests_is_none_without_a_pending_request(db: Sessions) -> None:
    async with db() as session:
        assert await runner.pending_requests(session) is None
    served = await _request(db, requested_at=AT)
    async with db() as session, session.begin():
        await session.execute(
            text("UPDATE analysis_request SET picked_at = :at WHERE id = :id"),
            {"at": AT, "id": served},
        )
    async with db() as session:
        assert await runner.pending_requests(session) is None


async def test_pending_requests_summarises_a_large_backlog_in_bounded_reads(
    db: Sessions,
) -> None:
    await _backlog(db, BACKLOG, requested_at=AT - timedelta(minutes=5), reasons=50)
    oldest = AT - timedelta(hours=1)
    await _request(db, requested_at=oldest, reason="reason-1")
    async with db() as session:
        highest = int(
            (await session.execute(text("SELECT max(id) FROM analysis_request"))).one()[0]
        )

    with _request_binds() as binds:
        async with db() as session:
            backlog = await runner.pending_requests(session)

    assert backlog is not None
    assert backlog.count == BACKLOG + 1
    assert backlog.oldest_requested_at == oldest
    assert backlog.through_id == highest
    # 50 distinct reasons are pending; the first 32 queued are returned, in queue order.
    assert backlog.reasons == tuple(f"reason-{index}" for index in range(1, 33))
    assert len(backlog.reasons) == runner.PENDING_REASONS_LIMIT
    assert binds
    assert max(binds) <= MOST_REQUEST_BINDS


# ---------------------------------------------------------------------------------------------
# The scheduler's coalescing step
# ---------------------------------------------------------------------------------------------


async def test_scheduler_serves_a_backlog_beyond_the_bind_limit_with_one_run(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    await _backlog(db, BACKLOG, requested_at=AT - timedelta(minutes=5))
    scheduler = Scheduler(clock=lambda: AT)

    with _request_binds() as binds:
        tick = await _coalesce(scheduler, AT)

    assert tick.errors == []
    assert not tick.analysis_deferred
    (run,) = await _runs(db)
    assert tick.analysis_run_id == run.id
    assert (run.kind, run.trigger, run.status) == ("default", "event", "succeeded")
    assert await _queue(db) == _Queue(BACKLOG, 0, 0, (run.id,))
    assert binds
    assert max(binds) <= MOST_REQUEST_BINDS
    records = captured_logs()
    (started,) = _events(records, "analysis_run_started")
    (summary,) = _events(records, "analysis_run_summary")
    assert started["requests"] == summary["requests"] == BACKLOG
    assert summary["reason"] == "reason-0"


async def test_scheduler_waits_until_the_oldest_request_is_coalesced(db: Sessions) -> None:
    await _seed(db)
    young = AT - COALESCE_WINDOW + timedelta(seconds=1)
    await _backlog(db, 3, requested_at=young)
    scheduler = Scheduler(clock=lambda: AT)

    early = await _coalesce(scheduler, AT)

    assert early.analysis_run_id is None
    assert await _runs(db) == []
    assert await _queue(db) == _Queue(3, 3, 3, ())
    later = AT + timedelta(seconds=1)
    served = await _coalesce(Scheduler(clock=lambda: later), later)
    assert served.analysis_run_id is not None
    assert await _queue(db) == _Queue(3, 0, 0, (served.analysis_run_id,))


async def test_a_request_queued_after_the_backlog_read_waits_for_the_next_tick(
    db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(db)
    await _backlog(db, 5, requested_at=AT - timedelta(minutes=5))
    original = runner.pending_requests
    late: list[int] = []

    async def then_another_arrives(session: AsyncSession) -> runner.PendingRequests | None:
        backlog = await original(session)
        late.append(await _request(db, requested_at=AT - timedelta(minutes=2)))
        return backlog

    monkeypatch.setattr(runner, "pending_requests", then_another_arrives)
    scheduler = Scheduler(clock=lambda: AT)
    first = await _coalesce(scheduler, AT)
    monkeypatch.setattr(runner, "pending_requests", original)

    assert first.analysis_run_id is not None
    assert await _stamps(db, late) == {late[0]: (False, None)}
    queue = await _queue(db)
    assert (queue.pending, queue.run_ids) == (1, (first.analysis_run_id,))

    second = await _coalesce(scheduler, AT)
    assert second.analysis_run_id is not None
    assert second.analysis_run_id != first.analysis_run_id
    assert await _stamps(db, late) == {late[0]: (True, second.analysis_run_id)}
    assert await _queue(db) == _Queue(6, 0, 0, (first.analysis_run_id, second.analysis_run_id))


async def test_a_held_lock_leaves_the_whole_backlog_pending(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    await _backlog(db, BACKLOG, requested_at=AT - timedelta(minutes=5))
    scheduler = Scheduler(clock=lambda: AT)

    async with analysis_lock():
        tick = await _coalesce(scheduler, AT)

    assert tick.analysis_deferred
    assert tick.analysis_run_id is None
    assert tick.errors == []
    assert await _runs(db) == []
    assert await _queue(db) == _Queue(BACKLOG, BACKLOG, BACKLOG, ())
    (deferred,) = _events(captured_logs(), "scheduler.analysis_deferred")
    assert (deferred["reason"], deferred["requests"]) == ("run_in_progress", BACKLOG)


async def test_a_run_that_lost_its_lock_queues_a_catch_up_request(
    db: Sessions, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(db)
    await _backlog(db, 4, requested_at=AT - timedelta(minutes=5))
    original = precedence.apply_precedence

    def taken_over(results: Any, ctx: AnalysisContext) -> Any:
        out = original(results, ctx)
        # Another process took the lock after this one lost it, and failed the row.
        with sync_engine.begin() as connection:
            connection.execute(
                text("UPDATE analysis_run SET status = 'failed', finished_at = now()")
            )
        return out

    monkeypatch.setattr(precedence, "apply_precedence", taken_over)
    tick = await _coalesce(Scheduler(clock=lambda: AT), AT)

    assert tick.errors == ["analysis"]
    assert tick.analysis_run_id is None
    (run,) = await _runs(db)
    assert run.status == "failed"
    queue = await _queue(db)
    # The four claimed requests stay with the failed run; one catch-up request is pending.
    assert (queue.total, queue.pending, queue.run_ids) == (5, 1, (run.id,))
    async with db() as session:
        reasons = list(
            await session.scalars(
                select(AnalysisRequest.reason).where(AnalysisRequest.picked_at.is_(None))
            )
        )
    assert reasons == ["analysis_lock_lost"]


# ---------------------------------------------------------------------------------------------
# The worker's startup run
# ---------------------------------------------------------------------------------------------


async def test_worker_startup_serves_a_backlog_beyond_the_bind_limit_with_one_run(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    # No coalescing wait at startup: requests queued a moment ago are served too.
    await _backlog(db, BACKLOG, requested_at=datetime.now(UTC))

    with _request_binds() as binds:
        await worker_main._startup_analysis(Thresholds.load())

    (run,) = await _runs(db)
    assert (run.kind, run.trigger, run.status) == ("default", "scheduled", "succeeded")
    assert await _queue(db) == _Queue(BACKLOG, 0, 0, (run.id,))
    assert binds
    assert max(binds) <= MOST_REQUEST_BINDS
    records = captured_logs()
    (succeeded,) = _events(records, "startup_analysis_succeeded")
    assert (succeeded["run_id"], succeeded["requests"]) == (run.id, BACKLOG)
    (summary,) = _events(records, "analysis_run_summary")
    assert summary["requests"] == BACKLOG


async def test_worker_startup_behind_a_held_lock_leaves_the_backlog_pending(
    db: Sessions,
) -> None:
    await _seed(db)
    await _backlog(db, 3, requested_at=datetime.now(UTC))

    async with analysis_lock():
        await worker_main._startup_analysis(Thresholds.load())

    assert await _runs(db) == []
    # The backlog is untouched and one 'startup' request is queued for the scheduler.
    assert await _queue(db) == _Queue(4, 4, 4, ())
    async with db() as session:
        startup = list(
            await session.scalars(
                select(AnalysisRequest.id).where(
                    AnalysisRequest.reason == worker_main.REASON_STARTUP
                )
            )
        )
    assert len(startup) == 1


async def test_worker_startup_with_no_backlog_still_computes_the_default_run(
    db: Sessions,
) -> None:
    await _seed(db)

    await worker_main._startup_analysis(Thresholds.load())

    (run,) = await _runs(db)
    assert (run.kind, run.status) == ("default", "succeeded")
    assert await _queue(db) == _Queue(0, 0, 0, ())


# ---------------------------------------------------------------------------------------------
# run_analysis: the watermark, explicit ids and the claim's place in the pipeline
# ---------------------------------------------------------------------------------------------


async def test_a_request_above_serve_through_stays_pending(db: Sessions) -> None:
    await _seed(db)
    earlier = await _request(db, requested_at=AT - timedelta(minutes=9))
    async with db() as session, session.begin():
        prior = AnalysisRun(
            kind="default",
            window_start=AT.date() - timedelta(days=28),
            window_end=AT.date() - timedelta(days=1),
            source_day_boundaries=[],
            trigger="event",
            data_modes={},
            dataset_ids={},
            config_hash="0" * 64,
            default_days=28,
            trusted_min_days=14,
            status="succeeded",
            started_at=AT - timedelta(minutes=8),
            finished_at=AT - timedelta(minutes=8),
        )
        session.add(prior)
        await session.flush()
        prior_id = prior.id
        await session.execute(
            text("UPDATE analysis_request SET picked_at = :at, run_id = :run WHERE id = :id"),
            {"at": AT - timedelta(minutes=8), "run": prior_id, "id": earlier},
        )
    first = await _request(db, requested_at=AT - timedelta(minutes=5))
    through = await _request(db, requested_at=AT - timedelta(minutes=4))
    above = await _request(db, requested_at=AT - timedelta(minutes=3))

    run = await runner.run_analysis(
        await runner.default_window(now=AT), "default", "event", serve_through=through, now=AT
    )

    assert await _stamps(db, [earlier, first, through, above]) == {
        earlier: (True, prior_id),
        first: (True, run.id),
        through: (True, run.id),
        above: (False, None),
    }


async def test_explicit_request_ids_beyond_the_bind_limit_are_stamped(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    await _backlog(db, BACKLOG, requested_at=AT - timedelta(minutes=5))
    async with db() as session:
        ids = list(await session.scalars(select(AnalysisRequest.id).order_by(AnalysisRequest.id)))
    assert len(ids) > BIND_LIMIT

    with _request_binds() as binds:
        run = await runner.run_analysis(
            await runner.default_window(now=AT), "default", "event", request_ids=ids, now=AT
        )

    assert run.status == "succeeded"
    assert await _queue(db) == _Queue(BACKLOG, 0, 0, (run.id,))
    assert max(binds) <= MOST_REQUEST_BINDS
    records = captured_logs()
    assert _events(records, "analysis_requests_not_served") == []
    (summary,) = _events(records, "analysis_run_summary")
    assert summary["requests"] == BACKLOG


async def test_explicit_request_ids_already_served_are_reported_not_served(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    pending = [await _request(db, requested_at=AT - timedelta(minutes=5)) for _ in range(2)]
    window = await runner.default_window(now=AT)
    first = await runner.run_analysis(window, "default", "event", request_ids=pending, now=AT)
    captured_logs.clear()

    second = await runner.run_analysis(
        window, "default", "event", request_ids=[*pending, 10**6], now=AT
    )

    assert await _stamps(db, pending) == dict.fromkeys(pending, (True, first.id))
    (warning,) = _events(captured_logs(), "analysis_requests_not_served")
    assert (warning["run_id"], warning["requested"], warning["served"]) == (second.id, 3, 0)


@pytest.mark.parametrize("path", ["serve_through", "request_ids"])
async def test_requests_are_claimed_before_the_context_snapshot_opens(
    db: Sessions, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    await _seed(db)
    ids = [await _request(db, requested_at=AT - timedelta(minutes=5)) for _ in range(3)]
    seen: list[tuple[int, bool, int | None]] = []
    original = metrics.load_context

    async def spying(session: AsyncSession, *args: Any, **kwargs: Any) -> AnalysisContext:
        rows = await session.execute(
            select(AnalysisRequest.id, AnalysisRequest.picked_at, AnalysisRequest.run_id)
        )
        seen.extend((int(row_id), picked is not None, run_id) for row_id, picked, run_id in rows)
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(metrics, "load_context", spying)
    window = await runner.default_window(now=AT)
    if path == "serve_through":
        run = await runner.run_analysis(window, "default", "event", serve_through=ids[-1], now=AT)
    else:
        run = await runner.run_analysis(window, "default", "event", request_ids=ids, now=AT)

    # Inside the run's read-only snapshot every served request already carries its claim.
    assert sorted(seen) == [(request_id, True, run.id) for request_id in ids]


async def test_a_failed_claim_fails_the_run_and_leaves_every_request_pending(
    db: Sessions, captured_logs: Any
) -> None:
    await _seed(db)
    await _backlog(db, 10, requested_at=AT - timedelta(minutes=5))
    async with db() as session:
        through = int(
            (await session.execute(text("SELECT max(id) FROM analysis_request"))).one()[0]
        )
    async with db() as session, session.begin():
        await session.execute(
            text(
                "CREATE FUNCTION test_backlog_refuse_claim() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'claim refused'; END $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER test_backlog_refuse_claim BEFORE UPDATE ON analysis_request "
                "FOR EACH ROW EXECUTE FUNCTION test_backlog_refuse_claim()"
            )
        )
    try:
        with pytest.raises(SQLAlchemyError, match="claim refused"):
            await runner.run_analysis(
                await runner.default_window(now=AT),
                "default",
                "event",
                serve_through=through,
                now=AT,
            )
    finally:
        async with db() as session, session.begin():
            await session.execute(
                text("DROP TRIGGER IF EXISTS test_backlog_refuse_claim ON analysis_request")
            )
            await session.execute(text("DROP FUNCTION IF EXISTS test_backlog_refuse_claim()"))

    (run,) = await _runs(db)
    assert run.status == "failed"
    assert await _queue(db) == _Queue(10, 10, 10, ())
    (failed,) = _events(captured_logs(), "analysis_run_failed")
    assert (failed["run_id"], failed["stage"]) == (run.id, "claim_requests")
    assert _events(captured_logs(), "analysis_run_started") == []


async def test_serve_through_is_validated_before_any_run_is_written(db: Sessions) -> None:
    await _seed(db)
    window = await runner.default_window(now=AT)
    pending = await _request(db, requested_at=AT - timedelta(minutes=5))
    for value, error in (
        (True, TypeError),
        ("7", TypeError),
        (7.0, TypeError),
        (0, ValueError),
        (-3, ValueError),
        (2**63, ValueError),
    ):
        with pytest.raises(error, match="serve_through"):
            await runner.run_analysis(
                window,
                "default",
                "event",
                serve_through=value,  # type: ignore[arg-type]
                now=AT,
            )
    with pytest.raises(ValueError, match="not both"):
        await runner.run_analysis(
            window, "default", "event", request_ids=[pending], serve_through=pending, now=AT
        )
    assert await _runs(db) == []
    assert await _stamps(db, [pending]) == {pending: (False, None)}


async def test_run_in_progress_from_the_runner_claims_nothing(db: Sessions) -> None:
    await _seed(db)
    await _backlog(db, 5, requested_at=AT - timedelta(minutes=5))
    async with db() as session:
        through = int(
            (await session.execute(text("SELECT max(id) FROM analysis_request"))).one()[0]
        )

    async with analysis_lock():
        with pytest.raises(RunInProgress):
            await runner.run_analysis(
                await runner.default_window(now=AT),
                "default",
                "event",
                serve_through=through,
                now=AT,
            )

    assert await _runs(db) == []
    assert await _queue(db) == _Queue(5, 5, 5, ())
