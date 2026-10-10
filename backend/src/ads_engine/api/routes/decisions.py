"""Decision log routes: ``GET /decisions`` and ``POST /decisions`` (AAP 0.5.3).

The decision log is the append-only record of what was last decided for each decision
subject, a campaign of either source or a Google explicitly shared budget (AAP 0.5.2). The
``/decisions`` screen's ``DecisionLog`` lists it per subject with origin, time and the patch
applied, and ``DecisionForm`` records a new decision as a patch (AAP 0.5.5).

What this module does, and what it leaves to the domain:

* **Reading** is a single joined statement per request: each ``decision`` row with its
  subject's name, source and currency, and a shared member's assigned budget's Google id
  (:func:`_decision_query`), restricted to the active primary datasets, so decisions
  about replaced data never reappear in the log. The
  newest-first page is drawn from one bounded index probe per active dataset
  (:func:`_newest_decision_ids`), so its cost does not grow with the log's history.
  ``POST`` answers with the same statement over the committed row, re-read from the
  database, so both routes render an amount at its stored scale (``"120.000000"``).
* **Writing** delegates every rule to :func:`ads_engine.decisions.record_decision`: the
  decision shape, the per-shape field rules, the currency's minor unit, empty and no-op
  patches, retired subjects and the rerun request all live there (AAP 0.5.2, 0.3.2). The
  route adds only what is about the *request*, and commits the transaction. A decision
  recorded from an item must be the one the item offers: an item without a
  ``decision_subject`` is 422 ``decision_not_offered``, one naming another subject 422
  ``subject_mismatch`` (:func:`_require_offered_subject`). The item must also still rest
  on current data, and stay so until the commit: every dataset its provenance names, and
  the subject's, is locked ``FOR SHARE`` in ``id`` order and the check repeated under the
  locks (:func:`_hold_item_current`), so an activation of another source cannot retire a
  dataset the item read before the decision commits (409 ``subject_retired``).
* **Errors** raised by the domain (``PatchRejected`` 422, ``SubjectRetired`` 409
  ``subject_retired``, ``SubjectNotFound`` 404) and ``DatasetNotWritable`` propagate
  unchanged to the handlers ``api/app.py`` registers, which render the AAP error shape.

The CSRF header on ``POST`` is enforced by the ``require_csrf`` dependency ``api/app.py``
attaches to every router; both routes require a signed-in user through :data:`UserDep`.
Logs carry ids, shapes and origins only, never the user's email, the note or an amount.

Example:
    Recording a budget-only decision keeps the decided status::

        POST /api/v1/decisions
        X-Requested-With: ads-engine

        {"subject": {"type": "campaign", "id": 42}, "patch": {"daily_budget": "120.00"}}

        201 {"id": 7, "shape": "own_budget", "status": "active",
             "daily_budget": "120.000000", "patch": {"daily_budget": "120.00"}, ...}
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from sqlalchemy import BigInteger, Select, and_, case, column, distinct, func, select, true, values
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ads_engine.analysis.overlay import current_dataset_ids, is_current, item_provenance
from ads_engine.api import schemas
from ads_engine.api.deps import ApiError, NowDep, SessionDep, UserDep, conflict, not_found
from ads_engine.db.models import AdAccount, Budget, Campaign, Recommendation
from ads_engine.db.models import Decision as DecisionRow
from ads_engine.db.repositories import active_datasets, lock_dataset
from ads_engine.decisions import DecisionPatch, SubjectRef, record_decision
from ads_engine.logging import get_logger

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "create_decision", "list_decisions", "router"]

router = APIRouter(tags=["decisions"])

_log = get_logger(__name__)

DEFAULT_LIMIT: Final = 200
"""Rows ``GET /decisions`` returns when the request names no ``limit``."""

MAX_LIMIT: Final = 500
"""The largest ``limit`` ``GET /decisions`` accepts."""

# The overlay's wording for an item whose provenance datasets are no longer active (AAP
# 0.3.2), repeated here so the 409 reads exactly like the card's "historical" reason.
_HISTORICAL_MESSAGE: Final = "Computed from data that has since been replaced"

# The display name ads_engine.decisions gives a shared budget, which has no name of its own
# on the platform; the log uses the same wording as the domain's messages.
_SHARED_BUDGET_NAME: Final = "Shared budget {external_id}"

# The subject types an item's stored ``decision_subject`` may name (AAP 0.5.2 ``decision``).
_SUBJECT_TYPES: Final[frozenset[str]] = frozenset({"campaign", "budget"})

# The scale of the decision amount columns, ``numeric(18,6)``: PostgreSQL reads every stored
# amount back with exactly six fraction digits, so "187.65" is stored as "187.650000".
_STORED_QUANTUM: Final = Decimal("0.000001")

# Aliases for the budget-currency subqueries. They must differ from the outer statement's
# ``ad_account`` and ``campaign``: SQLAlchemy auto-correlates a scalar subquery with any
# table the enclosing statement already selects from, which would silently turn "the
# accounts of the campaigns drawing from this budget" into "the subject campaign's account".
_MemberAccount = aliased(AdAccount, name="member_account")
_MemberCampaign = aliased(Campaign, name="member_campaign")
_GoogleAccount = aliased(AdAccount, name="google_account")

# The shared budget a ``shared_member`` row assigns its campaign to, joined for its Google id.
# It must differ from the subject's own ``budget`` join: the correlated subqueries of
# _budget_currency correlate on the unaliased ``budget``, which must remain the budget
# *subject*, and joining ``budget`` a second time unaliased would name one table twice.
_AssignedBudget = aliased(Budget, name="assigned_budget")


# ---------------------------------------------------------------------------
# The joined read
# ---------------------------------------------------------------------------


def _budget_currency() -> Any:
    """Return the SQL expression for a shared budget's currency, or ``NULL`` when unknown.

    A Google budget carries no account of its own. The expression applies the rule
    ``ads_engine.decisions`` uses when it validates amounts: the currency of the accounts
    whose campaigns draw from the budget, or, when no campaign draws from it, the currency
    of the dataset's Google account. Amounts are never converted across currencies, so when
    either set holds more than one currency the answer is ``NULL`` (shown as unknown)
    rather than a guess. Reading the log never fails on it; recording a decision on such a
    budget is refused by the domain.

    The three correlated subqueries run only for budget subjects, through the ``CASE`` in
    :func:`_decision_query`, and each reads the indexed ``campaign.budget_id`` or
    ``ad_account`` rows of one dataset.

    Returns:
        A ``CASE`` expression over correlated scalar subqueries on the outer ``budget``.
    """
    single_currency = func.count(distinct(_MemberAccount.currency)) == 1
    member_count = (
        select(func.count(distinct(_MemberAccount.currency)))
        .select_from(_MemberAccount)
        .join(_MemberCampaign, _MemberCampaign.account_id == _MemberAccount.id)
        .where(_MemberCampaign.budget_id == Budget.id)
        .correlate(Budget)
        .scalar_subquery()
    )
    member_currency = (
        select(func.min(_MemberAccount.currency))
        .select_from(_MemberAccount)
        .join(_MemberCampaign, _MemberCampaign.account_id == _MemberAccount.id)
        .where(_MemberCampaign.budget_id == Budget.id)
        .having(single_currency)
        .correlate(Budget)
        .scalar_subquery()
    )
    dataset_currency = (
        select(func.min(_GoogleAccount.currency))
        .where(_GoogleAccount.dataset_id == Budget.dataset_id, _GoogleAccount.source == "google")
        .having(func.count(distinct(_GoogleAccount.currency)) == 1)
        .correlate(Budget)
        .scalar_subquery()
    )
    return case((member_count > 0, member_currency), else_=dataset_currency)


def _decision_query() -> Select[DecisionRow, str | None, str | None, str | None, str | None]:
    """Return the statement that reads decisions with their subjects' display fields.

    One statement, no per-row queries: each ``decision`` is outer-joined to the
    ``campaign`` or ``budget`` row its ``subject_type`` names (``subject_id`` cannot carry a
    foreign key, because the referenced table varies) within the decision's own dataset,
    and a campaign to its ``ad_account`` for the currency. A ``shared_member`` row is also
    outer-joined to the shared budget its ``budget_id`` assigns, within the same dataset
    (the (``budget_id``, ``dataset_id``) foreign key guarantees the row), for the Google id
    the product names that budget by. The outer joins keep a decision readable even if a
    joined row were missing; its name, currency or assigned budget's id are then ``NULL``.

    Callers add their own filters, ordering and limit.

    Returns:
        ``SELECT decision, campaign.name, budget.external_id, <subject currency>,
        assigned_budget.external_id``.
    """
    currency = case(
        (DecisionRow.subject_type == "campaign", AdAccount.currency),
        else_=_budget_currency(),
    ).label("subject_currency")
    return (
        select(
            DecisionRow,
            Campaign.name.label("campaign_name"),
            Budget.external_id.label("budget_external_id"),
            currency,
            _AssignedBudget.external_id.label("assigned_budget_external_id"),
        )
        .select_from(DecisionRow)
        .outerjoin(
            Campaign,
            and_(
                DecisionRow.subject_type == "campaign",
                Campaign.id == DecisionRow.subject_id,
                Campaign.dataset_id == DecisionRow.dataset_id,
            ),
        )
        .outerjoin(AdAccount, AdAccount.id == Campaign.account_id)
        .outerjoin(
            Budget,
            and_(
                DecisionRow.subject_type == "budget",
                Budget.id == DecisionRow.subject_id,
                Budget.dataset_id == DecisionRow.dataset_id,
            ),
        )
        .outerjoin(
            _AssignedBudget,
            and_(
                _AssignedBudget.id == DecisionRow.budget_id,
                _AssignedBudget.dataset_id == DecisionRow.dataset_id,
            ),
        )
    )


def _newest_decision_ids(
    dataset_ids: Sequence[int],
    *,
    subject_type: schemas.SubjectType | None,
    limit: int,
) -> Select[int]:
    """Return the ids of the newest ``limit`` decisions of each listed dataset.

    One bounded probe per dataset: a ``VALUES`` list of the dataset ids joined ``LATERAL``
    to ``ORDER BY decided_at DESC, id DESC LIMIT limit``, which
    ``ix_decision_dataset_decided`` (``dataset_id``, ``decided_at DESC``, ``id DESC``) serves
    as an index scan that stops after ``limit`` rows, however long the log grows. A single
    ``dataset_id IN (...)`` with the same order would read and sort every decision of the
    listed datasets instead, because the index orders rows within one dataset only. With
    ``subject_type`` the probe skips the other type's rows as it walks the same index.

    The newest ``limit`` rows of the union are exactly the newest ``limit`` rows of all the
    listed datasets together, since each of them is among its own dataset's newest
    ``limit``; the caller applies the final order and limit.

    Args:
        dataset_ids: The datasets to read, at least one.
        subject_type: Restrict to ``campaign`` or ``budget`` subjects.
        limit: Rows kept per dataset.

    Returns:
        ``SELECT id`` of at most ``limit`` decisions per dataset.
    """
    listed = values(column("dataset_id", BigInteger), name="listed_dataset").data(
        [(dataset_id,) for dataset_id in dataset_ids]
    )
    newest = select(DecisionRow.id).where(DecisionRow.dataset_id == listed.c.dataset_id)
    if subject_type is not None:
        newest = newest.where(DecisionRow.subject_type == subject_type)
    probe = (
        newest.order_by(DecisionRow.decided_at.desc(), DecisionRow.id.desc())
        .limit(limit)
        .lateral("newest_decision")
    )
    return select(probe.c.id).select_from(listed).join(probe, true())


def _subject_view(
    row: DecisionRow,
    *,
    campaign_name: str | None,
    budget_external_id: str | None,
    currency: str | None,
) -> schemas.SubjectView:
    """Build the subject view of one decision from the joined display fields.

    Args:
        row: The decision row.
        campaign_name: The joined campaign's name; ``None`` for budget subjects.
        budget_external_id: The joined budget's Google id; ``None`` for campaign subjects.
        currency: The subject's account currency; ``None`` when it cannot be determined.

    Returns:
        The subject view: a campaign by its name, a shared budget as
        ``Shared budget {external_id}``, the decision's source and the currency.
    """
    if row.subject_type == "campaign":
        name = campaign_name
    elif budget_external_id is not None:
        name = _SHARED_BUDGET_NAME.format(external_id=budget_external_id)
    else:
        name = None
    # model_validate rather than keyword construction: the ORM columns are plain ``str`` and
    # the view's fields are Literals, which the model checks at runtime.
    return schemas.SubjectView.model_validate(
        {
            "type": row.subject_type,
            "id": row.subject_id,
            "name": name,
            "source": row.source,
            "currency": currency,
        }
    )


async def _read_views(
    session: AsyncSession,
    statement: Select[DecisionRow, str | None, str | None, str | None, str | None],
) -> list[schemas.Decision]:
    """Execute a decision read and build one :class:`schemas.Decision` per row.

    Args:
        session: The request's session.
        statement: :func:`_decision_query` with the caller's filters applied.

    Returns:
        The decision views, in the statement's order, a shared member's carrying its
        assigned budget's Google id.
    """
    result = await session.execute(statement)
    return [
        schemas.Decision.from_row(
            row,
            subject=_subject_view(
                row,
                campaign_name=campaign_name,
                budget_external_id=budget_external_id,
                currency=currency,
            ),
            budget_external_id=assigned_budget_external_id,
        )
        for row, campaign_name, budget_external_id, currency, assigned_budget_external_id in result
    ]


def _stored_amount(amount: Decimal | None) -> Decimal | None:
    """Return ``amount`` at the scale its ``numeric(18,6)`` column stores and reads it back.

    The domain accepts only amounts no finer than the currency's minor unit, at most six
    fraction digits, so the quantization pads and never rounds: ``Decimal("187.65")``
    becomes ``Decimal("187.650000")``, the value ``GET /decisions`` reads for the same row.

    Args:
        amount: An amount of a decision row not yet read back from the database.

    Returns:
        The amount with exactly six fraction digits, or ``None`` when there is none.
    """
    return None if amount is None else amount.quantize(_STORED_QUANTUM)


# ---------------------------------------------------------------------------
# Decisions recorded from an item
# ---------------------------------------------------------------------------


def _offered_subject(value: object) -> tuple[str, int] | None:
    """Decode an item's stored ``decision_subject``, or ``None`` when it offers no decision.

    The runner stores ``{type, id}`` (``DecisionSubject.to_json``) on the items that carry
    "Record decision" and ``NULL`` on every other item (AAP 0.5.5). Stored ``jsonb`` may hold
    any JSON value, so anything that is not an object with a ``campaign`` or ``budget``
    ``type`` and a positive integer ``id`` (a JSON ``true`` is not an id) offers nothing.

    Args:
        value: ``recommendation.decision_subject`` as loaded.

    Returns:
        ``(type, id)`` of the subject the item offers a decision on, or ``None``.
    """
    if not isinstance(value, Mapping):
        return None
    subject_type = value.get("type")
    subject_id = value.get("id")
    if not isinstance(subject_type, str) or subject_type not in _SUBJECT_TYPES:
        return None
    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
        return None
    return (subject_type, subject_id)


def _require_offered_subject(recommendation: Recommendation, subject: schemas.SubjectIn) -> None:
    """Refuse a decision its item does not offer, before anything is locked or written.

    A decision recorded from an item is stored with ``origin='recommendation'`` and the
    item's id, so the item must offer exactly that decision: its ``decision_subject`` is the
    campaign or shared budget the decision writes (AAP 0.5.2, 0.5.5). The messages name the
    item and the subjects by type and id only.

    Args:
        recommendation: The item named by ``recommendation_id``.
        subject: The subject the request names.

    Raises:
        ApiError: 422 ``decision_not_offered`` when the item stores no decision subject, or
            one that does not decode; 422 ``subject_mismatch`` when it offers a decision on
            another subject (another id, or the other subject type).
    """
    offered = _offered_subject(recommendation.decision_subject)
    if offered is None:
        raise ApiError(
            422,
            "decision_not_offered",
            f"Recommendation {recommendation.id} offers no decision to record; record the "
            "decision without recommendation_id",
        )
    offered_type, offered_id = offered
    if (offered_type, offered_id) != (subject.type, subject.id):
        raise ApiError(
            422,
            "subject_mismatch",
            f"Recommendation {recommendation.id} offers a decision on {offered_type} "
            f"{offered_id}, not on {subject.type} {subject.id}",
        )


async def _subject_dataset_id(session: AsyncSession, subject: schemas.SubjectIn) -> int | None:
    """Return the dataset of the campaign or budget ``subject`` names, read without a lock.

    A subject row never moves to another dataset, so the unlocked read is the value the
    domain locks later.

    Returns:
        The subject's ``dataset_id``, or ``None`` when no such subject exists.
    """
    if subject.type == "campaign":
        statement = select(Campaign.dataset_id).where(Campaign.id == subject.id)
    else:
        statement = select(Budget.dataset_id).where(Budget.id == subject.id)
    return await session.scalar(statement)


async def _hold_item_current(
    session: AsyncSession, recommendation: Recommendation, subject: schemas.SubjectIn
) -> None:
    """Keep the item's data current until the caller's transaction ends, or refuse with 409.

    An item computed from a replaced dataset is historical-only and permits no write (AAP
    0.3.2). The domain locks only the subject's own dataset, but an item may also have read
    another source's dataset (a LinkedIn campaign's CRM evidence reads HubSpot's primary or
    the companion), whose activation could otherwise retire it between the check and the
    commit. In order:

    1. The stored provenance must decode and be current (:func:`is_current` against
       :func:`current_dataset_ids`), and so must the subject's dataset. Every dataset locked
       next was therefore active, never a dataset still building.
    2. Every provenance dataset and the subject's dataset is locked ``FOR SHARE``
       (:func:`~ads_engine.db.repositories.lock_dataset`) in ascending ``id`` order: a
       primary before its companion, the order activation and the domain's subject lock
       use, so no lock cycle can form with an activation. The domain's later lock of the
       subject's dataset is already held.
    3. Currentness is checked again under the locks. An activation that retired a dataset
       first has committed by now and is seen (READ COMMITTED); one that comes later waits
       for this transaction to end.

    A subject that does not exist is not locked, so the domain still answers 404.

    Args:
        session: The request's session; its transaction holds the locks.
        recommendation: The item, already known to offer a decision on ``subject``.
        subject: The subject the request names.

    Raises:
        ApiError: 409 ``subject_retired`` when the provenance is unverifiable, any of its
            datasets is not current before or after the locks, or the subject's dataset is
            not active.
    """
    provenance = item_provenance(recommendation)
    if not provenance:
        raise conflict("subject_retired", _HISTORICAL_MESSAGE)
    current_ids = await current_dataset_ids(session)
    if not is_current(provenance, current_ids):
        raise conflict("subject_retired", _HISTORICAL_MESSAGE)
    dataset_ids = {entry.dataset_id for entry in provenance}
    subject_dataset_id = await _subject_dataset_id(session, subject)
    if subject_dataset_id is not None:
        if subject_dataset_id not in current_ids:
            raise conflict(
                "subject_retired",
                f"{subject.type.capitalize()} {subject.id} does not belong to an active "
                "dataset; decisions apply only to the active dataset's subjects",
            )
        dataset_ids.add(subject_dataset_id)
    for dataset_id in sorted(dataset_ids):
        await lock_dataset(session, dataset_id)
    if not is_current(provenance, await current_dataset_ids(session)):
        raise conflict("subject_retired", _HISTORICAL_MESSAGE)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/decisions",
    response_model=list[schemas.Decision],
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def list_decisions(
    user: UserDep,
    session: SessionDep,
    subject_type: Annotated[schemas.SubjectType | None, Query()] = None,
    subject_id: Annotated[int | None, Query(gt=0, le=schemas.BIGINT_MAX)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[schemas.Decision]:
    """List the decision log of the active datasets, newest first.

    Each row carries its decision shape, the complete materialised expected state for that
    shape (status, the amounts the shape owns, a shared member's ``budget_id`` with that
    budget's Google id, ``budget_external_id``), its origin
    (``baseline``, ``user``, ``recommendation`` or ``resolution``), when and by whom it was
    decided, the patch the submission carried and its note (AAP 0.5.2, 0.5.5). Only
    decisions of each source's active primary dataset are listed, so decisions about
    replaced data never reappear. Rows are ordered by ``decided_at`` and then ``id``, both
    descending, so a subject's first row is its current "last decided" state.

    One subject's history is read through ``ix_decision_subject_latest``; the whole log, or
    one subject type's, through the newest ``limit`` rows of each active dataset
    (:func:`_newest_decision_ids`), merged into the same order and limit.

    Args:
        user: The signed-in user.
        session: The request's session; only read.
        subject_type: Restrict the log to ``campaign`` or ``budget`` subjects.
        subject_id: Restrict the log to one subject; needs ``subject_type``. 1 to
            ``schemas.BIGINT_MAX``: a larger id is a 422 before any SQL, and an unknown id
            in range lists nothing.
        limit: At most this many rows, 1 to 500 (default 200).

    Returns:
        The decision views, newest first.

    Raises:
        ApiError: 422 ``invalid_query`` when ``subject_id`` comes without ``subject_type``.
    """
    if subject_id is not None and subject_type is None:
        # Subject ids are internal ids of two different tables, so an id alone could name a
        # campaign and a shared budget at once; the log of "one subject" needs both keys.
        raise ApiError(
            422,
            "invalid_query",
            "subject_id requires subject_type: ids are unique only within a subject type",
        )
    active_ids = [dataset.id for dataset in (await active_datasets(session)).values()]
    if not active_ids:
        return []
    if subject_id is not None:
        # One subject's history, which ix_decision_subject_latest serves directly.
        statement = _decision_query().where(
            DecisionRow.dataset_id.in_(active_ids),
            DecisionRow.subject_type == subject_type,
            DecisionRow.subject_id == subject_id,
        )
    else:
        # The whole log: bounded per-dataset probes, never a sort of the whole history.
        newest = _newest_decision_ids(active_ids, subject_type=subject_type, limit=limit)
        statement = _decision_query().where(DecisionRow.id.in_(newest))
    statement = statement.order_by(DecisionRow.decided_at.desc(), DecisionRow.id.desc()).limit(
        limit
    )
    return await _read_views(session, statement)


@router.post(
    "/decisions",
    response_model=schemas.Decision,
    status_code=201,
    responses=schemas.ERROR_RESPONSES,
)
async def create_decision(
    body: schemas.DecisionRequest,
    user: UserDep,
    session: SessionDep,
    now: NowDep,
) -> schemas.Decision:
    """Record a decision on a campaign or shared budget, submitted as a patch.

    The patch carries only the fields being decided.
    :func:`ads_engine.decisions.record_decision` reads the subject's current expected
    state, applies the patch, validates the result for the subject's decision shape and
    appends one complete row, so
    a budget-only patch keeps the decided status and a status-only patch keeps the decided
    budget (AAP 0.5.2). It rejects an empty or no-op patch, a field the shape does not own
    (any amount on a shared member, ``status`` or both amounts on a shared budget, the
    budget field its period does not allow) and an amount finer than the currency's minor
    unit with 422; a subject of a non-active dataset with 409 ``subject_retired``; and it
    queues the analysis rerun in the same transaction (AAP 0.3.2). This route commits.

    A decision recorded from an item (``recommendation_id``) must be the decision that item
    offers, on the subject its ``decision_subject`` names, and is accepted only while the
    item still rests on current data: an item computed from a replaced dataset is
    historical-only, and every action on it is disabled (AAP 0.3.2, 0.5.5). The checks run
    404, then 422, then 409 before any lock, then 409 again under the dataset locks
    (:func:`_hold_item_current`), which stay held until this route commits; nothing is
    written and no rerun is queued when any of them refuses. A request without
    ``recommendation_id`` goes straight to the domain.

    Args:
        body: The subject, the patch, and optionally the item and a note.
        user: The signed-in user, recorded as ``decided_by``.
        session: The request's session; committed on success.
        now: The decision instant.

    Returns:
        The new decision with its shape and complete materialised expected state.

    Raises:
        ApiError: 404 ``not_found`` when ``recommendation_id`` names no item; 422
            ``decision_not_offered`` when that item offers no decision, and
            ``subject_mismatch`` when it offers one on another subject; 409
            ``subject_retired`` when the item is historical-only, before the locks or under
            them because a dataset it read was retired meanwhile, or the subject's dataset
            is not active.
        ads_engine.decisions.DecisionError: Propagated to the error handlers:
            ``PatchRejected`` (422, ``untracked_subject`` among its codes for a reported
            subject with no decision yet), ``SubjectRetired`` (409),
            ``PlatformStateUnknown`` (409 ``platform_state_unknown`` for a subject with no
            decision that no entity sync has reported) and ``SubjectNotFound`` (404).
    """
    recommendation_id = body.recommendation_id
    if recommendation_id is not None:
        recommendation = await session.get(Recommendation, recommendation_id)
        if recommendation is None:
            raise not_found(f"Recommendation {recommendation_id}")
        _require_offered_subject(recommendation, body.subject)
        await _hold_item_current(session, recommendation, body.subject)
    decision = await record_decision(
        SubjectRef(type=body.subject.type, id=body.subject.id),
        # Only the fields the client sent: an absent field keeps its decided value.
        DecisionPatch.from_mapping(body.patch.present_fields()),
        "recommendation" if recommendation_id is not None else "user",
        session=session,
        decided_by=user.email,
        recommendation_id=recommendation_id,
        note=body.note,
        now=now,
    )
    await session.commit()
    # record_decision already logs "decisions.recorded" when it flushes the row; this event
    # confirms the commit under its own name, so log-based counts see each decision once.
    _log.info(
        "decisions.committed",
        decision_id=decision.id,
        subject_type=decision.subject_type,
        subject_id=decision.subject_id,
        shape=decision.shape,
        origin=decision.origin,
    )
    # The answer renders the row as stored, exactly as GET /decisions does. Sessions keep
    # loaded attributes after commit, so without populate_existing this read would return
    # the identity-mapped row still holding the submitted amount ("187.65"), not the stored
    # numeric(18,6) value ("187.650000").
    views = await _read_views(
        session,
        _decision_query()
        .where(DecisionRow.id == decision.id)
        .execution_options(populate_existing=True),
    )
    if views:
        return views[0]
    # Unreachable unless the dataset was purged between the commit and this read: the row
    # is still in hand, so answer with it, its amounts at the stored scale, rather than fail
    # a committed write.
    view = schemas.Decision.from_row(
        decision,
        subject=_subject_view(decision, campaign_name=None, budget_external_id=None, currency=None),
    )
    return view.model_copy(
        update={
            "daily_budget": _stored_amount(view.daily_budget),
            "total_budget": _stored_amount(view.total_budget),
        }
    )
