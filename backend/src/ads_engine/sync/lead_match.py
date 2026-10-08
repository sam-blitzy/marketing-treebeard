"""Durable lead-to-contact matching: staging, due-row claiming and the retry schedule.

AAP 0.3.3 ("Lead-to-contact matching", "Staged matching on a CRM dataset change",
"Cross-source joins with simulation") and 0.5.1 ("Sync jobs and snapshots"). Every LinkedIn
lead is matched to a HubSpot contact once per CRM dataset, and the state of that match lives
in one ``lead_match`` row keyed (``lead_id``, ``crm_dataset_id``). A ``building`` CRM dataset
therefore stages and attempts its own rows while the active dataset's rows stay in use, and
one transaction in ``sync/datasets.py`` later activates the building dataset with its
matching state.

Personal data:
    Lead emails are persisted only as SHA-256 hashes (``lead.email_sha256``). To match a
    lead, :func:`run_matching` re-fetches its response from LinkedIn by id, decodes the email
    with the response's own form-version map, holds the plaintext in memory for exactly one
    HubSpot ``contacts_by_email`` batch call, and drops it before the batch commits. No
    email, lead id or contact id is logged: log events carry counts only.

Row lifecycle:
    * ``pending``: staged, never attempted; always due.
    * ``matched`` (terminal): a contact of ``crm_dataset_id`` was found; ``contact_id`` and
      ``contact_read_status = 'ok'`` are set together, because the email batch read that
      found the contact is itself a successful read of it.
    * ``no_match``: HubSpot has no contact with the email yet; retried on the schedule of
      :func:`next_attempt_after` (1 h, 6 h, 24 h, then daily) until 30 days after
      submission, so contacts created late are still found.
    * ``error``: the attempt got no answer. A transient LinkedIn or HubSpot failure is
      retried like ``no_match``. Terminal errors: :data:`REASON_EMAIL_UNRETRIEVABLE`
      (LinkedIn no longer returns the response), :data:`REASON_SIMULATED_WITHHELD`,
      :data:`REASON_DATASET_MISMATCH` and :data:`REASON_RESPONSE_DEFECT`.
    * ``no_email`` (terminal): the response carries no decodable email, for example
      :data:`REASON_FORM_MAP_MISMATCH` or :data:`REASON_UNKNOWN_FORM_VERSION`.

    Every status other than ``matched`` counts as unresolved in attribution (AAP 0.5.4.1),
    and every status other than ``pending`` counts as attempted for the staged-matching
    readiness test (``DatasetManager.readiness``).

Which (lead dataset, CRM dataset) pairs get rows (the callers in ``sync/mode.py`` and
``sync/jobs.py`` follow this list, and :func:`create_pending` and :func:`stage_rows` refuse
any other pair):

    * a new lead of the active LinkedIn dataset gets a row in its effective CRM dataset
      (``repositories.effective_crm_dataset_id``: the companion when LinkedIn is simulated,
      HubSpot's active primary otherwise), plus one in every ``building`` HubSpot primary
      dataset when the lead is live;
    * a companion build stages its parent's leads;
    * a HubSpot primary build stages the active LinkedIn dataset's leads in the backfill
      range, only when that LinkedIn dataset is live (simulated leads read their companion);
    * a live LinkedIn build stages its backfilled leads against HubSpot's active primary.

    Isolation: leads of a simulated LinkedIn dataset are matched only against their own
    companion, never against a live CRM dataset, and a companion only ever holds its
    parent's leads. Live leads may be matched against a live or a simulated HubSpot primary
    (the simulated HubSpot transport answers real emails for a deterministic share).

Restart safety:
    :func:`run_matching` works one batch at a time, and no transaction, row lock or pooled
    connection is held while LinkedIn or HubSpot is called:

    1. **Claim**, in one short transaction: due rows are selected ``FOR UPDATE OF
       lead_match SKIP LOCKED``, which still serialises claims so concurrent runs take
       disjoint rows, and each claimed row is leased by setting its ``next_attempt_at`` to
       the batch's lease instant (:data:`CLAIM_LEASE`). A future ``next_attempt_at`` is not
       due, so no other run claims a leased row; its status and attempts are untouched, so
       a leased ``pending`` row still counts as not attempted for readiness. Commit.
    2. **Attempt** with no session open: the LinkedIn re-fetch, decoding and the single
       HubSpot lookup.
    3. **Persist**, in a second short transaction: the claimed rows are locked again
       ``FOR UPDATE`` and written only while they still hold the lease (status, attempts
       and ``next_attempt_at`` exactly as the claim left them), and only the contacts the
       lookup found for those rows are stored. A row whose lease expired and was claimed
       again elsewhere belongs to that run and is skipped, its contact included, so newer
       state that run stored is never overwritten. Commit.

    An exception or a cancellation between the claim and the persist hands the lease back
    (every row still under it gets its previous ``next_attempt_at`` again) and then
    propagates, so the batch's rows are due again unchanged; batches already committed
    stand. A process that dies mid-batch leaves its lease to expire, after which the rows
    are due again: no state is lost. ``AccessDenied`` and ``QueryDefect`` raised by a
    connector call propagate, because ``sync/mode.py`` alone decides what follows from them.

Example:
    The CRM job of a sync cycle::

        clients = MatchClients(
            linkedin_for=lambda dataset_id: linkedin_clients[dataset_id],
            hubspot=hubspot,
            contact_properties=HubSpotClient.contact_property_map(bindings, settings),
        )
        report = await run_matching(get_sessionmaker(), crm_dataset_id, clients, now=now)
"""

import asyncio
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from ads_engine.connectors.errors import QueryDefect
from ads_engine.connectors.hubspot.client import EmailLookup, HubSpotClient
from ads_engine.connectors.hubspot.parser import ContactPropertyMap, ContactRow
from ads_engine.connectors.linkedin.client import LEAD_BATCH_SIZE, LinkedInClient
from ads_engine.connectors.linkedin.parser import (
    REASON_FORM_MAP_MISMATCH,
    REASON_NO_EMAIL_ANSWER,
    REASON_UNKNOWN_FORM_VERSION,
    FormVersionMap,
    KnownVersions,
    decode_email,
)
from ads_engine.db import repositories
from ads_engine.db.models.accounts import AdAccount, Campaign, LeadForm, LeadFormVersion
from ads_engine.db.models.crm import (
    LEAD_MATCH_CLAIMABLE_SQL,
    LEAD_MATCH_STATUSES,
    CrmContact,
    Lead,
    LeadMatch,
)
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.session import get_sessionmaker
from ads_engine.logging import get_logger
from ads_engine.simulation import is_simulated_email

__all__ = [
    "BATCH_SIZE",
    "CLAIM_LEASE",
    "MATCH_HORIZON",
    "REASON_DATASET_MISMATCH",
    "REASON_EMAIL_UNRETRIEVABLE",
    "REASON_FORM_MAP_MISMATCH",
    "REASON_LOOKUP_FAILED",
    "REASON_NO_CONTACT",
    "REASON_RESPONSE_DEFECT",
    "REASON_SIMULATED_WITHHELD",
    "REASON_UNKNOWN_FORM_VERSION",
    "RETRY_DELAYS",
    "TRANSIENT_ATTEMPT_LIMIT",
    "MatchClients",
    "MatchReport",
    "create_pending",
    "known_versions",
    "next_attempt_after",
    "run_matching",
    "stage_rows",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------------------
# Retry schedule and batch size
# ---------------------------------------------------------------------------------------

RETRY_DELAYS: Final[tuple[timedelta, ...]] = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)
"""Wait after the 1st, 2nd and 3rd attempt of a ``no_match`` or ``error`` row; then daily."""

_DAILY: Final = timedelta(days=1)
"""Wait after every attempt beyond those :data:`RETRY_DELAYS` covers."""

MATCH_HORIZON: Final = timedelta(days=30)
"""Retries stop once the next attempt would fall later than submission plus this horizon."""

TRANSIENT_ATTEMPT_LIMIT: Final = 10
"""Total attempts up to which a transient failure is retried even past :data:`MATCH_HORIZON`.

The horizon bounds how long matching waits for a contact to be *created* (``no_match``). A
transient failure is no answer at all, and backfilled leads staged for a new CRM dataset are
often older than the horizon at their first attempt, so one HubSpot or LinkedIn outage would
otherwise leave them unresolved for good. Such a row keeps the normal schedule until this
many attempts (about a week), then stops like any other row.
"""

BATCH_SIZE: Final = 100
"""Rows claimed per batch: the product's LinkedIn re-fetch size and HubSpot's batch read."""

CLAIM_LEASE: Final = timedelta(minutes=30)
"""How long a claimed batch stays reserved for the run that claimed it.

The claim writes the lease instant to each claimed row's ``next_attempt_at`` and commits, so
the re-fetch and the lookup run with no row lock or connection held while no other run can
claim the rows. The instant is the run's ``now`` plus the time the run has spent so far plus
this lease: ``now`` is fixed for a whole run, and the elapsed term keeps a late batch's lease
ahead of the wall clock. A batch normally makes one LinkedIn and one HubSpot call, each at
most three attempts of 60 s with waits of at most 120 s between them, so about 14 minutes at
worst; the lease leaves room beyond that. A batch that outlives it anyway (many ad accounts
in one batch) loses only the rows another run has re-claimed meanwhile, which the persist
step detects and skips. Rows a dead process leaves leased are due again once it has passed.
"""

_INSERT_CHUNK: Final = 1_000
"""Rows per ``INSERT``: seven bind parameters each stays far below PostgreSQL's limit."""

# ---------------------------------------------------------------------------------------
# Reasons. REASON_FORM_MAP_MISMATCH and REASON_UNKNOWN_FORM_VERSION are the LinkedIn
# parser's own strings, re-exported rather than redefined so the two can never drift.
# ---------------------------------------------------------------------------------------

REASON_EMAIL_UNRETRIEVABLE: Final = "email no longer retrievable"
"""Terminal ``error``: LinkedIn's batch get no longer returns the lead's response."""

REASON_SIMULATED_WITHHELD: Final = "simulated email withheld from live portal"
"""Terminal ``error``: a simulated lead reached a live HubSpot client and was not sent."""

REASON_DATASET_MISMATCH: Final = "lead dataset is not paired with this CRM dataset"
"""Terminal ``error``: a row pairs a lead with a CRM dataset the isolation rule forbids."""

REASON_RESPONSE_DEFECT: Final = "re-fetched lead response failed contract validation"
"""Terminal ``error``: the re-fetched response breaks the Lead Sync contract."""

REASON_NO_CONTACT: Final = "no HubSpot contact with this email yet"
"""Retried ``no_match``: HubSpot reported the email as not found."""

REASON_LOOKUP_FAILED: Final = "HubSpot contact lookup failed"
"""Retried ``error``: the HubSpot batch read failed for the email."""

# Status vocabulary, spelled once and tied to the schema's CHECK values below.
_PENDING: Final = "pending"
_MATCHED: Final = "matched"
_NO_MATCH: Final = "no_match"
_NO_EMAIL: Final = "no_email"
_ERROR: Final = "error"
_READ_OK: Final = "ok"

if not {_PENDING, _MATCHED, _NO_MATCH, _NO_EMAIL, _ERROR} <= set(LEAD_MATCH_STATUSES):
    raise RuntimeError(  # pragma: no cover - schema drift fails at import
        f"lead_match statuses {LEAD_MATCH_STATUSES!r} no longer match sync/lead_match.py"
    )


def _require_aware(value: datetime, name: str) -> datetime:
    """Return ``value`` when it is timezone-aware, else raise ``ValueError``."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _delay_after(attempts: int) -> timedelta:
    """Return the wait after attempt ``attempts`` (1-based) on the regular schedule."""
    if attempts <= len(RETRY_DELAYS):
        return RETRY_DELAYS[attempts - 1]
    return _DAILY


def next_attempt_after(attempts: int, submitted_at: datetime, now: datetime) -> datetime | None:
    """Return when a ``no_match`` or ``error`` row is next due, or ``None`` to stop retrying.

    After attempt ``n`` (1-based) the row waits ``RETRY_DELAYS[n - 1]`` (1 h, 6 h, 24 h), and
    one day after every later attempt. Retries stop when the next attempt would fall after
    ``submitted_at + MATCH_HORIZON``: the row then keeps its status with no
    ``next_attempt_at`` and counts as unresolved.

    Example::

        >>> from datetime import UTC
        >>> t = datetime(2026, 10, 1, tzinfo=UTC)
        >>> next_attempt_after(1, t, t) - t
        datetime.timedelta(seconds=3600)
        >>> next_attempt_after(4, t, t + timedelta(days=29, hours=1)) is None
        True

    Args:
        attempts: Attempts made so far, including the one just finished (at least 1).
        submitted_at: The lead's submission instant.
        now: The instant the attempt finished.

    Returns:
        The next due instant, or ``None`` when it would pass the horizon.

    Raises:
        ValueError: When ``attempts`` is below 1 or not an int, or an instant is naive.
    """
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError("attempts must be an integer of at least 1")
    _require_aware(submitted_at, "submitted_at")
    _require_aware(now, "now")
    candidate = now + _delay_after(attempts)
    if candidate > submitted_at + MATCH_HORIZON:
        return None
    return candidate


def _retry_at(
    attempts: int, submitted_at: datetime, now: datetime, *, transient: bool
) -> datetime | None:
    """Schedule a retryable outcome, granting transient failures the bounded extension.

    See :data:`TRANSIENT_ATTEMPT_LIMIT` for why a transient failure past the horizon is
    retried a few more times.
    """
    scheduled = next_attempt_after(attempts, submitted_at, now)
    if scheduled is None and transient and attempts < TRANSIENT_ATTEMPT_LIMIT:
        return now + _delay_after(attempts)
    return scheduled


# ---------------------------------------------------------------------------------------
# Datasets and the isolation rule
# ---------------------------------------------------------------------------------------


async def _crm_dataset(session: AsyncSession, crm_dataset_id: int) -> SourceDataset:
    """Load a writable HubSpot dataset (primary or companion).

    Raises:
        DatasetNotWritable: If the dataset does not exist or is retired.
        ValueError: If it is not a HubSpot dataset.
    """
    dataset = await repositories.ensure_writable(session, crm_dataset_id)
    if dataset.source != "hubspot":
        raise ValueError(
            f"source_dataset {crm_dataset_id} belongs to {dataset.source!r}, not 'hubspot'"
        )
    return dataset


async def _lead_dataset(session: AsyncSession, lead_dataset_id: int) -> SourceDataset:
    """Load a writable (building or active) LinkedIn dataset.

    Raises:
        DatasetNotWritable: If the dataset does not exist or is retired.
        ValueError: If it is not a LinkedIn dataset.
    """
    dataset = await repositories.ensure_writable(session, lead_dataset_id)
    if dataset.source != "linkedin":
        raise ValueError(
            f"source_dataset {lead_dataset_id} belongs to {dataset.source!r}, not 'linkedin'"
        )
    return dataset


@dataclass(frozen=True, slots=True)
class _CrmView:
    """The fields of a CRM dataset that matching reads after its claim has committed.

    The re-fetch and the lookup run with no session open, so they read this snapshot rather
    than a ``SourceDataset`` bound to a closed session.
    """

    id: int
    mode: str
    role: str
    parent_dataset_id: int | None

    @classmethod
    def of(cls, dataset: SourceDataset) -> _CrmView:
        """Snapshot a loaded dataset row."""
        return cls(
            id=dataset.id,
            mode=dataset.mode,
            role=dataset.role,
            parent_dataset_id=dataset.parent_dataset_id,
        )


def _pair_problem(
    lead_dataset_id: int, lead_mode: str, crm: SourceDataset | _CrmView
) -> str | None:
    """Return why leads of one LinkedIn dataset may not be matched in ``crm``, or ``None``.

    Simulated leads match only their own companion, so a simulated email never reaches a
    live portal and a simulated lead never joins a live contact; a companion holds only its
    parent's leads. Live leads may be matched in any HubSpot primary, live or simulated.
    """
    if lead_mode == "simulated":
        if crm.mode == "live":
            return (
                f"leads of simulated LinkedIn dataset {lead_dataset_id} cannot be matched "
                f"against live CRM dataset {crm.id}: simulated leads match only their companion"
            )
        if crm.role != "companion" or crm.parent_dataset_id != lead_dataset_id:
            return (
                f"leads of simulated LinkedIn dataset {lead_dataset_id} match only its own "
                f"companion, not CRM dataset {crm.id}"
            )
        return None
    if crm.role == "companion":
        return (
            f"companion CRM dataset {crm.id} holds only its parent's leads, not those of "
            f"live LinkedIn dataset {lead_dataset_id}"
        )
    return None


async def _check_pair(session: AsyncSession, lead_dataset_id: int, crm: SourceDataset) -> None:
    """Refuse to stage leads of ``lead_dataset_id`` in ``crm`` unless the pair is allowed.

    Raises:
        DatasetNotWritable: If the LinkedIn dataset does not exist or is retired.
        ValueError: If it is not a LinkedIn dataset, or the isolation rule forbids the pair.
    """
    leads = await _lead_dataset(session, lead_dataset_id)
    problem = _pair_problem(leads.id, leads.mode, crm)
    if problem is not None:
        raise ValueError(problem)


# ---------------------------------------------------------------------------------------
# Staging: pending rows
# ---------------------------------------------------------------------------------------


def _row(
    lead_id: int,
    crm_dataset_id: int,
    *,
    next_attempt_at: datetime | None,
    no_email_reason: str | None,
    checked_at: datetime | None,
) -> dict[str, Any]:
    """Build one ``lead_match`` insert row: ``pending``, or ``no_email`` with its reason."""
    if no_email_reason is None:
        return {
            "lead_id": lead_id,
            "crm_dataset_id": crm_dataset_id,
            "status": _PENDING,
            "attempts": 0,
            "next_attempt_at": next_attempt_at,
            "last_checked_at": None,
            "reason": None,
        }
    # ``no_email`` is decided by the LinkedIn response alone and is terminal in every CRM
    # dataset, so it is written directly, already attempted, with no retry scheduled.
    return {
        "lead_id": lead_id,
        "crm_dataset_id": crm_dataset_id,
        "status": _NO_EMAIL,
        "attempts": 0,
        "next_attempt_at": None,
        "last_checked_at": checked_at,
        "reason": no_email_reason,
    }


async def _insert_rows(session: AsyncSession, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert ``lead_match`` rows, leaving every existing (lead, CRM dataset) row untouched.

    Returns:
        The number of rows actually inserted.
    """
    inserted = 0
    for start in range(0, len(rows), _INSERT_CHUNK):
        chunk = [dict(row) for row in rows[start : start + _INSERT_CHUNK]]
        statement = (
            insert(LeadMatch)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=[LeadMatch.lead_id, LeadMatch.crm_dataset_id])
            .returning(LeadMatch.lead_id)
        )
        result = await session.execute(statement)
        inserted += len(result.all())
    return inserted


def _checked_reasons(
    reasons: Mapping[int, str] | None, lead_ids: Iterable[int]
) -> Mapping[int, str]:
    """Validate ``no_email_reasons`` against the leads being staged."""
    if reasons is None:
        return MappingProxyType({})
    known = set(lead_ids)
    for lead_id, reason in reasons.items():
        if lead_id not in known:
            raise ValueError(f"no_email_reasons names lead {lead_id}, which is not being staged")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("every no_email reason must be a non-blank string")
    return reasons


async def create_pending(
    session: AsyncSession,
    lead_ids: Sequence[int],
    crm_dataset_id: int,
    *,
    now: datetime,
    no_email_reasons: Mapping[int, str] | None = None,
) -> int:
    """Stage newly ingested leads for matching in one CRM dataset.

    ``LeadSyncJob`` calls this for each lead it stores, against the lead's effective CRM
    dataset and, for a live lead, every ``building`` HubSpot primary (module docstring). Each
    lead gets a ``pending`` row (``attempts = 0``, due at ``now``). A lead whose parse already
    ended ``no_email`` is written directly as ``no_email`` with its reason, which is terminal.
    Existing rows are never touched (``ON CONFLICT DO NOTHING`` on the primary key), so the
    call is idempotent and never resets a match. Nothing is committed here.

    Args:
        session: The caller's session; the rows land in its transaction.
        lead_ids: Ids of stored leads; repeats are staged once.
        crm_dataset_id: The HubSpot dataset (primary or companion) to stage them in.
        now: The staging instant, the rows' first due time.
        no_email_reasons: Reasons of leads whose email was not decodable at ingest, keyed by
            lead id (``ParsedLead.email_reason``).

    Returns:
        The number of rows inserted.

    Raises:
        DatasetNotWritable: If the CRM dataset or a lead's dataset is missing or retired.
        ValueError: If ``now`` is naive, a lead does not exist, a reason names a lead not
            being staged or is blank, the CRM dataset is not HubSpot's, or the isolation
            rule forbids staging a lead's dataset in it.
    """
    _require_aware(now, "now")
    wanted = list(dict.fromkeys(lead_ids))
    reasons = _checked_reasons(no_email_reasons, wanted)
    if not wanted:
        return 0
    datasets_of: dict[int, int] = {}
    for start in range(0, len(wanted), _INSERT_CHUNK):
        chunk = wanted[start : start + _INSERT_CHUNK]
        result = await session.execute(select(Lead.id, Lead.dataset_id).where(Lead.id.in_(chunk)))
        datasets_of.update((lead_id, dataset_id) for lead_id, dataset_id in result)
    lead_datasets = sorted(set(datasets_of.values()))
    # Lock order: the LinkedIn datasets' lifecycle guards before the CRM dataset's, the
    # order activation locks a primary and its companion in; the reverse could deadlock.
    for lead_dataset_id in lead_datasets:
        await repositories.lock_dataset(session, lead_dataset_id)
    crm = await _crm_dataset(session, crm_dataset_id)
    missing = [lead_id for lead_id in wanted if lead_id not in datasets_of]
    if missing:
        raise ValueError(f"{len(missing)} of {len(wanted)} leads to stage do not exist")
    for lead_dataset_id in lead_datasets:
        await _check_pair(session, lead_dataset_id, crm)
    rows = [
        _row(
            lead_id,
            crm.id,
            next_attempt_at=now,
            no_email_reason=reasons.get(lead_id),
            checked_at=now,
        )
        for lead_id in wanted
    ]
    inserted = await _insert_rows(session, rows)
    _log.info(
        "sync.lead_match.staged",
        crm_dataset_id=crm.id,
        requested=len(wanted),
        inserted=inserted,
        no_email=sum(1 for lead_id in wanted if lead_id in reasons),
    )
    return inserted


async def stage_rows(
    session: AsyncSession,
    crm_dataset_id: int,
    lead_dataset_id: int,
    *,
    since: datetime,
    now: datetime | None = None,
) -> int:
    """Stage every lead of one LinkedIn dataset submitted since ``since`` in a CRM dataset.

    Used when a CRM dataset is built (companion or HubSpot primary), when a live LinkedIn
    dataset is built, and to top up a HubSpot build when LinkedIn activates a new dataset
    meanwhile (AAP 0.3.3, "Staged matching"). Only leads with no row in ``crm_dataset_id``
    are staged, so the call is idempotent and never resets a match.

    A lead with no stored email hash whose ``no_email`` outcome is already recorded in
    another CRM dataset gets that terminal outcome copied: ``no_email`` depends only on the
    LinkedIn response, and re-fetching it could only turn a known reason into a failed
    re-fetch. Every other lead gets a ``pending`` row. Nothing is committed here.

    Args:
        session: The caller's session; the rows land in its transaction.
        crm_dataset_id: The HubSpot dataset (primary or companion) to stage in.
        lead_dataset_id: The LinkedIn dataset whose leads are staged.
        since: The earliest ``submitted_at`` staged, normally the backfill start.
        now: The staging instant, the rows' first due time. ``None`` leaves
            ``next_attempt_at`` empty, which the claim treats as due at once.

    Returns:
        The number of rows inserted.

    Raises:
        DatasetNotWritable: If either dataset is missing or retired.
        ValueError: If an instant is naive, a dataset belongs to the wrong source, or the
            isolation rule forbids the pair.
    """
    _require_aware(since, "since")
    if now is not None:
        _require_aware(now, "now")
    # Lock order: the LinkedIn dataset's lifecycle guard before the CRM dataset's, the
    # order activation locks a primary and its companion in; the reverse could deadlock.
    await repositories.lock_dataset(session, lead_dataset_id)
    crm = await _crm_dataset(session, crm_dataset_id)
    await _check_pair(session, lead_dataset_id, crm)
    # An alias, correlated to ``lead`` only: the outer query joins ``lead_match`` as well.
    staged = aliased(LeadMatch)
    already = (
        select(staged.lead_id)
        .where(staged.lead_id == Lead.id, staged.crm_dataset_id == crm.id)
        .correlate(Lead)
    )
    # One row per unstaged lead (DISTINCT ON), with the newest recorded no_email outcome of
    # a lead that has no email hash, if any.
    prior = (
        select(Lead.id, LeadMatch.reason, LeadMatch.last_checked_at)
        .outerjoin(
            LeadMatch,
            and_(
                LeadMatch.lead_id == Lead.id,
                Lead.email_sha256.is_(None),
                LeadMatch.status == _NO_EMAIL,
                LeadMatch.reason.is_not(None),
            ),
        )
        .where(
            Lead.dataset_id == lead_dataset_id,
            Lead.submitted_at >= since,
            ~already.exists(),
        )
        .ext(distinct_on(Lead.id))
        .order_by(Lead.id, LeadMatch.last_checked_at.desc().nulls_last())
    )
    candidates = (await session.execute(prior)).all()
    rows = [
        _row(
            lead_id,
            crm.id,
            next_attempt_at=now,
            no_email_reason=reason,
            checked_at=checked_at if checked_at is not None else now,
        )
        for lead_id, reason, checked_at in candidates
    ]
    inserted = await _insert_rows(session, rows)
    _log.info(
        "sync.lead_match.staged",
        crm_dataset_id=crm.id,
        lead_dataset_id=lead_dataset_id,
        requested=len(rows),
        inserted=inserted,
        no_email=sum(1 for _lead, reason, _checked in candidates if reason is not None),
    )
    return inserted


# ---------------------------------------------------------------------------------------
# Matching: clients, form versions and the report
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MatchClients:
    """The connector clients one matching run uses.

    Attributes:
        linkedin_for: Returns the LinkedIn client for a lead's dataset id: the live client
            for a live dataset, the client on the simulation's mock transport for a
            simulated one. Matching re-fetches each lead through its own dataset's client.
        hubspot: The client of the CRM dataset's portal; ``live`` must be true exactly when
            the CRM dataset is live, which :func:`run_matching` checks.
        contact_properties: The bound contact properties
            (``HubSpotClient.contact_property_map(bindings, settings)``), so matched
            contacts are stored with their stage dates and Google fields.
    """

    linkedin_for: Callable[[int], LinkedInClient]
    hubspot: HubSpotClient
    contact_properties: ContactPropertyMap


@dataclass(frozen=True, slots=True)
class MatchReport:
    """What one :func:`run_matching` call did; counts only, never ids or emails.

    Every attempted row lands in exactly one of ``matched``, ``no_match``, ``no_email``,
    ``errors`` and ``skipped``, so they sum to ``attempted``.

    Attributes:
        attempted: Rows claimed, attempted and written. A row whose lease was lost to
            another run before the write belongs to that run and is not counted.
        matched: Rows matched to a contact.
        no_match: Rows HubSpot reported as not found (retried on schedule).
        no_email: Rows whose response carried no decodable email (terminal).
        errors: Rows that ended ``error``: a failed re-fetch, a failed lookup or a contract
            defect.
        skipped: Rows withheld by the isolation rule, stored as terminal ``error``; a
            non-zero count means rows were staged against the wrong CRM dataset.
        batches: Batches committed.
        pending_left: Rows of the CRM dataset (restricted to ``lead_dataset_id`` when given)
            still ``pending`` after the run, which the readiness test counts.
    """

    attempted: int = 0
    matched: int = 0
    no_match: int = 0
    no_email: int = 0
    errors: int = 0
    skipped: int = 0
    batches: int = 0
    pending_left: int = 0


async def known_versions(session: AsyncSession, linkedin_dataset_id: int) -> KnownVersions:
    """Return the stored form-version maps of one LinkedIn dataset.

    Built from ``lead_form`` and ``lead_form_version``. A response re-fetched with the
    ``LEAD_FIELDS`` projection carries its own form decoration, which the parser prefers;
    these maps decode a response that arrives without one.

    Args:
        session: The caller's session.
        linkedin_dataset_id: The LinkedIn dataset whose forms are read (any state).

    Returns:
        A read-only mapping of ``(canonical_urn, version)`` to its map. Maps rebuilt from
        storage carry only the email question ids (``question_ids`` is ``None``).

    Raises:
        ValueError: When a stored email question id is not a digit string.
    """
    result = await session.execute(
        select(LeadForm.canonical_urn, LeadFormVersion.version, LeadFormVersion.email_question_ids)
        .join(LeadFormVersion, LeadFormVersion.lead_form_id == LeadForm.id)
        .where(
            LeadForm.dataset_id == linkedin_dataset_id,
            LeadFormVersion.dataset_id == linkedin_dataset_id,
        )
    )
    maps: dict[tuple[str, int], FormVersionMap] = {}
    for canonical_urn, version, email_question_ids in result:
        maps[(canonical_urn, version)] = FormVersionMap.from_storage(
            canonical_urn, version, email_question_ids
        )
    return MappingProxyType(maps)


# ---------------------------------------------------------------------------------------
# Matching: one batch
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Claim:
    """One claimed row as it stood when claimed, with what matching needs of its lead.

    No ORM object is kept, because the claim's transaction has committed by the time the
    row is attempted. ``status``, ``attempts`` and ``previous_attempt_at`` (the
    ``next_attempt_at`` the lease replaced) let the persist step verify the lease and a
    release restore the row.
    """

    lead_id: int
    status: str
    attempts: int
    previous_attempt_at: datetime | None
    lead_dataset_id: int
    lead_mode: str
    external_id: str
    account_id: str
    submitted_at: datetime
    email_sha256: str | None


@dataclass(frozen=True, slots=True)
class _Outcome:
    """The result of one attempt, before it is written to the row.

    ``retry`` schedules the next attempt; ``transient`` marks a failure that got no answer
    (eligible for :data:`TRANSIENT_ATTEMPT_LIMIT`); ``skipped`` marks an isolation refusal.
    """

    status: str
    reason: str | None
    retry: bool = False
    transient: bool = False
    skipped: bool = False
    contact_id: int | None = None


_UNRETRIEVABLE: Final = _Outcome(_ERROR, REASON_EMAIL_UNRETRIEVABLE)
_DEFECT: Final = _Outcome(_ERROR, REASON_RESPONSE_DEFECT)
_WITHHELD: Final = _Outcome(_ERROR, REASON_SIMULATED_WITHHELD, skipped=True)
_MISMATCHED: Final = _Outcome(_ERROR, REASON_DATASET_MISMATCH, skipped=True)
_NOT_FOUND: Final = _Outcome(_NO_MATCH, REASON_NO_CONTACT, retry=True)
_LOOKUP_FAILED: Final = _Outcome(_ERROR, REASON_LOOKUP_FAILED, retry=True, transient=True)


@dataclass(slots=True)
class _BatchCounts:
    """Counts of one batch, logged and folded into the :class:`MatchReport`."""

    attempted: int = 0
    matched: int = 0
    no_match: int = 0
    no_email: int = 0
    errors: int = 0
    skipped: int = 0
    retries_scheduled: int = 0
    retries_exhausted: int = 0
    defects: int = 0
    hash_mismatches: int = 0
    leases_lost: int = 0


@dataclass(slots=True)
class _Refetch:
    """Outcomes decided while re-fetching, and the emails still to look up.

    ``emails`` maps each email's SHA-256 to its plaintext and is cleared as soon as the one
    HubSpot call that needs it returns; ``waiting`` maps the same digest to its rows.
    """

    outcomes: dict[int, _Outcome]
    emails: dict[str, str]
    waiting: dict[str, list[_Claim]]
    defects: int = 0
    hash_mismatches: int = 0


def _refetchable(external_id: str) -> bool:
    """Whether a stored response id is one LinkedIn's batch get can be asked for.

    The client rejects a blank id, a URN or one holding a double quote with ``ValueError``
    before any call; such a lead can never be re-fetched, so its email is unretrievable.
    """
    return (
        bool(external_id.strip()) and not external_id.startswith("urn:") and '"' not in external_id
    )


async def _claim(
    session: AsyncSession, crm_dataset_id: int, now: datetime, lead_dataset_id: int | None
) -> list[_Claim]:
    """Lock and return up to :data:`BATCH_SIZE` due rows of one CRM dataset.

    Due: ``pending`` rows whose ``next_attempt_at`` is empty or has passed (a leased row is
    not due until its lease expires); ``no_match`` and ``error`` rows whose
    ``next_attempt_at`` has passed (a row with no ``next_attempt_at`` has stopped
    retrying). Leads of a retired LinkedIn dataset are never attempted: no evidence reads
    them again. Only ``lead_match`` rows are locked (``FOR UPDATE OF lead_match SKIP
    LOCKED``): locking the joined ``source_dataset`` rows would block a concurrent
    activation, and rows another worker holds are skipped rather than waited for. The
    caller leases the rows (:func:`_lease`) before this transaction commits.

    The status test is :data:`~ads_engine.db.models.crm.LEAD_MATCH_CLAIMABLE_SQL`, sent as
    literal SQL, so PostgreSQL proves under custom and generic plans alike that the partial
    index ``ix_lead_match_claim`` (``crm_dataset_id``, ``next_attempt_at NULLS FIRST``,
    ``lead_id``) applies. That index returns the claimable rows in this query's order, so a
    batch reads its rows in order and stops at its limit instead of sorting every due row;
    only the due-time test is bound to ``now``. A leased ``pending`` row stays in that
    index; the due-time test alone keeps it out of other claims until its lease expires.
    """
    statement = (
        select(
            LeadMatch,
            Lead.dataset_id,
            SourceDataset.mode,
            Lead.external_id,
            AdAccount.external_id,
            Lead.submitted_at,
            Lead.email_sha256,
        )
        .join(Lead, Lead.id == LeadMatch.lead_id)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        .join(AdAccount, AdAccount.id == Campaign.account_id)
        .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
        .where(
            LeadMatch.crm_dataset_id == crm_dataset_id,
            text(LEAD_MATCH_CLAIMABLE_SQL),
            or_(LeadMatch.next_attempt_at.is_(None), LeadMatch.next_attempt_at <= now),
            SourceDataset.state != "retired",
        )
        .order_by(LeadMatch.next_attempt_at.asc().nulls_first(), LeadMatch.lead_id)
        .limit(BATCH_SIZE)
        .with_for_update(skip_locked=True, of=LeadMatch)
    )
    if lead_dataset_id is not None:
        statement = statement.where(Lead.dataset_id == lead_dataset_id)
    result = await session.execute(statement)
    return [
        _Claim(
            lead_id=match.lead_id,
            status=match.status,
            attempts=match.attempts,
            previous_attempt_at=match.next_attempt_at,
            lead_dataset_id=dataset_id,
            lead_mode=mode,
            external_id=external_id,
            account_id=account_id,
            submitted_at=submitted_at,
            email_sha256=email_sha256,
        )
        for match, dataset_id, mode, external_id, account_id, submitted_at, email_sha256 in result
    ]


async def _lease(
    session: AsyncSession, crm_dataset_id: int, claims: Sequence[_Claim], lease_until: datetime
) -> None:
    """Reserve the claimed rows until ``lease_until``, inside the claim's transaction.

    The rows are still locked by :func:`_claim`. Once this transaction commits, their
    ``next_attempt_at`` lies in the future, so no other claim finds them due while the batch
    calls LinkedIn and HubSpot without holding any lock. Status and attempts stay as they
    are.
    """
    await session.execute(
        update(LeadMatch)
        .where(
            LeadMatch.crm_dataset_id == crm_dataset_id,
            LeadMatch.lead_id.in_([claim.lead_id for claim in claims]),
        )
        .values(next_attempt_at=lease_until)
        .execution_options(synchronize_session=False)
    )


async def _held(
    session: AsyncSession, crm_dataset_id: int, claims: Sequence[_Claim], lease_until: datetime
) -> dict[int, LeadMatch]:
    """Lock the claimed rows again and return those still under this batch's lease.

    A row still holds the lease when its status, attempts and ``next_attempt_at`` are
    exactly as the claim left them. Otherwise the lease expired and another run claimed the
    row (and perhaps already wrote it), or the row is gone; it is no longer this batch's.
    Rows are locked in key order, so two batches writing back never deadlock.

    Returns:
        The held rows, keyed by lead id.
    """
    rows = await session.scalars(
        select(LeadMatch)
        .where(
            LeadMatch.crm_dataset_id == crm_dataset_id,
            LeadMatch.lead_id.in_([claim.lead_id for claim in claims]),
        )
        .order_by(LeadMatch.lead_id)
        .with_for_update()
    )
    current = {row.lead_id: row for row in rows}
    held: dict[int, LeadMatch] = {}
    for claim in claims:
        row = current.get(claim.lead_id)
        if (
            row is not None
            and row.status == claim.status
            and row.attempts == claim.attempts
            and row.next_attempt_at == lease_until
        ):
            held[claim.lead_id] = row
    return held


async def _release(
    factory: async_sessionmaker[AsyncSession],
    crm_dataset_id: int,
    claims: Sequence[_Claim],
    lease_until: datetime,
) -> int:
    """Hand a batch's lease back: each row still under it gets its previous due time.

    Returns:
        The number of rows released; rows whose lease was already lost are left alone.
    """
    async with factory() as session, session.begin():
        held = await _held(session, crm_dataset_id, claims, lease_until)
        for claim in claims:
            row = held.get(claim.lead_id)
            if row is not None:
                row.next_attempt_at = claim.previous_attempt_at
        return len(held)


async def _release_quietly(
    factory: async_sessionmaker[AsyncSession],
    crm_dataset_id: int,
    claims: Sequence[_Claim],
    lease_until: datetime,
) -> None:
    """Release a failed batch's lease while the batch's exception propagates.

    A database error is logged (counts and the error type only) and never replaces that
    exception: the lease then expires at ``lease_until`` and the rows are due again.
    """
    try:
        released = await _release(factory, crm_dataset_id, claims, lease_until)
    except SQLAlchemyError as exc:
        _log.warning(
            "sync.lead_match.release_failed",
            crm_dataset_id=crm_dataset_id,
            claimed=len(claims),
            error=type(exc).__name__,
        )
        return
    _log.info(
        "sync.lead_match.released",
        crm_dataset_id=crm_dataset_id,
        claimed=len(claims),
        released=released,
    )


async def _run_to_completion(cleanup: Coroutine[Any, Any, None]) -> None:
    """Run a cleanup coroutine to completion even if the calling task is cancelled.

    The cleanup runs in a task of its own behind :func:`asyncio.shield`, so a cancellation
    that ended the batch, or one that arrives meanwhile, never abandons the release
    halfway. A cancellation received while waiting is re-raised once the cleanup has
    finished.
    """
    task = asyncio.create_task(cleanup)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as err:
            if task.done():
                raise
            cancelled = err
        else:
            break
    if cancelled is not None:
        raise cancelled


async def _versions_for(
    session: AsyncSession, cache: dict[int, KnownVersions], lead_dataset_id: int
) -> KnownVersions:
    """Return a LinkedIn dataset's stored form-version maps, loaded once per run."""
    known = cache.get(lead_dataset_id)
    if known is None:
        known = await known_versions(session, lead_dataset_id)
        cache[lead_dataset_id] = known
    return known


async def _refetch(
    crm: _CrmView,
    claims: Sequence[_Claim],
    clients: MatchClients,
    versions: Mapping[int, KnownVersions],
) -> _Refetch:
    """Re-fetch the claimed leads from LinkedIn and decode their emails transiently.

    Rows the isolation rule forbids, rows whose response LinkedIn no longer returns, rows
    with no decodable email and rows whose response breaks the contract get their outcome
    here; every decoded email is returned for the single HubSpot lookup of the batch. Runs
    with no session open: ``versions`` already holds the stored form-version maps of every
    claimed lead's dataset (:func:`_claim_batch`).

    Raises:
        AccessDenied: When LinkedIn refuses the credentials; the batch's lease is released.
        QueryDefect: When a whole LinkedIn response breaks the contract.
    """
    refetch = _Refetch(outcomes={}, emails={}, waiting={})
    live_portal = clients.hubspot.live
    groups: dict[tuple[int, str], list[_Claim]] = {}
    for claim in claims:
        lead_id = claim.lead_id
        if _pair_problem(claim.lead_dataset_id, claim.lead_mode, crm) is not None:
            # Defence in depth: staging already refuses these pairs (module docstring).
            simulated_to_live = claim.lead_mode == "simulated" and crm.mode == "live"
            refetch.outcomes[lead_id] = _WITHHELD if simulated_to_live else _MISMATCHED
        elif not _refetchable(claim.external_id):
            refetch.outcomes[lead_id] = _UNRETRIEVABLE
        else:
            groups.setdefault((claim.lead_dataset_id, claim.account_id), []).append(claim)

    for (lead_dataset_id, account_id), members in sorted(groups.items()):
        client = clients.linkedin_for(lead_dataset_id)
        known = versions[lead_dataset_id]
        for start in range(0, len(members), LEAD_BATCH_SIZE):
            chunk = members[start : start + LEAD_BATCH_SIZE]
            batch = await client.get_lead_responses(
                account_id, [claim.external_id for claim in chunk]
            )
            interrupted = batch.interrupted_by
            for claim in chunk:
                lead_id = claim.lead_id
                element = batch.found.get(claim.external_id)
                if element is None:
                    if interrupted is not None:
                        # Not read before a transient failure ended the batch: no answer
                        # yet, so retry. Without an interruption LinkedIn answered that it
                        # no longer has the response, which is terminal.
                        refetch.outcomes[lead_id] = _Outcome(
                            _ERROR, interrupted.reason, retry=True, transient=True
                        )
                    else:
                        refetch.outcomes[lead_id] = _UNRETRIEVABLE
                    continue
                try:
                    decoded = decode_email(element, known)
                except QueryDefect:
                    # One malformed response must not block every later row of the queue
                    # behind it: the row ends as a terminal, unresolved error and the
                    # defect is logged (with counts) for the batch.
                    refetch.defects += 1
                    refetch.outcomes[lead_id] = _DEFECT
                    continue
                if decoded.status != "ok" or decoded.email is None or decoded.sha256 is None:
                    refetch.outcomes[lead_id] = _Outcome(
                        _NO_EMAIL, decoded.reason or REASON_NO_EMAIL_ANSWER
                    )
                    continue
                if live_portal and is_simulated_email(decoded.email):
                    # Defence in depth: a simulated address never reaches a live portal.
                    refetch.outcomes[lead_id] = _WITHHELD
                    continue
                if claim.email_sha256 is not None and claim.email_sha256 != decoded.sha256:
                    refetch.hash_mismatches += 1
                refetch.emails[decoded.sha256] = decoded.email
                refetch.waiting.setdefault(decoded.sha256, []).append(claim)
    return refetch


async def _lookup(clients: MatchClients, emails: dict[str, str]) -> EmailLookup | None:
    """Look the decoded emails up in one HubSpot call, then drop every plaintext.

    The dictionary is cleared whether the call returns or raises, so no address outlives
    the call that needs it.
    """
    try:
        if not emails:
            return None
        return await clients.hubspot.contacts_by_email(
            list(emails.values()), clients.contact_properties
        )
    finally:
        emails.clear()


def _keep_held(refetch: _Refetch, held: Mapping[int, LeadMatch]) -> None:
    """Keep only the rows still under the lease among those waiting for the lookup.

    Every email left with no held row is dropped from ``refetch.waiting``, so neither its
    contact nor an outcome is written: the row belongs to the run that re-claimed it, which
    may already have stored a newer contact.
    """
    for digest in list(refetch.waiting):
        members = [claim for claim in refetch.waiting[digest] if claim.lead_id in held]
        if members:
            refetch.waiting[digest] = members
        else:
            del refetch.waiting[digest]


async def _store_contacts(
    session: AsyncSession, crm_dataset_id: int, contacts: Iterable[ContactRow]
) -> dict[str, int]:
    """Upsert matched contacts into the CRM dataset; return their ids keyed by HubSpot id.

    The caller passes only the contacts found for rows still under the batch's lease
    (:func:`_keep_held`); no contacts means no statement. Only the contact's email hash is
    stored, never an address.
    """
    rows: dict[str, dict[str, Any]] = {}
    for contact in contacts:
        rows[contact.hs_id] = {
            "hs_id": contact.hs_id,
            "email_sha256": contact.email_sha256,
            "lifecycle_stage": contact.lifecycle_stage,
            "mql_at": contact.mql_at,
            "ql_at": contact.ql_at,
            "google_campaign_ref": contact.google_campaign_ref,
            "google_touch_at": contact.google_touch_at,
            "updated_at": contact.updated_at,
        }
    if not rows:
        return {}
    stored = await repositories.upsert_entities(
        session, CrmContact, list(rows.values()), crm_dataset_id
    )
    return {str(key[-1]): contact_id for key, contact_id in stored.items()}


def _lookup_outcomes(
    refetch: _Refetch, lookup: EmailLookup | None, contact_ids: Mapping[str, int]
) -> None:
    """Turn the HubSpot lookup into the outcome of every held row that waited for it.

    ``refetch.waiting`` holds only rows still under the lease (:func:`_keep_held`), and
    ``contact_ids`` every contact found for them.
    """
    for digest, members in refetch.waiting.items():
        outcome: _Outcome
        if lookup is not None and digest in lookup.found:
            contact_id = contact_ids[lookup.found[digest].hs_id]
            outcome = _Outcome(_MATCHED, None, contact_id=contact_id)
        elif lookup is not None and digest in lookup.not_found:
            outcome = _NOT_FOUND
        elif lookup is not None and digest in lookup.skipped:
            outcome = _WITHHELD
        else:
            # Listed as failed, or (defensively) given no outcome at all: no answer yet.
            outcome = _LOOKUP_FAILED
        for claim in members:
            refetch.outcomes[claim.lead_id] = outcome


def _apply(
    claims: Sequence[_Claim],
    rows: Mapping[int, LeadMatch],
    outcomes: Mapping[int, _Outcome],
    now: datetime,
) -> _BatchCounts:
    """Write each held row's outcome: one more attempt, checked now, next due or done.

    ``claims`` are the batch's claims whose lease still holds and ``rows`` their rows, locked
    again by :func:`_held`.
    """
    counts = _BatchCounts(attempted=len(claims))
    for claim in claims:
        row = rows[claim.lead_id]
        outcome = outcomes.get(claim.lead_id)
        if outcome is None:
            raise RuntimeError("a claimed lead_match row was left without an outcome")
        row.attempts = row.attempts + 1
        row.last_checked_at = now
        row.status = outcome.status
        row.reason = outcome.reason
        if outcome.status == _MATCHED:
            row.contact_id = outcome.contact_id
            # The email batch read that found the contact is a successful read of it.
            row.contact_read_status = _READ_OK
        else:
            row.contact_id = None
            row.contact_read_status = None
        row.next_attempt_at = (
            _retry_at(row.attempts, claim.submitted_at, now, transient=outcome.transient)
            if outcome.retry
            else None
        )
        if outcome.retry:
            if row.next_attempt_at is None:
                counts.retries_exhausted += 1
            else:
                counts.retries_scheduled += 1
        if outcome.skipped:
            counts.skipped += 1
        elif outcome.status == _MATCHED:
            counts.matched += 1
        elif outcome.status == _NO_MATCH:
            counts.no_match += 1
        elif outcome.status == _NO_EMAIL:
            counts.no_email += 1
        else:
            counts.errors += 1
    return counts


async def _claim_batch(
    factory: async_sessionmaker[AsyncSession],
    crm_dataset_id: int,
    now: datetime,
    lead_dataset_id: int | None,
    lease_until: datetime,
    versions: dict[int, KnownVersions],
) -> tuple[_CrmView, list[_Claim]]:
    """Claim and lease one batch in a short transaction of its own, then commit.

    The stored form-version maps of the claimed leads' datasets are loaded into
    ``versions`` here (once per run), so the attempt that follows needs no session.

    Returns:
        The CRM dataset's snapshot and the claims; no claim when nothing is due.
    """
    async with factory() as session, session.begin():
        crm = await _crm_dataset(session, crm_dataset_id)
        claims = await _claim(session, crm.id, now, lead_dataset_id)
        if claims:
            await _lease(session, crm.id, claims, lease_until)
            for claimed_dataset_id in sorted({claim.lead_dataset_id for claim in claims}):
                await _versions_for(session, versions, claimed_dataset_id)
        return _CrmView.of(crm), claims


async def _attempt_batch(
    factory: async_sessionmaker[AsyncSession],
    crm: _CrmView,
    claims: Sequence[_Claim],
    clients: MatchClients,
    now: datetime,
    lease_until: datetime,
    versions: Mapping[int, KnownVersions],
) -> _BatchCounts:
    """Attempt one leased batch, then write its outcomes in a short transaction.

    The re-fetch and the lookup run with no session open. The write first checks that the
    CRM dataset is still writable and locks the claimed rows again; only rows still under
    ``lease_until`` are written, with only the contacts the lookup found for them, and the
    rest are counted as lost leases. A batch that holds no row writes nothing.
    """
    refetch = await _refetch(crm, claims, clients, versions)
    lookup = await _lookup(clients, refetch.emails)
    async with factory() as session, session.begin():
        await _crm_dataset(session, crm.id)
        held = await _held(session, crm.id, claims, lease_until)
        _keep_held(refetch, held)
        found: Mapping[str, ContactRow] = {} if lookup is None else lookup.found
        contact_ids = await _store_contacts(
            session, crm.id, [found[digest] for digest in refetch.waiting if digest in found]
        )
        _lookup_outcomes(refetch, lookup, contact_ids)
        counts = _apply(
            [claim for claim in claims if claim.lead_id in held], held, refetch.outcomes, now
        )
        counts.leases_lost = len(claims) - len(held)
        counts.defects = refetch.defects
        counts.hash_mismatches = refetch.hash_mismatches
        await session.flush()
    return counts


def _log_batch(crm_dataset_id: int, counts: _BatchCounts) -> None:
    """Log one ``sync.lead_match.batch`` event: counts only, never ids or emails."""
    fields: dict[str, object] = {
        "crm_dataset_id": crm_dataset_id,
        "attempted": counts.attempted,
        "matched": counts.matched,
        "no_match": counts.no_match,
        "no_email": counts.no_email,
        "errors": counts.errors,
        "skipped": counts.skipped,
        "retries_scheduled": counts.retries_scheduled,
        "retries_exhausted": counts.retries_exhausted,
        "defects": counts.defects,
        "hash_mismatches": counts.hash_mismatches,
        "leases_lost": counts.leases_lost,
    }
    if counts.skipped or counts.defects or counts.hash_mismatches or counts.leases_lost:
        # Each indicates a defect upstream: rows staged against the wrong CRM dataset, a
        # response that breaks the Lead Sync contract, a parser that hashes differently, or
        # a batch that outlived CLAIM_LEASE so another run re-claimed some of its rows.
        _log.warning("sync.lead_match.batch", **fields)
    else:
        _log.info("sync.lead_match.batch", **fields)


async def _pending_left(
    factory: async_sessionmaker[AsyncSession], crm_dataset_id: int, lead_dataset_id: int | None
) -> int:
    """Count the rows of a CRM dataset still ``pending``, as the readiness test does.

    Rows of leads whose LinkedIn dataset has retired are not counted: they are never claimed
    and the readiness test ignores them too.
    """
    statement = (
        select(func.count())
        .select_from(LeadMatch)
        .join(Lead, Lead.id == LeadMatch.lead_id)
        .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
        .where(
            LeadMatch.crm_dataset_id == crm_dataset_id,
            LeadMatch.status == _PENDING,
            SourceDataset.state != "retired",
        )
    )
    if lead_dataset_id is not None:
        statement = statement.where(Lead.dataset_id == lead_dataset_id)
    async with factory() as session:
        return int(await session.scalar(statement) or 0)


async def run_matching(
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    crm_dataset_id: int,
    clients: MatchClients,
    *,
    now: datetime,
    lead_dataset_id: int | None = None,
    max_batches: int | None = None,
) -> MatchReport:
    """Attempt every due ``lead_match`` row of one CRM dataset, one batch at a time.

    No transaction, row lock or pooled connection is held while LinkedIn or HubSpot is
    called. Each batch:

    1. **Claims** up to :data:`BATCH_SIZE` due rows with ``FOR UPDATE OF lead_match SKIP
       LOCKED`` (``pending`` first, then the longest-overdue retries), so concurrent runs
       claim disjoint rows, leases them until ``now`` plus the run's elapsed time plus
       :data:`CLAIM_LEASE` (written to ``next_attempt_at``), and commits.
    2. **Re-fetches** the leads from LinkedIn by plain response id, per (lead dataset, ad
       account), through ``clients.linkedin_for``. An id LinkedIn no longer returns ends as
       terminal ``error`` (:data:`REASON_EMAIL_UNRETRIEVABLE`); ids left unread by a
       transient failure end as ``error`` with that failure's reason and are retried.
    3. **Decodes** each email with the response's own form-version map. ``no_email`` is
       terminal.
    4. **Looks up** the decoded emails in one ``contacts_by_email`` call, then drops the
       plaintext. Found contacts become ``matched``; not found becomes ``no_match`` and a
       failed read ``error``, both retried; a simulated address a live portal refused
       becomes terminal ``error`` (:data:`REASON_SIMULATED_WITHHELD`).
    5. **Writes**, in a second short transaction that first checks the CRM dataset is still
       writable and locks the rows again ``FOR UPDATE``, every row still under the lease:
       the contacts found for those rows, and no others, are upserted into ``crm_contact``
       of the CRM dataset (email hash only), and each row gets one more attempt,
       ``last_checked_at = now`` and the next due time from :func:`next_attempt_after` for a
       retried outcome; then commits. A row whose lease expired and was claimed again by
       another run is left to that run, its contact included, so newer state that run
       stored is never overwritten.

    An exception or a cancellation between the claim and the write releases the lease
    (each row still under it gets its previous ``next_attempt_at`` again) and propagates
    unchanged, so the batch's rows are due again exactly as before and committed batches
    stand (AAP 0.3.3, "Restart safety"). A process that dies mid-batch leaves the lease to
    expire, after which the rows are due again. Rows attempted or leased in this run are
    not due again before ``now``, so the loop ends.

    Args:
        sessionmaker: The session factory; ``None`` uses the process-wide one.
        crm_dataset_id: The HubSpot dataset (primary or companion) whose rows are matched.
        clients: The connector clients; ``clients.hubspot.live`` must equal whether the CRM
            dataset is live.
        now: The run's instant: rows due at or before it are claimed, and it is recorded as
            ``last_checked_at``.
        lead_dataset_id: Restrict the run to the leads of one LinkedIn dataset.
        max_batches: Stop after this many batches; ``None`` runs until nothing is due.

    Returns:
        The run's counts.

    Raises:
        ValueError: If ``now`` is naive, ``max_batches`` is not a positive integer, the CRM
            dataset is not HubSpot's, or the HubSpot client's mode differs from the CRM
            dataset's.
        DatasetNotWritable: If the CRM dataset is missing or retired, including when it is
            retired while a batch is being attempted.
        AccessDenied: When LinkedIn or HubSpot refuses the credentials; the current batch's
            lease is released and ``sync/mode.py`` decides what follows.
        QueryDefect: When a whole response or a request breaks its contract.
    """
    _require_aware(now, "now")
    if max_batches is not None and (
        isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches < 1
    ):
        raise ValueError("max_batches must be a positive integer or None")
    factory = sessionmaker if sessionmaker is not None else get_sessionmaker()
    async with factory() as session:
        crm = await _crm_dataset(session, crm_dataset_id)
        crm_live = crm.mode == "live"
    if clients.hubspot.live != crm_live:
        raise ValueError(
            f"CRM dataset {crm_dataset_id} is {'live' if crm_live else 'simulated'} but the "
            f"HubSpot client is {'live' if clients.hubspot.live else 'simulated'}"
        )

    totals = _BatchCounts()
    batches = 0
    versions: dict[int, KnownVersions] = {}
    started = time.monotonic()
    while max_batches is None or batches < max_batches:
        # ``now`` is fixed for the whole run, so the time spent so far keeps a later batch's
        # lease CLAIM_LEASE ahead of the wall clock at its claim.
        lease_until = now + timedelta(seconds=time.monotonic() - started) + CLAIM_LEASE
        claimed_in, claims = await _claim_batch(
            factory, crm_dataset_id, now, lead_dataset_id, lease_until, versions
        )
        if not claims:
            break
        try:
            counts = await _attempt_batch(
                factory, claimed_in, claims, clients, now, lease_until, versions
            )
        except BaseException:
            # The claim has committed: hand the lease back so the rows are due again
            # unchanged, then let the batch's own exception propagate.
            await _run_to_completion(_release_quietly(factory, claimed_in.id, claims, lease_until))
            raise
        batches += 1
        _log_batch(crm_dataset_id, counts)
        totals.attempted += counts.attempted
        totals.matched += counts.matched
        totals.no_match += counts.no_match
        totals.no_email += counts.no_email
        totals.errors += counts.errors
        totals.skipped += counts.skipped

    report = MatchReport(
        attempted=totals.attempted,
        matched=totals.matched,
        no_match=totals.no_match,
        no_email=totals.no_email,
        errors=totals.errors,
        skipped=totals.skipped,
        batches=batches,
        pending_left=await _pending_left(factory, crm_dataset_id, lead_dataset_id),
    )
    _log.info(
        "sync.lead_match.run",
        crm_dataset_id=crm_dataset_id,
        attempted=report.attempted,
        matched=report.matched,
        no_match=report.no_match,
        no_email=report.no_email,
        errors=report.errors,
        skipped=report.skipped,
        batches=report.batches,
        pending_left=report.pending_left,
    )
    return report
