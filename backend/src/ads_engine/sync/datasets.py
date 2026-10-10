"""Dataset lifecycle: building, companions, readiness, atomic activation, discard and purge.

AAP 0.3.3 ("Datasets and provenance", "Staged matching on a CRM dataset change",
"Recovery readiness") and 0.5.1 ("Source mode and datasets"). Every source-derived row
carries a ``dataset_id`` that references ``source_dataset``, and a source's single
``active`` primary dataset is entirely live or entirely simulated, so real and fictional
ids never join within one source. :class:`DatasetManager` owns every state change of a
``source_dataset`` row:

* :meth:`DatasetManager.begin` inserts a ``building`` dataset. A simulated LinkedIn dataset
  is accompanied by a ``companion``: a simulated HubSpot dataset whose parent is that
  LinkedIn dataset and which holds the generated contacts, meetings, deals, associations
  and ``lead_match`` rows its leads are attributed through.
* :meth:`DatasetManager.readiness` decides whether a backfilled dataset may activate: every
  required freshness row is recorded, its latest attempt succeeded, and it is ``complete``,
  or ``incomplete`` with a reason that is not unread work (an interrupted or call-budget
  deferred fetch: the backfill did not complete), and never ``unknown``; every staged
  ``lead_match`` row has been attempted at least once;
  every lead of a LinkedIn dataset, live or simulated, has a ``lead_match`` row in its
  effective CRM dataset (the companion of a simulated one), and a live one with leads has
  such a dataset; a simulated LinkedIn dataset's companion is ready too. Staging the rows
  is the job of ``sync/lead_match.py``; readiness only counts.
* :meth:`DatasetManager.unhydrated_matches` counts a CRM dataset's matches whose contact's
  meeting or deal associations are not read yet: the debt a build's own CRM dataset, or a
  live LinkedIn build's leads in HubSpot's active primary, must clear before the build
  activates. It is not part of readiness.

* :meth:`DatasetManager.activate` flips a ready dataset (and its companion) to ``active``
  and retires the source's previous active primary (and its companions) in one
  transaction, serialised on the source's ``source_connection`` row. The previous
  dataset's matching state retires with it, so active matching state is never reset.
* :meth:`DatasetManager.discard` deletes a failed build; the previous dataset stays active.
* :meth:`DatasetManager.purge_retired` deletes retired datasets older than
  :data:`RETENTION` with one ``DELETE FROM source_dataset WHERE id = :retired`` each. The
  ``ON DELETE CASCADE`` foreign keys of the schema remove every dependent row in a
  referentially safe order (a companion goes with its parent, ``lead_match`` rows with
  their lead or their CRM dataset), and run history survives through ``ON DELETE SET
  NULL``. No table order is written here.

It also persists the CRM bindings verified against a HubSpot portal on that portal's
datasets (``source_dataset.crm_bindings``), which attribution and ``GET /config`` read
without calling HubSpot.

Transactions: every public method opens and commits its own transaction through the
session factory unless it is given ``session=``, in which case it runs inside the caller's
transaction and never commits, so the caller can make the change atomic with its own
work. :meth:`DatasetManager.purge_retired` always uses one transaction per dataset.

Example:
    A recovery build, as ``sync/mode.py`` drives it::

        manager = DatasetManager()
        dataset_id = await manager.begin("hubspot", "live", reason="recovered")
        ...  # backfill, staged matching, freshness rows
        readiness = await manager.readiness(dataset_id)
        if readiness.ready:
            activation = await manager.activate(dataset_id, reason="recovered")
        else:
            await manager.discard(dataset_id)
"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, get_args

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.thresholds import CrmBindings
from ads_engine.db import repositories
from ads_engine.db.models.crm import (
    ASSOCIATION_FETCH_STATUSES,
    CONTACT_READ_STATUSES,
    CRM_OBJECT_TYPES,
    LEAD_MATCH_STATUSES,
    CrmAssociationFetch,
    CrmContact,
    Lead,
    LeadMatch,
)
from ads_engine.db.models.ops import (
    DATASET_ROLES,
    DATASET_STATES,
    MODES,
    DatasetFreshness,
    SourceConnection,
    SourceDataset,
)
from ads_engine.db.session import get_sessionmaker
from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Source
from ads_engine.sync.freshness import (
    SOURCE_DATASETS,
    is_repairable_reason,
    load_freshness,
    recorded_shortfall,
)

__all__ = [
    "RETENTION",
    "Activation",
    "DatasetManager",
    "DatasetNotFoundError",
    "DatasetStateError",
    "Mode",
    "Readiness",
    "Role",
]

_log = get_logger(__name__)

RETENTION: Final = timedelta(days=30)
"""How long a retired dataset is kept, read-only, before the nightly purge deletes it."""

Mode = Literal["live", "simulated"]
"""A dataset's mode; equals :data:`ads_engine.db.models.ops.MODES`."""

Role = Literal["primary", "companion"]
"""A dataset's role; equals :data:`ads_engine.db.models.ops.DATASET_ROLES`."""

# The lifecycle and matching vocabulary, spelled once. The import-time check below ties
# every value to the tuples the schema's CHECK constraints are built from, so a renamed
# state fails at import rather than as a constraint violation at run time.
_BUILDING: Final = "building"
_ACTIVE: Final = "active"
_RETIRED: Final = "retired"
_PRIMARY: Final = "primary"
_COMPANION: Final = "companion"
_PENDING: Final = "pending"
_MATCHED: Final = "matched"
_CONTACT_READ_OK: Final = "ok"
_CONNECTION_OK: Final = "ok"

# A matched contact's CRM evidence is read through its contact-to-meeting and contact-to-deal
# association fetches; an ``ok`` fetch whose targets are not read yet has no target-read
# status (``crm_association_fetch``, ``db/models/crm.py``).
_CONTACT: Final = "contact"
_HYDRATED_TARGETS: Final = ("meeting", "deal")
_FETCH_OK: Final = "ok"

# Discovery of Google-touch contacts runs only when both Google CRM properties are
# configured and verified, so only then does a HubSpot primary dataset get this row.
_GOOGLE_CONTACTS: Final = "hubspot.google_contacts"

# Bounds the wait for the source_connection row lock in an activation this module owns.
# Every other holder (a concurrent activation, the Google call budget) keeps it briefly; a
# longer wait means a stuck transaction, which becomes a database error instead of a hang.
_LOCK_TIMEOUT: Final = text("SET LOCAL lock_timeout = '30s'")


def _check_vocabulary() -> None:
    """Verify the values used here against the schema's enumerations.

    Raises:
        RuntimeError: If a state, role, mode, match status, contact-read status, CRM object
            type or association-fetch status is not the one ``db/models`` constrains its
            column to, or the Google-contacts dataset name is unknown.
    """
    if tuple(DATASET_STATES) != (_BUILDING, _ACTIVE, _RETIRED):
        raise RuntimeError(f"dataset states {DATASET_STATES!r} no longer match sync/datasets.py")
    if tuple(DATASET_ROLES) != (_PRIMARY, _COMPANION) or get_args(Role) != tuple(DATASET_ROLES):
        raise RuntimeError(f"dataset roles {DATASET_ROLES!r} no longer match sync/datasets.py")
    if get_args(Mode) != tuple(MODES):
        raise RuntimeError(f"modes {MODES!r} no longer match sync/datasets.py")
    for status in (_PENDING, _MATCHED):
        if status not in LEAD_MATCH_STATUSES:
            raise RuntimeError(f"lead_match statuses {LEAD_MATCH_STATUSES!r} lack {status!r}")
    if _CONTACT_READ_OK not in CONTACT_READ_STATUSES:
        raise RuntimeError(
            f"contact read statuses {CONTACT_READ_STATUSES!r} lack {_CONTACT_READ_OK!r}"
        )
    if not {_CONTACT, *_HYDRATED_TARGETS} <= set(CRM_OBJECT_TYPES):
        raise RuntimeError(
            f"CRM object types {CRM_OBJECT_TYPES!r} no longer match sync/datasets.py"
        )
    if _FETCH_OK not in ASSOCIATION_FETCH_STATUSES:
        raise RuntimeError(
            f"association fetch statuses {ASSOCIATION_FETCH_STATUSES!r} lack {_FETCH_OK!r}"
        )
    if _GOOGLE_CONTACTS not in SOURCE_DATASETS["hubspot"]:
        raise RuntimeError(f"{_GOOGLE_CONTACTS!r} is not a HubSpot freshness dataset")


_check_vocabulary()

# Freshness rows every dataset of a source must have recorded before it can activate. The
# names come from ops.DATASETS through SOURCE_DATASETS: Google 3, LinkedIn 4, HubSpot 3. A
# HubSpot primary adds hubspot.google_contacts when its Google link is verified; a
# companion never does, because Google-touch discovery reads only HubSpot's primary.
_REQUIRED_FRESHNESS: Final[Mapping[Source, tuple[str, ...]]] = MappingProxyType(
    {
        "google": SOURCE_DATASETS["google"],
        "linkedin": SOURCE_DATASETS["linkedin"],
        "hubspot": tuple(name for name in SOURCE_DATASETS["hubspot"] if name != _GOOGLE_CONTACTS),
    }
)


def _utcnow() -> datetime:
    """Return the current instant in UTC: the default clock."""
    return datetime.now(UTC)


def _as_source(value: str) -> Source:
    """Narrow a source name to :data:`~ads_engine.settings.Source`.

    Raises:
        ValueError: If ``value`` is not one of :data:`~ads_engine.settings.SOURCES`.
    """
    for source in SOURCES:
        if value == source:
            return source
    raise ValueError(f"unknown source {value!r}; expected one of {list(SOURCES)!r}")


def _as_mode(value: str) -> Mode:
    """Narrow a mode name to :data:`Mode`.

    Raises:
        ValueError: If ``value`` is not ``live`` or ``simulated``.
    """
    for mode in get_args(Mode):
        if value == mode:
            narrowed: Mode = mode
            return narrowed
    raise ValueError(f"unknown mode {value!r}; expected one of {list(MODES)!r}")


def _as_role(value: str) -> Role:
    """Narrow a role name to :data:`Role`.

    Raises:
        ValueError: If ``value`` is not ``primary`` or ``companion``.
    """
    for role in get_args(Role):
        if value == role:
            narrowed: Role = role
            return narrowed
    raise ValueError(f"unknown dataset role {value!r}; expected one of {list(DATASET_ROLES)!r}")


def _check_reason(reason: str | None) -> None:
    """Reject a blank reason; ``None`` means "no reason supplied".

    Raises:
        ValueError: If ``reason`` is an empty or whitespace-only string.
    """
    if reason is not None and not reason.strip():
        raise ValueError("a dataset reason must not be blank")


def _google_link_verified(document: Mapping[str, object] | None) -> bool:
    """Return whether stored CRM bindings show both Google CRM properties verified.

    Args:
        document: A dataset's ``crm_bindings`` value, or ``None`` before verification.

    Returns:
        ``True`` only when the bindings decode and their Google link is available.

    Raises:
        ads_engine.analysis.thresholds.ConfigError: If the stored document is malformed.
    """
    if document is None:
        return False
    return CrmBindings.from_json(document).google.available


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _unread_work(row: DatasetFreshness) -> str | None:
    """Return the repairable shortfall of an ``incomplete`` freshness row, if it has one.

    A repairable shortfall (:func:`~ads_engine.sync.freshness.is_repairable_reason`) is work
    a fetch left unread: a transient error outlived its retries part-way through it
    (``interrupted``), or the Google call budget deferred its remaining pages (``call budget
    exhausted``, a transient condition in AAP 0.3.3). The backfill that wrote the row did
    not complete. A permanent shortfall (a platform result limit that no refetch removes)
    is not unread work.

    Args:
        row: A stored freshness row.

    Returns:
        The shortfall as recorded, without a later failed attempt's note, or ``None`` when
        the row is not ``incomplete`` or its reason is not repairable.
    """
    if row.completeness != "incomplete" or not is_repairable_reason(row.reason):
        return None
    return recorded_shortfall(row) or row.reason


def _failed_latest_attempt(row: DatasetFreshness) -> str | None:
    """Return why a freshness row's latest attempt did not succeed, if it did not.

    The latest attempt did not succeed when no success is recorded at all, or when
    ``last_attempt_at`` is later than ``last_success_at``: a failed or cut-short attempt
    moves only ``last_attempt_at``, so the row's data is not what the latest read returned.

    Args:
        row: A stored freshness row.

    Returns:
        The row's reason (``"attempt failed"`` when it records none), prefixed with ``"no
        successful attempt"`` when no success is recorded; ``None`` when the latest attempt
        succeeded.
    """
    reason = row.reason.strip() if row.reason is not None else ""
    if row.last_success_at is None:
        return f"no successful attempt: {reason}" if reason else "no successful attempt"
    if row.last_attempt_at is not None and row.last_attempt_at > row.last_success_at:
        return reason or "attempt failed"
    return None


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a backfilled dataset may activate, and what still stands in the way.

    AAP 0.3.3 ("Readiness", "Recovery readiness"). A dataset is ready only when all of these
    hold:

    * every required freshness row (:meth:`DatasetManager.required_freshness`) exists and is
      ``complete``, or ``incomplete`` with a recorded reason; never ``unknown``;
    * every source, HubSpot primary and companion included: the backfill completed, so no
      required row is ``incomplete`` for a repairable reason (an ``interrupted`` or ``call
      budget exhausted`` fetch, whose unread work is still outstanding), and the latest
      attempt of every required row succeeded (``last_success_at`` is recorded and
      ``last_attempt_at`` is not later). A permanent shortfall (a platform result limit no
      refetch removes) whose latest attempt succeeded is accepted;
    * HubSpot, primary or companion: no ``lead_match`` row of this CRM dataset is still
      ``pending``, so every staged row was attempted at least once. Rows of leads whose
      LinkedIn dataset has retired are not counted: matching never attempts them;
    * LinkedIn: no ``lead_match`` row of this dataset's leads is ``pending`` against its
      effective CRM dataset (HubSpot's active primary for a live dataset, its companion for
      a simulated one), and a simulated dataset has a companion that is itself ready;
    * LinkedIn, live or simulated: every lead has a ``lead_match`` row in that effective
      CRM dataset, because the pending counts see only rows that exist. So a lead the build
      never staged is detected, as is a HubSpot primary replaced during a live build, and
      LinkedIn-attributed CRM evidence is complete from the first run on the dataset;
    * live LinkedIn with at least one lead: an effective CRM dataset exists (HubSpot has
      an active primary). A dataset with no lead has nothing to match: a live one carries
      no CRM requirement, and a simulated one needs only its ready companion;
    * Google: freshness only.

    Attributes:
        dataset_id: The dataset assessed.
        ready: Whether every condition above holds.
        pending_matches: ``lead_match`` rows not yet attempted: of this CRM dataset for
            HubSpot, of this dataset's leads against its effective CRM dataset for LinkedIn,
            always 0 for Google.
        missing_freshness: Required freshness datasets with no row yet.
        unknown_freshness: Required freshness datasets whose completeness is ``unknown``.
        incomplete_without_reason: Required freshness datasets ``incomplete`` with no reason.
        companion: The companion's own readiness, for a simulated LinkedIn dataset that has
            one; ``None`` otherwise.
        reasons: Human-readable shortfalls, for logs and ``source_dataset.reason``; empty
            exactly when ``ready``.
        unstaged_leads: Leads of a LinkedIn dataset, live or simulated, with no
            ``lead_match`` row in its effective CRM dataset (the companion of a simulated
            one), or all the leads of a live dataset that has none (HubSpot has no active
            primary); always 0 for the other datasets.
        unfinished_freshness: Required freshness datasets, ``complete`` or ``incomplete``
            with a reason, that the backfill did not complete: ``incomplete`` with unread
            work (reported as "backfill incomplete"), or with a latest attempt that did not
            succeed (reported as "latest attempt failed"). A dataset listed under
            ``missing_freshness``, ``unknown_freshness`` or ``incomplete_without_reason`` is
            not repeated here.
    """

    dataset_id: int
    ready: bool
    pending_matches: int
    missing_freshness: tuple[str, ...]
    unknown_freshness: tuple[str, ...]
    incomplete_without_reason: tuple[str, ...]
    companion: Readiness | None = None
    reasons: tuple[str, ...] = ()
    unstaged_leads: int = 0
    unfinished_freshness: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Activation:
    """What one :meth:`DatasetManager.activate` transaction changed.

    Attributes:
        source: The source whose active primary dataset changed.
        activated: The dataset activated, followed by its companion when it has one.
        retired: The previous active primary of the source and its companions, ascending;
            empty on a source's first activation.
        mode: The mode the source now reads (``live`` or ``simulated``).
    """

    source: Source
    activated: tuple[int, ...]
    retired: tuple[int, ...]
    mode: Mode


class DatasetStateError(RuntimeError):
    """An illegal dataset transition was requested.

    Raised for activating or discarding a dataset that is not ``building``, activating or
    discarding a companion directly (it changes state only with its parent), activating a
    dataset that is not ready, or beginning a companion for a parent that is not a building
    simulated LinkedIn dataset or already has one.

    Attributes:
        dataset_id: The dataset the transition targeted, when known.
        readiness: The failed readiness when activation was refused as not ready.
    """

    def __init__(
        self,
        message: str,
        *,
        dataset_id: int | None = None,
        readiness: Readiness | None = None,
    ) -> None:
        super().__init__(message)
        self.dataset_id = dataset_id
        self.readiness = readiness


class DatasetNotFoundError(DatasetStateError, LookupError):
    """The dataset a call names does not exist (never created, discarded or purged).

    A :class:`DatasetStateError`, because no transition applies to a missing dataset, and
    a :class:`LookupError`, so callers that only look a dataset up can catch it as one.
    """

    def __init__(self, dataset_id: int) -> None:
        super().__init__(f"source_dataset {dataset_id} does not exist", dataset_id=dataset_id)


class DatasetManager:
    """Creates, assesses, activates, discards and purges ``source_dataset`` rows.

    The manager holds no state of its own beyond its session factory and clock; every
    fact lives in the database, so the worker, the CLI and tests can each use their own
    instance and still serialise on the same rows.
    """

    __slots__ = ("_clock", "_sessionmaker")

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Configure the manager; no database work happens until the first call.

        Args:
            sessionmaker: The session factory for transactions this manager owns. When
                omitted, :func:`~ads_engine.db.session.get_sessionmaker` is looked up on
                every call, so a manager outlives an engine disposed and rebuilt in between.
            clock: Returns the current instant, timezone-aware; ``datetime.now(UTC)`` when
                omitted. It stamps ``created_at``, ``activated_at`` and ``retired_at`` and
                sets the purge cutoff.
        """
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = sessionmaker
        self._clock: Callable[[], datetime] = clock if clock is not None else _utcnow

    def __repr__(self) -> str:
        """Return the class name; every dataset fact lives in the database."""
        return f"{type(self).__name__}()"

    # -- infrastructure ----------------------------------------------------------------

    def _sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker if self._sessionmaker is not None else get_sessionmaker()

    def _now(self) -> datetime:
        """Read the clock once.

        Raises:
            ValueError: If the clock returns a naive datetime.
        """
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("the dataset clock must return a timezone-aware datetime")
        return now

    @asynccontextmanager
    async def _unit(self, session: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        """Yield the caller's session as is, or a new one inside its own transaction.

        A caller's session is neither committed nor rolled back here. An owned session is
        committed when the block exits normally and rolled back when it raises.
        """
        if session is not None:
            yield session
            return
        async with self._sessions()() as owned, owned.begin():
            yield owned

    @staticmethod
    async def _load(session: AsyncSession, dataset_id: int, *, lock: bool = False) -> SourceDataset:
        """Read one dataset as committed now, optionally locking its row.

        Raises:
            DatasetNotFoundError: If no such dataset exists.
        """
        statement = select(SourceDataset).where(SourceDataset.id == dataset_id)
        if lock:
            statement = statement.with_for_update()
        dataset = await session.scalar(statement.execution_options(populate_existing=True))
        if dataset is None:
            raise DatasetNotFoundError(dataset_id)
        return dataset

    @staticmethod
    def _require_linkedin(dataset: SourceDataset) -> None:
        """Reject a dataset that is not a LinkedIn primary.

        Raises:
            ValueError: If the dataset belongs to another source or is a companion.
        """
        if dataset.source != "linkedin" or dataset.role != _PRIMARY:
            raise ValueError(
                f"source_dataset {dataset.id} is a {dataset.source} {dataset.role} dataset, "
                "not a LinkedIn primary"
            )

    @staticmethod
    def _require_hubspot(dataset: SourceDataset) -> None:
        """Reject a dataset that is not a HubSpot dataset (primary or companion).

        Raises:
            ValueError: If the dataset belongs to another source.
        """
        if dataset.source != "hubspot":
            raise ValueError(
                f"CRM bindings belong to HubSpot datasets; source_dataset {dataset.id} is "
                f"{dataset.source}"
            )

    # -- creation and lookups ----------------------------------------------------------

    async def begin(
        self,
        source: str,
        mode: str,
        role: str = _PRIMARY,
        parent: int | None = None,
        *,
        reason: str | None = None,
        session: AsyncSession | None = None,
    ) -> int:
        """Insert a ``building`` dataset and return its id.

        A primary dataset has no parent. A companion is the simulated HubSpot dataset of a
        simulated LinkedIn dataset that is still ``building``: it is created after its
        parent, activates and retires with it, and a parent has at most one. The parent's
        row is locked while the companion is created, so two concurrent begins cannot both
        attach one.

        Args:
            source: ``google``, ``linkedin`` or ``hubspot``.
            mode: ``live`` or ``simulated``.
            role: ``primary`` (default) or ``companion``.
            parent: The LinkedIn parent of a companion; must be ``None`` for a primary.
            reason: Why the dataset exists, for example the access-denied code that caused
                a switch to simulated data, ``credentials not configured`` or
                ``recovered``. A companion without one records its parent instead.
            session: Join the caller's transaction instead of committing one here.

        Returns:
            The new dataset's id.

        Raises:
            ValueError: On an unknown source, mode or role, a blank reason, a primary with
                a parent, a companion that is not simulated HubSpot or has no parent, or a
                parent that is not a simulated LinkedIn primary.
            DatasetNotFoundError: If the parent does not exist.
            DatasetStateError: If the parent is not ``building`` or already has a companion.
        """
        checked_source = _as_source(source)
        checked_mode = _as_mode(mode)
        checked_role = _as_role(role)
        _check_reason(reason)
        if checked_role == _PRIMARY and parent is not None:
            raise ValueError("a primary dataset has no parent")
        if checked_role == _COMPANION:
            if checked_source != "hubspot" or checked_mode != "simulated":
                raise ValueError(
                    "a companion dataset is always simulated HubSpot, "
                    f"not {checked_mode} {checked_source}"
                )
            if parent is None:
                raise ValueError("a companion dataset needs its LinkedIn parent")
        now = self._now()

        async with self._unit(session) as unit:
            if parent is not None:
                await self._check_companion_parent(unit, parent)
                # ``reason`` is optional so a companion can be begun as
                # ``begin("hubspot", "simulated", role="companion", parent=id)``; its
                # provenance is then its parent, which is what the row records.
                if reason is None:
                    reason = f"companion CRM of simulated LinkedIn dataset {parent}"
            dataset = SourceDataset(
                source=checked_source,
                mode=checked_mode,
                state=_BUILDING,
                role=checked_role,
                parent_dataset_id=parent,
                reason=reason,
                created_at=now,
            )
            unit.add(dataset)
            await unit.flush()
            dataset_id = dataset.id

        _log.info(
            "sync.dataset.begin",
            source=checked_source,
            mode=checked_mode,
            role=checked_role,
            dataset_id=dataset_id,
            parent_dataset_id=parent,
            reason=reason,
        )
        return dataset_id

    async def _check_companion_parent(self, session: AsyncSession, parent: int) -> None:
        """Lock a companion's parent and check it may take one.

        Raises:
            DatasetNotFoundError: If the parent does not exist.
            ValueError: If the parent is not a simulated LinkedIn primary.
            DatasetStateError: If the parent is not ``building`` or already has a companion.
        """
        dataset = await self._load(session, parent, lock=True)
        self._require_linkedin(dataset)
        if dataset.mode != "simulated":
            raise ValueError(
                f"source_dataset {parent} is live; only a simulated LinkedIn dataset has a "
                "companion CRM dataset"
            )
        if dataset.state != _BUILDING:
            raise DatasetStateError(
                f"source_dataset {parent} is {dataset.state}; a companion is begun only "
                "while its parent is building",
                dataset_id=parent,
            )
        existing = await session.scalar(
            select(SourceDataset.id).where(
                SourceDataset.parent_dataset_id == parent,
                SourceDataset.role == _COMPANION,
            )
        )
        if existing is not None:
            raise DatasetStateError(
                f"source_dataset {parent} already has companion {existing}",
                dataset_id=parent,
            )

    async def get(self, dataset_id: int, *, session: AsyncSession | None = None) -> SourceDataset:
        """Return one dataset as committed now.

        Without ``session`` the row is detached before its transaction ends, so its
        attributes stay readable afterwards whatever the factory's ``expire_on_commit``.

        Args:
            dataset_id: The dataset to read.
            session: Read in the caller's session; the object stays attached to it.

        Returns:
            The dataset row.

        Raises:
            DatasetNotFoundError: If no such dataset exists.
        """
        async with self._unit(session) as unit:
            dataset = await self._load(unit, dataset_id)
            if session is None:
                unit.expunge(dataset)
        return dataset

    async def companion_of(
        self, linkedin_dataset_id: int, *, session: AsyncSession | None = None
    ) -> int | None:
        """Return the companion CRM dataset of a LinkedIn dataset.

        Args:
            linkedin_dataset_id: A LinkedIn primary dataset of any state.
            session: Read in the caller's session.

        Returns:
            The companion's id, or ``None`` when the dataset has none (a live dataset, or a
            simulated one whose companion is not begun yet).

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            ValueError: If it is not a LinkedIn primary.
        """
        async with self._unit(session) as unit:
            dataset = await self._load(unit, linkedin_dataset_id)
            self._require_linkedin(dataset)
            return await self._companion_id(unit, linkedin_dataset_id)

    @staticmethod
    async def _companion_id(session: AsyncSession, parent: int) -> int | None:
        """Return the id of the companion whose parent is ``parent``, if any."""
        return await session.scalar(
            select(SourceDataset.id).where(
                SourceDataset.parent_dataset_id == parent,
                SourceDataset.role == _COMPANION,
            )
        )

    async def effective_crm_dataset(
        self, linkedin_dataset_id: int, *, session: AsyncSession | None = None
    ) -> int | None:
        """Return the CRM dataset LinkedIn-attributed evidence reads for a LinkedIn dataset.

        Delegates to :func:`ads_engine.db.repositories.effective_crm_dataset_id`: the
        companion of a simulated LinkedIn dataset, else HubSpot's active primary.

        Args:
            linkedin_dataset_id: A LinkedIn dataset of any state.
            session: Read in the caller's session.

        Returns:
            The effective CRM dataset id, or ``None`` when there is none yet.

        Raises:
            ValueError: If the dataset does not exist or is not a LinkedIn dataset.
        """
        async with self._unit(session) as unit:
            return await repositories.effective_crm_dataset_id(unit, linkedin_dataset_id)

    # -- CRM bindings ------------------------------------------------------------------

    async def store_crm_bindings(
        self,
        dataset_id: int,
        bindings: CrmBindings,
        *,
        session: AsyncSession | None = None,
    ) -> None:
        """Persist the CRM bindings verified against a HubSpot dataset's portal.

        ``sync/mode.py`` verifies the bindings when a HubSpot dataset (primary or
        companion) is begun and again at worker startup for every active one, then stores
        them here. A live and a simulated portal resolve different stage ids, so the
        bindings belong to the dataset and retire with it. A newer verification replaces
        the stored document.

        Args:
            dataset_id: A HubSpot dataset that is ``building`` or ``active``.
            bindings: The verified bindings; stored as :meth:`CrmBindings.to_json`.
            session: Join the caller's transaction instead of committing one here.

        Raises:
            ads_engine.db.repositories.DatasetNotWritable: If the dataset does not exist or
                is retired (retired datasets are read-only).
            ValueError: If the dataset is not a HubSpot dataset.
        """
        async with self._unit(session) as unit:
            # Exclusive: this transaction updates the row it guards, and two binders that
            # each held the shared guard would deadlock upgrading it.
            dataset = await repositories.ensure_writable(unit, dataset_id, exclusive=True)
            self._require_hubspot(dataset)
            dataset.crm_bindings = bindings.to_json()
            await unit.flush()
        _log.info(
            "sync.dataset.crm_bindings",
            dataset_id=dataset_id,
            mql_available=bindings.mql.available,
            ql_available=bindings.ql.available,
            google_link_available=bindings.google.available,
            verified_at=bindings.verified_at,
        )

    async def load_crm_bindings(
        self, dataset_id: int, *, session: AsyncSession | None = None
    ) -> CrmBindings | None:
        """Return the CRM bindings stored on a HubSpot dataset.

        Args:
            dataset_id: A HubSpot dataset (primary or companion) of any state.
            session: Read in the caller's session.

        Returns:
            The decoded bindings, or ``None`` when none were verified for the dataset yet;
            callers then treat MQL, QL and the Google link as unavailable, never as 0.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            ValueError: If the dataset is not a HubSpot dataset.
            ads_engine.analysis.thresholds.ConfigError: If the stored document is malformed.
        """
        async with self._unit(session) as unit:
            dataset = await self._load(unit, dataset_id)
            self._require_hubspot(dataset)
            document = dataset.crm_bindings
        return None if document is None else CrmBindings.from_json(document)

    # -- readiness ---------------------------------------------------------------------

    async def required_freshness(
        self, dataset_id: int, *, session: AsyncSession | None = None
    ) -> tuple[str, ...]:
        """Return the freshness datasets a dataset must record before it can activate.

        * Google: ``google.entities``, ``google.metrics_daily``, ``google.search_terms``.
        * LinkedIn: ``linkedin.entities``, ``linkedin.analytics_daily``,
          ``linkedin.segments``, ``linkedin.leads``.
        * HubSpot primary: ``hubspot.contacts``, ``hubspot.associations``,
          ``hubspot.deal_search``, plus ``hubspot.google_contacts`` only when its stored
          CRM bindings show both Google properties verified; otherwise Google-touch
          discovery does not run and writes no row.
        * HubSpot companion: ``hubspot.contacts``, ``hubspot.associations``,
          ``hubspot.deal_search``, the rows the simulated generator writes.

        Args:
            dataset_id: The dataset to assess.
            session: Read in the caller's session.

        Returns:
            The dataset names, in :data:`ads_engine.db.models.ops.DATASETS` order.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            ads_engine.analysis.thresholds.ConfigError: If stored CRM bindings are malformed.
        """
        async with self._unit(session) as unit:
            return self._required(await self._load(unit, dataset_id))

    @staticmethod
    def _required(dataset: SourceDataset) -> tuple[str, ...]:
        """Return the required freshness names of a loaded dataset (see above)."""
        source = _as_source(dataset.source)
        names = _REQUIRED_FRESHNESS[source]
        if (
            source == "hubspot"
            and dataset.role == _PRIMARY
            and _google_link_verified(dataset.crm_bindings)
        ):
            return (*names, _GOOGLE_CONTACTS)
        return names

    async def readiness(self, dataset_id: int, *, session: AsyncSession | None = None) -> Readiness:
        """Assess whether a dataset may activate (see :class:`Readiness`).

        Readiness only counts; it stages nothing and changes no row. Staging the
        ``lead_match`` rows a build must attempt is ``sync/lead_match.py``'s job.

        A backfill cut short is never ready: a required freshness row ``incomplete`` with
        unread work (an interrupted or call-budget deferred fetch) is reported as "backfill
        incomplete: <dataset> (<shortfall>)", and one whose latest attempt did not succeed
        as "latest attempt failed: <dataset> (<reason>)"; ``sync/mode.py`` then discards the
        build and the previous dataset stays active (AAP 0.3.3, "Recovery readiness").

        Args:
            dataset_id: The dataset to assess, normally a ``building`` primary.
            session: Read in the caller's session.

        Returns:
            The assessment, including the companion's for a simulated LinkedIn dataset.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            ads_engine.analysis.thresholds.ConfigError: If stored CRM bindings are malformed.
        """
        async with self._unit(session) as unit:
            dataset = await self._load(unit, dataset_id)
            result = await self._readiness(unit, dataset)
            source = dataset.source
        _log.info(
            "sync.dataset.readiness",
            dataset_id=dataset_id,
            source=source,
            ready=result.ready,
            pending_matches=result.pending_matches,
            unstaged_leads=result.unstaged_leads,
            reasons=list(result.reasons),
        )
        return result

    async def _readiness(self, session: AsyncSession, dataset: SourceDataset) -> Readiness:
        """Assess a loaded dataset inside ``session``; see :class:`Readiness`.

        Each required freshness row falls short at most once, in this order: missing,
        ``unknown``, ``incomplete`` without a reason, then (for a ``complete`` row or a
        reasoned ``incomplete`` one) unread work before a failed latest attempt. The same
        rules apply to every source, and to a simulated LinkedIn dataset's companion through
        its own assessment.
        """
        rows = await load_freshness(session, dataset.id)
        missing: list[str] = []
        unknown: list[str] = []
        without_reason: list[str] = []
        unfinished: list[str] = []
        unread: list[str] = []
        failed: list[str] = []
        for name in self._required(dataset):
            row = rows.get(name)
            if row is None:
                missing.append(name)
                continue
            if row.completeness == "incomplete":
                if row.reason is None or not row.reason.strip():
                    without_reason.append(name)
                    continue
            elif row.completeness != "complete":
                # "unknown", and defensively any value outside the two a success writes.
                unknown.append(name)
                continue
            # The backfill must have completed (AAP 0.3.3 "Recovery readiness"): unread work
            # still outstanding, or a latest attempt that did not succeed, holds the build.
            shortfall = _unread_work(row)
            if shortfall is not None:
                unfinished.append(name)
                unread.append(f"{name} ({shortfall})")
                continue
            failure = _failed_latest_attempt(row)
            if failure is not None:
                unfinished.append(name)
                failed.append(f"{name} ({failure})")

        reasons: list[str] = []
        if missing:
            reasons.append(f"freshness not yet recorded: {', '.join(missing)}")
        if unknown:
            reasons.append(f"freshness unknown: {', '.join(unknown)}")
        if without_reason:
            reasons.append(f"incomplete with no recorded reason: {', '.join(without_reason)}")
        if unread:
            reasons.append(f"backfill incomplete: {', '.join(unread)}")
        if failed:
            reasons.append(f"latest attempt failed: {', '.join(failed)}")

        pending = 0
        unstaged = 0
        companion: Readiness | None = None
        if dataset.source == "hubspot":
            pending = await self._pending_in_crm(session, dataset.id)
            if pending:
                reasons.append(
                    f"{_plural(pending, 'lead match', 'lead matches')} not yet attempted "
                    f"in CRM dataset {dataset.id}"
                )
        elif dataset.source == "linkedin":
            crm_dataset_id = await repositories.effective_crm_dataset_id(session, dataset.id)
            if dataset.mode == "simulated":
                # Simulated leads are attributed only through the companion, so its own
                # readiness (freshness and pending matches) is part of this dataset's.
                if crm_dataset_id is None:
                    reasons.append("companion CRM dataset not begun")
                else:
                    companion = await self._readiness(
                        session, await self._load(session, crm_dataset_id)
                    )
                    if not companion.ready:
                        reasons.append(
                            f"companion CRM dataset {crm_dataset_id} not ready: "
                            + "; ".join(companion.reasons)
                        )
            if crm_dataset_id is not None:
                pending = await self._pending_for_leads(session, dataset.id, crm_dataset_id)
                # A companion's pending rows are already among its own reasons.
                if pending and dataset.mode == "live":
                    reasons.append(
                        f"{_plural(pending, 'lead match', 'lead matches')} of this dataset's "
                        f"leads not yet attempted in CRM dataset {crm_dataset_id}"
                    )
                # Live or simulated, every lead needs a row in the CRM dataset in effect now:
                # the pending counts see only rows that exist, so a lead never staged there
                # would pass them. Rows staged against an earlier HubSpot primary do not
                # count either: a replaced CRM dataset leaves the leads unstaged in this one.
                unstaged = await self._unstaged_leads(session, dataset.id, crm_dataset_id)
                if unstaged:
                    reasons.append(
                        f"{_plural(unstaged, 'lead', 'leads')} of this dataset not yet "
                        f"staged in CRM dataset {crm_dataset_id}"
                    )
            elif dataset.mode == "live":
                # HubSpot has no active primary, so the leads cannot have been matched
                # anywhere. A dataset with no lead has nothing to match and is not held.
                unstaged = await self._lead_count(session, dataset.id)
                if unstaged:
                    reasons.append(
                        "no effective CRM dataset: HubSpot has no active primary dataset to "
                        f"match {_plural(unstaged, 'lead', 'leads')} against"
                    )

        return Readiness(
            dataset_id=dataset.id,
            ready=not reasons,
            pending_matches=pending,
            missing_freshness=tuple(missing),
            unknown_freshness=tuple(unknown),
            incomplete_without_reason=tuple(without_reason),
            companion=companion,
            reasons=tuple(reasons),
            unstaged_leads=unstaged,
            unfinished_freshness=tuple(unfinished),
        )

    @staticmethod
    async def _pending_in_crm(session: AsyncSession, crm_dataset_id: int) -> int:
        """Count the never-attempted ``lead_match`` rows staged in one CRM dataset.

        Rows of leads whose LinkedIn dataset has retired are not counted: matching never
        attempts them (no evidence reads them again), so they would hold the CRM dataset
        forever once LinkedIn activated a newer dataset.
        """
        count = await session.scalar(
            select(func.count())
            .select_from(LeadMatch)
            .join(Lead, Lead.id == LeadMatch.lead_id)
            .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
            .where(
                LeadMatch.crm_dataset_id == crm_dataset_id,
                LeadMatch.status == _PENDING,
                SourceDataset.state != _RETIRED,
            )
        )
        return int(count or 0)

    @staticmethod
    async def _pending_for_leads(
        session: AsyncSession, lead_dataset_id: int, crm_dataset_id: int
    ) -> int:
        """Count one LinkedIn dataset's leads whose match in a CRM dataset is still pending."""
        count = await session.scalar(
            select(func.count())
            .select_from(LeadMatch)
            .join(Lead, Lead.id == LeadMatch.lead_id)
            .where(
                Lead.dataset_id == lead_dataset_id,
                LeadMatch.crm_dataset_id == crm_dataset_id,
                LeadMatch.status == _PENDING,
            )
        )
        return int(count or 0)

    @staticmethod
    async def _lead_count(session: AsyncSession, lead_dataset_id: int) -> int:
        """Count the leads of one LinkedIn dataset."""
        count = await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.dataset_id == lead_dataset_id)
        )
        return int(count or 0)

    @staticmethod
    async def _unstaged_leads(
        session: AsyncSession, lead_dataset_id: int, crm_dataset_id: int
    ) -> int:
        """Count one LinkedIn dataset's leads that have no ``lead_match`` row in a CRM dataset."""
        staged = select(LeadMatch.lead_id).where(
            LeadMatch.lead_id == Lead.id, LeadMatch.crm_dataset_id == crm_dataset_id
        )
        count = await session.scalar(
            select(func.count())
            .select_from(Lead)
            .where(Lead.dataset_id == lead_dataset_id, ~staged.exists())
        )
        return int(count or 0)

    async def unhydrated_matches(
        self,
        crm_dataset_id: int,
        *,
        lead_dataset_id: int | None = None,
        session: AsyncSession | None = None,
    ) -> int:
        """Count a CRM dataset's matches whose contact's associations are not read yet.

        A ``lead_match`` row of the dataset counts when it is ``matched`` with its contact
        read ``ok``, its lead's LinkedIn dataset is not retired, and its contact
        (``crm_contact.hs_id`` in the same dataset) lacks its contact-to-meeting or its
        contact-to-deal ``crm_association_fetch`` row, or has one that is ``ok`` with no
        target-read status (associations read, their targets not). Every other recorded
        fetch outcome was attempted and does not count; whether it is known evidence is
        attribution's concern.

        Matching stores a contact and its outcome but reads none of its associations, so a
        row matched after the dataset's last CRM sync, by a build or by any other matcher,
        leaves this debt behind. ``sync/mode.py`` clears it before a build activates: it
        reruns the backfill of a build's own CRM dataset until the dataset's count is 0,
        and runs the CRM sync of HubSpot's active primary until a live LinkedIn build's own
        leads (``lead_dataset_id``) count 0 there. The count is not part of
        :meth:`readiness`, which counts freshness, unattempted rows and unstaged leads, not
        association reads.

        Args:
            crm_dataset_id: A HubSpot dataset, primary or companion.
            lead_dataset_id: Count only the rows of this LinkedIn dataset's leads; ``None``
                counts every lead's.
            session: Read in the caller's session.

        Returns:
            The number of such rows; 0 for a dataset that holds none.
        """
        hydrated = [
            select(CrmAssociationFetch.id)
            .where(
                CrmAssociationFetch.dataset_id == crm_dataset_id,
                CrmAssociationFetch.object_type == _CONTACT,
                CrmAssociationFetch.object_id == CrmContact.hs_id,
                CrmAssociationFetch.to_type == to_type,
                ~and_(
                    CrmAssociationFetch.status == _FETCH_OK,
                    CrmAssociationFetch.target_read_status.is_(None),
                ),
            )
            .exists()
            for to_type in _HYDRATED_TARGETS
        ]
        statement = (
            select(func.count())
            .select_from(LeadMatch)
            .join(
                CrmContact,
                and_(
                    CrmContact.id == LeadMatch.contact_id,
                    CrmContact.dataset_id == LeadMatch.crm_dataset_id,
                ),
            )
            .join(Lead, Lead.id == LeadMatch.lead_id)
            .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
            .where(
                LeadMatch.crm_dataset_id == crm_dataset_id,
                LeadMatch.status == _MATCHED,
                LeadMatch.contact_read_status == _CONTACT_READ_OK,
                SourceDataset.state != _RETIRED,
                ~and_(*hydrated),
            )
        )
        if lead_dataset_id is not None:
            statement = statement.where(Lead.dataset_id == lead_dataset_id)
        async with self._unit(session) as unit:
            count = await unit.scalar(statement)
        return int(count or 0)

    # -- transitions -------------------------------------------------------------------

    async def activate(
        self,
        dataset_id: int,
        *,
        reason: str | None = None,
        session: AsyncSession | None = None,
    ) -> Activation:
        """Activate a ready building primary and retire the source's previous one, atomically.

        In one transaction:

        1. Ensure the source's ``source_connection`` row exists and lock it ``FOR UPDATE``,
           so concurrent activations of one source (worker and CLI) run one after another.
        2. Lock and re-read the dataset and its companion; the dataset must be a
           ``building`` primary and ready, assessed inside this transaction.
        3. Retire the previous active primary of the source and its companions first, so
           ``uq_source_dataset_active_primary`` never sees two active primaries. Their
           ``lead_match`` rows retire with them; no row is reset or requeued.
        4. Activate the dataset and, for a simulated LinkedIn dataset, its companion.
        5. Point ``source_connection`` at the dataset: its mode, ``status='ok'`` and the
           mode reason.
        6. Queue a default-window analysis rerun (``dataset_activated:<source>``).

        Args:
            dataset_id: The building primary dataset to activate.
            reason: The mode reason ``sync/mode.py`` records on ``source_connection``;
                defaults to the dataset's own ``reason``.
            session: Join the caller's transaction instead of committing one here. The
                locks are then held until the caller commits, and the lock-wait bound this
                method sets on its own transactions is not applied to the caller's.

        Returns:
            What was activated and retired.

        Raises:
            ValueError: If ``reason`` is blank or the clock is naive.
            DatasetNotFoundError: If the dataset does not exist.
            DatasetStateError: If the dataset is a companion, is not ``building``, has a
                companion that is not ``building``, or is not ready (``readiness`` set).
        """
        _check_reason(reason)
        async with self._unit(session) as unit:
            if session is None:
                await unit.execute(_LOCK_TIMEOUT)
            activation = await self._activate(unit, dataset_id, reason)
        _log.info(
            "sync.dataset.activate",
            source=activation.source,
            mode=activation.mode,
            activated=list(activation.activated),
            retired=list(activation.retired),
        )
        return activation

    async def _activate(
        self, session: AsyncSession, dataset_id: int, reason: str | None
    ) -> Activation:
        """Run the activation steps of :meth:`activate` inside ``session``."""
        peek = (
            await session.execute(
                select(SourceDataset.source, SourceDataset.mode, SourceDataset.reason).where(
                    SourceDataset.id == dataset_id
                )
            )
        ).one_or_none()
        if peek is None:
            raise DatasetNotFoundError(dataset_id)
        source = _as_source(peek.source)

        # FOR UPDATE locks nothing when the row is missing, so create it first. The values
        # are overwritten below, before this transaction can commit.
        await session.execute(
            insert(SourceConnection)
            .values(
                source=source,
                mode=_as_mode(peek.mode),
                status=_CONNECTION_OK,
                reason=reason if reason is not None else peek.reason,
                calls_24h=0,
                calls_by_minute={},
            )
            .on_conflict_do_nothing(index_elements=["source"])
        )
        connection = (
            await session.scalars(
                select(SourceConnection)
                .where(SourceConnection.source == source)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).one()

        # Re-read under the lock: another activation or a discard may have run meanwhile.
        dataset = await self._load(session, dataset_id, lock=True)
        if dataset.role != _PRIMARY:
            raise DatasetStateError(
                f"source_dataset {dataset_id} is a companion; it activates only with its "
                f"parent {dataset.parent_dataset_id}",
                dataset_id=dataset_id,
            )
        if dataset.state != _BUILDING:
            raise DatasetStateError(
                f"source_dataset {dataset_id} is {dataset.state}; only a building dataset "
                "can be activated",
                dataset_id=dataset_id,
            )
        companions = list(
            (
                await session.scalars(
                    select(SourceDataset)
                    .where(
                        SourceDataset.parent_dataset_id == dataset_id,
                        SourceDataset.role == _COMPANION,
                    )
                    .order_by(SourceDataset.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        for companion in companions:
            if companion.state != _BUILDING:
                raise DatasetStateError(
                    f"companion {companion.id} of source_dataset {dataset_id} is "
                    f"{companion.state}; a companion shares its parent's state",
                    dataset_id=dataset_id,
                )

        readiness = await self._readiness(session, dataset)
        if not readiness.ready:
            _log.warning(
                "sync.dataset.activate_refused",
                source=source,
                dataset_id=dataset_id,
                reasons=list(readiness.reasons),
            )
            raise DatasetStateError(
                f"source_dataset {dataset_id} is not ready: {'; '.join(readiness.reasons)}",
                dataset_id=dataset_id,
                readiness=readiness,
            )

        now = self._now()
        previous = list(
            await session.scalars(
                select(SourceDataset.id)
                .where(
                    SourceDataset.source == source,
                    SourceDataset.role == _PRIMARY,
                    SourceDataset.state == _ACTIVE,
                    SourceDataset.id != dataset_id,
                )
                .with_for_update()
            )
        )
        retired: list[int] = []
        if previous:
            # Retire before activating: the partial unique index on the active primary is
            # checked per statement, so the old primary must leave "active" first. This
            # statement runs before any attribute below is set, so autoflush cannot
            # reorder the two.
            result = await session.execute(
                update(SourceDataset)
                .where(
                    or_(
                        SourceDataset.id.in_(previous),
                        and_(
                            SourceDataset.parent_dataset_id.in_(previous),
                            SourceDataset.role == _COMPANION,
                            SourceDataset.state != _RETIRED,
                        ),
                    )
                )
                .values(state=_RETIRED, retired_at=now)
                .returning(SourceDataset.id)
                .execution_options(synchronize_session="fetch")
            )
            retired = sorted(result.scalars())

        dataset.state = _ACTIVE
        dataset.activated_at = now
        for companion in companions:
            companion.state = _ACTIVE
            companion.activated_at = now
        connection.mode = dataset.mode
        connection.status = _CONNECTION_OK
        connection.reason = reason if reason is not None else dataset.reason
        connection.active_dataset_id = dataset.id
        await session.flush()
        await repositories.enqueue_analysis_request(session, reason=f"dataset_activated:{source}")

        return Activation(
            source=source,
            activated=(dataset.id, *(companion.id for companion in companions)),
            retired=tuple(retired),
            mode=_as_mode(dataset.mode),
        )

    async def discard(self, dataset_id: int, *, session: AsyncSession | None = None) -> None:
        """Delete a failed build; the source's active dataset stays active.

        One ``DELETE FROM source_dataset WHERE id = :id``: the companion (through
        ``parent_dataset_id``) and every row of the build cascade with it, including the
        ``lead_match`` rows staged against it and every row of its own leads. Objects of
        the discarded build loaded in the caller's session are stale afterwards.

        Args:
            dataset_id: A ``building`` primary dataset.
            session: Join the caller's transaction instead of committing one here.

        Raises:
            DatasetNotFoundError: If the dataset does not exist.
            DatasetStateError: If the dataset is a companion (it is discarded with its
                parent) or is not ``building``; active and retired datasets are never
                deleted here.
        """
        async with self._unit(session) as unit:
            dataset = await self._load(unit, dataset_id, lock=True)
            if dataset.role != _PRIMARY:
                raise DatasetStateError(
                    f"source_dataset {dataset_id} is a companion; it is discarded with its "
                    f"parent {dataset.parent_dataset_id}",
                    dataset_id=dataset_id,
                )
            if dataset.state != _BUILDING:
                raise DatasetStateError(
                    f"source_dataset {dataset_id} is {dataset.state}; only a building "
                    "dataset can be discarded",
                    dataset_id=dataset_id,
                )
            source, mode = dataset.source, dataset.mode
            companion_id = await self._companion_id(unit, dataset_id)
            await unit.execute(delete(SourceDataset).where(SourceDataset.id == dataset_id))
        _log.info(
            "sync.dataset.discard",
            source=source,
            mode=mode,
            dataset_id=dataset_id,
            companion_dataset_id=companion_id,
        )

    async def purge_retired(self, older_than: timedelta = RETENTION) -> list[int]:
        """Delete retired primary datasets retired longer than ``older_than`` ago.

        AAP 0.3.3 ("Retirement and purge"): each dataset is purged by exactly one
        ``DELETE FROM source_dataset WHERE id = :retired`` in a transaction of its own, and
        the schema's foreign-key actions remove everything that depends on it:

        * every row of the dataset, through its ``dataset_id`` ``ON DELETE CASCADE``;
        * its companion, through ``parent_dataset_id``, together with the companion's rows;
        * for a HubSpot dataset, only its own ``lead_match`` rows (``crm_dataset_id``), so
          active leads keep the rows staged for the active CRM dataset;
        * for a LinkedIn dataset, its leads and every ``lead_match`` row referencing them.

        Run history keeps rendering, because run tables reference entities through
        ``ON DELETE SET NULL`` and keep their evidence in ``jsonb``. The statement repeats
        the retired-and-old conditions, so a dataset changed or purged by another process
        meanwhile is skipped rather than deleted. Active and building datasets are never
        touched.

        Args:
            older_than: The retention; :data:`RETENTION` (30 days) by default, ``0`` purges
                every retired dataset.

        Returns:
            The ids purged by this call, ascending.

        Raises:
            ValueError: If ``older_than`` is negative or the clock is naive.
        """
        if older_than < timedelta(0):
            raise ValueError(f"older_than must not be negative, got {older_than}")
        cutoff = self._now() - older_than
        sessions = self._sessions()
        is_purgeable = and_(
            SourceDataset.role == _PRIMARY,
            SourceDataset.state == _RETIRED,
            SourceDataset.retired_at < cutoff,
        )

        async with sessions() as session, session.begin():
            candidates = (
                await session.execute(
                    select(SourceDataset.id, SourceDataset.source, SourceDataset.mode)
                    .where(is_purgeable)
                    .order_by(SourceDataset.id)
                )
            ).all()

        purged: list[int] = []
        for candidate in candidates:
            async with sessions() as session, session.begin():
                deleted = await session.scalar(
                    delete(SourceDataset)
                    .where(SourceDataset.id == candidate.id, is_purgeable)
                    .returning(SourceDataset.id)
                    .execution_options(synchronize_session=False)
                )
            if deleted is None:
                _log.info("sync.dataset.purge_skipped", dataset_id=candidate.id)
                continue
            purged.append(deleted)
            _log.info(
                "sync.dataset.purge",
                source=candidate.source,
                mode=candidate.mode,
                dataset_id=deleted,
                retired_before=cutoff,
            )
        return purged
