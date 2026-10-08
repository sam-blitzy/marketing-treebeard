"""Integration tests of the stand-in entity insert of the sync jobs (``jobs._insert_missing``).

Metrics, segment and lead jobs stub the campaigns, ad groups, creatives and lead forms their
rows reference before any entity sync stored them (AAP 0.3.3 "Queries issued": metrics carry
no status filter, so spend by a campaign removed before tracking still counts). At the scale
AAP 0.1.3 sets (thousands to low millions of rows) and the 25,000-campaign keyset scan of AAP
0.11.2, one batch of stubs can exceed PostgreSQL's limit of 65,535 bind parameters per
statement: a campaign stub binds seven columns, so 9,363 stubs in one statement fail.

Covered, on the PostgreSQL 18 test database through the conftest ``db`` fixture:

* **Bind budget**: 12,000 campaign stubs inserted by ``_stub_campaigns`` in one call are all
  stored with the dataset's ``dataset_id`` and ``source``, and no executed statement carries
  more than 65,535 parameters;
* **Never overwrite**: an entity an entity sync stored keeps every column when a stub with its
  natural key arrives (``ON CONFLICT DO NOTHING``);
* **Caller's transaction**: the helper never commits, so a caller's rollback discards every
  stub it inserted;
* **Idempotence**: re-running the same 12,000 stubs adds nothing and keeps every id;
* **Lock order**: rows are inserted in ascending natural-key order whatever the caller's order,
  observed in the ids the inserts allocate;
* **Guards**: a retired or missing dataset raises ``DatasetNotWritable`` and writes nothing; a
  row lacking a natural-key value raises ``NaturalKeyError`` before any statement; an empty
  batch returns 0 and sends no statement.
"""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Final

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.repositories import DatasetNotWritable, NaturalKeyError
from ads_engine.sync.jobs import _insert_missing, _stub_campaigns

type Sessions = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.anyio

BIND_LIMIT: Final = 65_535
"""PostgreSQL's maximum number of bind parameters in one statement (a 16-bit count)."""

STUBS: Final = 12_000
"""Campaign stubs per batch: above the 9,363 at which seven bound columns pass the limit."""


def _externals(count: int) -> list[str]:
    """``count`` distinct numeric Google campaign ids, in descending order (the worst case)."""
    return [str(9_000_000_000 + index) for index in reversed(range(count))]


def _stub_rows(account_id: int, externals: Sequence[str]) -> list[dict[str, Any]]:
    """Campaign stub rows shaped as ``_stub_campaigns`` builds them, in the given order."""
    return [
        {
            "account_id": account_id,
            "external_id": external,
            "name": f"Campaign {external}",
            "status": "ended",
            "raw_status": "UNKNOWN",
        }
        for external in externals
    ]


async def _google_dataset(sessions: Sessions, state: str = "active") -> tuple[int, int]:
    """Commit a Google primary dataset in ``state`` with one ad account.

    Returns:
        ``(dataset_id, account_id)``.
    """
    async with sessions() as session, session.begin():
        dataset = SourceDataset(source="google", mode="simulated", state=state, role="primary")
        session.add(dataset)
        await session.flush()
        account = AdAccount(
            source="google",
            dataset_id=dataset.id,
            external_id="1234567890",
            name="Acme",
            currency="USD",
            timezone="America/New_York",
        )
        session.add(account)
        await session.flush()
        return dataset.id, account.id


async def _campaigns(sessions: Sessions, dataset_id: int) -> list[Campaign]:
    """Every stored campaign of ``dataset_id``, in ``id`` (allocation) order."""
    async with sessions() as session:
        result = await session.scalars(
            select(Campaign).where(Campaign.dataset_id == dataset_id).order_by(Campaign.id)
        )
        return list(result)


async def _campaign_count(sessions: Sessions) -> int:
    """The number of stored campaigns across every dataset."""
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(Campaign)) or 0)


@contextmanager
def _statement_parameters(sessions: Sessions) -> Iterator[list[tuple[str, int]]]:
    """Record ``(sql, parameter count)`` for every statement the engine sends.

    An ``executemany`` sends one statement per parameter set, so its count is the largest
    set's; a single statement (including an insertmanyvalues page) counts its own parameters.
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
        else:
            count = len(parameters) if parameters else 0
        sent.append((statement, count))

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _inserts(sent: Sequence[tuple[str, int]], table: str) -> list[int]:
    """Parameter counts of the ``INSERT INTO <table>`` statements among ``sent``."""
    prefix = f"INSERT INTO {table} "
    return [count for statement, count in sent if statement.lstrip().startswith(prefix)]


async def test_twelve_thousand_campaign_stubs_stay_under_the_bind_parameter_limit(
    db: Sessions,
) -> None:
    dataset_id, account_id = await _google_dataset(db)
    externals = _externals(STUBS)
    known: dict[str, int] = {}

    with _statement_parameters(db) as sent:
        async with db() as session, session.begin():
            requested = await _stub_campaigns(session, dataset_id, account_id, externals, known)

    assert requested == STUBS
    assert sorted(known) == sorted(externals)
    counts = _inserts(sent, "campaign")
    assert counts, "no INSERT INTO campaign statement was sent"
    assert max(counts) <= BIND_LIMIT
    stored = await _campaigns(db, dataset_id)
    assert len(stored) == STUBS
    assert {row.external_id for row in stored} == set(externals)
    assert {(row.dataset_id, row.source, row.account_id) for row in stored} == {
        (dataset_id, "google", account_id)
    }
    assert {(row.status, row.raw_status) for row in stored} == {("ended", "UNKNOWN")}
    assert {row.id: row.external_id for row in stored} == {
        known[external]: external for external in externals
    }


async def test_a_stub_never_overwrites_a_stored_entity(db: Sessions) -> None:
    dataset_id, account_id = await _google_dataset(db)
    async with db() as session, session.begin():
        stored = Campaign(
            source="google",
            dataset_id=dataset_id,
            account_id=account_id,
            external_id="9000000005",
            name="Brand search",
            status="active",
            raw_status="ENABLED",
            objective_or_channel="SEARCH",
        )
        session.add(stored)
        await session.flush()
        stored_id = stored.id
    externals = _externals(10)

    async with db() as session, session.begin():
        requested = await _insert_missing(
            session,
            Campaign,
            Campaign.__natural_key__,
            _stub_rows(account_id, externals),
            dataset_id,
        )

    assert requested == len(externals)
    rows = {row.external_id: row for row in await _campaigns(db, dataset_id)}
    assert set(rows) == set(externals)
    kept = rows["9000000005"]
    assert (kept.id, kept.name, kept.status, kept.raw_status, kept.objective_or_channel) == (
        stored_id,
        "Brand search",
        "active",
        "ENABLED",
        "SEARCH",
    )
    others = [row for external, row in rows.items() if external != "9000000005"]
    assert {(row.status, row.raw_status) for row in others} == {("ended", "UNKNOWN")}


async def test_the_stubs_belong_to_the_callers_transaction(db: Sessions) -> None:
    dataset_id, account_id = await _google_dataset(db)
    async with db() as session:
        await session.begin()
        requested = await _insert_missing(
            session,
            Campaign,
            Campaign.__natural_key__,
            _stub_rows(account_id, _externals(STUBS)),
            dataset_id,
        )
        assert session.in_transaction()
        assert int(await session.scalar(select(func.count()).select_from(Campaign)) or 0) == STUBS
        await session.rollback()

    assert requested == STUBS
    assert await _campaign_count(db) == 0


async def test_re_running_the_same_stubs_is_idempotent(db: Sessions) -> None:
    dataset_id, account_id = await _google_dataset(db)
    rows = _stub_rows(account_id, _externals(STUBS))
    async with db() as session, session.begin():
        await _insert_missing(session, Campaign, Campaign.__natural_key__, rows, dataset_id)
    first = {row.external_id: row.id for row in await _campaigns(db, dataset_id)}

    with _statement_parameters(db) as sent:
        async with db() as session, session.begin():
            requested = await _insert_missing(
                session, Campaign, Campaign.__natural_key__, rows, dataset_id
            )

    assert requested == STUBS
    assert max(_inserts(sent, "campaign")) <= BIND_LIMIT
    second = {row.external_id: row.id for row in await _campaigns(db, dataset_id)}
    assert second == first
    assert await _campaign_count(db) == STUBS


async def test_rows_are_inserted_in_ascending_natural_key_order(db: Sessions) -> None:
    dataset_id, account_id = await _google_dataset(db)
    # Interleaved and unsorted, with ids whose string order differs from their numeric order.
    externals = ["31", "4", "1000", "200", "9", "17", "5000000000", "65"]
    async with db() as session, session.begin():
        await _insert_missing(
            session,
            Campaign,
            Campaign.__natural_key__,
            _stub_rows(account_id, externals),
            dataset_id,
        )

    allocated = [row.external_id for row in await _campaigns(db, dataset_id)]
    assert allocated == sorted(externals)


@pytest.mark.parametrize("state", ["retired", None], ids=["retired", "missing"])
async def test_a_retired_or_missing_dataset_is_refused(db: Sessions, state: str | None) -> None:
    if state == "retired":
        target, account_id = await _google_dataset(db, "retired")
    else:
        dataset_id, account_id = await _google_dataset(db)
        target = dataset_id + 1_000

    async with db() as session, session.begin():
        with pytest.raises(DatasetNotWritable) as refused:
            await _insert_missing(
                session,
                Campaign,
                Campaign.__natural_key__,
                _stub_rows(account_id, _externals(3)),
                target,
            )

    assert refused.value.dataset_id == target
    assert refused.value.state == state
    assert await _campaign_count(db) == 0


@pytest.mark.parametrize(
    "broken",
    [{"external_id": None}, {"account_id": None}, {}],
    ids=["external_id_none", "account_id_none", "external_id_absent"],
)
async def test_a_row_without_a_natural_key_value_is_refused_before_any_insert(
    db: Sessions, broken: Mapping[str, Any]
) -> None:
    dataset_id, account_id = await _google_dataset(db)
    rows = _stub_rows(account_id, _externals(3))
    if broken:
        rows[1] = {**rows[1], **broken}
    else:
        del rows[1]["external_id"]

    with _statement_parameters(db) as sent:
        async with db() as session, session.begin():
            with pytest.raises(NaturalKeyError, match="Campaign row 1 lacks natural-key values"):
                await _insert_missing(session, Campaign, Campaign.__natural_key__, rows, dataset_id)

    assert _inserts(sent, "campaign") == []
    assert await _campaign_count(db) == 0


async def test_an_empty_batch_sends_no_statement(db: Sessions) -> None:
    dataset_id, _ = await _google_dataset(db)

    with _statement_parameters(db) as sent:
        async with db() as session, session.begin():
            requested = await _insert_missing(
                session, Campaign, Campaign.__natural_key__, [], dataset_id
            )

    assert requested == 0
    assert sent == []
