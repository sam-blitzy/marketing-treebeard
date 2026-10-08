"""The worker process: ``python -m ads_engine.worker.main``.

AAP 0.6.2 (entry point of the ``worker`` container), 0.5.1 ("Worker entry and CLI"), 0.3.2
(the worker owns all external I/O, runs the sync schedule and precomputes analysis runs; "a
changed threshold file detected at startup" enqueues a default rerun), 0.2.2 (a single active
worker, enforced by a PostgreSQL advisory lock) and 0.11.2 ("First start").

How it runs. The root ``docker-compose.yml`` service ``worker`` runs this module from the
``backend/Dockerfile`` image with working directory ``/app``, where ``config/`` is bind-mounted
read-only. Compose starts it only after the one-shot ``migrate`` service (``ads-engine
migrate`` and ``ads-engine ensure-admin``) succeeded, so the worker assumes the schema exists
and never migrates. On the host it runs from ``backend/``, where ``../config`` resolves.

Lifecycle:

1. :func:`main` loads the settings (an invalid environment exits with code 1, naming the
   variables but never their values), configures redacted JSON logging on stdout and runs
   the event loop.
2. The scheduler (:class:`~ads_engine.sync.scheduler.Scheduler`) takes the scheduler
   advisory lock. A second worker waits as a standby and takes over when the active one's
   database session ends.
3. Holding the lock and before its first tick, the scheduler awaits :func:`startup`:
   content assets from ``config/content_assets.yaml``, the changed-configuration check, the
   source bootstrap (every source live or simulated, with an active dataset; with no
   credentials, all simulated with the reason ``credentials not configured``) and the
   default-window analysis, so the first default run exists before the first scheduled
   cycle.
4. The scheduler then ticks every 30 s until SIGTERM or SIGINT cancels it.

Boundaries: the worker never serves HTTP and exposes no health endpoint (the API's
``/healthz`` and the structured logs are the monitoring surface), and it never writes to
LinkedIn, Google Ads or HubSpot: the product only recommends. Logs never carry emails,
secrets, tokens, the webhook URL or the n8n header value.

Importing this module has no side effects: no settings are read, no logging is configured,
no engine is built and no signal handler is installed until :func:`main` runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine import __version__
from ads_engine.analysis.lock import RunInProgress
from ads_engine.analysis.runner import (
    RunNoLongerRunning,
    default_window,
    latest_default_run,
    run_analysis,
)
from ads_engine.analysis.thresholds import ConfigError, Thresholds
from ads_engine.analysis.window import AnalysisWindow
from ads_engine.db.models.analysis import AnalysisRequest
from ads_engine.db.repositories import enqueue_analysis_request, sync_content_assets
from ads_engine.db.session import dispose_engine, session_scope
from ads_engine.logging import configure_logging, get_logger, logged_as
from ads_engine.settings import SOURCES, Settings, get_settings
from ads_engine.sync.mode import BootstrapReport, bootstrap_sources
from ads_engine.sync.scheduler import JOB_FAILURES, Scheduler

__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "REASON_CONFIG_CHANGED",
    "REASON_STARTUP",
    "SCHEDULER_TASK_NAME",
    "main",
    "startup",
]

# A lazy structlog proxy: it reads no settings and binds to the configuration that
# configure_logging() installs when main() runs. The name is spelled out because
# `python -m ads_engine.worker.main` runs this module as `__main__`, and the worker's log
# lines should carry the same logger name however the module was loaded.
_log = get_logger("ads_engine.worker.main")

REASON_CONFIG_CHANGED: Final = "config_changed"
"""``analysis_request.reason`` when the configuration hash differs from the latest default run."""

REASON_STARTUP: Final = "startup"
"""The startup run's reason, and the reason of the request queued when it cannot run now."""

SCHEDULER_TASK_NAME: Final = "ads-engine-scheduler"
"""The name of the asyncio task that runs :meth:`Scheduler.run_forever`."""

EXIT_OK: Final = 0
"""Exit code of a graceful shutdown (SIGTERM or SIGINT)."""

EXIT_FAILURE: Final = 1
"""Exit code of an invalid environment, an invalid configuration file or a fatal defect."""

_STOP_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)


# ---------------------------------------------------------------------------------------------
# Startup: run once by the scheduler, holding its lock, before the first tick
# ---------------------------------------------------------------------------------------------


async def startup(settings: Settings | None = None) -> None:
    """Prepare the active worker before its first scheduled cycle.

    :meth:`Scheduler.run_forever` awaits this once it holds the scheduler advisory lock and
    before its first tick, so it runs only on the single active worker (AAP 0.2.2) and the
    first default run always precedes the first scheduled cycle (AAP 0.11.2). In order:

    1. Load ``config/`` and replace the content-asset tables with the white paper registry.
    2. Compare the configuration hash with the latest succeeded default run's and enqueue a
       ``config_changed`` analysis request when they differ (AAP 0.3.2). With no prior
       default run nothing is enqueued: step 4 computes the first one.
    3. Bootstrap every source (:func:`~ads_engine.sync.mode.bootstrap_sources`): resolve it
       live or simulated and build, backfill and activate a dataset where none of that mode
       is active, with the companion CRM dataset of a simulated LinkedIn and the baseline
       decisions of the first entity sync. A failure in
       :data:`~ads_engine.sync.scheduler.JOB_FAILURES` is logged and startup continues:
       every scheduler tick re-runs ``ensure_dataset``, so the build is retried rather than
       crash-looping the worker.
    4. Compute the default-window analysis, serving every pending ``analysis_request``.
       Behind another holder of the ``analysis`` lock, after a
       :data:`~ads_engine.sync.scheduler.JOB_FAILURES` error, or when the run lost its lock,
       a ``startup`` request is queued instead, so the scheduler's coalescing loop computes
       the run shortly after.

    Args:
        settings: The process settings; :func:`~ads_engine.settings.get_settings` when
            ``None``.

    Raises:
        ConfigError: When a configuration file is missing or invalid (for example
            ``window.presets`` lacking ``window.default_days``, or a form listed under two
            assets); the worker exits with code 1.
        sqlalchemy.exc.SQLAlchemyError: When the database fails during steps 1 or 2.
    """
    resolved = settings if settings is not None else get_settings()
    thresholds = await _sync_config_and_assets()
    await _check_config_change(thresholds)
    await _bootstrap(resolved)
    await _startup_analysis(thresholds)


async def _sync_config_and_assets() -> Thresholds:
    """Load the configuration and make the content-asset tables equal its registry.

    Returns:
        The loaded configuration, reused by every later startup step so the startup run's
        ``config_hash`` is exactly the one compared in :func:`_check_config_change`.

    Raises:
        ConfigError: When a configuration file is missing or invalid.
        ValueError: When the registry repeats a slug or URN (already rejected by the load).
    """
    thresholds = Thresholds.load()
    rows = thresholds.content_asset_rows()
    async with session_scope() as session:
        await sync_content_assets(session, rows)
    _log.info(
        "content_assets_synced",
        assets=len(rows),
        config_hash=thresholds.config_hash,
        config_dir=str(thresholds.source_dir),
    )
    return thresholds


async def _check_config_change(thresholds: Thresholds) -> None:
    """Enqueue a ``config_changed`` request when the configuration changed since the last run.

    The request and the read that decided it share one transaction; the ``config_changed``
    line is logged only once the request has committed.

    Args:
        thresholds: The configuration loaded at this startup.
    """
    async with session_scope() as session:
        latest = await latest_default_run(session)
        if latest is None:
            _log.info("config_no_prior_default_run", config_hash=thresholds.config_hash)
            return
        previous_hash = latest.config_hash
        run_id = latest.id
        if previous_hash == thresholds.config_hash:
            _log.info("config_unchanged", config_hash=thresholds.config_hash, run_id=run_id)
            return
        await enqueue_analysis_request(session, REASON_CONFIG_CHANGED)
    _log.info(
        "config_changed",
        previous_hash=previous_hash,
        config_hash=thresholds.config_hash,
        run_id=run_id,
    )


def _bootstrap_summary(report: BootstrapReport) -> dict[str, dict[str, object]]:
    """Describe each source's bootstrap for the log: modes, reasons and dataset ids only.

    A :class:`~ads_engine.sync.mode.BootstrapReport` never carries credentials; the failure
    text passes through the logging redaction like every other value.

    Args:
        report: What the bootstrap did.

    Returns:
        One entry per source, in display order.
    """
    summary: dict[str, dict[str, object]] = {}
    for source in SOURCES:
        resolved = report.modes.get(source)
        active = report.active.get(source)
        summary[source] = {
            "mode": None if resolved is None else resolved.mode,
            "reason": None if resolved is None else resolved.reason,
            "fallback": None if resolved is None else resolved.fallback,
            "dataset_id": None if active is None else active.dataset_id,
            "dataset_mode": None if active is None else active.mode,
            "companion_id": None if active is None else active.companion_id,
            "built": report.outcomes.get(source) is not None,
            "failure": report.failures.get(source),
        }
    return summary


async def _bootstrap(settings: Settings) -> None:
    """Give every source an active dataset of its resolved mode; never fatal on its own.

    ``bootstrap_sources`` already isolates each source's failure. What still escapes it, a
    database outage included, is in :data:`JOB_FAILURES` and is logged here: the scheduler's
    ticks retry ``ensure_dataset`` and reconnect, so the worker keeps running.

    Args:
        settings: The process settings.
    """
    try:
        report = await bootstrap_sources(settings)
    except JOB_FAILURES as exc:
        _log.exception("startup_bootstrap_failed", error=type(exc).__name__)
        return
    summary = _bootstrap_summary(report)
    failed = sorted(report.failures)
    if failed:
        _log.warning("sources_bootstrapped", sources=summary, failed=failed)
    else:
        _log.info("sources_bootstrapped", sources=summary, failed=failed)


async def _pending_request_ids(session: AsyncSession) -> tuple[int, ...]:
    """Return the ids of every pending ``analysis_request`` (``picked_at IS NULL``), by id.

    Args:
        session: The caller's session; only read.

    Returns:
        The pending ids, ascending.
    """
    result = await session.scalars(
        select(AnalysisRequest.id)
        .where(AnalysisRequest.picked_at.is_(None))
        .order_by(AnalysisRequest.id)
    )
    return tuple(int(request_id) for request_id in result)


async def _requeue_startup_analysis() -> None:
    """Queue a ``startup`` analysis request for the scheduler's coalescing loop.

    A database failure here is logged and not raised: the scheduler's lock check detects an
    outage and reconnects, and the next completed sync or dataset activation enqueues a
    request of its own, so the default run is only delayed, never lost for good.
    """
    try:
        async with session_scope() as session:
            await enqueue_analysis_request(session, REASON_STARTUP)
    except JOB_FAILURES as exc:
        _log.exception("startup_analysis_requeue_failed", error=type(exc).__name__)
        return
    _log.info("startup_analysis_requeued", reason=REASON_STARTUP)


async def _startup_analysis(thresholds: Thresholds) -> None:
    """Compute the default-window run before the first tick, serving every pending request.

    The window comes from :func:`~ads_engine.analysis.runner.default_window`, the helper the
    scheduler uses, so startup and event-driven default runs compute identical windows. The
    pending request ids are passed to ``run_analysis``, which stamps them with the run in its
    own transaction, so the scheduler does not recompute the same run on its first tick.
    ``run_analysis`` takes the shared ``analysis`` lock itself; this function never does.

    Args:
        thresholds: The configuration loaded at this startup; the run records its hash.
    """
    at = datetime.now(UTC)
    request_ids: tuple[int, ...] = ()
    try:
        async with session_scope() as session:
            request_ids = await _pending_request_ids(session)
        window: AnalysisWindow = await default_window(now=at, thresholds=thresholds)
        run = await run_analysis(
            window,
            "default",
            "scheduled",
            reason=REASON_STARTUP,
            request_ids=request_ids,
            thresholds=thresholds,
            now=at,
        )
    except RunInProgress:
        # The API (POST /runs) or the CLI (analyze) holds the analysis lock. The pending
        # requests were not touched; the queued one guarantees a default run once it frees.
        _log.info("startup_analysis_skipped", reason="run_in_progress", requests=len(request_ids))
        await _requeue_startup_analysis()
        return
    except RunNoLongerRunning as exc:
        # The run's lock connection was lost and another process marked the row failed; the
        # runner stamped the requests as served by that failed run, so queue a fresh one, as
        # the scheduler's own coalescing step does.
        _log.error("startup_analysis_lock_lost", run_id=exc.run_id, status=exc.status)
        await _requeue_startup_analysis()
        return
    except JOB_FAILURES as exc:
        owner = logged_as(exc)
        if owner is None:
            _log.exception(
                "startup_analysis_failed", error=type(exc).__name__, requests=len(request_ids)
            )
        else:
            # The runner logged the failure with its traceback (analysis_run_failed).
            _log.warning(
                "startup_analysis_failed",
                error=type(exc).__name__,
                requests=len(request_ids),
                logged_as=owner,
            )
        await _requeue_startup_analysis()
        return
    _log.info(
        "startup_analysis_succeeded",
        run_id=run.id,
        kind=run.kind,
        status=run.status,
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        days=window.days,
        config_hash=run.config_hash,
        requests=len(request_ids),
    )


# ---------------------------------------------------------------------------------------------
# Process lifetime: event loop, signals and engine disposal
# ---------------------------------------------------------------------------------------------


def _install_stop_handlers(
    loop: asyncio.AbstractEventLoop, handler: Callable[[signal.Signals], None]
) -> list[signal.Signals]:
    """Route SIGTERM and SIGINT to ``handler`` on the running loop.

    Python runs as PID 1 in the container, where SIGTERM has no default action, so this
    handler is what makes ``docker compose stop`` graceful. Event loops without signal
    support (``NotImplementedError``), and loops outside the main thread (``RuntimeError``),
    leave the signal's default behaviour in place; that is logged once per signal.

    Args:
        loop: The running event loop.
        handler: Called on the loop with the signal received.

    Returns:
        The signals whose handler was installed, for :func:`_remove_stop_handlers`.
    """
    installed: list[signal.Signals] = []
    for sig in _STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, handler, sig)
        except (NotImplementedError, RuntimeError) as exc:
            _log.warning("worker_signal_handler_unavailable", signal=sig.name, error=str(exc))
            continue
        installed.append(sig)
    return installed


def _remove_stop_handlers(loop: asyncio.AbstractEventLoop, installed: list[signal.Signals]) -> None:
    """Restore the default disposition of every signal :func:`_install_stop_handlers` took."""
    for sig in installed:
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(sig)


def _cancelled_from_outside() -> bool:
    """Return whether the current task itself has a pending cancellation request."""
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0


async def _run(settings: Settings) -> int:
    """Own the event loop's lifetime: the scheduler task, signal handling and engine disposal.

    The scheduler runs as the task :data:`SCHEDULER_TASK_NAME` with :func:`startup` as its
    ``on_start``. SIGTERM or SIGINT logs ``worker_stopping`` and cancels that task; a second
    signal while stopping only logs ``worker_stop_already_requested``.

    Graceful shutdown: the cancellation interrupts the scheduler at its next await. An
    in-flight ``session_scope()`` rolls back, so at most the current batch is redone, and no
    state is lost: lead matching commits per batch and claims rows ``FOR UPDATE SKIP
    LOCKED``, upserts are idempotent and snapshots are append-only (AAP 0.3.2, 0.3.3). The
    scheduler's dedicated connection runs ``pg_advisory_unlock_all()`` on exit, and the lock
    dies with its database session in any case, so a standby worker takes over at once.

    Args:
        settings: The process settings.

    Returns:
        :data:`EXIT_OK` after a signal-requested shutdown, :data:`EXIT_FAILURE` when a
        configuration file is invalid (``worker_config_invalid``).

    Raises:
        asyncio.CancelledError: When this coroutine itself is cancelled, not by a signal.
        Exception: Any other failure, after logging it as ``worker_fatal``: with its
            traceback, or as a warning naming the event that already logged it in full.
    """
    loop = asyncio.get_running_loop()
    stopping = False
    installed: list[signal.Signals] = []
    task: asyncio.Task[None] | None = None

    async def _on_start() -> None:
        await startup(settings)

    def _request_stop(sig: signal.Signals) -> None:
        nonlocal stopping
        if stopping:
            _log.info("worker_stop_already_requested", signal=sig.name)
            return
        stopping = True
        _log.info("worker_stopping", signal=sig.name)
        if task is not None:
            task.cancel()

    try:
        scheduler = Scheduler(settings)
        task = asyncio.create_task(
            scheduler.run_forever(on_start=_on_start), name=SCHEDULER_TASK_NAME
        )
        installed = _install_stop_handlers(loop, _request_stop)
        await task
    except asyncio.CancelledError:
        if stopping and not _cancelled_from_outside():
            return EXIT_OK
        raise
    except ConfigError as exc:
        _log.error(
            "worker_config_invalid",
            config_file=exc.file,
            config_key=exc.key,
            detail=exc.detail,
            error=str(exc),
        )
        return EXIT_FAILURE
    except Exception as exc:
        owner = logged_as(exc)
        if owner is None:
            _log.exception("worker_fatal")
        else:
            # Its owner (an analysis run's analysis_run_failed) logged the traceback already.
            _log.warning("worker_fatal", error=type(exc).__name__, logged_as=owner)
        raise
    finally:
        _remove_stop_handlers(loop, installed)
        if task is not None and not task.done():
            # Reached only when this coroutine failed before awaiting the task to its end;
            # wait() never raises the task's own outcome into this cleanup.
            task.cancel()
            await asyncio.wait((task,))
        await dispose_engine()
        _log.info("worker_stopped")
    return EXIT_OK


def _report_invalid_settings(exc: ValidationError) -> None:
    """Write one line per invalid environment variable to stderr: its name and the problem.

    Built with ``include_input=False``, so a malformed secret is never echoed.

    Args:
        exc: The settings validation error.
    """
    errors = exc.errors(include_input=False, include_url=False)
    sys.stderr.write(f"ads-engine worker: invalid settings ({len(errors)}); not started\n")
    for error in errors:
        location = ".".join(str(part) for part in error["loc"]) or "settings"
        sys.stderr.write(f"  {location.upper()}: {error['msg']}\n")
    sys.stderr.flush()


def main() -> None:
    """Run the worker until SIGTERM or SIGINT; the ``worker`` container's command.

    Raises:
        SystemExit: With :data:`EXIT_FAILURE` when the environment is invalid, a
            configuration file is invalid or a fatal defect ended the worker; nothing is
            raised after a graceful shutdown.
    """
    try:
        settings = get_settings()
    except ValidationError as exc:
        _report_invalid_settings(exc)
        raise SystemExit(EXIT_FAILURE) from None
    configure_logging(settings=settings)
    _log.info(
        "worker_starting",
        version=__version__,
        data_mode=settings.data_mode,
        display_timezone=settings.display_timezone,
        backfill_days=settings.backfill_days,
    )
    try:
        exit_code = asyncio.run(_run(settings))
    except Exception as exc:
        # _run logged the failure as worker_fatal: with its traceback redacted, or naming the
        # event that already logged that traceback (logged_as). Exiting with a code instead
        # of letting it reach the interpreter keeps the unredacted default traceback off
        # stderr; the container restart policy acts on the exit code alike.
        _log.error("worker_exited", error=type(exc).__name__, exit_code=EXIT_FAILURE)
        raise SystemExit(EXIT_FAILURE) from exc
    if exit_code != EXIT_OK:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
