"""Per-source sync jobs: entities, daily metrics, segments, leads, lead matching and CRM.

AAP 0.5.1 ("Sync jobs and snapshots"), 0.3.2 (data flow, sync schedule, fetch policy,
refresh after change) and 0.3.3 (per-source calls, safe absence, lead-to-contact matching,
contact refresh, associations, deal search, Google-to-CRM link). Every job reads through one
connector client, writes only into its own dataset (decision 8: every row carries the job's
``dataset_id``, so simulated and live rows never join) and is read-only towards the
platforms: no job writes to LinkedIn, Google Ads or HubSpot (AAP 0.7.1).

=======================  ==========================================================  ======
Job                      What it reads and writes                                    Every
=======================  ==========================================================  ======
:class:`EntitySyncJob`   Full current entity state: Google Q-G1, Q-G2/Q-G2b, Q-G4,   3 h
                         Q-G7, Q-G8; LinkedIn accounts, campaign groups, campaigns
                         (all statuses), creatives with their posts, lead forms. One
                         snapshot per entity, the baseline while it is open, and the
                         discrepancy check, all in one transaction.
:class:`MetricsSyncJob`  Daily facts on each source's native dates: incremental      6 h
                         (trailing 3 days), restatement (Google 30, LinkedIn 14      03:00
                         days) or backfill (``BACKFILL_DAYS``, LinkedIn capped at
                         180). Upserts overwrite, so restated data wins, and a
                         complete re-fetch drops the rows it no longer returns.
:class:`SegmentSyncJob`  LinkedIn demographic aggregates for each exact preset       04:00
                         window ending on the default window end; never summed from
                         daily rows. A complete re-fetch drops the values it no
                         longer returns.
:class:`LeadSyncJob`     LinkedIn lead responses since the last success minus 1 h;    3 h
                         test leads never stored, emails only as SHA-256; every lead
                         staged for matching. A lead with no stored ad account fails
                         the attempt, so the cursor stays before it.
:class:`LeadMatchJob`    Due ``lead_match`` rows of one CRM dataset
                         (``sync/lead_match.run_matching``).
:class:`CrmSyncJob`      Google-touch discovery (primary only, when configured),     3 h
                         matching, contact refresh, the window deal search,
                         associations with their target reads, and freshness.
=======================  ==========================================================  ======

Shared plumbing (:class:`_JobBase`): one ``sync_run`` row per freshness dataset a job writes,
inserted ``running`` at start (the scheduler's due check reads ``started_at``) and closed at
finish, a row whose completion rolled back closed as failed (completions reach the job's
bookkeeping only once committed); ``sync.job.start`` and exactly one ``sync.job.finish`` per
run, also when it raises or is cancelled (source, job, dataset, rows, calls, completeness,
duration; never an email, a secret or a token); the dataset's writability check (a dataset
retired mid-job ends the job quietly as failed); and error hand-off. Only
``ConnectorError`` subclasses are handed off, and every one goes to
:meth:`~ads_engine.sync.mode.SourceModeController.on_error`, which alone decides what it means
(AAP 0.3.3 fallback table). ``on_error`` is never called while a transaction of this module is
open, because it takes the ``source_connection`` lock that a concurrent activation takes before
the dataset guard this module holds. A database integrity violation (``IntegrityError``) is no
connector error and is never handed off, retried or simulated: the job records it as a failed
attempt with sanitized diagnostics (:class:`IntegrityFailure`) on its unsettled freshness rows
and ``sync_run`` rows, and returns a failed result. Every other non-connector error propagates.
For an **active** dataset, every transaction that records
a success, or a failed attempt together with data it wrote or deleted, queues one
``analysis_request`` with its freshness write, so the job's last write is always followed by a
pending request (AAP 0.3.2; the worker coalesces requests within 60 s), and a stand-alone
matching pass that attempted rows queues one after it; a building dataset queues nothing, its
activation does.

Publication. A job reads a dataset's whole scope first and then publishes it in one
transaction: its fact or CRM rows with their complete-scope deletions, a Google dataset's
checkpoint, the freshness outcome, the ``sync_run`` close and the ``analysis_request``. No
transaction is held open across a connector call, and a cancellation, a database error or a
bug before that commit leaves the dataset's stored rows and its checkpoint as they were and
queues no request; its freshness stands too, but for the failed attempt an integrity
violation records (above).

Interruptions. Connectors return what they read when a ``TransientError`` survives their
retries, with the error in the result's ``interrupted_by``. A Google call-budget deferral is
recorded as an ``incomplete`` success whose reason starts with
:data:`~ads_engine.sync.freshness.REASON_DEFERRED`, and the unread work is kept as the
freshness row's checkpoint in the same transaction, so the dataset's next run reads those
scopes first and the dataset stays ``incomplete`` until they finish (AAP 0.3.3 "Budget");
calls beyond the cap therefore move to a later 24 h window instead of re-reading the same
prefix. Any other interruption of a dated dataset or of a CRM dataset (search, contact
refresh, associations, target reads) is recorded as a failed attempt, so the dataset keeps
its last good data and turns stale (AAP 0.3.3 transient row); what was read is still
written, with its per-object unknown state, in the transaction that records the failed
attempt and queues the rerun, and a Google dataset's unread scopes are kept the same way.
The error is then handed to ``on_error``, which folds the same failure into those rows
(only ``last_attempt_at`` and the reason can move) and decides its consequence. An
interrupted matching pass is handed over with no dataset, its rows already
retried ``error``, also when a later batch of the pass raises (before the raised error,
whose consequence stands). An interrupted full-state entity read is ``incomplete`` with its
reason: no removal or disappearance is inferred from it (AAP 0.3.3 "Safe absence"), and
the next entity sync reads only what is still outstanding.

Google checkpoints (``dataset_freshness.checkpoint``, version 1; scopes are
:func:`~ads_engine.connectors.google_n8n.partition.scope_to_json` objects)::

    google.metrics_daily, google.search_terms:
        {"version": 1,
         "pending": {"Q-G5": [{"scope": {"start": "2026-09-01", "end": "2026-09-14"},
                               "deferred_at": "2026-09-14T06:00:00+00:00"},
                              {"scope": {"start": "2026-09-15", "end": "2026-09-15",
                                         "ad_group_id": 123, "last_id": 9999},
                               "deferred_at": "2026-09-15T00:00:00+00:00"}]},
         "covered_from": "2026-09-01", "reasons": []}
    google.entities:
        {"version": 1, "started_at": "2026-10-01T06:00:00+00:00",
         "pending": {"Q-G8": [{}]},
         "enumerations": {
             "Q-G2": {"query_id": "Q-G2", "pending": [{"ids": [1001, 1002]}],
                      "absent": [7, 1001, 1002], "rereading": true,
                      "reread_rows": [{"campaign": {"id": "7", "status": "REMOVED", ...}}]},
             "Q-G4": {"query_id": "Q-G4", "pending": [{"last_id": 9999}],
                      "absent": [12], "rereading": false, "reread_rows": []}},
         "reasons": []}

``pending`` holds unread scopes per query in reading order. A metrics entry pairs its scope
with ``deferred_at``, the ``now`` of the run that first deferred that work; a partition later
deferred out of a stored scope keeps the stored scope's instant, and so does a scope merged in
from a concurrent run. The next metrics run reads every stored scope of both datasets as
stored, oldest deferral first, before any own range. ``covered_from`` is the first native
date the interrupted metrics cycle vouches for, ``started_at`` when the entity cycle began,
``enumerations`` each unfinished safe-absence enumeration
(:class:`~ads_engine.connectors.google_n8n.partition.EnumerationProgress`; an interrupted
Q-G2b re-read keeps its unread chunks, the raw rows of the chunks already re-read and every
candidate, so the next sync reads only those chunks), and ``reasons``
the permanent shortfalls (result limits) met so far, which a drained checkpoint still
reports. A checkpoint that cannot be read is logged and ignored: the job then falls back to
a full read (metrics widen to the recorded range when it needs repair).

Entry points: :func:`run_source_jobs` (the scheduler's due jobs and ``sync_request`` rows, and
``ads-engine sync``), :func:`backfill_dataset` (the build in ``sync/mode.py``) and
:func:`refresh_crm_dataset` (a live LinkedIn build's CRM sync of the HubSpot primary its
leads were matched in after its backfill, also in ``sync/mode.py``). Tests and
tools can run one job directly on injected clients through :meth:`JobContext.for_dataset`::

    async with controller.open_clients("google", "simulated") as clients:
        ctx = await JobContext.for_dataset(controller, dataset_id, clients)
        result = await EntitySyncJob(ctx).run()
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
from sqlalchemy import (
    ARRAY,
    BigInteger,
    ColumnElement,
    any_,
    column,
    delete,
    event,
    exists,
    literal,
    select,
    update,
)
from sqlalchemy import values as sql_values
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute, Session, SessionTransaction

from ads_engine.analysis.runner import window_inputs
from ads_engine.analysis.thresholds import CrmBindings, Thresholds
from ads_engine.analysis.window import default_end
from ads_engine.connectors.errors import (
    CallBudgetExhausted,
    ConnectorError,
    QueryDefect,
    TransientError,
)
from ads_engine.connectors.google_n8n.parser import (
    AdGroupRow,
    CampaignRow,
    ConversionActionRow,
    GoogleAdsRow,
    KeywordRow,
    parse_ad_group,
    parse_campaign,
    parse_campaign_daily,
    parse_conversion_action,
    parse_customer,
    parse_keyword,
    parse_keyword_view,
    parse_search_term,
)
from ads_engine.connectors.google_n8n.partition import (
    EnumerationProgress,
    FetchOutcome,
    PartitionedFetcher,
    enumerate_campaigns,
    enumerate_entities,
    interruption_reason,
    scope_from_json,
    scope_to_json,
)
from ads_engine.connectors.hubspot.client import (
    AssociationFetch,
    ContactBatch,
    HubSpotClient,
    TargetRead,
)
from ads_engine.connectors.hubspot.parser import (
    ContactPropertyMap,
    ContactRow,
    DealRow,
    MeetingRow,
    parse_contact,
    parse_deal,
)
from ads_engine.connectors.hubspot.search import (
    CEILING_REASON,
    SHORT_READ_REASON,
    SearchOutcome,
    deal_search_filter,
    google_contacts_filter,
)
from ads_engine.connectors.linkedin.client import LinkedInClient, Listing
from ads_engine.connectors.linkedin.parser import (
    PREFIX_ACCOUNT,
    PREFIX_CAMPAIGN,
    PREFIX_CAMPAIGN_GROUP,
    SEGMENT_FACETS,
    SEGMENT_PIVOTS,
    CreativeDailyRow,
    CreativeRow,
    FormVersionMap,
    LeadFormRow,
    Money,
    ParsedLead,
    SegmentRow,
    sponsored_account_urn,
    urn_id,
)
from ads_engine.connectors.linkedin.parser import CampaignGroupRow as LinkedInGroupRow
from ads_engine.connectors.linkedin.parser import CampaignRow as LinkedInCampaignRow
from ads_engine.db import repositories
from ads_engine.db.base import Base
from ads_engine.db.models.accounts import (
    AdAccount,
    AdGroup,
    Budget,
    Campaign,
    CampaignGroup,
    ConversionAction,
    Creative,
    Keyword,
    LeadForm,
    LeadFormVersion,
)
from ads_engine.db.models.crm import (
    ASSOCIATION_FETCH_ERROR,
    ASSOCIATION_FETCH_OK,
    ASSOCIATION_FETCH_OK_EMPTY,
    ASSOCIATION_FETCH_PARTIAL,
    CONTACT_READ_ERROR,
    CONTACT_READ_OK,
    CRM_OBJECT_CONTACT,
    CRM_OBJECT_DEAL,
    CRM_OBJECT_MEETING,
    LEAD_MATCH_ERROR,
    LEAD_MATCH_MATCHED,
    LEAD_MATCH_NO_MATCH,
    LEAD_MATCH_PENDING,
    TARGET_READ_ERROR,
    TARGET_READ_OK,
    TARGET_READ_PARTIAL,
    CrmAssociation,
    CrmAssociationFetch,
    CrmContact,
    CrmDeal,
    CrmMeeting,
    Lead,
    LeadMatch,
)
from ads_engine.db.models.facts import (
    FactCampaignDaily,
    FactCreativeDaily,
    FactKeywordDaily,
    FactSearchTermDaily,
    FactSegmentWindow,
)
from ads_engine.db.models.ops import (
    COMPLETENESS_COMPLETE,
    COMPLETENESS_INCOMPLETE,
    DATASET_GOOGLE_ENTITIES,
    DATASET_GOOGLE_METRICS_DAILY,
    DATASET_GOOGLE_SEARCH_TERMS,
    DATASET_HUBSPOT_ASSOCIATIONS,
    DATASET_HUBSPOT_CONTACTS,
    DATASET_HUBSPOT_DEAL_SEARCH,
    DATASET_HUBSPOT_GOOGLE_CONTACTS,
    DATASET_LINKEDIN_ANALYTICS_DAILY,
    DATASET_LINKEDIN_ENTITIES,
    DATASET_LINKEDIN_LEADS,
    DATASET_LINKEDIN_SEGMENTS,
    DATASET_ROLE_PRIMARY,
    DATASET_STATE_ACTIVE,
    DATASET_STATE_BUILDING,
    DATASET_STATE_RETIRED,
    MODE_LIVE,
    SOURCE_HUBSPOT,
    SYNC_RUN_FAILED,
    SYNC_RUN_RUNNING,
    SYNC_RUN_SUCCEEDED,
    DatasetFreshness,
    SourceDataset,
    SyncRun,
)
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.logging import get_logger
from ads_engine.settings import SOURCES, Settings, Source
from ads_engine.sync import freshness, lead_match, snapshots
from ads_engine.sync.datasets import Mode, Role
from ads_engine.sync.freshness import FreshnessOutcome
from ads_engine.sync.mode import (
    LINKEDIN_RETENTION_DAYS,
    ErrorDisposition,
    SourceClients,
    SourceModeController,
)
from ads_engine.sync.snapshots import EnumerationResult, SnapshotRow

__all__ = [
    "CANCEL_CLOSE_TIMEOUT",
    "INCREMENTAL_DAYS",
    "JOB_CRM",
    "JOB_CRM_COMPANION",
    "JOB_ENTITIES",
    "JOB_LEADS",
    "JOB_LEAD_MATCH",
    "JOB_METRICS",
    "JOB_RESTATEMENT",
    "JOB_SEGMENTS",
    "LEAD_OVERLAP",
    "RESTATEMENT_DAYS",
    "SEGMENT_RETRY_AFTER",
    "SOURCE_JOBS",
    "CrmSyncJob",
    "EntitySyncJob",
    "IntegrityFailure",
    "IntegrityKind",
    "JobContext",
    "JobResult",
    "LeadMatchJob",
    "LeadSyncJob",
    "MetricsKind",
    "MetricsSyncJob",
    "SegmentSyncJob",
    "SourceSyncReport",
    "Trigger",
    "backfill_dataset",
    "default_window_end",
    "parse_segment_reason",
    "refresh_crm_dataset",
    "run_source_jobs",
    "segment_reason",
    "segments_due",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Job names and schedule constants
# ---------------------------------------------------------------------------------------------

JOB_ENTITIES: Final = "entities"
"""Full current entity state, snapshots, baseline and discrepancy check (every 3 h)."""

JOB_METRICS: Final = "metrics"
"""Incremental daily facts, the trailing :data:`INCREMENTAL_DAYS` days (every 6 h)."""

JOB_RESTATEMENT: Final = "restatement"
"""The nightly 03:00 restatement: a metrics run over :data:`RESTATEMENT_DAYS`, under its own
name so the scheduler can track its slot apart from the 6-hourly incremental run."""

JOB_SEGMENTS: Final = "segments"
"""LinkedIn demographic aggregates for the preset windows (04:00 and on window change)."""

JOB_LEADS: Final = "leads"
"""LinkedIn lead responses since the last cursor minus :data:`LEAD_OVERLAP` (every 3 h)."""

JOB_CRM: Final = "crm"
"""The HubSpot primary dataset's CRM sync (every 3 h)."""

JOB_CRM_COMPANION: Final = "crm_companion"
"""The ``sync_run`` job name of a companion CRM dataset's sync. It runs inside LinkedIn's lead
run while LinkedIn is simulated, under its own name so it never satisfies the HubSpot
primary's ``crm`` slot in the scheduler's due check."""

JOB_LEAD_MATCH: Final = "lead_match"
"""The ``sync_run`` job name of a stand-alone matching pass (a live LinkedIn backfill)."""

SOURCE_JOBS: Final[Mapping[Source, tuple[str, ...]]] = MappingProxyType(
    {
        "google": (JOB_ENTITIES, JOB_METRICS),
        "linkedin": (JOB_ENTITIES, JOB_METRICS, JOB_SEGMENTS, JOB_LEADS),
        "hubspot": (JOB_CRM,),
    }
)
"""The jobs :func:`run_source_jobs` runs for a source when none are named."""

# Every job a source accepts by name, in the order run_source_jobs runs them.
_JOB_ORDER: Final[tuple[str, ...]] = (
    JOB_ENTITIES,
    JOB_METRICS,
    JOB_RESTATEMENT,
    JOB_SEGMENTS,
    JOB_LEADS,
    JOB_CRM,
)
_ACCEPTED_JOBS: Final[Mapping[Source, frozenset[str]]] = MappingProxyType(
    {
        "google": frozenset({JOB_ENTITIES, JOB_METRICS, JOB_RESTATEMENT}),
        "linkedin": frozenset(
            {JOB_ENTITIES, JOB_METRICS, JOB_RESTATEMENT, JOB_SEGMENTS, JOB_LEADS}
        ),
        "hubspot": frozenset({JOB_CRM}),
    }
)

MetricsKind = Literal["incremental", "restatement", "backfill"]
"""Which date range a :class:`MetricsSyncJob` reads."""

Trigger = Literal["scheduled", "manual", "backfill"]
"""What started a :class:`SourceSyncReport`'s jobs: the schedule, a request or a build."""

INCREMENTAL_DAYS: Final = 3
"""Native days an incremental metrics run reads, ending native today (AAP 0.3.2)."""

RESTATEMENT_DAYS: Final[Mapping[Source, int]] = MappingProxyType({"google": 30, "linkedin": 14})
"""Native days the nightly restatement reads: Google 30 (conversion lag), LinkedIn 14."""

LEAD_OVERLAP: Final = timedelta(hours=1)
"""How far before the last successful lead sync the next one starts (AAP 0.3.2)."""

SEGMENT_RETRY_AFTER: Final = timedelta(minutes=30)
"""A failed segment fetch is not due again until this long after its attempt."""

CANCEL_CLOSE_TIMEOUT: Final = timedelta(seconds=2)
"""The most time a cancelled job spends recording its own ``sync_run`` rows as failed.

Kept short so a job cancelled by a lost scheduler lock ends within the time the scheduler
waits for its cancelled work, ``LOCK_LOST_STOP_TIMEOUT``, before it aborts the process.
``sync.scheduler`` checks at import that this bound is shorter than that timeout, and that
its lock check interval, check timeout and that timeout together end inside the next holder's
``LOCK_TAKEOVER_GRACE``. Bookkeeping that does not finish in time is abandoned and its rows
stay ``running``.
"""

# Google partition scope keys, as connectors/google_n8n/queries.py names them ("start", "end"
# of a dated template). queries.py is not imported here; the values are the wire contract of
# PartitionedFetcher.partitioned_fetch's scope argument.
_SCOPE_START: Final = "start"
_SCOPE_END: Final = "end"

# Minimal stand-ins for entities that facts or leads reference before any entity sync has
# stored them (a campaign removed before tracking still has spend; metrics carry no status
# filter). An entity sync that later returns the entity overwrites every field.
_STUB_STATUS: Final = "ended"
_STUB_RAW_STATUS: Final = "UNKNOWN"
# Google's own status for a removed ad group or keyword: the only one Q-G4 and Q-G7 exclude,
# written to tracked rows a complete enumeration no longer returns.
_REMOVED_RAW_STATUS: Final = "REMOVED"
_UTC_ZONE: Final = "UTC"
_ID_CHUNK: Final = 1_000
_ERROR_MAX: Final = 500
# The ``sync_run.error`` of rows a run left open when it raised or was cancelled.
_REASON_UNEXPECTED: Final = "job failed unexpectedly"
_REASON_CANCELLED: Final = "job cancelled"

# HubSpot API object types (plural, as paths name them) and the singular names stored.
_CONTACTS: Final = "contacts"
_MEETINGS: Final = "meetings"
_DEALS: Final = "deals"
_SINGULAR: Final[Mapping[str, str]] = MappingProxyType(
    {_CONTACTS: CRM_OBJECT_CONTACT, _MEETINGS: CRM_OBJECT_MEETING, _DEALS: CRM_OBJECT_DEAL}
)

_SEGMENT_REASON_RE: Final = re.compile(
    r"windows: (?P<presets>[1-9][0-9]*(?:,[1-9][0-9]*)*) days ending "
    r"(?P<end>[0-9]{4}-[0-9]{2}-[0-9]{2})"
)
_REASON_SEPARATOR: Final = "; "


# ---------------------------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobContext:
    """Everything one job needs: the dataset it writes, its clients, configuration and clock.

    Attributes:
        controller: The source-mode controller every connector error is handed to.
        source: The source whose dataset the job writes (``hubspot`` for a CRM dataset,
            primary or companion).
        dataset_id: The dataset written: a primary, or for :class:`LeadMatchJob` and
            :class:`CrmSyncJob` the CRM dataset (primary or companion).
        mode: The dataset's mode; the clients are of the same mode.
        role: The dataset's role, ``primary`` or ``companion``.
        active: Whether the dataset is ``active`` (``False`` while ``building``); only an
            active dataset's jobs queue an ``analysis_request``.
        clients: The connector client of ``source`` in ``mode``.
        settings: The process settings.
        thresholds: The configuration snapshot the job runs on (window presets and default
            days); the entry points read it once per run and share it with every context the
            run builds.
        now: The run's instant, timezone-aware; every attempt of the run is stamped with it.
        sessionmaker: The session factory every write goes through.
    """

    controller: SourceModeController
    source: Source
    dataset_id: int
    mode: Mode
    role: Role
    active: bool
    clients: SourceClients
    settings: Settings
    thresholds: Thresholds
    now: datetime
    sessionmaker: async_sessionmaker[AsyncSession]

    def __post_init__(self) -> None:
        """Reject a naive clock and clients of another source or mode."""
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("JobContext.now must be timezone-aware")
        if self.clients.source != self.source or self.clients.mode != self.mode:
            raise ValueError(
                f"clients are {self.clients.source}/{self.clients.mode}, but dataset "
                f"{self.dataset_id} is {self.source}/{self.mode}"
            )

    @classmethod
    async def for_dataset(
        cls,
        controller: SourceModeController,
        dataset_id: int,
        clients: SourceClients,
        *,
        now: datetime | None = None,
        thresholds: Thresholds | None = None,
    ) -> JobContext:
        """Build the context of one dataset from its stored row.

        Args:
            controller: The controller whose settings, sessions and clock apply, and whose
                configuration does when ``thresholds`` is omitted.
            dataset_id: The dataset the job writes.
            clients: Clients of the dataset's source and mode, normally from
                :meth:`SourceModeController.open_clients`; tests inject their own.
            now: The run's instant; the controller's clock when omitted.
            thresholds: The configuration snapshot the job runs on; one read of
                :attr:`SourceModeController.thresholds` when omitted. A caller that decided
                on a snapshot (the scheduler judging segments due) or builds several contexts
                for one run passes it, so one run never mixes configuration generations.

        Returns:
            The context.

        Raises:
            ads_engine.sync.datasets.DatasetNotFoundError: If the dataset does not exist.
            ValueError: If the clients do not match the dataset's source and mode.
            ads_engine.analysis.thresholds.ConfigError: If ``thresholds`` is omitted and the
                configuration fails to load.
        """
        dataset = await controller.datasets.get(dataset_id)
        return cls(
            controller=controller,
            source=_as_source(dataset.source),
            dataset_id=dataset.id,
            mode=_as_mode(dataset.mode),
            role=_as_role(dataset.role),
            active=dataset.state == DATASET_STATE_ACTIVE,
            clients=clients,
            settings=controller.settings,
            thresholds=thresholds if thresholds is not None else controller.thresholds,
            now=now if now is not None else controller.now(),
            sessionmaker=controller.sessionmaker,
        )


IntegrityKind = Literal[
    "foreign_key_violation",
    "check_violation",
    "unique_violation",
    "not_null_violation",
    "exclusion_violation",
    "integrity_violation",
]
"""The class of a database integrity violation, from its SQLSTATE (:class:`IntegrityFailure`)."""

# PostgreSQL's integrity-constraint SQLSTATEs (class 23); any other is ``integrity_violation``.
_INTEGRITY_KINDS: Final[Mapping[str, IntegrityKind]] = MappingProxyType(
    {
        "23503": "foreign_key_violation",
        "23514": "check_violation",
        "23505": "unique_violation",
        "23502": "not_null_violation",
        "23P01": "exclusion_violation",
    }
)
_INTEGRITY_OTHER: Final[IntegrityKind] = "integrity_violation"


@dataclass(frozen=True, slots=True)
class IntegrityFailure:
    """A database integrity violation that ended a job, as sanitized diagnostics only.

    A write the schema refused (a foreign-key, ``CHECK``, unique, not-null or exclusion
    constraint) is a defect of the job's own data, not a platform error: the job records it
    as a failed attempt and returns a failed :class:`JobResult` instead of raising, and it is
    never handed to ``on_error``, retried or simulated (:meth:`_JobBase.run`). Only the
    violation's class, its SQLSTATE and the names of the table and constraint are kept. The
    driver's message and detail, the statement and its parameters can carry key values, so
    none of them is ever read.

    Attributes:
        kind: The violation's class: ``foreign_key_violation`` (SQLSTATE 23503),
            ``check_violation`` (23514), ``unique_violation`` (23505), ``not_null_violation``
            (23502), ``exclusion_violation`` (23P01), or ``integrity_violation`` for any other
            or unreported SQLSTATE.
        sqlstate: The SQLSTATE PostgreSQL reported, or ``None`` when the driver gave none.
        table: The table whose constraint refused the write, or ``None`` when not reported.
        constraint: The refusing constraint's name, or ``None`` when not reported.
    """

    kind: IntegrityKind
    sqlstate: str | None
    table: str | None
    constraint: str | None

    @classmethod
    def from_error(cls, error: IntegrityError) -> IntegrityFailure:
        """Read the sanitized diagnostics of a violation; never its text or parameters.

        Args:
            error: The violation SQLAlchemy raised; its ``orig`` is the driver's error, a
                :class:`psycopg.Error` whose diagnostics name the table and constraint.

        Returns:
            The diagnostics; a name the driver did not report is ``None``, and an error from
            another driver yields ``integrity_violation`` with no names.
        """
        orig = error.orig
        if not isinstance(orig, psycopg.Error):
            return cls(kind=_INTEGRITY_OTHER, sqlstate=None, table=None, constraint=None)
        diag = orig.diag
        sqlstate = orig.sqlstate or diag.sqlstate
        kind = _INTEGRITY_KINDS.get(sqlstate, _INTEGRITY_OTHER) if sqlstate else _INTEGRITY_OTHER
        return cls(
            kind=kind,
            sqlstate=sqlstate,
            table=diag.table_name,
            constraint=diag.constraint_name,
        )

    @property
    def reason(self) -> str:
        """The one-line reason a failed job records (``sync_run.error``, the freshness reason).

        For example ``database integrity violation (foreign_key_violation) on
        fact_campaign_daily: constraint fk_fact_campaign_daily_campaign_id_campaign (SQLSTATE
        23503)``; the parts the driver did not report are left out.
        """
        text = f"database integrity violation ({self.kind})"
        if self.table:
            text += f" on {self.table}"
        if self.constraint:
            text += f": constraint {self.constraint}"
        if self.sqlstate:
            text += f" (SQLSTATE {self.sqlstate})"
        return text

    def to_json(self) -> dict[str, Any]:
        """Return the diagnostics as plain JSON; names and codes only, never a value."""
        return {
            "kind": self.kind,
            "sqlstate": self.sqlstate,
            "table": self.table,
            "constraint": self.constraint,
        }


@dataclass(frozen=True, slots=True)
class JobResult:
    """What one job did; counts and reasons only, never ids of people, emails or secrets.

    Attributes:
        job: The job name (:data:`JOB_ENTITIES` …, :data:`JOB_CRM_COMPANION`,
            :data:`JOB_LEAD_MATCH`).
        source: The source of the dataset written.
        dataset_id: The dataset written.
        status: ``succeeded`` when every freshness dataset of the job recorded a success
            (complete or incomplete); ``failed`` when the job raised, a dataset's refresh
            failed and kept its last good data, or a database integrity violation ended it.
        rows: Rows written (entities, facts, leads, contacts, associations, …).
        calls: Connector calls made.
        freshness: The outcome recorded per freshness dataset name; a dataset whose refresh
            failed carries the failure outcome recorded for it (by ``on_error`` for a
            transient error, by the job otherwise).
        disposition: What ``on_error`` decided for the last connector error the job handed
            over, or ``None`` when there was none.
        error: The connector error's reason, the integrity violation's sanitized reason
            (:attr:`IntegrityFailure.reason`), or why the job could not write; ``None`` when
            nothing failed.
        duration_ms: Wall-clock duration.
        integrity: The sanitized diagnostics of the database integrity violation that ended
            the job, or ``None`` when none did.
    """

    job: str
    source: Source
    dataset_id: int
    status: Literal["succeeded", "failed"]
    rows: int
    calls: int
    freshness: Mapping[str, FreshnessOutcome]
    disposition: ErrorDisposition | None
    error: str | None
    duration_ms: int
    integrity: IntegrityFailure | None = None

    @property
    def completeness(self) -> str:
        """``complete`` when every recorded outcome is complete, else ``incomplete``/``unknown``."""
        values = [outcome.completeness for outcome in self.freshness.values()]
        if self.status == "failed" and not values:
            return "unknown"
        if "unknown" in values:
            return "unknown"
        if "incomplete" in values:
            return "incomplete"
        return "complete"

    @property
    def access_denied(self) -> bool:
        """Whether the job ended on an ``AccessDenied`` (switched or ignored)."""
        return self.disposition in ("switched_to_simulated", "ignored")

    def to_json(self) -> dict[str, Any]:
        """Return the result as plain JSON for ``ads-engine sync``; no credentials included."""
        return {
            "job": self.job,
            "source": self.source,
            "dataset_id": self.dataset_id,
            "status": self.status,
            "rows": self.rows,
            "calls": self.calls,
            "completeness": self.completeness,
            "freshness": {
                name: {
                    "succeeded": outcome.succeeded,
                    "completeness": outcome.completeness,
                    "covered_from": _iso(outcome.covered_from),
                    "covered_through": _iso(outcome.covered_through),
                    "reason": outcome.reason,
                }
                for name, outcome in sorted(self.freshness.items())
            },
            "disposition": self.disposition,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "integrity": None if self.integrity is None else self.integrity.to_json(),
        }


@dataclass(frozen=True, slots=True)
class SourceSyncReport:
    """The jobs one :func:`run_source_jobs` or :func:`backfill_dataset` call ran, in order.

    Attributes:
        source: The source synced.
        trigger: ``scheduled``, ``manual`` (a ``sync_request`` row or ``ads-engine sync``) or
            ``backfill`` (a dataset build).
        results: Each job's result, in the order run.
        dataset_id: The dataset synced, or ``None`` when the source has no active dataset.
        mode: That dataset's mode, or ``None``.
        reason: Why no job ran (no active dataset, or a live dataset that may not be synced
            now); ``None`` when the jobs ran.
    """

    source: Source
    trigger: Trigger
    results: tuple[JobResult, ...] = ()
    dataset_id: int | None = None
    mode: Mode | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the jobs ran and every one succeeded."""
        return self.reason is None and all(result.status == "succeeded" for result in self.results)

    @property
    def has_defect(self) -> bool:
        """Whether a job failed on a defect (the CLI's non-zero exit).

        A defect is a ``QueryDefect`` the connectors raised, or a database integrity
        violation that ended the job (:attr:`JobResult.integrity`); neither is retried.
        """
        return any(
            result.disposition == "defect" or result.integrity is not None
            for result in self.results
        )

    def to_json(self) -> dict[str, Any]:
        """Return the report as plain JSON for ``ads-engine sync``; no credentials included."""
        return {
            "source": self.source,
            "trigger": self.trigger,
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "reason": self.reason,
            "ok": self.ok,
            "jobs": [result.to_json() for result in self.results],
        }


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _as_source(value: str) -> Source:
    """Narrow a stored source name to :data:`~ads_engine.settings.Source`."""
    for source in SOURCES:
        if value == source:
            return source
    raise ValueError(f"unknown source {value!r}")


def _as_mode(value: str) -> Mode:
    """Narrow a stored mode to :data:`~ads_engine.sync.datasets.Mode`."""
    for mode in get_args(Mode):
        if value == mode:
            narrowed: Mode = mode
            return narrowed
    raise ValueError(f"unknown mode {value!r}")


def _as_role(value: str) -> Role:
    """Narrow a stored role to :data:`~ads_engine.sync.datasets.Role`."""
    for role in get_args(Role):
        if value == role:
            narrowed: Role = role
            return narrowed
    raise ValueError(f"unknown dataset role {value!r}")


def _iso(value: date | None) -> str | None:
    """Return a date as ISO text, or ``None``."""
    return None if value is None else value.isoformat()


def _utc_midnight(day: date) -> datetime:
    """Return 00:00 UTC of ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _truncate(text: str, limit: int = _ERROR_MAX) -> str:
    """Shorten ``text`` to ``limit`` characters with an ellipsis."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunks[T](items: Sequence[T], size: int = _ID_CHUNK) -> Iterator[Sequence[T]]:
    """Yield ``items`` in slices of at most ``size``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _join_reasons(reasons: Iterable[str | None]) -> str | None:
    """Join distinct reasons with ``"; "``, repairable ones first; ``None`` when there are none."""
    distinct = list(dict.fromkeys(reason for reason in reasons if reason))
    if not distinct:
        return None
    distinct.sort(key=lambda reason: 0 if freshness.is_repairable_reason(reason) else 1)
    return _REASON_SEPARATOR.join(distinct)


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``"<count> <noun>"`` with the noun agreeing in number."""
    return f"{count} {singular if count == 1 else plural}"


def _success(
    now: datetime,
    *,
    covered: tuple[date, date] | None,
    reasons: Iterable[str | None],
) -> FreshnessOutcome:
    """Build a success outcome: complete without reasons, else incomplete with them joined.

    ``repairable`` is set exactly when the joined reason starts with a repairable prefix
    (:data:`~ads_engine.sync.freshness.REASON_DEFERRED` or ``REASON_INTERRUPTED``): the
    shortfall is unread work a later run can still read (a Google checkpoint's scopes, or the
    recorded range :func:`~ads_engine.sync.freshness.needs_repair` re-reads otherwise).
    """
    reason = _join_reasons(reasons)
    covered_from, covered_through = covered if covered is not None else (None, None)
    return FreshnessOutcome.success(
        now,
        covered_from=covered_from,
        covered_through=covered_through,
        complete=reason is None,
        reason=reason,
        repairable=reason is not None and freshness.is_repairable_reason(reason),
    )


def _zone(name: str | None) -> ZoneInfo:
    """Return the IANA zone ``name``, UTC when it is missing or unknown (logged)."""
    if not name:
        return ZoneInfo(_UTC_ZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        _log.warning("sync.job.timezone_unknown", timezone=name, used=_UTC_ZONE)
        return ZoneInfo(_UTC_ZONE)


def _span_days(settings: Settings, source: Source) -> int:
    """The backfill span in days: ``BACKFILL_DAYS``, LinkedIn capped at its daily retention."""
    if source == "linkedin":
        return min(settings.backfill_days, LINKEDIN_RETENTION_DAYS)
    return settings.backfill_days


def _crm_span_days(settings: Settings) -> int:
    """The CRM scope in days: the lead staging range, ``BACKFILL_DAYS`` capped at 180."""
    return min(settings.backfill_days, LINKEDIN_RETENTION_DAYS)


def _failure_text(error: ConnectorError) -> str:
    """The text a failed job records for a connector error (``sync_run.error``, freshness).

    The error's one-line ``reason``; a ``QueryDefect`` also names its query id when the
    reason does not, so the failed ``sync_run`` row says which query to fix (AAP 0.3.3 "the
    job fails and is logged with the query id").
    """
    reason = error.reason
    if isinstance(error, QueryDefect) and error.query_id and error.query_id not in reason:
        return f"{reason} (query {error.query_id})"
    return reason


def _key_value(key: tuple[Any, ...]) -> Any:
    """Return the last column of a natural-key tuple (the external id or URN)."""
    return key[-1]


# ---------------------------------------------------------------------------------------------
# Segment windows (AAP 0.3.2 "LinkedIn demographic segments"; AAP 0.5.4.1)
# ---------------------------------------------------------------------------------------------


def segment_reason(presets: Iterable[int], end: date) -> str:
    """Return the ``linkedin.segments`` reason recording which windows were fetched.

    Example:
        >>> segment_reason((90, 14, 28, 56), date(2026, 10, 5))
        'windows: 14,28,56,90 days ending 2026-10-05'

    Args:
        presets: The preset window lengths in days; repeats are dropped and the rest sorted.
        end: The default window end every preset window ended on.

    Returns:
        The reason; :func:`parse_segment_reason` is its exact inverse.

    Raises:
        ValueError: When ``presets`` is empty or holds a value that is not a positive int.
    """
    values = sorted(set(presets))
    if not values:
        raise ValueError("segment_reason needs at least one preset window")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"preset windows must be positive integers, found {value!r}")
    return f"windows: {','.join(str(value) for value in values)} days ending {end.isoformat()}"


def parse_segment_reason(reason: str | None) -> tuple[frozenset[int], date] | None:
    """Return the preset windows and end a ``linkedin.segments`` reason records.

    The exact inverse of :func:`segment_reason`. Only the text before the first ``"; "`` is
    read: the freshness merge appends ``"; last attempt failed: …"`` to an incomplete row's
    reason, and an incomplete fetch records ``"<windows>; <shortfall>"``.

    Example:
        >>> parse_segment_reason("windows: 14,28,56,90 days ending 2026-10-05")
        (frozenset({56, 90, 28, 14}), datetime.date(2026, 10, 5))

    Args:
        reason: A stored ``dataset_freshness.reason``, or ``None``.

    Returns:
        ``(presets, end)``, or ``None`` when the reason does not record segment windows.
    """
    if reason is None:
        return None
    head = reason.split(_REASON_SEPARATOR, 1)[0]
    match = _SEGMENT_REASON_RE.fullmatch(head)
    if match is None:
        return None
    try:
        end = date.fromisoformat(match.group("end"))
    except ValueError:
        return None
    presets = frozenset(int(value) for value in match.group("presets").split(","))
    return presets, end


async def default_window_end(
    session: AsyncSession, *, now: datetime, thresholds: Thresholds | None = None
) -> date:
    """Return the default window end the segment windows and the deal search end on.

    The earlier of yesterday in the Google account's time zone and yesterday UTC (AAP
    0.5.4.1), from the active datasets' day boundaries
    (:func:`ads_engine.analysis.runner.window_inputs`), so segment windows always match the
    default run's window, first start included.

    Args:
        session: An open session; only read.
        now: The instant to judge at; timezone-aware.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when omitted.

    Returns:
        The default window end as a native date.
    """
    _, sources = await window_inputs(session, thresholds=thresholds)
    return default_end(sources, now=now)


async def segments_due(
    session: AsyncSession, *, now: datetime, thresholds: Thresholds | None = None
) -> bool:
    """Return whether LinkedIn's demographic segments must be fetched now.

    Checked by the scheduler on every wake (AAP 0.3.2). Due when the active LinkedIn
    dataset has no ``linkedin.segments`` row, its coverage ends before the current default
    window end, or the preset windows its reason records differ from ``window.presets``. Not
    due while the latest attempt failed less than :data:`SEGMENT_RETRY_AFTER` ago, so a failed
    or unfinished fetch is retried at a bounded rate rather than on every wake.

    Args:
        session: An open session; only read.
        now: The instant to judge at; timezone-aware.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when omitted, so a
            changed ``window.presets`` is seen at once.

    Returns:
        ``True`` when :class:`SegmentSyncJob` should run.
    """
    config = thresholds if thresholds is not None else Thresholds.load()
    dataset_id = await repositories.active_dataset_id(session, "linkedin")
    if dataset_id is None:
        return False
    row = (await freshness.load_freshness(session, dataset_id)).get(DATASET_LINKEDIN_SEGMENTS)
    if row is None:
        return True
    last_attempt_failed = row.last_attempt_at is not None and (
        row.last_success_at is None or row.last_attempt_at > row.last_success_at
    )
    if (
        last_attempt_failed
        and row.last_attempt_at is not None
        and now - row.last_attempt_at < SEGMENT_RETRY_AFTER
    ):
        return False
    end = await default_window_end(session, now=now, thresholds=config)
    if row.covered_through is None or row.covered_through < end:
        return True
    parsed = parse_segment_reason(row.reason)
    if parsed is None:
        return True
    presets, recorded_end = parsed
    return presets != frozenset(config.presets) or recorded_end < end


# ---------------------------------------------------------------------------------------------
# Job base: sync_run rows, logging, freshness, analysis requests and error hand-off
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Interruption:
    """A transient error a connector returned instead of raising, for ``on_error`` later.

    Attributes:
        error: The error that ended the read early.
        datasets: The freshness datasets whose refresh it failed; empty when the shortfall
            was recorded as an ``incomplete`` success and only the connection's health is
            reported.
    """

    error: ConnectorError
    datasets: tuple[str, ...]


class _JobBase(ABC):
    """The plumbing every job shares (module docstring, "Shared plumbing").

    Subclasses implement :meth:`freshness_datasets` and :meth:`_execute`. Inside
    ``_execute`` they read a dataset's scope with no transaction open, then write it in one
    short transaction that records its outcome with :meth:`_complete`: a success, or the
    failed attempt of an interrupted read together with the rows it did read (module
    docstring, "Publication"). Transient interruptions are noted with :meth:`_interrupted`
    once that transaction committed and handed to :meth:`SourceModeController.on_error` once
    no transaction is open.
    """

    job: str = ""

    def __init__(self, ctx: JobContext) -> None:
        """Bind the job to its context; nothing is read or sent until :meth:`run`."""
        self.ctx = ctx
        self._interruptions: list[_Interruption] = []
        self._failed: dict[str, str] = {}
        self._freshness: dict[str, FreshnessOutcome] = {}
        self._rows: dict[str, int] = {}
        self._total_rows = 0
        self._calls = 0
        self._runs: dict[str | None, list[int]] = {}
        self._closed: set[str | None] = set()
        self._disposition: ErrorDisposition | None = None
        self._error: str | None = None
        self._integrity: IntegrityFailure | None = None

    # -- what subclasses declare ---------------------------------------------------------

    @abstractmethod
    def freshness_datasets(self) -> tuple[str, ...]:
        """The freshness datasets the job writes; one ``sync_run`` row each."""

    def sync_run_jobs(self) -> tuple[str, ...]:
        """The ``sync_run.job`` names the job's rows are recorded under; its own name."""
        return (self.job,)

    @abstractmethod
    async def _execute(self) -> None:
        """Read, write and record outcomes (see the class docstring)."""

    # -- run ---------------------------------------------------------------------------

    async def run(self) -> JobResult:
        """Run the job once and return what it did.

        Every run logs one ``sync.job.start`` and exactly one ``sync.job.finish``, whether it
        returns, raises or is cancelled (:meth:`_result`). Every interruption noted while the
        job ran is handed to ``on_error`` on every exit but a cancellation: after a normal
        finish, before a raised connector error, on a retired dataset, before a database
        integrity violation is recorded, and on a best-effort basis before any other
        non-connector error propagates.

        Returns:
            The result. A ``ConnectorError`` the connectors raise is handed to ``on_error``
            after the interruptions noted before it, and returned as a failed result with its
            disposition; a dataset retired while the job ran ends it as failed with that
            reason, its noted interruptions handed over first (the disposition is the last
            hand-over's). A database integrity violation (``IntegrityError``: a foreign-key,
            ``CHECK``, unique, not-null or exclusion constraint refused a write, whose
            transaction rolled back) is a defect of the job's data: it is never handed to
            ``on_error``, retried or simulated. It is returned as a failed result whose
            :attr:`~JobResult.integrity` carries the sanitized diagnostics, after
            :meth:`_attempt` recorded the failed attempt (:meth:`_settle_integrity`).

        Raises:
            Exception: Any other non-connector error (a database error, a bug), unchanged,
                or one raised while an integrity violation was being recorded, after the
                noted interruptions were handed to ``on_error`` (best effort: a database
                error doing so is logged, never raised in its place), the job's still-open
                ``sync_run`` rows were closed as failed (best effort) and ``sync.job.finish``
                was logged ``failed`` with the error's class name; the scheduler logs its
                traceback and the next slot retries.
            asyncio.CancelledError: Unchanged, when the task running the job is cancelled (a
                lost scheduler lock, the worker's graceful shutdown), wherever the job then
                was (:meth:`_close_cancelled`). It is logged as ``sync.job.cancelled``, and
                within :data:`CANCEL_CLOSE_TIMEOUT` the job's still-open ``sync_run`` rows,
                including rows whose close the cancellation rolled back, are closed as failed
                with ``job cancelled``; nothing else is written. Bookkeeping that times out or
                fails is logged as ``sync.job.close_failed`` and abandoned, and a further
                cancellation while it runs propagates at once. ``sync.job.finish`` is then
                logged ``cancelled``.
        """
        ctx = self.ctx
        started = time.perf_counter()
        _log.info(
            "sync.job.start",
            source=ctx.source,
            job=self.job,
            dataset_id=ctx.dataset_id,
            mode=ctx.mode,
            role=ctx.role,
            active=ctx.active,
            rows=0,
            calls=0,
            duration_ms=0,
        )
        try:
            await self._attempt()
        except asyncio.CancelledError as exc:
            # Also a cancellation that lands in a handler of :meth:`_attempt`.
            try:
                await self._close_cancelled(started)
            finally:
                # Logged even when the cleanup is itself cancelled.
                self._result(started, raised=exc)
            raise
        except Exception as exc:
            try:
                await self._close_remaining(_REASON_UNEXPECTED, quiet=True)
            finally:
                # Logged even when the cleanup is itself cancelled or fails.
                self._result(started, raised=exc)
            raise
        return self._result(started)

    async def _close_cancelled(self, started: float) -> None:
        """Log ``sync.job.cancelled`` and close the still-open rows ``job cancelled``, bounded.

        The scheduler cancels and awaits its lane tasks when its lock is lost and must stop
        them inside its own bound, so this bookkeeping is abandoned after
        :data:`CANCEL_CLOSE_TIMEOUT`, or at once when it fails, and logged as
        ``sync.job.close_failed``; a further cancellation propagates at once. A row whose
        close the cancellation rolled back is still open (a dataset counts as closed only
        once its transaction commits), so it is closed here too. Nothing else is written.

        Args:
            started: ``time.perf_counter()`` when the run started.
        """
        ctx = self.ctx
        _log.warning(
            "sync.job.cancelled",
            source=ctx.source,
            job=self.job,
            dataset_id=ctx.dataset_id,
            rows=self._total_rows,
            calls=self._calls,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        try:
            async with asyncio.timeout(CANCEL_CLOSE_TIMEOUT.total_seconds()):
                await self._close_remaining(_REASON_CANCELLED, quiet=True)
        except (TimeoutError, SQLAlchemyError) as exc:
            # Best effort: the cancellation must end the job within its bound.
            _log.warning(
                "sync.job.close_failed",
                source=ctx.source,
                job=self.job,
                dataset_id=ctx.dataset_id,
                error=type(exc).__name__,
            )

    async def _attempt(self) -> None:
        """Open the ``sync_run`` rows, execute, and settle what the run may return (:meth:`run`).

        A ``ConnectorError`` is handed to ``on_error``, a retired dataset ends the job, and a
        database integrity violation is recorded as a failed attempt
        (:meth:`_settle_integrity`); all three close the rows here and return. Anything else
        propagates to :meth:`run`, and so does an error raised while settling one of them.
        """
        ctx = self.ctx
        try:
            await self._open_runs(self.freshness_datasets())
            await self._execute()
        except ConnectorError as exc:
            pending = tuple(
                name for name in self.freshness_datasets() if name not in self._freshness
            )
            # Interruptions noted before the raise failed their own datasets (which is why
            # those are not pending); hand them over first, so their failed attempts are
            # recorded and the raised error's consequence is the one that stands.
            await self._report_interruptions()
            self._disposition = await self._hand_over(exc, pending)
            text = _failure_text(exc)
            self._error = text
            failures = {name: FreshnessOutcome.failure(ctx.now, reason=text) for name in pending}
            if self._disposition != "stale":
                # ``on_error`` records a transient's failure on the named rows itself; a
                # defect or a denial is recorded here, so ``last_attempt_at`` moves on every
                # attempt (AAP 0.5.2) and a due check backing off from a failed attempt
                # (``segments_due``) sees it.
                await self._record_failures(failures)
            # Published once recorded, as completions are once committed.
            for name, outcome in failures.items():
                self._failed[name] = text
                self._freshness[name] = outcome
            await self._close_remaining(text)
        except repositories.DatasetNotWritable as exc:
            # Retired (or discarded) while the job ran: its rows are read-only history now.
            self._error = str(exc)
            _log.info("sync.job.not_writable", source=ctx.source, job=self.job, reason=self._error)
            # The interruptions noted before still happened: ``on_error`` records the
            # connection's health and skips the read-only dataset's freshness rows.
            await self._report_interruptions()
            await self._close_remaining(self._error)
        except IntegrityError as exc:
            # The refused write's transaction rolled back as its ``session.begin()`` exited.
            await self._settle_integrity(exc)
        except Exception:
            # Best effort; the original error then propagates to :meth:`run`, which closes
            # the still-open rows as failed and logs the one ``sync.job.finish``.
            await self._report_interruptions(quiet=True)
            raise
        else:
            await self._report_interruptions()
            await self._close_remaining(None)

    async def _settle_integrity(self, error: IntegrityError) -> None:
        """Record a database integrity violation as the job's failed attempt.

        A constraint refused one of the job's writes, so the job's own data is defective: no
        connector raised it, so it is never handed to ``on_error`` (which decides only what a
        connector error means), never retried and never answered with simulated data. Only
        sanitized diagnostics are kept (:class:`IntegrityFailure`). In order:

        1. ``sync.job.integrity_failed`` is logged at error level with the source, job,
           dataset, the freshness datasets the violation fails, and the violation's kind,
           SQLSTATE, table and constraint; never a value, the driver's text or the statement.
        2. The interruptions noted before it are handed over, so their failed attempts are
           recorded first.
        3. Every freshness dataset of the job with no outcome yet records a failed attempt in
           a fresh transaction (:meth:`_record_failures`): ``last_attempt_at`` moves and the
           reason is the violation's, while coverage, completeness and ``last_success_at``
           stand, so the last good data keeps its standing until it turns stale (AAP 0.5.2,
           freshness updated on every attempt). A first-ever attempt records ``unknown``.
        4. The still-open ``sync_run`` rows are closed ``failed`` with that reason.

        An error raised by any step propagates to :meth:`run`, which closes the rows still
        open and re-raises it.

        Args:
            error: The violation, raised after its transaction rolled back.
        """
        ctx = self.ctx
        failure = IntegrityFailure.from_error(error)
        reason = failure.reason
        pending = tuple(name for name in self.freshness_datasets() if name not in self._freshness)
        _log.error(
            "sync.job.integrity_failed",
            source=ctx.source,
            job=self.job,
            dataset_id=ctx.dataset_id,
            datasets=list(pending),
            kind=failure.kind,
            sqlstate=failure.sqlstate,
            table=failure.table,
            constraint=failure.constraint,
        )
        await self._report_interruptions()
        failures = {name: FreshnessOutcome.failure(ctx.now, reason=reason) for name in pending}
        await self._record_failures(failures)
        # Published once recorded, as completions are once committed.
        for name, outcome in failures.items():
            self._failed[name] = reason
            self._freshness[name] = outcome
        self._error = reason
        self._integrity = failure
        await self._close_remaining(reason)

    def _result(self, started: float, *, raised: BaseException | None = None) -> JobResult:
        """Build the result and log the run's one ``sync.job.finish``.

        Every path of :meth:`run` ends here exactly once, so the event has one shape: source,
        job, dataset, status, rows, calls, completeness, the outcome per freshness dataset,
        disposition, duration and error. Outcomes are those committed (or, for a failure,
        recorded); the event carries counts, names and an exception's class, never an email,
        a secret, a token or an exception's text.

        Args:
            started: ``time.perf_counter()`` when the run started.
            raised: The exception :meth:`run` re-raises after this, if any: the status logged
                is ``cancelled`` for a cancellation and ``failed`` otherwise, and ``error``
                is its class name (``None`` when the run returns).

        Returns:
            The result :meth:`run` returns; discarded when ``raised`` is given.
        """
        ctx = self.ctx
        status: Literal["succeeded", "failed"] = (
            "failed"
            if raised is not None or self._error is not None or self._failed
            else "succeeded"
        )
        result = JobResult(
            job=self.job,
            source=ctx.source,
            dataset_id=ctx.dataset_id,
            status=status,
            rows=self._total_rows,
            calls=self._calls,
            freshness=MappingProxyType(dict(self._freshness)),
            disposition=self._disposition,
            error=self._error if self._error is not None else _join_reasons(self._failed.values()),
            duration_ms=int((time.perf_counter() - started) * 1000),
            integrity=self._integrity,
        )
        log = _log.warning if status == "failed" else _log.info
        log(
            "sync.job.finish",
            source=ctx.source,
            job=self.job,
            dataset_id=ctx.dataset_id,
            status="cancelled" if isinstance(raised, asyncio.CancelledError) else status,
            rows=result.rows,
            calls=result.calls,
            completeness=result.completeness,
            datasets={name: outcome.completeness for name, outcome in self._freshness.items()},
            disposition=result.disposition,
            duration_ms=result.duration_ms,
            error=None if raised is None else type(raised).__name__,
        )
        return result

    # -- helpers for subclasses ------------------------------------------------------------

    def _count(self, dataset: str | None, rows: int) -> None:
        """Add ``rows`` written to a freshness dataset's (and the job's) row count."""
        self._total_rows += rows
        if dataset is not None:
            self._rows[dataset] = self._rows.get(dataset, 0) + rows

    def _interrupted(
        self, error: TransientError, *, dataset: str | None, datasets: Sequence[str] = ()
    ) -> None:
        """Note a transient interruption a connector returned instead of raising.

        Args:
            error: The interruption.
            dataset: The freshness dataset whose refresh it failed, so ``on_error`` records
                the failure and the dataset keeps its last good data and turns stale; or
                ``None`` when the shortfall is recorded as an ``incomplete`` success (a
                call-budget deferral, an incomplete full-state read) or lives outside
                ``dataset_freshness`` (lead matching's retried ``lead_match`` rows), and only
                the connection's health is reported.
            datasets: Further freshness datasets the same interruption failed; all of them
                are handed to ``on_error`` together, in one call.
        """
        names = tuple(dict.fromkeys(name for name in (dataset, *datasets) if name is not None))
        self._interruptions.append(_Interruption(error, names))
        for name in names:
            self._failed[name] = error.reason
            self._freshness[name] = FreshnessOutcome.failure(self.ctx.now, reason=error.reason)

    async def _complete(
        self,
        session: AsyncSession,
        outcomes: Mapping[str, FreshnessOutcome],
        *,
        changed: bool = False,
        rows: Mapping[str, int] | None = None,
    ) -> None:
        """Record freshness outcomes in the transaction that wrote their data.

        Closes the outcomes' ``sync_run`` rows and, when at least one outcome is a success or
        ``changed`` says the transaction wrote or deleted data, queues an ``analysis_request``
        in the same transaction (:meth:`_queue_analysis`; AAP 0.3.2 "a completed entity,
        metrics, segment, lead or CRM sync"). A failed attempt that still stored what its
        interrupted read returned therefore queues the rerun its data needs, while a failed
        attempt that changed nothing queues none. Every such transaction queues its own
        request, so data a later transaction of the job writes is never left behind by a run
        that served an earlier request; the worker coalesces the requests that arrive within
        60 s into one run.

        The job's own bookkeeping learns of the completion only when the transaction commits
        (:meth:`_publish_on_commit`): a completion rolled back by a later failure of its
        transaction is neither reported nor logged, its rows are not counted, and its
        ``sync_run`` rows stay open, so :meth:`run` closes them as failed in a fresh
        transaction.

        Args:
            session: The transaction that wrote the data the outcomes describe.
            outcomes: The outcome per freshness dataset: a success, or a failed attempt.
            changed: Whether the transaction wrote or deleted data; a failed attempt with
                changed data queues the analysis request too.
            rows: Rows the transaction wrote per freshness dataset that the job has not
                counted yet. They are included in the closed ``sync_run`` rows and added to
                the job's counts once the transaction commits.

        Raises:
            repositories.DatasetNotWritable: If the dataset was retired meanwhile.
        """
        ctx = self.ctx
        uncounted = dict(rows) if rows is not None else {}
        dataset = await repositories.ensure_writable(session, ctx.dataset_id)
        for name, outcome in outcomes.items():
            await freshness.record_freshness(session, ctx.dataset_id, name, outcome)
        closed: list[str | None] = []
        for name, outcome in outcomes.items():
            if await self._close(
                session,
                name,
                status=SYNC_RUN_SUCCEEDED if outcome.succeeded else SYNC_RUN_FAILED,
                completeness=outcome.completeness,
                error=None if outcome.succeeded else outcome.reason,
                uncounted=uncounted.get(name, 0),
            ):
                closed.append(name)
        if changed or any(outcome.succeeded for outcome in outcomes.values()):
            await self._queue_analysis(session, dataset)
        self._publish_on_commit(session, outcomes, closed, uncounted)

    def _publish_on_commit(
        self,
        session: AsyncSession,
        outcomes: Mapping[str, FreshnessOutcome],
        closed: Sequence[str | None],
        rows: Mapping[str, int] | None = None,
    ) -> None:
        """Publish a completion to the job's bookkeeping once ``session``'s transaction commits.

        The outcomes join the ones :meth:`run` reports and logs, the closed datasets the ones
        :meth:`_close_remaining` skips, and the rows the job's counts (:meth:`_count`), only
        after the commit. A rollback of the transaction (a failed statement after the
        completion, a failed enqueue or commit) discards them, and a later transaction of the
        same session never publishes them. A savepoint released inside the transaction is not
        its commit.

        Args:
            session: The transaction the completion was written in.
            outcomes: The freshness outcomes it recorded.
            closed: The datasets whose ``sync_run`` rows it closed.
            rows: Rows it wrote per freshness dataset that are not counted yet.
        """
        published = dict(outcomes)
        names = tuple(closed)
        counts = dict(rows) if rows is not None else {}
        settled = False

        def on_commit(committed: Session) -> None:
            nonlocal settled
            if settled or committed.in_nested_transaction():
                return
            settled = True
            self._freshness.update(published)
            self._closed.update(names)
            for name, count in counts.items():
                self._count(name, count)

        def on_rollback(_session: Session, previous: SessionTransaction) -> None:
            nonlocal settled
            if previous.parent is None:
                settled = True

        # Listeners cannot be removed while their event is dispatched; ``settled`` makes the
        # pair one-shot, and they go with the session.
        event.listen(session.sync_session, "after_commit", on_commit)
        event.listen(session.sync_session, "after_soft_rollback", on_rollback)

    async def _queue_analysis(self, session: AsyncSession, dataset: SourceDataset) -> None:
        """Queue the job's default-window ``analysis_request`` in ``session``'s transaction.

        Only an active context writing a dataset that is still ``active`` queues one: a
        building dataset queues nothing, its activation does (AAP 0.3.2).

        Args:
            session: The transaction to queue it in: the one writing the change it reports,
                or, after a matching pass whose batches committed on their own, one of its own.
            dataset: The job's dataset, as :func:`repositories.ensure_writable` locked it in
                this transaction.
        """
        if self.ctx.active and dataset.state == DATASET_STATE_ACTIVE:
            await repositories.enqueue_analysis_request(
                session, reason=f"sync:{self.ctx.source}.{self.job}"
            )

    async def _hand_over(self, error: ConnectorError, datasets: Sequence[str]) -> ErrorDisposition:
        """Hand one connector error to ``on_error``; no transaction of this job is open.

        An error of another source (LinkedIn during a CRM job's matching) is that source's
        error, reported without this job's dataset. ``on_error`` logs the error's one record,
        ``sync.classification``, with its reason, disposition and this job's name; the job's
        outcome is reported by ``sync.job.finish``.
        """
        ctx = self.ctx
        error_source = _as_source(error.source) if error.source in SOURCES else ctx.source
        if error_source == ctx.source:
            return await ctx.controller.on_error(
                error_source,
                error,
                dataset_id=ctx.dataset_id,
                datasets=tuple(datasets),
                job=self.job,
            )
        return await ctx.controller.on_error(error_source, error, job=self.job)

    async def _record_failures(self, outcomes: Mapping[str, FreshnessOutcome]) -> None:
        """Record failed attempts on their freshness rows, in a transaction of their own.

        Coverage, completeness and ``last_success_at`` stand (the last good data keeps its
        standing); a dataset retired meanwhile records nothing, as its rows are history.
        """
        if not outcomes:
            return
        ctx = self.ctx
        try:
            async with ctx.sessionmaker() as session, session.begin():
                await repositories.ensure_writable(session, ctx.dataset_id)
                for name, outcome in outcomes.items():
                    await freshness.record_freshness(session, ctx.dataset_id, name, outcome)
        except repositories.DatasetNotWritable:
            _log.info(
                "sync.job.not_writable",
                source=ctx.source,
                job=self.job,
                reason="dataset no longer writable; failure not recorded",
            )

    async def _report_interruptions(self, *, quiet: bool = False) -> None:
        """Hand every noted interruption to ``on_error``, outside any transaction.

        Args:
            quiet: Swallow a database error while handing over (the job is already raising
                its own error, which must propagate unchanged).
        """

        async def hand_over_all() -> None:
            for interruption in self._interruptions:
                self._disposition = await self._hand_over(interruption.error, interruption.datasets)

        if not quiet:
            await hand_over_all()
            return
        try:
            await hand_over_all()
        except SQLAlchemyError as exc:
            # Best effort: an interruption not handed over leaves the connection's health
            # as it was, and the next run meets the platform again.
            _log.warning("sync.job.report_failed", job=self.job, error=type(exc).__name__)

    # -- sync_run rows -----------------------------------------------------------------

    async def _open_runs(self, datasets: Sequence[str | None]) -> None:
        """Insert ``running`` ``sync_run`` rows, one per dataset and job name, and commit.

        A job writing no freshness dataset gets one row with no dataset.
        """
        ctx = self.ctx
        names: Sequence[str | None] = datasets if datasets else (None,)
        async with ctx.sessionmaker() as session, session.begin():
            for name in names:
                if name in self._runs:
                    continue
                rows = [
                    SyncRun(
                        source=ctx.source,
                        job=job,
                        dataset=name,
                        started_at=ctx.now,
                        status=SYNC_RUN_RUNNING,
                    )
                    for job in self.sync_run_jobs()
                ]
                session.add_all(rows)
                await session.flush()
                self._runs[name] = [row.id for row in rows]

    async def _close(
        self,
        session: AsyncSession,
        name: str | None,
        *,
        status: Literal["succeeded", "failed"],
        completeness: str | None,
        error: str | None,
        uncounted: int = 0,
    ) -> bool:
        """Close one dataset's still-``running`` ``sync_run`` rows in ``session``.

        Only the UPDATE is issued: the caller marks the dataset closed once its transaction
        commits. A row a committed transaction already closed is never overwritten.

        Args:
            session: The transaction to close the rows in.
            name: The freshness dataset, or ``None`` for a job that writes none.
            status: The terminal status.
            completeness: The completeness recorded, or ``None``.
            error: The failure reason, or ``None``.
            uncounted: Rows ``session``'s transaction wrote that the job counts only once it
                commits; recorded in ``rows`` together with those already counted.

        Returns:
            Whether the dataset had rows of this job left to close.
        """
        ids = self._runs.get(name)
        if not ids or name in self._closed:
            return False
        counted = self._total_rows if name is None else self._rows.get(name, 0)
        rows = counted + uncounted
        await session.execute(
            update(SyncRun)
            .where(SyncRun.id.in_(ids), SyncRun.status == SYNC_RUN_RUNNING)
            .values(
                finished_at=self.ctx.controller.now(),
                status=status,
                rows=rows,
                completeness=completeness,
                error=None if error is None else _truncate(error),
            )
        )
        return True

    async def _close_remaining(self, error: str | None, *, quiet: bool = False) -> None:
        """Close every still-open ``sync_run`` row: failed datasets failed, the rest by ``error``.

        Open rows include those of a completion whose transaction rolled back, so a raised
        run leaves no ``running`` row. They are closed in a transaction of their own and
        count as closed once it commits; rows a committed completion closed stay as it
        closed them.

        Args:
            error: The job's error, or ``None`` when it finished normally.
            quiet: Swallow a database error while closing (the job is already raising).
        """
        open_names = [name for name in self._runs if name not in self._closed]
        if not open_names:
            return

        async def close_all() -> None:
            async with self.ctx.sessionmaker() as session, session.begin():
                for name in open_names:
                    reason = self._failed.get(name) if name is not None else None
                    if (
                        reason is None
                        and error is None
                        and name is not None
                        and name not in self._freshness
                    ):
                        # Defensive: a dataset the job never recorded is not a success.
                        reason = "no outcome recorded"
                    failed = error is not None or reason is not None
                    await self._close(
                        session,
                        name,
                        status=SYNC_RUN_FAILED if failed else SYNC_RUN_SUCCEEDED,
                        completeness=None if failed else COMPLETENESS_COMPLETE,
                        error=reason if reason is not None else error,
                    )
            self._closed.update(open_names)

        if not quiet:
            await close_all()
            return
        try:
            await close_all()
        except SQLAlchemyError as exc:
            # The job is already raising its own error; closing its rows is best effort, and
            # a row left ``running`` still records the attempt's ``started_at``.
            _log.warning("sync.job.close_failed", job=self.job, error=type(exc).__name__)


# ---------------------------------------------------------------------------------------------
# Shared database helpers
# ---------------------------------------------------------------------------------------------


def _by_last_key(mapping: Mapping[tuple[Any, ...], int]) -> dict[str, int]:
    """Re-key an ``entity_id_map``/``upsert_entities`` result by its last key column."""
    return {str(_key_value(key)): entity_id for key, entity_id in mapping.items()}


async def _insert_missing(
    session: AsyncSession,
    model: type[Base],
    natural_key: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    dataset_id: int,
) -> int:
    """Insert stand-in entity rows that do not exist yet, never touching existing ones.

    ``INSERT ... ON CONFLICT (<natural key>) DO NOTHING``: a stub must never overwrite an
    entity an entity sync stored (``upsert_entities`` would). Provenance comes from the
    dataset, as for every repository write. Nothing is committed: the rows and the dataset
    guard belong to the caller's transaction.

    The rows go to one ORM bulk insert as an executemany of one parameter set per row, the
    way the repository upserts write, never as a single multi-row ``VALUES`` statement.
    The driver therefore binds one row's columns per statement (psycopg's pipelined
    ``executemany``, or insertmanyvalues pages under SQLAlchemy's parameter cap), so a batch
    of any size stays under PostgreSQL's 65,535 bind parameters per statement: thousands of
    campaigns referenced by facts or leads before any entity sync stored them still ingest.
    Rows are sent in ascending natural-key order whatever the caller's order, so concurrent
    writers of overlapping keys take their unique-index locks in one order and queue instead
    of deadlocking.

    Returns:
        The number of rows requested.

    Raises:
        repositories.DatasetNotWritable: If the dataset is missing or retired.
        repositories.NaturalKeyError: If a row lacks a natural-key value or carries ``None``
            for one, before any row is inserted.
    """
    if not rows:
        return 0
    dataset = await repositories.ensure_writable(session, dataset_id)
    stubs = [{**row, "dataset_id": dataset_id, "source": dataset.source} for row in rows]
    for position, stub in enumerate(stubs):
        absent = [name for name in natural_key if stub.get(name) is None]
        if absent:
            raise repositories.NaturalKeyError(
                f"{model.__name__} row {position} lacks natural-key values {absent}"
            )
    stubs.sort(key=lambda stub: tuple(stub[name] for name in natural_key))
    await session.execute(
        insert(model).on_conflict_do_nothing(index_elements=list(natural_key)), stubs
    )
    return len(stubs)


async def _lock_subjects(session: AsyncSession, dataset_id: int) -> SourceDataset:
    """Take the dataset guard and its decision-subject locks in the code base's lock order.

    The dataset ``FOR SHARE`` (refusing a retired one), then every campaign and every
    explicitly shared budget ``FOR NO KEY UPDATE`` in ``id`` order: the order
    ``sync/snapshots.py`` and every decision writer use. Taken before an entity sync upserts
    budgets and campaigns, so its upserts re-lock rows it already holds and can never form
    a lock cycle with a concurrent decision or resolution write.
    """
    dataset = await repositories.ensure_writable(session, dataset_id)
    await session.execute(
        select(Campaign.id)
        .where(Campaign.dataset_id == dataset_id)
        .order_by(Campaign.id)
        .with_for_update(key_share=True)
    )
    await session.execute(
        select(Budget.id)
        .where(Budget.dataset_id == dataset_id, Budget.explicitly_shared.is_(True))
        .order_by(Budget.id)
        .with_for_update(key_share=True)
    )
    return dataset


async def _snapshotted_ids(
    session: AsyncSession, dataset_id: int, entity_type: str, *, since: datetime | None = None
) -> set[int]:
    """Ids of a dataset's entities of one type that have at least one snapshot (tracked).

    With ``since``, only snapshots captured at or after that instant count: the entities an
    entity cycle that began then has already returned.
    """
    statement = select(EntitySnapshot.entity_id).where(
        EntitySnapshot.dataset_id == dataset_id, EntitySnapshot.entity_type == entity_type
    )
    if since is not None:
        statement = statement.where(EntitySnapshot.captured_at >= since)
    result = await session.scalars(statement.distinct())
    return set(result)


async def _account_row(session: AsyncSession, dataset_id: int, source: Source) -> AdAccount | None:
    """The dataset's first ad account of ``source`` (Google has one customer per deployment)."""
    result = await session.scalars(
        select(AdAccount)
        .where(AdAccount.dataset_id == dataset_id, AdAccount.source == source)
        .order_by(AdAccount.id)
        .limit(1)
    )
    return result.first()


async def _campaign_ids(session: AsyncSession, dataset_id: int) -> dict[str, int]:
    """The dataset's campaigns keyed by external id (unique per platform account set)."""
    return _by_last_key(await repositories.entity_id_map(session, Campaign, dataset_id))


async def _stub_campaigns(
    session: AsyncSession,
    dataset_id: int,
    account_id: int,
    external_ids: Iterable[str],
    known: dict[str, int],
) -> int:
    """Insert stand-ins for campaigns facts or leads reference before any entity sync stored them.

    Metrics carry no status filter, so spend by a campaign removed before tracking still
    counts (AAP 0.3.3 "Queries issued"); a stub is ``ended`` with raw status ``UNKNOWN``
    and has no snapshot, so it is never tracked, baselined or reported missing. ``known``
    is refreshed in place.

    Returns:
        The number of stubs requested.
    """
    missing = sorted({external for external in external_ids if external not in known})
    if not missing:
        return 0
    rows = [
        {
            "account_id": account_id,
            "external_id": external,
            "name": f"Campaign {external}",
            "status": _STUB_STATUS,
            "raw_status": _STUB_RAW_STATUS,
        }
        for external in missing
    ]
    await _insert_missing(session, Campaign, Campaign.__natural_key__, rows, dataset_id)
    known.update(await _campaign_ids(session, dataset_id))
    _log.info(
        "sync.job.stub_entities", entity_type="campaign", dataset_id=dataset_id, rows=len(rows)
    )
    return len(rows)


async def _stub_ad_groups(
    session: AsyncSession,
    dataset_id: int,
    wanted: Mapping[str, int],
    known: dict[str, int],
) -> int:
    """Insert stand-ins for ad groups (external id -> internal campaign id) not stored yet."""
    missing = sorted(external for external in wanted if external not in known)
    if not missing:
        return 0
    rows = [
        {
            "campaign_id": wanted[external],
            "external_id": external,
            "name": f"Ad group {external}",
            "status": _STUB_RAW_STATUS,
        }
        for external in missing
    ]
    await _insert_missing(session, AdGroup, AdGroup.__natural_key__, rows, dataset_id)
    known.update(_by_last_key(await repositories.entity_id_map(session, AdGroup, dataset_id)))
    _log.info(
        "sync.job.stub_entities", entity_type="ad_group", dataset_id=dataset_id, rows=len(rows)
    )
    return len(rows)


async def _lead_form_ids(session: AsyncSession, dataset_id: int) -> dict[str, int]:
    """The dataset's lead forms keyed by canonical URN."""
    return _by_last_key(await repositories.entity_id_map(session, LeadForm, dataset_id))


async def _store_form_versions(
    session: AsyncSession,
    dataset_id: int,
    maps: Iterable[FormVersionMap],
    *,
    now: datetime,
) -> int:
    """Persist form-version maps as ``lead_form_version`` rows, keeping ``first_seen_at``.

    Each map is keyed by its canonical form URN and the version parsed from a response's
    ``versionedLeadGenFormUrn`` (or a form's current ``versionId``). A form not stored yet
    gets a stand-in ``lead_form`` row (its URN only; the next entity sync fills its name). A
    new version gets ``first_seen_at = now``; a known version keeps its first sighting, and
    its email question ids are rewritten only when they differ from the stored list (order
    included, as it is the preference order). The stored versions of the maps' forms are
    read in one query per :data:`_ID_CHUNK` forms and the changed ones rewritten in one
    statement per :data:`_ID_CHUNK` rows, never one statement per version.

    Returns:
        The number of version rows written: new versions plus known versions whose email
        question ids changed. An unchanged known version is not written.
    """
    distinct: dict[tuple[str, int], FormVersionMap] = {}
    for version_map in maps:
        distinct[(version_map.canonical_urn, version_map.version)] = version_map
    if not distinct:
        return 0
    forms = await _lead_form_ids(session, dataset_id)
    missing_forms = sorted({urn for urn, _ in distinct if urn not in forms})
    if missing_forms:
        await _insert_missing(
            session,
            LeadForm,
            LeadForm.__natural_key__,
            [{"canonical_urn": urn} for urn in missing_forms],
            dataset_id,
        )
        forms = await _lead_form_ids(session, dataset_id)
    # (lead_form_id, version) -> (id, stored email question ids), for the maps' forms only.
    stored: dict[tuple[int, int], tuple[int, list[str]]] = {}
    for form_chunk in _chunks(sorted({forms[urn] for urn, _ in distinct})):
        result = await session.execute(
            select(
                LeadFormVersion.id,
                LeadFormVersion.lead_form_id,
                LeadFormVersion.version,
                LeadFormVersion.email_question_ids,
            ).where(
                LeadFormVersion.dataset_id == dataset_id,
                LeadFormVersion.lead_form_id.in_(form_chunk),
            )
        )
        for version_id, form_id, version, question_ids in result:
            stored[(form_id, version)] = (version_id, list(question_ids))
    new_rows: list[dict[str, Any]] = []
    changed: list[tuple[int, list[str]]] = []
    for (urn, version), version_map in sorted(distinct.items()):
        form_id = forms[urn]
        question_ids = version_map.stored_email_question_ids
        known = stored.get((form_id, version))
        if known is None:
            new_rows.append(
                {
                    "lead_form_id": form_id,
                    "version": version,
                    "email_question_ids": question_ids,
                    "first_seen_at": now,
                }
            )
        elif known[1] != question_ids:
            changed.append((known[0], question_ids))
    # PostgreSQL checks NOT NULL on the proposed tuple before ON CONFLICT arbitration, so a
    # known version cannot go through the upsert without ``first_seen_at``; supplying it
    # would overwrite the first sighting. Changed known versions therefore rewrite only their
    # email question ids, one ``UPDATE ... FROM (VALUES ...)`` per chunk. A join locks rows
    # in its plan's order, so each chunk's rows are first locked ``FOR NO KEY UPDATE`` in id
    # order (the order every writer of these rows locks them in); the UPDATE re-locks them.
    await repositories.upsert_entities(session, LeadFormVersion, new_rows, dataset_id)
    if changed:
        await repositories.ensure_writable(session, dataset_id)
        for chunk in _chunks(sorted(changed)):
            await session.execute(
                select(LeadFormVersion.id)
                .where(
                    LeadFormVersion.id.in_([version_id for version_id, _ in chunk]),
                    LeadFormVersion.dataset_id == dataset_id,
                )
                .order_by(LeadFormVersion.id)
                .with_for_update(key_share=True)
            )
            rewritten = sql_values(
                column("id", LeadFormVersion.id.type),
                column("email_question_ids", LeadFormVersion.email_question_ids.type),
                name="rewritten",
            ).data(list(chunk))
            await session.execute(
                update(LeadFormVersion)
                .where(
                    LeadFormVersion.id == rewritten.c.id,
                    LeadFormVersion.dataset_id == dataset_id,
                )
                .values(email_question_ids=rewritten.c.email_question_ids)
            )
    return len(new_rows) + len(changed)


# ---------------------------------------------------------------------------------------------
# Google deferred work: checkpoints (AAP 0.3.3 "Budget"; module docstring)
# ---------------------------------------------------------------------------------------------

_CHECKPOINT_VERSION: Final = 1

# The one scope of a ``single`` query (Q-G1, Q-G8), which has nothing to narrow.
_SINGLE_SCOPE: Final[tuple[Mapping[str, object], ...]] = (MappingProxyType({}),)

# The Google metrics datasets and the dated queries each reads, in reading order.
_GOOGLE_METRICS_QUERIES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {DATASET_GOOGLE_METRICS_DAILY: ("Q-G3", "Q-G5"), DATASET_GOOGLE_SEARCH_TERMS: ("Q-G6",)}
)
_METRICS_CHECKPOINT_KEYS: Final = frozenset({"version", "pending", "covered_from", "reasons"})
# One unread metrics scope: the scope and when its work was first deferred.
_METRICS_ENTRY_KEYS: Final = frozenset({"scope", "deferred_at"})

# The entity cycle's single queries and safe-absence enumerations, in reading order.
_ENTITY_SINGLE_QUERIES: Final = ("Q-G1", "Q-G8")
_ENTITY_ENUMERATIONS: Final = ("Q-G2", "Q-G4", "Q-G7")
_ENTITY_CHECKPOINT_KEYS: Final = frozenset(
    {"version", "started_at", "pending", "enumerations", "reasons"}
)
# The safe-absence verdict of a resumed entity sync that did not read campaigns: no absence
# is inferred and nothing is present.
_CAMPAIGNS_READ_EARLIER: Final = "campaign enumeration finished earlier in this entity cycle"

_CHECKPOINT_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _checkpoint_object(data: object, keys: frozenset[str]) -> Mapping[str, object]:
    """Return a stored checkpoint object holding exactly ``keys`` at the current version.

    Raises:
        ValueError: When it is not such an object.
    """
    if not isinstance(data, Mapping) or set(data) != keys:
        raise ValueError(f"a checkpoint must hold exactly {sorted(keys)}")
    version = data["version"]
    if isinstance(version, bool) or version != _CHECKPOINT_VERSION:
        raise ValueError("unsupported checkpoint version")
    return data


def _checkpoint_pending(
    value: object, queries: Sequence[str]
) -> dict[str, tuple[Mapping[str, object], ...]]:
    """Rebuild the stored unread scopes per query id, each validated for its template.

    Raises:
        ValueError: When ``value`` is not an object of non-empty scope lists keyed by the
            given query ids, or a scope is invalid for its template.
    """
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint pending must be an object keyed by query id")
    pending: dict[str, tuple[Mapping[str, object], ...]] = {}
    for query_id in queries:
        scopes = value.get(query_id)
        if scopes is None:
            continue
        if not isinstance(scopes, list) or not scopes:
            raise ValueError(f"{query_id}: checkpoint pending scopes must be a non-empty list")
        pending[query_id] = tuple(scope_from_json(query_id, scope) for scope in scopes)
    if set(value) - set(pending):
        raise ValueError("checkpoint pending names a query this dataset does not read")
    return pending


def _checkpoint_reasons(value: object) -> tuple[str, ...]:
    """Rebuild the stored permanent reasons, without repeats.

    Raises:
        ValueError: When ``value`` is not a list of non-blank strings.
    """
    if not isinstance(value, list) or not all(
        isinstance(reason, str) and reason.strip() for reason in value
    ):
        raise ValueError("checkpoint reasons must be a list of non-blank strings")
    return tuple(dict.fromkeys(value))


def _checkpoint_date(value: object) -> date:
    """Read a stored ``YYYY-MM-DD`` native date.

    Raises:
        ValueError: When it is not text of that exact form or not a valid date.
    """
    if not isinstance(value, str) or _CHECKPOINT_DATE_RE.fullmatch(value) is None:
        raise ValueError("a checkpoint date must be YYYY-MM-DD text")
    return date.fromisoformat(value)


def _checkpoint_instant(value: object) -> datetime:
    """Read a stored timezone-aware ISO instant.

    Raises:
        ValueError: When it is not ISO text or carries no UTC offset.
    """
    if not isinstance(value, str):
        raise ValueError("a checkpoint instant must be ISO text")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("a checkpoint instant must carry its UTC offset")
    return instant


def _pending_json(
    pending: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """Return unread scopes per query id as JSON, leaving out queries with none."""
    return {
        query_id: [scope_to_json(scope) for scope in scopes]
        for query_id, scopes in pending.items()
        if scopes
    }


def _scope_key(query_id: str, scope: Mapping[str, object]) -> str:
    """Return one partition's identity: its query id and its canonical JSON scope."""
    return json.dumps([query_id, scope_to_json(scope)], sort_keys=True)


def _usable_checkpoint[T](
    parse: Callable[[object], T],
    stored: object | None,
    *,
    job: str,
    dataset_id: int,
    dataset: str,
) -> T | None:
    """Parse a stored checkpoint, or return ``None`` (logged) when it cannot be used.

    An unreadable checkpoint (written by another version, or edited by hand) must neither
    fail the job nor be trusted: the job runs as if none were stored, so it reads its full
    scope (a metrics dataset that needs repair widens to its recorded range). The warning
    carries the error's type only, never scope values.
    """
    if stored is None:
        return None
    try:
        return parse(stored)
    except ValueError as exc:
        _log.warning(
            "sync.google.checkpoint_unusable",
            source="google",
            job=job,
            dataset_id=dataset_id,
            dataset=dataset,
            error=type(exc).__name__,
        )
        return None


@dataclass(frozen=True, slots=True)
class _GoogleRead:
    """What one query's scopes, read in order, established (:func:`_read_google_scopes`).

    Attributes:
        rows: The rows read.
        pending: The scopes still unread: the interrupted fetch's deferred partitions, then
            every scope not started; empty when every scope was read.
        interrupted_by: The transient error that stopped the reads, or ``None``.
        shortfalls: Permanent shortfalls (result limits) of the fetches read.
        calls: POST attempts the fetches made, retries and next pages included.
    """

    rows: tuple[GoogleAdsRow, ...]
    pending: tuple[Mapping[str, object], ...]
    interrupted_by: TransientError | None
    shortfalls: tuple[str, ...]
    calls: int


async def _read_google_scopes(
    fetcher: PartitionedFetcher,
    query_id: str,
    scopes: Sequence[Mapping[str, object]],
) -> _GoogleRead:
    """Read ``scopes`` of one query in order, keeping their rows, until the first interruption.

    The entity cycle reads its single queries (Q-G1, Q-G8) this way.
    """
    rows: list[GoogleAdsRow] = []
    shortfalls: list[str] = []
    calls = 0
    for index, scope in enumerate(scopes):
        outcome = await fetcher.partitioned_fetch(query_id, scope)
        calls += outcome.calls
        rows.extend(outcome.rows)
        shortfalls.extend(_shortfall_reasons(outcome))
        if outcome.interrupted_by is not None:
            return _GoogleRead(
                rows=tuple(rows),
                pending=(*outcome.deferred, *scopes[index + 1 :]),
                interrupted_by=outcome.interrupted_by,
                shortfalls=tuple(shortfalls),
                calls=calls,
            )
    return _GoogleRead(
        rows=tuple(rows),
        pending=(),
        interrupted_by=None,
        shortfalls=tuple(shortfalls),
        calls=calls,
    )


def _shortfall_reasons(outcome: FetchOutcome) -> tuple[str, ...]:
    """Return the permanent shortfall of one fetch (a result limit), without its interruption.

    ``partition.py`` puts the interruption's reason first; what follows it is a result limit
    that re-reading cannot remove, so it outlives the deferral.
    """
    reason = outcome.reason
    if reason is None:
        return ()
    if outcome.interrupted_by is None:
        return (reason,)
    head = interruption_reason(outcome.interrupted_by, len(outcome.deferred))
    rest = reason.removeprefix(head).removeprefix(_REASON_SEPARATOR)
    return (rest,) if rest else ()


def _deferred_reason(error: TransientError | None, total: int) -> str:
    """Return the repairable reason of a dataset with ``total`` scopes still unread.

    ``error`` is this run's first interruption. Without one, the unread work was deferred by
    another run that stored it meanwhile; it is stated as a call-budget deferral, the
    shortfall an incomplete success records.
    """
    return interruption_reason(error if error is not None else CallBudgetExhausted(), total)


@dataclass(frozen=True, slots=True)
class _EntityCheckpoint:
    """The outstanding work of one ``google.entities`` cycle (module docstring).

    A cycle is the set of reads one uninterrupted entity sync makes: Q-G1, the Q-G2 scan
    with its Q-G2b re-read, Q-G4, Q-G7 and Q-G8. After an interruption, the following syncs
    read only what is still outstanding until the cycle drains.

    Attributes:
        started_at: When the cycle's first sync started.
        pending: The unread scope of each single query (Q-G1, Q-G8) still outstanding.
        enumerations: Each unfinished safe-absence enumeration's progress; an enumeration
            missing here finished earlier in the cycle.
        reasons: Permanent shortfalls (a Q-G1 or Q-G8 response at the result limit) met so
            far in the cycle.
    """

    started_at: datetime
    pending: Mapping[str, tuple[Mapping[str, object], ...]]
    enumerations: Mapping[str, EnumerationProgress]
    reasons: tuple[str, ...]

    @property
    def total(self) -> int:
        """The number of scopes (Q-G2b re-read chunks included) still outstanding."""
        return sum(len(scopes) for scopes in self.pending.values()) + sum(
            progress.outstanding for progress in self.enumerations.values()
        )

    def to_json(self) -> dict[str, Any]:
        """Return the stored JSON object."""
        return {
            "version": _CHECKPOINT_VERSION,
            "started_at": self.started_at.isoformat(),
            "pending": _pending_json(self.pending),
            "enumerations": {
                query_id: progress.to_json() for query_id, progress in self.enumerations.items()
            },
            "reasons": list(self.reasons),
        }

    @classmethod
    def parse(cls, data: object) -> _EntityCheckpoint:
        """Rebuild a stored entity checkpoint.

        Raises:
            ValueError: When it is not exactly the object :meth:`to_json` writes, a scope or a
                progress fails its validation, or nothing is outstanding.
        """
        stored = _checkpoint_object(data, _ENTITY_CHECKPOINT_KEYS)
        pending = _checkpoint_pending(stored["pending"], _ENTITY_SINGLE_QUERIES)
        if any(len(scopes) != 1 for scopes in pending.values()):
            raise ValueError("a single query has exactly one scope")
        stored_enumerations = stored["enumerations"]
        if not isinstance(stored_enumerations, Mapping) or set(stored_enumerations) - set(
            _ENTITY_ENUMERATIONS
        ):
            raise ValueError("checkpoint enumerations must be keyed by Q-G2, Q-G4 or Q-G7")
        enumerations: dict[str, EnumerationProgress] = {}
        for query_id in _ENTITY_ENUMERATIONS:
            if query_id in stored_enumerations:
                progress = EnumerationProgress.from_json(stored_enumerations[query_id])
                if progress.query_id != query_id:
                    raise ValueError("a stored progress is filed under another query id")
                enumerations[query_id] = progress
        checkpoint = cls(
            started_at=_checkpoint_instant(stored["started_at"]),
            pending=pending,
            enumerations=enumerations,
            reasons=_checkpoint_reasons(stored["reasons"]),
        )
        if not checkpoint.total:
            raise ValueError("a stored checkpoint holds unread work")
        return checkpoint


# ---------------------------------------------------------------------------------------------
# Entity sync (AAP 0.3.2 "Entity sync, all sources"; AAP 0.3.3 "Safe absence")
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _GoogleTracked:
    """What a Google entity sync compares its enumerations with, read before any call.

    Attributes:
        campaigns: External ids of campaigns seen in earlier snapshots (stubs never are).
        ad_groups: External ids of stored ad groups not already marked removed.
        keywords: ``(ad group external id, criterion id)`` of stored keywords not removed.
    """

    campaigns: frozenset[int]
    ad_groups: frozenset[int]
    keywords: frozenset[tuple[int, int]]


def _digits(value: str) -> int | None:
    """Return a stored external id as an INT64, or ``None`` when it is not one."""
    return int(value) if value.isdigit() and len(value) <= 19 else None


async def _google_tracked(session: AsyncSession, dataset_id: int) -> _GoogleTracked:
    """Read the tracked Google campaigns, ad groups and keywords of a dataset."""
    snapshotted = exists().where(
        EntitySnapshot.dataset_id == dataset_id,
        EntitySnapshot.entity_type == "campaign",
        EntitySnapshot.entity_id == Campaign.id,
    )
    campaigns = await session.scalars(
        select(Campaign.external_id).where(Campaign.dataset_id == dataset_id, snapshotted)
    )
    ad_groups = await session.scalars(
        select(AdGroup.external_id).where(
            AdGroup.dataset_id == dataset_id, AdGroup.status != _REMOVED_RAW_STATUS
        )
    )
    keywords = await session.execute(
        select(AdGroup.external_id, Keyword.criterion_id)
        .join(AdGroup, AdGroup.id == Keyword.ad_group_id)
        .where(Keyword.dataset_id == dataset_id, Keyword.status != _REMOVED_RAW_STATUS)
    )
    pairs: set[tuple[int, int]] = set()
    for ad_group, criterion in keywords.all():
        group_id, criterion_id = _digits(ad_group), _digits(criterion)
        if group_id is not None and criterion_id is not None:
            pairs.add((group_id, criterion_id))
    return _GoogleTracked(
        campaigns=frozenset(
            value for value in (_digits(text) for text in campaigns) if value is not None
        ),
        ad_groups=frozenset(
            value for value in (_digits(text) for text in ad_groups) if value is not None
        ),
        keywords=frozenset(pairs),
    )


class EntitySyncJob(_JobBase):
    """Full current entity state of one Google or LinkedIn dataset, every 3 h.

    Google (``google.entities``): Q-G1 (the account, its currency and time zone), the
    complete campaign enumeration (Q-G2 keyset scan, then Q-G2b for tracked campaigns it no
    longer returned), Q-G4 ad groups, Q-G7 keywords (with no-traffic keywords and their
    quality score) and Q-G8 conversion actions (no status filter). LinkedIn
    (``linkedin.entities``), per configured ad account: the account, campaign groups and
    campaigns in every status, creatives with their posts' headline and intro text, and
    lead forms with their current version's email question map.

    One transaction then writes the entities, one snapshot per entity
    (:func:`~ads_engine.sync.snapshots.write_snapshots`), the baseline decisions while the
    dataset's baseline is open, the discrepancy check (AAP 0.3.2: immediately after each
    entity sync), the freshness row, the ``sync_run`` row and the analysis request, so a
    crash leaves no half-written entity state.

    Safe absence: a removal or a disappearance is inferred only from a complete enumeration
    (AAP 0.3.3). An incomplete one records ``incomplete`` with its reason (a call-budget
    deferral starts with :data:`~ads_engine.sync.freshness.REASON_DEFERRED`), raises no
    ``missing`` discrepancy and leaves the previous snapshots standing.

    Google cycles: an interrupted sync stores its outstanding work as the
    ``google.entities`` checkpoint (module docstring) in the entity transaction, and the
    following syncs read only that work, in the order Q-G1, Q-G2 (with Q-G2b), Q-G4, Q-G7,
    Q-G8, each enumeration resuming from its stored progress
    (:class:`~ads_engine.connectors.google_n8n.partition.EnumerationProgress`). Each sync
    snapshots what it read. A missing campaign or a removed ad group or keyword is inferred
    in the sync in which its enumeration finishes, from the absences accumulated over the
    cycle; a shared budget snapshotted earlier in the cycle counts as returned. The dataset
    stays ``incomplete`` while any work is outstanding and turns ``complete`` once the cycle
    drains without a permanent shortfall.
    """

    job = JOB_ENTITIES

    def __init__(self, ctx: JobContext) -> None:
        """Bind the job; HubSpot has no entity sync.

        Raises:
            ValueError: If the context is a HubSpot dataset's.
        """
        if ctx.source == "hubspot":
            raise ValueError("HubSpot has no entity sync; its CRM job reads current state")
        super().__init__(ctx)

    def freshness_datasets(self) -> tuple[str, ...]:
        """``google.entities`` or ``linkedin.entities``."""
        return (f"{self.ctx.source}.entities",)

    async def _execute(self) -> None:
        """Run the source's entity sync."""
        if self.ctx.source == "google":
            await self._google()
        else:
            await self._linkedin()

    # -- Google ----------------------------------------------------------------------------

    async def _google(self) -> None:
        """Read Google's current entity state and write it with its checks (class docstring)."""
        ctx = self.ctx
        name = DATASET_GOOGLE_ENTITIES
        client = ctx.clients.require_google()
        fetcher = PartitionedFetcher(client)
        calls_before = client.calls_made
        async with ctx.sessionmaker() as session:
            tracked = await _google_tracked(session, ctx.dataset_id)
            stored_account = await _account_row(session, ctx.dataset_id, "google")
            stored_account_id = None if stored_account is None else stored_account.id
            freshness_row = (await freshness.load_freshness(session, ctx.dataset_id)).get(name)
        loaded = _usable_checkpoint(
            _EntityCheckpoint.parse,
            None if freshness_row is None else freshness_row.checkpoint,
            job=self.job,
            dataset_id=ctx.dataset_id,
            dataset=name,
        )
        # Without a usable checkpoint a new cycle starts: every read is outstanding, and each
        # enumeration starts from its full scope.
        singles: Mapping[str, Sequence[Mapping[str, object]]] = (
            dict.fromkeys(_ENTITY_SINGLE_QUERIES, _SINGLE_SCOPE)
            if loaded is None
            else loaded.pending
        )
        resumes: Mapping[str, EnumerationProgress | None] = (
            dict.fromkeys(_ENTITY_ENUMERATIONS) if loaded is None else loaded.enumerations
        )
        # Q-G1 is also read while no account is stored, since campaigns attach to it.
        customer_scopes = singles.get("Q-G1") or (
            () if stored_account_id is not None else _SINGLE_SCOPE
        )
        customer = (
            await _read_google_scopes(fetcher, "Q-G1", customer_scopes) if customer_scopes else None
        )
        campaigns = (
            await enumerate_campaigns(client, tracked.campaigns, resume=resumes["Q-G2"])
            if "Q-G2" in resumes
            else None
        )
        ad_groups = (
            await enumerate_entities(client, "Q-G4", tracked.ad_groups, resume=resumes["Q-G4"])
            if "Q-G4" in resumes
            else None
        )
        keywords = (
            await enumerate_entities(client, "Q-G7", tracked.keywords, resume=resumes["Q-G7"])
            if "Q-G7" in resumes
            else None
        )
        action_scopes = singles.get("Q-G8", ())
        actions = (
            await _read_google_scopes(fetcher, "Q-G8", action_scopes) if action_scopes else None
        )
        self._calls += client.calls_made - calls_before

        customer_rows = [] if customer is None else [parse_customer(row) for row in customer.rows]
        if not customer_rows and stored_account_id is None:
            # Without the customer row there is no account to attach campaigns to.
            if customer is not None and customer.interrupted_by is not None:
                raise customer.interrupted_by
            raise _google_defect("Q-G1 returned no customer row")
        interruptions = [
            error
            for error in (
                None if customer is None else customer.interrupted_by,
                None if campaigns is None else campaigns.interrupted_by,
                None if ad_groups is None else ad_groups.interrupted_by,
                None if keywords is None else keywords.interrupted_by,
                None if actions is None else actions.interrupted_by,
            )
            if error is not None
        ]
        enumerations: dict[str, EnumerationProgress] = {}
        if campaigns is not None and campaigns.progress is not None:
            enumerations["Q-G2"] = campaigns.progress
        for query_id, entities in (("Q-G4", ad_groups), ("Q-G7", keywords)):
            if entities is not None and entities.progress is not None:
                enumerations[query_id] = entities.progress
        cycle = _EntityCheckpoint(
            started_at=ctx.now if loaded is None else loaded.started_at,
            pending={
                query_id: read.pending
                for query_id, read in (("Q-G1", customer), ("Q-G8", actions))
                if read is not None and read.pending
            },
            enumerations=enumerations,
            reasons=tuple(
                dict.fromkeys(
                    (
                        *(() if loaded is None else loaded.reasons),
                        *(() if customer is None else customer.shortfalls),
                        *(() if actions is None else actions.shortfalls),
                    )
                )
            ),
        )
        outstanding = cycle.total
        reasons = [
            _deferred_reason(interruptions[0] if interruptions else None, outstanding)
            if outstanding
            else None,
            *cycle.reasons,
        ]
        present = [] if campaigns is None else [parse_campaign(row) for row in campaigns.present]
        removed = [] if campaigns is None else [parse_campaign(row) for row in campaigns.removed]
        group_rows = [] if ad_groups is None else [parse_ad_group(row) for row in ad_groups.rows]
        keyword_rows = [] if keywords is None else [parse_keyword(row) for row in keywords.rows]
        action_rows = (
            [] if actions is None else [parse_conversion_action(row) for row in actions.rows]
        )
        _log.info(
            "sync.entities.google_cycle",
            source="google",
            dataset_id=ctx.dataset_id,
            resumed=loaded is not None,
            outstanding=outstanding,
        )

        async with ctx.sessionmaker() as session, session.begin():
            await _lock_subjects(session, ctx.dataset_id)
            sealed = await snapshots.baseline_sealed(session, ctx.dataset_id)
            account_id = stored_account_id
            if customer_rows:
                first = customer_rows[0]
                stored = await repositories.upsert_entities(
                    session,
                    AdAccount,
                    [
                        {
                            "external_id": str(first.customer_id),
                            "name": first.descriptive_name,
                            "currency": first.currency_code,
                            "timezone": first.time_zone,
                        }
                    ],
                    ctx.dataset_id,
                )
                account_id = next(iter(stored.values()))
                self._count(name, 1)
            if account_id is None:  # pragma: no cover - guarded above
                raise _google_defect("no Google account row to attach campaigns to")
            rows, enumeration = await self._write_google(
                session,
                account_id,
                present=present,
                removed=removed,
                missing=(
                    campaigns.missing_ids
                    if campaigns is not None and campaigns.complete
                    else frozenset()
                ),
                complete=campaigns is not None and campaigns.complete,
                reason=(_CAMPAIGNS_READ_EARLIER if campaigns is None else campaigns.reason),
                cycle_started=None if loaded is None else loaded.started_at,
                ad_groups=group_rows,
                absent_ad_groups=(
                    ad_groups.absent_keys
                    if ad_groups is not None and ad_groups.complete
                    else frozenset()
                ),
                keywords=keyword_rows,
                absent_keywords=(
                    keywords.absent_keys
                    if keywords is not None and keywords.complete
                    else frozenset()
                ),
                actions=action_rows,
            )
            await self._finish_entities(session, rows, enumeration, sealed=sealed, reasons=reasons)
            # After the freshness write, so the row is locked in record_freshness's order.
            await freshness.write_checkpoint(
                session, ctx.dataset_id, name, cycle.to_json() if outstanding else None
            )
        if interruptions:
            self._interrupted(interruptions[0], dataset=None)

    async def _write_google(
        self,
        session: AsyncSession,
        account_id: int,
        *,
        present: Sequence[CampaignRow],
        removed: Sequence[CampaignRow],
        missing: frozenset[int],
        complete: bool,
        reason: str | None,
        cycle_started: datetime | None,
        ad_groups: Sequence[AdGroupRow],
        absent_ad_groups: frozenset[int] | frozenset[tuple[int, int]],
        keywords: Sequence[KeywordRow],
        absent_keywords: frozenset[int] | frozenset[tuple[int, int]],
        actions: Sequence[ConversionActionRow],
    ) -> tuple[list[SnapshotRow], EnumerationResult]:
        """Upsert Google entities and build their snapshots and the safe-absence verdict.

        ``cycle_started`` is when the resumed entity cycle this sync continues began, or
        ``None`` when the cycle began in this sync: a shared budget snapshotted since then was
        returned by a campaign read earlier in the cycle, so a campaign enumeration that
        finishes in this sync does not report it missing.
        """
        ctx = self.ctx
        dataset_id = ctx.dataset_id
        name = DATASET_GOOGLE_ENTITIES
        campaign_rows = list({row.campaign_id: row for row in (*present, *removed)}.values())

        # Budgets first: campaigns reference them.
        budgets: dict[str, dict[str, Any]] = {}
        for row in campaign_rows:
            if row.budget_id is None or row.budget_period is None:
                continue
            budgets[str(row.budget_id)] = {
                "external_id": str(row.budget_id),
                "amount": row.budget_amount,
                "amount_micros": row.budget_amount_micros,
                "period": row.budget_period,
                "explicitly_shared": row.budget_explicitly_shared,
            }
        budget_ids = _by_last_key(
            await repositories.upsert_entities(session, Budget, list(budgets.values()), dataset_id)
        )
        self._count(name, len(budget_ids))

        # Decision: every Google campaign row repeats the own-period amount of the budget it
        # draws from, shared members included. That is the documented meaning of
        # campaign.daily_budget/total_budget (db/models/accounts.py) and what the analysis
        # fixtures store; the shared amount still belongs to the budget subject, because
        # decisions and discrepancy checks read a member's budget row, never these columns.
        campaign_values = [
            {
                "account_id": account_id,
                "external_id": str(row.campaign_id),
                "name": row.name,
                "status": row.status,
                "raw_status": row.raw_status,
                "objective_or_channel": row.advertising_channel_type,
                "campaign_group_id": None,
                "budget_id": None if row.budget_id is None else budget_ids[str(row.budget_id)],
                "budget_period": row.budget_period,
                "daily_budget": row.daily_budget,
                "total_budget": row.total_budget,
            }
            for row in campaign_rows
        ]
        await repositories.upsert_entities(session, Campaign, campaign_values, dataset_id)
        self._count(name, len(campaign_values))
        campaign_ids = await _campaign_ids(session, dataset_id)

        # Ad groups, then keywords; a parent no entity list returned gets a stand-in.
        stubs = await _stub_campaigns(
            session,
            dataset_id,
            account_id,
            [str(row.campaign_id) for row in ad_groups]
            + [str(row.campaign_id) for row in keywords],
            campaign_ids,
        )
        group_values = [
            {
                "campaign_id": campaign_ids[str(row.campaign_id)],
                "external_id": str(row.ad_group_id),
                "name": row.name,
                "status": row.raw_status,
            }
            for row in {row.ad_group_id: row for row in ad_groups}.values()
        ]
        await repositories.upsert_entities(session, AdGroup, group_values, dataset_id)
        self._count(name, len(group_values))
        group_ids = _by_last_key(await repositories.entity_id_map(session, AdGroup, dataset_id))
        stubs += await _stub_ad_groups(
            session,
            dataset_id,
            {str(row.ad_group_id): campaign_ids[str(row.campaign_id)] for row in keywords},
            group_ids,
        )
        distinct_keywords = list(
            {(row.ad_group_id, row.criterion_id): row for row in keywords}.values()
        )
        keyword_ids = await repositories.upsert_entities(
            session,
            Keyword,
            [
                {
                    "ad_group_id": group_ids[str(row.ad_group_id)],
                    "criterion_id": str(row.criterion_id),
                    "text": row.text,
                    "match_type": row.match_type,
                    "status": row.raw_status,
                    "quality_score": row.quality_score,
                }
                for row in distinct_keywords
            ],
            dataset_id,
        )
        self._count(name, len(distinct_keywords))
        distinct_actions = list({row.conversion_action_id: row for row in actions}.values())
        action_ids = _by_last_key(
            await repositories.upsert_entities(
                session,
                ConversionAction,
                [
                    {
                        "external_id": str(row.conversion_action_id),
                        "name": row.name,
                        "status": row.status,
                        "include_in_conversions_metric": row.include_in_conversions_metric,
                        "counting_type": row.counting_type,
                        "attribution_model": row.attribution_model,
                    }
                    for row in distinct_actions
                ],
                dataset_id,
            )
        )
        self._count(name, len(distinct_actions))
        await self._mark_removed(session, absent_ad_groups, absent_keywords, group_ids)

        # Snapshots: one per entity this sync returned.
        rows: list[SnapshotRow] = []
        for row in campaign_rows:
            budget_id = None if row.budget_id is None else budget_ids[str(row.budget_id)]
            rows.append(
                SnapshotRow.campaign(
                    source="google",
                    entity_id=campaign_ids[str(row.campaign_id)],
                    status=row.status,
                    raw_status=row.raw_status,
                    budget_id=budget_id,
                    budget_period=row.budget_period if budget_id is not None else None,
                    budget_amount_micros=(
                        row.budget_amount_micros if budget_id is not None else None
                    ),
                    explicitly_shared=row.budget_explicitly_shared
                    if budget_id is not None
                    else False,
                )
            )
        for external, values in budgets.items():
            rows.append(
                SnapshotRow.budget(
                    entity_id=budget_ids[external],
                    period=values["period"],
                    amount_micros=values["amount_micros"],
                    explicitly_shared=values["explicitly_shared"],
                )
            )
        # Keyword natural key: (dataset_id, ad_group_id, criterion_id).
        keyword_by_key = {
            (int(key[1]), str(key[2])): keyword_id for key, keyword_id in keyword_ids.items()
        }
        for keyword in distinct_keywords:
            group_id = group_ids[str(keyword.ad_group_id)]
            rows.append(
                SnapshotRow.keyword(
                    entity_id=keyword_by_key[(group_id, str(keyword.criterion_id))],
                    status=keyword.raw_status,
                    match_type=keyword.match_type,
                    quality_score=keyword.quality_score,
                )
            )
        for action in distinct_actions:
            rows.append(
                SnapshotRow.conversion_action(
                    entity_id=action_ids[str(action.conversion_action_id)],
                    status=action.status,
                    include_in_conversions_metric=action.include_in_conversions_metric,
                    counting_type=action.counting_type,
                    attribution_model=action.attribution_model,
                )
            )

        present_ids = frozenset(campaign_ids[str(row.campaign_id)] for row in present)
        removed_ids = frozenset(campaign_ids[str(row.campaign_id)] for row in removed) - present_ids
        present_budgets = frozenset(budget_ids.values())
        missing_ids: frozenset[int] = frozenset()
        missing_budgets: frozenset[int] = frozenset()
        if complete:
            missing_ids = frozenset(
                campaign_ids[str(external)] for external in missing if str(external) in campaign_ids
            )
            tracked_budgets = await _snapshotted_ids(session, dataset_id, "budget")
            shared = await session.scalars(
                select(Budget.id).where(
                    Budget.dataset_id == dataset_id, Budget.explicitly_shared.is_(True)
                )
            )
            returned = (
                set()
                if cycle_started is None
                else await _snapshotted_ids(session, dataset_id, "budget", since=cycle_started)
            )
            missing_budgets = frozenset(
                (set(shared) & tracked_budgets) - present_budgets - returned
            )
        enumeration = EnumerationResult(
            source="google",
            complete=complete,
            present_campaign_ids=present_ids,
            present_budget_ids=present_budgets,
            removed_campaign_ids=removed_ids,
            missing_campaign_ids=missing_ids - present_ids - removed_ids,
            missing_budget_ids=missing_budgets,
            reason=None if complete else (reason or "campaign enumeration incomplete"),
        )
        if stubs:
            _log.info("sync.job.stubs", source="google", job=self.job, stubs=stubs)
        return rows, enumeration

    async def _mark_removed(
        self,
        session: AsyncSession,
        absent_ad_groups: frozenset[int] | frozenset[tuple[int, int]],
        absent_keywords: frozenset[int] | frozenset[tuple[int, int]],
        group_ids: Mapping[str, int],
    ) -> None:
        """Mark tracked ad groups and keywords a complete enumeration no longer returned.

        Q-G4 and Q-G7 exclude ``REMOVED`` rows, so a tracked key a complete enumeration did
        not return is removed (or gone); its row takes Google's ``REMOVED`` status. Keys of an
        incomplete enumeration never reach here (AAP 0.3.3 "Safe absence").
        """
        dataset_id = self.ctx.dataset_id
        groups = sorted(str(key) for key in absent_ad_groups if isinstance(key, int))
        for chunk in _chunks(groups):
            await session.execute(
                update(AdGroup)
                .where(AdGroup.dataset_id == dataset_id, AdGroup.external_id.in_(chunk))
                .values(status=_REMOVED_RAW_STATUS)
            )
        by_group: dict[int, list[str]] = {}
        for key in absent_keywords:
            if isinstance(key, tuple):
                group_id = group_ids.get(str(key[0]))
                if group_id is not None:
                    by_group.setdefault(group_id, []).append(str(key[1]))
        for group_id, criteria in sorted(by_group.items()):
            for chunk in _chunks(sorted(criteria)):
                await session.execute(
                    update(Keyword)
                    .where(
                        Keyword.dataset_id == dataset_id,
                        Keyword.ad_group_id == group_id,
                        Keyword.criterion_id.in_(chunk),
                    )
                    .values(status=_REMOVED_RAW_STATUS)
                )
        if groups or by_group:
            _log.info(
                "sync.job.entities_removed",
                source="google",
                dataset_id=dataset_id,
                ad_groups=len(groups),
                keywords=sum(len(criteria) for criteria in by_group.values()),
            )

    # -- shared tail -----------------------------------------------------------------------

    async def _finish_entities(
        self,
        session: AsyncSession,
        rows: Sequence[SnapshotRow],
        enumeration: EnumerationResult,
        *,
        sealed: bool,
        reasons: Iterable[str | None],
    ) -> None:
        """Snapshots, baseline, discrepancy check and freshness, in the entity transaction.

        ``sealed`` was read in this transaction before the freshness write, as
        :func:`~ads_engine.sync.snapshots.baseline_sealed` requires.
        """
        ctx = self.ctx
        distinct = list({row.key: row for row in rows}.values())
        snapshot_ids = await snapshots.write_snapshots(
            session, ctx.dataset_id, distinct, captured_at=ctx.now
        )
        baselined = await snapshots.ensure_baseline(
            session, ctx.dataset_id, captured_at=ctx.now, sealed=sealed
        )
        report = await snapshots.detect_discrepancies(
            session, ctx.dataset_id, enumeration, snapshot_ids, sealed=sealed, now=ctx.now
        )
        _log.info(
            "sync.entities.checked",
            source=ctx.source,
            dataset_id=ctx.dataset_id,
            snapshots=len(snapshot_ids),
            baselined=baselined,
            sealed=sealed,
            enumeration_complete=enumeration.complete,
            discrepancies_raised=report.total_raised,
            discrepancies_resolved=report.auto_resolved,
        )
        await self._complete(
            session, {f"{ctx.source}.entities": _success(ctx.now, covered=None, reasons=reasons)}
        )

    # -- LinkedIn --------------------------------------------------------------------------

    async def _linkedin(self) -> None:
        """Read LinkedIn's current entity state and write it with its checks (class docstring)."""
        ctx = self.ctx
        client = ctx.clients.require_linkedin()
        reads: list[_LinkedInAccountRead] = []
        for account_id in ctx.clients.linkedin_account_ids:
            account = await client.get_ad_account(account_id)
            groups = await client.list_campaign_groups(
                account_id, account_currency=account.currency
            )
            campaigns = await client.list_campaigns(account_id, account_currency=account.currency)
            creatives = await client.list_creatives(
                account_id, [campaign.urn for campaign in campaigns.rows]
            )
            post_urns = sorted({row.post_urn for row in creatives.rows if row.post_urn})
            posts = await client.get_posts(post_urns)
            forms = await client.list_lead_forms(account_id)
            self._calls += 1 + groups.calls + campaigns.calls + creatives.calls + posts.calls
            self._calls += forms.calls
            reads.append(
                _LinkedInAccountRead(
                    external_id=account.external_id,
                    name=account.name,
                    currency=account.currency,
                    groups=groups,
                    campaigns=campaigns,
                    creatives=creatives,
                    headlines={
                        urn: (post.headline, post.intro_text) for urn, post in posts.posts.items()
                    },
                    posts_complete=posts.complete,
                    posts_reason=posts.reason,
                    posts_interrupted=posts.interrupted_by,
                    forms=forms,
                )
            )
        listings: list[Listing[Any]] = []
        for read in reads:
            listings.extend((read.groups, read.campaigns, read.creatives, read.forms))
        complete = all(read.groups.complete and read.campaigns.complete for read in reads)
        enumeration_reason = _join_reasons(
            listing.reason
            for read in reads
            for listing in (read.groups, read.campaigns)
            if not listing.complete
        )
        reasons: list[str | None] = [
            listing.reason or "listing incomplete" for listing in listings if not listing.complete
        ]
        reasons.extend(
            read.posts_reason or "posts incomplete" for read in reads if not read.posts_complete
        )
        interruptions = [listing.interrupted_by for listing in listings if listing.interrupted_by]
        interruptions.extend(read.posts_interrupted for read in reads if read.posts_interrupted)

        async with ctx.sessionmaker() as session, session.begin():
            await _lock_subjects(session, ctx.dataset_id)
            sealed = await snapshots.baseline_sealed(session, ctx.dataset_id)
            rows: list[SnapshotRow] = []
            present: set[int] = set()
            account_ids: list[int] = []
            for read in reads:
                account_row_id, account_rows, account_present = await self._write_linkedin_account(
                    session, read
                )
                account_ids.append(account_row_id)
                rows.extend(account_rows)
                present.update(account_present)
            missing: frozenset[int] = frozenset()
            if complete:
                tracked = await _snapshotted_ids(session, ctx.dataset_id, "campaign")
                in_accounts = await session.scalars(
                    select(Campaign.id).where(
                        Campaign.dataset_id == ctx.dataset_id, Campaign.account_id.in_(account_ids)
                    )
                )
                missing = frozenset((tracked & set(in_accounts)) - present)
            enumeration = EnumerationResult(
                source="linkedin",
                complete=complete,
                present_campaign_ids=frozenset(present),
                missing_campaign_ids=missing,
                reason=None if complete else (enumeration_reason or "campaign listing incomplete"),
            )
            await self._finish_entities(session, rows, enumeration, sealed=sealed, reasons=reasons)
        if interruptions:
            self._interrupted(interruptions[0], dataset=None)

    async def _write_linkedin_account(
        self, session: AsyncSession, read: _LinkedInAccountRead
    ) -> tuple[int, list[SnapshotRow], set[int]]:
        """Upsert one account's entities; return its id, its snapshots and present campaigns."""
        ctx = self.ctx
        dataset_id = ctx.dataset_id
        name = DATASET_LINKEDIN_ENTITIES
        stored = await repositories.upsert_entities(
            session,
            AdAccount,
            [
                {
                    "external_id": read.external_id,
                    "name": read.name,
                    "currency": read.currency,
                    # LinkedIn reports daily buckets on UTC days (AAP 0.3.3).
                    "timezone": _UTC_ZONE,
                }
            ],
            dataset_id,
        )
        account_id = next(iter(stored.values()))
        self._count(name, 1)

        groups = list({row.urn: row for row in read.groups.rows}.values())
        group_ids = _by_last_key(
            await repositories.upsert_entities(
                session,
                CampaignGroup,
                [_group_values(account_id, row) for row in groups],
                dataset_id,
            )
        )
        self._count(name, len(groups))
        known_groups = _by_last_key(
            await repositories.entity_id_map(session, CampaignGroup, dataset_id)
        )
        known_groups.update(group_ids)

        campaigns = list({row.urn: row for row in read.campaigns.rows}.values())
        await repositories.upsert_entities(
            session,
            Campaign,
            [_linkedin_campaign_values(account_id, row, known_groups) for row in campaigns],
            dataset_id,
        )
        self._count(name, len(campaigns))
        campaign_ids = await _campaign_ids(session, dataset_id)

        creatives = [
            row
            for row in {row.urn: row for row in read.creatives.rows}.values()
            if _urn_tail(row.campaign_urn, PREFIX_CAMPAIGN) in campaign_ids
        ]
        creative_ids = _by_last_key(
            await repositories.upsert_entities(
                session,
                Creative,
                [_creative_values(row, campaign_ids, read.headlines) for row in creatives],
                dataset_id,
            )
        )
        self._count(name, len(creatives))

        forms = list({row.canonical_urn: row for row in read.forms.rows}.values())
        await repositories.upsert_entities(
            session,
            LeadForm,
            [
                {
                    "canonical_urn": row.canonical_urn,
                    "name": row.name,
                    "current_version": row.current_version,
                }
                for row in forms
            ],
            dataset_id,
        )
        versions = await _store_form_versions(
            session, dataset_id, [row.version_map for row in forms], now=ctx.now
        )
        self._count(name, len(forms) + versions)

        rows: list[SnapshotRow] = []
        for group in groups:
            rows.append(
                SnapshotRow.campaign_group(
                    entity_id=group_ids[group.external_id],
                    status=group.raw_status,
                    daily_budget=None if group.daily_budget is None else group.daily_budget.amount,
                    total_budget=None if group.total_budget is None else group.total_budget.amount,
                    run_start=group.run_start,
                    run_end=group.run_end,
                )
            )
        present: set[int] = set()
        for campaign in campaigns:
            campaign_id = campaign_ids[campaign.external_id]
            present.add(campaign_id)
            rows.append(
                SnapshotRow.campaign(
                    source="linkedin",
                    entity_id=campaign_id,
                    status=campaign.status,
                    raw_status=campaign.raw_status,
                    daily_budget=_amount(campaign.daily_budget),
                    total_budget=_amount(campaign.total_budget),
                    campaign_group_urn=campaign.campaign_group_urn,
                )
            )
        for creative in creatives:
            rows.append(
                SnapshotRow.creative(
                    entity_id=creative_ids[creative.urn],
                    intended_status=creative.intended_status,
                    lead_form_urn=creative.lead_form_urn,
                )
            )
        return account_id, rows, present


@dataclass(frozen=True, slots=True)
class _LinkedInAccountRead:
    """Everything one LinkedIn entity sync read for one ad account, before any write."""

    external_id: str
    name: str
    currency: str
    groups: Listing[LinkedInGroupRow]
    campaigns: Listing[LinkedInCampaignRow]
    creatives: Listing[CreativeRow]
    headlines: Mapping[str, tuple[str | None, str | None]]
    posts_complete: bool
    posts_reason: str | None
    posts_interrupted: TransientError | None
    forms: Listing[LeadFormRow]


def _amount(money: Money | None) -> Decimal | None:
    """Return a LinkedIn ``Money``'s exact amount, or ``None`` when there is no budget."""
    return None if money is None else money.amount


def _urn_tail(urn: str | None, prefix: str) -> str | None:
    """Return the numeric id of a LinkedIn URN, or ``None`` when it is absent or malformed."""
    if urn is None:
        return None
    try:
        return urn_id(urn, prefix)
    except ValueError:
        return None


def _group_values(account_id: int, row: LinkedInGroupRow) -> dict[str, Any]:
    """The ``campaign_group`` row of one LinkedIn campaign group (raw status as returned)."""
    return {
        "account_id": account_id,
        "external_id": row.external_id,
        "name": row.name,
        "status": row.raw_status,
        "daily_budget": _amount(row.daily_budget),
        "total_budget": _amount(row.total_budget),
        "run_start": row.run_start,
        "run_end": row.run_end,
    }


def _linkedin_campaign_values(
    account_id: int, row: LinkedInCampaignRow, groups: Mapping[str, int]
) -> dict[str, Any]:
    """The ``campaign`` row of one LinkedIn campaign; its budgets live on the row.

    A group URN no stored group matches leaves ``campaign_group_id`` out of the row, so a
    group listing cut short keeps the campaign's stored group instead of clearing it.
    """
    values: dict[str, Any] = {
        "account_id": account_id,
        "external_id": row.external_id,
        "name": row.name,
        "status": row.status,
        "raw_status": row.raw_status,
        "objective_or_channel": row.objective_type,
        "budget_id": None,
        "budget_period": None,
        "daily_budget": _amount(row.daily_budget),
        "total_budget": _amount(row.total_budget),
    }
    if row.campaign_group_urn is None:
        values["campaign_group_id"] = None
    else:
        group_external = _urn_tail(row.campaign_group_urn, PREFIX_CAMPAIGN_GROUP)
        if group_external is not None and group_external in groups:
            values["campaign_group_id"] = groups[group_external]
    return values


def _creative_values(
    row: CreativeRow,
    campaigns: Mapping[str, int],
    headlines: Mapping[str, tuple[str | None, str | None]],
) -> dict[str, Any]:
    """The ``creative`` row of one LinkedIn creative.

    The headline and intro text come from the referenced post. A post that could not be read
    leaves both out of the row, so the stored copy stands; a creative without a post has
    neither.
    """
    campaign_external = _urn_tail(row.campaign_urn, PREFIX_CAMPAIGN)
    values: dict[str, Any] = {
        "campaign_id": campaigns[campaign_external] if campaign_external is not None else None,
        "urn": row.urn,
        "intended_status": row.intended_status,
        "post_urn": row.post_urn,
        "cta_label": row.cta_label,
        "lead_form_urn": row.lead_form_urn,
    }
    if row.post_urn is None:
        values["headline"] = None
        values["intro_text"] = None
    elif row.post_urn in headlines:
        values["headline"], values["intro_text"] = headlines[row.post_urn]
    return values


def _google_defect(message: str, query_id: str = "Q-G1") -> QueryDefect:
    """A Google response that breaks the job's contract (for example no customer row)."""
    return QueryDefect(
        message,
        source="google",
        endpoint=f"search {query_id}",
        code="validation",
        query_id=query_id,
    )


# ---------------------------------------------------------------------------------------------
# Metrics (AAP 0.3.2 "Metrics, incremental", "Metrics, restatement", "Initial backfill")
# ---------------------------------------------------------------------------------------------


def _metrics_range(
    kind: MetricsKind,
    source: Source,
    today: date,
    settings: Settings,
    row: DatasetFreshness | None,
    *,
    repair: bool = True,
) -> tuple[date, date]:
    """Return the native ``[start, end]`` a metrics run reads for one freshness dataset.

    ``incremental``: the trailing :data:`INCREMENTAL_DAYS` days ending native today;
    ``restatement``: :data:`RESTATEMENT_DAYS`; ``backfill``: ``BACKFILL_DAYS`` (LinkedIn capped
    at its 180-day daily retention). With ``repair``, when the dataset's stored row needs
    repair (deferred or interrupted pages), the start widens to the row's ``covered_from`` so
    the shortfall is re-read and the row can turn complete
    (:func:`~ads_engine.sync.freshness.needs_repair`). LinkedIn always repairs this way; a
    Google dataset does only when its row holds no usable checkpoint, because a checkpoint's
    unread scopes are read first instead (:class:`MetricsSyncJob`).
    """
    match kind:
        case "incremental":
            days = INCREMENTAL_DAYS
        case "restatement":
            days = RESTATEMENT_DAYS[source]
        case "backfill":
            days = _span_days(settings, source)
    start = today - timedelta(days=days - 1)
    if repair and freshness.needs_repair(row) and row is not None and row.covered_from is not None:
        start = min(start, row.covered_from)
    if source == "linkedin":
        start = max(start, today - timedelta(days=LINKEDIN_RETENTION_DAYS - 1))
    return start, today


def _scope_day(scope: Mapping[str, object], key: str) -> date:
    """Return the native date of a dated scope's ``start`` or ``end``.

    Raises:
        ValueError: When the scope holds no native date there (stored scopes are validated
            by :func:`~ads_engine.connectors.google_n8n.partition.scope_from_json`).
    """
    value = scope.get(key)
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"a dated scope needs a native {key!r} date")
    return value


def _date_scope(start: date, end: date) -> Mapping[str, object]:
    """Return the immutable dated scope ``{start, end}``."""
    return MappingProxyType({_SCOPE_START: start, _SCOPE_END: end})


def _touches(first: tuple[date, date], second: tuple[date, date]) -> bool:
    """Return whether two closed date ranges overlap or are adjacent (no day between)."""
    one_day = timedelta(days=1)
    return first[0] <= second[1] + one_day and second[0] <= first[1] + one_day


def _own_segments(
    own: tuple[date, date], stored: Sequence[_Deferred]
) -> tuple[Mapping[str, object], ...]:
    """Return the dated scopes of ``own`` that no stored scope of the same query covers.

    Stored scopes are read as stored, before any own range (:class:`MetricsSyncJob`), and
    are never trimmed or dropped, so a stored criterion keyset or campaign range keeps its
    narrowed progress. The own range leaves out every native day a stored scope spans: a
    plain ``{start, end}`` scope its whole span, a narrowed one (always a single day) its
    day; the days a narrowed scope does not reach on that day were read by the run that
    deferred it. What remains is zero or more contiguous ``{start, end}`` segments in
    ascending order.
    """
    low, high = own
    one_day = timedelta(days=1)
    segments: list[Mapping[str, object]] = []
    cursor: date | None = low  # the first own day not yet accounted for
    spans = sorted(
        (_scope_day(entry.scope, _SCOPE_START), _scope_day(entry.scope, _SCOPE_END))
        for entry in stored
    )
    for start, end in spans:
        if cursor is None or start > high:
            break
        if end < cursor:
            continue
        if start > cursor:
            segments.append(_date_scope(cursor, start - one_day))
        cursor = None if end >= high else end + one_day
    if cursor is not None:
        segments.append(_date_scope(cursor, high))
    return tuple(segments)


@dataclass(frozen=True, slots=True)
class _Deferred:
    """One unread scope of a Google metrics dataset and when its work was first deferred.

    Attributes:
        scope: A dated scope ``partitioned_fetch`` accepts.
        deferred_at: The ``now`` of the run that first deferred the work: the run whose own
            range (a fresh fetch) was deferred. A partition deferred out of an interrupted
            stored scope keeps that scope's instant, and so does a scope merged in from a
            concurrent run, so stored work is read oldest deferral first.
    """

    scope: Mapping[str, object]
    deferred_at: datetime


def _normalized_pending(query_id: str, entries: Iterable[_Deferred]) -> tuple[_Deferred, ...]:
    """Return one query's unread scopes once each, ordered by their deferral instant.

    A scope held twice (the same partition deferred again, or stored by two runs) is kept
    at its first position with the earlier of its instants, since that is when its work was
    first deferred. The sort is stable, so scopes of one instant keep their reading order.
    """
    unique: dict[str, _Deferred] = {}
    for entry in entries:
        key = _scope_key(query_id, entry.scope)
        held = unique.get(key)
        if held is None:
            unique[key] = entry
        elif entry.deferred_at < held.deferred_at:
            unique[key] = _Deferred(held.scope, entry.deferred_at)
    return tuple(sorted(unique.values(), key=lambda entry: entry.deferred_at))


def _metrics_pending(value: object, queries: Sequence[str]) -> dict[str, tuple[_Deferred, ...]]:
    """Rebuild a metrics checkpoint's unread scopes per query id, with their deferral instants.

    Raises:
        ValueError: When ``value`` is not an object of non-empty lists keyed by the given
            query ids, an entry is not exactly ``{"scope", "deferred_at"}``, a scope is
            invalid for its template, ends before it starts, is narrowed (a campaign or
            ad-group range, a criterion keyset) over more than one day, which the connector
            never defers, or repeats a scope of its query, or an instant carries no UTC
            offset.
    """
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint pending must be an object keyed by query id")
    if set(value) - set(queries):
        raise ValueError("checkpoint pending names a query this dataset does not read")
    pending: dict[str, tuple[_Deferred, ...]] = {}
    for query_id in queries:
        entries = value.get(query_id)
        if entries is None:
            continue
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{query_id}: checkpoint pending scopes must be a non-empty list")
        rebuilt: list[_Deferred] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != _METRICS_ENTRY_KEYS:
                raise ValueError(
                    f"{query_id}: a pending entry must hold exactly {sorted(_METRICS_ENTRY_KEYS)}"
                )
            scope = scope_from_json(query_id, entry["scope"])
            start, end = _scope_day(scope, _SCOPE_START), _scope_day(scope, _SCOPE_END)
            if start > end:
                raise ValueError("a stored scope ends before it starts")
            if start != end and set(scope) != {_SCOPE_START, _SCOPE_END}:
                raise ValueError("a narrowed stored scope covers a single day")
            key = _scope_key(query_id, scope)
            if key in seen:
                raise ValueError(f"{query_id}: a stored scope is pending twice")
            seen.add(key)
            rebuilt.append(_Deferred(scope, _checkpoint_instant(entry["deferred_at"])))
        pending[query_id] = tuple(rebuilt)
    return pending


@dataclass(frozen=True, slots=True)
class _MetricsCheckpoint:
    """The unread work of one Google metrics dataset (module docstring, "Google checkpoints").

    Attributes:
        pending: Unread dated scopes per query id, each with its deferral instant, in
            reading order; empty once drained.
        covered_from: The first native date the interrupted cycle vouches for once its
            scopes are read.
        reasons: Permanent shortfalls (result limits) met during the cycle.
    """

    pending: Mapping[str, tuple[_Deferred, ...]]
    covered_from: date
    reasons: tuple[str, ...]

    @property
    def total(self) -> int:
        """The number of scopes still unread."""
        return sum(len(entries) for entries in self.pending.values())

    @property
    def latest_end(self) -> date:
        """The last native date any unread scope reaches; ``covered_from`` when none does."""
        return max(
            (
                _scope_day(entry.scope, _SCOPE_END)
                for entries in self.pending.values()
                for entry in entries
            ),
            default=self.covered_from,
        )

    def to_json(self) -> dict[str, Any]:
        """Return the stored JSON object (module docstring, "Google checkpoints")."""
        return {
            "version": _CHECKPOINT_VERSION,
            "pending": {
                query_id: [
                    {
                        "scope": scope_to_json(entry.scope),
                        "deferred_at": entry.deferred_at.isoformat(),
                    }
                    for entry in entries
                ]
                for query_id, entries in self.pending.items()
                if entries
            },
            "covered_from": self.covered_from.isoformat(),
            "reasons": list(self.reasons),
        }

    @classmethod
    def parse(cls, data: object, queries: Sequence[str]) -> _MetricsCheckpoint:
        """Rebuild a stored checkpoint of a dataset that reads ``queries``.

        Raises:
            ValueError: When it is not exactly the object :meth:`to_json` writes, an entry
                fails :func:`_metrics_pending`, or nothing is pending.
        """
        stored = _checkpoint_object(data, _METRICS_CHECKPOINT_KEYS)
        checkpoint = cls(
            pending=_metrics_pending(stored["pending"], queries),
            covered_from=_checkpoint_date(stored["covered_from"]),
            reasons=_checkpoint_reasons(stored["reasons"]),
        )
        if not checkpoint.total:
            raise ValueError("a stored checkpoint holds unread work")
        return checkpoint

    def merged_with(
        self, current: _MetricsCheckpoint | None, loaded: _MetricsCheckpoint | None
    ) -> _MetricsCheckpoint:
        """Add what another run stored meanwhile, so no deferred scope is ever lost.

        ``current`` is the checkpoint stored now, read under the row lock; ``loaded`` the one
        this run started from. When they differ, another run wrote in between: its scopes
        that this run did not load are added with their own deferral instants (a scope is
        identified by its query id and scope, whatever its instant; one this run still
        holds keeps the earlier instant) and its reasons are merged in. The later of the two
        start dates is kept, because each run vouches only for the days from its own start,
        and the two cycles may leave a gap between them. Scopes this run read in the
        meantime may be read again later, which the idempotent upserts make harmless.
        """
        if current is None or (loaded is not None and current.to_json() == loaded.to_json()):
            return self
        known = {
            _scope_key(query_id, entry.scope)
            for query_id, entries in (loaded.pending.items() if loaded is not None else ())
            for entry in entries
        }
        pending = {query_id: list(entries) for query_id, entries in self.pending.items()}
        for query_id, entries in current.pending.items():
            pending.setdefault(query_id, []).extend(
                entry for entry in entries if _scope_key(query_id, entry.scope) not in known
            )
        return _MetricsCheckpoint(
            pending={
                query_id: _normalized_pending(query_id, entries)
                for query_id, entries in pending.items()
                if entries
            },
            covered_from=max(self.covered_from, current.covered_from),
            reasons=tuple(dict.fromkeys((*self.reasons, *current.reasons))),
        )


def _resumed_coverage(
    own: tuple[date, date], checkpoint: _MetricsCheckpoint, row: DatasetFreshness | None
) -> tuple[date, date] | None:
    """Return the range a run resuming ``checkpoint`` vouches for, or ``None`` past a gap.

    The interrupted cycle spans ``covered_from`` to the last date of its unread scopes,
    joined with the stored coverage when that touches it. A run whose own range touches
    the cycle continues it: once nothing is pending it has read everything the cycle left
    unread, so it vouches for the cycle and its own range together, and
    ``record_freshness``'s whole-range rule can turn the row complete. A run whose own range
    leaves a gap of unread days to the cycle (the worker was down longer than the range)
    vouches for its own range only, so no unread day is ever claimed.
    """
    low, high = checkpoint.covered_from, max(checkpoint.latest_end, checkpoint.covered_from)
    if (
        row is not None
        and row.covered_from is not None
        and row.covered_through is not None
        and _touches((row.covered_from, row.covered_through), (low, high))
    ):
        low, high = min(low, row.covered_from), max(high, row.covered_through)
    if not _touches(own, (low, high)):
        return None
    return min(low, own[0]), max(high, own[1])


def _carried_shortfall(
    row: DatasetFreshness | None, own: tuple[date, date], recorded: tuple[date, date]
) -> tuple[str, ...]:
    """Return the stored permanent shortfall a checkpointed run must keep reporting.

    A run that defers work records a repairable reason, which replaces a permanent one the
    stored row holds (a repairable shortfall is never masked), and once its checkpoint
    drains, the range it vouches for (``recorded``) can span the stored range, so the row
    would read complete although the permanent shortfall (a Q-G6 day at the result limit)
    was never re-read. The shortfall is kept when ``recorded`` merges into the stored range
    and the run's ``own`` read did not re-read all of it; otherwise nothing is carried,
    because the run either replaces the stored range or meets the limit again itself.
    """
    shortfall = freshness.recorded_shortfall(row)
    if (
        shortfall is None
        or freshness.is_repairable_reason(shortfall)
        or row is None
        or row.covered_from is None
        or row.covered_through is None
    ):
        return ()
    stored = (row.covered_from, row.covered_through)
    if not _touches(recorded, stored) or (own[0] <= stored[0] and own[1] >= stored[1]):
        return ()
    return (shortfall,)


@dataclass(frozen=True, slots=True)
class _GooglePlan:
    """What one Google metrics run reads for one freshness dataset, decided before any call.

    Attributes:
        name: The freshness dataset.
        queries: Its dated queries, in reading order.
        row: Its freshness row as loaded, or ``None``.
        stored: The checkpoint JSON the row held when loaded, to detect a concurrent writer.
        loaded: That checkpoint parsed, or ``None`` when none is stored or it is unusable.
        own: The run's own native range (:func:`_metrics_range`).
        resumed: The range the run vouches for when it continues ``loaded``
            (:func:`_resumed_coverage`), or ``None``.
        segments: Per query, the parts of ``own`` no stored scope of that query covers
            (:func:`_own_segments`).
    """

    name: str
    queries: tuple[str, ...]
    row: DatasetFreshness | None
    stored: object | None
    loaded: _MetricsCheckpoint | None
    own: tuple[date, date]
    resumed: tuple[date, date] | None
    segments: Mapping[str, tuple[Mapping[str, object], ...]]

    @property
    def recorded(self) -> tuple[date, date]:
        """The range the dataset's freshness records: ``resumed``, else the full own range."""
        return self.own if self.resumed is None else self.resumed

    def parse(self, data: object) -> _MetricsCheckpoint:
        """Parse a checkpoint of this dataset (:meth:`_MetricsCheckpoint.parse`)."""
        return _MetricsCheckpoint.parse(data, self.queries)


@dataclass(frozen=True, slots=True)
class _Written:
    """What writing one fetch or listing changed inside its dataset's transaction.

    Attributes:
        rows: Fact rows upserted.
        removed: Stored rows deleted because the proven-complete fetch no longer returned them
            (:func:`_remove_absent`).
        skipped: Rows read but not stored, because the entity they name is not stored.
    """

    rows: int = 0
    removed: int = 0
    skipped: int = 0

    @property
    def changed(self) -> bool:
        """Whether anything was written or deleted."""
        return bool(self.rows or self.removed)

    def __add__(self, other: _Written) -> _Written:
        """Add two writes' counts."""
        return _Written(
            rows=self.rows + other.rows,
            removed=self.removed + other.removed,
            skipped=self.skipped + other.skipped,
        )


# Writes one fetch's rows in the dataset's transaction; the range is the scope's
# :func:`_date_range`.
type _FetchWrite = Callable[
    [AsyncSession, FetchOutcome, tuple[date, date] | None], Awaitable[_Written]
]


@dataclass(frozen=True, slots=True)
class _Fetched:
    """One fetch of a metrics run, kept until the transaction that records its dataset.

    Attributes:
        query_id: The query the fetch read.
        outcome: What the fetch returned.
        covered: The scope's :func:`_date_range`: the native days it read in full, or
            ``None`` for a scope narrowed inside its days.
    """

    query_id: str
    outcome: FetchOutcome
    covered: tuple[date, date] | None


def _date_range(scope: Mapping[str, object]) -> tuple[date, date] | None:
    """Return the native ``[start, end]`` a scope reads in full, or ``None`` if it narrows.

    Only a scope that is a date range alone (an own segment, a stored date range) asks for
    every row of its days. One with campaign or ad-group bounds or a criterion keyset asks
    for part of its day, so even a complete fetch of it says nothing about the rest of that
    day, and the rows stored there must not be reconciled against it.
    """
    start, end = scope.get(_SCOPE_START), scope.get(_SCOPE_END)
    if set(scope) != {_SCOPE_START, _SCOPE_END}:
        return None
    if not isinstance(start, date) or not isinstance(end, date):
        return None
    return (start, end)


class _QueryReads:
    """What one query's fetches in one metrics run established, in reading order.

    The first interruption stops the query: every later scope of it is kept unread, with
    its deferral instant, behind the interrupted fetch's deferred partitions, which keep the
    instant of the scope they came from.

    Attributes:
        query_id: The query read.
        write: Writes one of the query's fetches in its dataset's transaction.
        fetched: The run's fetches of every query, in reading order, shared by the run's
            reads; each read appends its own.
        pending: The query's unread scopes, in reading order.
        interrupted_by: The transient error that stopped the query, or ``None``.
        shortfalls: Permanent shortfalls (result limits) of the fetches read.
        calls: POST attempts the fetches made, retries and next pages included.
    """

    __slots__ = (
        "calls",
        "fetched",
        "interrupted_by",
        "pending",
        "query_id",
        "shortfalls",
        "write",
    )

    def __init__(self, query_id: str, write: _FetchWrite, fetched: list[_Fetched]) -> None:
        """Start with nothing read; each fetch is kept in ``fetched`` for ``write`` later."""
        self.query_id = query_id
        self.write = write
        self.fetched = fetched
        self.pending: list[_Deferred] = []
        self.interrupted_by: TransientError | None = None
        self.shortfalls: list[str] = []
        self.calls = 0

    async def read(
        self, fetcher: PartitionedFetcher, scope: Mapping[str, object], deferred_at: datetime
    ) -> None:
        """Read one scope, or keep it unread with ``deferred_at`` once the query is interrupted.

        The fetch is kept in :attr:`fetched` with the scope's :func:`_date_range`, and nothing
        is written yet: the transaction that records the query's dataset writes every kept
        fetch in reading order (:meth:`MetricsSyncJob._record_google`), so rows read before an
        interruption are stored with that dataset's outcome; upserts are idempotent, so
        re-reading a scope later changes no row count.
        """
        if self.interrupted_by is not None:
            self.pending.append(_Deferred(scope, deferred_at))
            return
        outcome = await fetcher.partitioned_fetch(self.query_id, scope)
        self.calls += outcome.calls
        self.fetched.append(_Fetched(self.query_id, outcome, _date_range(scope)))
        self.shortfalls.extend(_shortfall_reasons(outcome))
        if outcome.interrupted_by is not None:
            self.interrupted_by = outcome.interrupted_by
            self.pending.extend(_Deferred(part, deferred_at) for part in outcome.deferred)


def _stored_reading_order(plans: Sequence[_GooglePlan]) -> list[tuple[str, _Deferred]]:
    """Return every stored scope of the datasets' checkpoints, oldest deferral first.

    Ties go by dataset order (``google.metrics_daily``, then ``google.search_terms``), then
    query order (Q-G3, Q-G5; Q-G6), then position in the stored list, so no dataset's or
    query's older work waits behind another's newer work.
    """
    ordered: list[tuple[tuple[datetime, int, int, int], str, _Deferred]] = []
    for dataset_index, plan in enumerate(plans):
        if plan.loaded is None:
            continue
        for query_index, query_id in enumerate(plan.queries):
            for position, entry in enumerate(plan.loaded.pending.get(query_id, ())):
                key = (entry.deferred_at, dataset_index, query_index, position)
                ordered.append((key, query_id, entry))
    ordered.sort(key=lambda item: item[0])
    return [(query_id, entry) for _key, query_id, entry in ordered]


# The fact tables whose fetch scopes :func:`_remove_absent` reconciles.
type _ReconciledFact = (
    FactCampaignDaily
    | FactCreativeDaily
    | FactKeywordDaily
    | FactSearchTermDaily
    | FactSegmentWindow
)


def _proven_complete(fetch: FetchOutcome | Listing[Any]) -> bool:
    """Whether a fetch read its whole scope: complete, and not interrupted.

    Only such a fetch shows that a stored key it did not return is no longer reported. A
    deferred, interrupted or capped read says nothing about what it did not reach, so its
    rows are only upserted and the last good data stands (AAP 0.3.3 "Budget", transient row).
    """
    return fetch.complete and fetch.interrupted_by is None


def _any_id(attribute: InstrumentedAttribute[int], ids: Iterable[int]) -> ColumnElement[bool]:
    """``attribute = ANY(:ids)``: one array parameter however many ids, never one bind per id."""
    clause: ColumnElement[bool] = attribute == any_(literal(sorted(ids), ARRAY(BigInteger())))
    return clause


async def _remove_absent(
    session: AsyncSession,
    model: type[_ReconciledFact],
    dataset_id: int,
    *,
    scope: Sequence[ColumnElement[bool]],
    key: Sequence[InstrumentedAttribute[Any]],
    returned: set[tuple[Any, ...]],
    guarded: bool,
    event: str,
    fields: Mapping[str, object],
) -> int:
    """Delete the stored facts of one proven-complete fetch scope that the fetch did not return.

    AAP 0.1.1 ("Restated platform data overwrites earlier values") and 0.3.2 (daily facts are
    restated; segment rows belong to their exact window). Once a :func:`_proven_complete`
    re-fetch of a scope is upserted, a stored row inside that scope whose natural key the
    fetch no longer returned holds values the platform has withdrawn, so it is removed in the
    same transaction as the upsert, and the scope then holds exactly what the platform
    reports for it, nothing at all after an empty complete answer. Rows outside ``scope``
    (other days, windows, facets, accounts or datasets) are neither read nor touched, and the
    run tables are never involved: their evidence is denormalised ``jsonb``.

    The scope's ``(id, key)`` pairs are read with a fixed number of bind parameters (id sets
    travel as one array, :func:`_any_id`), diffed against ``returned`` here, and the obsolete
    rows deleted by id in ascending order, at most :data:`_ID_CHUNK` per statement, so a
    reconcile of any size stays under PostgreSQL's bind-parameter limit. Running the same
    fetch again removes nothing more.

    Args:
        session: The open transaction that upserted the fetch's rows.
        model: The fact table.
        dataset_id: The dataset written; every read and delete is limited to it.
        scope: The conditions, besides ``dataset_id``, that select exactly the fetched scope.
        key: The natural-key columns that vary inside the scope, in the order of ``returned``.
        returned: The keys of the returned rows that resolve to stored entities.
        guarded: Whether the transaction already holds the dataset guard (an upsert or a stub
            insert in it wrote rows); when not, :func:`repositories.ensure_writable` takes it.
        event: The log event that reports a removal.
        fields: The context logged with it (source, query id or facet, scope).

    Returns:
        The number of rows removed.

    Raises:
        repositories.DatasetNotWritable: If the dataset is missing or retired.
    """
    if not guarded:
        await repositories.ensure_writable(session, dataset_id)
    result = await session.execute(
        select(model.id, *key).where(model.dataset_id == dataset_id, *scope)
    )
    obsolete = sorted(int(row[0]) for row in result if tuple(row[1:]) not in returned)
    for chunk in _chunks(obsolete):
        await session.execute(
            delete(model)
            .where(model.dataset_id == dataset_id, model.id.in_(chunk))
            .execution_options(synchronize_session=False)
        )
    if obsolete:
        _log.info(event, dataset_id=dataset_id, rows=len(obsolete), **fields)
    return len(obsolete)


class MetricsSyncJob(_JobBase):
    """Daily facts of one Google or LinkedIn dataset over a native date range.

    Facts are stored per native day and never as window aggregates (AAP 0.3.2): Google days
    in the customer account's time zone (Q-G1's ``customer.time_zone``, stored on the
    dataset's ``ad_account``), LinkedIn days in UTC; nothing is rebucketed (decision 6).
    Every upsert overwrites the stored day and stamps ``fetched_at``, so a restatement
    replaces earlier values and a re-run changes no row count.

    Restatement also withdraws rows (AAP 0.1.1, 0.3.2). When a fetch is
    :func:`_proven_complete`, the rows stored inside its scope whose key it no longer
    returned are deleted in the transaction that upserts it (:func:`_remove_absent`), an
    empty answer included, so the scope holds exactly what the platform now reports. An
    incomplete, deferred or interrupted fetch only upserts, and every other stored row stands
    ("last good data is kept", AAP 0.3.3).

    * **Google**: Q-G3 to ``fact_campaign_daily`` and Q-G5 to ``fact_keyword_daily``
      (``google.metrics_daily``), Q-G6 to ``fact_search_term_daily``
      (``google.search_terms``). Every fetch is kept until its dataset's last read is done,
      then the dataset's fetches are written in reading order in the one transaction that
      records its checkpoint and freshness (:meth:`_record_google`). A campaign or ad group no
      entity sync stored (removed before tracking) gets a stand-in so its spend counts; a
      keyword row of an unknown keyword is skipped and counted (campaign spend is covered
      by Q-G3, and K3 reviews current keywords only). The unread scopes both datasets'
      checkpoints hold (module docstring, "Google checkpoints") are read first, as stored
      and oldest deferral first across datasets and queries, so one dataset's fresh range
      never spends a small window's budget before another's older work. Then come the own
      ranges, Q-G3, Q-G5, Q-G6, each without the native days a stored scope of its query
      covers (those days are read by the stored scopes, a narrowed keyset or id range
      keeping its progress). Whatever stays unread, this run's and any another run stored
      meanwhile, is written back as the checkpoint in the transaction that records the
      dataset's freshness, so a workload above the daily call cap advances through its
      partitions run by run instead of restarting from the same prefix, and the dataset
      turns ``complete`` only once nothing is pending. A fetch's reconcile scope is the
      dataset (one customer) over the native ``[start, end]`` of the scope it read, stand-in
      campaigns included, when that scope is a date range alone (an own segment or a stored
      date range). A stored scope narrowed inside its day (campaign or ad-group bounds, a
      criterion keyset) reads only part of that day, so its rows are only upserted.
    * **LinkedIn**: per account, daily creative analytics to ``fact_creative_daily``
      (``linkedin.analytics_daily``); ``conversions`` and ``conversions_value`` stay
      ``NULL`` (LinkedIn reports leads and website conversions instead). Rows of creatives
      the dataset does not know are counted and make the dataset ``incomplete``. An
      account's reconcile scope is the creatives of its campaigns over the fetched
      ``[start, end]``. The creatives are read once per job, not once per account. Every
      account's listing is read first; one transaction then writes them all with the
      dataset's outcome.

    Args of the constructor: ``kind`` selects the range (:func:`_metrics_range`); a
    ``restatement`` run records ``sync_run`` rows under :data:`JOB_RESTATEMENT`, a
    ``backfill`` under both :data:`JOB_METRICS` and :data:`JOB_RESTATEMENT`, because its
    range covers both slots and the first scheduler tick must not repeat it.
    """

    def __init__(self, ctx: JobContext, kind: MetricsKind = "incremental") -> None:
        """Bind the job and its range kind.

        Raises:
            ValueError: If the context is a HubSpot dataset's or ``kind`` is unknown.
        """
        if ctx.source == "hubspot":
            raise ValueError("HubSpot has no daily metrics")
        if kind not in get_args(MetricsKind):
            raise ValueError(f"unknown metrics kind {kind!r}")
        super().__init__(ctx)
        self.kind: MetricsKind = kind
        self.job = JOB_RESTATEMENT if kind == "restatement" else JOB_METRICS

    def freshness_datasets(self) -> tuple[str, ...]:
        """Google: ``google.metrics_daily`` and ``google.search_terms``; LinkedIn analytics."""
        if self.ctx.source == "google":
            return (DATASET_GOOGLE_METRICS_DAILY, DATASET_GOOGLE_SEARCH_TERMS)
        return (DATASET_LINKEDIN_ANALYTICS_DAILY,)

    def sync_run_jobs(self) -> tuple[str, ...]:
        """A backfill satisfies both the incremental and the restatement slot."""
        if self.kind == "backfill":
            return (JOB_METRICS, JOB_RESTATEMENT)
        return (self.job,)

    async def _execute(self) -> None:
        """Run the source's metrics fetch."""
        _log.info(
            "sync.metrics.kind",
            source=self.ctx.source,
            dataset_id=self.ctx.dataset_id,
            kind=self.kind,
        )
        if self.ctx.source == "google":
            await self._google()
        else:
            await self._linkedin()

    async def _ranges(self, today: date) -> dict[str, tuple[date, date]]:
        """The range of every freshness dataset of the job."""
        ctx = self.ctx
        async with ctx.sessionmaker() as session:
            rows = await freshness.load_freshness(session, ctx.dataset_id)
        return {
            name: _metrics_range(self.kind, ctx.source, today, ctx.settings, rows.get(name))
            for name in self.freshness_datasets()
        }

    # -- Google ----------------------------------------------------------------------------

    async def _google(self) -> None:
        """Fetch and store Q-G3, Q-G5 and Q-G6, stored work first (class docstring).

        1. Both datasets' checkpoints are loaded and each dataset's own range is decided.
        2. Every stored scope of both datasets is read, as stored, before any own range,
           oldest deferral first (:func:`_stored_reading_order`).
        3. The own ranges are read in the order Q-G3, Q-G5, Q-G6, each without the days a
           stored scope of its query covers (:func:`_own_segments`).
        4. Each dataset is recorded (:meth:`_record_google`) as soon as its last read is
           done: one transaction writes its fetches in reading order with its checkpoint and
           outcome, so a defect raised by a later dataset's own range leaves
           ``google.metrics_daily`` recorded, and an error or a cancellation before a
           dataset's transaction commits leaves that dataset as it was.

        Each query stops at its first interruption; its later scopes stay unread.
        """
        ctx = self.ctx
        fetcher = PartitionedFetcher(ctx.clients.require_google())
        async with ctx.sessionmaker() as session:
            account = await _account_row(session, ctx.dataset_id, "google")
            rows = await freshness.load_freshness(session, ctx.dataset_id)
        account_id = None if account is None else account.id
        today = ctx.now.astimezone(_zone(None if account is None else account.timezone)).date()

        async def campaign_daily(
            session: AsyncSession, outcome: FetchOutcome, covered: tuple[date, date] | None
        ) -> _Written:
            return await self._store_campaign_daily(session, outcome, account_id, covered)

        async def search_terms(
            session: AsyncSession, outcome: FetchOutcome, covered: tuple[date, date] | None
        ) -> _Written:
            return await self._store_search_terms(session, outcome, account_id, covered)

        writes: Mapping[str, _FetchWrite] = {
            "Q-G3": campaign_daily,
            "Q-G5": self._store_keyword_daily,
            "Q-G6": search_terms,
        }
        plans = [self._google_plan(name, today, rows.get(name)) for name in _GOOGLE_METRICS_QUERIES]
        fetched: list[_Fetched] = []
        reads = {
            query_id: _QueryReads(query_id, writes[query_id], fetched)
            for plan in plans
            for query_id in plan.queries
        }
        for query_id, entry in _stored_reading_order(plans):
            await reads[query_id].read(fetcher, entry.scope, entry.deferred_at)
        for plan in plans:
            for query_id in plan.queries:
                for segment in plan.segments[query_id]:
                    await reads[query_id].read(fetcher, segment, ctx.now)
            mine = [entry for entry in fetched if entry.query_id in plan.queries]
            await self._record_google(plan, [reads[query_id] for query_id in plan.queries], mine)
            # Recorded: the dataset's fetches are no longer needed.
            fetched[:] = [entry for entry in fetched if entry.query_id not in plan.queries]

    def _google_plan(self, name: str, today: date, row: DatasetFreshness | None) -> _GooglePlan:
        """Decide what this run reads for one Google metrics dataset (:class:`_GooglePlan`)."""
        ctx = self.ctx
        queries = _GOOGLE_METRICS_QUERIES[name]
        stored = None if row is None else row.checkpoint

        def parse(data: object) -> _MetricsCheckpoint:
            return _MetricsCheckpoint.parse(data, queries)

        loaded = _usable_checkpoint(
            parse, stored, job=self.job, dataset_id=ctx.dataset_id, dataset=name
        )
        own = _metrics_range(self.kind, "google", today, ctx.settings, row, repair=loaded is None)
        return _GooglePlan(
            name=name,
            queries=queries,
            row=row,
            stored=stored,
            loaded=loaded,
            own=own,
            resumed=None if loaded is None else _resumed_coverage(own, loaded, row),
            segments={
                query_id: _own_segments(
                    own, () if loaded is None else loaded.pending.get(query_id, ())
                )
                for query_id in queries
            },
        )

    async def _record_google(
        self, plan: _GooglePlan, reads: Sequence[_QueryReads], fetched: Sequence[_Fetched]
    ) -> None:
        """Write and record one Google metrics dataset after its reads, in one transaction.

        The freshness row is locked first, so two runs publishing the same dataset queue
        rather than interleave their fact writes. ``fetched`` (the dataset's fetches, in
        reading order) is then written with each query's write, upserts and complete-scope
        deletions in the order they were read. What stays unread is merged with what another
        run stored meanwhile and written back (or the checkpoint cleared), and in the same
        transaction:

        * nothing pending: a success, ``complete`` unless a result limit was met;
        * work pending after a call-budget deferral: an ``incomplete`` success, repairable,
          its reason (:data:`~ads_engine.sync.freshness.REASON_DEFERRED` prefix) counting
          every pending scope, followed by the permanent shortfalls;
        * any other transient interruption fails the refresh instead: the checkpoint is
          still written, the failed attempt is recorded with the rows read, so the dataset
          keeps its last good standing and turns stale, and the rerun is queued when those
          rows changed anything.

        The recorded range is the full own range (or the resumed cycle's), whatever days the
        own read left to stored scopes: those days are vouched for once nothing is pending.

        Args:
            plan: What the run read for the dataset.
            reads: The reads of the dataset's queries.
            fetched: The dataset's fetches, in reading order.

        Raises:
            repositories.DatasetNotWritable: If the dataset was retired meanwhile.
        """
        ctx = self.ctx
        name, row, stored, loaded = plan.name, plan.row, plan.stored, plan.loaded
        recorded = plan.recorded
        interruptions = [error for read in reads if (error := read.interrupted_by) is not None]
        failure = next(
            (error for error in interruptions if not isinstance(error, CallBudgetExhausted)), None
        )
        pending = {
            read.query_id: _normalized_pending(read.query_id, read.pending)
            for read in reads
            if read.pending
        }
        carried = (
            _carried_shortfall(row, plan.own, recorded) if pending or loaded is not None else ()
        )
        inherited = () if loaded is None or plan.resumed is None else loaded.reasons
        mine = _MetricsCheckpoint(
            pending=pending,
            covered_from=recorded[0],
            reasons=tuple(
                dict.fromkeys(
                    (
                        *(reason for read in reads for reason in read.shortfalls),
                        *inherited,
                        *carried,
                    )
                )
            ),
        )
        self._calls += sum(read.calls for read in reads)
        writes = {read.query_id: read.write for read in reads}

        async with ctx.sessionmaker() as session, session.begin():
            now_stored = await freshness.lock_checkpoint(session, ctx.dataset_id, name)
            written = _Written()
            for entry in fetched:
                written += await writes[entry.query_id](session, entry.outcome, entry.covered)
            current = (
                loaded
                if now_stored == stored
                else _usable_checkpoint(
                    plan.parse, now_stored, job=self.job, dataset_id=ctx.dataset_id, dataset=name
                )
            )
            merged = mine.merged_with(current, loaded)
            await freshness.write_checkpoint(
                session, ctx.dataset_id, name, merged.to_json() if merged.total else None
            )
            if failure is None:
                deferred = (
                    _deferred_reason(interruptions[0] if interruptions else None, merged.total)
                    if merged.total
                    else None
                )
                outcome = _success(ctx.now, covered=recorded, reasons=(deferred, *merged.reasons))
            else:
                outcome = FreshnessOutcome.failure(ctx.now, reason=failure.reason)
            await self._complete(
                session, {name: outcome}, changed=written.changed, rows={name: written.rows}
            )
        _log.info(
            "sync.metrics.google_checkpoint",
            source="google",
            dataset_id=ctx.dataset_id,
            dataset=name,
            resumed=loaded is not None,
            pending=merged.total,
        )
        if failure is not None:
            self._interrupted(failure, dataset=name)
        elif interruptions:
            self._interrupted(interruptions[0], dataset=None)

    async def _store_campaign_daily(
        self,
        session: AsyncSession,
        outcome: FetchOutcome,
        account_id: int | None,
        covered: tuple[date, date] | None,
    ) -> _Written:
        """Store Q-G3 over ``covered`` in ``fact_campaign_daily``, stubbing unknown campaigns.

        The rows are upserted in ``session``'s transaction, the one that records
        ``google.metrics_daily`` (:meth:`_record_google`); nothing is committed here. When
        the fetch is :func:`_proven_complete`, the dataset's rows dated inside ``covered``
        whose (campaign, day) it did not return are deleted in the same transaction
        (:func:`_remove_absent`), also when it returned no row at all. ``covered`` is
        ``None`` for a scope narrowed inside its days (campaign bounds), which reads only
        part of each day, so its rows are only upserted.

        Returns:
            The facts upserted, the rows removed and the rows skipped for want of an account.
        """
        ctx = self.ctx
        rows = [parse_campaign_daily(row) for row in outcome.rows]
        reconciled = covered if covered is not None and _proven_complete(outcome) else None
        if not rows and reconciled is None:
            return _Written()
        campaigns = await _campaign_ids(session, ctx.dataset_id)
        unknown = {str(row.campaign_id) for row in rows} - set(campaigns)
        if unknown and account_id is not None:
            await _stub_campaigns(session, ctx.dataset_id, account_id, unknown, campaigns)
        facts = [
            {
                "campaign_id": campaigns[str(row.campaign_id)],
                "date": row.date,
                "cost": row.cost,
                "impressions": row.impressions,
                "clicks": row.clicks,
                "conversions": row.conversions,
                "conversions_value": row.conversions_value,
                "budget_lost_is": row.search_budget_lost_impression_share,
                "impression_share": row.search_impression_share,
            }
            for row in rows
            if str(row.campaign_id) in campaigns
        ]
        written = await repositories.upsert_facts(
            FactCampaignDaily, facts, ctx.dataset_id, session=session
        )
        removed = 0
        if reconciled is not None:
            removed = await _remove_absent(
                session,
                FactCampaignDaily,
                ctx.dataset_id,
                scope=(FactCampaignDaily.date.between(*reconciled),),
                key=(FactCampaignDaily.campaign_id, FactCampaignDaily.date),
                returned={(fact["campaign_id"], fact["date"]) for fact in facts},
                guarded=bool(facts),
                event="sync.metrics.rows_removed",
                fields={"source": "google", "query_id": "Q-G3"},
            )
        skipped = len(rows) - len(facts)
        if skipped:
            _log.warning(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G3",
                rows=skipped,
                reason="no Google account stored to attach stand-in campaigns to",
            )
        return _Written(rows=written, removed=removed, skipped=skipped)

    async def _store_keyword_daily(
        self, session: AsyncSession, outcome: FetchOutcome, covered: tuple[date, date] | None
    ) -> _Written:
        """Store Q-G5 over ``covered`` in ``fact_keyword_daily``; unknown keywords are skipped.

        The rows are upserted in ``session``'s transaction, the one that records
        ``google.metrics_daily`` (:meth:`_record_google`); nothing is committed here. When
        the fetch is :func:`_proven_complete`, the dataset's rows dated inside ``covered``
        whose (keyword, day) it did not return are deleted in the same transaction
        (:func:`_remove_absent`), also when it returned no row at all. ``covered`` is
        ``None`` for a scope narrowed inside its days (campaign or ad-group bounds, a
        criterion keyset), which reads only part of each day, so its rows are only upserted.

        Returns:
            The facts upserted, the rows removed and the rows of unknown keywords skipped.
        """
        ctx = self.ctx
        rows = [parse_keyword_view(row) for row in outcome.rows]
        reconciled = covered if covered is not None and _proven_complete(outcome) else None
        if not rows and reconciled is None:
            return _Written()
        groups = _by_last_key(await repositories.entity_id_map(session, AdGroup, ctx.dataset_id))
        keywords = {
            (key[1], str(key[2])): keyword_id
            for key, keyword_id in (
                await repositories.entity_id_map(session, Keyword, ctx.dataset_id)
            ).items()
        }
        facts: list[dict[str, Any]] = []
        for row in rows:
            group_id = groups.get(str(row.ad_group_id))
            keyword_id = (
                None if group_id is None else keywords.get((group_id, str(row.criterion_id)))
            )
            if keyword_id is None:
                continue
            facts.append(
                {
                    "keyword_id": keyword_id,
                    "date": row.date,
                    "cost": row.cost,
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "conversions": row.conversions,
                    "historical_quality_score": row.historical_quality_score,
                    "average_cpc": row.average_cpc,
                }
            )
        written = await repositories.upsert_facts(
            FactKeywordDaily, facts, ctx.dataset_id, session=session
        )
        removed = 0
        if reconciled is not None:
            removed = await _remove_absent(
                session,
                FactKeywordDaily,
                ctx.dataset_id,
                scope=(FactKeywordDaily.date.between(*reconciled),),
                key=(FactKeywordDaily.keyword_id, FactKeywordDaily.date),
                returned={(fact["keyword_id"], fact["date"]) for fact in facts},
                guarded=bool(facts),
                event="sync.metrics.rows_removed",
                fields={"source": "google", "query_id": "Q-G5"},
            )
        skipped = len(rows) - len(facts)
        if skipped:
            # Campaign spend is complete through Q-G3; K3 reviews current keywords only.
            _log.info(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G5",
                rows=skipped,
                reason="keyword not in the current keyword list",
            )
        return _Written(rows=written, removed=removed, skipped=skipped)

    async def _store_search_terms(
        self,
        session: AsyncSession,
        outcome: FetchOutcome,
        account_id: int | None,
        covered: tuple[date, date] | None,
    ) -> _Written:
        """Store Q-G6 over ``covered`` in ``fact_search_term_daily``, stubbing unknown parents.

        The rows are upserted in ``session``'s transaction, the one that records
        ``google.search_terms`` (:meth:`_record_google`); nothing is committed here. When the
        fetch is :func:`_proven_complete`, the dataset's rows dated inside ``covered`` whose
        (ad group, matched criterion, term, day) it did not return are deleted in the same
        transaction (:func:`_remove_absent`), also when it returned no row at all. A finest
        (day, ad group) partition at the result limit makes the fetch incomplete, so its rows
        are stored as returned and nothing is deleted. ``covered`` is ``None`` for a scope
        narrowed inside its days (campaign or ad-group bounds), which reads only part of each
        day, so its rows are only upserted.

        Returns:
            The facts upserted, the rows removed and the rows skipped for want of an account.
        """
        ctx = self.ctx
        rows = [parse_search_term(row) for row in outcome.rows]
        reconciled = covered if covered is not None and _proven_complete(outcome) else None
        if not rows and reconciled is None:
            return _Written()
        campaigns = await _campaign_ids(session, ctx.dataset_id)
        unknown = {str(row.campaign_id) for row in rows} - set(campaigns)
        if unknown and account_id is not None:
            await _stub_campaigns(session, ctx.dataset_id, account_id, unknown, campaigns)
        groups = _by_last_key(await repositories.entity_id_map(session, AdGroup, ctx.dataset_id))
        await _stub_ad_groups(
            session,
            ctx.dataset_id,
            {
                str(row.ad_group_id): campaigns[str(row.campaign_id)]
                for row in rows
                if str(row.campaign_id) in campaigns
            },
            groups,
        )
        facts = [
            {
                "ad_group_id": groups[str(row.ad_group_id)],
                "campaign_id": campaigns[str(row.campaign_id)],
                "date": row.date,
                "search_term": row.search_term,
                "term_status": row.term_status,
                "matched_criterion_id": row.matched_criterion_id,
                "matched_keyword_text": row.matched_keyword_text,
                "matched_match_type": row.matched_match_type,
                "cost": row.cost,
                "impressions": row.impressions,
                "clicks": row.clicks,
                "conversions": row.conversions,
                "conversions_value": row.conversions_value,
            }
            for row in rows
            if str(row.campaign_id) in campaigns and str(row.ad_group_id) in groups
        ]
        written = await repositories.upsert_facts(
            FactSearchTermDaily, facts, ctx.dataset_id, session=session
        )
        removed = 0
        if reconciled is not None:
            # Keyed on every returned row whose ad group is stored, even one skipped for an
            # unattachable campaign, so a returned key is never taken for an absent one.
            removed = await _remove_absent(
                session,
                FactSearchTermDaily,
                ctx.dataset_id,
                scope=(FactSearchTermDaily.date.between(*reconciled),),
                key=(
                    FactSearchTermDaily.ad_group_id,
                    FactSearchTermDaily.matched_criterion_id,
                    FactSearchTermDaily.search_term,
                    FactSearchTermDaily.date,
                ),
                returned={
                    (
                        groups[str(row.ad_group_id)],
                        row.matched_criterion_id,
                        row.search_term,
                        row.date,
                    )
                    for row in rows
                    if str(row.ad_group_id) in groups
                },
                guarded=bool(facts),
                event="sync.metrics.rows_removed",
                fields={"source": "google", "query_id": "Q-G6"},
            )
        skipped = len(rows) - len(facts)
        if skipped:
            _log.warning(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G6",
                rows=skipped,
                reason="no Google account stored to attach stand-in campaigns to",
            )
        return _Written(rows=written, removed=removed, skipped=skipped)

    # -- LinkedIn --------------------------------------------------------------------------

    async def _linkedin(self) -> None:
        """Fetch and store daily creative analytics per account (class docstring).

        The dataset's creatives are read once for the job (:func:`_linkedin_creatives`): the
        URN map every account's rows resolve through, and each account's creative ids, which
        size its date chunks and bound its reconcile scope. The job stores no creative, so the
        map stays exact for its whole run.

        Every account's listing is read first, with no transaction open. One transaction then
        writes every listing and records the dataset: a success, or, when a transient error
        interrupted a listing, the failed attempt together with the rows read (and the rerun
        they need); the error is handed over once it committed.
        """
        ctx = self.ctx
        name = DATASET_LINKEDIN_ANALYTICS_DAILY
        client = ctx.clients.require_linkedin()
        today = ctx.now.astimezone(UTC).date()
        start, end = (await self._ranges(today))[name]
        async with ctx.sessionmaker() as session:
            creatives = await _linkedin_creatives(session, ctx.dataset_id)
        reasons: list[str | None] = []
        failure: TransientError | None = None
        listings: list[tuple[Listing[CreativeDailyRow], frozenset[int]]] = []
        for account_id in ctx.clients.linkedin_account_ids:
            scope = creatives.of(account_id)
            listing = await client.analytics_daily(
                account_id, (start, end), expected_creatives=len(scope) or None
            )
            self._calls += listing.calls
            listings.append((listing, scope))
            if listing.interrupted_by is not None and failure is None:
                failure = listing.interrupted_by
            if not listing.complete:
                reasons.append(listing.reason or "analytics listing incomplete")
        async with ctx.sessionmaker() as session, session.begin():
            written = _Written()
            for listing, scope in listings:
                written += await self._store_creative_daily(
                    session, listing, (start, end), creatives.by_key, scope
                )
            if failure is not None:
                outcome = FreshnessOutcome.failure(ctx.now, reason=failure.reason)
            else:
                if written.skipped:
                    noun = _plural(written.skipped, "row", "rows")
                    reasons.append(f"{noun} for unknown creatives")
                outcome = _success(ctx.now, covered=(start, end), reasons=reasons)
            await self._complete(
                session, {name: outcome}, changed=written.changed, rows={name: written.rows}
            )
        if failure is not None:
            self._interrupted(failure, dataset=name)

    async def _store_creative_daily(
        self,
        session: AsyncSession,
        listing: Listing[CreativeDailyRow],
        covered: tuple[date, date],
        creatives: Mapping[str, int],
        scope: frozenset[int],
    ) -> _Written:
        """Store one account's creative-day rows in the dataset's transaction.

        The rows are upserted in ``session``'s transaction, the one that records
        ``linkedin.analytics_daily``; nothing is committed here. When the account's listing
        is :func:`_proven_complete`, the rows of the account's creatives (``scope``) dated
        inside ``covered`` whose (creative, day) it did not return are deleted in the same
        transaction (:func:`_remove_absent`), also when it returned no row at all.

        Args:
            session: The transaction that records the dataset.
            listing: The account's daily analytics listing.
            covered: The UTC ``[start, end]`` the listing read.
            creatives: The dataset's creative ids by URN, read once per job.
            scope: The ids of the account's creatives, read once per job.

        Returns:
            The facts upserted, the rows removed and the rows whose creative the dataset
            does not store.
        """
        ctx = self.ctx
        rows = listing.rows
        reconcile = _proven_complete(listing) and bool(scope)
        if not rows and not reconcile:
            return _Written()
        facts = [
            {
                "creative_id": creatives[row.creative_urn],
                "date": row.date,
                "cost": row.cost,
                "impressions": row.impressions,
                "clicks": row.clicks,
                "conversions": None,
                "conversions_value": None,
                "form_opens": row.form_opens,
                "leads": row.leads,
                "landing_page_clicks": row.landing_page_clicks,
                "website_conversions": row.website_conversions,
            }
            for row in rows
            if row.creative_urn in creatives
        ]
        written = await repositories.upsert_facts(
            FactCreativeDaily, facts, ctx.dataset_id, session=session
        )
        removed = 0
        if reconcile:
            removed = await _remove_absent(
                session,
                FactCreativeDaily,
                ctx.dataset_id,
                scope=(
                    _any_id(FactCreativeDaily.creative_id, scope),
                    FactCreativeDaily.date.between(*covered),
                ),
                key=(FactCreativeDaily.creative_id, FactCreativeDaily.date),
                returned={(fact["creative_id"], fact["date"]) for fact in facts},
                guarded=bool(facts),
                event="sync.metrics.rows_removed",
                fields={"source": "linkedin", "surface": "ad_analytics"},
            )
        unknown = len(rows) - len(facts)
        if unknown:
            _log.warning(
                "sync.metrics.rows_skipped",
                source="linkedin",
                rows=unknown,
                reason="creative not stored in the dataset",
            )
        return _Written(rows=written, removed=removed, skipped=unknown)


@dataclass(frozen=True, slots=True)
class _AccountEntities:
    """A LinkedIn dataset's campaigns or creatives, read once per job.

    Attributes:
        by_key: Internal id by external key: a campaign's external id or a creative's URN.
        by_account: Internal ids by ad account external id (numeric), the reconcile scopes.
    """

    by_key: Mapping[str, int]
    by_account: Mapping[str, frozenset[int]]

    @classmethod
    def from_rows(cls, rows: Iterable[tuple[str, int, str]]) -> _AccountEntities:
        """Build both maps from ``(external key, id, account external id)`` rows."""
        by_key: dict[str, int] = {}
        by_account: dict[str, set[int]] = {}
        for key, entity_id, account in rows:
            by_key[str(key)] = entity_id
            by_account.setdefault(str(account), set()).add(entity_id)
        return cls(
            by_key=MappingProxyType(by_key),
            by_account=MappingProxyType(
                {account: frozenset(ids) for account, ids in by_account.items()}
            ),
        )

    def of(self, account_id: str) -> frozenset[int]:
        """The ids under one configured ad account (numeric id or URN); empty when none."""
        return self.by_account.get(_account_external(account_id), frozenset())


async def _linkedin_creatives(session: AsyncSession, dataset_id: int) -> _AccountEntities:
    """Read a LinkedIn dataset's creatives in one query: by URN and per ad account.

    A creative belongs to the account of its campaign (both keys are ``NOT NULL``).
    """
    result = await session.execute(
        select(Creative.urn, Creative.id, AdAccount.external_id)
        .join(Campaign, Campaign.id == Creative.campaign_id)
        .join(AdAccount, AdAccount.id == Campaign.account_id)
        .where(Creative.dataset_id == dataset_id)
    )
    return _AccountEntities.from_rows(result)


async def _linkedin_campaigns(session: AsyncSession, dataset_id: int) -> _AccountEntities:
    """Read a LinkedIn dataset's campaigns in one query: by external id and per ad account."""
    result = await session.execute(
        select(Campaign.external_id, Campaign.id, AdAccount.external_id)
        .join(AdAccount, AdAccount.id == Campaign.account_id)
        .where(Campaign.dataset_id == dataset_id)
    )
    return _AccountEntities.from_rows(result)


# ---------------------------------------------------------------------------------------------
# Segments (AAP 0.3.2 "LinkedIn demographic segments"; AAP 0.5.4.4 C3a)
# ---------------------------------------------------------------------------------------------


def _account_external(account_id: str) -> str:
    """The numeric external id of a configured LinkedIn ad account (id or URN)."""
    return urn_id(sponsored_account_urn(account_id), PREFIX_ACCOUNT)


class SegmentSyncJob(_JobBase):
    """LinkedIn demographic aggregates for every exact preset window (``linkedin.segments``).

    For each ad account, each pivot of ``SEGMENT_PIVOTS`` (job function, seniority,
    industry) and each preset ``n`` of ``window.presets``, the window
    ``[end - n + 1, end]`` is read with ``q=statistics`` and ``timeGranularity=ALL``, where
    ``end`` is the default window end (:func:`default_window_end`), so segment windows always
    equal the default run's window, first start included. LinkedIn approximates and
    privacy-thresholds these values, so a window is stored as returned and never summed from
    daily rows; a metric the pivot does not return stays ``NULL`` (unavailable).

    Freshness covers ``[end - max(presets) + 1, end]``; a complete fetch records
    :func:`segment_reason`, which :func:`segments_due` reads back to see whether the windows
    or presets moved on. An incomplete fetch records ``"<windows>; <shortfall>"``.

    Each (account, pivot, window) listing is its own scope: the dataset's rows of that
    account's campaigns, the pivot's facet and exactly that ``window_start`` and
    ``window_end``. When a listing is :func:`_proven_complete`, the rows of its scope whose
    (campaign, value) it no longer returned, such as a value that fell out of the top 100 or
    below the privacy threshold, are deleted in the transaction that upserts it
    (:func:`_remove_absent`), all of them after an empty complete answer, so C3a never reads
    a withdrawn segment as current evidence. An incomplete or interrupted listing only
    upserts. Rows of other windows (earlier end dates included), facets, accounts and
    datasets are never touched. The campaigns are read once per job.

    Every listing is read first, with no transaction open; one transaction then writes them
    all and records ``linkedin.segments``. A transient interruption stops the reads, and that
    transaction records the failed attempt with the listings read before it (and the rerun
    they need) instead of a success.
    """

    job = JOB_SEGMENTS

    def __init__(self, ctx: JobContext) -> None:
        """Bind the job to a LinkedIn dataset.

        Raises:
            ValueError: If the context is not a LinkedIn dataset's.
        """
        if ctx.source != "linkedin":
            raise ValueError("demographic segments are LinkedIn's")
        super().__init__(ctx)

    def freshness_datasets(self) -> tuple[str, ...]:
        """``linkedin.segments``."""
        return (DATASET_LINKEDIN_SEGMENTS,)

    async def _execute(self) -> None:
        """Fetch every (account, pivot, preset) window and store it.

        The dataset's campaigns are read once for the job (:func:`_linkedin_campaigns`), not
        once per batch: the job stores no campaign, so the map stays exact for its whole run.
        """
        ctx = self.ctx
        name = DATASET_LINKEDIN_SEGMENTS
        client = ctx.clients.require_linkedin()
        presets = ctx.thresholds.presets
        async with ctx.sessionmaker() as session:
            end = await default_window_end(session, now=ctx.now, thresholds=ctx.thresholds)
            campaigns = await _linkedin_campaigns(session, ctx.dataset_id)
        windows = [(end - timedelta(days=days - 1), end) for days in presets]
        reasons: list[str | None] = []
        failure: TransientError | None = None
        listings: list[tuple[Listing[SegmentRow], tuple[date, date], str, frozenset[int]]] = []
        for account_id in ctx.clients.linkedin_account_ids:
            scope = campaigns.of(account_id)
            for pivot in SEGMENT_PIVOTS:
                for window in windows:
                    listing = await client.analytics_segments(account_id, pivot, window)
                    self._calls += listing.calls
                    listings.append((listing, window, SEGMENT_FACETS[pivot], scope))
                    if listing.interrupted_by is not None:
                        failure = failure or listing.interrupted_by
                    if not listing.complete:
                        reasons.append(listing.reason or "segment listing incomplete")
                    if failure is not None:
                        break
                if failure is not None:
                    break
            if failure is not None:
                break
        async with ctx.sessionmaker() as session, session.begin():
            written = _Written()
            for listing, window, facet, scope in listings:
                written += await self._store(
                    session, listing, window, facet, campaigns.by_key, scope
                )
            if failure is not None:
                outcome = FreshnessOutcome.failure(ctx.now, reason=failure.reason)
            else:
                if written.skipped:
                    noun = _plural(written.skipped, "row", "rows")
                    reasons.append(f"{noun} for unknown campaigns")
                shortfall = _join_reasons(reasons)
                recorded = segment_reason(presets, end)
                outcome = FreshnessOutcome.success(
                    ctx.now,
                    covered_from=end - timedelta(days=max(presets) - 1),
                    covered_through=end,
                    complete=shortfall is None,
                    reason=(
                        recorded
                        if shortfall is None
                        else f"{recorded}{_REASON_SEPARATOR}{shortfall}"
                    ),
                )
            await self._complete(
                session, {name: outcome}, changed=written.changed, rows={name: written.rows}
            )
        if failure is not None:
            self._interrupted(failure, dataset=name)

    async def _store(
        self,
        session: AsyncSession,
        listing: Listing[SegmentRow],
        window: tuple[date, date],
        facet: str,
        campaigns: Mapping[str, int],
        scope: frozenset[int],
    ) -> _Written:
        """Store one (account, pivot, window) listing in the dataset's transaction.

        The rows are upserted in ``session``'s transaction, the one that records
        ``linkedin.segments``; nothing is committed here. When the listing is
        :func:`_proven_complete`, the rows of the account's campaigns (``scope``) for
        ``facet`` and exactly ``window`` whose (campaign, value) it did not return are
        deleted in the same transaction (:func:`_remove_absent`), also when it returned no
        row at all.

        Args:
            session: The transaction that records the dataset.
            listing: The listing of one account, pivot and window.
            window: The exact ``(window_start, window_end)`` the listing read.
            facet: The pivot's facet (``SEGMENT_FACETS[pivot]``).
            campaigns: The dataset's campaign ids by external id, read once per job.
            scope: The ids of the account's campaigns, read once per job.

        Returns:
            The facts upserted, the rows removed and the rows whose campaign the dataset
            does not store.
        """
        ctx = self.ctx
        rows = listing.rows
        reconcile = _proven_complete(listing) and bool(scope)
        if not rows and not reconcile:
            return _Written()
        start, end = window
        facts: list[dict[str, Any]] = []
        for row in rows:
            external = _urn_tail(row.campaign_urn, PREFIX_CAMPAIGN)
            campaign_id = None if external is None else campaigns.get(external)
            if campaign_id is None:
                continue
            facts.append(
                {
                    "campaign_id": campaign_id,
                    "facet": row.facet,
                    "value": row.value,
                    "window_start": start,
                    "window_end": end,
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "cost": row.cost,
                    "leads": row.leads,
                }
            )
        written = await repositories.upsert_facts(
            FactSegmentWindow, facts, ctx.dataset_id, session=session
        )
        removed = 0
        if reconcile:
            removed = await _remove_absent(
                session,
                FactSegmentWindow,
                ctx.dataset_id,
                scope=(
                    _any_id(FactSegmentWindow.campaign_id, scope),
                    FactSegmentWindow.facet == facet,
                    FactSegmentWindow.window_start == start,
                    FactSegmentWindow.window_end == end,
                ),
                key=(FactSegmentWindow.campaign_id, FactSegmentWindow.value),
                returned={
                    (fact["campaign_id"], fact["value"]) for fact in facts if fact["facet"] == facet
                },
                guarded=bool(facts),
                event="sync.segments.rows_removed",
                fields={
                    "source": "linkedin",
                    "facet": facet,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                },
            )
        unknown = len(rows) - len(facts)
        if unknown:
            _log.warning(
                "sync.segments.rows_skipped",
                source="linkedin",
                rows=unknown,
                reason="campaign not stored in the dataset",
            )
        return _Written(rows=written, removed=removed, skipped=unknown)


# ---------------------------------------------------------------------------------------------
# Leads (AAP 0.3.2 "LinkedIn lead responses"; AAP 0.3.3 "Form identity", "Lead-to-contact
# matching")
# ---------------------------------------------------------------------------------------------


class LeadSyncJob(_JobBase):
    """LinkedIn lead responses since the last success minus 1 h (``linkedin.leads``).

    The cursor is the ``last_success_at`` of the dataset's ``linkedin.leads`` row: the range
    is ``[last_success_at - LEAD_OVERLAP, now)``, or the backfill range (``BACKFILL_DAYS``,
    capped at 180 days) when there is none or ``backfill`` is set. Responses are
    de-duplicated on their id, so the overlap and any re-read are harmless. Test leads were
    dropped by the connector and are never stored; emails reach this job only as SHA-256
    hashes (AAP 0.3.2 "Personal data").

    One transaction stores the form-version maps learned from the responses' decorations
    (keyed by the version parsed from each response's ``versionedLeadGenFormUrn``), the
    leads (campaign, creative, canonical form URN and version, email hash) and their
    ``lead_match`` staging: one row per lead in its effective CRM dataset (the companion
    while LinkedIn is simulated, HubSpot's active primary otherwise) and, for a live lead,
    in every ``building`` HubSpot primary (staged matching, AAP 0.3.3). A lead whose email
    could not be decoded is staged ``no_email`` with its reason (terminal).

    A lead is stored under its campaign or, when no entity sync stored the campaign yet,
    under a stand-in created beneath its ad account. A lead whose ad account is not stored
    either (an account whose entity sync has not succeeded yet) cannot be placed, as no
    account is invented, and it fails the attempt: ``linkedin.leads`` records a failed
    attempt naming the count, which keeps ``last_success_at`` (the cursor), its coverage
    and its completeness, so every later run re-reads the range until the account is
    stored and the lead with it. The attempt's placed leads are stored and staged all the
    same. A transient interruption fails the attempt the same way, keeping the cursor: the
    leads read before it are stored and staged in the transaction that records the failed
    attempt, and the error then goes to ``on_error``. Either failed attempt queues the
    ``analysis_request`` the leads it stored or staged need.
    """

    job = JOB_LEADS

    def __init__(self, ctx: JobContext, *, backfill: bool = False) -> None:
        """Bind the job to a LinkedIn dataset.

        Args:
            ctx: The LinkedIn dataset's context.
            backfill: Read the whole backfill range instead of the incremental one.

        Raises:
            ValueError: If the context is not a LinkedIn dataset's.
        """
        if ctx.source != "linkedin":
            raise ValueError("lead responses are LinkedIn's")
        super().__init__(ctx)
        self.backfill = backfill

    def freshness_datasets(self) -> tuple[str, ...]:
        """``linkedin.leads``."""
        return (DATASET_LINKEDIN_LEADS,)

    async def _execute(self) -> None:
        """List, store and stage the range's leads (class docstring)."""
        ctx = self.ctx
        name = DATASET_LINKEDIN_LEADS
        client = ctx.clients.require_linkedin()
        async with ctx.sessionmaker() as session:
            row = (await freshness.load_freshness(session, ctx.dataset_id)).get(name)
            known = await lead_match.known_versions(session, ctx.dataset_id)
        earliest = ctx.now - timedelta(days=_span_days(ctx.settings, "linkedin"))
        if self.backfill or row is None or row.last_success_at is None:
            start = earliest
        else:
            start = max(row.last_success_at - LEAD_OVERLAP, earliest)
        reads: list[tuple[str, Sequence[ParsedLead]]] = []
        versions: list[FormVersionMap] = []
        reasons: list[str | None] = []
        failure: TransientError | None = None
        skipped_tests = 0
        for account_id in ctx.clients.linkedin_account_ids:
            listing = await client.list_lead_responses(
                account_id, (start, ctx.now), known_versions=known
            )
            self._calls += listing.calls
            skipped_tests += listing.test_leads_skipped
            reads.append((_account_external(account_id), listing.leads))
            versions.extend(listing.versions)
            versions.extend(lead.version_map for lead in listing.leads if lead.version_map)
            if listing.interrupted_by is not None and failure is None:
                failure = listing.interrupted_by
            if not listing.complete:
                reasons.append(listing.reason or "lead listing incomplete")
            if failure is not None:
                break

        covered = (start.astimezone(UTC).date(), ctx.now.astimezone(UTC).date())
        async with ctx.sessionmaker() as session, session.begin():
            await repositories.ensure_writable(session, ctx.dataset_id)
            await _store_form_versions(session, ctx.dataset_id, versions, now=ctx.now)
            stored, staged, unplaced = await self._store_leads(session, reads)
            if failure is not None:
                # The leads read are stored and staged, but the listing did not finish: a
                # failed attempt keeps the cursor, and on_error then decides the rest.
                outcome = FreshnessOutcome.failure(ctx.now, reason=failure.reason)
            elif unplaced:
                # A success would move the cursor past the leads left unstored; a failed
                # attempt keeps it, so the next run re-reads them (class docstring).
                reason = (
                    f"{_plural(unplaced, 'lead', 'leads')} not stored: campaign and ad "
                    "account not stored yet"
                )
                outcome = FreshnessOutcome.failure(ctx.now, reason=reason)
                self._failed[name] = reason
            else:
                outcome = _success(ctx.now, covered=covered, reasons=reasons)
            await self._complete(session, {name: outcome}, changed=bool(stored or staged))
        _log.info(
            "sync.leads.stored",
            dataset_id=ctx.dataset_id,
            leads=self._rows.get(name, 0),
            staged=staged,
            unplaced=unplaced,
            test_leads_skipped=skipped_tests,
            form_versions=len({(item.canonical_urn, item.version) for item in versions}),
        )
        if failure is not None:
            self._interrupted(failure, dataset=name)

    async def _store_leads(
        self, session: AsyncSession, reads: Sequence[tuple[str, Sequence[ParsedLead]]]
    ) -> tuple[int, int, int]:
        """Upsert the leads (stubbing unknown campaigns and creatives) and stage them.

        A lead whose campaign is not stored and whose ad account is not stored either (an
        account whose entity sync has not succeeded yet) cannot be placed: a campaign stub
        needs its account, and an account is never invented, as its currency is unknown.
        Such a lead is left unstored, and counted, for the caller to fail the attempt.

        Returns:
            The leads stored, the ``lead_match`` rows inserted across the CRM datasets staged
            in, and the number of leads left unstored for want of a campaign.
        """
        ctx = self.ctx
        dataset_id = ctx.dataset_id
        accounts = _by_last_key(await repositories.entity_id_map(session, AdAccount, dataset_id))
        campaigns = await _campaign_ids(session, dataset_id)
        leads: dict[str, tuple[ParsedLead, str]] = {}
        for account_external, account_leads in reads:
            for lead in account_leads:
                leads.setdefault(lead.external_id, (lead, account_external))

        # A lead can reference a campaign or creative no entity sync stored yet (it ran
        # before the campaign was listed). A stand-in keeps the lead and its attribution;
        # the next entity sync fills the rest in.
        unknown_by_account: dict[int, set[str]] = {}
        for lead, account_external in leads.values():
            external = _urn_tail(lead.campaign_urn, PREFIX_CAMPAIGN)
            owner = _urn_tail(lead.account_urn, PREFIX_ACCOUNT) or account_external
            account_id = accounts.get(owner)
            if external is not None and external not in campaigns and account_id is not None:
                unknown_by_account.setdefault(account_id, set()).add(external)
        for account_id, externals in sorted(unknown_by_account.items()):
            await _stub_campaigns(session, dataset_id, account_id, externals, campaigns)
        creatives = _by_last_key(await repositories.entity_id_map(session, Creative, dataset_id))
        stub_creatives: dict[str, int] = {}
        for lead, _ in leads.values():
            external = _urn_tail(lead.campaign_urn, PREFIX_CAMPAIGN)
            if (
                lead.creative_urn is not None
                and lead.creative_urn not in creatives
                and external in campaigns
            ):
                stub_creatives[lead.creative_urn] = campaigns[external]
        if stub_creatives:
            await _insert_missing(
                session,
                Creative,
                Creative.__natural_key__,
                [
                    {"urn": urn, "campaign_id": campaign_id}
                    for urn, campaign_id in sorted(stub_creatives.items())
                ],
                dataset_id,
            )
            creatives = _by_last_key(
                await repositories.entity_id_map(session, Creative, dataset_id)
            )

        values: list[dict[str, Any]] = []
        no_email: dict[str, str] = {}
        unplaced: list[tuple[ParsedLead, str]] = []
        for lead, account_external in leads.values():
            external = _urn_tail(lead.campaign_urn, PREFIX_CAMPAIGN)
            campaign_id = None if external is None else campaigns.get(external)
            if campaign_id is None:
                unplaced.append((lead, account_external))
                continue
            values.append(
                {
                    "external_id": lead.external_id,
                    "submitted_at": lead.submitted_at,
                    "campaign_id": campaign_id,
                    "creative_id": (
                        None if lead.creative_urn is None else creatives.get(lead.creative_urn)
                    ),
                    "lead_form_urn": lead.lead_form_urn,
                    "lead_form_version": lead.lead_form_version,
                    "email_sha256": lead.email_sha256,
                }
            )
            if lead.email_status == "no_email":
                no_email[lead.external_id] = lead.email_reason or "no email"
        if unplaced:
            _log.warning(
                "sync.leads.unplaced",
                dataset_id=dataset_id,
                leads=len(unplaced),
                accounts=sorted(
                    {
                        _urn_tail(lead.account_urn, PREFIX_ACCOUNT) or account_external
                        for lead, account_external in unplaced
                    }
                ),
                earliest_submitted_at=_iso(min(lead.submitted_at for lead, _ in unplaced)),
                reason="campaign and ad account not stored",
            )
        stored = _by_last_key(await repositories.upsert_entities(session, Lead, values, dataset_id))
        self._count(DATASET_LINKEDIN_LEADS, len(stored))
        lead_ids = sorted(stored.values())
        reasons = {stored[external]: reason for external, reason in no_email.items()}
        staged = 0
        for crm_dataset_id in await self._staging_targets(session):
            staged += await lead_match.create_pending(
                session, lead_ids, crm_dataset_id, now=ctx.now, no_email_reasons=reasons
            )
        return len(stored), staged, len(unplaced)

    async def _staging_targets(self, session: AsyncSession) -> list[int]:
        """The CRM datasets new leads are staged in (module docstring of ``lead_match``)."""
        ctx = self.ctx
        targets: list[int] = []
        effective = await repositories.effective_crm_dataset_id(session, ctx.dataset_id)
        if effective is not None:
            targets.append(effective)
        else:
            _log.warning(
                "sync.leads.no_crm",
                dataset_id=ctx.dataset_id,
                reason="no effective CRM dataset to stage lead matches in yet",
            )
        if ctx.mode == MODE_LIVE:
            building = await session.scalars(
                select(SourceDataset.id)
                .where(
                    SourceDataset.source == SOURCE_HUBSPOT,
                    SourceDataset.role == DATASET_ROLE_PRIMARY,
                    SourceDataset.state == DATASET_STATE_BUILDING,
                )
                .order_by(SourceDataset.id)
            )
            targets.extend(dataset_id for dataset_id in building if dataset_id not in targets)
        return targets


async def _due_matches(session: AsyncSession, crm_dataset_id: int, now: datetime) -> bool:
    """Whether any ``lead_match`` row of a CRM dataset could be claimed now."""
    due = await session.scalar(
        select(
            exists().where(
                LeadMatch.crm_dataset_id == crm_dataset_id,
                (LeadMatch.status == LEAD_MATCH_PENDING)
                | (
                    LeadMatch.status.in_((LEAD_MATCH_NO_MATCH, LEAD_MATCH_ERROR))
                    & LeadMatch.next_attempt_at.is_not(None)
                    & (LeadMatch.next_attempt_at <= now)
                ),
            )
        )
    )
    return bool(due)


async def _crm_bindings(ctx: JobContext) -> CrmBindings:
    """The CRM dataset's stored bindings, verified against its portal when none are stored.

    Raises:
        ConnectorError: When the verification call fails.
    """
    bindings = await ctx.controller.datasets.load_crm_bindings(ctx.dataset_id)
    if bindings is None:
        bindings = await ctx.controller.verify_crm_bindings(ctx.dataset_id)
    return bindings


async def _match_leads(
    job: _JobBase,
    *,
    lead_dataset_id: int | None,
    lead_client: LinkedInClient | None,
    props: ContactPropertyMap,
    interruptions: list[TransientError],
) -> lead_match.MatchReport | None:
    """Attempt the due ``lead_match`` rows of the job's CRM dataset.

    Leads are re-fetched through a LinkedIn client of their own mode: simulated for a
    companion, whose rows are its parent's simulated leads, live for a primary, which only
    ever holds live leads (``sync/lead_match.py`` isolation rule). A live re-fetch needs
    LinkedIn credentials under ``DATA_MODE=auto``; without them no live lead can exist, and
    the pass is skipped.

    ``interruptions`` receives the pass's first exhausted ``TransientError`` per source as
    soon as it is observed (``run_matching``), so the caller still has them when the pass
    raises.

    Returns:
        The matching report, or ``None`` when the pass was skipped.

    Raises:
        AccessDenied, QueryDefect: From LinkedIn or HubSpot, for the caller to hand over.
    """
    ctx = job.ctx
    hubspot = ctx.clients.require_hubspot()
    async with ctx.sessionmaker() as session:
        due = await _due_matches(session, ctx.dataset_id, ctx.now)
    if not due:
        return lead_match.MatchReport()
    lead_mode: Mode = "simulated" if ctx.role == "companion" else "live"

    async def run(client: LinkedInClient) -> lead_match.MatchReport:
        clients = lead_match.MatchClients(
            # Every lead of one CRM dataset is of one mode, so one client re-fetches them all.
            linkedin_for=lambda _lead_dataset_id: client,
            hubspot=hubspot,
            contact_properties=props,
        )
        return await lead_match.run_matching(
            ctx.sessionmaker,
            ctx.dataset_id,
            clients,
            now=ctx.now,
            lead_dataset_id=lead_dataset_id,
            interruptions=interruptions,
        )

    if lead_client is not None:
        return await run(lead_client)
    if lead_mode == "live" and not (
        ctx.settings.data_mode == "auto" and ctx.settings.has_credentials("linkedin")
    ):
        _log.warning(
            "sync.lead_match.skipped",
            crm_dataset_id=ctx.dataset_id,
            reason="live leads are due but LinkedIn live access is not configured",
        )
        return None
    async with ctx.controller.open_clients("linkedin", lead_mode) as lead_clients:
        return await run(lead_clients.require_linkedin())


def _matching_interruptions(
    collected: Sequence[TransientError], report: lead_match.MatchReport | None
) -> list[TransientError]:
    """The first exhausted ``TransientError`` per source a matching pass met, in order.

    ``collected`` is what the pass handed over as it went (``run_matching``'s
    ``interruptions``), complete also when the pass raised; ``report.interrupted_by``
    repeats it on a normal return, so each source is listed once.
    """
    first: dict[str, TransientError] = {}
    for error in (*collected, *(() if report is None else report.interrupted_by)):
        first.setdefault(error.source, error)
    return list(first.values())


class LeadMatchJob(_JobBase):
    """One matching pass over a CRM dataset's due ``lead_match`` rows.

    The context is the **CRM dataset's** (HubSpot, primary or companion): ``ctx.dataset_id``
    is the CRM dataset matched in, and ``ctx.clients`` its portal's client. Used on its own
    by a live LinkedIn backfill, so every row staged against HubSpot's active primary is
    attempted before the LinkedIn dataset activates; :class:`CrmSyncJob` runs the same pass
    as its matching step. ``AccessDenied`` from LinkedIn is handed to
    ``on_error("linkedin", …)``, from HubSpot to ``on_error("hubspot", …)``; so is a transient
    re-fetch or lookup failure the pass met (``MatchReport.interrupted_by``), with no
    freshness dataset, as its rows were already left ``error`` and due for a retry. One met
    before a later batch raised is handed over too, ahead of the raised error. A pass that
    attempted rows in an active context queues one ``analysis_request``; the backfill's
    inactive context queues none.
    """

    job = JOB_LEAD_MATCH

    def __init__(
        self,
        ctx: JobContext,
        *,
        lead_dataset_id: int | None = None,
        lead_client: LinkedInClient | None = None,
    ) -> None:
        """Bind the pass.

        Args:
            ctx: The CRM dataset's context.
            lead_dataset_id: Restrict the pass to one LinkedIn dataset's leads.
            lead_client: A LinkedIn client to re-fetch the leads with (the building dataset's
                own); one of the leads' mode is opened when omitted.

        Raises:
            ValueError: If the context is not a HubSpot dataset's.
        """
        if ctx.source != "hubspot":
            raise ValueError("lead matching runs in a HubSpot (CRM) dataset")
        super().__init__(ctx)
        self.lead_dataset_id = lead_dataset_id
        self.lead_client = lead_client

    def freshness_datasets(self) -> tuple[str, ...]:
        """None: matching state lives in ``lead_match``, which readiness counts."""
        return ()

    async def _execute(self) -> None:
        """Run the pass, count the rows attempted, and queue the request their changes need.

        Matching commits per batch, so a pass that attempted rows of an active CRM dataset
        queues one ``analysis_request`` afterwards (AAP 0.5.1 "analysis requests after each
        job"); an inactive context (a backfill's) or a pass with nothing due queues none.

        Raises:
            repositories.DatasetNotWritable: If the dataset was retired meanwhile.
        """
        hubspot = self.ctx.clients.require_hubspot()
        calls_before = hubspot.calls
        bindings = await _crm_bindings(self.ctx)
        props = hubspot.bind_contact_properties(bindings, self.ctx.settings)
        interruptions: list[TransientError] = []
        report: lead_match.MatchReport | None = None
        try:
            report = await _match_leads(
                self,
                lead_dataset_id=self.lead_dataset_id,
                lead_client=self.lead_client,
                props=props,
                interruptions=interruptions,
            )
        finally:
            self._calls += hubspot.calls - calls_before
            for error in _matching_interruptions(interruptions, report):
                # The rows already ended retried ``error``; the source's mode decides the
                # rest. Noted on every exit: when a later batch raised, ``run`` hands these
                # over first, so the raised error's consequence is the one that stands.
                self._interrupted(error, dataset=None)
        if report is not None:
            self._count(None, report.attempted)
            _log.info(
                "sync.lead_match.pass",
                crm_dataset_id=self.ctx.dataset_id,
                attempted=report.attempted,
                matched=report.matched,
                pending_left=report.pending_left,
            )
            if report.attempted and self.ctx.active:
                async with self.ctx.sessionmaker() as session, session.begin():
                    dataset = await repositories.ensure_writable(session, self.ctx.dataset_id)
                    await self._queue_analysis(session, dataset)


# ---------------------------------------------------------------------------------------------
# CRM (AAP 0.3.3 "Contact refresh", "Associations", "Deal search", "Google-to-CRM link")
# ---------------------------------------------------------------------------------------------


def _search_reason(outcome: SearchOutcome) -> str | None:
    """Map a completed partitioned search's shortfall onto the freshness reason vocabulary.

    Only a search that read every partition reaches this: one a transient failure stopped
    (``outcome.interrupted_by``) records no success at all, its error going to ``on_error``.
    A one-hour partition above the 10,000-result ceiling is permanent (prefixed
    :data:`~ads_engine.sync.freshness.REASON_SEARCH_CEILING`); a short read keeps the search
    module's text (:data:`SHORT_READ_REASON`), as does any other reason part.
    """
    if outcome.is_complete:
        return None
    parts: list[str] = []
    for part in (outcome.reason or "search incomplete").split(_REASON_SEPARATOR):
        if part == CEILING_REASON:
            parts.append(f"{freshness.REASON_SEARCH_CEILING}: {part}")
        else:
            parts.append(part)
    if outcome.partitions_incomplete:
        noun = _plural(len(outcome.partitions_incomplete), "partition", "partitions")
        parts.append(f"{noun} not read in full")
    return _REASON_SEPARATOR.join(parts)


def _widened(start: datetime, row: DatasetFreshness | None) -> datetime:
    """Widen a search's start to a stored row's coverage when the row needs repair."""
    if freshness.needs_repair(row) and row is not None and row.covered_from is not None:
        return min(start, _utc_midnight(row.covered_from))
    return start


def _contact_values(row: ContactRow) -> dict[str, Any]:
    """The ``crm_contact`` row of one parsed contact (email as its SHA-256 only)."""
    return {
        "hs_id": row.hs_id,
        "email_sha256": row.email_sha256,
        "lifecycle_stage": row.lifecycle_stage,
        "mql_at": row.mql_at,
        "ql_at": row.ql_at,
        "google_campaign_ref": row.google_campaign_ref,
        "google_touch_at": row.google_touch_at,
        "updated_at": row.updated_at,
    }


def _discovery_shortfall(row: DatasetFreshness | None) -> bool:
    """Whether a stored ``hubspot.google_contacts`` row is incomplete for a discovery shortfall.

    That is a search short of the ceiling
    (:data:`~ads_engine.sync.freshness.REASON_SEARCH_CEILING`) or of its reported total
    (:data:`SHORT_READ_REASON`), which the next discovery re-reads only over its own range, or
    a repairable shortfall, whose range :func:`_widened` already re-reads. A shortfall of the
    refresh by id alone is not one: every run re-reads every stored contact.
    """
    if row is None or row.completeness != COMPLETENESS_INCOMPLETE or row.reason is None:
        return False
    reason = row.reason
    return (
        freshness.REASON_SEARCH_CEILING in reason
        or SHORT_READ_REASON in reason
        or freshness.is_repairable_reason(reason)
    )


@dataclass(frozen=True, slots=True)
class _Discovery:
    """What one Google-touch discovery search read, for the refresh that stores and records it.

    Attributes:
        covered: The UTC dates the search covered, from its (widened) start to today.
        reason: The completed search's shortfall in the freshness vocabulary
            (:func:`_search_reason`), or ``None``.
        contacts: The contacts the search returned, parsed and not stored yet: the contact
            refresh's transaction stores them (:meth:`CrmSyncJob._refresh_contacts`).
        interrupted_by: The transient error that stopped the search, or ``None``; the dataset
            then records a failed attempt this run.
    """

    covered: tuple[date, date]
    reason: str | None
    contacts: tuple[ContactRow, ...]
    interrupted_by: TransientError | None

    @property
    def found(self) -> frozenset[str]:
        """HubSpot ids of the contacts the search returned; their properties were read."""
        return frozenset(row.hs_id for row in self.contacts)

    @property
    def interrupted(self) -> bool:
        """Whether a transient failure stopped the search."""
        return self.interrupted_by is not None


class CrmSyncJob(_JobBase):
    """The CRM sync of one HubSpot dataset, primary or companion, every 3 h.

    The context is the CRM dataset's: ``ctx.dataset_id`` is the HubSpot primary or a
    simulated LinkedIn dataset's companion. A companion is populated by exactly this job
    against the simulated HubSpot transport, so it writes its own freshness rows the same
    way. In order:

    1. **Google-touch discovery** (primary only, and only when the stored bindings verify
       both ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY``
       and the settings still name them): contacts whose touch date falls in the sync range
       and whose campaign property is set are read; step 3's transaction stores them with the
       dataset's outcome. Without the configuration nothing is called and no row is written;
       Google items then carry ``no_crm_link`` and no LinkedIn-derived subset stands in.
    2. **Matching**: the due ``lead_match`` rows (:class:`LeadMatchJob`'s pass).
    3. **Contact refresh** by id of every matched contact and, when discovery runs, every
       stored Google-touch contact whatever its touch date (a Google-only contact touched
       before the current range is no longer rediscovered, so only this read keeps its stage
       dates current). It reads the email (hashed), the lifecycle stage, the bound MQL and QL
       date properties and the Google properties; ``lead_match.contact_read_status`` records
       ``ok`` or ``error`` per matched contact, and a failed read keeps the previous stage
       values (``hubspot.contacts``, counting every contact not read). The same transaction
       stores the contacts discovery read, before the newer reads by id, and records
       ``hubspot.google_contacts``: the discovery's outcome, ``incomplete`` while any
       Google-touch contact was not refreshed (:meth:`_google_outcome`).
    4. **Deal search**: every deal created from the default window start (or the backfill
       start) to now, partitioned below the 10,000-result ceiling (``hubspot.deal_search``).
    5. **Associations**: contacts to meetings and deals for the contacts step 3 refreshes,
       deals to contacts for every searched deal, each input followed through its own cursor,
       then **target reads** of the associated meetings, deals and contacts. An associated
       but unread target is never treated as absent: the fetch's ``target_read_status`` and
       ``missing_target_ids`` say so (``hubspot.associations``).

    Transient failures (AAP 0.3.3 transient row): a search, contact read, association read or
    target read whose call still fails after its retries returns what it read with the error
    in ``interrupted_by``. The step's transaction stores what was read with its per-object
    unknown state (``contact_read_status`` ``error``, fetch rows ``error``/``partial``, missing
    targets) together with its dataset's failed attempt, and queues the rerun when that
    changed data: ``last_success_at`` and the coverage stand and the dataset turns stale. The
    error is then handed to ``on_error``, which turns the connection ``stale``. An interrupted
    discovery fails ``hubspot.google_contacts`` in step 3's transaction, which stores the
    contacts it read. A matching interruption is handed over with no dataset. A one-hour
    partition above the search ceiling, or a short read, is a completed read and stays an
    ``incomplete`` success.

    The HubSpot client throttles itself to 4 requests per second; nothing here sleeps.
    """

    def __init__(self, ctx: JobContext, *, backfill: bool = False) -> None:
        """Bind the job to a CRM dataset.

        Args:
            ctx: The CRM dataset's context (primary or companion).
            backfill: Search deals and Google-touch contacts over the whole backfill range.

        Raises:
            ValueError: If the context is not a HubSpot dataset's.
        """
        if ctx.source != "hubspot":
            raise ValueError("the CRM sync runs in a HubSpot dataset")
        super().__init__(ctx)
        self.backfill = backfill
        self.job = JOB_CRM_COMPANION if ctx.role == "companion" else JOB_CRM
        self._discovery = False

    def freshness_datasets(self) -> tuple[str, ...]:
        """Contacts, associations and the deal search; Google contacts when discovery runs."""
        names = (
            DATASET_HUBSPOT_CONTACTS,
            DATASET_HUBSPOT_ASSOCIATIONS,
            DATASET_HUBSPOT_DEAL_SEARCH,
        )
        return (*names, DATASET_HUBSPOT_GOOGLE_CONTACTS) if self._discovery else names

    async def _execute(self) -> None:
        """Run the five steps of the class docstring."""
        ctx = self.ctx
        client = ctx.clients.require_hubspot()
        calls_before = client.calls
        try:
            bindings = await _crm_bindings(ctx)
            props = client.bind_contact_properties(bindings, ctx.settings)
            async with ctx.sessionmaker() as session:
                rows = await freshness.load_freshness(session, ctx.dataset_id)
                end = await default_window_end(session, now=ctx.now, thresholds=ctx.thresholds)
            scope_start = ctx.now - timedelta(days=_crm_span_days(ctx.settings))
            if self.backfill:
                search_start = scope_start
            else:
                search_start = _utc_midnight(end - timedelta(days=ctx.thresholds.default_days - 1))
            search_start = min(search_start, ctx.now)

            discovery: _Discovery | None = None
            campaign_property = props.google_campaign_property
            touch_property = props.google_touch_property
            if (
                ctx.role == "primary"
                and campaign_property is not None
                and touch_property is not None
            ):
                self._discovery = True
                await self._open_runs((DATASET_HUBSPOT_GOOGLE_CONTACTS,))
                discovery = await self._discover(
                    client,
                    props,
                    (campaign_property, touch_property),
                    _widened(search_start, rows.get(DATASET_HUBSPOT_GOOGLE_CONTACTS)),
                )

            await self._match(props)
            refreshed = await self._refresh_contacts(client, props, rows, scope_start, discovery)
            deal_ids = await self._search_deals(
                client, _widened(search_start, rows.get(DATASET_HUBSPOT_DEAL_SEARCH))
            )
            await self._associations(
                client, props, rows, scope_start, refreshed, discovery, deal_ids
            )
        finally:
            self._calls += client.calls - calls_before

    # -- steps -----------------------------------------------------------------------------

    async def _discover(
        self,
        client: HubSpotClient,
        props: ContactPropertyMap,
        properties: tuple[str, str],
        start: datetime,
    ) -> _Discovery:
        """Step 1: read the Google-touch contacts of the sync range; nothing is written.

        ``properties`` are the verified campaign and touch-date properties. The contacts read
        are stored, and the outcome of ``hubspot.google_contacts`` recorded, in the refresh by
        id's transaction (step 3), because the dataset is the stored contacts, which that
        refresh re-reads: a failure before it commits leaves the dataset as it was.
        """
        ctx = self.ctx
        name = DATASET_HUBSPOT_GOOGLE_CONTACTS
        campaign_property, touch_property = properties
        outcome = await client.search_partitioned(
            _CONTACTS,
            google_contacts_filter(campaign_property, touch_property, props.request_properties()),
            (start, ctx.now),
        )
        contacts = {row.hs_id: row for row in (parse_contact(r, props) for r in outcome.rows)}
        failure = outcome.interrupted_by
        if failure is not None:
            # Retries are exhausted: the contacts read are still stored by step 3, which
            # records the failed attempt with them; noted now, so the interruption is handed
            # over even when a later step raises first.
            self._interrupted(failure, dataset=name)
        return _Discovery(
            covered=(start.astimezone(UTC).date(), ctx.now.astimezone(UTC).date()),
            reason=None if failure is not None else _search_reason(outcome),
            contacts=tuple(contacts.values()),
            interrupted_by=failure,
        )

    async def _match(self, props: ContactPropertyMap) -> None:
        """Step 2: the matching pass; a LinkedIn error is LinkedIn's and the sync goes on.

        Every exhausted transient error the pass met reaches ``on_error`` with no dataset (its
        rows already ended retried ``error``), also when a later batch raised: ahead of that
        error, so the raised error's consequence is the one that stands.
        """
        interruptions: list[TransientError] = []
        report: lead_match.MatchReport | None = None
        handed_over = False
        try:
            report = await _match_leads(
                self,
                lead_dataset_id=None,
                lead_client=None,
                props=props,
                interruptions=interruptions,
            )
        except ConnectorError as exc:
            if exc.source != "linkedin":
                raise
            # Re-fetching leads failed on LinkedIn's side: LinkedIn's source mode decides
            # what follows; the CRM refresh of contacts already matched still runs. The error
            # is handed over now, so the pass's interruptions are handed over first, here.
            handed_over = True
            for error in _matching_interruptions(interruptions, None):
                self._disposition = await self._hand_over(error, ())
            self._disposition = await self._hand_over(exc, ())
            return
        finally:
            if not handed_over:
                for error in _matching_interruptions(interruptions, report):
                    # The rows already ended retried ``error``; the source's mode decides the
                    # rest (a LinkedIn re-fetch error is LinkedIn's). When a later batch
                    # raised, ``run`` hands these over first, then the raised error.
                    self._interrupted(error, dataset=None)
        if report is not None:
            _log.info(
                "sync.lead_match.pass",
                crm_dataset_id=self.ctx.dataset_id,
                attempted=report.attempted,
                matched=report.matched,
                pending_left=report.pending_left,
            )

    async def _matched_contacts(self, session: AsyncSession) -> list[str]:
        """HubSpot ids of every contact a live lead dataset's lead is matched to here."""
        ctx = self.ctx
        result = await session.scalars(
            select(CrmContact.hs_id)
            .join(
                LeadMatch,
                (LeadMatch.contact_id == CrmContact.id)
                & (LeadMatch.crm_dataset_id == CrmContact.dataset_id),
            )
            .join(Lead, Lead.id == LeadMatch.lead_id)
            .join(SourceDataset, SourceDataset.id == Lead.dataset_id)
            .where(
                LeadMatch.crm_dataset_id == ctx.dataset_id,
                LeadMatch.status == LEAD_MATCH_MATCHED,
                SourceDataset.state != DATASET_STATE_RETIRED,
            )
            .distinct()
        )
        return sorted(result)

    async def _google_contacts(self, session: AsyncSession) -> list[str]:
        """HubSpot ids of every stored Google-touch contact here, whatever its touch date.

        Selected on the touch date alone: a contact whose campaign property a refresh cleared
        is still re-read, so a campaign set again in HubSpot is seen even after its touch has
        left the discovery range.
        """
        result = await session.scalars(
            select(CrmContact.hs_id).where(
                CrmContact.dataset_id == self.ctx.dataset_id,
                CrmContact.google_touch_at.is_not(None),
            )
        )
        return sorted(result)

    async def _refresh_contacts(
        self,
        client: HubSpotClient,
        props: ContactPropertyMap,
        rows: Mapping[str, DatasetFreshness],
        scope_start: datetime,
        discovery: _Discovery | None,
    ) -> ContactBatch:
        """Step 3: refresh by id every matched and Google-touch contact; store and record them.

        The Google-touch contacts are the stored ones as this run's discovery leaves them:
        the stored ids, less those discovery read with no touch date, plus those it read with
        one. One transaction stores the contacts discovery read, then the newer reads by id,
        updates the matched leads' ``contact_read_status`` and records ``hubspot.contacts``
        and, when discovery ran, ``hubspot.google_contacts`` (:meth:`_google_outcome`). An
        interrupted refresh fails ``hubspot.contacts`` there, and ``hubspot.google_contacts``
        too when it cost a Google-touch read; an interrupted discovery fails
        ``hubspot.google_contacts`` with its own reason. A failed attempt that wrote data
        queues the rerun (:meth:`_complete`).
        """
        ctx = self.ctx
        name = DATASET_HUBSPOT_CONTACTS
        google_name = DATASET_HUBSPOT_GOOGLE_CONTACTS
        async with ctx.sessionmaker() as session:
            matched = await self._matched_contacts(session)
            google = await self._google_contacts(session) if discovery is not None else []
        discovered = discovery.contacts if discovery is not None else ()
        if discovered:
            # Storing the discovery (below) sets each contact's touch date as read.
            untouched = {row.hs_id for row in discovered if row.google_touch_at is None}
            touched = {row.hs_id for row in discovered if row.google_touch_at is not None}
            google = sorted((set(google) - untouched) | touched)
        ids = sorted({*matched, *google})
        batch = (
            await client.contacts_by_id(ids, props)
            if ids
            else ContactBatch(records=MappingProxyType({}), read_status={})
        )
        statuses = batch.read_status
        failed = sorted(hs_id for hs_id, status in statuses.items() if status != CONTACT_READ_OK)
        read = sorted(hs_id for hs_id, status in statuses.items() if status == CONTACT_READ_OK)
        google_unread = sum(1 for hs_id in google if statuses.get(hs_id) != CONTACT_READ_OK)
        interruption = batch.interrupted_by
        # A refresh interruption fails the Google dataset too when it cost a Google-touch
        # read; an interrupted discovery has failed it already.
        google_interrupted = interruption is not None and google_unread > 0
        discovery_interrupted = discovery is not None and discovery.interrupted
        refreshed = [hs_id for hs_id in read if hs_id in batch.records]
        # One statement, so the rows are locked in natural-key order; a read by id is newer
        # than discovery's read of the same contact and replaces it.
        values = {row.hs_id: _contact_values(row) for row in discovered}
        values.update((hs_id, _contact_values(batch.records[hs_id])) for hs_id in refreshed)
        async with ctx.sessionmaker() as session, session.begin():
            await repositories.ensure_writable(session, ctx.dataset_id)
            stored = await repositories.upsert_entities(
                session, CrmContact, list(values.values()), ctx.dataset_id
            )
            stored_ids = {str(key[-1]) for key in stored}
            updated = 0
            for status, hs_ids in ((CONTACT_READ_OK, read), (CONTACT_READ_ERROR, failed)):
                for chunk in _chunks(hs_ids):
                    result = await session.execute(
                        update(LeadMatch)
                        .where(
                            LeadMatch.crm_dataset_id == ctx.dataset_id,
                            LeadMatch.status == LEAD_MATCH_MATCHED,
                            LeadMatch.contact_id.in_(
                                select(CrmContact.id).where(
                                    CrmContact.dataset_id == ctx.dataset_id,
                                    CrmContact.hs_id.in_(chunk),
                                )
                            ),
                        )
                        .values(contact_read_status=status)
                        .returning(LeadMatch.lead_id)
                    )
                    updated += len(result.all())
            reason = f"{_plural(len(failed), 'contact', 'contacts')} not read" if failed else None
            outcomes: dict[str, FreshnessOutcome] = {}
            counts = {name: sum(1 for hs_id in refreshed if hs_id in stored_ids)}
            if interruption is None:
                outcomes[name] = _success(
                    ctx.now,
                    covered=self._state_range(rows.get(name), scope_start),
                    reasons=[reason],
                )
            else:
                outcomes[name] = FreshnessOutcome.failure(ctx.now, reason=interruption.reason)
            if discovery is not None:
                counts[google_name] = sum(1 for row in discovered if row.hs_id in stored_ids)
                if discovery.interrupted_by is not None:
                    outcomes[google_name] = FreshnessOutcome.failure(
                        ctx.now, reason=discovery.interrupted_by.reason
                    )
                elif google_interrupted and interruption is not None:
                    outcomes[google_name] = FreshnessOutcome.failure(
                        ctx.now, reason=interruption.reason
                    )
                else:
                    outcomes[google_name] = self._google_outcome(
                        rows.get(google_name), discovery, google_unread
                    )
            await self._complete(
                session, outcomes, changed=bool(stored) or updated > 0, rows=counts
            )
        if interruption is not None:
            # The contacts read are stored and every unread one keeps its previous stage
            # values marked ``error``, with the failed attempt; on_error decides the rest.
            also = (google_name,) if google_interrupted and not discovery_interrupted else ()
            self._interrupted(interruption, dataset=name, datasets=also)
        return batch

    def _google_outcome(
        self, row: DatasetFreshness | None, discovery: _Discovery, unread: int
    ) -> FreshnessOutcome:
        """The ``hubspot.google_contacts`` outcome once its stored contacts were refreshed.

        Args:
            row: The stored row, as the job found it before this run.
            discovery: This run's discovery, which was not interrupted.
            unread: Stored Google-touch contacts the refresh by id did not read.

        Returns:
            A success: the discovery's shortfall plus ``"N Google-touch contacts not
            refreshed"`` when any was not read (their stage dates and evidence are unknown),
            else complete.
        """
        # The dataset is the stored Google-touch contacts. Discovery adds the ones touched in
        # its range; the refresh by id re-read every stored one, whatever its touch date. The
        # run therefore vouches for the whole stored range, so the coverage reaches back to
        # the stored start and this run's completeness replaces the stored one (no sticky
        # shortfall, no widened search). A stored discovery shortfall (ceiling, short read)
        # lies in a range this discovery may not re-read: the coverage is then the discovery
        # range alone, so the merge keeps the worse of the two. A repairable one was widened
        # over its whole range already, so both rules agree there.
        start, today = discovery.covered
        if row is not None and row.covered_from is not None and not _discovery_shortfall(row):
            start = min(start, row.covered_from)
        reasons = [discovery.reason]
        if unread:
            noun = _plural(unread, "Google-touch contact", "Google-touch contacts")
            reasons.append(f"{noun} not refreshed")
        return _success(self.ctx.now, covered=(start, max(start, today)), reasons=reasons)

    def _state_range(
        self, row: DatasetFreshness | None, scope_start: datetime
    ) -> tuple[date, date]:
        """The coverage of a current-state CRM read: the whole lead range up to today.

        Contacts and associations are re-read for every matched contact, whatever its lead's
        date, and, when discovery runs, for every stored Google-touch contact, whatever its
        touch date, so each run re-covers the stored range and its completeness replaces the
        stored one (``record_freshness``): the range starts at the earlier of the stored
        start and the staging range's start (``BACKFILL_DAYS``, at most 180 days).
        """
        start = scope_start.astimezone(UTC).date()
        if row is not None and row.covered_from is not None:
            start = min(start, row.covered_from)
        return start, max(start, self.ctx.now.astimezone(UTC).date())

    async def _search_deals(self, client: HubSpotClient, start: datetime) -> list[str]:
        """Step 4: every deal created from ``start`` to now (``hubspot.deal_search``).

        One transaction stores the deals read and records the outcome: a success, or, when a
        transient failure stopped the search, the failed attempt, which queues the rerun when
        it stored deals (:meth:`_complete`). Returns the ids of the deals read, also when the
        search was interrupted, so their deal-to-contact associations are still read.
        """
        ctx = self.ctx
        name = DATASET_HUBSPOT_DEAL_SEARCH
        outcome = await client.search_partitioned(_DEALS, deal_search_filter(), (start, ctx.now))
        deals = list(
            {row.hs_id: row for row in (parse_deal(record) for record in outcome.rows)}.values()
        )
        failure = outcome.interrupted_by
        async with ctx.sessionmaker() as session, session.begin():
            await repositories.ensure_writable(session, ctx.dataset_id)
            stored = await repositories.upsert_entities(
                session,
                CrmDeal,
                [
                    {
                        "hs_id": row.hs_id,
                        "created_at": row.created_at,
                        "stage": row.stage,
                        "pipeline": row.pipeline,
                        "amount": row.amount,
                    }
                    for row in deals
                ],
                ctx.dataset_id,
            )
            recorded = (
                FreshnessOutcome.failure(ctx.now, reason=failure.reason)
                if failure is not None
                else _success(
                    ctx.now,
                    covered=(start.astimezone(UTC).date(), ctx.now.astimezone(UTC).date()),
                    reasons=[_search_reason(outcome)],
                )
            )
            await self._complete(
                session, {name: recorded}, changed=bool(stored), rows={name: len(stored)}
            )
        if failure is not None:
            # The deals read are stored with the failed attempt: the dataset keeps its last
            # good standing, and on_error decides the rest.
            self._interrupted(failure, dataset=name)
        return sorted(row.hs_id for row in deals)

    async def _associations(
        self,
        client: HubSpotClient,
        props: ContactPropertyMap,
        rows: Mapping[str, DatasetFreshness],
        scope_start: datetime,
        refreshed: ContactBatch,
        discovery: _Discovery | None,
        deal_ids: Sequence[str],
    ) -> None:
        """Step 5: associations with their target reads (``hubspot.associations``).

        The contacts are step 3's population, read again after it: every matched contact
        and, when discovery ran, every stored Google-touch contact, whatever its touch date.
        A contact is a known deal-to-contact target, not read again, when step 3 read it or
        this run's discovery returned it. Every read happens first; one transaction then
        stores what was read and records the outcome: a success, or, when a call failed after
        its retries, the failed attempt, which queues the rerun when it wrote rows
        (:meth:`_complete`).
        """
        ctx = self.ctx
        name = DATASET_HUBSPOT_ASSOCIATIONS
        async with ctx.sessionmaker() as session:
            matched = await self._matched_contacts(session)
            google = await self._google_contacts(session) if discovery is not None else []
        contact_ids = sorted({*matched, *google})
        searched = set(deal_ids)
        meetings_fetch = await self._fetch(client, _CONTACTS, _MEETINGS, contact_ids)
        deals_fetch = await self._fetch(client, _CONTACTS, _DEALS, contact_ids)
        deal_contacts_fetch = await self._fetch(client, _DEALS, _CONTACTS, deal_ids)

        meeting_targets = list(meetings_fetch.all_targets)
        meeting_read = (
            await client.meetings(meeting_targets)
            if meeting_targets
            else TargetRead[MeetingRow](MappingProxyType({}), frozenset(), "ok")
        )
        deal_targets = [target for target in deals_fetch.all_targets if target not in searched]
        deal_read = (
            await client.deals(deal_targets)
            if deal_targets
            else TargetRead[DealRow](MappingProxyType({}), frozenset(), "ok")
        )
        known_contacts = {
            hs_id for hs_id, status in refreshed.read_status.items() if status == CONTACT_READ_OK
        } | (discovery.found if discovery is not None else frozenset())
        contact_targets = [
            target for target in deal_contacts_fetch.all_targets if target not in known_contacts
        ]
        contact_read = (
            await client.contacts_by_id(contact_targets, props)
            if contact_targets
            else ContactBatch(records=MappingProxyType({}), read_status={})
        )
        # The first call that still failed after its retries, in step order. Its inputs and
        # targets are already ``error``/``partial`` or missing, and are stored as such below.
        interruption = next(
            (
                error
                for error in (
                    meetings_fetch.interrupted_by,
                    deals_fetch.interrupted_by,
                    deal_contacts_fetch.interrupted_by,
                    meeting_read.interrupted_by,
                    deal_read.interrupted_by,
                    contact_read.interrupted_by,
                )
                if error is not None
            ),
            None,
        )

        async with ctx.sessionmaker() as session, session.begin():
            await repositories.ensure_writable(session, ctx.dataset_id)
            meetings = [
                {
                    "hs_id": row.hs_id,
                    "created_at": row.created_at,
                    "start_at": row.start_at,
                    "meeting_timestamp": row.meeting_timestamp,
                    "outcome": row.outcome,
                    "meeting_source": row.meeting_source,
                }
                for row in meeting_read.records.values()
            ]
            await repositories.upsert_entities(session, CrmMeeting, meetings, ctx.dataset_id)
            deals = [
                {
                    "hs_id": row.hs_id,
                    "created_at": row.created_at,
                    "stage": row.stage,
                    "pipeline": row.pipeline,
                    "amount": row.amount,
                }
                for row in deal_read.records.values()
            ]
            await repositories.upsert_entities(session, CrmDeal, deals, ctx.dataset_id)
            contacts_read = [
                hs_id
                for hs_id, status in contact_read.read_status.items()
                if status == CONTACT_READ_OK
            ]
            await repositories.upsert_entities(
                session,
                CrmContact,
                [
                    _contact_values(contact_read.records[hs_id])
                    for hs_id in contacts_read
                    if hs_id in contact_read.records
                ],
                ctx.dataset_id,
            )
            written = len(meetings) + len(deals) + len(contacts_read)
            known: dict[str, set[str]] = {
                _MEETINGS: set(meeting_read.records),
                _DEALS: searched | set(deal_read.records),
                _CONTACTS: known_contacts | set(contacts_read),
            }
            unknown = 0
            for fetch in (meetings_fetch, deals_fetch, deal_contacts_fetch):
                fetch_unknown, fetch_written = await self._write_fetch(
                    session, fetch, known[fetch.to_type]
                )
                unknown += fetch_unknown
                written += fetch_written
            reason = (
                f"{_plural(unknown, 'association read', 'association reads')} unknown: the "
                "fetch or a target read failed"
                if unknown
                else None
            )
            recorded = (
                FreshnessOutcome.failure(ctx.now, reason=interruption.reason)
                if interruption is not None
                else _success(
                    ctx.now,
                    covered=self._state_range(rows.get(name), scope_start),
                    reasons=[reason],
                )
            )
            # Every replaced association belongs to an input whose fetch row was written, so
            # the rows written alone tell whether the transaction changed data.
            await self._complete(
                session, {name: recorded}, changed=written > 0, rows={name: written}
            )
        if interruption is not None:
            # Every fetch row keeps its unknown state and is stored with the failed attempt:
            # the dataset keeps its last good standing, and on_error decides the rest.
            self._interrupted(interruption, dataset=name)

    async def _fetch(
        self, client: HubSpotClient, from_type: str, to_type: str, ids: Sequence[str]
    ) -> AssociationFetch:
        """Read associations of ``ids``; an empty input makes no call."""
        if not ids:
            return AssociationFetch(from_type, to_type, MappingProxyType({}))
        return await client.associations(from_type, to_type, ids)

    async def _write_fetch(
        self, session: AsyncSession, fetch: AssociationFetch, known: set[str]
    ) -> tuple[int, int]:
        """Store one association read and its fetch rows in ``session``'s transaction.

        Returns the inputs left unknown and the rows written (associations and fetch rows),
        which the caller counts once its transaction commits.

        On ``ok`` or ``ok_empty`` an input's stored associations of the target type are
        replaced by what was read; on ``partial`` or ``error`` the stored rows stay (they are
        unknown evidence either way) and newly read targets are added. Each fetch row gets the
        target-read outcome: ``ok`` when every associated id was read (or there was none to
        read on an ``ok`` fetch), ``partial`` when some were not, ``error`` when none were,
        and ``NULL`` for a genuine zero (``ok_empty``) or a failed fetch with no targets.
        """
        ctx = self.ctx
        from_type = _SINGULAR[fetch.from_type]
        to_type = _SINGULAR[fetch.to_type]
        replaced = sorted(
            input_id
            for input_id, read in fetch.per_input.items()
            if read.status in (ASSOCIATION_FETCH_OK, ASSOCIATION_FETCH_OK_EMPTY)
        )
        for chunk in _chunks(replaced):
            await session.execute(
                delete(CrmAssociation)
                .where(
                    CrmAssociation.dataset_id == ctx.dataset_id,
                    CrmAssociation.from_type == from_type,
                    CrmAssociation.to_type == to_type,
                    CrmAssociation.from_id.in_(chunk),
                )
                .execution_options(synchronize_session=False)
            )
        links = {
            (input_id, target): {
                "from_type": from_type,
                "from_id": input_id,
                "to_type": to_type,
                "to_id": target,
            }
            for input_id, read in fetch.per_input.items()
            if read.status in (ASSOCIATION_FETCH_OK, ASSOCIATION_FETCH_PARTIAL)
            for target in read.targets
        }
        await repositories.upsert_entities(
            session, CrmAssociation, list(links.values()), ctx.dataset_id
        )
        unknown = 0
        fetch_rows: list[dict[str, Any]] = []
        for input_id, read in fetch.per_input.items():
            target_status: str | None
            missing: list[str] = []
            targets = set(read.targets)
            if read.status == ASSOCIATION_FETCH_OK_EMPTY:
                target_status = None
            elif not targets:
                target_status = TARGET_READ_OK if read.status == ASSOCIATION_FETCH_OK else None
            else:
                missing = sorted(targets - known)
                if not missing:
                    target_status = TARGET_READ_OK
                elif len(missing) == len(targets):
                    target_status = TARGET_READ_ERROR
                else:
                    target_status = TARGET_READ_PARTIAL
            if read.status in (ASSOCIATION_FETCH_PARTIAL, ASSOCIATION_FETCH_ERROR) or (
                read.status == ASSOCIATION_FETCH_OK and target_status != TARGET_READ_OK
            ):
                unknown += 1
            fetch_rows.append(
                {
                    "object_type": from_type,
                    "object_id": input_id,
                    "to_type": to_type,
                    "status": read.status,
                    "target_read_status": target_status,
                    "missing_target_ids": missing,
                    "error": None if read.error is None else _truncate(read.error),
                }
            )
        await repositories.upsert_entities(session, CrmAssociationFetch, fetch_rows, ctx.dataset_id)
        if unknown:
            _log.warning(
                "sync.crm.associations_unknown",
                crm_dataset_id=ctx.dataset_id,
                from_type=from_type,
                to_type=to_type,
                inputs=len(fetch.per_input),
                unknown=unknown,
                statuses=dict(fetch.status_counts()),
            )
        return unknown, len(links) + len(fetch_rows)


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def _ordered_jobs(source: Source, jobs: Sequence[str] | None) -> tuple[str, ...]:
    """The requested jobs of ``source`` in run order (all of :data:`SOURCE_JOBS` by default).

    Raises:
        ValueError: If a job name is not one the source runs.
    """
    if jobs is None:
        return SOURCE_JOBS[source]
    unknown = sorted(set(jobs) - _ACCEPTED_JOBS[source])
    if unknown:
        raise ValueError(
            f"{source} runs {sorted(_ACCEPTED_JOBS[source])}; unknown job(s) {unknown}"
        )
    return tuple(job for job in _JOB_ORDER if job in set(jobs))


def _live_blocked(settings: Settings, source: Source, mode: Mode) -> str | None:
    """Why a ``mode`` client of ``source`` may not be opened now, or ``None``.

    A live dataset is synced only under ``DATA_MODE=auto`` with the source's credentials;
    otherwise ``ensure_dataset`` switches the source and the sync waits for it.
    """
    if mode != "live":
        return None
    if settings.data_mode == "simulated":
        return "live dataset not synced: DATA_MODE=simulated"
    if not settings.has_credentials(source):
        return "live dataset not synced: credentials not configured"
    return None


def _job(ctx: JobContext, name: str, *, backfill: bool = False) -> _JobBase:
    """The job object for one job name."""
    match name:
        case "entities":
            return EntitySyncJob(ctx)
        case "metrics":
            return MetricsSyncJob(ctx, "backfill" if backfill else "incremental")
        case "restatement":
            return MetricsSyncJob(ctx, "restatement")
        case "segments":
            return SegmentSyncJob(ctx)
        case "leads":
            return LeadSyncJob(ctx, backfill=backfill)
        case "crm":
            return CrmSyncJob(ctx, backfill=backfill)
        case _:
            raise ValueError(f"unknown job {name!r}")


async def _companion_crm(
    controller: SourceModeController,
    companion_id: int,
    *,
    now: datetime,
    backfill: bool,
    thresholds: Thresholds,
) -> JobResult:
    """Run the companion CRM dataset's sync against the simulated HubSpot transport.

    ``thresholds`` is the configuration snapshot of the run that owns the companion, so the
    companion's deal search spans the same default window as its parent's jobs.
    """
    async with controller.open_clients("hubspot", "simulated") as clients:
        ctx = await JobContext.for_dataset(
            controller, companion_id, clients, now=now, thresholds=thresholds
        )
        if backfill:
            ctx = _inactive(ctx)
        return await CrmSyncJob(ctx, backfill=backfill).run()


def _inactive(ctx: JobContext) -> JobContext:
    """The same context marked inactive: a backfill queues no analysis request."""
    return JobContext(
        controller=ctx.controller,
        source=ctx.source,
        dataset_id=ctx.dataset_id,
        mode=ctx.mode,
        role=ctx.role,
        active=False,
        clients=ctx.clients,
        settings=ctx.settings,
        thresholds=ctx.thresholds,
        now=ctx.now,
        sessionmaker=ctx.sessionmaker,
    )


def _log_report(report: SourceSyncReport) -> SourceSyncReport:
    """Log one ``sync.source.finish`` summary (counts only) and return the report."""
    _log.info(
        "sync.source.finish",
        source=report.source,
        trigger=report.trigger,
        dataset_id=report.dataset_id,
        mode=report.mode,
        reason=report.reason,
        ok=report.ok,
        jobs={result.job: result.status for result in report.results},
        rows=sum(result.rows for result in report.results),
        calls=sum(result.calls for result in report.results),
    )
    return report


async def run_source_jobs(
    source: Source,
    *,
    trigger: Literal["scheduled", "manual"],
    jobs: Sequence[str] | None = None,
    controller: SourceModeController | None = None,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
) -> SourceSyncReport:
    """Run a source's sync jobs now, against its active primary dataset.

    Shared by the scheduler (due jobs and ``sync_request`` rows) and ``ads-engine sync``.
    The jobs run in the order entities, metrics, restatement, segments, leads, crm, with
    clients of the dataset's mode. While LinkedIn is simulated, its ``leads`` job is followed
    by its companion CRM dataset's sync (:data:`JOB_CRM_COMPANION`), against the simulated
    HubSpot transport. HubSpot runs its primary's :class:`CrmSyncJob`. An ``AccessDenied``
    stops the source's remaining jobs: ``on_error`` has decided what follows, and the
    scheduler's next ``ensure_dataset`` builds the simulated dataset. Every job of the call,
    the companion's included, runs on one configuration snapshot.

    Args:
        source: ``google``, ``linkedin`` or ``hubspot``.
        trigger: ``scheduled`` or ``manual``.
        jobs: The job names to run (:data:`JOB_ENTITIES`, :data:`JOB_METRICS`,
            :data:`JOB_RESTATEMENT`, :data:`JOB_SEGMENTS`, :data:`JOB_LEADS`,
            :data:`JOB_CRM`); :data:`SOURCE_JOBS` of the source when omitted.
        controller: The source-mode controller; one on the process settings when omitted.
        now: The run's instant; the controller's clock when omitted.
        thresholds: The configuration snapshot the jobs run on; one read of the controller's
            :attr:`~ads_engine.sync.mode.SourceModeController.thresholds` when omitted. The
            scheduler passes the snapshot it judged the segments due with, so the presets
            fetched and recorded are the presets it detected.

    Returns:
        The report. With no active dataset (the scheduler's ``ensure_dataset`` builds it), or
        a live dataset that may not be synced now, no job runs and ``reason`` says why.

    Raises:
        ValueError: If ``source`` or a job name is unknown.
        ads_engine.analysis.thresholds.ConfigError: If ``thresholds`` is omitted and the
            configuration fails to load.
    """
    checked = _as_source(source)
    requested = _ordered_jobs(checked, jobs)
    ctl = controller if controller is not None else SourceModeController()
    instant = now if now is not None else ctl.now()
    async with ctl.sessionmaker() as session:
        dataset_id = await repositories.active_dataset_id(session, checked)
    if dataset_id is None:
        return _log_report(
            SourceSyncReport(checked, trigger, reason=f"{checked} has no active dataset yet")
        )
    dataset = await ctl.datasets.get(dataset_id)
    mode = _as_mode(dataset.mode)
    blocked = _live_blocked(ctl.settings, checked, mode)
    if blocked is not None:
        return _log_report(
            SourceSyncReport(checked, trigger, dataset_id=dataset_id, mode=mode, reason=blocked)
        )
    companion_id: int | None = None
    if checked == "linkedin" and mode == "simulated":
        companion_id = await ctl.datasets.companion_of(dataset_id)
    config = thresholds if thresholds is not None else ctl.thresholds

    results: list[JobResult] = []
    async with ctl.open_clients(checked, mode) as clients:
        ctx = await JobContext.for_dataset(ctl, dataset_id, clients, now=instant, thresholds=config)
        for name in requested:
            result = await _job(ctx, name).run()
            results.append(result)
            if result.access_denied:
                break
            if name == JOB_LEADS and companion_id is not None:
                companion = await _companion_crm(
                    ctl, companion_id, now=instant, backfill=False, thresholds=config
                )
                results.append(companion)
                if companion.access_denied:
                    break
    return _log_report(
        SourceSyncReport(checked, trigger, results=tuple(results), dataset_id=dataset_id, mode=mode)
    )


async def backfill_dataset(
    controller: SourceModeController, dataset_id: int, *, now: datetime | None = None
) -> SourceSyncReport:
    """Backfill a building primary dataset: the contract ``sync/mode.py`` builds call.

    By the dataset's source (every job of the building dataset with ``active=False``, so no
    analysis request; the activation queues one):

    * **Google**: entities (the first entity sync writes the baseline), then metrics over
      ``BACKFILL_DAYS``.
    * **LinkedIn**: entities, metrics (capped at 180 days), segments and leads over the
      backfill range. Simulated: then the companion CRM dataset's sync with
      ``backfill=True``. Live: then one matching pass of this dataset's leads in HubSpot's
      active primary, so every staged row is attempted before the dataset activates, and on
      success a CRM sync of that primary, so the new matches' evidence is read too. That
      primary is active, so the sync's success queues its own analysis request.
    * **HubSpot** primary: the CRM sync with ``backfill=True`` (``sync/mode.py`` staged the
      rows before calling this).

    An ``AccessDenied`` stops the backfill: ``on_error`` advanced the source's access
    generation, which voids a live build. Connector errors never propagate; the build reads
    readiness from the freshness rows the jobs recorded. A rerun (``sync/mode.py`` reruns
    the backfill when matching added rows after it) is idempotent. Every job of the backfill,
    its CRM step included, runs on one configuration snapshot read from the controller once.

    Args:
        controller: The controller running the build.
        dataset_id: A ``building`` primary dataset.
        now: The run's instant; the controller's clock when omitted.

    Returns:
        The jobs' report, with trigger ``backfill``.

    Raises:
        ValueError: If the dataset is a companion (it is backfilled with its parent).
        ads_engine.sync.datasets.DatasetNotFoundError: If the dataset does not exist.
        ads_engine.analysis.thresholds.ConfigError: If the configuration fails to load.
    """
    instant = now if now is not None else controller.now()
    dataset = await controller.datasets.get(dataset_id)
    source = _as_source(dataset.source)
    mode = _as_mode(dataset.mode)
    if dataset.role != DATASET_ROLE_PRIMARY:
        raise ValueError(
            f"dataset {dataset_id} is a {dataset.role}; it is backfilled with its parent"
        )
    names = {
        "google": (JOB_ENTITIES, JOB_METRICS),
        "linkedin": (JOB_ENTITIES, JOB_METRICS, JOB_SEGMENTS, JOB_LEADS),
        "hubspot": (JOB_CRM,),
    }[source]
    results: list[JobResult] = []
    denied = False
    async with controller.open_clients(source, mode) as clients:
        ctx = _inactive(await JobContext.for_dataset(controller, dataset_id, clients, now=instant))
        for name in names:
            result = await _job(ctx, name, backfill=True).run()
            results.append(result)
            if result.access_denied:
                denied = True
                break
        if source == "linkedin" and not denied:
            results.extend(await _linkedin_crm_backfill(controller, ctx, clients))
    return _log_report(
        SourceSyncReport(
            source, "backfill", results=tuple(results), dataset_id=dataset_id, mode=mode
        )
    )


async def _linkedin_crm_backfill(
    controller: SourceModeController, ctx: JobContext, clients: SourceClients
) -> tuple[JobResult, ...]:
    """The CRM step of a LinkedIn backfill, before the dataset activates.

    Simulated: the companion CRM dataset's sync. Live: a matching pass of this dataset's
    staged leads in HubSpot's active primary, re-fetched with the building dataset's own
    client; when it succeeds, a CRM sync of that primary follows, so the newly matched
    contacts get their contact refresh, associations and target reads now. LinkedIn-attributed
    CRM evidence is then known from the first run on the new dataset (AAP 0.3.3, staged
    matching) instead of reading as unknown until the next three-hourly CRM sync. Every CRM
    context runs on the backfill's configuration snapshot (``ctx.thresholds``).

    Returns:
        The results of the jobs run, in order; empty when there is no CRM dataset to use.
    """
    if ctx.mode == "simulated":
        companion_id = await controller.datasets.companion_of(ctx.dataset_id)
        if companion_id is None:
            _log.warning("sync.backfill.no_companion", dataset_id=ctx.dataset_id)
            return ()
        return (
            await _companion_crm(
                controller, companion_id, now=ctx.now, backfill=True, thresholds=ctx.thresholds
            ),
        )
    async with controller.sessionmaker() as session:
        crm_id = await repositories.active_dataset_id(session, "hubspot")
    if crm_id is None:
        _log.warning(
            "sync.backfill.no_crm",
            dataset_id=ctx.dataset_id,
            reason="HubSpot has no active primary dataset to match the leads against",
        )
        return ()
    crm = await controller.datasets.get(crm_id)
    crm_mode = _as_mode(crm.mode)
    blocked = _live_blocked(controller.settings, "hubspot", crm_mode)
    if blocked is not None:
        _log.warning("sync.backfill.matching_skipped", crm_dataset_id=crm_id, reason=blocked)
        return ()
    async with controller.open_clients("hubspot", crm_mode) as crm_clients:
        crm_ctx = await JobContext.for_dataset(
            controller, crm_id, crm_clients, now=ctx.now, thresholds=ctx.thresholds
        )
        # The matching pass writes only this build's own rows in the CRM dataset and records
        # no freshness, so it runs inactive. The CRM sync keeps the dataset's own context:
        # it refreshes data of an active dataset already in use, so its success queues the
        # rerun every completed CRM sync queues (AAP 0.3.2), even if this build is then
        # discarded and never activates.
        matched = await LeadMatchJob(
            _inactive(crm_ctx),
            lead_dataset_id=ctx.dataset_id,
            lead_client=clients.require_linkedin(),
        ).run()
        if matched.status != "succeeded":
            return (matched,)
        return matched, await CrmSyncJob(crm_ctx).run()


async def refresh_crm_dataset(
    controller: SourceModeController, crm_dataset_id: int, *, now: datetime | None = None
) -> JobResult | None:
    """Run one CRM sync of a HubSpot dataset on behalf of another source's build.

    ``sync/mode.py`` calls it while a live LinkedIn build settles. When HubSpot replaced its
    primary after the build's backfill, the build's leads are staged and matched in the new
    primary by a matching pass alone, which stores the contacts but reads none of their
    associations. This sync of that primary runs the CRM steps of :class:`CrmSyncJob`
    (matching of due rows, contact refresh, the window deal search, associations with their
    target reads and, when configured, Google-touch discovery), so the build's new matches
    are hydrated before the LinkedIn dataset activates (AAP 0.3.3, staged matching).

    The sync runs on the dataset's own context. For an ``active`` dataset, the data in use,
    its success queues a default-window ``analysis_request`` like every completed CRM sync
    (AAP 0.3.2 "Events that trigger a rerun"), whether or not the LinkedIn build then
    activates; the worker coalesces it with the activation's request when both arrive
    within 60 s. A ``building`` dataset queues none. Connector errors go to ``on_error`` and
    come back as a failed result, as for any job.

    Args:
        controller: The controller running the build.
        crm_dataset_id: The HubSpot dataset (normally HubSpot's active primary) to sync.
        now: The run's instant; the controller's clock when omitted.

    Returns:
        The CRM sync's result, or ``None`` when no client of the dataset's mode may be
        opened now (a live dataset under ``DATA_MODE=simulated`` or without HubSpot
        credentials), in which case nothing ran.

    Raises:
        ValueError: If the dataset is not a HubSpot dataset.
        ads_engine.sync.datasets.DatasetNotFoundError: If the dataset does not exist.
        ads_engine.analysis.thresholds.ConfigError: If the configuration fails to load.
    """
    instant = now if now is not None else controller.now()
    dataset = await controller.datasets.get(crm_dataset_id)
    if dataset.source != SOURCE_HUBSPOT:
        raise ValueError(
            f"dataset {crm_dataset_id} is a {dataset.source} dataset; the CRM sync runs in a "
            "HubSpot dataset"
        )
    mode = _as_mode(dataset.mode)
    blocked = _live_blocked(controller.settings, "hubspot", mode)
    if blocked is not None:
        _log.warning("sync.crm_refresh.skipped", crm_dataset_id=crm_dataset_id, reason=blocked)
        return None
    async with controller.open_clients("hubspot", mode) as clients:
        crm_ctx = await JobContext.for_dataset(controller, crm_dataset_id, clients, now=instant)
        return await CrmSyncJob(crm_ctx).run()
