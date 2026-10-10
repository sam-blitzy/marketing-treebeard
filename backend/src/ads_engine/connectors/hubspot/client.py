"""Read-only HubSpot CRM client (API ``2026-09``) and the process-wide 4 req/s throttle.

:class:`HubSpotClient` makes every HubSpot call the product needs (AAP 0.3.3, "HubSpot CRM"):

* startup property verification (:meth:`HubSpotClient.verify_bindings`), which binds MQL
  and Qualified Lead to the portal's own lifecycle stages or date properties and confirms
  the optional Google-to-CRM link, returning the
  :class:`~ads_engine.analysis.thresholds.CrmBindings` that ``sync/datasets.py`` stores per
  HubSpot dataset;
* lead-to-contact matching (:meth:`HubSpotClient.contacts_by_email`) and contact refresh
  (:meth:`HubSpotClient.contacts_by_id`);
* association reads with per-input ``{id, after}`` continuation
  (:meth:`HubSpotClient.associations`) and the target reads of the associated meetings
  and deals (:meth:`HubSpotClient.meetings`, :meth:`HubSpotClient.deals`);
* CRM Search pages (:meth:`HubSpotClient.search_page`) and the partitioned deal and
  Google-touch contact searches built on them (:meth:`HubSpotClient.search_partitioned`);
* the recovery-readiness probe (:meth:`HubSpotClient.probe`).

Read-only:
    Only batch reads, searches and property ``GET`` requests are sent. Nothing is ever
    created, updated or deleted in HubSpot, because the product only recommends.

Paths:
    Every call uses its surface's dated ``2026-09`` path (:data:`HUBSPOT_API_VERSION`, the
    version pinned in :mod:`ads_engine.settings`), relative to the
    ``https://api.hubapi.com`` base of :func:`ads_engine.connectors.http.build_client`. No
    numbered path is used, because HubSpot ends support for the numbered association and
    object APIs during the product's first year. Requests and responses follow the contract
    files in ``backend/tests/contracts/hubspot/``; the simulator validates requests against
    the same files, so live and simulated calls take one code path and this module never
    branches on the data mode, except for the cross-source isolation rule below.

Throttle:
    Every attempt, retries included, first awaits :meth:`RateLimiter.acquire` of the
    client's limiter. :data:`SHARED_LIMITER` is the default of every client in the process,
    so total HubSpot traffic stays at 4 requests per second, below CRM Search's 5 per
    second per account and the private-app burst of 100 to 190 per 10 s. Starts are at
    least 0.25 s apart and any five span at least 1.05 s, so no one-second window holds
    more than four requests, even when a request reaches the transport up to 50 ms
    (:data:`WINDOW_GUARD_SECONDS`) after its start is granted.

Errors:
    Every call goes through :func:`ads_engine.connectors.http.send_json`, which retries
    transient failures and classifies the rest. 401 and 403 (``MISSING_SCOPES`` included)
    raise ``AccessDenied``; 429 and 5xx raise ``TransientError`` after the retries; 400
    raises ``QueryDefect``. Batch methods record, per input or per id (``failed``, ``error``,
    ``partial``), a ``TransientError`` that outlasts its retries, a batch status other than
    ``COMPLETE`` and a per-input failure instead of raising them, so a sync keeps what it
    read and marks the rest unknown. The first such ``TransientError`` of a method call is
    also returned in its result's ``interrupted_by``, so the sync hands it to
    ``ads_engine.sync.mode`` and records the refresh as failed rather than successful;
    ``AccessDenied`` and ``QueryDefect`` always propagate, and ``ads_engine.sync.mode`` alone
    decides what follows. Invalid arguments raise ``ValueError`` before any call.

Personal data and isolation:
    Emails exist in memory only for the batch read that looks them up. Results are keyed
    by the SHA-256 of the normalised email, and no email, token, ``Authorization`` value,
    request body or HubSpot error message is ever logged or placed in an exception; log
    events carry object types, counts and statuses only. A live client never sends an
    address on the reserved simulated domain ``sim.invalid``: simulated leads match only
    their simulated companion portal, never a live one.

Contact properties:
    The contact batch reads request and parse the properties the verified bindings resolve
    (:func:`contact_property_map`). :meth:`HubSpotClient.verify_bindings` binds that map to
    the client; :meth:`HubSpotClient.bind_contact_properties` binds it from bindings already
    stored for a dataset, and the ``contact_properties`` constructor argument binds a map
    built elsewhere. A map passed to a call overrides the bound one. A contact read with no
    map raises ``QueryDefect`` (code ``CONTACT_PROPERTIES_UNBOUND``) before any call: an
    empty map would read every bound MQL and QL date as absent, which counts the stage as
    skipped instead of unavailable.

Example:
    ``sync/jobs.py`` refreshes matched contacts and reads their meetings::

        async with build_client("hubspot") as http:
            client = HubSpotClient.from_settings(settings, http, live=True)
            client.bind_contact_properties(bindings, settings)
            batch = await client.contacts_by_id(contact_ids)
            fetch = await client.associations("contacts", "meetings", batch.records)
            meetings = await client.meetings(fetch.all_targets)
"""

import asyncio
import builtins
import json
import math
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from ads_engine.analysis.thresholds import (
    CrmBindings,
    GoogleCrmBinding,
    HubSpotMapping,
    ResolvedBinding,
    StageBinding,
)
from ads_engine.connectors.errors import QueryDefect, TransientError
from ads_engine.connectors.http import DEFAULT_RETRY, JsonResponse, RetryPolicy, send_json
from ads_engine.connectors.hubspot import search
from ads_engine.connectors.hubspot.parser import (
    BATCH_COMPLETE,
    DEAL_PROPERTIES,
    MEETING_PROPERTIES,
    AssociationBatch,
    ContactPropertyMap,
    ContactRow,
    DealRow,
    HubSpotRecord,
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
    parse_meeting,
    parse_property,
    parse_search_page,
)
from ads_engine.connectors.hubspot.search import SearchFilter, SearchOutcome
from ads_engine.db.models.crm import (
    ASSOCIATION_FETCH_STATUSES,
    CONTACT_READ_STATUSES,
    TARGET_READ_STATUSES,
)
from ads_engine.logging import get_logger
from ads_engine.settings import HUBSPOT_API_VERSION, Settings, Source

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

__all__ = [
    "HUBSPOT_API_VERSION",
    "MAX_ASSOCIATION_INPUTS",
    "MAX_BATCH_READ_INPUTS",
    "MAX_SEARCH_BODY_CHARS",
    "MAX_SEARCH_LIMIT",
    "PROBE_EMAIL",
    "SEARCH_RESULT_CEILING",
    "SHARED_LIMITER",
    "STAGE_DATE_PROPERTY_PREFIX",
    "WINDOW_GUARD_SECONDS",
    "AssociationFetch",
    "AssociationStatus",
    "ContactBatch",
    "ContactReadStatus",
    "EmailLookup",
    "HubSpotClient",
    "InputAssociations",
    "RateLimiter",
    "TargetRead",
    "TargetReadStatus",
    "contact_property_map",
]

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants (AAP 0.2.4 surfaces table; backend/tests/contracts/hubspot/*.json "limits")
# ---------------------------------------------------------------------------

# HUBSPOT_API_VERSION, the dated API version of every surface this client calls, is the
# settings pin imported from ads_engine.settings and re-exported through __all__.

MAX_BATCH_READ_INPUTS: Final = 100
"""Inputs per objects batch read; HubSpot rejects a larger batch with HTTP 400."""

MAX_ASSOCIATION_INPUTS: Final = 1_000
"""Inputs per associations batch read, the limit of HubSpot's associations guide."""

MAX_SEARCH_LIMIT: Final = 200
"""Largest ``limit`` of one CRM Search page."""

MAX_SEARCH_BODY_CHARS: Final = 3_000
"""CRM Search request bodies must serialise to fewer characters than this."""

SEARCH_RESULT_CEILING: Final = 10_000
"""Results one CRM Search query can page through; a cursor at or past it returns 400."""

STAGE_DATE_PROPERTY_PREFIX: Final = "hs_v2_date_entered_"
"""Prefix of a lifecycle stage's "date entered" contact property; the stage id follows."""

PROBE_EMAIL: Final = "ads-engine-probe@ads-engine.invalid"
"""The address the recovery probe looks up. ``.invalid`` is a reserved top-level domain, so
it never names a real contact, and it is not the simulated domain, so a live client sends
it."""

# Must equal ads_engine.simulation.SIM_EMAIL_DOMAIN. It is repeated rather than imported
# because connectors never import the simulation package.
_SIM_EMAIL_DOMAIN: Final = "sim.invalid"

_SOURCE: Final[Source] = "hubspot"
_SURFACE_OBJECTS: Final = "objects_batch_read"
_SURFACE_SEARCH: Final = "crm_search"
_SURFACE_ASSOCIATIONS: Final = "associations_batch_read"
_SURFACE_PROPERTIES: Final = "properties"
_BATCH_STATUSES: Final = (200, 207)
_PROPERTY_STATUSES: Final = (200, 404)
# The per-input reason of an association read whose next page a continuation bound left
# unread: the input is partial (or error with no target read), never ok.
_REASON_CONTINUATION_LIMIT: Final = "continuation limit reached; later pages were not read"
_OBJECT_NOT_FOUND: Final = "OBJECT_NOT_FOUND"
_LIFECYCLE_STAGE: Final = "lifecyclestage"
_CONTACTS: Final = "contacts"
_GOOGLE_CAMPAIGN_ENV: Final = "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY"
_GOOGLE_TOUCH_ENV: Final = "HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY"

# A CRM object type as it appears in a request path: `contacts`, `deals`, `meetings`, or a
# custom object's `p_{name}` or `2-{id}` form. The same rule as connectors/hubspot/search.py.
_OBJECT_TYPE_RE: Final = re.compile(r"[a-z0-9][a-z0-9_-]*")

ContactReadStatus = Literal["ok", "error"]
"""Outcome of refreshing one contact by id; equals ``crm.CONTACT_READ_STATUSES``."""

AssociationStatus = Literal["ok", "ok_empty", "partial", "error"]
"""Outcome of one input's association read; equals ``crm.ASSOCIATION_FETCH_STATUSES``."""

TargetReadStatus = Literal["ok", "partial", "error"]
"""Outcome of reading associated targets by id; equals ``crm.TARGET_READ_STATUSES``."""


# ---------------------------------------------------------------------------
# Paths and argument helpers
# ---------------------------------------------------------------------------


def _require_object_type(object_type: object, what: str = "object_type") -> str:
    """Return ``object_type`` when it is a single CRM object type path segment.

    Raises:
        ValueError: When it is not, for example ``"contacts/../owners"``.
    """
    if not isinstance(object_type, str) or not _OBJECT_TYPE_RE.fullmatch(object_type):
        raise ValueError(f"{what} must be a CRM object type such as 'contacts' or 'deals'")
    return object_type


def _objects_batch_read(object_type: str) -> str:
    """Return ``/crm/objects/2026-09/{objectType}/batch/read``."""
    return f"/crm/objects/{HUBSPOT_API_VERSION}/{_require_object_type(object_type)}/batch/read"


def _objects_search(object_type: str) -> str:
    """Return ``/crm/objects/2026-09/{objectType}/search``."""
    return f"/crm/objects/{HUBSPOT_API_VERSION}/{_require_object_type(object_type)}/search"


def _associations_batch_read(from_type: str, to_type: str) -> str:
    """Return ``/crm/associations/2026-09/{fromObjectType}/{toObjectType}/batch/read``."""
    source = _require_object_type(from_type, "from_type")
    target = _require_object_type(to_type, "to_type")
    return f"/crm/associations/{HUBSPOT_API_VERSION}/{source}/{target}/batch/read"


def _property(object_type: str, name: str) -> str:
    """Return ``/crm/properties/2026-09/{objectType}/{propertyName}``.

    The property name is percent-encoded as one path segment, so a misconfigured name (a
    space or a slash) is sent as a name HubSpot does not know (404, unavailable) rather than
    as a different path.
    """
    segment = quote(name, safe="")
    return f"/crm/properties/{HUBSPOT_API_VERSION}/{_require_object_type(object_type)}/{segment}"


def _normalise_ids(ids: Iterable[str], what: str) -> tuple[str, ...]:
    """Return the distinct, stripped ids of ``ids`` in their first order.

    Raises:
        ValueError: When ``ids`` is a single string, or an id is not a non-blank string or
            an integer.
    """
    given: object = ids
    if isinstance(given, str):
        raise ValueError(f"{what} must be an iterable of ids, not one string")
    distinct: dict[str, None] = {}
    for value in ids:
        raw: object = value
        if isinstance(raw, bool) or not isinstance(raw, str | int):
            raise ValueError(f"every entry of {what} must be a string id")
        text = raw.strip() if isinstance(raw, str) else str(raw)
        if not text:
            raise ValueError(f"{what} must not contain a blank id")
        distinct.setdefault(text, None)
    return tuple(distinct)


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _is_simulated_email(normalised: str) -> bool:
    """Whether a normalised address is on the reserved simulated domain.

    Mirrors ``ads_engine.simulation.is_simulated_email``: the domain after the last ``@``
    equals ``sim.invalid`` exactly.
    """
    _local, separator, domain = normalised.rpartition("@")
    return bool(separator) and domain == _SIM_EMAIL_DOMAIN


# ---------------------------------------------------------------------------
# Throttle
# ---------------------------------------------------------------------------

WINDOW_GUARD_SECONDS: Final = 0.05
"""Seconds :class:`RateLimiter` adds to the window its last starts must span.

The limiter times each start by the clock reading at which it grants it, but the request
reaches the transport later: a garbage-collection pause or OS preemption between the grant
and the send can take tens of milliseconds. If five grants spanned exactly one second, such
a delay on the first would put all five requests inside one second at the transport, where
HubSpot's CRM Search limit is 5 per second. With this guard any five starts are at least
1.05 s apart at 4 per second, so a request that reaches the transport up to 50 ms after its
grant still leaves at most four in every one-second window.
"""


class RateLimiter:
    """Spaces request starts one interval apart and guards every one-second window.

    Two rules hold across every client that shares the limiter, measured by the clock
    reading at which :meth:`acquire` grants each start, with ``N`` the rate rounded down
    (at least 1):

    * consecutive starts are at least one interval (``1 / rate_per_second``) apart, 0.25 s
      at the default 4 per second;
    * once ``N`` starts are recorded, the next waits until the oldest of the last ``N`` is
      at least the window, ``N / rate_per_second + window_guard`` seconds, old.

    At the defaults (4 per second, :data:`WINDOW_GUARD_SECONDS`) any five starts are
    therefore at least 1.05 s apart. A back-to-back stream runs three 0.25 s gaps and then
    one 0.30 s gap, about 3.81 starts per second, and a request that reaches the transport
    up to 50 ms after its grant, delayed by a garbage-collection pause or OS preemption,
    still leaves at most four requests in every one-second window.

    Each :meth:`acquire` reserves the next slot when it is called, reading and advancing the
    reservation with no ``await`` in between. A waiter re-reads the clock after every wake
    and sleeps again until its slot, one interval after the previous start and the window
    over the last ``N`` starts have all passed, so a wake that comes early or late never
    shortens a gap or a window. A start granted after its slot, because a wake was late,
    the window held it or the event loop was blocked, shifts every outstanding reservation
    by that lateness: waiters whose slots came due during a stall are released one interval
    apart under the same window, never together. A call made once every reserved slot has
    passed and the latest start is at least the window old (1.05 s at the defaults) starts
    without sleeping. A waiter cancelled before its start leaves its slot unused, so the
    waiters behind it keep their own slots and the stream has one longer gap.

    Callers start in the order they reserved, except right after a stall: a call made when
    every outstanding reservation is already overdue can start before the overdue waiters,
    which then start in no guaranteed order among themselves. The order also relies on a
    ``window_guard`` shorter than one interval, as at the defaults; a longer guard can hold
    several waiters until the same instant and release them in any order. Spacing and the
    window hold regardless.

    No ``asyncio`` lock, event or future is held, so one instance is safe to share across
    event loops (tests run each in its own).

    Example:
        Tests drive a fake clock instead of sleeping::

            now = [0.0]


            async def advance(seconds: float) -> None:
                now[0] += seconds


            limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=advance)

    Args:
        rate_per_second: Request starts allowed per second; consecutive starts are at least
            ``1 / rate_per_second`` seconds apart, so 4.0 (0.25 s) keeps HubSpot traffic
            below CRM Search's 5 per second.
        clock: Monotonic clock in seconds, read when a call reserves its slot and after
            every wake.
        sleep: Awaitable sleep used to wait for a slot; it may return early or late.
        window_guard: Seconds added to the ``N / rate_per_second`` window the last ``N``
            starts must span before the next (:data:`WINDOW_GUARD_SECONDS`): the longest
            delay between a grant and its request reaching the transport that still leaves
            at most ``N`` requests in every such window there. 0 keeps the bare window.

    Raises:
        ValueError: When ``rate_per_second`` is not a finite number above 0, or
            ``window_guard`` is not a finite number of at least 0.
    """

    __slots__ = (
        "_clock",
        "_interval",
        "_last_start",
        "_next_slot",
        "_offset",
        "_rate",
        "_recent",
        "_sleep",
        "_window",
    )

    def __init__(
        self,
        rate_per_second: float = 4.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        window_guard: float = WINDOW_GUARD_SECONDS,
    ) -> None:
        rate: object = rate_per_second
        if isinstance(rate, bool) or not isinstance(rate, int | float):
            raise ValueError("rate_per_second must be a number")
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("rate_per_second must be a finite number above 0")
        guard: object = window_guard
        if isinstance(guard, bool) or not isinstance(guard, int | float):
            raise ValueError("window_guard must be a number")
        if not math.isfinite(guard) or guard < 0:
            raise ValueError("window_guard must be a finite number of at least 0")
        self._rate = float(rate)
        self._interval = 1.0 / self._rate
        window_starts = max(1, math.floor(self._rate))
        self._window = window_starts / self._rate + float(guard)
        self._clock = clock
        self._sleep = sleep
        # Reservations are kept in virtual time: the clock minus ``_offset``, the lateness
        # accumulated by starts granted after their slot. ``_last_start`` is the clock
        # reading of the latest granted start, and ``_recent`` holds the readings of up to
        # the last ``window_starts`` granted starts, oldest first.
        self._next_slot = -math.inf
        self._offset = 0.0
        self._last_start = -math.inf
        self._recent: deque[float] = deque(maxlen=window_starts)

    @property
    def rate_per_second(self) -> float:
        """The configured requests per second."""
        return self._rate

    async def acquire(self) -> None:
        """Reserve the next request slot and wait until this request may start.

        The slot is reserved before the first ``await``. The call then sleeps until its slot,
        shifted by the lateness of earlier late starts, one interval after the previous start
        and, once ``N`` starts are recorded, ``N / rate_per_second + window_guard`` after the
        oldest of the last ``N`` have all passed, re-reading the clock after every wake.
        Granting the start and recording its clock reading happen with no ``await`` in
        between, and the call returns at once. The first call, and a call made once every
        reserved slot has passed and the latest start is at least that window old (1.05 s at
        the defaults), return without sleeping. Callers start in reservation order except in
        the cases the class docstring names.
        """
        now = self._clock()
        slot = max(now - self._offset, self._next_slot)
        self._next_slot = slot + self._interval
        recent = self._recent
        while True:
            due = max(slot + self._offset, self._last_start + self._interval)
            if len(recent) == recent.maxlen:
                due = max(due, recent[0] + self._window)
            if now >= due:
                self._offset = max(self._offset, now - slot)
                self._last_start = now
                recent.append(now)
                return
            await self._sleep(due - now)
            now = self._clock()


SHARED_LIMITER: Final = RateLimiter(4.0)
"""The process-wide default limiter of every :class:`HubSpotClient`: 4 requests per second
in total, whatever the number of clients."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


def _require_status(value: str, allowed: tuple[str, ...], what: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a status the database's check accepts."""
    if value not in allowed:
        raise ValueError(f"{what} must be one of {', '.join(allowed)}, found {value!r}")


@dataclass(frozen=True, slots=True, repr=False)
class EmailLookup:
    """The outcome of looking contacts up by email, keyed by the email's SHA-256.

    Every key is :func:`~ads_engine.connectors.hubspot.parser.email_sha256` of a normalised
    input address, the digest ``lead.email_sha256`` holds, so the result carries no
    plaintext. Each input lands in exactly one field. ``repr`` shows counts only.

    Attributes:
        found: The contact each matched input resolved to (``lead_match.status`` ``matched``).
            A contact matches an input through its primary or any additional email.
        not_found: Inputs HubSpot reported as ``OBJECT_NOT_FOUND`` (``no_match``, retried).
        failed: Inputs whose read failed: another error category, a batch status other than
            ``COMPLETE``, an input neither returned nor listed, or a call that still failed
            after its retries (``error``, retried).
        skipped: Simulated ``@sim.invalid`` addresses a live client never sends.
        interrupted_by: The first ``TransientError`` a call still raised after its retries,
            for ``ads_engine.sync.mode`` to decide on; ``None`` when every call was answered.
            Its inputs are in ``failed`` either way.
    """

    found: Mapping[str, ContactRow]
    not_found: frozenset[str]
    failed: frozenset[str]
    skipped: frozenset[str]
    interrupted_by: TransientError | None = None

    def __repr__(self) -> str:
        """Return the field counts, never a digest."""
        return (
            f"EmailLookup(found={len(self.found)}, not_found={len(self.not_found)}, "
            f"failed={len(self.failed)}, skipped={len(self.skipped)})"
        )


@dataclass(frozen=True, slots=True)
class ContactBatch:
    """The outcome of refreshing contacts by HubSpot id.

    Attributes:
        records: The parsed contacts that were returned, keyed by HubSpot id.
        read_status: ``ok`` for every requested id that was returned; ``error`` for one that
            was absent, listed in ``errors[]``, or in a call that failed. It becomes
            ``lead_match.contact_read_status``; an ``error`` keeps the previous stage values
            and marks them unknown for the window.
        interrupted_by: The first ``TransientError`` a call still raised after its retries,
            for ``ads_engine.sync.mode`` to decide on; ``None`` when every call was answered.
            The ids of a failed call read ``error`` either way.

    Raises:
        ValueError: On construction, when a status is not one the database accepts.
    """

    records: Mapping[str, ContactRow]
    read_status: Mapping[str, ContactReadStatus]
    interrupted_by: TransientError | None = None

    def __post_init__(self) -> None:
        """Check every status against ``crm.CONTACT_READ_STATUSES``."""
        for status in self.read_status.values():
            _require_status(status, CONTACT_READ_STATUSES, "contact read status")


@dataclass(frozen=True, slots=True)
class InputAssociations:
    """The associations read for one input record, and how far the read got.

    Attributes:
        targets: The associated target ids, de-duplicated in the order HubSpot returned them.
        status: ``ok`` (targets read and every continuation succeeded), ``ok_empty`` (no
            associations: a genuine zero), ``partial`` (a continuation failed after some
            targets were read) or ``error`` (nothing reliable was read).
        error: Why the read is ``partial`` or ``error``, safe to store in
            ``crm_association_fetch.error``; ``None`` otherwise.

    Raises:
        ValueError: On construction, when the status is not one the database accepts, or
            an ``ok_empty`` read carries targets.
    """

    targets: tuple[str, ...]
    status: AssociationStatus
    error: str | None

    def __post_init__(self) -> None:
        """Check the status and the shape of a genuine zero."""
        _require_status(self.status, ASSOCIATION_FETCH_STATUSES, "association status")
        if self.status == "ok_empty" and self.targets:
            raise ValueError("an ok_empty association read cannot carry targets")

    @builtins.property
    def is_known(self) -> bool:
        """Whether the read is known evidence (``ok`` or ``ok_empty``) before target reads."""
        return self.status in {"ok", "ok_empty"}


@dataclass(frozen=True, slots=True)
class AssociationFetch:
    """Every input's associations from ``from_type`` to ``to_type``.

    ``sync/jobs.py`` writes ``crm_association`` rows and one ``crm_association_fetch`` row
    per input from it.

    Attributes:
        from_type: The source object type, for example ``contacts``.
        to_type: The target object type, for example ``meetings``.
        per_input: Each requested input id's read, in request order.
        interrupted_by: The first ``TransientError`` an initial or continuation call still
            raised after its retries, for ``ads_engine.sync.mode`` to decide on; ``None``
            when every call was answered. The affected inputs are ``error`` or ``partial``
            either way; a 207 ``errors[]`` entry or a batch status other than ``COMPLETE`` is
            a per-input outcome only and never sets it.
    """

    from_type: str
    to_type: str
    per_input: Mapping[str, InputAssociations]
    interrupted_by: TransientError | None = None

    @builtins.property
    def all_targets(self) -> tuple[str, ...]:
        """Every distinct target id across inputs, in first-seen order, for the target read."""
        distinct: dict[str, None] = {}
        for read in self.per_input.values():
            for target in read.targets:
                distinct.setdefault(target, None)
        return tuple(distinct)

    def status_counts(self) -> Mapping[AssociationStatus, int]:
        """Return how many inputs ended in each status; statuses with none are omitted."""
        counts: dict[AssociationStatus, int] = {}
        for read in self.per_input.values():
            counts[read.status] = counts.get(read.status, 0) + 1
        return MappingProxyType(counts)


@dataclass(frozen=True, slots=True)
class TargetRead[RowT]:
    """The outcome of batch-reading associated meetings or deals by id.

    An associated but unread target is never treated as absent: it is listed in
    :attr:`missing_ids`, and the fetch's evidence is unknown.

    Attributes:
        records: The parsed rows that were returned, keyed by HubSpot id.
        missing_ids: Every requested id that was not read, whether HubSpot omitted it or its
            call failed.
        status: ``ok`` when every requested id was read (also for an empty request),
            ``partial`` when some were not, ``error`` when none were.
        interrupted_by: The first ``TransientError`` a call still raised after its retries,
            for ``ads_engine.sync.mode`` to decide on; ``None`` when every call was answered.
            The ids of a failed call are in ``missing_ids`` either way.

    Raises:
        ValueError: On construction, when the status is not one the database accepts.
    """

    records: Mapping[str, RowT]
    missing_ids: frozenset[str]
    status: TargetReadStatus
    interrupted_by: TransientError | None = None

    def __post_init__(self) -> None:
        """Check the status against ``crm.TARGET_READ_STATUSES``."""
        _require_status(self.status, TARGET_READ_STATUSES, "target read status")


class _Identified(Protocol):
    """A parsed CRM row: every row type carries its HubSpot id."""

    @builtins.property
    def hs_id(self) -> str:
        """The HubSpot record id."""
        ...


# ---------------------------------------------------------------------------
# Contact property map
# ---------------------------------------------------------------------------


def contact_property_map(bindings: CrmBindings, settings: Settings) -> ContactPropertyMap:
    """Return the contact properties a batch read requests and parses, from verified bindings.

    The MQL and QL date properties are included only when their binding is available, so an
    unavailable count reads as unavailable, never 0. The Google campaign and touch-date
    properties are included only when the Google link is available and the configured
    ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` still
    name the properties that were verified; a configuration changed since verification
    leaves the link out until the next :meth:`HubSpotClient.verify_bindings`.

    Args:
        bindings: The dataset's verified CRM bindings.
        settings: The current settings.

    Returns:
        The property map :meth:`HubSpotClient.contacts_by_email` and
        :meth:`HubSpotClient.contacts_by_id` request and parse: the one
        :meth:`HubSpotClient.verify_bindings` and :meth:`HubSpotClient.bind_contact_properties`
        bind to the client, or an explicit ``props`` override of a call.
    """
    google = bindings.google
    google_current = (
        google.available
        and google.campaign_property == settings.hubspot_google_campaign_property
        and google.touch_date_property == settings.hubspot_google_touch_date_property
    )
    return ContactPropertyMap(
        mql_property=bindings.mql.property if bindings.mql.available else None,
        ql_property=bindings.ql.property if bindings.ql.available else None,
        google_campaign_property=google.campaign_property if google_current else None,
        google_touch_property=google.touch_date_property if google_current else None,
    )


# ---------------------------------------------------------------------------
# Binding resolution helpers
# ---------------------------------------------------------------------------


def _match_option(options: Sequence[PropertyOption], label: str) -> PropertyOption | None:
    """Find the ``lifecyclestage`` option a configured label names.

    Labels match when equal after trimming and case-folding. An exact match is preferred,
    then a visible option over a hidden one, then the first in response order, so the
    result is deterministic.
    """
    key = label.strip().casefold()
    candidates = [option for option in options if option.label.strip().casefold() == key]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda option: (option.label != label, option.hidden),
    )


def _quoted(labels: Sequence[str]) -> str:
    return ", ".join(f"'{label}'" for label in labels)


def _labels_not_found(labels: Sequence[str]) -> str:
    """Reason for a stage-label binding none of whose labels is a lifecycle stage."""
    noun = "stage" if len(labels) == 1 else "stages"
    return f"{noun} {_quoted(labels)} not found in {_LIFECYCLE_STAGE} options"


def _target_read[RowT](
    requested: Sequence[str],
    records: Mapping[str, RowT],
    interrupted_by: TransientError | None = None,
) -> TargetRead[RowT]:
    """Summarise a by-id target read: ``ok`` all read, ``partial`` some, ``error`` none.

    An empty request is ``ok``: there was nothing to read. ``interrupted_by`` is the first
    transient error a call of the read still raised after its retries.
    """
    missing = frozenset(hs_id for hs_id in requested if hs_id not in records)
    status: TargetReadStatus
    if not missing:
        status = "ok"
    elif len(missing) == len(requested):
        status = "error"
    else:
        status = "partial"
    return TargetRead(
        records=MappingProxyType(dict(records)),
        missing_ids=missing,
        status=status,
        interrupted_by=interrupted_by,
    )


def _require_complete(status: str, *, endpoint: str) -> None:
    """Raise the ``TransientError`` of a probe call whose batch status is not ``COMPLETE``.

    A batch reports ``CANCELED``, ``PENDING`` or ``PROCESSING`` when HubSpot did not finish
    it; the probe treats that as a failed check, retried at the next probe.
    """
    if status != BATCH_COMPLETE:
        raise TransientError(
            f"batch status {status}",
            source=_SOURCE,
            endpoint=endpoint,
            code="BATCH_INCOMPLETE",
        )


_DECIMAL_CURSOR_RE: Final = re.compile(r"[0-9]+")


def _search_defect(message: str, *, endpoint: str, code: str) -> QueryDefect:
    """Build the defect of a CRM Search body HubSpot would reject with HTTP 400."""
    return QueryDefect(message, source=_SOURCE, endpoint=endpoint, code=code)


def _check_search_body(body: Mapping[str, object], *, endpoint: str) -> dict[str, object]:
    """Check a CRM Search body against the limits HubSpot enforces, before it is sent.

    Args:
        body: The ``PublicObjectSearchRequest``.
        endpoint: The call's endpoint label, for the defect.

    Returns:
        A plain ``dict`` copy of the top level, which serialises as JSON as is.

    Raises:
        QueryDefect: When the body does not serialise, has 3,000 characters or more
            (compact JSON), lacks an integer ``limit`` from 1 to 200, or carries a blank
            ``after`` or one at or past the 10,000-result ceiling.
    """
    payload = dict(body)
    try:
        serialized = json.dumps(payload, separators=(",", ":"))
    except TypeError, ValueError:
        raise _search_defect(
            "CRM Search body does not serialise as JSON", endpoint=endpoint, code="CLIENT"
        ) from None
    if len(serialized) >= MAX_SEARCH_BODY_CHARS:
        raise _search_defect(
            f"CRM Search body has {len(serialized)} characters; HubSpot accepts fewer than "
            f"{MAX_SEARCH_BODY_CHARS}",
            endpoint=endpoint,
            code="BODY_TOO_LARGE",
        )
    limit = payload.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise _search_defect(
            f"CRM Search limit must be an integer from 1 to {MAX_SEARCH_LIMIT}",
            endpoint=endpoint,
            code="INVALID_LIMIT",
        )
    after = payload.get("after")
    if after is not None:
        if not isinstance(after, str) or not after.strip():
            raise _search_defect(
                "CRM Search after must be a non-blank paging cursor",
                endpoint=endpoint,
                code="INVALID_CURSOR",
            )
        if _DECIMAL_CURSOR_RE.fullmatch(after) and int(after) >= SEARCH_RESULT_CEILING:
            raise _search_defect(
                f"CRM Search cursor is at or past the {SEARCH_RESULT_CEILING}-result ceiling",
                endpoint=endpoint,
                code="RESULT_CEILING",
            )
    return payload


@dataclass(slots=True)
class _InputState:
    """Mutable progress of one input during :meth:`HubSpotClient.associations`."""

    targets: dict[str, None] = field(default_factory=dict)
    cursors: set[str] = field(default_factory=set)
    after: str | None = None
    status: AssociationStatus | None = None
    error: str | None = None

    def add(self, targets: Iterable[str]) -> None:
        """Merge one page's targets, keeping first-seen order."""
        for target in targets:
            self.targets.setdefault(target, None)

    def finish(self) -> None:
        """Mark the input fully read: ``ok`` with targets, else the genuine zero."""
        self.after = None
        self.status = "ok" if self.targets else "ok_empty"

    def fail(self, reason: str) -> None:
        """Mark the read failed: ``partial`` once targets were read, else ``error``."""
        self.after = None
        self.status = "partial" if self.targets else "error"
        self.error = reason

    def apply(self, batch: AssociationBatch, input_id: str) -> None:
        """Apply one decoded call's outcome for ``input_id``.

        A result with ``paging.next.after`` leaves the input pending with that cursor; a
        cursor this input already sent is a page loop, which fails the input rather than
        repeating forever.
        """
        result = batch.results.get(input_id)
        if result is not None:
            self.add(result.targets)
            if result.after is None:
                self.finish()
            elif result.after in self.cursors:
                self.fail("paging cursor did not advance")
            else:
                self.cursors.add(result.after)
                self.after = result.after
            return
        if input_id in batch.empty_ids:
            self.finish()
            return
        label = batch.error_ids.get(input_id)
        self.fail(label if label is not None else "input missing from response")

    def outcome(self) -> InputAssociations:
        """Freeze the state; an input no call settled is ``error``."""
        if self.status is None:
            return InputAssociations(targets=(), status="error", error="input was not read")
        return InputAssociations(
            targets=tuple(self.targets),
            status=self.status,
            error=self.error,
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HubSpotClient:
    """Read-only client of one HubSpot portal, live or simulated.

    The caller owns ``http`` (from ``build_client("hubspot", transport)``) and its
    lifecycle. A simulated client gets ``SecretStr("simulated")``, ``live=False`` and an
    ``http`` built on the simulation's mock transport; every method then runs unchanged.

    Args:
        http: The HubSpot ``httpx.AsyncClient``, whose base URL is ``https://api.hubapi.com``.
        token: The Service Key or private-app token, sent as ``Authorization: Bearer``.
        live: Whether the portal is a real one. A live client never sends a ``@sim.invalid``
            address (cross-source isolation).
        retry: The retry policy of every call.
        limiter: The throttle every attempt awaits; :data:`SHARED_LIMITER` when ``None``.
        logger: The structured logger; this module's logger when ``None``.
        contact_properties: The contact property map the contact batch reads use, built
            from verified bindings (:func:`contact_property_map`); ``None`` leaves it unbound
            until :meth:`verify_bindings` or :meth:`bind_contact_properties` binds one.

    Raises:
        ValueError: When ``token`` is blank.
    """

    __slots__ = (
        "_calls",
        "_contact_properties",
        "_http",
        "_limiter",
        "_live",
        "_log",
        "_retry",
        "_token",
    )

    def __init__(
        self,
        http: httpx.AsyncClient,
        token: SecretStr,
        *,
        live: bool,
        retry: RetryPolicy = DEFAULT_RETRY,
        limiter: RateLimiter | None = None,
        logger: BoundLogger | None = None,
        contact_properties: ContactPropertyMap | None = None,
    ) -> None:
        if not token.get_secret_value().strip():
            raise ValueError("the HubSpot token must not be blank")
        self._http = http
        self._token = token
        self._live = live
        self._retry = retry
        self._limiter = limiter if limiter is not None else SHARED_LIMITER
        self._log: BoundLogger = logger if logger is not None else log
        self._calls = 0
        self._contact_properties = contact_properties

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        live: bool = True,
        retry: RetryPolicy = DEFAULT_RETRY,
        limiter: RateLimiter | None = None,
        contact_properties: ContactPropertyMap | None = None,
    ) -> HubSpotClient:
        """Build the client of the configured portal from ``HUBSPOT_ACCESS_TOKEN``.

        Args:
            settings: The settings holding ``hubspot_access_token``.
            http: The HubSpot ``httpx.AsyncClient``.
            live: Whether the portal is a real one.
            retry: The retry policy of every call.
            limiter: The throttle; :data:`SHARED_LIMITER` when ``None``.
            contact_properties: The contact property map to bind; ``None`` leaves it
                unbound until :meth:`verify_bindings` or :meth:`bind_contact_properties`.

        Returns:
            The client.

        Raises:
            ValueError: When ``HUBSPOT_ACCESS_TOKEN`` is not set. Mode resolution has
                already sent such a source to simulated data, so this is a caller defect.
        """
        token = settings.hubspot_access_token
        if token is None or not token.get_secret_value().strip():
            raise ValueError("HUBSPOT_ACCESS_TOKEN is not configured")
        return cls(
            http,
            token,
            live=live,
            retry=retry,
            limiter=limiter,
            contact_properties=contact_properties,
        )

    @staticmethod
    def contact_property_map(bindings: CrmBindings, settings: Settings) -> ContactPropertyMap:
        """Return the contact property map of ``bindings``; see :func:`contact_property_map`.

        Args:
            bindings: The dataset's verified CRM bindings.
            settings: The current settings.

        Returns:
            The property map for contact batch reads.
        """
        return contact_property_map(bindings, settings)

    def bind_contact_properties(
        self, bindings: CrmBindings, settings: Settings
    ) -> ContactPropertyMap:
        """Bind the contact property map of already verified ``bindings`` to this client.

        For bindings a dataset already stores, so a client that did not run
        :meth:`verify_bindings` reads contacts with the same properties. An unavailable
        binding stays unbound in the map, so its metric reads unavailable, never 0.

        Args:
            bindings: The dataset's verified CRM bindings.
            settings: The current settings.

        Returns:
            The bound map (:func:`contact_property_map`), which replaces any bound before.
        """
        props = contact_property_map(bindings, settings)
        self._contact_properties = props
        return props

    def _contact_properties_for(self, props: ContactPropertyMap | None) -> ContactPropertyMap:
        """Return the map a contact read uses: ``props`` when given, else the bound one.

        Raises:
            QueryDefect: When neither is set (code ``CONTACT_PROPERTIES_UNBOUND``), before
                any call. No empty map stands in, because it would read every bound MQL and
                QL date as absent, a skipped stage instead of an unavailable metric.
        """
        if props is not None:
            return props
        if self._contact_properties is not None:
            return self._contact_properties
        raise QueryDefect(
            "contact properties are not bound: call verify_bindings or "
            "bind_contact_properties first, or pass props",
            source=_SOURCE,
            endpoint=f"{_SURFACE_OBJECTS} {_CONTACTS}",
            code="CONTACT_PROPERTIES_UNBOUND",
        )

    @builtins.property
    def contact_properties(self) -> ContactPropertyMap | None:
        """The bound contact property map; ``None`` until one is bound."""
        return self._contact_properties

    @builtins.property
    def live(self) -> bool:
        """Whether the portal is a real one; a live client never sends simulated emails."""
        return self._live

    @builtins.property
    def calls(self) -> int:
        """Logical calls made so far; a retry sequence counts once."""
        return self._calls

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _send(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        endpoint: str,
        json_body: Mapping[str, object] | None = None,
        accept_statuses: Collection[int] = (200,),
    ) -> JsonResponse:
        """Send one call through ``send_json`` with the Bearer token and the throttle.

        Args:
            method: ``GET`` (properties) or ``POST`` (batch reads and searches).
            path: The path relative to ``https://api.hubapi.com``.
            endpoint: The contract surface name and object types, such as
                ``associations_batch_read contacts/meetings``; it names the call in errors
                and logs, so a mode reason names the failing endpoint.
            json_body: The JSON request body of a ``POST``.
            accept_statuses: Statuses returned as data rather than raised.

        Returns:
            The accepted response.

        Raises:
            AccessDenied: On 401 or 403.
            TransientError: On 429, 5xx or a network failure after the retries.
            QueryDefect: On 400 or another unexpected status, or an undecodable body.
        """
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        def build() -> httpx.Request:
            """Build a fresh request for each attempt."""
            return self._http.build_request(method, path, json=json_body, headers=headers)

        self._calls += 1
        return await send_json(
            self._http,
            build,
            source=_SOURCE,
            endpoint=endpoint,
            policy=self._retry,
            before_attempt=self._limiter.acquire,
            accept_statuses=accept_statuses,
        )

    # ------------------------------------------------------------------
    # Properties and binding verification (AAP 0.3.3, 0.5.4.7)
    # ------------------------------------------------------------------

    async def property(self, object_type: str, name: str) -> PropertyDefinition | None:
        """Read one property definition, or learn that the portal has no such property.

        ``GET /crm/properties/2026-09/{objectType}/{propertyName}``.

        Args:
            object_type: The object type, for example ``contacts``.
            name: The internal property name, for example ``lifecyclestage``.

        Returns:
            The definition with its type and options; ``None`` on 404.

        Raises:
            ValueError: When ``object_type`` is not a path segment or ``name`` is blank.
            AccessDenied: When the token or its scopes are refused.
            TransientError: When the call still fails after its retries.
            QueryDefect: When the request or the response breaks the contract.
        """
        if not name.strip():
            raise ValueError("a property name must not be blank")
        response = await self._send(
            "GET",
            _property(object_type, name),
            endpoint=f"{_SURFACE_PROPERTIES} {object_type}/{name}",
            accept_statuses=_PROPERTY_STATUSES,
        )
        if response.status == 404:
            return None
        return parse_property(response.body)

    async def verify_bindings(self, mapping: HubSpotMapping, settings: Settings) -> CrmBindings:
        """Resolve the MQL, QL and Google bindings against the portal's property definitions.

        * ``stage_label`` mode: the configured labels are matched, in order, against the
          ``lifecyclestage`` options (trimmed, case-insensitive, an exact match preferred);
          the first label that matches wins, and its stage's
          ``hs_v2_date_entered_{stage id}`` property must exist. ``lifecyclestage`` is read
          at most once.
        * ``property`` mode: the named contact property must exist and be a ``date`` or
          ``datetime`` property.
        * The portal's default "Marketing Qualified Lead" stage is never assumed to mean
          "First meeting booked"; MQL uses it only when the configuration names it. QL is
          resolved on its own, never from MQL.
        * The Google link is available only when ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and
          ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY`` are both set, both exist on contacts, and
          the touch-date property is a ``datetime`` property. A ``date`` property is
          refused: its midnight would anchor the causal check before a same-day touch.

        Anything unresolved is unavailable with a reason naming the missing stage or
        property, never counted as 0, and each binding's availability is independent.

        On success the bindings' contact property map is bound to this client (as
        :meth:`bind_contact_properties` binds it), so :meth:`contacts_by_email` and
        :meth:`contacts_by_id` need no ``props``. A failed verification binds nothing and
        keeps any map bound before.

        Args:
            mapping: The configured bindings from ``config/hubspot_mapping.yaml``.
            settings: The settings naming the Google link properties.

        Returns:
            The verified bindings, with ``verified_at`` now in UTC.

        Raises:
            AccessDenied: When the token or its scopes are refused; the caller switches mode.
            TransientError: When a read still fails after its retries; the caller keeps the
                previous bindings.
            QueryDefect: When a response breaks the properties contract.
        """
        cache: dict[str, PropertyDefinition | None] = {}
        mql = await self._resolve_binding(mapping.mql, cache)
        ql = await self._resolve_binding(mapping.ql, cache)
        google = await self._resolve_google(settings, cache)
        bindings = CrmBindings(mql=mql, ql=ql, google=google, verified_at=datetime.now(UTC))
        self.bind_contact_properties(bindings, settings)
        self._log.info(
            "hubspot.verify",
            mql_mode=mapping.mql.mode,
            mql_available=mql.available,
            ql_mode=mapping.ql.mode,
            ql_available=ql.available,
            google_available=google.available,
            properties_read=len(cache),
        )
        return bindings

    async def _contact_property(
        self, name: str, cache: dict[str, PropertyDefinition | None]
    ) -> PropertyDefinition | None:
        """Read a contact property definition once per verification."""
        if name not in cache:
            cache[name] = await self.property(_CONTACTS, name)
        return cache[name]

    async def _resolve_binding(
        self, binding: StageBinding, cache: dict[str, PropertyDefinition | None]
    ) -> ResolvedBinding:
        """Resolve one MQL or QL binding in its configured mode."""
        if binding.mode == "property":
            return await self._resolve_property_binding(binding, cache)
        lifecycle = await self._contact_property(_LIFECYCLE_STAGE, cache)
        if lifecycle is None:
            return ResolvedBinding(available=False, reason=f"{_LIFECYCLE_STAGE} property not found")
        for label in binding.stage_labels:
            option = _match_option(lifecycle.options, label)
            if option is None:
                continue
            # The first label that matches wins; a later label never stands in for it.
            date_property = f"{STAGE_DATE_PROPERTY_PREFIX}{option.value}"
            if await self._contact_property(date_property, cache) is None:
                return ResolvedBinding(
                    available=False,
                    stage_id=option.value,
                    label=option.label,
                    reason=(
                        f"stage '{label}' resolved to id '{option.value}', but property "
                        f"'{date_property}' not found"
                    ),
                )
            return ResolvedBinding(
                available=True,
                property=date_property,
                stage_id=option.value,
                label=option.label,
            )
        return ResolvedBinding(available=False, reason=_labels_not_found(binding.stage_labels))

    async def _resolve_property_binding(
        self, binding: StageBinding, cache: dict[str, PropertyDefinition | None]
    ) -> ResolvedBinding:
        """Resolve a ``property``-mode binding; its labels are kept for display only."""
        name = binding.property
        label = binding.stage_labels[0] if binding.stage_labels else None
        if name is None:
            # StageBinding forbids this in property mode; kept so a hand-built binding is
            # reported as unavailable rather than crashing verification.
            return ResolvedBinding(
                available=False, label=label, reason="property mode names no property"
            )
        definition = await self._contact_property(name, cache)
        if definition is None:
            return ResolvedBinding(
                available=False, label=label, reason=f"property '{name}' not found on contacts"
            )
        if not definition.is_datetime:
            return ResolvedBinding(
                available=False,
                label=label,
                reason=(
                    f"property '{name}' has type '{definition.type}', not a date or datetime "
                    "property"
                ),
            )
        return ResolvedBinding(available=True, property=name, label=label)

    async def _resolve_google(
        self, settings: Settings, cache: dict[str, PropertyDefinition | None]
    ) -> GoogleCrmBinding:
        """Verify the optional Google-to-CRM link (``no_crm_link`` when unavailable).

        The touch-date property must be a ``datetime`` property, whose value is the touch
        instant; a ``date`` property leaves the link unavailable.
        """
        campaign = settings.hubspot_google_campaign_property
        touch = settings.hubspot_google_touch_date_property
        unset = [
            env
            for env, value in ((_GOOGLE_CAMPAIGN_ENV, campaign), (_GOOGLE_TOUCH_ENV, touch))
            if value is None
        ]
        if campaign is None or touch is None:
            return GoogleCrmBinding(
                available=False,
                campaign_property=campaign,
                touch_date_property=touch,
                reason=f"{' and '.join(unset)} not configured",
            )
        problems: list[str] = []
        if await self._contact_property(campaign, cache) is None:
            problems.append(f"{_GOOGLE_CAMPAIGN_ENV} property '{campaign}' not found on contacts")
        touch_definition = await self._contact_property(touch, cache)
        if touch_definition is None:
            problems.append(f"{_GOOGLE_TOUCH_ENV} property '{touch}' not found on contacts")
        elif not touch_definition.is_instant:
            problems.append(
                f"{_GOOGLE_TOUCH_ENV} property '{touch}' has type '{touch_definition.type}', "
                "not a datetime property"
            )
        if problems:
            return GoogleCrmBinding(
                available=False,
                campaign_property=campaign,
                touch_date_property=touch,
                reason="; ".join(problems),
            )
        return GoogleCrmBinding(
            available=True, campaign_property=campaign, touch_date_property=touch
        )

    # ------------------------------------------------------------------
    # Contacts (objects_batch_read; AAP 0.3.3 matching and contact refresh)
    # ------------------------------------------------------------------

    async def contacts_by_email(
        self, emails: Iterable[str], props: ContactPropertyMap | None = None
    ) -> EmailLookup:
        """Look contacts up by email, at most 100 addresses per batch read.

        ``POST /crm/objects/2026-09/contacts/batch/read`` with ``idProperty=email``. Each
        address is normalised (trimmed, lowercased) and de-duplicated; on a live client a
        ``@sim.invalid`` address is never sent and lands in ``skipped``. Returned contacts
        are mapped back to their inputs through their primary and additional emails. The
        addresses exist only in memory for the call: they are never logged, stored or
        placed in an exception, and the result is keyed by their SHA-256.

        Args:
            emails: The lead emails to look up.
            props: The contact properties to request and parse, overriding the bound map
                (:attr:`contact_properties`); ``None`` uses the bound map.

        Returns:
            Every input's digest in exactly one of ``found``, ``not_found``, ``failed`` or
            ``skipped``, and in ``interrupted_by`` the first ``TransientError`` a call still
            raised after its retries, if any.

        Raises:
            ValueError: When ``emails`` is one string or holds a blank or non-string entry;
                before any call.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When no ``props`` is given and none is bound (code
                ``CONTACT_PROPERTIES_UNBOUND``, before any call), or a request or a
                response breaks the contract.
        """
        props = self._contact_properties_for(props)
        given: object = emails
        if isinstance(given, str):
            raise ValueError("emails must be an iterable of addresses, not one string")
        pending: dict[str, str] = {}
        skipped: set[str] = set()
        for value in emails:
            raw: object = value
            if not isinstance(raw, str):
                raise ValueError("every email must be a string")
            address = normalize_email(raw)
            digest = email_sha256(address)
            if digest in pending or digest in skipped:
                continue
            if self._live and _is_simulated_email(address):
                skipped.add(digest)
                continue
            pending[digest] = address
        if skipped:
            self._log.warning(
                "hubspot.batch_read.simulated_withheld", object_type=_CONTACTS, count=len(skipped)
            )

        found: dict[str, ContactRow] = {}
        not_found: set[str] = set()
        failed: set[str] = set()
        interrupted: TransientError | None = None
        calls = 0
        path = _objects_batch_read(_CONTACTS)
        endpoint = f"{_SURFACE_OBJECTS} {_CONTACTS}"
        properties = list(props.request_properties())
        for chunk in _chunks(list(pending.items()), MAX_BATCH_READ_INPUTS):
            digests = {digest for digest, _address in chunk}
            body: dict[str, object] = {
                "idProperty": "email",
                "inputs": [{"id": address} for _digest, address in chunk],
                "properties": properties,
            }
            calls += 1
            try:
                response = await self._send(
                    "POST", path, endpoint=endpoint, json_body=body, accept_statuses=_BATCH_STATUSES
                )
            except TransientError as error:
                failed |= digests
                if interrupted is None:
                    interrupted = error
                self._log.warning(
                    "hubspot.batch_read.call_failed",
                    object_type=_CONTACTS,
                    inputs=len(digests),
                    **error.log_fields,
                )
                continue
            result = parse_batch_read(response.body)
            if not result.is_complete:
                failed |= digests
                continue
            chunk_found: dict[str, ContactRow] = {}
            for record in result.records:
                matches = contact_emails_sha256(record) & digests
                if matches:
                    row = parse_contact(record, props)
                    for digest in matches:
                        chunk_found.setdefault(digest, row)
            chunk_not_found: set[str] = set()
            chunk_failed: set[str] = set()
            for entry in result.errors:
                listed = set(entry.email_sha256s) & digests
                if entry.category == _OBJECT_NOT_FOUND:
                    chunk_not_found |= listed
                else:
                    chunk_failed |= listed
            for digest in digests:
                # A returned contact is never also listed (the parser rejects that
                # contradiction); an explicit failure outranks an explicit "not found"; an
                # input neither returned nor listed failed.
                if digest in chunk_found:
                    found[digest] = chunk_found[digest]
                elif digest in chunk_failed or digest not in chunk_not_found:
                    failed.add(digest)
                else:
                    not_found.add(digest)
        pending.clear()

        lookup = EmailLookup(
            found=MappingProxyType(found),
            not_found=frozenset(not_found),
            failed=frozenset(failed),
            skipped=frozenset(skipped),
            interrupted_by=interrupted,
        )
        self._log.info(
            "hubspot.batch_read",
            object_type=_CONTACTS,
            id_property="email",
            calls=calls,
            found=len(lookup.found),
            not_found=len(lookup.not_found),
            failed=len(lookup.failed),
            skipped=len(lookup.skipped),
        )
        return lookup

    async def contacts_by_id(
        self, ids: Iterable[str], props: ContactPropertyMap | None = None
    ) -> ContactBatch:
        """Refresh contacts by HubSpot id, at most 100 per batch read.

        ``POST /crm/objects/2026-09/contacts/batch/read`` without ``idProperty``. A requested
        id that is absent from the response, listed in ``errors[]``, or in a call that
        failed after its retries reads ``error``: the caller keeps its previous stage values
        and marks them unknown for the window.

        Args:
            ids: The contact ids to refresh.
            props: The contact properties to request and parse, overriding the bound map
                (:attr:`contact_properties`); ``None`` uses the bound map.

        Returns:
            The returned contacts, every requested id's read status and, in
            ``interrupted_by``, the first ``TransientError`` a call still raised after its
            retries, if any.

        Raises:
            ValueError: When ``ids`` is one string or holds a blank id; before any call.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When no ``props`` is given and none is bound (code
                ``CONTACT_PROPERTIES_UNBOUND``, before any call), or a request or a
                response breaks the contract.
        """
        props = self._contact_properties_for(props)
        requested = _normalise_ids(ids, "ids")
        records, interrupted = await self._read_by_id(
            _CONTACTS,
            requested,
            props.request_properties(),
            partial(parse_contact, props=props),
        )
        read_status: dict[str, ContactReadStatus] = {
            hs_id: "ok" if hs_id in records else "error" for hs_id in requested
        }
        return ContactBatch(
            records=MappingProxyType(records), read_status=read_status, interrupted_by=interrupted
        )

    # ------------------------------------------------------------------
    # Target reads (objects_batch_read; AAP 0.3.3 "Target reads")
    # ------------------------------------------------------------------

    async def meetings(self, ids: Iterable[str]) -> TargetRead[MeetingRow]:
        """Read associated meetings by id, at most 100 per batch read.

        Requests :data:`~ads_engine.connectors.hubspot.parser.MEETING_PROPERTIES`
        (``hs_createdate``, ``hs_timestamp``, ``hs_meeting_start_time``,
        ``hs_meeting_outcome``, ``hs_meeting_source``).

        Args:
            ids: The meeting ids an association read returned.

        Returns:
            The meetings read, the ids not read, the read status and the first
            ``TransientError`` a call still raised after its retries, if any.

        Raises:
            ValueError: When ``ids`` is one string or holds a blank id.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When a request or a response breaks the contract.
        """
        requested = _normalise_ids(ids, "ids")
        records, interrupted = await self._read_by_id(
            "meetings", requested, MEETING_PROPERTIES, parse_meeting
        )
        return _target_read(requested, records, interrupted)

    async def deals(self, ids: Iterable[str]) -> TargetRead[DealRow]:
        """Read associated deals by id, at most 100 per batch read.

        Requests :data:`~ads_engine.connectors.hubspot.parser.DEAL_PROPERTIES`
        (``createdate``, ``dealstage``, ``pipeline``, ``amount``).

        Args:
            ids: The deal ids an association read returned.

        Returns:
            The deals read, the ids not read, the read status and the first
            ``TransientError`` a call still raised after its retries, if any.

        Raises:
            ValueError: When ``ids`` is one string or holds a blank id.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When a request or a response breaks the contract.
        """
        requested = _normalise_ids(ids, "ids")
        records, interrupted = await self._read_by_id(
            "deals", requested, DEAL_PROPERTIES, parse_deal
        )
        return _target_read(requested, records, interrupted)

    async def _read_by_id[RowT: _Identified](
        self,
        object_type: str,
        ids: Sequence[str],
        properties: Sequence[str],
        parse: Callable[[HubSpotRecord], RowT],
    ) -> tuple[dict[str, RowT], TransientError | None]:
        """Batch-read records by id; return the requested ones that came back.

        A call that fails after its retries, or whose batch status is not ``COMPLETE``,
        reads none of its ids; ids HubSpot omits or lists in ``errors[]`` are not read.
        Records HubSpot returns under an id that was not requested (a merged contact's new
        id) are ignored, so the caller sees the requested id as not read.

        Returns:
            The records read, keyed by id, and the first ``TransientError`` a call still
            raised after its retries (``None`` when every call was answered; a batch status
            other than ``COMPLETE`` is not one).
        """
        path = _objects_batch_read(object_type)
        endpoint = f"{_SURFACE_OBJECTS} {object_type}"
        records: dict[str, RowT] = {}
        interrupted: TransientError | None = None
        calls = failed_calls = 0
        for chunk in _chunks(ids, MAX_BATCH_READ_INPUTS):
            body: dict[str, object] = {
                "inputs": [{"id": hs_id} for hs_id in chunk],
                "properties": list(properties),
            }
            calls += 1
            try:
                response = await self._send(
                    "POST", path, endpoint=endpoint, json_body=body, accept_statuses=_BATCH_STATUSES
                )
            except TransientError as error:
                failed_calls += 1
                if interrupted is None:
                    interrupted = error
                self._log.warning(
                    "hubspot.batch_read.call_failed",
                    object_type=object_type,
                    inputs=len(chunk),
                    **error.log_fields,
                )
                continue
            result = parse_batch_read(response.body)
            if not result.is_complete:
                failed_calls += 1
                continue
            wanted = set(chunk)
            for record in result.records:
                row = parse(record)
                if row.hs_id in wanted:
                    records.setdefault(row.hs_id, row)
        missing = len(ids) - len(records)
        log_event = self._log.warning if missing else self._log.info
        log_event(
            "hubspot.batch_read",
            object_type=object_type,
            id_property="id",
            calls=calls,
            failed_calls=failed_calls,
            requested=len(ids),
            read=len(records),
            missing=missing,
        )
        return records, interrupted

    # ------------------------------------------------------------------
    # Associations (associations_batch_read; AAP 0.2.4 exact contract)
    # ------------------------------------------------------------------

    async def associations(
        self,
        from_type: str,
        to_type: str,
        ids: Iterable[str],
        *,
        max_continuations: int | None = None,
    ) -> AssociationFetch:
        """Read every input's associations to ``to_type``, following each input's cursor.

        ``POST /crm/associations/2026-09/{fromObjectType}/{toObjectType}/batch/read``,
        used for ``contacts`` to ``meetings``, ``contacts`` to ``deals`` and ``deals`` to
        ``contacts``:

        1. Initial calls of at most 1,000 inputs send exactly ``{"inputs": [{"id": …}]}``.
        2. Each input whose result carries ``paging.next.after`` is re-sent as
           ``{"id": …, "after": …}`` with its own cursor, batched at most 1,000 per call,
           until no paging object comes back. Inputs without paging are never re-sent, and
           ``paging.next.link`` is ignored.

        Each input ends in one status:

        * ``ok``: targets read and every continuation succeeded;
        * ``ok_empty``: no associations, a genuine zero, from an empty ``to`` or an
          ``errors[]`` entry whose subcategory is ``crm.associations.NO_ASSOCIATIONS_FOUND``;
        * ``error``: any other ``errors[]`` entry, an input in neither ``results`` nor
          ``errors``, or an initial call that failed after its retries or returned a batch
          status other than ``COMPLETE`` (every input of that call);
        * ``partial``: a continuation failed (after its retries, through an error entry or
          a status other than ``COMPLETE``) or was left unsent by ``max_continuations``, after
          some targets were read; with no target read yet it is ``error``.

        **Plan correction**: AAP 0.3.3 reads every input listed in a 207 ``errors[]`` as
        ``error``. HubSpot, however, reports an input that simply has no associations
        exactly as such an entry (category ``OBJECT_NOT_FOUND``, subcategory
        ``crm.associations.NO_ASSOCIATIONS_FOUND``, the input in ``context.fromObjectId``), as
        ``backend/tests/contracts/hubspot/associations_batch_read.json`` records. Reading it
        as ``error`` would make every contact without meetings or deals "unknown" and
        withhold W2 and C1b permanently, while AAP 0.5.4.7 requires ``ok_empty`` to be the
        genuine zero. Only that subcategory is ``ok_empty``; every other entry is ``error``.

        A call that still fails with ``TransientError`` after its retries, a batch status
        other than ``COMPLETE``, a per-input ``errors[]`` entry other than
        ``NO_ASSOCIATIONS_FOUND``, an input missing from the response and a repeated cursor
        are recorded per input (``error`` or ``partial``) rather than raised, so the caller
        keeps what was read; the first such ``TransientError`` is also returned as
        ``interrupted_by`` for the caller to hand to ``ads_engine.sync.mode``.
        ``AccessDenied`` and ``QueryDefect`` propagate unchanged for ``ads_engine.sync.mode``
        to decide.

        Args:
            from_type: The input records' object type, for example ``contacts``.
            to_type: The target object type, for example ``meetings``.
            ids: The input record ids.
            max_continuations: The most continuation rounds after the initial calls, each
                round re-sending every input still carrying a cursor; ``0`` sends the initial
                calls only. An input whose cursor is left unsent is ``partial`` (``error``
                with no target read) with the reason "continuation limit reached; later pages
                were not read". ``None`` (every sync) continues until no paging comes back;
                ``capture-fixtures`` sends ``0``.

        Returns:
            Every requested input's targets and status, in request order, and the first
            ``TransientError`` a call still raised after its retries, if any.

        Raises:
            ValueError: When an object type is not a path segment, ``ids`` is one string or
                holds a blank id, or ``max_continuations`` is given and is not a
                non-negative integer.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When a request or a response breaks the contract.
        """
        path = _associations_batch_read(from_type, to_type)
        requested = _normalise_ids(ids, "ids")
        if max_continuations is not None and (
            isinstance(max_continuations, bool)
            or not isinstance(max_continuations, int)
            or max_continuations < 0
        ):
            raise ValueError("max_continuations must be a non-negative integer when given")
        endpoint = f"{_SURFACE_ASSOCIATIONS} {from_type}/{to_type}"
        states = {input_id: _InputState() for input_id in requested}
        calls = 0
        rounds = 0
        interrupted: TransientError | None = None

        for chunk in _chunks(requested, MAX_ASSOCIATION_INPUTS):
            calls += 1
            batch, failure, error = await self._association_call(
                path, endpoint, [{"id": input_id} for input_id in chunk]
            )
            if interrupted is None:
                interrupted = error
            for input_id in chunk:
                if batch is None:
                    states[input_id].fail(failure)
                else:
                    states[input_id].apply(batch, input_id)

        while True:
            cursors = [
                (input_id, state.after)
                for input_id, state in states.items()
                if state.after is not None
            ]
            if not cursors:
                break
            if max_continuations is not None and rounds >= max_continuations:
                for input_id, _after in cursors:
                    states[input_id].fail(_REASON_CONTINUATION_LIMIT)
                break
            rounds += 1
            for page in _chunks(cursors, MAX_ASSOCIATION_INPUTS):
                calls += 1
                batch, failure, error = await self._association_call(
                    path, endpoint, [{"id": input_id, "after": after} for input_id, after in page]
                )
                if interrupted is None:
                    interrupted = error
                for input_id, _after in page:
                    state = states[input_id]
                    state.after = None
                    if batch is None:
                        state.fail(failure)
                    else:
                        state.apply(batch, input_id)

        fetch = AssociationFetch(
            from_type=from_type,
            to_type=to_type,
            per_input=MappingProxyType(
                {input_id: state.outcome() for input_id, state in states.items()}
            ),
            interrupted_by=interrupted,
        )
        counts = fetch.status_counts()
        unknown = counts.get("partial", 0) + counts.get("error", 0)
        log_event = self._log.warning if unknown else self._log.info
        log_event(
            "hubspot.associations",
            from_type=from_type,
            to_type=to_type,
            inputs=len(requested),
            calls=calls,
            ok=counts.get("ok", 0),
            ok_empty=counts.get("ok_empty", 0),
            partial=counts.get("partial", 0),
            error=counts.get("error", 0),
        )
        return fetch

    async def _association_call(
        self, path: str, endpoint: str, inputs: list[dict[str, str]]
    ) -> tuple[AssociationBatch | None, str, TransientError | None]:
        """Send one associations batch read; a failed call returns ``None`` and its reason.

        A call fails when it still raises ``TransientError`` after its retries, which is
        returned as the third element, or when its batch status is not ``COMPLETE`` (a
        per-input outcome, no error returned). ``AccessDenied`` and ``QueryDefect``
        propagate.
        """
        try:
            response = await self._send(
                "POST",
                path,
                endpoint=endpoint,
                json_body={"inputs": inputs},
                accept_statuses=_BATCH_STATUSES,
            )
        except TransientError as error:
            self._log.warning(
                "hubspot.associations.call_failed", inputs=len(inputs), **error.log_fields
            )
            return None, error.reason, error
        batch = parse_association_batch(response.body)
        if not batch.is_complete:
            return None, f"{endpoint}: batch status {batch.status}", None
        return batch, "", None

    # ------------------------------------------------------------------
    # CRM Search (crm_search)
    # ------------------------------------------------------------------

    async def search_page(self, object_type: str, body: Mapping[str, object]) -> SearchPage:
        """Run one CRM Search request and decode its page.

        ``POST /crm/objects/2026-09/{objectType}/search``. The body is checked before it is
        sent: it must serialise to fewer than 3,000 characters, carry an integer ``limit``
        from 1 to 200, and carry no ``after`` cursor at or past the 10,000-result ceiling,
        because HubSpot rejects each of these with HTTP 400.

        Args:
            object_type: The object type in the path, ``deals`` or ``contacts``.
            body: The ``PublicObjectSearchRequest``, serialised as JSON unchanged.

        Returns:
            The page's raw records, the query's ``total`` and ``paging.next.after``.

        Raises:
            ValueError: When ``object_type`` is not a path segment.
            AccessDenied: When the token or its scopes are refused.
            TransientError: When the call still fails after its retries.
            QueryDefect: When the body fails a check above, or the response breaks the
                contract.
        """
        path = _objects_search(object_type)
        endpoint = f"{_SURFACE_SEARCH} {object_type}"
        payload = _check_search_body(body, endpoint=endpoint)
        response = await self._send("POST", path, endpoint=endpoint, json_body=payload)
        page = parse_search_page(response.body)
        self._log.debug(
            "hubspot.search",
            scope="page",
            object_type=object_type,
            results=len(page.results),
            total=page.total,
            more=page.after is not None,
        )
        return page

    async def search_partitioned(
        self,
        object_type: str,
        search_filter: SearchFilter,
        time_range: tuple[datetime, datetime],
    ) -> SearchOutcome:
        """Read every record whose range property falls in ``time_range``, or state a floor.

        Delegates to :func:`ads_engine.connectors.hubspot.search.search_partitioned` with this
        client as the pager: one-day partitions, halved to one hour while ``total`` exceeds
        10,000, and a one-hour partition still above the ceiling read to exactly 10,000
        results and marked ``incomplete``.

        Args:
            object_type: ``deals`` or ``contacts``.
            search_filter: The range property, extra filters, sort and properties, such as
                ``search.deal_search_filter()`` or ``search.google_contacts_filter(...)``.
            time_range: ``(start, end)``, timezone-aware, start inclusive, end exclusive.

        Returns:
            The de-duplicated records with their completeness, and in ``interrupted_by`` the
            ``TransientError`` that stopped the search after its retries, if one did.

        Raises:
            ValueError: On an invalid object type or range, before any call.
            AccessDenied: When the token or its scopes are refused.
            QueryDefect: When a request or a response breaks the contract.
        """
        return await search.search_partitioned(self, object_type, search_filter, time_range)

    # ------------------------------------------------------------------
    # Recovery-readiness probe (AAP 0.3.3)
    # ------------------------------------------------------------------

    async def probe(self) -> None:
        """Exercise every HubSpot capability the product uses, raising on the first failure.

        The checks, in order:

        1. a one-email contacts batch read of :data:`PROBE_EMAIL` (``OBJECT_NOT_FOUND`` is
           success);
        2. a one-result deals search with no filters, sorted by ``createdate``;
        3. a one-input associations read: ``deals`` to ``contacts`` on the deal found, or
           ``contacts`` to ``deals`` on id ``"0"`` when the portal has no deals. Any decoded
           result or error entry is success.

        A batch status other than ``COMPLETE`` fails its check as a ``TransientError``.
        Nothing is retried beyond each call's own retries, and nothing is recorded.

        Raises:
            AccessDenied: When the token or a scope is refused; the source stays simulated.
            TransientError: When a check still fails after its retries.
            QueryDefect: When a request or a response breaks its contract.
        """
        contacts_endpoint = f"{_SURFACE_OBJECTS} {_CONTACTS}"
        response = await self._send(
            "POST",
            _objects_batch_read(_CONTACTS),
            endpoint=contacts_endpoint,
            json_body={
                "idProperty": "email",
                "inputs": [{"id": PROBE_EMAIL}],
                "properties": ["email"],
            },
            accept_statuses=_BATCH_STATUSES,
        )
        _require_complete(parse_batch_read(response.body).status, endpoint=contacts_endpoint)

        page = await self.search_page(
            "deals",
            {
                "filterGroups": [],
                "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}],
                "properties": ["createdate"],
                "limit": 1,
            },
        )
        if page.results:
            from_type, to_type = "deals", _CONTACTS
            input_id = str(page.results[0]["id"])
        else:
            from_type, to_type, input_id = _CONTACTS, "deals", "0"
        associations_endpoint = f"{_SURFACE_ASSOCIATIONS} {from_type}/{to_type}"
        response = await self._send(
            "POST",
            _associations_batch_read(from_type, to_type),
            endpoint=associations_endpoint,
            json_body={"inputs": [{"id": input_id}]},
            accept_statuses=_BATCH_STATUSES,
        )
        batch = parse_association_batch(response.body)
        _require_complete(batch.status, endpoint=associations_endpoint)
        self._log.info("hubspot.probe", passed=True, live=self._live, calls=3)
