"""Typed rows from LinkedIn Marketing API payloads, lead-form identity and lead email hashing.

The module turns decoded Rest.li 2.0 JSON elements (``Linkedin-Version: 202609``) into frozen,
typed rows that ``ads_engine.sync`` persists, and it holds the request lists and status maps
that ``connectors/linkedin/client.py`` sends and normalises:

* ad accounts, campaign groups, campaigns and creatives, each status kept raw and normalised
  to ``active``, ``paused`` or ``ended``;
* the posts creatives reference, for headline and intro text;
* daily creative analytics and preset-window demographic segments;
* lead forms with their versions, and lead form responses with the email decoded per version.

Purity:
    The module performs no I/O, logs nothing and never reads the clock. It imports only the
    standard library and :class:`ads_engine.connectors.errors.QueryDefect`. Every malformed
    payload raises ``QueryDefect(code="validation")`` naming the contract surface (the file
    names in ``backend/tests/contracts/linkedin/``) and the field, never a value from a lead.

Days and instants:
    LinkedIn days are UTC. A daily analytics row keeps the UTC calendar date LinkedIn returns
    and is never rebucketed. Epoch-millisecond instants become timezone-aware UTC datetimes
    through integer arithmetic, never float division.

Money:
    Amounts are exact :class:`~decimal.Decimal` values in the ad account's currency and are
    never rounded here; the minor-unit check on decisions belongs to ``ads_engine.decisions``.

Personal data:
    A lead email is normalised (trimmed, lowercased) and hashed with SHA-256. The plaintext
    never leaves the return value of :func:`decode_email`: :class:`ParsedLead` has no field
    that can hold it, and no exception message, ``repr`` or row carries it. Free-text answers,
    consent responses and hidden fields are never read, returned or retained.

Absent values:
    A field LinkedIn did not return is ``None``, never a silent ``0``. Requested metrics that
    an element omits are unavailable, not zero.

Example:
    The lead job parses one page element; the HubSpot matcher re-decodes transiently::

        lead = parse_lead(element, known_versions)
        if lead is not None and lead.version_map is not None:
            persist_version(lead.version_map)  # sync stores lead_form_version
        decoded = decode_email(refetched_element, known_versions)
        if decoded.status == "ok":
            await hubspot.contacts_by_email([decoded.email])  # then drop `decoded`
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final, Literal, overload

from ads_engine.connectors.errors import QueryDefect

__all__ = [
    "CAMPAIGN_GROUP_SEARCH_STATUSES",
    "CAMPAIGN_GROUP_STATUS_MAP",
    "CAMPAIGN_SEARCH_STATUSES",
    "CAMPAIGN_STATUS_MAP",
    "CREATIVE_INTENDED_STATUSES",
    "CREATIVE_STATUS_MAP",
    "PREFIX_ACCOUNT",
    "PREFIX_CAMPAIGN",
    "PREFIX_CAMPAIGN_GROUP",
    "PREFIX_CREATIVE",
    "PREFIX_LEAD_FORM",
    "REASON_FORM_MAP_MISMATCH",
    "REASON_NO_EMAIL_ANSWER",
    "REASON_NO_EMAIL_QUESTION",
    "REASON_UNKNOWN_FORM_VERSION",
    "SEGMENT_FACETS",
    "SEGMENT_PIVOTS",
    "AdAccountRow",
    "CampaignGroupRow",
    "CampaignRow",
    "CreativeDailyRow",
    "CreativeRow",
    "DecodedEmail",
    "EmailStatus",
    "FormVersionMap",
    "JsonObject",
    "KnownVersions",
    "LeadFormRow",
    "Money",
    "NormalisedStatus",
    "ParsedLead",
    "PostRow",
    "SegmentRow",
    "canonical_form_urn",
    "decode_email",
    "form_map_from_decoration",
    "parse_ad_account",
    "parse_analytics_daily",
    "parse_campaign",
    "parse_campaign_group",
    "parse_creative",
    "parse_lead",
    "parse_lead_form",
    "parse_post",
    "parse_segment",
    "parse_versioned_form_urn",
    "sponsored_account_urn",
    "sponsored_campaign_urn",
    "urn_id",
]

# Implicit aliases rather than `X: TypeAlias = ...`: ruff's UP040 rewrites explicit TypeAlias
# annotations for the py314 target, and implicit aliases stay usable at runtime.
JsonObject = Mapping[str, object]
"""One decoded JSON object, as ``connectors/http.py`` returns it."""

NormalisedStatus = Literal["active", "paused", "ended"]
"""Status normalised through a per-entity map; the raw LinkedIn status is always kept."""

EmailStatus = Literal["ok", "no_email"]
"""Outcome of decoding a lead's email; ``no_email`` is terminal for lead matching."""

# ---------------------------------------------------------------------------
# Contract surfaces: the file names in backend/tests/contracts/linkedin/
# ---------------------------------------------------------------------------

_SURFACE_AD_ACCOUNT: Final = "ad_account"
_SURFACE_CAMPAIGN_GROUPS: Final = "ad_campaign_groups"
_SURFACE_CAMPAIGNS: Final = "ad_campaigns"
_SURFACE_CREATIVES: Final = "creatives"
_SURFACE_POSTS: Final = "posts"
_SURFACE_ANALYTICS: Final = "ad_analytics"
_SURFACE_STATISTICS: Final = "ad_analytics_statistics"
_SURFACE_LEAD_FORMS: Final = "lead_forms"
_SURFACE_LEAD_RESPONSES: Final = "lead_form_responses"
_SURFACE_LEAD_RESPONSES_BATCH: Final = "lead_form_responses_batch"
_SURFACES: Final = frozenset(
    {
        _SURFACE_AD_ACCOUNT,
        _SURFACE_CAMPAIGN_GROUPS,
        _SURFACE_CAMPAIGNS,
        _SURFACE_CREATIVES,
        _SURFACE_POSTS,
        _SURFACE_ANALYTICS,
        _SURFACE_STATISTICS,
        _SURFACE_LEAD_FORMS,
        _SURFACE_LEAD_RESPONSES,
        _SURFACE_LEAD_RESPONSES_BATCH,
    }
)

# ---------------------------------------------------------------------------
# URN prefixes
# ---------------------------------------------------------------------------

PREFIX_ACCOUNT: Final = "urn:li:sponsoredAccount:"
"""URN prefix of a LinkedIn ad account."""
PREFIX_CAMPAIGN_GROUP: Final = "urn:li:sponsoredCampaignGroup:"
"""URN prefix of a LinkedIn campaign group."""
PREFIX_CAMPAIGN: Final = "urn:li:sponsoredCampaign:"
"""URN prefix of a LinkedIn campaign."""
PREFIX_CREATIVE: Final = "urn:li:sponsoredCreative:"
"""URN prefix of a LinkedIn creative."""
PREFIX_LEAD_FORM: Final = "urn:li:leadGenForm:"
"""URN prefix of a canonical (version-free) lead generation form."""

# The Creatives API documents leadgenCallToAction.destination as an AdFormUrn; it names the
# same form id as urn:li:leadGenForm:{id}.
_PREFIX_AD_FORM: Final = "urn:li:adForm:"
_POST_PREFIXES: Final = ("urn:li:share:", "urn:li:ugcPost:")

_DIGITS_RE: Final = re.compile(r"[0-9]+")
_ENUM_TOKEN_RE: Final = re.compile(r"[A-Z][A-Z_]{0,39}")
_CURRENCY_RE: Final = re.compile(r"[A-Z]{3}")
# BigDecimal strings as Rest.li serialises them: plain or scientific, never a JSON float.
_DECIMAL_RE: Final = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
# Rest.li ids, counts, epoch milliseconds and date parts are 64-bit longs. Numerals are bounded
# before conversion: int() refuses a string of more than 4,300 digits, and Decimal() an exponent
# beyond the decimal module's limits.
_INT64_MAX: Final = 2**63 - 1
_MAX_INT_DIGITS: Final = len(str(_INT64_MAX))  # 19
# The longest BigDecimal string accepted, far above any amount LinkedIn returns.
_MAX_DECIMAL_CHARS: Final = 64
_VERSIONED_FORM_RE: Final = re.compile(
    r"urn:li:versionedLeadGenForm:\(urn:li:leadGenForm:([0-9]+),([0-9]+)\)"
)

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Request lists and status maps (LinkedIn 202609 references, verified October 2026)
# ---------------------------------------------------------------------------

CAMPAIGN_SEARCH_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "ARCHIVED",
    "COMPLETED",
    "CANCELED",
    "DRAFT",
    "PENDING_DELETION",
    "REMOVED",
)
"""Every campaign status, sent as ``search=(status:(values:List(...)))`` in this order.

The campaign reference documents ``COMPLETED`` (a budgetary or chronological limit reached)
beside the campaign-group statuses. Requesting it keeps completed campaigns in the tracked set,
so the discrepancy check never reports them as missing.
"""

CAMPAIGN_GROUP_SEARCH_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "ARCHIVED",
    "CANCELED",
    "DRAFT",
    "PENDING_DELETION",
    "REMOVED",
)
"""The seven documented campaign-group search statuses, in request order.

``COMPLETED`` is not a documented group search value, and an undocumented enum value risks
HTTP 400.
"""

CREATIVE_INTENDED_STATUSES: Final = (
    "ACTIVE",
    "PAUSED",
    "DRAFT",
    "ARCHIVED",
    "CANCELED",
    "PENDING_DELETION",
    "REMOVED",
)
"""Every creative intended status, sent as ``intendedStatuses=List(...)`` in this order.

LinkedIn hides ``CANCELED`` creatives from a campaign query unless they are requested, and
entity state is synced in all statuses.
"""

_ENDED: Final[NormalisedStatus] = "ended"

CAMPAIGN_STATUS_MAP: Final[Mapping[str, NormalisedStatus]] = MappingProxyType(
    {
        "ACTIVE": "active",
        "PAUSED": "paused",
        "DRAFT": "paused",
        "ARCHIVED": _ENDED,
        "COMPLETED": _ENDED,
        "CANCELED": _ENDED,
        "PENDING_DELETION": _ENDED,
        "REMOVED": _ENDED,
    }
)
"""Raw campaign ``status`` to the normalised status stored on ``campaign.status``."""

CAMPAIGN_GROUP_STATUS_MAP: Final[Mapping[str, NormalisedStatus]] = MappingProxyType(
    {
        "ACTIVE": "active",
        "PAUSED": "paused",
        "DRAFT": "paused",
        "ARCHIVED": _ENDED,
        "CANCELED": _ENDED,
        # CANCELLED is the group schema table's spelling of the cancel status that search
        # spells CANCELED, a documented alias. COMPLETED is not a documented campaign-group
        # status (it is documented for campaigns); it is tolerated defensively, as the
        # ad_campaign_groups contract does, so a real response is never rejected. Neither
        # is sent as a search value, and both mean the group has ended.
        "CANCELLED": _ENDED,
        "COMPLETED": _ENDED,
        "PENDING_DELETION": _ENDED,
        "REMOVED": _ENDED,
    }
)
"""Raw campaign-group ``status`` to the normalised status stored on ``campaign_group.status``."""

CREATIVE_STATUS_MAP: Final[Mapping[str, NormalisedStatus]] = MappingProxyType(
    {
        "ACTIVE": "active",
        "PAUSED": "paused",
        "DRAFT": "paused",
        "ARCHIVED": _ENDED,
        "CANCELED": _ENDED,
        "PENDING_DELETION": _ENDED,
        "REMOVED": _ENDED,
    }
)
"""Raw creative ``intendedStatus`` to the normalised creative status."""

SEGMENT_PIVOTS: Final = ("MEMBER_JOB_FUNCTION", "MEMBER_SENIORITY", "MEMBER_INDUSTRY")
"""The professional-demographic pivots fetched per preset window for segment-gap ideas."""

SEGMENT_FACETS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "MEMBER_JOB_FUNCTION": "job_function",
        "MEMBER_SENIORITY": "seniority",
        "MEMBER_INDUSTRY": "industry",
    }
)
"""Demographic pivot to the ``fact_segment_window.facet`` value."""

# The value URN each demographic pivot returns (Targeting Criteria Facet URNs reference).
_SEGMENT_VALUE_PREFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "MEMBER_JOB_FUNCTION": "urn:li:function:",
        "MEMBER_SENIORITY": "urn:li:seniority:",
        "MEMBER_INDUSTRY": "urn:li:industry:",
    }
)

# ---------------------------------------------------------------------------
# Lead email outcomes
# ---------------------------------------------------------------------------

REASON_FORM_MAP_MISMATCH: Final = "form map mismatch"
"""An answered question id is absent from the questions of the response's own form version."""
REASON_UNKNOWN_FORM_VERSION: Final = "unknown form version"
"""The response carries no form decoration and no stored map exists for its version."""
REASON_NO_EMAIL_QUESTION: Final = "form has no email question"
"""The form version asks no ``EMAIL`` or ``WORK_EMAIL`` question."""
REASON_NO_EMAIL_ANSWER: Final = "no email answer"
"""No email question carries a usable address."""

# Email questions in order of preference. WORK_EMAIL is a distinct documented predefinedField,
# and forms that ask only for a work email must still be matched to HubSpot contacts.
_EMAIL_FIELDS: Final = ("EMAIL", "WORK_EMAIL")

# ---------------------------------------------------------------------------
# Defects and JSON narrowing
# ---------------------------------------------------------------------------


def _defect(message: str, surface: str) -> QueryDefect:
    """Build the validation defect for a payload that breaks its contract.

    Args:
        message: What is wrong, naming the field only; never a lead's value or a token.
        surface: The contract surface the payload came from.

    Returns:
        A ``QueryDefect`` with ``code="validation"`` for the ``linkedin`` source.
    """
    return QueryDefect(
        f"{surface}: {message}", source="linkedin", endpoint=surface, code="validation"
    )


def _check_surface(surface: str) -> str:
    """Return ``surface`` when it names a LinkedIn contract surface, else raise ValueError."""
    if surface not in _SURFACES:
        raise ValueError(f"unknown LinkedIn contract surface {surface!r}")
    return surface


def _as_obj(value: object, what: str, surface: str) -> JsonObject:
    """Narrow a decoded JSON value to an object, or raise a defect naming ``what``."""
    if not isinstance(value, Mapping):
        raise _defect(f"{what} must be a JSON object", surface)
    return value


def _present(parent: JsonObject, key: str, surface: str, *, required: bool) -> object | None:
    """Return ``parent[key]``; an absent key or JSON null is ``None`` unless required."""
    value = parent.get(key)
    if value is None and required:
        raise _defect(f"required field '{key}' is missing", surface)
    return value


@overload
def _obj(parent: JsonObject, key: str, surface: str, *, required: Literal[True]) -> JsonObject: ...
@overload
def _obj(
    parent: JsonObject, key: str, surface: str, *, required: Literal[False]
) -> JsonObject | None: ...
def _obj(parent: JsonObject, key: str, surface: str, *, required: bool) -> JsonObject | None:
    """Read an object field."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    return _as_obj(value, f"field '{key}'", surface)


@overload
def _list(
    parent: JsonObject, key: str, surface: str, *, required: Literal[True]
) -> Sequence[object]: ...
@overload
def _list(
    parent: JsonObject, key: str, surface: str, *, required: Literal[False]
) -> Sequence[object] | None: ...
def _list(parent: JsonObject, key: str, surface: str, *, required: bool) -> Sequence[object] | None:
    """Read an array field."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise _defect(f"field '{key}' must be a JSON array", surface)
    return value


@overload
def _str(parent: JsonObject, key: str, surface: str, *, required: Literal[True]) -> str: ...
@overload
def _str(parent: JsonObject, key: str, surface: str, *, required: Literal[False]) -> str | None: ...
def _str(parent: JsonObject, key: str, surface: str, *, required: bool) -> str | None:
    """Read a string field."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _defect(f"field '{key}' must be a string", surface)
    return value


def _to_int(value: object, what: str, surface: str) -> int:
    """Convert an int, an integral Decimal or a digit string to a non-negative 64-bit int.

    ``bool`` is rejected although it subclasses ``int``. ``connectors/http.py`` decodes JSON
    floats as ``Decimal``, so an integral ``Decimal`` such as ``Decimal("5")`` is accepted.
    Rest.li sends ids, counts and epoch milliseconds as longs, so a value above ``2**63 - 1``
    is a defect. The bound is checked before conversion: a digit string over 19 characters or
    a ``Decimal`` such as ``Decimal("1E+999999999")`` never reaches ``int()``.
    """
    if isinstance(value, bool):
        raise _defect(f"{what} must be an integer, not a boolean", surface)
    if isinstance(value, int):
        number = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise _defect(f"{what} must be an integer", surface)
        if value.copy_abs() > _INT64_MAX:
            raise _defect(f"{what} is out of the 64-bit integer range", surface)
        number = int(value)
    elif isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        if len(value) > _MAX_INT_DIGITS:
            raise _defect(f"{what} is out of the 64-bit integer range", surface)
        number = int(value)
    else:
        raise _defect(f"{what} must be an integer", surface)
    if number < 0:
        raise _defect(f"{what} must not be negative", surface)
    if number > _INT64_MAX:
        raise _defect(f"{what} is out of the 64-bit integer range", surface)
    return number


@overload
def _int(parent: JsonObject, key: str, surface: str, *, required: Literal[True]) -> int: ...
@overload
def _int(parent: JsonObject, key: str, surface: str, *, required: Literal[False]) -> int | None: ...
def _int(parent: JsonObject, key: str, surface: str, *, required: bool) -> int | None:
    """Read a non-negative integer field (ids, counts, epoch milliseconds, dates)."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    return _to_int(value, f"field '{key}'", surface)


@overload
def _bool(parent: JsonObject, key: str, surface: str, *, required: Literal[True]) -> bool: ...
@overload
def _bool(
    parent: JsonObject, key: str, surface: str, *, required: Literal[False]
) -> bool | None: ...
def _bool(parent: JsonObject, key: str, surface: str, *, required: bool) -> bool | None:
    """Read a boolean field."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _defect(f"field '{key}' must be a boolean", surface)
    return value


def _to_decimal(value: object, what: str, surface: str) -> Decimal:
    """Convert a BigDecimal string, an int or a Decimal to a finite, non-negative Decimal.

    Floats are rejected: a binary float cannot carry an exact money amount, and LinkedIn
    serialises BigDecimal values as strings. A string longer than 64 characters is a defect
    before it reaches ``Decimal()``, as is one whose exponent the decimal module cannot hold.
    An amount of ``10**19`` or more, in any input form, is out of range: the same 19-digit
    bound as a 64-bit integer. A zero such as ``0E+19`` or ``-0E+19`` is below that bound
    whatever its exponent, and is returned unsigned.
    """
    if isinstance(value, bool):
        raise _defect(f"{what} must be a decimal, not a boolean", surface)
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        if len(value) > _MAX_DECIMAL_CHARS:
            raise _defect(f"{what} is longer than {_MAX_DECIMAL_CHARS} characters", surface)
        # The pattern admits only plain or scientific numerals; Decimal() refuses only an
        # exponent beyond its limits, such as 1e9999999999999999999.
        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise _defect(f"{what} is out of range", surface) from exc
    else:
        raise _defect(f"{what} must be a decimal string", surface)
    if not amount.is_finite():
        raise _defect(f"{what} must be finite", surface)
    if amount < 0:
        raise _defect(f"{what} must not be negative", surface)
    # adjusted() is the exponent of the leading digit, so for a nonzero amount it is 19 or
    # more exactly when |amount| >= 10**19. Zero is exempt whatever its exponent: 0E+19 has
    # adjusted() 19, but its value is below the bound.
    if not amount.is_zero() and amount.adjusted() >= _MAX_INT_DIGITS:
        raise _defect(f"{what} is out of range", surface)
    # "-0.0" compares equal to zero; store it unsigned.
    return amount.copy_abs() if amount.is_zero() else amount


@overload
def _decimal(parent: JsonObject, key: str, surface: str, *, required: Literal[True]) -> Decimal: ...
@overload
def _decimal(
    parent: JsonObject, key: str, surface: str, *, required: Literal[False]
) -> Decimal | None: ...
def _decimal(parent: JsonObject, key: str, surface: str, *, required: bool) -> Decimal | None:
    """Read a non-negative decimal field (spend, budget amounts)."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    return _to_decimal(value, f"field '{key}'", surface)


def _epoch_ms_to_utc(value: int, what: str, surface: str) -> datetime:
    """Convert epoch milliseconds to a timezone-aware UTC datetime with integer arithmetic."""
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError as exc:
        raise _defect(f"{what} is outside the representable date range", surface) from exc


@overload
def _instant(
    parent: JsonObject, key: str, surface: str, *, required: Literal[True]
) -> datetime: ...
@overload
def _instant(
    parent: JsonObject, key: str, surface: str, *, required: Literal[False]
) -> datetime | None: ...
def _instant(parent: JsonObject, key: str, surface: str, *, required: bool) -> datetime | None:
    """Read an epoch-milliseconds field as a UTC instant."""
    value = _present(parent, key, surface, required=required)
    if value is None:
        return None
    return _epoch_ms_to_utc(_to_int(value, f"field '{key}'", surface), f"field '{key}'", surface)


def _calendar_date(parent: JsonObject, key: str, surface: str) -> date:
    """Read a required Rest.li ``{year, month, day}`` date as a calendar date.

    Each part is a 64-bit long. A date ``datetime.date`` cannot represent is a defect, whether
    a part is out of its calendar range (``ValueError``) or beyond a C int (``OverflowError``).
    """
    obj = _obj(parent, key, surface, required=True)
    year = _int(obj, "year", surface, required=True)
    month = _int(obj, "month", surface, required=True)
    day = _int(obj, "day", surface, required=True)
    try:
        return date(year, month, day)
    except (ValueError, OverflowError) as exc:
        raise _defect(f"field '{key}' is not a valid calendar date", surface) from exc


def _enum_hint(value: str) -> str:
    """Quote ``value`` for a defect message only when it is an enum token, never free text."""
    return f" {value!r}" if _ENUM_TOKEN_RE.fullmatch(value) else ""


def _normalise_status(
    raw: str, mapping: Mapping[str, NormalisedStatus], key: str, surface: str
) -> NormalisedStatus:
    """Map a raw LinkedIn status, raising a defect for an unmapped value."""
    status = mapping.get(raw)
    if status is None:
        raise _defect(f"unmapped status{_enum_hint(raw)} in field '{key}'", surface)
    return status


# ---------------------------------------------------------------------------
# URN helpers
# ---------------------------------------------------------------------------


def _urn_for(value: object, prefix: str, what: str) -> str:
    """Build ``{prefix}{id}`` from a numeric id, a digit string or an existing URN.

    Only a non-negative ``int`` (never a ``bool``) or a ``str`` is accepted; a float, a
    ``Decimal``, ``bytes``, ``None`` or any other type raises ``ValueError``.
    """
    if isinstance(value, str):
        tail = value.removeprefix(prefix)
        if _DIGITS_RE.fullmatch(tail):
            return prefix + tail
    elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return f"{prefix}{value}"
    raise ValueError(f"{what} must be a numeric id or a {prefix}{{id}} URN")


def sponsored_account_urn(account_id: str | int) -> str:
    """Return the ad account URN ``urn:li:sponsoredAccount:{id}``.

    Args:
        account_id: A numeric id, its digit string, or an existing sponsoredAccount URN.

    Returns:
        The canonical account URN.

    Raises:
        ValueError: When ``account_id`` is none of the accepted forms.

    Example::

        >>> sponsored_account_urn(123)
        'urn:li:sponsoredAccount:123'
    """
    return _urn_for(account_id, PREFIX_ACCOUNT, "account_id")


def sponsored_campaign_urn(campaign_id: str | int) -> str:
    """Return the campaign URN ``urn:li:sponsoredCampaign:{id}``.

    Args:
        campaign_id: A numeric id, its digit string, or an existing sponsoredCampaign URN.

    Returns:
        The canonical campaign URN.

    Raises:
        ValueError: When ``campaign_id`` is none of the accepted forms.
    """
    return _urn_for(campaign_id, PREFIX_CAMPAIGN, "campaign_id")


def urn_id(urn: str, prefix: str) -> str:
    """Return the numeric id that follows ``prefix`` in ``urn``.

    Args:
        urn: A URN such as ``urn:li:sponsoredAccount:123``.
        prefix: The expected prefix, for example :data:`PREFIX_ACCOUNT`.

    Returns:
        The trailing id as a digit string, for example ``"123"``.

    Raises:
        ValueError: When the prefix does not match or the id is not all digits.
    """
    if not prefix or not urn.startswith(prefix):
        raise ValueError(f"URN does not start with {prefix!r}")
    tail = urn[len(prefix) :]
    if not _DIGITS_RE.fullmatch(tail):
        raise ValueError(f"URN id after {prefix!r} is not numeric")
    return tail


def _is_urn(value: str, prefix: str) -> bool:
    """Whether ``value`` is ``{prefix}{digits}``."""
    return value.startswith(prefix) and _DIGITS_RE.fullmatch(value[len(prefix) :]) is not None


def _checked_urn(value: str, prefix: str, what: str, surface: str) -> str:
    """Return ``value`` when it is ``{prefix}{digits}``, else raise a defect naming ``what``."""
    if not _is_urn(value, prefix):
        raise _defect(f"{what} must be a {prefix}{{id}} URN", surface)
    return value


@overload
def _urn(
    parent: JsonObject, key: str, prefix: str, surface: str, *, required: Literal[True]
) -> str: ...
@overload
def _urn(
    parent: JsonObject, key: str, prefix: str, surface: str, *, required: Literal[False]
) -> str | None: ...
def _urn(parent: JsonObject, key: str, prefix: str, surface: str, *, required: bool) -> str | None:
    """Read a URN field and check its prefix and numeric id."""
    value = (
        _str(parent, key, surface, required=True)
        if required
        else _str(parent, key, surface, required=False)
    )
    if value is None:
        return None
    return _checked_urn(value, prefix, f"field '{key}'", surface)


# ---------------------------------------------------------------------------
# Entity rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in one currency, as LinkedIn budget objects carry it.

    Attributes:
        amount: The amount, never rounded; the minor-unit check belongs to decisions.
        currency: ISO 4217 code, which equals the ad account's currency.
    """

    amount: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class AdAccountRow:
    """One ad account from ``GET adAccounts/{id}``: the probe and the currency source.

    LinkedIn days are UTC by definition, so the row carries no time zone; sync sets the
    ``ad_account.timezone`` column.

    Attributes:
        external_id: The numeric account id as a string.
        urn: ``urn:li:sponsoredAccount:{id}``.
        name: The account label.
        currency: ISO 4217 code of every amount under the account.
        raw_status: The raw account status, or ``None`` when absent.
        test: Whether the account is a test account (documented default ``false``).
    """

    external_id: str
    urn: str
    name: str
    currency: str
    raw_status: str | None
    test: bool


@dataclass(frozen=True, slots=True)
class CampaignGroupRow:
    """One campaign group, stored in ``campaign_group`` and snapshotted every entity sync.

    Attributes:
        external_id: The numeric group id as a string.
        urn: ``urn:li:sponsoredCampaignGroup:{id}``.
        account_urn: The owning ad account URN.
        name: The group name.
        status: The normalised status.
        raw_status: The raw LinkedIn status.
        daily_budget: The daily budget of a dynamic group, or ``None``.
        total_budget: The lifetime budget, or ``None``.
        run_start: Inclusive start of the run schedule, or ``None``.
        run_end: Exclusive end of the run schedule, or ``None`` for an open-ended group.
    """

    external_id: str
    urn: str
    account_urn: str
    name: str
    status: NormalisedStatus
    raw_status: str
    daily_budget: Money | None
    total_budget: Money | None
    run_start: datetime | None
    run_end: datetime | None


@dataclass(frozen=True, slots=True)
class CampaignRow:
    """One campaign, stored in ``campaign`` and snapshotted every entity sync.

    A campaign may carry a daily budget, a lifetime budget or both. Each is ``None`` when
    LinkedIn did not return it, because the ``own_budget`` decision shape and the discrepancy
    check use exactly the budget fields the campaign has.

    Attributes:
        external_id: The numeric campaign id as a string.
        urn: ``urn:li:sponsoredCampaign:{id}``.
        account_urn: The owning ad account URN.
        name: The campaign name.
        status: The normalised status.
        raw_status: The raw LinkedIn status.
        campaign_group_urn: The parent campaign group URN, or ``None``.
        daily_budget: Maximum spend per UTC day, or ``None``.
        total_budget: Maximum spend over the campaign's life, or ``None``.
        objective_type: The raw objective, such as ``LEAD_GENERATION`` (which selects
            ``oneClickLeads`` as the outcome), or ``None`` when absent.
        run_start: Inclusive start of the run schedule, or ``None``.
        run_end: Exclusive end of the run schedule, or ``None``.
    """

    external_id: str
    urn: str
    account_urn: str
    name: str
    status: NormalisedStatus
    raw_status: str
    campaign_group_urn: str | None
    daily_budget: Money | None
    total_budget: Money | None
    objective_type: str | None
    run_start: datetime | None
    run_end: datetime | None


@dataclass(frozen=True, slots=True)
class CreativeRow:
    """One creative, stored in ``creative``.

    Attributes:
        urn: ``urn:li:sponsoredCreative:{id}``, the key of spend and lead attribution.
        campaign_urn: The campaign the creative runs in.
        account_urn: The owning ad account URN, or ``None`` when absent.
        name: The advertiser-set name, or ``None`` when none was set.
        intended_status: The raw ``intendedStatus``.
        status: The normalised intended status.
        is_serving: Whether the creative is serving, or ``None`` when absent.
        post_urn: The referenced share or ugcPost URN, or ``None`` for content without a
            post (inline, text, spotlight or message ads).
        lead_form_urn: The canonical ``urn:li:leadGenForm:{id}`` of the lead-gen call to
            action, or ``None`` for a creative without one.
        cta_label: The raw call-to-action label, such as ``DOWNLOAD``, or ``None``.
    """

    urn: str
    campaign_urn: str
    account_urn: str | None
    name: str | None
    intended_status: str
    status: NormalisedStatus
    is_serving: bool | None
    post_urn: str | None
    lead_form_urn: str | None
    cta_label: str | None


@dataclass(frozen=True, slots=True)
class PostRow:
    """Headline and intro text of the post a creative references (advertiser ad copy).

    Attributes:
        urn: The post URN, the batch-result key.
        headline: ``content.media.title``, else ``content.article.title``, else ``None``.
        intro_text: ``commentary``, or ``None`` when absent or blank.
    """

    urn: str
    headline: str | None
    intro_text: str | None


def _account_currency(value: str | None) -> str | None:
    """Validate an ad account currency passed in by the caller."""
    if value is not None and not _CURRENCY_RE.fullmatch(value):
        raise ValueError("account_currency must be a three-letter upper-case ISO 4217 code")
    return value


def _money(
    parent: JsonObject,
    key: str,
    surface: str,
    *,
    account_currency: str | None,
    allow_bare: bool,
) -> Money | None:
    """Read a budget: a ``{amount, currencyCode}`` object, or a bare amount when allowed.

    A bare amount (the campaign-group ``dailyBudget``) is in the ad account's currency, so it
    needs ``account_currency``. When ``account_currency`` is given, an object whose
    ``currencyCode`` differs is a defect: amounts are never converted between currencies.
    """
    value = _present(parent, key, surface, required=False)
    if value is None:
        return None
    if isinstance(value, Mapping):
        amount = _decimal(value, "amount", surface, required=True)
        currency = _str(value, "currencyCode", surface, required=True)
        if not _CURRENCY_RE.fullmatch(currency):
            raise _defect(f"field '{key}.currencyCode' must be an ISO 4217 code", surface)
        if account_currency is not None and currency != account_currency:
            raise _defect(f"field '{key}' is not in the ad account currency", surface)
        return Money(amount=amount, currency=currency)
    if not allow_bare:
        raise _defect(f"field '{key}' must be an amount and currency object", surface)
    if account_currency is None:
        raise _defect(
            f"field '{key}' is a bare amount; the ad account currency must be supplied", surface
        )
    return Money(amount=_to_decimal(value, f"field '{key}'", surface), currency=account_currency)


def _run_schedule(element: JsonObject, surface: str) -> tuple[datetime | None, datetime | None]:
    """Read ``runSchedule.start`` (required when the schedule exists) and ``.end``."""
    schedule = _obj(element, "runSchedule", surface, required=False)
    if schedule is None:
        return None, None
    start = _instant(schedule, "start", surface, required=True)
    end = _instant(schedule, "end", surface, required=False)
    return start, end


def parse_ad_account(element: object) -> AdAccountRow:
    """Parse the ``GET adAccounts/{id}`` response (contract ``ad_account``).

    Args:
        element: The decoded response body.

    Returns:
        The account row with its currency.

    Raises:
        QueryDefect: When a required field is missing or mistyped (``code="validation"``).
    """
    surface = _SURFACE_AD_ACCOUNT
    obj = _as_obj(element, "the ad account", surface)
    external_id = str(_int(obj, "id", surface, required=True))
    currency = _str(obj, "currency", surface, required=True)
    if not _CURRENCY_RE.fullmatch(currency):
        raise _defect("field 'currency' must be an ISO 4217 code", surface)
    # `test` is optional with a documented default of false (contract ad_account).
    test = _bool(obj, "test", surface, required=False)
    return AdAccountRow(
        external_id=external_id,
        urn=PREFIX_ACCOUNT + external_id,
        name=_str(obj, "name", surface, required=True),
        currency=currency,
        raw_status=_str(obj, "status", surface, required=False),
        test=bool(test),
    )


def parse_campaign_group(
    element: object, *, account_currency: str | None = None
) -> CampaignGroupRow:
    """Parse one ``adCampaignGroups?q=search`` element (contract ``ad_campaign_groups``).

    ``totalBudget`` is an ``{amount, currencyCode}`` object, while ``dailyBudget`` (dynamic
    groups only) is a bare BigDecimal string in the ad account's currency; parsing a group
    that carries one therefore needs ``account_currency``.

    Args:
        element: One element of the search response.
        account_currency: The ad account's currency, from :func:`parse_ad_account`. When
            given, a budget in another currency is a defect.

    Returns:
        The campaign-group row.

    Raises:
        QueryDefect: When the element breaks its contract, its status is unmapped, or a bare
            daily budget arrives without ``account_currency``.
        ValueError: When ``account_currency`` is not an ISO 4217 code.
    """
    surface = _SURFACE_CAMPAIGN_GROUPS
    currency = _account_currency(account_currency)
    obj = _as_obj(element, "a campaign group", surface)
    external_id = str(_int(obj, "id", surface, required=True))
    raw_status = _str(obj, "status", surface, required=True)
    run_start, run_end = _run_schedule(obj, surface)
    return CampaignGroupRow(
        external_id=external_id,
        urn=PREFIX_CAMPAIGN_GROUP + external_id,
        account_urn=_urn(obj, "account", PREFIX_ACCOUNT, surface, required=True),
        name=_str(obj, "name", surface, required=True),
        status=_normalise_status(raw_status, CAMPAIGN_GROUP_STATUS_MAP, "status", surface),
        raw_status=raw_status,
        daily_budget=_money(
            obj, "dailyBudget", surface, account_currency=currency, allow_bare=True
        ),
        total_budget=_money(
            obj, "totalBudget", surface, account_currency=currency, allow_bare=False
        ),
        run_start=run_start,
        run_end=run_end,
    )


def parse_campaign(element: object, *, account_currency: str | None = None) -> CampaignRow:
    """Parse one ``adCampaigns?q=search`` element (contract ``ad_campaigns``).

    Args:
        element: One element of the search response.
        account_currency: The ad account's currency. When given, a budget in another
            currency is a defect.

    Returns:
        The campaign row. A budget LinkedIn did not return is ``None``, never invented.

    Raises:
        QueryDefect: When the element breaks its contract or its status is unmapped.
        ValueError: When ``account_currency`` is not an ISO 4217 code.
    """
    surface = _SURFACE_CAMPAIGNS
    currency = _account_currency(account_currency)
    obj = _as_obj(element, "a campaign", surface)
    external_id = str(_int(obj, "id", surface, required=True))
    raw_status = _str(obj, "status", surface, required=True)
    run_start, run_end = _run_schedule(obj, surface)
    return CampaignRow(
        external_id=external_id,
        urn=PREFIX_CAMPAIGN + external_id,
        account_urn=_urn(obj, "account", PREFIX_ACCOUNT, surface, required=True),
        name=_str(obj, "name", surface, required=True),
        status=_normalise_status(raw_status, CAMPAIGN_STATUS_MAP, "status", surface),
        raw_status=raw_status,
        campaign_group_urn=_urn(
            obj, "campaignGroup", PREFIX_CAMPAIGN_GROUP, surface, required=False
        ),
        daily_budget=_money(
            obj, "dailyBudget", surface, account_currency=currency, allow_bare=False
        ),
        total_budget=_money(
            obj, "totalBudget", surface, account_currency=currency, allow_bare=False
        ),
        objective_type=_str(obj, "objectiveType", surface, required=False),
        run_start=run_start,
        run_end=run_end,
    )


def _creative_urn(obj: JsonObject, surface: str) -> str:
    """Read a creative ``id``: a URN in every documented response, or a bare numeric id."""
    value = _present(obj, "id", surface, required=True)
    if isinstance(value, str) and value.startswith(PREFIX_CREATIVE):
        return _checked_urn(value, PREFIX_CREATIVE, "field 'id'", surface)
    return PREFIX_CREATIVE + str(_to_int(value, "field 'id'", surface))


def parse_creative(element: object) -> CreativeRow:
    """Parse one ``creatives?q=criteria`` element (contract ``creatives``).

    The lead-gen call-to-action destination, documented as ``urn:li:adForm:{id}``, is stored
    as the canonical ``urn:li:leadGenForm:{id}``, so every version of the form joins the same
    content asset. Headline and intro text come from the referenced post
    (:func:`parse_post`). A ``content.reference`` with a share or ugcPost prefix must carry a
    numeric id, because it becomes a posts batch key; any other reference, such as message-ad
    content, leaves ``post_urn`` ``None``.

    Args:
        element: One element of the criteria search response.

    Returns:
        The creative row.

    Raises:
        QueryDefect: When the element breaks its contract, its post reference has a
            malformed id, or its intended status is unmapped.
    """
    surface = _SURFACE_CREATIVES
    obj = _as_obj(element, "a creative", surface)
    intended_status = _str(obj, "intendedStatus", surface, required=True)
    post_urn: str | None = None
    content = _obj(obj, "content", surface, required=False)
    if content is not None:
        reference = _str(content, "reference", surface, required=False)
        if reference is not None:
            for prefix in _POST_PREFIXES:
                if reference.startswith(prefix):
                    post_urn = _checked_urn(reference, prefix, "field 'content.reference'", surface)
                    break
    lead_form_urn: str | None = None
    cta_label: str | None = None
    cta = _obj(obj, "leadgenCallToAction", surface, required=False)
    if cta is not None:
        destination = _str(cta, "destination", surface, required=True)
        lead_form_urn = canonical_form_urn(destination, surface=surface)
        cta_label = _str(cta, "label", surface, required=True)
    return CreativeRow(
        urn=_creative_urn(obj, surface),
        campaign_urn=_urn(obj, "campaign", PREFIX_CAMPAIGN, surface, required=True),
        account_urn=_urn(obj, "account", PREFIX_ACCOUNT, surface, required=False),
        name=_str(obj, "name", surface, required=False),
        intended_status=intended_status,
        status=_normalise_status(intended_status, CREATIVE_STATUS_MAP, "intendedStatus", surface),
        is_serving=_bool(obj, "isServing", surface, required=False),
        post_urn=post_urn,
        lead_form_urn=lead_form_urn,
        cta_label=cta_label,
    )


def _clean_text(value: str | None) -> str | None:
    """Strip surrounding whitespace; an empty result is ``None``."""
    if value is None:
        return None
    text = value.strip()
    return text or None


def parse_post(urn: object, element: object) -> PostRow:
    """Parse one value of the ``posts?ids=List(...)`` batch-result map (contract ``posts``).

    ``content`` is a union with at most one member set, so the headline is
    ``content.media.title``, else ``content.article.title``, else ``None``; the order follows
    the posts contract and is immaterial for a valid post.

    Args:
        urn: The batch-result key exactly as LinkedIn returned it, already unquoted by the
            client. It must be a ``urn:li:share:{id}`` or ``urn:li:ugcPost:{id}`` URN with a
            numeric id.
        element: The post object.

    Returns:
        The post row; blank texts are ``None``.

    Raises:
        QueryDefect: When the batch-result key is not a share or ugcPost URN, or the post
            breaks its contract.
    """
    surface = _SURFACE_POSTS
    if not isinstance(urn, str) or not any(_is_urn(urn, prefix) for prefix in _POST_PREFIXES):
        raise _defect(
            "the batch-result key must be a urn:li:share:{id} or urn:li:ugcPost:{id} URN", surface
        )
    obj = _as_obj(element, "a post", surface)
    headline: str | None = None
    content = _obj(obj, "content", surface, required=False)
    if content is not None:
        for member in ("media", "article"):
            node = _obj(content, member, surface, required=False)
            if node is not None:
                headline = _clean_text(_str(node, "title", surface, required=False))
                if headline is not None:
                    break
    return PostRow(
        urn=urn,
        headline=headline,
        intro_text=_clean_text(_str(obj, "commentary", surface, required=False)),
    )


# ---------------------------------------------------------------------------
# Analytics rows (LinkedIn days are UTC)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreativeDailyRow:
    """One creative on one UTC day, from ``adAnalytics?q=analytics&pivot=CREATIVE``.

    Every metric is ``None`` when the element omits it: the ``ad_analytics`` contract records
    that live elements may omit metrics, and an absent metric is unavailable, never zero.

    Attributes:
        creative_urn: The creative URN (``pivotValues``).
        date: The UTC calendar day as returned, never rebucketed.
        cost: ``costInLocalCurrency`` in the ad account's currency.
        impressions: ``impressions``.
        clicks: ``clicks``.
        landing_page_clicks: ``landingPageClicks``.
        form_opens: ``oneClickLeadFormOpens``.
        leads: ``oneClickLeads``.
        website_conversions: ``externalWebsiteConversions``.
    """

    creative_urn: str
    date: date
    cost: Decimal | None
    impressions: int | None
    clicks: int | None
    landing_page_clicks: int | None
    form_opens: int | None
    leads: int | None
    website_conversions: int | None


@dataclass(frozen=True, slots=True)
class SegmentRow:
    """One campaign and one demographic value over a requested preset window.

    The window is not in the element; the client and sync attach the window they requested.
    LinkedIn approximates and privacy-thresholds these values, so they are stored per exact
    window and never summed from daily rows.

    Attributes:
        campaign_urn: The campaign URN.
        facet: ``job_function``, ``seniority`` or ``industry`` (:data:`SEGMENT_FACETS`).
        value: The demographic value URN, such as ``urn:li:seniority:5``.
        impressions: ``impressions``, or ``None`` when unavailable.
        clicks: ``clicks``, or ``None`` when unavailable.
        cost: ``costInLocalCurrency``, or ``None`` when unavailable.
        leads: ``oneClickLeads``, or ``None`` when the pivot does not return it.
        website_conversions: ``externalWebsiteConversions``, or ``None``.
    """

    campaign_urn: str
    facet: str
    value: str
    impressions: int | None
    clicks: int | None
    cost: Decimal | None
    leads: int | None
    website_conversions: int | None


def _pivot_values(obj: JsonObject, surface: str) -> list[str]:
    """Read ``pivotValues`` as a list of strings."""
    values: list[str] = []
    for item in _list(obj, "pivotValues", surface, required=True):
        if not isinstance(item, str):
            raise _defect("field 'pivotValues' must hold strings", surface)
        values.append(item)
    return values


def parse_analytics_daily(element: object) -> CreativeDailyRow:
    """Parse one daily ``pivot=CREATIVE`` analytics element (contract ``ad_analytics``).

    Only creative-pivot data points are accepted: ``pivotValues`` must hold exactly one
    ``urn:li:sponsoredCreative:{id}``, because the row keys ``fact_creative_daily``. The
    contract's documented sample pivots by ``MEMBER_COMPANY`` (``urn:li:organization:…``),
    which is therefore rejected; tests check metric and date decoding on that sample with its
    pivot value replaced by a creative URN.

    Args:
        element: One element of the analytics response.

    Returns:
        The daily creative row, dated by ``dateRange.start`` (a UTC day).

    Raises:
        QueryDefect: When the pivot is not one creative URN, ``dateRange.end`` differs from
            its start, or a field breaks its contract.
    """
    surface = _SURFACE_ANALYTICS
    obj = _as_obj(element, "an analytics element", surface)
    pivots = _pivot_values(obj, surface)
    if len(pivots) != 1:
        raise _defect("field 'pivotValues' must hold exactly one creative URN", surface)
    creative_urn = _checked_urn(pivots[0], PREFIX_CREATIVE, "field 'pivotValues'", surface)
    date_range = _obj(obj, "dateRange", surface, required=True)
    day = _calendar_date(date_range, "start", surface)
    if date_range.get("end") is not None and _calendar_date(date_range, "end", surface) != day:
        raise _defect("a DAILY element must have dateRange.end equal to its start", surface)
    return CreativeDailyRow(
        creative_urn=creative_urn,
        date=day,
        cost=_decimal(obj, "costInLocalCurrency", surface, required=False),
        impressions=_int(obj, "impressions", surface, required=False),
        clicks=_int(obj, "clicks", surface, required=False),
        landing_page_clicks=_int(obj, "landingPageClicks", surface, required=False),
        form_opens=_int(obj, "oneClickLeadFormOpens", surface, required=False),
        leads=_int(obj, "oneClickLeads", surface, required=False),
        website_conversions=_int(obj, "externalWebsiteConversions", surface, required=False),
    )


def parse_segment(element: object, pivot: str, *, campaign_urn: str | None = None) -> SegmentRow:
    """Parse one demographic segment element (contract ``ad_analytics_statistics``).

    Two request forms return the same element shape:

    * the Statistics Finder (``q=statistics&pivots=List(CAMPAIGN,{pivot})``), whose
      ``pivotValues`` holds the campaign URN and the demographic value in either order;
    * the Analytics Finder fallback (``q=analytics&pivot={pivot}&campaigns=List(...)`` for one
      campaign), whose ``pivotValues`` holds only the demographic value; the caller passes the
      requested campaign as ``campaign_urn``. LinkedIn 202609 rejects ``MEMBER_*`` values in
      ``pivots``, so this is the form that works live.

    Args:
        element: One element of the response.
        pivot: The requested demographic pivot, one of :data:`SEGMENT_PIVOTS`.
        campaign_urn: The campaign the request was scoped to (an id or URN). Required when
            ``pivotValues`` carries no campaign; when both exist they must agree.

    Returns:
        The segment row. A metric the pivot does not return is ``None``.

    Raises:
        ValueError: When ``pivot`` is not a segment pivot or ``campaign_urn`` is malformed.
        QueryDefect: When the campaign is unknown or contradicts ``campaign_urn``, the
            demographic value is missing or of another facet, or a field breaks its contract.
    """
    if pivot not in SEGMENT_FACETS:
        raise ValueError(f"pivot must be one of {', '.join(SEGMENT_PIVOTS)}")
    surface = _SURFACE_STATISTICS
    requested = sponsored_campaign_urn(campaign_urn) if campaign_urn is not None else None
    obj = _as_obj(element, "a segment element", surface)
    campaigns: list[str] = []
    values: list[str] = []
    for item in _pivot_values(obj, surface):
        (campaigns if item.startswith(PREFIX_CAMPAIGN) else values).append(item)
    if len(campaigns) > 1:
        raise _defect("field 'pivotValues' holds more than one campaign", surface)
    if campaigns:
        campaign = _checked_urn(campaigns[0], PREFIX_CAMPAIGN, "field 'pivotValues'", surface)
        if requested is not None and campaign != requested:
            raise _defect("field 'pivotValues' names a campaign other than requested", surface)
    elif requested is not None:
        campaign = requested
    else:
        raise _defect("field 'pivotValues' holds no campaign and none was requested", surface)
    if len(values) != 1:
        raise _defect("field 'pivotValues' must hold exactly one demographic value", surface)
    value = _checked_urn(
        values[0], _SEGMENT_VALUE_PREFIXES[pivot], "the demographic value", surface
    )
    return SegmentRow(
        campaign_urn=campaign,
        facet=SEGMENT_FACETS[pivot],
        value=value,
        impressions=_int(obj, "impressions", surface, required=False),
        clicks=_int(obj, "clicks", surface, required=False),
        cost=_decimal(obj, "costInLocalCurrency", surface, required=False),
        leads=_int(obj, "oneClickLeads", surface, required=False),
        website_conversions=_int(obj, "externalWebsiteConversions", surface, required=False),
    )


# ---------------------------------------------------------------------------
# Lead form identity and versioned email maps
# ---------------------------------------------------------------------------


def parse_versioned_form_urn(
    urn: str, *, surface: str = _SURFACE_LEAD_RESPONSES
) -> tuple[str, int]:
    """Split a versioned lead-form URN into the canonical form URN and its version.

    The version comes only from this URN: the ``form`` decoration of a lead response carries
    the form id but no version field.

    Example::

        >>> parse_versioned_form_urn("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:3162,1)")
        ('urn:li:leadGenForm:3162', 1)

    Args:
        urn: ``urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{id},{version})``.
        surface: The contract surface the URN came from, for the defect.

    Returns:
        ``("urn:li:leadGenForm:{id}", version)``, the version a 64-bit integer.

    Raises:
        QueryDefect: When ``urn`` is malformed, or its version exceeds ``2**63 - 1``
            (``code="validation"``).
        ValueError: When ``surface`` is not a LinkedIn contract surface.
    """
    surface = _check_surface(surface)
    match = _VERSIONED_FORM_RE.fullmatch(urn)
    if match is None:
        raise _defect("a versioned lead-form URN is malformed", surface)
    return PREFIX_LEAD_FORM + match.group(1), _to_int(match.group(2), "the form version", surface)


def canonical_form_urn(value: str, *, surface: str = _SURFACE_LEAD_FORMS) -> str:
    """Return the canonical, version-free ``urn:li:leadGenForm:{id}`` of a form reference.

    Accepted forms, all naming the same form id:

    * ``urn:li:leadGenForm:{id}``, returned unchanged;
    * ``urn:li:adForm:{id}``, the creative call-to-action destination;
    * ``urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{id},{version})``, version dropped.

    The content-asset registry and creative destinations join on this URN, so every version
    of a form maps to one asset.

    Args:
        value: The form reference.
        surface: The contract surface the reference came from, for the defect.

    Returns:
        The canonical form URN.

    Raises:
        QueryDefect: When ``value`` is none of the accepted forms (``code="validation"``).
        ValueError: When ``surface`` is not a LinkedIn contract surface.
    """
    surface = _check_surface(surface)
    if isinstance(value, str):
        if _is_urn(value, PREFIX_LEAD_FORM):
            return value
        if _is_urn(value, _PREFIX_AD_FORM):
            return PREFIX_LEAD_FORM + value.removeprefix(_PREFIX_AD_FORM)
        if _VERSIONED_FORM_RE.fullmatch(value):
            return parse_versioned_form_urn(value, surface=surface)[0]
    raise _defect("a lead-form reference is not a leadGenForm, adForm or versioned URN", surface)


@dataclass(frozen=True, slots=True)
class FormVersionMap:
    """The question map of one lead-form version, used to decode emails for that version only.

    Editing a form creates a new version, so a question id can change meaning between
    versions; a response is only ever decoded with its own version's map.

    Attributes:
        canonical_urn: ``urn:li:leadGenForm:{id}``.
        version: The form version, parsed from ``versionedLeadGenFormUrn`` or ``versionId``.
        email_question_ids: The email question ids in order of preference: ``EMAIL``
            questions first, then ``WORK_EMAIL``, each ascending. A tuple, not a set, so the
            preference survives the round trip through ``lead_form_version.email_question_ids``
            (``text[]``).
        question_ids: Every question id of the version when known (from a decoration or the
            ``leadForms`` sync), or ``None`` for a map rebuilt from storage, which persists
            only the email ids. When known, it drives the consistency check.
    """

    canonical_urn: str
    version: int
    email_question_ids: tuple[int, ...]
    question_ids: frozenset[int] | None = None

    def __post_init__(self) -> None:
        """Validate the identity and keep the email ids within the known questions."""
        if not _is_urn(self.canonical_urn, PREFIX_LEAD_FORM):
            raise ValueError("canonical_urn must be a urn:li:leadGenForm:{id} URN")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("version must be a non-negative integer")
        if len(set(self.email_question_ids)) != len(self.email_question_ids):
            raise ValueError("email_question_ids must not repeat an id")
        if self.question_ids is not None and not self.question_ids.issuperset(
            self.email_question_ids
        ):
            raise ValueError("email_question_ids must be among question_ids")

    @classmethod
    def from_storage(
        cls, canonical_urn: str, version: int, email_question_ids: Sequence[str]
    ) -> FormVersionMap:
        """Rebuild a map from a ``lead_form_version`` row (``email_question_ids text[]``).

        Args:
            canonical_urn: The form's canonical URN.
            version: The stored version.
            email_question_ids: The stored email question ids, in preference order.

        Returns:
            A map with ``question_ids=None``, because only email ids are persisted.

        Raises:
            ValueError: When an id is not a digit string or the identity is malformed.
        """
        ids: list[int] = []
        for text in email_question_ids:
            if not _DIGITS_RE.fullmatch(text):
                raise ValueError("stored email question ids must be digit strings")
            ids.append(int(text))
        return cls(canonical_urn=canonical_urn, version=version, email_question_ids=tuple(ids))

    @property
    def stored_email_question_ids(self) -> list[str]:
        """The email question ids as ``lead_form_version.email_question_ids`` stores them."""
        return [str(question_id) for question_id in self.email_question_ids]


KnownVersions = Mapping[tuple[str, int], FormVersionMap]
"""Stored form-version maps keyed by ``(canonical_urn, version)``."""


@dataclass(frozen=True, slots=True)
class LeadFormRow:
    """One lead form as its current version, from ``leadForms?q=owner``.

    Attributes:
        canonical_urn: ``urn:li:leadGenForm:{id}``.
        external_id: The numeric form id as a string.
        name: The form name, or ``None`` when absent.
        current_version: ``versionId``, the current version.
        version_map: The current version's question map, with ``question_ids`` filled.
    """

    canonical_urn: str
    external_id: str
    name: str | None
    current_version: int
    version_map: FormVersionMap


def _version_map(
    canonical_urn: str, version: int, content: JsonObject, surface: str
) -> FormVersionMap:
    """Build a version map from a ``content.questions`` list.

    A ``questionId`` identifies one question within a form version, so a repeated id is a
    defect: two definitions would leave the question's meaning, and the email choice,
    ambiguous.
    """
    question_ids: set[int] = set()
    by_field: dict[str, set[int]] = {name: set() for name in _EMAIL_FIELDS}
    for item in _list(content, "questions", surface, required=True):
        question = _as_obj(item, "a form question", surface)
        question_id = _int(question, "questionId", surface, required=True)
        if question_id in question_ids:
            raise _defect("field 'questionId' repeats a question of the form version", surface)
        question_ids.add(question_id)
        predefined = _str(question, "predefinedField", surface, required=False)
        if predefined is not None and predefined in by_field:
            by_field[predefined].add(question_id)
    # Each id has one definition, so the EMAIL and WORK_EMAIL sets are disjoint.
    email_ids = [qid for name in _EMAIL_FIELDS for qid in sorted(by_field[name])]
    return FormVersionMap(
        canonical_urn=canonical_urn,
        version=version,
        email_question_ids=tuple(email_ids),
        question_ids=frozenset(question_ids),
    )


def form_map_from_decoration(
    canonical_urn: str,
    version: int,
    form: JsonObject,
    *,
    surface: str = _SURFACE_LEAD_RESPONSES,
) -> FormVersionMap:
    """Build the question map of a lead response's ``form`` decoration.

    The decoration is built by LinkedIn from the response's own ``versionedLeadGenFormUrn``,
    so the map is keyed by the version parsed from that URN. ``form.id`` is never read: the
    documented sample shows it as a rounded JSON number, so identity comes only from the URN.

    Args:
        canonical_urn: The canonical form URN parsed from the response.
        version: The version parsed from the response.
        form: The ``form`` decoration object.
        surface: The contract surface, for defects.

    Returns:
        The map with every question id and the email question ids in preference order.

    Raises:
        QueryDefect: When ``form.content.questions`` is missing or malformed, or repeats a
            ``questionId``.
        ValueError: When ``canonical_urn`` or ``version`` is malformed, or ``surface`` is
            unknown.
    """
    surface = _check_surface(surface)
    decoration = _as_obj(form, "the form decoration", surface)
    content = _obj(decoration, "content", surface, required=True)
    return _version_map(canonical_urn, version, content, surface)


def parse_lead_form(element: object) -> LeadFormRow:
    """Parse one ``leadForms?q=owner`` element (contract ``lead_forms``).

    Args:
        element: One element of the forms response, which describes the current version.

    Returns:
        The form row with the current version's map.

    Raises:
        QueryDefect: When the element breaks its contract, including a ``questionId`` that
            repeats among the version's questions.
    """
    surface = _SURFACE_LEAD_FORMS
    obj = _as_obj(element, "a lead form", surface)
    external_id = str(_int(obj, "id", surface, required=True))
    canonical_urn = PREFIX_LEAD_FORM + external_id
    current_version = _int(obj, "versionId", surface, required=True)
    content = _obj(obj, "content", surface, required=True)
    return LeadFormRow(
        canonical_urn=canonical_urn,
        external_id=external_id,
        name=_str(obj, "name", surface, required=False),
        current_version=current_version,
        version_map=_version_map(canonical_urn, current_version, content, surface),
    )


# ---------------------------------------------------------------------------
# Lead responses and email decoding
# ---------------------------------------------------------------------------

_LEAD_SURFACES: Final = frozenset({_SURFACE_LEAD_RESPONSES, _SURFACE_LEAD_RESPONSES_BATCH})


@dataclass(frozen=True, slots=True)
class ParsedLead:
    """One sponsored lead form response, ready for the ``lead`` table.

    The row holds no plaintext email and no answer: only the SHA-256 of the normalised email.

    Attributes:
        external_id: The plain response id, such as ``aaaabbbb-0000-cccc-1111-dddd2222eeee-5``,
            the de-duplication key and the id the batch re-fetch sends.
        submitted_at: ``submittedAt`` as a UTC instant: the lead's touch.
        account_urn: ``owner.sponsoredAccount``, or ``None`` when absent.
        campaign_urn: The campaign that produced the lead.
        creative_urn: The creative that produced the lead, or ``None`` (unknown creative).
        lead_form_urn: The canonical form URN.
        lead_form_version: The form version, from ``versionedLeadGenFormUrn`` only.
        email_sha256: 64 lowercase hex characters, or ``None`` when no email was decoded.
        email_status: ``ok`` or ``no_email`` (terminal for lead matching).
        email_reason: One of the ``REASON_*`` constants when ``no_email``, else ``None``.
        version_map: The map learned from this element's ``form`` decoration when it passed
            the consistency check, for sync to persist as ``lead_form_version``; else ``None``.
    """

    external_id: str
    submitted_at: datetime
    account_urn: str | None
    campaign_urn: str
    creative_urn: str | None
    lead_form_urn: str
    lead_form_version: int
    email_sha256: str | None
    email_status: EmailStatus
    email_reason: str | None
    version_map: FormVersionMap | None


@dataclass(frozen=True, slots=True)
class DecodedEmail:
    """A lead's email decoded transiently for one HubSpot ``contacts_by_email`` call.

    Never store, log or attach this object or its ``email`` to an exception, and drop it once
    the HubSpot call returns. ``email`` is excluded from ``repr`` so tracebacks and log lines
    cannot carry the address.

    Attributes:
        email: The normalised (trimmed, lowercased) address, or ``None``.
        sha256: SHA-256 hex digest of ``email``, equal to the lead's stored ``email_sha256``.
        status: ``ok`` or ``no_email``.
        reason: One of the ``REASON_*`` constants when ``no_email``, else ``None``.
    """

    email: str | None = field(repr=False)
    sha256: str | None
    status: EmailStatus
    reason: str | None


def _lead_surface(surface: str) -> str:
    """Return ``surface`` when it is a lead-response surface, else raise ValueError."""
    if surface not in _LEAD_SURFACES:
        raise ValueError("surface must be lead_form_responses or lead_form_responses_batch")
    return surface


def _path(obj: JsonObject, keys: Sequence[str], surface: str) -> JsonObject | None:
    """Follow optional nested objects; ``None`` as soon as one is absent."""
    node: JsonObject | None = obj
    for key in keys:
        if node is None:
            return None
        node = _obj(node, key, surface, required=False)
    return node


def _agreeing(projected: str | None, plain: str | None, what: str, surface: str) -> str | None:
    """Return the link found by either path, rejecting two paths that disagree."""
    if projected is not None and plain is not None and projected != plain:
        raise _defect(f"the projected and unprojected {what} links disagree", surface)
    return projected if projected is not None else plain


def _lead_campaign(obj: JsonObject, surface: str) -> str:
    """Read the campaign: ``leadMetadataInfo…campaign.id`` first, then ``leadMetadata``.

    Under the ``LEAD_FIELDS`` projection LinkedIn returns only the ``*Info`` decoration;
    the unprojected documented sample carries ``leadMetadata.sponsoredLeadMetadata.campaign``.
    """
    info = _path(obj, ("leadMetadataInfo", "sponsoredLeadMetadataInfo", "campaign"), surface)
    projected = None if info is None else _urn(info, "id", PREFIX_CAMPAIGN, surface, required=False)
    metadata = _path(obj, ("leadMetadata", "sponsoredLeadMetadata"), surface)
    plain = (
        None
        if metadata is None
        else _urn(metadata, "campaign", PREFIX_CAMPAIGN, surface, required=False)
    )
    campaign = _agreeing(projected, plain, "campaign", surface)
    if campaign is None:
        raise _defect("required field 'campaign' is missing from a sponsored lead", surface)
    return campaign


def _lead_creative(obj: JsonObject, surface: str) -> str | None:
    """Read the creative: ``associatedEntityInfo.associatedCreative.id``, then the plain link.

    A null or absent creative means "unknown creative" for white-paper attribution.
    """
    info = _path(obj, ("associatedEntityInfo", "associatedCreative"), surface)
    projected = None if info is None else _urn(info, "id", PREFIX_CREATIVE, surface, required=False)
    entity = _obj(obj, "associatedEntity", surface, required=False)
    plain = (
        None
        if entity is None
        else _urn(entity, "associatedCreative", PREFIX_CREATIVE, surface, required=False)
    )
    return _agreeing(projected, plain, "creative", surface)


def _answers(obj: JsonObject, surface: str) -> list[tuple[int, JsonObject]]:
    """Read ``formResponse.answers`` as ``(questionId, answer)`` pairs, in response order.

    Only question ids are read here; an answer's content is read solely for email questions.
    ``consentResponses`` and hidden fields are never read. Answers are keyed by question id,
    so a repeated id is a defect, never an arbitrary choice between two answers.
    """
    form_response = _obj(obj, "formResponse", surface, required=True)
    pairs: list[tuple[int, JsonObject]] = []
    seen: set[int] = set()
    for item in _list(form_response, "answers", surface, required=True):
        answer = _as_obj(item, "an answer", surface)
        question_id = _int(answer, "questionId", surface, required=True)
        if question_id in seen:
            raise _defect("field 'questionId' repeats an answered question", surface)
        seen.add(question_id)
        pairs.append((question_id, answer))
    return pairs


def _is_email(candidate: str) -> bool:
    """Whether ``candidate`` is a usable address: hashable as UTF-8, with exactly one ``@``.

    The local and domain parts must be non-empty. A JSON answer can carry an escaped unpaired
    surrogate such as U+D800, which decodes to a string UTF-8 cannot encode, so it could never
    be hashed for ``email_sha256``; such an answer is not an address. Non-ASCII characters that
    UTF-8 encodes are accepted.
    """
    try:
        candidate.encode("utf-8")
    except UnicodeEncodeError:
        return False
    local, separator, domain = candidate.partition("@")
    return bool(separator) and bool(local) and bool(domain) and "@" not in domain


def _email_answer(
    answers: Sequence[tuple[int, JsonObject]], email_question_ids: Sequence[int], surface: str
) -> str | None:
    """Return the first usable email answer in preference order, normalised.

    ``answers`` comes from :func:`_answers`, which rejects a repeated question id, so each
    question has at most one answer.
    """
    by_question: dict[int, JsonObject] = dict(answers)
    for question_id in email_question_ids:
        chosen = by_question.get(question_id)
        if chosen is None:
            continue
        text = _path(chosen, ("answerDetails", "textQuestionAnswer"), surface)
        raw = None if text is None else _str(text, "answer", surface, required=False)
        if raw is None:
            continue
        normalised = raw.strip().lower()
        if _is_email(normalised):
            return normalised
    return None


def _decode(
    obj: JsonObject, known_versions: KnownVersions, surface: str
) -> tuple[str | None, EmailStatus, str | None, FormVersionMap | None, str, int]:
    """Decode a response's email with its own form version's map.

    Returns:
        ``(plaintext, status, reason, learned_map, canonical_urn, version)``. ``learned_map``
        is the decoration's map when it passed the consistency check, else ``None``.
    """
    canonical_urn, version = parse_versioned_form_urn(
        _str(obj, "versionedLeadGenFormUrn", surface, required=True), surface=surface
    )
    answers = _answers(obj, surface)
    answered = frozenset(question_id for question_id, _ in answers)
    form = _obj(obj, "form", surface, required=False)
    learned: FormVersionMap | None = None
    if form is not None:
        # The decoration describes this response's version; when an answered question is not
        # among its questions it does not, and the lead is never decoded with another map.
        version_map = form_map_from_decoration(canonical_urn, version, form, surface=surface)
        if version_map.question_ids is None or not answered <= version_map.question_ids:
            return None, "no_email", REASON_FORM_MAP_MISMATCH, None, canonical_urn, version
        learned = version_map
    else:
        stored = known_versions.get((canonical_urn, version))
        if stored is None:
            return None, "no_email", REASON_UNKNOWN_FORM_VERSION, None, canonical_urn, version
        if stored.question_ids is not None and not answered <= stored.question_ids:
            return None, "no_email", REASON_FORM_MAP_MISMATCH, None, canonical_urn, version
        version_map = stored
    if not version_map.email_question_ids:
        return None, "no_email", REASON_NO_EMAIL_QUESTION, learned, canonical_urn, version
    email = _email_answer(answers, version_map.email_question_ids, surface)
    if email is None:
        return None, "no_email", REASON_NO_EMAIL_ANSWER, learned, canonical_urn, version
    return email, "ok", None, learned, canonical_urn, version


def _sha256(email: str) -> str:
    """Return the SHA-256 hex digest of a normalised email (64 lowercase hex characters)."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


def parse_lead(
    element: object,
    known_versions: KnownVersions,
    *,
    surface: str = _SURFACE_LEAD_RESPONSES,
) -> ParsedLead | None:
    """Parse one lead form response for ingest, hashing its email and dropping test leads.

    ``limitedToTestLeads=false`` returns test and real leads together, so an element with
    ``testLead`` true yields ``None`` and is never stored. The email is decoded with the
    response's own form version (from ``versionedLeadGenFormUrn``): the ``form`` decoration
    when present, else ``known_versions``. The plaintext is hashed and discarded here.

    Args:
        element: One element of ``leadFormResponses?q=owner`` or one value of the batch
            re-fetch ``results`` map.
        known_versions: Stored version maps, keyed by ``(canonical_urn, version)``.
        surface: ``lead_form_responses`` or ``lead_form_responses_batch``, for defects.

    Returns:
        The parsed lead, or ``None`` for a test lead.

    Raises:
        QueryDefect: When ``id``, ``submittedAt``, ``versionedLeadGenFormUrn`` or the
            campaign is missing, a ``questionId`` repeats among the answers or the decorated
            questions, or a field breaks its contract.
        ValueError: When ``surface`` is not a lead-response surface.
    """
    surface = _lead_surface(surface)
    obj = _as_obj(element, "a lead form response", surface)
    if _bool(obj, "testLead", surface, required=True):
        return None
    external_id = _str(obj, "id", surface, required=True)
    if not external_id.strip():
        raise _defect("field 'id' must not be blank", surface)
    submitted_at = _instant(obj, "submittedAt", surface, required=True)
    owner = _obj(obj, "owner", surface, required=False)
    account_urn = (
        None
        if owner is None
        else _urn(owner, "sponsoredAccount", PREFIX_ACCOUNT, surface, required=False)
    )
    campaign_urn = _lead_campaign(obj, surface)
    creative_urn = _lead_creative(obj, surface)
    email, status, reason, learned, canonical_urn, version = _decode(obj, known_versions, surface)
    email_sha256 = None if email is None else _sha256(email)
    del email  # the plaintext never outlives this function
    return ParsedLead(
        external_id=external_id,
        submitted_at=submitted_at,
        account_urn=account_urn,
        campaign_urn=campaign_urn,
        creative_urn=creative_urn,
        lead_form_urn=canonical_urn,
        lead_form_version=version,
        email_sha256=email_sha256,
        email_status=status,
        email_reason=reason,
        version_map=learned,
    )


def decode_email(
    element: object,
    known_versions: KnownVersions,
    *,
    surface: str = _SURFACE_LEAD_RESPONSES_BATCH,
) -> DecodedEmail:
    """Decode a re-fetched lead's email transiently, for one HubSpot batch read.

    ``sync/lead_match.py`` calls this on an element returned by
    ``LinkedInClient.get_lead_responses``, passes ``email`` to a single HubSpot
    ``contacts_by_email`` call and then drops the result. The result must never be stored,
    logged or attached to an exception. It shares its decoding with :func:`parse_lead`, so
    ``sha256`` always equals the ``email_sha256`` stored for the same element.

    Args:
        element: One value of the batch re-fetch ``results`` map (or a page element).
        known_versions: Stored version maps, keyed by ``(canonical_urn, version)``.
        surface: ``lead_form_responses_batch`` or ``lead_form_responses``, for defects.

    Returns:
        The decoded email, or ``status="no_email"`` with its reason.

    Raises:
        QueryDefect: When ``versionedLeadGenFormUrn``, the answers or the decorated questions
            break the contract, including a ``questionId`` repeated among the answers or the
            decorated questions.
        ValueError: When ``surface`` is not a lead-response surface.
    """
    surface = _lead_surface(surface)
    obj = _as_obj(element, "a lead form response", surface)
    email, status, reason, _learned, _canonical, _version = _decode(obj, known_versions, surface)
    return DecodedEmail(
        email=email,
        sha256=None if email is None else _sha256(email),
        status=status,
        reason=reason,
    )
