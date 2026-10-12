"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-06 00:00:00+00:00

Creates every table of AAP 0.5.2, 41 in all, exactly as ``ads_engine.db.models`` declares
them on ``Base.metadata``: the same columns in the same order, types, nullability, server
defaults, constraint names and index definitions, so ``alembic check`` reports no drift.

The revision is frozen. It imports nothing from ``ads_engine``; the naming convention, the
enumerated values and the ``CHECK`` texts are repeated here as literals, so later model
changes arrive only through later revisions and never rewrite this one.

Foreign-key actions are the contract the dataset purge relies on (AAP 0.3.3). A retired
dataset is removed by the single statement ``DELETE FROM source_dataset WHERE id = :retired``
and PostgreSQL resolves the order itself:

* every ``dataset_id``, ``crm_dataset_id`` and ``parent_dataset_id`` reference, and every
  foreign key between two rows of the same dataset, is ``ON DELETE CASCADE``;
* every foreign key between two rows of the same dataset is dataset-qualified
  (:func:`_same_dataset_fk`): the composite (``<column>``, ``dataset_id``) references the
  parent's unique (``dataset_id``, ``id``), so a row can never point at another dataset's
  parent and purging one dataset never cascades into another;
* ``lead_match`` is the only table that links two datasets (a LinkedIn lead and a CRM
  contact), and both of its links cascade, so purging either side removes only its own
  matching state;
* run tables (``recommendation``, ``run_finding``, ``run_withheld``) reference entities
  through nullable keys that are ``ON DELETE SET NULL``, and ``analysis_run`` keeps its
  dataset ids in ``jsonb`` with no foreign key, so run history survives a purge.

Enumerations are ``text`` columns guarded by ``CHECK (<column> IN (...))`` constraints, never
PostgreSQL ``ENUM`` types.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Repeats ads_engine.db.base.NAMING_CONVENTION key for key and value for value. Alembic's
# ``op`` MetaData has no naming convention, so every name below is the name this convention
# produces for the model (or the model's explicit name), spelled out and marked final with
# ``op.f``. Every name is at most 63 characters, PostgreSQL's identifier limit.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# ---------------------------------------------------------------------------------------
# Enumerated values, frozen copies of the model tuples in their declared order. The order
# fixes the CHECK text, so it must not be changed here.
# ---------------------------------------------------------------------------------------

# mirrors ads_engine.db.models.ops.SOURCES
SOURCES: tuple[str, ...] = ("google", "linkedin", "hubspot")
# mirrors ads_engine.db.models.ops.MODES
MODES: tuple[str, ...] = ("live", "simulated")
# mirrors ads_engine.db.models.ops.DATASET_STATES
DATASET_STATES: tuple[str, ...] = ("building", "active", "retired")
# mirrors ads_engine.db.models.ops.DATASET_ROLES
DATASET_ROLES: tuple[str, ...] = ("primary", "companion")
# mirrors ads_engine.db.models.ops.DATASETS
DATASETS: tuple[str, ...] = (
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
# mirrors ads_engine.db.models.ops.COMPLETENESS
COMPLETENESS: tuple[str, ...] = ("complete", "incomplete", "unknown")
# mirrors ads_engine.db.models.ops.CONNECTION_STATUSES
CONNECTION_STATUSES: tuple[str, ...] = ("ok", "stale", "denied", "error")
# mirrors ads_engine.db.models.ops.SYNC_RUN_STATUSES
SYNC_RUN_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")
# mirrors ads_engine.db.models.accounts.CAMPAIGN_STATUSES
CAMPAIGN_STATUSES: tuple[str, ...] = ("active", "paused", "ended")
# mirrors ads_engine.db.models.accounts.BUDGET_PERIODS
BUDGET_PERIODS: tuple[str, ...] = ("DAILY", "CUSTOM_PERIOD")
# mirrors ads_engine.db.models.facts.SEGMENT_FACETS
SEGMENT_FACETS: tuple[str, ...] = ("job_function", "seniority", "industry")
# mirrors ads_engine.db.models.facts.SEARCH_TERM_STATUSES
SEARCH_TERM_STATUSES: tuple[str, ...] = (
    "ADDED",
    "EXCLUDED",
    "ADDED_EXCLUDED",
    "NONE",
    "UNKNOWN",
    "UNSPECIFIED",
)
# mirrors ads_engine.db.models.snapshots.ENTITY_TYPES
ENTITY_TYPES: tuple[str, ...] = (
    "campaign",
    "campaign_group",
    "budget",
    "keyword",
    "creative",
    "conversion_action",
)
# mirrors ads_engine.db.models.crm.LEAD_MATCH_STATUSES
LEAD_MATCH_STATUSES: tuple[str, ...] = ("pending", "matched", "no_match", "no_email", "error")
# mirrors ads_engine.db.models.crm.CONTACT_READ_STATUSES
CONTACT_READ_STATUSES: tuple[str, ...] = ("ok", "error")
# mirrors ads_engine.db.models.crm.ASSOCIATION_FETCH_STATUSES
ASSOCIATION_FETCH_STATUSES: tuple[str, ...] = ("ok", "ok_empty", "partial", "error")
# mirrors ads_engine.db.models.crm.TARGET_READ_STATUSES
TARGET_READ_STATUSES: tuple[str, ...] = ("ok", "partial", "error")
# mirrors ads_engine.db.models.crm.CRM_OBJECT_TYPES
CRM_OBJECT_TYPES: tuple[str, ...] = ("contact", "meeting", "deal")
# mirrors ads_engine.db.models.decisions.SUBJECT_TYPES
SUBJECT_TYPES: tuple[str, ...] = ("campaign", "budget")
# mirrors ads_engine.db.models.decisions.DECISION_SHAPES
DECISION_SHAPES: tuple[str, ...] = ("own_budget", "shared_member", "budget")
# mirrors ads_engine.db.models.decisions.DECISION_ORIGINS
DECISION_ORIGINS: tuple[str, ...] = ("baseline", "user", "recommendation", "resolution")
# mirrors ads_engine.db.models.decisions.DISCREPANCY_KINDS
DISCREPANCY_KINDS: tuple[str, ...] = ("status", "budget", "untracked", "missing")
# mirrors ads_engine.db.models.decisions.DISCREPANCY_STATES
DISCREPANCY_STATES: tuple[str, ...] = ("open", "awaiting_platform", "resolved")
# mirrors ads_engine.db.models.decisions.DISCREPANCY_RESOLUTIONS
DISCREPANCY_RESOLUTIONS: tuple[str, ...] = (
    "platform_correct",
    "decision_correct",
    "adopt_platform",
    "record_intended",
    "acknowledge_removal",
)
# mirrors ads_engine.db.models.analysis.RUN_KINDS
RUN_KINDS: tuple[str, ...] = ("default", "custom")
# mirrors ads_engine.db.models.analysis.RUN_TRIGGERS
RUN_TRIGGERS: tuple[str, ...] = ("scheduled", "event", "manual")
# mirrors ads_engine.db.models.analysis.RUN_STATUSES
RUN_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed")
# mirrors ads_engine.db.models.analysis.FEEDBACK_KINDS
FEEDBACK_KINDS: tuple[str, ...] = ("dismiss", "snooze", "done")
# mirrors ads_engine.db.models.analysis.DATA_STATUSES
DATA_STATUSES: tuple[str, ...] = ("live", "simulated", "stale", "mixed")
# mirrors ads_engine.db.models.analysis.RECOMMENDATION_SECTIONS
RECOMMENDATION_SECTIONS: tuple[str, ...] = (
    "keywords",
    "campaigns",
    "budget",
    "creative",
    "white_papers",
)

# ---------------------------------------------------------------------------------------
# Hand-written CHECK texts, repeated verbatim from the models. Implicit concatenation keeps
# the rendered SQL identical to the model's string.
# ---------------------------------------------------------------------------------------

# mirrors ads_engine.db.models.ops._ROLE_SHAPE_SQL: a primary dataset has no parent; a
# companion is always a simulated HubSpot dataset hanging off its LinkedIn parent.
ROLE_SHAPE_SQL: str = (
    "(role = 'primary' AND parent_dataset_id IS NULL) OR "
    "(role = 'companion' AND source = 'hubspot' AND mode = 'simulated' "
    "AND parent_dataset_id IS NOT NULL)"
)
# mirrors ads_engine.db.models.ops._SIMULATED_WORLD_SQL: a live dataset names no simulated
# world; a simulated one names its world and seed together, or neither.
SIMULATED_WORLD_SQL: str = (
    "(mode = 'live' AND simulated_world IS NULL AND simulated_seed IS NULL) OR "
    "(mode = 'simulated' AND (simulated_world IS NULL) = (simulated_seed IS NULL))"
)
# mirrors ads_engine.db.models.accounts._AMOUNT_PAIR_SQL: a budget amount is known in both
# units or in neither.
AMOUNT_PAIR_SQL: str = "(amount IS NULL) = (amount_micros IS NULL)"
# mirrors the window_order CHECK of ads_engine.db.models.facts.FactSegmentWindow and
# ads_engine.db.models.analysis.AnalysisRun.
WINDOW_ORDER_SQL: str = "window_end >= window_start"
# mirrors ads_engine.db.models.crm._EMAIL_SHA256_HEX_SQL: a stored hash is exactly 64
# lower-case hex characters; NULL (no email) passes.
EMAIL_SHA256_HEX_SQL: str = "email_sha256 ~ '^[0-9a-f]{64}$'"
# mirrors ads_engine.db.models.crm._MATCHED_HAS_CONTACT_SQL: a matched row names its contact
# and the outcome of reading it.
MATCHED_HAS_CONTACT_SQL: str = (
    "status <> 'matched' OR (contact_id IS NOT NULL AND contact_read_status IS NOT NULL)"
)
# mirrors ads_engine.db.models.crm._OK_EMPTY_SHAPE_SQL: a genuine zero has no targets to read
# and none missing.
OK_EMPTY_SHAPE_SQL: str = (
    "status <> 'ok_empty' OR (target_read_status IS NULL AND cardinality(missing_target_ids) = 0)"
)
# mirrors ads_engine.db.models.crm._TARGET_READ_OK_COMPLETE_SQL: target reads are ok only
# when every associated id was returned.
TARGET_READ_OK_COMPLETE_SQL: str = (
    "target_read_status <> 'ok' OR cardinality(missing_target_ids) = 0"
)
# mirrors ads_engine.db.models.crm.LEAD_MATCH_CLAIMABLE_SQL, the partial-index predicate of
# ix_lead_match_claim: pending rows, and no_match or error rows with a retry scheduled.
LEAD_MATCH_CLAIMABLE_SQL: str = (
    "(lead_match.status = 'pending' OR (lead_match.status IN ('no_match', 'error')"
    " AND lead_match.next_attempt_at IS NOT NULL))"
)
# mirrors ads_engine.db.models.decisions._SHAPE_COLUMNS_SQL: the columns each decision shape
# may fill (AAP 0.5.2 "Decisions: shapes").
SHAPE_COLUMNS_SQL: str = (
    "(shape = 'own_budget' AND subject_type = 'campaign' AND status IS NOT NULL"
    " AND budget_id IS NULL)"
    " OR (shape = 'shared_member' AND subject_type = 'campaign' AND status IS NOT NULL"
    " AND budget_id IS NOT NULL AND daily_budget IS NULL AND total_budget IS NULL)"
    " OR (shape = 'budget' AND subject_type = 'budget' AND status IS NULL"
    " AND budget_id IS NULL AND (daily_budget IS NULL) <> (total_budget IS NULL))"
)

# Server defaults, the only five the schema uses.
NOW: str = "now()"
FALSE: str = "false"
ZERO: str = "0"
EMPTY_TEXT_ARRAY: str = "'{}'::text[]"
EMPTY_JSONB: str = "'{}'::jsonb"

# The typed entity keys every run table carries (AAP 0.5.2: nullable, ON DELETE SET NULL),
# as (column, referred table), in the models' column order.
RUN_ENTITY_KEYS: tuple[tuple[str, str], ...] = (
    ("campaign_id", "campaign"),
    ("budget_id", "budget"),
    ("ad_group_id", "ad_group"),
    ("keyword_id", "keyword"),
    ("creative_id", "creative"),
)

# ---------------------------------------------------------------------------------------
# Builders. Each returns schema objects carrying final (``op.f``) names; none touches the
# database by itself.
# ---------------------------------------------------------------------------------------


def _id() -> sa.Column[int]:
    """Return the ``IdPkMixin`` surrogate key: ``id bigint GENERATED BY DEFAULT AS IDENTITY``.

    Returns:
        The ``id`` column; its primary key is declared with :func:`_pk`.
    """
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False)


def _scope() -> tuple[sa.Column[str], sa.Column[int]]:
    """Return the ``DatasetScopedMixin`` columns, which follow ``id`` in every scoped table.

    Returns:
        ``source text NOT NULL`` and ``dataset_id bigint NOT NULL``, in that order. The
        matching constraints come from :func:`_source_check` and :func:`_dataset_fk`.
    """
    return (
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
    )


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    """Return the ``pk_<table>`` primary key on ``id``.

    Args:
        table: The table name.

    Returns:
        The named primary-key constraint.
    """
    return sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}"))


def _check(table: str, name: str, sqltext: str) -> sa.CheckConstraint:
    """Return the ``ck_<table>_<name>`` constraint with the given SQL text.

    Args:
        table: The table name.
        name: The model's constraint name before the convention is applied.
        sqltext: The condition, repeated verbatim from the model.

    Returns:
        The named check constraint.
    """
    return sa.CheckConstraint(sqltext, name=op.f(f"ck_{table}_{name}"))


def _in_check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    """Return ``ck_<table>_<column>_valid``, the text that ``ads_engine.db.base.check_in`` renders.

    The text is ``<column> IN ('v1', 'v2', ...)`` with the values in the order given. No
    enumerated column in this schema is a reserved word, so the column name is never quoted.

    Args:
        table: The table name.
        column: The enumerated column.
        values: The allowed values, in the model tuple's order.

    Returns:
        The named check constraint.
    """
    allowed = ", ".join(f"'{value}'" for value in values)
    return _check(table, f"{column}_valid", f"{column} IN ({allowed})")


def _source_check(table: str) -> sa.CheckConstraint:
    """Return ``ck_<table>_source_valid``, the ``source`` guard of every scoped table.

    Args:
        table: The table name.

    Returns:
        The check restricting ``source`` to :data:`SOURCES`.
    """
    return _in_check(table, "source", SOURCES)


def _non_negative(table: str, *columns: str, numeric: Sequence[str] = ()) -> sa.CheckConstraint:
    """Return ``ck_<table>_non_negative``, the text ``ads_engine.db.base.non_negative`` renders.

    Each column renders as ``<col> >= 0`` and each ``numeric`` column as ``<col> >= 0 AND
    <col> <> 'NaN'::numeric``, joined by ``AND`` in argument order. PostgreSQL sorts a
    ``numeric`` NaN above every number, so ``>= 0`` alone would admit it. A NULL metric,
    which means "unavailable", satisfies it. No metric column is a reserved word, so the
    column names are never quoted.

    Args:
        table: The table name.
        *columns: The metric columns, in the model's order.
        numeric: The columns among ``columns`` of type ``numeric``, as the model names them.

    Returns:
        The named check constraint.
    """
    conditions: list[str] = []
    for column in columns:
        conditions.append(f"{column} >= 0")
        if column in numeric:
            conditions.append(f"{column} <> 'NaN'::numeric")
    return _check(table, "non_negative", " AND ".join(conditions))


def _unique(table: str, *columns: str, name: str | None = None) -> sa.UniqueConstraint:
    """Return a unique constraint named by the convention or by the model's explicit name.

    Args:
        table: The table name.
        *columns: The constrained columns, in order.
        name: The model's explicit name; defaults to ``uq_<table>_<col1>_<col2>...``.

    Returns:
        The named unique constraint.
    """
    return sa.UniqueConstraint(*columns, name=op.f(name or f"uq_{table}_{'_'.join(columns)}"))


def _fk(
    table: str,
    columns: Sequence[str],
    referred: str,
    *,
    ondelete: str,
    referred_columns: Sequence[str] = ("id",),
) -> sa.ForeignKeyConstraint:
    """Return the ``fk_<table>_<first column>_<referred>`` foreign key.

    Args:
        table: The referencing table.
        columns: The referencing columns, in order.
        referred: The referenced table.
        ondelete: ``CASCADE`` or ``SET NULL``.
        referred_columns: The referenced columns, paired with ``columns``.

    Returns:
        The named foreign-key constraint.
    """
    return sa.ForeignKeyConstraint(
        list(columns),
        [f"{referred}.{column}" for column in referred_columns],
        name=op.f(f"fk_{table}_{columns[0]}_{referred}"),
        ondelete=ondelete,
    )


def _dataset_fk(table: str) -> sa.ForeignKeyConstraint:
    """Return ``fk_<table>_dataset_id_source_dataset``, ``ON DELETE CASCADE``.

    Args:
        table: The scoped table.

    Returns:
        The foreign key through which a dataset purge reaches the table's rows.
    """
    return _fk(table, ["dataset_id"], "source_dataset", ondelete="CASCADE")


def _same_dataset_fk(table: str, column: str, referred: str) -> sa.ForeignKeyConstraint:
    """Return a dataset-qualified foreign key on (``column``, ``dataset_id``).

    It references the parent's (``id``, ``dataset_id``), which the parent's unique
    (``dataset_id``, ``id``) backs, so the database rejects a reference to a parent of
    another dataset. It cascades with the parent.

    Args:
        table: The referencing table.
        column: The referencing id column.
        referred: The parent table.

    Returns:
        The composite foreign key, named after its first column.
    """
    return _fk(
        table,
        [column, "dataset_id"],
        referred,
        ondelete="CASCADE",
        referred_columns=("id", "dataset_id"),
    )


def _run_entity_columns() -> list[sa.Column[int]]:
    """Return the nullable typed entity keys of a run table, in :data:`RUN_ENTITY_KEYS` order.

    Returns:
        One ``bigint NULL`` column per entity key.
    """
    return [sa.Column(column, sa.BigInteger(), nullable=True) for column, _ in RUN_ENTITY_KEYS]


def _run_entity_fks(table: str) -> list[sa.ForeignKeyConstraint]:
    """Return the ``ON DELETE SET NULL`` foreign keys of a run table's typed entity keys.

    A purge nulls the key and the run row keeps its denormalised evidence (AAP 0.5.2).

    Args:
        table: The run table.

    Returns:
        One foreign key per entity key.
    """
    return [
        _fk(table, [column], referred, ondelete="SET NULL") for column, referred in RUN_ENTITY_KEYS
    ]


def _create_index(
    name: str,
    table: str,
    columns: Sequence[str | sa.TextClause],
    *,
    unique: bool = False,
    where: str | None = None,
) -> None:
    """Create one index with its final name, optionally unique and partial.

    Args:
        name: The index name, exactly as the model declares or the convention renders it.
        table: The indexed table.
        columns: Column names, or ``sa.text("<col> DESC")`` for a descending column.
        unique: Whether the index is unique.
        where: The partial-index predicate, repeated verbatim from the model.
    """
    if where is None:
        op.create_index(op.f(name), table, list(columns), unique=unique)
    else:
        op.create_index(
            op.f(name), table, list(columns), unique=unique, postgresql_where=sa.text(where)
        )


def _create_run_entity_indexes(table: str) -> None:
    """Create ``ix_<table>_<column>`` for each typed entity key of a run table.

    Args:
        table: The run table.
    """
    for column, _ in RUN_ENTITY_KEYS:
        _create_index(f"ix_{table}_{column}", table, [column])


# ---------------------------------------------------------------------------------------
# ops: datasets, freshness, the admin user, source connections and sync history
# ---------------------------------------------------------------------------------------


def _create_ops_tables() -> None:
    """Create the operational tables (``ads_engine.db.models.ops``).

    These are ``source_dataset``, the provenance every source-derived row points at, then
    ``dataset_freshness``, ``app_user``, ``source_connection``, ``sync_run`` and
    ``sync_request``.
    """
    op.create_table(
        "source_dataset",
        _id(),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("parent_dataset_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(NOW),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        # Resolved CRM stage bindings of a HubSpot dataset's portal; NULL for other sources.
        sa.Column("crm_bindings", postgresql.JSONB(), nullable=True),
        # The simulated world (MOCK_SCENARIO) and seed (MOCK_SEED) of a simulated dataset.
        sa.Column("simulated_world", sa.Text(), nullable=True),
        sa.Column("simulated_seed", sa.BigInteger(), nullable=True),
        _pk("source_dataset"),
        _source_check("source_dataset"),
        _in_check("source_dataset", "mode", MODES),
        _in_check("source_dataset", "state", DATASET_STATES),
        _in_check("source_dataset", "role", DATASET_ROLES),
        _check("source_dataset", "role_shape", ROLE_SHAPE_SQL),
        _check("source_dataset", "simulated_world", SIMULATED_WORLD_SQL),
        # A companion is purged with its LinkedIn parent.
        _fk("source_dataset", ["parent_dataset_id"], "source_dataset", ondelete="CASCADE"),
    )
    _create_index("ix_source_dataset_parent_dataset_id", "source_dataset", ["parent_dataset_id"])
    # At most one active primary dataset per source (AAP 0.5.2).
    _create_index(
        "uq_source_dataset_active_primary",
        "source_dataset",
        ["source"],
        unique=True,
        where="state = 'active' AND role = 'primary'",
    )

    op.create_table(
        "dataset_freshness",
        _id(),
        *_scope(),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_from", sa.Date(), nullable=True),
        sa.Column("covered_through", sa.Date(), nullable=True),
        sa.Column("completeness", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        # The unread work of an interrupted Google fetch, resumed by the dataset's next run.
        sa.Column("checkpoint", postgresql.JSONB(), nullable=True),
        _pk("dataset_freshness"),
        _source_check("dataset_freshness"),
        _in_check("dataset_freshness", "dataset", DATASETS),
        _in_check("dataset_freshness", "completeness", COMPLETENESS),
        _unique("dataset_freshness", "dataset_id", "dataset"),
        _dataset_fk("dataset_freshness"),
    )

    # The only ``email`` column in the schema: lead and contact emails are stored solely as
    # SHA-256 hashes.
    op.create_table(
        "app_user",
        _id(),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "session_generation", sa.BigInteger(), server_default=sa.text(ZERO), nullable=False
        ),
        _pk("app_user"),
        _unique("app_user", "email"),
    )

    op.create_table(
        "source_connection",
        _id(),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("active_dataset_id", sa.BigInteger(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        # The instant a failed call last marked the connection; last_attempt_at moves on
        # every attempt, so the return of a stale or error status to ok is judged by this.
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("calls_24h", sa.BigInteger(), server_default=sa.text(ZERO), nullable=False),
        sa.Column(
            "calls_by_minute",
            postgresql.JSONB(),
            server_default=sa.text(EMPTY_JSONB),
            nullable=False,
        ),
        sa.Column(
            "access_generation", sa.BigInteger(), server_default=sa.text(ZERO), nullable=False
        ),
        _pk("source_connection"),
        _source_check("source_connection"),
        _in_check("source_connection", "mode", MODES),
        _in_check("source_connection", "status", CONNECTION_STATUSES),
        _unique("source_connection", "source"),
        # SET NULL: a purge never removes the connection row itself.
        _fk("source_connection", ["active_dataset_id"], "source_dataset", ondelete="SET NULL"),
    )

    # Operational history, not provenance: no dataset_id, so it survives a purge.
    op.create_table(
        "sync_run",
        _id(),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows", sa.BigInteger(), nullable=True),
        sa.Column("completeness", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _pk("sync_run"),
        _source_check("sync_run"),
        _in_check("sync_run", "dataset", DATASETS),
        _in_check("sync_run", "status", SYNC_RUN_STATUSES),
        _in_check("sync_run", "completeness", COMPLETENESS),
    )
    _create_index("ix_sync_run_source_started", "sync_run", ["source", sa.text("started_at DESC")])

    op.create_table(
        "sync_request",
        _id(),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(NOW),
            nullable=False,
        ),
        sa.Column("picked_at", sa.DateTime(timezone=True), nullable=True),
        _pk("sync_request"),
        _source_check("sync_request"),
    )
    # One pending request per source (AAP 0.5.3).
    _create_index(
        "uq_sync_request_pending",
        "sync_request",
        ["source"],
        unique=True,
        where="picked_at IS NULL",
    )


# ---------------------------------------------------------------------------------------
# accounts: platform entities and the white paper registry
# ---------------------------------------------------------------------------------------


def _create_account_tables() -> None:
    """Create the platform entity and content-asset tables (``ads_engine.db.models.accounts``).

    Every foreign key between two rows of one dataset cascades, and each referencing column
    is indexed unless it leads another index, so a purge never scans a child table per
    parent row. Each such key is dataset-qualified (:func:`_same_dataset_fk`) against its
    parent's unique (``dataset_id``, ``id``), which every parent (``ad_account``,
    ``campaign_group``, ``budget``, ``campaign``, ``ad_group``, ``lead_form``) declares in
    its own ``create_table`` before its children are created. Raw platform values
    (``campaign.raw_status``, ``campaign_group.status``, ``ad_group.status``,
    ``keyword.status`` and ``match_type``, ``creative.intended_status`` and the
    ``conversion_action`` vocabulary columns) are open ``text`` with no ``CHECK``.
    """
    op.create_table(
        "ad_account",
        _id(),
        *_scope(),
        sa.Column("external_id", sa.Text(), nullable=False),
        # Nullable as in the model: Google omits descriptive_name for an unnamed account.
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=True),
        _pk("ad_account"),
        _source_check("ad_account"),
        _unique("ad_account", "dataset_id", "source", "external_id"),
        # Anchors the dataset-qualified (account_id, dataset_id) foreign keys of child rows.
        _unique("ad_account", "dataset_id", "id"),
        _dataset_fk("ad_account"),
    )

    op.create_table(
        "budget",
        _id(),
        *_scope(),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("amount_micros", sa.BigInteger(), nullable=True),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("explicitly_shared", sa.Boolean(), server_default=sa.text(FALSE), nullable=False),
        _pk("budget"),
        _source_check("budget"),
        _in_check("budget", "period", BUDGET_PERIODS),
        _check("budget", "amount_pair", AMOUNT_PAIR_SQL),
        _unique("budget", "dataset_id", "external_id"),
        # Anchors the dataset-qualified (budget_id, dataset_id) foreign keys of child rows.
        _unique("budget", "dataset_id", "id"),
        _dataset_fk("budget"),
    )

    op.create_table(
        "conversion_action",
        _id(),
        *_scope(),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "include_in_conversions_metric",
            sa.Boolean(),
            server_default=sa.text(FALSE),
            nullable=False,
        ),
        # Nullable as in the model: NULL when Google omits the value, which leaves the
        # counting contract unknown (B4 withholds); never replaced by a guessed value.
        sa.Column("counting_type", sa.Text(), nullable=True),
        sa.Column("attribution_model", sa.Text(), nullable=True),
        _pk("conversion_action"),
        _source_check("conversion_action"),
        _unique("conversion_action", "dataset_id", "external_id"),
        _dataset_fk("conversion_action"),
    )

    op.create_table(
        "lead_form",
        _id(),
        *_scope(),
        sa.Column("canonical_urn", sa.Text(), nullable=False),
        # Nullable as in the model: a form first seen through a response may lack both.
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("current_version", sa.BigInteger(), nullable=True),
        _pk("lead_form"),
        _source_check("lead_form"),
        _unique("lead_form", "dataset_id", "canonical_urn"),
        # Anchors the dataset-qualified (lead_form_id, dataset_id) foreign keys of child rows.
        _unique("lead_form", "dataset_id", "id"),
        _dataset_fk("lead_form"),
    )

    # Config-derived (config/content_assets.yaml), not dataset-scoped.
    op.create_table(
        "content_asset",
        _id(),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        _pk("content_asset"),
        _unique("content_asset", "slug"),
    )

    op.create_table(
        "campaign_group",
        _id(),
        *_scope(),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("daily_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("total_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("run_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_end", sa.DateTime(timezone=True), nullable=True),
        _pk("campaign_group"),
        _source_check("campaign_group"),
        _unique("campaign_group", "dataset_id", "account_id", "external_id"),
        # Anchors the dataset-qualified (campaign_group_id, dataset_id) foreign keys of child rows.
        _unique("campaign_group", "dataset_id", "id"),
        _dataset_fk("campaign_group"),
        _same_dataset_fk("campaign_group", "account_id", "ad_account"),
    )
    _create_index("ix_campaign_group_account_id", "campaign_group", ["account_id"])

    op.create_table(
        "campaign",
        _id(),
        *_scope(),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("raw_status", sa.Text(), nullable=False),
        sa.Column("objective_or_channel", sa.Text(), nullable=True),
        sa.Column("campaign_group_id", sa.BigInteger(), nullable=True),
        sa.Column("budget_id", sa.BigInteger(), nullable=True),
        sa.Column("daily_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("total_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("budget_period", sa.Text(), nullable=True),
        _pk("campaign"),
        _source_check("campaign"),
        _in_check("campaign", "status", CAMPAIGN_STATUSES),
        _in_check("campaign", "budget_period", BUDGET_PERIODS),
        _unique("campaign", "dataset_id", "account_id", "external_id"),
        # Anchors the dataset-qualified (campaign_id, dataset_id) foreign keys of child rows.
        _unique("campaign", "dataset_id", "id"),
        _dataset_fk("campaign"),
        _same_dataset_fk("campaign", "account_id", "ad_account"),
        # Nullable composite keys (MATCH SIMPLE): a NULL group or budget is not checked.
        _same_dataset_fk("campaign", "campaign_group_id", "campaign_group"),
        _same_dataset_fk("campaign", "budget_id", "budget"),
    )
    _create_index("ix_campaign_account_id", "campaign", ["account_id"])
    _create_index("ix_campaign_budget_id", "campaign", ["budget_id"])
    _create_index("ix_campaign_campaign_group_id", "campaign", ["campaign_group_id"])

    op.create_table(
        "ad_group",
        _id(),
        *_scope(),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _pk("ad_group"),
        _source_check("ad_group"),
        _unique("ad_group", "dataset_id", "campaign_id", "external_id"),
        _unique("ad_group", "dataset_id", "id"),
        _dataset_fk("ad_group"),
        _same_dataset_fk("ad_group", "campaign_id", "campaign"),
    )
    _create_index("ix_ad_group_campaign_id", "ad_group", ["campaign_id"])

    op.create_table(
        "creative",
        _id(),
        *_scope(),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("urn", sa.Text(), nullable=False),
        sa.Column("intended_status", sa.Text(), nullable=True),
        sa.Column("post_urn", sa.Text(), nullable=True),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("intro_text", sa.Text(), nullable=True),
        sa.Column("cta_label", sa.Text(), nullable=True),
        # The canonical urn:li:leadGenForm:{id} of the call-to-action form, if any.
        sa.Column("lead_form_urn", sa.Text(), nullable=True),
        _pk("creative"),
        _source_check("creative"),
        _unique("creative", "dataset_id", "urn"),
        _unique("creative", "dataset_id", "id"),
        _dataset_fk("creative"),
        _same_dataset_fk("creative", "campaign_id", "campaign"),
    )
    _create_index("ix_creative_campaign_id", "creative", ["campaign_id"])

    # The natural key (lead_form_id, version) is scoped through the parent form, so the
    # purge cascade finds rows through ix_lead_form_version_dataset_id instead.
    op.create_table(
        "lead_form_version",
        _id(),
        *_scope(),
        sa.Column("lead_form_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column(
            "email_question_ids",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(EMPTY_TEXT_ARRAY),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        _pk("lead_form_version"),
        _source_check("lead_form_version"),
        _unique("lead_form_version", "lead_form_id", "version"),
        _dataset_fk("lead_form_version"),
        _same_dataset_fk("lead_form_version", "lead_form_id", "lead_form"),
    )
    _create_index("ix_lead_form_version_dataset_id", "lead_form_version", ["dataset_id"])

    # Registry links hold URN strings with no foreign key to dataset rows, so every dataset
    # of a form or creative maps to the same asset.
    op.create_table(
        "content_asset_form",
        _id(),
        sa.Column("asset_id", sa.BigInteger(), nullable=False),
        sa.Column("lead_form_urn", sa.Text(), nullable=False),
        _pk("content_asset_form"),
        _fk("content_asset_form", ["asset_id"], "content_asset", ondelete="CASCADE"),
        _unique("content_asset_form", "lead_form_urn"),
    )

    op.create_table(
        "content_asset_creative",
        _id(),
        sa.Column("asset_id", sa.BigInteger(), nullable=False),
        sa.Column("creative_urn", sa.Text(), nullable=False),
        _pk("content_asset_creative"),
        _fk("content_asset_creative", ["asset_id"], "content_asset", ondelete="CASCADE"),
        _unique("content_asset_creative", "creative_urn"),
    )

    op.create_table(
        "keyword",
        _id(),
        *_scope(),
        sa.Column("ad_group_id", sa.BigInteger(), nullable=False),
        # Google criterion ids are unique only within an ad group (AAP 0.3.3).
        sa.Column("criterion_id", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("match_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("quality_score", sa.BigInteger(), nullable=True),
        _pk("keyword"),
        _source_check("keyword"),
        _unique("keyword", "dataset_id", "ad_group_id", "criterion_id"),
        _unique("keyword", "dataset_id", "id"),
        _dataset_fk("keyword"),
        _same_dataset_fk("keyword", "ad_group_id", "ad_group"),
    )
    _create_index("ix_keyword_ad_group_id", "keyword", ["ad_group_id"])


# ---------------------------------------------------------------------------------------
# facts: daily facts in each source's native day, and preset-window segments
# ---------------------------------------------------------------------------------------


def _create_fact_tables() -> None:
    """Create the fact tables (``ads_engine.db.models.facts``).

    Each references its entity through a dataset-qualified composite foreign key, so a fact
    can only point at an entity of its own dataset, and each guards its metrics with a
    ``non_negative`` check that a NULL ("unavailable") metric satisfies and a ``numeric``
    NaN does not. Besides its entity-leading indexes, each daily table has
    ``ix_<table>_dataset_date`` on (``dataset_id``, ``date``) and ``fact_segment_window`` has
    ``ix_fact_segment_window_dataset_window`` on (``dataset_id``, ``window_start``,
    ``window_end``), for window reads that leave the entity unconstrained.
    """
    op.create_table(
        "fact_campaign_daily",
        _id(),
        *_scope(),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False),
        sa.Column("clicks", sa.BigInteger(), nullable=False),
        sa.Column("conversions", sa.Numeric(18, 6), nullable=True),
        sa.Column("conversions_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("budget_lost_is", sa.Numeric(18, 6), nullable=True),
        sa.Column("impression_share", sa.Numeric(18, 6), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _pk("fact_campaign_daily"),
        _source_check("fact_campaign_daily"),
        _non_negative(
            "fact_campaign_daily",
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "budget_lost_is",
            "impression_share",
            numeric=(
                "cost",
                "conversions",
                "conversions_value",
                "budget_lost_is",
                "impression_share",
            ),
        ),
        _same_dataset_fk("fact_campaign_daily", "campaign_id", "campaign"),
        _unique("fact_campaign_daily", "dataset_id", "campaign_id", "date"),
        _dataset_fk("fact_campaign_daily"),
    )
    _create_index(
        "ix_fact_campaign_daily_campaign_date", "fact_campaign_daily", ["campaign_id", "date"]
    )
    _create_index(
        "ix_fact_campaign_daily_dataset_date", "fact_campaign_daily", ["dataset_id", "date"]
    )

    op.create_table(
        "fact_creative_daily",
        _id(),
        *_scope(),
        sa.Column("creative_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        # Every metric is nullable, as in the model: a LinkedIn analytics element may omit
        # any metric, and NULL means unavailable, never zero.
        sa.Column("cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("conversions", sa.Numeric(18, 6), nullable=True),
        sa.Column("conversions_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("form_opens", sa.BigInteger(), nullable=True),
        sa.Column("leads", sa.BigInteger(), nullable=True),
        sa.Column("landing_page_clicks", sa.BigInteger(), nullable=True),
        sa.Column("website_conversions", sa.BigInteger(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _pk("fact_creative_daily"),
        _source_check("fact_creative_daily"),
        _non_negative(
            "fact_creative_daily",
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "form_opens",
            "leads",
            "landing_page_clicks",
            "website_conversions",
            numeric=("cost", "conversions", "conversions_value"),
        ),
        _same_dataset_fk("fact_creative_daily", "creative_id", "creative"),
        _unique("fact_creative_daily", "dataset_id", "creative_id", "date"),
        _dataset_fk("fact_creative_daily"),
    )
    _create_index(
        "ix_fact_creative_daily_creative_date", "fact_creative_daily", ["creative_id", "date"]
    )
    _create_index(
        "ix_fact_creative_daily_dataset_date", "fact_creative_daily", ["dataset_id", "date"]
    )

    op.create_table(
        "fact_keyword_daily",
        _id(),
        *_scope(),
        sa.Column("keyword_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False),
        sa.Column("clicks", sa.BigInteger(), nullable=False),
        sa.Column("conversions", sa.Numeric(18, 6), nullable=True),
        sa.Column("conversions_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("historical_quality_score", sa.BigInteger(), nullable=True),
        sa.Column("average_cpc", sa.Numeric(18, 6), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _pk("fact_keyword_daily"),
        _source_check("fact_keyword_daily"),
        _non_negative(
            "fact_keyword_daily",
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            "historical_quality_score",
            "average_cpc",
            numeric=("cost", "conversions", "conversions_value", "average_cpc"),
        ),
        _same_dataset_fk("fact_keyword_daily", "keyword_id", "keyword"),
        _unique("fact_keyword_daily", "dataset_id", "keyword_id", "date"),
        _dataset_fk("fact_keyword_daily"),
    )
    _create_index(
        "ix_fact_keyword_daily_keyword_date", "fact_keyword_daily", ["keyword_id", "date"]
    )
    _create_index(
        "ix_fact_keyword_daily_dataset_date", "fact_keyword_daily", ["dataset_id", "date"]
    )

    op.create_table(
        "fact_search_term_daily",
        _id(),
        *_scope(),
        sa.Column("ad_group_id", sa.BigInteger(), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("search_term", sa.Text(), nullable=False),
        sa.Column("term_status", sa.Text(), nullable=False),
        # '' when Google returns no keyword segment, so the natural key stays NOT NULL.
        sa.Column("matched_criterion_id", sa.Text(), nullable=False),
        sa.Column("matched_keyword_text", sa.Text(), nullable=True),
        sa.Column("matched_match_type", sa.Text(), nullable=True),
        sa.Column("cost", sa.Numeric(18, 6), nullable=False),
        sa.Column("impressions", sa.BigInteger(), nullable=False),
        sa.Column("clicks", sa.BigInteger(), nullable=False),
        sa.Column("conversions", sa.Numeric(18, 6), nullable=True),
        sa.Column("conversions_value", sa.Numeric(18, 6), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _pk("fact_search_term_daily"),
        _source_check("fact_search_term_daily"),
        _in_check("fact_search_term_daily", "term_status", SEARCH_TERM_STATUSES),
        _non_negative(
            "fact_search_term_daily",
            "cost",
            "impressions",
            "clicks",
            "conversions",
            "conversions_value",
            numeric=("cost", "conversions", "conversions_value"),
        ),
        _same_dataset_fk("fact_search_term_daily", "ad_group_id", "ad_group"),
        _same_dataset_fk("fact_search_term_daily", "campaign_id", "campaign"),
        _unique(
            "fact_search_term_daily",
            "dataset_id",
            "ad_group_id",
            "matched_criterion_id",
            "search_term",
            "date",
            name="uq_fact_search_term_daily_natural",
        ),
        _dataset_fk("fact_search_term_daily"),
    )
    _create_index(
        "ix_fact_search_term_daily_ad_group_date",
        "fact_search_term_daily",
        ["ad_group_id", "date"],
    )
    _create_index(
        "ix_fact_search_term_daily_campaign_date",
        "fact_search_term_daily",
        ["campaign_id", "date"],
    )
    _create_index(
        "ix_fact_search_term_daily_dataset_date",
        "fact_search_term_daily",
        ["dataset_id", "date"],
    )

    op.create_table(
        "fact_segment_window",
        _id(),
        *_scope(),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("facet", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        # NULL means the pivot does not return the metric (unavailable), never zero.
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.BigInteger(), nullable=True),
        sa.Column("cost", sa.Numeric(18, 6), nullable=True),
        sa.Column("leads", sa.BigInteger(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _pk("fact_segment_window"),
        _source_check("fact_segment_window"),
        _in_check("fact_segment_window", "facet", SEGMENT_FACETS),
        _non_negative(
            "fact_segment_window", "impressions", "clicks", "cost", "leads", numeric=("cost",)
        ),
        _check("fact_segment_window", "window_order", WINDOW_ORDER_SQL),
        _same_dataset_fk("fact_segment_window", "campaign_id", "campaign"),
        _unique(
            "fact_segment_window",
            "dataset_id",
            "campaign_id",
            "facet",
            "value",
            "window_start",
            "window_end",
            name="uq_fact_segment_window_natural",
        ),
        _dataset_fk("fact_segment_window"),
    )
    _create_index("ix_fact_segment_window_campaign_id", "fact_segment_window", ["campaign_id"])
    _create_index(
        "ix_fact_segment_window_dataset_window",
        "fact_segment_window",
        ["dataset_id", "window_start", "window_end"],
    )


# ---------------------------------------------------------------------------------------
# snapshots: append-only entity observations
# ---------------------------------------------------------------------------------------


def _create_snapshot_tables() -> None:
    """Create ``entity_snapshot`` (``ads_engine.db.models.snapshots``).

    ``entity_id`` is polymorphic over :data:`ENTITY_TYPES` and so carries no foreign key;
    ``budget_id`` is the Google budget of the snapshot's own dataset a campaign drew from at
    ``captured_at``, a dataset-qualified key that cascades. The unique (``dataset_id``,
    ``id``) anchors ``discrepancy.snapshot_id``'s dataset-qualified key. The table has no
    natural key, so the purge cascade finds rows through ``ix_entity_snapshot_dataset_id``.
    ``ix_entity_snapshot_dataset_type_captured`` serves the analysis window's observation
    range and ``ix_entity_snapshot_entity_captured`` each entity's history.
    """
    op.create_table(
        "entity_snapshot",
        _id(),
        *_scope(),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("budget_daily", sa.Numeric(18, 6), nullable=True),
        sa.Column("budget_total", sa.Numeric(18, 6), nullable=True),
        sa.Column("budget_amount_micros", sa.BigInteger(), nullable=True),
        sa.Column("budget_id", sa.BigInteger(), nullable=True),
        sa.Column("quality_score", sa.BigInteger(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text(EMPTY_JSONB),
            nullable=False,
        ),
        _pk("entity_snapshot"),
        _source_check("entity_snapshot"),
        _in_check("entity_snapshot", "entity_type", ENTITY_TYPES),
        # Anchors the dataset-qualified (snapshot_id, dataset_id) foreign keys of child rows.
        _unique("entity_snapshot", "dataset_id", "id"),
        _dataset_fk("entity_snapshot"),
        # Nullable composite key (MATCH SIMPLE): a NULL budget_id is not checked.
        _same_dataset_fk("entity_snapshot", "budget_id", "budget"),
    )
    _create_index("ix_entity_snapshot_budget_id", "entity_snapshot", ["budget_id"])
    _create_index("ix_entity_snapshot_dataset_id", "entity_snapshot", ["dataset_id"])
    # The analysis window's entity-free observation range per dataset and entity type.
    _create_index(
        "ix_entity_snapshot_dataset_type_captured",
        "entity_snapshot",
        ["dataset_id", "entity_type", "captured_at"],
    )
    _create_index(
        "ix_entity_snapshot_entity_captured",
        "entity_snapshot",
        ["entity_type", "entity_id", sa.text("captured_at DESC")],
    )


# ---------------------------------------------------------------------------------------
# crm: LinkedIn leads, HubSpot records and the per-CRM-dataset matching state
# ---------------------------------------------------------------------------------------


def _create_crm_tables() -> None:
    """Create the lead and CRM tables (``ads_engine.db.models.crm``).

    No table holds a plaintext email: leads and contacts keep only the SHA-256 hex digest of
    the normalised email (AAP 0.3.2). HubSpot ids in ``crm_association`` and
    ``crm_association_fetch`` are text with no foreign key, because an associated record may
    not have been read; such a record is unknown, never absent (AAP 0.3.3).
    """
    op.create_table(
        "lead",
        _id(),
        *_scope(),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("campaign_id", sa.BigInteger(), nullable=False),
        sa.Column("creative_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_form_urn", sa.Text(), nullable=False),
        sa.Column("lead_form_version", sa.BigInteger(), nullable=False),
        sa.Column("email_sha256", sa.CHAR(64), nullable=True),
        _pk("lead"),
        _source_check("lead"),
        _check("lead", "email_sha256_hex", EMAIL_SHA256_HEX_SQL),
        # Dataset-qualified: the campaign and the creative must belong to the lead's own
        # dataset. A NULL creative_id is not checked (MATCH SIMPLE).
        _same_dataset_fk("lead", "campaign_id", "campaign"),
        _same_dataset_fk("lead", "creative_id", "creative"),
        _unique("lead", "dataset_id", "external_id"),
        _dataset_fk("lead"),
    )
    _create_index("ix_lead_campaign_id", "lead", ["campaign_id"])
    _create_index("ix_lead_creative_id", "lead", ["creative_id"])
    _create_index("ix_lead_dataset_submitted", "lead", ["dataset_id", "submitted_at"])

    op.create_table(
        "crm_contact",
        _id(),
        *_scope(),
        sa.Column("hs_id", sa.Text(), nullable=False),
        sa.Column("email_sha256", sa.CHAR(64), nullable=True),
        sa.Column("lifecycle_stage", sa.Text(), nullable=True),
        sa.Column("mql_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ql_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_campaign_ref", sa.Text(), nullable=True),
        sa.Column("google_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        _pk("crm_contact"),
        _source_check("crm_contact"),
        _unique("crm_contact", "dataset_id", "hs_id"),
        # Anchors lead_match's dataset-qualified (contact_id, crm_dataset_id) foreign key.
        _unique("crm_contact", "dataset_id", "id"),
        _dataset_fk("crm_contact"),
    )
    _create_index("ix_crm_contact_dataset_email", "crm_contact", ["dataset_id", "email_sha256"])
    # Google-touch discovery: a dataset's contacts with a campaign ref, by touch-instant range.
    _create_index(
        "ix_crm_contact_dataset_google_touch",
        "crm_contact",
        ["dataset_id", "google_touch_at"],
        where="google_campaign_ref IS NOT NULL",
    )

    op.create_table(
        "crm_meeting",
        _id(),
        *_scope(),
        sa.Column("hs_id", sa.Text(), nullable=False),
        # HubSpot's hs_createdate, the labelled booking-time proxy; no server default.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meeting_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("meeting_source", sa.Text(), nullable=True),
        _pk("crm_meeting"),
        _source_check("crm_meeting"),
        _unique("crm_meeting", "dataset_id", "hs_id"),
        _dataset_fk("crm_meeting"),
    )

    op.create_table(
        "crm_deal",
        _id(),
        *_scope(),
        sa.Column("hs_id", sa.Text(), nullable=False),
        # HubSpot's createdate, the event the causal check compares; no server default.
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("pipeline", sa.Text(), nullable=True),
        sa.Column("amount", sa.Numeric(18, 6), nullable=True),
        _pk("crm_deal"),
        _source_check("crm_deal"),
        _unique("crm_deal", "dataset_id", "hs_id"),
        _dataset_fk("crm_deal"),
    )
    _create_index("ix_crm_deal_dataset_created", "crm_deal", ["dataset_id", "created_at"])

    op.create_table(
        "crm_association",
        _id(),
        *_scope(),
        sa.Column("from_type", sa.Text(), nullable=False),
        sa.Column("from_id", sa.Text(), nullable=False),
        sa.Column("to_type", sa.Text(), nullable=False),
        sa.Column("to_id", sa.Text(), nullable=False),
        _pk("crm_association"),
        _source_check("crm_association"),
        _in_check("crm_association", "from_type", CRM_OBJECT_TYPES),
        _in_check("crm_association", "to_type", CRM_OBJECT_TYPES),
        _unique(
            "crm_association",
            "dataset_id",
            "from_type",
            "from_id",
            "to_type",
            "to_id",
            name="uq_crm_association_tuple",
        ),
        _dataset_fk("crm_association"),
    )
    _create_index(
        "ix_crm_association_target", "crm_association", ["dataset_id", "to_type", "to_id"]
    )

    op.create_table(
        "crm_association_fetch",
        _id(),
        *_scope(),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("to_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("target_read_status", sa.Text(), nullable=True),
        sa.Column(
            "missing_target_ids",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(EMPTY_TEXT_ARRAY),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        _pk("crm_association_fetch"),
        _source_check("crm_association_fetch"),
        _in_check("crm_association_fetch", "object_type", CRM_OBJECT_TYPES),
        _in_check("crm_association_fetch", "to_type", CRM_OBJECT_TYPES),
        _in_check("crm_association_fetch", "status", ASSOCIATION_FETCH_STATUSES),
        _in_check("crm_association_fetch", "target_read_status", TARGET_READ_STATUSES),
        _check("crm_association_fetch", "ok_empty_shape", OK_EMPTY_SHAPE_SQL),
        _check("crm_association_fetch", "target_read_ok_complete", TARGET_READ_OK_COMPLETE_SQL),
        _unique(
            "crm_association_fetch",
            "dataset_id",
            "object_type",
            "object_id",
            "to_type",
            name="uq_crm_association_fetch_object",
        ),
        _dataset_fk("crm_association_fetch"),
    )

    # The only table that links two datasets: a LinkedIn lead and a contact of one CRM
    # dataset. Keyed (lead_id, crm_dataset_id), so a building CRM dataset stages its own
    # matches without touching the active ones (AAP 0.3.3); every link cascades.
    op.create_table(
        "lead_match",
        sa.Column("lead_id", sa.BigInteger(), nullable=False),
        sa.Column("crm_dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.BigInteger(), server_default=sa.text(ZERO), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contact_id", sa.BigInteger(), nullable=True),
        sa.Column("contact_read_status", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("lead_id", "crm_dataset_id", name=op.f("pk_lead_match")),
        _in_check("lead_match", "status", LEAD_MATCH_STATUSES),
        _in_check("lead_match", "contact_read_status", CONTACT_READ_STATUSES),
        _check("lead_match", "matched_has_contact", MATCHED_HAS_CONTACT_SQL),
        # The contact must belong to this match's CRM dataset; a NULL contact_id (no match
        # yet) is not checked.
        _fk(
            "lead_match",
            ["contact_id", "crm_dataset_id"],
            "crm_contact",
            ondelete="CASCADE",
            referred_columns=("id", "dataset_id"),
        ),
        _fk("lead_match", ["lead_id"], "lead", ondelete="CASCADE"),
        _fk("lead_match", ["crm_dataset_id"], "source_dataset", ondelete="CASCADE"),
    )
    _create_index("ix_lead_match_contact_id", "lead_match", ["contact_id"])
    _create_index(
        "ix_lead_match_due", "lead_match", ["crm_dataset_id", "status", "next_attempt_at"]
    )
    # The matching claim's due rows in its exact order; claimable rows only.
    _create_index(
        "ix_lead_match_claim",
        "lead_match",
        ["crm_dataset_id", sa.text("next_attempt_at NULLS FIRST"), "lead_id"],
        where=LEAD_MATCH_CLAIMABLE_SQL,
    )


# ---------------------------------------------------------------------------------------
# analysis: runs, requests, frozen run outputs and feedback
# ---------------------------------------------------------------------------------------


def _create_analysis_tables() -> None:
    """Create the analysis tables (``ads_engine.db.models.analysis``).

    None is dataset-scoped. ``analysis_run`` keeps ``source_day_boundaries``, ``data_modes``
    and ``dataset_ids`` in ``jsonb`` with no foreign key, and the run tables reference
    entities through nullable ``ON DELETE SET NULL`` keys, so frozen run history still
    renders after the datasets it read are purged (AAP 0.3.3). ``analysis_run`` also keeps
    the ``window.default_days`` and ``window.trusted_min_days`` it was computed with, so its
    run metadata and short-window banner stay frozen with the run (AAP 0.5.3).
    """
    op.create_table(
        "analysis_run",
        _id(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("source_day_boundaries", postgresql.JSONB(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("data_modes", postgresql.JSONB(), nullable=False),
        sa.Column("dataset_ids", postgresql.JSONB(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.Column("default_days", sa.BigInteger(), nullable=False),
        sa.Column("trusted_min_days", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        _pk("analysis_run"),
        _in_check("analysis_run", "kind", RUN_KINDS),
        _in_check("analysis_run", "trigger", RUN_TRIGGERS),
        _in_check("analysis_run", "status", RUN_STATUSES),
        _check("analysis_run", "window_order", WINDOW_ORDER_SQL),
    )
    _create_index(
        "ix_analysis_run_kind_status_finished",
        "analysis_run",
        ["kind", "status", "finished_at"],
    )
    # The newest runs of one kind, newest first (GET /runs).
    _create_index("ix_analysis_run_kind_id", "analysis_run", ["kind", sa.text("id DESC")])
    # The database-enforced second guard behind the ``analysis`` advisory lock: at most one
    # running run (AAP 0.5.3).
    _create_index(
        "uq_analysis_run_single_running",
        "analysis_run",
        ["status"],
        unique=True,
        where="status = 'running'",
    )

    op.create_table(
        "analysis_request",
        _id(),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(NOW),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("picked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        _pk("analysis_request"),
        _fk("analysis_request", ["run_id"], "analysis_run", ondelete="SET NULL"),
    )
    _create_index(
        "ix_analysis_request_pending",
        "analysis_request",
        ["requested_at"],
        where="picked_at IS NULL",
    )

    op.create_table(
        "recommendation",
        _id(),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        # A reserved word; SQLAlchemy quotes it as "group" in the DDL.
        sa.Column("group", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("rank", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "flags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(EMPTY_TEXT_ARRAY),
            nullable=False,
        ),
        sa.Column("data_status", sa.Text(), nullable=False),
        sa.Column("held", sa.Boolean(), server_default=sa.text(FALSE), nullable=False),
        sa.Column("held_reason", sa.Text(), nullable=True),
        sa.Column(
            "required_datasets",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text(EMPTY_TEXT_ARRAY),
            nullable=False,
        ),
        sa.Column("decision_subject", postgresql.JSONB(), nullable=True),
        *_run_entity_columns(),
        _pk("recommendation"),
        _in_check("recommendation", "section", RECOMMENDATION_SECTIONS),
        _in_check("recommendation", "data_status", DATA_STATUSES),
        _fk("recommendation", ["run_id"], "analysis_run", ondelete="CASCADE"),
        *_run_entity_fks("recommendation"),
    )
    _create_run_entity_indexes("recommendation")
    _create_index(
        "ix_recommendation_run_section_rank", "recommendation", ["run_id", "section", "rank"]
    )

    op.create_table(
        "run_finding",
        _id(),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("entity", postgresql.JSONB(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            server_default=sa.text(EMPTY_JSONB),
            nullable=False,
        ),
        *_run_entity_columns(),
        _pk("run_finding"),
        _in_check("run_finding", "section", RECOMMENDATION_SECTIONS),
        _fk("run_finding", ["run_id"], "analysis_run", ondelete="CASCADE"),
        *_run_entity_fks("run_finding"),
    )
    _create_run_entity_indexes("run_finding")
    _create_index("ix_run_finding_run_id", "run_finding", ["run_id"])

    op.create_table(
        "run_withheld",
        _id(),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("entity", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("observed", sa.Numeric(18, 6), nullable=True),
        sa.Column("required", sa.Numeric(18, 6), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        *_run_entity_columns(),
        _pk("run_withheld"),
        _in_check("run_withheld", "section", RECOMMENDATION_SECTIONS),
        _fk("run_withheld", ["run_id"], "analysis_run", ondelete="CASCADE"),
        *_run_entity_fks("run_withheld"),
    )
    _create_run_entity_indexes("run_withheld")
    _create_index("ix_run_withheld_run_id", "run_withheld", ["run_id"])

    op.create_table(
        "run_suppression",
        _id(),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        _pk("run_suppression"),
        _in_check("run_suppression", "section", RECOMMENDATION_SECTIONS),
        _unique(
            "run_suppression",
            "run_id",
            "section",
            "reason",
            name="uq_run_suppression_run_section_reason",
        ),
        _fk("run_suppression", ["run_id"], "analysis_run", ondelete="CASCADE"),
    )

    # Feedback references its recommendation only; later runs match on rule and entity.
    op.create_table(
        "recommendation_feedback",
        _id(),
        sa.Column("recommendation_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text(NOW),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        _pk("recommendation_feedback"),
        _in_check("recommendation_feedback", "kind", FEEDBACK_KINDS),
        _fk(
            "recommendation_feedback",
            ["recommendation_id"],
            "recommendation",
            ondelete="CASCADE",
        ),
    )
    _create_index(
        "ix_recommendation_feedback_rule_entity",
        "recommendation_feedback",
        ["rule_id", "entity_type", "entity_id", "until"],
    )
    # The runner's read of every row still in force is a range over ``until`` alone.
    _create_index("ix_recommendation_feedback_until", "recommendation_feedback", ["until"])


# ---------------------------------------------------------------------------------------
# decisions: the append-only decision log and its discrepancies
# ---------------------------------------------------------------------------------------


def _create_decision_tables() -> None:
    """Create ``decision`` and ``discrepancy`` (``ads_engine.db.models.decisions``).

    Both are dataset-scoped, so a simulated decision never becomes the expected state of a
    live campaign. ``subject_id`` points at a campaign or a budget according to
    ``subject_type`` and therefore carries no foreign key. ``ck_decision_shape_columns``
    fixes which columns each decision shape may fill (AAP 0.5.2). ``decision.budget_id``
    and ``discrepancy``'s ``snapshot_id`` and ``decision_id`` are dataset-qualified keys;
    ``decision`` declares the unique (``dataset_id``, ``id``) that ``decision_id``
    references.
    """
    op.create_table(
        "decision",
        _id(),
        *_scope(),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("shape", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("daily_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("total_budget", sa.Numeric(18, 6), nullable=True),
        sa.Column("budget_id", sa.BigInteger(), nullable=True),
        sa.Column("tombstone", sa.Boolean(), server_default=sa.text(FALSE), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("recommendation_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "patch",
            postgresql.JSONB(),
            server_default=sa.text(EMPTY_JSONB),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        _pk("decision"),
        _source_check("decision"),
        _in_check("decision", "subject_type", SUBJECT_TYPES),
        _in_check("decision", "shape", DECISION_SHAPES),
        _in_check("decision", "status", CAMPAIGN_STATUSES),
        _in_check("decision", "origin", DECISION_ORIGINS),
        _check("decision", "shape_columns", SHAPE_COLUMNS_SQL),
        # Anchors the dataset-qualified (decision_id, dataset_id) foreign keys of child rows.
        _unique("decision", "dataset_id", "id"),
        _dataset_fk("decision"),
        # Nullable composite key (MATCH SIMPLE): a NULL budget_id is not checked.
        _same_dataset_fk("decision", "budget_id", "budget"),
        # SET NULL: the decision log outlives the run whose item it was recorded from.
        _fk("decision", ["recommendation_id"], "recommendation", ondelete="SET NULL"),
    )
    _create_index("ix_decision_budget_id", "decision", ["budget_id"])
    _create_index("ix_decision_dataset_id", "decision", ["dataset_id"])
    # The decision log's newest-first page, probed per active dataset (GET /decisions).
    _create_index(
        "ix_decision_dataset_decided",
        "decision",
        ["dataset_id", sa.text("decided_at DESC"), sa.text("id DESC")],
    )
    _create_index(
        "ix_decision_subject_latest",
        "decision",
        ["subject_type", "subject_id", sa.text("decided_at DESC")],
    )

    op.create_table(
        "discrepancy",
        _id(),
        *_scope(),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("decided_value", postgresql.JSONB(), nullable=True),
        sa.Column("platform_value", postgresql.JSONB(), nullable=True),
        # NULL for a ``missing`` discrepancy, which has no snapshot to point at.
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("decision_id", sa.BigInteger(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        _pk("discrepancy"),
        _source_check("discrepancy"),
        _in_check("discrepancy", "subject_type", SUBJECT_TYPES),
        _in_check("discrepancy", "kind", DISCREPANCY_KINDS),
        _in_check("discrepancy", "state", DISCREPANCY_STATES),
        _in_check("discrepancy", "resolution", DISCREPANCY_RESOLUTIONS),
        _dataset_fk("discrepancy"),
        # Nullable composite keys (MATCH SIMPLE): a NULL snapshot or decision is not checked.
        _same_dataset_fk("discrepancy", "snapshot_id", "entity_snapshot"),
        _same_dataset_fk("discrepancy", "decision_id", "decision"),
    )
    _create_index("ix_discrepancy_dataset_id", "discrepancy", ["dataset_id"])
    _create_index("ix_discrepancy_decision_id", "discrepancy", ["decision_id"])
    _create_index("ix_discrepancy_snapshot_id", "discrepancy", ["snapshot_id"])
    _create_index("ix_discrepancy_state_detected", "discrepancy", ["state", "detected_at"])
    # At most one non-resolved discrepancy per subject and kind (AAP 0.5.2).
    _create_index(
        "uq_discrepancy_open_subject_kind",
        "discrepancy",
        ["subject_type", "subject_id", "kind"],
        unique=True,
        where="state <> 'resolved'",
    )


# ---------------------------------------------------------------------------------------
# Revision entry points
# ---------------------------------------------------------------------------------------

# Creation order: every table follows the tables it references, so no foreign key needs
# ALTER TABLE. ``downgrade`` drops in exactly the reverse order.
TABLES: tuple[str, ...] = (
    # ops
    "source_dataset",
    "dataset_freshness",
    "app_user",
    "source_connection",
    "sync_run",
    "sync_request",
    # accounts
    "ad_account",
    "budget",
    "conversion_action",
    "lead_form",
    "content_asset",
    "campaign_group",
    "campaign",
    "ad_group",
    "creative",
    "lead_form_version",
    "content_asset_form",
    "content_asset_creative",
    "keyword",
    # facts
    "fact_campaign_daily",
    "fact_creative_daily",
    "fact_keyword_daily",
    "fact_search_term_daily",
    "fact_segment_window",
    # snapshots
    "entity_snapshot",
    # crm
    "lead",
    "crm_contact",
    "crm_meeting",
    "crm_deal",
    "crm_association",
    "crm_association_fetch",
    "lead_match",
    # analysis
    "analysis_run",
    "analysis_request",
    "recommendation",
    "run_finding",
    "run_withheld",
    "run_suppression",
    "recommendation_feedback",
    # decisions
    "decision",
    "discrepancy",
)


def upgrade() -> None:
    """Create all 41 tables, with their constraints and indexes, in :data:`TABLES` order."""
    _create_ops_tables()
    _create_account_tables()
    _create_fact_tables()
    _create_snapshot_tables()
    _create_crm_tables()
    _create_analysis_tables()
    _create_decision_tables()


def downgrade() -> None:
    """Drop every table in exact reverse creation order, leaving only ``alembic_version``.

    Dropping a table drops its indexes and constraints with it, so no index is dropped
    separately.
    """
    for table in reversed(TABLES):
        op.drop_table(table)
