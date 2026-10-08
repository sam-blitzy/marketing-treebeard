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
                         180). Upserts overwrite, so restated data wins.
:class:`SegmentSyncJob`  LinkedIn demographic aggregates for each exact preset       04:00
                         window ending on the default window end; never summed from
                         daily rows.
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
finish; ``sync.job.start`` and ``sync.job.finish`` log events (source, job, dataset, rows,
calls, completeness, duration; never an email, a secret or a token); the dataset's writability
check (a dataset retired mid-job ends the job quietly as failed); and error hand-off. Only
``ConnectorError`` subclasses are caught, and every one goes to
:meth:`~ads_engine.sync.mode.SourceModeController.on_error`, which alone decides what it means
(AAP 0.3.3 fallback table). ``on_error`` is never called while a transaction of this module is
open, because it takes the ``source_connection`` lock that a concurrent activation takes before
the dataset guard this module holds. A job that succeeded for an **active** dataset queues one
``analysis_request`` in the same transaction as its freshness write (AAP 0.3.2); a building
dataset queues nothing, its activation does.

Interruptions. Connectors return what they read when a ``TransientError`` survives their
retries. A Google call-budget deferral is recorded as an ``incomplete`` success whose reason
starts with :data:`~ads_engine.sync.freshness.REASON_DEFERRED`, so the next run widens its
range and finishes the deferred pages (AAP 0.3.3 "Budget"). Any other interruption of a dated
dataset is recorded as a failed attempt, so the dataset keeps its last good data and turns
stale (AAP 0.3.3 transient row). An interrupted full-state entity read is ``incomplete`` with
its reason: no removal or disappearance is inferred from it (AAP 0.3.3 "Safe absence").

Entry points: :func:`run_source_jobs` (the scheduler's due jobs and ``sync_request`` rows, and
``ads-engine sync``) and :func:`backfill_dataset` (the build in ``sync/mode.py``). Tests and
tools can run one job directly on injected clients through :meth:`JobContext.for_dataset`::

    async with controller.open_clients("google", "simulated") as clients:
        ctx = await JobContext.for_dataset(controller, dataset_id, clients)
        result = await EntitySyncJob(ctx).run()
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    FetchOutcome,
    PartitionedFetcher,
    enumerate_campaigns,
    enumerate_entities,
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
from ads_engine.db.models.ops import DatasetFreshness, SourceDataset, SyncRun
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

# HubSpot API object types (plural, as paths name them) and the singular names stored.
_CONTACTS: Final = "contacts"
_MEETINGS: Final = "meetings"
_DEALS: Final = "deals"
_SINGULAR: Final[Mapping[str, str]] = MappingProxyType(
    {_CONTACTS: "contact", _MEETINGS: "meeting", _DEALS: "deal"}
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
            active=dataset.state == "active",
            clients=clients,
            settings=controller.settings,
            thresholds=thresholds if thresholds is not None else controller.thresholds,
            now=now if now is not None else controller.now(),
            sessionmaker=controller.sessionmaker,
        )


@dataclass(frozen=True, slots=True)
class JobResult:
    """What one job did; counts and reasons only, never ids of people, emails or secrets.

    Attributes:
        job: The job name (:data:`JOB_ENTITIES` …, :data:`JOB_CRM_COMPANION`,
            :data:`JOB_LEAD_MATCH`).
        source: The source of the dataset written.
        dataset_id: The dataset written.
        status: ``succeeded`` when every freshness dataset of the job recorded a success
            (complete or incomplete); ``failed`` when the job raised, or a dataset's refresh
            failed and kept its last good data.
        rows: Rows written (entities, facts, leads, contacts, associations, …).
        calls: Connector calls made.
        freshness: The outcome recorded per freshness dataset name; a dataset whose refresh
            failed carries the failure outcome ``on_error`` recorded for it.
        disposition: What ``on_error`` decided for the last connector error the job handed
            over, or ``None`` when there was none.
        error: The connector error's reason, or why the job could not write; ``None`` when
            nothing failed.
        duration_ms: Wall-clock duration.
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
        """Whether a job failed on a ``QueryDefect`` (the CLI's non-zero exit)."""
        return any(result.disposition == "defect" for result in self.results)

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
    (:data:`~ads_engine.sync.freshness.REASON_DEFERRED` or ``REASON_INTERRUPTED``), so the
    next run widens its range (:func:`~ads_engine.sync.freshness.needs_repair`).
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
    row = (await freshness.load_freshness(session, dataset_id)).get("linkedin.segments")
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
    ``_execute`` they write in short transactions of their own and record outcomes with
    :meth:`_complete` in the transaction that writes the data those outcomes describe.
    Transient interruptions are noted with :meth:`_interrupted` and handed to
    :meth:`SourceModeController.on_error` once no transaction is open.
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
        self._enqueued = False
        self._disposition: ErrorDisposition | None = None
        self._error: str | None = None

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

        Returns:
            The result. A ``ConnectorError`` the connectors raise is handed to ``on_error``
            and returned as a failed result with its disposition; a dataset retired while
            the job ran ends it as failed with that reason.

        Raises:
            Any non-connector error (a database error, a bug) after the job's ``sync_run``
            rows were marked failed; the scheduler logs it and the next slot retries.
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
        )
        try:
            await self._open_runs(self.freshness_datasets())
            await self._execute()
        except ConnectorError as exc:
            pending = tuple(
                name for name in self.freshness_datasets() if name not in self._freshness
            )
            self._disposition = await self._hand_over(exc, pending)
            text = _failure_text(exc)
            self._error = text
            failures = {name: FreshnessOutcome.failure(ctx.now, reason=text) for name in pending}
            for name, outcome in failures.items():
                self._failed[name] = text
                self._freshness[name] = outcome
            if self._disposition != "stale":
                # ``on_error`` records a transient's failure on the named rows itself; a
                # defect or a denial is recorded here, so ``last_attempt_at`` moves on every
                # attempt (AAP 0.5.2) and a due check backing off from a failed attempt
                # (``segments_due``) sees it.
                await self._record_failures(failures)
            await self._close_remaining(text)
        except repositories.DatasetNotWritable as exc:
            # Retired (or discarded) while the job ran: its rows are read-only history now.
            self._error = str(exc)
            _log.info("sync.job.not_writable", source=ctx.source, job=self.job, reason=self._error)
            await self._close_remaining(self._error)
        except Exception:
            await self._close_remaining("job failed unexpectedly", quiet=True)
            raise
        else:
            await self._report_interruptions()
            await self._close_remaining(None)
        return self._result(started)

    def _result(self, started: float) -> JobResult:
        """Build the result and log ``sync.job.finish``."""
        ctx = self.ctx
        status: Literal["succeeded", "failed"] = (
            "failed" if self._error is not None or self._failed else "succeeded"
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
        )
        log = _log.warning if status == "failed" else _log.info
        log(
            "sync.job.finish",
            source=ctx.source,
            job=self.job,
            dataset_id=ctx.dataset_id,
            status=status,
            rows=result.rows,
            calls=result.calls,
            completeness=result.completeness,
            datasets={name: outcome.completeness for name, outcome in self._freshness.items()},
            disposition=result.disposition,
            duration_ms=result.duration_ms,
        )
        return result

    # -- helpers for subclasses ------------------------------------------------------------

    def _count(self, dataset: str | None, rows: int) -> None:
        """Add ``rows`` written to a freshness dataset's (and the job's) row count."""
        self._total_rows += rows
        if dataset is not None:
            self._rows[dataset] = self._rows.get(dataset, 0) + rows

    def _interrupted(self, error: TransientError, *, dataset: str | None) -> None:
        """Note a transient interruption a connector returned instead of raising.

        Args:
            error: The interruption.
            dataset: The freshness dataset whose refresh it failed, so ``on_error`` records
                the failure and the dataset keeps its last good data and turns stale; or
                ``None`` when the shortfall is recorded as an ``incomplete`` success (a
                call-budget deferral, an incomplete full-state read) and only the
                connection's health is reported.
        """
        names = (dataset,) if dataset is not None else ()
        self._interruptions.append(_Interruption(error, names))
        if dataset is not None:
            self._failed[dataset] = error.reason
            self._freshness[dataset] = FreshnessOutcome.failure(self.ctx.now, reason=error.reason)

    async def _complete(
        self, session: AsyncSession, outcomes: Mapping[str, FreshnessOutcome]
    ) -> None:
        """Record freshness outcomes in the transaction that wrote their data.

        Closes the outcomes' ``sync_run`` rows and, the first time a success is recorded for
        an active dataset, queues the job's ``analysis_request`` in the same transaction
        (AAP 0.3.2 "a completed entity, metrics, segment, lead or CRM sync").

        Raises:
            repositories.DatasetNotWritable: If the dataset was retired meanwhile.
        """
        ctx = self.ctx
        dataset = await repositories.ensure_writable(session, ctx.dataset_id)
        for name, outcome in outcomes.items():
            await freshness.record_freshness(session, ctx.dataset_id, name, outcome)
            self._freshness[name] = outcome
        for name, outcome in outcomes.items():
            await self._close(
                session,
                name,
                status="succeeded" if outcome.succeeded else "failed",
                completeness=outcome.completeness,
                error=None if outcome.succeeded else outcome.reason,
            )
        succeeded = any(outcome.succeeded for outcome in outcomes.values())
        if succeeded and not self._enqueued and ctx.active and dataset.state == "active":
            await repositories.enqueue_analysis_request(
                session, reason=f"sync:{ctx.source}.{self.job}"
            )
            self._enqueued = True

    async def _hand_over(self, error: ConnectorError, datasets: Sequence[str]) -> ErrorDisposition:
        """Hand one connector error to ``on_error``; no transaction of this job is open.

        An error of another source (LinkedIn during a CRM job's matching) is that source's
        error, reported without this job's dataset.
        """
        ctx = self.ctx
        error_source = _as_source(error.source) if error.source in SOURCES else ctx.source
        if error_source == ctx.source:
            disposition = await ctx.controller.on_error(
                error_source, error, dataset_id=ctx.dataset_id, datasets=tuple(datasets)
            )
        else:
            disposition = await ctx.controller.on_error(error_source, error)
        # ``log_fields`` carries the error's own ``source``; it is logged as ``error_source``
        # so the job's source keeps its key.
        fields: dict[str, object] = {
            **error.log_fields,
            "source": ctx.source,
            "error_source": error.source,
            "job": self.job,
            "dataset_id": ctx.dataset_id,
            "disposition": disposition,
        }
        _log.warning("sync.job.connector_error", **fields)
        return disposition

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

    async def _report_interruptions(self) -> None:
        """Hand every noted interruption to ``on_error``, outside any transaction."""
        for interruption in self._interruptions:
            self._disposition = await self._hand_over(interruption.error, interruption.datasets)

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
                        status="running",
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
    ) -> None:
        """Close one dataset's ``sync_run`` rows in ``session``."""
        ids = self._runs.get(name)
        if not ids or name in self._closed:
            return
        rows = self._total_rows if name is None else self._rows.get(name, 0)
        await session.execute(
            update(SyncRun)
            .where(SyncRun.id.in_(ids))
            .values(
                finished_at=self.ctx.controller.now(),
                status=status,
                rows=rows,
                completeness=completeness,
                error=None if error is None else _truncate(error),
            )
        )
        self._closed.add(name)

    async def _close_remaining(self, error: str | None, *, quiet: bool = False) -> None:
        """Close every still-open ``sync_run`` row: failed datasets failed, the rest by ``error``.

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
                        status="failed" if failed else "succeeded",
                        completeness=None if failed else "complete",
                        error=reason if reason is not None else error,
                    )

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
    dataset, as for every repository write.

    Returns:
        The number of rows requested.

    Raises:
        repositories.DatasetNotWritable: If the dataset is missing or retired.
    """
    if not rows:
        return 0
    dataset = await repositories.ensure_writable(session, dataset_id)
    values = [{**row, "dataset_id": dataset_id, "source": dataset.source} for row in rows]
    await session.execute(
        insert(model).values(values).on_conflict_do_nothing(index_elements=list(natural_key))
    )
    return len(values)


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


async def _snapshotted_ids(session: AsyncSession, dataset_id: int, entity_type: str) -> set[int]:
    """Ids of a dataset's entities of one type that have at least one snapshot (tracked)."""
    result = await session.scalars(
        select(EntitySnapshot.entity_id)
        .where(EntitySnapshot.dataset_id == dataset_id, EntitySnapshot.entity_type == entity_type)
        .distinct()
    )
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
    new version gets ``first_seen_at = now``; a known version keeps its first sighting and
    only its email question ids are rewritten.

    Returns:
        The number of version rows written.
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
    existing = await repositories.entity_id_map(session, LeadFormVersion, dataset_id)
    new_rows: list[dict[str, Any]] = []
    known: list[tuple[int, list[str]]] = []
    for (urn, version), version_map in sorted(distinct.items()):
        form_id = forms[urn]
        version_id = existing.get((form_id, version))
        if version_id is None:
            new_rows.append(
                {
                    "lead_form_id": form_id,
                    "version": version,
                    "email_question_ids": version_map.stored_email_question_ids,
                    "first_seen_at": now,
                }
            )
        else:
            known.append((version_id, list(version_map.stored_email_question_ids)))
    # PostgreSQL checks NOT NULL on the proposed tuple before ON CONFLICT arbitration, so a
    # known version cannot go through the upsert without ``first_seen_at``; supplying it
    # would overwrite the first sighting. Known versions therefore rewrite only their email
    # question ids, in id order (the order every writer of these rows locks them in).
    await repositories.upsert_entities(session, LeadFormVersion, new_rows, dataset_id)
    if known:
        await repositories.ensure_writable(session, dataset_id)
        for version_id, question_ids in sorted(known):
            await session.execute(
                update(LeadFormVersion)
                .where(
                    LeadFormVersion.id == version_id,
                    LeadFormVersion.dataset_id == dataset_id,
                )
                .values(email_question_ids=question_ids)
            )
    return len(new_rows) + len(known)


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
        name = "google.entities"
        client = ctx.clients.require_google()
        fetcher = PartitionedFetcher(client)
        calls_before = client.calls_made
        async with ctx.sessionmaker() as session:
            tracked = await _google_tracked(session, ctx.dataset_id)
            stored_account = await _account_row(session, ctx.dataset_id, "google")
            stored_account_id = None if stored_account is None else stored_account.id
        customer = await fetcher.partitioned_fetch("Q-G1")
        campaigns = await enumerate_campaigns(client, tracked.campaigns)
        ad_groups = await enumerate_entities(client, "Q-G4", tracked.ad_groups)
        keywords = await enumerate_entities(client, "Q-G7", tracked.keywords)
        actions = await fetcher.partitioned_fetch("Q-G8")
        self._calls += client.calls_made - calls_before

        customer_rows = [parse_customer(row) for row in customer.rows]
        if not customer_rows and stored_account_id is None:
            # Without the customer row there is no account to attach campaigns to.
            if customer.interrupted_by is not None:
                raise customer.interrupted_by
            raise _google_defect("Q-G1 returned no customer row")
        interruptions = [
            outcome.interrupted_by
            for outcome in (customer, campaigns, ad_groups, keywords, actions)
            if outcome.interrupted_by is not None
        ]
        reasons = [
            None if customer.complete else customer.reason,
            campaigns.reason if not campaigns.complete else None,
            ad_groups.reason if not ad_groups.complete else None,
            keywords.reason if not keywords.complete else None,
            None if actions.complete else actions.reason,
        ]
        present = [parse_campaign(row) for row in campaigns.present]
        removed = [parse_campaign(row) for row in campaigns.removed]
        group_rows = [parse_ad_group(row) for row in ad_groups.rows]
        keyword_rows = [parse_keyword(row) for row in keywords.rows]
        action_rows = [parse_conversion_action(row) for row in actions.rows]

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
                missing=campaigns.missing_ids if campaigns.complete else frozenset(),
                complete=campaigns.complete,
                reason=campaigns.reason,
                ad_groups=group_rows,
                absent_ad_groups=ad_groups.absent_keys if ad_groups.complete else frozenset(),
                keywords=keyword_rows,
                absent_keywords=keywords.absent_keys if keywords.complete else frozenset(),
                actions=action_rows,
            )
            await self._finish_entities(session, rows, enumeration, sealed=sealed, reasons=reasons)
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
        ad_groups: Sequence[AdGroupRow],
        absent_ad_groups: frozenset[int] | frozenset[tuple[int, int]],
        keywords: Sequence[KeywordRow],
        absent_keywords: frozenset[int] | frozenset[tuple[int, int]],
        actions: Sequence[ConversionActionRow],
    ) -> tuple[list[SnapshotRow], EnumerationResult]:
        """Upsert Google entities and build their snapshots and the safe-absence verdict."""
        ctx = self.ctx
        dataset_id = ctx.dataset_id
        name = "google.entities"
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
            missing_budgets = frozenset((set(shared) & tracked_budgets) - present_budgets)
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
        name = "linkedin.entities"
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
) -> tuple[date, date]:
    """Return the native ``[start, end]`` a metrics run reads for one freshness dataset.

    ``incremental``: the trailing :data:`INCREMENTAL_DAYS` days ending native today;
    ``restatement``: :data:`RESTATEMENT_DAYS`; ``backfill``: ``BACKFILL_DAYS`` (LinkedIn capped
    at its 180-day daily retention). When the dataset's stored row needs repair (deferred or
    interrupted pages), the start widens to the row's ``covered_from`` so the shortfall is
    re-read and the row can turn complete (:func:`~ads_engine.sync.freshness.needs_repair`).
    """
    match kind:
        case "incremental":
            days = INCREMENTAL_DAYS
        case "restatement":
            days = RESTATEMENT_DAYS[source]
        case "backfill":
            days = _span_days(settings, source)
    start = today - timedelta(days=days - 1)
    if freshness.needs_repair(row) and row is not None and row.covered_from is not None:
        start = min(start, row.covered_from)
    if source == "linkedin":
        start = max(start, today - timedelta(days=LINKEDIN_RETENTION_DAYS - 1))
    return start, today


def _fetch_reason(outcome: FetchOutcome) -> str | None:
    """The shortfall of a Google fetch, ``None`` when it read everything."""
    return None if outcome.complete else (outcome.reason or "result limit reached")


class MetricsSyncJob(_JobBase):
    """Daily facts of one Google or LinkedIn dataset over a native date range.

    Facts are stored per native day and never as window aggregates (AAP 0.3.2): Google days
    in the customer account's time zone (Q-G1's ``customer.time_zone``, stored on the
    dataset's ``ad_account``), LinkedIn days in UTC; nothing is rebucketed (decision 6).
    Every upsert overwrites the stored day and stamps ``fetched_at``, so a restatement
    replaces earlier values and a re-run changes no row count.

    * **Google**: Q-G3 to ``fact_campaign_daily`` and Q-G5 to ``fact_keyword_daily``
      (``google.metrics_daily``), Q-G6 to ``fact_search_term_daily``
      (``google.search_terms``), each query committed on its own. A campaign or ad group no
      entity sync stored (removed before tracking) gets a stand-in so its spend counts; a
      keyword row of an unknown keyword is skipped and counted (campaign spend is covered
      by Q-G3, and K3 reviews current keywords only).
    * **LinkedIn**: per account, daily creative analytics to ``fact_creative_daily``
      (``linkedin.analytics_daily``); ``conversions`` and ``conversions_value`` stay
      ``NULL`` (LinkedIn reports leads and website conversions instead). Rows of creatives
      the dataset does not know are counted and make the dataset ``incomplete``.

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
            return ("google.metrics_daily", "google.search_terms")
        return ("linkedin.analytics_daily",)

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
        """Fetch and store Q-G3, Q-G5 and Q-G6 (class docstring)."""
        ctx = self.ctx
        client = ctx.clients.require_google()
        fetcher = PartitionedFetcher(client)
        async with ctx.sessionmaker() as session:
            account = await _account_row(session, ctx.dataset_id, "google")
        account_id = None if account is None else account.id
        today = ctx.now.astimezone(_zone(None if account is None else account.timezone)).date()
        ranges = await self._ranges(today)

        start, end = ranges["google.metrics_daily"]
        scope = {_SCOPE_START: start, _SCOPE_END: end}
        calls_before = client.calls_made
        daily = await fetcher.partitioned_fetch("Q-G3", scope)
        self._calls += client.calls_made - calls_before
        await self._store_campaign_daily(daily, account_id)
        calls_before = client.calls_made
        keyword_view = await fetcher.partitioned_fetch("Q-G5", scope)
        self._calls += client.calls_made - calls_before
        await self._store_keyword_daily(keyword_view)
        await self._record_google("google.metrics_daily", (start, end), (daily, keyword_view))

        start, end = ranges["google.search_terms"]
        calls_before = client.calls_made
        terms = await fetcher.partitioned_fetch("Q-G6", {_SCOPE_START: start, _SCOPE_END: end})
        self._calls += client.calls_made - calls_before
        await self._store_search_terms(terms, account_id)
        await self._record_google("google.search_terms", (start, end), (terms,))

    async def _record_google(
        self, name: str, covered: tuple[date, date], outcomes: Sequence[FetchOutcome]
    ) -> None:
        """Record one Google dataset: a success, a deferral, or a failed refresh.

        A call-budget deferral is an ``incomplete`` success (repairable, reason prefixed
        :data:`~ads_engine.sync.freshness.REASON_DEFERRED`); any other transient interruption
        fails the refresh, so the dataset keeps its last good data and turns stale.
        """
        ctx = self.ctx
        failure = next(
            (
                outcome.interrupted_by
                for outcome in outcomes
                if outcome.interrupted_by is not None
                and not isinstance(outcome.interrupted_by, CallBudgetExhausted)
            ),
            None,
        )
        if failure is not None:
            self._interrupted(failure, dataset=name)
            return
        deferral = next(
            (outcome.interrupted_by for outcome in outcomes if outcome.interrupted_by is not None),
            None,
        )
        outcome = _success(
            ctx.now, covered=covered, reasons=[_fetch_reason(outcome) for outcome in outcomes]
        )
        async with ctx.sessionmaker() as session, session.begin():
            await self._complete(session, {name: outcome})
        if deferral is not None:
            self._interrupted(deferral, dataset=None)

    async def _store_campaign_daily(self, outcome: FetchOutcome, account_id: int | None) -> None:
        """Upsert Q-G3 rows into ``fact_campaign_daily``, stubbing unknown campaigns."""
        ctx = self.ctx
        rows = [parse_campaign_daily(row) for row in outcome.rows]
        if not rows:
            return
        async with ctx.sessionmaker() as session, session.begin():
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
        self._count("google.metrics_daily", written)
        if len(facts) < len(rows):
            _log.warning(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G3",
                rows=len(rows) - len(facts),
                reason="no Google account stored to attach stand-in campaigns to",
            )

    async def _store_keyword_daily(self, outcome: FetchOutcome) -> None:
        """Upsert Q-G5 rows into ``fact_keyword_daily``; rows of unknown keywords are skipped."""
        ctx = self.ctx
        rows = [parse_keyword_view(row) for row in outcome.rows]
        if not rows:
            return
        async with ctx.sessionmaker() as session, session.begin():
            groups = _by_last_key(
                await repositories.entity_id_map(session, AdGroup, ctx.dataset_id)
            )
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
        self._count("google.metrics_daily", written)
        if len(facts) < len(rows):
            # Campaign spend is complete through Q-G3; K3 reviews current keywords only.
            _log.info(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G5",
                rows=len(rows) - len(facts),
                reason="keyword not in the current keyword list",
            )

    async def _store_search_terms(self, outcome: FetchOutcome, account_id: int | None) -> None:
        """Upsert Q-G6 rows into ``fact_search_term_daily``, stubbing unknown parents."""
        ctx = self.ctx
        rows = [parse_search_term(row) for row in outcome.rows]
        if not rows:
            return
        async with ctx.sessionmaker() as session, session.begin():
            campaigns = await _campaign_ids(session, ctx.dataset_id)
            unknown = {str(row.campaign_id) for row in rows} - set(campaigns)
            if unknown and account_id is not None:
                await _stub_campaigns(session, ctx.dataset_id, account_id, unknown, campaigns)
            groups = _by_last_key(
                await repositories.entity_id_map(session, AdGroup, ctx.dataset_id)
            )
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
        self._count("google.search_terms", written)
        if len(facts) < len(rows):
            _log.warning(
                "sync.metrics.rows_skipped",
                source="google",
                query_id="Q-G6",
                rows=len(rows) - len(facts),
                reason="no Google account stored to attach stand-in campaigns to",
            )

    # -- LinkedIn --------------------------------------------------------------------------

    async def _linkedin(self) -> None:
        """Fetch and store daily creative analytics per account (class docstring)."""
        ctx = self.ctx
        name = "linkedin.analytics_daily"
        client = ctx.clients.require_linkedin()
        today = ctx.now.astimezone(UTC).date()
        start, end = (await self._ranges(today))[name]
        async with ctx.sessionmaker() as session:
            creative_counts = await _creatives_per_account(session, ctx.dataset_id)
        reasons: list[str | None] = []
        failure: TransientError | None = None
        unknown_rows = 0
        for account_id in ctx.clients.linkedin_account_ids:
            expected = creative_counts.get(account_id)
            listing = await client.analytics_daily(
                account_id, (start, end), expected_creatives=expected or None
            )
            self._calls += listing.calls
            unknown_rows += await self._store_creative_daily(listing.rows)
            if listing.interrupted_by is not None and failure is None:
                failure = listing.interrupted_by
            if not listing.complete:
                reasons.append(listing.reason or "analytics listing incomplete")
        if failure is not None:
            self._interrupted(failure, dataset=name)
            return
        if unknown_rows:
            reasons.append(f"{_plural(unknown_rows, 'row', 'rows')} for unknown creatives")
        outcome = _success(ctx.now, covered=(start, end), reasons=reasons)
        async with ctx.sessionmaker() as session, session.begin():
            await self._complete(session, {name: outcome})

    async def _store_creative_daily(self, rows: Sequence[CreativeDailyRow]) -> int:
        """Upsert creative-day rows; return how many named a creative the dataset lacks."""
        ctx = self.ctx
        if not rows:
            return 0
        async with ctx.sessionmaker() as session, session.begin():
            creatives = _by_last_key(
                await repositories.entity_id_map(session, Creative, ctx.dataset_id)
            )
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
        self._count("linkedin.analytics_daily", written)
        unknown = len(rows) - len(facts)
        if unknown:
            _log.warning(
                "sync.metrics.rows_skipped",
                source="linkedin",
                rows=unknown,
                reason="creative not stored in the dataset",
            )
        return unknown


async def _creatives_per_account(session: AsyncSession, dataset_id: int) -> dict[str, int]:
    """Count a LinkedIn dataset's creatives per ad account external id (chunk sizing)."""
    result = await session.execute(
        select(AdAccount.external_id, Creative.id)
        .join(Campaign, Campaign.id == Creative.campaign_id)
        .join(AdAccount, AdAccount.id == Campaign.account_id)
        .where(Creative.dataset_id == dataset_id)
    )
    counts: dict[str, int] = {}
    for external_id, _ in result.all():
        counts[external_id] = counts.get(external_id, 0) + 1
    return counts


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
        return ("linkedin.segments",)

    async def _execute(self) -> None:
        """Fetch every (account, pivot, preset) window and store it."""
        ctx = self.ctx
        name = "linkedin.segments"
        client = ctx.clients.require_linkedin()
        presets = ctx.thresholds.presets
        async with ctx.sessionmaker() as session:
            end = await default_window_end(session, now=ctx.now, thresholds=ctx.thresholds)
        windows = [(end - timedelta(days=days - 1), end) for days in presets]
        reasons: list[str | None] = []
        failure: TransientError | None = None
        unknown_rows = 0
        for account_id in ctx.clients.linkedin_account_ids:
            for pivot in SEGMENT_PIVOTS:
                for window in windows:
                    listing = await client.analytics_segments(account_id, pivot, window)
                    self._calls += listing.calls
                    unknown_rows += await self._store(listing.rows, window)
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
        if failure is not None:
            self._interrupted(failure, dataset=name)
            return
        if unknown_rows:
            reasons.append(f"{_plural(unknown_rows, 'row', 'rows')} for unknown campaigns")
        shortfall = _join_reasons(reasons)
        recorded = segment_reason(presets, end)
        outcome = FreshnessOutcome.success(
            ctx.now,
            covered_from=end - timedelta(days=max(presets) - 1),
            covered_through=end,
            complete=shortfall is None,
            reason=recorded if shortfall is None else f"{recorded}{_REASON_SEPARATOR}{shortfall}",
        )
        async with ctx.sessionmaker() as session, session.begin():
            await self._complete(session, {name: outcome})

    async def _store(self, rows: Sequence[SegmentRow], window: tuple[date, date]) -> int:
        """Upsert one window's segment rows; return how many named an unknown campaign."""
        ctx = self.ctx
        if not rows:
            return 0
        start, end = window
        async with ctx.sessionmaker() as session, session.begin():
            campaigns = await _campaign_ids(session, ctx.dataset_id)
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
        self._count("linkedin.segments", written)
        unknown = len(rows) - len(facts)
        if unknown:
            _log.warning(
                "sync.segments.rows_skipped",
                source="linkedin",
                rows=unknown,
                reason="campaign not stored in the dataset",
            )
        return unknown


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
    same. A transient interruption fails the attempt the same way (``on_error``), keeping
    the cursor.
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
        return ("linkedin.leads",)

    async def _execute(self) -> None:
        """List, store and stage the range's leads (class docstring)."""
        ctx = self.ctx
        name = "linkedin.leads"
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
            staged, unplaced = await self._store_leads(session, reads)
            if failure is None:
                if unplaced:
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
                await self._complete(session, {name: outcome})
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
    ) -> tuple[int, int]:
        """Upsert the leads (stubbing unknown campaigns and creatives) and stage them.

        A lead whose campaign is not stored and whose ad account is not stored either (an
        account whose entity sync has not succeeded yet) cannot be placed: a campaign stub
        needs its account, and an account is never invented, as its currency is unknown.
        Such a lead is left unstored, and counted, for the caller to fail the attempt.

        Returns:
            The ``lead_match`` rows inserted across the CRM datasets staged in, and the
            number of leads left unstored for want of a campaign.
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
        self._count("linkedin.leads", len(stored))
        lead_ids = sorted(stored.values())
        reasons = {stored[external]: reason for external, reason in no_email.items()}
        staged = 0
        for crm_dataset_id in await self._staging_targets(session):
            staged += await lead_match.create_pending(
                session, lead_ids, crm_dataset_id, now=ctx.now, no_email_reasons=reasons
            )
        return staged, len(unplaced)

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
        if ctx.mode == "live":
            building = await session.scalars(
                select(SourceDataset.id)
                .where(
                    SourceDataset.source == "hubspot",
                    SourceDataset.role == "primary",
                    SourceDataset.state == "building",
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
                (LeadMatch.status == "pending")
                | (
                    LeadMatch.status.in_(("no_match", "error"))
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
) -> lead_match.MatchReport | None:
    """Attempt the due ``lead_match`` rows of the job's CRM dataset.

    Leads are re-fetched through a LinkedIn client of their own mode: simulated for a
    companion, whose rows are its parent's simulated leads, live for a primary, which only
    ever holds live leads (``sync/lead_match.py`` isolation rule). A live re-fetch needs
    LinkedIn credentials under ``DATA_MODE=auto``; without them no live lead can exist, and
    the pass is skipped.

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


class LeadMatchJob(_JobBase):
    """One matching pass over a CRM dataset's due ``lead_match`` rows.

    The context is the **CRM dataset's** (HubSpot, primary or companion): ``ctx.dataset_id``
    is the CRM dataset matched in, and ``ctx.clients`` its portal's client. Used on its own
    by a live LinkedIn backfill, so every row staged against HubSpot's active primary is
    attempted before the LinkedIn dataset activates; :class:`CrmSyncJob` runs the same pass
    as its matching step. ``AccessDenied`` from LinkedIn is handed to
    ``on_error("linkedin", …)``, from HubSpot to ``on_error("hubspot", …)``.
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
        """Run the pass and count the rows attempted."""
        hubspot = self.ctx.clients.require_hubspot()
        calls_before = hubspot.calls
        bindings = await _crm_bindings(self.ctx)
        props = hubspot.bind_contact_properties(bindings, self.ctx.settings)
        try:
            report = await _match_leads(
                self,
                lead_dataset_id=self.lead_dataset_id,
                lead_client=self.lead_client,
                props=props,
            )
        finally:
            self._calls += hubspot.calls - calls_before
        if report is not None:
            self._count(None, report.attempted)
            _log.info(
                "sync.lead_match.pass",
                crm_dataset_id=self.ctx.dataset_id,
                attempted=report.attempted,
                matched=report.matched,
                pending_left=report.pending_left,
            )


# ---------------------------------------------------------------------------------------------
# CRM (AAP 0.3.3 "Contact refresh", "Associations", "Deal search", "Google-to-CRM link")
# ---------------------------------------------------------------------------------------------


def _search_reason(outcome: SearchOutcome) -> str | None:
    """Map a partitioned search's shortfall onto the freshness reason vocabulary.

    A transient failure that stopped the search is repairable (prefixed
    :data:`~ads_engine.sync.freshness.REASON_INTERRUPTED`, so the next run widens back to the
    stored range); a one-hour partition above the 10,000-result ceiling is permanent
    (prefixed :data:`~ads_engine.sync.freshness.REASON_SEARCH_CEILING`); a short read keeps
    the search module's text.
    """
    if outcome.is_complete:
        return None
    interrupted: list[str] = []
    permanent: list[str] = []
    for part in (outcome.reason or "search incomplete").split(_REASON_SEPARATOR):
        if part == CEILING_REASON:
            permanent.append(f"{freshness.REASON_SEARCH_CEILING}: {part}")
        elif part == SHORT_READ_REASON:
            permanent.append(part)
        else:
            interrupted.append(f"{freshness.REASON_INTERRUPTED}: {part}")
    if outcome.partitions_incomplete:
        noun = _plural(len(outcome.partitions_incomplete), "partition", "partitions")
        permanent.append(f"{noun} not read in full")
    return _REASON_SEPARATOR.join((*interrupted, *permanent))


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


class CrmSyncJob(_JobBase):
    """The CRM sync of one HubSpot dataset, primary or companion, every 3 h.

    The context is the CRM dataset's: ``ctx.dataset_id`` is the HubSpot primary or a
    simulated LinkedIn dataset's companion. A companion is populated by exactly this job
    against the simulated HubSpot transport, so it writes its own freshness rows the same
    way. In order:

    1. **Google-touch discovery** (primary only, and only when the stored bindings verify
       both ``HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY`` and ``HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY``
       and the settings still name them): contacts whose touch date falls in the sync range
       and whose campaign property is set (``hubspot.google_contacts``). Without the
       configuration nothing is called and no row is written; Google items then carry
       ``no_crm_link`` and no LinkedIn-derived subset stands in.
    2. **Matching**: the due ``lead_match`` rows (:class:`LeadMatchJob`'s pass).
    3. **Contact refresh** by id of every matched contact, reading the email (hashed), the
       lifecycle stage, the bound MQL and QL date properties and the Google properties;
       ``lead_match.contact_read_status`` records ``ok`` or ``error`` per contact, and a
       failed refresh keeps the previous stage values (``hubspot.contacts``).
    4. **Deal search**: every deal created from the default window start (or the backfill
       start) to now, partitioned below the 10,000-result ceiling (``hubspot.deal_search``).
    5. **Associations**: contacts to meetings and deals for the matched and Google-touch
       contacts, deals to contacts for every searched deal, each input followed through its
       own cursor, then **target reads** of the associated meetings, deals and contacts. An
       associated but unread target is never treated as absent: the fetch's
       ``target_read_status`` and ``missing_target_ids`` say so (``hubspot.associations``).

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
        names = ("hubspot.contacts", "hubspot.associations", "hubspot.deal_search")
        return (*names, "hubspot.google_contacts") if self._discovery else names

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

            if (
                ctx.role == "primary"
                and props.google_campaign_property is not None
                and props.google_touch_property is not None
            ):
                self._discovery = True
                await self._open_runs(("hubspot.google_contacts",))
                await self._discover(
                    client, props, _widened(search_start, rows.get("hubspot.google_contacts"))
                )

            await self._match(props)
            refreshed = await self._refresh_contacts(client, props, rows, scope_start)
            deal_ids = await self._search_deals(
                client, _widened(search_start, rows.get("hubspot.deal_search"))
            )
            await self._associations(
                client, props, rows, scope_start, search_start, refreshed, deal_ids
            )
        finally:
            self._calls += client.calls - calls_before

    # -- steps -----------------------------------------------------------------------------

    async def _discover(
        self, client: HubSpotClient, props: ContactPropertyMap, start: datetime
    ) -> None:
        """Step 1: Google-touch contacts of the sync range (``hubspot.google_contacts``)."""
        ctx = self.ctx
        name = "hubspot.google_contacts"
        campaign_property = props.google_campaign_property
        touch_property = props.google_touch_property
        if campaign_property is None or touch_property is None:  # pragma: no cover - guarded
            return
        outcome = await client.search_partitioned(
            _CONTACTS,
            google_contacts_filter(campaign_property, touch_property, props.request_properties()),
            (start, ctx.now),
        )
        contacts = [parse_contact(record, props) for record in outcome.rows]
        reason = _search_reason(outcome)
        async with ctx.sessionmaker() as session, session.begin():
            stored = await repositories.upsert_entities(
                session,
                CrmContact,
                [_contact_values(row) for row in {row.hs_id: row for row in contacts}.values()],
                ctx.dataset_id,
            )
            self._count(name, len(stored))
            await self._complete(
                session,
                {
                    name: _success(
                        ctx.now,
                        covered=(start.astimezone(UTC).date(), ctx.now.astimezone(UTC).date()),
                        reasons=[reason],
                    )
                },
            )

    async def _match(self, props: ContactPropertyMap) -> None:
        """Step 2: the matching pass; a LinkedIn error is LinkedIn's and the sync goes on."""
        try:
            report = await _match_leads(self, lead_dataset_id=None, lead_client=None, props=props)
        except ConnectorError as exc:
            if exc.source != "linkedin":
                raise
            # Re-fetching leads failed on LinkedIn's side: LinkedIn's source mode decides
            # what follows; the CRM refresh of contacts already matched still runs.
            self._disposition = await self._hand_over(exc, ())
            return
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
                LeadMatch.status == "matched",
                SourceDataset.state != "retired",
            )
            .distinct()
        )
        return sorted(result)

    async def _refresh_contacts(
        self,
        client: HubSpotClient,
        props: ContactPropertyMap,
        rows: Mapping[str, DatasetFreshness],
        scope_start: datetime,
    ) -> ContactBatch:
        """Step 3: refresh every matched contact by id (``hubspot.contacts``)."""
        ctx = self.ctx
        name = "hubspot.contacts"
        async with ctx.sessionmaker() as session:
            ids = await self._matched_contacts(session)
        batch = (
            await client.contacts_by_id(ids, props)
            if ids
            else ContactBatch(records=MappingProxyType({}), read_status={})
        )
        failed = sorted(hs_id for hs_id, status in batch.read_status.items() if status != "ok")
        read = sorted(hs_id for hs_id, status in batch.read_status.items() if status == "ok")
        async with ctx.sessionmaker() as session, session.begin():
            stored = await repositories.upsert_entities(
                session,
                CrmContact,
                [_contact_values(batch.records[hs_id]) for hs_id in read if hs_id in batch.records],
                ctx.dataset_id,
            )
            self._count(name, len(stored))
            for status, hs_ids in (("ok", read), ("error", failed)):
                for chunk in _chunks(hs_ids):
                    await session.execute(
                        update(LeadMatch)
                        .where(
                            LeadMatch.crm_dataset_id == ctx.dataset_id,
                            LeadMatch.status == "matched",
                            LeadMatch.contact_id.in_(
                                select(CrmContact.id).where(
                                    CrmContact.dataset_id == ctx.dataset_id,
                                    CrmContact.hs_id.in_(chunk),
                                )
                            ),
                        )
                        .values(contact_read_status=status)
                    )
            reason = f"{_plural(len(failed), 'contact', 'contacts')} not read" if failed else None
            await self._complete(
                session,
                {
                    name: _success(
                        ctx.now,
                        covered=self._state_range(rows.get(name), scope_start),
                        reasons=[reason],
                    )
                },
            )
        return batch

    def _state_range(
        self, row: DatasetFreshness | None, scope_start: datetime
    ) -> tuple[date, date]:
        """The coverage of a current-state CRM read: the whole lead range up to today.

        Contacts and associations are re-read for every matched contact, whatever its lead's
        date, so each run re-covers the stored range and its completeness replaces the
        stored one (``record_freshness``): the range starts at the earlier of the stored
        start and the staging range's start (``BACKFILL_DAYS``, at most 180 days).
        """
        start = scope_start.astimezone(UTC).date()
        if row is not None and row.covered_from is not None:
            start = min(start, row.covered_from)
        return start, max(start, self.ctx.now.astimezone(UTC).date())

    async def _search_deals(self, client: HubSpotClient, start: datetime) -> list[str]:
        """Step 4: every deal created from ``start`` to now (``hubspot.deal_search``)."""
        ctx = self.ctx
        name = "hubspot.deal_search"
        outcome = await client.search_partitioned(_DEALS, deal_search_filter(), (start, ctx.now))
        deals = list(
            {row.hs_id: row for row in (parse_deal(record) for record in outcome.rows)}.values()
        )
        async with ctx.sessionmaker() as session, session.begin():
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
            self._count(name, len(stored))
            await self._complete(
                session,
                {
                    name: _success(
                        ctx.now,
                        covered=(start.astimezone(UTC).date(), ctx.now.astimezone(UTC).date()),
                        reasons=[_search_reason(outcome)],
                    )
                },
            )
        return sorted(row.hs_id for row in deals)

    async def _associations(
        self,
        client: HubSpotClient,
        props: ContactPropertyMap,
        rows: Mapping[str, DatasetFreshness],
        scope_start: datetime,
        search_start: datetime,
        refreshed: ContactBatch,
        deal_ids: Sequence[str],
    ) -> None:
        """Step 5: associations with their target reads (``hubspot.associations``)."""
        ctx = self.ctx
        name = "hubspot.associations"
        async with ctx.sessionmaker() as session:
            matched = await self._matched_contacts(session)
            touched = await session.scalars(
                select(CrmContact.hs_id).where(
                    CrmContact.dataset_id == ctx.dataset_id,
                    CrmContact.google_touch_at.is_not(None),
                    CrmContact.google_touch_at >= search_start,
                )
            )
            google = set(touched)
        contact_ids = sorted(set(matched) | google)
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
            hs_id for hs_id, status in refreshed.read_status.items() if status == "ok"
        } | google
        contact_targets = [
            target for target in deal_contacts_fetch.all_targets if target not in known_contacts
        ]
        contact_read = (
            await client.contacts_by_id(contact_targets, props)
            if contact_targets
            else ContactBatch(records=MappingProxyType({}), read_status={})
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
                hs_id for hs_id, status in contact_read.read_status.items() if status == "ok"
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
            self._count(name, len(meetings) + len(deals) + len(contacts_read))
            known: dict[str, set[str]] = {
                _MEETINGS: set(meeting_read.records),
                _DEALS: searched | set(deal_read.records),
                _CONTACTS: known_contacts | set(contacts_read),
            }
            unknown = 0
            for fetch in (meetings_fetch, deals_fetch, deal_contacts_fetch):
                unknown += await self._write_fetch(session, fetch, known[fetch.to_type])
            reason = (
                f"{_plural(unknown, 'association read', 'association reads')} unknown: the "
                "fetch or a target read failed"
                if unknown
                else None
            )
            await self._complete(
                session,
                {
                    name: _success(
                        ctx.now,
                        covered=self._state_range(rows.get(name), scope_start),
                        reasons=[reason],
                    )
                },
            )

    async def _fetch(
        self, client: HubSpotClient, from_type: str, to_type: str, ids: Sequence[str]
    ) -> AssociationFetch:
        """Read associations of ``ids``; an empty input makes no call."""
        if not ids:
            return AssociationFetch(from_type, to_type, MappingProxyType({}))
        return await client.associations(from_type, to_type, ids)

    async def _write_fetch(
        self, session: AsyncSession, fetch: AssociationFetch, known: set[str]
    ) -> int:
        """Store one association read and its fetch rows; return the inputs left unknown.

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
            if read.status in ("ok", "ok_empty")
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
            if read.status in ("ok", "partial")
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
            if read.status == "ok_empty":
                target_status = None
            elif not targets:
                target_status = "ok" if read.status == "ok" else None
            else:
                missing = sorted(targets - known)
                if not missing:
                    target_status = "ok"
                elif len(missing) == len(targets):
                    target_status = "error"
                else:
                    target_status = "partial"
            if read.status in ("partial", "error") or (
                read.status == "ok" and target_status != "ok"
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
        self._count("hubspot.associations", len(links) + len(fetch_rows))
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
        return unknown


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

    By the dataset's source (every job with ``active=False``, so no analysis request; the
    activation queues one):

    * **Google**: entities (the first entity sync writes the baseline), then metrics over
      ``BACKFILL_DAYS``.
    * **LinkedIn**: entities, metrics (capped at 180 days), segments and leads over the
      backfill range. Simulated: then the companion CRM dataset's sync with
      ``backfill=True``. Live: then one matching pass of this dataset's leads in HubSpot's
      active primary, so every staged row is attempted before the dataset activates, and on
      success a CRM sync of that primary, so the new matches' evidence is read too.
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
    if dataset.role != "primary":
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
        # Inactive: the CRM dataset is active, but this runs inside another source's build,
        # whose activation queues the analysis request (no request per backfill job).
        crm_ctx = _inactive(
            await JobContext.for_dataset(
                controller, crm_id, crm_clients, now=ctx.now, thresholds=ctx.thresholds
            )
        )
        matched = await LeadMatchJob(
            crm_ctx, lead_dataset_id=ctx.dataset_id, lead_client=clients.require_linkedin()
        ).run()
        if matched.status != "succeeded":
            return (matched,)
        return matched, await CrmSyncJob(crm_ctx).run()
