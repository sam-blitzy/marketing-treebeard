"""The coverage gates of AAP 0.11.2, as the root conftest's session hook enforces them.

``backend/src/ads_engine/analysis/`` and the connector parsers need at least 90% line coverage,
configured in ``[tool.ads_engine.coverage_gates]`` of ``backend/pyproject.toml``, and the backend
overall at least 80%, configured as ``[tool.coverage.report] fail_under`` and overridden by
pytest-cov's ``--cov-fail-under``. ``pytest_sessionfinish`` in ``tests/conftest.py`` must decide
each gate on the unrounded percentage coverage.py measures: 89.96% is short of 90% and 79.96% of
80% although they round to 90.0 and 80.0 at the report precision of one digit, and a short gate
is never printed at or above its minimum.

Under ``--import-mode=importlib`` no test module can import the conftest, so these tests take the
module pytest loaded from its plugin manager and call the real hook on a stub session. Only the
coverage report is replaced, so it returns a chosen percentage per include pattern and another
for the overall total (the report with no include); everything else is real: the repository's
``pyproject.toml`` with its gates, ``fail_under`` and precision, and a coverage data file under
``tmp_path``. No test performs network or database I/O.
"""

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import coverage
import pytest
from coverage.exceptions import CoverageException, NoDataError

ANALYSIS_GATE = "*/ads_engine/analysis/*"
PARSER_GATE = "*/ads_engine/connectors/*/parser.py"
OVERALL_LABEL = "overall"

# Coverage of the gate a case does not exercise: comfortably above its minimum.
PASSING_PERCENT = 95.0

BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_CONFTEST = BACKEND_DIR / "tests" / "conftest.py"

SessionFinish = Callable[[object, int | pytest.ExitCode], None]


@dataclass
class _Reporter:
    """Terminal reporter stand-in: records separators and lines with their red markup."""

    separators: list[str] = field(default_factory=list)
    lines: list[tuple[str, bool]] = field(default_factory=list)

    def write_sep(self, sep: str, title: str) -> None:
        """Record a separator title, as ``TerminalReporter.write_sep`` would print it."""
        del sep  # Only the title matters.
        self.separators.append(title)

    def write_line(self, line: str, **markup: bool) -> None:
        """Record a line and whether it was marked red, as a failure is."""
        self.lines.append((line, markup.get("red", False)))

    def gate_lines(self) -> list[tuple[str, bool]]:
        """The per-gate verdict lines, overall included, without the report's opening blank."""
        return [(line, red) for line, red in self.lines if line.startswith("coverage gate ")]


@dataclass
class _PluginManager:
    """Plugin manager stand-in: pytest-cov is active and the reporter is registered."""

    reporter: _Reporter

    def hasplugin(self, name: str) -> bool:
        """Report pytest-cov (registered as ``_cov``) as loaded."""
        return name == "_cov"

    def get_plugin(self, name: str) -> _Reporter | None:
        """Return the recording reporter for ``terminalreporter``."""
        return self.reporter if name == "terminalreporter" else None


@dataclass
class _InvocationParams:
    """The launch directory pytest was started from."""

    dir: Path


@dataclass
class _Config:
    """Config stand-in for a coverage run that neither disables coverage nor only collects."""

    pluginmanager: _PluginManager
    invocation_params: _InvocationParams
    cov_fail_under: float | None = None

    def getoption(self, name: str, default: object = None) -> object:
        """Answer ``--no-cov`` and ``--collect-only`` as not given, ``--cov-fail-under`` as set.

        ``cov_fail_under`` is pytest-cov's dest for ``--cov-fail-under``; ``None`` means the
        option was not given, as pytest reports it.
        """
        options = {"no_cov": False, "collectonly": False, "cov_fail_under": self.cov_fail_under}
        return options.get(name, default)


@dataclass
class _Session:
    """Session stand-in whose ``exitstatus`` the hook may turn into ``TESTS_FAILED``."""

    config: _Config
    exitstatus: int | pytest.ExitCode = pytest.ExitCode.OK


class RunGates(Protocol):
    """Run the real hook on a passing session with chosen per-gate and overall coverage."""

    def __call__(
        self,
        percents: Mapping[str, float],
        *,
        overall: float | CoverageException = PASSING_PERCENT,
        cov_fail_under: float | None = None,
    ) -> tuple[_Session, _Reporter]:
        """Return the session after the hook and the reporter that recorded its lines."""
        ...


@pytest.fixture
def sessionfinish(pytestconfig: pytest.Config) -> SessionFinish:
    """The ``pytest_sessionfinish`` hook of the root conftest module pytest has loaded."""
    for plugin in pytestconfig.pluginmanager.get_plugins():
        module_file = getattr(plugin, "__file__", None)
        if isinstance(module_file, str) and Path(module_file).resolve() == ROOT_CONFTEST:
            return cast("SessionFinish", plugin.pytest_sessionfinish)
    pytest.fail(f"{ROOT_CONFTEST} is not a registered plugin of this session")


@pytest.fixture
def run_gates(
    sessionfinish: SessionFinish, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> RunGates:
    """Run the real hook on a passing session whose coverage measures ``percents`` per gate.

    The hook finds a real coverage data file through ``COVERAGE_FILE`` and loads it. The report
    it asks for one include pattern returns that pattern's percentage, unrounded; the report
    with no include (the overall total) returns ``overall``, or raises it when it is a coverage
    error. ``cov_fail_under`` is the value of pytest-cov's ``--cov-fail-under``, ``None`` when
    not given.
    """
    data_file = tmp_path / "gate.coverage"
    data = coverage.CoverageData(basename=str(data_file))
    data.add_lines({str(tmp_path / "ads_engine" / "analysis" / "rule.py"): [1]})
    data.write()
    monkeypatch.setenv("COVERAGE_FILE", str(data_file))

    def run(
        percents: Mapping[str, float],
        *,
        overall: float | CoverageException = PASSING_PERCENT,
        cov_fail_under: float | None = None,
    ) -> tuple[_Session, _Reporter]:
        def report(
            self: coverage.Coverage, *, include: list[str] | None = None, file: object
        ) -> float:
            del self, file  # The measurement is the case's, not the data file's.
            if include is None:
                if isinstance(overall, CoverageException):
                    raise overall
                return overall
            (pattern,) = include
            return percents[pattern]

        monkeypatch.setattr(coverage.Coverage, "report", report)
        reporter = _Reporter()
        config = _Config(_PluginManager(reporter), _InvocationParams(tmp_path), cov_fail_under)
        session = _Session(config)
        sessionfinish(session, pytest.ExitCode.OK)
        return session, reporter

    return run


def test_gates_and_precision_are_those_of_the_plan() -> None:
    """AAP 0.11.2 sets both per-path gates at 90% and the overall at 80%; precision is one digit.

    The cases below rely on all three: 89.96% and 79.96% round to the 90.0 and 80.0 that used to
    pass.
    """
    with (BACKEND_DIR / "pyproject.toml").open("rb") as handle:
        document = tomllib.load(handle)
    assert document["tool"]["ads_engine"]["coverage_gates"] == {ANALYSIS_GATE: 90, PARSER_GATE: 90}
    assert document["tool"]["coverage"]["report"]["fail_under"] == 80
    assert document["tool"]["coverage"]["report"]["precision"] == 1


@pytest.mark.parametrize("gate", [ANALYSIS_GATE, PARSER_GATE])
@pytest.mark.parametrize(
    ("percent", "shown", "short"),
    [
        pytest.param(89.96, "89.96", True, id="89.96-rounds-to-90.0-but-fails"),
        pytest.param(89.9999, "89.9999", True, id="89.9999-fails-shown-below-90"),
        pytest.param(89.94, "89.9", True, id="89.94-fails-at-report-precision"),
        pytest.param(90.0, "90.0", False, id="exactly-the-minimum-passes"),
        pytest.param(90.04, "90.0", False, id="90.04-passes"),
    ],
)
def test_a_gate_is_decided_on_the_unrounded_percentage(
    run_gates: RunGates,
    gate: str,
    percent: float,
    shown: str,
    short: bool,
) -> None:
    """A gate below its 90% minimum (AAP 0.11.2) fails the run however its value rounds.

    The failing line shows the value below the minimum, with digits beyond the report precision
    when rounding would reach it; a value at or above the minimum passes and leaves the run OK.
    """
    other = PARSER_GATE if gate == ANALYSIS_GATE else ANALYSIS_GATE
    session, reporter = run_gates({gate: percent, other: PASSING_PERCENT})

    verdict = " FAILED" if short else ""
    assert reporter.separators == ["coverage gates"]
    assert sorted(reporter.gate_lines()) == sorted(
        [
            (f"coverage gate {gate}: {shown}% (min 90){verdict}", short),
            (f"coverage gate {other}: {PASSING_PERCENT:.1f}% (min 90)", False),
            (f"coverage gate {OVERALL_LABEL}: {PASSING_PERCENT:.1f}% (min 80)", False),
        ]
    )
    if short:
        assert float(shown) < 90
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    else:
        assert session.exitstatus == pytest.ExitCode.OK


@pytest.mark.parametrize(
    ("overall", "shown", "short"),
    [
        pytest.param(79.96, "79.96", True, id="79.96-rounds-to-80.0-but-fails"),
        pytest.param(79.9999, "79.9999", True, id="79.9999-fails-shown-below-80"),
        pytest.param(79.94, "79.9", True, id="79.94-fails-at-report-precision"),
        pytest.param(80.0, "80.0", False, id="exactly-the-minimum-passes"),
        pytest.param(80.04, "80.0", False, id="80.04-passes"),
    ],
)
def test_the_overall_gate_is_decided_on_the_unrounded_total(
    run_gates: RunGates, overall: float, shown: str, short: bool
) -> None:
    """A backend total below the 80% of ``fail_under`` (AAP 0.11.2) fails however it rounds.

    pytest-cov alone compares the total rounded to one digit, so 79.96% would pass it. The hook
    prints the overall line after the per-path gates, from the report with no include, and a
    short total turns a passing run into ``TESTS_FAILED`` with the value shown below 80%.
    """
    session, reporter = run_gates(
        {ANALYSIS_GATE: PASSING_PERCENT, PARSER_GATE: PASSING_PERCENT}, overall=overall
    )

    verdict = " FAILED" if short else ""
    assert reporter.separators == ["coverage gates"]
    assert reporter.gate_lines() == [
        (f"coverage gate {ANALYSIS_GATE}: {PASSING_PERCENT:.1f}% (min 90)", False),
        (f"coverage gate {PARSER_GATE}: {PASSING_PERCENT:.1f}% (min 90)", False),
        (f"coverage gate {OVERALL_LABEL}: {shown}% (min 80){verdict}", short),
    ]
    if short:
        assert float(shown) < 80
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    else:
        assert session.exitstatus == pytest.ExitCode.OK


@pytest.mark.parametrize(
    ("cov_fail_under", "overall", "line", "short"),
    [
        pytest.param(
            85, 82.0, "coverage gate overall: 82.0% (min 85) FAILED", True, id="raised-to-85"
        ),
        pytest.param(
            85.5,
            85.46,
            "coverage gate overall: 85.46% (min 85.5) FAILED",
            True,
            id="fractional-85.5-unrounded",
        ),
        pytest.param(75, 79.96, "coverage gate overall: 80.0% (min 75)", False, id="lowered-to-75"),
    ],
)
def test_cov_fail_under_overrides_the_configured_overall_minimum(
    run_gates: RunGates, cov_fail_under: float, overall: float, line: str, short: bool
) -> None:
    """``--cov-fail-under`` replaces ``fail_under = 80`` for the overall gate, as for pytest-cov.

    A raised minimum fails a total that 80% would pass, a fractional one is still decided on the
    unrounded total, and a lowered one passes a total that 80% would fail.
    """
    session, reporter = run_gates(
        {ANALYSIS_GATE: PASSING_PERCENT, PARSER_GATE: PASSING_PERCENT},
        overall=overall,
        cov_fail_under=cov_fail_under,
    )

    assert reporter.gate_lines()[-1] == (line, short)
    expected = pytest.ExitCode.TESTS_FAILED if short else pytest.ExitCode.OK
    assert session.exitstatus == expected


def test_a_zero_overall_minimum_means_no_overall_gate(run_gates: RunGates) -> None:
    """``--cov-fail-under=0`` disables the overall gate: no overall line, and the run stays OK.

    The per-path gates are unaffected and still print their lines.
    """
    session, reporter = run_gates(
        {ANALYSIS_GATE: PASSING_PERCENT, PARSER_GATE: PASSING_PERCENT},
        overall=10.0,
        cov_fail_under=0,
    )

    assert reporter.gate_lines() == [
        (f"coverage gate {ANALYSIS_GATE}: {PASSING_PERCENT:.1f}% (min 90)", False),
        (f"coverage gate {PARSER_GATE}: {PASSING_PERCENT:.1f}% (min 90)", False),
    ]
    assert session.exitstatus == pytest.ExitCode.OK


def test_the_overall_gate_is_decided_after_a_short_per_path_gate(run_gates: RunGates) -> None:
    """A short per-path gate does not stop the overall gate: every short gate is reported."""
    session, reporter = run_gates(
        {ANALYSIS_GATE: 89.96, PARSER_GATE: PASSING_PERCENT}, overall=79.96
    )

    assert [line for line, red in reporter.gate_lines() if red] == [
        f"coverage gate {ANALYSIS_GATE}: 89.96% (min 90) FAILED",
        f"coverage gate {OVERALL_LABEL}: 79.96% (min 80) FAILED",
    ]
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize(
    ("error", "line"),
    [
        pytest.param(
            NoDataError("No data to report."),
            "coverage gate overall: 0.0% (min 80) FAILED",
            id="no-measured-file-counts-as-0",
        ),
        pytest.param(
            CoverageException("No source for code: 'gone.py'."),
            "coverage gate overall: report failed (No source for code: 'gone.py'.)",
            id="failed-report-is-short",
        ),
    ],
)
def test_an_overall_report_that_cannot_measure_fails_the_run(
    run_gates: RunGates, error: CoverageException, line: str
) -> None:
    """The overall gate handles coverage errors as the per-path gates do, never as a pass."""
    session, reporter = run_gates(
        {ANALYSIS_GATE: PASSING_PERCENT, PARSER_GATE: PASSING_PERCENT}, overall=error
    )

    assert reporter.gate_lines()[-1] == (line, True)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
