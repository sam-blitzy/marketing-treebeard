"""Tests for ``ads_engine.analysis.overlay``: the read-time current-state overlay.

AAP 0.3.2 ("Refresh after change, and the current-state overlay", "Items from replaced data
are historical-only"), 0.5.3 (``Overlay``, ``ItemOverlay``, ``Banner``), 0.5.4.2 (P1 live
holds and counts, P2 tracking, P3 simulated, P4 stale and incomplete, P5 short window,
feedback) and 0.5.5 (the fixed banner order). The scenarios follow the P1, P3, P4, P5,
feedback and historical-only rows of ``acceptance/test_precedence.py`` in AAP 0.11.2: a
discrepancy detected after the run holds dependent items at read time and the banner count
updates at once, C0 exempt; items computed from a replaced dataset are historical with
every action disabled, keep their simulated provenance, and report ``data_status_now``
from the source's current mode; stale and incomplete flags derive from each item's own
required datasets; the short-window banner uses the configured minimum.

The pure parts need no database: the view dataclasses, their validation and JSON shapes,
:func:`is_current`, and the argument checks :func:`overlay` makes before it reads anything.

The database tests run against the PostgreSQL 18 test database named by
``TEST_DATABASE_URL`` (falling back to ``DATABASE_URL``). The module creates a private
schema, builds every table of the model metadata in it, points every connection's
``search_path`` at it alone, and drops it with ``CASCADE`` at teardown, so the ``public``
schema and every other schema stay untouched. The overlay reads every active dataset,
discrepancy, connection and feedback row, so each test starts from empty tables of that
private schema. An unreachable database fails the database tests; it never skips them.
"""

import copy
import doctest
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import create_engine, event, or_, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.analysis.context import (
    B1,
    BANNER_INCOMPLETE_DATASETS,
    BANNER_OPEN_DISCREPANCIES,
    BANNER_ORDER,
    BANNER_SHORT_WINDOW,
    BANNER_SIMULATED_SOURCES,
    BANNER_STALE_DATASETS,
    BANNER_TRACKING_ABSENT,
    C0,
    C1,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FINDING_NO_CRM_LINK,
    FLAG_INCOMPLETE,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    HELD_DISCREPANCY_REASON,
    HISTORICAL_REASON,
    K1,
    K2,
    K3,
    W1,
    Flag,
    ProvenanceEntry,
    unverified_message,
)
from ads_engine.analysis.overlay import (
    BannerView,
    ItemOverlayView,
    OverlayResult,
    OverlayView,
    _load_feedback,
    current_dataset_ids,
    is_current,
    item_hidden_state,
    item_is_current,
    load_check_representatives,
    overlay,
)
from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CONTENT_ASSETS_FILE,
    HUBSPOT_MAPPING_FILE,
    THRESHOLDS_FILE,
    Thresholds,
)
from ads_engine.db.base import Base
from ads_engine.db.models.analysis import (
    AnalysisRun,
    Recommendation,
    RecommendationFeedback,
    RunFinding,
)
from ads_engine.db.models.decisions import Discrepancy
from ads_engine.db.models.ops import DATASETS, DatasetFreshness, SourceConnection, SourceDataset

pytestmark = pytest.mark.integration

type Sessions = async_sessionmaker[AsyncSession]

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
"""The read time of every page."""

FRESH = NOW - timedelta(hours=1)
"""A last success well inside ``sources.stale_after_hours``."""

OLD = NOW - timedelta(hours=25)
"""A last success just beyond the shipped 24-hour ``sources.stale_after_hours``."""

WINDOW_START = date(2026, 9, 9)
WINDOW_END = date(2026, 10, 6)
"""The default 28-day window: yesterday UTC is its end."""

TRACKING_MESSAGE = (
    "No conversions or leads recorded — verify tracking before acting on zero-outcome rules"
)
"""The P2 finding message of AAP 0.5.4.2."""


# ---------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------


def _thresholds(section: str, **values: object) -> Thresholds:
    """Build the shipped configuration with the given ``<section>.*`` values replaced."""
    trees = {
        name: yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in CONFIG_FILES
    }
    edited = copy.deepcopy(trees)
    edited[THRESHOLDS_FILE][section].update(values)
    return Thresholds.from_trees(
        thresholds=edited[THRESHOLDS_FILE],
        content_assets=edited[CONTENT_ASSETS_FILE],
        hubspot_mapping=edited[HUBSPOT_MAPPING_FILE],
        source_dir=CONFIG_DIR,
    )


@pytest.fixture(scope="module")
def shipped() -> Thresholds:
    """The shipped ``config/*.yaml``."""
    return Thresholds.load(CONFIG_DIR)


# ---------------------------------------------------------------------------------------
# Pure: view validation and JSON shapes
# ---------------------------------------------------------------------------------------


def _modes(**overrides: str | None) -> dict[str, str | None]:
    """Return ``{google, linkedin, hubspot}`` modes, ``live`` unless overridden."""
    modes: dict[str, str | None] = {"google": "live", "linkedin": "live", "hubspot": "live"}
    modes.update(overrides)
    return modes


def test_overlay_view_orders_sources_and_datasets_and_renders_the_overlay_shape() -> None:
    view = OverlayView(
        open_discrepancies=2,
        source_modes={"hubspot": None, "linkedin": "simulated", "google": "live"},
        stale_datasets=("linkedin.leads", "google.metrics_daily", "google.metrics_daily"),
        incomplete_datasets=["hubspot.contacts"],  # type: ignore[arg-type]
        computed_at=NOW,
    )
    assert list(view.source_modes) == ["google", "linkedin", "hubspot"]
    assert view.stale_datasets == ("google.metrics_daily", "linkedin.leads")
    assert view.incomplete_datasets == ("hubspot.contacts",)
    assert view.to_json() == {
        "open_discrepancies": 2,
        "source_modes": {"google": "live", "linkedin": "simulated", "hubspot": None},
        "stale_datasets": ["google.metrics_daily", "linkedin.leads"],
        "incomplete_datasets": ["hubspot.contacts"],
        "computed_at": "2026-10-07T12:00:00+00:00",
    }
    with pytest.raises(TypeError):
        view.source_modes["google"] = "simulated"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        view.open_discrepancies = 3  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "error", "match"),
    [
        ({"open_discrepancies": True}, TypeError, "must be an int"),
        ({"open_discrepancies": "1"}, TypeError, "must be an int"),
        ({"open_discrepancies": -1}, ValueError, "must be >= 0"),
        ({"source_modes": {"google": "live", "linkedin": "live"}}, ValueError, "exactly"),
        ({"source_modes": {**_modes(), "posthog": "live"}}, ValueError, "exactly"),
        ({"source_modes": _modes(linkedin="mock")}, ValueError, "must be one of"),
        ({"stale_datasets": ("google.metrics",)}, ValueError, "unknown dataset names"),
        ({"incomplete_datasets": ("crm.contacts",)}, ValueError, "unknown dataset names"),
        ({"computed_at": NOW.replace(tzinfo=None)}, ValueError, "must be tz-aware"),
        ({"computed_at": date(2026, 10, 7)}, TypeError, "tz-aware datetime"),
    ],
)
def test_overlay_view_rejects_invalid_fields(
    overrides: dict[str, Any], error: type[Exception], match: str
) -> None:
    fields: dict[str, Any] = {
        "open_discrepancies": 0,
        "source_modes": _modes(),
        "stale_datasets": (),
        "incomplete_datasets": (),
        "computed_at": NOW,
    }
    fields.update(overrides)
    with pytest.raises(error, match=match):
        OverlayView(**fields)


def test_item_overlay_view_renders_item_overlay_with_optional_keys_only_when_set() -> None:
    plain = ItemOverlayView(held_now=False, historical=False, data_status_now="live")
    assert plain.actions_enabled
    assert plain.to_json() == {
        "held_now": False,
        "historical": False,
        "data_status_now": "live",
        "flags_now": [],
    }
    stale = Flag(FLAG_STALE, "Data as of Oct 6, 2026, 11:00 AM UTC")
    historical = ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="stale",
        flags_now=[stale],  # type: ignore[arg-type]
        hidden_by="snooze",
    )
    assert historical.flags_now == (stale,)
    assert not historical.actions_enabled
    assert historical.to_json() == {
        "held_now": True,
        "held_reason_now": HISTORICAL_REASON,
        "historical": True,
        "data_status_now": "stale",
        "flags_now": [{"code": FLAG_STALE, "message": "Data as of Oct 6, 2026, 11:00 AM UTC"}],
        "hidden_by": "snooze",
    }
    assert list(historical.to_json()) == [
        "held_now",
        "held_reason_now",
        "historical",
        "data_status_now",
        "flags_now",
        "hidden_by",
    ]


@pytest.mark.parametrize(
    ("overrides", "error", "match"),
    [
        ({"held_now": True}, ValueError, "set exactly when held_now"),
        ({"held_reason_now": HELD_DISCREPANCY_REASON}, ValueError, "set exactly when held_now"),
        ({"held_now": True, "held_reason_now": "  "}, ValueError, "must not be blank"),
        ({"historical": True}, ValueError, "always held"),
        ({"data_status_now": "unknown"}, ValueError, "data_status_now must be one of"),
        ({"flags_now": ("stale",)}, TypeError, "must be Flag"),
        (
            {"flags_now": (Flag(FLAG_STALE, "a"), Flag(FLAG_STALE, "b"))},
            ValueError,
            "repeats a code",
        ),
        ({"hidden_by": "archive"}, ValueError, "hidden_by must be one of"),
    ],
)
def test_item_overlay_view_rejects_invalid_fields(
    overrides: dict[str, Any], error: type[Exception], match: str
) -> None:
    fields: dict[str, Any] = {"held_now": False, "historical": False, "data_status_now": "live"}
    fields.update(overrides)
    with pytest.raises(error, match=match):
        ItemOverlayView(**fields)


def test_banner_view_alerts_only_for_discrepancies_and_omits_an_absent_count() -> None:
    discrepancies = BannerView(
        code=BANNER_OPEN_DISCREPANCIES, tone="critical", message="1 discrepancy", count=1
    )
    short = BannerView(code=BANNER_SHORT_WINDOW, tone="warning", message="Window of 10 days")
    assert discrepancies.alert
    assert not short.alert
    assert discrepancies.to_json() == {
        "code": BANNER_OPEN_DISCREPANCIES,
        "tone": "critical",
        "message": "1 discrepancy",
        "count": 1,
    }
    assert short.to_json() == {
        "code": BANNER_SHORT_WINDOW,
        "tone": "warning",
        "message": "Window of 10 days",
    }


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"code": "custom_window"}, "code must be one of"),
        ({"tone": "warning"}, "has tone 'critical'"),
        ({"message": " "}, "must not be blank"),
        ({"count": -1}, "non-negative int"),
        ({"count": True}, "non-negative int"),
    ],
)
def test_banner_view_rejects_invalid_fields(overrides: dict[str, Any], match: str) -> None:
    fields: dict[str, Any] = {
        "code": BANNER_OPEN_DISCREPANCIES,
        "tone": "critical",
        "message": "2 discrepancies",
        "count": 2,
    }
    fields.update(overrides)
    with pytest.raises(ValueError, match=match):
        BannerView(**fields)


def test_overlay_result_renders_items_keyed_by_their_string_id() -> None:
    view = OverlayView(
        open_discrepancies=0,
        source_modes=_modes(),
        stale_datasets=(),
        incomplete_datasets=(),
        computed_at=NOW,
    )
    item = ItemOverlayView(held_now=False, historical=False, data_status_now="live")
    banner = BannerView(code=BANNER_SHORT_WINDOW, tone="warning", message="Window of 10 days")
    result = OverlayResult(overlay=view, items={7: item}, banners=(banner,))
    assert result.to_json() == {
        "overlay": view.to_json(),
        "items": {"7": item.to_json()},
        "banners": [banner.to_json()],
    }


# ---------------------------------------------------------------------------------------
# Pure: is_current
# ---------------------------------------------------------------------------------------


def _entry(*, drop: str | None = None, **changes: object) -> dict[str, object]:
    """Return the complete current entry (google 3, live, primary), edited or minus ``drop``."""
    entry: dict[str, object] = {
        "source": "google",
        "dataset_id": 3,
        "mode": "live",
        "role": "primary",
    }
    entry.update(changes)
    if drop is not None:
        del entry[drop]
    return entry


@pytest.mark.parametrize(
    ("provenance", "expected"),
    [
        # Complete entries: current exactly when every dataset id is current.
        ([_entry()], True),
        ([_entry(), _entry(source="linkedin", dataset_id=5, mode="simulated")], True),
        ([_entry(), _entry(source="linkedin", dataset_id=9, mode="simulated")], False),
        ([ProvenanceEntry("google", 3, "live", "primary")], True),
        (
            [
                ProvenanceEntry("google", 3, "live", "primary"),
                ProvenanceEntry("hubspot", 5, "simulated", "companion"),
            ],
            True,
        ),
        ([ProvenanceEntry("hubspot", 6, "simulated", "companion")], False),
        ([ProvenanceEntry("google", 3, "live", "primary"), _entry(dataset_id=5)], True),
        # Fails closed: an empty provenance is unverifiable, never current.
        ([], False),
        ((), False),
        # A current id never vouches for an entry missing or misstating a key.
        ([_entry(drop="role")], False),
        ([_entry(drop="mode")], False),
        ([_entry(drop="source")], False),
        ([_entry(drop="dataset_id")], False),
        ([_entry(source="posthog")], False),
        ([_entry(source=["google"])], False),
        ([_entry(mode="mock")], False),
        ([_entry(role="parent")], False),
        ([_entry(role=None)], False),
        ([_entry(dataset_id="3")], False),
        ([_entry(dataset_id=True)], False),
        ([_entry(dataset_id=1.0)], False),
        ([{"source": "linkedin", "dataset_id": 4}], False),
        ([{"source": "google"}], False),
        # One undecodable entry makes the whole provenance unverifiable: nothing is dropped.
        ([_entry(), _entry(dataset_id=5, drop="role")], False),
        ([ProvenanceEntry("google", 3, "live", "primary"), {"dataset_id": 5}], False),
        ([_entry(), 3], False),
        ([_entry(), None], False),
        ([3], False),
        ("google", False),
    ],
)
def test_is_current_requires_a_complete_provenance_of_current_datasets(
    provenance: Sequence[Any], expected: bool
) -> None:
    assert is_current(provenance, frozenset({1, 3, 5})) is expected


def test_is_current_doctest_examples_hold() -> None:
    finder = doctest.DocTestFinder()
    runner = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS)
    examples = finder.find(is_current, "is_current", globs={"is_current": is_current})
    assert examples
    assert examples[0].examples
    for example in examples:
        runner.run(example)
    assert runner.summarize(verbose=False).failed == 0


# ---------------------------------------------------------------------------------------
# Pure: argument checks made before any read
# ---------------------------------------------------------------------------------------


def _transient_run(run_id: int = 1, *, days: int = 28) -> AnalysisRun:
    """Return an unsaved run ending on :data:`WINDOW_END`."""
    return AnalysisRun(
        id=run_id,
        kind="default",
        window_start=WINDOW_END - timedelta(days=days - 1),
        window_end=WINDOW_END,
        source_day_boundaries=[],
        trigger="scheduled",
        data_modes={},
        dataset_ids={},
        config_hash="overlay-test",
        default_days=28,
        trusted_min_days=14,
        status="succeeded",
        started_at=NOW - timedelta(hours=2),
        finished_at=NOW - timedelta(hours=2),
    )


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("now", "error", "match"),
    [
        (NOW.replace(tzinfo=None), ValueError, "now must be tz-aware"),
        (date(2026, 10, 7), TypeError, "now must be a tz-aware datetime"),
        ("2026-10-07T12:00:00Z", TypeError, "now must be a tz-aware datetime"),
    ],
)
async def test_overlay_rejects_a_now_that_is_not_an_instant(
    now: object, error: type[Exception], match: str, shipped: Thresholds
) -> None:
    session = AsyncSession()
    try:
        with pytest.raises(error, match=match):
            await overlay(
                session,
                _transient_run(),
                [],
                now,  # type: ignore[arg-type]
                thresholds=shipped,
                display_timezone="UTC",
            )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_overlay_rejects_items_of_another_run(shipped: Thresholds) -> None:
    items = [
        Recommendation(id=10, run_id=1, rule_id=K1),
        Recommendation(id=11, run_id=3, rule_id=K1),
        Recommendation(id=12, run_id=2, rule_id=K2),
        Recommendation(id=13, run_id=3, rule_id=K2),
    ]
    session = AsyncSession()
    try:
        with pytest.raises(ValueError, match=r"overlay of run 1 was given items of runs \[2, 3\]"):
            await overlay(
                session, _transient_run(1), items, NOW, thresholds=shipped, display_timezone="UTC"
            )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_overlay_rejects_check_items_of_another_run(shipped: Thresholds) -> None:
    items = [Recommendation(id=10, run_id=1, rule_id=K1)]
    check_items = [
        Recommendation(id=11, run_id=4, rule_id=K2),
        Recommendation(id=12, run_id=1, rule_id=K2),
    ]
    session = AsyncSession()
    try:
        with pytest.raises(ValueError, match=r"overlay of run 1 was given items of runs \[4\]"):
            await overlay(
                session,
                _transient_run(1),
                items,
                NOW,
                check_items=check_items,
                thresholds=shipped,
                display_timezone="UTC",
            )
    finally:
        await session.close()


@pytest.mark.parametrize(
    ("now", "error", "match"),
    [
        (NOW.replace(tzinfo=None), ValueError, "now must be tz-aware"),
        ("2026-10-07T12:00:00Z", TypeError, "now must be a tz-aware datetime"),
    ],
)
def test_item_hidden_state_needs_an_instant(
    now: object, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        item_hidden_state(1, now)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_overlay_requires_injected_thresholds_and_never_loads_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_load(*args: object, **kwargs: object) -> Thresholds:
        pytest.fail("overlay() read config/*.yaml on the async read path")

    monkeypatch.setattr(Thresholds, "load", forbidden_load)
    # An unbound session: any database read would raise UnboundExecutionError, not TypeError.
    session = AsyncSession()
    try:
        with pytest.raises(TypeError, match="missing 1 required keyword-only argument"):
            await overlay(session, _transient_run(), [], NOW, display_timezone="UTC")  # type: ignore[call-arg]
        for value, found in ((None, "NoneType"), ({"window": {}}, "dict")):
            with pytest.raises(TypeError, match=rf"injected by the caller .*found {found}$"):
                await overlay(
                    session,
                    _transient_run(),
                    [],
                    NOW,
                    thresholds=value,  # type: ignore[arg-type]
                    display_timezone="UTC",
                )
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("zone", "error", "match"),
    [
        ("Mars/Base", ValueError, "display_timezone must be a loadable IANA time zone name"),
        ("", ValueError, "display_timezone must be a loadable IANA time zone name"),
        (None, TypeError, "display_timezone must be an IANA time zone name, found None"),
    ],
)
async def test_overlay_requires_a_loadable_display_timezone_before_any_read(
    zone: object, error: type[Exception], match: str, shipped: Thresholds
) -> None:
    """The zone the stale banner and flags render instants in (AAP 0.5.5) is checked first."""
    # An unbound session: any database read would raise UnboundExecutionError instead.
    session = AsyncSession()
    try:
        with pytest.raises(TypeError, match="missing 1 required keyword-only argument"):
            await overlay(session, _transient_run(), [], NOW, thresholds=shipped)  # type: ignore[call-arg]
        with pytest.raises(error, match=match):
            await overlay(
                session,
                _transient_run(),
                [],
                NOW,
                thresholds=shipped,
                display_timezone=zone,  # type: ignore[arg-type]
            )
    finally:
        await session.close()


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the current-state overlay tests"
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
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"overlay_test_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
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
async def engine(database: tuple[URL, str]) -> AsyncIterator[AsyncEngine]:
    """Yield an engine on the private schema, whose tables this test starts from empty."""
    url, schema = database
    engine = create_async_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        async with engine.begin() as connection:
            preparer = connection.dialect.identifier_preparer
            tables = ", ".join(preparer.quote(table.name) for table in Base.metadata.sorted_tables)
            await connection.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> Sessions:
    """Return a session factory on this test's engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------------------


async def _dataset(
    session: AsyncSession,
    source: str,
    *,
    mode: str = "live",
    state: str = "active",
    reason: str | None = None,
) -> SourceDataset:
    """Insert a primary dataset; an active one retires the source's previous one first.

    Retiring a LinkedIn dataset retires its companion with it: a companion shares its
    parent's state (AAP 0.3.3).
    """
    if state == "active":
        previous = list(
            await session.scalars(
                select(SourceDataset.id).where(
                    SourceDataset.source == source,
                    SourceDataset.state == "active",
                    SourceDataset.role == "primary",
                )
            )
        )
        if previous:
            await session.execute(
                update(SourceDataset)
                .where(
                    or_(
                        SourceDataset.id.in_(previous),
                        SourceDataset.parent_dataset_id.in_(previous),
                    )
                )
                .values(state="retired", retired_at=NOW - timedelta(minutes=30))
            )
    dataset = SourceDataset(
        source=source,
        mode=mode,
        state=state,
        role="primary",
        reason=reason,
        activated_at=None if state == "building" else NOW - timedelta(days=10),
        retired_at=NOW - timedelta(days=1) if state == "retired" else None,
    )
    session.add(dataset)
    await session.flush()
    return dataset


async def _companion(session: AsyncSession, parent: SourceDataset) -> SourceDataset:
    """Insert the simulated CRM companion of a simulated LinkedIn dataset."""
    companion = SourceDataset(
        source="hubspot",
        mode="simulated",
        state=parent.state,
        role="companion",
        parent_dataset_id=parent.id,
        reason=parent.reason,
        activated_at=parent.activated_at,
    )
    session.add(companion)
    await session.flush()
    return companion


async def _freshness(
    session: AsyncSession,
    dataset: SourceDataset,
    name: str,
    *,
    success: datetime | None = FRESH,
    through: date | None = WINDOW_END,
    covered_from: date = WINDOW_START - timedelta(days=150),
    completeness: str = "complete",
    reason: str | None = None,
) -> None:
    """Insert one ``dataset_freshness`` row of ``dataset``.

    Coverage is ``[covered_from, through]``, or no range at all when ``through`` is ``None``
    (a sync records both ends or neither).
    """
    session.add(
        DatasetFreshness(
            source=dataset.source,
            dataset_id=dataset.id,
            dataset=name,
            last_attempt_at=FRESH if success is None else success,
            last_success_at=success,
            covered_from=None if through is None else covered_from,
            covered_through=through,
            completeness=completeness,
            reason=reason,
        )
    )
    await session.flush()


async def _connection(
    session: AsyncSession,
    source: str,
    mode: str,
    reason: str | None,
    dataset: SourceDataset | None,
    *,
    status: str = "ok",
) -> None:
    """Insert the ``source_connection`` row of ``source``; ``dataset`` ``None`` has none active."""
    session.add(
        SourceConnection(
            source=source,
            mode=mode,
            status=status,
            reason=reason,
            active_dataset_id=None if dataset is None else dataset.id,
            last_attempt_at=FRESH,
        )
    )
    await session.flush()


async def _covered(
    session: AsyncSession, *sources: str, through: date = WINDOW_END
) -> list[SourceDataset]:
    """Give each of ``sources`` an active live dataset whose every dataset is fresh and complete.

    A source with no active dataset has every one of its datasets listed stale and incomplete
    on every page, so a test about something else gives the sources it does not exercise
    complete data, covering through ``through`` (a run ending later needs a later one).
    """
    datasets: list[SourceDataset] = []
    for source in sources:
        dataset = await _dataset(session, source)
        for name in DATASETS:
            if name.startswith(f"{source}."):
                await _freshness(session, dataset, name, through=through)
        datasets.append(dataset)
    return datasets


async def _run(
    session: AsyncSession,
    *,
    start: date = WINDOW_START,
    end: date = WINDOW_END,
    kind: str = "default",
    trusted_min_days: int = 14,
) -> AnalysisRun:
    """Insert a succeeded run over ``[start, end]`` computed under ``trusted_min_days``."""
    run = AnalysisRun(
        kind=kind,
        window_start=start,
        window_end=end,
        source_day_boundaries=[],
        trigger="scheduled" if kind == "default" else "manual",
        data_modes={},
        dataset_ids={},
        config_hash="overlay-test",
        default_days=28,
        trusted_min_days=trusted_min_days,
        status="succeeded",
        started_at=NOW - timedelta(hours=2),
        finished_at=NOW - timedelta(hours=2) + timedelta(minutes=1),
    )
    session.add(run)
    await session.flush()
    return run


def _prov(*datasets: SourceDataset) -> list[dict[str, Any]]:
    """Return the frozen ``evidence.provenance`` of an item that read ``datasets``."""
    return [
        ProvenanceEntry(
            source=dataset.source,  # type: ignore[arg-type]
            dataset_id=dataset.id,
            mode=dataset.mode,  # type: ignore[arg-type]
            role=dataset.role,  # type: ignore[arg-type]
        ).to_json()
        for dataset in datasets
    ]


async def _item(
    session: AsyncSession,
    run: AnalysisRun,
    *,
    rule_id: str = K1,
    section: str = "keywords",
    group: str = "block",
    entity_type: str | None = "search_term",
    entity_id: str | None = "7|term",
    provenance: Sequence[dict[str, Any]] = (),
    required: Sequence[str] = (),
    decision_subject: dict[str, Any] | None = None,
    depends_on: list[object] | None = None,
    held: bool = False,
    held_reason: str | None = None,
    data_status: str = "live",
    flags: Sequence[str] = (),
    evidence: Any = None,
) -> Recommendation:
    """Insert one stored item of ``run``; ``evidence`` replaces the built evidence block."""
    if evidence is None:
        evidence = {
            "window": {
                "start": run.window_start.isoformat(),
                "end": run.window_end.isoformat(),
                "days": (run.window_end - run.window_start).days + 1,
                "source_days": [],
            },
            "metrics": [],
            "baselines": [],
            "thresholds": {},
            "datasets": [],
            "provenance": list(provenance),
        }
        if depends_on is not None:
            evidence["extra"] = {"depends_on": depends_on}
    item = Recommendation(
        run_id=run.id,
        section=section,
        group=group,
        rule_id=rule_id,
        entity_type=entity_type,
        entity_id=entity_id,
        rank=1,
        action="Add as negative keyword",
        reason="Spent with no conversions",
        evidence=evidence,
        flags=list(flags),
        data_status=data_status,
        held=held,
        held_reason=held_reason,
        required_datasets=list(required),
        decision_subject=decision_subject,
    )
    session.add(item)
    await session.flush()
    return item


async def _discrepancy(
    session: AsyncSession,
    dataset: SourceDataset,
    subject_type: str,
    subject_id: int,
    *,
    kind: str = "status",
    state: str = "open",
) -> None:
    """Insert a C0 discrepancy on a subject of ``dataset``."""
    resolved = state == "resolved"
    session.add(
        Discrepancy(
            source=dataset.source,
            dataset_id=dataset.id,
            subject_type=subject_type,
            subject_id=subject_id,
            kind=kind,
            decided_value={"status": "paused"},
            platform_value={"status": "active"},
            detected_at=NOW - timedelta(minutes=10),
            state=state,
            resolved_at=NOW - timedelta(minutes=5) if resolved else None,
            resolution="platform_correct" if resolved else None,
        )
    )
    await session.flush()


async def _feedback(
    session: AsyncSession,
    item: Recommendation,
    kind: str,
    *,
    until: datetime,
    created_at: datetime,
) -> None:
    """Insert feedback on ``item``, copying its rule and entity as the API does."""
    session.add(
        RecommendationFeedback(
            recommendation_id=item.id,
            rule_id=item.rule_id,
            entity_type=item.entity_type,
            entity_id=item.entity_id,
            kind=kind,
            until=until,
            created_at=created_at,
            created_by="admin@example.com",
        )
    )
    await session.flush()


async def _finding(
    session: AsyncSession,
    run: AnalysisRun,
    code: str,
    *,
    entity: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    message: str = TRACKING_MESSAGE,
) -> RunFinding:
    """Insert a stored finding of ``run``."""
    finding = RunFinding(
        run_id=run.id,
        section="keywords",
        code=code,
        entity=entity,
        message=message,
        evidence={} if evidence is None else evidence,
    )
    session.add(finding)
    await session.flush()
    return finding


async def _page(
    sessions: Sessions,
    run_id: int,
    *,
    thresholds: Thresholds,
    findings: Sequence[RunFinding] | None = None,
    display_timezone: str = "UTC",
) -> OverlayResult:
    """Load a run and its items in a fresh session and overlay them, as the API does.

    ``display_timezone`` is the API's ``settings.display_timezone``: ``UTC``, the shipped
    default, unless a test shows instants in another zone.
    """
    async with sessions() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        items = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.run_id == run_id)
                .order_by(Recommendation.id)
            )
        )
        return await overlay(
            session,
            run,
            items,
            NOW,
            findings=findings,
            thresholds=thresholds,
            display_timezone=display_timezone,
        )


async def _stored(sessions: Sessions, item_id: int) -> Recommendation:
    """Reload one stored item in a fresh session."""
    async with sessions() as session:
        item = await session.get(Recommendation, item_id)
        assert item is not None
        return item


def _unverified(count: int) -> Flag:
    """The ``unverified_discrepancies`` flag for ``count`` unresolved discrepancies."""
    return Flag(FLAG_UNVERIFIED_DISCREPANCIES, unverified_message(count))


# ---------------------------------------------------------------------------------------
# P1: live holds and the live discrepancy count
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_a_discrepancy_detected_after_the_run_holds_dependent_items_at_read_time(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        retired = await _dataset(session, "google", state="retired")
        google = await _dataset(session, "google")
        for name in ("google.entities", "google.metrics_daily", "google.search_terms"):
            await _freshness(session, google, name)
        await _covered(session, "linkedin", "hubspot")
        run = await _run(session)
        provenance = _prov(google)
        c0 = await _item(
            session,
            run,
            rule_id=C0,
            section="campaigns",
            group="discrepancies",
            entity_type="campaign",
            entity_id="11",
            provenance=provenance,
            required=("google.entities",),
            decision_subject={"type": "campaign", "id": 11},
        )
        keyword = await _item(
            session,
            run,
            provenance=provenance,
            required=("google.search_terms", "google.metrics_daily"),
            depends_on=[{"type": "campaign", "id": 11}],
        )
        budget = await _item(
            session,
            run,
            rule_id=B1,
            section="budget",
            group="changes",
            entity_type="budget",
            entity_id="21",
            provenance=provenance,
            decision_subject={"type": "budget", "id": 21},
        )
        pause = await _item(
            session,
            run,
            rule_id=C1,
            section="campaigns",
            group="pause",
            entity_type="campaign",
            entity_id="12",
            provenance=provenance,
            decision_subject={"type": "campaign", "id": 12},
        )
        # Not decision subjects: an id of the wrong type, a campaign group, a bool id and a
        # bare id never match the discrepant campaign 11.
        malformed = await _item(
            session,
            run,
            rule_id=K2,
            group="expand",
            provenance=provenance,
            decision_subject={"type": "campaign"},
            depends_on=[
                {"type": "campaign", "id": "11"},
                {"type": "campaign_group", "id": 11},
                {"type": "campaign", "id": True},
                {"kind": "campaign", "id": 11},
                11,
            ],
        )
        extra_not_mapping = await _item(
            session,
            run,
            rule_id=K3,
            group="review",
            evidence={"provenance": provenance, "extra": [{"type": "campaign", "id": 11}]},
        )

    before = await _page(sessions, run.id, thresholds=shipped)
    assert before.overlay.open_discrepancies == 0
    assert before.banners == ()
    for view in before.items.values():
        assert view == ItemOverlayView(held_now=False, historical=False, data_status_now="live")

    async with sessions() as session, session.begin():
        await _discrepancy(session, google, "campaign", 11)
        # Neither counts: a resolved discrepancy, and one of a retired dataset.
        await _discrepancy(session, google, "campaign", 12, state="resolved")
        await _discrepancy(session, retired, "campaign", 12, kind="budget")

    after = await _page(sessions, run.id, thresholds=shipped)
    assert after.overlay.open_discrepancies == 1
    assert after.items[c0.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert after.items[keyword.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HELD_DISCREPANCY_REASON,
        historical=False,
        data_status_now="live",
        flags_now=(_unverified(1),),
    )
    for unrelated in (budget, pause, malformed, extra_not_mapping):
        assert after.items[unrelated.id] == ItemOverlayView(
            held_now=False,
            historical=False,
            data_status_now="live",
            flags_now=(_unverified(1),),
        )
    assert after.items[keyword.id].flags_now[0].message == (
        "Unverified while 1 discrepancy is open"
    )
    assert after.banners == (
        BannerView(
            code=BANNER_OPEN_DISCREPANCIES,
            tone="critical",
            message="1 discrepancy to resolve before acting on other recommendations",
            count=1,
        ),
    )
    assert after.banners[0].alert

    async with sessions() as session, session.begin():
        await _discrepancy(session, google, "budget", 21, kind="budget", state="awaiting_platform")

    later = await _page(sessions, run.id, thresholds=shipped)
    assert later.overlay.open_discrepancies == 2
    assert later.items[budget.id].held_reason_now == HELD_DISCREPANCY_REASON
    assert later.items[budget.id].flags_now == (_unverified(2),)
    assert later.items[budget.id].flags_now[0].message == (
        "Unverified while 2 discrepancies are open"
    )
    assert not later.items[pause.id].held_now
    assert later.items[c0.id].flags_now == ()
    assert later.banners[0].message == (
        "2 discrepancies to resolve before acting on other recommendations"
    )
    assert later.banners[0].count == 2

    # The overlay adds holds beside the frozen item; the stored row is never rewritten.
    stored = await _stored(sessions, keyword.id)
    assert (stored.held, stored.held_reason, stored.flags) == (False, None, [])
    assert stored.evidence == keyword.evidence


@pytest.mark.anyio
@pytest.mark.db
async def test_holds_frozen_at_compute_time_are_never_lifted_at_read_time(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        run = await _run(session)
        frozen = await _item(
            session,
            run,
            provenance=_prov(google),
            depends_on=[{"type": "campaign", "id": 11}],
            held=True,
            held_reason=HELD_DISCREPANCY_REASON,
        )
        without_reason = await _item(session, run, provenance=_prov(google), held=True)
        open_item = await _item(session, run, rule_id=K2, provenance=_prov(google))

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.overlay.open_discrepancies == 0
    for held in (frozen, without_reason):
        view = result.items[held.id]
        assert view.held_now
        assert view.held_reason_now == HELD_DISCREPANCY_REASON
        assert not view.historical
        assert view.actions_enabled
    assert not result.items[open_item.id].held_now


# ---------------------------------------------------------------------------------------
# Historical-only items and the effective CRM dataset
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_items_from_a_replaced_dataset_are_historical_and_keep_their_provenance(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        simulated = await _dataset(
            session, "linkedin", mode="simulated", reason="credentials not configured"
        )
        companion = await _companion(session, simulated)
        hubspot = await _dataset(session, "hubspot")
        await _freshness(session, google, "google.search_terms")
        await _freshness(session, simulated, "linkedin.leads")
        await _freshness(session, companion, "hubspot.contacts")
        await _freshness(session, hubspot, "hubspot.contacts")
        custom = await _run(session, kind="custom")
        white_paper = await _item(
            session,
            custom,
            rule_id=W1,
            section="white_papers",
            group="cohort:USD",
            entity_type="content_asset",
            entity_id="buyers-guide|USD",
            provenance=_prov(simulated, companion),
            required=("linkedin.leads", "hubspot.contacts"),
            data_status="simulated",
            flags=(FLAG_SIMULATED,),
        )
        keyword = await _item(
            session, custom, provenance=_prov(google), required=("google.search_terms",)
        )
        frozen_hold = await _item(
            session,
            custom,
            rule_id=K2,
            provenance=_prov(simulated),
            held=True,
            held_reason=HELD_DISCREPANCY_REASON,
        )
    frozen_provenance = _prov(simulated, companion)

    before = await _page(sessions, custom.id, thresholds=shipped)
    assert before.items[white_paper.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="simulated",
        flags_now=(Flag(FLAG_SIMULATED, "Simulated data (LinkedIn, HubSpot)"),),
    )
    async with sessions() as session:
        assert await current_dataset_ids(session) == frozenset(
            {google.id, simulated.id, companion.id, hubspot.id}
        )
        assert await item_is_current(session, white_paper)

    # LinkedIn goes live: the simulated dataset and its companion retire together.
    async with sessions() as session, session.begin():
        live = await _dataset(session, "linkedin")
        await _freshness(session, live, "linkedin.leads")
        rerun = await _run(session)
        rerun_item = await _item(
            session,
            rerun,
            rule_id=W1,
            section="white_papers",
            group="cohort:USD",
            entity_type="content_asset",
            entity_id="buyers-guide|USD",
            provenance=_prov(live, hubspot),
            required=("linkedin.leads", "hubspot.contacts"),
        )

    after = await _page(sessions, custom.id, thresholds=shipped)
    historical = after.items[white_paper.id]
    assert historical == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="live",
    )
    assert historical.held_reason_now == "Computed from data that has since been replaced"
    assert not historical.actions_enabled
    assert after.items[frozen_hold.id].held_reason_now == HISTORICAL_REASON
    assert after.items[keyword.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert after.overlay.source_modes == {"google": "live", "linkedin": "live", "hubspot": "live"}
    assert BANNER_SIMULATED_SOURCES not in {banner.code for banner in after.banners}
    # A historical item's required datasets resolve to the current ones, which are fresh, so
    # the page lists nothing stale or incomplete.
    assert (after.overlay.stale_datasets, after.overlay.incomplete_datasets) == ((), ())

    stored = await _stored(sessions, white_paper.id)
    assert stored.evidence["provenance"] == frozen_provenance
    assert {entry["mode"] for entry in stored.evidence["provenance"]} == {"simulated"}
    assert (stored.data_status, stored.flags) == ("simulated", [FLAG_SIMULATED])

    rerun_page = await _page(sessions, rerun.id, thresholds=shipped)
    assert rerun_page.items[rerun_item.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    async with sessions() as session:
        assert await current_dataset_ids(session) == frozenset({google.id, live.id, hubspot.id})
        assert not await item_is_current(session, stored)
        assert await item_is_current(session, rerun_item)


@pytest.mark.anyio
@pytest.mark.db
async def test_linkedin_evidence_reads_the_companion_while_google_reads_the_crm_primary(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin", mode="simulated")
        companion = await _companion(session, linkedin)
        hubspot = await _dataset(session, "hubspot")
        await _freshness(session, google, "google.metrics_daily")
        await _freshness(session, linkedin, "linkedin.leads")
        await _freshness(session, companion, "hubspot.contacts", success=NOW - timedelta(hours=30))
        await _freshness(session, hubspot, "hubspot.contacts")
        run = await _run(session)
        pipeline = await _item(
            session,
            run,
            rule_id="C1b",
            section="campaigns",
            group="pause",
            entity_type="campaign",
            entity_id="31",
            provenance=_prov(linkedin, companion),
            required=("linkedin.leads", "hubspot.contacts"),
            data_status="simulated",
        )
        google_crm = await _item(
            session,
            run,
            rule_id="C2",
            section="campaigns",
            group="scale",
            entity_type="campaign",
            entity_id="41",
            provenance=_prov(google, hubspot),
            required=("google.metrics_daily", "hubspot.contacts"),
        )
        joined = await _item(
            session,
            run,
            rule_id="B3",
            section="budget",
            group="changes",
            provenance=_prov(google, linkedin),
            data_status="mixed",
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    # Stale never replaces a simulated status; the stale flag still names the data age.
    assert result.items[pipeline.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="simulated",
        flags_now=(
            Flag(FLAG_SIMULATED, "Simulated data (LinkedIn, HubSpot)"),
            Flag(FLAG_STALE, "Data as of Oct 6, 2026, 6:00 AM UTC"),
        ),
    )
    assert result.items[google_crm.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert result.items[joined.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="mixed",
        flags_now=(Flag(FLAG_SIMULATED, "Joins live and simulated data (LinkedIn simulated)"),),
    )
    assert result.overlay.source_modes == {
        "google": "live",
        "linkedin": "simulated",
        "hubspot": "live",
    }
    assert result.overlay.stale_datasets == ("hubspot.contacts",)
    assert result.overlay.incomplete_datasets == ()
    assert [banner.to_json() for banner in result.banners] == [
        {
            "code": BANNER_SIMULATED_SOURCES,
            "tone": "info",
            "message": "Simulated data: LinkedIn (simulated data mode)",
            "count": 1,
        },
        {
            "code": BANNER_STALE_DATASETS,
            "tone": "warning",
            "message": "Stale data: hubspot.contacts (last success Oct 6, 2026, 6:00 AM UTC)",
            "count": 1,
        },
    ]


# ---------------------------------------------------------------------------------------
# P3: the simulated banner
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_the_simulated_banner_names_each_simulated_source_with_its_reason(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google", mode="simulated")
        linkedin = await _dataset(session, "linkedin", mode="simulated")
        hubspot = await _dataset(
            session, "hubspot", mode="simulated", reason="credentials not configured"
        )
        await _connection(session, "google", "simulated", "access denied: 403 on search", google)
        # A connection already back in live mode lends no reason to the simulated dataset.
        await _connection(session, "linkedin", "live", "recovered", linkedin)
        # A simulated connection's current reason wins over the reason its dataset was built
        # with.
        await _connection(session, "hubspot", "simulated", "probe failed", hubspot)
        run = await _run(session)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items == {}
    assert result.overlay.source_modes == {
        "google": "simulated",
        "linkedin": "simulated",
        "hubspot": "simulated",
    }
    assert result.banners == (
        BannerView(
            code=BANNER_SIMULATED_SOURCES,
            tone="info",
            message=(
                "Simulated data: Google (access denied: 403 on search); "
                "LinkedIn (simulated data mode); HubSpot (probe failed)"
            ),
            count=3,
        ),
    )
    assert not result.banners[0].alert


@pytest.mark.anyio
@pytest.mark.db
async def test_the_simulated_banner_names_the_denial_that_keeps_a_source_simulated(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # First start without credentials builds the dataset; credentials are then added and the
    # probe's 401 puts Google into automatic fallback, keeping that dataset active.
    denial = "Q-G1: HTTP 401: Authorization data is wrong!"
    async with sessions() as session, session.begin():
        google = await _dataset(
            session, "google", mode="simulated", reason="credentials not configured"
        )
        await _connection(session, "google", "simulated", denial, google)
        await _covered(session, "linkedin", "hubspot")
        run = await _run(session)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.banners == (
        BannerView(
            code=BANNER_SIMULATED_SOURCES,
            tone="info",
            message=f"Simulated data: Google ({denial})",
            count=1,
        ),
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_the_simulated_banner_names_a_forced_mode_over_the_dataset_reason(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # A stack first started without credentials restarts under DATA_MODE=simulated: every
    # connection reports the forced mode while each kept dataset keeps the reason it was
    # built with.
    async with sessions() as session, session.begin():
        for source in ("google", "linkedin", "hubspot"):
            dataset = await _dataset(
                session, source, mode="simulated", reason="credentials not configured"
            )
            await _connection(session, source, "simulated", "DATA_MODE=simulated", dataset)
        run = await _run(session)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.banners == (
        BannerView(
            code=BANNER_SIMULATED_SOURCES,
            tone="info",
            message=(
                "Simulated data: Google (DATA_MODE=simulated); "
                "LinkedIn (DATA_MODE=simulated); HubSpot (DATA_MODE=simulated)"
            ),
            count=3,
        ),
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_the_simulated_banner_falls_back_to_the_dataset_reason_for_a_blank_connection(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(
            session, "google", mode="simulated", reason="credentials not configured"
        )
        linkedin = await _dataset(
            session, "linkedin", mode="simulated", reason="simulated data seeded on request"
        )
        hubspot = await _dataset(session, "hubspot", mode="simulated")
        await _connection(session, "google", "simulated", None, google)
        await _connection(session, "linkedin", "simulated", "   ", linkedin)
        # Neither the connection nor the dataset records a reason: the default names the mode.
        await _connection(session, "hubspot", "simulated", "", hubspot)
        run = await _run(session)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.banners == (
        BannerView(
            code=BANNER_SIMULATED_SOURCES,
            tone="info",
            message=(
                "Simulated data: Google (credentials not configured); "
                "LinkedIn (simulated data seeded on request); HubSpot (simulated data mode)"
            ),
            count=3,
        ),
    )


# ---------------------------------------------------------------------------------------
# P4: stale and incomplete, per item from its required datasets
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_stale_and_incomplete_flags_derive_from_each_items_required_datasets(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin")
        await _freshness(session, google, "google.entities")
        await _freshness(session, google, "google.metrics_daily", success=OLD)
        await _freshness(
            session,
            google,
            "google.search_terms",
            through=WINDOW_END - timedelta(days=1),
            completeness="incomplete",
            reason="result limit reached at finest partition",
        )
        await _freshness(session, linkedin, "linkedin.entities", success=NOW - timedelta(hours=24))
        await _freshness(
            session,
            linkedin,
            "linkedin.analytics_daily",
            success=NOW - timedelta(hours=24, seconds=1),
        )
        await _freshness(
            session,
            linkedin,
            "linkedin.segments",
            success=None,
            through=None,
            completeness="unknown",
        )
        await _freshness(session, linkedin, "linkedin.leads", through=None)
        run = await _run(session)
        entities = await _item(
            session, run, provenance=_prov(google), required=("google.entities",)
        )
        keyword = await _item(
            session,
            run,
            rule_id=K2,
            provenance=_prov(google),
            required=("google.search_terms", "google.metrics_daily", "google.metrics_daily"),
        )
        at_limit = await _item(
            session, run, rule_id="CR1", provenance=_prov(linkedin), required=("linkedin.entities",)
        )
        past_limit = await _item(
            session,
            run,
            rule_id="CR2",
            provenance=_prov(linkedin),
            required=("linkedin.analytics_daily",),
        )
        never = await _item(
            session,
            run,
            rule_id="C3a",
            provenance=_prov(linkedin),
            required=("linkedin.segments", "linkedin.leads"),
        )
        # LinkedIn is live and HubSpot has no active dataset, so no CRM dataset is current.
        no_crm = await _item(
            session,
            run,
            rule_id="W2",
            provenance=_prov(linkedin),
            required=("hubspot.deal_search", "linkedin.entities"),
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items[entities.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert result.items[keyword.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="stale",
        flags_now=(
            Flag(
                FLAG_STALE,
                "Data as of Oct 6, 2026, 11:00 AM UTC; "
                "google.search_terms covers through 2026-10-05",
            ),
            # Both shortfalls are named: the recorded reason and the missing last day.
            Flag(
                FLAG_INCOMPLETE,
                "Incomplete: google.search_terms (result limit reached at finest partition; "
                "covers through 2026-10-05, before the window end 2026-10-06)",
            ),
        ),
    )
    # Exactly sources.stale_after_hours old is not stale; one second more is.
    assert result.items[at_limit.id].flags_now == ()
    assert result.items[past_limit.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="stale",
        flags_now=(Flag(FLAG_STALE, "Data as of Oct 6, 2026, 11:59 AM UTC"),),
    )
    assert result.items[never.id].flags_now == (
        Flag(
            FLAG_STALE,
            "Data as of: no successful sync of linkedin.segments; linkedin.leads covers no "
            "dates yet",
        ),
        # A dated dataset that covers no dates is not complete over the window either; an
        # unknown one with no coverage names both shortfalls.
        Flag(
            FLAG_INCOMPLETE,
            "Incomplete: linkedin.segments (unknown; covers no dates); "
            "linkedin.leads (covers no dates)",
        ),
    )
    assert result.items[no_crm.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="stale",
        flags_now=(
            Flag(FLAG_STALE, "Data as of: no successful sync of hubspot.deal_search"),
            Flag(FLAG_INCOMPLETE, "Incomplete: hubspot.deal_search (no freshness record)"),
        ),
    )
    assert result.overlay.source_modes == {"google": "live", "linkedin": "live", "hubspot": None}
    # The page lists what the cards flag; HubSpot has no active dataset at all, so every one
    # of its datasets is listed and both banners name them together, once.
    hubspot = ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search")
    no_hubspot = f"{', '.join(hubspot)}, hubspot.google_contacts (no active HubSpot dataset)"
    assert result.overlay.stale_datasets == (
        "google.metrics_daily",
        "google.search_terms",
        "linkedin.analytics_daily",
        "linkedin.segments",
        "linkedin.leads",
        *hubspot,
        "hubspot.google_contacts",
    )
    assert result.overlay.incomplete_datasets == (
        "google.search_terms",
        "linkedin.segments",
        "linkedin.leads",
        *hubspot,
        "hubspot.google_contacts",
    )
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=(
                "Stale data: google.metrics_daily (last success Oct 6, 2026, 11:00 AM UTC), "
                "google.search_terms (last success Oct 7, 2026, 11:00 AM UTC), "
                "linkedin.analytics_daily (last success Oct 6, 2026, 11:59 AM UTC), "
                "linkedin.segments (no successful sync), "
                f"linkedin.leads (last success Oct 7, 2026, 11:00 AM UTC), {no_hubspot}"
            ),
            count=9,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=(
                "Incomplete data: google.search_terms (result limit reached at finest "
                "partition; covers through 2026-10-05, before the window end 2026-10-06), "
                "linkedin.segments (unknown; covers no dates), "
                f"linkedin.leads (covers no dates), {no_hubspot}"
            ),
            count=7,
        ),
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_staleness_reads_the_configured_stale_after_hours(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        await _freshness(session, google, "google.metrics_daily", success=OLD)
        await _covered(session, "linkedin", "hubspot")
        run = await _run(session)
        item = await _item(
            session, run, provenance=_prov(google), required=("google.metrics_daily",)
        )

    assert shipped.int("sources.stale_after_hours") == 24
    strict = await _page(sessions, run.id, thresholds=shipped)
    assert strict.items[item.id].data_status_now == "stale"
    lenient = await _page(sessions, run.id, thresholds=_thresholds("sources", stale_after_hours=48))
    assert lenient.items[item.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert lenient.overlay.stale_datasets == ()
    assert lenient.banners == ()


@pytest.mark.anyio
@pytest.mark.db
async def test_coverage_starting_after_the_window_start_is_incomplete_over_the_window(
    sessions: Sessions, shipped: Thresholds
) -> None:
    window_start, window_end = date(2026, 9, 4), date(2026, 10, 1)
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin")
        # The review's reproduction: complete and fresh, but only from 2026-09-20.
        await _freshness(
            session,
            google,
            "google.metrics_daily",
            covered_from=date(2026, 9, 20),
            through=window_end,
        )
        # Entity syncs re-read full current state and record no date range.
        await _freshness(session, google, "google.entities", through=None)
        await _freshness(session, linkedin, "linkedin.entities", success=OLD, through=None)
        await _covered(session, "hubspot")
        run = await _run(session, start=window_start, end=window_end)
        # K2 reads no lookback, so its required range is exactly the run's window (a K1
        # item's google.metrics_daily must also cover its baseline: tested below).
        metrics = await _item(
            session,
            run,
            rule_id=K2,
            provenance=_prov(google),
            required=("google.metrics_daily", "google.entities"),
        )
        entities = await _item(
            session, run, rule_id="CR1", provenance=_prov(linkedin), required=("linkedin.entities",)
        )
        covered = await _run(session, start=date(2026, 9, 20), end=window_end, kind="custom")
        boundary = await _item(
            session,
            covered,
            rule_id=K2,
            provenance=_prov(google),
            required=("google.metrics_daily",),
        )

    shortfall = "google.metrics_daily (covers from 2026-09-20, after the window start 2026-09-04)"
    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items[metrics.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="live",
        flags_now=(Flag(FLAG_INCOMPLETE, f"Incomplete: {shortfall}"),),
    )
    # A full-state dataset is stale by age alone, with no coverage shortfall to name.
    assert result.items[entities.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="stale",
        flags_now=(Flag(FLAG_STALE, "Data as of Oct 6, 2026, 11:00 AM UTC"),),
    )
    assert result.overlay.stale_datasets == ("linkedin.entities",)
    assert result.overlay.incomplete_datasets == ("google.metrics_daily",)
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message="Stale data: linkedin.entities (last success Oct 6, 2026, 11:00 AM UTC)",
            count=1,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=f"Incomplete data: {shortfall}",
            count=1,
        ),
    )
    assert "(complete)" not in str(result.to_json())

    # A window that starts where the coverage starts is complete over the window.
    exact = await _page(sessions, covered.id, thresholds=shipped)
    assert exact.items[boundary.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert exact.overlay.incomplete_datasets == ()
    assert BANNER_INCOMPLETE_DATASETS not in {banner.code for banner in exact.banners}


@pytest.mark.anyio
@pytest.mark.db
async def test_a_finest_partition_day_outside_the_window_flags_nothing(
    sessions: Sessions, shipped: Thresholds
) -> None:
    """A Q-G6 shortfall flags only windows that hold one of its days.

    ``google.search_terms`` is incomplete for two (day, ad group) partitions at the result
    limit, 2026-09-01 and 2026-10-08. The default window (2026-09-09 to 2026-10-06) holds
    neither, so its items and page carry no ``incomplete`` flag or banner; a window ending
    2026-10-08 is flagged, naming that day alone.
    """
    finest = "result limit reached at finest partition"
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        await _freshness(session, google, "google.entities", through=None)
        await _freshness(session, google, "google.metrics_daily", through=date(2026, 10, 8))
        await _freshness(
            session,
            google,
            "google.search_terms",
            through=date(2026, 10, 8),
            completeness="incomplete",
            reason=(
                f"{finest} (2026-09-01, ad group 990200000000; 2026-10-08, ad group 990200000000)"
            ),
        )
        await _covered(session, "linkedin", "hubspot", through=date(2026, 10, 8))
        excluding = await _run(session)
        outside = await _item(
            session,
            excluding,
            rule_id=K2,
            provenance=_prov(google),
            required=("google.search_terms", "google.metrics_daily"),
        )
        including = await _run(
            session, start=date(2026, 9, 11), end=date(2026, 10, 8), kind="custom"
        )
        inside = await _item(
            session,
            including,
            rule_id=K2,
            provenance=_prov(google),
            required=("google.search_terms", "google.metrics_daily"),
        )

    clear = await _page(sessions, excluding.id, thresholds=shipped)
    assert clear.items[outside.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert clear.overlay.incomplete_datasets == ()
    assert clear.banners == ()

    named = f"google.search_terms ({finest} (2026-10-08, ad group 990200000000))"
    flagged = await _page(sessions, including.id, thresholds=shipped)
    assert flagged.items[inside.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="live",
        flags_now=(Flag(FLAG_INCOMPLETE, f"Incomplete: {named}"),),
    )
    assert flagged.overlay.incomplete_datasets == ("google.search_terms",)
    assert flagged.banners == (
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=f"Incomplete data: {named}",
            count=1,
        ),
    )
    assert "2026-09-01" not in str(flagged.to_json())


def _k1_evidence(
    run: AnalysisRun,
    provenance: list[dict[str, Any]],
    *,
    thresholds: dict[str, Any],
    baseline_window: object = None,
) -> dict[str, Any]:
    """Return a K1 item's frozen evidence; ``extra.baseline_window`` only when given."""
    extra: dict[str, Any] = {"term_status": "NONE"}
    if baseline_window is not None:
        extra["baseline_window"] = baseline_window
    return {
        "window": {
            "start": run.window_start.isoformat(),
            "end": run.window_end.isoformat(),
            "days": (run.window_end - run.window_start).days + 1,
            "source_days": [],
        },
        "metrics": [],
        "baselines": [],
        "thresholds": thresholds,
        "datasets": [],
        "provenance": provenance,
        "extra": extra,
    }


@pytest.mark.anyio
@pytest.mark.db
async def test_a_k1_items_metrics_must_cover_its_frozen_baseline_window(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # The verifier's reproduction: K1's baseline rate reads google.metrics_daily over the
    # trailing k1.baseline_days ending at the window end (AAP 0.5.4.3), so a K1 item needs
    # that dataset complete from the baseline start, not only from the window start.
    window_start, window_end = date(2026, 9, 4), date(2026, 10, 1)
    baseline_days = shipped.int("k1.baseline_days")
    baseline_start = window_end - timedelta(days=baseline_days - 1)
    assert baseline_start == date(2026, 7, 4)
    frozen_window = {
        "start": baseline_start.isoformat(),
        "end": window_end.isoformat(),
        "days": baseline_days,
    }
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        for name in ("google.search_terms", "google.metrics_daily"):
            await _freshness(session, google, name, covered_from=window_start, through=window_end)
        await _covered(session, "linkedin", "hubspot")
        run = await _run(session, start=window_start, end=window_end)
        required = ("google.search_terms", "google.metrics_daily")

        async def k1(entity_id: str, evidence: dict[str, Any]) -> Recommendation:
            return await _item(
                session, run, entity_id=entity_id, required=required, evidence=evidence
            )

        frozen = await k1(
            "7|frozen",
            _k1_evidence(
                run,
                _prov(google),
                thresholds={"k1.baseline_days": baseline_days},
                baseline_window=frozen_window,
            ),
        )
        # A non-K1 item reading the same datasets needs only the run's window.
        expand = await _item(
            session,
            run,
            rule_id=K2,
            entity_id="7|expand",
            provenance=_prov(google),
            required=required,
        )
        # No frozen baseline window and no frozen length: the configured k1.baseline_days.
        missing = await k1("7|missing", _k1_evidence(run, _prov(google), thresholds={}))
        # A malformed frozen start falls back to the frozen length, never to the window start.
        frozen_days = await k1(
            "7|frozen-days",
            _k1_evidence(
                run,
                _prov(google),
                thresholds={"k1.baseline_days": 60},
                baseline_window={**frozen_window, "start": "2026-13-01"},
            ),
        )
        # Malformed start and length alike: the configured length.
        malformed = [
            await k1(
                f"7|malformed-{index}",
                _k1_evidence(
                    run,
                    _prov(google),
                    thresholds=frozen_thresholds,
                    baseline_window=baseline_window,
                ),
            )
            for index, (baseline_window, frozen_thresholds) in enumerate(
                [
                    ("2026-07-04", {"k1.baseline_days": True}),
                    ({"end": window_end.isoformat()}, {"k1.baseline_days": "90"}),
                    ({**frozen_window, "start": None}, {"k1.baseline_days": 0}),
                    ({**frozen_window, "start": "2026-07-04T00:00:00"}, {}),
                    ({**frozen_window, "start": 20260704}, {"k1.baseline_days": 12.5}),
                ]
            )
        ]

    baseline = "covers from 2026-09-04, after the K1 baseline start 2026-07-04"
    sixty_days = "covers from 2026-09-04, after the K1 baseline start 2026-08-03"
    result = await _page(sessions, run.id, thresholds=shipped)
    for item in (frozen, missing, *malformed):
        assert result.items[item.id] == ItemOverlayView(
            held_now=False,
            historical=False,
            data_status_now="live",
            flags_now=(Flag(FLAG_INCOMPLETE, f"Incomplete: google.metrics_daily ({baseline})"),),
        ), item.evidence
    assert result.items[frozen_days.id].flags_now == (
        Flag(FLAG_INCOMPLETE, f"Incomplete: google.metrics_daily ({sixty_days})"),
    )
    assert result.items[expand.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    # The K2 card's complete check of google.metrics_daily never masks the K1 cards' checks.
    assert result.overlay.stale_datasets == ()
    assert result.overlay.incomplete_datasets == ("google.metrics_daily",)
    incomplete_banner = BannerView(
        code=BANNER_INCOMPLETE_DATASETS,
        tone="warning",
        message=f"Incomplete data: google.metrics_daily ({baseline}; {sixty_days})",
        count=1,
    )
    assert result.banners == (incomplete_banner,)
    assert "(complete)" not in str(result.to_json())

    # The page reads the same in any item order.
    async with sessions() as session:
        stored_run = await session.get(AnalysisRun, run.id)
        assert stored_run is not None
        items = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.run_id == run.id)
                .order_by(Recommendation.id.desc())
            )
        )
        reversed_page = await overlay(
            session, stored_run, items, NOW, thresholds=shipped, display_timezone="UTC"
        )
    assert reversed_page.overlay == result.overlay
    assert reversed_page.banners == result.banners

    # Only an item with no usable frozen value reads the configured length.
    shorter = await _page(sessions, run.id, thresholds=_thresholds("k1", baseline_days=30))
    assert shorter.items[missing.id].flags_now == (
        Flag(
            FLAG_INCOMPLETE,
            "Incomplete: google.metrics_daily "
            "(covers from 2026-09-04, after the K1 baseline start 2026-09-02)",
        ),
    )
    assert shorter.items[frozen.id] == result.items[frozen.id]
    assert shorter.items[frozen_days.id] == result.items[frozen_days.id]
    # A lookback no longer than the window leaves the window as the required range.
    window_long = await _page(sessions, run.id, thresholds=_thresholds("k1", baseline_days=28))
    assert window_long.items[missing.id].flags_now == ()

    # Coverage from the baseline start completes every K1 item.
    async with sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(
                DatasetFreshness.dataset_id == google.id,
                DatasetFreshness.dataset == "google.metrics_daily",
            )
            .values(covered_from=baseline_start)
        )
    covered = await _page(sessions, run.id, thresholds=shipped)
    assert all(view.flags_now == () for view in covered.items.values())
    assert covered.overlay.incomplete_datasets == ()
    assert covered.banners == ()
    # The overlay never rewrites the frozen evidence it read.
    assert (await _stored(sessions, frozen.id)).evidence == frozen.evidence


@pytest.mark.anyio
@pytest.mark.db
async def test_stale_banner_and_flags_render_instants_in_the_display_timezone(
    sessions: Sessions, shipped: Thresholds
) -> None:
    """AAP 0.5.5: instants are formatted in ``DISPLAY_TIMEZONE``, as the web formats them.

    The stale banner's "last success …" and the ``stale`` flag's "Data as of …" read
    America/New_York time (EDT, UTC-4) beside the web's "Times shown in America/New_York".
    The zone only formats instants (AAP 0.5.4.1): coverage ends stay ISO native dates, and
    which datasets are stale does not change with it.
    """
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin")
        await _freshness(
            session,
            google,
            "google.metrics_daily",
            success=OLD,
            through=WINDOW_END - timedelta(days=1),
        )
        await _freshness(session, linkedin, "linkedin.analytics_daily", success=OLD)
        await _covered(session, "hubspot")
        run = await _run(session)
        expand = await _item(
            session, run, rule_id=K2, provenance=_prov(google), required=("google.metrics_daily",)
        )

    zoned = await _page(sessions, run.id, thresholds=shipped, display_timezone="America/New_York")
    utc = await _page(sessions, run.id, thresholds=shipped)
    zoned_flags = {flag.code: flag.message for flag in zoned.items[expand.id].flags_now}
    assert zoned_flags[FLAG_STALE] == (
        "Data as of Oct 6, 2026, 7:00 AM America/New_York; "
        "google.metrics_daily covers through 2026-10-05"
    )
    utc_flags = {flag.code: flag.message for flag in utc.items[expand.id].flags_now}
    assert utc_flags[FLAG_STALE] == (
        "Data as of Oct 6, 2026, 11:00 AM UTC; google.metrics_daily covers through 2026-10-05"
    )
    zoned_banners = {banner.code: banner for banner in zoned.banners}
    assert zoned_banners[BANNER_STALE_DATASETS] == BannerView(
        code=BANNER_STALE_DATASETS,
        tone="warning",
        message=(
            "Stale data: google.metrics_daily (last success Oct 6, 2026, 7:00 AM "
            "America/New_York), linkedin.analytics_daily (last success Oct 6, 2026, 7:00 AM "
            "America/New_York)"
        ),
        count=2,
    )
    # Only the instants differ: the stale lists, the incomplete flag and its banner do not.
    assert zoned.overlay == utc.overlay
    assert zoned_flags[FLAG_INCOMPLETE] == utc_flags[FLAG_INCOMPLETE]
    assert {banner.code: banner for banner in utc.banners}[BANNER_INCOMPLETE_DATASETS] == (
        zoned_banners[BANNER_INCOMPLETE_DATASETS]
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_coverage_ending_before_the_window_end_is_stale_and_incomplete(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # AAP 0.3.2: stale when a dataset covers less than the window end, and incomplete when it
    # is not complete over the window, which coverage missing the window's last day is not.
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin")
        await _freshness(
            session, google, "google.metrics_daily", through=WINDOW_END - timedelta(days=1)
        )
        # A row no item requires is judged over the run's window too.
        await _freshness(
            session, linkedin, "linkedin.analytics_daily", through=WINDOW_END - timedelta(days=2)
        )
        await _covered(session, "hubspot")
        run = await _run(session)
        expand = await _item(
            session, run, rule_id=K2, provenance=_prov(google), required=("google.metrics_daily",)
        )

    end_shortfall = "covers through 2026-10-05, before the window end 2026-10-06"
    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items[expand.id] == ItemOverlayView(
        held_now=False,
        historical=False,
        data_status_now="stale",
        flags_now=(
            Flag(
                FLAG_STALE,
                "Data as of Oct 7, 2026, 11:00 AM UTC; "
                "google.metrics_daily covers through 2026-10-05",
            ),
            Flag(FLAG_INCOMPLETE, f"Incomplete: google.metrics_daily ({end_shortfall})"),
        ),
    )
    both = ("google.metrics_daily", "linkedin.analytics_daily")
    assert result.overlay.stale_datasets == both
    assert result.overlay.incomplete_datasets == both
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=(
                "Stale data: google.metrics_daily (last success Oct 7, 2026, 11:00 AM UTC), "
                "linkedin.analytics_daily (last success Oct 7, 2026, 11:00 AM UTC)"
            ),
            count=2,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=(
                f"Incomplete data: google.metrics_daily ({end_shortfall}), "
                "linkedin.analytics_daily "
                "(covers through 2026-10-04, before the window end 2026-10-06)"
            ),
            count=2,
        ),
    )

    # Short at both ends: a K1 item names both shortfalls; the page names each reason once.
    async with sessions() as session, session.begin():
        await session.execute(
            update(DatasetFreshness)
            .where(
                DatasetFreshness.dataset_id == google.id,
                DatasetFreshness.dataset == "google.metrics_daily",
            )
            .values(covered_from=WINDOW_START)
        )
        stored_run = await session.get(AnalysisRun, run.id)
        assert stored_run is not None
        block = await _item(
            session, stored_run, provenance=_prov(google), required=("google.metrics_daily",)
        )
    start_shortfall = "covers from 2026-09-09, after the K1 baseline start 2026-07-09"
    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items[block.id].flags_now[1] == Flag(
        FLAG_INCOMPLETE,
        f"Incomplete: google.metrics_daily ({start_shortfall}; {end_shortfall})",
    )
    assert result.items[expand.id].flags_now[1] == Flag(
        FLAG_INCOMPLETE, f"Incomplete: google.metrics_daily ({end_shortfall})"
    )
    assert result.overlay.incomplete_datasets == both
    assert result.banners[1] == BannerView(
        code=BANNER_INCOMPLETE_DATASETS,
        tone="warning",
        message=(
            f"Incomplete data: google.metrics_daily ({start_shortfall}; {end_shortfall}), "
            "linkedin.analytics_daily "
            "(covers through 2026-10-04, before the window end 2026-10-06)"
        ),
        count=2,
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_the_page_lists_and_banners_name_required_datasets_without_freshness(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin")
        await _freshness(session, google, "google.metrics_daily")
        # A stale row no item requires still reaches the page, as before.
        await _freshness(session, linkedin, "linkedin.leads", success=OLD)
        run = await _run(session)
        # google.search_terms has no freshness row; HubSpot has no active dataset at all.
        keyword = await _item(
            session,
            run,
            provenance=_prov(google),
            required=("google.search_terms", "google.metrics_daily"),
        )
        expand = await _item(
            session, run, rule_id=K2, provenance=_prov(google), required=("google.search_terms",)
        )
        pipeline = await _item(
            session,
            run,
            rule_id="C1b",
            section="campaigns",
            group="pause",
            entity_type="campaign",
            entity_id="31",
            provenance=_prov(linkedin),
            required=("hubspot.contacts",),
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    for item, name in ((keyword, "google.search_terms"), (expand, "google.search_terms")):
        assert result.items[item.id].flags_now == (
            Flag(FLAG_STALE, f"Data as of: no successful sync of {name}"),
            Flag(FLAG_INCOMPLETE, f"Incomplete: {name} (no freshness record)"),
        )
    assert result.items[pipeline.id].flags_now == (
        Flag(FLAG_STALE, "Data as of: no successful sync of hubspot.contacts"),
        Flag(FLAG_INCOMPLETE, "Incomplete: hubspot.contacts (no freshness record)"),
    )
    # Each dataset counts once, however many cards require it. HubSpot has no active dataset,
    # so every one of its datasets is listed, and both banners name them together, once.
    hubspot = (
        "hubspot.contacts",
        "hubspot.associations",
        "hubspot.deal_search",
        "hubspot.google_contacts",
    )
    no_hubspot = f"{', '.join(hubspot)} (no active HubSpot dataset)"
    assert result.overlay.stale_datasets == ("google.search_terms", "linkedin.leads", *hubspot)
    assert result.overlay.incomplete_datasets == ("google.search_terms", *hubspot)
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=(
                "Stale data: google.search_terms (no successful sync), "
                f"linkedin.leads (last success Oct 6, 2026, 11:00 AM UTC), {no_hubspot}"
            ),
            count=6,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=f"Incomplete data: google.search_terms (no freshness record), {no_hubspot}",
            count=5,
        ),
    )
    # Every dataset a card flags is on the page.
    for view in result.items.values():
        for flag in view.flags_now:
            names = result.overlay.stale_datasets
            if flag.code == FLAG_INCOMPLETE:
                names = result.overlay.incomplete_datasets
            assert any(name in flag.message for name in names), flag

    # Only the items on the page contribute: a page without them lists the rows and the
    # source with no active dataset alone.
    rows_only = await _page_of(sessions, run.id, [], shipped)
    assert rows_only.overlay.stale_datasets == ("linkedin.leads", *hubspot)
    assert rows_only.overlay.incomplete_datasets == hubspot


@pytest.mark.anyio
@pytest.mark.db
async def test_a_source_with_no_active_dataset_is_named_with_its_status_and_reason(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # A Google probe defect at first start: no fallback and no mock (AAP 0.3.3), so Google
    # has no active dataset and Keywords is empty. The page still says why.
    defect = "probe Q-G1: QueryError (query Q-G1)"
    google_names = ("google.entities", "google.metrics_daily", "google.search_terms")
    async with sessions() as session, session.begin():
        retired = await _dataset(session, "google", state="retired")
        linkedin, hubspot = await _covered(session, "linkedin", "hubspot")
        await _connection(session, "google", "live", defect, None, status="error")
        await _connection(session, "linkedin", "live", None, linkedin)
        # A degraded source that still has its active dataset adds no source part.
        await _connection(session, "hubspot", "live", "HTTP 503", hubspot, status="stale")
        run = await _run(session)
        pause = await _item(
            session,
            run,
            rule_id=C1,
            section="campaigns",
            group="pause",
            entity_type="campaign",
            entity_id="31",
            provenance=_prov(linkedin),
            required=("linkedin.entities", "linkedin.analytics_daily"),
        )
        # An older item of the replaced Google dataset, with K1's wider metrics range.
        replaced = await _item(
            session,
            run,
            evidence=_k1_evidence(run, _prov(retired), thresholds={"k1.baseline_days": 90}),
            required=("google.search_terms", "google.metrics_daily"),
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.overlay.source_modes == {"google": None, "linkedin": "live", "hubspot": "live"}
    assert result.overlay.stale_datasets == google_names
    assert result.overlay.incomplete_datasets == google_names
    listed = ", ".join(google_names)
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=(
                f"Stale data: {listed} (no active Google dataset; Google status error: {defect})"
            ),
            count=3,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=f"Incomplete data: {listed} (no active Google dataset)",
            count=3,
        ),
    )
    assert result.items[pause.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    # The replaced item's own cards still say what it reads; the page counts each name once.
    assert result.items[replaced.id].historical
    assert result.items[replaced.id].flags_now[1] == Flag(
        FLAG_INCOMPLETE,
        "Incomplete: google.search_terms (no freshness record); "
        "google.metrics_daily (no freshness record)",
    )

    # The same page with no item at all, as an empty custom run reads.
    empty = await _page_of(sessions, run.id, [], shipped)
    assert empty.overlay.stale_datasets == google_names
    assert empty.banners == result.banners

    # The status and reason are named only when the status is not ok and a reason exists.
    for status, reason in (("ok", defect), ("error", None)):
        async with sessions() as session, session.begin():
            await session.execute(
                update(SourceConnection)
                .where(SourceConnection.source == "google")
                .values(status=status, reason=reason)
            )
        plain = await _page(sessions, run.id, thresholds=shipped)
        assert plain.banners[0] == BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=f"Stale data: {listed} (no active Google dataset)",
            count=3,
        )
        assert plain.banners[1] == result.banners[1]

    # Without any source_connection row the source is still named.
    async with sessions() as session, session.begin():
        await session.execute(text("DELETE FROM source_connection WHERE source = 'google'"))
    bare = await _page(sessions, run.id, thresholds=shipped)
    assert bare.banners[0].message == f"Stale data: {listed} (no active Google dataset)"


# ---------------------------------------------------------------------------------------
# Items without a usable provenance
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_without_active_datasets_any_provenance_is_historical(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        retired = await _dataset(session, "google", state="retired")
        run = await _run(session)
        replaced = await _item(
            session, run, provenance=_prov(retired), required=("google.metrics_daily",)
        )
        bare_simulated = await _item(
            session, run, evidence={"metrics": []}, data_status="simulated"
        )
        bare_mixed = await _item(session, run, rule_id=K2, evidence={}, data_status="mixed")
        bare_stale = await _item(session, run, rule_id=K3, evidence={}, data_status="stale")
        not_a_list = await _item(session, run, rule_id="B2", evidence={"provenance": "google"})
        not_an_object = await _item(session, run, rule_id="B4", evidence={"provenance": [3]})
        not_a_mapping = await _item(session, run, rule_id="CR3", evidence=["provenance"])
        unknown_mode = await _item(
            session,
            run,
            rule_id="C3b",
            evidence={"provenance": [{"source": "google", "dataset_id": retired.id}]},
            data_status="simulated",
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.overlay == OverlayView(
        open_discrepancies=0,
        source_modes={"google": None, "linkedin": None, "hubspot": None},
        stale_datasets=DATASETS,
        incomplete_datasets=DATASETS,
        computed_at=NOW,
    )
    # No source has a current dataset: the page lists every dataset, and both banners name
    # each source's datasets together, once.
    none_active = (
        "google.entities, google.metrics_daily, google.search_terms (no active Google dataset), "
        "linkedin.entities, linkedin.analytics_daily, linkedin.segments, linkedin.leads "
        "(no active LinkedIn dataset), hubspot.contacts, hubspot.associations, "
        "hubspot.deal_search, hubspot.google_contacts (no active HubSpot dataset)"
    )
    assert result.banners == (
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message=f"Stale data: {none_active}",
            count=len(DATASETS),
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message=f"Incomplete data: {none_active}",
            count=len(DATASETS),
        ),
    )
    # The frozen mode stands in for a source that has no current dataset.
    assert result.items[replaced.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="stale",
        flags_now=(
            Flag(FLAG_STALE, "Data as of: no successful sync of google.metrics_daily"),
            Flag(FLAG_INCOMPLETE, "Incomplete: google.metrics_daily (no freshness record)"),
        ),
    )
    # No provenance is unverifiable: historical, with every action disabled; the frozen
    # simulated or mixed status still labels the card.
    assert result.items[bare_simulated.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="simulated",
        flags_now=(Flag(FLAG_SIMULATED, "Simulated data"),),
    )
    assert result.items[bare_mixed.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="mixed",
        flags_now=(Flag(FLAG_SIMULATED, "Joins live and simulated data"),),
    )
    assert result.items[bare_stale.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="live",
    )
    for view in result.items.values():
        assert not view.actions_enabled
    for malformed in (not_a_list, not_an_object, not_a_mapping):
        assert result.items[malformed.id] == ItemOverlayView(
            held_now=True,
            held_reason_now=HISTORICAL_REASON,
            historical=True,
            data_status_now="live",
        )
    # An undecodable entry carries no mode, so the frozen data status stands.
    assert result.items[unknown_mode.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="simulated",
        flags_now=(Flag(FLAG_SIMULATED, "Simulated data"),),
    )
    async with sessions() as session:
        assert await current_dataset_ids(session) == frozenset()
        assert not await item_is_current(session, replaced)
        assert not await item_is_current(session, not_a_mapping)
        assert not await item_is_current(session, bare_simulated)


@pytest.mark.anyio
@pytest.mark.db
async def test_unverifiable_provenance_is_historical_even_when_its_ids_are_current(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        await _freshness(session, google, "google.metrics_daily")
        others = await _covered(session, "linkedin", "hubspot")
        run = await _run(session)
        complete = _prov(google)[0]
        required = ("google.metrics_daily",)
        current = await _item(session, run, provenance=[complete], required=required)
        unverifiable_evidence: list[dict[str, Any]] = [
            {"metrics": []},
            {"provenance": []},
            {"provenance": [{key: value for key, value in complete.items() if key != "source"}]},
            {"provenance": [{key: value for key, value in complete.items() if key != "mode"}]},
            {"provenance": [{key: value for key, value in complete.items() if key != "role"}]},
            {"provenance": [{**complete, "source": "posthog"}]},
            {"provenance": [{**complete, "mode": "mock"}]},
            {"provenance": [{**complete, "role": "parent"}]},
            {"provenance": [complete, {**complete, "role": None}]},
        ]
        unverifiable = [
            await _item(
                session,
                run,
                entity_id=f"7|unverifiable-{index}",
                required=required,
                evidence=evidence,
                held=index == 0,
                held_reason=HELD_DISCREPANCY_REASON if index == 0 else None,
            )
            for index, evidence in enumerate(unverifiable_evidence)
        ]

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.items[current.id] == ItemOverlayView(
        held_now=False, historical=False, data_status_now="live"
    )
    assert result.items[current.id].actions_enabled
    for item in unverifiable:
        view = result.items[item.id]
        assert view == ItemOverlayView(
            held_now=True,
            held_reason_now=HISTORICAL_REASON,
            historical=True,
            data_status_now="live",
        ), item.evidence
        assert not view.actions_enabled
    assert result.overlay.stale_datasets == ()
    assert result.overlay.incomplete_datasets == ()
    assert result.banners == ()

    async with sessions() as session:
        assert await current_dataset_ids(session) == frozenset(
            {google.id, *(dataset.id for dataset in others)}
        )
        assert await item_is_current(session, current)
        for item in unverifiable:
            assert not await item_is_current(session, item), item.evidence
    # The overlay never rewrites the frozen evidence it could not verify.
    for item, evidence in zip(unverifiable, unverifiable_evidence, strict=True):
        assert (await _stored(sessions, item.id)).evidence == evidence


@pytest.mark.anyio
@pytest.mark.db
async def test_an_unverifiable_linkedin_item_still_reports_against_the_effective_crm_dataset(
    sessions: Sessions, shipped: Thresholds
) -> None:
    # The verifier's reproduction: LinkedIn is simulated, so LinkedIn-attributed hubspot.*
    # datasets are the companion's (AAP 0.5.4.1, 0.3.3). The companion has no
    # hubspot.contacts row while HubSpot's primary is fresh.
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin", mode="simulated")
        companion = await _companion(session, linkedin)
        hubspot = await _dataset(session, "hubspot")
        await _freshness(session, google, "google.metrics_daily")
        await _freshness(session, linkedin, "linkedin.leads")
        await _freshness(session, hubspot, "hubspot.contacts")
        run = await _run(session)
        required = ("linkedin.leads", "hubspot.contacts")
        # A complete LinkedIn entry plus a companion entry missing its role.
        no_role = _prov(linkedin, companion)
        del no_role[1]["role"]
        # A malformed LinkedIn entry: still LinkedIn-attributed, as it requires linkedin.leads.
        no_mode = _prov(linkedin, companion)
        del no_mode[0]["mode"]
        # Google-to-CRM evidence with a malformed Google entry keeps reading HubSpot's primary.
        google_crm_provenance = _prov(google, hubspot)
        del google_crm_provenance[0]["source"]
        pipeline = await _item(
            session,
            run,
            rule_id="C1b",
            section="campaigns",
            group="pause",
            entity_type="campaign",
            entity_id="31",
            provenance=no_role,
            required=required,
            data_status="simulated",
        )
        veto = await _item(
            session,
            run,
            rule_id="W1",
            section="white_papers",
            group="cohort:USD",
            entity_type="content_asset",
            entity_id="wp-1",
            provenance=no_mode,
            required=required,
            data_status="simulated",
        )
        google_crm = await _item(
            session,
            run,
            rule_id="C2",
            section="campaigns",
            group="scale",
            entity_type="campaign",
            entity_id="41",
            provenance=google_crm_provenance,
            required=("google.metrics_daily", "hubspot.contacts"),
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    companion_flags = (
        Flag(FLAG_STALE, "Data as of: no successful sync of hubspot.contacts"),
        Flag(FLAG_INCOMPLETE, "Incomplete: hubspot.contacts (no freshness record)"),
    )
    # Currentness still fails closed: every unverifiable item is historical.
    assert result.items[pipeline.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="simulated",
        flags_now=(Flag(FLAG_SIMULATED, "Simulated data (LinkedIn)"), *companion_flags),
    )
    assert result.items[veto.id] == ItemOverlayView(
        held_now=True,
        held_reason_now=HISTORICAL_REASON,
        historical=True,
        data_status_now="simulated",
        flags_now=(Flag(FLAG_SIMULATED, "Simulated data (HubSpot)"), *companion_flags),
    )
    assert result.items[google_crm.id] == ItemOverlayView(
        held_now=True, held_reason_now=HISTORICAL_REASON, historical=True, data_status_now="live"
    )
    # The page lists and banners carry the companion-scope verdict the cards show.
    assert result.overlay.stale_datasets == ("hubspot.contacts",)
    assert result.overlay.incomplete_datasets == ("hubspot.contacts",)
    assert result.banners == (
        BannerView(
            code=BANNER_SIMULATED_SOURCES,
            tone="info",
            message="Simulated data: LinkedIn (simulated data mode)",
            count=1,
        ),
        BannerView(
            code=BANNER_STALE_DATASETS,
            tone="warning",
            message="Stale data: hubspot.contacts (no successful sync)",
            count=1,
        ),
        BannerView(
            code=BANNER_INCOMPLETE_DATASETS,
            tone="warning",
            message="Incomplete data: hubspot.contacts (no freshness record)",
            count=1,
        ),
    )
    async with sessions() as session:
        for item in (pipeline, veto, google_crm):
            assert not await item_is_current(session, item)
    # The overlay never rewrites the frozen evidence it could not verify.
    assert (await _stored(sessions, pipeline.id)).evidence["provenance"] == no_role


# ---------------------------------------------------------------------------------------
# P2 tracking, P5 short window and the fixed banner order
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_the_tracking_banner_names_the_sources_of_the_runs_stored_findings(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        await _covered(session, "google", "linkedin", "hubspot")
        run = await _run(session)
        other = await _run(session, kind="custom")
        await _finding(
            session,
            run,
            FINDING_CONVERSION_TRACKING_ABSENT,
            entity={"source": "linkedin", "type": "account", "id": 4},
        )
        await _finding(
            session,
            run,
            FINDING_CONVERSION_TRACKING_ABSENT,
            entity={"source": "unknown"},
            evidence={"source": "google", "spend": "120.000000"},
        )
        await _finding(session, run, FINDING_NO_CRM_LINK, evidence={"source": "hubspot"})
        foreign = await _finding(
            session, other, FINDING_CONVERSION_TRACKING_ABSENT, evidence={"source": "hubspot"}
        )

    loaded = await _page(sessions, run.id, thresholds=shipped)
    assert loaded.banners == (
        BannerView(
            code=BANNER_TRACKING_ABSENT,
            tone="warning",
            message=(
                "No conversions or leads recorded for Google, LinkedIn — verify tracking "
                "before acting on zero-outcome rules"
            ),
            count=2,
        ),
    )

    # Findings the caller passes are used instead of a read; another run's are ignored.
    passed_foreign = await _page(sessions, run.id, thresholds=shipped, findings=[foreign])
    assert passed_foreign.banners == ()
    sourceless = [
        RunFinding(
            run_id=run.id,
            section="campaigns",
            code=FINDING_CONVERSION_TRACKING_ABSENT,
            entity=None,
            message=TRACKING_MESSAGE,
            evidence={"source": "posthog"},
        )
        for _ in range(2)
    ]
    passed = await _page(sessions, run.id, thresholds=shipped, findings=sourceless)
    assert passed.banners == (
        BannerView(code=BANNER_TRACKING_ABSENT, tone="warning", message=TRACKING_MESSAGE),
    )


@pytest.mark.anyio
@pytest.mark.db
@pytest.mark.parametrize(
    ("days", "stored_min", "read_min", "flagged"),
    [
        (20, 21, 21, True),
        (21, 21, 21, False),
        (13, 14, 14, True),
        (14, 14, 14, False),
        # The run's stored minimum decides, never the configuration it is read under.
        (20, 21, 14, True),
        (14, 14, 21, False),
        (20, 14, 21, False),
    ],
)
async def test_the_short_window_banner_uses_the_trusted_minimum_the_run_stored(
    sessions: Sessions,
    shipped: Thresholds,
    days: int,
    stored_min: int,
    read_min: int,
    flagged: bool,
) -> None:
    """P5: the banner judges the window against the run's stored ``trusted_min_days``.

    The run stores the ``window.trusted_min_days`` it was computed with (AAP 0.5.3 "Where
    outputs are frozen"), so the banner and its message quote that value whatever
    ``window.trusted_min_days`` the overlay is read under.
    """
    config = (
        shipped
        if read_min == shipped.trusted_min_days
        else _thresholds("window", trusted_min_days=read_min)
    )
    assert config.trusted_min_days == read_min
    async with sessions() as session, session.begin():
        await _covered(session, "google", "linkedin", "hubspot")
        run = await _run(
            session, start=WINDOW_END - timedelta(days=days - 1), trusted_min_days=stored_min
        )

    result = await _page(sessions, run.id, thresholds=config)
    expected = (
        (
            BannerView(
                code=BANNER_SHORT_WINDOW,
                tone="warning",
                message=f"Window of {days} days is below the {stored_min}-day trusted minimum",
            ),
        )
        if flagged
        else ()
    )
    assert result.banners == expected


@pytest.mark.anyio
@pytest.mark.db
async def test_a_one_day_short_window_banner_reads_1_day(
    sessions: Sessions, shipped: Thresholds
) -> None:
    """P5: a one-day run's banner reads "Window of 1 day", never "1 days" (AAP 0.5.5).

    The "{trusted_min_days}-day" adjective stays singular whatever the minimum.
    """
    async with sessions() as session, session.begin():
        await _covered(session, "google", "linkedin", "hubspot")
        run = await _run(session, start=WINDOW_END, trusted_min_days=shipped.trusted_min_days)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert result.banners == (
        BannerView(
            code=BANNER_SHORT_WINDOW,
            tone="warning",
            message=f"Window of 1 day is below the {shipped.trusted_min_days}-day trusted minimum",
        ),
    )


@pytest.mark.anyio
@pytest.mark.db
async def test_every_banner_renders_in_the_fixed_order(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        await _dataset(session, "linkedin", mode="simulated", reason="credentials not configured")
        await _covered(session, "hubspot")
        await _freshness(session, google, "google.metrics_daily", success=OLD)
        await _freshness(
            session,
            google,
            "google.search_terms",
            completeness="incomplete",
            reason="call budget exhausted",
        )
        run = await _run(session, start=WINDOW_END - timedelta(days=9))
        await _finding(
            session, run, FINDING_CONVERSION_TRACKING_ABSENT, evidence={"source": "google"}
        )
        await _discrepancy(session, google, "campaign", 11)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert tuple(banner.code for banner in result.banners) == BANNER_ORDER
    assert [banner.alert for banner in result.banners] == [True] + [False] * 5
    assert [banner.message for banner in result.banners] == [
        "1 discrepancy to resolve before acting on other recommendations",
        "No conversions or leads recorded for Google — verify tracking before acting on "
        "zero-outcome rules",
        "Simulated data: LinkedIn (credentials not configured)",
        "Stale data: google.metrics_daily (last success Oct 6, 2026, 11:00 AM UTC)",
        "Incomplete data: google.search_terms (call budget exhausted)",
        f"Window of 10 days is below the {shipped.trusted_min_days}-day trusted minimum",
    ]


# ---------------------------------------------------------------------------------------
# Feedback, read live
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_feedback_hides_items_live_until_it_expires_and_never_hides_c0(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        provenance = _prov(google)
        earlier = await _run(session)
        run = await _run(session)
        earlier_item = await _item(session, earlier, provenance=provenance, entity_id="7|a")
        dismissed = await _item(session, run, provenance=provenance, entity_id="7|a")
        snoozed = await _item(session, run, rule_id=K2, provenance=provenance, entity_id="7|b")
        other_type = await _item(
            session, run, rule_id=K2, provenance=provenance, entity_type="keyword", entity_id="7|b"
        )
        done = await _item(
            session, run, rule_id=K3, provenance=provenance, entity_type="keyword", entity_id="9"
        )
        reversed_order = await _item(session, run, provenance=provenance, entity_id="7|e")
        expired = await _item(session, run, provenance=provenance, entity_id="7|c")
        ends_now = await _item(session, run, provenance=provenance, entity_id="7|d")
        c0 = await _item(
            session,
            run,
            rule_id=C0,
            section="campaigns",
            group="discrepancies",
            entity_type="campaign",
            entity_id="11",
            provenance=provenance,
            decision_subject={"type": "campaign", "id": 11},
        )
        day = timedelta(days=1)
        # Given on the earlier run's item, matched to this run by rule and entity; the later
        # dismiss wins over the earlier snooze.
        await _feedback(
            session, earlier_item, "snooze", until=NOW + 7 * day, created_at=NOW - 2 * day
        )
        await _feedback(session, dismissed, "dismiss", until=NOW + 30 * day, created_at=NOW - day)
        await _feedback(session, snoozed, "snooze", until=NOW + 7 * day, created_at=NOW - day)
        await _feedback(session, done, "done", until=NOW + 30 * day, created_at=NOW - 3 * day)
        # A later feedback that has already expired never overrides one still in force.
        await _feedback(
            session, done, "dismiss", until=NOW - timedelta(hours=1), created_at=NOW - day
        )
        await _feedback(
            session, reversed_order, "dismiss", until=NOW + 30 * day, created_at=NOW - 3 * day
        )
        await _feedback(
            session, reversed_order, "snooze", until=NOW + 7 * day, created_at=NOW - day
        )
        await _feedback(
            session, expired, "dismiss", until=NOW - timedelta(seconds=1), created_at=NOW - day
        )
        await _feedback(session, ends_now, "snooze", until=NOW, created_at=NOW - 7 * day)
        await _feedback(session, c0, "dismiss", until=NOW + 30 * day, created_at=NOW - day)

    result = await _page(sessions, run.id, thresholds=shipped)
    hidden = {item_id: view.hidden_by for item_id, view in result.items.items()}
    assert hidden == {
        dismissed.id: "dismiss",
        snoozed.id: "snooze",
        other_type.id: None,
        done.id: "done",
        reversed_order.id: "snooze",
        expired.id: None,
        ends_now.id: None,
        c0.id: None,
    }
    assert result.items[dismissed.id].to_json() == {
        "held_now": False,
        "historical": False,
        "data_status_now": "live",
        "flags_now": [],
        "hidden_by": "dismiss",
    }
    # Hidden state is read time only: the items themselves are not held.
    assert not any(view.held_now for view in result.items.values())

    only_c0 = await _page_of(sessions, run.id, [c0.id], shipped)
    assert only_c0.items[c0.id].hidden_by is None


async def _page_of(
    sessions: Sessions, run_id: int, item_ids: Sequence[int], thresholds: Thresholds
) -> OverlayResult:
    """Overlay only the given items of a run."""
    async with sessions() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        items = list(
            await session.scalars(select(Recommendation).where(Recommendation.id.in_(item_ids)))
        )
        return await overlay(
            session, run, items, NOW, thresholds=thresholds, display_timezone="UTC"
        )


@pytest.mark.anyio
@pytest.mark.db
async def test_feedback_applies_only_to_the_exact_rule_and_entity_of_each_item(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        provenance = _prov(google)
        earlier = await _run(session)
        run = await _run(session)
        dismissed = await _item(session, run, provenance=provenance, entity_id="7|a")
        same_rule = await _item(session, run, provenance=provenance, entity_id="7|b")
        other_rule = await _item(
            session, run, rule_id=K2, provenance=provenance, entity_type="keyword", entity_id="9"
        )
        # Active feedback on keys no item of the page carries: the same rule and type on
        # another entity, the same entity id under another type, another rule on the entity.
        elsewhere = await _item(session, earlier, provenance=provenance, entity_id="7|z")
        retyped = await _item(session, earlier, rule_id=K2, provenance=provenance, entity_id="9")
        other_rule_same_entity = await _item(
            session, earlier, rule_id=K3, provenance=provenance, entity_id="7|b"
        )
        day = timedelta(days=1)
        await _feedback(session, dismissed, "dismiss", until=NOW + 30 * day, created_at=NOW - day)
        for given in (elsewhere, retyped, other_rule_same_entity):
            await _feedback(session, given, "snooze", until=NOW + 7 * day, created_at=NOW - day)
        await _feedback(
            session, same_rule, "dismiss", until=NOW - timedelta(minutes=1), created_at=NOW - day
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    assert {item_id: view.hidden_by for item_id, view in result.items.items()} == {
        dismissed.id: "dismiss",
        same_rule.id: None,
        other_rule.id: None,
    }

    # The lookup reads only the page's exact keys: feedback on any other key never loads.
    page_keys = [
        (item.rule_id, item.entity_type, item.entity_id)
        for item in (dismissed, same_rule, other_rule)
    ]
    async with sessions() as session:
        assert await _load_feedback(session, page_keys * 3, NOW) == {
            (K1, "search_term", "7|a"): "dismiss"
        }
        assert await _load_feedback(session, [], NOW) == {}


@pytest.mark.anyio
@pytest.mark.db
async def test_feedback_with_a_null_entity_matches_only_items_with_the_same_null_entity(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        provenance = _prov(google)
        earlier = await _run(session)
        run = await _run(session)
        entityless = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id=None
        )
        untyped_named = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id="x"
        )
        typed_without_id = await _item(
            session, run, rule_id=K3, provenance=provenance, entity_type="keyword", entity_id=None
        )
        typed_named = await _item(
            session, run, rule_id=K3, provenance=provenance, entity_type="keyword", entity_id="5"
        )
        unmatched_entityless = await _item(
            session, run, rule_id=C1, provenance=provenance, entity_type=None, entity_id=None
        )
        # Rows that differ from a page key only in a NULL or named component.
        named_elsewhere = await _item(
            session,
            earlier,
            rule_id=C1,
            provenance=provenance,
            entity_type="campaign",
            entity_id="11",
        )
        day = timedelta(days=1)
        await _feedback(session, entityless, "snooze", until=NOW + 7 * day, created_at=NOW - day)
        await _feedback(
            session, typed_without_id, "done", until=NOW + 30 * day, created_at=NOW - day
        )
        await _feedback(
            session, named_elsewhere, "dismiss", until=NOW + 30 * day, created_at=NOW - day
        )

    result = await _page(sessions, run.id, thresholds=shipped)
    assert {item_id: view.hidden_by for item_id, view in result.items.items()} == {
        entityless.id: "snooze",
        untyped_named.id: None,
        typed_without_id.id: "done",
        typed_named.id: None,
        unmatched_entityless.id: None,
    }


@pytest.mark.anyio
@pytest.mark.db
async def test_the_latest_feedback_in_force_wins_per_exact_key_and_expired_rows_are_ignored(
    sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        provenance = _prov(google)
        run = await _run(session)
        tie = await _item(session, run, provenance=provenance, entity_id="7|tie")
        entityless = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id=None
        )
        later_expired = await _item(session, run, provenance=provenance, entity_id="7|late")
        only_expired = await _item(session, run, provenance=provenance, entity_id="7|gone")
        ends_now = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id="7|now"
        )
        day = timedelta(days=1)
        written = NOW - day
        # Written at the same instant: the row inserted last (the higher id) wins.
        await _feedback(session, tie, "snooze", until=NOW + 7 * day, created_at=written)
        await _feedback(session, tie, "dismiss", until=NOW + 30 * day, created_at=written)
        # A NULL entity key follows the same rule: the latest row in force wins.
        await _feedback(session, entityless, "done", until=NOW + 30 * day, created_at=NOW - 3 * day)
        await _feedback(session, entityless, "snooze", until=NOW + 7 * day, created_at=written)
        await _feedback(
            session, entityless, "dismiss", until=NOW - timedelta(seconds=1), created_at=NOW
        )
        # A later row that has already expired never overrides one still in force.
        await _feedback(
            session, later_expired, "done", until=NOW + 30 * day, created_at=NOW - 2 * day
        )
        await _feedback(
            session, later_expired, "snooze", until=NOW - timedelta(hours=1), created_at=written
        )
        await _feedback(
            session, only_expired, "dismiss", until=NOW - 2 * day, created_at=NOW - 32 * day
        )
        await _feedback(session, ends_now, "snooze", until=NOW, created_at=NOW - 7 * day)

    result = await _page(sessions, run.id, thresholds=shipped)
    assert {item_id: view.hidden_by for item_id, view in result.items.items()} == {
        tie.id: "dismiss",
        entityless.id: "snooze",
        later_expired.id: "done",
        only_expired.id: None,
        ends_now.id: None,
    }


# ---------------------------------------------------------------------------------------
# Read-only, bounded statements
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.db
async def test_overlay_is_read_only_and_issues_at_most_eight_statements_per_page(
    engine: AsyncEngine, sessions: Sessions, shipped: Thresholds
) -> None:
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        linkedin = await _dataset(session, "linkedin", mode="simulated")
        companion = await _companion(session, linkedin)
        hubspot = await _dataset(session, "hubspot")
        for dataset, name in (
            (google, "google.metrics_daily"),
            (linkedin, "linkedin.leads"),
            (companion, "hubspot.contacts"),
            (hubspot, "hubspot.contacts"),
        ):
            await _freshness(session, dataset, name)
        run = await _run(session)
        await _finding(
            session, run, FINDING_CONVERSION_TRACKING_ABSENT, evidence={"source": "google"}
        )
        await _discrepancy(session, google, "campaign", 3)
        created: list[Recommendation] = []
        for index in range(40):
            linkedin_item = index % 2 == 0
            created.append(
                await _item(
                    session,
                    run,
                    rule_id=W1 if linkedin_item else K1,
                    entity_id=f"entity-{index}",
                    provenance=_prov(linkedin, companion) if linkedin_item else _prov(google),
                    required=(
                        ("linkedin.leads", "hubspot.contacts")
                        if linkedin_item
                        else ("google.metrics_daily",)
                    ),
                    depends_on=[{"type": "campaign", "id": index}],
                )
            )
        await _feedback(
            session, created[1], "dismiss", until=NOW + timedelta(days=30), created_at=FRESH
        )

    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(str(args[2]))

    async with sessions() as session:
        stored = await session.get(AnalysisRun, run.id)
        assert stored is not None
        items = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.run_id == run.id)
                .order_by(Recommendation.id)
            )
        )
        before = [
            (item.evidence, item.flags, item.held, item.held_reason, item.data_status)
            for item in items
        ]
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            result = await overlay(
                session, stored, items, NOW, thresholds=shipped, display_timezone="UTC"
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        assert not session.new
        assert not session.dirty
        assert not session.deleted

    assert len(result.items) == 40
    assert len(statements) <= 8
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert result.items[created[3].id].held_reason_now == HELD_DISCREPANCY_REASON
    assert result.items[created[1].id].hidden_by == "dismiss"
    assert [banner.code for banner in result.banners] == [
        BANNER_OPEN_DISCREPANCIES,
        BANNER_TRACKING_ABSENT,
        BANNER_SIMULATED_SOURCES,
    ]

    async with sessions() as session:
        reloaded = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.run_id == run.id)
                .order_by(Recommendation.id)
            )
        )
    assert [
        (item.evidence, item.flags, item.held, item.held_reason, item.data_status)
        for item in reloaded
    ] == before


# ---------------------------------------------------------------------------------------
# Bounded pages: check representatives and the hidden state, set-based
# ---------------------------------------------------------------------------------------


async def _bounded_page(
    sessions: Sessions,
    run_id: int,
    served: Sequence[int],
    *,
    thresholds: Thresholds,
    representatives: bool = True,
) -> tuple[OverlayResult, tuple[Recommendation, ...]]:
    """Overlay only the ``served`` items of a run, as ``GET /recommendations`` does.

    With ``representatives`` the run's :func:`load_check_representatives` are passed as
    ``check_items``; without, the aggregates cover the served items alone.
    """
    async with sessions() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        items = list(
            await session.scalars(
                select(Recommendation)
                .where(Recommendation.id.in_(served))
                .order_by(Recommendation.id)
            )
        )
        checked = await load_check_representatives(session, run_id) if representatives else ()
        result = await overlay(
            session,
            run,
            items,
            NOW,
            check_items=checked,
            thresholds=thresholds,
            display_timezone="UTC",
        )
        return result, checked


@pytest.mark.anyio
@pytest.mark.db
async def test_check_representatives_keep_a_bounded_pages_aggregates_equal_to_the_whole_run(
    sessions: Sessions, shipped: Thresholds
) -> None:
    """AAP 0.1.1, 0.3.2: a page serving a few items still lists every item's dataset verdicts.

    ``GET /recommendations`` serves a bounded number of items per group and passes the run's
    check representatives as ``check_items``: whichever items are served, none included, the
    page-level stale and incomplete lists and the banners equal those computed over every
    stored item, and each served item's own view is unchanged. One representative stands for
    the items sharing every check input, the lowest id of them.
    """
    window_start, window_end = date(2026, 9, 4), date(2026, 10, 1)
    baseline_days = shipped.int("k1.baseline_days")
    baseline_start = window_end - timedelta(days=baseline_days - 1)
    frozen_window = {
        "start": baseline_start.isoformat(),
        "end": window_end.isoformat(),
        "days": baseline_days,
    }
    required = ("google.search_terms", "google.metrics_daily")
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        for name in required:
            await _freshness(session, google, name, covered_from=window_start, through=window_end)
        linkedin = await _dataset(session, "linkedin", mode="simulated")
        companion = await _companion(session, linkedin)
        for dataset, name in ((linkedin, "linkedin.leads"), (companion, "hubspot.contacts")):
            await _freshness(session, dataset, name, covered_from=window_start, through=window_end)
        await _covered(session, "hubspot")
        run = await _run(session, start=window_start, end=window_end)
        empty_run = await _run(session, start=window_start, end=window_end)
        # Items sharing one set of check inputs, complete over the run's window.
        expand = [
            await _item(
                session,
                run,
                rule_id=K2,
                entity_id=f"7|expand-{index}",
                provenance=_prov(google),
                required=required,
            )
            for index in range(12)
        ]
        # K1 cards whose frozen baselines reach before the coverage, each with its own reason.
        frozen = await _item(
            session,
            run,
            entity_id="7|frozen",
            required=required,
            evidence=_k1_evidence(
                run,
                _prov(google),
                thresholds={"k1.baseline_days": baseline_days},
                baseline_window=frozen_window,
            ),
        )
        frozen_days = await _item(
            session,
            run,
            entity_id="7|frozen-days",
            required=required,
            evidence=_k1_evidence(
                run,
                _prov(google),
                thresholds={"k1.baseline_days": 60},
                baseline_window={**frozen_window, "start": "2026-13-01"},
            ),
        )
        # google.entities has no freshness row: only this item's check names it.
        unrecorded = await _item(
            session,
            run,
            rule_id=K3,
            entity_type="keyword",
            entity_id="9",
            provenance=_prov(google),
            required=("google.entities",),
        )
        leads = await _item(
            session,
            run,
            rule_id=W1,
            entity_type=None,
            entity_id=None,
            provenance=_prov(linkedin, companion),
            required=("linkedin.leads", "hubspot.contacts"),
        )
        # A stored evidence value that is not a JSON object reads as one without those keys.
        malformed = await _item(
            session,
            run,
            rule_id=K3,
            entity_type="keyword",
            entity_id="10",
            required=("google.search_terms",),
            evidence=["provenance"],
        )

    whole = await _page(sessions, run.id, thresholds=shipped)
    assert {"google.entities", "google.metrics_daily"} <= set(whole.overlay.incomplete_datasets)
    assert "google.entities" in whole.overlay.stale_datasets
    incomplete = next(b for b in whole.banners if b.code == BANNER_INCOMPLETE_DATASETS)
    assert "after the K1 baseline start 2026-07-04" in incomplete.message
    assert "after the K1 baseline start 2026-08-03" in incomplete.message

    stored = [*expand, frozen, frozen_days, unrecorded, leads, malformed]
    every_id = [item.id for item in stored]
    for served in ([], [expand[0].id], [expand[5].id], [leads.id, malformed.id], every_id):
        page, checked = await _bounded_page(sessions, run.id, served, thresholds=shipped)
        assert page.overlay == whole.overlay, served
        assert page.banners == whole.banners, served
        assert set(page.items) == set(served)
        assert all(page.items[item_id] == whole.items[item_id] for item_id in served)

    # The twelve K2 items share every input: the lowest id stands for them all.
    assert [item.id for item in checked] == sorted(
        [expand[0].id, frozen.id, frozen_days.id, unrecorded.id, leads.id, malformed.id]
    )
    # Without the representatives, a page of one complete card loses the others' verdicts.
    alone, none = await _bounded_page(
        sessions, run.id, [expand[0].id], thresholds=shipped, representatives=False
    )
    assert none == ()
    assert "google.entities" not in alone.overlay.incomplete_datasets
    assert alone.banners != whole.banners
    async with sessions() as session:
        assert await load_check_representatives(session, empty_run.id) == ()


@pytest.mark.anyio
@pytest.mark.db
async def test_item_hidden_state_marks_exactly_the_items_the_overlay_hides(
    sessions: Sessions, shipped: Thresholds
) -> None:
    """``Group.hidden``: the items whose latest feedback in force hides them, C0 never.

    The set-based state reads the overlay's rule: feedback on the item's exact rule and
    entity (``NULL`` matching ``NULL`` only) whose ``until`` is after now, given on any run's
    item. Several rows in force on one key mark the item once, and every item of the run has
    one row with a non-``NULL`` ``hidden``.
    """
    async with sessions() as session, session.begin():
        google = await _dataset(session, "google")
        provenance = _prov(google)
        earlier = await _run(session)
        run = await _run(session)
        other_run = await _run(session)
        given_earlier = await _item(session, earlier, provenance=provenance, entity_id="7|a")
        matched = await _item(session, run, provenance=provenance, entity_id="7|a")
        twice = await _item(session, run, rule_id=K2, provenance=provenance, entity_id="7|b")
        other_type = await _item(
            session, run, rule_id=K2, provenance=provenance, entity_type="keyword", entity_id="7|b"
        )
        expired = await _item(session, run, provenance=provenance, entity_id="7|c")
        ends_now = await _item(session, run, provenance=provenance, entity_id="7|d")
        entityless = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id=None
        )
        untyped_named = await _item(
            session, run, rule_id=W1, provenance=provenance, entity_type=None, entity_id="x"
        )
        typed_without_id = await _item(
            session, run, rule_id=K3, provenance=provenance, entity_type="keyword", entity_id=None
        )
        never = await _item(session, run, provenance=provenance, entity_id="7|never")
        c0 = await _item(
            session,
            run,
            rule_id=C0,
            section="campaigns",
            group="discrepancies",
            entity_type="campaign",
            entity_id="11",
            provenance=provenance,
            decision_subject={"type": "campaign", "id": 11},
        )
        await _item(session, other_run, provenance=provenance, entity_id="7|a")
        day = timedelta(days=1)
        await _feedback(
            session, given_earlier, "snooze", until=NOW + 7 * day, created_at=NOW - 2 * day
        )
        await _feedback(session, twice, "snooze", until=NOW + 7 * day, created_at=NOW - 2 * day)
        await _feedback(session, twice, "dismiss", until=NOW + 30 * day, created_at=NOW - day)
        await _feedback(
            session, expired, "dismiss", until=NOW - timedelta(seconds=1), created_at=NOW - day
        )
        await _feedback(session, ends_now, "snooze", until=NOW, created_at=NOW - 7 * day)
        await _feedback(session, entityless, "snooze", until=NOW + 7 * day, created_at=NOW - day)
        await _feedback(
            session, typed_without_id, "done", until=NOW + 30 * day, created_at=NOW - day
        )
        await _feedback(session, c0, "dismiss", until=NOW + 30 * day, created_at=NOW - day)

    page = await _page(sessions, run.id, thresholds=shipped)
    hidden_by_overlay = {item_id for item_id, view in page.items.items() if view.hidden_by}
    assert hidden_by_overlay == {matched.id, twice.id, entityless.id, typed_without_id.id}

    state = item_hidden_state(run.id, NOW)
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    state.c.id, state.c.section, state.c["group"], state.c.rank, state.c.hidden
                ).order_by(state.c.id)
            )
        ).all()
    assert [row.id for row in rows] == sorted(page.items)
    assert all(row.hidden in (True, False) for row in rows)
    assert {row.id for row in rows if row.hidden} == hidden_by_overlay
    by_id = {row.id: row for row in rows}
    assert (by_id[c0.id].section, by_id[c0.id].group, by_id[c0.id].rank) == (
        "campaigns",
        "discrepancies",
        1,
    )
    assert not any(
        by_id[item.id].hidden for item in (other_type, expired, ends_now, untyped_named, never, c0)
    )
