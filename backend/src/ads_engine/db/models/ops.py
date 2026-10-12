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
    "COMPLETENESS_COMPLETE",
    "COMPLETENESS_INCOMPLETE",
    "COMPLETENESS_UNKNOWN",
    "CONNECTION_STATUSES",
    "CONNECTION_STATUS_DENIED",
    "CONNECTION_STATUS_ERROR",
    "CONNECTION_STATUS_OK",
    "CONNECTION_STATUS_STALE",
    "DATASETS",
    "DATASET_GOOGLE_ENTITIES",
    "DATASET_GOOGLE_METRICS_DAILY",
    "DATASET_GOOGLE_SEARCH_TERMS",
    "DATASET_HUBSPOT_ASSOCIATIONS",
    "DATASET_HUBSPOT_CONTACTS",
    "DATASET_HUBSPOT_DEAL_SEARCH",
    "DATASET_HUBSPOT_GOOGLE_CONTACTS",
    "DATASET_LINKEDIN_ANALYTICS_DAILY",
    "DATASET_LINKEDIN_ENTITIES",
    "DATASET_LINKEDIN_LEADS",
    "DATASET_LINKEDIN_SEGMENTS",
    "DATASET_ROLES",
    "DATASET_ROLE_COMPANION",
    "DATASET_ROLE_PRIMARY",
    "DATASET_STATES",
    "DATASET_STATE_ACTIVE",
    "DATASET_STATE_BUILDING",
    "DATASET_STATE_RETIRED",
    "MODES",
    "MODE_LIVE",
    "MODE_SIMULATED",
    "SOURCES",
    "SOURCE_GOOGLE",
    "SOURCE_HUBSPOT",
    "SOURCE_LINKEDIN",
    "SYNC_RUN_FAILED",
    "SYNC_RUN_RUNNING",
    "SYNC_RUN_STATUSES",
    "SYNC_RUN_SUCCEEDED",
    "AppUser",
    "DatasetFreshness",
    "SourceConnection",
    "SourceDataset",
    "SyncRequest",
    "SyncRun",
]

SOURCE_GOOGLE: Final = "google"
"""The Google Ads source (through the n8n webhook)."""
SOURCE_LINKEDIN: Final = "linkedin"
"""The LinkedIn Ads source."""
SOURCE_HUBSPOT: Final = "hubspot"
"""The HubSpot CRM source."""

SOURCES: Final[tuple[str, ...]] = (SOURCE_GOOGLE, SOURCE_LINKEDIN, SOURCE_HUBSPOT)
"""The external data sources, in display order: the allowed values of every ``source``."""

MODE_LIVE: Final = "live"
"""A dataset of real platform data."""
MODE_SIMULATED: Final = "simulated"
"""A dataset of seeded mock data in the live wire shape."""

MODES: Final[tuple[str, ...]] = (MODE_LIVE, MODE_SIMULATED)
"""Whether a dataset holds real platform data or seeded mock data in the live wire shape."""

DATASET_STATE_BUILDING: Final = "building"
"""A dataset being backfilled; nothing reads it until it is activated."""
DATASET_STATE_ACTIVE: Final = "active"
"""The dataset analysis and the UI read."""
DATASET_STATE_RETIRED: Final = "retired"
"""A replaced dataset: read-only history until it is purged."""

DATASET_STATES: Final[tuple[str, ...]] = (
    DATASET_STATE_BUILDING,
    DATASET_STATE_ACTIVE,
    DATASET_STATE_RETIRED,
)
"""Dataset lifecycle: backfilled while ``building``, read while ``active``, purged later."""

DATASET_ROLE_PRIMARY: Final = "primary"
"""A source's own dataset."""
DATASET_ROLE_COMPANION: Final = "companion"
"""The simulated HubSpot dataset built alongside a simulated LinkedIn one."""

DATASET_ROLES: Final[tuple[str, ...]] = (DATASET_ROLE_PRIMARY, DATASET_ROLE_COMPANION)
"""``primary`` is a source's own dataset; ``companion`` is the simulated CRM of a LinkedIn one."""

DATASET_GOOGLE_ENTITIES: Final = "google.entities"
"""Google customer, campaigns, budgets, conversion actions, ad groups and keywords."""
DATASET_GOOGLE_METRICS_DAILY: Final = "google.metrics_daily"
"""Google campaign and keyword daily facts (Q-G3, Q-G5)."""
DATASET_GOOGLE_SEARCH_TERMS: Final = "google.search_terms"
"""Google search-term daily facts (Q-G6)."""
DATASET_LINKEDIN_ENTITIES: Final = "linkedin.entities"
"""LinkedIn accounts, campaign groups, campaigns, creatives and lead forms."""
DATASET_LINKEDIN_ANALYTICS_DAILY: Final = "linkedin.analytics_daily"
"""LinkedIn creative daily facts."""
DATASET_LINKEDIN_SEGMENTS: Final = "linkedin.segments"
"""LinkedIn demographic aggregates per exact preset window."""
DATASET_LINKEDIN_LEADS: Final = "linkedin.leads"
"""LinkedIn lead form responses."""
DATASET_HUBSPOT_CONTACTS: Final = "hubspot.contacts"
"""HubSpot contacts matched to leads, refreshed by id."""
DATASET_HUBSPOT_ASSOCIATIONS: Final = "hubspot.associations"
"""HubSpot associations with their target reads."""
DATASET_HUBSPOT_DEAL_SEARCH: Final = "hubspot.deal_search"
"""HubSpot deals created in the search range."""
DATASET_HUBSPOT_GOOGLE_CONTACTS: Final = "hubspot.google_contacts"
"""HubSpot contacts with a Google campaign touch."""

DATASETS: Final[tuple[str, ...]] = (
    DATASET_GOOGLE_ENTITIES,
    DATASET_GOOGLE_METRICS_DAILY,
    DATASET_GOOGLE_SEARCH_TERMS,
    DATASET_LINKEDIN_ENTITIES,
    DATASET_LINKEDIN_ANALYTICS_DAILY,
    DATASET_LINKEDIN_SEGMENTS,
    DATASET_LINKEDIN_LEADS,
    DATASET_HUBSPOT_CONTACTS,
    DATASET_HUBSPOT_ASSOCIATIONS,
    DATASET_HUBSPOT_DEAL_SEARCH,
    DATASET_HUBSPOT_GOOGLE_CONTACTS,
)
"""The freshness-tracked dataset names; each rule declares which of them it reads."""

COMPLETENESS_COMPLETE: Final = "complete"
"""Every row of the covered range was read."""
COMPLETENESS_INCOMPLETE: Final = "incomplete"
"""Part of the covered range was not read in full; the reason says what."""
COMPLETENESS_UNKNOWN: Final = "unknown"
"""Nothing has been established yet (before the first successful read)."""

COMPLETENESS: Final[tuple[str, ...]] = (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INCOMPLETE,
    COMPLETENESS_UNKNOWN,
)
"""Completeness of a dataset over its covered range (``unknown`` before the first read)."""

CONNECTION_STATUS_OK: Final = "ok"
"""The source answered its last call."""
CONNECTION_STATUS_STALE: Final = "stale"
"""Transient failures outlived their retries; the last good data stands."""
CONNECTION_STATUS_DENIED: Final = "denied"
"""The platform refused access."""
CONNECTION_STATUS_ERROR: Final = "error"
"""A defect (a query or contract error) stopped the source."""

CONNECTION_STATUSES: Final[tuple[str, ...]] = (
    CONNECTION_STATUS_OK,
    CONNECTION_STATUS_STALE,
    CONNECTION_STATUS_DENIED,
    CONNECTION_STATUS_ERROR,
)
"""Health of a source connection as shown on ``/sources``."""

SYNC_RUN_RUNNING: Final = "running"
"""A sync job run that has started and not finished."""
SYNC_RUN_SUCCEEDED: Final = "succeeded"
"""A sync job run that recorded its dataset's success."""
SYNC_RUN_FAILED: Final = "failed"
"""A sync job run that failed, was cancelled or was interrupted."""

SYNC_RUN_STATUSES: Final[tuple[str, ...]] = (
    SYNC_RUN_RUNNING,
    SYNC_RUN_SUCCEEDED,
    SYNC_RUN_FAILED,
)
"""Outcome of one sync job run."""

# The CHECK text the migration repeats verbatim. A primary dataset has no parent; a companion
# is always a simulated HubSpot dataset hanging off its (LinkedIn) parent.
_ROLE_SHAPE_SQL: Final[str] = (
    "(role = 'primary' AND parent_dataset_id IS NULL) OR "
    "(role = 'companion' AND source = 'hubspot' AND mode = 'simulated' "
    "AND parent_dataset_id IS NOT NULL)"
)
# The CHECK text the migration repeats verbatim. A live dataset names no simulated world; a
# simulated one names its world and seed together, or neither (rows written before worlds
# were recorded).
_SIMULATED_WORLD_SQL: Final[str] = (
    "(mode = 'live' AND simulated_world IS NULL AND simulated_seed IS NULL) OR "
    "(mode = 'simulated' AND (simulated_world IS NULL) = (simulated_seed IS NULL))"
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

    A simulated dataset records the world it was generated from (``MOCK_SCENARIO`` and
    ``MOCK_SEED``), and a companion its parent's, so one dataset never mixes two worlds: a
    changed setting builds and activates a new dataset through the normal transition, and
    the old one becomes historical. The ``simulated_world`` check keeps both columns empty on
    live datasets and set together on simulated ones.

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
        simulated_world: For simulated datasets, the runtime world (``MOCK_SCENARIO``) it was
            generated from; ``None`` for live datasets and for simulated rows written without
            one, which are never replaced for it.
        simulated_seed: For simulated datasets, the world's seed (``MOCK_SEED``); set exactly
            when ``simulated_world`` is.
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
    simulated_world: Mapped[str | None]
    simulated_seed: Mapped[int | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("mode", MODES),
        check_in("state", DATASET_STATES),
        check_in("role", DATASET_ROLES),
        sa.CheckConstraint(_ROLE_SHAPE_SQL, name="role_shape"),
        sa.CheckConstraint(_SIMULATED_WORLD_SQL, name="simulated_world"),
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

    :attr:`checkpoint` is written only by
    :func:`ads_engine.sync.freshness.write_checkpoint`, never by ``record_freshness``, in the
    transaction of the Google job whose fetch it describes.

    Attributes:
        dataset: The dataset name (:data:`DATASETS`).
        last_attempt_at: When a job last tried to refresh the dataset.
        last_success_at: When a job last refreshed it successfully.
        covered_from: First native date the stored data covers.
        covered_through: Last native date the stored data covers.
        completeness: ``complete``, ``incomplete`` or ``unknown`` (:data:`COMPLETENESS`).
        reason: Why the dataset is not complete, for example "result limit reached at
            finest partition" or a call-budget deferral.
        checkpoint: The unread work of an interrupted Google fetch that the next run of the
            dataset's job resumes before anything else: the deferred partition scopes per
            query and, for ``google.entities``, the progress of each unfinished safe-absence
            enumeration (``sync/jobs.py`` documents the JSON shapes). ``None`` when nothing
            is outstanding, and always for LinkedIn and HubSpot datasets.
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
    # Beyond the planned freshness columns: pages the Google call budget defers must finish in
    # a later 24 h window, across worker restarts and CLI runs, so the unread scopes are kept
    # durably on the row whose completeness they decide, and retire with its dataset.
    checkpoint: Mapped[dict[str, Any] | None]

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
        session_generation: The generation of this user's sessions (server default ``0``).
            Every session cookie carries the value it was issued with, and
            ``api/deps.py`` accepts a cookie only while it still equals this column.
            ``POST /auth/logout`` increments it, which revokes every session issued
            before the sign-out, on every browser and every API replica.
            ``ensure-admin`` increments it when it stores a different password hash, which
            revokes every earlier session the same way, and leaves it unchanged when the
            configured hash equals the stored one.
    """

    __tablename__ = "app_user"
    __natural_key__: ClassVar[tuple[str, ...]] = ("email",)

    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str]
    session_generation: Mapped[int] = mapped_column(server_default=sa.text("0"))


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
        last_attempt_at: When a job or a live probe last called the source, successful or
            not; it never moves back.
        last_failure_at: When a transient, defective or denied call last marked the
            connection, or a live probe or build last failed into automatic fallback; it
            never moves back. A ``stale`` or ``error`` status returns to ``ok`` only once
            every freshness row of the active dataset has succeeded at or after it.
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
    last_failure_at: Mapped[datetime | None]
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
