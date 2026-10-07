"""Declarative base, constraint naming convention and shared column types for every model.

Every module under ``ads_engine.db.models`` builds on this file, so it imports only the
standard library and SQLAlchemy and never another ``ads_engine`` module. That keeps the
model package free of import cycles.

The conventions fixed here apply to the whole schema:

* **Constraint names** come from :data:`NAMING_CONVENTION`, so every primary key, unique,
  check, foreign key and index has a deterministic name (``pk_<table>``,
  ``uq_<table>_<col1>_<col2>``, ``ck_<table>_<name>``, ``fk_<table>_<col>_<referred>``,
  ``ix_<table>_<col>``). PostgreSQL truncates identifiers longer than 63 characters, so
  every rendered name must stay within that limit.
* **Enumerations** are ``text`` columns guarded by ``CHECK`` constraints built with
  :func:`check_in` from exported tuples of values, never PostgreSQL ``ENUM`` types. Adding a
  value is then a constraint swap rather than a type migration.
* **Value types** resolve through :attr:`Base.type_annotation_map`: money and conversions are
  ``Decimal`` in ``numeric(18, 6)`` (Google reports conversions as doubles, so they are not
  integers); instants are tz-aware ``datetime`` in ``timestamptz``; native daily dates are
  ``date`` and are stored exactly as each source reports them, never rebucketed; every
  integer is ``bigint``; strings are ``text``; ``dict[str, Any]`` is ``jsonb`` and
  ``list[str]`` is ``text[]``.
* **Provenance**: every source-derived table mixes in :class:`DatasetScopedMixin`, whose
  ``dataset_id`` references ``source_dataset.id`` with ``ON DELETE CASCADE``. Purging a
  retired dataset is then one ``DELETE FROM source_dataset WHERE id = :retired`` and
  PostgreSQL removes every dependent row in a referentially safe order.

``backend/migrations/versions/0001_initial_schema.py`` is written by hand and mirrors this
module verbatim: it repeats :data:`NAMING_CONVENTION` key for key and value for value, and
writes every ``CHECK`` with exactly the SQL text :func:`check_in` and :func:`non_negative`
render. ``uv run alembic check`` must report no drift between it and ``Base.metadata``.

Mapping pattern for a model::

    class Campaign(IdPkMixin, DatasetScopedMixin, Base):
        __tablename__ = "campaign"

        account_id: Mapped[int]
        external_id: Mapped[str]
        daily_budget: Mapped[Decimal | None]
        status: Mapped[str]

        __table_args__ = (
            check_in("source", SOURCES),
            check_in("status", CAMPAIGN_STATUSES),
            UniqueConstraint("dataset_id", "account_id", "external_id"),
        )
"""

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    MetaData,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql.base import RESERVED_WORDS
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeEngine

__all__ = [
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
]

NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""Constraint and index naming convention shared by ``Base.metadata`` and the migration.

The keys and values are repeated character for character in
``backend/migrations/versions/0001_initial_schema.py``; changing one here without the other
makes ``alembic check`` report drift.
"""

# Characters that may never appear in a column name or an enumerated value passed to the
# CHECK builders. The builders interpolate their arguments into SQL text, so refusing quotes,
# backslashes and whitespace means no constant can break out of its literal or identifier.
_FORBIDDEN_CHARACTERS: Final[frozenset[str]] = frozenset({"'", '"', "\\"})

# A column name renders bare in CHECK text only when PostgreSQL accepts it unquoted: a
# lower-case identifier that is not a reserved word. Anything else, such as ``group`` or
# ``order``, is double-quoted so the constraint stays valid SQL. The reserved-word list is the
# one SQLAlchemy's PostgreSQL dialect uses when it renders DDL.
_PLAIN_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[a-z_][a-z0-9_$]*")
_PG_RESERVED_WORDS: Final[frozenset[str]] = frozenset(RESERVED_WORDS)


def utcnow() -> datetime:
    """Return the current instant as a timezone-aware UTC ``datetime``.

    Every instant the application sets itself (``created_at``, ``captured_at``,
    ``decided_at``, ``fetched_at`` and so on) is produced by this function, so a naive
    datetime never reaches a ``timestamptz`` column.

    Returns:
        The current time with ``tzinfo`` set to :data:`datetime.UTC`.
    """
    return datetime.now(UTC)


def _ensure_safe_token(token: str, *, what: str) -> None:
    """Reject a column name or enumerated value that is unsafe to interpolate into SQL.

    Args:
        token: The column name or value to check.
        what: Description of the token used in the error message.

    Raises:
        ValueError: If ``token`` is empty or contains a quote, a backslash or whitespace.
    """
    if not token:
        raise ValueError(f"{what} must be a non-empty string")
    if any(char in _FORBIDDEN_CHARACTERS or char.isspace() for char in token):
        raise ValueError(f"{what} {token!r} must not contain quotes, backslashes or whitespace")


def _column_sql(column: str) -> str:
    """Validate a column name and render it as a PostgreSQL identifier for CHECK text.

    Ordinary lower-case snake_case names render unchanged (``status`` stays ``status``).
    Names PostgreSQL cannot accept bare, such as the reserved word ``group``, are
    double-quoted (``"group"``) so the constraint remains valid SQL.

    Args:
        column: The column name.

    Returns:
        The column name as it must appear in the constraint's SQL text.

    Raises:
        ValueError: If the column name is empty or contains a quote, a backslash or
            whitespace.
    """
    _ensure_safe_token(column, what="column name")
    if _PLAIN_IDENTIFIER.fullmatch(column) and column not in _PG_RESERVED_WORDS:
        return column
    return f'"{column}"'


def check_in(column: str, values: Sequence[str], *, name: str | None = None) -> CheckConstraint:
    """Build the ``CHECK`` constraint that restricts a text column to an enumerated set.

    Enumerations are stored as ``text`` and guarded by this constraint rather than by a
    PostgreSQL ``ENUM`` type. The SQL text is ``"<column> IN ('v1', 'v2', ...)"`` with the
    values in the order given, each single-quoted and joined by ``", "``. A column name that
    PostgreSQL cannot take bare, such as the reserved word ``group``, is double-quoted
    (``"group" IN (...)``). The migration repeats that text verbatim.

    A ``NULL`` satisfies a ``CHECK`` in PostgreSQL, so the same constraint guards nullable
    enumeration columns without a special case.

    The constraint name defaults to ``"<column>_valid"``. The naming convention renders it as
    ``ck_<table>_<name>``, for example ``ck_campaign_status_valid``. Keep ``name`` short
    enough that the full rendered name is at most 63 characters, PostgreSQL's identifier
    limit.

    Example:
        >>> str(check_in("status", ("active", "paused")).sqltext)
        "status IN ('active', 'paused')"

    Args:
        column: Name of the constrained column.
        values: The allowed values, in the order they should appear in the SQL text.
        name: Constraint name before the convention is applied; defaults to
            ``"<column>_valid"``.

    Returns:
        A named ``CheckConstraint`` ready for a model's ``__table_args__``.

    Raises:
        TypeError: If ``values`` is a single string rather than a sequence of strings.
        ValueError: If ``values`` is empty or holds a duplicate, or if the column name or
            any value is empty or contains a quote, a backslash or whitespace.
    """
    if isinstance(values, str):
        raise TypeError("values must be a sequence of strings, not a single string")
    column_sql = _column_sql(column)
    if not values:
        raise ValueError(f"check_in({column!r}) needs at least one allowed value")
    for value in values:
        _ensure_safe_token(value, what=f"value for {column!r}")
    if len(set(values)) != len(values):
        raise ValueError(f"check_in({column!r}) received duplicate values: {list(values)!r}")
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column_sql} IN ({allowed})", name=name or f"{column}_valid")


def non_negative(*columns: str, name: str = "non_negative") -> CheckConstraint:
    """Build the ``CHECK`` constraint that keeps metric columns at zero or above.

    Fact tables guard their metrics (cost, impressions, clicks, conversions and the like)
    with this constraint. The SQL text is ``"col1 >= 0 AND col2 >= 0 ..."`` in argument
    order, with column names quoted exactly as :func:`check_in` quotes them, and the
    migration repeats it verbatim. A ``NULL`` metric, which means
    "unavailable", satisfies the constraint.

    The naming convention renders the constraint as ``ck_<table>_<name>``, for example
    ``ck_fact_campaign_daily_non_negative``. Keep ``name`` short enough that the full
    rendered name is at most 63 characters, PostgreSQL's identifier limit.

    Example:
        >>> str(non_negative("cost", "clicks").sqltext)
        'cost >= 0 AND clicks >= 0'

    Args:
        *columns: The metric columns, in the order they should appear in the SQL text.
        name: Constraint name before the convention is applied.

    Returns:
        A named ``CheckConstraint`` ready for a model's ``__table_args__``.

    Raises:
        ValueError: If no column is given, a column is repeated, or a column name is empty
            or contains a quote, a backslash or whitespace.
    """
    if not columns:
        raise ValueError("non_negative() needs at least one column")
    if len(set(columns)) != len(columns):
        raise ValueError(f"non_negative() received duplicate columns: {list(columns)!r}")
    conditions = " AND ".join(f"{_column_sql(column)} >= 0" for column in columns)
    return CheckConstraint(conditions, name=name)


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base of every model; its ``metadata`` is the schema ``alembic`` compares.

    ``AsyncAttrs`` adds ``awaitable_attrs``, so a lazily loaded attribute can be read safely
    under an ``AsyncSession`` (``await obj.awaitable_attrs.name``).

    ``type_annotation_map`` resolves bare annotations to the schema's column types, so
    ``Mapped[X]`` declares a ``NOT NULL`` column and ``Mapped[X | None]`` a nullable one
    without repeating the type:

    ==================  =====================
    Annotation          Column type
    ==================  =====================
    ``Decimal``         ``numeric(18, 6)``
    ``datetime``        ``timestamptz``
    ``date``            ``date``
    ``str``             ``text``
    ``int``             ``bigint``
    ``bool``            ``boolean``
    ``dict[str, Any]``  ``jsonb``
    ``list[str]``       ``text[]``
    ==================  =====================

    Every integer is ``bigint`` (ids, counts and small integers alike), so the migration
    mirrors a single rule. Money is never ``float``. A column that needs another type
    declares it explicitly, for example ``mapped_column(CHAR(64))`` for a SHA-256 hex digest.

    Models set ``__tablename__`` explicitly. The base adds no ``__repr__``, no relationship
    helpers and no automatic table naming.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[Mapping[Any, TypeEngine[Any]]] = {
        Decimal: Numeric(18, 6),
        datetime: DateTime(timezone=True),
        date: Date(),
        str: Text(),
        int: BigInteger(),
        bool: Boolean(),
        dict[str, Any]: JSONB(),
        list[str]: ARRAY(Text()),
    }


Money = Annotated[Decimal, mapped_column(Numeric(18, 6))]
"""Amount in the account's currency (or a conversion count): ``numeric(18, 6)``."""

Instant = Annotated[datetime, mapped_column(DateTime(timezone=True))]
"""Point in time, always tz-aware: ``timestamptz``. Set from :func:`utcnow`."""

NativeDate = Annotated[date, mapped_column(Date())]
"""A source's own calendar day, stored as reported and never rebucketed: ``date``.

Models whose attribute is itself named ``date`` use this alias, so the annotation never
refers to a name the class body also binds.
"""

JsonB = Annotated[dict[str, Any], mapped_column(JSONB())]
"""JSON object such as evidence, a decision patch or a raw payload: ``jsonb``."""

TextArray = Annotated[list[str], mapped_column(ARRAY(Text()))]
"""List of strings such as flags or question ids: ``text[]``."""

# Mixin columns would otherwise follow the model's own columns. These negative sort orders
# put them first, as ``id, source, dataset_id, <model columns>``, the order the migration
# writes each table in. Model columns keep the default sort order 0 and their declared order.
_SORT_ID: Final[int] = -30
_SORT_SOURCE: Final[int] = -20
_SORT_DATASET_ID: Final[int] = -10


class IdPkMixin:
    """Surrogate primary key: ``id bigint GENERATED BY DEFAULT AS IDENTITY``.

    ``BY DEFAULT`` lets PostgreSQL generate the value while still accepting an explicit id
    when one is supplied. The naming convention names the key ``pk_<table>``. Tables with a
    composite primary key, such as ``lead_match`` keyed on ``(lead_id, crm_dataset_id)``,
    do not use this mixin.

    ``id`` is the table's first column. Place the mixin first in the bases, ahead of
    :class:`DatasetScopedMixin` and :class:`Base`::

        class Campaign(IdPkMixin, DatasetScopedMixin, Base): ...
    """

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=False),
        primary_key=True,
        sort_order=_SORT_ID,
    )


class DatasetScopedMixin:
    """Provenance columns of every source-derived row: ``source`` and ``dataset_id``.

    ``dataset_id`` references ``source_dataset.id`` with ``ON DELETE CASCADE`` and is named
    ``fk_<table>_dataset_id_source_dataset`` by the convention. A source's active dataset is
    entirely live or entirely simulated, reads and rules filter to each source's active
    dataset, and rows from a retired dataset never enter a computation. Purging a retired
    dataset is a single ``DELETE FROM source_dataset WHERE id = :retired``; the cascade
    removes the rows of every table that carries this mixin.

    Every natural-key unique constraint of a dataset-scoped table includes ``dataset_id``,
    normally as its first column, so the same external entity can exist once per dataset and
    the cascade's lookup by ``dataset_id`` is served by that unique index.

    The allowed values of ``source`` live with the dataset constants in
    ``ads_engine.db.models.ops``, which imports this module. Each model therefore adds
    ``check_in("source", SOURCES)`` to its own ``__table_args__``.

    ``source`` and ``dataset_id`` follow ``id`` and precede the model's own columns. Place
    the mixin after :class:`IdPkMixin` and before :class:`Base`::

        class Campaign(IdPkMixin, DatasetScopedMixin, Base): ...
    """

    source: Mapped[str] = mapped_column(Text, nullable=False, sort_order=_SORT_SOURCE)
    dataset_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("source_dataset.id", ondelete="CASCADE"),
        nullable=False,
        sort_order=_SORT_DATASET_ID,
    )
