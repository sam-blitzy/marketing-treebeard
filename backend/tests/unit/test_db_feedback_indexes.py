"""Metadata tests: the two lookup indexes of ``recommendation_feedback``.

Feedback is read two ways. The runner (``ads_engine.analysis.metrics``) reads every row still
in force, ``until`` after the run time, before it knows its items: a range over ``until``
alone, which ``ix_recommendation_feedback_until`` serves. The read-time overlay
(``ads_engine.analysis.overlay``) reads the exact ``(rule_id, entity_type, entity_id)`` keys
of one page with ``until`` after the read time, which ``ix_recommendation_feedback_rule_entity``
serves. Expired rows stay as history, so neither read may scan the whole table.
``backend/migrations/versions/0001_initial_schema.py`` must mirror both indexes.

DDL is compiled offline with the PostgreSQL dialect; no database is needed.
"""

from sqlalchemy import Index, Table
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.schema import CreateIndex

from ads_engine.db.models.analysis import RecommendationFeedback

# The project's own driver dialect, resolved through the typed URL API rather than the untyped
# ``postgresql.dialect()`` constructor, so this module also passes ``mypy --strict``.
_PG: Dialect = make_url("postgresql+psycopg://").get_dialect()()

# PostgreSQL truncates identifiers longer than this (NAMEDATALEN - 1).
_MAX_IDENTIFIER_LENGTH = 63

_EXACT_KEY_INDEX = "ix_recommendation_feedback_rule_entity"
_EXPIRY_INDEX = "ix_recommendation_feedback_until"


def _table() -> Table:
    """Return the ``recommendation_feedback`` table of the model metadata."""
    table = RecommendationFeedback.__table__
    assert isinstance(table, Table)
    return table


def _indexes() -> dict[str, Index]:
    """Return the indexes declared on ``recommendation_feedback`` by name."""
    return {str(index.name): index for index in _table().indexes}


def _ddl(index: Index) -> str:
    """Render ``CREATE INDEX`` for ``index`` with the PostgreSQL dialect."""
    return str(CreateIndex(index).compile(dialect=_PG)).strip()


def test_feedback_has_exactly_the_exact_key_and_expiry_indexes() -> None:
    """Both reads have their index, and nothing else is indexed on the table."""
    assert set(_indexes()) == {_EXACT_KEY_INDEX, _EXPIRY_INDEX}


def test_expiry_index_is_a_plain_index_leading_with_until() -> None:
    """The runner's ``until > :now`` range is the leading column of a non-unique index."""
    index = _indexes()[_EXPIRY_INDEX]
    assert tuple(str(column.name) for column in index.columns) == ("until",)
    assert index.unique is False
    assert index.dialect_options["postgresql"]["where"] is None


def test_expiry_index_renders_the_exact_postgresql_ddl() -> None:
    """``CREATE INDEX`` compiles to the DDL the migration must mirror."""
    assert _ddl(_indexes()[_EXPIRY_INDEX]) == (
        "CREATE INDEX ix_recommendation_feedback_until ON recommendation_feedback (until)"
    )


def test_exact_key_index_is_unchanged() -> None:
    """The overlay's exact-key lookup keeps its (rule, type, id, until) index."""
    index = _indexes()[_EXACT_KEY_INDEX]
    assert index.unique is False
    assert _ddl(index) == (
        "CREATE INDEX ix_recommendation_feedback_rule_entity ON recommendation_feedback "
        "(rule_id, entity_type, entity_id, until)"
    )


def test_index_names_carry_the_table_prefix_and_fit_postgresql_identifiers() -> None:
    """Explicit ``ix_recommendation_feedback_`` names that PostgreSQL never truncates."""
    for name in _indexes():
        assert name.startswith("ix_recommendation_feedback_")
        assert len(name) <= _MAX_IDENTIFIER_LENGTH
