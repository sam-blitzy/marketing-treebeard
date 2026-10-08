"""Unit tests: ``sync/mode.py`` takes its mode vocabulary from the schema, never from a copy.

``ads_engine.db.models.ops.MODES`` is the source of the ``mode`` CHECK constraints on
``source_dataset`` and ``source_connection``. ``sync/mode.py`` narrows every stored mode
through the shared :data:`~ads_engine.sync.datasets.Mode` literal, and its import-time guard
refuses to load once that literal and ``MODES`` differ. No database is needed.
"""

import ast
import importlib.util
import re
from pathlib import Path
from typing import get_args

import pytest

from ads_engine.db.models import ops
from ads_engine.db.models.ops import MODES
from ads_engine.sync import mode as mode_module
from ads_engine.sync.datasets import Mode

_MODE_SOURCE: Path = Path(mode_module.__file__)


@pytest.mark.parametrize("value", MODES)
def test_as_mode_returns_every_schema_mode_unchanged(value: str) -> None:
    narrowed = mode_module._as_mode(value)

    assert narrowed == value
    assert type(narrowed) is str


@pytest.mark.parametrize("value", ["mock", "", "LIVE", " live", "simulated ", "auto"])
def test_as_mode_rejects_unknown_mode_naming_allowed_modes(value: str) -> None:
    with pytest.raises(ValueError, match=re.escape(f"expected one of {list(MODES)!r}")) as info:
        mode_module._as_mode(value)

    message = str(info.value)
    assert message.startswith(f"unknown mode {value!r}")
    for allowed in MODES:
        assert repr(allowed) in message


def test_mode_literal_equals_schema_modes() -> None:
    assert get_args(Mode) == tuple(MODES)
    assert mode_module.Mode is Mode
    assert mode_module.MODES is MODES


def test_mode_module_defines_no_private_mode_tuple() -> None:
    assert not hasattr(mode_module, "_MODES")
    tree = ast.parse(_MODE_SOURCE.read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "_MODES" not in names


@pytest.mark.parametrize(
    "drifted",
    [
        ("live", "simulated", "mock"),
        ("live", "mocked"),
        ("simulated", "live"),
    ],
)
def test_import_guard_rejects_schema_mode_drift(
    monkeypatch: pytest.MonkeyPatch, drifted: tuple[str, ...]
) -> None:
    monkeypatch.setattr(ops, "MODES", drifted)
    spec = importlib.util.spec_from_file_location("_sync_mode_vocabulary_probe", _MODE_SOURCE)
    assert spec is not None
    assert spec.loader is not None
    probe = importlib.util.module_from_spec(spec)

    expected = f"modes {drifted!r} no longer match sync/mode.py"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        spec.loader.exec_module(probe)
