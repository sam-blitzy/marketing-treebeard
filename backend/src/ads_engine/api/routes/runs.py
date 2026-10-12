"""``/runs``: analysis run metadata and on-demand custom-window runs.

AAP 0.5.3 (``GET /runs/latest``, ``GET /runs/{id}``, ``GET /runs?kind=custom&limit=``,
``POST /runs``; "Run selection and the analysis lock"), 0.3.2 (the API computes custom-window
runs from stored data only, under the shared analysis lock, and never calls LinkedIn, n8n or
HubSpot) and 0.5.4.1 (window bounds). Paths are relative to ``/api/v1``;
``ads_engine.api.app.create_app`` includes :data:`router` with that prefix and with the CSRF
dependency, so ``POST /runs`` needs ``X-Requested-With: ads-engine`` without this module
repeating the check.

* ``GET /runs/latest`` returns the latest **succeeded** ``kind=default`` run, the one the
  ``/actions`` page shows by default. A custom run, even a manual 90-day one, never stands in
  for it, and a running or failed default run never replaces the last good one.
* ``GET /runs/{run_id}`` returns one run in any status; the client reads ``status``. The UI
  addresses a custom run as ``/actions?run=<id>``.
* ``GET /runs`` lists the newest runs of one kind (``custom`` by default), newest first, in
  every status.
* ``POST /runs`` computes a ``kind=custom`` run synchronously from stored data and answers
  201 with it, even when the window equals the default.

Errors: a missing run is 404 ``not_found`` (:func:`~ads_engine.api.deps.not_found`). Domain
exceptions are deliberately not caught here; ``api/app.py`` renders them:
:class:`~ads_engine.analysis.lock.RunInProgress` as 409 ``run_in_progress`` and
:class:`~ads_engine.analysis.window.InvalidWindow` as 422 with its own code
(``end_before_start``, ``end_after_default_end``, ``start_before_earliest``, ``too_long`` or
``too_short``), raised before the run takes the lock or writes a row.

Decisions taken in this module, each stated where its code lives:

* **No lock and no window rules here.** :func:`ads_engine.analysis.runner.run_analysis` takes
  the shared ``analysis`` advisory lock itself, for the API, the worker and the CLI alike;
  taking it here as well would make the run fail against its own route. The window bounds
  (end no later than the default window end, start no earlier than the earliest date the
  analysis's lookbacks allow, 1 day to ``window.max_days``) live once, in
  ``AnalysisWindow.custom``.
* **One configuration per request.** The application's loaded thresholds
  (:data:`~ads_engine.api.deps.ThresholdsDep`) validate the window and drive the run, and the
  request's clock (:data:`~ads_engine.api.deps.NowDep`) fixes both the default window end and
  the run instant, so they can never disagree.
* **Run views are frozen with the run.** Every ``Run`` this module returns reads
  ``default_days``, ``trusted_min_days`` and ``short_window`` from the window settings the run
  stored when it was computed, never from today's configuration, so the read routes take no
  thresholds.
* **The request's read-only transaction ends before the run.** The run commits through
  sessions of its own; the request session has only read (the signed-in user and the window
  inputs), so it is rolled back before the computation to return its pooled connection rather
  than hold it idle in a transaction for the length of the run.
* **The run computes in the API's analysis process.** ``run_analysis(..., isolated=True)``
  takes the lock and writes the ``running`` row here, then loads, computes and persists the
  run in the API's analysis process, a child with its own GIL at a lower CPU priority, so
  this process keeps serving every other request at its usual latency while a large window
  computes, and a contending ``POST /runs`` is answered 409 at once.

The module imports nothing from ``connectors``, ``sync`` or ``simulation``, and it logs no
email, secret or token: events carry run ids, dates and the user id only.

Example::

    POST /api/v1/runs      (X-Requested-With: ads-engine)
    {"window_start": "2026-07-09", "window_end": "2026-10-06"}
    201 {"id": 42, "kind": "custom", "window_start": "2026-07-09", "days": 90, ...}
    409 {"error": {"code": "run_in_progress", "message": "..."}}
    422 {"error": {"code": "end_after_default_end", "message": "..."}}
"""

from typing import Annotated, Final

from fastapi import APIRouter, Path, Query

from ads_engine.analysis import runner
from ads_engine.api import schemas
from ads_engine.api.deps import (
    ApiError,
    NowDep,
    SessionDep,
    SettingsDep,
    ThresholdsDep,
    UserDep,
    not_found,
)
from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.logging import get_logger
from ads_engine.settings import Settings

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "create_run",
    "get_latest_run",
    "get_run_by_id",
    "list_runs_route",
    "router",
]

router = APIRouter(tags=["runs"])
"""The ``runs`` routes, included by ``create_app`` under ``/api/v1``."""

_log = get_logger(__name__)

DEFAULT_LIMIT: Final[int] = 20
"""How many runs ``GET /runs`` returns when ``limit`` is omitted."""

MAX_LIMIT: Final[int] = 100
"""The largest ``limit`` ``GET /runs`` accepts; a larger one is a 422."""

_NO_RUN_YET: Final = "No analysis run yet"
"""The 404 message of ``GET /runs/latest`` before the first default run succeeds."""


def _to_view(run: AnalysisRun, settings: Settings) -> schemas.Run:
    """Render a stored run as the ``Run`` response every route of this module returns.

    Args:
        run: The stored ``analysis_run`` row, whose stored ``default_days`` and
            ``trusted_min_days`` fill the window settings of the view.
        settings: The application's settings (``DISPLAY_TIMEZONE``).

    Returns:
        The run view.
    """
    return schemas.Run.from_row(run, display_timezone=settings.display_timezone)


# ``/runs/latest`` is declared before ``/runs/{run_id}``, and the id is an ``int``, so the
# literal path ``latest`` can never be read as a run id.
@router.get(
    "/runs/latest",
    response_model=schemas.Run,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def get_latest_run(
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> schemas.Run:
    """Return the latest succeeded default run, the one ``/actions`` shows by default.

    Only ``kind=default`` runs with ``status=succeeded`` qualify, newest first, so a custom
    run (a manual 90-day window, or one equal to the default) never stands in for the
    default, and a running or failed default run never replaces the last good one.

    Args:
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session; only read.
        settings: The application's settings, for ``DISPLAY_TIMEZONE``.

    Returns:
        The run.

    Raises:
        ApiError: 404 ``not_found`` before the first default run has succeeded.
    """
    run = await runner.latest_default_run(session)
    if run is None:
        raise ApiError(404, "not_found", _NO_RUN_YET)
    return _to_view(run, settings)


@router.get(
    "/runs/{run_id}",
    response_model=schemas.Run,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def get_run_by_id(
    run_id: Annotated[int, Path(gt=0, le=schemas.BIGINT_MAX, description="The run id.")],
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> schemas.Run:
    """Return one run, default or custom, in any status.

    A ``running`` or ``failed`` run is returned as it stands; the client reads ``status``.
    The ``/actions?run=<id>`` page addresses a custom run through this route. An id outside
    1 to 2^63-1, which no ``analysis_run`` row can carry, is a 422.

    Args:
        run_id: The ``analysis_run`` id.
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session; only read.
        settings: The application's settings, for ``DISPLAY_TIMEZONE``.

    Returns:
        The run.

    Raises:
        ApiError: 404 ``not_found`` when no run has this id.
    """
    run = await runner.get_run(session, run_id)
    if run is None:
        raise not_found(f"Run {run_id}")
    return _to_view(run, settings)


@router.get(
    "/runs",
    response_model=list[schemas.Run],
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def list_runs_route(
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
    kind: Annotated[
        schemas.RunKind,
        Query(description="Which runs to list: `custom` (the default) or `default`."),
    ] = "custom",
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description="How many runs to return, newest first."),
    ] = DEFAULT_LIMIT,
) -> list[schemas.Run]:
    """List the newest runs of one kind, in every status, newest first.

    The web client lists earlier custom windows with ``GET /runs?kind=custom``. An unknown
    ``kind`` or a ``limit`` outside 1 to :data:`MAX_LIMIT` is a 422.

    Args:
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session; only read.
        settings: The application's settings, for ``DISPLAY_TIMEZONE``.
        kind: ``custom`` (the default) or ``default``.
        limit: How many runs to return, :data:`DEFAULT_LIMIT` when omitted.

    Returns:
        Up to ``limit`` runs, newest first; an empty list when there are none.
    """
    runs = await runner.list_runs(session, kind=kind, limit=limit)
    return [_to_view(run, settings) for run in runs]


@router.post(
    "/runs",
    response_model=schemas.Run,
    status_code=201,
    responses=schemas.ERROR_RESPONSES,
)
async def create_run(
    body: schemas.CreateRunRequest,
    user: UserDep,
    session: SessionDep,
    thresholds: ThresholdsDep,
    settings: SettingsDep,
    now: NowDep,
) -> schemas.Run:
    """Compute a custom-window run synchronously from stored data and return it (201).

    The window is a range of calendar dates applied to each source's native days. It must
    end no later than the default window end at the request's instant, start no earlier
    than ``earliest_start`` of the configuration (the first date whose lookbacks the run can
    compute; the message names it) and span 1 day to ``window.max_days``;
    ``AnalysisWindow.custom`` enforces all three, and a violation is a 422 carrying its code,
    answered before any run row is written. The run reads only stored data (the API never
    calls a platform) and is recorded as ``kind=custom`` with ``trigger=manual``, even when
    the window equals the default, so it never becomes ``/runs/latest``.

    The runner takes the shared ``analysis`` lock that the worker and the CLI also take,
    so only one run executes at a time whichever process starts it. While any process holds
    it, this route answers 409 ``run_in_progress`` without waiting. The run's load,
    computation and persistence execute in the API's analysis process
    (``isolated=True``), so this process keeps serving other requests, and answering
    contenders 409, while the run computes.

    Args:
        body: The window's first and last calendar dates, both inclusive.
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session; it reads the window inputs and is never
            committed.
        thresholds: The loaded configuration the window and the run use; the view reads
            the window settings the run stored from it.
        settings: The application's settings, for ``DISPLAY_TIMEZONE``: the run's frozen
            flag messages and the returned view render instants in it.
        now: The request's instant: the default window end is derived from it and it is the
            run's instant.

    Returns:
        The succeeded custom run.

    Raises:
        InvalidWindow: When the window breaks a bound (422, rendered by ``api/app.py``).
        RunInProgress: When any process holds the analysis lock (409 ``run_in_progress``,
            rendered by ``api/app.py``).
        BrokenProcessPool: When the analysis process died during the run; the run is
            recorded failed and the next ``POST /runs`` spawns a fresh one (500).
        AnalysisProcessError: When the analysis process could not report the run's outcome
            (500).
    """
    window = await runner.custom_window(
        body.window_start,
        body.window_end,
        now=now,
        thresholds=thresholds,
        session=session,
    )
    # The request session has only read (the signed-in user and the window inputs); ending
    # its transaction returns its pooled connection instead of holding it idle in a
    # transaction while the run, which commits through sessions of its own, computes.
    await session.rollback()
    run = await runner.run_analysis(
        window,
        "custom",
        "manual",
        reason="api",
        thresholds=thresholds,
        now=now,
        display_timezone=settings.display_timezone,
        isolated=True,
    )
    _log.info(
        "runs.custom_created",
        run_id=run.id,
        window_start=window.start.isoformat(),
        window_end=window.end.isoformat(),
        days=window.days,
        user_id=user.id,
    )
    return _to_view(run, settings)
