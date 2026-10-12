"""Unit tests for ``ads_engine.analysis.segment_labels``: LinkedIn's standardized segment names.

They pin the names C3a shows for the stored demographic segment URNs: known URNs of each
stored facet read as LinkedIn's en_US standardized name, and a value the fetched data does not
name (an unknown code, an unknown facet, another facet's URN or a value that is no URN) keeps
its stored text, never a guess and never blank. The table holds exactly LinkedIn's 26 job
functions, 10 seniorities and 494 industries, by numeric id, and is read-only. Pure: no
database and no network.
"""

import ast
import doctest
import inspect
import re
import sys
from collections.abc import Mapping

import pytest

from ads_engine.analysis import segment_labels
from ads_engine.analysis.segment_labels import SEGMENT_VALUE_LABELS, segment_value_label
from ads_engine.db.models.facts import SEGMENT_FACETS

URN_KINDS = {"job_function": "function", "seniority": "seniority", "industry": "industry"}
"""The URN entity type LinkedIn uses for each stored facet: ``urn:li:{type}:{id}``."""


@pytest.mark.parametrize(
    ("facet", "value", "label"),
    [
        ("seniority", "urn:li:seniority:6", "Director"),
        ("seniority", "urn:li:seniority:1", "Unpaid"),
        ("seniority", "urn:li:seniority:10", "Owner"),
        ("job_function", "urn:li:function:15", "Marketing"),
        ("job_function", "urn:li:function:1", "Accounting"),
        ("job_function", "urn:li:function:26", "Customer Success and Support"),
        ("industry", "urn:li:industry:4", "Software Development"),
        ("industry", "urn:li:industry:1", "Defense and Space Manufacturing"),
        ("industry", "urn:li:industry:1530001", "Artificial Intelligence"),
    ],
)
def test_a_known_urn_reads_as_linkedins_standardized_name(
    facet: str, value: str, label: str
) -> None:
    assert segment_value_label(facet, value) == label


@pytest.mark.parametrize(
    ("facet", "value"),
    [
        ("seniority", "urn:li:seniority:999"),
        ("seniority", "urn:li:seniority:0"),
        ("industry", "urn:li:industry:2"),
        ("job_function", "urn:li:function:27"),
        ("seniority", "urn:li:function:15"),
        ("job_function", "urn:li:seniority:6"),
        ("seniority", "Director"),
        ("seniority", "URN:LI:SENIORITY:6"),
        ("seniority", " urn:li:seniority:6"),
        ("geo", "urn:li:geo:103644278"),
        ("MEMBER_SENIORITY", "urn:li:seniority:6"),
        ("", "urn:li:seniority:6"),
    ],
    ids=[
        "unknown-code",
        "code-zero",
        "industry-gap",
        "newer-function",
        "other-facets-urn",
        "seniority-urn-under-function",
        "not-a-urn",
        "case-differs",
        "padded",
        "unknown-facet",
        "pivot-name-not-facet",
        "blank-facet",
    ],
)
def test_a_value_linkedin_names_none_for_keeps_its_stored_text(facet: str, value: str) -> None:
    assert segment_value_label(facet, value) == value


def test_the_table_holds_linkedins_counts_for_exactly_the_stored_facets() -> None:
    assert tuple(SEGMENT_VALUE_LABELS) == SEGMENT_FACETS
    assert {facet: len(names) for facet, names in SEGMENT_VALUE_LABELS.items()} == {
        "job_function": 26,
        "seniority": 10,
        "industry": 494,
    }


@pytest.mark.parametrize("facet", SEGMENT_FACETS)
def test_every_entry_is_a_facet_urn_by_ascending_id_with_a_trimmed_name(facet: str) -> None:
    pattern = re.compile(rf"urn:li:{URN_KINDS[facet]}:([1-9][0-9]*)")
    ids: list[int] = []
    for urn, name in SEGMENT_VALUE_LABELS[facet].items():
        match = pattern.fullmatch(urn)
        assert match is not None, urn
        ids.append(int(match.group(1)))
        assert name == name.strip() != "", urn
        assert segment_value_label(facet, urn) == name
    assert ids == sorted(set(ids))


def test_the_table_is_read_only_at_both_levels() -> None:
    with pytest.raises(TypeError):
        SEGMENT_VALUE_LABELS["geo"] = {}  # type: ignore[index]
    for names in SEGMENT_VALUE_LABELS.values():
        assert isinstance(names, Mapping)
        with pytest.raises(TypeError):
            names["urn:li:seniority:6"] = "Boss"  # type: ignore[index]
    assert segment_value_label("seniority", "urn:li:seniority:6") == "Director"


def test_docstring_examples_hold() -> None:
    results = doctest.testmod(segment_labels, report=False)
    assert results.attempted > 0
    assert results.failed == 0


def test_module_is_pure_and_imports_only_the_standard_library() -> None:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(segment_labels))):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported
    assert imported <= sys.stdlib_module_names
    assert not imported & {"logging", "os", "io", "pathlib", "socket", "urllib", "http"}
