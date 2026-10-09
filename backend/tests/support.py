"""Shared, pure root test support for the acceptance suite.

Generic readers of a recommendation's evidence and the numeric normalisation used to compare
shown values with expected ones. Acceptance modules import them from here
(``from tests.support import ...``) rather than keeping their own copies. Rule-specific oracles,
which restate a rule independently of the code under test, stay in the module that tests that
rule.

Pure: no fixture, database, network or clock.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ads_engine.analysis.context import Candidate


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
