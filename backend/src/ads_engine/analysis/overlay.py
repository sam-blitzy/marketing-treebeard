"""Read-time current-state overlay: live holds, historical marking, flags-now and banners.

A stored run is immutable evidence (AAP 0.5.3 "Where outputs are frozen"), but what the
screen shows must not go stale after a change (AAP 0.3.2 "Refresh after change, and the
current-state overlay"). Until the queued rerun completes, ``GET /recommendations`` reads the
current state from live tables and returns it beside the frozen items, never inside them:

* **Open discrepancies** are read live, so a discrepancy detected after the run holds every
  dependent item at once (P1, AAP 0.5.4.2) and the banner count is always current.
* **Source modes, active datasets and dataset freshness** are read live, so simulation labels
  and stale or incomplete flags are current.
* **Feedback** (dismiss, snooze, done) is read live, as ``hidden_by``.
* **Items from replaced data are historical-only**: an item whose frozen
  ``evidence.provenance`` names a dataset that is no longer active (or no longer the effective
  CRM dataset of the active LinkedIn dataset) is ``historical``, ``held_now`` with
  :data:`~ads_engine.analysis.context.HISTORICAL_REASON`, and the API disables every action on
  it: Record decision, Mark done, Dismiss, Snooze and the C0 resolver. The original provenance
  stays in ``evidence.provenance``; the current source status is ``data_status_now``.
* **Unverifiable provenance fails closed**: every rule declares the datasets it reads and its
  evidence freezes one entry per dataset read, so every stored item is source-derived. An
  item whose provenance is missing, empty, or holds any entry that is not a complete
  ``{source, dataset_id, mode, role}`` record cannot be checked against the current datasets:
  it is historical exactly like an item from replaced data, and never permits a write.
  Reporting is separate and never affects currentness: the CRM scope of the stale and
  incomplete checks, ``data_status_now`` and the simulated-source labels read the entries
  that decode on their own, so a LinkedIn-attributed item with one malformed entry is still
  judged against the effective CRM dataset (AAP 0.5.4.1 "Required datasets", 0.3.3).

The overlay never rewrites a run's stored evidence, flags or holds: it only adds holds, flags
and hidden state. Its outputs mirror AAP 0.5.3 ``Overlay``, ``ItemOverlay`` and ``Banner``.

Decisions taken in this module, each stated where its code lives:

* **Signatures**: AAP 0.5.1 names ``overlay(run, items, now)`` and ``is_current(provenance)``.
  :func:`overlay` gains a leading ``session`` because it reads live tables, plus keyword-only
  ``findings`` (the run's stored findings, when the caller already loaded them) and a
  required ``thresholds``: the caller injects the configuration it already holds (the API's
  cached ``app.state.thresholds``), so the async read path never reads config files.
  :func:`is_current` takes the current id set so it stays pure.
* **Hold subjects**: an item's live P1 hold reads the subjects ``precedence.py`` froze in
  ``evidence.extra.depends_on`` (``[{type, id}]``) plus the item's own ``decision_subject``,
  the campaign or budget a decision on the item would write.
* **Short-window minimum**: ``analysis_run`` stores the window but not
  ``window.trusted_min_days``, so the short-window banner reads that value from the
  configuration, exactly as ``api/schemas.py`` computes ``Run.short_window``; banner and run
  metadata therefore always agree.
* **Required ranges** (P4, AAP 0.3.2): an item is ``stale`` when a required dataset is old
  or covers less than the window end, and ``incomplete`` when one is not ``complete`` over
  its required range: coverage starting after the range start or ending before the window
  end is incomplete, so an end shortfall is both stale and incomplete. The range is the
  run's window, widened where the item's rule reads further back (``_RULE_LOOKBACKS``): a K1
  item's ``google.metrics_daily`` must cover its frozen ``evidence.extra.baseline_window``
  from its start, because K1's baseline rate reads the trailing ``k1.baseline_days``
  (AAP 0.5.4.3). A missing or malformed baseline start fails closed to that many days back
  from the window end (the frozen ``k1.baseline_days``, else the configured one), never to
  the window start. Full-state entity datasets are judged by completeness and age alone.
* **Missing freshness rows**: a required dataset with no ``dataset_freshness`` row (or no
  active dataset at all) has never succeeded, so it is both stale and incomplete, never
  silently fresh: on its cards, in ``stale_datasets`` and ``incomplete_datasets``, and in
  both banners ("no successful sync", "no freshness record").
* **Page aggregates**: the page-level lists and banners aggregate the same checks the cards
  show plus every current freshness row (judged over the run's window). A dataset is listed
  stale or incomplete when any of those checks is, so one item's complete check never masks
  another item's wider range; the banners name every distinct reason of each dataset in a
  fixed order and count distinct dataset names.

Performance: one query per table for the whole page, never one per item (AAP 0.1.1: the five
sections render in under 2 s). :func:`overlay` issues at most eight statements whatever the
number of items: the active datasets, the effective CRM dataset (and its companion row), the
source connections, the unresolved discrepancies, the freshness rows, the active feedback and,
when ``findings`` is not given, the run's tracking findings.

Database access is read-only: no writes, no flush, no commit; the caller owns the session.

Example::

    run = await session.get(AnalysisRun, run_id)
    rows = (await session.scalars(select(Recommendation).filter_by(run_id=run.id))).all()
    result = await overlay(session, run, rows, now, findings=findings, thresholds=thresholds)
    result.items[rows[0].id].held_now  # live P1 hold or historical-only marking
    [banner.code for banner in result.banners]  # AAP 0.5.5 fixed banner order
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import ARRAY, ColumnElement, Text, and_, any_, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.context import (
    ALERT_BANNERS,
    BANNER_INCOMPLETE_DATASETS,
    BANNER_OPEN_DISCREPANCIES,
    BANNER_ORDER,
    BANNER_SHORT_WINDOW,
    BANNER_SIMULATED_SOURCES,
    BANNER_STALE_DATASETS,
    BANNER_TONES,
    BANNER_TRACKING_ABSENT,
    C0,
    DATA_LIVE,
    DATA_MIXED,
    DATA_SIMULATED,
    DATA_STALE,
    DATA_STATUS_VALUES,
    FINDING_CONVERSION_TRACKING_ABSENT,
    FLAG_INCOMPLETE,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    FULL_STATE_DATASETS,
    HELD_DISCREPANCY_REASON,
    HISTORICAL_REASON,
    K1,
    Completeness,
    Flag,
    Freshness,
    ProvenanceEntry,
    Tone,
    json_value,
    unverified_message,
)
from ads_engine.analysis.thresholds import Thresholds
from ads_engine.db.models.analysis import (
    FEEDBACK_KINDS,
    AnalysisRun,
    Recommendation,
    RecommendationFeedback,
    RunFinding,
)
from ads_engine.db.models.decisions import Discrepancy
from ads_engine.db.models.ops import (
    DATASETS,
    MODES,
    SOURCES,
    DatasetFreshness,
    SourceConnection,
    SourceDataset,
)
from ads_engine.db.repositories import active_datasets, effective_crm_dataset_id

__all__ = [
    "BannerView",
    "ItemOverlayView",
    "OverlayResult",
    "OverlayView",
    "current_dataset_ids",
    "is_current",
    "item_is_current",
    "overlay",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNRESOLVED_STATES: Final[tuple[str, ...]] = ("open", "awaiting_platform")
"""Discrepancy states that hold dependent items (every state except ``resolved``)."""

_SUBJECT_TYPES: Final[frozenset[str]] = frozenset({"campaign", "budget"})
"""Decision subject types an ``extra.depends_on`` entry or ``decision_subject`` may name."""

_SOURCE_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"google": "Google", "linkedin": "LinkedIn", "hubspot": "HubSpot"}
)
"""Display names of the sources, for banner and flag messages."""

_DATASET_ORDER: Final[Mapping[str, int]] = MappingProxyType(
    {name: index for index, name in enumerate(DATASETS)}
)
"""Position of each dataset name, so dataset lists render in one stable order."""

_TRACKING_ADVICE: Final = "verify tracking before acting on zero-outcome rules"
"""The P2 advice the tracking banner repeats (AAP 0.5.4.2)."""

_DEFAULT_SIMULATED_REASON: Final = "simulated data mode"
"""Banner reason for a simulated source whose dataset and connection record no reason."""

_RULE_LOOKBACKS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {K1: MappingProxyType({"google.metrics_daily": "baseline_window"})}
)
"""``rule_id -> {dataset name: evidence.extra key}``: the frozen lookbacks reaching before
the window start.

The ``evidence.extra`` key holds ``{start, end, days}``. K1's baseline rate reads account
conversions and clicks from ``google.metrics_daily`` over the trailing ``k1.baseline_days``
ending at the window end (AAP 0.5.4.3; ``analysis/metrics.py`` ``google_rate_baseline``), so
a K1 item's ``google.metrics_daily`` must be complete from its baseline start. No other rule
reads a dated dataset before its window: C3a's ``segment_window`` is the run window itself,
and the budget-history lookbacks read entity snapshots, which are full-state datasets.
"""

_LOOKBACK_DAYS_KEYS: Final[Mapping[str, str]] = MappingProxyType({K1: "k1.baseline_days"})
"""The threshold giving the length of each rule's lookback (:data:`_RULE_LOOKBACKS`).

Used only when an item's frozen lookback start is missing or malformed: the item's frozen
``evidence.thresholds`` value when it is a positive integer, else the configured one, counted
back from the run's window end. A lookback is never silently narrowed to the window.
"""

SubjectRef = tuple[str, int]
"""A decision subject as ``(subject_type, internal id)``, as discrepancies store it."""

FeedbackKey = tuple[str, str | None, str | None]
"""``(rule_id, entity_type, entity_id)``: how feedback matches an item across runs."""


def _require_aware(value: object, what: str) -> datetime:
    """Return ``value`` as a tz-aware ``datetime``.

    Raises:
        TypeError: When ``value`` is not a ``datetime``.
        ValueError: When it is naive.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be a tz-aware datetime, found {value!r}")
    if value.utcoffset() is None:
        raise ValueError(f"{what} must be tz-aware (an instant), found a naive datetime")
    return value


def _require_thresholds(value: object) -> Thresholds:
    """Return ``value`` as the caller's loaded configuration.

    The overlay runs on the async read path, so it never reads ``config/*.yaml`` itself:
    the caller injects the instance it already holds, as the API does with its cached
    ``app.state.thresholds`` (:data:`~ads_engine.api.deps.ThresholdsDep`).

    Raises:
        TypeError: When ``value`` is not a :class:`Thresholds`, ``None`` included.
    """
    if not isinstance(value, Thresholds):
        raise TypeError(
            "thresholds must be the loaded Thresholds configuration injected by the caller "
            f"(ThresholdsDep / app.state.thresholds), found {type(value).__name__}"
        )
    return value


def _format_instant(value: datetime) -> str:
    """Render an instant as ``YYYY-MM-DD HH:MM UTC``; the UI re-formats in DISPLAY_TIMEZONE."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _ordered_names(names: Iterable[str]) -> tuple[str, ...]:
    """Return distinct dataset names in :data:`~ads_engine.db.models.ops.DATASETS` order."""
    return tuple(
        sorted(set(names), key=lambda name: (_DATASET_ORDER.get(name, len(DATASETS)), name))
    )


def _source_list(sources: Iterable[str]) -> str:
    """Return distinct source display names in source order, joined with commas."""
    distinct = set(sources)
    return ", ".join(_SOURCE_LABELS[source] for source in SOURCES if source in distinct)


# ---------------------------------------------------------------------------
# Public views (AAP 0.5.3 Overlay, ItemOverlay, Banner)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OverlayView:
    """The page-level current state (AAP 0.5.3 ``Overlay``), computed at read time.

    Attributes:
        open_discrepancies: Every unresolved (``open`` or ``awaiting_platform``) discrepancy
            of the active datasets, counted live.
        source_modes: ``{google, linkedin, hubspot}``: the mode of each source's active
            primary dataset, ``None`` for a source with no active dataset.
        stale_datasets: Names of the datasets that are stale now (last success older than
            ``sources.stale_after_hours``, never succeeded, or coverage short of the run's
            window end), in ``DATASETS`` order: every current freshness row and every
            dataset an item on the page requires, including one with no freshness row or
            whose source has no current dataset.
        incomplete_datasets: Names of the datasets that are not ``complete`` over a required
            range, in ``DATASETS`` order: an item's range is the run's window, widened for a
            K1 item's ``google.metrics_daily`` to its frozen baseline start, and a freshness
            row no item requires is judged over the run's window. ``complete`` rows whose
            coverage starts after the range start or ends before the window end count. A
            dataset is listed when any card's check or its current row's check is incomplete.
        computed_at: The read time the overlay was computed for.

    Raises:
        ValueError: On construction, for a negative count, a mode outside ``MODES``, a source
            set other than ``SOURCES``, an unknown dataset name or a naive ``computed_at``.
        TypeError: On construction, for a non-``int`` count or a non-``datetime`` instant.
    """

    open_discrepancies: int
    source_modes: Mapping[str, str | None]
    stale_datasets: tuple[str, ...]
    incomplete_datasets: tuple[str, ...]
    computed_at: datetime

    def __post_init__(self) -> None:
        """Validate the fields and freeze ``source_modes`` in source order."""
        count: object = self.open_discrepancies
        if isinstance(count, bool) or not isinstance(count, int):
            raise TypeError(f"OverlayView.open_discrepancies must be an int, found {count!r}")
        if count < 0:
            raise ValueError(f"OverlayView.open_discrepancies must be >= 0, found {count}")
        modes = dict(self.source_modes)
        if set(modes) != set(SOURCES):
            raise ValueError(f"OverlayView.source_modes must name exactly {SOURCES}, found {modes}")
        for source, mode in modes.items():
            if mode is not None and mode not in MODES:
                raise ValueError(f"OverlayView.source_modes[{source!r}] must be one of {MODES}")
        object.__setattr__(
            self, "source_modes", MappingProxyType({source: modes[source] for source in SOURCES})
        )
        for what in ("stale_datasets", "incomplete_datasets"):
            names = tuple(getattr(self, what))
            unknown = [name for name in names if name not in _DATASET_ORDER]
            if unknown:
                raise ValueError(f"OverlayView.{what} holds unknown dataset names {unknown}")
            object.__setattr__(self, what, _ordered_names(names))
        _require_aware(self.computed_at, "OverlayView.computed_at")

    def to_json(self) -> dict[str, Any]:
        """Return AAP 0.5.3 ``Overlay``: ``{open_discrepancies, source_modes, ...}``."""
        return {
            "open_discrepancies": self.open_discrepancies,
            "source_modes": dict(self.source_modes),
            "stale_datasets": list(self.stale_datasets),
            "incomplete_datasets": list(self.incomplete_datasets),
            "computed_at": json_value(self.computed_at),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemOverlayView:
    """One item's read-time state (AAP 0.5.3 ``ItemOverlay``), beside its frozen fields.

    Attributes:
        held_now: Whether the item is held now: historical-only, held at compute time, or
            dependent on a subject with a discrepancy that is unresolved now.
        held_reason_now: Why it is held now; set exactly when :attr:`held_now`.
        historical: Whether any dataset in the item's frozen provenance is no longer current,
            or the provenance cannot be verified (missing, empty or malformed); every action
            on the item is then disabled (AAP 0.3.2).
        data_status_now: ``live``, ``simulated``, ``mixed`` or ``stale``, from the current
            modes of the item's provenance sources and the current freshness of its required
            datasets.
        flags_now: Current-state flags only (``simulated``, ``stale``, ``incomplete``,
            ``unverified_discrepancies``), in ``FLAG_CODES`` order; the UI shows them with the
            flags recorded in the run.
        hidden_by: The active feedback kind on the item's rule and entity, if any; never set
            for a C0 item.

    Raises:
        ValueError: On construction, for a hold without a reason (or a reason without a
            hold), a historical item that is not held, an unknown data status or feedback
            kind, or a repeated flag code.
        TypeError: On construction, when a flag is not a :class:`Flag`.
    """

    held_now: bool
    held_reason_now: str | None = None
    historical: bool
    data_status_now: str
    flags_now: tuple[Flag, ...] = ()
    hidden_by: str | None = None

    def __post_init__(self) -> None:
        """Validate the hold, status, flags and feedback kind."""
        if self.held_now != (self.held_reason_now is not None):
            raise ValueError("ItemOverlayView.held_reason_now is set exactly when held_now")
        if self.held_reason_now is not None and not self.held_reason_now.strip():
            raise ValueError("ItemOverlayView.held_reason_now must not be blank")
        if self.historical and not self.held_now:
            raise ValueError("a historical item is always held (every action is disabled)")
        if self.data_status_now not in DATA_STATUS_VALUES:
            raise ValueError(
                f"ItemOverlayView.data_status_now must be one of {DATA_STATUS_VALUES}, "
                f"found {self.data_status_now!r}"
            )
        flags = tuple(self.flags_now)
        for flag in flags:
            if not isinstance(flag, Flag):
                raise TypeError(f"ItemOverlayView.flags_now items must be Flag, found {flag!r}")
        codes = [flag.code for flag in flags]
        if len(set(codes)) != len(codes):
            raise ValueError(f"ItemOverlayView.flags_now repeats a code: {codes}")
        object.__setattr__(self, "flags_now", flags)
        if self.hidden_by is not None and self.hidden_by not in FEEDBACK_KINDS:
            raise ValueError(
                f"ItemOverlayView.hidden_by must be one of {FEEDBACK_KINDS}, "
                f"found {self.hidden_by!r}"
            )

    @property
    def actions_enabled(self) -> bool:
        """Whether the item's actions may be offered; ``False`` for a historical item."""
        return not self.historical

    def to_json(self) -> dict[str, Any]:
        """Return AAP 0.5.3 ``ItemOverlay``; ``held_reason_now`` and ``hidden_by`` when set.

        Key order: ``held_now, held_reason_now?, historical, data_status_now, flags_now,
        hidden_by?``.
        """
        rendered: dict[str, Any] = {"held_now": self.held_now}
        if self.held_reason_now is not None:
            rendered["held_reason_now"] = self.held_reason_now
        rendered["historical"] = self.historical
        rendered["data_status_now"] = self.data_status_now
        rendered["flags_now"] = [flag.to_json() for flag in self.flags_now]
        if self.hidden_by is not None:
            rendered["hidden_by"] = self.hidden_by
        return rendered


@dataclass(frozen=True, slots=True, kw_only=True)
class BannerView:
    """One global banner (AAP 0.5.3 ``Banner``), built from the run plus the overlay.

    Attributes:
        code: One of :data:`~ads_engine.analysis.context.BANNER_ORDER`.
        tone: The tone of that code (:data:`~ads_engine.analysis.context.BANNER_TONES`).
        message: The rendered message.
        count: The number the banner is about (discrepancies, sources or datasets), when
            there is one.

    Raises:
        ValueError: On construction, for an unknown code, a tone other than the code's own, a
            blank message or a negative count.
    """

    code: str
    tone: Tone
    message: str
    count: int | None = None

    def __post_init__(self) -> None:
        """Validate the code, tone, message and count."""
        if self.code not in BANNER_ORDER:
            raise ValueError(f"BannerView.code must be one of {BANNER_ORDER}, found {self.code!r}")
        if self.tone != BANNER_TONES[self.code]:
            raise ValueError(
                f"banner {self.code!r} has tone {BANNER_TONES[self.code]!r}, found {self.tone!r}"
            )
        if not self.message.strip():
            raise ValueError("BannerView.message must not be blank")
        if self.count is not None and (isinstance(self.count, bool) or self.count < 0):
            raise ValueError(f"BannerView.count must be a non-negative int, found {self.count!r}")

    @property
    def alert(self) -> bool:
        """Whether the UI renders it with the ``alert`` role: discrepancies only (AAP 0.5.5)."""
        return self.code in ALERT_BANNERS

    def to_json(self) -> dict[str, Any]:
        """Return ``{code, tone, message, count?}``."""
        rendered: dict[str, Any] = {"code": self.code, "tone": self.tone, "message": self.message}
        if self.count is not None:
            rendered["count"] = self.count
        return rendered


@dataclass(frozen=True, slots=True)
class OverlayResult:
    """Everything the overlay adds to one page of a run, computed at read time.

    Attributes:
        overlay: The page-level current state.
        items: The per-item current state, keyed by ``recommendation.id``; every item passed
            to :func:`overlay` has an entry.
        banners: The global banners, in the fixed AAP 0.5.5 order (``BANNER_ORDER``), each
            present only when it applies.
    """

    overlay: OverlayView
    items: Mapping[int, ItemOverlayView]
    banners: tuple[BannerView, ...]

    def to_json(self) -> dict[str, Any]:
        """Return ``{overlay, items: {"<id>": ItemOverlay}, banners: [Banner]}``."""
        return {
            "overlay": self.overlay.to_json(),
            "items": {str(item_id): view.to_json() for item_id, view in self.items.items()},
            "banners": [banner.to_json() for banner in self.banners],
        }


# ---------------------------------------------------------------------------
# Current datasets and provenance (AAP 0.3.2 "Items from replaced data are historical-only")
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ActiveDataset:
    """A dataset the current computations read, as loaded from ``source_dataset``."""

    dataset_id: int
    source: str
    mode: str
    role: str
    reason: str | None

    @classmethod
    def of(cls, row: SourceDataset) -> _ActiveDataset:
        """Copy the fields the overlay needs from a ``source_dataset`` row."""
        return cls(row.id, row.source, row.mode, row.role, row.reason)


@dataclass(frozen=True, slots=True)
class _CurrentDatasets:
    """Each source's active primary dataset plus the effective CRM dataset for LinkedIn.

    Attributes:
        primaries: The active primary dataset per source; a source with none is absent.
        linkedin_crm: The CRM dataset LinkedIn-attributed evidence reads now: the companion
            while LinkedIn is simulated, HubSpot's active primary otherwise; ``None`` without
            an active LinkedIn dataset or a CRM dataset for it.
    """

    primaries: Mapping[str, _ActiveDataset]
    linkedin_crm: _ActiveDataset | None

    @property
    def ids(self) -> frozenset[int]:
        """Every current dataset id: the active primaries and the effective CRM dataset."""
        ids = {dataset.dataset_id for dataset in self.primaries.values()}
        if self.linkedin_crm is not None:
            ids.add(self.linkedin_crm.dataset_id)
        return frozenset(ids)

    @property
    def primary_ids(self) -> tuple[int, ...]:
        """The active primary dataset ids, in source order."""
        return tuple(
            self.primaries[source].dataset_id for source in SOURCES if source in self.primaries
        )

    def for_source(self, source: str, *, linkedin_scoped: bool) -> _ActiveDataset | None:
        """Return the current dataset a source's evidence resolves to.

        ``google`` and ``linkedin`` resolve to their active primary. ``hubspot`` resolves to
        the effective CRM dataset of the active LinkedIn dataset for LinkedIn-attributed
        evidence (``linkedin_scoped``), and to HubSpot's active primary otherwise: the
        Google-to-CRM link always reads the primary (AAP 0.3.3).
        """
        if source == "hubspot" and linkedin_scoped:
            return self.linkedin_crm
        return self.primaries.get(source)


async def _load_current_datasets(session: AsyncSession) -> _CurrentDatasets:
    """Read the current datasets: at most three statements, independent of any item count."""
    primaries: dict[str, _ActiveDataset] = {
        source: _ActiveDataset.of(row) for source, row in (await active_datasets(session)).items()
    }
    linkedin = primaries.get("linkedin")
    crm: _ActiveDataset | None = None
    if linkedin is not None:
        crm_id = await effective_crm_dataset_id(session, linkedin.dataset_id)
        hubspot = primaries.get("hubspot")
        if crm_id is not None and hubspot is not None and hubspot.dataset_id == crm_id:
            crm = hubspot
        elif crm_id is not None:
            # The companion of a simulated LinkedIn dataset: it shares its parent's state, so
            # it is current exactly while its parent is the active LinkedIn dataset.
            companion = await session.get(SourceDataset, crm_id)
            if companion is not None:
                crm = _ActiveDataset.of(companion)
    return _CurrentDatasets(MappingProxyType(primaries), crm)


async def current_dataset_ids(session: AsyncSession) -> frozenset[int]:
    """Return the ids of every dataset current computations read (AAP 0.3.2, 0.3.3).

    These are the active primary dataset of each source plus the effective CRM dataset of the
    active LinkedIn dataset: the companion while LinkedIn is simulated, HubSpot's active
    primary otherwise. An item whose provenance names any other dataset is historical-only.

    Args:
        session: The caller's session; only read.

    Returns:
        The current dataset ids; empty before any dataset is active.
    """
    return (await _load_current_datasets(session)).ids


def is_current(
    provenance: Sequence[Mapping[str, Any] | ProvenanceEntry], current_ids: frozenset[int]
) -> bool:
    """Return whether every dataset in a frozen provenance is still current (AAP 0.3.2).

    Pure: compares the provenance with ids from :func:`current_dataset_ids`. Fails closed: a
    provenance that cannot be verified is never current, so every action on the item stays
    disabled. Every rule declares the datasets it reads and its evidence freezes one entry per
    dataset read, so an empty provenance is unverifiable, as is any entry that is not a
    complete :class:`~ads_engine.analysis.context.ProvenanceEntry` (all four keys, a known
    ``source``, ``mode`` and ``role``, an integer ``dataset_id``).

    Args:
        provenance: ``evidence.provenance`` as stored (``[{source, dataset_id, mode, role}]``)
            or as :class:`~ads_engine.analysis.context.ProvenanceEntry` values.
        current_ids: The current dataset ids.

    Returns:
        ``True`` only when the provenance is non-empty, every entry decodes, and every
        ``dataset_id`` is in ``current_ids``.

    Example:
        >>> entry = {"source": "google", "dataset_id": 3, "mode": "live", "role": "primary"}
        >>> is_current([entry], frozenset({3}))
        True
        >>> is_current([entry], frozenset({4}))
        False
        >>> is_current([{"source": "google", "dataset_id": 3, "mode": "live"}], frozenset({3}))
        False
        >>> is_current([], frozenset({3}))
        False
    """
    entries = _decode_provenance(provenance)
    if not entries:
        return False
    return all(entry.dataset_id in current_ids for entry in entries)


def _decode_provenance(entries: Iterable[object]) -> tuple[ProvenanceEntry, ...] | None:
    """Decode every provenance entry, or return ``None`` when any one does not decode.

    An entry is a :class:`~ads_engine.analysis.context.ProvenanceEntry` or a mapping that
    :meth:`~ads_engine.analysis.context.ProvenanceEntry.from_json` accepts. Nothing is ever
    dropped: one undecodable entry makes the whole provenance unverifiable.
    """
    decoded: list[ProvenanceEntry] = []
    for entry in entries:
        if isinstance(entry, ProvenanceEntry):
            decoded.append(entry)
            continue
        # Stored provenance is decoded JSON, so an element may be any JSON value at runtime.
        if not isinstance(entry, Mapping):
            return None
        try:
            decoded.append(ProvenanceEntry.from_json(entry))
        except KeyError, TypeError, ValueError:
            return None
    return tuple(decoded)


def _stored_provenance(evidence: object) -> tuple[ProvenanceEntry, ...] | None:
    """Decode a stored item's ``evidence.provenance``, or return ``None`` when unverifiable.

    ``None`` when the evidence is not an object, has no ``provenance`` key, holds a value that
    is not a list, or holds any entry that does not decode (:func:`_decode_provenance`). An
    empty list decodes to ``()``, which :func:`is_current` rejects as well: every stored item
    is source-derived, so neither a missing nor an empty provenance is ever current.
    """
    if not isinstance(evidence, Mapping) or "provenance" not in evidence:
        return None
    provenance: object = evidence["provenance"]
    if not isinstance(provenance, list | tuple):
        return None
    return _decode_provenance(provenance)


def _reporting_provenance(
    evidence: object, verified: tuple[ProvenanceEntry, ...] | None
) -> tuple[ProvenanceEntry, ...]:
    """Return the provenance entries the overlay reports from; never used for currentness.

    ``verified`` is :func:`_stored_provenance` of the same evidence. When the whole
    provenance verifies, its entries are returned; otherwise every entry that decodes on its
    own is kept and the rest are skipped. This view is for reporting only: it chooses the CRM
    scope of the required-dataset checks, the ``data_status_now`` and the simulated-source
    labels, so one malformed companion entry cannot move a LinkedIn-attributed item's
    ``hubspot.*`` checks to HubSpot's primary or drop its "Simulated data (LinkedIn)" label.
    Currentness (``historical``, :func:`is_current`, :func:`item_is_current`) always uses the
    atomic decode, which fails closed: a partially decodable provenance is never current.
    """
    if verified is not None:
        return verified
    if not isinstance(evidence, Mapping):
        return ()
    provenance: object = evidence.get("provenance")
    if not isinstance(provenance, list | tuple):
        return ()
    decodable: list[ProvenanceEntry] = []
    for entry in provenance:
        decoded = _decode_provenance((entry,))
        if decoded is not None:
            decodable.extend(decoded)
    return tuple(decodable)


def _recommendation_is_current(item: Recommendation, current_ids: frozenset[int]) -> bool:
    """Return whether a stored item's provenance is current; an unverifiable one never is.

    Missing, empty and malformed provenance (any entry that does not decode) all fail closed.
    """
    provenance = _stored_provenance(item.evidence)
    return provenance is not None and is_current(provenance, current_ids)


async def item_is_current(session: AsyncSession, recommendation: Recommendation) -> bool:
    """Return whether a stored item still rests on current data (AAP 0.3.2).

    ``POST /recommendations/{id}/feedback`` rejects a write on an item that is not current
    with 409 ``subject_retired``, and decision routes use it for decisions recorded from an
    item. Three statements at most, independent of the item.

    Args:
        session: The caller's session; only read.
        recommendation: The stored item.

    Returns:
        ``True`` when its frozen provenance is non-empty, every entry is a complete
        ``{source, dataset_id, mode, role}`` record, and every dataset in it is current.
        ``False`` when any dataset is not current, or the provenance is missing, empty or
        malformed: an unverifiable item never permits a write.
    """
    return _recommendation_is_current(recommendation, await current_dataset_ids(session))


# ---------------------------------------------------------------------------
# Live state: one statement per table for the whole page
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LiveState:
    """Everything the overlay reads from live tables, loaded once per page.

    Attributes:
        datasets: The current datasets.
        connections: ``source -> (mode, reason)`` from ``source_connection``.
        discrepant: Subjects with an unresolved discrepancy in an active dataset.
        open_discrepancies: How many unresolved discrepancies the active datasets hold.
        freshness: ``dataset_freshness`` rows of the current datasets, keyed
            ``(dataset_id, dataset name)``.
        feedback: The latest active feedback kind per ``(rule_id, entity_type, entity_id)``
            of the page's non-C0 items.
    """

    datasets: _CurrentDatasets
    connections: Mapping[str, tuple[str, str | None]]
    discrepant: frozenset[SubjectRef]
    open_discrepancies: int
    freshness: Mapping[tuple[int, str], Freshness]
    feedback: Mapping[FeedbackKey, str]


def _completeness(value: str) -> Completeness:
    """Narrow a stored ``completeness`` to its literal; an unexpected value is ``unknown``."""
    if value == "complete":
        return "complete"
    if value == "incomplete":
        return "incomplete"
    return "unknown"


async def _load_connections(session: AsyncSession) -> dict[str, tuple[str, str | None]]:
    """Read every ``source_connection`` row as ``source -> (mode, reason)``."""
    result = await session.execute(
        select(SourceConnection.source, SourceConnection.mode, SourceConnection.reason)
    )
    return {source: (mode, reason) for source, mode, reason in result}


async def _load_discrepancies(
    session: AsyncSession, dataset_ids: Sequence[int]
) -> tuple[frozenset[SubjectRef], int]:
    """Read the unresolved discrepancies of the active datasets (P1, AAP 0.5.4.2).

    Returns:
        The discrepant subjects and the number of unresolved discrepancies; a subject with a
        ``status`` and a ``budget`` discrepancy counts twice, as the banner counts rows.
    """
    if not dataset_ids:
        return frozenset(), 0
    result = await session.execute(
        select(Discrepancy.subject_type, Discrepancy.subject_id).where(
            Discrepancy.state.in_(_UNRESOLVED_STATES),
            Discrepancy.dataset_id.in_(dataset_ids),
        )
    )
    rows = result.all()
    return frozenset((subject_type, subject_id) for subject_type, subject_id in rows), len(rows)


async def _load_freshness(
    session: AsyncSession, dataset_ids: frozenset[int]
) -> dict[tuple[int, str], Freshness]:
    """Read the ``dataset_freshness`` rows of the current datasets (AAP 0.3.2)."""
    if not dataset_ids:
        return {}
    result = await session.execute(
        select(
            DatasetFreshness.dataset_id,
            DatasetFreshness.dataset,
            DatasetFreshness.last_attempt_at,
            DatasetFreshness.last_success_at,
            DatasetFreshness.covered_from,
            DatasetFreshness.covered_through,
            DatasetFreshness.completeness,
            DatasetFreshness.reason,
        ).where(DatasetFreshness.dataset_id.in_(sorted(dataset_ids)))
    )
    rows: dict[tuple[int, str], Freshness] = {}
    for (
        dataset_id,
        name,
        last_attempt_at,
        last_success_at,
        covered_from,
        covered_through,
        completeness,
        reason,
    ) in result:
        rows[(dataset_id, name)] = Freshness(
            dataset_id=dataset_id,
            dataset=name,
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at,
            covered_from=covered_from,
            covered_through=covered_through,
            completeness=_completeness(completeness),
            reason=reason,
        )
    return rows


def _feedback_key_filter(keys: Iterable[FeedbackKey]) -> ColumnElement[bool] | None:
    """Match exactly the distinct ``keys``; ``None`` when there are none.

    The keys are grouped by ``(rule_id, entity_type)``. Each group is one conjunction of
    equality on both, ``IS NULL`` for a ``None`` entity type, and ``entity_id = ANY(:ids)``
    over its entity ids as one array parameter, so the parameter count grows with the groups,
    not the items. A group holding a ``None`` entity id adds an ``entity_id IS NULL``
    conjunction: ``NULL`` never equals ``NULL`` in SQL, so equality or a tuple ``IN`` would
    drop that key. Every conjunction constrains the three leading columns of
    ``ix_recommendation_feedback_rule_entity``, so that index also bounds ``until``.
    """
    feedback = RecommendationFeedback
    groups: dict[tuple[str, str | None], set[str | None]] = {}
    for rule_id, entity_type, entity_id in keys:
        groups.setdefault((rule_id, entity_type), set()).add(entity_id)
    clauses: list[ColumnElement[bool]] = []
    for (rule_id, entity_type), entity_ids in groups.items():
        rule_and_type = and_(
            feedback.rule_id == rule_id,
            (
                feedback.entity_type.is_(None)
                if entity_type is None
                else feedback.entity_type == entity_type
            ),
        )
        named = sorted(entity_id for entity_id in entity_ids if entity_id is not None)
        if named:
            clauses.append(
                and_(rule_and_type, feedback.entity_id == any_(literal(named, ARRAY(Text()))))
            )
        if None in entity_ids:
            clauses.append(and_(rule_and_type, feedback.entity_id.is_(None)))
    return or_(*clauses) if clauses else None


async def _load_feedback(
    session: AsyncSession, keys: Iterable[FeedbackKey], now: datetime
) -> dict[FeedbackKey, str]:
    """Read the feedback still in force at ``now`` on the page's exact rule and entity keys.

    One statement over the distinct ``(rule_id, entity_type, entity_id)`` keys
    (:func:`_feedback_key_filter`), served by ``ix_recommendation_feedback_rule_entity``:
    feedback on the same rule but another entity, and expired history, are never read. Only
    ``until`` after ``now`` is in force. When several active rows match one key, the latest
    one written wins: it is the user's most recent choice (for example a dismiss given after
    a snooze).
    """
    key_filter = _feedback_key_filter(keys)
    if key_filter is None:
        return {}
    result = await session.execute(
        select(
            RecommendationFeedback.rule_id,
            RecommendationFeedback.entity_type,
            RecommendationFeedback.entity_id,
            RecommendationFeedback.kind,
        )
        .where(RecommendationFeedback.until > now, key_filter)
        .order_by(RecommendationFeedback.created_at, RecommendationFeedback.id)
    )
    active: dict[FeedbackKey, str] = {}
    for rule_id, entity_type, entity_id, kind in result:
        active[(rule_id, entity_type, entity_id)] = kind
    return active


async def _load_tracking_findings(session: AsyncSession, run_id: int) -> tuple[RunFinding, ...]:
    """Read a run's stored ``conversion_tracking_absent`` findings (P2), in write order."""
    result = await session.scalars(
        select(RunFinding)
        .where(
            RunFinding.run_id == run_id,
            RunFinding.code == FINDING_CONVERSION_TRACKING_ABSENT,
        )
        .order_by(RunFinding.id)
    )
    return tuple(result.all())


# ---------------------------------------------------------------------------
# Per-item overlay (AAP 0.3.2, 0.5.4.2 P1, P3, P4)
# ---------------------------------------------------------------------------


_WINDOW_START_LABEL: Final = "window start"
"""How an incomplete reason names a required range that starts at the run's window start."""


@dataclass(frozen=True, slots=True)
class _RequiredRange:
    """The native dates a required dataset must cover completely for one item (P4).

    Attributes:
        start: The first date: the run's window start, or the earlier start of a rule's
            frozen lookback (:data:`_RULE_LOOKBACKS`).
        end: The last date: the run's window end.
        start_label: How reasons name ``start``: ``"window start"``, or for a lookback
            ``"<rule> <lookback> start"`` (``"K1 baseline start"``).
    """

    start: date
    end: date
    start_label: str = _WINDOW_START_LABEL


@dataclass(frozen=True, slots=True)
class _DatasetCheck:
    """One dataset judged against its current freshness row, for a card and for the page.

    Attributes:
        name: The dataset name.
        dataset_id: The current dataset the name resolves to; ``None`` when its source has no
            current dataset.
        row: The current freshness row; ``None`` when there is none.
        required: The range the dataset must cover completely: the run's window, widened to
            a rule's frozen lookback start where the item's rule reads further back.
        stale: Whether the dataset is stale now (age, or coverage short of the window end).
        incomplete: Whether it is not ``complete`` over :attr:`required`, at either end.
    """

    name: str
    dataset_id: int | None
    row: Freshness | None
    required: _RequiredRange
    stale: bool
    incomplete: bool


def _check_order(check: _DatasetCheck) -> tuple[bool, int, int, str, date, date, str]:
    """Sort key giving page checks one order whatever the item order: banner text is stable.

    By dataset id (a dataset with no current dataset last), then dataset name, then the
    required range, earliest start first.
    """
    return (
        check.dataset_id is None,
        check.dataset_id or 0,
        _DATASET_ORDER.get(check.name, len(DATASETS)),
        check.name,
        check.required.start,
        check.required.end,
        check.required.start_label,
    )


def _frozen_lookback_start(evidence: object, extra_key: str) -> date | None:
    """Return the ``start`` of ``evidence.extra[extra_key]``, or ``None`` when unusable.

    The start is a native date, stored as an ISO ``YYYY-MM-DD`` string (evidence is JSON) or
    held as a ``date``. A missing key, a non-mapping, an unparsable string, a ``datetime``
    (an instant is not a native day) or any other value is unusable.
    """
    extra = evidence.get("extra") if isinstance(evidence, Mapping) else None
    lookback = extra.get(extra_key) if isinstance(extra, Mapping) else None
    value = lookback.get("start") if isinstance(lookback, Mapping) else None
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _frozen_lookback_days(evidence: object, threshold_key: str) -> int | None:
    """Return the item's frozen ``evidence.thresholds[threshold_key]`` when a positive int."""
    thresholds = evidence.get("thresholds") if isinstance(evidence, Mapping) else None
    value = thresholds.get(threshold_key) if isinstance(thresholds, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _days_back(end: date, days: int) -> date:
    """Return the first day of the ``days``-day span ending on ``end``.

    ``date.min`` when the span reaches before the calendar: no coverage starts that early,
    so the range fails closed.
    """
    try:
        return end - timedelta(days=days - 1)
    except OverflowError:
        return date.min


def _required_range(
    item: Recommendation,
    name: str,
    *,
    window_start: date,
    window_end: date,
    lookback_days: Mapping[str, int],
) -> _RequiredRange:
    """Return the range a required dataset must cover completely for one item (P4).

    The run's window, widened to an earlier start where the item's rule reads ``name``
    further back (:data:`_RULE_LOOKBACKS`): a K1 item's ``google.metrics_daily`` must cover
    from its frozen ``evidence.extra.baseline_window.start``. When that start is missing or
    malformed the range fails closed to the lookback's length, counted back from the window
    end: the item's frozen ``evidence.thresholds["k1.baseline_days"]`` when it is a positive
    integer, else the configured ``lookback_days`` value. It never falls back to the window
    start. A lookback that starts on or after the window start leaves the window as is.

    Args:
        item: The stored item.
        name: One of the item's required dataset names.
        window_start: The run's window start.
        window_end: The run's window end; every required range ends there.
        lookback_days: The configured length per rule of :data:`_LOOKBACK_DAYS_KEYS`.
    """
    lookbacks = _RULE_LOOKBACKS.get(item.rule_id)
    extra_key = None if lookbacks is None else lookbacks.get(name)
    if extra_key is None:
        return _RequiredRange(window_start, window_end)
    start = _frozen_lookback_start(item.evidence, extra_key)
    if start is None:
        days = _frozen_lookback_days(item.evidence, _LOOKBACK_DAYS_KEYS[item.rule_id])
        start = _days_back(window_end, lookback_days[item.rule_id] if days is None else days)
    if start >= window_start:
        return _RequiredRange(window_start, window_end)
    lookback = extra_key.removesuffix("_window").replace("_", " ")
    return _RequiredRange(start, window_end, f"{item.rule_id} {lookback} start")


def _check_dataset(
    name: str,
    dataset: _ActiveDataset | None,
    freshness: Mapping[tuple[int, str], Freshness],
    *,
    now: datetime,
    stale_after: timedelta,
    required: _RequiredRange,
) -> _DatasetCheck:
    """Judge one required dataset now (P4), from its current dataset's freshness row.

    ``required`` is the item's required range for this dataset (:func:`_required_range`).
    """
    if dataset is None:
        dataset_id, row = None, None
    else:
        dataset_id, row = dataset.dataset_id, freshness.get((dataset.dataset_id, name))
    return _judge(name, dataset_id, row, now=now, stale_after=stale_after, required=required)


def _judge(
    name: str,
    dataset_id: int | None,
    row: Freshness | None,
    *,
    now: datetime,
    stale_after: timedelta,
    required: _RequiredRange,
) -> _DatasetCheck:
    """Judge one dataset against its required range now (P4); cards and page share it.

    Stale follows :meth:`~ads_engine.analysis.context.Freshness.is_stale` (age, or coverage
    short of the window end) and incomplete
    :meth:`~ads_engine.analysis.context.Freshness.is_complete_over`, the shared definitions
    in ``context.py``: a dataset is incomplete when it is not ``complete`` over the whole
    required range, at both ends (AAP 0.3.2). The range is the run's window, or for a rule
    that reads further back (a K1 item's ``google.metrics_daily``) the window widened to the
    rule's frozen lookback start, so coverage starting after either start, or ending before
    the window end, is incomplete. A dataset with no freshness row, or no current dataset at
    all, has never succeeded: it is stale and incomplete, never silently fresh.
    """
    if row is None:
        return _DatasetCheck(name, dataset_id, None, required, stale=True, incomplete=True)
    return _DatasetCheck(
        name,
        dataset_id,
        row,
        required,
        stale=row.is_stale(now=now, stale_after=stale_after, window_end=required.end),
        incomplete=not row.is_complete_over(window_start=required.start, window_end=required.end),
    )


def _stale_flag(checks: Sequence[_DatasetCheck], window_end: date) -> Flag:
    """Build the ``stale`` flag: "Data as of …", naming any coverage shortfall."""
    never: list[str] = []
    successes: list[datetime] = []
    shortfalls: list[str] = []
    for check in checks:
        row = check.row
        if row is None or row.last_success_at is None:
            never.append(check.name)
            continue
        successes.append(row.last_success_at)
        if check.name in FULL_STATE_DATASETS:
            # A full-state read records no date range, so it has no coverage to fall short.
            continue
        if row.covered_through is None:
            shortfalls.append(f"{check.name} covers no dates yet")
        elif row.covered_through < window_end:
            shortfalls.append(f"{check.name} covers through {row.covered_through.isoformat()}")
    if never:
        message = f"Data as of: no successful sync of {', '.join(_ordered_names(never))}"
    else:
        message = f"Data as of {_format_instant(min(successes))}"
    if shortfalls:
        message = f"{message}; {'; '.join(shortfalls)}"
    return Flag(FLAG_STALE, message)


def _incomplete_reasons(check: _DatasetCheck) -> tuple[str, ...]:
    """Say why an incomplete dataset is not ``complete`` over its required range.

    Every shortfall is named once, in this order: a row that is not ``complete`` gives its
    recorded reason (else its completeness); a dated dataset also names coverage starting
    after the required start ("covers from 2026-09-04, after the K1 baseline start
    2026-07-04", or "after the window start …" for the plain window) and coverage ending
    before the window end ("covers through 2026-09-30, before the window end 2026-10-01").
    A ``complete`` row reaches this only through a range shortfall
    (:meth:`~ads_engine.analysis.context.Freshness.is_complete_over`), so the text never
    reads "(complete)". Called only for checks judged incomplete, so never empty.
    """
    row = check.row
    if row is None:
        return ("no freshness record",)
    reasons: list[str] = []
    if row.completeness != "complete":
        reasons.append(row.reason or row.completeness)
    if row.dataset in FULL_STATE_DATASETS:
        # A full-state read records no date range; its completeness alone decides.
        return tuple(reasons)
    required = check.required
    if row.covered_from is None and row.covered_through is None:
        reasons.append("covers no dates")
        return tuple(reasons)
    if row.covered_from is None:
        reasons.append("records no coverage start")
    elif row.covered_from > required.start:
        reasons.append(
            f"covers from {row.covered_from.isoformat()}, "
            f"after the {required.start_label} {required.start.isoformat()}"
        )
    if row.covered_through is None:
        reasons.append("records no coverage end")
    elif row.covered_through < required.end:
        reasons.append(
            f"covers through {row.covered_through.isoformat()}, "
            f"before the window end {required.end.isoformat()}"
        )
    return tuple(reasons)


def _incomplete_flag(checks: Sequence[_DatasetCheck]) -> Flag:
    """Build the ``incomplete`` flag, naming each dataset and every reason it is incomplete."""
    parts = [f"{check.name} ({'; '.join(_incomplete_reasons(check))})" for check in checks]
    return Flag(FLAG_INCOMPLETE, f"Incomplete: {'; '.join(parts)}")


def _simulated_flag(status: str, simulated_sources: Sequence[str]) -> Flag:
    """Build the ``simulated`` flag for a ``simulated`` or ``mixed`` item (P3)."""
    labels = _source_list(simulated_sources)
    if status == DATA_MIXED:
        detail = f" ({labels} simulated)" if labels else ""
        return Flag(FLAG_SIMULATED, f"Joins live and simulated data{detail}")
    return Flag(FLAG_SIMULATED, f"Simulated data ({labels})" if labels else "Simulated data")


def _subject_ref(value: object) -> SubjectRef | None:
    """Decode one ``{type, id}`` subject, or ``None`` when it is not a decision subject."""
    if not isinstance(value, Mapping):
        return None
    subject_type = value.get("type")
    subject_id = value.get("id")
    if not isinstance(subject_type, str) or subject_type not in _SUBJECT_TYPES:
        return None
    if isinstance(subject_id, bool) or not isinstance(subject_id, int):
        return None
    return (subject_type, subject_id)


def _dependent_subjects(item: Recommendation) -> frozenset[SubjectRef]:
    """Return the subjects whose unresolved discrepancy holds the item (P1).

    These are the subjects ``precedence.py`` froze in ``evidence.extra.depends_on`` (the
    campaign itself, its budget, the shared budget containing it, the campaigns behind a
    keyword, creative or white paper row) plus the item's own ``decision_subject``: a
    decision recorded from the item would write that subject, so a discrepancy on it always
    holds the item.
    """
    candidates: list[object] = [item.decision_subject]
    evidence: object = item.evidence
    extra = evidence.get("extra") if isinstance(evidence, Mapping) else None
    depends_on = extra.get("depends_on") if isinstance(extra, Mapping) else None
    if isinstance(depends_on, list | tuple):
        candidates.extend(depends_on)
    subjects = {ref for ref in (_subject_ref(value) for value in candidates) if ref is not None}
    return frozenset(subjects)


def _dataset_source(name: str) -> str:
    """Return the source prefix of a dataset name (``google.metrics_daily`` → ``google``)."""
    return name.split(".", 1)[0]


def _item_view(
    item: Recommendation,
    live: _LiveState,
    *,
    now: datetime,
    stale_after: timedelta,
    window_start: date,
    window_end: date,
    lookback_days: Mapping[str, int],
) -> tuple[ItemOverlayView, tuple[_DatasetCheck, ...]]:
    """Compute one stored item's read-time state; pure over the preloaded live state.

    Returns the view together with the item's required-dataset checks, so the page-level
    lists and banners aggregate exactly the verdicts the card shows. Each required dataset
    is judged over its required range (:func:`_required_range`): the run's window, widened
    for a K1 item's ``google.metrics_daily`` to its frozen baseline start, with
    ``lookback_days`` (the configured ``k1.baseline_days``) as the fail-closed length when
    that start is missing or malformed.

    * ``historical`` when any frozen provenance dataset is no longer current, or when the
      provenance is unverifiable (missing, empty, or with any entry that does not decode);
      the item is then held with ``HISTORICAL_REASON`` and every action is disabled
      (AAP 0.3.2). Only the atomic decode decides this.
    * Otherwise held when it was held at compute time, or (non-C0 only) when a subject it
      depends on has a discrepancy that is unresolved now (P1 at read time).
    * The CRM scope of the checks, ``data_status_now`` and the simulated-source labels read
      the reporting view (:func:`_reporting_provenance`): the verified entries, or the
      individually decodable ones of an unverifiable provenance. The item is
      LinkedIn-attributed when a reporting entry is LinkedIn's or it requires a
      ``linkedin.*`` dataset; its ``hubspot.*`` datasets are then those of the effective CRM
      dataset (the companion while LinkedIn is simulated), else HubSpot's primary (AAP
      0.5.4.1, 0.3.3).
    * ``data_status_now`` from the current modes of the reporting sources: all simulated
      → ``simulated``, mixed → ``mixed``, else ``live``, which becomes ``stale`` when a
      required dataset is stale now (P3, P4). With no decodable entry the frozen
      ``simulated``/``mixed`` status stands.
    * ``flags_now``: current ``simulated``, ``stale``, ``incomplete`` and, for non-C0 items
      while discrepancies are open, ``unverified_discrepancies`` with the live count.
    * ``hidden_by``: the active feedback on the item's rule and entity; C0 is exempt.
    """
    is_c0 = item.rule_id == C0
    stored = _stored_provenance(item.evidence)
    # Fails closed: an unverifiable provenance (None) or an empty one is never current.
    historical = stored is None or not is_current(stored, live.datasets.ids)
    # Reporting only, never currentness: the entries that decode, even when the whole
    # provenance does not verify, so a malformed entry cannot change the CRM scope or labels.
    entries = _reporting_provenance(item.evidence, stored)
    # LinkedIn-attributed evidence reads the effective CRM dataset of the LinkedIn dataset in
    # use; any other HubSpot evidence (the Google-to-CRM link) reads HubSpot's primary. A
    # LinkedIn-attributed item always reads a linkedin.* dataset; Google-to-CRM never does.
    linkedin_scoped = any(entry.source == "linkedin" for entry in entries) or any(
        _dataset_source(name) == "linkedin" for name in item.required_datasets
    )

    checks = tuple(
        _check_dataset(
            name,
            live.datasets.for_source(_dataset_source(name), linkedin_scoped=linkedin_scoped),
            live.freshness,
            now=now,
            stale_after=stale_after,
            required=_required_range(
                item,
                name,
                window_start=window_start,
                window_end=window_end,
                lookback_days=lookback_days,
            ),
        )
        for name in dict.fromkeys(item.required_datasets)
    )
    stale_checks = [check for check in checks if check.stale]
    incomplete_checks = [check for check in checks if check.incomplete]

    simulated_sources: list[str] = []
    for entry in entries:
        current = live.datasets.for_source(entry.source, linkedin_scoped=linkedin_scoped)
        mode = entry.mode if current is None else current.mode
        if mode == DATA_SIMULATED:
            simulated_sources.append(entry.source)
    if not entries:
        # No entry decodes, so no usable mode is named: the item (already historical) keeps
        # its frozen simulated/mixed status.
        base = item.data_status if item.data_status in (DATA_SIMULATED, DATA_MIXED) else DATA_LIVE
    elif len(simulated_sources) == len(entries):
        base = DATA_SIMULATED
    elif simulated_sources:
        base = DATA_MIXED
    else:
        base = DATA_LIVE
    data_status_now = DATA_STALE if base == DATA_LIVE and stale_checks else base

    flags: list[Flag] = []
    if base in (DATA_SIMULATED, DATA_MIXED):
        flags.append(_simulated_flag(base, simulated_sources))
    if stale_checks:
        flags.append(_stale_flag(stale_checks, window_end))
    if incomplete_checks:
        flags.append(_incomplete_flag(incomplete_checks))
    if not is_c0 and live.open_discrepancies > 0:
        flags.append(
            Flag(FLAG_UNVERIFIED_DISCREPANCIES, unverified_message(live.open_discrepancies))
        )

    held_reason: str | None
    if historical:
        held_reason = HISTORICAL_REASON
    elif item.held:
        # Frozen holds are never lifted at read time; the overlay only adds holds.
        held_reason = item.held_reason or HELD_DISCREPANCY_REASON
    elif not is_c0 and not _dependent_subjects(item).isdisjoint(live.discrepant):
        held_reason = HELD_DISCREPANCY_REASON
    else:
        held_reason = None

    hidden_by = (
        None if is_c0 else live.feedback.get((item.rule_id, item.entity_type, item.entity_id))
    )
    view = ItemOverlayView(
        held_now=held_reason is not None,
        held_reason_now=held_reason,
        historical=historical,
        data_status_now=data_status_now,
        flags_now=tuple(flags),
        hidden_by=hidden_by,
    )
    return view, checks


# ---------------------------------------------------------------------------
# Banners (AAP 0.5.5 fixed order; built from the run plus the overlay)
# ---------------------------------------------------------------------------


def _banner(code: str, message: str, count: int | None = None) -> BannerView:
    """Build a banner with its code's tone."""
    return BannerView(code=code, tone=BANNER_TONES[code], message=message, count=count)


def _discrepancy_banner(count: int) -> BannerView | None:
    """Unresolved discrepancies, counted live: the only ``alert`` banner (P1)."""
    if count <= 0:
        return None
    noun = "discrepancy" if count == 1 else "discrepancies"
    return _banner(
        BANNER_OPEN_DISCREPANCIES,
        f"{count} {noun} to resolve before acting on other recommendations",
        count,
    )


def _finding_source(finding: RunFinding) -> str | None:
    """Return the source a stored tracking finding names, from its evidence or entity."""
    for holder in (finding.evidence, finding.entity):
        if isinstance(holder, Mapping):
            source = holder.get("source")
            if isinstance(source, str) and source in SOURCES:
                return source
    return None


def _tracking_banner(run: AnalysisRun, findings: Sequence[RunFinding]) -> BannerView | None:
    """The run's stored ``conversion_tracking_absent`` findings, as one banner (P2)."""
    relevant = [
        finding
        for finding in findings
        if finding.run_id == run.id and finding.code == FINDING_CONVERSION_TRACKING_ABSENT
    ]
    if not relevant:
        return None
    sources = {source for source in map(_finding_source, relevant) if source is not None}
    if sources:
        return _banner(
            BANNER_TRACKING_ABSENT,
            f"No conversions or leads recorded for {_source_list(sources)} — {_TRACKING_ADVICE}",
            len(sources),
        )
    messages = list(dict.fromkeys(finding.message for finding in relevant))
    return _banner(BANNER_TRACKING_ABSENT, " ".join(messages))


def _simulated_banner(live: _LiveState) -> BannerView | None:
    """Every source whose active dataset is simulated, each with its reason (P3)."""
    parts: list[str] = []
    for source in SOURCES:
        dataset = live.datasets.primaries.get(source)
        if dataset is None or dataset.mode != DATA_SIMULATED:
            continue
        connection = live.connections.get(source)
        connection_reason = (
            connection[1] if connection is not None and connection[0] == DATA_SIMULATED else None
        )
        reason = dataset.reason or connection_reason or _DEFAULT_SIMULATED_REASON
        parts.append(f"{_SOURCE_LABELS[source]} ({reason})")
    if not parts:
        return None
    return _banner(BANNER_SIMULATED_SOURCES, f"Simulated data: {'; '.join(parts)}", len(parts))


def _stale_banner(checks: Sequence[_DatasetCheck]) -> BannerView | None:
    """The datasets that are stale now, each with its last success."""
    names = _ordered_names(check.name for check in checks)
    if not names:
        return None
    parts: list[str] = []
    for name in names:
        group = [check.row for check in checks if check.name == name]
        successes = [
            row.last_success_at
            for row in group
            if row is not None and row.last_success_at is not None
        ]
        if len(successes) < len(group):
            parts.append(f"{name} (no successful sync)")
        else:
            parts.append(f"{name} (last success {_format_instant(min(successes))})")
    return _banner(BANNER_STALE_DATASETS, f"Stale data: {', '.join(parts)}", len(names))


def _incomplete_banner(checks: Sequence[_DatasetCheck]) -> BannerView | None:
    """The datasets that are not complete over a required range, each with every reason.

    ``checks`` holds every incomplete check on the page, in :func:`_check_order`; each
    distinct reason of a dataset is named once, in that order (an end shortfall that several
    items share appears once beside a K1 item's baseline-start shortfall), and the count is
    the number of distinct dataset names.
    """
    names = _ordered_names(check.name for check in checks)
    if not names:
        return None
    parts: list[str] = []
    for name in names:
        reasons = dict.fromkeys(
            reason
            for check in checks
            if check.name == name
            for reason in _incomplete_reasons(check)
        )
        parts.append(f"{name} ({'; '.join(reasons)})")
    return _banner(BANNER_INCOMPLETE_DATASETS, f"Incomplete data: {', '.join(parts)}", len(names))


def _short_window_banner(run: AnalysisRun, trusted_min_days: int) -> BannerView | None:
    """The run's window against the configured trusted minimum (P5)."""
    days = (run.window_end - run.window_start).days + 1
    if days >= trusted_min_days:
        return None
    return _banner(
        BANNER_SHORT_WINDOW,
        f"Window of {days} days is below the {trusted_min_days}-day trusted minimum",
    )


def _banners(
    run: AnalysisRun,
    live: _LiveState,
    findings: Sequence[RunFinding],
    *,
    stale_checks: Sequence[_DatasetCheck],
    incomplete_checks: Sequence[_DatasetCheck],
    trusted_min_days: int,
) -> tuple[BannerView, ...]:
    """Build every banner that applies, in ``BANNER_ORDER`` (AAP 0.5.5)."""
    candidates = (
        _discrepancy_banner(live.open_discrepancies),
        _tracking_banner(run, findings),
        _simulated_banner(live),
        _stale_banner(stale_checks),
        _incomplete_banner(incomplete_checks),
        _short_window_banner(run, trusted_min_days),
    )
    built = {banner.code: banner for banner in candidates if banner is not None}
    return tuple(built[code] for code in BANNER_ORDER if code in built)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def overlay(
    session: AsyncSession,
    run: AnalysisRun,
    items: Sequence[Recommendation],
    now: datetime,
    *,
    findings: Sequence[RunFinding] | None = None,
    thresholds: Thresholds,
) -> OverlayResult:
    """Compute the current-state overlay of one page of a stored run (AAP 0.3.2, 0.5.3).

    Reads live tables once each, never per item, and never writes: the run's stored
    evidence, flags and holds stay exactly as recorded, and the overlay only adds holds,
    flags and hidden state beside them.

    Args:
        session: The caller's session; only read (no flush, no commit).
        run: The run being shown, default or custom.
        items: The run's stored items (every one must belong to ``run``).
        now: The read time (tz-aware): feedback in force, staleness and ``computed_at``.
        findings: The run's stored findings, when the caller already loaded them; only the
            ``conversion_tracking_absent`` ones of this run are used. ``None`` reads them.
        thresholds: Required: the loaded configuration (``sources.stale_after_hours``,
            ``window.trusted_min_days``, and ``k1.baseline_days`` for a K1 item whose frozen
            baseline window is unusable), the cached instance the caller already holds, as
            the API injects ``app.state.thresholds`` through
            :data:`~ads_engine.api.deps.ThresholdsDep`. It is never read from disk here, so
            the async read path does no blocking file I/O.

    Returns:
        The page-level overlay, one :class:`ItemOverlayView` per item keyed by its id, and the
        banners in the fixed AAP 0.5.5 order.

    Raises:
        TypeError: When ``now`` is not a ``datetime``, or ``thresholds`` is not a loaded
            :class:`~ads_engine.analysis.thresholds.Thresholds` (``None`` included).
        ValueError: When ``now`` is naive or an item belongs to another run.
    """
    now = _require_aware(now, "now")
    config = _require_thresholds(thresholds)
    foreign = sorted({item.run_id for item in items if item.run_id != run.id})
    if foreign:
        raise ValueError(f"overlay of run {run.id} was given items of runs {foreign}")
    stale_after = timedelta(hours=config.int("sources.stale_after_hours"))
    window_start = run.window_start
    window_end = run.window_end
    lookback_days = {rule_id: config.int(key) for rule_id, key in _LOOKBACK_DAYS_KEYS.items()}

    datasets = await _load_current_datasets(session)
    discrepant, open_count = await _load_discrepancies(session, datasets.primary_ids)
    live = _LiveState(
        datasets=datasets,
        connections=MappingProxyType(await _load_connections(session)),
        discrepant=discrepant,
        open_discrepancies=open_count,
        freshness=MappingProxyType(await _load_freshness(session, datasets.ids)),
        feedback=MappingProxyType(
            await _load_feedback(
                session,
                (
                    (item.rule_id, item.entity_type, item.entity_id)
                    for item in items
                    if item.rule_id != C0
                ),
                now,
            )
        ),
    )
    stored_findings = (
        await _load_tracking_findings(session, run.id) if findings is None else tuple(findings)
    )

    # The page aggregates the very checks its cards show (including required datasets with no
    # freshness row or no current dataset, and those of historical items), plus every current
    # freshness row judged over the run's window. A dataset is listed stale or incomplete when
    # ANY of those checks is, so one item's complete check never masks another item's wider
    # required range (a K1 baseline) on the same dataset.
    views: dict[int, ItemOverlayView] = {}
    assessments: dict[_DatasetCheck, None] = {}
    for item in items:
        item_view, item_checks = _item_view(
            item,
            live,
            now=now,
            stale_after=stale_after,
            window_start=window_start,
            window_end=window_end,
            lookback_days=lookback_days,
        )
        views[item.id] = item_view
        assessments.update(dict.fromkeys(item_checks))
    run_window = _RequiredRange(window_start, window_end)
    for (dataset_id, name), row in live.freshness.items():
        check = _judge(name, dataset_id, row, now=now, stale_after=stale_after, required=run_window)
        assessments[check] = None
    page_checks = sorted(assessments, key=_check_order)
    stale_checks = [check for check in page_checks if check.stale]
    incomplete_checks = [check for check in page_checks if check.incomplete]
    source_modes: dict[str, str | None] = {}
    for source in SOURCES:
        dataset = datasets.primaries.get(source)
        source_modes[source] = None if dataset is None else dataset.mode
    view = OverlayView(
        open_discrepancies=open_count,
        source_modes=source_modes,
        stale_datasets=tuple(check.name for check in stale_checks),
        incomplete_datasets=tuple(check.name for check in incomplete_checks),
        computed_at=now,
    )
    banners = _banners(
        run,
        live,
        stored_findings,
        stale_checks=stale_checks,
        incomplete_checks=incomplete_checks,
        trusted_min_days=config.trusted_min_days,
    )
    return OverlayResult(overlay=view, items=MappingProxyType(views), banners=banners)
