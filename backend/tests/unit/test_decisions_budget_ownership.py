"""Unit tests for an own-budget campaign's budget fields (AAP 0.5.2, 0.5.4.4).

* ``ads_engine.decisions.expected_state_from_snapshot`` refuses, with 409
  ``platform_state_unknown``, a Google own-budget campaign snapshot that names its budget
  but not that budget's amount or period: the amount is unknown, never absent, so no
  baseline or adoption records it as missing and later raises a false ``budget``
  discrepancy. A Google campaign snapshot naming no budget, known Google amounts and
  LinkedIn amounts materialise exactly as observed.
* ``ads_engine.decisions.apply_patch`` refuses, with 422 ``budget_changed``, an
  ``own_budget`` patch whose result would not hold exactly the budget fields the campaign
  has now: a Google budget that moved between ``DAILY`` and ``CUSTOM_PERIOD``, or a
  LinkedIn campaign that gained or lost a daily or lifetime budget. The decided amount is
  never dropped silently; coherent patches, budget-less campaigns and the other shapes
  behave as before, and ``field_not_allowed`` still comes first. A Google budget no sync
  reports is unknown, not changed: a status patch keeps the decided amount, and an amount
  for it is still refused with ``field_not_allowed``.

Pure: no database and no network.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    BudgetPeriod,
    DecisionPatch,
    DecisionShape,
    DecisionSource,
    ExpectedState,
    PatchRejected,
    PlatformStateUnknown,
    SubjectRef,
    SubjectState,
    apply_patch,
    expected_state_from_snapshot,
)
from ads_engine.sync.snapshots import SnapshotRow

CAMPAIGN = 11
DATASET = 3
OWN_BUDGET = 501
SNAPSHOT = 77
CAPTURED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------------------
# Materialising an observed own budget
# ---------------------------------------------------------------------------------------


def _snapshot(row: SnapshotRow) -> EntitySnapshot:
    """Return the ``entity_snapshot`` an entity sync stores for a builder row."""
    return EntitySnapshot(id=SNAPSHOT, **row.to_row(DATASET, CAPTURED))


def _google_own(
    *, budget_id: int | None, period: str | None, micros: int | None, status: str = "active"
) -> EntitySnapshot:
    """Return a Google campaign snapshot on a budget that is not explicitly shared."""
    return _snapshot(
        SnapshotRow.campaign(
            source="google",
            entity_id=CAMPAIGN,
            status=status,
            raw_status="ENABLED" if status == "active" else "PAUSED",
            budget_id=budget_id,
            budget_period=period,
            budget_amount_micros=micros,
        )
    )


def _linkedin(daily: Decimal | None, total: Decimal | None) -> EntitySnapshot:
    """Return a LinkedIn campaign snapshot with its own daily and lifetime budgets."""
    return _snapshot(
        SnapshotRow.campaign(
            source="linkedin",
            entity_id=CAMPAIGN,
            status="paused",
            raw_status="PAUSED",
            daily_budget=daily,
            total_budget=total,
        )
    )


def _refused(snapshot: EntitySnapshot, period: BudgetPeriod | None) -> PlatformStateUnknown:
    """Materialise an own-budget snapshot that must be refused, and return the refusal."""
    with pytest.raises(PlatformStateUnknown) as refused:
        expected_state_from_snapshot(snapshot, shape="own_budget", budget_period=period)
    return refused.value


@pytest.mark.parametrize("period", ["DAILY", "CUSTOM_PERIOD"])
def test_a_google_own_budget_whose_amount_google_omitted_is_refused(period: BudgetPeriod) -> None:
    snapshot = _google_own(budget_id=OWN_BUDGET, period=period, micros=None)

    error = _refused(snapshot, period)

    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details == {"snapshot_id": SNAPSHOT}
    assert "did not report the amount or period of the campaign's budget" in error.message
    assert "until a sync observes it" in error.message


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(
            _google_own(budget_id=OWN_BUDGET, period="DAILY", micros=100_000_000),
            id="amount-reported-period-unknown-now",
        ),
        pytest.param(
            _google_own(budget_id=OWN_BUDGET, period=None, micros=None),
            id="neither-amount-nor-period-reported",
        ),
    ],
)
def test_a_google_own_budget_without_a_known_period_is_refused(snapshot: EntitySnapshot) -> None:
    error = _refused(snapshot, None)

    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details == {"snapshot_id": SNAPSHOT}


def test_a_google_campaign_naming_no_budget_materialises_no_amount() -> None:
    snapshot = _google_own(budget_id=None, period=None, micros=None, status="paused")

    expected = expected_state_from_snapshot(snapshot, shape="own_budget", budget_period=None)

    assert expected == ExpectedState(shape="own_budget", status="paused")


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        pytest.param(
            "DAILY",
            ExpectedState(shape="own_budget", status="active", daily_budget=Decimal("100.000001")),
            id="daily",
        ),
        pytest.param(
            "CUSTOM_PERIOD",
            ExpectedState(shape="own_budget", status="active", total_budget=Decimal("100.000001")),
            id="custom-period",
        ),
    ],
)
def test_a_known_google_own_budget_materialises_exactly(
    period: BudgetPeriod, expected: ExpectedState
) -> None:
    snapshot = _google_own(budget_id=OWN_BUDGET, period=period, micros=100_000_001)

    assert (
        expected_state_from_snapshot(snapshot, shape="own_budget", budget_period=period) == expected
    )


@pytest.mark.parametrize(
    ("daily", "total"),
    [
        pytest.param(Decimal("50.00"), None, id="daily"),
        pytest.param(None, Decimal("900.00"), id="lifetime"),
        pytest.param(Decimal("50.00"), Decimal("900.00"), id="daily-and-lifetime"),
        pytest.param(None, None, id="no-budget"),
    ],
)
def test_linkedin_amounts_materialise_as_observed(
    daily: Decimal | None, total: Decimal | None
) -> None:
    expected = expected_state_from_snapshot(
        _linkedin(daily, total), shape="own_budget", budget_period=None
    )

    assert expected == ExpectedState(
        shape="own_budget", status="paused", daily_budget=daily, total_budget=total
    )


def test_an_unreported_campaign_status_is_still_refused() -> None:
    snapshot = EntitySnapshot(
        id=SNAPSHOT,
        dataset_id=DATASET,
        source="google",
        entity_type="campaign",
        entity_id=CAMPAIGN,
        captured_at=CAPTURED,
        status=None,
        budget_id=OWN_BUDGET,
        budget_amount_micros=100_000_000,
        payload={"explicitly_shared": False, "budget_period": "DAILY"},
    )

    error = _refused(snapshot, "DAILY")

    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.message == "The campaign's platform status is not a normalised status."


# ---------------------------------------------------------------------------------------
# Patching an own budget whose kind or period changed
# ---------------------------------------------------------------------------------------

SHARED_BUDGET = 502
"""An explicitly shared budget."""

DAILY_100 = ExpectedState(shape="own_budget", status="active", daily_budget=Decimal(100))
TOTAL_900 = ExpectedState(shape="own_budget", status="active", total_budget=Decimal(900))
BOTH = ExpectedState(
    shape="own_budget", status="active", daily_budget=Decimal(100), total_budget=Decimal(900)
)
NO_BUDGET = ExpectedState(shape="own_budget", status="active")


def _subject(
    *,
    source: DecisionSource,
    has_daily: bool,
    has_total: bool,
    period: BudgetPeriod | None,
    shape: DecisionShape = "own_budget",
    budget_id: int | None = None,
) -> SubjectState:
    """Return a subject's current state with the budget fields it owns now."""
    ref = (
        SubjectRef(type="budget", id=SHARED_BUDGET)
        if shape == "budget"
        else SubjectRef(type="campaign", id=CAMPAIGN)
    )
    return SubjectState(
        ref=ref,
        dataset_id=DATASET,
        dataset_state="active",
        source=source,
        currency="USD",
        name="Campaign 11" if shape != "budget" else f"Shared budget {SHARED_BUDGET}",
        shape=shape,
        has_daily_budget=has_daily,
        has_total_budget=has_total,
        budget_period=period,
        budget_id=budget_id,
        budget_external_id=None if budget_id is None else str(budget_id),
        latest_snapshot=None,
        current_decision=None,
    )


def _google(period: BudgetPeriod | None) -> SubjectState:
    """Return a Google own-budget campaign whose budget now has ``period`` (``None``: none)."""
    return _subject(
        source="google",
        has_daily=period == "DAILY",
        has_total=period == "CUSTOM_PERIOD",
        period=period,
    )


def _linkedin_owning(*, daily: bool, total: bool) -> SubjectState:
    """Return a LinkedIn campaign that now has a daily and/or a lifetime budget."""
    return _subject(source="linkedin", has_daily=daily, has_total=total, period=None)


@pytest.mark.parametrize(
    ("state", "current", "patch", "decided", "owned"),
    [
        pytest.param(
            _google("CUSTOM_PERIOD"),
            DAILY_100,
            DecisionPatch(total_budget=Decimal(200)),
            ["daily_budget", "total_budget"],
            ["total_budget"],
            id="google-daily-now-custom-total-patch",
        ),
        pytest.param(
            _google("CUSTOM_PERIOD"),
            DAILY_100,
            DecisionPatch(status="paused"),
            ["daily_budget"],
            ["total_budget"],
            id="google-daily-now-custom-status-patch",
        ),
        pytest.param(
            _google("CUSTOM_PERIOD"),
            DAILY_100,
            DecisionPatch(status="active"),
            ["daily_budget"],
            ["total_budget"],
            id="google-daily-now-custom-unchanged-status-before-no-change",
        ),
        pytest.param(
            _google("DAILY"),
            TOTAL_900,
            DecisionPatch(daily_budget=Decimal(150)),
            ["daily_budget", "total_budget"],
            ["daily_budget"],
            id="google-custom-now-daily-daily-patch",
        ),
        pytest.param(
            _google("DAILY"),
            TOTAL_900,
            DecisionPatch(status="paused"),
            ["total_budget"],
            ["daily_budget"],
            id="google-custom-now-daily-status-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=True),
            DAILY_100,
            DecisionPatch(total_budget=Decimal(900)),
            ["daily_budget", "total_budget"],
            ["total_budget"],
            id="linkedin-daily-now-lifetime-total-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=True),
            DAILY_100,
            DecisionPatch(status="paused"),
            ["daily_budget"],
            ["total_budget"],
            id="linkedin-daily-now-lifetime-status-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=True, total=False),
            BOTH,
            DecisionPatch(status="paused"),
            ["daily_budget", "total_budget"],
            ["daily_budget"],
            id="linkedin-both-now-daily-status-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=True, total=False),
            BOTH,
            DecisionPatch(daily_budget=Decimal(120)),
            ["daily_budget", "total_budget"],
            ["daily_budget"],
            id="linkedin-both-now-daily-daily-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=True, total=True),
            DAILY_100,
            DecisionPatch(status="paused"),
            ["daily_budget"],
            ["daily_budget", "total_budget"],
            id="linkedin-daily-now-both-status-patch",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=False),
            DAILY_100,
            DecisionPatch(status="paused"),
            ["daily_budget"],
            [],
            id="linkedin-daily-now-none-status-patch",
        ),
    ],
)
def test_a_patch_over_a_changed_own_budget_is_refused(
    state: SubjectState,
    current: ExpectedState,
    patch: DecisionPatch,
    decided: list[str],
    owned: list[str],
) -> None:
    with pytest.raises(PatchRejected) as refused:
        apply_patch(state, current, patch)

    error = refused.value
    assert (error.status_code, error.code) == (422, "budget_changed")
    assert error.details == {
        "decided_fields": decided,
        "current_fields": owned,
        "current_budget_period": state.budget_period,
    }
    assert error.message.startswith("Campaign 11's budget changed after it was decided")
    for field in decided:
        if field not in owned:
            assert f"The decision still holds {field}" in error.message
    for field in owned:
        if field not in decided:
            assert f"The campaign now has {field}" in error.message


def test_the_refusal_names_the_new_google_period_and_the_resolution() -> None:
    with pytest.raises(PatchRejected) as refused:
        apply_patch(_google("CUSTOM_PERIOD"), DAILY_100, DecisionPatch(total_budget=Decimal(200)))

    assert refused.value.message == (
        "Campaign 11's budget changed after it was decided: it now has a CUSTOM_PERIOD budget. "
        "The decision still holds daily_budget, which the campaign no longer has. "
        "Resolve its budget discrepancy first."
    )


def test_the_refusal_offers_to_send_the_field_the_decision_lacks() -> None:
    with pytest.raises(PatchRejected) as refused:
        apply_patch(
            _linkedin_owning(daily=True, total=True), DAILY_100, DecisionPatch(status="paused")
        )

    assert refused.value.message == (
        "Campaign 11's budget changed after it was decided. "
        "The campaign now has total_budget, which the decision does not hold. "
        "Resolve its budget discrepancy first, or include total_budget in the patch."
    )


def test_a_status_patch_over_a_google_budget_no_longer_reported_keeps_the_amount() -> None:
    state = _google(None)
    assert (state.budget_period, state.has_daily_budget, state.has_total_budget) == (
        None,
        False,
        False,
    )

    result = apply_patch(state, DAILY_100, DecisionPatch(status="paused"))

    assert result == ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(100))


def test_an_amount_for_a_google_budget_no_longer_reported_is_still_refused() -> None:
    with pytest.raises(PatchRejected) as refused:
        apply_patch(_google(None), DAILY_100, DecisionPatch(daily_budget=Decimal(150)))

    error = refused.value
    assert (error.status_code, error.code) == (422, "field_not_allowed")
    assert error.details == {"field": "daily_budget"}
    assert error.message == "Campaign 11 has no budget; daily_budget cannot be decided"


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(DecisionPatch(total_budget=Decimal(900)), id="total"),
        pytest.param(DecisionPatch(status="paused", total_budget=Decimal(900)), id="status-total"),
    ],
)
def test_sending_the_field_a_linkedin_campaign_gained_is_accepted(patch: DecisionPatch) -> None:
    result = apply_patch(_linkedin_owning(daily=True, total=True), DAILY_100, patch)

    assert result == ExpectedState(
        shape="own_budget",
        status=patch.status or "active",
        daily_budget=Decimal(100),
        total_budget=Decimal(900),
    )


@pytest.mark.parametrize(
    ("state", "current", "patch", "result"),
    [
        pytest.param(
            _google(None),
            NO_BUDGET,
            DecisionPatch(status="paused"),
            ExpectedState(shape="own_budget", status="paused"),
            id="google-without-a-budget",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=False),
            NO_BUDGET,
            DecisionPatch(status="ended"),
            ExpectedState(shape="own_budget", status="ended"),
            id="linkedin-without-a-budget",
        ),
        pytest.param(
            _google("DAILY"),
            DAILY_100,
            DecisionPatch(status="paused"),
            ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(100)),
            id="google-daily-status-keeps-the-amount",
        ),
        pytest.param(
            _google("DAILY"),
            DAILY_100,
            DecisionPatch(daily_budget=Decimal("120.000001")),
            ExpectedState(shape="own_budget", status="active", daily_budget=Decimal("120.000001")),
            id="google-daily-amount-keeps-the-status",
        ),
        pytest.param(
            _google("CUSTOM_PERIOD"),
            TOTAL_900,
            DecisionPatch(total_budget=Decimal(950)),
            ExpectedState(shape="own_budget", status="active", total_budget=Decimal(950)),
            id="google-custom-period-amount",
        ),
        pytest.param(
            _linkedin_owning(daily=True, total=True),
            BOTH,
            DecisionPatch(daily_budget=Decimal("110.50")),
            ExpectedState(
                shape="own_budget",
                status="active",
                daily_budget=Decimal("110.50"),
                total_budget=Decimal(900),
            ),
            id="linkedin-both-daily-keeps-the-total",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=True),
            TOTAL_900,
            DecisionPatch(status="paused"),
            ExpectedState(shape="own_budget", status="paused", total_budget=Decimal(900)),
            id="linkedin-lifetime-status-keeps-the-amount",
        ),
    ],
)
def test_a_patch_over_a_coherent_own_budget_applies_as_before(
    state: SubjectState, current: ExpectedState, patch: DecisionPatch, result: ExpectedState
) -> None:
    assert apply_patch(state, current, patch) == result


@pytest.mark.parametrize(
    ("state", "current", "patch", "code"),
    [
        pytest.param(
            _google("CUSTOM_PERIOD"),
            DAILY_100,
            DecisionPatch(daily_budget=Decimal(150)),
            "field_not_allowed",
            id="google-field-the-period-does-not-own",
        ),
        pytest.param(
            _google(None),
            DAILY_100,
            DecisionPatch(daily_budget=Decimal(150)),
            "field_not_allowed",
            id="google-amount-without-a-budget",
        ),
        pytest.param(
            _linkedin_owning(daily=False, total=True),
            DAILY_100,
            DecisionPatch(daily_budget=Decimal(120)),
            "field_not_allowed",
            id="linkedin-daily-budget-it-lost",
        ),
        pytest.param(
            _google("CUSTOM_PERIOD"),
            DAILY_100,
            DecisionPatch(total_budget=Decimal("200.0000001")),
            "sub_minor_unit",
            id="google-amount-finer-than-a-micro",
        ),
        pytest.param(
            _google("DAILY"),
            DAILY_100,
            DecisionPatch(daily_budget=Decimal(100)),
            "no_change",
            id="coherent-no-change",
        ),
    ],
)
def test_the_earlier_and_later_rules_keep_their_order(
    state: SubjectState, current: ExpectedState, patch: DecisionPatch, code: str
) -> None:
    with pytest.raises(PatchRejected) as refused:
        apply_patch(state, current, patch)

    assert refused.value.code == code


def test_budget_and_shared_member_subjects_are_unchanged() -> None:
    budget = _subject(
        source="google", has_daily=True, has_total=False, period="DAILY", shape="budget"
    )
    member = _subject(
        source="google",
        has_daily=False,
        has_total=False,
        period="DAILY",
        shape="shared_member",
        budget_id=SHARED_BUDGET,
    )
    decided_member = ExpectedState(shape="shared_member", status="active", budget_id=SHARED_BUDGET)

    assert apply_patch(
        budget,
        ExpectedState(shape="budget", daily_budget=Decimal(50)),
        DecisionPatch(daily_budget=Decimal(60)),
    ) == ExpectedState(shape="budget", daily_budget=Decimal(60))
    assert apply_patch(member, decided_member, DecisionPatch(status="paused")) == ExpectedState(
        shape="shared_member", status="paused", budget_id=SHARED_BUDGET
    )
