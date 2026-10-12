"""The backfill span of ``sync/jobs.py``: ``BACKFILL_DAYS`` capped at 180 days for every source.

AAP 0.3.2 ("Initial backfill": 180 days, ``BACKFILL_DAYS``, capped at LinkedIn's 6-month daily
retention) and 0.2.4 (the 180-day backfill stays well inside Google's 37-month retention).
Proved here on the pure range helpers, with no database, client or clock:

* **Every source capped**: with ``BACKFILL_DAYS=1200`` the Google backfill range (the job's
  own range with and without repair) and LinkedIn's are exactly 180 native days ending today;
  the lead backfill and the CRM scope span 180 days too.
* **Capped, not rejected**: ``BACKFILL_DAYS=1200`` still loads from the environment.
* **Below the cap, as set**: ``BACKFILL_DAYS=90`` gives every source 90 days, and every value
  up to 180 is read unchanged.
* **Other kinds unaffected**: the incremental and restatement ranges ignore
  ``BACKFILL_DAYS``.
* **LinkedIn's retention clamp kept**: a LinkedIn repair never widens past 180 days.

``tests/integration/test_sync_backfill_cap.py`` proves the same cap on the freshness rows a
simulated build records.
"""

from collections.abc import Callable
from datetime import date, timedelta
from typing import Final

import pytest

from ads_engine.db.models import DatasetFreshness
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.sync import jobs
from ads_engine.sync.freshness import REASON_INTERRUPTED
from ads_engine.sync.mode import LINKEDIN_RETENTION_DAYS

TODAY: Final = date(2026, 10, 1)
"""The native today every range here ends on."""

CAP: Final = 180
"""AAP 0.3.2: the backfill is at most 180 days, LinkedIn's 6-month daily retention."""

METRICS_SOURCES: Final[tuple[Source, ...]] = ("google", "linkedin")
"""The sources with daily metrics, and so a metrics backfill range."""


def _days(span: tuple[date, date]) -> int:
    """The number of native days an inclusive ``[start, end]`` range covers."""
    start, end = span
    return (end - start).days + 1


def test_cap_is_linkedin_daily_retention_of_180_days() -> None:
    assert LINKEDIN_RETENTION_DAYS == CAP


@pytest.mark.parametrize("repair", [True, False])
def test_google_backfill_above_cap_reads_180_days(repair: bool) -> None:
    """``BACKFILL_DAYS=1200`` backfills Google 180 days, not 1,200."""
    settings = Settings(backfill_days=1200)

    span = jobs._metrics_range("backfill", "google", TODAY, settings, None, repair=repair)

    assert span == (TODAY - timedelta(days=CAP - 1), TODAY)
    assert _days(span) == CAP


def test_linkedin_backfill_above_cap_reads_180_days() -> None:
    settings = Settings(backfill_days=1200)

    span = jobs._metrics_range("backfill", "linkedin", TODAY, settings, None)

    assert span == (TODAY - timedelta(days=CAP - 1), TODAY)
    assert _days(span) == CAP


def test_lead_and_crm_spans_above_cap_are_180_days() -> None:
    settings = Settings(backfill_days=1200)

    assert jobs._span_days(settings) == CAP
    assert jobs._crm_span_days(settings) == CAP


def test_backfill_days_above_cap_loads_and_is_capped(set_env: Callable[..., None]) -> None:
    """The cap applies at sync time; a larger ``BACKFILL_DAYS`` is accepted at load."""
    set_env(BACKFILL_DAYS="1200")
    settings = get_settings()

    assert settings.backfill_days == 1200
    assert jobs._span_days(settings) == CAP
    for source in METRICS_SOURCES:
        span = jobs._metrics_range("backfill", source, TODAY, settings, None)
        assert _days(span) == CAP, source


@pytest.mark.parametrize("source", METRICS_SOURCES)
def test_backfill_below_cap_reads_backfill_days(source: Source) -> None:
    """``BACKFILL_DAYS=90`` backfills every source 90 days."""
    settings = Settings(backfill_days=90)

    span = jobs._metrics_range("backfill", source, TODAY, settings, None)

    assert span == (TODAY - timedelta(days=89), TODAY)
    assert jobs._span_days(settings) == 90
    assert jobs._crm_span_days(settings) == 90


@pytest.mark.parametrize("backfill_days", [1, 2, 35, 90, 179, 180, 181, 365, 1200])
def test_every_span_is_backfill_days_capped_at_180(backfill_days: int) -> None:
    settings = Settings(backfill_days=backfill_days)
    expected = min(backfill_days, CAP)

    assert jobs._span_days(settings) == expected
    assert jobs._crm_span_days(settings) == expected
    for source in METRICS_SOURCES:
        span = jobs._metrics_range("backfill", source, TODAY, settings, None)
        assert span[1] == TODAY, source
        assert _days(span) == expected, source


@pytest.mark.parametrize(
    ("kind", "source", "days"),
    [
        ("incremental", "google", jobs.INCREMENTAL_DAYS),
        ("incremental", "linkedin", jobs.INCREMENTAL_DAYS),
        ("restatement", "google", jobs.RESTATEMENT_DAYS["google"]),
        ("restatement", "linkedin", jobs.RESTATEMENT_DAYS["linkedin"]),
    ],
)
def test_incremental_and_restatement_ignore_backfill_days(
    kind: jobs.MetricsKind, source: Source, days: int
) -> None:
    for backfill_days in (1, 1200):
        settings = Settings(backfill_days=backfill_days)
        span = jobs._metrics_range(kind, source, TODAY, settings, None)
        assert span == (TODAY - timedelta(days=days - 1), TODAY), backfill_days


def test_linkedin_repair_never_widens_past_its_retention() -> None:
    """A LinkedIn row needing repair widens to ``covered_from`` only within 180 days."""
    row = DatasetFreshness(
        dataset_id=1,
        dataset="linkedin.analytics_daily",
        covered_from=TODAY - timedelta(days=400),
        covered_through=TODAY,
        completeness="incomplete",
        reason=REASON_INTERRUPTED,
    )

    span = jobs._metrics_range("incremental", "linkedin", TODAY, Settings(), row)

    assert span == (TODAY - timedelta(days=CAP - 1), TODAY)
