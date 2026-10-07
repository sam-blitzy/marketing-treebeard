"""Append-only, dated snapshots of platform entity state: ``entity_snapshot``.

AAP 0.5.2 assigns ``entity_snapshot`` to this module. Every entity sync appends one row per
entity it read (campaigns, LinkedIn campaign groups, Google budgets, keywords, creatives and
Google conversion actions), recording the status, budget and quality score observed at that
instant. Rows are never updated or deleted while their dataset is active:
``db/repositories.append_snapshots`` only inserts, so the table carries no natural key and
no ``__natural_key__``. The rows of a retired dataset are removed with it by the
``dataset_id`` cascade when the dataset is purged (AAP 0.3.3). That cascade looks rows up
through ``ix_entity_snapshot_dataset_id``, because an active dataset's history is never
purged and no other index of the table leads with ``dataset_id``.

The snapshots are the state the logic depends on beyond the fetched daily facts:

* **Discrepancy check (C0)**: after each entity sync, each decision subject's newest
  snapshot is compared with its current decision (AAP 0.5.4.4). Google budget amounts are
  compared at native precision through ``budget_amount_micros``.
* **Budget history**: ``captured_at`` is an *observation* instant, never an effective time.
  Whether a native day's budget and status are whole-day known, changed intraday or
  unobserved is derived from consecutive observations by ``analysis/metrics.py``
  (AAP 0.5.4.1); this table stores only what was seen and when.
* **Dated shared-budget membership**: on campaign rows, ``budget_id`` is the Google budget
  the campaign drew from at that instant, so membership of a shared budget is evaluated day
  by day from snapshot history rather than from today's member list (AAP 0.5.4.5).
* **Quality-score trend (K3)**: keyword rows carry ``quality_info.quality_score`` from Q-G7,
  including keywords with no traffic, the fallback when daily scores are absent
  (AAP 0.5.4.3).
* **Counting contract (B4)**: conversion-action rows keep the settings that say how Google
  counted ``metrics.conversions`` on each day (AAP 0.5.4.5).

``backend/migrations/versions/0001_initial_schema.py`` mirrors every column, type,
nullability, server default, constraint and index name declared here; ``alembic check``
must report no drift.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from ads_engine.db.base import Base, DatasetScopedMixin, IdPkMixin, check_in
from ads_engine.db.models.ops import SOURCES

__all__ = ["ENTITY_TYPES", "EntitySnapshot"]

ENTITY_TYPES: Final[tuple[str, ...]] = (
    "campaign",
    "campaign_group",
    "budget",
    "keyword",
    "creative",
    "conversion_action",
)
"""The entity kinds a snapshot can describe; each value names the table ``entity_id`` points at.

``campaign_group`` is LinkedIn only; ``budget``, ``keyword`` and ``conversion_action`` are
Google only; ``creative`` is LinkedIn only; ``campaign`` covers both sources.
"""


class EntitySnapshot(IdPkMixin, DatasetScopedMixin, Base):
    """One observation of one platform entity, taken by one entity sync.

    AAP 0.5.2 (``entity_snapshot``). The table is append-only: every entity sync writes
    exactly one row per entity it read, and no row is ever updated. A snapshot records what
    the platform reported at its ``captured_at`` instant. It is an observation, not an
    effective time: it does not say when a budget or status change took effect, and
    observations every few hours cannot establish that. ``analysis/metrics.py`` classifies
    each native day as whole-day known, changed intraday or unobserved from the
    observations that bracket it (AAP 0.5.4.1).

    ``entity_id`` is polymorphic: it holds the internal ``id`` of a row in the table named by
    ``entity_type`` (``campaign``, ``campaign_group``, ``budget``, ``keyword``, ``creative``
    or ``conversion_action``). A single column cannot reference several tables, so it has no
    foreign key; the row is still removed with its dataset through the ``dataset_id``
    cascade, because a snapshot always belongs to the same dataset as its entity.

    ``budget_id`` is a real foreign key to ``budget`` with ``ON DELETE CASCADE``, consistent
    with every other key between rows of one dataset (AAP 0.5.2). It is indexed
    (``ix_entity_snapshot_budget_id``) for dated shared-budget membership queries and so the
    cascade does not scan the table per deleted budget.

    The index ``ix_entity_snapshot_entity_captured`` on (``entity_type``, ``entity_id``,
    ``captured_at DESC``) serves both "newest snapshot per entity" for the discrepancy check
    and the ordered observation history of one entity for budget-day classification.

    The index ``ix_entity_snapshot_dataset_id`` on (``dataset_id``) serves the purge's
    ``dataset_id`` cascade from ``source_dataset``. The table has no natural-key unique to
    lead with ``dataset_id``, and active snapshot history is kept with no purge (AAP 0.3.2),
    so without it deleting a retired dataset would scan the whole history.

    Attributes:
        entity_type: The kind of entity observed (:data:`ENTITY_TYPES`).
        entity_id: Internal id of the observed row in the table named by ``entity_type``;
            no foreign key, because the referenced table varies by ``entity_type``.
        captured_at: The observation instant, set by the writer with
            :func:`ads_engine.db.base.utcnow`; never an effective time.
        status: For campaigns, the normalised status (``active``, ``paused`` or ``ended``,
            the values of ``CAMPAIGN_STATUSES``); for every other entity type, the raw
            platform status as returned. ``None`` where the entity has no status, such as
            a Google budget. No ``CHECK``, because raw values vary by platform.
        budget_daily: The daily budget observed, in the account's currency: a LinkedIn
            campaign's ``dailyBudget``, a campaign group's daily budget, or a Google
            ``DAILY`` budget amount (on the budget row and its campaigns' rows).
        budget_total: The lifetime or custom-period budget observed, in the account's
            currency.
        budget_amount_micros: The Google budget amount in micros, Google's native integer
            precision, so C0 compares without rounding (AAP 0.5.4.4); ``None`` for LinkedIn.
        budget_id: On Google campaign rows, the :class:`~ads_engine.db.models.accounts.Budget`
            the campaign drew from at ``captured_at``; this gives dated shared-budget
            membership (AAP 0.5.4.5). ``None`` for every other row.
        quality_score: On keyword rows, ``ad_group_criterion.quality_info.quality_score``
            from Q-G7 (1 to 10), including keywords with no traffic; ``None`` when Google
            leaves it unpopulated and on every other entity type.
        payload: The remaining observed fields as a JSON object (server default
            ``'{}'::jsonb``), for example a conversion action's
            ``include_in_conversions_metric``, ``counting_type`` and ``attribution_model``,
            or a budget's ``period`` and ``explicitly_shared``. An unknown ``counting_type``
            or ``attribution_model`` is kept as JSON ``null``, never filled in.
    """

    __tablename__ = "entity_snapshot"

    entity_type: Mapped[str]
    entity_id: Mapped[int]
    # Bound to a name (not annotation-only) so __table_args__ can index it descending.
    captured_at: Mapped[datetime] = mapped_column()
    status: Mapped[str | None]
    budget_daily: Mapped[Decimal | None]
    budget_total: Mapped[Decimal | None]
    budget_amount_micros: Mapped[int | None]
    budget_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("budget.id", ondelete="CASCADE"),
        index=True,
    )
    quality_score: Mapped[int | None]
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=sa.text("'{}'::jsonb"))

    __table_args__ = (
        check_in("source", SOURCES),
        check_in("entity_type", ENTITY_TYPES),
        sa.Index(
            "ix_entity_snapshot_entity_captured",
            "entity_type",
            "entity_id",
            captured_at.desc(),
        ),
        # No index above leads with dataset_id; this serves the purge's dataset_id cascade.
        sa.Index("ix_entity_snapshot_dataset_id", "dataset_id"),
    )
