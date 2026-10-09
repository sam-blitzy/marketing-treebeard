"""Integration tests of durable lead-to-contact matching (AAP 0.11.2, ``test_lead_matching.py``).

AAP 0.3.3 ("Lead-to-contact matching", "Staged matching on a CRM dataset change", "Form
identity") and the AAP 0.1.1 requirement that lead emails are persisted only as SHA-256
hashes. Each test names the AAP 0.11.2 guarantee it proves:

* **Late contacts**: a contact HubSpot creates two days after the lead is matched on the
  1 h / 6 h / 24 h / daily retry schedule, and a row that is not due is never claimed.
* **Transient failure**: a HubSpot 5xx that outlives its retries leaves the row ``error``,
  retried on schedule, with the source mode untouched; the next due run matches it.
* **Staged CRM recovery**: a building HubSpot dataset stages a row for every backfill-range
  lead and matches it while the active dataset's matches stay in use; activation is refused
  while a building row is ``pending`` or a ``hubspot.*`` freshness row is ``unknown``; one
  activation then swaps the matching state without resetting any lead to ``pending``; when
  LinkedIn activates a new dataset during a real live HubSpot build, the build itself
  stages, attempts and hydrates the new leads before it activates; and a new live LinkedIn
  dataset is staged against HubSpot's active primary.
* **Versioned forms**: a response whose answered question ids are missing from its own
  decorated questions ends ``no_email`` "form map mismatch" and is never retried.
* **Terminal cases**: an unknown form version ends ``no_email``; an email LinkedIn no longer
  returns ends ``error`` "email no longer retrievable" and counts as unresolved; test leads
  are never stored; a lead whose ad account no entity sync has stored yet fails the lead
  sync's attempt and keeps its cursor, so a later run stores and stages it, while the
  attempt's placeable leads are stored and staged at once.
* **Restart safety**: a crash mid-batch loses no row, and concurrent runs claim disjoint
  rows (``FOR UPDATE SKIP LOCKED``).
* **No plaintext**: after a full simulated lead sync with matching, and after a live one,
  no text, character, jsonb or text-array column of any table holds a lead email, and no
  log line does.

Every platform answers through an ``httpx.MockTransport``. The live LinkedIn Lead Sync
listing and batch re-fetch and the HubSpot contacts batch read by email are hand-written
handlers (:class:`Platforms`) wrapped in the suite's contract checks
(``contract_checked_transport``), so every request and every 2xx response is validated
against ``tests/contracts/linkedin/lead_form_responses.json``,
``lead_form_responses_batch.json`` and ``tests/contracts/hubspot/objects_batch_read.json``;
the handlers hold plaintext emails only in test memory. A simulated HubSpot portal is the
simulator's own transport (``SourceModeController.open_clients``). So is the live portal of
the one case that runs a whole live HubSpot build (property verification, matching,
contact refresh, deal search, associations and target reads), in strict mode, so a request
that breaks its contract raises. Every clock is fixed: ``run_matching`` gets an explicit
``now``, the controllers and dataset manager stand at :data:`T0`, the retry policy keeps its
production attempts with its sleep replaced, and the HubSpot throttle runs on a virtual
clock, so no test sleeps in real time.

The tests run against the PostgreSQL 18 test database of ``tests/conftest.py`` (``db`` and
``sync_engine``); nothing is skipped without it.
"""

import dataclasses
import hashlib
import json
import re
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal
from urllib.parse import unquote

import anyio
import httpx
import pytest
import structlog
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.connectors.errors import TransientError
from ads_engine.connectors.hubspot.client import RateLimiter
from ads_engine.connectors.hubspot.parser import ContactPropertyMap
from ads_engine.connectors.linkedin.parser import (
    REASON_FORM_MAP_MISMATCH,
    REASON_UNKNOWN_FORM_VERSION,
)
from ads_engine.db.models.accounts import AdAccount, Campaign, LeadForm, LeadFormVersion
from ads_engine.db.models.crm import CrmContact, Lead, LeadMatch
from ads_engine.db.models.ops import DatasetFreshness, SourceConnection, SourceDataset, SyncRun
from ads_engine.settings import Settings, Source, get_settings
from ads_engine.simulation import HUBSPOT_HASHED_CONTACT_IDS, SIM_EMAIL_DOMAIN
from ads_engine.simulation.generator import SimulatedWorld
from ads_engine.simulation.scenarios import SCENARIOS
from ads_engine.sync import jobs
from ads_engine.sync.datasets import DatasetManager, DatasetStateError
from ads_engine.sync.freshness import (
    FreshnessOutcome,
    load_freshness,
    mark_unknown,
    record_freshness,
)
from ads_engine.sync.jobs import LEAD_OVERLAP, JobContext, JobResult, LeadSyncJob
from ads_engine.sync.lead_match import (
    BATCH_SIZE,
    MATCH_HORIZON,
    REASON_EMAIL_UNRETRIEVABLE,
    REASON_LOOKUP_FAILED,
    REASON_NO_CONTACT,
    RETRY_DELAYS,
    MatchClients,
    MatchReport,
    create_pending,
    next_attempt_after,
    run_matching,
    stage_rows,
)
from ads_engine.sync.mode import REASON_RECOVERED, SourceModeController

pytestmark = pytest.mark.anyio

type Sessions = async_sessionmaker[AsyncSession]
type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]
type CheckedTransportFactory = Callable[[str, Handler], httpx.MockTransport]
type RecordingFactory = Callable[[httpx.MockTransport], Any]
"""``recording_transport``: wraps a transport and keeps its ``requests``."""
type WorldFactory = Callable[..., SimulatedWorld]
type SimulatorFactory = Callable[..., httpx.MockTransport]
"""``mock_transport_for``: the simulator's transport of one source, answering from a world."""
type CredentialsFactory = Callable[[str], Mapping[str, str]]
"""``live_credentials``: configures one source's fake credentials."""
type Decoration = Literal["full", "mismatch", "none"]

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------

T0: Final = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
"""The fixed instant of every controller, dataset manager and simulated world here."""

BACKFILL_DAYS: Final = 180
"""``BACKFILL_DAYS``' documented default, which the isolated test environment keeps."""

BACKFILL_START: Final = T0 - timedelta(days=BACKFILL_DAYS)
"""The earliest submission a HubSpot primary build stages (AAP 0.3.2 "Initial backfill")."""

EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
"""The origin of LinkedIn's epoch-millisecond instants."""

STAGED_RECOVERY_SCENARIO: Final = "leadmatch.staged_recovery.case"
"""The catalogue world whose simulated HubSpot portal backs the simulated primary dataset."""

CAMPAIGN_ID: Final = "614000001"
CAMPAIGN_URN: Final = f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}"
NEW_ACCOUNT_ID: Final = "990000002"
NEW_CAMPAIGN_URN: Final = "urn:li:sponsoredCampaign:614000002"
"""A second ad account, configured after the first, and its campaign: no entity sync of the
account has succeeded yet, so nothing of it is stored."""
LEAD_CURSOR: Final = T0 - timedelta(hours=6)
"""The ``linkedin.leads`` cursor (``last_success_at``) the incremental lead-sync tests seed."""
CREATIVE_URN: Final = "urn:li:sponsoredCreative:715000001"
FORM_ID: Final = 7001
FORM_URN: Final = f"urn:li:leadGenForm:{FORM_ID}"
NAME_QID: Final = 1
EMAIL_QID: Final = 2
COMPANY_QID: Final = 3
"""Question ids of the hand-written form: first name, email and (undecorated) company."""

HUBSPOT_NAMES: Final = ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search")
"""The freshness rows a HubSpot primary dataset needs before it can activate."""

LINKEDIN_NAMES: Final = ("linkedin.entities", "linkedin.analytics_daily", "linkedin.segments")
"""The LinkedIn freshness rows the lead sync does not write itself."""

EMAIL_PATTERN: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
"""Anything shaped like an email address."""

SCANNED_TYPES: Final = ("text", "character varying", "character", "jsonb")
"""``information_schema.columns.data_type`` values scanned for plaintext emails."""

SCAN_EXCLUDED: Final = frozenset({("app_user", "email")})
"""The admin's login identity: not lead data, the only email column of the schema."""


def _clock() -> datetime:
    """Return :data:`T0`: the clock of every controller, manager and world here."""
    return T0


async def _no_sleep(_seconds: float) -> None:
    """Stand in for the retry policy's backoff sleep, so a retried call never waits."""


def _ms(instant: datetime) -> int:
    """Epoch milliseconds of a timezone-aware instant, LinkedIn's ``submittedAt`` unit."""
    return int((instant - EPOCH) / timedelta(milliseconds=1))


def _digest(email: str) -> str:
    """SHA-256 of the normalised (trimmed, lowercased) email: what ``lead.email_sha256`` holds.

    Computed here independently of the product's own helpers, as AAP 0.3.2 defines it.
    """
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _iso(instant: datetime) -> str:
    """HubSpot's ISO-8601 UTC instant format."""
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerCrashError(RuntimeError):
    """A failure inside a platform handler that is no connector error: a simulated crash."""


# ---------------------------------------------------------------------------------------------
# Wire bodies (contract shapes)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LeadSpec:
    """One lead response the hand-written LinkedIn platform serves.

    Attributes:
        response_id: The plain response id (``lead.external_id``).
        email: The lead's email, held only in test memory and on the wire.
        submitted_at: The submission instant.
        version: The form version named by ``versionedLeadGenFormUrn``.
        decoration: ``full``: the ``form`` decoration lists every answered question;
            ``mismatch``: the response also answers :data:`COMPANY_QID`, which its own
            decoration does not list; ``none``: no ``form`` decoration at all.
        test_lead: Whether LinkedIn flags the response as a test lead.
        account_id: The ad account owning the response, ``None`` for the platform's
            configured one (:attr:`Platforms.account_id`); a listing serves an account's own
            responses only.
        campaign_urn: The campaign that produced the response.
    """

    response_id: str
    email: str
    submitted_at: datetime
    version: int = 1
    decoration: Decoration = "full"
    test_lead: bool = False
    account_id: str | None = None
    campaign_urn: str = CAMPAIGN_URN


def _text_answer(question_id: int, answer: str) -> dict[str, Any]:
    """One text answer of ``formResponse.answers``."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": answer}}}


def _element(spec: LeadSpec, account_urn: str) -> dict[str, Any]:
    """One lead form response under the ``LEAD_FIELDS`` projection.

    Under the projection LinkedIn returns the campaign and creative as
    ``leadMetadataInfo`` and ``associatedEntityInfo`` and adds the ``form`` decoration of the
    response's own version, which carries no version field (the version is in the URN).
    """
    answers = [_text_answer(NAME_QID, "Ada"), _text_answer(EMAIL_QID, spec.email)]
    if spec.decoration == "mismatch":
        answers.append(_text_answer(COMPANY_QID, "Analytical Engines Ltd"))
    element: dict[str, Any] = {
        "id": spec.response_id,
        "submittedAt": _ms(spec.submitted_at),
        "testLead": spec.test_lead,
        "leadType": "SPONSORED",
        "owner": {"sponsoredAccount": account_urn},
        "ownerInfo": {"sponsoredAccountInfo": {"name": "Acme LinkedIn"}},
        "versionedLeadGenFormUrn": (f"urn:li:versionedLeadGenForm:({FORM_URN},{spec.version})"),
        "leadMetadataInfo": {
            "sponsoredLeadMetadataInfo": {
                "campaign": {
                    "id": spec.campaign_urn,
                    "name": "White paper - Lead gen",
                    "type": "SPONSORED_UPDATES",
                }
            }
        },
        "associatedEntityInfo": {
            "associatedCreative": {"id": CREATIVE_URN, "intendedStatus": "ACTIVE", "content": {}}
        },
        "formResponse": {"answers": answers},
    }
    if spec.decoration != "none":
        element["form"] = {
            "id": FORM_ID,
            "name": "White paper download",
            "content": {
                "questions": [
                    {"questionId": NAME_QID, "name": "firstName", "predefinedField": "FIRST_NAME"},
                    {"questionId": EMAIL_QID, "name": "email", "predefinedField": "EMAIL"},
                ]
            },
        }
    return element


def _contact_record(hs_id: str, email: str, created_at: datetime, now: datetime) -> dict[str, Any]:
    """One contact of a HubSpot objects batch read."""
    return {
        "id": hs_id,
        "properties": {"email": email, "lifecyclestage": "lead", "hs_object_id": hs_id},
        "createdAt": _iso(created_at),
        "updatedAt": _iso(now),
        "archived": False,
    }


def _restli_ids(request: httpx.Request) -> list[str]:
    """The plain response ids of a batch re-fetch's ``ids=List(...)``, quotes removed."""
    value = request.url.params["ids"]
    assert value.startswith("List(")
    assert value.endswith(")")
    ids: list[str] = []
    for item in value[len("List(") : -1].split(","):
        plain = unquote(item)
        if len(plain) >= 2 and plain.startswith('"') and plain.endswith('"'):
            plain = plain[1:-1]
        ids.append(plain)
    return ids


_TIME_RANGE: Final = re.compile(r"^\(start:(?P<start>[0-9]+),end:(?P<end>[0-9]+)\)$")


@dataclass(frozen=True, slots=True)
class PortalContact:
    """A contact of the hand-written HubSpot portal, which exists from ``created_at`` on."""

    hs_id: str
    created_at: datetime


@dataclass
class Platforms:
    """The hand-written live LinkedIn Lead Sync API and HubSpot portal of one test.

    Both handlers are async and are wrapped in the suite's contract checks, so every request
    and every 2xx body is validated against its contract before the client sees it.

    Attributes:
        account_id: The live LinkedIn ad account (``LINKEDIN_AD_ACCOUNT_IDS``).
        leads: The responses LinkedIn holds, by plain response id.
        contacts: The portal's contacts, by normalised email.
        now: The portal's clock: a contact exists once ``now`` reaches its ``created_at``.
            :meth:`Harness.match` sets it to the run's ``now``.
        refetch_errors: Response ids the batch re-fetch lists under ``errors``.
        refetch_omitted: Response ids the batch re-fetch leaves out of ``results``.
        hubspot_status: An error status every HubSpot call answers with, or ``None``.
        hubspot_crash_on_call: The 1-based HubSpot call that raises
            :class:`HandlerCrashError` instead of answering, or ``None``.
        refetch_gate: Awaited inside every batch re-fetch before it answers, or ``None``.
        listing_calls: Lead listing pages served.
        refetch_calls: The ids of every batch re-fetch, in call order.
        hubspot_calls: The emails of every HubSpot batch read by email, in call order.
    """

    account_id: str
    leads: dict[str, LeadSpec] = field(default_factory=dict)
    contacts: dict[str, PortalContact] = field(default_factory=dict)
    now: datetime = T0
    refetch_errors: set[str] = field(default_factory=set)
    refetch_omitted: set[str] = field(default_factory=set)
    hubspot_status: int | None = None
    hubspot_crash_on_call: int | None = None
    refetch_gate: Callable[[], Awaitable[None]] | None = None
    listing_calls: int = 0
    refetch_calls: list[list[str]] = field(default_factory=list)
    hubspot_calls: list[list[str]] = field(default_factory=list)

    @property
    def account_urn(self) -> str:
        """``urn:li:sponsoredAccount:{account_id}``."""
        return f"urn:li:sponsoredAccount:{self.account_id}"

    def add_lead(self, spec: LeadSpec) -> LeadSpec:
        """Make LinkedIn hold one response; return it."""
        self.leads[spec.response_id] = spec
        return spec

    def owner_urn(self, spec: LeadSpec) -> str:
        """The ``owner.sponsoredAccount`` URN of one response."""
        if spec.account_id is None:
            return self.account_urn
        return f"urn:li:sponsoredAccount:{spec.account_id}"

    def add_contact(self, email: str, hs_id: str, *, created_at: datetime) -> None:
        """Give the portal a contact with ``email``, created at ``created_at``."""
        self.contacts[email.strip().lower()] = PortalContact(hs_id, created_at)

    def emails_sent(self) -> list[str]:
        """Every email the portal received, in call order, repeats included."""
        return [email for call in self.hubspot_calls for email in call]

    def reset_calls(self) -> None:
        """Forget the calls recorded so far."""
        self.listing_calls = 0
        self.refetch_calls.clear()
        self.hubspot_calls.clear()

    # -- LinkedIn ---------------------------------------------------------------------------

    async def linkedin(self, request: httpx.Request) -> httpx.Response:
        """Answer a Lead Sync listing (``q=owner``) or a batch re-fetch (``ids=List(...)``)."""
        if "ids" in request.url.params:
            return await self._refetch(request)
        return self._listing(request)

    def _listing(self, request: httpx.Request) -> httpx.Response:
        """One offset page of the owner's responses submitted in ``submittedAtTimeRange``."""
        params = request.url.params
        assert params["q"] == "owner"
        assert params["limitedToTestLeads"] == "false"
        bounds = _TIME_RANGE.match(params["submittedAtTimeRange"])
        assert bounds is not None, params["submittedAtTimeRange"]
        start_ms, end_ms = int(bounds["start"]), int(bounds["end"])
        start, count = int(params["start"]), int(params["count"])
        owner = unquote(params["owner"])
        self.listing_calls += 1
        submitted = sorted(
            (
                spec
                for spec in self.leads.values()
                if owner == f"(sponsoredAccount:{self.owner_urn(spec)})"
                and start_ms <= _ms(spec.submitted_at) < end_ms
            ),
            key=lambda spec: (spec.submitted_at, spec.response_id),
        )
        page = submitted[start : start + count]
        body = {
            "elements": [_element(spec, self.owner_urn(spec)) for spec in page],
            "paging": {"start": start, "count": count, "total": len(page), "links": []},
        }
        return httpx.Response(200, json=body)

    async def _refetch(self, request: httpx.Request) -> httpx.Response:
        """Answer a batch re-fetch by id with the documented ``{results, statuses, errors}``."""
        ids = _restli_ids(request)
        self.refetch_calls.append(ids)
        if self.refetch_gate is not None:
            await self.refetch_gate()
        results: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        for response_id in ids:
            spec = self.leads.get(response_id)
            if response_id in self.refetch_errors:
                errors[response_id] = {"status": 404, "message": "Lead form response not found"}
            elif spec is not None and response_id not in self.refetch_omitted:
                results[response_id] = _element(spec, self.owner_urn(spec))
        return httpx.Response(200, json={"results": results, "statuses": {}, "errors": errors})

    # -- HubSpot ----------------------------------------------------------------------------

    async def hubspot(self, request: httpx.Request) -> httpx.Response:
        """Answer a contacts batch read by email: found contacts, the rest ``OBJECT_NOT_FOUND``."""
        payload = json.loads(request.content)
        assert request.url.path == "/crm/objects/2026-09/contacts/batch/read"
        assert payload["idProperty"] == "email"
        emails = [str(item["id"]) for item in payload["inputs"]]
        self.hubspot_calls.append(emails)
        if self.hubspot_crash_on_call == len(self.hubspot_calls):
            raise HandlerCrashError("the HubSpot handler crashed mid-batch")
        if self.hubspot_status is not None:
            return httpx.Response(
                self.hubspot_status,
                json={"status": "error", "message": "Service unavailable", "category": "ERROR"},
            )
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for email in emails:
            contact = self.contacts.get(email.strip().lower())
            if contact is not None and contact.created_at <= self.now:
                found.append(_contact_record(contact.hs_id, email, contact.created_at, self.now))
            else:
                missing.append(email)
        body: dict[str, Any] = {
            "status": "COMPLETE",
            "results": found,
            "startedAt": _iso(self.now),
            "completedAt": _iso(self.now),
        }
        if not missing:
            return httpx.Response(200, json=body)
        body["errors"] = [
            {
                "status": "error",
                "category": "OBJECT_NOT_FOUND",
                "message": "Could not get some CONTACT objects, they may be deleted or not exist",
                "context": {"ids": missing},
            }
        ]
        body["numErrors"] = 1
        return httpx.Response(207, json=body)


# ---------------------------------------------------------------------------------------------
# Logging isolation
# ---------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _uncached_loggers() -> Iterator[None]:
    """Keep every logger these tests use lazy, then restore structlog's configuration.

    ``configure_logging`` (run by any earlier application or ``captured_logs``) caches each
    logger's processor chain on its first use, and a later reconfiguration installs a new
    chain the cached logger never sees. The connector and lead-matching loggers first used
    here would then write past ``structlog.testing.capture_logs`` in the modules that run
    after this one. As ``tests/unit/test_cli.py`` does, nothing is cached while these tests
    run, and the configuration in force before each test is put back after it.
    """
    was_configured = structlog.is_configured()
    saved = structlog.get_config()
    structlog.configure(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        if was_configured:
            structlog.configure(**saved)
        else:
            structlog.reset_defaults()


# ---------------------------------------------------------------------------------------------
# The harness: controller, clients and the fixed clock
# ---------------------------------------------------------------------------------------------


def _controller(
    settings: Settings,
    sessions: Sessions,
    *,
    world: SimulatedWorld,
    transports: Mapping[Source, httpx.AsyncBaseTransport] | None = None,
) -> SourceModeController:
    """A controller on the test database whose clients never wait in real time.

    The retry policy is the production one (3 attempts, exponential backoff, ``Retry-After``
    honoured) with only its sleep replaced, read from a controller's own default so this
    module needs no import from the HTTP layer. The HubSpot throttle keeps its 4 requests
    per second on a virtual clock that each wait advances.
    """
    tick = [0.0]

    async def advance(seconds: float) -> None:
        """Advance the throttle's virtual clock instead of sleeping."""
        tick[0] += seconds

    retry = dataclasses.replace(SourceModeController(settings).retry, sleep=_no_sleep)
    return SourceModeController(
        settings,
        sessionmaker=sessions,
        world=world,
        clock=_clock,
        transports=dict(transports or {}),
        retry=retry,
        hubspot_limiter=RateLimiter(4.0, clock=lambda: tick[0], sleep=advance),
    )


@dataclass
class Harness:
    """Live LinkedIn and HubSpot answered by :class:`Platforms`, on the test database.

    Attributes:
        sessions: The application's session factory on the emptied test database.
        settings: The isolated settings with fake LinkedIn and HubSpot credentials.
        controller: The source-mode controller whose live transports are the platforms and
            whose simulated HubSpot is the staged-recovery catalogue world.
        platforms: The hand-written platforms.
        linkedin_requests: Every request the LinkedIn transport received.
        hubspot_requests: Every request the live HubSpot transport received.
    """

    sessions: Sessions
    settings: Settings
    controller: SourceModeController
    platforms: Platforms
    linkedin_requests: list[httpx.Request]
    hubspot_requests: list[httpx.Request]

    @property
    def manager(self) -> DatasetManager:
        """The dataset manager of the controller, on the fixed clock."""
        return self.controller.datasets

    @asynccontextmanager
    async def clients(self, *, hubspot: str = "live") -> AsyncIterator[MatchClients]:
        """Open live LinkedIn and a HubSpot client of ``hubspot``'s mode as matching clients."""
        async with (
            self.controller.open_clients("linkedin", "live") as linkedin,
            self.controller.open_clients("hubspot", hubspot) as portal,
        ):
            client = linkedin.require_linkedin()
            yield MatchClients(
                linkedin_for=lambda _lead_dataset_id: client,
                hubspot=portal.require_hubspot(),
                contact_properties=ContactPropertyMap(),
            )

    async def match(
        self,
        crm_dataset_id: int,
        *,
        now: datetime,
        hubspot: str = "live",
        lead_dataset_id: int | None = None,
    ) -> MatchReport:
        """Run ``run_matching`` on one CRM dataset at ``now``, the portal's clock set to it."""
        self.platforms.now = now
        async with self.clients(hubspot=hubspot) as clients:
            return await run_matching(
                self.sessions, crm_dataset_id, clients, now=now, lead_dataset_id=lead_dataset_id
            )

    async def lead_sync(
        self,
        linkedin_dataset_id: int,
        *,
        now: datetime = T0,
        backfill: bool = True,
        extra_accounts: Sequence[str] = (),
    ) -> JobResult:
        """Run the ``LeadSyncJob`` of a live LinkedIn dataset against the platforms.

        Args:
            linkedin_dataset_id: The dataset written.
            now: The run's instant.
            backfill: Read the backfill range; ``False`` reads the incremental range from
                the ``linkedin.leads`` cursor.
            extra_accounts: Ad accounts read after the configured one, as further
                ``LINKEDIN_AD_ACCOUNT_IDS`` entries would be.
        """
        async with self.controller.open_clients("linkedin", "live") as clients:
            if extra_accounts:
                clients = dataclasses.replace(
                    clients,
                    linkedin_account_ids=(*clients.linkedin_account_ids, *extra_accounts),
                )
            ctx = await JobContext.for_dataset(
                self.controller, linkedin_dataset_id, clients, now=now
            )
            return await LeadSyncJob(ctx, backfill=backfill).run()


def _build_harness(
    sessions: Sessions,
    live_credentials: CredentialsFactory,
    contract_checked_transport: CheckedTransportFactory,
    recording_transport: RecordingFactory,
    make_world: WorldFactory,
) -> Harness:
    """Configure fake LinkedIn and HubSpot credentials and wire the platforms in as live."""
    live_credentials("linkedin")
    live_credentials("hubspot")
    settings = get_settings()
    assert settings.backfill_days == BACKFILL_DAYS
    platforms = Platforms(account_id=settings.linkedin_ad_account_ids[0])
    linkedin = recording_transport(contract_checked_transport("linkedin", platforms.linkedin))
    hubspot = recording_transport(contract_checked_transport("hubspot", platforms.hubspot))
    controller = _controller(
        settings,
        sessions,
        world=make_world(STAGED_RECOVERY_SCENARIO, clock=_clock),
        transports={"linkedin": linkedin, "hubspot": hubspot},
    )
    return Harness(
        sessions=sessions,
        settings=settings,
        controller=controller,
        platforms=platforms,
        linkedin_requests=linkedin.requests,
        hubspot_requests=hubspot.requests,
    )


@pytest.fixture
def harness(
    db: Sessions,
    live_credentials: CredentialsFactory,
    contract_checked_transport: CheckedTransportFactory,
    recording_transport: RecordingFactory,
    make_world: WorldFactory,
) -> Harness:
    """The :class:`Harness` of one test, on the emptied test database."""
    return _build_harness(
        db, live_credentials, contract_checked_transport, recording_transport, make_world
    )


# ---------------------------------------------------------------------------------------------
# Seeding and reading rows
# ---------------------------------------------------------------------------------------------


async def _dataset(sessions: Sessions, source: str, mode: str) -> int:
    """Insert an active primary dataset and point its source's connection at it."""
    async with sessions() as session, session.begin():
        dataset = SourceDataset(
            source=source,
            mode=mode,
            state="active",
            role="primary",
            reason="seeded by the lead-matching tests",
            created_at=T0 - timedelta(days=200),
            activated_at=T0 - timedelta(days=200),
        )
        session.add(dataset)
        await session.flush()
        session.add(
            SourceConnection(
                source=source,
                mode=mode,
                status="ok",
                reason="seeded by the lead-matching tests",
                active_dataset_id=dataset.id,
            )
        )
        return dataset.id


async def _linkedin_entities(sessions: Sessions, dataset_id: int, account_id: str) -> int:
    """Store the ad account, campaign, lead form and its version-1 map of a LinkedIn dataset.

    Version 1 asks the email in :data:`EMAIL_QID`, so a version-1 map could decode any
    response of the form; version-specific decoding must still never use it for another
    version. Returns the campaign's id.
    """
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=account_id,
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=dataset_id,
            account_id=account.id,
            external_id=CAMPAIGN_ID,
            name="White paper - Lead gen",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("50.00"),
        )
        form = LeadForm(
            source="linkedin",
            dataset_id=dataset_id,
            canonical_urn=FORM_URN,
            name="White paper download",
            current_version=1,
        )
        session.add_all([campaign, form])
        await session.flush()
        session.add(
            LeadFormVersion(
                source="linkedin",
                dataset_id=dataset_id,
                lead_form_id=form.id,
                version=1,
                email_question_ids=[str(EMAIL_QID)],
                first_seen_at=T0 - timedelta(days=200),
            )
        )
        return campaign.id


async def _seed_leads(
    sessions: Sessions, dataset_id: int, specs: Iterable[LeadSpec]
) -> dict[str, int]:
    """Store leads as ingest would (email as its SHA-256 only); return ids by response id."""
    async with sessions() as session, session.begin():
        campaign_id = await session.scalar(
            select(Campaign.id).where(
                Campaign.dataset_id == dataset_id, Campaign.external_id == CAMPAIGN_ID
            )
        )
        assert campaign_id is not None, "store the dataset's entities first"
        leads = [
            Lead(
                source="linkedin",
                dataset_id=dataset_id,
                external_id=spec.response_id,
                submitted_at=spec.submitted_at,
                campaign_id=campaign_id,
                creative_id=None,
                lead_form_urn=FORM_URN,
                lead_form_version=spec.version,
                email_sha256=_digest(spec.email),
            )
            for spec in specs
        ]
        session.add_all(leads)
        await session.flush()
        return {lead.external_id: lead.id for lead in leads}


async def _stage(
    sessions: Sessions,
    lead_ids: Sequence[int],
    crm_dataset_id: int,
    *,
    now: datetime,
    no_email_reasons: Mapping[int, str] | None = None,
) -> int:
    """Stage leads with ``create_pending`` in one committed transaction."""
    async with sessions() as session, session.begin():
        return await create_pending(
            session, lead_ids, crm_dataset_id, now=now, no_email_reasons=no_email_reasons
        )


async def _record(
    sessions: Sessions,
    dataset_id: int,
    names: Iterable[str],
    *,
    reason: str | None = None,
) -> None:
    """Record a successful refresh of each named freshness row: complete, or with a reason."""
    outcome = FreshnessOutcome.success(
        T0,
        covered_from=BACKFILL_START.date(),
        covered_through=T0.date(),
        complete=reason is None,
        reason=reason,
    )
    async with sessions() as session, session.begin():
        for name in names:
            await record_freshness(session, dataset_id, name, outcome)


@dataclass(frozen=True, slots=True)
class MatchRow:
    """One ``lead_match`` row as committed."""

    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_checked_at: datetime | None
    contact_id: int | None
    contact_read_status: str | None
    reason: str | None


async def _rows(sessions: Sessions, crm_dataset_id: int) -> dict[int, MatchRow]:
    """Every ``lead_match`` row of one CRM dataset, by lead id."""
    async with sessions() as session:
        result = await session.execute(
            select(
                LeadMatch.lead_id,
                LeadMatch.status,
                LeadMatch.attempts,
                LeadMatch.next_attempt_at,
                LeadMatch.last_checked_at,
                LeadMatch.contact_id,
                LeadMatch.contact_read_status,
                LeadMatch.reason,
            ).where(LeadMatch.crm_dataset_id == crm_dataset_id)
        )
        return {lead_id: MatchRow(*values) for lead_id, *values in result}


async def _leads(sessions: Sessions, dataset_id: int) -> dict[str, Lead]:
    """Every lead of one LinkedIn dataset, by response id."""
    async with sessions() as session:
        rows = await session.scalars(select(Lead).where(Lead.dataset_id == dataset_id))
        return {lead.external_id: lead for lead in rows}


async def _state(sessions: Sessions, dataset_id: int) -> str:
    """The lifecycle state of one dataset."""
    async with sessions() as session:
        state = await session.scalar(
            select(SourceDataset.state).where(SourceDataset.id == dataset_id)
        )
        assert state is not None, dataset_id
        return state


@dataclass(frozen=True, slots=True)
class LivePair:
    """An active live LinkedIn dataset and an active live HubSpot primary."""

    linkedin: int
    hubspot: int


async def _live_pair(harness: Harness) -> LivePair:
    """Seed the live pair with the LinkedIn entities leads are placed under."""
    linkedin = await _dataset(harness.sessions, "linkedin", "live")
    hubspot = await _dataset(harness.sessions, "hubspot", "live")
    await _linkedin_entities(harness.sessions, linkedin, harness.platforms.account_id)
    return LivePair(linkedin=linkedin, hubspot=hubspot)


# ---------------------------------------------------------------------------------------------
# Late contacts: the retry schedule (AAP 0.3.3 "Retry schedule")
# ---------------------------------------------------------------------------------------------


def test_retry_schedule() -> None:
    """Late contacts: retries wait 1 h, 6 h and 24 h, then daily, until 30 days after submission.

    The schedule lets a contact HubSpot creates late still be found; past the horizon a row
    stops retrying (``None``) and keeps counting as unresolved.
    """
    submitted = T0 - timedelta(days=3)
    assert (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24)) == RETRY_DELAYS
    assert timedelta(days=30) == MATCH_HORIZON
    for attempts, delay in (
        (1, timedelta(hours=1)),
        (2, timedelta(hours=6)),
        (3, timedelta(hours=24)),
        (4, timedelta(days=1)),
        (5, timedelta(days=1)),
        (12, timedelta(days=1)),
    ):
        finished = submitted + timedelta(minutes=5 * attempts)
        assert next_attempt_after(attempts, submitted, finished) == finished + delay, attempts

    horizon = submitted + MATCH_HORIZON
    # A retry landing exactly on the horizon is still made; one past it is not.
    assert next_attempt_after(6, submitted, horizon - timedelta(days=1)) == horizon
    one_second_later = horizon - timedelta(days=1) + timedelta(seconds=1)
    assert next_attempt_after(6, submitted, one_second_later) is None
    assert next_attempt_after(1, submitted, horizon) is None
    assert next_attempt_after(2, submitted, horizon - timedelta(hours=5)) is None

    with pytest.raises(ValueError, match="attempts"):
        next_attempt_after(0, submitted, T0)
    with pytest.raises(ValueError, match="attempts"):
        next_attempt_after(True, submitted, T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        next_attempt_after(1, submitted.replace(tzinfo=None), T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        next_attempt_after(1, submitted, T0.replace(tzinfo=None))


async def test_no_email_rows_are_never_rescheduled(harness: Harness) -> None:
    """Late contacts: a ``no_email`` row is terminal and no later run claims it.

    ``no_email`` is decided by the LinkedIn response alone, so it is staged terminal (not
    ``pending``, with no ``next_attempt_at``) and never reaches LinkedIn or HubSpot again.
    """
    pair = await _live_pair(harness)
    spec = harness.platforms.add_lead(
        LeadSpec("noemail-0001", "first.noemail@example.com", T0 - timedelta(days=2))
    )
    lead_id = (await _seed_leads(harness.sessions, pair.linkedin, [spec]))[spec.response_id]
    staged_at = T0 - timedelta(days=2)
    inserted = await _stage(
        harness.sessions,
        [lead_id],
        pair.hubspot,
        now=staged_at,
        no_email_reasons={lead_id: REASON_FORM_MAP_MISMATCH},
    )
    assert inserted == 1
    staged = (await _rows(harness.sessions, pair.hubspot))[lead_id]
    assert staged == MatchRow("no_email", 0, None, staged_at, None, None, REASON_FORM_MAP_MISMATCH)

    for later in (T0, T0 + timedelta(days=1), T0 + MATCH_HORIZON):
        report = await harness.match(pair.hubspot, now=later)
        assert report == MatchReport()
    assert (await _rows(harness.sessions, pair.hubspot))[lead_id] == staged
    assert harness.linkedin_requests == []
    assert harness.hubspot_requests == []


async def test_late_contact_matched_on_retry(harness: Harness) -> None:
    """Late contacts: a contact created two days after the lead is matched on retry.

    The first attempt five minutes after submission finds no contact (``OBJECT_NOT_FOUND``),
    so the row is ``no_match`` and due one hour later. Each run at the row's due time follows
    the 1 h / 6 h / 24 h / daily schedule until the portal has created the contact, and the
    match then points at a contact of the same CRM dataset, read ``ok``.
    """
    pair = await _live_pair(harness)
    submitted = T0 - timedelta(days=5)
    email = "Late.Contact@Example.com"
    spec = harness.platforms.add_lead(LeadSpec("late-0001", email, submitted))
    created = submitted + timedelta(days=2)
    harness.platforms.add_contact(email, "5100001", created_at=created)
    lead_id = (await _seed_leads(harness.sessions, pair.linkedin, [spec]))[spec.response_id]
    assert await _stage(harness.sessions, [lead_id], pair.hubspot, now=submitted) == 1

    first = submitted + timedelta(minutes=5)
    report = await harness.match(pair.hubspot, now=first)
    assert report == MatchReport(attempted=1, no_match=1, batches=1)
    row = (await _rows(harness.sessions, pair.hubspot))[lead_id]
    assert row == MatchRow(
        "no_match", 1, first + timedelta(hours=1), first, None, None, REASON_NO_CONTACT
    )

    due_times: list[datetime] = []
    while row.status != "matched":
        assert row.status == "no_match"
        assert row.next_attempt_at is not None
        assert len(due_times) < 8, "the contact was never matched"
        now = row.next_attempt_at
        due_times.append(now)
        report = await harness.match(pair.hubspot, now=now)
        assert report.attempted == 1
        row = (await _rows(harness.sessions, pair.hubspot))[lead_id]
        if row.status == "no_match":
            assert now < created
            assert row.next_attempt_at == next_attempt_after(row.attempts, submitted, now)

    # 1 h, 6 h, 24 h and then one day: the fourth retry is the first after the contact exists.
    assert due_times == [
        first + timedelta(hours=1),
        first + timedelta(hours=7),
        first + timedelta(hours=31),
        first + timedelta(hours=55),
    ]
    assert due_times[-2] < created <= due_times[-1]
    matched_at = due_times[-1]
    assert row.attempts == 5
    assert (row.status, row.next_attempt_at, row.last_checked_at, row.reason) == (
        "matched",
        None,
        matched_at,
        None,
    )
    assert row.contact_read_status == "ok"
    assert row.contact_id is not None
    async with harness.sessions() as session:
        contact = await session.get(CrmContact, row.contact_id)
    assert contact is not None
    assert (contact.dataset_id, contact.hs_id, contact.email_sha256) == (
        pair.hubspot,
        "5100001",
        _digest(email),
    )
    # Every attempt re-fetched the response and asked HubSpot for exactly this one email.
    assert harness.platforms.refetch_calls == [[spec.response_id]] * 5
    assert harness.platforms.hubspot_calls == [[email.lower()]] * 5


async def test_not_due_rows_are_not_claimed(harness: Harness) -> None:
    """Late contacts: a run before a row's ``next_attempt_at`` sends no request for it.

    The retried row waits for its due time while a newly staged row in the same CRM dataset
    is attempted; a run with nothing due makes no LinkedIn or HubSpot request at all.
    """
    pair = await _live_pair(harness)
    submitted = T0 - timedelta(days=1)
    waiting = harness.platforms.add_lead(LeadSpec("due-0001", "waiting@example.com", submitted))
    fresh = harness.platforms.add_lead(
        LeadSpec("due-0002", "fresh@example.com", submitted + timedelta(minutes=8))
    )
    harness.platforms.add_contact(fresh.email, "5100002", created_at=submitted)
    ids = await _seed_leads(harness.sessions, pair.linkedin, [waiting, fresh])
    await _stage(harness.sessions, [ids[waiting.response_id]], pair.hubspot, now=submitted)
    first = submitted + timedelta(minutes=5)
    assert (await harness.match(pair.hubspot, now=first)).no_match == 1
    retry_row = (await _rows(harness.sessions, pair.hubspot))[ids[waiting.response_id]]
    assert retry_row.next_attempt_at == first + timedelta(hours=1)

    await _stage(
        harness.sessions,
        [ids[fresh.response_id]],
        pair.hubspot,
        now=submitted + timedelta(minutes=10),
    )
    harness.platforms.reset_calls()
    report = await harness.match(pair.hubspot, now=submitted + timedelta(minutes=30))
    assert report == MatchReport(attempted=1, matched=1, batches=1)
    assert harness.platforms.refetch_calls == [[fresh.response_id]]
    assert harness.platforms.hubspot_calls == [[fresh.email]]
    assert (await _rows(harness.sessions, pair.hubspot))[ids[waiting.response_id]] == retry_row

    harness.platforms.reset_calls()
    sent_before = (len(harness.linkedin_requests), len(harness.hubspot_requests))
    report = await harness.match(pair.hubspot, now=first + timedelta(minutes=59))
    assert report == MatchReport()
    assert (len(harness.linkedin_requests), len(harness.hubspot_requests)) == sent_before
    assert (await _rows(harness.sessions, pair.hubspot))[ids[waiting.response_id]] == retry_row


# ---------------------------------------------------------------------------------------------
# Transient failure (AAP 0.3.3 fallback table, "Transient")
# ---------------------------------------------------------------------------------------------


async def test_hubspot_5xx_marks_error_then_matches(harness: Harness) -> None:
    """Transient failure: a HubSpot 5xx leaves the row ``error``, then it is matched.

    HubSpot answers 503 to every attempt the retry policy makes, so the lookup fails: the
    row ends ``error`` with its reason and is due again one hour later, never simulated and
    with neither source's mode touched, and the exhausted error is returned in
    ``MatchReport.interrupted_by`` for the calling job to hand to ``on_error``. The next due
    run reaches a healthy portal and matches.
    """
    pair = await _live_pair(harness)
    email = "transient@example.com"
    spec = harness.platforms.add_lead(LeadSpec("5xx-0001", email, T0 - timedelta(hours=3)))
    harness.platforms.add_contact(email, "5100003", created_at=T0 - timedelta(days=30))
    lead_id = (await _seed_leads(harness.sessions, pair.linkedin, [spec]))[spec.response_id]
    await _stage(harness.sessions, [lead_id], pair.hubspot, now=T0 - timedelta(hours=3))

    async def connections() -> dict[str, tuple[str, str, str | None, int | None]]:
        """Each source's connection: mode, status, reason and active dataset."""
        async with harness.sessions() as session:
            rows = await session.scalars(select(SourceConnection))
            return {
                row.source: (row.mode, row.status, row.reason, row.active_dataset_id)
                for row in rows
            }

    before = await connections()
    harness.platforms.hubspot_status = 503
    report = await harness.match(pair.hubspot, now=T0)
    # The exhausted lookup is returned, not swallowed, for the calling job to hand to
    # ``on_error``; ``run_matching`` itself never touches a connection.
    (interruption,) = report.interrupted_by
    assert isinstance(interruption, TransientError)
    assert (interruption.source, interruption.http_status) == ("hubspot", 503)
    assert dataclasses.replace(report, interrupted_by=()) == MatchReport(
        attempted=1, errors=1, batches=1
    )
    row = (await _rows(harness.sessions, pair.hubspot))[lead_id]
    assert row == MatchRow(
        "error", 1, T0 + timedelta(hours=1), T0, None, None, REASON_LOOKUP_FAILED
    )
    # Every attempt of the retry policy was answered 503 before the lookup gave up.
    assert len(harness.platforms.hubspot_calls) == harness.controller.retry.attempts
    assert harness.controller.retry.attempts > 1
    assert await connections() == before
    assert before["hubspot"] == ("live", "ok", "seeded by the lead-matching tests", pair.hubspot)
    assert await _state(harness.sessions, pair.hubspot) == "active"

    harness.platforms.hubspot_status = None
    report = await harness.match(pair.hubspot, now=T0 + timedelta(hours=1))
    assert report == MatchReport(attempted=1, matched=1, batches=1)
    row = (await _rows(harness.sessions, pair.hubspot))[lead_id]
    assert (row.status, row.attempts, row.next_attempt_at, row.reason) == ("matched", 2, None, None)
    assert row.contact_id is not None
    assert row.contact_read_status == "ok"
    assert await connections() == before


# ---------------------------------------------------------------------------------------------
# Staged CRM recovery (AAP 0.3.3 "Staged matching on a CRM dataset change", "Readiness")
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedRecovery:
    """A live LinkedIn dataset matched in a simulated HubSpot primary, before a live build.

    Attributes:
        linkedin: The active live LinkedIn dataset.
        hubspot: The active simulated HubSpot primary every lead is matched in.
        in_range: Leads submitted inside the backfill range, newest first.
        out_of_range: Leads submitted before the backfill range.
        no_contact: The newest in-range lead, for whom the live portal has no contact.
        active_rows: The simulated dataset's ``lead_match`` rows as seeded.
    """

    linkedin: int
    hubspot: int
    in_range: tuple[int, ...]
    out_of_range: tuple[int, ...]
    no_contact: int
    active_rows: dict[int, MatchRow]


async def _staged_recovery(harness: Harness) -> StagedRecovery:
    """Seed the live leads and the simulated primary's matches of the staged-recovery cases.

    The simulated primary's contacts carry ids from the simulator's reserved range of seeded
    contacts that answer real lead emails, as the simulated portal would have created them.
    """
    sessions = harness.sessions
    linkedin = await _dataset(sessions, "linkedin", "live")
    hubspot = await _dataset(sessions, "hubspot", "simulated")
    await _linkedin_entities(sessions, linkedin, harness.platforms.account_id)
    inside = [
        LeadSpec(f"rec-in-{index:04d}", f"recovered.{index}@example.com", T0 - timedelta(days=age))
        for index, age in enumerate((3, 20, 60, 170))
    ]
    outside = [
        LeadSpec(f"rec-out-{index:04d}", f"archived.{index}@example.com", T0 - timedelta(days=age))
        for index, age in enumerate((181, 300))
    ]
    for spec in (*inside, *outside):
        harness.platforms.add_lead(spec)
    # The live portal knows every in-range person except the newest one, who is still well
    # inside the 30-day retry horizon.
    for index, spec in enumerate(inside[1:], start=1):
        harness.platforms.add_contact(spec.email, f"52{index:06d}", created_at=spec.submitted_at)
    ids = await _seed_leads(sessions, linkedin, (*inside, *outside))
    async with sessions() as session, session.begin():
        contacts = {
            spec.response_id: CrmContact(
                source="hubspot",
                dataset_id=hubspot,
                hs_id=str(HUBSPOT_HASHED_CONTACT_IDS.start + offset),
                email_sha256=_digest(spec.email),
                lifecycle_stage="lead",
                updated_at=T0 - timedelta(days=10),
            )
            for offset, spec in enumerate((*inside, *outside))
        }
        session.add_all(contacts.values())
        await session.flush()
        session.add_all(
            LeadMatch(
                lead_id=ids[response_id],
                crm_dataset_id=hubspot,
                status="matched",
                attempts=1,
                next_attempt_at=None,
                last_checked_at=T0 - timedelta(days=10),
                contact_id=contact.id,
                contact_read_status="ok",
            )
            for response_id, contact in contacts.items()
        )
    return StagedRecovery(
        linkedin=linkedin,
        hubspot=hubspot,
        in_range=tuple(ids[spec.response_id] for spec in inside),
        out_of_range=tuple(ids[spec.response_id] for spec in outside),
        no_contact=ids[inside[0].response_id],
        active_rows=await _rows(sessions, hubspot),
    )


async def _begin_live_build(harness: Harness, setup: StagedRecovery) -> int:
    """Begin a live HubSpot build and stage the active LinkedIn dataset's backfill-range leads."""
    building = await harness.manager.begin("hubspot", "live", reason="recovered")
    async with harness.sessions() as session, session.begin():
        inserted = await stage_rows(session, building, setup.linkedin, since=BACKFILL_START, now=T0)
    assert inserted == len(setup.in_range)
    return building


async def _new_live_linkedin(harness: Harness) -> tuple[int, dict[str, int]]:
    """Begin a live LinkedIn dataset and run its backfill lead sync against the platforms.

    LinkedIn adds three leads to the account first; the backfill then ingests every response
    of the backfill range, the earlier dataset's included. Returns the dataset and its leads
    by response id.
    """
    for index, age in enumerate((1, 2, 40)):
        spec = harness.platforms.add_lead(
            LeadSpec(
                f"rec-new-{index:04d}", f"newcomer.{index}@example.com", T0 - timedelta(days=age)
            )
        )
        if index != 2:
            harness.platforms.add_contact(
                spec.email, f"53{index:06d}", created_at=spec.submitted_at
            )
    dataset = await harness.manager.begin("linkedin", "live", reason="recovered")
    await _linkedin_entities(harness.sessions, dataset, harness.platforms.account_id)
    result = await harness.lead_sync(dataset)
    assert (result.status, result.error) == ("succeeded", None)
    leads = await _leads(harness.sessions, dataset)
    in_range = {
        spec.response_id
        for spec in harness.platforms.leads.values()
        if spec.submitted_at >= BACKFILL_START
    }
    assert set(leads) == in_range
    return dataset, {response_id: lead.id for response_id, lead in leads.items()}


async def test_building_dataset_stages_a_row_per_backfill_lead(harness: Harness) -> None:
    """Staged CRM recovery: the build gets one ``pending`` row per backfill-range lead.

    A live HubSpot dataset begun while the simulated one is active stages a row
    ``(lead, building dataset)`` for every lead of the active LinkedIn dataset submitted
    inside the backfill range, none for older leads; staging again inserts nothing, and the
    active dataset's matches are untouched.
    """
    setup = await _staged_recovery(harness)
    building = await _begin_live_build(harness, setup)

    rows = await _rows(harness.sessions, building)
    assert set(rows) == set(setup.in_range)
    assert set(rows).isdisjoint(setup.out_of_range)
    assert all(row == MatchRow("pending", 0, T0, None, None, None, None) for row in rows.values())
    async with harness.sessions() as session, session.begin():
        again = await stage_rows(session, building, setup.linkedin, since=BACKFILL_START, now=T0)
    assert again == 0
    assert await _rows(harness.sessions, building) == rows
    assert await _rows(harness.sessions, setup.hubspot) == setup.active_rows
    assert await _state(harness.sessions, building) == "building"
    assert await _state(harness.sessions, setup.hubspot) == "active"


async def test_active_matches_untouched_during_build(harness: Harness) -> None:
    """Staged CRM recovery: the active dataset's matches stay in use while the build matches.

    Matching the building dataset (and its retry pass) stores the live portal's contacts in
    the building dataset only; LinkedIn-attributed evidence keeps reading the simulated
    dataset, whose ``lead_match`` rows and contacts stay exactly as they were.
    """
    setup = await _staged_recovery(harness)
    building = await _begin_live_build(harness, setup)
    async with harness.sessions() as session:
        active_contacts = (
            await session.execute(
                select(CrmContact.id, CrmContact.hs_id, CrmContact.email_sha256).where(
                    CrmContact.dataset_id == setup.hubspot
                )
            )
        ).all()

    report = await harness.match(building, now=T0)
    assert report == MatchReport(
        attempted=len(setup.in_range), matched=len(setup.in_range) - 1, no_match=1, batches=1
    )
    await _record(harness.sessions, building, HUBSPOT_NAMES)
    retry = await harness.match(building, now=T0 + timedelta(hours=1))
    assert retry == MatchReport(attempted=1, no_match=1, batches=1)

    assert await harness.manager.effective_crm_dataset(setup.linkedin) == setup.hubspot
    assert await _rows(harness.sessions, setup.hubspot) == setup.active_rows
    building_rows = await _rows(harness.sessions, building)
    assert {row.status for row in building_rows.values()} == {"matched", "no_match"}
    assert building_rows[setup.no_contact].status == "no_match"
    async with harness.sessions() as session:
        assert (
            await session.execute(
                select(CrmContact.id, CrmContact.hs_id, CrmContact.email_sha256).where(
                    CrmContact.dataset_id == setup.hubspot
                )
            )
        ).all() == active_contacts
        matched_in = set(
            await session.scalars(
                select(CrmContact.dataset_id).where(
                    CrmContact.id.in_(
                        [row.contact_id for row in building_rows.values() if row.contact_id]
                    )
                )
            )
        )
    assert matched_in == {building}
    assert await _state(harness.sessions, building) == "building"


async def test_activation_refused_while_pending_or_unknown(harness: Harness) -> None:
    """Staged CRM recovery: activation is refused while a row is pending or freshness unknown.

    The build is not ready while any of its rows is ``pending``, including a lead staged
    after its first pass, and not while one ``hubspot.*`` freshness row is ``unknown`` once
    every row is attempted; ``activate`` refuses in both cases and leaves the simulated
    dataset active.
    """
    setup = await _staged_recovery(harness)
    building = await _begin_live_build(harness, setup)
    await _record(harness.sessions, building, HUBSPOT_NAMES)

    readiness = await harness.manager.readiness(building)
    assert not readiness.ready
    assert readiness.pending_matches == len(setup.in_range)
    assert (readiness.missing_freshness, readiness.unknown_freshness) == ((), ())
    with pytest.raises(DatasetStateError, match="not ready") as refused:
        await harness.manager.activate(building, reason="recovered")
    assert refused.value.readiness is not None
    assert refused.value.readiness.pending_matches == len(setup.in_range)

    await harness.match(building, now=T0)
    late = harness.platforms.add_lead(
        LeadSpec("rec-late-0001", "late.arrival@example.com", T0 - timedelta(hours=2))
    )
    late_id = (await _seed_leads(harness.sessions, setup.linkedin, [late]))[late.response_id]
    assert await _stage(harness.sessions, [late_id], building, now=T0) == 1
    readiness = await harness.manager.readiness(building)
    assert (readiness.ready, readiness.pending_matches) == (False, 1)
    with pytest.raises(DatasetStateError, match="not ready"):
        await harness.manager.activate(building, reason="recovered")

    assert (await harness.match(building, now=T0 + timedelta(minutes=1))).attempted == 1
    assert (await harness.manager.readiness(building)).ready
    async with harness.sessions() as session, session.begin():
        await mark_unknown(
            session,
            building,
            "hubspot.associations",
            reason="contacts matched after the association read",
        )
    readiness = await harness.manager.readiness(building)
    assert not readiness.ready
    assert readiness.pending_matches == 0
    assert readiness.unknown_freshness == ("hubspot.associations",)
    with pytest.raises(DatasetStateError, match="not ready") as refused:
        await harness.manager.activate(building, reason="recovered")
    assert refused.value.readiness is not None
    assert refused.value.readiness.unknown_freshness == ("hubspot.associations",)

    assert await _state(harness.sessions, building) == "building"
    assert await _state(harness.sessions, setup.hubspot) == "active"
    assert await harness.manager.effective_crm_dataset(setup.linkedin) == setup.hubspot
    assert await _rows(harness.sessions, setup.hubspot) == setup.active_rows


async def test_activation_swaps_matching_state_atomically(harness: Harness) -> None:
    """Staged CRM recovery: one activation swaps the matching state; no lead is reset.

    Once every building row is attempted and each ``hubspot.*`` freshness row is
    ``complete`` (or ``incomplete`` with a reason that is no unread work, from a successful
    latest attempt), the build is ready. One transaction activates it and retires the
    simulated dataset with its matches: the active dataset then has no ``pending`` row, its
    rows are exactly those its build wrote, and the retired rows still exist unchanged until
    the purge.
    """
    setup = await _staged_recovery(harness)
    building = await _begin_live_build(harness, setup)
    await harness.match(building, now=T0)
    await _record(harness.sessions, building, ("hubspot.contacts", "hubspot.deal_search"))
    await _record(
        harness.sessions, building, ("hubspot.associations",), reason="1 contact not read"
    )
    readiness = await harness.manager.readiness(building)
    assert readiness.ready, readiness.reasons
    before = await _rows(harness.sessions, building)
    assert "pending" not in {row.status for row in before.values()}

    activation = await harness.manager.activate(building, reason="recovered")

    assert (activation.source, activation.mode) == ("hubspot", "live")
    assert (activation.activated, activation.retired) == ((building,), (setup.hubspot,))
    async with harness.sessions() as session:
        live = await session.get(SourceDataset, building)
        retired = await session.get(SourceDataset, setup.hubspot)
        connection = await session.scalar(
            select(SourceConnection).where(SourceConnection.source == "hubspot")
        )
    assert live is not None
    assert retired is not None
    assert connection is not None
    assert (live.state, retired.state) == ("active", "retired")
    # Both state changes carry the one instant of the activation's single transaction.
    assert live.activated_at == retired.retired_at == T0
    assert (connection.mode, connection.active_dataset_id) == ("live", building)
    assert await harness.manager.effective_crm_dataset(setup.linkedin) == building
    after = await _rows(harness.sessions, building)
    assert after == before
    assert all(row.status != "pending" for row in after.values())
    assert await _rows(harness.sessions, setup.hubspot) == setup.active_rows


async def test_linkedin_activation_during_hubspot_build_stages_new_leads(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    make_world: WorldFactory,
    mock_transport_for: SimulatorFactory,
    contract_checked_transport: CheckedTransportFactory,
    recording_transport: RecordingFactory,
) -> None:
    """Staged CRM recovery: a LinkedIn activation during the build stages its new leads.

    A real live HubSpot build runs as recovery runs it
    (``SourceModeController.build_dataset`` with the ``recovered`` reason) while a new live
    LinkedIn dataset, already backfilled and attempted against HubSpot's active (simulated)
    primary, is still building. The live portal is the simulator's HubSpot transport, strict,
    with a contact for every real lead email; LinkedIn is the hand-written platform.

    The seam is the build's backfill (``jobs.backfill_dataset``). After the first run every
    backfill-range lead of the previous LinkedIn dataset is attempted in the build and it
    would be ready; the new LinkedIn dataset then activates. The build holds no row for the
    new leads, and readiness, which counts only rows that exist, still reads ready: only the
    build's own restaging stands between it and an activation without them. Before the
    second run the build has staged a ``pending`` row for every new lead and marked its
    contact and association freshness ``unknown``; the rerun attempts, matches and hydrates
    them, and only then does the build activate, retiring the simulated primary, whose
    matches stay unchanged. The test stages no row itself.
    """
    setup = await _staged_recovery(harness)
    newer, newer_leads = await _new_live_linkedin(harness)
    report = await harness.match(setup.hubspot, now=T0, hubspot="simulated", lead_dataset_id=newer)
    assert (report.attempted, report.pending_left) == (len(newer_leads), 0)
    await _record(harness.sessions, newer, LINKEDIN_NAMES)
    active_rows = await _rows(harness.sessions, setup.hubspot)
    newer_ids = set(newer_leads.values())
    assert set(active_rows) == set(setup.active_rows) | newer_ids
    previous_responses = {
        response_id
        for response_id, lead in (await _leads(harness.sessions, setup.linkedin)).items()
        if lead.id in setup.in_range
    }

    entry = SCENARIOS[STAGED_RECOVERY_SCENARIO]
    portal_world = make_world(
        dataclasses.replace(entry, spec=entry.spec.with_hubspot(live_lead_match_share=Decimal(1))),
        7,
        clock=_clock,
    )
    builder = _controller(
        harness.settings,
        harness.sessions,
        world=harness.controller.world,
        transports={
            "linkedin": recording_transport(
                contract_checked_transport("linkedin", harness.platforms.linkedin)
            ),
            "hubspot": recording_transport(
                mock_transport_for("hubspot", portal_world, strict=True)
            ),
        },
    )
    backfilled: list[int] = []
    backfill = jobs.backfill_dataset

    async def activate_linkedin(building: int) -> None:
        """After the first backfill the build is complete for the old leads; LinkedIn activates."""
        rows = await _rows(harness.sessions, building)
        assert set(rows) == set(setup.in_range)
        assert all((row.status, row.attempts) == ("matched", 1) for row in rows.values()), rows
        assert (await harness.manager.readiness(building)).ready
        activation = await harness.manager.activate(newer, reason=REASON_RECOVERED)
        assert (activation.activated, activation.retired) == ((newer,), (setup.linkedin,))
        assert set(await _rows(harness.sessions, building)).isdisjoint(newer_ids)
        # Readiness counts only staged rows, so it still reads ready without the new leads.
        assert (await harness.manager.readiness(building)).ready

    async def assert_restaged(building: int) -> None:
        """Before the rerun: every new lead is staged and the CRM evidence is withdrawn."""
        rows = await _rows(harness.sessions, building)
        staged = MatchRow("pending", 0, T0, None, None, None, None)
        assert {lead_id: rows.get(lead_id) for lead_id in newer_ids} == dict.fromkeys(
            newer_ids, staged
        )
        async with harness.sessions() as session:
            freshness = await load_freshness(session, building)
        withdrawn = ("hubspot.associations", "hubspot.contacts")
        for name in withdrawn:
            assert freshness[name].completeness == "unknown", name
            assert f"{len(newer_ids)} lead matches staged" in (freshness[name].reason or ""), name
        readiness = await harness.manager.readiness(building)
        assert not readiness.ready
        assert readiness.pending_matches == len(newer_ids)
        assert sorted(readiness.unknown_freshness) == list(withdrawn)

    async def at_seam(
        controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
    ) -> jobs.SourceSyncReport:
        """The build's backfill: LinkedIn activates after the first run; the second is checked."""
        assert controller is builder
        backfilled.append(dataset_id)
        if len(backfilled) == 1:
            assert await _state(harness.sessions, dataset_id) == "building"
            report = await backfill(controller, dataset_id, now=now)
            await activate_linkedin(dataset_id)
            return report
        assert backfilled == [dataset_id, dataset_id], f"unexpected backfills {backfilled}"
        await assert_restaged(dataset_id)
        return await backfill(controller, dataset_id, now=now)

    monkeypatch.setattr(jobs, "backfill_dataset", at_seam)
    harness.platforms.reset_calls()
    outcome = await builder.build_dataset("hubspot", "live", reason=REASON_RECOVERED)

    assert outcome.activated, outcome.reason
    building = outcome.dataset_id
    assert building is not None
    assert (outcome.source, outcome.mode, outcome.reason, outcome.retired) == (
        "hubspot",
        "live",
        REASON_RECOVERED,
        (setup.hubspot,),
    )
    assert backfilled == [building, building]
    states = {
        dataset_id: await _state(harness.sessions, dataset_id)
        for dataset_id in (building, setup.hubspot, setup.linkedin, newer)
    }
    assert states == {
        building: "active",
        setup.hubspot: "retired",
        setup.linkedin: "retired",
        newer: "active",
    }
    async with harness.sessions() as session:
        connection = await session.scalar(
            select(SourceConnection).where(SourceConnection.source == "hubspot")
        )
    assert connection is not None
    assert (connection.mode, connection.active_dataset_id) == ("live", building)
    assert await harness.manager.effective_crm_dataset(newer) == building

    # The rerun attempted every new lead against the live portal and matched it to the
    # portal's contact for its own email, stored in the build's dataset with its associations
    # read. The simulator's seeded contacts for real emails carry no email, so they are told
    # apart by id: the one the portal answers for the lead's email hash.
    rows = await _rows(harness.sessions, building)
    assert set(rows) == set(setup.in_range) | newer_ids
    for lead_id in newer_ids:
        row = rows[lead_id]
        assert (row.status, row.attempts, row.last_checked_at, row.contact_read_status) == (
            "matched",
            1,
            T0,
            "ok",
        ), lead_id
    async with harness.sessions() as session:
        contacts = {
            contact.id: (contact.dataset_id, contact.hs_id)
            for contact in await session.scalars(
                select(CrmContact).where(
                    CrmContact.id.in_([rows[lead_id].contact_id for lead_id in newer_ids])
                )
            )
        }
        freshness = await load_freshness(session, building)
    for lead in (await _leads(harness.sessions, newer)).values():
        contact_id = rows[lead.id].contact_id
        assert contact_id is not None
        assert lead.email_sha256 is not None
        expected = portal_world.hashed_contact_for(lead.email_sha256)
        assert expected is not None
        assert contacts[contact_id] == (building, expected.id), lead.external_id
    assert await harness.manager.unhydrated_matches(building) == 0
    crm_freshness = {name: row for name, row in freshness.items() if name.startswith("hubspot.")}
    assert set(HUBSPOT_NAMES) <= set(crm_freshness)
    for name, recorded in crm_freshness.items():
        assert recorded.completeness == "complete" or (
            recorded.completeness == "incomplete" and bool(recorded.reason)
        ), (name, recorded.completeness, recorded.reason)
    assert await _rows(harness.sessions, setup.hubspot) == active_rows
    # The first run re-fetched the previous dataset's leads, the rerun the new dataset's.
    assert [set(call) for call in harness.platforms.refetch_calls] == [
        previous_responses,
        set(newer_leads),
    ]


async def test_new_live_linkedin_dataset_staged_against_active_crm(harness: Harness) -> None:
    """Staged CRM recovery: a new live LinkedIn dataset is staged against HubSpot's active primary.

    The backfill lead sync of a live LinkedIn build stages every lead it stores in HubSpot's
    active primary; the build is not ready, and cannot activate, until each of those rows
    has been attempted, so LinkedIn-attributed CRM evidence is complete from its first run.
    """
    setup = await _staged_recovery(harness)
    newer, newer_leads = await _new_live_linkedin(harness)

    active_rows = await _rows(harness.sessions, setup.hubspot)
    for lead_id in newer_leads.values():
        assert active_rows[lead_id] == MatchRow("pending", 0, T0, None, None, None, None)
    assert {lead_id: active_rows[lead_id] for lead_id in setup.active_rows} == setup.active_rows
    await _record(harness.sessions, newer, LINKEDIN_NAMES)
    readiness = await harness.manager.readiness(newer)
    assert not readiness.ready
    assert (readiness.pending_matches, readiness.unstaged_leads) == (len(newer_leads), 0)
    with pytest.raises(DatasetStateError, match="not ready"):
        await harness.manager.activate(newer, reason="recovered")

    report = await harness.match(setup.hubspot, now=T0, hubspot="simulated", lead_dataset_id=newer)
    assert (report.attempted, report.pending_left) == (len(newer_leads), 0)
    active_rows = await _rows(harness.sessions, setup.hubspot)
    for lead_id in newer_leads.values():
        row = active_rows[lead_id]
        assert (row.status != "pending", row.attempts, row.last_checked_at) == (True, 1, T0)
    assert (await harness.manager.readiness(newer)).ready

    activation = await harness.manager.activate(newer, reason="recovered")
    assert (activation.activated, activation.retired) == ((newer,), (setup.linkedin,))
    assert await _state(harness.sessions, setup.hubspot) == "active"
    assert await harness.manager.effective_crm_dataset(newer) == setup.hubspot
    assert {
        lead_id: row
        for lead_id, row in (await _rows(harness.sessions, setup.hubspot)).items()
        if lead_id in setup.active_rows
    } == setup.active_rows


# ---------------------------------------------------------------------------------------------
# Versioned forms and terminal cases (AAP 0.3.3 "Form identity", "Unretrievable emails")
# ---------------------------------------------------------------------------------------------


async def _form_versions(sessions: Sessions, dataset_id: int) -> dict[int, list[str]]:
    """The stored version maps of the hand-written form: email question ids by version."""
    async with sessions() as session:
        result = await session.execute(
            select(LeadFormVersion.version, LeadFormVersion.email_question_ids)
            .join(LeadForm, LeadForm.id == LeadFormVersion.lead_form_id)
            .where(LeadForm.dataset_id == dataset_id, LeadForm.canonical_urn == FORM_URN)
        )
        return {version: list(question_ids) for version, question_ids in result}


async def test_form_map_mismatch_is_no_email(harness: Harness) -> None:
    """Versioned forms: answered question ids missing from the decoration end ``no_email``.

    The lead sync ingests a version-2 response that answers a question its own decorated
    questions do not list. The decoration does not describe that response's version, so the
    lead is stored without an email hash and its ``lead_match`` row is ``no_email`` "form map
    mismatch", terminal: the stored version-1 map, which could decode it, is never used, the
    mismatched decoration is never learned, and no matching run re-fetches the lead.
    """
    pair = await _live_pair(harness)
    good = harness.platforms.add_lead(
        LeadSpec("form-0001", "good.version@example.com", T0 - timedelta(days=4))
    )
    mismatched = harness.platforms.add_lead(
        LeadSpec(
            "form-0002",
            "mismatched.version@example.com",
            T0 - timedelta(days=3),
            version=2,
            decoration="mismatch",
        )
    )
    result = await harness.lead_sync(pair.linkedin)
    assert (result.status, result.error) == ("succeeded", None)

    leads = await _leads(harness.sessions, pair.linkedin)
    assert set(leads) == {good.response_id, mismatched.response_id}
    stored = leads[mismatched.response_id]
    assert (stored.email_sha256, stored.lead_form_urn, stored.lead_form_version) == (
        None,
        FORM_URN,
        2,
    )
    assert leads[good.response_id].email_sha256 == _digest(good.email)
    rows = await _rows(harness.sessions, pair.hubspot)
    terminal = MatchRow("no_email", 0, None, T0, None, None, REASON_FORM_MAP_MISMATCH)
    assert rows[stored.id] == terminal
    assert rows[leads[good.response_id].id] == MatchRow("pending", 0, T0, None, None, None, None)
    assert await _form_versions(harness.sessions, pair.linkedin) == {1: [str(EMAIL_QID)]}

    harness.platforms.add_contact(good.email, "5400001", created_at=T0 - timedelta(days=4))
    report = await harness.match(pair.hubspot, now=T0 + timedelta(minutes=5))
    assert report == MatchReport(attempted=1, matched=1, batches=1)
    assert harness.platforms.refetch_calls == [[good.response_id]]
    report = await harness.match(pair.hubspot, now=T0 + MATCH_HORIZON)
    assert report == MatchReport()
    assert (await _rows(harness.sessions, pair.hubspot))[stored.id] == terminal
    assert all(mismatched.response_id not in call for call in harness.platforms.refetch_calls)


async def test_unknown_form_version_is_no_email(harness: Harness) -> None:
    """Terminal cases: a version with no decoration and no stored map ends ``no_email``.

    The response names form version 3, carries no ``form`` decoration, and no
    ``lead_form_version`` map exists for version 3 (only version 1's does), so the lead is
    stored without an email hash and its row is ``no_email`` "unknown form version" with no
    ``next_attempt_at``.
    """
    pair = await _live_pair(harness)
    unknown = harness.platforms.add_lead(
        LeadSpec(
            "form-0003",
            "unknown.version@example.com",
            T0 - timedelta(days=2),
            version=3,
            decoration="none",
        )
    )
    result = await harness.lead_sync(pair.linkedin)
    assert (result.status, result.error) == ("succeeded", None)

    stored = (await _leads(harness.sessions, pair.linkedin))[unknown.response_id]
    assert (stored.email_sha256, stored.lead_form_version) == (None, 3)
    terminal = MatchRow("no_email", 0, None, T0, None, None, REASON_UNKNOWN_FORM_VERSION)
    assert (await _rows(harness.sessions, pair.hubspot))[stored.id] == terminal
    assert await _form_versions(harness.sessions, pair.linkedin) == {1: [str(EMAIL_QID)]}

    for later in (T0 + timedelta(hours=1), T0 + timedelta(days=7)):
        assert await harness.match(pair.hubspot, now=later) == MatchReport()
    assert (await _rows(harness.sessions, pair.hubspot))[stored.id] == terminal
    assert harness.platforms.refetch_calls == []
    assert harness.platforms.hubspot_calls == []


@pytest.mark.parametrize("failure", ["listed_in_errors", "absent_from_results"])
async def test_email_no_longer_retrievable_is_error(harness: Harness, failure: str) -> None:
    """Terminal cases: an email LinkedIn no longer returns ends ``error`` and stays unresolved.

    The batch re-fetch lists the lead's id under ``errors`` or leaves it out of ``results``.
    Its email cannot be decoded, so the row ends ``error`` "email no longer retrievable"
    with no retry, is never ``matched`` (it counts as unresolved), and HubSpot is asked only
    for the lead in the same batch that LinkedIn did return.
    """
    pair = await _live_pair(harness)
    gone = harness.platforms.add_lead(
        LeadSpec("gone-0001", "deleted.response@example.com", T0 - timedelta(days=1))
    )
    kept = harness.platforms.add_lead(
        LeadSpec("gone-0002", "kept.response@example.com", T0 - timedelta(days=1))
    )
    harness.platforms.add_contact(gone.email, "5400002", created_at=T0 - timedelta(days=9))
    harness.platforms.add_contact(kept.email, "5400003", created_at=T0 - timedelta(days=9))
    if failure == "listed_in_errors":
        harness.platforms.refetch_errors.add(gone.response_id)
    else:
        harness.platforms.refetch_omitted.add(gone.response_id)
    ids = await _seed_leads(harness.sessions, pair.linkedin, [gone, kept])
    await _stage(harness.sessions, list(ids.values()), pair.hubspot, now=T0 - timedelta(hours=1))

    report = await harness.match(pair.hubspot, now=T0)
    assert report == MatchReport(attempted=2, matched=1, errors=1, batches=1)
    rows = await _rows(harness.sessions, pair.hubspot)
    terminal = MatchRow("error", 1, None, T0, None, None, REASON_EMAIL_UNRETRIEVABLE)
    assert rows[ids[gone.response_id]] == terminal
    assert rows[ids[gone.response_id]].status != "matched"
    assert rows[ids[kept.response_id]].status == "matched"
    assert harness.platforms.refetch_calls == [[gone.response_id, kept.response_id]]
    assert harness.platforms.emails_sent() == [kept.email]

    assert await harness.match(pair.hubspot, now=T0 + timedelta(days=2)) == MatchReport()
    assert (await _rows(harness.sessions, pair.hubspot))[ids[gone.response_id]] == terminal


async def test_test_leads_never_stored(harness: Harness) -> None:
    """Terminal cases: test leads are dropped at ingest; no ``lead`` and no ``lead_match`` row.

    ``limitedToTestLeads=false`` returns test and real leads together; only the real ones
    are stored and staged for matching.
    """
    pair = await _live_pair(harness)
    real = [
        harness.platforms.add_lead(
            LeadSpec(f"real-{index:04d}", f"real.{index}@example.com", T0 - timedelta(days=index))
        )
        for index in (1, 2)
    ]
    tests = [
        harness.platforms.add_lead(
            LeadSpec(
                f"test-{index:04d}",
                f"test.{index}@example.com",
                T0 - timedelta(days=index),
                test_lead=True,
            )
        )
        for index in (1, 2)
    ]
    result = await harness.lead_sync(pair.linkedin)
    assert (result.status, result.error) == ("succeeded", None)
    assert harness.platforms.listing_calls == 1

    leads = await _leads(harness.sessions, pair.linkedin)
    assert set(leads) == {spec.response_id for spec in real}
    assert {spec.response_id for spec in tests}.isdisjoint(leads)
    rows = await _rows(harness.sessions, pair.hubspot)
    assert set(rows) == {lead.id for lead in leads.values()}
    async with harness.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Lead)) == len(real)
        assert await session.scalar(select(func.count()).select_from(LeadMatch)) == len(real)


async def _ad_account(sessions: Sessions, dataset_id: int, account_id: str) -> int:
    """Store a LinkedIn dataset's ad account alone, as its first successful entity sync would.

    Leads of the account can then be placed: a campaign no entity sync stored yet gets a
    stand-in under the account. Returns the account's id.
    """
    async with sessions() as session, session.begin():
        account = AdAccount(
            source="linkedin",
            dataset_id=dataset_id,
            external_id=account_id,
            name=f"Acme LinkedIn {account_id}",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        return account.id


async def _seed_cursor(sessions: Sessions, dataset_id: int) -> None:
    """Record a complete ``linkedin.leads`` success at :data:`LEAD_CURSOR`: the lead cursor."""
    outcome = FreshnessOutcome.success(
        LEAD_CURSOR,
        covered_from=BACKFILL_START.date(),
        covered_through=LEAD_CURSOR.date(),
        complete=True,
    )
    async with sessions() as session, session.begin():
        await record_freshness(session, dataset_id, "linkedin.leads", outcome)


async def _leads_freshness(sessions: Sessions, dataset_id: int) -> DatasetFreshness:
    """The committed ``linkedin.leads`` freshness row of one LinkedIn dataset."""
    async with sessions() as session:
        return (await load_freshness(session, dataset_id))["linkedin.leads"]


async def _lead_runs(sessions: Sessions) -> list[tuple[str, str | None, datetime]]:
    """The lead job's ``linkedin.leads`` ``sync_run`` rows as (status, error, start), in order."""
    async with sessions() as session:
        result = await session.execute(
            select(SyncRun.status, SyncRun.error, SyncRun.started_at)
            .where(
                SyncRun.source == "linkedin",
                SyncRun.job == "leads",
                SyncRun.dataset == "linkedin.leads",
            )
            .order_by(SyncRun.id)
        )
        return [(status, error, started_at) for status, error, started_at in result]


def _listing_starts(harness: Harness) -> list[int]:
    """The ``submittedAtTimeRange`` start (epoch ms) of every lead listing, in call order."""
    starts: list[int] = []
    for request in harness.linkedin_requests:
        value = request.url.params.get("submittedAtTimeRange")
        if value is None:
            continue
        bounds = _TIME_RANGE.match(value)
        assert bounds is not None, value
        starts.append(int(bounds["start"]))
    return starts


async def test_unplaceable_leads_fail_the_attempt_and_keep_the_cursor(harness: Harness) -> None:
    """Terminal cases: leads with no stored ad account fail the attempt and are not lost.

    The live LinkedIn dataset's ``linkedin.leads`` cursor stands at :data:`LEAD_CURSOR`, and
    no entity sync has stored its ad account or campaign yet. The incremental lead sync
    reads the two leads submitted since the cursor and cannot place them, as a campaign
    stand-in needs its account and no account is invented. It records a failed attempt
    naming them: no ``lead`` and no ``lead_match`` row is written, ``last_success_at``, the
    coverage and the completeness stand, and the ``sync_run`` row closes failed with the
    reason. Once the account is stored, a later run re-reads the same range from the kept
    cursor, stores both leads under a campaign stand-in, stages each in the HubSpot dataset
    and records the success that moves the cursor on.
    """
    linkedin = await _dataset(harness.sessions, "linkedin", "live")
    hubspot = await _dataset(harness.sessions, "hubspot", "live")
    await _seed_cursor(harness.sessions, linkedin)
    specs = [
        harness.platforms.add_lead(
            LeadSpec(
                f"late-{index:04d}",
                f"late.{index}@example.com",
                LEAD_CURSOR + timedelta(hours=index),
            )
        )
        for index in (1, 2)
    ]
    reason = "2 leads not stored: campaign and ad account not stored yet"
    range_start = _ms(LEAD_CURSOR - LEAD_OVERLAP)

    result = await harness.lead_sync(linkedin, backfill=False)
    assert (result.status, result.error, result.rows) == ("failed", reason, 0)
    assert result.freshness["linkedin.leads"] == FreshnessOutcome.failure(T0, reason=reason)
    assert _listing_starts(harness) == [range_start]
    assert await _leads(harness.sessions, linkedin) == {}
    assert await _rows(harness.sessions, hubspot) == {}
    row = await _leads_freshness(harness.sessions, linkedin)
    assert (
        row.last_attempt_at,
        row.last_success_at,
        row.covered_from,
        row.covered_through,
        row.completeness,
        row.reason,
    ) == (T0, LEAD_CURSOR, BACKFILL_START.date(), LEAD_CURSOR.date(), "complete", reason)
    assert await _lead_runs(harness.sessions) == [("failed", reason, T0)]

    await _ad_account(harness.sessions, linkedin, harness.platforms.account_id)
    later = T0 + timedelta(hours=3)
    result = await harness.lead_sync(linkedin, now=later, backfill=False)
    assert (result.status, result.error, result.rows) == ("succeeded", None, len(specs))
    assert _listing_starts(harness) == [range_start, range_start]
    leads = await _leads(harness.sessions, linkedin)
    assert set(leads) == {spec.response_id for spec in specs}
    assert all(leads[spec.response_id].email_sha256 == _digest(spec.email) for spec in specs)
    async with harness.sessions() as session:
        placed = await session.scalars(
            select(Campaign.external_id).where(
                Campaign.id.in_({lead.campaign_id for lead in leads.values()})
            )
        )
        assert set(placed) == {CAMPAIGN_ID}
    pending = MatchRow("pending", 0, later, None, None, None, None)
    assert await _rows(harness.sessions, hubspot) == {lead.id: pending for lead in leads.values()}
    row = await _leads_freshness(harness.sessions, linkedin)
    assert (
        row.last_attempt_at,
        row.last_success_at,
        row.covered_through,
        row.completeness,
        row.reason,
    ) == (later, later, later.date(), "complete", None)
    # A ``sync_run`` row starts when its job started, on the controller's clock (still at T0
    # here); the run's ``now`` (``later``) anchors the range and the freshness row above.
    assert await _lead_runs(harness.sessions) == [
        ("failed", reason, T0),
        ("succeeded", None, T0),
    ]


async def test_placed_leads_stored_while_an_unplaceable_one_keeps_the_cursor(
    harness: Harness,
) -> None:
    """Terminal cases: one run stores and stages the placeable leads and fails for the rest.

    The configured account's entities are stored; a second, newly configured account has
    had no successful entity sync. Its lead fails the attempt and keeps the cursor, while
    the first account's two leads are stored and staged in the same run. Once the second
    account is stored, the next run re-reads both accounts from the kept cursor, stores the
    missing lead and stages it once; the first two keep their rows unchanged.
    """
    pair = await _live_pair(harness)
    await _seed_cursor(harness.sessions, pair.linkedin)
    placed = [
        harness.platforms.add_lead(
            LeadSpec(
                f"placed-{index:04d}",
                f"placed.{index}@example.com",
                LEAD_CURSOR + timedelta(hours=index),
            )
        )
        for index in (1, 2)
    ]
    unplaced = harness.platforms.add_lead(
        LeadSpec(
            "unplaced-0001",
            "unplaced.1@example.com",
            LEAD_CURSOR + timedelta(minutes=30),
            account_id=NEW_ACCOUNT_ID,
            campaign_urn=NEW_CAMPAIGN_URN,
        )
    )
    reason = "1 lead not stored: campaign and ad account not stored yet"
    range_start = _ms(LEAD_CURSOR - LEAD_OVERLAP)
    accounts = (NEW_ACCOUNT_ID,)

    result = await harness.lead_sync(pair.linkedin, backfill=False, extra_accounts=accounts)
    assert (result.status, result.error, result.rows) == ("failed", reason, len(placed))
    assert _listing_starts(harness) == [range_start, range_start]
    leads = await _leads(harness.sessions, pair.linkedin)
    assert set(leads) == {spec.response_id for spec in placed}
    first = {lead.id: MatchRow("pending", 0, T0, None, None, None, None) for lead in leads.values()}
    assert await _rows(harness.sessions, pair.hubspot) == first
    row = await _leads_freshness(harness.sessions, pair.linkedin)
    assert (row.last_attempt_at, row.last_success_at, row.completeness, row.reason) == (
        T0,
        LEAD_CURSOR,
        "complete",
        reason,
    )
    assert await _lead_runs(harness.sessions) == [("failed", reason, T0)]

    await _ad_account(harness.sessions, pair.linkedin, NEW_ACCOUNT_ID)
    later = T0 + timedelta(hours=3)
    result = await harness.lead_sync(
        pair.linkedin, now=later, backfill=False, extra_accounts=accounts
    )
    assert (result.status, result.error, result.rows) == ("succeeded", None, len(placed) + 1)
    assert _listing_starts(harness) == [range_start] * 4
    leads = await _leads(harness.sessions, pair.linkedin)
    assert set(leads) == {spec.response_id for spec in (*placed, unplaced)}
    async with harness.sessions() as session:
        campaign = await session.scalar(
            select(Campaign.external_id).where(
                Campaign.id == leads[unplaced.response_id].campaign_id
            )
        )
    assert campaign == NEW_CAMPAIGN_URN.rsplit(":", 1)[1]
    staged = MatchRow("pending", 0, later, None, None, None, None)
    assert await _rows(harness.sessions, pair.hubspot) == {
        **first,
        leads[unplaced.response_id].id: staged,
    }
    row = await _leads_freshness(harness.sessions, pair.linkedin)
    assert (row.last_attempt_at, row.last_success_at, row.completeness, row.reason) == (
        later,
        later,
        "complete",
        None,
    )


# ---------------------------------------------------------------------------------------------
# Restart safety (AAP 0.3.3 "Restart safety")
# ---------------------------------------------------------------------------------------------


async def _bulk_leads(harness: Harness, pair: LivePair, count: int, *, prefix: str) -> list[int]:
    """Seed ``count`` leads the portal knows, staged ``pending`` at :data:`T0`; ids ascending."""
    specs = []
    for index in range(count):
        spec = harness.platforms.add_lead(
            LeadSpec(
                f"{prefix}-{index:04d}", f"{prefix}.{index:04d}@example.com", T0 - timedelta(days=1)
            )
        )
        harness.platforms.add_contact(
            spec.email, f"55{index:06d}", created_at=T0 - timedelta(days=1)
        )
        specs.append(spec)
    ids = await _seed_leads(harness.sessions, pair.linkedin, specs)
    lead_ids = sorted(ids.values())
    assert await _stage(harness.sessions, lead_ids, pair.hubspot, now=T0) == count
    return lead_ids


async def test_crash_mid_batch_loses_no_rows(harness: Harness) -> None:
    """Restart safety: a crash mid-batch loses no row.

    With more due rows than one batch holds, the HubSpot handler fails with a non-connector
    exception on the second batch. The exception propagates unchanged; the first batch stays
    committed with its final statuses; every row of the crashed batch is still ``pending``
    with its attempts and due time as staged, never half-written; and a rerun completes them.
    """
    pair = await _live_pair(harness)
    lead_ids = await _bulk_leads(harness, pair, BATCH_SIZE + 50, prefix="crash")
    first_batch, second_batch = lead_ids[:BATCH_SIZE], lead_ids[BATCH_SIZE:]
    harness.platforms.hubspot_crash_on_call = 2

    with pytest.raises(HandlerCrashError, match="crashed mid-batch"):
        await harness.match(pair.hubspot, now=T0)

    rows = await _rows(harness.sessions, pair.hubspot)
    for lead_id in first_batch:
        row = rows[lead_id]
        assert (row.status, row.attempts, row.next_attempt_at, row.last_checked_at) == (
            "matched",
            1,
            None,
            T0,
        ), lead_id
        assert (row.contact_id is not None, row.contact_read_status) == (True, "ok"), lead_id
    for lead_id in second_batch:
        assert rows[lead_id] == MatchRow("pending", 0, T0, None, None, None, None), lead_id
    assert [len(call) for call in harness.platforms.hubspot_calls] == [BATCH_SIZE, 50]

    harness.platforms.hubspot_crash_on_call = None
    report = await harness.match(pair.hubspot, now=T0)
    assert report == MatchReport(attempted=50, matched=50, batches=1)
    rows = await _rows(harness.sessions, pair.hubspot)
    assert {row.status for row in rows.values()} == {"matched"}
    assert {row.attempts for row in rows.values()} == {1}
    async with harness.sessions() as session:
        stored = await session.scalar(
            select(func.count())
            .select_from(CrmContact)
            .where(CrmContact.dataset_id == pair.hubspot)
        )
    assert stored == len(lead_ids)


async def test_concurrent_runs_claim_disjoint_rows(harness: Harness) -> None:
    """Restart safety: two concurrent runs claim disjoint rows (``FOR UPDATE SKIP LOCKED``).

    Two matching runs on one CRM dataset start together. The first batch re-fetch waits
    until the other run's re-fetch is in flight too, so both batches are claimed and leased
    at the same time. Each row is claimed by exactly one run: HubSpot receives every email
    exactly once and every row ends attempted exactly once.
    """
    pair = await _live_pair(harness)
    lead_ids = await _bulk_leads(harness, pair, 2 * BATCH_SIZE, prefix="race")
    both_in_flight = anyio.Event()

    async def rendezvous() -> None:
        """Hold each of the first two re-fetches until both runs have one in flight."""
        if len(harness.platforms.refetch_calls) >= 2:
            both_in_flight.set()
        await both_in_flight.wait()

    harness.platforms.refetch_gate = rendezvous
    reports: list[MatchReport] = []

    async def run() -> None:
        """One matching run on the shared CRM dataset."""
        reports.append(await harness.match(pair.hubspot, now=T0))

    with anyio.fail_after(60):
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            group.start_soon(run)

    assert len(reports) == 2
    assert sorted(report.attempted for report in reports) == [BATCH_SIZE, BATCH_SIZE]
    assert sum(report.matched for report in reports) == len(lead_ids)
    claimed = [set(call) for call in harness.platforms.refetch_calls]
    assert len(claimed) == 2
    assert claimed[0].isdisjoint(claimed[1])
    sent = harness.platforms.emails_sent()
    assert len(sent) == len(set(sent)) == len(lead_ids)
    rows = await _rows(harness.sessions, pair.hubspot)
    assert set(rows) == set(lead_ids)
    assert {(row.status, row.attempts) for row in rows.values()} == {("matched", 1)}


# ---------------------------------------------------------------------------------------------
# No plaintext (AAP 0.1.1 "Lead emails are persisted only as SHA-256 hashes"; AAP 0.3.2)
# ---------------------------------------------------------------------------------------------

_HEX_DIGEST: Final = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one table scan read and found.

    Attributes:
        columns: Every ``(table, column)`` scanned.
        hits: ``(table, column, why)`` of every value holding a known email or anything shaped
            like one; the value itself is not kept.
    """

    columns: frozenset[tuple[str, str]]
    hits: tuple[tuple[str, str, str], ...]


def _scan(connection: Connection, emails: Iterable[str]) -> ScanResult:
    """Read every text-like column of every table and report values that hold an email.

    Scanned: ``text``, ``character varying``, ``character``, ``jsonb`` and ``text[]`` columns
    of schema ``public``, each value cast to text. Only ``app_user.email``, the admin's login
    identity, is excluded.
    """
    wanted = {email.strip().lower() for email in emails}
    rows = connection.execute(
        text(
            "SELECT c.table_name, c.column_name FROM information_schema.columns AS c "
            "JOIN information_schema.tables AS t "
            "ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
            "AND (c.data_type = ANY(:types) OR (c.data_type = 'ARRAY' AND c.udt_name = '_text')) "
            "ORDER BY c.table_name, c.ordinal_position"
        ),
        {"types": list(SCANNED_TYPES)},
    ).all()
    quote = connection.dialect.identifier_preparer.quote
    columns: set[tuple[str, str]] = set()
    hits: list[tuple[str, str, str]] = []
    for table, column in rows:
        if (table, column) in SCAN_EXCLUDED:
            continue
        columns.add((table, column))
        # Identifiers come from the catalogue and are quoted; no value is interpolated.
        statement = text(
            f"SELECT {quote(column)}::text FROM {quote(table)} "  # noqa: S608
            f"WHERE {quote(column)} IS NOT NULL"
        )
        for value in connection.execute(statement).scalars():
            lowered = str(value).lower()
            if any(email in lowered for email in wanted):
                hits.append((table, column, "known lead email"))
            elif EMAIL_PATTERN.search(lowered):
                hits.append((table, column, "email-shaped value"))
    return ScanResult(frozenset(columns), tuple(hits))


def _assert_no_plaintext(engine: Engine, emails: Sequence[str]) -> ScanResult:
    """Scan the whole database for ``emails`` after proving the scan detects one.

    A canary address written into ``source_dataset.reason`` inside a transaction that is
    rolled back must be reported, so a scan that silently reads nothing cannot pass.
    """
    assert emails, "the scan needs the emails the world holds"
    canary = "canary.check@example.com"
    with engine.connect() as connection:
        with connection.begin() as transaction:
            connection.execute(
                text("UPDATE source_dataset SET reason = :reason"), {"reason": canary}
            )
            probe = _scan(connection, [canary])
            transaction.rollback()
        assert ("source_dataset", "reason", "known lead email") in probe.hits
        result = _scan(connection, emails)
    for required in (
        ("lead", "external_id"),
        ("lead", "email_sha256"),
        ("lead_match", "reason"),
        ("crm_contact", "email_sha256"),
        ("lead_form_version", "email_question_ids"),
        ("entity_snapshot", "payload"),
        ("sync_run", "error"),
    ):
        assert required in result.columns, required
    assert result.hits == ()
    return result


async def _assert_hashes(
    sessions: Sessions, linkedin_dataset_id: int, email_of: Mapping[str, str]
) -> int:
    """Check every stored hash: lead hashes equal SHA-256 of the normalised world email.

    A lead without a hash must be a ``no_email`` lead. Every stored contact hash is a
    lowercase SHA-256 digest. Returns the number of leads checked.
    """
    leads = await _leads(sessions, linkedin_dataset_id)
    assert leads
    async with sessions() as session:
        no_email = set(
            await session.scalars(
                select(LeadMatch.lead_id).where(LeadMatch.status == "no_email").distinct()
            )
        )
        contact_hashes = list(
            await session.scalars(
                select(CrmContact.email_sha256).where(CrmContact.email_sha256.is_not(None))
            )
        )
    for response_id, lead in leads.items():
        if lead.email_sha256 is None:
            assert lead.id in no_email, response_id
            continue
        assert _HEX_DIGEST.fullmatch(lead.email_sha256), response_id
        assert lead.email_sha256 == _digest(email_of[response_id]), response_id
    assert contact_hashes
    assert all(digest is not None and _HEX_DIGEST.fullmatch(digest) for digest in contact_hashes)
    return len(leads)


def _world_emails(world: SimulatedWorld) -> dict[str, str]:
    """Every response of the simulated world, test leads included: email by response id."""
    emails: dict[str, str] = {}
    for account_id in world.linkedin_account_ids:
        for response in world.linkedin_lead_responses(account_id, 0, _ms(T0 + timedelta(days=1))):
            emails[response.id] = response.email
    return emails


async def _simulated_lead_sync(
    sessions: Sessions, make_world: WorldFactory
) -> tuple[int, dict[str, str]]:
    """Build, backfill and activate a simulated LinkedIn dataset with its companion CRM.

    The build runs the full simulated lead sync, stages every lead in the companion and
    matches it there against the simulated portal before the pair activates. Returns the
    LinkedIn dataset and the world's emails by response id.
    """
    settings = get_settings()
    assert not settings.has_credentials("linkedin")
    world = make_world("default", clock=_clock)
    controller = _controller(settings, sessions, world=world)
    outcome = await controller.build_dataset(
        "linkedin", "simulated", reason="credentials not configured"
    )
    assert outcome.activated, outcome
    dataset_id = outcome.dataset_id
    assert dataset_id is not None
    companion = await controller.datasets.effective_crm_dataset(dataset_id)
    assert companion is not None
    assert companion == outcome.companion_id
    rows = await _rows(sessions, companion)
    assert rows
    assert "pending" not in {row.status for row in rows.values()}
    assert "matched" in {row.status for row in rows.values()}
    emails = _world_emails(world)
    assert all(email.endswith(f"@{SIM_EMAIL_DOMAIN}") for email in emails.values())
    return dataset_id, emails


async def _live_lead_sync(harness: Harness) -> tuple[int, dict[str, str]]:
    """Ingest and match live leads: matched, not found, form mismatch and test leads.

    Returns the LinkedIn dataset and every email the platforms hold, by response id.
    """
    pair = await _live_pair(harness)
    specs = [
        harness.platforms.add_lead(
            LeadSpec(
                f"live-{index:04d}", f"Live.Person{index}@Example.com", T0 - timedelta(days=index)
            )
        )
        for index in range(1, 6)
    ]
    for index, spec in enumerate(specs[:3]):
        harness.platforms.add_contact(spec.email, f"56{index:06d}", created_at=spec.submitted_at)
    extra = [
        harness.platforms.add_lead(
            LeadSpec(
                "live-mismatch",
                "mismatch.person@example.com",
                T0 - timedelta(days=2),
                version=2,
                decoration="mismatch",
            )
        ),
        harness.platforms.add_lead(
            LeadSpec("live-test", "test.person@example.com", T0 - timedelta(days=2), test_lead=True)
        ),
    ]
    result = await harness.lead_sync(pair.linkedin)
    assert (result.status, result.error) == ("succeeded", None)
    report = await harness.match(pair.hubspot, now=T0 + timedelta(minutes=5))
    assert report == MatchReport(attempted=len(specs), matched=3, no_match=2, batches=1)
    return pair.linkedin, {spec.response_id: spec.email for spec in (*specs, *extra)}


@pytest.mark.parametrize("variant", ["simulated", "live_hubspot"])
async def test_no_plaintext_email_anywhere(
    variant: str,
    db: Sessions,
    sync_engine: Engine,
    live_credentials: CredentialsFactory,
    contract_checked_transport: CheckedTransportFactory,
    recording_transport: RecordingFactory,
    make_world: WorldFactory,
) -> None:
    """No plaintext: no table holds a lead email after lead sync and matching.

    ``simulated``: the full simulated LinkedIn build (lead sync, companion CRM sync and
    matching against the simulated portal). ``live_hubspot``: live leads ingested by the
    lead sync and matched against a live HubSpot portal that stores the contacts found.
    Every text, character, jsonb and text-array column of every table is read: none holds
    an email of the world (test leads and undecodable responses included) or anything
    shaped like one, ``app_user.email`` aside. Each stored lead hash is the SHA-256 of the
    world's normalised email for that response.
    """
    if variant == "simulated":
        linkedin, email_of = await _simulated_lead_sync(db, make_world)
    else:
        harness = _build_harness(
            db, live_credentials, contract_checked_transport, recording_transport, make_world
        )
        linkedin, email_of = await _live_lead_sync(harness)
    assert await _assert_hashes(db, linkedin, email_of) > 0
    _assert_no_plaintext(sync_engine, list(email_of.values()))


async def test_no_plaintext_email_in_logs(harness: Harness, captured_logs: Any) -> None:
    """No plaintext: the logs written during lead sync and matching hold no lead email.

    Captured at debug level, so request logging is included, across ingest, the batch
    re-fetch and a HubSpot lookup that both finds contacts and reports ``OBJECT_NOT_FOUND``.
    """
    captured_logs.configure(level="DEBUG")
    _, email_of = await _live_lead_sync(harness)
    records = captured_logs()
    events = {record.get("event") for record in records}
    assert {"sync.leads.stored", "sync.lead_match.batch", "hubspot.batch_read"} <= events
    logged = captured_logs.text.lower()
    for email in email_of.values():
        assert email.strip().lower() not in logged
    assert EMAIL_PATTERN.search(logged) is None
