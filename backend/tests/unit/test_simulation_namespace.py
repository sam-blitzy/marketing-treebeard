"""Behavioural tests of the reserved simulated-id namespace in ``ads_engine.simulation``.

The package root holds only constants and pure predicates, so these tests need no database,
no network and none of the simulation submodules.
"""

import ast
import dataclasses
import doctest
import re
import sys
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import ads_engine.simulation as simulation
from ads_engine.simulation import (
    DEFAULT_SCENARIO,
    DEFAULT_SEED,
    GOOGLE_AD_GROUP_IDS,
    GOOGLE_BUDGET_IDS,
    GOOGLE_CAMPAIGN_IDS,
    GOOGLE_CONVERSION_ACTION_IDS,
    GOOGLE_CRITERION_IDS,
    GOOGLE_ID_RANGE,
    HUBSPOT_CONTACT_IDS,
    HUBSPOT_DEAL_IDS,
    HUBSPOT_HASHED_CONTACT_IDS,
    HUBSPOT_ID_RANGE,
    HUBSPOT_MEETING_IDS,
    HUBSPOT_STAGE_IDS,
    LINKEDIN_ACCOUNT_IDS,
    LINKEDIN_CAMPAIGN_GROUP_IDS,
    LINKEDIN_CAMPAIGN_IDS,
    LINKEDIN_CREATIVE_IDS,
    LINKEDIN_FORM_IDS,
    LINKEDIN_ID_RANGE,
    LINKEDIN_POST_IDS,
    SIM_EMAIL_DOMAIN,
    SIM_GOOGLE_CUSTOMER_ID,
    IdRange,
    is_simulated_email,
    is_simulated_id,
)

type Source = Literal["google", "linkedin", "hubspot"]

SOURCES: tuple[Source, ...] = ("google", "linkedin", "hubspot")

LINKEDIN_SUB_RANGES: dict[str, IdRange] = {
    "LINKEDIN_ACCOUNT_IDS": LINKEDIN_ACCOUNT_IDS,
    "LINKEDIN_FORM_IDS": LINKEDIN_FORM_IDS,
    "LINKEDIN_CREATIVE_IDS": LINKEDIN_CREATIVE_IDS,
    "LINKEDIN_CAMPAIGN_IDS": LINKEDIN_CAMPAIGN_IDS,
    "LINKEDIN_CAMPAIGN_GROUP_IDS": LINKEDIN_CAMPAIGN_GROUP_IDS,
    "LINKEDIN_POST_IDS": LINKEDIN_POST_IDS,
}
GOOGLE_SUB_RANGES: dict[str, IdRange] = {
    "GOOGLE_CAMPAIGN_IDS": GOOGLE_CAMPAIGN_IDS,
    "GOOGLE_BUDGET_IDS": GOOGLE_BUDGET_IDS,
    "GOOGLE_AD_GROUP_IDS": GOOGLE_AD_GROUP_IDS,
    "GOOGLE_CRITERION_IDS": GOOGLE_CRITERION_IDS,
    "GOOGLE_CONVERSION_ACTION_IDS": GOOGLE_CONVERSION_ACTION_IDS,
}
HUBSPOT_SUB_RANGES: dict[str, IdRange] = {
    "HUBSPOT_CONTACT_IDS": HUBSPOT_CONTACT_IDS,
    "HUBSPOT_HASHED_CONTACT_IDS": HUBSPOT_HASHED_CONTACT_IDS,
    "HUBSPOT_MEETING_IDS": HUBSPOT_MEETING_IDS,
    "HUBSPOT_DEAL_IDS": HUBSPOT_DEAL_IDS,
}

# (source, parent name, parent block, sub-ranges): the object-id namespace of each source.
SOURCE_FAMILIES: tuple[tuple[Source, str, IdRange, dict[str, IdRange]], ...] = (
    ("linkedin", "LINKEDIN_ID_RANGE", LINKEDIN_ID_RANGE, LINKEDIN_SUB_RANGES),
    ("google", "GOOGLE_ID_RANGE", GOOGLE_ID_RANGE, GOOGLE_SUB_RANGES),
    ("hubspot", "HUBSPOT_ID_RANGE", HUBSPOT_ID_RANGE, HUBSPOT_SUB_RANGES),
)

# Largest integer a JSON number (IEEE 754 double) carries exactly.
FLOAT_EXACT_LIMIT = 2**53

# Values that are not identifiers of any source; no predicate may raise on them.
MALFORMED_IDS: tuple[int | str, ...] = (
    True,
    False,
    -1,
    "",
    " ",
    "abc",
    "-1",
    "+990000101",
    " 990000101",
    "990000101 ",
    "990000101.0",
    "990_000_101",
    "9.9e8",
    "0x3B023B65",
    "\u0663",  # ARABIC-INDIC DIGIT THREE: decimal, but not ASCII
    "\uff19\uff19\uff10",  # FULLWIDTH DIGITs 9, 9, 0
    "urn:",
    "urn:li:",
)


def _source_family_id(family: tuple[Source, str, IdRange, dict[str, IdRange]]) -> str:
    return family[0]


# ---------------------------------------------------------------------------
# Exported names and constants
# ---------------------------------------------------------------------------


def test_public_interface_lists_each_documented_name_once() -> None:
    expected = {
        "DEFAULT_SCENARIO",
        "DEFAULT_SEED",
        "GOOGLE_AD_GROUP_IDS",
        "GOOGLE_BUDGET_IDS",
        "GOOGLE_CAMPAIGN_IDS",
        "GOOGLE_CONVERSION_ACTION_IDS",
        "GOOGLE_CRITERION_IDS",
        "GOOGLE_ID_RANGE",
        "HUBSPOT_CONTACT_IDS",
        "HUBSPOT_DEAL_IDS",
        "HUBSPOT_HASHED_CONTACT_IDS",
        "HUBSPOT_ID_RANGE",
        "HUBSPOT_MEETING_IDS",
        "HUBSPOT_STAGE_IDS",
        "LINKEDIN_ACCOUNT_IDS",
        "LINKEDIN_CAMPAIGN_GROUP_IDS",
        "LINKEDIN_CAMPAIGN_IDS",
        "LINKEDIN_CREATIVE_IDS",
        "LINKEDIN_FORM_IDS",
        "LINKEDIN_ID_RANGE",
        "LINKEDIN_POST_IDS",
        "SIM_EMAIL_DOMAIN",
        "SIM_GOOGLE_CUSTOMER_ID",
        "IdRange",
        "is_simulated_email",
        "is_simulated_id",
    }

    assert len(simulation.__all__) == len(set(simulation.__all__))
    assert set(simulation.__all__) == expected
    for name in simulation.__all__:
        assert hasattr(simulation, name), name


def test_email_domain_is_the_rfc2606_reserved_invalid_tld() -> None:
    assert SIM_EMAIL_DOMAIN == "sim.invalid"
    assert SIM_EMAIL_DOMAIN.rsplit(".", 1)[-1] == "invalid"


def test_default_seed_mirrors_the_mock_seed_default() -> None:
    assert type(DEFAULT_SEED) is int
    assert DEFAULT_SEED == 42


def test_default_scenario_name() -> None:
    assert DEFAULT_SCENARIO == "default"


def test_simulated_google_customer_id_has_the_webhook_shape() -> None:
    assert SIM_GOOGLE_CUSTOMER_ID == "9900000001"
    assert len(SIM_GOOGLE_CUSTOMER_ID) == 10
    assert SIM_GOOGLE_CUSTOMER_ID.isascii()
    assert SIM_GOOGLE_CUSTOMER_ID.isdecimal()
    # A 10-digit customer id sits outside the 12-digit block and is recognised on its own.
    assert not GOOGLE_ID_RANGE.contains(SIM_GOOGLE_CUSTOMER_ID)
    assert is_simulated_id("google", SIM_GOOGLE_CUSTOMER_ID)


def test_module_imports_only_the_standard_library() -> None:
    module_file = simulation.__file__
    assert module_file is not None
    tree = ast.parse(Path(module_file).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module)

    assert imported
    for module in imported:
        assert module.partition(".")[0] in sys.stdlib_module_names, module


def test_docstring_examples_hold() -> None:
    results = doctest.testmod(simulation, report=False)

    assert results.attempted > 0
    assert results.failed == 0


# ---------------------------------------------------------------------------
# IdRange
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "size"),
    [
        (0, 0, 1),
        (7, 7, 1),
        (5, 9, 5),
        (990_000_100, 990_000_999, 900),
        (HUBSPOT_ID_RANGE.start, HUBSPOT_ID_RANGE.end, 1_000_000_000_000),
    ],
)
def test_size_counts_both_bounds(start: int, end: int, size: int) -> None:
    block = IdRange(start, end)

    assert block.start == start
    assert block.end == end
    assert block.size == size


def test_id_range_is_an_immutable_value() -> None:
    block = IdRange(1, 2)
    mutable: Any = block

    with pytest.raises(dataclasses.FrozenInstanceError):
        mutable.start = 0

    assert block == IdRange(1, 2)
    assert block != IdRange(1, 3)
    assert len({IdRange(1, 2), IdRange(1, 2), IdRange(1, 3)}) == 2


@pytest.mark.parametrize(
    ("start", "end"),
    [(True, 5), (0, True), (False, False), (False, 1)],
)
def test_boolean_bounds_are_rejected(start: int, end: int) -> None:
    with pytest.raises(TypeError, match="bounds must be integers, not booleans"):
        IdRange(start, end)


@pytest.mark.parametrize(("start", "end"), [(-1, 5), (-1, -1), (-10, -20)])
def test_negative_start_is_rejected(start: int, end: int) -> None:
    with pytest.raises(ValueError, match=re.escape(f"IdRange start {start} must be non-negative")):
        IdRange(start, end)


@pytest.mark.parametrize(("start", "end"), [(10, 9), (1, 0), (990_000_001, 990_000_000)])
def test_start_above_end_is_rejected(start: int, end: int) -> None:
    with pytest.raises(ValueError, match=re.escape(f"IdRange start {start} exceeds end {end}")):
        IdRange(start, end)


BLOCK = IdRange(990_000_100, 990_000_999)


@pytest.mark.parametrize(
    "value",
    [
        990_000_100,
        990_000_101,
        990_000_999,
        "990000100",
        "990000101",
        "990000999",
        "0990000101",  # leading zeros are still ASCII decimal digits
    ],
)
def test_contains_accepts_ints_and_decimal_strings_within_bounds(value: int | str) -> None:
    assert BLOCK.contains(value)


@pytest.mark.parametrize(
    "value",
    [
        990_000_099,
        990_001_000,
        0,
        -990_000_101,
        "990000099",
        "990001000",
        "n/a",
        "urn:li:leadGenForm:990000101",
        *MALFORMED_IDS,
    ],
)
def test_contains_rejects_values_outside_bounds_and_non_decimal_text(value: int | str) -> None:
    assert not BLOCK.contains(value)


def test_contains_rejects_booleans_even_when_their_int_value_is_in_range() -> None:
    block = IdRange(0, 10)

    assert block.contains(0)
    assert block.contains(1)
    assert not block.contains(False)
    assert not block.contains(True)


def test_contains_rejects_digit_strings_int_parses_but_wire_ids_never_use() -> None:
    block = IdRange(0, 10_000)

    # int() accepts each of these; contains() requires plain ASCII decimal digits.
    for text in (" 5", "5\n", "+5", "1_000", "\u0663", "\uff15"):
        assert int(text) <= block.end
        assert not block.contains(text), repr(text)


def test_digit_string_beyond_the_int_parse_limit_is_rejected_without_raising() -> None:
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(640)
    try:
        oversized = "9" * 641
        assert simulation._parse_decimal_id(oversized) is None
        assert not HUBSPOT_ID_RANGE.contains(oversized)
        for source in SOURCES:
            assert not is_simulated_id(source, oversized)
    finally:
        sys.set_int_max_str_digits(previous_limit)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("007", 7),
        ("990000101", 990_000_101),
        ("9900000000000001", 9_900_000_000_000_001),
        ("", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("1.0", None),
        ("1_000", None),
        ("\u0663", None),
        ("abc", None),
    ],
)
def test_parse_decimal_id(text: str, expected: int | None) -> None:
    assert simulation._parse_decimal_id(text) == expected


def test_at_allocates_every_id_of_the_range_in_order() -> None:
    block = IdRange(10, 14)

    allocated = [block.at(offset) for offset in range(block.size)]

    assert allocated == [10, 11, 12, 13, 14]
    assert all(block.contains(value) for value in allocated)
    assert block.at(3) == block.at(3)


def test_at_allocates_the_documented_config_ids() -> None:
    assert LINKEDIN_ACCOUNT_IDS.at(0) == 990_000_001
    assert LINKEDIN_ACCOUNT_IDS.at(1) == 990_000_002
    assert LINKEDIN_FORM_IDS.at(1) == 990_000_101
    assert LINKEDIN_CREATIVE_IDS.at(301) == 990_001_301
    assert GOOGLE_CAMPAIGN_IDS.at(GOOGLE_CAMPAIGN_IDS.size - 1) == GOOGLE_CAMPAIGN_IDS.end


def test_at_rejects_a_negative_offset() -> None:
    with pytest.raises(ValueError, match=re.escape("IdRange offset -1 must be non-negative")):
        IdRange(10, 14).at(-1)


@pytest.mark.parametrize("offset", [5, 6, 10**20])
def test_at_rejects_an_offset_past_the_end(offset: int) -> None:
    with pytest.raises(
        ValueError,
        match=re.escape(f"IdRange offset {offset} exceeds the range [10, 14] of 5 ids"),
    ):
        IdRange(10, 14).at(offset)


# ---------------------------------------------------------------------------
# Reserved ranges per source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "digits", "prefix"),
    [
        (LINKEDIN_ID_RANGE, 9, "990"),
        (GOOGLE_ID_RANGE, 12, "990"),
        (HUBSPOT_ID_RANGE, 16, "9900"),
        (HUBSPOT_STAGE_IDS, 9, "990"),
        *[(block, 9, "990") for block in LINKEDIN_SUB_RANGES.values()],
        *[(block, 12, "990") for block in GOOGLE_SUB_RANGES.values()],
        *[(block, 16, "9900") for block in HUBSPOT_SUB_RANGES.values()],
    ],
)
def test_every_id_of_a_block_has_one_digit_width(block: IdRange, digits: int, prefix: str) -> None:
    assert len(str(block.start)) == digits
    assert len(str(block.end)) == digits
    assert str(block.start).startswith(prefix)
    assert str(block.end).startswith(prefix)


@pytest.mark.parametrize("family", SOURCE_FAMILIES, ids=_source_family_id)
def test_sub_ranges_lie_inside_their_block_and_never_overlap(
    family: tuple[Source, str, IdRange, dict[str, IdRange]],
) -> None:
    _source, _parent_name, parent, children = family

    for name, child in children.items():
        assert parent.start <= child.start <= child.end <= parent.end, name
    for (left_name, left), (right_name, right) in combinations(children.items(), 2):
        assert left.end < right.start or right.end < left.start, (left_name, right_name)


@pytest.mark.parametrize("family", SOURCE_FAMILIES, ids=_source_family_id)
def test_every_sub_range_id_is_simulated_for_its_source_only(
    family: tuple[Source, str, IdRange, dict[str, IdRange]],
) -> None:
    source, _parent_name, _parent, children = family
    other_sources = [other for other in SOURCES if other != source]

    for child in children.values():
        for value in (child.start, child.end, str(child.start), str(child.end)):
            assert is_simulated_id(source, value), (source, value)
            for other in other_sources:
                assert not is_simulated_id(other, value), (other, value)


def test_source_blocks_do_not_overlap_one_another() -> None:
    blocks = [GOOGLE_ID_RANGE, LINKEDIN_ID_RANGE, HUBSPOT_ID_RANGE]

    for left, right in combinations(blocks, 2):
        assert left.end < right.start or right.end < left.start


def test_import_time_check_covers_every_exported_object_range() -> None:
    checked: set[str] = set()
    for parent_name, parent, children in simulation._RANGE_FAMILIES:
        assert getattr(simulation, parent_name) is parent
        checked.add(parent_name)
        for name, child in children:
            assert getattr(simulation, name) is child
            checked.add(name)

    exported_ranges = {
        name for name in simulation.__all__ if isinstance(getattr(simulation, name), IdRange)
    }

    # HUBSPOT_STAGE_IDS is a single-range namespace of its own, with nothing to check.
    assert checked == exported_ranges - {"HUBSPOT_STAGE_IDS"}
    assert {family[1] for family in SOURCE_FAMILIES} == {
        parent_name for parent_name, _parent, _children in simulation._RANGE_FAMILIES
    }


def test_documented_default_world_and_config_ids_fall_in_exactly_one_sub_range() -> None:
    expected = {
        990_000_001: "LINKEDIN_ACCOUNT_IDS",
        990_000_002: "LINKEDIN_ACCOUNT_IDS",
        990_000_101: "LINKEDIN_FORM_IDS",
        990_000_102: "LINKEDIN_FORM_IDS",
        990_000_201: "LINKEDIN_FORM_IDS",
        990_000_301: "LINKEDIN_FORM_IDS",
        990_001_301: "LINKEDIN_CREATIVE_IDS",
    }

    for value, owner in expected.items():
        owners = [name for name, block in LINKEDIN_SUB_RANGES.items() if block.contains(value)]
        assert owners == [owner], value


def test_hubspot_object_ids_lie_above_every_id_a_json_number_carries_exactly() -> None:
    assert HUBSPOT_ID_RANGE.start > FLOAT_EXACT_LIMIT
    for block in HUBSPOT_SUB_RANGES.values():
        assert block.start > FLOAT_EXACT_LIMIT

    # Live HubSpot object ids, small or as large as a JSON number holds exactly, are never
    # taken for simulated ones.
    for live_id in (1, 51, 12_345_678_901, FLOAT_EXACT_LIMIT - 1, FLOAT_EXACT_LIMIT):
        assert not is_simulated_id("hubspot", live_id)
        assert not is_simulated_id("hubspot", str(live_id))


@pytest.mark.parametrize("block", HUBSPOT_SUB_RANGES.values(), ids=HUBSPOT_SUB_RANGES.keys())
def test_hubspot_ids_beyond_float_precision_are_recognised_as_strings(block: IdRange) -> None:
    odd_id = block.at(1)

    # A float cannot hold the id, which is why the wire format carries it as a string.
    assert int(float(odd_id)) != odd_id
    assert is_simulated_id("hubspot", str(odd_id))
    assert is_simulated_id("hubspot", odd_id)
    assert block.contains(str(odd_id))
    assert is_simulated_id("hubspot", str(block.end))


def test_hubspot_predicate_covers_the_whole_object_block() -> None:
    # Between sub-ranges, yet inside the reserved block.
    unallocated = HUBSPOT_DEAL_IDS.end + 1
    assert not any(block.contains(unallocated) for block in HUBSPOT_SUB_RANGES.values())
    assert is_simulated_id("hubspot", str(unallocated))

    assert is_simulated_id("hubspot", str(HUBSPOT_ID_RANGE.end))
    assert not is_simulated_id("hubspot", str(HUBSPOT_ID_RANGE.start - 1))
    assert not is_simulated_id("hubspot", str(HUBSPOT_ID_RANGE.end + 1))


def test_hubspot_stage_ids_are_a_namespace_apart_from_crm_object_ids() -> None:
    assert HUBSPOT_STAGE_IDS.end < HUBSPOT_ID_RANGE.start
    for parent_name, _parent, children in simulation._RANGE_FAMILIES:
        assert parent_name != "HUBSPOT_STAGE_IDS"
        assert all(name != "HUBSPOT_STAGE_IDS" for name, _child in children)

    for stage_id in (HUBSPOT_STAGE_IDS.start, HUBSPOT_STAGE_IDS.at(1), HUBSPOT_STAGE_IDS.end):
        assert HUBSPOT_STAGE_IDS.contains(stage_id)
        assert HUBSPOT_STAGE_IDS.contains(str(stage_id))
        # is_simulated_id("hubspot", ...) judges CRM object ids only.
        assert not is_simulated_id("hubspot", stage_id)
        assert not is_simulated_id("hubspot", str(stage_id))

    assert not HUBSPOT_STAGE_IDS.contains(HUBSPOT_STAGE_IDS.start - 1)
    assert not HUBSPOT_STAGE_IDS.contains(HUBSPOT_STAGE_IDS.end + 1)


# ---------------------------------------------------------------------------
# _check_disjoint
# ---------------------------------------------------------------------------


def test_check_disjoint_accepts_the_module_ranges() -> None:
    simulation._check_disjoint()
    simulation._check_disjoint(simulation._RANGE_FAMILIES)


def test_check_disjoint_accepts_adjacent_children_and_children_filling_the_parent() -> None:
    parent = IdRange(0, 9)

    simulation._check_disjoint(())
    simulation._check_disjoint((("P", parent, ()),))
    simulation._check_disjoint((("P", parent, (("WHOLE", IdRange(0, 9)),)),))
    simulation._check_disjoint(
        (("P", parent, (("LOW", IdRange(0, 4)), ("HIGH", IdRange(5, 9)))),),
    )


def test_check_disjoint_lets_families_of_different_sources_overlap() -> None:
    simulation._check_disjoint(
        (
            ("A", IdRange(0, 9), (("A1", IdRange(0, 9)),)),
            ("B", IdRange(0, 9), (("B1", IdRange(0, 9)),)),
        ),
    )


@pytest.mark.parametrize(
    "child",
    [
        IdRange(0, 15),  # starts below the parent
        IdRange(9, 30),  # one id below the parent
        IdRange(20, 35),  # ends above the parent
        IdRange(30, 31),  # one id above the parent
        IdRange(0, 5),  # wholly below
        IdRange(0, 40),  # encloses the parent
    ],
)
def test_check_disjoint_reports_a_child_leaving_its_parent(child: IdRange) -> None:
    families = (("PARENT", IdRange(10, 30), (("CHILD", child),)),)
    expected = (
        "Reserved simulation id ranges are inconsistent: "
        f"CHILD [{child.start}, {child.end}] lies outside PARENT [10, 30]"
    )

    with pytest.raises(AssertionError, match=re.escape(expected)):
        simulation._check_disjoint(families)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (IdRange(0, 5), IdRange(5, 9)),  # one shared boundary id
        (IdRange(5, 9), IdRange(0, 5)),  # the same pair, listed high first
        (IdRange(0, 9), IdRange(3, 4)),  # nested
        (IdRange(3, 4), IdRange(0, 9)),  # nested, listed inner first
        (IdRange(2, 7), IdRange(2, 7)),  # identical
    ],
)
def test_check_disjoint_reports_overlapping_children(left: IdRange, right: IdRange) -> None:
    families = (("PARENT", IdRange(0, 9), (("LEFT", left), ("RIGHT", right))),)

    with pytest.raises(AssertionError, match=re.escape("LEFT overlaps RIGHT within PARENT")):
        simulation._check_disjoint(families)


def test_check_disjoint_reports_every_problem_in_one_error() -> None:
    families = (
        (
            "LINKED",
            IdRange(0, 99),
            (("ONE", IdRange(0, 49)), ("TWO", IdRange(40, 120)), ("THREE", IdRange(45, 60))),
        ),
        ("CLEAN", IdRange(0, 9), (("C1", IdRange(0, 9)),)),
        ("STRAY", IdRange(200, 299), (("BELOW", IdRange(100, 250)),)),
    )

    with pytest.raises(AssertionError) as excinfo:
        simulation._check_disjoint(families)

    prefix = "Reserved simulation id ranges are inconsistent: "
    message = str(excinfo.value)
    assert message.startswith(prefix)
    assert message.removeprefix(prefix).split("; ") == [
        "TWO [40, 120] lies outside LINKED [0, 99]",
        "ONE overlaps TWO within LINKED",
        "ONE overlaps THREE within LINKED",
        "TWO overlaps THREE within LINKED",
        "BELOW [100, 250] lies outside STRAY [200, 299]",
    ]


# ---------------------------------------------------------------------------
# is_simulated_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        SIM_GOOGLE_CUSTOMER_ID,
        int(SIM_GOOGLE_CUSTOMER_ID),
        GOOGLE_ID_RANGE.start,
        GOOGLE_ID_RANGE.end,
        str(GOOGLE_ID_RANGE.start),
        str(GOOGLE_ID_RANGE.end),
        "990000000001",
    ],
)
def test_google_simulated_ids(value: int | str) -> None:
    assert is_simulated_id("google", value)


@pytest.mark.parametrize(
    "value",
    [
        "1234567890",  # a live-looking customer id
        "990-000-0001",  # the simulated customer id in the dashed UI form
        "09900000001",
        "9900000002",
        GOOGLE_ID_RANGE.start - 1,
        GOOGLE_ID_RANGE.end + 1,
        str(GOOGLE_ID_RANGE.start - 1),
        str(GOOGLE_ID_RANGE.end + 1),
        21_234_567_890,  # a live-looking campaign id
        "990000101",  # a simulated LinkedIn id
        str(HUBSPOT_CONTACT_IDS.start),
        *MALFORMED_IDS,
    ],
)
def test_google_non_simulated_ids(value: int | str) -> None:
    assert not is_simulated_id("google", value)


@pytest.mark.parametrize(
    "value",
    [
        "990000101",
        990_000_101,
        str(LINKEDIN_ID_RANGE.start),
        LINKEDIN_ID_RANGE.end,
        f"urn:li:sponsoredAccount:{LINKEDIN_ACCOUNT_IDS.start}",
        "urn:li:leadGenForm:990000301",
        f"urn:li:sponsoredCreative:{LINKEDIN_CREATIVE_IDS.end}",
        f"urn:li:sponsoredCampaign:{LINKEDIN_CAMPAIGN_IDS.start}",
        f"urn:li:sponsoredCampaignGroup:{LINKEDIN_CAMPAIGN_GROUP_IDS.start}",
        f"urn:li:share:{LINKEDIN_POST_IDS.end}",
    ],
)
def test_linkedin_simulated_ids_and_simple_urns(value: int | str) -> None:
    assert is_simulated_id("linkedin", value)


@pytest.mark.parametrize(
    "value",
    [
        "512345678",  # a live-looking account id
        "urn:li:sponsoredAccount:512345678",
        str(LINKEDIN_ID_RANGE.start - 1),
        LINKEDIN_ID_RANGE.end + 1,
        f"urn:li:sponsoredCampaign:{LINKEDIN_ID_RANGE.end + 1}",
        # Compound URNs are reduced to their canonical URN by callers first.
        "urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101,2)",
        "urn:li::990000101",  # empty entity type
        "urn:li:leadGenForm:",  # empty id
        "urn:li:leadGenForm:990000101:2",  # five segments
        "urn:xx:leadGenForm:990000101",  # not a LinkedIn URN
        "li:leadGenForm:990000101",  # not a URN at all
        "urn:li:leadGenForm: 990000101",
        str(GOOGLE_CAMPAIGN_IDS.start),
        str(HUBSPOT_CONTACT_IDS.start),
        *MALFORMED_IDS,
    ],
)
def test_linkedin_non_simulated_ids(value: int | str) -> None:
    assert not is_simulated_id("linkedin", value)


@pytest.mark.parametrize(
    ("value", "plain_id"),
    [
        ("990000101", "990000101"),
        ("urn:li:leadGenForm:990000101", "990000101"),
        ("urn:li:sponsoredCreative:abc", "abc"),
        ("urn:li:leadGenForm:", ""),
        ("urn:li::990000101", ""),
        ("urn:li:versionedLeadGenForm:(urn:li:leadGenForm:990000101,2)", ""),
        ("urn:xx:leadGenForm:990000101", ""),
        ("urn:", ""),
        ("", ""),
        ("li:leadGenForm:990000101", "li:leadGenForm:990000101"),
    ],
)
def test_linkedin_plain_id(value: str, plain_id: str) -> None:
    assert simulation._linkedin_plain_id(value) == plain_id


@pytest.mark.parametrize(
    "value",
    [
        str(HUBSPOT_ID_RANGE.start),
        HUBSPOT_ID_RANGE.start,
        "9900000000000001",
        str(HUBSPOT_HASHED_CONTACT_IDS.start),
        str(HUBSPOT_MEETING_IDS.end),
        HUBSPOT_DEAL_IDS.end,
    ],
)
def test_hubspot_simulated_ids(value: int | str) -> None:
    assert is_simulated_id("hubspot", value)


@pytest.mark.parametrize(
    "value",
    [
        "51",
        "12345678901",
        str(FLOAT_EXACT_LIMIT),
        str(HUBSPOT_ID_RANGE.start - 1),
        HUBSPOT_ID_RANGE.end + 1,
        "9900000000000000.0",
        "9.9e15",
        "0-9900000000000001",
        str(HUBSPOT_STAGE_IDS.start),
        SIM_GOOGLE_CUSTOMER_ID,
        "990000101",
        f"urn:li:leadGenForm:{HUBSPOT_ID_RANGE.start}",  # URNs are not HubSpot ids
        *MALFORMED_IDS,
    ],
)
def test_hubspot_non_simulated_ids(value: int | str) -> None:
    assert not is_simulated_id("hubspot", value)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("value", MALFORMED_IDS)
def test_malformed_values_are_never_simulated_and_never_raise(
    source: Source, value: int | str
) -> None:
    assert is_simulated_id(source, value) is False


@pytest.mark.parametrize("source", ["meta", "Google", "LINKEDIN", "hubspot ", ""])
def test_unknown_source_is_rejected(source: str) -> None:
    unchecked_source = cast(Any, source)

    with pytest.raises(ValueError, match=re.escape(f"Unknown source {source!r}")):
        is_simulated_id(unchecked_source, SIM_GOOGLE_CUSTOMER_ID)


def test_source_dispatch_is_read_only_and_covers_exactly_the_three_sources() -> None:
    predicates: Any = simulation._ID_PREDICATES

    assert set(predicates) == set(SOURCES)
    with pytest.raises(TypeError, match="does not support item assignment"):
        predicates["meta"] = HUBSPOT_ID_RANGE.contains


# ---------------------------------------------------------------------------
# is_simulated_email
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "lead-0001@sim.invalid",
        " Jane.Doe@SIM.invalid ",
        "\tlead@Sim.Invalid\n",
        "first+tag@sim.invalid",
        '"odd@local"@sim.invalid',  # only the domain after the last "@" counts
        "@sim.invalid",
    ],
)
def test_simulated_emails(email: str) -> None:
    assert is_simulated_email(email)


@pytest.mark.parametrize(
    "email",
    [
        "ann@example.com",
        "lead@x.sim.invalid",  # subdomains are not the reserved domain
        "lead@sim.invalid.com",
        "lead@sim.invalid.",
        "lead@sim-invalid",
        "lead@invalid",
        "sim.invalid@example.com",
        "sim.invalid",
        "lead@",
        "lead sim.invalid",
        "",
        "   ",
    ],
)
def test_non_simulated_emails(email: str) -> None:
    assert not is_simulated_email(email)
