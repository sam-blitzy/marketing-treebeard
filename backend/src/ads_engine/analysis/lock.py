"""The shared ``analysis`` advisory lock: one analysis run at a time, whoever starts it.

AAP 0.2.2 and 0.5.3 ("Run selection and the analysis lock"). The API (``POST /runs``), the
worker (default-window runs from the ``analysis_request`` queue) and the CLI
(``ads-engine analyze``) all compute runs through ``analysis.runner.run_analysis``, and that
function alone enters :func:`analysis_lock`; no entry point calls it directly. The lock is a
session-level ``pg_try_advisory_lock`` on :data:`ANALYSIS_LOCK_KEY`, held on a dedicated
``AUTOCOMMIT`` connection from :func:`ads_engine.db.session.dedicated_connection` for the
whole run. The partial unique index ``uq_analysis_run_single_running`` on ``analysis_run``
is the second, database-enforced guard: at most one row is ever ``running``.

When the lock is held elsewhere, :func:`analysis_lock` raises :class:`RunInProgress` at
once; it never waits. Each entry point maps that one exception to its own behaviour:

* the API answers 409 with ``{"error": {"code": "run_in_progress", ...}}``;
* the worker leaves its ``analysis_request`` rows pending for the next tick;
* the CLI exits with code 3.

Crash recovery. A session-level advisory lock lives exactly as long as the database session
that took it. A process that crashes mid-run therefore releases the lock the moment
PostgreSQL ends its session, but leaves its ``analysis_run`` row ``running``. Whoever takes
the lock next knows that no live run exists, so it marks every ``running`` row ``failed``
before its own run starts.

Example, as ``run_analysis`` uses it::

    async with analysis_lock():
        async with session_scope() as session:
            run = AnalysisRun(status="running", ...)
            session.add(run)
        ...  # compute the sections, then persist them and mark the run succeeded

The yielded connection exists to hold the lock. It is in ``AUTOCOMMIT`` mode, so a run
writes its outputs through its own :func:`~ads_engine.db.session.session_scope`, where they
commit in one transaction. Should the lock connection be lost mid-run (the server ends that
session), another process may take the lock and mark the row ``failed``; the runner's final
status update is therefore conditioned on the row still being ``running``.
"""

import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar, Final

from sqlalchemy import func, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.db.session import dedicated_connection
from ads_engine.logging import get_logger

__all__ = ["ANALYSIS_LOCK_KEY", "RunInProgress", "analysis_lock"]

_log = get_logger(__name__)

# The name the analysis lock key is derived from. `sync/scheduler.py` derives
# `SCHEDULER_LOCK_KEY` the same way from "ads_engine:scheduler", so the two keys differ.
_ANALYSIS_LOCK_NAME: Final = "ads_engine:analysis"


def _derive_key(name: str) -> int:
    """Derive a deterministic signed 64-bit advisory-lock key from a lock name.

    The key is the first 8 bytes of the SHA-256 digest of the UTF-8 name, read big-endian as
    a signed integer, so it always fits PostgreSQL's ``bigint`` and is identical in every
    process, release and host. Deriving keys from names, rather than picking literals, keeps
    each lock's key defined once next to its owner and makes collisions between differently
    named locks vanishingly unlikely.

    Example::

        >>> _derive_key("ads_engine:analysis") == _derive_key("ads_engine:analysis")
        True

    Args:
        name: The lock's name, for example ``"ads_engine:analysis"``.

    Returns:
        An integer in ``[-2**63, 2**63)``.
    """
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


ANALYSIS_LOCK_KEY: Final[int] = _derive_key(_ANALYSIS_LOCK_NAME)
"""The advisory-lock key of the shared ``analysis`` lock (AAP 0.5.3).

Derived as ``int.from_bytes(sha256(b"ads_engine:analysis").digest()[:8], "big",
signed=True)``: deterministic and within PostgreSQL ``bigint``. Every lock key is a constant
defined once, by its owner. The scheduler's single-worker lock,
``sync/scheduler.SCHEDULER_LOCK_KEY``, is derived the same way from the different name
``"ads_engine:scheduler"`` and must never equal this key; the scheduler asserts that at
import. Advisory locks are scoped to the database, so separate databases (one per test run or
deployment) never contend for it.
"""

_RUN_IN_PROGRESS_MESSAGE: Final = "An analysis run is already in progress"

# Lock statements. `pg_try_advisory_lock` returns at once with true or false; it never
# queues behind the holder.
_TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK: Final = text("SELECT pg_advisory_unlock(:key)")

# Crash recovery: run only while the lock is held, when no `running` row can belong to a
# live run. It returns the ids it marked, for the log.
_FAIL_ORPHANED_RUNS: Final = (
    update(AnalysisRun)
    .where(AnalysisRun.status == "running")
    .values(status="failed", finished_at=func.now())
    .returning(AnalysisRun.id)
)


class RunInProgress(RuntimeError):  # noqa: N818 - name fixed by the analysis-lock contract
    """Another process (or task) holds the ``analysis`` lock, so no run may start now.

    Raised by :func:`analysis_lock` and passed through ``analysis.runner.run_analysis``
    unchanged. The API maps it to 409 with :attr:`code` as the error code, the worker leaves
    its ``analysis_request`` rows pending, and the CLI exits with code 3 (AAP 0.5.3).

    Attributes:
        code: The machine error code of the API's 409 response, ``"run_in_progress"``.
        message: The human-readable message, also the exception's ``str()``.
    """

    code: ClassVar[str] = "run_in_progress"

    def __init__(self, message: str = _RUN_IN_PROGRESS_MESSAGE) -> None:
        super().__init__(message)
        self.message: str = message


async def _fail_orphaned_runs(conn: AsyncConnection) -> list[int]:
    """Mark every ``running`` analysis run ``failed``; the caller must hold the lock.

    Holding the ``analysis`` lock proves that no other session runs an analysis, because
    every run executes under that lock and the lock dies with its session. Any ``running``
    row is therefore left over from a crashed process (AAP 0.5.3 crash recovery). The
    connection is in ``AUTOCOMMIT`` mode, so the update is a short transaction of its own
    and commits before the new run inserts its ``running`` row, which the partial unique
    index would otherwise reject.

    Args:
        conn: The dedicated connection that holds the ``analysis`` lock.

    Returns:
        The ids of the runs marked ``failed``, in ascending order; empty when none was left.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: When the update fails; it is never swallowed.
    """
    result = await conn.execute(_FAIL_ORPHANED_RUNS)
    run_ids = sorted(int(run_id) for run_id in result.scalars())
    if run_ids:
        _log.warning("analysis_orphaned_runs_failed", count=len(run_ids), run_ids=run_ids)
    return run_ids


async def _release(conn: AsyncConnection, held_since: float) -> None:
    """Release the ``analysis`` lock explicitly at the end of the block.

    Runs while the block's own exception, if any, propagates, so a database error here is
    logged and never replaces that exception. The release is guaranteed regardless:
    :func:`~ads_engine.db.session.dedicated_connection` runs ``pg_advisory_unlock_all()``
    on exit and invalidates the connection, closing its database session, when that fails.
    An invalidated connection has already closed its session, which released the lock, so
    nothing is sent on it. Cancellation is not caught and propagates.

    Args:
        conn: The dedicated connection that took the lock.
        held_since: The :func:`time.monotonic` reading taken when the lock was acquired.
    """
    held_ms = round((time.monotonic() - held_since) * 1000)
    if conn.invalidated:
        _log.warning(
            "analysis_lock_released", held_ms=held_ms, via="session_end", reason="invalidated"
        )
        return
    try:
        released = await conn.scalar(_UNLOCK, {"key": ANALYSIS_LOCK_KEY})
    except SQLAlchemyError as exc:
        # The connection's exit unlocks all or invalidates; the lock cannot outlive it.
        _log.exception("analysis_lock_release_failed", error=type(exc).__name__, held_ms=held_ms)
        return
    if released:
        _log.info("analysis_lock_released", held_ms=held_ms)
    else:
        # The session no longer held the lock, for example because the block released it
        # itself. Nothing remains to release.
        _log.warning("analysis_lock_not_held_at_release", held_ms=held_ms)


@asynccontextmanager
async def analysis_lock() -> AsyncIterator[AsyncConnection]:
    """Hold the shared ``analysis`` advisory lock for the duration of the block.

    Steps (AAP 0.5.3):

    1. Check out a dedicated ``AUTOCOMMIT`` connection and try
       ``pg_try_advisory_lock(ANALYSIS_LOCK_KEY)``. When another session holds it, log
       ``analysis_lock_unavailable`` and raise :class:`RunInProgress` without waiting.
    2. Holding the lock, mark every ``running`` ``analysis_run`` row ``failed``: it was
       left by a crashed process, since a live run would still hold the lock.
    3. Yield the connection; the run executes while it stays checked out.
    4. On exit, whether the block succeeded, raised or was cancelled, release the lock with
       ``pg_advisory_unlock``; the dedicated connection then also runs
       ``pg_advisory_unlock_all()`` before it returns to the pool.

    Each call checks out its own connection, so two tasks of one process contend for the
    lock exactly as two processes do: session-level advisory locks are re-entrant only
    within one database session. Exceptions raised by the block propagate unchanged.

    Example::

        try:
            async with analysis_lock():
                ...  # insert the running row, compute, persist
        except RunInProgress as exc:
            ...  # API: 409 exc.code; worker: leave requests pending; CLI: exit 3

    Yields:
        The dedicated :class:`~sqlalchemy.ext.asyncio.AsyncConnection` holding the lock.

    Raises:
        RunInProgress: When the lock is held by another session.
        sqlalchemy.exc.SQLAlchemyError: When the database cannot be reached or a lock or
            recovery statement fails.
    """
    async with dedicated_connection() as conn:
        acquired = await conn.scalar(_TRY_LOCK, {"key": ANALYSIS_LOCK_KEY})
        if not acquired:
            _log.info("analysis_lock_unavailable", code=RunInProgress.code)
            raise RunInProgress
        held_since = time.monotonic()
        _log.info("analysis_lock_acquired")
        try:
            await _fail_orphaned_runs(conn)
            yield conn
        finally:
            await _release(conn, held_since)
