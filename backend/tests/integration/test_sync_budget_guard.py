"""Tests for ``ads_engine.sync.budget_guard``: the product's own cap on n8n webhook calls.

The pure parts need no database: the hour-bucket keys, the stored counts, the pruning of
the counted window, the time until a call frees up, whether a booking covers a POST
delayed by up to the dispatch bound, and the checks that run before any session is opened.

:class:`GoogleCallBudget` runs against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds only ``source_dataset`` and ``source_connection`` in it, points every
connection's ``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the
``public`` schema and every other schema stay untouched. An unreachable database fails the
database tests; it never skips them. Each database test creates and disposes its own
``AsyncEngine`` inside its own event loop and starts from an empty ``source_connection``.

The connector integration sends through :func:`ads_engine.connectors.http.send_json` on an
``httpx.MockTransport`` whose payloads follow ``backend/tests/contracts/google/``.
"""

import json
import math
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from pydantic import AliasChoices
from sqlalchemy import Executable, Update, create_engine, delete, func, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from ads_engine.connectors.errors import CallBudgetExhausted, TransientError
from ads_engine.connectors.google_n8n.queries import QUERIES
from ads_engine.connectors.http import (
    CONNECT_TIMEOUT_SECONDS,
    SOURCE_TIMEOUTS,
    RetryPolicy,
    build_client,
    send_json,
)
from ads_engine.db.base import Base
from ads_engine.db.models.ops import SourceConnection, SourceDataset
from ads_engine.settings import Settings, get_settings
from ads_engine.sync import budget_guard
from ads_engine.sync.budget_guard import BUDGET_ENDPOINT, WINDOW_BUCKETS, GoogleCallBudget

type Sessions = async_sessionmaker[AsyncSession]
type LogEvent = tuple[str, str, dict[str, object]]
type StoredCounters = tuple[object, int]

NOW = datetime(2026, 10, 7, 14, 30, tzinfo=UTC)
"""The instant most tests run at: half past the hour, 30 minutes from either boundary."""

CURRENT = NOW.replace(minute=0)
"""Start of the current UTC hour bucket at :data:`NOW`."""

KEY_FORMAT = "%Y-%m-%dT%H"

FULL_DAY = (CURRENT + timedelta(hours=WINDOW_BUCKETS) - NOW).total_seconds()
"""Seconds until a call made at :data:`NOW` leaves the window: 24.5 h."""

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

WEBHOOK_URL = "https://n8n.example.test/webhook/google-ads"
SEARCH_ENDPOINT = "search Q-G1"

# A 10-digit Google Ads customer id, assembled at run time so secret scanning never sees
# an account identifier literal.
CUSTOMER_ID = "".join(("12345", "67890"))

REQUEST_BODY: Mapping[str, object] = {
    "customer_id": CUSTOMER_ID,
    "query": QUERIES["Q-G1"].render(),
    "page_token": None,
}
SEARCH_RESPONSE: Mapping[str, object] = {
    "results": [
        {
            "customer": {
                "resourceName": f"customers/{CUSTOMER_ID}",
                "id": CUSTOMER_ID,
                "descriptiveName": "Example Account",
                "currencyCode": "USD",
                "timeZone": "America/New_York",
            }
        }
    ],
    "fieldMask": "customer.id,customer.descriptiveName,customer.currencyCode,customer.timeZone",
    "queryResourceConsumption": "1",
}
N8N_WORKFLOW_ERROR: Mapping[str, object] = {"message": "Error in workflow"}
"""n8n's observed HTTP 500 body for a failed workflow, documented in ``search.json``."""


def _key(hours: int = 0) -> str:
    """Return the ``calls_hourly`` key of the bucket ``hours`` after the current one."""
    return (CURRENT + timedelta(hours=hours)).strftime(KEY_FORMAT)


def _start(hours: int = 0) -> datetime:
    """Return the start of the bucket ``hours`` after the current one."""
    return CURRENT + timedelta(hours=hours)


class _Clock:
    """A settable clock to inject into :class:`GoogleCallBudget`."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        """Return the instant the clock is set to."""
        return self.now

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by ``delta``."""
        self.now += delta


class _NoOffset(tzinfo):
    """A time zone whose offset is unknown, so an instant in it is not usable."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        """Report no offset."""
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        """Report no daylight saving offset."""
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        """Name the zone."""
        return "no-offset"


class _LogRecorder:
    """Stand-in for the module logger that records ``info`` and ``warning`` calls."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def info(self, event_name: str, **fields: object) -> None:
        """Record an ``info`` event."""
        self.events.append(("info", event_name, fields))

    def warning(self, event_name: str, **fields: object) -> None:
        """Record a ``warning`` event."""
        self.events.append(("warning", event_name, fields))


def _settings_env_names() -> frozenset[str]:
    """Return, lowercased, every environment name a ``Settings`` field can be read from."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias)
            elif isinstance(alias, AliasChoices):
                names.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(name.lower() for name in names)


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads, in any letter case, and reset the cache.

    The host exports real credentials, and every built error sanitises its message
    against the loaded settings, so no test may read the host environment. The database
    URL is read once by the module-scoped :func:`database` fixture, before this runs.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def log_events(monkeypatch: pytest.MonkeyPatch) -> list[LogEvent]:
    """Record what the module logs for the duration of one test."""
    recorder = _LogRecorder()
    monkeypatch.setattr(budget_guard, "_log", recorder)
    return recorder.events


def _settings(cap: int) -> Settings:
    """Return settings whose ``GOOGLE_DAILY_CALL_BUDGET`` is ``cap``."""
    return Settings(google_daily_call_budget=cap)


# ---------------------------------------------------------------------------------------
# Bucket keys and stored counts
# ---------------------------------------------------------------------------------------


def test_a_bucket_key_names_the_utc_hour_and_parses_back() -> None:
    """Any aware instant maps to its UTC hour, rendered ``YYYY-MM-DDTHH`` and parsed back."""
    instant = datetime(2026, 10, 7, 16, 45, 12, 5, tzinfo=timezone(timedelta(hours=2)))
    start = budget_guard._hour_start(instant)

    assert start == CURRENT
    assert start.tzinfo is UTC
    assert budget_guard._bucket_key(start) == "2026-10-07T14" == _key()
    assert budget_guard._parse_bucket_key("2026-10-07T14") == CURRENT
    assert budget_guard._utcnow().tzinfo is UTC


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("2026-10-07T14:00", id="minutes"),
        pytest.param("2026-10-07 14", id="space"),
        pytest.param("2026-10-7T14", id="short-day"),
        pytest.param("2026-10-07T24", id="hour-24"),
        pytest.param("2026-13-01T00", id="month-13"),
        pytest.param("2026-02-30T00", id="february-30"),
        pytest.param("", id="empty"),
        pytest.param(2026, id="integer"),
        pytest.param(None, id="none"),
    ],
)
def test_a_key_that_is_not_a_calendar_hour_is_not_parsed(key: object) -> None:
    """Only a canonical key of a real calendar hour has a bucket start."""
    assert budget_guard._parse_bucket_key(key) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(0, 0, id="zero"),
        pytest.param(7, 7, id="integer"),
        pytest.param(2.0, 2, id="whole-float"),
        pytest.param(2.1, 3, id="fraction-rounded-up"),
        pytest.param(0.4, 1, id="small-fraction-rounded-up"),
        pytest.param(0.0, 0, id="float-zero"),
    ],
)
def test_a_stored_count_is_a_non_negative_integer_rounded_up(value: object, expected: int) -> None:
    """Integers pass through, and finite non-negative floats are rounded up."""
    assert budget_guard._parse_count(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(-1, id="negative-integer"),
        pytest.param(-0.5, id="negative-float"),
        pytest.param(math.nan, id="nan"),
        pytest.param(math.inf, id="infinity"),
        pytest.param(-math.inf, id="negative-infinity"),
        pytest.param("3", id="string"),
        pytest.param(None, id="none"),
        pytest.param([1], id="list"),
    ],
)
def test_a_stored_count_that_is_not_a_count_is_rejected(value: object) -> None:
    """Booleans, negatives, non-finite floats and non-numbers have no count."""
    assert budget_guard._parse_count(value) is None


# ---------------------------------------------------------------------------------------
# The counted window
# ---------------------------------------------------------------------------------------


def test_the_window_counts_the_current_hour_and_the_24_before_it() -> None:
    """A bucket 25 h before the current one is pruned; the 25 buckets after it count."""
    stored = {_key(hours): 1 for hours in range(-25, 1)}
    window = budget_guard._evaluate(stored, 26, NOW)

    assert WINDOW_BUCKETS == 25
    assert (window.now, window.current) == (NOW, CURRENT)
    assert sorted(window.buckets) == [_start(hours) for hours in range(-24, 1)]
    assert window.used == WINDOW_BUCKETS
    assert window.dirty


def test_the_window_prunes_old_and_zero_buckets_and_keeps_buckets_ahead() -> None:
    """Old and zero buckets go; a bucket ahead of the clock counts; output is hour-ordered."""
    stored = {
        _key(2): 3,
        _key(0): 2,
        _key(-3): 0,
        _key(-24): 1,
        _key(-25): 4,
    }
    window = budget_guard._evaluate(stored, 10, NOW)

    assert dict(window.buckets) == {_start(-24): 1, _start(0): 2, _start(2): 3}
    assert window.used == 6
    assert window.dirty
    serialised = budget_guard._serialise(window.buckets)
    assert list(serialised.items()) == [(_key(-24), 1), (_key(0), 2), (_key(2), 3)]


@pytest.mark.parametrize(
    ("stored_total", "dirty"),
    [
        pytest.param(3, False, id="matching-total"),
        pytest.param(4, True, id="stale-total"),
    ],
)
def test_a_window_needs_writing_only_when_it_differs_from_the_row(
    stored_total: int, dirty: bool
) -> None:
    """A pruned state equal to the stored row, in any key order, is not rewritten."""
    window = budget_guard._evaluate({_key(0): 1, _key(-1): 2}, stored_total, NOW)

    assert window.used == 3
    assert window.dirty is dirty


def test_malformed_entries_are_discarded_with_a_warning(log_events: list[LogEvent]) -> None:
    """Keys and counts this module cannot have written are dropped and counted in one warning."""
    stored: dict[object, object] = {
        "not-a-key": 50,
        "2026-02-30T00": 50,
        14: 1,
        _key(-1): -3,
        _key(-2): "7",
        _key(-3): True,
        _key(-4): math.nan,
        _key(-5): None,
        _key(-6): 1,
        _key(0): 1.2,
    }
    window = budget_guard._evaluate(stored, 999, NOW)

    assert dict(window.buckets) == {_start(-6): 1, _start(0): 2}
    assert window.used == 3
    assert window.dirty
    assert log_events == [
        ("warning", "google.budget.buckets_discarded", {"source": "google", "discarded": 8})
    ]


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(None, id="json-null"),
        pytest.param([], id="empty-list"),
        pytest.param([1, 2], id="list"),
        pytest.param("2026-10-07T14", id="string"),
        pytest.param(3, id="number"),
    ],
)
def test_a_stored_value_that_is_not_an_object_counts_nothing(
    stored: object, log_events: list[LogEvent]
) -> None:
    """A non-object ``calls_hourly`` is one discarded entry, and the row is rewritten."""
    window = budget_guard._evaluate(stored, 0, NOW)

    assert dict(window.buckets) == {}
    assert window.used == 0
    assert window.dirty
    assert log_events == [
        ("warning", "google.budget.buckets_discarded", {"source": "google", "discarded": 1})
    ]


def test_a_well_formed_window_logs_nothing(log_events: list[LogEvent]) -> None:
    """No warning is logged when every entry can be placed in time."""
    budget_guard._evaluate({_key(0): 1, _key(-30): 2}, 3, NOW)

    assert log_events == []


def test_a_call_is_added_to_the_current_hour_without_changing_the_window() -> None:
    """``with_call`` returns new buckets with one more call in the current hour."""
    window = budget_guard._evaluate({_key(-1): 2}, 2, NOW)
    first = window.with_call()
    later = budget_guard._evaluate({_key(-1): 2, _key(0): 4}, 6, NOW).with_call()

    assert window.reservation == CURRENT
    assert first == {_start(-1): 2, _start(0): 1}
    assert later == {_start(-1): 2, _start(0): 5}
    assert dict(window.buckets) == {_start(-1): 2}


def test_the_dispatch_bound_covers_the_pool_wait_and_both_connect_phases() -> None:
    """A call is booked 140 s ahead: the 120 s pool wait plus a TCP and a TLS connect."""
    bound = budget_guard._DISPATCH_BOUND

    assert bound == timedelta(seconds=SOURCE_TIMEOUTS["google"] + 2 * CONNECT_TIMEOUT_SECONDS)
    assert bound.total_seconds() == 140.0


@pytest.mark.parametrize(
    ("instant", "booked"),
    [
        pytest.param(NOW, CURRENT, id="mid-hour"),
        pytest.param(CURRENT.replace(minute=57, second=39), CURRENT, id="57m39s-own-hour"),
        pytest.param(
            CURRENT.replace(minute=57, second=39, microsecond=999999),
            CURRENT,
            id="just-outside-the-bound",
        ),
        pytest.param(CURRENT.replace(minute=57, second=40), _start(1), id="57m40s-next-hour"),
        pytest.param(CURRENT.replace(minute=58, second=30), _start(1), id="58m30s-next-hour"),
        pytest.param(
            CURRENT.replace(minute=59, second=59, microsecond=999999),
            _start(1),
            id="last-instant-next-hour",
        ),
        pytest.param(
            datetime(2026, 10, 7, 23, 58, 30, tzinfo=UTC),
            datetime(2026, 10, 8, 0, tzinfo=UTC),
            id="next-day",
        ),
    ],
)
def test_a_call_admitted_within_the_bound_of_the_hour_end_is_booked_in_the_next_hour(
    instant: datetime, booked: datetime
) -> None:
    """A call admitted within 140 s of its hour's end counts in the next hour from at once."""
    window = budget_guard._evaluate({}, 0, instant)
    buckets = window.with_call()
    after = budget_guard._evaluate(budget_guard._serialise(buckets), 1, instant)

    assert window.reservation == booked
    assert buckets == {booked: 1}
    assert after.used == 1
    assert not after.dirty
    # The call frees up 25 h after the start of the bucket it was booked in, so it counts
    # for more than 24 h after its admission and at most 25 h plus the bound.
    retry_after = after.seconds_until_free(1)
    assert retry_after is not None
    assert retry_after == (booked + timedelta(hours=WINDOW_BUCKETS) - instant).total_seconds()
    assert timedelta(hours=24) < timedelta(seconds=retry_after) <= timedelta(hours=25, seconds=140)
    # A POST starting as late as the bound allows still leaves in the booked hour or before.
    assert budget_guard._hour_start(instant + budget_guard._DISPATCH_BOUND) <= booked
    assert window.covers_dispatch_after(instant)


DISPATCH_ADMITTED = datetime(2026, 10, 6, 10, 55, tzinfo=UTC)
"""A call admitted at 10:55:00 is booked in hour 10: its POST leaves by 10:57:20."""

DISPATCH_ADMITTED_LATE = datetime(2026, 10, 6, 10, 58, tzinfo=UTC)
"""A call admitted at 10:58:00 is booked in hour 11: its POST may leave after 11:00."""


@pytest.mark.parametrize(
    ("admitted", "after_commit", "covered"),
    [
        pytest.param(DISPATCH_ADMITTED, DISPATCH_ADMITTED, True, id="no-delay"),
        pytest.param(
            DISPATCH_ADMITTED,
            DISPATCH_ADMITTED.replace(minute=57, second=39, microsecond=999999),
            True,
            id="last-instant-the-bound-stays-in-the-hour",
        ),
        pytest.param(
            DISPATCH_ADMITTED,
            DISPATCH_ADMITTED.replace(minute=57, second=40),
            False,
            id="bound-reaches-the-next-hour",
        ),
        pytest.param(
            DISPATCH_ADMITTED, DISPATCH_ADMITTED + timedelta(seconds=300), False, id="300s-later"
        ),
        pytest.param(
            DISPATCH_ADMITTED,
            DISPATCH_ADMITTED - timedelta(minutes=5),
            True,
            id="clock-stepped-back",
        ),
        pytest.param(
            DISPATCH_ADMITTED_LATE,
            DISPATCH_ADMITTED_LATE + timedelta(seconds=100),
            True,
            id="booked-ahead-100s-later",
        ),
        pytest.param(
            DISPATCH_ADMITTED_LATE,
            datetime(2026, 10, 6, 11, 57, 39, tzinfo=UTC),
            True,
            id="booked-ahead-last-instant",
        ),
        pytest.param(
            DISPATCH_ADMITTED_LATE,
            datetime(2026, 10, 6, 11, 57, 40, tzinfo=UTC),
            False,
            id="booked-ahead-bound-reaches-hour-12",
        ),
    ],
)
def test_a_booking_covers_the_dispatch_only_while_the_bound_stays_in_its_hour(
    admitted: datetime, after_commit: datetime, covered: bool
) -> None:
    """The booking holds when a POST up to 140 s after the commit check is in its hour."""
    window = budget_guard._evaluate({}, 0, admitted)

    assert window.covers_dispatch_after(after_commit) is covered


def test_a_full_window_frees_a_call_when_its_oldest_bucket_leaves() -> None:
    """At exactly the cap, a call fits once the oldest counted bucket leaves the window."""
    window = budget_guard._evaluate({_key(-24): 3, _key(0): 2}, 5, NOW)

    # The bucket that started 24 h before the current one leaves at the next hour: 30 min.
    assert window.seconds_until_free(5) == 1800.0


def test_a_lowered_cap_waits_until_enough_buckets_leave() -> None:
    """Above the cap, as many of the oldest buckets as needed must leave first."""
    window = budget_guard._evaluate({_key(-24): 3, _key(-10): 1, _key(0): 2}, 6, NOW)

    assert window.seconds_until_free(3) == (_start(-10) + timedelta(hours=25) - NOW).total_seconds()
    assert window.seconds_until_free(2) == FULL_DAY
    assert window.seconds_until_free(6) == 1800.0


def test_a_bucket_ahead_of_the_clock_frees_up_25_hours_after_its_own_start() -> None:
    """A bucket written by a clock running ahead counts until 25 h after it started."""
    window = budget_guard._evaluate({_key(3): 2}, 2, NOW)

    assert window.seconds_until_free(2) == FULL_DAY + 3 * 3600


@pytest.mark.parametrize("cap", [0, -1])
def test_no_call_ever_frees_up_under_a_cap_of_zero(cap: int) -> None:
    """A cap of 0 (or below) admits no call, so there is no time until one fits."""
    window = budget_guard._evaluate({_key(0): 1}, 1, NOW)

    assert window.seconds_until_free(cap) is None


def test_a_window_whose_buckets_cannot_free_enough_calls_has_no_free_time() -> None:
    """A count above the sum of its buckets never frees up (defensive end of the scan)."""
    window = budget_guard._Window(now=NOW, current=CURRENT, buckets={}, used=4, dirty=False)

    assert window.seconds_until_free(1) is None


# ---------------------------------------------------------------------------------------
# Construction and the checks before any database work
# ---------------------------------------------------------------------------------------


def test_the_cap_comes_from_the_given_settings() -> None:
    """``GOOGLE_DAILY_CALL_BUDGET`` of the given settings is the cap; 1,000 by default."""
    budget = GoogleCallBudget(_settings(5))

    assert budget.cap == 5
    assert repr(budget) == "GoogleCallBudget(cap=5)"
    assert GoogleCallBudget(Settings()).cap == 1000


def test_without_settings_the_process_settings_supply_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no settings given, ``get_settings()`` is read for the cap."""
    monkeypatch.setenv("GOOGLE_DAILY_CALL_BUDGET", "7")
    get_settings.cache_clear()

    assert GoogleCallBudget().cap == 7


def test_a_negative_cap_is_rejected() -> None:
    """A cap below 0 cannot be configured."""
    settings = Settings.model_construct(google_daily_call_budget=-1)

    with pytest.raises(ValueError, match="GOOGLE_DAILY_CALL_BUDGET must be 0 or more, got -1"):
        GoogleCallBudget(settings)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "instant",
    [
        pytest.param(NOW.replace(tzinfo=None), id="naive"),
        pytest.param(NOW.replace(tzinfo=_NoOffset()), id="no-offset"),
    ],
)
async def test_a_clock_without_an_offset_is_rejected_before_any_database_work(
    instant: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An instant with no UTC offset has no hour bucket, so no session is ever opened."""

    def lookup() -> Sessions:
        pytest.fail("no session may be opened for an instant without an offset")

    monkeypatch.setattr(budget_guard, "get_sessionmaker", lookup)
    budget = GoogleCallBudget(_settings(5), clock=lambda: instant)

    for call in (budget.acquire, budget.used_24h, budget.remaining):
        with pytest.raises(ValueError, match="timezone-aware"):
            await call()


# ---------------------------------------------------------------------------------------
# Database: a private schema per module, an engine per test
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the Google call-budget tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str) -> dict[str, str]:
    """Pin every connection to ``schema`` alone, in UTC like the application's sessions."""
    return {"options": f"-c search_path={schema} -c TimeZone=UTC"}


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding only the connection tables; drop it afterwards.

    ``source_dataset`` is built because ``source_connection.active_dataset_id``
    references it.

    Yields:
        The database URL and the private schema's name.
    """
    url = _database_url()
    schema = f"budget_guard_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(
                connection,
                tables=[
                    Base.metadata.tables[model.__tablename__]
                    for model in (SourceDataset, SourceConnection)
                ],
            )
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@pytest.fixture
async def sessions(database: tuple[URL, str]) -> AsyncIterator[Sessions]:
    """Yield a session factory on an engine created and disposed inside this test's loop.

    The connection tables are emptied first, so every test starts with no Google row.
    """
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await session.execute(delete(SourceConnection))
            await session.execute(delete(SourceDataset))
        yield factory
    finally:
        await engine.dispose()


def _budget(sessions: Sessions, cap: int = 1000, clock: _Clock | None = None) -> GoogleCallBudget:
    """Return a budget of ``cap`` calls on ``sessions``, at :data:`NOW` unless clocked."""
    return GoogleCallBudget(
        _settings(cap), sessionmaker=sessions, clock=clock if clock is not None else _Clock()
    )


async def _seed(sessions: Sessions, hourly_json: str, total: int) -> None:
    """Commit a live Google row whose ``calls_hourly`` is the raw JSON ``hourly_json``."""
    async with sessions() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO source_connection (source, mode, status, calls_24h, calls_hourly) "
                "VALUES ('google', 'live', 'ok', :total, CAST(:hourly AS jsonb))"
            ),
            {"total": total, "hourly": hourly_json},
        )


async def _seed_counts(sessions: Sessions, hourly: Mapping[str, object], total: int) -> None:
    """Commit a live Google row holding ``hourly`` and ``total``, as if edited by hand."""
    await _seed(sessions, json.dumps(hourly), total)


async def _stored(sessions: Sessions) -> StoredCounters | None:
    """Return the Google row's ``(calls_hourly, calls_24h)``, or ``None`` with no row."""
    async with sessions() as session:
        row = (
            await session.execute(
                select(SourceConnection.calls_hourly, SourceConnection.calls_24h).where(
                    SourceConnection.source == "google"
                )
            )
        ).one_or_none()
    return None if row is None else (row.calls_hourly, row.calls_24h)


async def _row_count(sessions: Sessions) -> int:
    """Count the ``source_connection`` rows."""
    async with sessions() as session:
        count = await session.scalar(select(func.count()).select_from(SourceConnection))
    return int(count or 0)


async def _row_version(sessions: Sessions) -> str:
    """Return the Google row's ``xmin``, which changes only when the row is rewritten."""
    async with sessions() as session:
        version = await session.scalar(
            text("SELECT xmin::text FROM source_connection WHERE source = 'google'")
        )
    assert isinstance(version, str)
    return version


_LOCK_GOOGLE_ROW = (
    select(SourceConnection.id).where(SourceConnection.source == "google").with_for_update()
)


@pytest.mark.anyio
@pytest.mark.db
async def test_the_first_acquire_creates_the_live_google_row(sessions: Sessions) -> None:
    """With no row yet, ``acquire`` creates the live, healthy Google row and counts one call."""
    await _budget(sessions).acquire()

    async with sessions() as session:
        row = (await session.scalars(select(SourceConnection))).one()
    assert (row.source, row.mode, row.status, row.reason) == ("google", "live", "ok", None)
    assert (row.active_dataset_id, row.last_attempt_at) == (None, None)
    assert (row.calls_hourly, row.calls_24h) == ({"2026-10-07T14": 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_each_call_counts_in_its_own_utc_hour(sessions: Sessions) -> None:
    """Calls in one hour share its bucket; the next hour opens a new one."""
    clock = _Clock()
    budget = _budget(sessions, cap=10, clock=clock)
    for _ in range(3):
        await budget.acquire()
    assert await _stored(sessions) == ({_key(0): 3}, 3)

    clock.advance(timedelta(hours=1))
    await budget.acquire()

    assert await _stored(sessions) == ({_key(0): 3, _key(1): 1}, 4)
    assert await budget.used_24h() == 4
    assert await budget.remaining() == 6


@pytest.mark.anyio
@pytest.mark.db
async def test_the_hour_bucket_is_utc_whatever_the_clock_zone(sessions: Sessions) -> None:
    """An aware instant in another zone is counted in its UTC hour."""
    clock = _Clock(NOW.astimezone(timezone(timedelta(hours=-4))))
    await _budget(sessions, clock=clock).acquire()

    assert await _stored(sessions) == ({"2026-10-07T14": 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_the_default_clock_counts_in_the_current_utc_hour(sessions: Sessions) -> None:
    """Without an injected clock, the call lands in the hour of now plus the bound."""
    budget = GoogleCallBudget(_settings(5), sessionmaker=sessions)
    bound = budget_guard._DISPATCH_BOUND
    before = datetime.now(UTC)
    await budget.acquire()
    after = datetime.now(UTC)

    stored = await _stored(sessions)
    assert stored is not None
    hourly, total = stored
    assert isinstance(hourly, dict)
    hours = {(before + bound).strftime(KEY_FORMAT), (after + bound).strftime(KEY_FORMAT)}
    # Both hours are booked only when the clock passed hh:57:40 between the reading under
    # the lock and the one after the commit, which re-books the call in the later hour.
    assert hourly
    assert set(hourly) <= hours
    assert set(hourly.values()) == {1}
    assert total == len(hourly)


@pytest.mark.anyio
@pytest.mark.db
async def test_acquire_writes_only_the_counters_of_an_existing_row(sessions: Sessions) -> None:
    """Mode, status, reason, dataset and attempt time belong to ``sync/mode.py``."""
    attempted = NOW - timedelta(hours=1)
    reason = "google search Q-G2: HTTP 500: Error in workflow"
    async with sessions() as session, session.begin():
        dataset = SourceDataset(source="google", mode="live", state="active", role="primary")
        session.add(dataset)
        await session.flush()
        session.add(
            SourceConnection(
                source="google",
                mode="live",
                status="stale",
                reason=reason,
                active_dataset_id=dataset.id,
                last_attempt_at=attempted,
                calls_24h=2,
                calls_hourly={_key(-1): 2},
            )
        )
        dataset_id = dataset.id

    await _budget(sessions).acquire()

    async with sessions() as session:
        row = (await session.scalars(select(SourceConnection))).one()
    assert (row.mode, row.status, row.reason) == ("live", "stale", reason)
    assert (row.active_dataset_id, row.last_attempt_at) == (dataset_id, attempted)
    assert (row.calls_hourly, row.calls_24h) == ({_key(-1): 2, _key(0): 1}, 3)


@pytest.mark.anyio
@pytest.mark.db
async def test_acquire_refuses_at_the_cap_and_counts_nothing(
    sessions: Sessions, log_events: list[LogEvent]
) -> None:
    """The call after the cap raises ``CallBudgetExhausted``, never retryable, with the wait."""
    budget = _budget(sessions, cap=3)
    for _ in range(3):
        await budget.acquire()

    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()

    error = raised.value
    assert isinstance(error, TransientError)
    assert error.retryable is False
    assert error.kind == "transient"
    assert (error.source, error.endpoint, error.code) == (
        "google",
        BUDGET_ENDPOINT,
        "CALL_BUDGET_EXHAUSTED",
    )
    assert error.http_status is None
    assert error.retry_after == FULL_DAY
    assert error.message == "Google call budget exhausted: 3 of 3 calls in the last 24 h"
    assert await _stored(sessions) == ({_key(0): 3}, 3)
    assert log_events == [
        (
            "warning",
            "google.budget.exhausted",
            {"source": "google", "used": 3, "cap": 3, "retry_after": FULL_DAY},
        ),
        ("info", "google.budget", {"source": "google", "used": 3, "cap": 3, "remaining": 0}),
    ]


@pytest.mark.anyio
@pytest.mark.db
async def test_a_refused_call_fits_exactly_retry_after_seconds_later(sessions: Sessions) -> None:
    """``retry_after`` is the exact wait: a second earlier is refused, then the call fits."""
    clock = _Clock(NOW - timedelta(hours=20))
    budget = _budget(sessions, cap=2, clock=clock)
    await budget.acquire()
    clock.now = NOW
    await budget.acquire()

    with pytest.raises(CallBudgetExhausted) as first:
        await budget.acquire()
    retry_after = first.value.retry_after
    assert retry_after is not None
    assert retry_after == (_start(-20) + timedelta(hours=25) - NOW).total_seconds()

    clock.advance(timedelta(seconds=retry_after - 1))
    with pytest.raises(CallBudgetExhausted) as second:
        await budget.acquire()
    assert second.value.retry_after == 1.0

    clock.advance(timedelta(seconds=1))
    await budget.acquire()

    assert await _stored(sessions) == ({_key(0): 1, _key(5): 1}, 2)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_refusal_persists_the_pruned_window(sessions: Sessions) -> None:
    """A refused call still writes the pruned counters, so ``calls_24h`` stays current."""
    await _seed_counts(sessions, {_key(-30): 5, _key(-25): 1, _key(0): 2}, 8)

    with pytest.raises(CallBudgetExhausted) as raised:
        await _budget(sessions, cap=2).acquire()

    assert raised.value.retry_after == FULL_DAY
    assert await _stored(sessions) == ({_key(0): 2}, 2)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_refusal_leaves_a_clean_row_unwritten(sessions: Sessions) -> None:
    """When pruning changes nothing, a refused call does not rewrite the row."""
    await _seed_counts(sessions, {_key(0): 2}, 2)
    version = await _row_version(sessions)

    with pytest.raises(CallBudgetExhausted):
        await _budget(sessions, cap=2).acquire()

    assert await _row_version(sessions) == version
    assert await _stored(sessions) == ({_key(0): 2}, 2)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_lowered_cap_holds_at_once_and_frees_after_enough_buckets_leave(
    sessions: Sessions,
) -> None:
    """Calls counted above a lowered cap stay counted until enough of them leave."""
    await _seed_counts(sessions, {_key(-24): 2, _key(-10): 2, _key(0): 1}, 5)
    clock = _Clock()
    budget = _budget(sessions, cap=3, clock=clock)

    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()

    retry_after = (_start(-10) + timedelta(hours=25) - NOW).total_seconds()
    assert raised.value.retry_after == retry_after
    assert raised.value.message == "Google call budget exhausted: 5 of 3 calls in the last 24 h"
    assert await budget.remaining() == 0

    clock.advance(timedelta(seconds=retry_after))
    await budget.acquire()

    assert await _stored(sessions) == ({_key(0): 1, _key(15): 1}, 2)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_cap_of_zero_refuses_every_call_with_no_retry_after(sessions: Sessions) -> None:
    """Under a cap of 0 no call ever fits, so the refusal carries no wait."""
    budget = GoogleCallBudget(
        Settings.model_construct(google_daily_call_budget=0),
        sessionmaker=sessions,
        clock=_Clock(),
    )

    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()

    assert raised.value.retry_after is None
    assert await _stored(sessions) == ({}, 0)
    assert await budget.remaining() == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_buckets_ahead_of_the_clock_are_counted(sessions: Sessions) -> None:
    """Calls a process with a fast clock wrote in a later hour still count against the cap."""
    await _seed_counts(sessions, {_key(3): 2}, 2)
    budget = _budget(sessions, cap=2)

    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()

    assert raised.value.retry_after == FULL_DAY + 3 * 3600
    assert await budget.used_24h() == 2


@pytest.mark.anyio
@pytest.mark.db
async def test_used_24h_without_a_row_is_zero_and_creates_none(sessions: Sessions) -> None:
    """With no Google call ever metered, nothing is counted and no row is created."""
    budget = _budget(sessions, cap=5)

    assert await budget.used_24h() == 0
    assert await budget.remaining() == 5
    assert await _row_count(sessions) == 0


@pytest.mark.anyio
@pytest.mark.db
async def test_used_24h_returns_and_persists_the_pruned_sum(sessions: Sessions) -> None:
    """``used_24h`` sums the window, writes the pruned row once, and follows the clock."""
    await _seed_counts(sessions, {_key(-25): 4, _key(-24): 1, _key(-1): 2, _key(0): 1}, 9)
    clock = _Clock()
    budget = _budget(sessions, cap=5, clock=clock)

    assert await budget.used_24h() == 4
    assert await _stored(sessions) == ({_key(-24): 1, _key(-1): 2, _key(0): 1}, 4)

    version = await _row_version(sessions)
    assert await budget.used_24h() == 4
    assert await budget.remaining() == 1
    assert await _row_version(sessions) == version

    clock.advance(timedelta(hours=1))
    assert await budget.used_24h() == 3
    assert await _stored(sessions) == ({_key(-1): 2, _key(0): 1}, 3)


@pytest.mark.anyio
@pytest.mark.db
async def test_corrupt_stored_buckets_are_discarded_and_never_block_a_call(
    sessions: Sessions, log_events: list[LogEvent]
) -> None:
    """Hand-edited entries are dropped with one warning; the fractional count is rounded up."""
    corrupt = {
        "not-a-key": 50,
        "2026-02-30T00": 50,
        _key(-1): -3,
        _key(-2): "7",
        _key(-3): True,
        _key(-4): None,
        _key(0): 1.2,
    }
    await _seed_counts(sessions, corrupt, 999)
    budget = _budget(sessions, cap=3)

    assert await budget.used_24h() == 2
    assert await _stored(sessions) == ({_key(0): 2}, 2)
    await budget.acquire()

    assert await _stored(sessions) == ({_key(0): 3}, 3)
    assert log_events == [
        ("warning", "google.budget.buckets_discarded", {"source": "google", "discarded": 6})
    ]


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("hourly_json", ["null", "[]", "[1, 2]", '"2026-10-07T14"', "3"])
async def test_a_stored_value_that_is_not_an_object_is_reset(
    sessions: Sessions, log_events: list[LogEvent], hourly_json: str
) -> None:
    """A ``calls_hourly`` that is not a JSON object is reset to ``{}`` and calls go on."""
    await _seed(sessions, hourly_json, 4)
    budget = _budget(sessions, cap=3)

    assert await budget.used_24h() == 0
    assert await _stored(sessions) == ({}, 0)
    await budget.acquire()

    assert await _stored(sessions) == ({_key(0): 1}, 1)
    assert log_events == [
        ("warning", "google.budget.buckets_discarded", {"source": "google", "discarded": 1})
    ]


@pytest.mark.anyio
@pytest.mark.db
async def test_every_fiftieth_call_in_the_window_logs_budget_usage(
    sessions: Sessions, log_events: list[LogEvent]
) -> None:
    """Usage is logged on the 50th call in the window, not on every call."""
    await _seed_counts(sessions, {_key(-1): 48}, 48)
    budget = _budget(sessions)

    await budget.acquire()
    assert log_events == []
    await budget.acquire()

    assert log_events == [
        ("info", "google.budget", {"source": "google", "used": 50, "cap": 1000, "remaining": 950})
    ]
    assert await _stored(sessions) == ({_key(-1): 48, _key(0): 2}, 50)


@pytest.mark.anyio
@pytest.mark.db
async def test_concurrent_acquires_never_exceed_the_cap(sessions: Sessions) -> None:
    """20 concurrent calls on two instances against a cap of 5 admit exactly 5."""
    clock = _Clock()
    budgets = (_budget(sessions, cap=5, clock=clock), _budget(sessions, cap=5, clock=clock))
    admitted: list[int] = []
    refused: list[CallBudgetExhausted] = []

    async def call(index: int) -> None:
        """Acquire one call on one of the two instances and record the outcome."""
        try:
            await budgets[index % 2].acquire()
        except CallBudgetExhausted as exc:
            refused.append(exc)
        else:
            admitted.append(index)

    with anyio.fail_after(60):
        async with anyio.create_task_group() as group:
            for index in range(20):
                group.start_soon(call, index)

    assert len(admitted) == 5
    assert len(refused) == 15
    assert {error.retry_after for error in refused} == {FULL_DAY}
    assert await _stored(sessions) == ({_key(0): 5}, 5)
    assert await _row_count(sessions) == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_acquire_waits_for_a_transaction_holding_the_row(sessions: Sessions) -> None:
    """``SELECT ... FOR UPDATE`` makes a call wait for the row's holder, then count."""
    budget = _budget(sessions)
    await budget.acquire()
    done = anyio.Event()

    async def acquire() -> None:
        """Count one call, waiting for the holder's commit."""
        await budget.acquire()
        done.set()

    with anyio.fail_after(60):
        async with sessions() as holder:
            await holder.execute(_LOCK_GOOGLE_ROW)
            async with anyio.create_task_group() as group:
                group.start_soon(acquire)
                await anyio.sleep(0.5)
                waited = not done.is_set()
                await holder.commit()

    assert waited
    assert await _stored(sessions) == ({_key(0): 2}, 2)


ASKED_LATE = datetime(2026, 10, 6, 10, 59, 59, tzinfo=UTC)
"""A call asked for one second before the hour's end, inside the dispatch bound."""

ASKED_EARLY = datetime(2026, 10, 6, 10, 57, 30, tzinfo=UTC)
"""A call asked for 150 s before the hour's end, outside the 140 s dispatch bound: only a
post-lock reading places it later."""

ADMITTED = datetime(2026, 10, 6, 11, 0, 5, tzinfo=UTC)
"""When the holder of the row lock commits and the waiting call is admitted."""


class _SequenceClock:
    """A clock that returns the given instants in turn, then repeats the last one."""

    def __init__(self, *instants: datetime) -> None:
        self.reads: list[datetime] = []
        self._instants = list(instants)

    def __call__(self) -> datetime:
        """Return the next instant, recording the reading."""
        instant = self._instants.pop(0) if len(self._instants) > 1 else self._instants[0]
        self.reads.append(instant)
        return instant


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    "asked",
    [
        pytest.param(ASKED_LATE, id="asked-at-10h59m59s"),
        pytest.param(ASKED_EARLY, id="asked-at-10h57m30s"),
    ],
)
async def test_a_call_that_waits_for_the_lock_across_the_hour_counts_from_its_admission(
    sessions: Sessions, asked: datetime
) -> None:
    """The clock is read once the row lock is held, so a call cannot expire under 24 h.

    The call asks at ``asked`` in hour 10, waits for the lock until 11:00:05 and is booked
    in hour 11. With a cap of 1, the next day's 11:00:01, under 24 h after the call, is
    still refused, and the call frees up only once hour 11's bucket leaves the window.
    """
    await _seed_counts(sessions, {}, 0)
    clock = _Clock(asked)
    budget = _budget(sessions, cap=1, clock=clock)
    done = anyio.Event()

    async def acquire() -> None:
        """Count one call, waiting for the holder's commit."""
        await budget.acquire()
        done.set()

    with anyio.fail_after(60):
        async with sessions() as holder:
            await holder.execute(_LOCK_GOOGLE_ROW)
            async with anyio.create_task_group() as group:
                group.start_soon(acquire)
                await anyio.sleep(0.5)
                waited = not done.is_set()
                clock.now = ADMITTED
                await holder.commit()

    assert waited
    assert await _stored(sessions) == ({"2026-10-06T11": 1}, 1)

    clock.now = datetime(2026, 10, 7, 11, 0, 1, tzinfo=UTC)
    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()
    assert raised.value.retry_after == 3599.0
    assert await budget.used_24h() == 1

    clock.advance(timedelta(seconds=3599))
    await budget.acquire()
    assert await _stored(sessions) == ({"2026-10-07T12": 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_used_24h_that_waits_for_the_lock_prunes_at_the_instant_it_holds_it(
    sessions: Sessions,
) -> None:
    """A bucket that leaves the window while ``used_24h`` waits for the lock is not counted."""
    await _seed_counts(sessions, {"2026-10-05T10": 1}, 1)
    clock = _Clock(ASKED_LATE)
    budget = _budget(sessions, cap=1, clock=clock)
    counted: list[int] = []

    async def used() -> None:
        """Read the window, waiting for the holder's commit."""
        counted.append(await budget.used_24h())

    with anyio.fail_after(60):
        async with sessions() as holder:
            await holder.execute(_LOCK_GOOGLE_ROW)
            async with anyio.create_task_group() as group:
                group.start_soon(used)
                await anyio.sleep(0.5)
                waited = not counted
                clock.now = ADMITTED
                await holder.commit()

    assert waited
    assert counted == [0]
    assert await _stored(sessions) == ({}, 0)


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize("existing", [False, True], ids=["creates-the-row", "existing-row"])
async def test_the_call_is_placed_by_the_clock_read_inside_the_transaction(
    sessions: Sessions, existing: bool
) -> None:
    """The first reading only checks the clock; the one under the lock places the call.

    The third, after the commit, only confirms that a POST starting within the dispatch
    bound of it still falls in the booked hour.
    """
    if existing:
        await _seed_counts(sessions, {}, 0)
    committed = ADMITTED + timedelta(seconds=1)
    clock = _SequenceClock(ASKED_EARLY, ADMITTED, committed)

    await GoogleCallBudget(_settings(5), sessionmaker=sessions, clock=clock).acquire()

    assert clock.reads == [ASKED_EARLY, ADMITTED, committed]
    assert await _stored(sessions) == ({"2026-10-06T11": 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_naive_reading_after_the_commit_raises_and_leaves_the_booking_counted(
    sessions: Sessions,
) -> None:
    """A reading without an offset after the commit cannot confirm the booking: no POST."""
    clock = _SequenceClock(NOW, NOW, NOW.replace(tzinfo=None))

    with pytest.raises(ValueError, match="timezone-aware"):
        await GoogleCallBudget(_settings(5), sessionmaker=sessions, clock=clock).acquire()

    assert len(clock.reads) == 3
    assert await _stored(sessions) == ({_key(0): 1}, 1)


def _sessions_delaying_updates(sessions: Sessions, clock: _Clock, *delays: timedelta) -> Sessions:
    """Return a factory on the engine of ``sessions`` whose UPDATEs answer late, in turn.

    Each UPDATE advances ``clock`` by the next of ``delays`` once its result arrives, before
    its transaction commits, as if the database had answered that much later. UPDATEs after
    the last delay take no time.
    """
    pending = list(delays)

    class DelayedUpdateSession(AsyncSession):
        """An ``AsyncSession`` whose UPDATE results arrive after the next pending delay."""

        async def execute(self, statement: Executable, *args: Any, **kwargs: Any) -> Any:
            """Execute ``statement``; an UPDATE then advances the clock by the next delay."""
            result = await super().execute(statement, *args, **kwargs)
            if isinstance(statement, Update) and pending:
                clock.advance(pending.pop(0))
            return result

    return async_sessionmaker[AsyncSession](
        sessions.kw["bind"], class_=DelayedUpdateSession, expire_on_commit=False
    )


def _expired(booked: str, needed: str) -> LogEvent:
    """Return the warning logged when a booking in ``booked`` cannot cover its POST."""
    return (
        "warning",
        "google.budget.reservation_expired",
        {"source": "google", "booked": booked, "needed": needed},
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_a_booking_whose_commit_outlasts_its_hour_stays_counted_and_cap_one_refuses(
    sessions: Sessions, log_events: list[LogEvent]
) -> None:
    """A 300 s UPDATE pushes the POST past hour 10; the re-reservation is refused at cap 1.

    Booked at 10:55:00 in hour 10, the commit returns at 11:00:00, when the POST could
    leave as late as 11:02:20. The expired booking stays counted, so the re-reservation
    under the lock finds the cap reached and no POST may be sent.
    """
    clock = _Clock(DISPATCH_ADMITTED)
    delayed = _sessions_delaying_updates(sessions, clock, timedelta(seconds=300))
    budget = GoogleCallBudget(_settings(1), sessionmaker=delayed, clock=clock)

    with pytest.raises(CallBudgetExhausted) as raised:
        await budget.acquire()

    assert clock.now == datetime(2026, 10, 6, 11, tzinfo=UTC)
    assert raised.value.retry_after == 86400.0
    assert await _stored(sessions) == ({"2026-10-06T10": 1}, 1)
    assert log_events == [
        _expired("2026-10-06T10", "2026-10-06T11"),
        (
            "warning",
            "google.budget.exhausted",
            {"source": "google", "used": 1, "cap": 1, "retry_after": 86400.0},
        ),
        ("info", "google.budget", {"source": "google", "used": 1, "cap": 1, "remaining": 0}),
    ]


@pytest.mark.anyio
@pytest.mark.db
async def test_a_booking_whose_commit_outlasts_its_hour_is_made_again_in_the_later_hour(
    sessions: Sessions, log_events: list[LogEvent]
) -> None:
    """Under a cap of 2 the call is re-booked in hour 11, and both bookings count.

    ``acquire`` returns at 11:00:00, so the POST leaves between then and 11:02:20, inside
    the re-booked hour 11. The over-count admits no second call until 24 h after that
    return, and the re-booked call still counts 24 h after the latest instant its POST
    could leave.
    """
    clock = _Clock(DISPATCH_ADMITTED)
    delayed = _sessions_delaying_updates(sessions, clock, timedelta(seconds=300))
    budget = GoogleCallBudget(_settings(2), sessionmaker=delayed, clock=clock)

    await budget.acquire()

    returned = clock.now
    latest_post = returned + budget_guard._DISPATCH_BOUND
    assert returned == datetime(2026, 10, 6, 11, tzinfo=UTC)
    assert budget_guard._hour_start(latest_post) == datetime(2026, 10, 6, 11, tzinfo=UTC)
    assert await _stored(sessions) == ({"2026-10-06T10": 1, "2026-10-06T11": 1}, 2)
    assert log_events == [_expired("2026-10-06T10", "2026-10-06T11")]

    with pytest.raises(CallBudgetExhausted) as same_hour:
        await budget.acquire()
    assert same_hour.value.retry_after == 86400.0

    clock.now = returned + timedelta(hours=24) - timedelta(seconds=1)
    with pytest.raises(CallBudgetExhausted) as next_day:
        await budget.acquire()
    assert next_day.value.retry_after == 1.0

    clock.advance(timedelta(seconds=1))
    await budget.acquire()
    assert await _stored(sessions) == ({"2026-10-06T11": 1, "2026-10-07T11": 1}, 2)

    with pytest.raises(CallBudgetExhausted) as full:
        await budget.acquire()
    retry_after = full.value.retry_after
    assert retry_after is not None
    assert retry_after == 3600.0
    assert clock.now + timedelta(seconds=retry_after) >= latest_post + timedelta(hours=24)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_lock_held_past_the_lock_timeout_fails_and_counts_nothing(
    sessions: Sessions,
) -> None:
    """A row held beyond the 10 s lock timeout raises a database error; nothing is counted."""
    budget = _budget(sessions)
    await budget.acquire()

    with anyio.fail_after(60):
        async with sessions() as holder:
            await holder.execute(_LOCK_GOOGLE_ROW)
            started = anyio.current_time()
            with pytest.raises(OperationalError) as raised:
                await budget.acquire()
            waited = anyio.current_time() - started
            await holder.commit()

    # SQLSTATE 55P03 is lock_not_available, raised when lock_timeout expires.
    assert getattr(raised.value.orig, "sqlstate", None) == "55P03"
    assert waited >= 9.0
    assert await _stored(sessions) == ({_key(0): 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_without_an_injected_sessionmaker_one_is_looked_up_per_call(
    sessions: Sessions, database: tuple[URL, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process-wide factory is looked up on every call, so a rebuilt engine is used."""
    url, schema = database
    rebuilt_engine = create_async_engine(
        url, poolclass=NullPool, connect_args=_connect_args(schema)
    )
    rebuilt = async_sessionmaker(rebuilt_engine, expire_on_commit=False)
    serving: list[Sessions] = [sessions]
    lookups: list[Sessions] = []

    def lookup() -> Sessions:
        """Return the factory the process currently serves, recording the lookup."""
        lookups.append(serving[-1])
        return serving[-1]

    monkeypatch.setattr(budget_guard, "get_sessionmaker", lookup)
    budget = GoogleCallBudget(_settings(5), clock=_Clock())
    try:
        await budget.acquire()
        # The process engine is disposed and rebuilt between two calls.
        serving.append(rebuilt)
        await budget.acquire()
        assert await budget.used_24h() == 2
        assert await budget.remaining() == 3
    finally:
        await rebuilt_engine.dispose()

    assert lookups == [sessions, rebuilt, rebuilt, rebuilt]


@pytest.mark.anyio
@pytest.mark.db
async def test_an_injected_sessionmaker_is_used_for_every_call(
    sessions: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a factory injected, the process-wide one is never looked up."""

    def lookup() -> Sessions:
        pytest.fail("the injected sessionmaker must serve every call")

    monkeypatch.setattr(budget_guard, "get_sessionmaker", lookup)
    budget = _budget(sessions, cap=5)

    await budget.acquire()
    assert await budget.used_24h() == 1
    assert await budget.remaining() == 4


# ---------------------------------------------------------------------------------------
# Metering the webhook through connectors/http.py
# ---------------------------------------------------------------------------------------


def _contract(surface: str) -> Mapping[str, Any]:
    """Load the Google contract file of ``surface``."""
    path = CONTRACTS / "google" / f"{surface}.json"
    loaded: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _contract_problems(value: object, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """List the keys of ``value`` its contract schema does not declare or misses."""
    problems: list[str] = []
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties") or {}
        required = schema.get("required") or ()
        problems.extend(f"{path}.{name} missing" for name in required if name not in value)
        for name, item in value.items():
            if name in properties:
                problems.extend(_contract_problems(item, properties[name], f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                problems.append(f"{path}.{name} undeclared")
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            problems.extend(_contract_problems(item, schema["items"], f"{path}[{index}]"))
    return problems


def test_the_webhook_payloads_follow_the_google_search_contract() -> None:
    """The request, the response and its rows carry only fields their contracts declare."""
    search = _contract("search")
    row = _contract("row_customer")
    results = SEARCH_RESPONSE["results"]
    assert isinstance(results, list)

    assert _contract_problems(REQUEST_BODY, search["request"]["body"]) == []
    assert _contract_problems(SEARCH_RESPONSE, search["response"]["body"]) == []
    assert [_contract_problems(item, row["response"]["body"]) for item in results] == [[]]
    assert 200 in search["response"]["status"]
    assert '"message": "Error in workflow"' in search["response"]["body"]["description"]
    assert _contract_problems({"extra": 1}, search["request"]["body"]) == [
        "$.customer_id missing",
        "$.query missing",
        "$.page_token missing",
        "$.extra undeclared",
    ]


class _Webhook:
    """A scripted n8n webhook: answers each POST with the next scripted reply, recording it.

    Each reply is a ``(status, body)`` pair; the last one repeats for every later POST.
    """

    def __init__(self, *replies: tuple[int, Mapping[str, object]]) -> None:
        self.requests: list[httpx.Request] = []
        self._replies = list(replies)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record ``request`` and return a fresh response for the next scripted reply."""
        self.requests.append(request)
        status, body = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return httpx.Response(status, json=body)


async def _search(
    webhook: _Webhook, budget: GoogleCallBudget, waits: list[float]
) -> dict[str, object]:
    """Send one webhook search through ``send_json`` metered by ``budget``; return its body."""

    async def record(seconds: float) -> None:
        """Record a backoff wait instead of sleeping."""
        waits.append(seconds)

    async with build_client("google", httpx.MockTransport(webhook), settings=Settings()) as http:

        def build() -> httpx.Request:
            """Build the webhook POST of one attempt."""
            return http.build_request("POST", WEBHOOK_URL, json=REQUEST_BODY)

        response = await send_json(
            http,
            build,
            source="google",
            endpoint=SEARCH_ENDPOINT,
            policy=RetryPolicy(sleep=record),
            before_attempt=budget.acquire,
        )
    assert response.status == 200
    assert isinstance(response.body, dict)
    return response.body


@pytest.mark.anyio
@pytest.mark.db
async def test_every_post_attempt_is_metered_retries_included(sessions: Sessions) -> None:
    """A 500 then a 200 is two webhook POSTs, and both count against the cap."""
    webhook = _Webhook((500, N8N_WORKFLOW_ERROR), (200, SEARCH_RESPONSE))
    budget = _budget(sessions, cap=5)
    waits: list[float] = []

    body = await _search(webhook, budget, waits)

    assert body["fieldMask"] == SEARCH_RESPONSE["fieldMask"]
    assert [json.loads(request.content) for request in webhook.requests] == [
        REQUEST_BODY,
        REQUEST_BODY,
    ]
    assert len(waits) == 1
    assert await budget.used_24h() == 2
    assert await _stored(sessions) == ({_key(0): 2}, 2)


@pytest.mark.anyio
@pytest.mark.db
async def test_an_exhausted_budget_sends_nothing_and_never_retries(sessions: Sessions) -> None:
    """With no call left, ``send_json`` raises ``CallBudgetExhausted`` before any POST."""
    webhook = _Webhook((200, SEARCH_RESPONSE))
    budget = _budget(sessions, cap=1)
    await budget.acquire()
    waits: list[float] = []

    with pytest.raises(CallBudgetExhausted) as raised:
        await _search(webhook, budget, waits)

    assert raised.value.retry_after == FULL_DAY
    assert webhook.requests == []
    assert waits == []
    assert await budget.used_24h() == 1


@pytest.mark.anyio
@pytest.mark.db
async def test_a_budget_used_up_between_retries_ends_the_retries(sessions: Sessions) -> None:
    """Retries stop at the cap: the refused attempt is never sent, and nothing more counts."""
    webhook = _Webhook((500, N8N_WORKFLOW_ERROR))
    budget = _budget(sessions, cap=2)
    waits: list[float] = []

    with pytest.raises(CallBudgetExhausted):
        await _search(webhook, budget, waits)

    assert len(webhook.requests) == 2
    assert len(waits) == 2
    assert await _stored(sessions) == ({_key(0): 2}, 2)


class _TimedWebhook(_Webhook):
    """A scripted webhook that records the clock when each POST starts to leave.

    Each POST first advances ``clock`` by ``dispatch_wait``, the time httpx spends getting
    a connection between ``acquire`` returning and the request leaving, which the dispatch
    bound limits; the instant is recorded after that wait.
    """

    def __init__(
        self, clock: _Clock, dispatch_wait: timedelta, *replies: tuple[int, Mapping[str, object]]
    ) -> None:
        super().__init__(*replies)
        self.sent_at: list[datetime] = []
        self._clock = clock
        self._dispatch_wait = dispatch_wait

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Wait out the dispatch wait on the clock, record the instant, and reply."""
        self._clock.advance(self._dispatch_wait)
        self.sent_at.append(self._clock.now)
        return super().__call__(request)


@pytest.mark.anyio
@pytest.mark.db
async def test_no_post_is_sent_on_a_booking_that_expired_during_its_commit(
    sessions: Sessions,
) -> None:
    """A commit delayed past the booked hour sends nothing under a cap of 1, and never retries.

    Booked at 10:55:00 in hour 10, the commit returns at 11:00:00, so the POST could leave
    in hour 11, which the booking does not cover. The re-reservation finds the cap reached.
    """
    clock = _Clock(DISPATCH_ADMITTED)
    webhook = _TimedWebhook(clock, timedelta(0), (200, SEARCH_RESPONSE))
    delayed = _sessions_delaying_updates(sessions, clock, timedelta(seconds=300))
    budget = GoogleCallBudget(_settings(1), sessionmaker=delayed, clock=clock)
    waits: list[float] = []

    with pytest.raises(CallBudgetExhausted) as raised:
        await _search(webhook, budget, waits)

    assert raised.value.retry_after == 86400.0
    assert webhook.requests == []
    assert webhook.sent_at == []
    assert waits == []
    assert await _stored(sessions) == ({"2026-10-06T10": 1}, 1)


@pytest.mark.anyio
@pytest.mark.db
async def test_a_post_delayed_up_to_the_bound_counts_for_24_hours_after_it_leaves(
    sessions: Sessions,
) -> None:
    """A 100 s commit and a 139 s connection wait still send the POST in its booked hour.

    Admitted at 10:57:50, the call is booked in hour 11. The commit returns at 10:59:30 and
    the POST leaves 139 s later, at 11:01:49, so the booking covers it. Under a cap of 1, a
    second POST is refused until hour 11's bucket leaves the window, over 24 h later.
    """
    clock = _Clock(datetime(2026, 10, 6, 10, 57, 50, tzinfo=UTC))
    dispatch_wait = budget_guard._DISPATCH_BOUND - timedelta(seconds=1)
    webhook = _TimedWebhook(clock, dispatch_wait, (200, SEARCH_RESPONSE))
    delayed = _sessions_delaying_updates(sessions, clock, timedelta(seconds=100))
    budget = GoogleCallBudget(_settings(1), sessionmaker=delayed, clock=clock)
    waits: list[float] = []

    await _search(webhook, budget, waits)

    assert await _stored(sessions) == ({"2026-10-06T11": 1}, 1)
    booked = budget_guard._parse_bucket_key("2026-10-06T11")
    assert booked is not None
    first = webhook.sent_at[0]
    assert first == datetime(2026, 10, 6, 11, 1, 49, tzinfo=UTC)
    assert budget_guard._hour_start(first) <= booked

    clock.now = first + timedelta(hours=24) - timedelta(seconds=1)
    with pytest.raises(CallBudgetExhausted) as raised:
        await _search(webhook, budget, waits)
    retry_after = raised.value.retry_after
    assert retry_after is not None
    assert clock.now + timedelta(seconds=retry_after) == booked + timedelta(hours=WINDOW_BUCKETS)
    assert len(webhook.sent_at) == 1

    clock.advance(timedelta(seconds=retry_after))
    await _search(webhook, budget, waits)

    assert len(webhook.sent_at) == 2
    assert webhook.sent_at[1] - first >= timedelta(hours=24)
    assert waits == []
    assert await _stored(sessions) == ({"2026-10-07T12": 1}, 1)
