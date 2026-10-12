"""Integration tests: ``GET /api/v1/recommendations`` reads a fixed set of statements.

AAP 0.1.1 (the five sections render from precomputed runs in under 2 s), 0.3.2 (only the
current-state overlay is computed at read time, from live tables) and 0.5.3
(``GET /recommendations?run_id=``; stored outputs are returned without re-running rules). Every
request goes through ``create_app()`` and ``httpx.ASGITransport`` against the PostgreSQL 18 test
database, signed in as the test admin, and every SQL statement the application's engine sends
while the request is served is recorded. The guarantees proven here:

* **Bounded**: one page read, of the latest default run (no ``run_id``) or of a run addressed
  by ``run_id``, sends at most 15 statements, authentication included, and every one is a
  ``SELECT``: the session's user, the run, the four stored outputs (each group's first items
  with its counts, findings, withheld patterns, suppression counts), the overlay's check
  representatives, the current datasets and the overlay's live reads.
* **Independent of the run's size**: a default run holding several times as many items, and
  more findings and withheld patterns, is read with exactly the same statements as a small one,
  on either path, so no statement is sent per group, item, finding or withheld pattern.
* **At scale** (AAP 0.1.1): a run of 20,000 more items, cloned set-based from a stored one, is
  read with the same statements as the run it was cloned from, as a page of at most
  ``GROUP_PAGE_SIZE`` items per group and a few megabytes, in under 2 s; its groups count every
  stored item, and ``GET /recommendations/items`` reads any slice of its 20,000-item group with
  the same bounded statements at any offset.

**Seeding.** ``sync.mode.bootstrap_sources(..., force_simulated=True)`` builds, backfills and
activates a simulated dataset per source, as the worker's first start does. A small default run
is then computed over a one-day default window with the creative impression floor out of reach,
which withholds the window-gated and creative rules, and the full default run of the shipped
configuration over the same stored data follows it. Both are persisted by
``runner.run_analysis``, so every row read is one the product wrote.
"""

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import BigInteger, Text, cast, event, func, insert, literal, select, true

from ads_engine.analysis import runner
from ads_engine.analysis.thresholds import Thresholds, resolve_config_dir
from ads_engine.api import schemas
from ads_engine.api.app import API_PREFIX
from ads_engine.db.models.analysis import AnalysisRun, Recommendation
from ads_engine.db.models.ops import SOURCES
from ads_engine.db.session import get_engine, get_sessionmaker
from ads_engine.settings import get_settings
from ads_engine.sync import mode

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

MOST_PAGE_STATEMENTS: Final = 15
"""The most statements one signed-in page read may send, the session's user lookup included."""

MOST_ITEMS_STATEMENTS: Final = 11
"""The most statements one signed-in ``GET /recommendations/items`` may send: the user, the run,
the group's counts, the slice and the overlay's live reads."""

SCALE_ITEMS: Final = 20_000
"""How many items the scale run adds to one group: the size of the run the API ran out of
memory on, whose 100.7 MB page took 7.3 s."""

PAGE_BYTES_BOUND: Final = 3_000_000
"""The most bytes the scale run's page may weigh: a few megabytes, whatever the run's size."""

RENDER_SECONDS: Final = 2.0
"""AAP 0.1.1: the five sections render from precomputed runs in under 2 s."""

TIMED_READS: Final = 3
"""How many times the scale page is read; the fastest read is held to :data:`RENDER_SECONDS`,
so a moment of contention on a shared host does not decide the outcome."""

SEED_BACKFILL_DAYS: Final = "28"
"""``BACKFILL_DAYS`` of the seed: the default window's length, far below the 180-day default."""

SMALL_RUN_OVERRIDES: Final[Mapping[str, object]] = {
    "window.default_days": 1,
    "window.presets": [1, 14, 28, 56, 90],
    "cr.min_impressions": 10**9,
}
"""The small run's configuration: a one-day default window withholds every rule that needs a
longer window, and an impression floor no creative reaches withholds the creative rules."""

GROWTH_FACTOR: Final = 3
"""How many times as many items the full run must hold as the small one."""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageRead:
    """One signed-in ``GET /recommendations`` and the SQL it sent.

    Attributes:
        run_id: The id of the run the page shows.
        statements: Every statement the application's engine sent, whitespace collapsed.
        items: The page's items, over every section and group.
        findings: The page's findings, over every section.
        withheld: The page's withheld patterns, over every section.
        stored: The items the run stored, the sum of every group's ``total``.
        groups: How many groups the page holds, over every section.
        largest_group: The most items one group of the page carries.
        size: The response body's length in bytes.
        seconds: How long the request took, from send to the parsed body.
    """

    run_id: int
    statements: tuple[str, ...]
    items: int
    findings: int
    withheld: int
    stored: int
    groups: int
    largest_group: int
    size: int
    seconds: float

    @property
    def targets(self) -> list[str]:
        """The table each statement reads, in the order sent."""
        return [target(statement) for statement in self.statements]


def target(statement: str) -> str:
    """Return the table a recorded ``SELECT`` reads: the name after its first ``FROM``.

    A statement without ``FROM`` is returned as its first 60 characters, so a failure message
    still names it.
    """
    _, found, rest = statement.partition(" FROM ")
    if not found:
        return statement[:60]
    return rest.split(" ", 1)[0]


@contextmanager
def recorded_statements() -> Iterator[list[str]]:
    """Record every statement the application's engine sends meanwhile, whitespace collapsed."""
    engine = get_engine().sync_engine
    seen: list[str] = []

    def record(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: object,
        _context: Any,
        _executemany: bool,
    ) -> None:
        """Append the statement about to be sent."""
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def read_page(client: httpx.AsyncClient, run_id: int | None) -> PageRead:
    """Read the page of ``run_id``, or of the latest default run when ``None``, recording its SQL.

    Raises:
        AssertionError: When the page is not served with 200.
    """
    params = {} if run_id is None else {"run_id": str(run_id)}
    started = time.perf_counter()
    with recorded_statements() as seen:
        response = await client.get(f"{API_PREFIX}/recommendations", params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    seconds = time.perf_counter() - started
    sections: Sequence[Mapping[str, Any]] = page["sections"]
    groups = [group for section in sections for group in section["groups"]]
    return PageRead(
        run_id=int(page["run"]["id"]),
        statements=tuple(seen),
        items=sum(len(group["items"]) for group in groups),
        findings=sum(len(section["findings"]) for section in sections),
        withheld=sum(len(section["withheld"]) for section in sections),
        stored=sum(int(group["total"]) for group in groups),
        groups=len(groups),
        largest_group=max(len(group["items"]) for group in groups),
        size=len(response.content),
        seconds=seconds,
    )


async def read_items(
    client: httpx.AsyncClient, run_id: int, section: str, group: str, offset: int
) -> tuple[tuple[str, ...], Mapping[str, Any]]:
    """Read one slice of a group, recording its SQL.

    Raises:
        AssertionError: When the slice is not served with 200.
    """
    params = {"run_id": str(run_id), "section": section, "group": group, "offset": str(offset)}
    with recorded_statements() as seen:
        response = await client.get(f"{API_PREFIX}/recommendations/items", params=params)
    assert response.status_code == 200, response.text
    body: Mapping[str, Any] = response.json()
    return tuple(seen), body


async def scaled_copy(run_id: int, clones: int) -> tuple[int, tuple[str, str]]:
    """Copy a run with every stored item, plus ``clones`` copies of one item, set-based.

    The copy is a ``custom`` run of the same window and settings. Its extra items repeat the
    lowest-ranked item of the run's largest Keywords group (else of its largest group) with
    ranks after it and distinct entity keys, in one ``INSERT ... SELECT`` over
    ``generate_series``, so the run the finding measured is built in a few statements.

    Returns:
        The copy's id and the ``(section, group)`` the clones were added to.
    """
    runs = AnalysisRun.__table__
    items = Recommendation.__table__
    run_columns = [column for column in runs.columns if column.name != "id"]
    item_columns = [column for column in items.columns if column.name not in {"id", "run_id"}]
    overrides = {"kind": literal("custom"), "trigger": literal("manual")}
    async with get_sessionmaker()() as session, session.begin():
        copy_id = (
            await session.execute(
                insert(runs)
                .from_select(
                    [column.name for column in run_columns],
                    select(*(overrides.get(column.name, column) for column in run_columns)).where(
                        runs.c.id == run_id
                    ),
                )
                .returning(runs.c.id)
            )
        ).scalar_one()
        copy = literal(copy_id, BigInteger)
        names = ["run_id", *(column.name for column in item_columns)]
        await session.execute(
            insert(items).from_select(
                names, select(copy, *item_columns).where(items.c.run_id == run_id)
            )
        )
        size = func.count().over(partition_by=(items.c.section, items.c["group"]))
        section, group, source_id = (
            await session.execute(
                select(items.c.section, items.c["group"], items.c.id)
                .where(items.c.run_id == run_id)
                .order_by(
                    (items.c.section == "keywords").desc(),
                    size.desc(),
                    items.c.rank,
                    items.c.id,
                )
                .limit(1)
            )
        ).one()
        clone = func.generate_series(1, clones).table_valued("value", name="clone").render_derived()
        cloned = {
            "rank": items.c.rank + clone.c.value,
            "entity_id": func.coalesce(items.c.entity_id, "")
            + ":clone-"
            + cast(clone.c.value, Text),
        }
        await session.execute(
            insert(items).from_select(
                names,
                select(copy, *(cloned.get(column.name, column) for column in item_columns))
                .select_from(items)
                .join(clone, true())
                .where(items.c.id == source_id),
            )
        )
    return int(copy_id), (str(section), str(group))


async def bootstrap_simulated() -> None:
    """Build, backfill and activate a simulated dataset per source, as the first start does.

    ``DATA_MODE=simulated`` and a short backfill apply to the bootstrap alone, and the settings
    cache is cleared on both sides of it.

    Raises:
        AssertionError: When any source failed or resolved to another mode.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATA_MODE", "simulated")
        patch.setenv("BACKFILL_DAYS", SEED_BACKFILL_DAYS)
        get_settings.cache_clear()
        try:
            report = await mode.bootstrap_sources(get_settings(), force_simulated=True)
        finally:
            get_settings.cache_clear()
    get_settings.cache_clear()
    assert not report.failures, dict(report.failures)
    assert {source: resolved.mode for source, resolved in report.modes.items()} == dict.fromkeys(
        SOURCES, "simulated"
    )


async def default_run(thresholds: Thresholds | None) -> AnalysisRun:
    """Compute and persist one default-window run under ``thresholds`` (shipped when ``None``).

    Raises:
        AssertionError: When the run did not succeed.
    """
    now = datetime.now(UTC)
    window = await runner.default_window(now=now, thresholds=thresholds)
    run = await runner.run_analysis(window, "default", "scheduled", thresholds=thresholds, now=now)
    assert run.status == "succeeded", run.status
    return run


# ---------------------------------------------------------------------------------------------
# GET /recommendations (AAP 0.1.1, 0.3.2, 0.5.3)
# ---------------------------------------------------------------------------------------------


async def test_page_statements_are_bounded_and_independent_of_the_run_size(
    authed_client: httpx.AsyncClient,
) -> None:
    """AAP 0.1.1, 0.5.3: a page read sends at most 15 ``SELECT`` statements, whatever the run.

    The small default run is read without ``run_id`` and by its id; the full default run,
    computed next over the same stored data with several times as many items and more
    findings and withheld patterns, is read the same two ways. Each read sends at most
    :data:`MOST_PAGE_STATEMENTS` statements, every one a ``SELECT``, and each path sends the
    same statements, in the same order, for the full run as for the small one.
    """
    await bootstrap_simulated()
    small = await default_run(Thresholds.load(resolve_config_dir(), overrides=SMALL_RUN_OVERRIDES))
    small_latest = await read_page(authed_client, None)
    small_by_id = await read_page(authed_client, small.id)
    full = await default_run(None)
    full_latest = await read_page(authed_client, None)
    full_by_id = await read_page(authed_client, full.id)

    assert (small_latest.run_id, small_by_id.run_id) == (small.id, small.id)
    assert (full_latest.run_id, full_by_id.run_id) == (full.id, full.id)
    # Items on the small page too, so the overlay's feedback read runs on both pages.
    assert small_latest.items >= 1, "the small default run holds no item"
    assert full_latest.items >= GROWTH_FACTOR * small_latest.items, (
        f"the full run holds {full_latest.items} items, the small one {small_latest.items}"
    )
    assert full_latest.findings > small_latest.findings, (full_latest, small_latest)
    assert full_latest.withheld > small_latest.withheld, (full_latest, small_latest)

    for read in (small_latest, small_by_id, full_latest, full_by_id):
        assert len(read.statements) <= MOST_PAGE_STATEMENTS, read.targets
        assert all(statement.startswith("SELECT ") for statement in read.statements), (
            read.statements
        )
    for small_read, full_read in ((small_latest, full_latest), (small_by_id, full_by_id)):
        assert len(full_read.statements) == len(small_read.statements), (
            small_read.targets,
            full_read.targets,
        )
        assert full_read.targets == small_read.targets


async def test_a_twenty_thousand_item_run_reads_as_a_bounded_page_in_under_two_seconds(
    authed_client: httpx.AsyncClient,
) -> None:
    """AAP 0.1.1: a run of 20,000 more items is read like the run it grew from, in under 2 s.

    The shipped default run is computed over the simulated data and read by id. A copy of it
    with :data:`SCALE_ITEMS` more items in one group, the size the finding measured, is read
    the same way: with exactly the run's statements, at most :data:`MOST_PAGE_STATEMENTS`;
    with at most ``GROUP_PAGE_SIZE`` items per group while the groups' totals count every
    stored item; as a body of at most :data:`PAGE_BYTES_BOUND` bytes; and, at its fastest of
    :data:`TIMED_READS` reads, in under :data:`RENDER_SECONDS`. ``GET /recommendations/items``
    then reads the first and the last slice of the 20,000-item group with the same statements,
    at most :data:`MOST_ITEMS_STATEMENTS`, as it reads the original run's group.
    """
    await bootstrap_simulated()
    run = await default_run(None)
    small = await read_page(authed_client, run.id)
    scale_id, (section, group) = await scaled_copy(run.id, SCALE_ITEMS)
    reads = [await read_page(authed_client, scale_id) for _ in range(TIMED_READS)]
    large = min(reads, key=lambda read: read.seconds)

    assert large.run_id == scale_id
    assert large.stored == small.stored + SCALE_ITEMS
    assert large.groups == small.groups
    assert large.largest_group == schemas.GROUP_PAGE_SIZE
    assert large.items <= schemas.GROUP_PAGE_SIZE * large.groups
    assert large.items < small.items + schemas.GROUP_PAGE_SIZE
    assert large.size <= PAGE_BYTES_BOUND, large.size
    assert large.seconds < RENDER_SECONDS, [read.seconds for read in reads]
    for read in reads:
        assert len(read.statements) == len(small.statements) <= MOST_PAGE_STATEMENTS
        assert read.targets == small.targets
        assert all(statement.startswith("SELECT ") for statement in read.statements)

    original, original_body = await read_items(authed_client, run.id, section, group, 0)
    first, first_body = await read_items(authed_client, scale_id, section, group, 0)
    last_offset = int(first_body["total"]) - 1
    last, last_body = await read_items(authed_client, scale_id, section, group, last_offset)
    assert int(first_body["total"]) == int(original_body["total"]) + SCALE_ITEMS
    assert len(first_body["items"]) == schemas.GROUP_PAGE_SIZE
    assert len(last_body["items"]) == 1
    assert last_body["items"][0]["rank"] == max(
        item["rank"] for item in (*first_body["items"], *last_body["items"])
    )
    for statements in (first, last):
        assert len(statements) == len(original) <= MOST_ITEMS_STATEMENTS
        assert [target(statement) for statement in statements] == [
            target(statement) for statement in original
        ]
        assert all(statement.startswith("SELECT ") for statement in statements)
