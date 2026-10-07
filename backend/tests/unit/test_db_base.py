"""Unit tests for ``ads_engine.db.base``.

They cover the declarative ``Base``, the constraint naming convention, the annotation map and
column aliases, the provenance mixins and their column order, and the ``CHECK`` builders.

Every mapped class lives on a throwaway registry or declarative base configured from ``Base``
itself (its naming convention and its annotation map), so ``Base.metadata`` and
``Base.registry`` never gain a test table or mapper; an autouse fixture asserts that after
every test. DDL is rendered offline with the PostgreSQL dialect, so no database is needed.
"""

import ast
import asyncio
import doctest
import inspect
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, get_args, get_origin

import pytest
from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column, registry
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.types import TypeEngine

from ads_engine.db import base
from ads_engine.db.base import (
    NAMING_CONVENTION,
    Base,
    DatasetScopedMixin,
    IdPkMixin,
    Instant,
    JsonB,
    Money,
    NativeDate,
    TextArray,
    check_in,
    non_negative,
    utcnow,
)

_PG = postgresql.dialect()

_SOURCES = ("google", "linkedin", "hubspot")
_CAMPAIGN_STATUSES = ("active", "paused", "ended")

_EXPECTED_TYPE_SQL: dict[Any, str] = {
    Decimal: "NUMERIC(18, 6)",
    datetime: "TIMESTAMP WITH TIME ZONE",
    date: "DATE",
    str: "TEXT",
    int: "BIGINT",
    bool: "BOOLEAN",
    dict[str, Any]: "JSONB",
    list[str]: "TEXT[]",
}

# (column name, how both CHECK builders must render it). Bare only when PostgreSQL accepts the
# name unquoted: lower-case letters, digits, ``_`` or ``$``, no leading digit or ``$``, and not
# a reserved word.
_IDENTIFIER_CASES = [
    ("status", "status"),
    ("dataset_id", "dataset_id"),
    ("date", "date"),
    ("_internal", "_internal"),
    ("cost$1", "cost$1"),
    ("group", '"group"'),
    ("order", '"order"'),
    ("user", '"user"'),
    ("window", '"window"'),
    ("Status", '"Status"'),
    ("1st_day", '"1st_day"'),
    ("ad-group", '"ad-group"'),
    ("x);--", '"x);--"'),
]

_UNSAFE_TOKENS = [
    "it's",
    'say"hi"',
    "back\\slash",
    "two words",
    "tab\tbed",
    "line\nbreak",
    "\u00a0x",
]

# The clause that follows ``CONSTRAINT fk_<table>_dataset_id_source_dataset`` in every
# dataset-scoped table's DDL.
_SOURCE_DATASET_FK = "FOREIGN KEY(dataset_id) REFERENCES source_dataset (id) ON DELETE CASCADE"


def _mirror_registry() -> registry:
    """Return an empty registry configured exactly as ``Base`` configures its own."""
    return registry(
        metadata=MetaData(naming_convention=Base.metadata.naming_convention),
        type_annotation_map=Base.registry.type_annotation_map,
    )


def _map_source_dataset(reg: registry) -> None:
    """Map ``source_dataset``, the table ``DatasetScopedMixin.dataset_id`` references."""

    @reg.mapped
    class SourceDataset(IdPkMixin):
        __tablename__ = "source_dataset"

        mode: Mapped[str]


def _ddl_lines(table: Table) -> list[str]:
    """Render ``CREATE TABLE`` for PostgreSQL as stripped lines without trailing commas."""
    ddl = str(CreateTable(table).compile(dialect=_PG))
    return [line.strip().removesuffix(",") for line in ddl.splitlines() if line.strip()]


def _index_ddl(table: Table) -> list[str]:
    """Render ``CREATE INDEX`` for every index of ``table``, sorted."""
    return sorted(str(CreateIndex(index).compile(dialect=_PG)).strip() for index in table.indexes)


def _type_sql(type_: TypeEngine[Any]) -> str:
    """Render a column type as PostgreSQL DDL."""
    return str(type_.compile(dialect=_PG))


@pytest.fixture(autouse=True)
def shared_base_untouched() -> Iterator[None]:
    """Fail any test that leaves a table on ``Base.metadata`` or a mapper on ``Base.registry``."""
    tables = set(Base.metadata.tables)
    mappers = set(Base.registry.mappers)
    yield
    assert set(Base.metadata.tables) == tables
    assert set(Base.registry.mappers) == mappers


@pytest.fixture(scope="module")
def annotated_table() -> Table:
    """A table holding one required and one nullable column per annotation the base resolves."""
    reg = _mirror_registry()

    @reg.mapped
    class AnnotatedRow(IdPkMixin):
        __tablename__ = "annotated_row"

        bare_decimal: Mapped[Decimal]
        bare_decimal_or_none: Mapped[Decimal | None]
        bare_datetime: Mapped[datetime]
        bare_datetime_or_none: Mapped[datetime | None]
        bare_date: Mapped[date]
        bare_date_or_none: Mapped[date | None]
        bare_str: Mapped[str]
        bare_str_or_none: Mapped[str | None]
        bare_int: Mapped[int]
        bare_int_or_none: Mapped[int | None]
        bare_bool: Mapped[bool]
        bare_bool_or_none: Mapped[bool | None]
        bare_dict: Mapped[dict[str, Any]]
        bare_dict_or_none: Mapped[dict[str, Any] | None]
        bare_list: Mapped[list[str]]
        bare_list_or_none: Mapped[list[str] | None]
        alias_money: Mapped[Money]
        alias_money_or_none: Mapped[Money | None]
        alias_instant: Mapped[Instant]
        alias_instant_or_none: Mapped[Instant | None]
        alias_native_date: Mapped[NativeDate]
        alias_native_date_or_none: Mapped[NativeDate | None]
        alias_jsonb: Mapped[JsonB]
        alias_jsonb_or_none: Mapped[JsonB | None]
        alias_text_array: Mapped[TextArray]
        alias_text_array_or_none: Mapped[TextArray | None]
        explicit_char: Mapped[str] = mapped_column(CHAR(64))
        explicit_char_or_none: Mapped[str | None] = mapped_column(CHAR(64))

    return reg.metadata.tables["annotated_row"]


# --- Module surface -----------------------------------------------------------------------


def test_public_names_are_exported() -> None:
    assert set(base.__all__) == {
        "NAMING_CONVENTION",
        "Base",
        "DatasetScopedMixin",
        "IdPkMixin",
        "Instant",
        "JsonB",
        "Money",
        "NativeDate",
        "TextArray",
        "check_in",
        "non_negative",
        "utcnow",
    }
    missing = [name for name in base.__all__ if not hasattr(base, name)]
    assert missing == []


def test_module_imports_only_the_standard_library_and_sqlalchemy() -> None:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(base))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert "ads_engine" not in roots
    assert roots - {"sqlalchemy"} <= sys.stdlib_module_names


def test_docstring_examples_render_the_documented_sql() -> None:
    results = doctest.testmod(base)
    assert results.failed == 0
    assert results.attempted >= 2


# --- utcnow -------------------------------------------------------------------------------


def test_utcnow_returns_the_current_instant_in_utc() -> None:
    before = datetime.now(UTC)
    now = utcnow()
    after = datetime.now(UTC)
    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)
    assert before <= now <= after


# --- Base and the naming convention -------------------------------------------------------


def test_naming_convention_is_exact() -> None:
    assert NAMING_CONVENTION == {
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }


def test_base_is_an_async_attrs_declarative_base() -> None:
    assert Base.__bases__ == (AsyncAttrs, DeclarativeBase)


def test_base_metadata_carries_the_naming_convention() -> None:
    assert isinstance(Base.metadata, MetaData)
    assert Base.registry.metadata is Base.metadata
    assert dict(Base.metadata.naming_convention) == NAMING_CONVENTION


def test_base_adds_no_repr_and_no_automatic_table_name() -> None:
    assert Base.__repr__ is object.__repr__
    assert not hasattr(Base, "__tablename__")


def test_mapped_instances_expose_awaitable_attrs() -> None:
    class MirrorBase(AsyncAttrs, DeclarativeBase):
        metadata = MetaData(naming_convention=Base.metadata.naming_convention)
        type_annotation_map = Base.type_annotation_map

    class Campaign(IdPkMixin, MirrorBase):
        __tablename__ = "campaign"

        name: Mapped[str]

    campaign = Campaign(name="Brand search")

    async def read_name() -> str:
        name: str = await campaign.awaitable_attrs.name
        return name

    assert asyncio.run(read_name()) == "Brand search"


def test_naming_convention_names_every_constraint_and_index() -> None:
    reg = _mirror_registry()
    _map_source_dataset(reg)

    @reg.mapped
    class Keyword(IdPkMixin, DatasetScopedMixin):
        __tablename__ = "keyword"

        ad_group_id: Mapped[int]
        criterion_id: Mapped[int]
        match_type: Mapped[str] = mapped_column(index=True)

        __table_args__ = (
            check_in("match_type", ("EXACT", "PHRASE", "BROAD")),
            UniqueConstraint("dataset_id", "ad_group_id", "criterion_id"),
        )

    @reg.mapped
    class LeadMatch:
        __tablename__ = "lead_match"

        lead_id: Mapped[int]
        crm_dataset_id: Mapped[int]

        __table_args__ = (PrimaryKeyConstraint("lead_id", "crm_dataset_id"),)

    keyword = reg.metadata.tables["keyword"]
    lines = _ddl_lines(keyword)
    assert "CONSTRAINT pk_keyword PRIMARY KEY (id)" in lines
    assert (
        "CONSTRAINT ck_keyword_match_type_valid CHECK (match_type IN ('EXACT', 'PHRASE', 'BROAD'))"
    ) in lines
    assert (
        "CONSTRAINT uq_keyword_dataset_id_ad_group_id_criterion_id "
        "UNIQUE (dataset_id, ad_group_id, criterion_id)"
    ) in lines
    assert f"CONSTRAINT fk_keyword_dataset_id_source_dataset {_SOURCE_DATASET_FK}" in lines
    assert _index_ddl(keyword) == ["CREATE INDEX ix_keyword_match_type ON keyword (match_type)"]
    assert "CONSTRAINT pk_lead_match PRIMARY KEY (lead_id, crm_dataset_id)" in _ddl_lines(
        reg.metadata.tables["lead_match"]
    )


def test_documented_mapping_pattern_renders_the_full_ddl() -> None:
    class MirrorBase(AsyncAttrs, DeclarativeBase):
        metadata = MetaData(naming_convention=Base.metadata.naming_convention)
        type_annotation_map = Base.type_annotation_map

    class SourceDataset(IdPkMixin, MirrorBase):
        __tablename__ = "source_dataset"

    class Campaign(IdPkMixin, DatasetScopedMixin, MirrorBase):
        __tablename__ = "campaign"

        account_id: Mapped[int]
        external_id: Mapped[str]
        daily_budget: Mapped[Decimal | None]
        status: Mapped[str]

        __table_args__ = (
            check_in("source", _SOURCES),
            check_in("status", _CAMPAIGN_STATUSES),
            UniqueConstraint("dataset_id", "account_id", "external_id"),
        )

    assert _ddl_lines(MirrorBase.metadata.tables["campaign"]) == [
        "CREATE TABLE campaign (",
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY",
        "source TEXT NOT NULL",
        "dataset_id BIGINT NOT NULL",
        "account_id BIGINT NOT NULL",
        "external_id TEXT NOT NULL",
        "daily_budget NUMERIC(18, 6)",
        "status TEXT NOT NULL",
        "CONSTRAINT pk_campaign PRIMARY KEY (id)",
        "CONSTRAINT ck_campaign_source_valid CHECK (source IN ('google', 'linkedin', 'hubspot'))",
        "CONSTRAINT ck_campaign_status_valid CHECK (status IN ('active', 'paused', 'ended'))",
        "CONSTRAINT uq_campaign_dataset_id_account_id_external_id "
        "UNIQUE (dataset_id, account_id, external_id)",
        f"CONSTRAINT fk_campaign_dataset_id_source_dataset {_SOURCE_DATASET_FK}",
        ")",
    ]


# --- Annotation map and column aliases ----------------------------------------------------


def test_type_annotation_map_resolves_each_python_type() -> None:
    declared = {key: _type_sql(type_) for key, type_ in Base.type_annotation_map.items()}
    assert declared == _EXPECTED_TYPE_SQL
    registered: dict[Any, str] = {}
    for key, type_ in Base.registry.type_annotation_map.items():
        assert isinstance(type_, TypeEngine)
        registered[key] = _type_sql(type_)
    assert registered == _EXPECTED_TYPE_SQL


def test_money_is_exact_decimal_never_float() -> None:
    money = Base.type_annotation_map[Decimal]
    assert isinstance(money, Numeric)
    assert (money.precision, money.scale, money.asdecimal) == (18, 6, True)
    assert money.python_type is Decimal


def test_instants_are_timezone_aware() -> None:
    instant = Base.type_annotation_map[datetime]
    assert isinstance(instant, DateTime)
    assert instant.timezone is True


@pytest.mark.parametrize(
    ("column", "type_sql"),
    [
        ("bare_decimal", "NUMERIC(18, 6)"),
        ("bare_datetime", "TIMESTAMP WITH TIME ZONE"),
        ("bare_date", "DATE"),
        ("bare_str", "TEXT"),
        ("bare_int", "BIGINT"),
        ("bare_bool", "BOOLEAN"),
        ("bare_dict", "JSONB"),
        ("bare_list", "TEXT[]"),
        ("alias_money", "NUMERIC(18, 6)"),
        ("alias_instant", "TIMESTAMP WITH TIME ZONE"),
        ("alias_native_date", "DATE"),
        ("alias_jsonb", "JSONB"),
        ("alias_text_array", "TEXT[]"),
        ("explicit_char", "CHAR(64)"),
    ],
)
def test_annotation_sets_type_and_nullability(
    annotated_table: Table, column: str, type_sql: str
) -> None:
    required = annotated_table.c[column]
    optional = annotated_table.c[f"{column}_or_none"]
    assert _type_sql(required.type) == type_sql
    assert _type_sql(optional.type) == type_sql
    assert required.nullable is False
    assert optional.nullable is True


@pytest.mark.parametrize(
    ("alias", "python_type", "type_sql"),
    [
        (Money, Decimal, "NUMERIC(18, 6)"),
        (Instant, datetime, "TIMESTAMP WITH TIME ZONE"),
        (NativeDate, date, "DATE"),
        (JsonB, dict[str, Any], "JSONB"),
        (TextArray, list[str], "TEXT[]"),
    ],
)
def test_alias_annotates_its_python_type_with_a_column_type(
    alias: Any, python_type: Any, type_sql: str
) -> None:
    assert get_origin(alias) is Annotated
    annotated_type, column = get_args(alias)
    assert annotated_type == python_type
    assert isinstance(column, MappedColumn)
    assert _type_sql(column.column.type) == type_sql


def test_native_date_maps_an_attribute_named_date() -> None:
    reg = _mirror_registry()
    _map_source_dataset(reg)

    @reg.mapped
    class FactCampaignDaily(IdPkMixin, DatasetScopedMixin):
        __tablename__ = "fact_campaign_daily"

        date: Mapped[NativeDate] = mapped_column(index=True)
        cost: Mapped[Money]
        clicks: Mapped[int]

        __table_args__ = (
            non_negative("cost", "clicks"),
            UniqueConstraint("dataset_id", "date"),
        )

    table = reg.metadata.tables["fact_campaign_daily"]
    assert _ddl_lines(table) == [
        "CREATE TABLE fact_campaign_daily (",
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY",
        "source TEXT NOT NULL",
        "dataset_id BIGINT NOT NULL",
        "date DATE NOT NULL",
        "cost NUMERIC(18, 6) NOT NULL",
        "clicks BIGINT NOT NULL",
        "CONSTRAINT pk_fact_campaign_daily PRIMARY KEY (id)",
        "CONSTRAINT ck_fact_campaign_daily_non_negative CHECK (cost >= 0 AND clicks >= 0)",
        "CONSTRAINT uq_fact_campaign_daily_dataset_id_date UNIQUE (dataset_id, date)",
        f"CONSTRAINT fk_fact_campaign_daily_dataset_id_source_dataset {_SOURCE_DATASET_FK}",
        ")",
    ]
    assert _index_ddl(table) == [
        "CREATE INDEX ix_fact_campaign_daily_date ON fact_campaign_daily (date)"
    ]


# --- Mixins -------------------------------------------------------------------------------


def test_id_pk_mixin_is_a_bigint_identity_generated_by_default() -> None:
    reg = _mirror_registry()
    _map_source_dataset(reg)

    table = reg.metadata.tables["source_dataset"]
    assert _ddl_lines(table) == [
        "CREATE TABLE source_dataset (",
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY",
        "mode TEXT NOT NULL",
        "CONSTRAINT pk_source_dataset PRIMARY KEY (id)",
        ")",
    ]
    identity = table.c.id.identity
    assert identity is not None
    assert identity.always is False
    assert list(table.primary_key.columns.keys()) == ["id"]


def test_dataset_scoped_mixin_cascades_from_source_dataset_on_every_table() -> None:
    reg = _mirror_registry()
    _map_source_dataset(reg)

    @reg.mapped
    class Campaign(IdPkMixin, DatasetScopedMixin):
        __tablename__ = "campaign"

    @reg.mapped
    class Creative(IdPkMixin, DatasetScopedMixin):
        __tablename__ = "creative"

    for name in ("campaign", "creative"):
        table = reg.metadata.tables[name]
        source, dataset_id = table.c.source, table.c.dataset_id
        assert (_type_sql(source.type), source.nullable) == ("TEXT", False)
        assert (_type_sql(dataset_id.type), dataset_id.nullable) == ("BIGINT", False)
        (foreign_key,) = dataset_id.foreign_keys
        assert foreign_key.target_fullname == "source_dataset.id"
        assert foreign_key.ondelete == "CASCADE"
        expected_fk = f"CONSTRAINT fk_{name}_dataset_id_source_dataset {_SOURCE_DATASET_FK}"
        assert expected_fk in _ddl_lines(table)
        # The mixin leaves the ``source`` CHECK to each model's ``__table_args__``.
        assert not [c for c in table.constraints if isinstance(c, CheckConstraint)]

    campaign, creative = reg.metadata.tables["campaign"], reg.metadata.tables["creative"]
    assert campaign.c.dataset_id is not creative.c.dataset_id


def test_mixin_columns_lead_in_fixed_order_whatever_the_base_order() -> None:
    reg = _mirror_registry()
    _map_source_dataset(reg)

    @reg.mapped
    class DocumentedOrder(IdPkMixin, DatasetScopedMixin):
        __tablename__ = "documented_order"

        status: Mapped[str]
        external_id: Mapped[str]
        daily_budget: Mapped[Money | None]

    @reg.mapped
    class ReversedOrder(DatasetScopedMixin, IdPkMixin):
        __tablename__ = "reversed_order"

        status: Mapped[str]
        external_id: Mapped[str]
        daily_budget: Mapped[Money | None]

    expected = ["id", "source", "dataset_id", "status", "external_id", "daily_budget"]
    for name in ("documented_order", "reversed_order"):
        table = reg.metadata.tables[name]
        assert list(table.columns.keys()) == expected
        rendered = [line.split()[0] for line in _ddl_lines(table)[1 : 1 + len(expected)]]
        assert rendered == expected


# --- check_in -----------------------------------------------------------------------------


def test_check_in_renders_values_in_the_given_order() -> None:
    constraint = check_in("status", ("paused", "active", "ended"))
    assert isinstance(constraint, CheckConstraint)
    assert str(constraint.sqltext) == "status IN ('paused', 'active', 'ended')"


def test_check_in_accepts_any_sequence_and_a_single_value() -> None:
    assert str(check_in("mode", ["live", "simulated"]).sqltext) == "mode IN ('live', 'simulated')"
    assert str(check_in("mode", ("live",)).sqltext) == "mode IN ('live')"


def test_check_in_keeps_punctuated_values_verbatim() -> None:
    constraint = check_in("kind", ("MANY_PER_CLICK", "first-meeting", "urn:li:form", "v1.2"))
    assert str(constraint.sqltext) == (
        "kind IN ('MANY_PER_CLICK', 'first-meeting', 'urn:li:form', 'v1.2')"
    )


def test_check_in_names_the_constraint_after_the_raw_column_by_default() -> None:
    assert check_in("status", ("a",)).name == "status_valid"
    assert check_in("status", ("a",), name=None).name == "status_valid"
    assert check_in("group", ("a",)).name == "group_valid"


def test_check_in_takes_an_explicit_name() -> None:
    assert check_in("status", ("a",), name="decision_status").name == "decision_status"


@pytest.mark.parametrize(("column", "rendered"), _IDENTIFIER_CASES)
def test_check_in_quotes_only_names_postgresql_rejects_bare(column: str, rendered: str) -> None:
    assert str(check_in(column, ("a", "b")).sqltext) == f"{rendered} IN ('a', 'b')"


def test_check_in_and_non_negative_quote_every_postgresql_reserved_word() -> None:
    words = sorted(RESERVED_WORDS)
    assert words
    bare_in = [w for w in words if str(check_in(w, ("a",)).sqltext) != f"\"{w}\" IN ('a')"]
    bare_non_negative = [w for w in words if str(non_negative(w).sqltext) != f'"{w}" >= 0']
    assert bare_in == []
    assert bare_non_negative == []


def test_reserved_word_columns_render_valid_check_ddl() -> None:
    reg = _mirror_registry()

    @reg.mapped
    class Recommendation(IdPkMixin):
        __tablename__ = "recommendation"

        group: Mapped[str]
        rank: Mapped[int]
        order: Mapped[int | None]

        __table_args__ = (
            check_in("group", ("block", "expand")),
            non_negative("rank", "order"),
        )

    lines = _ddl_lines(reg.metadata.tables["recommendation"])
    assert '"group" TEXT NOT NULL' in lines
    assert '"order" BIGINT' in lines
    assert (
        "CONSTRAINT ck_recommendation_group_valid CHECK (\"group\" IN ('block', 'expand'))" in lines
    )
    assert 'CONSTRAINT ck_recommendation_non_negative CHECK (rank >= 0 AND "order" >= 0)' in lines


@pytest.mark.parametrize("column", ["status", "not a column"])
def test_check_in_rejects_a_bare_string_first(column: str) -> None:
    with pytest.raises(TypeError, match=r"^values must be a sequence of strings, not a single"):
        check_in(column, "active")


@pytest.mark.parametrize("values", [(), []])
def test_check_in_rejects_no_values(values: Sequence[str]) -> None:
    with pytest.raises(ValueError, match=r"^check_in\('status'\) needs at least one allowed value"):
        check_in("status", values)


def test_check_in_rejects_duplicate_values() -> None:
    with pytest.raises(
        ValueError, match=r"^check_in\('status'\) received duplicate values: \['a', 'b', 'a'\]$"
    ):
        check_in("status", ("a", "b", "a"))


def test_check_in_rejects_an_empty_value() -> None:
    with pytest.raises(ValueError, match=r"^value for 'status' must be a non-empty string$"):
        check_in("status", ("active", ""))


@pytest.mark.parametrize("value", _UNSAFE_TOKENS)
def test_check_in_rejects_values_that_could_break_out_of_the_literal(value: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"^value for 'status' .+ must not contain quotes, backslashes or whitespace$",
    ):
        check_in("status", ("active", value))


def test_check_in_rejects_an_empty_column_before_its_values() -> None:
    with pytest.raises(ValueError, match=r"^column name must be a non-empty string$"):
        check_in("", ())


@pytest.mark.parametrize("column", _UNSAFE_TOKENS)
def test_check_in_rejects_unsafe_column_names(column: str) -> None:
    with pytest.raises(
        ValueError, match=r"^column name .+ must not contain quotes, backslashes or whitespace$"
    ):
        check_in(column, ("a",))


# --- non_negative -------------------------------------------------------------------------


def test_non_negative_renders_columns_in_argument_order() -> None:
    constraint = non_negative("cost", "impressions", "clicks")
    assert isinstance(constraint, CheckConstraint)
    assert str(constraint.sqltext) == "cost >= 0 AND impressions >= 0 AND clicks >= 0"
    assert constraint.name == "non_negative"


def test_non_negative_takes_a_single_column_and_an_explicit_name() -> None:
    constraint = non_negative("amount_micros", name="amount_non_negative")
    assert str(constraint.sqltext) == "amount_micros >= 0"
    assert constraint.name == "amount_non_negative"


@pytest.mark.parametrize(("column", "rendered"), _IDENTIFIER_CASES)
def test_non_negative_quotes_columns_exactly_as_check_in(column: str, rendered: str) -> None:
    assert str(non_negative("cost", column).sqltext) == f"cost >= 0 AND {rendered} >= 0"


def test_non_negative_requires_a_column() -> None:
    with pytest.raises(ValueError, match=r"^non_negative\(\) needs at least one column$"):
        non_negative()


def test_non_negative_rejects_duplicate_columns() -> None:
    with pytest.raises(
        ValueError,
        match=r"^non_negative\(\) received duplicate columns: \['cost', 'clicks', 'cost'\]$",
    ):
        non_negative("cost", "clicks", "cost")


def test_non_negative_rejects_an_empty_column() -> None:
    with pytest.raises(ValueError, match=r"^column name must be a non-empty string$"):
        non_negative("cost", "")


@pytest.mark.parametrize("column", _UNSAFE_TOKENS)
def test_non_negative_rejects_unsafe_column_names(column: str) -> None:
    with pytest.raises(
        ValueError, match=r"^column name .+ must not contain quotes, backslashes or whitespace$"
    ):
        non_negative("cost", column)
