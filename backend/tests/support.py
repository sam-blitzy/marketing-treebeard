"""Shared, pure root test support for the acceptance suite.

Generic readers of a recommendation's evidence, the numeric normalisation used to compare
shown values with expected ones, and :func:`with_google_link`, which sets a context's
Google-to-CRM link state. Acceptance modules import them from here
(``from tests.support import ...``) rather than keeping their own copies. Rule-specific oracles,
which restate a rule independently of the code under test, stay in the module that tests that
rule.

Pure: no fixture, database, network or clock.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from ads_engine.analysis.context import AnalysisContext, Candidate, CrmData, DatasetRef
from ads_engine.analysis.thresholds import CrmBindings, GoogleCrmBinding

type GoogleLinkState = Literal["absent", "unverified", "verified"]
"""A context's Google-to-CRM link: no Google CRM dataset, an unverified link or a verified one."""

GOOGLE_LINK_UNVERIFIED_REASON: Final = "HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"
"""The binding reason :func:`with_google_link` gives an ``unverified`` link."""

_STAND_IN_CRM_DATASET: Final = DatasetRef(90, "hubspot", "simulated")
"""HubSpot's primary dataset for a context that loaded none."""


def evidence_metric(item: Candidate, key: str) -> Any:
    """Return the value of an item's evidence metric ``key``.

    Args:
        item: The recommendation whose evidence is read.
        key: The metric key.

    Returns:
        The metric's value, exactly as the evidence carries it.

    Raises:
        KeyError: When the evidence has no metric with that key.
    """
    return item.evidence.metric(key).value


def evidence_metrics(item: Candidate) -> dict[str, Any]:
    """Return an item's evidence metric values by key.

    Args:
        item: The recommendation whose evidence is read.

    Returns:
        A new dict mapping each metric key to its value, in evidence order.
    """
    return {metric.key: metric.value for metric in item.evidence.metrics}


def numeric(value: object) -> object:
    """Normalise a value so numbers compare by value.

    ``"3.0"``, ``3`` and ``Decimal("3.000000")`` all become equal ``Decimal`` values.

    Args:
        value: A shown or expected value.

    Returns:
        ``None`` and booleans unchanged; an ``int`` or ``Decimal`` as a ``Decimal``; a numeric
        string as a ``Decimal`` and any other string unchanged; a list or tuple as a tuple of
        its normalised items; any other value unchanged.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    if isinstance(value, list | tuple):
        return tuple(numeric(item) for item in value)
    return value


def with_google_link(ctx: AnalysisContext, state: GoogleLinkState) -> AnalysisContext:
    """Return a copy of ``ctx`` whose Google-to-CRM link is in ``state`` (AAP 0.3.3, 0.5.4.1).

    Only the Google link changes: the context's own Google CRM dataset keeps its contacts and
    its MQL and QL bindings, and a context without one gets an empty HubSpot primary dataset
    (its loaded one, else a stand-in) whose counts are all unavailable. The copy has an empty
    per-run cache, so no memoised table of ``ctx`` leaks in.

    Args:
        ctx: The context to copy.
        state: ``absent`` (no Google CRM dataset), ``unverified`` (the link is not configured,
            :data:`GOOGLE_LINK_UNVERIFIED_REASON`) or ``verified`` (both Google contact
            properties confirmed).

    Returns:
        The copied context.
    """
    if state == "absent":
        return replace(ctx, google_crm=None, cache={})
    crm = ctx.google_crm
    if crm is None:
        dataset = ctx.datasets.get("hubspot", _STAND_IN_CRM_DATASET)
        crm = CrmData(dataset, CrmBindings.unavailable("CRM bindings not yet verified", at=ctx.now))
    link = (
        GoogleCrmBinding(
            available=True,
            campaign_property="google_campaign_id",
            touch_date_property="google_touch_at",
        )
        if state == "verified"
        else GoogleCrmBinding(available=False, reason=GOOGLE_LINK_UNVERIFIED_REASON)
    )
    google_crm = replace(crm, bindings=replace(crm.bindings, google=link))
    return replace(ctx, google_crm=google_crm, cache={})
