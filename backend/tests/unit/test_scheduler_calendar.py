"""The scheduler's calendar (``sync/scheduler.py``): entries, latest slots, due-ness and DST.

AAP 0.3.2 ("Continuous analysis: sync schedule"): scheduled times are wall-clock times in
``DISPLAY_TIMEZONE`` and the worker wakes every 30 s; entity sync, LinkedIn lead responses and
HubSpot run every 3 h, incremental metrics every 6 h, the restatement nightly at 03:00 and
LinkedIn's demographic segments nightly at 04:00. AAP 0.3.3 ("Retirement and purge"): retired
datasets are purged by the nightly cleanup, whose slot the scheduler places at 05:00. Proved
here, on fixed instants only:

* **Entries**: ``ScheduleEntry`` rejects a blank job, no or unknown sources, both or neither of
  ``every_hours`` and ``at``, an ``every_hours`` that is a bool, not an int, outside 1 to 24 or
  not a divisor of 24, and an ``at`` carrying ``tzinfo``; ``wall_times`` lists exactly the
  local slot times of one day.
* **Schedule**: ``SCHEDULE`` is exactly the AAP's cadence per job and source, in order.
* **Latest slot**: for every entry of ``SCHEDULE``, in UTC and in America/New_York, an instant
  exactly on, one second before and one second after a slot, local midnight, 23:59:59, and day,
  month and year rollovers resolve to the expected slot, always a UTC instant; the
  documented example (2026-10-01 13:15 UTC, New York, 3 h -> 13:00 UTC) holds and runs.
* **DST** (America/New_York 2026, transitions read from zoneinfo in the test): the 00:00 and
  03:00 three-hourly slots are two real hours apart on the spring-forward night and four on the
  fall-back night; a nonexistent 02:30 maps forward to 03:30 EDT; an ambiguous 01:30 is its
  first occurrence, and its second occurrence neither is a slot nor makes the job due again.
  Where local midnight is skipped or repeated (America/Havana 2026) and where the clock falls
  back across midnight (Antarctica/Casey 2010, the case the scan of the day ahead exists for),
  the latest slot is never later than now and never misses a slot, the midnight one included.
* **Due-ness**: never run -> due; a run at or after the latest slot (in any zone) -> not due;
  1 µs before it -> due; a 30 h outage makes the job due once, and its run satisfies it until
  the next slot; a daily job last run the day before is due only from its slot on; a
  non-``datetime`` is a ``TypeError`` and a naive instant a ``ValueError``.
* **Recurrence**: a worker waking every 30 s across a whole local day (UTC; New York on an
  ordinary, the spring-forward and the fall-back day) and running whenever ``is_due`` runs each
  ``SCHEDULE`` entry exactly at its slot instants, computed independently from the wall-clock
  times with zoneinfo (``fold=0``): 8 three-hourly, 4 six-hourly and one daily run; an hourly
  entry runs 23 times on the spring-forward day and 24 times on the fall-back day.

Pure: no database, settings or clock is read; expected instants are derived in this module,
never by the scheduler's own helpers.
"""

import doctest
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest

from ads_engine.settings import SOURCES
from ads_engine.sync import scheduler as scheduler_module
from ads_engine.sync.scheduler import JOB_PURGE, SCHEDULE, ScheduleEntry, is_due, latest_slot

UTC_ZONE: Final = ZoneInfo("UTC")
"""``DISPLAY_TIMEZONE``'s default, as ``Settings.display_tz`` loads it."""

NEW_YORK: Final = ZoneInfo("America/New_York")
HAVANA: Final = ZoneInfo("America/Havana")
CASEY: Final = ZoneInfo("Antarctica/Casey")

WAKE: Final = timedelta(seconds=30)
"""The worker's wake interval (AAP 0.3.2), the step of every simulated day."""

ONE_SECOND: Final = timedelta(seconds=1)
ONE_MICROSECOND: Final = timedelta(microseconds=1)

THREE_HOURLY: Final = ("entities", "leads", "crm")
SIX_HOURLY: Final = ("metrics",)
DAILY_AT: Final = {"restatement": 3, "segments": 4, "purge": 5}
"""The daily jobs and their local hour (AAP 0.3.2: restatement 03:00, segments 04:00; purge
05:00, the scheduler's documented nightly-cleanup slot)."""

type Parts = tuple[int, ...]
"""Local wall-clock fields: year, month, day[, hour, minute[, second[, µs]]]."""


# ---------------------------------------------------------------------------------------------
# Helpers: independent wall-clock arithmetic
# ---------------------------------------------------------------------------------------------


def _entry(job: str) -> ScheduleEntry:
    """The ``SCHEDULE`` entry of ``job``; it must exist exactly once."""
    (found,) = [entry for entry in SCHEDULE if entry.job == job]
    return found


def _aware(tz: tzinfo, parts: Parts, fold: int = 0) -> datetime:
    """``parts`` (year, month, day[, hour, minute, second, µs]) on the wall clock of ``tz``."""
    year, month, day, hour, minute, second, micro = (*parts, 0, 0, 0, 0)[:7]
    return datetime(year, month, day, hour, minute, second, micro, tzinfo=tz, fold=fold)


def _utc(*parts: int) -> datetime:
    """The UTC instant of ``parts`` (year, month, day[, hour, minute, second, µs])."""
    return _aware(UTC, parts)


def _local(tz: tzinfo, parts: Parts, *, fold: int = 0) -> datetime:
    """The UTC instant of the local wall-clock ``parts`` in ``tz``, with the given ``fold``."""
    return _aware(tz, parts, fold).astimezone(UTC)


def _walls(every_hours: int | None = None, at: int | None = None) -> tuple[time, ...]:
    """The local slot times of one day: every ``every_hours`` from 00:00, or once at ``at``."""
    if at is not None:
        return (time(at),)
    assert every_hours is not None
    return tuple(time(hour) for hour in range(0, 24, every_hours))


def _expected_walls(entry: ScheduleEntry) -> tuple[time, ...]:
    """The slot times this module expects for ``entry``, from its fields alone."""
    if entry.at is not None:
        return (entry.at,)
    return _walls(every_hours=entry.every_hours)


def _slot_instants(walls: Iterable[time], tz: tzinfo, first: date, last: date) -> list[datetime]:
    """Every ``fold=0`` slot instant of the local days ``first`` to ``last`` in ``tz``.

    UTC, ascending, de-duplicated: a nonexistent wall time maps forward onto the instant of
    a later one (02:00 on a spring-forward night is 03:00), which then is a single slot.
    """
    slots: set[datetime] = set()
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        for wall in walls:
            local = datetime(
                day.year, day.month, day.day, wall.hour, wall.minute, tzinfo=tz, fold=0
            )
            slots.add(local.astimezone(UTC))
    return sorted(slots)


def _day_bounds(tz: tzinfo, day: date) -> tuple[datetime, datetime]:
    """The UTC instants of local midnight (``fold=0``) of ``day`` and of the day after."""
    following = day + timedelta(days=1)
    return (
        _local(tz, (day.year, day.month, day.day)),
        _local(tz, (following.year, following.month, following.day)),
    )


def _simulate(entry: ScheduleEntry, tz: tzinfo, start: datetime, end: datetime) -> list[datetime]:
    """Wake every :data:`WAKE` in ``[start, end)``; run whenever due; return the run instants.

    The wake before ``start`` ran everything due then, so it is the first ``last_started``.
    """
    runs: list[datetime] = []
    last = start - WAKE
    now = start
    while now < end:
        if is_due(entry, now, tz, last):
            runs.append(now)
            last = now
        now += WAKE
    return runs


def _scan_latest(
    entry: ScheduleEntry, tz: tzinfo, start: datetime, end: datetime, step: timedelta
) -> list[str]:
    """Check ``latest_slot`` at every ``step`` in ``[start, end)`` against an independent slot set.

    Returns one line per instant where the latest slot is later than now, is not a slot, or
    is not the latest slot at or before now (a missed slot), so the caller sees every
    violation at once.
    """
    first = start.astimezone(tz).date() - timedelta(days=3)
    last = end.astimezone(tz).date() + timedelta(days=3)
    slots = _slot_instants(_expected_walls(entry), tz, first, last)
    problems: list[str] = []
    now = start
    while now < end:
        expected = slots[bisect_right(slots, now) - 1]
        found = latest_slot(entry, now, tz)
        if found > now or found != expected:
            problems.append(
                f"{now.isoformat()} ({now.astimezone(tz).isoformat()}): latest_slot "
                f"{found.isoformat()}, expected {expected.isoformat()}"
            )
        now += step
    return problems


def _transitions(tz: tzinfo, start: datetime, end: datetime) -> list[tuple[datetime, str, str]]:
    """The UTC offset changes of ``tz`` in ``[start, end)``, to the minute, read from zoneinfo.

    Each is ``(instant, local clock one second before, local clock at the instant)``.
    """
    found: list[tuple[datetime, str, str]] = []
    previous = start.astimezone(tz).utcoffset()
    now = start
    while now < end:
        offset = now.astimezone(tz).utcoffset()
        if offset != previous:
            found.append(
                (now, (now - ONE_SECOND).astimezone(tz).isoformat(), now.astimezone(tz).isoformat())
            )
            previous = offset
        now += timedelta(minutes=1)
    return found


# ---------------------------------------------------------------------------------------------
# ScheduleEntry
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param({"job": "", "every_hours": 3}, "needs a job name", id="empty-job"),
        pytest.param({"job": "   ", "every_hours": 3}, "needs a job name", id="blank-job"),
        pytest.param({"sources": (), "every_hours": 3}, "names no source", id="no-source"),
        pytest.param(
            {"sources": ("google", "bing"), "every_hours": 3},
            r"names unknown sources \['bing'\]",
            id="unknown-source",
        ),
        pytest.param(
            {"every_hours": 3, "at": time(3)}, "exactly one of every_hours, at", id="both"
        ),
        pytest.param({}, "exactly one of every_hours, at", id="neither"),
        pytest.param({"every_hours": True}, "must be 1 to 24, found True", id="every-true"),
        pytest.param({"every_hours": False}, "must be 1 to 24, found False", id="every-false"),
        pytest.param({"every_hours": 0}, "must be 1 to 24, found 0", id="every-0"),
        pytest.param({"every_hours": -3}, "must be 1 to 24, found -3", id="every-negative"),
        pytest.param({"every_hours": 25}, "must be 1 to 24, found 25", id="every-25"),
        pytest.param({"every_hours": 48}, "must be 1 to 24, found 48", id="every-48"),
        pytest.param({"every_hours": 3.0}, "must be 1 to 24, found 3.0", id="every-float"),
        pytest.param({"every_hours": "3"}, "must be 1 to 24, found '3'", id="every-str"),
        pytest.param({"every_hours": 5}, "must divide 24, found 5", id="every-5"),
        pytest.param({"every_hours": 7}, "must divide 24, found 7", id="every-7"),
        pytest.param({"every_hours": 9}, "must divide 24, found 9", id="every-9"),
        pytest.param(
            {"at": time(3, 0, tzinfo=UTC)}, "wall-clock time and takes no tzinfo", id="at-utc"
        ),
        pytest.param(
            {"at": time(3, 0, tzinfo=NEW_YORK)},
            "wall-clock time and takes no tzinfo",
            id="at-zone",
        ),
    ],
)
def test_schedule_entry_rejects_invalid_fields(fields: dict[str, Any], message: str) -> None:
    """Each malformed job name, source set or timing is a ``ValueError`` at construction."""
    arguments: dict[str, Any] = {"job": "entities", "sources": ("google",), **fields}
    with pytest.raises(ValueError, match=message):
        ScheduleEntry(**arguments)


@pytest.mark.parametrize("every_hours", [1, 2, 3, 4, 6, 8, 12, 24])
def test_schedule_entry_accepts_every_divisor_of_24(every_hours: int) -> None:
    """Every divisor of 24 is a valid cadence, and its slots start at local midnight."""
    entry = ScheduleEntry("entities", ("google",), every_hours=every_hours)

    assert entry.wall_times == tuple(time(hour) for hour in range(0, 24, every_hours))
    assert entry.wall_times[0] == time(0)
    assert len(entry.wall_times) == 24 // every_hours


def test_wall_times_list_the_local_slots_of_one_day() -> None:
    """3 h -> 00:00 to 21:00 (8); 6 h -> 4; 24 h -> 00:00 only; ``at`` -> that time only."""
    three = ScheduleEntry("entities", ("google",), every_hours=3)
    six = ScheduleEntry("metrics", ("google",), every_hours=6)
    daily = ScheduleEntry("metrics", ("google",), every_hours=24)
    at = ScheduleEntry("restatement", ("google",), at=time(3, 0))

    assert three.wall_times == (
        time(0),
        time(3),
        time(6),
        time(9),
        time(12),
        time(15),
        time(18),
        time(21),
    )
    assert len(three.wall_times) == 8
    assert six.wall_times == (time(0), time(6), time(12), time(18))
    assert daily.wall_times == (time(0),)
    assert at.wall_times == (time(3, 0),)


def test_schedule_entry_keeps_its_sources_in_run_order() -> None:
    """The sources are kept exactly as given, so the run order is the entry's order."""
    entry = ScheduleEntry("purge", ("hubspot", "google"), at=time(5))

    assert entry.sources == ("hubspot", "google")
    assert entry.every_hours is None


# ---------------------------------------------------------------------------------------------
# SCHEDULE
# ---------------------------------------------------------------------------------------------


def test_schedule_is_the_planned_cadence_per_job_and_source() -> None:
    """``SCHEDULE`` is exactly the AAP 0.3.2 cadence, plus the 05:00 purge over every source."""
    planned = (
        ScheduleEntry("entities", ("google", "linkedin"), every_hours=3),
        ScheduleEntry("metrics", ("google", "linkedin"), every_hours=6),
        ScheduleEntry("restatement", ("google", "linkedin"), at=time(3, 0)),
        ScheduleEntry("leads", ("linkedin",), every_hours=3),
        ScheduleEntry("crm", ("hubspot",), every_hours=3),
        ScheduleEntry("segments", ("linkedin",), at=time(4, 0)),
        ScheduleEntry("purge", ("google", "linkedin", "hubspot"), at=time(5, 0)),
    )
    assert SOURCES == ("google", "linkedin", "hubspot")
    assert JOB_PURGE == "purge"
    assert planned == SCHEDULE
    for entry in SCHEDULE:
        assert (entry.every_hours is None) != (entry.at is None), entry
    assert _entry("purge").sources == SOURCES


# ---------------------------------------------------------------------------------------------
# latest_slot on ordinary days
# ---------------------------------------------------------------------------------------------

_THREE_HOURLY_CASES: Final[tuple[tuple[str, Parts, Parts], ...]] = (
    ("on the 03:00 slot", (2026, 10, 1, 3, 0, 0), (2026, 10, 1, 3, 0)),
    ("1 s before 03:00", (2026, 10, 1, 2, 59, 59), (2026, 10, 1, 0, 0)),
    ("1 s after 03:00", (2026, 10, 1, 3, 0, 1), (2026, 10, 1, 3, 0)),
    ("on the 12:00 slot", (2026, 10, 1, 12, 0, 0), (2026, 10, 1, 12, 0)),
    ("1 s before 12:00", (2026, 10, 1, 11, 59, 59), (2026, 10, 1, 9, 0)),
    ("local midnight", (2026, 10, 1, 0, 0, 0), (2026, 10, 1, 0, 0)),
    ("23:59:59", (2026, 10, 1, 23, 59, 59), (2026, 10, 1, 21, 0)),
    ("month rollover, 1 s before", (2026, 9, 30, 23, 59, 59), (2026, 9, 30, 21, 0)),
    ("Jan 1 00:00:30", (2027, 1, 1, 0, 0, 30), (2027, 1, 1, 0, 0)),
    ("Jan 1 02:59:59", (2027, 1, 1, 2, 59, 59), (2027, 1, 1, 0, 0)),
    ("Dec 31 23:00", (2026, 12, 31, 23, 0, 0), (2026, 12, 31, 21, 0)),
    ("Dec 31 23:59:59", (2026, 12, 31, 23, 59, 59), (2026, 12, 31, 21, 0)),
)
"""(case, local now, local slot it resolves to) for a 3-hourly entry."""

_SIX_HOURLY_CASES: Final[tuple[tuple[str, Parts, Parts], ...]] = (
    ("on the 06:00 slot", (2026, 10, 1, 6, 0, 0), (2026, 10, 1, 6, 0)),
    ("1 s before 06:00", (2026, 10, 1, 5, 59, 59), (2026, 10, 1, 0, 0)),
    ("1 s after 06:00", (2026, 10, 1, 6, 0, 1), (2026, 10, 1, 6, 0)),
    ("on the 18:00 slot", (2026, 10, 1, 18, 0, 0), (2026, 10, 1, 18, 0)),
    ("local midnight", (2026, 10, 1, 0, 0, 0), (2026, 10, 1, 0, 0)),
    ("23:59:59", (2026, 10, 1, 23, 59, 59), (2026, 10, 1, 18, 0)),
    ("month rollover, 1 s before", (2026, 9, 30, 23, 59, 59), (2026, 9, 30, 18, 0)),
    ("Jan 1 00:00:30", (2027, 1, 1, 0, 0, 30), (2027, 1, 1, 0, 0)),
    ("Dec 31 23:00", (2026, 12, 31, 23, 0, 0), (2026, 12, 31, 18, 0)),
)
"""(case, local now, local slot it resolves to) for a 6-hourly entry."""


def _daily_cases(hour: int) -> tuple[tuple[str, Parts, Parts], ...]:
    """(case, local now, local slot) for a daily entry at ``hour``:00, rollovers included."""
    return (
        (f"1 s before {hour:02}:00", (2026, 10, 1, hour - 1, 59, 59), (2026, 9, 30, hour, 0)),
        (f"on the {hour:02}:00 slot", (2026, 10, 1, hour, 0, 0), (2026, 10, 1, hour, 0)),
        (f"1 s after {hour:02}:00", (2026, 10, 1, hour, 0, 1), (2026, 10, 1, hour, 0)),
        ("local midnight", (2026, 10, 1, 0, 0, 0), (2026, 9, 30, hour, 0)),
        ("23:59:59", (2026, 10, 1, 23, 59, 59), (2026, 10, 1, hour, 0)),
        ("Jan 1 00:00:30", (2027, 1, 1, 0, 0, 30), (2026, 12, 31, hour, 0)),
        ("Jan 1, 1 s before", (2027, 1, 1, hour - 1, 59, 59), (2026, 12, 31, hour, 0)),
        ("Jan 1 on the slot", (2027, 1, 1, hour, 0, 0), (2027, 1, 1, hour, 0)),
        ("Dec 31 23:00", (2026, 12, 31, 23, 0, 0), (2026, 12, 31, hour, 0)),
    )


def _cases_of(job: str) -> tuple[tuple[str, Parts, Parts], ...]:
    """The ordinary-day cases of ``job``, by its planned cadence (not by its entry)."""
    if job in THREE_HOURLY:
        return _THREE_HOURLY_CASES
    if job in SIX_HOURLY:
        return _SIX_HOURLY_CASES
    return _daily_cases(DAILY_AT[job])


@pytest.mark.parametrize("zone", [UTC_ZONE, NEW_YORK], ids=str)
@pytest.mark.parametrize("job", [*THREE_HOURLY, *SIX_HOURLY, *DAILY_AT])
def test_latest_slot_on_ordinary_days(job: str, zone: ZoneInfo) -> None:
    """Exact, 1 s before and after, midnight, 23:59:59 and rollovers, for each real entry.

    The local instants are read in ``zone`` (``DISPLAY_TIMEZONE``), and every result is a UTC
    instant equal to the expected local slot.
    """
    entry = _entry(job)
    problems: list[str] = []
    for case, now_parts, slot_parts in _cases_of(job):
        now = _local(zone, now_parts)
        expected = _local(zone, slot_parts)
        found = latest_slot(entry, now, zone)
        if found != expected or found.tzinfo is not UTC:
            problems.append(f"{case}: {found!r}, expected {expected.isoformat()}")
    assert problems == []


def test_latest_slot_reads_the_wall_clock_of_the_given_zone() -> None:
    """The documented example: 13:15 UTC is 09:15 EDT, so the 3-hourly slot is 13:00 UTC.

    In UTC itself the same instant's slot is 12:00 UTC, so the zone, not UTC, places slots.
    """
    entry = ScheduleEntry("entities", ("google",), every_hours=3)
    now = _utc(2026, 10, 1, 13, 15)

    in_new_york = latest_slot(entry, now, NEW_YORK)
    in_utc = latest_slot(entry, now, UTC_ZONE)

    assert in_new_york == _utc(2026, 10, 1, 13, 0)
    assert in_new_york.tzinfo is UTC
    assert in_new_york.isoformat() == "2026-10-01T13:00:00+00:00"
    assert in_utc == _utc(2026, 10, 1, 12, 0)
    assert latest_slot(entry, now.astimezone(NEW_YORK), NEW_YORK) == in_new_york


def test_latest_slot_docstring_example_runs() -> None:
    """The example in ``latest_slot``'s docstring executes and prints what it claims."""
    finder = doctest.DocTestFinder()
    runner = doctest.DocTestRunner(verbose=False)
    examples = finder.find(latest_slot, "latest_slot", globs=dict(vars(scheduler_module)))
    assert sum(len(example.examples) for example in examples) == 4
    outcomes = [runner.run(example) for example in examples]
    assert [outcome.failed for outcome in outcomes] == [0] * len(examples)
    assert sum(outcome.attempted for outcome in outcomes) == 4


# ---------------------------------------------------------------------------------------------
# DST: America/New_York 2026
# ---------------------------------------------------------------------------------------------


def test_new_york_2026_transitions_are_as_assumed() -> None:
    """zoneinfo's 2026 New York transitions: 03-08 07:00Z springs forward, 11-01 06:00Z back."""
    assert _transitions(NEW_YORK, _utc(2026, 3, 7), _utc(2026, 3, 10)) == [
        (_utc(2026, 3, 8, 7), "2026-03-08T01:59:59-05:00", "2026-03-08T03:00:00-04:00")
    ]
    assert _transitions(NEW_YORK, _utc(2026, 10, 31), _utc(2026, 11, 3)) == [
        (_utc(2026, 11, 1, 6), "2026-11-01T01:59:59-04:00", "2026-11-01T01:00:00-05:00")
    ]


def test_spring_forward_puts_the_00_and_03_slots_two_hours_apart() -> None:
    """00:00 EST is 05:00Z and 03:00 EDT is 07:00Z: two real hours between the slots."""
    entry = _entry("entities")

    assert latest_slot(entry, _utc(2026, 3, 8, 5), NEW_YORK) == _utc(2026, 3, 8, 5)
    assert latest_slot(entry, _utc(2026, 3, 8, 6, 59, 59), NEW_YORK) == _utc(2026, 3, 8, 5)
    assert latest_slot(entry, _utc(2026, 3, 8, 7), NEW_YORK) == _utc(2026, 3, 8, 7)
    assert latest_slot(entry, _utc(2026, 3, 8, 9, 59, 59), NEW_YORK) == _utc(2026, 3, 8, 7)
    assert latest_slot(entry, _utc(2026, 3, 8, 10), NEW_YORK) == _utc(2026, 3, 8, 10)
    assert _utc(2026, 3, 8, 7) - _utc(2026, 3, 8, 5) == timedelta(hours=2)


def test_fall_back_puts_the_00_and_03_slots_four_hours_apart() -> None:
    """00:00 EDT is 04:00Z and 03:00 EST is 08:00Z: four real hours between the slots."""
    entry = _entry("entities")

    assert latest_slot(entry, _utc(2026, 11, 1, 4), NEW_YORK) == _utc(2026, 11, 1, 4)
    assert latest_slot(entry, _utc(2026, 11, 1, 7, 59, 59), NEW_YORK) == _utc(2026, 11, 1, 4)
    assert latest_slot(entry, _utc(2026, 11, 1, 8), NEW_YORK) == _utc(2026, 11, 1, 8)
    assert _utc(2026, 11, 1, 8) - _utc(2026, 11, 1, 4) == timedelta(hours=4)


def test_nonexistent_spring_forward_time_maps_forward() -> None:
    """02:30 does not exist on 2026-03-08: its slot is 03:30 EDT (07:30Z), never 01:30 EST."""
    entry = ScheduleEntry("restatement", ("google",), at=time(2, 30))
    previous = _utc(2026, 3, 7, 7, 30)  # 02:30 EST the day before

    assert latest_slot(entry, _utc(2026, 3, 8, 7, 30), NEW_YORK) == _utc(2026, 3, 8, 7, 30)
    assert _utc(2026, 3, 8, 7, 30).astimezone(NEW_YORK).isoformat() == "2026-03-08T03:30:00-04:00"
    assert latest_slot(entry, _utc(2026, 3, 8, 7, 29, 59), NEW_YORK) == previous
    # 06:30Z reads 01:30 EST: mapping backwards would make the slot fall here.
    assert latest_slot(entry, _utc(2026, 3, 8, 6, 30), NEW_YORK) == previous
    assert is_due(entry, _utc(2026, 3, 8, 7, 30), NEW_YORK, previous)
    assert not is_due(entry, _utc(2026, 3, 8, 7, 29, 59), NEW_YORK, previous)


def test_ambiguous_fall_back_time_is_its_first_occurrence_only() -> None:
    """01:30 occurs twice on 2026-11-01: the slot is 01:30 EDT (05:30Z); 01:30 EST is not one."""
    entry = ScheduleEntry("restatement", ("google",), at=time(1, 30))
    first = _utc(2026, 11, 1, 5, 30)
    second = _utc(2026, 11, 1, 6, 30)
    assert first.astimezone(NEW_YORK).isoformat() == "2026-11-01T01:30:00-04:00"
    assert second.astimezone(NEW_YORK).isoformat() == "2026-11-01T01:30:00-05:00"

    assert latest_slot(entry, first, NEW_YORK) == first
    assert latest_slot(entry, first - ONE_SECOND, NEW_YORK) == _utc(2026, 10, 31, 5, 30)
    assert latest_slot(entry, second, NEW_YORK) == first
    assert latest_slot(entry, second + timedelta(hours=12), NEW_YORK) == first
    assert is_due(entry, first, NEW_YORK, _utc(2026, 10, 31, 5, 30))
    # The run at the first 01:30 satisfies the day: the second 01:30 runs nothing again.
    assert not is_due(entry, second, NEW_YORK, first)
    assert not is_due(entry, second + ONE_SECOND, NEW_YORK, first)


# ---------------------------------------------------------------------------------------------
# DST at local midnight
# ---------------------------------------------------------------------------------------------


def test_havana_skipped_and_repeated_midnight_never_misses_a_slot() -> None:
    """Havana 2026 skips 00:00 in March and repeats it in November; every slot is found.

    The transitions are read from zoneinfo first. On the spring night the nonexistent
    midnight maps forward onto 01:00 CDT (05:00Z); on the autumn night the midnight slot is the
    first 00:00 (04:00Z) and the second 00:00 does not make the job due again. Every 30 s
    around both nights, the latest slot is never later than now and is the latest slot of an
    independently computed set.
    """
    spring = _transitions(HAVANA, _utc(2026, 3, 7), _utc(2026, 3, 10))
    autumn = _transitions(HAVANA, _utc(2026, 10, 31), _utc(2026, 11, 3))
    assert spring == [
        (_utc(2026, 3, 8, 5), "2026-03-07T23:59:59-05:00", "2026-03-08T01:00:00-04:00")
    ]
    assert autumn == [
        (_utc(2026, 11, 1, 5), "2026-11-01T00:59:59-04:00", "2026-11-01T00:00:00-05:00")
    ]
    entry = _entry("entities")

    assert latest_slot(entry, _utc(2026, 3, 8, 4, 59, 59), HAVANA) == _utc(2026, 3, 8, 2)
    assert latest_slot(entry, _utc(2026, 3, 8, 5), HAVANA) == _utc(2026, 3, 8, 5)
    assert latest_slot(entry, _utc(2026, 3, 8, 6, 59, 59), HAVANA) == _utc(2026, 3, 8, 5)
    assert latest_slot(entry, _utc(2026, 3, 8, 7), HAVANA) == _utc(2026, 3, 8, 7)

    assert latest_slot(entry, _utc(2026, 11, 1, 4), HAVANA) == _utc(2026, 11, 1, 4)
    assert latest_slot(entry, _utc(2026, 11, 1, 5, 30), HAVANA) == _utc(2026, 11, 1, 4)
    assert not is_due(entry, _utc(2026, 11, 1, 5, 30), HAVANA, _utc(2026, 11, 1, 4))
    assert latest_slot(entry, _utc(2026, 11, 1, 8), HAVANA) == _utc(2026, 11, 1, 8)

    for job in ("entities", "metrics", "purge"):
        for instant, _, _ in (*spring, *autumn):
            window = (instant - timedelta(hours=30), instant + timedelta(hours=30))
            assert _scan_latest(_entry(job), HAVANA, *window, WAKE) == [], job


def test_clock_falling_back_across_midnight_keeps_the_midnight_slot() -> None:
    """Antarctica/Casey, 2010-03-04 15:00Z: 01:59:59 (+11) on 03-05 falls back to 23:00 (+08)
    on 03-04, so the clock reads the previous date after 03-05's midnight slot has passed.

    The latest slot of 15:30Z (23:30 on 03-04) is that midnight (13:00Z), found only by
    looking at the day ahead of the local date, not 21:00 on 03-04 (10:00Z); the second
    00:00 (16:00Z) is no slot. Every 30 s around the change the latest slot is never later
    than now and never misses one.
    """
    changes = _transitions(CASEY, _utc(2010, 3, 3), _utc(2010, 3, 6))
    assert changes == [
        (_utc(2010, 3, 4, 15), "2010-03-05T01:59:59+11:00", "2010-03-04T23:00:00+08:00")
    ], "the tz database no longer holds the 2010 Casey change this test relies on"
    now = _utc(2010, 3, 4, 15, 30)
    assert now.astimezone(CASEY).date() == date(2010, 3, 4)
    midnight = _local(CASEY, (2010, 3, 5))
    assert midnight == _utc(2010, 3, 4, 13)
    entry = _entry("entities")

    assert latest_slot(entry, now, CASEY) == midnight
    assert latest_slot(entry, _utc(2010, 3, 4, 16, 30), CASEY) == midnight
    assert not is_due(entry, _utc(2010, 3, 4, 16), CASEY, midnight)
    assert latest_slot(entry, _utc(2010, 3, 4, 18, 59, 59), CASEY) == midnight
    assert latest_slot(entry, _utc(2010, 3, 4, 19), CASEY) == _utc(2010, 3, 4, 19)

    for job in ("entities", "metrics", "restatement"):
        window = (_utc(2010, 3, 3, 12), _utc(2010, 3, 5, 18))
        assert _scan_latest(_entry(job), CASEY, *window, WAKE) == [], job


# ---------------------------------------------------------------------------------------------
# is_due
# ---------------------------------------------------------------------------------------------


def test_is_due_compares_the_last_start_with_the_latest_slot() -> None:
    """Never run -> due; at or after the slot (any zone) -> not due; 1 µs before -> due."""
    entry = _entry("entities")
    now = _utc(2026, 10, 1, 13, 15)
    slot = _utc(2026, 10, 1, 13, 0)
    assert latest_slot(entry, now, NEW_YORK) == slot

    assert is_due(entry, now, NEW_YORK, None)
    assert not is_due(entry, now, NEW_YORK, slot)
    assert not is_due(entry, now, NEW_YORK, slot.astimezone(NEW_YORK))
    assert is_due(entry, now, NEW_YORK, slot - ONE_MICROSECOND)
    assert not is_due(entry, now, NEW_YORK, slot + ONE_SECOND)
    assert not is_due(entry, now, NEW_YORK, now)
    # A run on the previous slot (10:00Z) leaves the 13:00Z slot due.
    assert is_due(entry, now, NEW_YORK, _utc(2026, 10, 1, 10, 0))


def test_downtime_makes_a_job_due_once_until_the_next_slot() -> None:
    """A last run 30 h ago (ten slots missed) is due once; its run satisfies it to 16:00Z."""
    entry = _entry("entities")
    now = _utc(2026, 10, 1, 13, 15)
    last = now - timedelta(hours=30)

    assert is_due(entry, now, NEW_YORK, last)
    ran = now
    for later in (now, now + timedelta(minutes=1), _utc(2026, 10, 1, 15, 59, 59)):
        assert not is_due(entry, later, NEW_YORK, ran), later
    assert is_due(entry, _utc(2026, 10, 1, 16, 0), NEW_YORK, ran)


def test_daily_job_run_yesterday_is_due_only_from_its_slot() -> None:
    """Restatement last run at yesterday's 03:00 EDT: not due at 02:59:59, due from 03:00."""
    entry = _entry("restatement")
    yesterday = _local(NEW_YORK, (2026, 9, 30, 3, 0))
    assert yesterday == _utc(2026, 9, 30, 7, 0)

    assert not is_due(entry, _local(NEW_YORK, (2026, 10, 1, 2, 59, 59)), NEW_YORK, yesterday)
    assert is_due(entry, _local(NEW_YORK, (2026, 10, 1, 3, 0, 0)), NEW_YORK, yesterday)
    assert is_due(entry, _local(NEW_YORK, (2026, 10, 1, 23, 0, 0)), NEW_YORK, yesterday)
    # A run from two days ago is due before today's slot: yesterday's slot was missed.
    two_days = _local(NEW_YORK, (2026, 9, 29, 3, 0))
    assert is_due(entry, _local(NEW_YORK, (2026, 10, 1, 2, 59, 59)), NEW_YORK, two_days)


class _NoOffset(tzinfo):
    """A tzinfo whose offset is unknown, which makes an attached datetime naive."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """No offset is known."""
        del dt  # The same answer for every instant.
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """No DST information is known."""
        del dt  # The same answer for every instant.
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """No name is known."""
        del dt  # The same answer for every instant.
        return None


def test_is_due_and_latest_slot_reject_non_instants() -> None:
    """A non-``datetime`` is a ``TypeError``; a naive or offset-less datetime a ``ValueError``."""
    entry = _entry("entities")
    now = _utc(2026, 10, 1, 13, 15)
    naive = now.replace(tzinfo=None)
    offsetless = now.replace(tzinfo=_NoOffset())
    text: Any = "2026-10-01T13:15:00+00:00"
    day: Any = date(2026, 10, 1)

    for bad in (text, day):
        with pytest.raises(TypeError, match="now must be a timezone-aware datetime"):
            latest_slot(entry, bad, NEW_YORK)
        with pytest.raises(TypeError, match="now must be a timezone-aware datetime"):
            is_due(entry, bad, NEW_YORK, None)
        with pytest.raises(TypeError, match="last_started must be a timezone-aware datetime"):
            is_due(entry, now, NEW_YORK, bad)
    for bad_instant in (naive, offsetless):
        with pytest.raises(ValueError, match="now must be a timezone-aware datetime"):
            latest_slot(entry, bad_instant, NEW_YORK)
        with pytest.raises(ValueError, match="now must be a timezone-aware datetime"):
            is_due(entry, bad_instant, NEW_YORK, now)
        with pytest.raises(ValueError, match="last_started must be a timezone-aware datetime"):
            is_due(entry, now, NEW_YORK, bad_instant)


# ---------------------------------------------------------------------------------------------
# Recurrence over whole local days
# ---------------------------------------------------------------------------------------------

_DAYS: Final[tuple[tuple[str, ZoneInfo, date], ...]] = (
    ("utc-ordinary", UTC_ZONE, date(2026, 10, 1)),
    ("new-york-ordinary", NEW_YORK, date(2026, 10, 1)),
    ("new-york-spring-forward", NEW_YORK, date(2026, 3, 8)),
    ("new-york-fall-back", NEW_YORK, date(2026, 11, 1)),
)

_RUNS_PER_DAY: Final = {
    "entities": 8,
    "metrics": 4,
    "restatement": 1,
    "leads": 8,
    "crm": 8,
    "segments": 1,
    "purge": 1,
}
"""The runs a whole local day holds per job: 24 / 3, 24 / 6, or one nightly run."""


def _planned_walls(job: str) -> tuple[time, ...]:
    """The planned local slot times of ``job`` (AAP 0.3.2), independent of ``SCHEDULE``."""
    if job in THREE_HOURLY:
        return _walls(every_hours=3)
    if job in SIX_HOURLY:
        return _walls(every_hours=6)
    return _walls(at=DAILY_AT[job])


@pytest.mark.parametrize(("name", "zone", "day"), _DAYS, ids=[name for name, _, _ in _DAYS])
def test_a_worker_waking_every_30_s_runs_each_job_at_its_slots(
    name: str, zone: ZoneInfo, day: date
) -> None:
    """Every ``SCHEDULE`` entry runs exactly its day's count, at the expected instants."""
    start, end = _day_bounds(zone, day)
    if name.endswith("spring-forward"):
        assert end - start == timedelta(hours=23)
    elif name.endswith("fall-back"):
        assert end - start == timedelta(hours=25)
    else:
        assert end - start == timedelta(hours=24)

    for entry in SCHEDULE:
        expected = _slot_instants(_planned_walls(entry.job), zone, day, day)
        assert all(start <= slot < end for slot in expected), entry.job

        runs = _simulate(entry, zone, start, end)

        assert runs == expected, entry.job
        assert len(runs) == _RUNS_PER_DAY[entry.job], entry.job


def test_three_hourly_runs_on_new_york_dst_days_follow_the_wall_clock() -> None:
    """Spring: 05, 07, 10 ... 01Z (one gap of 2 h); fall: 04, 08, 11 ... 02Z (one of 4 h)."""
    entry = _entry("entities")

    spring = _simulate(entry, NEW_YORK, *_day_bounds(NEW_YORK, date(2026, 3, 8)))
    fall = _simulate(entry, NEW_YORK, *_day_bounds(NEW_YORK, date(2026, 11, 1)))

    assert spring == [
        _utc(2026, 3, 8, 5),
        _utc(2026, 3, 8, 7),
        _utc(2026, 3, 8, 10),
        _utc(2026, 3, 8, 13),
        _utc(2026, 3, 8, 16),
        _utc(2026, 3, 8, 19),
        _utc(2026, 3, 8, 22),
        _utc(2026, 3, 9, 1),
    ]
    assert fall == [
        _utc(2026, 11, 1, 4),
        _utc(2026, 11, 1, 8),
        _utc(2026, 11, 1, 11),
        _utc(2026, 11, 1, 14),
        _utc(2026, 11, 1, 17),
        _utc(2026, 11, 1, 20),
        _utc(2026, 11, 1, 23),
        _utc(2026, 11, 2, 2),
    ]


def test_hourly_entry_runs_23_times_in_spring_and_24_in_the_fall() -> None:
    """02:00 maps onto 03:00 on the spring day (23 runs); the second 01:00 is no slot (24)."""
    entry = ScheduleEntry("entities", ("google",), every_hours=1)
    hourly = _walls(every_hours=1)
    assert len(hourly) == 24

    spring_day = date(2026, 3, 8)
    spring = _simulate(entry, NEW_YORK, *_day_bounds(NEW_YORK, spring_day))
    assert spring == _slot_instants(hourly, NEW_YORK, spring_day, spring_day)
    assert len(spring) == 23
    assert _utc(2026, 3, 8, 7) in spring  # 03:00 EDT, which 02:00 maps onto
    assert spring[2] - spring[1] == timedelta(hours=1)  # 01:00 EST -> 03:00 EDT

    fall_day = date(2026, 11, 1)
    fall = _simulate(entry, NEW_YORK, *_day_bounds(NEW_YORK, fall_day))
    assert fall == _slot_instants(hourly, NEW_YORK, fall_day, fall_day)
    assert len(fall) == 24
    assert _utc(2026, 11, 1, 5) in fall  # 01:00 EDT, the first 01:00
    assert _utc(2026, 11, 1, 6) not in fall  # 01:00 EST, the second 01:00
    assert fall[2] - fall[1] == timedelta(hours=2)  # 01:00 EDT -> 02:00 EST


_IRREGULAR_WAKES: Final[Sequence[timedelta]] = (timedelta(seconds=7), timedelta(minutes=13))
"""Wake spacings that do not divide an hour, so wakes rarely fall on a slot."""


def test_irregular_wakes_never_run_a_slot_twice() -> None:
    """Whatever the wake spacing, each slot of a day runs once, at the first wake after it."""
    entry = _entry("metrics")
    start, end = _day_bounds(NEW_YORK, date(2026, 11, 1))
    slots = _slot_instants(_walls(every_hours=6), NEW_YORK, date(2026, 11, 1), date(2026, 11, 1))

    for spacing in _IRREGULAR_WAKES:
        runs: list[datetime] = []
        last = start - spacing
        now = start
        while now < end:
            if is_due(entry, now, NEW_YORK, last):
                runs.append(now)
                last = now
            now += spacing
        assert len(runs) == len(slots) == 4, spacing
        for run, slot in zip(runs, slots, strict=True):
            assert slot <= run < slot + spacing, (spacing, run, slot)
