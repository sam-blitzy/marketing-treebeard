"""``httpx.MockTransport`` that answers one source's requests from the simulated world.

When a source is simulated (``DATA_MODE=simulated``, or access unavailable or denied under
``auto``), ``sync`` builds that source's live client exactly as it would for a live call,
``connectors.http.build_client(source, transport=mock_transport(source, world))``, and the
transport is the only difference (AAP 0.3.3 "Contract fidelity"). Every request is handed to
``payloads.render``, which identifies the contract surface, validates the request against its
contract file under ``tests/contracts/``, applies the world's faults and renders seeded state in
the platform's live wire shape; this module turns the result into an ``httpx.Response``.

Behavioural guarantees:

* **No network I/O.** The transport never opens a socket, whatever host a request names. It
  answers the configured live webhook URL, ``SIMULATED_WEBHOOK_URL``
  (``https://n8n.sim.invalid/webhook/google-ads``, from ``connectors/google_n8n/client.py``),
  ``https://api.linkedin.com/rest/…``, ``https://www.linkedin.com/oauth/v2/accessToken`` and
  ``https://api.hubapi.com/crm/…`` alike; hosts and paths are checked only against the contracts.
* **Contracts bind in simulated mode too.** A request that violates its contract (a missing
  owner selector, a missing webhook secret header, an unknown query parameter) is answered with
  HTTP 400 in the platform's own error shape, which the live client classifies as a defect: it
  is never retried and never mocked over. A request that matches none of the source's surfaces
  gets the same 400 treatment. With ``strict=True`` both raise instead (``ContractViolation``,
  ``UnknownSurface``, or ``SimulatedTransportError`` for a request meant for another source).
  A rendered body that breaks its own contract is a simulator bug and always raises
  ``ContractViolation``, in both modes; it is never disguised as a platform error.
* **Live behaviour above the transport is unchanged.** Clients, parsers, retries
  (``connectors.http.send_json`` with tenacity) and error classification run exactly as live:
  injected 429/5xx faults carry ``Retry-After`` when the fault sets one, and an injected timeout
  raises ``httpx.ReadTimeout`` at once, so the transient path is exercised without real time
  passing.
* **Simulated Google calls are never metered.** The transport never touches
  ``sync.budget_guard`` or ``source_connection.calls_24h``; ``sync`` hands the Google client its
  ``UNMETERED`` budget when Google is simulated, so simulated n8n calls never count against
  ``GOOGLE_DAILY_CALL_BUDGET``. Metering does not belong here: adding it would charge the
  product's live cap for calls that never left the process.
* **Simulated emails stay simulated.** Simulated LinkedIn leads carry ``@sim.invalid`` emails;
  when HubSpot is live the HubSpot client skips them, so they never reach a live portal, and
  this transport only ever answers them from the simulated HubSpot world.
* **Privacy.** The journal and the ``simulation.request`` debug log carry the source, surface,
  method, URL path, status and fault label only. Headers, query strings and bodies can hold
  tokens, emails or GAQL id lists, so they are never recorded.
* **Concurrency.** The handler keeps no mutable state of its own; the world's fault counters and
  caches are lock-guarded, and the optional journal is only appended to. Concurrent requests on
  one ``httpx.AsyncClient`` are safe.

Each transport serves exactly one source, matching ``build_client(source, transport=…)``. The
handler is asynchronous, so the transport serves ``httpx.AsyncClient`` (every product client).

Example::

    world = SimulatedWorld(42)
    journal: list[JournalEntry] = []
    async with httpx.AsyncClient(
        transport=mock_transport("google", world, journal=journal)
    ) as http:
        response = await http.post(
            SIMULATED_WEBHOOK_URL,
            headers={"X-Webhook-Secret": "simulated"},
            json={
                "customer_id": "9900000001",
                "query": QUERIES["Q-G1"].render(),
                "page_token": None,
            },
        )
    assert response.status_code == 200 and journal[0].surface == "search"
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Final

import httpx

from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Settings, Source
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.payloads import (
    ContractViolation,
    JSONValue,
    RenderedResponse,
    RequestShape,
    UnknownSurface,
    identify_surface,
    load_contract,
    render,
    validate_request,
)

__all__ = [
    "UNKNOWN_SURFACE",
    "JournalEntry",
    "SimulatedTransportError",
    "mock_transport",
]

_LOG: Final = get_logger("ads_engine.simulation")

UNKNOWN_SURFACE: Final = "unknown"
"""Journal surface of a request that matched none of the transport source's surfaces."""

# Platform error shapes of the 400 that answers a request matching no surface. They mirror the
# bodies ``payloads`` renders for a contract violation, so a client classifies both alike.
_GOOGLE_FAILURE_TYPE: Final = "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
_LINKEDIN_OAUTH_HOST: Final = "www.linkedin.com"
_LINKEDIN_OAUTH_PREFIX: Final = "/oauth/"
_JSON_CONTENT_TYPE: Final = "application/json"
_BAD_REQUEST: Final = 400


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One request the transport handled, in the order handled.

    Tests read the journal to assert call counts and order, such as how many webhook calls a
    keyset scan made or that an association continuation was re-sent only for paged inputs.

    Attributes:
        source: The source the transport serves.
        surface: The contract surface the request addressed, such as ``search`` or
            ``lead_form_responses``; :data:`UNKNOWN_SURFACE` when it matched none.
        method: The HTTP method, upper-cased.
        path: The URL path only. The query string, headers and body are never recorded,
            because they can carry tokens, emails or GAQL id lists.
        status: The HTTP status returned, or ``None`` when no response was returned: a
            simulated timeout, or an error raised in strict mode or for a simulator bug.
        fault: The world fault kinds applied (``payloads.RenderedResponse.fault``), or
            ``None`` when the request was answered without a fault.
    """

    source: Source
    surface: str
    method: str
    path: str
    status: int | None
    fault: str | None


class SimulatedTransportError(RuntimeError):
    """A strict transport received a request meant for another source.

    Each transport serves one source, so a HubSpot path sent through the LinkedIn transport is
    a wiring bug in the caller, not a platform answer. Raised only with ``strict=True``;
    otherwise the request is answered with HTTP 400 like any request matching no surface.

    Attributes:
        source: The source the transport serves.
        addressed: The ``{source}.{surface}`` the request matches instead.
    """

    def __init__(self, source: Source, addressed: str) -> None:
        """Name the transport's source and the surface the request addresses."""
        self.source = source
        self.addressed = addressed
        super().__init__(
            f"the {source} transport received a request for {addressed}; "
            "each source's client needs its own mock_transport"
        )


def _digest(*parts: object) -> str:
    """A SHA-256 hex digest of ``parts``: stable across processes, never ``hash()``."""
    text = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unknown_surface_body(source: Source, url: httpx.URL, message: str, digest: str) -> JSONValue:
    """The platform's HTTP 400 body for a request that matches no surface.

    Google: the ``google.rpc.Status`` n8n relays, with a ``GoogleAdsFailure`` detail whose
    ``requestError`` marks the request itself as invalid. LinkedIn REST:
    ``{status, serviceErrorCode, code, message}``; the OAuth host:
    ``{error, error_description}``. HubSpot: ``{status: "error", message, correlationId,
    category}``.

    Args:
        source: The source the transport serves.
        url: The request URL (only its host and path are read).
        message: The error message; it names the method and path, never the query string.
        digest: A digest of the request, for the deterministic request or correlation id.

    Returns:
        The JSON body.
    """
    if source == "google":
        return {
            "error": {
                "code": _BAD_REQUEST,
                "message": message,
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": _GOOGLE_FAILURE_TYPE,
                        "errors": [{"errorCode": {"requestError": "UNKNOWN"}, "message": message}],
                        "requestId": digest[:22],
                    }
                ],
            }
        }
    if source == "linkedin":
        if url.host == _LINKEDIN_OAUTH_HOST and url.path.startswith(_LINKEDIN_OAUTH_PREFIX):
            return {"error": "invalid_request", "error_description": message}
        return {
            "status": _BAD_REQUEST,
            "serviceErrorCode": 100,
            "code": "ILLEGAL_ARGUMENT",
            "message": message,
        }
    correlation = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    return {
        "status": "error",
        "message": message,
        "correlationId": correlation,
        "category": "VALIDATION_ERROR",
    }


def _foreign_surface(source: Source, shape: RequestShape) -> str | None:
    """The ``{source}.{surface}`` of another source that ``shape`` addresses, or ``None``.

    Google is tried last: its webhook URL is arbitrary, so it identifies any POST carrying a
    GAQL ``query``, while LinkedIn and HubSpot paths are specific.
    """
    others = sorted((other for other in SOURCES if other != source), key=lambda s: s == "google")
    for other in others:
        try:
            return f"{other}.{identify_surface(other, shape)}"
        except UnknownSurface:
            continue
    return None


def _unknown_surface_response(
    source: Source,
    request: httpx.Request,
    world: SimulatedWorld,
    unknown: UnknownSurface,
    addressed: str | None,
) -> httpx.Response:
    """The HTTP 400 that answers, in non-strict mode, a request matching no surface.

    A 400 is a defect to every live client: never retried and never mocked over, which is how
    the same request fails against the live platform.
    """
    message = f"Simulated transport: {unknown}"
    if addressed is not None:
        message += f" (the request addresses {addressed}; each source has its own transport)"
    digest = _digest(world.seed, source, request.method, request.url.path, message)
    body = _unknown_surface_body(source, request.url, message, digest)
    return httpx.Response(
        _BAD_REQUEST,
        headers=[("content-type", _JSON_CONTENT_TYPE)],
        content=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        request=request,
    )


def _to_response(
    source: Source, rendered: RenderedResponse, request: httpx.Request
) -> httpx.Response:
    """Serialise a rendered answer as the ``httpx.Response`` a live server would send.

    The body is compact UTF-8 JSON with ``Content-Type: application/json``; a body of ``None``
    is sent as empty content with no JSON content type. ``rendered.headers`` (``Retry-After``)
    follow the content type.

    Raises:
        ContractViolation: When the rendered body is not serialisable, strict JSON (a
            ``Decimal`` left unconverted, a NaN or infinity): a simulator bug, never a
            platform answer.
    """
    extra = list(rendered.headers)
    if rendered.body is None:
        return httpx.Response(rendered.status, headers=extra, content=b"", request=request)
    try:
        text = json.dumps(rendered.body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractViolation(
            f"{source}.{rendered.surface}",
            (f"$: the rendered body is not strict JSON ({type(exc).__name__}: {exc})",),
        ) from exc
    has_content_type = any(name.lower() == "content-type" for name, _value in extra)
    headers = extra if has_content_type else [("content-type", _JSON_CONTENT_TYPE), *extra]
    return httpx.Response(
        rendered.status, headers=headers, content=text.encode("utf-8"), request=request
    )


def mock_transport(
    source: Source,
    world: SimulatedWorld,
    *,
    strict: bool = False,
    journal: list[JournalEntry] | None = None,
    settings: Settings | None = None,
) -> httpx.MockTransport:
    """Build the transport that answers ``source``'s requests from ``world``.

    Per request: identify the surface; in strict mode validate the request against its
    contract first; ``payloads.render`` it (validation, faults, the live wire shape, response
    validation); then answer it as an ``httpx.Response``, or raise ``httpx.ReadTimeout`` for a
    timeout fault. See the module docstring for the guarantees.

    Args:
        source: The one source this transport serves: ``google``, ``linkedin`` or
            ``hubspot``. Each client gets its own transport.
        world: The seeded simulated world (``SimulatedWorld.from_settings`` in the product).
        strict: When true, a request that breaks its contract raises ``ContractViolation``, a
            request matching no surface raises ``UnknownSurface``, and one meant for another
            source raises :class:`SimulatedTransportError`. When false (the product's mode),
            each is answered with HTTP 400 in the platform's error shape. A rendered body
            that breaks its contract raises ``ContractViolation`` in both modes.
        journal: When given, receives one :class:`JournalEntry` per request, in order. Only
            appended to; the caller owns the list.
        settings: The settings the client was built with; their
            ``N8N_WEBHOOK_SECRET_HEADER`` is the one webhook header name requests are
            validated against. :func:`~ads_engine.settings.get_settings` when ``None``.

    Returns:
        An ``httpx.MockTransport`` for ``httpx.AsyncClient``.

    Raises:
        ValueError: When ``source`` is not a known source.
        TypeError: When ``world`` is not a ``SimulatedWorld``, ``journal`` is not a list or
            ``settings`` is not a ``Settings``.

    Example::

        async with build_client("linkedin", transport=mock_transport("linkedin", world)) as http:
            client = LinkedInClient(http, StaticTokenProvider(SIMULATED_TOKEN))
            forms = await client.list_lead_forms("990000001")
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; expected one of {', '.join(SOURCES)}")
    if not isinstance(world, SimulatedWorld):
        raise TypeError(f"world must be a SimulatedWorld, not {type(world).__name__}")
    if journal is not None and not isinstance(journal, list):
        raise TypeError(f"journal must be a list or None, not {type(journal).__name__}")
    if settings is not None and not isinstance(settings, Settings):
        raise TypeError(f"settings must be a Settings or None, not {type(settings).__name__}")

    def record(request: httpx.Request, surface: str, status: int | None, fault: str | None) -> None:
        """Journal and log one handled request: never headers, query strings or bodies."""
        if journal is not None:
            journal.append(
                JournalEntry(
                    source=source,
                    surface=surface,
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    fault=fault,
                )
            )
        _LOG.debug("simulation.request", source=source, surface=surface, status=status, fault=fault)

    async def handle(request: httpx.Request) -> httpx.Response:
        """Answer one request from the world (the ``MockTransport`` handler).

        Raises:
            httpx.ReadTimeout: For a timeout fault, as a live read would time out.
            ContractViolation: For a rendered body that breaks its contract, or, in strict
                mode, a request that breaks its contract.
            UnknownSurface: In strict mode, for a request matching no surface.
            SimulatedTransportError: In strict mode, for a request meant for another source.
        """
        # MockTransport has read the body before calling the handler, so content is complete.
        shape = RequestShape.from_httpx(request)
        surface = UNKNOWN_SURFACE
        status: int | None = None
        fault: str | None = None
        try:
            try:
                surface = identify_surface(source, shape)
            except UnknownSurface as unknown:
                addressed = _foreign_surface(source, shape)
                if strict:
                    if addressed is not None:
                        raise SimulatedTransportError(source, addressed) from unknown
                    raise
                response = _unknown_surface_response(source, request, world, unknown, addressed)
                status = response.status_code
                return response
            if strict:
                validate_request(load_contract(source, surface), shape, settings=settings)
            rendered = render(source, shape, world, settings=settings)
            fault = rendered.fault
            if rendered.timeout:
                raise httpx.ReadTimeout("simulated timeout", request=request)
            response = _to_response(source, rendered, request)
            status = response.status_code
            return response
        finally:
            record(request, surface, status, fault)

    return httpx.MockTransport(handle)
