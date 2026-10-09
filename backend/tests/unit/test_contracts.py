"""Contract files are traceable to verified sources and consistent with the code that reads them.

AAP 0.11.2 ("External fixtures are traceable to verified contracts") and AAP 0.3.3 ("Contract
fidelity"): every external payload used in tests is rendered from a contract file under
``backend/tests/contracts/{google,linkedin,hubspot}/`` that records the official reference URL,
the API version or the page's last-modified date, the request shape and the documented sample,
and a fixture that carries a field its contract does not define fails. The simulator
(``ads_engine.simulation.payloads``) and the contract tests read the same files, so this module
guards the files themselves:

* inventory: exactly the 26 surfaces of ``payloads.SURFACES`` exist on disk, one file each, and
  nothing else sits beside them;
* shape: each file is UTF-8 JSON with a trailing newline and no tab, holds exactly the
  documented keys, names its own surface and source, carries its source's pinned API version,
  an ``https`` reference URL (HubSpot: its ``latest`` CRM reference page) and a last-modified
  date that is ``null`` or ``YYYY-MM-DD``, and loads through the simulator's own loader;
* samples and strictness: every documented sample satisfies its response schema (a contract
  without one must accept the minimal body built from its required members), and adding an
  undeclared top-level key, dropping a required one or answering with an unlisted status
  raises ``ContractViolation``, because ``additionalProperties`` defaults to false;
* request omissions: every surface a client calls (all but the virtual Google ``row_*``
  contracts) has a valid baseline request, rebuilt from the fixture manifest or, where the
  manifest records none, from its contract's URL template, that passes ``validate_request`` and
  is identified as its surface. Removing any one required header, query parameter or body
  member its contract file lists, nested members inside objects and array items included
  (``$.inputs[0].id``), raises ``ContractViolation`` naming that field, and
  ``contract_checked_transport`` refuses the request before its handler runs, while the intact
  baseline reaches the handler once;
* cross-checks with code: the Google row contracts select exactly the GAQL templates' fields
  and declare each of them, the webhook body is exactly ``{customer_id, query, page_token}``
  with the documented limits, the Lead Sync projection equals the client's ``LEAD_FIELDS`` and
  the AAP's literal, versioned LinkedIn surfaces require the version headers, and HubSpot
  paths are dated ``2026-09`` with no numbered version;
* captures: every redacted live capture under ``captured/`` names its surface, satisfies its
  contract and holds no email address in plaintext. Zero captures pass.

Files are read from disk only and requests go to an in-process ``MockTransport``: no test
touches the network or a database.
"""

import copy
import json
import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from functools import cache
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlencode

import httpx
import pytest

from ads_engine.connectors.google_n8n.queries import (
    KEYSET_LIMIT,
    MAX_IDS_PER_IN,
    PAGE_SIZE,
    QUERIES,
    rest_path,
)
from ads_engine.connectors.linkedin.client import LEAD_FIELDS
from ads_engine.settings import Source, get_settings
from ads_engine.simulation.payloads import (
    SURFACES,
    Contract,
    ContractViolation,
    check_schema,
    identify_surface,
    validate_documented_sample,
    validate_google_rows,
    validate_request,
    validate_response,
)

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------------------------
# The fixed contract catalogue
# ---------------------------------------------------------------------------------------------

BACKEND_DIR: Final = Path(__file__).resolve().parents[2]
CONTRACTS_DIR: Final = BACKEND_DIR / "tests" / "contracts"

EXPECTED_SURFACES: Final[Mapping[str, tuple[str, ...]]] = {
    "google": (
        "search",
        "row_customer",
        "row_campaign",
        "row_campaign_daily",
        "row_ad_group",
        "row_keyword_view",
        "row_search_term_view",
        "row_ad_group_criterion",
        "row_conversion_action",
    ),
    "linkedin": (
        "access_token",
        "ad_account",
        "ad_campaign_groups",
        "ad_campaigns",
        "creatives",
        "posts",
        "ad_analytics",
        "ad_analytics_statistics",
        "ad_analytics_campaign_delivery",
        "ad_analytics_member_segments",
        "lead_forms",
        "lead_form_responses",
        "lead_form_responses_batch",
    ),
    "hubspot": (
        "objects_batch_read",
        "crm_search",
        "associations_batch_read",
        "properties",
    ),
}
"""Every contract surface per source: 9 Google, 13 LinkedIn and 4 HubSpot files."""

EXPECTED_PAIRS: Final = frozenset(
    (source, name) for source, names in EXPECTED_SURFACES.items() for name in names
)

PINNED_API_VERSIONS: Final[Mapping[str, str]] = {
    "google": "v25",
    "linkedin": "202609",
    "hubspot": "2026-09",
}
"""The API version each source is pinned to (AAP 0.1.3, 0.2.4)."""

TOP_LEVEL_KEYS: Final = frozenset(
    {"surface", "source", "reference_url", "api_version", "last_modified", "request", "response"}
)
REQUEST_KEYS: Final = frozenset(
    {
        "method",
        "url",
        "required_headers",
        "required_query",
        "optional_query",
        "fields",
        "body",
        "gaql",
        "limits",
    }
)
RESPONSE_KEYS: Final = frozenset({"status", "body", "documented_sample"})
GAQL_KEYS: Final = frozenset({"query_id", "resource", "select"})
CAPTURE_KEYS: Final = frozenset({"surface", "captured_at", "request", "response"})

HUBSPOT_REFERENCE_PREFIX: Final = "https://developers.hubspot.com/docs/api-reference/latest/crm/"
ASSOCIATIONS_LAST_MODIFIED: Final = "2026-09-09"
"""The last-modified date of HubSpot's "Retrieve associations" reference page (AAP 0.2.4)."""

LEAD_FIELDS_LITERAL: Final = (
    "ownerInfo,associatedEntityInfo,leadMetadataInfo,owner,leadType,versionedLeadGenFormUrn,"
    "id,submittedAt,testLead,formResponse,form:(hiddenFields,creationLocale,name,id,content)"
)
"""LinkedIn's Lead Sync migration-guide projection, verbatim (AAP 0.2.4, 0.3.3)."""

WEBHOOK_BODY_KEYS: Final = frozenset({"customer_id", "query", "page_token"})
"""The n8n webhook request body the user prescribed, and nothing else (AAP 0.3.3)."""

VERSION_HEADERS: Final = ("Linkedin-Version", "X-Restli-Protocol-Version")
UNVERSIONED_LINKEDIN: Final = frozenset({"access_token"})
"""The OAuth token exchange on www.linkedin.com, which takes no Rest.li version headers."""

REDACTION_MARKER: Final = "<redacted>"
"""What ``ads-engine capture-fixtures`` writes in place of every email address."""

# A conservative address pattern: local@domain.tld. Strings are percent-decoded first, so an
# address encoded into a URL (`%40`) is found too.
EMAIL_LIKE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
ISO_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBERED_VERSION: Final = re.compile(r"/v\d+/")

UNDECLARED_KEY: Final = "undeclaredByTheContract"
UNLISTED_STATUS: Final = 418
"""An HTTP status no contract lists for a successful call."""

FIXTURES_DIR: Final = BACKEND_DIR / "tests" / "fixtures"
MANIFEST_PATH: Final = FIXTURES_DIR / "manifest.json"
"""The seeded fixture manifest: one recorded wire request per committed snapshot."""

JSON_MEDIA: Final = "application/json"
FORM_MEDIA: Final = "application/x-www-form-urlencoded"
LINKEDIN_REST_BASE: Final = "https://api.linkedin.com/rest/"
"""The base of LinkedIn's relative REST URL templates."""

SECRET_HEADER_PLACEHOLDER: Final = "{N8N_WEBHOOK_SECRET_HEADER}"
WEBHOOK_SECRET_HEADER: Final = "X-Webhook-Secret"
"""The webhook secret header name the test settings give the placeholder: their default."""

TEMPLATE_DONORS: Final[Mapping[tuple[str, str], tuple[str, str]]] = {
    ("linkedin", "ad_analytics_campaign_delivery"): ("linkedin", "ad_analytics"),
    ("linkedin", "ad_analytics_member_segments"): ("linkedin", "ad_analytics"),
}
"""The request surfaces no manifest entry records, each mapped to the recorded surface of the
same endpoint, whose URL and headers its baseline reuses; the query is the contract's own."""

TEMPLATE_VALUES: Final[Mapping[str, str]] = {
    "acct": "urn%3Ali%3AsponsoredAccount%3A990000001",
    "campaign": "urn%3Ali%3AsponsoredCampaign%3A990010000",
    "member_pivot": "MEMBER_JOB_FUNCTION",
}
"""Wire values of the URL-template placeholders: the manifest's first account and campaign,
URNs percent-encoded once as inside a Rest.li ``List(...)``, and a segment pivot. ``{y}``,
``{m}`` and ``{d}`` take the manifest's day, and ``{fields}`` the contract's projection."""

TEMPLATE_PLACEHOLDER: Final = re.compile(r"\{(\w+)\}")

JSON_IDENTIFIER: Final = re.compile(r"[A-Za-z_@$][A-Za-z0-9_@$\-]*")
"""A member name ``check_schema`` writes as ``.name`` in a JSON path; any other is
``["name"]``, JSON-quoted."""

EXPECTED_OMISSION_COUNTS: Final[Mapping[str, int]] = {"header": 47, "query": 51, "body": 21}
"""Required request members across the 18 request surfaces' contract files, per kind (body:
14 top-level members and 7 nested inside objects and array items), so the omission matrix
cannot shrink unnoticed when a contract loses one."""


def _param(source: str, name: str) -> Any:
    """One ``(source, name)`` parameter with the readable id ``source/name``."""
    return pytest.param(source, name, id=f"{source}/{name}")


CONTRACT_PARAMS: Final = [
    _param(source, name) for source, names in EXPECTED_SURFACES.items() for name in names
]
GOOGLE_ROW_NAMES: Final = tuple(name for name in EXPECTED_SURFACES["google"] if name != "search")
VERSIONED_LINKEDIN_NAMES: Final = tuple(
    name for name in EXPECTED_SURFACES["linkedin"] if name not in UNVERSIONED_LINKEDIN
)
LEAD_RESPONSE_NAMES: Final = ("lead_form_responses", "lead_form_responses_batch")
REQUEST_PAIRS: Final[tuple[tuple[Source, str], ...]] = tuple(
    (source, name)
    for source, names in SURFACES.items()
    for name in names
    if not (source == "google" and name.startswith("row_"))
)
"""Every surface a client sends a request to: all but the virtual Google ``row_*`` contracts."""
REQUEST_PARAMS: Final = [_param(source, name) for source, name in REQUEST_PAIRS]

ContractLoader = Callable[..., Contract]
Handler = Callable[[httpx.Request], httpx.Response]
CheckedTransport = Callable[[str, Handler], httpx.MockTransport]
BodySteps = tuple[str | int, ...]


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _contract_path(source: str, name: str) -> Path:
    """The file of one surface's contract."""
    return CONTRACTS_DIR / source / f"{name}.json"


def _read_raw(source: str, name: str) -> dict[str, Any]:
    """Decode one contract file as written, without the simulator's loader."""
    path = _contract_path(source, name)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{source}/{name}.json must hold a JSON object"
    return raw


def _minimal_value(schema: Mapping[str, Any]) -> Any:
    """Build the smallest value a contract schema accepts.

    Objects carry only their required members (a member the schema does not declare takes the
    ``additionalProperties`` schema, when that is one); arrays are empty; an enum takes its
    first option and a type list its first type. A schema without a type is an object when it
    declares members and ``null`` otherwise.
    """
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else list(kind or ())
    if not kinds:
        kinds = ["object"] if "properties" in schema or "required" in schema else ["null"]
    first = kinds[0]
    if first == "object":
        properties = schema.get("properties") or {}
        extra = schema.get("additionalProperties", False)
        value: dict[str, Any] = {}
        for member in schema.get("required") or ():
            sub = properties.get(member)
            if sub is None and isinstance(extra, dict):
                sub = extra
            value[member] = _minimal_value(sub if isinstance(sub, dict) else {})
        return value
    if first == "array":
        return []
    scalars: Mapping[str, Any] = {
        "string": "x",
        "integer": 0,
        "number": 0,
        "boolean": False,
        "null": None,
    }
    return scalars[first]


def _valid_body(loaded: Contract) -> dict[str, Any]:
    """A fresh valid success body: the documented sample, else the schema's minimal body.

    The loader caches contracts, so the sample is deep-copied before any test mutates it.
    """
    sample = loaded.response.documented_sample
    body = copy.deepcopy(sample) if sample is not None else _minimal_value(loaded.response.body)
    assert isinstance(body, dict), f"{loaded.surface}: a success body must be a JSON object"
    return body


def _declared_schema(schema: Mapping[str, Any], path: tuple[str, ...]) -> Mapping[str, Any] | None:
    """The member schema at ``path`` inside an object schema, or ``None`` when undeclared."""
    node: Mapping[str, Any] = schema
    for key in path:
        properties = node.get("properties")
        if not isinstance(properties, dict) or not isinstance(properties.get(key), dict):
            return None
        node = properties[key]
    return node


def _is_email_like(text: str) -> bool:
    """Whether ``text``, percent-decoded once, holds an email address."""
    return EMAIL_LIKE.search(unquote(text)) is not None


def _strings_with_paths(node: object, path: str = "$") -> Iterator[tuple[str, str]]:
    """Yield ``(json path, text)`` for every string value and object key inside ``node``.

    A key that holds an address is named ``[email-like key]`` in the paths, so a path never
    repeats the address it reports.
    """
    if isinstance(node, dict):
        for key, item in node.items():
            text = str(key)
            child = f"{path}.{'[email-like key]' if _is_email_like(text) else text}"
            yield f"{child} (key)", text
            yield from _strings_with_paths(item, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _strings_with_paths(item, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node


def _email_leaks(node: object) -> list[str]:
    """The JSON paths of every string that still holds an email address (values not echoed)."""
    return [path for path, text in _strings_with_paths(node) if _is_email_like(text)]


# ---------------------------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------------------------


def test_the_fixed_catalogue_equals_payloads_surfaces() -> None:
    surfaces = {source: tuple(names) for source, names in SURFACES.items()}
    assert set(surfaces) == set(EXPECTED_SURFACES)
    for source, names in surfaces.items():
        assert len(set(names)) == len(names), f"{source}: a surface is listed twice"
        assert set(names) == set(EXPECTED_SURFACES[source]), source
    assert len(EXPECTED_PAIRS) == 26


def test_the_contract_files_on_disk_are_exactly_the_fixed_catalogue(
    iter_contract_paths: Callable[[], Iterator[Path]],
) -> None:
    yielded = [(path.parent.name, path.stem) for path in iter_contract_paths()]
    assert len(set(yielded)) == len(yielded), "a contract file is yielded twice"
    assert set(yielded) == EXPECTED_PAIRS
    assert all("captured" not in path.parts for path in iter_contract_paths())
    # Nothing outside the three source directories: a contract the fixture would not reach.
    assert {path.name for path in CONTRACTS_DIR.iterdir()} == set(EXPECTED_SURFACES)
    on_disk = {(path.parent.name, path.stem) for path in CONTRACTS_DIR.glob("*/*.json")}
    assert on_disk == EXPECTED_PAIRS
    for source in EXPECTED_SURFACES:
        entries = {path.name for path in (CONTRACTS_DIR / source).iterdir()}
        expected = {f"{name}.json" for name in EXPECTED_SURFACES[source]} | {"captured"}
        assert entries == expected, f"{source}: unexpected entries {sorted(entries - expected)}"


# ---------------------------------------------------------------------------------------------
# Per-file shape
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_file_is_utf8_json_with_a_trailing_newline_and_no_tab(source: str, name: str) -> None:
    data = _contract_path(source, name).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{source}/{name}.json is not UTF-8 ({exc.reason})", pytrace=False)
    assert not text.startswith("\ufeff"), "no byte-order mark"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        pytest.fail(f"{source}/{name}.json is not JSON ({exc.msg})", pytrace=False)
    assert isinstance(raw, dict)
    assert text.endswith("\n")
    assert "\t" not in text


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_file_holds_exactly_the_documented_keys(source: str, name: str) -> None:
    raw = _read_raw(source, name)
    assert set(raw) == TOP_LEVEL_KEYS
    request, response = raw["request"], raw["response"]
    assert isinstance(request, dict)
    assert set(request) == REQUEST_KEYS
    assert isinstance(response, dict)
    assert set(response) == RESPONSE_KEYS


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_file_names_its_surface_its_source_and_the_pinned_version(
    source: str, name: str
) -> None:
    raw = _read_raw(source, name)
    assert raw["surface"] == f"{source}.{name}"
    assert raw["source"] == source == _contract_path(source, name).parent.name
    assert raw["api_version"] == PINNED_API_VERSIONS[source]


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_reference_url_is_an_https_official_page(source: str, name: str) -> None:
    url = _read_raw(source, name)["reference_url"]
    assert isinstance(url, str)
    assert url.startswith("https://")
    assert len(url) > len("https://")
    assert not any(character.isspace() for character in url)
    if source == "hubspot":
        assert url.startswith(HUBSPOT_REFERENCE_PREFIX)


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_last_modified_is_null_or_an_iso_calendar_date(source: str, name: str) -> None:
    value = _read_raw(source, name)["last_modified"]
    if value is None:
        return
    assert isinstance(value, str)
    assert ISO_DATE.fullmatch(value), value
    assert date.fromisoformat(value).isoformat() == value


def test_the_associations_contract_records_its_reference_page_date() -> None:
    assert _read_raw("hubspot", "associations_batch_read")["last_modified"] == (
        ASSOCIATIONS_LAST_MODIFIED
    )


@pytest.mark.parametrize("field", ["fromObjectTypeId", "toObjectTypeId"])
def test_association_object_type_ids_admit_the_live_null_and_reject_an_integer(
    contract: ContractLoader, field: str
) -> None:
    """Live 2026-09 responses carry null where the reference gives a string: both pass."""
    loaded = contract("hubspot", "associations_batch_read")

    def body(value: object) -> dict[str, Any]:
        spec = {"category": "HUBSPOT_DEFINED", "typeId": 200, "label": None, field: value}
        target = {"toObjectId": 99100000001, "associationTypes": [spec]}
        return {
            "status": "COMPLETE",
            "results": [{"from": {"id": "99000000001"}, "to": [target]}],
            "startedAt": "2026-10-08T12:00:00.100Z",
            "completedAt": "2026-10-08T12:00:00.250Z",
        }

    validate_response(loaded, body(None), status=200)
    validate_response(loaded, body("0-1"), status=200)
    with pytest.raises(ContractViolation) as caught:
        validate_response(loaded, body(1), status=200)
    assert caught.value.surface == "hubspot.associations_batch_read"
    assert caught.value.errors == (
        f"$.results[0].to[0].associationTypes[0].{field}: expected string or null, got integer",
    )


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_method_statuses_and_gaql_placement(source: str, name: str) -> None:
    raw = _read_raw(source, name)
    request, response = raw["request"], raw["response"]
    assert request["method"] in {"GET", "POST"}
    status = response["status"]
    assert isinstance(status, list)
    assert status
    assert all(isinstance(code, int) and not isinstance(code, bool) for code in status)
    assert all(100 <= code <= 599 for code in status)
    assert len(set(status)) == len(status)
    is_google_row = source == "google" and name.startswith("row_")
    assert (request["gaql"] is not None) is is_google_row


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_simulator_loader_reads_the_file_as_written(
    contract: ContractLoader, source: str, name: str
) -> None:
    loaded = contract(source, name)
    raw = _read_raw(source, name)
    assert loaded.path.resolve() == _contract_path(source, name).resolve()
    assert (loaded.surface, loaded.source, loaded.name) == (raw["surface"], source, name)
    assert loaded.reference_url == raw["reference_url"]
    assert loaded.api_version == raw["api_version"]
    assert loaded.last_modified == raw["last_modified"]
    assert loaded.request.method == raw["request"]["method"]
    assert loaded.request.url == raw["request"]["url"]
    assert loaded.request.fields == raw["request"]["fields"]
    assert list(loaded.request.required_headers) == raw["request"]["required_headers"]
    assert loaded.response.status == tuple(raw["response"]["status"])
    assert loaded.response.documented_sample == raw["response"]["documented_sample"]


# ---------------------------------------------------------------------------------------------
# Samples and validator strictness
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_the_documented_sample_satisfies_the_response_schema(
    contract: ContractLoader, source: str, name: str
) -> None:
    loaded = contract(source, name)
    status = loaded.response.status[0]
    sample = loaded.response.documented_sample
    if sample is None:
        # No official JSON sample (Google's field pages): the schema must still be satisfiable.
        body = _minimal_value(loaded.response.body)
        assert check_schema(loaded.response.body, body) == []
        validate_response(loaded, body, status=status)
        return
    validate_documented_sample(loaded)
    validate_response(loaded, copy.deepcopy(sample), status=status)


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_an_undeclared_top_level_key_breaks_the_contract(
    contract: ContractLoader, source: str, name: str
) -> None:
    loaded = contract(source, name)
    status = loaded.response.status[0]
    body = _valid_body(loaded)
    validate_response(loaded, body, status=status)
    body[UNDECLARED_KEY] = "x"
    with pytest.raises(ContractViolation, match=UNDECLARED_KEY) as caught:
        validate_response(loaded, body, status=status)
    assert caught.value.surface == f"{source}.{name}"
    assert f"$.{UNDECLARED_KEY}: unknown field" in caught.value.errors


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_dropping_a_required_top_level_key_breaks_the_contract(
    contract: ContractLoader, source: str, name: str
) -> None:
    loaded = contract(source, name)
    status = loaded.response.status[0]
    required = loaded.response.body.get("required")
    assert isinstance(required, list)
    assert required, f"{loaded.surface}: the success body must require at least one member"
    for member in required:
        assert isinstance(member, str)
        body = _valid_body(loaded)
        del body[member]
        with pytest.raises(ContractViolation) as caught:
            validate_response(loaded, body, status=status)
        assert f"$.{member}: missing required field" in caught.value.errors


@pytest.mark.parametrize(("source", "name"), CONTRACT_PARAMS)
def test_a_status_the_contract_does_not_list_is_rejected(
    contract: ContractLoader, source: str, name: str
) -> None:
    loaded = contract(source, name)
    assert UNLISTED_STATUS not in loaded.response.status
    with pytest.raises(ContractViolation, match=f"HTTP {UNLISTED_STATUS}"):
        validate_response(loaded, _valid_body(loaded), status=UNLISTED_STATUS)


# ---------------------------------------------------------------------------------------------
# Request validation: baselines and the omission matrix
# ---------------------------------------------------------------------------------------------


@cache
def _manifest() -> dict[str, Any]:
    """The fixture manifest, decoded once; callers deep-copy whatever they change."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{MANIFEST_PATH} must hold a JSON object"
    assert isinstance(raw.get("entries"), list), f"{MANIFEST_PATH} must list its entries"
    return raw


@cache
def _recorded_entries() -> dict[tuple[str, str], dict[str, Any]]:
    """The first manifest entry of each surface it records, keyed by ``(source, name)``."""
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in _manifest()["entries"]:
        source, _dot, name = entry["surface"].partition(".")
        assert source == entry["source"], f"{entry['file']}: surface and source disagree"
        entries.setdefault((source, name), entry)
    return entries


def _media_type(headers: Mapping[str, str]) -> str | None:
    """The media type of a recorded ``Content-Type`` header (any letter case), or ``None``."""
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return None


def _build_request(recorded: Mapping[str, Any]) -> httpx.Request:
    """Rebuild a recorded ``{method, url, query, headers, body}`` request as its client sends it.

    The reconstruction ``test_fixtures_determinism.build_request`` uses, repeated here because
    test modules never import one another: the URL carries no query string, and the
    ``[name, wire_value]`` pairs, already in wire form, are joined and attached unchanged (a
    second encoding pass would turn ``%3A`` into ``%253A``). A JSON body is sent as compact
    JSON, the LinkedIn token exchange as the form its ``Content-Type`` names, and headers as
    recorded, credentials redacted in place.
    """
    url = recorded["url"]
    assert isinstance(url, str), "a recorded url is a string"
    assert "?" not in url, f"{url}: the query travels as pairs, not in the url"
    assert "#" not in url, f"{url}: a request carries no fragment"
    raw = "&".join(f"{name}={value}" for name, value in recorded["query"])
    target = httpx.URL(url, query=raw.encode("ascii")) if raw else httpx.URL(url)
    headers = dict(recorded["headers"])
    body = recorded["body"]
    content: bytes | None = None
    if body is not None:
        assert isinstance(body, dict), f"{url}: a recorded body is a JSON object or a form"
        media = _media_type(headers)
        if media == FORM_MEDIA:
            assert all(isinstance(value, str) for value in body.values()), "form values: strings"
            content = urlencode(body).encode("ascii")
        else:
            assert media == JSON_MEDIA, f"{url}: a body needs a JSON or form Content-Type"
            content = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = httpx.Request(recorded["method"], target, headers=headers, content=content)
    assert request.url.query == raw.encode("ascii"), f"{url}: the query was re-encoded"
    return request


def _fill_template(text: str, values: Mapping[str, str]) -> str:
    """Replace every ``{placeholder}`` in a contract URL-template value with its wire value."""

    def _value(match: re.Match[str]) -> str:
        """The wire value of one placeholder; an unknown one fails the test."""
        name = match.group(1)
        assert name in values, f"no template value for {{{name}}}"
        return values[name]

    return TEMPLATE_PLACEHOLDER.sub(_value, text)


def _template_request(source: str, name: str, donor: Mapping[str, Any]) -> dict[str, Any]:
    """A recorded-form request built from a contract's own URL template.

    The template's query is split on ``&`` and each part's first ``=``, and its placeholders
    are filled (:data:`TEMPLATE_VALUES`, the manifest's day and the contract's ``fields``). It
    must name every required query parameter and nothing outside the contract. URL and
    headers come from ``donor``, the recorded request of the same endpoint.
    """
    spec = _read_raw(source, name)["request"]
    path, _mark, template_query = spec["url"].partition("?")
    url = LINKEDIN_REST_BASE + path
    assert url == donor["url"], f"{source}/{name}: the donor addresses another endpoint"
    day = date.fromisoformat(_manifest()["day"])
    values = {
        **TEMPLATE_VALUES,
        "y": str(day.year),
        "m": str(day.month),
        "d": str(day.day),
        "fields": spec["fields"],
    }
    pairs = [
        [key, _fill_template(value, values)]
        for key, _eq, value in (part.partition("=") for part in template_query.split("&"))
    ]
    names = [key for key, _value in pairs]
    assert len(set(names)) == len(names), f"{source}/{name}: the template repeats a parameter"
    assert set(spec["required_query"]) <= set(names), f"{source}/{name}: template incomplete"
    assert set(names) <= set(spec["required_query"]) | set(spec["optional_query"]), names
    return {
        "method": spec["method"],
        "url": url,
        "query": pairs,
        "headers": dict(donor["headers"]),
        "body": None,
    }


@cache
def _baselines() -> dict[tuple[str, str], dict[str, Any]]:
    """One valid recorded-form request per surface, keyed by ``(source, name)``.

    The manifest's first entry of a surface where it records one, otherwise the contract
    template of :data:`TEMPLATE_DONORS`. Never mutated: :func:`_baseline` hands out copies.
    """
    recorded = _recorded_entries()
    baselines = {key: entry["request"] for key, entry in recorded.items()}
    for key, donor in TEMPLATE_DONORS.items():
        assert key not in recorded, f"{key}: the manifest records it; drop its template donor"
        assert donor in recorded, f"{key}: the donor {donor} is not recorded in the manifest"
        baselines[key] = _template_request(*key, recorded[donor]["request"])
    return baselines


def _baseline(source: str, name: str) -> dict[str, Any]:
    """A fresh copy of a surface's baseline request in recorded form, safe to mutate."""
    baselines = _baselines()
    if (source, name) not in baselines:
        pytest.fail(
            f"{source}/{name}: no baseline request; record one in the fixture manifest or add "
            "a TEMPLATE_DONORS entry",
            pytrace=False,
        )
    return copy.deepcopy(baselines[(source, name)])


def _control_response(loaded: Contract) -> httpx.Response:
    """A contract-valid answer to a surface's baseline, for a transport handler to return.

    A recorded baseline gets its committed snapshot's response: the simulator's rendering,
    which the fixture tests hold to the contract, Google rows included. A template baseline
    gets the documented sample (else the minimal body) at the first listed status.
    """
    entry = _recorded_entries().get((loaded.source, loaded.name))
    if entry is None:
        return httpx.Response(loaded.response.status[0], json=_valid_body(loaded))
    snapshot = json.loads((FIXTURES_DIR / entry["file"]).read_text(encoding="utf-8"))
    response = snapshot["response"]
    return httpx.Response(response["status"], json=response["body"])


def _resolve_header(name: str) -> str:
    """A contract's required header name as the request carries it.

    ``{N8N_WEBHOOK_SECRET_HEADER}`` takes the current settings' name, which the isolated test
    environment leaves at its default; no other placeholder exists.
    """
    if name == SECRET_HEADER_PLACEHOLDER:
        resolved = get_settings().n8n_webhook_secret_header
        assert resolved == WEBHOOK_SECRET_HEADER
        return resolved
    assert not name.startswith("{"), f"unknown header placeholder {name}"
    return name


def _json_child(path: str, key: str) -> str:
    """The JSON path of member ``key`` under ``path``, written as ``check_schema`` writes it."""
    if JSON_IDENTIFIER.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _required_body_paths(
    schema: Mapping[str, Any], steps: BodySteps = (), path: str = "$"
) -> Iterator[tuple[BodySteps, str]]:
    """Yield ``(steps, path)`` for every required member of a request body schema.

    An object schema's own ``required`` names come first, then, depth-first in declaration
    order, those of each object schema reachable through ``properties`` and array ``items``,
    so the top-level members lead and nested ones such as ``$.inputs[0].id`` follow.
    ``steps`` leads from the body to the member: member names, and index 0 for an array item.
    ``path`` is the member's JSON path as ``check_schema`` reports it.
    """
    for member in schema.get("required") or ():
        yield (*steps, member), _json_child(path, member)
    for key, sub in (schema.get("properties") or {}).items():
        yield from _required_body_paths(sub, (*steps, key), _json_child(path, key))
    items = schema.get("items")
    if isinstance(items, dict):
        yield from _required_body_paths(items, (*steps, 0), f"{path}[0]")


def _body_member(path: str) -> str:
    """A body omission's member: its JSON path below ``$``, e.g. ``inputs[0].id``."""
    return path.removeprefix("$").removeprefix(".")


def _required_members(request: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, member)`` for each required header, query name and body member.

    ``request`` is a contract file's ``request`` object as written; kinds are ``header``,
    ``query`` and ``body``, in that order. A body member is named by its path
    (:func:`_body_member`), nested members inside objects and array items included.
    """
    for header in request["required_headers"]:
        yield "header", header
    for parameter in request["required_query"]:
        yield "query", parameter
    for _steps, path in _required_body_paths(request["body"] or {}):
        yield "body", _body_member(path)


def _body_requirement(source: str, name: str, member: str) -> tuple[BodySteps, str]:
    """The ``(steps, path)`` of one required body member of a surface's contract file."""
    schema = _read_raw(source, name)["request"]["body"] or {}
    found = [
        (steps, path)
        for steps, path in _required_body_paths(schema)
        if _body_member(path) == member
    ]
    assert len(found) == 1, f"{source}/{name}: {member} is not one required body member"
    return found[0]


def _holder(body: dict[str, Any], steps: BodySteps, surface: str) -> tuple[dict[str, Any], str]:
    """The object inside a recorded body that holds the member ``steps`` leads to, and its name.

    Every parent on the way must be held (an object member, or the array item at its index)
    and so must the member, so removing it is a real omission: a baseline lacking any of them
    fails the case instead of skipping it.
    """
    node: Any = body
    path = "$"
    for step in steps[:-1]:
        if isinstance(step, int):
            held = isinstance(node, list) and step < len(node)
            path = f"{path}[{step}]"
        else:
            held = isinstance(node, dict) and step in node
            path = _json_child(path, step)
        assert held, f"{surface}: the baseline body holds no {path}; record one that does"
        node = node[step]
    member = steps[-1]
    assert isinstance(member, str), f"{surface}: a required member is named, not indexed"
    assert isinstance(node, dict), f"{surface}: the baseline's {path} is not an object"
    assert member in node, f"{surface}: the baseline body lacks {_json_child(path, member)}"
    return node, member


def _omission_id(source: str, name: str, kind: str, member: str) -> str:
    """The readable case id ``source/name-kind-member``, placeholder braces dropped."""
    return f"{source}/{name}-{kind}-{member.strip('{}')}"


# Enumerated from the contract files at collection, so a required member a contract adds is
# a new case at once; EXPECTED_OMISSION_COUNTS catches one a contract loses.
OMISSION_CASES: Final[tuple[tuple[Source, str, str, str], ...]] = tuple(
    (source, name, kind, member)
    for source, name in REQUEST_PAIRS
    for kind, member in _required_members(_read_raw(source, name)["request"])
)
OMISSION_PARAMS: Final = [pytest.param(*case, id=_omission_id(*case)) for case in OMISSION_CASES]


def _omitted(source: str, name: str, kind: str, member: str) -> tuple[httpx.Request, str]:
    """A surface's baseline with one required member removed, and the error that must name it.

    A header is removed from the rebuilt request in any letter case, so the body keeps the
    encoding its recorded ``Content-Type`` names even when that header is the one removed. A
    query parameter is dropped from the recorded pairs, and a body member, at its path (index
    0 of an array), from a copy of the recorded body, which is then re-encoded.
    """
    recorded = _baseline(source, name)
    if kind == "header":
        resolved = _resolve_header(member)
        request = _build_request(recorded)
        headers = request.headers.copy()
        assert resolved in headers, f"{source}/{name}: the baseline lacks {resolved}"
        del headers[resolved]
        omitted = httpx.Request(
            request.method, request.url, headers=headers, content=request.content
        )
        assert all(key.lower() != resolved.lower() for key in omitted.headers), resolved
        return omitted, f"header {resolved}: missing or empty"
    if kind == "query":
        pairs = recorded["query"]
        kept = [pair for pair in pairs if pair[0] != member]
        assert len(kept) == len(pairs) - 1, f"{source}/{name}: {member} must be sent once"
        recorded["query"] = kept
        return _build_request(recorded), f"query parameter {member}: missing (required)"
    assert kind == "body", f"unknown omission kind {kind}"
    body = recorded["body"]
    assert isinstance(body, dict), f"{source}/{name}: the baseline has no body"
    steps, path = _body_requirement(source, name, member)
    holder, key = _holder(body, steps, f"{source}/{name}")
    del holder[key]
    return _build_request(recorded), f"body {path}: missing required field"


def test_every_request_surface_has_one_baseline() -> None:
    request_surfaces = {
        (source, name)
        for source, names in SURFACES.items()
        for name in names
        if not (source == "google" and name.startswith("row_"))
    }
    assert len(request_surfaces) == 18
    assert set(REQUEST_PAIRS) == request_surfaces
    assert set(_baselines()) == request_surfaces
    assert set(_baselines()) - set(_recorded_entries()) == set(TEMPLATE_DONORS)


@pytest.mark.parametrize(("source", "name"), REQUEST_PARAMS)
def test_the_baseline_request_passes_its_contract_and_names_its_surface(
    contract: ContractLoader, source: Source, name: str
) -> None:
    request = _build_request(_baseline(source, name))
    assert identify_surface(source, request) == name
    validate_request(contract(source, name), request)


def test_the_omission_matrix_is_every_required_request_member(contract: ContractLoader) -> None:
    derived: list[tuple[str, str, str, str]] = []
    for source, name in REQUEST_PAIRS:
        spec = contract(source, name).request
        derived.extend((source, name, "header", header) for header in spec.required_headers)
        derived.extend((source, name, "query", parameter) for parameter in spec.required_query)
        # The loaded body schema, walked here with an explicit stack rather than through
        # _required_body_paths: each object schema's required names, then depth-first those of
        # every schema below it. A map's value schema has no fixed path, so a required member
        # there must fail here rather than leave the matrix without its omission.
        pending: list[tuple[Mapping[str, Any], str, bool]] = (
            [(spec.body, "$", False)] if spec.body is not None else []
        )
        while pending:
            schema, path, in_map = pending.pop()
            required = schema.get("required") or []
            assert isinstance(required, list)
            assert not (in_map and required), f"{source}/{name} {path}: no omission path"
            derived.extend(
                (source, name, "body", _body_member(_json_child(path, str(member))))
                for member in required
            )
            properties = schema.get("properties") or {}
            assert isinstance(properties, dict)
            children = [(sub, _json_child(path, key), in_map) for key, sub in properties.items()]
            items = schema.get("items")
            if isinstance(items, dict):
                children.append((items, f"{path}[0]", in_map))
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                children.append((extra, f"{path}.*", True))
            pending.extend(reversed(children))
    assert list(OMISSION_CASES) == derived
    assert len(set(OMISSION_CASES)) == len(OMISSION_CASES)
    assert Counter(kind for _source, _name, kind, _member in OMISSION_CASES) == (
        EXPECTED_OMISSION_COUNTS
    )
    assert {(source, name) for source, name, _kind, _member in OMISSION_CASES} == set(REQUEST_PAIRS)
    ids = [_omission_id(*case) for case in OMISSION_CASES]
    assert len(set(ids)) == len(ids)


def test_the_webhook_secret_header_placeholder_resolves_to_the_settings_name() -> None:
    headers = _read_raw("google", "search")["request"]["required_headers"]
    assert SECRET_HEADER_PLACEHOLDER in headers
    assert get_settings().n8n_webhook_secret_header == WEBHOOK_SECRET_HEADER
    assert _resolve_header(SECRET_HEADER_PLACEHOLDER) == WEBHOOK_SECRET_HEADER
    assert WEBHOOK_SECRET_HEADER in _baseline("google", "search")["headers"]


@pytest.mark.parametrize(("source", "name", "kind", "member"), OMISSION_PARAMS)
def test_omitting_a_required_request_member_raises_a_field_specific_violation(
    contract: ContractLoader, source: Source, name: str, kind: str, member: str
) -> None:
    loaded = contract(source, name)
    validate_request(loaded, _build_request(_baseline(source, name)))
    request, expected = _omitted(source, name, kind, member)
    with pytest.raises(ContractViolation) as caught:
        validate_request(loaded, request)
    assert caught.value.surface == f"{source}.{name}"
    assert expected in caught.value.errors, caught.value.errors


@pytest.mark.parametrize(("source", "name"), REQUEST_PARAMS)
def test_the_contract_checked_transport_hands_a_baseline_to_its_handler_once(
    contract: ContractLoader,
    contract_checked_transport: CheckedTransport,
    source: Source,
    name: str,
) -> None:
    loaded = contract(source, name)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the call and answer with a contract-valid body."""
        calls.append(request)
        return _control_response(loaded)

    request = _build_request(_baseline(source, name))
    with httpx.Client(transport=contract_checked_transport(source, handler)) as client:
        response = client.send(request)
    assert len(calls) == 1
    assert (calls[0].method, calls[0].url, calls[0].content) == (
        request.method,
        request.url,
        request.content,
    )
    expected = _control_response(loaded)
    assert response.status_code == expected.status_code
    assert response.json() == expected.json()


@pytest.mark.parametrize(("source", "name", "kind", "member"), OMISSION_PARAMS)
def test_the_contract_checked_transport_refuses_an_omission_before_its_handler(
    contract: ContractLoader,
    contract_checked_transport: CheckedTransport,
    source: Source,
    name: str,
    kind: str,
    member: str,
) -> None:
    loaded = contract(source, name)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the call; the transport must never get this far."""
        calls.append(request)
        return _control_response(loaded)

    request, _expected = _omitted(source, name, kind, member)
    # Removing a routing selector (pivot, q, ids) may address another surface or none, so the
    # refusal is asserted, not the surface it names.
    refused = rf"^(request breaks the {source}\.\w+ contract|{source}: the request matches no)"
    with (
        httpx.Client(transport=contract_checked_transport(source, handler)) as client,
        pytest.raises(AssertionError, match=refused),
    ):
        client.send(request)
    assert calls == []


# ---------------------------------------------------------------------------------------------
# Cross-checks with code
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", GOOGLE_ROW_NAMES, ids=[f"google/{n}" for n in GOOGLE_ROW_NAMES])
def test_each_google_row_contract_selects_exactly_its_gaql_template(
    contract: ContractLoader, name: str
) -> None:
    gaql = _read_raw("google", name)["request"]["gaql"]
    assert isinstance(gaql, dict)
    assert set(gaql) == GAQL_KEYS
    assert gaql["query_id"] in QUERIES
    template = QUERIES[gaql["query_id"]]
    assert gaql["select"] == list(template.select)
    assert gaql["resource"] == template.resource
    assert template.row_surface == name
    spec = contract("google", name).request.gaql
    assert spec is not None
    assert (spec.query_id, spec.resource, spec.select) == (
        template.query_id,
        template.resource,
        template.select,
    )


@pytest.mark.parametrize("name", GOOGLE_ROW_NAMES, ids=[f"google/{n}" for n in GOOGLE_ROW_NAMES])
def test_each_selected_field_is_declared_by_its_row_schema(
    contract: ContractLoader, name: str
) -> None:
    loaded = contract("google", name)
    assert loaded.request.gaql is not None
    undeclared = [
        field
        for field in loaded.request.gaql.select
        if _declared_schema(loaded.response.body, rest_path(field)) is None
    ]
    assert undeclared == [], f"{loaded.surface}: SELECT fields without a schema"


def test_every_gaql_template_reads_rows_of_a_contract_with_its_select() -> None:
    for query_id, template in QUERIES.items():
        assert template.row_surface in GOOGLE_ROW_NAMES, query_id
        gaql = _read_raw("google", template.row_surface)["request"]["gaql"]
        assert gaql["select"] == list(template.select), query_id
        assert gaql["resource"] == template.resource, query_id
    assert {template.row_surface for template in QUERIES.values()} == set(GOOGLE_ROW_NAMES)


def test_the_campaign_row_selects_the_lifetime_budget_and_q_g2b_shares_its_select() -> None:
    raw = _read_raw("google", "row_campaign")
    select = raw["request"]["gaql"]["select"]
    assert "campaign_budget.amount_micros" in select
    assert "campaign_budget.total_amount_micros" in select
    assert list(QUERIES["Q-G2"].select) == select
    assert list(QUERIES["Q-G2b"].select) == select
    assert QUERIES["Q-G2b"].row_surface == "row_campaign"
    budget = _declared_schema(raw["response"]["body"], ("campaignBudget",))
    assert budget is not None
    assert "totalAmountMicros" in budget["properties"]


def test_the_webhook_body_is_exactly_customer_id_query_and_page_token() -> None:
    request = _read_raw("google", "search")["request"]
    assert request["method"] == "POST"
    assert request["gaql"] is None
    body = request["body"]
    assert isinstance(body, dict)
    assert body["type"] == "object"
    assert set(body["properties"]) == WEBHOOK_BODY_KEYS
    assert len(body["required"]) == len(WEBHOOK_BODY_KEYS)
    assert set(body["required"]) == WEBHOOK_BODY_KEYS
    assert body.get("additionalProperties", False) is False
    assert body["properties"]["customer_id"]["type"] == "string"
    assert body["properties"]["query"]["type"] == "string"
    assert set(body["properties"]["page_token"]["type"]) == {"string", "null"}


def test_the_webhook_limits_match_the_connector_constants() -> None:
    limits = _read_raw("google", "search")["request"]["limits"]
    assert limits["page_size"] == 10_000 == PAGE_SIZE
    assert limits["keyset_limit"] == 9_999 == KEYSET_LIMIT
    assert limits["in_list_max"] == 1_000 == MAX_IDS_PER_IN
    assert limits["timeout_s"] == 120


@pytest.mark.parametrize(
    "name", LEAD_RESPONSE_NAMES, ids=[f"linkedin/{n}" for n in LEAD_RESPONSE_NAMES]
)
def test_the_lead_sync_projection_equals_lead_fields_and_the_aap_literal(
    contract: ContractLoader, name: str
) -> None:
    fields = _read_raw("linkedin", name)["request"]["fields"]
    assert fields == LEAD_FIELDS_LITERAL
    assert LEAD_FIELDS == LEAD_FIELDS_LITERAL
    assert contract("linkedin", name).request.fields == LEAD_FIELDS_LITERAL


@pytest.mark.parametrize(
    "name", VERSIONED_LINKEDIN_NAMES, ids=[f"linkedin/{n}" for n in VERSIONED_LINKEDIN_NAMES]
)
def test_versioned_linkedin_surfaces_require_the_version_headers(name: str) -> None:
    headers = _read_raw("linkedin", name)["request"]["required_headers"]
    for header in VERSION_HEADERS:
        assert header in headers, f"linkedin/{name}: {header} is not required"


@pytest.mark.parametrize(
    "name", EXPECTED_SURFACES["hubspot"], ids=[f"hubspot/{n}" for n in EXPECTED_SURFACES["hubspot"]]
)
def test_hubspot_paths_are_dated_2026_09_and_never_numbered(name: str) -> None:
    url = _read_raw("hubspot", name)["request"]["url"]
    assert "/2026-09/" in url
    assert "/v3/" not in url
    assert "/v4/" not in url
    assert NUMBERED_VERSION.search(url) is None, url


# ---------------------------------------------------------------------------------------------
# Redacted live captures
# ---------------------------------------------------------------------------------------------


def test_the_email_guard_flags_plaintext_and_accepts_the_marker() -> None:
    redacted = {
        "request": {"url": f"https://example.invalid/x?email={REDACTION_MARKER}"},
        "response": {"body": {"email": REDACTION_MARKER, "note": f"from {REDACTION_MARKER}"}},
    }
    assert _email_leaks(redacted) == []
    leaking = {
        "a": ["kept", "person@example.com"],
        "b": {"url": "https://example.invalid/?email=person%40example.com"},
        "person@example.com": 1,
    }
    assert _email_leaks(leaking) == [
        "$.a[1]",
        "$.b.url",
        "$.[email-like key] (key)",
    ]


@pytest.mark.parametrize("source", tuple(EXPECTED_SURFACES), ids=tuple(EXPECTED_SURFACES))
def test_every_capture_names_its_surface_satisfies_its_contract_and_holds_no_email(
    captured_payloads: Callable[[str], list[dict[str, Any]]],
    contract: ContractLoader,
    source: str,
) -> None:
    directory = CONTRACTS_DIR / source / "captured"
    assert directory.is_dir(), f"{source}: captured/ must exist (it holds .gitkeep when empty)"
    strays = sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name != ".gitkeep" and (entry.suffix != ".json" or not entry.is_file())
    )
    assert strays == [], f"{source}/captured: only .gitkeep and *.json files belong here"
    paths = sorted(directory.glob("*.json"))
    captures = captured_payloads(source)
    assert len(captures) == len(paths)
    for path, capture in zip(paths, captures, strict=True):
        shown = f"{source}/captured/{path.name}"
        assert set(capture) == CAPTURE_KEYS, shown
        assert path.stem in EXPECTED_SURFACES[source], f"{shown}: not a {source} surface"
        assert capture["surface"] == f"{source}.{path.stem}", shown
        captured_at = capture["captured_at"]
        assert isinstance(captured_at, str), shown
        assert datetime.fromisoformat(captured_at).tzinfo is not None, shown
        response = capture["response"]
        assert isinstance(response, dict), shown
        status = response.get("status")
        assert isinstance(status, int), shown
        assert not isinstance(status, bool), shown
        body = response.get("body")
        loaded = contract(source, path.stem)
        validate_response(loaded, body, status=status)
        if loaded.surface == "google.search" and 200 <= status < 300:
            request_body = capture["request"]["body"]
            assert isinstance(request_body, dict), f"{shown}: the webhook body must be JSON"
            validate_google_rows(request_body["query"], body)
        leaks = _email_leaks(capture)
        assert leaks == [], f"{shown}: replace every address with {REDACTION_MARKER!r}: {leaks}"
