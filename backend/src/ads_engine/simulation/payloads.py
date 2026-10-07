"""Contracts, request validation and live wire shapes for every simulated surface.

This module is the single place where the product's external API contracts are read and
enforced. Each contract is a JSON file at ``./tests/contracts/{source}/{name}.json`` relative to
the working directory (``backend/`` on the host, ``/app`` in the image), recording the official
reference URL, the API version or the page's last-modified date, the request shape and the
documented sample response (AAP 0.3.3 "Contract fidelity", AAP 0.11.2 "External fixtures are
traceable to verified contracts"). Three consumers share it, so none of them encodes its own
assumptions:

* the simulator (:func:`render`, served through ``simulation/transport.py``), which validates
  every request against its contract, renders seeded :class:`SimulatedWorld` state in the
  exact live wire shape and validates every successful body before returning it;
* the contract tests, which use :func:`load_contract`, :func:`check_schema`,
  :func:`validate_request`, :func:`validate_response` and :func:`validate_documented_sample`;
* ``ads-engine capture-fixtures``, which uses :func:`identify_surface` on live requests (it
  never needs a world) and :func:`validate_capture` on what it recorded under ``captured/``.

Pipeline of :func:`render`::

    identify_surface -> load_contract -> validate_request (a violation renders HTTP 400 in the
    platform's own error shape, a defect the client never mocks or retries) -> first matching
    Fault of the world -> body from world state at world.now() -> validate_response on every
    2xx body (a failure raises ContractViolation: a simulator bug must be loud) -> response

Determinism: rendering reads no clock other than ``world.now()``, orders every list stably
(ids ascending unless the request sorts), and derives page tokens, cursors and correlation ids
from SHA-256 digests of the request, never from ``hash()`` or randomness, so the same seed,
spec and clock render byte-identical bodies in every process.

Privacy: no header value, token, email, GAQL id list or request body is ever logged. A HubSpot
lookup by email hashes the normalised address to find the contact; the plaintext appears only
in the response object, as HubSpot itself echoes it, and is never stored, cached or logged.

Only the standard library, ``httpx`` (for the request type), the simulation package and
generator, the Google GAQL templates, settings, logging and ``db.models.ops.SOURCES`` are
imported. The LinkedIn and HubSpot connector modules are deliberately not imported: the shapes
come from the contract files and the platforms' documentation, never from the clients the
simulator exists to test. There is no database I/O.

Example::

    world = SimulatedWorld(42)
    request = httpx.Request(
        "POST",
        "https://n8n.sim.invalid/webhook/google-ads",
        headers={"X-Webhook-Secret": "simulated", "Content-Type": "application/json"},
        json={"customer_id": "9900000001", "query": QUERIES["Q-G1"].render(), "page_token": None},
    )
    response = render("google", request, world)
    assert response.status == 200 and response.surface == "search"
"""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn, cast
from urllib.parse import parse_qsl, unquote

import httpx

from ads_engine.connectors.google_n8n.queries import (
    AD_GROUP_HI,
    AD_GROUP_ID,
    AD_GROUP_LO,
    CAMPAIGN_HI,
    CAMPAIGN_LO,
    END,
    IDS,
    LAST_ID,
    PAGE_SIZE,
    QUERIES,
    START,
    ParsedQuery,
    QueryTemplate,
    parse_query,
    query_id_for,
    rest_path,
)
from ads_engine.db.models.ops import SOURCES
from ads_engine.logging import get_logger
from ads_engine.settings import Source, get_settings
from ads_engine.simulation import SIM_EMAIL_DOMAIN
from ads_engine.simulation.generator import (
    FAULT_SURFACES,
    SEGMENT_PIVOTS,
    ContactRecord,
    Fault,
    GoogleAdGroupRecord,
    GoogleCampaignDay,
    GoogleCampaignRecord,
    GoogleConversionActionRecord,
    GoogleCustomerRecord,
    GoogleKeywordDay,
    GoogleKeywordRecord,
    GoogleSearchTermDay,
    HubSpotRecord,
    LinkedInAccountRecord,
    LinkedInCampaignGroupRecord,
    LinkedInCampaignRecord,
    LinkedInCreativeDay,
    LinkedInCreativeRecord,
    LinkedInLeadFormRecord,
    LinkedInLeadResponse,
    LinkedInPostRecord,
    LinkedInQuestionRecord,
    LinkedInSegmentRecord,
    PropertyValue,
    SearchFilter,
    SimulatedWorld,
)

__all__ = [
    "SURFACES",
    "Contract",
    "ContractNotFound",
    "ContractViolation",
    "GaqlSpec",
    "JSONValue",
    "RenderedResponse",
    "RequestShape",
    "RequestSpec",
    "ResponseSpec",
    "RestliValue",
    "UnknownSurface",
    "canonical_json",
    "check_schema",
    "clear_contract_cache",
    "contracts_root",
    "identify_surface",
    "iter_contracts",
    "load_contract",
    "parse_restli",
    "render",
    "row_surface_for",
    "validate_capture",
    "validate_documented_sample",
    "validate_google_rows",
    "validate_request",
    "validate_response",
]

_LOG: Final = get_logger("ads_engine.simulation.payloads")

# ---------------------------------------------------------------------------------------------
# Types and exceptions
# ---------------------------------------------------------------------------------------------

type JSONValue = bool | int | float | str | list[JSONValue] | dict[str, JSONValue] | None
"""A JSON-native value: what ``json.loads`` returns and what a rendered body may hold."""

type RestliValue = str | list[RestliValue] | dict[str, RestliValue]
"""A parsed Rest.li 2.0 query value: a leaf string, a ``List(...)`` or a ``(k:v,...)`` record."""

type JSONObject = dict[str, JSONValue]


class ContractNotFound(FileNotFoundError):  # noqa: N818 - name fixed by the contract API
    """A contract file is missing from the contracts directory.

    Attributes:
        path: The path that was expected to hold the contract.
    """

    def __init__(self, path: Path) -> None:
        """Name the expected path and the working directory the loader needs."""
        self.path = path
        super().__init__(
            f"contract file not found: {path}. Contracts are read from ./tests/contracts "
            "relative to the working directory, which must be backend/ (host) or /app (image)"
        )


class ContractViolation(ValueError):  # noqa: N818 - name fixed by the contract API
    """A request, response or contract file breaks a contract.

    Attributes:
        surface: The contract surface, such as ``linkedin.lead_form_responses``.
        errors: Every violation found, each a JSON-path message such as
            ``$.results[0].campaign.foo: unknown field``, sorted for determinism.
    """

    def __init__(self, surface: str, errors: Iterable[str]) -> None:
        """Record the surface and the violations; the message joins them."""
        self.surface = surface
        self.errors: tuple[str, ...] = tuple(errors)
        detail = "; ".join(self.errors) if self.errors else "contract violated"
        super().__init__(f"{surface}: {detail}")


class UnknownSurface(ValueError):  # noqa: N818 - name fixed by the contract API
    """A source has no such surface, or a request matches none of the source's surfaces."""


# ---------------------------------------------------------------------------------------------
# Surfaces and contract files
# ---------------------------------------------------------------------------------------------

SURFACES: Final[Mapping[Source, tuple[str, ...]]] = MappingProxyType(
    {
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
)
"""Every contract surface per source, one contract file each (``{source}/{name}.json``)."""

# The world's fault surfaces must be exactly these surfaces, so a fault can name every surface
# the renderer serves and nothing else. A drift between the two modules fails at import.
if {key: tuple(value) for key, value in SURFACES.items()} != {
    key: tuple(value) for key, value in FAULT_SURFACES.items()
}:  # pragma: no cover - a static invariant between two modules
    raise RuntimeError("payloads.SURFACES and generator.FAULT_SURFACES disagree")

_API_VERSIONS: Final = frozenset({"v25", "202609", "2026-09"})
_TOP_LEVEL_KEYS: Final = frozenset(
    {"surface", "source", "reference_url", "api_version", "last_modified", "request", "response"}
)
_REQUEST_KEYS: Final = frozenset(
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
_RESPONSE_KEYS: Final = frozenset({"status", "body", "documented_sample"})
_GAQL_KEYS: Final = frozenset({"query_id", "resource", "select"})
_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_METHODS: Final = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def contracts_root() -> Path:
    """The contracts directory: ``./tests/contracts`` under the current working directory."""
    return Path.cwd() / "tests" / "contracts"


@dataclass(frozen=True, slots=True)
class GaqlSpec:
    """The GAQL query a Google row contract describes.

    Attributes:
        query_id: The template id, such as ``Q-G2`` (Q-G2b shares ``row_campaign``).
        resource: The ``FROM`` resource.
        select: The ``SELECT`` fields in order.
    """

    query_id: str
    resource: str
    select: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """The request half of a contract.

    Attributes:
        method: HTTP method.
        url: URL template; ``{placeholders}`` match one path segment each. LinkedIn REST
            templates are relative to ``https://api.linkedin.com/rest/``; Google templates are
            the configured webhook ``{N8N_GOOGLE_ADS_WEBHOOK_URL}``.
        required_headers: Header names that must be present with a non-empty value.
        required_query: Query parameter names that must be present.
        optional_query: Further query parameter names that may be present.
        fields: The exact ``fields`` projection, or ``None``.
        body: The request body schema, or ``None`` when the request has no body.
        gaql: The GAQL query of a Google row contract, or ``None``.
        limits: Documented limits; see :func:`validate_request` for the enforced keys.
    """

    method: str
    url: str
    required_headers: tuple[str, ...]
    required_query: tuple[str, ...]
    optional_query: tuple[str, ...]
    fields: str | None
    body: Mapping[str, JSONValue] | None
    gaql: GaqlSpec | None
    limits: Mapping[str, JSONValue]

    def limit(self, name: str) -> int | None:
        """An integer limit by name, or ``None`` when the contract does not set it."""
        value = self.limits.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None


@dataclass(frozen=True, slots=True)
class ResponseSpec:
    """The response half of a contract.

    Attributes:
        status: The HTTP statuses a successful call may return (for example 200 and 207).
        body: The schema every successful body must satisfy.
        documented_sample: The official page's sample, verbatim, or ``None``.
    """

    status: tuple[int, ...]
    body: Mapping[str, JSONValue]
    documented_sample: JSONValue


@dataclass(frozen=True, slots=True)
class Contract:
    """One external surface's contract, loaded from its JSON file.

    Attributes:
        surface: ``{source}.{name}``.
        source: ``google``, ``linkedin`` or ``hubspot``.
        name: The surface name within the source.
        reference_url: The official reference page.
        api_version: ``v25``, ``202609`` or ``2026-09``.
        last_modified: The page's last-modified date (``YYYY-MM-DD``), or ``None``.
        request: The request specification.
        response: The response specification.
        path: The file the contract was read from.
    """

    surface: str
    source: Source
    name: str
    reference_url: str
    api_version: str
    last_modified: str | None
    request: RequestSpec
    response: ResponseSpec
    path: Path

    @classmethod
    def from_json(cls, raw: JSONValue, path: Path) -> Contract:
        """Build a contract from a decoded file, checking its structure strictly.

        Args:
            raw: The decoded JSON document.
            path: The file it came from.

        Returns:
            The contract.

        Raises:
            ContractViolation: When a key is unknown or missing, a value has the wrong type,
                ``reference_url`` is empty, ``api_version`` is not a known version, the
                surface does not equal ``{source}.{name}``, or a schema uses an unsupported
                keyword.
        """
        label = path.stem if not isinstance(raw, dict) else str(raw.get("surface", path.stem))
        errors: list[str] = []
        if not isinstance(raw, dict):
            raise ContractViolation(label, ("$: a contract must be a JSON object",))
        _exact_keys(raw, _TOP_LEVEL_KEYS, "$", errors)
        surface = raw.get("surface")
        source = raw.get("source")
        reference_url = raw.get("reference_url")
        api_version = raw.get("api_version")
        last_modified = raw.get("last_modified")
        if not isinstance(source, str) or source not in SURFACES:
            errors.append("$.source: must be google, linkedin or hubspot")
        if not isinstance(surface, str) or not surface:
            errors.append("$.surface: must be a non-empty string")
        if not isinstance(reference_url, str) or not reference_url.strip():
            errors.append("$.reference_url: must be a non-empty string")
        if not isinstance(api_version, str) or api_version not in _API_VERSIONS:
            errors.append("$.api_version: must be one of 2026-09, 202609, v25")
        if last_modified is not None and (
            not isinstance(last_modified, str) or not _DATE_RE.fullmatch(last_modified)
        ):
            errors.append("$.last_modified: must be YYYY-MM-DD or null")
        name = path.stem
        if isinstance(source, str) and isinstance(surface, str) and surface != f"{source}.{name}":
            errors.append(f"$.surface: must equal {source}.{name}")
        request = _request_spec(raw.get("request"), errors)
        response = _response_spec(raw.get("response"), errors)
        if errors or request is None or response is None:
            raise ContractViolation(label, sorted(errors) or ("$: malformed contract",))
        return cls(
            surface=cast(str, surface),
            source=cast(Source, source),
            name=name,
            reference_url=cast(str, reference_url),
            api_version=cast(str, api_version),
            last_modified=cast(str | None, last_modified),
            request=request,
            response=response,
            path=path,
        )


def _exact_keys(
    obj: Mapping[str, JSONValue], keys: frozenset[str], path: str, errors: list[str]
) -> None:
    """Report keys of ``obj`` that are unknown or missing against ``keys``."""
    for key in sorted(set(obj) - keys):
        errors.append(f"{_child(path, key)}: unknown key")
    for key in sorted(keys - set(obj)):
        errors.append(f"{_child(path, key)}: missing key")


def _string_list(value: JSONValue, path: str, errors: list[str]) -> tuple[str, ...]:
    """Read a list of strings, reporting a wrong type."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{path}: must be a list of strings")
        return ()
    return tuple(cast(list[str], value))


def _request_spec(raw: JSONValue, errors: list[str]) -> RequestSpec | None:
    """Parse the ``request`` object of a contract."""
    if not isinstance(raw, dict):
        errors.append("$.request: must be an object")
        return None
    _exact_keys(raw, _REQUEST_KEYS, "$.request", errors)
    method = raw.get("method")
    url = raw.get("url")
    if not isinstance(method, str) or method not in _METHODS:
        errors.append("$.request.method: must be an HTTP method")
    if not isinstance(url, str) or not url:
        errors.append("$.request.url: must be a non-empty string")
    fields = raw.get("fields")
    if fields is not None and (not isinstance(fields, str) or not fields):
        errors.append("$.request.fields: must be a non-empty string or null")
    body = raw.get("body")
    if body is not None:
        if isinstance(body, dict):
            _check_schema_keywords(body, "$.request.body", errors)
        else:
            errors.append("$.request.body: must be a schema object or null")
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        errors.append("$.request.limits: must be an object")
        limits = {}
    gaql = _gaql_spec(raw.get("gaql"), errors)
    return RequestSpec(
        method=method if isinstance(method, str) else "",
        url=url if isinstance(url, str) else "",
        required_headers=_string_list(
            raw.get("required_headers"), "$.request.required_headers", errors
        ),
        required_query=_string_list(raw.get("required_query"), "$.request.required_query", errors),
        optional_query=_string_list(raw.get("optional_query"), "$.request.optional_query", errors),
        fields=fields if isinstance(fields, str) else None,
        body=MappingProxyType(body) if isinstance(body, dict) else None,
        gaql=gaql,
        limits=MappingProxyType(limits),
    )


def _gaql_spec(raw: JSONValue, errors: list[str]) -> GaqlSpec | None:
    """Parse the optional ``request.gaql`` object of a Google row contract."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        errors.append("$.request.gaql: must be an object or null")
        return None
    _exact_keys(raw, _GAQL_KEYS, "$.request.gaql", errors)
    query_id = raw.get("query_id")
    resource = raw.get("resource")
    if not isinstance(query_id, str) or query_id not in QUERIES:
        errors.append("$.request.gaql.query_id: must name a GAQL template")
    if not isinstance(resource, str) or not resource:
        errors.append("$.request.gaql.resource: must be a non-empty string")
    select = _string_list(raw.get("select"), "$.request.gaql.select", errors)
    return GaqlSpec(
        query_id=query_id if isinstance(query_id, str) else "",
        resource=resource if isinstance(resource, str) else "",
        select=select,
    )


def _response_spec(raw: JSONValue, errors: list[str]) -> ResponseSpec | None:
    """Parse the ``response`` object of a contract."""
    if not isinstance(raw, dict):
        errors.append("$.response: must be an object")
        return None
    _exact_keys(raw, _RESPONSE_KEYS, "$.response", errors)
    status = raw.get("status")
    statuses: tuple[int, ...] = ()
    if (
        not isinstance(status, list)
        or not status
        or not all(
            isinstance(item, int) and not isinstance(item, bool) and 100 <= item <= 599
            for item in status
        )
    ):
        errors.append("$.response.status: must be a non-empty list of HTTP statuses")
    else:
        statuses = tuple(cast(list[int], status))
    body = raw.get("body")
    if isinstance(body, dict):
        _check_schema_keywords(body, "$.response.body", errors)
    else:
        errors.append("$.response.body: must be a schema object")
        body = {}
    return ResponseSpec(
        status=statuses,
        body=MappingProxyType(body),
        documented_sample=raw.get("documented_sample"),
    )


def load_contract(source: Source, name: str, *, root: Path | None = None) -> Contract:
    """Load one surface's contract, cached per (resolved root, source, name).

    Args:
        source: ``google``, ``linkedin`` or ``hubspot``.
        name: A surface of :data:`SURFACES` for the source.
        root: The contracts directory; :func:`contracts_root` when ``None``.

    Returns:
        The contract.

    Raises:
        UnknownSurface: When the source or name is not a known surface.
        ContractNotFound: When the file is missing.
        ContractViolation: When the file is not valid JSON or breaks the contract structure.
    """
    if source not in SURFACES or name not in SURFACES[source]:
        raise UnknownSurface(f"{source!r} has no contract surface {name!r}")
    base = (root if root is not None else contracts_root()).resolve()
    return _load_cached(base, source, name)


@lru_cache(maxsize=64)
def _load_cached(root: Path, source: Source, name: str) -> Contract:
    """Read and parse one contract file (cached by :func:`load_contract`)."""
    path = root / source / f"{name}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ContractNotFound(path) from None
    try:
        raw = cast(JSONValue, json.loads(text))
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"{source}.{name}", (f"$: not valid JSON ({exc.msg})",)) from None
    contract = Contract.from_json(raw, path)
    if contract.source != source:
        raise ContractViolation(contract.surface, (f"$.source: must be {source}",))
    return contract


def iter_contracts(root: Path | None = None) -> Iterator[Contract]:
    """Yield every surface's contract, sources in display order, surfaces in catalogue order.

    Raises:
        ContractNotFound: When a contract file is missing.
        ContractViolation: When a contract file is malformed.
    """
    for source in SOURCES:
        typed = cast(Source, source)
        for name in SURFACES[typed]:
            yield load_contract(typed, name, root=root)


def clear_contract_cache() -> None:
    """Forget every loaded contract (tests that rewrite contract files call this)."""
    _load_cached.cache_clear()


# ---------------------------------------------------------------------------------------------
# Schema validation (the contract subset: no jsonschema dependency)
# ---------------------------------------------------------------------------------------------

_SCHEMA_KEYWORDS: Final = frozenset(
    {"type", "properties", "required", "items", "enum", "additionalProperties", "description"}
)
_JSON_TYPES: Final = ("object", "array", "string", "integer", "number", "boolean", "null")
_IDENTIFIER_RE: Final = re.compile(r"[A-Za-z_@$][A-Za-z0-9_@$\-]*")


def _child(path: str, key: str) -> str:
    """The JSON path of ``key`` under ``path``: ``$.a.b``, or ``$.a["urn:li:x:1"]``."""
    if _IDENTIFIER_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _check_schema_keywords(schema: Mapping[str, JSONValue], path: str, errors: list[str]) -> None:
    """Walk a schema, reporting unsupported keywords and malformed keyword values."""
    for keyword in sorted(set(schema) - _SCHEMA_KEYWORDS):
        errors.append(f"{path}: unsupported schema keyword {keyword!r}")
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not kinds or not all(isinstance(item, str) and item in _JSON_TYPES for item in kinds):
            errors.append(f"{path}.type: must name JSON types")
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(item, str) for item in required)
    ):
        errors.append(f"{path}.required: must be a list of names")
    enum = schema.get("enum")
    if enum is not None and not isinstance(enum, list):
        errors.append(f"{path}.enum: must be a list")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            errors.append(f"{path}.properties: must be an object")
        else:
            for key, sub in properties.items():
                if isinstance(sub, dict):
                    _check_schema_keywords(sub, _child(f"{path}.properties", key), errors)
                else:
                    errors.append(f"{_child(f'{path}.properties', key)}: must be a schema")
    items = schema.get("items")
    if items is not None:
        if isinstance(items, dict):
            _check_schema_keywords(items, f"{path}.items", errors)
        else:
            errors.append(f"{path}.items: must be a schema")
    extra = schema.get("additionalProperties")
    if extra is not None and not isinstance(extra, bool):
        if isinstance(extra, dict):
            _check_schema_keywords(extra, f"{path}.additionalProperties", errors)
        else:
            errors.append(f"{path}.additionalProperties: must be a boolean or a schema")


_TYPE_NAMES: Final[Mapping[type, str]] = MappingProxyType(
    {
        type(None): "null",
        bool: "boolean",
        int: "integer",
        float: "number",
        str: "string",
        list: "array",
        dict: "object",
    }
)


def _json_type_of(value: object) -> str | None:
    """The JSON type name of a value, or ``None`` when it is not JSON-native.

    ``bool`` is ``boolean``, never ``integer``; ``Decimal`` and tuples are not JSON types, so
    callers convert at render time rather than relying on the validator to accept them.
    """
    name = _TYPE_NAMES.get(type(value))
    if name is not None:
        return name
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return None


def _type_matches(expected: str, actual: str) -> bool:
    """Whether a value of JSON type ``actual`` satisfies the schema type ``expected``."""
    return expected == actual or (expected == "number" and actual == "integer")


def _json_equal(left: JSONValue, right: JSONValue) -> bool:
    """JSON equality that keeps ``true`` and ``1`` apart."""
    return _json_type_of(left) == _json_type_of(right) and left == right


def _describe(value: JSONValue) -> str:
    """A short rendering of a scalar for an enum message (never more than 40 characters)."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 40 else text[:37] + "..."


def check_schema(schema: Mapping[str, JSONValue], value: JSONValue, path: str = "$") -> list[str]:
    """Validate a JSON value against a contract schema, returning every error, sorted.

    The supported subset is exactly the keywords the contracts use:

    * ``type``: one name or a list among ``object``, ``array``, ``string``, ``integer``,
      ``number``, ``boolean`` and ``null``. ``integer`` excludes ``bool``; ``number`` accepts
      integers and floats but not ``bool``. Non-JSON values (``Decimal``, tuples) never match.
    * ``properties``, ``required``, ``items`` and ``enum``.
    * ``additionalProperties``: **defaults to false**, so an undeclared key is an error;
      ``true`` allows anything; a schema validates every undeclared value.
    * ``description``: ignored.

    Args:
        schema: The schema object.
        value: The JSON-native value to check.
        path: The JSON path of ``value`` in error messages.

    Returns:
        Every violation as ``"<json path>: <problem>"``, sorted; empty when valid.

    Raises:
        ContractViolation: When the schema uses any other keyword, so a contract can never
            rely silently on a keyword this validator ignores.

    Example::

        >>> check_schema({"type": "object", "properties": {"a": {"type": "string"}}}, {"b": 1})
        ['$.b: unknown field']
    """
    # Valid values, the overwhelming case (every rendered row), take the compiled boolean
    # path that builds no strings; only an invalid value is walked again for its messages.
    if _valid(_compiled(schema), value):
        return []
    errors: list[str] = []
    _check(schema, value, path, errors)
    return sorted(errors)


@dataclass(frozen=True, slots=True)
class _Node:
    """A schema compiled for the boolean fast path of :func:`check_schema`.

    Attributes:
        types: The allowed JSON types, or ``None`` for any.
        enum: The allowed values, or ``None``.
        properties: Compiled declared members.
        required: Required member names.
        extra: ``additionalProperties``: a boolean or a compiled schema.
        items: The compiled item schema, or ``None``.
        leaves: The members that only a type constrains, with their allowed types (an empty
            set allows any type); checked inline by :func:`_valid`.
    """

    types: frozenset[str] | None
    enum: tuple[JSONValue, ...] | None
    properties: Mapping[str, _Node]
    required: tuple[str, ...]
    extra: bool | _Node
    items: _Node | None
    leaves: Mapping[str, frozenset[str]]

    @property
    def leaf(self) -> bool:
        """Whether only the type constrains a value (no enum, members or items)."""
        return self.enum is None and not self.properties and self.items is None


_COMPILED: Final[dict[int, tuple[Mapping[str, JSONValue], _Node]]] = {}
_COMPILED_LIMIT: Final = 512


def _compiled(schema: Mapping[str, JSONValue]) -> _Node:
    """The compiled form of ``schema``, cached by identity (contract schemas are long-lived).

    The cache keeps a reference to each schema, so an identity can never be reused by
    another object while cached; it is cleared when it grows past :data:`_COMPILED_LIMIT`.

    Raises:
        ContractViolation: When the schema uses an unsupported keyword anywhere.
    """
    entry = _COMPILED.get(id(schema))
    if entry is not None and entry[0] is schema:
        return entry[1]
    node = _compile(schema, "$")
    if len(_COMPILED) >= _COMPILED_LIMIT:
        _COMPILED.clear()
    _COMPILED[id(schema)] = (schema, node)
    return node


def _compile(schema: Mapping[str, JSONValue], path: str) -> _Node:
    """Compile one schema object (see :class:`_Node`)."""
    unsupported = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unsupported:
        raise ContractViolation(
            "schema", tuple(f"{path}: unsupported schema keyword {name!r}" for name in unsupported)
        )
    kind = schema.get("type")
    types: frozenset[str] | None = None
    if kind is not None:
        names = [kind] if isinstance(kind, str) else cast(list[str], kind)
        types = frozenset(names) | ({"integer"} if "number" in names else frozenset())
    enum = schema.get("enum")
    properties = schema.get("properties")
    declared = (
        {
            key: _compile(cast(Mapping[str, JSONValue], sub), _child(path, key))
            for key, sub in properties.items()
            if isinstance(sub, dict)
        }
        if isinstance(properties, dict)
        else {}
    )
    extra_raw = schema.get("additionalProperties", False)
    extra: bool | _Node = (
        _compile(extra_raw, f"{path}.*") if isinstance(extra_raw, dict) else extra_raw is True
    )
    items = schema.get("items")
    required = schema.get("required")
    leaves = {
        key: sub.types if sub.types is not None else frozenset()
        for key, sub in declared.items()
        if sub.leaf
    }
    return _Node(
        types=types,
        enum=tuple(enum) if isinstance(enum, list) else None,
        properties=declared,
        required=tuple(name for name in required if isinstance(name, str))
        if isinstance(required, list)
        else (),
        extra=extra,
        items=_compile(items, f"{path}[]") if isinstance(items, dict) else None,
        leaves=leaves,
    )


def _valid(node: _Node, value: JSONValue) -> bool:
    """Whether ``value`` satisfies a compiled schema (no messages)."""
    actual = _json_type_of(value)
    if actual is None or (node.types is not None and actual not in node.types):
        return False
    if node.enum is not None and not any(_json_equal(option, value) for option in node.enum):
        return False
    if isinstance(value, dict):
        declared = node.properties
        leaves = node.leaves
        extra = node.extra
        for key, member in value.items():
            sub = declared.get(key)
            if sub is not None:
                allowed = leaves.get(key)
                if allowed is not None:
                    # A leaf member: one type lookup instead of a recursive call.
                    kind = _json_type_of(member)
                    if kind is None or (allowed and kind not in allowed):
                        return False
                elif not _valid(sub, member):
                    return False
            elif isinstance(extra, _Node):
                if not _valid(extra, member):
                    return False
            elif not extra:
                return False
        return all(name in value for name in node.required)
    if isinstance(value, list) and node.items is not None:
        items = node.items
        return all(_valid(items, item) for item in value)
    return True


def _check(schema: Mapping[str, JSONValue], value: JSONValue, path: str, errors: list[str]) -> None:
    """Recursive worker of :func:`check_schema`."""
    unsupported = sorted(set(schema) - _SCHEMA_KEYWORDS)
    if unsupported:
        raise ContractViolation(
            "schema", tuple(f"{path}: unsupported schema keyword {name!r}" for name in unsupported)
        )
    actual = _json_type_of(value)
    if actual is None:
        errors.append(f"{path}: {type(value).__name__} is not a JSON value")
        return
    kind = schema.get("type")
    if kind is not None:
        expected = [kind] if isinstance(kind, str) else cast(list[str], kind)
        if not any(_type_matches(name, actual) for name in expected):
            errors.append(f"{path}: expected {' or '.join(expected)}, got {actual}")
            return
    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(option, value) for option in enum):
        errors.append(f"{path}: {_describe(value)} is not an allowed value")
    if isinstance(value, dict):
        _check_object(schema, value, path, errors)
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _check(items, item, f"{path}[{index}]", errors)


def _check_object(
    schema: Mapping[str, JSONValue], value: JSONObject, path: str, errors: list[str]
) -> None:
    """Validate the members of an object value."""
    properties = schema.get("properties")
    declared = properties if isinstance(properties, dict) else {}
    extra = schema.get("additionalProperties", False)
    for key, member in value.items():
        sub = declared.get(key)
        member_path = _child(path, key)
        if isinstance(sub, dict):
            _check(sub, member, member_path, errors)
        elif isinstance(extra, dict):
            _check(extra, member, member_path, errors)
        elif extra is not True:
            errors.append(f"{member_path}: unknown field")
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in value:
                errors.append(f"{_child(path, name)}: missing required field")


def validate_response(contract: Contract, body: JSONValue, *, status: int = 200) -> None:
    """Validate a response status and, for a 2xx status, its body.

    Error bodies (non-2xx) have no schema in the contracts, so only the status is checked for
    them. Google ``search`` rows are validated separately by :func:`validate_google_rows`,
    because the envelope's ``results`` items are open in ``search.json``.

    Args:
        contract: The surface's contract.
        body: The decoded response body.
        status: The HTTP status.

    Raises:
        ContractViolation: When the status is not one the contract lists, or a 2xx body
            breaks the schema.
    """
    errors: list[str] = []
    if status not in contract.response.status:
        listed = ", ".join(str(code) for code in contract.response.status)
        errors.append(f"$: HTTP {status} is not a status the contract lists ({listed})")
    if 200 <= status < 300:
        errors.extend(check_schema(contract.response.body, body))
    if errors:
        raise ContractViolation(contract.surface, errors)


def validate_documented_sample(contract: Contract) -> None:
    """Validate the contract's documented sample against its own response schema.

    The contract tests use this to prove the schema accepts the official page's sample. A
    contract without a sample passes.

    Raises:
        ContractViolation: When the sample breaks the schema.
    """
    sample = contract.response.documented_sample
    if sample is None:
        return
    errors = check_schema(contract.response.body, sample)
    if errors:
        raise ContractViolation(contract.surface, errors)


def validate_google_rows(query: str, body: JSONValue, *, root: Path | None = None) -> None:
    """Validate every row of a Google ``search`` body against its ``row_*`` contract.

    Args:
        query: The GAQL query the body answers; it selects the row contract.
        body: The decoded webhook response (the n8n envelope).
        root: The contracts directory; :func:`contracts_root` when ``None``.

    Raises:
        UnknownSurface: When the query is not a recognised GAQL template.
        ContractViolation: When a row breaks its contract, or the envelope's ``fieldMask``
            does not list exactly the selected fields.
    """
    surface = row_surface_for(query)
    contract = load_contract("google", surface, root=root)
    if not isinstance(body, dict):
        raise ContractViolation(contract.surface, ("$: the envelope must be an object",))
    errors: list[str] = []
    template = QUERIES[cast(str, query_id_for(query))]
    mask = body.get("fieldMask")
    # Compared as a set: the mask names the selected fields, and a live capture is not
    # required to list them in SELECT order.
    if not isinstance(mask, str) or set(mask.split(",")) != set(_field_mask(template).split(",")):
        errors.append("$.fieldMask: must list exactly the selected fields")
    rows = body.get("results", [])
    if not isinstance(rows, list):
        errors.append("$.results: expected array")
        rows = []
    for index, row in enumerate(rows):
        errors.extend(check_schema(contract.response.body, row, f"$.results[{index}]"))
    if errors:
        raise ContractViolation(contract.surface, sorted(errors))


# ---------------------------------------------------------------------------------------------
# Requests: shape, query strings and Rest.li values
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestShape:
    """A transport-neutral HTTP request: what validation and identification read.

    Attributes:
        method: HTTP method, upper-cased.
        url: The full URL, query string included, exactly as sent.
        headers: Header names lower-cased; use :meth:`header` for case-insensitive lookup.
        body: The raw request body (``b""`` when there is none).
    """

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes = b""

    def __post_init__(self) -> None:
        """Upper-case the method and lower-case the header names."""
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(
            self,
            "headers",
            MappingProxyType({name.lower(): value for name, value in self.headers.items()}),
        )

    def header(self, name: str) -> str | None:
        """A header value by case-insensitive name, or ``None``."""
        return self.headers.get(name.lower())

    @classmethod
    def from_httpx(cls, request: httpx.Request) -> RequestShape:
        """The shape of an ``httpx.Request`` (its body must already be read)."""
        return cls(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers.items()),
            body=request.content,
        )

    @classmethod
    def from_capture(cls, raw: JSONValue) -> RequestShape:
        """The shape of a recorded request ``{method, url, headers, body}``.

        ``headers`` is an object of names to values with credentials redacted in place
        (``Authorization: Bearer REDACTED``, the webhook secret ``REDACTED``), so the header
        rules still apply. ``body`` is ``null``, a string (sent verbatim, such as a form body)
        or a JSON value (sent as compact JSON).

        Raises:
            ContractViolation: When the record is malformed.
        """
        if not isinstance(raw, dict):
            raise ContractViolation("capture", ("$.request: must be an object",))
        method = raw.get("method")
        url = raw.get("url")
        headers = raw.get("headers", {})
        errors: list[str] = []
        if not isinstance(method, str) or not method:
            errors.append("$.request.method: must be a string")
        if not isinstance(url, str) or not url:
            errors.append("$.request.url: must be a string")
        if not isinstance(headers, dict) or not all(
            isinstance(value, str) for value in headers.values()
        ):
            errors.append("$.request.headers: must map names to strings")
            headers = {}
        if errors:
            raise ContractViolation("capture", errors)
        body = raw.get("body")
        if body is None:
            content = b""
        elif isinstance(body, str):
            content = body.encode("utf-8")
        else:
            content = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return cls(
            method=cast(str, method),
            url=cast(str, url),
            headers=cast(dict[str, str], headers),
            body=content,
        )


def _shape(request: RequestShape | httpx.Request) -> RequestShape:
    """Normalise either request type to a :class:`RequestShape`."""
    return request if isinstance(request, RequestShape) else RequestShape.from_httpx(request)


_PERCENT_RE: Final = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _decode_once(text: str) -> str:
    """Percent-decode exactly once, strictly (a malformed escape or invalid UTF-8 raises).

    Raises:
        ValueError: On a malformed percent escape or bytes that are not UTF-8.
    """
    if _PERCENT_RE.search(text):
        raise ValueError("malformed percent escape")
    return unquote(text, encoding="utf-8", errors="strict")


@dataclass(frozen=True, slots=True)
class _Query:
    """A query string split by hand, so Rest.li structure characters are never mangled.

    Attributes:
        pairs: ``(decoded name, raw value)`` in request order.
        errors: Problems found while splitting.
    """

    pairs: tuple[tuple[str, str], ...]
    errors: tuple[str, ...]

    def raw(self, name: str) -> str | None:
        """The raw (still percent-encoded) value of the first parameter ``name``."""
        for key, value in self.pairs:
            if key == name:
                return value
        return None

    def names(self) -> list[str]:
        """Parameter names in request order (repeats included)."""
        return [key for key, _value in self.pairs]


def _split_query(url: httpx.URL) -> _Query:
    """Split ``url``'s raw query on ``&`` and the first ``=`` of each part."""
    errors: list[str] = []
    try:
        raw = url.query.decode("ascii")
    except UnicodeDecodeError:
        return _Query((), ("query string: must be ASCII (percent-encode other characters)",))
    pairs: list[tuple[str, str]] = []
    for part in raw.split("&"):
        if not part:
            continue
        name, separator, value = part.partition("=")
        try:
            decoded = _decode_once(name)
        except ValueError:
            errors.append("query string: a parameter name has a malformed percent escape")
            continue
        if not separator:
            errors.append(f"query parameter {decoded}: has no value")
        pairs.append((decoded, value))
    return _Query(tuple(pairs), tuple(errors))


class _RestliParser:
    """Recursive-descent parser of Rest.li 2.0 query values (see :func:`parse_restli`)."""

    def __init__(self, text: str, surface: str) -> None:
        self.text = text
        self.pos = 0
        self.surface = surface

    def fail(self, problem: str) -> NoReturn:
        """Raise a violation naming the position of the problem."""
        raise ContractViolation(self.surface, (f"Rest.li value at offset {self.pos}: {problem}",))

    def peek(self) -> str:
        """The next character, or ``""`` at the end."""
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def value(self) -> RestliValue:
        """Parse one value: a list, a record or a leaf."""
        if self.text.startswith("List(", self.pos):
            self.pos += len("List(")
            return self.sequence()
        if self.peek() == "(":
            self.pos += 1
            return self.record()
        return self.leaf()

    def sequence(self) -> list[RestliValue]:
        """Parse the items of ``List(...)`` after its opening parenthesis."""
        items: list[RestliValue] = []
        if self.peek() == ")":
            self.pos += 1
            return items
        while True:
            items.append(self.value())
            mark = self.peek()
            self.pos += 1
            if mark == ")":
                return items
            if mark != ",":
                self.pos -= 1
                self.fail("expected ',' or ')' in a List")

    def record(self) -> dict[str, RestliValue]:
        """Parse the members of ``(k:v,...)`` after its opening parenthesis."""
        members: dict[str, RestliValue] = {}
        if self.peek() == ")":
            self.pos += 1
            return members
        while True:
            start = self.pos
            while self.peek() not in ("", ":", ",", "(", ")"):
                self.pos += 1
            raw_key = self.text[start : self.pos]
            if not raw_key or self.peek() != ":":
                self.fail("expected 'key:' in a record")
            self.pos += 1
            key = self.decode(raw_key)
            if key in members:
                self.fail(f"key {key!r} repeats in a record")
            members[key] = self.value()
            mark = self.peek()
            self.pos += 1
            if mark == ")":
                return members
            if mark != ",":
                self.pos -= 1
                self.fail("expected ',' or ')' in a record")

    def leaf(self) -> str:
        """Parse a leaf: raw-quoted ``"..."`` or text up to a structure character."""
        if self.peek() == '"':
            end = self.text.find('"', self.pos + 1)
            if end < 0:
                self.fail("unterminated quoted string")
            raw = self.text[self.pos + 1 : end]
            self.pos = end + 1
            return self.decode(raw)
        start = self.pos
        while self.peek() not in ("", ",", "(", ")"):
            if self.peek() == ":":
                self.fail("':' must be percent-encoded inside a value (%3A)")
            self.pos += 1
        raw = self.text[start : self.pos]
        if not raw:
            self.fail("empty value")
        decoded = self.decode(raw)
        if len(decoded) >= 2 and decoded.startswith('"') and decoded.endswith('"'):
            return decoded[1:-1]
        return decoded

    def decode(self, raw: str) -> str:
        """Percent-decode a leaf or key once."""
        try:
            return _decode_once(raw)
        except ValueError:
            self.fail("malformed percent escape")


def parse_restli(value: str, *, surface: str = "linkedin.restli") -> RestliValue:
    """Parse a raw (percent-encoded) Rest.li 2.0 query value.

    ``List(a,b)`` becomes a list, ``(k:v,k2:(...))`` a mapping, and a leaf is percent-decoded
    exactly once (URNs arrive as ``urn%3Ali%3AsponsoredAccount%3A123``). A quoted leaf, raw
    ``"..."`` or encoded ``%22...%22``, is unquoted. Structure characters (``( ) , :``) are
    literal; a ``:`` inside a leaf must be encoded, as Rest.li 2.0 requires.

    Args:
        value: The raw query value.
        surface: The contract surface named in a violation.

    Returns:
        The parsed value.

    Raises:
        ContractViolation: On malformed syntax.

    Example::

        >>> parse_restli("(sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A5)")
        {'sponsoredAccount': 'urn:li:sponsoredAccount:5'}
        >>> parse_restli("List(%22a-1%22,b-2)")
        ['a-1', 'b-2']
    """
    parser = _RestliParser(value, surface)
    result = parser.value()
    if parser.pos != len(value):
        parser.fail("unexpected trailing characters")
    return result


# ---------------------------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------------------------

_WEBHOOK_PLACEHOLDER: Final = "{N8N_GOOGLE_ADS_WEBHOOK_URL}"
_SECRET_HEADER_PLACEHOLDER: Final = "N8N_WEBHOOK_SECRET_HEADER"  # noqa: S105 - a name
_LINKEDIN_REST_BASE: Final = "https://api.linkedin.com/rest/"
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
_TEMPLATE_RE: Final = re.compile(r"(?P<scheme>https)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?")
_FORM_SURFACES: Final = frozenset({"linkedin.access_token"})
_JSON_MEDIA: Final = "application/json"
_FORM_MEDIA: Final = "application/x-www-form-urlencoded"
_CUSTOMER_ID_RE: Final = re.compile(r"\d{10}")
_ACCOUNT_URN_RE: Final = re.compile(r"urn:li:sponsoredAccount:(\d+)")
_CAMPAIGN_URN_RE: Final = re.compile(r"urn:li:sponsoredCampaign:(\d+)")
_POST_URN_RE: Final = re.compile(r"urn:li:(?:share|ugcPost):(\d+)")
_VERSIONED_FORM_RE: Final = re.compile(
    r"urn:li:versionedLeadGenForm:\(urn:li:leadGenForm:(\d+),(\d+)\)"
)
_DIGITS_RE: Final = re.compile(r"\d+")

# Continuation parameters a LinkedIn ``paging.links`` ``next`` href carries. The analytics
# contracts list none, because live adAnalytics truncates at 15,000 elements instead of paging;
# the simulator emits a next link only when a world sets ``analytics_next_links``, and the
# follow-up request carries these two offset parameters (LinkedIn's own paging names). They are
# accepted on these surfaces only, so every other unknown parameter still fails validation.
_CONTINUATION_QUERY: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "linkedin.ad_analytics": ("start", "count"),
        "linkedin.ad_analytics_statistics": ("start", "count"),
    }
)


@dataclass(frozen=True, slots=True)
class _Prepared:
    """A request read against its contract: parsed parts plus every violation found.

    Attributes:
        contract: The contract it was checked against.
        shape: The request.
        url: The parsed URL, or ``None`` when it is not a URL.
        query: The split query string.
        body: The decoded body (JSON, or the OAuth form as an object), or ``None``.
        errors: Every violation, sorted.
    """

    contract: Contract
    shape: RequestShape
    url: httpx.URL | None
    query: _Query
    body: JSONValue
    errors: tuple[str, ...]


def validate_request(contract: Contract, request: RequestShape | httpx.Request) -> None:
    """Validate a request against its contract, collecting every violation.

    Checks, in one pass:

    * the method;
    * the URL against the template: each ``{placeholder}`` matches one path segment and the
      host must match, except for the Google webhook (``{N8N_GOOGLE_ADS_WEBHOOK_URL}``),
      whose scheme must be ``https`` (``http`` only on ``localhost``/``127.0.0.1``, as
      settings allow) and whose path never names ``searchStream``;
    * every required header, present and non-empty (case-insensitive). The webhook's
      ``{N8N_WEBHOOK_SECRET_HEADER}`` resolves to ``get_settings().n8n_webhook_secret_header``.
      ``Linkedin-Version`` must equal the contract's version, ``X-Restli-Protocol-Version``
      ``2.0.0``, ``X-RestLi-Method`` ``BATCH_GET``, ``Authorization`` a Bearer credential and
      ``Content-Type`` the body's media type. Header values are never echoed;
    * query names: every required name present, none repeated, none outside
      ``required_query`` plus ``optional_query``, no value percent-encoded twice (``%25``);
    * ``fields``: decoded once it must equal the contract's projection, and it must be sent
      raw (live LinkedIn rejects an encoded projection with 400);
    * the body: JSON (the OAuth token request: a form) satisfying the body schema;
    * limits: ``max_inputs``, ``max_body_chars``, ``max_limit`` (CRM Search: ``page_size``),
      ``max_results``, ``max_filter_groups``, ``max_filters_per_group``, ``max_filters_total``,
      ``max_ids``, ``max_page_size``, ``max_fields``, ``max_pivots`` and ``in_list_max``;
      other limit keys are informational;
    * per-surface values the contracts fix (``q``, ``pivot``, ``timeGranularity``,
      ``leadType``, URN forms, the GAQL template and its row contract's resource and SELECT
      list, a 10-digit ``customer_id``).

    Args:
        contract: The surface's contract.
        request: The request.

    Raises:
        ContractViolation: Listing every violation.
    """
    prepared = _prepare(contract, _shape(request))
    if prepared.errors:
        raise ContractViolation(contract.surface, prepared.errors)


def _prepare(contract: Contract, shape: RequestShape) -> _Prepared:
    """Read ``shape`` against ``contract`` (see :func:`validate_request`)."""
    errors: list[str] = []
    spec = contract.request
    if shape.method != spec.method:
        errors.append(f"method: expected {spec.method}, got {shape.method}")
    url = _check_url(contract, shape.url, errors)
    _check_headers(contract, shape, errors)
    query = _split_query(url) if url is not None else _Query((), ())
    errors.extend(query.errors)
    _check_query_names(contract, query, errors)
    _check_fields(contract, query, errors)
    body = _decode_body(contract, shape, errors)
    _check_limits(contract, shape, query, body, errors)
    rule = _SURFACE_RULES.get(contract.surface)
    if rule is not None:
        rule(contract, query, body, errors)
    return _Prepared(
        contract=contract,
        shape=shape,
        url=url,
        query=query,
        body=body,
        errors=tuple(sorted(set(errors))),
    )


def _check_url(contract: Contract, text: str, errors: list[str]) -> httpx.URL | None:
    """Parse the URL and match it against the contract's template."""
    try:
        url = httpx.URL(text)
    except httpx.InvalidURL:
        errors.append("url: not a valid URL")
        return None
    template = contract.request.url
    if "searchstream" in url.path.lower():
        errors.append("url: googleAds:searchStream is never used; the webhook relays search")
    if template == _WEBHOOK_PLACEHOLDER:
        if not (url.scheme == "https" or (url.scheme == "http" and url.host in _LOCAL_HOSTS)):
            errors.append("url: the webhook must be https (http only on localhost)")
        return url
    full = template if template.startswith("https://") else _LINKEDIN_REST_BASE + template
    match = _TEMPLATE_RE.fullmatch(full.split("?", 1)[0])
    if match is None:
        raise ContractViolation(contract.surface, ("$.request.url: not an https URL template",))
    if url.scheme != "https":
        errors.append("url: scheme must be https")
    if url.host != match.group("host"):
        errors.append(f"url: host must be {match.group('host')}")
    expected = (match.group("path") or "/").split("/")
    actual = url.path.split("/")
    matches = len(expected) == len(actual) and all(
        (part.startswith("{") and part.endswith("}") and bool(value)) or part == value
        for part, value in zip(expected, actual, strict=True)
    )
    if not matches:
        errors.append(f"url: path must match {match.group('path')}")
    return url


def _resolve_header(contract: Contract, name: str) -> str:
    """Resolve a ``{PLACEHOLDER}`` header name from settings."""
    if name.startswith("{") and name.endswith("}"):
        if name[1:-1] == _SECRET_HEADER_PLACEHOLDER:
            return get_settings().n8n_webhook_secret_header
        raise ContractViolation(
            contract.surface, (f"$.request.required_headers: unknown placeholder {name}",)
        )
    return name


def _check_headers(contract: Contract, shape: RequestShape, errors: list[str]) -> None:
    """Check required headers and the values the contracts fix (values are never echoed)."""
    for name in contract.request.required_headers:
        resolved = _resolve_header(contract, name)
        value = shape.header(resolved)
        if value is None or not value.strip():
            errors.append(f"header {resolved}: missing or empty")
            continue
        lowered = resolved.lower()
        if lowered == "linkedin-version" and value.strip() != contract.api_version:
            errors.append(f"header {resolved}: must be {contract.api_version}")
        elif lowered == "x-restli-protocol-version" and value.strip() != "2.0.0":
            errors.append(f"header {resolved}: must be 2.0.0")
        elif lowered == "x-restli-method" and value.strip().upper() != "BATCH_GET":
            errors.append(f"header {resolved}: must be BATCH_GET")
        elif lowered == "authorization":
            scheme, _space, credential = value.strip().partition(" ")
            if scheme.lower() != "bearer" or not credential.strip():
                errors.append(f"header {resolved}: must be a Bearer credential")
        elif lowered == "content-type":
            media = value.split(";", 1)[0].strip().lower()
            expected = _FORM_MEDIA if contract.surface in _FORM_SURFACES else _JSON_MEDIA
            if media != expected:
                errors.append(f"header {resolved}: must be {expected}")


def _check_query_names(contract: Contract, query: _Query, errors: list[str]) -> None:
    """Required, unknown, repeated and double-encoded query parameters."""
    spec = contract.request
    allowed = (
        set(spec.required_query)
        | set(spec.optional_query)
        | set(_CONTINUATION_QUERY.get(contract.surface, ()))
    )
    names = query.names()
    for name in sorted({name for name in names if names.count(name) > 1}):
        errors.append(f"query parameter {name}: repeats")
    for name in spec.required_query:
        raw = query.raw(name)
        if raw is None:
            errors.append(f"query parameter {name}: missing (required)")
        elif not raw:
            errors.append(f"query parameter {name}: empty")
    for name in sorted(set(names) - allowed):
        errors.append(f"query parameter {name}: unknown to the contract")
    for name, raw in query.pairs:
        if "%25" in raw:
            errors.append(f"query parameter {name}: percent-encoded twice")


def _check_fields(contract: Contract, query: _Query, errors: list[str]) -> None:
    """The ``fields`` projection: equal after one decode, and sent raw."""
    expected = contract.request.fields
    raw = query.raw("fields")
    if expected is None or raw is None:
        return
    try:
        decoded = _decode_once(raw)
    except ValueError:
        errors.append("query parameter fields: malformed percent escape")
        return
    if decoded != expected:
        errors.append("query parameter fields: differs from the contract's projection")
    elif raw != decoded:
        errors.append(
            "query parameter fields: must be sent raw; LinkedIn rejects an encoded projection"
        )
    cap = contract.request.limit("max_fields")
    if cap is not None and len(_top_level_fields(decoded)) > cap:
        errors.append(f"query parameter fields: more than {cap} fields")


def _top_level_fields(projection: str) -> list[str]:
    """Split a projection on commas outside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in projection:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part for part in parts if part]


def _decode_body(contract: Contract, shape: RequestShape, errors: list[str]) -> JSONValue:
    """Decode the request body and check it against the body schema."""
    schema = contract.request.body
    if schema is None:
        if shape.body:
            errors.append("body: this request takes no body")
        return None
    if not shape.body:
        errors.append("body: missing")
        return None
    try:
        text = shape.body.decode("utf-8")
    except UnicodeDecodeError:
        errors.append("body: not UTF-8")
        return None
    parsed: JSONValue
    if contract.surface in _FORM_SURFACES:
        try:
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True, errors="strict")
        except ValueError, UnicodeDecodeError:
            errors.append("body: not a URL-encoded form")
            return None
        form: JSONObject = {}
        for key, value in pairs:
            if key in form:
                errors.append(f"body field {key}: repeats")
            form[key] = value
        parsed = form
    else:
        try:
            parsed = cast(JSONValue, json.loads(text))
        except json.JSONDecodeError:
            errors.append("body: not valid JSON")
            return None
    errors.extend(f"body {problem}" for problem in check_schema(schema, parsed))
    return parsed


def _check_limits(
    contract: Contract,
    shape: RequestShape,
    query: _Query,
    body: JSONValue,
    errors: list[str],
) -> None:
    """Enforce the request-side limits a contract documents."""
    spec = contract.request
    cap = spec.limit("max_body_chars")
    if cap is not None and len(shape.body.decode("utf-8", errors="replace")) > cap:
        errors.append(f"body: longer than {cap} characters")
    if isinstance(body, dict):
        inputs = body.get("inputs")
        cap = spec.limit("max_inputs")
        if cap is not None and isinstance(inputs, list) and len(inputs) > cap:
            errors.append(f"body inputs: more than {cap} inputs")
        limit = body.get("limit")
        cap = spec.limit("max_limit") or spec.limit("page_size")
        is_count = isinstance(limit, int) and not isinstance(limit, bool)
        if is_count and cap is not None and not 1 <= cast(int, limit) <= cap:
            errors.append(f"body limit: must be between 1 and {cap}")
        after = body.get("after")
        cap = spec.limit("max_results")
        if cap is not None and after is not None:
            if not isinstance(after, str) or not _DIGITS_RE.fullmatch(after):
                errors.append("body after: must be the numeric cursor of a previous page")
            elif int(after) >= cap:
                errors.append(f"body after: paging past {cap} results is not allowed")
        _check_filter_limits(spec, body, errors)
    for name, key in (("ids", "max_ids"), ("pivots", "max_pivots")):
        cap = spec.limit(key)
        raw = query.raw(name)
        if cap is None or raw is None:
            continue
        try:
            value = parse_restli(raw, surface=contract.surface)
        except ContractViolation:
            continue  # reported by the surface rule
        if isinstance(value, list) and len(value) > cap:
            errors.append(f"query parameter {name}: more than {cap} values")
    cap = spec.limit("max_page_size")
    raw = query.raw("pageSize")
    if (
        cap is not None
        and raw is not None
        and (not _DIGITS_RE.fullmatch(raw) or not 1 <= int(raw) <= cap)
    ):
        errors.append(f"query parameter pageSize: must be between 1 and {cap}")


def _check_filter_limits(spec: RequestSpec, body: JSONObject, errors: list[str]) -> None:
    """CRM Search filter-group limits."""
    groups = body.get("filterGroups")
    if not isinstance(groups, list):
        return
    cap = spec.limit("max_filter_groups")
    if cap is not None and len(groups) > cap:
        errors.append(f"body filterGroups: more than {cap} groups")
    total = 0
    per_group = spec.limit("max_filters_per_group")
    for index, group in enumerate(groups):
        filters = group.get("filters") if isinstance(group, dict) else None
        count = len(filters) if isinstance(filters, list) else 0
        total += count
        if per_group is not None and count > per_group:
            errors.append(f"body filterGroups[{index}]: more than {per_group} filters")
    cap = spec.limit("max_filters_total")
    if cap is not None and total > cap:
        errors.append(f"body filterGroups: more than {cap} filters in total")


# -- Per-surface value rules -------------------------------------------------------------------

type _Rule = Callable[[Contract, _Query, JSONValue, list[str]], None]


def _restli_param(
    contract: Contract, query: _Query, name: str, errors: list[str]
) -> RestliValue | None:
    """Parse a Rest.li query parameter, reporting malformed syntax."""
    raw = query.raw(name)
    if raw is None or not raw:
        return None
    try:
        return parse_restli(raw, surface=contract.surface)
    except ContractViolation as exc:
        errors.extend(f"query parameter {name}: {problem}" for problem in exc.errors)
        return None


def _leaf_param(query: _Query, name: str, errors: list[str]) -> str | None:
    """A plain query parameter, percent-decoded once."""
    raw = query.raw(name)
    if raw is None:
        return None
    try:
        return _decode_once(raw)
    except ValueError:
        errors.append(f"query parameter {name}: malformed percent escape")
        return None


def _expect_leaf(query: _Query, name: str, expected: str, errors: list[str]) -> None:
    """A plain parameter that must hold one fixed value."""
    value = _leaf_param(query, name, errors)
    if value is not None and value != expected:
        errors.append(f"query parameter {name}: must be {expected}")


def _int_param(query: _Query, name: str, minimum: int, errors: list[str]) -> int | None:
    """A decimal integer parameter of at least ``minimum``."""
    value = _leaf_param(query, name, errors)
    if value is None:
        return None
    if not _DIGITS_RE.fullmatch(value) or int(value) < minimum:
        errors.append(f"query parameter {name}: must be an integer of at least {minimum}")
        return None
    return int(value)


def _urn_list(
    value: RestliValue | None, pattern: re.Pattern[str], name: str, errors: list[str]
) -> list[str]:
    """A non-empty ``List(...)`` of URNs matching ``pattern``."""
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        errors.append(f"query parameter {name}: must be a non-empty List(...)")
        return []
    urns: list[str] = []
    for item in value:
        if isinstance(item, str) and pattern.fullmatch(item):
            urns.append(item)
        else:
            errors.append(
                f"query parameter {name}: every value must be a URN like {pattern.pattern}"
            )
    return urns


def _owner_account(value: RestliValue | None, errors: list[str]) -> str | None:
    """The account id of ``owner=(sponsoredAccount:urn...)``."""
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"sponsoredAccount"}:
        urn = value["sponsoredAccount"]
        match = _ACCOUNT_URN_RE.fullmatch(urn) if isinstance(urn, str) else None
        if match is not None:
            return match.group(1)
    errors.append(
        "query parameter owner: must be (sponsoredAccount:urn%3Ali%3AsponsoredAccount%3A{id})"
    )
    return None


def _restli_day(value: RestliValue, part: str, errors: list[str]) -> date | None:
    """One ``(year:Y,month:M,day:D)`` record."""
    if not isinstance(value, dict) or set(value) != {"year", "month", "day"}:
        errors.append(f"query parameter dateRange: {part} must be (year:Y,month:M,day:D)")
        return None
    numbers = [value[key] for key in ("year", "month", "day")]
    if not all(isinstance(number, str) and _DIGITS_RE.fullmatch(number) for number in numbers):
        errors.append(f"query parameter dateRange: {part} must hold integers")
        return None
    try:
        return date(*(int(cast(str, number)) for number in numbers))
    except ValueError:
        errors.append(f"query parameter dateRange: {part} is not a calendar date")
        return None


def _date_range(value: RestliValue | None, errors: list[str]) -> tuple[date, date | None] | None:
    """``dateRange=(start:(...),end:(...))``; ``end`` is optional on LinkedIn."""
    if value is None:
        return None
    if not isinstance(value, dict) or "start" not in value or set(value) - {"start", "end"}:
        errors.append("query parameter dateRange: must be (start:(...),end:(...))")
        return None
    start = _restli_day(value["start"], "start", errors)
    end = _restli_day(value["end"], "end", errors) if "end" in value else None
    if start is None:
        return None
    if end is not None and end < start:
        errors.append("query parameter dateRange: end is before start")
    return start, end


def _rule_analytics(contract: Contract, query: _Query, body: JSONValue, errors: list[str]) -> None:
    """``adAnalytics``: finder, granularity, pivot(s), date range and account facet."""
    del body
    statistics = contract.name == "ad_analytics_statistics"
    _expect_leaf(query, "q", "statistics" if statistics else "analytics", errors)
    _expect_leaf(query, "timeGranularity", "ALL" if statistics else "DAILY", errors)
    if statistics:
        pivots = _restli_param(contract, query, "pivots", errors)
        if pivots is not None and not (
            isinstance(pivots, list)
            and len(pivots) == 2
            and pivots[0] == "CAMPAIGN"
            and pivots[1] in SEGMENT_PIVOTS
        ):
            errors.append(
                "query parameter pivots: must be List(CAMPAIGN,MEMBER_JOB_FUNCTION|"
                "MEMBER_SENIORITY|MEMBER_INDUSTRY)"
            )
    else:
        _expect_leaf(query, "pivot", "CREATIVE", errors)
    _date_range(_restli_param(contract, query, "dateRange", errors), errors)
    _urn_list(
        _restli_param(contract, query, "accounts", errors), _ACCOUNT_URN_RE, "accounts", errors
    )
    _int_param(query, "start", 0, errors)
    _int_param(query, "count", 1, errors)


def _search_statuses(value: RestliValue | None, errors: list[str]) -> list[str] | None:
    """The status list of ``search=(status:(values:List(...)))``."""
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"status"}:
        status = value["status"]
        if isinstance(status, dict) and set(status) == {"values"}:
            values = status["values"]
            if isinstance(values, list) and values and all(isinstance(v, str) for v in values):
                return [cast(str, item) for item in values]
    errors.append("query parameter search: must be (status:(values:List(...)))")
    return None


def _rule_entity_search(
    contract: Contract, query: _Query, body: JSONValue, errors: list[str]
) -> None:
    """``adCampaignGroups`` and ``adCampaigns`` ``q=search``."""
    del body
    _expect_leaf(query, "q", "search", errors)
    _search_statuses(_restli_param(contract, query, "search", errors), errors)
    token = query.raw("pageToken")
    if token is not None and not token:
        errors.append("query parameter pageToken: empty")


def _rule_creatives(contract: Contract, query: _Query, body: JSONValue, errors: list[str]) -> None:
    """``creatives?q=criteria``."""
    del body
    _expect_leaf(query, "q", "criteria", errors)
    _urn_list(
        _restli_param(contract, query, "campaigns", errors), _CAMPAIGN_URN_RE, "campaigns", errors
    )
    statuses = _restli_param(contract, query, "intendedStatuses", errors)
    if statuses is not None and not (
        isinstance(statuses, list) and statuses and all(isinstance(item, str) for item in statuses)
    ):
        errors.append("query parameter intendedStatuses: must be a non-empty List(...)")
    order = _leaf_param(query, "sortOrder", errors)
    if order is not None and order not in ("ASCENDING", "DESCENDING"):
        errors.append("query parameter sortOrder: must be ASCENDING or DESCENDING")


def _rule_posts(contract: Contract, query: _Query, body: JSONValue, errors: list[str]) -> None:
    """``posts?ids=List(...)``: share or ugcPost URNs."""
    del body
    _urn_list(_restli_param(contract, query, "ids", errors), _POST_URN_RE, "ids", errors)


def _expect_lead_type(contract: Contract, query: _Query, errors: list[str]) -> None:
    """``leadType=(leadType:SPONSORED)``."""
    value = _restli_param(contract, query, "leadType", errors)
    if value is not None and value != {"leadType": "SPONSORED"}:
        errors.append("query parameter leadType: must be (leadType:SPONSORED)")


def _rule_lead_forms(contract: Contract, query: _Query, body: JSONValue, errors: list[str]) -> None:
    """``leadForms?q=owner``."""
    del body
    _expect_leaf(query, "q", "owner", errors)
    _owner_account(_restli_param(contract, query, "owner", errors), errors)
    _int_param(query, "count", 1, errors)
    _int_param(query, "start", 0, errors)


def _time_range(value: RestliValue | None, errors: list[str]) -> tuple[int, int] | None:
    """``submittedAtTimeRange=(start:{ms},end:{ms})``."""
    if value is None:
        return None
    if isinstance(value, dict) and set(value) == {"start", "end"}:
        start, end = value["start"], value["end"]
        if (
            isinstance(start, str)
            and isinstance(end, str)
            and _DIGITS_RE.fullmatch(start)
            and _DIGITS_RE.fullmatch(end)
            and int(start) <= int(end)
        ):
            return int(start), int(end)
    errors.append(
        "query parameter submittedAtTimeRange: must be (start:{ms},end:{ms}), start <= end"
    )
    return None


def _rule_lead_responses(
    contract: Contract, query: _Query, body: JSONValue, errors: list[str]
) -> None:
    """``leadFormResponses?q=owner``."""
    del body
    _expect_leaf(query, "q", "owner", errors)
    _owner_account(_restli_param(contract, query, "owner", errors), errors)
    _expect_lead_type(contract, query, errors)
    flag = _leaf_param(query, "limitedToTestLeads", errors)
    if flag is not None and flag not in ("true", "false"):
        errors.append("query parameter limitedToTestLeads: must be true or false")
    _time_range(_restli_param(contract, query, "submittedAtTimeRange", errors), errors)
    _int_param(query, "count", 1, errors)
    _int_param(query, "start", 0, errors)
    form = _leaf_param(query, "versionedLeadGenFormUrn", errors)
    if form is not None and not _VERSIONED_FORM_RE.fullmatch(form):
        errors.append("query parameter versionedLeadGenFormUrn: must be a versioned form URN")


def _batch_ids(value: RestliValue | None, errors: list[str]) -> list[str]:
    """``ids=List(...)`` of plain lead response ids (never URNs)."""
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        errors.append("query parameter ids: must be a non-empty List(...)")
        return []
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append("query parameter ids: every id must be a non-empty string")
        elif item.startswith("urn:"):
            errors.append("query parameter ids: plain response ids, never URNs")
        else:
            ids.append(item)
    return ids


def _rule_lead_batch(contract: Contract, query: _Query, body: JSONValue, errors: list[str]) -> None:
    """``leadFormResponses?ids=List(...)`` batch get."""
    del body
    _batch_ids(_restli_param(contract, query, "ids", errors), errors)
    _owner_account(_restli_param(contract, query, "owner", errors), errors)
    _expect_lead_type(contract, query, errors)


def _rule_google_search(
    contract: Contract, query: _Query, body: JSONValue, errors: list[str]
) -> None:
    """The webhook body: 10-digit customer, a recognised GAQL template, its row contract."""
    del query
    if not isinstance(body, dict):
        return
    customer = body.get("customer_id")
    if isinstance(customer, str) and not _CUSTOMER_ID_RE.fullmatch(customer):
        errors.append("body customer_id: must be 10 digits with no dashes")
    token = body.get("page_token")
    if isinstance(token, str) and not token:
        errors.append("body page_token: must be null or a token")
    text = body.get("query")
    if not isinstance(text, str):
        return
    if "searchstream" in text.lower():
        errors.append("body query: googleAds:searchStream is never used")
    query_id = query_id_for(text)
    if query_id is None:
        errors.append("body query: not a recognised GAQL template (Q-G1 to Q-G8, Q-G2b)")
        return
    try:
        parsed = parse_query(text)
    except ValueError as exc:
        errors.append(f"body query: {exc}")
        return
    template = parsed.template
    row = load_contract("google", template.row_surface, root=contract.path.parents[1])
    gaql = row.request.gaql
    if gaql is None:
        raise ContractViolation(row.surface, ("$.request.gaql: a row contract needs gaql",))
    if template.resource != gaql.resource or template.select != gaql.select:
        fields = ", ".join(gaql.select)
        errors.append(f"body query: {query_id} must read {gaql.resource} fields {fields}")
    cap = contract.request.limit("in_list_max")
    ids = parsed.scope.get(IDS)
    if cap is not None and isinstance(ids, tuple) and len(ids) > cap:
        errors.append(f"body query: IN lists hold at most {cap} ids")
    cap = contract.request.limit("keyset_limit")
    if cap is not None and parsed.limit is not None and parsed.limit > cap:
        errors.append(f"body query: LIMIT must be at most {cap}")


def _rule_hubspot_inputs(
    contract: Contract, query: _Query, body: JSONValue, errors: list[str]
) -> None:
    """Batch reads: non-empty input ids, a non-empty cursor and ``idProperty`` when present."""
    del contract, query
    if not isinstance(body, dict):
        return
    inputs = body.get("inputs")
    if isinstance(inputs, list):
        if not inputs:
            errors.append("body inputs: must not be empty")
        for index, item in enumerate(inputs):
            if not isinstance(item, dict):
                continue
            identifier = item.get("id")
            if isinstance(identifier, str) and not identifier.strip():
                errors.append(f"body inputs[{index}].id: must not be blank")
            after = item.get("after")
            if isinstance(after, str) and not after:
                errors.append(f"body inputs[{index}].after: must not be empty")
    id_property = body.get("idProperty")
    if isinstance(id_property, str) and not id_property.strip():
        errors.append("body idProperty: must not be blank")


_RANGE_OPERATORS: Final = frozenset(
    {"EQ", "NEQ", "LT", "LTE", "GT", "GTE", "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"}
)


def _rule_hubspot_search(
    contract: Contract, query: _Query, body: JSONValue, errors: list[str]
) -> None:
    """CRM Search filters: each operator carries the operands HubSpot requires."""
    del contract, query
    if not isinstance(body, dict):
        return
    groups = body.get("filterGroups")
    if not isinstance(groups, list):
        return
    for g_index, group in enumerate(groups):
        filters = group.get("filters") if isinstance(group, dict) else None
        if not isinstance(filters, list):
            continue
        for f_index, item in enumerate(filters):
            if not isinstance(item, dict):
                continue
            where = f"body filterGroups[{g_index}].filters[{f_index}]"
            operator = item.get("operator")
            if operator in _RANGE_OPERATORS and "value" not in item:
                errors.append(f"{where}: {operator} needs value")
            elif operator == "BETWEEN" and ("value" not in item or "highValue" not in item):
                errors.append(f"{where}: BETWEEN needs value and highValue")
            elif operator in ("IN", "NOT_IN") and not item.get("values"):
                errors.append(f"{where}: {operator} needs values")


_SURFACE_RULES: Final[Mapping[str, _Rule]] = MappingProxyType(
    {
        "google.search": _rule_google_search,
        "linkedin.ad_analytics": _rule_analytics,
        "linkedin.ad_analytics_statistics": _rule_analytics,
        "linkedin.ad_campaign_groups": _rule_entity_search,
        "linkedin.ad_campaigns": _rule_entity_search,
        "linkedin.creatives": _rule_creatives,
        "linkedin.posts": _rule_posts,
        "linkedin.lead_forms": _rule_lead_forms,
        "linkedin.lead_form_responses": _rule_lead_responses,
        "linkedin.lead_form_responses_batch": _rule_lead_batch,
        "hubspot.objects_batch_read": _rule_hubspot_inputs,
        "hubspot.associations_batch_read": _rule_hubspot_inputs,
        "hubspot.crm_search": _rule_hubspot_search,
    }
)


# ---------------------------------------------------------------------------------------------
# Surface identification
# ---------------------------------------------------------------------------------------------

_HUBSPOT_VERSION: Final = "2026-09"
_LINKEDIN_OAUTH_HOST: Final = "www.linkedin.com"
_LINKEDIN_OAUTH_PATH: Final = "/oauth/v2/accessToken"
_REST_PREFIX: Final = "/rest/"


def identify_surface(source: Source, request: RequestShape | httpx.Request) -> str:
    """Name the contract surface a request addresses, by method and path pattern.

    Identification never needs a world, so ``capture-fixtures`` uses it on live requests. It
    is host-independent except for the LinkedIn OAuth endpoint on ``www.linkedin.com``.

    * google: any ``POST`` whose JSON body is an object with a ``query`` key is ``search``.
    * linkedin: ``POST`` OAuth ``accessToken``; under ``/rest/`` (``GET``): ``adAccounts/{id}``,
      ``adAccounts/{id}/adCampaignGroups``, ``adAccounts/{id}/adCampaigns``,
      ``adAccounts/{id}/creatives``, ``posts``, ``adAnalytics`` (``q=statistics`` is
      ``ad_analytics_statistics``, anything else ``ad_analytics``), ``leadForms`` and
      ``leadFormResponses`` (with ``ids`` the batch get, otherwise the owner finder).
    * hubspot: the ``2026-09`` paths ``POST /crm/objects/2026-09/{type}/batch/read``,
      ``POST /crm/objects/2026-09/{type}/search``,
      ``POST /crm/associations/2026-09/{from}/{to}/batch/read`` and
      ``GET /crm/properties/2026-09/{objectType}/{propertyName}``. Numbered paths (``/v3/``,
      ``/v4/``) and other date versions are unknown.

    Args:
        source: ``google``, ``linkedin`` or ``hubspot``.
        request: The request.

    Returns:
        The surface name, a member of ``SURFACES[source]``.

    Raises:
        UnknownSurface: When the request matches no surface of the source.
    """
    if source not in SURFACES:
        raise UnknownSurface(f"unknown source {source!r}")
    shape = _shape(request)
    try:
        url = httpx.URL(shape.url)
    except httpx.InvalidURL:
        raise UnknownSurface(f"{source}: the request URL is not a URL") from None
    if source == "google":
        return _google_surface(shape)
    if source == "linkedin":
        return _linkedin_surface(shape, url)
    return _hubspot_surface(shape, url)


def _google_surface(shape: RequestShape) -> str:
    """``search`` for a POST with a JSON object body carrying ``query``."""
    if shape.method == "POST" and shape.body:
        try:
            body = json.loads(shape.body.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and "query" in body:
            return "search"
    raise UnknownSurface("google: only POSTs carrying a GAQL query reach the webhook")


def _linkedin_surface(shape: RequestShape, url: httpx.URL) -> str:
    """Match a LinkedIn path."""
    path = url.path
    if shape.method == "POST" and url.host == _LINKEDIN_OAUTH_HOST and path == _LINKEDIN_OAUTH_PATH:
        return "access_token"
    if shape.method == "GET" and path.startswith(_REST_PREFIX):
        segments = path[len(_REST_PREFIX) :].split("/")
        query = _split_query(url)
        if segments[0] == "adAccounts" and len(segments) in (2, 3) and segments[1]:
            if len(segments) == 2:
                return "ad_account"
            surface = _ACCOUNT_SUBRESOURCES.get(segments[2])
            if surface is not None:
                return surface
        elif segments == ["posts"]:
            return "posts"
        elif segments == ["adAnalytics"]:
            finder = query.raw("q")
            return "ad_analytics_statistics" if finder == "statistics" else "ad_analytics"
        elif segments == ["leadForms"]:
            return "lead_forms"
        elif segments == ["leadFormResponses"]:
            batch = "ids" in query.names()
            return "lead_form_responses_batch" if batch else "lead_form_responses"
    raise UnknownSurface(f"linkedin: no surface serves {shape.method} {path}")


_ACCOUNT_SUBRESOURCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "adCampaignGroups": "ad_campaign_groups",
        "adCampaigns": "ad_campaigns",
        "creatives": "creatives",
    }
)


def _hubspot_surface(shape: RequestShape, url: httpx.URL) -> str:
    """Match a HubSpot ``2026-09`` path."""
    segments = url.path.strip("/").split("/")
    if len(segments) >= 3 and segments[0] == "crm" and segments[2] == _HUBSPOT_VERSION:
        kind, rest = segments[1], segments[3:]
        if shape.method == "POST" and kind == "objects":
            if len(rest) == 3 and rest[0] and rest[1:] == ["batch", "read"]:
                return "objects_batch_read"
            if len(rest) == 2 and rest[0] and rest[1] == "search":
                return "crm_search"
        if (
            shape.method == "POST"
            and kind == "associations"
            and len(rest) == 4
            and rest[0]
            and rest[1]
            and rest[2:] == ["batch", "read"]
        ):
            return "associations_batch_read"
        if shape.method == "GET" and kind == "properties" and len(rest) == 2 and all(rest):
            return "properties"
    raise UnknownSurface(f"hubspot: no 2026-09 surface serves {shape.method} {url.path}")


def row_surface_for(query: str) -> str:
    """The ``row_*`` contract of a GAQL query (``QUERIES[query_id_for(query)].row_surface``).

    Raises:
        UnknownSurface: When the query is not a recognised template.
    """
    query_id = query_id_for(query)
    if query_id is None:
        raise UnknownSurface("google: not a recognised GAQL template")
    return QUERIES[query_id].row_surface


# ---------------------------------------------------------------------------------------------
# Rendered responses, platform error bodies and deterministic identifiers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderedResponse:
    """A simulated response, ready for the transport to serialise.

    Attributes:
        status: HTTP status (ignored when ``timeout`` is true).
        body: The JSON body, or ``None`` for no content.
        headers: Extra headers such as ``Retry-After``.
        timeout: True when the request must time out instead of answering.
        surface: The contract surface that answered.
        fault: The world fault kinds applied to the request, comma-separated in spec order
            (``"timeout"``, ``"omit_ids,batch_errors"``), or ``None`` when no fault applied.
            Set by :func:`render`; ``simulation/transport.py`` journals and logs it.
    """

    status: int
    body: JSONValue | None
    headers: tuple[tuple[str, str], ...] = ()
    timeout: bool = False
    surface: str = field(kw_only=True)
    fault: str | None = field(default=None, kw_only=True)


class _RejectionError(Exception):
    """A request the platform rejects although it satisfies the contract's shape.

    Examples: a customer or account the simulated world does not hold, an unknown page token,
    an operator the simulated portal does not implement. Rendered as the platform's error.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str | None = None,
        google_code: JSONObject | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.google_code = google_code


class _FaultResponseError(Exception):
    """Carries a fault response out of a renderer."""

    def __init__(self, response: RenderedResponse) -> None:
        super().__init__(response.surface)
        self.response = response


def _digest(*parts: object) -> str:
    """A SHA-256 hex digest of ``parts`` (stable across processes; never ``hash()``)."""
    text = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _uuid_like(digest: str) -> str:
    """Format 32 hex characters of a digest as a UUID."""
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _token(*parts: object) -> str:
    """An opaque, URL-safe cursor that encodes ``parts`` (decoded by :func:`_untoken`)."""
    text = "|".join(str(part) for part in parts)
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _untoken(token: str) -> list[str] | None:
    """Decode a cursor made by :func:`_token`; ``None`` when it is not one."""
    padded = token + "=" * (-len(token) % 4)
    try:
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except ValueError, UnicodeError:
        return None
    return text.split("|")


_GOOGLE_FAILURE_TYPE: Final = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
_RPC_STATUS: Final[Mapping[int, str]] = MappingProxyType(
    {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        409: "ABORTED",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
        501: "UNIMPLEMENTED",
        503: "UNAVAILABLE",
        504: "DEADLINE_EXCEEDED",
    }
)
# Google error codes a fault may name: (HTTP-equivalent status, google.rpc status, errorCode).
# They follow the Google Ads API v25 error catalogue; connectors/errors.py classifies the same
# tokens (authorization and authentication errors deny access, quota exhaustion is transient,
# query errors are defects), so each fault lands in the classification it models.
_GOOGLE_CODES: Final[Mapping[str, tuple[int, str, Mapping[str, str]]]] = MappingProxyType(
    {
        "PERMISSION_DENIED": (
            403,
            "PERMISSION_DENIED",
            {"authorizationError": "USER_PERMISSION_DENIED"},
        ),
        "AuthorizationError": (
            403,
            "PERMISSION_DENIED",
            {"authorizationError": "USER_PERMISSION_DENIED"},
        ),
        "UNAUTHENTICATED": (401, "UNAUTHENTICATED", {"authenticationError": "OAUTH_TOKEN_INVALID"}),
        "AuthenticationError": (
            401,
            "UNAUTHENTICATED",
            {"authenticationError": "OAUTH_TOKEN_INVALID"},
        ),
        "RESOURCE_EXHAUSTED": (429, "RESOURCE_EXHAUSTED", {"quotaError": "RESOURCE_EXHAUSTED"}),
        "QueryError": (400, "INVALID_ARGUMENT", {"queryError": "UNRECOGNIZED_FIELD"}),
        "INVALID_ARGUMENT": (400, "INVALID_ARGUMENT", {"requestError": "UNKNOWN"}),
        "INTERNAL": (500, "INTERNAL", {"internalError": "INTERNAL_ERROR"}),
        "UNAVAILABLE": (503, "UNAVAILABLE", {"internalError": "TRANSIENT_ERROR"}),
        "DEADLINE_EXCEEDED": (504, "DEADLINE_EXCEEDED", {"internalError": "DEADLINE_EXCEEDED"}),
    }
)
_GOOGLE_CODE_BY_STATUS: Final[Mapping[int, str]] = MappingProxyType(
    {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
        503: "UNAVAILABLE",
        504: "DEADLINE_EXCEEDED",
    }
)
_LINKEDIN_CODES: Final[Mapping[int, tuple[str, int]]] = MappingProxyType(
    {
        400: ("ILLEGAL_ARGUMENT", 100),
        401: ("INVALID_ACCESS_TOKEN", 65600),
        403: ("ACCESS_DENIED", 100),
        404: ("NOT_FOUND", 0),
        429: ("TOO_MANY_REQUESTS", 0),
        500: ("INTERNAL_SERVER_ERROR", 0),
        503: ("SERVICE_UNAVAILABLE", 0),
    }
)
_OAUTH_CODES: Final[Mapping[int, str]] = MappingProxyType(
    {400: "invalid_request", 401: "invalid_client", 429: "too_many_requests"}
)
_HUBSPOT_CATEGORIES: Final[Mapping[int, str]] = MappingProxyType(
    {
        400: "VALIDATION_ERROR",
        401: "INVALID_AUTHENTICATION",
        403: "MISSING_SCOPES",
        404: "OBJECT_NOT_FOUND",
        409: "CONFLICT",
        429: "RATE_LIMITS",
    }
)
_DEFAULT_MESSAGES: Final[Mapping[int, str]] = MappingProxyType(
    {
        400: "The request is invalid.",
        401: "The access token is invalid or expired.",
        403: "The caller does not have permission.",
        404: "The requested resource was not found.",
        429: "Too many requests; retry later.",
        500: "Internal error encountered.",
        502: "Bad gateway.",
        503: "The service is currently unavailable.",
        504: "The request deadline was exceeded.",
    }
)


def _default_message(status: int) -> str:
    """A platform-style message for a status without one."""
    return _DEFAULT_MESSAGES.get(status, "The request failed.")


def _google_error(status: int, code: str | None, message: str, digest: str) -> JSONObject:
    """A Google Ads API error as n8n relays it.

    A bare 5xx without a Google code is n8n's own ``{"message": "Error in workflow"}``, the body
    the live webhook returns when the Google node fails (observed 2026-10-07).
    """
    if code is None and status >= 500:
        return {"message": message}
    name = code if code is not None else _GOOGLE_CODE_BY_STATUS.get(status, "INVALID_ARGUMENT")
    known = _GOOGLE_CODES.get(name)
    # Inside an n8n HTTP 200 the error's own ``code`` is the HTTP equivalent of the failure;
    # otherwise it is the status actually returned.
    if status != 200:
        http_code = status
    elif known is not None:
        http_code = known[0]
    else:
        http_code = 400
    rpc = known[1] if known is not None else _RPC_STATUS.get(http_code, name)
    failure: JSONObject = {"message": message}
    if known is not None:
        failure = {"errorCode": dict(known[2]), "message": message}
    return {
        "error": {
            "code": http_code,
            "message": message,
            "status": rpc,
            "details": [
                {
                    "@type": _GOOGLE_FAILURE_TYPE,
                    "errors": [failure],
                    "requestId": digest[:22],
                }
            ],
        }
    }


def _error_body(
    source: Source,
    surface: str,
    status: int,
    *,
    code: str | None,
    message: str,
    digest: str,
    google_code: JSONObject | None = None,
) -> JSONObject:
    """The platform's own error body for ``status``.

    Google: the ``google.rpc.Status`` with a ``GoogleAdsFailure`` detail (``status`` 200 for an
    n8n ``body_error``). LinkedIn REST: ``{status, serviceErrorCode, code, message}``; the
    OAuth endpoint: ``{error, error_description}``. HubSpot: ``{status: "error", message,
    correlationId, category}``.
    """
    if source == "google":
        body = _google_error(status, code, message, digest)
        if google_code is not None and "error" in body:
            error = cast(JSONObject, body["error"])
            details = cast(list[JSONValue], error["details"])
            detail = cast(JSONObject, details[0])
            detail["errors"] = [{"errorCode": google_code, "message": message}]
        return body
    if source == "linkedin":
        if surface == "access_token":
            return {
                "error": code if code is not None else _OAUTH_CODES.get(status, "server_error"),
                "error_description": message,
            }
        default_code, service = _LINKEDIN_CODES.get(status, ("INTERNAL_SERVER_ERROR", 0))
        return {
            "status": status,
            "serviceErrorCode": service,
            "code": code if code is not None else default_code,
            "message": message,
        }
    category = code if code is not None else _HUBSPOT_CATEGORIES.get(status, "INTERNAL_ERROR")
    return {
        "status": "error",
        "message": message,
        "correlationId": _uuid_like(digest),
        "category": category,
    }


# ---------------------------------------------------------------------------------------------
# Faults
# ---------------------------------------------------------------------------------------------

_WHOLE_FAULTS: Final = frozenset({"http_status", "timeout", "body_error", "non_complete_status"})
_BATCH_STATUS_SURFACES: Final = frozenset(
    {"hubspot.objects_batch_read", "hubspot.associations_batch_read"}
)
# The id-level fault kinds each surface can express; a kind a surface cannot express never
# applies there, so it is never consumed by a request it could not affect.
_ID_FAULTS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "linkedin.posts": frozenset({"batch_errors", "omit_ids"}),
        "linkedin.lead_form_responses": frozenset(
            {"omit_ids", "decoration_mismatch", "no_decoration"}
        ),
        "linkedin.lead_form_responses_batch": frozenset(
            {"batch_errors", "omit_ids", "decoration_mismatch", "no_decoration"}
        ),
        "hubspot.objects_batch_read": frozenset({"batch_errors", "omit_ids"}),
        "hubspot.associations_batch_read": frozenset(
            {"batch_errors", "omit_ids", "continuation_failure"}
        ),
    }
)


@dataclass(frozen=True, slots=True)
class _FaultPlan:
    """The faults applied to one request.

    Attributes:
        whole: A fault that shapes the whole response, or ``None``.
        by_id: Id-level faults, in spec order.
    """

    whole: Fault | None = None
    by_id: tuple[Fault, ...] = ()

    def ids(self, kind: str) -> frozenset[str]:
        """Every id an id-level fault of ``kind`` names."""
        return frozenset(i for fault in self.by_id if fault.kind == kind for i in fault.ids)

    def fault_for(self, kind: str, identifier: str) -> Fault | None:
        """The first id-level fault of ``kind`` naming ``identifier``."""
        for fault in self.by_id:
            if fault.kind == kind and identifier in fault.ids:
                return fault
        return None

    @property
    def batch_status(self) -> str | None:
        """The batch status of a ``non_complete_status`` fault, else ``None``."""
        if self.whole is not None and self.whole.kind == "non_complete_status":
            return self.whole.batch_status
        return None

    def label(self) -> str | None:
        """The applied fault kinds, for the render log line."""
        kinds = [self.whole.kind] if self.whole is not None else []
        kinds.extend(fault.kind for fault in self.by_id)
        return ",".join(dict.fromkeys(kinds)) if kinds else None


def _select_faults(
    world: SimulatedWorld,
    source: Source,
    surfaces: Sequence[str],
    surface_key: str,
    attributes: Mapping[str, str],
    ids: frozenset[str],
    continuation_ids: frozenset[str],
) -> _FaultPlan:
    """Claim the faults that apply to one request, in spec order.

    The first relevant fault decides: a whole-response fault (``http_status``, ``timeout``,
    ``body_error``, or ``non_complete_status`` on a batch surface) is the response, alone. When
    the first relevant fault is id-level, every relevant id-level fault applies together (an
    omission and a batch error can shape one batch), and whole-response faults wait for a later
    request. A fault is relevant when its ``match`` predicates hold and, for an id-level kind,
    when the surface can express it and the request touches one of its ids. Each applied fault
    is claimed with :meth:`SimulatedWorld.consume_fault`, which honours ``times``.
    """
    applicable = {id(fault) for name in surfaces for fault in world.faults_for(source, name)}
    expressible = _ID_FAULTS.get(surface_key, frozenset())
    chosen: list[Fault] = []
    for fault in world.spec.faults:
        if id(fault) not in applicable or not fault.matches(attributes):
            continue
        if fault.kind in _WHOLE_FAULTS:
            if chosen:
                continue
            if fault.kind == "non_complete_status" and surface_key not in _BATCH_STATUS_SURFACES:
                continue
            if fault.kind == "body_error" and source != "google":
                continue
            if world.consume_fault(fault):
                return _FaultPlan(whole=fault)
            continue
        if fault.kind not in expressible:
            continue
        pool = continuation_ids if fault.kind == "continuation_failure" else ids
        if not pool.intersection(fault.ids):
            continue
        if world.consume_fault(fault):
            chosen.append(fault)
    return _FaultPlan(by_id=tuple(chosen))


@dataclass(slots=True)
class _Ctx:
    """Everything a surface renderer reads: the validated request and the world."""

    source: Source
    surface: str
    prepared: _Prepared
    world: SimulatedWorld
    fault_surfaces: tuple[str, ...]
    plan: _FaultPlan = field(default_factory=_FaultPlan)

    @property
    def key(self) -> str:
        """``{source}.{surface}``."""
        return f"{self.source}.{self.surface}"

    @property
    def query(self) -> _Query:
        """The split query string."""
        return self.prepared.query

    @property
    def body(self) -> JSONValue:
        """The decoded request body."""
        return self.prepared.body

    @property
    def path(self) -> str:
        """The URL path."""
        url = self.prepared.url
        return url.path if url is not None else ""

    def segments(self) -> list[str]:
        """The URL path segments."""
        return self.path.strip("/").split("/")

    def leaf(self, name: str) -> str | None:
        """A plain query parameter, decoded once.

        Raises:
            _RejectionError: On a malformed percent escape (a parameter no rule checked,
                such as an opaque ``pageToken``).
        """
        raw = self.query.raw(name)
        if raw is None:
            return None
        try:
            return _decode_once(raw)
        except ValueError:
            raise _RejectionError(f"query parameter {name}: malformed percent escape") from None

    def restli(self, name: str) -> RestliValue | None:
        """A Rest.li query parameter (validated already)."""
        raw = self.query.raw(name)
        return None if raw is None else parse_restli(raw, surface=self.key)

    def integer(self, name: str, default: int) -> int:
        """An integer query parameter, or ``default`` when absent."""
        value = self.leaf(name)
        return default if value is None else int(value)

    def faults(
        self,
        attributes: Mapping[str, str],
        *,
        ids: Iterable[str] = (),
        continuation_ids: Iterable[str] = (),
    ) -> _FaultPlan:
        """Select this request's faults; a whole-response fault interrupts the renderer.

        Raises:
            _FaultResponseError: Carrying the fault's response (``http_status``, ``timeout``,
                ``body_error``). ``non_complete_status`` is returned for the batch renderer.
        """
        plan = _select_faults(
            self.world,
            self.source,
            self.fault_surfaces,
            self.key,
            attributes,
            frozenset(ids),
            frozenset(continuation_ids),
        )
        self.plan = plan
        whole = plan.whole
        if whole is not None and whole.kind != "non_complete_status":
            raise _FaultResponseError(_fault_response(self, whole))
        return plan

    def digest(self, *parts: object) -> str:
        """A digest bound to the world seed, the surface and the request path."""
        return _digest(self.world.seed, self.key, self.prepared.shape.method, self.path, *parts)

    def error(
        self,
        status: int,
        message: str,
        *,
        code: str | None = None,
        google_code: JSONObject | None = None,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> RenderedResponse:
        """The platform's error response."""
        body = _error_body(
            self.source,
            self.surface,
            status,
            code=code,
            message=message,
            digest=self.digest(status, message),
            google_code=google_code,
        )
        return RenderedResponse(status, body, headers, surface=self.surface)


def _fault_response(ctx: _Ctx, fault: Fault) -> RenderedResponse:
    """The response a whole-response fault produces."""
    if fault.kind == "timeout":
        return RenderedResponse(504, None, timeout=True, surface=ctx.surface)
    if fault.kind == "body_error":
        relayed = fault.message or f"Google Ads API error {fault.code}"
        return ctx.error(200, relayed, code=fault.code)
    status = cast(int, fault.status)
    headers: tuple[tuple[str, str], ...] = ()
    if fault.retry_after is not None:
        headers = (("Retry-After", str(fault.retry_after)),)
    # A Google 5xx without a Google code is n8n's own workflow failure.
    n8n_failure = ctx.source == "google" and fault.code is None and status >= 500
    default = "Error in workflow" if n8n_failure else _default_message(status)
    return ctx.error(status, fault.message or default, code=fault.code, headers=headers)


# ---------------------------------------------------------------------------------------------
# Wire-value helpers
# ---------------------------------------------------------------------------------------------

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def _ms(instant: datetime) -> int:
    """Epoch milliseconds of an aware instant, with integer arithmetic."""
    return (instant - _EPOCH) // timedelta(milliseconds=1)


def _iso(instant: datetime) -> str:
    """HubSpot's instant format: ``2026-09-04T13:05:22.123Z``."""
    utc = instant.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _amount(value: Decimal) -> str:
    """A decimal amount as a plain string (never exponent notation)."""
    return format(value, "f")


def _double(value: Decimal) -> float:
    """A DOUBLE wire value (``float(Decimal)`` rounds correctly to the nearest double)."""
    return float(value)


def _put(row: JSONObject, path: tuple[str, ...], value: JSONValue) -> None:
    """Set ``value`` at a nested camelCase ``path`` of ``row``."""
    node = row
    for key in path[:-1]:
        child = node.setdefault(key, {})
        node = cast(JSONObject, child)
    node[path[-1]] = value


# ---------------------------------------------------------------------------------------------
# Google: googleAds:search v25 relayed by the n8n webhook
# ---------------------------------------------------------------------------------------------


def _field_mask(template: QueryTemplate) -> str:
    """The envelope's ``fieldMask``: the selected fields as comma-joined camelCase paths."""
    return ",".join(".".join(rest_path(name)) for name in template.select)


@dataclass(frozen=True, slots=True)
class _GoogleRow:
    """One row before assembly: values by GAQL field, resource names by REST object key."""

    values: Mapping[str, JSONValue]
    names: Mapping[str, str]


def _int64(value: int) -> str:
    """INT64 values travel as decimal strings in proto3 JSON."""
    return str(value)


def _criterion_name(cid: str, ad_group_id: int, criterion_id: int) -> str:
    """``customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}``."""
    return f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"


def _customer_row(record: GoogleCustomerRecord) -> _GoogleRow:
    """Q-G1."""
    cid = record.customer_id
    return _GoogleRow(
        values={
            "customer.id": cid,
            "customer.descriptive_name": record.descriptive_name,
            "customer.currency_code": record.currency_code,
            "customer.time_zone": record.time_zone,
        },
        names={"customer": f"customers/{cid}"},
    )


def _campaign_row(cid: str, record: GoogleCampaignRecord) -> _GoogleRow:
    """Q-G2 and Q-G2b. ``explicitlyShared`` has implicit presence: omitted when false."""
    names = {"campaign": f"customers/{cid}/campaigns/{record.id}"}
    values: dict[str, JSONValue] = {
        "campaign.id": _int64(record.id),
        "campaign.name": record.name,
        "campaign.status": record.status,
        "campaign.advertising_channel_type": record.channel,
    }
    if record.budget_id is not None:
        names["campaignBudget"] = f"customers/{cid}/campaignBudgets/{record.budget_id}"
        values["campaign_budget.id"] = _int64(record.budget_id)
        values["campaign_budget.period"] = record.budget_period
        if record.budget_amount_micros is not None:
            values["campaign_budget.amount_micros"] = _int64(record.budget_amount_micros)
        if record.budget_total_amount_micros is not None:
            values["campaign_budget.total_amount_micros"] = _int64(
                record.budget_total_amount_micros
            )
        if record.budget_explicitly_shared:
            values["campaign_budget.explicitly_shared"] = True
    return _GoogleRow(values=values, names=names)


def _campaign_day_row(cid: str, record: GoogleCampaignDay) -> _GoogleRow:
    """Q-G3. Absent impression shares are omitted (unavailable, never zero)."""
    values: dict[str, JSONValue] = {
        "campaign.id": _int64(record.campaign_id),
        "segments.date": record.date.isoformat(),
        "metrics.cost_micros": _int64(record.cost_micros),
        "metrics.impressions": _int64(record.impressions),
        "metrics.clicks": _int64(record.clicks),
        "metrics.conversions": _double(record.conversions),
        "metrics.conversions_value": _double(record.conversions_value),
    }
    if record.search_budget_lost_impression_share is not None:
        values["metrics.search_budget_lost_impression_share"] = _double(
            record.search_budget_lost_impression_share
        )
    if record.search_impression_share is not None:
        values["metrics.search_impression_share"] = _double(record.search_impression_share)
    return _GoogleRow(
        values=values, names={"campaign": f"customers/{cid}/campaigns/{record.campaign_id}"}
    )


def _ad_group_row(cid: str, record: GoogleAdGroupRecord) -> _GoogleRow:
    """Q-G4."""
    return _GoogleRow(
        values={
            "ad_group.id": _int64(record.id),
            "ad_group.name": record.name,
            "ad_group.status": record.status,
            "campaign.id": _int64(record.campaign_id),
        },
        names={
            "adGroup": f"customers/{cid}/adGroups/{record.id}",
            "campaign": f"customers/{cid}/campaigns/{record.campaign_id}",
        },
    )


def _keyword_day_row(cid: str, record: GoogleKeywordDay) -> _GoogleRow:
    """Q-G5. ``averageCpc`` is a DOUBLE in micros; an unpopulated score or CPC is omitted."""
    values: dict[str, JSONValue] = {
        "ad_group_criterion.criterion_id": _int64(record.criterion_id),
        "ad_group.id": _int64(record.ad_group_id),
        "segments.date": record.date.isoformat(),
        "metrics.cost_micros": _int64(record.cost_micros),
        "metrics.clicks": _int64(record.clicks),
        "metrics.impressions": _int64(record.impressions),
        "metrics.conversions": _double(record.conversions),
    }
    if record.historical_quality_score is not None:
        values["metrics.historical_quality_score"] = _int64(record.historical_quality_score)
    if record.average_cpc_micros is not None:
        values["metrics.average_cpc"] = float(record.average_cpc_micros)
    pair = f"{record.ad_group_id}~{record.criterion_id}"
    return _GoogleRow(
        values=values,
        names={
            "keywordView": f"customers/{cid}/keywordViews/{pair}",
            "adGroupCriterion": f"customers/{cid}/adGroupCriteria/{pair}",
            "adGroup": f"customers/{cid}/adGroups/{record.ad_group_id}",
        },
    )


def _search_term_name(cid: str, record: GoogleSearchTermDay) -> str:
    """``customers/{cid}/searchTermViews/{campaign}~{ad group}~{URL-base64 term}``."""
    encoded = base64.urlsafe_b64encode(record.search_term.encode("utf-8")).decode("ascii")
    key = f"{record.campaign_id}~{record.ad_group_id}~{encoded.rstrip('=')}"
    return f"customers/{cid}/searchTermViews/{key}"


def _search_term_row(cid: str, record: GoogleSearchTermDay) -> _GoogleRow:
    """Q-G6. Traffic without a matched keyword omits ``segments.keyword``."""
    values: dict[str, JSONValue] = {
        "search_term_view.search_term": record.search_term,
        "search_term_view.status": record.status,
        "ad_group.id": _int64(record.ad_group_id),
        "campaign.id": _int64(record.campaign_id),
        "segments.date": record.date.isoformat(),
        "metrics.cost_micros": _int64(record.cost_micros),
        "metrics.clicks": _int64(record.clicks),
        "metrics.impressions": _int64(record.impressions),
        "metrics.conversions": _double(record.conversions),
        "metrics.conversions_value": _double(record.conversions_value),
    }
    if record.criterion_id is not None:
        values["segments.keyword.ad_group_criterion"] = _criterion_name(
            cid, record.ad_group_id, record.criterion_id
        )
    if record.keyword_text is not None and record.match_type is not None:
        values["segments.keyword.info.text"] = record.keyword_text
        values["segments.keyword.info.match_type"] = record.match_type
    return _GoogleRow(
        values=values,
        names={
            "searchTermView": _search_term_name(cid, record),
            "adGroup": f"customers/{cid}/adGroups/{record.ad_group_id}",
            "campaign": f"customers/{cid}/campaigns/{record.campaign_id}",
        },
    )


def _keyword_row(cid: str, record: GoogleKeywordRecord) -> _GoogleRow:
    """Q-G7. An unpopulated quality score is omitted (``qualityInfo`` absent)."""
    values: dict[str, JSONValue] = {
        "ad_group_criterion.criterion_id": _int64(record.criterion_id),
        "ad_group.id": _int64(record.ad_group_id),
        "campaign.id": _int64(record.campaign_id),
        "ad_group_criterion.keyword.text": record.text,
        "ad_group_criterion.keyword.match_type": record.match_type,
        "ad_group_criterion.status": record.status,
    }
    if record.quality_score is not None:
        values["ad_group_criterion.quality_info.quality_score"] = record.quality_score
    return _GoogleRow(
        values=values,
        names={
            "adGroupCriterion": _criterion_name(cid, record.ad_group_id, record.criterion_id),
            "adGroup": f"customers/{cid}/adGroups/{record.ad_group_id}",
            "campaign": f"customers/{cid}/campaigns/{record.campaign_id}",
        },
    )


def _conversion_action_row(cid: str, record: GoogleConversionActionRecord) -> _GoogleRow:
    """Q-G8. ``includeInConversionsMetric`` has explicit presence: sent even when false."""
    return _GoogleRow(
        values={
            "conversion_action.id": _int64(record.id),
            "conversion_action.name": record.name,
            "conversion_action.status": record.status,
            "conversion_action.include_in_conversions_metric": (
                record.include_in_conversions_metric
            ),
            "conversion_action.counting_type": record.counting_type,
            "conversion_action.attribution_model_settings.attribution_model": (
                record.attribution_model
            ),
        },
        names={"conversionAction": f"customers/{cid}/conversionActions/{record.id}"},
    )


def _assemble(paths: Sequence[tuple[str, tuple[str, ...]]], row: _GoogleRow) -> JSONObject:
    """A REST ``GoogleAdsRow`` with exactly the selected fields plus resource names.

    ``paths`` pairs each selected GAQL field with its camelCase path (computed once per query).
    """
    assembled: JSONObject = {}
    values = row.values
    for name, path in paths:
        value = values.get(name)
        if value is not None:
            _put(assembled, path, value)
    for key, resource_name in row.names.items():
        node = cast(JSONObject, assembled.setdefault(key, {}))
        node["resourceName"] = resource_name
    return assembled


@dataclass(frozen=True, slots=True)
class _Selection:
    """The rows a query selects, counted up front and built only for the page served.

    Attributes:
        count: The number of rows the query selects (after ``LIMIT``).
        rows: Builds the rows ``[start, stop)``.
    """

    count: int
    rows: Callable[[int, int], list[_GoogleRow]]


def _select[T](
    records: Sequence[T], build: Callable[[T], _GoogleRow], limit: int | None
) -> _Selection:
    """A :class:`_Selection` over ``records``, cut at the query's ``LIMIT``."""
    kept = records if limit is None else records[:limit]
    return _Selection(
        count=len(kept), rows=lambda start, stop: [build(record) for record in kept[start:stop]]
    )


def _id_range(scope: Mapping[str, object], low: str, high: str) -> tuple[int, int] | None:
    """An inclusive id range from the parsed scope, or ``None`` when the query has none."""
    if low in scope and high in scope:
        return cast(int, scope[low]), cast(int, scope[high])
    return None


def _in_range(value: int, bounds: tuple[int, int] | None) -> bool:
    """Whether ``value`` lies in an inclusive range (``None`` = unbounded)."""
    return bounds is None or bounds[0] <= value <= bounds[1]


def _google_rows(world: SimulatedWorld, parsed: ParsedQuery, cid: str) -> _Selection:
    """The rows a recognised query selects, filtered, ordered and limited as GAQL would.

    Entity queries read the world at ``world.now()``. Q-G2, Q-G4 and Q-G7 exclude ``REMOVED``
    (their fixed conditions); Q-G2b, Q-G8 and the dated metrics queries have no status filter,
    so a removed campaign's spend still counts. Keyset scans order by their id and apply
    ``LIMIT``; other queries keep the world's stable order (ids ascending). Rows are built
    lazily, so a partition step that reads 10,000 of 15,000 rows builds only those 10,000.
    """
    scope = parsed.scope
    query_id = parsed.query_id
    at = world.now()
    last_id = cast(int, scope[LAST_ID]) if LAST_ID in scope else None
    limit = parsed.limit
    if query_id == "Q-G1":
        return _select([world.google_customer()], _customer_row, limit)
    if query_id in ("Q-G2", "Q-G2b"):
        campaigns = sorted(world.google_campaigns(at), key=lambda record: record.id)
        if query_id == "Q-G2":
            after = cast(int, last_id)
            selected = [c for c in campaigns if c.status != "REMOVED" and c.id > after]
        else:
            wanted = set(cast(tuple[int, ...], scope[IDS]))
            selected = [c for c in campaigns if c.id in wanted]
        return _select(selected, lambda record: _campaign_row(cid, record), limit)
    if query_id == "Q-G3":
        days = world.google_campaign_daily(
            cast(date, scope[START]),
            cast(date, scope[END]),
            campaign_ids=_id_range(scope, CAMPAIGN_LO, CAMPAIGN_HI),
        )
        return _select(days, lambda record: _campaign_day_row(cid, record), limit)
    if query_id == "Q-G4":
        after = cast(int, last_id)
        groups = sorted(
            (g for g in world.google_ad_groups(at) if g.status != "REMOVED" and g.id > after),
            key=lambda record: record.id,
        )
        return _select(groups, lambda record: _ad_group_row(cid, record), limit)
    if query_id == "Q-G5":
        keyword_days = _keyword_days(world, scope, last_id)
        return _select(keyword_days, lambda record: _keyword_day_row(cid, record), limit)
    if query_id == "Q-G6":
        terms = world.google_search_term_daily(
            cast(date, scope[START]),
            cast(date, scope[END]),
            campaign_ids=_id_range(scope, CAMPAIGN_LO, CAMPAIGN_HI),
            ad_group_ids=_id_range(scope, AD_GROUP_LO, AD_GROUP_HI),
        )
        return _select(terms, lambda record: _search_term_row(cid, record), limit)
    if query_id == "Q-G7":
        keywords = _keywords(world, scope, last_id)
        return _select(keywords, lambda record: _keyword_row(cid, record), limit)
    actions = sorted(world.google_conversion_actions(at), key=lambda record: record.id)
    return _select(actions, lambda record: _conversion_action_row(cid, record), limit)


def _ad_group_scope(scope: Mapping[str, object]) -> tuple[int, int] | None:
    """The ad-group bound of a query: one ad group (keyset form) or an id range."""
    if AD_GROUP_ID in scope:
        single = cast(int, scope[AD_GROUP_ID])
        return single, single
    return _id_range(scope, AD_GROUP_LO, AD_GROUP_HI)


def _keyword_days(
    world: SimulatedWorld, scope: Mapping[str, object], last_id: int | None
) -> list[GoogleKeywordDay]:
    """Q-G5 rows; the per-ad-group keyset form orders by criterion id after ``last_id``."""
    days = list(
        world.google_keyword_daily(
            cast(date, scope[START]),
            cast(date, scope[END]),
            campaign_ids=_id_range(scope, CAMPAIGN_LO, CAMPAIGN_HI),
            ad_group_ids=_ad_group_scope(scope),
        )
    )
    if last_id is not None:
        days = [day for day in days if day.criterion_id > last_id]
        days.sort(key=lambda day: day.criterion_id)
    return days


def _keywords(
    world: SimulatedWorld, scope: Mapping[str, object], last_id: int | None
) -> list[GoogleKeywordRecord]:
    """Q-G7 rows: non-removed keywords in the ad-group bound, keyset by criterion id."""
    bounds = _ad_group_scope(scope)
    keywords = [
        keyword
        for keyword in world.google_keywords(world.now())
        if keyword.status != "REMOVED" and _in_range(keyword.ad_group_id, bounds)
    ]
    if last_id is not None:
        keywords = [keyword for keyword in keywords if keyword.criterion_id > last_id]
        keywords.sort(key=lambda keyword: keyword.criterion_id)
    else:
        keywords.sort(key=lambda keyword: (keyword.ad_group_id, keyword.criterion_id))
    return keywords


def _render_google(ctx: _Ctx) -> RenderedResponse:
    """``search``: the n8n envelope ``{results[], fieldMask, queryResourceConsumption}``.

    Rows are capped at :data:`PAGE_SIZE`. By default no page token is returned, as the
    user-specified n8n response carries none: an exactly-10,000-row page without a token is
    how truncation looks (AAP 0.2.4). A world with ``emit_page_tokens`` returns a
    deterministic ``nextPageToken`` and serves the continuation for ``page_token``. An empty
    result omits ``results`` and keeps ``fieldMask``, as Google omits empty repeated fields.
    """
    world = ctx.world
    body = cast(JSONObject, ctx.body)
    customer_id = cast(str, body["customer_id"])
    text = cast(str, body["query"])
    token = body.get("page_token")
    spec = world.spec.google
    if spec is None:
        raise _RejectionError(
            "The simulated world has no Google Ads account.",
            google_code={"requestError": "INVALID_CUSTOMER_ID"},
        )
    if customer_id != world.google_customer_id:
        raise _RejectionError(
            "The simulated world serves one Google Ads customer; the customer_id differs.",
            google_code={"requestError": "INVALID_CUSTOMER_ID"},
        )
    parsed = parse_query(text)
    query_key = _digest(world.seed, text)[:16]
    offset = 0
    if token is not None:
        decoded = _untoken(cast(str, token))
        valid = (
            spec.emit_page_tokens
            and decoded is not None
            and len(decoded) == 3
            and decoded[0] == "gads"
            and decoded[1] == query_key
            and _DIGITS_RE.fullmatch(decoded[2]) is not None
        )
        if not valid or decoded is None:
            raise _RejectionError(
                "The page token is invalid for this query.",
                google_code={"requestError": "INVALID_PAGE_TOKEN"},
            )
        offset = int(decoded[2])
    attributes = {
        "query_id": parsed.query_id,
        "customer_id": customer_id,
        "page": str(offset // PAGE_SIZE + 1),
    }
    ctx.faults(attributes)
    selection = _google_rows(world, parsed, customer_id)
    page = selection.rows(offset, offset + PAGE_SIZE)
    paths = [(name, rest_path(name)) for name in parsed.template.select]
    envelope: JSONObject = {
        "fieldMask": _field_mask(parsed.template),
        "queryResourceConsumption": _int64(len(page) // 100 + 1),
    }
    if page:
        envelope["results"] = [cast(JSONValue, _assemble(paths, row)) for row in page]
    if spec.emit_page_tokens and offset + PAGE_SIZE < selection.count:
        envelope["nextPageToken"] = _token("gads", query_key, offset + PAGE_SIZE)
    return RenderedResponse(200, envelope, surface=ctx.surface)


# ---------------------------------------------------------------------------------------------
# LinkedIn Marketing API 202609
# ---------------------------------------------------------------------------------------------

_ACCOUNT_URN: Final = "urn:li:sponsoredAccount:"
_CAMPAIGN_URN: Final = "urn:li:sponsoredCampaign:"
_GROUP_URN: Final = "urn:li:sponsoredCampaignGroup:"
_CREATIVE_URN: Final = "urn:li:sponsoredCreative:"
_LEAD_SCOPES: Final = "r_ads,r_ads_reporting,r_marketing_leadgen_automation"
_ACCESS_TOKEN_TTL_S: Final = 5_184_000
_REFRESH_TOKEN_TTL_S: Final = 31_536_000
_DEFAULT_LOCALE: Final[JSONObject] = {"country": "US", "language": "en"}


def _li_account(ctx: _Ctx, account_id: str) -> LinkedInAccountRecord:
    """The simulated account ``account_id``; an account the world does not hold is a 404.

    Raises:
        _RejectionError: When the world has no such LinkedIn account.
    """
    if account_id not in ctx.world.linkedin_account_ids:
        raise _RejectionError(
            f"Ad account {account_id} does not exist in the simulated world.",
            status=404,
        )
    for account in ctx.world.linkedin_accounts():
        if str(account.id) == account_id:
            return account
    raise _RejectionError(
        f"Ad account {account_id} does not exist in the simulated world.", status=404
    )


def _owner_id(ctx: _Ctx) -> str:
    """The account id of the (validated) ``owner`` parameter."""
    return cast(str, _owner_account(ctx.restli("owner"), []))


def _audit(created: datetime, modified: datetime) -> JSONObject:
    """``changeAuditStamps`` with both stamps."""
    return {"created": {"time": _ms(created)}, "lastModified": {"time": _ms(modified)}}


def _money(amount: Decimal, currency: str) -> JSONObject:
    """A ``{amount, currencyCode}`` budget object."""
    return {"amount": _amount(amount), "currencyCode": currency}


def _cursor_offset(ctx: _Ctx, account_id: str) -> int:
    """The offset a ``pageToken`` resumes from (0 without one).

    Raises:
        _RejectionError: When the token was not issued for this surface and account.
    """
    token = ctx.leaf("pageToken")
    if token is None:
        return 0
    decoded = _untoken(token)
    if (
        decoded is None
        or len(decoded) != 4
        or decoded[:3] != ["li", ctx.surface, account_id]
        or not _DIGITS_RE.fullmatch(decoded[3])
    ):
        raise _RejectionError("The pageToken is invalid for this request.")
    return int(decoded[3])


def _cursor_page[T](
    ctx: _Ctx, account_id: str, records: Sequence[T]
) -> tuple[Sequence[T], str | None, int]:
    """One ``pageSize`` page of ``records`` and the ``nextPageToken`` (``None`` at the end)."""
    page_size = ctx.integer("pageSize", 100)
    offset = _cursor_offset(ctx, account_id)
    page = records[offset : offset + page_size]
    following = offset + page_size
    token = _token("li", ctx.surface, account_id, following) if following < len(records) else None
    return page, token, offset // page_size + 1


def _render_access_token(ctx: _Ctx) -> RenderedResponse:
    """``POST oauth/v2/accessToken``: a refresh-token grant answered with fixed lifetimes.

    Tokens are derived from the presented refresh token's digest: deterministic, and clearly
    simulated (``sim-token-…``), so they never resemble a real credential.
    """
    form = cast(JSONObject, ctx.body)
    ctx.faults({"page": "1"})
    digest = ctx.digest(form.get("client_id"), form.get("refresh_token"))
    body: JSONObject = {
        "access_token": f"sim-token-{digest[:32]}",
        "expires_in": _ACCESS_TOKEN_TTL_S,
        "refresh_token": f"sim-refresh-{digest[32:64]}",
        "refresh_token_expires_in": _REFRESH_TOKEN_TTL_S,
        "scope": _LEAD_SCOPES,
    }
    return RenderedResponse(200, body, surface=ctx.surface)


def _render_ad_account(ctx: _Ctx) -> RenderedResponse:
    """``GET adAccounts/{id}``."""
    account_id = ctx.segments()[2]
    account = _li_account(ctx, account_id)
    ctx.faults({"account_id": account_id, "page": "1"})
    body: JSONObject = {
        "id": account.id,
        "name": account.name,
        "currency": account.currency,
        "status": account.status,
        "type": account.type,
        "test": False,
        "changeAuditStamps": _audit(account.created_at, account.created_at),
    }
    return RenderedResponse(200, body, surface=ctx.surface)


def _group_element(record: LinkedInCampaignGroupRecord) -> JSONObject:
    """One campaign group: ``dailyBudget`` is a bare amount, ``totalBudget`` an object."""
    schedule: JSONObject = {"start": _ms(record.run_start)}
    if record.run_end is not None:
        schedule["end"] = _ms(record.run_end)
    element: JSONObject = {
        "id": record.id,
        "name": record.name,
        "account": f"{_ACCOUNT_URN}{record.account_id}",
        "status": record.status,
        "runSchedule": schedule,
        "test": False,
        "changeAuditStamps": _audit(record.created_at, record.last_modified_at),
    }
    if record.total_budget is not None:
        element["totalBudget"] = _money(record.total_budget, record.currency)
    if record.daily_budget is not None:
        element["dailyBudget"] = _amount(record.daily_budget)
    return element


def _campaign_element(record: LinkedInCampaignRecord) -> JSONObject:
    """One campaign: both budgets are ``{amount, currencyCode}`` objects."""
    element: JSONObject = {
        "id": record.id,
        "name": record.name,
        "account": f"{_ACCOUNT_URN}{record.account_id}",
        "campaignGroup": f"{_GROUP_URN}{record.group_id}",
        "status": record.status,
        "type": "SPONSORED_UPDATES",
        "objectiveType": record.objective,
        "runSchedule": {"start": _ms(record.run_start)},
        "test": False,
        "changeAuditStamps": _audit(record.created_at, record.last_modified_at),
        "version": {"versionTag": record.version_tag},
    }
    if record.daily_budget is not None:
        element["dailyBudget"] = _money(record.daily_budget, record.currency)
    if record.total_budget is not None:
        element["totalBudget"] = _money(record.total_budget, record.currency)
    return element


def _render_entity_search(ctx: _Ctx) -> RenderedResponse:
    """``adCampaignGroups`` / ``adCampaigns`` ``q=search``: only the requested statuses.

    A client that omits a status (for example ``COMPLETED``) misses those entities, exactly as
    live. Pages follow ``metadata.nextPageToken`` until it is ``null``.
    """
    account_id = ctx.segments()[2]
    _li_account(ctx, account_id)
    statuses = set(_search_statuses(ctx.restli("search"), []) or ())
    now = ctx.world.now()
    elements: list[JSONValue]
    if ctx.surface == "ad_campaign_groups":
        groups = [
            record
            for record in ctx.world.linkedin_campaign_groups(account_id, at=now)
            if record.status in statuses
        ]
        groups.sort(key=lambda record: record.id)
        group_page, token, page = _cursor_page(ctx, account_id, groups)
        ctx.faults({"account_id": account_id, "page": str(page)})
        elements = [cast(JSONValue, _group_element(record)) for record in group_page]
    else:
        campaigns = [
            record
            for record in ctx.world.linkedin_campaigns(account_id, at=now)
            if record.status in statuses
        ]
        campaigns.sort(key=lambda record: record.id)
        campaign_page, token, page = _cursor_page(ctx, account_id, campaigns)
        ctx.faults({"account_id": account_id, "page": str(page)})
        elements = [cast(JSONValue, _campaign_element(record)) for record in campaign_page]
    body: JSONObject = {"elements": elements, "metadata": {"nextPageToken": token}}
    return RenderedResponse(200, body, surface=ctx.surface)


def _creative_element(record: LinkedInCreativeRecord) -> JSONObject:
    """One creative; the lead-gen destination is documented as ``urn:li:adForm:{id}``."""
    element: JSONObject = {
        "id": record.urn,
        "campaign": f"{_CAMPAIGN_URN}{record.campaign_id}",
        "account": f"{_ACCOUNT_URN}{record.account_id}",
        "intendedStatus": record.intended_status,
        "content": {"reference": record.post_urn},
        "isTest": False,
        "createdAt": _ms(record.created_at),
        "lastModifiedAt": _ms(record.last_modified_at),
    }
    if record.lead_form_id is not None:
        element["leadgenCallToAction"] = {
            "destination": f"urn:li:adForm:{record.lead_form_id}",
            "label": record.cta_label,
        }
    return element


def _render_creatives(ctx: _Ctx) -> RenderedResponse:
    """``creatives?q=criteria``: the listed campaigns' creatives in the listed statuses."""
    account_id = ctx.segments()[2]
    _li_account(ctx, account_id)
    campaigns = _urn_list(ctx.restli("campaigns"), _CAMPAIGN_URN_RE, "campaigns", [])
    statuses = {cast(str, item) for item in cast(list[RestliValue], ctx.restli("intendedStatuses"))}
    records = [
        record
        for record in ctx.world.linkedin_creatives(account_id, campaigns, at=ctx.world.now())
        if record.intended_status in statuses
    ]
    records.sort(key=lambda record: record.id, reverse=ctx.leaf("sortOrder") == "DESCENDING")
    page_records, token, page = _cursor_page(ctx, account_id, records)
    ctx.faults({"account_id": account_id, "page": str(page)})
    body: JSONObject = {
        "elements": [cast(JSONValue, _creative_element(record)) for record in page_records],
        "metadata": {"nextPageToken": token},
    }
    return RenderedResponse(200, body, surface=ctx.surface)


def _post_element(record: LinkedInPostRecord) -> JSONObject:
    """One post. The simulated ad account doubles as the authoring organization.

    A landing-page post carries an article; a lead-gen post a document (``media``).
    """
    content: JSONObject
    if record.landing_page_url is not None:
        content = {"article": {"source": record.landing_page_url, "title": record.headline}}
    else:
        content = {"media": {"id": f"urn:li:document:{record.id}", "title": record.headline}}
    instant = _ms(record.created_at)
    return {
        "id": record.urn,
        "author": f"urn:li:organization:{record.account_id}",
        "commentary": record.commentary,
        "content": content,
        "lifecycleState": "PUBLISHED",
        "visibility": "PUBLIC",
        "createdAt": instant,
        "lastModifiedAt": instant,
        "publishedAt": instant,
    }


def _batch_error(fault: Fault, default_status: int, message: str) -> tuple[int, JSONObject]:
    """A LinkedIn batch ``errors`` entry for a ``batch_errors`` fault."""
    status = fault.status if fault.status is not None else default_status
    entry: JSONObject = {"status": status, "message": fault.message or message}
    if fault.category is not None:
        entry["code"] = fault.category
    return status, entry


def _render_posts(ctx: _Ctx) -> RenderedResponse:
    """``posts?ids=List(...)`` batch get: ``{results, statuses, errors}`` keyed by URN.

    ``statuses`` and ``errors`` are top-level only (the posts contract declares them there).
    """
    urns = list(dict.fromkeys(_urn_list(ctx.restli("ids"), _POST_URN_RE, "ids", [])))
    plan = ctx.faults({"page": "1"}, ids=urns)
    found = {record.urn: record for record in ctx.world.linkedin_posts(urns)}
    omitted = plan.ids("omit_ids")
    results: JSONObject = {}
    statuses: JSONObject = {}
    errors: JSONObject = {}
    for urn in urns:
        if urn in omitted:
            continue
        fault = plan.fault_for("batch_errors", urn)
        record = found.get(urn)
        if fault is not None:
            statuses[urn], errors[urn] = _batch_error(fault, 500, "The post could not be read.")
        elif record is None:
            statuses[urn] = 404
            errors[urn] = {"status": 404, "message": "The post does not exist."}
        else:
            statuses[urn] = 200
            results[urn] = _post_element(record)
    body: JSONObject = {"results": results, "statuses": statuses, "errors": errors}
    return RenderedResponse(200, body, surface=ctx.surface)


def _analytics_window(ctx: _Ctx) -> tuple[date, date]:
    """The (validated) ``dateRange``; a missing end is the current UTC date."""
    window = cast(tuple[date, date | None], _date_range(ctx.restli("dateRange"), []))
    start, end = window
    return start, end if end is not None else ctx.world.now().astimezone(UTC).date()


def _restli_date(day: date) -> JSONObject:
    """``{year, month, day}``."""
    return {"year": day.year, "month": day.month, "day": day.day}


def _daily_element(day: LinkedInCreativeDay, fields: frozenset[str]) -> JSONObject:
    """One creative-day data point, holding only the projected fields."""
    values: dict[str, JSONValue] = {
        "pivotValues": [f"{_CREATIVE_URN}{day.creative_id}"],
        "dateRange": {"start": _restli_date(day.date), "end": _restli_date(day.date)},
        "costInLocalCurrency": _amount(day.cost),
        "impressions": day.impressions,
        "clicks": day.clicks,
        "landingPageClicks": day.landing_page_clicks,
        "oneClickLeadFormOpens": day.form_opens,
        "oneClickLeads": day.leads,
        "externalWebsiteConversions": day.website_conversions,
    }
    return {name: value for name, value in values.items() if name in fields}


def _segment_element(record: LinkedInSegmentRecord, fields: frozenset[str]) -> JSONObject:
    """One (campaign, demographic value) aggregate; metrics the pivot lacks are absent."""
    values: dict[str, JSONValue | None] = {
        "pivotValues": [f"{_CAMPAIGN_URN}{record.campaign_id}", record.value],
        "costInLocalCurrency": None if record.cost is None else _amount(record.cost),
        "impressions": record.impressions,
        "clicks": record.clicks,
        "oneClickLeads": record.leads,
        "externalWebsiteConversions": record.website_conversions,
    }
    return {name: value for name, value in values.items() if name in fields and value is not None}


def _render_analytics(ctx: _Ctx) -> RenderedResponse:
    """``adAnalytics``: daily creative facts (``q=analytics``) or demographic aggregates.

    Responses are capped at the contract's ``max_elements`` (15,000). Beyond the cap, live
    LinkedIn truncates without a link; a world with ``analytics_next_links`` adds a
    ``paging.links`` ``next`` href that serves the remainder through ``start``/``count``.
    """
    accounts = [
        urn.removeprefix(_ACCOUNT_URN)
        for urn in _urn_list(ctx.restli("accounts"), _ACCOUNT_URN_RE, "accounts", [])
    ]
    for account_id in accounts:
        _li_account(ctx, account_id)
    start, end = _analytics_window(ctx)
    fields = frozenset(_top_level_fields(cast(str, ctx.leaf("fields"))))
    cap = ctx.prepared.contract.request.limit("max_elements") or 15_000
    offset = ctx.integer("start", 0)
    elements: list[JSONValue] = []
    attributes = {"account_id": accounts[0] if accounts else "", "page": str(offset // cap + 1)}
    if ctx.surface == "ad_analytics":
        days: list[LinkedInCreativeDay] = []
        for account_id in sorted(set(accounts), key=int):
            days.extend(ctx.world.linkedin_creative_daily(account_id, start, end))
        days.sort(key=lambda day: (day.date, day.creative_id))
        ctx.faults(attributes)
        elements = [cast(JSONValue, _daily_element(day, fields)) for day in days]
    else:
        pivot = cast(str, cast(list[RestliValue], ctx.restli("pivots"))[1])
        ctx.faults({**attributes, "pivot": pivot})
        for account_id in sorted(set(accounts), key=int):
            segments = ctx.world.linkedin_segments(account_id, pivot, start, end)
            elements.extend(
                cast(JSONValue, _segment_element(record, fields)) for record in segments
            )
    page = elements[offset : offset + cap]
    paging: JSONObject = {"count": cap, "start": offset, "links": []}
    linkedin = ctx.world.spec.linkedin
    if linkedin is not None and linkedin.analytics_next_links and offset + cap < len(elements):
        kept = "&".join(
            f"{name}={value}" for name, value in ctx.query.pairs if name not in ("start", "count")
        )
        href = f"{_REST_PREFIX}adAnalytics?{kept}&start={offset + cap}&count={cap}"
        paging["links"] = [{"rel": "next", "href": href, "type": "application/json"}]
    body: JSONObject = {"elements": page, "paging": paging}
    return RenderedResponse(200, body, surface=ctx.surface)


def _question(record: LinkedInQuestionRecord) -> JSONObject:
    """One form question as ``content.questions[]`` carries it."""
    question: JSONObject = {
        "questionId": record.question_id,
        "name": record.name,
        "question": {"localized": {"en_US": record.text}, "preferredLocale": dict(_DEFAULT_LOCALE)},
    }
    if record.predefined_field is not None:
        question["predefinedField"] = record.predefined_field
    return question


def _offset_page[T](ctx: _Ctx, records: Sequence[T]) -> tuple[Sequence[T], int, int, int]:
    """``count``/``start`` paging: the page, its start, its count and its 1-based number."""
    count = ctx.integer("count", 100)
    start = ctx.integer("start", 0)
    return records[start : start + count], start, count, start // count + 1


def _render_lead_forms(ctx: _Ctx) -> RenderedResponse:
    """``leadForms?q=owner``: each form's canonical id, current version and its questions.

    As live, ``paging.total`` counts this page's elements, so a client pages on until a
    short page.
    """
    account_id = _owner_id(ctx)
    _li_account(ctx, account_id)
    forms = sorted(
        ctx.world.linkedin_lead_forms(account_id, at=ctx.world.now()), key=lambda form: form.id
    )
    page, start, count, number = _offset_page(ctx, forms)
    ctx.faults({"account_id": account_id, "page": str(number)})
    elements: list[JSONValue] = [
        {
            "id": form.id,
            "versionId": form.current_version,
            "name": form.name,
            "state": form.state,
            "owner": {"sponsoredAccount": f"{_ACCOUNT_URN}{form.account_id}"},
            "created": _ms(form.created_at),
            "lastModified": _ms(form.last_modified_at),
            "content": {"questions": [cast(JSONValue, _question(q)) for q in form.questions]},
        }
        for form in page
    ]
    body: JSONObject = {
        "elements": elements,
        "paging": {"start": start, "count": count, "total": len(elements)},
    }
    return RenderedResponse(200, body, surface=ctx.surface)


@dataclass(frozen=True, slots=True)
class _LeadLookups:
    """Names and entities a lead response's ``*Info`` decorations carry."""

    account: LinkedInAccountRecord
    campaigns: Mapping[int, LinkedInCampaignRecord]
    creatives: Mapping[int, LinkedInCreativeRecord]
    forms: Mapping[int, LinkedInLeadFormRecord]


def _lead_lookups(ctx: _Ctx, account: LinkedInAccountRecord) -> _LeadLookups:
    """Read the account's campaigns, creatives and forms once per request."""
    now = ctx.world.now()
    account_id = str(account.id)
    return _LeadLookups(
        account=account,
        campaigns={c.id: c for c in ctx.world.linkedin_campaigns(account_id, at=now)},
        creatives={c.id: c for c in ctx.world.linkedin_creatives(account_id, at=now)},
        forms={f.id: f for f in ctx.world.linkedin_lead_forms(account_id, at=now)},
    )


def _versioned_form_urn(form_id: int, version: int) -> str:
    """``urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{id},{version})``."""
    return f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{form_id},{version})"


def _latest_version(lookups: _LeadLookups, response: LinkedInLeadResponse) -> int:
    """The highest published version of the response's form."""
    form = lookups.forms.get(response.form_id)
    current = form.current_version if form is not None else response.form_version
    return max(current, response.form_version)


def _mismatched_questions(
    ctx: _Ctx, lookups: _LeadLookups, response: LinkedInLeadResponse
) -> tuple[LinkedInQuestionRecord, ...]:
    """Questions of another version that lack one of the response's answered questions.

    When every other version holds all answered questions, the response's own questions
    less its highest answered question stand in, so the decoration never describes the
    response's version (the parser then reports "form map mismatch").
    """
    answered = {answer.question_id for answer in response.answers}
    for version in range(1, _latest_version(lookups, response) + 1):
        if version == response.form_version:
            continue
        other = ctx.world.linkedin_form_version(response.form_id, version)
        if other is not None and not answered <= {q.question_id for q in other.questions}:
            return other.questions
    own = ctx.world.linkedin_form_version(response.form_id, response.form_version)
    questions = own.questions if own is not None else ()
    dropped = max(answered) if answered else None
    return tuple(question for question in questions if question.question_id != dropped)


def _lead_element(
    ctx: _Ctx, lookups: _LeadLookups, response: LinkedInLeadResponse, plan: _FaultPlan
) -> JSONObject:
    """One sponsored lead response under the ``LEAD_FIELDS`` projection.

    Under the projection LinkedIn returns the ``*Info`` decorations (``ownerInfo``,
    ``leadMetadataInfo``, ``associatedEntityInfo``) instead of the plain ``leadMetadata`` and
    ``associatedEntity`` links (observed 2026-10-07). ``associatedEntityInfo`` is absent for a
    lead without a creative. The ``form`` decoration describes the response's own version and
    carries no version field. Faults: ``decoration_mismatch`` decorates with another
    version's questions; ``no_decoration`` drops the decoration and names a version that was
    never published, so no stored map can decode it ("unknown form version").
    """
    account_urn = f"{_ACCOUNT_URN}{response.account_id}"
    campaign = lookups.campaigns.get(response.campaign_id)
    form = lookups.forms.get(response.form_id)
    version = response.form_version
    no_decoration = response.id in plan.ids("no_decoration")
    if no_decoration:
        version = _latest_version(lookups, response) + 1
    campaign_info: JSONObject = {
        "id": f"{_CAMPAIGN_URN}{response.campaign_id}",
        "type": "SPONSORED_UPDATES",
    }
    if campaign is not None:
        campaign_info["name"] = campaign.name
    element: JSONObject = {
        "id": response.id,
        "submittedAt": _ms(response.submitted_at),
        "testLead": response.test_lead,
        "leadType": "SPONSORED",
        "owner": {"sponsoredAccount": account_urn},
        "ownerInfo": {
            "sponsoredAccountInfo": {"sponsoredAccount": account_urn, "name": lookups.account.name}
        },
        "leadMetadataInfo": {"sponsoredLeadMetadataInfo": {"campaign": campaign_info}},
        "versionedLeadGenFormUrn": _versioned_form_urn(response.form_id, version),
        "formResponse": {
            "answers": [
                {
                    "questionId": answer.question_id,
                    "answerDetails": {"textQuestionAnswer": {"answer": answer.answer}},
                }
                for answer in response.answers
            ]
        },
    }
    if response.creative_id is not None:
        creative_info: JSONObject = {"id": f"{_CREATIVE_URN}{response.creative_id}"}
        creative = lookups.creatives.get(response.creative_id)
        if creative is not None:
            creative_info["intendedStatus"] = creative.intended_status
            creative_info["content"] = {"reference": creative.post_urn}
        element["associatedEntityInfo"] = {"associatedCreative": creative_info}
    if no_decoration:
        return element
    if response.id in plan.ids("decoration_mismatch"):
        questions = _mismatched_questions(ctx, lookups, response)
    else:
        own = ctx.world.linkedin_form_version(response.form_id, response.form_version)
        if own is None:
            return element
        questions = own.questions
    element["form"] = {
        "id": response.form_id,
        "name": form.name if form is not None else f"Lead form {response.form_id}",
        "content": {"questions": [cast(JSONValue, _question(q)) for q in questions]},
    }
    return element


def _render_lead_responses(ctx: _Ctx) -> RenderedResponse:
    """``leadFormResponses?q=owner``: sponsored responses submitted in the time range.

    ``limitedToTestLeads=false`` returns test and real leads together (``testLead`` marks
    them); ``true`` only test leads. As live, ``paging.total`` is this page's element count.
    """
    account_id = _owner_id(ctx)
    account = _li_account(ctx, account_id)
    start_ms, end_ms = cast(tuple[int, int], _time_range(ctx.restli("submittedAtTimeRange"), []))
    try:
        responses = list(ctx.world.linkedin_lead_responses(account_id, start_ms, end_ms))
    except OverflowError:
        raise _RejectionError(
            "submittedAtTimeRange lies outside the representable range of instants."
        ) from None
    if ctx.leaf("limitedToTestLeads") == "true":
        responses = [response for response in responses if response.test_lead]
    form_filter = ctx.leaf("versionedLeadGenFormUrn")
    if form_filter is not None:
        responses = [r for r in responses if r.versioned_form_urn == form_filter]
    entity_filter = ctx.leaf("associatedEntity")
    if entity_filter is not None:
        responses = [
            r
            for r in responses
            if r.creative_id is not None and f"{_CREATIVE_URN}{r.creative_id}" == entity_filter
        ]
    page, start, count, number = _offset_page(ctx, responses)
    plan = ctx.faults(
        {"account_id": account_id, "page": str(number)}, ids=[response.id for response in page]
    )
    lookups = _lead_lookups(ctx, account)
    omitted = plan.ids("omit_ids")
    elements: list[JSONValue] = [
        cast(JSONValue, _lead_element(ctx, lookups, response, plan))
        for response in page
        if response.id not in omitted
    ]
    body: JSONObject = {
        "elements": elements,
        "paging": {"start": start, "count": count, "total": len(elements)},
    }
    return RenderedResponse(200, body, surface=ctx.surface)


def _render_lead_batch(ctx: _Ctx) -> RenderedResponse:
    """``leadFormResponses?ids=List(...)``: the batch re-fetch keyed by plain response id.

    Ids are sent unquoted. Quoted ids (raw or ``%22``) validate, as the documented vendor
    request does, but live 202609 answers them with empty ``results``, ``statuses`` and
    ``errors`` (observed 2026-10-07), and so does the simulator. An unknown id, or one of
    another owner, is absent with no error, as live; ``omit_ids`` and ``batch_errors``
    faults model "email no longer retrievable".
    """
    account_id = _owner_id(ctx)
    account = _li_account(ctx, account_id)
    raw_ids = cast(str, ctx.query.raw("ids"))
    quoted = '"' in raw_ids or "%22" in raw_ids.upper()
    ids = list(dict.fromkeys(_batch_ids(ctx.restli("ids"), [])))
    plan = ctx.faults({"account_id": account_id, "page": "1"}, ids=() if quoted else ids)
    results: JSONObject = {}
    statuses: JSONObject = {}
    errors: JSONObject = {}
    if not quoted:
        lookups = _lead_lookups(ctx, account)
        omitted = plan.ids("omit_ids")
        for identifier in ids:
            if identifier in omitted:
                continue
            fault = plan.fault_for("batch_errors", identifier)
            if fault is not None:
                statuses[identifier], errors[identifier] = _batch_error(
                    fault, 404, "The lead form response is not available."
                )
                continue
            response = ctx.world.linkedin_lead_response(identifier)
            if response is None or str(response.account_id) != account_id:
                continue
            results[identifier] = _lead_element(ctx, lookups, response, plan)
    body: JSONObject = {"results": results, "statuses": statuses, "errors": errors}
    return RenderedResponse(200, body, surface=ctx.surface)


_LINKEDIN_RENDERERS: Final[Mapping[str, Callable[[_Ctx], RenderedResponse]]] = MappingProxyType(
    {
        "access_token": _render_access_token,
        "ad_account": _render_ad_account,
        "ad_campaign_groups": _render_entity_search,
        "ad_campaigns": _render_entity_search,
        "creatives": _render_creatives,
        "posts": _render_posts,
        "ad_analytics": _render_analytics,
        "ad_analytics_statistics": _render_analytics,
        "lead_forms": _render_lead_forms,
        "lead_form_responses": _render_lead_responses,
        "lead_form_responses_batch": _render_lead_batch,
    }
)


# ---------------------------------------------------------------------------------------------
# HubSpot CRM 2026-09
# ---------------------------------------------------------------------------------------------

_HUBSPOT_OBJECTS: Final = ("contacts", "meetings", "deals")
# HubSpot-defined association type ids by direction, from HubSpot's "Associate records"
# guide (association type ID values): contact to meeting 199, meeting to contact 200, contact
# to deal 4, deal to contact 3.
_ASSOCIATION_TYPE_IDS: Final[Mapping[tuple[str, str], int]] = MappingProxyType(
    {
        ("contacts", "meetings"): 199,
        ("meetings", "contacts"): 200,
        ("contacts", "deals"): 4,
        ("deals", "contacts"): 3,
    }
)
_GROUP_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "contacts": "contactinformation",
        "meetings": "meetinginformation",
        "deals": "dealinformation",
    }
)
_UNSUPPORTED_OPERATORS: Final = frozenset({"CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN", "NOT_IN"})
_NO_ASSOCIATIONS: Final = "crm.associations.NO_ASSOCIATIONS_FOUND"


def _require_portal(ctx: _Ctx) -> None:
    """A world without a HubSpot part answers 404.

    Raises:
        _RejectionError: When the world simulates no HubSpot portal.
    """
    if ctx.world.spec.hubspot is None:
        raise _RejectionError("The simulated world has no HubSpot portal.", status=404)


def _object_type(ctx: _Ctx) -> str:
    """The path's object type, one the simulated portal holds.

    Raises:
        _RejectionError: For any other object type.
    """
    object_type = ctx.segments()[3]
    if object_type not in _HUBSPOT_OBJECTS:
        raise _RejectionError(f"The simulated portal does not hold object type {object_type}.")
    return object_type


def _property_text(value: PropertyValue) -> str | None:
    """A property value as HubSpot sends it: a string or null.

    Datetimes are ISO instants with milliseconds (``…T13:05:22.123Z``), date properties
    ``YYYY-MM-DD``, numbers plain decimal strings.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return _amount(value)
    return value


def _object_result(
    ctx: _Ctx,
    record: HubSpotRecord,
    requested: Sequence[str],
    *,
    email: str | None = None,
) -> JSONObject:
    """One CRM object: the requested properties the record has, plus ``hs_object_id``.

    ``email`` echoes the address a by-email lookup asked for, as HubSpot returns it; the
    plaintext exists only in this response object.
    """
    values = ctx.world.hubspot_properties(record)
    properties: JSONObject = {}
    for name in dict.fromkeys((*requested, "hs_object_id")):
        if name == "email" and email is not None:
            properties[name] = email
        elif name in values:
            properties[name] = _property_text(values[name])
    return {
        "id": record.id,
        "properties": properties,
        "createdAt": _iso(record.created_at),
        "updatedAt": _iso(record.updated_at),
        "archived": False,
    }


def _batch_envelope(
    ctx: _Ctx, status: str, results: list[JSONValue], errors: list[JSONValue]
) -> RenderedResponse:
    """A batch response: 207 with ``errors[]`` and ``numErrors`` when any input failed."""
    instant = _iso(ctx.world.now())
    body: JSONObject = {
        "status": status,
        "results": results,
        "startedAt": instant,
        "completedAt": instant,
    }
    if errors:
        body["errors"] = errors
        body["numErrors"] = len(errors)
    return RenderedResponse(207 if errors else 200, body, surface=ctx.surface)


def _fault_error(fault: Fault, context: JSONObject, message: str) -> JSONObject:
    """A 207 ``errors[]`` entry for a ``batch_errors`` fault."""
    entry: JSONObject = {
        "status": "error",
        "category": cast(str, fault.category),
        "message": fault.message or message,
        "context": context,
    }
    if fault.sub_category is not None:
        entry["subCategory"] = fault.sub_category
    return entry


def _normalise_email(value: str) -> str:
    """Trim and lowercase an address, as the lead pipeline normalises it before hashing."""
    return value.strip().lower()


def _contact_for_email(ctx: _Ctx, email: str) -> ContactRecord | None:
    """The contact a normalised email resolves to, found by its SHA-256 only.

    Simulated people are found by hash. A real lead email (not on the reserved simulated
    domain) is answered by a seeded hash-keyed contact for a deterministic share, which is
    how a simulated portal answers a live LinkedIn.
    """
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    contact = ctx.world.hubspot_contact_by_email_hash(digest)
    if contact is None and not email.endswith(f"@{SIM_EMAIL_DOMAIN}"):
        contact = ctx.world.hashed_contact_for(digest)
    return contact


def _records_by_id(ctx: _Ctx, object_type: str, ids: Sequence[str]) -> dict[str, HubSpotRecord]:
    """Records of one type by id; unknown ids are absent."""
    records: Sequence[HubSpotRecord]
    if object_type == "contacts":
        records = ctx.world.hubspot_contacts_by_id(ids)
    elif object_type == "meetings":
        records = ctx.world.hubspot_meetings(ids)
    else:
        records = ctx.world.hubspot_deals(ids)
    return {record.id: record for record in records}


def _render_objects_batch(ctx: _Ctx) -> RenderedResponse:
    """``POST /crm/objects/2026-09/{type}/batch/read`` by id, or contacts by email.

    Not-found inputs are one 207 ``OBJECT_NOT_FOUND`` entry listing them in ``context.ids``.
    By-id reads of hash-keyed contacts return ``email: null``. ``omit_ids`` drops inputs
    silently (an unread target or a failed contact refresh), ``batch_errors`` reports them,
    and ``non_complete_status`` returns its batch status with no results.
    """
    _require_portal(ctx)
    object_type = _object_type(ctx)
    body = cast(JSONObject, ctx.body)
    requested = [cast(str, name) for name in cast(list[JSONValue], body["properties"])]
    id_property = body.get("idProperty")
    by_email = id_property == "email"
    if id_property not in (None, "hs_object_id", "email") or (
        by_email and object_type != "contacts"
    ):
        raise _RejectionError("The simulated portal reads records by id, or contacts by email.")
    inputs = [
        cast(str, cast(JSONObject, item)["id"]) for item in cast(list[JSONValue], body["inputs"])
    ]
    keys = list(dict.fromkeys(_normalise_email(i) if by_email else i for i in inputs))
    found: dict[str, HubSpotRecord] = {}
    if by_email:
        for key in keys:
            contact = _contact_for_email(ctx, key)
            if contact is not None:
                found[key] = contact
    else:
        found = _records_by_id(ctx, object_type, keys)
    touched = [*keys, *(record.id for record in found.values())]
    plan = ctx.faults({"object_type": object_type, "page": "1"}, ids=touched)
    if plan.batch_status is not None:
        return _batch_envelope(ctx, plan.batch_status, [], [])
    omitted = plan.ids("omit_ids")
    results: list[JSONValue] = []
    emitted: set[str] = set()
    missing: list[JSONValue] = []
    failed: dict[int, tuple[Fault, list[JSONValue]]] = {}
    for key in keys:
        record = found.get(key)
        names = {key} if record is None else {key, record.id}
        if names & omitted:
            continue
        fault = next(
            (f for name in sorted(names) if (f := plan.fault_for("batch_errors", name))), None
        )
        if fault is not None:
            failed.setdefault(id(fault), (fault, []))[1].append(key)
        elif record is None:
            missing.append(key)
        elif record.id not in emitted:
            emitted.add(record.id)
            results.append(_object_result(ctx, record, requested, email=key if by_email else None))
    errors: list[JSONValue] = []
    if missing:
        singular = object_type.rstrip("s").upper()
        errors.append(
            {
                "status": "error",
                "category": "OBJECT_NOT_FOUND",
                "message": (
                    f"Could not get some {singular} objects, they may be deleted or not exist. "
                    "Check that ids are valid."
                ),
                "context": {"ids": missing},
            }
        )
    for fault, failed_keys in failed.values():
        errors.append(_fault_error(fault, {"ids": failed_keys}, "Some objects could not be read."))
    return _batch_envelope(ctx, "COMPLETE", results, errors)


def _association_error(
    category: str, message: str, object_id: str, sub_category: str | None = None
) -> JSONObject:
    """A 207 ``errors[]`` entry naming one input in ``context.fromObjectId``."""
    entry: JSONObject = {
        "status": "error",
        "category": category,
        "message": message,
        "context": {"fromObjectId": [object_id]},
    }
    if sub_category is not None:
        entry["subCategory"] = sub_category
    return entry


def _association_offset(after: str, key: tuple[str, str, str]) -> int | None:
    """The offset an association ``after`` cursor resumes, or ``None`` when it is not ours."""
    decoded = _untoken(after)
    if decoded is None or len(decoded) != 5 or decoded[0] != "hsa":
        return None
    if tuple(decoded[1:4]) != key or not _DIGITS_RE.fullmatch(decoded[4]):
        return None
    return int(decoded[4])


def _render_associations(ctx: _Ctx) -> RenderedResponse:
    """``POST /crm/associations/2026-09/{from}/{to}/batch/read``.

    Each input's ``to`` list is paged at the world's ``association_page_size`` with
    ``paging.next.after``; a continuation input ``{id, after}`` resumes that input only. An
    input with no associations is a 207 ``OBJECT_NOT_FOUND`` entry with subcategory
    ``crm.associations.NO_ASSOCIATIONS_FOUND`` and ``context.fromObjectId``, HubSpot's
    documented (and observed) behaviour.
    """
    _require_portal(ctx)
    segments = ctx.segments()
    from_type, to_type = segments[3], segments[4]
    type_id = _ASSOCIATION_TYPE_IDS.get((from_type, to_type))
    if type_id is None:
        raise _RejectionError(
            f"The simulated portal holds no {from_type} to {to_type} associations."
        )
    body = cast(JSONObject, ctx.body)
    inputs: list[tuple[str, str | None]] = []
    for item in cast(list[JSONValue], body["inputs"]):
        entry = cast(JSONObject, item)
        after = entry.get("after")
        inputs.append((cast(str, entry["id"]), after if isinstance(after, str) else None))
    inputs = list(dict.fromkeys(inputs))
    continuation = [identifier for identifier, after in inputs if after is not None]
    plan = ctx.faults(
        {
            "object_type": from_type,
            "to_object_type": to_type,
            "page": "2" if continuation else "1",
        },
        ids=[identifier for identifier, _after in inputs],
        continuation_ids=continuation,
    )
    if plan.batch_status is not None:
        return _batch_envelope(ctx, plan.batch_status, [], [])
    portal = ctx.world.spec.hubspot
    page_size = portal.association_page_size if portal is not None else 500
    omitted = plan.ids("omit_ids")
    results: list[JSONValue] = []
    errors: list[JSONValue] = []
    for identifier, after in inputs:
        if identifier in omitted:
            continue
        fault = plan.fault_for("batch_errors", identifier)
        if fault is not None:
            errors.append(
                _fault_error(
                    fault, {"fromObjectId": [identifier]}, "The associations could not be read."
                )
            )
            continue
        offset = 0
        if after is not None:
            failure = plan.fault_for("continuation_failure", identifier)
            resumed = _association_offset(after, (from_type, to_type, identifier))
            if failure is not None or resumed is None:
                errors.append(
                    _association_error(
                        (failure.category if failure is not None else None) or "VALIDATION_ERROR",
                        (failure.message if failure is not None else None)
                        or f"The paging cursor for {from_type} {identifier} cannot be resumed.",
                        identifier,
                        failure.sub_category if failure is not None else None,
                    )
                )
                continue
            offset = resumed
        targets = ctx.world.hubspot_associations(from_type, to_type, identifier)
        if not targets:
            errors.append(
                _association_error(
                    "OBJECT_NOT_FOUND",
                    f"No {to_type} is associated with {from_type} {identifier}.",
                    identifier,
                    _NO_ASSOCIATIONS,
                )
            )
            continue
        page = targets[offset : offset + page_size]
        result: JSONObject = {
            "from": {"id": identifier},
            "to": [
                {
                    "toObjectId": target,
                    "associationTypes": [
                        {"category": "HUBSPOT_DEFINED", "typeId": type_id, "label": None}
                    ],
                }
                for target in page
            ],
        }
        if offset + page_size < len(targets):
            cursor = _token("hsa", from_type, to_type, identifier, offset + page_size)
            result["paging"] = {"next": {"after": cursor, "link": f"?after={cursor}"}}
        results.append(result)
    return _batch_envelope(ctx, "COMPLETE", results, errors)


def _search_operand(text: str) -> int | str:
    """A filter operand: an ISO date or instant becomes epoch milliseconds, else verbatim.

    HubSpot compares date and datetime properties in epoch milliseconds and also accepts ISO
    values; the world compares epoch milliseconds, so ISO operands are converted here.
    """
    candidate = text.strip()
    if _DIGITS_RE.fullmatch(candidate):
        return candidate
    try:
        if len(candidate) == 10:
            day = date.fromisoformat(candidate)
            return _ms(datetime(day.year, day.month, day.day, tzinfo=UTC))
        instant = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return _ms(instant)


def _search_filters(body: JSONObject) -> list[list[SearchFilter]]:
    """The request's filter groups as typed world filters (groups are OR'd, filters AND'd).

    Raises:
        _RejectionError: For an operator the simulated portal does not implement, or operands the
            operator cannot use.
    """
    groups: list[list[SearchFilter]] = []
    for group in cast(list[JSONValue], body["filterGroups"]):
        filters: list[SearchFilter] = []
        for item in cast(list[JSONValue], cast(JSONObject, group)["filters"]):
            raw = cast(JSONObject, item)
            operator = cast(str, raw["operator"])
            if operator in _UNSUPPORTED_OPERATORS:
                raise _RejectionError(
                    f"The simulated portal does not implement the {operator} operator."
                )
            value = raw.get("value")
            high = raw.get("highValue")
            try:
                filters.append(
                    SearchFilter(
                        property=cast(str, raw["propertyName"]),
                        operator=operator,
                        value=_search_operand(value) if isinstance(value, str) else None,
                        high_value=_search_operand(high) if isinstance(high, str) else None,
                        values=tuple(cast(list[str], raw.get("values", []))),
                    )
                )
            except ValueError as exc:
                raise _RejectionError(f"Invalid filter: {exc}") from None
        groups.append(filters)
    return groups


def _sortable(value: PropertyValue) -> int | Decimal | str:
    """A property value in the order CRM Search sorts it (instants by epoch milliseconds)."""
    if isinstance(value, datetime):
        return _ms(value)
    if isinstance(value, date):
        return _ms(datetime(value.year, value.month, value.day, tzinfo=UTC))
    return cast(Decimal | str, value)


def _search_matches(
    ctx: _Ctx,
    object_type: str,
    groups: list[list[SearchFilter]],
    sort_property: str | None,
    descending: bool,
) -> list[HubSpotRecord]:
    """Records matching any filter group, ordered by id unless the request sorts.

    No group matches every record. With several groups the union is ordered like a single
    search: by id, then stably by the sort property with records lacking it last.
    """
    world = ctx.world
    if len(groups) <= 1:
        filters = groups[0] if groups else []
        return list(
            world.hubspot_search(
                object_type, filters, sort_property=sort_property, descending=descending
            )
        )
    union: dict[str, HubSpotRecord] = {}
    for filters in groups:
        for record in world.hubspot_search(object_type, filters):
            union.setdefault(record.id, record)
    matched = sorted(union.values(), key=lambda record: int(record.id))
    if sort_property is None:
        return matched
    present: list[tuple[HubSpotRecord, int | Decimal | str]] = []
    absent: list[HubSpotRecord] = []
    for record in matched:
        value = world.hubspot_properties(record).get(sort_property)
        if value is None:
            absent.append(record)
        else:
            present.append((record, _sortable(value)))
    present.sort(key=lambda item: cast(str, item[1]), reverse=descending)
    return [record for record, _value in present] + absent


def _render_search(ctx: _Ctx) -> RenderedResponse:
    """``POST /crm/objects/2026-09/{type}/search``.

    ``total`` counts every match; pages of ``limit`` follow ``paging.next.after`` (a numeric
    offset). Requesting ``after`` at or past 10,000 is rejected by validation with HTTP 400,
    so paging past the ceiling fails as live (AAP 0.2.4).
    """
    _require_portal(ctx)
    object_type = _object_type(ctx)
    body = cast(JSONObject, ctx.body)
    if isinstance(body.get("query"), str) and cast(str, body["query"]).strip():
        raise _RejectionError("The simulated portal does not implement free-text search queries.")
    sorts = cast(list[JSONValue], body["sorts"])
    if len(sorts) > 1:
        raise _RejectionError("CRM Search sorts by at most one property.")
    sort_property: str | None = None
    descending = False
    if sorts:
        sort = cast(JSONObject, sorts[0])
        sort_property = cast(str, sort["propertyName"])
        descending = sort["direction"] == "DESCENDING"
    groups = _search_filters(body)
    limit = cast(int, body["limit"])
    after_text = body.get("after")
    offset = int(after_text) if isinstance(after_text, str) else 0
    ctx.faults({"object_type": object_type, "page": str(offset // limit + 1)})
    matches = _search_matches(ctx, object_type, groups, sort_property, descending)
    requested = [cast(str, name) for name in cast(list[JSONValue], body["properties"])]
    page = matches[offset : offset + limit]
    response: JSONObject = {
        "total": len(matches),
        "results": [cast(JSONValue, _object_result(ctx, record, requested)) for record in page],
    }
    if offset + limit < len(matches):
        response["paging"] = {
            "next": {"after": str(offset + limit), "link": f"?after={offset + limit}"}
        }
    return RenderedResponse(200, response, surface=ctx.surface)


def _render_property(ctx: _Ctx) -> RenderedResponse:
    """``GET /crm/properties/2026-09/{objectType}/{propertyName}``; unknown is a 404.

    The world records name, label, type, field type and options; ``description`` (empty)
    and ``groupName`` (the object's information group) are required by the contract and
    filled deterministically.
    """
    _require_portal(ctx)
    segments = ctx.segments()
    object_type, name = segments[3], segments[4]
    ctx.faults({"object_type": object_type, "property_name": name, "page": "1"})
    record = (
        ctx.world.hubspot_property(object_type, name) if object_type in _HUBSPOT_OBJECTS else None
    )
    if record is None:
        return ctx.error(
            404,
            f"Unable to find property {name} for object type {object_type}.",
            code="OBJECT_NOT_FOUND",
        )
    body: JSONObject = {
        "name": record.name,
        "label": record.label,
        "type": record.type,
        "fieldType": record.field_type,
        "description": "",
        "groupName": _GROUP_NAMES[object_type],
        "options": [
            {
                "label": option.label,
                "value": option.value,
                "displayOrder": option.display_order,
                "hidden": option.hidden,
            }
            for option in record.options
        ],
        "hidden": False,
        "calculated": record.name.startswith("hs_v2_date_entered_"),
    }
    return RenderedResponse(200, body, surface=ctx.surface)


_HUBSPOT_RENDERERS: Final[Mapping[str, Callable[[_Ctx], RenderedResponse]]] = MappingProxyType(
    {
        "objects_batch_read": _render_objects_batch,
        "associations_batch_read": _render_associations,
        "crm_search": _render_search,
        "properties": _render_property,
    }
)


# ---------------------------------------------------------------------------------------------
# The render pipeline
# ---------------------------------------------------------------------------------------------


def _renderer(source: Source, surface: str) -> Callable[[_Ctx], RenderedResponse]:
    """The body builder of a surface."""
    if source == "google":
        return _render_google
    if source == "linkedin":
        return _LINKEDIN_RENDERERS[surface]
    return _HUBSPOT_RENDERERS[surface]


def _violation_message(errors: Sequence[str]) -> str:
    """The message of a 400 a contract violation renders: every rule, in order."""
    return "Simulated contract validation failed: " + "; ".join(errors)


def _google_violation_code(errors: Sequence[str]) -> JSONObject:
    """The GoogleAdsFailure error code of a violation: GAQL problems are query errors."""
    if any(problem.startswith("body query") for problem in errors):
        return {"queryError": "UNRECOGNIZED_FIELD"}
    if any(problem.startswith("body customer_id") for problem in errors):
        return {"requestError": "INVALID_CUSTOMER_ID"}
    return {"requestError": "UNKNOWN"}


def _element_count(body: JSONValue | None) -> int:
    """The number of results or elements a body carries, for the render log line."""
    if isinstance(body, dict):
        for key in ("results", "elements"):
            value = body.get(key)
            if isinstance(value, list | dict):
                return len(value)
    return 0


def render(
    source: Source, request: RequestShape | httpx.Request, world: SimulatedWorld
) -> RenderedResponse:
    """Answer a request from the simulated world in the platform's live wire shape.

    Pipeline: identify the surface; load its contract; validate the request (a violation is
    an HTTP 400 in the platform's own error shape, naming every violated rule: a defect the
    client never mocks or retries); apply the first matching unconsumed fault of
    ``world.faults_for`` (``match`` predicates and ``times`` honoured); build the body from
    world state at ``world.now()``; validate every 2xx body against the contract (and Google
    rows against their ``row_*`` contracts).

    Note: the live n8n webhook surfaces an invalid GAQL query as HTTP 500 "Error in
    workflow" (observed 2026-10-07). The simulator answers with Google's own 400
    ``INVALID_ARGUMENT`` failure instead, so a simulated defect is classified as a defect and
    never retried as a transient 5xx.

    Fault request attributes for ``match``: Google ``query_id``, ``customer_id``, ``page``;
    LinkedIn ``account_id``, ``page`` (and ``pivot`` for statistics); HubSpot
    ``object_type``, ``to_object_type`` (associations), ``property_name`` (properties) and
    ``page``. ``page`` is 1-based; a HubSpot association continuation call is page ``2``.

    Args:
        source: The source the transport serves.
        request: The request.
        world: The seeded simulated world.

    Returns:
        The response to serialise, or one that must time out.

    Raises:
        UnknownSurface: When the request matches no surface of ``source``.
        ContractViolation: When a rendered 2xx body breaks its contract, a simulator bug.
    """
    shape = _shape(request)
    surface = identify_surface(source, shape)
    contract = load_contract(source, surface)
    prepared = _prepare(contract, shape)
    fault_surfaces: tuple[str, ...] = (surface,)
    if source == "google" and isinstance(prepared.body, dict) and not prepared.errors:
        fault_surfaces = (surface, row_surface_for(cast(str, prepared.body["query"])))
    ctx = _Ctx(
        source=source,
        surface=surface,
        prepared=prepared,
        world=world,
        fault_surfaces=fault_surfaces,
    )
    if prepared.errors:
        google_code = _google_violation_code(prepared.errors) if source == "google" else None
        response = ctx.error(400, _violation_message(prepared.errors), google_code=google_code)
    else:
        try:
            response = _renderer(source, surface)(ctx)
        except _FaultResponseError as interrupt:
            response = interrupt.response
        except _RejectionError as defect:
            response = ctx.error(
                defect.status, defect.message, code=defect.code, google_code=defect.google_code
            )
    # An n8n body_error is an error carried inside HTTP 200, so it has no success schema.
    relayed_error = ctx.plan.whole is not None and ctx.plan.whole.kind == "body_error"
    if not response.timeout and 200 <= response.status < 300 and not relayed_error:
        validate_response(contract, response.body, status=response.status)
        if source == "google":
            validate_google_rows(
                cast(str, cast(JSONObject, prepared.body)["query"]),
                response.body,
                root=contract.path.parents[1],
            )
    fault_label = ctx.plan.label()
    if fault_label != response.fault:
        # The applied faults travel with the response, so the transport can journal them.
        response = replace(response, fault=fault_label)
    _LOG.debug(
        "simulation.render",
        source=source,
        surface=surface,
        status=None if response.timeout else response.status,
        elements=_element_count(response.body),
        fault=fault_label,
    )
    return response


def validate_capture(raw: JSONValue, *, root: Path | None = None) -> Contract:
    """Validate a recorded live exchange ``{surface, captured_at, request, response}``.

    ``request`` is ``{method, url, headers, body}`` (see :meth:`RequestShape.from_capture`);
    ``response`` is ``{status, body}`` (any further keys, such as headers, are ignored). The
    same functions that guard the simulator check it, so a capture can never pass a looser
    contract.

    Args:
        raw: The decoded capture file.
        root: The contracts directory; :func:`contracts_root` when ``None``.

    Returns:
        The contract the capture satisfies.

    Raises:
        ContractViolation: When the capture is malformed or breaks its contract.
        UnknownSurface: When it names an unknown surface, or its request matches another.
    """
    if not isinstance(raw, dict):
        raise ContractViolation("capture", ("$: a capture must be an object",))
    errors: list[str] = []
    for key in ("surface", "captured_at", "request", "response"):
        if key not in raw:
            errors.append(f"$.{key}: missing key")
    surface = raw.get("surface")
    response = raw.get("response")
    if not isinstance(surface, str) or "." not in surface:
        errors.append("$.surface: must be {source}.{name}")
    if not isinstance(response, dict) or not isinstance(response.get("status"), int):
        errors.append("$.response: must be an object with an integer status")
    if errors:
        raise ContractViolation("capture", errors)
    source_text, _dot, name = cast(str, surface).partition(".")
    if source_text not in SURFACES:
        raise UnknownSurface(f"unknown source {source_text!r}")
    source = cast(Source, source_text)
    contract = load_contract(source, name, root=root)
    shape = RequestShape.from_capture(raw["request"])
    if source != "google" or name == "search":
        identified = identify_surface(source, shape)
        if identified != name:
            raise UnknownSurface(f"the captured request addresses {source}.{identified}")
        validate_request(contract, shape)
    answer = cast(JSONObject, response)
    status = cast(int, answer["status"])
    body = answer.get("body")
    validate_response(contract, body, status=status)
    if source == "google" and name == "search" and 200 <= status < 300:
        query = json.loads(shape.body.decode("utf-8"))["query"]
        validate_google_rows(cast(str, query), body, root=root)
    return contract


# ---------------------------------------------------------------------------------------------
# Fixture serialisation
# ---------------------------------------------------------------------------------------------


def canonical_json(value: JSONValue) -> str:
    """Serialise a JSON value canonically: sorted keys, two-space indent, trailing newline.

    ``backend/tests/fixtures/`` snapshots are written and compared with this function.
    Rendered bodies are JSON-native already; ``Decimal`` is converted at render time, never
    here, so a non-JSON value raises ``TypeError``.
    """
    return (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, separators=(",", ": "))
        + "\n"
    )
