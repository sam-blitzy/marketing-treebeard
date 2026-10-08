"""Operational tables and the source and dataset constants shared by every model.

This module holds the tables that describe *where data came from and how fresh it is*,
rather than the data itself:

* ``source_dataset``: one row per dataset of a source. Every source-derived row in the
  schema carries a ``dataset_id`` that references it with ``ON DELETE CASCADE``, so a
  source's active dataset is entirely live or entirely simulated and a retired dataset is
  purged with one ``DELETE FROM source_dataset WHERE id = :retired``.
* ``dataset_freshness``: last attempt, last success, coverage and completeness per
  dataset name, from which each recommendation is flagged ``stale`` or ``incomplete``.
* ``app_user``: the single admin login.
* ``source_connection``: one row per source with its current mode, health, active dataset
  and the Google webhook call budget.
* ``sync_run``: history of every sync job.
* ``sync_request``: the "Sync now" queue, with at most one pending request per source.

It also exports the enumerations those tables (and the other model modules) constrain
their ``text`` columns to. Enumerations are ``text`` plus a ``CHECK`` built with
:func:`ads_engine.db.base.check_in`, never PostgreSQL ``ENUM`` types, and the tuples below
fix the value order the ``CHECK`` text renders. ``SOURCES`` is the allowed set of every
model's ``source`` column; ``ads_engine.settings.SOURCES`` must hold the same values.

``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, server default, constraint and index name declared here; ``alembic check``
must report no drift.

The module imports only the standard library, SQLAlchemy and ``ads_engine.db.base``, so the
other model modules can import its constants without an import cycle.
"""

from datetime import datetime
from typing import Any, ClassVar, Final

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, DatasetScopedMixin, IdPkMixin, NativeDate, check_in

__all__ = [
    "COMPLETENESS",
    "CONNECTION_STATUSES",
    "DATASETS",
    "DATASET_ROLES",
    "DATASET_STATES",
    "MODES",
    "SOURCES",
    "SYNC_RUN_STATUSES",
    "AppUser",
    "DatasetFreshness",
    "SourceConnection",
    "SourceDataset",
    "SyncRequest",
    "SyncRun",
]

SOURCES: Final[tuple[str, ...]] = ("google", "linkedin", "hubspot")
"""The external data sources, in display order: the allowed values of every ``source``."""

MODES: Final[tuple[str, ...]] = ("live", "simulated")
"""Whether a dataset holds real platform data or seeded mock data in the live wire shape."""

DATASET_STATES: Final[tuple[str, ...]] = ("building", "active", "retired")
"""Dataset lifecycle: backfilled while ``building``, read while ``active``, purged later."""

DATASET_ROLES: Final[tuple[str, ...]] = ("primary", "companion")
"""``primary`` is a source's own dataset; ``companion`` is the simulated CRM of a LinkedIn one."""

DATASETS: Final[tuple[str, ...]] = (
    "google.entities",
    "google.metrics_daily",
    "google.search_terms",
    "linkedin.entities",
    "linkedin.analytics_daily",
    "linkedin.segments",
    "linkedin.leads",
    "hubspot.contacts",
    "hubspot.associations",
    "hubspot.deal_search",
    "hubspot.google_contacts",
)
"""The freshness-tracked dataset names; each rule declares which of them it reads."""

COMPLETENESS: Final[tuple[str, ...]] = ("complete", "incomplete", "unknown")
"""Completeness of a dataset over its covered range (``unknown`` before the first read)."""

CONNECTION_STATUSES: Final[tuple[str, ...]] = ("ok", "stale", "denied", "error")
"""Health of a source connection as shown on ``/sources``."""

SYNC_RUN_STATUSES: Final[tuple[str, ...]] = ("running", "succeeded", "failed")
"""Outcome of one sync job run."""

# The CHECK text the migration repeats verbatim. A primary dataset has no parent; a companion
# is always a simulated HubSpot dataset hanging off its (LinkedIn) parent.
_ROLE_SHAPE_SQL: Final[str] = (
    "(role = 'primary' AND parent_dataset_id IS NULL) OR "
    "(role = 'companion' AND source = 'hubspot' AND mode = 'simulated' "
    "AND parent_dataset_id IS NOT NULL)"
)


class SourceDataset(IdPkMixin, Base):
    """One dataset of a source: the provenance every source-derived row points at.

    AAP 0.5.2 (``source_dataset``) and 0.3.3 (datasets and provenance). A dataset is entirely
    ``live`` or entirely ``simulated``, so real and fictional ids never join within one
    source. The worker creates a dataset as ``building``, backfills it, and then one
    transaction activates it and retires the previous one. At most one ``active`` primary
    dataset exists per source, enforced by the partial unique index
    ``uq_source_dataset_active_primary``.

    A ``companion`` is the simulated HubSpot dataset built alongside a simulated LinkedIn
    dataset (``parent_dataset_id``). It shares its parent's state: ``DatasetManager.activate``
    flips parent and companion in the same transaction, and ``parent_dataset_id`` is
    ``ON DELETE CASCADE``, so the companion is purged with its parent. The ``role_shape``
    check keeps the role, source, mode and parent columns consistent.

    Retired datasets are read-only and purged by deleting their row; every ``dataset_id``
    reference cascades. This table is not itself dataset-scoped and is never upserted.

    Attributes:
        source: The source the dataset belongs to (:data:`SOURCES`).
        mode: ``live`` or ``simulated`` (:data:`MODES`).
        state: ``building``, ``active`` or ``retired`` (:data:`DATASET_STATES`).
        role: ``primary`` or ``companion`` (:data:`DATASET_ROLES`).
        parent_dataset_id: The LinkedIn parent of a companion; ``None`` for a primary.
        reason: Why the dataset exists, for example the access-denied code that caused a
            switch to simulated data.
        created_at: When the dataset row was created (server default ``now()``).
        activated_at: When the dataset became active, if it has.
        retired_at: When the dataset was retired, if it has been.
        crm_bindings: For HubSpot datasets only, the CRM bindings verified against that
            portal, written by ``sync/datasets.py``: the resolved MQL and QL property names
            and stage ids, their availability with the reason when unavailable, the Google
            campaign and touch-date property verification, and ``verified_at``. Attribution
            and ``GET /config`` read it. ``None`` for every other dataset.
    """

    __tablename__ = "source_dataset"

    source: Mapped[str]
    mode: Mapped[str]
    state: Mapped[str]
    role: Mapped[str]
    parent_dataset_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("source_dataset.id", ondelete="CASCADE"),
        index=True,
    )
    reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=sa.text("now()"))
    activated_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]
    # The CRM stage bindings resolved at startup are a property of the portal a HubSpot
    # dataset was read from, so they are stored per dataset: a live and a simulated portal
    # resolve different stage ids, and the bindings retire with their dataset.
    crm_bindings: Mapped[dict[str, Any] | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("mode", MODES),
        check_in("state", DATASET_STATES),
        check_in("role", DATASET_ROLES),
        sa.CheckConstraint(_ROLE_SHAPE_SQL, name="role_shape"),
        sa.Index(
            "uq_source_dataset_active_primary",
            "source",
            unique=True,
            postgresql_where=sa.text("state = 'active' AND role = 'primary'"),
        ),
    )


class DatasetFreshness(IdPkMixin, DatasetScopedMixin, Base):
    """Freshness, coverage and completeness of one named dataset within one source dataset.

    AAP 0.5.2 (``dataset_freshness``) and 0.3.2 ("Freshness and coverage per dataset"). There
    is one row per (``dataset_id``, ``dataset``), where ``dataset`` is one of the eleven names
    in :data:`DATASETS`. Each rule declares the datasets it reads, and an item is ``stale``
    when any of them last succeeded too long ago or covers less than the window end, and
    ``incomplete`` when any of them is not ``complete`` over the window. A successful sync of
    one dataset therefore never hides a failed refresh of another.

    :func:`ads_engine.sync.freshness.record_freshness` writes these rows directly, with no
    repository helper, because its merge needs the locked row: it seeds the row with
    ``INSERT ... ON CONFLICT DO NOTHING``, locks it with ``SELECT ... FOR UPDATE``, merges the
    attempt's outcome and upserts the result on :attr:`__natural_key__` with
    ``INSERT ... ON CONFLICT DO UPDATE``. It flushes but never commits, so the row lands in
    the caller's transaction, atomically with the data it describes. A row may be created as
    ``unknown`` before its first attempt, so every timestamp and coverage date is nullable.

    Attributes:
        dataset: The dataset name (:data:`DATASETS`).
        last_attempt_at: When a job last tried to refresh the dataset.
        last_success_at: When a job last refreshed it successfully.
        covered_from: First native date the stored data covers.
        covered_through: Last native date the stored data covers.
        completeness: ``complete``, ``incomplete`` or ``unknown`` (:data:`COMPLETENESS`).
        reason: Why the dataset is not complete, for example "result limit reached at
            finest partition" or a call-budget deferral.
    """

    __tablename__ = "dataset_freshness"
    __natural_key__: ClassVar[tuple[str, ...]] = ("dataset_id", "dataset")

    dataset: Mapped[str]
    last_attempt_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    covered_from: Mapped[NativeDate | None]
    covered_through: Mapped[NativeDate | None]
    completeness: Mapped[str]
    reason: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("dataset", DATASETS),
        check_in("completeness", COMPLETENESS),
        sa.UniqueConstraint("dataset_id", "dataset"),
    )


class AppUser(IdPkMixin, Base):
    """The admin login of the single-tenant UI.

    AAP 0.5.2 (``app_user``) and 0.3.2 (security architecture). ``cli.py ensure-admin``
    creates or updates the admin from ``APP_ADMIN_EMAIL`` and ``APP_ADMIN_PASSWORD_HASH``
    idempotently on :attr:`__natural_key__`. This is the only ``email`` column in the
    schema; lead and contact emails are stored solely as SHA-256 hashes elsewhere.

    Attributes:
        email: The login email, unique.
        password_hash: The argon2id hash of the password; the plaintext is never stored.
    """

    __tablename__ = "app_user"
    __natural_key__: ClassVar[tuple[str, ...]] = ("email",)

    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]


class SourceConnection(IdPkMixin, Base):
    """Current mode, health and active dataset of one source, plus its outbound call budget.

    AAP 0.5.2 (``source_connection``) and 0.3.3 (error classification and mock-data
    fallback). There is exactly one row per source, upserted on :attr:`__natural_key__` by
    ``sync/mode.py`` on every attempt. ``active_dataset_id`` is ``ON DELETE SET NULL`` so a
    purge never removes the connection row itself.

    ``calls_by_minute`` backs the product's own Google cap of ``GOOGLE_DAILY_CALL_BUDGET``
    webhook calls per sliding 24 h (AAP 0.3.3). The cap must hold across the worker and the
    CLI processes, so the count lives in the database: ``sync/budget_guard.py`` keeps
    per-UTC-minute call counts here, updated under ``SELECT ... FOR UPDATE`` on this row.
    A call is booked in the minute by which its POST has started, and a minute bucket
    counts until 24 hours after it ends. ``calls_24h`` is the total of the buckets still
    counting, shown on ``/sources``. That is a tight upper bound on the calls in the
    trailing 24 hours, not an exact count: a call counts from its admission until 24 hours
    after the minute by which its POST has started, at most 24 hours plus the call
    budget's 140 s dispatch bound plus one minute, and a call whose reservation had to be
    made again may count twice.

    Attributes:
        source: The source (:data:`SOURCES`), unique.
        mode: ``live`` or ``simulated`` (:data:`MODES`).
        status: ``ok``, ``stale``, ``denied`` or ``error`` (:data:`CONNECTION_STATUSES`).
        reason: Why the source is in its mode or status, for example "credentials not
            configured" or the platform's error code and failing endpoint.
        active_dataset_id: The source's active primary dataset, if any.
        last_attempt_at: When a job last called the source.
        calls_24h: Call bookings in the counted window described above, a tight upper
            bound on the calls in the trailing 24 hours (server default ``0``).
        calls_by_minute: Call counts keyed by UTC minute, ``"YYYY-MM-DDTHH:MM"`` (server
            default ``'{}'::jsonb``).
        access_generation: Incremented in the same transaction as every recorded access
            denial and every entry into automatic fallback (server default ``0``). A live
            build is bound to the value read before its probe and may activate only while
            it is unchanged, so a denial recorded by any process voids every earlier live
            build of the source.
    """

    __tablename__ = "source_connection"
    __natural_key__: ClassVar[tuple[str, ...]] = ("source",)

    source: Mapped[str] = mapped_column(unique=True)
    mode: Mapped[str]
    status: Mapped[str]
    reason: Mapped[str | None]
    active_dataset_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("source_dataset.id", ondelete="SET NULL"),
    )
    last_attempt_at: Mapped[datetime | None]
    calls_24h: Mapped[int] = mapped_column(server_default=sa.text("0"))
    # Per-minute buckets rather than a single counter: a sliding 24 h window needs to drop
    # the oldest minute as time passes, which a lone running total cannot do.
    calls_by_minute: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    access_generation: Mapped[int] = mapped_column(server_default=sa.text("0"))

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("mode", MODES),
        check_in("status", CONNECTION_STATUSES),
    )


class SyncRun(IdPkMixin, Base):
    """History of one sync job run, as listed under recent runs on ``/sources``.

    AAP 0.5.2 (``sync_run``) and 0.11.1 (sync job logging: source, job, rows and duration).
    ``dataset`` names the freshness dataset the job wrote, when it wrote exactly one. The
    table is operational history, not provenance, so it carries no ``dataset_id`` and
    survives the purge of the datasets it refreshed. The index on (``source``,
    ``started_at DESC``) serves "latest runs per source".

    Attributes:
        source: The source the job synced (:data:`SOURCES`).
        job: The name of the job as ``sync/jobs.py`` and the scheduler record it; free text
            with no ``CHECK``, so adding a job never needs a migration.
        dataset: The dataset name the job refreshed (:data:`DATASETS`), if a single one.
        started_at: When the job started.
        finished_at: When the job finished, if it has.
        status: ``running``, ``succeeded`` or ``failed`` (:data:`SYNC_RUN_STATUSES`).
        rows: Rows the job wrote, once known.
        completeness: Completeness of what the job read (:data:`COMPLETENESS`), once known.
        error: The failure message of a failed job, with secrets redacted.
    """

    __tablename__ = "sync_run"

    source: Mapped[str]
    job: Mapped[str]
    dataset: Mapped[str | None]
    # Bound to a name (not annotation-only) so __table_args__ can index it descending.
    started_at: Mapped[datetime] = mapped_column()
    finished_at: Mapped[datetime | None]
    status: Mapped[str]
    rows: Mapped[int | None]
    completeness: Mapped[str | None]
    error: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("dataset", DATASETS),
        check_in("status", SYNC_RUN_STATUSES),
        check_in("completeness", COMPLETENESS),
        sa.Index("ix_sync_run_source_started", "source", started_at.desc()),
    )


class SyncRequest(IdPkMixin, Base):
    """A pending or picked "Sync now" request for one source.

    AAP 0.5.2 (``sync_request``) and 0.5.3 (``POST /sources/{source}/sync``). The worker
    polls rows with ``picked_at IS NULL`` on every wake and stamps ``picked_at`` when it
    takes one. The partial unique index ``uq_sync_request_pending`` allows at most one
    pending request per source, which is what makes the API answer 409 when one is already
    pending.

    Attributes:
        source: The source to sync (:data:`SOURCES`).
        requested_at: When the request was made (server default ``now()``).
        picked_at: When the worker picked the request up; ``None`` while pending.
    """

    __tablename__ = "sync_request"

    source: Mapped[str]
    requested_at: Mapped[datetime] = mapped_column(server_default=sa.text("now()"))
    picked_at: Mapped[datetime | None]

    __table_args__ = (
        check_in("source", SOURCES),
        sa.Index(
            "uq_sync_request_pending",
            "source",
            unique=True,
            postgresql_where=sa.text("picked_at IS NULL"),
        ),
    )
