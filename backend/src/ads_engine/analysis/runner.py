"""The analysis runner: window inputs, run selection and ``run_analysis``.

AAP 0.5.1 ("Precedence, runner and lock": ``run_analysis(window, kind, trigger) ->
AnalysisRun``, persisting runs, recommendations, findings, withheld patterns and suppression
counts), 0.5.3 ("Where outputs are frozen", "Run selection and the analysis lock"), 0.3.2
(data flow; the analysis requests the worker coalesces), 0.5.2 (``analysis_run``,
``recommendation``, ``run_finding``, ``run_withheld``, ``run_suppression``), 0.1.1
(reproducibility; a custom 180-day run computes in under 30 s) and 0.11.1 (the run summary
log line).

Every analysis run, whoever starts it, goes through :func:`run_analysis`:

* the worker's scheduler and its startup serve the pending ``analysis_request`` backlog with
  ``run_analysis(await default_window(), "default", ..., serve_through=backlog.through_id)``,
  where ``backlog`` is :func:`pending_requests`: a bounded summary (count, oldest request,
  highest id, a few reasons) however long the backlog grew, so no caller ever holds one id
  per pending request;
* ``POST /runs`` and ``ads-engine analyze`` compute custom windows with
  ``run_analysis(await custom_window(start, end), "custom", "manual")``, a ``custom`` run even
  when the window equals the default.

One lock for every entry point: :func:`run_analysis` enters
:func:`~ads_engine.analysis.lock.analysis_lock` itself, so no caller takes it.
:class:`~ads_engine.analysis.lock.RunInProgress` propagates unchanged: the API answers 409
``run_in_progress``, the worker leaves its requests pending and the CLI exits with code 3.

Pipeline of one run (each step in its own short transaction where it touches the database):

1. Take the ``analysis`` lock; any ``running`` row left by a crashed process is marked
   ``failed`` by the lock itself.
2. Insert the ``running`` row and commit it. The partial unique index
   ``uq_analysis_run_single_running`` is the second, database-enforced guard; a violation is
   reported as :class:`~ads_engine.analysis.lock.RunInProgress`.
3. Claim the ``analysis_request`` rows the run serves and commit the claim: ``picked_at``
   (an existing one is kept) and this run's id on every still-unserved row with
   ``id <= serve_through`` (one set-based ``UPDATE``, no per-id parameter), or on the
   explicit ``request_ids`` (one array parameter, however many). The claim commits before
   the context snapshot opens, so every claimed request's change is visible to the load,
   and a request committed later stays pending for the next run. Behind another lock
   holder (step 1) nothing is claimed, so the requests stay pending (AAP 0.5.3).
4. Read the :class:`~ads_engine.analysis.context.AnalysisContext` through
   :func:`ads_engine.analysis.metrics.load_context` in one read-only ``REPEATABLE READ``
   transaction, so every query of the load sees the same snapshot even while a sync writes.
5. Check the white-paper creative overrides against the stored creatives
   (:meth:`~ads_engine.analysis.thresholds.Thresholds.check_overrides`).
6. Evaluate the rules in a fixed order (C0, keywords, campaigns, budget, creative, white
   papers) outside any transaction. Rules are pure; an exception in any of them fails the
   run, so a run is never partial.
7. Apply precedence (:func:`~ads_engine.analysis.precedence.apply_precedence`).
8. Persist every output in **one** transaction, never mutated afterwards: the ranked
   recommendations, findings, withheld patterns, positive suppression counts and the run's
   ``succeeded`` state with the dataset ids and modes it read. The run row is locked first
   and must still be ``running``: if the lock connection was lost and another process marked
   it ``failed``, nothing is written (:class:`RunNoLongerRunning`).
9. On any failure from the flush of the ``running`` row on (its commit and session close
   included) the run is marked ``failed`` in a fresh transaction, the failure is logged as
   ``analysis_run_failed``, and the exception is re-raised. Requests claimed in step 3 stay
   stamped with the failed run, so a deterministic failure (a contradicting configuration)
   does not restart every coalescing window; the next sync, decision or feedback queues a
   fresh request. A failure before the claim committed, the claim's own included, leaves
   every request pending.

Off the event loop. Steps 5 to 7 and the rendering of step 8's rows are synchronous and
CPU-bound (statistical tests, attribution, clustering). They run as one function on the
analysis thread, a single-worker pool created on first use, so the API process keeps
serving stored pages and lock conflicts while a custom run computes (AAP 0.1.1: stored
reads under 2 s). The thread runs in a copy of the caller's context variables, so the
bound log context stays on its lines, and only the thread touches the context while it
computes. The event loop keeps every database step (AAP 0.11.1 async I/O).

Cancellation never outlives the lock. A cancellation of :func:`run_analysis` while the
thread computes sets a stop flag, checked before the override check, before each rule
module, before precedence and before row rendering; the cancellation, repeated ones
included, is held until the thread has finished and then re-raised. The failure record of
step 9 runs to completion despite cancellation, bounded by a timeout, so a cancelled run's
row is ``failed`` before the lock is released.

What each stored recommendation carries (AAP 0.5.3 ``Item``, 0.5.4.8): its section, group,
rule id, rank, action, reason, data status, hold and required datasets as columns; its
entity as ``entity_type`` and the stable key ``entity_id`` (``EntityRef.key``) plus the typed
row keys (``campaign_id`` … ``creative_id``); and the frozen evidence, whose ``extra`` gains
two runner-owned keys: ``entity`` (``{source, type, id, name}``, the name the page shows)
and ``flag_messages`` (``{code: message}`` for the codes stored in ``flags``).

Boundaries: imports the analysis modules, ``db.session``, ``db.repositories``, the analysis
and accounts models and ``logging`` only; never ``sync``, ``connectors``, ``simulation`` or
``api``.

Example::

    async with session_scope() as session:
        backlog = await pending_requests(session)
    if backlog is not None:
        window = await default_window()
        try:
            run = await run_analysis(window, "default", "event", serve_through=backlog.through_id)
        except RunInProgress:
            ...  # nothing was claimed: the requests stay pending for the next tick
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal, get_args

from sqlalchemy import ARRAY, BigInteger, any_, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from ads_engine.analysis import (
    budget,
    campaigns,
    creative,
    discrepancy,
    keywords,
    metrics,
    precedence,
    white_papers,
)
from ads_engine.analysis.context import (
    ENTITY_AD_GROUP,
    ENTITY_BUDGET,
    ENTITY_CAMPAIGN,
    ENTITY_CREATIVE,
    ENTITY_CREATIVE_PAIR,
    ENTITY_KEYWORD,
    ENTITY_SEARCH_TERM,
    ENTITY_SEGMENT,
    ENTITY_TERM_CLUSTER,
    SECTION_ORDER,
    AnalysisContext,
    Candidate,
    EntityRef,
    Finding,
    RuleResult,
    Withheld,
)
from ads_engine.analysis.lock import RunInProgress, analysis_lock
from ads_engine.analysis.precedence import PrecedenceOutput, RankedItem
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.analysis.window import DAY_SOURCES, AnalysisWindow, DaySource, SourceDayInfo
from ads_engine.db.models.accounts import AdAccount, AdGroup, Budget, Campaign, Creative, Keyword
from ads_engine.db.models.analysis import (
    RUN_KINDS,
    RUN_TRIGGERS,
    AnalysisRequest,
    AnalysisRun,
    Recommendation,
    RunFinding,
    RunSuppression,
    RunWithheld,
)
from ads_engine.db.repositories import active_datasets
from ads_engine.db.session import run_to_completion, session_scope, use_session
from ads_engine.logging import get_logger, mark_logged

__all__ = [
    "DATASET_ID_KEYS",
    "EVALUATION_ORDER",
    "PENDING_REASONS_LIMIT",
    "RUNNER_EXTRA_KEYS",
    "PendingRequests",
    "RunKind",
    "RunNoLongerRunning",
    "RunTrigger",
    "custom_window",
    "default_window",
    "get_run",
    "latest_default_run",
    "list_runs",
    "pending_requests",
    "run_analysis",
    "window_inputs",
]

_log = get_logger(__name__)

RunKind = Literal["default", "custom"]
"""``default``: the worker's run of the configured default window; ``custom``: any run with an
explicit window, even one equal to the default (AAP 0.5.3 "Run kinds")."""

RunTrigger = Literal["scheduled", "event", "manual"]
"""What started a run: the schedule, a coalesced ``analysis_request`` event, or a user."""

# The Literal types above must name exactly the values the table's check constraints allow.
if get_args(RunKind) != RUN_KINDS or get_args(RunTrigger) != RUN_TRIGGERS:  # pragma: no cover
    raise RuntimeError("runner RunKind/RunTrigger disagree with db.models.analysis")

EVALUATION_ORDER: Final[tuple[str, ...]] = (
    "discrepancy",
    "keywords",
    "campaigns",
    "budget",
    "creative",
    "white_papers",
)
"""The rule modules in the fixed order :func:`run_analysis` evaluates them.

C0 (``discrepancy``) first, then the five sections in display order. Precedence is
order-independent, so the order only fixes which failing rule a failed run reports.
"""

_RULES: Final[tuple[tuple[str, Callable[[AnalysisContext], RuleResult]], ...]] = (
    ("discrepancy", discrepancy.evaluate),
    ("keywords", keywords.evaluate),
    ("campaigns", campaigns.evaluate),
    ("budget", budget.evaluate),
    ("creative", creative.evaluate),
    ("white_papers", white_papers.evaluate),
)
"""Each rule module's name and its ``evaluate`` callable, in :data:`EVALUATION_ORDER`.

The callables are bound here, at import, so replacing a module's ``evaluate`` afterwards
does not change what a run evaluates. :func:`_evaluate` reads this tuple at call time, so
replacing ``_RULES`` itself is the seam that substitutes an evaluator.
"""

DATASET_ID_KEYS: Final[tuple[str, ...]] = (
    "google",
    "linkedin",
    "hubspot",
    "hubspot_linkedin_effective",
)
"""The keys of ``analysis_run.dataset_ids``.

``google``, ``linkedin`` and ``hubspot`` are each source's active primary dataset as the run
read it; ``hubspot_linkedin_effective`` is the CRM dataset LinkedIn-attributed evidence read
(the companion while LinkedIn is simulated, else HubSpot's primary). A missing dataset is
``null``.
"""

_MODE_SOURCES: Final[tuple[Literal["google", "linkedin", "hubspot"], ...]] = (
    "google",
    "linkedin",
    "hubspot",
)

_EXTRA_ENTITY: Final = "entity"
_EXTRA_FLAG_MESSAGES: Final = "flag_messages"

RUNNER_EXTRA_KEYS: Final[frozenset[str]] = frozenset({_EXTRA_ENTITY, _EXTRA_FLAG_MESSAGES})
"""``evidence.extra`` keys the runner adds to every stored recommendation.

``entity`` is ``EntityRef.to_json()`` (``{source, type, id, name}``): ``recommendation`` has
no name column, and the page shows the entity's name. ``flag_messages`` is ``{code:
message}`` for every code in the ``flags`` column, in the same order. A rule that sets either
key itself breaks the contract, and the run fails rather than overwrite it.
"""

_SINGLE_RUNNING_INDEX: Final = "uq_analysis_run_single_running"
_REASON_LOG_LIMIT: Final = 500
_BIGINT_MAX: Final = 2**63 - 1

PENDING_REASONS_LIMIT: Final = 32
"""The most distinct reasons :func:`pending_requests` returns, whatever the backlog holds.

Reasons only describe a run in its log line, which keeps at most 500 characters of them, and
the events that enqueue requests name a few dozen distinct reasons at most; the cap keeps the
read bounded even when a backlog piles up after a long worker outage.
"""

_ANALYSIS_THREAD_PREFIX: Final = "ads-engine-analysis"
"""The name prefix of the thread the pure part of every run executes on."""

_FAILURE_RECORD_TIMEOUT_S: Final = 30.0
"""The bound, in seconds, on marking a failed run's row ``failed`` (:func:`_mark_failed`).

It bounds only that terminal-state write: requests are stamped only by the run's claim,
committed before its context is loaded (:func:`_claim_requests`), and failure handling never
touches ``analysis_request``.
"""

# The analysis thread pool: one worker, created on first use. The analysis lock admits one
# run per process at a time, and run_analysis waits for its computation to stop before the
# lock is released, so a second worker would never be busy.
_executor_lock: Final = threading.Lock()
_executor: ThreadPoolExecutor | None = None


class RunNoLongerRunning(RuntimeError):  # noqa: N818 - names the state, like RunInProgress
    """The run's row stopped being ``running`` before its outputs were written.

    Raised inside the persistence transaction, which therefore rolls back: the run's lock
    connection was lost, another process took the ``analysis`` lock and marked this row
    ``failed`` (AAP 0.5.3 crash recovery), so this process must not report it ``succeeded``.

    Attributes:
        run_id: The ``analysis_run`` id.
        status: The status found on the row, or ``None`` when the row no longer exists.
    """

    def __init__(self, run_id: int, status: str | None) -> None:
        found = "deleted" if status is None else f"'{status}'"
        super().__init__(
            f"analysis run {run_id} is {found}, no longer 'running'; its outputs were not stored"
        )
        self.run_id = run_id
        self.status = status


# ---------------------------------------------------------------------------
# Window inputs
# ---------------------------------------------------------------------------


def _aware(now: datetime | None, what: str) -> datetime:
    """Return ``now`` (or the current instant) as a tz-aware UTC datetime.

    Raises:
        TypeError: When ``now`` is not a ``datetime``.
        ValueError: When ``now`` is naive.
    """
    if now is None:
        return datetime.now(UTC)
    value: object = now
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be a tz-aware datetime, found {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be a tz-aware datetime (an instant), found a naive one")
    return value.astimezone(UTC)


async def window_inputs(
    session: AsyncSession, *, thresholds: Thresholds | None = None
) -> tuple[Thresholds, SourceDayInfo]:
    """Return the configuration and the sources' native day boundaries a window is built on.

    AAP 0.5.4.1: Google days end at midnight in the Google customer account's time zone,
    LinkedIn days are UTC. The Google zone is the ``ad_account.timezone`` of the active
    Google dataset (``None``, which reads as UTC, when Google has no active dataset or no
    account row with a zone yet). The day sources are those of ``google`` and ``linkedin``
    with an active primary dataset, in evidence order; with neither active, both, so a first
    start still renders both boundaries.

    Args:
        session: The caller's session; only read.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when ``None``.

    Returns:
        ``(thresholds, source day info)``.

    Raises:
        ConfigError: When the configuration fails to load.
        ValueError: When the stored Google time zone is not a loadable IANA zone.
    """
    config = thresholds if thresholds is not None else Thresholds.load()
    active = await active_datasets(session)
    day_sources: tuple[DaySource, ...] = tuple(
        source for source in DAY_SOURCES if source in active
    ) or tuple(DAY_SOURCES)
    google_timezone: str | None = None
    google = active.get("google")
    if google is not None:
        result = await session.execute(
            select(AdAccount.id, AdAccount.timezone)
            .where(
                AdAccount.dataset_id == google.id,
                AdAccount.source == "google",
                AdAccount.timezone.is_not(None),
            )
            .order_by(AdAccount.id)
        )
        zones = [str(zone).strip() for _, zone in result.all() if str(zone).strip()]
        if zones:
            # One Google customer per deployment (AAP 0.1.3); the lowest account id decides
            # should a dataset ever hold several, and the disagreement is logged.
            google_timezone = zones[0]
            if len(set(zones)) > 1:
                _log.warning(
                    "analysis_google_timezones_differ",
                    dataset_id=google.id,
                    timezones=sorted(set(zones)),
                    used=google_timezone,
                )
    return config, SourceDayInfo(google_timezone, day_sources)


async def default_window(
    *,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
    session: AsyncSession | None = None,
) -> AnalysisWindow:
    """Return the configured default window at ``now`` (AAP 0.5.4.1).

    ``window.default_days`` complete days ending on the earlier of yesterday in the Google
    account's time zone and yesterday UTC. The worker's scheduler passes it to
    :func:`run_analysis` with ``kind="default"``.

    Args:
        now: The current instant; ``datetime.now(UTC)`` when omitted. Must be tz-aware.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when ``None``.
        session: A session to read the active datasets through; a short read-only unit of
            work of its own when ``None``.

    Returns:
        The default window.

    Raises:
        ConfigError: When the configuration fails to load.
        TypeError: When ``now`` is not a ``datetime``.
        ValueError: When ``now`` is naive.
    """
    instant = _aware(now, "now")
    async with use_session(session) as unit:
        config, sources = await window_inputs(unit, thresholds=thresholds)
    return AnalysisWindow.default(config, sources, now=instant)


async def custom_window(
    start: date,
    end: date,
    *,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
    session: AsyncSession | None = None,
) -> AnalysisWindow:
    """Return a validated custom window (AAP 0.5.3 ``POST /runs``, ``ads-engine analyze``).

    Delegates to ``AnalysisWindow.custom``: the end is no later than the default window end
    at ``now``, and the span runs from 1 day to ``window.max_days``. The caller records the
    run as ``custom`` even when the window equals the default.

    Args:
        start: The first native date, inclusive.
        end: The last native date, inclusive.
        now: The current instant; ``datetime.now(UTC)`` when omitted. Must be tz-aware.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when ``None``.
        session: A session to read the active datasets through; a short read-only unit of
            work of its own when ``None``.

    Returns:
        The custom window.

    Raises:
        InvalidWindow: ``end_before_start``, ``end_after_default_end``, ``too_long`` or
            ``too_short``; the API maps it to 422 and the CLI to exit code 2.
        ConfigError: When the configuration fails to load.
        TypeError: When ``start`` or ``end`` is not a native ``date`` or ``now`` is not a
            ``datetime``.
        ValueError: When ``now`` is naive.
    """
    instant = _aware(now, "now")
    async with use_session(session) as unit:
        config, sources = await window_inputs(unit, thresholds=thresholds)
    return AnalysisWindow.custom(start, end, config=config, sources=sources, now=instant)


# ---------------------------------------------------------------------------
# Run selection (AAP 0.5.3 "Run selection")
# ---------------------------------------------------------------------------


async def latest_default_run(session: AsyncSession) -> AnalysisRun | None:
    """Return the latest succeeded ``default`` run, the one ``/runs/latest`` shows.

    Only ``kind='default' AND status='succeeded'`` runs qualify, newest ``finished_at`` first
    (ties by id), so a manual 90-day custom run never stands in for the default and a failed
    or running default run never replaces the last good one.

    Args:
        session: The caller's session.

    Returns:
        The run, or ``None`` before the first default run succeeds.
    """
    result = await session.execute(
        select(AnalysisRun)
        .where(AnalysisRun.kind == "default", AnalysisRun.status == "succeeded")
        .order_by(AnalysisRun.finished_at.desc().nulls_last(), AnalysisRun.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_run(session: AsyncSession, run_id: int) -> AnalysisRun | None:
    """Return run ``run_id`` in any status, or ``None`` when it does not exist.

    Args:
        session: The caller's session.
        run_id: The ``analysis_run`` id.

    Raises:
        TypeError: When ``run_id`` is not an ``int``.
    """
    value: object = run_id
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"run_id must be an int, found {value!r}")
    return await session.get(AnalysisRun, run_id)


async def list_runs(session: AsyncSession, *, kind: str, limit: int) -> list[AnalysisRun]:
    """Return the newest runs of ``kind``, in every status, newest first (``GET /runs``).

    Newest is the highest id: ids follow insertion, while ``started_at`` is the run's
    logical instant, which a caller may set (``now``). ``ix_analysis_run_kind_id`` on
    (``kind``, ``id DESC``) serves this read as one index range in the returned order, with
    no sort, so sparse ``custom`` runs among many ``default`` runs cost only ``limit`` rows.
    ``kind`` is rendered inline, so a server-side prepared statement's generic plan is also
    costed on that kind's own statistics and keeps the index range; with ``kind = $1`` a
    generic plan assumes half the runs match and walks the primary key backwards through
    every newer ``default`` run instead.

    Args:
        session: The caller's session.
        kind: ``default`` or ``custom``.
        limit: How many runs to return, at least 1.

    Returns:
        Up to ``limit`` runs.

    Raises:
        ValueError: For an unknown ``kind`` or a ``limit`` below 1.
        TypeError: When ``limit`` is not an ``int``.
    """
    if kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {RUN_KINDS}, found {kind!r}")
    count: object = limit
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError(f"limit must be an int, found {count!r}")
    if count < 1:
        raise ValueError(f"limit must be at least 1, found {count}")
    result = await session.execute(
        select(AnalysisRun)
        # Inline only a value already checked against RUN_KINDS; the limit stays bound.
        .where(AnalysisRun.kind == literal(kind, literal_execute=True))
        .order_by(AnalysisRun.id.desc())
        .limit(count)
    )
    return list(result.scalars())


# ---------------------------------------------------------------------------
# The pending analysis-request backlog (AAP 0.3.2 "coalesced within 60 s", 0.5.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingRequests:
    """A bounded summary of the pending ``analysis_request`` backlog (``picked_at IS NULL``).

    The one backlog reader of the scheduler's coalescing step and the worker's startup run:
    its size never depends on how many requests are pending, and a default run serves them
    all through ``run_analysis(..., serve_through=through_id)``.

    Attributes:
        count: How many requests are pending.
        oldest_requested_at: The earliest pending ``requested_at``; the scheduler waits until
            it is 60 s old before it starts the run (AAP 0.3.2).
        through_id: The highest pending id when the backlog was read, the run's
            ``serve_through`` watermark.
        reasons: Up to :data:`PENDING_REASONS_LIMIT` distinct reasons of requests up to
            ``through_id``, in first-queued order, for the run's log line.
    """

    count: int
    oldest_requested_at: datetime
    through_id: int
    reasons: tuple[str, ...]


async def pending_requests(session: AsyncSession) -> PendingRequests | None:
    """Summarise the pending ``analysis_request`` backlog in two bounded reads.

    One aggregate ``SELECT count(*), min(requested_at), max(id)`` over the pending rows, then
    the distinct reasons up to that highest id, ordered by the first request carrying each
    and limited to :data:`PENDING_REASONS_LIMIT`. Memory and bind parameters stay constant
    however many requests are pending.

    Args:
        session: The caller's session; only read.

    Returns:
        The backlog, or ``None`` when no request is pending.
    """
    pending = AnalysisRequest.picked_at.is_(None)
    count, oldest, through = (
        await session.execute(
            select(
                func.count(),
                func.min(AnalysisRequest.requested_at),
                func.max(AnalysisRequest.id),
            ).where(pending)
        )
    ).one()
    if not count or oldest is None or through is None:
        return None
    reasons = await session.scalars(
        select(AnalysisRequest.reason)
        .where(pending, AnalysisRequest.id <= through)
        .group_by(AnalysisRequest.reason)
        .order_by(func.min(AnalysisRequest.id))
        .limit(PENDING_REASONS_LIMIT)
    )
    return PendingRequests(
        count=int(count),
        oldest_requested_at=oldest,
        through_id=int(through),
        reasons=tuple(str(reason) for reason in reasons),
    )


# ---------------------------------------------------------------------------
# Typed entity keys of run-output rows (AAP 0.5.2 "Keys from run tables to entities")
# ---------------------------------------------------------------------------

_KEY_MODELS: Final[tuple[tuple[str, InstrumentedAttribute[int]], ...]] = (
    ("campaign_id", Campaign.id),
    ("budget_id", Budget.id),
    ("ad_group_id", AdGroup.id),
    ("keyword_id", Keyword.id),
    ("creative_id", Creative.id),
)
"""Each typed key column of the run-output tables and the primary key it references."""

_KEY_COLUMNS: Final[tuple[str, ...]] = tuple(column for column, _ in _KEY_MODELS)


@dataclass(frozen=True, slots=True)
class _TypedKeys:
    """The nullable ``ON DELETE SET NULL`` entity keys of one run-output row."""

    campaign_id: int | None = None
    budget_id: int | None = None
    ad_group_id: int | None = None
    keyword_id: int | None = None
    creative_id: int | None = None

    def row(self, known: Mapping[str, frozenset[int]]) -> dict[str, int | None]:
        """Return the columns, with every id that no stored row carries set to ``None``."""
        values: dict[str, int | None] = {}
        for column in _KEY_COLUMNS:
            value: int | None = getattr(self, column)
            values[column] = value if value is not None and value in known[column] else None
        return values

    def ids(self) -> Iterable[tuple[str, int]]:
        """Yield ``(column, id)`` for every set key."""
        for column in _KEY_COLUMNS:
            value: int | None = getattr(self, column)
            if value is not None:
                yield column, value


_NO_KEYS: Final = _TypedKeys()


def _first_part(external_id: str) -> str:
    """The leading ``|``-separated part of a composite entity key."""
    return external_id.split("|", 1)[0]


def _only(ids: Iterable[int]) -> int | None:
    """The single id of ``ids``, or ``None`` when there are none or several (ambiguous)."""
    distinct = set(ids)
    return next(iter(distinct)) if len(distinct) == 1 else None


@dataclass(frozen=True, slots=True)
class _KeyIndex:
    """Lookups from platform ids to internal row ids, built once per run from the context.

    Attributes:
        ad_groups: Google ad group external id → ad group ids.
        campaigns: ``(source, campaign external id)`` → campaign ids.
        creatives: LinkedIn creative URN → creative ids.
    """

    ad_groups: Mapping[str, tuple[int, ...]]
    campaigns: Mapping[tuple[str, str], tuple[int, ...]]
    creatives: Mapping[str, tuple[int, ...]]

    @classmethod
    def of(cls, ctx: AnalysisContext) -> _KeyIndex:
        """Index the context's ad groups, campaigns and creatives by their platform ids."""
        ad_groups: dict[str, list[int]] = {}
        for ad_group in ctx.ad_groups.values():
            ad_groups.setdefault(ad_group.external_id, []).append(ad_group.id)
        campaign_ids: dict[tuple[str, str], list[int]] = {}
        for campaign in ctx.campaigns.values():
            campaign_ids.setdefault((campaign.source, campaign.external_id), []).append(campaign.id)
        creative_ids: dict[str, list[int]] = {}
        for item in ctx.creatives.values():
            creative_ids.setdefault(item.urn, []).append(item.id)
        return cls(
            MappingProxyType({key: tuple(ids) for key, ids in ad_groups.items()}),
            MappingProxyType({key: tuple(ids) for key, ids in campaign_ids.items()}),
            MappingProxyType({key: tuple(ids) for key, ids in creative_ids.items()}),
        )


def _cluster_campaign(extra: Mapping[str, Any] | None) -> int | None:
    """A C3b term cluster's campaign: its ``evidence.extra.campaign_ids`` when exactly one."""
    if extra is None:
        return None
    found: object = extra.get("campaign_ids")
    if not isinstance(found, Sequence) or isinstance(found, str):
        return None
    ids = [value for value in found if isinstance(value, int) and not isinstance(value, bool)]
    return _only(ids) if len(ids) == len(found) else None


def _typed_keys(
    ctx: AnalysisContext,
    index: _KeyIndex,
    ref: EntityRef | None,
    extra: Mapping[str, Any] | None = None,
) -> _TypedKeys:
    """Return the typed row keys of the entity a run-output row is about.

    A campaign fills ``campaign_id``; a budget ``budget_id``; an ad group ``ad_group_id`` and
    its campaign; a keyword ``keyword_id`` with its ad group and campaign; a search term its
    ad group and campaign (from the ad group part of its composite key); a creative
    ``creative_id`` and its campaign; a demographic segment, a CR1 creative pair and a C3b
    term cluster their campaign where exactly one is known; a white paper, an account or a
    row without an entity none. A composite key whose platform id matches several rows
    leaves the key ``None`` rather than guess.
    """
    if ref is None:
        return _NO_KEYS
    kind = ref.type
    if kind == ENTITY_CAMPAIGN:
        if ref.id is not None:
            return _TypedKeys(campaign_id=ref.id)
        return _TypedKeys(campaign_id=_only(index.campaigns.get((ref.source, ref.external_id), ())))
    if kind == ENTITY_BUDGET:
        return _TypedKeys(budget_id=ref.id)
    if kind == ENTITY_AD_GROUP:
        ad_group_id = (
            ref.id if ref.id is not None else _only(index.ad_groups.get(ref.external_id, ()))
        )
        ad_group = None if ad_group_id is None else ctx.ad_groups.get(ad_group_id)
        return _TypedKeys(
            campaign_id=None if ad_group is None else ad_group.campaign_id,
            ad_group_id=ad_group_id,
        )
    if kind == ENTITY_KEYWORD:
        keyword = None if ref.id is None else ctx.keywords.get(ref.id)
        if keyword is not None:
            return _TypedKeys(
                campaign_id=keyword.campaign_id,
                ad_group_id=keyword.ad_group_id,
                keyword_id=keyword.id,
            )
        ad_group_id = _only(index.ad_groups.get(_first_part(ref.external_id), ()))
        ad_group = None if ad_group_id is None else ctx.ad_groups.get(ad_group_id)
        return _TypedKeys(
            campaign_id=None if ad_group is None else ad_group.campaign_id,
            ad_group_id=ad_group_id,
            keyword_id=ref.id,
        )
    if kind == ENTITY_SEARCH_TERM:
        ad_group_id = _only(index.ad_groups.get(_first_part(ref.external_id), ()))
        ad_group = None if ad_group_id is None else ctx.ad_groups.get(ad_group_id)
        return _TypedKeys(
            campaign_id=None if ad_group is None else ad_group.campaign_id,
            ad_group_id=ad_group_id,
        )
    if kind == ENTITY_CREATIVE:
        # A creative's external id is its URN (EntityRef contract).
        creative_id = (
            ref.id if ref.id is not None else _only(index.creatives.get(ref.external_id, ()))
        )
        found = None if creative_id is None else ctx.creatives.get(creative_id)
        return _TypedKeys(
            campaign_id=None if found is None else found.campaign_id,
            creative_id=creative_id,
        )
    if kind == ENTITY_SEGMENT:
        key = (ref.source, _first_part(ref.external_id))
        return _TypedKeys(campaign_id=_only(index.campaigns.get(key, ())))
    if kind == ENTITY_CREATIVE_PAIR:
        owners = [
            ctx.creatives[creative_id].campaign_id
            for urn in ref.external_id.split("|")
            for creative_id in index.creatives.get(urn, ())
        ]
        return _TypedKeys(campaign_id=_only(owners))
    if kind == ENTITY_TERM_CLUSTER:
        return _TypedKeys(campaign_id=_cluster_campaign(extra))
    return _NO_KEYS


async def _known_ids(
    session: AsyncSession, keys: Iterable[_TypedKeys]
) -> dict[str, frozenset[int]]:
    """Return, per typed key column, the ids among ``keys`` that a stored row carries.

    The typed keys are foreign keys. Every id normally comes from the context, which read
    those rows from the active datasets, but a C0 item keeps a discrepancy's subject id even
    when the context lacks that row. Checking existence in the persisting transaction makes
    a missing row leave its key ``NULL`` (the evidence keeps the entity) instead of failing
    the whole run on a foreign-key violation. Each column's distinct ids travel as one
    PostgreSQL array parameter (``id = ANY(:ids)``), so any number of them stays within the
    bind-parameter limit.
    """
    wanted: dict[str, set[int]] = {column: set() for column in _KEY_COLUMNS}
    for item in keys:
        for column, value in item.ids():
            wanted[column].add(value)
    known: dict[str, frozenset[int]] = {}
    for column, target in _KEY_MODELS:
        ids = sorted(wanted[column])
        if not ids:
            known[column] = frozenset()
            continue
        result = await session.execute(
            select(target).where(target == any_(literal(ids, ARRAY(BigInteger()))))
        )
        known[column] = frozenset(int(value) for value in result.scalars())
        missing = len(ids) - len(known[column])
        if missing:
            _log.warning("analysis_run_entity_keys_unmatched", column=column, count=missing)
    return known


# ---------------------------------------------------------------------------
# Rows of the frozen run outputs (AAP 0.5.3 "Where outputs are frozen")
# ---------------------------------------------------------------------------


def _evidence_json(candidate: Candidate) -> dict[str, Any]:
    """Return the candidate's frozen evidence with the runner-owned ``extra`` keys added.

    ``extra.entity`` keeps the entity's display name, which ``recommendation`` has no column
    for, and ``extra.flag_messages`` the message of every stored flag code. Everything else
    is :meth:`~ads_engine.analysis.context.Evidence.to_json` unchanged, so the provenance,
    window, thresholds and ``extra.depends_on`` stay exactly as computed.

    Raises:
        ValueError: When the rule already set a runner-owned key.
    """
    clash = sorted(RUNNER_EXTRA_KEYS & set(candidate.evidence.extra))
    if clash:
        raise ValueError(
            f"{candidate.rule_id} evidence.extra sets the runner-owned keys {clash}; "
            "rules must not write them"
        )
    rendered = candidate.evidence.to_json()
    extra: dict[str, Any] = dict(rendered.get("extra") or {})
    extra[_EXTRA_ENTITY] = candidate.entity_ref.to_json()
    extra[_EXTRA_FLAG_MESSAGES] = {flag.code: flag.message for flag in candidate.flags}
    rendered["extra"] = extra
    return rendered


def _recommendation_row(run_id: int, item: RankedItem) -> dict[str, Any]:
    """Return the ``recommendation`` row of one ranked item, without its typed entity keys.

    Raises:
        ValueError: When precedence left the item without a data status, or the rule set a
            runner-owned evidence key.
    """
    candidate = item.candidate
    if candidate.data_status is None:
        raise ValueError(
            f"{candidate.rule_id} item {candidate.entity_ref.key} has no data_status; "
            "apply_precedence sets it on every item"
        )
    subject = candidate.decision_subject
    return {
        "run_id": run_id,
        "section": candidate.section,
        "group": candidate.group,
        "rule_id": candidate.rule_id,
        "entity_type": candidate.entity_ref.type,
        "entity_id": candidate.entity_ref.key,
        "rank": item.rank,
        "action": candidate.action,
        "reason": candidate.reason,
        "evidence": _evidence_json(candidate),
        "flags": list(candidate.flag_codes),
        "data_status": candidate.data_status,
        "held": candidate.held,
        "held_reason": candidate.held_reason,
        "required_datasets": list(candidate.required_datasets),
        "decision_subject": None if subject is None else subject.to_json(),
    }


def _finding_row(run_id: int, section: str, finding: Finding) -> dict[str, Any]:
    """Return the ``run_finding`` row of one finding, without its typed entity keys."""
    return {
        "run_id": run_id,
        "section": section,
        "code": finding.code,
        "entity": None if finding.entity is None else finding.entity.to_json(),
        "message": finding.message,
        "evidence": finding.to_json()["evidence"],
    }


def _sample_size(value: Decimal | int | None) -> Decimal | None:
    """A withheld sample size as the ``numeric`` column stores it."""
    return None if value is None else Decimal(value)


def _withheld_row(run_id: int, section: str, withheld: Withheld) -> dict[str, Any]:
    """Return the ``run_withheld`` row of one withheld pattern, without its typed entity keys."""
    return {
        "run_id": run_id,
        "section": section,
        "rule_id": withheld.rule_id,
        "entity": None if withheld.entity is None else withheld.entity.to_json(),
        "reason": withheld.reason,
        "observed": _sample_size(withheld.observed),
        "required": _sample_size(withheld.required),
        "unit": withheld.unit,
    }


def _suppression_rows(run_id: int, out: PrecedenceOutput) -> list[dict[str, Any]]:
    """Return one ``run_suppression`` row per (section, reason) with a positive count."""
    rows: list[dict[str, Any]] = []
    for section in SECTION_ORDER:
        for reason, count in sorted(out.suppressed[section].items()):
            if count > 0:
                rows.append(
                    {"run_id": run_id, "section": section, "reason": reason, "count": count}
                )
    return rows


def _run_datasets(ctx: AnalysisContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(data_modes, dataset_ids)``: the datasets the run actually read.

    Taken from the context rather than re-read, so the run records exactly what its evidence
    was computed from even if a dataset activated meanwhile (AAP 0.3.2 provenance).
    """
    modes: dict[str, Any] = {}
    ids: dict[str, Any] = {}
    for source in _MODE_SOURCES:
        ref = ctx.datasets.get(source)
        modes[source] = None if ref is None else ref.mode
        ids[source] = None if ref is None else ref.dataset_id
    crm = ctx.linkedin_crm
    ids["hubspot_linkedin_effective"] = None if crm is None else crm.dataset.dataset_id
    return modes, ids


_KeyedRow = tuple[dict[str, Any], _TypedKeys]
"""A run-output row without its typed entity keys, and the keys it is about."""


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    """Every output row of one run, rendered on the analysis thread before the write.

    Built from the context and the precedence output, so that :func:`_persist` never reads
    either: the event loop only adds the typed entity keys a stored row carries
    (:func:`_known_ids`) and inserts. Rows keep section and rank order.

    Attributes:
        recommendations: The ``recommendation`` rows, in ``PrecedenceOutput.all_items`` order.
        findings: The ``run_finding`` rows, in section order.
        withheld: The ``run_withheld`` rows, in section order.
        suppression: The ``run_suppression`` rows with a positive count.
        data_modes: ``analysis_run.data_modes``, as the context read them.
        dataset_ids: ``analysis_run.dataset_ids``, as the context read them.
        summary: The per-section counts of the ``analysis_run_summary`` line.
    """

    recommendations: tuple[_KeyedRow, ...]
    findings: tuple[_KeyedRow, ...]
    withheld: tuple[_KeyedRow, ...]
    suppression: tuple[dict[str, Any], ...]
    data_modes: dict[str, Any]
    dataset_ids: dict[str, Any]
    summary: dict[str, Any]

    @classmethod
    def of(cls, run_id: int, ctx: AnalysisContext, out: PrecedenceOutput) -> _PreparedRun:
        """Render every output row of run ``run_id`` and resolve its typed entity keys.

        Raises:
            ValueError: When an item breaks the persistence contract (no data status, or a
                rule set a runner-owned evidence key).
        """
        index = _KeyIndex.of(ctx)
        data_modes, dataset_ids = _run_datasets(ctx)
        return cls(
            recommendations=tuple(
                (
                    _recommendation_row(run_id, item),
                    _typed_keys(
                        ctx, index, item.candidate.entity_ref, item.candidate.evidence.extra
                    ),
                )
                for item in out.all_items()
            ),
            findings=tuple(
                (
                    _finding_row(run_id, section, finding),
                    _typed_keys(ctx, index, finding.entity),
                )
                for section in SECTION_ORDER
                for finding in out.findings[section]
            ),
            withheld=tuple(
                (
                    _withheld_row(run_id, section, withheld),
                    _typed_keys(ctx, index, withheld.entity),
                )
                for section in SECTION_ORDER
                for withheld in out.withheld[section]
            ),
            suppression=tuple(_suppression_rows(run_id, out)),
            data_modes=data_modes,
            dataset_ids=dataset_ids,
            summary=_summary(out),
        )

    def keys(self) -> Iterable[_TypedKeys]:
        """Every row's typed keys."""
        for rows in (self.recommendations, self.findings, self.withheld):
            for _, keys in rows:
                yield keys


def _keyed(rows: Iterable[_KeyedRow], known: Mapping[str, frozenset[int]]) -> list[dict[str, Any]]:
    """Return ``rows`` with their typed entity keys, each id no stored row carries ``None``."""
    return [{**row, **keys.row(known)} for row, keys in rows]


async def _claim_requests(
    run_id: int, request_ids: Sequence[int], serve_through: int | None
) -> int:
    """Stamp ``picked_at`` and ``run_id`` on the requests the run serves; commit at once.

    Called after the ``running`` row is committed and before the context is loaded, so every
    claimed request's change is in the run's snapshot and a request committed later stays
    pending for the next run. With ``serve_through`` one set-based ``UPDATE`` claims every
    unserved pending row with ``id <= serve_through``, with no per-id parameter and no
    ``RETURNING``. With ``request_ids`` the ids travel as one PostgreSQL array parameter
    (``id = ANY(:ids)``), so any number of them stays within the bind-parameter limit. A
    request already carrying a ``run_id`` was served by another run and is left alone; an
    existing ``picked_at`` is kept.

    Args:
        run_id: The ``analysis_run`` id the requests are stamped with.
        request_ids: Validated, distinct explicit ids; empty with ``serve_through``.
        serve_through: The validated id watermark, or ``None``.

    Returns:
        How many requests were claimed.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: When the update fails; the claim rolls back and every
            request stays pending.
    """
    if serve_through is None and not request_ids:
        return 0
    picked_at = datetime.now(UTC)
    claim = (
        update(AnalysisRequest)
        .where(AnalysisRequest.run_id.is_(None))
        .values(picked_at=func.coalesce(AnalysisRequest.picked_at, picked_at), run_id=run_id)
    )
    if serve_through is not None:
        claim = claim.where(
            AnalysisRequest.picked_at.is_(None), AnalysisRequest.id <= serve_through
        )
    else:
        claim = claim.where(
            AnalysisRequest.id == any_(literal(sorted(request_ids), ARRAY(BigInteger())))
        )
    async with session_scope() as session:
        connection = await session.connection()
        claimed = (await connection.execute(claim)).rowcount
    if serve_through is None and claimed != len(request_ids):
        _log.warning(
            "analysis_requests_not_served",
            run_id=run_id,
            requested=len(request_ids),
            served=claimed,
        )
    return claimed


async def _persist(run_id: int, prepared: _PreparedRun) -> AnalysisRun:
    """Write every output of the run and mark it ``succeeded``, in one transaction.

    The run row is locked first (``SELECT … FOR UPDATE``) and must still be ``running``: a
    process that took the ``analysis`` lock after this one lost its lock connection may have
    marked it ``failed``, and then nothing is written. The rows were rendered on the analysis
    thread (:class:`_PreparedRun`); here they only gain the typed entity keys a stored row
    carries and are inserted in section and rank order with the ORM's bulk ``INSERT``;
    outputs are never updated afterwards. The requests the run serves were already claimed
    before its context was loaded (:func:`_claim_requests`), so nothing here touches
    ``analysis_request``.

    Returns:
        The persisted run, ``succeeded``, readable after the session closes.

    Raises:
        RunNoLongerRunning: When the row is no longer ``running``; the transaction rolls back.
        sqlalchemy.exc.SQLAlchemyError: When a statement fails; the transaction rolls back.
    """
    async with session_scope() as session:
        run = (
            await session.execute(
                select(AnalysisRun).where(AnalysisRun.id == run_id).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status != "running":
            raise RunNoLongerRunning(run_id, None if run is None else run.status)
        known = await _known_ids(session, prepared.keys())
        for model, rows in (
            (Recommendation, _keyed(prepared.recommendations, known)),
            (RunFinding, _keyed(prepared.findings, known)),
            (RunWithheld, _keyed(prepared.withheld, known)),
            (RunSuppression, list(prepared.suppression)),
        ):
            if rows:
                await session.execute(insert(model), rows)
        finished = datetime.now(UTC)
        run.status = "succeeded"
        run.finished_at = finished
        run.data_modes = prepared.data_modes
        run.dataset_ids = prepared.dataset_ids
    return run


async def _mark_failed(run_id: int, failed_at: datetime) -> None:
    """Mark the run ``failed`` in one bounded transaction.

    :func:`_record_failure` runs this to completion whatever cancels the run, so
    :data:`_FAILURE_RECORD_TIMEOUT_S` bounds it instead of a cancellation. The update is
    conditioned on the row still being ``running``, so a row another process already marked
    ``failed`` keeps its state. ``analysis_request`` is not touched: the requests the run
    claimed (:func:`_claim_requests`) stay stamped with it, and a run that failed before its
    claim committed (its ``running`` row's interrupted commit included, when no row exists
    to update) claimed nothing, so its requests stay pending for the next run.

    A database error or the timeout is logged as ``analysis_run_mark_failed_error`` and never
    raised, so the run's own exception reaches the caller. The transaction then rolls back,
    and a row left ``running`` is marked ``failed`` by the next process to take the
    ``analysis`` lock (AAP 0.5.3 crash recovery).
    """
    try:
        async with asyncio.timeout(_FAILURE_RECORD_TIMEOUT_S), session_scope() as session:
            marked = await session.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == run_id, AnalysisRun.status == "running")
                .values(status="failed", finished_at=failed_at)
                .returning(AnalysisRun.id)
                .execution_options(synchronize_session=False)
            )
            if marked.scalar_one_or_none() is None:
                found = await session.scalar(select(AnalysisRun.id).where(AnalysisRun.id == run_id))
                if found is None:
                    _log.warning("analysis_run_row_missing", run_id=run_id)
    except (SQLAlchemyError, TimeoutError) as db_exc:
        _log.exception("analysis_run_mark_failed_error", run_id=run_id, error=type(db_exc).__name__)


async def _record_failure(run_id: int, exc: BaseException, stage: str, started: float) -> None:
    """Mark the run ``failed`` in a fresh transaction and log ``analysis_run_failed``.

    The terminal state is written by :func:`_mark_failed` through
    :func:`~ads_engine.db.session.run_to_completion`, so a cancellation arriving meanwhile,
    repeated or level-triggered, waits until the row is ``failed``. :func:`run_analysis`
    calls this inside the ``analysis`` lock, so the terminal state is persisted before the
    lock is released. ``analysis_request`` is not touched: requests the run claimed before
    its context was loaded stay stamped as picked by this failed run, because leaving them
    pending would make the scheduler start a new failing run every coalescing window for a
    deterministic failure (a contradicting configuration), while the next sync, decision or
    feedback enqueues a fresh request. A run that failed before its claim committed, the
    claim's own failure included, left every request pending.

    A database error or timeout while recording the failure is logged, never raised, so the
    run's own exception always reaches the caller, which re-raises it. A row the failed write
    leaves ``running`` is marked ``failed`` by the next process to take the ``analysis`` lock
    (AAP 0.5.3 crash recovery).

    ``analysis_run_failed`` is the failure's one record with its traceback: the exception is
    marked with :func:`~ads_engine.logging.mark_logged`, and the callers that receive it
    (the API, the scheduler, the worker, the CLI) read
    :func:`~ads_engine.logging.logged_as` and log their own outcome without a second
    traceback. Cancellation is logged as a warning without one, and is not marked.

    Raises:
        asyncio.CancelledError: When the caller was cancelled while the failure was being
            recorded and ``exc`` is an ordinary error (an ``Exception``); ``exc`` is its
            context. The cancellation is raised once the failure is recorded and logged. A
            cancellation, interrupt or exit in ``exc`` is left for the caller to re-raise.
    """
    failed_at = datetime.now(UTC)
    interrupted: asyncio.CancelledError | None = None
    try:
        await run_to_completion(_mark_failed(run_id, failed_at))
    except asyncio.CancelledError as cancel:
        interrupted = cancel
    fields: dict[str, Any] = {
        "run_id": run_id,
        "stage": stage,
        "error": type(exc).__name__,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    if isinstance(exc, ConfigError):
        fields["config_file"] = exc.file
        fields["config_key"] = exc.key
    if isinstance(exc, Exception):
        _log.error("analysis_run_failed", exc_info=exc, **fields)
        mark_logged(exc, "analysis_run_failed")
    else:
        # Cancellation or interpreter exit: no traceback, the caller is shutting down.
        _log.warning("analysis_run_failed", **fields)
    if interrupted is not None and isinstance(exc, Exception):
        # The caller was cancelled meanwhile: propagate that, rather than lose it.
        raise interrupted


# ---------------------------------------------------------------------------
# run_analysis (AAP 0.5.1, 0.5.3)
# ---------------------------------------------------------------------------


def _constraint_name(exc: IntegrityError) -> str | None:
    """The name of the constraint or index a PostgreSQL integrity error names, if any."""
    diag = getattr(exc.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


async def _insert_running(
    window: AnalysisWindow,
    kind: RunKind,
    trigger: RunTrigger,
    config: Thresholds,
    instant: datetime,
    progress: _RunProgress,
) -> AnalysisRun:
    """Insert the run's ``running`` row and commit it at once.

    The row records the window's ``default_days`` and ``trusted_min_days``, the values the
    P5 short-window flag judges this window against, so the run's metadata and banner stay
    frozen with the run whatever the configuration later says.

    The row is flushed first and its id recorded in ``progress.run_id`` before the commit.
    A failure or cancellation from then on, in the commit, while the session closes or at
    any later step, therefore reaches the failure handling of :func:`run_analysis` with the
    id of a row that may already be committed. When the commit itself was interrupted the
    row may not exist, and recording the failure finds nothing to update.

    Raises:
        RunInProgress: When ``uq_analysis_run_single_running`` rejects the row, the
            database-enforced second guard: another run is ``running``. The insert rolled
            back, so ``progress.run_id`` is ``None``.
    """
    run = AnalysisRun(
        kind=kind,
        trigger=trigger,
        status="running",
        window_start=window.start,
        window_end=window.end,
        source_day_boundaries=[item.to_json() for item in window.source_days],
        config_hash=config.config_hash,
        default_days=window.default_days,
        trusted_min_days=window.trusted_min_days,
        started_at=instant,
        finished_at=None,
        data_modes={},
        dataset_ids={},
    )
    try:
        async with session_scope() as session:
            session.add(run)
            await session.flush()
            progress.run_id = run.id
    except IntegrityError as exc:
        # The insert's transaction rolled back: no row exists to record a failure on.
        progress.run_id = None
        if _constraint_name(exc) == _SINGLE_RUNNING_INDEX:
            _log.info("analysis_run_already_running", code=RunInProgress.code)
            raise RunInProgress from exc
        raise
    return run


async def _load(
    window: AnalysisWindow, config: Thresholds, kind: RunKind, instant: datetime
) -> AnalysisContext:
    """Read the run's context in one read-only ``REPEATABLE READ`` transaction.

    ``load_context`` issues one query per part (entities, facts, snapshots, leads, CRM,
    decisions, discrepancies, feedback). Under one snapshot they all see the same committed
    state, so a sync committing midway can never pair, say, new leads with old matches. The
    transaction is read-only, so it cannot fail on a serialization conflict, and the
    connection's isolation level is restored when it returns to the pool.
    """
    async with session_scope() as session:
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        return await metrics.load_context(session, window, config, kind=kind, now=instant)


# ---------------------------------------------------------------------------
# The pure computation, on the analysis thread (AAP 0.11.1, 0.1.1)
# ---------------------------------------------------------------------------


class _ComputationStoppedError(Exception):
    """Raised on the analysis thread at a stop checkpoint once its run was cancelled.

    It never leaves the runner: :func:`_in_analysis_thread` replaces it with the caller's
    :class:`asyncio.CancelledError` once the thread has stopped.
    """


@dataclass(slots=True)
class _RunProgress:
    """How far one run got, shared by :func:`run_analysis` and its analysis thread.

    The event loop writes :attr:`stage` up to the hand-off to the analysis thread, the thread
    writes it while it computes, and the loop reads it again only after the thread has
    finished, for the ``analysis_run_failed`` line. :attr:`stop` goes the other way: the loop
    sets it when the run is cancelled and the thread checks it at every checkpoint.

    Attributes:
        stage: The step in progress: ``insert`` (the ``running`` row's commit and close),
            ``claim_requests``, ``load_context``, ``check_overrides``, ``rules``,
            ``precedence`` or ``persist`` (row rendering included).
        run_id: The ``running`` row's id, recorded by :func:`_insert_running` once the row
            is flushed and before it commits; ``None`` while no row can exist.
        stop: Set once the run is cancelled; the computation stops at its next checkpoint.
    """

    stage: str
    run_id: int | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def checkpoint(self) -> None:
        """Stop the computation here when its run was cancelled.

        Raises:
            _ComputationStoppedError: When :attr:`stop` is set.
        """
        if self.stop.is_set():
            raise _ComputationStoppedError(self.stage)

    def enter(self, stage: str) -> None:
        """Pass a checkpoint, then record that ``stage`` begins.

        Raises:
            _ComputationStoppedError: When :attr:`stop` is set; the stage stays the one the
                run was cancelled in.
        """
        self.checkpoint()
        self.stage = stage


def _evaluate(ctx: AnalysisContext, checkpoint: Callable[[], None]) -> tuple[RuleResult, ...]:
    """Evaluate every rule module in :data:`EVALUATION_ORDER`, passing ``checkpoint`` first.

    ``_RULES`` is read at call time and its ``evaluate`` callables, bound at import, are
    called as found there; ``checkpoint`` runs before each module, so a cancelled run stops
    before the next module starts rather than after the last.

    Raises:
        _ComputationStoppedError: From ``checkpoint``, after the run was cancelled.
        Exception: Whatever a rule raises, with a note naming the rule module; the run fails.
    """
    results: list[RuleResult] = []
    for name, evaluate in _RULES:
        checkpoint()
        try:
            results.append(evaluate(ctx))
        except Exception as exc:
            exc.add_note(f"raised by analysis rule module {name!r}")
            raise
    return tuple(results)


def _compute(
    run_id: int, ctx: AnalysisContext, config: Thresholds, progress: _RunProgress
) -> tuple[_PreparedRun, float]:
    """Run the pure part of run ``run_id``: steps 5 to 7 and the row rendering of step 8.

    Executes on the analysis thread (:func:`_in_analysis_thread`) and alone touches ``ctx``
    meanwhile, including its per-run ``cache``. It checks the white-paper creative overrides,
    evaluates the rule modules, applies precedence and renders every output row with its
    typed keys and the summary counts. The override check and precedence are looked up when
    they run; the rule evaluators are the callables :data:`_RULES` bound at import, read
    from it when :func:`_evaluate` runs, so replacing ``_RULES`` substitutes an evaluator
    and replacing a module's ``evaluate`` does not. A checkpoint precedes the override
    check, each rule module, precedence and the row rendering; once ``progress.stop`` is set
    the computation raises there. ``progress.stage`` names the step in progress.

    Returns:
        The rendered rows and the :func:`time.perf_counter` reading taken when precedence
        finished, which ends ``compute_ms``.

    Raises:
        _ComputationStoppedError: At a checkpoint after the run was cancelled.
        ConfigError: When a creative override contradicts its creative's form.
        PrecedenceInvariantError: When a precedence guarantee breaks.
        ValueError: When an item breaks the persistence contract.
        Exception: Whatever a rule raises, with a note naming its module.
    """
    progress.checkpoint()
    config.check_overrides({item.urn: item.lead_form_urn for item in ctx.creatives.values()})
    progress.enter("rules")
    results = _evaluate(ctx, progress.checkpoint)
    progress.enter("precedence")
    out = precedence.apply_precedence(results, ctx)
    computed = time.perf_counter()
    progress.enter("persist")
    return _PreparedRun.of(run_id, ctx, out), computed


def _analysis_executor() -> ThreadPoolExecutor:
    """Return the process's analysis thread pool, creating it on first use.

    One worker thread, named ``ads-engine-analysis-<n>``: the analysis lock admits one run
    per process at a time, so a run never queues behind another one's computation.
    """
    global _executor
    executor = _executor
    if executor is not None:
        return executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=_ANALYSIS_THREAD_PREFIX
            )
        return _executor


async def _in_analysis_thread[T](compute: Callable[[], T], stop: threading.Event) -> T:
    """Run ``compute`` on the analysis thread and await it without blocking the event loop.

    The rules are synchronous and CPU-bound (statistical tests, attribution, clustering), so
    running them on the event loop would stall every other request of the process for the
    whole computation. ``compute`` runs in a copy of the caller's :mod:`contextvars`
    context, so the structlog context bound around the run stays on its log lines.

    The wait is cancellation-safe: the first cancellation of the caller sets ``stop``, on
    which the computation raises at its next checkpoint; it and any repeated cancellation
    are held until the thread has actually finished, and then re-raised. The caller holds
    the analysis lock around this wait, so the lock is never released while a computation
    still runs, and no computation outlives its run. The future is waited on with
    :func:`asyncio.wait`, which leaves it untouched when the wait is cancelled; re-shielding
    it after a cancellation would make asyncio log the expected stop as an error.

    Returns:
        What ``compute`` returned.

    Raises:
        asyncio.CancelledError: When the caller was cancelled meanwhile, once ``compute`` has
            finished, whatever it returned or raised (an error other than the stop is
            chained as the cause).
        Exception: Whatever ``compute`` raised, unchanged and with its notes, when no
            cancellation arrived.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        _analysis_executor(), functools.partial(contextvars.copy_context().run, compute)
    )
    cancelled: asyncio.CancelledError | None = None
    # Only this function holds the future, which therefore completes only once ``compute``
    # has returned or raised: from then on nothing runs on the thread for this run.
    while not future.done():
        try:
            await asyncio.wait((future,))
        except asyncio.CancelledError as err:
            stop.set()
            if cancelled is None:
                cancelled = err
    if cancelled is None:
        return future.result()
    error = None if future.cancelled() else future.exception()
    if error is None or isinstance(error, _ComputationStoppedError):
        raise cancelled from None
    raise cancelled from error


def _check_default_shape(window: AnalysisWindow, config: Thresholds) -> None:
    """Require a ``default`` run to read the configured default window.

    ``/runs/latest`` shows only ``default`` runs (AAP 0.5.3), so a default run over any other
    window would stand in for the default.

    Raises:
        ValueError: When the window is not ``window.default_days`` days ending on its
            default end.
    """
    if not window.is_default_shape or window.days != config.default_days:
        raise ValueError(
            f"a default run needs the configured default window ({config.default_days} days "
            f"ending {window.default_end.isoformat()}), found {window.days} days from "
            f"{window.start.isoformat()} to {window.end.isoformat()}; record it as custom"
        )


def _request_ids(request_ids: Sequence[int]) -> tuple[int, ...]:
    """Validate and de-duplicate the explicit ``analysis_request`` ids a caller passed.

    Raises:
        TypeError: When an id is not an ``int``.
    """
    ids: dict[int, None] = {}
    for value in request_ids:
        item: object = value
        if isinstance(item, bool) or not isinstance(item, int):
            raise TypeError(f"request_ids must hold ints, found {item!r}")
        ids.setdefault(item, None)
    return tuple(ids)


def _serve_through(serve_through: int | None) -> int | None:
    """Validate the ``serve_through`` watermark: an ``analysis_request`` id, or ``None``.

    Raises:
        TypeError: When it is not an ``int`` (a ``bool`` included).
        ValueError: When it is not a positive ``bigint``.
    """
    if serve_through is None:
        return None
    value: object = serve_through
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"serve_through must be an int, found {value!r}")
    if not 1 <= value <= _BIGINT_MAX:
        raise ValueError(f"serve_through must be a positive analysis_request id, found {value}")
    return value


def _summary(out: PrecedenceOutput) -> dict[str, Any]:
    """The per-section counts the ``analysis_run_summary`` line reports (AAP 0.11.1)."""
    suppressed: dict[str, int] = {}
    for section in SECTION_ORDER:
        for reason, count in out.suppressed[section].items():
            suppressed[reason] = suppressed.get(reason, 0) + count
    items = out.all_items()
    return {
        "items": {section: len(out.items[section]) for section in SECTION_ORDER},
        "items_total": len(items),
        "held": sum(1 for item in items if item.candidate.held),
        "withheld": {section: len(out.withheld[section]) for section in SECTION_ORDER},
        "withheld_total": sum(len(out.withheld[section]) for section in SECTION_ORDER),
        "findings_total": sum(len(out.findings[section]) for section in SECTION_ORDER),
        "suppressed": dict(sorted(suppressed.items())),
        "allocations": len(out.allocations),
    }


async def run_analysis(
    window: AnalysisWindow,
    kind: RunKind,
    trigger: RunTrigger,
    *,
    reason: str | None = None,
    request_ids: Sequence[int] = (),
    serve_through: int | None = None,
    thresholds: Thresholds | None = None,
    now: datetime | None = None,
) -> AnalysisRun:
    """Compute one analysis run over ``window`` from stored data and persist it, frozen.

    AAP 0.5.1, 0.5.3. The steps are those of the module docstring: take the shared
    ``analysis`` lock (never taken by the caller), insert and commit the ``running`` row,
    claim the requests it serves and commit the claim, read the context in one snapshot,
    check the creative overrides, evaluate the rules in :data:`EVALUATION_ORDER`, apply
    precedence, and write every output with the run's ``succeeded`` state in one
    transaction. Any failure after the row exists marks it ``failed`` and re-raises; a run
    is never partial, and requests it claimed stay stamped with it.

    The override check, the rules, precedence and the rendering of the output rows run on
    the analysis thread, so the event loop stays free while the run computes. Cancelling
    the call stops that computation at its next checkpoint (before a rule module,
    precedence or row rendering); the lock is released only after the thread has stopped
    and the row is ``failed`` (the requests it claimed stay stamped with it), and then
    ``CancelledError`` propagates.

    ``kind="default"`` is the worker's run of the configured default window and must be
    given that window (:func:`default_window`); ``kind="custom"`` takes any valid window,
    even one equal to the default (AAP 0.5.3 "Run kinds").

    Args:
        window: The analysis window (:func:`default_window` or :func:`custom_window`).
        kind: ``default`` or ``custom``.
        trigger: ``scheduled``, ``event`` or ``manual``.
        reason: Why the run happens, for the log (for example the coalesced request
            reasons); not stored.
        request_ids: Explicit ``analysis_request`` ids the run serves, any number of them;
            they are stamped with ``picked_at`` and this run's id once its ``running`` row
            is committed, before its context is loaded.
        serve_through: The backlog watermark (:attr:`PendingRequests.through_id`): the run
            serves, and stamps the same way, every unserved pending request with an id up
            to it, with one set-based update. Exclusive with ``request_ids``.
        thresholds: The loaded configuration; :meth:`Thresholds.load` when ``None``.
        now: The run instant (tz-aware): ``started_at``, the CRM "as of" time and the
            feedback cut-off. ``datetime.now(UTC)`` when omitted.

    Returns:
        The persisted ``succeeded`` run, readable after its session closed.

    Raises:
        RunInProgress: When another process or task holds the ``analysis`` lock (API 409
            ``run_in_progress``, worker leaves requests pending, CLI exit 3).
        RunNoLongerRunning: When the run lost its lock and was marked ``failed`` elsewhere.
        ConfigError: When the configuration fails to load (no run row is written) or a
            creative override contradicts its creative's form (the run is marked failed).
        PrecedenceInvariantError: When a precedence guarantee breaks (the run is marked
            failed).
        TypeError: When ``window`` is not an :class:`AnalysisWindow`, ``now`` is not a
            ``datetime``, or a request id or ``serve_through`` is not an ``int``.
        ValueError: For an unknown ``kind`` or ``trigger``, a naive ``now``, a
            ``serve_through`` that is not a positive id, both ``serve_through`` and
            ``request_ids``, or a default run over a window other than the configured
            default.
        sqlalchemy.exc.SQLAlchemyError: When the database fails.
        asyncio.CancelledError: When the call is cancelled, once the computation has
            stopped and the run, if its row exists, is recorded ``failed``.
    """
    window_value: object = window
    if not isinstance(window_value, AnalysisWindow):
        raise TypeError(f"window must be an AnalysisWindow, found {window_value!r}")
    if kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {RUN_KINDS}, found {kind!r}")
    if trigger not in RUN_TRIGGERS:
        raise ValueError(f"trigger must be one of {RUN_TRIGGERS}, found {trigger!r}")
    instant = _aware(now, "now")
    served = _request_ids(request_ids)
    through = _serve_through(serve_through)
    if through is not None and served:
        raise ValueError("pass either serve_through or request_ids to run_analysis, not both")
    log_reason = None if reason is None else reason[:_REASON_LOG_LIMIT]

    async with analysis_lock():
        started = time.perf_counter()
        config = thresholds if thresholds is not None else Thresholds.load()
        if kind == "default":
            _check_default_shape(window, config)
        progress = _RunProgress(stage="insert")
        try:
            run = await _insert_running(window, kind, trigger, config, instant, progress)
            progress.stage = "claim_requests"
            claimed = await _claim_requests(run.id, served, through)
            _log.info(
                "analysis_run_started",
                run_id=run.id,
                kind=kind,
                trigger=trigger,
                reason=log_reason,
                window_start=window.start.isoformat(),
                window_end=window.end.isoformat(),
                days=window.days,
                requests=claimed,
                config_hash=config.config_hash,
            )
            progress.stage = "load_context"
            ctx = await _load(window, config, kind, instant)
            loaded = time.perf_counter()
            progress.stage = "check_overrides"
            prepared, computed = await _in_analysis_thread(
                functools.partial(_compute, run.id, ctx, config, progress), progress.stop
            )
            stored = await _persist(run.id, prepared)
        except BaseException as exc:
            # From the flush of the running row on, every failure, cancellation included,
            # leaves that row failed before the lock is released.
            if progress.run_id is not None:
                await _record_failure(progress.run_id, exc, progress.stage, started)
            raise
        finished = time.perf_counter()

    _log.info(
        "analysis_run_summary",
        run_id=stored.id,
        kind=kind,
        trigger=trigger,
        reason=log_reason,
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        days=window.days,
        data_modes=stored.data_modes,
        config_hash=config.config_hash,
        requests=claimed,
        load_ms=round((loaded - started) * 1000),
        compute_ms=round((computed - loaded) * 1000),
        persist_ms=round((finished - computed) * 1000),
        duration_ms=round((finished - started) * 1000),
        **prepared.summary,
    )
    return stored
