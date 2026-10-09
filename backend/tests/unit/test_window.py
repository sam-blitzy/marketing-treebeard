"""Unit tests for the analysis window and the window keys of the threshold loader.

They prove the window guarantees of AAP 0.5.4.1 ("Analysis window", "Day boundaries"), 0.5.3
(``POST /runs`` bounds, the ``Run`` and ``Evidence.window`` shapes), 0.3.2 (segment windows,
the C3a precondition) and 0.11.2 (P5 uses the configured trusted minimum; "Changed defaults"):

* the default window ends on the earlier of yesterday in the Google account's time zone and
  yesterday UTC, and today is never included;
* one calendar-date range is applied to each source's native dates, each source keeping its
  own boundary, and the evidence states when the boundaries differ;
* the short-window, sustained-minimum, custom-bound and segment-window decisions read the
  configured ``window.*`` values, never literals;
* ``DISPLAY_TIMEZONE`` never moves a day boundary;
* the loader rejects a default missing from ``window.presets`` and hashes the configuration.

Window values come from the root ``thresholds`` fixture (the shipped ``config/``) or from
``make_thresholds`` overrides. Every instant is passed explicitly as a tz-aware UTC datetime,
so no test depends on the wall clock. Pure: no database and no network.
"""

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Final, Protocol
from zoneinfo import ZoneInfo

import pytest

from ads_engine.analysis.thresholds import THRESHOLDS_FILE, ConfigError, Thresholds
from ads_engine.analysis.window import AnalysisWindow, InvalidWindow, SourceDayInfo, default_end
from ads_engine.settings import get_settings


class ThresholdsFactory(Protocol):
    """The call shape of the root ``make_thresholds`` fixture, for type hints only."""

    def __call__(self, overrides: Mapping[str, object] | None = None) -> Thresholds:
        """Write an overridden copy of ``config/`` and load it."""
        ...

    def write(self, overrides: Mapping[str, object] | None = None) -> Path:
        """Write an overridden copy of ``config/`` and return its directory, unloaded."""
        ...


NEW_YORK: Final = "America/New_York"
TOKYO: Final = "Asia/Tokyo"
ONE_DAY: Final = timedelta(days=1)

# 02:00 UTC on 2026-10-06 is 22:00 on 2026-10-05 in New York (EDT, UTC-4). Yesterday UTC is
# 2026-10-05, yesterday in New York is 2026-10-04: the Google account's day 2026-10-05 is not
# complete yet, so the default window ends on 2026-10-04.
NOW: Final = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
NY_SOURCES: Final = SourceDayInfo(NEW_YORK)
NY_DEFAULT_END: Final = date(2026, 10, 4)

# 08:00 UTC on 2026-10-06 is 17:00 on 2026-10-06 in Tokyo, so the default end is 2026-10-05.
# It is 21:00 on 2026-10-06 in Auckland (NZDT, UTC+13) and 22:00 on 2026-10-05 in Honolulu
# (UTC-10): a window that let either display zone move a day boundary would end elsewhere.
DISPLAY_NOW: Final = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
TOKYO_SOURCES: Final = SourceDayInfo(TOKYO)
TOKYO_DEFAULT_END: Final = date(2026, 10, 5)
DISPLAY_ZONES: Final = ("UTC", "Pacific/Auckland", "Pacific/Honolulu")

SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")


def _custom(
    config: Thresholds,
    days: int,
    *,
    end: date = NY_DEFAULT_END,
    sources: SourceDayInfo = NY_SOURCES,
    now: datetime = NOW,
) -> AnalysisWindow:
    """Build a custom window of ``days`` native days ending on ``end``, validated at ``now``."""
    return AnalysisWindow.custom(
        end - timedelta(days=days - 1), end, config=config, sources=sources, now=now
    )


@pytest.fixture
def ny_default(thresholds: Thresholds) -> AnalysisWindow:
    """The shipped default window at :data:`NOW` for a New York Google account."""
    return AnalysisWindow.default(thresholds, NY_SOURCES, now=NOW)


# ---------------------------------------------------------------------------------------------
# Phase 1: the default end on native day boundaries (AAP 0.5.4.1)
# ---------------------------------------------------------------------------------------------

DEFAULT_END_CASES: Final = [
    pytest.param(NEW_YORK, NOW, date(2026, 10, 4), id="new-york-evening-behind-utc"),
    pytest.param(
        NEW_YORK, datetime(2026, 10, 6, 12, 0, tzinfo=UTC), date(2026, 10, 5), id="new-york-noon"
    ),
    pytest.param(
        TOKYO, datetime(2026, 10, 6, 20, 0, tzinfo=UTC), date(2026, 10, 5), id="tokyo-ahead-of-utc"
    ),
]


@pytest.mark.parametrize(("google_timezone", "now", "expected"), DEFAULT_END_CASES)
def test_default_end_is_the_earlier_of_google_and_utc_yesterday(
    thresholds: Thresholds, google_timezone: str, now: datetime, expected: date
) -> None:
    """AAP 0.5.4.1: the end is the earlier of yesterday in the Google zone and yesterday UTC."""
    sources = SourceDayInfo(google_timezone)

    assert default_end(sources, now=now) == expected
    window = AnalysisWindow.default(thresholds, sources, now=now)
    assert window.end == expected
    assert window.default_end == expected


@pytest.mark.parametrize(("google_timezone", "now", "expected"), DEFAULT_END_CASES)
def test_default_end_never_includes_today(
    google_timezone: str, now: datetime, expected: date
) -> None:
    """AAP 0.5.4.1: today is a partial day in every source and is excluded from the window."""
    end = default_end(SourceDayInfo(google_timezone), now=now)

    assert end == expected
    assert end < now.astimezone(UTC).date()
    assert end < now.astimezone(ZoneInfo(google_timezone)).date()


@pytest.mark.parametrize("zone", ["Pacific/Auckland", NEW_YORK, "Pacific/Honolulu"])
def test_default_end_depends_on_the_instant_not_the_offset_now_is_written_in(zone: str) -> None:
    """AAP 0.5.4.1: one instant gives one default end, whatever offset expresses it."""
    assert default_end(NY_SOURCES, now=NOW.astimezone(ZoneInfo(zone))) == NY_DEFAULT_END


def test_default_end_excludes_the_current_utc_day_from_its_first_instant() -> None:
    """AAP 0.5.4.1: the UTC day that has just begun is partial; the one before it is complete."""
    midnight = datetime(2026, 10, 6, 0, 0, tzinfo=UTC)
    utc_days = SourceDayInfo("UTC")

    assert default_end(utc_days, now=midnight) == date(2026, 10, 5)
    assert default_end(utc_days, now=midnight - timedelta(microseconds=1)) == date(2026, 10, 4)


@pytest.mark.parametrize(
    "sources",
    [SourceDayInfo(None), SourceDayInfo(NEW_YORK, ("linkedin",))],
    ids=["google-zone-unknown", "linkedin-days-only"],
)
def test_default_end_without_google_days_is_yesterday_utc(sources: SourceDayInfo) -> None:
    """AAP 0.5.4.1: with no Google account zone in play, only UTC (LinkedIn) days bound the end."""
    assert default_end(sources, now=NOW) == date(2026, 10, 5)


# ---------------------------------------------------------------------------------------------
# Phase 2: the default window's shape (AAP 0.5.4.1, 0.5.3)
# ---------------------------------------------------------------------------------------------


def test_default_window_spans_default_days_ending_on_the_default_end(
    thresholds: Thresholds, ny_default: AnalysisWindow
) -> None:
    """AAP 0.5.4.1: the default is ``window.default_days`` complete days ending on the end."""
    days = thresholds.int("window.default_days")

    assert ny_default.days == days
    assert ny_default.default_days == days
    assert ny_default.end == NY_DEFAULT_END
    assert ny_default.start == ny_default.end - timedelta(days=days - 1)
    assert ny_default.is_default_shape

    dates = ny_default.dates()
    assert len(dates) == days
    assert dates[0] == ny_default.start
    assert dates[-1] == ny_default.end
    assert all(later - earlier == ONE_DAY for earlier, later in pairwise(dates))


def test_default_window_applies_one_date_range_to_each_native_boundary(
    ny_default: AnalysisWindow,
) -> None:
    """AAP 0.5.4.1: one calendar range per source, Google in its account zone, LinkedIn on UTC."""
    entries = ny_default.source_days

    assert [item.source for item in entries] == ["google", "linkedin"]
    assert {item.source: item.boundary for item in entries} == {
        "google": NEW_YORK,
        "linkedin": "UTC",
    }
    for item in entries:
        # No rebucketing: every source reads the same calendar dates on its own day boundary.
        assert (item.start, item.end) == (ny_default.start, ny_default.end)
    assert ny_default.boundary("google") == NEW_YORK
    assert ny_default.boundary("linkedin") == "UTC"


@pytest.mark.parametrize(
    ("google_timezone", "differ"),
    [(NEW_YORK, True), (TOKYO, True), ("UTC", False), (None, False)],
    ids=["new-york", "tokyo", "google-utc", "google-zone-unknown"],
)
def test_window_states_when_the_source_day_boundaries_differ(
    thresholds: Thresholds, google_timezone: str | None, differ: bool
) -> None:
    """AAP 0.5.4.1: when the boundaries differ the evidence says so; otherwise it does not."""
    sources = SourceDayInfo(google_timezone)
    window = AnalysisWindow.default(thresholds, sources, now=NOW)

    assert window.boundaries_differ is differ
    assert (len(set(sources.boundaries().values())) > 1) is differ
    google = google_timezone or "UTC"
    assert window.describe() == (
        f"Google {window.start.isoformat()} to {window.end.isoformat()} ({google}); "
        "LinkedIn same dates (UTC)"
    )


def test_utc_bounds_are_half_open_from_start_to_the_day_after_end(
    ny_default: AnalysisWindow,
) -> None:
    """AAP 0.5.4.1: leads count when submittedAt is in [start 00:00 UTC, end + 1 day 00:00 UTC)."""
    lower, upper = ny_default.utc_bounds()

    assert lower == datetime.combine(ny_default.start, time.min, tzinfo=UTC)
    assert upper == datetime.combine(ny_default.end + ONE_DAY, time.min, tzinfo=UTC)
    assert lower.utcoffset() == timedelta(0)
    assert upper.utcoffset() == timedelta(0)
    assert upper - lower == timedelta(days=ny_default.days)
    # The instant range never reaches into the current, partial UTC day.
    assert upper <= datetime.combine(NOW.date(), time.min, tzinfo=UTC)


def test_halves_split_a_28_day_window_into_two_14_day_halves(thresholds: Thresholds) -> None:
    """AAP 0.5.4.3 K3: the halves cover the window with no overlap and no gap."""
    window = _custom(thresholds, 28)

    (first_start, first_end), (second_start, second_end) = window.halves()

    assert first_start == window.start
    assert second_end == window.end
    assert second_start == first_end + ONE_DAY
    assert (first_end - first_start).days + 1 == 14
    assert (second_end - second_start).days + 1 == 14


def test_to_json_is_the_evidence_window_shape(ny_default: AnalysisWindow) -> None:
    """AAP 0.5.3 and 0.5.4.1: ``window`` is {start, end, days, source_days[{source, start, end,
    boundary}], boundaries_differ, description}: when the boundaries differ, it says so."""
    start = ny_default.start.isoformat()
    end = ny_default.end.isoformat()

    payload = ny_default.to_json()

    assert payload == {
        "start": start,
        "end": end,
        "days": ny_default.days,
        "source_days": [
            {"source": "google", "start": start, "end": end, "boundary": NEW_YORK},
            {"source": "linkedin", "start": start, "end": end, "boundary": "UTC"},
        ],
        "boundaries_differ": True,
        "description": f"Google {start} to {end} (America/New_York); LinkedIn same dates (UTC)",
    }
    assert json.loads(json.dumps(payload)) == payload
    assert ny_default.source_days_json() == payload["source_days"]


# ---------------------------------------------------------------------------------------------
# Phase 3: short window and sustained minimum (AAP 0.5.4.1, 0.5.4.2 P5, 0.11.2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("offset", "short"), [(-1, True), (0, False), (1, False)])
def test_short_window_uses_the_shipped_trusted_minimum(
    thresholds: Thresholds, offset: int, short: bool
) -> None:
    """AAP 0.5.4.1: a window shorter than ``window.trusted_min_days`` is a short window."""
    minimum = thresholds.int("window.trusted_min_days")
    window = _custom(thresholds, minimum + offset)

    assert window.trusted_min_days == minimum
    assert window.short_window is short


@pytest.mark.parametrize(("days", "short"), [(20, True), (21, False)])
def test_short_window_follows_a_configured_trusted_minimum_of_21(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory, days: int, short: bool
) -> None:
    """AAP 0.11.2 P5: with ``window.trusted_min_days: 21`` a 20-day window is flagged short."""
    config = make_thresholds({"window.trusted_min_days": 21})
    window = _custom(config, days)

    assert config.trusted_min_days == 21
    assert window.trusted_min_days == 21
    assert window.short_window is short
    # The same span under the shipped minimum is trusted: the flag is never a literal.
    assert _custom(thresholds, days).short_window is (days < thresholds.trusted_min_days)


@pytest.mark.parametrize(("offset", "ok"), [(-1, False), (0, True), (1, True)])
def test_sustained_ok_needs_the_configured_sustained_minimum(
    thresholds: Thresholds, offset: int, ok: bool
) -> None:
    """AAP 0.5.4.1: sustained-pattern rules need ``window.sustained_min_days`` days."""
    minimum = thresholds.int("window.sustained_min_days")
    window = _custom(thresholds, minimum + offset)

    assert window.sustained_min_days == minimum
    assert window.sustained_ok is ok


def test_sustained_ok_follows_a_configured_sustained_minimum(
    make_thresholds: ThresholdsFactory,
) -> None:
    """AAP 0.5.4.1: a changed ``window.sustained_min_days`` moves the sustained threshold."""
    config = make_thresholds({"window.sustained_min_days": 10})

    assert not _custom(config, 9).sustained_ok
    assert _custom(config, 10).sustained_ok
    assert _custom(config, 10).sustained_min_days == 10


# ---------------------------------------------------------------------------------------------
# Phase 4: custom window validation (AAP 0.5.3 POST /runs)
# ---------------------------------------------------------------------------------------------


def test_custom_accepts_a_one_day_window_ending_on_the_default_end(thresholds: Thresholds) -> None:
    """AAP 0.5.3: a custom window spans from 1 day and may end on the default end."""
    window = _custom(thresholds, 1)

    assert window.start == window.end == NY_DEFAULT_END
    assert window.days == 1
    assert window.dates() == [NY_DEFAULT_END]


def test_custom_accepts_exactly_max_days_ending_on_the_default_end(thresholds: Thresholds) -> None:
    """AAP 0.5.3: a custom window spans up to ``window.max_days`` days."""
    max_days = thresholds.int("window.max_days")
    window = _custom(thresholds, max_days)

    assert window.days == max_days
    assert window.end == window.default_end == NY_DEFAULT_END
    assert window.max_days == max_days


@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        # Yesterday UTC, but the New York account's 2026-10-05 is still in progress.
        (NY_DEFAULT_END - timedelta(days=5), NY_DEFAULT_END + ONE_DAY, "end_after_default_end"),
        # Today is never a complete day.
        (NOW.date() - timedelta(days=6), NOW.date(), "end_after_default_end"),
        # The start after the end leaves a span below 1 day.
        (NY_DEFAULT_END, NY_DEFAULT_END - ONE_DAY, "end_before_start"),
    ],
    ids=["end-after-default-end", "end-today", "start-after-end"],
)
def test_custom_rejects_a_window_outside_the_post_runs_bounds(
    thresholds: Thresholds, ny_default: AnalysisWindow, start: date, end: date, code: str
) -> None:
    """AAP 0.5.3: the end is no later than the default end and the span is at least 1 day."""
    with pytest.raises(InvalidWindow) as caught:
        AnalysisWindow.custom(start, end, config=thresholds, sources=NY_SOURCES, now=NOW)

    assert isinstance(caught.value, ValueError)
    assert caught.value.code == code
    assert caught.value.message
    # The two-argument form on a configured window applies the same bounds and code.
    with pytest.raises(InvalidWindow) as bound:
        ny_default.custom(start, end)
    assert bound.value.code == code


def test_custom_rejects_a_span_of_max_days_plus_one(thresholds: Thresholds) -> None:
    """AAP 0.5.3: a span above ``window.max_days`` (LinkedIn daily retention) is rejected."""
    max_days = thresholds.int("window.max_days")

    with pytest.raises(InvalidWindow) as caught:
        _custom(thresholds, max_days + 1)

    assert caught.value.code == "too_long"
    assert str(max_days) in caught.value.message


def test_custom_window_equal_to_the_default_still_validates(
    thresholds: Thresholds, ny_default: AnalysisWindow
) -> None:
    """AAP 0.5.3 "Run kinds": a custom window equal to the default is valid; the caller decides
    whether the run is ``custom``, so the window itself is the same."""
    custom = AnalysisWindow.custom(
        ny_default.start, ny_default.end, config=thresholds, sources=NY_SOURCES, now=NOW
    )

    assert custom == ny_default
    assert custom.is_default_shape
    assert ny_default.custom(ny_default.start, ny_default.end) == ny_default


def test_a_shorter_custom_window_on_the_default_end_is_not_the_default_shape(
    thresholds: Thresholds,
) -> None:
    """AAP 0.5.3: only the default length ending on the default end has the default shape."""
    default_days = thresholds.int("window.default_days")

    assert not _custom(thresholds, default_days - 1).is_default_shape
    assert not _custom(thresholds, default_days, end=NY_DEFAULT_END - ONE_DAY).is_default_shape


# ---------------------------------------------------------------------------------------------
# Phase 5: segment windows, the C3a precondition (AAP 0.3.2)
# ---------------------------------------------------------------------------------------------


def test_every_preset_ending_on_the_default_end_is_a_segment_window(
    thresholds: Thresholds, ny_default: AnalysisWindow
) -> None:
    """AAP 0.3.2: segments are fetched for each ``window.presets`` length ending on the default
    end, so exactly those windows carry C3a segment evidence."""
    assert ny_default.is_segment_window()
    for preset in thresholds.presets:
        window = _custom(thresholds, preset)
        assert window.days == preset
        assert window.is_segment_window(), preset


def test_a_window_whose_length_is_not_a_preset_is_not_a_segment_window(
    thresholds: Thresholds,
) -> None:
    """AAP 0.3.2: a 30-day custom window has no fetched segment window."""
    assert 30 not in thresholds.presets
    assert not _custom(thresholds, 30).is_segment_window()


def test_a_preset_ending_before_the_default_end_is_not_a_segment_window(
    thresholds: Thresholds,
) -> None:
    """AAP 0.3.2: segments are never reused for another end date, even at a preset length."""
    for preset in thresholds.presets:
        window = _custom(thresholds, preset, end=NY_DEFAULT_END - ONE_DAY)
        assert window.days == preset
        assert not window.is_segment_window(), preset


# ---------------------------------------------------------------------------------------------
# Phase 6: DISPLAY_TIMEZONE never moves a day boundary (AAP 0.5.4.1)
# ---------------------------------------------------------------------------------------------


def test_display_timezone_never_moves_a_day_boundary(
    thresholds: Thresholds, set_env: Callable[..., None]
) -> None:
    """AAP 0.5.4.1: DISPLAY_TIMEZONE only formats instants; the window and its boundaries are
    the same under every display zone, Google keeping its account zone and LinkedIn UTC."""
    windows: dict[str, AnalysisWindow] = {}
    for zone in DISPLAY_ZONES:
        # set_env clears the get_settings cache, so the next read sees this zone.
        set_env(DISPLAY_TIMEZONE=zone)
        assert get_settings().display_timezone == zone
        windows[zone] = AnalysisWindow.default(thresholds, TOKYO_SOURCES, now=DISPLAY_NOW)

    reference = windows["UTC"]
    assert reference.end == TOKYO_DEFAULT_END
    assert {item.source: item.boundary for item in reference.source_days} == {
        "google": TOKYO,
        "linkedin": "UTC",
    }
    for zone, window in windows.items():
        assert (window.start, window.end, window.days) == (
            reference.start,
            reference.end,
            reference.days,
        ), zone
        assert window.source_days == reference.source_days, zone
        assert window.to_json() == reference.to_json(), zone
        assert window.utc_bounds() == reference.utc_bounds(), zone


# ---------------------------------------------------------------------------------------------
# Phase 7: window keys in the threshold loader (AAP 0.5.4.1, 0.11.2 "Changed defaults")
# ---------------------------------------------------------------------------------------------


def test_shipped_window_settings_are_the_plan_defaults(thresholds: Thresholds) -> None:
    """AAP 0.5.4.1: default 28 days, trusted minimum 14, presets 14/28/56/90, sustained minimum
    7 and maximum 180, with the default among the presets."""
    assert thresholds.presets == (14, 28, 56, 90)
    assert thresholds.default_days in thresholds.presets
    assert thresholds.default_days == thresholds.int("window.default_days") == 28
    assert thresholds.trusted_min_days == thresholds.int("window.trusted_min_days") == 14
    assert thresholds.sustained_min_days == thresholds.int("window.sustained_min_days") == 7
    assert thresholds.max_days == thresholds.int("window.max_days") == 180


def test_a_changed_default_of_21_days_makes_the_default_window_21_days(
    make_thresholds: ThresholdsFactory,
) -> None:
    """AAP 0.11.2 "Changed defaults": ``window.default_days: 21`` makes the default run 21 days,
    and that preset is the one marked default."""
    config = make_thresholds({"window.default_days": 21, "window.presets": [14, 21, 28]})

    window = AnalysisWindow.default(config, NY_SOURCES, now=NOW)

    assert config.default_days == 21
    assert config.presets == (14, 21, 28)
    assert window.days == 21
    assert window.end == NY_DEFAULT_END
    assert window.start == NY_DEFAULT_END - timedelta(days=20)
    assert window.default_days == 21
    assert window.presets == (14, 21, 28)
    assert window.is_default_shape
    assert window.is_segment_window()


def test_a_default_missing_from_the_presets_fails_config_load(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory
) -> None:
    """AAP 0.5.4.1: ``window.presets`` must contain ``window.default_days``, or config load
    fails, through the files and through in-memory overrides alike."""
    assert 21 not in thresholds.presets

    with pytest.raises(ConfigError) as from_files:
        make_thresholds({"window.default_days": 21})
    with pytest.raises(ConfigError) as from_overrides:
        Thresholds.load(thresholds.source_dir, overrides={"window.default_days": 21})

    for caught in (from_files, from_overrides):
        assert caught.value.file == THRESHOLDS_FILE
        assert caught.value.key == "window.presets"
        assert "window.default_days" in caught.value.detail


def test_config_hash_is_a_lowercase_sha256_hex_digest(thresholds: Thresholds) -> None:
    """AAP 0.5.2 ``analysis_run.config_hash``: a SHA-256 hex digest of the configuration."""
    assert SHA256_HEX.fullmatch(thresholds.config_hash)


def test_config_hash_is_equal_for_two_loads_of_the_same_config(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory
) -> None:
    """AAP 0.3.2: an unchanged configuration hashes the same, so no rerun is triggered."""
    reloaded = Thresholds.load(thresholds.source_dir)
    copied = make_thresholds()

    assert reloaded.config_hash == thresholds.config_hash
    assert copied.config_hash == thresholds.config_hash
    assert copied == thresholds


def test_config_hash_ignores_a_comment_only_edit(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory
) -> None:
    """AAP 0.3.2: the hash covers the parsed values, so editing a comment changes nothing."""
    directory = make_thresholds.write()
    path = directory / THRESHOLDS_FILE
    edited = "# A comment-only edit.\n" + path.read_text(encoding="utf-8")
    path.write_text(edited, encoding="utf-8")

    assert Thresholds.load(directory).config_hash == thresholds.config_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"window.trusted_min_days": 21},
        {"window.default_days": 21, "window.presets": [14, 21, 28]},
        {"window.sustained_min_days": 10},
        {"window.max_days": 120},
        {"k1.rule_of_three": 4},
    ],
    ids=["trusted-min", "default-and-presets", "sustained-min", "max-days", "non-window-key"],
)
def test_config_hash_changes_after_any_override(
    thresholds: Thresholds, make_thresholds: ThresholdsFactory, overrides: Mapping[str, object]
) -> None:
    """AAP 0.3.2: any value change alters ``config_hash`` and so re-triggers analysis."""
    changed = make_thresholds(overrides)

    assert SHA256_HEX.fullmatch(changed.config_hash)
    assert changed.config_hash != thresholds.config_hash
    assert changed != thresholds
