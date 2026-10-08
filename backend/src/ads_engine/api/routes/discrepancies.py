"""``/discrepancies``: list C0 discrepancies and apply their resolutions.

AAP 0.5.3 (``GET /discrepancies?state=open``, paged oldest first by ``limit`` and the
``after`` keyset cursor, with the next page named in a ``Link: rel="next"`` header;
``POST /discrepancies/{id}/resolve``), 0.5.4.4 ("C0 resolutions by kind and subject type")
and 0.5.5 (the ``/decisions`` screen's ``DiscrepancyResolver``, which lists open and
awaiting-platform discrepancies and renders exactly the resolutions valid for each one's kind
and subject type).

Paths are relative to ``/api/v1``; ``api/app.py`` includes :data:`router` with that prefix and
the CSRF dependency, so the ``POST`` also needs ``X-Requested-With: ads-engine``.

**No business rules live here.** Which resolutions a (kind, decision shape) pair allows, what a
complete intended state is for a shape, tombstones, immediate closure versus
``awaiting_platform``, the retired-dataset refusal and the rerun request all belong to
:mod:`ads_engine.decisions`. This module reads :data:`ads_engine.decisions.RESOLUTIONS` only
to show each discrepancy its valid resolutions, takes each subject's decision shape, name,
source and currency from :func:`ads_engine.decisions.load_subject_states` (the list, every
subject in one set-based call) and :func:`ads_engine.decisions.load_subject_state` (the
``POST``): the same state :func:`ads_engine.decisions.resolve_discrepancy` validates against,
so the resolutions offered are exactly the ones the ``POST`` accepts. Every write is
delegated. Domain rejections propagate unchanged as
:class:`ads_engine.decisions.DecisionError` subclasses, which ``api/app.py`` renders as
``{"error": {"code", "message"}}``:

* ``InvalidResolution``: 422 (a resolution not valid for the pair, or an intended state that
  is incomplete or does not fit the subject's shape);
* ``SubjectRetired``: 409 ``subject_retired`` (the discrepancy's dataset is not active);
* ``DiscrepancyClosed``: 409 ``discrepancy_resolved``;
* ``SubjectNotFound``: 404 ``not_found``;
* ``PlatformStateUnknown``: 409 ``platform_state_unknown``.

Logs carry ids, kinds, resolutions and states only, never the user's email or the note.

Example:
    Resolving an ``untracked`` daily shared budget by recording its intended amount::

        POST /api/v1/discrepancies/17/resolve
        X-Requested-With: ads-engine

        {"resolution": "record_intended", "intended_state": {"daily_budget": "80.00"}}

    answers ``200`` with the discrepancy in state ``awaiting_platform``; it closes when a
    later entity sync's snapshot shows that amount.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Final, TypeIs, get_args

from fastapi import APIRouter, Path, Query, Request, Response
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.api import schemas
from ads_engine.api.deps import ApiError, NowDep, SessionDep, UserDep
from ads_engine.db.models import DISCREPANCY_RESOLUTIONS, Discrepancy
from ads_engine.db.repositories import active_datasets
from ads_engine.decisions import (
    RESOLUTIONS,
    DiscrepancyKind,
    SubjectNotFound,
    SubjectRef,
    SubjectState,
    SubjectType,
    load_subject_state,
    load_subject_states,
    resolve_discrepancy,
)
from ads_engine.logging import get_logger

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "list_discrepancies", "resolve", "router"]

router = APIRouter(tags=["discrepancies"])
"""The discrepancy routes; ``api/app.py`` mounts them under ``/api/v1``."""

_log = get_logger(__name__)

DEFAULT_LIMIT: Final = 200
"""Rows a ``GET /discrepancies`` page reads when the request names no ``limit``."""

MAX_LIMIT: Final = 500
"""The largest ``limit`` ``GET /discrepancies`` accepts."""

_INVALID_CURSOR_MESSAGE: Final = (
    "The after cursor names no discrepancy; list again from the first page"
)
"""The 422 ``invalid_cursor`` message; it names no user data."""

_DEFAULT_STATES: Final[list[schemas.DiscrepancyState]] = ["open", "awaiting_platform"]
"""The states ``GET /discrepancies`` lists without ``?state=``: the unresolved ones the
resolver shows (AAP 0.5.5). FastAPI copies a parameter default per request, and nothing
here mutates it."""

_MAX_ID: Final[int] = schemas.BIGINT_MAX
"""The largest ``bigint`` identity; a larger path or cursor id is a 422, not a database error."""

_KINDS: Final[tuple[str, ...]] = get_args(DiscrepancyKind)
_SUBJECT_TYPES: Final[tuple[str, ...]] = get_args(SubjectType)


# ---------------------------------------------------------------------------------------
# Row narrowing and the response view
# ---------------------------------------------------------------------------------------


def _is_kind(value: str) -> TypeIs[DiscrepancyKind]:
    """Return whether a stored ``discrepancy.kind`` is one of the C0 kinds."""
    return value in _KINDS


def _is_subject_type(value: str) -> TypeIs[SubjectType]:
    """Return whether a stored ``discrepancy.subject_type`` is a decision subject type."""
    return value in _SUBJECT_TYPES


def _subject_ref(row: Discrepancy) -> SubjectRef:
    """Return the decision subject a discrepancy row is about.

    Args:
        row: The discrepancy row.

    Returns:
        The subject reference.

    Raises:
        ValueError: If the row holds a subject type outside ``SUBJECT_TYPES``, which the
            table's ``CHECK`` constraint rules out.
    """
    subject_type = row.subject_type
    if not _is_subject_type(subject_type):
        raise ValueError(f"discrepancy {row.id} holds an unknown subject type {subject_type!r}")
    return SubjectRef(type=subject_type, id=row.subject_id)


def _view(row: Discrepancy, state: SubjectState) -> schemas.Discrepancy:
    """Build the response view of a discrepancy from its subject's current state.

    The subject's name, source and currency and its decision shape come from ``state``; the
    resolutions are the ones :data:`ads_engine.decisions.RESOLUTIONS` defines for the row's
    kind and that shape, in the fixed order of ``DISCREPANCY_RESOLUTIONS``.

    Args:
        row: The discrepancy row.
        state: Its subject's state, as :func:`ads_engine.decisions.load_subject_state`
            builds it.

    Returns:
        The discrepancy view.

    Raises:
        ValueError: If the row holds a kind outside ``DISCREPANCY_KINDS`` (ruled out by the
            table's ``CHECK`` constraint).
        KeyError: If ``RESOLUTIONS`` defines no entry for the (kind, shape) pair. Detection
            never produces such a pair (a shared budget has no status), so the error
            surfaces as a domain defect instead of an empty resolver row.
    """
    kind = row.kind
    if not _is_kind(kind):
        raise ValueError(f"discrepancy {row.id} holds an unknown kind {kind!r}")
    valid = RESOLUTIONS[(kind, state.shape)]
    return schemas.Discrepancy.from_row(
        row,
        subject=schemas.SubjectView(
            type=state.ref.type,
            id=state.ref.id,
            name=state.name,
            source=state.source,
            currency=state.currency,
        ),
        shape=state.shape,
        resolutions=[key for key in DISCREPANCY_RESOLUTIONS if key in valid],
    )


def _report_unavailable(row: Discrepancy, ref: SubjectRef, error: SubjectNotFound) -> None:
    """Log that a listed discrepancy is left out because its subject cannot be loaded.

    Args:
        row: The first listed discrepancy that names the subject.
        ref: The subject.
        error: The domain's reason, from :func:`ads_engine.decisions.load_subject_states`.
    """
    # Priority decision: a subject the domain cannot load (its row gone, a budget that is no
    # longer explicitly shared, or a shared budget whose currency is undeterminable) has no
    # decision shape, so no resolution is offerable and resolve_discrepancy would answer 404
    # for it. Its discrepancies are left out of the list with a warning instead of failing
    # the whole resolver list with a 404 for one subject.
    _log.warning(
        "discrepancies.subject_unavailable",
        discrepancy_id=row.id,
        subject_type=ref.type,
        subject_id=ref.id,
        code=error.code,
    )


# ---------------------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------------------


async def _cursor_position(session: AsyncSession, after: int) -> tuple[datetime, int]:
    """Return the ``(detected_at, id)`` keyset position the ``after`` cursor names.

    The cursor row is read by id whatever its state or dataset. ``detected_at`` is set once,
    when the discrepancy is raised, and never changes afterwards, so the position still holds
    when the row was resolved, or its dataset replaced, after the previous page was served.

    Args:
        session: The request's session.
        after: The id of the last discrepancy of the previous page.

    Returns:
        The cursor row's ``detected_at`` and id.

    Raises:
        ApiError: 422 ``invalid_cursor`` when no discrepancy has that id, for example after
            its dataset was purged.
    """
    detected_at = await session.scalar(
        select(Discrepancy.detected_at).where(Discrepancy.id == after)
    )
    if detected_at is None:
        raise ApiError(422, "invalid_cursor", _INVALID_CURSOR_MESSAGE)
    return detected_at, after


def _next_link(request: Request, after: int) -> str:
    """Return the RFC 8288 ``Link`` header value that names the next page.

    The target is a relative reference: the request's path and query with ``after`` set to
    the given id and every other parameter, each repeated ``state`` included, kept as sent.

    Args:
        request: The current request.
        after: The id of the last discrepancy row the current page read.

    Returns:
        ``<{path}?{query}>; rel="next"``.
    """
    url = request.url.include_query_params(after=after)
    return f'<{url.path}?{url.query}>; rel="next"'


# ---------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------


@router.get(
    "/discrepancies",
    response_model=list[schemas.Discrepancy],
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def list_discrepancies(
    request: Request,
    response: Response,
    user: UserDep,
    session: SessionDep,
    state: Annotated[
        list[schemas.DiscrepancyState],
        Query(
            description=(
                "Discrepancy states to list; repeat the parameter for several "
                "(``?state=open&state=awaiting_platform``). Defaults to the unresolved "
                "states, ``open`` and ``awaiting_platform``."
            ),
        ),
    ] = _DEFAULT_STATES,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_LIMIT,
            description=(
                f"The most discrepancies the page reads, 1 to {MAX_LIMIT} "
                f"(default {DEFAULT_LIMIT})."
            ),
        ),
    ] = DEFAULT_LIMIT,
    after: Annotated[
        int | None,
        Query(
            gt=0,
            le=_MAX_ID,
            description=(
                "Continue the list after this discrepancy: the id of the last discrepancy "
                'of the previous page, as the ``Link: rel="next"`` header names it. Omit '
                "it for the first page."
            ),
        ),
    ] = None,
) -> list[schemas.Discrepancy]:
    """List the discrepancies of the active datasets, oldest first, one page at a time.

    Only discrepancies of each source's active primary dataset are listed, so a discrepancy
    about replaced data never reaches the resolver. They are ordered by ``detected_at``,
    then id, the order C0 items are pinned in (AAP 0.5.4.4). Each carries its subject (name,
    source, currency), the subject's current decision shape, and the resolutions valid for
    its (kind, shape) pair in their fixed display order, so the resolver renders exactly
    that row (AAP 0.5.5).

    **Paging.** A page reads at most ``limit`` discrepancy rows (1 to 500, default 200).
    ``after`` continues the list after the discrepancy with that id: the page holds the rows
    whose ``(detected_at, id)`` sorts after the cursor row's. The cursor row is found
    whatever its current state or dataset, because ``detected_at`` never changes, so a
    cursor stays valid after its discrepancy is resolved. When the page read ``limit``
    rows, more may follow, and the response carries an RFC 8288 ``Link`` header naming the
    next page as a relative reference, for example
    ``</api/v1/discrepancies?state=resolved&limit=50&after=812>; rel="next"``: the same
    path and query, each repeated ``state`` included, with ``after`` set to the id of the
    last row read. Without that header the list is complete, so following it until it no
    longer comes back returns every row exactly once.

    A discrepancy whose subject the domain reports as not found is left out and logged as
    ``discrepancies.subject_unavailable``: no resolution could be applied to it. A page can
    therefore hold fewer than ``limit`` discrepancies while its ``Link`` header still
    announces more.

    Args:
        request: The current request; its path and query build the ``Link`` header.
        response: The response that carries the ``Link`` header.
        user: The signed-in user; the route requires a session and reads nothing else from it.
        session: The request's session.
        state: The states to list; ``open`` and ``awaiting_platform`` by default.
        limit: At most this many rows are read for the page; a ``limit`` outside 1-500 is
            a 422 validation error.
        after: The id of the last discrepancy of the previous page, from the ``Link``
            header; ``None`` for the first page. An id outside 1 to ``2**63 - 1`` is a 422
            validation error.

    Returns:
        The page's discrepancies, oldest first.

    Raises:
        ApiError: 422 ``invalid_cursor`` when ``after`` names no discrepancy.
    """
    cursor = None if after is None else await _cursor_position(session, after)
    dataset_ids = sorted(dataset.id for dataset in (await active_datasets(session)).values())
    if not dataset_ids:
        return []
    statement = select(Discrepancy).where(
        Discrepancy.dataset_id.in_(dataset_ids),
        Discrepancy.state.in_(sorted(set(state))),
    )
    if cursor is not None:
        # The row comparison is the keyset bound; PostgreSQL also derives its leading
        # ``detected_at >=`` bound, so the (state, detected_at) index starts at the cursor.
        statement = statement.where(
            tuple_(Discrepancy.detected_at, Discrepancy.id) > tuple_(*cursor),
        )
    rows: Sequence[Discrepancy] = (
        await session.scalars(
            statement.order_by(Discrepancy.detected_at, Discrepancy.id).limit(limit)
        )
    ).all()
    # The decision shape is a property of the subject, not of the discrepancy: every distinct
    # subject of the page is loaded once, all of them in one set-based domain call whose
    # statement count does not grow with the number of subjects.
    refs = [_subject_ref(row) for row in rows]
    loaded = await load_subject_states(session, refs)
    unavailable: set[SubjectRef] = set()
    views: list[schemas.Discrepancy] = []
    for row, ref in zip(rows, refs, strict=True):
        subject_state = loaded.states.get(ref)
        if subject_state is not None:
            views.append(_view(row, subject_state))
        elif ref not in unavailable:
            unavailable.add(ref)
            _report_unavailable(row, ref, loaded.not_found[ref])
    if len(rows) == limit:
        # A full read of rows may have more behind it; a page of fewer views does not end
        # the list, because rows whose subject is unavailable are left out of the views.
        response.headers["Link"] = _next_link(request, rows[-1].id)
    return views


@router.post(
    "/discrepancies/{discrepancy_id}/resolve",
    response_model=schemas.Discrepancy,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def resolve(
    discrepancy_id: Annotated[int, Path(gt=0, le=_MAX_ID, description="The discrepancy id.")],
    body: schemas.ResolveRequest,
    user: UserDep,
    session: SessionDep,
    now: NowDep,
) -> schemas.Discrepancy:
    """Apply one C0 resolution to an open or awaiting-platform discrepancy.

    :func:`ads_engine.decisions.resolve_discrepancy` applies it and queues the analysis
    rerun (AAP 0.3.2); this route commits and answers with the updated discrepancy:

    * ``platform_correct``, ``adopt_platform`` and ``acknowledge_removal`` close it at once
      (``state`` ``resolved``); ``acknowledge_removal`` writes a tombstone decision and
      never waits for a snapshot.
    * ``decision_correct`` and ``record_intended`` move it to ``awaiting_platform``; it
      closes when a later snapshot matches the decision. ``record_intended`` takes
      ``intended_state`` in the subject's decision shape: ``own_budget``
      ``{status, daily_budget?, total_budget?}``, ``shared_member`` ``{status}``,
      ``budget`` ``{daily_budget}`` or ``{total_budget}`` per its period.

    Only the fields the client sent in ``intended_state`` are passed on, so the domain judges
    completeness against the shape.

    Args:
        discrepancy_id: The discrepancy to resolve.
        body: The resolution, the intended state for ``record_intended``, and a note.
        user: The signed-in user, recorded as ``decided_by`` on any decision written.
        session: The request's session.
        now: The resolution instant.

    Returns:
        The discrepancy as committed, with its subject, shape and valid resolutions.

    Raises:
        ads_engine.decisions.InvalidResolution: 422, for a resolution not valid for the
            (kind, shape) pair, or an intended state that is missing, incomplete or does not
            fit the shape.
        ads_engine.decisions.SubjectRetired: 409 ``subject_retired``, when the
            discrepancy's dataset is not active.
        ads_engine.decisions.DiscrepancyClosed: 409, when it is already resolved.
        ads_engine.decisions.SubjectNotFound: 404, when it or its subject does not exist.
        ads_engine.decisions.PlatformStateUnknown: 409, when the platform state to adopt
            is not known yet.
    """
    intended = None if body.intended_state is None else body.intended_state.present_fields()
    row = await resolve_discrepancy(
        discrepancy_id,
        body.resolution,
        intended,
        session=session,
        decided_by=user.email,
        note=body.note,
        now=now,
    )
    # The view is built inside the resolution's transaction, from the subject state that
    # includes any decision it wrote, so a failure here leaves nothing committed.
    view = _view(row, await load_subject_state(session, _subject_ref(row)))
    await session.commit()
    _log.info(
        "discrepancies.resolved",
        discrepancy_id=row.id,
        kind=row.kind,
        resolution=body.resolution,
        state=row.state,
        user_id=user.id,
    )
    return view
