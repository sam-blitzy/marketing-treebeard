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

Off the event loop, and for ``POST /runs`` out of the API process. Steps 5 to 7 and the
rendering of step 8's rows are synchronous and CPU-bound (statistical tests, attribution,
clustering). They run on one of two paths:

* **The analysis thread** (the worker, the CLI, every call without ``isolated``): one
  function on a single-worker thread pool created on first use, so the caller's event loop
  stays free. The thread runs in a copy of the caller's context variables, so the bound log
  context stays on its lines, and only the thread touches the context while it computes.
  The event loop keeps every database step (AAP 0.11.1 async I/O).
* **The analysis process** (``run_analysis(..., isolated=True)``, which ``POST /runs``
  passes): steps 4 to 8, the context load and the persistence included, run in one
  persistent child of the API process behind a one-worker ``spawn``
  ``ProcessPoolExecutor``, at a lower CPU priority (``nice`` 10). A thread would share the
  API process's GIL, and a large custom run then slowed every request and lock conflict;
  in the child it shares neither, so the API keeps serving stored pages and answers
  contenders 409 at once (AAP 0.1.1: stored reads under 2 s). The API spawns it at startup
  (:func:`start_analysis_process`) and stops it at shutdown
  (:func:`stop_analysis_process`); a child that died is replaced on the next run, and one
  spawned under another environment (another ``DATABASE_URL``) is replaced at the next
  hand-off, so the child always reads the database the caller's lock connection reads.
  Steps 1 to 3, the failure record of step 9 and both log lines stay with the caller, which
  holds the lock until the child has finished the run, so contenders are refused before any
  heavy work starts, exactly as on the thread path. The child dies with the API process (a
  Linux parent-death signal, and a watchdog thread on every platform), and PostgreSQL ends
  its sessions soon after, so no run outlives the lock even when the API process is killed
  mid-run. Every exception that crosses back is checked to unpickle as itself, or else
  replaced by :class:`AnalysisProcessError` naming its type, and the child's traceback
  becomes the cause of the error the caller raises.

Cancellation never outlives the lock. A cancellation of :func:`run_analysis` while the
thread computes sets a stop flag, checked before the override check, before each rule
module, before precedence and before row rendering; the cancellation, repeated ones
included, is held until the thread has finished and then re-raised. In the analysis
process the same stop is a value shared with the API process naming the run's job: the
same checkpoints honour it, and a database step in flight there is cancelled within 50 ms,
its transaction rolled back. A run the analysis process stored before the stop took hold
stays ``succeeded``, its summary is logged, and the cancellation is re-raised. The failure
record of step 9 runs to completion despite cancellation, bounded by a timeout, so a
cancelled run's row is ``failed`` before the lock is released.

What each stored recommendation carries (AAP 0.5.3 ``Item``, 0.5.4.8): its section, group,
rule id, rank, action, reason, data status, hold and required datasets as columns; its
entity as ``entity_type`` and the stable key ``entity_id`` (``EntityRef.key``) plus the typed
row keys (``campaign_id`` … ``creative_id``); and the frozen evidence, whose ``extra`` gains
two runner-owned keys: ``entity`` (``{source, type, id, name}``, the name the page shows)
and ``flag_messages`` (``{code: message}`` for the codes stored in ``flags``).

Boundaries: imports only the analysis modules, ``db.session``, ``db.repositories``, the
analysis and accounts models, ``logging`` and ``settings`` (for ``DISPLAY_TIMEZONE`` when the
caller passes no zone); never ``sync``, ``connectors``, ``simulation`` or ``api``.

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
import ctypes
import functools
import hashlib
import itertools
import multiprocessing
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from multiprocessing.reduction import ForkingPickler
from types import MappingProxyType
from typing import Any, Final, Literal, NoReturn, Protocol, get_args

import structlog
from sqlalchemy import ARRAY, BigInteger, any_, event, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
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
    display_zone,
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
from ads_engine.db.session import (
    dispose_engine,
    get_engine,
    run_to_completion,
    session_scope,
    use_session,
)
from ads_engine.logging import configure_logging, get_logger, mark_logged
from ads_engine.settings import get_settings

__all__ = [
    "DATASET_ID_KEYS",
    "EVALUATION_ORDER",
    "PENDING_REASONS_LIMIT",
    "RUNNER_EXTRA_KEYS",
    "AnalysisProcessError",
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
    "start_analysis_process",
    "stop_analysis_process",
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

The stored evidence keeps both keys. ``GET /recommendations`` projects them into
``Item.entity`` and ``Item.flags[].message`` and leaves them out of the ``evidence.extra`` it
returns, so a page never sends them twice.
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

_STAGES: Final[tuple[str, ...]] = (
    "insert",
    "claim_requests",
    "load_context",
    "check_overrides",
    "rules",
    "precedence",
    "persist",
)
"""Every value of ``_RunProgress.stage``, in run order; the analysis process reports an index."""

_LOAD_STAGE: Final = "load_context"
"""The stage a run is in when it is handed to the analysis process."""

_ANALYSIS_NICENESS: Final = 10
"""How much the analysis process lowers its own priority (``os.nice``), so the API process's
request handling wins the CPU the two share."""

_STOP_POLL_S: Final = 0.05
"""How often, in seconds, the analysis process checks for a stop while a database step runs."""

_PARENT_POLL_S: Final = 0.1
"""How often, in seconds, the analysis process's watchdog checks that the API process that
spawned it is still its parent (:func:`_watch_parent`)."""

_PARENT_WATCH_THREAD: Final = f"{_ANALYSIS_THREAD_PREFIX}-parent-watch"
"""The name of the analysis process's watchdog thread."""

_ORPHANED_EXIT_CODE: Final = 70
"""The exit status of an analysis process that outlived its API process: non-zero, and apart
from the interpreter's own 1, so whoever reaps it can tell why it exited."""

_PR_SET_PDEATHSIG: Final = 1
"""Linux's ``prctl`` option that names the signal a process receives when its parent dies."""

_CLIENT_CHECK_SQL: Final = "SET client_connection_check_interval = 1000"
"""Make PostgreSQL check every second that the analysis process is still connected while one
of its queries runs or waits for a lock, and end the session once it is gone.

Without it a session whose client died is ended only when it next answers that client: a load
blocked behind another session's table lock would wait on, and then run, after its process
and the run's lock were long gone."""

# The API's analysis process: one child, spawned on first use and guarded by its lock. The
# analysis lock admits one run at a time, so the child never queues a second job. Each job
# gets its own stop token, never its run id, which a test database may reuse.
_process_lock: Final = threading.Lock()
_job_tokens: Final = itertools.count(1)


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

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle as the constructor's arguments, so it crosses back from the analysis process.

        The default reduction would call the constructor with the message alone. The
        instance dict (its notes included) is restored as well.
        """
        return (type(self), (self.run_id, self.status), dict(vars(self)))


class AnalysisProcessError(RuntimeError):
    """A run handed to the API's analysis process could not report its own outcome.

    Raised by :func:`run_analysis` with ``isolated=True`` when the analysis process shut down
    before it ran the run, or when the error the run raised there cannot be handed back to
    the API process intact (it does not pickle, or unpickles as another type). Then
    :attr:`error_type` names that error's type, the message carries its text, and the
    analysis process's traceback is the cause.

    Attributes:
        message: The human-readable message, also the exception's ``str()``.
        error_type: The qualified type name of the error the analysis process could not hand
            back, or ``None`` when there was none.
    """

    def __init__(self, message: str, error_type: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type

    def __reduce__(self) -> tuple[Any, ...]:
        """Pickle as the constructor's arguments and the instance dict, its notes included."""
        return (type(self), (self.message, self.error_type), dict(vars(self)))


class _RemoteTracebackError(Exception):
    """The analysis process's formatted traceback, chained as the cause of the error it raised.

    Its ``str()`` is the traceback text, so a log line of the error the API process raises
    shows where the analysis process failed.
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text

    def __str__(self) -> str:
        """The analysis process's traceback, on lines of its own."""
        return f"\n{self.text}"


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

    ``finished_at DESC`` (PostgreSQL's ``NULLS FIRST``) is the order a backward scan of
    ``ix_analysis_run_kind_status_finished`` (``kind``, ``status``, ``finished_at``) returns,
    so the read is one index probe, sorting only the runs that share the newest
    ``finished_at`` by id, however long the run history grows; ``NULLS LAST`` would instead
    scan and sort every succeeded default run. A run is marked ``succeeded`` together with its
    ``finished_at``, so ``finished_at IS NOT NULL`` excludes no real run: it is an index
    condition of the same probe, and it stops a succeeded row without a finish time from
    sorting first.

    Args:
        session: The caller's session.

    Returns:
        The run, or ``None`` before the first default run succeeds.
    """
    result = await session.execute(
        select(AnalysisRun)
        .where(
            AnalysisRun.kind == "default",
            AnalysisRun.status == "succeeded",
            AnalysisRun.finished_at.is_not(None),
        )
        .order_by(AnalysisRun.finished_at.desc(), AnalysisRun.id.desc())
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
    """Every output row of one run, rendered off the event loop before the write.

    Built on the analysis thread, or in the analysis process, from the context and the
    precedence output, so that :func:`_persist` never reads either: the event loop only adds
    the typed entity keys a stored row carries (:func:`_known_ids`) and inserts. Rows keep
    section and rank order.

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
    marked it ``failed``, and then nothing is written. The rows were rendered off the event
    loop (:class:`_PreparedRun`); here they only gain the typed entity keys a stored row
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
    window: AnalysisWindow,
    config: Thresholds,
    kind: RunKind,
    instant: datetime,
    *,
    display_timezone: str,
) -> AnalysisContext:
    """Read the run's context in one read-only ``REPEATABLE READ`` transaction.

    ``load_context`` issues one query per part (entities, facts, snapshots, leads, CRM,
    decisions, discrepancies, feedback). Under one snapshot they all see the same committed
    state, so a sync committing midway can never pair, say, new leads with old matches. The
    transaction is read-only, so it cannot fail on a serialization conflict, and the
    connection's isolation level is restored when it returns to the pool. The context
    carries ``display_timezone``, the zone of the instants in the run's frozen flag messages.
    """
    async with session_scope() as session:
        await session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        return await metrics.load_context(
            session, window, config, kind=kind, now=instant, display_timezone=display_timezone
        )


# ---------------------------------------------------------------------------
# The pure computation, on the analysis thread or in the analysis process (AAP 0.11.1, 0.1.1)
# ---------------------------------------------------------------------------


class _ComputationStoppedError(Exception):
    """Raised at a stop checkpoint once its run was cancelled.

    It never leaves the runner: :func:`_in_analysis_thread` and :func:`_in_analysis_process`
    replace it with the caller's :class:`asyncio.CancelledError` once the computation has
    stopped.
    """


class _StopFlag(Protocol):
    """A run's stop signal: set on the caller's cancellation, read at every checkpoint.

    A :class:`threading.Event` on the analysis thread; a :class:`_JobStop` in the analysis
    process.
    """

    def is_set(self) -> bool:
        """Whether the run was stopped."""
        ...

    def set(self) -> None:
        """Stop the run at its next checkpoint."""
        ...


@dataclass(slots=True)
class _RunProgress:
    """How far one run got, shared by :func:`run_analysis` and its analysis thread or process.

    The event loop writes :attr:`stage` up to the hand-off to the analysis thread, the thread
    writes it while it computes, and the loop reads it again only after the thread has
    finished, for the ``analysis_run_failed`` line. :attr:`stop` goes the other way: the loop
    sets it when the run is cancelled and the thread checks it at every checkpoint. The
    analysis process keeps a progress of its own, whose stages :attr:`report` mirrors to the
    API process; the API process copies the stage the run failed in into its own progress.

    Attributes:
        stage: The step in progress: ``insert`` (the ``running`` row's commit and close),
            ``claim_requests``, ``load_context``, ``check_overrides``, ``rules``,
            ``precedence`` or ``persist`` (row rendering included).
        run_id: The ``running`` row's id, recorded by :func:`_insert_running` once the row
            is flushed and before it commits; ``None`` while no row can exist.
        stop: Set once the run is cancelled; the computation stops at its next checkpoint.
        report: Called with every stage :meth:`begin` records, or ``None``. The analysis
            process writes it to the value it shares with the API process, which reads it
            when the analysis process died mid-run.
    """

    stage: str
    run_id: int | None = None
    stop: _StopFlag = field(default_factory=threading.Event)
    report: Callable[[str], None] | None = None

    def checkpoint(self) -> None:
        """Stop the computation here when its run was cancelled.

        Raises:
            _ComputationStoppedError: When :attr:`stop` is set.
        """
        if self.stop.is_set():
            raise _ComputationStoppedError(self.stage)

    def begin(self, stage: str) -> None:
        """Record that ``stage`` begins, and pass it to :attr:`report` when one is set."""
        self.stage = stage
        if self.report is not None:
            self.report(stage)

    def enter(self, stage: str) -> None:
        """Pass a checkpoint, then record that ``stage`` begins.

        Raises:
            _ComputationStoppedError: When :attr:`stop` is set; the stage stays the one the
                run was cancelled in.
        """
        self.checkpoint()
        self.begin(stage)


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

    Executes on the analysis thread (:func:`_in_analysis_thread`) or in the analysis process
    (:func:`_run_in_process`) and alone touches ``ctx`` meanwhile, including its per-run
    ``cache``. It checks the white-paper creative overrides,
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


async def _in_analysis_thread[T](compute: Callable[[], T], stop: _StopFlag) -> T:
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


# ---------------------------------------------------------------------------
# The analysis process: custom runs from the API, outside its GIL (AAP 0.1.1, 0.5.3)
# ---------------------------------------------------------------------------


class _SharedInt(Protocol):
    """An integer in shared memory, as ``multiprocessing`` ``Value(code, 0, lock=False)``."""

    value: int


@dataclass(frozen=True, slots=True)
class _ProcessSignals:
    """The two integers the API process shares with its analysis process.

    Both live in shared memory without a lock (``Value(..., lock=False)``). Each is one
    machine word that one side alone writes, and a lock held by an analysis process that was
    killed would stall the API process's next read or write for good.

    Attributes:
        stop: The token of the last job the API process stopped; ``0`` before any.
        stage: The index in :data:`_STAGES` of the step the current job is in. The analysis
            process writes it; the API process resets it before each job and reads it when
            the analysis process died.
    """

    stop: _SharedInt
    stage: _SharedInt

    def report(self, stage: str) -> None:
        """Record that the current job entered ``stage``, one of :data:`_STAGES`."""
        self.stage.value = _STAGES.index(stage)

    def stage_name(self) -> str:
        """The step the current job last reported."""
        code = self.stage.value
        return _STAGES[code] if 0 <= code < len(_STAGES) else _LOAD_STAGE


@dataclass(frozen=True, slots=True)
class _JobStop:
    """One job's stop flag: set once the shared stop value holds the job's token.

    The API process sets it when the run is cancelled; the analysis process reads it at every
    checkpoint and while a database step runs. A later job has another token, so a stop never
    outlives the job it was meant for.
    """

    signals: _ProcessSignals
    token: int

    def is_set(self) -> bool:
        """Whether the API process stopped this job."""
        return self.signals.stop.value == self.token

    def set(self) -> None:
        """Stop this job at its next checkpoint."""
        self.signals.stop.value = self.token


@dataclass(frozen=True, slots=True)
class _AnalysisJob:
    """What the analysis process needs to load, compute and persist one run.

    Attributes:
        token: The job's own stop token, from :data:`_job_tokens`.
        run_id: The ``running`` row the calling process inserted and committed.
        window: The run's window.
        kind: ``default`` or ``custom``.
        config: The configuration the run uses (the API's loaded thresholds).
        instant: The run instant.
        display_timezone: The zone of the instants in the run's frozen flag messages.
        log_context: The caller's bound log context (scalar values only) and the run id,
            bound around the job's log lines.
    """

    token: int
    run_id: int
    window: AnalysisWindow
    kind: RunKind
    config: Thresholds
    instant: datetime
    display_timezone: str
    log_context: dict[str, str | int | float | bool | None]


@dataclass(frozen=True, slots=True)
class _ProcessSuccess:
    """A job the analysis process stored ``succeeded``.

    Attributes:
        stored: The persisted run, detached.
        summary: The per-section counts of the ``analysis_run_summary`` line.
        load_ms: Milliseconds the context load took in the analysis process.
        compute_ms: Milliseconds from the loaded context to the end of precedence.
        persist_ms: Milliseconds of row rendering and the persistence transaction.
    """

    stored: AnalysisRun
    summary: dict[str, Any]
    load_ms: int
    compute_ms: int
    persist_ms: int


@dataclass(frozen=True, slots=True)
class _ProcessFailure:
    """A job that failed or stopped in the analysis process; nothing of it was stored.

    Attributes:
        stage: The step it failed or stopped in.
        error: Its error, or the :class:`AnalysisProcessError` standing in for one that
            cannot cross back intact.
        remote_traceback: The analysis process's formatted traceback of the error.
        stopped: Whether it stopped on the API process's stop (a stop checkpoint, or a
            cancelled database step).
    """

    stage: str
    error: BaseException
    remote_traceback: str
    stopped: bool


@dataclass(frozen=True, slots=True)
class _AnalysisProcess:
    """The API's analysis process: its executor, its shared values and its environment.

    Attributes:
        executor: The one-worker ``spawn`` executor whose worker is the analysis process.
        signals: The values shared with that worker.
        fingerprint: The SHA-256 of the API process's environment when it was created.
    """

    executor: ProcessPoolExecutor
    signals: _ProcessSignals
    fingerprint: str


_process: _AnalysisProcess | None = None
"""The API process's analysis process, created on first use; guarded by ``_process_lock``."""

_process_signals: _ProcessSignals | None = None
"""In the analysis process, the values it shares with the API process, set by
:func:`_init_analysis_process`; ``None`` in every other process."""

_parent_pid: int | None = None
"""In the analysis process, the pid of the API process that spawned it and whose lifetime it
shares, set by :func:`_init_analysis_process`; ``None`` in every other process."""


def _ms(seconds: float) -> int:
    """``seconds`` as whole milliseconds."""
    return round(seconds * 1000)


def _set_parent_death_signal() -> bool:
    """Ask Linux to ``SIGKILL`` this process the moment its parent dies; report whether it took.

    ``prctl(PR_SET_PDEATHSIG, SIGKILL)`` through the C library. A library that cannot be
    loaded or a refused call is logged, with the call's ``errno``, and the watchdog of
    :func:`_bind_to_parent` then ends the process on its own.

    Returns:
        Whether the kernel accepted the death signal; ``False`` on every other platform.
    """
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result: int = libc.prctl(ctypes.c_int(_PR_SET_PDEATHSIG), ctypes.c_ulong(signal.SIGKILL))
    except (OSError, AttributeError) as exc:
        _log.warning("analysis_process_death_signal_failed", error=type(exc).__name__)
        return False
    if result != 0:
        _log.warning("analysis_process_death_signal_failed", errno=ctypes.get_errno())
        return False
    return True


def _exit_orphaned(parent_pid: int) -> NoReturn:
    """Log that the API process ``parent_pid`` is gone and end this process at once.

    ``os._exit`` runs no cleanup: the executor's own shutdown would wait on a parent that can
    no longer answer. The kernel closes the process's connections, and PostgreSQL ends their
    sessions and rolls back their open transactions (:data:`_CLIENT_CHECK_SQL`).
    """
    _log.error("analysis_process_orphaned", parent_pid=parent_pid)
    os._exit(_ORPHANED_EXIT_CODE)


def _watch_parent(parent_pid: int) -> None:
    """The watchdog thread's loop: end the process once ``parent_pid`` is no longer its parent.

    A process whose parent died is re-parented (to ``init`` or a subreaper), so its
    ``getppid`` changes; it is compared every :data:`_PARENT_POLL_S` seconds.
    """
    while os.getppid() == parent_pid:
        time.sleep(_PARENT_POLL_S)
    _exit_orphaned(parent_pid)


def _bind_to_parent(parent_pid: int) -> None:
    """Make the analysis process die with ``parent_pid``, the API process that spawned it.

    The API process holds the run's ``analysis`` lock on its own connection, which
    PostgreSQL releases the moment that process dies (``SIGKILL``, the OOM killer). An
    analysis process that lived on would keep loading or computing a run whose lock any API
    process, the worker or the CLI could then take, so two runs would execute at once. Two
    guards end it instead:

    * on Linux, the parent-death signal (:func:`_set_parent_death_signal`): the kernel kills
      the process together with its parent;
    * on every platform, a daemon watchdog thread, :data:`_PARENT_WATCH_THREAD`
      (:func:`_watch_parent`), that ends the process within :data:`_PARENT_POLL_S` seconds of
      its parent's death, should the signal be unavailable.

    A parent that died before the signal was set left the process orphaned already: it
    exits at once (:func:`_exit_orphaned`).
    """
    # The kernel sends the death signal when the *thread* that spawned this process ends.
    # ProcessPoolExecutor spawns its worker in the thread that calls submit (CPython 3.14
    # concurrent/futures/process.py: submit -> _adjust_process_count), and _submit runs on
    # the API process's event-loop thread, which lives as long as the API process does.
    _set_parent_death_signal()
    if os.getppid() == parent_pid:
        threading.Thread(
            target=_watch_parent, args=(parent_pid,), name=_PARENT_WATCH_THREAD, daemon=True
        ).start()
    else:
        _exit_orphaned(parent_pid)


def _init_analysis_process(stop: _SharedInt, stage: _SharedInt, parent_pid: int) -> None:
    """Prepare a freshly spawned analysis process: the executor's initializer.

    Stores the values shared with the API process and that process's pid, ``parent_pid``;
    ignores ``SIGINT``, which a terminal sends to the whole process group, so that only the
    API process shuts its analysis process down; configures logging from the environment's
    settings, as the API does; binds its own lifetime to the API process
    (:func:`_bind_to_parent`), so it dies with that process and no run outlives the
    ``analysis`` lock the API process holds; and lowers its own priority by
    :data:`_ANALYSIS_NICENESS`, so the API's request handling wins the CPU the two share. A
    refused priority change is logged and the process runs on.
    """
    global _process_signals, _parent_pid
    _process_signals = _ProcessSignals(stop=stop, stage=stage)
    _parent_pid = parent_pid
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging()
    _bind_to_parent(parent_pid)
    try:
        os.nice(_ANALYSIS_NICENESS)
    except OSError as exc:
        _log.warning(
            "analysis_process_nice_failed", niceness=_ANALYSIS_NICENESS, error=type(exc).__name__
        )


def _analysis_process_pid() -> int:
    """Return the analysis process's pid: the warm-up job of :func:`start_analysis_process`."""
    return os.getpid()


def _type_name(error: BaseException) -> str:
    """``error``'s qualified type name, ``module.QualName``."""
    kind = type(error)
    return f"{kind.__module__}.{kind.__qualname__}"


def _portable_error(error: BaseException) -> BaseException:
    """Return ``error`` when it crosses back to the API process intact, else a stand-in.

    The error is pickled and unpickled exactly as the executor hands a result back
    (:class:`~multiprocessing.reduction.ForkingPickler`). One that cannot be pickled or
    rebuilt (a constructor needing more than its message, say), or that comes back as
    another type, is replaced by :class:`AnalysisProcessError` naming its type and text:
    an unpickling failure in the API process would break the executor itself.
    """
    try:
        restored: object = ForkingPickler.loads(ForkingPickler.dumps(error))
    except BaseException as failure:
        if not isinstance(failure, Exception):
            raise
        restored = None
    if type(restored) is type(error):
        return error
    text = "".join(traceback.format_exception_only(error)).strip()
    return AnalysisProcessError(
        f"the analysis process raised {text}, which cannot be handed back intact",
        _type_name(error),
    )


def _failure(stage: str, error: BaseException) -> _ProcessFailure:
    """The outcome of a job that raised ``error`` in ``stage``, ready to cross back."""
    return _ProcessFailure(
        stage=stage,
        error=_portable_error(error),
        remote_traceback="".join(traceback.format_exception(error)),
        stopped=isinstance(error, _ComputationStoppedError | asyncio.CancelledError),
    )


async def _until_stopped[T](step: Coroutine[Any, Any, T], progress: _RunProgress) -> T:
    """Await one database step of a job, cancelling it once the API process stops the job.

    The step runs as a task that is checked every :data:`_STOP_POLL_S` seconds. Once the job
    is stopped the task is cancelled and awaited: its session rolls back
    (:func:`~ads_engine.db.session.session_scope`), so a stopped step leaves nothing behind.
    A step that finished before the cancellation took hold returns as usual.

    Returns:
        What the step returned.

    Raises:
        _ComputationStoppedError: When the step was cancelled because the job was stopped.
        Exception: Whatever the step raised.
    """
    task = asyncio.create_task(step)
    while True:
        done, _ = await asyncio.wait((task,), timeout=_STOP_POLL_S)
        if not done and progress.stop.is_set():
            task.cancel()
            await asyncio.wait((task,))
        if task.done():
            if task.cancelled():
                raise _ComputationStoppedError(progress.stage)
            return task.result()


async def _dispose_process_engine() -> None:
    """Dispose the analysis process's engine after a job; a failure is logged, never raised.

    Each job runs on an event loop of its own, and an async connection belongs to the loop
    that opened it, so no connection may outlive its job; the process then holds none
    between runs either.
    """
    try:
        await dispose_engine()
    except (SQLAlchemyError, OSError) as exc:
        _log.warning("analysis_process_dispose_failed", error=type(exc).__name__)


def _check_clients_of(engine: AsyncEngine) -> None:
    """Make PostgreSQL end each session ``engine`` opens soon after this process is gone.

    Every new connection runs :data:`_CLIENT_CHECK_SQL` and commits it, so the setting
    outlives the transaction the statement opened. A server that refuses the setting
    (PostgreSQL accepts it only on systems that report closed sockets) is logged as
    ``analysis_process_client_check_unavailable`` and the connection is used as it is.
    Called once per job, on the engine the job opens.
    """
    dbapi_error: type[Exception] = engine.dialect.loaded_dbapi.Error

    def check(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(_CLIENT_CHECK_SQL)
            dbapi_connection.commit()
        except dbapi_error as exc:
            dbapi_connection.rollback()
            _log.warning("analysis_process_client_check_unavailable", error=type(exc).__name__)
        finally:
            cursor.close()

    event.listen(engine.sync_engine, "connect", check)


async def _run_in_process(
    job: _AnalysisJob, signals: _ProcessSignals
) -> _ProcessSuccess | _ProcessFailure:
    """Load, compute and persist one job in the analysis process: steps 4 to 8 of a run.

    The context is read in one read-only snapshot (:func:`_load`); the override check, the
    rules, precedence and row rendering run in :func:`_compute`, whose checkpoints honour
    the job's stop; and every output is written in one transaction (:func:`_persist`). The
    two database steps run under :func:`_until_stopped`, so a stop cancels them within
    :data:`_STOP_POLL_S` seconds. The job's engine checks that this process is still
    connected (:func:`_check_clients_of`), so a database step outlives no orphaned process.
    Every stage is reported through ``signals``, and the engine is disposed at the end
    whatever happened.

    Returns:
        The stored run with its timings and summary, or the failure with the stage it
        happened in. An ordinary error or a cancellation is never raised.
    """
    progress = _RunProgress(
        stage=_LOAD_STAGE,
        run_id=job.run_id,
        stop=_JobStop(signals, job.token),
        report=signals.report,
    )
    try:
        try:
            progress.enter(_LOAD_STAGE)
            _check_clients_of(get_engine())
            started = time.perf_counter()
            ctx = await _until_stopped(
                _load(
                    job.window,
                    job.config,
                    job.kind,
                    job.instant,
                    display_timezone=job.display_timezone,
                ),
                progress,
            )
            loaded = time.perf_counter()
            progress.begin("check_overrides")
            prepared, computed = _compute(job.run_id, ctx, job.config, progress)
            stored = await _until_stopped(_persist(job.run_id, prepared), progress)
            finished = time.perf_counter()
        finally:
            await _dispose_process_engine()
    except BaseException as error:
        # Interpreter exits propagate; everything else becomes an outcome that crosses back.
        if not isinstance(error, Exception | asyncio.CancelledError):
            raise
        return _failure(progress.stage, error)
    return _ProcessSuccess(
        stored=stored,
        summary=prepared.summary,
        load_ms=_ms(loaded - started),
        compute_ms=_ms(computed - loaded),
        persist_ms=_ms(finished - computed),
    )


def _execute_in_process(job: _AnalysisJob) -> _ProcessSuccess | _ProcessFailure:
    """Run one job in the analysis process: the executor's task entry.

    Binds the job's log context and runs :func:`_run_in_process` on an event loop of its
    own; the executor hands the outcome back to the API process.

    Raises:
        RuntimeError: When called in a process :func:`_init_analysis_process` did not
            prepare.
    """
    signals = _process_signals
    if signals is None:
        raise RuntimeError("the analysis process was not prepared by _init_analysis_process")
    with structlog.contextvars.bound_contextvars(**job.log_context):
        return asyncio.run(_run_in_process(job, signals))


def _environment_fingerprint() -> str:
    """The SHA-256 of the process environment, compared at every hand-off; never logged."""
    digest = hashlib.sha256()
    for name, value in sorted(os.environ.items()):
        for part in (name, value):
            digest.update(part.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
    return digest.hexdigest()


def _spawn(fingerprint: str) -> _AnalysisProcess:
    """Create the analysis process's executor; its worker starts with the first submission.

    The worker receives this process's pid, so it dies with this process
    (:func:`_bind_to_parent`).
    """
    context = multiprocessing.get_context("spawn")
    stop = context.Value("q", 0, lock=False)
    stage = context.Value("i", _STAGES.index(_LOAD_STAGE), lock=False)
    executor = ProcessPoolExecutor(
        max_workers=1,
        mp_context=context,
        initializer=_init_analysis_process,
        initargs=(stop, stage, os.getpid()),
    )
    return _AnalysisProcess(
        executor=executor, signals=_ProcessSignals(stop=stop, stage=stage), fingerprint=fingerprint
    )


def _forget_locked(process: _AnalysisProcess) -> None:
    """Forget ``process`` if current and shut it down without waiting; the lock is held."""
    global _process
    if _process is process:
        _process = None
    process.executor.shutdown(wait=False, cancel_futures=True)


def _discard(process: _AnalysisProcess) -> None:
    """Forget ``process`` if current and shut it down without waiting."""
    with _process_lock:
        _forget_locked(process)


def _current_process_locked() -> _AnalysisProcess:
    """Return the analysis process for the current environment; ``_process_lock`` is held.

    One spawned under another environment (a changed ``DATABASE_URL``, say) is shut down
    without waiting and replaced, so the analysis process always reads the database the
    calling process's lock connection reads.
    """
    global _process
    fingerprint = _environment_fingerprint()
    process = _process
    if process is not None and process.fingerprint != fingerprint:
        _log.info("analysis_process_replaced", reason="environment_changed")
        _forget_locked(process)
        process = None
    if process is None:
        process = _process = _spawn(fingerprint)
    return process


def _submit[T](
    submit: Callable[[_AnalysisProcess], Future[T]],
) -> tuple[_AnalysisProcess, Future[T]]:
    """Hand work to the analysis process through ``submit``; replace a broken one once.

    The analysis process is spawned on first use, and again when the environment changed.
    One whose executor broke while idle (its worker died) is discarded, and the work goes to
    a fresh one.

    Returns:
        The analysis process the work went to, and the work's future.

    Raises:
        BrokenProcessPool: When the fresh analysis process refuses it as well.
        RuntimeError: When the interpreter is shutting down.
    """
    with _process_lock:
        process = _current_process_locked()
        try:
            return process, submit(process)
        except BrokenProcessPool:
            _log.warning("analysis_process_replaced", reason="broken")
            _forget_locked(process)
        process = _current_process_locked()
        return process, submit(process)


def _hand_off(
    job: _AnalysisJob, process: _AnalysisProcess
) -> Future[_ProcessSuccess | _ProcessFailure]:
    """Submit ``job`` to ``process``, whose reported stage is reset to the context load."""
    process.signals.report(_LOAD_STAGE)
    return process.executor.submit(_execute_in_process, job)


def _job_log_context(run_id: int) -> dict[str, str | int | float | bool | None]:
    """The caller's bound log context, scalar values only, and ``run_id``: a job's context."""
    context: dict[str, str | int | float | bool | None] = {
        key: value
        for key, value in structlog.contextvars.get_contextvars().items()
        if value is None or isinstance(value, str | int | float | bool)
    }
    context["run_id"] = run_id
    return context


async def _in_analysis_process(
    job: _AnalysisJob, progress: _RunProgress
) -> tuple[_ProcessSuccess, asyncio.CancelledError | None]:
    """Run ``job`` in the analysis process and await it without blocking the event loop.

    The counterpart of :func:`_in_analysis_thread` for steps 4 to 8 of a run. The wait is
    cancellation-safe: the first cancellation of the caller sets the job's stop
    (:class:`_JobStop`), on which the analysis process stops at its next checkpoint or
    cancels the database step in flight; every cancellation is held until the job has
    finished. The caller holds the analysis lock around this wait, so the lock is never
    released while the job still runs.

    Returns:
        The stored run's outcome, and the cancellation that arrived meanwhile or ``None``:
        a run stored before the stop took hold stays stored, and the caller re-raises the
        cancellation once it has logged the run.

    Raises:
        asyncio.CancelledError: When the caller was cancelled and nothing was stored; an
            error other than the stop is chained as the cause.
        BrokenProcessPool: When the analysis process died. It is discarded, so the next
            run spawns a fresh one, and ``progress.stage`` is the stage it last reported.
        AnalysisProcessError: When the analysis process shut down before it ran the job,
            the job stopped although nobody cancelled it, or the job's error could not
            cross back intact.
        Exception: The job's own error, with the analysis process's traceback as its cause;
            ``progress.stage`` is the stage it failed in.
    """
    process, future = _submit(functools.partial(_hand_off, job))
    waited = asyncio.wrap_future(future)
    cancelled: asyncio.CancelledError | None = None
    # Only this function holds the future, which completes only once the job has returned,
    # raised or lost its process: from then on nothing runs in the process for this run.
    while not waited.done():
        try:
            await asyncio.wait((waited,))
        except asyncio.CancelledError as err:
            _JobStop(process.signals, job.token).set()
            if cancelled is None:
                cancelled = err
    error: BaseException
    if waited.cancelled():
        error = AnalysisProcessError("the analysis process shut down before it ran the run")
    elif (raised := waited.exception()) is not None:
        if isinstance(raised, BrokenProcessPool):
            _discard(process)
            progress.stage = process.signals.stage_name()
        error = raised
    else:
        outcome = waited.result()
        if isinstance(outcome, _ProcessSuccess):
            return outcome, cancelled
        progress.stage = outcome.stage
        if outcome.stopped and cancelled is not None:
            raise cancelled from None
        error = (
            AnalysisProcessError("the analysis process stopped a run nobody cancelled")
            if outcome.stopped
            else outcome.error
        )
        error.__cause__ = _RemoteTracebackError(outcome.remote_traceback)
    if cancelled is not None:
        raise cancelled from error
    raise error


async def start_analysis_process() -> int:
    """Spawn the API's analysis process now and wait until it is ready; return its pid.

    The API calls this at startup, so its first ``POST /runs`` does not wait for the spawn
    and its imports; without it, the first isolated run spawns the process. A running
    analysis process is reused while the environment is unchanged, and replaced otherwise.
    Logs ``analysis_process_started`` with the pid and ``startup_ms``.

    Returns:
        The analysis process's pid.

    Raises:
        BrokenProcessPool: When the analysis process died while starting (its
            initialisation failed, say); it is discarded, and the next run spawns another.
        OSError: When the process cannot be created.
        RuntimeError: When the interpreter is shutting down.
    """
    started = time.perf_counter()
    process, future = _submit(lambda current: current.executor.submit(_analysis_process_pid))
    try:
        pid = await asyncio.wrap_future(future)
    except BrokenProcessPool:
        _discard(process)
        raise
    _log.info("analysis_process_started", pid=pid, startup_ms=_ms(time.perf_counter() - started))
    return pid


async def stop_analysis_process() -> None:
    """Shut the API's analysis process down and wait until it has exited.

    The API calls this when it stops. A job not yet started is cancelled (its run fails with
    :class:`AnalysisProcessError`) and one running is awaited. Does nothing when no analysis
    process exists; otherwise logs ``analysis_process_stopped``.
    """
    global _process
    with _process_lock:
        process, _process = _process, None
    if process is None:
        return
    await asyncio.to_thread(process.executor.shutdown, wait=True, cancel_futures=True)
    _log.info("analysis_process_stopped")


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
    display_timezone: str | None = None,
    isolated: bool = False,
) -> AnalysisRun:
    """Compute one analysis run over ``window`` from stored data and persist it, frozen.

    AAP 0.5.1, 0.5.3. The steps are those of the module docstring: take the shared
    ``analysis`` lock (never taken by the caller), insert and commit the ``running`` row,
    claim the requests it serves and commit the claim, read the context in one snapshot,
    check the creative overrides, evaluate the rules in :data:`EVALUATION_ORDER`, apply
    precedence, and write every output with the run's ``succeeded`` state in one
    transaction. Any failure after the row exists marks it ``failed`` and re-raises; a run
    is never partial, and requests it claimed stay stamped with it.

    By default the override check, the rules, precedence and the rendering of the output
    rows run on the analysis thread, so the event loop stays free while the run computes.
    Cancelling the call stops that computation at its next checkpoint (before a rule
    module, precedence or row rendering); the lock is released only after the thread has
    stopped and the row is ``failed`` (the requests it claimed stay stamped with it), and
    then ``CancelledError`` propagates.

    With ``isolated=True`` (``POST /runs``) the context load, the computation and the
    persistence run in the API's analysis process instead, so they share neither the
    caller's GIL nor its CPU priority. The lock, the ``running`` row, the claim, the
    failure record and both log lines stay with the caller, which holds the lock until the
    analysis process has finished the run. Cancelling the call stops the run there at its
    next checkpoint or cancels its database step in flight, with the same guarantees. A run
    the analysis process already stored when the cancellation arrived stays ``succeeded``:
    its summary is logged and then ``CancelledError`` propagates.

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
        display_timezone: ``DISPLAY_TIMEZONE``, the IANA zone the instants inside the
            run's frozen flag messages are rendered in (the ``stale`` flag's "Data as of …");
            ``get_settings().display_timezone`` when ``None``. It never moves a day boundary.
        isolated: Run steps 4 to 8 in the API's analysis process (:func:`start_analysis_process`
            spawns it at API startup; the first isolated run otherwise), rather than on the
            caller's event loop and analysis thread. Only ``POST /runs`` passes ``True``.

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
            ``datetime``, ``display_timezone`` is not a string, or a request id or
            ``serve_through`` is not an ``int``.
        ValueError: For an unknown ``kind`` or ``trigger``, a naive ``now``, a
            ``display_timezone`` that is not a loadable IANA zone, a ``serve_through`` that
            is not a positive id, both ``serve_through`` and ``request_ids``, or a default
            run over a window other than the configured default.
        pydantic.ValidationError: When ``display_timezone`` is ``None`` and the settings
            cannot be loaded from the environment.
        sqlalchemy.exc.SQLAlchemyError: When the database fails.
        BrokenProcessPool: With ``isolated``, when the analysis process died during the run
            (the run is marked failed; the next isolated run spawns a fresh process).
        AnalysisProcessError: With ``isolated``, when the analysis process shut down before
            it ran the run, or the run's error could not cross back intact (the run is
            marked failed).
        asyncio.CancelledError: When the call is cancelled, once the computation has
            stopped and the run, if its row exists, is recorded ``failed``; or, with
            ``isolated``, once the analysis process stored the run ``succeeded``.
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
    zone = get_settings().display_timezone if display_timezone is None else display_timezone
    display_zone(zone, "display_timezone")
    log_reason = None if reason is None else reason[:_REASON_LOG_LIMIT]

    async with analysis_lock():
        started = time.perf_counter()
        config = thresholds if thresholds is not None else Thresholds.load()
        if kind == "default":
            _check_default_shape(window, config)
        progress = _RunProgress(stage="insert")
        cancelled: asyncio.CancelledError | None = None
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
            progress.stage = _LOAD_STAGE
            if isolated:
                handed_off = time.perf_counter()
                job = _AnalysisJob(
                    token=next(_job_tokens),
                    run_id=run.id,
                    window=window,
                    kind=kind,
                    config=config,
                    instant=instant,
                    display_timezone=zone,
                    log_context=_job_log_context(run.id),
                )
                done, cancelled = await _in_analysis_process(job, progress)
                finished = time.perf_counter()
                stored, summary = done.stored, done.summary
                load_ms = _ms(handed_off - started) + done.load_ms
                compute_ms, persist_ms = done.compute_ms, done.persist_ms
            else:
                ctx = await _load(window, config, kind, instant, display_timezone=zone)
                loaded = time.perf_counter()
                progress.stage = "check_overrides"
                prepared, computed = await _in_analysis_thread(
                    functools.partial(_compute, run.id, ctx, config, progress), progress.stop
                )
                stored = await _persist(run.id, prepared)
                finished = time.perf_counter()
                summary = prepared.summary
                load_ms = _ms(loaded - started)
                compute_ms, persist_ms = _ms(computed - loaded), _ms(finished - computed)
        except BaseException as exc:
            # From the flush of the running row on, every failure, cancellation included,
            # leaves that row failed before the lock is released.
            if progress.run_id is not None:
                await _record_failure(progress.run_id, exc, progress.stage, started)
            raise

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
        load_ms=load_ms,
        compute_ms=compute_ms,
        persist_ms=persist_ms,
        duration_ms=_ms(finished - started),
        **summary,
    )
    if cancelled is not None:
        # The analysis process stored the run before the stop took hold: the run stands,
        # and the caller still learns that it was cancelled.
        raise cancelled
    return stored
