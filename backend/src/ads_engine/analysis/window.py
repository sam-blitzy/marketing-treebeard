"""Analysis windows: one calendar-date range applied to each source's native days.

AAP 0.5.1 ("Window and thresholds"), 0.5.3 (``POST /runs``, ``Run``, ``Evidence.window``),
0.5.4.1 ("Analysis window", "Day boundaries"), 0.3.2 (segment windows for C3a).

Native-day policy (AAP 0.5.4.1):

* Each source's daily rows keep their native day. Google daily rows keep ``segments.date`` in
  the customer account's ``customer.time_zone``; LinkedIn daily rows are UTC dates. Daily rows
  are never relabelled or rebucketed.
* A window is **one calendar-date range applied to each source's own dates**. Evidence lists
  each source's range and boundary in ``source_days`` and as the sentence ``description``, for
  example "Google 2026-09-04 to 2026-10-01 (America/New_York); LinkedIn same dates (UTC)"
  (:meth:`AnalysisWindow.describe`), and when the boundaries differ the evidence says so in
  ``boundaries_differ`` (:attr:`AnalysisWindow.boundaries_differ`), so misaligned buckets are
  never presented as one common day. :meth:`AnalysisWindow.to_json` freezes all four into
  ``Evidence.window``.
* No computation combines two sources' daily rows into one daily series. Cross-source joins
  work on instants: LinkedIn leads count when ``submittedAt`` falls in
  ``[start 00:00 UTC, end + 1 day 00:00 UTC)`` (:meth:`AnalysisWindow.utc_bounds`), and HubSpot
  events, which have no day boundary, are compared as instants.
* ``DISPLAY_TIMEZONE`` only formats instants for display. It never moves a day boundary, and
  this module never reads it.

Window rules (AAP 0.5.4.1, 0.5.3):

* The default window is ``window.default_days`` complete days ending on :func:`default_end`:
  the latest date complete in every active source's native day, the earlier of yesterday in
  the Google account's time zone and yesterday UTC. Today is always excluded as a partial day.
* A custom window ends no later than the default end and spans 1 to ``window.max_days`` days;
  anything else raises :class:`InvalidWindow`, which the API maps to 422 with its ``code``.
  ``custom`` has two call shapes that share one validation:
  ``AnalysisWindow.custom(start, end, config=..., sources=..., now=...)`` on the class builds
  the window from the loaded configuration and the sources' boundaries at ``now``, and
  ``window.custom(start, end)`` on a configured window (AAP 0.5.1) reuses that window's
  configuration, boundaries and default end.
* A window shorter than ``window.trusted_min_days`` is a short window; sustained-pattern rules
  need at least ``window.sustained_min_days``; C3a segment evidence exists only for a window in
  ``window.presets`` ending on the default end. Every one of these values is read from
  :class:`~ads_engine.analysis.thresholds.Thresholds`, never written here as a literal, so the
  UI and evidence render the configured values.

Types (decision 6): native dates are :class:`datetime.date`; instants are tz-aware
:class:`datetime.datetime`. A ``datetime`` passed where a native date is expected is rejected
with ``TypeError`` rather than silently truncated, and every instant this module returns is
expressed in UTC so that subtraction and comparison are true instant arithmetic.

Purity: standard library plus :class:`~ads_engine.analysis.thresholds.Thresholds` only. No
database, settings, logging or network access.

Example:
    >>> from datetime import UTC, date, datetime
    >>> info = SourceDayInfo("America/New_York")
    >>> default_end(info, now=datetime(2026, 10, 6, 2, 0, tzinfo=UTC))
    datetime.date(2026, 10, 4)
    >>> window = AnalysisWindow.default(thresholds, info)  # doctest: +SKIP
    >>> window.days, window.short_window, window.is_segment_window()  # doctest: +SKIP
    (28, False, True)
    >>> window.custom(window.end - timedelta(days=6), window.end).days  # doctest: +SKIP
    7
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, Literal, Protocol, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_engine.analysis.thresholds import Thresholds

__all__ = [
    "DAY_SOURCES",
    "SOURCE_LABELS",
    "UTC_BOUNDARY",
    "AnalysisWindow",
    "DaySource",
    "InvalidWindow",
    "InvalidWindowCode",
    "SourceDayInfo",
    "SourceDays",
    "default_end",
]

DaySource = Literal["google", "linkedin"]
"""A source with native daily rows. HubSpot holds instants only and has no day boundary."""

InvalidWindowCode = Literal["end_before_start", "end_after_default_end", "too_long", "too_short"]
"""The machine codes :class:`InvalidWindow` carries; ``POST /runs`` returns them with 422."""

DAY_SOURCES: Final[tuple[DaySource, ...]] = ("google", "linkedin")
"""Every day source, in evidence order: Google first, then LinkedIn (AAP 0.5.4.1 example)."""

SOURCE_LABELS: Final[dict[DaySource, str]] = {"google": "Google", "linkedin": "LinkedIn"}
"""Display names used by :meth:`AnalysisWindow.describe`."""

UTC_BOUNDARY: Final = "UTC"
"""The boundary of LinkedIn days, and of Google days while no account time zone is known."""

_ONE_DAY: Final = timedelta(days=1)


# ---------------------------------------------------------------------------
# Errors and validation helpers
# ---------------------------------------------------------------------------


class InvalidWindow(ValueError):  # noqa: N818 - contract name used by api/app.py and cli.py
    """A requested window violates the AAP 0.5.3 ``POST /runs`` bounds.

    ``api/app.py`` maps it to HTTP 422 with ``{"error": {"code": code, "message": message}}``,
    and ``ads-engine analyze`` exits with code 2.

    Attributes:
        code: One of ``end_before_start``, ``end_after_default_end``, ``too_long`` or
            ``too_short``.
        message: A sentence naming the offending dates or day counts.
    """

    code: InvalidWindowCode
    message: str

    def __init__(self, code: InvalidWindowCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __reduce__(self) -> tuple[type[InvalidWindow], tuple[InvalidWindowCode, str]]:
        """Pickle with both constructor arguments, so the error survives a process boundary."""
        return (type(self), (self.code, self.message))


def _zone(name: object, what: str) -> ZoneInfo:
    """Load an IANA time zone, turning every lookup failure into ``ValueError``.

    Args:
        name: The zone name, for example ``America/New_York`` or ``UTC``.
        what: The argument being validated, for the error message.

    Raises:
        ValueError: When ``name`` is not a string the tz database can load.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"{what} must be an IANA time zone name, found {name!r}")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise ValueError(
            f"{what} must be an IANA time zone name such as UTC or America/New_York, found {name!r}"
        ) from None


def _native_date(value: object, what: str) -> date:
    """Require a native calendar date (decision 6).

    ``datetime`` is a subclass of ``date``; accepting one would silently truncate an instant
    onto whatever day its own offset puts it on, which is exactly the rebucketing the
    native-day policy forbids.

    Raises:
        TypeError: When ``value`` is not a ``date`` or is a ``datetime``.
    """
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError(f"{what} must be a datetime.date (a native day), found {value!r}")
    return value


def _positive_int(value: object, what: str) -> int:
    """Require a positive integer day count; ``bool`` is rejected.

    Raises:
        ValueError: When ``value`` is not an ``int`` of at least 1.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be a positive integer number of days, found {value!r}")
    return value


def _day_source(value: object, what: str) -> DaySource:
    """Require one of :data:`DAY_SOURCES`.

    Raises:
        ValueError: When ``value`` is not ``"google"`` or ``"linkedin"``.
    """
    for source in DAY_SOURCES:
        if value == source:
            return source
    raise ValueError(f"{what} must be one of {DAY_SOURCES}, found {value!r}")


def _aware_now(now: datetime | None) -> datetime:
    """Return ``now`` (tz-aware), or the current instant when ``None``.

    Raises:
        TypeError: When ``now`` is not a ``datetime``.
        ValueError: When ``now`` is naive.
    """
    if now is None:
        return datetime.now(UTC)
    candidate: object = now
    if not isinstance(candidate, datetime):
        raise TypeError(f"now must be a datetime, found {candidate!r}")
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError("now must be a tz-aware datetime (an instant), found a naive datetime")
    return candidate


def _check_span(start: date, end: date, latest: date, max_days: int) -> None:
    """Enforce the window bounds of AAP 0.5.3 ``POST /runs`` and 0.5.4.1, in a fixed order.

    The end is no later than ``latest``, the default window end, and the span runs from 1 day
    to ``max_days`` (``window.max_days``). Every path that builds or constructs a window
    applies this one check, so a bound is reported with the same code and message whichever
    path met it.

    Args:
        start: The first native date, inclusive.
        end: The last native date, inclusive.
        latest: The default window end the window may not pass.
        max_days: ``window.max_days``, the longest window in days.

    Raises:
        InvalidWindow: ``end_before_start``, ``end_after_default_end`` (the message names
            ``latest``), ``too_long`` above ``max_days``, or ``too_short`` below 1 day.
    """
    if end < start:
        raise InvalidWindow("end_before_start", f"Window end {end} is before its start {start}")
    if end > latest:
        raise InvalidWindow(
            "end_after_default_end",
            f"Window end {end} is after the default window end {latest}, the latest date "
            "complete in every source's native day",
        )
    days = (end - start).days + 1
    if days > max_days:
        raise InvalidWindow(
            "too_long",
            f"Window of {days} days exceeds the {max_days}-day maximum (window.max_days)",
        )
    # Unreachable once end >= start (the span is then at least 1 day); kept so the
    # documented bound "from 1 day" is checked explicitly rather than implied.
    if days < 1:
        raise InvalidWindow("too_short", f"Window of {days} days is shorter than 1 day")


# ---------------------------------------------------------------------------
# Source day boundaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceDayInfo:
    """The native day boundary of each day source, as the active datasets define it.

    ``runner.window_inputs(session)`` builds it from the active Google dataset's
    ``ad_account.timezone``; tests build it directly, e.g. ``SourceDayInfo("America/New_York")``.

    Attributes:
        google_timezone: The Google customer account's IANA time zone (``customer.time_zone``),
            or ``None`` when Google has no active dataset or no account row yet, in which case
            Google days are treated as UTC.
        day_sources: The sources whose daily rows the window applies to, in evidence order.
            Non-empty, without repeats, each in :data:`DAY_SOURCES`; a list is accepted and
            stored as a tuple.

    Raises:
        ValueError: On construction, when ``google_timezone`` is not a loadable IANA zone or
            ``day_sources`` is empty, repeats a source or names an unknown one.
    """

    google_timezone: str | None
    day_sources: tuple[DaySource, ...] = DAY_SOURCES

    def __post_init__(self) -> None:
        """Validate the time zone and the day sources."""
        if self.google_timezone is not None:
            _zone(self.google_timezone, "google_timezone")
        raw: object = self.day_sources
        if isinstance(raw, str) or not isinstance(raw, tuple | list):
            raise ValueError(f"day_sources must be a tuple of day sources, found {raw!r}")
        sources = tuple(_day_source(item, "day_sources item") for item in raw)
        if not sources:
            raise ValueError("day_sources must name at least one day source")
        if len(set(sources)) != len(sources):
            raise ValueError(f"day_sources must not repeat a source, found {sources}")
        object.__setattr__(self, "day_sources", sources)

    def boundary(self, source: DaySource) -> str:
        """Return the native day boundary of ``source``.

        Google days end at midnight in ``google_timezone`` (``UTC`` while unknown); LinkedIn
        days are UTC (AAP 0.3.3, "LinkedIn days are UTC").
        """
        if source == "google":
            return self.google_timezone or UTC_BOUNDARY
        return UTC_BOUNDARY

    def boundaries(self) -> dict[DaySource, str]:
        """Return ``{source: boundary}`` for every source in ``day_sources``, in order.

        Example:
            >>> SourceDayInfo("America/New_York").boundaries()
            {'google': 'America/New_York', 'linkedin': 'UTC'}
        """
        return {source: self.boundary(source) for source in self.day_sources}


@dataclass(frozen=True, slots=True)
class SourceDays:
    """One source's date range and day boundary within a window.

    This is one element of ``Run.source_days`` and ``Evidence.window.source_days`` (AAP 0.5.3).

    Attributes:
        source: The day source.
        start: The first native date of the source's range, inclusive.
        end: The last native date of the source's range, inclusive.
        boundary: The IANA zone whose midnight ends the source's days.

    Raises:
        TypeError: On construction, when ``start`` or ``end`` is not a native ``date``.
        ValueError: On construction, when ``source`` is unknown, ``end`` precedes ``start`` or
            ``boundary`` is not a loadable zone.
    """

    source: DaySource
    start: date
    end: date
    boundary: str

    def __post_init__(self) -> None:
        """Validate the source, the dates and the boundary."""
        _day_source(self.source, "source")
        _native_date(self.start, "start")
        _native_date(self.end, "end")
        if self.end < self.start:
            raise ValueError(f"end {self.end} precedes start {self.start}")
        _zone(self.boundary, "boundary")

    def to_json(self) -> dict[str, str]:
        """Return ``{"source", "start", "end", "boundary"}`` with ISO dates (AAP 0.5.3)."""
        return {
            "source": self.source,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "boundary": self.boundary,
        }


def default_end(sources: SourceDayInfo, *, now: datetime | None = None) -> date:
    """Return the default window end: the latest date complete in every source's native day.

    AAP 0.5.4.1: the window end is the earlier of yesterday in the Google account's time zone
    and yesterday in UTC; today is excluded as a partial day. Yesterday UTC always takes part,
    because LinkedIn days are UTC and the window's instant bounds
    (:meth:`AnalysisWindow.utc_bounds`, used for leads and white-paper cohorts) must never
    reach into the current UTC day. Yesterday in the Google zone takes part when ``"google"``
    is in ``sources.day_sources``.

    Args:
        sources: The sources' native day boundaries.
        now: The current instant; ``datetime.now(UTC)`` when omitted. Must be tz-aware.

    Returns:
        The default window end as a native date.

    Raises:
        TypeError: When ``now`` is not a ``datetime``.
        ValueError: When ``now`` is naive.

    Example:
        At 02:00 UTC on 2026-10-06 it is still 2026-10-05 in New York, so yesterday there is
        2026-10-04 while yesterday UTC is 2026-10-05; the earlier date wins. This is the first
        start with the Google account's time zone behind UTC (C3a acceptance case).

        >>> default_end(
        ...     SourceDayInfo("America/New_York"), now=datetime(2026, 10, 6, 2, 0, tzinfo=UTC)
        ... )
        datetime.date(2026, 10, 4)
    """
    instant = _aware_now(now)
    latest = instant.astimezone(UTC).date() - _ONE_DAY
    if "google" in sources.day_sources:
        google_zone = _zone(sources.boundary("google"), "google_timezone")
        latest = min(latest, instant.astimezone(google_zone).date() - _ONE_DAY)
    return latest


# ---------------------------------------------------------------------------
# Analysis window
# ---------------------------------------------------------------------------


class _WindowSettings(Protocol):
    """The configured ``window.*`` values a window is built with.

    Both :class:`~ads_engine.analysis.thresholds.Thresholds` (the loaded configuration) and
    :class:`AnalysisWindow` (the values it was built with) provide them, so a custom window is
    assembled the same way from either.
    """

    @property
    def trusted_min_days(self) -> int:
        """``window.trusted_min_days``."""
        ...

    @property
    def default_days(self) -> int:
        """``window.default_days``."""
        ...

    @property
    def sustained_min_days(self) -> int:
        """``window.sustained_min_days``."""
        ...

    @property
    def presets(self) -> tuple[int, ...]:
        """``window.presets``."""
        ...

    @property
    def max_days(self) -> int:
        """``window.max_days``."""
        ...


class _ConfiguredCustom(Protocol):
    """``AnalysisWindow.custom`` read from the class: the configuration-aware form."""

    def __call__(
        self,
        start: date,
        end: date,
        *,
        config: Thresholds,
        sources: SourceDayInfo,
        now: datetime | None = None,
    ) -> AnalysisWindow:
        """Return a validated custom window built from ``config`` and ``sources`` at ``now``."""
        ...


class _BoundCustom(Protocol):
    """``window.custom`` read from an instance: the AAP 0.5.1 two-argument form."""

    def __call__(self, start: date, end: date) -> AnalysisWindow:
        """Return a validated custom window built from the window's own configuration."""
        ...


class _CustomConstructor:
    """The ``AnalysisWindow.custom`` descriptor: one name, two call shapes.

    Read from the class it returns the configuration-aware constructor
    ``custom(start, end, *, config, sources, now=None)``; read from a window it returns the
    two-argument constructor ``custom(start, end)`` bound to that window's configuration. Both
    validate through the same routine. As a class attribute without an annotation it is not a
    dataclass field.
    """

    @overload
    def __get__(self, instance: None, owner: type[AnalysisWindow]) -> _ConfiguredCustom: ...

    @overload
    def __get__(
        self, instance: AnalysisWindow, owner: type[AnalysisWindow] | None = None
    ) -> _BoundCustom: ...

    def __get__(
        self, instance: AnalysisWindow | None, owner: type[AnalysisWindow] | None = None
    ) -> _ConfiguredCustom | _BoundCustom:
        """Return the class-level or the bound constructor, by how ``custom`` was reached.

        Raises:
            TypeError: When called with neither an instance nor an owner class.
        """
        if instance is not None:
            return instance._custom_within
        if owner is None:
            raise TypeError("AnalysisWindow.custom must be read from AnalysisWindow or a window")
        return owner._custom_from_config


@dataclass(frozen=True, slots=True)
class AnalysisWindow:
    """An inclusive calendar-date range applied to each source's native days.

    Build it with :meth:`default` (the configured default window) or ``custom`` (a validated
    ``POST /runs`` or ``ads-engine analyze`` window), in either of its two call shapes:

    * ``AnalysisWindow.custom(start, end, config=config, sources=sources, now=now)`` on the
      class, when no window exists yet: it reads ``window.*`` from ``config`` and computes the
      default end from ``sources`` at ``now``.
    * ``window.custom(start, end)`` on a configured window, for example
      ``AnalysisWindow.default(config, sources).custom(start, end)`` (AAP 0.5.1): it reuses
      that window's ``window.*`` values, its sources and boundaries and its
      :attr:`default_end`, so it needs no configuration and no clock.

    Both shapes validate through one routine and raise the same :class:`InvalidWindow` codes
    and messages. Whether a run is ``default`` or ``custom`` is the caller's decision (AAP
    0.5.3, "Run kinds"): a custom window equal to the default is still a custom run.

    The window carries the configured window settings it was built with, so its flags and
    evidence always reflect ``config/thresholds.yaml`` rather than literals (AAP 0.5.4.1).

    Attributes:
        start: The first native date, inclusive.
        end: The last native date, inclusive.
        default_end: The default window end at build time (:func:`default_end`). Segment
            matching, :attr:`is_default_shape` and ``window.custom(start, end)`` compare
            against it.
        source_days: One :class:`SourceDays` per day source, each with exactly ``start`` and
            ``end`` and that source's boundary.
        trusted_min_days: ``window.trusted_min_days``; shorter windows are flagged short.
        default_days: ``window.default_days``.
        sustained_min_days: ``window.sustained_min_days``; sustained-pattern rules are withheld
            below it.
        presets: ``window.presets``, stored ascending and distinct.
        max_days: ``window.max_days``, the longest window; ``window.custom(start, end)``
            rejects a longer span. It is configuration, not evidence, so :meth:`to_json`
            leaves it out.

    Raises:
        InvalidWindow: On construction, ``end_before_start`` when ``end`` precedes ``start``,
            ``end_after_default_end`` when ``end`` is after ``default_end`` and ``too_long``
            when the window spans more than ``max_days`` days, with the messages ``custom``
            raises.
        TypeError: On construction, when a date field is not a native ``date``.
        ValueError: On construction, when ``source_days`` is empty, repeats a source or
            disagrees with ``start``/``end``, or a day count is not a positive integer.
    """

    start: date
    end: date
    default_end: date
    source_days: tuple[SourceDays, ...]
    trusted_min_days: int
    default_days: int
    sustained_min_days: int
    presets: tuple[int, ...]
    max_days: int

    custom = _CustomConstructor()

    def __post_init__(self) -> None:
        """Enforce the window invariants and normalise the tuple fields."""
        start = _native_date(self.start, "start")
        end = _native_date(self.end, "end")
        latest = _native_date(self.default_end, "default_end")
        _check_span(start, end, latest, _positive_int(self.max_days, "max_days"))

        raw_days: object = self.source_days
        if not isinstance(raw_days, tuple | list):
            raise ValueError(f"source_days must be a tuple of SourceDays, found {raw_days!r}")
        source_days: list[SourceDays] = []
        for item in raw_days:
            if not isinstance(item, SourceDays):
                raise ValueError(f"source_days items must be SourceDays, found {item!r}")
            # Native-day policy: one calendar-date range applied to every source's own dates.
            if item.start != start or item.end != end:
                raise ValueError(
                    f"source_days for {item.source} spans {item.start} to {item.end}, but the "
                    f"window spans {start} to {end}; every source uses the same dates"
                )
            source_days.append(item)
        if not source_days:
            raise ValueError("source_days must list at least one day source")
        sources = [item.source for item in source_days]
        if len(set(sources)) != len(sources):
            raise ValueError(f"source_days must not repeat a source, found {sources}")
        object.__setattr__(self, "source_days", tuple(source_days))

        _positive_int(self.trusted_min_days, "trusted_min_days")
        _positive_int(self.default_days, "default_days")
        _positive_int(self.sustained_min_days, "sustained_min_days")
        raw_presets: object = self.presets
        if not isinstance(raw_presets, tuple | list) or not raw_presets:
            raise ValueError(
                f"presets must be a non-empty tuple of day counts, found {raw_presets!r}"
            )
        presets = {_positive_int(item, "presets item") for item in raw_presets}
        object.__setattr__(self, "presets", tuple(sorted(presets)))

    # -- construction ------------------------------------------------------------------

    @classmethod
    def default(
        cls, config: Thresholds, sources: SourceDayInfo, *, now: datetime | None = None
    ) -> AnalysisWindow:
        """Return the configured default window (AAP 0.5.4.1).

        The window is ``window.default_days`` complete days ending on :func:`default_end`:
        ``start = end - (default_days - 1)``.

        Args:
            config: The loaded configuration; ``window.*`` values are read from it.
            sources: The sources' native day boundaries.
            now: The current instant; ``datetime.now(UTC)`` when omitted. Must be tz-aware.

        Returns:
            The default window, with one :class:`SourceDays` per day source.

        Raises:
            TypeError: When ``now`` is not a ``datetime``.
            ValueError: When ``now`` is naive.
        """
        end = default_end(sources, now=now)
        start = end - timedelta(days=config.default_days - 1)
        return cls._build(start, end, end, config, sources)

    @classmethod
    def _custom_from_config(
        cls,
        start: date,
        end: date,
        *,
        config: Thresholds | None = None,
        sources: SourceDayInfo | None = None,
        now: datetime | None = None,
    ) -> AnalysisWindow:
        """Return a validated custom window (AAP 0.5.3 ``POST /runs``): the class-level form.

        Reached as ``AnalysisWindow.custom(start, end, config=config, sources=sources,
        now=now)``. Use it where no window exists yet, as ``POST /runs`` and
        ``ads-engine analyze`` do; on a configured window, ``window.custom(start, end)``
        builds the same window without restating the configuration.

        The end is no later than the default window end, and the span runs from 1 day to
        ``window.max_days`` (LinkedIn's daily retention). A window equal to the default is
        valid; the caller still records it as a ``custom`` run.

        Args:
            start: The first native date, inclusive.
            end: The last native date, inclusive.
            config: The loaded configuration; ``window.*`` values are read from it. Required;
                the static type of ``AnalysisWindow.custom`` marks it so.
            sources: The sources' native day boundaries. Required, like ``config``.
            now: The current instant; ``datetime.now(UTC)`` when omitted. Must be tz-aware.

        Returns:
            The custom window, with one :class:`SourceDays` per day source.

        Raises:
            InvalidWindow: ``end_before_start``, ``end_after_default_end`` (the message names
                the default end), ``too_long`` above ``window.max_days``, or ``too_short``
                below 1 day.
            TypeError: When ``config`` or ``sources`` is missing, ``start`` or ``end`` is not
                a native ``date``, or ``now`` is not a ``datetime``.
            ValueError: When ``now`` is naive.
        """
        if config is None or sources is None:
            missing = [
                name for name, value in (("config", config), ("sources", sources)) if value is None
            ]
            raise TypeError(
                "AnalysisWindow.custom(start, end) on the class needs the keyword arguments "
                f"config and sources (missing: {', '.join(missing)}); to reuse a window's "
                "configuration call custom(start, end) on that window, for example "
                "AnalysisWindow.default(config, sources).custom(start, end)"
            )
        # The dates are checked before ``now`` is read, so a non-date is reported first.
        first = _native_date(start, "start")
        last = _native_date(end, "end")
        latest = default_end(sources, now=now)
        return cls._custom_window(first, last, latest, config, sources.boundaries())

    def _custom_within(self, start: date, end: date) -> AnalysisWindow:
        """Return a validated custom window on this window's configuration: the AAP 0.5.1 form.

        Reached as ``window.custom(start, end)``. The new window keeps this window's
        ``window.*`` values (:attr:`trusted_min_days`, :attr:`default_days`,
        :attr:`sustained_min_days`, :attr:`presets`, :attr:`max_days`), its day sources and
        boundaries in order, and its :attr:`default_end`, which bounds the new end. No clock
        is read: on a window from ``AnalysisWindow.default(config, sources, now=now)`` it
        returns exactly what ``AnalysisWindow.custom(start, end, config=config,
        sources=sources, now=now)`` returns. For a later instant build a fresh default window.

        Args:
            start: The first native date, inclusive.
            end: The last native date, inclusive.

        Returns:
            The custom window, with one :class:`SourceDays` per day source of this window.

        Raises:
            InvalidWindow: ``end_before_start``, ``end_after_default_end`` (the message names
                :attr:`default_end`), ``too_long`` above :attr:`max_days`, or ``too_short``
                below 1 day.
            TypeError: When ``start`` or ``end`` is not a native ``date``.
        """
        boundaries = {item.source: item.boundary for item in self.source_days}
        return type(self)._custom_window(start, end, self.default_end, self, boundaries)

    @classmethod
    def _custom_window(
        cls,
        start: object,
        end: object,
        latest: date,
        settings: _WindowSettings,
        boundaries: Mapping[DaySource, str],
    ) -> AnalysisWindow:
        """Validate and assemble a custom window; both ``custom`` call shapes end here.

        Raises:
            InvalidWindow: As :func:`_check_span`.
            TypeError: When ``start`` or ``end`` is not a native ``date``.
        """
        first = _native_date(start, "start")
        last = _native_date(end, "end")
        # Checked before assembly: SourceDays would otherwise reject a reversed range with a
        # plain ValueError instead of the InvalidWindow code POST /runs returns.
        _check_span(first, last, latest, settings.max_days)
        return cls._assemble(first, last, latest, settings, boundaries)

    @classmethod
    def _build(
        cls,
        start: date,
        end: date,
        latest: date,
        config: Thresholds,
        sources: SourceDayInfo,
    ) -> AnalysisWindow:
        """Assemble a window from the loaded configuration and the sources' boundaries."""
        return cls._assemble(start, end, latest, config, sources.boundaries())

    @classmethod
    def _assemble(
        cls,
        start: date,
        end: date,
        latest: date,
        settings: _WindowSettings,
        boundaries: Mapping[DaySource, str],
    ) -> AnalysisWindow:
        """Assemble a window: the same dates for every day source, each with its boundary."""
        source_days = tuple(
            SourceDays(source=source, start=start, end=end, boundary=boundary)
            for source, boundary in boundaries.items()
        )
        return cls(
            start=start,
            end=end,
            default_end=latest,
            source_days=source_days,
            trusted_min_days=settings.trusted_min_days,
            default_days=settings.default_days,
            sustained_min_days=settings.sustained_min_days,
            presets=settings.presets,
            max_days=settings.max_days,
        )

    # -- flags (AAP 0.5.4.1, 0.5.4.2 P5, 0.3.2) ------------------------------------------

    @property
    def days(self) -> int:
        """The number of native days in the window, both ends included."""
        return (self.end - self.start).days + 1

    @property
    def short_window(self) -> bool:
        """Whether the window is shorter than ``window.trusted_min_days`` (P5).

        A short window raises the short-window banner and puts the ``short_window`` flag on
        every item. With ``window.trusted_min_days: 21`` a 20-day window is short.
        """
        return self.days < self.trusted_min_days

    @property
    def sustained_ok(self) -> bool:
        """Whether the window reaches ``window.sustained_min_days``.

        Below it the sustained-pattern rules (K3, C1, C1b, C2/B1, B2, B3, B4) are withheld with
        the observed and required days (:attr:`days`, :attr:`sustained_min_days`).
        """
        return self.days >= self.sustained_min_days

    @property
    def is_default_shape(self) -> bool:
        """Whether the window has the default length and ends on the default end."""
        return self.days == self.default_days and self.end == self.default_end

    @property
    def boundaries_differ(self) -> bool:
        """Whether the sources' day boundaries differ, which evidence states (:meth:`to_json`)."""
        return len({item.boundary for item in self.source_days}) > 1

    def is_segment_window(self) -> bool:
        """Whether LinkedIn demographic segments exist for exactly this window (C3a).

        Segments are fetched per preset window, each ending on the default window end
        (AAP 0.3.2). C3a is evaluated only when the window's length is a preset and it ends on
        the default end; otherwise it is withheld with ``segment_window_unavailable``.
        """
        return self.days in self.presets and self.end == self.default_end

    # -- native dates --------------------------------------------------------------------

    def dates(self) -> list[date]:
        """Return every native date of the window, ascending."""
        return [self.start + timedelta(days=offset) for offset in range(self.days)]

    def contains(self, d: date) -> bool:
        """Return whether the native date ``d`` lies in the window, ends included.

        Raises:
            TypeError: When ``d`` is a ``datetime``; an instant has no native day until a
                boundary is chosen, so it is never treated as one here.
        """
        day = _native_date(d, "d")
        return self.start <= day <= self.end

    def halves(self) -> tuple[tuple[date, date], tuple[date, date]]:
        """Split the window into a first and a second half by day count (K3).

        Each half is an inclusive ``(start, end)`` pair. For an odd length the middle day goes
        to the first half: 15 days split as 8 and 7.

        Raises:
            ValueError: When the window has fewer than 2 days, so a half would be empty.
        """
        if self.days < 2:
            raise ValueError(f"a {self.days}-day window cannot be split into two halves")
        first_length = (self.days + 1) // 2
        first_end = self.start + timedelta(days=first_length - 1)
        return (self.start, first_end), (first_end + _ONE_DAY, self.end)

    def first_n(self, n: int) -> tuple[date, date]:
        """Return the inclusive span of the first ``n`` days (K3 quality-score spans).

        Raises:
            ValueError: When ``n`` is not an integer from 1 to :attr:`days`.
        """
        count = self._span_length(n)
        return self.start, self.start + timedelta(days=count - 1)

    def last_n(self, n: int) -> tuple[date, date]:
        """Return the inclusive span of the last ``n`` days (K3 quality-score spans).

        Raises:
            ValueError: When ``n`` is not an integer from 1 to :attr:`days`.
        """
        count = self._span_length(n)
        return self.end - timedelta(days=count - 1), self.end

    def _span_length(self, n: object) -> int:
        """Validate a sub-span length against the window."""
        count = _positive_int(n, "n")
        if count > self.days:
            raise ValueError(f"n must be at most the window's {self.days} days, found {count}")
        return count

    # -- instants ------------------------------------------------------------------------

    def utc_bounds(self) -> tuple[datetime, datetime]:
        """Return the half-open instant range ``[start 00:00 UTC, end + 1 day 00:00 UTC)``.

        Used for LinkedIn leads (``submittedAt``) and white-paper cohorts (AAP 0.5.4.1).
        """
        lower = datetime.combine(self.start, time.min, tzinfo=UTC)
        upper = datetime.combine(self.end + _ONE_DAY, time.min, tzinfo=UTC)
        return lower, upper

    @staticmethod
    def day_bounds(d: date, tz: str) -> tuple[datetime, datetime]:
        """Return the half-open instant range of native day ``d`` in zone ``tz``.

        The range is ``[d 00:00 tz, d + 1 day 00:00 tz)``, resolved through :mod:`zoneinfo`,
        so a day across a DST change has its true length (23 or 25 hours in
        ``America/New_York``). Both instants are returned in UTC: Python compares and
        subtracts two datetimes that share one ``tzinfo`` by wall clock, which would make
        every day 24 hours long. A midnight that falls in a DST gap resolves to the first
        instant of the day. Used by budget-day classification (AAP 0.5.4.1).

        Args:
            d: The native date.
            tz: The IANA zone of the source's day boundary, e.g. a :class:`SourceDays`
                ``boundary``.

        Returns:
            ``(day start, next day start)`` as tz-aware UTC datetimes.

        Raises:
            TypeError: When ``d`` is not a native ``date``.
            ValueError: When ``tz`` is not a loadable IANA zone.

        Example:
            >>> lower, upper = AnalysisWindow.day_bounds(date(2026, 3, 8), "America/New_York")
            >>> upper - lower
            datetime.timedelta(seconds=82800)
        """
        day = _native_date(d, "d")
        zone = _zone(tz, "tz")
        lower = datetime.combine(day, time.min, tzinfo=zone)
        upper = datetime.combine(day + _ONE_DAY, time.min, tzinfo=zone)
        return lower.astimezone(UTC), upper.astimezone(UTC)

    # -- evidence (AAP 0.5.3, 0.5.4.8) ---------------------------------------------------

    def boundary(self, source: DaySource) -> str:
        """Return the day boundary ``source`` used in this window.

        Raises:
            KeyError: When ``source`` is not one of the window's day sources.
        """
        for item in self.source_days:
            if item.source == source:
                return item.boundary
        raise KeyError(source)

    def describe(self) -> str:
        """Render each source's range and boundary for evidence and reason text.

        Every source shares the window's dates, so sources after the first read "same dates".

        Example:
            >>> window.describe()  # doctest: +SKIP
            'Google 2026-09-04 to 2026-10-01 (America/New_York); LinkedIn same dates (UTC)'
        """
        parts: list[str] = []
        for index, item in enumerate(self.source_days):
            label = SOURCE_LABELS[item.source]
            if index == 0:
                parts.append(
                    f"{label} {item.start.isoformat()} to {item.end.isoformat()} ({item.boundary})"
                )
            else:
                parts.append(f"{label} same dates ({item.boundary})")
        return "; ".join(parts)

    def source_days_json(self) -> list[dict[str, str]]:
        """Return ``source_days`` as JSON: ``Run.source_days``, ``source_day_boundaries``."""
        return [item.to_json() for item in self.source_days]

    def to_json(self) -> dict[str, Any]:
        """Return exactly ``Evidence.window``.

        Keys, in order: ``start``, ``end``, ``days``, ``source_days``, ``boundaries_differ``
        (:attr:`boundaries_differ`) and ``description`` (:meth:`describe`), so evidence states
        whether the sources' day boundaries differ and never presents misaligned buckets as
        one common day (AAP 0.5.4.1).

        Example:
            With Google on ``America/New_York`` and LinkedIn on UTC, ``boundaries_differ`` is
            ``True`` and ``description`` reads "Google 2026-09-04 to 2026-10-01
            (America/New_York); LinkedIn same dates (UTC)". With both on UTC it is ``False``.
        """
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "source_days": self.source_days_json(),
            "boundaries_differ": self.boundaries_differ,
            "description": self.describe(),
        }
