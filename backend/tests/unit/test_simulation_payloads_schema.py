"""Behavioural tests of the contract schema validator in ``ads_engine.simulation.payloads``.

AAP 0.3.3 ("Contract fidelity") and AAP 0.11.2 ("External fixtures are traceable to verified
contracts"): every simulated body must satisfy its contract, and a fixture carrying a field its
contract does not define, or omitting a required one, fails. ``check_schema`` first runs a
compiled boolean fast path and walks the value again for messages only when the fast path
rejects it, so the fast path must reject exactly what the recursive checker reports:

* a map whose ``additionalProperties`` is a value schema (the LinkedIn posts ``results`` map),
  a closed ``{"type": "object"}`` member, a member that only lists ``required`` and a member
  with an empty schema (closed by default) are always descended;
* scalar members stay on the one-type-lookup path and still report type mismatches;
* for every contract, the fast path and the recursive checker agree on the documented sample,
  on every rendered manifest body (Google rows against their ``row_*`` contracts), on every
  manifest request body and on their single-point mutations (an undeclared member, a dropped
  member, a value of another type), made at every position the value reaches.

Every rendered body comes from ``simulation.payloads.render`` over the seeded world the
fixture manifest names; no test encodes a platform payload of its own. A socket guard fails any
test that tries to open a network connection.
"""

import functools
import itertools
import json
import os
import socket
from collections.abc import Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

import pytest
from pydantic import AliasChoices

from ads_engine.analysis.thresholds import Thresholds
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.simulation import payloads
from ads_engine.simulation.generator import DEFAULT_SPEC, SimulatedWorld
from ads_engine.simulation.payloads import (
    Contract,
    JSONValue,
    RequestShape,
    check_schema,
    identify_surface,
    iter_contracts,
    load_contract,
    render,
    row_surface_for,
    validate_google_rows,
    validate_response,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "manifest.json"
FORM_MEDIA = "application/x-www-form-urlencoded"
# A member name no contract declares, injected by the mutation checks.
UNDECLARED = "undeclaredByTheSchemaTest"
POST_URN = "urn:li:share:1"


@functools.cache
def _manifest() -> dict[str, Any]:
    """The fixture manifest: the world it was rendered from and one request per fixture."""
    return cast(dict[str, Any], json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


@functools.cache
def _world() -> SimulatedWorld:
    """The manifest's world: its seed, the default scenario and its fixed clock.

    The default spec has no faults, so one world serves every test of this module.
    """
    manifest = _manifest()
    assert manifest["scenario"] == DEFAULT_SPEC.name
    now = datetime.fromisoformat(manifest["clock"])
    config = Thresholds.load(REPO_ROOT / "config")
    return SimulatedWorld(
        manifest["seed"],
        DEFAULT_SPEC,
        clock=lambda: now,
        content_assets=config.content_assets,
        mapping=config.mapping,
    )


def _shape(request: Mapping[str, Any]) -> RequestShape:
    """The request a manifest entry records, rebuilt exactly as its client sends it.

    The manifest keeps the URL without its query string and the query as ordered
    ``[name, value]`` pairs whose values are already in wire form, so they are joined
    unchanged. A form body (the OAuth token exchange) is form-encoded; any other body is
    compact JSON. Credentials are redacted in place (``Bearer <redacted>``, ``<redacted>``),
    which still satisfies the header rules.
    """
    url = cast(str, request["url"])
    pairs = cast(list[list[str]], request["query"])
    if pairs:
        url += "?" + "&".join(f"{name}={value}" for name, value in pairs)
    headers = cast(dict[str, str], request["headers"])
    body = request["body"]
    content_type = next(
        (value for name, value in headers.items() if name.lower() == "content-type"), ""
    )
    if body is None:
        content = b""
    elif content_type.split(";", 1)[0].strip().lower() == FORM_MEDIA:
        content = urlencode(cast(dict[str, str], body)).encode("ascii")
    else:
        content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return RequestShape(method=request["method"], url=url, headers=headers, body=content)


def _entry_id(entry: Mapping[str, Any]) -> str:
    """A readable test id: the fixture file the entry renders."""
    return cast(str, entry["file"])


def _recursive_errors(schema: Mapping[str, JSONValue], value: JSONValue) -> list[str]:
    """The violations the recursive checker reports, sorted, bypassing the fast path."""
    errors: list[str] = []
    payloads._check(schema, value, "$", errors)
    return sorted(errors)


def _fast_valid(schema: Mapping[str, JSONValue], value: JSONValue) -> bool:
    """The compiled fast path's verdict alone."""
    return payloads._valid(payloads._compiled(schema), value)


def _retyped(value: JSONValue) -> JSONValue:
    """A value of another JSON type: ``0`` for a string, a string for anything else."""
    return 0 if isinstance(value, str) else "retyped"


def _mutations(
    value: JSONValue, path: str = "$", position: str = "$", seen: set[str] | None = None
) -> Iterator[tuple[str, JSONValue]]:
    """Yield ``(description, copy)`` for each single-point mutation of ``value``.

    Every value, container or scalar, is replaced by one of another JSON type; every object
    gets an undeclared member, and loses each member in turn. Each kind of mutation is made
    once per position, a JSON path with its array indices dropped: the elements of an array
    share one item schema, so the first element that reaches a position stands for the rest,
    and a body of many elements stays linear to check. Object keys, map keys included, stay
    in the position. Only the ancestors of the mutated value are copied, so the original is
    never changed.
    """
    done = set() if seen is None else seen

    def first(kind: str) -> bool:
        """Whether this kind of mutation is new at this position, recording it."""
        key = f"{position} {kind}"
        if key in done:
            return False
        done.add(key)
        return True

    if first("retyped"):
        yield f"{path} retyped", _retyped(value)
    if isinstance(value, dict):
        if first("+"):
            yield f"{path} +{UNDECLARED}", {**value, UNDECLARED: True}
        for key, member in value.items():
            if first(f"-{key}"):
                yield f"{path} -{key}", {name: item for name, item in value.items() if name != key}
            inner = _mutations(member, f"{path}[{key!r}]", f"{position}[{key!r}]", done)
            for label, mutated in inner:
                yield label, {**value, key: mutated}
    elif isinstance(value, list):
        for index, item in enumerate(value):
            inner = _mutations(item, f"{path}[{index}]", f"{position}[]", done)
            for label, mutated in inner:
                yield label, [*value[:index], mutated, *value[index + 1 :]]


def _assert_agreement(schema: Mapping[str, JSONValue], value: JSONValue, label: str) -> None:
    """Assert ``value`` is valid and the two paths agree on it and on each of its mutations.

    Agreement means the fast path accepts a candidate exactly when the recursive checker
    reports nothing, and :func:`check_schema` returns exactly the recursive checker's errors.
    """
    assert _recursive_errors(schema, value) == [], f"{label}: the original is invalid"
    problems: list[str] = []
    for description, candidate in itertools.chain([("original", value)], _mutations(value)):
        errors = _recursive_errors(schema, candidate)
        fast = _fast_valid(schema, candidate)
        if fast is bool(errors) or check_schema(schema, candidate) != errors:
            problems.append(f"{label} {description}: fast path {fast}, recursive {errors}")
    assert not problems, "\n".join(problems[:20])


def _settings_env_names() -> frozenset[str]:
    """Return, lowercased, every environment name a ``Settings`` field can be read from."""
    names: set[str] = set()
    for field_name, field in Settings.model_fields.items():
        names.add(field_name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                names.add(alias)
            elif isinstance(alias, AliasChoices):
                names.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(name.lower() for name in names)


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove every variable ``Settings`` reads, in any letter case, and reset the cache.

    The host exports real credentials, and request validation reads the configured webhook
    secret header, so no test reads the host.
    """
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test on any attempt to resolve a host or open a connection."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the schema tests attempted network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# ---------------------------------------------------------------------------------------------
# Map values: the LinkedIn posts ``results`` map
# ---------------------------------------------------------------------------------------------


def _posts() -> Contract:
    """The LinkedIn posts batch-get contract, whose ``results`` is a map of post values."""
    return load_contract("linkedin", "posts")


def test_a_post_map_value_with_an_undeclared_key_and_missing_fields_reports_all_four() -> None:
    schema = _posts().response.body
    body: JSONValue = {"results": {POST_URN: {"undeclared": True}}, "statuses": {}, "errors": {}}

    assert check_schema(schema, body) == [
        '$.results["urn:li:share:1"].author: missing required field',
        '$.results["urn:li:share:1"].id: missing required field',
        '$.results["urn:li:share:1"].lifecycleState: missing required field',
        '$.results["urn:li:share:1"].undeclared: unknown field',
    ]
    assert _fast_valid(schema, body) is False


def test_a_post_map_value_that_is_not_an_object_is_rejected() -> None:
    schema = _posts().response.body
    body: JSONValue = {"results": {POST_URN: "a post"}, "statuses": {}, "errors": {}}

    assert check_schema(schema, body) == [
        '$.results["urn:li:share:1"]: expected object, got string'
    ]


def test_a_post_map_value_valid_per_its_schema_passes() -> None:
    contract = _posts()
    sample = cast(dict[str, JSONValue], contract.response.documented_sample)
    post = next(iter(cast(dict[str, JSONValue], sample["results"]).values()))
    body: JSONValue = {"results": {POST_URN: post}, "statuses": {}, "errors": {}}

    assert check_schema(contract.response.body, sample) == []
    assert check_schema(contract.response.body, body) == []
    assert _fast_valid(contract.response.body, body) is True


# ---------------------------------------------------------------------------------------------
# Object members without named properties
# ---------------------------------------------------------------------------------------------


def _wrapping(member: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """A closed object whose only declared member ``m`` has the schema ``member``."""
    return {"type": "object", "properties": {"m": member}}


def test_a_closed_empty_object_member_rejects_any_key() -> None:
    schema = _wrapping({"type": "object"})

    assert check_schema(schema, {"m": {}}) == []
    assert check_schema(schema, {"m": {"x": 1}}) == ["$.m.x: unknown field"]
    assert check_schema(schema, {"m": {"x": 1, "y": None}}) == [
        "$.m.x: unknown field",
        "$.m.y: unknown field",
    ]


def test_an_empty_schema_member_is_closed_by_default_but_accepts_any_scalar() -> None:
    schema = _wrapping({})

    assert check_schema(schema, {"m": "text"}) == []
    assert check_schema(schema, {"m": [1, "a", None]}) == []
    assert check_schema(schema, {"m": {"x": 1}}) == ["$.m.x: unknown field"]


def test_a_required_only_member_reports_the_missing_name() -> None:
    schema = _wrapping({"type": "object", "required": ["a"], "additionalProperties": True})

    assert check_schema(schema, {"m": {}}) == ["$.m.a: missing required field"]
    assert check_schema(schema, {"m": {"b": 2}}) == ["$.m.a: missing required field"]
    assert check_schema(schema, {"m": {"a": 1, "b": 2}}) == []


def test_a_required_only_member_that_stays_closed_reports_both_problems() -> None:
    schema = _wrapping({"type": "object", "required": ["a"]})

    assert check_schema(schema, {"m": {"b": 2}}) == [
        "$.m.a: missing required field",
        "$.m.b: unknown field",
    ]


def test_an_additional_properties_true_member_accepts_anything_inside() -> None:
    schema = _wrapping({"type": "object", "additionalProperties": True})

    assert check_schema(schema, {"m": {}}) == []
    assert check_schema(schema, {"m": {"x": {"deep": [1, {"y": None}]}, "z": "s"}}) == []
    assert check_schema(schema, {"m": "s"}) == ["$.m: expected object, got string"]


def test_a_map_member_validates_every_value_against_its_schema() -> None:
    value_schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "n": {"type": "integer"}},
        "required": ["id"],
    }
    schema = _wrapping({"type": "object", "additionalProperties": value_schema})

    assert check_schema(schema, {"m": {"k1": {"id": "a"}, "k2": {"id": "b", "n": 2}}}) == []
    assert check_schema(schema, {"m": {"k1": {"id": "a"}, "k2": {"n": "2", "x": 0}}}) == [
        "$.m.k2.id: missing required field",
        "$.m.k2.n: expected integer, got string",
        "$.m.k2.x: unknown field",
    ]


# ---------------------------------------------------------------------------------------------
# Arrays and scalars
# ---------------------------------------------------------------------------------------------


def test_nested_arrays_of_objects_are_descended_to_every_object() -> None:
    row: dict[str, JSONValue] = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }
    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {"rows": {"type": "array", "items": {"type": "array", "items": row}}},
    }

    assert check_schema(schema, {"rows": [[{"id": "a"}], []]}) == []
    assert check_schema(schema, {"rows": [[{"id": "a"}], [{"id": "b", "x": 1}, {}]]}) == [
        "$.rows[1][0].x: unknown field",
        "$.rows[1][1].id: missing required field",
    ]
    assert check_schema(schema, {"rows": [{"id": "a"}]}) == [
        "$.rows[0]: expected array, got object"
    ]


def test_an_untyped_member_with_an_item_schema_checks_array_items() -> None:
    schema = _wrapping({"items": {"type": "string"}})

    assert check_schema(schema, {"m": ["a", "b"]}) == []
    assert check_schema(schema, {"m": "not an array"}) == []
    assert check_schema(schema, {"m": ["a", 1]}) == ["$.m[1]: expected string, got integer"]


SCALARS: dict[str, JSONValue] = {
    "type": "object",
    "properties": {
        "s": {"type": "string"},
        "n": {"type": "number"},
        "i": {"type": "integer"},
        "b": {"type": "boolean"},
        "u": {"type": ["string", "null"]},
        "a": {"type": "array"},
        "o": {"type": "object", "additionalProperties": True},
    },
}


def test_scalar_members_stay_on_the_type_lookup_path() -> None:
    node = payloads._compiled(SCALARS)

    assert dict(node.leaves) == {
        "s": frozenset({"string"}),
        "n": frozenset({"number", "integer"}),
        "i": frozenset({"integer"}),
        "b": frozenset({"boolean"}),
        "u": frozenset({"string", "null"}),
        "a": frozenset({"array"}),
        "o": frozenset({"object"}),
    }


def test_members_the_type_alone_cannot_decide_are_never_leaves() -> None:
    schema: dict[str, JSONValue] = {
        "type": "object",
        "properties": {
            "closed": {"type": "object"},
            "untyped": {},
            "required_only": {"type": "object", "required": ["a"], "additionalProperties": True},
            "map": {"type": "object", "additionalProperties": {"type": "string"}},
            "nullable_object": {"type": ["object", "null"], "properties": {"a": {}}},
            "items": {"type": "array", "items": {"type": "string"}},
            "enum": {"type": "string", "enum": ["A"]},
            "no_type": {"type": []},
        },
    }

    assert dict(payloads._compiled(schema).leaves) == {}


def test_scalar_leaf_type_mismatches_are_still_reported() -> None:
    valid: JSONValue = {"s": "x", "n": 1, "i": 2, "b": False, "u": None, "a": [1], "o": {"k": 1}}
    invalid: JSONValue = {"s": 1, "n": True, "i": 1.5, "b": 0, "u": 3, "a": {}, "o": []}

    assert check_schema(SCALARS, valid) == []
    assert check_schema(SCALARS, {"n": 1.5}) == []
    assert check_schema(SCALARS, invalid) == [
        "$.a: expected array, got object",
        "$.b: expected boolean, got integer",
        "$.i: expected integer, got number",
        "$.n: expected number, got boolean",
        "$.o: expected object, got array",
        "$.s: expected string, got integer",
        "$.u: expected string or null, got integer",
    ]


def test_a_member_whose_type_list_is_empty_rejects_every_value() -> None:
    schema = _wrapping({"type": []})

    assert check_schema(schema, {"m": 1}) != []
    assert _fast_valid(schema, {"m": 1}) is False


HANDCRAFTED: list[tuple[str, dict[str, JSONValue], JSONValue]] = [
    ("closed object", _wrapping({"type": "object"}), {"m": {}}),
    ("empty schema", _wrapping({}), {"m": {}}),
    (
        "required only",
        _wrapping({"type": "object", "required": ["a"], "additionalProperties": True}),
        {"m": {"a": {"b": [1]}}},
    ),
    (
        "map of objects",
        _wrapping(
            {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "tags": {"type": "array"}},
                    "required": ["id"],
                },
            }
        ),
        {"m": {"k1": {"id": "a", "tags": ["x"]}, "k2": {"id": "b"}}},
    ),
    (
        "nullable object",
        _wrapping({"type": ["object", "null"], "properties": {"a": {"type": "string"}}}),
        {"m": {"a": "x"}},
    ),
    ("scalars", SCALARS, {"s": "x", "n": 1.5, "i": 2, "b": True, "u": "y", "a": [], "o": {}}),
    ("enum", _wrapping({"type": "string", "enum": ["A", "B"]}), {"m": "A"}),
]


@pytest.mark.parametrize(
    ("schema", "value"),
    [(schema, value) for _name, schema, value in HANDCRAFTED],
    ids=[name for name, _schema, _value in HANDCRAFTED],
)
def test_the_fast_path_agrees_with_the_recursive_checker_on_handcrafted_schemas(
    schema: dict[str, JSONValue], value: JSONValue
) -> None:
    _assert_agreement(schema, value, "handcrafted")


# ---------------------------------------------------------------------------------------------
# Every contract: documented samples and rendered manifest bodies
# ---------------------------------------------------------------------------------------------


def _contracts() -> list[Contract]:
    """Every contract file, in the loader's order."""
    return list(iter_contracts())


@pytest.mark.parametrize("contract", _contracts(), ids=lambda contract: contract.surface)
def test_the_fast_path_agrees_with_the_recursive_checker_on_every_documented_sample(
    contract: Contract,
) -> None:
    sample = contract.response.documented_sample
    if sample is None:
        # Google publishes no sample; its rendered rows are checked by the manifest test.
        assert contract.surface.startswith("google."), f"{contract.surface} has no sample"
        return
    _assert_agreement(contract.response.body, sample, contract.surface)


def test_every_contract_is_covered_by_a_sample_or_a_manifest_body() -> None:
    sampled = {c.surface for c in _contracts() if c.response.documented_sample is not None}
    rendered = {entry["surface"] for entry in _manifest()["entries"]}
    rows = {
        f"google.{row_surface_for(entry['request']['body']['query'])}"
        for entry in _manifest()["entries"]
        if entry["source"] == "google"
    }

    assert {c.surface for c in _contracts()} <= sampled | rendered | rows


@pytest.mark.parametrize("entry", _manifest()["entries"], ids=_entry_id)
def test_every_manifest_request_renders_a_body_its_contract_accepts(
    entry: dict[str, Any],
) -> None:
    source = cast(Source, entry["source"])
    _source, _dot, name = cast(str, entry["surface"]).partition(".")
    shape = _shape(entry["request"])
    contract = load_contract(source, name)

    assert identify_surface(source, shape) == name
    response = render(source, shape, _world())

    assert not response.timeout
    assert 200 <= response.status < 300, response.body
    validate_response(contract, response.body, status=response.status)
    if source == "google":
        validate_google_rows(entry["request"]["body"]["query"], response.body)


@pytest.mark.parametrize("entry", _manifest()["entries"], ids=_entry_id)
def test_the_fast_path_agrees_with_the_recursive_checker_on_every_manifest_exchange(
    entry: dict[str, Any],
) -> None:
    source = cast(Source, entry["source"])
    _source, _dot, name = cast(str, entry["surface"]).partition(".")
    contract = load_contract(source, name)
    response = render(source, _shape(entry["request"]), _world())
    body = cast(JSONValue, response.body)

    _assert_agreement(contract.response.body, body, entry["file"])
    request_schema = contract.request.body
    if request_schema is not None:
        _assert_agreement(request_schema, entry["request"]["body"], f"{entry['file']} request")
    if source == "google":
        row_contract = load_contract("google", row_surface_for(entry["request"]["body"]["query"]))
        for index, row in enumerate(cast(list[JSONValue], cast(dict[str, Any], body)["results"])):
            label = f"{entry['file']} row {index}"
            _assert_agreement(row_contract.response.body, row, label)
