"""The product's own cap on Google Ads webhook calls (``GOOGLE_DAILY_CALL_BUDGET``).

AAP 0.2.4 (access tiers and quotas) and 0.3.3 ("Timeouts and budget"). The Google Ads
quota belongs to the developer token inside n8n's Google Ads credential. Every other n8n
workflow using that credential draws on the same quota, and the product cannot see it. The
product therefore caps its own webhook calls at ``GOOGLE_DAILY_CALL_BUDGET`` (1,000 by
default) per sliding 24 h, below the 2,880 operations of the smallest documented access
level (Explorer). The cap is separate from the token-wide quota and never measures it.

The cap counts *every* webhook call the product makes: first pages, next-page calls,
partition calls and retries. The live Google client awaits :meth:`GoogleCallBudget.acquire`
before every POST attempt, and a refused call raises
:class:`~ads_engine.connectors.errors.CallBudgetExhausted`. ``connectors/http.py`` never
retries that error; ``connectors/google_n8n/partition.py`` defers the remaining pages, and
the affected datasets stay ``incomplete`` until they finish in a later window. Simulated
mode never meters: a simulated n8n call is not a live Google call, so ``sync/mode.py``
hands the client an unmetered budget instead of this class.

State lives in the database, on the ``source_connection`` row whose ``source`` is
``google``, so the worker, ``ads-engine sync`` and ``ads-engine capture-fixtures`` all
share one cap:

* ``calls_hourly``: call counts per UTC hour, keyed ``"YYYY-MM-DDTHH"``;
* ``calls_24h``: their sum over the counted window, shown on ``/sources``.

Every read-modify-write runs in a short transaction of its own, never inside the caller's
job transaction, under ``SELECT ... FOR UPDATE`` on that row. The row lock is therefore
held for milliseconds, and concurrent coroutines and processes cannot overspend. No count
is ever cached in process, and database errors propagate unchanged.

The clock that places a call in its hour is read only once the row lock is held, so a
call that waited for the lock is placed at the instant it is admitted, not when it asked.
An admitted call is booked in the hour that holds that instant plus the dispatch bound
(140 s), the longest its POST can take to start leaving once ``acquire`` returns: a call
admitted in the last 140 s of an hour counts in the next hour, which the window already
counts. The bound runs from the return, not the admission, so the clock is read again
once the booking has committed. When the commit took so long that the POST could now
start after the booked hour, the booking stays counted and the call is reserved afresh,
or refused, so a POST never leaves after the hour it was booked in.

Example:
    One ``acquire`` per POST attempt; a refusal is a deferral, never a retry::

        budget = GoogleCallBudget()
        try:
            await budget.acquire()
        except CallBudgetExhausted as exc:
            ...  # defer the page; exc.retry_after says when a call frees up
        else:
            ...  # exactly one webhook POST attempt

        used = await budget.used_24h()  # the scheduler refreshes calls_24h on each wake
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, overload

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.errors import CallBudgetExhausted
from ads_engine.connectors.http import CONNECT_TIMEOUT_SECONDS, SOURCE_TIMEOUTS
from ads_engine.db.models.ops import SourceConnection
from ads_engine.db.session import get_sessionmaker
from ads_engine.logging import get_logger
from ads_engine.settings import Settings, Source, get_settings

__all__ = ["BUDGET_ENDPOINT", "WINDOW_BUCKETS", "GoogleCallBudget"]

_log = get_logger(__name__)

BUDGET_ENDPOINT: Final = "webhook"
"""The ``endpoint`` of every :class:`CallBudgetExhausted` raised here: the n8n webhook."""

WINDOW_BUCKETS: Final = 25
"""UTC hour buckets counted against the cap: the current hour plus the 24 preceding ones."""

_SOURCE: Final[Source] = "google"
_EXHAUSTED_CODE: Final = "CALL_BUDGET_EXHAUSTED"

# "Google call-budget usage" (AAP 0.11.1) is logged on every 50th call in the window and
# on every refusal, so the log shows consumption without one line per webhook call.
_LOG_EVERY_CALLS: Final = 50

# The oldest counted bucket starts this long before the current one, and a bucket stops
# being counted once the current bucket starts more than 24 h after it, which happens
# exactly WINDOW_BUCKETS hours after the bucket itself started.
_LOOKBACK: Final = timedelta(hours=WINDOW_BUCKETS - 1)
_BUCKET_LIFETIME: Final = timedelta(hours=WINDOW_BUCKETS)

# The dispatch bound: the longest time from acquire() returning to its POST starting to
# leave. connectors/http.py builds the request at once and hands it to httpx. The AAP's
# 120 s Google per-call timeout is the client's pool, read and write setting, so httpx
# waits at most 120 s for a pooled connection. A new connection then opens under the
# 10 s connect timeout, which httpcore applies to the TCP connect (name resolution
# included) and again to the TLS handshake of the https webhook. Past either limit httpx
# raises PoolTimeout or ConnectTimeout instead of sending later, so on a direct connection
# the POST starts to leave within 120 + 10 + 10 = 140 s. The call is booked in the hour
# holding admission + bound, and acquire() re-checks that hour against the clock after
# the commit, so the booked bucket is never earlier than the hour the POST leaves in and
# the call keeps counting for at least 24 h after it is made. That bucket is at most the
# next one, which _evaluate already counts as a bucket ahead of the current one, so the
# call also counts from the moment it is reserved.
_DISPATCH_BOUND: Final = timedelta(seconds=SOURCE_TIMEOUTS["google"] + 2 * CONNECT_TIMEOUT_SECONDS)

_BUCKET_KEY_FORMAT: Final = "%Y-%m-%dT%H"
_BUCKET_KEY_RE: Final = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})")

# Bounds the wait for the row lock. Every holder of this lock keeps it for milliseconds,
# so a long wait means a caller's own open transaction holds the row (for example a job
# that updated source_connection and has not committed yet). That wait passes through
# the application, where PostgreSQL's deadlock detector cannot see it; the timeout turns
# it into a database error instead of a worker that hangs forever.
_LOCK_TIMEOUT: Final = text("SET LOCAL lock_timeout = '10s'")

_LOCK_ROW: Final = (
    select(SourceConnection.calls_hourly, SourceConnection.calls_24h)
    .where(SourceConnection.source == _SOURCE)
    .with_for_update()
)

# Only live Google calls are metered, so a row created here is live and healthy.
# sync/mode.py owns every later mode and status change and never resets the counters.
_ENSURE_ROW: Final = (
    insert(SourceConnection)
    .values(source=_SOURCE, mode="live", status="ok", calls_24h=0, calls_hourly={})
    .on_conflict_do_nothing(index_elements=["source"])
)


def _utcnow() -> datetime:
    """Return the current instant in UTC: the default clock."""
    return datetime.now(UTC)


def _hour_start(instant: datetime) -> datetime:
    """Return the start of the UTC hour that contains ``instant``."""
    return instant.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _bucket_key(start: datetime) -> str:
    """Render a bucket start as its ``calls_hourly`` key, for example ``2026-10-07T14``."""
    return start.strftime(_BUCKET_KEY_FORMAT)


def _parse_bucket_key(key: object) -> datetime | None:
    """Parse a canonical ``"YYYY-MM-DDTHH"`` key into its UTC hour start.

    Args:
        key: A key of the stored ``calls_hourly`` object.

    Returns:
        The aware UTC start of the hour, or ``None`` for anything that is not a canonical
        key of a real calendar hour.
    """
    if not isinstance(key, str):
        return None
    match = _BUCKET_KEY_RE.fullmatch(key)
    if match is None:
        return None
    year, month, day, hour = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, hour, tzinfo=UTC)
    except ValueError:
        return None


def _parse_count(value: object) -> int | None:
    """Parse a stored bucket count.

    Args:
        value: A value of the stored ``calls_hourly`` object.

    Returns:
        A non-negative integer count. A finite non-negative float is rounded *up*, so a
        hand-edited value can only tighten the cap. ``None`` for anything else, including
        booleans and negative numbers.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0:
        return math.ceil(value)
    return None


@dataclass(frozen=True, slots=True)
class _Window:
    """The calls counted against the cap at one instant, after pruning.

    Attributes:
        now: The UTC instant the window was evaluated at.
        current: Start of the current UTC hour bucket.
        buckets: Counted bucket starts and their positive counts. Buckets later than
            ``current`` (a call booked in the next hour under the dispatch bound, or one
            written by a process whose clock runs ahead) are kept and counted, which errs
            towards refusing a call rather than overspending.
        used: The sum of ``buckets``.
        dirty: Whether the stored row differs from this pruned state and must be written.
    """

    now: datetime
    current: datetime
    buckets: Mapping[datetime, int]
    used: int
    dirty: bool

    @property
    def reservation(self) -> datetime:
        """Start of the bucket a call admitted at ``now`` is booked in.

        It is the UTC hour holding ``now`` plus the 140 s dispatch bound: ``current`` for
        most of the hour, and the next hour for a call admitted within the bound of the
        hour's end, whose POST may start after that end. ``acquire`` relies on the booking
        only when :meth:`covers_dispatch_after` holds for the instant it returns at.
        """
        return _hour_start(self.now + _DISPATCH_BOUND)

    def covers_dispatch_after(self, instant: datetime) -> bool:
        """Whether a POST that starts within the dispatch bound of ``instant`` stays booked.

        Args:
            instant: The aware UTC instant from which the POST may still be delayed by up
                to the dispatch bound, read after the booking committed.

        Returns:
            ``True`` when the latest such POST falls in the :attr:`reservation` hour or
            before it, so the booking keeps counting for at least 24 h after the POST.
            ``False`` when it could fall in a later hour, whose call the booking would
            stop counting less than 24 h after it was made.
        """
        return _hour_start(instant + _DISPATCH_BOUND) <= self.reservation

    def with_call(self) -> dict[datetime, int]:
        """Return the buckets with one more call in the :attr:`reservation` bucket."""
        buckets = dict(self.buckets)
        buckets[self.reservation] = buckets.get(self.reservation, 0) + 1
        return buckets

    def seconds_until_free(self, cap: int) -> float | None:
        """Return how long until enough buckets leave the window for one call to fit.

        When the window holds exactly ``cap`` calls, which is the normal case, this is the
        time until the oldest counted bucket leaves the window. When it holds more (the
        cap was lowered), as many of the oldest buckets as needed must leave first.

        Args:
            cap: The call cap.

        Returns:
            Positive seconds, or ``None`` when the cap is 0 and no call ever fits.
        """
        if cap <= 0:
            return None
        freed = 0
        for start in sorted(self.buckets):
            freed += self.buckets[start]
            if self.used - freed < cap:
                return (start + _BUCKET_LIFETIME - self.now).total_seconds()
        return None


def _serialise(buckets: Mapping[datetime, int]) -> dict[str, int]:
    """Render buckets as the ``calls_hourly`` object, ordered by hour."""
    return {_bucket_key(start): buckets[start] for start in sorted(buckets)}


def _evaluate(stored_hourly: object, stored_total: int, now: datetime) -> _Window:
    """Prune the stored counters to the counted window at ``now``.

    Buckets that started more than 24 hours before the current hour bucket are dropped,
    and so are zero counts. A key or count this module cannot have written (the row was
    edited by hand) cannot be placed in time; it is dropped too, with a warning, so one
    corrupt entry can never block every later call.

    Args:
        stored_hourly: The stored ``calls_hourly`` value.
        stored_total: The stored ``calls_24h`` value.
        now: The aware UTC instant to evaluate at.

    Returns:
        The pruned window, with ``dirty`` set when the row must be rewritten.
    """
    current = _hour_start(now)
    oldest = current - _LOOKBACK
    buckets: dict[datetime, int] = {}
    discarded = 0
    entries: list[tuple[object, object]] = []
    if isinstance(stored_hourly, Mapping):
        entries.extend(stored_hourly.items())
    else:
        # Not an object at all (JSON null, a list): nothing in it can be placed in time.
        discarded += 1
    for key, value in entries:
        start = _parse_bucket_key(key)
        count = _parse_count(value)
        if start is None or count is None:
            discarded += 1
            continue
        if count == 0 or start < oldest:
            continue
        buckets[start] = buckets.get(start, 0) + count
    if discarded:
        _log.warning("google.budget.buckets_discarded", source=_SOURCE, discarded=discarded)
    used = sum(buckets.values())
    dirty = stored_total != used or stored_hourly != _serialise(buckets)
    return _Window(now=now, current=current, buckets=buckets, used=used, dirty=dirty)


@overload
async def _lock_window(
    session: AsyncSession, clock: Callable[[], datetime], *, create: Literal[True]
) -> _Window: ...


@overload
async def _lock_window(
    session: AsyncSession, clock: Callable[[], datetime], *, create: Literal[False]
) -> _Window | None: ...


async def _lock_window(
    session: AsyncSession, clock: Callable[[], datetime], *, create: bool
) -> _Window | None:
    """Lock the Google ``source_connection`` row and evaluate its window.

    The clock is read only after the row lock is held, on the create path too. However
    long the lock wait took, the window is evaluated, and a call booked, at the instant
    the row is this transaction's, which no other holder can still change.

    Args:
        session: A session inside the transaction that will write the row.
        clock: Returns the aware UTC instant to evaluate at; read once, under the lock.
        create: Whether to create the row when it does not exist yet.

    Returns:
        The pruned window, or ``None`` when the row is absent and ``create`` is false.
        The clock is not read when ``None`` is returned.

    Raises:
        ValueError: When ``clock`` raises it for a naive instant; the caller's transaction
            then rolls back and nothing is counted.
        sqlalchemy.exc.SQLAlchemyError: On any database failure, including a lock wait
            beyond the lock timeout.
    """
    await session.execute(_LOCK_TIMEOUT)
    row = (await session.execute(_LOCK_ROW)).one_or_none()
    if row is None:
        if not create:
            return None
        # Selecting first keeps the common path to one statement; the insert runs only
        # once per database, and ON CONFLICT covers two processes creating it at once.
        await session.execute(_ENSURE_ROW)
        row = (await session.execute(_LOCK_ROW)).one()
    return _evaluate(row.calls_hourly, row.calls_24h, clock())


async def _store(session: AsyncSession, buckets: Mapping[datetime, int], used: int) -> None:
    """Write the counters of the locked Google row.

    Only ``calls_hourly`` and ``calls_24h`` are written; mode, status and every other
    column belong to ``sync/mode.py``.
    """
    await session.execute(
        update(SourceConnection)
        .where(SourceConnection.source == _SOURCE)
        .values(calls_hourly=_serialise(buckets), calls_24h=used)
        .execution_options(synchronize_session=False)
    )


class GoogleCallBudget:
    """The product's cap on n8n webhook calls per sliding 24 h, shared across processes.

    It satisfies the Google client's ``CallBudget`` protocol structurally: one awaitable
    :meth:`acquire` per POST attempt, raising :class:`CallBudgetExhausted` when no call is
    left. Every instance reads and writes the same ``source_connection`` row, so a worker
    and a CLI process holding separate instances share one count.

    **Window.** Calls are counted in UTC hour buckets, and the window is the current hour
    bucket plus the 24 preceding ones (:data:`WINDOW_BUCKETS`), together with any bucket
    ahead of it. Hour buckets can only approximate a sliding 24 h, so the approximation is
    deliberately conservative: any 24 h interval ending now lies inside those 25 buckets,
    so the count is never below the true number of calls in the trailing 24 h, and no 24 h
    interval can ever exceed the cap. A call is booked in the hour holding its admission
    instant plus the 140 s dispatch bound, the longest its POST can take to start leaving
    once :meth:`acquire` returns: a call admitted within 140 s of an hour's end is booked
    in the next hour. The price is that a call keeps counting for up to 25 h plus that
    bound. Advancing 25 h plus the bound from a call's last admission always frees it. A
    reservation whose commit lasts so long that the POST could start after the booked
    hour is not relied on: it stays counted, and the call is reserved again or refused, so
    one such call may count twice for a day.

    **Cap.** ``Settings.google_daily_call_budget`` (``GOOGLE_DAILY_CALL_BUDGET``, 1,000 by
    default). A cap of 0 refuses every call. A lowered cap takes effect at once: calls
    already counted above it stay counted until they leave the window.

    **Transactions.** Each method opens its own short sessions and transactions, never the
    caller's: one per reservation or read, each taking ``SELECT ... FOR UPDATE`` on the
    row (waiting at most 10 s for the lock) and committing before the method returns or
    raises. Correctness rests on that row lock, so concurrent coroutines and processes
    cannot overspend, and nothing is cached in process. Database errors
    (:class:`sqlalchemy.exc.SQLAlchemyError`) propagate.

    Example:
        Two processes share one cap through the database::

            worker_budget = GoogleCallBudget()
            cli_budget = GoogleCallBudget()
            await worker_budget.acquire()
            await cli_budget.acquire()
            assert await worker_budget.used_24h() == 2
    """

    __slots__ = ("_cap", "_clock", "_sessionmaker")

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure the budget; no database work happens until the first call.

        Args:
            settings: Supplies the cap; :func:`~ads_engine.settings.get_settings` when
                omitted.
            sessionmaker: The session factory to use. When omitted, the process-wide
                :func:`~ads_engine.db.session.get_sessionmaker` is looked up on every
                call rather than once, so a budget outlives an engine disposed and rebuilt
                in between (worker shutdown, tests switching databases).
            clock: Returns the current instant, timezone-aware; ``datetime.now(UTC)``
                when omitted. Tests inject one to move through the window.

        Raises:
            ValueError: When the configured cap is negative.
        """
        resolved = settings if settings is not None else get_settings()
        cap = resolved.google_daily_call_budget
        if cap < 0:
            raise ValueError(f"GOOGLE_DAILY_CALL_BUDGET must be 0 or more, got {cap}")
        self._cap: int = cap
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = sessionmaker
        self._clock: Callable[[], datetime] = clock if clock is not None else _utcnow

    def __repr__(self) -> str:
        """Return the class name and the cap; the counts live only in the database."""
        return f"{type(self).__name__}(cap={self._cap})"

    @property
    def cap(self) -> int:
        """The cap on webhook calls per sliding 24 h."""
        return self._cap

    async def acquire(self) -> None:
        """Count one webhook call against the cap, or refuse it.

        Call it once before every POST attempt to the n8n webhook, including next-page,
        partition and retry attempts. A counted call stays counted whether or not the POST
        then succeeds: Google charges an operation even for a rejected request.

        The clock is checked before any session is opened, but the instant that admits and
        places the call is read only once the row lock is held, so a wait for the lock
        across an hour's end cannot book the call in the hour it asked in. The call is
        booked in the hour holding that instant plus the 140 s dispatch bound.

        The bound runs from this method's return, so the clock is read again after the
        booking commits. When the UPDATE or the COMMIT took so long that the POST could
        now start after the booked hour, the booking would stop counting less than 24 h
        after the POST, so it is not relied on: ``google.budget.reservation_expired`` is
        logged, the booking stays counted (an over-count, which only errs towards
        refusing), and the call is reserved afresh in a new short transaction, with the
        clock read under the lock and the cap checked again. That repeats until a booking
        covers the POST, or a reservation is refused.

        Raises:
            CallBudgetExhausted: When the window already holds ``cap`` calls. Its
                ``retry_after`` is the number of seconds until a call frees up, which is
                when the oldest counted hour bucket leaves the window whenever the window
                holds exactly ``cap`` calls; ``None`` for a cap of 0. A refused
                reservation counts nothing; an expired booking it was to replace stays
                counted. No POST may be made.
            ValueError: When the injected clock returns a naive datetime. On a reading
                after a commit, the booking stays counted and no POST may be made.
            sqlalchemy.exc.SQLAlchemyError: On any database failure; the failed
                reservation counts nothing.
        """
        # Fails fast on a clock without an offset; this reading places nothing.
        self._now()
        window = await self._reserve()
        # A booking fails this check only when the clock passed its hour's end less the
        # dispatch bound between the reading under the lock and the one after the commit.
        # A fresh booking is placed by a reading taken no earlier than that one, so each
        # further failure needs wall time to pass another hour's end, and each
        # re-reservation takes one more call of the cap: the loop runs at most once more
        # per elapsed hour, and at most ``cap`` times before a reservation is refused.
        while not window.covers_dispatch_after(after_commit := self._now()):
            _log.warning(
                "google.budget.reservation_expired",
                source=_SOURCE,
                booked=_bucket_key(window.reservation),
                needed=_bucket_key(_hour_start(after_commit + _DISPATCH_BOUND)),
            )
            window = await self._reserve()
        used = window.used + 1
        if used % _LOG_EVERY_CALLS == 0:
            self._log_usage(used)

    async def used_24h(self) -> int:
        """Prune the window and return the calls it holds.

        Persists the pruned ``calls_hourly`` and the recomputed ``calls_24h``, so
        ``/sources`` stays current while no calls are made; the scheduler calls this on
        each wake. The row is written only when pruning changed it. As in :meth:`acquire`,
        the window is evaluated at the clock read once the row lock is held.

        Returns:
            The calls counted in the window. 0 when no Google call was ever metered, in
            which case no row is created: ``sync/mode.py`` creates it with the source's
            real mode, which may be simulated.

        Raises:
            ValueError: When the injected clock returns a naive datetime.
            sqlalchemy.exc.SQLAlchemyError: On any database failure.
        """
        # Fails fast on a clock without an offset; this reading is not the one evaluated.
        self._now()
        async with self._sessions()() as session, session.begin():
            window = await _lock_window(session, self._now, create=False)
            if window is not None and window.dirty:
                await _store(session, window.buckets, window.used)
        return 0 if window is None else window.used

    async def remaining(self) -> int:
        """Return how many calls the window still admits.

        Returns:
            ``max(cap - used_24h(), 0)``.

        Raises:
            ValueError: When the injected clock returns a naive datetime.
            sqlalchemy.exc.SQLAlchemyError: On any database failure.
        """
        return max(self._cap - await self.used_24h(), 0)

    async def _reserve(self) -> _Window:
        """Book one call in a short transaction of its own, or refuse it.

        Returns:
            The window the call was admitted in, evaluated at the clock read under the row
            lock; its :attr:`_Window.reservation` is the bucket the call was booked in, and
            ``used`` excludes the call.

        Raises:
            CallBudgetExhausted: When the window already holds ``cap`` calls; nothing is
                booked.
            ValueError: When the clock read under the lock is naive; nothing is booked.
            sqlalchemy.exc.SQLAlchemyError: On any database failure; nothing is booked.
        """
        async with self._sessions()() as session, session.begin():
            window = await _lock_window(session, self._now, create=True)
            refused = window.used >= self._cap
            if refused:
                if window.dirty:
                    await _store(session, window.buckets, window.used)
            else:
                await _store(session, window.with_call(), window.used + 1)
        # Raised and logged only after the commit, so the pruned counters persist.
        if refused:
            raise self._exhausted(window)
        return window

    def _now(self) -> datetime:
        """Read the clock as an aware UTC instant.

        Raises:
            ValueError: When the clock returns a naive datetime, whose hour bucket would
                depend on the host's time zone.
        """
        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("the call-budget clock must return a timezone-aware datetime")
        return instant.astimezone(UTC)

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        """Return the injected session factory, or the process-wide one."""
        return self._sessionmaker if self._sessionmaker is not None else get_sessionmaker()

    def _log_usage(self, used: int) -> None:
        """Log Google call-budget usage: calls used, the cap and the calls remaining."""
        _log.info(
            "google.budget",
            source=_SOURCE,
            used=used,
            cap=self._cap,
            remaining=max(self._cap - used, 0),
        )

    def _exhausted(self, window: _Window) -> CallBudgetExhausted:
        """Log a refusal and build the error that reports it.

        Args:
            window: The full window that refused the call.

        Returns:
            The :class:`CallBudgetExhausted` to raise.
        """
        retry_after = window.seconds_until_free(self._cap)
        _log.warning(
            "google.budget.exhausted",
            source=_SOURCE,
            used=window.used,
            cap=self._cap,
            retry_after=retry_after,
        )
        self._log_usage(window.used)
        return CallBudgetExhausted(
            f"Google call budget exhausted: {window.used} of {self._cap} calls in the last 24 h",
            source=_SOURCE,
            endpoint=BUDGET_ENDPOINT,
            code=_EXHAUSTED_CODE,
            retry_after=retry_after,
        )
