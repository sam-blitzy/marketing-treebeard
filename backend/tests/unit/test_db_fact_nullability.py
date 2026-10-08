"""Metadata tests: which daily fact metrics may be ``NULL``, and what their checks admit.

AAP 0.11.1 allows no silent default for a missing metric: an absent value is ``None`` and the
rules treat it as unavailable. Every Google row carries ``cost``, ``impressions`` and
``clicks``, so those columns are ``NOT NULL`` on ``fact_campaign_daily``,
``fact_keyword_daily`` and ``fact_search_term_daily``. A LinkedIn ``adAnalytics`` element may
omit any metric (``backend/tests/contracts/linkedin/ad_analytics.json``), so every metric of
``fact_creative_daily`` is nullable and an omitted one is stored as ``NULL``, never as zero;
its ``ck_fact_creative_daily_non_negative`` check still names every metric, because a
``NULL`` satisfies it. ``backend/migrations/versions/0001_initial_schema.py`` must mirror
these definitions.

AAP 0.5.2 puts a non-negative check on the metrics. PostgreSQL sorts a ``numeric`` ``NaN``
above every number, so every ``ck_<table>_non_negative`` check must also keep each
``numeric`` metric it covers from ``NaN``; ``tests/migrations/test_fact_metric_checks.py``
proves the effect on a migrated database.

Column DDL is compiled offline with the PostgreSQL dialect; no database is needed.
"""

import re

import pytest
from sqlalchemy import CheckConstraint, Numeric, Table
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.schema import CreateColumn

from ads_engine.db.base import Base, non_negative
from ads_engine.db.models import facts

# The project's own driver dialect, resolved through the typed URL API rather than the untyped
# ``postgresql.dialect()`` constructor, so this module also passes ``mypy --strict``.
_PG: Dialect = make_url("postgresql+psycopg://").get_dialect()()

# Each base metric and its PostgreSQL type, rendered exactly as ``CREATE TABLE`` writes it.
_BASE_METRIC_TYPES: tuple[tuple[str, str], ...] = (
    ("cost", "NUMERIC(18, 6)"),
    ("impressions", "BIGINT"),
    ("clicks", "BIGINT"),
)

_GOOGLE_DAILY_MODELS: tuple[type[Base], ...] = (
    facts.FactCampaignDaily,
    facts.FactKeywordDaily,
    facts.FactSearchTermDaily,
)

# Every metric column of ``fact_creative_daily``, in the order its non-negative check names.
_CREATIVE_METRICS: tuple[str, ...] = (
    "cost",
    "impressions",
    "clicks",
    "conversions",
    "conversions_value",
    "form_opens",
    "leads",
    "landing_page_clicks",
    "website_conversions",
)

# The ``numeric`` metrics of ``fact_creative_daily``, which its check also keeps from ``NaN``.
_CREATIVE_NUMERIC_METRICS: frozenset[str] = frozenset({"cost", "conversions", "conversions_value"})

# The tables whose metrics AAP 0.5.2 puts under a non-negative check.
_NON_NEGATIVE_TABLES: tuple[str, ...] = (
    "fact_campaign_daily",
    "fact_creative_daily",
    "fact_keyword_daily",
    "fact_search_term_daily",
    "fact_segment_window",
)

# The two condition shapes ``ads_engine.db.base.non_negative`` renders for a column.
_AT_LEAST_ZERO: re.Pattern[str] = re.compile(r"(?P<column>[a-z_][a-z0-9_]*) >= 0")
_NOT_NAN: re.Pattern[str] = re.compile(r"(?P<column>[a-z_][a-z0-9_]*) <> 'NaN'::numeric")


def _table(model: type[Base]) -> Table:
    """Return the ``Table`` a model class maps."""
    table = model.__table__
    assert isinstance(table, Table)
    return table


def _column_ddl(table: Table, name: str) -> str:
    """Render the ``CREATE TABLE`` clause of one column for PostgreSQL."""
    return str(CreateColumn(table.c[name]).compile(dialect=_PG)).strip()


@pytest.mark.parametrize(("column", "type_sql"), _BASE_METRIC_TYPES)
def test_creative_daily_base_metrics_are_nullable(column: str, type_sql: str) -> None:
    """``fact_creative_daily`` keeps each base metric's type and accepts ``NULL``."""
    table = _table(facts.FactCreativeDaily)
    assert table.c[column].nullable is True
    assert _column_ddl(table, column) == f"{column} {type_sql}"


@pytest.mark.parametrize("model", _GOOGLE_DAILY_MODELS, ids=lambda model: model.__tablename__)
@pytest.mark.parametrize(("column", "type_sql"), _BASE_METRIC_TYPES)
def test_google_daily_base_metrics_stay_not_null(
    model: type[Base], column: str, type_sql: str
) -> None:
    """The three Google-fed daily tables keep each base metric ``NOT NULL``."""
    table = _table(model)
    assert table.c[column].nullable is False
    assert _column_ddl(table, column) == f"{column} {type_sql} NOT NULL"


def test_creative_daily_requires_only_its_key_and_provenance_columns() -> None:
    """Every ``fact_creative_daily`` metric is nullable; only identity, key and fetch time
    are required."""
    table = _table(facts.FactCreativeDaily)
    assert {str(column.name) for column in table.columns if not column.nullable} == {
        "id",
        "source",
        "dataset_id",
        "creative_id",
        "date",
        "fetched_at",
    }
    assert all(table.c[name].nullable for name in _CREATIVE_METRICS)


def test_creative_daily_non_negative_check_still_guards_every_metric() -> None:
    """The non-negative check still names every metric, the base metrics included, and keeps
    each ``numeric`` one from ``NaN``."""
    checks = {
        str(constraint.name): str(constraint.sqltext)
        for constraint in _table(facts.FactCreativeDaily).constraints
        if isinstance(constraint, CheckConstraint)
    }
    conditions: list[str] = []
    for name in _CREATIVE_METRICS:
        conditions.append(f"{name} >= 0")
        if name in _CREATIVE_NUMERIC_METRICS:
            conditions.append(f"{name} <> 'NaN'::numeric")
    assert checks["ck_fact_creative_daily_non_negative"] == " AND ".join(conditions)


def _non_negative_checks() -> dict[str, tuple[Table, str]]:
    """Return every ``*_non_negative`` check of ``Base.metadata``: its table and its SQL text."""
    found: dict[str, tuple[Table, str]] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and str(constraint.name).endswith(
                "_non_negative"
            ):
                found[str(constraint.name)] = (table, str(constraint.sqltext))
    return found


def test_the_fact_tables_carry_the_non_negative_checks() -> None:
    """The five fact tables each declare ``ck_<table>_non_negative``, so the parity test below
    never passes vacuously."""
    assert {f"ck_{name}_non_negative" for name in _NON_NEGATIVE_TABLES} <= set(
        _non_negative_checks()
    )


@pytest.mark.parametrize("name", sorted(_non_negative_checks()))
def test_every_non_negative_check_keeps_exactly_its_numeric_metrics_from_nan(name: str) -> None:
    """Each ``numeric`` metric a non-negative check covers is also kept from ``NaN``, which
    PostgreSQL sorts above every number, and no other column is: a new ``Decimal`` metric
    cannot miss the guard."""
    table, sqltext = _non_negative_checks()[name]
    covered: list[str] = []
    guarded: list[str] = []
    for condition in sqltext.split(" AND "):
        if (match := _AT_LEAST_ZERO.fullmatch(condition)) is not None:
            covered.append(match["column"])
        elif (match := _NOT_NAN.fullmatch(condition)) is not None:
            guarded.append(match["column"])
        else:
            pytest.fail(f"{name}: unexpected condition {condition!r}")
    numeric = [column for column in covered if isinstance(table.c[column].type, Numeric)]
    assert guarded == numeric
    assert sqltext == str(non_negative(*covered, numeric=numeric).sqltext)
