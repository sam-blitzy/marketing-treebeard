"""Rule C0: a campaign or shared budget whose platform state differs from the last decision.

AAP 0.5.4.4 (C0 row and "C0 resolutions by kind and subject type"), 0.5.4.2 P1 and FR-5/FR-10:
discrepancies are resolved first, before any other recommendation is trusted. Every unresolved
discrepancy of the active datasets (``open`` and ``awaiting_platform``) becomes one pinned item
in Campaigns → Discrepancies, oldest ``detected_at`` first, carrying the platform value against
the last decided value and exactly the resolutions valid for its (kind, decision shape) pair.

Detection is not here. ``sync/snapshots.py`` raises discrepancies after each entity sync through
``ads_engine.decisions.compare`` and ``raise_discrepancy``, and ``metrics.load_context`` puts
them in :attr:`AnalysisContext.discrepancies`. This module only presents them, so it is pure: no
database, network, settings or logging access.

C0 items are exempt from holds, feedback and ordinary ranking (P1). They are ``pinned`` with a
``rank_key`` of ``(detected_at, discrepancy id)``, carry no ``decision_subject`` (the card shows
its resolver instead of "Record decision") and list their own subject in ``depends_on``, the
subject whose discrepancy holds every dependent item.

Example:
    >>> render_value("budget", "own_budget", {"daily_budget": "50.000000", "currency": "USD"}, None)
    'daily budget USD 50.00'
    >>> render_value("budget", "shared_member", {"budget_id": 7, "budget_external_id": "123"}, None)
    'shared budget 123'
    >>> offered_resolutions("missing", "own_budget")
    ('decision_correct', 'acknowledge_removal')
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from decimal import Context, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final, get_args

from ads_engine.analysis.context import (
    C0,
    CAMPAIGNS,
    GROUP_DISCREPANCIES,
    AnalysisContext,
    Candidate,
    DecisionShape,
    DecisionSubject,
    DiscrepancyInfo,
    DiscrepancyKind,
    EntityRef,
    Flag,
    Metric,
    RuleResult,
    google_link_gap,
    no_crm_link_flag,
)
from ads_engine.decisions import RESOLUTIONS, Resolution, from_micros, minor_unit_exponent

__all__ = [
    "NOT_RETURNED_TEXT",
    "NO_DECISION_TEXT",
    "RESOLUTION_ORDER",
    "STATUS_LABELS",
    "STATUS_NOT_REPORTED_TEXT",
    "TOMBSTONE_TEXT",
    "UNAVAILABLE_TEXT",
    "evaluate",
    "offered_resolutions",
    "render_value",
]

# ---------------------------------------------------------------------------
# Display texts and the fixed resolution order
# ---------------------------------------------------------------------------

RESOLUTION_ORDER: Final[tuple[Resolution, ...]] = (
    "platform_correct",
    "decision_correct",
    "adopt_platform",
    "record_intended",
    "acknowledge_removal",
)
"""The order resolutions are listed in ``evidence.extra.resolutions`` and rendered by the UI."""

if set(RESOLUTION_ORDER) != set(get_args(Resolution)) or len(RESOLUTION_ORDER) != len(
    get_args(Resolution)
):
    raise RuntimeError(
        f"RESOLUTION_ORDER {RESOLUTION_ORDER} must list every resolution of "
        f"ads_engine.decisions.Resolution {get_args(Resolution)} exactly once"
    )

UNAVAILABLE_TEXT: Final = "unavailable"
"""A value that is not known (``None``), never shown as zero."""

NOT_RETURNED_TEXT: Final = "not returned by the platform"
"""The platform side of a ``missing`` discrepancy."""

NO_DECISION_TEXT: Final = "no decision recorded"
"""The decided side of an ``untracked`` discrepancy before an intended state is recorded."""

TOMBSTONE_TEXT: Final = "Removal acknowledged"
"""A decided state written by ``acknowledge_removal``; a campaign's adds its status label."""

STATUS_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {"active": "Active", "paused": "Paused", "ended": "Ended"}
)
"""The label of each normalised campaign status.

The web client's decision log, decision form and discrepancy resolver show the same labels, so
a value reads alike on ``/actions`` and ``/decisions``.
"""

STATUS_NOT_REPORTED_TEXT: Final = "status not reported"
"""A side whose status is absent, worded as the web client's decision screens word it."""

_PART_SEPARATOR: Final = " · "
"""Joins the parts of one state (status, then each amount or the assignment), as the web
client's decision screens join them."""

_TOMBSTONE_STATUS: Final = "ended"
"""The status ``acknowledge_removal`` writes on a campaign, shown when a tombstone carries none."""

_AWAITING_SUFFIX: Final = " Awaiting a matching platform snapshot."
"""Appended to the reason of an ``awaiting_platform`` discrepancy."""

_DISCREPANCY_KINDS: Final[tuple[str, ...]] = get_args(DiscrepancyKind)
_DECISION_SOURCES: Final[tuple[str, ...]] = ("google", "linkedin")
_MONEY_KEYS: Final[frozenset[str]] = frozenset({"daily_budget", "total_budget", "amount_micros"})
_ASSIGNMENT_KEYS: Final[frozenset[str]] = frozenset({"budget_id", "budget_external_id"})

_DEFAULT_MINOR_UNIT: Final[int] = 2
"""Decimal places shown when an amount's currency is not known."""

_MAX_PLACES: Final[int] = 6
"""Finest stored money precision: one Google micro, the scale of ``numeric(18, 6)``."""

_EXACT: Final[Context] = Context(prec=60)
"""Wide enough that normalising and quantizing any storable amount is exact."""


# ---------------------------------------------------------------------------
# Resolutions
# ---------------------------------------------------------------------------


def offered_resolutions(kind: DiscrepancyKind, shape: DecisionShape) -> tuple[Resolution, ...]:
    """Return the resolutions valid for a (kind, decision shape) pair, in :data:`RESOLUTION_ORDER`.

    The matrix is ``ads_engine.decisions.RESOLUTIONS``, the same one ``resolve_discrepancy``
    enforces, so the resolver offers exactly what the API accepts (AAP 0.5.4.4, 0.5.5).

    Example:
        >>> offered_resolutions("untracked", "budget")
        ('adopt_platform', 'record_intended')

    Args:
        kind: ``status``, ``budget``, ``untracked`` or ``missing``.
        shape: ``own_budget``, ``shared_member`` or ``budget``.

    Returns:
        The valid resolutions, ordered.

    Raises:
        ValueError: For a pair the matrix does not define, such as ``("status", "budget")``: a
            shared budget has no status, so detection can never produce it.
    """
    valid = RESOLUTIONS.get((kind, shape))
    if valid is None:
        raise ValueError(
            f"no resolution is defined for a {kind!r} discrepancy on a {shape!r} subject"
        )
    return tuple(resolution for resolution in RESOLUTION_ORDER if resolution in valid)


# ---------------------------------------------------------------------------
# Value rendering (pure)
# ---------------------------------------------------------------------------


def _amount(raw: object) -> Decimal | None:
    """Read a stored amount exactly: ``Decimal``, ``int`` or decimal text; else ``None``.

    Discrepancy values store amounts as positional decimal text (``"100.000000"``). Anything
    that is not an exact finite amount is unknown, never zero.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        amount = raw
    elif isinstance(raw, int):
        amount = Decimal(raw)
    elif isinstance(raw, str):
        try:
            amount = Decimal(raw.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return amount if amount.is_finite() else None


def _currency_code(value: Mapping[str, Any], fallback: str | None) -> str | None:
    """Return the value's own currency code, else ``fallback``."""
    code = value.get("currency")
    if isinstance(code, str) and code.strip():
        return code.strip().upper()
    if fallback is not None and fallback.strip():
        return fallback.strip().upper()
    return None


def _format_amount(amount: Decimal, currency: str | None) -> str:
    """Format an amount with its currency code at minor-unit precision, hiding no digit.

    The amount shows at least the currency's minor-unit places (two for USD, none for JPY) and
    more when it has significant digits beyond them, up to one micro. A Google budget that
    differs by one micro therefore never renders as two identical amounts: C0 has no
    tolerance, and rounding for display must not hide the mismatch it reports. The web client's
    ``formatExactMoney`` (``web/src/lib/format.ts``) renders the same text on ``/decisions``.
    """
    minor = _DEFAULT_MINOR_UNIT if currency is None else minor_unit_exponent(currency)
    exponent = amount.normalize(_EXACT).as_tuple().exponent
    significant = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    places = max(minor, min(significant, _MAX_PLACES))
    quantized = amount.quantize(Decimal(1).scaleb(-places, context=_EXACT), context=_EXACT)
    text = format(quantized, f",.{places}f")
    return text if currency is None else f"{currency} {text}"


def _status_text(value: Mapping[str, Any]) -> str:
    """Render a value's normalised status by its :data:`STATUS_LABELS` label (``Paused``).

    A status outside that vocabulary is shown as sent rather than mislabelled; an absent or
    blank one is :data:`STATUS_NOT_REPORTED_TEXT`.
    """
    status = value.get("status")
    if isinstance(status, str) and status.strip():
        text = status.strip()
        return STATUS_LABELS.get(text, text)
    return STATUS_NOT_REPORTED_TEXT


def _tombstone_text(shape: DecisionShape, value: Mapping[str, Any]) -> str:
    """Render a decided state written by ``acknowledge_removal``.

    A shared budget has no status, so its tombstone is :data:`TOMBSTONE_TEXT` alone; a
    campaign's adds its status label, ``ended`` unless the value names another:
    "Removal acknowledged · Ended", as the decision log on ``/decisions`` words the decision.
    """
    if shape == "budget":
        return TOMBSTONE_TEXT
    status = value.get("status")
    shown = value if isinstance(status, str) and status.strip() else {"status": _TOMBSTONE_STATUS}
    return _PART_SEPARATOR.join((TOMBSTONE_TEXT, _status_text(shown)))


def _has_assignment(value: Mapping[str, Any]) -> bool:
    """Whether a value describes a budget assignment, shared or not, rather than an amount."""
    return not _ASSIGNMENT_KEYS.isdisjoint(value)


def _assignment_text(value: Mapping[str, Any]) -> str:
    """Render a campaign's budget assignment.

    ``{"budget_id": 7, "budget_external_id": "123"}`` is "shared budget 123". A ``budget_id``
    of ``None`` is the campaign's own, unshared budget: :func:`ads_engine.decisions.compare`
    stores it for an own-budget decision on a campaign that now draws from a shared budget.
    ``explicitly_shared: false`` (added for display by :func:`evaluate` from the context's
    budgets) names a budget that is not shared.
    """
    if not _has_assignment(value):
        return f"budget assignment {UNAVAILABLE_TEXT}"
    external_id = value.get("budget_external_id")
    if isinstance(external_id, str) and external_id.strip():
        if value.get("explicitly_shared") is False:
            return f"non-shared budget {external_id.strip()}"
        return f"shared budget {external_id.strip()}"
    budget_id = value.get("budget_id")
    if isinstance(budget_id, int) and not isinstance(budget_id, bool):
        return f"budget (internal id {budget_id})"
    return "own budget (not shared)"


def _money_text(value: Mapping[str, Any], currency: str | None) -> str:
    """Render the amounts of an ``own_budget`` or ``budget`` value.

    Google values carry ``amount_micros``, converted exactly (``amount_micros / 1_000_000``);
    the ``daily_budget`` or ``total_budget`` field present names the period. LinkedIn values
    carry ``daily_budget`` and ``total_budget`` as decimal text. A Google amount the platform
    omitted (``amount_micros`` null) is unavailable, never zero; a LinkedIn campaign with
    neither budget has "no budget set".
    """
    if _MONEY_KEYS.isdisjoint(value):
        return f"budget {UNAVAILABLE_TEXT}"
    code = _currency_code(value, currency)
    daily = _amount(value.get("daily_budget"))
    total = _amount(value.get("total_budget"))
    micros = value.get("amount_micros")
    if isinstance(micros, int) and not isinstance(micros, bool):
        exact = _format_amount(from_micros(micros), code)
        if daily is not None and total is None:
            return f"daily budget {exact}"
        if total is not None and daily is None:
            return f"total budget {exact}"
        return f"budget {exact}"
    parts: list[str] = []
    if daily is not None:
        parts.append(f"daily budget {_format_amount(daily, code)}")
    if total is not None:
        parts.append(f"total budget {_format_amount(total, code)}")
    if parts:
        return _PART_SEPARATOR.join(parts)
    reported = value.get("daily_budget") is not None or value.get("total_budget") is not None
    if "amount_micros" in value or reported:
        return f"budget {UNAVAILABLE_TEXT}"
    return "no budget set"


def render_value(
    kind: DiscrepancyKind,
    shape: DecisionShape,
    value: Mapping[str, Any] | None,
    currency: str | None,
) -> str:
    """Render one side of a discrepancy (``decided_value`` or ``platform_value``) for display.

    The wording is the web client's on ``/decisions`` (``DiscrepancyResolver``, ``DecisionLog``),
    so the C0 card on ``/actions`` and the resolver card show one value alike: status labels,
    amounts as "{code} {amount}", and the parts of one state joined by " · ".

    * ``status``: the status label (:data:`STATUS_LABELS`), for example ``Paused``; an absent
      status is "status not reported".
    * ``budget``: a ``shared_member``'s (or any assignment value's) budget assignment,
      "shared budget {external_id}"; otherwise the amounts with the currency code at minor-unit
      precision, Google micros converted exactly, for example "daily budget USD 50.00", a daily
      and a total budget joined by " · ".
    * ``untracked`` and ``missing``: the complete state the subject's shape owns: status and
      amounts (``own_budget``), status and assignment (``shared_member``), or the amount alone
      (``budget``), joined by " · ".
    * A tombstone is "Removal acknowledged", followed on a campaign by its status label
      ("Removal acknowledged · Ended"); ``None`` is "unavailable".

    Example:
        >>> render_value("status", "own_budget", {"status": "paused"}, "USD")
        'Paused'
        >>> value = {"daily_budget": "50.000001", "currency": "USD", "amount_micros": 50000001}
        >>> render_value("budget", "budget", value, "USD")
        'daily budget USD 50.000001'
        >>> value = {"status": "active", "daily_budget": "400.000000", "currency": "USD"}
        >>> render_value("untracked", "own_budget", value, "USD")
        'Active · daily budget USD 400.00'
        >>> render_value("missing", "own_budget", {"status": "ended", "tombstone": True}, "USD")
        'Removal acknowledged · Ended'

    Args:
        kind: The discrepancy kind.
        shape: The subject's decision shape.
        value: The stored JSON value, or ``None``.
        currency: The subject's account currency, used when the value carries none.

    Returns:
        The display text.

    Raises:
        ValueError: For an unknown discrepancy kind.
    """
    if kind not in _DISCREPANCY_KINDS:
        raise ValueError(f"discrepancy kind must be one of {_DISCREPANCY_KINDS}, found {kind!r}")
    if value is None:
        return UNAVAILABLE_TEXT
    if value.get("tombstone") is True:
        return _tombstone_text(shape, value)
    if kind == "status":
        return _status_text(value)
    assignment = shape == "shared_member" or _has_assignment(value)
    if kind == "budget":
        return _assignment_text(value) if assignment else _money_text(value, currency)
    parts: list[str] = []
    if shape != "budget":
        parts.append(_status_text(value))
    parts.append(_assignment_text(value) if assignment else _money_text(value, currency))
    return _PART_SEPARATOR.join(parts)


# ---------------------------------------------------------------------------
# C0 items
# ---------------------------------------------------------------------------


def _subject_currency(ctx: AnalysisContext, item: DiscrepancyInfo) -> str | None:
    """Return the account currency of a discrepancy's subject, when the context holds it."""
    if item.subject_type == "campaign":
        campaign = ctx.campaigns.get(item.subject_id)
        if campaign is None:
            return None
        account = ctx.accounts.get(campaign.account_id)
        return campaign.currency if account is None else account.currency
    budget = ctx.budgets.get(item.subject_id)
    return None if budget is None else budget.currency


def _subject_external_id(ctx: AnalysisContext, item: DiscrepancyInfo) -> str:
    """Return the platform id of a discrepancy's subject.

    A subject always has its row in the active dataset, so the context normally holds it. If a
    caller built the context without that row, the item is still shown, keyed by its internal
    id: an unresolved discrepancy is never dropped, because it gates trust in everything else.
    """
    if item.subject_type == "campaign":
        campaign = ctx.campaigns.get(item.subject_id)
        external_id = None if campaign is None else campaign.external_id
    else:
        budget = ctx.budgets.get(item.subject_id)
        external_id = None if budget is None else budget.external_id
    if external_id is not None and external_id.strip():
        return external_id
    return f"internal-{item.subject_id}"


def _display_value(
    ctx: AnalysisContext, value: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Return a value enriched for display with the assigned budget's Google id and sharing.

    ``compare`` stores ``budget_external_id`` only for budgets the subject state knew; the
    context's budgets fill the gap so an assignment reads "shared budget 123" rather than an
    internal id. The stored value in ``evidence.extra`` is never altered.
    """
    if value is None:
        return None
    budget_id = value.get("budget_id")
    if not isinstance(budget_id, int) or isinstance(budget_id, bool):
        return value
    budget = ctx.budgets.get(budget_id)
    if budget is None:
        return value
    enriched = dict(value)
    external_id = enriched.get("budget_external_id")
    if not isinstance(external_id, str) or not external_id.strip():
        enriched["budget_external_id"] = budget.external_id
    enriched.setdefault("explicitly_shared", budget.explicitly_shared)
    return enriched


def _shows_money(item: DiscrepancyInfo, value: Mapping[str, Any] | None) -> bool:
    """Whether :func:`render_value` renders an amount for this side of the discrepancy."""
    if value is None or value.get("tombstone") is True or item.kind == "status":
        return False
    return item.shape != "shared_member" and not _has_assignment(value)


def _metric_unit(item: DiscrepancyInfo) -> str:
    """Return the unit of the two rendered state metrics."""
    if item.kind == "status":
        return "status"
    if item.kind == "budget":
        assignment = item.shape == "shared_member" or any(
            value is not None and _has_assignment(value)
            for value in (item.decided_value, item.platform_value)
        )
        return "budget_assignment" if assignment else "budget"
    return "state"


def _reason(item: DiscrepancyInfo) -> str:
    """Render the reason sentence from the discrepancy's own kind, shape, values and state."""
    decided = item.decided_value
    if decided is not None and decided.get("tombstone") is True:
        # A tombstone reappearance (AAP 0.5.4.4 "If a complete enumeration later returns it");
        # compare() raises it as ``status`` for a campaign and ``budget`` for a shared budget.
        reason = "An entity whose removal was acknowledged was returned by a complete enumeration."
    elif item.kind == "status":
        reason = "Platform status differs from the last decided status."
    elif item.kind == "budget":
        if item.shape == "shared_member":
            reason = "Campaign is assigned to a different shared budget than decided."
        elif decided is not None and _has_assignment(decided):
            reason = "Campaign's budget assignment differs from the last decided assignment."
        else:
            reason = "Platform budget differs from the last decided budget."
    elif item.kind == "untracked":
        if decided is None:
            reason = "Entity first seen after this dataset's baseline, with no decision."
        else:
            # record_intended stores the intended state as the decided value and waits.
            reason = (
                "Entity first seen after this dataset's baseline; its intended state is recorded."
            )
    else:
        reason = "A tracked entity was not returned by a complete enumeration."
    if item.state == "awaiting_platform":
        reason += _AWAITING_SUFFIX
    return reason


def _candidate(ctx: AnalysisContext, item: DiscrepancyInfo, crm_link: Flag | None) -> Candidate:
    """Build the pinned C0 item of one unresolved discrepancy.

    Threshold keys: none. C0 has no tolerance: Google amounts are compared in micros exactly
    and LinkedIn amounts at the currency's minor unit, and decisions are validated to that
    unit, so rounding can neither cause nor hide a mismatch.

    Rationale (AAP 0.5.4.4, P1): discrepancies come first, because no other recommendation on
    a subject can be trusted while its platform state differs from the decided state. The item
    is therefore pinned first, ranked by ``(detected_at, id)``, and exempt from holds, feedback
    and ordinary ranking; :func:`evaluate` documents the rule.

    ``crm_link`` is the run's ``no_crm_link`` flag, or ``None`` while the Google-to-CRM link is
    verified. A Google subject's item carries it (AAP 0.3.3: Google items carry
    ``no_crm_link``); a LinkedIn subject's never does.

    Raises:
        ValueError: When the subject's source has no decision subjects, or the (kind, shape)
            pair has no resolution (:func:`offered_resolutions`).
    """
    if item.source not in _DECISION_SOURCES:
        raise ValueError(
            f"discrepancy {item.id}: {item.source!r} has no decision subjects; "
            f"expected one of {_DECISION_SOURCES}"
        )
    resolutions = offered_resolutions(item.kind, item.shape)
    currency = _subject_currency(ctx, item)
    platform_display = _display_value(ctx, item.platform_value)
    decided_display = _display_value(ctx, item.decided_value)

    if item.kind == "missing":
        platform_text = NOT_RETURNED_TEXT
    else:
        platform_text = render_value(item.kind, item.shape, platform_display, currency)
    if item.kind == "untracked" and item.decided_value is None:
        decided_text = NO_DECISION_TEXT
    else:
        decided_text = render_value(item.kind, item.shape, decided_display, currency)

    unit = _metric_unit(item)
    platform_currency = (
        _currency_code(item.platform_value, currency)
        if item.kind != "missing"
        and item.platform_value is not None
        and _shows_money(item, platform_display)
        else None
    )
    decided_currency = (
        _currency_code(item.decided_value, currency)
        if item.decided_value is not None and _shows_money(item, decided_display)
        else None
    )
    evidence = ctx.make_evidence(
        required_datasets=(f"{item.source}.entities",),
        crm_scope=None,
        metrics=(
            Metric("platform", "Platform", platform_text, unit, currency=platform_currency),
            Metric("decided", "Last decided", decided_text, unit, currency=decided_currency),
        ),
        extra={
            "discrepancy_id": item.id,
            "kind": item.kind,
            "shape": item.shape,
            "state": item.state,
            "detected_at": item.detected_at.astimezone(UTC).isoformat(),
            "decided": None if item.decided_value is None else dict(item.decided_value),
            "platform": None if item.platform_value is None else dict(item.platform_value),
            "resolutions": list(resolutions),
        },
        # Precedence sets the headline once every hold is final: the number of recommendations
        # this discrepancy holds (``held_recommendations``, appended after these metrics).
        headline=None,
    )
    name = item.subject_name if item.subject_name.strip() else ctx.subject_name(item.key)
    subject = DecisionSubject(item.subject_type, item.subject_id)
    return Candidate(
        rule_id=C0,
        section=CAMPAIGNS,
        group=GROUP_DISCREPANCIES,
        entity_ref=EntityRef(
            source=item.source,
            type=item.subject_type,
            id=item.subject_id,
            external_id=_subject_external_id(ctx, item),
            name=name,
        ),
        action=f"Resolve: platform shows {platform_text}, last decided {decided_text}",
        reason=_reason(item),
        evidence=evidence,
        rank_key=(item.detected_at, item.id),
        flags=(crm_link,) if crm_link is not None and item.source == "google" else (),
        required_datasets=(f"{item.source}.entities",),
        increases_spend_on=(),
        # C0 offers its resolver instead of "Record decision" (AAP 0.5.5).
        decision_subject=None,
        depends_on=(subject,),
        pinned=True,
    )


def evaluate(ctx: AnalysisContext) -> RuleResult:
    """Rule C0: present every unresolved discrepancy as a pinned item, oldest first.

    AAP 0.5.4.4: the newest snapshot of each campaign and shared budget is compared with its
    current decision after every entity sync (``sync/snapshots.py``); kinds are ``status``,
    ``budget``, ``untracked`` (first seen after the dataset's baseline, with no decision) and
    ``missing`` (a tracked entity a complete enumeration no longer returns). This rule turns
    each ``open`` or ``awaiting_platform`` row of :attr:`AnalysisContext.discrepancies` into
    an item "Resolve: platform shows {platform}, last decided {decided}".

    Threshold keys: none. C0 has no tolerance: Google amounts are compared in whole micros and
    LinkedIn amounts at the currency's minor unit, and decisions are validated to that unit
    (AAP 0.5.2), so rounding can neither cause nor hide a mismatch. Display keeps every
    significant digit for the same reason.

    Rationale (FR-5, FR-10, P1): a recommendation computed on a campaign whose platform state
    differs from what was decided cannot be trusted, so C0 items are pinned at the top of
    Campaigns, oldest ``detected_at`` first, and exempt from holds, feedback and ordinary
    ranking. Each item lists in ``evidence.extra.resolutions`` exactly the resolutions
    ``ads_engine.decisions.RESOLUTIONS`` allows for its (kind, shape) pair, in
    :data:`RESOLUTION_ORDER`, so the resolver renders the row for its discrepancy.

    CRM link: the Google-to-CRM link gap is decided once per run
    (:func:`~ads_engine.analysis.context.google_link_gap`). While it is not verified, the item
    of every Google subject carries the ``no_crm_link`` flag (AAP 0.3.3, 0.5.4.1); LinkedIn
    subjects' items never do.

    Args:
        ctx: The analysis context.

    Returns:
        One pinned candidate per unresolved discrepancy, ordered by ``(detected_at, id)``; no
        findings, withheld patterns or suppression counts.

    Raises:
        ValueError: For a discrepancy whose source has no decision subjects or whose (kind,
            shape) pair has no defined resolution; detection never produces either.
    """
    ordered = sorted(ctx.discrepancies, key=lambda item: (item.detected_at, item.id))
    gap = google_link_gap(ctx)
    crm_link = None if gap is None else no_crm_link_flag(gap)
    return RuleResult(candidates=tuple(_candidate(ctx, item, crm_link) for item in ordered))
