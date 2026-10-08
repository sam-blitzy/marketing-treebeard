"""Idempotent dataset-scoped upserts, append-only snapshots and shared dataset reads.

AAP 0.5.1 ("Repositories") puts the write path of every sync job here, together with the
few reads that ``analysis`` and ``api`` share with ``sync`` (so neither imports ``sync``):

* :func:`upsert_facts` and :func:`upsert_entities` write rows with ``INSERT ... ON CONFLICT
  (<__natural_key__>) DO UPDATE``. Re-running a sync is therefore safe, and restated
  platform data overwrites the earlier values (AAP 0.1.1, 0.3.2).
* :func:`append_snapshots` appends ``entity_snapshot`` observations and never updates one
  (AAP 0.5.2).
* :func:`lock_dataset`, :func:`ensure_writable`, :func:`active_datasets`,
  :func:`active_dataset_id` and :func:`effective_crm_dataset_id` read ``source_dataset``
  (AAP 0.3.3).
* :func:`enqueue_analysis_request` queues a default-window rerun (AAP 0.3.2).
* :func:`sync_content_assets` rewrites the white paper registry from
  ``config/content_assets.yaml`` (AAP 0.5.2).

Rules every function here follows:

* **Provenance**: every write is scoped to one ``dataset_id`` and first passes
  :func:`ensure_writable`, because a retired dataset is read-only until it is purged
  (AAP 0.3.3). Its lifecycle lock lasts until the caller's transaction ends, so a
  dataset cannot retire between the check and the commit. ``source`` is taken from the
  dataset, never trusted from the row.
* **Value types**: values arrive as ``Decimal``, tz-aware ``datetime`` and native ``date``
  and are written unchanged. A ``float`` or a naive ``datetime`` is rejected with
  ``ValueError`` rather than coerced, and dates are never rebucketed (AAP 0.5.4.1).
* **Transactions**: nothing here commits, rolls back or opens a transaction in a
  caller's session. Callers wrap the work in :func:`ads_engine.db.session.session_scope`
  and pass that session, so data, freshness and the ``analysis_request`` they trigger
  commit atomically. Functions may ``flush()``. :func:`upsert_facts` and
  :func:`append_snapshots` keep the AAP 0.5.1 signatures, ``upsert_facts(table, rows,
  dataset_id)`` and ``append_snapshots(rows)``, with ``session`` as an optional keyword:
  given, the call joins it; omitted, the call runs in a unit of work of its own
  (:func:`ads_engine.db.session.use_session`).
* **Model contract**: every upsertable model declares ``__natural_key__``, the NOT NULL
  columns of its natural-key unique constraint. That contract is verified at call time and
  a violation raises :class:`NaturalKeyError`.

Example::

    async with session_scope() as session:
        ids = await upsert_entities(session, Campaign, campaign_rows, dataset_id)
        await upsert_facts(FactCampaignDaily, daily_rows, dataset_id, session=session)
        await enqueue_analysis_request(session, "metrics_sync")
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Any, Final, TypedDict

from sqlalchemy import PrimaryKeyConstraint, Table, UniqueConstraint, delete, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.db.base import Base, utcnow
from ads_engine.db.models import (
    AnalysisRequest,
    ContentAsset,
    ContentAssetCreative,
    ContentAssetForm,
    EntitySnapshot,
    SourceDataset,
)
from ads_engine.db.session import use_session
from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Source

# ``Row`` is listed with the functions because it appears in their public signatures, so
# callers type their row lists with it.
__all__ = [
    "ContentAssetRow",
    "DatasetNotWritable",
    "NaturalKeyError",
    "Row",
    "active_dataset_id",
    "active_datasets",
    "append_snapshots",
    "effective_crm_dataset_id",
    "enqueue_analysis_request",
    "ensure_writable",
    "entity_id_map",
    "lock_dataset",
    "sync_content_assets",
    "upsert_entities",
    "upsert_facts",
]

_log = get_logger(__name__)

Row = Mapping[str, Any]
"""One row to write: column name to value, in the model's own column names."""

_MAX_REASON_LENGTH: Final[int] = 200
"""Longest ``analysis_request.reason`` accepted by :func:`enqueue_analysis_request`."""

# Columns an ON CONFLICT update never rewrites: the surrogate key and the provenance pair.
# A conflicting row already carries this dataset's id and source, so rewriting them could
# only ever move a row into another dataset, which provenance forbids.
_NEVER_UPDATED: Final[frozenset[str]] = frozenset({"id", "dataset_id", "source"})


class ContentAssetRow(TypedDict):
    """One white paper of ``config/content_assets.yaml``, as :func:`sync_content_assets` takes it.

    ``analysis/thresholds.py`` builds these from the YAML and imports this type from here,
    because ``db`` never imports ``analysis``.

    Attributes:
        slug: The asset's stable identifier.
        title: The asset's display title.
        lead_form_urns: Canonical ``urn:li:leadGenForm:{id}`` URNs of the forms that gate it.
        creative_urns: Creative URNs credited to the asset by override.
    """

    slug: str
    title: str
    lead_form_urns: list[str]
    creative_urns: list[str]


# The name is part of the repository contract that sync, decisions and the API import, so
# pep8-naming's N818 rule (an "Error" suffix) gives way to it.
class DatasetNotWritable(RuntimeError):  # noqa: N818 - name fixed by the repository contract
    """A write targeted a dataset that does not exist or has been retired.

    AAP 0.3.3: retired datasets are read-only, kept 30 days and then purged.

    Attributes:
        dataset_id: The dataset the write targeted.
        state: The dataset's state (``retired``), or ``None`` when no such dataset exists.
    """

    def __init__(self, dataset_id: int, state: str | None) -> None:
        self.dataset_id = dataset_id
        self.state = state
        if state is None:
            message = f"source_dataset {dataset_id} does not exist"
        else:
            message = f"source_dataset {dataset_id} is {state} and read-only"
        super().__init__(message)


class NaturalKeyError(TypeError):
    """A model or a row does not meet the natural-key contract the upserts rely on.

    Raised when a model lacks ``__natural_key__``, a ``dataset_id`` column, an ``id`` column
    (entities) or a ``fetched_at`` column (facts); when its natural key names an unknown or
    nullable column or matches no unique constraint; or when a row lacks a natural-key value.
    """


@dataclass(frozen=True, slots=True)
class _ModelInfo:
    """What the upserts need to know about one model, verified once per model."""

    name: str
    table: Table
    columns: frozenset[str]
    natural_key: tuple[str, ...]
    has_id: bool
    has_source: bool
    has_dataset_id: bool
    has_fetched_at: bool


def _table_of(model: type[Base]) -> Table:
    """Return the ``Table`` a model is mapped to.

    Args:
        model: A mapped model class.

    Returns:
        The model's table.

    Raises:
        NaturalKeyError: If the model is not mapped to a plain table.
    """
    table = model.__table__
    if not isinstance(table, Table):
        raise NaturalKeyError(f"{model.__name__} is not mapped to a table")
    return table


def _natural_key(model: type[Base]) -> tuple[str, ...]:
    """Read and check a model's ``__natural_key__``.

    Args:
        model: A mapped model class.

    Returns:
        The natural-key column names, in declaration order.

    Raises:
        NaturalKeyError: If ``__natural_key__`` is absent, empty, not a tuple of strings,
            repeats a column, or names a column the model's table does not have.
    """
    key = getattr(model, "__natural_key__", None)
    if not isinstance(key, tuple) or not key:
        raise NaturalKeyError(f"{model.__name__} declares no __natural_key__")
    names: list[str] = []
    for name in key:
        if not isinstance(name, str):
            raise NaturalKeyError(f"{model.__name__}.__natural_key__ holds non-string {name!r}")
        names.append(name)
    if len(set(names)) != len(names):
        raise NaturalKeyError(f"{model.__name__}.__natural_key__ repeats a column: {names}")
    table = _table_of(model)
    unknown = [name for name in names if name not in table.c]
    if unknown:
        raise NaturalKeyError(
            f"{model.__name__}.__natural_key__ names columns {table.name} lacks: {unknown}"
        )
    return tuple(names)


def _has_unique_target(table: Table, key: tuple[str, ...]) -> bool:
    """Tell whether ``key`` is exactly the column set of a non-partial unique constraint.

    ``ON CONFLICT (<columns>)`` needs a unique index over exactly those columns with no
    ``WHERE`` clause; checking here turns PostgreSQL's late "no unique or exclusion
    constraint matching the ON CONFLICT specification" into an immediate, named error.

    Args:
        table: The model's table.
        key: The natural-key column names.

    Returns:
        ``True`` when a primary key, unique constraint or plain unique index matches.
    """
    wanted = frozenset(key)
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint | PrimaryKeyConstraint) and (
            frozenset(constraint.columns.keys()) == wanted
        ):
            return True
    for index in table.indexes:
        if (
            index.unique
            and index.dialect_options["postgresql"]["where"] is None
            and len(index.expressions) == len(wanted)
            and frozenset(column.key for column in index.columns) == wanted
        ):
            return True
    return False


@cache
def _model_info(model: type[Base]) -> _ModelInfo:
    """Verify a model against the natural-key contract and describe it.

    The result is cached per model class: the mapped schema never changes at runtime.

    Args:
        model: A mapped model class.

    Returns:
        The verified description of the model.

    Raises:
        NaturalKeyError: If the model breaks the contract described in the module docstring.
    """
    table = _table_of(model)
    key = _natural_key(model)
    nullable = [name for name in key if table.c[name].nullable]
    if nullable:
        raise NaturalKeyError(
            f"{model.__name__}.__natural_key__ names nullable columns {nullable}; a NULL "
            "never matches ON CONFLICT, so every re-sync would insert a duplicate"
        )
    if not _has_unique_target(table, key):
        raise NaturalKeyError(
            f"{model.__name__}.__natural_key__ {list(key)} matches no unique constraint "
            f"of {table.name}"
        )
    columns = frozenset(table.c.keys())
    return _ModelInfo(
        name=table.name,
        table=table,
        columns=columns,
        natural_key=key,
        has_id="id" in columns,
        has_source="source" in columns,
        has_dataset_id="dataset_id" in columns,
        has_fetched_at="fetched_at" in columns,
    )


def _scoped_info(model: type[Base]) -> _ModelInfo:
    """Return a model's description, requiring the ``dataset_id`` provenance column.

    Args:
        model: A mapped model class.

    Returns:
        The verified description of a dataset-scoped model.

    Raises:
        NaturalKeyError: If the model breaks the contract or carries no ``dataset_id``.
    """
    info = _model_info(model)
    if not info.has_dataset_id:
        raise NaturalKeyError(
            f"{model.__name__} has no dataset_id column; it is not dataset-scoped"
        )
    return info


def _as_source(value: str) -> Source:
    """Narrow a stored ``source`` value to the :data:`~ads_engine.settings.Source` literal.

    Args:
        value: The stored value.

    Returns:
        The same value, typed as a source.

    Raises:
        ValueError: If the value is not one of :data:`ads_engine.settings.SOURCES`.
    """
    for source in SOURCES:
        if value == source:
            return source
    raise ValueError(f"unknown source {value!r}; expected one of {list(SOURCES)}")


def _check_value(table: str, column: str, value: object) -> None:
    """Reject a value whose type would lose meaning on the way into PostgreSQL.

    Decision 6 (value types): money and conversions travel as ``Decimal`` and instants as
    tz-aware ``datetime``. A ``float`` would be bound as ``float8`` and silently rounded
    into ``numeric``; a naive ``datetime`` would be read in the session time zone. Both are
    refused here instead of coerced.

    Args:
        table: Table name, for the error message.
        column: Column name, for the error message.
        value: The value to check.

    Raises:
        ValueError: If ``value`` is a ``float`` or a naive ``datetime``.
    """
    if isinstance(value, float):
        raise ValueError(
            f"{table}.{column}: float {value!r} is not accepted; pass Decimal for amounts "
            "and conversions or int for counts"
        )
    if isinstance(value, datetime) and value.utcoffset() is None:
        raise ValueError(f"{table}.{column}: naive datetime {value!r}; instants must be tz-aware")


def _prepare(rows: Sequence[Row], info: _ModelInfo, dataset: SourceDataset) -> list[dict[str, Any]]:
    """Copy, scope, validate and de-duplicate rows for one upsert statement.

    Each row is copied, given the dataset's ``dataset_id`` and, when the model has the
    column, the dataset's ``source``. Rows are de-duplicated on the natural key with the
    last occurrence winning: PostgreSQL rejects an ``ON CONFLICT DO UPDATE`` that touches
    the same row twice in one statement, and a restated row later in the batch carries the
    newest values.

    Args:
        rows: The caller's rows.
        info: The verified model description.
        dataset: The writable target dataset.

    Returns:
        One dict per distinct natural key, in first-occurrence order.

    Raises:
        ValueError: If a row carries an unknown column, another ``dataset_id`` or
            ``source``, a ``float`` or a naive ``datetime``.
        NaturalKeyError: If a row lacks a natural-key value or carries ``None`` for one.
    """
    prepared: dict[tuple[Any, ...], dict[str, Any]] = {}
    for position, row in enumerate(rows):
        values = dict(row)
        unknown = sorted(set(values) - info.columns)
        if unknown:
            raise ValueError(f"{info.name} row {position} carries unknown columns {unknown}")
        if "dataset_id" in values and values["dataset_id"] != dataset.id:
            raise ValueError(
                f"{info.name} row {position} carries dataset_id {values['dataset_id']!r}, "
                f"not the target dataset {dataset.id}"
            )
        values["dataset_id"] = dataset.id
        if info.has_source:
            if "source" in values and values["source"] != dataset.source:
                raise ValueError(
                    f"{info.name} row {position} carries source {values['source']!r}, but "
                    f"dataset {dataset.id} belongs to {dataset.source!r}"
                )
            values["source"] = dataset.source
        missing = [name for name in info.natural_key if values.get(name) is None]
        if missing:
            raise NaturalKeyError(f"{info.name} row {position} lacks natural-key values {missing}")
        for column, value in values.items():
            _check_value(info.name, column, value)
        prepared[tuple(values[name] for name in info.natural_key)] = values
    return list(prepared.values())


def _group_by_columns(
    rows: Sequence[dict[str, Any]],
) -> list[tuple[tuple[str, ...], list[tuple[int, dict[str, Any]]]]]:
    """Group rows by the exact set of columns they supply, keeping each row's position.

    One statement per column set means a column a row does not supply is neither inserted
    as ``NULL`` nor overwritten on conflict: it keeps its server default on insert and its
    stored value on update. Groups and their members keep first-occurrence order.

    Args:
        rows: Prepared rows.

    Returns:
        ``(columns, [(position, row), ...])`` pairs, columns sorted by name.
    """
    groups: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = {}
    for position, values in enumerate(rows):
        groups.setdefault(tuple(sorted(values)), []).append((position, values))
    return list(groups.items())


def _upsert_statement(model: type[Base], info: _ModelInfo, columns: Sequence[str]) -> Insert:
    """Build ``INSERT ... ON CONFLICT (<natural key>) DO UPDATE`` for one column set.

    The update sets every supplied column except the natural key, ``id`` and the
    provenance pair (``dataset_id``, ``source``), each to its ``EXCLUDED`` value: restated
    metrics overwrite, and a column supplied as ``None`` becomes ``NULL`` ("unavailable",
    never 0). When nothing is left to set (a row of key columns only, such as an
    association) the last key column is set to itself, a no-op that still makes
    ``RETURNING`` report the existing row.

    Where the natural key does not include ``dataset_id`` (``lead_form_version``, scoped
    through its parent form), the update is guarded with ``WHERE <table>.dataset_id =
    EXCLUDED.dataset_id``, so a conflicting row of another dataset is never rewritten.

    Args:
        model: The mapped model class.
        info: The verified model description.
        columns: The columns the rows of this statement supply.

    Returns:
        The ORM-enabled PostgreSQL upsert statement.
    """
    statement = insert(model)
    updated = [name for name in columns if name not in info.natural_key]
    updated = [name for name in updated if name not in _NEVER_UPDATED]
    if not updated:
        updated = [info.natural_key[-1]]
    where = None
    if "dataset_id" not in info.natural_key:
        where = info.table.c.dataset_id == statement.excluded.dataset_id
    return statement.on_conflict_do_update(
        index_elements=list(info.natural_key),
        set_={name: statement.excluded[name] for name in updated},
        where=where,
    )


async def lock_dataset(
    session: AsyncSession, dataset_id: int, *, exclusive: bool = False
) -> SourceDataset | None:
    """Load one dataset under a lifecycle lock that conflicts with its retirement.

    AAP 0.3.3: one transaction activates a dataset and retires the previous one with its
    companions, and retired datasets are read-only. ``DatasetManager.activate`` locks the
    rows it retires ``FOR UPDATE`` and updates them, so the shared lock taken here makes
    the two serialise. A retirement waits for every writer already holding the lock until
    that writer's transaction ends, and a writer arriving while a retirement is
    uncommitted waits for it and then reads the committed ``retired`` state (READ
    COMMITTED re-check), so no write can land in a dataset retired after its check.

    The lock is held until the caller's transaction commits or rolls back. Every writer
    takes its locks in one order across the code base, the order activation uses:
    ``source_connection`` -> ``source_dataset`` (a primary before its companion) ->
    subject row (``campaign``, ``budget``) -> ``discrepancy`` rows.

    Args:
        session: The caller's session; its transaction holds the lock.
        dataset_id: The dataset to lock.
        exclusive: Take ``FOR NO KEY UPDATE`` instead of ``FOR SHARE``, for a caller that
            updates the dataset row itself: two shared holders that both upgrade would
            deadlock. It still lets the ``FOR KEY SHARE`` checks of foreign-key inserts
            through.

    Returns:
        The dataset as committed now (``populate_existing``, after the session's
        autoflush), or ``None`` when no such dataset exists.
    """
    return await session.get(
        SourceDataset,
        dataset_id,
        populate_existing=True,
        with_for_update={"key_share": True} if exclusive else {"read": True},
    )


async def ensure_writable(
    session: AsyncSession, dataset_id: int, *, exclusive: bool = False
) -> SourceDataset:
    """Lock a dataset and refuse writes to a missing or retired one.

    AAP 0.3.3 (datasets and provenance): retired datasets are read-only, kept 30 days and
    then purged. ``building`` and ``active`` datasets are writable. The row is read under
    :func:`lock_dataset`'s lifecycle lock, which is held until the caller's transaction
    ends: a retirement cannot commit between this check and the caller's commit, and a
    check that waited on a retirement sees it committed and refuses.

    Args:
        session: The caller's session; its transaction holds the lock.
        dataset_id: The dataset a write targets.
        exclusive: Lock ``FOR NO KEY UPDATE`` for a caller that then updates the dataset
            row (see :func:`lock_dataset`).

    Returns:
        The dataset, so callers get its ``source`` and ``mode``.

    Raises:
        DatasetNotWritable: If the dataset does not exist (``state=None``) or is retired.
    """
    dataset = await lock_dataset(session, dataset_id, exclusive=exclusive)
    if dataset is None:
        raise DatasetNotWritable(dataset_id, None)
    if dataset.state == "retired":
        raise DatasetNotWritable(dataset_id, dataset.state)
    return dataset


async def active_datasets(session: AsyncSession) -> dict[Source, SourceDataset]:
    """Return each source's active primary dataset.

    AAP 0.3.3 (active-dataset filter): reads, aggregations and rules filter to each
    source's single active primary dataset. The partial unique index
    ``uq_source_dataset_active_primary`` guarantees at most one per source. Companion
    datasets are never returned; see :func:`effective_crm_dataset_id`.

    Args:
        session: The caller's session.

    Returns:
        The active primary dataset keyed by source; a source with none is absent.
    """
    result = await session.execute(
        select(SourceDataset).where(
            SourceDataset.state == "active",
            SourceDataset.role == "primary",
        )
    )
    return {_as_source(dataset.source): dataset for dataset in result.scalars()}


async def active_dataset_id(session: AsyncSession, source: Source) -> int | None:
    """Return the id of one source's active primary dataset.

    AAP 0.3.3 (active-dataset filter); the convenience form of :func:`active_datasets`.

    Args:
        session: The caller's session.
        source: The source to look up.

    Returns:
        The dataset id, or ``None`` when the source has no active primary dataset.

    Raises:
        ValueError: If ``source`` is not a known source.
    """
    result = await session.execute(
        select(SourceDataset.id).where(
            SourceDataset.source == _as_source(source),
            SourceDataset.state == "active",
            SourceDataset.role == "primary",
        )
    )
    return result.scalar_one_or_none()


async def effective_crm_dataset_id(session: AsyncSession, linkedin_dataset_id: int) -> int | None:
    """Return the CRM dataset LinkedIn-attributed evidence reads for a LinkedIn dataset.

    AAP 0.3.3 ("Effective CRM dataset"). A simulated LinkedIn dataset reads its companion
    CRM dataset, so simulated leads never join live CRM records. A companion shares its
    parent's state, so this holds for a building parent as well as an active one. A live
    LinkedIn dataset, building or active, reads HubSpot's active primary dataset; that is
    also the dataset staged matching uses for a new live LinkedIn dataset.

    The Google-to-CRM link never uses this function: it always reads HubSpot's active
    primary dataset (:func:`active_dataset_id` with ``"hubspot"``).

    Args:
        session: The caller's session.
        linkedin_dataset_id: A LinkedIn dataset of any state.

    Returns:
        The companion's id for a simulated dataset (``None`` before the companion exists),
        or HubSpot's active primary id for a live one (``None`` when HubSpot has none).

    Raises:
        ValueError: If the dataset does not exist or is not a LinkedIn dataset.
    """
    dataset = await session.get(SourceDataset, linkedin_dataset_id)
    if dataset is None:
        raise ValueError(f"source_dataset {linkedin_dataset_id} does not exist")
    if dataset.source != "linkedin":
        raise ValueError(
            f"source_dataset {linkedin_dataset_id} belongs to {dataset.source!r}, not 'linkedin'"
        )
    if dataset.mode == "simulated":
        result = await session.execute(
            select(SourceDataset.id).where(
                SourceDataset.parent_dataset_id == linkedin_dataset_id,
                SourceDataset.role == "companion",
                SourceDataset.source == "hubspot",
            )
        )
        return result.scalar_one_or_none()
    return await active_dataset_id(session, "hubspot")


async def upsert_facts(
    table: type[Base],
    rows: Sequence[Row],
    dataset_id: int,
    *,
    session: AsyncSession | None = None,
) -> int:
    """Idempotently write daily fact rows into one dataset, restatements overwriting.

    AAP 0.5.1 ("Repositories"), 0.3.2 (fetch policy: daily facts are fetched incrementally
    and restated) and 0.1.1 (reliability: re-running a sync is safe). The statement is
    ``INSERT ... ON CONFLICT (<__natural_key__>) DO UPDATE SET <supplied columns> =
    EXCLUDED.<column>``:

    * a restated metric overwrites the stored one, and a metric supplied as ``None``
      overwrites it with ``NULL`` ("unavailable", never 0);
    * a column the rows do not supply is left untouched;
    * duplicate natural keys within ``rows`` keep the last occurrence;
    * ``fetched_at`` is owned by this function: every row gets the same ``utcnow()``
      instant of this call, replacing any value a row supplies, because the column records
      when the stored value was last written.

    Rows are executed as one ORM bulk upsert per supplied column set; SQLAlchemy's
    insertmanyvalues batching keeps each statement under PostgreSQL's bind-parameter limit.

    Fact foreign keys to entities are NOT NULL and dataset-qualified, so a fact row whose
    entity is not stored yet raises ``IntegrityError``. Callers stub the entity first with
    :func:`upsert_entities`, for example a campaign removed before first tracking that still
    has spend in the backfill window.

    The signature is AAP 0.5.1's ``upsert_facts(table, rows, dataset_id)``. A sync job
    passes its ``session`` so the facts commit with its freshness row and its
    ``analysis_request``; without one the write is its own unit of work.

    Args:
        table: The fact table's mapped model class (``FactCampaignDaily``,
            ``FactCreativeDaily``, ``FactKeywordDaily``, ``FactSearchTermDaily`` or
            ``FactSegmentWindow``).
        rows: Column-name rows; ``dataset_id`` and ``source`` may be omitted.
        dataset_id: The building or active dataset written to.
        session: The caller's session, joined without committing; the dataset guard is then
            held until the caller's transaction ends. ``None`` runs the write in a
            transaction of its own, committed on success and rolled back on error.

    Returns:
        The number of distinct rows written; 0, with no statement, for empty ``rows``.

    Raises:
        NaturalKeyError: If ``table`` breaks the contract, has no ``fetched_at``, its
            natural key omits ``dataset_id``, or a row lacks a natural-key value.
        DatasetNotWritable: If the dataset is missing or retired.
        ValueError: If a row carries an unknown column, another dataset or source, a
            ``float`` or a naive ``datetime``.
    """
    info = _scoped_info(table)
    if not info.has_fetched_at:
        raise NaturalKeyError(
            f"{table.__name__} has no fetched_at column; use upsert_entities for entity rows"
        )
    if "dataset_id" not in info.natural_key:
        raise NaturalKeyError(f"{table.__name__}.__natural_key__ must include dataset_id")
    if not rows:
        return 0
    async with use_session(session) as unit:
        dataset = await ensure_writable(unit, dataset_id)
        prepared = _prepare(rows, info, dataset)
        fetched_at = utcnow()
        for values in prepared:
            values["fetched_at"] = fetched_at
        for columns, members in _group_by_columns(prepared):
            statement = _upsert_statement(table, info, columns)
            await unit.execute(statement, [values for _, values in members])
    _log.debug("db.upsert_facts", table=info.name, dataset_id=dataset.id, rows=len(prepared))
    return len(prepared)


async def upsert_entities(
    session: AsyncSession,
    model: type[Base],
    rows: Sequence[Row],
    dataset_id: int,
) -> dict[tuple[Any, ...], int]:
    """Idempotently write entity rows into one dataset and return their internal ids.

    AAP 0.5.1 ("Repositories") and 0.3.2 (entity state is fetched in full every cycle).
    The same guard, preparation and ``ON CONFLICT (<__natural_key__>) DO UPDATE`` as
    :func:`upsert_facts`, plus ``RETURNING id, <natural key>``; PostgreSQL returns both
    inserted and updated rows. When the model has ``fetched_at`` (``crm_association_fetch``)
    every row gets this call's ``utcnow()`` instant.

    Sync jobs use it for campaign groups, campaigns, budgets, conversion actions, ad
    groups, keywords, creatives, lead forms, lead-form versions, leads, CRM contacts,
    meetings, deals, associations and association fetches. It never deletes:
    disappearance is handled by snapshots and discrepancies (AAP 0.3.3, "Safe absence").

    Args:
        session: The caller's session; nothing is committed here.
        model: An entity model with an ``id`` column and a ``__natural_key__``.
        rows: Column-name rows; ``dataset_id`` and ``source`` may be omitted.
        dataset_id: The building or active dataset written to.

    Returns:
        Each written row's natural-key tuple, in ``__natural_key__`` order and as stored,
        mapped to its ``id``; built from the returned rows, never from input order. An empty
        ``rows`` returns ``{}`` with no statement.

    Raises:
        NaturalKeyError: If ``model`` breaks the contract or has no ``id`` column (use
            :func:`upsert_facts`), or a row lacks a natural-key value.
        DatasetNotWritable: If the dataset is missing or retired.
        ValueError: If a row carries an unknown column, another dataset or source, a
            ``float`` or a naive ``datetime``, or its natural key belongs to a row of
            another dataset.
    """
    info = _scoped_info(model)
    if not info.has_id:
        raise NaturalKeyError(
            f"{model.__name__} has no id column; use upsert_facts for rows without ids"
        )
    if not rows:
        return {}
    dataset = await ensure_writable(session, dataset_id)
    prepared = _prepare(rows, info, dataset)
    if info.has_fetched_at:
        fetched_at = utcnow()
        for values in prepared:
            values["fetched_at"] = fetched_at
    key_columns = [info.table.c[name] for name in info.natural_key]
    ids: dict[tuple[Any, ...], int] = {}
    for columns, members in _group_by_columns(prepared):
        statement = _upsert_statement(model, info, columns).returning(info.table.c.id, *key_columns)
        result = await session.execute(statement, [values for _, values in members])
        for returned in result:
            stored = tuple(returned)
            ids[stored[1:]] = int(stored[0])
    if len(ids) != len(prepared):
        # Only the dataset guard of a key without dataset_id can skip a conflicting row.
        raise ValueError(
            f"{info.name}: {len(prepared) - len(ids)} of {len(prepared)} rows conflict with "
            f"rows of another dataset on {list(info.natural_key)}"
        )
    _log.debug("db.upsert_entities", table=info.name, dataset_id=dataset.id, rows=len(ids))
    return ids


async def entity_id_map(
    session: AsyncSession,
    model: type[Base],
    dataset_id: int,
) -> dict[tuple[Any, ...], int]:
    """Map every stored entity of one dataset from its natural key to its internal id.

    AAP 0.5.1 ("Repositories"). Metrics jobs use it to resolve external ids to the
    internal ids the fact rows reference, without re-upserting entities. It is a read, so
    retired datasets may be read too.

    Args:
        session: The caller's session.
        model: An entity model with an ``id`` column and a ``__natural_key__``.
        dataset_id: The dataset to read.

    Returns:
        Natural-key tuples, in ``__natural_key__`` order, mapped to ids; the same mapping
        :func:`upsert_entities` returns for the rows it wrote.

    Raises:
        NaturalKeyError: If ``model`` breaks the contract or has no ``id`` column.
    """
    info = _scoped_info(model)
    if not info.has_id:
        raise NaturalKeyError(f"{model.__name__} has no id column")
    key_columns = [info.table.c[name] for name in info.natural_key]
    result = await session.execute(
        select(info.table.c.id, *key_columns).where(info.table.c.dataset_id == dataset_id)
    )
    mapping: dict[tuple[Any, ...], int] = {}
    for returned in result:
        stored = tuple(returned)
        mapping[stored[1:]] = int(stored[0])
    return mapping


async def append_snapshots(
    rows: Sequence[Row], *, session: AsyncSession | None = None
) -> list[int]:
    """Append ``entity_snapshot`` observations and return their ids in input order.

    AAP 0.5.2 (``entity_snapshot``: "Append-only") and 0.5.4.1 (budget history). Every row
    is a plain ``INSERT`` with no ``ON CONFLICT``, so the same input twice yields two rows
    and an existing row is never updated. ``captured_at`` is the observation instant, never
    an effective time. Ids come back in input order, because discrepancy detection stores
    the ``snapshot_id`` it compared.

    The signature is AAP 0.5.1's ``append_snapshots(rows)``. An entity sync passes its
    ``session`` so the snapshots commit with its entities, baselines and discrepancies.

    Args:
        rows: Snapshot rows, each carrying its own ``dataset_id`` and a tz-aware
            ``captured_at``. ``source`` may be omitted; it is set from the dataset.
        session: The caller's session, joined without committing; the dataset guards are
            then held until the caller's transaction ends. ``None`` runs the write in a
            transaction of its own, committed on success and rolled back on error.

    Returns:
        The new snapshot ids, one per input row, in input order; ``[]`` for empty ``rows``.

    Raises:
        DatasetNotWritable: If a row's dataset is missing or retired; the guard runs once
            per distinct ``dataset_id``.
        ValueError: If a row carries an unknown column or an ``id``, lacks an integer
            ``dataset_id``, lacks a tz-aware ``captured_at``, names another source than its
            dataset's, or carries a ``float``.
    """
    if not rows:
        return []
    async with use_session(session) as unit:
        return await _append_snapshots(unit, rows)


async def _append_snapshots(session: AsyncSession, rows: Sequence[Row]) -> list[int]:
    """Validate and insert non-empty snapshot rows in ``session``; see :func:`append_snapshots`."""
    table = _table_of(EntitySnapshot)
    name = table.name
    columns = frozenset(table.c.keys())
    datasets: dict[int, SourceDataset] = {}
    prepared: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        values = dict(row)
        unknown = sorted(set(values) - columns)
        if unknown:
            raise ValueError(f"{name} row {position} carries unknown columns {unknown}")
        if "id" in values:
            raise ValueError(f"{name} row {position} carries an id; snapshots are append-only")
        dataset_id = values.get("dataset_id")
        if not isinstance(dataset_id, int) or isinstance(dataset_id, bool):
            raise ValueError(f"{name} row {position} lacks an integer dataset_id")
        dataset = datasets.get(dataset_id)
        if dataset is None:
            dataset = await ensure_writable(session, dataset_id)
            datasets[dataset_id] = dataset
        if "source" in values and values["source"] != dataset.source:
            raise ValueError(
                f"{name} row {position} carries source {values['source']!r}, but dataset "
                f"{dataset_id} belongs to {dataset.source!r}"
            )
        values["source"] = dataset.source
        captured_at = values.get("captured_at")
        if not isinstance(captured_at, datetime) or captured_at.utcoffset() is None:
            raise ValueError(f"{name} row {position} lacks a tz-aware captured_at")
        for column, value in values.items():
            _check_value(name, column, value)
        prepared.append(values)

    ids: dict[int, int] = {}
    for _, members in _group_by_columns(prepared):
        statement = insert(EntitySnapshot).returning(table.c.id, sort_by_parameter_order=True)
        result = await session.execute(statement, [values for _, values in members])
        new_ids = [int(new_id) for new_id in result.scalars()]
        for (position, _), new_id in zip(members, new_ids, strict=True):
            ids[position] = new_id
    _log.debug("db.append_snapshots", datasets=sorted(datasets), rows=len(prepared))
    return [ids[position] for position in range(len(prepared))]


async def enqueue_analysis_request(session: AsyncSession, reason: str) -> None:
    """Queue a default-window analysis run in the caller's transaction.

    AAP 0.3.2 ("Events that trigger a rerun"). This is the single helper for every event
    that makes the latest run stale:

    * a completed entity, metrics, segment, lead or CRM sync;
    * a dataset activation or a source-mode change;
    * a decision write;
    * a discrepancy being created or resolved;
    * a feedback write;
    * a changed threshold file detected at startup (``config_changed``).

    The row is pending (``picked_at`` and ``run_id`` ``NULL``) and is flushed, so it commits
    or rolls back with the change that caused it. Coalescing requests that arrive within
    60 s into one run is the worker's job; this function never coalesces.

    Args:
        session: The caller's session; nothing is committed here.
        reason: The triggering event, for example ``decision`` or ``entity_sync``; at most
            200 characters.

    Raises:
        ValueError: If ``reason`` is blank or longer than 200 characters.
    """
    if not reason.strip():
        raise ValueError("analysis_request reason must not be blank")
    if len(reason) > _MAX_REASON_LENGTH:
        raise ValueError(
            f"analysis_request reason is {len(reason)} characters; at most "
            f"{_MAX_REASON_LENGTH} are allowed"
        )
    session.add(AnalysisRequest(requested_at=utcnow(), reason=reason))
    await session.flush()
    _log.debug("db.analysis_request_enqueued", reason=reason)


@dataclass(frozen=True, slots=True)
class _RegistryAsset:
    """One validated registry entry, its URNs de-duplicated in listed order."""

    slug: str
    title: str
    lead_form_urns: tuple[str, ...]
    creative_urns: tuple[str, ...]


def _registry_text(value: object, what: str) -> str:
    """Return a non-blank registry string, or fail naming what it was.

    Args:
        value: The value read from the registry row.
        what: Description used in the error message.

    Returns:
        The string.

    Raises:
        ValueError: If ``value`` is not a non-blank string.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"content asset registry: {what} must be a non-blank string")
    return value


def _registry_urns(value: object, slug: str, field: str, owners: dict[str, str]) -> tuple[str, ...]:
    """Validate one asset's URN list and claim each URN for the asset.

    A URN listed twice under the same asset is kept once, which changes nothing the tables
    can hold; a URN listed under two assets is a configuration error (AAP 0.5.2).

    Args:
        value: The list read from the registry row.
        slug: The owning asset's slug.
        field: ``lead_form_urns`` or ``creative_urns``, for error messages.
        owners: URN to owning slug for every URN claimed so far; updated in place.

    Returns:
        The asset's distinct URNs in listed order.

    Raises:
        ValueError: If the list is not a sequence of non-blank strings, or a URN is
            already claimed by another asset.
    """
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"content asset {slug!r}: {field} must be a list of URNs")
    urns: list[str] = []
    for item in value:
        urn = _registry_text(item, f"a URN in {slug!r} {field}")
        owner = owners.get(urn)
        if owner is None:
            owners[urn] = slug
            urns.append(urn)
        elif owner != slug:
            raise ValueError(
                f"content asset registry: {urn!r} in {field} is listed under two assets, "
                f"{owner!r} and {slug!r}"
            )
    return tuple(urns)


def _validate_registry(rows: Sequence[ContentAssetRow]) -> list[_RegistryAsset]:
    """Validate the whole registry before anything is written.

    AAP 0.5.2: a form or creative listed under two assets fails config load.
    ``analysis/thresholds.py`` checks the same rules at load; this is defence in depth, so
    a bad registry fails with a named ``ValueError`` before a unique constraint would.

    Args:
        rows: The registry entries.

    Returns:
        The validated entries, in registry order.

    Raises:
        ValueError: On a repeated slug, a URN under two assets, or a malformed entry.
    """
    slugs: set[str] = set()
    form_owners: dict[str, str] = {}
    creative_owners: dict[str, str] = {}
    assets: list[_RegistryAsset] = []
    for row in rows:
        slug = _registry_text(row["slug"], "slug")
        if slug in slugs:
            raise ValueError(f"content asset registry: slug {slug!r} is listed twice")
        slugs.add(slug)
        title = _registry_text(row["title"], f"title of {slug!r}")
        forms = _registry_urns(row["lead_form_urns"], slug, "lead_form_urns", form_owners)
        creatives = _registry_urns(row["creative_urns"], slug, "creative_urns", creative_owners)
        assets.append(_RegistryAsset(slug, title, forms, creatives))
    return assets


async def sync_content_assets(session: AsyncSession, rows: Sequence[ContentAssetRow]) -> None:
    """Make the content-asset tables equal the white paper registry, in one transaction.

    AAP 0.5.2 (``content_asset``, ``content_asset_form``, ``content_asset_creative``) and
    0.5.4.7 (asset identification). The registry is configuration, not source data: these
    tables carry no ``dataset_id``, so no dataset guard applies. In the caller's
    transaction this function:

    1. validates the whole registry first (repeated slug, URN under two assets);
    2. deletes every ``content_asset_form`` and ``content_asset_creative`` row, so a URN
       can move between assets without tripping its unique constraint;
    3. deletes the assets whose slug is no longer listed;
    4. upserts the listed assets on ``slug``, updating ``title``, so a retained slug keeps
       its id;
    5. inserts the listed ``(asset_id, lead_form_urn)`` and ``(asset_id, creative_urn)``
       pairs.

    Args:
        session: The caller's session; nothing is committed here.
        rows: The registry entries from ``config/content_assets.yaml``. An empty registry
            empties the three tables.

    Raises:
        ValueError: Naming the offending slug or URN, before anything is written.
    """
    assets = _validate_registry(rows)
    await session.execute(delete(ContentAssetForm))
    await session.execute(delete(ContentAssetCreative))
    stale = delete(ContentAsset)
    if assets:
        stale = stale.where(ContentAsset.slug.not_in([asset.slug for asset in assets]))
    await session.execute(stale, execution_options={"synchronize_session": "fetch"})

    forms: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    if assets:
        statement = insert(ContentAsset)
        upsert = statement.on_conflict_do_update(
            index_elements=["slug"],
            set_={"title": statement.excluded.title},
        ).returning(ContentAsset.id, ContentAsset.slug)
        result = await session.execute(
            upsert, [{"slug": asset.slug, "title": asset.title} for asset in assets]
        )
        asset_ids = {slug: int(asset_id) for asset_id, slug in result}
        for asset in assets:
            asset_id = asset_ids[asset.slug]
            forms.extend(
                {"asset_id": asset_id, "lead_form_urn": urn} for urn in asset.lead_form_urns
            )
            overrides.extend(
                {"asset_id": asset_id, "creative_urn": urn} for urn in asset.creative_urns
            )
    if forms:
        await session.execute(insert(ContentAssetForm), forms)
    if overrides:
        await session.execute(insert(ContentAssetCreative), overrides)
    await session.flush()
    _log.info(
        "db.content_assets_synced",
        assets=len(assets),
        forms=len(forms),
        overrides=len(overrides),
    )
