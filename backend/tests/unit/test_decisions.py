"""Unit tests for the pure core of ``ads_engine.decisions`` (AAP 0.5.2, 0.5.4.4).

What is proven here, with no database, no session and no network:

* **Decision shapes** (AAP 0.5.2 "Decisions: shapes"): a LinkedIn campaign and a Google
  campaign on a budget that is not explicitly shared are ``own_budget``; a Google campaign
  on an explicitly shared budget is a ``shared_member``; the shared budget itself is a
  ``budget`` subject.
* **The patch model** (AAP 0.5.2 "Decisions: patch model") through ``apply_patch``, the pure
  function ``record_decision`` applies each submitted ``DecisionPatch`` with: unsent fields
  keep their decided values, the result is the complete expected state, and every 422 rule
  (empty or no-change patch, amounts finer than the currency's minor unit or than a whole
  micro on Google, the wrong budget field for the period or for the campaign's budgets, any
  amount on a shared member, a status or two amounts on a budget subject).
* **Money helpers**: exact micros, minor-unit validity and half-up rounding.
* **Comparison at native precision** (rule C0, AAP 0.5.4.4): Google in micros, LinkedIn
  exactly; a shared member compares status and budget assignment only, a budget subject its
  amount only; ``expected_state_from_snapshot`` materialises each shape.
* **The resolution matrix** (AAP 0.5.4.4 "C0 resolutions by kind and subject type"): exactly
  eleven (kind, shape) pairs.

The database-backed paths (``record_decision``, ``resolve_discrepancy`` and the 409
``subject_retired`` refusal through the API) are proven in
``tests/integration/test_runs_and_decisions.py``.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, get_args

import pytest
import sqlalchemy as sa

from ads_engine.db.models.decisions import (
    DECISION_ORIGINS,
    DECISION_SHAPES,
    DISCREPANCY_KINDS,
    DISCREPANCY_RESOLUTIONS,
    SUBJECT_TYPES,
    Decision,
)
from ads_engine.decisions import (
    RESOLUTIONS,
    BudgetPeriod,
    CampaignStatus,
    DecisionError,
    DecisionPatch,
    DecisionShape,
    DiscrepancyClosed,
    DiscrepancyKind,
    ExpectedState,
    InvalidResolution,
    Mismatch,
    Origin,
    PatchRejected,
    Resolution,
    SubjectNotFound,
    SubjectRef,
    SubjectRetired,
    SubjectState,
    SubjectType,
    apply_patch,
    compare,
    decision_shape,
    expected_state_from_snapshot,
    from_micros,
    is_valid_amount,
    minor_unit_exponent,
    round_to_minor_unit,
    to_micros,
)

# ---------------------------------------------------------------------------------------
# Fixed identifiers. Every subject, snapshot and decision belongs to one active dataset.
# ---------------------------------------------------------------------------------------

DATASET: Final = 3
CAMPAIGN: Final = 11
OWN_BUDGET: Final = 601
"""A Google ``campaign_budget`` that is not explicitly shared: the campaign owns its amount."""
SHARED_BUDGET: Final = 501
SHARED_BUDGET_EXTERNAL: Final = "9876543210"
OTHER_SHARED_BUDGET: Final = 502
OTHER_SHARED_BUDGET_EXTERNAL: Final = "1234567890"
SNAPSHOT_ID: Final = 77
DECISION_ID: Final = 55
DECIDED_AT: Final = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
CAPTURED_AT: Final = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _entity_snapshot_model() -> type[Any]:
    """Return the mapped ``entity_snapshot`` model, the snapshot type ``compare`` reads.

    This module imports only its declared dependencies (``ads_engine.decisions`` and
    ``ads_engine.db.models.decisions``), so the snapshot model is taken from the ORM
    registry it shares with :class:`Decision` instead of from its own module. The tests
    therefore build real ``EntitySnapshot`` instances, whose constructor rejects a column
    the model does not define, rather than a stand-in that could drift from the model.
    """
    for mapper in Decision.registry.mappers:
        table = mapper.local_table
        if isinstance(table, sa.Table) and table.name == "entity_snapshot":
            model: type[Any] = mapper.class_
            return model
    raise LookupError("the entity_snapshot model is not registered beside Decision")


_ENTITY_SNAPSHOT: Final[type[Any]] = _entity_snapshot_model()


# ---------------------------------------------------------------------------------------
# Typed builders of subject states, expected states, snapshots and decisions
# ---------------------------------------------------------------------------------------


def _state(
    *,
    ref: SubjectRef,
    source: str,
    shape: DecisionShape,
    currency: str = "USD",
    name: str = "Campaign 11",
    has_daily_budget: bool = False,
    has_total_budget: bool = False,
    budget_period: BudgetPeriod | None = None,
    budget_id: int | None = None,
    budget_external_id: str | None = None,
    decided_budget_external_id: str | None = None,
) -> SubjectState:
    """Return a subject state in an active dataset, as ``load_subject_state`` builds it.

    ``source`` is a plain ``str`` so the refusal tests can pass a source without decision
    subjects; ``SubjectState`` itself checks it.
    """
    return SubjectState(
        ref=ref,
        dataset_id=DATASET,
        dataset_state="active",
        source=source,  # type: ignore[arg-type]  # validated by SubjectState itself
        currency=currency,
        name=name,
        shape=shape,
        has_daily_budget=has_daily_budget,
        has_total_budget=has_total_budget,
        budget_period=budget_period,
        budget_id=budget_id,
        budget_external_id=budget_external_id,
        latest_snapshot=None,
        current_decision=None,
        decided_budget_external_id=decided_budget_external_id,
    )


def _linkedin_campaign(
    *, daily: bool = True, total: bool = False, currency: str = "USD"
) -> SubjectState:
    """Return a LinkedIn campaign: always ``own_budget``, with its own daily/lifetime budget."""
    return _state(
        ref=SubjectRef(type="campaign", id=CAMPAIGN),
        source="linkedin",
        shape="own_budget",
        currency=currency,
        has_daily_budget=daily,
        has_total_budget=total,
    )


def _google_campaign(period: BudgetPeriod | None, *, currency: str = "USD") -> SubjectState:
    """Return a Google campaign on a budget that is not explicitly shared (``own_budget``).

    ``period`` is the period of the campaign's own budget, which fixes the one amount field
    it owns (``DAILY``: ``daily_budget``; ``CUSTOM_PERIOD``: ``total_budget``); ``None``
    means no entity sync reports a budget for it.
    """
    return _state(
        ref=SubjectRef(type="campaign", id=CAMPAIGN),
        source="google",
        shape="own_budget",
        currency=currency,
        has_daily_budget=period == "DAILY",
        has_total_budget=period == "CUSTOM_PERIOD",
        budget_period=period,
    )


def _google_member(
    *,
    budget_id: int = SHARED_BUDGET,
    budget_external_id: str = SHARED_BUDGET_EXTERNAL,
    decided_budget_external_id: str | None = None,
    period: BudgetPeriod = "DAILY",
) -> SubjectState:
    """Return a Google campaign drawing from an explicitly shared budget (``shared_member``).

    A member owns no amount, so neither budget field is allowed on it.
    """
    return _state(
        ref=SubjectRef(type="campaign", id=CAMPAIGN),
        source="google",
        shape="shared_member",
        budget_period=period,
        budget_id=budget_id,
        budget_external_id=budget_external_id,
        decided_budget_external_id=decided_budget_external_id,
    )


def _google_shared_budget(period: BudgetPeriod) -> SubjectState:
    """Return a Google explicitly shared budget subject (``budget``), with its period."""
    return _state(
        ref=SubjectRef(type="budget", id=SHARED_BUDGET),
        source="google",
        shape="budget",
        name=f"Shared budget {SHARED_BUDGET_EXTERNAL}",
        has_daily_budget=period == "DAILY",
        has_total_budget=period == "CUSTOM_PERIOD",
        budget_period=period,
    )


def _own(
    status: CampaignStatus = "active",
    *,
    daily: Decimal | None = None,
    total: Decimal | None = None,
    tombstone: bool = False,
) -> ExpectedState:
    """Return an ``own_budget`` expected state: a status plus the campaign's own amounts."""
    return ExpectedState(
        shape="own_budget",
        status=status,
        daily_budget=daily,
        total_budget=total,
        tombstone=tombstone,
    )


def _member(status: CampaignStatus = "active", *, budget_id: int = SHARED_BUDGET) -> ExpectedState:
    """Return a ``shared_member`` expected state: a status plus the assigned shared budget."""
    return ExpectedState(shape="shared_member", status=status, budget_id=budget_id)


def _budget(*, daily: Decimal | None = None, total: Decimal | None = None) -> ExpectedState:
    """Return a ``budget`` expected state: exactly one amount and no status."""
    return ExpectedState(shape="budget", daily_budget=daily, total_budget=total)


def _payload(state: SubjectState) -> dict[str, object]:
    """Return the shape metadata an entity sync records in a snapshot's ``payload``."""
    if state.ref.type == "budget":
        return {"period": state.budget_period, "explicitly_shared": True}
    return {
        "explicitly_shared": state.shape == "shared_member",
        "budget_period": state.budget_period,
    }


def _snapshot(
    state: SubjectState,
    *,
    status: str | None = "active",
    budget_daily: Decimal | None = None,
    budget_total: Decimal | None = None,
    budget_amount_micros: int | None = None,
    budget_id: int | None = None,
) -> Any:
    """Return the ``entity_snapshot`` an entity sync stores for ``state``'s subject.

    Google budgets carry their native ``budget_amount_micros``; LinkedIn campaigns carry
    ``budget_daily`` and ``budget_total`` in the account's currency.
    """
    return _ENTITY_SNAPSHOT(
        id=SNAPSHOT_ID,
        source=state.source,
        dataset_id=DATASET,
        entity_type=state.ref.type,
        entity_id=state.ref.id,
        captured_at=CAPTURED_AT,
        status=status,
        budget_daily=budget_daily,
        budget_total=budget_total,
        budget_amount_micros=budget_amount_micros,
        budget_id=budget_id,
        quality_score=None,
        payload=_payload(state),
    )


def _decision(state: SubjectState, expected: ExpectedState) -> Decision:
    """Return the ``decision`` row that records ``expected`` for ``state``'s subject."""
    return Decision(
        id=DECISION_ID,
        source=state.source,
        dataset_id=DATASET,
        subject_type=state.ref.type,
        subject_id=state.ref.id,
        shape=expected.shape,
        status=expected.status,
        daily_budget=expected.daily_budget,
        total_budget=expected.total_budget,
        budget_id=expected.budget_id,
        tombstone=expected.tombstone,
        decided_at=DECIDED_AT,
        decided_by=None,
        origin="baseline",
        patch={},
    )


def _tracked(state: SubjectState, expected: ExpectedState) -> tuple[SubjectState, Decision]:
    """Record ``expected`` as the subject's current decision, as ``load_subject_state`` reads it.

    Returns:
        The state carrying the decision as ``current_decision``, and the decision row.
    """
    decision = _decision(state, expected)
    return replace(state, current_decision=decision), decision


def _rejected(state: SubjectState, current: ExpectedState, patch: DecisionPatch) -> PatchRejected:
    """Apply a patch that must be refused, and return the refusal."""
    with pytest.raises(PatchRejected) as rejected:
        apply_patch(state, current, patch)
    return rejected.value


# ---------------------------------------------------------------------------------------
# Phase 1: decision shapes (AAP 0.5.2 "Decisions: shapes")
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "shape"),
    [
        pytest.param(_linkedin_campaign(), "own_budget", id="linkedin-campaign-daily-budget"),
        pytest.param(
            _linkedin_campaign(daily=False, total=True),
            "own_budget",
            id="linkedin-campaign-lifetime-budget",
        ),
        pytest.param(_google_campaign("DAILY"), "own_budget", id="google-campaign-own-daily"),
        pytest.param(
            _google_campaign("CUSTOM_PERIOD"), "own_budget", id="google-campaign-own-custom"
        ),
        pytest.param(_google_campaign(None), "own_budget", id="google-campaign-no-budget"),
        pytest.param(_google_member(), "shared_member", id="google-campaign-shared-budget"),
        pytest.param(_google_shared_budget("DAILY"), "budget", id="google-shared-budget-daily"),
        pytest.param(
            _google_shared_budget("CUSTOM_PERIOD"), "budget", id="google-shared-budget-custom"
        ),
    ],
)
def test_the_subject_state_fixes_the_decision_shape(
    state: SubjectState, shape: DecisionShape
) -> None:
    """AAP 0.5.2: the subject's current state, not the caller, fixes its decision shape."""
    assert decision_shape(state) == shape
    assert state.shape == shape


@pytest.mark.parametrize(
    ("source", "ref", "budget_id", "claimed", "message"),
    [
        pytest.param(
            "google",
            SubjectRef(type="campaign", id=CAMPAIGN),
            SHARED_BUDGET,
            "own_budget",
            "disagrees with the subject state",
            id="google-campaign-on-shared-budget-claimed-own-budget",
        ),
        pytest.param(
            "google",
            SubjectRef(type="campaign", id=CAMPAIGN),
            None,
            "shared_member",
            "disagrees with the subject state",
            id="google-campaign-on-own-budget-claimed-shared-member",
        ),
        pytest.param(
            "google",
            SubjectRef(type="budget", id=SHARED_BUDGET),
            None,
            "own_budget",
            "disagrees with the subject state",
            id="google-shared-budget-claimed-own-budget",
        ),
        pytest.param(
            "linkedin",
            SubjectRef(type="campaign", id=CAMPAIGN),
            SHARED_BUDGET,
            "shared_member",
            "LinkedIn budgets live on the campaign row",
            id="linkedin-campaign-with-shared-budget",
        ),
        pytest.param(
            "linkedin",
            SubjectRef(type="budget", id=SHARED_BUDGET),
            None,
            "budget",
            "LinkedIn budgets live on the campaign row",
            id="linkedin-budget-subject",
        ),
        pytest.param(
            "hubspot",
            SubjectRef(type="campaign", id=CAMPAIGN),
            None,
            "own_budget",
            "has no decision subjects",
            id="hubspot-has-no-subjects",
        ),
    ],
)
def test_a_state_whose_shape_disagrees_with_its_fields_is_refused(
    source: str, ref: SubjectRef, budget_id: int | None, claimed: DecisionShape, message: str
) -> None:
    """AAP 0.5.2: a shape that does not follow from the subject's state is never accepted."""
    with pytest.raises(ValueError, match=message):
        _state(ref=ref, source=source, shape=claimed, budget_id=budget_id)


# ---------------------------------------------------------------------------------------
# Phase 2: patch materialisation and the 422 rules (AAP 0.5.2 "Decisions: patch model")
# ---------------------------------------------------------------------------------------

LINKEDIN_ACTIVE_100: Final = _own("active", daily=Decimal(100))
"""The decided state of a LinkedIn campaign with a 100 USD daily budget."""


def test_a_status_patch_keeps_the_decided_daily_budget() -> None:
    """AAP 0.5.2: a status-only decision keeps the last decided budget."""
    result = apply_patch(_linkedin_campaign(), LINKEDIN_ACTIVE_100, DecisionPatch(status="paused"))

    assert result == ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(100))


def test_a_budget_patch_keeps_the_decided_status() -> None:
    """AAP 0.5.2: a budget-only decision keeps the last decided status."""
    result = apply_patch(
        _linkedin_campaign(), LINKEDIN_ACTIVE_100, DecisionPatch(daily_budget=Decimal(120))
    )

    assert result == ExpectedState(shape="own_budget", status="active", daily_budget=Decimal(120))


def test_the_result_is_the_complete_materialised_expected_state() -> None:
    """AAP 0.5.2: one complete row is written, every unsent field carried from the decision."""
    state = _linkedin_campaign(daily=True, total=True)
    current = _own("paused", daily=Decimal(100), total=Decimal(5000))

    result = apply_patch(state, current, DecisionPatch(total_budget=Decimal(6000)))

    assert result == ExpectedState(
        shape="own_budget",
        status="paused",
        daily_budget=Decimal(100),
        total_budget=Decimal(6000),
        budget_id=None,
        tombstone=False,
    )


def test_an_empty_patch_is_rejected() -> None:
    """AAP 0.5.2: a patch with no field to decide is rejected with 422."""
    error = _rejected(_linkedin_campaign(), LINKEDIN_ACTIVE_100, DecisionPatch())

    assert (error.status_code, error.code) == (422, "empty_patch")


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(DecisionPatch(status="active"), id="same-status"),
        pytest.param(DecisionPatch(daily_budget=Decimal(100)), id="same-budget"),
        pytest.param(DecisionPatch(daily_budget=Decimal("100.00")), id="same-budget-other-scale"),
        pytest.param(
            DecisionPatch(status="active", daily_budget=Decimal(100)), id="same-status-and-budget"
        ),
    ],
)
def test_a_patch_that_changes_nothing_is_rejected(patch: DecisionPatch) -> None:
    """AAP 0.5.2: a patch equal to the decided state (amounts compared by value) is a 422."""
    error = _rejected(_linkedin_campaign(), LINKEDIN_ACTIVE_100, patch)

    assert (error.status_code, error.code) == (422, "no_change")


@pytest.mark.parametrize(
    ("currency", "amount"),
    [
        pytest.param("USD", Decimal("12.34"), id="usd-cents"),
        pytest.param("USD", Decimal("12.3"), id="usd-dimes"),
        pytest.param("USD", Decimal("12.340000"), id="usd-trailing-zeros"),
        pytest.param("JPY", Decimal(100), id="jpy-whole-yen"),
        pytest.param("KWD", Decimal("12.345"), id="kwd-fils"),
    ],
)
def test_a_linkedin_amount_at_the_minor_unit_is_accepted(currency: str, amount: Decimal) -> None:
    """AAP 0.5.2: LinkedIn amounts are validated to the account currency's minor unit."""
    current = _own("active", daily=Decimal(1000))

    result = apply_patch(
        _linkedin_campaign(currency=currency), current, DecisionPatch(daily_budget=amount)
    )

    assert result == _own("active", daily=amount)


@pytest.mark.parametrize(
    ("currency", "amount"),
    [
        pytest.param("USD", Decimal("12.345"), id="usd-finer-than-a-cent"),
        pytest.param("JPY", Decimal("100.5"), id="jpy-finer-than-a-yen"),
        pytest.param("KWD", Decimal("12.3456"), id="kwd-finer-than-a-fils"),
    ],
)
def test_a_linkedin_amount_finer_than_the_minor_unit_is_rejected(
    currency: str, amount: Decimal
) -> None:
    """AAP 0.5.2: an amount finer than the currency's minor unit is rejected with 422."""
    current = _own("active", daily=Decimal(1000))

    error = _rejected(
        _linkedin_campaign(currency=currency), current, DecisionPatch(daily_budget=amount)
    )

    assert (error.status_code, error.code) == (422, "sub_minor_unit")
    assert error.details == {"field": "daily_budget"}
    assert currency in error.message


def test_a_google_daily_budget_in_whole_micros_is_accepted() -> None:
    """AAP 0.5.2: a Google amount is valid when it is a whole number of micros."""
    current = _own("active", daily=Decimal("10.000000"))

    result = apply_patch(
        _google_campaign("DAILY"), current, DecisionPatch(daily_budget=Decimal("12.345678"))
    )

    assert result == _own("active", daily=Decimal("12.345678"))


def test_a_google_amount_finer_than_a_micro_is_rejected() -> None:
    """AAP 0.5.2: a Google amount finer than one micro is rejected with 422."""
    current = _own("active", daily=Decimal("10.000000"))

    error = _rejected(
        _google_campaign("DAILY"), current, DecisionPatch(daily_budget=Decimal("12.3456789"))
    )

    assert (error.status_code, error.code) == (422, "sub_minor_unit")
    assert "whole number of micros" in error.message


def test_a_google_daily_campaign_rejects_a_total_budget() -> None:
    """AAP 0.5.2: a Google ``DAILY`` budget takes ``daily_budget``, never ``total_budget``."""
    current = _own("active", daily=Decimal("10.000000"))

    error = _rejected(
        _google_campaign("DAILY"), current, DecisionPatch(total_budget=Decimal("500"))
    )

    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert error.details == {"field": "total_budget"}
    assert "DAILY budget, which takes daily_budget, not total_budget" in error.message


def test_a_google_custom_period_campaign_accepts_a_total_budget() -> None:
    """AAP 0.5.2: a Google ``CUSTOM_PERIOD`` budget takes ``total_budget``."""
    current = _own("paused", total=Decimal("500.000000"))

    result = apply_patch(
        _google_campaign("CUSTOM_PERIOD"), current, DecisionPatch(total_budget=Decimal("750.5"))
    )

    assert result == _own("paused", total=Decimal("750.5"))


def test_a_google_custom_period_campaign_rejects_a_daily_budget() -> None:
    """AAP 0.5.2: a Google ``CUSTOM_PERIOD`` budget never takes ``daily_budget``."""
    current = _own("paused", total=Decimal("500.000000"))

    error = _rejected(
        _google_campaign("CUSTOM_PERIOD"), current, DecisionPatch(daily_budget=Decimal(25))
    )

    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert error.details == {"field": "daily_budget"}
    assert "CUSTOM_PERIOD budget, which takes total_budget, not daily_budget" in error.message


@pytest.mark.parametrize(
    ("state", "current", "patch", "message"),
    [
        pytest.param(
            _linkedin_campaign(daily=False, total=True),
            _own("active", total=Decimal(5000)),
            DecisionPatch(daily_budget=Decimal(50)),
            "has no daily budget",
            id="linkedin-daily-on-lifetime-only-campaign",
        ),
        pytest.param(
            _linkedin_campaign(daily=True, total=False),
            LINKEDIN_ACTIVE_100,
            DecisionPatch(total_budget=Decimal(5000)),
            "has no lifetime budget",
            id="linkedin-total-on-daily-only-campaign",
        ),
        pytest.param(
            _linkedin_campaign(daily=False, total=False),
            _own("active"),
            DecisionPatch(daily_budget=Decimal(50)),
            "has no daily budget",
            id="linkedin-daily-on-budgetless-campaign",
        ),
        pytest.param(
            _google_campaign(None),
            _own("active"),
            DecisionPatch(daily_budget=Decimal(50)),
            "has no budget",
            id="google-daily-on-budgetless-campaign",
        ),
        pytest.param(
            _google_campaign(None),
            _own("active"),
            DecisionPatch(total_budget=Decimal(50)),
            "has no budget",
            id="google-total-on-budgetless-campaign",
        ),
    ],
)
def test_a_budget_field_the_campaign_does_not_have_is_rejected(
    state: SubjectState, current: ExpectedState, patch: DecisionPatch, message: str
) -> None:
    """AAP 0.5.2: no daily budget on a campaign without one, no total without a lifetime one."""
    error = _rejected(state, current, patch)

    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert message in error.message


@pytest.mark.parametrize(
    ("state", "current"),
    [
        pytest.param(_linkedin_campaign(), LINKEDIN_ACTIVE_100, id="linkedin"),
        pytest.param(
            _google_campaign("DAILY"), _own("active", daily=Decimal("10.000000")), id="google"
        ),
    ],
)
@pytest.mark.parametrize(
    "amount",
    [
        pytest.param(Decimal(0), id="zero"),
        pytest.param(Decimal(-5), id="negative"),
        pytest.param(Decimal(10) ** 12, id="beyond-numeric-18-6"),
    ],
)
def test_a_non_positive_or_unstorable_amount_is_rejected(
    state: SubjectState, current: ExpectedState, amount: Decimal
) -> None:
    """AAP 0.5.2: a decided amount is positive and fits ``numeric(18, 6)``."""
    error = _rejected(state, current, DecisionPatch(daily_budget=amount))

    assert (error.status_code, error.code) == (422, "invalid_amount")
    assert error.details == {"field": "daily_budget"}


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(DecisionPatch(daily_budget=Decimal(80)), id="daily-budget"),
        pytest.param(DecisionPatch(total_budget=Decimal(800)), id="total-budget"),
        pytest.param(
            DecisionPatch(daily_budget=Decimal(80), total_budget=Decimal(800)), id="both-amounts"
        ),
        pytest.param(
            DecisionPatch(status="paused", daily_budget=Decimal(80)), id="status-with-amount"
        ),
    ],
)
def test_a_shared_member_rejects_any_amount_naming_the_owning_budget(
    patch: DecisionPatch,
) -> None:
    """AAP 0.5.2: a member owns no amount; the error names the shared budget that does."""
    error = _rejected(_google_member(), _member("active"), patch)

    assert (error.status_code, error.code) == (422, "shared_budget_amount")
    assert SHARED_BUDGET_EXTERNAL in error.message
    assert str(SHARED_BUDGET) in error.message
    assert error.details["budget_id"] == SHARED_BUDGET
    assert error.details["budget_external_id"] == SHARED_BUDGET_EXTERNAL


def test_a_shared_member_status_patch_keeps_its_budget_assignment() -> None:
    """AAP 0.5.2: a member's status decision keeps ``budget_id``; it changes only by resolution."""
    result = apply_patch(_google_member(), _member("active"), DecisionPatch(status="paused"))

    assert result == ExpectedState(shape="shared_member", status="paused", budget_id=SHARED_BUDGET)
    assert (result.daily_budget, result.total_budget) == (None, None)


def test_a_daily_shared_budget_accepts_its_daily_amount_with_no_status() -> None:
    """AAP 0.5.2: a ``DAILY`` budget subject takes ``daily_budget`` and has no status."""
    current = _budget(daily=Decimal("50.000000"))

    result = apply_patch(
        _google_shared_budget("DAILY"), current, DecisionPatch(daily_budget=Decimal("60.5"))
    )

    assert result == ExpectedState(shape="budget", daily_budget=Decimal("60.5"))
    assert result.status is None
    assert result.total_budget is None
    assert result.budget_id is None


@pytest.mark.parametrize(
    ("patch", "detail"),
    [
        pytest.param(DecisionPatch(status="paused"), {"field": "status"}, id="status"),
        pytest.param(
            DecisionPatch(status="paused", daily_budget=Decimal(60)),
            {"field": "status"},
            id="status-with-amount",
        ),
        pytest.param(
            DecisionPatch(daily_budget=Decimal(60), total_budget=Decimal(600)),
            {"fields": ["daily_budget", "total_budget"]},
            id="both-amounts",
        ),
        pytest.param(
            DecisionPatch(total_budget=Decimal(600)), {"field": "total_budget"}, id="total-budget"
        ),
    ],
)
def test_a_daily_shared_budget_rejects_a_status_two_amounts_or_the_wrong_amount(
    patch: DecisionPatch, detail: dict[str, object]
) -> None:
    """AAP 0.5.2: a budget subject has no status and takes only the one amount its period allows."""
    error = _rejected(_google_shared_budget("DAILY"), _budget(daily=Decimal("50.000000")), patch)

    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert error.details == detail


def test_a_custom_period_shared_budget_accepts_its_total_amount() -> None:
    """AAP 0.5.2: a ``CUSTOM_PERIOD`` budget subject takes ``total_budget``."""
    current = _budget(total=Decimal("1000.000000"))

    result = apply_patch(
        _google_shared_budget("CUSTOM_PERIOD"), current, DecisionPatch(total_budget=Decimal(1200))
    )

    assert result == ExpectedState(shape="budget", total_budget=Decimal(1200))
    assert result.status is None
    assert result.daily_budget is None


def test_a_custom_period_shared_budget_rejects_a_daily_amount() -> None:
    """AAP 0.5.2: a ``CUSTOM_PERIOD`` budget subject never takes ``daily_budget``."""
    error = _rejected(
        _google_shared_budget("CUSTOM_PERIOD"),
        _budget(total=Decimal("1000.000000")),
        DecisionPatch(daily_budget=Decimal(10)),
    )

    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert error.details == {"field": "daily_budget"}
    assert "CUSTOM_PERIOD period, which takes total_budget, not daily_budget" in error.message


def test_a_tombstoned_subject_takes_no_further_patch() -> None:
    """AAP 0.5.4.4: a subject tombstoned by ``acknowledge_removal`` takes no further decision."""
    current = _own("ended", daily=Decimal(100), tombstone=True)

    error = _rejected(_linkedin_campaign(), current, DecisionPatch(status="active"))

    assert (error.status_code, error.code) == (422, "subject_removed")


def test_a_patch_after_the_shape_changed_is_rejected() -> None:
    """AAP 0.5.2: the current state fixes the shape; an assignment changes only by resolution."""
    current = _own("active", daily=Decimal("10.000000"))

    error = _rejected(_google_member(), current, DecisionPatch(status="paused"))

    assert (error.status_code, error.code) == (422, "shape_changed")
    assert error.details == {"decided_shape": "own_budget", "current_shape": "shared_member"}


def test_a_patch_from_submitted_fields_carries_exactly_those_fields() -> None:
    """AAP 0.5.3 ``POST /decisions``: the patch carries only the fields being decided."""
    patch = DecisionPatch.from_mapping({"status": "paused", "daily_budget": "120.50"})

    assert patch.fields_set() == frozenset({"status", "daily_budget"})
    assert patch == DecisionPatch(status="paused", daily_budget=Decimal("120.50"))
    assert patch.as_json() == {"status": "paused", "daily_budget": "120.50"}


@pytest.mark.parametrize(
    ("data", "code"),
    [
        pytest.param({"daily_budget": 12.5}, "invalid_value", id="float-amount"),
        pytest.param({"daily_budget": None}, "invalid_value", id="null-amount"),
        pytest.param({"daily_budget": "twelve"}, "invalid_value", id="non-numeric-amount"),
        pytest.param({"daily_budget": "NaN"}, "invalid_value", id="nan-amount"),
        pytest.param({"status": "archived"}, "invalid_value", id="unknown-status"),
        pytest.param({"budget_id": SHARED_BUDGET}, "unknown_field", id="budget-assignment"),
    ],
)
def test_submitted_fields_that_are_not_a_valid_patch_are_rejected(
    data: dict[str, object], code: str
) -> None:
    """AAP 0.5.2: money is exact (never ``float``) and a patch carries only decidable fields."""
    with pytest.raises(PatchRejected) as rejected:
        DecisionPatch.from_mapping(data)

    assert (rejected.value.status_code, rejected.value.code) == (422, code)


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        pytest.param(PatchRejected, 422, "patch_rejected", id="patch-rejected"),
        pytest.param(InvalidResolution, 422, "resolution_not_allowed", id="invalid-resolution"),
        pytest.param(SubjectRetired, 409, "subject_retired", id="subject-retired"),
        pytest.param(SubjectNotFound, 404, "not_found", id="subject-not-found"),
        pytest.param(DiscrepancyClosed, 409, "discrepancy_resolved", id="discrepancy-closed"),
    ],
)
def test_each_rejection_carries_its_http_status_and_code(
    error: type[DecisionError], status_code: int, code: str
) -> None:
    """AAP 0.5.3: domain rejections map to ``{"error": {"code", "message"}}`` at their status."""
    raised = error()

    assert isinstance(raised, DecisionError)
    assert (raised.status_code, raised.code) == (status_code, code)
    assert raised.message
    assert str(raised) == raised.message


def test_a_named_rule_keeps_the_class_status_code() -> None:
    """AAP 0.5.3: a specific code and message never change the rejection's HTTP status."""
    error = PatchRejected("empty_patch", "The patch carries no field to decide.")

    assert (error.status_code, error.code) == (422, "empty_patch")
    assert error.message == "The patch carries no field to decide."
    assert error.details == {}


# ---------------------------------------------------------------------------------------
# Phase 3: money helpers (AAP 0.5.2 conventions; B1 rounding, AAP 0.5.4.5)
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("currency", "exponent"),
    [
        pytest.param("USD", 2, id="usd"),
        pytest.param("EUR", 2, id="eur"),
        pytest.param("JPY", 0, id="jpy"),
        pytest.param(" jpy ", 0, id="jpy-case-and-whitespace"),
        pytest.param("KRW", 0, id="krw"),
        pytest.param("KWD", 3, id="kwd"),
    ],
)
def test_minor_unit_exponent_follows_iso_4217(currency: str, exponent: int) -> None:
    """AAP 0.5.2: amounts are validated to the account currency's minor unit."""
    assert minor_unit_exponent(currency) == exponent


@pytest.mark.parametrize(
    ("amount", "micros"),
    [
        pytest.param(Decimal("12.345678"), 12_345_678, id="six-places"),
        pytest.param(Decimal("12.3456780"), 12_345_678, id="trailing-zero-below-a-micro"),
        pytest.param(Decimal(100), 100_000_000, id="whole-units"),
        pytest.param(Decimal("0.000001"), 1, id="one-micro"),
        pytest.param(Decimal("-1.5"), -1_500_000, id="negative"),
    ],
)
def test_to_micros_converts_exactly(amount: Decimal, micros: int) -> None:
    """AAP 0.5.4.4: Google amounts compare as ``amount_micros``, converted with no rounding."""
    assert to_micros(amount) == micros
    assert from_micros(micros) == amount


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param(Decimal("12.3456789"), id="finer-than-a-micro"),
        pytest.param(Decimal("12.3456780000000000000000000001"), id="far-finer-than-a-micro"),
        pytest.param(Decimal("Infinity"), id="infinite"),
        pytest.param(Decimal("NaN"), id="nan"),
    ],
)
def test_to_micros_refuses_what_is_not_whole_micros(amount: Decimal) -> None:
    """AAP 0.5.4.4: no rounding can hide a sub-micro difference."""
    with pytest.raises(ValueError, match="is not a whole number of micros"):
        to_micros(amount)


def test_from_micros_keeps_six_decimal_places() -> None:
    """AAP 0.5.2: Google ``cost_micros`` and budgets divide by 1,000,000 exactly."""
    assert from_micros(12_345_679) == Decimal("12.345679")
    assert format(from_micros(100_000_000), "f") == "100.000000"


AMOUNT_VALIDITY: Final = [
    pytest.param("linkedin", "USD", Decimal("12.34"), True, id="linkedin-usd-cents"),
    pytest.param("linkedin", "USD", Decimal("12.345"), False, id="linkedin-usd-sub-cent"),
    pytest.param("linkedin", "JPY", Decimal(100), True, id="linkedin-jpy-whole"),
    pytest.param("linkedin", "JPY", Decimal("100.5"), False, id="linkedin-jpy-fraction"),
    pytest.param("linkedin", "KWD", Decimal("12.345"), True, id="linkedin-kwd-fils"),
    pytest.param("google", "USD", Decimal("12.345678"), True, id="google-whole-micros"),
    pytest.param("google", "USD", Decimal("12.3456789"), False, id="google-sub-micro"),
    pytest.param("google", "JPY", Decimal("12.345678"), True, id="google-micros-any-currency"),
    pytest.param("google", "USD", Decimal(0), False, id="google-zero"),
    pytest.param("linkedin", "USD", Decimal(-1), False, id="linkedin-negative"),
]
"""Amounts with their validity per source and currency (AAP 0.5.2)."""


@pytest.mark.parametrize(("source", "currency", "amount", "valid"), AMOUNT_VALIDITY)
def test_is_valid_amount_per_source_and_currency(
    source: str, currency: str, amount: Decimal, valid: bool
) -> None:
    """AAP 0.5.2: LinkedIn to the minor unit, Google to whole micros, always positive."""
    assert is_valid_amount(amount, source=source, currency=currency) is valid


@pytest.mark.parametrize(("source", "currency", "amount", "valid"), AMOUNT_VALIDITY)
def test_is_valid_amount_agrees_with_the_patch_rules(
    source: str, currency: str, amount: Decimal, valid: bool
) -> None:
    """AAP 0.5.2: ``apply_patch`` accepts a daily budget exactly when it is a valid amount."""
    if source == "google":
        state = _google_campaign("DAILY", currency=currency)
    else:
        state = _linkedin_campaign(currency=currency)
    current = _own("active", daily=Decimal(1000))
    patch = DecisionPatch(daily_budget=amount)

    if valid:
        assert apply_patch(state, current, patch).daily_budget == amount
    else:
        assert _rejected(state, current, patch).code in {"sub_minor_unit", "invalid_amount"}


def test_is_valid_amount_refuses_a_float_and_a_source_without_subjects() -> None:
    """AAP 0.5.2: money is never binary floating point; HubSpot has no decision subjects."""
    binary: Any = 12.34
    assert is_valid_amount(binary, source="linkedin", currency="USD") is False

    with pytest.raises(ValueError, match="has no decision subjects"):
        is_valid_amount(Decimal("12.34"), source="hubspot", currency="USD")


@pytest.mark.parametrize(
    ("amount", "currency", "rounded"),
    [
        pytest.param(Decimal("123.456"), "USD", Decimal("123.46"), id="usd-up"),
        pytest.param(Decimal("123.454"), "USD", Decimal("123.45"), id="usd-down"),
        # ROUND_HALF_UP, as the module documents: a half rounds away from zero, so 0.125
        # becomes 0.13 (banker's rounding, ROUND_HALF_EVEN, would give 0.12).
        pytest.param(Decimal("0.125"), "USD", Decimal("0.13"), id="usd-half-up"),
        pytest.param(Decimal(120), "USD", Decimal("120.00"), id="usd-whole"),
        pytest.param(Decimal("1234.5"), "JPY", Decimal(1235), id="jpy-half-up"),
        pytest.param(Decimal("1.23456"), "KWD", Decimal("1.235"), id="kwd"),
    ],
)
def test_round_to_minor_unit_rounds_half_up_to_the_currency_minor_unit(
    amount: Decimal, currency: str, rounded: Decimal
) -> None:
    """AAP 0.5.4.5 (B1): a proposed amount is rounded to the currency's minor unit, half up."""
    result = round_to_minor_unit(amount, currency)

    assert result == rounded
    assert result.as_tuple().exponent == -minor_unit_exponent(currency)
    # The rounded amount is also valid for both sources: a minor unit is at least a micro.
    assert is_valid_amount(result, source="linkedin", currency=currency)
    assert is_valid_amount(result, source="google", currency=currency)


def test_a_rounded_amount_is_whole_micros_for_google() -> None:
    """AAP 0.5.2: Google decisions are whole micros, so a rounded amount converts exactly."""
    raised = round_to_minor_unit(Decimal("12.345678") * Decimal("1.2"), "USD")

    assert raised == Decimal("14.81")
    assert to_micros(raised) == 14_810_000
    assert to_micros(round_to_minor_unit(Decimal("123.456"), "USD")) == 123_460_000


def test_round_to_minor_unit_refuses_a_non_finite_amount() -> None:
    """AAP 0.5.2: an amount that is not finite is never decided."""
    with pytest.raises(ValueError, match="is not a finite amount"):
        round_to_minor_unit(Decimal("Infinity"), "USD")


# ---------------------------------------------------------------------------------------
# Phase 4: comparison at native precision (rule C0, AAP 0.5.4.4)
# ---------------------------------------------------------------------------------------


def test_a_google_budget_one_micro_apart_is_a_budget_mismatch() -> None:
    """AAP 0.5.4.4 (C0): Google compares ``amount_micros`` with the decided amount in micros."""
    state = _google_campaign("DAILY")
    state, decision = _tracked(state, _own("active", daily=Decimal("12.345678")))
    snapshot = _snapshot(state, budget_id=OWN_BUDGET, budget_amount_micros=12_345_679)

    assert compare(snapshot, decision, state=state) == [
        Mismatch(
            kind="budget",
            decided_value={
                "daily_budget": "12.345678",
                "total_budget": None,
                "currency": "USD",
                "amount_micros": 12_345_678,
            },
            platform_value={
                "daily_budget": "12.345679",
                "total_budget": None,
                "currency": "USD",
                "amount_micros": 12_345_679,
            },
        )
    ]


@pytest.mark.parametrize(
    ("period", "decided", "micros"),
    [
        pytest.param("DAILY", _own("active", daily=Decimal("12.345678")), 12_345_678, id="daily"),
        pytest.param(
            "CUSTOM_PERIOD", _own("active", total=Decimal("500")), 500_000_000, id="custom-period"
        ),
    ],
)
def test_identical_google_micros_are_no_mismatch(
    period: BudgetPeriod, decided: ExpectedState, micros: int
) -> None:
    """AAP 0.5.4.4 (C0): no tolerance, and no false mismatch from the decimal scale."""
    state, decision = _tracked(_google_campaign(period), decided)
    snapshot = _snapshot(state, budget_id=OWN_BUDGET, budget_amount_micros=micros)

    assert compare(snapshot, decision, state=state) == []


def test_a_google_amount_google_omitted_raises_no_mismatch() -> None:
    """AAP 0.5.4.4 (C0): an unreported amount is unknown, never zero, so nothing is raised."""
    state = _google_campaign("DAILY")
    state, decision = _tracked(state, _own("active", daily=Decimal("12.345678")))
    snapshot = _snapshot(state, budget_id=OWN_BUDGET, budget_amount_micros=None)

    assert compare(snapshot, decision, state=state) == []


def test_a_linkedin_budget_one_minor_unit_apart_is_a_budget_mismatch() -> None:
    """AAP 0.5.4.4 (C0): LinkedIn amounts compare exactly at the currency's minor unit."""
    state = _linkedin_campaign()
    state, decision = _tracked(state, _own("active", daily=Decimal("100.00")))
    snapshot = _snapshot(state, budget_daily=Decimal("100.01"))

    assert compare(snapshot, decision, state=state) == [
        Mismatch(
            kind="budget",
            decided_value={"daily_budget": "100.00", "total_budget": None, "currency": "USD"},
            platform_value={"daily_budget": "100.01", "total_budget": None, "currency": "USD"},
        )
    ]


def test_an_equal_linkedin_budget_at_another_scale_is_no_mismatch() -> None:
    """AAP 0.5.4.4 (C0): equality is by value, so rounding can neither cause nor hide one."""
    state = _linkedin_campaign()
    state, decision = _tracked(state, _own("active", daily=Decimal("100.00")))
    snapshot = _snapshot(state, budget_daily=Decimal("100.0"))

    assert compare(snapshot, decision, state=state) == []


def test_a_different_status_is_a_status_mismatch() -> None:
    """AAP 0.5.4.4 (C0): ``status`` is raised where the normalised status differs."""
    state = _linkedin_campaign()
    state, decision = _tracked(state, LINKEDIN_ACTIVE_100)
    snapshot = _snapshot(state, status="paused", budget_daily=Decimal(100))

    assert compare(snapshot, decision, state=state) == [
        Mismatch(
            kind="status", decided_value={"status": "active"}, platform_value={"status": "paused"}
        )
    ]


def test_status_and_budget_mismatches_are_both_raised_status_first() -> None:
    """AAP 0.5.4.4 (C0): each kind is compared on its own; status comes first."""
    state = _google_campaign("DAILY")
    state, decision = _tracked(state, _own("active", daily=Decimal("10.000000")))
    snapshot = _snapshot(
        state, status="paused", budget_id=OWN_BUDGET, budget_amount_micros=11_000_000
    )

    assert [mismatch.kind for mismatch in compare(snapshot, decision, state=state)] == [
        "status",
        "budget",
    ]


def test_a_shared_amount_change_raises_no_member_mismatch() -> None:
    """AAP 0.5.2: a member compares status and ``budget_id`` only, never the shared amount."""
    state = _google_member()
    state, decision = _tracked(state, _member("active"))
    snapshot = _snapshot(state, budget_id=SHARED_BUDGET, budget_amount_micros=75_000_000)

    assert compare(snapshot, decision, state=state) == []


def test_a_member_moved_to_another_budget_is_an_assignment_mismatch() -> None:
    """AAP 0.5.4.4 (C0): a member moved to another shared budget raises a ``budget`` mismatch."""
    state = _google_member(
        budget_id=OTHER_SHARED_BUDGET,
        budget_external_id=OTHER_SHARED_BUDGET_EXTERNAL,
        decided_budget_external_id=SHARED_BUDGET_EXTERNAL,
    )
    state, decision = _tracked(state, _member("active", budget_id=SHARED_BUDGET))
    snapshot = _snapshot(state, budget_id=OTHER_SHARED_BUDGET, budget_amount_micros=75_000_000)

    assert compare(snapshot, decision, state=state) == [
        Mismatch(
            kind="budget",
            decided_value={
                "budget_id": SHARED_BUDGET,
                "budget_external_id": SHARED_BUDGET_EXTERNAL,
            },
            platform_value={
                "budget_id": OTHER_SHARED_BUDGET,
                "budget_external_id": OTHER_SHARED_BUDGET_EXTERNAL,
            },
        )
    ]


def test_a_member_status_change_is_a_status_mismatch_only() -> None:
    """AAP 0.5.4.4 (C0): a member's status is compared like any campaign's."""
    state = _google_member()
    state, decision = _tracked(state, _member("active"))
    snapshot = _snapshot(state, status="paused", budget_id=SHARED_BUDGET)

    assert [mismatch.kind for mismatch in compare(snapshot, decision, state=state)] == ["status"]


@pytest.mark.parametrize(
    ("micros", "kinds"),
    [
        pytest.param(50_000_000, [], id="same-amount"),
        pytest.param(50_000_001, ["budget"], id="one-micro-more"),
    ],
)
def test_a_budget_subject_compares_its_amount_and_never_a_status(
    micros: int, kinds: list[DiscrepancyKind]
) -> None:
    """AAP 0.5.2: a budget subject has no status; only its amount is compared, in micros.

    The snapshot carries a status anyway, which a campaign comparison would read; a budget
    subject never does.
    """
    state = _google_shared_budget("DAILY")
    state, decision = _tracked(state, _budget(daily=Decimal("50.000000")))
    snapshot = _snapshot(state, status="paused", budget_amount_micros=micros)

    assert [mismatch.kind for mismatch in compare(snapshot, decision, state=state)] == kinds


def test_compare_refuses_a_snapshot_of_another_subject() -> None:
    """AAP 0.5.4.4 (C0): each subject's newest snapshot is compared with its own decision."""
    state = _linkedin_campaign()
    other = _state(
        ref=SubjectRef(type="campaign", id=CAMPAIGN + 1), source="linkedin", shape="own_budget"
    )

    with pytest.raises(ValueError, match="describes campaign 12, not campaign 11"):
        compare(_snapshot(other), _decision(state, LINKEDIN_ACTIVE_100), state=state)


@pytest.mark.parametrize(
    ("state", "snapshot", "expected"),
    [
        pytest.param(
            _linkedin_campaign(daily=True, total=True),
            _snapshot(
                _linkedin_campaign(daily=True, total=True),
                status="paused",
                budget_daily=Decimal("100.00"),
                budget_total=Decimal("5000.00"),
            ),
            ExpectedState(
                shape="own_budget",
                status="paused",
                daily_budget=Decimal("100.00"),
                total_budget=Decimal("5000.00"),
            ),
            id="linkedin-own-budget",
        ),
        pytest.param(
            _google_campaign("DAILY"),
            _snapshot(
                _google_campaign("DAILY"), budget_id=OWN_BUDGET, budget_amount_micros=12_345_678
            ),
            ExpectedState(shape="own_budget", status="active", daily_budget=Decimal("12.345678")),
            id="google-own-budget-daily",
        ),
        pytest.param(
            _google_campaign("CUSTOM_PERIOD"),
            _snapshot(
                _google_campaign("CUSTOM_PERIOD"),
                status="ended",
                budget_id=OWN_BUDGET,
                budget_amount_micros=500_000_000,
            ),
            ExpectedState(shape="own_budget", status="ended", total_budget=Decimal(500)),
            id="google-own-budget-custom-period",
        ),
        pytest.param(
            _google_member(),
            _snapshot(_google_member(), budget_id=SHARED_BUDGET, budget_amount_micros=75_000_000),
            ExpectedState(shape="shared_member", status="active", budget_id=SHARED_BUDGET),
            id="google-shared-member-without-amount",
        ),
        pytest.param(
            _google_shared_budget("DAILY"),
            _snapshot(_google_shared_budget("DAILY"), status=None, budget_amount_micros=50_000_000),
            ExpectedState(shape="budget", daily_budget=Decimal(50)),
            id="google-shared-budget-daily",
        ),
        pytest.param(
            _google_shared_budget("CUSTOM_PERIOD"),
            _snapshot(
                _google_shared_budget("CUSTOM_PERIOD"),
                status=None,
                budget_amount_micros=1_000_000_000,
            ),
            ExpectedState(shape="budget", total_budget=Decimal(1000)),
            id="google-shared-budget-custom-period",
        ),
    ],
)
def test_expected_state_from_snapshot_materialises_each_shape(
    state: SubjectState, snapshot: Any, expected: ExpectedState
) -> None:
    """AAP 0.5.2 (baselines, ``adopt_platform``): only the fields the shape owns are filled.

    Recording that state as the decision then compares equal to the snapshot it came from,
    which is why a baseline raises no discrepancy on day one.
    """
    materialised = expected_state_from_snapshot(
        snapshot, shape=state.shape, budget_period=state.budget_period
    )

    assert materialised == expected
    tracked, baseline = _tracked(state, materialised)
    assert compare(snapshot, baseline, state=tracked) == []


# ---------------------------------------------------------------------------------------
# Phase 5: the resolution matrix (AAP 0.5.4.4 "C0 resolutions by kind and subject type")
# ---------------------------------------------------------------------------------------

_PLATFORM_OR_DECISION: Final[frozenset[Resolution]] = frozenset(
    {"platform_correct", "decision_correct"}
)
_ADOPT_OR_INTEND: Final[frozenset[Resolution]] = frozenset({"adopt_platform", "record_intended"})
_REMOVAL: Final[frozenset[Resolution]] = frozenset({"acknowledge_removal", "decision_correct"})

EXPECTED_RESOLUTIONS: Final[dict[tuple[DiscrepancyKind, DecisionShape], frozenset[Resolution]]] = {
    ("status", "own_budget"): _PLATFORM_OR_DECISION,
    ("status", "shared_member"): _PLATFORM_OR_DECISION,
    ("budget", "own_budget"): _PLATFORM_OR_DECISION,
    ("budget", "shared_member"): _PLATFORM_OR_DECISION,
    ("budget", "budget"): _PLATFORM_OR_DECISION,
    ("untracked", "own_budget"): _ADOPT_OR_INTEND,
    ("untracked", "shared_member"): _ADOPT_OR_INTEND,
    ("untracked", "budget"): _ADOPT_OR_INTEND,
    ("missing", "own_budget"): _REMOVAL,
    ("missing", "shared_member"): _REMOVAL,
    ("missing", "budget"): _REMOVAL,
}
"""AAP 0.5.4.4, spelled out pair by pair."""


@pytest.mark.parametrize(
    ("pair", "resolutions"),
    [
        pytest.param(pair, resolutions, id=f"{pair[0]}-{pair[1]}")
        for pair, resolutions in EXPECTED_RESOLUTIONS.items()
    ],
)
def test_each_kind_and_shape_offers_exactly_its_resolutions(
    pair: tuple[DiscrepancyKind, DecisionShape], resolutions: frozenset[Resolution]
) -> None:
    """AAP 0.5.4.4: only the resolutions valid for the (kind, subject type) pair are offered."""
    assert RESOLUTIONS[pair] == resolutions


def test_the_matrix_holds_exactly_the_eleven_pairs() -> None:
    """AAP 0.5.4.4: every kind for every shape, except a status on a statusless budget subject."""
    assert dict(RESOLUTIONS) == EXPECTED_RESOLUTIONS
    assert len(RESOLUTIONS) == 11
    assert ("status", "budget") not in RESOLUTIONS
    every_pair = {(kind, shape) for kind in DISCREPANCY_KINDS for shape in DECISION_SHAPES}
    assert set(RESOLUTIONS) == every_pair - {("status", "budget")}


def test_the_matrix_offers_only_known_resolutions_and_is_read_only() -> None:
    """AAP 0.5.4.4: resolutions are the five API keys, and the matrix cannot be altered."""
    offered = frozenset().union(*RESOLUTIONS.values())
    assert offered == frozenset(DISCREPANCY_RESOLUTIONS)

    writable: Any = RESOLUTIONS
    with pytest.raises(TypeError):
        writable[("status", "budget")] = _PLATFORM_OR_DECISION


@pytest.mark.parametrize(
    ("alias", "values"),
    [
        pytest.param(SubjectType, SUBJECT_TYPES, id="subject-type"),
        pytest.param(DecisionShape, DECISION_SHAPES, id="decision-shape"),
        pytest.param(DiscrepancyKind, DISCREPANCY_KINDS, id="discrepancy-kind"),
        pytest.param(Resolution, DISCREPANCY_RESOLUTIONS, id="resolution"),
        pytest.param(Origin, DECISION_ORIGINS, id="origin"),
    ],
)
def test_the_vocabulary_matches_the_model_check_values(
    alias: object, values: tuple[str, ...]
) -> None:
    """AAP 0.5.2: the decision vocabulary is the one the ``decision`` CHECK constraints admit."""
    assert set(get_args(alias)) == set(values)
