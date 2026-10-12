"""Metadata tests: the three indexes of ``analysis_run``.

Runs are read three ways. ``GET /runs?kind=`` (``ads_engine.analysis.runner.list_runs``) reads
the newest runs of one kind, newest first by id, which ``ix_analysis_run_kind_id`` on
(``kind``, ``id DESC``) serves as one index range in order, with no sort, however many runs of
the other kind lie between them. ``/runs/latest`` (``latest_default_run``) reads the latest
succeeded ``default`` run through ``ix_analysis_run_kind_status_finished``. The partial unique
``uq_analysis_run_single_running`` allows one ``running`` run at a time (AAP 0.5.3).
``backend/migrations/versions/0001_initial_schema.py`` must mirror all three.

DDL is compiled offline with the PostgreSQL dialect; no database is needed.
"""

from sqlalchemy import Index, Table
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.schema import CreateIndex

from ads_engine.db.models.analysis import AnalysisRun

# The project's own driver dialect, resolved through the typed URL API rather than the untyped
# ``postgresql.dialect()`` constructor, so this module also passes ``mypy --strict``.
_PG: Dialect = make_url("postgresql+psycopg://").get_dialect()()

# PostgreSQL truncates identifiers longer than this (NAMEDATALEN - 1).
_MAX_IDENTIFIER_LENGTH = 63

_KIND_ID_INDEX = "ix_analysis_run_kind_id"
_LATEST_INDEX = "ix_analysis_run_kind_status_finished"
_SINGLE_RUNNING_INDEX = "uq_analysis_run_single_running"


def _table() -> Table:
    """Return the ``analysis_run`` table of the model metadata."""
    table = AnalysisRun.__table__
    assert isinstance(table, Table)
    return table


def _indexes() -> dict[str, Index]:
    """Return the indexes declared on ``analysis_run`` by name."""
    return {str(index.name): index for index in _table().indexes}


def _ddl(index: Index) -> str:
    """Render ``CREATE INDEX`` for ``index`` with the PostgreSQL dialect."""
    return str(CreateIndex(index).compile(dialect=_PG)).strip()


def test_analysis_run_has_exactly_the_list_latest_and_single_running_indexes() -> None:
    """Each read and the running guard have their index, and nothing else is indexed."""
    assert set(_indexes()) == {_KIND_ID_INDEX, _LATEST_INDEX, _SINGLE_RUNNING_INDEX}


def test_kind_id_index_is_a_plain_full_index() -> None:
    """``GET /runs`` reads every status, so the index is neither unique nor partial."""
    index = _indexes()[_KIND_ID_INDEX]
    assert index.unique is False
    assert index.dialect_options["postgresql"]["where"] is None


def test_kind_id_index_renders_the_exact_postgresql_ddl() -> None:
    """``kind`` equality, then ``id`` descending: the order ``list_runs`` returns."""
    assert _ddl(_indexes()[_KIND_ID_INDEX]) == (
        "CREATE INDEX ix_analysis_run_kind_id ON analysis_run (kind, id DESC)"
    )


def test_latest_and_single_running_indexes_are_unchanged() -> None:
    """``/runs/latest`` keeps (kind, status, finished_at); one ``running`` run stays enforced."""
    latest = _indexes()[_LATEST_INDEX]
    single_running = _indexes()[_SINGLE_RUNNING_INDEX]
    assert latest.unique is False
    assert _ddl(latest) == (
        "CREATE INDEX ix_analysis_run_kind_status_finished ON analysis_run "
        "(kind, status, finished_at)"
    )
    assert single_running.unique is True
    assert _ddl(single_running) == (
        "CREATE UNIQUE INDEX uq_analysis_run_single_running ON analysis_run (status) "
        "WHERE status = 'running'"
    )


def test_index_names_carry_the_table_prefix_and_fit_postgresql_identifiers() -> None:
    """Explicit ``ix_analysis_run_`` / ``uq_analysis_run_`` names PostgreSQL never truncates."""
    for name, index in _indexes().items():
        prefix = "uq_analysis_run_" if index.unique else "ix_analysis_run_"
        assert name.startswith(prefix), name
        assert len(name) <= _MAX_IDENTIFIER_LENGTH, name
