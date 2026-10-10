"""End-to-end simulated run: with no credentials, sync, analysis and API produce all five sections.

AAP 0.11.2 ``integration/test_end_to_end_simulated.py`` (**Simulated run**: "with
``DATA_MODE=simulated`` and no credentials, sync, analysis and UI produce all five sections,
each item showing evidence, window, source boundaries and simulated labels"), AAP 0.7.1
(simulated-data paths), 0.5.4.8 (the evidence every recommendation carries), 0.5.4.1 (native
day boundaries; ``DISPLAY_TIMEZONE`` never moves one), 0.5.5 (the field sets each section
shows, as stored in evidence) and 0.1.1 (the sections render from a precomputed run in under
2 s; a custom-window run computes in under 30 s). The UI half ("Decisions in the UI") belongs
to ``web/tests/e2e/*.spec.ts``; this module proves the backend produces what the UI renders.

Everything goes through the real product pipeline against the PostgreSQL 18 test database,
with no network: the simulator's ``httpx.MockTransport`` answers every platform call in its
live wire shape, and the conftest guard refuses any real transport.

* :func:`test_simulated_pipeline_produces_every_section_with_evidence` seeds in process:
  ``bootstrap_sources`` builds, backfills and activates a simulated dataset per source (plus
  LinkedIn's companion CRM dataset), ``run_source_jobs`` runs every source's manual sync, and
  ``run_analysis`` computes the default-window run. ``GET /api/v1/recommendations`` is then
  checked guarantee by guarantee (one subtest each): five populated sections, evidence with
  window and source boundaries on every item, simulated labels, the simulated-sources banner
  and modes, the per-section field sets, complete dataset freshness, a page under 2 s, a
  deterministic re-analysis and a 90-day custom run under 30 s.
* :func:`test_seed_simulated_cli` runs the first-start command ``ads-engine seed-simulated``
  in a subprocess on an empty database and checks the page its default run serves with the
  same helpers.

What the default world must produce comes from the scenario catalogue's ``default`` entry
(``SCENARIOS[DEFAULT_SCENARIO]``, whose module is this file), so a regression in
``simulation/scenarios.py`` or ``DEFAULT_SPEC`` fails here naming the empty section, the
missing rule or the item lacking evidence.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from functools import partial
from subprocess import CompletedProcess
from time import perf_counter
from typing import Any, Final

import anyio
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ads_engine.analysis.context import (
    B1,
    B2,
    B4,
    BANNER_OPEN_DISCREPANCIES,
    BANNER_SIMULATED_SOURCES,
    BUDGET,
    C1,
    C2,
    C3A,
    C3B,
    CAMPAIGNS,
    COHORT_GROUP_PREFIX,
    CR1,
    CR3,
    CREATIVE,
    CREATIVE_GROUP_PREFIX,
    DATA_SIMULATED,
    FLAG_SIMULATED,
    FULL_STATE_DATASETS,
    GROUP_BLOCK,
    GROUP_EXPAND,
    GROUP_ORDER,
    GROUP_REVIEW,
    K1,
    K2,
    K3,
    KEYWORDS,
    SECTION_ORDER,
    W1,
    WHITE_PAPERS,
)
from ads_engine.analysis.runner import default_window, run_analysis
from ads_engine.db.models.analysis import AnalysisRun
from ads_engine.db.models.ops import DatasetFreshness, SourceDataset, SyncRun
from ads_engine.settings import SOURCES, Settings, get_settings
from ads_engine.simulation.scenarios import DEFAULT_SCENARIO, MODULE_END_TO_END, SCENARIOS
from ads_engine.sync.freshness import SOURCE_DATASETS
from ads_engine.sync.jobs import JOB_CRM_COMPANION, SOURCE_JOBS, run_source_jobs
from ads_engine.sync.mode import REASON_FORCED, SourceModeController, bootstrap_sources

pytestmark = pytest.mark.anyio

type Json = dict[str, Any]
type SetEnv = Callable[..., None]
type RunCli = Callable[..., CompletedProcess[str]]
type Sessions = async_sessionmaker[AsyncSession]

# ---------------------------------------------------------------------------------------------
# What the default world promises
# ---------------------------------------------------------------------------------------------

SCENARIO: Final = SCENARIOS[DEFAULT_SCENARIO]
"""The catalogue's first-start world, the one ``DATA_MODE=simulated`` builds from ``MOCK_SEED``."""

MOCK_SEED: Final = "42"
"""``MOCK_SEED``: the documented default, so the run reproduces the shipped fixtures' world."""

BACKFILL_DAYS: Final = "120"
"""``BACKFILL_DAYS`` for the seed: covers the 28-day default window, K1's 90-day baseline
(``k1.baseline_days``) ending on the window end and the 90-day custom run, with a margin.
It stays below the default 180 because every simulated HubSpot call still passes the client's
4 requests/s throttle, so the CRM backfill time grows with the backfill range."""

DISPLAY_TIMEZONE: Final = "Europe/Berlin"
"""A display zone east of UTC: it must format instants only and never move a day boundary
(AAP 0.5.4.1), so every Google and LinkedIn boundary stays as the sources define it."""

CUSTOM_DAYS: Final = 90
"""The custom window of the performance check: the longest preset, ending on the default end."""

PAGE_BUDGET_SECONDS: Final = 2.0
"""AAP 0.1.1: the five sections render from a precomputed run in under 2 s."""

CUSTOM_RUN_BUDGET_SECONDS: Final = 30.0
"""AAP 0.1.1: a custom-window run on a typical account computes in under 30 s."""

CLI_TIMEOUT_SECONDS: Final = 270.0
"""Ceiling of the ``seed-simulated`` subprocess. The first start takes about 90 s here, most of
it the HubSpot throttle; the ceiling reports a hang as a failure before pytest's own timeout."""

LINKEDIN_BOUNDARY: Final = "UTC"
"""LinkedIn reports daily buckets on UTC days (AAP 0.3.3)."""

DAY_SOURCES: Final[tuple[str, ...]] = ("google", "linkedin")
"""The sources with native daily rows; HubSpot holds instants only, so it has no day boundary."""

SOURCE_LABELS: Final[Mapping[str, str]] = {
    "google": "Google",
    "linkedin": "LinkedIn",
    "hubspot": "HubSpot",
}
"""How the simulated-sources banner names each source."""

FIRST_START_RULES: Final[Mapping[str, frozenset[str]]] = {
    KEYWORDS: frozenset({K1, K2, K3}),
    CAMPAIGNS: frozenset({C1, C3A, C3B}),
    BUDGET: frozenset({B4}),
    # CR2 (the weak stage) is stated by the CR3 card of each campaign block; it has no item.
    CREATIVE: frozenset({CR1, CR3}),
    WHITE_PAPERS: frozenset({W1}),
}
"""The rules the default world makes fire on first start (``DEFAULT_SPEC``; the default
scenario's guarantee): every one of them must surface as at least one item of its section."""

FIRST_START_WITHHELD: Final[Mapping[str, frozenset[str]]] = {
    # No snapshot history exists on first start, so every budget day is unobserved: the cap
    # rules (C2, B1) and the high-utilization rule (B2) are listed as withheld with their day
    # counts (AAP 0.5.4.1 "Short history"). B3 is not expected here: it lists a withheld line
    # only once known days show under-utilized spending, and an all-unobserved history shows
    # none (analysis/budget.py), so there is no B3 pattern to withhold yet.
    CAMPAIGNS: frozenset({C2}),
    BUDGET: frozenset({B1, B2}),
}
"""Rules the default world withholds on first start, each with observed and required sizes."""

KEYWORD_FIELDS: Final = frozenset({"term", "spend", "conversions", "cost_per_conversion"})
"""AAP 0.5.4.3: term, spend, conversions and cost/conversion on every keyword item (plus the
reason, which every item carries)."""

K3_FIELDS: Final = frozenset({"qs_first", "qs_last", "cpc_first_half", "cpc_second_half"})
"""AAP 0.5.5: review cards add the quality score and CPC by half."""

BUDGET_FIELDS: Final = frozenset(
    {
        "window_utilization",
        "cost_per_outcome",
        "daily_budget",
        "known_days",
        "spending_days",
        "idle_days",
        "changed_intraday_days",
        "unobserved_days",
    }
)
"""AAP 0.5.4.5: utilization and efficiency as separate adjacent metrics, the daily budget and
the day counts (known-budget, spending, idle, changed-intraday and unobserved days)."""

VARIANT_COLUMNS: Final = frozenset(
    {"impressions", "click_rate", "open_rate", "completion_rate", "leads"}
)
"""AAP 0.5.5: the copy variants table (impressions, click, open and completion rate, leads)."""


# ---------------------------------------------------------------------------------------------
# Page walking
# ---------------------------------------------------------------------------------------------


def _items(page: Json) -> Iterator[tuple[str, str, Json]]:
    """Yield ``(section key, group key, item)`` for every item of the page, in page order."""
    for section in page["sections"]:
        for group in section["groups"]:
            for item in group["items"]:
                yield section["key"], group["key"], item


def _label(section: str, group: str, item: Json) -> str:
    """Name one item in a failure message: section, group, rank, rule and entity."""
    entity = item.get("entity") or {}
    return (
        f"{section}/{group} rank {item.get('rank')} {item.get('rule_id')} "
        f"{entity.get('name')!r} (recommendation {item.get('id')})"
    )


def _metric_keys(item: Json) -> set[str]:
    """The keys of an item's evidence metrics."""
    return {metric.get("key") for metric in item["evidence"].get("metrics") or []}


def _extra(item: Json) -> Mapping[str, Any]:
    """An item's ``evidence.extra``, empty when absent."""
    extra = item["evidence"].get("extra")
    return extra if isinstance(extra, Mapping) else {}


def _report(problems: Sequence[str], guarantee: str) -> None:
    """Fail naming the guarantee and every problem found, or return when there is none."""
    assert not problems, f"{guarantee}:\n  " + "\n  ".join(problems)


def _google_zone() -> str:
    """The Google Ads account time zone of the default world (Google's native day)."""
    google = SCENARIO.spec.google
    assert google is not None, "the default world has no Google Ads customer"
    assert google.time_zone != LINKEDIN_BOUNDARY, (
        "the default world must put Google on a non-UTC day so differing boundaries are shown"
    )
    return google.time_zone


def _google_crm_configured(settings: Settings) -> bool:
    """Whether both Google-to-CRM contact properties are configured (AAP 0.3.3)."""
    return bool(
        settings.hubspot_google_campaign_property and settings.hubspot_google_touch_date_property
    )


# ---------------------------------------------------------------------------------------------
# Guarantee checks on GET /api/v1/recommendations
# ---------------------------------------------------------------------------------------------


def _check_five_sections_populated(page: Json) -> None:
    """All five sections, in order, each populated; static groups in their fixed order.

    Keywords always offer the Block, Expand and Review sub-lists (each populated on first
    start), Campaigns start with the Discrepancies group, Copy has one ``campaign:{id}`` block
    per campaign and White papers one ``cohort:{CURRENCY}`` table per currency. Every rule the
    default world makes fire surfaces in its section, and the withheld budget patterns are
    listed with their observed and required sample sizes.
    """
    problems: list[str] = []
    keys = [section["key"] for section in page["sections"]]
    if keys != list(SECTION_ORDER):
        problems.append(f"sections are {keys}, expected {list(SECTION_ORDER)}")
    populated = tuple(SCENARIO.expect.values.get("populated_sections", SECTION_ORDER))
    for section in page["sections"]:
        key = section["key"]
        groups = section["groups"]
        group_keys = [group["key"] for group in groups]
        items = [item for group in groups for item in group["items"]]
        if key in populated and not items:
            problems.append(
                f"section {key} ({section['title']}) has no items; findings "
                f"{[finding['code'] for finding in section['findings']]}, withheld "
                f"{[(line['rule_id'], line['reason']) for line in section['withheld']]}"
            )
        static = list(GROUP_ORDER.get(key, ()))
        if group_keys[: len(static)] != static:
            problems.append(f"section {key} groups start {group_keys}, expected {static} first")
        prefix = {CREATIVE: CREATIVE_GROUP_PREFIX, WHITE_PAPERS: COHORT_GROUP_PREFIX}.get(key)
        if prefix is not None:
            strays = [group_key for group_key in group_keys if not group_key.startswith(prefix)]
            if strays or not group_keys:
                problems.append(f"section {key} groups {group_keys} are not all {prefix}*")
        if key == KEYWORDS:
            for group in groups:
                if group["key"] in (GROUP_BLOCK, GROUP_EXPAND, GROUP_REVIEW) and not group["items"]:
                    problems.append(f"keywords group {group['key']} ({group['title']}) is empty")
        missing_rules = FIRST_START_RULES.get(key, frozenset()) - {
            item["rule_id"] for item in items
        }
        if missing_rules:
            problems.append(f"section {key} lacks items of rule(s) {sorted(missing_rules)}")
        withheld = {line["rule_id"]: line for line in section["withheld"]}
        for rule_id in sorted(FIRST_START_WITHHELD.get(key, frozenset())):
            line = withheld.get(rule_id)
            if line is None:
                problems.append(f"section {key} lists no withheld {rule_id} pattern")
            elif line["sample"].get("observed") is None or line["sample"].get("required") is None:
                problems.append(f"withheld {rule_id} in {key} lacks observed/required sizes")
    _report(problems, "the simulated run must populate all five sections")


def _check_evidence_window_and_boundaries(
    page: Json, *, google_zone: str, active_ids: Mapping[str, frozenset[int]]
) -> None:
    """Every item carries its numbers, thresholds, datasets, provenance and exact window.

    AAP 0.5.4.8 and 0.5.4.1: the evidence window equals the run's window, ``source_days`` names
    each day source (Google on the account's zone, LinkedIn on UTC) with the window's dates and
    covers every day source the item read, ``boundaries_differ`` and ``description`` say that
    those boundaries differ, metrics carry units (and a currency for money),
    thresholds and datasets are present and complete, and the frozen provenance names active
    simulated datasets. ``action`` and ``reason`` are stated.
    """
    run = page["run"]
    problems: list[str] = []
    for section, group, item in _items(page):
        where = _label(section, group, item)
        evidence = item.get("evidence") or {}
        if not str(item.get("action") or "").strip():
            problems.append(f"{where}: no action")
        if not str(item.get("reason") or "").strip():
            problems.append(f"{where}: no reason")
        entity = item.get("entity") or {}
        if entity.get("source") not in SOURCES or not str(entity.get("name") or "").strip():
            problems.append(f"{where}: entity {entity} lacks a source or a name")

        window = evidence.get("window") or {}
        if (window.get("start"), window.get("end"), window.get("days")) != (
            run["window_start"],
            run["window_end"],
            run["days"],
        ):
            problems.append(
                f"{where}: evidence window {window.get('start')}..{window.get('end')} "
                f"({window.get('days')} days) differs from the run's {run['window_start']}.."
                f"{run['window_end']} ({run['days']} days)"
            )
        source_days = {entry["source"]: entry for entry in window.get("source_days") or []}
        expected_boundaries = {"google": google_zone, "linkedin": LINKEDIN_BOUNDARY}
        for source, boundary in expected_boundaries.items():
            entry = source_days.get(source)
            if entry is None:
                problems.append(f"{where}: source_days names no {source} day boundary")
                continue
            if entry["boundary"] != boundary:
                problems.append(f"{where}: {source} boundary {entry['boundary']}, not {boundary}")
            if (entry["start"], entry["end"]) != (run["window_start"], run["window_end"]):
                problems.append(f"{where}: {source} days {entry['start']}..{entry['end']}")
        if window.get("boundaries_differ") is not True:
            problems.append(
                f"{where}: boundaries_differ is {window.get('boundaries_differ')!r}, but Google "
                f"days end in {google_zone} and LinkedIn days in {LINKEDIN_BOUNDARY}"
            )
        description = (
            f"Google {run['window_start']} to {run['window_end']} ({google_zone}); "
            f"LinkedIn same dates ({LINKEDIN_BOUNDARY})"
        )
        if window.get("description") != description:
            problems.append(f"{where}: window description {window.get('description')!r}")

        metrics = evidence.get("metrics") or []
        if not metrics:
            problems.append(f"{where}: evidence cites no metric")
        for metric in metrics:
            if not metric.get("key") or not metric.get("label") or not metric.get("unit"):
                problems.append(f"{where}: metric {metric} lacks a key, label or unit")
            elif metric["unit"] == "currency" and not metric.get("currency"):
                problems.append(f"{where}: money metric {metric['key']} carries no currency")
        if not evidence.get("thresholds"):
            problems.append(f"{where}: evidence names no threshold")
        datasets = evidence.get("datasets") or []
        if not datasets:
            problems.append(f"{where}: evidence names no dataset read")
        for dataset in datasets:
            if dataset.get("completeness") != "complete":
                problems.append(f"{where}: dataset {dataset} is not complete")

        provenance = evidence.get("provenance") or []
        if not provenance:
            problems.append(f"{where}: evidence records no provenance")
        for entry in provenance:
            source = entry.get("source")
            if entry.get("mode") != DATA_SIMULATED:
                problems.append(f"{where}: provenance {entry} is not simulated")
            if entry.get("dataset_id") not in active_ids.get(source, frozenset()):
                problems.append(
                    f"{where}: provenance {entry} names no active {source} dataset "
                    f"{sorted(active_ids.get(source, frozenset()))}"
                )
            if source in DAY_SOURCES and source not in source_days:
                problems.append(f"{where}: read {source} days but source_days omits {source}")
    _report(problems, "every item must carry evidence, window and source boundaries")


def _check_labelled_simulated(page: Json) -> None:
    """Every item is labelled simulated in its frozen status, its flags and its overlay.

    AAP 0.3.3 "Labelling" and P3: ``data_status`` and ``current.data_status_now`` are
    ``simulated``, both the stored flags and ``current.flags_now`` carry ``simulated``, and no
    item is historical, because its provenance datasets are still the active ones.
    """
    problems: list[str] = []
    for section, group, item in _items(page):
        where = _label(section, group, item)
        current = item.get("current") or {}
        if item.get("data_status") != DATA_SIMULATED:
            problems.append(f"{where}: data_status {item.get('data_status')!r}")
        if FLAG_SIMULATED not in {flag["code"] for flag in item.get("flags") or []}:
            problems.append(f"{where}: stored flags {item.get('flags')} lack {FLAG_SIMULATED}")
        if current.get("data_status_now") != DATA_SIMULATED:
            problems.append(f"{where}: data_status_now {current.get('data_status_now')!r}")
        if FLAG_SIMULATED not in {flag["code"] for flag in current.get("flags_now") or []}:
            problems.append(f"{where}: flags_now {current.get('flags_now')} lack {FLAG_SIMULATED}")
        if current.get("historical") is not False:
            problems.append(f"{where}: historical is {current.get('historical')!r}")
    _report(problems, "every item must be labelled as simulated data")


def _check_banner_and_modes(page: Json, *, google_zone: str, reason: str) -> None:
    """The simulated-sources banner names every source with its reason; modes are simulated.

    AAP 0.3.3 "Labelling" and 0.5.5: one ``simulated_sources`` banner names Google, LinkedIn and
    HubSpot each with its simulated connection's current reason; ``overlay.source_modes`` and the
    frozen ``run.data_modes`` are all simulated; ``run.source_days`` names both boundaries and
    ``DISPLAY_TIMEZONE`` stays a display setting. A first start raises no discrepancy and leaves
    no dataset stale or incomplete, so no hold and no other warning banner appears.
    """
    run, overlay = page["run"], page["overlay"]
    problems: list[str] = []
    banners = {banner["code"]: banner for banner in page["banners"]}
    banner = banners.get(BANNER_SIMULATED_SOURCES)
    if banner is None:
        problems.append(f"no {BANNER_SIMULATED_SOURCES} banner among {list(banners)}")
    else:
        if banner.get("count") != len(SOURCES):
            problems.append(f"simulated banner counts {banner.get('count')}, not {len(SOURCES)}")
        for source in SOURCES:
            named = f"{SOURCE_LABELS[source]} ({reason})"
            if named not in banner["message"]:
                problems.append(f"simulated banner {banner['message']!r} does not name {named!r}")
    if BANNER_OPEN_DISCREPANCIES in banners or overlay["open_discrepancies"] != 0:
        problems.append(f"first start shows {overlay['open_discrepancies']} open discrepancies")
    for source in SOURCES:
        if overlay["source_modes"].get(source) != DATA_SIMULATED:
            problems.append(f"overlay mode of {source}: {overlay['source_modes'].get(source)!r}")
        if run["data_modes"].get(source) != DATA_SIMULATED:
            problems.append(f"run data mode of {source}: {run['data_modes'].get(source)!r}")
    if overlay["stale_datasets"] or overlay["incomplete_datasets"]:
        problems.append(
            f"stale {overlay['stale_datasets']} / incomplete {overlay['incomplete_datasets']}"
        )
    boundaries = {entry["source"]: entry["boundary"] for entry in run["source_days"]}
    if boundaries != {"google": google_zone, "linkedin": LINKEDIN_BOUNDARY}:
        problems.append(f"run.source_days boundaries {boundaries}")
    if run["display_timezone"] != DISPLAY_TIMEZONE:
        problems.append(f"display_timezone {run['display_timezone']!r}, not {DISPLAY_TIMEZONE}")
    held = [
        _label(section, group, item)
        for section, group, item in _items(page)
        if item.get("held") or (item.get("current") or {}).get("held_now")
    ]
    if held:
        problems.append(f"items held with no discrepancy open: {held}")
    _report(problems, "the page must label every simulated source with its reason")


def _check_section_field_sets(page: Json) -> None:
    """Each section's items carry the fields AAP 0.5.5 lists for its cards, in evidence.

    * Keywords: term, spend, conversions and cost/conversion; expansion cards add adjacent
      terms (at most ``k2.adjacent_max_terms``), review cards QS and CPC by half.
    * Budget: utilization and efficiency as separate metrics beside the daily budget and the
      day counts; a weekday/weekend card states its test.
    * Copy: each campaign block carries the variants table, and each CR1 pair its test and
      distinguishability wording (P7: no winner without a test).
    * White papers: MQL and QL as two separate metrics, never one combined field, and the
      per-creative ``extra.rows``, each keeping MQL and QL apart too.
    """
    problems: list[str] = []
    variant_tables: dict[str, bool] = {}
    for section, group, item in _items(page):
        where = _label(section, group, item)
        keys = _metric_keys(item)
        extra = _extra(item)
        rule_id = item["rule_id"]
        if section == KEYWORDS:
            if missing := KEYWORD_FIELDS - keys:
                problems.append(f"{where}: keyword card lacks {sorted(missing)}")
            if rule_id == K2:
                adjacent = extra.get("adjacent_terms")
                limit = item["evidence"]["thresholds"].get("k2.adjacent_max_terms")
                if not isinstance(adjacent, list) or (limit is not None and len(adjacent) > limit):
                    problems.append(f"{where}: adjacent terms {adjacent!r} (at most {limit})")
            if rule_id == K3 and (missing := K3_FIELDS - keys):
                problems.append(f"{where}: review card lacks {sorted(missing)}")
        elif section == BUDGET:
            if missing := BUDGET_FIELDS - keys:
                problems.append(f"{where}: budget card lacks {sorted(missing)}")
            if not item["evidence"].get("baselines"):
                problems.append(f"{where}: budget card shows no efficiency baseline")
            test = item["evidence"].get("test") or {}
            if rule_id == B4 and (not test.get("name") or test.get("p_value") is None):
                problems.append(f"{where}: weekday/weekend card states no rate test")
        elif section == CREATIVE:
            rows = extra.get("variants")
            if isinstance(rows, list) and rows:
                complete = all(
                    isinstance(row, Mapping) and row.keys() >= VARIANT_COLUMNS for row in rows
                )
                variant_tables[group] = variant_tables.get(group, False) or complete
                if not complete:
                    problems.append(f"{where}: variant rows lack {sorted(VARIANT_COLUMNS)}")
            else:
                variant_tables.setdefault(group, False)
            if rule_id == CR1:
                test = item["evidence"].get("test") or {}
                if any(test.get(field) is None for field in ("name", "p_value", "p_adjusted")):
                    problems.append(f"{where}: pair comparison states no adjusted test {test}")
                if "distinguishable" not in item["action"].lower():
                    problems.append(f"{where}: pair action {item['action']!r} names no verdict")
        elif section == WHITE_PAPERS:
            if not {"mql", "ql"} <= keys:
                problems.append(f"{where}: MQL and QL are not separate metrics ({sorted(keys)})")
            blended = [
                metric["key"]
                for metric in item["evidence"]["metrics"]
                if metric["key"] not in ("mql", "ql", "cost_per_ql")
                and ("mql" in metric["key"] or "ql" in metric["key"].split("_"))
            ]
            if blended:
                problems.append(f"{where}: metrics {blended} blend MQL and QL")
            rows = extra.get("rows")
            if not isinstance(rows, list) or not rows:
                problems.append(f"{where}: no per-creative rows in extra.rows")
            else:
                for row in rows:
                    if (
                        not isinstance(row, Mapping)
                        or not {"mql", "ql", "creative_urn"} <= row.keys()
                    ):
                        problems.append(
                            f"{where}: creative row {row!r} lacks mql, ql or creative_urn"
                        )
    problems.extend(
        f"creative block {group} has no variants table"
        for group, present in variant_tables.items()
        if not present
    )
    _report(problems, "each section must expose its card fields in evidence")


async def _check_freshness_complete(
    db: Sessions, *, settings: Settings, window_start: date, window_end: date
) -> None:
    """Every dataset of the active datasets was synced completely over the window.

    AAP 0.3.2: one ``dataset_freshness`` row per source dataset; each is ``complete`` with a
    recorded success, and every dated dataset covers the whole window. LinkedIn's companion
    CRM dataset carries its own HubSpot rows.
    """
    async with db() as session:
        datasets = (
            (await session.execute(select(SourceDataset).where(SourceDataset.state == "active")))
            .scalars()
            .all()
        )
        rows = (await session.execute(select(DatasetFreshness))).scalars().all()
    # Google-touch discovery runs only when both Google CRM properties are configured (AAP
    # 0.3.3 "When configured"), so without them hubspot.google_contacts has no row to check.
    google_contacts = "hubspot.google_contacts"
    skipped = set() if _google_crm_configured(settings) else {google_contacts}
    by_dataset: dict[int, dict[str, DatasetFreshness]] = {}
    for row in rows:
        by_dataset.setdefault(row.dataset_id, {})[row.dataset] = row
    problems: list[str] = []
    roles = {(dataset.source, dataset.role) for dataset in datasets}
    expected_roles = {(source, "primary") for source in SOURCES} | {("hubspot", "companion")}
    if roles != expected_roles:
        problems.append(f"active datasets are {sorted(roles)}, expected {sorted(expected_roles)}")
    for dataset in datasets:
        names = set(SOURCE_DATASETS[dataset.source]) - skipped
        if dataset.role == "companion":
            # The companion is matched and synced only from LinkedIn's leads; it never runs
            # the primary-only Google-touch discovery.
            names -= {google_contacts}
        stored = by_dataset.get(dataset.id, {})
        where = f"{dataset.source} {dataset.role} dataset {dataset.id}"
        if missing := names - stored.keys():
            problems.append(f"{where}: no freshness row for {sorted(missing)}")
        for name in sorted(names & stored.keys()):
            row = stored[name]
            if row.completeness != "complete" or row.last_success_at is None:
                problems.append(f"{where} {name}: {row.completeness}, reason {row.reason!r}")
            if name in FULL_STATE_DATASETS:
                # Full-state entity reads carry no native date range of their own.
                continue
            if row.covered_through is None or row.covered_through < window_end:
                problems.append(f"{where} {name}: covered through {row.covered_through}")
            if row.covered_from is None or row.covered_from > window_start:
                problems.append(f"{where} {name}: covered from {row.covered_from}")
    _report(problems, "every active dataset must be complete over the window")


def _item_keys(page: Json) -> list[tuple[Any, ...]]:
    """The ordered identity of every item: section, group, rank, rule, entity, action, reason."""
    return [
        (
            section,
            group,
            item["rank"],
            item["rule_id"],
            item["entity"]["source"],
            item["entity"]["type"],
            item["entity"]["id"],
            item["action"],
            item["reason"],
        )
        for section, group, item in _items(page)
    ]


# ---------------------------------------------------------------------------------------------
# Database and API reads
# ---------------------------------------------------------------------------------------------


async def _active_dataset_ids(db: Sessions) -> dict[str, frozenset[int]]:
    """The active dataset ids of each source, LinkedIn's companion CRM dataset under hubspot."""
    async with db() as session:
        result = await session.execute(
            select(SourceDataset.source, SourceDataset.id).where(SourceDataset.state == "active")
        )
        pairs = result.all()
    return {
        source: frozenset(dataset_id for owner, dataset_id in pairs if owner == source)
        for source in SOURCES
    }


async def _assert_sync_runs_succeeded(db: Sessions) -> None:
    """Every ``sync_run`` row, backfill and manual alike, finished ``succeeded``."""
    async with db() as session:
        runs = (await session.execute(select(SyncRun).order_by(SyncRun.id))).scalars().all()
    assert runs, "the seed recorded no sync_run row"
    failed = [
        f"{run.source}/{run.job} {run.dataset}: {run.status} ({run.error})"
        for run in runs
        if run.status != "succeeded"
    ]
    assert not failed, "sync runs did not succeed:\n  " + "\n  ".join(failed)


async def _get_page(client: httpx.AsyncClient, run_id: int | None = None) -> tuple[Json, float]:
    """``GET /api/v1/recommendations`` (one run by id, or the latest default run) and its time."""
    params = {} if run_id is None else {"run_id": run_id}
    started = perf_counter()
    response = await client.get("/api/v1/recommendations", params=params)
    elapsed = perf_counter() - started
    assert response.status_code == 200, (
        f"GET /recommendations: HTTP {response.status_code} {response.text}"
    )
    page: Json = response.json()
    return page, elapsed


@pytest.fixture
def simulated_env(set_env: SetEnv) -> None:
    """Force simulated data with no credentials (the conftest baseline removes them all).

    Applied through ``usefixtures``, which pytest sets up before a test's own arguments, so
    the application ``authed_client`` builds reads these settings (``DISPLAY_TIMEZONE``
    included) when it is created; the test body asserts that it did.
    """
    set_env(
        DATA_MODE="simulated",
        MOCK_SEED=MOCK_SEED,
        BACKFILL_DAYS=BACKFILL_DAYS,
        DISPLAY_TIMEZONE=DISPLAY_TIMEZONE,
    )
    settings = get_settings()
    configured = [source for source in SOURCES if settings.has_credentials(source)]
    assert not configured, f"credentials leaked into the test environment for {configured}"


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


def test_the_default_scenario_is_this_modules_world() -> None:
    """The catalogue's default world is run by this module and promises all five sections."""
    assert SCENARIO.module == MODULE_END_TO_END
    assert tuple(SCENARIO.expect.values["populated_sections"]) == SECTION_ORDER
    assert FLAG_SIMULATED in SCENARIO.expect.flags
    assert set(FIRST_START_RULES) == set(SECTION_ORDER)


@pytest.mark.usefixtures("simulated_env")
async def test_simulated_pipeline_produces_every_section_with_evidence(
    authed_client: httpx.AsyncClient,
    db: Sessions,
    subtests: pytest.Subtests,
) -> None:
    """Sync, analysis and API on simulated data produce every section with its evidence.

    Phase 1 seeds through the real pipeline: ``bootstrap_sources`` simulates every source with
    reason ``DATA_MODE=simulated`` (building, backfilling and activating each dataset and
    LinkedIn's companion), every source's manual sync succeeds, and the default-window run
    succeeds. Each AAP 0.11.2 guarantee is then a subtest on ``GET /recommendations``, plus
    dataset freshness, the 2 s page budget, a deterministic re-analysis and the 30 s custom run.
    """
    # The guarantees are subtests of this one seeded test, not one seeded test each: every
    # table is emptied before each test, and a simulated first start takes about two minutes
    # because every HubSpot call passes the client's 4 requests/s throttle, so each further
    # seeded test would repeat that cost without proving anything new.
    settings = get_settings()
    # One instant for the whole seed, so every build, sync and run reads the same native days.
    # It is the real instant, not a fixed past one: the API's overlay reads the real clock,
    # and a past seed would turn every dataset stale (older than sources.stale_after_hours).
    now = datetime.now(UTC)
    controller = SourceModeController(settings, clock=lambda: now)

    report = await bootstrap_sources(settings, controller=controller)
    assert not report.failures, f"bootstrap failed: {dict(report.failures)}"
    for source in SOURCES:
        resolved = report.modes[source]
        assert (resolved.mode, resolved.reason) == ("simulated", REASON_FORCED), (
            f"{source} resolved {resolved}"
        )
        active = report.active.get(source)
        assert active is not None, f"{source} has no active dataset after the bootstrap"
        assert (active.mode, active.reason) == ("simulated", REASON_FORCED), f"{source}: {active}"
    linkedin = report.active["linkedin"]
    assert linkedin is not None
    assert linkedin.companion_id is not None, "simulated LinkedIn has no companion CRM dataset"

    for source in SOURCES:
        synced = await run_source_jobs(source, trigger="manual", controller=controller, now=now)
        ran = [result.job for result in synced.results]
        assert synced.ok, f"{source} manual sync did not succeed: {synced.to_json()}"
        assert synced.mode == "simulated", f"{source} synced a {synced.mode} dataset"
        assert set(SOURCE_JOBS[source]) <= set(ran), f"{source} ran {ran}"
        if source == "linkedin":
            assert JOB_CRM_COMPANION in ran, f"LinkedIn's companion CRM was not synced: {ran}"
    await _assert_sync_runs_succeeded(db)

    window = await default_window(now=now)
    run = await run_analysis(window, "default", "scheduled", now=now)
    assert run.status == "succeeded", f"default run {run.id} is {run.status}"

    page, elapsed = await _get_page(authed_client)
    assert page["run"]["id"] == run.id, f"the page serves run {page['run']['id']}, not {run.id}"
    assert (page["run"]["kind"], page["run"]["trigger"]) == ("default", "scheduled")
    assert page["run"]["days"] == page["run"]["default_days"] == window.days
    assert page["run"]["short_window"] is False
    google_zone = _google_zone()
    active_ids = await _active_dataset_ids(db)

    with subtests.test(msg="five_sections_populated"):
        _check_five_sections_populated(page)
    with subtests.test(msg="every_item_carries_evidence_window_and_boundaries"):
        _check_evidence_window_and_boundaries(page, google_zone=google_zone, active_ids=active_ids)
    with subtests.test(msg="every_item_labelled_simulated"):
        _check_labelled_simulated(page)
    with subtests.test(msg="simulated_banner_and_modes"):
        _check_banner_and_modes(page, google_zone=google_zone, reason=REASON_FORCED)
    with subtests.test(msg="section_field_sets"):
        _check_section_field_sets(page)
    with subtests.test(msg="freshness_complete"):
        await _check_freshness_complete(
            db, settings=settings, window_start=window.start, window_end=window.end
        )
    with subtests.test(msg="recommendations_page_under_2s"):
        assert elapsed < PAGE_BUDGET_SECONDS, (
            f"GET /recommendations took {elapsed:.2f} s, budget {PAGE_BUDGET_SECONDS} s"
        )

    with subtests.test(msg="reanalysis_is_deterministic"):
        again = await run_analysis(await default_window(now=now), "default", "scheduled", now=now)
        assert again.status == "succeeded", f"second default run {again.id} is {again.status}"
        assert (again.config_hash, again.dataset_ids, again.data_modes) == (
            run.config_hash,
            run.dataset_ids,
            run.data_modes,
        )
        repeat, _ = await _get_page(authed_client, again.id)
        assert repeat["run"]["id"] == again.id
        assert _item_keys(repeat) == _item_keys(page), "a re-analysis changed the ranked items"
        evidence = [item["evidence"] for _, _, item in _items(page)]
        assert [item["evidence"] for _, _, item in _items(repeat)] == evidence, (
            "a re-analysis of the same data changed an item's evidence"
        )

    with subtests.test(msg="custom_run_under_30s"):
        end = date.fromisoformat(page["run"]["window_end"])
        start = end - timedelta(days=CUSTOM_DAYS - 1)
        started = perf_counter()
        response = await authed_client.post(
            "/api/v1/runs",
            json={"window_start": start.isoformat(), "window_end": end.isoformat()},
        )
        took = perf_counter() - started
        assert response.status_code == 201, (
            f"POST /runs: HTTP {response.status_code} {response.text}"
        )
        custom = response.json()
        assert (custom["kind"], custom["trigger"], custom["status"]) == (
            "custom",
            "manual",
            "succeeded",
        )
        assert (custom["window_start"], custom["window_end"], custom["days"]) == (
            start.isoformat(),
            end.isoformat(),
            CUSTOM_DAYS,
        )
        assert all(custom["data_modes"][source] == DATA_SIMULATED for source in SOURCES)
        assert took < CUSTOM_RUN_BUDGET_SECONDS, (
            f"a {CUSTOM_DAYS}-day custom run took {took:.1f} s, "
            f"budget {CUSTOM_RUN_BUDGET_SECONDS} s"
        )
        latest = await authed_client.get("/api/v1/runs/latest")
        assert latest.status_code == 200, latest.text
        assert latest.json()["kind"] == "default", "a custom run stood in for the default run"


@pytest.mark.usefixtures("simulated_env")
async def test_seed_simulated_cli(
    run_cli: RunCli,
    authed_client: httpx.AsyncClient,
    db: Sessions,
    subtests: pytest.Subtests,
) -> None:
    """``ads-engine seed-simulated`` on an empty database leaves a populated, labelled default run.

    The first-start command builds, backfills and activates a simulated dataset per source
    with no credentials, computes the default-window run and exits 0. The page that run serves
    has all five sections, each item labelled simulated with its evidence, window and source
    boundaries, and the banner names every simulated source with its reason.
    """
    # The CLI is a separate process with its own event loop; it runs in a worker thread so
    # this test's loop is not blocked while it seeds.
    completed = await anyio.to_thread.run_sync(
        partial(run_cli, "seed-simulated", timeout=CLI_TIMEOUT_SECONDS)
    )
    assert completed.returncode == 0, (
        f"seed-simulated exited {completed.returncode}\nstdout: {completed.stdout[-2000:]}\n"
        f"stderr: {completed.stderr[-4000:]}"
    )
    output = json.loads(completed.stdout)
    for source in SOURCES:
        entry = output["sources"][source]
        assert entry["failure"] is None, f"{source} failed to seed: {entry['failure']}"
        assert (entry["mode"], entry["reason"], entry["dataset_mode"]) == (
            "simulated",
            REASON_FORCED,
            "simulated",
        ), f"{source}: {entry}"
        assert entry["dataset_id"] is not None, f"{source} has no active dataset: {entry}"
    assert output["sources"]["linkedin"]["companion_id"] is not None
    summary = output["run"]
    assert summary is not None, "seed-simulated computed no default run"
    assert (summary["kind"], summary["status"]) == ("default", "succeeded"), summary

    async with db() as session:
        runs = (await session.execute(select(AnalysisRun))).scalars().all()
    assert [(row.id, row.kind, row.status) for row in runs] == [
        (summary["run_id"], "default", "succeeded")
    ]
    latest = await authed_client.get("/api/v1/runs/latest")
    assert latest.status_code == 200, latest.text
    assert latest.json()["id"] == summary["run_id"]

    page, _ = await _get_page(authed_client)
    assert page["run"]["id"] == summary["run_id"]
    google_zone = _google_zone()
    active_ids = await _active_dataset_ids(db)
    with subtests.test(msg="five_sections_populated"):
        _check_five_sections_populated(page)
    with subtests.test(msg="every_item_carries_evidence_window_and_boundaries"):
        _check_evidence_window_and_boundaries(page, google_zone=google_zone, active_ids=active_ids)
    with subtests.test(msg="every_item_labelled_simulated"):
        _check_labelled_simulated(page)
    with subtests.test(msg="simulated_banner_and_modes"):
        _check_banner_and_modes(page, google_zone=google_zone, reason=REASON_FORCED)
