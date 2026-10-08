"""Pure decoders for HubSpot CRM responses (API ``2026-09``) and the typed rows they yield.

``connectors/hubspot/client.py`` and ``connectors/hubspot/search.py`` send the requests; this
module turns each decoded response body into frozen dataclasses:

* typed CRM rows, one per column set of AAP 0.5.2: :class:`ContactRow` (``crm_contact``),
  :class:`MeetingRow` (``crm_meeting``) and :class:`DealRow` (``crm_deal``);
* one envelope per contract surface in ``backend/tests/contracts/hubspot/``:
  :func:`parse_batch_read` (``objects_batch_read``), :func:`parse_association_batch`
  (``associations_batch_read``), :func:`parse_search_page` (``crm_search``) and
  :func:`parse_property` (``properties``).

Purity:
    No I/O, no HTTP, no database, no clock and no logging. The module imports only the
    standard library and :mod:`ads_engine.connectors.errors`. Every payload that breaks its
    contract raises ``QueryDefect(source="hubspot", endpoint="parse", code="validation")``
    whose message names the surface and the field, never a value that could be an email.

Validation (``backend/tests/contracts/hubspot/``):
    * Batch envelopes (objects and associations): ``status`` is read first and must be one of
      ``CANCELED``, ``COMPLETE``, ``PENDING`` and ``PROCESSING``; ``results`` must be an
      array. A status other than ``COMPLETE`` is a failed call (AAP 0.2.4), returned with no
      usable data and with its ``results`` elements never validated.
    * A ``COMPLETE`` envelope also needs ``startedAt`` and ``completedAt`` instants, error
      entries with string ``status``, ``category`` and ``message``, and, when present, a
      ``numErrors`` equal to the number of error entries. A failed input is absent from
      ``results``, so an input both returned and named by an error entry is a defect.
    * CRM objects (a ``COMPLETE`` batch read or any search page): a string ``id``, a
      ``properties`` object, ``createdAt`` and ``updatedAt`` instants and a boolean
      ``archived``. Every property a decoder reads must be a string or ``null``.
    * Association targets: ``toObjectId`` (a string or an integer) and ``associationTypes``
      entries with a contract ``category`` and an integer ``typeId``.
    * Property definitions: every field the contract requires, and each option's ``hidden``,
      ``label`` and ``value``; nothing is defaulted.

Personal data (AAP 0.3.2):
    An email is normalised (trimmed, lowercased) and kept only as the SHA-256 hex digest of its
    UTF-8 bytes, the same digest the LinkedIn parser stores on ``lead.email_sha256``, so the two
    join. No typed row and no :class:`BatchError` holds a plaintext address: an error context
    id that is an email (contacts read with ``idProperty=email``) is carried as its digest in
    :attr:`BatchError.email_sha256s`. The raw records inside :class:`BatchReadResult` and
    :class:`SearchPage` are HubSpot's own objects; the connector holds them in memory only for
    the call that parses them.

Instants and money (Decision 6):
    Instants are timezone-aware UTC ``datetime`` values parsed from HubSpot's own fields and
    never replaced by the current time. Money is an exact :class:`~decimal.Decimal`.

Absent values:
    A property HubSpot did not return, or returned empty, is ``None`` ("unavailable"), never a
    default. An empty stage-entry date means the contact skipped the stage, so it is not
    counted, and QL is read from its own property, never derived from MQL (AAP 0.5.4.7).

Strings:
    Text kept for storage is stripped, and a value carrying U+0000 or a code point UTF-8 cannot
    encode is a defect, because PostgreSQL ``text`` can hold neither.

Example:
    The client decodes a 207 contacts batch read and maps it back to its inputs by hash::

        result = parse_batch_read(response.body)
        for record in result.records:
            row = parse_contact(record, props)
            hashes = contact_emails_sha256(record)  # primary plus additional emails
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final

from ads_engine.connectors.errors import QueryDefect, sanitize_message

__all__ = [
    "BATCH_COMPLETE",
    "DEAL_PROPERTIES",
    "MEETING_PROPERTIES",
    "NO_ASSOCIATIONS_FOUND",
    "AssociationBatch",
    "AssociationResult",
    "BatchError",
    "BatchReadResult",
    "ContactPropertyMap",
    "ContactRow",
    "DealRow",
    "HubSpotRecord",
    "MeetingRow",
    "PropertyDefinition",
    "PropertyOption",
    "SearchPage",
    "contact_emails_sha256",
    "email_sha256",
    "normalize_email",
    "parse_association_batch",
    "parse_batch_read",
    "parse_contact",
    "parse_deal",
    "parse_decimal",
    "parse_instant",
    "parse_meeting",
    "parse_property",
    "parse_search_page",
]

# An implicit alias rather than `HubSpotRecord: TypeAlias = ...`: ruff's UP040 rejects the
# explicit `TypeAlias` annotation for the py314 target, and a `type` statement would stop the
# alias from being an ordinary `Mapping[str, Any]` at runtime. mypy treats both alike.
HubSpotRecord = Mapping[str, Any]
"""One raw CRM object as HubSpot returns it: ``{id, properties{…}, createdAt, updatedAt,
archived}``, with every property value a string or ``null``."""

BATCH_COMPLETE: Final = "COMPLETE"
"""The only batch ``status`` that is a successful call; ``CANCELED``, ``PENDING`` and
``PROCESSING`` are failed calls even when the body is otherwise valid."""

# The batch `status` enum of the objects and associations contracts, in contract order.
_BATCH_STATES: Final = ("CANCELED", BATCH_COMPLETE, "PENDING", "PROCESSING")
# The `to[].associationTypes[].category` enum of the associations contract.
_ASSOCIATION_CATEGORIES: Final = ("HUBSPOT_DEFINED", "INTEGRATOR_DEFINED", "USER_DEFINED", "WORK")

NO_ASSOCIATIONS_FOUND: Final = "crm.associations.NO_ASSOCIATIONS_FOUND"
"""The 207 ``errors[].subCategory`` HubSpot uses for an input that has no associations."""

MEETING_PROPERTIES: Final = (
    "hs_createdate",
    "hs_timestamp",
    "hs_meeting_start_time",
    "hs_meeting_outcome",
    "hs_meeting_source",
)
"""Meeting properties a target read requests (AAP 0.3.3).

``hs_meeting_source`` is the internal name of HubSpot's "Meeting source" enumeration, whose
options include ``MEETINGS_PUBLIC`` (scheduling page) and ``BIDIRECTIONAL_SYNC``.
``hs_meeting_end_time`` is not requested, because ``crm_meeting`` has no column for it."""

DEAL_PROPERTIES: Final = ("createdate", "dealstage", "pipeline", "amount")
"""Deal properties a target read or the window deal search requests (AAP 0.3.3)."""

_BASE_CONTACT_PROPERTIES: Final = (
    "email",
    "hs_additional_emails",
    "lifecyclestage",
    "lastmodifieddate",
)

_ENDPOINT: Final = "parse"
_SURFACE_OBJECTS: Final = "objects_batch_read"
_SURFACE_ASSOCIATIONS: Final = "associations_batch_read"
_SURFACE_SEARCH: Final = "crm_search"
_SURFACE_PROPERTIES: Final = "properties"

# The last dotted segment of an error category or subcategory that marks the empty case.
_NO_ASSOCIATIONS_SEGMENT: Final = "NO_ASSOCIATIONS_FOUND"
_REDACTED_EMAIL: Final = "[REDACTED_EMAIL]"

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_DATE_ONLY_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# An ISO-8601 calendar date followed by a time: `2026-09-04T13:05:22.123Z`, an offset, or none.
_ISO_DATETIME_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}")
_INSTANT_EXPECTED: Final = "expected an ISO-8601 instant, a YYYY-MM-DD date or epoch milliseconds"

# numeric(18, 6), the column type db/base.py maps Decimal to: below 10**12, six places at most.
_NUMERIC_LIMIT: Final = Decimal(10) ** 12
_NUMERIC_QUANTUM: Final = Decimal("0.000001")


# ---------------------------------------------------------------------------
# Defects and storage checks
# ---------------------------------------------------------------------------


def _defect(message: str, surface: str | None = None) -> QueryDefect:
    """Build the validation defect for a payload that breaks its contract.

    Args:
        message: What is wrong, naming the field only, never a value from a record.
        surface: The contract surface the payload came from, when known.

    Returns:
        A ``QueryDefect`` for the ``hubspot`` source with ``endpoint="parse"`` and
        ``code="validation"``.
    """
    text = f"{surface}: {message}" if surface else message
    return QueryDefect(text, source="hubspot", endpoint=_ENDPOINT, code="validation")


def _utf8_encodable(value: str) -> bool:
    """Whether UTF-8 can encode ``value``; an unpaired surrogate such as U+D800 cannot."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check_storable(value: str, what: str, surface: str | None = None) -> str:
    """Return ``value`` when PostgreSQL ``text`` can store it, else raise a defect.

    The defect is raised outside any ``except`` block, so it chains no
    ``UnicodeEncodeError``, which would carry the offending string.
    """
    if "\x00" in value:
        raise _defect(f"{what} must not contain a NUL character", surface)
    if not _utf8_encodable(value):
        raise _defect(f"{what} must be valid UTF-8 text", surface)
    return value


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def normalize_email(value: str) -> str:
    """Normalise an email address the way every hashed email in the product is normalised.

    Args:
        value: An email address as entered or returned.

    Returns:
        ``value`` trimmed and lowercased.

    Example::

        >>> normalize_email("  Lead@Example.COM ")
        'lead@example.com'
    """
    return value.strip().lower()


def email_sha256(value: str) -> str:
    """Hash an email for storage and joins: SHA-256 of its normalised UTF-8 bytes.

    The digest equals the LinkedIn parser's ``lead.email_sha256`` for the same address, so
    ``lead.email_sha256`` joins ``crm_contact.email_sha256``.

    Args:
        value: An email address; it is normalised with :func:`normalize_email` first.

    Returns:
        64 lowercase hexadecimal characters.

    Raises:
        ValueError: When the normalised address is empty or UTF-8 cannot encode it. The error
            carries no part of the address.

    Example::

        >>> email_sha256(" A@B.com ") == hashlib.sha256(b"a@b.com").hexdigest()
        True
    """
    normalized = normalize_email(value)
    if not normalized:
        raise ValueError("email address is empty")
    if not _utf8_encodable(normalized):
        raise ValueError("email address is not valid UTF-8 text")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _optional_email_sha256(value: object, what: str, surface: str | None = None) -> str | None:
    """Hash an optional email property value; ``None`` when it holds no usable address.

    An absent, empty or whitespace-only value, or one UTF-8 cannot encode, holds no address
    to match on, so it is ``None`` rather than a defect. A value that is not a string breaks
    the contract (property values are strings or ``null``).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise _defect(f"{what} must be a string", surface)
    normalized = normalize_email(value)
    if not normalized or not _utf8_encodable(normalized):
        return None
    return email_sha256(normalized)


def _from_epoch_ms(milliseconds: int) -> datetime | None:
    """Convert epoch milliseconds to a UTC instant; ``None`` when out of ``datetime`` range."""
    try:
        return _EPOCH + timedelta(milliseconds=milliseconds)
    except OverflowError:
        return None


def _from_epoch_ms_digits(digits: str) -> datetime | None:
    """Convert a string of ASCII digits, epoch milliseconds, to a UTC instant.

    ``None`` when the number is out of ``datetime`` range, including a string too long for
    ``int`` to convert at all (Python refuses more than 4,300 digits with ``ValueError``), so
    the caller raises its defect outside the ``except`` block.
    """
    try:
        milliseconds = int(digits)
    except ValueError:
        return None
    return _from_epoch_ms(milliseconds)


def _from_iso(text: str) -> datetime | None:
    """Parse an ISO-8601 datetime as UTC; ``None`` when it is not one or is out of range.

    A naive value is read as UTC; an offset is converted to UTC. Failures return ``None`` so
    the caller raises its defect outside the ``except`` block, chaining no exception that
    carries the text.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        return None


def _from_date(text: str) -> datetime | None:
    """Parse ``YYYY-MM-DD`` as midnight UTC; ``None`` when it is not a valid date."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def parse_instant(
    value: object, *, what: str = "instant", surface: str | None = None
) -> datetime | None:
    """Parse a HubSpot date or datetime value into a timezone-aware UTC ``datetime``.

    Accepted forms:

    * ``None``, ``""`` or whitespace: ``None``. An empty stage-entry date means the contact
      skipped that stage, so it is not counted (AAP 0.5.4.7).
    * ISO-8601 with ``Z`` or an offset (``2026-09-04T13:05:22.123Z``): converted to UTC.
    * A naive ISO-8601 datetime: read as UTC.
    * ``YYYY-MM-DD`` (HubSpot ``date`` properties): midnight UTC.
    * A string of ASCII digits, an ``int`` or an integral ``Decimal``: epoch milliseconds.

    Args:
        value: The raw property or record field value.
        what: The field's name for the defect message, for example
            ``"contact property 'hs_v2_date_entered_1406943076'"``. The value itself is
            never put in the message, because it could be an email.
        surface: The contract surface the value came from, which prefixes the defect message
            when given.

    Returns:
        The instant in UTC, or ``None`` when the value is empty.

    Raises:
        QueryDefect: For any other value, including a ``bool``, a non-integral number, a
            malformed string and an instant outside the ``datetime`` range.

    Example::

        >>> parse_instant("2026-09-04T09:05:22-04:00")
        datetime.datetime(2026, 9, 4, 13, 5, 22, tzinfo=datetime.timezone.utc)
    """
    if value is None:
        return None
    parsed: datetime | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = _from_epoch_ms(value)
    elif isinstance(value, Decimal):
        if value.is_finite() and value == value.to_integral_value():
            parsed = _from_epoch_ms(int(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isascii() and text.isdigit():
            parsed = _from_epoch_ms_digits(text)
        elif _DATE_ONLY_RE.fullmatch(text):
            parsed = _from_date(text)
        elif _ISO_DATETIME_RE.match(text):
            parsed = _from_iso(text)
    if parsed is None:
        raise _defect(f"{what}: {_INSTANT_EXPECTED}", surface)
    return parsed


def _decimal_from_text(text: str) -> Decimal | None:
    """Parse a numeric string as an exact ``Decimal``; ``None`` when it is not numeric."""
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_decimal(value: object) -> Decimal | None:
    """Parse a HubSpot number property into an exact ``Decimal``.

    HubSpot sends number properties as strings (``"1500.50"``); ``connectors/http.py``
    decodes JSON numbers with a fraction as ``Decimal`` and whole numbers as ``int``. Floats
    are converted through their shortest ``repr``, never ``Decimal(float)``.

    Args:
        value: The raw property value.

    Returns:
        The exact value, or ``None`` ("unavailable") when the value is ``None``, empty, a
        ``bool``, not numeric (``"n/a"``), NaN or infinite. A non-numeric value is
        unavailable rather than a defect, because number properties are user-entered.

    Example::

        >>> parse_decimal("1500.50")
        Decimal('1500.50')
        >>> parse_decimal("") is None
        True
    """
    number: Decimal | None
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = _decimal_from_text(repr(value))
    elif isinstance(value, str):
        text = value.strip()
        number = _decimal_from_text(text) if text else None
    else:
        number = None
    if number is None or not number.is_finite():
        return None
    return number


def _text(value: str | None, *, what: str = "value", surface: str | None = None) -> str | None:
    """Return a wire string as stripped text; ``None`` when it is absent, empty or blank.

    Callers check the wire type first (:func:`_property` for record properties), so only a
    string or ``None`` reaches this.

    Raises:
        QueryDefect: When the text holds a NUL or a code point UTF-8 cannot encode.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return _check_storable(text, what, surface)


# ---------------------------------------------------------------------------
# Record helpers
# ---------------------------------------------------------------------------


def _as_mapping(value: object, what: str, surface: str | None = None) -> Mapping[str, Any]:
    """Narrow a decoded JSON value to an object, or raise a defect naming ``what``."""
    if not isinstance(value, Mapping):
        raise _defect(f"{what} must be a JSON object", surface)
    return value


def _id_text(value: object, what: str, surface: str | None = None) -> str:
    """Return a HubSpot id as text: a non-blank string as given, or an ``int`` in decimal.

    HubSpot ids exceed 2**53, so they usually arrive as strings; association target ids
    arrive as JSON integers. Either becomes the same string.
    """
    if value is None:
        raise _defect(f"required field {what} is missing", surface)
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise _defect(f"{what} must be a string or an integer", surface)
    text = value if isinstance(value, str) else str(value)
    if not text.strip():
        raise _defect(f"{what} must not be blank", surface)
    return _check_storable(text, what, surface)


def _string_id(value: object, what: str, surface: str | None = None) -> str:
    """Return an id the contract types as a string, such as a CRM object's ``id``, as given.

    Unlike :func:`_id_text`, a JSON number is a defect: only association ids are admitted as
    numbers by their contract.
    """
    if value is None:
        raise _defect(f"required field {what} is missing", surface)
    if not isinstance(value, str):
        raise _defect(f"{what} must be a string", surface)
    if not value.strip():
        raise _defect(f"{what} must not be blank", surface)
    return _check_storable(value, what, surface)


def _string_field(obj: Mapping[str, Any], key: str, what: str, surface: str | None) -> str:
    """Read a required string field; only its presence and type are checked.

    Args:
        obj: The JSON object holding the field.
        key: The field's key.
        what: The quoted field name for defect messages, for example ``"'errors[].status'"``.
        surface: The contract surface, when known.
    """
    value = obj.get(key)
    if value is None:
        raise _defect(f"required field {what} is missing", surface)
    if not isinstance(value, str):
        raise _defect(f"field {what} must be a string", surface)
    return value


def _required_instant(obj: Mapping[str, Any], key: str, what: str, surface: str | None) -> datetime:
    """Read a required string field that must hold an instant, such as ``createdAt``.

    Raises:
        QueryDefect: When the field is missing, not a string, blank, or not an instant
            :func:`parse_instant` accepts.
    """
    instant = parse_instant(_string_field(obj, key, what, surface), what=what, surface=surface)
    if instant is None:
        raise _defect(f"field {what} must not be blank", surface)
    return instant


@dataclass(frozen=True, slots=True)
class _CrmObject:
    """The validated parts of one raw CRM object (see :func:`_record`)."""

    hs_id: str
    properties: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


def _record(record: object, kind: str, surface: str | None = None) -> _CrmObject:
    """Validate a raw CRM object against the contract's required record fields.

    The objects batch read and CRM Search contracts both require ``id`` (a string),
    ``properties`` (an object), ``createdAt`` and ``updatedAt`` (instants) and ``archived``
    (a boolean) on every returned record, so a record lacking any of them is never read.

    Args:
        record: One element of ``results[]``.
        kind: ``contact``, ``meeting``, ``deal`` or ``record``, for defect messages.
        surface: The contract surface, when known.

    Returns:
        The id, the ``properties`` map (possibly empty, never absent) and the record's
        creation and last-update instants.

    Raises:
        QueryDefect: When the record is not an object, its ``id`` is missing, not a string or
            blank, its ``properties`` is missing or not an object, ``createdAt`` or
            ``updatedAt`` is missing, not a string, blank or not an instant, or ``archived``
            is missing or not a boolean.
    """
    obj = _as_mapping(record, f"{kind} record", surface)
    hs_id = _string_id(obj.get("id"), f"'{kind}.id'", surface)
    properties = obj.get("properties")
    if properties is None:
        raise _defect(f"required field '{kind}.properties' is missing", surface)
    properties_map = _as_mapping(properties, f"field '{kind}.properties'", surface)
    created_at = _required_instant(obj, "createdAt", f"'{kind}.createdAt'", surface)
    updated_at = _required_instant(obj, "updatedAt", f"'{kind}.updatedAt'", surface)
    archived = obj.get("archived")
    if archived is None:
        raise _defect(f"required field '{kind}.archived' is missing", surface)
    if not isinstance(archived, bool):
        raise _defect(f"field '{kind}.archived' must be a boolean", surface)
    return _CrmObject(
        hs_id=hs_id, properties=properties_map, created_at=created_at, updated_at=updated_at
    )


def _property_label(kind: str, name: str) -> str:
    """Name a record property for defect messages, for example ``contact property 'email'``."""
    return f"{kind} property '{name}'"


def _property(
    properties: Mapping[str, Any], name: str, kind: str, surface: str | None = None
) -> str | None:
    """Read one property a decoder consumes; its contract value is a string or ``null``.

    An absent property reads as ``None``, like ``null``. A string is returned as given, so an
    empty or blank value reaches the caller, which treats it as unavailable.

    Raises:
        QueryDefect: When the value is any other JSON type; the message names the property.
    """
    value = properties.get(name)
    if value is None or isinstance(value, str):
        return value
    raise _defect(f"{_property_label(kind, name)} must be a string", surface)


def _text_property(properties: Mapping[str, Any], name: str, kind: str) -> str | None:
    """Read a text property as stripped text; ``None`` when absent, ``null`` or blank."""
    label = _property_label(kind, name)
    return _text(_property(properties, name, kind), what=label)


def _instant_property(properties: Mapping[str, Any], name: str, kind: str) -> datetime | None:
    """Read a date or datetime property; ``None`` when absent, ``null`` or blank."""
    label = _property_label(kind, name)
    return parse_instant(_property(properties, name, kind), what=label)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContactPropertyMap:
    """The contact properties bound to each CRM metric, resolved against the portal.

    ``client.contact_property_map`` builds it from the verified ``CrmBindings``. Each field is
    ``None`` when its binding is unavailable: the metric is then "unavailable", never 0.

    Attributes:
        mql_property: The MQL date property, ``hs_v2_date_entered_{stage id}`` in stage-label
            mode or the configured datetime property (for example
            ``date_first_meeting_was_booked``) in property mode.
        ql_property: The Qualified Lead date property, resolved independently of MQL.
        google_campaign_property: ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` when the Google link is
            configured and verified.
        google_touch_property: ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` under the same
            condition.

    Raises:
        ValueError: On construction, when a property name is blank or has surrounding
            whitespace, which HubSpot would reject.
    """

    mql_property: str | None = None
    ql_property: str | None = None
    google_campaign_property: str | None = None
    google_touch_property: str | None = None

    def __post_init__(self) -> None:
        """Reject property names HubSpot could never resolve."""
        for name in self._bound():
            if not name.strip() or name != name.strip():
                raise ValueError(f"contact property name {name!r} must be a non-blank name")

    def _bound(self) -> tuple[str, ...]:
        """Return the bound property names, in field order."""
        names = (
            self.mql_property,
            self.ql_property,
            self.google_campaign_property,
            self.google_touch_property,
        )
        return tuple(name for name in names if name is not None)

    def request_properties(self) -> tuple[str, ...]:
        """Return the ``properties`` list of a contact batch read.

        Returns:
            ``("email", "hs_additional_emails", "lifecyclestage", "lastmodifieddate",
            *bound properties)``, de-duplicated with the first position kept.

        Example::

            >>> props = ContactPropertyMap(ql_property="hs_v2_date_entered_1406943076")
            >>> props.request_properties()[3:]
            ('lastmodifieddate', 'hs_v2_date_entered_1406943076')
        """
        return tuple(dict.fromkeys((*_BASE_CONTACT_PROPERTIES, *self._bound())))


@dataclass(frozen=True, slots=True)
class ContactRow:
    """One HubSpot contact, shaped as a ``crm_contact`` row (AAP 0.5.2).

    Attributes:
        hs_id: The HubSpot contact id.
        email_sha256: SHA-256 hex of the normalised primary ``email``; ``None`` without one.
            The plaintext is never kept.
        lifecycle_stage: The raw internal ``lifecyclestage`` value; custom stages are numeric.
        mql_at: When the contact entered the bound MQL stage; ``None`` when it skipped the
            stage or the binding is unavailable.
        ql_at: When the contact entered the bound QL stage, read from its own property.
        google_campaign_ref: The Google campaign id held in the configured property.
        google_touch_at: The instant held in the configured Google touch-date property.
        updated_at: The record's ``updatedAt``, which the contracts require on every record.
    """

    hs_id: str
    email_sha256: str | None
    lifecycle_stage: str | None
    mql_at: datetime | None
    ql_at: datetime | None
    google_campaign_ref: str | None
    google_touch_at: datetime | None
    updated_at: datetime | None


def _bound_instant(properties: Mapping[str, Any], name: str | None) -> datetime | None:
    """Read the instant of a bound contact property; ``None`` when it is unbound or empty."""
    if name is None:
        return None
    return _instant_property(properties, name, "contact")


def parse_contact(record: HubSpotRecord, props: ContactPropertyMap) -> ContactRow:
    """Parse one contact from a batch read or a CRM Search page.

    MQL and QL are each read from their own bound property: an unbound property is
    unavailable (``None``), an empty date means the stage was skipped (``None``), and neither
    is ever derived from the other or from the current ``lifecyclestage`` (AAP 0.5.4.7).

    Args:
        record: One raw contact object.
        props: The bound contact properties, the same map whose
            :meth:`~ContactPropertyMap.request_properties` the request sent.

    Returns:
        The contact row, holding the email only as its SHA-256 digest.

    Raises:
        QueryDefect: When the record lacks a string ``id``, a ``properties`` object,
            ``createdAt`` or ``updatedAt`` instants or a boolean ``archived``, a property it
            reads is neither a string nor ``null``, a date property is malformed, or a text
            property cannot be stored.
    """
    crm = _record(record, "contact")
    properties = crm.properties
    google_campaign = props.google_campaign_property
    return ContactRow(
        hs_id=crm.hs_id,
        email_sha256=_optional_email_sha256(
            _property(properties, "email", "contact"), _property_label("contact", "email")
        ),
        lifecycle_stage=_text_property(properties, "lifecyclestage", "contact"),
        mql_at=_bound_instant(properties, props.mql_property),
        ql_at=_bound_instant(properties, props.ql_property),
        google_campaign_ref=(
            None
            if google_campaign is None
            else _text_property(properties, google_campaign, "contact")
        ),
        google_touch_at=_bound_instant(properties, props.google_touch_property),
        updated_at=crm.updated_at,
    )


def contact_emails_sha256(record: HubSpotRecord) -> frozenset[str]:
    """Return the digests of every email a contact answers to.

    That is the primary ``email`` plus each semicolon-separated entry of
    ``hs_additional_emails``: a contacts batch read by ``idProperty=email`` can match an input
    on either. ``client.contacts_by_email`` maps returned records back to its hashed inputs
    with this set, so it never holds a plaintext address longer than the call.

    Args:
        record: One raw contact object.

    Returns:
        The SHA-256 hex digests; empty when the contact has no usable email.

    Raises:
        QueryDefect: When the record lacks a string ``id``, a ``properties`` object,
            ``createdAt`` or ``updatedAt`` instants or a boolean ``archived``, or an email
            property is neither a string nor ``null``.
    """
    return _contact_digests(_record(record, "contact").properties)


def _contact_digests(properties: Mapping[str, Any], surface: str | None = None) -> frozenset[str]:
    """Hash ``email`` and each ``hs_additional_emails`` entry of a contact's properties."""
    hashes: set[str] = set()
    primary = _optional_email_sha256(
        _property(properties, "email", "contact", surface),
        _property_label("contact", "email"),
        surface,
    )
    if primary is not None:
        hashes.add(primary)
    additional = _property(properties, "hs_additional_emails", "contact", surface)
    what = _property_label("contact", "hs_additional_emails")
    if additional is not None:
        for entry in additional.split(";"):
            digest = _optional_email_sha256(entry, what, surface)
            if digest is not None:
                hashes.add(digest)
    return frozenset(hashes)


# ---------------------------------------------------------------------------
# Meetings and deals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MeetingRow:
    """One HubSpot meeting, shaped as a ``crm_meeting`` row (AAP 0.5.2).

    Attributes:
        hs_id: The HubSpot meeting id.
        created_at: ``hs_createdate``, else the record's ``createdAt``. Both are HubSpot's
            record-creation time, the labelled booking proxy "booked (record created)"
            (AAP 0.5.4.7); never the current time.
        start_at: ``hs_meeting_start_time``.
        meeting_timestamp: ``hs_timestamp``.
        outcome: ``hs_meeting_outcome`` stripped and upper-cased, so it compares with
            ``crm.meeting_outcome_exclusions`` such as ``CANCELED``.
        meeting_source: The raw ``hs_meeting_source``, for example ``MEETINGS_PUBLIC``.
    """

    hs_id: str
    created_at: datetime | None
    start_at: datetime | None
    meeting_timestamp: datetime | None
    outcome: str | None
    meeting_source: str | None


def parse_meeting(record: HubSpotRecord) -> MeetingRow:
    """Parse one meeting from a target batch read that requested :data:`MEETING_PROPERTIES`.

    Args:
        record: One raw meeting object.

    Returns:
        The meeting row.

    Raises:
        QueryDefect: When the record lacks a string ``id``, a ``properties`` object,
            ``createdAt`` or ``updatedAt`` instants or a boolean ``archived``, a property it
            reads is neither a string nor ``null``, an instant is malformed, or a text
            property cannot be stored.
    """
    crm = _record(record, "meeting")
    properties = crm.properties
    created_at = _instant_property(properties, "hs_createdate", "meeting")
    outcome = _text_property(properties, "hs_meeting_outcome", "meeting")
    return MeetingRow(
        hs_id=crm.hs_id,
        created_at=crm.created_at if created_at is None else created_at,
        start_at=_instant_property(properties, "hs_meeting_start_time", "meeting"),
        meeting_timestamp=_instant_property(properties, "hs_timestamp", "meeting"),
        outcome=None if outcome is None else outcome.upper(),
        meeting_source=_text_property(properties, "hs_meeting_source", "meeting"),
    )


@dataclass(frozen=True, slots=True)
class DealRow:
    """One HubSpot deal, shaped as a ``crm_deal`` row (AAP 0.5.2).

    Attributes:
        hs_id: The HubSpot deal id.
        created_at: ``createdate``, else the record's ``createdAt``: the deal's creation time,
            exactly the event the causal check names (AAP 0.5.4.7).
        stage: The raw ``dealstage``.
        pipeline: The raw ``pipeline``.
        amount: The exact ``amount``; ``None`` when unset, not numeric, or not storable
            exactly in ``numeric(18, 6)``.
    """

    hs_id: str
    created_at: datetime | None
    stage: str | None
    pipeline: str | None
    amount: Decimal | None


def _storable_amount(amount: Decimal | None) -> Decimal | None:
    """Return ``amount`` when ``numeric(18, 6)`` stores it exactly, else ``None``.

    A deal amount is user-entered and read by no rule, so an amount of ``10**12`` or more, or
    one finer than six decimal places, is unavailable rather than rounded or a defect that
    would fail the whole CRM sync.
    """
    if amount is None or abs(amount) >= _NUMERIC_LIMIT:
        return None
    if amount != amount.quantize(_NUMERIC_QUANTUM):
        return None
    return amount


def parse_deal(record: HubSpotRecord) -> DealRow:
    """Parse one deal from a target batch read or a deal search page.

    Args:
        record: One raw deal object that requested :data:`DEAL_PROPERTIES`.

    Returns:
        The deal row.

    Raises:
        QueryDefect: When the record lacks a string ``id``, a ``properties`` object,
            ``createdAt`` or ``updatedAt`` instants or a boolean ``archived``, a property it
            reads is neither a string nor ``null``, an instant is malformed, or a text
            property cannot be stored. A non-numeric ``amount`` string is unavailable, not a
            defect.
    """
    crm = _record(record, "deal")
    properties = crm.properties
    created_at = _instant_property(properties, "createdate", "deal")
    return DealRow(
        hs_id=crm.hs_id,
        created_at=crm.created_at if created_at is None else created_at,
        stage=_text_property(properties, "dealstage", "deal"),
        pipeline=_text_property(properties, "pipeline", "deal"),
        amount=_storable_amount(parse_decimal(_property(properties, "amount", "deal"))),
    )


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _required_str(obj: Mapping[str, Any], key: str, surface: str) -> str:
    """Read a required string field of an envelope, checked for text storage."""
    value = _string_field(obj, key, f"'{key}'", surface)
    return _check_storable(value, f"field '{key}'", surface)


def _batch_status(obj: Mapping[str, Any], surface: str) -> str:
    """Read a batch envelope's ``status``: a required string from the contract's enum."""
    status = _required_str(obj, "status", surface)
    if status not in _BATCH_STATES:
        raise _defect(f"field 'status' must be one of {', '.join(_BATCH_STATES)}", surface)
    return status


def _array(obj: Mapping[str, Any], key: str, surface: str, *, required: bool) -> Sequence[object]:
    """Read an array field; an absent optional array is empty."""
    value = obj.get(key)
    if value is None:
        if required:
            raise _defect(f"required field '{key}' is missing", surface)
        return ()
    if not isinstance(value, list | tuple):
        raise _defect(f"field '{key}' must be a JSON array", surface)
    return value


def _records(values: Sequence[object], surface: str) -> tuple[HubSpotRecord, ...]:
    """Validate every element of ``results[]`` as a CRM object (see :func:`_record`)."""
    records: list[HubSpotRecord] = []
    for value in values:
        _record(value, "record", surface)
        records.append(_as_mapping(value, "record", surface))
    return tuple(records)


def _next_after(obj: Mapping[str, Any], what: str, surface: str) -> str | None:
    """Return ``paging.next.after``, or ``None`` when no next page exists.

    ``paging`` is present only when more pages exist; ``paging.next.link`` is deprecated by
    HubSpot and ignored, and ``paging.prev`` is never followed.
    """
    paging = obj.get("paging")
    if paging is None:
        return None
    next_page = _as_mapping(paging, f"field '{what}paging'", surface).get("next")
    if next_page is None:
        return None
    after = _as_mapping(next_page, f"field '{what}paging.next'", surface).get("after")
    return _id_text(after, f"'{what}paging.next.after'", surface)


# ---------------------------------------------------------------------------
# Batch errors and objects batch read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchError:
    """One failed input of a 207 multi-status batch response.

    Attributes:
        category: HubSpot's error category, for example ``OBJECT_NOT_FOUND``.
        sub_category: The string ``subCategory``, for example
            ``crm.associations.NO_ASSOCIATIONS_FOUND``; ``None`` when absent or, as in the
            reference's rendered example, an object.
        message: HubSpot's message with every email removed and secrets redacted
            (``sanitize_message``). Callers still never log it, because it is free text.
        ids: The failed input ids from ``context.ids`` and ``context.fromObjectId``, in order,
            excluding email addresses.
        email_sha256s: The SHA-256 digest of each failed input id that is an email address
            (a contacts batch read with ``idProperty=email``), so the plaintext is never held.
            The digests compare directly with :func:`email_sha256` of the inputs.
    """

    category: str
    sub_category: str | None
    message: str
    ids: tuple[str, ...]
    email_sha256s: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Return ``category`` or ``category/subCategory``, safe to store as a fetch error."""
        if self.sub_category is None:
            return self.category
        return f"{self.category}/{self.sub_category}"


def _context_ids(context: object, surface: str) -> list[str]:
    """Collect the input ids an error's ``context`` names, de-duplicated in order.

    ``context`` is an open map of string arrays whose keys vary by error: ``ids`` for an
    objects batch read, ``fromObjectId`` for an associations batch read. A single string or
    integer is accepted in place of an array; anything else is ignored, because the context
    only attributes the error and carries no data the product stores.
    """
    if not isinstance(context, Mapping):
        return []
    collected: list[str] = []
    for key in ("ids", "fromObjectId"):
        values = context.get(key)
        items = values if isinstance(values, list | tuple) else [values]
        for item in items:
            if item is None:
                continue
            text = _id_text(item, f"'errors[].context.{key}[]'", surface)
            if text not in collected:
                collected.append(text)
    return collected


def _safe_message(message: str, context_ids: Sequence[str]) -> str:
    """Strip every email-shaped context id from ``message``, then sanitise it."""
    for raw in context_ids:
        if "@" in raw:
            message = message.replace(raw, _REDACTED_EMAIL)
    return sanitize_message(message)


def _parse_batch_error(entry: object, surface: str) -> BatchError:
    """Decode one ``errors[]`` entry; ``category``, ``message`` and ``status`` are required."""
    obj = _as_mapping(entry, "errors[] entry", surface)
    category = _required_str(obj, "category", surface).strip()
    if not category:
        raise _defect("field 'errors[].category' must not be blank", surface)
    message = obj.get("message")
    if not isinstance(message, str):
        raise _defect("field 'errors[].message' must be a string", surface)
    _string_field(obj, "status", "'errors[].status'", surface)
    raw_sub = obj.get("subCategory")
    sub_category = None
    if isinstance(raw_sub, str) and raw_sub.strip():
        sub_category = _check_storable(raw_sub.strip(), "field 'errors[].subCategory'", surface)
    context_ids = _context_ids(obj.get("context"), surface)
    ids = tuple(raw for raw in context_ids if "@" not in raw)
    digests: list[str] = []
    for raw in context_ids:
        if "@" in raw:
            digest = _optional_email_sha256(raw, "'errors[].context.ids[]'", surface)
            if digest is not None and digest not in digests:
                digests.append(digest)
    return BatchError(
        category=category,
        sub_category=sub_category,
        message=_safe_message(message, context_ids),
        ids=ids,
        email_sha256s=tuple(digests),
    )


def _check_completed_envelope(obj: Mapping[str, Any], surface: str) -> None:
    """Require a ``COMPLETE`` batch envelope's ``startedAt`` and ``completedAt`` instants."""
    _required_instant(obj, "startedAt", "'startedAt'", surface)
    _required_instant(obj, "completedAt", "'completedAt'", surface)


def _batch_errors(obj: Mapping[str, Any], surface: str) -> tuple[BatchError, ...]:
    """Decode a ``COMPLETE`` envelope's ``errors[]`` and check ``numErrors`` against it.

    ``numErrors`` is optional, but when present it must be a non-negative integer equal to
    the number of ``errors[]`` entries (zero when ``errors`` is absent).
    """
    entries = _array(obj, "errors", surface, required=False)
    errors = tuple(_parse_batch_error(entry, surface) for entry in entries)
    count = obj.get("numErrors")
    if count is not None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _defect("field 'numErrors' must be a non-negative integer", surface)
        if count != len(entries):
            raise _defect("field 'numErrors' must equal the number of 'errors' entries", surface)
    return errors


def _check_records_not_failed(
    records: Sequence[HubSpotRecord], errors: Sequence[BatchError], surface: str
) -> None:
    """Reject a returned record that an error entry also names as a failed input.

    A failed input is absent from ``results`` (AAP 0.2.4), so a record whose ``id`` is in an
    error's context ids, or a contact whose email digests (:func:`contact_emails_sha256`)
    meet an error's :attr:`BatchError.email_sha256s` (a read by ``idProperty=email``), is
    contradictory attribution, never a successful read.
    """
    failed_ids = {input_id for error in errors for input_id in error.ids}
    failed_digests = {digest for error in errors for digest in error.email_sha256s}
    if not failed_ids and not failed_digests:
        return
    for record in records:
        if record["id"] in failed_ids:
            raise _defect("a 'results' record is also named by an 'errors' entry", surface)
        if failed_digests and _contact_digests(record["properties"], surface) & failed_digests:
            raise _defect(
                "a 'results' contact answers to an email an 'errors' entry names", surface
            )


@dataclass(frozen=True, slots=True)
class BatchReadResult:
    """A decoded objects batch read (``POST /crm/objects/2026-09/{type}/batch/read``).

    A ``COMPLETE`` body is validated in full (see :func:`parse_batch_read`). Any other status
    is a failed call, decoded with no records and no errors, because a failed call carries
    no usable data.

    Attributes:
        status: The batch ``status``; only ``COMPLETE`` is a successful call.
        records: The returned raw objects, in response order, each with a string ``id``, a
            ``properties`` object, ``createdAt`` and ``updatedAt`` instants and a boolean
            ``archived``. An input that was not found is absent here and may appear in
            :attr:`errors`; an input listed there is never also here. Empty for a failed call.
        errors: The 207 per-input failures; empty on a 200 and for a failed call.
    """

    status: str
    records: tuple[HubSpotRecord, ...]
    errors: tuple[BatchError, ...]

    @property
    def is_complete(self) -> bool:
        """Whether the call succeeded (``status == "COMPLETE"``)."""
        return self.status == BATCH_COMPLETE


def parse_batch_read(body: object) -> BatchReadResult:
    """Decode an objects batch read body (200, or 207 with ``errors[]``).

    ``status`` is read first. A status other than ``COMPLETE`` is a failed call (AAP 0.2.4):
    it is returned with no records and no errors, and its ``results`` elements are never
    validated, so the reference page's ``CANCELED`` sample decodes as a failed call.

    Args:
        body: The decoded JSON body ``{status, results[], numErrors?, errors?[…], startedAt,
            completedAt}``.

    Returns:
        The status, the validated raw records and the per-input errors.

    Raises:
        QueryDefect: When the body is not an object, ``status`` is missing or outside the
            contract's ``CANCELED``/``COMPLETE``/``PENDING``/``PROCESSING`` enum, or
            ``results`` is missing or not an array. For a ``COMPLETE`` body also when
            ``startedAt`` or ``completedAt`` is not an instant, a record lacks a string
            ``id``, a ``properties`` object, ``createdAt`` or ``updatedAt`` instants or a
            boolean ``archived``, an error entry lacks a string ``status``, ``category`` or
            ``message``, ``numErrors`` differs from the number of error entries, or a
            returned record is also named by an error entry.
    """
    surface = _SURFACE_OBJECTS
    obj = _as_mapping(body, "response body", surface)
    status = _batch_status(obj, surface)
    results = _array(obj, "results", surface, required=True)
    if status != BATCH_COMPLETE:
        # A failed call carries no usable data, so its results are never read as records.
        return BatchReadResult(status=status, records=(), errors=())
    _check_completed_envelope(obj, surface)
    records = _records(results, surface)
    errors = _batch_errors(obj, surface)
    _check_records_not_failed(records, errors, surface)
    return BatchReadResult(status=status, records=records, errors=errors)


# ---------------------------------------------------------------------------
# Associations batch read
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssociationResult:
    """The associations of one input on one page.

    Attributes:
        from_id: ``from.id``, the input record's id.
        targets: Each ``to[].toObjectId`` as a string, de-duplicated in order. Each target's
            ``associationTypes`` is validated (a ``category`` from the contract's enum and an
            integer ``typeId`` per entry) but not kept; labels are not read.
        after: ``paging.next.after`` when this input has more pages, which the client re-sends
            as ``{"id": from_id, "after": after}``; ``None`` once the input is fully read.
    """

    from_id: str
    targets: tuple[str, ...]
    after: str | None


@dataclass(frozen=True, slots=True)
class AssociationBatch:
    """A decoded associations batch read (the ``associations_batch_read`` surface).

    ``POST /crm/associations/2026-09/{fromObjectType}/{toObjectType}/batch/read``.

    Inputs with no associations are reported through :attr:`empty_ids`, a genuine zero
    (``ok_empty``). **Plan correction**: AAP 0.3.3 treats every input listed in a 207
    ``errors[]`` as ``error``, but HubSpot reports an input with no associations exactly as
    such an entry (``category`` ``OBJECT_NOT_FOUND``, ``subCategory``
    :data:`NO_ASSOCIATIONS_FOUND`, the input in ``context.fromObjectId``). Reading it as an
    error would make every contact without meetings or deals "unknown" and withhold W2 and
    C1b permanently, so only that subcategory (or a category of ``NO_ASSOCIATIONS_FOUND``) is
    empty; every other entry is an error.

    A ``COMPLETE`` body is validated in full (see :func:`parse_association_batch`). Any other
    status is a failed call, decoded with no results, no empty inputs and no failed inputs,
    so the caller settles none of its inputs from it.

    Attributes:
        status: The batch ``status``; only ``COMPLETE`` is a successful call.
        results: Each succeeded input's result, keyed by ``from.id``. A failed input is absent
            from ``results`` (AAP 0.2.4), so no input here is named by any error entry.
        empty_ids: Inputs with no associations: a ``NO_ASSOCIATIONS_FOUND`` error entry, or a
            result whose ``to`` is empty and that has no further page.
        error_ids: Every other failed input, mapped to its error ``category`` or
            ``category/subCategory``.
    """

    status: str
    results: Mapping[str, AssociationResult]
    empty_ids: frozenset[str]
    error_ids: Mapping[str, str]

    @property
    def is_complete(self) -> bool:
        """Whether the call succeeded (``status == "COMPLETE"``)."""
        return self.status == BATCH_COMPLETE


def _is_no_associations(error: BatchError) -> bool:
    """Whether an error entry is HubSpot's "no associations" report for its inputs."""
    for value in (error.sub_category, error.category):
        if value is not None and value.rsplit(".", 1)[-1] == _NO_ASSOCIATIONS_SEGMENT:
            return True
    return False


def _check_association_types(target: Mapping[str, Any], surface: str) -> None:
    """Validate one target's ``associationTypes``: a required array of type specs.

    Each spec needs a ``category`` from the contract's enum and an integer ``typeId``. The
    types are not kept, because any association to the target counts, but a target without
    them breaks the contract and is never read as an association.
    """
    what = "'results[].to[].associationTypes'"
    specs = target.get("associationTypes")
    if specs is None:
        raise _defect(f"required field {what} is missing", surface)
    if not isinstance(specs, list | tuple):
        raise _defect(f"field {what} must be a JSON array", surface)
    for spec in specs:
        spec_obj = _as_mapping(spec, "results[].to[].associationTypes[] entry", surface)
        category_what = "'results[].to[].associationTypes[].category'"
        category = _string_field(spec_obj, "category", category_what, surface)
        if category not in _ASSOCIATION_CATEGORIES:
            allowed = ", ".join(_ASSOCIATION_CATEGORIES)
            raise _defect(f"field {category_what} must be one of {allowed}", surface)
        type_id = spec_obj.get("typeId")
        if type_id is None:
            raise _defect(
                "required field 'results[].to[].associationTypes[].typeId' is missing", surface
            )
        if isinstance(type_id, bool) or not isinstance(type_id, int):
            raise _defect(
                "field 'results[].to[].associationTypes[].typeId' must be an integer", surface
            )


def _parse_association_result(item: object, surface: str) -> AssociationResult:
    """Decode one ``results[]`` element ``{from{id}, to[{toObjectId, …}], paging?}``."""
    obj = _as_mapping(item, "results[] entry", surface)
    source = obj.get("from")
    if source is None:
        raise _defect("required field 'results[].from' is missing", surface)
    from_id = _id_text(
        _as_mapping(source, "field 'results[].from'", surface).get("id"),
        "'results[].from.id'",
        surface,
    )
    targets: list[str] = []
    for target in _array(obj, "to", surface, required=True):
        target_obj = _as_mapping(target, "results[].to[] entry", surface)
        target_id = _id_text(target_obj.get("toObjectId"), "'results[].to[].toObjectId'", surface)
        _check_association_types(target_obj, surface)
        if target_id not in targets:
            targets.append(target_id)
    return AssociationResult(
        from_id=from_id,
        targets=tuple(targets),
        after=_next_after(obj, "results[].", surface),
    )


def parse_association_batch(body: object) -> AssociationBatch:
    """Decode an associations batch read body (200, or 207 with ``errors[]``).

    ``status`` is read first. A status other than ``COMPLETE`` is a failed call (AAP 0.2.4):
    it is returned with no results, empty inputs or failed inputs, and its ``results``
    elements are never validated, so the reference page's ``CANCELED`` sample decodes as a
    failed call.

    Args:
        body: The decoded JSON body ``{status, results[], errors?[…], numErrors?, startedAt,
            completedAt}``.

    Returns:
        The per-input results, the inputs with no associations and the failed inputs.

    Raises:
        QueryDefect: When the body is not an object, ``status`` is missing or outside the
            contract's ``CANCELED``/``COMPLETE``/``PENDING``/``PROCESSING`` enum, or
            ``results`` is missing or not an array. For a ``COMPLETE`` body also when
            ``startedAt`` or ``completedAt`` is not an instant, a result lacks ``from.id`` or
            ``to``, a target lacks ``toObjectId`` or valid ``associationTypes``, a
            ``from.id`` repeats, an error entry lacks a string ``status``, ``category`` or
            ``message``, ``numErrors`` differs from the number of error entries, or an input
            in ``results`` is also named by an error entry (``NO_ASSOCIATIONS_FOUND``
            included).
    """
    surface = _SURFACE_ASSOCIATIONS
    obj = _as_mapping(body, "response body", surface)
    status = _batch_status(obj, surface)
    items = _array(obj, "results", surface, required=True)
    if status != BATCH_COMPLETE:
        # A failed call carries no usable data, so no input is settled by it.
        return AssociationBatch(
            status=status,
            results=MappingProxyType({}),
            empty_ids=frozenset(),
            error_ids=MappingProxyType({}),
        )
    _check_completed_envelope(obj, surface)
    results: dict[str, AssociationResult] = {}
    for item in items:
        result = _parse_association_result(item, surface)
        if result.from_id in results:
            raise _defect("field 'results[].from.id' repeats an input", surface)
        results[result.from_id] = result
    empty: set[str] = {
        from_id
        for from_id, result in results.items()
        if not result.targets and result.after is None
    }
    error_ids: dict[str, str] = {}
    for error in _batch_errors(obj, surface):
        if any(input_id in results for input_id in error.ids):
            # A failed input is absent from results, so one in both is contradictory.
            raise _defect("an input in 'results' is also named by an 'errors' entry", surface)
        if _is_no_associations(error):
            empty.update(error.ids)
            continue
        for input_id in error.ids:
            error_ids.setdefault(input_id, error.label)
    return AssociationBatch(
        status=status,
        results=MappingProxyType(results),
        empty_ids=frozenset(empty),
        error_ids=MappingProxyType(error_ids),
    )


# ---------------------------------------------------------------------------
# CRM Search
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of a CRM Search (``POST /crm/objects/2026-09/{type}/search``).

    Attributes:
        results: The raw objects on this page, in response order, each with a string ``id``,
            a ``properties`` object, ``createdAt`` and ``updatedAt`` instants and a boolean
            ``archived``.
        total: Every match of the query, across all pages. ``search.py`` splits a partition
            whose ``total`` exceeds the 10,000-result ceiling.
        after: ``paging.next.after``, sent as ``after`` for the next page; ``None`` on the
            last page.
    """

    results: tuple[HubSpotRecord, ...]
    total: int
    after: str | None


def parse_search_page(body: object) -> SearchPage:
    """Decode a CRM Search response body.

    Args:
        body: The decoded JSON body ``{total, results[], paging?{next{after}}}``.

    Returns:
        The page's raw records, the query's total and the next cursor.

    Raises:
        QueryDefect: When the body is not an object, ``total`` is missing or not a
            non-negative integer, ``results`` is missing or not an array, a record lacks a
            string ``id``, a ``properties`` object, ``createdAt`` or ``updatedAt`` instants or
            a boolean ``archived``, or ``paging.next.after`` is malformed.
    """
    surface = _SURFACE_SEARCH
    obj = _as_mapping(body, "response body", surface)
    total = obj.get("total")
    if total is None:
        raise _defect("required field 'total' is missing", surface)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise _defect("field 'total' must be a non-negative integer", surface)
    return SearchPage(
        results=_records(_array(obj, "results", surface, required=True), surface),
        total=total,
        after=_next_after(obj, "", surface),
    )


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PropertyOption:
    """One option of an enumerated property, such as a ``lifecyclestage`` stage.

    Attributes:
        label: The human-readable label configured stage labels are matched against.
        value: The internal value; custom lifecycle stages use numeric strings such as
            ``"1406943076"``, the suffix of ``hs_v2_date_entered_{value}``.
        hidden: Whether HubSpot hides the option from its UI, as the required ``hidden``
            field states it.
    """

    label: str
    value: str
    hidden: bool


@dataclass(frozen=True, slots=True)
class PropertyDefinition:
    """A property definition (``GET /crm/properties/2026-09/{objectType}/{propertyName}``).

    Attributes:
        name: The internal property name.
        type: The data type, for example ``enumeration``, ``datetime``, ``date`` or ``string``.
        field_type: How HubSpot renders it (``select``, ``date``, …), stripped; ``None`` when
            the required ``fieldType`` string is blank.
        options: The options of an enumerated property, in response order; empty, as the
            required ``options`` array is, for other property types.
    """

    name: str
    type: str
    field_type: str | None
    options: tuple[PropertyOption, ...]

    @property
    def is_datetime(self) -> bool:
        """Whether the property holds a date or an instant (``type`` ``datetime`` or ``date``).

        A property-mode MQL or QL binding must be one.
        """
        return self.type in {"datetime", "date"}

    @property
    def is_instant(self) -> bool:
        """Whether the property holds an instant, a date with a time (``type`` ``datetime``).

        The Google touch-date property must be one (AAP 0.3.3): a ``date`` property holds a
        day without a time, which reads as midnight UTC and would anchor the causal check
        before a touch later that day.
        """
        return self.type == "datetime"


def _parse_option(item: object, surface: str) -> PropertyOption:
    """Decode one ``options[]`` element; ``label``, ``value`` and ``hidden`` are required."""
    obj = _as_mapping(item, "options[] entry", surface)
    label = _required_str(obj, "label", surface)
    value = _string_id(obj.get("value"), "'options[].value'", surface)
    hidden = obj.get("hidden")
    if hidden is None:
        raise _defect("required field 'options[].hidden' is missing", surface)
    if not isinstance(hidden, bool):
        raise _defect("field 'options[].hidden' must be a boolean", surface)
    return PropertyOption(label=label, value=value, hidden=hidden)


def _required_name(obj: Mapping[str, Any], key: str, surface: str) -> str:
    """Read a required, non-blank string field of a property definition."""
    value = _required_str(obj, key, surface)
    if not value.strip():
        raise _defect(f"field '{key}' must not be blank", surface)
    return value


def parse_property(body: object) -> PropertyDefinition:
    """Decode a property definition, used by startup binding verification (AAP 0.3.3).

    Every field the properties contract requires must be present: ``name`` and ``type`` as
    non-blank strings; ``fieldType``, ``description`` (possibly empty), ``groupName`` and
    ``label`` as strings; and ``options`` as an array whose every entry has a string
    ``label``, a non-blank string ``value`` and a boolean ``hidden``. Nothing is defaulted.

    Args:
        body: The decoded JSON body of a 200 response. A 404 (no such property) is handled by
            the client before this is called.

    Returns:
        The definition's name, type, field type and options.

    Raises:
        QueryDefect: When the body is not an object, a required field is missing or of the
            wrong type, ``name``, ``type`` or an option's ``value`` is blank, or a field read
            for binding verification cannot be stored as text.
    """
    surface = _SURFACE_PROPERTIES
    obj = _as_mapping(body, "response body", surface)
    name = _required_name(obj, "name", surface)
    data_type = _required_name(obj, "type", surface)
    field_type = _string_field(obj, "fieldType", "'fieldType'", surface)
    for key in ("description", "groupName", "label"):
        _string_field(obj, key, f"'{key}'", surface)
    return PropertyDefinition(
        name=name,
        type=data_type,
        field_type=_text(field_type, what="field 'fieldType'", surface=surface),
        options=tuple(
            _parse_option(item, surface) for item in _array(obj, "options", surface, required=True)
        ),
    )
