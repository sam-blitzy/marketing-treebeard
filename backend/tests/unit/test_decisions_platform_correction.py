"""Unit tests for what a C0 resolution adopts from the platform (AAP 0.5.4.4, 0.5.2).

* ``ads_engine.decisions._platform_correction`` turns the compared platform state, already
  in the subject's current shape, and the current decision into the expected state
  ``platform_correct`` records: ``status`` adopts the platform status and ``budget`` the
  platform amount or member assignment, in an unchanged shape and across a move onto or
  off an explicitly shared budget.
* ``ads_engine.decisions._check_observed_shape`` refuses, with 409
  ``platform_state_unknown``, a compared snapshot whose own metadata records another budget
  shape, shared budget or budget period than the subject has now, so old values are never
  materialised in a new shape or period.

Pure: no database and no network.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.decisions import (
    BudgetPeriod,
    DecisionShape,
    DecisionSource,
    DiscrepancyKind,
    ExpectedState,
    PlatformStateUnknown,
    SubjectRef,
    SubjectState,
)
from ads_engine.decisions import _check_observed_shape as check_observed_shape
from ads_engine.decisions import _observed_shape as observed_shape
from ads_engine.decisions import _platform_correction as platform_correction
from ads_engine.sync.snapshots import SnapshotRow

SHARED_B = 502
"""The explicitly shared budget a member draws from."""

SHARED_C = 503
"""Another explicitly shared budget."""

OWN_A = 501
"""A Google budget that is not explicitly shared: the campaign's own."""

CAMPAIGN = 11
DATASET = 3
DISCREPANCY = 41
SNAPSHOT = 77
CAPTURED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

OWN_PAUSED_100 = ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(100))
OWN_ACTIVE_120 = ExpectedState(shape="own_budget", status="active", daily_budget=Decimal(120))
MEMBER_PAUSED_B = ExpectedState(shape="shared_member", status="paused", budget_id=SHARED_B)
MEMBER_ACTIVE_B = ExpectedState(shape="shared_member", status="active", budget_id=SHARED_B)
MEMBER_ACTIVE_C = ExpectedState(shape="shared_member", status="active", budget_id=SHARED_C)
BUDGET_DAILY_50 = ExpectedState(shape="budget", daily_budget=Decimal(50))
BUDGET_DAILY_60 = ExpectedState(shape="budget", daily_budget=Decimal(60))
BUDGET_TOTAL_900 = ExpectedState(shape="budget", total_budget=Decimal(900))


@pytest.mark.parametrize(
    ("kind", "platform", "current", "written"),
    [
        pytest.param(
            "status",
            OWN_ACTIVE_120,
            OWN_PAUSED_100,
            ExpectedState(shape="own_budget", status="active", daily_budget=Decimal(100)),
            id="status-own-keeps-the-decided-amount",
        ),
        pytest.param(
            "status",
            MEMBER_ACTIVE_C,
            MEMBER_PAUSED_B,
            MEMBER_ACTIVE_B,
            id="status-member-keeps-the-decided-assignment",
        ),
        pytest.param(
            "budget",
            OWN_ACTIVE_120,
            OWN_PAUSED_100,
            ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(120)),
            id="budget-own-keeps-the-decided-status",
        ),
        pytest.param(
            "budget",
            MEMBER_ACTIVE_C,
            MEMBER_PAUSED_B,
            ExpectedState(shape="shared_member", status="paused", budget_id=SHARED_C),
            id="budget-member-adopts-the-new-assignment",
        ),
        pytest.param(
            "budget",
            BUDGET_DAILY_60,
            BUDGET_DAILY_50,
            BUDGET_DAILY_60,
            id="budget-subject-adopts-the-amount",
        ),
        pytest.param(
            "budget",
            BUDGET_TOTAL_900,
            BUDGET_DAILY_50,
            BUDGET_TOTAL_900,
            id="budget-subject-adopts-the-amount-of-a-new-period",
        ),
    ],
)
def test_an_unchanged_shape_replaces_only_the_discrepant_field(
    kind: DiscrepancyKind,
    platform: ExpectedState,
    current: ExpectedState,
    written: ExpectedState,
) -> None:
    assert platform_correction(kind, platform, current) == written


@pytest.mark.parametrize(
    ("platform", "current", "written"),
    [
        pytest.param(
            MEMBER_ACTIVE_B,
            OWN_PAUSED_100,
            MEMBER_ACTIVE_B,
            id="own-budget-to-shared-member",
        ),
        pytest.param(
            OWN_ACTIVE_120,
            MEMBER_PAUSED_B,
            OWN_ACTIVE_120,
            id="shared-member-to-own-budget",
        ),
    ],
)
def test_a_status_correction_after_a_shape_change_adopts_the_platform_status(
    platform: ExpectedState, current: ExpectedState, written: ExpectedState
) -> None:
    corrected = platform_correction("status", platform, current)

    assert corrected == written
    # The status the discrepancy compared is the one recorded, so it cannot recur.
    assert corrected.status == platform.status
    assert corrected.status != current.status
    assert corrected.shape == platform.shape


@pytest.mark.parametrize(
    ("platform", "current", "written"),
    [
        pytest.param(
            MEMBER_ACTIVE_B,
            OWN_PAUSED_100,
            MEMBER_PAUSED_B,
            id="own-budget-to-shared-member",
        ),
        pytest.param(
            OWN_ACTIVE_120,
            MEMBER_PAUSED_B,
            ExpectedState(shape="own_budget", status="paused", daily_budget=Decimal(120)),
            id="shared-member-to-own-budget",
        ),
    ],
)
def test_a_budget_correction_after_a_shape_change_keeps_the_decided_status(
    platform: ExpectedState, current: ExpectedState, written: ExpectedState
) -> None:
    corrected = platform_correction("budget", platform, current)

    assert corrected == written
    assert corrected.status == current.status
    assert corrected.shape == platform.shape
    assert (corrected.budget_id, corrected.daily_budget, corrected.total_budget) == (
        platform.budget_id,
        platform.daily_budget,
        platform.total_budget,
    )


@pytest.mark.parametrize("kind", ["status", "budget"])
@pytest.mark.parametrize(
    ("platform", "current"),
    [
        pytest.param(OWN_ACTIVE_120, None, id="untracked-own-budget"),
        pytest.param(MEMBER_ACTIVE_B, None, id="untracked-member"),
        pytest.param(
            OWN_ACTIVE_120,
            ExpectedState(
                shape="own_budget", status="ended", daily_budget=Decimal(100), tombstone=True
            ),
            id="tombstoned-campaign-reappeared",
        ),
        pytest.param(
            MEMBER_ACTIVE_B,
            ExpectedState(
                shape="own_budget", status="ended", daily_budget=Decimal(100), tombstone=True
            ),
            id="tombstoned-campaign-reappeared-as-member",
        ),
    ],
)
def test_without_a_live_decision_the_platform_state_is_written(
    kind: DiscrepancyKind, platform: ExpectedState, current: ExpectedState | None
) -> None:
    assert platform_correction(kind, platform, current) == platform


def test_a_tombstoned_budget_subject_takes_the_platform_amount() -> None:
    tombstone = ExpectedState(shape="budget", daily_budget=Decimal(50), tombstone=True)

    assert platform_correction("budget", BUDGET_DAILY_60, tombstone) == BUDGET_DAILY_60


@pytest.mark.parametrize("kind", ["untracked", "missing"])
def test_kinds_without_platform_correct_are_refused(kind: DiscrepancyKind) -> None:
    with pytest.raises(ValueError, match="status and budget"):
        platform_correction(kind, OWN_ACTIVE_120, OWN_PAUSED_100)


# ---------------------------------------------------------------------------------------
# The compared snapshot's own observation (BE2-F11)
# ---------------------------------------------------------------------------------------


def _snapshot(row: SnapshotRow) -> EntitySnapshot:
    """Return the ``entity_snapshot`` an entity sync stores for a builder row."""
    return EntitySnapshot(id=SNAPSHOT, **row.to_row(DATASET, CAPTURED))


def _google_campaign(
    *, budget_id: int | None, period: str | None, shared: bool, status: str = "paused"
) -> EntitySnapshot:
    """Return a Google campaign snapshot drawing from ``budget_id``."""
    return _snapshot(
        SnapshotRow.campaign(
            source="google",
            entity_id=CAMPAIGN,
            status=status,
            raw_status="PAUSED" if status == "paused" else "ENABLED",
            budget_id=budget_id,
            budget_period=period,
            budget_amount_micros=None if budget_id is None else 100_000_000,
            explicitly_shared=shared,
        )
    )


def _budget_snapshot(period: str) -> EntitySnapshot:
    """Return a snapshot of the shared budget B with the given period."""
    return _snapshot(
        SnapshotRow.budget(
            entity_id=SHARED_B, period=period, amount_micros=50_000_000, explicitly_shared=True
        )
    )


def _linkedin_campaign() -> EntitySnapshot:
    """Return a LinkedIn campaign snapshot with its own daily budget."""
    return _snapshot(
        SnapshotRow.campaign(
            source="linkedin",
            entity_id=CAMPAIGN,
            status="active",
            raw_status="ACTIVE",
            daily_budget=Decimal(50),
        )
    )


def _raw(**columns: object) -> EntitySnapshot:
    """Return a campaign snapshot with hand-set columns, for metadata no builder writes."""
    values: dict[str, object] = {
        "id": SNAPSHOT,
        "dataset_id": DATASET,
        "source": "google",
        "entity_type": "campaign",
        "entity_id": CAMPAIGN,
        "captured_at": CAPTURED,
        "status": "paused",
        "budget_id": OWN_A,
        "budget_amount_micros": 100_000_000,
    }
    values.update(columns)
    return EntitySnapshot(**values)


def _state(
    shape: DecisionShape,
    *,
    period: BudgetPeriod | None = "DAILY",
    budget_id: int | None = None,
    source: DecisionSource = "google",
) -> SubjectState:
    """Return a subject's current state in ``shape``."""
    ref = (
        SubjectRef(type="budget", id=SHARED_B)
        if shape == "budget"
        else SubjectRef(type="campaign", id=CAMPAIGN)
    )
    owns_amount = shape != "shared_member"
    return SubjectState(
        ref=ref,
        dataset_id=DATASET,
        dataset_state="active",
        source=source,
        currency="USD",
        name="Campaign 11" if shape != "budget" else f"Shared budget {SHARED_B}",
        shape=shape,
        has_daily_budget=owns_amount and (period == "DAILY" or source == "linkedin"),
        has_total_budget=owns_amount and period == "CUSTOM_PERIOD",
        budget_period=period,
        budget_id=budget_id,
        budget_external_id=None if budget_id is None else str(budget_id),
        latest_snapshot=None,
        current_decision=None,
    )


@pytest.mark.parametrize(
    ("snapshot", "observed"),
    [
        pytest.param(
            _google_campaign(budget_id=OWN_A, period="DAILY", shared=False),
            ("own_budget", "DAILY", None),
            id="google-own-daily",
        ),
        pytest.param(
            _google_campaign(budget_id=OWN_A, period="CUSTOM_PERIOD", shared=False),
            ("own_budget", "CUSTOM_PERIOD", None),
            id="google-own-custom-period",
        ),
        pytest.param(
            _google_campaign(budget_id=None, period=None, shared=False),
            ("own_budget", None, None),
            id="google-without-budget",
        ),
        pytest.param(
            _google_campaign(budget_id=SHARED_B, period="DAILY", shared=True),
            ("shared_member", "DAILY", SHARED_B),
            id="google-member",
        ),
        pytest.param(_budget_snapshot("DAILY"), ("budget", "DAILY", None), id="budget-daily"),
        pytest.param(
            _budget_snapshot("CUSTOM_PERIOD"),
            ("budget", "CUSTOM_PERIOD", None),
            id="budget-custom-period",
        ),
        pytest.param(_linkedin_campaign(), ("own_budget", None, None), id="linkedin"),
    ],
)
def test_the_observed_shape_comes_from_the_snapshot_metadata(
    snapshot: EntitySnapshot, observed: tuple[str, str | None, int | None]
) -> None:
    observation = observed_shape(snapshot)

    assert observation is not None
    assert (observation.shape, observation.budget_period, observation.budget_id) == observed


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(_raw(payload={}), id="no-metadata"),
        pytest.param(_raw(payload=None), id="unflushed-payload"),
        pytest.param(
            _raw(payload={"explicitly_shared": False}), id="campaign-without-budget-period"
        ),
        pytest.param(_raw(payload={"budget_period": "DAILY"}), id="campaign-without-shared-flag"),
        pytest.param(
            _raw(payload={"explicitly_shared": "yes", "budget_period": "DAILY"}),
            id="non-boolean-shared-flag",
        ),
        pytest.param(
            _raw(payload={"explicitly_shared": False, "budget_period": "MONTHLY"}),
            id="unknown-campaign-period",
        ),
        pytest.param(
            _raw(budget_id=None, payload={"explicitly_shared": True, "budget_period": "DAILY"}),
            id="member-without-budget",
        ),
        pytest.param(
            _raw(source="linkedin", payload={"explicitly_shared": True, "budget_period": None}),
            id="linkedin-marked-shared",
        ),
        pytest.param(
            _raw(source="linkedin", payload={"explicitly_shared": False, "budget_period": "DAILY"}),
            id="linkedin-with-google-period",
        ),
        pytest.param(
            _raw(source="hubspot", payload={"explicitly_shared": False, "budget_period": None}),
            id="source-without-subjects",
        ),
        pytest.param(
            _raw(entity_type="budget", status=None, budget_id=None, payload={}),
            id="budget-without-period",
        ),
        pytest.param(
            _raw(entity_type="budget", status=None, budget_id=None, payload={"period": "WEEKLY"}),
            id="budget-with-unknown-period",
        ),
        pytest.param(
            _raw(
                entity_type="keyword", payload={"explicitly_shared": False, "budget_period": None}
            ),
            id="entity-type-without-subjects",
        ),
    ],
)
def test_missing_or_unknown_metadata_records_no_observation(snapshot: EntitySnapshot) -> None:
    assert observed_shape(snapshot) is None


@pytest.mark.parametrize(
    ("snapshot", "state"),
    [
        pytest.param(
            _google_campaign(budget_id=OWN_A, period="DAILY", shared=False),
            _state("own_budget", period="DAILY"),
            id="google-own-daily",
        ),
        pytest.param(
            _google_campaign(budget_id=None, period=None, shared=False),
            _state("own_budget", period=None),
            id="google-without-budget",
        ),
        pytest.param(
            _google_campaign(budget_id=SHARED_B, period="DAILY", shared=True),
            _state("shared_member", budget_id=SHARED_B),
            id="member-of-its-current-budget",
        ),
        pytest.param(
            _budget_snapshot("CUSTOM_PERIOD"),
            _state("budget", period="CUSTOM_PERIOD"),
            id="budget-custom-period",
        ),
        pytest.param(
            _linkedin_campaign(),
            _state("own_budget", period=None, source="linkedin"),
            id="linkedin",
        ),
    ],
)
def test_a_snapshot_observed_in_the_current_shape_and_period_is_adoptable(
    snapshot: EntitySnapshot, state: SubjectState
) -> None:
    check_observed_shape(snapshot, state, discrepancy_id=DISCREPANCY)


@pytest.mark.parametrize(
    ("snapshot", "state", "observed"),
    [
        pytest.param(
            _google_campaign(budget_id=OWN_A, period="DAILY", shared=False),
            _state("shared_member", budget_id=SHARED_B),
            ("own_budget", "DAILY", None),
            id="own-budget-a-now-a-member-of-b",
        ),
        pytest.param(
            _google_campaign(budget_id=SHARED_B, period="DAILY", shared=True),
            _state("own_budget", period="DAILY"),
            ("shared_member", "DAILY", SHARED_B),
            id="member-of-b-now-on-its-own-budget",
        ),
        pytest.param(
            _google_campaign(budget_id=SHARED_B, period="DAILY", shared=True),
            _state("shared_member", budget_id=SHARED_C),
            ("shared_member", "DAILY", SHARED_B),
            id="member-of-b-now-a-member-of-c",
        ),
        pytest.param(
            _google_campaign(budget_id=OWN_A, period="DAILY", shared=False),
            _state("own_budget", period="CUSTOM_PERIOD"),
            ("own_budget", "DAILY", None),
            id="daily-own-budget-now-custom-period",
        ),
        pytest.param(
            _google_campaign(budget_id=None, period=None, shared=False),
            _state("own_budget", period="DAILY"),
            ("own_budget", None, None),
            id="no-budget-now-daily",
        ),
        pytest.param(
            _budget_snapshot("DAILY"),
            _state("budget", period="CUSTOM_PERIOD"),
            ("budget", "DAILY", None),
            id="daily-shared-budget-now-custom-period",
        ),
    ],
)
def test_a_snapshot_observed_before_a_shape_or_period_change_is_refused(
    snapshot: EntitySnapshot, state: SubjectState, observed: tuple[str, str | None, int | None]
) -> None:
    with pytest.raises(PlatformStateUnknown) as refused:
        check_observed_shape(snapshot, state, discrepancy_id=DISCREPANCY)

    error = refused.value
    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details == {
        "discrepancy_id": DISCREPANCY,
        "snapshot_id": SNAPSHOT,
        "observed_shape": observed[0],
        "observed_budget_period": observed[1],
        "observed_budget_id": observed[2],
        "current_shape": state.shape,
        "current_budget_period": state.budget_period,
        "current_budget_id": state.budget_id,
    }
    assert "was observed before" in error.message
    assert "budget shape or period changed" in error.message
    assert "next entity sync" in error.message
    for key in ("own_budget", "shared_member", "DAILY", "CUSTOM_PERIOD"):
        assert key not in error.message
    # The UI shows the message as it is: the snapshot's database id stays in ``details``.
    assert str(SNAPSHOT) not in error.message


def test_the_shape_change_refusal_names_both_shapes_in_words() -> None:
    snapshot = _google_campaign(budget_id=OWN_A, period="DAILY", shared=False)

    with pytest.raises(PlatformStateUnknown) as refused:
        check_observed_shape(
            snapshot, _state("shared_member", budget_id=SHARED_B), discrepancy_id=DISCREPANCY
        )

    assert refused.value.message == (
        "The platform state this discrepancy compared was observed before Campaign 11's budget "
        "shape or period changed: it shows a campaign with its own daily budget, and the "
        f"subject is now a campaign on shared budget {SHARED_B}. The next entity sync that "
        "compares Campaign 11 again refreshes or closes this discrepancy; resolve it after that."
    )
    assert refused.value.details["snapshot_id"] == SNAPSHOT


def test_an_observed_budget_without_a_known_google_id_is_named_in_words() -> None:
    # The state knows only the Google id of the budget drawn from now, so the observed one
    # is named in words; its database id stays in ``details``.
    snapshot = _google_campaign(budget_id=SHARED_B, period="DAILY", shared=True)

    with pytest.raises(PlatformStateUnknown) as refused:
        check_observed_shape(
            snapshot, _state("shared_member", budget_id=SHARED_C), discrepancy_id=DISCREPANCY
        )

    error = refused.value
    assert error.message == (
        "The platform state this discrepancy compared was observed before Campaign 11's budget "
        "shape or period changed: it shows a campaign on a shared budget, and the subject is "
        f"now a campaign on shared budget {SHARED_C}. The next entity sync that compares "
        "Campaign 11 again refreshes or closes this discrepancy; resolve it after that."
    )
    assert "budget id" not in error.message
    assert str(SHARED_B) not in error.message
    assert str(SNAPSHOT) not in error.message
    assert error.details["observed_budget_id"] == SHARED_B


def test_a_snapshot_without_metadata_is_refused() -> None:
    state = _state("own_budget", period="DAILY")

    with pytest.raises(PlatformStateUnknown) as refused:
        check_observed_shape(_raw(payload={}), state, discrepancy_id=DISCREPANCY)

    error = refused.value
    assert (error.status_code, error.code) == (409, "platform_state_unknown")
    assert error.details["observed_shape"] is None
    assert error.details["current_shape"] == "own_budget"
    assert error.details["snapshot_id"] == SNAPSHOT
    assert error.message == (
        "The platform state this discrepancy compared does not record the budget shape it was "
        "observed in, so it cannot be adopted for Campaign 11, which is now a campaign with its "
        "own daily budget. The next entity sync that compares Campaign 11 again refreshes or "
        "closes this discrepancy; resolve it after that."
    )
    assert str(SNAPSHOT) not in error.message
