"""Dated finest-partition shortfalls: the grammar of a Q-G6 ``dataset_freshness.reason``.

A Q-G6 (day, ad group) partition still at 10,000 rows leaves ``google.search_terms``
``incomplete`` with a reason starting "result limit reached at finest partition" (AAP
0.3.3). The connector names the days of those partitions, so the reason is *dated*: a later
finished read of those days ends the shortfall, and a range that holds none of them is
complete over that range (AAP 0.3.2, 0.5.4.2 P4: an item is incomplete when a dataset "is not
complete over the window"). The reason text carries the days; no column records them.

Grammar: one or more parts joined by ``"; "``::

    part    = "result limit reached at finest partition (" entries ")"
    entries = pair *("; " pair) ["; " more] | alone
    pair    = date ", ad group " id
    more    = "and " count " more on " days | "and more on " days
    alone   = "1 partition on " days | count " partitions on " days | "partitions on " days
    days    = span *(", " span)
    span    = date | date " to " date

``date`` is ``YYYY-MM-DD``; spans ascend and are disjoint and never adjacent, and ``to`` joins
two different days. A bucket (``more`` or ``alone``) counts the partitions it does not name;
once removing days from it has lost some of its days the count is unknown and is left out.
A count is never below the number of days its bucket spans. A trailing failure note
(:data:`FAILURE_MARKER` and what follows it) is ignored for parsing and kept on every result.

Anything else is *undated* and every helper returns it unchanged: the bare prefix, the earlier
"and K more" form that names no days, LinkedIn's "(one day)", HubSpot's "search result ceiling
reached …", a repairable "call budget exhausted" or "interrupted" reason, "earlier coverage
incomplete", text that is not in the canonical form above, and any mix with such a part. Its
consumers stay conservative with it: incomplete over every range, and cleared only by a read of
the whole stored range.

Only the standard library is imported: ``ads_engine.sync.freshness``, the writer, re-exports
these helpers, and ``ads_engine.analysis.context``, a reader the API process also loads, uses
them without the API importing ``ads_engine.sync``.

Example:
    >>> reason = (
    ...     "result limit reached at finest partition (2026-10-08, ad group 7; "
    ...     "and 3 more on 2026-10-08 to 2026-10-09, 2026-10-12)"
    ... )
    >>> [(start.isoformat(), end.isoformat()) for start, end in shortfall_spans(reason)]
    [('2026-10-08', '2026-10-09'), ('2026-10-12', '2026-10-12')]
    >>> shortfall_outside(reason, start=date(2026, 10, 8), end=date(2026, 10, 8))
    'result limit reached at finest partition (partitions on 2026-10-09, 2026-10-12)'
    >>> shortfall_within(reason, start=date(2026, 10, 10), end=date(2026, 10, 11)) is None
    True
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

__all__ = [
    "FAILURE_MARKER",
    "FINEST_PARTITION",
    "DaySpan",
    "shortfall_outside",
    "shortfall_spans",
    "shortfall_within",
]

FINEST_PARTITION: Final = "result limit reached at finest partition"
"""Prefix of every part of a dated shortfall; ``sync.freshness.REASON_FINEST_PARTITION``."""

FAILURE_MARKER: Final = "; last attempt failed: "
"""Separates a failed attempt's note from the shortfall it was appended to."""

type DaySpan = tuple[date, date]
"""A closed range of native days, ``(first, last)`` with ``first <= last``."""

_SEPARATOR: Final = "; "
_DAY_SEPARATOR: Final = ", "
_ONE_DAY: Final = timedelta(days=1)

_DATE: Final = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_PART: Final = re.compile(re.escape(FINEST_PARTITION) + r" \(([^()]*)\)")
_PAIR: Final = re.compile(rf"({_DATE}), ad group ([0-9]+)")
_MORE: Final = re.compile(r"and (?:([0-9]+) )?more on (.+)")
_ALONE: Final = re.compile(r"(?:([0-9]+) )?partitions? on (.+)")
_SPAN: Final = re.compile(rf"({_DATE})(?: to ({_DATE}))?")


@dataclass(frozen=True, slots=True)
class _Bucket:
    """The partitions a part counts without naming them: how many, and on which days."""

    count: int | None
    spans: tuple[DaySpan, ...]


@dataclass(frozen=True, slots=True)
class _Part:
    """One ``result limit reached at finest partition (…)`` part."""

    pairs: tuple[tuple[date, int], ...]
    bucket: _Bucket | None


def _day(text: str) -> date | None:
    """Return the date ``YYYY-MM-DD`` names, or ``None`` when it is no calendar date."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _day_count(spans: Iterable[DaySpan]) -> int:
    """Return how many days the spans hold."""
    return sum((last - first).days + 1 for first, last in spans)


def _parse_days(text: str) -> tuple[DaySpan, ...] | None:
    """Parse ``days``: ascending, disjoint, non-adjacent spans; ``None`` when malformed."""
    spans: list[DaySpan] = []
    for piece in text.split(_DAY_SEPARATOR):
        match = _SPAN.fullmatch(piece)
        if match is None:
            return None
        first = _day(match.group(1))
        last = first if match.group(2) is None else _day(match.group(2))
        if first is None or last is None or last < first:
            return None
        if spans and (first - spans[-1][1]).days <= 1:
            return None
        spans.append((first, last))
    return tuple(spans)


def _parse_bucket(token: str, *, after_pairs: bool) -> _Bucket | None:
    """Parse the final ``more`` (after pairs) or ``alone`` entry; ``None`` when malformed."""
    match = (_MORE if after_pairs else _ALONE).fullmatch(token)
    if match is None:
        return None
    count = None if match.group(1) is None else int(match.group(1))
    spans = _parse_days(match.group(2))
    if spans is None or (count is not None and count < max(1, _day_count(spans))):
        return None
    return _Bucket(count, spans)


def _parse_entries(inner: str) -> _Part | None:
    """Parse the text between a part's parentheses; ``None`` when it is not ``entries``."""
    tokens = inner.split(_SEPARATOR)
    pairs: list[tuple[date, int]] = []
    for index, token in enumerate(tokens):
        pair = _PAIR.fullmatch(token)
        if pair is not None:
            day = _day(pair.group(1))
            if day is None:
                return None
            pairs.append((day, int(pair.group(2))))
            continue
        if index != len(tokens) - 1:
            return None
        bucket = _parse_bucket(token, after_pairs=bool(pairs))
        return None if bucket is None else _Part(tuple(pairs), bucket)
    return _Part(tuple(pairs), None)


def _render_days(spans: Iterable[DaySpan]) -> str:
    """Render ``days``: a single day as ``YYYY-MM-DD``, a longer span as ``… to …``."""
    return _DAY_SEPARATOR.join(
        first.isoformat() if first == last else f"{first.isoformat()} to {last.isoformat()}"
        for first, last in spans
    )


def _render_part(part: _Part) -> str:
    """Render one part in the canonical form the module docstring defines."""
    entries = [f"{day.isoformat()}, ad group {ad_group}" for day, ad_group in part.pairs]
    bucket = part.bucket
    if bucket is not None:
        days = _render_days(bucket.spans)
        count = bucket.count
        if part.pairs:
            entries.append(f"and {'' if count is None else f'{count} '}more on {days}")
        elif count is None:
            entries.append(f"partitions on {days}")
        else:
            entries.append(f"{count} partition{'' if count == 1 else 's'} on {days}")
    return f"{FINEST_PARTITION} ({_SEPARATOR.join(entries)})"


def _parse(shortfall: str) -> tuple[_Part, ...] | None:
    """Parse a shortfall with no failure note into its parts; ``None`` when it is undated.

    Text that parses but is not in canonical form (a leading zero, ``"1 partitions"``, two
    adjacent spans) is undated too, so every helper re-renders a dated shortfall exactly.
    """
    parts: list[_Part] = []
    position = 0
    while True:
        match = _PART.match(shortfall, position)
        if match is None:
            return None
        part = _parse_entries(match.group(1))
        if part is None:
            return None
        parts.append(part)
        position = match.end()
        if position == len(shortfall):
            break
        if not shortfall.startswith(_SEPARATOR, position):
            return None
        position += len(_SEPARATOR)
    if _SEPARATOR.join(_render_part(part) for part in parts) != shortfall:
        return None
    return tuple(parts)


def _split(reason: str | None) -> tuple[tuple[_Part, ...], str] | None:
    """Return a reason's dated parts and its failure note (``""`` when none), or ``None``."""
    if reason is None:
        return None
    shortfall, marker, note = reason.partition(FAILURE_MARKER)
    parts = _parse(shortfall)
    return None if parts is None else (parts, marker + note)


def _merged(spans: Iterable[DaySpan]) -> tuple[DaySpan, ...]:
    """Return the union of spans as ascending, disjoint, non-adjacent spans."""
    merged: list[DaySpan] = []
    for first, last in sorted(spans):
        if merged and (first - merged[-1][1]).days <= 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return tuple(merged)


def _outside(span: DaySpan, start: date, end: date) -> tuple[DaySpan, ...]:
    """Return the pieces of ``span`` before ``start`` and after ``end``."""
    first, last = span
    pieces: list[DaySpan] = []
    if first < start:
        pieces.append((first, min(last, start - _ONE_DAY)))
    if last > end:
        pieces.append((max(first, end + _ONE_DAY), last))
    return tuple(pieces)


def _within(span: DaySpan, start: date, end: date) -> tuple[DaySpan, ...]:
    """Return the piece of ``span`` inside ``[start, end]``, if any."""
    first, last = max(span[0], start), min(span[1], end)
    return ((first, last),) if first <= last else ()


def _kept_part(part: _Part, start: date, end: date, *, inside: bool) -> _Part | None:
    """Keep the partitions of ``part`` whose day is inside ``[start, end]`` (or outside).

    A bucket whose days are all kept keeps its count; one that lost some of its days loses
    its count; one with no day left is dropped, and so is a part with nothing left.
    """
    pairs = tuple(pair for pair in part.pairs if (start <= pair[0] <= end) is inside)
    bucket = part.bucket
    if bucket is not None:
        clip = _within if inside else _outside
        spans = tuple(piece for span in bucket.spans for piece in clip(span, start, end))
        if not spans:
            bucket = None
        elif spans != bucket.spans:
            bucket = _Bucket(None, spans)
    if not pairs and bucket is None:
        return None
    return _Part(pairs, bucket)


def _kept(reason: str | None, start: date, end: date, *, inside: bool) -> str | None:
    """Shared body of :func:`shortfall_outside` and :func:`shortfall_within`."""
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    split = _split(reason)
    if split is None:
        return reason
    parts, note = split
    kept = [
        _render_part(kept_part)
        for part in parts
        if (kept_part := _kept_part(part, start, end, inside=inside)) is not None
    ]
    if not kept:
        return None
    return _SEPARATOR.join(kept) + note


def shortfall_spans(reason: str | None) -> tuple[DaySpan, ...] | None:
    """Return the days a dated shortfall names, or ``None`` when it is undated.

    Example:
        >>> shortfall_spans("result limit reached at finest partition (2026-10-08, ad group 7)")
        ((datetime.date(2026, 10, 8), datetime.date(2026, 10, 8)),)
        >>> shortfall_spans("result limit reached at finest partition (one day)") is None
        True

    Args:
        reason: A ``dataset_freshness.reason`` value, with or without a failure note, or
            ``None``.

    Returns:
        Every day a named partition or a bucket is on, as ascending, disjoint, non-adjacent
        closed spans; ``None`` for an undated reason (the module docstring), ``None`` included.
    """
    split = _split(reason)
    if split is None:
        return None
    return _merged(
        span
        for part in split[0]
        for span in (
            *((day, day) for day, _ in part.pairs),
            *(() if part.bucket is None else part.bucket.spans),
        )
    )


def shortfall_outside(reason: str | None, *, start: date, end: date) -> str | None:
    """Return a dated shortfall without the days ``[start, end]``: what a re-read leaves.

    Named partitions on those days are dropped and a bucket loses them (and its count, once
    it lost any); a part with nothing left is dropped. The rest is re-rendered exactly, with
    the failure note, if any, still at the end.

    Example:
        >>> shortfall_outside(
        ...     "result limit reached at finest partition (2026-10-08, ad group 7; "
        ...     "2026-10-09, ad group 8)",
        ...     start=date(2026, 10, 8),
        ...     end=date(2026, 10, 8),
        ... )
        'result limit reached at finest partition (2026-10-09, ad group 8)'

    Args:
        reason: A ``dataset_freshness.reason`` value, or ``None``.
        start: The first native day removed.
        end: The last native day removed.

    Returns:
        The remaining shortfall; ``None`` when no named day remains; an undated ``reason``
        (``None`` included) unchanged.

    Raises:
        ValueError: If ``start`` is after ``end``.
    """
    return _kept(reason, start, end, inside=False)


def shortfall_within(reason: str | None, *, start: date, end: date) -> str | None:
    """Return a dated shortfall restricted to the days ``[start, end]``: what a range lacks.

    The counterpart of :func:`shortfall_outside`, for naming only the days of the range a
    message is about; the failure note, if any, stays at the end.

    Example:
        >>> shortfall_within(
        ...     "result limit reached at finest partition (2026-10-08, ad group 7; "
        ...     "and 3 more on 2026-10-08 to 2026-10-10)",
        ...     start=date(2026, 10, 9),
        ...     end=date(2026, 10, 31),
        ... )
        'result limit reached at finest partition (partitions on 2026-10-09 to 2026-10-10)'

    Args:
        reason: A ``dataset_freshness.reason`` value, or ``None``.
        start: The first native day of the range.
        end: The last native day of the range.

    Returns:
        The shortfall on the range's days; ``None`` when none of its days is inside; an
        undated ``reason`` (``None`` included) unchanged.

    Raises:
        ValueError: If ``start`` is after ``end``.
    """
    return _kept(reason, start, end, inside=True)
