"""Contract and decoding tests for ``ads_engine.connectors.hubspot.parser``.

Every payload is traceable to ``backend/tests/contracts/hubspot/*.json``. Each contract's
documented sample runs through its decoder verbatim (AAP 0.11.2): the associations sample is
the "Retrieve associations" reference page's rendered 207 example, placeholders included.
The objects and associations samples carry status ``CANCELED``, so each decodes as a failed
call with no usable data; their result and error entries are also decoded inside a
successful envelope. Every hand-built body starts from the contract's own shapes, uses only
fields its contract defines, and is checked against that contract's response schema before
it is parsed, so a fixture carrying an undeclared field fails the test. Deliberately
malformed inputs for the defect tests, and the never-read results of a failed batch, are the
only exceptions.

Covered: the batch envelope (``status`` from the contract's enum read first, required
``results``, :data:`BATCH_COMPLETE`, a failed call yielding no usable data, ``startedAt``,
``completedAt`` and ``numErrors``, 207 ``errors[]`` decoded into :class:`BatchError` with
email inputs held only as digests, an input both returned and failed rejected), every CRM
object's required ``id``, ``properties``, ``createdAt``, ``updatedAt`` and ``archived``,
string-or-null property values, association ``associationTypes``, the
associations batch (``from.id`` to ``toObjectId`` pairs, per-input ``paging.next.after``,
``NO_ASSOCIATIONS_FOUND`` as a genuine zero, every other error as a failed input), contact,
meeting and deal rows, instants, exact amounts, property definitions, email hashing, the
contact property map, and every ``QueryDefect`` the parser raises, whose message names the
surface and the field but never a value.

Identifiers are clearly fake (``99…`` ids) and emails are ``example.com`` addresses that exist
in memory only. Everything is pure and synchronous: no database and no network.
"""

import copy
import functools
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

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
    MeetingRow,
    PropertyDefinition,
    PropertyOption,
    SearchPage,
    contact_emails_sha256,
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

pytestmark = pytest.mark.contract

CONTRACTS = Path(__file__).parents[1] / "contracts" / "hubspot"

# The decoder of each contract surface; the keys are the contract file stems.
DECODERS: Mapping[str, Callable[[object], object]] = {
    "associations_batch_read": parse_association_batch,
    "crm_search": parse_search_page,
    "objects_batch_read": parse_batch_read,
    "properties": parse_property,
}

# ---------------------------------------------------------------------------------------------
# Contract files: loading and the conformance check
# ---------------------------------------------------------------------------------------------

# The JSON Schema keywords the HubSpot contracts use. Any other keyword fails the check rather
# than being silently ignored.
_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}
)
_JSON_TYPES: Mapping[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, Mapping),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, int | Decimal) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


@functools.cache
def _contract(surface: str) -> Mapping[str, Any]:
    """Load a contract file the way ``connectors/http.py`` decodes bodies (floats as Decimal)."""
    text = (CONTRACTS / f"{surface}.json").read_text(encoding="utf-8")
    loaded: Mapping[str, Any] = json.loads(text, parse_float=Decimal)
    return loaded


def _sample(surface: str) -> Any:
    """Return a private deep copy of the contract's documented sample response."""
    sample = _contract(surface)["response"]["documented_sample"]
    assert sample is not None, f"{surface} has no documented sample"
    return copy.deepcopy(sample)


def _schema(surface: str, *path: str) -> Mapping[str, Any]:
    """Return the response body schema of ``surface``, or the sub-schema at ``path``."""
    schema: Mapping[str, Any] = _contract(surface)["response"]["body"]
    for key in path:
        schema = schema[key]
    return schema


def _schema_errors(value: object, schema: Mapping[str, Any], path: str) -> list[str]:
    """Validate ``value`` against a contract schema; objects are closed unless declared open."""
    unknown = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unknown:
        return [f"{path}: unsupported schema keywords {unknown}"]
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(_JSON_TYPES[name](value) for name in names):
            return [f"{path}: {type(value).__name__} is not {names}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not a declared enum value")
    if isinstance(value, Mapping):
        properties: Mapping[str, Any] = schema.get("properties", {})
        additional = schema.get("additionalProperties")
        errors.extend(
            f"{path}: required field {key!r} is missing"
            for key in schema.get("required", ())
            if key not in value
        )
        for key, item in value.items():
            if key in properties:
                errors.extend(_schema_errors(item, properties[key], f"{path}.{key}"))
            elif isinstance(additional, Mapping):
                errors.extend(_schema_errors(item, additional, f"{path}.{key}"))
            elif additional is not True:
                errors.append(f"{path}: field {key!r} is not defined by the contract")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_schema_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def _conforming[T](surface: str, value: T, *path: str) -> T:
    """Return ``value`` after asserting it satisfies the contract schema at ``path``."""
    errors = _schema_errors(value, _schema(surface, *path), surface)
    assert errors == [], errors
    return value


# ---------------------------------------------------------------------------------------------
# Fake identifiers and contract-conformant builders
# ---------------------------------------------------------------------------------------------

CONTACT_ID = "99000000001"
OTHER_CONTACT_ID = "99000000002"
MEETING_ID = "99100000001"
DEAL_ID = "99200000001"

RAW_EMAIL = "  Lead.One@Example.COM "
EMAIL = "lead.one@example.com"
EMAIL_SHA256 = hashlib.sha256(EMAIL.encode("utf-8")).hexdigest()

MQL_PROPERTY = "date_first_meeting_was_booked"
QL_PROPERTY = "hs_v2_date_entered_1406943076"
CAMPAIGN_PROPERTY = "google_campaign_id"
TOUCH_PROPERTY = "google_touch_date"
PROPS_ALL = ContactPropertyMap(
    mql_property=MQL_PROPERTY,
    ql_property=QL_PROPERTY,
    google_campaign_property=CAMPAIGN_PROPERTY,
    google_touch_property=TOUCH_PROPERTY,
)

# 2026-09-04T13:05:22.123Z in each wire form HubSpot uses.
INSTANT = datetime(2026, 9, 4, 13, 5, 22, 123000, tzinfo=UTC)
INSTANT_ISO_Z = "2026-09-04T13:05:22.123Z"
INSTANT_EPOCH_MS = 1_788_527_122_123
# 2026-09-10T15:30:00Z, the record timestamps of the hand-built objects.
UPDATED = datetime(2026, 9, 10, 15, 30, tzinfo=UTC)
UPDATED_ISO = "2026-09-10T15:30:00Z"
UPDATED_EPOCH_MS = 1_789_054_200_000
STARTED_AT = "2026-09-10T15:30:00.100Z"
COMPLETED_AT = "2026-09-10T15:30:00.250Z"

INSTANT_EXPECTED = "expected an ISO-8601 instant, a YYYY-MM-DD date or epoch milliseconds"
_RECORD_PATH = ("properties", "results", "items")


def _sha256(text: str) -> str:
    """SHA-256 hex digest of ``text`` as UTF-8, computed independently of the parser."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hs_record(
    hs_id: str,
    properties: Mapping[str, str | None],
    *,
    created_at: str = INSTANT_ISO_Z,
    updated_at: str = UPDATED_ISO,
) -> dict[str, Any]:
    """Build one CRM object from the objects contract's documented record."""
    record: dict[str, Any] = _sample("objects_batch_read")["results"][0]
    for optional in ("archivedAt", "objectWriteTraceId", "propertiesWithHistory", "url"):
        del record[optional]
    record.update(
        id=hs_id,
        properties=dict(properties),
        createdAt=created_at,
        updatedAt=updated_at,
        archived=False,
    )
    return _conforming("objects_batch_read", record, *_RECORD_PATH)


def _batch_body(
    surface: str,
    results: Sequence[Mapping[str, Any]],
    *,
    errors: Sequence[Mapping[str, Any]] | None = None,
    status: str = BATCH_COMPLETE,
) -> dict[str, Any]:
    """Build a 200 batch body, or a 207 one when ``errors`` is given."""
    body: dict[str, Any] = {
        "status": status,
        "results": [dict(result) for result in results],
        "startedAt": STARTED_AT,
        "completedAt": COMPLETED_AT,
    }
    if errors is not None:
        body["errors"] = [dict(entry) for entry in errors]
        body["numErrors"] = len(errors)
    return _conforming(surface, body)


def _error_entry(
    category: str,
    message: str,
    *,
    sub_category: str | None = None,
    context: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build one live-shaped 207 ``errors[]`` entry."""
    entry: dict[str, Any] = {"status": "error", "category": category, "message": message}
    if sub_category is not None:
        entry["subCategory"] = sub_category
    if context is not None:
        entry["context"] = dict(context)
    return entry


def _association(
    from_id: str, targets: Sequence[str | int], *, after: str | None = None
) -> dict[str, Any]:
    """Build one contact-to-meeting ``results[]`` element of an associations batch read."""
    result: dict[str, Any] = {
        "from": {"id": from_id},
        "to": [
            {
                "toObjectId": target,
                "associationTypes": [
                    {
                        "category": "HUBSPOT_DEFINED",
                        "typeId": 200,
                        "label": None,
                        "fromObjectTypeId": "0-1",
                        "toObjectTypeId": "0-47",
                    }
                ],
            }
            for target in targets
        ],
    }
    if after is not None:
        result["paging"] = {"next": {"after": after}}
    return result


def _search_body(
    results: Sequence[Mapping[str, Any]], total: int, *, paging: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build a CRM Search page body."""
    body: dict[str, Any] = {"total": total, "results": [dict(result) for result in results]}
    if paging is not None:
        body["paging"] = dict(paging)
    return _conforming("crm_search", body)


def _option(label: str, value: str, *, hidden: bool, order: int) -> dict[str, Any]:
    """Build one ``options[]`` element of a property definition."""
    return {"label": label, "value": value, "hidden": hidden, "displayOrder": order}


def _property_body(
    name: str, type_: str, field_type: str, options: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build a property definition from the properties contract's documented sample."""
    body: dict[str, Any] = _sample("properties")
    body.update(
        name=name,
        type=type_,
        fieldType=field_type,
        label=name.replace("_", " ").title(),
        description="Test property definition",
        groupName="contactinformation",
        options=[dict(option) for option in options],
    )
    return _conforming("properties", body)


def _assert_defect(error: QueryDefect, message: str, forbidden: str | None = None) -> None:
    """Assert ``error`` is the parser's validation defect with exactly ``message``."""
    assert error.kind == "defect"
    assert error.source == "hubspot"
    assert error.endpoint == "parse"
    assert error.code == "validation"
    assert error.http_status is None
    assert error.query_id is None
    assert error.message == message
    assert error.reason == f"hubspot parse: validation: {message}"
    if forbidden is not None:
        for text in (error.message, error.reason, str(error), repr(error)):
            assert forbidden not in text


# ---------------------------------------------------------------------------------------------
# Contract files and documented samples
# ---------------------------------------------------------------------------------------------


def test_every_contract_surface_has_a_decoder() -> None:
    stems = {path.stem for path in CONTRACTS.glob("*.json")}
    assert stems == set(DECODERS)
    for stem in stems:
        contract = _contract(stem)
        assert contract["source"] == "hubspot"
        assert contract["surface"] == f"hubspot.{stem}"
        assert contract["api_version"] == "2026-09"
        assert contract["reference_url"].startswith(
            "https://developers.hubspot.com/docs/api-reference/latest/crm/"
        )


def test_conformance_check_rejects_an_undeclared_field() -> None:
    record = _hs_record(CONTACT_ID, {})
    record["unexpectedField"] = "x"
    errors = _schema_errors(record, _schema("objects_batch_read", *_RECORD_PATH), "record")
    assert errors == ["record: field 'unexpectedField' is not defined by the contract"]


@pytest.mark.parametrize("surface", sorted(DECODERS))
def test_documented_sample_conforms_and_decodes_verbatim(surface: str) -> None:
    sample = _sample(surface)
    assert _schema_errors(sample, _schema(surface), surface) == []
    original = copy.deepcopy(sample)
    DECODERS[surface](sample)
    assert sample == original


def test_associations_documented_sample() -> None:
    sample = _sample("associations_batch_read")
    batch = parse_association_batch(sample)
    assert isinstance(batch, AssociationBatch)
    # Its CANCELED status makes the sample a failed call: no input is settled by it.
    assert batch.status == "CANCELED"
    assert batch.is_complete is False
    assert dict(batch.results) == {}
    assert batch.empty_ids == frozenset()
    assert dict(batch.error_ids) == {}

    # The sample's result and error entries, verbatim inside a successful envelope.
    complete = parse_association_batch(
        _batch_body("associations_batch_read", sample["results"], errors=sample["errors"])
    )
    assert dict(complete.results) == {
        "<string>": AssociationResult(from_id="<string>", targets=("<string>",), after="<string>")
    }
    assert complete.empty_ids == frozenset()
    # The sample's error entry has an empty context, so it names no failed input.
    assert dict(complete.error_ids) == {}


def test_objects_documented_sample() -> None:
    sample = _sample("objects_batch_read")
    result = parse_batch_read(sample)
    assert isinstance(result, BatchReadResult)
    # Its CANCELED status makes the sample a failed call: it yields no usable record.
    assert result.status == "CANCELED"
    assert result.is_complete is False
    assert result.records == ()
    assert result.errors == ()

    # The sample's error entry, verbatim inside a successful envelope. The rendered
    # subCategory is an object ({}), which is not a string subcategory.
    entries = sample["errors"]
    (error,) = parse_batch_read(_batch_body("objects_batch_read", [], errors=entries)).errors
    assert error == BatchError(category="<string>", sub_category=None, message="<string>", ids=())
    assert error.label == "<string>"
    assert error.email_sha256s == ()

    # The sample's record decodes on its own as each object type.
    record = sample["results"][0]
    created = datetime(2023, 11, 7, 5, 31, 56, tzinfo=UTC)
    assert parse_contact(record, PROPS_ALL) == ContactRow(
        hs_id="<string>",
        email_sha256=None,
        lifecycle_stage=None,
        mql_at=None,
        ql_at=None,
        google_campaign_ref=None,
        google_touch_at=None,
        updated_at=created,
    )
    assert contact_emails_sha256(record) == frozenset()
    assert parse_meeting(record) == MeetingRow(
        hs_id="<string>",
        created_at=created,
        start_at=None,
        meeting_timestamp=None,
        outcome=None,
        meeting_source=None,
    )
    assert parse_deal(record) == DealRow(
        hs_id="<string>", created_at=created, stage=None, pipeline=None, amount=None
    )


def test_crm_search_documented_sample() -> None:
    page = parse_search_page(_sample("crm_search"))
    assert isinstance(page, SearchPage)
    assert page.total == 2
    assert page.after is None
    assert [record["id"] for record in page.results] == ["100451", "57156923994"]
    rows = [parse_contact(record, ContactPropertyMap()) for record in page.results]
    assert [row.hs_id for row in rows] == ["100451", "57156923994"]
    assert [row.email_sha256 for row in rows] == [
        _sha256("testperson@hubspot.com"),
        _sha256("emailmaria@hubspot.com"),
    ]
    assert [row.updated_at for row in rows] == [
        datetime(2024, 9, 11, 13, 27, 39, 356000, tzinfo=UTC),
        datetime(2024, 10, 21, 21, 36, 2, 961000, tzinfo=UTC),
    ]
    assert all(row.lifecycle_stage is None and row.mql_at is None for row in rows)
    assert all("@hubspot.com" not in repr(row) for row in rows)
    assert contact_emails_sha256(page.results[0]) == {_sha256("testperson@hubspot.com")}


def test_properties_documented_sample() -> None:
    definition = parse_property(_sample("properties"))
    assert definition == PropertyDefinition(
        name="<string>",
        type="<string>",
        field_type="<string>",
        options=(PropertyOption(label="<string>", value="<string>", hidden=True),),
    )
    assert definition.is_datetime is False


# ---------------------------------------------------------------------------------------------
# Objects batch read and 207 errors
# ---------------------------------------------------------------------------------------------


def test_batch_read_200_keeps_records_in_response_order() -> None:
    first = _hs_record(OTHER_CONTACT_ID, {"email": "second@example.com"})
    second = _hs_record(CONTACT_ID, {"email": EMAIL})
    result = parse_batch_read(_batch_body("objects_batch_read", [first, second]))
    assert result.status == BATCH_COMPLETE == "COMPLETE"
    assert result.is_complete is True
    assert result.records == (first, second)
    assert result.errors == ()


@pytest.mark.parametrize("status", ["CANCELED", "PENDING", "PROCESSING"])
def test_batch_status_other_than_complete_is_a_failed_call(status: str) -> None:
    enum = _schema("objects_batch_read", "properties", "status")["enum"]
    assert set(enum) == {BATCH_COMPLETE, "CANCELED", "PENDING", "PROCESSING"}
    record = _hs_record(CONTACT_ID, {})
    objects = parse_batch_read(_batch_body("objects_batch_read", [record], status=status))
    associations = parse_association_batch(
        _batch_body("associations_batch_read", [_association(CONTACT_ID, [1])], status=status)
    )
    assert (objects.status, objects.is_complete) == (status, False)
    assert (associations.status, associations.is_complete) == (status, False)
    assert (objects.records, objects.errors) == ((), ())
    assert dict(associations.results) == {}
    assert (associations.empty_ids, dict(associations.error_ids)) == (frozenset(), {})


@pytest.mark.parametrize("status", ["CANCELED", "PENDING", "PROCESSING"])
def test_failed_batch_is_recognised_before_any_result_is_validated(status: str) -> None:
    # Deliberately non-conformant results: a failed call's results are never read, so
    # elements a successful record would need (properties, createdAt, to) may be absent.
    objects_body = {
        "status": status,
        "results": [{"id": CONTACT_ID}, "not a record"],
        "startedAt": STARTED_AT,
        "completedAt": COMPLETED_AT,
    }
    associations_body = {**objects_body, "results": [{"from": {"id": CONTACT_ID}}, 7]}
    objects = parse_batch_read(objects_body)
    associations = parse_association_batch(associations_body)
    assert objects == BatchReadResult(status=status, records=(), errors=())
    assert associations.status == status
    assert associations.is_complete is False
    assert dict(associations.results) == {}
    assert (associations.empty_ids, dict(associations.error_ids)) == (frozenset(), {})


def test_207_contacts_by_email_holds_email_inputs_only_as_digests() -> None:
    missing = "Nobody@Example.COM"
    entry = _error_entry(
        "OBJECT_NOT_FOUND",
        f"Could not get some CONTACT objects, they may be deleted or not exist: {missing}",
        context={"ids": [missing, OTHER_CONTACT_ID]},
    )
    result = parse_batch_read(
        _batch_body("objects_batch_read", [_hs_record(CONTACT_ID, {})], errors=[entry])
    )
    (error,) = result.errors
    assert error.category == "OBJECT_NOT_FOUND"
    assert error.sub_category is None
    assert error.label == "OBJECT_NOT_FOUND"
    assert error.ids == (OTHER_CONTACT_ID,)
    assert error.email_sha256s == (email_sha256(missing),) == (_sha256("nobody@example.com"),)
    assert error.message == (
        "Could not get some CONTACT objects, they may be deleted or not exist: [REDACTED_EMAIL]"
    )
    for text in (error.message, repr(error), repr(result.errors)):
        assert "nobody@example.com" not in text.lower()


def test_207_without_the_optional_num_errors_decodes_its_errors() -> None:
    entry = _error_entry("OBJECT_NOT_FOUND", "Not found", context={"ids": [OTHER_CONTACT_ID]})
    body = _batch_body("objects_batch_read", [_hs_record(CONTACT_ID, {})], errors=[entry])
    del body["numErrors"]
    result = parse_batch_read(_conforming("objects_batch_read", body))
    assert [record["id"] for record in result.records] == [CONTACT_ID]
    assert [error.ids for error in result.errors] == [(OTHER_CONTACT_ID,)]


def test_batch_error_message_drops_email_ids_no_pattern_would_catch() -> None:
    raw = "lead@intranet"
    entry = _error_entry("OBJECT_NOT_FOUND", f"No CONTACT {raw} found", context={"ids": [raw]})
    (error,) = parse_batch_read(_batch_body("objects_batch_read", [], errors=[entry])).errors
    assert error.ids == ()
    assert error.email_sha256s == (_sha256(raw),)
    assert error.message == "No CONTACT [REDACTED_EMAIL] found"


def test_batch_error_context_ids_are_collected_in_order_without_repeats() -> None:
    entry = _error_entry(
        "OBJECT_NOT_FOUND",
        "Some inputs failed",
        context={
            "ids": ["Lead.One@Example.COM", EMAIL, "5", 5, None],
            "fromObjectId": "6",
            "other": ["7"],
        },
    )
    second = _error_entry("OBJECT_NOT_FOUND", "One input failed", context={"fromObjectId": 8})
    third = _error_entry("VALIDATION_ERROR", "No context")
    errors = parse_batch_read(
        _batch_body("objects_batch_read", [], errors=[entry, second, third])
    ).errors
    assert errors[0].ids == ("5", "6")
    assert errors[0].email_sha256s == (EMAIL_SHA256,)
    assert errors[1].ids == ("8",)
    assert (errors[2].ids, errors[2].email_sha256s) == ((), ())


@pytest.mark.parametrize(
    ("sub_category", "expected", "label"),
    [
        (
            "  crm.objects.INVALID_ID  ",
            "crm.objects.INVALID_ID",
            "OBJECT_NOT_FOUND/crm.objects.INVALID_ID",
        ),
        ("   ", None, "OBJECT_NOT_FOUND"),
        ({}, None, "OBJECT_NOT_FOUND"),
        ({"reason": "x"}, None, "OBJECT_NOT_FOUND"),
    ],
)
def test_batch_error_sub_category_and_label(
    sub_category: object, expected: str | None, label: str
) -> None:
    entry = _error_entry("  OBJECT_NOT_FOUND ", "Not found", context={"ids": ["9"]})
    entry["subCategory"] = sub_category
    (error,) = parse_batch_read(_batch_body("objects_batch_read", [], errors=[entry])).errors
    assert error.category == "OBJECT_NOT_FOUND"
    assert error.sub_category == expected
    assert error.label == label


# ---------------------------------------------------------------------------------------------
# Associations batch read
# ---------------------------------------------------------------------------------------------


def test_association_batch_pairs_from_ids_with_target_ids_and_cursors() -> None:
    body = _batch_body(
        "associations_batch_read",
        [
            _association(CONTACT_ID, [99100000001, "99100000002", 99100000001], after="MTAw"),
            _association(OTHER_CONTACT_ID, [99100000003]),
        ],
    )
    batch = parse_association_batch(body)
    assert batch.is_complete is True
    assert isinstance(batch.results, MappingProxyType)
    assert dict(batch.results) == {
        CONTACT_ID: AssociationResult(
            from_id=CONTACT_ID, targets=("99100000001", "99100000002"), after="MTAw"
        ),
        OTHER_CONTACT_ID: AssociationResult(
            from_id=OTHER_CONTACT_ID, targets=("99100000003",), after=None
        ),
    }
    assert batch.empty_ids == frozenset()
    assert isinstance(batch.error_ids, MappingProxyType)
    assert dict(batch.error_ids) == {}


def test_association_empty_to_is_empty_only_without_a_next_page() -> None:
    body = _batch_body(
        "associations_batch_read",
        [_association("99000000004", []), _association("99000000005", [], after="cursor-5")],
    )
    batch = parse_association_batch(body)
    assert batch.empty_ids == frozenset({"99000000004"})
    assert batch.results["99000000005"] == AssociationResult(
        from_id="99000000005", targets=(), after="cursor-5"
    )
    assert dict(batch.error_ids) == {}


def test_association_207_no_associations_found_is_a_genuine_zero() -> None:
    assert NO_ASSOCIATIONS_FOUND == "crm.associations.NO_ASSOCIATIONS_FOUND"
    errors = [
        _error_entry(
            "OBJECT_NOT_FOUND",
            "No associations found for objectIds 99000000006, 99000000007",
            sub_category=NO_ASSOCIATIONS_FOUND,
            context={"fromObjectId": ["99000000006", "99000000007"]},
        ),
        _error_entry(
            "NO_ASSOCIATIONS_FOUND",
            "No associations found",
            context={"fromObjectId": ["99000000008"]},
        ),
    ]
    batch = parse_association_batch(_batch_body("associations_batch_read", [], errors=errors))
    assert batch.is_complete is True
    assert dict(batch.results) == {}
    assert batch.empty_ids == frozenset({"99000000006", "99000000007", "99000000008"})
    assert dict(batch.error_ids) == {}


def test_association_207_other_errors_fail_their_inputs_with_the_first_label() -> None:
    errors = [
        _error_entry("OBJECT_NOT_FOUND", "Unknown input", context={"fromObjectId": ["9"]}),
        _error_entry(
            "VALIDATION_ERROR",
            "Invalid inputs",
            sub_category="crm.associations.INVALID_INPUT",
            context={"fromObjectId": ["10", "9"]},
        ),
        _error_entry(
            "OBJECT_NOT_FOUND",
            "Not the empty case",
            sub_category="crm.associations.NO_ASSOCIATIONS_FOUND_FOR_LABEL",
            context={"fromObjectId": ["12"]},
        ),
    ]
    batch = parse_association_batch(_batch_body("associations_batch_read", [], errors=errors))
    assert dict(batch.error_ids) == {
        "9": "OBJECT_NOT_FOUND",
        "10": "VALIDATION_ERROR/crm.associations.INVALID_INPUT",
        "12": "OBJECT_NOT_FOUND/crm.associations.NO_ASSOCIATIONS_FOUND_FOR_LABEL",
    }
    assert batch.empty_ids == frozenset()


@pytest.mark.parametrize(
    ("result", "entry"),
    [
        (
            _association("11", [99100000009]),
            _error_entry(
                "OBJECT_NOT_FOUND",
                "No associations found",
                sub_category=NO_ASSOCIATIONS_FOUND,
                context={"fromObjectId": ["11", "13"]},
            ),
        ),
        (
            _association("11", []),
            _error_entry("VALIDATION_ERROR", "Invalid input", context={"fromObjectId": ["11"]}),
        ),
        (
            _association("11", [99100000009], after="cursor-11"),
            _error_entry("OBJECT_NOT_FOUND", "Unknown input", context={"fromObjectId": [11]}),
        ),
    ],
    ids=["targets-and-no-associations", "empty-and-error", "paged-and-error"],
)
def test_association_input_returned_and_listed_as_an_error_is_a_defect(
    result: Mapping[str, Any], entry: Mapping[str, Any]
) -> None:
    # A failed input is absent from results (AAP 0.2.4): one in both is contradictory, and
    # is never read as associated, empty or failed.
    body = _batch_body("associations_batch_read", [result], errors=[entry])
    with pytest.raises(QueryDefect) as caught:
        parse_association_batch(body)
    _assert_defect(
        caught.value,
        "associations_batch_read: an input in 'results' is also named by an 'errors' entry",
        forbidden="11",
    )


def test_association_inputs_named_only_by_errors_keep_their_own_outcome() -> None:
    errors = [
        _error_entry(
            "OBJECT_NOT_FOUND",
            "No associations found",
            sub_category=NO_ASSOCIATIONS_FOUND,
            context={"fromObjectId": ["13"]},
        ),
        _error_entry("VALIDATION_ERROR", "Invalid input", context={"fromObjectId": ["14"]}),
    ]
    body = _batch_body(
        "associations_batch_read", [_association("11", [99100000009])], errors=errors
    )
    batch = parse_association_batch(body)
    assert batch.results["11"].targets == ("99100000009",)
    assert batch.empty_ids == frozenset({"13"})
    assert dict(batch.error_ids) == {"14": "VALIDATION_ERROR"}


# ---------------------------------------------------------------------------------------------
# CRM Search pages
# ---------------------------------------------------------------------------------------------


def test_search_page_reads_total_records_and_next_cursor() -> None:
    records = [_hs_record(DEAL_ID, {"createdate": INSTANT_ISO_Z}), _hs_record("99200000002", {})]
    paging = {
        "next": {
            "after": "2",
            "link": "https://api.hubapi.com/crm/objects/2026-09/deals/search?after=2",
        }
    }
    page = parse_search_page(_search_body(records, 3, paging=paging))
    assert page == SearchPage(results=(records[0], records[1]), total=3, after="2")


@pytest.mark.parametrize(
    "paging",
    [None, {"prev": {"before": "1", "link": "https://api.hubapi.com/x"}}],
    ids=["no-paging", "prev-only"],
)
def test_search_page_without_a_next_page_has_no_cursor(paging: Mapping[str, Any] | None) -> None:
    page = parse_search_page(_search_body([], 0, paging=paging))
    assert page == SearchPage(results=(), total=0, after=None)


# ---------------------------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------------------------


def test_parse_contact_reads_each_bound_property_on_its_own() -> None:
    record = _hs_record(
        CONTACT_ID,
        {
            "email": RAW_EMAIL,
            "lifecyclestage": "  1406943076 ",
            MQL_PROPERTY: "2026-09-03",
            QL_PROPERTY: "2026-09-04T09:05:22.123-04:00",
            CAMPAIGN_PROPERTY: " 23456789012 ",
            TOUCH_PROPERTY: str(INSTANT_EPOCH_MS),
            "lastmodifieddate": "2026-01-01T00:00:00Z",
        },
    )
    row = parse_contact(record, PROPS_ALL)
    assert row == ContactRow(
        hs_id=CONTACT_ID,
        email_sha256=EMAIL_SHA256,
        lifecycle_stage="1406943076",
        mql_at=datetime(2026, 9, 3, tzinfo=UTC),
        ql_at=INSTANT,
        google_campaign_ref="23456789012",
        google_touch_at=INSTANT,
        updated_at=UPDATED,
    )
    for instant in (row.mql_at, row.ql_at, row.google_touch_at, row.updated_at):
        assert instant is not None
        assert instant.tzinfo is UTC
    for text in (repr(row), str(row)):
        assert EMAIL not in text.lower()
    with pytest.raises(FrozenInstanceError):
        row.email_sha256 = None  # type: ignore[misc]


def test_parse_contact_unbound_or_skipped_stages_are_none() -> None:
    record = _hs_record(
        CONTACT_ID,
        {
            "email": "   ",
            "lifecyclestage": "customer",
            MQL_PROPERTY: "2026-09-03",
            QL_PROPERTY: "",
            CAMPAIGN_PROPERTY: None,
            "lastmodifieddate": "2026-01-01T00:00:00Z",
        },
    )
    # Nothing bound: no metric is read, whatever the record holds.
    unbound = parse_contact(record, ContactPropertyMap())
    assert (unbound.mql_at, unbound.ql_at) == (None, None)
    assert (unbound.google_campaign_ref, unbound.google_touch_at) == (None, None)
    # Bound: an empty stage date is a skipped stage, and QL is never derived from MQL.
    bound = parse_contact(record, PROPS_ALL)
    assert bound.mql_at == datetime(2026, 9, 3, tzinfo=UTC)
    assert bound.ql_at is None
    assert bound.google_campaign_ref is None
    assert bound.google_touch_at is None
    assert bound.lifecycle_stage == "customer"
    assert bound.email_sha256 is None
    # updated_at is the record's required updatedAt, never the lastmodifieddate property.
    assert bound.updated_at == UPDATED


@pytest.mark.parametrize(
    "properties",
    [{}, {"email": None}, {"email": ""}, {"email": " \t "}, {"email": "lead\ud800@example.com"}],
    ids=["absent", "null", "empty", "blank", "unencodable"],
)
def test_contact_without_a_usable_email_has_no_digest(properties: Mapping[str, str | None]) -> None:
    record = _hs_record(CONTACT_ID, properties)
    assert parse_contact(record, ContactPropertyMap()).email_sha256 is None
    assert contact_emails_sha256(record) == frozenset()


def test_contact_emails_sha256_covers_primary_and_additional_emails() -> None:
    record = _hs_record(
        CONTACT_ID,
        {
            "email": RAW_EMAIL,
            "hs_additional_emails": "Second@Example.com;  ; third@example.com ;second@example.com;"
            "x\ud800@example.com",
        },
    )
    assert contact_emails_sha256(record) == frozenset(
        {EMAIL_SHA256, _sha256("second@example.com"), _sha256("third@example.com")}
    )
    only_additional = _hs_record(CONTACT_ID, {"hs_additional_emails": "Other@Example.com"})
    assert contact_emails_sha256(only_additional) == {_sha256("other@example.com")}


# ---------------------------------------------------------------------------------------------
# Meetings and deals
# ---------------------------------------------------------------------------------------------


def test_parse_meeting_reads_every_requested_property() -> None:
    properties = {
        "hs_createdate": INSTANT_ISO_Z,
        "hs_timestamp": str(UPDATED_EPOCH_MS),
        "hs_meeting_start_time": "2026-09-10T11:30:00-04:00",
        "hs_meeting_outcome": " completed ",
        "hs_meeting_source": "MEETINGS_PUBLIC",
    }
    assert set(properties) == set(MEETING_PROPERTIES)
    row = parse_meeting(_hs_record(MEETING_ID, properties, created_at="2026-01-01T00:00:00Z"))
    assert row == MeetingRow(
        hs_id=MEETING_ID,
        created_at=INSTANT,
        start_at=UPDATED,
        meeting_timestamp=UPDATED,
        outcome="COMPLETED",
        meeting_source="MEETINGS_PUBLIC",
    )


def test_parse_meeting_falls_back_to_the_record_creation_time() -> None:
    record = _hs_record(MEETING_ID, {"hs_createdate": "", "hs_meeting_outcome": None})
    assert parse_meeting(record) == MeetingRow(
        hs_id=MEETING_ID,
        created_at=INSTANT,
        start_at=None,
        meeting_timestamp=None,
        outcome=None,
        meeting_source=None,
    )


def test_parse_deal_reads_every_requested_property() -> None:
    properties = {
        "createdate": INSTANT_ISO_Z,
        "dealstage": " appointmentscheduled ",
        "pipeline": "default",
        "amount": "1500.50",
    }
    assert set(properties) == set(DEAL_PROPERTIES)
    row = parse_deal(_hs_record(DEAL_ID, properties, created_at="2026-01-01T00:00:00Z"))
    assert row == DealRow(
        hs_id=DEAL_ID,
        created_at=INSTANT,
        stage="appointmentscheduled",
        pipeline="default",
        amount=Decimal("1500.50"),
    )
    assert isinstance(row.amount, Decimal)


def test_parse_deal_falls_back_to_the_record_creation_time() -> None:
    row = parse_deal(_hs_record(DEAL_ID, {"createdate": None}, created_at=UPDATED_ISO))
    assert row == DealRow(hs_id=DEAL_ID, created_at=UPDATED, stage=None, pipeline=None, amount=None)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("1500.50", Decimal("1500.50")),
        (" 12.5 ", Decimal("12.5")),
        ("999999999999.999999", Decimal("999999999999.999999")),
        ("-999999999999.999999", Decimal("-999999999999.999999")),
        ("0.000001", Decimal("0.000001")),
        ("1000000000000", None),
        ("-1e12", None),
        ("1E+999999", None),
        ("0.0000001", None),
        ("12.3456789", None),
        ("1E-999999", None),
        ("NaN", None),
        ("sNaN", None),
        ("Infinity", None),
        ("-Infinity", None),
        ("n/a", None),
        ("", None),
        (None, None),
    ],
)
def test_deal_amount_is_none_unless_numeric_18_6_stores_it_exactly(
    amount: str | None, expected: Decimal | None
) -> None:
    row = parse_deal(_hs_record(DEAL_ID, {"amount": amount}))
    assert row.amount == expected
    if expected is not None:
        assert row.amount is not None
        assert row.amount.as_tuple() == expected.as_tuple()


# ---------------------------------------------------------------------------------------------
# Primitives: decimals and instants
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1500.50", Decimal("1500.50")),
        ("  7 ", Decimal("7")),
        (Decimal("1.10"), Decimal("1.10")),
        (7, Decimal(7)),
        (0.1, Decimal("0.1")),
        (float("nan"), None),
        (float("inf"), None),
        (Decimal("NaN"), None),
        (Decimal("-Infinity"), None),
        ("1e-9999999999999999999", None),
        ("n/a", None),
        ("   ", None),
        ("", None),
        (True, None),
        (None, None),
        ([], None),
    ],
)
def test_parse_decimal(value: object, expected: Decimal | None) -> None:
    result = parse_decimal(value)
    assert result == expected
    if expected is not None:
        assert isinstance(result, Decimal)
        assert result.as_tuple() == expected.as_tuple()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (INSTANT_ISO_Z, INSTANT),
        ("2026-09-04T09:05:22.123-04:00", INSTANT),
        ("2026-09-04T13:05:22.123+00:00", INSTANT),
        ("2026-09-04T13:05:22.123", INSTANT),
        ("2026-09-04 13:05:22.123", INSTANT),
        ("  2026-09-04T13:05:22.123Z ", INSTANT),
        ("2026-09-04", datetime(2026, 9, 4, tzinfo=UTC)),
        (str(INSTANT_EPOCH_MS), INSTANT),
        (f" {INSTANT_EPOCH_MS} ", INSTANT),
        (INSTANT_EPOCH_MS, INSTANT),
        (Decimal(INSTANT_EPOCH_MS), INSTANT),
        (Decimal("1.788527122123E+12"), INSTANT),
        (0, datetime(1970, 1, 1, tzinfo=UTC)),
        ("0", datetime(1970, 1, 1, tzinfo=UTC)),
        (-1, datetime(1970, 1, 1, tzinfo=UTC) - timedelta(milliseconds=1)),
    ],
)
def test_parse_instant_accepts_each_hubspot_form_as_utc(value: object, expected: datetime) -> None:
    result = parse_instant(value)
    assert result == expected
    assert result is not None
    assert result.tzinfo is UTC


@pytest.mark.parametrize("value", [None, "", "   ", "\t"])
def test_parse_instant_empty_is_none(value: str | None) -> None:
    assert parse_instant(value) is None


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        1.5,
        Decimal("1.5"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "yesterday",
        "lead@example.com",
        "2026-02-30",
        "2026-13-01T00:00",
        "2026-09-04T25:00",
        "2026-09-04X13:05",
        "9" * 30,
        "9" * 5000,
        10**30,
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "\u0661\u0662\u0663",
        [],
        {},
    ],
    ids=lambda value: type(value).__name__ + ":" + repr(value)[:24],
)
def test_parse_instant_rejects_anything_else_without_echoing_it(value: object) -> None:
    what = f"contact property '{TOUCH_PROPERTY}'"
    with pytest.raises(QueryDefect) as caught:
        parse_instant(value, what=what)
    _assert_defect(caught.value, f"{what}: {INSTANT_EXPECTED}", forbidden=str(value))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_parse_instant_names_a_generic_field_by_default() -> None:
    with pytest.raises(QueryDefect) as caught:
        parse_instant(True)
    _assert_defect(caught.value, f"instant: {INSTANT_EXPECTED}")


# ---------------------------------------------------------------------------------------------
# Property definitions
# ---------------------------------------------------------------------------------------------


def test_parse_property_reads_lifecycle_stage_options_in_order() -> None:
    body = _property_body(
        "lifecyclestage",
        "enumeration",
        "radio",
        [
            _option("Subscriber", "subscriber", hidden=False, order=0),
            _option("Qualified Lead", "1406943076", hidden=False, order=1),
            _option("Other", "other", hidden=True, order=-1),
        ],
    )
    definition = parse_property(body)
    assert definition == PropertyDefinition(
        name="lifecyclestage",
        type="enumeration",
        field_type="radio",
        options=(
            PropertyOption(label="Subscriber", value="subscriber", hidden=False),
            PropertyOption(label="Qualified Lead", value="1406943076", hidden=False),
            PropertyOption(label="Other", value="other", hidden=True),
        ),
    )
    assert definition.is_datetime is False
    assert definition.is_instant is False


@pytest.mark.parametrize(
    ("type_", "field_type", "is_datetime", "is_instant"),
    [
        ("datetime", "date", True, True),
        # A day without a time: usable for a stage date, never as the Google touch instant.
        ("date", "date", True, False),
        ("string", "text", False, False),
    ],
)
def test_parse_property_without_options(
    type_: str, field_type: str, is_datetime: bool, is_instant: bool
) -> None:
    definition = parse_property(_property_body(MQL_PROPERTY, type_, field_type, []))
    assert definition == PropertyDefinition(
        name=MQL_PROPERTY, type=type_, field_type=field_type, options=()
    )
    assert definition.is_datetime is is_datetime
    assert definition.is_instant is is_instant


def test_parse_property_blank_field_type_is_none() -> None:
    # fieldType is required and present, but blank: it holds no rendering, so it is None.
    body = _property_body(MQL_PROPERTY, "datetime", "  ", [])
    assert parse_property(body).field_type is None


def test_parse_property_accepts_the_empty_strings_its_contract_allows() -> None:
    body = _property_body(MQL_PROPERTY, "datetime", "date", [])
    body.update(description="", groupName="", label="")
    definition = parse_property(_conforming("properties", body))
    assert definition == PropertyDefinition(
        name=MQL_PROPERTY, type="datetime", field_type="date", options=()
    )


# ---------------------------------------------------------------------------------------------
# Email normalisation and hashing
# ---------------------------------------------------------------------------------------------


def test_normalize_email_trims_and_lowercases() -> None:
    assert normalize_email(RAW_EMAIL) == EMAIL
    assert normalize_email(EMAIL) == EMAIL


@pytest.mark.parametrize(
    "value", [RAW_EMAIL, EMAIL, "LEAD.ONE@EXAMPLE.COM", "\tlead.one@example.com\n"]
)
def test_email_sha256_hashes_the_normalised_address(value: str) -> None:
    digest = email_sha256(value)
    assert digest == EMAIL_SHA256
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "email address is empty"),
        ("   ", "email address is empty"),
        ("lead\ud800@example.com", "email address is not valid UTF-8 text"),
    ],
    ids=["empty", "blank", "unencodable"],
)
def test_email_sha256_rejects_unusable_addresses_without_echoing_them(
    value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=r"^email address is") as caught:
        email_sha256(value)
    assert str(caught.value) == message
    assert "example" not in str(caught.value)
    assert "@" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# ---------------------------------------------------------------------------------------------
# Contact property map
# ---------------------------------------------------------------------------------------------


def test_contact_property_map_request_properties() -> None:
    base = ("email", "hs_additional_emails", "lifecyclestage", "lastmodifieddate")
    assert ContactPropertyMap().request_properties() == base
    assert PROPS_ALL.request_properties() == (
        *base,
        MQL_PROPERTY,
        QL_PROPERTY,
        CAMPAIGN_PROPERTY,
        TOUCH_PROPERTY,
    )
    duplicates = ContactPropertyMap(
        mql_property="lifecyclestage", ql_property=QL_PROPERTY, google_touch_property=QL_PROPERTY
    )
    assert duplicates.request_properties() == (*base, QL_PROPERTY)


@pytest.mark.parametrize("name", ["", "   ", " hs_v2_date_entered_lead", "date_x\t"])
@pytest.mark.parametrize("field", [field.name for field in fields(ContactPropertyMap)])
def test_contact_property_map_rejects_blank_or_padded_names(field: str, name: str) -> None:
    with pytest.raises(ValueError, match="must be a non-blank name"):
        ContactPropertyMap(**{field: name})


# ---------------------------------------------------------------------------------------------
# Every validation defect: source hubspot, endpoint parse, code validation, no values
# ---------------------------------------------------------------------------------------------

_TIMES = {"startedAt": STARTED_AT, "completedAt": COMPLETED_AT}
_BATCH = {"status": BATCH_COMPLETE, "results": [], **_TIMES}
_ERROR = {"status": "error", "category": "OBJECT_NOT_FOUND", "message": "Not found"}
_META = {"createdAt": INSTANT_ISO_Z, "updatedAt": UPDATED_ISO, "archived": False}
_RECORD = {"id": "1", "properties": {}, **_META}
_TYPE = {"category": "HUBSPOT_DEFINED", "typeId": 200}
_TARGET = {"toObjectId": MEETING_ID, "associationTypes": [_TYPE]}
_FROM = {"from": {"id": CONTACT_ID}, "to": []}
_PROPERTY = {
    "name": "lifecyclestage",
    "type": "enumeration",
    "fieldType": "radio",
    "description": "",
    "groupName": "contactinformation",
    "label": "Lifecycle Stage",
    "options": [],
}
_OPTION = {"label": "Lead", "value": "lead", "hidden": False}
_LEAKED = "lead.one@example.com"
_STATUS_ENUM = "field 'status' must be one of CANCELED, COMPLETE, PENDING, PROCESSING"
_CATEGORY_ENUM = (
    "field 'results[].to[].associationTypes[].category' must be one of HUBSPOT_DEFINED, "
    "INTEGRATOR_DEFINED, USER_DEFINED, WORK"
)


def _without(mapping: Mapping[str, object], key: str) -> dict[str, object]:
    """Return a copy of ``mapping`` without ``key``."""
    return {name: value for name, value in mapping.items() if name != key}


def _contact(properties: Mapping[str, object], **record: object) -> Callable[[], object]:
    """Return a call parsing a contact whose properties are ``properties``."""
    body = {"id": CONTACT_ID, "properties": properties, **_META, **record}
    return lambda: parse_contact(body, PROPS_ALL)


def _meeting(properties: Mapping[str, object], **record: object) -> Callable[[], object]:
    """Return a call parsing a meeting whose properties are ``properties``."""
    body = {"id": MEETING_ID, "properties": properties, **_META, **record}
    return lambda: parse_meeting(body)


def _deal(properties: Mapping[str, object], **record: object) -> Callable[[], object]:
    """Return a call parsing a deal whose properties are ``properties``."""
    body = {"id": DEAL_ID, "properties": properties, **_META, **record}
    return lambda: parse_deal(body)


DEFECT_CASES = [
    # objects_batch_read
    pytest.param(
        lambda: parse_batch_read("x"),
        "objects_batch_read: response body must be a JSON object",
        None,
        id="objects-body-not-object",
    ),
    pytest.param(
        lambda: parse_batch_read({"results": []}),
        "objects_batch_read: required field 'status' is missing",
        None,
        id="objects-status-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": 5, "results": []}),
        "objects_batch_read: field 'status' must be a string",
        None,
        id="objects-status-not-string",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": f"{_LEAKED}\x00", "results": []}),
        "objects_batch_read: field 'status' must not contain a NUL character",
        _LEAKED,
        id="objects-status-nul",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": f"{_LEAKED}\ud800", "results": []}),
        "objects_batch_read: field 'status' must be valid UTF-8 text",
        _LEAKED,
        id="objects-status-unencodable",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": "DONE", "results": []}),
        f"objects_batch_read: {_STATUS_ENUM}",
        "DONE",
        id="objects-status-unknown",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": "CANCELED"}),
        "objects_batch_read: required field 'results' is missing",
        None,
        id="objects-failed-results-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": "PENDING", "results": {}}),
        "objects_batch_read: field 'results' must be a JSON array",
        None,
        id="objects-failed-results-not-array",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": BATCH_COMPLETE}),
        "objects_batch_read: required field 'results' is missing",
        None,
        id="objects-results-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": {}}),
        "objects_batch_read: field 'results' must be a JSON array",
        None,
        id="objects-results-not-array",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_LEAKED]}),
        "objects_batch_read: record record must be a JSON object",
        _LEAKED,
        id="objects-record-not-object",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_without(_RECORD, "id")]}),
        "objects_batch_read: required field 'record.id' is missing",
        None,
        id="objects-record-id-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "id": True}]}),
        "objects_batch_read: 'record.id' must be a string",
        None,
        id="objects-record-id-bool",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "id": Decimal("1.5")}]}),
        "objects_batch_read: 'record.id' must be a string",
        None,
        id="objects-record-id-decimal",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "id": 99200000001}]}),
        "objects_batch_read: 'record.id' must be a string",
        "99200000001",
        id="objects-record-id-integer",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "id": "  "}]}),
        "objects_batch_read: 'record.id' must not be blank",
        None,
        id="objects-record-id-blank",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "id": "1\x00"}]}),
        "objects_batch_read: 'record.id' must not contain a NUL character",
        None,
        id="objects-record-id-nul",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_without(_RECORD, "properties")]}),
        "objects_batch_read: required field 'record.properties' is missing",
        None,
        id="objects-record-properties-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "properties": [_LEAKED]}]}),
        "objects_batch_read: field 'record.properties' must be a JSON object",
        _LEAKED,
        id="objects-record-properties-not-object",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_without(_RECORD, "createdAt")]}),
        "objects_batch_read: required field 'record.createdAt' is missing",
        None,
        id="objects-record-created-at-missing",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {**_BATCH, "results": [{**_RECORD, "createdAt": INSTANT_EPOCH_MS}]}
        ),
        "objects_batch_read: field 'record.createdAt' must be a string",
        str(INSTANT_EPOCH_MS),
        id="objects-record-created-at-number",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "createdAt": " "}]}),
        "objects_batch_read: field 'record.createdAt' must not be blank",
        None,
        id="objects-record-created-at-blank",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "createdAt": _LEAKED}]}),
        f"objects_batch_read: 'record.createdAt': {INSTANT_EXPECTED}",
        _LEAKED,
        id="objects-record-created-at-malformed",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_without(_RECORD, "updatedAt")]}),
        "objects_batch_read: required field 'record.updatedAt' is missing",
        None,
        id="objects-record-updated-at-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "updatedAt": ""}]}),
        "objects_batch_read: field 'record.updatedAt' must not be blank",
        None,
        id="objects-record-updated-at-empty",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [_without(_RECORD, "archived")]}),
        "objects_batch_read: required field 'record.archived' is missing",
        None,
        id="objects-record-archived-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "results": [{**_RECORD, "archived": "false"}]}),
        "objects_batch_read: field 'record.archived' must be a boolean",
        None,
        id="objects-record-archived-not-boolean",
    ),
    pytest.param(
        lambda: parse_batch_read({"status": BATCH_COMPLETE, "results": []}),
        "objects_batch_read: required field 'startedAt' is missing",
        None,
        id="objects-started-at-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "startedAt": 1}),
        "objects_batch_read: field 'startedAt' must be a string",
        None,
        id="objects-started-at-number",
    ),
    pytest.param(
        lambda: parse_batch_read(_without(_BATCH, "completedAt")),
        "objects_batch_read: required field 'completedAt' is missing",
        None,
        id="objects-completed-at-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "completedAt": "soon"}),
        f"objects_batch_read: 'completedAt': {INSTANT_EXPECTED}",
        "soon",
        id="objects-completed-at-malformed",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": {}}),
        "objects_batch_read: field 'errors' must be a JSON array",
        None,
        id="objects-errors-not-array",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [_LEAKED]}),
        "objects_batch_read: errors[] entry must be a JSON object",
        _LEAKED,
        id="objects-error-not-object",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{"message": "m"}]}),
        "objects_batch_read: required field 'category' is missing",
        None,
        id="objects-error-category-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "category": 7}]}),
        "objects_batch_read: field 'category' must be a string",
        None,
        id="objects-error-category-not-string",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "category": "  "}]}),
        "objects_batch_read: field 'errors[].category' must not be blank",
        None,
        id="objects-error-category-blank",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "message": None}]}),
        "objects_batch_read: field 'errors[].message' must be a string",
        None,
        id="objects-error-message-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "subCategory": "a\x00"}]}),
        "objects_batch_read: field 'errors[].subCategory' must not contain a NUL character",
        None,
        id="objects-error-subcategory-nul",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "context": {"ids": [True]}}]}),
        "objects_batch_read: 'errors[].context.ids[]' must be a string or an integer",
        None,
        id="objects-error-context-id-bool",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {**_BATCH, "errors": [{**_ERROR, "context": {"ids": [f"{_LEAKED}\x00"]}}]}
        ),
        "objects_batch_read: 'errors[].context.ids[]' must not contain a NUL character",
        _LEAKED,
        id="objects-error-context-email-nul",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {**_BATCH, "errors": [{**_ERROR, "context": {"fromObjectId": [" "]}}]}
        ),
        "objects_batch_read: 'errors[].context.fromObjectId[]' must not be blank",
        None,
        id="objects-error-context-id-blank",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [_without(_ERROR, "status")]}),
        "objects_batch_read: required field 'errors[].status' is missing",
        None,
        id="objects-error-status-missing",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [{**_ERROR, "status": 404}]}),
        "objects_batch_read: field 'errors[].status' must be a string",
        None,
        id="objects-error-status-not-string",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [_ERROR], "numErrors": -1}),
        "objects_batch_read: field 'numErrors' must be a non-negative integer",
        None,
        id="objects-num-errors-negative",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [_ERROR], "numErrors": True}),
        "objects_batch_read: field 'numErrors' must be a non-negative integer",
        None,
        id="objects-num-errors-bool",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "errors": [_ERROR], "numErrors": 123}),
        "objects_batch_read: field 'numErrors' must equal the number of 'errors' entries",
        "123",
        id="objects-num-errors-mismatch",
    ),
    pytest.param(
        lambda: parse_batch_read({**_BATCH, "numErrors": 1}),
        "objects_batch_read: field 'numErrors' must equal the number of 'errors' entries",
        None,
        id="objects-num-errors-without-errors",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {
                **_BATCH,
                "results": [{**_RECORD, "id": DEAL_ID}],
                "errors": [{**_ERROR, "context": {"ids": ["99200000009", DEAL_ID]}}],
                "numErrors": 1,
            }
        ),
        "objects_batch_read: a 'results' record is also named by an 'errors' entry",
        DEAL_ID,
        id="objects-record-also-failed",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {
                **_BATCH,
                "results": [{**_RECORD, "properties": {"email": f" {_LEAKED.upper()} "}}],
                "errors": [{**_ERROR, "context": {"ids": [_LEAKED]}}],
            }
        ),
        "objects_batch_read: a 'results' contact answers to an email an 'errors' entry names",
        _LEAKED,
        id="objects-contact-email-also-failed",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {
                **_BATCH,
                "results": [
                    {**_RECORD, "properties": {"hs_additional_emails": f"x@example.com;{_LEAKED}"}}
                ],
                "errors": [
                    {**_ERROR, "category": "VALIDATION_ERROR", "context": {"ids": [_LEAKED]}}
                ],
            }
        ),
        "objects_batch_read: a 'results' contact answers to an email an 'errors' entry names",
        _LEAKED,
        id="objects-contact-additional-email-also-failed",
    ),
    pytest.param(
        lambda: parse_batch_read(
            {
                **_BATCH,
                "results": [{**_RECORD, "properties": {"email": [_LEAKED]}}],
                "errors": [{**_ERROR, "context": {"ids": [_LEAKED]}}],
            }
        ),
        "objects_batch_read: contact property 'email' must be a string",
        _LEAKED,
        id="objects-contradiction-check-email-not-string",
    ),
    # associations_batch_read
    pytest.param(
        lambda: parse_association_batch([]),
        "associations_batch_read: response body must be a JSON object",
        None,
        id="associations-body-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch({"results": []}),
        "associations_batch_read: required field 'status' is missing",
        None,
        id="associations-status-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({"status": "complete", "results": []}),
        f"associations_batch_read: {_STATUS_ENUM}",
        "complete",
        id="associations-status-unknown",
    ),
    pytest.param(
        lambda: parse_association_batch({"status": "PROCESSING", "results": "x"}),
        "associations_batch_read: field 'results' must be a JSON array",
        None,
        id="associations-failed-results-not-array",
    ),
    pytest.param(
        lambda: parse_association_batch({"status": BATCH_COMPLETE}),
        "associations_batch_read: required field 'results' is missing",
        None,
        id="associations-results-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [CONTACT_ID]}),
        "associations_batch_read: results[] entry must be a JSON object",
        CONTACT_ID,
        id="associations-result-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{"to": []}]}),
        "associations_batch_read: required field 'results[].from' is missing",
        None,
        id="associations-from-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "from": CONTACT_ID}]}),
        "associations_batch_read: field 'results[].from' must be a JSON object",
        CONTACT_ID,
        id="associations-from-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "from": {}}]}),
        "associations_batch_read: required field 'results[].from.id' is missing",
        None,
        id="associations-from-id-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{"from": {"id": CONTACT_ID}}]}),
        "associations_batch_read: required field 'to' is missing",
        None,
        id="associations-to-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "to": {}}]}),
        "associations_batch_read: field 'to' must be a JSON array",
        None,
        id="associations-to-not-array",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "to": [MEETING_ID]}]}),
        "associations_batch_read: results[].to[] entry must be a JSON object",
        MEETING_ID,
        id="associations-target-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "to": [{}]}]}),
        "associations_batch_read: required field 'results[].to[].toObjectId' is missing",
        None,
        id="associations-target-id-missing",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "to": [{"toObjectId": Decimal("1.5")}]}]}
        ),
        "associations_batch_read: 'results[].to[].toObjectId' must be a string or an integer",
        None,
        id="associations-target-id-decimal",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "paging": "x"}]}),
        "associations_batch_read: field 'results[].paging' must be a JSON object",
        None,
        id="associations-paging-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "paging": {"next": "x"}}]}
        ),
        "associations_batch_read: field 'results[].paging.next' must be a JSON object",
        None,
        id="associations-paging-next-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [{**_FROM, "paging": {"next": {}}}]}),
        "associations_batch_read: required field 'results[].paging.next.after' is missing",
        None,
        id="associations-after-missing",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "paging": {"next": {"after": " "}}}]}
        ),
        "associations_batch_read: 'results[].paging.next.after' must not be blank",
        None,
        id="associations-after-blank",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "results": [_FROM, _FROM]}),
        "associations_batch_read: field 'results[].from.id' repeats an input",
        CONTACT_ID,
        id="associations-from-id-repeats",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "errors": [{"category": "C"}]}),
        "associations_batch_read: field 'errors[].message' must be a string",
        None,
        id="associations-error-message-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "errors": [{**_ERROR, "status": None}]}),
        "associations_batch_read: required field 'errors[].status' is missing",
        None,
        id="associations-error-status-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "errors": [_ERROR, _ERROR], "numErrors": 1}),
        "associations_batch_read: field 'numErrors' must equal the number of 'errors' entries",
        None,
        id="associations-num-errors-mismatch",
    ),
    pytest.param(
        lambda: parse_association_batch(_without(_BATCH, "startedAt")),
        "associations_batch_read: required field 'startedAt' is missing",
        None,
        id="associations-started-at-missing",
    ),
    pytest.param(
        lambda: parse_association_batch({**_BATCH, "completedAt": "2026-13-01T00:00"}),
        f"associations_batch_read: 'completedAt': {INSTANT_EXPECTED}",
        "2026-13-01",
        id="associations-completed-at-malformed",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "to": [{"toObjectId": MEETING_ID}]}]}
        ),
        "associations_batch_read: required field 'results[].to[].associationTypes' is missing",
        None,
        id="associations-types-missing",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "to": [{**_TARGET, "associationTypes": {}}]}]}
        ),
        "associations_batch_read: field 'results[].to[].associationTypes' must be a JSON array",
        None,
        id="associations-types-not-array",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {**_BATCH, "results": [{**_FROM, "to": [{**_TARGET, "associationTypes": ["x"]}]}]}
        ),
        "associations_batch_read: results[].to[].associationTypes[] entry must be a JSON object",
        None,
        id="associations-type-not-object",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {
                **_BATCH,
                "results": [
                    {
                        **_FROM,
                        "to": [{**_TARGET, "associationTypes": [_without(_TYPE, "category")]}],
                    }
                ],
            }
        ),
        "associations_batch_read: required field "
        "'results[].to[].associationTypes[].category' is missing",
        None,
        id="associations-type-category-missing",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {
                **_BATCH,
                "results": [
                    {
                        **_FROM,
                        "to": [{**_TARGET, "associationTypes": [{**_TYPE, "category": "CUSTOM"}]}],
                    }
                ],
            }
        ),
        f"associations_batch_read: {_CATEGORY_ENUM}",
        "CUSTOM",
        id="associations-type-category-unknown",
    ),
    pytest.param(
        lambda: parse_association_batch(
            {
                **_BATCH,
                "results": [
                    {**_FROM, "to": [{**_TARGET, "associationTypes": [_without(_TYPE, "typeId")]}]}
                ],
            }
        ),
        "associations_batch_read: required field "
        "'results[].to[].associationTypes[].typeId' is missing",
        None,
        id="associations-type-id-missing",
    ),
    *(
        pytest.param(
            functools.partial(
                parse_association_batch,
                {
                    **_BATCH,
                    "results": [
                        {
                            **_FROM,
                            "to": [{**_TARGET, "associationTypes": [{**_TYPE, "typeId": type_id}]}],
                        }
                    ],
                },
            ),
            "associations_batch_read: field "
            "'results[].to[].associationTypes[].typeId' must be an integer",
            None,
            id=f"associations-type-id-{kind}",
        )
        for kind, type_id in (("string", "200"), ("decimal", Decimal("200.5")), ("bool", True))
    ),
    # crm_search
    pytest.param(
        lambda: parse_search_page("x"),
        "crm_search: response body must be a JSON object",
        None,
        id="search-body-not-object",
    ),
    pytest.param(
        lambda: parse_search_page({"results": []}),
        "crm_search: required field 'total' is missing",
        None,
        id="search-total-missing",
    ),
    pytest.param(
        lambda: parse_search_page({"total": "2", "results": []}),
        "crm_search: field 'total' must be a non-negative integer",
        None,
        id="search-total-string",
    ),
    pytest.param(
        lambda: parse_search_page({"total": -1, "results": []}),
        "crm_search: field 'total' must be a non-negative integer",
        None,
        id="search-total-negative",
    ),
    pytest.param(
        lambda: parse_search_page({"total": True, "results": []}),
        "crm_search: field 'total' must be a non-negative integer",
        None,
        id="search-total-bool",
    ),
    pytest.param(
        lambda: parse_search_page({"total": Decimal("2.5"), "results": []}),
        "crm_search: field 'total' must be a non-negative integer",
        None,
        id="search-total-decimal",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 0}),
        "crm_search: required field 'results' is missing",
        None,
        id="search-results-missing",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 1, "results": [{"id": DEAL_ID}]}),
        "crm_search: required field 'record.properties' is missing",
        None,
        id="search-record-properties-missing",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 1, "results": [{**_RECORD, "id": 12345}]}),
        "crm_search: 'record.id' must be a string",
        "12345",
        id="search-record-id-integer",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 1, "results": [_without(_RECORD, "createdAt")]}),
        "crm_search: required field 'record.createdAt' is missing",
        None,
        id="search-record-created-at-missing",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 1, "results": [{**_RECORD, "updatedAt": "later"}]}),
        f"crm_search: 'record.updatedAt': {INSTANT_EXPECTED}",
        "later",
        id="search-record-updated-at-malformed",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 1, "results": [{**_RECORD, "archived": None}]}),
        "crm_search: required field 'record.archived' is missing",
        None,
        id="search-record-archived-null",
    ),
    pytest.param(
        lambda: parse_search_page({"total": 0, "results": [], "paging": []}),
        "crm_search: field 'paging' must be a JSON object",
        None,
        id="search-paging-not-object",
    ),
    pytest.param(
        lambda: parse_search_page(
            {"total": 0, "results": [], "paging": {"next": {"after": Decimal("2.5")}}}
        ),
        "crm_search: 'paging.next.after' must be a string or an integer",
        None,
        id="search-after-decimal",
    ),
    # properties
    pytest.param(
        lambda: parse_property("x"),
        "properties: response body must be a JSON object",
        None,
        id="property-body-not-object",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "fieldType": 5}),
        "properties: field 'fieldType' must be a string",
        None,
        id="property-field-type-not-string",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "fieldType": "radio\x00"}),
        "properties: field 'fieldType' must not contain a NUL character",
        None,
        id="property-field-type-nul",
    ),
    pytest.param(
        lambda: parse_property({"type": "enumeration"}),
        "properties: required field 'name' is missing",
        None,
        id="property-name-missing",
    ),
    pytest.param(
        lambda: parse_property({"name": "lifecyclestage"}),
        "properties: required field 'type' is missing",
        None,
        id="property-type-missing",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "name": "  "}),
        "properties: field 'name' must not be blank",
        None,
        id="property-name-blank",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "type": ""}),
        "properties: field 'type' must not be blank",
        None,
        id="property-type-blank",
    ),
    pytest.param(
        lambda: parse_property(_without(_PROPERTY, "fieldType")),
        "properties: required field 'fieldType' is missing",
        None,
        id="property-field-type-missing",
    ),
    pytest.param(
        lambda: parse_property(_without(_PROPERTY, "description")),
        "properties: required field 'description' is missing",
        None,
        id="property-description-missing",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "groupName": ["contactinformation"]}),
        "properties: field 'groupName' must be a string",
        None,
        id="property-group-name-not-string",
    ),
    pytest.param(
        lambda: parse_property(_without(_PROPERTY, "label")),
        "properties: required field 'label' is missing",
        None,
        id="property-label-missing",
    ),
    pytest.param(
        lambda: parse_property(_without(_PROPERTY, "options")),
        "properties: required field 'options' is missing",
        None,
        id="property-options-missing",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": {}}),
        "properties: field 'options' must be a JSON array",
        None,
        id="property-options-not-array",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": ["lead"]}),
        "properties: options[] entry must be a JSON object",
        None,
        id="property-option-not-object",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [{"value": "lead", "hidden": False}]}),
        "properties: required field 'label' is missing",
        None,
        id="property-option-label-missing",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [{"label": "Lead", "hidden": False}]}),
        "properties: required field 'options[].value' is missing",
        None,
        id="property-option-value-missing",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [{**_OPTION, "value": ""}]}),
        "properties: 'options[].value' must not be blank",
        None,
        id="property-option-value-blank",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [{**_OPTION, "value": 1406943076}]}),
        "properties: 'options[].value' must be a string",
        None,
        id="property-option-value-integer",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [{**_OPTION, "hidden": "yes"}]}),
        "properties: field 'options[].hidden' must be a boolean",
        None,
        id="property-option-hidden-not-bool",
    ),
    pytest.param(
        lambda: parse_property({**_PROPERTY, "options": [_without(_OPTION, "hidden")]}),
        "properties: required field 'options[].hidden' is missing",
        None,
        id="property-option-hidden-missing",
    ),
    # Records decoded on their own carry no surface prefix.
    pytest.param(
        lambda: parse_contact(_LEAKED, PROPS_ALL),  # type: ignore[arg-type]
        "contact record must be a JSON object",
        _LEAKED,
        id="contact-not-object",
    ),
    pytest.param(
        lambda: parse_contact({"properties": {"email": _LEAKED}}, PROPS_ALL),
        "required field 'contact.id' is missing",
        _LEAKED,
        id="contact-id-missing",
    ),
    pytest.param(
        lambda: parse_contact({"id": CONTACT_ID, "properties": _LEAKED}, PROPS_ALL),
        "field 'contact.properties' must be a JSON object",
        _LEAKED,
        id="contact-properties-not-object",
    ),
    pytest.param(
        _contact({"email": 42}),
        "contact property 'email' must be a string",
        None,
        id="contact-email-not-string",
    ),
    pytest.param(
        _contact({"lifecyclestage": True}),
        "contact property 'lifecyclestage' must be a string",
        None,
        id="contact-stage-bool",
    ),
    pytest.param(
        _contact({"lifecyclestage": [_LEAKED]}),
        "contact property 'lifecyclestage' must be a string",
        _LEAKED,
        id="contact-stage-array",
    ),
    pytest.param(
        _contact({"lifecyclestage": f"{_LEAKED}\x00"}),
        "contact property 'lifecyclestage' must not contain a NUL character",
        _LEAKED,
        id="contact-stage-nul",
    ),
    pytest.param(
        _contact({CAMPAIGN_PROPERTY: f"{_LEAKED}\ud800"}),
        f"contact property '{CAMPAIGN_PROPERTY}' must be valid UTF-8 text",
        _LEAKED,
        id="contact-campaign-unencodable",
    ),
    pytest.param(
        _contact({MQL_PROPERTY: _LEAKED}),
        f"contact property '{MQL_PROPERTY}': {INSTANT_EXPECTED}",
        _LEAKED,
        id="contact-mql-malformed",
    ),
    pytest.param(
        _contact({QL_PROPERTY: "2026-02-30"}),
        f"contact property '{QL_PROPERTY}': {INSTANT_EXPECTED}",
        "2026-02-30",
        id="contact-ql-malformed",
    ),
    pytest.param(
        _contact({TOUCH_PROPERTY: True}),
        f"contact property '{TOUCH_PROPERTY}' must be a string",
        None,
        id="contact-touch-bool",
    ),
    pytest.param(
        _contact({TOUCH_PROPERTY: INSTANT_EPOCH_MS}),
        f"contact property '{TOUCH_PROPERTY}' must be a string",
        str(INSTANT_EPOCH_MS),
        id="contact-touch-number",
    ),
    pytest.param(
        _contact({CAMPAIGN_PROPERTY: 23456789012}),
        f"contact property '{CAMPAIGN_PROPERTY}' must be a string",
        "23456789012",
        id="contact-campaign-number",
    ),
    pytest.param(
        _contact({}, updatedAt=_LEAKED),
        f"'contact.updatedAt': {INSTANT_EXPECTED}",
        _LEAKED,
        id="contact-updated-at-malformed",
    ),
    pytest.param(
        _contact({}, updatedAt=""),
        "field 'contact.updatedAt' must not be blank",
        None,
        id="contact-updated-at-empty",
    ),
    pytest.param(
        _contact({}, createdAt=None),
        "required field 'contact.createdAt' is missing",
        None,
        id="contact-created-at-null",
    ),
    pytest.param(
        _contact({}, archived=0),
        "field 'contact.archived' must be a boolean",
        None,
        id="contact-archived-not-boolean",
    ),
    pytest.param(
        lambda: contact_emails_sha256(
            {"id": CONTACT_ID, "properties": {"email": [_LEAKED]}, **_META}
        ),
        "contact property 'email' must be a string",
        _LEAKED,
        id="contact-emails-primary-not-string",
    ),
    pytest.param(
        lambda: contact_emails_sha256(
            {"id": CONTACT_ID, "properties": {"hs_additional_emails": [_LEAKED]}, **_META}
        ),
        "contact property 'hs_additional_emails' must be a string",
        _LEAKED,
        id="contact-emails-additional-not-string",
    ),
    pytest.param(
        lambda: contact_emails_sha256({"id": CONTACT_ID}),
        "required field 'contact.properties' is missing",
        None,
        id="contact-emails-properties-missing",
    ),
    pytest.param(
        lambda: contact_emails_sha256({"id": CONTACT_ID, "properties": {}}),
        "required field 'contact.createdAt' is missing",
        None,
        id="contact-emails-created-at-missing",
    ),
    pytest.param(
        lambda: parse_meeting({"properties": {}, **_META}),
        "required field 'meeting.id' is missing",
        None,
        id="meeting-id-missing",
    ),
    pytest.param(
        # A meeting with an id and properties but no creation metadata is never read.
        lambda: parse_meeting(
            {
                "id": MEETING_ID,
                "properties": {"hs_createdate": INSTANT_ISO_Z},
                "updatedAt": UPDATED_ISO,
                "archived": False,
            }
        ),
        "required field 'meeting.createdAt' is missing",
        None,
        id="meeting-created-at-missing",
    ),
    pytest.param(
        _meeting({"hs_createdate": INSTANT_ISO_Z}, archived=None),
        "required field 'meeting.archived' is missing",
        None,
        id="meeting-archived-missing",
    ),
    pytest.param(
        _meeting({"hs_meeting_outcome": {}}),
        "meeting property 'hs_meeting_outcome' must be a string",
        None,
        id="meeting-outcome-not-string",
    ),
    pytest.param(
        _meeting({"hs_meeting_source": "MEETINGS_PUBLIC\x00"}),
        "meeting property 'hs_meeting_source' must not contain a NUL character",
        None,
        id="meeting-source-nul",
    ),
    pytest.param(
        _meeting({"hs_createdate": "soon"}),
        f"meeting property 'hs_createdate': {INSTANT_EXPECTED}",
        "soon",
        id="meeting-createdate-malformed",
    ),
    pytest.param(
        _meeting({"hs_createdate": ""}, createdAt="later"),
        f"'meeting.createdAt': {INSTANT_EXPECTED}",
        "later",
        id="meeting-created-at-malformed",
    ),
    pytest.param(
        _meeting({"hs_createdate": INSTANT_ISO_Z, "hs_meeting_start_time": "2026-13-01"}),
        f"meeting property 'hs_meeting_start_time': {INSTANT_EXPECTED}",
        "2026-13-01",
        id="meeting-start-malformed",
    ),
    pytest.param(
        _meeting({"hs_createdate": INSTANT_ISO_Z, "hs_meeting_start_time": UPDATED_EPOCH_MS}),
        "meeting property 'hs_meeting_start_time' must be a string",
        str(UPDATED_EPOCH_MS),
        id="meeting-start-number",
    ),
    pytest.param(
        _meeting({"hs_createdate": INSTANT_ISO_Z, "hs_timestamp": 1.5}),
        "meeting property 'hs_timestamp' must be a string",
        None,
        id="meeting-timestamp-float",
    ),
    pytest.param(
        _deal({}, id=""),
        "'deal.id' must not be blank",
        None,
        id="deal-id-blank",
    ),
    pytest.param(
        _deal({}, id=99200000001),
        "'deal.id' must be a string",
        "99200000001",
        id="deal-id-integer",
    ),
    pytest.param(
        lambda: parse_deal(_without({"id": DEAL_ID, "properties": {}, **_META}, "updatedAt")),
        "required field 'deal.updatedAt' is missing",
        None,
        id="deal-updated-at-missing",
    ),
    pytest.param(
        _deal({"createdate": INSTANT_ISO_Z, "dealstage": ["a"]}),
        "deal property 'dealstage' must be a string",
        None,
        id="deal-stage-array",
    ),
    pytest.param(
        _deal({"createdate": INSTANT_ISO_Z, "pipeline": "p\ud800"}),
        "deal property 'pipeline' must be valid UTF-8 text",
        None,
        id="deal-pipeline-unencodable",
    ),
    pytest.param(
        _deal({"createdate": "x"}),
        f"deal property 'createdate': {INSTANT_EXPECTED}",
        None,
        id="deal-createdate-malformed",
    ),
    pytest.param(
        _deal({}, createdAt="never"),
        f"'deal.createdAt': {INSTANT_EXPECTED}",
        "never",
        id="deal-created-at-malformed",
    ),
    *(
        pytest.param(
            _deal({"amount": amount}),
            "deal property 'amount' must be a string",
            None,
            id=f"deal-amount-{kind}",
        )
        for kind, amount in (("integer", 1500), ("decimal", Decimal("1500.50")), ("bool", False))
    ),
]


@pytest.mark.parametrize(("call", "message", "forbidden"), DEFECT_CASES)
def test_every_contract_breach_is_a_validation_defect_naming_only_the_field(
    call: Callable[[], object], message: str, forbidden: str | None
) -> None:
    with pytest.raises(QueryDefect) as caught:
        call()
    _assert_defect(caught.value, message, forbidden)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
