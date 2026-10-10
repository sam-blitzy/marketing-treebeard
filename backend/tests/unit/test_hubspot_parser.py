"""HubSpot decoding on verified contracts: ``ads_engine.connectors.hubspot.parser``.

Every payload here is traceable to ``backend/tests/contracts/hubspot/*.json`` through the same
contract machinery the simulator uses (AAP 0.11.2): contracts come from the root ``contract``
fixture (``simulation.payloads.load_contract``), and every hand-built body passes
``payloads.validate_response`` for its surface and HTTP status before the parser sees it, so a
fixture can never carry a field its contract does not define. Only the deliberately malformed
bodies of the defect tests skip that check.

Each contract's documented sample runs through its decoder verbatim. The associations sample
is the "Retrieve associations" reference page's rendered example (last modified 2026-09-09);
its ``CANCELED`` status is reported as parsed, never coerced to ``COMPLETE``, because the
client treats every other status as a failed call. Redacted live captures from
``ads-engine capture-fixtures`` are decoded too, once credentials have produced any.

A 207 ``errors[]`` entry whose ``subCategory`` is ``crm.associations.NO_ASSOCIATIONS_FOUND`` is
a genuine zero (``ok_empty``, :attr:`AssociationBatch.empty_ids`); every other entry fails its
input (:attr:`AssociationBatch.error_ids`). That is the parser's documented correction of the
plan, which counted every 207 entry as an error.

Ids are 16-digit fake strings and property values are strings or ``null``, as the contracts
state. Emails are ``example.com`` addresses held in memory only. No database, no network.
"""

import copy
import dataclasses
import hashlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

import pytest

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.hubspot.parser import (
    BATCH_COMPLETE,
    DEAL_PROPERTIES,
    MEETING_PROPERTIES,
    NO_ASSOCIATIONS_FOUND,
    AssociationBatch,
    AssociationResult,
    BatchError,
    BatchReadResult,
    ContactPropertyMap,
    ContactRow,
    DealRow,
    HubSpotRecord,
    MeetingRow,
    PropertyDefinition,
    PropertyOption,
    SearchPage,
    email_sha256,
    normalize_email,
    parse_association_batch,
    parse_batch_read,
    parse_contact,
    parse_deal,
    parse_decimal,
    parse_instant,
    parse_meeting,
    parse_property,
    parse_search_page,
)
from ads_engine.simulation.payloads import (
    SURFACES,
    Contract,
    validate_capture,
    validate_documented_sample,
    validate_response,
)

pytestmark = pytest.mark.contract

ContractLoader = Callable[..., Contract]
"""The root ``contract`` fixture: ``contract(source, name) -> Contract``."""

HUBSPOT: Final = "hubspot"

OBJECTS: Final = "objects_batch_read"
SEARCH: Final = "crm_search"
ASSOCIATIONS: Final = "associations_batch_read"
PROPERTIES: Final = "properties"

DECODERS: Final[Mapping[str, Callable[[object], object]]] = MappingProxyType(
    {
        OBJECTS: parse_batch_read,
        SEARCH: parse_search_page,
        ASSOCIATIONS: parse_association_batch,
        PROPERTIES: parse_property,
    }
)
"""The decoder of each HubSpot contract surface."""

DECODED_TYPES: Final[Mapping[str, type]] = MappingProxyType(
    {
        OBJECTS: BatchReadResult,
        SEARCH: SearchPage,
        ASSOCIATIONS: AssociationBatch,
        PROPERTIES: PropertyDefinition,
    }
)
"""What each surface's decoder returns."""

# Clearly fake HubSpot ids: 16 digits, strings as the contracts type them.
CONTACT_ID: Final = "9900000000000001"
OTHER_CONTACT_ID: Final = "9900000000000002"
THIRD_CONTACT_ID: Final = "9900000000000003"
FOURTH_CONTACT_ID: Final = "9900000000000004"
FIFTH_CONTACT_ID: Final = "9900000000000005"
MEETING_ID: Final = "9910000000000001"
OTHER_MEETING_ID: Final = "9910000000000002"
DEAL_ID: Final = "9920000000000001"

# A custom lifecycle stage id, as the properties API returns it for "First meeting booked".
CUSTOM_STAGE_ID: Final = "1406943076"
MQL_PROPERTY: Final = f"hs_v2_date_entered_{CUSTOM_STAGE_ID}"
QL_PROPERTY: Final = "hs_v2_date_entered_salesqualifiedlead"
GOOGLE_CAMPAIGN_PROPERTY: Final = "google_ads_campaign_id"
GOOGLE_TOUCH_PROPERTY: Final = "google_ads_touch_at"

RAW_EMAIL: Final = "  Alice@Example.COM "
NORMALISED_EMAIL: Final = "alice@example.com"

STARTED_AT: Final = "2026-09-20T10:00:00.000Z"
COMPLETED_AT: Final = "2026-09-20T10:00:00.250Z"
RECORD_CREATED_AT: Final = "2026-09-01T09:00:00.000Z"
RECORD_UPDATED_AT: Final = "2026-09-18T16:45:00.000Z"

# The HubSpot-defined contact-to-meeting association type.
CONTACT_TO_MEETING_TYPE_ID: Final = 200


def utc(*parts: int) -> datetime:
    """Build a timezone-aware UTC instant from ``year, month, day[, hour, minute, …]``."""
    return datetime(*parts, tzinfo=UTC)


# ---------------------------------------------------------------------------------------------
# Body builders: every builder output is checked against its contract before it is parsed
# ---------------------------------------------------------------------------------------------


def checked(
    contract: ContractLoader, surface: str, body: dict[str, Any], *, status: int = 200
) -> dict[str, Any]:
    """Validate a hand-built body against its HubSpot contract and return it unchanged.

    Raises:
        ContractViolation: When the status is not one the contract lists or the body carries
            a field the contract does not define, lacks a required one or has a wrong type.
    """
    validate_response(contract(HUBSPOT, surface), body, status=status)
    return body


def crm_record(
    hs_id: str,
    properties: Mapping[str, str | None],
    *,
    created_at: str = RECORD_CREATED_AT,
    updated_at: str = RECORD_UPDATED_AT,
) -> dict[str, Any]:
    """One CRM object with every field the objects and search contracts require."""
    return {
        "id": hs_id,
        "properties": dict(properties),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "archived": False,
    }


def batch_envelope(
    results: list[dict[str, Any]],
    *,
    errors: list[dict[str, Any]] | None = None,
    status: str = BATCH_COMPLETE,
) -> dict[str, Any]:
    """A batch read envelope (objects or associations); ``errors`` makes it a 207 body."""
    body: dict[str, Any] = {
        "status": status,
        "results": results,
        "startedAt": STARTED_AT,
        "completedAt": COMPLETED_AT,
    }
    if errors is not None:
        body["errors"] = errors
        body["numErrors"] = len(errors)
    return body


def error_entry(
    category: str,
    message: str,
    context: Mapping[str, list[str]],
    *,
    sub_category: str | None = None,
) -> dict[str, Any]:
    """One 207 ``errors[]`` entry with the required ``status``, ``category`` and ``message``."""
    entry: dict[str, Any] = {
        "status": "error",
        "category": category,
        "message": message,
        "context": {key: list(values) for key, values in context.items()},
    }
    if sub_category is not None:
        entry["subCategory"] = sub_category
    return entry


def association(
    from_id: str, targets: list[str | int], *, after: str | None = None
) -> dict[str, Any]:
    """One associations ``results[]`` element; ``after`` adds this input's next-page cursor."""
    item: dict[str, Any] = {
        "from": {"id": from_id},
        "to": [
            {
                "toObjectId": target,
                "associationTypes": [
                    {
                        "category": "HUBSPOT_DEFINED",
                        "typeId": CONTACT_TO_MEETING_TYPE_ID,
                        "label": None,
                    }
                ],
            }
            for target in targets
        ],
    }
    if after is not None:
        item["paging"] = {"next": {"after": after}}
    return item


def read_records(
    contract: ContractLoader, records: list[dict[str, Any]]
) -> tuple[HubSpotRecord, ...]:
    """Pass records through a contract-checked 200 objects batch read and return them parsed.

    Every record a row decoder receives in these tests therefore satisfies the objects batch
    read contract and the parser's own envelope validation first.
    """
    body = checked(contract, OBJECTS, batch_envelope(records))
    result = parse_batch_read(body)
    assert result.is_complete
    assert len(result.records) == len(records)
    return result.records


def read_one(contract: ContractLoader, record: dict[str, Any]) -> HubSpotRecord:
    """Pass one record through :func:`read_records`."""
    (parsed,) = read_records(contract, [record])
    return parsed


def walk_strings(value: object) -> Iterator[str]:
    """Yield every string nested anywhere in ``value`` (mappings, sequences and scalars)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from walk_strings(key)
            yield from walk_strings(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from walk_strings(item)


# ---------------------------------------------------------------------------------------------
# Phase 1: documented samples and live captures, verbatim
# ---------------------------------------------------------------------------------------------


def test_every_hubspot_contract_surface_has_a_decoder() -> None:
    """The decoders cover exactly the HubSpot contract surfaces: none missing, none extra."""
    assert set(DECODERS) == set(SURFACES[HUBSPOT])


@pytest.mark.parametrize("surface", sorted(DECODERS))
def test_contract_is_traceable_to_its_2026_09_reference_page(
    contract: ContractLoader, surface: str
) -> None:
    """Each contract cites its dated HubSpot reference page and the ``2026-09`` version and path."""
    loaded = contract(HUBSPOT, surface)

    assert loaded.surface == f"{HUBSPOT}.{surface}"
    assert loaded.api_version == "2026-09"
    assert loaded.reference_url.startswith(
        "https://developers.hubspot.com/docs/api-reference/latest/crm/"
    )
    assert loaded.last_modified is not None
    assert "/2026-09/" in loaded.request.url


@pytest.mark.parametrize("surface", sorted(DECODERS))
def test_documented_sample_conforms_and_decodes_verbatim(
    contract: ContractLoader, surface: str
) -> None:
    """Each documented sample conforms and decodes verbatim, unmutated, to its surface's type."""
    loaded = contract(HUBSPOT, surface)
    validate_documented_sample(loaded)
    sample = loaded.response.documented_sample
    if sample is None:
        # The page publishes no sample: nothing verbatim to decode for this surface.
        return
    pristine = copy.deepcopy(sample)

    decoded = DECODERS[surface](sample)

    assert isinstance(decoded, DECODED_TYPES[surface])
    assert sample == pristine, "decoding must never mutate the documented sample"


def test_associations_reference_sample_reports_its_canceled_status_as_parsed(
    contract: ContractLoader,
) -> None:
    """The associations sample's ``CANCELED`` status is kept as parsed, so it settles no input."""
    loaded = contract(HUBSPOT, ASSOCIATIONS)
    assert loaded.reference_url.endswith(
        "/crm/associations/associate-records/batch/get-associations"
    )
    assert loaded.last_modified == "2026-09-09"
    sample = copy.deepcopy(loaded.response.documented_sample)
    assert isinstance(sample, dict)
    assert sample["status"] == "CANCELED"

    batch = parse_association_batch(sample)

    # Reported as parsed, never coerced: the client treats a non-COMPLETE status as a failed
    # call, so the sample settles none of its inputs.
    assert batch.status == "CANCELED"
    assert batch.status != BATCH_COMPLETE
    assert not batch.is_complete
    assert dict(batch.results) == {}
    assert batch.empty_ids == frozenset()
    assert dict(batch.error_ids) == {}


def test_objects_reference_sample_is_a_failed_call_with_no_usable_data(
    contract: ContractLoader,
) -> None:
    """The objects batch read sample is ``CANCELED``: incomplete, with no records or errors."""
    sample = copy.deepcopy(contract(HUBSPOT, OBJECTS).response.documented_sample)
    assert isinstance(sample, dict)

    result = parse_batch_read(sample)

    assert result.status == sample["status"] == "CANCELED"
    assert not result.is_complete
    assert result.records == ()
    assert result.errors == ()


def test_crm_search_reference_sample_decodes_into_a_page_of_contacts(
    contract: ContractLoader,
) -> None:
    """The CRM Search sample is a last page of two contacts whose unbound fields are ``None``."""
    sample = copy.deepcopy(contract(HUBSPOT, SEARCH).response.documented_sample)

    page = parse_search_page(sample)

    assert page.total == 2
    assert [record["id"] for record in page.results] == ["100451", "57156923994"]
    assert page.after is None
    rows = [parse_contact(record, ContactPropertyMap()) for record in page.results]
    assert [row.hs_id for row in rows] == ["100451", "57156923994"]
    assert rows[0].email_sha256 == email_sha256("testperson@hubspot.com")
    assert rows[1].email_sha256 == email_sha256("emailmaria@hubspot.com")
    assert rows[0].updated_at == utc(2024, 9, 11, 13, 27, 39, 356000)
    # No binding is configured, so every bound metric is unavailable rather than zero.
    assert all(row.mql_at is None and row.ql_at is None for row in rows)
    assert all(row.google_campaign_ref is None and row.google_touch_at is None for row in rows)


def test_properties_reference_sample_decodes_every_required_field(
    contract: ContractLoader,
) -> None:
    """The properties sample decodes its placeholder fields and option; it is not a datetime."""
    sample = copy.deepcopy(contract(HUBSPOT, PROPERTIES).response.documented_sample)

    definition = parse_property(sample)

    assert definition.name == "<string>"
    assert definition.type == "<string>"
    assert definition.field_type == "<string>"
    assert definition.options == (PropertyOption(label="<string>", value="<string>", hidden=True),)
    assert not definition.is_datetime
    assert not definition.is_instant


def test_captured_live_payloads_decode_with_their_surface_decoder(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
) -> None:
    """Every redacted 2xx live capture decodes to its surface's type; error captures are skipped."""
    # Zero captures is the normal state until `ads-engine capture-fixtures` has run with
    # working credentials; every capture present is decoded.
    for capture in captured_payloads(HUBSPOT):
        loaded = validate_capture(capture)
        assert loaded.source == HUBSPOT
        response = capture["response"]
        status = response["status"]
        if not 200 <= status < 300:
            # Error bodies have no success schema and no decoder (a 404 property is handled
            # by the client before parsing).
            continue
        decoded = DECODERS[loaded.name](response["body"])
        assert isinstance(decoded, DECODED_TYPES[loaded.name])


# ---------------------------------------------------------------------------------------------
# Phase 2: primitives
# ---------------------------------------------------------------------------------------------


def test_normalize_email_trims_and_lowercases() -> None:
    """An email is trimmed of surrounding whitespace and lowercased."""
    assert normalize_email(RAW_EMAIL) == NORMALISED_EMAIL


def test_email_sha256_is_the_digest_of_the_normalised_utf8_address() -> None:
    """The digest is the hex SHA-256 of the normalised address, for raw or normalised input."""
    expected = hashlib.sha256(b"alice@example.com").hexdigest()

    assert email_sha256(NORMALISED_EMAIL) == expected
    assert email_sha256(RAW_EMAIL) == expected
    assert len(expected) == 64


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("2026-09-04T12:00:00Z", utc(2026, 9, 4, 12), id="zulu"),
        pytest.param("2026-09-04T14:00:00+02:00", utc(2026, 9, 4, 12), id="offset-same-instant"),
        pytest.param("1756987200000", utc(2025, 9, 4, 12), id="epoch-milliseconds-string"),
        pytest.param("2026-09-04", utc(2026, 9, 4), id="date-only-midnight-utc"),
    ],
)
def test_parse_instant_returns_timezone_aware_utc(value: str, expected: datetime) -> None:
    """Zulu, offset, epoch-millisecond and date-only values all parse to an aware UTC instant."""
    parsed = parse_instant(value)

    assert parsed == expected
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.utcoffset() is not None


def test_parse_instant_offset_and_zulu_forms_are_the_same_instant() -> None:
    """A ``+02:00`` offset and its Zulu equivalent parse to the same instant."""
    assert parse_instant("2026-09-04T12:00:00Z") == parse_instant("2026-09-04T14:00:00+02:00")


@pytest.mark.parametrize("value", ["", None], ids=["empty", "null"])
def test_parse_instant_empty_is_none(value: str | None) -> None:
    """An empty or ``null`` instant parses to ``None``."""
    assert parse_instant(value) is None


@pytest.mark.parametrize(
    "value",
    ["next tuesday", "2026-13-45", "2026-09-04T25:00:00Z", True, 12.5],
    ids=["text", "bad-date", "bad-time", "bool", "float"],
)
def test_parse_instant_rejects_anything_else_as_a_defect(value: object) -> None:
    """Any other value is a HubSpot validation defect naming the field, never the string value."""
    with pytest.raises(QueryDefect) as caught:
        parse_instant(value, what="contact property 'hs_v2_date_entered_x'")

    assert caught.value.source == HUBSPOT
    assert caught.value.code == "validation"
    assert "hs_v2_date_entered_x" in caught.value.message
    if isinstance(value, str):
        assert value not in caught.value.message


def test_parse_decimal_is_exact() -> None:
    """A decimal string parses to an exact ``Decimal`` that keeps its scale, never a float."""
    parsed = parse_decimal("1250.50")

    assert parsed == Decimal("1250.50")
    assert isinstance(parsed, Decimal)
    # Exact, not a binary float: the trailing zero and scale survive.
    assert str(parsed) == "1250.50"


@pytest.mark.parametrize("value", ["", None], ids=["empty", "null"])
def test_parse_decimal_empty_is_none(value: str | None) -> None:
    """An empty or ``null`` decimal parses to ``None``."""
    assert parse_decimal(value) is None


# ---------------------------------------------------------------------------------------------
# Phase 3: contact, meeting and deal rows
# ---------------------------------------------------------------------------------------------

FULL_BINDINGS: Final = ContactPropertyMap(
    mql_property=MQL_PROPERTY,
    ql_property=QL_PROPERTY,
    google_campaign_property=GOOGLE_CAMPAIGN_PROPERTY,
    google_touch_property=GOOGLE_TOUCH_PROPERTY,
)


def contact_properties(**overrides: str | None) -> dict[str, str | None]:
    """A contact's properties with every bound property set; ``overrides`` replace values."""
    properties: dict[str, str | None] = {
        "email": RAW_EMAIL,
        "lifecyclestage": "salesqualifiedlead",
        "lastmodifieddate": RECORD_UPDATED_AT,
        MQL_PROPERTY: "2026-09-04T12:00:00.000Z",
        QL_PROPERTY: "2026-09-10T08:30:00.000Z",
        GOOGLE_CAMPAIGN_PROPERTY: "21000000001",
        GOOGLE_TOUCH_PROPERTY: "2026-09-01T10:15:00.000Z",
    }
    properties.update(overrides)
    return properties


def test_parse_contact_reads_each_bound_property(contract: ContractLoader) -> None:
    """With every binding resolved, each bound property is read into its ``ContactRow`` field."""
    record = read_one(contract, crm_record(CONTACT_ID, contact_properties()))

    row = parse_contact(record, FULL_BINDINGS)

    assert row == ContactRow(
        hs_id=CONTACT_ID,
        email_sha256=hashlib.sha256(b"alice@example.com").hexdigest(),
        lifecycle_stage="salesqualifiedlead",
        mql_at=utc(2026, 9, 4, 12),
        ql_at=utc(2026, 9, 10, 8, 30),
        google_campaign_ref="21000000001",
        google_touch_at=utc(2026, 9, 1, 10, 15),
        updated_at=utc(2026, 9, 18, 16, 45),
    )


@pytest.mark.parametrize("empty", ["", None], ids=["empty-string", "null"])
def test_parse_contact_empty_bound_date_is_a_skipped_stage(
    contract: ContractLoader, empty: str | None
) -> None:
    """An empty or ``null`` MQL date leaves ``mql_at`` ``None``; the QL date and stage are kept."""
    # The contact is in a later stage now, but never entered the MQL stage: it is not counted,
    # and neither the current stage nor the QL date stands in for the missing MQL date.
    properties = contact_properties(**{MQL_PROPERTY: empty})
    record = read_one(contract, crm_record(CONTACT_ID, properties))

    row = parse_contact(record, FULL_BINDINGS)

    assert row.mql_at is None
    assert row.ql_at == utc(2026, 9, 10, 8, 30)
    assert row.lifecycle_stage == "salesqualifiedlead"


def test_parse_contact_unavailable_binding_leaves_its_field_none(
    contract: ContractLoader,
) -> None:
    """An unresolved binding leaves its field ``None`` even when the record holds the property."""
    # The record carries every property, but the MQL stage and the Google link did not resolve
    # against the portal, so their fields are unavailable whatever the record holds.
    bindings = ContactPropertyMap(mql_property=None, ql_property=QL_PROPERTY)
    record = read_one(contract, crm_record(CONTACT_ID, contact_properties()))

    row = parse_contact(record, bindings)

    assert row.mql_at is None
    assert row.ql_at == utc(2026, 9, 10, 8, 30)
    assert row.google_campaign_ref is None
    assert row.google_touch_at is None


def test_parse_contact_keeps_no_plaintext_email(contract: ContractLoader) -> None:
    """No field, ``repr`` or ``str`` of a contact row shows the address, only its digest."""
    record = read_one(contract, crm_record(CONTACT_ID, contact_properties()))

    row = parse_contact(record, FULL_BINDINGS)

    assert row.email_sha256 == email_sha256(NORMALISED_EMAIL)
    texts = [*walk_strings(dataclasses.asdict(row)), repr(row), str(row)]
    for text in texts:
        assert "@" not in text
        assert "alice" not in text.lower()


def test_request_properties_cover_every_bound_property(contract: ContractLoader) -> None:
    """Each bound property is requested once, and the requested ones alone yield the same row."""
    requested = FULL_BINDINGS.request_properties()

    for name in (
        "email",
        "lifecyclestage",
        MQL_PROPERTY,
        QL_PROPERTY,
        GOOGLE_CAMPAIGN_PROPERTY,
        GOOGLE_TOUCH_PROPERTY,
    ):
        assert name in requested
    assert len(requested) == len(set(requested))
    # A record holding only the requested properties yields every field the row needs.
    full = contact_properties()
    record = read_one(
        contract, crm_record(CONTACT_ID, {name: full[name] for name in requested if name in full})
    )
    assert parse_contact(record, FULL_BINDINGS) == parse_contact(
        read_one(contract, crm_record(CONTACT_ID, full)), FULL_BINDINGS
    )


def test_request_properties_omit_unavailable_bindings() -> None:
    """Only resolved bindings are requested, beside ``email`` and ``lifecyclestage``."""
    bindings = ContactPropertyMap(mql_property=MQL_PROPERTY)

    requested = bindings.request_properties()

    assert "email" in requested
    assert "lifecyclestage" in requested
    assert MQL_PROPERTY in requested
    for unavailable in (QL_PROPERTY, GOOGLE_CAMPAIGN_PROPERTY, GOOGLE_TOUCH_PROPERTY):
        assert unavailable not in requested
    stage_dates = [name for name in requested if name.startswith("hs_v2_date_entered_")]
    assert stage_dates == [MQL_PROPERTY]


def test_request_properties_de_duplicate_a_property_bound_twice() -> None:
    """A property bound to both MQL and QL is requested once."""
    # A property-mode binding may name the same datetime property for MQL and QL.
    bindings = ContactPropertyMap(mql_property=QL_PROPERTY, ql_property=QL_PROPERTY)

    requested = bindings.request_properties()

    assert requested.count(QL_PROPERTY) == 1


def meeting_properties(**overrides: str | None) -> dict[str, str | None]:
    """A meeting's requested properties, each a distinct value; ``overrides`` replace values."""
    properties: dict[str, str | None] = {
        "hs_createdate": "2026-09-05T09:00:00.000Z",
        "hs_timestamp": "2026-09-12T15:00:00.000Z",
        "hs_meeting_start_time": "2026-09-12T15:00:00.000Z",
        "hs_meeting_outcome": "COMPLETED",
        "hs_meeting_source": "MEETINGS_PUBLIC",
    }
    properties.update(overrides)
    return properties


def test_parse_meeting_creation_comes_from_hs_createdate(contract: ContractLoader) -> None:
    """A meeting's creation is ``hs_createdate``, not ``createdAt`` or ``hs_timestamp``."""
    properties = meeting_properties(hs_timestamp="2026-09-13T11:00:00.000Z")
    record = read_one(
        contract, crm_record(MEETING_ID, properties, created_at="2026-09-05T09:00:07.000Z")
    )

    row = parse_meeting(record)

    assert row == MeetingRow(
        hs_id=MEETING_ID,
        created_at=utc(2026, 9, 5, 9),
        start_at=utc(2026, 9, 12, 15),
        meeting_timestamp=utc(2026, 9, 13, 11),
        outcome="COMPLETED",
        meeting_source="MEETINGS_PUBLIC",
    )
    # The booking proxy is the record's creation, never the meeting's own timestamp.
    assert row.created_at != row.meeting_timestamp


def test_meeting_properties_list_exactly_the_properties_read(contract: ContractLoader) -> None:
    """``MEETING_PROPERTIES`` lists, once each, exactly the properties ``parse_meeting`` reads."""
    assert len(MEETING_PROPERTIES) == len(set(MEETING_PROPERTIES))
    assert set(MEETING_PROPERTIES) == set(meeting_properties())
    # A record holding nothing but MEETING_PROPERTIES fills every field of the row.
    row = parse_meeting(read_one(contract, crm_record(MEETING_ID, meeting_properties())))
    assert all(getattr(row, field.name) is not None for field in dataclasses.fields(row))
    # Each listed property is read (a non-string value is caught), and nothing else is.
    for name in MEETING_PROPERTIES:
        broken = crm_record(MEETING_ID, meeting_properties())
        broken["properties"][name] = 7
        with pytest.raises(QueryDefect, match=name):
            parse_meeting(broken)
    unread = crm_record(MEETING_ID, meeting_properties())
    unread["properties"]["hs_meeting_end_time"] = 7
    assert parse_meeting(unread) == row


def deal_properties(**overrides: str | None) -> dict[str, str | None]:
    """A deal's requested properties; ``overrides`` replace values."""
    properties: dict[str, str | None] = {
        "createdate": "2026-09-14T13:20:00.000Z",
        "dealstage": "appointmentscheduled",
        "pipeline": "default",
        "amount": "1250.50",
    }
    properties.update(overrides)
    return properties


def test_parse_deal_reads_creation_stage_pipeline_and_exact_amount(
    contract: ContractLoader,
) -> None:
    """A deal yields its ``createdate``, stage, pipeline and an exact ``Decimal`` amount."""
    record = read_one(
        contract, crm_record(DEAL_ID, deal_properties(), created_at="2026-09-14T13:20:01.000Z")
    )

    row = parse_deal(record)

    assert row == DealRow(
        hs_id=DEAL_ID,
        created_at=utc(2026, 9, 14, 13, 20),
        stage="appointmentscheduled",
        pipeline="default",
        amount=Decimal("1250.50"),
    )
    assert isinstance(row.amount, Decimal)


def test_parse_deal_empty_amount_is_unavailable(contract: ContractLoader) -> None:
    """An empty deal amount is ``None`` (unavailable), never zero."""
    record = read_one(contract, crm_record(DEAL_ID, deal_properties(amount="")))

    assert parse_deal(record).amount is None


def test_parse_deal_creation_is_never_the_current_time(contract: ContractLoader) -> None:
    """A deal's creation is ``createdate``, else the record's ``createdAt``; never the clock."""
    with_createdate = parse_deal(read_one(contract, crm_record(DEAL_ID, deal_properties())))
    without_createdate = parse_deal(
        read_one(contract, crm_record(DEAL_ID, deal_properties(createdate=None)))
    )

    # Exact fixture instants, not a comparison with the host clock: each row carries its own
    # source field's value, so the result holds under any clock, frozen or early.
    assert with_createdate.created_at == utc(2026, 9, 14, 13, 20)
    # Without `createdate` the record's own creation instant is used, never now().
    assert without_createdate.created_at == utc(2026, 9, 1, 9)
    assert with_createdate.created_at != without_createdate.created_at
    for row in (with_createdate, without_createdate):
        assert row.created_at is not None


def test_deal_properties_list_exactly_the_properties_read(contract: ContractLoader) -> None:
    """``DEAL_PROPERTIES`` lists, once each, exactly the properties ``parse_deal`` reads."""
    assert len(DEAL_PROPERTIES) == len(set(DEAL_PROPERTIES))
    assert set(DEAL_PROPERTIES) == set(deal_properties())
    row = parse_deal(read_one(contract, crm_record(DEAL_ID, deal_properties())))
    for name in DEAL_PROPERTIES:
        broken = crm_record(DEAL_ID, deal_properties())
        broken["properties"][name] = 7
        with pytest.raises(QueryDefect, match=name):
            parse_deal(broken)
    unread = crm_record(DEAL_ID, deal_properties())
    unread["properties"]["closedate"] = 7
    assert parse_deal(unread) == row


# ---------------------------------------------------------------------------------------------
# Phase 4: envelopes
# ---------------------------------------------------------------------------------------------

NOT_FOUND_MESSAGE: Final = (
    "Could not get some CONTACT objects, they may be deleted or not exist. "
    "Check that ids are valid."
)


def test_batch_read_200_returns_records_by_id(contract: ContractLoader) -> None:
    """A 200 batch read is complete with no errors; records keep their order and ``null`` values."""
    body = checked(
        contract,
        OBJECTS,
        batch_envelope(
            [
                crm_record(CONTACT_ID, {"email": "first@example.com"}),
                crm_record(OTHER_CONTACT_ID, {"email": None}),
            ]
        ),
    )

    result = parse_batch_read(body)

    assert result.status == BATCH_COMPLETE
    assert result.is_complete
    assert result.errors == ()
    by_id = {record["id"]: record for record in result.records}
    assert list(by_id) == [CONTACT_ID, OTHER_CONTACT_ID]
    assert by_id[OTHER_CONTACT_ID]["properties"] == {"email": None}


def test_batch_read_207_reports_failed_inputs_and_invents_none(contract: ContractLoader) -> None:
    """A 207 batch read reports failed inputs by category and invents nothing for unlisted ones."""
    # Three ids were requested: one returned, one named by an error, one neither.
    requested = (CONTACT_ID, OTHER_CONTACT_ID, THIRD_CONTACT_ID)
    body = checked(
        contract,
        OBJECTS,
        batch_envelope(
            [crm_record(CONTACT_ID, {"email": "first@example.com"})],
            errors=[
                error_entry("OBJECT_NOT_FOUND", NOT_FOUND_MESSAGE, {"ids": [OTHER_CONTACT_ID]})
            ],
        ),
        status=207,
    )

    result = parse_batch_read(body)

    assert result.is_complete
    assert [record["id"] for record in result.records] == [CONTACT_ID]
    assert result.errors == (
        BatchError(
            category="OBJECT_NOT_FOUND",
            sub_category=None,
            message=NOT_FOUND_MESSAGE,
            ids=(OTHER_CONTACT_ID,),
        ),
    )
    assert result.errors[0].label == "OBJECT_NOT_FOUND"
    returned = {record["id"] for record in result.records}
    failed = {input_id for error in result.errors for input_id in error.ids}
    # The third input is neither returned nor failed: the parser invents nothing for it, so
    # the caller reads it as absent from the response.
    assert THIRD_CONTACT_ID in requested
    assert THIRD_CONTACT_ID not in returned | failed


def test_batch_read_207_by_email_holds_failed_inputs_only_as_digests(
    contract: ContractLoader,
) -> None:
    """A 207 read by email holds each failed input only as its digest, never as plaintext."""
    missing = "Missing.Person@Example.com"
    body = checked(
        contract,
        OBJECTS,
        batch_envelope(
            [crm_record(CONTACT_ID, {"email": "first@example.com"})],
            errors=[
                error_entry(
                    "OBJECT_NOT_FOUND",
                    f"Could not get some CONTACT objects: {missing}",
                    {"ids": [missing]},
                )
            ],
        ),
        status=207,
    )

    (error,) = parse_batch_read(body).errors

    assert error.ids == ()
    assert error.email_sha256s == (email_sha256(missing),)
    for text in walk_strings(dataclasses.asdict(error)):
        assert "@" not in text
    assert "missing.person" not in repr(error).lower()


def test_association_batch_exposes_each_inputs_own_cursor(contract: ContractLoader) -> None:
    """Each association input keeps its own cursor; a paged input is neither empty nor failed."""
    body = checked(
        contract,
        ASSOCIATIONS,
        batch_envelope(
            [
                association(CONTACT_ID, [MEETING_ID], after="MTAwMA"),
                association(OTHER_CONTACT_ID, [OTHER_MEETING_ID]),
            ]
        ),
    )

    batch = parse_association_batch(body)

    assert batch.is_complete
    assert dict(batch.results) == {
        CONTACT_ID: AssociationResult(from_id=CONTACT_ID, targets=(MEETING_ID,), after="MTAwMA"),
        OTHER_CONTACT_ID: AssociationResult(
            from_id=OTHER_CONTACT_ID, targets=(OTHER_MEETING_ID,), after=None
        ),
    }
    # An input with a cursor is not fully read, so it is neither empty nor failed.
    assert batch.empty_ids == frozenset()
    assert dict(batch.error_ids) == {}


def test_association_target_ids_normalise_to_strings(contract: ContractLoader) -> None:
    """Numeric ``toObjectId`` values normalise to strings beside string ones, in order."""
    numeric_target = 99100000003  # The associations guide and live responses send numbers.
    body = checked(
        contract,
        ASSOCIATIONS,
        batch_envelope([association(CONTACT_ID, [MEETING_ID, numeric_target])]),
    )

    result = parse_association_batch(body).results[CONTACT_ID]

    assert result.targets == (MEETING_ID, "99100000003")
    assert all(isinstance(target, str) for target in result.targets)


def test_association_207_no_associations_found_is_a_genuine_zero_not_an_error(
    contract: ContractLoader,
) -> None:
    """A 207 ``NO_ASSOCIATIONS_FOUND`` entry is a genuine zero (``ok_empty``), never an error.

    An empty successful result is ``ok_empty`` too; every other ``errors[]`` entry fails its
    input, labelled with its category.
    """
    body = checked(
        contract,
        ASSOCIATIONS,
        batch_envelope(
            [
                association(CONTACT_ID, [MEETING_ID]),
                # A successful result with nothing associated and no further page.
                association(THIRD_CONTACT_ID, []),
            ],
            errors=[
                error_entry(
                    "OBJECT_NOT_FOUND",
                    f"No meetings is associated with contact {FOURTH_CONTACT_ID}.",
                    {"fromObjectId": [FOURTH_CONTACT_ID]},
                    sub_category=NO_ASSOCIATIONS_FOUND,
                ),
                error_entry(
                    "VALIDATION_ERROR",
                    "The input id is not a valid contact id.",
                    {"fromObjectId": [FIFTH_CONTACT_ID]},
                ),
            ],
        ),
        status=207,
    )

    batch = parse_association_batch(body)

    assert NO_ASSOCIATIONS_FOUND == "crm.associations.NO_ASSOCIATIONS_FOUND"
    assert batch.is_complete
    assert set(batch.results) == {CONTACT_ID, THIRD_CONTACT_ID}
    # ok_empty: the NO_ASSOCIATIONS_FOUND input and the empty successful result.
    assert batch.empty_ids == frozenset({THIRD_CONTACT_ID, FOURTH_CONTACT_ID})
    # error: every other entry, labelled with its category.
    assert dict(batch.error_ids) == {FIFTH_CONTACT_ID: "VALIDATION_ERROR"}
    assert FOURTH_CONTACT_ID not in batch.error_ids
    assert FIFTH_CONTACT_ID not in batch.empty_ids


def test_association_input_both_returned_and_reported_empty_is_a_defect(
    contract: ContractLoader,
) -> None:
    """An input both returned and reported ``NO_ASSOCIATIONS_FOUND`` is an associations defect."""
    body = checked(
        contract,
        ASSOCIATIONS,
        batch_envelope(
            [association(CONTACT_ID, [MEETING_ID])],
            errors=[
                error_entry(
                    "OBJECT_NOT_FOUND",
                    "No associations.",
                    {"fromObjectId": [CONTACT_ID]},
                    sub_category=NO_ASSOCIATIONS_FOUND,
                )
            ],
        ),
        status=207,
    )

    with pytest.raises(QueryDefect, match=ASSOCIATIONS):
        parse_association_batch(body)


def test_search_page_reads_total_records_and_next_cursor(contract: ContractLoader) -> None:
    """A search page yields its ``total``, its records in order and its next-page cursor."""
    body = checked(
        contract,
        SEARCH,
        {
            "total": 3,
            "results": [
                crm_record(DEAL_ID, deal_properties()),
                crm_record("9920000000000002", deal_properties(amount=None)),
            ],
            "paging": {"next": {"after": "2"}},
        },
    )

    page = parse_search_page(body)

    assert page.total == 3
    assert [record["id"] for record in page.results] == [DEAL_ID, "9920000000000002"]
    assert page.after == "2"
    assert [parse_deal(record).amount for record in page.results] == [Decimal("1250.50"), None]


def test_search_page_without_paging_is_the_last_page(contract: ContractLoader) -> None:
    """A search page without ``paging`` is the last page: its cursor is ``None``."""
    body = checked(contract, SEARCH, {"total": 1, "results": [crm_record(DEAL_ID, {})]})

    page = parse_search_page(body)

    assert page.total == 1
    assert len(page.results) == 1
    assert page.after is None


def lifecycle_stage_definition() -> dict[str, Any]:
    """A ``contacts.lifecyclestage`` definition with default and custom stages."""
    return {
        "name": "lifecyclestage",
        "label": "Lifecycle Stage",
        "type": "enumeration",
        "fieldType": "radio",
        "groupName": "contactinformation",
        "description": "The qualification of contacts to sales readiness.",
        "hubspotDefined": True,
        "calculated": False,
        "options": [
            {"label": "Subscriber", "value": "subscriber", "hidden": False, "displayOrder": 0},
            {
                "label": "Marketing Qualified Lead",
                "value": "marketingqualifiedlead",
                "hidden": False,
                "displayOrder": 2,
            },
            {
                "label": "First meeting booked",
                "value": CUSTOM_STAGE_ID,
                "hidden": False,
                "displayOrder": 3,
            },
            {
                "label": "Sales Qualified Lead",
                "value": "salesqualifiedlead",
                "hidden": False,
                "displayOrder": 4,
            },
        ],
    }


def test_parse_property_keeps_lifecycle_stage_labels_and_values(
    contract: ContractLoader,
) -> None:
    """``lifecyclestage`` keeps each option's label and value in order, custom stage included."""
    body = checked(contract, PROPERTIES, lifecycle_stage_definition())

    definition = parse_property(body)

    assert definition.name == "lifecyclestage"
    assert definition.type == "enumeration"
    assert definition.field_type == "radio"
    assert not definition.is_datetime
    assert not definition.is_instant
    assert [(option.label, option.value) for option in definition.options] == [
        ("Subscriber", "subscriber"),
        ("Marketing Qualified Lead", "marketingqualifiedlead"),
        ("First meeting booked", CUSTOM_STAGE_ID),
        ("Sales Qualified Lead", "salesqualifiedlead"),
    ]
    assert all(not option.hidden for option in definition.options)


def test_custom_stage_value_stays_a_string_usable_as_a_stage_date_suffix(
    contract: ContractLoader,
) -> None:
    """A custom stage id stays a string that names its ``hs_v2_date_entered_`` property."""
    definition = parse_property(checked(contract, PROPERTIES, lifecycle_stage_definition()))
    by_label = {option.label: option for option in definition.options}

    value = by_label["First meeting booked"].value

    assert isinstance(value, str)
    assert value == CUSTOM_STAGE_ID
    assert value.isdigit()
    assert f"hs_v2_date_entered_{value}" == MQL_PROPERTY


def test_parse_property_of_a_stage_date_is_a_datetime_without_options(
    contract: ContractLoader,
) -> None:
    """A stage-date property decodes as a datetime instant with no options."""
    body = checked(
        contract,
        PROPERTIES,
        {
            "name": MQL_PROPERTY,
            "label": "Date entered 'First meeting booked (Lifecycle Stage Pipeline)'",
            "type": "datetime",
            "fieldType": "calculation_read_time",
            "groupName": "contactinformation",
            "description": "",
            "calculated": True,
            "options": [],
        },
    )

    definition = parse_property(body)

    assert definition == PropertyDefinition(
        name=MQL_PROPERTY, type="datetime", field_type="calculation_read_time", options=()
    )
    assert definition.is_datetime
    assert definition.is_instant


# ---------------------------------------------------------------------------------------------
# Phase 5: malformed input is a validation defect
# ---------------------------------------------------------------------------------------------


def assert_validation_defect(error: QueryDefect, surface: str) -> None:
    """Check the defect is the parser's validation defect for ``surface``."""
    assert error.kind == "defect"
    assert error.source == HUBSPOT
    assert error.endpoint == "parse"
    assert error.code == "validation"
    assert error.message.startswith(f"{surface}:")


@pytest.mark.parametrize("surface", sorted(DECODERS))
@pytest.mark.parametrize(
    "body", [[], "COMPLETE", None, 7, True], ids=["array", "string", "null", "number", "bool"]
)
def test_non_object_body_is_a_defect(surface: str, body: object) -> None:
    """A body that is not a JSON object is a validation defect on every surface."""
    with pytest.raises(QueryDefect) as caught:
        DECODERS[surface](body)

    assert_validation_defect(caught.value, surface)
    assert "JSON object" in caught.value.message


@pytest.mark.parametrize(
    ("surface", "body"),
    [
        pytest.param(
            OBJECTS,
            {
                "status": BATCH_COMPLETE,
                "results": {},
                "startedAt": STARTED_AT,
                "completedAt": COMPLETED_AT,
            },
            id="objects-results-object",
        ),
        pytest.param(
            ASSOCIATIONS,
            {
                "status": BATCH_COMPLETE,
                "results": "none",
                "startedAt": STARTED_AT,
                "completedAt": COMPLETED_AT,
            },
            id="associations-results-string",
        ),
        pytest.param(SEARCH, {"total": 0, "results": None}, id="search-results-null"),
        pytest.param(SEARCH, {"total": 0, "results": 3}, id="search-results-number"),
    ],
)
def test_results_that_are_not_a_list_are_a_defect(surface: str, body: dict[str, Any]) -> None:
    """A ``results`` that is not a list is a validation defect naming ``results``."""
    with pytest.raises(QueryDefect) as caught:
        DECODERS[surface](body)

    assert_validation_defect(caught.value, surface)
    assert "'results'" in caught.value.message


def record_without_id() -> dict[str, Any]:
    """A CRM object missing its required ``id``, otherwise complete."""
    record = crm_record(CONTACT_ID, {"email": None})
    del record["id"]
    return record


@pytest.mark.parametrize(
    ("surface", "body"),
    [
        pytest.param(OBJECTS, batch_envelope([record_without_id()]), id="objects"),
        pytest.param(SEARCH, {"total": 1, "results": [record_without_id()]}, id="search"),
    ],
)
def test_record_without_id_is_a_defect(surface: str, body: dict[str, Any]) -> None:
    """A record without its ``id`` is a validation defect in a batch read or a search page."""
    with pytest.raises(QueryDefect) as caught:
        DECODERS[surface](body)

    assert_validation_defect(caught.value, surface)
    assert "'record.id'" in caught.value.message


@pytest.mark.parametrize(
    ("kind", "decode"),
    [
        pytest.param("contact", lambda record: parse_contact(record, FULL_BINDINGS), id="contact"),
        pytest.param("meeting", parse_meeting, id="meeting"),
        pytest.param("deal", parse_deal, id="deal"),
    ],
)
def test_row_decoders_reject_a_record_without_id(
    kind: str, decode: Callable[[HubSpotRecord], object]
) -> None:
    """Each row decoder rejects a record without its ``id`` as a defect naming the missing field."""
    with pytest.raises(QueryDefect) as caught:
        decode(record_without_id())

    assert caught.value.source == HUBSPOT
    assert caught.value.code == "validation"
    assert f"required field '{kind}.id' is missing" in caught.value.message


def broken(body: dict[str, Any], edit: Callable[[dict[str, Any]], object]) -> dict[str, Any]:
    """Apply ``edit`` to a deep copy of a contract-valid body and return the broken copy."""
    copied = copy.deepcopy(body)
    edit(copied)
    return copied


OBJECTS_OK: Final = batch_envelope([crm_record(CONTACT_ID, {"email": None})])
ASSOCIATIONS_OK: Final = batch_envelope([association(CONTACT_ID, [MEETING_ID])])
SEARCH_OK: Final = {"total": 1, "results": [crm_record(DEAL_ID, {})]}


def first_target(body: dict[str, Any]) -> dict[str, Any]:
    """The first ``to[]`` entry of the first associations result."""
    target: dict[str, Any] = body["results"][0]["to"][0]
    return target


def first_type(body: dict[str, Any]) -> dict[str, Any]:
    """The first ``associationTypes[]`` entry of :func:`first_target`."""
    spec: dict[str, Any] = first_target(body)["associationTypes"][0]
    return spec


def first_option(body: dict[str, Any]) -> dict[str, Any]:
    """The first ``options[]`` entry of a property definition."""
    option: dict[str, Any] = body["options"][0]
    return option


MALFORMED: Final = [
    pytest.param(OBJECTS, broken(OBJECTS_OK, lambda b: b.pop("status")), "'status'", id="status"),
    pytest.param(
        OBJECTS, broken(OBJECTS_OK, lambda b: b.update(status="DONE")), "one of", id="status-enum"
    ),
    pytest.param(
        OBJECTS,
        broken(OBJECTS_OK, lambda b: b.update(completedAt="yesterday")),
        "'completedAt'",
        id="completed-at",
    ),
    pytest.param(
        OBJECTS,
        broken(
            OBJECTS_OK,
            lambda b: b.update(
                errors=[error_entry("OBJECT_NOT_FOUND", "Not found.", {"ids": [OTHER_CONTACT_ID]})],
                numErrors=2,
            ),
        ),
        "'numErrors'",
        id="num-errors",
    ),
    pytest.param(
        OBJECTS,
        broken(OBJECTS_OK, lambda b: b.update(errors=[{"status": "error", "category": "X"}])),
        "'errors[].message'",
        id="error-message",
    ),
    pytest.param(
        OBJECTS,
        broken(OBJECTS_OK, lambda b: b["results"][0].update(properties=[])),
        "'record.properties'",
        id="record-properties",
    ),
    pytest.param(
        OBJECTS,
        broken(OBJECTS_OK, lambda b: b["results"][0].pop("archived")),
        "'record.archived'",
        id="record-archived",
    ),
    pytest.param(
        OBJECTS,
        broken(OBJECTS_OK, lambda b: b["results"][0].update(id=int(CONTACT_ID))),
        "'record.id'",
        id="record-id-number",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: b["results"][0].pop("from")),
        "'results[].from'",
        id="association-from",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: first_target(b).pop("toObjectId")),
        "'results[].to[].toObjectId'",
        id="association-target",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: first_target(b).pop("associationTypes")),
        "'results[].to[].associationTypes'",
        id="association-types",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: first_type(b).update(category="SYSTEM")),
        "associationTypes[].category'",
        id="association-category",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: first_type(b).update(typeId="200")),
        "associationTypes[].typeId'",
        id="association-type-id",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: b["results"].append(copy.deepcopy(b["results"][0]))),
        "repeats",
        id="association-repeated-input",
    ),
    pytest.param(
        ASSOCIATIONS,
        broken(ASSOCIATIONS_OK, lambda b: b["results"][0].update(paging={"next": {}})),
        "'results[].paging.next.after'",
        id="association-cursor",
    ),
    pytest.param(SEARCH, broken(SEARCH_OK, lambda b: b.pop("total")), "'total'", id="total"),
    pytest.param(
        SEARCH, broken(SEARCH_OK, lambda b: b.update(total=-1)), "'total'", id="total-negative"
    ),
    pytest.param(
        SEARCH,
        broken(SEARCH_OK, lambda b: b["results"][0].pop("createdAt")),
        "'record.createdAt'",
        id="search-record-created-at",
    ),
    pytest.param(
        PROPERTIES,
        broken(lifecycle_stage_definition(), lambda b: b.pop("name")),
        "'name'",
        id="property-name",
    ),
    pytest.param(
        PROPERTIES,
        broken(lifecycle_stage_definition(), lambda b: b.update(type="  ")),
        "'type'",
        id="property-type-blank",
    ),
    pytest.param(
        PROPERTIES,
        broken(lifecycle_stage_definition(), lambda b: b.update(options={})),
        "'options'",
        id="property-options",
    ),
    pytest.param(
        PROPERTIES,
        broken(lifecycle_stage_definition(), lambda b: first_option(b).pop("hidden")),
        "'options[].hidden'",
        id="option-hidden",
    ),
    pytest.param(
        PROPERTIES,
        broken(lifecycle_stage_definition(), lambda b: first_option(b).update(value=1406943076)),
        "'options[].value'",
        id="option-value-number",
    ),
]


@pytest.mark.parametrize(("surface", "body", "field"), MALFORMED)
def test_contract_breach_is_a_defect_naming_the_field_not_the_value(
    surface: str, body: dict[str, Any], field: str
) -> None:
    """Each single contract breach is a validation defect naming the field, never an id or value."""
    with pytest.raises(QueryDefect) as caught:
        DECODERS[surface](body)

    assert_validation_defect(caught.value, surface)
    assert field in caught.value.message
    assert CONTACT_ID not in caught.value.message
    assert CUSTOM_STAGE_ID not in caught.value.message


def test_malformed_cases_start_from_contract_valid_bodies(contract: ContractLoader) -> None:
    """The bodies the malformed cases edit conform to their contracts and decode cleanly."""
    # Each broken body above is a single edit of one of these, so every defect test isolates
    # exactly one contract breach.
    checked(contract, OBJECTS, copy.deepcopy(OBJECTS_OK))
    checked(contract, ASSOCIATIONS, copy.deepcopy(ASSOCIATIONS_OK))
    checked(contract, SEARCH, copy.deepcopy(SEARCH_OK))
    checked(contract, PROPERTIES, lifecycle_stage_definition())
    assert parse_batch_read(copy.deepcopy(OBJECTS_OK)).is_complete
    assert parse_association_batch(copy.deepcopy(ASSOCIATIONS_OK)).is_complete
    assert parse_search_page(copy.deepcopy(SEARCH_OK)).total == 1
    assert parse_property(lifecycle_stage_definition()).name == "lifecyclestage"
