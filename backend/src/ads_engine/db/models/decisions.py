"""Decision log and discrepancies: ``decision`` and ``discrepancy``.

AAP 0.5.2 assigns these tables to this module:

* ``decision``: the append-only log of what was last decided for each decision subject, a
  campaign or a Google explicitly shared budget. Every row holds the complete materialised
  expected state for the subject's decision shape (``own_budget``, ``shared_member`` or
  ``budget``), never a partial patch, and ``ck_decision_shape_columns`` enforces which
  columns each shape may fill. The latest row per subject is the "last decided" state.
* ``discrepancy``: a mismatch between a subject's newest ``entity_snapshot`` and its current
  decision, raised after each entity sync (rule C0, AAP 0.5.4.4). At most one non-resolved
  row exists per subject and kind (``uq_discrepancy_open_subject_kind``).

Decisions and discrepancies are dataset-scoped (AAP 0.3.3), so a simulated decision never
becomes the expected state of a live campaign. Purging a retired dataset is the single
``DELETE FROM source_dataset WHERE id = :retired``: the ``dataset_id`` cascade removes both
tables' rows, and the intra-dataset keys (``discrepancy`` -> ``decision`` and
``entity_snapshot``, ``decision`` -> ``budget``) are ``ON DELETE CASCADE`` as well, so
PostgreSQL resolves the order itself. Those keys are dataset-qualified (AAP 0.3.3: only
``lead_match`` links two datasets): each composite (``<column>``, ``dataset_id``) references
the parent's unique (``dataset_id``, ``id``), so a decision or discrepancy can name only a
budget, snapshot or decision of its own dataset and purging another dataset never cascades
into it; a ``NULL`` key is not checked. Each referencing column is indexed, because PostgreSQL
does not index referencing columns and the cascade would otherwise scan the child table once
per deleted parent row. ``decision.recommendation_id`` is the one cross-run key; it is
``ON DELETE SET NULL``, the action AAP 0.5.2 gives every key that points into run history.

``subject_id`` holds the internal ``id`` of the ``campaign`` or ``budget`` row named by
``subject_type``. One column cannot reference two tables, so it has no foreign key; a subject
and its decisions always belong to the same dataset and leave together through the
``dataset_id`` cascade. ``ads_engine/decisions.py`` guarantees it: every decision and
discrepancy takes its ``dataset_id`` from the subject row itself (``load_subject_state``,
``_subject_provenance``).

Neither model declares ``__natural_key__``: rows are inserted by ``ads_engine/decisions.py``
and the sync jobs, never upserted by ``db/repositories.py``. This module imports only
constants from its sibling modules; the foreign-key targets ``budget``, ``entity_snapshot``
and ``recommendation`` are referenced by table name, so it never imports
``ads_engine.db.models.analysis`` and the package stays free of import cycles.

``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, server default, constraint and index name declared here; ``alembic check``
must report no drift.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Final

# Imported as a module and always written ``sa.<name>``, as in the sibling model modules.
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, DatasetScopedMixin, IdPkMixin, check_in
from ads_engine.db.models.accounts import CAMPAIGN_STATUSES
from ads_engine.db.models.ops import SOURCES

__all__ = [
    "DECISION_ORIGINS",
    "DECISION_SHAPES",
    "DISCREPANCY_KINDS",
    "DISCREPANCY_RESOLUTIONS",
    "DISCREPANCY_STATES",
    "SUBJECT_TYPES",
    "Decision",
    "Discrepancy",
]

SUBJECT_TYPES: Final[tuple[str, ...]] = ("campaign", "budget")
"""What a decision or discrepancy is about: a campaign of either source, or a Google
explicitly shared budget (AAP 0.5.2). LinkedIn campaign groups are never decision subjects;
they are snapshotted and shown in campaign evidence only."""

DECISION_SHAPES: Final[tuple[str, ...]] = ("own_budget", "shared_member", "budget")
"""The decision shape fixed by the subject's current state (AAP 0.5.2 "Decisions: shapes").

* ``own_budget``: a LinkedIn campaign, or a Google campaign whose budget is not explicitly
  shared. Expected state: status plus the budget fields the campaign has.
* ``shared_member``: a Google campaign drawing from an explicitly shared budget. Expected
  state: status plus ``budget_id``, the shared budget it is assigned to, and never an amount.
* ``budget``: a Google explicitly shared budget. Expected state: exactly one amount,
  ``daily_budget`` for a ``DAILY`` period or ``total_budget`` for ``CUSTOM_PERIOD``, and no
  status.
"""

DECISION_ORIGINS: Final[tuple[str, ...]] = ("baseline", "user", "recommendation", "resolution")
"""Who wrote a decision row (AAP 0.5.2).

``baseline`` rows are written by the first entity sync of each new dataset, one per campaign
and shared budget in its own shape, so day one raises no false discrepancies. After the
baseline, rows come only from ``POST /decisions`` (``user``, or ``recommendation`` when the
decision is recorded from an item) and from discrepancy resolutions (``resolution``).
"""

DISCREPANCY_KINDS: Final[tuple[str, ...]] = ("status", "budget", "untracked", "missing")
"""The C0 discrepancy kinds (AAP 0.5.4.4).

* ``status``: the normalised platform status differs from the decided status.
* ``budget``: a decided amount differs at native precision (Google micros, LinkedIn minor
  unit), or a shared member's budget assignment differs.
* ``untracked``: an entity first seen after its dataset's baseline, with no decision.
* ``missing``: a tracked entity the platform no longer returns, inferred only from a
  complete enumeration (AAP 0.3.3 safe absence).
"""

DISCREPANCY_STATES: Final[tuple[str, ...]] = ("open", "awaiting_platform", "resolved")
"""Discrepancy lifecycle (AAP 0.5.4.4). ``awaiting_platform`` follows a resolution that waits
for the platform to change (``decision_correct`` or ``record_intended``) and closes when a
later snapshot matches the current decision."""

DISCREPANCY_RESOLUTIONS: Final[tuple[str, ...]] = (
    "platform_correct",
    "decision_correct",
    "adopt_platform",
    "record_intended",
    "acknowledge_removal",
)
"""The resolution API keys (AAP 0.5.4.4). Which ones apply depends on the (kind, subject
type) pair; ``ads_engine/decisions.py`` holds that table (``RESOLUTIONS``) and rejects any
other combination with 422."""

# The CHECK text the migration repeats verbatim: the columns each decision shape may fill.
_SHAPE_COLUMNS_SQL: Final[str] = (
    "(shape = 'own_budget' AND subject_type = 'campaign' AND status IS NOT NULL"
    " AND budget_id IS NULL)"
    " OR (shape = 'shared_member' AND subject_type = 'campaign' AND status IS NOT NULL"
    " AND budget_id IS NOT NULL AND daily_budget IS NULL AND total_budget IS NULL)"
    " OR (shape = 'budget' AND subject_type = 'budget' AND status IS NULL"
    " AND budget_id IS NULL AND (daily_budget IS NULL) <> (total_budget IS NULL))"
)


class Decision(IdPkMixin, DatasetScopedMixin, Base):
    """One materialised expected state of a decision subject, appended to the decision log.

    AAP 0.5.2 (``decision``) and 0.5.4.4. The table is append-only: no row is ever updated,
    and the latest row per subject (by ``decided_at``) is the subject's "last decided" state,
    the state the discrepancy check (C0) compares each new snapshot against.
    ``ix_decision_subject_latest`` on (``subject_type``, ``subject_id``, ``decided_at DESC``)
    serves that lookup and the per-subject history of ``GET /decisions``.
    ``ix_decision_dataset_decided`` on (``dataset_id``, ``decided_at DESC``, ``id DESC``)
    serves the log's newest-first page: ``GET /decisions`` reads the newest rows of each
    active dataset as one bounded index probe per dataset, never the whole history.

    ``budget_id`` is dataset-qualified: (``budget_id``, ``dataset_id``) references the
    budget's unique (``dataset_id``, ``id``). The table's own unique (``dataset_id``, ``id``)
    is the target of ``discrepancy.decision_id``'s dataset-qualified foreign key.

    A submission is a patch carrying only the fields being decided. ``ads_engine/decisions.py``
    reads the subject's current expected state, applies the patch, validates the result and
    writes one complete row, so a budget-only decision keeps the last decided status and a
    status-only decision keeps the last decided budget. The submitted patch itself is kept in
    ``patch`` for the decision log.

    ``ck_decision_shape_columns`` enforces which columns each shape may fill:

    * ``own_budget`` (campaign subject): a status, no ``budget_id``, and any of the campaign's
      own amounts (``decisions.py`` admits only the fields the campaign actually has);
    * ``shared_member`` (campaign subject): a status and the assigned shared ``budget_id``,
      never an amount, because the amount belongs to the shared budget;
    * ``budget`` (budget subject): no status, no ``budget_id`` and exactly one amount, the one
      the budget's period allows.

    A change to a shared budget's amount is therefore recorded on the budget subject alone
    and can never create a discrepancy on its members. A shared budget whose amount Google
    omits (``budget.amount`` is ``NULL``) has no materialisable ``budget`` state, so no
    ``budget`` row can be written for it until the amount is known.

    A tombstone row, written only by the ``acknowledge_removal`` resolution of a ``missing``
    discrepancy, still satisfies ``ck_decision_shape_columns``: ``ads_engine/decisions.py``
    carries the last materialised amount into a budget subject's tombstone and writes status
    ``ended`` (with the rest of the shape kept) into a campaign's. While the entity stays
    absent, no further ``missing`` discrepancy is raised against it.

    Attributes:
        subject_type: ``campaign`` or ``budget`` (:data:`SUBJECT_TYPES`).
        subject_id: Internal id of the ``campaign`` or ``budget`` row named by
            ``subject_type``; no foreign key, because the referenced table varies.
        shape: ``own_budget``, ``shared_member`` or ``budget`` (:data:`DECISION_SHAPES`).
        status: The decided normalised status (:data:`CAMPAIGN_STATUSES`); ``None`` for
            budget subjects, which have no status.
        daily_budget: The decided daily budget in the account's currency, validated to the
            currency's minor unit (Google: a whole number of micros); ``None`` where the shape
            or the subject has no daily budget.
        total_budget: The decided lifetime or custom-period budget in the account's
            currency; ``None`` where the shape or the subject has none.
        budget_id: For ``shared_member`` rows only, the shared
            :class:`~ads_engine.db.models.accounts.Budget` of the decision's own dataset the
            campaign is assigned to; ``None`` for every other shape.
        tombstone: Whether the row records an acknowledged removal (server default
            ``false``); set only by ``acknowledge_removal``.
        decided_at: When the decision was recorded, set by the writer with
            :func:`ads_engine.db.base.utcnow`.
        decided_by: Email of the user who recorded it; ``None`` for ``baseline`` rows and
            other system writes.
        origin: ``baseline``, ``user``, ``recommendation`` or ``resolution``
            (:data:`DECISION_ORIGINS`).
        recommendation_id: The :class:`~ads_engine.db.models.analysis.Recommendation` the
            decision was recorded from; ``None`` otherwise, and set to ``None`` if that
            recommendation is ever deleted.
        patch: The fields the submission carried, as a JSON object (server default
            ``'{}'::jsonb``; empty for ``baseline`` rows).
        note: Free-text note entered with the decision.
    """

    __tablename__ = "decision"

    subject_type: Mapped[str]
    subject_id: Mapped[int]
    shape: Mapped[str]
    status: Mapped[str | None]
    daily_budget: Mapped[Decimal | None]
    total_budget: Mapped[Decimal | None]
    budget_id: Mapped[int | None] = mapped_column(index=True)
    tombstone: Mapped[bool] = mapped_column(server_default=sa.text("false"))
    # Bound to a name (not annotation-only) so __table_args__ can index it descending.
    decided_at: Mapped[datetime] = mapped_column()
    decided_by: Mapped[str | None]
    origin: Mapped[str]
    # Runs are never purged in normal operation, so the SET NULL action needs no index here.
    recommendation_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("recommendation.id", ondelete="SET NULL"),
    )
    patch: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))
    note: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("subject_type", SUBJECT_TYPES),
        check_in("shape", DECISION_SHAPES),
        check_in("status", CAMPAIGN_STATUSES),
        check_in("origin", DECISION_ORIGINS),
        sa.CheckConstraint(_SHAPE_COLUMNS_SQL, name="shape_columns"),
        # Dataset-qualified: a shared member's budget must belong to this decision's
        # dataset; a NULL budget_id (every other shape) is not checked.
        sa.ForeignKeyConstraint(
            ["budget_id", "dataset_id"],
            ["budget.id", "budget.dataset_id"],
            ondelete="CASCADE",
        ),
        # Anchors the dataset-qualified (decision_id, dataset_id) foreign keys of child rows.
        sa.UniqueConstraint("dataset_id", "id"),
        sa.Index(
            "ix_decision_subject_latest",
            "subject_type",
            "subject_id",
            decided_at.desc(),
        ),
        # The decision log's newest-first page, probed per active dataset (GET /decisions).
        # ``id`` comes from IdPkMixin, so the class body names it rather than binding it.
        sa.Index(
            "ix_decision_dataset_decided",
            "dataset_id",
            decided_at.desc(),
            sa.desc("id"),
        ),
        # The table has no natural-key unique; this serves the purge's dataset_id cascade
        # (the DatasetScopedMixin contract that every dataset-scoped table has one).
        sa.Index("ix_decision_dataset_id", "dataset_id"),
    )


class Discrepancy(IdPkMixin, DatasetScopedMixin, Base):
    """A mismatch between a subject's platform state and its last decided state (rule C0).

    AAP 0.5.2 (``discrepancy``) and 0.5.4.4. After each entity sync, each subject's newest
    snapshot is compared with its current decision; a mismatch of a given kind opens a row,
    unless a non-resolved row of that kind already exists for the subject. The partial unique
    index ``uq_discrepancy_open_subject_kind`` on (``subject_type``, ``subject_id``,
    ``kind``) ``WHERE state <> 'resolved'`` enforces at most one non-resolved row per subject
    and kind, while resolved rows accumulate as history. ``ix_discrepancy_state_detected``
    serves ``GET /discrepancies?state=open`` and the C0 ordering, oldest ``detected_at``
    first.

    Lifecycle (AAP 0.5.4.4):

    * Every ``status``, ``budget`` and ``untracked`` row in ``open`` or ``awaiting_platform``
      auto-resolves when a later snapshot matches the current decision; this is re-checked on
      every entity sync.
    * ``platform_correct`` and ``adopt_platform`` write a decision from the platform state
      and close the row at once; ``decision_correct`` and ``record_intended`` move it to
      ``awaiting_platform`` until a matching snapshot arrives.
    * A ``missing`` row closes only through ``acknowledge_removal``, which writes a tombstone
      decision and resolves it immediately, or through a matching snapshot after
      ``decision_correct``, once a complete enumeration returns the entity again.

    The comparison values are stored as JSON so every kind and shape fits one table: a
    status, the amounts at native precision (Google micros), or a shared member's
    ``budget_id``.

    Attributes:
        subject_type: ``campaign`` or ``budget`` (:data:`SUBJECT_TYPES`).
        subject_id: Internal id of the ``campaign`` or ``budget`` row named by
            ``subject_type``; no foreign key, because the referenced table varies.
        kind: ``status``, ``budget``, ``untracked`` or ``missing``
            (:data:`DISCREPANCY_KINDS`).
        decided_value: The decided side of the comparison as a JSON object; ``None`` for
            ``untracked``, which has no decision, until ``record_intended`` writes the
            intended state as one and moves the row to ``awaiting_platform``; it then holds
            that state, or a decision recorded since.
        platform_value: The platform side of the comparison as a JSON object; ``None`` for
            ``missing``, where the platform no longer returns the entity.
        snapshot_id: The :class:`~ads_engine.db.models.snapshots.EntitySnapshot` of the
            discrepancy's own dataset the comparison read; ``None`` for ``missing``.
        decision_id: The :class:`Decision` of the discrepancy's own dataset the comparison
            read; ``None`` for ``untracked`` until ``record_intended`` writes its decision.
        detected_at: When the discrepancy was detected, set by the writer with
            :func:`ads_engine.db.base.utcnow`.
        state: ``open``, ``awaiting_platform`` or ``resolved`` (:data:`DISCREPANCY_STATES`).
        resolved_at: When the row was resolved; ``None`` until then.
        resolution: The resolution last applied (:data:`DISCREPANCY_RESOLUTIONS`); ``None``
            while ``open``. ``decision_correct`` and ``record_intended`` set it as they move
            the row to ``awaiting_platform``, and the resolutions that close at once set it
            on closing. Auto-resolution keeps it, so a row that closed from ``open`` on its
            own stays ``None``.
    """

    __tablename__ = "discrepancy"

    subject_type: Mapped[str]
    subject_id: Mapped[int]
    kind: Mapped[str]
    decided_value: Mapped[dict[str, Any] | None]
    platform_value: Mapped[dict[str, Any] | None]
    snapshot_id: Mapped[int | None] = mapped_column(index=True)
    decision_id: Mapped[int | None] = mapped_column(index=True)
    detected_at: Mapped[datetime]
    state: Mapped[str]
    resolved_at: Mapped[datetime | None]
    resolution: Mapped[str | None]

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("subject_type", SUBJECT_TYPES),
        check_in("kind", DISCREPANCY_KINDS),
        check_in("state", DISCREPANCY_STATES),
        check_in("resolution", DISCREPANCY_RESOLUTIONS),
        # Dataset-qualified: the snapshot and the decision compared must belong to this
        # discrepancy's dataset; a NULL snapshot_id (missing) or decision_id (untracked) is
        # not checked.
        sa.ForeignKeyConstraint(
            ["snapshot_id", "dataset_id"],
            ["entity_snapshot.id", "entity_snapshot.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id", "dataset_id"],
            ["decision.id", "decision.dataset_id"],
            ondelete="CASCADE",
        ),
        sa.Index(
            "uq_discrepancy_open_subject_kind",
            "subject_type",
            "subject_id",
            "kind",
            unique=True,
            postgresql_where=sa.text("state <> 'resolved'"),
        ),
        sa.Index("ix_discrepancy_state_detected", "state", "detected_at"),
        # No index above leads with dataset_id; this serves the purge's dataset_id cascade
        # (the DatasetScopedMixin contract that every dataset-scoped table has one).
        sa.Index("ix_discrepancy_dataset_id", "dataset_id"),
    )
