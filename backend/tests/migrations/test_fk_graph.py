"""Schema and foreign-key graph tests for the initial migration.

AAP 0.11.2 ``migrations/test_fk_graph.py``; AAP 0.5.2 FK actions; AAP 0.3.3 dataset purge.

The nightly cleanup purges a retired dataset with one statement,
``DELETE FROM source_dataset WHERE id = :retired``, and encodes no table order: it relies
entirely on the foreign-key actions of revision 0001 (AAP 0.3.3, 0.5.2). This module proves
those actions on the schema ``alembic upgrade head`` built for the session (the ``sync_engine``
fixture of ``tests/conftest.py``):

* every ``dataset_id``, ``crm_dataset_id`` and ``parent_dataset_id`` reference, and every key
  between two rows of one dataset, is ``ON DELETE CASCADE``;
* the run tables' typed entity keys are nullable and ``ON DELETE SET NULL``, so frozen run
  history still renders after a purge (AAP 0.3.2);
* ``lead_match`` has the primary key (``lead_id``, ``crm_dataset_id``);
* deleting a populated ``source_dataset`` row succeeds and leaves no orphan rows, while every
  row of every other dataset, and the run history, survives.

It also proves that the hand-written migration and ``Base.metadata`` cannot drift apart
(``compare_metadata`` plus a full DDL comparison against ``create_all``, which also covers the
CHECK texts and partial-index predicates ``compare_metadata`` ignores), and that names and
column conventions hold (no PostgreSQL ``ENUM`` types; lead emails only as SHA-256 hashes).

Isolation: data tests run inside one transaction per test that first empties every table and
is always rolled back, so nothing is committed and the session schema stays exactly as the
migration left it. The DDL comparison builds its probe schema inside a rolled-back
transaction too. An unreachable database fails these tests (the conftest never skips them).
"""

import hashlib
import pprint
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, NamedTuple

import psycopg
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.db.base import NAMING_CONVENTION, Base
from ads_engine.db.models import (
    DATASET_ROLES,
    DATASET_STATES,
    DATASETS,
    DECISION_SHAPES,
    LEAD_MATCH_STATUSES,
    MODES,
    SOURCES,
)

# ---------------------------------------------------------------------------------------------
# The planned schema (AAP 0.5.2)
# ---------------------------------------------------------------------------------------------

# AAP 0.5.2: 6 ops + 13 accounts + 5 facts + 1 snapshots + 7 crm + 2 decisions + 7 analysis.
EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        # ops
        "source_dataset",
        "dataset_freshness",
        "app_user",
        "source_connection",
        "sync_run",
        "sync_request",
        # accounts
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
        "content_asset",
        "content_asset_form",
        "content_asset_creative",
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
        "lead_match",
        "crm_contact",
        "crm_meeting",
        "crm_deal",
        "crm_association",
        "crm_association_fetch",
        # decisions
        "decision",
        "discrepancy",
        # analysis
        "analysis_run",
        "analysis_request",
        "recommendation",
        "run_finding",
        "run_withheld",
        "run_suppression",
        "recommendation_feedback",
    }
)

# AAP 0.3.3 "Tagging": every source-derived row carries a dataset_id that cascades from
# source_dataset. lead_form_version may or may not carry its own dataset_id (the plan is
# ambiguous); when it does, the key must cascade too (OPTIONAL_CASCADE_EDGES).
DATASET_SCOPED_TABLES: frozenset[str] = frozenset(
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

# AAP 0.3.3 "Retirement and purge": every table whose rows must disappear with their dataset.
PURGEABLE_TABLES: frozenset[str] = DATASET_SCOPED_TABLES | {"lead_form_version", "lead_match"}

# AAP 0.3.3 "Run history": run tables reference entities only through SET NULL keys, so a
# purge never deletes them.
RUN_HISTORY_TABLES: frozenset[str] = frozenset(
    {
        "analysis_run",
        "recommendation",
        "run_finding",
        "run_withheld",
        "run_suppression",
        "recommendation_feedback",
    }
)

# Tables that are not source data: they must survive every purge (AAP 0.3.3, 0.5.2).
NON_SCOPED_TABLES: frozenset[str] = RUN_HISTORY_TABLES | {
    "app_user",
    "source_connection",
    "sync_run",
    "sync_request",
    "analysis_request",
    "content_asset",
    "content_asset_form",
    "content_asset_creative",
}

# AAP 0.5.2: the typed, nullable entity keys of the run-output tables, column -> entity table.
TYPED_ENTITY_KEYS: Mapping[str, str] = {
    "campaign_id": "campaign",
    "budget_id": "budget",
    "ad_group_id": "ad_group",
    "keyword_id": "keyword",
    "creative_id": "creative",
}
RUN_ENTITY_KEY_TABLES: tuple[str, ...] = ("recommendation", "run_finding", "run_withheld")

# An edge is (table, first referencing column, referred table): the naming convention keys a
# composite (dataset-qualified) foreign key on its first column as well.
Edge = tuple[str, str, str]

# AAP 0.5.2 "Keys from run tables (recommendation, run_finding, run_withheld) to entities are
# nullable and ON DELETE SET NULL", plus the two other keys that point into replaceable rows:
# decision.recommendation_id into run history and source_connection.active_dataset_id at a
# dataset (both keep their row when the target goes), and analysis_request.run_id.
SET_NULL_EDGES: frozenset[Edge] = frozenset(
    {
        (table, column, referred)
        for table in RUN_ENTITY_KEY_TABLES
        for column, referred in TYPED_ENTITY_KEYS.items()
    }
    | {
        ("decision", "recommendation_id", "recommendation"),
        ("source_connection", "active_dataset_id", "source_dataset"),
        ("analysis_request", "run_id", "analysis_run"),
    }
)

# AAP 0.5.2 "Foreign-key actions": every dataset reference and every key between two rows of
# one dataset cascades; run outputs cascade from their run, the registry from its asset.
CASCADE_EDGES: frozenset[Edge] = frozenset(
    {("source_dataset", "parent_dataset_id", "source_dataset")}
    | {(table, "dataset_id", "source_dataset") for table in DATASET_SCOPED_TABLES}
    | {
        ("lead_match", "crm_dataset_id", "source_dataset"),
        # accounts
        ("campaign_group", "account_id", "ad_account"),
        ("campaign", "account_id", "ad_account"),
        ("campaign", "campaign_group_id", "campaign_group"),
        ("campaign", "budget_id", "budget"),
        ("ad_group", "campaign_id", "campaign"),
        ("keyword", "ad_group_id", "ad_group"),
        ("creative", "campaign_id", "campaign"),
        ("lead_form_version", "lead_form_id", "lead_form"),
        # facts
        ("fact_campaign_daily", "campaign_id", "campaign"),
        ("fact_creative_daily", "creative_id", "creative"),
        ("fact_keyword_daily", "keyword_id", "keyword"),
        ("fact_search_term_daily", "ad_group_id", "ad_group"),
        ("fact_search_term_daily", "campaign_id", "campaign"),
        ("fact_segment_window", "campaign_id", "campaign"),
        # snapshots, leads and decisions
        ("entity_snapshot", "budget_id", "budget"),
        ("lead", "campaign_id", "campaign"),
        ("lead", "creative_id", "creative"),
        ("lead_match", "lead_id", "lead"),
        ("lead_match", "contact_id", "crm_contact"),
        ("decision", "budget_id", "budget"),
        ("discrepancy", "snapshot_id", "entity_snapshot"),
        ("discrepancy", "decision_id", "decision"),
        # run tables
        ("recommendation", "run_id", "analysis_run"),
        ("run_finding", "run_id", "analysis_run"),
        ("run_withheld", "run_id", "analysis_run"),
        ("run_suppression", "run_id", "analysis_run"),
        ("recommendation_feedback", "recommendation_id", "recommendation"),
        # content assets
        ("content_asset_form", "asset_id", "content_asset"),
        ("content_asset_creative", "asset_id", "content_asset"),
    }
)

# lead_form_version's own dataset_id: optional in the plan, CASCADE when present.
OPTIONAL_CASCADE_EDGES: frozenset[Edge] = frozenset(
    {("lead_form_version", "dataset_id", "source_dataset")}
)

# Columns whose names start a dataset reference; each such key must cascade (AAP 0.5.2).
DATASET_REFERENCE_COLUMNS: frozenset[str] = frozenset(
    {"dataset_id", "crm_dataset_id", "parent_dataset_id"}
)

# Polymorphic ids and HubSpot association endpoints carry no foreign key: one column cannot
# reference several tables, and an unread association target is unknown, never absent
# (AAP 0.3.3, 0.5.2).
NO_FK_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("entity_snapshot", "entity_id"),
        ("decision", "subject_id"),
        ("discrepancy", "subject_id"),
        ("recommendation", "entity_id"),
        ("recommendation_feedback", "entity_id"),
        ("crm_association", "from_id"),
        ("crm_association", "to_id"),
        ("crm_association_fetch", "object_id"),
    }
)


class PartialUnique(NamedTuple):
    """The expected shape of one partial unique index."""

    table: str
    columns: tuple[str, ...]
    predicate: str


# AAP 0.5.2 / 0.5.3: one active primary dataset per source, one pending sync request per
# source, one running analysis run, one non-resolved discrepancy per (subject, kind). The
# predicates are PostgreSQL's own rendering (pg_get_expr) of the migration's WHERE clauses.
PARTIAL_UNIQUE_INDEXES: Mapping[str, PartialUnique] = {
    "uq_source_dataset_active_primary": PartialUnique(
        "source_dataset",
        ("source",),
        "((state = 'active'::text) AND (role = 'primary'::text))",
    ),
    "uq_sync_request_pending": PartialUnique("sync_request", ("source",), "(picked_at IS NULL)"),
    "uq_analysis_run_single_running": PartialUnique(
        "analysis_run", ("status",), "(status = 'running'::text)"
    ),
    "uq_discrepancy_open_subject_kind": PartialUnique(
        "discrepancy",
        ("subject_type", "subject_id", "kind"),
        "(state <> 'resolved'::text)",
    ),
}

# AAP 0.3.2 "Personal data": the only columns whose names mention email.
EMAIL_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("app_user", "email"),
        ("lead", "email_sha256"),
        ("crm_contact", "email_sha256"),
        ("lead_form_version", "email_question_ids"),
    }
)

ALEMBIC_VERSION = "alembic_version"
"""Alembic's own bookkeeping table: not part of the application schema."""

PROBE_SCHEMA = "drift_probe"
"""The rolled-back schema ``Base.metadata.create_all`` builds for the DDL comparison."""

UNIQUE_VIOLATION = "23505"
"""SQLSTATE ``unique_violation``."""

CHECK_VIOLATION = "23514"
"""SQLSTATE ``check_violation``."""

PG_MAX_IDENTIFIER = 63
"""PostgreSQL's identifier length limit; a longer name would be silently truncated."""

# Fixed, tz-aware instants and native days: identities are never reset, so the tests assert
# by the ids they inserted, never by literal ids.
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)
DAY = date(2026, 8, 31)
WINDOW_START = date(2026, 8, 4)
WINDOW_END = date(2026, 8, 31)
EMAIL_HASH = hashlib.sha256(b"lead@sim.invalid").hexdigest()
OTHER_EMAIL_HASH = hashlib.sha256(b"other@sim.invalid").hexdigest()

# ---------------------------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------------------------


class ForeignKey(NamedTuple):
    """One foreign-key constraint of the migrated schema, read from ``pg_constraint``.

    Attributes:
        name: The constraint name.
        table: The referencing table.
        columns: The referencing columns, in key order.
        referred_table: The referenced table.
        referred_columns: The referenced columns, in key order.
        on_delete: ``confdeltype``: ``a`` no action, ``r`` restrict, ``c`` cascade, ``n`` set
            null, ``d`` set default.
    """

    name: str
    table: str
    columns: tuple[str, ...]
    referred_table: str
    referred_columns: tuple[str, ...]
    on_delete: str

    @property
    def edge(self) -> Edge:
        """The (table, first column, referred table) key the expected edge sets use."""
        return (self.table, self.columns[0], self.referred_table)


_FOREIGN_KEYS_SQL = sa.text(
    "SELECT c.conname, cl.relname, rf.relname, c.confdeltype::text, "
    "ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute AS a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
    "ORDER BY k.ord), "
    "ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute AS a ON a.attrelid = c.confrelid AND a.attnum = k.attnum "
    "ORDER BY k.ord) "
    "FROM pg_constraint AS c "
    "JOIN pg_class AS cl ON cl.oid = c.conrelid "
    "JOIN pg_namespace AS n ON n.oid = cl.relnamespace "
    "JOIN pg_class AS rf ON rf.oid = c.confrelid "
    "WHERE c.contype = 'f' AND n.nspname = current_schema() "
    "ORDER BY cl.relname, c.conname"
)


def _foreign_keys(conn: Connection) -> list[ForeignKey]:
    """Return every foreign key of the current schema, ordered by table and name."""
    return [
        ForeignKey(
            name=name,
            table=table,
            columns=tuple(columns),
            referred_table=referred_table,
            referred_columns=tuple(referred_columns),
            on_delete=on_delete,
        )
        for name, table, referred_table, on_delete, columns, referred_columns in conn.execute(
            _FOREIGN_KEYS_SQL
        ).all()
    ]


def _current_schema(conn: Connection) -> str:
    """Return the schema the session migrated and unqualified names resolve to."""
    return str(conn.execute(sa.text("SELECT current_schema()")).scalar_one())


def _application_tables(conn: Connection) -> set[str]:
    """Return the reflected tables of the current schema, without ``alembic_version``."""
    return set(sa.inspect(conn).get_table_names()) - {ALEMBIC_VERSION}


def _nullable_columns(conn: Connection, tables: Iterable[str]) -> dict[tuple[str, str], bool]:
    """Return ``{(table, column): nullable}`` for ``tables``, reflected with the inspector."""
    inspector = sa.inspect(conn)
    return {
        (table, column["name"]): bool(column["nullable"])
        for table in tables
        for column in inspector.get_columns(table)
    }


def _tables_with_dataset_id(conn: Connection) -> list[str]:
    """Return the reflected tables of the current schema that carry a ``dataset_id`` column."""
    inspector = sa.inspect(conn)
    return [
        table
        for table in sorted(_application_tables(conn))
        if any(column["name"] == "dataset_id" for column in inspector.get_columns(table))
    ]


def _cascade_graph(foreign_keys: Iterable[ForeignKey]) -> dict[str, set[str]]:
    """Return the directed graph child -> parents of the ``ON DELETE CASCADE`` keys."""
    graph: dict[str, set[str]] = defaultdict(set)
    for fk in foreign_keys:
        if fk.on_delete == "c":
            graph[fk.table].add(fk.referred_table)
    return graph


def _reaches(graph: Mapping[str, set[str]], start: str, target: str) -> bool:
    """Report whether ``target`` is reachable from ``start`` along ``graph``'s edges."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        parents = graph.get(node, set())
        if target in parents:
            return True
        stack.extend(parents - seen)
    return False


# ---------------------------------------------------------------------------------------------
# Data helpers: Core statements over Base.metadata, inside one rolled-back transaction
# ---------------------------------------------------------------------------------------------


@contextmanager
def _scratch_transaction(engine: Engine) -> Iterator[Connection]:
    """Yield a connection inside a transaction that starts empty and is always rolled back.

    Every table is emptied first, children before parents, so rows other modules committed
    cannot collide with the partial unique indexes; the rollback restores them and the
    schema stays exactly as ``alembic upgrade head`` left it.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
            yield conn
        finally:
            transaction.rollback()


def _table(name: str) -> sa.Table:
    """Return the ``Base.metadata`` table ``name``; it resolves to the session schema."""
    return Base.metadata.tables[name]


def _insert(conn: Connection, table: str, **values: object) -> int:
    """Insert one row into ``table`` and return its generated ``id``."""
    target = _table(table)
    return int(conn.execute(sa.insert(target).values(**values).returning(target.c.id)).scalar_one())


def _insert_row(conn: Connection, table: str, **values: object) -> None:
    """Insert one row into a table without an ``id`` column (``lead_match``)."""
    conn.execute(sa.insert(_table(table)).values(**values))


def _where(table: sa.Table, values: Mapping[str, object]) -> list[sa.ColumnElement[bool]]:
    """Return one equality per item of ``values``; a ``None`` value tests ``IS NULL``."""
    return [
        table.c[column].is_(None) if value is None else table.c[column] == value
        for column, value in values.items()
    ]


def _count(conn: Connection, table: str, *clauses: sa.ColumnElement[bool]) -> int:
    """Return how many rows of ``table`` satisfy every clause."""
    target = _table(table)
    statement = sa.select(sa.func.count()).select_from(target).where(*clauses)
    return int(conn.execute(statement).scalar_one())


def _exists(conn: Connection, table: str, **where: object) -> bool:
    """Report whether ``table`` holds a row whose columns equal ``where``."""
    return _count(conn, table, *_where(_table(table), where)) > 0


def _row(conn: Connection, table: str, **where: object) -> dict[str, Any] | None:
    """Return the single row of ``table`` matching ``where`` as a dict, or ``None``."""
    target = _table(table)
    row = conn.execute(sa.select(target).where(*_where(target, where))).mappings().one_or_none()
    return None if row is None else dict(row)


def _require_row(conn: Connection, table: str, **where: object) -> dict[str, Any]:
    """Return the single row of ``table`` matching ``where``; fail when it is absent."""
    row = _row(conn, table, **where)
    assert row is not None, f"{table} row {where} is missing"
    return row


class Dataset(NamedTuple):
    """One inserted ``source_dataset`` row: its id and source."""

    id: int
    source: str

    @property
    def scope(self) -> dict[str, object]:
        """The provenance columns of a row in this dataset (``source`` matches the dataset)."""
        return {"source": self.source, "dataset_id": self.id}


@dataclass
class Ledger:
    """The rows a purge test inserted, grouped by the dataset label they belong to.

    Attributes:
        rows: ``label -> [(table, id), ...]`` in insertion order.
        matches: ``label -> [(lead_id, crm_dataset_id), ...]`` for ``lead_match`` rows, whose
            label is the CRM dataset they were staged for.
    """

    rows: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    matches: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def put(self, conn: Connection, label: str, table: str, **values: object) -> int:
        """Insert one row, record it under ``label`` and return its id."""
        row_id = _insert(conn, table, **values)
        self.rows.setdefault(label, []).append((table, row_id))
        return row_id

    def dataset(
        self,
        conn: Connection,
        label: str,
        *,
        source: str,
        mode: str,
        state: str,
        role: str = "primary",
        parent: Dataset | None = None,
    ) -> Dataset:
        """Insert a ``source_dataset`` row satisfying ``ck_source_dataset_role_shape``."""
        assert source in SOURCES, source
        assert mode in MODES, mode
        assert state in DATASET_STATES, state
        assert role in DATASET_ROLES, role
        row_id = self.put(
            conn,
            label,
            "source_dataset",
            source=source,
            mode=mode,
            state=state,
            role=role,
            parent_dataset_id=None if parent is None else parent.id,
            reason=f"{label} fixture dataset",
            activated_at=T0 if state != "building" else None,
            retired_at=T0 if state == "retired" else None,
        )
        return Dataset(row_id, source)

    def freshness(self, conn: Connection, label: str, dataset: Dataset, name: str) -> int:
        """Insert a ``dataset_freshness`` row for the named dataset (AAP 0.3.2 names)."""
        assert name in DATASETS, name
        assert name.startswith(f"{dataset.source}."), (name, dataset.source)
        return self.put(
            conn,
            label,
            "dataset_freshness",
            **dataset.scope,
            dataset=name,
            last_attempt_at=T0,
            last_success_at=T0,
            covered_from=WINDOW_START,
            covered_through=WINDOW_END,
            completeness="complete",
        )

    def match(
        self,
        conn: Connection,
        label: str,
        *,
        lead_id: int,
        crm: Dataset,
        status: str,
        contact_id: int | None = None,
        attempts: int = 1,
        next_attempt_at: datetime | None = None,
    ) -> tuple[int, int]:
        """Insert a ``lead_match`` row staged for ``crm`` and record its key under ``label``.

        A ``matched`` row carries its contact and ``contact_read_status = 'ok'``, as
        ``ck_lead_match_matched_has_contact`` requires.
        """
        assert status in LEAD_MATCH_STATUSES, status
        assert crm.source == "hubspot", crm
        _insert_row(
            conn,
            "lead_match",
            lead_id=lead_id,
            crm_dataset_id=crm.id,
            status=status,
            attempts=attempts,
            next_attempt_at=next_attempt_at,
            last_checked_at=T0,
            contact_id=contact_id,
            contact_read_status="ok" if status == "matched" else None,
        )
        key = (lead_id, crm.id)
        self.matches.setdefault(label, []).append(key)
        return key

    def tracked(self, *labels: str) -> list[tuple[str, int]]:
        """Return every ``(table, id)`` recorded under ``labels``."""
        return [entry for label in labels for entry in self.rows.get(label, [])]

    def tracked_matches(self, *labels: str) -> list[tuple[int, int]]:
        """Return every ``lead_match`` key recorded under ``labels``."""
        return [key for label in labels for key in self.matches.get(label, [])]


def _decision(
    ledger: Ledger,
    conn: Connection,
    label: str,
    dataset: Dataset,
    *,
    shape: str,
    subject_type: str,
    subject_id: int,
    **state: object,
) -> int:
    """Insert one materialised ``decision``; ``state`` must fit ``ck_decision_shape_columns``."""
    assert shape in DECISION_SHAPES, shape
    return ledger.put(
        conn,
        label,
        "decision",
        **dataset.scope,
        subject_type=subject_type,
        subject_id=subject_id,
        shape=shape,
        decided_at=T0,
        origin="baseline",
        **state,
    )


def _pg_error(error: IntegrityError) -> psycopg.Error:
    """Return the psycopg error behind a SQLAlchemy ``IntegrityError``."""
    original = error.orig
    assert isinstance(original, psycopg.Error), type(original)
    return original


def _assert_rejected(
    conn: Connection, sqlstate: str, constraint: str, insert: Callable[[], object]
) -> None:
    """Assert that ``insert`` fails with ``sqlstate`` on ``constraint`` inside a savepoint.

    The savepoint is rolled back, so the enclosing transaction stays usable afterwards.
    """
    with pytest.raises(IntegrityError) as caught, conn.begin_nested():
        insert()
    error = _pg_error(caught.value)
    assert error.sqlstate == sqlstate, (error.sqlstate, str(error))
    assert error.diag.constraint_name == constraint, (error.diag.constraint_name, str(error))


def _accepted(conn: Connection, insert: Callable[[], object]) -> None:
    """Run ``insert`` inside a savepoint that is released: it must succeed."""
    with conn.begin_nested():
        insert()


# ---------------------------------------------------------------------------------------------
# 1-6: the foreign-key graph, read from the catalog
# ---------------------------------------------------------------------------------------------


def test_schema_has_exactly_the_planned_tables(sync_engine: Engine) -> None:
    """AAP 0.5.2: the migrated schema holds exactly the 41 planned tables, as the models do."""
    with sync_engine.connect() as conn:
        reflected = _application_tables(conn)
    modelled = set(Base.metadata.tables)
    assert reflected == EXPECTED_TABLES, (
        f"missing from the schema: {sorted(EXPECTED_TABLES - reflected)}; "
        f"not planned: {sorted(reflected - EXPECTED_TABLES)}"
    )
    assert reflected == modelled, (
        f"migrated but not modelled: {sorted(reflected - modelled)}; "
        f"modelled but not migrated: {sorted(modelled - reflected)}"
    )
    assert len(EXPECTED_TABLES) == 41


def test_every_foreign_key_cascades_or_sets_null_as_specified(sync_engine: Engine) -> None:
    """AAP 0.5.2 "Foreign-key actions" and AAP 0.11.2 `migrations/test_fk_graph.py`.

    Every ``dataset_id``, ``crm_dataset_id`` and ``parent_dataset_id`` reference and every
    intra-dataset key is ``ON DELETE CASCADE``; every run-table entity key (and every other key
    into replaceable rows) is a nullable, single-column ``ON DELETE SET NULL``; no key uses
    ``NO ACTION``, ``RESTRICT`` or ``SET DEFAULT``, any of which would make the one-statement
    purge fail or depend on deletion order.
    """
    with sync_engine.connect() as conn:
        foreign_keys = _foreign_keys(conn)
        nullable = _nullable_columns(conn, {fk.table for fk in foreign_keys})

    by_edge: dict[Edge, list[ForeignKey]] = defaultdict(list)
    for fk in foreign_keys:
        by_edge[fk.edge].append(fk)
    expected = CASCADE_EDGES | SET_NULL_EDGES
    allowed = expected | OPTIONAL_CASCADE_EDGES

    violations: list[str] = []
    violations += [f"missing foreign key {edge}" for edge in sorted(expected - set(by_edge))]
    violations += [
        f"unplanned foreign key {fk.name} {fk.edge}"
        for fk in foreign_keys
        if fk.edge not in allowed
    ]
    violations += [
        f"duplicate foreign keys on {edge}: {[fk.name for fk in fks]}"
        for edge, fks in sorted(by_edge.items())
        if len(fks) > 1
    ]
    for fk in foreign_keys:
        if fk.on_delete in {"a", "r", "d"}:
            violations.append(f"{fk.name} has ON DELETE action {fk.on_delete!r}")
        if fk.columns[0] in DATASET_REFERENCE_COLUMNS and fk.on_delete != "c":
            violations.append(f"{fk.name}: dataset reference is {fk.on_delete!r}, not CASCADE")
        if fk.edge in CASCADE_EDGES | OPTIONAL_CASCADE_EDGES and fk.on_delete != "c":
            violations.append(f"{fk.name}: expected CASCADE, found {fk.on_delete!r}")
        if fk.edge in SET_NULL_EDGES:
            if fk.on_delete != "n":
                violations.append(f"{fk.name}: expected SET NULL, found {fk.on_delete!r}")
            if len(fk.columns) != 1:
                # A composite SET NULL would also null the row's NOT NULL dataset_id.
                violations.append(f"{fk.name}: SET NULL key spans {fk.columns}")
            if not nullable[(fk.table, fk.columns[0])]:
                violations.append(f"{fk.name}: SET NULL column {fk.columns[0]} is NOT NULL")
    assert not violations, "\n".join(violations)


def test_dataset_scoped_tables_reach_source_dataset_by_cascade(sync_engine: Engine) -> None:
    """AAP 0.3.3 purge "within a dataset": deleting a dataset removes exactly its own rows.

    Every purgeable table has a CASCADE path to ``source_dataset``; no non-scoped table has
    one, and the only direct key from a non-scoped table to ``source_dataset`` is the SET NULL
    ``source_connection.active_dataset_id``, so run history and operational rows survive. Every
    key between two rows of one dataset is dataset-qualified, (``<col>``, ``dataset_id``) ->
    (``id``, ``dataset_id``), so a purge can never cascade into another dataset's rows: only
    ``lead_match`` links two datasets, and its contact key is qualified by ``crm_dataset_id``.
    """
    with sync_engine.connect() as conn:
        foreign_keys = _foreign_keys(conn)
        with_dataset_id = set(_tables_with_dataset_id(conn))
    graph = _cascade_graph(foreign_keys)

    assert PURGEABLE_TABLES | NON_SCOPED_TABLES | {"source_dataset"} == EXPECTED_TABLES
    assert not PURGEABLE_TABLES & NON_SCOPED_TABLES
    assert with_dataset_id - {"lead_form_version"} == DATASET_SCOPED_TABLES, sorted(
        with_dataset_id ^ DATASET_SCOPED_TABLES
    )

    unreachable = sorted(t for t in PURGEABLE_TABLES if not _reaches(graph, t, "source_dataset"))
    assert not unreachable, f"no CASCADE path to source_dataset from {unreachable}"
    purged_with_dataset = sorted(
        t for t in NON_SCOPED_TABLES if _reaches(graph, t, "source_dataset")
    )
    assert not purged_with_dataset, f"a purge would delete rows of {purged_with_dataset}"
    direct = sorted(
        (fk.edge, fk.on_delete)
        for fk in foreign_keys
        if fk.table in NON_SCOPED_TABLES and fk.referred_table == "source_dataset"
    )
    assert direct == [(("source_connection", "active_dataset_id", "source_dataset"), "n")]

    scoped = DATASET_SCOPED_TABLES | {"lead_form_version"}
    unqualified = [
        f"{fk.name}: {fk.columns} -> {fk.referred_table}{fk.referred_columns}"
        for fk in foreign_keys
        if fk.table in scoped
        and fk.referred_table in scoped
        and (
            fk.columns != (fk.columns[0], "dataset_id")
            or fk.referred_columns != ("id", "dataset_id")
        )
    ]
    assert not unqualified, "keys between dataset rows not dataset-qualified:\n" + "\n".join(
        unqualified
    )
    intra_dataset = [
        fk for fk in foreign_keys if fk.table in scoped and fk.referred_table in scoped
    ]
    assert len(intra_dataset) == 20, [fk.name for fk in intra_dataset]


def test_lead_match_primary_key_and_cascades(sync_engine: Engine) -> None:
    """AAP 0.11.2 "`lead_match` has primary key (lead_id, crm_dataset_id)".

    A building CRM dataset therefore stages its own match rows without touching the active
    ones (AAP 0.3.3), and all three keys cascade: from the lead, from the CRM dataset, and from
    the contact, which must belong to the match's own CRM dataset.
    """
    with sync_engine.connect() as conn:
        inspector = sa.inspect(conn)
        primary_key = inspector.get_pk_constraint("lead_match")
        columns = {column["name"]: column for column in inspector.get_columns("lead_match")}
        keys = {fk.columns[0]: fk for fk in _foreign_keys(conn) if fk.table == "lead_match"}

    assert primary_key["name"] == "pk_lead_match"
    assert primary_key["constrained_columns"] == ["lead_id", "crm_dataset_id"]
    assert "id" not in columns
    assert set(keys) == {"lead_id", "crm_dataset_id", "contact_id"}
    assert (keys["lead_id"].referred_table, keys["lead_id"].on_delete) == ("lead", "c")
    assert (keys["crm_dataset_id"].referred_table, keys["crm_dataset_id"].on_delete) == (
        "source_dataset",
        "c",
    )
    contact = keys["contact_id"]
    assert (contact.referred_table, contact.on_delete) == ("crm_contact", "c")
    assert contact.columns == ("contact_id", "crm_dataset_id")
    assert contact.referred_columns == ("id", "dataset_id")
    assert columns["contact_id"]["nullable"] is True
    assert columns["lead_id"]["nullable"] is False
    assert columns["crm_dataset_id"]["nullable"] is False


def test_run_table_entity_keys_set_null(sync_engine: Engine) -> None:
    """AAP 0.5.2 "Keys from run tables ... to entities are nullable and ON DELETE SET NULL".

    Each run output also cascades from its own run, and ``recommendation_feedback``
    references ``recommendation`` only.
    """
    with sync_engine.connect() as conn:
        foreign_keys = _foreign_keys(conn)
        nullable = _nullable_columns(conn, RUN_HISTORY_TABLES)
    keys = {(fk.table, fk.columns[0]): fk for fk in foreign_keys}

    for table in RUN_ENTITY_KEY_TABLES:
        for column, referred in TYPED_ENTITY_KEYS.items():
            fk = keys.get((table, column))
            assert fk is not None, f"{table}.{column} has no foreign key"
            assert (fk.referred_table, fk.columns, fk.on_delete) == (referred, (column,), "n")
            assert nullable[(table, column)], f"{table}.{column} must be nullable"
    for table in (*RUN_ENTITY_KEY_TABLES, "run_suppression"):
        fk = keys[(table, "run_id")]
        assert (fk.referred_table, fk.on_delete) == ("analysis_run", "c"), fk
        assert not nullable[(table, "run_id")]
    feedback = [fk for fk in foreign_keys if fk.table == "recommendation_feedback"]
    assert [(fk.columns, fk.referred_table, fk.on_delete) for fk in feedback] == [
        (("recommendation_id",), "recommendation", "c")
    ]


def test_polymorphic_and_jsonb_references_carry_no_foreign_key(sync_engine: Engine) -> None:
    """AAP 0.5.2: polymorphic ids and HubSpot endpoints have no FK; run provenance is jsonb.

    ``analysis_run.dataset_ids`` is ``jsonb`` with no foreign key, so a run still renders
    after the datasets it read are purged (AAP 0.3.2 historical-only items).
    """
    with sync_engine.connect() as conn:
        foreign_keys = _foreign_keys(conn)
        types: dict[str, str] = dict(
            conn.execute(
                sa.text(
                    "SELECT column_name::text, udt_name::text FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'analysis_run'"
                )
            ).all()
        )
    constrained = {(fk.table, column) for fk in foreign_keys for column in fk.columns}
    assert not NO_FK_COLUMNS & constrained, sorted(NO_FK_COLUMNS & constrained)
    assert [fk.name for fk in foreign_keys if fk.table == "analysis_run"] == []
    for column in ("dataset_ids", "data_modes", "source_day_boundaries"):
        assert types[column] == "jsonb", (column, types.get(column))


# ---------------------------------------------------------------------------------------------
# 7-10: purges of populated datasets and runs
# ---------------------------------------------------------------------------------------------

_PURGE = sa.text("DELETE FROM source_dataset WHERE id = :id")
"""The nightly cleanup's statement, exactly (AAP 0.3.3); it never hand-orders deletes."""


def _purge(conn: Connection, dataset: Dataset) -> None:
    """Purge ``dataset`` with the cleanup's one statement; exactly one row must go."""
    result = conn.execute(_PURGE, {"id": dataset.id})
    assert result.rowcount == 1


def _account(ledger: Ledger, conn: Connection, label: str, dataset: Dataset, key: str) -> int:
    """Insert an ``ad_account`` of ``dataset``."""
    return ledger.put(
        conn,
        label,
        "ad_account",
        **dataset.scope,
        external_id=f"{key}-account",
        name=f"{key} account",
        currency="USD",
        timezone="UTC" if dataset.source == "linkedin" else "America/New_York",
    )


def _campaign(
    ledger: Ledger,
    conn: Connection,
    label: str,
    dataset: Dataset,
    key: str,
    account_id: int,
    **extra: object,
) -> int:
    """Insert an active ``campaign`` of ``dataset`` under ``account_id``."""
    return ledger.put(
        conn,
        label,
        "campaign",
        **dataset.scope,
        account_id=account_id,
        external_id=f"{key}-campaign",
        name=f"{key} campaign",
        status="active",
        raw_status="ACTIVE" if dataset.source == "linkedin" else "ENABLED",
        **extra,
    )


def _lead(
    ledger: Ledger,
    conn: Connection,
    label: str,
    dataset: Dataset,
    key: str,
    campaign_id: int,
    creative_id: int | None,
    email_sha256: str,
) -> int:
    """Insert a LinkedIn ``lead`` of ``dataset``, its email stored only as a SHA-256 hash."""
    return ledger.put(
        conn,
        label,
        "lead",
        **dataset.scope,
        external_id=f"{key}-lead",
        submitted_at=T0 - timedelta(days=20),
        campaign_id=campaign_id,
        creative_id=creative_id,
        lead_form_urn="urn:li:leadGenForm:7001",
        lead_form_version=2,
        email_sha256=email_sha256,
    )


def _contact(
    ledger: Ledger, conn: Connection, label: str, dataset: Dataset, hs_id: str, email: str
) -> int:
    """Insert a HubSpot ``crm_contact`` of ``dataset``."""
    return ledger.put(
        conn,
        label,
        "crm_contact",
        **dataset.scope,
        hs_id=hs_id,
        email_sha256=email,
        lifecycle_stage="salesqualifiedlead",
        mql_at=T0 - timedelta(days=15),
        ql_at=T0 - timedelta(days=10),
        updated_at=T0,
    )


def _crm_pipeline(
    ledger: Ledger, conn: Connection, label: str, dataset: Dataset, contact_hs_id: str
) -> None:
    """Insert a meeting, a deal, their two associations and a fetch row for one contact."""
    ledger.put(
        conn,
        label,
        "crm_meeting",
        **dataset.scope,
        hs_id=f"{contact_hs_id}-meeting",
        created_at=T0 - timedelta(days=12),
        start_at=T0 - timedelta(days=11),
        meeting_timestamp=T0 - timedelta(days=11),
        outcome="COMPLETED",
        meeting_source="MEETINGS_PUBLIC",
    )
    ledger.put(
        conn,
        label,
        "crm_deal",
        **dataset.scope,
        hs_id=f"{contact_hs_id}-deal",
        created_at=T0 - timedelta(days=9),
        stage="appointmentscheduled",
        pipeline="default",
        amount=Decimal("12000.000000"),
    )
    for to_type in ("meeting", "deal"):
        ledger.put(
            conn,
            label,
            "crm_association",
            **dataset.scope,
            from_type="contact",
            from_id=contact_hs_id,
            to_type=to_type,
            to_id=f"{contact_hs_id}-{to_type}",
        )
    ledger.put(
        conn,
        label,
        "crm_association_fetch",
        **dataset.scope,
        object_type="contact",
        object_id=contact_hs_id,
        to_type="meeting",
        status="ok",
        target_read_status="ok",
        missing_target_ids=[],
        fetched_at=T0,
    )


def _run(ledger: Ledger, conn: Connection, label: str, dataset_ids: dict[str, int]) -> int:
    """Insert a succeeded default ``analysis_run`` whose provenance names ``dataset_ids``."""
    return ledger.put(
        conn,
        label,
        "analysis_run",
        kind="default",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        source_day_boundaries=[
            {"source": "linkedin", "start": "2026-08-04", "end": "2026-08-31", "boundary": "UTC"}
        ],
        trigger="event",
        data_modes={"google": "live", "linkedin": "simulated", "hubspot": "simulated"},
        dataset_ids=dataset_ids,
        config_hash="0" * 64,
        status="succeeded",
        started_at=T0,
        finished_at=T0 + timedelta(seconds=5),
    )


def _recommendation(
    ledger: Ledger,
    conn: Connection,
    label: str,
    run_id: int,
    evidence: dict[str, Any],
    **entity_keys: int,
) -> int:
    """Insert a frozen ``recommendation`` of ``run_id`` with typed entity keys ``entity_keys``."""
    assert set(entity_keys) <= set(TYPED_ENTITY_KEYS), entity_keys
    return ledger.put(
        conn,
        label,
        "recommendation",
        run_id=run_id,
        section="campaigns",
        group="pause",
        rule_id="C1",
        entity_type="campaign",
        entity_id="linkedin:campaign:l-campaign",
        rank=1,
        action="Pause",
        reason="Spent on 26 of 28 days with 0 leads against 3.4 expected",
        evidence=evidence,
        flags=["simulated"],
        data_status="simulated",
        required_datasets=["linkedin.analytics_daily"],
        decision_subject={"type": "campaign", "id": 1},
        **entity_keys,
    )


def _left_and_lost(
    conn: Connection, ledger: Ledger, *, gone: Iterable[str], kept: Iterable[str]
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Return the tracked rows still present under ``gone`` and those absent under ``kept``."""
    left = [(t, i) for t, i in ledger.tracked(*gone) if _exists(conn, t, id=i)]
    lost = [(t, i) for t, i in ledger.tracked(*kept) if not _exists(conn, t, id=i)]
    return left, lost


def _match_exists(conn: Connection, key: tuple[int, int]) -> bool:
    """Report whether the ``lead_match`` row ``(lead_id, crm_dataset_id)`` exists."""
    return _exists(conn, "lead_match", lead_id=key[0], crm_dataset_id=key[1])


def test_purge_retired_linkedin_dataset_with_companion(sync_engine: Engine) -> None:
    """AAP 0.11.2 "deleting a populated source_dataset row ... succeeds with no orphan rows".

    AAP 0.3.3: purging a retired simulated LinkedIn dataset removes every row of it, its
    companion CRM dataset (``parent_dataset_id`` cascade) with every row of that, and every
    ``lead_match`` row of its leads, including those staged against the live HubSpot dataset.
    Every row of the other datasets survives, and so does the run history: its typed entity
    keys become NULL while its names, numbers and provenance stay frozen (AAP 0.3.2).
    """
    with _scratch_transaction(sync_engine) as conn:
        ledger = Ledger()
        retired = ledger.dataset(conn, "L", source="linkedin", mode="simulated", state="retired")
        companion = ledger.dataset(
            conn,
            "C",
            source="hubspot",
            mode="simulated",
            state="retired",
            role="companion",
            parent=retired,
        )
        live_linkedin = ledger.dataset(conn, "L2", source="linkedin", mode="live", state="active")
        hubspot = ledger.dataset(conn, "H", source="hubspot", mode="live", state="active")
        google = ledger.dataset(conn, "G", source="google", mode="live", state="active")

        # The retired LinkedIn dataset: entities, facts, snapshots, leads, decisions.
        ledger.freshness(conn, "L", retired, "linkedin.entities")
        ledger.freshness(conn, "L", retired, "linkedin.leads")
        account = _account(ledger, conn, "L", retired, "l")
        group = ledger.put(
            conn,
            "L",
            "campaign_group",
            **retired.scope,
            account_id=account,
            external_id="l-group",
            name="l group",
            status="ACTIVE",
            daily_budget=Decimal("80.000000"),
        )
        campaign = _campaign(
            ledger,
            conn,
            "L",
            retired,
            "l",
            account,
            campaign_group_id=group,
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("50.000000"),
        )
        creative = ledger.put(
            conn,
            "L",
            "creative",
            **retired.scope,
            campaign_id=campaign,
            urn="urn:li:sponsoredCreative:9001",
            intended_status="ACTIVE",
            headline="The 2026 benchmark report",
            cta_label="DOWNLOAD",
            lead_form_urn="urn:li:leadGenForm:7001",
        )
        form = ledger.put(
            conn,
            "L",
            "lead_form",
            **retired.scope,
            canonical_urn="urn:li:leadGenForm:7001",
            name="Benchmark report",
            current_version=2,
        )
        form_version = ledger.put(
            conn,
            "L",
            "lead_form_version",
            **retired.scope,
            lead_form_id=form,
            version=2,
            email_question_ids=["q-email"],
            first_seen_at=T0,
        )
        ledger.put(
            conn,
            "L",
            "fact_campaign_daily",
            **retired.scope,
            campaign_id=campaign,
            date=DAY,
            cost=Decimal("48.250000"),
            impressions=5200,
            clicks=41,
            fetched_at=T0,
        )
        ledger.put(
            conn,
            "L",
            "fact_creative_daily",
            **retired.scope,
            creative_id=creative,
            date=DAY,
            cost=Decimal("48.250000"),
            impressions=5200,
            clicks=41,
            form_opens=12,
            leads=3,
            fetched_at=T0,
        )
        ledger.put(
            conn,
            "L",
            "fact_segment_window",
            **retired.scope,
            campaign_id=campaign,
            facet="seniority",
            value="urn:li:seniority:5",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            impressions=900,
            clicks=11,
            cost=Decimal("120.000000"),
            leads=6,
            fetched_at=T0,
        )
        campaign_snapshot = ledger.put(
            conn,
            "L",
            "entity_snapshot",
            **retired.scope,
            entity_type="campaign",
            entity_id=campaign,
            captured_at=T0,
            status="paused",
            budget_daily=Decimal("50.000000"),
        )
        ledger.put(
            conn,
            "L",
            "entity_snapshot",
            **retired.scope,
            entity_type="creative",
            entity_id=creative,
            captured_at=T0,
            status="ACTIVE",
        )
        ledger.put(
            conn,
            "L",
            "entity_snapshot",
            **retired.scope,
            entity_type="campaign_group",
            entity_id=group,
            captured_at=T0,
            status="ACTIVE",
            budget_daily=Decimal("80.000000"),
        )
        lead_one = _lead(ledger, conn, "L", retired, "l1", campaign, creative, EMAIL_HASH)
        lead_two = _lead(ledger, conn, "L", retired, "l2", campaign, creative, OTHER_EMAIL_HASH)
        decision = _decision(
            ledger,
            conn,
            "L",
            retired,
            shape="own_budget",
            subject_type="campaign",
            subject_id=campaign,
            status="active",
            daily_budget=Decimal("50.000000"),
        )
        ledger.put(
            conn,
            "L",
            "discrepancy",
            **retired.scope,
            subject_type="campaign",
            subject_id=campaign,
            kind="status",
            decided_value={"status": "active"},
            platform_value={"status": "paused"},
            snapshot_id=campaign_snapshot,
            decision_id=decision,
            detected_at=T0,
            state="open",
        )

        # The companion CRM dataset: contacts, meetings, deals, associations, matches.
        companion_contact = _contact(ledger, conn, "C", companion, "9000000001", EMAIL_HASH)
        _crm_pipeline(ledger, conn, "C", companion, "9000000001")
        ledger.freshness(conn, "C", companion, "hubspot.contacts")
        ledger.match(
            conn,
            "C",
            lead_id=lead_one,
            crm=companion,
            status="matched",
            contact_id=companion_contact,
        )

        # Cross-dataset matches of the retired leads against the live HubSpot dataset: they
        # belong to the retired leads, so they go with them.
        live_contact = _contact(ledger, conn, "H", hubspot, "40100", EMAIL_HASH)
        ledger.match(
            conn, "L", lead_id=lead_one, crm=hubspot, status="matched", contact_id=live_contact
        )
        ledger.match(
            conn,
            "L",
            lead_id=lead_two,
            crm=hubspot,
            status="no_match",
            attempts=2,
            next_attempt_at=T0 + timedelta(hours=6),
        )

        # Bystanders: the live LinkedIn and Google datasets and the live CRM's own match.
        live_account = _account(ledger, conn, "L2", live_linkedin, "l2")
        live_campaign = _campaign(ledger, conn, "L2", live_linkedin, "l2", live_account)
        live_lead = _lead(
            ledger, conn, "L2", live_linkedin, "l2-1", live_campaign, None, EMAIL_HASH
        )
        ledger.match(
            conn, "H", lead_id=live_lead, crm=hubspot, status="matched", contact_id=live_contact
        )
        google_account = _account(ledger, conn, "G", google, "g")
        _campaign(ledger, conn, "G", google, "g", google_account)
        connection_row = ledger.put(
            conn,
            "ops",
            "source_connection",
            source="linkedin",
            mode="live",
            status="ok",
            active_dataset_id=live_linkedin.id,
        )

        # Run history computed from the retired datasets.
        run = _run(ledger, conn, "runs", {"linkedin": retired.id, "hubspot": companion.id})
        evidence = {
            "metrics": [{"key": "spend", "value": "120.00"}],
            "provenance": [
                {
                    "source": "linkedin",
                    "dataset_id": retired.id,
                    "mode": "simulated",
                    "role": "primary",
                }
            ],
        }
        recommendation = _recommendation(
            ledger, conn, "runs", run, evidence, campaign_id=campaign, creative_id=creative
        )
        finding = ledger.put(
            conn,
            "runs",
            "run_finding",
            run_id=run,
            section="white_papers",
            code="skipped_stage",
            entity={"type": "campaign", "name": "l campaign"},
            message="2 contacts skipped this stage (not counted)",
            evidence={"skipped": 2},
            campaign_id=campaign,
        )
        withheld = ledger.put(
            conn,
            "runs",
            "run_withheld",
            run_id=run,
            section="creative",
            rule_id="CR1",
            entity={"type": "creative", "name": "The 2026 benchmark report"},
            reason="impressions 640 of 1000 required",
            observed=Decimal("640"),
            required=Decimal("1000"),
            unit="impressions",
            creative_id=creative,
        )
        suppression = ledger.put(
            conn, "runs", "run_suppression", run_id=run, section="campaigns", reason="g1", count=2
        )
        feedback = ledger.put(
            conn,
            "runs",
            "recommendation_feedback",
            recommendation_id=recommendation,
            rule_id="C1",
            entity_type="campaign",
            entity_id="linkedin:campaign:l-campaign",
            kind="snooze",
            until=T0 + timedelta(days=7),
            created_by="admin@example.com",
        )
        history = {
            ("analysis_run", run): {},
            ("recommendation", recommendation): {"campaign_id": None, "creative_id": None},
            ("run_finding", finding): {"campaign_id": None},
            ("run_withheld", withheld): {"creative_id": None},
            ("run_suppression", suppression): {},
            ("recommendation_feedback", feedback): {},
        }
        before = {key: _require_row(conn, key[0], id=key[1]) for key in history}
        assert before[("recommendation", recommendation)]["campaign_id"] == campaign

        _purge(conn, retired)

        assert not _exists(conn, "source_dataset", id=companion.id), "companion not purged"
        purged_ids = [retired.id, companion.id]
        remaining = {
            table: _count(conn, table, _table(table).c.dataset_id.in_(purged_ids))
            for table in _tables_with_dataset_id(conn)
        }
        orphans = {table: count for table, count in remaining.items() if count}
        assert not orphans, f"rows of the purged datasets remain: {orphans}"
        lead_match = _table("lead_match")
        assert _count(conn, "lead_match", lead_match.c.crm_dataset_id.in_(purged_ids)) == 0
        assert _count(conn, "lead_match", lead_match.c.lead_id.in_([lead_one, lead_two])) == 0
        assert not _exists(conn, "lead_form_version", id=form_version)

        left, lost = _left_and_lost(
            conn, ledger, gone=("L", "C"), kept=("L2", "H", "G", "ops", "runs")
        )
        assert not left, f"rows of the purged datasets remain: {left}"
        assert not lost, f"rows of other datasets or run history were deleted: {lost}"
        assert not [key for key in ledger.tracked_matches("L", "C") if _match_exists(conn, key)]
        assert all(_match_exists(conn, key) for key in ledger.tracked_matches("H"))
        assert _require_row(conn, "source_connection", id=connection_row)["active_dataset_id"] == (
            live_linkedin.id
        )

        for (table, row_id), nulled in history.items():
            after = _require_row(conn, table, id=row_id)
            assert after == {**before[(table, row_id)], **nulled}, (table, after)
        frozen = _require_row(conn, "recommendation", id=recommendation)
        assert frozen["evidence"] == evidence
        assert frozen["entity_id"] == "linkedin:campaign:l-campaign"
        assert frozen["rule_id"] == "C1"
        assert _require_row(conn, "analysis_run", id=run)["dataset_ids"] == {
            "linkedin": retired.id,
            "hubspot": companion.id,
        }


def _google_entities(
    ledger: Ledger, conn: Connection, label: str, dataset: Dataset, key: str
) -> dict[str, int]:
    """Insert a Google account, shared budget, member campaign, ad group and keyword.

    Returns:
        The inserted ids keyed ``account``, ``budget``, ``campaign``, ``ad_group`` and
        ``keyword``.
    """
    account = _account(ledger, conn, label, dataset, key)
    budget = ledger.put(
        conn,
        label,
        "budget",
        **dataset.scope,
        external_id=f"{key}-budget",
        amount=Decimal("100.000000"),
        amount_micros=100_000_000,
        period="DAILY",
        explicitly_shared=True,
    )
    campaign = _campaign(
        ledger,
        conn,
        label,
        dataset,
        key,
        account,
        objective_or_channel="SEARCH",
        budget_id=budget,
        daily_budget=Decimal("100.000000"),
        budget_period="DAILY",
    )
    ad_group = ledger.put(
        conn,
        label,
        "ad_group",
        **dataset.scope,
        campaign_id=campaign,
        external_id=f"{key}-ad-group",
        name=f"{key} ad group",
        status="ENABLED",
    )
    keyword = ledger.put(
        conn,
        label,
        "keyword",
        **dataset.scope,
        ad_group_id=ad_group,
        criterion_id=f"{key}-criterion",
        text="benchmark report",
        match_type="PHRASE",
        status="ENABLED",
        quality_score=7,
    )
    return {
        "account": account,
        "budget": budget,
        "campaign": campaign,
        "ad_group": ad_group,
        "keyword": keyword,
    }


def test_purge_retired_google_dataset(sync_engine: Engine) -> None:
    """AAP 0.5.2 intra-dataset cascades for the Google-only tables, and SET NULL pointers.

    Purging a retired Google dataset removes its budgets, campaigns, ad groups, keywords,
    conversion actions, facts, snapshots, decisions (both shared-budget shapes),
    discrepancies and freshness rows; the active Google dataset is untouched. The
    ``source_connection`` row that still pointed at the purged dataset survives with
    ``active_dataset_id`` NULL, and the recommendation computed from it survives with all
    four typed keys NULL and its evidence intact (AAP 0.3.2, 0.3.3).
    """
    with _scratch_transaction(sync_engine) as conn:
        ledger = Ledger()
        old = ledger.dataset(conn, "G_old", source="google", mode="live", state="retired")
        new = ledger.dataset(conn, "G_new", source="google", mode="live", state="active")

        entities = _google_entities(ledger, conn, "G_old", old, "g-old")
        conversion_action = ledger.put(
            conn,
            "G_old",
            "conversion_action",
            **old.scope,
            external_id="g-old-conversion",
            name="Demo request",
            status="ENABLED",
            include_in_conversions_metric=True,
            counting_type="ONE_PER_CLICK",
            attribution_model="GOOGLE_ADS_LAST_CLICK",
        )
        for name in ("google.entities", "google.metrics_daily", "google.search_terms"):
            ledger.freshness(conn, "G_old", old, name)
        ledger.put(
            conn,
            "G_old",
            "fact_campaign_daily",
            **old.scope,
            campaign_id=entities["campaign"],
            date=DAY,
            cost=Decimal("97.410000"),
            impressions=3100,
            clicks=88,
            conversions=Decimal("2.500000"),
            budget_lost_is=Decimal("0.120000"),
            fetched_at=T0,
        )
        ledger.put(
            conn,
            "G_old",
            "fact_keyword_daily",
            **old.scope,
            keyword_id=entities["keyword"],
            date=DAY,
            cost=Decimal("31.200000"),
            impressions=900,
            clicks=24,
            conversions=Decimal("1.000000"),
            historical_quality_score=7,
            average_cpc=Decimal("1.300000"),
            fetched_at=T0,
        )
        ledger.put(
            conn,
            "G_old",
            "fact_search_term_daily",
            **old.scope,
            ad_group_id=entities["ad_group"],
            campaign_id=entities["campaign"],
            date=DAY,
            search_term="benchmark report pdf",
            term_status="NONE",
            matched_criterion_id="g-old-criterion",
            matched_keyword_text="benchmark report",
            matched_match_type="PHRASE",
            cost=Decimal("12.000000"),
            impressions=210,
            clicks=9,
            conversions=Decimal("0.000000"),
            fetched_at=T0,
        )
        budget_snapshot = ledger.put(
            conn,
            "G_old",
            "entity_snapshot",
            **old.scope,
            entity_type="budget",
            entity_id=entities["budget"],
            captured_at=T0,
            budget_daily=Decimal("120.000000"),
            budget_amount_micros=120_000_000,
            payload={"period": "DAILY", "explicitly_shared": True},
        )
        ledger.put(
            conn,
            "G_old",
            "entity_snapshot",
            **old.scope,
            entity_type="campaign",
            entity_id=entities["campaign"],
            captured_at=T0,
            status="active",
            budget_daily=Decimal("120.000000"),
            budget_amount_micros=120_000_000,
            budget_id=entities["budget"],
        )
        ledger.put(
            conn,
            "G_old",
            "entity_snapshot",
            **old.scope,
            entity_type="keyword",
            entity_id=entities["keyword"],
            captured_at=T0,
            status="ENABLED",
            quality_score=6,
        )
        ledger.put(
            conn,
            "G_old",
            "entity_snapshot",
            **old.scope,
            entity_type="conversion_action",
            entity_id=conversion_action,
            captured_at=T0,
            status="ENABLED",
            payload={
                "include_in_conversions_metric": True,
                "counting_type": "ONE_PER_CLICK",
                "attribution_model": "GOOGLE_ADS_LAST_CLICK",
            },
        )
        member_decision = _decision(
            ledger,
            conn,
            "G_old",
            old,
            shape="shared_member",
            subject_type="campaign",
            subject_id=entities["campaign"],
            status="active",
            budget_id=entities["budget"],
        )
        budget_decision = _decision(
            ledger,
            conn,
            "G_old",
            old,
            shape="budget",
            subject_type="budget",
            subject_id=entities["budget"],
            daily_budget=Decimal("100.000000"),
        )
        ledger.put(
            conn,
            "G_old",
            "discrepancy",
            **old.scope,
            subject_type="budget",
            subject_id=entities["budget"],
            kind="budget",
            decided_value={"amount_micros": 100_000_000},
            platform_value={"amount_micros": 120_000_000},
            snapshot_id=budget_snapshot,
            decision_id=budget_decision,
            detected_at=T0,
            state="open",
        )
        # A stale pointer: the connection still names the dataset being purged.
        connection_row = ledger.put(
            conn,
            "ops",
            "source_connection",
            source="google",
            mode="live",
            status="ok",
            active_dataset_id=old.id,
        )

        # The bystander: the active Google dataset with its own shared-budget decision.
        kept = _google_entities(ledger, conn, "G_new", new, "g-new")
        _decision(
            ledger,
            conn,
            "G_new",
            new,
            shape="budget",
            subject_type="budget",
            subject_id=kept["budget"],
            daily_budget=Decimal("100.000000"),
        )
        ledger.freshness(conn, "G_new", new, "google.entities")

        run = _run(ledger, conn, "runs", {"google": old.id})
        evidence = {
            "metrics": [{"key": "utilization", "value": "0.97"}],
            "provenance": [
                {"source": "google", "dataset_id": old.id, "mode": "live", "role": "primary"}
            ],
        }
        recommendation = _recommendation(
            ledger,
            conn,
            "runs",
            run,
            evidence,
            campaign_id=entities["campaign"],
            budget_id=entities["budget"],
            ad_group_id=entities["ad_group"],
            keyword_id=entities["keyword"],
        )
        decisions = _table("decision")
        conn.execute(
            sa.update(decisions)
            .where(decisions.c.id == member_decision)
            .values(recommendation_id=recommendation, origin="recommendation")
        )
        before = _require_row(conn, "recommendation", id=recommendation)

        _purge(conn, old)

        left, lost = _left_and_lost(conn, ledger, gone=("G_old",), kept=("G_new", "ops", "runs"))
        assert not left, f"rows of the purged dataset remain: {left}"
        assert not lost, f"rows of the active dataset or run history were deleted: {lost}"
        remaining = {
            table: _count(conn, table, _table(table).c.dataset_id == old.id)
            for table in _tables_with_dataset_id(conn)
        }
        assert not {table: count for table, count in remaining.items() if count}, remaining
        assert (
            _require_row(conn, "source_connection", id=connection_row)["active_dataset_id"] is None
        )
        after = _require_row(conn, "recommendation", id=recommendation)
        nulled = dict.fromkeys(("campaign_id", "budget_id", "ad_group_id", "keyword_id"))
        assert after == {**before, **nulled}
        assert after["evidence"] == evidence


def test_purge_retired_hubspot_primary_keeps_active_matches(sync_engine: Engine) -> None:
    """AAP 0.3.3 "Purging a retired HubSpot dataset deletes only its own lead_match rows".

    The active LinkedIn leads keep the match rows staged for the active CRM dataset before it
    activated: those rows are neither deleted nor requeued (status, attempts, contact and
    next attempt unchanged), and the leads and active contacts are untouched.
    """
    with _scratch_transaction(sync_engine) as conn:
        ledger = Ledger()
        linkedin = ledger.dataset(conn, "LA", source="linkedin", mode="live", state="active")
        retired = ledger.dataset(conn, "R", source="hubspot", mode="live", state="retired")
        active = ledger.dataset(conn, "H", source="hubspot", mode="live", state="active")

        account = _account(ledger, conn, "LA", linkedin, "la")
        campaign = _campaign(
            ledger, conn, "LA", linkedin, "la", account, objective_or_channel="LEAD_GENERATION"
        )
        creative = ledger.put(
            conn,
            "LA",
            "creative",
            **linkedin.scope,
            campaign_id=campaign,
            urn="urn:li:sponsoredCreative:9101",
            lead_form_urn="urn:li:leadGenForm:7001",
        )
        lead_one = _lead(ledger, conn, "LA", linkedin, "la1", campaign, creative, EMAIL_HASH)
        lead_two = _lead(ledger, conn, "LA", linkedin, "la2", campaign, None, OTHER_EMAIL_HASH)

        retired_contact = _contact(ledger, conn, "R", retired, "500001", EMAIL_HASH)
        _contact(ledger, conn, "R", retired, "500002", OTHER_EMAIL_HASH)
        _crm_pipeline(ledger, conn, "R", retired, "500001")
        ledger.freshness(conn, "R", retired, "hubspot.contacts")
        ledger.freshness(conn, "R", retired, "hubspot.associations")
        ledger.match(
            conn, "R", lead_id=lead_one, crm=retired, status="matched", contact_id=retired_contact
        )
        ledger.match(
            conn,
            "R",
            lead_id=lead_two,
            crm=retired,
            status="no_match",
            attempts=2,
            next_attempt_at=T0 + timedelta(hours=6),
        )

        active_contact = _contact(ledger, conn, "H", active, "600001", EMAIL_HASH)
        _contact(ledger, conn, "H", active, "600002", OTHER_EMAIL_HASH)
        ledger.freshness(conn, "H", active, "hubspot.contacts")
        ledger.match(
            conn, "H", lead_id=lead_one, crm=active, status="matched", contact_id=active_contact
        )
        ledger.match(
            conn,
            "H",
            lead_id=lead_two,
            crm=active,
            status="pending",
            attempts=0,
            next_attempt_at=T0,
        )
        kept_matches = {
            key: _require_row(conn, "lead_match", lead_id=key[0], crm_dataset_id=key[1])
            for key in ledger.tracked_matches("H")
        }
        kept_rows = {
            entry: _require_row(conn, entry[0], id=entry[1]) for entry in ledger.tracked("LA", "H")
        }

        _purge(conn, retired)

        lead_match = _table("lead_match")
        assert _count(conn, "lead_match", lead_match.c.crm_dataset_id == retired.id) == 0
        assert not [key for key in ledger.tracked_matches("R") if _match_exists(conn, key)]
        left, lost = _left_and_lost(conn, ledger, gone=("R",), kept=("LA", "H"))
        assert not left, f"rows of the purged CRM dataset remain: {left}"
        assert not lost, f"rows of the active datasets were deleted: {lost}"
        for key, row in kept_matches.items():
            assert _require_row(conn, "lead_match", lead_id=key[0], crm_dataset_id=key[1]) == row
        for (table, row_id), row in kept_rows.items():
            assert _require_row(conn, table, id=row_id) == row, (table, row_id)
        assert _count(conn, "lead_match") == len(kept_matches)


def test_deleting_analysis_run_cascades_outputs_and_nulls_references(sync_engine: Engine) -> None:
    """AAP 0.5.2: run outputs cascade from their run; "recommendation_feedback references
    recommendation only"; keys into run history from decisions and requests are SET NULL.

    Deleting a run removes its recommendations, findings, withheld lines, suppression counts
    and the feedback on its items, while the decision recorded from one of its items and the
    analysis request it served survive with their reference NULL.
    """
    with _scratch_transaction(sync_engine) as conn:
        ledger = Ledger()
        google = ledger.dataset(conn, "G", source="google", mode="live", state="active")
        account = _account(ledger, conn, "G", google, "g")
        campaign = _campaign(
            ledger,
            conn,
            "G",
            google,
            "g",
            account,
            daily_budget=Decimal("40.000000"),
            budget_period="DAILY",
        )
        run = _run(ledger, conn, "run", {"google": google.id})
        recommendation = _recommendation(
            ledger, conn, "run", run, {"metrics": []}, campaign_id=campaign
        )
        ledger.put(
            conn,
            "run",
            "run_finding",
            run_id=run,
            section="keywords",
            code="search_term_coverage",
            message="Search terms cover 74% of keyword spend",
        )
        ledger.put(
            conn,
            "run",
            "run_withheld",
            run_id=run,
            section="budget",
            rule_id="B4",
            reason="3 weekend days of 4 required",
            observed=Decimal("3"),
            required=Decimal("4"),
            unit="days",
            campaign_id=campaign,
        )
        ledger.put(
            conn,
            "run",
            "run_suppression",
            run_id=run,
            section="keywords",
            reason="dismiss",
            count=1,
        )
        ledger.put(
            conn,
            "run",
            "recommendation_feedback",
            recommendation_id=recommendation,
            rule_id="C1",
            entity_type="campaign",
            entity_id="google:campaign:g-campaign",
            kind="done",
            until=T0 + timedelta(days=30),
        )
        decision = _decision(
            ledger,
            conn,
            "G",
            google,
            shape="own_budget",
            subject_type="campaign",
            subject_id=campaign,
            status="paused",
            daily_budget=Decimal("40.000000"),
            recommendation_id=recommendation,
        )
        request = ledger.put(
            conn, "G", "analysis_request", reason="decision", picked_at=T0, run_id=run
        )
        runs = _table("analysis_run")

        deleted = conn.execute(sa.delete(runs).where(runs.c.id == run))

        assert deleted.rowcount == 1
        left, lost = _left_and_lost(conn, ledger, gone=("run",), kept=("G",))
        assert not left, f"run outputs survived their run: {left}"
        assert not lost, f"rows outside the run were deleted: {lost}"
        assert _require_row(conn, "decision", id=decision)["recommendation_id"] is None
        assert _require_row(conn, "analysis_request", id=request)["run_id"] is None
        assert _require_row(conn, "campaign", id=campaign)["status"] == "active"


# ---------------------------------------------------------------------------------------------
# 11-12: partial unique indexes and the dataset role shape
# ---------------------------------------------------------------------------------------------

_PARTIAL_UNIQUE_SQL = sa.text(
    "SELECT ic.relname, t.relname, i.indisunique, pg_get_expr(i.indpred, i.indrelid), "
    "ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
    "ORDER BY k.ord) "
    "FROM pg_index AS i "
    "JOIN pg_class AS ic ON ic.oid = i.indexrelid "
    "JOIN pg_class AS t ON t.oid = i.indrelid "
    "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
    "WHERE n.nspname = current_schema() AND i.indisunique AND i.indpred IS NOT NULL"
)


def _analysis_run_values(status: str) -> dict[str, object]:
    """Return the columns of a valid default ``analysis_run`` row with ``status``."""
    return {
        "kind": "default",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "source_day_boundaries": [],
        "trigger": "scheduled",
        "data_modes": {},
        "dataset_ids": {},
        "config_hash": "0" * 64,
        "status": status,
        "started_at": T0,
    }


def test_partial_unique_indexes_exist_and_enforce(sync_engine: Engine) -> None:
    """AAP 0.5.2 / 0.5.3: the four partial unique indexes exist and enforce exactly their scope.

    One active primary dataset per source (companions excluded), one pending sync request per
    source, one running analysis run, and one non-resolved discrepancy per (subject, kind),
    while picked, finished, retired and resolved rows accumulate as history. Each rejected
    insert runs in a savepoint, so the enclosing transaction stays usable.
    """
    with _scratch_transaction(sync_engine) as conn:
        found = {
            name: (PartialUnique(table, tuple(columns), predicate), unique)
            for name, table, unique, predicate, columns in conn.execute(_PARTIAL_UNIQUE_SQL)
        }
        assert set(found) == set(PARTIAL_UNIQUE_INDEXES), sorted(found)
        for name, expected in PARTIAL_UNIQUE_INDEXES.items():
            assert found[name] == (expected, True), (name, found[name])

        # Datasets: one active primary per source; retired and companion rows do not count.
        ledger = Ledger()
        google = ledger.dataset(conn, "x", source="google", mode="live", state="active")
        _assert_rejected(
            conn,
            UNIQUE_VIOLATION,
            "uq_source_dataset_active_primary",
            partial(
                _insert,
                conn,
                "source_dataset",
                source="google",
                mode="simulated",
                state="active",
                role="primary",
            ),
        )
        _accepted(
            conn,
            partial(
                _insert,
                conn,
                "source_dataset",
                source="google",
                mode="simulated",
                state="retired",
                role="primary",
            ),
        )
        parent = ledger.dataset(conn, "x", source="linkedin", mode="simulated", state="active")
        ledger.dataset(conn, "x", source="hubspot", mode="live", state="active")
        _accepted(
            conn,
            partial(
                _insert,
                conn,
                "source_dataset",
                source="hubspot",
                mode="simulated",
                state="active",
                role="companion",
                parent_dataset_id=parent.id,
            ),
        )

        # Sync requests: one pending per source; picked requests are history.
        _insert(conn, "sync_request", source="google")
        _assert_rejected(
            conn,
            UNIQUE_VIOLATION,
            "uq_sync_request_pending",
            partial(_insert, conn, "sync_request", source="google"),
        )
        _accepted(conn, partial(_insert, conn, "sync_request", source="google", picked_at=T0))
        _accepted(conn, partial(_insert, conn, "sync_request", source="linkedin"))

        # Analysis runs: one running run across every process.
        _insert(conn, "analysis_run", **_analysis_run_values("running"))
        _assert_rejected(
            conn,
            UNIQUE_VIOLATION,
            "uq_analysis_run_single_running",
            partial(_insert, conn, "analysis_run", **_analysis_run_values("running")),
        )
        _accepted(conn, partial(_insert, conn, "analysis_run", **_analysis_run_values("succeeded")))
        _accepted(conn, partial(_insert, conn, "analysis_run", **_analysis_run_values("failed")))

        # Discrepancies: one open or awaiting_platform row per (subject, kind).
        account = _account(ledger, conn, "x", google, "g")
        campaign = _campaign(ledger, conn, "x", google, "g", account)

        def discrepancy(kind: str, state: str, subject_type: str = "campaign") -> int:
            return _insert(
                conn,
                "discrepancy",
                **google.scope,
                subject_type=subject_type,
                subject_id=campaign,
                kind=kind,
                detected_at=T0,
                state=state,
            )

        discrepancy("status", "open")
        _assert_rejected(
            conn,
            UNIQUE_VIOLATION,
            "uq_discrepancy_open_subject_kind",
            partial(discrepancy, "status", "awaiting_platform"),
        )
        _accepted(conn, partial(discrepancy, "status", "resolved"))
        _accepted(conn, partial(discrepancy, "budget", "open"))
        _accepted(conn, partial(discrepancy, "status", "open", "budget"))

        # The outer transaction is still usable after every rejected savepoint.
        assert _count(conn, "source_dataset") == 5
        assert _count(conn, "sync_request") == 3
        assert _count(conn, "analysis_run") == 3
        assert _count(conn, "discrepancy") == 4


def test_companion_shape_check(sync_engine: Engine) -> None:
    """AAP 0.5.2 "A companion is HubSpot, simulated, and has a LinkedIn parent".

    ``ck_source_dataset_role_shape`` rejects (SQLSTATE 23514) a LinkedIn companion, a live
    companion, a companion without a parent and a primary with a parent, and admits the one
    valid companion shape.
    """
    with _scratch_transaction(sync_engine) as conn:
        ledger = Ledger()
        parent = ledger.dataset(conn, "x", source="linkedin", mode="simulated", state="building")
        invalid: dict[str, dict[str, object]] = {
            "linkedin companion": {
                "source": "linkedin",
                "mode": "simulated",
                "role": "companion",
                "parent_dataset_id": parent.id,
            },
            "live companion": {
                "source": "hubspot",
                "mode": "live",
                "role": "companion",
                "parent_dataset_id": parent.id,
            },
            "companion without parent": {
                "source": "hubspot",
                "mode": "simulated",
                "role": "companion",
                "parent_dataset_id": None,
            },
            "primary with parent": {
                "source": "hubspot",
                "mode": "simulated",
                "role": "primary",
                "parent_dataset_id": parent.id,
            },
        }
        for case, values in invalid.items():
            assert values["role"] in DATASET_ROLES, case
            _assert_rejected(
                conn,
                CHECK_VIOLATION,
                "ck_source_dataset_role_shape",
                partial(_insert, conn, "source_dataset", state="building", **values),
            )
        _accepted(
            conn,
            partial(
                _insert,
                conn,
                "source_dataset",
                source="hubspot",
                mode="simulated",
                state="building",
                role="companion",
                parent_dataset_id=parent.id,
            ),
        )
        assert _count(conn, "source_dataset") == 2


# ---------------------------------------------------------------------------------------------
# 13-16: no drift between the migration and the models; naming and column conventions
# ---------------------------------------------------------------------------------------------


def test_migration_matches_orm_metadata(sync_engine: Engine) -> None:
    """No drift: ``compare_metadata`` (types and server defaults included) finds nothing.

    The migration is hand-written and imports nothing from ``ads_engine``, so this is what
    keeps it and ``Base.metadata`` in step; Alembic excludes its own ``alembic_version``.
    """
    with sync_engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], pprint.pformat(diff)


_COLUMNS_SQL = sa.text(
    "SELECT table_name::text, column_name::text, udt_name::text, character_maximum_length, "
    "numeric_precision, numeric_scale, is_nullable::text, column_default::text, "
    "is_identity::text, identity_generation::text "
    "FROM information_schema.columns WHERE table_schema = :schema AND table_name <> :skip"
)
_CONSTRAINTS_SQL = sa.text(
    "SELECT cl.relname::text, c.conname::text, c.contype::text, pg_get_constraintdef(c.oid) "
    "FROM pg_constraint AS c "
    "JOIN pg_class AS cl ON cl.oid = c.conrelid "
    "JOIN pg_namespace AS n ON n.oid = cl.relnamespace "
    "WHERE n.nspname = :schema AND c.contype IN ('p', 'f', 'u', 'c') AND cl.relname <> :skip"
)
_INDEXES_SQL = sa.text(
    "SELECT tablename::text, indexname::text, indexdef "
    "FROM pg_indexes WHERE schemaname = :schema AND tablename <> :skip"
)


def _ddl_shape(conn: Connection, schema: str) -> dict[str, set[tuple[Any, ...]]]:
    """Return the columns, constraints and indexes of ``schema`` with its name stripped.

    Columns leave out their ordinal position, because column order is not drift. NOT NULL
    constraints (``contype = 'n'`` in PostgreSQL 18) are left out, because nullability is
    already compared with the columns.
    """
    params = {"schema": schema, "skip": ALEMBIC_VERSION}
    columns = {tuple(row) for row in conn.execute(_COLUMNS_SQL, params)}
    constraints = {
        (table, name, kind, definition.replace(f"{schema}.", ""))
        for table, name, kind, definition in conn.execute(_CONSTRAINTS_SQL, params)
    }
    indexes = {
        (table, name, definition.replace(f" ON {schema}.", " ON "))
        for table, name, definition in conn.execute(_INDEXES_SQL, params)
    }
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def test_migration_ddl_matches_metadata_create_all(sync_engine: Engine) -> None:
    """No drift in what ``compare_metadata`` ignores: CHECK texts and partial-index predicates.

    ``Base.metadata.create_all`` builds every table into a probe schema inside a rolled-back
    transaction; its columns, constraint definitions and index definitions must equal the
    migrated schema's, so the hand-written migration repeats every CHECK and WHERE clause of
    the models verbatim. The probe schema never persists.
    """
    with sync_engine.connect() as conn:
        transaction = conn.begin()
        try:
            migrated_schema = _current_schema(conn)
            conn.execute(CreateSchema(PROBE_SCHEMA))
            probe = conn.execution_options(schema_translate_map={None: PROBE_SCHEMA})
            Base.metadata.create_all(probe, checkfirst=False)
            migrated = _ddl_shape(conn, migrated_schema)
            modelled = _ddl_shape(conn, PROBE_SCHEMA)
        finally:
            transaction.rollback()
    for part in ("columns", "constraints", "indexes"):
        only_migrated = sorted(migrated[part] - modelled[part], key=repr)
        only_modelled = sorted(modelled[part] - migrated[part], key=repr)
        assert migrated[part] == modelled[part], (
            f"{part} differ.\nonly in the migration:\n{pprint.pformat(only_migrated)}\n"
            f"only in Base.metadata:\n{pprint.pformat(only_modelled)}"
        )
        assert migrated[part], f"no {part} read from {migrated_schema}"
    with sync_engine.connect() as conn:
        assert not conn.execute(
            sa.text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": PROBE_SCHEMA}
        ).first()


_NAMED_CONSTRAINTS_SQL = sa.text(
    "SELECT cl.relname::text, c.conname::text, c.contype::text, rf.relname::text, "
    "(SELECT a.attname::text FROM pg_attribute AS a "
    "WHERE a.attrelid = c.conrelid AND a.attnum = c.conkey[1]) "
    "FROM pg_constraint AS c "
    "JOIN pg_class AS cl ON cl.oid = c.conrelid "
    "JOIN pg_namespace AS n ON n.oid = cl.relnamespace "
    "LEFT JOIN pg_class AS rf ON rf.oid = c.confrelid "
    "WHERE n.nspname = current_schema() AND c.contype IN ('p', 'f', 'u', 'c') "
    "AND cl.relname <> :skip"
)
_STANDALONE_INDEXES_SQL = sa.text(
    "SELECT t.relname::text, ic.relname::text, i.indisunique "
    "FROM pg_index AS i "
    "JOIN pg_class AS ic ON ic.oid = i.indexrelid "
    "JOIN pg_class AS t ON t.oid = i.indrelid "
    "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
    "WHERE n.nspname = current_schema() AND t.relname <> :skip "
    "AND NOT EXISTS (SELECT 1 FROM pg_constraint AS c "
    "WHERE c.conindid = i.indexrelid AND c.contype IN ('p', 'u', 'x'))"
)


def test_constraint_and_index_names_follow_convention(sync_engine: Engine) -> None:
    """``db/base.py`` ``NAMING_CONVENTION``: every name is deterministic and untruncated.

    Primary keys are ``pk_<table>``, foreign keys ``fk_<table>_<first column>_<referred>``,
    uniques ``uq_<table>_...``, checks ``ck_<table>_...``; every index that backs no
    constraint is ``ix_<table>_...``, or ``uq_<table>_...`` when unique. No name exceeds
    PostgreSQL's 63-character limit, so none was silently truncated.
    """
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}
    params = {"skip": ALEMBIC_VERSION}
    with sync_engine.connect() as conn:
        constraints = conn.execute(_NAMED_CONSTRAINTS_SQL, params).all()
        indexes = conn.execute(_STANDALONE_INDEXES_SQL, params).all()
    assert constraints, "no constraints read"
    assert indexes, "no standalone indexes read"

    violations: list[str] = []
    for table, name, kind, referred, first_column in constraints:
        expected_prefix = {"u": f"uq_{table}_", "c": f"ck_{table}_"}
        if kind == "p" and name != f"pk_{table}":
            violations.append(f"primary key {name} on {table}")
        elif kind == "f" and name != f"fk_{table}_{first_column}_{referred}":
            violations.append(f"foreign key {name} on {table}.{first_column} -> {referred}")
        elif kind in expected_prefix and not name.startswith(expected_prefix[kind]):
            violations.append(f"{kind} constraint {name} on {table}")
        if len(name) > PG_MAX_IDENTIFIER:
            violations.append(f"{name} is longer than {PG_MAX_IDENTIFIER} characters")
    for table, name, unique in indexes:
        prefix = f"uq_{table}_" if unique else f"ix_{table}_"
        if not name.startswith(prefix):
            violations.append(f"index {name} on {table} does not start with {prefix}")
        if len(name) > PG_MAX_IDENTIFIER:
            violations.append(f"{name} is longer than {PG_MAX_IDENTIFIER} characters")
    assert not violations, "\n".join(violations)


def test_schema_conventions(sync_engine: Engine) -> None:
    """Cross-cutting conventions: no ENUM types; lead emails persisted only as SHA-256 hashes.

    Enumerations are ``text`` plus ``CHECK`` (``db/base.check_in``), never PostgreSQL
    ``ENUM`` types. The only columns whose names mention email are the admin login, the two
    64-character hash columns and the form-version email question ids (AAP 0.3.2 "Personal
    data"), so no plaintext lead or contact email can be stored.
    """
    with sync_engine.connect() as conn:
        enums = conn.execute(
            sa.text(
                "SELECT t.typname::text FROM pg_type AS t "
                "JOIN pg_namespace AS n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = current_schema()"
            )
        ).all()
        email_columns = {
            (table, column): (udt, length)
            for table, column, udt, length in conn.execute(
                sa.text(
                    "SELECT table_name::text, column_name::text, udt_name::text, "
                    "character_maximum_length FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND column_name LIKE :pattern"
                ),
                {"pattern": "%email%"},
            )
        }
    assert enums == [], enums
    assert set(email_columns) == EMAIL_COLUMNS, sorted(set(email_columns) ^ EMAIL_COLUMNS)
    assert email_columns[("lead", "email_sha256")] == ("bpchar", 64)
    assert email_columns[("crm_contact", "email_sha256")] == ("bpchar", 64)
    assert email_columns[("lead_form_version", "email_question_ids")][0] == "_text"
