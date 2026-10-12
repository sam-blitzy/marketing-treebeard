"""Integration tests for the claim lease of ``ads_engine.sync.lead_match.run_matching``.

AAP 0.3.3 ("Lead-to-contact matching", "Restart safety"): due ``lead_match`` rows are
claimed with ``FOR UPDATE SKIP LOCKED`` and committed per batch, so a crash mid-job loses no
state, and each email exists only in memory for its one HubSpot call. A batch claims and
leases its rows in one short transaction, calls LinkedIn and HubSpot with no transaction,
row lock or pooled connection held, and writes the outcomes in a second short transaction
that verifies the lease.

Covered:

* while the LinkedIn re-fetch and the HubSpot lookup are in flight the claimed rows are
  unlocked (another connection takes them ``FOR UPDATE NOWAIT``), the matcher's engine has no
  connection checked out and no backend outside ``idle``, and the rows carry one lease;
* a crash mid-batch (an exception in the HubSpot or the LinkedIn handler, or HubSpot's 401 as
  ``AccessDenied``) propagates unchanged, the first batch stays committed, the crashed batch's
  rows are ``pending`` with unchanged attempts and their previous ``next_attempt_at``, and a
  rerun with the same ``now`` completes them;
* a cancelled batch hands its lease back the same way;
* two concurrent runs claim disjoint rows and send no email twice;
* a row whose lease was lost during the I/O is not overwritten, nor is its contact written,
  and the batch log warns; a batch left with no held row writes no contact, so the newer
  contact and match another run stored meanwhile stand;
* a lease abandoned without release (the release's database error is tolerated) keeps its
  rows from being claimed until ``now`` reaches the lease instant;
* a CRM dataset retired (or purged) while its batch is in flight is never written by the
  release, whether the batch then fails its write with ``DatasetNotWritable``, is cancelled
  or crashes: the original exception or cancellation propagates unmasked, the rows stay
  exactly as the lease left them, and the skip is logged instead of a release; a release
  that meets an uncommitted retirement waits for it and then skips.

No network: both platforms answer through ``httpx.MockTransport`` handlers whose requests and
responses are validated against ``tests/contracts/linkedin/lead_form_responses_batch.json``
and ``tests/contracts/hubspot/objects_batch_read.json``.

The tests run against the PostgreSQL 18 test database named by ``TEST_DATABASE_URL``
(falling back to ``DATABASE_URL``). The module creates a private schema, builds every table
of the model metadata in it, points every connection's ``search_path`` at it alone, and
drops it with ``CASCADE`` at teardown, as ``test_sync_datasets.py`` does. An unreachable
database fails the tests; it never skips them.
"""

import asyncio
import copy
import functools
import hashlib
import json
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, delete, insert, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy.schema import CreateSchema, DropSchema
from structlog.testing import capture_logs

import ads_engine.db.models  # noqa: F401 - registers every table on Base.metadata
from ads_engine.connectors.errors import AccessDenied
from ads_engine.connectors.http import RetryPolicy, build_client
from ads_engine.connectors.hubspot.client import HubSpotClient, RateLimiter
from ads_engine.connectors.hubspot.parser import ContactPropertyMap
from ads_engine.connectors.linkedin.auth import SIMULATED_TOKEN, StaticTokenProvider
from ads_engine.connectors.linkedin.client import LinkedInClient
from ads_engine.db import repositories
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import AdAccount, Campaign
from ads_engine.db.models.crm import CrmContact, Lead, LeadMatch
from ads_engine.db.models.ops import SourceDataset
from ads_engine.settings import Settings
from ads_engine.simulation.payloads import (
    Contract,
    load_contract,
    validate_request,
    validate_response,
)
from ads_engine.sync import lead_match
from ads_engine.sync.lead_match import BATCH_SIZE, CLAIM_LEASE, MatchClients, MatchReport

type Sessions = async_sessionmaker[AsyncSession]
type Hook = Callable[[list[str]], Awaitable[httpx.Response | None]]

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)
"""Every run's ``now`` unless a test says otherwise."""

SUBMITTED = T0 - timedelta(days=1)
"""When every seeded lead was submitted."""

LEASE_SLACK = timedelta(minutes=2)
"""Upper bound of a test run's elapsed time, which the lease instant adds to ``now``."""

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"

ACCOUNT_ID = "508012345"
ACCOUNT_URN = f"urn:li:sponsoredAccount:{ACCOUNT_ID}"
CAMPAIGN_ID = "614000001"
FORM_ID = 7
FORM_VERSION = 1
FIRST_NAME_QID = 1
EMAIL_QID = 2

SAMPLE_LEAD_ID = "aaaabbbb-0000-cccc-1111-dddd2222eeee-5"
"""The response id of the batch contract's documented sample."""


async def _no_sleep(_seconds: float) -> None:
    """Skip the retry policy's backoff."""


NO_SLEEP_RETRY = RetryPolicy(attempts=2, sleep=_no_sleep)


class HandlerCrashError(RuntimeError):
    """A failure inside a platform handler that is not a connector error."""


# ---------------------------------------------------------------------------------------
# Contracts and wire bodies
# ---------------------------------------------------------------------------------------


@functools.cache
def _contract(source: str, surface: str) -> Contract:
    """Load one surface's contract from this repository's contracts directory."""
    if source == "linkedin":
        return load_contract("linkedin", surface, root=CONTRACTS)
    return load_contract("hubspot", surface, root=CONTRACTS)


def _linkedin_contract() -> Contract:
    """The LinkedIn batch re-fetch contract."""
    return _contract("linkedin", "lead_form_responses_batch")


def _hubspot_contract() -> Contract:
    """The HubSpot objects batch read contract."""
    return _contract("hubspot", "objects_batch_read")


@functools.cache
def _documented_element() -> dict[str, Any]:
    """The batch contract's documented sample response."""
    raw = json.loads(
        (CONTRACTS / "linkedin" / "lead_form_responses_batch.json").read_text(encoding="utf-8")
    )
    element: dict[str, Any] = raw["response"]["documented_sample"]["results"][SAMPLE_LEAD_ID]
    return element


def _text_answer(question_id: int, answer: str) -> dict[str, Any]:
    """A text answer, shaped as the documented sample's first answer."""
    return {"questionId": question_id, "answerDetails": {"textQuestionAnswer": {"answer": answer}}}


def _element(response_id: str, email: str) -> dict[str, Any]:
    """One response under the ``LEAD_FIELDS`` projection, built from the documented sample.

    Under the projection LinkedIn omits ``submitter``, ``associatedEntity`` and
    ``leadMetadata`` and adds the ``form`` decoration of the response's own version, which
    asks ``FIRST_NAME`` and ``EMAIL``.
    """
    element = copy.deepcopy(_documented_element())
    for key in ("submitter", "associatedEntity", "leadMetadata"):
        element.pop(key, None)
    element["id"] = response_id
    element["testLead"] = False
    element["submittedAt"] = int(SUBMITTED.timestamp() * 1000)
    element["owner"]["sponsoredAccount"] = ACCOUNT_URN
    element["versionedLeadGenFormUrn"] = (
        f"urn:li:versionedLeadGenForm:(urn:li:leadGenForm:{FORM_ID},{FORM_VERSION})"
    )
    element["leadMetadataInfo"]["sponsoredLeadMetadataInfo"]["campaign"]["id"] = (
        f"urn:li:sponsoredCampaign:{CAMPAIGN_ID}"
    )
    element["formResponse"] = {
        "answers": [_text_answer(FIRST_NAME_QID, "Ada"), _text_answer(EMAIL_QID, email)]
    }
    element["form"] = {
        "id": FORM_ID,
        "name": "White paper form",
        "content": {
            "questions": [
                {
                    "questionId": FIRST_NAME_QID,
                    "name": "firstName",
                    "predefinedField": "FIRST_NAME",
                },
                {"questionId": EMAIL_QID, "name": "email", "predefinedField": "EMAIL"},
            ]
        },
    }
    return element


def _contact(hs_id: str, email: str) -> dict[str, Any]:
    """One contact of an objects batch read."""
    return {
        "id": hs_id,
        "properties": {"email": email, "lifecyclestage": "lead", "hs_object_id": hs_id},
        "createdAt": "2026-09-01T00:00:00Z",
        "updatedAt": "2026-09-30T12:00:00Z",
        "archived": False,
    }


def _requested_ids(request: httpx.Request) -> list[str]:
    """Return the decoded ids of a batch re-fetch's ``ids=List(...)``."""
    value = request.url.params["ids"]
    assert value.startswith("List(")
    assert value.endswith(")")
    return [unquote(item) for item in value[len("List(") : -1].split(",")]


# ---------------------------------------------------------------------------------------
# The seeded world and the mock platforms
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """One live LinkedIn dataset's leads staged ``pending`` in one live HubSpot dataset.

    Attributes:
        crm_dataset_id: The HubSpot dataset the rows belong to.
        lead_ids: Every seeded lead.
        email_of: Each LinkedIn response id's email.
        lead_of_response: Each response id's lead.
        lead_of_email: Each email's lead.
        seeded_due: Each lead's ``next_attempt_at`` as seeded.
    """

    crm_dataset_id: int
    lead_ids: tuple[int, ...]
    email_of: dict[str, str]
    lead_of_response: dict[str, int]
    lead_of_email: dict[str, int]
    seeded_due: dict[int, datetime | None]

    def leads_for_emails(self, emails: Iterable[str]) -> set[int]:
        """Return the leads behind HubSpot inputs."""
        return {self.lead_of_email[email] for email in emails}

    def leads_for_ids(self, response_ids: Iterable[str]) -> set[int]:
        """Return the leads behind LinkedIn response ids."""
        return {self.lead_of_response[response_id] for response_id in response_ids}


@dataclass
class Platforms:
    """The two mock platforms of one test, recording what each was sent.

    A hook runs while its call is in flight; it may return a response to send instead.
    """

    world: World
    linkedin_hook: Hook | None = None
    hubspot_hook: Hook | None = None
    linkedin_calls: list[list[str]] = field(default_factory=list)
    hubspot_calls: list[list[str]] = field(default_factory=list)

    async def linkedin(self, request: httpx.Request) -> httpx.Response:
        """Answer a batch re-fetch with every requested response."""
        validate_request(_linkedin_contract(), request)
        ids = _requested_ids(request)
        self.linkedin_calls.append(ids)
        if self.linkedin_hook is not None:
            replaced = await self.linkedin_hook(ids)
            if replaced is not None:
                return replaced
        body: dict[str, Any] = {
            "results": {
                response_id: _element(response_id, self.world.email_of[response_id])
                for response_id in ids
            },
            "statuses": {},
            "errors": {},
        }
        validate_response(_linkedin_contract(), body)
        return httpx.Response(200, json=body)

    async def hubspot(self, request: httpx.Request) -> httpx.Response:
        """Answer a contacts batch read by email with one contact per input."""
        validate_request(_hubspot_contract(), request)
        payload = json.loads(request.content)
        assert payload["idProperty"] == "email"
        emails = [item["id"] for item in payload["inputs"]]
        self.hubspot_calls.append(emails)
        if self.hubspot_hook is not None:
            replaced = await self.hubspot_hook(emails)
            if replaced is not None:
                return replaced
        body: dict[str, Any] = {
            "status": "COMPLETE",
            "results": [
                _contact(_hs_id(self.world.lead_of_email[email]), email) for email in emails
            ],
            "startedAt": "2026-10-01T06:00:00Z",
            "completedAt": "2026-10-01T06:00:01Z",
        }
        validate_response(_hubspot_contract(), body)
        return httpx.Response(200, json=body)

    def emails_sent(self) -> list[str]:
        """Every email HubSpot received, in call order, repeats included."""
        return [email for call in self.hubspot_calls for email in call]


@asynccontextmanager
async def _clients(platforms: Platforms) -> AsyncIterator[MatchClients]:
    """Yield live LinkedIn and HubSpot clients answering through ``platforms``."""
    settings = Settings()
    async with (
        build_client("linkedin", httpx.MockTransport(platforms.linkedin), settings=settings) as li,
        build_client("hubspot", httpx.MockTransport(platforms.hubspot), settings=settings) as hs,
    ):
        linkedin = LinkedInClient(li, StaticTokenProvider(SIMULATED_TOKEN), retry=NO_SLEEP_RETRY)
        hubspot = HubSpotClient(
            hs,
            SecretStr("-".join(("lead", "match", "claims"))),
            live=True,
            retry=NO_SLEEP_RETRY,
            limiter=RateLimiter(1_000.0),
        )
        yield MatchClients(
            linkedin_for=lambda _lead_dataset_id: linkedin,
            hubspot=hubspot,
            contact_properties=ContactPropertyMap(),
        )


# ---------------------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    """Run the async tests on asyncio only."""
    return "asyncio"


def _database_url() -> URL:
    """Return the test database URL on the psycopg 3 driver, or fail the test."""
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        pytest.fail(
            "TEST_DATABASE_URL (or DATABASE_URL) must name the PostgreSQL 18 test database "
            "for the lead-match claim tests"
        )
    try:
        url = make_url(raw)
    except ArgumentError:
        pytest.fail("TEST_DATABASE_URL is not a valid SQLAlchemy database URL")
    if url.get_backend_name() != "postgresql":
        pytest.fail(f"TEST_DATABASE_URL must be a PostgreSQL URL, not {url.get_backend_name()}")
    return url.set(drivername="postgresql+psycopg")


def _connect_args(schema: str, application_name: str | None = None) -> dict[str, str]:
    """Pin a connection to ``schema`` alone, in UTC, optionally naming its application."""
    args = {"options": f"-c search_path={schema} -c TimeZone=UTC"}
    if application_name is not None:
        args["application_name"] = application_name
    return args


@pytest.fixture(scope="module")
def database() -> Iterator[tuple[URL, str]]:
    """Create a private schema holding every table of the metadata; drop it afterwards."""
    url = _database_url()
    schema = f"lead_match_claims_{uuid.uuid4().hex}"
    engine = create_engine(url, poolclass=NullPool, connect_args=_connect_args(schema))
    try:
        with engine.begin() as connection:
            connection.execute(CreateSchema(schema))
            Base.metadata.create_all(connection)
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.fail(
            "PostgreSQL test database unreachable or unusable at "
            f"{url.render_as_string(hide_password=True)}: {exc}"
        )
    try:
        yield url, schema
    finally:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


@dataclass(frozen=True)
class Env:
    """The matcher's pooled engine and an independent probe engine on the same schema.

    Attributes:
        sessions: The matcher's session factory (SQLAlchemy defaults, so nothing relies on
            objects staying readable after a commit).
        matcher: The matcher's engine, a queue pool whose connections carry ``app``.
        probe: A ``NullPool`` engine for seeding and for observing the matcher meanwhile.
        app: The matcher connections' ``application_name`` in ``pg_stat_activity``.
    """

    sessions: Sessions
    matcher: AsyncEngine
    probe: AsyncEngine
    app: str


@pytest.fixture
async def env(database: tuple[URL, str]) -> AsyncIterator[Env]:
    """Yield the engines on empty dataset tables of the private schema."""
    url, schema = database
    app = f"lm_claims_{uuid.uuid4().hex[:16]}"
    matcher = create_async_engine(
        url, pool_size=4, max_overflow=4, connect_args=_connect_args(schema, app)
    )
    probe = create_async_engine(
        url, poolclass=NullPool, connect_args=_connect_args(schema, f"{app}_probe")
    )
    try:
        async with probe.begin() as connection:
            await connection.execute(text("TRUNCATE source_dataset CASCADE"))
        yield Env(async_sessionmaker(matcher), matcher, probe, app)
    finally:
        await matcher.dispose()
        await probe.dispose()


async def _seed(env: Env, count: int) -> World:
    """Commit ``count`` live leads, each with a ``pending`` row in a live HubSpot dataset.

    Even-numbered rows have no ``next_attempt_at``; odd-numbered ones are due at distinct
    past instants, so a released lease must restore each row's own value.
    """
    async with async_sessionmaker(env.probe)() as session, session.begin():
        linkedin = SourceDataset(source="linkedin", mode="live", state="active", role="primary")
        hubspot = SourceDataset(source="hubspot", mode="live", state="active", role="primary")
        session.add_all([linkedin, hubspot])
        await session.flush()
        account = AdAccount(
            source="linkedin",
            dataset_id=linkedin.id,
            external_id=ACCOUNT_ID,
            name="Acme LinkedIn",
            currency="USD",
            timezone="UTC",
        )
        session.add(account)
        await session.flush()
        campaign = Campaign(
            source="linkedin",
            dataset_id=linkedin.id,
            account_id=account.id,
            external_id=CAMPAIGN_ID,
            name="White paper A",
            status="active",
            raw_status="ACTIVE",
            objective_or_channel="LEAD_GENERATION",
            daily_budget=Decimal("25.00"),
        )
        session.add(campaign)
        await session.flush()
        emails = [f"lead.{index:04d}@example.com" for index in range(count)]
        response_ids = [f"claims-{index:04d}" for index in range(count)]
        leads = [
            Lead(
                source="linkedin",
                dataset_id=linkedin.id,
                external_id=response_ids[index],
                submitted_at=SUBMITTED,
                campaign_id=campaign.id,
                creative_id=None,
                lead_form_urn=f"urn:li:leadGenForm:{FORM_ID}",
                lead_form_version=FORM_VERSION,
                email_sha256=hashlib.sha256(emails[index].encode()).hexdigest(),
            )
            for index in range(count)
        ]
        session.add_all(leads)
        await session.flush()
        due: dict[int, datetime | None] = {
            lead.id: None if index % 2 == 0 else T0 - timedelta(minutes=index)
            for index, lead in enumerate(leads)
        }
        session.add_all(
            LeadMatch(
                lead_id=lead.id,
                crm_dataset_id=hubspot.id,
                status="pending",
                attempts=0,
                next_attempt_at=due[lead.id],
            )
            for lead in leads
        )
        lead_ids = tuple(lead.id for lead in leads)
        crm_dataset_id = hubspot.id
    return World(
        crm_dataset_id=crm_dataset_id,
        lead_ids=lead_ids,
        email_of=dict(zip(response_ids, emails, strict=True)),
        lead_of_response=dict(zip(response_ids, lead_ids, strict=True)),
        lead_of_email=dict(zip(emails, lead_ids, strict=True)),
        seeded_due=due,
    )


@dataclass(frozen=True)
class Row:
    """One ``lead_match`` row as committed."""

    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_checked_at: datetime | None
    contact_id: int | None
    reason: str | None


async def _rows(env: Env, world: World) -> dict[int, Row]:
    """Read every row of the world's CRM dataset through the probe engine."""
    async with env.probe.connect() as connection:
        result = await connection.execute(
            select(
                LeadMatch.lead_id,
                LeadMatch.status,
                LeadMatch.attempts,
                LeadMatch.next_attempt_at,
                LeadMatch.last_checked_at,
                LeadMatch.contact_id,
                LeadMatch.reason,
            ).where(LeadMatch.crm_dataset_id == world.crm_dataset_id)
        )
        return {lead_id: Row(*values) for lead_id, *values in result}


def _assert_matched(rows: dict[int, Row], leads: Iterable[int]) -> None:
    """Every given lead was attempted once at ``T0`` and matched."""
    for lead_id in leads:
        row = rows[lead_id]
        assert (row.status, row.attempts, row.next_attempt_at) == ("matched", 1, None), lead_id
        assert row.last_checked_at == T0
        assert row.contact_id is not None


def _assert_untouched(rows: dict[int, Row], world: World, leads: Iterable[int]) -> None:
    """Every given lead's row is exactly as seeded: pending, never attempted, same due time."""
    for lead_id in leads:
        row = rows[lead_id]
        assert row == Row("pending", 0, world.seeded_due[lead_id], None, None, None), lead_id


@dataclass(frozen=True)
class Snapshot:
    """What another connection saw while one platform call was in flight.

    Attributes:
        surface: ``linkedin`` or ``hubspot``.
        claimed: The leads of the call.
        unlocked: Whether ``FOR UPDATE NOWAIT`` on every row of the CRM dataset succeeded.
        checked_out: Connections of the matcher's pool checked out.
        busy_backends: Matcher backends in any state other than ``idle`` (``active`` or
            ``idle in transaction``).
        claimed_rows: The claimed leads' (status, attempts, next_attempt_at).
    """

    surface: str
    claimed: frozenset[int]
    unlocked: bool
    checked_out: int
    busy_backends: int
    claimed_rows: dict[int, tuple[str, int, datetime | None]]


async def _snapshot(env: Env, world: World, surface: str, claimed: set[int]) -> Snapshot:
    """Observe locks, the matcher's connections and the claimed rows from the probe."""
    pool = env.matcher.pool
    assert isinstance(pool, QueuePool)
    async with env.probe.connect() as connection:
        try:
            await connection.execute(
                text(
                    "SELECT lead_id FROM lead_match WHERE crm_dataset_id = :crm FOR UPDATE NOWAIT"
                ),
                {"crm": world.crm_dataset_id},
            )
            unlocked = True
        except OperationalError:
            unlocked = False
        await connection.rollback()
        busy = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = :app AND state IS DISTINCT FROM 'idle'"
            ),
            {"app": env.app},
        )
        result = await connection.execute(
            select(
                LeadMatch.lead_id, LeadMatch.status, LeadMatch.attempts, LeadMatch.next_attempt_at
            ).where(
                LeadMatch.crm_dataset_id == world.crm_dataset_id,
                LeadMatch.lead_id.in_(sorted(claimed)),
            )
        )
        claimed_rows = {
            lead_id: (status, attempts, due) for lead_id, status, attempts, due in result
        }
        await connection.rollback()
    return Snapshot(
        surface=surface,
        claimed=frozenset(claimed),
        unlocked=unlocked,
        checked_out=pool.checkedout(),
        busy_backends=int(busy or 0),
        claimed_rows=claimed_rows,
    )


def _lease_of(rows: Iterable[tuple[str, int, datetime | None]]) -> datetime:
    """Return the one lease instant the rows carry; they must still be pending and unattempted."""
    listed = list(rows)
    assert {(status, attempts) for status, attempts, _due in listed} == {("pending", 0)}
    leases = {due for _status, _attempts, due in listed}
    assert len(leases) == 1, leases
    (lease,) = leases
    assert lease is not None
    assert T0 + CLAIM_LEASE <= lease <= T0 + CLAIM_LEASE + LEASE_SLACK
    return lease


def _events(logs: Sequence[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    """Return the captured log entries of one event."""
    return [entry for entry in logs if entry["event"] == event]


def _hs_id(lead_id: int) -> str:
    """The HubSpot id the mock portal gives the contact of a lead's email."""
    return f"90{lead_id}"


@dataclass(frozen=True)
class StoredContact:
    """One ``crm_contact`` row as committed, with the transaction that last wrote it.

    Attributes:
        id: The row's id.
        lifecycle_stage: The stored ``lifecyclestage``.
        ql_at: The stored QL date.
        updated_at: The stored ``updatedAt``.
        xmin: PostgreSQL's ``xmin`` of the row: an upsert changes it even when every value
            stays the same.
    """

    id: int
    lifecycle_stage: str | None
    ql_at: datetime | None
    updated_at: datetime | None
    xmin: str


async def _contacts(env: Env, world: World) -> dict[str, StoredContact]:
    """Read every contact of the world's CRM dataset through the probe engine, by HubSpot id."""
    async with env.probe.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT hs_id, id, lifecycle_stage, ql_at, updated_at, xmin::text "
                "FROM crm_contact WHERE dataset_id = :crm"
            ),
            {"crm": world.crm_dataset_id},
        )
        return {hs_id: StoredContact(*values) for hs_id, *values in result}


# ---------------------------------------------------------------------------------------
# The lease constant
# ---------------------------------------------------------------------------------------


def test_claim_lease_is_public_and_outlasts_a_batch_of_retried_calls() -> None:
    """Two calls of three 60 s attempts with two 120 s waits each fit inside the lease."""
    assert "CLAIM_LEASE" in lead_match.__all__
    assert 2 * (3 * timedelta(seconds=60) + 2 * timedelta(seconds=120)) < CLAIM_LEASE


# ---------------------------------------------------------------------------------------
# (a) No lock, connection or transaction is held across the platform calls
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_lock_or_connection_is_held_while_the_platforms_are_called(env: Env) -> None:
    world = await _seed(env, BATCH_SIZE + 50)
    platforms = Platforms(world)
    snapshots: list[Snapshot] = []

    async def on_linkedin(ids: list[str]) -> None:
        snapshots.append(await _snapshot(env, world, "linkedin", world.leads_for_ids(ids)))

    async def on_hubspot(emails: list[str]) -> None:
        snapshots.append(await _snapshot(env, world, "hubspot", world.leads_for_emails(emails)))

    platforms.linkedin_hook = on_linkedin
    platforms.hubspot_hook = on_hubspot
    async with _clients(platforms) as clients:
        report = await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    assert [snapshot.surface for snapshot in snapshots] == [
        "linkedin",
        "hubspot",
        "linkedin",
        "hubspot",
    ]
    for snapshot in snapshots:
        assert snapshot.unlocked, f"claimed rows were locked during the {snapshot.surface} call"
        assert snapshot.checked_out == 0, snapshot
        assert snapshot.busy_backends == 0, snapshot
        assert set(snapshot.claimed_rows) == snapshot.claimed
    first, second = snapshots[0], snapshots[2]
    assert snapshots[1].claimed == first.claimed
    assert snapshots[3].claimed == second.claimed
    assert (len(first.claimed), len(second.claimed)) == (BATCH_SIZE, 50)
    assert first.claimed.isdisjoint(second.claimed)
    first_lease = _lease_of(first.claimed_rows.values())
    assert _lease_of(snapshots[1].claimed_rows.values()) == first_lease
    # The run's elapsed time moves a later batch's lease forward, although ``now`` is fixed.
    assert _lease_of(second.claimed_rows.values()) > first_lease

    assert report == MatchReport(attempted=BATCH_SIZE + 50, matched=BATCH_SIZE + 50, batches=2)
    _assert_matched(await _rows(env, world), world.lead_ids)
    assert Counter(platforms.emails_sent()) == Counter(list(world.lead_of_email))


# ---------------------------------------------------------------------------------------
# (b) A crash mid-batch loses no state; a cancellation releases the lease too
# ---------------------------------------------------------------------------------------


def _on_second(calls: list[list[str]], outcome: Callable[[], httpx.Response]) -> Hook:
    """Return a hook that answers the second call of a platform with ``outcome()``."""

    async def hook(_inputs: list[str]) -> httpx.Response | None:
        return outcome() if len(calls) == 2 else None

    return hook


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["hubspot_crash", "hubspot_denied", "linkedin_crash"])
async def test_a_crash_mid_batch_loses_no_state(env: Env, failure: str) -> None:
    world = await _seed(env, BATCH_SIZE + 50)
    platforms = Platforms(world)
    crash = HandlerCrashError(f"{failure} on the second batch")

    def raise_crash() -> httpx.Response:
        raise crash

    def denied() -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "status": "error",
                "message": "Authentication credentials not found.",
                "category": "INVALID_AUTHENTICATION",
            },
        )

    expected: type[BaseException] = HandlerCrashError
    if failure == "hubspot_crash":
        platforms.hubspot_hook = _on_second(platforms.hubspot_calls, raise_crash)
    elif failure == "hubspot_denied":
        platforms.hubspot_hook = _on_second(platforms.hubspot_calls, denied)
        expected = AccessDenied
    else:
        platforms.linkedin_hook = _on_second(platforms.linkedin_calls, raise_crash)

    async with _clients(platforms) as clients:
        with capture_logs() as logs, pytest.raises(expected) as raised:
            await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)
        if expected is HandlerCrashError:
            assert raised.value is crash

        committed = world.leads_for_emails(platforms.hubspot_calls[0])
        crashed = set(world.lead_ids) - committed
        assert (len(committed), len(crashed)) == (BATCH_SIZE, 50)
        rows = await _rows(env, world)
        _assert_matched(rows, committed)
        _assert_untouched(rows, world, crashed)
        released = _events(logs, "sync.lead_match.released")
        assert [(entry["claimed"], entry["released"]) for entry in released] == [(50, 50)]

        # A rerun at the same instant finds the crashed batch due again and completes it.
        platforms.hubspot_hook = None
        platforms.linkedin_hook = None
        sent_before = len(platforms.hubspot_calls)
        report = await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    assert report == MatchReport(attempted=50, matched=50, batches=1)
    rerun_emails = [email for call in platforms.hubspot_calls[sent_before:] for email in call]
    assert world.leads_for_emails(rerun_emails) == crashed
    _assert_matched(await _rows(env, world), world.lead_ids)


@pytest.mark.anyio
async def test_a_cancelled_batch_hands_its_lease_back(env: Env) -> None:
    world = await _seed(env, BATCH_SIZE + 50)
    platforms = Platforms(world)
    in_flight = asyncio.Event()
    never = asyncio.Event()

    async def hang_on_second(_emails: list[str]) -> None:
        if len(platforms.hubspot_calls) == 2:
            in_flight.set()
            await never.wait()

    platforms.hubspot_hook = hang_on_second
    async with _clients(platforms) as clients:
        task = asyncio.create_task(
            lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)
        )
        await asyncio.wait_for(in_flight.wait(), timeout=60)
        hung = world.leads_for_emails(platforms.hubspot_calls[1])
        leased = await _snapshot(env, world, "hubspot", hung)
        assert leased.unlocked
        _lease_of(leased.claimed_rows.values())
        with capture_logs() as logs:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        rows = await _rows(env, world)
        _assert_matched(rows, set(world.lead_ids) - hung)
        _assert_untouched(rows, world, hung)
        released = _events(logs, "sync.lead_match.released")
        assert [(entry["claimed"], entry["released"]) for entry in released] == [(50, 50)]

        platforms.hubspot_hook = None
        report = await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    assert report == MatchReport(attempted=50, matched=50, batches=1)
    _assert_matched(await _rows(env, world), world.lead_ids)


# ---------------------------------------------------------------------------------------
# (c) Concurrent runs claim disjoint rows
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_runs_claim_disjoint_rows_and_send_no_email_twice(env: Env) -> None:
    world = await _seed(env, 2 * BATCH_SIZE + 50)
    platforms = Platforms(world)
    both_in_flight = asyncio.Event()

    async def rendezvous(_ids: list[str]) -> None:
        # The first re-fetch of each run waits for the other's: both batches are claimed
        # and in flight at the same time.
        if len(platforms.linkedin_calls) <= 2:
            if len(platforms.linkedin_calls) == 2:
                both_in_flight.set()
            await asyncio.wait_for(both_in_flight.wait(), timeout=60)

    platforms.linkedin_hook = rendezvous
    async with _clients(platforms) as clients:
        reports = await asyncio.gather(
            lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0),
            lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0),
        )

    assert both_in_flight.is_set()
    assert set(platforms.linkedin_calls[0]).isdisjoint(platforms.linkedin_calls[1])
    requested = Counter(response_id for call in platforms.linkedin_calls for response_id in call)
    assert set(requested.values()) == {1}
    assert len(requested) == 2 * BATCH_SIZE + 50
    sent = Counter(platforms.emails_sent())
    assert set(sent.values()) == {1}, "an email was sent to HubSpot twice"
    assert set(sent) == set(world.lead_of_email)
    assert sum(report.attempted for report in reports) == 2 * BATCH_SIZE + 50
    assert all(report.attempted >= BATCH_SIZE for report in reports)
    _assert_matched(await _rows(env, world), world.lead_ids)


# ---------------------------------------------------------------------------------------
# (d) A lost lease is never overwritten
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_row_whose_lease_was_lost_is_not_overwritten(env: Env) -> None:
    world = await _seed(env, 3)
    re_leased, written, kept = world.lead_ids
    other_lease: list[datetime] = []
    other_write = Row(
        "no_match", 1, T0 + timedelta(hours=2), T0 + timedelta(minutes=40), None, "another run"
    )

    async def lose_two_leases(_emails: list[str]) -> None:
        # While HubSpot is called, one row's lease expires and another run re-leases it,
        # and a third run re-claims and writes another row.
        async with env.probe.begin() as connection:
            lease = await connection.scalar(
                select(LeadMatch.next_attempt_at).where(
                    LeadMatch.crm_dataset_id == world.crm_dataset_id,
                    LeadMatch.lead_id == re_leased,
                )
            )
            assert lease is not None
            other_lease.append(lease + timedelta(minutes=1))
            await connection.execute(
                update(LeadMatch)
                .where(
                    LeadMatch.crm_dataset_id == world.crm_dataset_id,
                    LeadMatch.lead_id == re_leased,
                )
                .values(next_attempt_at=other_lease[0])
            )
            await connection.execute(
                update(LeadMatch)
                .where(
                    LeadMatch.crm_dataset_id == world.crm_dataset_id,
                    LeadMatch.lead_id == written,
                )
                .values(
                    status=other_write.status,
                    attempts=other_write.attempts,
                    next_attempt_at=other_write.next_attempt_at,
                    last_checked_at=other_write.last_checked_at,
                    reason=other_write.reason,
                )
            )

    platforms = Platforms(world, hubspot_hook=lose_two_leases)
    async with _clients(platforms) as clients:
        with capture_logs() as logs:
            report = await lead_match.run_matching(
                env.sessions, world.crm_dataset_id, clients, now=T0
            )

    assert report == MatchReport(attempted=1, matched=1, batches=1, pending_left=1)
    rows = await _rows(env, world)
    assert rows[re_leased] == Row("pending", 0, other_lease[0], None, None, None)
    assert rows[written] == other_write
    _assert_matched(rows, [kept])
    # HubSpot found all three contacts, but only the held row's contact is written: the lost
    # rows belong to the runs that took them over.
    assert len(platforms.emails_sent()) == 3
    contacts = await _contacts(env, world)
    assert set(contacts) == {_hs_id(kept)}
    assert rows[kept].contact_id == contacts[_hs_id(kept)].id
    (batch,) = _events(logs, "sync.lead_match.batch")
    assert batch["log_level"] == "warning"
    assert (batch["attempted"], batch["matched"], batch["leases_lost"]) == (1, 1, 2)


@pytest.mark.anyio
async def test_a_batch_that_lost_every_lease_writes_no_contact(env: Env) -> None:
    world = await _seed(env, 1)
    (lead_id,) = world.lead_ids
    (email,) = world.lead_of_email
    winner: list[tuple[Row, StoredContact]] = []

    async def another_run_wins(_emails: list[str]) -> None:
        # While HubSpot is called, the lease expires and another run re-claims the row,
        # stores a newer contact under the same HubSpot id this lookup returns, and
        # completes the match.
        async with env.probe.begin() as connection:
            contact_id = await connection.scalar(
                insert(CrmContact)
                .values(
                    source="hubspot",
                    dataset_id=world.crm_dataset_id,
                    hs_id=_hs_id(lead_id),
                    email_sha256=hashlib.sha256(email.encode()).hexdigest(),
                    lifecycle_stage="qualified",
                    mql_at=T0 - timedelta(hours=2),
                    ql_at=T0 - timedelta(hours=1),
                    updated_at=T0 + timedelta(minutes=30),
                )
                .returning(CrmContact.id)
            )
            assert contact_id is not None
            won = Row("matched", 1, None, T0 + timedelta(minutes=40), contact_id, None)
            await connection.execute(
                update(LeadMatch)
                .where(
                    LeadMatch.crm_dataset_id == world.crm_dataset_id,
                    LeadMatch.lead_id == lead_id,
                )
                .values(
                    status=won.status,
                    attempts=won.attempts,
                    next_attempt_at=won.next_attempt_at,
                    last_checked_at=won.last_checked_at,
                    contact_id=won.contact_id,
                    contact_read_status="ok",
                    reason=won.reason,
                )
            )
        winner.append((won, (await _contacts(env, world))[_hs_id(lead_id)]))

    platforms = Platforms(world, hubspot_hook=another_run_wins)
    async with _clients(platforms) as clients:
        with capture_logs() as logs:
            report = await lead_match.run_matching(
                env.sessions, world.crm_dataset_id, clients, now=T0
            )

    # The lookup did find the contact, older than the winner's ("lead", updated 2026-09-30).
    assert platforms.emails_sent() == [email]
    ((won_row, won_contact),) = winner
    assert won_contact.lifecycle_stage == "qualified"
    assert report == MatchReport(attempted=0, batches=1)
    (batch,) = _events(logs, "sync.lead_match.batch")
    assert batch["log_level"] == "warning"
    assert (batch["attempted"], batch["matched"], batch["leases_lost"]) == (0, 0, 1)
    # Neither the winner's contact nor its match was written again: same values, same xmin.
    assert await _contacts(env, world) == {_hs_id(lead_id): won_contact}
    assert await _rows(env, world) == {lead_id: won_row}
    async with env.probe.connect() as connection:
        read_status = await connection.scalar(
            select(LeadMatch.contact_read_status).where(
                LeadMatch.crm_dataset_id == world.crm_dataset_id, LeadMatch.lead_id == lead_id
            )
        )
    assert read_status == "ok"


# ---------------------------------------------------------------------------------------
# (e) An abandoned lease expires
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_abandoned_lease_is_due_again_only_once_it_has_passed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _seed(env, 5)
    crash = HandlerCrashError("the worker died mid-batch")

    async def die(_emails: list[str]) -> None:
        raise crash

    async def unreachable_database(*_args: object) -> int:
        raise OperationalError(
            "UPDATE lead_match", None, ConnectionError("server closed the connection")
        )

    # The release cannot reach the database, as after a crash: the lease is left in place.
    monkeypatch.setattr(lead_match, "_release", unreachable_database)
    platforms = Platforms(world, hubspot_hook=die)
    async with _clients(platforms) as clients:
        with capture_logs() as logs, pytest.raises(HandlerCrashError) as raised:
            await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)
        assert raised.value is crash
        (failed,) = _events(logs, "sync.lead_match.release_failed")
        assert (failed["log_level"], failed["claimed"], failed["error"]) == (
            "warning",
            5,
            "OperationalError",
        )
        abandoned = await _rows(env, world)
        lease = _lease_of(
            (row.status, row.attempts, row.next_attempt_at) for row in abandoned.values()
        )

        platforms.hubspot_hook = None
        for early in (T0, lease - timedelta(seconds=1), lease - timedelta(microseconds=1)):
            report = await lead_match.run_matching(
                env.sessions, world.crm_dataset_id, clients, now=early
            )
            assert report == MatchReport(pending_left=5), early
        assert await _rows(env, world) == abandoned
        assert len(platforms.hubspot_calls) == 1

        report = await lead_match.run_matching(
            env.sessions, world.crm_dataset_id, clients, now=lease
        )

    assert report == MatchReport(attempted=5, matched=5, batches=1)
    rows = await _rows(env, world)
    assert {(row.status, row.attempts, row.last_checked_at) for row in rows.values()} == {
        ("matched", 1, lease)
    }
    assert sorted(platforms.hubspot_calls[1]) == sorted(world.lead_of_email)


# ---------------------------------------------------------------------------------------
# (f) The release never writes into a CRM dataset retired or purged mid-batch
# ---------------------------------------------------------------------------------------

RETIRED_AT = T0 + timedelta(minutes=5)
"""When a test's CRM dataset is retired, while its batch is in flight."""


async def _retire(connection: AsyncConnection, crm_dataset_id: int) -> None:
    """Retire a dataset as ``DatasetManager.activate`` does: lock its row, then update it.

    The caller's transaction decides when the retirement commits.
    """
    await connection.execute(
        select(SourceDataset.id).where(SourceDataset.id == crm_dataset_id).with_for_update()
    )
    await connection.execute(
        update(SourceDataset)
        .where(SourceDataset.id == crm_dataset_id)
        .values(state="retired", retired_at=RETIRED_AT)
    )


async def _lease_in_flight(env: Env, world: World, emails: list[str]) -> datetime:
    """Return the lease instant the rows behind a HubSpot call carry while it is in flight."""
    leased = await _snapshot(env, world, "hubspot", world.leads_for_emails(emails))
    assert leased.unlocked
    return _lease_of(leased.claimed_rows.values())


def _assert_leased(rows: dict[int, Row], world: World, lease: datetime) -> None:
    """Every row of the world is exactly as its claim's lease left it."""
    assert set(rows) == set(world.lead_ids)
    for lead_id, row in rows.items():
        assert row == Row("pending", 0, lease, None, None, None), lead_id


def _assert_release_skipped(
    logs: Sequence[dict[str, Any]], world: World, claimed: int, state: str | None
) -> None:
    """The release was skipped and logged as such, with counts and the state only."""
    assert _events(logs, "sync.lead_match.released") == []
    assert _events(logs, "sync.lead_match.release_failed") == []
    assert _events(logs, "sync.lead_match.release_skipped") == [
        {
            "event": "sync.lead_match.release_skipped",
            "log_level": "info",
            "crm_dataset_id": world.crm_dataset_id,
            "claimed": claimed,
            "state": state,
        }
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("ending", ["retired", "purged"])
async def test_a_crm_dataset_ended_mid_batch_is_not_written_by_the_release(
    env: Env, ending: str
) -> None:
    world = await _seed(env, 5)
    leases: list[datetime] = []

    async def end_in_flight(emails: list[str]) -> None:
        leases.append(await _lease_in_flight(env, world, emails))
        async with env.probe.begin() as connection:
            if ending == "retired":
                await _retire(connection, world.crm_dataset_id)
            else:
                await connection.execute(
                    delete(SourceDataset).where(SourceDataset.id == world.crm_dataset_id)
                )

    platforms = Platforms(world, hubspot_hook=end_in_flight)
    async with _clients(platforms) as clients:
        with capture_logs() as logs, pytest.raises(repositories.DatasetNotWritable) as raised:
            await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    state = "retired" if ending == "retired" else None
    assert (raised.value.dataset_id, raised.value.state) == (world.crm_dataset_id, state)
    assert len(platforms.hubspot_calls) == 1
    rows = await _rows(env, world)
    if ending == "retired":
        # The read-only dataset's rows keep the lease: neither the batch's outcomes nor the
        # release's previous due times were written, and no contact was stored.
        (lease,) = leases
        _assert_leased(rows, world, lease)
        assert await _contacts(env, world) == {}
    else:
        assert rows == {}
    assert _events(logs, "sync.lead_match.batch") == []
    _assert_release_skipped(logs, world, 5, state)


@pytest.mark.anyio
async def test_a_batch_cancelled_after_its_crm_dataset_retired_keeps_the_lease(
    env: Env,
) -> None:
    world = await _seed(env, 5)
    leases: list[datetime] = []
    in_flight = asyncio.Event()
    never = asyncio.Event()

    async def retire_then_hang(emails: list[str]) -> None:
        leases.append(await _lease_in_flight(env, world, emails))
        async with env.probe.begin() as connection:
            await _retire(connection, world.crm_dataset_id)
        in_flight.set()
        await never.wait()

    platforms = Platforms(world, hubspot_hook=retire_then_hang)
    async with _clients(platforms) as clients:
        task = asyncio.create_task(
            lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)
        )
        await asyncio.wait_for(in_flight.wait(), timeout=60)
        with capture_logs() as logs:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    (lease,) = leases
    _assert_leased(await _rows(env, world), world, lease)
    _assert_release_skipped(logs, world, 5, "retired")


@pytest.mark.anyio
async def test_a_crash_after_the_crm_dataset_retired_propagates_unmasked(env: Env) -> None:
    world = await _seed(env, 5)
    leases: list[datetime] = []
    crash = HandlerCrashError("the HubSpot handler crashed after the retirement")

    async def retire_then_crash(emails: list[str]) -> None:
        leases.append(await _lease_in_flight(env, world, emails))
        async with env.probe.begin() as connection:
            await _retire(connection, world.crm_dataset_id)
        raise crash

    platforms = Platforms(world, hubspot_hook=retire_then_crash)
    async with _clients(platforms) as clients:
        with capture_logs() as logs, pytest.raises(HandlerCrashError) as raised:
            await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    assert raised.value is crash
    (lease,) = leases
    _assert_leased(await _rows(env, world), world, lease)
    _assert_release_skipped(logs, world, 5, "retired")


async def _wait_until_the_matcher_waits_on_a_lock(env: Env, task: asyncio.Task[Any]) -> None:
    """Return once a matcher backend waits on a lock; fail if the run ends first or never does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while True:
        async with env.probe.connect() as connection:
            waiting = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = :app AND wait_event_type = 'Lock'"
                ),
                {"app": env.app},
            )
        if waiting:
            return
        assert not task.done(), "the release ran without waiting for the uncommitted retirement"
        assert loop.time() < deadline, "the release never waited for the uncommitted retirement"
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_a_release_waits_for_an_uncommitted_retirement_and_then_skips(env: Env) -> None:
    world = await _seed(env, 5)
    leases: list[datetime] = []
    retirements: list[AsyncConnection] = []
    crash = HandlerCrashError("the HubSpot handler crashed during a retirement")

    async def begin_retirement_then_crash(emails: list[str]) -> None:
        # The retirement locks the dataset row and updates it, but does not commit yet.
        leases.append(await _lease_in_flight(env, world, emails))
        connection = await env.probe.connect()
        retirements.append(connection)
        await _retire(connection, world.crm_dataset_id)
        raise crash

    platforms = Platforms(world, hubspot_hook=begin_retirement_then_crash)
    async with _clients(platforms) as clients:
        with capture_logs() as logs:
            task = asyncio.create_task(
                lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)
            )
            try:
                await _wait_until_the_matcher_waits_on_a_lock(env, task)
                # The release waits on the dataset's lifecycle lock before it locks any row.
                _assert_leased(await _rows(env, world), world, leases[0])
                (retirement,) = retirements
                await retirement.commit()
            finally:
                # Closing rolls back a retirement a failed assertion left uncommitted, so the
                # run always ends and is waited for here.
                for connection in retirements:
                    await connection.close()
                done, _pending = await asyncio.wait([task], timeout=60)
            assert task in done
            with pytest.raises(HandlerCrashError) as raised:
                task.result()

    assert raised.value is crash
    (lease,) = leases
    _assert_leased(await _rows(env, world), world, lease)
    _assert_release_skipped(logs, world, 5, "retired")


# ---------------------------------------------------------------------------------------
# The pending count matches the readiness test
# ---------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_left_ignores_rows_of_a_retired_linkedin_dataset(env: Env) -> None:
    world = await _seed(env, 3)
    async with env.probe.begin() as connection:
        lead_dataset_id = await connection.scalar(
            select(Lead.dataset_id).where(Lead.id == world.lead_ids[0])
        )
        await connection.execute(
            update(SourceDataset)
            .where(SourceDataset.id == lead_dataset_id)
            .values(state="retired", retired_at=T0)
        )
    platforms = Platforms(world)
    async with _clients(platforms) as clients:
        report = await lead_match.run_matching(env.sessions, world.crm_dataset_id, clients, now=T0)

    # The retired dataset's leads are never claimed, and like the readiness test the run's
    # pending count leaves them out although their rows stay pending.
    assert report == MatchReport()
    assert platforms.linkedin_calls == []
    _assert_untouched(await _rows(env, world), world, world.lead_ids)
