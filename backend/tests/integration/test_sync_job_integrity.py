"""A database integrity violation ends a sync job as a failed attempt, never as a raw error.

Proves the ``IntegrityError`` outcome of ``_JobBase`` in ``sync/jobs.py`` on the PostgreSQL test
database (``db`` of ``tests/conftest.py``), with simulated clients answering from the
catalogue's clean world. Every violation is genuine: PostgreSQL refuses a row written through
``repositories.upsert_facts``, so the job meets the driver's real ``ForeignKeyViolation`` or
``CheckViolation``. Each test's docstring names the guarantee it proves:

* **Domain outcome** (finding F04; AAP 0.3.3 defect row): ``run()`` returns a failed
  ``JobResult`` whose ``integrity`` names the violation's kind, SQLSTATE, table and constraint,
  and ``run_source_jobs`` returns a report whose ``has_defect`` is true; nothing is retried,
  simulated or handed to ``on_error``.
* **Attempts recorded on every attempt** (AAP 0.5.2): every freshness dataset the violation
  failed records the attempt (``last_attempt_at`` moves, the sanitized reason) while the last
  good data keeps its success, coverage and completeness; a first-ever attempt records
  ``unknown``; a dataset completed before the violation keeps its success; an interruption
  noted before it is handed over first and keeps its own reason; the ``sync_run`` rows close
  ``failed`` with the sanitized reason, never ``job failed unexpectedly``.
* **Sanitized diagnostics** (AAP 0.11.1 logging; 0.3.2 personal data): no stored reason, result
  or log event carries a value of the refused row or the driver's detail line.
* **Logging points** (AAP 0.11.1): one ``sync.job.integrity_failed`` at error level and one
  failed ``sync.job.finish`` per run.
* **Other errors unchanged**: a cancellation, a non-integrity error and an error raised while
  the violation is being recorded still propagate as before.

Every clock is fixed (:data:`T0` builds the datasets, :data:`T1` runs the job under test),
retries never wait in real time, and no test sleeps.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Final

import psycopg
import pytest
import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ads_engine.connectors.errors import ConnectorError, TransientError
from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db import repositories
from ads_engine.db.base import Base
from ads_engine.db.models import (
    DatasetFreshness,
    FactCampaignDaily,
    SourceConnection,
    SourceDataset,
    SyncRun,
)
from ads_engine.settings import Source, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_CLOCK, FIXED_NOW
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.freshness import FreshnessOutcome
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Row = Mapping[str, Any]

SCENARIO: Final = "fallback.missing_credentials.case"
"""The catalogue's clean every-source world: a Google search campaign with daily metrics."""

T0: Final = FIXED_NOW
"""The instant every dataset is built and activated at."""

T1: Final = FIXED_NOW + timedelta(hours=1)
"""The instant the job under test runs at; its ``sync_run`` rows start here."""

BACKFILL_DAYS: Final = 35
"""``BACKFILL_DAYS`` of every build."""

ENTITIES: Final = "google.entities"
METRICS_DAILY: Final = "google.metrics_daily"
SEARCH_TERMS: Final = "google.search_terms"
GOOGLE_METRICS: Final = (METRICS_DAILY, SEARCH_TERMS)
"""The freshness datasets of Google's metrics job."""

UNEXPECTED: Final = "job failed unexpectedly"
CANCELLED: Final = "job cancelled"
"""The ``sync_run.error`` of rows a raised or cancelled run left open."""

BOGUS_CAMPAIGN_ID: Final = 7_700_000_000_123
"""A campaign id no dataset stores: the refused row's key value, which nothing may repeat."""

NEGATIVE_IMPRESSIONS: Final = -987_654_321
"""An impression count the non-negative ``CHECK`` refuses, which nothing may repeat."""

FK_CONSTRAINT: Final = "fk_fact_campaign_daily_campaign_id_campaign"
CHECK_CONSTRAINT: Final = "ck_fact_campaign_daily_non_negative"


def _reason(kind: str, constraint: str, sqlstate: str) -> str:
    """The sanitized reason a violation on ``fact_campaign_daily`` records."""
    return (
        f"database integrity violation ({kind}) on fact_campaign_daily: "
        f"constraint {constraint} (SQLSTATE {sqlstate})"
    )


# ---------------------------------------------------------------------------------------------
# Logging isolation
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uncached_loggers() -> Iterator[None]:
    """Keep every logger lazy, so ``capture_logs`` sees ``sync/jobs.py``'s events."""
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


def _events(entries: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """The captured entries of one event."""
    return [entry for entry in entries if entry.get("event") == name]


def _one(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """The single captured entry of one event; fails when there are none or several."""
    found = _events(entries, name)
    assert len(found) == 1, f"{name}: {len(found)} entries"
    return found[0]


# ---------------------------------------------------------------------------------------------
# The lab: controllers, datasets, contexts and database reads
# ---------------------------------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    """The retry policy's sleep: returns at once."""


@dataclasses.dataclass(slots=True)
class Lab:
    """Controllers on the test database, simulating every source from one world."""

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

    async def seed(self, source: Source) -> int:
        """Insert an empty active simulated primary of ``source``, with its connection."""
        async with self.sessions() as session, session.begin():
            dataset = SourceDataset(
                source=source,
                mode="simulated",
                state="active",
                role="primary",
                reason="seeded by the job integrity tests",
                created_at=T0,
                activated_at=T0,
            )
            session.add(dataset)
            await session.flush()
            session.add(
                SourceConnection(
                    source=source,
                    mode="simulated",
                    status="ok",
                    reason="seeded by the job integrity tests",
                    active_dataset_id=dataset.id,
                )
            )
            return dataset.id

    @asynccontextmanager
    async def context(self, source: Source, dataset_id: int) -> AsyncIterator[jobs.JobContext]:
        """The context of ``dataset_id`` at :data:`T1` on simulated clients of ``source``."""
        controller = self.controller(T1)
        async with controller.open_clients(source, "simulated") as clients:
            yield await jobs.JobContext.for_dataset(controller, dataset_id, clients, now=T1)

    async def runs(self, job: str) -> dict[str | None, SyncRun]:
        """The ``sync_run`` rows of ``job`` started at :data:`T1`, by freshness dataset."""
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SyncRun).where(SyncRun.job == job, SyncRun.started_at == T1)
            )
            found = {row.dataset: row for row in rows}
        assert found, f"no {job} sync_run row started at T1"
        return found

    async def freshness(self, dataset_id: int, name: str) -> DatasetFreshness | None:
        """One ``dataset_freshness`` row."""
        async with self.sessions() as session:
            return await session.scalar(
                select(DatasetFreshness).where(
                    DatasetFreshness.dataset_id == dataset_id, DatasetFreshness.dataset == name
                )
            )

    async def connection(self, source: Source) -> SourceConnection:
        """The ``source_connection`` row of ``source``."""
        async with self.sessions() as session:
            row = await session.scalar(
                select(SourceConnection).where(SourceConnection.source == source)
            )
        assert row is not None, f"no {source} connection"
        return row


@pytest.fixture
def lab(
    db: Sessions,
    make_world: Callable[..., SimulatedWorld],
    set_env: Callable[..., None],
) -> Lab:
    """The lab on the emptied test database, with ``BACKFILL_DAYS`` set to a short span."""
    set_env(BACKFILL_DAYS=str(BACKFILL_DAYS))
    return Lab(sessions=db, world=make_world(SCENARIO, clock=FIXED_CLOCK))


@pytest.fixture
def handed_over(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the class of every error handed to ``on_error``, which still runs as before."""
    seen: list[str] = []
    real = SourceModeController.on_error

    async def on_error(
        self: SourceModeController, source: Source, exc: ConnectorError, **kwargs: Any
    ) -> Any:
        seen.append(type(exc).__name__)
        return await real(self, source, exc, **kwargs)

    monkeypatch.setattr(SourceModeController, "on_error", on_error)
    return seen


# ---------------------------------------------------------------------------------------------
# Genuine violations
# ---------------------------------------------------------------------------------------------


def _refuse_campaign_daily(monkeypatch: pytest.MonkeyPatch, change: Mapping[str, Any]) -> list[int]:
    """Make every ``fact_campaign_daily`` upsert write ``change`` into its rows.

    The rows still go through the real ``repositories.upsert_facts``, so PostgreSQL itself
    refuses them. Returns the list of row counts each altered call received.
    """
    real = repositories.upsert_facts
    altered: list[int] = []

    async def upsert_facts(
        table: type[Base], rows: Sequence[Row], dataset_id: int, **kwargs: Any
    ) -> int:
        if table is FactCampaignDaily and rows:
            altered.append(len(rows))
            rows = [{**row, **change} for row in rows]
        return await real(table, rows, dataset_id, **kwargs)

    monkeypatch.setattr(repositories, "upsert_facts", upsert_facts)
    return altered


VIOLATIONS: Final = {
    "foreign_key": (
        {"campaign_id": BOGUS_CAMPAIGN_ID},
        jobs.IntegrityFailure(
            kind="foreign_key_violation",
            sqlstate="23503",
            table="fact_campaign_daily",
            constraint=FK_CONSTRAINT,
        ),
        str(BOGUS_CAMPAIGN_ID),
    ),
    "check": (
        {"impressions": NEGATIVE_IMPRESSIONS},
        jobs.IntegrityFailure(
            kind="check_violation",
            sqlstate="23514",
            table="fact_campaign_daily",
            constraint=CHECK_CONSTRAINT,
        ),
        str(NEGATIVE_IMPRESSIONS),
    ),
}
"""Per violation: the change that makes PostgreSQL refuse the rows, the diagnostics expected,
and the refused value that must appear nowhere."""


class _Violates(jobs._JobBase):
    """Completes :data:`ENTITIES`, notes an interruption, then writes a row PostgreSQL refuses.

    The refused row references a campaign the seeded dataset does not store, so the database
    raises a genuine ``ForeignKeyViolation``. ``google.metrics_daily`` has no freshness row
    yet; ``google.search_terms`` is failed by the noted interruption.
    """

    job = jobs.JOB_METRICS

    def __init__(self, ctx: jobs.JobContext, interruption: TransientError | None) -> None:
        super().__init__(ctx)
        self.interruption = interruption

    def freshness_datasets(self) -> tuple[str, ...]:
        """Three datasets: one completed, one interrupted, one left to the violation."""
        return (ENTITIES, METRICS_DAILY, SEARCH_TERMS)

    async def _execute(self) -> None:
        """Complete, note the interruption, then upsert the refused fact row."""
        ctx = self.ctx
        async with ctx.sessionmaker() as session, session.begin():
            self._count(ENTITIES, 2)
            await self._complete(
                session, {ENTITIES: jobs._success(ctx.now, covered=None, reasons=())}
            )
        self._calls += 3
        if self.interruption is not None:
            self._interrupted(self.interruption, dataset=SEARCH_TERMS)
        async with ctx.sessionmaker() as session, session.begin():
            await repositories.upsert_facts(
                FactCampaignDaily,
                [
                    {
                        "campaign_id": BOGUS_CAMPAIGN_ID,
                        "date": date(2026, 10, 1),
                        "cost": 1,
                        "impressions": 10,
                        "clicks": 1,
                    }
                ],
                ctx.dataset_id,
                session=session,
            )


class _Raises(jobs._JobBase):
    """Raises a given error from its execution, after counting rows and calls."""

    job = jobs.JOB_ENTITIES

    def __init__(self, ctx: jobs.JobContext, error: BaseException) -> None:
        super().__init__(ctx)
        self.error = error

    def freshness_datasets(self) -> tuple[str, ...]:
        """One dataset."""
        return (ENTITIES,)

    async def _execute(self) -> None:
        """Count, then raise."""
        self._count(ENTITIES, 4)
        self._calls += 2
        raise self.error


# ---------------------------------------------------------------------------------------------
# Domain outcome and recorded attempts on a real job
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("violation", sorted(VIOLATIONS))
async def test_violation_in_a_real_job_returns_a_failed_result_and_records_the_attempt(
    lab: Lab,
    monkeypatch: pytest.MonkeyPatch,
    handed_over: list[str],
    violation: str,
) -> None:
    """Domain outcome and recorded attempts: Google's metrics job meets a genuine violation.

    Through ``run_source_jobs`` (the scheduler's and the CLI's entry point), the metrics job's
    first ``fact_campaign_daily`` batch is refused by PostgreSQL. The call returns a report
    with a defect instead of raising; the job's result names the violation; both freshness
    datasets record the failed attempt and keep their last good data; both ``sync_run`` rows
    close ``failed`` with the sanitized reason; nothing reaches ``on_error``; no stored text,
    result or log event repeats the refused value.
    """
    change, expected, refused_value = VIOLATIONS[violation]
    dataset_id = await lab.build("google")
    before = {name: await lab.freshness(dataset_id, name) for name in GOOGLE_METRICS}
    assert all(row is not None and row.completeness == "complete" for row in before.values())
    altered = _refuse_campaign_daily(monkeypatch, change)
    reason = expected.reason
    assert reason == _reason(expected.kind, str(expected.constraint), str(expected.sqlstate))
    raw: list[str] = []
    real_from_error = jobs.IntegrityFailure.from_error

    def from_error(error: IntegrityError) -> jobs.IntegrityFailure:
        raw.append(str(error))
        return real_from_error(error)

    monkeypatch.setattr(jobs.IntegrityFailure, "from_error", from_error)

    with capture_logs() as entries:
        report = await jobs.run_source_jobs(
            "google",
            trigger="manual",
            jobs=[jobs.JOB_METRICS],
            controller=lab.controller(T1),
            now=T1,
        )

    assert altered, "the metrics job wrote fact_campaign_daily rows"
    (result,) = report.results
    assert (result.job, result.status, result.disposition) == (jobs.JOB_METRICS, "failed", None)
    assert result.integrity == expected
    assert result.error == reason
    assert report.has_defect
    assert not report.ok
    assert handed_over == [], "an integrity violation is never handed to on_error"
    assert set(result.freshness) == set(GOOGLE_METRICS)
    assert all(
        (outcome.succeeded, outcome.reason) == (False, reason)
        for outcome in result.freshness.values()
    )
    assert result.to_json()["integrity"] == {
        "kind": expected.kind,
        "sqlstate": expected.sqlstate,
        "table": "fact_campaign_daily",
        "constraint": expected.constraint,
    }

    stored: list[str | None] = []
    for name, previous in before.items():
        assert previous is not None
        after = await lab.freshness(dataset_id, name)
        assert after is not None
        stored.append(after.reason)
        assert (after.last_attempt_at, after.reason) == (T1, reason), name
        assert (
            after.last_success_at,
            after.covered_from,
            after.covered_through,
            after.completeness,
        ) == (
            previous.last_success_at,
            previous.covered_from,
            previous.covered_through,
            previous.completeness,
        ), f"{name}: the last good data keeps its standing"

    runs = await lab.runs(jobs.JOB_METRICS)
    expected_runs = dict.fromkeys(GOOGLE_METRICS, ("failed", reason))
    assert {name: (row.status, row.error) for name, row in runs.items()} == expected_runs
    assert all(row.finished_at == T1 for row in runs.values())

    failed = _one(entries, "sync.job.integrity_failed")
    assert {
        key: failed[key]
        for key in (
            "log_level",
            "source",
            "job",
            "dataset_id",
            "datasets",
            "kind",
            "sqlstate",
            "table",
            "constraint",
        )
    } == {
        "log_level": "error",
        "source": "google",
        "job": jobs.JOB_METRICS,
        "dataset_id": dataset_id,
        "datasets": list(GOOGLE_METRICS),
        "kind": expected.kind,
        "sqlstate": expected.sqlstate,
        "table": "fact_campaign_daily",
        "constraint": expected.constraint,
    }
    finish = _one(entries, "sync.job.finish")
    assert (finish["status"], finish["error"], finish["log_level"]) == (
        "failed",
        None,
        "warning",
    )
    assert set(finish["datasets"]) == set(GOOGLE_METRICS)

    # The driver's own text does carry the refused value and its detail line: the job never
    # repeats it.
    assert len(raw) == 1
    assert refused_value in raw[0]
    assert "DETAIL" in raw[0]
    texts = [repr(entries), repr(result.to_json()), *(row.error for row in runs.values()), *stored]
    for text in texts:
        assert refused_value not in str(text), "a refused value leaked"
        assert "DETAIL" not in str(text), "the driver's detail line leaked"


async def test_first_attempt_records_unknown_and_settled_datasets_stand(
    lab: Lab, handed_over: list[str]
) -> None:
    """Recorded attempts: settled datasets stand, interruptions go first, first attempts unknown.

    On a seeded dataset with no freshness row, the job completes ``google.entities``, notes a
    transient interruption of ``google.search_terms`` and then has a genuine foreign-key
    violation. The interruption alone is handed to ``on_error`` and keeps its own reason; the
    violation fails only ``google.metrics_daily``, whose first-ever attempt records
    ``unknown``; the completed dataset keeps its success; the result is failed with the
    violation's diagnostics and the run's rows and calls.
    """
    dataset_id = await lab.seed("google")
    interruption = TransientError(
        "HTTP 503 after 3 attempts", source="google", endpoint="search Q-G6", http_status=503
    )
    expected = _reason("foreign_key_violation", FK_CONSTRAINT, "23503")

    with capture_logs() as entries:
        async with lab.context("google", dataset_id) as ctx:
            result = await _Violates(ctx, interruption).run()

    assert handed_over == ["TransientError"]
    assert (result.status, result.error, result.rows, result.calls) == ("failed", expected, 2, 3)
    assert result.integrity is not None
    assert result.integrity.kind == "foreign_key_violation"
    assert result.integrity.constraint == FK_CONSTRAINT
    assert {name: outcome.succeeded for name, outcome in result.freshness.items()} == {
        ENTITIES: True,
        METRICS_DAILY: False,
        SEARCH_TERMS: False,
    }

    metrics = await lab.freshness(dataset_id, METRICS_DAILY)
    assert metrics is not None
    assert (
        metrics.last_attempt_at,
        metrics.last_success_at,
        metrics.completeness,
        metrics.reason,
    ) == (T1, None, "unknown", expected), "a first-ever failed attempt is unknown"
    terms = await lab.freshness(dataset_id, SEARCH_TERMS)
    assert terms is not None
    assert (terms.last_attempt_at, terms.reason) == (T1, interruption.reason)
    entities = await lab.freshness(dataset_id, ENTITIES)
    assert entities is not None
    assert (entities.last_success_at, entities.completeness) == (T1, "complete")
    assert (await lab.connection("google")).status == "stale", "the interruption was handed over"

    runs = await lab.runs(jobs.JOB_METRICS)
    assert {name: (row.status, row.error) for name, row in runs.items()} == {
        ENTITIES: ("succeeded", None),
        METRICS_DAILY: ("failed", expected),
        SEARCH_TERMS: ("failed", interruption.reason),
    }
    assert _one(entries, "sync.job.integrity_failed")["datasets"] == [METRICS_DAILY]
    assert _one(entries, "sync.job.finish")["status"] == "failed"
    assert str(BOGUS_CAMPAIGN_ID) not in repr(entries)


# ---------------------------------------------------------------------------------------------
# Other errors unchanged
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RuntimeError("a bug, not a violation"), id="runtime-error"),
        pytest.param(asyncio.CancelledError(), id="cancelled"),
    ],
)
async def test_non_integrity_errors_still_propagate_unchanged(
    lab: Lab, error: BaseException
) -> None:
    """Other errors unchanged: a bug and a cancellation propagate as the same object.

    Neither is recorded as an integrity failure: the open row closes with the run's generic
    reason (``job failed unexpectedly`` or ``job cancelled``) and no freshness row is written.
    """
    dataset_id = await lab.seed("google")
    expected = CANCELLED if isinstance(error, asyncio.CancelledError) else UNEXPECTED

    with capture_logs() as entries, pytest.raises(type(error)) as raised:
        async with lab.context("google", dataset_id) as ctx:
            await _Raises(ctx, error).run()

    assert raised.value is error
    assert _events(entries, "sync.job.integrity_failed") == []
    assert _one(entries, "sync.job.finish")["error"] == type(error).__name__
    runs = await lab.runs(jobs.JOB_ENTITIES)
    assert [(row.status, row.error) for row in runs.values()] == [("failed", expected)]
    assert await lab.freshness(dataset_id, ENTITIES) is None


async def test_failure_while_recording_the_violation_propagates(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other errors unchanged: a database error while recording the violation is raised.

    The violation is logged, then recording its failed attempt fails as a dropped connection
    would. That error propagates from ``run()`` (``run_source_jobs``' callers isolate it as
    before), the open rows are closed ``failed`` and one failed finish names its class.
    """
    dataset_id = await lab.seed("google")
    failure = OperationalError("INSERT INTO dataset_freshness", {}, ConnectionError("lost"))

    async def refuse(_self: jobs._JobBase, _outcomes: Mapping[str, FreshnessOutcome]) -> None:
        raise failure

    monkeypatch.setattr(jobs._JobBase, "_record_failures", refuse)

    with capture_logs() as entries, pytest.raises(OperationalError) as raised:
        async with lab.context("google", dataset_id) as ctx:
            await _Violates(ctx, None).run()

    assert raised.value is failure
    assert isinstance(raised.value.__context__, IntegrityError)
    assert len(_events(entries, "sync.job.integrity_failed")) == 1
    finish = _one(entries, "sync.job.finish")
    assert (finish["status"], finish["error"]) == ("failed", "OperationalError")
    runs = await lab.runs(jobs.JOB_METRICS)
    assert {name: row.status for name, row in runs.items()} == {
        ENTITIES: "succeeded",
        METRICS_DAILY: "failed",
        SEARCH_TERMS: "failed",
    }
    assert all(row.finished_at is not None for row in runs.values())


# ---------------------------------------------------------------------------------------------
# Sanitized diagnostics without a database
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("orig", "kind", "sqlstate", "reason"),
    [
        pytest.param(
            psycopg.errors.UniqueViolation("Key (id)=(42) already exists."),
            "unique_violation",
            "23505",
            "database integrity violation (unique_violation) (SQLSTATE 23505)",
            id="unique",
        ),
        pytest.param(
            psycopg.errors.NotNullViolation("null value in column"),
            "not_null_violation",
            "23502",
            "database integrity violation (not_null_violation) (SQLSTATE 23502)",
            id="not-null",
        ),
        pytest.param(
            psycopg.errors.ExclusionViolation("conflicting key value"),
            "exclusion_violation",
            "23P01",
            "database integrity violation (exclusion_violation) (SQLSTATE 23P01)",
            id="exclusion",
        ),
        pytest.param(
            psycopg.errors.RestrictViolation("Key (id)=(42) is still referenced."),
            "integrity_violation",
            "23001",
            "database integrity violation (integrity_violation) (SQLSTATE 23001)",
            id="other-class-23",
        ),
        pytest.param(
            ValueError("Key (id)=(42) from another driver"),
            "integrity_violation",
            None,
            "database integrity violation (integrity_violation)",
            id="not-psycopg",
        ),
    ],
)
def test_diagnostics_never_carry_the_driver_text(
    orig: Exception, kind: str, sqlstate: str | None, reason: str
) -> None:
    """Sanitized diagnostics: only the class, SQLSTATE and reported names are read.

    Without a server report the table and constraint are ``None`` and left out of the reason,
    and neither the driver's message nor its key values appear anywhere.
    """
    error = IntegrityError("INSERT INTO t (id) VALUES (%(id)s)", {"id": 42}, orig)

    failure = jobs.IntegrityFailure.from_error(error)

    assert (failure.kind, failure.sqlstate, failure.table, failure.constraint) == (
        kind,
        sqlstate,
        None,
        None,
    )
    assert failure.reason == reason
    assert "42" not in repr(failure.to_json())
    assert "42" not in failure.reason


def test_report_defect_covers_integrity_and_query_defects() -> None:
    """Domain outcome: ``has_defect`` is true for an integrity failure or a query defect only."""

    def result(disposition: Any, integrity: jobs.IntegrityFailure | None) -> jobs.JobResult:
        return jobs.JobResult(
            job=jobs.JOB_METRICS,
            source="google",
            dataset_id=1,
            status="failed",
            rows=0,
            calls=0,
            freshness={},
            disposition=disposition,
            error="failed",
            duration_ms=1,
            integrity=integrity,
        )

    violation = jobs.IntegrityFailure(
        kind="check_violation", sqlstate="23514", table="t", constraint="ck_t_valid"
    )

    def report(*results: jobs.JobResult) -> jobs.SourceSyncReport:
        return jobs.SourceSyncReport("google", "manual", results=results)

    assert report(result(None, violation)).has_defect
    assert report(result("defect", None)).has_defect
    assert not report(result("stale", None), result(None, None)).has_defect
    assert result(None, None).to_json()["integrity"] is None
