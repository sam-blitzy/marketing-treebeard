"""The seeded simulator snapshots in ``backend/tests/fixtures/`` reproduce byte for byte.

AAP 0.1.1 (determinism: simulated data is seeded, so tests and demos reproduce exactly),
AAP 0.6.1 (``tests/fixtures/``: scenario payloads rendered from the contracts by the simulator)
and AAP 0.11.2 (every external payload used in tests is rendered from a contract file, and a
test fails when a fixture carries a field its contract does not define). This module is the one
consumer that keeps the fixture directory honest:

* **Manifest** (``fixtures/manifest.json``): the world (scenario ``default``, seed 42, the fixed
  clock), the day the dated requests read, and one recorded request per snapshot, 25 entries in
  a fixed order. The directory holds exactly the snapshots the manifest names.
* **Request reconstruction**: :func:`build_request` rebuilds each recorded request exactly as
  its client sends it; the query pairs are already in wire form and are attached verbatim.
* **Determinism**: each rebuilt request is answered by
  :func:`~ads_engine.simulation.payloads.render` from a freshly built world. The canonical JSON
  of ``{source, surface, scenario, seed, request, response}`` must equal the committed file
  byte for byte, and independently built worlds must render the same bytes.
* **Contract conformance**: every rebuilt request passes ``validate_request`` and addresses its
  declared surface; every committed body passes ``validate_response`` at its status, and every
  Google row its ``row_*`` contract. The HubSpot continuation re-sends exactly the cursors the
  first page returned.
* **Privacy and isolation**: every email is on ``sim.invalid``, every credential is redacted,
  and every LinkedIn, Google and HubSpot id lies in the simulator's reserved blocks.

After an intended simulator change, re-render the snapshots from ``backend/`` with::

    uv run pytest tests/unit/test_fixtures_determinism.py --update-fixtures --no-cov

The update run compares what it wrote, so a following plain run passes. No database and no
network: ``render`` is called directly, without a transport.
"""

import difflib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal, cast
from urllib.parse import parse_qsl, unquote, urlencode

import httpx
import pytest

from ads_engine.simulation import (
    DEFAULT_SCENARIO,
    DEFAULT_SEED,
    GOOGLE_AD_GROUP_IDS,
    GOOGLE_BUDGET_IDS,
    GOOGLE_CAMPAIGN_IDS,
    GOOGLE_CONVERSION_ACTION_IDS,
    GOOGLE_CRITERION_IDS,
    HUBSPOT_CONTACT_IDS,
    HUBSPOT_DEAL_IDS,
    HUBSPOT_HASHED_CONTACT_IDS,
    HUBSPOT_MEETING_IDS,
    LINKEDIN_ACCOUNT_IDS,
    LINKEDIN_CAMPAIGN_GROUP_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    LINKEDIN_CREATIVE_IDS,
    LINKEDIN_FORM_IDS,
    LINKEDIN_POST_IDS,
    SIM_EMAIL_DOMAIN,
    SIM_GOOGLE_CUSTOMER_ID,
    IdRange,
    is_simulated_email,
    is_simulated_id,
    payloads,
)
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.payloads import JSONValue
from ads_engine.simulation.scenarios import SCENARIOS, fixed_clock

pytestmark = pytest.mark.contract

# ---------------------------------------------------------------------------------------------
# Paths and the manifest's fixed vocabulary
# ---------------------------------------------------------------------------------------------

# Every test runs from backend/ (tests/conftest.py changes directory in pytest_configure), the
# same working directory payloads.contracts_root() resolves ./tests/contracts against.
FIXTURES_DIR: Final = Path("tests/fixtures")
MANIFEST_PATH: Final = FIXTURES_DIR / "manifest.json"
UPDATE_HINT: Final = (
    "uv run pytest tests/unit/test_fixtures_determinism.py --update-fixtures --no-cov"
)

type SourceName = Literal["google", "linkedin", "hubspot"]

SOURCES: Final[tuple[SourceName, ...]] = ("google", "linkedin", "hubspot")

# The snapshot names per source, in manifest order; files are `{source}__{name}.json`.
SNAPSHOT_NAMES: Final[Mapping[SourceName, tuple[str, ...]]] = {
    "google": ("q_g1", "q_g2", "q_g2b", "q_g3", "q_g4", "q_g5", "q_g6", "q_g7", "q_g8"),
    "linkedin": (
        "access_token",
        "ad_account",
        "ad_campaign_groups",
        "ad_campaigns",
        "creatives",
        "posts",
        "ad_analytics",
        "ad_analytics_statistics",
        "lead_forms",
        "lead_form_responses",
        "lead_form_responses_batch",
    ),
    "hubspot": (
        "properties",
        "objects_batch_read",
        "crm_search",
        "associations_batch_read",
        "associations_batch_read_continuation",
    ),
}
EXPECTED_FILES: Final[tuple[str, ...]] = tuple(
    f"{source}__{name}.json" for source, names in SNAPSHOT_NAMES.items() for name in names
)

# The HubSpot pair whose second call continues the first page's per-input cursors.
ASSOCIATIONS_FILE: Final = "hubspot__associations_batch_read.json"
CONTINUATION_FILE: Final = "hubspot__associations_batch_read_continuation.json"

# The row contract each Google query answers with (Q-G2b re-reads campaigns, so it shares
# Q-G2's row_campaign contract).
GOOGLE_ROW_SURFACES: Final[Mapping[str, str]] = {
    "google__q_g1.json": "row_customer",
    "google__q_g2.json": "row_campaign",
    "google__q_g2b.json": "row_campaign",
    "google__q_g3.json": "row_campaign_daily",
    "google__q_g4.json": "row_ad_group",
    "google__q_g5.json": "row_keyword_view",
    "google__q_g6.json": "row_search_term_view",
    "google__q_g7.json": "row_ad_group_criterion",
    "google__q_g8.json": "row_conversion_action",
}

MANIFEST_KEYS: Final = frozenset({"scenario", "seed", "clock", "day", "entries"})
ENTRY_KEYS: Final = frozenset({"file", "source", "surface", "request"})
REQUEST_KEYS: Final = frozenset({"method", "url", "headers", "query", "body"})
SNAPSHOT_KEYS: Final = frozenset({"source", "surface", "scenario", "seed", "request", "response"})
RESPONSE_KEYS: Final = frozenset({"status", "body"})

REDACTED: Final = "<redacted>"
JSON_MEDIA: Final = "application/json"
FORM_MEDIA: Final = "application/x-www-form-urlencoded"
# The n8n webhook's shared-secret header: the N8N_WEBHOOK_SECRET_HEADER default, which the
# root conftest restores for every test.
WEBHOOK_SECRET_HEADER: Final = "X-Webhook-Secret"
# Headers that carry protocol values, never credentials; any other recorded header is a
# credential and must be redacted.
PROTOCOL_HEADERS: Final = frozenset(
    {"content-type", "linkedin-version", "x-restli-protocol-version", "x-restli-method"}
)
# The credential fields of the LinkedIn refresh-token exchange form.
TOKEN_FORM_SECRETS: Final = ("client_id", "client_secret", "refresh_token")

# How many unified-diff lines a snapshot mismatch shows before truncating.
DIFF_LINES_SHOWN: Final = 80

EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
GAQL_DATE_RANGE_RE: Final = re.compile(
    r"segments\.date BETWEEN '(\d{4}-\d{2}-\d{2})' AND '(\d{4}-\d{2}-\d{2})'"
)
RESTLI_RANGE_END_RE: Final = re.compile(r"end:\(year:(\d+),month:(\d+),day:(\d+)\)")
GOOGLE_CUSTOMER_PATH_RE: Final = re.compile(r"customers/(\d+)/")

# Simple LinkedIn URNs by entity type, and the reserved block each type's ids come from.
LINKEDIN_URN_RANGES: Final[Mapping[str, IdRange]] = {
    "sponsoredAccount": LINKEDIN_ACCOUNT_IDS,
    "sponsoredCampaignGroup": LINKEDIN_CAMPAIGN_GROUP_IDS,
    "sponsoredCampaign": LINKEDIN_CAMPAIGN_IDS,
    "sponsoredCreative": LINKEDIN_CREATIVE_IDS,
    "leadGenForm": LINKEDIN_FORM_IDS,
    "adForm": LINKEDIN_FORM_IDS,
    "share": LINKEDIN_POST_IDS,
    "ugcPost": LINKEDIN_POST_IDS,
}
LINKEDIN_URN_RE: Final = re.compile(
    r"urn:li:(" + "|".join(sorted(LINKEDIN_URN_RANGES, key=len, reverse=True)) + r"):(\d+)"
)
LINKEDIN_ACCOUNT_PATH_RE: Final = re.compile(r"/adAccounts/(\d+)")

# Google row resources that carry an `id`, and the reserved block of each.
GOOGLE_RESOURCE_RANGES: Final[Mapping[str, IdRange]] = {
    "campaign": GOOGLE_CAMPAIGN_IDS,
    "campaignBudget": GOOGLE_BUDGET_IDS,
    "adGroup": GOOGLE_AD_GROUP_IDS,
    "conversionAction": GOOGLE_CONVERSION_ACTION_IDS,
}

# HubSpot CRM object types and the reserved blocks their ids come from. Contacts of simulated
# people and seeded contacts answering real lead emails use separate blocks.
HUBSPOT_OBJECT_RANGES: Final[Mapping[str, tuple[IdRange, ...]]] = {
    "contacts": (HUBSPOT_CONTACT_IDS, HUBSPOT_HASHED_CONTACT_IDS),
    "meetings": (HUBSPOT_MEETING_IDS,),
    "deals": (HUBSPOT_DEAL_IDS,),
}


# ---------------------------------------------------------------------------------------------
# Manifest and snapshot access
# ---------------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    """Decode a UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def _manifest() -> dict[str, Any]:
    """The fixture manifest, read once per session (it is never rewritten by this module)."""
    raw = _read_json(MANIFEST_PATH)
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise TypeError(f"{MANIFEST_PATH} must be an object with an `entries` list")
    return cast(dict[str, Any], raw)


def _entries() -> list[dict[str, Any]]:
    """The manifest entries, in manifest order."""
    return cast(list[dict[str, Any]], _manifest()["entries"])


def _stem(entry: Mapping[str, Any]) -> str:
    """The snapshot file stem of an entry, its readable test id."""
    return Path(cast(str, entry["file"])).stem


def _params(source: SourceName | None = None) -> list[Any]:
    """One ``pytest.param`` per manifest entry (of ``source`` only, when given)."""
    return [
        pytest.param(entry, id=_stem(entry))
        for entry in _entries()
        if source is None or entry["source"] == source
    ]


ENTRY_PARAMS: Final = _params()
GOOGLE_PARAMS: Final = _params("google")


def _source(entry: Mapping[str, Any]) -> SourceName:
    """The entry's source, checked against the three known sources."""
    source = entry["source"]
    assert source in SOURCES, f"{entry['file']}: unknown source {source!r}"
    return cast(SourceName, source)


def _surface_name(entry: Mapping[str, Any]) -> str:
    """The surface name within the source: ``search`` for ``google.search``."""
    prefix, _dot, name = cast(str, entry["surface"]).partition(".")
    message = f"{entry['file']}: surface {entry['surface']!r} must be '{entry['source']}.<name>'"
    assert prefix == entry["source"], message
    assert name, message
    return name


def _expected_surface(file: str) -> str:
    """The ``{source}.{name}`` surface a snapshot file must declare."""
    source, _sep, name = Path(file).stem.partition("__")
    if source == "google":
        return "google.search"
    if file == CONTINUATION_FILE:
        return "hubspot.associations_batch_read"
    return f"{source}.{name}"


def _snapshot_path(entry: Mapping[str, Any]) -> Path:
    """The committed snapshot file of an entry."""
    return FIXTURES_DIR / cast(str, entry["file"])


def _snapshot(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The committed snapshot of an entry, decoded."""
    path = _snapshot_path(entry)
    if not path.is_file():
        pytest.fail(f"{path} is missing; render it with: {UPDATE_HINT}", pytrace=False)
    return cast(dict[str, Any], _read_json(path))


def _entry_named(file: str) -> dict[str, Any]:
    """The manifest entry whose snapshot is ``file``."""
    for entry in _entries():
        if entry["file"] == file:
            return entry
    pytest.fail(f"{MANIFEST_PATH} has no entry for {file}", pytrace=False)


# ---------------------------------------------------------------------------------------------
# Request reconstruction and rendering
# ---------------------------------------------------------------------------------------------


def _media_type(headers: Mapping[str, str]) -> str | None:
    """The media type of a recorded ``Content-Type`` header (any letter case), or ``None``."""
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return None


def build_request(entry: Mapping[str, Any]) -> httpx.Request:
    """Rebuild a manifest entry's request exactly as its client sends it.

    The manifest keeps the URL without its query string and the query as ordered
    ``[name, wire_value]`` pairs already in wire form: Rest.li structure characters stay raw
    and URNs are already percent-encoded. The pairs are therefore joined unchanged and attached
    without re-encoding; a second encoding pass would turn ``%3A`` into ``%253A``, which live
    LinkedIn rejects. A JSON body is sent as compact JSON; the LinkedIn token exchange is a
    form, encoded as its ``Content-Type`` says. Headers are copied as recorded, credentials
    redacted in place.

    Args:
        entry: A manifest entry.

    Returns:
        The request, with its body already in ``content``.
    """
    recorded = cast(Mapping[str, Any], entry["request"])
    url = cast(str, recorded["url"])
    assert "?" not in url, f"{entry['file']}: url must carry no query string"
    assert "#" not in url, f"{entry['file']}: url must carry no fragment"
    pairs = cast(Sequence[Sequence[str]], recorded["query"])
    raw = "&".join(f"{name}={value}" for name, value in pairs)
    target = httpx.URL(url, query=raw.encode("ascii")) if raw else httpx.URL(url)
    headers = dict(cast(Mapping[str, str], recorded["headers"]))
    body = recorded["body"]
    content: bytes | None = None
    if body is not None:
        media = _media_type(headers)
        if media == FORM_MEDIA:
            message = f"{entry['file']}: a form body must map field names to strings"
            assert isinstance(body, dict), message
            assert all(isinstance(value, str) for value in body.values()), message
            content = urlencode(cast(dict[str, str], body)).encode("ascii")
        else:
            assert media == JSON_MEDIA, (
                f"{entry['file']}: a body needs Content-Type {JSON_MEDIA} or {FORM_MEDIA}, "
                f"recorded {media!r}"
            )
            content = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = httpx.Request(cast(str, recorded["method"]), target, headers=headers, content=content)
    assert request.url.query == raw.encode("ascii"), (
        f"{entry['file']}: the query was re-encoded: {request.url.query!r} != {raw!r}"
    )
    return request


def _default_world() -> SimulatedWorld:
    """A fresh world of the manifest: the default scenario, seed 42, the fixed clock."""
    return SimulatedWorld(DEFAULT_SEED, SCENARIOS[DEFAULT_SCENARIO], clock=fixed_clock)


def _rendered_text(entry: Mapping[str, Any], world: SimulatedWorld) -> str:
    """The canonical snapshot text of an entry's request answered by ``world``.

    Args:
        entry: A manifest entry.
        world: The world that answers the rebuilt request.

    Returns:
        ``canonical_json`` of ``{source, surface, scenario, seed, request, response}``, where
        ``request`` is the entry's recorded request and ``response`` the rendered status and
        body.
    """
    rendered = payloads.render(_source(entry), build_request(entry), world)
    assert not rendered.timeout, f"{entry['file']}: the default world must answer, not time out"
    snapshot: dict[str, Any] = {
        "source": entry["source"],
        "surface": entry["surface"],
        "scenario": DEFAULT_SCENARIO,
        "seed": DEFAULT_SEED,
        "request": entry["request"],
        "response": {"status": rendered.status, "body": rendered.body},
    }
    return payloads.canonical_json(cast(JSONValue, snapshot))


def _diff(expected: str, actual: str, *, expected_label: str, actual_label: str) -> str:
    """A unified diff of two texts, truncated to :data:`DIFF_LINES_SHOWN` lines."""
    lines = list(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=expected_label,
            tofile=actual_label,
            lineterm="",
        )
    )
    shown = lines[:DIFF_LINES_SHOWN]
    if len(lines) > DIFF_LINES_SHOWN:
        shown.append(f"... {len(lines) - DIFF_LINES_SHOWN} more diff lines")
    return "\n".join(shown)


# ---------------------------------------------------------------------------------------------
# Phase 1: the manifest
# ---------------------------------------------------------------------------------------------


def test_manifest_names_the_default_world() -> None:
    """The manifest names scenario ``default``, seed 42 and the fixed clock, in UTC."""
    manifest = _manifest()
    assert set(manifest) == MANIFEST_KEYS
    assert manifest["scenario"] == DEFAULT_SCENARIO == "default"
    assert manifest["scenario"] in SCENARIOS
    assert type(manifest["seed"]) is int
    assert manifest["seed"] == DEFAULT_SEED == 42
    clock = datetime.fromisoformat(cast(str, manifest["clock"]))
    assert clock.utcoffset() == timedelta(0), "the manifest clock must be ISO-8601 UTC"
    assert clock == fixed_clock()


def test_manifest_day_is_the_last_complete_day_the_dated_requests_read() -> None:
    """``day`` is the day before the clock's UTC date, and every dated request ends on it.

    Google dated queries read ``segments.date BETWEEN … AND 'day'`` and LinkedIn analytics a
    ``dateRange`` ending on ``day``, so the snapshots are one consistent read of the world.
    """
    manifest = _manifest()
    day = date.fromisoformat(cast(str, manifest["day"]))
    assert day == fixed_clock().astimezone(UTC).date() - timedelta(days=1)
    ends: list[tuple[str, date]] = []
    for entry in _entries():
        recorded = entry["request"]
        if entry["source"] == "google":
            for _start, end in GAQL_DATE_RANGE_RE.findall(cast(str, recorded["body"]["query"])):
                ends.append((cast(str, entry["file"]), date.fromisoformat(end)))
        for name, value in recorded["query"]:
            if name == "dateRange":
                match = RESTLI_RANGE_END_RE.search(cast(str, value))
                assert match is not None, f"{entry['file']}: dateRange {value!r} has no end"
                year, month, day_of_month = (int(part) for part in match.groups())
                ends.append((cast(str, entry["file"]), date(year, month, day_of_month)))
    assert ends, "the manifest records no dated request"
    assert [(file, end) for file, end in ends if end != day] == []


def test_manifest_lists_every_snapshot_once_in_order() -> None:
    """25 entries, one per snapshot file, in the fixed source and surface order."""
    files = [entry["file"] for entry in _entries()]
    assert len(files) == len(EXPECTED_FILES) == 25
    assert len(set(files)) == len(files), "snapshot file names must be unique"
    assert files == list(EXPECTED_FILES)


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_manifest_entry_shape(entry: dict[str, Any]) -> None:
    """Each entry records its file, source, surface and a wire request without a query string."""
    assert set(entry) == ENTRY_KEYS
    source = _source(entry)
    assert cast(str, entry["file"]).startswith(f"{source}__")
    assert entry["surface"] == _expected_surface(cast(str, entry["file"]))
    recorded = entry["request"]
    assert set(recorded) == REQUEST_KEYS
    assert recorded["method"] in {"GET", "POST"}
    url = httpx.URL(cast(str, recorded["url"]))
    assert url.scheme == "https"
    assert url.query == b"", "the query lives in `query`, not `url`"
    assert url.fragment == ""
    pairs = recorded["query"]
    assert isinstance(pairs, list)
    for pair in pairs:
        assert isinstance(pair, list), f"query pair {pair!r}"
        assert len(pair) == 2, f"query pair {pair!r}"
        assert all(isinstance(part, str) and part for part in pair), f"query pair {pair!r}"
    headers = recorded["headers"]
    assert isinstance(headers, dict)
    assert headers, "every recorded request carries its headers"
    assert all(isinstance(name, str) and isinstance(value, str) for name, value in headers.items())
    if recorded["method"] == "GET":
        assert recorded["body"] is None, "a GET request carries no body"


# ---------------------------------------------------------------------------------------------
# Phase 2: request reconstruction
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_build_request_reproduces_the_recorded_wire_request(entry: dict[str, Any]) -> None:
    """The rebuilt request carries the recorded method, URL, pairs, headers and body verbatim."""
    recorded = entry["request"]
    request = build_request(entry)
    assert request.method == recorded["method"]
    assert str(request.url.copy_with(query=None)) == recorded["url"]
    query = request.url.query.decode("ascii")
    sent_pairs = [list(pair.split("=", 1)) for pair in query.split("&")] if query else []
    assert sent_pairs == recorded["query"]
    for name, value in recorded["headers"].items():
        assert request.headers.get(name) == value, f"header {name} was not sent as recorded"
    body = recorded["body"]
    if body is None:
        assert request.content == b""
    elif _media_type(recorded["headers"]) == FORM_MEDIA:
        assert dict(parse_qsl(request.content.decode("ascii"), strict_parsing=True)) == body
    else:
        assert request.content == json.dumps(body, separators=(",", ":")).encode("utf-8")
        assert json.loads(request.content) == body


# ---------------------------------------------------------------------------------------------
# Phase 3: byte-for-byte determinism
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_snapshot_reproduces_byte_for_byte(entry: dict[str, Any], update_fixtures: bool) -> None:
    """A fresh seeded world renders the committed snapshot exactly, byte for byte.

    With ``--update-fixtures`` the rendered bytes are written first, only where they differ, and
    then compared like in any other run, so an update run leaves only files a plain run accepts.
    """
    path = _snapshot_path(entry)
    rendered = _rendered_text(entry, _default_world())
    encoded = rendered.encode("utf-8")
    if update_fixtures and (not path.is_file() or path.read_bytes() != encoded):
        path.write_bytes(encoded)
    if not path.is_file():
        pytest.fail(f"{path} is missing; render it with: {UPDATE_HINT}", pytrace=False)
    on_disk = path.read_bytes()
    if on_disk != encoded:
        diff = _diff(
            on_disk.decode("utf-8", errors="replace"),
            rendered,
            expected_label=f"{path} (committed)",
            actual_label=f"{path} (rendered, seed {DEFAULT_SEED})",
        )
        pytest.fail(
            f"{path} differs from what the simulator renders.\n{diff}\n"
            f"If the simulator change is intended, re-render with: {UPDATE_HINT}",
            pytrace=False,
        )


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_independent_worlds_render_identical_bytes(entry: dict[str, Any]) -> None:
    """Separately built worlds, and a repeat call on one world, answer with the same bytes.

    One world is built directly and one through ``Scenario.world``; both must be the same
    world. Repeating the request on the first world proves rendering keeps no state that leaks
    into a later answer.
    """
    scenario = SCENARIOS[DEFAULT_SCENARIO]
    direct = SimulatedWorld(DEFAULT_SEED, scenario, clock=fixed_clock)
    via_scenario = scenario.world(DEFAULT_SEED, clock=fixed_clock)
    first = _rendered_text(entry, direct)
    texts = {
        "Scenario.world": _rendered_text(entry, via_scenario),
        "repeat on the same world": _rendered_text(entry, direct),
    }
    for label, text in texts.items():
        if text != first:
            diff = _diff(first, text, expected_label="first world", actual_label=label)
            pytest.fail(f"{entry['file']}: {label} renders different bytes\n{diff}", pytrace=False)


def test_canonical_json_is_the_shared_canonical_form() -> None:
    """``canonical_json`` is sorted, two-space indented, non-ASCII kept, newline-terminated.

    The manifest itself is stored in that form, so every file in the directory shares it.
    """
    manifest = _manifest()
    expected = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert payloads.canonical_json(cast(JSONValue, manifest)) == expected
    assert MANIFEST_PATH.read_bytes() == expected.encode("utf-8")
    sample: JSONValue = {"b": "é", "a": [1, None, True]}
    assert payloads.canonical_json(sample) == (
        '{\n  "a": [\n    1,\n    null,\n    true\n  ],\n  "b": "é"\n}\n'
    )


def test_fixture_directory_holds_exactly_the_manifest_snapshots() -> None:
    """Every ``*.json`` beside the manifest is a manifest entry's snapshot, and vice versa.

    Defined after the snapshot test on purpose: an ``--update-fixtures`` run writes the
    snapshot of a newly added entry before this check reads the directory. A stale snapshot
    is never deleted automatically; this check names it.
    """
    on_disk = {path.name for path in FIXTURES_DIR.glob("*.json") if path != MANIFEST_PATH}
    named = {cast(str, entry["file"]) for entry in _entries()}
    assert sorted(on_disk - named) == [], "snapshots no manifest entry names"
    assert sorted(named - on_disk) == [], f"snapshots missing on disk; render with: {UPDATE_HINT}"


# ---------------------------------------------------------------------------------------------
# Phase 4: contract conformance of every snapshot
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_snapshot_records_its_manifest_entry(entry: dict[str, Any]) -> None:
    """The snapshot names the manifest's world and records the entry's request unchanged."""
    snapshot = _snapshot(entry)
    assert set(snapshot) == SNAPSHOT_KEYS
    assert snapshot["source"] == entry["source"]
    assert snapshot["surface"] == entry["surface"]
    assert snapshot["scenario"] == _manifest()["scenario"]
    assert snapshot["seed"] == _manifest()["seed"]
    assert snapshot["request"] == entry["request"]
    response = snapshot["response"]
    assert set(response) == RESPONSE_KEYS
    assert type(response["status"]) is int


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_request_satisfies_its_surface_contract(entry: dict[str, Any]) -> None:
    """The rebuilt request addresses its declared surface and passes that surface's contract."""
    source = _source(entry)
    name = _surface_name(entry)
    request = build_request(entry)
    assert payloads.identify_surface(source, request) == name
    payloads.validate_request(payloads.load_contract(source, name), request)


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_response_satisfies_its_surface_contract(entry: dict[str, Any]) -> None:
    """The committed body is a success the contract lists, with no field it does not define."""
    contract = payloads.load_contract(_source(entry), _surface_name(entry))
    response = _snapshot(entry)["response"]
    status = cast(int, response["status"])
    assert 200 <= status < 300, f"{entry['file']}: fixtures record successful answers"
    payloads.validate_response(contract, cast(JSONValue, response["body"]), status=status)


@pytest.mark.parametrize("entry", GOOGLE_PARAMS)
def test_google_rows_satisfy_their_row_contracts(entry: dict[str, Any]) -> None:
    """Every Google row passes the ``row_*`` contract of its query; no page token is relayed.

    The n8n webhook answers ``{results[], fieldMask, queryResourceConsumption}`` and carries
    no ``nextPageToken`` (AAP 0.2.4), so a snapshot holding one would encode a shape the live
    relay never sends.
    """
    query = cast(str, entry["request"]["body"]["query"])
    surface = payloads.row_surface_for(query)
    assert surface == GOOGLE_ROW_SURFACES[cast(str, entry["file"])]
    body = _snapshot(entry)["response"]["body"]
    assert isinstance(body, dict)
    assert "nextPageToken" not in body
    rows = body.get("results")
    assert isinstance(rows, list)
    assert rows, f"{entry['file']}: the snapshot holds no rows to check"
    schema = payloads.load_contract("google", surface).response.body
    errors = [
        problem
        for index, row in enumerate(rows)
        for problem in payloads.check_schema(schema, row, f"$.results[{index}]")
    ]
    assert errors == []
    # The envelope's fieldMask must name exactly the selected fields.
    payloads.validate_google_rows(query, cast(JSONValue, body))


def test_associations_continuation_resends_exactly_the_returned_cursors() -> None:
    """The continuation call re-sends ``{id, after}`` for exactly the inputs that had more pages.

    HubSpot's ``2026-09`` associations batch read pages per input: an input whose result
    carries ``paging.next.after`` is re-sent with that cursor alongside its id, verbatim, to the
    same endpoint (AAP 0.2.4).
    """
    first_entry = _entry_named(ASSOCIATIONS_FILE)
    continuation_entry = _entry_named(CONTINUATION_FILE)
    assert continuation_entry["request"]["url"] == first_entry["request"]["url"]
    results = _snapshot(first_entry)["response"]["body"]["results"]
    expected = sorted(
        (
            {"id": result["from"]["id"], "after": result["paging"]["next"]["after"]}
            for result in results
            if "after" in result.get("paging", {}).get("next", {})
        ),
        key=lambda pair: (pair["id"], pair["after"]),
    )
    assert expected, f"{ASSOCIATIONS_FILE} returns no cursor for its continuation to follow"
    sent = _snapshot(continuation_entry)["request"]["body"]["inputs"]
    assert len(sent) == len(expected), "each paged input is continued exactly once"
    assert sorted(sent, key=lambda pair: (pair["id"], pair.get("after", ""))) == expected
    continued_ids = {pair["id"] for pair in expected}
    answered = _snapshot(continuation_entry)["response"]["body"]["results"]
    assert {result["from"]["id"] for result in answered} <= continued_ids


# ---------------------------------------------------------------------------------------------
# Phase 5: privacy and isolation
# ---------------------------------------------------------------------------------------------


def _decoded_texts(path: Path) -> tuple[str, str]:
    """A fixture file's text as stored and percent-decoded once.

    Query values keep URNs (and could keep emails) percent-encoded, so each scan reads both.
    """
    text = path.read_text(encoding="utf-8")
    return text, unquote(text)


def test_every_email_is_on_the_reserved_domain() -> None:
    """Every email in the fixture directory belongs to a simulated person on ``sim.invalid``.

    Failures name the file and the offending domain only, so a real address is never echoed.
    """
    found = 0
    offenders: set[str] = set()
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        for text in _decoded_texts(path):
            for match in EMAIL_RE.findall(text):
                found += 1
                if not (match.endswith(f"@{SIM_EMAIL_DOMAIN}") and is_simulated_email(match)):
                    offenders.add(f"{path.name}: an address on {match.rpartition('@')[2]!r}")
    assert found, "the fixtures carry no email, so this check guards nothing"
    assert sorted(offenders) == []


@pytest.mark.parametrize("entry", ENTRY_PARAMS)
def test_credentials_are_redacted(entry: dict[str, Any]) -> None:
    """Every credential header and token-form secret is recorded as ``<redacted>``.

    Any header other than the protocol headers is a credential. Authorization headers are
    recorded as ``Bearer <redacted>``, not a bare ``<redacted>``: the LinkedIn and HubSpot
    contracts require a Bearer credential, so only the credential after the scheme is redacted
    and the rebuilt request still passes its contract.

    Each comparison is reduced to a boolean before it is asserted, so a failure never echoes
    the credential it found.
    """
    bearer = f"Bearer {REDACTED}"
    for label, recorded in (
        ("manifest", entry["request"]),
        ("snapshot", _snapshot(entry)["request"]),
    ):
        headers = {name.lower(): value for name, value in recorded["headers"].items()}
        for name, value in headers.items():
            if name in PROTOCOL_HEADERS:
                continue
            redacted = value == (bearer if name == "authorization" else REDACTED)
            assert redacted, f"{label} header {name} is not redacted"
        if entry["source"] == "google":
            secret_redacted = headers.get(WEBHOOK_SECRET_HEADER.lower()) == REDACTED
            assert secret_redacted, f"{label}: the webhook call needs a redacted secret header"
        elif entry["surface"] == "linkedin.access_token":
            body = recorded["body"]
            assert set(body) == {*TOKEN_FORM_SECRETS, "grant_type"}
            assert body["grant_type"] == "refresh_token"
            for field in TOKEN_FORM_SECRETS:
                field_redacted = body[field] == REDACTED
                assert field_redacted, f"{label} token form field {field} is not redacted"
        else:
            bearer_redacted = headers.get("authorization") == bearer
            assert bearer_redacted, f"{label}: a {entry['source']} call needs a redacted bearer"


def test_linkedin_ids_lie_in_the_reserved_blocks() -> None:
    """Every LinkedIn account, campaign, group, creative, form and post id is a simulated one."""
    seen: dict[str, int] = dict.fromkeys(LINKEDIN_URN_RANGES, 0)
    problems: list[str] = []

    def check(path: Path, kind: str, value: str) -> None:
        """Record ``value`` as an id of ``kind``, noting it when it leaves its block."""
        seen[kind] += 1
        urn = f"urn:li:{kind}:{value}"
        if not (is_simulated_id("linkedin", urn) and LINKEDIN_URN_RANGES[kind].contains(value)):
            problems.append(f"{path.name}: {urn}")

    for entry in _entries():
        if entry["source"] != "linkedin":
            continue
        path = _snapshot_path(entry)
        for text in _decoded_texts(path):
            for kind, value in LINKEDIN_URN_RE.findall(text):
                check(path, kind, value)
            for value in LINKEDIN_ACCOUNT_PATH_RE.findall(text):
                check(path, "sponsoredAccount", value)
    account_entry = _entry_named("linkedin__ad_account.json")
    account = _snapshot(account_entry)["response"]["body"]
    check(_snapshot_path(account_entry), "sponsoredAccount", str(account["id"]))
    assert seen["sponsoredAccount"], seen
    assert seen["sponsoredCreative"], seen
    assert problems == []


def test_google_ids_lie_in_the_reserved_blocks() -> None:
    """The customer and every campaign, budget, ad group, criterion and action id is simulated."""
    problems: list[str] = []
    checked: dict[str, int] = {"customer": 0, "campaign": 0}

    def check(path: Path, resource: str, value: str, block: IdRange | None) -> None:
        """Note ``value`` when it is not a simulated id (in ``block``, when one is given)."""
        checked[resource] = checked.get(resource, 0) + 1
        if resource == "customer":
            in_block = value == SIM_GOOGLE_CUSTOMER_ID
        else:
            in_block = block is None or block.contains(value)
        if not (is_simulated_id("google", value) and in_block):
            problems.append(f"{path.name}: {resource} {value}")

    for entry in _entries():
        if entry["source"] != "google":
            continue
        path = _snapshot_path(entry)
        snapshot = _snapshot(entry)
        check(path, "customer", cast(str, snapshot["request"]["body"]["customer_id"]), None)
        for value in GOOGLE_CUSTOMER_PATH_RE.findall(path.read_text(encoding="utf-8")):
            check(path, "customer", value, None)
        for row in snapshot["response"]["body"]["results"]:
            for resource, fields in row.items():
                if not isinstance(fields, dict):
                    continue
                if "id" in fields:
                    block = GOOGLE_RESOURCE_RANGES.get(resource)
                    check(path, resource, str(fields["id"]), block)
                if "criterionId" in fields:
                    check(path, "criterion", str(fields["criterionId"]), GOOGLE_CRITERION_IDS)
    assert checked["customer"], checked
    assert checked["campaign"], checked
    assert problems == []


def _hubspot_object_ids(entry: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield ``(object type, id)`` for every CRM object id a HubSpot snapshot carries.

    Object types come from the request path: ``/crm/objects/2026-09/{type}/…`` and
    ``/crm/associations/2026-09/{from}/{to}/batch/read``. Inputs of a lookup by email are
    emails, not ids, and are covered by the email check instead.
    """
    snapshot = _snapshot(entry)
    segments = httpx.URL(cast(str, entry["request"]["url"])).path.strip("/").split("/")
    request_body = snapshot["request"]["body"] or {}
    response_body = snapshot["response"]["body"]
    kind = segments[1]
    if kind == "objects":
        object_type = segments[3]
        if request_body.get("idProperty") is None:
            for item in request_body.get("inputs", []):
                yield object_type, str(item["id"])
        for result in response_body.get("results", []):
            yield object_type, str(result["id"])
            hs_object_id = result.get("properties", {}).get("hs_object_id")
            if hs_object_id is not None:
                yield object_type, str(hs_object_id)
    elif kind == "associations":
        from_type, to_type = segments[3], segments[4]
        for item in request_body["inputs"]:
            yield from_type, str(item["id"])
        for result in response_body.get("results", []):
            yield from_type, str(result["from"]["id"])
            for target in result.get("to", []):
                yield to_type, str(target["toObjectId"])
        for error in response_body.get("errors", []):
            for identifier in error.get("context", {}).get("fromObjectId", []):
                yield from_type, str(identifier)


def test_hubspot_ids_lie_in_the_reserved_blocks() -> None:
    """Every HubSpot contact, meeting and deal id lies in its reserved simulated block."""
    problems: list[str] = []
    checked: dict[str, int] = {}
    for entry in _entries():
        if entry["source"] != "hubspot":
            continue
        for object_type, identifier in _hubspot_object_ids(entry):
            checked[object_type] = checked.get(object_type, 0) + 1
            blocks = HUBSPOT_OBJECT_RANGES.get(object_type)
            in_block = blocks is not None and any(block.contains(identifier) for block in blocks)
            if not (is_simulated_id("hubspot", identifier) and in_block):
                problems.append(f"{entry['file']}: {object_type} {identifier}")
    assert checked.get("contacts"), checked
    assert problems == []
