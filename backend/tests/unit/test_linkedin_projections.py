"""Tests of the packaged LinkedIn Lead Sync projection and of its generator.

Covers :mod:`ads_engine.connectors.linkedin.projections` (``lead_fields_from_contracts``,
``render_generated_module`` and ``main`` with and without ``--check``), the generated
:mod:`ads_engine.connectors.linkedin._projections_generated`, which must be exactly what the
generator renders from ``backend/tests/contracts/linkedin``, and the client's ``LEAD_FIELDS``,
which comes from that module, so the client imports from a working directory without the
contracts.

Temporary contract trees are copies of the real lead-response contracts with ``request.fields``
replaced. No test rewrites the real generated module, touches the network or needs a database.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ads_engine.connectors.linkedin import _projections_generated
from ads_engine.connectors.linkedin import client as client_module
from ads_engine.connectors.linkedin import projections as projections_module
from ads_engine.connectors.linkedin.client import LEAD_FIELDS
from ads_engine.connectors.linkedin.projections import (
    CHECK_COMMAND,
    DEFAULT_CONTRACTS_DIR,
    GENERATED_MODULE,
    LEAD_FIELDS_CONTRACTS,
    REGENERATE_COMMAND,
    lead_fields_from_contracts,
    main,
    render_generated_module,
)

pytestmark = pytest.mark.contract

BACKEND = Path(__file__).resolve().parents[2]
CONTRACTS = BACKEND / "tests" / "contracts" / "linkedin"

# A projection other than the shipped one, made only of characters valid in a raw query.
ALTERED_FIELDS = "id,submittedAt,testLead,versionedLeadGenFormUrn,formResponse,form:(id,content)"
_UNSET = object()
# The assignment the generated module holds; with its quotes it fits one line up to 77 chars.
_ASSIGNMENT = "LEAD_FIELDS: Final = "
_LINE_LENGTH = 100


def _contracts_tree(root: Path, *fields: object) -> Path:
    """Write the two lead-response contracts under ``root/tests/contracts/linkedin``.

    The n-th value of ``fields`` replaces ``request.fields`` of the n-th contract of
    :data:`LEAD_FIELDS_CONTRACTS`; :data:`_UNSET` removes the key. Contracts without a value
    are copied unchanged. Returns the ``linkedin`` directory.
    """
    directory = root / "tests" / "contracts" / "linkedin"
    directory.mkdir(parents=True)
    for index, surface in enumerate(LEAD_FIELDS_CONTRACTS):
        contract = json.loads((CONTRACTS / f"{surface}.json").read_text(encoding="utf-8"))
        if index < len(fields):
            if fields[index] is _UNSET:
                del contract["request"]["fields"]
            else:
                contract["request"]["fields"] = fields[index]
        (directory / f"{surface}.json").write_text(json.dumps(contract), encoding="utf-8")
    return directory


def _rendered_value(source: str) -> object:
    """Return the value the ``LEAD_FIELDS`` assignment of a generated module's source holds."""
    module = ast.parse(source)
    assignment = module.body[-1]
    assert isinstance(assignment, ast.AnnAssign)
    assert isinstance(assignment.target, ast.Name)
    assert assignment.target.id == "LEAD_FIELDS"
    assert assignment.value is not None
    return ast.literal_eval(assignment.value)


# ---------------------------------------------------------------------------------------------
# The generated module and the client's LEAD_FIELDS
# ---------------------------------------------------------------------------------------------


def test_the_generated_module_is_current_with_the_contracts() -> None:
    """The packaged module is byte for byte what the generator renders from the contracts."""
    expected = render_generated_module(lead_fields_from_contracts(CONTRACTS))
    assert GENERATED_MODULE.read_bytes() == expected.encode("utf-8"), (
        f"{GENERATED_MODULE.name} is stale; regenerate it from backend/ with: {REGENERATE_COMMAND}"
    )


def test_the_generator_writes_the_module_the_package_imports() -> None:
    """The default output is the imported generated module, which holds the contracts' value."""
    assert GENERATED_MODULE.resolve() == Path(_projections_generated.__file__).resolve()
    assert lead_fields_from_contracts(CONTRACTS) == _projections_generated.LEAD_FIELDS


def test_the_client_lead_fields_is_the_generated_projection_both_contracts_record() -> None:
    """``LEAD_FIELDS`` is public, equals both contracts and is the value both requests send."""
    for surface in LEAD_FIELDS_CONTRACTS:
        contract = json.loads((CONTRACTS / f"{surface}.json").read_text(encoding="utf-8"))
        assert contract["request"]["fields"] == LEAD_FIELDS
    assert "LEAD_FIELDS" in client_module.__all__
    assert LEAD_FIELDS is _projections_generated.LEAD_FIELDS
    assert client_module._LEAD_FIELDS_QUERY_VALUE is LEAD_FIELDS


def test_the_client_source_holds_no_copy_of_the_projection() -> None:
    """No string literal of ``client.py`` spells the projection; it is only imported."""
    source = Path(client_module.__file__).read_text(encoding="utf-8")
    literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert LEAD_FIELDS not in literals
    assert not any("formResponse,form:(" in literal for literal in literals)


def test_the_client_imports_from_a_working_directory_without_the_contracts(
    tmp_path: Path,
) -> None:
    """A fresh interpreter in an empty directory imports the client and its sync callers."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import ads_engine.connectors.linkedin.client as client\n"
            "import ads_engine.sync.lead_match\n"
            "import ads_engine.sync.mode\n"
            "sys.stdout.write(client.LEAD_FIELDS)\n",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == LEAD_FIELDS
    assert list(tmp_path.iterdir()) == []


def test_the_generator_imports_only_the_standard_library() -> None:
    """The generator never loads the product (httpx, settings, simulation) when it runs."""
    source = Path(projections_module.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= sys.stdlib_module_names


# ---------------------------------------------------------------------------------------------
# main: --check and write mode
# ---------------------------------------------------------------------------------------------


def test_check_passes_on_the_repository_with_the_default_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """From ``backend/`` the default contracts directory and module are current."""
    monkeypatch.chdir(BACKEND)
    assert (BACKEND / DEFAULT_CONTRACTS_DIR).resolve() == CONTRACTS
    assert main(["--check"]) == 0
    assert capsys.readouterr().out == f"{GENERATED_MODULE} is current\n"


def test_the_documented_check_command_runs_the_module_as_a_script() -> None:
    """``python -m ads_engine.connectors.linkedin.projections --check`` exits 0 in backend/."""
    assert f"{REGENERATE_COMMAND} --check" == CHECK_COMMAND
    assert CHECK_COMMAND.endswith(" python -m ads_engine.connectors.linkedin.projections --check")
    completed = subprocess.run(
        [sys.executable, "-m", "ads_engine.connectors.linkedin.projections", "--check"],
        cwd=BACKEND,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{GENERATED_MODULE} is current\n"


def test_check_fails_naming_the_regenerate_command_when_the_contracts_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Changed contracts make the real module stale; ``--check`` exits 1 and writes nothing."""
    directory = _contracts_tree(tmp_path, ALTERED_FIELDS, ALTERED_FIELDS)
    before = GENERATED_MODULE.read_bytes()
    assert main(["--check", "--contracts", str(directory)]) == 1
    assert capsys.readouterr().err == (
        f"{GENERATED_MODULE} is stale; regenerate it from backend/ with: {REGENERATE_COMMAND}\n"
    )
    assert GENERATED_MODULE.read_bytes() == before


def test_check_reports_a_missing_module_without_creating_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A generated module that does not exist is reported missing, exit 1."""
    output = tmp_path / "generated.py"
    assert main(["--check", "--contracts", str(CONTRACTS), "--output", str(output)]) == 1
    assert f"{output} is missing; regenerate it" in capsys.readouterr().err
    assert not output.exists()


def test_write_mode_renders_the_contracts_then_leaves_a_current_module_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Write mode replaces a stale module, after which ``--check`` passes and nothing rewrites."""
    directory = _contracts_tree(tmp_path, ALTERED_FIELDS, ALTERED_FIELDS)
    output = tmp_path / "generated.py"
    output.write_text('LEAD_FIELDS = "stale"\n', encoding="utf-8")
    arguments = ["--contracts", str(directory), "--output", str(output)]

    assert main(arguments) == 0
    assert capsys.readouterr().out == f"wrote {output}\n"
    assert output.read_bytes() == render_generated_module(ALTERED_FIELDS).encode("utf-8")
    assert _rendered_value(output.read_text(encoding="utf-8")) == ALTERED_FIELDS

    written = output.stat().st_mtime_ns
    assert main(["--check", *arguments]) == 0
    assert main(arguments) == 0
    assert capsys.readouterr().out == f"{output} is current\n" * 2
    assert output.stat().st_mtime_ns == written


@pytest.mark.parametrize("check", [False, True], ids=["write", "check"])
def test_invalid_contracts_exit_2_and_write_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], check: bool
) -> None:
    """A missing contract is reported by path, exit 2, and the module is left untouched."""
    directory = _contracts_tree(tmp_path)
    (directory / "lead_form_responses_batch.json").unlink()
    output = tmp_path / "generated.py"
    output.write_text("previous\n", encoding="utf-8")
    arguments = ["--contracts", str(directory), "--output", str(output)]
    assert main(["--check", *arguments] if check else arguments) == 2
    error = capsys.readouterr().err
    assert str(directory / "lead_form_responses_batch.json") in error
    assert output.read_text(encoding="utf-8") == "previous\n"


def test_an_unreadable_module_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An output path that cannot be read as a file is an error naming it, exit 2."""
    output = tmp_path / "generated.py"
    output.mkdir()
    assert main(["--contracts", str(CONTRACTS), "--output", str(output)]) == 2
    assert capsys.readouterr().err.startswith(f"{output} cannot be read (")


def test_an_unwritable_module_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An output path whose directory does not exist cannot be written, exit 2."""
    output = tmp_path / "absent" / "generated.py"
    assert main(["--contracts", str(CONTRACTS), "--output", str(output)]) == 2
    assert capsys.readouterr().err.startswith(f"{output} cannot be written (")
    assert not output.exists()


# ---------------------------------------------------------------------------------------------
# render_generated_module
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        LEAD_FIELDS,
        ALTERED_FIELDS,
        "a" * 77,
        "a" * 78,
        "a" * 94,
        "a" * 95,
        "id," + "b" * 250 + ",c",
        ",".join(["formResponse"] * 30),
    ],
    ids=[
        "shipped",
        "short",
        "one-line-max",
        "parenthesized",
        "one-piece",
        "two-pieces",
        "long-run",
        "many",
    ],
)
def test_render_round_trips_within_the_line_length(value: str) -> None:
    """The rendered module holds the value exactly, in lines of at most 100 characters."""
    source = render_generated_module(value)
    assert render_generated_module(value) == source
    assert source.endswith(")\n") or source.endswith('"\n')
    assert not source.endswith("\n\n")
    assert max(len(line) for line in source.splitlines()) <= _LINE_LENGTH
    assert _rendered_value(source) == value
    one_line = f'{_ASSIGNMENT}"{value}"'
    assert (one_line in source.splitlines()) is (len(one_line) <= _LINE_LENGTH)
    docstring = ast.get_docstring(ast.parse(source))
    assert docstring is not None
    assert "do not edit by hand" in docstring
    assert REGENERATE_COMMAND in docstring
    assert "lead_form_responses.json" in docstring
    assert "lead_form_responses_batch.json" in docstring


def test_render_keeps_the_shipped_projection_split_after_commas() -> None:
    """A wrapped value is split only after commas when every comma run fits a line."""
    source = render_generated_module(LEAD_FIELDS)
    pieces = [
        line.strip()[1:-1]
        for line in source.splitlines()
        if line.startswith('    "') and line.endswith('"')
    ]
    assert len(pieces) > 1
    assert "".join(pieces) == LEAD_FIELDS
    assert all(piece.endswith(",") for piece in pieces[:-1])


@pytest.mark.parametrize(
    "value",
    ["", 'id"', "id\\", "id%2C", "id name", "id\n", "idé", "id;x"],
    ids=["empty", "quote", "backslash", "percent", "space", "newline", "non-ascii", "semicolon"],
)
def test_render_rejects_text_a_raw_query_literal_cannot_hold(value: str) -> None:
    """Only raw query text is rendered, so the literal never needs escaping."""
    with pytest.raises(ValueError, match=r"must be raw Rest\.li query text"):
        render_generated_module(value)


# ---------------------------------------------------------------------------------------------
# lead_fields_from_contracts
# ---------------------------------------------------------------------------------------------


def test_lead_fields_from_contracts_reads_the_shared_projection(tmp_path: Path) -> None:
    """Both contracts recording one projection yield it, whatever it is."""
    assert lead_fields_from_contracts(CONTRACTS) == LEAD_FIELDS
    directory = _contracts_tree(tmp_path, ALTERED_FIELDS, ALTERED_FIELDS)
    assert lead_fields_from_contracts(directory) == ALTERED_FIELDS


@pytest.mark.parametrize("missing", LEAD_FIELDS_CONTRACTS)
def test_lead_fields_missing_contract_fails_naming_its_path(tmp_path: Path, missing: str) -> None:
    """Either contract missing is an error naming that file and how to point the tool at it."""
    directory = _contracts_tree(tmp_path)
    (directory / f"{missing}.json").unlink()
    with pytest.raises(FileNotFoundError) as raised:
        lead_fields_from_contracts(directory)
    assert str(raised.value) == (
        f"LinkedIn contract file not found: {directory / f'{missing}.json'}. Run the generator "
        "from backend/ or pass --contracts."
    )


def test_lead_fields_contracts_that_disagree_fail_without_echoing_either_value(
    tmp_path: Path,
) -> None:
    """Two different projections are an error naming both files and neither value."""
    directory = _contracts_tree(tmp_path, LEAD_FIELDS, ALTERED_FIELDS)
    with pytest.raises(ValueError, match=r"record different request\.fields") as raised:
        lead_fields_from_contracts(directory)
    message = str(raised.value)
    assert str(directory / "lead_form_responses.json") in message
    assert str(directory / "lead_form_responses_batch.json") in message
    assert LEAD_FIELDS not in message
    assert ALTERED_FIELDS not in message


@pytest.mark.parametrize("surface", LEAD_FIELDS_CONTRACTS)
@pytest.mark.parametrize(
    "fields",
    [_UNSET, None, 7, True, ["id"], {"id": "x"}, ""],
    ids=["absent", "null", "integer", "boolean", "list", "object", "empty"],
)
def test_lead_fields_must_be_a_non_empty_string(
    tmp_path: Path, surface: str, fields: object
) -> None:
    """A missing, non-string or empty ``request.fields`` in either contract is an error."""
    values: list[object] = [LEAD_FIELDS, LEAD_FIELDS]
    values[LEAD_FIELDS_CONTRACTS.index(surface)] = fields
    directory = _contracts_tree(tmp_path, *values)
    with pytest.raises(ValueError, match=r"request\.fields must be a non-empty string") as raised:
        lead_fields_from_contracts(directory)
    assert str(raised.value).startswith(f"{directory / f'{surface}.json'}: ")


@pytest.mark.parametrize(
    "fields",
    [
        "ownerInfo%2Cid",
        "id,%",
        "ownerInfo, id",
        " id",
        "id\n",
        '"id"',
        "id&count=1",
        "id=1",
        "id#frag",
        "id+name",
        "form:(id;content)",
    ],
)
def test_lead_fields_must_be_raw_query_text(tmp_path: Path, fields: str) -> None:
    """A percent escape, space or other character the raw query cannot carry is an error."""
    directory = _contracts_tree(tmp_path, fields, fields)
    with pytest.raises(ValueError, match=r"must be raw Rest\.li query text") as raised:
        lead_fields_from_contracts(directory)
    message = str(raised.value)
    assert message.startswith(f"{directory / 'lead_form_responses.json'}: ")
    assert fields not in message


@pytest.mark.parametrize(
    "document",
    [b"{not json", b"\xff\xfe{}", b"[]", b'"fields"', b'{"request": []}', b"{}"],
    ids=["invalid-json", "not-utf-8", "array", "string", "request-array", "no-request"],
)
def test_lead_fields_contract_that_is_no_json_object_with_fields_fails(
    tmp_path: Path, document: bytes
) -> None:
    """A contract that is not UTF-8 JSON, or holds no ``request.fields`` string, is an error."""
    directory = _contracts_tree(tmp_path)
    path = directory / "lead_form_responses_batch.json"
    path.write_bytes(document)
    with pytest.raises(ValueError, match=r"not UTF-8 JSON|must be a non-empty string") as raised:
        lead_fields_from_contracts(directory)
    assert str(path) in str(raised.value)


def test_lead_fields_unreadable_contract_fails_naming_its_path(tmp_path: Path) -> None:
    """A contract path that exists but cannot be read as a file is an OS error naming it."""
    directory = _contracts_tree(tmp_path)
    path = directory / "lead_form_responses.json"
    path.unlink()
    path.mkdir()
    with pytest.raises(OSError, match="cannot be read") as raised:
        lead_fields_from_contracts(directory)
    assert not isinstance(raised.value, FileNotFoundError)
    assert str(path) in str(raised.value)
