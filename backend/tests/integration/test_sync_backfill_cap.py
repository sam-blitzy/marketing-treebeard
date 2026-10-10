"""A simulated build with ``BACKFILL_DAYS=1200`` backfills every source 180 days, no more.

AAP 0.3.2 ("Initial backfill": 180 days, ``BACKFILL_DAYS``, capped at LinkedIn's 6-month daily
retention) and 0.2.4 (the 180-day backfill stays well inside Google's 37-month retention). On
the PostgreSQL test database (``db`` of ``tests/conftest.py``), with simulated clients
answering from the catalogue's clean every-source world at its fixed clock:

* **Google**: ``google.metrics_daily`` and ``google.search_terms`` are covered from native today
  (the account's time zone) minus 179 days, exactly 180 days, not 1,200.
* **LinkedIn and its companion CRM dataset**: ``linkedin.analytics_daily`` covers the same 180
  native days (UTC), and the lead and CRM ranges start 180 days before the build.

The pure range helpers are proved in ``tests/unit/test_sync_jobs_backfill_span.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.http import RetryPolicy
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.db.models import DatasetFreshness
from ads_engine.settings import Source, get_settings
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import FIXED_NOW, fixed_clock
from ads_engine.sync import jobs
from ads_engine.sync.budget_guard import GoogleCallBudget
from ads_engine.sync.mode import SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]

SCENARIO: Final = "fallback.missing_credentials.case"
"""The catalogue's clean every-source world: Google, LinkedIn with leads, and a CRM funnel."""

BACKFILL_DAYS: Final = "1200"
"""An operator value far above the cap: past Google's 37-month granular retention."""

CAP: Final = 180
"""AAP 0.3.2: the backfill is at most 180 days, LinkedIn's 6-month daily retention."""

GOOGLE_METRICS: Final = ("google.metrics_daily", "google.search_terms")
"""The freshness datasets of Google's metrics job."""


async def _no_sleep(_seconds: float) -> None:
    """The retry policy's sleep: returns at once."""


def _controller(sessions: Sessions, world: SimulatedWorld) -> SourceModeController:
    """A controller on the test database at the world's fixed clock, whose clients never wait."""
    settings = get_settings()
    tick = [0.0]

    async def advance(seconds: float) -> None:
        tick[0] += seconds

    return SourceModeController(
        settings,
        sessionmaker=sessions,
        world=world,
        clock=fixed_clock,
        budget=GoogleCallBudget(settings, sessionmaker=sessions, clock=fixed_clock),
        retry=RetryPolicy(sleep=_no_sleep),
        hubspot_limiter=RateLimiter(4.0, clock=lambda: tick[0], sleep=advance),
    )


async def _build(
    sessions: Sessions, world: SimulatedWorld, source: Source
) -> tuple[int, int | None]:
    """Build, backfill and activate a simulated primary of ``source``; its id and companion."""
    outcome = await _controller(sessions, world).build_dataset(
        source, "simulated", reason="credentials not configured"
    )
    assert outcome.activated, outcome
    assert outcome.dataset_id is not None
    return outcome.dataset_id, outcome.companion_id


async def _coverage(sessions: Sessions, dataset_id: int) -> dict[str, tuple[date, date]]:
    """Every recorded ``[covered_from, covered_through]`` of a dataset, by freshness dataset."""
    async with sessions() as session:
        rows = await session.scalars(
            select(DatasetFreshness).where(DatasetFreshness.dataset_id == dataset_id)
        )
        found: dict[str, tuple[date, date]] = {}
        for row in rows:
            if row.covered_from is not None and row.covered_through is not None:
                found[row.dataset] = (row.covered_from, row.covered_through)
        return found


@pytest.fixture
def world(
    set_env: Callable[..., None], make_world: Callable[..., SimulatedWorld]
) -> SimulatedWorld:
    """Simulated data with ``BACKFILL_DAYS=1200``; the world at its fixed clock."""
    set_env(DATA_MODE="simulated", BACKFILL_DAYS=BACKFILL_DAYS)
    assert get_settings().backfill_days == int(BACKFILL_DAYS)
    return make_world(SCENARIO, clock=fixed_clock)


async def test_google_backfill_is_capped_at_180_days(db: Sessions, world: SimulatedWorld) -> None:
    """Both Google metrics datasets cover exactly 180 native days ending today."""
    dataset_id, _ = await _build(db, world, "google")
    async with db() as session:
        account = await jobs._account_row(session, dataset_id, "google")
    assert account is not None
    today = FIXED_NOW.astimezone(ZoneInfo(account.timezone or "UTC")).date()

    coverage = await _coverage(db, dataset_id)

    for name in GOOGLE_METRICS:
        assert coverage[name] == (today - timedelta(days=CAP - 1), today), name


async def test_linkedin_and_crm_backfill_is_capped_at_180_days(
    db: Sessions, world: SimulatedWorld
) -> None:
    """LinkedIn analytics cover 180 native days; lead and CRM ranges start 180 days back."""
    dataset_id, companion_id = await _build(db, world, "linkedin")
    assert companion_id is not None
    today = FIXED_NOW.astimezone(UTC).date()
    earliest = (FIXED_NOW - timedelta(days=CAP)).astimezone(UTC).date()

    linkedin = await _coverage(db, dataset_id)
    crm = await _coverage(db, companion_id)

    assert linkedin["linkedin.analytics_daily"] == (today - timedelta(days=CAP - 1), today)
    assert linkedin["linkedin.leads"][0] == earliest
    assert crm["hubspot.contacts"][0] == earliest
