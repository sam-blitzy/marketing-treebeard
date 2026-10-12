"""Metadata tests: the window indexes of the fact tables.

Window aggregation in ``ads_engine.analysis.metrics`` reads each daily fact table by
``dataset_id`` and a ``date`` range with the entity unconstrained, and C3a reads
``fact_segment_window`` by ``dataset_id`` and one exact window. The natural-key uniques and
the (entity, ``date``) indexes place the entity before the dates, so neither serves those
reads as one contiguous range. Each daily table therefore has ``ix_<table>_dataset_date`` on
(``dataset_id``, ``date``), and ``fact_segment_window`` has
``ix_fact_segment_window_dataset_window`` on (``dataset_id``, ``window_start``,
``window_end``). The entity-date indexes (AAP 0.2.1: "Daily facts are indexed by entity and
date") and the natural keys (AAP 0.5.2) are unchanged.
``backend/migrations/versions/0001_initial_schema.py`` must mirror every index here.

DDL is compiled offline with the PostgreSQL dialect; no database is needed.
"""

import pytest
from sqlalchemy import Index, Table, UniqueConstraint
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.schema import CreateIndex

from ads_engine.db.base import Base
from ads_engine.db.models import facts

# The project's own driver dialect, resolved through the typed URL API rather than the untyped
# ``postgresql.dialect()`` constructor, so this module also passes ``mypy --strict``.
_PG: Dialect = make_url("postgresql+psycopg://").get_dialect()()

# PostgreSQL truncates identifiers longer than this (NAMEDATALEN - 1).
_MAX_IDENTIFIER_LENGTH = 63

_MODELS: dict[str, type[Base]] = {
    "fact_campaign_daily": facts.FactCampaignDaily,
    "fact_creative_daily": facts.FactCreativeDaily,
    "fact_keyword_daily": facts.FactKeywordDaily,
    "fact_search_term_daily": facts.FactSearchTermDaily,
    "fact_segment_window": facts.FactSegmentWindow,
}

# The dataset-leading window index of each fact table and its exact PostgreSQL DDL.
_WINDOW_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "fact_campaign_daily",
        "ix_fact_campaign_daily_dataset_date",
        "CREATE INDEX ix_fact_campaign_daily_dataset_date ON fact_campaign_daily "
        "(dataset_id, date)",
    ),
    (
        "fact_creative_daily",
        "ix_fact_creative_daily_dataset_date",
        "CREATE INDEX ix_fact_creative_daily_dataset_date ON fact_creative_daily "
        "(dataset_id, date)",
    ),
    (
        "fact_keyword_daily",
        "ix_fact_keyword_daily_dataset_date",
        "CREATE INDEX ix_fact_keyword_daily_dataset_date ON fact_keyword_daily (dataset_id, date)",
    ),
    (
        "fact_search_term_daily",
        "ix_fact_search_term_daily_dataset_date",
        "CREATE INDEX ix_fact_search_term_daily_dataset_date ON fact_search_term_daily "
        "(dataset_id, date)",
    ),
    (
        "fact_segment_window",
        "ix_fact_segment_window_dataset_window",
        "CREATE INDEX ix_fact_segment_window_dataset_window ON fact_segment_window "
        "(dataset_id, window_start, window_end)",
    ),
)

# The entity-leading indexes that existed before the window indexes, with their exact DDL.
_ENTITY_INDEXES: tuple[tuple[str, str, str], ...] = (
    (
        "fact_campaign_daily",
        "ix_fact_campaign_daily_campaign_date",
        "CREATE INDEX ix_fact_campaign_daily_campaign_date ON fact_campaign_daily "
        "(campaign_id, date)",
    ),
    (
        "fact_creative_daily",
        "ix_fact_creative_daily_creative_date",
        "CREATE INDEX ix_fact_creative_daily_creative_date ON fact_creative_daily "
        "(creative_id, date)",
    ),
    (
        "fact_keyword_daily",
        "ix_fact_keyword_daily_keyword_date",
        "CREATE INDEX ix_fact_keyword_daily_keyword_date ON fact_keyword_daily (keyword_id, date)",
    ),
    (
        "fact_search_term_daily",
        "ix_fact_search_term_daily_ad_group_date",
        "CREATE INDEX ix_fact_search_term_daily_ad_group_date ON fact_search_term_daily "
        "(ad_group_id, date)",
    ),
    (
        "fact_search_term_daily",
        "ix_fact_search_term_daily_campaign_date",
        "CREATE INDEX ix_fact_search_term_daily_campaign_date ON fact_search_term_daily "
        "(campaign_id, date)",
    ),
    (
        "fact_segment_window",
        "ix_fact_segment_window_campaign_id",
        "CREATE INDEX ix_fact_segment_window_campaign_id ON fact_segment_window (campaign_id)",
    ),
)

# Each fact table's complete natural key: the unique constraint name and its columns.
_NATURAL_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "fact_campaign_daily": (
        "uq_fact_campaign_daily_dataset_id_campaign_id_date",
        ("dataset_id", "campaign_id", "date"),
    ),
    "fact_creative_daily": (
        "uq_fact_creative_daily_dataset_id_creative_id_date",
        ("dataset_id", "creative_id", "date"),
    ),
    "fact_keyword_daily": (
        "uq_fact_keyword_daily_dataset_id_keyword_id_date",
        ("dataset_id", "keyword_id", "date"),
    ),
    "fact_search_term_daily": (
        "uq_fact_search_term_daily_natural",
        ("dataset_id", "ad_group_id", "matched_criterion_id", "search_term", "date"),
    ),
    "fact_segment_window": (
        "uq_fact_segment_window_natural",
        ("dataset_id", "campaign_id", "facet", "value", "window_start", "window_end"),
    ),
}

# The ``__natural_key__`` each model declares, the ``ON CONFLICT`` target of its upsert.
_DECLARED_NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "fact_campaign_daily": facts.FactCampaignDaily.__natural_key__,
    "fact_creative_daily": facts.FactCreativeDaily.__natural_key__,
    "fact_keyword_daily": facts.FactKeywordDaily.__natural_key__,
    "fact_search_term_daily": facts.FactSearchTermDaily.__natural_key__,
    "fact_segment_window": facts.FactSegmentWindow.__natural_key__,
}


def _table(name: str) -> Table:
    """Return the ``Table`` the fact model ``name`` maps."""
    table = _MODELS[name].__table__
    assert isinstance(table, Table)
    return table


def _index(table_name: str, index_name: str) -> Index:
    """Return the index ``index_name`` declared on the fact table ``table_name``."""
    indexes = {str(index.name): index for index in _table(table_name).indexes}
    assert index_name in indexes, f"{table_name} declares no index {index_name}"
    return indexes[index_name]


def _ddl(index: Index) -> str:
    """Compile ``CREATE INDEX`` for ``index`` with the PostgreSQL dialect."""
    return str(CreateIndex(index).compile(dialect=_PG)).strip()


@pytest.mark.parametrize(
    ("table_name", "index_name", "ddl"), _WINDOW_INDEXES, ids=[i[1] for i in _WINDOW_INDEXES]
)
def test_window_index_renders_the_exact_postgresql_ddl(
    table_name: str, index_name: str, ddl: str
) -> None:
    """Each window index is a plain, non-partial B-tree that leads with ``dataset_id``."""
    index = _index(table_name, index_name)
    assert _ddl(index) == ddl
    assert index.unique is False
    assert index.dialect_options["postgresql"]["where"] is None


@pytest.mark.parametrize(
    ("table_name", "index_name"),
    [(table, name) for table, name, _ in _WINDOW_INDEXES],
    ids=[i[1] for i in _WINDOW_INDEXES],
)
def test_window_index_name_is_explicit_and_fits_postgresql(
    table_name: str, index_name: str
) -> None:
    """The name carries the ``ix_<table>_`` prefix and is never truncated by PostgreSQL."""
    assert index_name.startswith(f"ix_{table_name}_")
    assert len(index_name) <= _MAX_IDENTIFIER_LENGTH


@pytest.mark.parametrize(
    ("table_name", "index_name", "ddl"), _ENTITY_INDEXES, ids=[i[1] for i in _ENTITY_INDEXES]
)
def test_entity_index_is_unchanged(table_name: str, index_name: str, ddl: str) -> None:
    """The (entity, ``date``) indexes and the segment campaign index keep their exact DDL."""
    index = _index(table_name, index_name)
    assert _ddl(index) == ddl
    assert index.unique is False


@pytest.mark.parametrize("table_name", sorted(_MODELS))
def test_fact_table_declares_exactly_its_entity_and_window_indexes(table_name: str) -> None:
    """No fact index was dropped or replaced: the set is the entity indexes plus the window one."""
    expected = {name for table, name, _ in _ENTITY_INDEXES if table == table_name} | {
        name for table, name, _ in _WINDOW_INDEXES if table == table_name
    }
    assert {str(index.name) for index in _table(table_name).indexes} == expected


@pytest.mark.parametrize("table_name", sorted(_MODELS))
def test_natural_key_is_unchanged(table_name: str) -> None:
    """The natural-key unique, the upsert's ``ON CONFLICT`` target, keeps its name and columns."""
    name, columns = _NATURAL_KEYS[table_name]
    uniques = {
        str(constraint.name): tuple(str(column.name) for column in constraint.columns)
        for constraint in _table(table_name).constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert uniques == {name: columns}
    assert _DECLARED_NATURAL_KEYS[table_name] == columns
