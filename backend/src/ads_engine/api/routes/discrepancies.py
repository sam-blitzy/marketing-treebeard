"""``/discrepancies``: list C0 discrepancies and apply their resolutions.

AAP 0.5.3 (``GET /discrepancies?state=open``, ``POST /discrepancies/{id}/resolve``), 0.5.4.4
("C0 resolutions by kind and subject type") and 0.5.5 (the ``/decisions`` screen's
``DiscrepancyResolver``, which lists open and awaiting-platform discrepancies and renders
exactly the resolutions valid for each one's kind and subject type).

Paths are relative to ``/api/v1``; ``api/app.py`` includes :data:`router` with that prefix and
the CSRF dependency, so the ``POST`` also needs ``X-Requested-With: ads-engine``.

**No business rules live here.** Which resolutions a (kind, decision shape) pair allows, what a
complete intended state is for a shape, tombstones, immediate closure versus
``awaiting_platform``, the retired-dataset refusal and the rerun request all belong to
:mod:`ads_engine.decisions`. This module reads :data:`ads_engine.decisions.RESOLUTIONS` only
to show each discrepancy its valid resolutions, takes each subject's decision shape, name,
source and currency from :func:`ads_engine.decisions.load_subject_state` (the same state
:func:`ads_engine.decisions.resolve_discrepancy` validates against, so the resolutions
offered are exactly the ones the ``POST`` accepts), and delegates every write. Domain
rejections propagate unchanged as :class:`ads_engine.decisions.DecisionError` subclasses,
which ``api/app.py`` renders as ``{"error": {"code", "message"}}``:

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
from typing import Annotated, Final, TypeIs, get_args

from fastapi import APIRouter, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.api import schemas
from ads_engine.api.deps import NowDep, SessionDep, UserDep
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
    resolve_discrepancy,
)
from ads_engine.logging import get_logger

__all__ = ["list_discrepancies", "resolve", "router"]

router = APIRouter(tags=["discrepancies"])
"""The discrepancy routes; ``api/app.py`` mounts them under ``/api/v1``."""

_log = get_logger(__name__)

_DEFAULT_STATES: Final[list[schemas.DiscrepancyState]] = ["open", "awaiting_platform"]
"""The states ``GET /discrepancies`` lists without ``?state=``: the unresolved ones the
resolver shows (AAP 0.5.5). FastAPI copies a parameter default per request, and nothing
here mutates it."""

_MAX_ID: Final[int] = 2**63 - 1
"""The largest ``bigint`` identity; a larger path id is a 422 rather than a database error."""

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
        state: Its subject's state from :func:`ads_engine.decisions.load_subject_state`.

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


async def _listed_subject_state(
    session: AsyncSession, row: Discrepancy, ref: SubjectRef
) -> SubjectState | None:
    """Load a listed discrepancy's subject state, or ``None`` when it cannot be loaded.

    Args:
        session: The request's session.
        row: The discrepancy that names the subject (for the log line).
        ref: The subject.

    Returns:
        The subject's state, or ``None`` when the domain reports the subject not found.
    """
    try:
        return await load_subject_state(session, ref)
    except SubjectNotFound as exc:
        # Priority decision: a subject the domain cannot load (its row gone, a budget that is
        # no longer explicitly shared, or a shared budget whose currency is undeterminable)
        # has no decision shape, so no resolution is offerable and resolve_discrepancy would
        # answer 404 for it. Its discrepancy is left out of the list with a warning instead
        # of failing the whole resolver list with a 404 for one row.
        _log.warning(
            "discrepancies.subject_unavailable",
            discrepancy_id=row.id,
            subject_type=ref.type,
            subject_id=ref.id,
            code=exc.code,
        )
        return None


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
) -> list[schemas.Discrepancy]:
    """List the discrepancies of the active datasets, oldest first.

    Only discrepancies of each source's active primary dataset are listed, so a discrepancy
    about replaced data never reaches the resolver. They are ordered by ``detected_at``,
    then id, the order C0 items are pinned in (AAP 0.5.4.4). Each carries its subject (name,
    source, currency), the subject's current decision shape, and the resolutions valid for
    its (kind, shape) pair in their fixed display order, so the resolver renders exactly
    that row (AAP 0.5.5).

    A discrepancy whose subject the domain reports as not found is left out and logged as
    ``discrepancies.subject_unavailable``: no resolution could be applied to it.

    Args:
        user: The signed-in user; the route requires a session and reads nothing else from it.
        session: The request's session.
        state: The states to list; ``open`` and ``awaiting_platform`` by default.

    Returns:
        The matching discrepancies, oldest first.
    """
    dataset_ids = sorted(dataset.id for dataset in (await active_datasets(session)).values())
    if not dataset_ids:
        return []
    rows: Sequence[Discrepancy] = (
        await session.scalars(
            select(Discrepancy)
            .where(
                Discrepancy.dataset_id.in_(dataset_ids),
                Discrepancy.state.in_(sorted(set(state))),
            )
            .order_by(Discrepancy.detected_at, Discrepancy.id)
        )
    ).all()
    # The decision shape is a property of the subject, not of the discrepancy: it is loaded
    # once per distinct subject, which the unresolved discrepancies bound to a few.
    subjects: dict[SubjectRef, SubjectState | None] = {}
    views: list[schemas.Discrepancy] = []
    for row in rows:
        ref = _subject_ref(row)
        if ref not in subjects:
            subjects[ref] = await _listed_subject_state(session, row, ref)
        subject_state = subjects[ref]
        if subject_state is not None:
            views.append(_view(row, subject_state))
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
        session,
        discrepancy_id,
        body.resolution,
        intended,
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
