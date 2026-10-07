"""Metadata tests: every dataset-scoped table can find its rows by ``dataset_id`` through an index.

Purging a retired dataset is one ``DELETE FROM source_dataset WHERE id = :retired`` whose
``ON DELETE CASCADE`` foreign keys then look up each child table's rows by ``dataset_id``.
PostgreSQL does not index referencing columns itself, so every table carrying
:class:`~ads_engine.db.base.DatasetScopedMixin` needs an index, unique constraint or primary
key whose first column is ``dataset_id``. Most tables get one from their natural-key unique;
``lead_form_version`` (natural key ``(lead_form_id, version)``), the append-only
``entity_snapshot`` (no natural key) and the decision log's ``decision`` and ``discrepancy``
(inserted, never upserted, so no natural key) declare an explicit ``ix_<table>_dataset_id``.

The tables are collected from the model classes defined in the seven model modules imported
below, not from all of ``Base.metadata``, so a module added later is covered only once it is
imported here deliberately. DDL is compiled offline with the PostgreSQL dialect; no database
is needed.
"""

from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import Column, Index, PrimaryKeyConstraint, Table, UniqueConstraint
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.elements import UnaryExpression

from ads_engine.db.base import Base, DatasetScopedMixin
from ads_engine.db.models import accounts, analysis, crm, decisions, facts, ops, snapshots

# The project's own driver dialect, resolved through the typed URL API rather than the untyped
# ``postgresql.dialect()`` constructor, so this module also passes ``mypy --strict``.
_PG: Dialect = make_url("postgresql+psycopg://").get_dialect()()

_MODEL_MODULES: tuple[ModuleType, ...] = (ops, accounts, facts, snapshots, crm, decisions, analysis)

# Every table of the seven model modules that mixes in DatasetScopedMixin. Listed explicitly so
# the collection below cannot pass vacuously by finding fewer tables than the schema has.
_EXPECTED_DATASET_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        "dataset_freshness",
        "ad_account",
        "campaign_group",
        "campaign",
        "budget",
        "conversion_action",
        "ad_group",
        "keyword",
        "creative",
        "lead_form",
        "lead_form_version",
        "fact_campaign_daily",
        "fact_creative_daily",
        "fact_keyword_daily",
        "fact_search_term_daily",
        "fact_segment_window",
        "entity_snapshot",
        "lead",
        "crm_contact",
        "crm_meeting",
        "crm_deal",
        "crm_association",
        "crm_association_fetch",
        "decision",
        "discrepancy",
    }
)


def _dataset_scoped_tables() -> dict[str, Table]:
    """Return the tables of the model classes in the seven modules that use the mixin.

    A class counts only where it is defined, so a model re-exported by another module is
    collected once.
    """
    tables: dict[str, Table] = {}
    for module in _MODEL_MODULES:
        for candidate in vars(module).values():
            if (
                isinstance(candidate, type)
                and candidate.__module__ == module.__name__
                and issubclass(candidate, DatasetScopedMixin)
                and issubclass(candidate, Base)
            ):
                table = candidate.__table__
                assert isinstance(table, Table)
                tables[table.name] = table
    return tables


def _expression_column_name(expression: Any) -> str | None:
    """Return the column name an index expression is built on, unwrapping ``DESC``/``ASC``."""
    if isinstance(expression, UnaryExpression):
        expression = expression.element
    if isinstance(expression, Column):
        return str(expression.name)
    if isinstance(expression, str):
        return expression
    return None


def _index_leading_column(index: Index) -> str | None:
    """Return the name of the first column of ``index``, or ``None`` for an expression."""
    expressions = list(index.expressions)
    return _expression_column_name(expressions[0]) if expressions else None


def _key_columns(constraint: UniqueConstraint | PrimaryKeyConstraint) -> tuple[str, ...]:
    """Return the column names of a unique or primary-key constraint in declared order."""
    return tuple(str(column.name) for column in constraint.columns)


def _dataset_id_leading_structures(table: Table) -> list[str]:
    """Return the names of the indexes and keys of ``table`` that lead with ``dataset_id``."""
    names: list[str] = [
        str(index.name) for index in table.indexes if _index_leading_column(index) == "dataset_id"
    ]
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint | PrimaryKeyConstraint):
            columns = _key_columns(constraint)
            if columns and columns[0] == "dataset_id":
                names.append(str(constraint.name))
    return names


def _unique_constraints(table: Table) -> list[UniqueConstraint]:
    """Return the unique constraints declared on ``table``."""
    return [c for c in table.constraints if isinstance(c, UniqueConstraint)]


def _table(name: str) -> Table:
    """Return the dataset-scoped table ``name`` from the collected model tables."""
    return _dataset_scoped_tables()[name]


def test_collection_finds_every_dataset_scoped_table() -> None:
    """The seven model modules yield exactly the schema's dataset-scoped tables."""
    assert set(_dataset_scoped_tables()) == _EXPECTED_DATASET_SCOPED_TABLES


def test_every_dataset_scoped_table_has_a_dataset_id_leading_index() -> None:
    """Each mixin table can serve the purge cascade's ``dataset_id`` lookup from an index."""
    missing = sorted(
        name
        for name, table in _dataset_scoped_tables().items()
        if not _dataset_id_leading_structures(table)
    )
    assert missing == []


@pytest.mark.parametrize(
    ("table_name", "index_name"),
    [
        ("lead_form_version", "ix_lead_form_version_dataset_id"),
        ("entity_snapshot", "ix_entity_snapshot_dataset_id"),
        ("decision", "ix_decision_dataset_id"),
        ("discrepancy", "ix_discrepancy_dataset_id"),
    ],
)
def test_explicit_dataset_id_index_is_a_plain_single_column_index(
    table_name: str, index_name: str
) -> None:
    """Tables without a dataset-leading natural key get a non-unique ``dataset_id`` index.

    The name is the one the ``ix_%(column_0_label)s`` convention would give the index.
    """
    table = _table(table_name)
    indexes = {str(index.name): index for index in table.indexes}
    assert index_name in indexes
    index = indexes[index_name]
    assert index_name == f"ix_{table_name}_dataset_id"
    assert tuple(str(column.name) for column in index.columns) == ("dataset_id",)
    assert index.unique is False


@pytest.mark.parametrize(
    ("table_name", "index_name", "ddl"),
    [
        (
            "lead_form_version",
            "ix_lead_form_version_dataset_id",
            "CREATE INDEX ix_lead_form_version_dataset_id ON lead_form_version (dataset_id)",
        ),
        (
            "entity_snapshot",
            "ix_entity_snapshot_dataset_id",
            "CREATE INDEX ix_entity_snapshot_dataset_id ON entity_snapshot (dataset_id)",
        ),
        (
            "decision",
            "ix_decision_dataset_id",
            "CREATE INDEX ix_decision_dataset_id ON decision (dataset_id)",
        ),
        (
            "discrepancy",
            "ix_discrepancy_dataset_id",
            "CREATE INDEX ix_discrepancy_dataset_id ON discrepancy (dataset_id)",
        ),
    ],
)
def test_explicit_dataset_id_index_renders_the_exact_postgresql_ddl(
    table_name: str, index_name: str, ddl: str
) -> None:
    """``CREATE INDEX`` for each new index compiles to the DDL the migration must mirror."""
    index = next(i for i in _table(table_name).indexes if i.name == index_name)
    assert str(CreateIndex(index).compile(dialect=_PG)).strip() == ddl


def test_lead_form_version_keeps_its_parent_scoped_natural_key() -> None:
    """``lead_form_version`` keeps unique ``(lead_form_id, version)`` as its natural key."""
    table = _table("lead_form_version")
    uniques = {_key_columns(c): str(c.name) for c in _unique_constraints(table)}
    assert uniques == {
        ("lead_form_id", "version"): "uq_lead_form_version_lead_form_id_version",
    }
    assert accounts.LeadFormVersion.__natural_key__ == ("lead_form_id", "version")


def test_lead_form_version_adds_no_index_leading_with_lead_form_id() -> None:
    """The natural-key unique already serves the ``lead_form`` cascade; nothing duplicates it."""
    table = _table("lead_form_version")
    leading_lead_form_id = [
        str(index.name) for index in table.indexes if _index_leading_column(index) == "lead_form_id"
    ]
    assert leading_lead_form_id == []
    assert {str(index.name) for index in table.indexes} == {"ix_lead_form_version_dataset_id"}


def test_entity_snapshot_keeps_its_history_and_budget_indexes() -> None:
    """The newest-per-entity index and the budget foreign-key index are unchanged."""
    table = _table("entity_snapshot")
    indexes = {str(index.name): index for index in table.indexes}
    assert set(indexes) == {
        "ix_entity_snapshot_entity_captured",
        "ix_entity_snapshot_budget_id",
        "ix_entity_snapshot_dataset_id",
    }
    rendered = {
        name: str(CreateIndex(index).compile(dialect=_PG)).strip()
        for name, index in indexes.items()
    }
    assert rendered["ix_entity_snapshot_entity_captured"] == (
        "CREATE INDEX ix_entity_snapshot_entity_captured ON entity_snapshot "
        "(entity_type, entity_id, captured_at DESC)"
    )
    assert rendered["ix_entity_snapshot_budget_id"] == (
        "CREATE INDEX ix_entity_snapshot_budget_id ON entity_snapshot (budget_id)"
    )
    assert _unique_constraints(table) == []
