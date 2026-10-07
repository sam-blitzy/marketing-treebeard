"""Unit tests for ``ads_engine.analysis.thresholds``: config loading, validation and hashing.

They pin the configuration contract every rule relies on: the shipped ``config/*.yaml`` load
with the documented values; a missing or ill-typed key, a default window missing from the
presets, a form or creative listed under two assets, and a contradicting creative override
each fail with a ``ConfigError`` naming the file and key; ``config_hash`` ignores comments and
follows values; and stored CRM bindings round-trip through JSON. Pure: files in ``tmp_path``
only, no database and no network.
"""

import copy
import json
import pickle
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from ads_engine.analysis.thresholds import (
    CONFIG_FILES,
    CONTENT_ASSETS_FILE,
    CRM_BINDINGS_DOCUMENT,
    HUBSPOT_MAPPING_FILE,
    THRESHOLDS_FILE,
    ConfigError,
    ContentAsset,
    CrmBindings,
    GoogleCrmBinding,
    HubSpotMapping,
    ResolvedBinding,
    StageBinding,
    Thresholds,
    resolve_config_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

OVERRIDE_CREATIVE = "urn:li:sponsoredCreative:990001301"
BENCHMARKS_FORM = "urn:li:leadGenForm:990000201"
PLAYBOOK_FORM = "urn:li:leadGenForm:990000301"

Trees = dict[str, Any]


def _shipped_trees() -> Trees:
    """Parse the three shipped files with plain ``yaml.safe_load``."""
    return {
        name: yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))
        for name in CONFIG_FILES
    }


def _build(trees: Trees) -> Thresholds:
    return Thresholds.from_trees(
        thresholds=trees[THRESHOLDS_FILE],
        content_assets=trees[CONTENT_ASSETS_FILE],
        hubspot_mapping=trees[HUBSPOT_MAPPING_FILE],
        source_dir=CONFIG_DIR,
    )


def _edited(edit: Callable[[Trees], None]) -> Thresholds:
    trees = copy.deepcopy(_shipped_trees())
    edit(trees)
    return _build(trees)


def _copy_config(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    for name in CONFIG_FILES:
        (target / name).write_text((CONFIG_DIR / name).read_text(encoding="utf-8"), "utf-8")
    return target


@pytest.fixture(scope="module")
def shipped() -> Thresholds:
    return Thresholds.load(CONFIG_DIR)


# ---------------------------------------------------------------------------
# Shipped configuration
# ---------------------------------------------------------------------------


def test_shipped_config_loads_with_documented_values(shipped: Thresholds) -> None:
    assert len(shipped.config_hash) == 64
    assert int(shipped.config_hash, 16) >= 0
    assert shipped.presets == (14, 28, 56, 90)
    assert shipped.default_days == 28
    assert shipped.trusted_min_days == 14
    assert shipped.sustained_min_days == 7
    assert shipped.max_days == 180
    assert shipped.subset("k1.rule_of_three") == {"k1.rule_of_three": 3}
    assert shipped.source_dir == CONFIG_DIR


def test_ratios_and_money_are_exact_decimals(shipped: Thresholds) -> None:
    assert shipped.ratio("budget.capped_day_ratio") == Decimal("0.95")
    assert shipped.decimal("b1.step") == Decimal("0.2")
    assert shipped.decimal("b3.headroom") == Decimal("1.1")
    assert shipped.decimal("k1.rule_of_three") == Decimal(3)
    assert shipped.int("cr.min_impressions") == 1000


def test_crm_meeting_lists_are_merged_from_the_mapping(shipped: Thresholds) -> None:
    assert shipped.strings("crm.booking_sources") == ("MEETINGS_PUBLIC",)
    assert shipped.strings("crm.meeting_outcome_exclusions") == ("CANCELED",)
    assert shipped.int("crm.lead_maturity_days") == 14
    assert shipped.mapping.booking_sources == ("MEETINGS_PUBLIC",)


def test_subset_keeps_yaml_scalars(shipped: Thresholds) -> None:
    subset = shipped.subset("budget.capped_day_ratio", "window.presets", "budget.min_known_days")
    assert subset == {
        "budget.capped_day_ratio": 0.95,
        "window.presets": [14, 28, 56, 90],
        "budget.min_known_days": 7,
    }
    assert isinstance(subset["window.presets"], list)


def test_as_view_is_the_full_json_tree(shipped: Thresholds) -> None:
    view = shipped.as_view()
    shipped_namespaces = set(_shipped_trees()[THRESHOLDS_FILE])
    assert set(view) == shipped_namespaces
    assert view["window"]["presets"] == [14, 28, 56, 90]
    assert view["crm"]["booking_sources"] == ["MEETINGS_PUBLIC"]
    view["window"]["default_days"] = 99
    assert shipped.default_days == 28


def test_raw_tree_is_read_only(shipped: Thresholds) -> None:
    with pytest.raises(TypeError):
        shipped.raw["window"]["default_days"] = 1
    assert isinstance(shipped.value("window.presets"), tuple)


def test_shipped_mapping_and_registry(shipped: Thresholds) -> None:
    assert shipped.mapping.mql == StageBinding("stage_label", ("First meeting booked",), None)
    assert shipped.mapping.ql.stage_labels == ("Qualified Lead", "Sales Qualified Lead")
    assert shipped.mapping.to_json()["ql"] == {
        "mode": "stage_label",
        "stage_labels": ["Qualified Lead", "Sales Qualified Lead"],
        "property": None,
    }
    assert [asset.slug for asset in shipped.content_assets] == [
        "b2b-pipeline-report-2026",
        "paid-media-benchmarks-2026",
        "attribution-playbook",
    ]
    assert shipped.content_assets[2].to_json() == {
        "slug": "attribution-playbook",
        "title": "The Attribution Playbook",
        "lead_forms": [PLAYBOOK_FORM],
        "creative_overrides": [OVERRIDE_CREATIVE],
    }


def test_content_asset_rows_match_the_repository_contract(shipped: Thresholds) -> None:
    rows = shipped.content_asset_rows()
    assert rows[0] == {
        "slug": "b2b-pipeline-report-2026",
        "title": "The 2026 B2B Pipeline Report",
        "lead_form_urns": ["urn:li:leadGenForm:990000101", "urn:li:leadGenForm:990000102"],
        "creative_urns": [],
    }
    assert rows[2]["creative_urns"] == [OVERRIDE_CREATIVE]


def test_asset_lookups_and_resolution_precedence(shipped: Thresholds) -> None:
    benchmarks = shipped.asset_for_form(BENCHMARKS_FORM)
    playbook = shipped.asset_for_creative(OVERRIDE_CREATIVE)
    assert benchmarks is not None
    assert benchmarks.slug == "paid-media-benchmarks-2026"
    assert playbook is not None
    assert playbook.slug == "attribution-playbook"
    assert shipped.asset_for_form("urn:li:leadGenForm:1") is None
    assert shipped.asset_for_creative("urn:li:sponsoredCreative:1") is None
    # The override wins over the form; otherwise the form decides.
    assert shipped.resolve_asset(creative_urn=OVERRIDE_CREATIVE, form_urn=BENCHMARKS_FORM) is (
        playbook
    )
    assert (
        shipped.resolve_asset(creative_urn="urn:li:sponsoredCreative:5", form_urn=BENCHMARKS_FORM)
        is benchmarks
    )
    assert shipped.resolve_asset(creative_urn=None, form_urn=None) is None


# ---------------------------------------------------------------------------
# config_hash
# ---------------------------------------------------------------------------


def test_config_hash_ignores_comments_and_follows_values(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path / "config")
    original = Thresholds.load(directory)
    assert Thresholds.load(directory).config_hash == original.config_hash
    assert Thresholds.load(directory) == original
    assert hash(Thresholds.load(directory)) == hash(original)

    path = directory / THRESHOLDS_FILE
    path.write_text(path.read_text("utf-8") + "\n# a comment-only edit\n", "utf-8")
    assert Thresholds.load(directory).config_hash == original.config_hash

    path.write_text(
        path.read_text("utf-8").replace("high_utilization: 0.90", "high_utilization: 0.91")
    )
    changed = Thresholds.load(directory)
    assert changed.config_hash != original.config_hash
    assert changed != original


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_every_file_feeds_the_hash(name: str, shipped: Thresholds) -> None:
    trees = copy.deepcopy(_shipped_trees())
    if name == THRESHOLDS_FILE:
        trees[name]["k1"]["baseline_days"] = 91
    elif name == CONTENT_ASSETS_FILE:
        trees[name]["assets"][0]["title"] = "Renamed"
    else:
        trees[name]["crm"]["meeting_outcome_exclusions"] = ["CANCELED", "NO_SHOW"]
    assert _build(trees).config_hash != shipped.config_hash
    assert _build(_shipped_trees()).config_hash == shipped.config_hash


# ---------------------------------------------------------------------------
# Window validation (AAP 0.5.4.1)
# ---------------------------------------------------------------------------


def test_default_missing_from_presets_fails_config_load(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path / "config")
    path = directory / THRESHOLDS_FILE
    path.write_text(path.read_text("utf-8").replace("default_days: 28", "default_days: 21"))
    with pytest.raises(ConfigError) as caught:
        Thresholds.load(directory)
    assert caught.value.file == THRESHOLDS_FILE
    assert caught.value.key == "window.presets"
    assert "window.default_days (21)" in str(caught.value)

    path.write_text(path.read_text("utf-8").replace("[14, 28, 56, 90]", "[28, 14, 21, 90, 56]"))
    loaded = Thresholds.load(directory)
    assert loaded.default_days == 21
    assert loaded.presets == (14, 21, 28, 56, 90)


def test_presets_are_sorted_and_distinct() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"]["presets"] = [90, 28, 14, 28]

    assert _edited(edit).presets == (14, 28, 90)


@pytest.mark.parametrize(
    ("key", "value", "fault"),
    [
        ("max_days", 181, "window.max_days"),
        ("trusted_min_days", 181, "window.max_days"),
        ("presets", [14, 28, 200], "window.max_days"),
        ("presets", [], "window.presets"),
        ("presets", "28", "window.presets"),
        ("presets", [28, 0], "window.presets[1]"),
        ("default_days", 0, "window.default_days"),
        ("default_days", 28.0, "window.default_days"),
        ("default_days", True, "window.default_days"),
    ],
)
def test_window_values_are_range_checked(key: str, value: object, fault: str) -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"][key] = value

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == THRESHOLDS_FILE
    assert fault in str(caught.value)


def test_window_limits_relative_to_max_days() -> None:
    def shorter_max(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"]["max_days"] = 90
        trees[THRESHOLDS_FILE]["window"]["sustained_min_days"] = 91

    with pytest.raises(ConfigError) as caught:
        _edited(shorter_max)
    assert caught.value.key == "window.sustained_min_days"

    def preset_above(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"]["max_days"] = 60

    with pytest.raises(ConfigError) as caught:
        _edited(preset_above)
    assert caught.value.key == "window.presets[3]"

    def default_above(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"]["max_days"] = 20

    with pytest.raises(ConfigError) as caught:
        _edited(default_above)
    assert caught.value.key == "window.default_days"


# ---------------------------------------------------------------------------
# Threshold schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("namespace", "name", "value", "message"),
    [
        ("budget", "capped_day_ratio", 1.2, "in (0, 1]"),
        ("budget", "sustained_share", 0, "in (0, 1]"),
        ("b4", "alpha", 1.0, "in (0, 1)"),
        ("cr", "power", "0.8", "must be a number"),
        ("b1", "step", 0, "> 0"),
        ("k1", "min_spend_cpa_multiple", -0.1, ">= 0"),
        ("b3", "spend_percentile", 101, "in [0, 100]"),
        ("k1", "min_clicks_floor", 2.5, "must be an integer"),
        ("w", "cohort_min_total_ql", -1, "must be >= 0"),
        ("k2", "max_cpa_ratio", float("inf"), "finite number"),
        ("c1", "min_spend_day_share", True, "must be a number"),
    ],
)
def test_threshold_kinds_are_enforced(
    namespace: str, name: str, value: object, message: str
) -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE][namespace][name] = value

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == THRESHOLDS_FILE
    assert caught.value.key == f"{namespace}.{name}"
    assert message in caught.value.detail


@pytest.mark.parametrize(
    "key", ["k1.rule_of_three", "feedback.snooze_days", "budget.capped_lost_is_share"]
)
def test_missing_threshold_key_fails_naming_it(key: str) -> None:
    namespace, name = key.split(".")

    def edit(trees: Trees) -> None:
        del trees[THRESHOLDS_FILE][namespace][name]

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == key
    assert "required key is missing" in str(caught.value)


def test_namespace_that_is_not_a_mapping_fails() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["sources"] = 24

    with pytest.raises(ConfigError, match="sources is not a mapping"):
        _edited(edit)


@pytest.mark.parametrize(
    ("namespace", "name", "value", "key"),
    [
        ("budget", "under_utilization", 0.9, "budget.under_utilization"),
        ("k3", "min_qs_days_per_span", 8, "k3.min_qs_days_per_span"),
        ("cr", "min_variants", 1, "cr.min_variants"),
    ],
)
def test_cross_key_rules(namespace: str, name: str, value: object, key: str) -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE][namespace][name] = value

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == key


def test_unrecognised_keys_are_frozen_and_readable() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["extra"] = {
            "negative": -1,
            "when": date(2026, 10, 7),
            "names": ["a", "b"],
            "mixed": ["a", 1],
            "flag": True,
        }

    loaded = _edited(edit)
    assert loaded.as_view()["extra"]["when"] == "2026-10-07"
    assert loaded.decimal("extra.negative") == Decimal(-1)
    assert loaded.strings("extra.names") == ("a", "b")
    with pytest.raises(ConfigError, match="must be >= 0"):
        loaded.ratio("extra.negative")
    with pytest.raises(ConfigError, match="list of strings"):
        loaded.strings("extra.mixed")
    with pytest.raises(ConfigError, match="must be an integer"):
        loaded.int("extra.flag")
    with pytest.raises(ConfigError, match="must be a number"):
        loaded.decimal("extra.names")


@pytest.mark.parametrize("key", ["", "window.missing", "window.default_days.deeper", "a..b"])
def test_accessors_never_default(shipped: Thresholds, key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        shipped.value(key)
    assert caught.value.file == THRESHOLDS_FILE


def test_merged_key_errors_name_the_mapping_file(shipped: Thresholds) -> None:
    with pytest.raises(ConfigError) as caught:
        shipped.int("crm.booking_sources")
    assert caught.value.file == HUBSPOT_MAPPING_FILE


@pytest.mark.parametrize("bad_key", ["a.b", "", 7])
def test_threshold_keys_must_be_plain_strings(bad_key: object) -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["window"][bad_key] = 1

    with pytest.raises(ConfigError, match="non-empty string without dots"):
        _edited(edit)


def test_unsupported_yaml_value_types_fail() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["extra"] = {"blob": b"\x00"}

    with pytest.raises(ConfigError, match="unsupported value type bytes"):
        _edited(edit)


def test_crm_key_in_both_files_fails() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["crm"]["booking_sources"] = ["MEETINGS_PUBLIC"]

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == "crm.booking_sources"
    assert "defined in both" in caught.value.detail


def test_thresholds_crm_must_be_a_mapping() -> None:
    def edit(trees: Trees) -> None:
        trees[THRESHOLDS_FILE]["crm"] = ["lead_maturity_days"]

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == "crm"


def test_thresholds_without_crm_namespace_fail_on_its_keys() -> None:
    def edit(trees: Trees) -> None:
        del trees[THRESHOLDS_FILE]["crm"]

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == "crm.lead_maturity_days"


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_from_trees_requires_mappings(name: str) -> None:
    trees = _shipped_trees()
    trees[name] = ["not", "a", "mapping"]
    with pytest.raises(ConfigError) as caught:
        _build(trees)
    assert caught.value.file == name


# ---------------------------------------------------------------------------
# White paper registry (AAP 0.5.2, 0.5.4.7)
# ---------------------------------------------------------------------------


def test_form_listed_under_two_assets_fails() -> None:
    def edit(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"][1]["lead_forms"].append(PLAYBOOK_FORM)

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == CONTENT_ASSETS_FILE
    assert caught.value.key == "assets[2].lead_forms[0]"
    assert "listed under two assets" in caught.value.detail


def test_creative_listed_under_two_assets_fails() -> None:
    def edit(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"][0]["creative_overrides"] = [OVERRIDE_CREATIVE]

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == "assets[2].creative_overrides[0]"
    assert "'b2b-pipeline-report-2026' and 'attribution-playbook'" in caught.value.detail


def test_override_contradicting_its_creatives_form_fails(shipped: Thresholds) -> None:
    with pytest.raises(ConfigError) as caught:
        shipped.check_overrides({OVERRIDE_CREATIVE: BENCHMARKS_FORM})
    assert caught.value.file == CONTENT_ASSETS_FILE
    assert caught.value.key == "assets[2].creative_overrides[0]"
    assert "paid-media-benchmarks-2026" in caught.value.detail


@pytest.mark.parametrize(
    "creative_forms",
    [
        {},
        {OVERRIDE_CREATIVE: None},
        {OVERRIDE_CREATIVE: PLAYBOOK_FORM},
        {OVERRIDE_CREATIVE: "urn:li:leadGenForm:42"},
        {"urn:li:sponsoredCreative:7": BENCHMARKS_FORM},
    ],
)
def test_consistent_overrides_pass(shipped: Thresholds, creative_forms: dict[str, Any]) -> None:
    shipped.check_overrides(creative_forms)


@pytest.mark.parametrize(
    ("field", "value", "key", "message"),
    [
        (
            "lead_forms",
            ["urn:li:versionedLeadGenForm:(urn:li:leadGenForm:1,2)"],
            "lead_forms[0]",
            "versioned",
        ),
        ("lead_forms", ["urn:li:leadGenForm:abc"], "lead_forms[0]", "canonical lead form URN"),
        ("lead_forms", [PLAYBOOK_FORM, PLAYBOOK_FORM], "lead_forms[1]", "listed twice"),
        ("lead_forms", "urn:li:leadGenForm:1", "lead_forms", "list of URNs"),
        (
            "creative_overrides",
            ["urn:li:sponsoredCampaign:1"],
            "creative_overrides[0]",
            "creative URN",
        ),
        ("creative_overrides", [7], "creative_overrides[0]", "must be a string"),
        ("slug", "Attribution Playbook", "slug", "kebab-case"),
        ("slug", "", "slug", "blank"),
        ("title", None, "title", "must be a string"),
    ],
)
def test_registry_entries_are_validated(field: str, value: object, key: str, message: str) -> None:
    def edit(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"][2][field] = value

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == CONTENT_ASSETS_FILE
    assert caught.value.key == f"assets[2].{key}"
    assert message in caught.value.detail


@pytest.mark.parametrize(
    ("edit_entry", "key"),
    [
        (lambda entry: entry.pop("lead_forms"), "assets[0].lead_forms"),
        (lambda entry: entry.pop("slug"), "assets[0].slug"),
        (lambda entry: entry.update({"owner": "x"}), "assets[0]"),
    ],
)
def test_registry_entry_keys(edit_entry: Callable[[dict[str, Any]], object], key: str) -> None:
    def edit(trees: Trees) -> None:
        edit_entry(trees[CONTENT_ASSETS_FILE]["assets"][0])

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.key == key


def test_registry_shape_errors() -> None:
    def duplicate_slug(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"][1]["slug"] = "b2b-pipeline-report-2026"

    with pytest.raises(ConfigError, match=r"already used by assets\[0\]"):
        _edited(duplicate_slug)

    def not_a_list(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"] = None

    with pytest.raises(ConfigError, match=r"use \[\] for none"):
        _edited(not_a_list)

    def entry_not_mapping(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"].append("paid-media")

    with pytest.raises(ConfigError) as caught:
        _edited(entry_not_mapping)
    assert caught.value.key == "assets[3]"

    def unknown_top_level(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["white_papers"] = []

    with pytest.raises(ConfigError, match="unknown key"):
        _edited(unknown_top_level)


def test_empty_registry_and_null_overrides_are_valid() -> None:
    def empty(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"] = []

    assert _edited(empty).content_asset_rows() == []

    def null_overrides(trees: Trees) -> None:
        trees[CONTENT_ASSETS_FILE]["assets"][0]["creative_overrides"] = None
        del trees[CONTENT_ASSETS_FILE]["assets"][1]["creative_overrides"]

    loaded = _edited(null_overrides)
    assert loaded.content_assets[0].creative_urns == ()
    assert loaded.content_assets[1].creative_urns == ()


def test_content_asset_rejects_non_tuple_urns() -> None:
    with pytest.raises(ConfigError, match="tuple of URNs"):
        ContentAsset("a", "A", [PLAYBOOK_FORM])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# HubSpot mapping (AAP 0.5.4.7)
# ---------------------------------------------------------------------------


def test_property_mode_binding_keeps_labels_for_display() -> None:
    def edit(trees: Trees) -> None:
        trees[HUBSPOT_MAPPING_FILE]["mql"] = {
            "mode": "property",
            "stage_label": "First meeting booked",
            "property": "date_first_meeting_was_booked",
        }

    mql = _edited(edit).mapping.mql
    assert mql == StageBinding(
        "property", ("First meeting booked",), "date_first_meeting_was_booked"
    )


@pytest.mark.parametrize(
    ("binding", "key", "message"),
    [
        ({"mode": "label", "stage_label": "X", "property": None}, "mql.mode", "must be one of"),
        ({"stage_label": "X"}, "mql.mode", "required key is missing"),
        (
            {"mode": "stage_label", "stage_label": None, "property": None},
            "mql.stage_labels",
            "at least one label",
        ),
        (
            {"mode": "stage_label", "stage_label": "X", "property": "p"},
            "mql.property",
            "must be null",
        ),
        (
            {"mode": "property", "stage_label": "X", "property": None},
            "mql.property",
            "needs a contact property",
        ),
        ({"mode": "property", "property": " p"}, "mql.property", "surrounding whitespace"),
        ({"mode": "stage_label", "stage_label": "  "}, "mql.stage_label", "blank"),
        ({"mode": "stage_label", "stage_label": "X", "stage_labels": ["Y"]}, "mql", "not both"),
        (
            {"mode": "stage_label", "stage_labels": ["Y", "Y"]},
            "mql.stage_labels[1]",
            "listed twice",
        ),
        ({"mode": "stage_label", "label": "X"}, "mql", "unknown key"),
        ("First meeting booked", "mql", "must be a mapping"),
    ],
)
def test_stage_bindings_are_validated(binding: object, key: str, message: str) -> None:
    def edit(trees: Trees) -> None:
        trees[HUBSPOT_MAPPING_FILE]["mql"] = binding

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == HUBSPOT_MAPPING_FILE
    assert caught.value.key == key
    assert message in caught.value.detail


@pytest.mark.parametrize(
    ("edit_mapping", "key"),
    [
        (lambda mapping: mapping.pop("ql"), None),
        (lambda mapping: mapping.update({"sql": {}}), None),
        (lambda mapping: mapping["crm"].pop("booking_sources"), "crm"),
        (lambda mapping: mapping["crm"].update({"lead_maturity_days": 14}), "crm"),
        (
            lambda mapping: mapping["crm"].update({"booking_sources": "MEETINGS_PUBLIC"}),
            "crm.booking_sources",
        ),
        (
            lambda mapping: mapping["crm"].update({"meeting_outcome_exclusions": [""]}),
            "crm.meeting_outcome_exclusions[0]",
        ),
    ],
)
def test_mapping_structure_is_validated(
    edit_mapping: Callable[[dict[str, Any]], object], key: str | None
) -> None:
    def edit(trees: Trees) -> None:
        edit_mapping(trees[HUBSPOT_MAPPING_FILE])

    with pytest.raises(ConfigError) as caught:
        _edited(edit)
    assert caught.value.file == HUBSPOT_MAPPING_FILE
    assert caught.value.key == key


def test_mapping_lists_may_be_empty() -> None:
    def edit(trees: Trees) -> None:
        trees[HUBSPOT_MAPPING_FILE]["crm"]["meeting_outcome_exclusions"] = []

    loaded = _edited(edit)
    assert loaded.mapping.meeting_outcome_exclusions == ()
    assert loaded.strings("crm.meeting_outcome_exclusions") == ()


def test_hubspot_mapping_type_checks() -> None:
    binding = StageBinding("stage_label", ("Qualified Lead",), None)
    with pytest.raises(ConfigError) as caught:
        HubSpotMapping(binding, "ql", ("MEETINGS_PUBLIC",), ())  # type: ignore[arg-type]
    assert caught.value.key == "ql"
    with pytest.raises(ConfigError) as caught:
        HubSpotMapping(binding, binding, ["MEETINGS_PUBLIC"], ())  # type: ignore[arg-type]
    assert caught.value.key == "crm.booking_sources"
    with pytest.raises(ConfigError, match="tuple of labels"):
        StageBinding("stage_label", ["Qualified Lead"], None)  # type: ignore[arg-type]
    with pytest.raises(ConfigError) as caught:
        StageBinding("stage_label", ("",), None)
    assert caught.value.key == "stage_labels[0]"


# ---------------------------------------------------------------------------
# Resolved CRM bindings (source_dataset.crm_bindings)
# ---------------------------------------------------------------------------

VERIFIED_AT = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)


def _bindings(verified_at: datetime = VERIFIED_AT) -> CrmBindings:
    return CrmBindings(
        mql=ResolvedBinding(
            available=False,
            label="First meeting booked",
            reason="stage label 'First meeting booked' not found in lifecyclestage options",
        ),
        ql=ResolvedBinding(
            available=True,
            property="hs_v2_date_entered_1406943076",
            stage_id="1406943076",
            label="Qualified Lead",
        ),
        google=GoogleCrmBinding(
            available=False, reason="HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY not configured"
        ),
        verified_at=verified_at,
    )


def test_crm_bindings_round_trip() -> None:
    bindings = _bindings()
    document = bindings.to_json()
    assert document["verified_at"] == "2026-10-07T09:30:00+00:00"
    assert document["ql"]["property"] == "hs_v2_date_entered_1406943076"
    assert CrmBindings.from_json(document) == bindings
    # JSON-serialisable as stored in jsonb.
    assert CrmBindings.from_json(json.loads(json.dumps(document))) == bindings


def test_crm_bindings_round_trip_normalises_offsets_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    bindings = _bindings(datetime(2026, 10, 7, 5, 30, tzinfo=eastern))
    decoded = CrmBindings.from_json(bindings.to_json())
    assert decoded == bindings
    assert decoded.verified_at.utcoffset() == timedelta(0)


def test_google_binding_round_trip() -> None:
    google = GoogleCrmBinding(
        available=True, campaign_property="gclid_campaign", touch_date_property="gclid_touch_at"
    )
    assert GoogleCrmBinding.from_json(google.to_json()) == google


def test_unavailable_bindings() -> None:
    bindings = CrmBindings.unavailable("CRM bindings not yet verified", at=VERIFIED_AT)
    assert not bindings.mql.available
    assert not bindings.ql.available
    assert not bindings.google.available
    assert bindings.ql.reason == "CRM bindings not yet verified"
    assert CrmBindings.from_json(bindings.to_json()) == bindings
    with pytest.raises(ConfigError):
        CrmBindings.unavailable("", at=VERIFIED_AT)
    with pytest.raises(ConfigError) as caught:
        CrmBindings.unavailable("x", at=datetime(2026, 10, 7))  # noqa: DTZ001 - naive on purpose
    assert caught.value.key == "verified_at"


@pytest.mark.parametrize(
    ("mutate", "key"),
    [
        (lambda doc: doc.pop("verified_at"), None),
        (lambda doc: doc.update({"extra": 1}), None),
        (lambda doc: doc["mql"].pop("reason"), "mql"),
        (lambda doc: doc["ql"].update({"available": "yes"}), "ql.available"),
        (lambda doc: doc["ql"].update({"property": None}), "ql.property"),
        (lambda doc: doc["mql"].update({"reason": None}), "mql.reason"),
        (lambda doc: doc["google"].update({"available": True}), "google.campaign_property"),
        (lambda doc: doc["google"].update({"reason": None}), "google.reason"),
        (lambda doc: doc["google"].update({"campaign_property": ""}), "google.campaign_property"),
        (lambda doc: doc.update({"google": []}), "google"),
        (lambda doc: doc.update({"verified_at": 1}), "verified_at"),
        (lambda doc: doc.update({"verified_at": "yesterday"}), "verified_at"),
        (lambda doc: doc.update({"verified_at": "2026-10-07T09:30:00"}), "verified_at"),
    ],
)
def test_crm_bindings_decode_strictly(
    mutate: Callable[[dict[str, Any]], object], key: str | None
) -> None:
    document = _bindings().to_json()
    mutate(document)
    with pytest.raises(ConfigError) as caught:
        CrmBindings.from_json(document)
    assert caught.value.file == CRM_BINDINGS_DOCUMENT
    assert caught.value.key == key


def test_crm_bindings_part_types() -> None:
    google = GoogleCrmBinding(available=False, reason="x")
    resolved = ResolvedBinding(available=False, reason="x")
    with pytest.raises(ConfigError) as caught:
        CrmBindings(google, resolved, google, VERIFIED_AT)  # type: ignore[arg-type]
    assert caught.value.key == "mql"
    with pytest.raises(ConfigError) as caught:
        CrmBindings(resolved, resolved, resolved, VERIFIED_AT)  # type: ignore[arg-type]
    assert caught.value.key == "google"
    with pytest.raises(ConfigError):
        CrmBindings.from_json([])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Files, directories and errors
# ---------------------------------------------------------------------------


def test_resolve_config_dir_from_repo_root_and_backend(tmp_path: Path) -> None:
    assert resolve_config_dir(REPO_ROOT) == REPO_ROOT / "config"
    assert resolve_config_dir(REPO_ROOT / "backend") == REPO_ROOT / "config"
    with pytest.raises(ConfigError, match="no config directory"):
        resolve_config_dir(tmp_path)


def test_resolve_config_dir_defaults_to_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT / "backend")
    assert resolve_config_dir() == REPO_ROOT / "config"
    assert Thresholds.load().source_dir == REPO_ROOT / "config"


def test_load_accepts_a_string_path() -> None:
    assert Thresholds.load(str(CONFIG_DIR)).config_hash == Thresholds.load(CONFIG_DIR).config_hash


def test_missing_directory_and_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config directory not found"):
        Thresholds.load(tmp_path / "absent")
    directory = _copy_config(tmp_path / "config")
    (directory / HUBSPOT_MAPPING_FILE).unlink()
    with pytest.raises(ConfigError) as caught:
        Thresholds.load(directory)
    assert caught.value.file == HUBSPOT_MAPPING_FILE
    assert "file not found" in caught.value.detail


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"window: [1, 2\n", "invalid YAML"),
        (b"- 1\n- 2\n", "top level must be a mapping, found list"),
        (b"", "found an empty file"),
        (b"k1:\n  baseline_days: 90\nk1:\n  baseline_days: 30\n", "duplicate key 'k1'"),
        (b"window:\n  max_days: 1\n  max_days: 2\n", "duplicate key 'max_days'"),
        (b"window: \xff\xfe\n", "not valid UTF-8"),
        (b"--- {a: 1}\n--- {b: 2}\n", "invalid YAML"),
        (b"? [a, b]\n: 1\n", "unhashable key"),
    ],
)
def test_unreadable_yaml_fails_naming_the_file(
    tmp_path: Path, content: bytes, message: str
) -> None:
    directory = _copy_config(tmp_path / "config")
    (directory / THRESHOLDS_FILE).write_bytes(content)
    with pytest.raises(ConfigError) as caught:
        Thresholds.load(directory)
    assert caught.value.file == THRESHOLDS_FILE
    assert caught.value.key is None
    assert message in caught.value.detail


def test_yaml_merge_keys_may_be_overridden(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path / "config")
    path = directory / THRESHOLDS_FILE
    text = (
        path.read_text("utf-8").replace("c1b:\n", "c1b: &c1b_defaults\n", 1)
        + "\nextra:\n  <<: *c1b_defaults\n  min_window_days: 21\n"
    )
    path.write_text(text, "utf-8")
    assert Thresholds.load(directory).int("extra.min_window_days") == 21


def test_unreadable_path_is_reported(tmp_path: Path) -> None:
    directory = _copy_config(tmp_path / "config")
    (directory / THRESHOLDS_FILE).unlink()
    (directory / THRESHOLDS_FILE).mkdir()
    with pytest.raises(ConfigError, match="cannot read"):
        Thresholds.load(directory)


def test_config_error_message_and_pickling() -> None:
    error = ConfigError(THRESHOLDS_FILE, "window.presets", "must contain window.default_days")
    assert str(error) == "thresholds.yaml: window.presets: must contain window.default_days"
    assert str(ConfigError("x.yaml", None, "bad")) == "x.yaml: bad"
    assert isinstance(error, ValueError)
    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 - round-trip of our own object
    assert (restored.file, restored.key, restored.detail) == (
        error.file,
        error.key,
        error.detail,
    )


def test_direct_construction_is_validated(shipped: Thresholds) -> None:
    with pytest.raises(ConfigError, match="SHA-256"):
        Thresholds(
            raw=shipped.raw,
            mapping=shipped.mapping,
            content_assets=shipped.content_assets,
            config_hash="not-a-hash",
            source_dir=CONFIG_DIR,
        )
    other_mapping = HubSpotMapping(
        shipped.mapping.mql, shipped.mapping.ql, ("MEETINGS_PUBLIC", "CRM_UI"), ("CANCELED",)
    )
    with pytest.raises(ConfigError) as caught:
        Thresholds(
            raw=shipped.raw,
            mapping=other_mapping,
            content_assets=shipped.content_assets,
            config_hash=shipped.config_hash,
            source_dir=CONFIG_DIR,
        )
    assert caught.value.key == "crm.booking_sources"
    with pytest.raises(ConfigError, match="must be a mapping"):
        Thresholds(
            raw=[],  # type: ignore[arg-type]
            mapping=shipped.mapping,
            content_assets=shipped.content_assets,
            config_hash=shipped.config_hash,
            source_dir=CONFIG_DIR,
        )
    with pytest.raises(ConfigError, match="HubSpotMapping"):
        Thresholds(
            raw=shipped.raw,
            mapping={},  # type: ignore[arg-type]
            content_assets=shipped.content_assets,
            config_hash=shipped.config_hash,
            source_dir=CONFIG_DIR,
        )
    with pytest.raises(ConfigError, match="tuple of ContentAsset"):
        Thresholds(
            raw=shipped.raw,
            mapping=shipped.mapping,
            content_assets=list(shipped.content_assets),  # type: ignore[arg-type]
            config_hash=shipped.config_hash,
            source_dir=CONFIG_DIR,
        )
    with pytest.raises(ConfigError, match="must be a ContentAsset"):
        Thresholds(
            raw=shipped.raw,
            mapping=shipped.mapping,
            content_assets=("asset",),  # type: ignore[arg-type]
            config_hash=shipped.config_hash,
            source_dir=CONFIG_DIR,
        )
    rebuilt = Thresholds(
        raw=dict(shipped.raw),
        mapping=shipped.mapping,
        content_assets=shipped.content_assets,
        config_hash=shipped.config_hash,
        source_dir=str(CONFIG_DIR),  # type: ignore[arg-type]
    )
    assert rebuilt == shipped
    assert rebuilt.source_dir == CONFIG_DIR
