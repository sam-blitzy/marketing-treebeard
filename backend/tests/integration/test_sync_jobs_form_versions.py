"""Integration tests of the sync jobs' lead-form version store (``jobs._store_form_versions``).

AAP 0.3.3 ("Form identity"): ``lead_form_version`` holds each form version's email question
ids and is filled from two places, every response decoration (keyed by the version parsed
from its ``versionedLeadGenFormUrn``) and the ``leadForms`` sync of each form's current
version. Both the entity and the lead jobs therefore present versions that are mostly
already stored, and AAP 0.5.2 keeps ``first_seen_at`` as the first sighting. A store whose
statement count grows with the versions seen costs one round trip per version per sync.

Covered, on the PostgreSQL 18 test database through the conftest ``db`` fixture:

* **New versions**: inserted with ``first_seen_at = now`` and their email ids in preference
  order, a form not stored yet getting a stand-in ``lead_form`` row; a repeated map keeps
  its last occurrence; nothing is updated;
* **Unchanged versions**: a re-sighting of stored maps reads them in one query and sends no
  INSERT, UPDATE or row lock on ``lead_form_version``; ``first_seen_at`` is kept;
* **Changed versions**: 50 changed maps (reordered, replaced or emptied ids) are rewritten
  in one UPDATE after one ``FOR NO KEY UPDATE`` lock in id order, keeping ``first_seen_at``;
  unchanged ones are left alone and new ones inserted;
* **Bounded statements**: one more changed version than a chunk holds takes exactly two
  UPDATEs, none binding more than two parameters per row plus the dataset;
* **Dataset scope**: the same form and versions in another dataset keep every value;
* **Guard**: a changed version of a retired dataset raises ``DatasetNotWritable`` and
  writes nothing;
* **Return value**: the rows written, new plus changed, never the unchanged ones.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.linkedin.parser import FormVersionMap
from ads_engine.db.models.accounts import LeadForm, LeadFormVersion
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.repositories import DatasetNotWritable
from ads_engine.sync.jobs import _ID_CHUNK, _store_form_versions

type Sessions = async_sessionmaker[AsyncSession]
type Stored = dict[tuple[str, int], tuple[list[str], datetime]]
"""``(canonical_urn, version)`` -> ``(email_question_ids, first_seen_at)``."""

pytestmark = pytest.mark.anyio

NOW: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The instant of the sighting under test."""

FIRST_SEEN: Final = NOW - timedelta(days=30)
"""The instant of the earlier sighting that stored the known versions."""

FORM_A: Final = "urn:li:leadGenForm:1001"
FORM_B: Final = "urn:li:leadGenForm:1002"

_TABLE: Final = "lead_form_version"


def _map(urn: str, version: int, *email_ids: int) -> FormVersionMap:
    """A version map with ``email_ids`` in preference order, as a decoration yields it."""
    return FormVersionMap(canonical_urn=urn, version=version, email_question_ids=email_ids)


async def _linkedin_dataset(sessions: Sessions, state: str = "building") -> int:
    """Commit a simulated LinkedIn primary dataset in ``state`` and return its id."""
    async with sessions() as session, session.begin():
        dataset = SourceDataset(source="linkedin", mode="simulated", state=state, role="primary")
        session.add(dataset)
        await session.flush()
        return dataset.id


async def _store(
    sessions: Sessions, dataset_id: int, maps: Sequence[FormVersionMap], *, now: datetime
) -> int:
    """Run ``_store_form_versions`` in a committed transaction of its own, as the jobs do."""
    async with sessions() as session, session.begin():
        return await _store_form_versions(session, dataset_id, maps, now=now)


async def _versions(sessions: Sessions, dataset_id: int) -> Stored:
    """Every stored version of ``dataset_id`` with its email ids and first sighting."""
    async with sessions() as session:
        result = await session.execute(
            select(
                LeadForm.canonical_urn,
                LeadFormVersion.version,
                LeadFormVersion.email_question_ids,
                LeadFormVersion.first_seen_at,
            )
            .join(LeadForm, LeadForm.id == LeadFormVersion.lead_form_id)
            .where(LeadFormVersion.dataset_id == dataset_id)
        )
        return {
            (urn, version): (list(question_ids), first_seen_at)
            for urn, version, question_ids, first_seen_at in result
        }


@contextmanager
def _statements(sessions: Sessions) -> Iterator[list[tuple[str, int]]]:
    """Record ``(sql, parameter count)`` for every statement the engine sends.

    An ``executemany`` sends one statement per parameter set, each recorded with the largest
    set's count, so a per-row loop is never hidden behind one cursor call.
    """
    engine = sessions.kw["bind"]
    sent: list[tuple[str, int]] = []

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: Any,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, context
        if executemany:
            count = max((len(parameter_set) for parameter_set in parameters), default=0)
            sent.extend((statement, count) for _ in parameters)
        else:
            sent.append((statement, len(parameters) if parameters else 0))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _updates(sent: Sequence[tuple[str, int]]) -> list[int]:
    """Parameter counts of the ``UPDATE lead_form_version`` statements among ``sent``."""
    return [count for sql, count in sent if sql.startswith(f"UPDATE {_TABLE} ")]


def _inserts(sent: Sequence[tuple[str, int]]) -> list[int]:
    """Parameter counts of the ``INSERT INTO lead_form_version`` statements among ``sent``."""
    return [count for sql, count in sent if sql.startswith(f"INSERT INTO {_TABLE} ")]


def _row_locks(sent: Sequence[tuple[str, int]]) -> list[str]:
    """The row-lock ``SELECT`` statements on ``lead_form_version`` among ``sent``."""
    return [
        sql
        for sql, _ in sent
        if sql.startswith(f"SELECT {_TABLE}.id ") and "FOR NO KEY UPDATE" in sql
    ]


def _reads(sent: Sequence[tuple[str, int]]) -> list[str]:
    """The plain (unlocked) reads of ``lead_form_version`` among ``sent``."""
    return [
        sql
        for sql, _ in sent
        if sql.startswith("SELECT ") and f"FROM {_TABLE} " in sql and "FOR " not in sql
    ]


async def test_new_versions_are_inserted_with_first_seen_at_now(db: Sessions) -> None:
    """New versions get ``first_seen_at = now``; unknown forms are stubbed; nothing updated."""
    dataset_id = await _linkedin_dataset(db)
    maps = [
        _map(FORM_A, 1, 7, 3),
        _map(FORM_A, 2, 9),
        _map(FORM_B, 1),
        _map(FORM_A, 2, 11),
    ]

    with _statements(db) as sent:
        written = await _store(db, dataset_id, maps, now=NOW)

    assert written == 3
    assert await _versions(db, dataset_id) == {
        (FORM_A, 1): (["7", "3"], NOW),
        (FORM_A, 2): (["11"], NOW),
        (FORM_B, 1): ([], NOW),
    }
    assert _inserts(sent), "new versions go through the upsert"
    assert _updates(sent) == []
    assert _row_locks(sent) == []
    async with db() as session:
        forms = set(
            await session.scalars(
                select(LeadForm.canonical_urn).where(LeadForm.dataset_id == dataset_id)
            )
        )
    assert forms == {FORM_A, FORM_B}


async def test_unchanged_known_versions_send_no_write(db: Sessions) -> None:
    """A re-sighting of stored maps is one read: no INSERT, UPDATE or lock; first sighting kept."""
    dataset_id = await _linkedin_dataset(db)
    maps = [_map(FORM_A, version, version, 500) for version in range(1, 21)]
    maps.append(_map(FORM_B, 4))
    assert await _store(db, dataset_id, maps, now=FIRST_SEEN) == 21
    before = await _versions(db, dataset_id)

    with _statements(db) as sent:
        written = await _store(db, dataset_id, maps, now=NOW)

    assert written == 0
    assert len(_reads(sent)) == 1, "the stored versions are read in one query"
    assert _inserts(sent) == []
    assert _updates(sent) == []
    assert _row_locks(sent) == []
    after = await _versions(db, dataset_id)
    assert after == before
    assert {first_seen for _, first_seen in after.values()} == {FIRST_SEEN}


async def test_changed_versions_are_rewritten_in_one_statement(db: Sessions) -> None:
    """50 changed maps take one id-ordered lock and one UPDATE; ``first_seen_at`` is kept."""
    dataset_id = await _linkedin_dataset(db)
    seeded = [_map(FORM_A, version, version, 500) for version in range(1, 61)]
    assert await _store(db, dataset_id, seeded, now=FIRST_SEEN) == 60

    reordered = [_map(FORM_A, version, 500, version) for version in range(1, 21)]
    replaced = [_map(FORM_A, version, version + 2_000) for version in range(21, 41)]
    emptied = [_map(FORM_A, version) for version in range(41, 51)]
    unchanged = seeded[50:]
    new = [_map(FORM_A, 61, 1), _map(FORM_A, 62, 2)]
    with _statements(db) as sent:
        written = await _store(
            db, dataset_id, [*reordered, *replaced, *emptied, *unchanged, *new], now=NOW
        )

    assert written == 52, "50 changed plus 2 new; the 10 unchanged are not written"
    updates = _updates(sent)
    assert len(updates) == 1
    assert updates[0] == 2 * 50 + 1, "two binds per changed row plus the dataset guard"
    locks = _row_locks(sent)
    assert len(locks) == 1
    assert f"ORDER BY {_TABLE}.id FOR NO KEY UPDATE" in locks[0]
    statements = [sql for sql, _ in sent]
    first_update = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE "))
    assert statements.index(locks[0]) < first_update, "rows are locked in id order first"

    expected: Stored = {}
    for version in range(1, 21):
        expected[(FORM_A, version)] = (["500", str(version)], FIRST_SEEN)
    for version in range(21, 41):
        expected[(FORM_A, version)] = ([str(version + 2_000)], FIRST_SEEN)
    for version in range(41, 51):
        expected[(FORM_A, version)] = ([], FIRST_SEEN)
    for version in range(51, 61):
        expected[(FORM_A, version)] = ([str(version), "500"], FIRST_SEEN)
    expected[(FORM_A, 61)] = (["1"], NOW)
    expected[(FORM_A, 62)] = (["2"], NOW)
    assert await _versions(db, dataset_id) == expected


async def test_changed_versions_beyond_one_chunk_take_bounded_statements(db: Sessions) -> None:
    """One more changed version than a chunk holds takes two UPDATEs, each bind-bounded."""
    count = _ID_CHUNK + 1
    dataset_id = await _linkedin_dataset(db)
    seeded = [_map(FORM_A, version, 1) for version in range(count)]
    assert await _store(db, dataset_id, seeded, now=FIRST_SEEN) == count

    with _statements(db) as sent:
        written = await _store(
            db, dataset_id, [_map(FORM_A, version, 2) for version in range(count)], now=NOW
        )

    assert written == count
    updates = _updates(sent)
    assert len(updates) == 2
    assert max(updates) <= 2 * _ID_CHUNK + 1
    assert len(_row_locks(sent)) == 2
    stored = await _versions(db, dataset_id)
    assert len(stored) == count
    assert all(value == (["2"], FIRST_SEEN) for value in stored.values())


async def test_another_datasets_versions_are_untouched(db: Sessions) -> None:
    """Rewriting one dataset's versions leaves the same form's versions elsewhere unchanged."""
    target = await _linkedin_dataset(db)
    other = await _linkedin_dataset(db)
    seeded = [_map(FORM_A, version, 1) for version in range(1, 4)]
    assert await _store(db, target, seeded, now=FIRST_SEEN) == 3
    assert await _store(db, other, seeded, now=FIRST_SEEN) == 3
    other_before = await _versions(db, other)

    changed = [_map(FORM_A, version, 2) for version in range(1, 4)]
    written = await _store(db, target, [*changed, _map(FORM_A, 4, 3)], now=NOW)

    assert written == 4
    assert await _versions(db, other) == other_before
    assert await _versions(db, target) == {
        (FORM_A, 1): (["2"], FIRST_SEEN),
        (FORM_A, 2): (["2"], FIRST_SEEN),
        (FORM_A, 3): (["2"], FIRST_SEEN),
        (FORM_A, 4): (["3"], NOW),
    }


async def test_retired_dataset_refuses_a_changed_version(db: Sessions) -> None:
    """A changed version of a retired dataset raises ``DatasetNotWritable``; nothing changes."""
    dataset_id = await _linkedin_dataset(db)
    assert await _store(db, dataset_id, [_map(FORM_A, 1, 1)], now=FIRST_SEEN) == 1
    async with db() as session, session.begin():
        await session.execute(
            update(SourceDataset)
            .where(SourceDataset.id == dataset_id)
            .values(state="retired", retired_at=NOW)
        )

    with pytest.raises(DatasetNotWritable):
        await _store(db, dataset_id, [_map(FORM_A, 1, 2)], now=NOW)

    assert await _versions(db, dataset_id) == {(FORM_A, 1): (["1"], FIRST_SEEN)}
