"""Generator of the packaged LinkedIn Lead Sync ``fields`` projection.

The LinkedIn contract files under ``backend/tests/contracts/linkedin/`` are the single authority
for the requests the connector sends; the simulator validates requests against the same files.
They are test files, outside the ``src/ads_engine`` package that the wheel and the image ship,
so the client cannot read them at run time. This tool reads the projection that
``lead_form_responses.json`` and ``lead_form_responses_batch.json`` both record as
``request.fields`` (LinkedIn's Lead Sync API migration guide projection, verbatim) and writes it
into :mod:`ads_engine.connectors.linkedin._projections_generated`, which ships with the package
and which :mod:`ads_engine.connectors.linkedin.client` imports as ``LEAD_FIELDS``. The
projection therefore has one source, the contracts, and no hand-maintained copy: a changed
contract is followed by regenerating, and a unit test fails while the generated module is stale.

Run from ``backend/``::

    uv run --frozen python -m ads_engine.connectors.linkedin.projections
    uv run --frozen python -m ads_engine.connectors.linkedin.projections --check

The first command rewrites the generated module when it differs from the contracts; the second
only compares, exiting 1 when the module is stale or missing. Both exit 2, writing nothing, when
a contract is missing, unreadable or invalid. ``--contracts`` and ``--output`` select another
contracts directory and generated file.

The tool uses the standard library only and imports nothing from the product, so running it
never loads httpx, the settings or the simulation.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

__all__ = [
    "CHECK_COMMAND",
    "DEFAULT_CONTRACTS_DIR",
    "GENERATED_MODULE",
    "LEAD_FIELDS_CONTRACTS",
    "REGENERATE_COMMAND",
    "lead_fields_from_contracts",
    "main",
    "render_generated_module",
]

DEFAULT_CONTRACTS_DIR: Final = Path("tests") / "contracts" / "linkedin"
"""The LinkedIn contracts directory, relative to ``backend/``, the tool's working directory."""

GENERATED_MODULE: Final = Path(__file__).with_name("_projections_generated.py")
"""The generated module this tool writes and checks, next to this module."""

LEAD_FIELDS_CONTRACTS: Final = ("lead_form_responses", "lead_form_responses_batch")
"""The contract surfaces whose ``request.fields`` record the Lead Sync projection, in read order."""

REGENERATE_COMMAND: Final = "uv run --frozen python -m ads_engine.connectors.linkedin.projections"
"""The command, run from ``backend/``, that regenerates the generated module."""

CHECK_COMMAND: Final = f"{REGENERATE_COMMAND} --check"
"""The command, run from ``backend/``, that exits 1 while the generated module is stale."""

# The raw Rest.li query text a projection may hold: RFC 3986 unreserved characters, the Rest.li
# structural characters and '*'. No '%': the client sends the value raw, so it must already be
# its own single URL encoding, and it never needs escaping inside a Python string literal.
_RAW_QUERY_TEXT_RE: Final = re.compile(r"[A-Za-z0-9\-._~():,*]+")
_RAW_QUERY_TEXT_RULE: Final = (
    "raw Rest.li query text, only [A-Za-z0-9-._~():,*] with no percent escape"
)

# The generated module's code lines may not exceed ruff's line length (backend/pyproject.toml).
_LINE_LENGTH: Final = 100
_INDENT: Final = "    "
_ASSIGNMENT: Final = "LEAD_FIELDS: Final = "

# Everything the generated module holds before its assignment. The backslash keeps the opening
# docstring quotes at the start of the generated file.
_HEADER: Final = f'''\
"""Lead Sync ``fields`` projection generated from the LinkedIn contracts; do not edit by hand.

:mod:`ads_engine.connectors.linkedin.projections` writes this module from the
``request.fields`` that ``backend/tests/contracts/linkedin/lead_form_responses.json`` and
``lead_form_responses_batch.json`` both record; the contracts are the single authority for the
projection. After changing them, regenerate this file from ``backend/`` with::

    {REGENERATE_COMMAND}

The same command with ``--check`` exits 1, and a unit test fails, while this file is stale.
"""

from typing import Final

'''


def _contract_fields(path: Path) -> str:
    """Return the validated ``request.fields`` of one contract file.

    Args:
        path: The contract file.

    Returns:
        Its ``request.fields``.

    Raises:
        FileNotFoundError: When the file is missing.
        OSError: When the file exists but cannot be read.
        ValueError: When the file is not UTF-8 JSON or its ``request.fields`` is not a
            non-empty string of raw query text. The message names the path, never a value
            read from the file.
    """
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"LinkedIn contract file not found: {path}. Run the generator from backend/ or "
            "pass --contracts."
        ) from None
    except OSError as exc:
        raise OSError(
            f"LinkedIn contract file {path} cannot be read ({type(exc).__name__})"
        ) from None
    except ValueError:
        # json.JSONDecodeError or UnicodeDecodeError; the message names the path only.
        raise ValueError(f"LinkedIn contract file {path} is not UTF-8 JSON") from None
    request: object = document.get("request") if isinstance(document, dict) else None
    fields: object = request.get("fields") if isinstance(request, dict) else None
    if not isinstance(fields, str) or not fields:
        raise ValueError(f"{path}: request.fields must be a non-empty string")
    if not _RAW_QUERY_TEXT_RE.fullmatch(fields):
        raise ValueError(f"{path}: request.fields must be {_RAW_QUERY_TEXT_RULE}")
    return fields


def lead_fields_from_contracts(directory: Path) -> str:
    """Return the Lead Sync ``fields`` projection both lead-response contracts record.

    ``lead_form_responses.json`` and ``lead_form_responses_batch.json`` in ``directory`` each
    hold it as ``request.fields``. Each value must be a non-empty string of raw Rest.li query
    text, only ``[A-Za-z0-9-._~():,*]`` with no percent escape, because the client sends it
    raw, and the two values must be identical. An error names the files, never a value read
    from them.

    Args:
        directory: The LinkedIn contracts directory, ``backend/tests/contracts/linkedin``.

    Returns:
        The projection both contracts record.

    Raises:
        FileNotFoundError: When a contract file is missing.
        OSError: When a contract file exists but cannot be read.
        ValueError: When a file is not UTF-8 JSON, its ``request.fields`` is not a non-empty
            raw query value, or the two files record different projections.
    """
    first_path, second_path = (directory / f"{surface}.json" for surface in LEAD_FIELDS_CONTRACTS)
    first = _contract_fields(first_path)
    second = _contract_fields(second_path)
    if first != second:
        raise ValueError(
            f"{first_path} and {second_path} record different request.fields; the two "
            "lead-response contracts must record one projection"
        )
    return first


def _wrapped(text: str, width: int) -> list[str]:
    """Split ``text`` into pieces of at most ``width`` characters, each ending after a comma.

    A run longer than ``width`` without a comma is cut at ``width``. Joining the pieces returns
    ``text``.
    """
    pieces: list[str] = []
    current = ""
    for part in re.findall(r"[^,]*,|[^,]+", text):
        while len(part) > width:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(part[:width])
            part = part[width:]
        if current and len(current) + len(part) > width:
            pieces.append(current)
            current = ""
        current += part
    if current:
        pieces.append(current)
    return pieces


def render_generated_module(lead_fields: str) -> str:
    """Return the exact source text of the generated projections module.

    The text is deterministic and already ``ruff format`` clean: the assignment stays on one
    line when it fits in 100 characters, and otherwise the value is split, after commas, into
    implicitly concatenated string literals inside parentheses.

    Args:
        lead_fields: The projection, as :func:`lead_fields_from_contracts` returns it.

    Returns:
        The module source, ending in one newline.

    Raises:
        ValueError: When ``lead_fields`` is not a non-empty string of raw query text, which a
            string literal could not hold unescaped.
    """
    if not _RAW_QUERY_TEXT_RE.fullmatch(lead_fields):
        raise ValueError(f"the projection must be {_RAW_QUERY_TEXT_RULE}")
    single = f'{_ASSIGNMENT}"{lead_fields}"'
    if len(single) <= _LINE_LENGTH:
        return f"{_HEADER}{single}\n"
    width = _LINE_LENGTH - len(_INDENT) - len('""')
    literals = "".join(f'{_INDENT}"{piece}"\n' for piece in _wrapped(lead_fields, width))
    return f"{_HEADER}{_ASSIGNMENT}(\n{literals})\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Regenerate the projections module from the contracts, or with ``--check`` verify it.

    Args:
        argv: Command-line arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        ``0`` when the module was written or is current, ``1`` when ``--check`` finds it stale
        or missing, and ``2`` when a contract is missing, unreadable or invalid, or the module
        cannot be read or written. Nothing is written unless the result is ``0``.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ads_engine.connectors.linkedin.projections",
        description=(
            "Write the LinkedIn Lead Sync fields projection that the lead-response contracts "
            "record into the packaged generated module."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare only, and exit 1 when the generated module is stale or missing",
    )
    parser.add_argument(
        "--contracts",
        type=Path,
        default=DEFAULT_CONTRACTS_DIR,
        help=f"LinkedIn contracts directory (default: {DEFAULT_CONTRACTS_DIR}, from backend/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=GENERATED_MODULE,
        help="generated module path (default: the packaged _projections_generated.py)",
    )
    args = parser.parse_args(argv)
    contracts: Path = args.contracts
    output: Path = args.output

    try:
        expected = render_generated_module(lead_fields_from_contracts(contracts)).encode("utf-8")
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    try:
        current: bytes | None = output.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError as exc:
        sys.stderr.write(f"{output} cannot be read ({type(exc).__name__})\n")
        return 2

    if current == expected:
        sys.stdout.write(f"{output} is current\n")
        return 0
    if args.check:
        state = "missing" if current is None else "stale"
        sys.stderr.write(
            f"{output} is {state}; regenerate it from backend/ with: {REGENERATE_COMMAND}\n"
        )
        return 1
    try:
        output.write_bytes(expected)
    except OSError as exc:
        sys.stderr.write(f"{output} cannot be written ({type(exc).__name__})\n")
        return 2
    sys.stdout.write(f"wrote {output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
