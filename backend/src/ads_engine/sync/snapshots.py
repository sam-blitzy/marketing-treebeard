"""Append-only entity snapshots, baseline decisions and discrepancy detection.

AAP 0.5.1 ("Sync jobs and snapshots"), 0.5.2 ("Dated snapshots", "Decisions: shapes",
"Decisions: baseline"), 0.5.4.4 (rule C0) and 0.3.3 ("Safe absence"). Every entity sync
(``sync/jobs.py``) ends with the same three steps, all in its own transaction:

1. :func:`write_snapshots` appends one ``entity_snapshot`` per entity the sync read, every
   row stamped with the sync's single observation instant ``captured_at``. Snapshots are
   observations, never effective times (AAP 0.5.4.1), and are never updated or deleted.
2. :func:`ensure_baseline` records the observed platform state as the ``baseline``
   decision of every campaign and Google shared budget that has none yet, while the
   dataset's baseline is still open (:func:`baseline_sealed`), so day one raises no false
   discrepancies. A subject observed while it was open whose baseline had to wait for a
   value the platform did not report (a shared budget's amount) is baselined as soon as
   a later sync reports it, even after the seal.
3. :func:`detect_discrepancies` compares each decision subject's newest snapshot with its
   current decision and raises ``status``, ``budget``, ``untracked`` and ``missing``
   discrepancies, then closes the ones whose subject now matches.

**This module detects; it never writes decisions or discrepancies itself.** Every
``decision`` and ``discrepancy`` row is written through :mod:`ads_engine.decisions`
(:func:`~ads_engine.decisions.write_baseline`, :func:`~ads_engine.decisions.raise_discrepancy`,
:func:`~ads_engine.decisions.auto_resolve`, :func:`~ads_engine.decisions.auto_resolve_absent`).
Detection asks them not to queue an
``analysis_request`` per row and queues one ``discrepancy_created`` and one
``discrepancy_resolved`` request per pass when it created or closed anything (AAP 0.3.2; the
worker coalesces requests within 60 s anyway). ``analysis/discrepancy.py`` only renders the
stored rows as C0 items.

**Set-based.** Baseline and detection read a number of statements that does not grow with
the number of subjects: each pass locks the dataset's subjects in three statements,
subject states come from :func:`~ads_engine.decisions.load_subject_states`, detection
row-locks the dataset's non-resolved discrepancies in one statement, and subjects are
judged in memory. Writes are proportional to the rows created or changed; baselines are
inserted with one flush.

**Locks.** Each pass starts by locking the dataset's complete subject set, once, in the
order every decision writer uses: the dataset ``FOR SHARE`` (so no activation retires it
until the sync commits), then every campaign and then every explicitly shared budget of
the dataset ``FOR NO KEY UPDATE``, each in ``id`` order; detection then locks the
discrepancy rows. The two passes judge different subjects within one transaction, so
locking only the subjects each judges would let two concurrent entity syncs of the
dataset lock in a cycle across the passes (A's baseline holds a new campaign, B's
detection holds an existing one and waits for the new one, A's detection waits for the
existing one), which PostgreSQL aborts as a deadlock. With the whole set taken first,
detection re-locks rows its sync already holds and a concurrent sync waits at its first
subject lock while it holds none. Both passes then load their subjects with ``lock``,
which re-locks those rows, and the :mod:`ads_engine.decisions` writers are told they hold
the locks (``locked``), so they neither lock nor read anything again.

**Decision subjects** are campaigns of both sources and Google explicitly shared budgets
(AAP 0.5.2). Campaign groups, ad groups, keywords, creatives and conversion actions are
snapshotted for the analysis (budget history, the K3 quality-score fallback, the B4 counting
contract) but are never decision subjects. HubSpot has no entity sync and no subjects.

**Safe absence** (AAP 0.3.3): a removal or a disappearance is inferred only from a complete
enumeration. When :attr:`EnumerationResult.complete` is false no ``missing`` discrepancy is
raised and the previous snapshot stands.

Nothing here commits; the entity sync owns the transaction, so snapshots, baselines,
discrepancies and freshness land together or not at all.

Example::

    sealed = await baseline_sealed(session, dataset_id)  # before this sync's freshness
    captured_at = utcnow()
    rows = [
        SnapshotRow.budget(
            entity_id=7, period="DAILY", amount_micros=50_000_000, explicitly_shared=True
        ),
        ...,
    ]
    snapshot_ids = await write_snapshots(session, dataset_id, rows, captured_at=captured_at)
    await ensure_baseline(session, dataset_id, captured_at=captured_at, sealed=sealed)
    report = await detect_discrepancies(
        session, dataset_id, enumeration, snapshot_ids, sealed=sealed, now=captured_at
    )
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.db.models.accounts import (
    BUDGET_PERIODS,
    CAMPAIGN_STATUSES,
    Budget,
    Campaign,
    CampaignGroup,
    ConversionAction,
    Creative,
    Keyword,
)
from ads_engine.db.models.decisions import DISCREPANCY_KINDS, Decision, Discrepancy
from ads_engine.db.models.ops import SOURCES, SourceDataset
from ads_engine.db.models.snapshots import ENTITY_TYPES, EntitySnapshot
from ads_engine.db.repositories import append_snapshots, enqueue_analysis_request, lock_dataset
from ads_engine.decisions import (
    DiscrepancyKind,
    PlatformStateUnknown,
    SubjectRef,
    SubjectState,
    SubjectStates,
    auto_resolve,
    auto_resolve_absent,
    compare,
    describe_decision,
    describe_snapshot,
    from_micros,
    load_subject_states,
    raise_discrepancy,
    write_baseline,
)
from ads_engine.logging import get_logger
from ads_engine.sync.freshness import load_freshness

__all__ = [
    "CONVERSION_ACTION_PAYLOAD_KEYS",
    "DetectionReport",
    "EnumerationResult",
    "SnapshotRow",
    "baseline_sealed",
    "detect_discrepancies",
    "ensure_baseline",
    "write_snapshots",
]

_log = get_logger(__name__)

CONVERSION_ACTION_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "status",
    "include_in_conversions_metric",
    "counting_type",
    "attribution_model",
)
"""Payload keys every ``conversion_action`` snapshot carries (AAP 0.5.4.5, rule B4).

``analysis/metrics.counting_contract`` reads them to decide, day by day, whether Google's
``metrics.conversions`` is an unweighted event count. Values are kept exactly as Google
returned them (for example ``GOOGLE_ADS_LAST_CLICK``, ``GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN``,
``ONE_PER_CLICK``, ``MANY_PER_CLICK``); an unreported ``counting_type`` or
``attribution_model`` stays JSON ``null``, which leaves the contract unknown.
"""

# Which sources can report each entity type (the ENTITY_TYPES docstring of the model).
_ENTITY_SOURCES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "campaign": frozenset({"google", "linkedin"}),
        "campaign_group": frozenset({"linkedin"}),
        "budget": frozenset({"google"}),
        "keyword": frozenset({"google"}),
        "creative": frozenset({"linkedin"}),
        "conversion_action": frozenset({"google"}),
    }
)

# The table each entity type's ``entity_id`` points at; write_snapshots checks membership.
type _EntityModel = (
    type[Campaign]
    | type[CampaignGroup]
    | type[Budget]
    | type[Keyword]
    | type[Creative]
    | type[ConversionAction]
)
_ENTITY_MODELS: Final[Mapping[str, _EntityModel]] = MappingProxyType(
    {
        "campaign": Campaign,
        "campaign_group": CampaignGroup,
        "budget": Budget,
        "keyword": Keyword,
        "creative": Creative,
        "conversion_action": ConversionAction,
    }
)

# The sources whose entity syncs have decision subjects and a ``{source}.entities`` dataset.
_SUBJECT_SOURCES: Final[frozenset[str]] = frozenset({"google", "linkedin"})

# Largest id list bound into one ``IN (...)``; keeps every statement far below psycopg's
# 65,535-parameter limit even for accounts with tens of thousands of keywords.
_ID_CHUNK: Final[int] = 5_000

_QUALITY_SCORE_RANGE: Final[range] = range(1, 11)

# What the ``entity_snapshot`` columns store exactly (``db/base.py`` type map, migration
# 0001): ``budget_daily`` and ``budget_total`` are money, ``numeric(18, 6)`` (twelve integer
# and six fractional digits); ``entity_id``, ``budget_id``, ``dataset_id`` and
# ``budget_amount_micros`` are ``bigint``. PostgreSQL silently rounds a seventh fractional
# digit and refuses an out-of-range value only at write time, so rows are held to these
# bounds on construction and an observation is never rounded.
_BIGINT_MAX: Final[int] = 2**63 - 1
_MONEY_PLACES: Final[int] = 6
_MONEY_LIMIT: Final[Decimal] = Decimal(10**12)
_MICROS_LIMIT: Final[int] = 10**18


def _check_tables() -> None:
    """Fail the import when the per-entity-type maps and ``ENTITY_TYPES`` disagree.

    Raises:
        RuntimeError: If a map omits an entity type or names an unknown one, or a source
            map names an unknown source.
    """
    expected = set(ENTITY_TYPES)
    for name, keys in (("_ENTITY_SOURCES", _ENTITY_SOURCES), ("_ENTITY_MODELS", _ENTITY_MODELS)):
        if set(keys) != expected:
            raise RuntimeError(
                f"ads_engine.sync.snapshots.{name} {sorted(keys)} must cover exactly "
                f"ENTITY_TYPES {sorted(expected)}"
            )
    stray = {source for sources in _ENTITY_SOURCES.values() for source in sources} - set(SOURCES)
    if stray:
        raise RuntimeError(f"ads_engine.sync.snapshots._ENTITY_SOURCES names unknown {stray}")


_check_tables()


# ---------------------------------------------------------------------------------------
# Value validation (pure)
# ---------------------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_id(value: object, name: str) -> None:
    """Refuse an internal row id that is not a positive integer ``bigint`` can hold.

    Raises:
        ValueError: If ``value`` is not a positive ``int``, or is beyond ``bigint``
            (greater than ``2**63 - 1``).
    """
    if not _is_int(value) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer id, not {value!r}")
    if value > _BIGINT_MAX:
        raise ValueError(f"{name} {value!r} is beyond bigint; ids are at most 2**63 - 1")


def _within_money_scale(amount: Decimal) -> bool:
    """Return whether ``amount`` has no non-zero digit past the sixth decimal place.

    Reads the exact digits and exponent, so no context precision can round a fine digit
    away: ``Decimal("1.2345670000")`` qualifies (its trailing zeros are exact), while
    ``Decimal("12.3456780000000000000000000001")`` does not.

    Args:
        amount: A finite amount.

    Returns:
        Whether ``numeric(18, 6)``'s scale holds ``amount`` without rounding.
    """
    _, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):
        return False
    if exponent >= -_MONEY_PLACES:
        return True
    finer = -_MONEY_PLACES - exponent  # the trailing digits that lie past the sixth place
    return all(digit == 0 for digit in digits[-finer:])


def _check_amount(value: object, name: str) -> None:
    """Refuse a money value ``numeric(18, 6)`` cannot store exactly.

    Money travels as ``Decimal`` (decision 6); a ``float`` would be rounded on its way into
    ``numeric``, so it is refused rather than coerced. The value must also be one the
    column stores as observed, because C0 compares at native precision with no tolerance:
    no non-zero digit finer than six decimal places, which PostgreSQL would silently round
    (trailing zeros past six places are exact and accepted), and below ``10**12``, which
    PostgreSQL refuses as a numeric overflow. The value is never quantized or rounded here.

    Raises:
        ValueError: If ``value`` is neither ``None`` nor a finite, non-negative ``Decimal``,
            has a non-zero digit finer than six decimal places, or is at least ``10**12``.
    """
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative Decimal or None, not {value!r}")
    if not _within_money_scale(value):
        raise ValueError(
            f"{name} {value!r} has a digit finer than six decimal places, which numeric(18, 6) "
            "would round; an observation is never rounded"
        )
    if value >= _MONEY_LIMIT:
        raise ValueError(
            f"{name} {value!r} is at least 10**12, beyond numeric(18, 6)'s twelve integer digits"
        )


def _check_micros(value: object, name: str) -> None:
    """Refuse a Google amount in micros that is not a non-negative ``int`` below ``10**18``.

    ``10**18`` micros is ``10**12`` in the currency unit, the first amount
    ``numeric(18, 6)`` cannot hold. Below it, the exact conversion
    (:func:`~ads_engine.decisions.from_micros`) always yields a ``budget_daily`` or
    ``budget_total`` :func:`_check_amount` accepts, so the micros and the ``Decimal`` amount
    of one row describe the same storable value. The bound is also inside ``bigint``
    (``2**63 - 1`` is about ``9.2 * 10**18``), the type of ``budget_amount_micros``.

    Raises:
        ValueError: If ``value`` is neither ``None`` nor a non-negative ``int``, or is at
            least ``10**18``.
    """
    if value is None:
        return
    if not _is_int(value) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer number of micros, not {value!r}")
    if value >= _MICROS_LIMIT:
        raise ValueError(
            f"{name} must be below 10**18 micros, not {value!r}: its amount would be at least "
            "10**12, beyond numeric(18, 6)"
        )


def _check_text(value: object, name: str, *, nullable: bool) -> None:
    """Refuse a raw platform value that is not a non-blank string.

    Raises:
        ValueError: If ``value`` is not a non-blank ``str`` (or ``None`` when allowed).
    """
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string, not {value!r}")


def _freeze(value: object, path: str) -> object:
    """Return an immutable, JSON-compatible copy of one payload value.

    Accepted: ``None``, ``bool``, ``int``, ``str``, and lists, tuples or string-keyed
    mappings of those. A ``Decimal`` or ``datetime`` must be passed as text (the builders
    do this), and a ``float`` is refused (decision 6), so the ``jsonb`` payload never
    carries a rounded number.

    Raises:
        ValueError: If the value or any nested value is of another type, or a mapping has a
            non-string key.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"payload key {path}.{key!r} must be a string")
            frozen[key] = _freeze(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(_freeze(item, f"{path}[{index}]") for index, item in enumerate(value))
    raise ValueError(
        f"payload value {path} must be JSON-compatible (None, bool, int, str, list or "
        f"mapping); {type(value).__name__} is not accepted, pass Decimals and instants as text"
    )


def _thaw(value: object) -> object:
    """Return the plain ``dict``/``list`` form of a frozen payload value, for ``jsonb``."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _period_amounts(
    micros: int | None, period: str | None
) -> tuple[Decimal | None, Decimal | None]:
    """Split a Google amount in micros into the (daily, total) pair its period owns.

    ``DAILY`` owns ``budget_daily`` and ``CUSTOM_PERIOD`` owns ``budget_total``. The amount
    is converted exactly (:func:`~ads_engine.decisions.from_micros`), so the ``Decimal``
    columns and ``budget_amount_micros`` always describe the same value.

    Raises:
        ValueError: If an amount is given without a known period.
    """
    if micros is None:
        return None, None
    if period not in BUDGET_PERIODS:
        raise ValueError(f"a budget amount needs a period in {BUDGET_PERIODS}, not {period!r}")
    amount = from_micros(micros)
    return (
        amount if period == "DAILY" else None,
        amount if period == "CUSTOM_PERIOD" else None,
    )


def _instant_text(value: object, name: str) -> str | None:
    """Return a tz-aware instant as ISO 8601 text for the payload, or ``None``.

    Raises:
        ValueError: If ``value`` is neither ``None`` nor a tz-aware ``datetime``.
    """
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime or None, not {value!r}")
    return value.isoformat()


def _check_instant(value: object, name: str) -> datetime:
    """Return ``value`` when it is a tz-aware ``datetime``.

    Raises:
        ValueError: If it is naive or not a ``datetime``.
    """
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, not {value!r}")
    return value


# ---------------------------------------------------------------------------------------
# Snapshot rows (pure)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One observation of one platform entity, ready for :func:`write_snapshots`.

    AAP 0.5.2 (``entity_snapshot``). Build rows with the classmethod builders, one per
    entity type; each documents what its row carries. Direct construction is validated the
    same way, so no row the database would misread can be written:

    * ``source`` must be able to report ``entity_type`` (campaigns: both sources; campaign
      groups and creatives: LinkedIn; budgets, keywords and conversion actions: Google);
    * a campaign's ``status`` is a normalised status (``active``, ``paused``, ``ended``);
      every other type keeps the raw platform status;
    * money is a finite, non-negative ``Decimal`` and micros a non-negative ``int``
      (decision 6), each one its column stores exactly and never rounded: money below
      ``10**12`` with no non-zero digit past the sixth decimal place (``numeric(18, 6)``),
      micros below ``10**18`` (the micros of that range, inside ``bigint``), and ids
      positive and at most ``2**63 - 1`` (``bigint``); ``budget_amount_micros`` exists only
      on Google campaign and budget rows, ``budget_id`` only on Google campaign rows and
      ``quality_score`` (1 to 10) only on keyword rows;
    * ``payload`` is JSON-compatible (Decimals and instants as text, never ``float``) and is
      frozen on construction; a ``conversion_action`` payload holds every key of
      :data:`CONVERSION_ACTION_PAYLOAD_KEYS`.

    Rows carry no instant: :func:`write_snapshots` stamps every row of one sync with the
    same ``captured_at``.

    Attributes:
        source: ``google`` or ``linkedin``.
        entity_type: One of :data:`~ads_engine.db.models.snapshots.ENTITY_TYPES`.
        entity_id: Internal ``id`` of the observed row in the table named by
            ``entity_type``, in the dataset being written.
        status: The observed status (normalised for campaigns, raw otherwise).
        budget_daily: The observed daily budget in the account currency.
        budget_total: The observed lifetime or custom-period budget.
        budget_amount_micros: The Google budget amount in micros (native precision).
        budget_id: On Google campaign rows, the internal ``budget`` id the campaign drew
            from at the observation instant (dated shared-budget membership, AAP 0.5.4.5).
        quality_score: On keyword rows, Q-G7 ``quality_info.quality_score``.
        payload: The remaining observed fields.

    Raises:
        ValueError: If any rule above is broken.
    """

    source: str
    entity_type: str
    entity_id: int
    status: str | None = None
    budget_daily: Decimal | None = None
    budget_total: Decimal | None = None
    budget_amount_micros: int | None = None
    budget_id: int | None = None
    quality_score: int | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the row and freeze its payload (see the class docstring)."""
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {ENTITY_TYPES}, not {self.entity_type!r}")
        allowed = _ENTITY_SOURCES[self.entity_type]
        if self.source not in allowed:
            raise ValueError(
                f"a {self.entity_type} snapshot comes from {sorted(allowed)}, not {self.source!r}"
            )
        _check_id(self.entity_id, "entity_id")
        if self.entity_type == "campaign":
            if self.status not in CAMPAIGN_STATUSES:
                raise ValueError(
                    f"a campaign snapshot's status must be one of {CAMPAIGN_STATUSES}, "
                    f"not {self.status!r}"
                )
        elif self.status is not None and not isinstance(self.status, str):
            raise ValueError(f"status must be a string or None, not {self.status!r}")
        _check_amount(self.budget_daily, "budget_daily")
        _check_amount(self.budget_total, "budget_total")
        _check_micros(self.budget_amount_micros, "budget_amount_micros")
        google_budget_row = self.source == "google" and self.entity_type in ("campaign", "budget")
        if self.budget_amount_micros is not None and not google_budget_row:
            raise ValueError("budget_amount_micros belongs on Google campaign and budget rows only")
        if self.budget_id is not None:
            if self.source != "google" or self.entity_type != "campaign":
                raise ValueError("budget_id belongs on Google campaign rows only")
            _check_id(self.budget_id, "budget_id")
        if self.quality_score is not None:
            if self.entity_type != "keyword":
                raise ValueError("quality_score belongs on keyword rows only")
            if not _is_int(self.quality_score) or self.quality_score not in _QUALITY_SCORE_RANGE:
                raise ValueError(
                    f"quality_score must be 1 to 10 or None, not {self.quality_score!r}"
                )
        if not isinstance(self.payload, Mapping):
            raise ValueError(f"payload must be a mapping, not {type(self.payload).__name__}")
        frozen = _freeze(self.payload, "payload")
        if self.entity_type == "conversion_action":
            absent = [key for key in CONVERSION_ACTION_PAYLOAD_KEYS if key not in self.payload]
            if absent:
                raise ValueError(
                    f"a conversion_action snapshot payload must carry {absent}; "
                    "analysis/metrics.counting_contract reads every key of "
                    "CONVERSION_ACTION_PAYLOAD_KEYS"
                )
        object.__setattr__(self, "payload", frozen)

    @property
    def key(self) -> tuple[str, int]:
        """The ``(entity_type, entity_id)`` pair :func:`write_snapshots` maps to the new id."""
        return self.entity_type, self.entity_id

    def to_row(self, dataset_id: int, captured_at: datetime) -> dict[str, Any]:
        """Return the ``entity_snapshot`` column values for ``append_snapshots``.

        Args:
            dataset_id: The dataset written to.
            captured_at: The sync's observation instant (tz-aware).

        Returns:
            Column name to value, with the payload as a plain JSON object.

        Raises:
            ValueError: If ``dataset_id`` is not a positive integer within ``bigint`` or
                ``captured_at`` is naive.
        """
        _check_id(dataset_id, "dataset_id")
        return {
            "dataset_id": dataset_id,
            "source": self.source,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "captured_at": _check_instant(captured_at, "captured_at"),
            "status": self.status,
            "budget_daily": self.budget_daily,
            "budget_total": self.budget_total,
            "budget_amount_micros": self.budget_amount_micros,
            "budget_id": self.budget_id,
            "quality_score": self.quality_score,
            "payload": _thaw(self.payload),
        }

    # -- Builders, one per entity type -------------------------------------------------

    @classmethod
    def campaign(
        cls,
        *,
        source: str,
        entity_id: int,
        status: str,
        raw_status: str,
        daily_budget: Decimal | None = None,
        total_budget: Decimal | None = None,
        campaign_group_urn: str | None = None,
        budget_id: int | None = None,
        budget_period: str | None = None,
        budget_amount_micros: int | None = None,
        explicitly_shared: bool = False,
    ) -> SnapshotRow:
        """Build a campaign snapshot, for either source.

        What the row carries (AAP 0.5.2, 0.5.4.1, 0.5.4.5):

        * ``status``: the normalised status (``active``, ``paused`` or ``ended``; a Q-G2b
          ``REMOVED`` campaign is ``ended``). C0 compares it with the decided status.
        * **LinkedIn**: the campaign's own ``daily_budget`` and ``total_budget`` as
          observed (``dailyBudget``, ``totalBudget``), compared exactly by C0 at the
          currency's minor unit; payload ``campaign_group`` (the group URN).
        * **Google**: ``budget_id``, the internal id of the ``budget`` the campaign drew
          from at this instant, which gives dated shared-budget membership; and that
          budget's amount, ``budget_amount_micros`` at native precision plus the same value
          in ``budget_daily`` for a ``DAILY`` period or ``budget_total`` for
          ``CUSTOM_PERIOD``. The ``Decimal`` amounts are derived from the micros here, so
          the two can never disagree. For a member of an explicitly shared budget the
          amount is the shared budget's, recorded as observed; it belongs to the budget
          subject, and C0 compares only the member's ``budget_id`` (``explicitly_shared``
          in the payload says so).
        * payload: ``raw_status``, ``explicitly_shared`` and ``budget_period`` for both
          sources (``False`` and ``None`` on LinkedIn, whose budgets live on the campaign).

        Args:
            source: ``google`` or ``linkedin``.
            entity_id: The internal ``campaign`` id.
            status: The normalised status.
            raw_status: The platform status as returned, for example ``ENABLED``.
            daily_budget: LinkedIn only: the observed daily budget.
            total_budget: LinkedIn only: the observed lifetime budget.
            campaign_group_urn: LinkedIn only: the campaign group URN.
            budget_id: Google only: the internal ``budget`` id; ``None`` without a budget.
            budget_period: Google only: ``DAILY`` or ``CUSTOM_PERIOD``.
            budget_amount_micros: Google only: the budget's own-period amount in micros;
                ``None`` when Google omitted it (unknown, never zero).
            explicitly_shared: Google only: whether the budget is explicitly shared.

        Returns:
            The validated row.

        Raises:
            ValueError: If an argument of the other source is given, a Google amount or
                period comes without a budget, an amount comes without a period, or the
                row fails :class:`SnapshotRow` validation.
        """
        _check_text(raw_status, "raw_status", nullable=False)
        if source == "linkedin":
            if (
                budget_id is not None
                or budget_period is not None
                or budget_amount_micros is not None
                or explicitly_shared
            ):
                raise ValueError(
                    "LinkedIn budgets live on the campaign row: pass daily_budget and "
                    "total_budget, not budget_id, budget_period, budget_amount_micros or "
                    "explicitly_shared"
                )
            _check_text(campaign_group_urn, "campaign_group_urn", nullable=True)
            return cls(
                source=source,
                entity_type="campaign",
                entity_id=entity_id,
                status=status,
                budget_daily=daily_budget,
                budget_total=total_budget,
                payload={
                    "raw_status": raw_status,
                    "explicitly_shared": False,
                    "budget_period": None,
                    "campaign_group": campaign_group_urn,
                },
            )
        if source != "google":
            raise ValueError(f"campaign snapshots come from google or linkedin, not {source!r}")
        if daily_budget is not None or total_budget is not None or campaign_group_urn is not None:
            raise ValueError(
                "a Google campaign's amounts are derived from budget_amount_micros and "
                "budget_period; daily_budget, total_budget and campaign_group_urn are "
                "LinkedIn-only"
            )
        if not isinstance(explicitly_shared, bool):
            raise ValueError(f"explicitly_shared must be a bool, not {explicitly_shared!r}")
        if budget_id is None and (
            budget_period is not None or budget_amount_micros is not None or explicitly_shared
        ):
            raise ValueError("a Google campaign without budget_id has no budget period or amount")
        if budget_period is not None and budget_period not in BUDGET_PERIODS:
            raise ValueError(
                f"budget_period must be one of {BUDGET_PERIODS}, not {budget_period!r}"
            )
        _check_micros(budget_amount_micros, "budget_amount_micros")
        budget_daily, budget_total = _period_amounts(budget_amount_micros, budget_period)
        return cls(
            source=source,
            entity_type="campaign",
            entity_id=entity_id,
            status=status,
            budget_daily=budget_daily,
            budget_total=budget_total,
            budget_amount_micros=budget_amount_micros,
            budget_id=budget_id,
            payload={
                "raw_status": raw_status,
                "explicitly_shared": explicitly_shared,
                "budget_period": budget_period,
            },
        )

    @classmethod
    def campaign_group(
        cls,
        *,
        entity_id: int,
        status: str,
        daily_budget: Decimal | None = None,
        total_budget: Decimal | None = None,
        run_start: datetime | None = None,
        run_end: datetime | None = None,
    ) -> SnapshotRow:
        """Build a LinkedIn campaign-group snapshot, shown in campaign evidence.

        The row carries the raw group status, the group's ``daily_budget`` and
        ``total_budget`` as observed, and the run schedule in the payload (``run_start``,
        ``run_end`` as ISO 8601 text, ``null`` when unset). Groups are not decision
        subjects (AAP 0.5.2).

        Args:
            entity_id: The internal ``campaign_group`` id.
            status: The raw LinkedIn status, for example ``ACTIVE`` or ``ARCHIVED``.
            daily_budget: The group's daily budget, if any.
            total_budget: The group's total budget, if any.
            run_start: Start of the run schedule (tz-aware), if set.
            run_end: End of the run schedule (tz-aware), if set.

        Returns:
            The validated row.

        Raises:
            ValueError: If the status is blank, an instant is naive or the row fails
                :class:`SnapshotRow` validation.
        """
        _check_text(status, "status", nullable=False)
        return cls(
            source="linkedin",
            entity_type="campaign_group",
            entity_id=entity_id,
            status=status,
            budget_daily=daily_budget,
            budget_total=total_budget,
            payload={
                "run_start": _instant_text(run_start, "run_start"),
                "run_end": _instant_text(run_end, "run_end"),
            },
        )

    @classmethod
    def budget(
        cls,
        *,
        entity_id: int,
        period: str,
        amount_micros: int | None,
        explicitly_shared: bool,
    ) -> SnapshotRow:
        """Build a Google ``campaign_budget`` snapshot.

        A budget has no status (``status`` is ``None``). The row carries the amount in
        micros (``budget_amount_micros``, compared by C0 at native precision for a shared
        budget subject) and the same value in ``budget_daily`` for a ``DAILY`` period or
        ``budget_total`` for ``CUSTOM_PERIOD``; payload ``period`` and
        ``explicitly_shared``. An amount Google omitted stays ``None`` (unknown, never
        zero). Every Google budget is snapshotted, shared or not, so the analysis can read
        the amount history of each.

        Args:
            entity_id: The internal ``budget`` id.
            period: ``DAILY`` or ``CUSTOM_PERIOD``.
            amount_micros: The budget's own-period amount in micros, or ``None``.
            explicitly_shared: Whether the budget is explicitly shared.

        Returns:
            The validated row.

        Raises:
            ValueError: If the period is unknown, ``explicitly_shared`` is not a ``bool``
                or the row fails :class:`SnapshotRow` validation.
        """
        if period not in BUDGET_PERIODS:
            raise ValueError(f"period must be one of {BUDGET_PERIODS}, not {period!r}")
        if not isinstance(explicitly_shared, bool):
            raise ValueError(f"explicitly_shared must be a bool, not {explicitly_shared!r}")
        _check_micros(amount_micros, "amount_micros")
        budget_daily, budget_total = _period_amounts(amount_micros, period)
        return cls(
            source="google",
            entity_type="budget",
            entity_id=entity_id,
            status=None,
            budget_daily=budget_daily,
            budget_total=budget_total,
            budget_amount_micros=amount_micros,
            payload={"period": period, "explicitly_shared": explicitly_shared},
        )

    @classmethod
    def keyword(
        cls,
        *,
        entity_id: int,
        status: str,
        match_type: str,
        quality_score: int | None,
    ) -> SnapshotRow:
        """Build a Google keyword snapshot from the current keyword list (Q-G7).

        Written for every keyword the entity sync returns, keywords with no traffic
        included, so K3 can fall back to the start and end quality-score snapshots when
        daily scores are absent (AAP 0.5.4.3). The row carries ``quality_score``
        (``None`` when Google leaves it unpopulated), the raw ``status``, and payload
        ``status`` and ``match_type``.

        Args:
            entity_id: The internal ``keyword`` id.
            status: The raw Google criterion status.
            match_type: The raw match type, for example ``EXACT``.
            quality_score: ``quality_info.quality_score`` (1 to 10) or ``None``.

        Returns:
            The validated row.

        Raises:
            ValueError: If a text value is blank or the row fails :class:`SnapshotRow`
                validation.
        """
        _check_text(status, "status", nullable=False)
        _check_text(match_type, "match_type", nullable=False)
        return cls(
            source="google",
            entity_type="keyword",
            entity_id=entity_id,
            status=status,
            quality_score=quality_score,
            payload={"status": status, "match_type": match_type},
        )

    @classmethod
    def creative(
        cls,
        *,
        entity_id: int,
        intended_status: str | None,
        lead_form_urn: str | None,
    ) -> SnapshotRow:
        """Build a LinkedIn creative snapshot.

        The row carries the raw ``intendedStatus`` as ``status`` (``None`` when LinkedIn
        omits it) and payload ``lead_form_urn``, the canonical form URN of the lead-gen
        call-to-action destination (``None`` for creatives without a form).

        Args:
            entity_id: The internal ``creative`` id.
            intended_status: The raw ``intendedStatus``, or ``None``.
            lead_form_urn: The canonical ``urn:li:leadGenForm:{id}``, or ``None``.

        Returns:
            The validated row.

        Raises:
            ValueError: If a text value is blank or the row fails :class:`SnapshotRow`
                validation.
        """
        _check_text(intended_status, "intended_status", nullable=True)
        _check_text(lead_form_urn, "lead_form_urn", nullable=True)
        return cls(
            source="linkedin",
            entity_type="creative",
            entity_id=entity_id,
            status=intended_status,
            payload={"lead_form_urn": lead_form_urn},
        )

    @classmethod
    def conversion_action(
        cls,
        *,
        entity_id: int,
        status: str,
        include_in_conversions_metric: bool,
        counting_type: str | None,
        attribution_model: str | None,
    ) -> SnapshotRow:
        """Build a Google conversion-action snapshot (Q-G8), the B4 counting contract.

        The payload carries every key of :data:`CONVERSION_ACTION_PAYLOAD_KEYS` with the
        raw Google values: ``status``, ``include_in_conversions_metric``,
        ``counting_type`` (``ONE_PER_CLICK``, ``MANY_PER_CLICK``, …) and
        ``attribution_model`` (``GOOGLE_ADS_LAST_CLICK``,
        ``GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN``, …). An unreported counting type or
        attribution model stays ``None`` and is never filled in, so the contract of that
        day reads unknown (AAP 0.5.4.5). The raw status is also the row's ``status``.

        Args:
            entity_id: The internal ``conversion_action`` id.
            status: The raw Google status.
            include_in_conversions_metric: Whether the action counts toward
                ``metrics.conversions``.
            counting_type: The raw counting type, or ``None``.
            attribution_model: The raw attribution model, or ``None``.

        Returns:
            The validated row.

        Raises:
            ValueError: If ``include_in_conversions_metric`` is not a ``bool``, a text
                value is blank or the row fails :class:`SnapshotRow` validation.
        """
        _check_text(status, "status", nullable=False)
        if not isinstance(include_in_conversions_metric, bool):
            raise ValueError(
                "include_in_conversions_metric must be a bool, not "
                f"{include_in_conversions_metric!r}"
            )
        _check_text(counting_type, "counting_type", nullable=True)
        _check_text(attribution_model, "attribution_model", nullable=True)
        payload: dict[str, object] = {
            "status": status,
            "include_in_conversions_metric": include_in_conversions_metric,
            "counting_type": counting_type,
            "attribution_model": attribution_model,
        }
        return cls(
            source="google",
            entity_type="conversion_action",
            entity_id=entity_id,
            status=status,
            payload=payload,
        )


# ---------------------------------------------------------------------------------------
# Writing snapshots
# ---------------------------------------------------------------------------------------


def _chunks(ids: Sequence[int]) -> Iterable[Sequence[int]]:
    """Yield ``ids`` in slices of at most :data:`_ID_CHUNK`."""
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]


async def _check_entities(
    session: AsyncSession, dataset_id: int, rows: Sequence[SnapshotRow]
) -> None:
    """Refuse snapshots of entities that are not rows of ``dataset_id``.

    ``entity_snapshot.entity_id`` is polymorphic and has no foreign key, so this is the
    check that keeps a snapshot in the same dataset as its entity (decision 8): a snapshot
    of another dataset's campaign would otherwise join live and simulated rows.

    Raises:
        ValueError: If any row names an id that is not a row of its entity type's table in
            this dataset.
    """
    by_type: dict[str, set[int]] = {}
    for row in rows:
        by_type.setdefault(row.entity_type, set()).add(row.entity_id)
    for entity_type, wanted in sorted(by_type.items()):
        model = _ENTITY_MODELS[entity_type]
        ordered = sorted(wanted)
        found: set[int] = set()
        for chunk in _chunks(ordered):
            result = await session.scalars(
                select(model.id).where(model.dataset_id == dataset_id, model.id.in_(chunk))
            )
            found.update(result)
        unknown = sorted(wanted - found)
        if unknown:
            raise ValueError(
                f"{len(unknown)} {entity_type} snapshot(s) name ids that are not "
                f"{entity_type} rows of dataset {dataset_id}: {unknown[:10]}"
            )


async def write_snapshots(
    session: AsyncSession,
    dataset_id: int,
    rows: Sequence[SnapshotRow],
    *,
    captured_at: datetime,
) -> dict[tuple[str, int], int]:
    """Append one ``entity_snapshot`` per entity this entity sync read.

    AAP 0.5.2 ("Dated snapshots"): append-only, never updated or deleted. Every row of the
    sync shares the one observation instant ``captured_at``; it is when the platform state
    was seen, never when a change took effect (AAP 0.5.4.1). Rows go through
    :func:`~ads_engine.db.repositories.append_snapshots`, which refuses a retired or
    missing dataset and a row whose source is not the dataset's.

    Args:
        session: The entity sync's session; nothing is committed here.
        dataset_id: The building or active dataset the entities were written to.
        rows: One row per entity, at most one per ``(entity_type, entity_id)``.
        captured_at: The sync's observation instant (tz-aware), shared by every row.

    Returns:
        ``(entity_type, entity_id)`` mapped to the new snapshot id, which
        :func:`detect_discrepancies` takes as ``snapshot_ids``; ``{}`` for no rows.

    Raises:
        ValueError: If ``captured_at`` is naive, ``dataset_id`` is not a positive integer
            within ``bigint``, an entity appears twice, a row names an entity outside the
            dataset, or a row's source is not the dataset's.
        DatasetNotWritable: If the dataset does not exist or is retired.
    """
    instant = _check_instant(captured_at, "captured_at")
    _check_id(dataset_id, "dataset_id")
    if not rows:
        return {}
    counts = Counter(row.key for row in rows)
    repeated = sorted(key for key, count in counts.items() if count > 1)
    if repeated:
        raise ValueError(
            f"one snapshot per entity per sync: {len(repeated)} entities appear more than "
            f"once, for example {repeated[:5]}"
        )
    await _check_entities(session, dataset_id, rows)
    ids = await append_snapshots([row.to_row(dataset_id, instant) for row in rows], session=session)
    written = dict(zip((row.key for row in rows), ids, strict=True))
    _log.debug(
        "sync.snapshots_written",
        dataset_id=dataset_id,
        rows=len(written),
        by_type=dict(sorted(Counter(row.entity_type for row in rows).items())),
    )
    return written


# ---------------------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------------------


async def _subject_source(session: AsyncSession, dataset_id: int) -> str:
    """Return the source of a dataset whose entity syncs have decision subjects.

    Raises:
        ValueError: If the dataset does not exist or belongs to HubSpot.
    """
    dataset = await session.get(SourceDataset, dataset_id, populate_existing=True)
    if dataset is None:
        raise ValueError(f"source dataset {dataset_id} does not exist")
    if dataset.source not in _SUBJECT_SOURCES:
        raise ValueError(
            f"source dataset {dataset_id} belongs to {dataset.source}, which has no entity "
            "sync and no decision subjects"
        )
    return dataset.source


async def _lock_subjects(session: AsyncSession, dataset_id: int) -> None:
    """Lock the dataset and every decision subject of it, in the order every writer uses.

    The dataset ``FOR SHARE`` (:func:`~ads_engine.db.repositories.lock_dataset`), then
    every campaign and then every explicitly shared budget of the dataset ``FOR NO KEY
    UPDATE`` (the mode of the :mod:`ads_engine.decisions` subject locks), each in
    ascending ``id`` order, held until the caller's transaction ends.
    :func:`ensure_baseline` and :func:`detect_discrepancies` call this before any other
    read, so an entity sync takes its complete subject lock set in its first pass and its
    second pass re-locks rows it already holds, which never waits. Two concurrent syncs of
    the dataset then queue at their first subject lock instead of each locking the subset
    one pass judges and meeting in a cycle across the passes (module **Locks.**).

    Three statements, whatever the number of subjects.

    Raises:
        ValueError: If the dataset no longer exists.
    """
    if await lock_dataset(session, dataset_id) is None:
        raise ValueError(f"source dataset {dataset_id} does not exist")
    await session.execute(
        select(Campaign.id)
        .where(Campaign.dataset_id == dataset_id)
        .order_by(Campaign.id)
        .with_for_update(key_share=True)
    )
    await session.execute(
        select(Budget.id)
        .where(Budget.dataset_id == dataset_id, Budget.explicitly_shared.is_(True))
        .order_by(Budget.id)
        .with_for_update(key_share=True)
    )


async def baseline_sealed(session: AsyncSession, dataset_id: int) -> bool:
    """Return whether the dataset's baseline is sealed.

    AAP 0.5.2 ("Decisions: baseline"): the first entity sync of each new dataset records
    the current platform state as each subject's baseline decision, so day one raises no
    false discrepancies. The baseline stays open until an entity sync of the dataset has
    completed with completeness ``complete``: an incomplete first enumeration (a deferred
    keyset page, a failed Q-G2b chunk, a LinkedIn listing cut short) may have omitted
    campaigns, and those must be baselined when a later sync returns them rather than be
    raised as ``untracked``. Once sealed, a subject first seen without a decision is
    ``untracked``; one observed while the baseline was open whose baseline had to wait for
    a value the platform did not report is still baselined by :func:`ensure_baseline`.

    The verdict is read from the dataset's ``{source}.entities`` freshness row
    (:func:`~ads_engine.sync.freshness.load_freshness`): sealed when ``last_success_at``
    is set and ``completeness`` is ``complete``. Call it at the start of the entity sync,
    **before** that sync records its own freshness, and pass the result to
    :func:`ensure_baseline` and :func:`detect_discrepancies`.

    An entity sync is a full-state read, so its completeness replaces the stored one: a
    later incomplete sync reopens the baseline until the next complete one. While it is
    open, a subject first returned by such a sync is baselined instead of being raised as
    ``untracked``; this errs towards never raising a false ``untracked`` from a truncated
    enumeration, and every later change to that subject is still detected against its
    baseline.

    Args:
        session: An open session.
        dataset_id: A Google or LinkedIn dataset.

    Returns:
        ``True`` when the baseline is sealed.

    Raises:
        ValueError: If the dataset does not exist or is a HubSpot dataset.
    """
    source = await _subject_source(session, dataset_id)
    row = (await load_freshness(session, dataset_id)).get(f"{source}.entities")
    return row is not None and row.last_success_at is not None and row.completeness == "complete"


async def _unbaselined(
    session: AsyncSession, dataset_id: int, captured_at: datetime
) -> list[tuple[SubjectRef, EntitySnapshot]]:
    """Return this sync's snapshot of every subject that has no decision yet.

    Subjects are the dataset's campaigns and its Google explicitly shared budgets. Only
    snapshots captured at ``captured_at`` count: a subject this sync did not observe has no
    current platform state to baseline (a stub campaign the metrics job created for spend
    of a campaign removed before any entity sync, or one an incomplete enumeration
    omitted). Ordered by subject type, then id, so the writes are deterministic.
    """
    pairs: list[tuple[SubjectRef, EntitySnapshot]] = []
    for subject_type in ("campaign", "budget"):
        model: type[Campaign] | type[Budget] = Campaign if subject_type == "campaign" else Budget
        has_decision = (
            select(Decision.id)
            .where(
                Decision.dataset_id == dataset_id,
                Decision.subject_type == subject_type,
                Decision.subject_id == EntitySnapshot.entity_id,
            )
            .exists()
        )
        joined = and_(model.id == EntitySnapshot.entity_id, model.dataset_id == dataset_id)
        statement = (
            select(EntitySnapshot)
            .join(model, joined)
            .where(
                EntitySnapshot.dataset_id == dataset_id,
                EntitySnapshot.entity_type == subject_type,
                EntitySnapshot.captured_at == captured_at,
                ~has_decision,
            )
            .order_by(EntitySnapshot.entity_id, EntitySnapshot.id)
        )
        if subject_type == "budget":
            statement = statement.where(Budget.explicitly_shared.is_(True))
        newest: dict[int, EntitySnapshot] = {}
        for snapshot in await session.scalars(statement):
            newest[snapshot.entity_id] = snapshot
        pairs.extend(
            (SubjectRef(type=subject_type, id=entity_id), snapshot)
            for entity_id, snapshot in sorted(newest.items())
        )
    return pairs


async def _deferred_baselines(
    session: AsyncSession, dataset_id: int, observed: Mapping[SubjectRef, datetime]
) -> frozenset[SubjectRef]:
    """Return the decision-less subjects whose baseline waited while the baseline was open.

    ``observed`` maps subjects without a decision to the instant this sync observed them.
    One of them was observed with the baseline population, and its baseline deferred, when
    an earlier snapshot of the dataset shows it as a decision subject (any campaign
    snapshot; a budget snapshot that marks it explicitly shared) and it has never had an
    ``untracked`` discrepancy. No stored marker is needed: while the baseline is open a
    decision-less subject is baselined unless its platform state cannot be materialised
    (:class:`~ads_engine.decisions.PlatformStateUnknown`, for example a shared budget, or
    an own-budget campaign's budget, whose amount Google omitted), and detection raises
    nothing for it; once sealed, every decision-less subject a sync observes gets its
    ``untracked`` discrepancy in that same sync, and that row closes only with a decision
    (``adopt_platform``, ``record_intended`` or a match against one). Every other subject
    here is first seen after the baseline.

    One statement per subject type and :data:`_ID_CHUNK` subjects.

    Args:
        session: The entity sync's session.
        dataset_id: The dataset just synced.
        observed: Decision-less subjects mapped to this sync's ``captured_at`` for them.

    Returns:
        The subjects whose baseline is deferred.
    """
    found: set[SubjectRef] = set()
    for subject_type in ("campaign", "budget"):
        wanted = {ref.id: at for ref, at in observed.items() if ref.type == subject_type}
        for chunk in _chunks(sorted(wanted)):
            ever_untracked = (
                select(Discrepancy.id)
                .where(
                    Discrepancy.dataset_id == dataset_id,
                    Discrepancy.subject_type == subject_type,
                    Discrepancy.subject_id == EntitySnapshot.entity_id,
                    Discrepancy.kind == "untracked",
                )
                .exists()
            )
            statement = (
                select(EntitySnapshot.entity_id, func.min(EntitySnapshot.captured_at))
                .where(
                    EntitySnapshot.dataset_id == dataset_id,
                    EntitySnapshot.entity_type == subject_type,
                    EntitySnapshot.entity_id.in_(chunk),
                    ~ever_untracked,
                )
                .group_by(EntitySnapshot.entity_id)
            )
            if subject_type == "budget":
                statement = statement.where(
                    EntitySnapshot.payload.contains({"explicitly_shared": True})
                )
            for entity_id, first_seen in await session.execute(statement):
                if first_seen < wanted[entity_id]:
                    found.add(SubjectRef(type=subject_type, id=entity_id))
    return frozenset(found)


async def ensure_baseline(
    session: AsyncSession,
    dataset_id: int,
    *,
    captured_at: datetime,
    sealed: bool,
) -> int:
    """Write the ``baseline`` decision of every subject of the baseline population that has none.

    AAP 0.5.2 ("Decisions: baseline"; "baseline per decision shape"). While the baseline
    is open (``sealed`` is false, from :func:`baseline_sealed`), every campaign of the
    dataset and every Google budget with ``explicitly_shared`` that this sync observed and
    that has no decision yet gets one ``origin=baseline`` decision through
    :func:`~ads_engine.decisions.write_baseline`. Each is written in its own shape, which
    the subject's current state fixes (:func:`~ads_engine.decisions.decision_shape`):
    ``own_budget`` for a LinkedIn campaign or a Google campaign on a budget that is not
    shared, ``shared_member`` for a Google campaign on a shared budget, ``budget`` for the
    shared budget itself. The decision instant is ``captured_at``, the observation it
    records. Campaign groups, ad groups, keywords and creatives are not decision subjects,
    and HubSpot has none.

    Idempotent: a subject that already has a decision keeps it, so calling this again
    writes nothing. A subject whose observed state cannot be materialised (a shared budget,
    or an own-budget campaign's budget, whose amount Google omitted,
    :class:`~ads_engine.decisions.PlatformStateUnknown`) is skipped and logged as
    ``sync.baseline_deferred``; a later sync retries it. That retry continues after the
    baseline seals: such a subject was observed with the baseline population, not first
    seen after it (AAP 0.5.4.4 ``untracked``), so once sealed this
    still baselines every decision-less subject first observed before this sync and never
    raised as ``untracked`` (identified from snapshot and discrepancy history, see
    :func:`_deferred_baselines`), as soon as its state can be materialised. A subject
    first seen after the seal gets no baseline; detection raises it as ``untracked``.

    **Locks.** Before anything else is read, the dataset and every one of its decision
    subjects are locked (:func:`_lock_subjects`), also when nothing is baselined (the
    baseline sealed, or no subject without a decision): the dataset ``FOR SHARE``, then
    every campaign and every explicitly shared budget ``FOR NO KEY UPDATE`` in ``id``
    order, held until the sync commits. Locking only the subjects baselined here would let
    a concurrent sync of the dataset lock the others in its detection pass and then wait
    for one baselined here, while this sync's :func:`detect_discrepancies` waits for one of
    the others: a deadlock across the two passes. A concurrent sync instead waits at its
    first subject lock, holding none, and reads the baselines written here once this sync
    commits.

    The subjects are locked in three statements, the states of every subject come from
    one :func:`~ads_engine.decisions.load_subject_states` call and the new rows are
    inserted with one flush, so the statements sent do not grow with the number of
    subjects.

    Args:
        session: The entity sync's session; nothing is committed here.
        dataset_id: The Google or LinkedIn dataset just synced.
        captured_at: The instant :func:`write_snapshots` stamped this sync's rows with.
        sealed: :func:`baseline_sealed` as evaluated at the start of this sync.

    Returns:
        The number of baseline decisions written.

    Raises:
        ValueError: If ``captured_at`` is naive, or the dataset does not exist or belongs
            to HubSpot.
        SubjectRetired: If the dataset is retired.
    """
    instant = _check_instant(captured_at, "captured_at")
    await _subject_source(session, dataset_id)
    await _lock_subjects(session, dataset_id)
    candidates = await _unbaselined(session, dataset_id, instant)
    if sealed:
        waiting = await _deferred_baselines(
            session, dataset_id, {ref: snapshot.captured_at for ref, snapshot in candidates}
        )
        candidates = [(ref, snapshot) for ref, snapshot in candidates if ref in waiting]
        if not candidates:
            _log.debug("sync.baseline_sealed", dataset_id=dataset_id)
            return 0
    # The candidates are already locked (_lock_subjects), so a decision a concurrent writer
    # committed first was seen above and is kept; the locked load re-locks them for the
    # writers told ``locked``.
    loaded = await load_subject_states(session, [ref for ref, _ in candidates], lock=True)
    written: Counter[str] = Counter()
    deferred: list[dict[str, object]] = []
    for ref, snapshot in candidates:
        try:
            decision = await write_baseline(
                session,
                ref,
                snapshot,
                now=instant,
                state=loaded.require(ref),
                locked=True,
                flush=False,
            )
        except PlatformStateUnknown as exc:
            deferred.append({"subject_type": ref.type, "subject_id": ref.id, "code": exc.code})
            continue
        if decision.origin == "baseline" and decision.decided_at == instant:
            written[decision.shape] += 1
    # One flush inserts every baseline of the sync in batches instead of one round trip each.
    await session.flush()
    total = sum(written.values())
    _log.info(
        "sync.baseline",
        dataset_id=dataset_id,
        written=total,
        by_shape=dict(sorted(written.items())),
        deferred=len(deferred),
    )
    if deferred:
        _log.warning("sync.baseline_deferred", dataset_id=dataset_id, subjects=deferred)
    return total


# ---------------------------------------------------------------------------------------
# Discrepancy detection
# ---------------------------------------------------------------------------------------


def _id_set(value: object, name: str) -> frozenset[int]:
    """Return ``value`` as a ``frozenset`` of positive ``bigint`` ids.

    Raises:
        ValueError: If it is not an iterable of positive integers of at most ``2**63 - 1``.
    """
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise ValueError(f"{name} must be a collection of ids, not {value!r}")
    ids = frozenset(value)
    for item in ids:
        _check_id(item, name)
    return ids


@dataclass(frozen=True, slots=True)
class EnumerationResult:
    """What one entity sync's subject enumeration established (AAP 0.3.3 safe absence).

    Built by ``sync/jobs.py`` from the connector's enumeration: Google
    ``CampaignEnumeration`` (``connectors/google_n8n/partition.py``), LinkedIn the
    all-status campaign ``Listing`` of every account (``connectors/linkedin/client.py``).
    Ids are internal ``campaign`` and ``budget`` ids of the dataset. ``present_budget_ids``
    may name every Google budget the sync returned; only explicitly shared ones are
    decision subjects, and :func:`detect_discrepancies` ignores the rest.

    Attributes:
        source: ``google`` or ``linkedin``.
        complete: The safe-absence verdict. Google: the Q-G2 keyset scan ended on a short
            page with no failed or deferred page and every Q-G2b chunk succeeded. LinkedIn:
            every account's all-status campaign listing (and group listing) finished. When
            ``False`` no removal or disappearance may be inferred.
        present_campaign_ids: Campaigns returned by this sync and snapshotted.
        present_budget_ids: Google budgets returned by this sync and snapshotted.
        removed_campaign_ids: Google tracked campaigns that Q-G2b returned as ``REMOVED``,
            snapshotted as ``ended``. Their snapshots are compared like present ones.
        missing_campaign_ids: Tracked campaigns the complete enumeration did not return at
            all. Empty unless ``complete``.
        missing_budget_ids: Tracked Google shared budgets no present or removed campaign
            draws from any more, after a complete enumeration. Empty unless ``complete``.
        reason: Why the enumeration is incomplete; ``None`` when complete.

    Raises:
        ValueError: If the source has no subjects, an id is not a positive integer within
            ``bigint``, a missing set is non-empty while incomplete, LinkedIn names budgets or
            removed campaigns, or one id is both present (or removed) and missing.
    """

    source: str
    complete: bool
    present_campaign_ids: frozenset[int] = frozenset()
    present_budget_ids: frozenset[int] = frozenset()
    removed_campaign_ids: frozenset[int] = frozenset()
    missing_campaign_ids: frozenset[int] = frozenset()
    missing_budget_ids: frozenset[int] = frozenset()
    reason: str | None = None

    def __post_init__(self) -> None:
        """Normalise every id collection to a ``frozenset`` and check the invariants."""
        if self.source not in _SUBJECT_SOURCES:
            raise ValueError(f"enumerations come from google or linkedin, not {self.source!r}")
        if not isinstance(self.complete, bool):
            raise ValueError(f"complete must be a bool, not {self.complete!r}")
        for name in (
            "present_campaign_ids",
            "present_budget_ids",
            "removed_campaign_ids",
            "missing_campaign_ids",
            "missing_budget_ids",
        ):
            object.__setattr__(self, name, _id_set(getattr(self, name), name))
        if not self.complete and (self.missing_campaign_ids or self.missing_budget_ids):
            raise ValueError(
                "an incomplete enumeration infers no disappearance: missing ids must be empty "
                "unless complete (AAP 0.3.3 safe absence)"
            )
        if self.source == "linkedin" and (
            self.present_budget_ids or self.missing_budget_ids or self.removed_campaign_ids
        ):
            raise ValueError(
                "LinkedIn has no shared budgets and no Q-G2b re-read: budget and removed ids "
                "must be empty"
            )
        overlaps = (
            ("present_campaign_ids", "removed_campaign_ids"),
            ("present_campaign_ids", "missing_campaign_ids"),
            ("removed_campaign_ids", "missing_campaign_ids"),
            ("present_budget_ids", "missing_budget_ids"),
        )
        for first, second in overlaps:
            both: frozenset[int] = getattr(self, first) & getattr(self, second)
            if both:
                raise ValueError(f"ids {sorted(both)[:5]} are in both {first} and {second}")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("reason must not be blank; pass None for no reason")


@dataclass(frozen=True, slots=True)
class DetectionReport:
    """What one :func:`detect_discrepancies` call did.

    Attributes:
        raised: New discrepancy rows by kind (``status``, ``budget``, ``untracked``,
            ``missing``; every kind present, zero when none).
        refreshed: Non-resolved rows that already existed and were refreshed with this
            sync's comparison instead of being raised again.
        auto_resolved: Discrepancies closed because the subject's snapshot now matches its
            decision, or because a complete enumeration no longer returns a subject whose
            decision is a tombstone.
        checked: Subjects examined: present, removed and (after a complete enumeration)
            missing ones.
        skipped: Subjects examined but not judged: a snapshot captured before the current
            decision, a tombstone while the enumeration is incomplete, a missing subject
            that is untracked or already tombstoned, or a subject without a decision whose
            baseline could not be written, while the baseline is open or, for a subject
            observed while it was open, after it sealed.
    """

    raised: Mapping[str, int]
    refreshed: int
    auto_resolved: int
    checked: int
    skipped: int

    @property
    def total_raised(self) -> int:
        """The number of new discrepancy rows of every kind."""
        return sum(self.raised.values())


@dataclass(slots=True)
class _Tally:
    """Mutable counters of one detection pass, frozen into a :class:`DetectionReport`."""

    open_ids: set[int]
    raised: Counter[str] = field(default_factory=Counter)
    refreshed: int = 0
    auto_resolved: int = 0
    checked: int = 0
    skipped: int = 0

    def report(self) -> DetectionReport:
        """Return the counters as a report, with every kind present."""
        return DetectionReport(
            raised=MappingProxyType({kind: self.raised[kind] for kind in DISCREPANCY_KINDS}),
            refreshed=self.refreshed,
            auto_resolved=self.auto_resolved,
            checked=self.checked,
            skipped=self.skipped,
        )


def _discrepancy_subject(row: Discrepancy) -> SubjectRef:
    """Return the decision subject a discrepancy row is about.

    Raises:
        ValueError: If the row holds a subject type the table's ``CHECK`` rules out.
    """
    if row.subject_type == "campaign":
        return SubjectRef(type="campaign", id=row.subject_id)
    if row.subject_type == "budget":
        return SubjectRef(type="budget", id=row.subject_id)
    raise ValueError(f"discrepancy {row.id} holds an unknown subject type {row.subject_type!r}")


async def _lock_unresolved(
    session: AsyncSession, dataset_id: int
) -> dict[SubjectRef, list[Discrepancy]]:
    """Row-lock every non-resolved discrepancy of the dataset in one statement, by subject.

    The rows are locked in ``id`` order, the same order on every pass, and re-read from the
    database (``populate_existing``). Each subject's rows are returned oldest first
    (``detected_at``, then ``id``); :mod:`ads_engine.decisions` works on them instead of
    locking and reading each subject's rows again.
    """
    rows = await session.scalars(
        select(Discrepancy)
        .where(Discrepancy.dataset_id == dataset_id, Discrepancy.state != "resolved")
        .order_by(Discrepancy.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    grouped: dict[SubjectRef, list[Discrepancy]] = {}
    for row in rows:
        grouped.setdefault(_discrepancy_subject(row), []).append(row)
    for subject_rows in grouped.values():
        subject_rows.sort(key=lambda row: (row.detected_at, row.id))
    return grouped


async def _raise(
    session: AsyncSession,
    tally: _Tally,
    state: SubjectState,
    rows: list[Discrepancy],
    kind: DiscrepancyKind,
    *,
    decided_value: Mapping[str, Any] | None,
    platform_value: Mapping[str, Any] | None,
    snapshot_id: int | None,
    decision_id: int | None,
    detected_at: datetime,
) -> None:
    """Raise one discrepancy through :mod:`ads_engine.decisions` and count it.

    :func:`~ads_engine.decisions.raise_discrepancy` refreshes the subject's non-resolved
    row of the kind when one exists (``uq_discrepancy_open_subject_kind``), so a rerun of
    detection never duplicates a row; a row id that was already open before this pass is
    counted as refreshed, any other as raised. It works on the subject's preloaded state and
    locked rows (``rows``, which a new row joins) and leaves the ``analysis_request`` to the
    pass, which queues one for everything it created.
    """
    row = await raise_discrepancy(
        session,
        state.ref,
        kind,
        decided_value=decided_value,
        platform_value=platform_value,
        snapshot_id=snapshot_id,
        decision_id=decision_id,
        detected_at=detected_at,
        state=state,
        locked=True,
        unresolved=rows,
        queue_analysis=False,
    )
    if row.id in tally.open_ids:
        tally.refreshed += 1
    else:
        tally.open_ids.add(row.id)
        tally.raised[kind] += 1
    if all(existing is not row for existing in rows):
        rows.append(row)


async def _shared_budgets(session: AsyncSession, dataset_id: int, ids: Iterable[int]) -> list[int]:
    """Return the ids among ``ids`` that are explicitly shared budgets of the dataset, sorted.

    Only explicitly shared budgets are decision subjects (AAP 0.5.2); any other budget id
    an enumeration names is not judged here.
    """
    found: set[int] = set()
    for chunk in _chunks(sorted(set(ids))):
        result = await session.scalars(
            select(Budget.id).where(
                Budget.dataset_id == dataset_id,
                Budget.explicitly_shared.is_(True),
                Budget.id.in_(chunk),
            )
        )
        found.update(result)
    return sorted(found)


def _dataset_states(
    loaded: SubjectStates, refs: Sequence[SubjectRef], dataset_id: int
) -> dict[SubjectRef, SubjectState]:
    """Return each subject's state, checked to belong to the dataset being synced.

    Raises:
        SubjectNotFound: If a subject does not exist or is not a decision subject.
        ValueError: If one belongs to another dataset.
    """
    states: dict[SubjectRef, SubjectState] = {}
    for ref in refs:
        state = loaded.require(ref)
        if state.dataset_id != dataset_id:
            raise ValueError(
                f"{ref.type} {ref.id} belongs to dataset {state.dataset_id}, not {dataset_id}"
            )
        states[ref] = state
    return states


async def _present_snapshots(
    session: AsyncSession,
    states: Mapping[SubjectRef, SubjectState],
    snapshot_ids: Mapping[SubjectRef, int],
    dataset_id: int,
) -> dict[SubjectRef, EntitySnapshot]:
    """Return this sync's snapshot of every present subject, checked against the subject.

    :func:`~ads_engine.decisions.load_subject_states` already loaded each subject's newest
    snapshot, which is normally this sync's; the others are read together, one statement
    per :data:`_ID_CHUNK` ids.

    Raises:
        ValueError: If a snapshot does not exist, describes another entity or belongs to
            another dataset.
    """
    reread: set[int] = set()
    for ref, snapshot_id in snapshot_ids.items():
        latest = states[ref].latest_snapshot
        if latest is None or latest.id != snapshot_id:
            reread.add(snapshot_id)
    others: dict[int, EntitySnapshot] = {}
    for chunk in _chunks(sorted(reread)):
        result = await session.scalars(select(EntitySnapshot).where(EntitySnapshot.id.in_(chunk)))
        others.update((row.id, row) for row in result)
    found: dict[SubjectRef, EntitySnapshot] = {}
    for ref, snapshot_id in snapshot_ids.items():
        latest = states[ref].latest_snapshot
        snapshot = (
            latest if latest is not None and latest.id == snapshot_id else others.get(snapshot_id)
        )
        if (
            snapshot is None
            or snapshot.entity_type != ref.type
            or snapshot.entity_id != ref.id
            or snapshot.dataset_id != dataset_id
        ):
            raise ValueError(
                f"snapshot {snapshot_id} is not a snapshot of {ref.type} {ref.id} "
                f"in dataset {dataset_id}"
            )
        found[ref] = snapshot
    return found


async def _check_present(
    session: AsyncSession,
    tally: _Tally,
    state: SubjectState,
    snapshot: EntitySnapshot,
    rows: list[Discrepancy],
    *,
    enumeration: EnumerationResult,
    sealed: bool,
    deferred: frozenset[SubjectRef],
    now: datetime,
) -> None:
    """Judge one subject this sync returned against its current decision.

    ``state``, ``snapshot`` and the subject's locked non-resolved ``rows`` were loaded for
    the whole pass, so judging reads nothing more; only what changes is written.

    * No decision: ``untracked`` once the baseline is sealed; while it is open the subject
      is left to :func:`ensure_baseline` (it is reached here without a decision only when
      its baseline could not be materialised yet). A subject observed while the baseline
      was open whose baseline is still deferred (``deferred``, from
      :func:`_deferred_baselines`) is not ``untracked`` after the seal either: it was not
      first seen after the baseline, and :func:`ensure_baseline` baselines it once its
      state is known. It is skipped meanwhile.
    * A snapshot captured before the current decision was recorded is not judged. The
      decision may have been recorded while this sync was reading the platform, after the
      user applied the change there, so that snapshot can neither confirm nor contradict
      it; AAP 0.5.4.4 resolves on "a later snapshot", and the next sync judges it. This
      keeps a decision recorded mid-sync from raising a false C0 item that would hold
      every dependent recommendation (P1) until the next sync.
    * A tombstone (``acknowledge_removal``) is compared only when the enumeration is
      complete: AAP 0.5.4.4 raises the reappearance discrepancy when "a complete
      enumeration later returns it".
    * Otherwise :func:`~ads_engine.decisions.compare` gives the ``status`` and ``budget``
      mismatches at native precision, each raised; with none,
      :func:`~ads_engine.decisions.auto_resolve` closes every closable discrepancy of the
      subject once :func:`~ads_engine.decisions.verified_match` establishes the match, so
      a value the snapshot omits leaves them open.
    """
    tally.checked += 1
    decision = state.current_decision
    if decision is None:
        if not sealed or state.ref in deferred:
            tally.skipped += 1
            return
        await _raise(
            session,
            tally,
            state,
            rows,
            "untracked",
            decided_value=None,
            platform_value=describe_snapshot(snapshot, state=state),
            snapshot_id=snapshot.id,
            decision_id=None,
            detected_at=now,
        )
        return
    if snapshot.captured_at < decision.decided_at:
        tally.skipped += 1
        return
    if decision.tombstone and not enumeration.complete:
        tally.skipped += 1
        return
    mismatches = compare(snapshot, decision, state=state)
    for mismatch in mismatches:
        await _raise(
            session,
            tally,
            state,
            rows,
            mismatch.kind,
            decided_value=mismatch.decided_value,
            platform_value=mismatch.platform_value,
            snapshot_id=snapshot.id,
            decision_id=decision.id,
            detected_at=now,
        )
    if not mismatches:
        closed = await auto_resolve(
            session,
            state.ref,
            snapshot,
            decision,
            now=now,
            state=state,
            locked=True,
            enumeration_complete=enumeration.complete,
            unresolved=rows,
            queue_analysis=False,
        )
        tally.auto_resolved += len(closed)


async def _check_missing(
    session: AsyncSession,
    tally: _Tally,
    state: SubjectState,
    rows: list[Discrepancy],
    *,
    now: datetime,
) -> None:
    """Raise ``missing`` for a tracked subject a complete enumeration did not return.

    Tracked means the subject has a current decision that is not a tombstone. While the
    current decision is a tombstone (``acknowledge_removal``) the absence is the
    acknowledged state: nothing is raised (the subject counts as skipped), and the
    subject's ``status`` and ``budget`` discrepancies, such as one raised when it came back
    in between, are closed by :func:`~ads_engine.decisions.auto_resolve_absent`, because
    the platform again shows exactly what the tombstone records. An untracked subject has
    nothing to be missing from. The discrepancy has no snapshot (``snapshot_id`` is
    ``None``) and no platform value.
    """
    tally.checked += 1
    decision = state.current_decision
    if decision is None:
        tally.skipped += 1
        return
    if decision.tombstone:
        tally.skipped += 1
        closed = await auto_resolve_absent(
            session,
            state.ref,
            decision,
            now=now,
            state=state,
            locked=True,
            unresolved=rows,
            queue_analysis=False,
        )
        tally.auto_resolved += len(closed)
        return
    await _raise(
        session,
        tally,
        state,
        rows,
        "missing",
        decided_value=describe_decision(decision, state=state),
        platform_value=None,
        snapshot_id=None,
        decision_id=decision.id,
        detected_at=now,
    )


async def detect_discrepancies(
    session: AsyncSession,
    dataset_id: int,
    enumeration: EnumerationResult,
    snapshot_ids: Mapping[tuple[str, int], int],
    *,
    sealed: bool,
    now: datetime,
) -> DetectionReport:
    """Run the C0 discrepancy check that follows every entity sync (AAP 0.5.4.4).

    Subjects are the dataset's campaigns and Google explicitly shared budgets:

    * **Present** (``present_*`` and Google ``removed_campaign_ids``): this sync's
      snapshot is compared with the current decision through
      :func:`~ads_engine.decisions.compare`, and each mismatch is raised with
      :func:`~ads_engine.decisions.raise_discrepancy`: ``status`` when the normalised
      status differs; ``budget`` when an owned amount differs at native precision (Google
      micros, LinkedIn minor unit), or, for a shared member, when its budget assignment
      differs. A shared budget's amount change raises one discrepancy on the budget
      subject and none on its members, whose decisions own no amount. A Q-G2b ``REMOVED``
      campaign, snapshotted as ``ended``, is compared the same way. A present subject with
      no decision is ``untracked`` once the baseline is ``sealed``, unless it was observed
      while the baseline was open and its baseline is still deferred for a value the
      platform has not reported (it is skipped until :func:`ensure_baseline` can write
      it). A tombstoned subject
      that a complete enumeration returns again gets a ``status`` discrepancy against the
      tombstone whatever status it is returned with, a Q-G2b ``REMOVED`` campaign
      snapshotted as ``ended`` included (``budget`` for a shared budget, which has no
      status). When the snapshot verifiably matches
      (:func:`~ads_engine.decisions.verified_match`: nothing differs and every compared
      value is known), :func:`~ads_engine.decisions.auto_resolve` closes the subject's
      ``status``, ``budget`` and ``untracked`` discrepancies, and a ``missing`` one
      waiting after ``decision_correct`` once a complete enumeration returned the entity.
    * **Missing** (only when ``enumeration.complete``): a tracked subject in
      ``missing_*_ids`` gets a ``missing`` discrepancy, unless its current decision is a
      tombstone. A tombstoned subject's absence matches the tombstone: no ``missing`` is
      raised, and :func:`~ads_engine.decisions.auto_resolve_absent` closes its ``status``
      and ``budget`` discrepancies (for example the reappearance one raised when it came
      back in between). An incomplete enumeration infers no removal or disappearance: the
      previous snapshot stands (AAP 0.3.3 safe absence).

    **Locks.** Before anything else is read, the dataset and every one of its decision
    subjects are locked (:func:`_lock_subjects`), whichever subjects this pass judges: the
    dataset ``FOR SHARE``, then every campaign and every explicitly shared budget ``FOR NO
    KEY UPDATE`` in ``id`` order, then (after the subjects' states are loaded) the
    discrepancy rows. :func:`ensure_baseline` took the same set first in this sync's
    transaction, so this pass re-locks rows the sync already holds, while a concurrent sync
    of the dataset waits at its first subject lock holding none: locking only the subjects
    judged here would let the two syncs deadlock across their baseline and detection
    passes.

    **Set-based.** The pass reads a number of statements that does not grow with the
    number of subjects: three statements lock the dataset and its subjects,
    :func:`~ads_engine.decisions.load_subject_states` re-locks the subjects judged
    (``lock``) and loads each subject's state with its newest snapshot and current
    decision, one statement then row-locks every non-resolved discrepancy of the dataset
    (in ``id`` order), and the subjects are judged in memory. Writes are proportional to
    the rows created or changed. One
    ``analysis_request`` ``discrepancy_created`` is queued when the pass created any
    discrepancy and one ``discrepancy_resolved`` when it closed any (AAP 0.3.2), instead
    of one per row.

    Re-running detection on the same sync is safe: a non-resolved row of the same subject
    and kind is refreshed, never duplicated.

    Args:
        session: The entity sync's session; nothing is committed here.
        dataset_id: The Google or LinkedIn dataset just synced.
        enumeration: The sync's enumeration verdict and subject ids.
        snapshot_ids: :func:`write_snapshots`'s result; every present and removed subject
            must have its snapshot of this sync in it.
        sealed: :func:`baseline_sealed` as evaluated at the start of this sync.
        now: The detection instant (tz-aware), stored as ``detected_at`` and
            ``resolved_at``.

    Returns:
        The counts of what was raised, refreshed, resolved, checked and skipped.

    Raises:
        ValueError: If ``now`` is naive, the dataset does not exist, is a HubSpot dataset
            or is not the enumeration's source, a present subject has no snapshot of this
            sync, or a subject belongs to another dataset.
        SubjectNotFound: If a campaign id does not exist.
        SubjectRetired: If the dataset is retired, from the first subject judged; a pass
            that skips every subject writes nothing.
    """
    instant = _check_instant(now, "now")
    source = await _subject_source(session, dataset_id)
    if enumeration.source != source:
        raise ValueError(
            f"the enumeration describes {enumeration.source}, but dataset {dataset_id} "
            f"belongs to {source}"
        )
    await _lock_subjects(session, dataset_id)
    present = [
        SubjectRef(type="campaign", id=campaign_id)
        for campaign_id in sorted(
            enumeration.present_campaign_ids | enumeration.removed_campaign_ids
        )
    ]
    present.extend(
        SubjectRef(type="budget", id=budget_id)
        for budget_id in await _shared_budgets(session, dataset_id, enumeration.present_budget_ids)
    )
    present_snapshot_ids: dict[SubjectRef, int] = {}
    for ref in present:
        snapshot_id = snapshot_ids.get((ref.type, ref.id))
        if snapshot_id is None:
            raise ValueError(
                f"{ref.type} {ref.id} was returned by this sync but has no snapshot of it in "
                "snapshot_ids"
            )
        present_snapshot_ids[ref] = snapshot_id
    missing: list[SubjectRef] = []
    if enumeration.complete:
        missing = [
            SubjectRef(type="campaign", id=campaign_id)
            for campaign_id in sorted(enumeration.missing_campaign_ids)
        ]
        missing.extend(
            SubjectRef(type="budget", id=budget_id)
            for budget_id in await _shared_budgets(
                session, dataset_id, enumeration.missing_budget_ids
            )
        )

    subjects = [*present, *missing]
    # Lock order of every decision writer: the dataset, then the subject rows (both held
    # since _lock_subjects; the locked load re-locks them for the writers told
    # ``locked``), then the discrepancy rows.
    states = _dataset_states(
        await load_subject_states(session, subjects, lock=True), subjects, dataset_id
    )
    unresolved = await _lock_unresolved(session, dataset_id)
    tally = _Tally(open_ids={row.id for rows in unresolved.values() for row in rows})
    snapshots = await _present_snapshots(session, states, present_snapshot_ids, dataset_id)
    deferred: frozenset[SubjectRef] = frozenset()
    if sealed:
        deferred = await _deferred_baselines(
            session,
            dataset_id,
            {
                ref: snapshots[ref].captured_at
                for ref in present
                if states[ref].current_decision is None
            },
        )
    for ref in present:
        await _check_present(
            session,
            tally,
            states[ref],
            snapshots[ref],
            unresolved.setdefault(ref, []),
            enumeration=enumeration,
            sealed=sealed,
            deferred=deferred,
            now=instant,
        )
    for ref in missing:
        await _check_missing(
            session, tally, states[ref], unresolved.setdefault(ref, []), now=instant
        )

    report = tally.report()
    if report.total_raised:
        await enqueue_analysis_request(session, "discrepancy_created")
    if report.auto_resolved:
        await enqueue_analysis_request(session, "discrepancy_resolved")
    event: dict[str, object] = {
        "dataset_id": dataset_id,
        "source": source,
        "complete": enumeration.complete,
        "sealed": sealed,
        "raised": dict(report.raised),
        "refreshed": report.refreshed,
        "auto_resolved": report.auto_resolved,
        "checked": report.checked,
        "skipped": report.skipped,
    }
    if not enumeration.complete:
        # No absence was inferred this sync; the reason says which step fell short.
        event["reason"] = enumeration.reason or "enumeration incomplete"
    _log.info("sync.discrepancies", **event)
    return report
