"""``GET /recommendations`` and ``POST /recommendations/{id}/feedback``: the five sections.

AAP 0.5.3 (``GET /recommendations?run_id=`` returning ``RecommendationsPage``;
``POST /recommendations/{id}/feedback``; "Where outputs are frozen"), 0.3.2 (the current-state
overlay; historical-only items; a feedback write enqueues a default-window ``analysis_request``),
0.5.2 (``recommendation_feedback``: suppression for ``feedback.dismiss_days``,
``feedback.snooze_days`` or ``feedback.done_days``, defaults 30, 7 and 30 days; C0 exempt),
0.5.5 (the ``/actions`` page) and 0.1.1 (the five sections render from precomputed runs in under
2 s). Paths are relative to ``/api/v1``; ``api/app.py`` includes :data:`router` with that prefix
and with the CSRF dependency, so the ``POST`` needs ``X-Requested-With: ads-engine`` without this
module repeating the check.

**No business rules live here.** Everything a card shows comes from one of two places:

* **Frozen run outputs**, returned exactly as the runner stored them, never re-derived:
  sections, groups, ranks, actions, reasons, evidence, flags, holds and decision subjects of
  ``recommendation``, plus ``run_finding``, ``run_withheld`` and ``run_suppression``. No rule is
  evaluated and no evidence is rewritten on the read path.
* **The read-time overlay** (:func:`ads_engine.analysis.overlay.overlay`): live holds from
  discrepancies detected after the run, historical-only marking, ``flags_now``,
  ``data_status_now``, ``hidden_by``, the page-level ``Overlay`` and the banners in their fixed
  order.

What this module adds is only the arrangement of those rows into the page:

* **Sections**: always all five, in ``SECTION_ORDER`` and titled from ``SECTION_TITLES``, an
  empty section included (the UI shows its ``EmptyState``).
* **Groups**: every static group of the section (``GROUP_ORDER``, titled from
  ``GROUP_TITLES``) is always present, empty or not, so the UI has each group to render: the
  Block / Expand / Review groups behind the Keywords tabs, and the discrepancy group
  (``#group-campaigns-discrepancies``) the discrepancy banner links to. The banner counts
  discrepancies at read time, so it can link there while the run left the group empty. Whether
  an empty group, and so its anchor, is in the page is the web section's job: ``SectionFrame``
  leaves it out unless its ``renderEmptyGroup`` hook supplies content, which
  ``CampaignsSection`` must do for the discrepancy group.
  Dynamic groups follow: ``campaign:{id}`` (one creative block per campaign, titled with the
  campaign's name) and ``cohort:{CURRENCY}`` (one white-paper table per currency, titled with
  the currency), ordered by their lowest item rank, which is the order precedence ranked them
  in. Any other key is kept, defensively, after them, titled with the key, so no stored item is
  ever dropped. Items keep their stored rank order; held items are not moved, because the UI
  collapses them.
* **Entity**: ``{source, type, id, name}`` with ``source``, ``type`` and the platform id taken
  from the stored key ``{source}:{type}:{external_id}`` and the display name from
  ``evidence.extra.entity``, where the runner stores it (``recommendation`` has no name column).
* **Flag messages**: a stored flag's message is the one the run recorded in
  ``evidence.extra.flag_messages``; a current flag's message is the overlay's. A fixed label
  (:data:`FLAG_LABELS`) stands in only when no recorded message exists.

Performance: the page issues exactly four set-based loads (recommendations served by
``ix_recommendation_run_section_rank``, findings, withheld patterns and suppression counts) plus
the run lookup and the overlay's own bounded statement set, never one statement per item, and
groups the rows in one pass in memory.

Feedback (``POST``) writes one ``recommendation_feedback`` row copying the item's rule and
entity key, suppressing the same rule and entity in later runs until ``until`` (``feedback.*_days``
from ``config/thresholds.yaml``). It is refused with 422 ``feedback_not_allowed`` on a C0 item and
409 ``subject_retired`` on an item computed from data that has since been replaced. It never
writes a decision and never touches a discrepancy: "Mark done" is feedback only (AAP 0.5.5).
It is idempotent per item: requests for one item are serialised on its row, and a request that
repeats the item's most recent feedback still in force (same kind and note), such as the second
half of a double click, is answered with that feedback and writes nothing.

The module imports nothing from ``connectors``, ``sync`` or ``simulation``, and its logs carry
ids, rule ids and kinds only, never the user's email or the note.

Example::

    GET /api/v1/recommendations            (latest succeeded default run)
    GET /api/v1/recommendations?run_id=42  (any run by id, for example a custom window)
    200 {"run": {...}, "overlay": {...}, "banners": [...],
         "sections": [{"key": "keywords", "title": "Keywords to adjust",
                       "groups": [{"key": "block", "title": "...", "items": [...]}, ...],
                       "findings": [...], "withheld": [...], "suppressed": {...}}, ...]}

    POST /api/v1/recommendations/7/feedback   (X-Requested-With: ads-engine)
    {"kind": "snooze", "note": "Revisit after the launch"}
    201 {"id": 3, "recommendation_id": 7, "rule_id": "K1", "kind": "snooze",
         "until": "2026-10-13T12:00:00Z", ...}
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ads_engine.analysis.context import (
    C0,
    ENTITY_CAMPAIGN,
    FLAG_ATTRIBUTION_FLOOR,
    FLAG_CODES,
    FLAG_CREATIVE_FLOOR,
    FLAG_DEAL_FLOOR,
    FLAG_INCOMPLETE,
    FLAG_MEETING_TIME_PROXY,
    FLAG_NO_CRM_LINK,
    FLAG_NOT_DISTINGUISHABLE,
    FLAG_PIPELINE_UNAVAILABLE,
    FLAG_SEARCH_TERM_COVERAGE,
    FLAG_SHARED_BUDGET,
    FLAG_SHORT_WINDOW,
    FLAG_SIMULATED,
    FLAG_STALE,
    FLAG_UNKNOWN_CRM_EVIDENCE,
    FLAG_UNVERIFIED_DISCREPANCIES,
    GROUP_ORDER,
    GROUP_TITLES,
    HISTORICAL_REASON,
    SECTION_ORDER,
    SECTION_TITLES,
    Flag,
    cohort_group_currency,
    creative_group_campaign,
)
from ads_engine.analysis.overlay import (
    ItemOverlayView,
    OverlayResult,
    item_is_current,
    item_provenance,
    overlay,
)
from ads_engine.analysis.runner import get_run, latest_default_run
from ads_engine.api import schemas
from ads_engine.api.deps import (
    ApiError,
    NowDep,
    SessionDep,
    SettingsDep,
    ThresholdsDep,
    UserDep,
    conflict,
    not_found,
)
from ads_engine.db.models.analysis import (
    FEEDBACK_KINDS,
    AnalysisRun,
    Recommendation,
    RecommendationFeedback,
    RunFinding,
    RunSuppression,
    RunWithheld,
)
from ads_engine.db.models.ops import SOURCES
from ads_engine.db.repositories import enqueue_analysis_request, lock_dataset
from ads_engine.logging import get_logger

__all__ = [
    "FEEDBACK_DAYS_KEYS",
    "FLAG_LABELS",
    "get_recommendations",
    "post_feedback",
    "router",
]

router = APIRouter(tags=["recommendations"])
"""The ``recommendations`` routes, included by ``create_app`` under ``/api/v1``."""

_log = get_logger(__name__)

FLAG_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        FLAG_SIMULATED: "Simulated data",
        FLAG_STALE: "Stale",
        FLAG_INCOMPLETE: "Incomplete",
        FLAG_SHORT_WINDOW: "Short window",
        FLAG_UNVERIFIED_DISCREPANCIES: "Unverified while discrepancies open",
        FLAG_ATTRIBUTION_FLOOR: "Floor (attribution)",
        FLAG_CREATIVE_FLOOR: "Floor (creative)",
        FLAG_DEAL_FLOOR: "Floor (deal)",
        FLAG_UNKNOWN_CRM_EVIDENCE: "Unknown CRM evidence",
        FLAG_MEETING_TIME_PROXY: "Meeting time proxy",
        FLAG_PIPELINE_UNAVAILABLE: "Pipeline unavailable",
        FLAG_NO_CRM_LINK: "No CRM link",
        FLAG_NOT_DISTINGUISHABLE: "Not distinguishable",
        FLAG_SEARCH_TERM_COVERAGE: "Low search-term coverage",
        FLAG_SHARED_BUDGET: "Shared budget",
    }
)
"""The badge label of every flag code, in the AAP 0.5.5 wording.

Used only when a flag carries no recorded message: the runner stores each flag's own message
(for example "Data as of Oct 1, 2026, 3:10 AM UTC") in ``evidence.extra.flag_messages``, and the
overlay gives every current flag its message, so this table is the fallback for rows written
without one."""

FEEDBACK_DAYS_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "dismiss": "feedback.dismiss_days",
        "snooze": "feedback.snooze_days",
        "done": "feedback.done_days",
    }
)
"""The ``config/thresholds.yaml`` key holding each feedback kind's suppression length in days."""

# Both tables must name exactly the vocabularies they label; checked once at import with
# `if … raise`, so the check also runs under `python -O`.
if set(FLAG_LABELS) != set(FLAG_CODES):  # pragma: no cover - import-time contract guard
    raise RuntimeError(
        f"FLAG_LABELS {sorted(FLAG_LABELS)} must label exactly FLAG_CODES {sorted(FLAG_CODES)}"
    )
if set(FEEDBACK_DAYS_KEYS) != set(FEEDBACK_KINDS):  # pragma: no cover - import-time guard
    raise RuntimeError(
        f"FEEDBACK_DAYS_KEYS {sorted(FEEDBACK_DAYS_KEYS)} must cover FEEDBACK_KINDS "
        f"{sorted(FEEDBACK_KINDS)}"
    )

_FEEDBACK_REASON: Final = "feedback"
"""The ``analysis_request.reason`` of the rerun a feedback write enqueues (AAP 0.3.2)."""

_C0_FEEDBACK_MESSAGE: Final = (
    "Discrepancies cannot be dismissed, snoozed or marked done; resolve them instead"
)
"""The 422 message for feedback on a C0 item, which is exempt (AAP 0.5.4.2)."""

_EXTRA_ENTITY: Final = "entity"
"""The ``evidence.extra`` key where the runner stores the item's ``{source, type, id, name}``."""

_EXTRA_FLAG_MESSAGES: Final = "flag_messages"
"""The ``evidence.extra`` key where the runner stores ``{flag code: message}``."""

_EXTRA_CAMPAIGN: Final = "campaign"
"""The ``evidence.extra`` key where creative items store their campaign's summary."""

_KEY_SEPARATOR: Final = ":"
"""The separator of the stored entity key ``{source}:{type}:{external_id}``."""


# ---------------------------------------------------------------------------
# Frozen item fields
# ---------------------------------------------------------------------------


def _extra(evidence: object) -> Mapping[str, Any]:
    """Return a stored evidence block's ``extra`` object, or an empty mapping when absent.

    Args:
        evidence: The ``recommendation.evidence`` ``jsonb`` value.

    Returns:
        ``evidence["extra"]`` when both are JSON objects; otherwise ``{}``.
    """
    if not isinstance(evidence, Mapping):
        return {}
    extra = evidence.get("extra")
    return extra if isinstance(extra, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _flag(code: str, message: object = None) -> dict[str, Any]:
    """Return one ``Flag`` as ``{code, message}``.

    Args:
        code: The flag code.
        message: The message recorded with the flag (by the run or the overlay); a missing or
            blank one falls back to :data:`FLAG_LABELS`, and an unknown code to the code
            itself, which the ``Flag`` schema then rejects as outside the contract.

    Returns:
        The flag, ready for ``schemas.Flag`` validation.
    """
    return {"code": code, "message": _text(message) or FLAG_LABELS.get(code, code)}


def _stored_flags(row: Recommendation, extra: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the flags the run recorded on ``row``, each with the message the run recorded.

    Args:
        row: The stored item.
        extra: Its ``evidence.extra``.

    Returns:
        One flag per code in ``row.flags``, in stored order.
    """
    recorded = extra.get(_EXTRA_FLAG_MESSAGES)
    messages: Mapping[str, Any] = recorded if isinstance(recorded, Mapping) else {}
    return [_flag(code, messages.get(code)) for code in row.flags]


def _current_flags(flags: Sequence[Flag]) -> list[dict[str, Any]]:
    return [_flag(flag.code, flag.message) for flag in flags]


def _entity(row: Recommendation, extra: Mapping[str, Any]) -> dict[str, Any]:
    """Return an item's ``Item.entity``: ``{source, type, id, name}``.

    ``source``, ``type`` and ``id`` (the platform id, or the composite key of an entity without
    a table row) come from the stored key ``{source}:{type}:{external_id}``, split at its first
    two separators so a URN external id keeps its own colons. The display name comes from
    ``evidence.extra.entity.name``, where the runner stores it; without one it falls back to
    the external id, as ``EntityRef`` names a blank-named entity. An item without a key keeps
    whatever ``evidence.extra.entity`` recorded, with its ``entity_type`` column as the type.

    Args:
        row: The stored item.
        extra: Its ``evidence.extra``.

    Returns:
        The entity, ready for ``schemas.EntityView`` validation.
    """
    recorded_value = extra.get(_EXTRA_ENTITY)
    recorded: Mapping[str, Any] = recorded_value if isinstance(recorded_value, Mapping) else {}
    recorded_source = recorded.get("source")
    source: str | None = recorded_source if recorded_source in SOURCES else None
    entity_type: str | None = row.entity_type or _text(recorded.get("type"))
    external_id: object = recorded.get("id")
    if row.entity_id:
        parts = row.entity_id.split(_KEY_SEPARATOR, 2)
        if len(parts) == 3 and parts[2]:
            key_source, key_type, key_id = parts
            if key_source in SOURCES:
                source = key_source
            entity_type = key_type or entity_type
            external_id = key_id
        else:
            # Not a `{source}:{type}:{external_id}` key: the key itself is the identifier.
            external_id = row.entity_id
    name = _text(recorded.get("name"))
    if name is None and external_id is not None:
        name = str(external_id)
    return {"source": source, "type": entity_type, "id": external_id, "name": name}


def _decision_subject(value: object) -> dict[str, Any] | None:
    """Return a stored ``decision_subject`` as ``{type, id}``, or ``None`` when absent."""
    if not isinstance(value, Mapping):
        return None
    return {"type": value.get("type"), "id": value.get("id")}


def _item_overlay(view: ItemOverlayView) -> dict[str, Any]:
    """Return ``Item.current`` from the overlay's view of one item (computed at read time)."""
    return {
        "held_now": view.held_now,
        "held_reason_now": view.held_reason_now,
        "historical": view.historical,
        "data_status_now": view.data_status_now,
        "flags_now": _current_flags(view.flags_now),
        "hidden_by": view.hidden_by,
    }


def _item(row: Recommendation, current: ItemOverlayView) -> schemas.Item:
    """Return one ``Item``: the stored row exactly as frozen, plus its read-time overlay.

    The evidence block is validated as stored and never edited, so its ``provenance`` keeps
    the datasets the run read even after they were replaced (AAP 0.3.2).

    Args:
        row: The stored item.
        current: The overlay's view of it.

    Returns:
        The validated item.

    Raises:
        pydantic.ValidationError: When the stored row holds a value outside the contract.
    """
    extra = _extra(row.evidence)
    return schemas.Item.model_validate(
        {
            "id": row.id,
            "rank": row.rank,
            "rule_id": row.rule_id,
            "action": row.action,
            "reason": row.reason,
            "entity": _entity(row, extra),
            "evidence": schemas.Evidence.model_validate(row.evidence),
            "flags": _stored_flags(row, extra),
            "data_status": row.data_status,
            "held": row.held,
            "held_reason": row.held_reason,
            "decision_subject": _decision_subject(row.decision_subject),
            "current": _item_overlay(current),
        }
    )


# ---------------------------------------------------------------------------
# Groups and sections
# ---------------------------------------------------------------------------


def _creative_group_title(key: str, rows: Sequence[Recommendation]) -> str:
    """Return a creative block's title: the name of the campaign it is about.

    The campaign's name is the entity name of the group's campaign item (the CR2/CR3 card),
    else the ``evidence.extra.campaign.name`` every creative item freezes, else the key.

    Args:
        key: The group key, ``campaign:{id}``.
        rows: The group's stored items, in rank order.

    Returns:
        The display title.
    """
    for row in rows:
        if row.entity_type == ENTITY_CAMPAIGN:
            name = _text(_entity(row, _extra(row.evidence)).get("name"))
            if name is not None:
                return name
    for row in rows:
        campaign = _extra(row.evidence).get(_EXTRA_CAMPAIGN)
        if isinstance(campaign, Mapping):
            name = _text(campaign.get("name"))
            if name is not None:
                return name
    return key


def _dynamic_title(key: str, rows: Sequence[Recommendation]) -> str:
    """Return the title of a group outside the section's static groups.

    ``campaign:{id}`` is titled with the campaign's name, ``cohort:{CURRENCY}`` with the
    currency (for example ``USD``), and any other key with the key itself.
    """
    if creative_group_campaign(key) is not None:
        return _creative_group_title(key, rows)
    currency = cohort_group_currency(key)
    if currency is not None:
        return currency
    return key


def _is_dynamic(key: str) -> bool:
    """Whether ``key`` is a creative block (``campaign:{id}``) or a currency cohort."""
    return creative_group_campaign(key) is not None or cohort_group_currency(key) is not None


def _groups(
    section: str,
    rows: Sequence[Recommendation],
    current: Mapping[int, ItemOverlayView],
) -> list[dict[str, Any]]:
    """Arrange one section's stored items into its groups, in display order.

    Order: every static group of the section (``GROUP_ORDER``), present even when empty so
    the UI has it to render; then the dynamic ``campaign:{id}`` and
    ``cohort:{CURRENCY}`` groups by their lowest item rank (the order precedence ranked them
    in); then any other key, defensively, by lowest rank, so no stored item is dropped. Ties
    break by key. Items keep their stored rank order within each group.

    Args:
        section: The section key.
        rows: The section's stored items in rank order.
        current: The overlay's view of every item, by id.

    Returns:
        The groups as ``{key, title, items}``.

    Raises:
        KeyError: When the overlay has no view of an item (the overlay covers every item).
        pydantic.ValidationError: When a stored row holds a value outside the contract.
    """
    by_key: dict[str, list[Recommendation]] = {}
    for row in rows:
        by_key.setdefault(row.group, []).append(row)
    static = GROUP_ORDER.get(section, ())
    titles = GROUP_TITLES.get(section, MappingProxyType({}))
    extra_keys = [key for key in by_key if key not in static]

    def lowest_rank(key: str) -> tuple[int, str]:
        return (min(row.rank for row in by_key[key]), key)

    dynamic = sorted((key for key in extra_keys if _is_dynamic(key)), key=lowest_rank)
    unknown = sorted((key for key in extra_keys if not _is_dynamic(key)), key=lowest_rank)
    groups: list[dict[str, Any]] = []
    # Static groups are emitted even when the run has no item in them, so the UI always has
    # the groups behind the keyword tabs and the discrepancy group the banner links to. Whether
    # an empty group's anchor is in the page is the web section's job: `SectionFrame` keeps an
    # empty group only through `renderEmptyGroup`.
    for key in (*static, *dynamic, *unknown):
        members = by_key.get(key, [])
        title = titles.get(key) if key in static else None
        groups.append(
            {
                "key": key,
                "title": title or _dynamic_title(key, members),
                "items": [_item(row, current[row.id]) for row in members],
            }
        )
    return groups


def _sections(
    recommendations: Sequence[Recommendation],
    findings: Sequence[RunFinding],
    withheld: Sequence[RunWithheld],
    suppressions: Sequence[RunSuppression],
    current: Mapping[int, ItemOverlayView],
) -> list[schemas.Section]:
    """Build the five sections from a run's stored outputs, in one pass over each list.

    Args:
        recommendations: The run's items, ordered by section and rank.
        findings: The run's findings, in stored order.
        withheld: The run's withheld patterns, in stored order.
        suppressions: The run's suppression counts.
        current: The overlay's view of every item, by id.

    Returns:
        Exactly five sections in ``SECTION_ORDER``; a section without stored outputs is
        present with empty lists.

    Raises:
        KeyError: When a stored row names a section outside ``SECTION_ORDER`` (the database's
            check constraint makes that impossible) or the overlay lacks an item.
        pydantic.ValidationError: When a stored row holds a value outside the contract.
    """
    items_by_section: dict[str, list[Recommendation]] = {key: [] for key in SECTION_ORDER}
    findings_by_section: dict[str, list[schemas.Finding]] = {key: [] for key in SECTION_ORDER}
    withheld_by_section: dict[str, list[schemas.Withheld]] = {key: [] for key in SECTION_ORDER}
    suppressed_by_section: dict[str, dict[str, int]] = {key: {} for key in SECTION_ORDER}
    for row in recommendations:
        items_by_section[row.section].append(row)
    for finding in findings:
        findings_by_section[finding.section].append(schemas.Finding.from_row(finding))
    for pattern in withheld:
        withheld_by_section[pattern.section].append(schemas.Withheld.from_row(pattern))
    for suppression in suppressions:
        counts = suppressed_by_section[suppression.section]
        counts[suppression.reason] = counts.get(suppression.reason, 0) + suppression.count
    return [
        schemas.Section.model_validate(
            {
                "key": key,
                "title": SECTION_TITLES[key],
                "groups": _groups(key, items_by_section[key], current),
                "findings": findings_by_section[key],
                "withheld": withheld_by_section[key],
                "suppressed": suppressed_by_section[key],
            }
        )
        for key in SECTION_ORDER
    ]


def _overlay_view(result: OverlayResult) -> schemas.Overlay:
    """Return the page-level ``Overlay``, field for field from the overlay's view."""
    view = result.overlay
    return schemas.Overlay.model_validate(
        {
            "open_discrepancies": view.open_discrepancies,
            "source_modes": dict(view.source_modes),
            "stale_datasets": list(view.stale_datasets),
            "incomplete_datasets": list(view.incomplete_datasets),
            "computed_at": view.computed_at,
        }
    )


def _banners(result: OverlayResult) -> list[schemas.Banner]:
    """Return the global banners in the order the overlay fixed (AAP 0.5.5)."""
    return [
        schemas.Banner.model_validate(
            {
                "code": banner.code,
                "tone": banner.tone,
                "message": banner.message,
                "count": banner.count,
            }
        )
        for banner in result.banners
    ]


# ---------------------------------------------------------------------------
# Reads (one set-based statement each)
# ---------------------------------------------------------------------------


async def _resolve_run(session: AsyncSession, run_id: int | None) -> AnalysisRun:
    """Return the run a page shows: ``run_id`` when given, else the latest default run.

    Args:
        session: The request's session.
        run_id: The run the request names (any kind, for example a custom window), or
            ``None`` for the latest succeeded ``default`` run.

    Returns:
        The run, which has succeeded.

    Raises:
        ApiError: 404 ``not_found`` for an unknown ``run_id`` or before the first default run
            succeeds; 409 ``run_not_ready`` for a run that is ``running`` or ``failed``.
    """
    if run_id is None:
        run = await latest_default_run(session)
        if run is None:
            raise ApiError(404, "not_found", "No analysis run yet")
    else:
        run = await get_run(session, run_id)
        if run is None:
            raise not_found(f"Run {run_id}")
    if run.status != "succeeded":
        raise conflict("run_not_ready", f"Run {run.id} is {run.status}")
    return run


async def _recommendations(session: AsyncSession, run_id: int) -> list[Recommendation]:
    """Return a run's items by section and rank (``ix_recommendation_run_section_rank``)."""
    result = await session.scalars(
        select(Recommendation)
        .where(Recommendation.run_id == run_id)
        .order_by(Recommendation.section, Recommendation.rank, Recommendation.id)
    )
    return list(result)


async def _findings(session: AsyncSession, run_id: int) -> list[RunFinding]:
    """Return a run's findings in stored order."""
    result = await session.scalars(
        select(RunFinding).where(RunFinding.run_id == run_id).order_by(RunFinding.id)
    )
    return list(result)


async def _withheld(session: AsyncSession, run_id: int) -> list[RunWithheld]:
    """Return a run's withheld patterns in stored order."""
    result = await session.scalars(
        select(RunWithheld).where(RunWithheld.run_id == run_id).order_by(RunWithheld.id)
    )
    return list(result)


async def _suppressions(session: AsyncSession, run_id: int) -> list[RunSuppression]:
    """Return a run's suppression counts in stored order."""
    result = await session.scalars(
        select(RunSuppression).where(RunSuppression.run_id == run_id).order_by(RunSuppression.id)
    )
    return list(result)


async def _hold_item_current(session: AsyncSession, recommendation: Recommendation) -> None:
    """Keep the item's datasets current until the caller's transaction ends, or refuse.

    An item computed from data that has since been replaced is historical-only and accepts
    no write (AAP 0.3.2). The check is made, then every dataset the item's frozen provenance
    names is locked ``FOR SHARE`` (:func:`~ads_engine.db.repositories.lock_dataset`) in
    ascending id order, the order dataset activation locks in, and the check is repeated
    under the locks. An activation that retired one of them first has committed by then and
    is seen; one that comes later waits for this transaction, so feedback never lands on an
    item that became historical between its check and its commit.

    Args:
        session: The request's session; its transaction holds the locks.
        recommendation: The stored item.

    Raises:
        ApiError: 409 ``subject_retired`` when the item's provenance is unverifiable or any of
            its datasets is not current, before or after the locks.
    """
    if not await item_is_current(session, recommendation):
        raise conflict("subject_retired", HISTORICAL_REASON)
    provenance = item_provenance(recommendation) or ()
    for dataset_id in sorted({entry.dataset_id for entry in provenance}):
        await lock_dataset(session, dataset_id)
    if not await item_is_current(session, recommendation):
        raise conflict("subject_retired", HISTORICAL_REASON)


async def _hold_item_row(session: AsyncSession, recommendation: Recommendation) -> None:
    """Lock the item's own row until the caller's transaction ends, serialising its feedback.

    Two feedback requests for one item, such as the two halves of a double click, would
    otherwise both find no earlier feedback and both insert one. The row is locked ``FOR NO
    KEY UPDATE``, so the second request waits here until the first commits and then, under
    READ COMMITTED, sees its feedback. The ``FOR KEY SHARE`` check of a feedback or decision
    insert that references the item still passes, and the stored item itself is never
    changed. Called after :func:`_hold_item_current`, because writers lock datasets before
    rows (:func:`~ads_engine.db.repositories.lock_dataset`).

    Args:
        session: The request's session; its transaction holds the lock.
        recommendation: The stored item.

    Raises:
        ApiError: 404 ``not_found`` when the row is gone by the time it is locked, as it is
            only once its run has been deleted.
    """
    locked = await session.scalar(
        select(Recommendation.id)
        .where(Recommendation.id == recommendation.id)
        .with_for_update(key_share=True)
    )
    if locked is None:
        raise not_found(f"Recommendation {recommendation.id}")


async def _latest_feedback_in_force(
    session: AsyncSession, recommendation_id: int, now: datetime
) -> RecommendationFeedback | None:
    """Return the item's most recent feedback still in force at ``now``, or ``None``.

    "Most recent" is the order the overlay applies feedback in, ``created_at`` then id, so
    the row returned is the one that hides the item now (``hidden_by``). Only feedback given
    on this item counts; feedback on another run's item of the same rule and entity is not
    a repeat of this request.
    """
    feedback = RecommendationFeedback
    return await session.scalar(
        select(feedback)
        .where(feedback.recommendation_id == recommendation_id, feedback.until > now)
        .order_by(feedback.created_at.desc(), feedback.id.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/recommendations",
    response_model=schemas.RecommendationsPage,
    status_code=200,
    responses=schemas.ERROR_RESPONSES,
)
async def get_recommendations(
    user: UserDep,
    session: SessionDep,
    thresholds: ThresholdsDep,
    settings: SettingsDep,
    now: NowDep,
    run_id: Annotated[
        int | None,
        Query(
            gt=0,
            le=schemas.BIGINT_MAX,
            description="The run to show; omitted for the latest succeeded default run.",
        ),
    ] = None,
) -> schemas.RecommendationsPage:
    """Return the five sections of one run with the current-state overlay.

    The sections, groups, items, evidence, flags, findings, withheld patterns and
    suppression counts are the run's frozen outputs, returned exactly as stored: no rule is
    re-run and no evidence rewritten. Only each item's ``current`` and the page's
    ``overlay`` and banners are computed now, from live discrepancies, source modes, active
    datasets, freshness and feedback, so a discrepancy detected after the run holds its
    dependent items at once, and an item computed from replaced data is ``historical``.

    Without ``run_id`` the page shows the latest succeeded ``default`` run, never a custom
    one; ``run_id`` addresses any run by id, such as a custom window (``/actions?run=<id>``).
    The five sections are always present, in the order ``keywords``, ``campaigns``,
    ``budget``, ``creative``, ``white_papers``, each with every static group even when empty.
    The run header and its short-window banner read the window settings the run stored when
    it was computed, so they agree with the run's frozen ``short_window`` flags.

    Args:
        user: The signed-in user; the dependency enforces the session.
        session: The request's database session; only read.
        thresholds: The loaded configuration the overlay reads (freshness limits and the
            K1 baseline length).
        settings: The application's settings (``DISPLAY_TIMEZONE`` for the run header and
            for the instants in the overlay's stale banner and ``stale`` flags).
        now: The read time the overlay is computed for.
        run_id: The run to show, or ``None`` for the latest succeeded default run.

    Returns:
        The ``RecommendationsPage``.

    Raises:
        ApiError: 404 ``not_found`` for an unknown ``run_id`` or before any default run has
            succeeded; 409 ``run_not_ready`` for a run still ``running`` or ``failed``.
    """
    run = await _resolve_run(session, run_id)
    recommendations = await _recommendations(session, run.id)
    findings = await _findings(session, run.id)
    withheld = await _withheld(session, run.id)
    suppressions = await _suppressions(session, run.id)
    result = await overlay(
        session,
        run,
        recommendations,
        now,
        findings=findings,
        thresholds=thresholds,
        display_timezone=settings.display_timezone,
    )
    page = schemas.RecommendationsPage(
        run=schemas.Run.from_row(run, display_timezone=settings.display_timezone),
        overlay=_overlay_view(result),
        banners=_banners(result),
        sections=_sections(recommendations, findings, withheld, suppressions, result.items),
    )
    _log.debug(
        "recommendations.page",
        run_id=run.id,
        kind=run.kind,
        items=len(recommendations),
        user_id=user.id,
    )
    return page


@router.post(
    "/recommendations/{rec_id}/feedback",
    response_model=schemas.Feedback,
    status_code=201,
    responses=schemas.ERROR_RESPONSES,
)
async def post_feedback(
    rec_id: Annotated[int, Path(gt=0, le=schemas.BIGINT_MAX, description="The recommendation id.")],
    body: schemas.FeedbackRequest,
    user: UserDep,
    session: SessionDep,
    thresholds: ThresholdsDep,
    now: NowDep,
) -> schemas.Feedback:
    """Dismiss, snooze or mark done one item, suppressing its rule and entity for a while.

    The feedback copies the item's rule id and stable entity key, so it suppresses the same
    rule and entity in later runs until ``until``: ``feedback.dismiss_days`` (30),
    ``feedback.snooze_days`` (7) or ``feedback.done_days`` (30) after now, as configured in
    ``config/thresholds.yaml``. The overlay hides the item at once (``hidden_by``), and a
    default-window rerun is requested in the same transaction. "Mark done" is feedback only:
    no decision is written and no discrepancy is touched.

    A repeat is answered with the feedback it repeats, so a double submit records one row.
    Feedback requests for one item are serialised on the item's row (:func:`_hold_item_row`);
    when the item's most recent feedback still in force has the requested kind and note, that
    row is returned unchanged, with 201 and the same ``Feedback`` body, and nothing is written:
    no second row, no later ``until`` and no second rerun request. Any other request is
    recorded as a new row, a different kind included (a dismiss after a snooze becomes the
    item's latest feedback), and so is one after the earlier feedback has expired.

    Args:
        rec_id: The recommendation id.
        body: The feedback kind and an optional note.
        user: The signed-in user, recorded as the feedback's author.
        session: The request's database session; committed here.
        thresholds: The loaded configuration (``feedback.*_days``).
        now: The feedback instant, from which ``until`` is counted.

    Returns:
        The recorded feedback, or the one this request repeats.

    Raises:
        ApiError: 404 ``not_found`` for an unknown item; 422 ``feedback_not_allowed`` for a C0
            item, which is exempt and must be resolved instead; 409 ``subject_retired`` for an
            item computed from data that has since been replaced, a repeat included.
    """
    recommendation = await session.get(Recommendation, rec_id)
    if recommendation is None:
        raise not_found(f"Recommendation {rec_id}")
    if recommendation.rule_id == C0:
        raise ApiError(422, "feedback_not_allowed", _C0_FEEDBACK_MESSAGE)
    await _hold_item_current(session, recommendation)
    await _hold_item_row(session, recommendation)
    latest = await _latest_feedback_in_force(session, recommendation.id, now)
    if latest is not None and (latest.kind, latest.note) == (body.kind, body.note):
        view = schemas.Feedback.from_row(latest)
        # Nothing was written; ending the transaction releases the dataset and row locks.
        await session.commit()
        _log.info(
            "recommendations.feedback_replayed",
            recommendation_id=recommendation.id,
            rule_id=recommendation.rule_id,
            kind=body.kind,
            feedback_id=latest.id,
            user_id=user.id,
        )
        return view
    days = thresholds.int(FEEDBACK_DAYS_KEYS[body.kind])
    row = RecommendationFeedback(
        recommendation_id=recommendation.id,
        rule_id=recommendation.rule_id,
        entity_type=recommendation.entity_type,
        entity_id=recommendation.entity_id,
        kind=body.kind,
        until=now + timedelta(days=days),
        note=body.note,
        # The request clock, not the database's now(): `until` is counted from the same
        # instant, and a test that fixes NowDep fixes both.
        created_at=now,
        created_by=user.email,
    )
    session.add(row)
    await session.flush()
    await enqueue_analysis_request(session, _FEEDBACK_REASON)
    # Built before the commit, so the view never depends on the session's expiry setting.
    view = schemas.Feedback.from_row(row)
    await session.commit()
    _log.info(
        "recommendations.feedback",
        recommendation_id=recommendation.id,
        rule_id=recommendation.rule_id,
        kind=body.kind,
        feedback_id=row.id,
        user_id=user.id,
    )
    return view
