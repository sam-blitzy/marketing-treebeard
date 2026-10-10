"""Unit tests for the pure helpers of ``ads_engine.api.routes.sources``.

``_is_stale`` is the screen's dataset-age rule. It judges age as elapsed time between two
instants, exactly as ``ads_engine.analysis.context.Freshness.is_stale`` does: a dataset that
never succeeded is stale, so is one whose last success is more than ``stale_after`` before
``now``, and the comparison stays exact across a DST change.

``_reported_mode`` is the mode ``GET /sources`` reports: a stored ``live`` with no active
primary dataset is a placeholder that never verified live access and reports no mode, while
``simulated`` and an activated ``live`` are reported as stored.

Pure: no application, database or network.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis.context import Freshness
from ads_engine.api.routes.sources import _is_stale, _reported_mode

NEW_YORK = ZoneInfo("America/New_York")
# Oct 31 noon EDT to Nov 1 noon EST: 25 real hours, though 24 by the wall clock.
FALL_BACK_SUCCESS = datetime(2026, 10, 31, 12, tzinfo=NEW_YORK)
FALL_BACK_NOW = datetime(2026, 11, 1, 12, tzinfo=NEW_YORK)


def test_never_succeeded_is_stale() -> None:
    now = datetime(2026, 10, 6, 2, tzinfo=UTC)
    assert _is_stale(None, now=now, stale_after=timedelta(hours=24))


@pytest.mark.parametrize(("stale_after_hours", "stale"), [(24, True), (25, False)])
def test_age_is_elapsed_time_across_dst_fall_back(stale_after_hours: int, stale: bool) -> None:
    stale_after = timedelta(hours=stale_after_hours)
    assert _is_stale(FALL_BACK_SUCCESS, now=FALL_BACK_NOW, stale_after=stale_after) is stale


@pytest.mark.parametrize("stale_after_hours", [24, 25])
def test_age_matches_freshness_is_stale(stale_after_hours: int) -> None:
    stale_after = timedelta(hours=stale_after_hours)
    row = Freshness(
        11,
        "google.metrics_daily",
        FALL_BACK_SUCCESS,
        FALL_BACK_SUCCESS,
        None,
        date(2026, 11, 1),
        "complete",
    )
    expected = row.is_stale(
        now=FALL_BACK_NOW, stale_after=stale_after, window_end=date(2026, 11, 1)
    )
    assert _is_stale(FALL_BACK_SUCCESS, now=FALL_BACK_NOW, stale_after=stale_after) is expected


def test_mixed_zones_compare_as_instants() -> None:
    success = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=NEW_YORK)  # 05:30 UTC (EDT)
    now = datetime(2026, 11, 2, 5, 30, tzinfo=UTC)
    assert not _is_stale(success, now=now, stale_after=timedelta(hours=24))
    assert _is_stale(success, now=now + timedelta(seconds=1), stale_after=timedelta(hours=24))


def test_naive_instants_are_rejected() -> None:
    naive = datetime(2026, 11, 1, 12)  # noqa: DTZ001 - a naive instant is the point
    with pytest.raises(ValueError, match="naive"):
        _is_stale(FALL_BACK_SUCCESS, now=naive, stale_after=timedelta(hours=24))
    with pytest.raises(ValueError, match="naive"):
        _is_stale(naive, now=FALL_BACK_NOW, stale_after=timedelta(hours=24))
    with pytest.raises(ValueError, match="naive"):
        _is_stale(None, now=naive, stale_after=timedelta(hours=24))


@pytest.mark.parametrize(
    ("stored_mode", "active_dataset_id", "reported"),
    [
        pytest.param("live", None, None, id="live-placeholder-without-dataset"),
        pytest.param("live", 7, "live", id="live-with-active-dataset"),
        pytest.param("simulated", None, "simulated", id="simulated-before-its-dataset"),
        pytest.param("simulated", 7, "simulated", id="simulated-with-active-dataset"),
        pytest.param(None, None, None, id="no-connection-row"),
    ],
)
def test_reported_mode_is_the_established_mode(
    stored_mode: str | None, active_dataset_id: int | None, reported: str | None
) -> None:
    assert _reported_mode(stored_mode, active_dataset_id) == reported
