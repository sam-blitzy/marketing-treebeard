"""Unit tests for ``ads_engine.analysis.window``: native-day analysis windows (AAP 0.5.4.1).

They pin the window contract every rule and the API rely on: the default end is the earlier
of yesterday in the Google account's time zone and yesterday UTC; the default window spans
``window.default_days`` days ending there; custom windows end no later than the default end
and span 1 to ``window.max_days`` days; the short-window, sustained and segment-window flags
read the configured values; every source shares one calendar-date range while keeping its own
boundary; and instant bounds are half-open and DST-true. Pure: no database and no network.
"""

import copy
import dataclasses
import doctest
import pickle
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

from ads_engine.analysis import window as window_module
from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CONTENT_ASSETS_FILE,
    HUBSPOT_MAPPING_FILE,
    THRESHOLDS_FILE,
    Thresholds,
)
from ads_engine.analysis.window import (
    DAY_SOURCES,
    SOURCE_LABELS,
    UTC_BOUNDARY,
    AnalysisWindow,
    InvalidWindow,
    SourceDayInfo,
    SourceDays,
    default_end,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NEW_YORK = "America/New_York"
TOKYO = "Asia/Tokyo"
# 02:00 UTC on 2026-10-06 is 22:00 on 2026-10-05 in New York: the Google account's zone is
# behind UTC, so yesterday there (2026-10-04) is earlier than yesterday UTC (2026-10-05).
NOW = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
DEFAULT_END = date(2026, 10, 4)
DEFAULT_START = date(2026, 9, 7)
NY_SOURCES = SourceDayInfo(NEW_YORK)
ONE_DAY = timedelta(days=1)


class _NoOffset(tzinfo):
    """A tzinfo whose offset is unknown, which makes a datetime naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


def _shipped_trees() -> dict[str, Any]:
    """Parse the three shipped config files with plain ``yaml.safe_load``."""
    return {
        name: yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in CONFIG_FILES
    }


def _thresholds(**window: object) -> Thresholds:
    """Build the shipped configuration with the given ``window.*`` values replaced."""
    trees = copy.deepcopy(_shipped_trees())
    trees[THRESHOLDS_FILE]["window"].update(window)
    return Thresholds.from_trees(
        thresholds=trees[THRESHOLDS_FILE],
        content_assets=trees[CONTENT_ASSETS_FILE],
        hubspot_mapping=trees[HUBSPOT_MAPPING_FILE],
        source_dir=CONFIG_DIR,
    )


@pytest.fixture(scope="module")
def shipped() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


def _custom(
    config: Thresholds,
    days: int,
    *,
    end: date = DEFAULT_END,
    sources: SourceDayInfo = NY_SOURCES,
) -> AnalysisWindow:
    """A custom window of ``days`` days ending on ``end``, built at :data:`NOW`."""
    return AnalysisWindow.custom(
        end - timedelta(days=days - 1), end, config=config, sources=sources, now=NOW
    )


def _source_days(
    start: date = DEFAULT_START, end: date = DEFAULT_END
) -> tuple[SourceDays, SourceDays]:
    return (
        SourceDays(source="google", start=start, end=end, boundary=NEW_YORK),
        SourceDays(source="linkedin", start=start, end=end, boundary=UTC_BOUNDARY),
    )


def _window_fields(**overrides: object) -> dict[str, Any]:
    """Valid ``AnalysisWindow`` constructor arguments, with ``overrides`` applied."""
    fields: dict[str, Any] = {
        "start": DEFAULT_START,
        "end": DEFAULT_END,
        "default_end": DEFAULT_END,
        "source_days": _source_days(),
        "trusted_min_days": 14,
        "default_days": 28,
        "sustained_min_days": 7,
        "presets": (14, 28, 56, 90),
        "max_days": 180,
    }
    fields.update(overrides)
    return fields


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_module_constants() -> None:
    assert DAY_SOURCES == ("google", "linkedin")
    assert SOURCE_LABELS == {"google": "Google", "linkedin": "LinkedIn"}
    assert UTC_BOUNDARY == "UTC"


def test_docstring_examples_hold() -> None:
    results = doctest.testmod(window_module, report=False)

    assert results.attempted > 0
    assert results.failed == 0


# ---------------------------------------------------------------------------
# default_end (the earlier of Google-zone yesterday and UTC yesterday)
# ---------------------------------------------------------------------------


def test_default_end_takes_google_yesterday_when_the_account_zone_is_behind_utc() -> None:
    assert default_end(NY_SOURCES, now=NOW) == DEFAULT_END
    # The same instant in another offset gives the same native dates.
    assert default_end(NY_SOURCES, now=NOW.astimezone(ZoneInfo(TOKYO))) == DEFAULT_END


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 11:00 in Tokyo, the same date as UTC: both yesterdays are 2026-10-05.
        (NOW, date(2026, 10, 5)),
        # 05:00 on 2026-10-07 in Tokyo, a day ahead of UTC: UTC yesterday (2026-10-05) wins
        # over Tokyo yesterday (2026-10-06).
        (datetime(2026, 10, 6, 20, 0, tzinfo=UTC), date(2026, 10, 5)),
    ],
    ids=["same-date", "tokyo-a-day-ahead"],
)
def test_default_end_takes_utc_yesterday_when_the_account_zone_is_ahead(
    now: datetime, expected: date
) -> None:
    assert default_end(SourceDayInfo(TOKYO), now=now) == expected


def test_default_end_without_a_google_zone_or_google_day_source_is_utc_yesterday() -> None:
    assert default_end(SourceDayInfo(None), now=NOW) == date(2026, 10, 5)
    linkedin_only = SourceDayInfo(NEW_YORK, ("linkedin",))
    assert default_end(linkedin_only, now=NOW) == date(2026, 10, 5)


def test_default_end_excludes_today_from_the_first_instant_of_the_utc_day() -> None:
    midnight = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    utc_only = SourceDayInfo(None)
    assert default_end(utc_only, now=midnight) == date(2026, 10, 5)
    assert default_end(utc_only, now=midnight - timedelta(microseconds=1)) == date(2026, 10, 4)


def test_default_end_defaults_to_the_current_instant() -> None:
    before = datetime.now(UTC).date() - ONE_DAY
    result = default_end(SourceDayInfo(None))
    after = datetime.now(UTC).date() - ONE_DAY
    assert result in {before, after}


@pytest.mark.parametrize(
    "naive",
    [NOW.replace(tzinfo=None), NOW.replace(tzinfo=_NoOffset())],
    ids=["no-tzinfo", "tzinfo-without-offset"],
)
def test_default_end_rejects_a_naive_now(naive: datetime) -> None:
    with pytest.raises(ValueError, match="now must be a tz-aware datetime"):
        default_end(NY_SOURCES, now=naive)


def test_default_end_rejects_a_now_that_is_not_a_datetime() -> None:
    not_an_instant: Any = date(2026, 10, 6)
    with pytest.raises(TypeError, match="now must be a datetime"):
        default_end(NY_SOURCES, now=not_an_instant)


# ---------------------------------------------------------------------------
# SourceDayInfo
# ---------------------------------------------------------------------------


def test_source_day_info_boundaries() -> None:
    assert NY_SOURCES.day_sources == ("google", "linkedin")
    assert NY_SOURCES.boundary("google") == NEW_YORK
    assert NY_SOURCES.boundary("linkedin") == UTC_BOUNDARY
    assert NY_SOURCES.boundaries() == {"google": NEW_YORK, "linkedin": UTC_BOUNDARY}

    unknown_zone = SourceDayInfo(None)
    assert unknown_zone.boundary("google") == UTC_BOUNDARY
    assert unknown_zone.boundaries() == {"google": UTC_BOUNDARY, "linkedin": UTC_BOUNDARY}


def test_source_day_info_keeps_the_given_order_and_stores_a_list_as_a_tuple() -> None:
    sources: Any = ["linkedin", "google"]
    info = SourceDayInfo(NEW_YORK, sources)
    assert info.day_sources == ("linkedin", "google")
    assert isinstance(info.day_sources, tuple)
    assert list(info.boundaries()) == ["linkedin", "google"]


@pytest.mark.parametrize(
    ("day_sources", "message"),
    [
        ((), "must name at least one day source"),
        ([], "must name at least one day source"),
        (("google", "google"), "must not repeat a source"),
        (("google", "hubspot"), "day_sources item must be one of"),
        ("google", "must be a tuple of day sources"),
        ({"google"}, "must be a tuple of day sources"),
        (None, "must be a tuple of day sources"),
    ],
    ids=["empty", "empty-list", "repeated", "unknown", "string", "set", "none"],
)
def test_source_day_info_rejects_invalid_day_sources(day_sources: Any, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        SourceDayInfo(NEW_YORK, day_sources)


@pytest.mark.parametrize(
    "zone",
    ["Not/AZone", "America", "../etc/passwd", "/etc/passwd", "", 123],
    ids=["unknown", "directory", "traversal", "absolute", "empty", "not-a-string"],
)
def test_source_day_info_rejects_an_invalid_timezone(zone: Any) -> None:
    with pytest.raises(ValueError, match="google_timezone must be an IANA time zone name"):
        SourceDayInfo(zone)


# ---------------------------------------------------------------------------
# SourceDays
# ---------------------------------------------------------------------------


def test_source_days_to_json_shape() -> None:
    item = SourceDays(source="google", start=DEFAULT_START, end=DEFAULT_END, boundary=NEW_YORK)
    assert item.to_json() == {
        "source": "google",
        "start": "2026-09-07",
        "end": "2026-10-04",
        "boundary": NEW_YORK,
    }


def test_source_days_accepts_a_single_day() -> None:
    item = SourceDays(source="linkedin", start=DEFAULT_END, end=DEFAULT_END, boundary="UTC")
    assert item.start == item.end == DEFAULT_END


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        ({"source": "hubspot"}, ValueError, "source must be one of"),
        ({"start": NOW}, TypeError, "start must be a datetime.date"),
        ({"end": NOW}, TypeError, "end must be a datetime.date"),
        ({"start": "2026-09-07"}, TypeError, "start must be a datetime.date"),
        ({"end": DEFAULT_START - ONE_DAY}, ValueError, "end 2026-09-06 precedes start 2026-09-07"),
        ({"boundary": "Mars/Olympus"}, ValueError, "boundary must be an IANA time zone name"),
        ({"boundary": ""}, ValueError, "boundary must be an IANA time zone name"),
    ],
    ids=["source", "datetime-start", "datetime-end", "text-start", "reversed", "zone", "empty"],
)
def test_source_days_validation(
    changes: dict[str, Any], error: type[Exception], message: str
) -> None:
    arguments: dict[str, Any] = {
        "source": "google",
        "start": DEFAULT_START,
        "end": DEFAULT_END,
        "boundary": NEW_YORK,
    }
    arguments.update(changes)
    with pytest.raises(error, match=re.escape(message)):
        SourceDays(**arguments)


# ---------------------------------------------------------------------------
# AnalysisWindow.default
# ---------------------------------------------------------------------------


def test_default_window_spans_default_days_ending_on_the_default_end(
    shipped: Thresholds,
) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    assert window.start == DEFAULT_START == DEFAULT_END - timedelta(days=27)
    assert window.end == window.default_end == DEFAULT_END
    assert window.days == shipped.default_days == 28
    assert window.source_days == (
        SourceDays(source="google", start=DEFAULT_START, end=DEFAULT_END, boundary=NEW_YORK),
        SourceDays(source="linkedin", start=DEFAULT_START, end=DEFAULT_END, boundary="UTC"),
    )
    assert window.trusted_min_days == shipped.trusted_min_days == 14
    assert window.default_days == 28
    assert window.sustained_min_days == shipped.sustained_min_days == 7
    assert window.presets == shipped.presets == (14, 28, 56, 90)
    assert window.max_days == shipped.max_days == 180
    assert not window.short_window
    assert window.sustained_ok
    assert window.is_default_shape
    assert window.is_segment_window()
    assert window.boundaries_differ


def test_default_window_reads_the_configured_default_days() -> None:
    config = _thresholds(default_days=21, presets=[14, 21, 28])
    window = AnalysisWindow.default(config, NY_SOURCES, now=NOW)

    assert window.days == 21
    assert window.start == date(2026, 9, 14)
    assert window.is_default_shape
    assert window.is_segment_window()


def test_default_window_follows_the_utc_end_when_google_is_ahead(shipped: Thresholds) -> None:
    later = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)
    window = AnalysisWindow.default(shipped, SourceDayInfo(TOKYO), now=later)

    assert window.end == window.default_end == date(2026, 10, 5)
    assert window.boundary("google") == TOKYO
    assert window.boundary("linkedin") == UTC_BOUNDARY


def test_default_window_propagates_now_validation(shipped: Thresholds) -> None:
    with pytest.raises(ValueError, match="now must be a tz-aware datetime"):
        AnalysisWindow.default(shipped, NY_SOURCES, now=NOW.replace(tzinfo=None))


def test_default_window_with_a_single_day_source(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, SourceDayInfo(NEW_YORK, ("linkedin",)), now=NOW)

    assert window.end == date(2026, 10, 5)
    assert [item.source for item in window.source_days] == ["linkedin"]
    assert not window.boundaries_differ
    with pytest.raises(KeyError):
        window.boundary("google")


# ---------------------------------------------------------------------------
# Flags: short window (P5), sustained minimum, default shape, segment window (C3a)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "short"), [(14, False), (13, True), (1, True), (28, False)])
def test_short_window_uses_the_shipped_trusted_minimum(
    shipped: Thresholds, days: int, short: bool
) -> None:
    assert _custom(shipped, days).short_window is short


@pytest.mark.parametrize(("days", "short"), [(20, True), (21, False), (14, True)])
def test_short_window_uses_a_configured_trusted_minimum_of_21(days: int, short: bool) -> None:
    window = _custom(_thresholds(trusted_min_days=21), days)

    assert window.trusted_min_days == 21
    assert window.short_window is short


@pytest.mark.parametrize(("days", "ok"), [(7, True), (6, False), (1, False), (28, True)])
def test_sustained_ok_uses_the_configured_minimum(shipped: Thresholds, days: int, ok: bool) -> None:
    assert _custom(shipped, days).sustained_ok is ok


def test_sustained_ok_follows_a_configured_minimum() -> None:
    config = _thresholds(sustained_min_days=10)

    assert not _custom(config, 9).sustained_ok
    assert _custom(config, 10).sustained_ok


def test_is_default_shape(shipped: Thresholds) -> None:
    # A custom window equal to the default has the default shape; the run kind is the
    # caller's decision.
    assert _custom(shipped, 28).is_default_shape
    assert not _custom(shipped, 27).is_default_shape
    assert not _custom(shipped, 28, end=DEFAULT_END - ONE_DAY).is_default_shape


@pytest.mark.parametrize("days", [14, 28, 56, 90])
def test_every_preset_ending_on_the_default_end_is_a_segment_window(
    shipped: Thresholds, days: int
) -> None:
    assert _custom(shipped, days).is_segment_window()


def test_non_preset_or_earlier_windows_are_not_segment_windows(shipped: Thresholds) -> None:
    assert not _custom(shipped, 30).is_segment_window()
    assert not _custom(shipped, 28, end=DEFAULT_END - ONE_DAY).is_segment_window()
    assert not _custom(shipped, 14, end=DEFAULT_END - ONE_DAY).is_segment_window()


# ---------------------------------------------------------------------------
# AnalysisWindow.custom (POST /runs bounds) and InvalidWindow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [1, 180])
def test_custom_accepts_one_to_max_days(shipped: Thresholds, days: int) -> None:
    window = _custom(shipped, days)

    assert window.days == days
    assert window.end == window.default_end == DEFAULT_END
    assert window.start == DEFAULT_END - timedelta(days=days - 1)
    assert all(item.start == window.start and item.end == window.end for item in window.source_days)


def test_custom_rejects_an_end_after_the_default_end(shipped: Thresholds) -> None:
    with pytest.raises(InvalidWindow) as caught:
        AnalysisWindow.custom(
            DEFAULT_START,
            DEFAULT_END + ONE_DAY,
            config=shipped,
            sources=NY_SOURCES,
            now=NOW,
        )
    assert caught.value.code == "end_after_default_end"
    assert "2026-10-05" in caught.value.message
    assert "default window end 2026-10-04" in caught.value.message


def test_custom_rejects_an_end_before_its_start(shipped: Thresholds) -> None:
    with pytest.raises(InvalidWindow) as caught:
        AnalysisWindow.custom(
            DEFAULT_END, DEFAULT_END - ONE_DAY, config=shipped, sources=NY_SOURCES, now=NOW
        )
    assert caught.value.code == "end_before_start"
    assert caught.value.message == "Window end 2026-10-03 is before its start 2026-10-04"


def test_custom_rejects_a_window_longer_than_max_days(shipped: Thresholds) -> None:
    with pytest.raises(InvalidWindow) as caught:
        _custom(shipped, 181)
    assert caught.value.code == "too_long"
    assert caught.value.message == (
        "Window of 181 days exceeds the 180-day maximum (window.max_days)"
    )


def test_custom_reads_the_configured_max_days() -> None:
    config = _thresholds(max_days=90)

    assert _custom(config, 90).days == 90
    with pytest.raises(InvalidWindow) as caught:
        _custom(config, 91)
    assert caught.value.code == "too_long"
    assert "the 90-day maximum" in caught.value.message


def test_custom_window_equal_to_the_default_matches_it(shipped: Thresholds) -> None:
    default = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    custom = AnalysisWindow.custom(
        DEFAULT_START, DEFAULT_END, config=shipped, sources=NY_SOURCES, now=NOW
    )
    assert custom == default


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (datetime(2026, 9, 7, tzinfo=UTC), DEFAULT_END, "start must be a datetime.date"),
        (DEFAULT_START, datetime(2026, 10, 4, tzinfo=UTC), "end must be a datetime.date"),
        ("2026-09-07", DEFAULT_END, "start must be a datetime.date"),
    ],
    ids=["datetime-start", "datetime-end", "text-start"],
)
def test_custom_rejects_non_native_dates(
    shipped: Thresholds, start: Any, end: Any, message: str
) -> None:
    with pytest.raises(TypeError, match=re.escape(message)):
        AnalysisWindow.custom(start, end, config=shipped, sources=NY_SOURCES, now=NOW)


def test_custom_rejects_a_naive_now(shipped: Thresholds) -> None:
    with pytest.raises(ValueError, match="now must be a tz-aware datetime"):
        AnalysisWindow.custom(
            DEFAULT_START,
            DEFAULT_END,
            config=shipped,
            sources=NY_SOURCES,
            now=NOW.replace(tzinfo=None),
        )


def test_custom_defaults_now_to_the_current_instant(shipped: Thresholds) -> None:
    end = datetime.now(UTC).date() - timedelta(days=3)
    window = AnalysisWindow.custom(end - ONE_DAY, end, config=shipped, sources=NY_SOURCES)

    assert window.days == 2
    assert window.default_end >= end


@pytest.mark.parametrize(
    ("given", "missing"),
    [((), "config, sources"), (("config",), "sources"), (("sources",), "config")],
    ids=["neither", "config-only", "sources-only"],
)
def test_class_level_custom_without_its_configuration_raises_a_clear_type_error(
    shipped: Thresholds, given: tuple[str, ...], missing: str
) -> None:
    available: dict[str, Any] = {"config": shipped, "sources": NY_SOURCES}
    arguments = {name: available[name] for name in given}
    unconfigured: Any = AnalysisWindow.custom

    with pytest.raises(TypeError) as caught:
        unconfigured(DEFAULT_START, DEFAULT_END, now=NOW, **arguments)
    assert str(caught.value) == (
        "AnalysisWindow.custom(start, end) on the class needs the keyword arguments config "
        f"and sources (missing: {missing}); to reuse a window's configuration call "
        "custom(start, end) on that window, for example "
        "AnalysisWindow.default(config, sources).custom(start, end)"
    )


# ---------------------------------------------------------------------------
# window.custom(start, end): the AAP 0.5.1 two-argument form on a configured window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (DEFAULT_START, DEFAULT_END),
        (DEFAULT_END, DEFAULT_END),
        (DEFAULT_END - timedelta(days=179), DEFAULT_END),
        (date(2026, 8, 1), date(2026, 8, 31)),
    ],
    ids=["default-span", "one-day", "max-days", "earlier-month"],
)
def test_bound_custom_equals_the_class_level_custom(
    shipped: Thresholds, start: date, end: date
) -> None:
    configured = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    bound = configured.custom(start, end)

    assert bound == AnalysisWindow.custom(start, end, config=shipped, sources=NY_SOURCES, now=NOW)
    assert (bound.start, bound.end, bound.default_end) == (start, end, DEFAULT_END)
    assert bound.to_json() == {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": (end - start).days + 1,
        "source_days": [
            {
                "source": "google",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "boundary": NEW_YORK,
            },
            {
                "source": "linkedin",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "boundary": UTC_BOUNDARY,
            },
        ],
        "boundaries_differ": True,
        "description": (
            f"Google {start.isoformat()} to {end.isoformat()} (America/New_York); "
            "LinkedIn same dates (UTC)"
        ),
    }


@pytest.mark.parametrize(
    ("start", "end", "code", "message"),
    [
        (
            DEFAULT_END,
            DEFAULT_END - ONE_DAY,
            "end_before_start",
            "Window end 2026-10-03 is before its start 2026-10-04",
        ),
        (
            DEFAULT_START,
            DEFAULT_END + ONE_DAY,
            "end_after_default_end",
            "Window end 2026-10-05 is after the default window end 2026-10-04, the latest date "
            "complete in every source's native day",
        ),
        (
            DEFAULT_END - timedelta(days=180),
            DEFAULT_END,
            "too_long",
            "Window of 181 days exceeds the 180-day maximum (window.max_days)",
        ),
    ],
    ids=["end-before-start", "end-after-default-end", "too-long"],
)
def test_bound_custom_raises_the_class_level_codes_and_messages(
    shipped: Thresholds, start: date, end: date, code: str, message: str
) -> None:
    # ``too_short`` cannot be reached through either form: a reversed range is reported as
    # ``end_before_start`` first, and any other range spans at least 1 day.
    configured = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    with pytest.raises(InvalidWindow) as bound:
        configured.custom(start, end)
    with pytest.raises(InvalidWindow) as explicit:
        AnalysisWindow.custom(start, end, config=shipped, sources=NY_SOURCES, now=NOW)

    assert (bound.value.code, bound.value.message) == (code, message)
    assert (explicit.value.code, explicit.value.message) == (code, message)


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (datetime(2026, 9, 7, tzinfo=UTC), DEFAULT_END, "start must be a datetime.date"),
        (DEFAULT_START, datetime(2026, 10, 4, tzinfo=UTC), "end must be a datetime.date"),
        ("2026-09-07", DEFAULT_END, "start must be a datetime.date"),
    ],
    ids=["datetime-start", "datetime-end", "text-start"],
)
def test_bound_custom_rejects_non_native_dates(
    shipped: Thresholds, start: Any, end: Any, message: str
) -> None:
    configured = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    with pytest.raises(TypeError, match=re.escape(message)):
        configured.custom(start, end)


def test_bound_custom_takes_only_the_two_dates(shipped: Thresholds) -> None:
    configured: Any = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    with pytest.raises(TypeError, match="unexpected keyword argument 'config'"):
        configured.custom(DEFAULT_START, DEFAULT_END, config=shipped)
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'end'"):
        configured.custom(DEFAULT_START)


def test_bound_custom_keeps_the_configured_values_and_boundaries() -> None:
    config = _thresholds(
        default_days=21,
        presets=[14, 21, 28, 56],
        trusted_min_days=10,
        sustained_min_days=5,
        max_days=60,
    )
    configured = AnalysisWindow.default(config, NY_SOURCES, now=NOW)

    bound = configured.custom(DEFAULT_END - timedelta(days=8), DEFAULT_END)

    assert bound.days == 9
    assert bound.default_end == DEFAULT_END
    assert (
        bound.trusted_min_days,
        bound.default_days,
        bound.sustained_min_days,
        bound.presets,
        bound.max_days,
    ) == (10, 21, 5, (14, 21, 28, 56), 60)
    assert [(item.source, item.boundary) for item in bound.source_days] == [
        ("google", NEW_YORK),
        ("linkedin", UTC_BOUNDARY),
    ]
    assert all(item.start == bound.start and item.end == bound.end for item in bound.source_days)
    assert bound.boundaries_differ
    assert bound.short_window
    assert bound.sustained_ok
    assert not bound.is_default_shape
    assert configured.custom(DEFAULT_END - timedelta(days=20), DEFAULT_END).is_segment_window()

    assert configured.custom(DEFAULT_END - timedelta(days=59), DEFAULT_END).days == 60
    with pytest.raises(InvalidWindow) as caught:
        configured.custom(DEFAULT_END - timedelta(days=60), DEFAULT_END)
    assert caught.value.code == "too_long"
    assert caught.value.message == "Window of 61 days exceeds the 60-day maximum (window.max_days)"


@pytest.mark.parametrize(
    ("sources", "source", "boundary", "latest"),
    [
        (SourceDayInfo(NEW_YORK, ("linkedin",)), "linkedin", UTC_BOUNDARY, date(2026, 10, 5)),
        (SourceDayInfo(NEW_YORK, ("google",)), "google", NEW_YORK, DEFAULT_END),
        (SourceDayInfo(None, ("google",)), "google", UTC_BOUNDARY, date(2026, 10, 5)),
    ],
    ids=["linkedin-only", "google-only", "google-without-zone"],
)
def test_bound_custom_on_a_single_source_window(
    shipped: Thresholds, sources: SourceDayInfo, source: str, boundary: str, latest: date
) -> None:
    configured = AnalysisWindow.default(shipped, sources, now=NOW)

    bound = configured.custom(latest - timedelta(days=6), latest)

    assert bound == AnalysisWindow.custom(
        latest - timedelta(days=6), latest, config=shipped, sources=sources, now=NOW
    )
    assert [(item.source, item.boundary) for item in bound.source_days] == [(source, boundary)]
    assert not bound.boundaries_differ
    with pytest.raises(InvalidWindow) as caught:
        configured.custom(latest, latest + ONE_DAY)
    assert caught.value.code == "end_after_default_end"
    assert f"default window end {latest.isoformat()}" in caught.value.message


def test_bound_custom_is_bounded_by_the_default_end_not_the_window_end(
    shipped: Thresholds,
) -> None:
    earlier = _custom(shipped, 7, end=DEFAULT_END - timedelta(days=10))

    assert earlier.end < earlier.default_end == DEFAULT_END
    widened = earlier.custom(DEFAULT_START, DEFAULT_END)
    assert widened == AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    with pytest.raises(InvalidWindow) as caught:
        earlier.custom(DEFAULT_START, DEFAULT_END + ONE_DAY)
    assert caught.value.code == "end_after_default_end"


def test_custom_is_a_descriptor_and_not_a_dataclass_field() -> None:
    assert [field.name for field in dataclasses.fields(AnalysisWindow)] == [
        "start",
        "end",
        "default_end",
        "source_days",
        "trusted_min_days",
        "default_days",
        "sustained_min_days",
        "presets",
        "max_days",
    ]
    descriptor = AnalysisWindow.__dict__["custom"]
    with pytest.raises(TypeError, match="must be read from AnalysisWindow or a window"):
        descriptor.__get__(None)


def test_invalid_window_carries_code_and_message() -> None:
    error = InvalidWindow("too_long", "Window of 181 days exceeds the 180-day maximum")

    assert isinstance(error, ValueError)
    assert error.code == "too_long"
    assert error.message == "Window of 181 days exceeds the 180-day maximum"
    assert str(error) == error.message
    assert error.args == (error.message,)


def test_invalid_window_pickles_with_both_constructor_arguments() -> None:
    error = InvalidWindow("end_after_default_end", "Window end 2026-10-05 is after ...")

    assert error.__reduce__() == (InvalidWindow, ("end_after_default_end", error.message))
    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 - round-trip of our own object
    assert type(restored) is InvalidWindow
    assert restored.code == error.code
    assert restored.message == error.message
    assert str(restored) == str(error)


# ---------------------------------------------------------------------------
# AnalysisWindow construction invariants
# ---------------------------------------------------------------------------


def test_direct_construction_normalises_tuple_fields() -> None:
    window = AnalysisWindow(
        **_window_fields(source_days=list(_source_days()), presets=[90, 28, 14, 28, 56])
    )

    assert window.source_days == _source_days()
    assert isinstance(window.source_days, tuple)
    assert window.presets == (14, 28, 56, 90)


def test_presets_are_stored_sorted_and_distinct_from_the_configuration() -> None:
    config = _thresholds(presets=[90, 28, 14, 28])
    window = AnalysisWindow.default(config, NY_SOURCES, now=NOW)

    assert window.presets == (14, 28, 90)
    assert not _custom(config, 56).is_segment_window()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("start", datetime(2026, 9, 7, tzinfo=UTC), "start must be a datetime.date"),
        ("end", datetime(2026, 10, 4, tzinfo=UTC), "end must be a datetime.date"),
        ("default_end", datetime(2026, 10, 4, tzinfo=UTC), "default_end must be a datetime.date"),
        ("default_end", "2026-10-04", "default_end must be a datetime.date"),
    ],
    ids=["start", "end", "default-end", "text-default-end"],
)
def test_direct_construction_rejects_datetimes_where_dates_are_expected(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(TypeError, match=re.escape(message)):
        AnalysisWindow(**_window_fields(**{field: value}))


def test_direct_construction_enforces_the_window_bounds() -> None:
    with pytest.raises(InvalidWindow) as reversed_window:
        AnalysisWindow(**_window_fields(end=DEFAULT_START - ONE_DAY))
    assert reversed_window.value.code == "end_before_start"

    with pytest.raises(InvalidWindow) as late_window:
        AnalysisWindow(**_window_fields(default_end=DEFAULT_END - ONE_DAY))
    assert late_window.value.code == "end_after_default_end"
    assert "default window end 2026-10-03" in late_window.value.message


def test_direct_construction_enforces_max_days_with_the_custom_message() -> None:
    assert AnalysisWindow(**_window_fields(max_days=28)).days == 28

    with pytest.raises(InvalidWindow) as long_window:
        AnalysisWindow(**_window_fields(max_days=27))
    assert long_window.value.code == "too_long"
    assert long_window.value.message == (
        "Window of 28 days exceeds the 27-day maximum (window.max_days)"
    )


@pytest.mark.parametrize(
    ("source_days", "message"),
    [
        ((), "source_days must list at least one day source"),
        (
            (_source_days()[0], _source_days()[0]),
            "source_days must not repeat a source",
        ),
        (
            _source_days(start=DEFAULT_START + ONE_DAY),
            "source_days for google spans 2026-09-08 to 2026-10-04, but the window spans "
            "2026-09-07 to 2026-10-04",
        ),
        (
            (
                _source_days()[0],
                SourceDays("linkedin", DEFAULT_START, DEFAULT_END - ONE_DAY, "UTC"),
            ),
            "source_days for linkedin spans",
        ),
        ((_source_days()[0], {"source": "linkedin"}), "source_days items must be SourceDays"),
        (_source_days()[0], "source_days must be a tuple of SourceDays"),
        (None, "source_days must be a tuple of SourceDays"),
    ],
    ids=["empty", "repeated", "start-mismatch", "end-mismatch", "not-source-days", "bare", "none"],
)
def test_direct_construction_validates_source_days(source_days: object, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        AnalysisWindow(**_window_fields(source_days=source_days))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("trusted_min_days", 0, "trusted_min_days must be a positive integer"),
        ("default_days", True, "default_days must be a positive integer"),
        ("sustained_min_days", 7.0, "sustained_min_days must be a positive integer"),
        ("presets", (), "presets must be a non-empty tuple of day counts"),
        ("presets", 28, "presets must be a non-empty tuple of day counts"),
        ("presets", (14, 0), "presets item must be a positive integer"),
        ("presets", (14, "28"), "presets item must be a positive integer"),
        ("max_days", 0, "max_days must be a positive integer"),
        ("max_days", True, "max_days must be a positive integer"),
        ("max_days", "180", "max_days must be a positive integer"),
    ],
    ids=[
        "trusted-zero",
        "default-bool",
        "sustained-float",
        "no-presets",
        "bare",
        "zero",
        "text",
        "max-zero",
        "max-bool",
        "max-text",
    ],
)
def test_direct_construction_validates_day_counts(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        AnalysisWindow(**_window_fields(**{field: value}))


# ---------------------------------------------------------------------------
# Native dates: dates, contains, halves, first_n, last_n
# ---------------------------------------------------------------------------


def test_dates_lists_every_native_day_ascending(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    days = window.dates()

    assert len(days) == window.days == 28
    assert days[0] == DEFAULT_START
    assert days[-1] == DEFAULT_END
    assert all(later - earlier == ONE_DAY for earlier, later in pairwise(days))
    assert _custom(shipped, 1).dates() == [DEFAULT_END]


def test_contains_includes_both_ends_only(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    assert window.contains(DEFAULT_START)
    assert window.contains(DEFAULT_END)
    assert window.contains(date(2026, 9, 20))
    assert not window.contains(DEFAULT_START - ONE_DAY)
    assert not window.contains(DEFAULT_END + ONE_DAY)
    with pytest.raises(TypeError, match=r"d must be a datetime\.date"):
        window.contains(datetime(2026, 9, 20, tzinfo=UTC))


@pytest.mark.parametrize(
    ("days", "first_length", "second_length"),
    [(28, 14, 14), (15, 8, 7), (14, 7, 7), (3, 2, 1), (2, 1, 1)],
)
def test_halves_put_the_middle_day_in_the_first_half(
    shipped: Thresholds, days: int, first_length: int, second_length: int
) -> None:
    window = _custom(shipped, days)
    (first_start, first_end), (second_start, second_end) = window.halves()

    assert first_start == window.start
    assert second_end == window.end
    assert second_start == first_end + ONE_DAY
    assert (first_end - first_start).days + 1 == first_length
    assert (second_end - second_start).days + 1 == second_length


def test_halves_need_two_days(shipped: Thresholds) -> None:
    with pytest.raises(ValueError, match="a 1-day window cannot be split into two halves"):
        _custom(shipped, 1).halves()


def test_first_n_and_last_n_bounds(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    assert window.first_n(7) == (DEFAULT_START, date(2026, 9, 13))
    assert window.last_n(7) == (date(2026, 9, 28), DEFAULT_END)
    assert window.first_n(1) == (DEFAULT_START, DEFAULT_START)
    assert window.last_n(1) == (DEFAULT_END, DEFAULT_END)
    assert window.first_n(28) == window.last_n(28) == (DEFAULT_START, DEFAULT_END)


@pytest.mark.parametrize(
    ("n", "message"),
    [
        (29, "n must be at most the window's 28 days, found 29"),
        (0, "n must be a positive integer number of days"),
        (-1, "n must be a positive integer number of days"),
        (True, "n must be a positive integer number of days"),
        (7.0, "n must be a positive integer number of days"),
    ],
    ids=["above-days", "zero", "negative", "bool", "float"],
)
def test_first_n_and_last_n_reject_invalid_lengths(
    shipped: Thresholds, n: Any, message: str
) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    with pytest.raises(ValueError, match=re.escape(message)):
        window.first_n(n)
    with pytest.raises(ValueError, match=re.escape(message)):
        window.last_n(n)


# ---------------------------------------------------------------------------
# Instants: utc_bounds, day_bounds
# ---------------------------------------------------------------------------


def test_utc_bounds_are_half_open_utc_midnights(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    lower, upper = window.utc_bounds()

    assert lower == datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
    assert upper == datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
    assert lower.tzinfo is UTC
    assert upper.tzinfo is UTC
    assert upper - lower == timedelta(days=28)
    # Half-open: the bounds are the first instant of the start date and of the day after the
    # end date, so a lead in the end date's last microsecond counts and one at the next UTC
    # midnight belongs to the following day.
    assert (lower, upper) == (
        AnalysisWindow.day_bounds(DEFAULT_START, UTC_BOUNDARY)[0],
        AnalysisWindow.day_bounds(DEFAULT_END, UTC_BOUNDARY)[1],
    )
    assert lower <= upper - timedelta(microseconds=1) < upper


@pytest.mark.parametrize(
    ("day", "lower", "upper", "hours"),
    [
        (
            date(2026, 3, 8),
            datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
            datetime(2026, 3, 9, 4, 0, tzinfo=UTC),
            23,
        ),
        (
            date(2026, 11, 1),
            datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
            25,
        ),
        (
            date(2026, 10, 4),
            datetime(2026, 10, 4, 4, 0, tzinfo=UTC),
            datetime(2026, 10, 5, 4, 0, tzinfo=UTC),
            24,
        ),
    ],
    ids=["spring-forward", "fall-back", "ordinary"],
)
def test_day_bounds_have_the_true_length_across_new_york_dst(
    day: date, lower: datetime, upper: datetime, hours: int
) -> None:
    bounds = AnalysisWindow.day_bounds(day, NEW_YORK)

    assert bounds == (lower, upper)
    assert bounds[0].tzinfo is UTC
    assert bounds[1].tzinfo is UTC
    assert bounds[1] - bounds[0] == timedelta(hours=hours)
    # Consecutive native days tile the timeline with no gap and no overlap.
    assert AnalysisWindow.day_bounds(day + ONE_DAY, NEW_YORK)[0] == bounds[1]


def test_day_bounds_resolve_a_midnight_in_a_dst_gap_to_the_first_instant_of_the_day() -> None:
    # Havana springs forward at 00:00 on 2026-03-08, so that local midnight never exists;
    # the day starts at 01:00 CDT (05:00 UTC).
    havana = "America/Havana"
    lower, upper = AnalysisWindow.day_bounds(date(2026, 3, 8), havana)

    assert lower == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    assert upper == datetime(2026, 3, 9, 4, 0, tzinfo=UTC)
    assert upper - lower == timedelta(hours=23)
    zone = ZoneInfo(havana)
    assert lower.astimezone(zone).hour == 1
    assert lower.astimezone(zone).date() == date(2026, 3, 8)
    assert (lower - timedelta(microseconds=1)).astimezone(zone).date() == date(2026, 3, 7)
    assert AnalysisWindow.day_bounds(date(2026, 3, 7), havana)[1] == lower


def test_day_bounds_in_utc() -> None:
    assert AnalysisWindow.day_bounds(DEFAULT_END, UTC_BOUNDARY) == (
        datetime(2026, 10, 4, tzinfo=UTC),
        datetime(2026, 10, 5, tzinfo=UTC),
    )


def test_day_bounds_validate_their_arguments() -> None:
    with pytest.raises(TypeError, match=r"d must be a datetime\.date"):
        AnalysisWindow.day_bounds(datetime(2026, 10, 4, tzinfo=UTC), NEW_YORK)
    with pytest.raises(ValueError, match="tz must be an IANA time zone name"):
        AnalysisWindow.day_bounds(DEFAULT_END, "Not/AZone")


# ---------------------------------------------------------------------------
# Evidence: boundary, boundaries_differ, describe, source_days_json, to_json
# ---------------------------------------------------------------------------


def test_describe_matches_the_documented_evidence_example(shipped: Thresholds) -> None:
    # 02:00 UTC on 2026-10-03: yesterday in New York is 2026-10-01.
    window = AnalysisWindow.default(
        shipped, NY_SOURCES, now=datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
    )

    assert window.describe() == (
        "Google 2026-09-04 to 2026-10-01 (America/New_York); LinkedIn same dates (UTC)"
    )


def test_describe_follows_the_source_order_and_single_sources(shipped: Thresholds) -> None:
    reordered = AnalysisWindow.default(shipped, SourceDayInfo(NEW_YORK, ("linkedin", "google")))
    assert reordered.describe() == (
        f"LinkedIn {reordered.start.isoformat()} to {reordered.end.isoformat()} (UTC); "
        "Google same dates (America/New_York)"
    )

    single = _custom(shipped, 1, sources=SourceDayInfo(None, ("google",)))
    assert single.describe() == "Google 2026-10-04 to 2026-10-04 (UTC)"


def test_boundary_per_source_and_unknown_source(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)

    assert window.boundary("google") == NEW_YORK
    assert window.boundary("linkedin") == UTC_BOUNDARY
    unknown: Any = "hubspot"
    with pytest.raises(KeyError):
        window.boundary(unknown)


def test_boundaries_differ_only_when_the_sources_boundaries_differ(shipped: Thresholds) -> None:
    assert AnalysisWindow.default(shipped, NY_SOURCES, now=NOW).boundaries_differ
    assert not AnalysisWindow.default(shipped, SourceDayInfo(None), now=NOW).boundaries_differ
    assert not AnalysisWindow.default(shipped, SourceDayInfo("UTC"), now=NOW).boundaries_differ


def test_to_json_is_exactly_the_evidence_window(shipped: Thresholds) -> None:
    window = AnalysisWindow.default(shipped, NY_SOURCES, now=NOW)
    source_days = [
        {"source": "google", "start": "2026-09-07", "end": "2026-10-04", "boundary": NEW_YORK},
        {"source": "linkedin", "start": "2026-09-07", "end": "2026-10-04", "boundary": "UTC"},
    ]

    assert window.source_days_json() == source_days
    payload = window.to_json()
    assert payload == {
        "start": "2026-09-07",
        "end": "2026-10-04",
        "days": 28,
        "source_days": source_days,
        "boundaries_differ": True,
        "description": (
            "Google 2026-09-07 to 2026-10-04 (America/New_York); LinkedIn same dates (UTC)"
        ),
    }
    assert list(payload) == [
        "start",
        "end",
        "days",
        "source_days",
        "boundaries_differ",
        "description",
    ]


@pytest.mark.parametrize("google_timezone", [None, UTC_BOUNDARY])
def test_to_json_states_when_the_boundaries_are_the_same(
    shipped: Thresholds, google_timezone: str | None
) -> None:
    # Google without a known account zone is treated as UTC, the same boundary as LinkedIn.
    window = AnalysisWindow.default(shipped, SourceDayInfo(google_timezone), now=NOW)

    payload = window.to_json()

    assert payload["boundaries_differ"] is False
    assert payload["description"] == (
        f"Google {window.start.isoformat()} to {window.end.isoformat()} (UTC); "
        "LinkedIn same dates (UTC)"
    )
    assert [day["boundary"] for day in payload["source_days"]] == [UTC_BOUNDARY, UTC_BOUNDARY]


def test_to_json_names_a_single_source_without_claiming_a_difference(
    shipped: Thresholds,
) -> None:
    window = _custom(shipped, 1, sources=SourceDayInfo(NEW_YORK, ("google",)))

    payload = window.to_json()

    assert payload["boundaries_differ"] is False
    assert payload["description"] == "Google 2026-10-04 to 2026-10-04 (America/New_York)"
