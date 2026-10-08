"""Decision log routes: ``GET /decisions`` and ``POST /decisions`` (AAP 0.5.3).

The decision log is the append-only record of what was last decided for each decision
subject, a campaign of either source or a Google explicitly shared budget (AAP 0.5.2). The
``/decisions`` screen's ``DecisionLog`` lists it per subject with origin, time and the patch
applied, and ``DecisionForm`` records a new decision as a patch (AAP 0.5.5).

What this module does, and what it leaves to the domain:

* **Reading** is a single joined statement per request: each ``decision`` row with its
  subject's name, source and currency (:func:`_decision_query`), restricted to the active
  primary datasets, so decisions about replaced data never reappear in the log.
* **Writing** delegates every rule to :func:`ads_engine.decisions.record_decision`: the
  decision shape, the per-shape field rules, the currency's minor unit, empty and no-op
  patches, retired subjects and the rerun request all live there (AAP 0.5.2, 0.3.2). The
  route adds only what is about the *request*: a decision recorded from an item must come
  from an item that still rests on current data, and the transaction is committed here.
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

from typing import Annotated, Any, Final

from fastapi import APIRouter, Query
from sqlalchemy import Select, and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ads_engine.analysis.overlay import item_is_current
from ads_engine.api import schemas
from ads_engine.api.deps import ApiError, NowDep, SessionDep, UserDep, conflict, not_found
from ads_engine.db.models import AdAccount, Budget, Campaign, Recommendation
from ads_engine.db.models import Decision as DecisionRow
from ads_engine.db.repositories import active_datasets
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

# Aliases for the budget-currency subqueries. They must differ from the outer statement's
# ``ad_account`` and ``campaign``: SQLAlchemy auto-correlates a scalar subquery with any
# table the enclosing statement already selects from, which would silently turn "the
# accounts of the campaigns drawing from this budget" into "the subject campaign's account".
_MemberAccount = aliased(AdAccount, name="member_account")
_MemberCampaign = aliased(Campaign, name="member_campaign")
_GoogleAccount = aliased(AdAccount, name="google_account")


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


def _decision_query() -> Select[DecisionRow, str | None, str | None, str | None]:
    """Return the statement that reads decisions with their subjects' display fields.

    One statement, no per-row queries: each ``decision`` is outer-joined to the
    ``campaign`` or ``budget`` row its ``subject_type`` names (``subject_id`` cannot carry a
    foreign key, because the referenced table varies) within the decision's own dataset,
    and a campaign to its ``ad_account`` for the currency. The outer joins keep a decision
    readable even if its subject row were missing; its name and currency are then ``NULL``.

    Callers add their own filters, ordering and limit.

    Returns:
        ``SELECT decision, campaign.name, budget.external_id, <subject currency>``.
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
    )


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
    statement: Select[DecisionRow, str | None, str | None, str | None],
) -> list[schemas.Decision]:
    """Execute a decision read and build one :class:`schemas.Decision` per row.

    Args:
        session: The request's session.
        statement: :func:`_decision_query` with the caller's filters applied.

    Returns:
        The decision views, in the statement's order.
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
        )
        for row, campaign_name, budget_external_id, currency in result
    ]


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
    shape (status, the amounts the shape owns, a shared member's ``budget_id``), its origin
    (``baseline``, ``user``, ``recommendation`` or ``resolution``), when and by whom it was
    decided, the patch the submission carried and its note (AAP 0.5.2, 0.5.5). Only
    decisions of each source's active primary dataset are listed, so decisions about
    replaced data never reappear. Rows are ordered by ``decided_at`` and then ``id``, both
    descending, so a subject's first row is its current "last decided" state.

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
    statement = _decision_query().where(DecisionRow.dataset_id.in_(active_ids))
    if subject_type is not None:
        statement = statement.where(DecisionRow.subject_type == subject_type)
    if subject_id is not None:
        statement = statement.where(DecisionRow.subject_id == subject_id)
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

    A decision recorded from an item (``recommendation_id``) is accepted only while the
    item still rests on current data: an item computed from a replaced dataset is
    historical-only, and every action on it is disabled (AAP 0.3.2).

    Args:
        body: The subject, the patch, and optionally the item and a note.
        user: The signed-in user, recorded as ``decided_by``.
        session: The request's session; committed on success.
        now: The decision instant.

    Returns:
        The new decision with its shape and complete materialised expected state.

    Raises:
        ApiError: 404 ``not_found`` when ``recommendation_id`` names no item; 409
            ``subject_retired`` when that item is historical-only.
        ads_engine.decisions.DecisionError: Propagated to the error handlers:
            ``PatchRejected`` (422), ``SubjectRetired`` (409), ``SubjectNotFound`` (404).
    """
    recommendation_id = body.recommendation_id
    if recommendation_id is not None:
        recommendation = await session.get(Recommendation, recommendation_id)
        if recommendation is None:
            raise not_found(f"Recommendation {recommendation_id}")
        if not await item_is_current(session, recommendation):
            raise conflict("subject_retired", _HISTORICAL_MESSAGE)
    decision = await record_decision(
        session,
        SubjectRef(type=body.subject.type, id=body.subject.id),
        # Only the fields the client sent: an absent field keeps its decided value.
        DecisionPatch.from_mapping(body.patch.present_fields()),
        "recommendation" if recommendation_id is not None else "user",
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
    views = await _read_views(session, _decision_query().where(DecisionRow.id == decision.id))
    if views:
        return views[0]
    # Unreachable unless the dataset was purged between the commit and this read: the row
    # is still in hand, so answer with it rather than fail a committed write.
    return schemas.Decision.from_row(
        decision,
        subject=_subject_view(decision, campaign_name=None, budget_external_id=None, currency=None),
    )
