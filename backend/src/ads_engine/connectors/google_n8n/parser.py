"""Typed rows parsed from the REST ``GoogleAdsRow`` objects the n8n webhook relays.

Every element of a webhook response's ``results[]`` is one REST ``GoogleAdsRow``: camelCase keys
nested per resource (``campaign``, ``campaignBudget``, ``adGroup``, ``adGroupCriterion``,
``searchTermView``, ``conversionAction``, ``customer``, ``segments``, ``metrics``). Each row
follows the contract ``backend/tests/contracts/google/{surface}.json`` named by the
``row_surface`` of its template in :data:`~ads_engine.connectors.google_n8n.queries.QUERIES`, and
:data:`PARSERS` maps that surface to the function that turns the row into a frozen dataclass.

Wire encoding (proto3 JSON, as relayed by n8n):
    * INT64 values (ids, ``*Micros``, ``clicks``, ``impressions``,
      ``historicalQualityScore``) arrive as decimal **strings** and become ``int``.
    * INT32 values (``qualityInfo.qualityScore``) arrive as JSON integers.
    * DOUBLE values (``conversions``, ``conversionsValue``, ``averageCpc``, impression shares)
      arrive as JSON numbers, which ``connectors/http.py`` decodes as :class:`~decimal.Decimal`.
      They stay ``Decimal``: conversions are never assumed integral, because data-driven
      attribution makes them fractional and ``MANY_PER_CLICK`` actions allow several per click.
      A ``float`` or ``str`` is accepted as well and converted through ``str``, never through
      ``Decimal(float)``. No DOUBLE value may be negative, and the impression shares are
      fractions from 0 to 1 inclusive, Google's reporting sentinels ``0.9001`` and ``0.0999``
      kept as returned; a value outside those bounds is a defect. Every DOUBLE is stored as
      ``numeric(18, 6)``, and the parser, never the database, makes each row equal what its
      column holds. Conversions and conversion value are kept exactly as returned (Google sends
      at most six decimal places); one the column cannot hold exactly, ``10**12`` or more or
      finer than six places, is a defect, because rounding could turn a fractional count whole
      or a converting day into a zero outcome. Impression shares, returned as full doubles, are
      rounded toward zero to six places, which leaves every comparison with a six-place
      threshold (such as the ``budget.capped_lost_is_share`` capped-day test, default 0.10)
      unchanged, and a positive share below ``0.000001`` is a defect. ``averageCpc`` is rounded
      half-even to the micro, and one that rounds to ``10**12`` or more, or a positive one that
      rounds to zero, is a defect.
    * Enums arrive as their names (``"ENABLED"``, ``"CUSTOM_PERIOD"``).
    * Zero-valued and unset fields may be omitted.
    * Booleans come in two presence kinds. An implicit-presence ``bool``
      (``campaign_budget.explicitly_shared``) is omitted when ``false``. An explicit-presence
      ``optional bool`` (``conversion_action.include_in_conversions_metric``) is sent whenever
      it is set, a ``false`` included, and is absent only when unset. Both read absence as
      ``False``.

Missing values:
    An *absent* (omitted) key and an explicit JSON ``null`` are different things. Absent
    *additive* metrics (``costMicros``, ``clicks``, ``impressions``, ``conversions``,
    ``conversionsValue``) are zero, as the row contracts state, because REST JSON omits
    zero-valued fields. Absent *rate or score* values (``historicalQualityScore``,
    ``qualityInfo.qualityScore``, ``averageCpc``, the impression shares) and absent budget
    amounts are ``None`` ("unavailable"), never zero. No row contract declares a ``null`` type,
    so a key present with ``null`` (``"metrics": null``, ``"conversions": null``) is a
    validation defect at any level of a field's key path, never a zero or an unavailable value.

Money:
    ``cost_micros`` is divided by 1,000,000 exactly into a ``Decimal`` in the account currency
    (``customer.currency_code``); the integer micros are kept beside it wherever a comparison at
    native precision needs them (C0 compares budget micros exactly). Amounts are stored as
    ``numeric(18, 6)``, at most ``999999999999.999999``, so the micros of every emitted amount
    (``metrics.cost_micros``, ``campaign_budget.amount_micros`` and ``total_amount_micros``)
    must lie in ``0 .. 999999999999999999``. A larger value is a defect, never clamped, and an
    accepted value is kept exact.

Dates:
    ``segments.date`` is Google's native day in the customer account's time zone
    (``customer.time_zone``). It is parsed as written and never converted or rebucketed.

Strings:
    Names, search terms, keyword texts, enum names and resource names are kept exactly as
    returned, any Unicode included, except that a string holding U+0000 or an unpaired
    surrogate code point is a defect: PostgreSQL ``text`` can store neither.

Errors:
    Every failure raises :class:`~ads_engine.connectors.errors.QueryDefect` with
    ``source="google"``, ``endpoint="parse {surface}"`` and ``code="validation"``. Messages name
    the GAQL field and the expected type or bounds only, never a row value, search term or
    name, so they are safe to persist and log.

Example::

    row = {
        "campaign": {"resourceName": "customers/1/campaigns/7", "id": "7"},
        "segments": {"date": "2026-09-01"},
        "metrics": {"costMicros": "1500000", "conversions": Decimal("0.3")},
    }
    daily = PARSERS["row_campaign_daily"](row)
    # CampaignDailyRow(campaign_id=7, date=date(2026, 9, 1), cost=Decimal("1.500000"),
    #                  cost_micros=1500000, impressions=0, clicks=0,
    #                  conversions=Decimal("0.3"), ...)

Only the standard library, :mod:`~ads_engine.connectors.google_n8n.queries` and
:mod:`~ads_engine.connectors.errors` are imported; ``client`` and ``partition`` build on this
module, never the reverse.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.google_n8n.queries import INT64_MAX, QUERIES, rest_path

__all__ = [
    "CAMPAIGN_STATUS_MAP",
    "PARSERS",
    "SEARCH_TERM_STATUSES",
    "AdGroupRow",
    "BudgetPeriod",
    "CampaignDailyRow",
    "CampaignRow",
    "ConversionActionRow",
    "CustomerRow",
    "GoogleAdsRow",
    "KeywordRow",
    "KeywordViewRow",
    "NormalisedStatus",
    "SearchTermRow",
    "get_field",
    "parse_ad_group",
    "parse_campaign",
    "parse_campaign_daily",
    "parse_conversion_action",
    "parse_customer",
    "parse_keyword",
    "parse_keyword_view",
    "parse_search_term",
]

# ---------------------------------------------------------------------------------------------
# Types and constants
# ---------------------------------------------------------------------------------------------

# The aliases below are implicit (plain assignment) rather than annotated with `TypeAlias`:
# ruff's UP040 rejects the `TypeAlias` annotation on Python 3.14, and a `type` statement would
# hide the value from `typing.get_args` and from runtime use. An implicit alias is the same
# alias to mypy and stays an ordinary `Mapping[str, Any]` / `Literal[...]` object at runtime.
GoogleAdsRow = Mapping[str, Any]
"""One raw REST ``GoogleAdsRow`` as decoded from the webhook's ``results[]``.

camelCase keys nested per resource; ``client.py`` re-exports this alias."""

NormalisedStatus = Literal["active", "paused", "ended"]
"""Normalised entity status. Equals ``CAMPAIGN_STATUSES`` of ``db/models/accounts.py``."""

BudgetPeriod = Literal["DAILY", "CUSTOM_PERIOD"]
"""Google ``BudgetPeriodEnum`` values the product supports: daily or lifetime (custom period)."""

_STATUS_MAP: Final[dict[str, NormalisedStatus]] = {
    "ENABLED": "active",
    "PAUSED": "paused",
    "REMOVED": "ended",
}
CAMPAIGN_STATUS_MAP: Final[Mapping[str, NormalisedStatus]] = MappingProxyType(_STATUS_MAP)
"""Google status enum name to normalised status, for campaigns, ad groups and keywords.

Any other value (``UNKNOWN``, ``UNSPECIFIED`` or an enum value added in a later version) is a
parse defect; the raw value is always kept beside the normalised one."""

_BUDGET_PERIOD_MAP: Final[dict[str, BudgetPeriod]] = {
    "DAILY": "DAILY",
    "CUSTOM_PERIOD": "CUSTOM_PERIOD",
}

_DEFAULT_BUDGET_PERIOD: Final[BudgetPeriod] = "DAILY"
"""Google's documented default ``campaign_budget.period``, applied when the key is absent."""

# Google keeps a DAILY budget's amount in amount_micros and a CUSTOM_PERIOD (lifetime) budget's
# amount in total_amount_micros; the two are mutually exclusive, so each period reads its own
# and the other period's field must be absent.
_BUDGET_AMOUNT_FIELD: Final[Mapping[BudgetPeriod, str]] = MappingProxyType(
    {
        "DAILY": "campaign_budget.amount_micros",
        "CUSTOM_PERIOD": "campaign_budget.total_amount_micros",
    }
)

# The contract row_search_term_view.json lists UNSPECIFIED beside UNKNOWN as wire values no rule
# acts on, so both are accepted and stored raw; K1 and K2 act only on NONE and ADDED.
SEARCH_TERM_STATUSES: Final[frozenset[str]] = frozenset(
    {"ADDED", "EXCLUDED", "ADDED_EXCLUDED", "NONE", "UNKNOWN", "UNSPECIFIED"}
)
"""``search_term_view.status`` values accepted and stored raw as the term status."""

# The v25 enum names each row contract lists for these fields. Values are stored raw, never
# normalised; UNKNOWN and UNSPECIFIED are accepted wire values that no rule treats as known, and
# the retired rule-based attribution models stay valid because past days were counted under them.
_ADVERTISING_CHANNEL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "DEMAND_GEN",
        "DISPLAY",
        "HOTEL",
        "LOCAL",
        "LOCAL_SERVICES",
        "MULTI_CHANNEL",
        "PERFORMANCE_MAX",
        "SEARCH",
        "SHOPPING",
        "SMART",
        "TRAVEL",
        "UNKNOWN",
        "UNSPECIFIED",
        "VIDEO",
    }
)
_KEYWORD_MATCH_TYPES: Final[frozenset[str]] = frozenset(
    {"BROAD", "EXACT", "PHRASE", "UNKNOWN", "UNSPECIFIED"}
)
_CONVERSION_ACTION_STATUSES: Final[frozenset[str]] = frozenset(
    {"ENABLED", "HIDDEN", "REMOVED", "UNKNOWN", "UNSPECIFIED"}
)
_COUNTING_TYPES: Final[frozenset[str]] = frozenset(
    {"MANY_PER_CLICK", "ONE_PER_CLICK", "UNKNOWN", "UNSPECIFIED"}
)
_ATTRIBUTION_MODELS: Final[frozenset[str]] = frozenset(
    {
        "EXTERNAL",
        "GOOGLE_ADS_LAST_CLICK",
        "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
        "GOOGLE_SEARCH_ATTRIBUTION_FIRST_CLICK",
        "GOOGLE_SEARCH_ATTRIBUTION_LINEAR",
        "GOOGLE_SEARCH_ATTRIBUTION_POSITION_BASED",
        "GOOGLE_SEARCH_ATTRIBUTION_TIME_DECAY",
        "UNKNOWN",
        "UNSPECIFIED",
    }
)
# Listing the nine attribution model names would overflow QueryDefect's 300-character message
# cap and cut the expectation mid-name, so the defect names the enum and where it is listed.
_ATTRIBUTION_MODEL_EXPECTED: Final = (
    "a v25 AttributionModelEnum name listed in the row_conversion_action contract"
)

_QUALITY_SCORE_MIN: Final = 1
_QUALITY_SCORE_MAX: Final = 10
_MICROS_EXPONENT: Final = -6
_AMOUNT_QUANTUM: Final = Decimal("0.000001")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)

# Every Decimal row field is stored as numeric(18, 6) (db/base.py maps Decimal to Numeric(18, 6)):
# 18 digits, 6 of them after the point, so 12 integral digits. The largest storable magnitude is
# therefore 999999999999.999999 and anything from 10**12 up overflows the column. Integer micros
# carry exactly the six fractional digits, so 10**18 - 1 micros is the largest count of micros
# whose amount fits; it is far below INT64_MAX, so the bigint micros columns hold it as well.
_NUMERIC_LIMIT: Final = Decimal("1E+12")
_NUMERIC_MAX: Final = Decimal("999999999999.999999")
_AMOUNT_MICROS_MAX: Final = 999_999_999_999_999_999
_MICROS_LIMIT: Final = Decimal("1E+18")
_NUMERIC_RANGE_EXPECTED: Final = f"a value of at most {_NUMERIC_MAX}, the numeric(18, 6) range"
_NUMERIC_SCALE_EXPECTED: Final = (
    "a value with at most six decimal places, which numeric(18, 6) stores exactly"
)
_FRACTION_POSITIVE_EXPECTED: Final = (
    "zero or a fraction of at least 0.000001, which six decimal places keep above zero"
)
_MICROS_RATE_RANGE_EXPECTED: Final = (
    "an amount in micros that, divided by 1000000 and rounded half-even to six decimal places, "
    f"is at most {_NUMERIC_MAX}, the numeric(18, 6) range"
)
_MICROS_RATE_POSITIVE_EXPECTED: Final = (
    "zero or a value above 0.5 micros, which six decimal places keep above zero"
)

# A private context makes micros conversion and rounding independent of the caller's decimal
# context: 38 digits hold every INT64 micros value exactly, and an invalid result raises.
_DECIMAL_CONTEXT: Final = Context(prec=38, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])

# At most 19 digits: every INT64 fits, and int() never sees an oversized string.
_INT_RE: Final = re.compile(r"[+-]?[0-9]{1,19}")
_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CURRENCY_RE: Final = re.compile(r"[A-Z]{3}")
# PostgreSQL text cannot store U+0000, and an unpaired surrogate code point (what a lone JSON
# "\ud800" escape decodes to) has no UTF-8 encoding, so a string holding either is rejected
# before it reaches a row. A JSON surrogate pair decodes to one astral code point and is kept.
_UNSTORABLE_TEXT_RE: Final = re.compile(r"[\x00\ud800-\udfff]")
_STORABLE_TEXT: Final = "a string without U+0000 or unpaired surrogates (PostgreSQL text)"
# Groups: customer, ad group and criterion id, each a run of at most 19 digits (as _INT_RE).
_AD_GROUP_CRITERION_RE: Final = re.compile(
    r"customers/([0-9]{1,19})/adGroupCriteria/([0-9]{1,19})~([0-9]{1,19})"
)
_AD_GROUP_CRITERION_FORM: Final = (
    "an adGroupCriteria resource name customers/{customer}/adGroupCriteria/{ad_group}~{criterion}"
)
_AD_GROUP_CRITERION_IDS: Final = (
    f"an adGroupCriteria resource name whose ids are integers from 0 to {INT64_MAX}"
)
_AD_GROUP_CRITERION_PARENT: Final = (
    "an adGroupCriteria resource name of the row's ad group (ad_group.id)"
)

_GENERIC_SURFACE: Final = "google_ads_row"
"""Surface named by errors raised outside a specific parser, such as from :func:`get_field`."""


def _query_id_by_surface() -> Mapping[str, str]:
    """Map each row surface read by exactly one template to that template's query id.

    ``row_campaign`` serves both Q-G2 and Q-G2b, so its defects carry no query id; the client,
    which knows the query, logs it.
    """
    by_surface: dict[str, list[str]] = {}
    for template in QUERIES.values():
        by_surface.setdefault(template.row_surface, []).append(template.query_id)
    return MappingProxyType({s: ids[0] for s, ids in by_surface.items() if len(ids) == 1})


_QUERY_ID_BY_SURFACE: Final = _query_id_by_surface()

# ---------------------------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------------------------


def _defect(surface: str, gaql_field: str, expected: str) -> QueryDefect:
    """Build the validation defect for one field of one surface.

    Args:
        surface: Row surface being parsed, such as ``"row_campaign"``.
        gaql_field: GAQL name of the offending field, such as ``"campaign.status"``.
        expected: What the field must hold. Never a row value.

    Returns:
        A :class:`QueryDefect` with ``source="google"``, ``endpoint="parse {surface}"`` and
        ``code="validation"``.
    """
    return QueryDefect(
        f"{gaql_field}: expected {expected}",
        source="google",
        endpoint=f"parse {surface}",
        code="validation",
        query_id=_QUERY_ID_BY_SURFACE.get(surface),
    )


def _walk(row: object, gaql_field: str, surface: str) -> object | None:
    """Return the value at a GAQL field's REST key path, or ``None`` when a key is absent.

    An absent (omitted) key and an explicit JSON ``null`` are told apart. Omission is how REST
    JSON encodes an unset or default value, so it reads as ``None`` and each reader applies its
    contract's meaning. No row contract declares a ``null`` type, so a key that is present with
    ``null`` is a defect: it would otherwise pass for omission and turn an unknown value into a
    trusted zero.

    Args:
        row: The raw row; must be a mapping.
        gaql_field: GAQL field or resource name in snake_case.
        surface: Row surface, for the error.

    Returns:
        The raw JSON value, or ``None`` when any key along the path is absent.

    Raises:
        QueryDefect: When ``gaql_field`` is not a GAQL name, the row or an intermediate value is
            not a JSON object, or any key along the path is present with JSON ``null``.
    """
    try:
        path = rest_path(gaql_field)
    except ValueError as exc:
        raise _defect(surface, gaql_field, "a GAQL field name") from exc
    node: object = row
    for key in path:
        if not isinstance(node, Mapping):
            raise _defect(surface, gaql_field, "a JSON object at every level of its key path")
        if key not in node:
            return None
        node = node[key]
        if node is None:
            raise _defect(surface, gaql_field, "a non-null value at every level of its key path")
    return node


def get_field(
    row: GoogleAdsRow, gaql_field: str, *, surface: str = _GENERIC_SURFACE
) -> object | None:
    """Return the raw value of one GAQL field of a REST ``GoogleAdsRow``.

    The field's snake_case segments are walked as their lowerCamelCase REST keys
    (:func:`~ads_engine.connectors.google_n8n.queries.rest_path`). ``partition.py`` reads keyset
    ids with it.

    Example::

        get_field({"campaign": {"id": "42"}}, "campaign.id")  # "42"
        get_field({"campaign": {"id": "42"}}, "campaign_budget.id")  # None
        get_field({"campaign": None}, "campaign.id")  # raises QueryDefect

    Args:
        row: The raw row.
        gaql_field: GAQL field name, such as ``"ad_group_criterion.criterion_id"``.
        surface: Row surface named in the error endpoint; defaults to a generic name.

    Returns:
        The raw JSON value (int64 values are still strings), or ``None`` when any key along the
        path is absent (omitted).

    Raises:
        QueryDefect: When ``gaql_field`` is not a GAQL name, the row or an intermediate value is
            not a JSON object, or any key along the path is present with JSON ``null``, which no
            row contract allows.
    """
    return _walk(row, gaql_field, surface)


def _int(value: object, *, surface: str = _GENERIC_SURFACE, gaql_field: str = "value") -> int:
    """Convert an INT64 string, a JSON integer or an integral ``Decimal`` to ``int``.

    Args:
        value: Raw value. ``bool`` is rejected although it subclasses ``int``.
        surface: Row surface, for the error.
        gaql_field: GAQL field, for the error.

    Returns:
        The integer.

    Raises:
        QueryDefect: When the value is none of the accepted forms.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _INT_RE.fullmatch(value) is not None:
        return int(value)
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        return int(value)
    raise _defect(surface, gaql_field, "an INT64 decimal string or a JSON integer")


def _decimal(
    value: object, *, surface: str = _GENERIC_SURFACE, gaql_field: str = "value"
) -> Decimal:
    """Convert a DOUBLE (``Decimal``, ``int``, ``float`` or numeric ``str``) to a ``Decimal``.

    A ``float`` goes through ``str`` (``0.3`` becomes ``Decimal("0.3")``), never through
    ``Decimal(float)``, which would carry the binary representation error.

    Args:
        value: Raw value. ``bool`` is rejected.
        surface: Row surface, for the error.
        gaql_field: GAQL field, for the error.

    Returns:
        The decimal value.

    Raises:
        QueryDefect: When the value is not numeric, or is NaN or infinite.
    """
    expected = "a finite JSON number"
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int) and not isinstance(value, bool):
        result = Decimal(value)
    elif isinstance(value, float | str):
        try:
            result = Decimal(str(value))
        except InvalidOperation as exc:
            raise _defect(surface, gaql_field, expected) from exc
    else:
        raise _defect(surface, gaql_field, expected)
    if not result.is_finite():
        raise _defect(surface, gaql_field, expected)
    return result


def _micros_to_amount(micros: int) -> Decimal:
    """Convert integer micros to an exact amount: ``1500000`` becomes ``Decimal("1.500000")``.

    Args:
        micros: Amount in millionths of the account currency, already bounded to
            ``0 .. 999999999999999999`` by :meth:`_RowReader.amount_micros` or
            :meth:`_RowReader.optional_amount_micros`.

    Returns:
        The amount with exponent ``-6``, exact and at most ``999999999999.999999``, so the
        ``numeric(18, 6)`` money columns store it unchanged.
    """
    return Decimal(micros).scaleb(_MICROS_EXPONENT, context=_DECIMAL_CONTEXT)


def _date(value: object, *, surface: str = _GENERIC_SURFACE, gaql_field: str = "value") -> date:
    """Parse a ``YYYY-MM-DD`` native Google date, as written.

    The date is Google's native day in the customer account's time zone; it is never converted
    or rebucketed.

    Args:
        value: Raw value.
        surface: Row surface, for the error.
        gaql_field: GAQL field, for the error.

    Returns:
        The calendar date.

    Raises:
        QueryDefect: When the value is not a valid ``YYYY-MM-DD`` string.
    """
    expected = "a YYYY-MM-DD date"
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise _defect(surface, gaql_field, expected)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _defect(surface, gaql_field, expected) from exc


def _bool(
    value: object,
    default: bool = False,
    *,
    surface: str = _GENERIC_SURFACE,
    gaql_field: str = "value",
) -> bool:
    """Read a JSON boolean whose absence is read as ``default``.

    Two presence kinds reach this reader. An implicit-presence ``bool`` is omitted from proto3
    JSON when ``false`` (``campaign_budget.explicitly_shared``, whose contract documents the
    omission), so its absence means ``false``. An explicit-presence ``optional bool``
    (``conversion_action.include_in_conversions_metric``) is serialized whenever it is set, a
    ``false`` included, so its absence means unset; the caller's ``default`` decides what an
    unset value counts as.

    Args:
        value: Raw value; ``None`` (absent) yields ``default``.
        default: Value of an absent field.
        surface: Row surface, for the error.
        gaql_field: GAQL field, for the error.

    Returns:
        The boolean.

    Raises:
        QueryDefect: When the value is present and not a JSON boolean.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise _defect(surface, gaql_field, "a JSON boolean")


class _RowReader:
    """Typed field access to one raw row of one surface.

    Every method takes a GAQL field name and raises a :class:`QueryDefect` naming the surface
    and that field, so each parser reads as a list of fields and their kinds.
    """

    __slots__ = ("_row", "_surface")

    def __init__(self, row: GoogleAdsRow, surface: str) -> None:
        self._row = row
        self._surface = surface

    def defect(self, gaql_field: str, expected: str) -> QueryDefect:
        """Build the validation defect for one field of this row's surface."""
        return _defect(self._surface, gaql_field, expected)

    def raw(self, gaql_field: str) -> object | None:
        """Return the raw value, or ``None`` when absent (omitted); explicit ``null`` raises."""
        return _walk(self._row, gaql_field, self._surface)

    def _present(self, gaql_field: str, expected: str) -> object:
        """Return the raw value of a required field, raising when it is absent."""
        value = self.raw(gaql_field)
        if value is None:
            raise self.defect(gaql_field, expected)
        return value

    def _bounded(self, gaql_field: str, value: int, low: int, high: int) -> int:
        """Return ``value`` when it lies in ``low .. high``, else raise."""
        if not low <= value <= high:
            raise self.defect(gaql_field, f"an integer from {low} to {high}")
        return value

    def identifier(self, gaql_field: str) -> int:
        """Read a required INT64 id in ``0 .. INT64_MAX``."""
        value = self._present(gaql_field, "an INT64 id")
        number = _int(value, surface=self._surface, gaql_field=gaql_field)
        return self._bounded(gaql_field, number, 0, INT64_MAX)

    def count(self, gaql_field: str) -> int:
        """Read an additive INT64 metric in ``0 .. INT64_MAX``.

        Absent (omitted) means zero, because proto3 JSON omits zero values; an explicit JSON
        ``null`` is a defect, never zero.
        """
        value = self.raw(gaql_field)
        if value is None:
            return 0
        number = _int(value, surface=self._surface, gaql_field=gaql_field)
        return self._bounded(gaql_field, number, 0, INT64_MAX)

    def amount_micros(self, gaql_field: str) -> int:
        """Read additive INT64 micros of an emitted amount, such as ``metrics.cost_micros``.

        The micros are bounded to ``0 .. 999999999999999999`` so their amount fits the
        ``numeric(18, 6)`` money columns; a larger value is a defect, never clamped, and an
        accepted value is returned exact. Absent (omitted) means zero, because proto3 JSON omits
        zero values; an explicit JSON ``null`` is a defect, never zero.
        """
        value = self.raw(gaql_field)
        if value is None:
            return 0
        number = _int(value, surface=self._surface, gaql_field=gaql_field)
        return self._bounded(gaql_field, number, 0, _AMOUNT_MICROS_MAX)

    def optional_amount_micros(self, gaql_field: str) -> int | None:
        """Read amount micros whose absence means unknown (``None``), such as a budget amount.

        Bounded as :meth:`amount_micros`: ``0 .. 999999999999999999``, exact, never clamped.
        """
        if self.raw(gaql_field) is None:
            return None
        return self.amount_micros(gaql_field)

    def score(self, gaql_field: str) -> int | None:
        """Read a 1-10 quality score; absent means unavailable (``None``), never zero."""
        value = self.raw(gaql_field)
        if value is None:
            return None
        number = _int(value, surface=self._surface, gaql_field=gaql_field)
        return self._bounded(gaql_field, number, _QUALITY_SCORE_MIN, _QUALITY_SCORE_MAX)

    def _non_negative(self, gaql_field: str) -> Decimal | None:
        """Return a non-negative DOUBLE exactly as returned, or ``None`` when absent (omitted).

        A negative value is a defect; a negative zero is zero and is returned without its sign.
        An explicit JSON ``null`` is a defect, never unavailable.
        """
        value = self.raw(gaql_field)
        if value is None:
            return None
        number = _decimal(value, surface=self._surface, gaql_field=gaql_field)
        if number < _ZERO:
            raise self.defect(gaql_field, "a non-negative finite JSON number")
        return number.copy_abs()

    def _exact(self, gaql_field: str, number: Decimal) -> Decimal:
        """Return a non-negative, unsigned DOUBLE measure that ``numeric(18, 6)`` holds exactly.

        The value is returned exactly as given, exponent included (``Decimal("1.0000000")``
        stays as it is), and is never rounded. A value of ``10**12`` or more overflows the
        column, and one with a nonzero digit past the sixth decimal place would be changed by
        storage: ``2.9999996`` would read as a whole count of 3, ``0.0000003`` as no
        conversion at all. Both are defects naming only the field and its bounds. The range is
        checked first, so the six-place comparison stays within the context's 38 digits.
        """
        if number >= _NUMERIC_LIMIT:
            raise self.defect(gaql_field, _NUMERIC_RANGE_EXPECTED)
        # Truncating to six places changes the value exactly when a nonzero digit lies beyond.
        six_places = number.quantize(_AMOUNT_QUANTUM, rounding=ROUND_DOWN, context=_DECIMAL_CONTEXT)
        if six_places != number:
            raise self.defect(gaql_field, _NUMERIC_SCALE_EXPECTED)
        return number

    def _truncated(self, gaql_field: str, number: Decimal) -> Decimal:
        """Return an unsigned fraction from 0 to 1 as its ``numeric(18, 6)`` column stores it.

        A value with at most six decimal places is returned exactly as given, exponent
        included. A finer value is rounded toward zero to six places here, in the parser's own
        decimal context, so the row already equals what storage holds and no rounding is left
        to the database. A positive value below ``0.000001`` would be stored as zero, a share
        Google reported as positive, so it is a defect naming only the field and its bounds.
        """
        # Google returns impression shares as full doubles, most of them finer than six places,
        # so they are rounded rather than rejected. Rounding toward zero keeps every comparison
        # with a six-place threshold t unchanged: x >= t exactly when the stored value >= t, so
        # the capped-day test against budget.capped_lost_is_share (default 0.10) on the
        # budget-lost share reads the same on the stored value.
        stored = number.quantize(_AMOUNT_QUANTUM, rounding=ROUND_DOWN, context=_DECIMAL_CONTEXT)
        if stored == number:
            return number
        if stored.is_zero():
            raise self.defect(gaql_field, _FRACTION_POSITIVE_EXPECTED)
        return stored

    def measure(self, gaql_field: str) -> Decimal:
        """Read an additive, non-negative DOUBLE metric, such as ``conversions``.

        Absent (omitted) means zero, because proto3 JSON omits zero values; an explicit JSON
        ``null`` is a defect, never zero. Otherwise as :meth:`optional_measure`.
        """
        value = self.optional_measure(gaql_field)
        return _ZERO if value is None else value

    def optional_measure(self, gaql_field: str) -> Decimal | None:
        """Read a non-negative DOUBLE whose absence (omission) means unavailable (``None``).

        The DOUBLE metrics read this way (conversions, conversion value) are counts or amounts,
        so a negative value is a defect; a negative zero is zero and is returned without its
        sign. Fractions are kept exactly as returned, exponent included, and never rounded:
        rounding could turn a fractional count whole or a positive outcome into zero. Google
        returns at most six decimal places, so a value the ``numeric(18, 6)`` column cannot
        hold exactly, ``10**12`` or more or one finer than six places (``2.3333333333333335``),
        is a defect. An explicit JSON ``null`` is a defect, never unavailable.
        """
        number = self._non_negative(gaql_field)
        return None if number is None else self._exact(gaql_field, number)

    def fraction(self, gaql_field: str) -> Decimal | None:
        """Read a DOUBLE fraction from 0 to 1 inclusive, such as an impression share.

        A value with at most six decimal places is kept as returned, Google's reporting
        sentinels included (``0.9001`` for a budget-lost share above 90%, ``0.0999`` for an
        impression share below 10%). Google returns shares as full doubles, so a finer one is
        rounded toward zero to six places, as the ``numeric(18, 6)`` column stores it
        (``0.09999999`` becomes ``0.099999``, still below 10%), and a positive value below
        ``0.000001``, which would be stored as zero, is a defect. A negative zero is returned
        without its sign. Absent (omitted) means unavailable (``None``), never zero; an
        explicit JSON ``null`` or a value outside 0 to 1 is a defect.
        """
        value = self.raw(gaql_field)
        if value is None:
            return None
        number = _decimal(value, surface=self._surface, gaql_field=gaql_field)
        if not _ZERO <= number <= _ONE:
            raise self.defect(gaql_field, "a fraction from 0 to 1")
        return self._truncated(gaql_field, number.copy_abs())

    def text(self, gaql_field: str) -> str:
        """Read a required string (a name, an enum name or an id-like string) unchanged.

        An absent value or a non-string is a defect expecting "a string". A string holding
        U+0000 or an unpaired surrogate is a defect too, because PostgreSQL ``text`` can store
        neither; like every defect, it names the field only, never the value. Every other code
        point, astral characters, tabs and newlines included, is kept as returned.
        """
        value = self._present(gaql_field, "a string")
        if not isinstance(value, str):
            raise self.defect(gaql_field, "a string")
        # An ASCII string holds no surrogate, so it needs only the NUL check.
        if value.isascii():
            unstorable = "\x00" in value
        else:
            unstorable = _UNSTORABLE_TEXT_RE.search(value) is not None
        if unstorable:
            raise self.defect(gaql_field, _STORABLE_TEXT)
        return value

    def optional_text(self, gaql_field: str) -> str | None:
        """Read an optional string; absent means ``None``.

        A present value is read by :meth:`text`, so a non-string, U+0000 or an unpaired
        surrogate is a defect; an explicit JSON ``null`` is a defect, never ``None``.
        """
        if self.raw(gaql_field) is None:
            return None
        return self.text(gaql_field)

    def day(self, gaql_field: str) -> date:
        """Read a required native ``YYYY-MM-DD`` date."""
        value = self._present(gaql_field, "a YYYY-MM-DD date")
        return _date(value, surface=self._surface, gaql_field=gaql_field)

    def flag(self, gaql_field: str) -> bool:
        """Read a boolean; absent means ``False`` for both presence kinds.

        Absence is an implicit-presence ``bool`` omitted because it is ``false``, or an
        explicit-presence ``optional bool`` left unset (a set ``false`` is sent); see
        :func:`_bool`.
        """
        return _bool(self.raw(gaql_field), surface=self._surface, gaql_field=gaql_field)

    def status(self, gaql_field: str) -> tuple[NormalisedStatus, str]:
        """Read a campaign-like status as ``(normalised, raw)`` through ``CAMPAIGN_STATUS_MAP``."""
        raw_status = self.text(gaql_field)
        normalised = CAMPAIGN_STATUS_MAP.get(raw_status)
        if normalised is None:
            raise self.defect(gaql_field, "one of " + ", ".join(CAMPAIGN_STATUS_MAP))
        return normalised, raw_status

    def enum(self, gaql_field: str, values: frozenset[str], *, expected: str | None = None) -> str:
        """Read a required enum name that must be one of ``values``; it is returned raw.

        A defect lists ``values`` sorted ("one of ..."), or states ``expected`` instead when
        given: a complete description, never a row value, for a list too long to fit the
        error message cap.
        """
        raw_value = self.text(gaql_field)
        if raw_value not in values:
            if expected is None:
                expected = "one of " + ", ".join(sorted(values))
            raise self.defect(gaql_field, expected)
        return raw_value

    def optional_enum(
        self, gaql_field: str, values: frozenset[str], *, expected: str | None = None
    ) -> str | None:
        """Read an optional enum name; absent means ``None``, present must be one of ``values``.

        ``expected`` replaces the listed values in a defect, as in :meth:`enum`.
        """
        if self.raw(gaql_field) is None:
            return None
        return self.enum(gaql_field, values, expected=expected)

    def budget_period(self, gaql_field: str) -> BudgetPeriod:
        """Read ``campaign_budget.period``; absent means Google's default, ``DAILY``."""
        raw_period = self.optional_text(gaql_field)
        if raw_period is None:
            return _DEFAULT_BUDGET_PERIOD
        period = _BUDGET_PERIOD_MAP.get(raw_period)
        if period is None:
            raise self.defect(gaql_field, "one of " + ", ".join(_BUDGET_PERIOD_MAP))
        return period

    def micros_rate(self, gaql_field: str) -> Decimal | None:
        """Read a DOUBLE in micros (such as ``average_cpc``) as an amount rounded to 6 places.

        The value is derived (cost divided by clicks), not exact, so it is quantized half-even
        to the micro, exactly once, from the value as returned. Absent means unavailable. Cost
        and clicks are never negative, so a negative value is a defect; the sign is checked
        before rounding, so no negative value can round to zero. The amount must fit the
        ``numeric(18, 6)`` column: one that rounds to ``10**12`` or more is a defect, checked on
        the micros before rounding so a huge value cannot exceed the context's 38 digits, and
        so is a positive value of at most half a micro, which would round to zero.
        """
        micros = self._non_negative(gaql_field)
        if micros is None:
            return None
        if micros >= _MICROS_LIMIT:
            raise self.defect(gaql_field, _MICROS_RATE_RANGE_EXPECTED)
        # Rounding the micros to a whole micro, then shifting the point, rounds the exact value
        # once: the shift is exact, whereas shifting first would round to 38 digits beforehand.
        whole_micros = micros.quantize(_ONE, rounding=ROUND_HALF_EVEN, context=_DECIMAL_CONTEXT)
        amount = whole_micros.scaleb(_MICROS_EXPONENT, context=_DECIMAL_CONTEXT)
        if amount >= _NUMERIC_LIMIT:
            raise self.defect(gaql_field, _MICROS_RATE_RANGE_EXPECTED)
        if amount.is_zero() and not micros.is_zero():
            raise self.defect(gaql_field, _MICROS_RATE_POSITIVE_EXPECTED)
        return amount


# ---------------------------------------------------------------------------------------------
# Row types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CustomerRow:
    """One Q-G1 row: the Google Ads customer account.

    Attributes:
        customer_id: ``customer.id``.
        descriptive_name: ``customer.descriptive_name``; ``None`` when the account has none.
        currency_code: ISO 4217 currency of every amount in the account. Amounts are never
            converted or summed across currencies.
        time_zone: IANA time-zone id of the account, validated with :class:`zoneinfo.ZoneInfo`.
            Google's ``segments.date`` values are dates in this zone.
    """

    customer_id: int
    descriptive_name: str | None
    currency_code: str
    time_zone: str


@dataclass(frozen=True, slots=True)
class CampaignRow:
    """One Q-G2 or Q-G2b row: a campaign and the budget it draws from.

    Attributes:
        campaign_id: ``campaign.id``.
        name: ``campaign.name``.
        status: Normalised status through :data:`CAMPAIGN_STATUS_MAP`; a Q-G2b ``REMOVED`` row
            is ``ended``.
        raw_status: ``campaign.status`` as returned.
        advertising_channel_type: ``campaign.advertising_channel_type`` as returned, one of the
            contract's ``AdvertisingChannelTypeEnum`` names, stored as the campaign's objective
            or channel; ``None`` only when the key is absent.
        budget_id: ``campaign_budget.id``; ``None`` when the row has no ``campaignBudget``.
        budget_period: ``DAILY`` or ``CUSTOM_PERIOD``; ``DAILY`` when the budget omits it.
        budget_amount_micros: The budget's own amount in micros at native precision: Google's
            ``amount_micros`` for a ``DAILY`` budget, ``total_amount_micros`` for a
            ``CUSTOM_PERIOD`` (lifetime) budget; the other period's field is never present.
            From 0 to 999999999999999999, so the amount fits ``numeric(18, 6)``; ``None`` when
            the period's own field is absent (unknown, never zero).
        budget_amount: ``budget_amount_micros`` as an exact amount in the account currency, at
            most ``999999999999.999999``.
        budget_explicitly_shared: Whether the budget is an explicitly shared budget, a decision
            subject of its own; ``False`` when absent or when there is no budget. Never ``True``
            for a ``CUSTOM_PERIOD`` budget.
    """

    campaign_id: int
    name: str
    status: NormalisedStatus
    raw_status: str
    advertising_channel_type: str | None
    budget_id: int | None
    budget_period: BudgetPeriod | None
    budget_amount_micros: int | None
    budget_amount: Decimal | None
    budget_explicitly_shared: bool

    @property
    def daily_budget(self) -> Decimal | None:
        """The budget amount when the period is ``DAILY``, else ``None``."""
        return self.budget_amount if self.budget_period == "DAILY" else None

    @property
    def total_budget(self) -> Decimal | None:
        """The budget amount when the period is ``CUSTOM_PERIOD`` (lifetime), else ``None``."""
        return self.budget_amount if self.budget_period == "CUSTOM_PERIOD" else None


@dataclass(frozen=True, slots=True)
class CampaignDailyRow:
    """One Q-G3 row: one campaign on one native Google day (``fact_campaign_daily``).

    Attributes:
        campaign_id: ``campaign.id``.
        date: ``segments.date``, the native day in the account time zone.
        cost: ``cost_micros`` as an exact amount in the account currency, at most
            ``999999999999.999999`` (``numeric(18, 6)``).
        cost_micros: ``metrics.cost_micros``, from 0 to 999999999999999999.
        impressions: ``metrics.impressions``.
        clicks: ``metrics.clicks``.
        conversions: ``metrics.conversions``, kept as a decimal (may be fractional) exactly as
            returned, never rounded: at most six decimal places, never negative and below
            ``10**12``, so ``numeric(18, 6)`` stores it unchanged.
        conversions_value: ``metrics.conversions_value`` in the account currency, kept and
            bounded as ``conversions``.
        search_budget_lost_impression_share: Fraction from 0 to 1 inclusive, as returned
            (Google reports "> 90%" as ``0.9001``) or rounded toward zero to six places when
            finer, so a comparison with a threshold of at most six places, such as
            ``budget.capped_lost_is_share`` (default 0.10), reads the same as on the value
            returned; never zero when Google reports a positive value; ``None`` when
            unavailable. Stored as ``budget_lost_is``.
        search_impression_share: Fraction from 0 to 1 inclusive, as returned (Google reports
            "< 10%" as ``0.0999``) or rounded toward zero to six places when finer; never zero
            when Google reports a positive value; ``None`` when unavailable. Stored as
            ``impression_share``.
    """

    campaign_id: int
    date: date
    cost: Decimal
    cost_micros: int
    impressions: int
    clicks: int
    conversions: Decimal
    conversions_value: Decimal
    search_budget_lost_impression_share: Decimal | None
    search_impression_share: Decimal | None


@dataclass(frozen=True, slots=True)
class AdGroupRow:
    """One Q-G4 row: an ad group.

    Attributes:
        ad_group_id: ``ad_group.id``.
        campaign_id: ``campaign.id`` of the ad group's campaign.
        name: ``ad_group.name``.
        status: Normalised status through :data:`CAMPAIGN_STATUS_MAP`.
        raw_status: ``ad_group.status`` as returned.
    """

    ad_group_id: int
    campaign_id: int
    name: str
    status: NormalisedStatus
    raw_status: str


@dataclass(frozen=True, slots=True)
class KeywordViewRow:
    """One Q-G5 row: one keyword on one native Google day (``fact_keyword_daily``).

    Attributes:
        ad_group_id: ``ad_group.id``; with ``criterion_id`` the keyword's identity.
        criterion_id: ``ad_group_criterion.criterion_id``, unique only within its ad group.
        date: ``segments.date``, the native day in the account time zone.
        historical_quality_score: ``metrics.historical_quality_score`` from 1 to 10; ``None`` on
            days without a score.
        average_cpc: ``metrics.average_cpc`` converted from micros and rounded half-even to six
            places (it is derived, not exact); never negative, below ``10**12`` and never zero
            when Google reports a positive value; ``None`` without clicks.
        cost: ``cost_micros`` as an exact amount in the account currency, at most
            ``999999999999.999999`` (``numeric(18, 6)``).
        cost_micros: ``metrics.cost_micros``, from 0 to 999999999999999999.
        clicks: ``metrics.clicks``.
        impressions: ``metrics.impressions``.
        conversions: ``metrics.conversions``, kept as a decimal exactly as returned, never
            rounded: at most six decimal places, never negative and below ``10**12``.
    """

    ad_group_id: int
    criterion_id: int
    date: date
    historical_quality_score: int | None
    average_cpc: Decimal | None
    cost: Decimal
    cost_micros: int
    clicks: int
    impressions: int
    conversions: Decimal


@dataclass(frozen=True, slots=True)
class SearchTermRow:
    """One Q-G6 row: one search term in one ad group on one native day (``fact_search_term_daily``).

    Attributes:
        campaign_id: ``campaign.id``.
        ad_group_id: ``ad_group.id``.
        date: ``segments.date``, the native day in the account time zone.
        search_term: ``search_term_view.search_term``.
        term_status: ``search_term_view.status`` as returned, one of
            :data:`SEARCH_TERM_STATUSES`.
        matched_criterion_id: Criterion id of the matched keyword in canonical decimal form,
            taken from the ``segments.keyword.ad_group_criterion`` resource name, whose ad-group
            id must equal ``ad_group_id`` (a criterion id is unique only within its ad group);
            ``""`` when the row matched no keyword criterion (the fact column is not nullable).
        matched_keyword_text: ``segments.keyword.info.text``; ``None`` when absent.
        matched_match_type: ``segments.keyword.info.match_type`` as returned, one of the
            contract's ``KeywordMatchTypeEnum`` names; ``None`` when absent.
        cost: ``cost_micros`` as an exact amount in the account currency, at most
            ``999999999999.999999`` (``numeric(18, 6)``).
        cost_micros: ``metrics.cost_micros``, from 0 to 999999999999999999.
        clicks: ``metrics.clicks``.
        impressions: ``metrics.impressions``.
        conversions: ``metrics.conversions``, kept as a decimal exactly as returned, never
            rounded, so any fraction still counts as converting: at most six decimal places,
            never negative and below ``10**12``.
        conversions_value: ``metrics.conversions_value`` in the account currency, kept and
            bounded as ``conversions``.
    """

    campaign_id: int
    ad_group_id: int
    date: date
    search_term: str
    term_status: str
    matched_criterion_id: str
    matched_keyword_text: str | None
    matched_match_type: str | None
    cost: Decimal
    cost_micros: int
    clicks: int
    impressions: int
    conversions: Decimal
    conversions_value: Decimal


@dataclass(frozen=True, slots=True)
class KeywordRow:
    """One Q-G7 row: a current keyword, including keywords with no traffic.

    Attributes:
        ad_group_id: ``ad_group.id``.
        criterion_id: ``ad_group_criterion.criterion_id``, unique only within its ad group.
        campaign_id: ``campaign.id``.
        text: ``ad_group_criterion.keyword.text``.
        match_type: ``ad_group_criterion.keyword.match_type`` as returned, one of the
            contract's ``KeywordMatchTypeEnum`` names.
        status: Normalised status through :data:`CAMPAIGN_STATUS_MAP`.
        raw_status: ``ad_group_criterion.status`` as returned.
        quality_score: ``ad_group_criterion.quality_info.quality_score`` from 1 to 10; ``None``
            when Google has not populated it.
    """

    ad_group_id: int
    criterion_id: int
    campaign_id: int
    text: str
    match_type: str
    status: NormalisedStatus
    raw_status: str
    quality_score: int | None


@dataclass(frozen=True, slots=True)
class ConversionActionRow:
    """One Q-G8 row: a conversion action and its counting contract.

    B4 reads these fields to decide whether a day's ``metrics.conversions`` is an unweighted
    event count, so every enum is kept raw and never normalised.

    Attributes:
        conversion_action_id: ``conversion_action.id``.
        name: ``conversion_action.name``.
        status: ``conversion_action.status`` as returned: ``ENABLED``, ``HIDDEN``, ``REMOVED``,
            ``UNKNOWN`` or ``UNSPECIFIED``.
        include_in_conversions_metric: Whether the action counts in ``metrics.conversions``;
            ``False`` when absent.
        counting_type: ``ONE_PER_CLICK``, ``MANY_PER_CLICK``, ``UNKNOWN`` or ``UNSPECIFIED`` as
            returned; ``None`` when absent.
        attribution_model: ``attribution_model_settings.attribution_model`` as returned, one of
            the contract's ``AttributionModelEnum`` names, the retired rule-based models
            included (for example ``GOOGLE_ADS_LAST_CLICK``); ``None`` when the action has no
            attribution settings, which B4 treats as unknown, never as last click.
    """

    conversion_action_id: int
    name: str
    status: str
    include_in_conversions_metric: bool
    counting_type: str | None
    attribution_model: str | None


# ---------------------------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------------------------


def parse_customer(row: GoogleAdsRow) -> CustomerRow:
    """Parse one Q-G1 row (contract ``row_customer``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The customer account.

    Raises:
        QueryDefect: When the id, currency code or time zone is absent or invalid.
    """
    reader = _RowReader(row, "row_customer")
    currency_code = reader.text("customer.currency_code")
    if _CURRENCY_RE.fullmatch(currency_code) is None:
        raise reader.defect("customer.currency_code", "an ISO 4217 currency code")
    time_zone = reader.text("customer.time_zone")
    try:
        ZoneInfo(time_zone)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise reader.defect("customer.time_zone", "an IANA time-zone id") from exc
    return CustomerRow(
        customer_id=reader.identifier("customer.id"),
        descriptive_name=reader.optional_text("customer.descriptive_name"),
        currency_code=currency_code,
        time_zone=time_zone,
    )


def parse_campaign(row: GoogleAdsRow) -> CampaignRow:
    """Parse one Q-G2 or Q-G2b row (contract ``row_campaign``).

    The budget amount is read from the field of the budget's own period: ``amount_micros`` for
    ``DAILY``, ``total_amount_micros`` for ``CUSTOM_PERIOD``. The two are mutually exclusive, so
    the other period's field must be absent, and a ``CUSTOM_PERIOD`` budget cannot be explicitly
    shared. A row without ``campaignBudget`` has no budget fields; a ``campaignBudget`` object
    must carry its id.

    Example::

        parse_campaign(
            {
                "campaign": {
                    "id": "7",
                    "name": "Brand",
                    "status": "ENABLED",
                    "advertisingChannelType": "SEARCH",
                },
                "campaignBudget": {
                    "id": "9",
                    "period": "CUSTOM_PERIOD",
                    "totalAmountMicros": "500000000",
                },
            }
        ).total_budget  # Decimal("500.000000")

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The campaign with its budget.

    Raises:
        QueryDefect: When a required field is absent, the status or period is not mapped, a
            present channel type is not one of the contract's enum names, the budget carries
            the amount field of the other period, its own amount lies outside
            ``0 .. 999999999999999999`` micros (the ``numeric(18, 6)`` range), a
            ``CUSTOM_PERIOD`` budget is explicitly shared, or a value has the wrong type.
    """
    reader = _RowReader(row, "row_campaign")
    status, raw_status = reader.status("campaign.status")
    budget_id: int | None = None
    budget_period: BudgetPeriod | None = None
    amount_micros: int | None = None
    shared = False
    if reader.raw("campaign_budget") is not None:
        budget_id = reader.identifier("campaign_budget.id")
        budget_period = reader.budget_period("campaign_budget.period")
        for period, amount_field in _BUDGET_AMOUNT_FIELD.items():
            if period != budget_period and reader.raw(amount_field) is not None:
                raise reader.defect(
                    amount_field, f"absent unless campaign_budget.period is {period}"
                )
        amount_micros = reader.optional_amount_micros(_BUDGET_AMOUNT_FIELD[budget_period])
        shared = reader.flag("campaign_budget.explicitly_shared")
        if shared and budget_period == "CUSTOM_PERIOD":
            raise reader.defect(
                "campaign_budget.explicitly_shared",
                "false or absent when campaign_budget.period is CUSTOM_PERIOD",
            )
    return CampaignRow(
        campaign_id=reader.identifier("campaign.id"),
        name=reader.text("campaign.name"),
        status=status,
        raw_status=raw_status,
        # The contract always returns the channel type; an absent key stays None (unknown)
        # rather than failing the whole entity sync, as the field's declared type allows.
        advertising_channel_type=reader.optional_enum(
            "campaign.advertising_channel_type", _ADVERTISING_CHANNEL_TYPES
        ),
        budget_id=budget_id,
        budget_period=budget_period,
        budget_amount_micros=amount_micros,
        budget_amount=None if amount_micros is None else _micros_to_amount(amount_micros),
        budget_explicitly_shared=shared,
    )


def parse_campaign_daily(row: GoogleAdsRow) -> CampaignDailyRow:
    """Parse one Q-G3 row (contract ``row_campaign_daily``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The campaign's metrics on one native day.

    Raises:
        QueryDefect: When the campaign id or date is absent, a metric is negative, an
            impression share lies outside 0 to 1, the cost does not fit ``numeric(18, 6)``, a
            conversion metric is not exactly storable there (``10**12`` or more, or finer than
            six decimal places), a positive share would be stored as zero, a key is JSON
            ``null``, or a value has the wrong type.
    """
    reader = _RowReader(row, "row_campaign_daily")
    cost_micros = reader.amount_micros("metrics.cost_micros")
    return CampaignDailyRow(
        campaign_id=reader.identifier("campaign.id"),
        date=reader.day("segments.date"),
        cost=_micros_to_amount(cost_micros),
        cost_micros=cost_micros,
        impressions=reader.count("metrics.impressions"),
        clicks=reader.count("metrics.clicks"),
        conversions=reader.measure("metrics.conversions"),
        conversions_value=reader.measure("metrics.conversions_value"),
        search_budget_lost_impression_share=reader.fraction(
            "metrics.search_budget_lost_impression_share"
        ),
        search_impression_share=reader.fraction("metrics.search_impression_share"),
    )


def parse_ad_group(row: GoogleAdsRow) -> AdGroupRow:
    """Parse one Q-G4 row (contract ``row_ad_group``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The ad group.

    Raises:
        QueryDefect: When a required field is absent, the status is not mapped, or a value has
            the wrong type.
    """
    reader = _RowReader(row, "row_ad_group")
    status, raw_status = reader.status("ad_group.status")
    return AdGroupRow(
        ad_group_id=reader.identifier("ad_group.id"),
        campaign_id=reader.identifier("campaign.id"),
        name=reader.text("ad_group.name"),
        status=status,
        raw_status=raw_status,
    )


def parse_keyword_view(row: GoogleAdsRow) -> KeywordViewRow:
    """Parse one Q-G5 row (contract ``row_keyword_view``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The keyword's metrics on one native day.

    Raises:
        QueryDefect: When an id or the date is absent, the quality score lies outside 1 to 10,
            a metric (``average_cpc`` included) is negative, the cost or average CPC does not
            fit ``numeric(18, 6)`` or a positive average CPC would be stored as zero, the
            conversions are not exactly storable there (``10**12`` or more, or finer than six
            decimal places), a key is JSON ``null``, or a value has the wrong type.
    """
    reader = _RowReader(row, "row_keyword_view")
    cost_micros = reader.amount_micros("metrics.cost_micros")
    return KeywordViewRow(
        ad_group_id=reader.identifier("ad_group.id"),
        criterion_id=reader.identifier("ad_group_criterion.criterion_id"),
        date=reader.day("segments.date"),
        historical_quality_score=reader.score("metrics.historical_quality_score"),
        average_cpc=reader.micros_rate("metrics.average_cpc"),
        cost=_micros_to_amount(cost_micros),
        cost_micros=cost_micros,
        clicks=reader.count("metrics.clicks"),
        impressions=reader.count("metrics.impressions"),
        conversions=reader.measure("metrics.conversions"),
    )


def _matched_criterion_id(reader: _RowReader, ad_group_id: int) -> str:
    """Return the criterion id of ``segments.keyword.ad_group_criterion``, or ``""`` without one.

    The resource name has the form ``customers/{customer}/adGroupCriteria/{ad_group}~{criterion}``.
    A criterion id is unique only within its ad group, so the name's ad-group id must be the
    row's own ``ad_group.id``; only then is the part after ``~`` returned, in canonical decimal
    form.

    Args:
        reader: Reader of the Q-G6 row.
        ad_group_id: The row's ``ad_group.id``.

    Returns:
        The criterion id, or ``""`` when the row carries no matched criterion.

    Raises:
        QueryDefect: When the value is not a string of that form, an id in it exceeds the INT64
            range, or its ad-group id is not ``ad_group_id``.
    """
    gaql_field = "segments.keyword.ad_group_criterion"
    resource_name = reader.optional_text(gaql_field)
    if resource_name is None:
        return ""
    match = _AD_GROUP_CRITERION_RE.fullmatch(resource_name)
    if match is None:
        raise reader.defect(gaql_field, _AD_GROUP_CRITERION_FORM)
    customer, ad_group, criterion = (int(part) for part in match.groups())
    if max(customer, ad_group, criterion) > INT64_MAX:
        raise reader.defect(gaql_field, _AD_GROUP_CRITERION_IDS)
    if ad_group != ad_group_id:
        raise reader.defect(gaql_field, _AD_GROUP_CRITERION_PARENT)
    return str(criterion)


def parse_search_term(row: GoogleAdsRow) -> SearchTermRow:
    """Parse one Q-G6 row (contract ``row_search_term_view``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The search term's metrics in one ad group on one native day.

    Raises:
        QueryDefect: When a required field is absent, the term status is not one of
            :data:`SEARCH_TERM_STATUSES`, the matched criterion is not an ``adGroupCriteria``
            resource name of the row's own ad group with ids in the INT64 range, a present
            match type is not one of the contract's enum names, a metric is negative, the cost
            does not fit ``numeric(18, 6)``, a conversion metric is not exactly storable there
            (``10**12`` or more, or finer than six decimal places), a key is JSON ``null``, or a
            value has the wrong type.
    """
    reader = _RowReader(row, "row_search_term_view")
    term_status = reader.text("search_term_view.status")
    if term_status not in SEARCH_TERM_STATUSES:
        raise reader.defect(
            "search_term_view.status", "one of " + ", ".join(sorted(SEARCH_TERM_STATUSES))
        )
    cost_micros = reader.amount_micros("metrics.cost_micros")
    campaign_id = reader.identifier("campaign.id")
    ad_group_id = reader.identifier("ad_group.id")
    return SearchTermRow(
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        date=reader.day("segments.date"),
        search_term=reader.text("search_term_view.search_term"),
        term_status=term_status,
        matched_criterion_id=_matched_criterion_id(reader, ad_group_id),
        matched_keyword_text=reader.optional_text("segments.keyword.info.text"),
        matched_match_type=reader.optional_enum(
            "segments.keyword.info.match_type", _KEYWORD_MATCH_TYPES
        ),
        cost=_micros_to_amount(cost_micros),
        cost_micros=cost_micros,
        clicks=reader.count("metrics.clicks"),
        impressions=reader.count("metrics.impressions"),
        conversions=reader.measure("metrics.conversions"),
        conversions_value=reader.measure("metrics.conversions_value"),
    )


def parse_keyword(row: GoogleAdsRow) -> KeywordRow:
    """Parse one Q-G7 row (contract ``row_ad_group_criterion``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The keyword, with its quality score when Google has populated one.

    Raises:
        QueryDefect: When a required field is absent, the status is not mapped, the match type
            is not one of the contract's enum names, the quality score lies outside 1 to 10, or
            a value has the wrong type.
    """
    reader = _RowReader(row, "row_ad_group_criterion")
    status, raw_status = reader.status("ad_group_criterion.status")
    return KeywordRow(
        ad_group_id=reader.identifier("ad_group.id"),
        criterion_id=reader.identifier("ad_group_criterion.criterion_id"),
        campaign_id=reader.identifier("campaign.id"),
        text=reader.text("ad_group_criterion.keyword.text"),
        match_type=reader.enum("ad_group_criterion.keyword.match_type", _KEYWORD_MATCH_TYPES),
        status=status,
        raw_status=raw_status,
        quality_score=reader.score("ad_group_criterion.quality_info.quality_score"),
    )


def parse_conversion_action(row: GoogleAdsRow) -> ConversionActionRow:
    """Parse one Q-G8 row (contract ``row_conversion_action``).

    Args:
        row: Raw REST ``GoogleAdsRow``.

    Returns:
        The conversion action with its raw counting contract.

    Raises:
        QueryDefect: When the id, name or status is absent, the status or a present counting
            type or attribution model is not one of the contract's enum names, or a value has
            the wrong type.
    """
    reader = _RowReader(row, "row_conversion_action")
    return ConversionActionRow(
        conversion_action_id=reader.identifier("conversion_action.id"),
        name=reader.text("conversion_action.name"),
        status=reader.enum("conversion_action.status", _CONVERSION_ACTION_STATUSES),
        include_in_conversions_metric=reader.flag(
            "conversion_action.include_in_conversions_metric"
        ),
        # The contract always returns the counting type; an absent key stays None, which B4
        # reads as an unknown counting contract, instead of failing the whole entity sync.
        counting_type=reader.optional_enum("conversion_action.counting_type", _COUNTING_TYPES),
        attribution_model=reader.optional_enum(
            "conversion_action.attribution_model_settings.attribution_model",
            _ATTRIBUTION_MODELS,
            expected=_ATTRIBUTION_MODEL_EXPECTED,
        ),
    )


PARSERS: Final[Mapping[str, Callable[[GoogleAdsRow], object]]] = MappingProxyType(
    {
        "row_customer": parse_customer,
        "row_campaign": parse_campaign,
        "row_campaign_daily": parse_campaign_daily,
        "row_ad_group": parse_ad_group,
        "row_keyword_view": parse_keyword_view,
        "row_search_term_view": parse_search_term,
        "row_ad_group_criterion": parse_keyword,
        "row_conversion_action": parse_conversion_action,
    }
)
"""Row parser per surface: the ``row_surface`` of every template in ``QUERIES``.

``client.py`` parses each row of a response with ``PARSERS[QUERIES[query_id].row_surface]``."""

# Fail at import, not at the first sync, when a template names a surface without a parser.
if set(PARSERS) != {template.row_surface for template in QUERIES.values()}:
    raise RuntimeError("PARSERS must cover exactly the row surfaces of QUERIES")
