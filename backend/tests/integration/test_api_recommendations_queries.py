"""Integration tests: ``GET /api/v1/recommendations`` reads a fixed set of statements.

AAP 0.1.1 (the five sections render from precomputed runs in under 2 s), 0.3.2 (only the
current-state overlay is computed at read time, from live tables) and 0.5.3
(``GET /recommendations?run_id=``; stored outputs are returned without re-running rules). Every
request goes through ``create_app()`` and ``httpx.ASGITransport`` against the PostgreSQL 18 test
database, signed in as the test admin, and every SQL statement the application's engine sends
while the request is served is recorded. The guarantees proven here:

* **Bounded**: one page read, of the latest default run (no ``run_id``) or of a run addressed
  by ``run_id``, sends at most 14 statements, authentication included, and every one is a
  ``SELECT``: the session's user, the run, the four stored outputs (items, findings, withheld
  patterns, suppression counts), the current datasets and the overlay's live reads.
* **Independent of the run's size**: a default run holding several times as many items, and
  more findings and withheld patterns, is read with exactly the same statements as a small one,
  on either path, so no statement is sent per item, finding or withheld pattern.

**Seeding.** ``sync.mode.bootstrap_sources(..., force_simulated=True)`` builds, backfills and
activates a simulated dataset per source, as the worker's first start does. A small default run
is then computed over a one-day default window with the creative impression floor out of reach,
which withholds the window-gated and creative rules, and the full default run of the shipped
configuration over the same stored data follows it. Both are persisted by
``runner.run_analysis``, so every row read is one the product wrote.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
from sqlalchemy import event

from ads_engine.analysis import runner
from ads_engine.analysis.thresholds import Thresholds, resolve_config_dir
from ads_engine.api.app import API_PREFIX
from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.db.models.ops import SOURCES
from ads_engine.db.session import get_engine
from ads_engine.settings import get_settings
from ads_engine.sync import mode

pytestmark = pytest.mark.anyio

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

MOST_PAGE_STATEMENTS: Final = 14
"""The most statements one signed-in page read may send, the session's user lookup included."""

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
    """

    run_id: int
    statements: tuple[str, ...]
    items: int
    findings: int
    withheld: int

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
    with recorded_statements() as seen:
        response = await client.get(f"{API_PREFIX}/recommendations", params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    sections: Sequence[Mapping[str, Any]] = page["sections"]
    return PageRead(
        run_id=int(page["run"]["id"]),
        statements=tuple(seen),
        items=sum(len(group["items"]) for section in sections for group in section["groups"]),
        findings=sum(len(section["findings"]) for section in sections),
        withheld=sum(len(section["withheld"]) for section in sections),
    )


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
    """AAP 0.1.1, 0.5.3: a page read sends at most 14 ``SELECT`` statements, whatever the run.

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
