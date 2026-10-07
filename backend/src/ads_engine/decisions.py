"""Decision patches, decision shapes and discrepancy resolutions.

AAP 0.5.2 ("Decisions: shapes", "baseline", "patch model") and 0.5.4.4 (C0, "C0 resolutions
by kind and subject type"). The product is recommend-only: nothing here calls a platform.
The user applies a change in LinkedIn or Google Ads and records it here; the next entity
sync's snapshot confirms it or raises a discrepancy.

**Decision subjects and shapes.** A subject is a campaign of either source or a Google
explicitly shared budget. The subject's *current* state fixes its decision shape, and the
expected state holds only the fields that subject owns:

=================  ==========================================  ============================
Shape              Applies to                                  Expected state
=================  ==========================================  ============================
``own_budget``     A LinkedIn campaign; a Google campaign      Status plus the budget
                   whose budget is not explicitly shared       fields the campaign has
``shared_member``  A Google campaign on an explicitly shared   Status plus ``budget_id``;
                   budget                                      never an amount
``budget``         A Google explicitly shared budget           Exactly one amount, the one
                                                               its period allows; no status
=================  ==========================================  ============================

A change to a shared budget's amount is therefore recorded on the budget subject alone and
can never create a discrepancy on its members.

**Patch model.** :func:`record_decision` takes a :class:`DecisionPatch` carrying only the
fields being decided, applies it over the subject's last decided state with the pure
:func:`apply_patch`, validates the result per shape and writes one complete, append-only
``decision`` row. A budget-only patch keeps the decided status; a status-only patch keeps
the decided budget. Every rejection is a :class:`DecisionError` whose ``status_code`` and
``code`` ``api/app.py`` maps to ``{"error": {"code", "message"}}``.

**Resolutions.** :data:`RESOLUTIONS` is the (kind, shape) matrix of AAP 0.5.4.4, and
:func:`resolve_discrepancy` applies one resolution. :func:`compare`, :func:`write_baseline`,
:func:`raise_discrepancy` and :func:`auto_resolve` are the detection-side helpers
``sync/snapshots.py`` calls after each entity sync; it decides ``untracked`` and
``missing`` itself, from baseline timing and complete enumeration (AAP 0.3.3 safe absence).

**Invariants every function keeps:**

* Decisions and discrepancies are dataset-scoped. A user decision or resolution on a subject
  whose dataset is not ``active`` is refused with :class:`SubjectRetired` (409
  ``subject_retired``), so a simulated decision never becomes a live campaign's expected
  state (AAP 0.3.2, 0.3.3).
* Money is ``Decimal`` throughout, never ``float``. Google amounts are compared in micros,
  their native integer precision; LinkedIn amounts are validated to the currency's minor
  unit and compared exactly, so rounding can neither cause nor hide a mismatch.
* Nothing here commits. Callers (API routes, sync jobs) own the transaction; functions
  flush, and every write that changes what the latest run shows enqueues one
  ``analysis_request`` in the same transaction (AAP 0.3.2).
* Logs carry ids, kinds, shapes and origins only: never ``decided_by`` (an email), notes or
  amounts.

Example::

    async with get_sessionmaker()() as session:  # ads_engine.db.session
        decision = await record_decision(
            session,
            SubjectRef(type="campaign", id=42),
            DecisionPatch(status="paused"),
            origin="user",
            decided_by=user.email,
        )
        await session.commit()
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, ClassVar, Final, Literal, TypeIs, get_args

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.db.models.accounts import (
    BUDGET_PERIODS,
    CAMPAIGN_STATUSES,
    AdAccount,
    Budget,
    Campaign,
)
from ads_engine.db.models.decisions import (
    DECISION_ORIGINS,
    DECISION_SHAPES,
    DISCREPANCY_KINDS,
    DISCREPANCY_RESOLUTIONS,
    DISCREPANCY_STATES,
    SUBJECT_TYPES,
    Decision,
    Discrepancy,
)
from ads_engine.db.models.ops import SourceDataset
from ads_engine.db.models.snapshots import EntitySnapshot
from ads_engine.db.repositories import enqueue_analysis_request
from ads_engine.logging import get_logger

__all__ = [
    "MICROS_PER_UNIT",
    "RESOLUTIONS",
    "BudgetPeriod",
    "CampaignStatus",
    "DecisionError",
    "DecisionPatch",
    "DecisionShape",
    "DecisionSource",
    "DiscrepancyClosed",
    "DiscrepancyKind",
    "DiscrepancyState",
    "ExpectedState",
    "InvalidResolution",
    "Mismatch",
    "Origin",
    "PatchRejected",
    "PlatformStateUnknown",
    "Resolution",
    "SubjectNotFound",
    "SubjectRef",
    "SubjectRetired",
    "SubjectState",
    "SubjectType",
    "apply_patch",
    "auto_resolve",
    "compare",
    "decision_shape",
    "describe_decision",
    "describe_expected",
    "describe_snapshot",
    "expected_state_from_snapshot",
    "expected_state_of",
    "from_micros",
    "is_valid_amount",
    "load_subject_state",
    "minor_unit_exponent",
    "parse_intended_state",
    "raise_discrepancy",
    "record_decision",
    "resolve_discrepancy",
    "round_to_minor_unit",
    "to_micros",
    "write_baseline",
]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------------------
# Types. The Literal aliases restate the CHECK-constraint tuples of the models because a
# type checker needs the values spelled out; _check_literal() below proves at import time
# that each alias and its tuple hold exactly the same values.
# ---------------------------------------------------------------------------------------

SubjectType = Literal["campaign", "budget"]
"""What a decision is about (``SUBJECT_TYPES``)."""

DecisionShape = Literal["own_budget", "shared_member", "budget"]
"""The decision shape fixed by the subject's current state (``DECISION_SHAPES``)."""

DiscrepancyKind = Literal["status", "budget", "untracked", "missing"]
"""The C0 discrepancy kinds (``DISCREPANCY_KINDS``)."""

DiscrepancyState = Literal["open", "awaiting_platform", "resolved"]
"""The discrepancy lifecycle (``DISCREPANCY_STATES``)."""

Resolution = Literal[
    "platform_correct",
    "decision_correct",
    "adopt_platform",
    "record_intended",
    "acknowledge_removal",
]
"""The resolution API keys (``DISCREPANCY_RESOLUTIONS``)."""

Origin = Literal["baseline", "user", "recommendation", "resolution"]
"""Who wrote a decision row (``DECISION_ORIGINS``)."""

CampaignStatus = Literal["active", "paused", "ended"]
"""The normalised campaign status (``CAMPAIGN_STATUSES``)."""

BudgetPeriod = Literal["DAILY", "CUSTOM_PERIOD"]
"""Google ``campaign_budget.period`` (``BUDGET_PERIODS``)."""

DecisionSource = Literal["google", "linkedin"]
"""The sources that have decision subjects; HubSpot has none."""


def _check_literal(alias: object, values: tuple[str, ...], name: str) -> None:
    """Fail the import when a Literal alias and its model tuple disagree.

    Args:
        alias: The ``Literal`` alias.
        values: The model module's tuple of allowed values.
        name: The alias name, for the error message.

    Raises:
        RuntimeError: If the alias and the tuple hold different values.
    """
    if set(get_args(alias)) != set(values):
        raise RuntimeError(
            f"ads_engine.decisions.{name} {sorted(get_args(alias))} disagrees with the "
            f"model CHECK values {sorted(values)}"
        )


_check_literal(SubjectType, SUBJECT_TYPES, "SubjectType")
_check_literal(DecisionShape, DECISION_SHAPES, "DecisionShape")
_check_literal(DiscrepancyKind, DISCREPANCY_KINDS, "DiscrepancyKind")
_check_literal(DiscrepancyState, DISCREPANCY_STATES, "DiscrepancyState")
_check_literal(Resolution, DISCREPANCY_RESOLUTIONS, "Resolution")
_check_literal(Origin, DECISION_ORIGINS, "Origin")
_check_literal(CampaignStatus, CAMPAIGN_STATUSES, "CampaignStatus")
_check_literal(BudgetPeriod, BUDGET_PERIODS, "BudgetPeriod")

MICROS_PER_UNIT: Final[int] = 1_000_000
"""Google reports money in micros: one millionth of the currency unit."""

_MICRO_PLACES: Final[int] = 6
"""Decimal places of one micro, and the scale of every ``numeric(18, 6)`` money column."""

_MAX_AMOUNT: Final[Decimal] = Decimal(10) ** 12
"""Exclusive upper bound of ``numeric(18, 6)``: twelve integer digits."""

_EXACT: Final[Context] = Context(prec=60)
"""A context wide enough that scaling and quantizing any storable amount is exact."""

_ZERO_EXPONENT_CURRENCIES: Final[frozenset[str]] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "UYI",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
"""ISO 4217 currencies with no minor unit."""

_THREE_EXPONENT_CURRENCIES: Final[frozenset[str]] = frozenset(
    {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}
)
"""ISO 4217 currencies whose minor unit is a thousandth."""

_PATCH_FIELDS: Final[frozenset[str]] = frozenset({"status", "daily_budget", "total_budget"})
"""The fields a decision patch or an intended state may carry."""

_AMOUNT_FIELDS: Final[tuple[str, str]] = ("daily_budget", "total_budget")
"""The two amount fields, in display order."""

_AUTO_RESOLVABLE_KINDS: Final[frozenset[str]] = frozenset({"status", "budget", "untracked"})
"""Kinds that close as soon as a later snapshot matches the current decision (AAP 0.5.4.4)."""


def _is_subject_type(value: object) -> TypeIs[SubjectType]:
    """Return whether ``value`` is a decision subject type."""
    return isinstance(value, str) and value in SUBJECT_TYPES


def _is_shape(value: object) -> TypeIs[DecisionShape]:
    """Return whether ``value`` is a decision shape."""
    return isinstance(value, str) and value in DECISION_SHAPES


def _is_kind(value: object) -> TypeIs[DiscrepancyKind]:
    """Return whether ``value`` is a discrepancy kind."""
    return isinstance(value, str) and value in DISCREPANCY_KINDS


def _is_resolution(value: object) -> TypeIs[Resolution]:
    """Return whether ``value`` is a resolution API key."""
    return isinstance(value, str) and value in DISCREPANCY_RESOLUTIONS


def _is_origin(value: object) -> TypeIs[Origin]:
    """Return whether ``value`` is a decision origin."""
    return isinstance(value, str) and value in DECISION_ORIGINS


def _is_campaign_status(value: object) -> TypeIs[CampaignStatus]:
    """Return whether ``value`` is a normalised campaign status."""
    return isinstance(value, str) and value in CAMPAIGN_STATUSES


def _is_period(value: object) -> TypeIs[BudgetPeriod]:
    """Return whether ``value`` is a Google budget period."""
    return isinstance(value, str) and value in BUDGET_PERIODS


def _is_decision_source(value: object) -> TypeIs[DecisionSource]:
    """Return whether ``value`` is a source that has decision subjects."""
    return value in ("google", "linkedin")


# ---------------------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------------------


class DecisionError(Exception):
    """Base of every domain rejection; ``api/app.py`` maps it to the error envelope.

    The API answers ``status_code`` with ``{"error": {"code": code, "message": message}}``
    (AAP 0.5.3). Each subclass fixes its status code and a default code and message, so
    ``PatchRejected()`` is valid on its own and ``PatchRejected("empty_patch", "...")``
    names the precise rule.

    Attributes:
        status_code: The HTTP status the API answers with.
        code: The machine-readable error code.
        message: The human-readable message; it never contains an email or a secret.
        details: Extra machine-readable context, for example the owning shared budget.
    """

    status_code: ClassVar[int] = 422
    default_code: ClassVar[str] = "decision_error"
    default_message: ClassVar[str] = "The decision request was rejected."

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code: str = code or self.default_code
        self.message: str = message or self.default_message
        self.details: dict[str, object] = dict(details or {})
        super().__init__(self.message)


# The exception names below are the decisions contract that the API routes, sync and the
# tests import, so pep8-naming's N818 rule (an "Error" suffix) gives way to them.
class PatchRejected(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """A decision patch breaks a rule of AAP 0.5.2 (422).

    Codes: ``empty_patch``, ``no_change``, ``invalid_amount``, ``sub_minor_unit``,
    ``field_not_allowed``, ``shared_budget_amount``, ``untracked_subject``,
    ``subject_removed``, ``shape_changed``, ``unknown_field`` and ``invalid_value``.
    """

    status_code: ClassVar[int] = 422
    default_code: ClassVar[str] = "patch_rejected"
    default_message: ClassVar[str] = "The decision patch was rejected."


class InvalidResolution(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """A resolution does not fit its discrepancy (422).

    Codes: ``resolution_not_allowed`` (not valid for the (kind, shape) pair) and
    ``intended_state_invalid`` (a ``record_intended`` state that is incomplete, carries
    extra fields, is mis-typed or is finer than the minor unit).
    """

    status_code: ClassVar[int] = 422
    default_code: ClassVar[str] = "resolution_not_allowed"
    default_message: ClassVar[str] = "The resolution is not valid for this discrepancy."


class SubjectRetired(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """The subject belongs to a dataset that is no longer active (409 ``subject_retired``).

    AAP 0.3.2: a decision, resolution or feedback write on a non-active dataset is refused,
    so a decision about replaced data never becomes the expected state of current data.
    """

    status_code: ClassVar[int] = 409
    default_code: ClassVar[str] = "subject_retired"
    default_message: ClassVar[str] = "Computed from data that has since been replaced."


class SubjectNotFound(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """The campaign, shared budget or discrepancy does not exist (404 ``not_found``)."""

    status_code: ClassVar[int] = 404
    default_code: ClassVar[str] = "not_found"
    default_message: ClassVar[str] = "The decision subject does not exist."


class DiscrepancyClosed(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """The discrepancy is already resolved (409 ``discrepancy_resolved``)."""

    status_code: ClassVar[int] = 409
    default_code: ClassVar[str] = "discrepancy_resolved"
    default_message: ClassVar[str] = "The discrepancy is already resolved."


class PlatformStateUnknown(DecisionError):  # noqa: N818 - name fixed by the decisions API contract
    """The platform state cannot be materialised as an expected state (409).

    Raised when a snapshot lacks what its shape needs: a campaign status outside the
    normalised set, a shared member without its budget, or a shared budget whose amount
    Google omitted. Such an amount is unknown, never zero, so no ``budget`` decision can be
    written for it until a later sync observes it (``Budget`` model contract). Code
    ``platform_state_unknown``.
    """

    status_code: ClassVar[int] = 409
    default_code: ClassVar[str] = "platform_state_unknown"
    default_message: ClassVar[str] = "The platform state of the subject is not known yet."


# ---------------------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------------------


def _as_decimal(value: object) -> Decimal | None:
    """Convert an exact amount to ``Decimal``, or return ``None`` when it is not exact.

    ``Decimal`` passes through, ``int`` converts exactly and ``str`` is parsed. ``float``
    is refused, because money is never binary floating point, and so are ``bool``, NaN and
    infinities.

    Args:
        value: The submitted amount.

    Returns:
        The finite ``Decimal``, or ``None`` when ``value`` is not an exact finite amount.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = Decimal(value)
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return amount if amount.is_finite() else None


def _decimal_text(amount: Decimal | None) -> str | None:
    """Render an amount as plain positional text (never exponent notation), or ``None``."""
    return None if amount is None else format(amount, "f")


@dataclass(frozen=True, slots=True)
class SubjectRef:
    """A decision subject: a campaign or a Google explicitly shared budget.

    Attributes:
        type: ``campaign`` or ``budget``.
        id: The internal ``id`` of the ``campaign`` or ``budget`` row.
    """

    type: SubjectType
    id: int

    def __post_init__(self) -> None:
        """Reject an unknown subject type or a non-integer id.

        Raises:
            ValueError: If ``type`` is not a subject type or ``id`` is not an ``int``.
        """
        if not _is_subject_type(self.type):
            raise ValueError(f"subject type must be one of {SUBJECT_TYPES}, not {self.type!r}")
        if not isinstance(self.id, int) or isinstance(self.id, bool):
            raise ValueError(f"subject id must be an integer, not {self.id!r}")


@dataclass(frozen=True, slots=True)
class DecisionPatch:
    """The fields a decision submission carries; every other field keeps its decided value.

    AAP 0.5.2 ("Decisions: patch model"). ``None`` means "not part of this patch": a
    decided field can never be cleared, so an explicit ``null`` is refused by
    :meth:`from_mapping` and :meth:`fields_set` tells sent fields from absent ones.
    Amounts are converted to ``Decimal`` on construction (``int`` and decimal strings are
    exact); ``float`` is refused.

    Attributes:
        status: The decided campaign status.
        daily_budget: The decided daily budget, in the account's currency.
        total_budget: The decided lifetime or custom-period budget.

    Raises:
        PatchRejected: ``invalid_value`` for a status outside ``active``, ``paused`` and
            ``ended``, or an amount that is not an exact finite number.
    """

    status: CampaignStatus | None = None
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate the status and normalise both amounts to exact ``Decimal`` values."""
        if self.status is not None and not _is_campaign_status(self.status):
            raise PatchRejected(
                "invalid_value",
                f"status must be one of {', '.join(CAMPAIGN_STATUSES)}",
                details={"field": "status"},
            )
        for name in _AMOUNT_FIELDS:
            raw: object = getattr(self, name)
            if raw is None:
                continue
            amount = _as_decimal(raw)
            if amount is None:
                raise PatchRejected(
                    "invalid_value",
                    f"{name} must be an exact decimal amount",
                    details={"field": name},
                )
            object.__setattr__(self, name, amount)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> DecisionPatch:
        """Build a patch from submitted JSON-like fields.

        Args:
            data: Field name to value; only ``status``, ``daily_budget`` and
                ``total_budget`` are accepted.

        Returns:
            The patch, carrying exactly the fields present in ``data``.

        Raises:
            PatchRejected: ``unknown_field`` for any other key; ``invalid_value`` for a
                ``null`` value, an unknown status or an inexact amount.
        """
        unknown = sorted(set(data) - _PATCH_FIELDS)
        if unknown:
            raise PatchRejected(
                "unknown_field",
                f"unknown patch fields: {', '.join(unknown)}",
                details={"fields": unknown},
            )
        nulls = sorted(name for name, value in data.items() if value is None)
        if nulls:
            raise PatchRejected(
                "invalid_value",
                f"decided fields cannot be null: {', '.join(nulls)}",
                details={"fields": nulls},
            )
        status = data.get("status")
        if status is not None and not _is_campaign_status(status):
            raise PatchRejected(
                "invalid_value",
                f"status must be one of {', '.join(CAMPAIGN_STATUSES)}",
                details={"field": "status"},
            )
        amounts: dict[str, Decimal | None] = {}
        for name in _AMOUNT_FIELDS:
            raw = data.get(name)
            amount = None if raw is None else _as_decimal(raw)
            if raw is not None and amount is None:
                raise PatchRejected(
                    "invalid_value",
                    f"{name} must be an exact decimal amount",
                    details={"field": name},
                )
            amounts[name] = amount
        return cls(
            status=status,
            daily_budget=amounts["daily_budget"],
            total_budget=amounts["total_budget"],
        )

    def fields_set(self) -> frozenset[str]:
        """Return the names of the fields this patch carries."""
        return frozenset(
            name
            for name, value in (
                ("status", self.status),
                ("daily_budget", self.daily_budget),
                ("total_budget", self.total_budget),
            )
            if value is not None
        )

    def as_json(self) -> dict[str, str]:
        """Return the submitted fields as the ``decision.patch`` JSON (amounts as strings)."""
        value: dict[str, str] = {}
        if self.status is not None:
            value["status"] = self.status
        for name, amount in (
            ("daily_budget", self.daily_budget),
            ("total_budget", self.total_budget),
        ):
            text = _decimal_text(amount)
            if text is not None:
                value[name] = text
        return value


@dataclass(frozen=True, slots=True)
class ExpectedState:
    """A subject's complete materialised expected state: what one ``decision`` row holds.

    Construction enforces the same column rules as the migration's
    ``ck_decision_shape_columns``, so no invalid row can reach the database:

    * ``own_budget``: a status, no ``budget_id``, any of the campaign's own amounts;
    * ``shared_member``: a status and the assigned shared ``budget_id``, no amount;
    * ``budget``: no status, no ``budget_id`` and exactly one amount.

    Attributes:
        shape: The decision shape.
        status: The decided status; ``None`` for a budget subject.
        daily_budget: The decided daily budget, if the shape and subject have one.
        total_budget: The decided lifetime or custom-period budget, if any.
        budget_id: The shared budget a ``shared_member`` is assigned to; else ``None``.
        tombstone: Whether the state records an acknowledged removal.

    Raises:
        ValueError: If the fields break the shape's column rules.
    """

    shape: DecisionShape
    status: CampaignStatus | None = None
    daily_budget: Decimal | None = None
    total_budget: Decimal | None = None
    budget_id: int | None = None
    tombstone: bool = False

    def __post_init__(self) -> None:
        """Check the shape's column rules (``ck_decision_shape_columns``)."""
        if self.shape == "own_budget":
            valid = self.status is not None and self.budget_id is None
        elif self.shape == "shared_member":
            valid = (
                self.status is not None
                and self.budget_id is not None
                and self.daily_budget is None
                and self.total_budget is None
            )
        elif self.shape == "budget":
            valid = (
                self.status is None
                and self.budget_id is None
                and (self.daily_budget is None) != (self.total_budget is None)
            )
        else:
            raise ValueError(f"unknown decision shape {self.shape!r}")
        if not valid:
            raise ValueError(f"fields do not fit the {self.shape} decision shape: {self!r}")
        if self.status is not None and not _is_campaign_status(self.status):
            raise ValueError(f"status must be one of {CAMPAIGN_STATUSES}, not {self.status!r}")


@dataclass(frozen=True, slots=True)
class SubjectState:
    """What validation needs to know about a decision subject *now*.

    Built by :func:`load_subject_state`; tests build it directly. ``shape`` must equal
    :func:`decision_shape` of the other fields, which construction checks.

    Attributes:
        ref: The subject.
        dataset_id: The subject's dataset.
        dataset_state: That dataset's state: ``building``, ``active`` or ``retired``.
        source: ``google`` or ``linkedin``.
        currency: ISO 4217 code of the subject's account.
        name: Display name: the campaign name, or ``Shared budget {external_id}``.
        shape: The decision shape the subject's current state fixes.
        has_daily_budget: Whether a decision may carry ``daily_budget``: a LinkedIn
            campaign with a ``dailyBudget``, or a Google own budget or shared budget whose
            period is ``DAILY``. ``False`` for a shared member, which owns no amount.
        has_total_budget: Whether a decision may carry ``total_budget``: a LinkedIn
            campaign with a lifetime budget, or a Google budget whose period is
            ``CUSTOM_PERIOD``. ``False`` for a shared member.
        budget_period: The Google budget period that applies (the campaign's own budget,
            the owning shared budget of a member, or the budget subject's); ``None`` for
            LinkedIn.
        budget_id: For a shared member only, the explicitly shared budget it draws from
            now; ``None`` for every other subject. Its presence is what makes a Google
            campaign a ``shared_member``.
        budget_external_id: The Google id of that shared budget; ``None`` otherwise.
        latest_snapshot: The newest ``entity_snapshot`` of the subject, if any.
        current_decision: The latest ``decision`` row of the subject, if any.
        decided_budget_external_id: The Google id of the shared budget the current
            decision assigns, when it assigns one (for display in discrepancy values).
    """

    ref: SubjectRef
    dataset_id: int
    dataset_state: str
    source: DecisionSource
    currency: str
    name: str
    shape: DecisionShape
    has_daily_budget: bool
    has_total_budget: bool
    budget_period: BudgetPeriod | None
    budget_id: int | None
    budget_external_id: str | None
    latest_snapshot: EntitySnapshot | None
    current_decision: Decision | None
    decided_budget_external_id: str | None = None

    def __post_init__(self) -> None:
        """Check that ``source`` has decision subjects and ``shape`` follows the state.

        Raises:
            ValueError: If the source is not Google or LinkedIn, a LinkedIn subject is a
                budget or carries a shared budget, or ``shape`` disagrees with
                :func:`decision_shape`.
        """
        if not _is_decision_source(self.source):
            raise ValueError(f"{self.source!r} has no decision subjects")
        if self.source == "linkedin" and (self.ref.type == "budget" or self.budget_id is not None):
            raise ValueError("LinkedIn budgets live on the campaign row; no shared budgets")
        derived = decision_shape(self)
        if self.shape != derived:
            raise ValueError(f"shape {self.shape!r} disagrees with the subject state ({derived!r})")

    @property
    def current_expected(self) -> ExpectedState | None:
        """The current decision as an :class:`ExpectedState`, or ``None`` when untracked."""
        if self.current_decision is None:
            return None
        return expected_state_of(self.current_decision)


@dataclass(frozen=True, slots=True)
class Mismatch:
    """One difference between a subject's snapshot and its current decision (rule C0).

    Produced by :func:`compare` and written by :func:`raise_discrepancy` into
    ``discrepancy.decided_value`` and ``platform_value`` (``jsonb``). Both values are JSON
    objects holding only the compared fields, for example ``{"status": "paused"}``,
    ``{"daily_budget": "100.000000", "total_budget": None, "currency": "USD",
    "amount_micros": 100000000}`` (``amount_micros`` on Google only), or
    ``{"budget_id": 7, "budget_external_id": "123"}`` for a budget assignment.

    Attributes:
        kind: ``status`` or ``budget``; :func:`compare` never produces ``untracked`` or
            ``missing``.
        decided_value: The decided side.
        platform_value: The platform side.
    """

    kind: DiscrepancyKind
    decided_value: dict[str, Any] | None
    platform_value: dict[str, Any] | None


# ---------------------------------------------------------------------------------------
# Money helpers (pure)
# ---------------------------------------------------------------------------------------


def minor_unit_exponent(currency: str) -> int:
    """Return the ISO 4217 minor-unit exponent of a currency.

    Example:
        >>> (minor_unit_exponent("USD"), minor_unit_exponent("JPY"), minor_unit_exponent("KWD"))
        (2, 0, 3)

    Args:
        currency: The ISO 4217 code; case and surrounding whitespace are ignored.

    Returns:
        ``0`` for currencies without a minor unit (for example ``JPY``), ``3`` for
        thousandth currencies (for example ``KWD``) and ``2`` for every other currency.
    """
    code = currency.strip().upper()
    if code in _ZERO_EXPONENT_CURRENCIES:
        return 0
    if code in _THREE_EXPONENT_CURRENCIES:
        return 3
    return 2


def _is_whole_at(amount: Decimal, places: int) -> bool:
    """Return whether ``amount`` has no digits finer than ``10 ** -places``.

    Reads the exact digits and exponent, so no context precision can round a fine digit
    away (``Decimal("12.3456780000000000000000000001")`` is not whole at six places).

    Args:
        amount: A finite amount.
        places: The number of decimal places allowed.

    Returns:
        Whether ``amount * 10 ** places`` is an integer.
    """
    _, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        return False
    if exponent >= -places:
        return True
    needed = -places - exponent
    return all(digit == 0 for digit in digits[-needed:])


def to_micros(amount: Decimal) -> int:
    """Convert an amount to Google micros exactly.

    Example:
        >>> to_micros(Decimal("12.345678"))
        12345678

    Args:
        amount: The amount in the currency unit.

    Returns:
        ``amount * 1_000_000`` as an ``int``.

    Raises:
        ValueError: If ``amount`` is not finite or is not a whole number of micros.
    """
    if not amount.is_finite() or not _is_whole_at(amount, _MICRO_PLACES):
        raise ValueError(f"{amount} is not a whole number of micros")
    sign, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"{amount} is not a whole number of micros")
    coefficient = int("".join(str(digit) for digit in digits)) if digits else 0
    shift = exponent + _MICRO_PLACES
    factor: int = 10 ** abs(shift)
    micros = coefficient * factor if shift >= 0 else coefficient // factor
    return -micros if sign else micros


def from_micros(micros: int) -> Decimal:
    """Convert Google micros to the currency unit exactly, at six decimal places.

    Example:
        >>> from_micros(100_000_000)
        Decimal('100.000000')

    Args:
        micros: The amount in micros.

    Returns:
        ``micros / 1_000_000`` as an exact ``Decimal``.
    """
    return Decimal(micros).scaleb(-_MICRO_PLACES, context=_EXACT)


def _amount_issue(amount: Decimal, *, source: str, currency: str) -> str | None:
    """Return why an amount cannot be decided, or ``None`` when it can.

    Args:
        amount: The submitted amount.
        source: ``google`` or ``linkedin``.
        currency: The subject account's currency.

    Returns:
        ``invalid_amount`` for a value that is not positive, not finite or too large for
        ``numeric(18, 6)``; ``sub_minor_unit`` for digits finer than the source allows;
        ``None`` for a valid amount.

    Raises:
        ValueError: If ``source`` has no decision subjects.
    """
    if not _is_decision_source(source):
        raise ValueError(f"{source!r} has no decision subjects")
    if not amount.is_finite() or amount <= 0 or amount >= _MAX_AMOUNT:
        return "invalid_amount"
    if source == "google":
        places = _MICRO_PLACES
    else:
        places = min(minor_unit_exponent(currency), _MICRO_PLACES)
    return None if _is_whole_at(amount, places) else "sub_minor_unit"


def is_valid_amount(amount: Decimal, *, source: str, currency: str) -> bool:
    """Return whether an amount may be decided for a subject of ``source``.

    AAP 0.5.2: an amount must be positive and no finer than the currency's minor unit;
    for Google that means a whole number of micros. The value must also fit
    ``numeric(18, 6)``.

    Example:
        >>> is_valid_amount(Decimal("12.345678"), source="google", currency="USD")
        True
        >>> is_valid_amount(Decimal("12.345"), source="linkedin", currency="USD")
        False

    Args:
        amount: The submitted amount.
        source: ``google`` or ``linkedin``.
        currency: The subject account's ISO 4217 currency.

    Returns:
        Whether the amount is valid.

    Raises:
        ValueError: If ``source`` has no decision subjects (``hubspot``).
    """
    # Untyped callers may pass a float or a string; those are never valid amounts.
    candidate: object = amount
    if not isinstance(candidate, Decimal):
        return False
    return _amount_issue(candidate, source=source, currency=currency) is None


def round_to_minor_unit(amount: Decimal, currency: str) -> Decimal:
    """Round an amount to the currency's minor unit, half up (``ROUND_HALF_UP``).

    Used by B1 for "rounded to the currency's minor unit" (AAP 0.5.4.5). Every minor unit
    is at least a micro, so the result is also a whole number of micros and valid for
    Google.

    Example:
        >>> round_to_minor_unit(Decimal("123.456"), "USD")
        Decimal('123.46')
        >>> round_to_minor_unit(Decimal("1234.5"), "JPY")
        Decimal('1235')

    Args:
        amount: A finite amount.
        currency: The ISO 4217 currency.

    Returns:
        The amount quantized to ``10 ** -minor_unit_exponent(currency)``.

    Raises:
        ValueError: If ``amount`` is not finite.
    """
    if not amount.is_finite():
        raise ValueError(f"{amount} is not a finite amount")
    quantum = Decimal(1).scaleb(-minor_unit_exponent(currency), context=_EXACT)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP, context=_EXACT)


# ---------------------------------------------------------------------------------------
# Shapes and expected states (pure)
# ---------------------------------------------------------------------------------------


def decision_shape(state: SubjectState) -> DecisionShape:
    """Return the decision shape a subject's current state fixes (AAP 0.5.2 table).

    * a Google explicitly shared budget subject: ``budget``;
    * a LinkedIn campaign: ``own_budget``;
    * a Google campaign on an explicitly shared budget (``state.budget_id`` set):
      ``shared_member``;
    * a Google campaign on a budget that is not explicitly shared: ``own_budget``.

    A Google budget that is not explicitly shared is never a subject;
    :func:`load_subject_state` refuses it with :class:`SubjectNotFound`.

    Args:
        state: The subject's current state.

    Returns:
        The decision shape.
    """
    if state.ref.type == "budget":
        return "budget"
    if state.source == "google" and state.budget_id is not None:
        return "shared_member"
    return "own_budget"


def expected_state_of(decision: Decision) -> ExpectedState:
    """Read a ``decision`` row as an :class:`ExpectedState`.

    Args:
        decision: A stored (or flushed) decision row.

    Returns:
        Its materialised expected state.

    Raises:
        ValueError: If the row holds an unknown shape or status, or breaks its shape's
            column rules (impossible for a row the database accepted).
    """
    shape = decision.shape
    if not _is_shape(shape):
        raise ValueError(f"decision {decision.id} has unknown shape {shape!r}")
    status = decision.status
    if status is not None and not _is_campaign_status(status):
        raise ValueError(f"decision {decision.id} has unknown status {status!r}")
    return ExpectedState(
        shape=shape,
        status=status,
        daily_budget=decision.daily_budget,
        total_budget=decision.total_budget,
        budget_id=decision.budget_id,
        tombstone=bool(decision.tombstone),
    )


def _check_snapshot_of(snapshot: EntitySnapshot, ref: SubjectRef) -> None:
    """Refuse a snapshot that describes another entity than ``ref``.

    Raises:
        ValueError: If the snapshot's entity type or id differs from the subject's.
    """
    if snapshot.entity_type != ref.type or snapshot.entity_id != ref.id:
        raise ValueError(
            f"snapshot {snapshot.id} describes {snapshot.entity_type} {snapshot.entity_id}, "
            f"not {ref.type} {ref.id}"
        )


def _google_amounts(
    micros: int | None, period: BudgetPeriod | None
) -> tuple[Decimal | None, Decimal | None]:
    """Split a Google amount in micros into the (daily, total) pair its period owns."""
    if micros is None:
        return None, None
    amount = from_micros(micros)
    return (
        amount if period == "DAILY" else None,
        amount if period == "CUSTOM_PERIOD" else None,
    )


def expected_state_from_snapshot(
    snapshot: EntitySnapshot, *, shape: DecisionShape, budget_period: BudgetPeriod | None
) -> ExpectedState:
    """Materialise the platform state a snapshot observed, in a decision shape.

    Used for baselines (:func:`write_baseline`), ``adopt_platform`` and
    ``platform_correct``. Only the fields the shape owns are filled:

    * ``own_budget``: the status plus the campaign's own amounts. LinkedIn takes
      ``budget_daily`` and ``budget_total`` as observed; Google takes
      ``budget_amount_micros / 1_000_000`` exactly, as ``daily_budget`` for ``DAILY`` or
      ``total_budget`` for ``CUSTOM_PERIOD``. A Google amount the platform omitted stays
      ``None`` (unknown).
    * ``shared_member``: the status and the snapshot's ``budget_id``; no amount.
    * ``budget``: the one amount its period allows; no status.

    Args:
        snapshot: A campaign snapshot (``own_budget``, ``shared_member``) or a budget
            snapshot (``budget``).
        shape: The decision shape to materialise.
        budget_period: The Google budget period that applies; ``None`` for LinkedIn.

    Returns:
        The platform state as an expected state.

    Raises:
        PlatformStateUnknown: If the snapshot's campaign status is not a normalised
            status, a shared member's snapshot has no ``budget_id``, or a budget's amount
            or period is unknown.
        ValueError: If the snapshot's entity type does not match the shape.
    """
    if shape == "budget":
        if snapshot.entity_type != "budget":
            raise ValueError(f"snapshot {snapshot.id} is not a budget snapshot")
        micros = snapshot.budget_amount_micros
        if micros is None or budget_period is None:
            raise PlatformStateUnknown(
                "platform_state_unknown",
                "The shared budget's amount was not reported by Google; no budget decision "
                "can be materialised until a sync observes it.",
                details={"snapshot_id": snapshot.id},
            )
        daily, total = _google_amounts(micros, budget_period)
        return ExpectedState(shape="budget", daily_budget=daily, total_budget=total)
    if snapshot.entity_type != "campaign":
        raise ValueError(f"snapshot {snapshot.id} is not a campaign snapshot")
    status = snapshot.status
    if not _is_campaign_status(status):
        raise PlatformStateUnknown(
            "platform_state_unknown",
            "The campaign's platform status is not a normalised status.",
            details={"snapshot_id": snapshot.id},
        )
    if shape == "shared_member":
        if snapshot.budget_id is None:
            raise PlatformStateUnknown(
                "platform_state_unknown",
                "The snapshot does not name the shared budget the campaign draws from.",
                details={"snapshot_id": snapshot.id},
            )
        return ExpectedState(shape="shared_member", status=status, budget_id=snapshot.budget_id)
    if snapshot.source == "google":
        daily, total = _google_amounts(snapshot.budget_amount_micros, budget_period)
    else:
        daily, total = snapshot.budget_daily, snapshot.budget_total
    return ExpectedState(shape="own_budget", status=status, daily_budget=daily, total_budget=total)


# ---------------------------------------------------------------------------------------
# JSON values of discrepancies (pure)
# ---------------------------------------------------------------------------------------


def _budget_external_id(budget_id: int | None, state: SubjectState) -> str | None:
    """Return the Google id of a shared budget the state knows, else ``None``."""
    if budget_id is None:
        return None
    if budget_id == state.budget_id:
        return state.budget_external_id
    decided = state.current_decision
    if decided is not None and decided.budget_id == budget_id:
        return state.decided_budget_external_id
    return None


def _money_value(
    daily: Decimal | None, total: Decimal | None, *, state: SubjectState, micros: int | None
) -> dict[str, Any]:
    """Return the JSON of an amount pair; Google values also carry ``amount_micros``."""
    value: dict[str, Any] = {
        "daily_budget": _decimal_text(daily),
        "total_budget": _decimal_text(total),
        "currency": state.currency,
    }
    if state.source == "google":
        value["amount_micros"] = micros
    return value


def _decided_micros(expected: ExpectedState) -> int | None:
    """Return the micros of the one amount a Google decision owns, if it owns one."""
    owned = expected.daily_budget if expected.daily_budget is not None else expected.total_budget
    return None if owned is None else to_micros(owned)


def _assignment_value(budget_id: int | None, state: SubjectState) -> dict[str, Any]:
    """Return the JSON of a shared-budget assignment."""
    return {"budget_id": budget_id, "budget_external_id": _budget_external_id(budget_id, state)}


def describe_expected(expected: ExpectedState, *, state: SubjectState) -> dict[str, Any]:
    """Return the JSON of a complete expected state, as discrepancy values store it.

    ``own_budget`` gives ``status`` plus the amounts (with ``currency``, and
    ``amount_micros`` on Google); ``shared_member`` gives ``status``, ``budget_id`` and
    ``budget_external_id``; ``budget`` gives the amounts. A tombstone adds
    ``"tombstone": true``.

    Args:
        expected: The expected state.
        state: The subject, for its currency, source and budget ids.

    Returns:
        A JSON-compatible object.
    """
    value: dict[str, Any]
    if expected.shape == "shared_member":
        value = {"status": expected.status, **_assignment_value(expected.budget_id, state)}
    else:
        micros = _decided_micros(expected) if state.source == "google" else None
        value = _money_value(
            expected.daily_budget, expected.total_budget, state=state, micros=micros
        )
        if expected.shape == "own_budget":
            value = {"status": expected.status, **value}
    if expected.tombstone:
        value["tombstone"] = True
    return value


def describe_decision(decision: Decision, *, state: SubjectState) -> dict[str, Any]:
    """Return the JSON of a decision row, for example a ``missing`` discrepancy's decided side.

    Args:
        decision: The decision row.
        state: The subject.

    Returns:
        :func:`describe_expected` of the row's expected state.
    """
    return describe_expected(expected_state_of(decision), state=state)


def describe_snapshot(snapshot: EntitySnapshot, *, state: SubjectState) -> dict[str, Any]:
    """Return the JSON of the platform state a snapshot observed, in the subject's shape.

    Used for an ``untracked`` discrepancy's platform side. Unlike
    :func:`expected_state_from_snapshot` it never raises for an unknown value: an
    unreported amount or status is JSON ``null``.

    Args:
        snapshot: The subject's snapshot.
        state: The subject.

    Returns:
        A JSON-compatible object with the fields of ``state.shape``.

    Raises:
        ValueError: If the snapshot describes another entity.
    """
    _check_snapshot_of(snapshot, state.ref)
    if state.shape == "shared_member":
        return {"status": snapshot.status, **_assignment_value(snapshot.budget_id, state)}
    if state.source == "google":
        micros = snapshot.budget_amount_micros
        daily, total = _google_amounts(micros, state.budget_period)
    else:
        micros = None
        daily, total = snapshot.budget_daily, snapshot.budget_total
    value = _money_value(daily, total, state=state, micros=micros)
    if state.shape == "own_budget":
        value = {"status": snapshot.status, **value}
    return value


# ---------------------------------------------------------------------------------------
# Patch validation and materialisation (pure)
# ---------------------------------------------------------------------------------------


def _missing_budget_message(state: SubjectState, field: str) -> str:
    """Explain why an own-budget campaign cannot take ``field``."""
    if state.source == "google":
        if state.budget_period is None:
            return f"{state.name} has no budget; {field} cannot be decided"
        other = "total_budget" if field == "daily_budget" else "daily_budget"
        return f"{state.name} has a {state.budget_period} budget, which takes {other}, not {field}"
    if field == "daily_budget":
        return f"{state.name} has no daily budget; daily_budget cannot be decided"
    return f"{state.name} has no lifetime budget; total_budget cannot be decided"


def _check_shape_fields(state: SubjectState, fields: frozenset[str]) -> None:
    """Apply the per-shape field rules of AAP 0.5.2 to a patch's fields.

    Raises:
        PatchRejected: ``field_not_allowed`` or ``shared_budget_amount``.
    """
    amounts = sorted(fields & set(_AMOUNT_FIELDS))
    if state.shape == "own_budget":
        for field, allowed in (
            ("daily_budget", state.has_daily_budget),
            ("total_budget", state.has_total_budget),
        ):
            if field in fields and not allowed:
                raise PatchRejected(
                    "field_not_allowed",
                    _missing_budget_message(state, field),
                    details={"field": field},
                )
        return
    if state.shape == "shared_member":
        if amounts:
            raise PatchRejected(
                "shared_budget_amount",
                f"Amounts are set on shared budget {state.budget_external_id} (budget id "
                f"{state.budget_id}); record the decision on that budget.",
                details={
                    "fields": amounts,
                    "budget_id": state.budget_id,
                    "budget_external_id": state.budget_external_id,
                },
            )
        return
    if "status" in fields:
        raise PatchRejected(
            "field_not_allowed",
            "A shared budget has no status; decide the status on its member campaigns.",
            details={"field": "status"},
        )
    if len(amounts) > 1:
        raise PatchRejected(
            "field_not_allowed",
            "A shared budget takes exactly one amount, not both daily_budget and total_budget.",
            details={"fields": amounts},
        )
    allowed_field = "daily_budget" if state.has_daily_budget else "total_budget"
    for field in amounts:
        if field != allowed_field or not (state.has_daily_budget or state.has_total_budget):
            raise PatchRejected(
                "field_not_allowed",
                f"Shared budget {state.name} has a {state.budget_period} period, which takes "
                f"{allowed_field}, not {field}.",
                details={"field": field},
            )


def _check_amount(amount: Decimal, *, field: str, state: SubjectState) -> None:
    """Refuse an amount that is not positive or is finer than the source allows.

    Raises:
        PatchRejected: ``invalid_amount`` or ``sub_minor_unit``.
    """
    issue = _amount_issue(amount, source=state.source, currency=state.currency)
    if issue is None:
        return
    if issue == "invalid_amount":
        message = f"{field} must be a positive amount below 10^12"
    elif state.source == "google":
        message = f"{field} must be a whole number of micros (at most 6 decimal places)"
    else:
        places = minor_unit_exponent(state.currency)
        message = (
            f"{field} must not be finer than the {state.currency} minor unit ({places} places)"
        )
    raise PatchRejected(issue, message, details={"field": field})


def apply_patch(state: SubjectState, current: ExpectedState, patch: DecisionPatch) -> ExpectedState:
    """Validate a patch and apply it over the current expected state (AAP 0.5.2).

    The pure core of :func:`record_decision`. Checks run in this order:

    1. a tombstoned subject takes no patch (``subject_removed``);
    2. the decided shape must still be the subject's shape (``shape_changed``): a
       campaign that moved onto or off a shared budget changes assignment only through
       its ``budget`` discrepancy's resolution;
    3. the patch must carry a field (``empty_patch``);
    4. the shape rules: ``own_budget`` takes only the budget fields the campaign has
       (Google: the field its period owns); ``shared_member`` takes no amount
       (``shared_budget_amount``, naming the owning budget); ``budget`` takes exactly the
       one amount its period allows and no status (``field_not_allowed``);
    5. amounts must be positive and no finer than the minor unit; Google: whole micros
       (``invalid_amount``, ``sub_minor_unit``);
    6. the patch is applied: unsent fields keep their decided values, a member keeps its
       decided ``budget_id``;
    7. a result equal to the current state is refused (``no_change``).

    Args:
        state: The subject's current state.
        current: The subject's current expected state.
        patch: The submitted fields.

    Returns:
        The complete expected state to record.

    Raises:
        PatchRejected: With the code of the first rule the patch breaks.
    """
    if current.tombstone:
        raise PatchRejected(
            "subject_removed",
            f"{state.name} was acknowledged as removed; it takes no further decisions.",
        )
    if current.shape != state.shape:
        raise PatchRejected(
            "shape_changed",
            f"{state.name} is now a {state.shape} subject but was decided as "
            f"{current.shape}; resolve its budget discrepancy first.",
            details={"decided_shape": current.shape, "current_shape": state.shape},
        )
    fields = patch.fields_set()
    if not fields:
        raise PatchRejected("empty_patch", "The patch carries no field to decide.")
    _check_shape_fields(state, fields)
    for field, amount in (
        ("daily_budget", patch.daily_budget),
        ("total_budget", patch.total_budget),
    ):
        if amount is not None:
            _check_amount(amount, field=field, state=state)

    if state.shape == "own_budget":
        result = ExpectedState(
            shape="own_budget",
            status=patch.status if patch.status is not None else current.status,
            daily_budget=patch.daily_budget
            if patch.daily_budget is not None
            else current.daily_budget,
            total_budget=patch.total_budget
            if patch.total_budget is not None
            else current.total_budget,
        )
    elif state.shape == "shared_member":
        result = ExpectedState(
            shape="shared_member", status=patch.status, budget_id=current.budget_id
        )
    else:
        result = ExpectedState(
            shape="budget", daily_budget=patch.daily_budget, total_budget=patch.total_budget
        )
    if result == current:
        raise PatchRejected("no_change", "The patch does not change the decided state.")
    return result


def _intended_invalid(message: str, **details: object) -> InvalidResolution:
    """Build the ``intended_state_invalid`` rejection."""
    return InvalidResolution("intended_state_invalid", message, details=details)


def parse_intended_state(state: SubjectState, intended: Mapping[str, object]) -> ExpectedState:
    """Validate a ``record_intended`` state against the subject's shape (AAP 0.5.4.4).

    * ``own_budget`` takes ``{status, daily_budget?, total_budget?}`` with exactly the
      budget fields the campaign has;
    * ``shared_member`` takes ``{status}`` only and keeps the shared budget the campaign
      draws from now;
    * ``budget`` takes exactly ``{daily_budget}`` (``DAILY``) or ``{total_budget}``
      (``CUSTOM_PERIOD``), with no status.

    Amounts follow the same rules as patches: positive and no finer than the minor unit
    (Google: whole micros).

    Args:
        state: The subject's current state.
        intended: The submitted intended state.

    Returns:
        The complete expected state to record.

    Raises:
        InvalidResolution: ``intended_state_invalid`` when a field is missing, extra,
            ``null``, mis-typed or too fine.
    """
    keys = set(intended)
    unknown = sorted(keys - _PATCH_FIELDS)
    if unknown:
        raise _intended_invalid(
            f"unknown intended-state fields: {', '.join(unknown)}", fields=unknown
        )
    nulls = sorted(name for name, value in intended.items() if value is None)
    if nulls:
        raise _intended_invalid(
            f"intended-state fields cannot be null: {', '.join(nulls)}", fields=nulls
        )

    required: set[str]
    if state.shape == "own_budget":
        required = {"status"}
        if state.has_daily_budget:
            required.add("daily_budget")
        if state.has_total_budget:
            required.add("total_budget")
    elif state.shape == "shared_member":
        required = {"status"}
    elif state.has_daily_budget:
        required = {"daily_budget"}
    elif state.has_total_budget:
        required = {"total_budget"}
    else:
        raise _intended_invalid(f"{state.name} has no budget period; no amount can be intended")
    if keys != required:
        raise _intended_invalid(
            f"a {state.shape} intended state takes exactly {', '.join(sorted(required))}",
            required=sorted(required),
            received=sorted(keys),
        )

    status_value = intended.get("status")
    status: CampaignStatus | None = None
    if "status" in required:
        if not _is_campaign_status(status_value):
            raise _intended_invalid(
                f"status must be one of {', '.join(CAMPAIGN_STATUSES)}", field="status"
            )
        status = status_value
    amounts: dict[str, Decimal | None] = {"daily_budget": None, "total_budget": None}
    for field in _AMOUNT_FIELDS:
        if field not in required:
            continue
        amount = _as_decimal(intended[field])
        if amount is None:
            raise _intended_invalid(f"{field} must be an exact decimal amount", field=field)
        try:
            _check_amount(amount, field=field, state=state)
        except PatchRejected as exc:
            raise _intended_invalid(exc.message, field=field, rule=exc.code) from exc
        amounts[field] = amount

    if state.shape == "shared_member":
        return ExpectedState(shape="shared_member", status=status, budget_id=state.budget_id)
    return ExpectedState(
        shape=state.shape,
        status=status,
        daily_budget=amounts["daily_budget"],
        total_budget=amounts["total_budget"],
    )


# ---------------------------------------------------------------------------------------
# Comparison at native precision (pure)
# ---------------------------------------------------------------------------------------


def _compare_tombstone(
    snapshot: EntitySnapshot, decision: Decision, expected: ExpectedState, state: SubjectState
) -> list[Mismatch]:
    """Compare a snapshot with an ``acknowledge_removal`` tombstone.

    A snapshot captured at or before the tombstone says nothing about a reappearance. A
    later one means a complete enumeration returned the entity again: a campaign whose
    status is not ``ended`` gives ``status``; a budget subject has no status, so its
    reappearance gives ``budget``.
    """
    if snapshot.captured_at <= decision.decided_at:
        return []
    if state.ref.type == "campaign":
        if snapshot.status is None or snapshot.status == "ended":
            return []
        return [
            Mismatch(
                kind="status",
                decided_value={"status": expected.status, "tombstone": True},
                platform_value={"status": snapshot.status},
            )
        ]
    platform_daily, platform_total = _google_amounts(
        snapshot.budget_amount_micros, state.budget_period
    )
    decided = _money_value(
        expected.daily_budget,
        expected.total_budget,
        state=state,
        micros=_decided_micros(expected),
    )
    decided["tombstone"] = True
    return [
        Mismatch(
            kind="budget",
            decided_value=decided,
            platform_value=_money_value(
                platform_daily,
                platform_total,
                state=state,
                micros=snapshot.budget_amount_micros,
            ),
        )
    ]


def _compare_amounts(
    snapshot: EntitySnapshot, expected: ExpectedState, state: SubjectState
) -> Mismatch | None:
    """Compare the amounts an ``own_budget`` or ``budget`` decision owns.

    Google compares ``snapshot.budget_amount_micros`` with the decided amount converted
    exactly to micros, split by the period that applies now, so a period change is a
    difference too. An amount Google omitted (``budget_amount_micros`` is ``None``) is
    unknown, never zero, and withholds the comparison. LinkedIn compares the observed
    ``budget_daily`` and ``budget_total`` with exact ``Decimal`` equality; a budget present
    on one side and absent on the other differs.
    """
    if state.source == "google":
        micros = snapshot.budget_amount_micros
        if micros is None or state.budget_period is None:
            return None
        platform_pair = (
            micros if state.budget_period == "DAILY" else None,
            micros if state.budget_period == "CUSTOM_PERIOD" else None,
        )
        decided_pair = (
            None if expected.daily_budget is None else to_micros(expected.daily_budget),
            None if expected.total_budget is None else to_micros(expected.total_budget),
        )
        if platform_pair == decided_pair:
            return None
        platform_daily, platform_total = _google_amounts(micros, state.budget_period)
        return Mismatch(
            kind="budget",
            decided_value=_money_value(
                expected.daily_budget,
                expected.total_budget,
                state=state,
                micros=_decided_micros(expected),
            ),
            platform_value=_money_value(platform_daily, platform_total, state=state, micros=micros),
        )
    if (snapshot.budget_daily, snapshot.budget_total) == (
        expected.daily_budget,
        expected.total_budget,
    ):
        return None
    return Mismatch(
        kind="budget",
        decided_value=_money_value(
            expected.daily_budget, expected.total_budget, state=state, micros=None
        ),
        platform_value=_money_value(
            snapshot.budget_daily, snapshot.budget_total, state=state, micros=None
        ),
    )


def compare(snapshot: EntitySnapshot, decision: Decision, *, state: SubjectState) -> list[Mismatch]:
    """Compare a subject's snapshot with its current decision (rule C0, AAP 0.5.4.4).

    No tolerance, at native precision:

    * ``status`` when a campaign's normalised status differs (budget subjects have none);
    * ``budget`` on an ``own_budget`` decision when an owned amount differs (Google in
      micros, LinkedIn exactly), or as a budget *assignment* when the campaign now draws
      from an explicitly shared budget;
    * ``budget`` on a ``shared_member`` decision when ``snapshot.budget_id`` differs from
      the decided ``budget_id``, never on an amount: a change to the shared amount raises
      a discrepancy on the budget subject alone;
    * ``budget`` on a ``budget`` decision when its amount differs in micros.

    A tombstone (``acknowledge_removal``) is compared only for reappearance: a campaign
    snapshot captured after it whose status is not ``ended`` gives ``status``. AAP 0.5.4.4
    calls the reappearance discrepancy a ``status`` one, but a budget subject has no
    status (AAP 0.5.2), so a shared budget's reappearance gives ``budget`` instead.

    ``untracked`` and ``missing`` are never produced here: ``sync/snapshots.py`` decides
    them from baseline timing and complete enumeration (AAP 0.3.3 safe absence). A status
    the snapshot does not report is unknown and raises nothing.

    Args:
        snapshot: The subject's snapshot, normally this sync's.
        decision: The subject's current decision.
        state: The subject's current state (for shape, source, period and currency).

    Returns:
        The mismatches, status first; empty when the snapshot matches.

    Raises:
        ValueError: If the snapshot or the decision belongs to another subject.
    """
    _check_snapshot_of(snapshot, state.ref)
    if decision.subject_type != state.ref.type or decision.subject_id != state.ref.id:
        raise ValueError(
            f"decision {decision.id} belongs to {decision.subject_type} {decision.subject_id}, "
            f"not {state.ref.type} {state.ref.id}"
        )
    expected = expected_state_of(decision)
    if expected.tombstone:
        return _compare_tombstone(snapshot, decision, expected, state)

    mismatches: list[Mismatch] = []
    if (
        state.ref.type == "campaign"
        and expected.status is not None
        and snapshot.status is not None
        and snapshot.status != expected.status
    ):
        mismatches.append(
            Mismatch(
                kind="status",
                decided_value={"status": expected.status},
                platform_value={"status": snapshot.status},
            )
        )
    if expected.shape == "own_budget" and state.shape == "shared_member":
        platform_budget = snapshot.budget_id if snapshot.budget_id is not None else state.budget_id
        mismatches.append(
            Mismatch(
                kind="budget",
                decided_value=_assignment_value(None, state),
                platform_value=_assignment_value(platform_budget, state),
            )
        )
    elif expected.shape == "shared_member":
        if snapshot.budget_id != expected.budget_id:
            mismatches.append(
                Mismatch(
                    kind="budget",
                    decided_value=_assignment_value(expected.budget_id, state),
                    platform_value=_assignment_value(snapshot.budget_id, state),
                )
            )
    else:
        amount_mismatch = _compare_amounts(snapshot, expected, state)
        if amount_mismatch is not None:
            mismatches.append(amount_mismatch)
    return mismatches


# ---------------------------------------------------------------------------------------
# Loading subject state
# ---------------------------------------------------------------------------------------


def _instant(now: datetime | None) -> datetime:
    """Return ``now`` (tz-aware) or the current UTC instant.

    Raises:
        ValueError: If ``now`` is naive.
    """
    if now is None:
        return datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now


async def _budget_currency(session: AsyncSession, budget: Budget) -> str:
    """Return the currency of a Google budget, which carries no account of its own.

    The currency is that of the accounts whose campaigns draw from the budget, or else of
    the dataset's Google account. Amounts are never converted across currencies, so an
    ambiguous answer is refused rather than guessed.

    Raises:
        SubjectNotFound: If no single currency can be determined.
    """
    by_members = await session.execute(
        select(AdAccount.currency)
        .join(Campaign, Campaign.account_id == AdAccount.id)
        .where(Campaign.budget_id == budget.id)
        .distinct()
    )
    currencies = set(by_members.scalars())
    if not currencies:
        by_dataset = await session.execute(
            select(AdAccount.currency)
            .where(AdAccount.dataset_id == budget.dataset_id, AdAccount.source == "google")
            .distinct()
        )
        currencies = set(by_dataset.scalars())
    if len(currencies) != 1:
        raise SubjectNotFound(
            "not_found",
            f"The currency of shared budget {budget.external_id} cannot be determined.",
            details={"budget_id": budget.id},
        )
    return currencies.pop()


async def _latest_snapshot(
    session: AsyncSession, ref: SubjectRef, dataset_id: int
) -> EntitySnapshot | None:
    """Return the subject's newest snapshot (by ``captured_at``, then ``id``)."""
    result = await session.execute(
        select(EntitySnapshot)
        .where(
            EntitySnapshot.entity_type == ref.type,
            EntitySnapshot.entity_id == ref.id,
            EntitySnapshot.dataset_id == dataset_id,
        )
        .order_by(EntitySnapshot.captured_at.desc(), EntitySnapshot.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _current_decision(
    session: AsyncSession, ref: SubjectRef, dataset_id: int
) -> Decision | None:
    """Return the subject's latest decision (by ``decided_at``, then ``id``)."""
    result = await session.execute(
        select(Decision)
        .where(
            Decision.subject_type == ref.type,
            Decision.subject_id == ref.id,
            Decision.dataset_id == dataset_id,
        )
        .order_by(Decision.decided_at.desc(), Decision.id.desc())
        .limit(1)
    )
    return result.scalars().first()


async def load_subject_state(session: AsyncSession, ref: SubjectRef) -> SubjectState:
    """Load what validation and comparison need to know about a subject now.

    * **Campaign**: the campaign, its ``ad_account`` (currency), its Google budget row and
      its dataset. A Google campaign on an explicitly shared budget is a ``shared_member``
      (``budget_id`` set to that budget); otherwise it owns its budget fields: LinkedIn
      the budgets present on the campaign row, Google the field its budget period owns.
    * **Budget**: the budget, which must be explicitly shared, and its currency from the
      accounts of the campaigns drawing from it (or the dataset's Google account).

    The newest ``entity_snapshot`` and the latest ``decision`` are loaded too. Entity and
    dataset rows are re-read from the database (``populate_existing``), because sync jobs
    upsert them with Core statements that do not refresh the session's identity map.

    Args:
        session: The caller's session.
        ref: The subject.

    Returns:
        The subject's state.

    Raises:
        SubjectNotFound: If the campaign or budget does not exist, the budget is not
            explicitly shared, or the budget's currency cannot be determined.
    """
    budget: Budget | None
    if ref.type == "campaign":
        campaign = await session.get(Campaign, ref.id, populate_existing=True)
        if campaign is None:
            raise SubjectNotFound("not_found", f"Campaign {ref.id} does not exist.")
        account = await session.get(AdAccount, campaign.account_id)
        if account is None:
            raise SubjectNotFound("not_found", f"The account of campaign {ref.id} does not exist.")
        source: object = campaign.source
        dataset_id = campaign.dataset_id
        currency = account.currency
        name = campaign.name
        budget = None
        if campaign.budget_id is not None:
            budget = await session.get(Budget, campaign.budget_id, populate_existing=True)
        shared_budget: Budget | None = None
        if source == "linkedin":
            has_daily = campaign.daily_budget is not None
            has_total = campaign.total_budget is not None
            raw_period: str | None = None
        elif budget is not None and budget.explicitly_shared:
            shared_budget = budget
            has_daily = has_total = False
            raw_period = budget.period
        else:
            raw_period = budget.period if budget is not None else campaign.budget_period
            has_daily = raw_period == "DAILY"
            has_total = raw_period == "CUSTOM_PERIOD"
        budget_id = shared_budget.id if shared_budget is not None else None
        budget_external_id = shared_budget.external_id if shared_budget is not None else None
    else:
        budget = await session.get(Budget, ref.id, populate_existing=True)
        if budget is None or not budget.explicitly_shared:
            raise SubjectNotFound(
                "not_found",
                f"Budget {ref.id} is not an explicitly shared budget; only shared budgets "
                "are decision subjects.",
            )
        source = budget.source
        dataset_id = budget.dataset_id
        currency = await _budget_currency(session, budget)
        name = f"Shared budget {budget.external_id}"
        raw_period = budget.period
        has_daily = raw_period == "DAILY"
        has_total = raw_period == "CUSTOM_PERIOD"
        budget_id = None
        budget_external_id = None

    if not _is_decision_source(source):
        raise SubjectNotFound("not_found", f"{ref.type} {ref.id} is not a decision subject.")
    period = raw_period if _is_period(raw_period) else None
    dataset = await session.get(SourceDataset, dataset_id, populate_existing=True)
    if dataset is None:
        raise SubjectNotFound("not_found", f"The dataset of {ref.type} {ref.id} does not exist.")
    snapshot = await _latest_snapshot(session, ref, dataset_id)
    decision = await _current_decision(session, ref, dataset_id)
    decided_budget_external_id: str | None = None
    if decision is not None and decision.budget_id is not None:
        if budget is not None and budget.id == decision.budget_id:
            decided_budget_external_id = budget.external_id
        else:
            decided_budget = await session.get(Budget, decision.budget_id)
            if decided_budget is not None:
                decided_budget_external_id = decided_budget.external_id
    return SubjectState(
        ref=ref,
        dataset_id=dataset_id,
        dataset_state=dataset.state,
        source=source,
        currency=currency,
        name=name,
        shape="budget"
        if ref.type == "budget"
        else ("shared_member" if budget_id is not None else "own_budget"),
        has_daily_budget=has_daily,
        has_total_budget=has_total,
        budget_period=period,
        budget_id=budget_id,
        budget_external_id=budget_external_id,
        latest_snapshot=snapshot,
        current_decision=decision,
        decided_budget_external_id=decided_budget_external_id,
    )


def _ensure_active(state: SubjectState) -> None:
    """Refuse a user decision or resolution on a subject of a non-active dataset.

    Raises:
        SubjectRetired: If the subject's dataset is ``building`` or ``retired``.
    """
    if state.dataset_state != "active":
        raise SubjectRetired(
            "subject_retired",
            f"{state.name} belongs to a {state.dataset_state} dataset; decisions apply only "
            "to the active dataset's subjects.",
            details={"dataset_id": state.dataset_id, "dataset_state": state.dataset_state},
        )


async def _insert_decision(
    session: AsyncSession,
    state: SubjectState,
    expected: ExpectedState,
    *,
    origin: Origin,
    decided_at: datetime,
    decided_by: str | None,
    recommendation_id: int | None,
    patch: Mapping[str, str],
    note: str | None,
) -> Decision:
    """Append one ``decision`` row holding ``expected`` and flush it.

    ``decided_at`` never moves behind the current decision's: the latest row by
    (``decided_at``, ``id``) is the "last decided" state, so a caller clock that runs
    behind another process's must not leave an older decision on top.
    """
    current = state.current_decision
    if current is not None and current.decided_at > decided_at:
        decided_at = current.decided_at
    row = Decision(
        source=state.source,
        dataset_id=state.dataset_id,
        subject_type=state.ref.type,
        subject_id=state.ref.id,
        shape=expected.shape,
        status=expected.status,
        daily_budget=expected.daily_budget,
        total_budget=expected.total_budget,
        budget_id=expected.budget_id,
        tombstone=expected.tombstone,
        decided_at=decided_at,
        decided_by=decided_by,
        origin=origin,
        recommendation_id=recommendation_id,
        patch=dict(patch),
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------------------
# Recording decisions (AAP 0.5.2 patch model)
# ---------------------------------------------------------------------------------------


async def record_decision(
    session: AsyncSession,
    subject: SubjectRef,
    patch: DecisionPatch,
    origin: Origin,
    *,
    decided_by: str | None = None,
    recommendation_id: int | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Record a decision submitted as a patch and return the appended row.

    AAP 0.5.2: the server reads the subject's current expected state, applies the patch,
    validates the result and writes one complete row for the subject's shape. The checks
    run in this order:

    1. the subject's dataset must be ``active`` (:class:`SubjectRetired`, 409);
    2. the subject must have a decision (``untracked_subject``: resolve its ``untracked``
       discrepancy first) that is not a tombstone (``subject_removed``);
    3. then :func:`apply_patch`: shape still current, non-empty patch, per-shape field
       rules, amount precision, materialisation and ``no_change``.

    The row is flushed and one ``analysis_request`` (reason ``decision``) is queued in the
    same transaction (AAP 0.3.2). Nothing is committed: the caller owns the transaction.

    Args:
        session: The caller's session.
        subject: The campaign or shared budget being decided.
        patch: The submitted fields only.
        origin: ``user``, or ``recommendation`` when recorded from an item; ``baseline``
            rows are written by :func:`write_baseline` and ``resolution`` rows by
            :func:`resolve_discrepancy`.
        decided_by: Email of the user recording it; stored, never logged.
        recommendation_id: The recommendation the decision was recorded from.
        note: Free-text note; stored, never logged.
        now: The decision instant (tz-aware); defaults to the current UTC time.

    Returns:
        The new ``decision`` row, carrying its shape and complete expected state.

    Raises:
        SubjectNotFound: If the subject does not exist (404).
        SubjectRetired: If its dataset is not active (409 ``subject_retired``).
        PatchRejected: If the patch breaks a rule of AAP 0.5.2 (422).
        ValueError: If ``origin`` is not ``user`` or ``recommendation``,
            ``recommendation`` comes without a ``recommendation_id``, or ``now`` is naive.
    """
    if not _is_origin(origin) or origin not in ("user", "recommendation"):
        raise ValueError(f"record_decision writes user or recommendation decisions, not {origin!r}")
    if origin == "recommendation" and recommendation_id is None:
        raise ValueError("a recommendation decision needs its recommendation_id")
    instant = _instant(now)
    state = await load_subject_state(session, subject)
    _ensure_active(state)
    current = state.current_expected
    if current is None:
        raise PatchRejected(
            "untracked_subject",
            f"{state.name} has no decision yet; resolve its untracked discrepancy first "
            "(Adopt platform state / Record intended state).",
        )
    expected = apply_patch(state, current, patch)
    decision = await _insert_decision(
        session,
        state,
        expected,
        origin=origin,
        decided_at=instant,
        decided_by=decided_by,
        recommendation_id=recommendation_id,
        patch=patch.as_json(),
        note=note,
    )
    await enqueue_analysis_request(session, "decision")
    _log.info(
        "decisions.recorded",
        decision_id=decision.id,
        subject_type=subject.type,
        subject_id=subject.id,
        shape=expected.shape,
        origin=origin,
        fields=sorted(patch.fields_set()),
    )
    return decision


# ---------------------------------------------------------------------------------------
# Resolutions (AAP 0.5.4.4)
# ---------------------------------------------------------------------------------------

_PLATFORM_OR_DECISION: Final[frozenset[Resolution]] = frozenset(
    {"platform_correct", "decision_correct"}
)
_ADOPT_OR_INTEND: Final[frozenset[Resolution]] = frozenset({"adopt_platform", "record_intended"})
_REMOVAL: Final[frozenset[Resolution]] = frozenset({"acknowledge_removal", "decision_correct"})

RESOLUTIONS: Final[Mapping[tuple[DiscrepancyKind, DecisionShape], frozenset[Resolution]]] = (
    MappingProxyType(
        {
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
    )
)
"""The resolutions valid for each (discrepancy kind, decision shape) pair (AAP 0.5.4.4).

Exactly eleven pairs. There is no ``("status", "budget")``: a shared budget has no status.
For a ``shared_member``, ``budget`` means its budget assignment. ``analysis/discrepancy.py``
and the API offer only these, so the resolver UI renders exactly the row for its
discrepancy, and :func:`resolve_discrepancy` refuses any other combination with 422.
"""


async def _lock_discrepancy(session: AsyncSession, discrepancy_id: int) -> Discrepancy | None:
    """Load and row-lock one discrepancy, re-read from the database."""
    result = await session.execute(
        select(Discrepancy)
        .where(Discrepancy.id == discrepancy_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def _comparison_snapshot(
    session: AsyncSession, discrepancy: Discrepancy, state: SubjectState
) -> EntitySnapshot:
    """Return the snapshot a discrepancy compared, else the subject's newest one.

    Raises:
        PlatformStateUnknown: If the subject has no snapshot at all.
    """
    snapshot: EntitySnapshot | None = None
    if discrepancy.snapshot_id is not None:
        snapshot = await session.get(EntitySnapshot, discrepancy.snapshot_id)
    if snapshot is None:
        snapshot = state.latest_snapshot
    if snapshot is None:
        raise PlatformStateUnknown(
            "platform_state_unknown",
            f"No platform snapshot of {state.name} exists to adopt.",
            details={"discrepancy_id": discrepancy.id},
        )
    _check_snapshot_of(snapshot, state.ref)
    return snapshot


def _platform_correction(
    kind: DiscrepancyKind, platform: ExpectedState, current: ExpectedState | None
) -> ExpectedState:
    """Return the expected state ``platform_correct`` writes for a discrepancy kind.

    The platform value replaces the discrepant field and the rest of the decided shape is
    kept: ``status`` adopts the platform status; ``budget`` adopts the platform amount, or
    for a member the new ``budget_id``, keeping the decided status. When the subject
    changed shape (a member moved onto or off a shared budget), or the current decision is
    a tombstone of a reappeared entity, the platform state is written in the new shape,
    keeping the decided status where both shapes have one.
    """
    if current is None:
        return platform
    if current.tombstone:
        return platform
    if current.shape != platform.shape:
        if platform.status is not None and current.status is not None:
            return replace(platform, status=current.status)
        return platform
    if kind == "status":
        return replace(current, status=platform.status)
    if platform.shape == "own_budget":
        return replace(
            current, daily_budget=platform.daily_budget, total_budget=platform.total_budget
        )
    if platform.shape == "shared_member":
        return replace(current, budget_id=platform.budget_id)
    return platform


def _close(discrepancy: Discrepancy, resolution: Resolution, instant: datetime) -> None:
    """Mark a discrepancy resolved by ``resolution`` at ``instant``."""
    discrepancy.state = "resolved"
    discrepancy.resolution = resolution
    discrepancy.resolved_at = instant


async def resolve_discrepancy(
    session: AsyncSession,
    discrepancy_id: int,
    resolution: Resolution,
    intended_state: Mapping[str, object] | None = None,
    *,
    decided_by: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> Discrepancy:
    """Apply one C0 resolution to an open or awaiting discrepancy (AAP 0.5.4.4).

    The discrepancy is row-locked first. Effects by resolution:

    * ``platform_correct`` (``status``, ``budget``): writes a ``resolution`` decision that
      adopts the platform value from the compared snapshot (the status, the amount, or a
      member's new ``budget_id``), keeping the rest of the shape; closes at once.
    * ``decision_correct`` (``status``, ``budget``, ``missing``): writes nothing and moves
      the row to ``awaiting_platform``; :func:`auto_resolve` closes it once a later
      snapshot matches (for ``missing``, once a complete enumeration returns the entity).
    * ``adopt_platform`` (``untracked``): writes the platform state in the subject's shape;
      closes at once.
    * ``record_intended`` (``untracked``): validates ``intended_state`` with
      :func:`parse_intended_state`, writes it and moves the row to ``awaiting_platform``.
    * ``acknowledge_removal`` (``missing``): writes a tombstone decision (``tombstone``
      set; a campaign's status ``ended``; every other field of the last expected state
      kept, so the shape's columns stay valid) and closes at once, never waiting for a
      snapshot, because a removed entity may never produce one.

    A row moved to ``awaiting_platform`` records the chosen resolution, which it keeps
    when it later closes. One ``analysis_request`` is queued: ``decision`` for
    ``record_intended``, ``discrepancy_resolved`` otherwise. Nothing is committed.

    Args:
        session: The caller's session.
        discrepancy_id: The discrepancy to resolve.
        resolution: The resolution API key.
        intended_state: For ``record_intended`` only, the intended state in the subject's
            shape.
        decided_by: Email of the user resolving it; stored on any decision written, never
            logged.
        note: Free-text note stored on any decision written; never logged.
        now: The resolution instant (tz-aware); defaults to the current UTC time.

    Returns:
        The updated discrepancy (``resolved`` or ``awaiting_platform``).

    Raises:
        SubjectNotFound: If the discrepancy or its subject does not exist (404).
        DiscrepancyClosed: If it is already resolved (409).
        SubjectRetired: If its dataset is not active (409 ``subject_retired``).
        InvalidResolution: If the resolution is not valid for the (kind, shape) pair, or
            ``intended_state`` is missing, misplaced or invalid (422).
        PlatformStateUnknown: If the platform state to adopt is not known (409).
        ValueError: If ``now`` is naive.
    """
    instant = _instant(now)
    discrepancy = await _lock_discrepancy(session, discrepancy_id)
    if discrepancy is None:
        raise SubjectNotFound("not_found", f"Discrepancy {discrepancy_id} does not exist.")
    if discrepancy.state == "resolved":
        raise DiscrepancyClosed(
            "discrepancy_resolved", f"Discrepancy {discrepancy_id} is already resolved."
        )
    dataset = await session.get(SourceDataset, discrepancy.dataset_id, populate_existing=True)
    if dataset is None or dataset.state != "active":
        raise SubjectRetired(
            "subject_retired",
            f"Discrepancy {discrepancy_id} belongs to a dataset that is no longer active.",
            details={"dataset_id": discrepancy.dataset_id},
        )
    subject_type = discrepancy.subject_type
    kind = discrepancy.kind
    if not _is_subject_type(subject_type) or not _is_kind(kind):
        raise ValueError(f"discrepancy {discrepancy_id} holds an unknown subject type or kind")
    if not _is_resolution(resolution):
        raise InvalidResolution(
            "resolution_not_allowed",
            f"Unknown resolution {resolution!r}.",
            details={"resolution": resolution},
        )
    state = await load_subject_state(
        session, SubjectRef(type=subject_type, id=discrepancy.subject_id)
    )
    _ensure_active(state)
    allowed = RESOLUTIONS.get((kind, state.shape), frozenset())
    if resolution not in allowed:
        offered = [key for key in DISCREPANCY_RESOLUTIONS if key in allowed]
        raise InvalidResolution(
            "resolution_not_allowed",
            f"{resolution} is not valid for a {kind} discrepancy on a {state.shape} subject; "
            f"valid: {', '.join(offered)}.",
            details={"kind": kind, "shape": state.shape, "valid": offered},
        )
    if intended_state is not None and resolution != "record_intended":
        raise _intended_invalid("intended_state applies only to record_intended")

    current = state.current_expected
    written: Decision | None = None

    async def write(expected: ExpectedState) -> Decision:
        return await _insert_decision(
            session,
            state,
            expected,
            origin="resolution",
            decided_at=instant,
            decided_by=decided_by,
            recommendation_id=None,
            patch={},
            note=note,
        )

    if resolution == "platform_correct":
        snapshot = await _comparison_snapshot(session, discrepancy, state)
        platform = expected_state_from_snapshot(
            snapshot, shape=state.shape, budget_period=state.budget_period
        )
        written = await write(_platform_correction(kind, platform, current))
        _close(discrepancy, resolution, instant)
    elif resolution == "adopt_platform":
        snapshot = await _comparison_snapshot(session, discrepancy, state)
        written = await write(
            expected_state_from_snapshot(
                snapshot, shape=state.shape, budget_period=state.budget_period
            )
        )
        _close(discrepancy, resolution, instant)
    elif resolution == "record_intended":
        if intended_state is None:
            raise _intended_invalid("record_intended needs an intended_state")
        expected = parse_intended_state(state, intended_state)
        written = await write(expected)
        discrepancy.state = "awaiting_platform"
        discrepancy.resolution = resolution
        discrepancy.decision_id = written.id
        discrepancy.decided_value = describe_expected(expected, state=state)
    elif resolution == "decision_correct":
        discrepancy.state = "awaiting_platform"
        discrepancy.resolution = resolution
    else:
        base = current
        if base is None:
            snapshot = await _comparison_snapshot(session, discrepancy, state)
            base = expected_state_from_snapshot(
                snapshot, shape=state.shape, budget_period=state.budget_period
            )
        if base.status is not None:
            base = replace(base, status="ended")
        written = await write(replace(base, tombstone=True))
        _close(discrepancy, resolution, instant)

    await session.flush()
    await enqueue_analysis_request(
        session, "decision" if resolution == "record_intended" else "discrepancy_resolved"
    )
    _log.info(
        "decisions.discrepancy_resolution",
        discrepancy_id=discrepancy.id,
        subject_type=subject_type,
        subject_id=discrepancy.subject_id,
        kind=kind,
        shape=state.shape,
        resolution=resolution,
        state=discrepancy.state,
        decision_id=written.id if written is not None else None,
    )
    return discrepancy


# ---------------------------------------------------------------------------------------
# Detection-side helpers shared with sync/snapshots.py
# ---------------------------------------------------------------------------------------


async def write_baseline(
    session: AsyncSession, ref: SubjectRef, snapshot: EntitySnapshot, *, now: datetime
) -> Decision:
    """Record a subject's observed platform state as its ``baseline`` decision.

    AAP 0.5.2 ("Decisions: baseline"): the first entity sync of each new dataset writes
    one baseline per campaign and shared budget, each in its own shape, so day one raises
    no false discrepancies. ``sync.snapshots.ensure_baseline`` calls this while the
    dataset is still ``building`` (or already ``active``); there is no patch validation
    and no ``no_change`` check. It is idempotent: a subject that already has a decision
    keeps it, and that decision is returned unchanged. No ``analysis_request`` is queued
    here; the entity sync that called it queues its own.

    Args:
        session: The caller's session.
        ref: The campaign or shared budget.
        snapshot: The subject's snapshot from this entity sync.
        now: The decision instant (tz-aware).

    Returns:
        The baseline decision, or the subject's existing current decision.

    Raises:
        SubjectNotFound: If the subject does not exist or is not a decision subject.
        SubjectRetired: If its dataset is retired.
        PlatformStateUnknown: If the snapshot cannot be materialised, for example a
            shared budget whose amount Google omitted; retry on a later sync.
        ValueError: If the snapshot describes another entity or dataset, or ``now`` is
            naive.
    """
    instant = _instant(now)
    state = await load_subject_state(session, ref)
    if state.dataset_state == "retired":
        raise SubjectRetired(
            "subject_retired",
            f"{state.name} belongs to a retired dataset; no baseline is written.",
            details={"dataset_id": state.dataset_id},
        )
    _check_snapshot_of(snapshot, ref)
    if snapshot.dataset_id != state.dataset_id:
        raise ValueError(f"snapshot {snapshot.id} belongs to another dataset than {ref}")
    if state.current_decision is not None:
        _log.debug(
            "decisions.baseline_exists",
            subject_type=ref.type,
            subject_id=ref.id,
            decision_id=state.current_decision.id,
        )
        return state.current_decision
    expected = expected_state_from_snapshot(
        snapshot, shape=state.shape, budget_period=state.budget_period
    )
    decision = await _insert_decision(
        session,
        state,
        expected,
        origin="baseline",
        decided_at=instant,
        decided_by=None,
        recommendation_id=None,
        patch={},
        note=None,
    )
    _log.debug(
        "decisions.baseline_written",
        decision_id=decision.id,
        subject_type=ref.type,
        subject_id=ref.id,
        shape=expected.shape,
    )
    return decision


async def _subject_provenance(session: AsyncSession, ref: SubjectRef) -> tuple[str, int, str]:
    """Return the subject's ``source``, ``dataset_id`` and dataset state.

    Raises:
        SubjectNotFound: If the subject or its dataset does not exist.
    """
    entity: Campaign | Budget | None
    if ref.type == "campaign":
        entity = await session.get(Campaign, ref.id)
    else:
        entity = await session.get(Budget, ref.id)
    if entity is None:
        raise SubjectNotFound("not_found", f"{ref.type.capitalize()} {ref.id} does not exist.")
    dataset = await session.get(SourceDataset, entity.dataset_id, populate_existing=True)
    if dataset is None:
        raise SubjectNotFound("not_found", f"The dataset of {ref.type} {ref.id} does not exist.")
    return entity.source, entity.dataset_id, dataset.state


async def _unresolved(
    session: AsyncSession, ref: SubjectRef, kind: DiscrepancyKind | None = None
) -> list[Discrepancy]:
    """Row-lock and return the subject's non-resolved discrepancies, oldest first."""
    statement = select(Discrepancy).where(
        Discrepancy.subject_type == ref.type,
        Discrepancy.subject_id == ref.id,
        Discrepancy.state != "resolved",
    )
    if kind is not None:
        statement = statement.where(Discrepancy.kind == kind)
    result = await session.execute(
        statement.order_by(Discrepancy.detected_at, Discrepancy.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def _awaiting_intended(session: AsyncSession, ref: SubjectRef) -> Discrepancy | None:
    """Return the subject's ``untracked`` row waiting after ``record_intended``, if any."""
    for row in await _unresolved(session, ref, "untracked"):
        if row.state == "awaiting_platform" and row.resolution == "record_intended":
            return row
    return None


def _refresh(
    discrepancy: Discrepancy,
    *,
    decided_value: Mapping[str, Any] | None,
    platform_value: Mapping[str, Any] | None,
    snapshot_id: int | None,
    decision_id: int | None,
) -> None:
    """Update a non-resolved discrepancy's comparison, keeping ``detected_at`` and state."""
    discrepancy.decided_value = None if decided_value is None else dict(decided_value)
    discrepancy.platform_value = None if platform_value is None else dict(platform_value)
    discrepancy.snapshot_id = snapshot_id
    discrepancy.decision_id = decision_id


async def raise_discrepancy(
    session: AsyncSession,
    ref: SubjectRef,
    kind: DiscrepancyKind,
    *,
    decided_value: Mapping[str, Any] | None,
    platform_value: Mapping[str, Any] | None,
    snapshot_id: int | None,
    decision_id: int | None,
    detected_at: datetime,
) -> Discrepancy:
    """Open a discrepancy, or refresh the subject's non-resolved one of the same kind.

    Idempotent under ``uq_discrepancy_open_subject_kind`` (one non-resolved row per
    subject and kind). An ``open`` or ``awaiting_platform`` row of the kind is refreshed
    with this comparison (both values, the snapshot and the decision compared), keeping
    its ``detected_at`` and state: the C0 card always shows the latest platform value
    against the current decision.

    A ``status`` or ``budget`` mismatch on a subject whose ``untracked`` discrepancy waits
    (``awaiting_platform``) after ``record_intended`` is that same pending change: the
    intended state *is* the current decision. It refreshes the waiting row's platform
    value and snapshot, which is returned, instead of opening a second C0 item; the row
    still closes through :func:`auto_resolve` once the platform matches, and stays
    resolvable by ``adopt_platform`` or a new ``record_intended`` meanwhile.

    Otherwise a new ``open`` row is inserted inside a savepoint and one
    ``analysis_request`` (``discrepancy_created``) is queued; if a concurrent writer
    inserted the row first, that row is refreshed instead.

    Args:
        session: The caller's session.
        ref: The subject.
        kind: ``status`` or ``budget`` (from :func:`compare`), ``untracked`` or
            ``missing`` (decided by the sync).
        decided_value: The decided side; ``None`` for ``untracked``.
        platform_value: The platform side; ``None`` for ``missing``.
        snapshot_id: The snapshot compared; ``None`` for ``missing``.
        decision_id: The decision compared; ``None`` for ``untracked``.
        detected_at: The detection instant (tz-aware).

    Returns:
        The new or refreshed discrepancy.

    Raises:
        SubjectNotFound: If the subject does not exist.
        SubjectRetired: If its dataset is retired.
        ValueError: If ``kind`` is unknown or ``detected_at`` is naive.
    """
    if not _is_kind(kind):
        raise ValueError(f"discrepancy kind must be one of {DISCREPANCY_KINDS}, not {kind!r}")
    if detected_at.utcoffset() is None:
        raise ValueError("detected_at must be a timezone-aware datetime")
    source, dataset_id, dataset_state = await _subject_provenance(session, ref)
    if dataset_state == "retired":
        raise SubjectRetired(
            "subject_retired",
            f"{ref.type} {ref.id} belongs to a retired dataset; no discrepancy is raised.",
            details={"dataset_id": dataset_id},
        )
    existing = await _unresolved(session, ref, kind)
    if existing:
        _refresh(
            existing[0],
            decided_value=decided_value,
            platform_value=platform_value,
            snapshot_id=snapshot_id,
            decision_id=decision_id,
        )
        await session.flush()
        return existing[0]
    if kind in ("status", "budget"):
        waiting = await _awaiting_intended(session, ref)
        if waiting is not None:
            # The intended state recorded for an untracked subject is the decision this
            # mismatch is measured against, and its untracked row already waits for the
            # platform to reach it; a second C0 item would ask the user to resolve the same
            # pending change twice. The waiting row takes the latest platform value instead.
            merged = dict(waiting.platform_value or {})
            merged.update(platform_value or {})
            waiting.platform_value = merged
            waiting.snapshot_id = snapshot_id
            if decision_id is not None:
                waiting.decision_id = decision_id
            await session.flush()
            return waiting

    row = Discrepancy(
        source=source,
        dataset_id=dataset_id,
        subject_type=ref.type,
        subject_id=ref.id,
        kind=kind,
        decided_value=None if decided_value is None else dict(decided_value),
        platform_value=None if platform_value is None else dict(platform_value),
        snapshot_id=snapshot_id,
        decision_id=decision_id,
        detected_at=detected_at,
        state="open",
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        concurrent = await _unresolved(session, ref, kind)
        if not concurrent:
            raise
        _refresh(
            concurrent[0],
            decided_value=decided_value,
            platform_value=platform_value,
            snapshot_id=snapshot_id,
            decision_id=decision_id,
        )
        await session.flush()
        return concurrent[0]
    await enqueue_analysis_request(session, "discrepancy_created")
    _log.info(
        "decisions.discrepancy_raised",
        discrepancy_id=row.id,
        subject_type=ref.type,
        subject_id=ref.id,
        kind=kind,
    )
    return row


async def auto_resolve(
    session: AsyncSession,
    ref: SubjectRef,
    snapshot: EntitySnapshot | None,
    decision: Decision | None,
    *,
    now: datetime,
    state: SubjectState | None = None,
    enumeration_complete: bool = False,
) -> list[Discrepancy]:
    """Close the subject's discrepancies once its snapshot matches its decision.

    AAP 0.5.4.4: every ``status``, ``budget`` and ``untracked`` discrepancy in ``open`` or
    ``awaiting_platform`` resolves when a later snapshot matches the current decision,
    re-checked on every entity sync. A ``missing`` discrepancy closes here only when it is
    ``awaiting_platform`` after ``decision_correct`` and the caller states that a complete
    enumeration returned the entity (``enumeration_complete``); it never auto-closes from
    ``open``. Without a decision (an untracked subject) or a snapshot, nothing closes.

    A closed row keeps its ``resolution``: the one the user chose before it waited, or
    ``None`` for a row that closed on its own. The ``discrepancy.resolution`` CHECK admits
    only the five resolution API keys, so an automatic match is recorded by
    ``resolved_at`` alone. One ``analysis_request`` (``discrepancy_resolved``) is queued
    when anything closed.

    Args:
        session: The caller's session.
        ref: The subject.
        snapshot: The subject's snapshot from this sync, or ``None`` when absent.
        decision: The subject's current decision, or ``None`` when untracked.
        now: The resolution instant (tz-aware).
        state: The subject's state when the caller already loaded it.
        enumeration_complete: Whether a complete enumeration returned the entity in this
            sync (AAP 0.3.3 safe absence); required to close ``missing``.

    Returns:
        The discrepancies closed, oldest first.

    Raises:
        SubjectNotFound: If the subject does not exist.
        ValueError: If the snapshot or decision belongs to another subject, or ``now`` is
            naive.
    """
    instant = _instant(now)
    if decision is None or snapshot is None:
        return []
    subject_state = state if state is not None else await load_subject_state(session, ref)
    if subject_state.ref != ref:
        raise ValueError(f"state describes {subject_state.ref}, not {ref}")
    if compare(snapshot, decision, state=subject_state):
        return []
    closed: list[Discrepancy] = []
    for discrepancy in await _unresolved(session, ref):
        closable = discrepancy.kind in _AUTO_RESOLVABLE_KINDS or (
            discrepancy.kind == "missing"
            and discrepancy.state == "awaiting_platform"
            and enumeration_complete
        )
        if not closable:
            continue
        discrepancy.state = "resolved"
        discrepancy.resolved_at = instant
        closed.append(discrepancy)
    if not closed:
        return []
    await session.flush()
    await enqueue_analysis_request(session, "discrepancy_resolved")
    _log.info(
        "decisions.auto_resolved",
        subject_type=ref.type,
        subject_id=ref.id,
        discrepancy_ids=[row.id for row in closed],
        kinds=sorted({row.kind for row in closed}),
    )
    return closed
