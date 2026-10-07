"""``GET /sources`` and ``POST /sources/{source}/sync``: source health and "Sync now".

AAP 0.5.3 (``GET /sources``, ``POST /sources/{source}/sync``; "Sync requests: one pending per
source") and 0.5.5 (the ``/sources`` screen). Paths are relative to ``/api/v1``;
``ads_engine.api.app.create_app`` includes :data:`router` with that prefix and with the CSRF
dependency, so the ``POST`` needs ``X-Requested-With: ads-engine`` without this module
repeating the check.

* ``GET /sources`` reports, per source in display order (google, linkedin, hubspot): mode,
  health status and reason from ``source_connection``; the active primary dataset with the
  freshness, coverage and completeness of each of its named datasets; for HubSpot, the
  active companion CRM dataset of a simulated LinkedIn dataset with its own freshness rows
  (AAP 0.3.3); the Google webhook calls counted in the trailing 24 h against the product's
  own cap (``GOOGLE_DAILY_CALL_BUDGET``); whether a "Sync now" request is pending; and the
  newest :data:`RECENT_RUNS_PER_SOURCE` ``sync_run`` rows. It is read-only.
* ``POST /sources/{source}/sync`` queues a ``sync_request`` row. The worker picks it up on
  its next 30 s wake (AAP 0.3.2); the API itself never calls LinkedIn, n8n or HubSpot. The
  partial unique index ``uq_sync_request_pending`` allows one pending request per source,
  and a second request while one is pending answers 409 ``sync_pending``. No
  ``analysis_request`` is enqueued here: the sync job enqueues one when it completes.

Decisions taken in this module, each stated where its code lives:

* **Staleness lives in one private helper** (:func:`_is_stale`).
  ``ads_engine.analysis.overlay`` exports no staleness helper, so this screen applies the
  age half of the AAP 0.3.2 rule itself: a dataset is stale when it never succeeded or last
  succeeded more than ``sources.stale_after_hours`` ago, exactly as
  ``analysis.context.Freshness.is_stale`` judges age. The coverage-versus-window-end half
  belongs to the items of a run (the overlay): this screen has no run window, so it reports
  coverage as stored beside the age verdict.
* **A source with no ``source_connection`` row yet** (before the worker's first attempt)
  reports ``mode`` and ``status`` as ``None``, never an invented ``live``.
* **Fixed query count**: the handler issues at most six statements whatever the data: the
  connections, the active primaries, the active companion, the freshness rows of those
  datasets, the pending requests and the recent runs (one ``LATERAL`` statement served by
  ``ix_sync_run_source_started``), so the screen stays fast as ``sync_run`` grows.

Nothing here exposes a secret or whether a credential is present beyond the stored
``reason`` (for example "credentials not configured"); settings are read only for the call
cap. The module imports nothing from ``connectors``, ``sync`` or ``simulation``.

Example::

    GET /api/v1/sources
    200 [{"source": "google", "mode": "simulated", "status": "ok", ...,
          "calls_24h": 12, "call_cap": 1000, "sync_pending": false, "recent_runs": [...]},
         {"source": "linkedin", ...}, {"source": "hubspot", ...}]

    POST /api/v1/sources/google/sync      (X-Requested-With: ads-engine)
    202 {"id": 7, "source": "google", "requested_at": "...", "picked_at": null}
    409 {"error": {"code": "sync_pending", "message": "A sync request for google is ..."}}
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from fastapi import APIRouter
from sqlalchemy import Text, column, select, true, values
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.api import schemas
from ads_engine.api.deps import (
    NowDep,
    SessionDep,
    SettingsDep,
    ThresholdsDep,
    UserDep,
    conflict,
)
from ads_engine.db.models.ops import (
    DATASETS,
    SOURCES,
    DatasetFreshness,
    SourceConnection,
    SourceDataset,
    SyncRequest,
    SyncRun,
)
from ads_engine.db.repositories import active_datasets
from ads_engine.logging import get_logger

__all__ = ["RECENT_RUNS_PER_SOURCE", "list_sources", "request_sync", "router"]

router = APIRouter(tags=["sources"])
"""The ``sources`` routes, included by ``create_app`` under ``/api/v1``."""

_log = get_logger(__name__)

RECENT_RUNS_PER_SOURCE: Final[int] = 10
"""How many of the newest ``sync_run`` rows ``GET /sources`` lists per source."""

_PENDING_INDEX: Final = "uq_sync_request_pending"
"""The partial unique index that allows one pending ``sync_request`` per source."""

_STALE_AFTER_KEY: Final = "sources.stale_after_hours"
"""The threshold key of the dataset age beyond which a dataset is stale (AAP 0.3.2)."""

_GOOGLE: Final = "google"
_LINKEDIN: Final = "linkedin"
_HUBSPOT: Final = "hubspot"

_DATASET_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {name: index for index, name in enumerate(DATASETS)}
)
"""Position of each dataset name, so freshness rows render in the ``DATASETS`` order."""


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def _is_stale(last_success_at: datetime | None, *, now: datetime, stale_after: timedelta) -> bool:
    """Return whether a dataset is stale by age (AAP 0.3.2).

    ``analysis.overlay`` exports no staleness helper, so the age rule lives here, once, with
    the same comparison ``analysis.context.Freshness.is_stale`` makes: a dataset that never
    succeeded is stale, and so is one whose last success is more than ``stale_after`` before
    ``now``. The coverage-versus-window-end part of staleness applies to the items of a run
    (the overlay) and not to this screen, which has no run window.

    Args:
        last_success_at: When the dataset last refreshed successfully, if ever.
        now: The request's instant.
        stale_after: ``sources.stale_after_hours`` as a duration.

    Returns:
        ``True`` when the dataset is stale now.
    """
    return last_success_at is None or now - last_success_at > stale_after


def _freshness_views(
    rows: Iterable[DatasetFreshness], *, now: datetime, stale_after: timedelta
) -> list[schemas.DatasetFreshnessView]:
    """Render one dataset's freshness rows in ``DATASETS`` order, each judged for staleness.

    Args:
        rows: The ``dataset_freshness`` rows of one source dataset.
        now: The request's instant.
        stale_after: ``sources.stale_after_hours`` as a duration.

    Returns:
        One view per row; ``incomplete`` is derived by the view from ``completeness``.
    """
    ordered = sorted(
        rows, key=lambda row: (_DATASET_ORDER.get(row.dataset, len(_DATASET_ORDER)), row.dataset)
    )
    return [
        schemas.DatasetFreshnessView.from_row(
            row, stale=_is_stale(row.last_success_at, now=now, stale_after=stale_after)
        )
        for row in ordered
    ]


# ---------------------------------------------------------------------------
# Reads (one statement each)
# ---------------------------------------------------------------------------


async def _connections(session: AsyncSession) -> dict[str, SourceConnection]:
    """Return the ``source_connection`` row of every source that has one, keyed by source."""
    result = await session.scalars(select(SourceConnection))
    return {row.source: row for row in result}


async def _active_companion(
    session: AsyncSession, linkedin: SourceDataset | None
) -> SourceDataset | None:
    """Return the active companion CRM dataset of the active LinkedIn dataset, if any.

    A companion is built only for a simulated LinkedIn dataset and shares its parent's state
    (AAP 0.3.3), so it is the one active companion whose parent is LinkedIn's active primary.
    Without an active LinkedIn dataset there is no companion to report and nothing is read.

    Args:
        session: The request's session.
        linkedin: LinkedIn's active primary dataset, if any.

    Returns:
        The companion dataset, or ``None``.
    """
    if linkedin is None:
        return None
    result = await session.scalars(
        select(SourceDataset)
        .where(
            SourceDataset.role == "companion",
            SourceDataset.state == "active",
            SourceDataset.parent_dataset_id == linkedin.id,
        )
        .order_by(SourceDataset.id.desc())
        .limit(1)
    )
    return result.first()


async def _freshness_by_dataset(
    session: AsyncSession, dataset_ids: Sequence[int]
) -> dict[int, list[DatasetFreshness]]:
    """Return the ``dataset_freshness`` rows of the given source datasets, keyed by dataset id.

    Args:
        session: The request's session.
        dataset_ids: The active primary and companion dataset ids.

    Returns:
        The rows of each dataset id that has any; no statement runs for an empty list.
    """
    if not dataset_ids:
        return {}
    result = await session.scalars(
        select(DatasetFreshness).where(DatasetFreshness.dataset_id.in_(dataset_ids))
    )
    grouped: dict[int, list[DatasetFreshness]] = {}
    for row in result:
        grouped.setdefault(row.dataset_id, []).append(row)
    return grouped


async def _pending_sources(session: AsyncSession) -> frozenset[str]:
    """Return the sources with a pending (not yet picked) ``sync_request``."""
    result = await session.scalars(
        select(SyncRequest.source).where(SyncRequest.picked_at.is_(None)).distinct()
    )
    return frozenset(result)


async def _recent_runs(session: AsyncSession) -> dict[str, list[SyncRun]]:
    """Return the newest :data:`RECENT_RUNS_PER_SOURCE` ``sync_run`` rows of every source.

    One statement: a ``VALUES`` list of the sources joined ``LATERAL`` to a per-source
    ``ORDER BY started_at DESC LIMIT n`` subquery, which ``ix_sync_run_source_started``
    serves with a bounded index scan per source however long the history grows. Ties on
    ``started_at`` break by id, newest first, so the list is deterministic.

    Args:
        session: The request's session.

    Returns:
        Each source's runs, newest first; a source without runs is absent.
    """
    sources = values(column("source", Text), name="listed_source").data(
        [(source,) for source in SOURCES]
    )
    newest = (
        select(SyncRun.id)
        .where(SyncRun.source == sources.c.source)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(RECENT_RUNS_PER_SOURCE)
        .lateral("newest_run")
    )
    newest_ids = select(newest.c.id).select_from(sources).join(newest, true())
    result = await session.scalars(
        select(SyncRun)
        .where(SyncRun.id.in_(newest_ids))
        .order_by(SyncRun.source, SyncRun.started_at.desc(), SyncRun.id.desc())
    )
    grouped: dict[str, list[SyncRun]] = {}
    for row in result:
        grouped.setdefault(row.source, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _source_view(
    source: str,
    *,
    connection: SourceConnection | None,
    active: SourceDataset | None,
    companion: SourceDataset | None,
    freshness: Mapping[int, Sequence[DatasetFreshness]],
    pending: frozenset[str],
    runs: Sequence[SyncRun],
    call_cap: int,
    now: datetime,
    stale_after: timedelta,
) -> schemas.Source:
    """Assemble the ``Source`` entry of one source.

    Args:
        source: The source.
        connection: Its ``source_connection`` row; ``None`` before the worker's first
            attempt, in which case mode, status, reason and last attempt are ``None``.
        active: Its active primary dataset, if any.
        companion: HubSpot only: the active companion CRM dataset, if any.
        freshness: Freshness rows keyed by dataset id.
        pending: The sources with a pending sync request.
        runs: Its newest sync runs, newest first.
        call_cap: The product's Google webhook call cap (``GOOGLE_DAILY_CALL_BUDGET``).
        now: The request's instant.
        stale_after: ``sources.stale_after_hours`` as a duration.

    Returns:
        The validated view.

    Raises:
        pydantic.ValidationError: When a stored row holds a value outside the contract.
    """
    calls_24h: int | None = None
    cap: int | None = None
    if source == _GOOGLE:
        # The call budget seeds Google's connection row before it books any call, so a
        # missing row means no call has been counted: zero is the observed count here.
        calls_24h = connection.calls_24h if connection is not None else 0
        cap = call_cap
    shown_companion = companion if source == _HUBSPOT else None
    return schemas.Source.model_validate(
        {
            "source": source,
            "mode": None if connection is None else connection.mode,
            "status": None if connection is None else connection.status,
            "reason": None if connection is None else connection.reason,
            "active_dataset": None if active is None else schemas.DatasetView.from_row(active),
            "companion_dataset": (
                None if shown_companion is None else schemas.DatasetView.from_row(shown_companion)
            ),
            "datasets": (
                []
                if active is None
                else _freshness_views(
                    freshness.get(active.id, ()), now=now, stale_after=stale_after
                )
            ),
            "companion_datasets": (
                []
                if shown_companion is None
                else _freshness_views(
                    freshness.get(shown_companion.id, ()), now=now, stale_after=stale_after
                )
            ),
            "last_attempt_at": None if connection is None else connection.last_attempt_at,
            "calls_24h": calls_24h,
            "call_cap": cap,
            "sync_pending": source in pending,
            "recent_runs": [schemas.SyncRunView.from_row(run) for run in runs],
        }
    )


def _is_pending_conflict(exc: IntegrityError) -> bool:
    """Return whether an ``IntegrityError`` is the one-pending-request-per-source violation.

    psycopg reports the violated index in ``diag.constraint_name``; when a driver gives no
    diagnostics, the index name in the error message decides instead.

    Args:
        exc: The error raised by the insert.

    Returns:
        ``True`` only for a violation of ``uq_sync_request_pending``.
    """
    diag = getattr(exc.orig, "diag", None)
    constraint = getattr(diag, "constraint_name", None)
    if isinstance(constraint, str):
        return constraint == _PENDING_INDEX
    return f'"{_PENDING_INDEX}"' in str(exc.orig)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/sources",
    response_model=list[schemas.Source],
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def list_sources(
    user: UserDep,
    session: SessionDep,
    settings: SettingsDep,
    thresholds: ThresholdsDep,
    now: NowDep,
) -> list[schemas.Source]:
    """List every source's mode, health, datasets, freshness, call budget and recent syncs.

    One entry per source, in the order google, linkedin, hubspot. Each carries the mode,
    status and reason the worker recorded (``null`` before its first attempt), the active
    primary dataset with the freshness, coverage and completeness of each named dataset
    (``stale`` when it last succeeded more than ``sources.stale_after_hours`` ago or never;
    ``incomplete`` when not ``complete``), whether a "Sync now" request is pending, and the
    newest sync runs. Google adds the webhook calls counted in the trailing 24 h against the
    product's own cap; HubSpot adds the active companion CRM dataset of a simulated LinkedIn
    dataset, with its own freshness rows. Read-only: nothing is written or synced.

    Args:
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session.
        settings: The application's settings, read only for the Google call cap.
        thresholds: The loaded configuration (``sources.stale_after_hours``).
        now: The request's instant, against which staleness is judged.

    Returns:
        The three ``Source`` entries.
    """
    stale_after = timedelta(hours=thresholds.int(_STALE_AFTER_KEY))
    connections = await _connections(session)
    primaries = {dataset.source: dataset for dataset in (await active_datasets(session)).values()}
    companion = await _active_companion(session, primaries.get(_LINKEDIN))
    dataset_ids = [dataset.id for dataset in primaries.values()]
    if companion is not None:
        dataset_ids.append(companion.id)
    freshness = await _freshness_by_dataset(session, dataset_ids)
    pending = await _pending_sources(session)
    runs = await _recent_runs(session)
    return [
        _source_view(
            source,
            connection=connections.get(source),
            active=primaries.get(source),
            companion=companion,
            freshness=freshness,
            pending=pending,
            runs=runs.get(source, ()),
            call_cap=settings.google_daily_call_budget,
            now=now,
            stale_after=stale_after,
        )
        for source in SOURCES
    ]


@router.post(
    "/sources/{source}/sync",
    response_model=schemas.SyncRequestView,
    status_code=202,
    responses=schemas.ERROR_RESPONSES,
)
async def request_sync(
    source: schemas.SourceName,
    user: UserDep,
    session: SessionDep,
) -> schemas.SyncRequestView:
    """Queue a sync of one source for the worker's next wake ("Sync now").

    The request is a ``sync_request`` row; the worker picks it up on its next 30 s wake and
    runs the source's jobs. The API performs no sync and calls no platform itself. At most
    one request per source may be pending: while one is, another answers 409
    ``sync_pending``. An unknown source is a 422.

    Args:
        source: ``google``, ``linkedin`` or ``hubspot``.
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session.

    Returns:
        The queued request, with ``picked_at`` ``null``.

    Raises:
        ApiError: 409 ``sync_pending`` when a request for the source is already pending.
        IntegrityError: For any integrity failure other than the pending-request index.
    """
    row = SyncRequest(source=source)
    session.add(row)
    try:
        await session.flush()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        # Only the partial unique index on pending requests is the documented conflict;
        # any other integrity failure is a defect and propagates unchanged.
        if not _is_pending_conflict(exc):
            raise
        _log.info("sources.sync_already_pending", source=source, user_id=user.id)
        raise conflict("sync_pending", f"A sync request for {source} is already pending") from exc
    _log.info("sources.sync_requested", source=source, request_id=row.id, user_id=user.id)
    return schemas.SyncRequestView.from_row(row)
