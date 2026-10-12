/**
 * End-to-end checks of the `/actions` screen (AAP 0.5.5, 0.11.2), run against one shared stack in
 * simulated mode (`DATA_MODE=simulated`, no source credentials).
 *
 * What the spec proves, test by test:
 * 1. The five sections render in the user's fixed order (Keywords, Campaigns, Budget,
 *    Copy/creative, White papers), titled as the API titles them, and each "Sections" navigation
 *    link counts the section's items that feedback has not hidden.
 * 2. Every item of every section renders its frozen evidence (AAP 0.5.4.8, 0.5.5, 0.11.2). With
 *    each keyword tab selected and every group, held disclosure and "Show … more" batch opened,
 *    each group shows exactly the API's unhidden items, and each card shows the values
 *    `src/lib/format.ts` renders from the API: every metric with its unit, currency, floor or
 *    "unavailable", its paired baselines, the other baselines, the sample and test lines, the
 *    thresholds and datasets, the window chip of its frozen window (dates, day count and each
 *    source's day boundary) and one badge per flag, "Simulated data" included. Every
 *    white-paper row of every currency cohort shows its cells, and its expanded details their
 *    per-creative rows, evidence, chip and flags. Rules that bypass the shared formatter check
 *    the floor markers, "unavailable", currency codes and the sample's observed / required,
 *    and a routed copy of the page (`page.route`; `page.request` reads are never routed) adds
 *    floors, unavailable counts, zero-denominator costs and an "unknown creative" row.
 * 3. The window picker offers exactly the configured presets with the configured default marked;
 *    a non-default preset opens a custom run at `/actions?run=<id>` with its banner, and "Back to
 *    default" returns to the default run.
 * 4. Snoozing a card hides it, and its section reports the hidden or suppressed count. The target
 *    is chosen deterministically (a keyword item first, in API order), and a run that offers no
 *    target fails rather than passing without a snooze. The UI's own request body must be
 *    `{"kind": "snooze"}` alone.
 * 5. Discrepancy (C0) cards carry their resolver and never a feedback action (AAP 0.5.4.2,
 *    0.5.4.4): with every batch revealed, the discrepancy group shows every C0 card of the run.
 *    A routed copy of the page whose discrepancy group holds more C0 cards than one batch of
 *    `CARD_BATCH_SIZE`, each with its open discrepancy in the routed `GET /discrepancies`, shows
 *    one batch first, then, revealed, every card with exactly its discrepancy's resolutions.
 *
 * How it stays reliable on a shared, mutating stack:
 * - Every expectation is derived from the API (`page.request`, which carries the signed-in
 *   session of `auth.setup.ts`), never from fixed names, ids or counts.
 * - The worker reruns the default analysis after every sync, decision and feedback write, so
 *   item ids and counts change under the spec. Every UI-versus-API comparison therefore runs in
 *   an `expect(...).toPass()` block that reloads the page and re-reads the API.
 * - Specs never run concurrently (`workers: 1`), but this spec relies on no order among them:
 *   any test may start before the worker's first default run exists, so every test waits for a
 *   succeeded default run itself (`openActions`).
 *
 * The spec never signs in (the API allows 5 sign-ins per minute per IP; `auth.setup.ts` signs in
 * once) and makes its product writes only through the UI, which adds the CSRF header. It writes
 * one custom run and one snooze, of an item without a decision subject in a section that keeps
 * at least one item, so the other specs, in whatever order they run, still find every section
 * populated and their Record-decision candidates untouched; the snooze's own teardown, below, is
 * the one write that bypasses the UI.
 *
 * The API cannot withdraw feedback, so the snooze test undoes its own snooze through the stack's
 * database. Before it snoozes, it proves that the database command reaches this stack. Each
 * attempt routes its snooze request (`page.route`) and forwards the UI's body with a `note` of
 * its own, `e2e-actions-snooze:` and a random UUID, so the stored row names its owner whether or
 * not the response arrives. The `afterEach` teardown runs on every attempt, passed or failed. It
 * deletes only the row carrying that attempt's note (and its id, once a response named it), never
 * another operator's snooze of the same item, and queues one analysis request, so the next
 * default run is computed without the suppression, then waits until that run shows the item
 * again. The database command is
 * `E2E_PSQL` when set: any command that runs `psql` on the stack's database and reads SQL on
 * stdin. Otherwise it is `psql` in the Compose `db` service through `E2E_COMPOSE` (default
 * `docker compose`; add the stack's `-p`/`-f` flags there), run from the repository root.
 *
 * @example
 * ```sh
 * cd web && E2E_BASE_URL=http://localhost:8080 E2E_ADMIN_PASSWORD=pw npx playwright test actions
 * ```
 *
 * @packageDocumentation
 */
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';
import type { APIRequestContext, APIResponse, Locator, Page, Route } from '@playwright/test';

import type {
  Baseline,
  ConfigView,
  DataStatus,
  DatasetCoverage,
  Discrepancy,
  DiscrepancyKind,
  DiscrepancyValue,
  EntityView,
  Evidence,
  EvidenceWindow,
  FeedbackKind,
  Flag,
  FlagCode,
  Group,
  Item,
  Metric,
  MetricValue,
  RecommendationsPage,
  Resolution,
  Run,
  Section,
  SectionKey,
  SourceName,
} from '../../src/api/types';
// Both modules import only types and each other, never React or CSS, so this Node-side spec
// derives every expected text with the very functions the cards render it with.
import {
  WHITE_PAPER_METRIC_KEYS,
  cohortSummary,
  creativeRows,
  discrepancyRef,
  evidenceCurrency,
  isRecord,
  whitePaperCells,
  whitePaperCostNotes,
  whitePaperMqlLabel,
  whitePaperMqlMetricLabel,
} from '../../src/features/actions/evidenceExtra';
import type { WhitePaperMetricKey } from '../../src/features/actions/evidenceExtra';
import {
  EM_DASH,
  UNAVAILABLE,
  formatDecimal,
  formatMetric,
  formatSample,
  formatSourceDate,
  formatTestLine,
  formatWindowChip,
  isDecimalString,
  rateMetricDigits,
  ratioMetricDigits,
} from '../../src/lib/format';
import type { MetricFormatOptions, MetricInput } from '../../src/lib/format';

// ---------------------------------------------------------------------------------------------
// API paths and timing
// ---------------------------------------------------------------------------------------------

const LATEST_RUN_PATH = '/api/v1/runs/latest';
const RECOMMENDATIONS_PATH = '/api/v1/recommendations';
const CONFIG_PATH = '/api/v1/config';
const ACTIONS_PATH = '/actions';

/** Whole-test budget: the first test may wait minutes for the worker's first default run. */
const TEST_TIMEOUT_MS = 10 * 60_000;

/**
 * How long the first default run may take: on first start the worker backfills 180 days for
 * three simulated sources and the companion CRM dataset before it analyses.
 */
const FIRST_RUN_TIMEOUT_MS = 9 * 60_000;
const FIRST_RUN_INTERVALS_MS = [5_000, 10_000, 15_000];

/** Bound on one API read, so a hung connection never stalls a poll. */
const API_READ_TIMEOUT_MS = 30_000;

/** Bound on one locator read inside a retried block, so a retry starts promptly. */
const READ_TIMEOUT_MS = 10_000;

/** Retry budget of a UI-versus-API comparison while reruns replace the run under the spec. */
const SETTLE = { intervals: [2_000, 5_000, 10_000], timeout: 120_000 };

/** Retry budget of the custom-run request while another process holds the analysis lock. */
const CUSTOM_RUN_RETRY = { intervals: [5_000, 10_000, 20_000], timeout: 180_000 };

/** `POST /runs` computes synchronously; a typical account computes in under 30 s. */
const CUSTOM_RUN_OPEN_TIMEOUT_MS = 45_000;

/** How long a snoozed card may stay on screen while the recommendations refetch. */
const CARD_REMOVAL_TIMEOUT_MS = 30_000;

/** Retry budget of the suppression report, which may wait for the coalesced (60 s) rerun. */
const SUPPRESSION_RETRY = { intervals: [5_000, 10_000, 15_000], timeout: 150_000 };

/**
 * Retry budget of the snooze restoration: the restoring analysis request waits up to 60 s to be
 * coalesced, plus one 30 s scheduler tick and the run itself.
 */
const RESTORE_RETRY = { intervals: [5_000, 10_000, 15_000], timeout: 180_000 };

/** Bound on one database command, so a hung `docker compose exec` never stalls a test or hook. */
const DATABASE_COMMAND_TIMEOUT_MS = 60_000;

// ---------------------------------------------------------------------------------------------
// UI vocabulary (each value is the label the written UI renders)
// ---------------------------------------------------------------------------------------------

/** The five sections in the user's order (AAP 0.5.5); the assertion target, not a copy. */
const SECTION_KEYS: readonly SectionKey[] = [
  'keywords',
  'campaigns',
  'budget',
  'creative',
  'white_papers',
];

/** The sections whose items render as recommendation cards (`<article id="item-{id}">`). */
const CARD_SECTION_KEYS: readonly SectionKey[] = ['keywords', 'campaigns', 'budget', 'creative'];

const PAGE_HEADING = 'Actions';
const SECTION_NAV_LABEL = 'Sections';
const EVIDENCE_CAPTION = 'Evidence';
const FLAGS_LABEL = 'Flags';
const NOT_SHOWN_LABEL = 'Not shown';
const KEYWORD_TABS_LABEL = 'Keyword actions';
const HELD_DISCLOSURE = /^Held until discrepancies are resolved/u;
const WHITE_PAPER_TABLE = /^White papers \(/u;
const WINDOW_PICKER_LABEL = 'Analysis window';
const CUSTOM_RANGE_LABEL = 'Custom range';
const PRESET_BUTTON = /^(\d+) days?( \(default\))?$/u;
const RUN_IN_PROGRESS_MESSAGE = 'Another analysis is running. Try again shortly.';
const CUSTOM_BANNER_SELECTOR = '#banner-custom-window';
const BACK_TO_DEFAULT_LABEL = 'Back to default';
const CUSTOM_RUN_URL = /\/actions\?run=\d+/u;
const DISCREPANCY_GROUP_SELECTOR = '#group-campaigns-discrepancies';
const DISCREPANCY_RULE_ID = 'C0';
const RESOLVED_SINCE_RUN = 'Resolved since this run';

/**
 * The C0 resolver's button label of each resolution key (AAP 0.5.4.4), as `RESOLUTION_LABELS`
 * in `src/features/decisions/DiscrepancyResolver.tsx` writes them; that module renders React,
 * so this Node-side spec cannot import it.
 */
const RESOLUTION_BUTTONS: Readonly<Record<Resolution, string>> = {
  platform_correct: 'Platform is correct',
  decision_correct: 'Decision is correct',
  adopt_platform: 'Adopt platform state',
  record_intended: 'Record intended state',
  acknowledge_removal: 'Acknowledge removal',
};

/** Every resolver button label. */
const RESOLVER_LABELS: ReadonlySet<string> = new Set(Object.values(RESOLUTION_BUTTONS));

/**
 * Cards mounted per batch of a card list: `CARD_BATCH_SIZE` in `src/features/actions/batches.tsx`,
 * a React module this Node-side spec cannot import. The discrepancy scenario crosses it.
 */
const CARD_BATCH_SIZE = 20;

/** A batched list's reveal button: "Show 20 more actions", "Show 1 more creative". */
const SHOW_MORE_BUTTON = /^Show \d[\d,]* more \S/u;

/**
 * A white-paper row's expand toggle, named "Show details for {asset} ({scope})" while collapsed
 * and "Hide details for …" while expanded (`DataTable.tsx`, `rowLabelOf` in
 * `WhitePapersSection.tsx`).
 */
const DETAILS_TOGGLE = /^(?:Show|Hide) details for /u;

/** The caption suffix, and toggle scope qualifier, of a cohort's replaced-data table. */
const REPLACED_TABLE_QUALIFIER = 'computed from replaced data';

/** The caption suffix, and toggle scope qualifier, of a cohort's held table. */
const HELD_TABLE_QUALIFIER = 'held until discrepancies are resolved';

/** The most presses one reveal makes, so controls that never settle fail the step. */
const REVEAL_LIMIT = 500;

/** The Campaigns discrepancy group's key (`analysis.context.GROUP_DISCREPANCIES`). */
const DISCREPANCY_GROUP_KEY = 'discrepancies';

/** The open discrepancies the C0 resolvers read (`GET /discrepancies`). */
const DISCREPANCIES_PATH = '/api/v1/discrepancies';

// The parts of an evidence block, as `src/features/actions/EvidenceTable.tsx` labels them.
const EVIDENCE_FOR_PREFIX = 'Evidence for ';
const NO_METRICS_MESSAGE = 'No metrics recorded';
const OTHER_BASELINES_LABEL = 'Other baselines';
const BASELINES_LABEL = 'Baselines';
const THRESHOLDS_LABEL = 'Thresholds applied';
const DATASETS_LABEL = 'Data used';
const UNNAMED_TEST = 'Statistical test';
const EMPTY_THRESHOLD_LIST = 'none';

/** Fraction digits of an unpaired baseline with no unit or currency (`RECORDED_DIGITS`). */
const RECORDED_DIGITS = 6;

/** Fraction digits of a numeric threshold (`THRESHOLD_DIGITS`). */
const THRESHOLD_DIGITS = 20;

/**
 * Where a window chip's text starts: "{start} – {end} · ", an en dash between the dates and a
 * middle dot after them (`formatWindowChip`). Given as a source because it is used in the page.
 */
const WINDOW_CHIP_PATTERN = '^\\S+ \u2013 \\S+ \u00b7 ';

// The white-paper tables, as `src/features/actions/sections/WhitePapersSection.tsx` labels them.
const PER_CREATIVE_PREFIX = 'Per creative: ';
/** The name of an asset row's notes list in its details, before the row's label. */
const ROW_NOTES_PREFIX = 'Notes on ';
const NO_CREATIVE_ROWS = 'No per-creative rows recorded for this white paper';
const UNKNOWN_CREATIVE_LABEL = 'unknown creative';
const REPLACED_DATA_LABEL = 'Computed from replaced data';
/** A held row's hold reason when it records none (`HELD_REASON_FALLBACK`). */
const HELD_REASON_FALLBACK = 'Resolve discrepancy first';
/**
 * The text of a held row's links to the discrepancies that hold it, as
 * `src/features/actions/BlockingDiscrepancies.tsx` (a React module this spec cannot import)
 * labels them: "View open discrepancies" when no blocker can be named, otherwise one or more
 * "View blocking discrepancy: {subject} ({kind})" or "View blocking discrepancy {id} ({kind})".
 * Which blockers are named depends on the run's C0 cards and the live discrepancy list the
 * browser read, so the spec checks the labels' form, not each subject.
 */
const BLOCKER_LINKS_PATTERN = '(?:View open discrepancies|View blocking discrepancy[: ].+)';
const MQL_PLAIN = 'MQL';
const TOGGLE_HEADER = 'Details';

/** The asset table's column headers around its metric columns. */
const ASSET_HEADER = 'Asset';
const CREATIVE_HEADER = 'Creative';
const ACTION_HEADER = 'Action';

/** The header of every white-paper metric column but MQL, whose header carries its stage label. */
const WHITE_PAPER_HEADERS: Readonly<Record<Exclude<WhitePaperMetricKey, 'mql'>, string>> = {
  spend: 'Spend',
  linkedin_leads: 'LinkedIn leads',
  ql: 'Qualified Lead',
  meetings_booked: 'Meetings booked',
  other_meetings: 'Other meetings (ordering unverified)',
  deals_after_touch: 'Deals after touch',
  cost_per_ql: 'Cost per Qualified Lead',
  cost_per_meeting: 'Cost per meeting',
};

/** The field of an `extra.rows` entry behind each white-paper count or cost column. */
const CREDIT_ROW_FIELDS: Readonly<Partial<Record<WhitePaperMetricKey, string>>> = {
  mql: 'mql',
  ql: 'ql',
  meetings_booked: 'meetings_booked',
  other_meetings: 'other_meetings',
  deals_after_touch: 'deals',
  cost_per_ql: 'cost_per_ql',
  cost_per_meeting: 'cost_per_meeting',
};

/** The white-paper columns that are costs, whose "—" carries its reason. */
const WHITE_PAPER_COSTS: ReadonlySet<WhitePaperMetricKey> = new Set([
  'cost_per_ql',
  'cost_per_meeting',
]);

/** Metric units that make a value an amount of money, whose floor is a ceiling ("≤ X"). */
const AMOUNT_UNITS: ReadonlySet<string> = new Set(['money', 'currency', 'amount', 'cost', 'spend']);

/** Footer button names of a recommendation card, per feedback kind. */
const FEEDBACK_BUTTONS: Readonly<Record<FeedbackKind, string>> = {
  done: 'Mark done',
  dismiss: 'Dismiss',
  snooze: 'Snooze for 7 days',
};

/** The feedback the spec gives: a 7-day snooze, the shortest suppression (AAP 0.5.2). */
const FEEDBACK_KIND: FeedbackKind = 'snooze';

/** The primary action of an item with a decision subject. */
const RECORD_DECISION_LABEL = 'Record decision';

/** How a section's "Not shown" list names the stored count of each feedback kind. */
const SUPPRESSION_LABELS: Readonly<Record<FeedbackKind, string>> = {
  done: 'marked done',
  dismiss: 'dismissed',
  snooze: 'snoozed',
};

/** Display names of the sources, as the day-boundary labels show them. */
const SOURCE_LABELS: Readonly<Record<SourceName, string>> = {
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
};

const COUNT_FORMAT = new Intl.NumberFormat('en');

/**
 * The no-break space (U+00A0) the UI joins a currency code and its figure with, so a wrapped
 * line never ends with a bare code.
 */
const NO_BREAK_SPACE = '\u00a0';

// ---------------------------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------------------------

/** One JSON read: the HTTP status and, on a 2xx, the parsed body. */
interface JsonResult<T> {
  status: number;
  body: T | null;
}

/** GETs `path` with the page's session and parses the body when the status is 2xx. */
async function getJson<T>(request: APIRequestContext, path: string): Promise<JsonResult<T>> {
  const response = await request.get(path, { timeout: API_READ_TIMEOUT_MS });
  if (!response.ok()) {
    return { status: response.status(), body: null };
  }
  return { status: response.status(), body: (await response.json()) as T };
}

/** GETs `path` and fails the step unless it answers 200 with a body. */
async function requireJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const { status, body } = await getJson<T>(request, path);
  expect(status, `GET ${path} answers 200`).toBe(200);
  if (body === null) {
    throw new Error(`GET ${path} answered ${status} without a body`);
  }
  return body;
}

/** The status of `GET /runs/latest`, or 0 while the stack does not answer. */
async function latestRunStatus(request: APIRequestContext): Promise<number> {
  try {
    return (await getJson<Run>(request, LATEST_RUN_PATH)).status;
  } catch {
    return 0;
  }
}

/**
 * Waits until the worker's first default run has succeeded (`/runs/latest` answers 404
 * `not_found` until then) and returns the latest default run.
 */
async function waitForDefaultRun(page: Page): Promise<Run> {
  await expect
    .poll(() => latestRunStatus(page.request), {
      message: `the worker has finished a default analysis run (${LATEST_RUN_PATH} answers 200)`,
      timeout: FIRST_RUN_TIMEOUT_MS,
      intervals: FIRST_RUN_INTERVALS_MS,
    })
    .toBe(200);
  return requireJson<Run>(page.request, LATEST_RUN_PATH);
}

/** The latest default run's five sections, with the current-state overlay. */
function fetchRecommendations(page: Page): Promise<RecommendationsPage> {
  return requireJson<RecommendationsPage>(page.request, RECOMMENDATIONS_PATH);
}

/** The section of `key`; fails when the API omitted it. */
function sectionOf(recommendations: RecommendationsPage, key: SectionKey): Section {
  const section = recommendations.sections.find((candidate) => candidate.key === key);
  if (section === undefined) {
    throw new Error(`The API page of run ${recommendations.run.id} has no "${key}" section`);
  }
  return section;
}

/** Every item of a section, in group order. */
function allItems(section: Section): Item[] {
  return section.groups.flatMap((group) => group.items);
}

/** Whether feedback given since the run hides the item (`current.hidden_by`). */
function isHidden(item: Item): boolean {
  return item.current.hidden_by !== null && item.current.hidden_by !== undefined;
}

/**
 * Whether any hold, recorded or read now, applies to the item: `held`, set when the run computed
 * it, or `current.held_now`, which the overlay sets for that frozen hold (it never lifts one), for
 * a discrepancy now unresolved on a subject a non-C0 item depends on, and for an item whose
 * provenance is replaced or unverifiable (`current.historical`). Unlike `isHeld` in
 * `SectionFrame.tsx`, it counts historical items as held; callers that need the held
 * disclosure's items exclude historical ones themselves.
 */
function isHeld(item: Item): boolean {
  return item.held || item.current.held_now;
}

/**
 * The items a section shows, held ones included: what its navigation link counts
 * (`visibleItemCount` in `SectionNav.tsx`).
 */
function visibleItems(section: Section): Item[] {
  return allItems(section).filter((item) => !isHidden(item));
}

// ---------------------------------------------------------------------------------------------
// Expected text, derived from API values the way `src/lib/format.ts` renders them
// ---------------------------------------------------------------------------------------------

/** A day count as `formatDays` renders it: `"1 day"`, `"28 days"`. */
function dayCount(days: number): string {
  return `${COUNT_FORMAT.format(days)} ${days === 1 ? 'day' : 'days'}`;
}

/** A preset button's day label as `DateRangePicker` renders it (no grouping separator). */
function presetLabel(days: number): string {
  return `${days} ${days === 1 ? 'day' : 'days'}`;
}

/** The display name of a source, or the raw value for a source this spec does not know. */
function sourceLabel(source: SourceName): string {
  return Object.hasOwn(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : String(source);
}

/**
 * The fragments an item's window chip must contain, built here without `formatWindowChip`:
 * `"{start} – {end} · {N} days"` of its frozen evidence window, then `"{Label} days: {boundary}"`
 * for each source, once per source (AAP 0.5.5 window chip). The chip may append a source's own
 * range to its boundary entry, so each entry is a fragment rather than the chip one string.
 */
function chipFragments(window: EvidenceWindow): string[] {
  const fragments = [`${window.start} – ${window.end} · ${dayCount(window.days)}`];
  const seen = new Set<SourceName>();
  for (const day of window.source_days) {
    if (!seen.has(day.source)) {
      seen.add(day.source);
      fragments.push(`${sourceLabel(day.source)} days: ${day.boundary}`);
    }
  }
  return fragments;
}

/**
 * The flag codes a card reads from `current.flags_now` alone (`LIVE_ONLY_FLAG_CODES` in
 * `FlagList.tsx`, restated because that module imports the design system and its CSS, which this
 * Node-side spec cannot load): the open-discrepancy count is read from the overlay.
 */
const LIVE_ONLY_FLAG_CODES: ReadonlySet<FlagCode> = new Set<FlagCode>([
  'unverified_discrepancies',
]);

/**
 * The flag badges an item shows (`mergeFlags` in `FlagList.tsx`): every code once, recorded
 * codes first, each labelled with its current message, else its recorded one, else its code. A
 * live-only code ({@link LIVE_ONLY_FLAG_CODES}) shows only while `item.current.flags_now`
 * carries it, at its recorded place if recorded, labelled with its current message, else its
 * code, never its recorded one.
 */
function shownFlags(item: Item): Flag[] {
  const order: FlagCode[] = [];
  const recorded = new Map<FlagCode, string>();
  const current = new Map<FlagCode, string>();
  const collect = (flags: readonly Flag[], messages: Map<FlagCode, string>): void => {
    for (const flag of flags) {
      if (!order.includes(flag.code)) {
        order.push(flag.code);
      }
      if (!messages.has(flag.code) && flag.message.trim() !== '') {
        messages.set(flag.code, flag.message);
      }
    }
  };
  collect(item.flags, recorded);
  collect(item.current.flags_now, current);
  const carriedNow = new Set(item.current.flags_now.map((flag) => flag.code));
  return order.flatMap((code): Flag[] => {
    if (LIVE_ONLY_FLAG_CODES.has(code)) {
      return carriedNow.has(code) ? [{ code, message: current.get(code) ?? code }] : [];
    }
    return [{ code, message: current.get(code) ?? recorded.get(code) ?? code }];
  });
}

/**
 * The label each flag code's badge names (`FLAG_LABELS` in `FlagList.tsx`, restated for the same
 * reason as {@link LIVE_ONLY_FLAG_CODES}): the wording AAP 0.5.5 gives the flags row, with one
 * "Floor" label for the attribution, creative and deal floors.
 */
const FLAG_LABELS: Readonly<Record<FlagCode, string>> = Object.freeze({
  simulated: 'Simulated data',
  stale: 'Stale',
  incomplete: 'Incomplete',
  short_window: 'Short window',
  unverified_discrepancies: 'Unverified while discrepancies open',
  attribution_floor: 'Floor',
  creative_floor: 'Floor',
  deal_floor: 'Floor',
  unknown_crm_evidence: 'Unknown CRM evidence',
  meeting_time_proxy: 'Meeting time proxy',
  pipeline_unavailable: 'Pipeline unavailable',
  no_crm_link: 'No CRM link',
  not_distinguishable: 'Not distinguishable',
  search_term_coverage: 'Low search-term coverage',
  shared_budget: 'Shared budget',
});

/** The lowercased words of `text`: each maximal run of letters and digits, in order. */
function flagWords(text: string): string[] {
  return text.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
}

/**
 * `word` with a simple English plural ending reduced (`singular` in `FlagList.tsx`, restated for
 * the same reason as {@link LIVE_ONLY_FLAG_CODES}): a final "ies" becomes "y" and any other
 * final "s" is dropped, except in a word of three characters or fewer or one ending in "ss".
 */
function flagSingular(word: string): string {
  if (word.length <= 3 || word.endsWith('ss')) {
    return word;
  }
  if (word.endsWith('ies')) {
    return `${word.slice(0, -3)}y`;
  }
  return word.endsWith('s') ? word.slice(0, -1) : word;
}

/**
 * A flag badge's visible text (`flagText` in `FlagList.tsx`, restated for the same reason as
 * {@link LIVE_ONLY_FLAG_CODES}): the trimmed message as written when its words contain the
 * code's label ({@link FLAG_LABELS}) in order, compared with plural endings reduced on both
 * sides ({@link flagSingular}), else "{label}: {message}"; the label alone for a blank message
 * or the code itself; and, for a code with no label, the message, else the code. So "Unverified
 * while 1 discrepancy is open" is shown as written, as "Unverified while 3 discrepancies are
 * open" is.
 *
 * @param flag - One flag, as {@link shownFlags} merges it.
 * @returns The badge text, for example "Short window: Window of 10 days is below the 14-day
 *   trusted minimum".
 */
function flagText(flag: Flag): string {
  const message = flag.message.trim();
  const blank = message === '' || message === flag.code;
  if (!Object.hasOwn(FLAG_LABELS, flag.code)) {
    return blank ? flag.code : message;
  }
  const label = FLAG_LABELS[flag.code];
  if (blank) {
    return label;
  }
  const wanted = flagWords(label).map(flagSingular);
  let matched = 0;
  for (const word of flagWords(message)) {
    if (matched < wanted.length && flagSingular(word) === wanted[matched]) {
      matched += 1;
    }
  }
  return matched === wanted.length ? message : `${label}: ${message}`;
}

/** The name a white-paper row shows (`assetName` in `WhitePapersSection.tsx`). */
function assetName(item: Item): string {
  const name = item.entity.name?.trim() ?? '';
  if (name !== '') {
    return name;
  }
  const id = item.entity.id?.trim() ?? '';
  return id !== '' ? id : `White paper ${item.id}`;
}

/** A group's tab or heading title (the key when the title is blank). */
function groupTitle(title: string, key: string): string {
  const trimmed = title.trim();
  return trimmed === '' ? key : trimmed;
}

/** `text` escaped for use inside a regular expression. */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
}

/**
 * An attribute selector matching `value` exactly, for the generated ids, anchors and hrefs this
 * spec passes. `JSON.stringify` quotes `value` as a CSS string, which is exact only while it holds
 * no control character (U+0000–U+001F) and no lone surrogate: JSON escapes those in forms CSS
 * reads differently.
 */
function attributeSelector(attribute: string, value: string): string {
  return `[${attribute}=${JSON.stringify(value)}]`;
}

/** The item id of a card's `item-{id}` element id; fails for anything else. */
function cardItemId(elementId: string | null): number {
  const match = /^item-(\d+)$/u.exec(elementId ?? '');
  const digits = match?.[1];
  if (digits === undefined) {
    throw new Error(`"${String(elementId)}" is not a recommendation card id`);
  }
  return Number.parseInt(digits, 10);
}

// ---------------------------------------------------------------------------------------------
// Page helpers
// ---------------------------------------------------------------------------------------------

/** The page's level-1 heading, rendered in every state of `/actions`, loading included. */
function actionsHeading(page: Page): Locator {
  return page.getByRole('heading', { level: 1, name: PAGE_HEADING, exact: true });
}

/** The "Sections" navigation, rendered only once a run's sections have loaded. */
function sectionNav(page: Page): Locator {
  return page.getByRole('navigation', { name: SECTION_NAV_LABEL, exact: true });
}

/** A section's `<section id="section-{key}">` region. */
function sectionRegion(page: Page, key: SectionKey): Locator {
  return page.locator(`#section-${key}`);
}

/**
 * Waits for the worker's first default run, opens `/actions` and waits for its heading: the
 * opening of every test.
 */
async function openActions(page: Page): Promise<Run> {
  const run = await waitForDefaultRun(page);
  await page.goto(ACTIONS_PATH);
  await expect(actionsHeading(page)).toBeVisible();
  return run;
}

/** Waits until the page shows a run's content, not its loading state. */
async function expectSectionsLoaded(page: Page): Promise<void> {
  await expect(actionsHeading(page)).toBeVisible();
  await expect(sectionNav(page)).toBeVisible();
}

/** Reloads the page and waits for its content, so it reads the newest run and overlay. */
async function reloadActions(page: Page): Promise<void> {
  await page.reload();
  await expectSectionsLoaded(page);
}

/**
 * The item count a section's navigation link shows: the trailing number of the link
 * `a[href="#section-{key}"]` ("Keywords to adjust 12").
 */
async function sectionNavCount(page: Page, key: SectionKey): Promise<number> {
  const link = sectionNav(page).locator(attributeSelector('href', `#section-${key}`));
  const text = ((await link.textContent({ timeout: READ_TIMEOUT_MS })) ?? '').trim();
  const digits = /(\d[\d,]*)$/u.exec(text)?.[1];
  if (digits === undefined) {
    throw new Error(`The "${key}" section link "${text}" shows no item count`);
  }
  return Number.parseInt(digits.replaceAll(',', ''), 10);
}

/**
 * Mounts everything `container` can show (`batches.tsx`, `SectionFrame.tsx`): opens each
 * collapsed "Held until discrepancies are resolved" disclosure, presses each "Show {n} more …"
 * button until none is left and, with `expandRows`, expands each white-paper row. Controls are
 * re-read after every press, because a press reveals new ones.
 *
 * @param container - the section, group or tab panel to open
 * @param options - `expandRows`: also expand every white-paper row from its "Show details" toggle
 * @returns how many presses it took
 */
async function revealAll(
  container: Locator,
  options: { expandRows?: boolean } = {},
): Promise<number> {
  let pending = container
    .getByRole('button', { name: HELD_DISCLOSURE, expanded: false })
    .or(container.getByRole('button', { name: SHOW_MORE_BUTTON }));
  if (options.expandRows === true) {
    pending = pending.or(container.getByRole('button', { name: DETAILS_TOGGLE, expanded: false }));
  }
  const shown = pending.filter({ visible: true });
  for (let presses = 0; presses < REVEAL_LIMIT; presses += 1) {
    if ((await shown.count()) === 0) {
      return presses;
    }
    await shown.first().click({ timeout: READ_TIMEOUT_MS });
  }
  throw new Error(`Disclosures and batches were still collapsed after ${REVEAL_LIMIT} presses`);
}

// ---------------------------------------------------------------------------------------------
// What the page renders, read in bulk inside the browser
// ---------------------------------------------------------------------------------------------

/** One metric row of an evidence table. */
interface EvidenceRow {
  /** The row header: the metric's label. */
  header: string;
  /** The Value cell. */
  value: string;
  /** Each paired baseline as "{label}: {value}", or the cell's own text ("—") when none pairs. */
  baselines: string[];
}

/** One labelled list below an evidence table: its visible heading, its name and its entries. */
interface EvidenceList {
  heading: string;
  label: string;
  entries: string[];
}

/** One evidence block (`EvidenceTable.tsx`): the captioned table and what follows it. */
interface EvidenceBlock {
  caption: string;
  rows: EvidenceRow[];
  /** The single cell of a table that cites no metric; `null` when it has metric rows. */
  empty: string | null;
  /** The unpaired baselines, the thresholds applied and the datasets used, in order. */
  lists: EvidenceList[];
  /** The sample and test lines, in order. */
  lines: string[];
}

/** What one item's card or white-paper detail shows of its evidence. */
interface RenderedEvidence {
  /** Every evidence block inside it (one is expected). */
  evidence: EvidenceBlock[];
  /** The text of every window chip inside it (one is expected). */
  chips: string[];
  /** The badge texts of every "Flags" list inside it (one is expected). */
  flags: string[][];
}

/** A recommendation card as rendered: its `item-{id}` element id and its evidence. */
interface RenderedCard extends RenderedEvidence {
  elementId: string;
}

/** A labelled list inside a white-paper row's detail: its name and its entries. */
interface RenderedList {
  label: string;
  entries: string[];
}

/** A white-paper row's expanded detail: its per-creative table, its evidence and its notes. */
interface RenderedDetail extends RenderedEvidence {
  creatives: RenderedTable[];
  /** Every list inside it whose name starts "Notes on " (one at most is expected). */
  notes: RenderedList[];
}

/** One body row of a rendered `DataTable`. */
interface RenderedRow {
  /** Every cell but the expand toggle's, row header first. */
  cells: string[];
  /** The row's expand toggle; `null` in a table that does not expand. */
  toggle: { label: string; expanded: string | null } | null;
  /** The detail the toggle controls, when it is expanded; `null` otherwise. */
  detail: RenderedDetail | null;
}

/** A rendered `DataTable`: caption, column headers and body rows. */
interface RenderedTable {
  caption: string;
  headers: string[];
  rows: RenderedRow[];
}

/** One element {@link snapshotRendered} read: a card, or a table. */
interface RenderedRoot {
  card: RenderedCard | null;
  table: RenderedTable | null;
}

/** What {@link snapshotRendered} finds the parts by; passed in, because it runs in the page. */
interface SnapshotLabels {
  evidenceCaption: string;
  evidenceCaptionPrefix: string;
  perCreativePrefix: string;
  rowNotesPrefix: string;
  flagsLabel: string;
  chipPattern: string;
}

const SNAPSHOT_LABELS: SnapshotLabels = {
  evidenceCaption: EVIDENCE_CAPTION,
  evidenceCaptionPrefix: EVIDENCE_FOR_PREFIX,
  perCreativePrefix: PER_CREATIVE_PREFIX,
  rowNotesPrefix: ROW_NOTES_PREFIX,
  flagsLabel: FLAGS_LABEL,
  chipPattern: WINDOW_CHIP_PATTERN,
};

/**
 * Reads, inside the page, what each element shows, so a section of any size is compared in
 * one round trip: a card's evidence blocks, window chips and flag lists, or a table's caption,
 * headers and body rows, each with its expanded white-paper detail (per-creative table,
 * evidence, chip, flags and "Notes on {asset} ({scope})" list). Texts are `textContent`,
 * trimmed, without the text of any `role="tooltip"` bubble: a metric label's `Tooltip` keeps its
 * help text in the DOM while closed, and the row header shows, and is named by, the label alone.
 * It runs through `evaluateAll`, so it reads only its arguments and the DOM.
 *
 * @param roots - the `article` cards or `table` elements to read
 * @param labels - the UI labels the parts are found by
 * @returns one entry per element, in document order
 */
function snapshotRendered(roots: Element[], labels: SnapshotLabels): RenderedRoot[] {
  const chipPattern = new RegExp(labels.chipPattern, 'u');
  const flagsSelector = `ul[aria-label="${labels.flagsLabel}"]`;
  const text = (node: Element | null | undefined): string => {
    if (node === null || node === undefined) {
      return '';
    }
    const copy = node.cloneNode(true);
    if (copy instanceof Element) {
      for (const bubble of Array.from(copy.querySelectorAll('[role="tooltip"]'))) {
        bubble.remove();
      }
    }
    return copy.textContent?.trim() ?? '';
  };

  const readBlock = (table: HTMLTableElement): EvidenceBlock => {
    const rows: EvidenceRow[] = [];
    let empty: string | null = null;
    for (const row of Array.from(table.tBodies[0]?.rows ?? [])) {
      const emptyCell = row.querySelector(':scope > td.ds-datatable__empty');
      if (emptyCell !== null) {
        empty = text(emptyCell);
        continue;
      }
      const cells = Array.from(row.querySelectorAll(':scope > td'));
      const baselineCell = cells[1];
      const entries = Array.from(
        baselineCell?.querySelectorAll(':scope > .ds-stack > div') ?? [],
      ).map(text);
      rows.push({
        header: text(row.querySelector(':scope > th')),
        value: text(cells[0]),
        baselines: entries.length > 0 ? entries : [text(baselineCell)],
      });
    }
    const parts = Array.from(table.closest('.ds-datatable__region')?.parentElement?.children ?? []);
    return {
      caption: text(table.caption),
      rows,
      empty,
      lists: parts
        .filter((part) => part.classList.contains('ds-stack'))
        .map((part) => {
          const list = part.querySelector(':scope > ul');
          return {
            heading: text(part.querySelector(':scope > div > b')),
            label: list?.getAttribute('aria-label') ?? '',
            entries: Array.from(list?.children ?? []).map(text),
          };
        }),
      lines: parts
        .filter((part) => part.tagName === 'DIV' && part.childElementCount === 0)
        .map(text),
    };
  };

  const readEvidence = (container: Element): RenderedEvidence => ({
    evidence: Array.from(container.querySelectorAll('table'))
      .filter((table) => {
        const caption = text(table.caption);
        return (
          caption === labels.evidenceCaption || caption.startsWith(labels.evidenceCaptionPrefix)
        );
      })
      .map(readBlock),
    chips: Array.from(container.querySelectorAll('.ds-badge'))
      .filter((badge) => badge.closest(flagsSelector) === null)
      .map(text)
      .filter((chip) => chipPattern.test(chip)),
    flags: Array.from(container.querySelectorAll(flagsSelector)).map((list) =>
      Array.from(list.children).map(text),
    ),
  });

  const readTable = (table: HTMLTableElement, withDetails: boolean): RenderedTable => ({
    caption: text(table.caption),
    headers: Array.from(table.tHead?.rows[0]?.cells ?? []).map(text),
    rows: Array.from(table.tBodies[0]?.rows ?? [])
      .filter((row) => !row.classList.contains('ds-datatable__detail-row'))
      .map((row) => {
        const toggle = row.querySelector(':scope > td.ds-datatable__toggle-cell button');
        const controls = toggle?.getAttribute('aria-controls') ?? null;
        const detailRow =
          withDetails && controls !== null ? document.getElementById(controls) : null;
        const detailCell =
          detailRow === null || detailRow.hidden ? null : detailRow.querySelector(':scope > td');
        return {
          cells: Array.from(row.cells)
            .filter((cell) => !cell.classList.contains('ds-datatable__toggle-cell'))
            .map(text),
          toggle:
            toggle === null
              ? null
              : { label: text(toggle), expanded: toggle.getAttribute('aria-expanded') },
          detail:
            detailCell === null
              ? null
              : {
                  ...readEvidence(detailCell),
                  creatives: Array.from(detailCell.querySelectorAll('table'))
                    .filter((inner) => text(inner.caption).startsWith(labels.perCreativePrefix))
                    .map((inner) => readTable(inner, false)),
                  notes: Array.from(detailCell.querySelectorAll('ul[aria-label]'))
                    .filter((list) =>
                      (list.getAttribute('aria-label') ?? '').startsWith(labels.rowNotesPrefix),
                    )
                    .map((list) => ({
                      label: list.getAttribute('aria-label') ?? '',
                      entries: Array.from(list.children).map(text),
                    })),
                },
        };
      }),
  });

  return roots.map((root) =>
    root instanceof HTMLTableElement
      ? { card: null, table: readTable(root, true) }
      : { card: { elementId: root.id, ...readEvidence(root) }, table: null },
  );
}

/** The recommendation cards inside `container`, read in bulk. */
async function renderedCards(container: Locator): Promise<RenderedCard[]> {
  const roots = await container
    .locator('article[id^="item-"]')
    .evaluateAll(snapshotRendered, SNAPSHOT_LABELS);
  return roots.flatMap((root) => (root.card === null ? [] : [root.card]));
}

/** The white-paper asset tables inside `container`, each row with its expanded detail. */
async function renderedAssetTables(container: Locator): Promise<RenderedTable[]> {
  const roots = await container
    .getByRole('table', { name: WHITE_PAPER_TABLE })
    .evaluateAll(snapshotRendered, SNAPSHOT_LABELS);
  return roots.flatMap((root) => (root.table === null ? [] : [root.table]));
}

// ---------------------------------------------------------------------------------------------
// Expected evidence: the API's frozen values, rendered as the cards render them
// ---------------------------------------------------------------------------------------------

/** A metric's row header: its label, or its key when the label is blank (`metricLabel`). */
function metricLabel(metric: Metric): string {
  return metric.label.trim() === '' ? metric.key : metric.label;
}

/** A baseline's label, or its key when the label is blank (`baselineLabel`). */
function baselineLabel(baseline: Baseline): string {
  return baseline.label.trim() === '' ? baseline.key : baseline.label;
}

/**
 * The index of the metric a baseline pairs with, mirroring `matchMetric` in
 * `src/features/actions/EvidenceTable.tsx` (a React module this Node-side spec cannot import):
 * the same key, else `{metric}_baseline` or any `{qualifier}_{metric}`, the longest metric key
 * winning and a tie keeping the first metric.
 */
function matchMetric(
  baselineKey: string,
  metricIndex: ReadonlyMap<string, number>,
): number | undefined {
  const exact = metricIndex.get(baselineKey);
  if (exact !== undefined) {
    return exact;
  }
  let best: { index: number; length: number } | undefined;
  for (const [key, index] of metricIndex) {
    if (key === '') {
      continue;
    }
    const derived = baselineKey === `${key}_baseline` || baselineKey.endsWith(`_${key}`);
    if (derived && (best === undefined || key.length > best.length)) {
      best = { index, length: key.length };
    }
  }
  return best?.index;
}

/** The baselines each metric row shows and those listed below the table (`pairBaselines`). */
function pairBaselines(
  metrics: readonly Metric[],
  baselines: readonly Baseline[],
): { byMetric: Baseline[][]; unpaired: Baseline[] } {
  const metricIndex = new Map<string, number>();
  metrics.forEach((metric, index) => {
    if (!metricIndex.has(metric.key)) {
      metricIndex.set(metric.key, index);
    }
  });
  const byMetric: Baseline[][] = metrics.map(() => []);
  const unpaired: Baseline[] = [];
  for (const baseline of baselines) {
    const index = matchMetric(baseline.key, metricIndex);
    const row = index === undefined ? undefined : byMetric[index];
    if (row === undefined) {
      unpaired.push(baseline);
    } else {
      row.push(baseline);
    }
  }
  return { byMetric, unpaired };
}

/**
 * A paired baseline as `formatMetric` formats it (`pairedBaselineInput`): its value in its
 * metric's unit and currency, without the metric's floor or unavailable markers.
 */
function pairedBaselineInput(baseline: Baseline, metric: Metric): MetricInput {
  return { value: baseline.value, unit: metric.unit, currency: metric.currency };
}

/** The baselines each metric row shows, as {@link pairBaselines} pairs them. */
interface BaselineRows {
  readonly byMetric: readonly (readonly Baseline[])[];
}

/**
 * Every baseline paired with a metric, each as the table formats it ({@link pairedBaselineInput};
 * `pairedBaselineInputs` in `EvidenceTable.tsx`).
 */
function pairedBaselineInputs(metrics: readonly Metric[], pairing: BaselineRows): MetricInput[] {
  return metrics.flatMap((metric, index) =>
    (pairing.byMetric[index] ?? []).map((baseline) => pairedBaselineInput(baseline, metric)),
  );
}

/**
 * The one fixed fraction-digit count of every percentage in an evidence table, as
 * `evidenceRatioDigits` in `EvidenceTable.tsx` picks it (`tableRatioDigits`):
 * `ratioMetricDigits` over every metric and every baseline paired with one, each as the table
 * formats it. CR1 lead rates of 0.000722 and 0.000698 therefore read "0.072%" and "0.070%",
 * never "0.1%" twice.
 */
function tableRatioDigits(metrics: readonly Metric[], pairing: BaselineRows): number {
  return ratioMetricDigits([...metrics, ...pairedBaselineInputs(metrics, pairing)]);
}

/**
 * The options every metric value and paired baseline of an evidence table is formatted with, as
 * `tableFormatOptions` in `EvidenceTable.tsx` picks them: every percentage at the table's one
 * precision ({@link tableRatioDigits}), and every rate per unit of spend stated per 100 units of
 * the evidence's account currency (`evidenceCurrency`, which a rate metric does not carry
 * itself), at the one precision `rateMetricDigits` picks for every metric and paired baseline in
 * that currency. B4's weekend and weekday rates of 0.013652 and 0.091546 in USD therefore read
 * "1.37 per USD 100" and "9.15 per USD 100" (a no-break space before "100"); with no currency
 * recorded, unscaled at one precision, "0.0137" and "0.0915". A rate is never a percentage.
 */
function tableFormatOptions(evidence: Evidence, pairing: BaselineRows): MetricFormatOptions {
  const { metrics } = evidence;
  const rateCurrency = evidenceCurrency(evidence);
  const cited = [...metrics, ...pairedBaselineInputs(metrics, pairing)];
  return {
    ratioDigits: tableRatioDigits(metrics, pairing),
    rateCurrency,
    rateDigits: rateMetricDigits(cited, rateCurrency),
  };
}

/**
 * An unpaired baseline's value (`formatUnpairedBaseline`): in its own unit and currency when it
 * records either, as C1's account cost per outcome does; else a number at six places, else
 * unitless. Blank unit or currency text counts as none.
 */
function unpairedBaselineText(baseline: Baseline): string {
  const { value, unit, currency } = baseline;
  if ((unit ?? '').trim() !== '' || (currency ?? '').trim() !== '') {
    return formatMetric({ value, unit, currency });
  }
  if (typeof value === 'number' || (typeof value === 'string' && isDecimalString(value))) {
    return formatDecimal(value, RECORDED_DIGITS);
  }
  return formatMetric({ value });
}

/** An object's own entries in code-unit key order (`sortedEntries` in `EvidenceTable.tsx`). */
function sortedEntries(record: object): [string, unknown][] {
  return Object.entries(record).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

/** A configured threshold's value, mirroring `formatThresholdValue` in `EvidenceTable.tsx`. */
function thresholdText(value: unknown): string {
  if (value === null || value === undefined) {
    return UNAVAILABLE;
  }
  if (typeof value === 'number') {
    return formatDecimal(value, THRESHOLD_DIGITS);
  }
  if (typeof value === 'string') {
    if (isDecimalString(value)) {
      return formatDecimal(value, THRESHOLD_DIGITS);
    }
    return value.trim() === '' ? UNAVAILABLE : value;
  }
  if (typeof value === 'boolean') {
    return formatMetric({ value });
  }
  if (Array.isArray(value)) {
    const entries: readonly unknown[] = value;
    return entries.length === 0
      ? EMPTY_THRESHOLD_LIST
      : entries.map((entry) => thresholdText(entry)).join(', ');
  }
  if (typeof value === 'object') {
    const pairs = sortedEntries(value).map(([key, entry]) => `${key}: ${thresholdText(entry)}`);
    return `{${pairs.join('; ')}}`;
  }
  return UNAVAILABLE;
}

/** The sources a dataset name's prefix can name, as in `google.metrics_daily`. */
const DATASET_SOURCES: readonly SourceName[] = ['google', 'linkedin', 'hubspot'];

/**
 * One "Data used" entry. With a recorded date: "{dataset}: {completeness}, covered through
 * {date}", the date as stored and labelled with its source's day boundary: the entry's own
 * `boundary` when it is not blank ("2026-10-05 (HubSpot day, UTC)"), else the one the window's
 * `source_days` records for the source ("2026-10-05 (Google day, America/New_York)"), the
 * source's day alone when neither does (evidence frozen before entries recorded a boundary
 * reads "(HubSpot day)"), and no label when the name's prefix is no known source. Without one:
 * "{dataset}: {completeness}".
 */
function datasetText(entry: DatasetCoverage, sourceDays: EvidenceWindow['source_days']): string {
  const head = `${entry.dataset}: ${entry.completeness}`;
  const through = entry.covered_through?.trim() ?? '';
  if (through === '') {
    return head;
  }
  const dot = entry.dataset.indexOf('.');
  const prefix = dot > 0 ? entry.dataset.slice(0, dot) : '';
  const source = DATASET_SOURCES.find((name) => name === prefix);
  if (source === undefined) {
    return `${head}, covered through ${through}`;
  }
  const own = entry.boundary?.trim() ?? '';
  const boundary =
    own === '' ? (sourceDays.find((day) => day.source === source)?.boundary ?? null) : own;
  return `${head}, covered through ${formatSourceDate(through, source, boundary)}`;
}

/**
 * The evidence block an item's frozen evidence renders as (AAP 0.5.4.8; `EvidenceTable.tsx`):
 * one row per metric with its value and paired baselines, every percentage among them at the
 * table's one precision and every rate per unit of spend per 100 units of the evidence's
 * currency at one shared precision ({@link tableFormatOptions}), the unpaired baselines, the
 * thresholds applied and the datasets used, then the sample and test lines.
 *
 * @param evidence - the item's `evidence`, as the API returned it
 * @param caption - the table's caption: "Evidence" on a card, "Evidence for {asset} ({scope})"
 *   on a row ({@link assetRowLabel})
 * @returns the block, every text trimmed as the snapshot reads it
 */
function expectedEvidenceBlock(evidence: Evidence, caption: string): EvidenceBlock {
  const { metrics, baselines, sample, test: statTest, thresholds, datasets } = evidence;
  const pairing = pairBaselines(metrics, baselines);
  const options = tableFormatOptions(evidence, pairing);
  const rows = metrics.map((metric, index): EvidenceRow => {
    const paired = pairing.byMetric[index] ?? [];
    return {
      header: metricLabel(metric).trim(),
      value: formatMetric(metric, options).trim(),
      baselines:
        paired.length === 0
          ? [EM_DASH]
          : paired.map((baseline) => {
              const value = formatMetric(pairedBaselineInput(baseline, metric), options);
              return `${baselineLabel(baseline)}: ${value}`.trim();
            }),
    };
  });

  const lists: EvidenceList[] = [];
  if (pairing.unpaired.length > 0) {
    const label = pairing.byMetric.some((paired) => paired.length > 0)
      ? OTHER_BASELINES_LABEL
      : BASELINES_LABEL;
    const entries = pairing.unpaired.map((baseline) =>
      `${baselineLabel(baseline)}: ${unpairedBaselineText(baseline)}`.trim(),
    );
    lists.push({ heading: label, label, entries });
  }
  const thresholdEntries = sortedEntries(thresholds);
  if (thresholdEntries.length > 0) {
    const entries = thresholdEntries.map(([key, value]) =>
      `${key} = ${thresholdText(value)}`.trim(),
    );
    lists.push({ heading: THRESHOLDS_LABEL, label: THRESHOLDS_LABEL, entries });
  }
  if (datasets.length > 0) {
    const entries = datasets.map((entry) =>
      datasetText(entry, evidence.window.source_days).trim(),
    );
    lists.push({ heading: DATASETS_LABEL, label: DATASETS_LABEL, entries });
  }

  const lines: string[] = [];
  const sampleText = sample === null ? '' : formatSample(sample);
  if (sampleText !== '') {
    lines.push(`Sample: ${sampleText}`.trim());
  }
  if (statTest !== null) {
    const name = statTest.name.trim() === '' ? UNNAMED_TEST : statTest.name;
    lines.push(`Test: ${formatTestLine({ ...statTest, name })}`.trim());
  }
  return { caption, rows, empty: metrics.length === 0 ? NO_METRICS_MESSAGE : null, lists, lines };
}

// ---------------------------------------------------------------------------------------------
// Rendering rules checked without the shared formatter
// ---------------------------------------------------------------------------------------------

/**
 * A currency's code as `Intl` prints it with `currencyDisplay: 'code'` ("USD", "EUR", "XYZ"),
 * the one money style the UI shares with the backend's reason texts; `null` for a code it
 * refuses.
 */
function currencyCode(code: string): string | null {
  try {
    const parts = new Intl.NumberFormat('en', {
      style: 'currency',
      currency: code,
      currencyDisplay: 'code',
    }).formatToParts(1);
    return parts.find((part) => part.type === 'currency')?.value ?? null;
  } catch {
    // Intl throws RangeError for a malformed code; the value then renders with no code prefix.
    return null;
  }
}

/** A whole sample size as plain `Intl` grouping prints it, or `null` for anything else. */
function wholeCount(value: MetricValue): string | null {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? COUNT_FORMAT.format(value) : null;
  }
  if (typeof value === 'string' && /^\d+$/u.test(value.trim())) {
    return COUNT_FORMAT.format(BigInt(value.trim()));
  }
  return null;
}

/**
 * Checks one rendered metric value against AAP 0.5.5 and 0.11.1 directly, so a regression in
 * `formatMetric` itself fails too: an unavailable value is exactly "unavailable", never 0; text
 * renders verbatim; a numeric value is never "unavailable"; a floored count starts "≥ " and a
 * floored amount "≤ "; an amount shows its currency's ISO code.
 *
 * @param rendered - the cell's text
 * @param metric - the metric, as the API returned it
 * @param where - the item and cell, for the failure message
 */
function expectMetricRendering(rendered: string, metric: Metric, where: string): void {
  const { value } = metric;
  const cell = `${where}, "${metric.key}"`;
  if (metric.unavailable === true || value === null) {
    expect(rendered, `${cell} is unavailable, so it reads "${UNAVAILABLE}", never 0`).toBe(
      UNAVAILABLE,
    );
    return;
  }
  if (typeof value === 'boolean') {
    expect(rendered, `${cell} is a yes/no value`).toBe(value ? 'yes' : 'no');
    return;
  }
  const unit = (metric.unit ?? '').trim().toLowerCase();
  if (typeof value === 'string' && (unit === 'text' || !isDecimalString(value))) {
    expect(rendered, `${cell} is text, shown verbatim`).toBe(value.trim());
    return;
  }
  expect(rendered, `${cell} holds a number, so it is not "${UNAVAILABLE}"`).not.toBe(UNAVAILABLE);
  const currency = (metric.currency ?? '').trim();
  if (metric.floor === true) {
    const marker = currency !== '' || AMOUNT_UNITS.has(unit) ? '≤ ' : '≥ ';
    expect(
      rendered.startsWith(marker),
      `${cell} is an attribution floor, so it reads "${marker}…", not "${rendered}"`,
    ).toBe(true);
  }
  const code = currency === '' ? null : currencyCode(currency);
  if (code !== null) {
    expect(
      rendered,
      `${cell} is an amount in ${currency}, its code kept with its figure`,
    ).toContain(`${code}${NO_BREAK_SPACE}`);
  }
}

/**
 * Checks the sample and test lines against the evidence directly: a sample line exactly when
 * the evidence has a sample, reading "{observed} / {required}" with its unit; a test line
 * exactly when a test was used, with its p-value and, after a Holm adjustment, the adjusted one.
 */
function expectSampleAndTest(lines: readonly string[], evidence: Evidence, where: string): void {
  const { sample, test: statTest } = evidence;
  const samples = lines.filter((line) => line.startsWith('Sample: '));
  const hasSample = sample !== null && (sample.observed !== null || sample.required !== null);
  expect(samples.length, `${where} shows a sample line exactly when it has a sample`).toBe(
    hasSample ? 1 : 0,
  );
  if (sample !== null && hasSample) {
    const observed = wholeCount(sample.observed);
    const required = wholeCount(sample.required);
    if (observed !== null && required !== null) {
      // The figure is joined by no-break spaces (U+00A0), so it never wraps inside the figure.
      expect(samples[0], `${where}: the sample line reads observed / required`).toContain(
        `${observed}\u00A0/\u00A0${required}`,
      );
    }
    const unit = (sample.unit ?? '').trim();
    if (unit !== '') {
      expect(samples[0], `${where}: the sample line names its unit`).toContain(` ${unit}`);
    }
  }
  const tests = lines.filter((line) => line.startsWith('Test: '));
  expect(tests.length, `${where} shows a test line exactly when a test was used`).toBe(
    statTest === null ? 0 : 1,
  );
  if (statTest !== null) {
    expect(tests[0], `${where}: the test line states its p-value`).toMatch(/ · p [=<] /u);
    if (statTest.p_adjusted !== null) {
      expect(tests[0], `${where}: the test line states its adjusted p-value`).toContain(
        ' · adjusted p ',
      );
    }
  }
}

/**
 * Checks everything an item's card or white-paper detail shows of its evidence: the rules above
 * first, then its one evidence block exactly as {@link expectedEvidenceBlock} renders it, its
 * one window chip exactly as `formatWindowChip` renders its frozen window (RecommendationCard
 * and AssetDetails render `item.evidence.window`, never the run's), with every fragment
 * {@link chipFragments} builds, and its one flag list, one badge per recorded and current
 * flag in order, "Simulated data" among them for simulated or mixed data.
 *
 * @param rendered - what the card or detail shows
 * @param item - the item, as the API returned it
 * @param caption - the evidence table's caption
 * @param where - the item, for failure messages
 */
function expectItemEvidence(
  rendered: RenderedEvidence,
  item: Item,
  caption: string,
  where: string,
): void {
  expect(
    rendered.evidence.map((block) => block.caption),
    `${where} shows one evidence table, "${caption}"`,
  ).toEqual([caption]);
  const block = rendered.evidence[0];
  if (block === undefined) {
    throw new Error(`${where} shows no evidence table`);
  }
  const { metrics } = item.evidence;
  expect(block.rows.length, `${where} lists each of its ${metrics.length} metrics`).toBe(
    metrics.length,
  );
  metrics.forEach((metric, index) => {
    expectMetricRendering(block.rows[index]?.value ?? '', metric, where);
  });
  expectSampleAndTest(block.lines, item.evidence, where);
  expect(
    block,
    `${where}: metric values, baselines, sample, test, thresholds and datasets`,
  ).toEqual(expectedEvidenceBlock(item.evidence, caption));

  const { window } = item.evidence;
  expect(rendered.chips.length, `${where} shows one window chip`).toBe(1);
  for (const fragment of chipFragments(window)) {
    expect(rendered.chips[0], `${where}: its window chip shows "${fragment}"`).toContain(fragment);
  }
  expect(rendered.chips, `${where}: the window chip of its frozen evidence window`).toEqual([
    formatWindowChip(window),
  ]);

  const flags = shownFlags(item);
  expect(
    flags.length,
    `${where} carries at least one flag; under DATA_MODE=simulated every item is flagged`,
  ).toBeGreaterThan(0);
  if (item.data_status === 'simulated' || item.data_status === 'mixed') {
    expect(
      flags.map((flag) => flag.code),
      `${where}, computed from ${item.data_status} data, carries the simulated flag`,
    ).toContain('simulated');
  }
  expect(rendered.flags, `${where}: one badge per recorded and current flag`).toEqual([
    flags.map((flag) => flagText(flag)),
  ]);
}

// ---------------------------------------------------------------------------------------------
// Card sections, every item
// ---------------------------------------------------------------------------------------------

/** A group's anchor id, `group-{section}-{group}` made link-safe (`groupAnchorId`). */
function groupAnchor(sectionKey: SectionKey, groupKey: string): string {
  return `group-${sectionKey}-${groupKey}`.replace(/[^A-Za-z0-9_-]/gu, '-');
}

/** Ascending numeric order, for comparing id sets. */
function ascending(left: number, right: number): number {
  return left - right;
}

/**
 * Opens one group and mounts all of it: selects its keyword tab (only the selected tab's panel
 * is shown; its id is the group's anchor), or finds its group region, then reveals every held
 * disclosure and batch inside.
 */
async function openGroup(page: Page, sectionKey: SectionKey, group: Group): Promise<Locator> {
  const anchor = groupAnchor(sectionKey, group.key);
  if (sectionKey === 'keywords') {
    const tab = page
      .getByRole('tablist', { name: KEYWORD_TABS_LABEL, exact: true })
      .locator(`[role="tab"]${attributeSelector('aria-controls', anchor)}`);
    await tab.click({ timeout: READ_TIMEOUT_MS });
    await expect(tab).toHaveAttribute('aria-selected', 'true');
  }
  const container = page.locator(attributeSelector('id', anchor));
  await expect(container, `group ${anchor} is shown`).toBeVisible({ timeout: READ_TIMEOUT_MS });
  await revealAll(container);
  return container;
}

/**
 * Checks every card of one card section against the API page `expected` (AAP 0.11.2 "each
 * item showing evidence, window, source boundaries and simulated labels"): group by group, with
 * every tab, held disclosure and batch opened, the group shows exactly its unhidden items, and
 * each card's evidence, chip and flags are {@link expectItemEvidence}'s; then the whole section
 * shows exactly the section's unhidden items.
 *
 * @returns the rendered cards by item id
 */
async function expectSectionCards(
  page: Page,
  expected: RecommendationsPage,
  key: SectionKey,
): Promise<Map<number, RenderedCard>> {
  const section = sectionOf(expected, key);
  const region = sectionRegion(page, key);
  await revealAll(region);
  const checked = new Map<number, RenderedCard>();
  for (const group of section.groups) {
    const items = new Map(
      group.items.filter((item) => !isHidden(item)).map((item) => [item.id, item]),
    );
    if (items.size === 0) {
      continue;
    }
    const cards = await renderedCards(await openGroup(page, key, group));
    expect(
      cards.map((card) => cardItemId(card.elementId)).toSorted(ascending),
      `the "${group.key}" group of run ${expected.run.id} shows exactly its unhidden items`,
    ).toEqual([...items.keys()].toSorted(ascending));
    for (const card of cards) {
      const id = cardItemId(card.elementId);
      const item = items.get(id);
      if (item === undefined) {
        throw new Error(`Card item-${id} is not in the "${group.key}" group`);
      }
      const where = `card item-${id} (${item.rule_id}, "${key}", run ${expected.run.id})`;
      expectItemEvidence(card, item, EVIDENCE_CAPTION, where);
      checked.set(id, card);
    }
  }
  const all = await region
    .locator('article[id^="item-"]')
    .evaluateAll((articles) => articles.map((article) => article.id));
  expect(
    all.map(cardItemId).toSorted(ascending),
    `the "${key}" section shows exactly the unhidden items of run ${expected.run.id}`,
  ).toEqual(visibleItems(section).map((item) => item.id).toSorted(ascending));
  return checked;
}

// ---------------------------------------------------------------------------------------------
// White papers, every row
// ---------------------------------------------------------------------------------------------

/** One asset table a cohort renders: its caption, its rows' items in order, its MQL header. */
interface ExpectedAssetTable {
  caption: string;
  /**
   * What its rows' toggle names add after the asset, in parentheses: the cohort's currency,
   * followed for the replaced-data and held tables by the table's qualifier.
   */
  scope: string;
  items: Item[];
  /** The MQL header the rows' frozen evidence records; `null` when none records one. */
  mql: string | null;
}

/**
 * A white-paper row's label, independently of `rowLabelOf` in `WhitePapersSection.tsx`:
 * "{asset} ({scope})", and, when another row of the table shows the same asset name, "{asset}
 * ({scope}, {asset id})", the item id standing in for a blank asset id. The row's toggle and its
 * details' per-creative table, evidence table and notes list are each named for it.
 */
function assetRowLabel(table: ExpectedAssetTable, item: Item): string {
  const name = assetName(item);
  const shared = table.items.filter((other) => assetName(other) === name).length > 1;
  const entityId = item.entity.id?.trim() ?? '';
  const id = entityId === '' ? `item ${String(item.id)}` : entityId;
  return `${name} (${shared ? `${table.scope}, ${id}` : table.scope})`;
}

/** The name of an expanded white-paper row's toggle: "Hide details for {label}". */
function expandedToggleName(table: ExpectedAssetTable, item: Item): string {
  return `Hide details for ${assetRowLabel(table, item)}`;
}

/**
 * Either state's name of the toggle of a white-paper row named `name` in a table of `scope`,
 * matched in full: "Show details for {name} ({scope})" or "Hide details for …", with any
 * shared-name id after the scope.
 */
function detailsTogglePattern(name: string, scope: string): RegExp {
  return new RegExp(
    `^(?:Show|Hide) details for ${escapeRegExp(name)} \\(${escapeRegExp(scope)}(?:, [^()]*)?\\)$`,
    'u',
  );
}

/** "MQL ({label})", or "MQL" for a blank label or one that repeats it (`headerForLabel`). */
function mqlHeaderFor(label: string | null): string {
  const text = label?.trim() ?? '';
  return text === '' || text.toLowerCase() === MQL_PLAIN.toLowerCase()
    ? MQL_PLAIN
    : `${MQL_PLAIN} (${text})`;
}

/** The MQL header one row's frozen evidence records (`frozenMqlHeader`), or `null`. */
function frozenMqlHeader(item: Item): string | null {
  const label = whitePaperMqlLabel(item);
  if (label !== null) {
    return mqlHeaderFor(label);
  }
  const recorded = whitePaperMqlMetricLabel(item)?.trim() ?? '';
  return recorded === '' ? null : recorded;
}

/**
 * A table's MQL header (`tableMqlHeader`): the one header its rows record, "MQL" when they
 * disagree, or `null` when none records one and the current configuration heads it.
 */
function tableMqlHeader(items: readonly Item[]): string | null {
  const frozen = new Set(items.flatMap((item) => frozenMqlHeader(item) ?? []));
  const [only] = frozen;
  if (frozen.size === 0 || only === undefined) {
    return null;
  }
  return frozen.size === 1 ? only : MQL_PLAIN;
}

/** The currency a cohort's captions name (`cohortCurrency` in `WhitePapersSection.tsx`). */
function cohortCurrency(group: Group): string {
  for (const item of group.items) {
    const summary = cohortSummary(item);
    if (summary !== null) {
      return summary.currency;
    }
  }
  for (const item of group.items) {
    const currency = evidenceCurrency(item);
    if (currency !== null) {
      return currency;
    }
  }
  return group.title.trim() === '' ? group.key : group.title.trim();
}

/**
 * The currency of the cohort an item belongs to, read from the item alone in the order
 * {@link cohortCurrency} reads its group: its cross-asset view's, else the first currency its
 * evidence names, else `fallback`, such as its group's title.
 */
function itemCurrency(item: Item, fallback: string): string {
  return cohortSummary(item)?.currency ?? evidenceCurrency(item) ?? fallback;
}

/**
 * The asset tables one cohort renders, in order (AAP 0.5.5, 0.3.2): its visible rows, its rows
 * computed from replaced data, then the rows held behind its disclosure; hidden rows are only
 * counted.
 */
function cohortTables(group: Group): ExpectedAssetTable[] {
  const shown = group.items.filter((item) => !isHidden(item));
  const historical = shown.filter((item) => item.current.historical);
  const held = shown.filter((item) => !item.current.historical && isHeld(item));
  const visible = shown.filter((item) => !item.current.historical && !isHeld(item));
  const currency = cohortCurrency(group);
  return [
    { caption: `White papers (${currency})`, scope: currency, items: visible },
    {
      caption: `White papers (${currency}) ${REPLACED_TABLE_QUALIFIER}`,
      scope: `${currency}, ${REPLACED_TABLE_QUALIFIER}`,
      items: historical,
    },
    {
      caption: `White papers (${currency}) ${HELD_TABLE_QUALIFIER}`,
      scope: `${currency}, ${HELD_TABLE_QUALIFIER}`,
      items: held,
    },
  ]
    .filter((table) => table.items.length > 0)
    .map((table) => ({ ...table, mql: tableMqlHeader(table.items) }));
}

/** A white-paper cell (`cellText`): its metric's text, "— ({reason})" after a known-zero "—". */
function whitePaperCellText(
  metric: Metric | null | undefined,
  note: string | null | undefined,
): string {
  if (metric === null || metric === undefined) {
    return UNAVAILABLE;
  }
  const text = formatMetric(metric);
  const reason = note?.trim() ?? '';
  return text.trim() === EM_DASH && reason !== '' ? `${EM_DASH} (${reason})` : text;
}

/**
 * The simulated-data badge text an asset row's name carries (`simulatedFlag`, shown with
 * {@link flagText} as the flags row shows it), or `null`.
 */
function assetBadge(item: Item): string | null {
  const flag = shownFlags(item).find((each) => each.code === 'simulated');
  if (flag !== undefined) {
    return flagText(flag);
  }
  const label = (status: DataStatus): string | null =>
    status === 'simulated'
      ? 'Simulated data'
      : status === 'mixed'
        ? 'Joins live and simulated data'
        : null;
  const fallback = label(item.data_status) ?? label(item.current.data_status_now);
  return fallback === null ? null : flagText({ code: 'simulated', message: fallback });
}

/**
 * The flag codes whose badges an asset row's name carries after its simulated-data badge
 * (`FLOOR_FLAG_CODES` in `WhitePapersSection.tsx`).
 */
const FLOOR_FLAG_CODES: ReadonlySet<FlagCode> = new Set<FlagCode>([
  'attribution_floor',
  'creative_floor',
  'deal_floor',
  'unknown_crm_evidence',
]);

/**
 * The floor badges an asset row's name carries after its simulated-data badge (`floorFlags`):
 * the badge text ({@link flagText}) of each of its shown floor flags ({@link shownFlags}), in
 * their shown order, as its flags row shows them, for example "Unknown CRM evidence: Floor: CRM
 * evidence unknown for 10% of matched contacts (2 of 20)".
 */
function assetFloorBadges(item: Item): string[] {
  return shownFlags(item)
    .filter((flag) => FLOOR_FLAG_CODES.has(flag.code))
    .map((flag) => flagText(flag));
}

/**
 * An entity's identity (`entityKey` in `WhitePapersSection.tsx`): source, type and id as one
 * JSON array, or `null` without an id.
 */
function entityKey(entity: EntityView): string | null {
  if (typeof entity.id !== 'string' || entity.id.trim() === '') {
    return null;
  }
  return JSON.stringify([entity.source, entity.type, entity.id]);
}

/**
 * The skipped-stage notes a rendered white-paper row lists first in its details' "Notes on
 * {asset} ({scope})" (`placeSkippedFindings`, `assetNotes`): the message of each
 * `skipped_stage` finding of the section about the row's entity, in API order, or its code when
 * the message is blank.
 */
function skippedStageNotes(section: Section, item: Item): string[] {
  const key = entityKey(item.entity);
  if (key === null) {
    return [];
  }
  return section.findings.flatMap((finding) =>
    finding.code === 'skipped_stage' &&
    finding.entity !== null &&
    entityKey(finding.entity) === key
      ? [finding.message.trim() === '' ? finding.code : finding.message.trim()]
      : [],
  );
}


/** The column headers of an asset table (`withToggle`) or a per-creative table. */
function whitePaperHeaders(first: string, mql: string, withToggle: boolean): string[] {
  const metrics = WHITE_PAPER_METRIC_KEYS.map((key) =>
    key === 'mql' ? mql : WHITE_PAPER_HEADERS[key],
  );
  return withToggle ? [TOGGLE_HEADER, first, ...metrics, ACTION_HEADER] : [first, ...metrics];
}

/** Checks headers whose MQL column may carry the current configuration's label (`null`). */
function expectHeaders(rendered: readonly string[], expected: string[], mql: string | null): void {
  const mqlAt = expected.indexOf(MQL_PLAIN);
  if (mql === null && mqlAt >= 0) {
    expect(rendered[mqlAt], 'the MQL column is headed "MQL…"').toMatch(/^MQL\b/u);
    expect(
      rendered.filter((_, index) => index !== mqlAt),
      'the white-paper columns, in the order AAP 0.5.5 fixes',
    ).toEqual(expected.filter((_, index) => index !== mqlAt));
    return;
  }
  expect(rendered, 'the white-paper columns, in the order AAP 0.5.5 fixes').toEqual(expected);
}

/**
 * The hold reason a row a discrepancy holds shows in its Action cell (`discrepancyHoldReason`):
 * the live reason, else the recorded one, else {@link HELD_REASON_FALLBACK}; `null` for a row no
 * discrepancy holds, a historical row included, whose badge states its hold instead.
 */
function discrepancyHoldReason(item: Item): string | null {
  if (item.current.historical || !isHeld(item)) {
    return null;
  }
  const reason = [item.current.held_reason_now, item.held_reason]
    .map((text) => text?.trim() ?? '')
    .find((text) => text !== '');
  return reason ?? HELD_REASON_FALLBACK;
}

/**
 * An asset row's cells (`AssetTable`): the Asset cell (name, simulated-data badge, then floor
 * badges, {@link assetFloorBadges}; the row's skipped-stage notes are in its details, not here),
 * the nine metric cells, then the action.
 * A historical row's action is followed by its replaced-data badge; a held row's by its hold
 * reason and its blocker links, whose labels are checked by form ({@link BLOCKER_LINKS_PATTERN}).
 */
function assetRowCells(item: Item): (string | ReturnType<typeof expect.stringMatching>)[] {
  const cells = whitePaperCells(item);
  const notes = whitePaperCostNotes(item);
  const badge = assetBadge(item) ?? '';
  const floors = assetFloorBadges(item).join('');
  const action = item.current.historical ? `${item.action}${REPLACED_DATA_LABEL}` : item.action;
  const heldReason = discrepancyHoldReason(item);
  return [
    `${assetName(item)}${badge}${floors}`.trim(),
    ...WHITE_PAPER_METRIC_KEYS.map((key) =>
      whitePaperCellText(cells[key], WHITE_PAPER_COSTS.has(key) ? notes[key] : null).trim(),
    ),
    heldReason === null
      ? action.trim()
      : expect.stringMatching(
          new RegExp(
            `^${escapeRegExp(`${item.action.trimStart()}${heldReason}`)}${BLOCKER_LINKS_PATTERN}$`,
            'su',
          ),
        ),
  ];
}

/** A per-creative table's rows (`creativeRows`), or its one empty-message cell. */
function creativeTableRows(item: Item): string[][] {
  const rows = creativeRows(item);
  if (rows.length === 0) {
    return [[NO_CREATIVE_ROWS]];
  }
  return rows.map((row) => [
    row.label.trim(),
    ...WHITE_PAPER_METRIC_KEYS.map((key) =>
      whitePaperCellText(row.cells[key], row.notes[key]).trim(),
    ),
  ]);
}

/** The raw `extra.rows` entries of a white-paper item, as the API returned them. */
function rawCreativeRows(item: Item): Record<string, unknown>[] {
  const rows = item.evidence.extra?.rows;
  return Array.isArray(rows) ? rows.filter(isRecord) : [];
}

/**
 * Checks a white-paper cell against AAP 0.5.5 directly from the raw API value: a count floor
 * reads "≥ N", a cost ceiling "≤ X", an unavailable value "unavailable", and a cost whose known
 * denominator is 0 "—".
 */
function expectRawCellRendering(
  rendered: string,
  raw: unknown,
  cost: boolean,
  where: string,
): void {
  if (!isRecord(raw)) {
    return;
  }
  if (raw.unavailable === true) {
    expect(rendered, `${where} is unavailable, never 0`).toBe(UNAVAILABLE);
  } else if (cost && raw.ceiling === true && typeof raw.value === 'string') {
    expect(rendered.startsWith('≤ '), `${where} is a ceiling, "≤ …", not "${rendered}"`).toBe(true);
  } else if (cost && raw.value === null && raw.unavailable === false) {
    expect(rendered.startsWith(EM_DASH), `${where} has a zero denominator, "—…"`).toBe(true);
  } else if (!cost && raw.floor === true) {
    expect(rendered.startsWith('≥ '), `${where} is a floor, "≥ …", not "${rendered}"`).toBe(true);
  }
}

/**
 * Checks an asset row's cells and its per-creative rows against AAP 0.5.5 directly: every
 * evidence metric by {@link expectMetricRendering} (a "—" cost followed by its reason), every
 * per-creative count and cost by {@link expectRawCellRendering}, and a creative with neither id,
 * URN nor label is the "unknown creative" row.
 */
function expectWhitePaperRendering(
  cells: readonly string[],
  creatives: readonly RenderedRow[],
  item: Item,
  where: string,
): void {
  const recordedCosts = item.evidence.extra?.costs;
  const costs = isRecord(recordedCosts) ? recordedCosts : {};
  WHITE_PAPER_METRIC_KEYS.forEach((key, index) => {
    const metric = item.evidence.metrics.find((each) => each.key === key);
    const rendered = cells[index + 1] ?? '';
    const cost = costs[key];
    const note = isRecord(cost) && typeof cost.note === 'string' ? cost.note.trim() : '';
    if (metric === undefined) {
      expect(rendered, `${where}, "${key}" is not in its evidence`).toBe(UNAVAILABLE);
    } else if (metric.value === EM_DASH && note !== '') {
      expect(rendered, `${where}, "${key}" has a zero denominator`).toBe(`${EM_DASH} (${note})`);
    } else {
      expectMetricRendering(rendered, metric, where);
    }
  });
  const raw = rawCreativeRows(item);
  expect(creatives.length, `${where} shows each of its ${raw.length} creative rows`).toBe(
    Math.max(raw.length, 1),
  );
  raw.forEach((entry, rowIndex) => {
    const rendered = creatives[rowIndex]?.cells ?? [];
    const unnamed = typeof entry.label !== 'string' || entry.label.trim() === '';
    const unidentified =
      (entry.creative_id === null || entry.creative_id === undefined) &&
      (entry.creative_urn === null || entry.creative_urn === undefined);
    if (unnamed && unidentified) {
      expect(rendered[0], `${where}: a creative without id is "unknown creative"`).toBe(
        UNKNOWN_CREATIVE_LABEL,
      );
    }
    WHITE_PAPER_METRIC_KEYS.forEach((key, index) => {
      const field = CREDIT_ROW_FIELDS[key];
      if (field !== undefined) {
        expectRawCellRendering(
          rendered[index + 1] ?? '',
          entry[field],
          WHITE_PAPER_COSTS.has(key),
          `${where}, creative row ${rowIndex + 1}, "${key}"`,
        );
      }
    });
  });
}

/**
 * Checks every white-paper row against the API page `expected` (AAP 0.5.5 White papers): with
 * every held disclosure, batch and row opened, the section shows one table per cohort and row
 * kind, in order; each table its column headers and exactly its rows, in API order; each row its
 * cells; and each row's detail its per-creative rows, its evidence, chip and flags
 * ({@link expectItemEvidence}), and its skipped-stage notes first in its "Notes on {asset}
 * ({scope})" ({@link skippedStageNotes}), each part named for the row's label
 * ({@link assetRowLabel}).
 *
 * @returns the rendered asset tables
 */
async function expectWhitePaperRows(
  page: Page,
  expected: RecommendationsPage,
): Promise<RenderedTable[]> {
  const section = sectionOf(expected, 'white_papers');
  await revealAll(sectionRegion(page, 'white_papers'), { expandRows: true });
  const tables = await renderedAssetTables(sectionRegion(page, 'white_papers'));
  const cohorts = section.groups.flatMap(cohortTables);
  expect(
    tables.map((table) => table.caption),
    `the white papers of run ${expected.run.id}: one table per cohort and row kind, in order`,
  ).toEqual(cohorts.map((cohort) => cohort.caption));
  cohorts.forEach((cohort, tableIndex) => {
    const table = tables[tableIndex];
    if (table === undefined) {
      throw new Error(`Table "${cohort.caption}" is not rendered`);
    }
    expectHeaders(
      table.headers,
      whitePaperHeaders(ASSET_HEADER, cohort.mql ?? MQL_PLAIN, true),
      cohort.mql,
    );
    expect(
      table.rows.map((row) => row.toggle?.label),
      `"${cohort.caption}" lists its assets in API order, each with its expanded toggle`,
    ).toEqual(cohort.items.map((item) => expandedToggleName(cohort, item)));
    cohort.items.forEach((item, rowIndex) => {
      const name = assetName(item);
      // The row's details are named for its label, its cohort's currency included.
      const label = assetRowLabel(cohort, item);
      const where =
        `white paper "${name}" (item ${item.id}, "${cohort.caption}", ` +
        `run ${expected.run.id})`;
      const row = table.rows[rowIndex];
      if (row?.detail === null || row?.detail === undefined) {
        throw new Error(`${where} shows no expanded details`);
      }
      expect(row.toggle?.expanded, `${where} is expanded`).toBe('true');
      const [creatives, ...extra] = row.detail.creatives;
      expect(extra.length, `${where} shows one per-creative table`).toBe(0);
      if (creatives === undefined) {
        throw new Error(`${where} shows no per-creative table`);
      }
      expectWhitePaperRendering(row.cells, creatives.rows, item, where);
      expect(row.cells, `${where}: its cells`).toEqual(assetRowCells(item));
      const skipped = skippedStageNotes(section, item);
      const notes = row.detail.notes;
      expect(
        notes.map((list) => list.label),
        `${where} shows at most one notes list, its own`,
      ).toEqual(notes.length === 0 ? [] : [`${ROW_NOTES_PREFIX}${label}`]);
      expect(
        notes[0]?.entries.slice(0, skipped.length) ?? [],
        `${where}: its skipped-stage notes, verbatim and first among its notes`,
      ).toEqual(skipped);
      expect(creatives.caption, `${where}: its per-creative caption`).toBe(
        `${PER_CREATIVE_PREFIX}${label}`,
      );
      const mql = frozenMqlHeader(item) ?? cohort.mql;
      expectHeaders(
        creatives.headers,
        whitePaperHeaders(CREATIVE_HEADER, mql ?? MQL_PLAIN, false),
        mql,
      );
      expect(
        creatives.rows.map((creative) => creative.cells),
        `${where}: its per-creative rows`,
      ).toEqual(creativeTableRows(item));
      expectItemEvidence(row.detail, item, `${EVIDENCE_FOR_PREFIX}${label}`, where);
    });
  });
  return tables;
}

// ---------------------------------------------------------------------------------------------
// Routed copies of the page: what real data rarely holds
// ---------------------------------------------------------------------------------------------

/** The response headers that describe the original body, dropped when it is replaced. */
const BODY_HEADERS: ReadonlySet<string> = new Set([
  'content-length',
  'content-encoding',
  'transfer-encoding',
]);

/**
 * The headers to serve an edited JSON body with: the API's own, less those describing its
 * original body, with `X-Total-Count` raised by `added` rows when the response carries it.
 */
function editedHeaders(response: APIResponse, added = 0): Record<string, string> {
  const headers = Object.fromEntries(
    Object.entries(response.headers()).filter(([name]) => !BODY_HEADERS.has(name)),
  );
  const total = headers['x-total-count'];
  if (total !== undefined && /^\d+$/u.test(total)) {
    headers['x-total-count'] = String(Number(total) + added);
  }
  return headers;
}

/** The edited copy a routed read served the browser last; `null` until it served one. */
interface Served<T> {
  latest: T | null;
}

/**
 * Serves the browser, until `page.unrouteAll`, an edited copy of each successful
 * `GET /recommendations` it sends: the API's own response, as fetched for that request, passed
 * through `edit`. `page.request` reads are never routed, so they still return the API's data.
 *
 * @returns where the edit last served is kept, to derive the expected texts from
 */
async function serveEditedRecommendations<T extends { page: RecommendationsPage }>(
  page: Page,
  edit: (source: RecommendationsPage) => T,
): Promise<Served<T>> {
  const served: Served<T> = { latest: null };
  await page.route(
    (url) => url.pathname === RECOMMENDATIONS_PATH,
    async (route: Route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      const response = await route.fetch();
      if (!response.ok()) {
        await route.fulfill({ response });
        return;
      }
      const edited = edit((await response.json()) as RecommendationsPage);
      served.latest = edited;
      await route.fulfill({
        status: response.status(),
        headers: editedHeaders(response),
        json: edited.page,
      });
    },
  );
  return served;
}

/** Whether an item shows openly: not hidden, not computed from replaced data and not held. */
function isOpen(item: Item): boolean {
  return !isHidden(item) && !item.current.historical && !isHeld(item);
}

/** The items {@link withRenderingEdgeCases} edited in the page it built. */
interface EdgeCasePage {
  page: RecommendationsPage;
  /** The card given a floored count, a floored cost and an unavailable count. */
  card: { sectionKey: SectionKey; itemId: number; currency: string } | null;
  /** The white-paper row given floors, an unavailable MQL and zero-denominator costs. */
  assetItemId: number | null;
}

// The values the edge-case copy adds; the checks read them back literally.
const EDGE_FLOORED_COUNT = { key: 'e2e_floored_count', label: 'Floored count (e2e)', value: 12 };
const EDGE_FLOORED_COST = { key: 'e2e_floored_cost', label: 'Floored cost (e2e)', value: '123.45' };
const EDGE_UNAVAILABLE = { key: 'e2e_unavailable_count', label: 'Unavailable count (e2e)' };
const EDGE_COST_BASELINE = { key: 'baseline_e2e_floored_cost', label: 'Cost baseline (e2e)' };
const EDGE_UNPAIRED = { key: 'e2e_unpaired_reference', label: 'Unpaired reference (e2e)' };
const NO_MEETINGS_NOTE = 'no meetings yet';
const NO_QL_NOTE = 'no QL yet';

/** An attribution count cell of `extra.rows` (`CountValue` JSON). */
function countValue(value: number | null, floor: boolean): Record<string, unknown> {
  return value === null
    ? { value: null, floor: false, unavailable: true, reason: 'stage label not resolved' }
    : { value, floor, unavailable: false, reason: null };
}

/** A cost cell of `extra.rows` or `extra.costs` (`CostValue` JSON). */
function costValue(
  value: string | null,
  ceiling: boolean,
  note: string | null,
): Record<string, unknown> {
  return { value, ceiling, unavailable: false, note };
}

/**
 * Edits one open white-paper row the way the backend records floors, unknown stages and empty
 * denominators (`white_papers._row_metrics`, `attribution.CreditRow.to_json`): QL a floor and its
 * cost a ceiling, MQL unavailable, cost per meeting "—" for no meetings, the same on its first
 * creative row, and an "unknown creative" row with no QL or meeting yet.
 */
function editAsset(asset: Item): void {
  for (const metric of asset.evidence.metrics) {
    if (metric.key === 'ql' || metric.key === 'cost_per_ql') {
      metric.floor = true;
    } else if (metric.key === 'mql') {
      metric.value = null;
      metric.unavailable = true;
    } else if (metric.key === 'cost_per_meeting') {
      metric.value = EM_DASH;
    }
  }
  const extra = asset.evidence.extra ?? {};
  asset.evidence.extra = extra;
  const costs = isRecord(extra.costs) ? extra.costs : {};
  costs.cost_per_meeting = costValue(null, false, NO_MEETINGS_NOTE);
  extra.costs = costs;
  const rows = rawCreativeRows(asset);
  const [first] = rows;
  if (first !== undefined) {
    const ql = isRecord(first.ql) && typeof first.ql.value === 'number' ? first.ql.value : 1;
    const cost = isRecord(first.cost_per_ql) ? first.cost_per_ql.value : null;
    first.ql = countValue(ql, true);
    first.cost_per_ql = costValue(typeof cost === 'string' ? cost : '1.00', true, null);
    first.mql = countValue(null, false);
    first.cost_per_meeting = costValue(null, false, NO_MEETINGS_NOTE);
  }
  rows.push({
    creative_id: null,
    creative_urn: null,
    label: null,
    spend: '0.000000',
    leads: 0,
    mql: countValue(0, false),
    ql: countValue(0, false),
    meetings_booked: countValue(0, false),
    other_meetings: countValue(0, false),
    deals: countValue(0, false),
    cost_per_ql: costValue(null, false, NO_QL_NOTE),
    cost_per_meeting: costValue(null, false, NO_MEETINGS_NOTE),
  });
  extra.rows = rows;
}

/**
 * A copy of the page in which the first open card gains a floored count, a floored cost with a
 * paired baseline, an unavailable count and an unpaired baseline, and the first open white-paper
 * row is edited by {@link editAsset}: the renderings AAP 0.5.5 fixes that the simulated world
 * rarely produces.
 */
function withRenderingEdgeCases(source: RecommendationsPage): EdgeCasePage {
  const page = structuredClone(source);
  const sectionKey = CARD_SECTION_KEYS.find((key) => allItems(sectionOf(page, key)).some(isOpen));
  const card =
    sectionKey === undefined ? undefined : allItems(sectionOf(page, sectionKey)).find(isOpen);
  let edited: EdgeCasePage['card'] = null;
  if (sectionKey !== undefined && card !== undefined) {
    const currency = evidenceCurrency(card) ?? 'USD';
    card.evidence.metrics.push(
      { ...EDGE_FLOORED_COUNT, unit: 'contacts', currency: null, floor: true, unavailable: false },
      { ...EDGE_FLOORED_COST, unit: 'currency', currency, floor: true, unavailable: false },
      {
        ...EDGE_UNAVAILABLE,
        value: null,
        unit: 'contacts',
        currency: null,
        floor: null,
        unavailable: true,
      },
    );
    card.evidence.baselines.push(
      { ...EDGE_COST_BASELINE, value: '100' },
      { ...EDGE_UNPAIRED, value: '0.123456789' },
    );
    edited = { sectionKey, itemId: card.id, currency };
  }
  const asset = allItems(sectionOf(page, 'white_papers')).find(isOpen);
  if (asset !== undefined) {
    editAsset(asset);
  }
  return { page, card: edited, assetItemId: asset?.id ?? null };
}

/**
 * An amount as `Intl` prints it in `currency` after its ISO code ("USD 75.00"), code and figure
 * joined by {@link NO_BREAK_SPACE} whichever space ICU emits there, independently of
 * `src/lib/format.ts`.
 */
function plainMoney(amount: number, currency: string): string {
  return new Intl.NumberFormat('en', { style: 'currency', currency, currencyDisplay: 'code' })
    .formatToParts(amount)
    .map((part) =>
      part.type === 'literal' && /^\s+$/u.test(part.value) ? NO_BREAK_SPACE : part.value,
    )
    .join('');
}

/** Checks, literally, how the card {@link withRenderingEdgeCases} edited renders its additions. */
function expectEdgeCaseCard(card: RenderedCard | undefined, currency: string): void {
  const block = card?.evidence[0];
  const row = (label: string): EvidenceRow | undefined =>
    block?.rows.find((each) => each.header === label);
  expect(row(EDGE_FLOORED_COUNT.label), 'a floored count reads "≥ N"').toEqual({
    header: EDGE_FLOORED_COUNT.label,
    value: `≥ ${COUNT_FORMAT.format(EDGE_FLOORED_COUNT.value)}`,
    baselines: [EM_DASH],
  });
  expect(row(EDGE_FLOORED_COST.label), 'a floored cost reads "≤ X", its baseline paired').toEqual({
    header: EDGE_FLOORED_COST.label,
    value: `≤ ${plainMoney(Number(EDGE_FLOORED_COST.value), currency)}`,
    baselines: [`${EDGE_COST_BASELINE.label}: ${plainMoney(100, currency)}`],
  });
  expect(row(EDGE_UNAVAILABLE.label), 'an unavailable count reads "unavailable"').toEqual({
    header: EDGE_UNAVAILABLE.label,
    value: UNAVAILABLE,
    baselines: [EM_DASH],
  });
  expect(
    block?.lists.find((list) => list.label === OTHER_BASELINES_LABEL)?.entries,
    'an unpaired baseline is listed at the six places it was recorded at',
  ).toContain(`${EDGE_UNPAIRED.label}: 0.123457`);
}

/** Checks, literally, how the white-paper row {@link editAsset} edited renders. */
function expectEdgeCaseAsset(tables: readonly RenderedTable[], asset: Item): void {
  const name = assetName(asset);
  // The edited asset is open, so it sits in its cohort's table of visible rows.
  const toggle = detailsTogglePattern(name, itemCurrency(asset, ''));
  const row = tables
    .flatMap((table) => table.rows)
    .find((each) => toggle.test(each.toggle?.label ?? ''));
  const column = (key: WhitePaperMetricKey): number => WHITE_PAPER_METRIC_KEYS.indexOf(key) + 1;
  const ql = asset.evidence.metrics.find((metric) => metric.key === 'ql')?.value;
  if (typeof ql === 'number') {
    expect(row?.cells[column('ql')], 'a floored QL count reads "≥ N"').toBe(
      `≥ ${COUNT_FORMAT.format(ql)}`,
    );
  }
  expect(row?.cells[column('cost_per_ql')], 'a floored cost reads "≤ X"').toMatch(/^≤ \D/u);
  expect(row?.cells[column('mql')], 'an unresolved MQL count reads "unavailable"').toBe(
    UNAVAILABLE,
  );
  expect(row?.cells[column('cost_per_meeting')], 'no meetings: "—" and its reason').toBe(
    `${EM_DASH} (${NO_MEETINGS_NOTE})`,
  );
  const creatives = row?.detail?.creatives[0]?.rows.map((creative) => creative.cells) ?? [];
  const [first] = creatives;
  expect(first?.[column('ql')], 'a creative QL floor reads "≥ N"').toMatch(/^≥ \d/u);
  expect(first?.[column('cost_per_ql')], 'a creative cost ceiling reads "≤ X"').toMatch(/^≤ \D/u);
  expect(first?.[column('mql')], 'an unresolved creative MQL reads "unavailable"').toBe(
    UNAVAILABLE,
  );
  expect(first?.[column('cost_per_meeting')], 'no creative meetings: "—" and its reason').toBe(
    `${EM_DASH} (${NO_MEETINGS_NOTE})`,
  );
  const unknown = creatives.at(-1);
  expect(unknown?.[0], 'the creative row without id is "unknown creative"').toBe(
    UNKNOWN_CREATIVE_LABEL,
  );
  expect(unknown?.[column('cost_per_ql')], 'no QL yet: "—" and its reason').toBe(
    `${EM_DASH} (${NO_QL_NOTE})`,
  );
}

// ---------------------------------------------------------------------------------------------
// Discrepancy (C0) cards, every batch
// ---------------------------------------------------------------------------------------------

/** What one card of the discrepancy group shows of its footer and comparison line. */
interface RenderedResolver {
  elementId: string;
  /** The text of each rendered button, in document order. */
  buttons: string[];
  /** The text of each status line. */
  statuses: string[];
  /** The card's whole text. */
  text: string;
}

/**
 * Reads, inside the page, each card's rendered buttons (a closed dialog's are not rendered),
 * status lines and text. It runs through `evaluateAll`, so it reads only its argument and the
 * DOM.
 */
function snapshotResolvers(articles: Element[]): RenderedResolver[] {
  const text = (node: Element): string => node.textContent?.trim() ?? '';
  return articles.map((article) => ({
    elementId: article.id,
    buttons: Array.from(article.querySelectorAll('button'))
      .filter((button) => button.getClientRects().length > 0)
      .map(text),
    statuses: Array.from(article.querySelectorAll('[role="status"]')).map(text),
    text: text(article),
  }));
}

/** The cards the discrepancy group has mounted, in document order. */
function renderedResolvers(group: Locator): Promise<RenderedResolver[]> {
  return group.locator('article[id^="item-"]').evaluateAll(snapshotResolvers);
}

/**
 * The discrepancy group's own card list (`DiscrepancyGroupBody`): its C0 cards, then its open
 * cards.
 */
function discrepancyCardList(recommendations: RecommendationsPage): Item[] {
  const group = sectionOf(recommendations, 'campaigns').groups.find(
    (candidate) => candidate.key === DISCREPANCY_GROUP_KEY,
  );
  const items = group?.items ?? [];
  return [
    ...items.filter((item) => item.rule_id === DISCREPANCY_RULE_ID && !isHidden(item)),
    ...items.filter((item) => item.rule_id !== DISCREPANCY_RULE_ID && isOpen(item)),
  ];
}

/**
 * A C0 card's comparison line (`comparisonText` in `CampaignsSection.tsx`), both sides as
 * `discrepancyRef` reads them; `null` for an item that names no discrepancy.
 */
function comparisonLine(item: Item): string | null {
  const ref = discrepancyRef(item);
  return ref === null ? null : `Platform shows ${ref.platform}, last decided ${ref.decided}`;
}

/**
 * Checks one C0 card's footer (AAP 0.5.4.2, 0.5.4.4, 0.5.5): never a feedback action or
 * "Record decision"; with `resolutions` known, exactly one resolver button per resolution, in
 * the API's order; otherwise (a real discrepancy this spec cannot fix the state of), at least
 * one resolver button, or "Resolved since this run".
 */
function expectResolver(
  card: RenderedResolver,
  resolutions: readonly Resolution[] | undefined,
  where: string,
): void {
  const feedback = new Set([...Object.values(FEEDBACK_BUTTONS), RECORD_DECISION_LABEL]);
  expect(
    card.buttons.filter((name) => feedback.has(name)),
    `${where} offers no feedback action and no decision`,
  ).toEqual([]);
  const offered = card.buttons.filter((name) => RESOLVER_LABELS.has(name));
  if (resolutions !== undefined) {
    expect(offered, `${where} offers exactly its discrepancy's resolutions, in order`).toEqual(
      resolutions.map((resolution) => RESOLUTION_BUTTONS[resolution]),
    );
    return;
  }
  expect(
    offered.length > 0 || card.statuses.includes(RESOLVED_SINCE_RUN),
    `${where} offers its resolutions or reports "${RESOLVED_SINCE_RUN}"`,
  ).toBe(true);
}

/**
 * Checks every C0 card of the API page `expected`: with every batch of the discrepancy group
 * revealed, its cards start with exactly the page's C0 items, in API order, and each shows its
 * comparison line and {@link expectResolver}'s footer. The cards read their discrepancies after
 * they mount, so the read is retried briefly.
 *
 * @param resolutions - the resolutions each routed C0 item's discrepancy lists, by item id
 * @returns how many C0 cards it checked
 */
async function expectDiscrepancyCards(
  page: Page,
  expected: RecommendationsPage,
  resolutions: ReadonlyMap<number, readonly Resolution[]>,
): Promise<number> {
  const c0 = discrepancyItems(expected).filter((item) => !isHidden(item));
  const group = page.locator(DISCREPANCY_GROUP_SELECTOR);
  await expect(group, 'the discrepancy group is shown').toBeVisible({ timeout: READ_TIMEOUT_MS });
  await revealAll(group);
  await expect(async () => {
    const cards = await renderedResolvers(group);
    expect(
      cards.slice(0, c0.length).map((card) => cardItemId(card.elementId)),
      `the discrepancy group of run ${expected.run.id} shows every C0 card first, in API order`,
    ).toEqual(c0.map((item) => item.id));
    c0.forEach((item, index) => {
      const card = cards[index];
      const where = `C0 card item-${item.id} (run ${expected.run.id})`;
      if (card === undefined) {
        throw new Error(`${where} is not shown`);
      }
      const line = comparisonLine(item);
      if (line === null) {
        throw new Error(`${where} names no discrepancy (extra.discrepancy_id)`);
      }
      expect(card.text, `${where} compares the platform with the decision`).toContain(line);
      expectResolver(card, resolutions.get(item.id), where);
    });
  }).toPass({ intervals: [250, 500, 1_000], timeout: READ_TIMEOUT_MS });
  return c0.length;
}

/** How many C0 cards the boundary copy adds beyond one {@link CARD_BATCH_SIZE} batch. */
const BOUNDARY_EXTRA = 5;

/** How many C0 cards and discrepancies the boundary copy adds. */
const BOUNDARY_COUNT = CARD_BATCH_SIZE + BOUNDARY_EXTRA;

/**
 * The first id of the boundary copy's discrepancies and subjects: far above any id a test stack
 * issues, so none matches a real one.
 */
const BOUNDARY_ID_BASE = 1_000_000_000;

/** The kinds the boundary discrepancies cycle through, so every resolution set is shown. */
const BOUNDARY_KINDS: readonly DiscrepancyKind[] = ['status', 'budget', 'untracked', 'missing'];

/**
 * The resolutions `GET /discrepancies` lists for each kind of an own-budget campaign
 * (`db.models.decisions.DISCREPANCY_RESOLUTIONS`, in `RESOLUTION_ORDER`).
 */
const BOUNDARY_RESOLUTIONS: Readonly<Record<DiscrepancyKind, readonly Resolution[]>> = {
  status: ['platform_correct', 'decision_correct'],
  budget: ['platform_correct', 'decision_correct'],
  untracked: ['adopt_platform', 'record_intended'],
  missing: ['decision_correct', 'acknowledge_removal'],
};

/** An own-budget campaign state at `daily` USD a day, as `DiscrepancyValue` carries it. */
function campaignState(status: 'active' | 'paused', daily: string): DiscrepancyValue {
  return {
    status,
    daily_budget: daily,
    total_budget: null,
    currency: 'USD',
    amount_micros: Math.round(Number(daily) * 1_000_000),
  };
}

/**
 * The boundary discrepancy `index`, with sides fitting its kind, and the texts the backend's
 * `render_value` renders for them, which the `/decisions` resolver words alike.
 */
function boundaryDiscrepancy(index: number): {
  discrepancy: Discrepancy;
  decided: string;
  platform: string;
} {
  const kind = BOUNDARY_KINDS[index % BOUNDARY_KINDS.length] ?? 'status';
  const id = BOUNDARY_ID_BASE + index;
  const sides: Record<
    DiscrepancyKind,
    { decided: DiscrepancyValue | null; platform: DiscrepancyValue | null; texts: [string, string] }
  > = {
    status: {
      decided: { status: 'active' },
      platform: { status: 'paused' },
      texts: ['Active', 'Paused'],
    },
    budget: {
      decided: campaignState('active', '50.00'),
      platform: campaignState('active', '75.00'),
      texts: ['daily budget USD 50.00', 'daily budget USD 75.00'],
    },
    untracked: {
      decided: null,
      platform: campaignState('active', '50.00'),
      texts: ['no decision recorded', 'Active · daily budget USD 50.00'],
    },
    missing: {
      decided: campaignState('active', '50.00'),
      platform: null,
      texts: ['Active · daily budget USD 50.00', 'not returned by the platform'],
    },
  };
  const side = sides[kind];
  return {
    discrepancy: {
      id,
      subject: {
        type: 'campaign',
        id,
        name: `E2E boundary campaign ${index + 1}`,
        source: 'google',
        currency: 'USD',
      },
      shape: 'own_budget',
      kind,
      decided_value: side.decided,
      platform_value: side.platform,
      // Oldest first, as the API lists them.
      detected_at: new Date(Date.UTC(2026, 0, 5, 9, index)).toISOString(),
      state: 'open',
      resolved_at: null,
      resolution: null,
      resolutions: [...BOUNDARY_RESOLUTIONS[kind]],
    },
    decided: side.texts[0],
    platform: side.texts[1],
  };
}

/**
 * The C0 item the run would hold for a boundary discrepancy (`discrepancy._candidate`): a
 * campaign card that compares the two sides, with its `extra` naming the discrepancy, no
 * decision subject, never held or hidden. Everything else (window, flags, data status) is the
 * template's, so it reads as the run's own.
 */
function boundaryItem(template: Item, id: number, rank: number, index: number): Item {
  const { discrepancy, decided, platform } = boundaryDiscrepancy(index);
  const item = structuredClone(template);
  const text = (key: string, label: string, value: string): Metric => ({
    key,
    label,
    value,
    unit: 'status',
    currency: null,
    floor: null,
    unavailable: false,
  });
  return {
    ...item,
    id,
    rank,
    rule_id: DISCREPANCY_RULE_ID,
    action: `Resolve: platform shows ${platform}, last decided ${decided}`,
    reason: 'The platform state differs from the last decided state.',
    entity: {
      source: discrepancy.subject.source,
      type: 'campaign',
      id: String(discrepancy.subject.id),
      name: discrepancy.subject.name,
    },
    evidence: {
      ...item.evidence,
      metrics: [text('platform', 'Platform', platform), text('decided', 'Last decided', decided)],
      baselines: [],
      sample: null,
      test: null,
      extra: {
        discrepancy_id: discrepancy.id,
        kind: discrepancy.kind,
        shape: discrepancy.shape,
        state: discrepancy.state,
        detected_at: discrepancy.detected_at,
        decided: discrepancy.decided_value,
        platform: discrepancy.platform_value,
        resolutions: [...discrepancy.resolutions],
      },
    },
    held: false,
    held_reason: null,
    decision_subject: null,
    current: {
      ...item.current,
      held_now: false,
      held_reason_now: null,
      historical: false,
      hidden_by: null,
    },
  };
}

/** The boundary copy {@link withDiscrepancyBoundary} built. */
interface BoundaryPage {
  page: RecommendationsPage;
  /** The resolutions of each added C0 item's discrepancy, by item id. */
  resolutions: Map<number, readonly Resolution[]>;
}

/**
 * A copy of the page whose discrepancy group gains {@link BOUNDARY_COUNT} C0 items after its
 * own, more than one {@link CARD_BATCH_SIZE} batch, each naming one boundary discrepancy that
 * {@link serveBoundaryDiscrepancies} lists as open, with ids above every item of the page.
 */
function withDiscrepancyBoundary(source: RecommendationsPage): BoundaryPage {
  const page = structuredClone(source);
  const campaigns = sectionOf(page, 'campaigns');
  let group = campaigns.groups.find((candidate) => candidate.key === DISCREPANCY_GROUP_KEY);
  if (group === undefined) {
    group = { key: DISCREPANCY_GROUP_KEY, title: 'Discrepancies', items: [] };
    campaigns.groups.unshift(group);
  }
  const everyItem = page.sections.flatMap(allItems);
  const template = allItems(campaigns)[0] ?? everyItem[0];
  if (template === undefined) {
    throw new Error(`Run ${page.run.id} has no item to model the boundary C0 items on`);
  }
  const firstId = Math.max(0, ...everyItem.map((item) => item.id)) + 1;
  const own = group.items.filter((item) => item.rule_id === DISCREPANCY_RULE_ID).length;
  const resolutions = new Map<number, readonly Resolution[]>();
  const added = Array.from({ length: BOUNDARY_COUNT }, (_, index) => {
    const item = boundaryItem(template, firstId + index, own + index + 1, index);
    resolutions.set(item.id, boundaryDiscrepancy(index).discrepancy.resolutions);
    return item;
  });
  group.items.splice(own, 0, ...added);
  page.overlay.open_discrepancies += added.length;
  return { page, resolutions };
}

/**
 * Serves the browser, until `page.unrouteAll`, the API's first page of each unresolved
 * `GET /discrepancies` read with the boundary discrepancies appended and `X-Total-Count` raised
 * to match. Later pages (`after`) and the resolved list pass through unchanged.
 */
async function serveBoundaryDiscrepancies(page: Page): Promise<void> {
  const added = Array.from(
    { length: BOUNDARY_COUNT },
    (_, index) => boundaryDiscrepancy(index).discrepancy,
  );
  await page.route(
    (url) => url.pathname === DISCREPANCIES_PATH,
    async (route: Route) => {
      const url = new URL(route.request().url());
      if (
        route.request().method() !== 'GET' ||
        url.searchParams.has('after') ||
        url.searchParams.getAll('state').includes('resolved')
      ) {
        await route.fallback();
        return;
      }
      const response = await route.fetch();
      if (!response.ok()) {
        await route.fulfill({ response });
        return;
      }
      const listed = (await response.json()) as Discrepancy[];
      const real = new Set(listed.map((discrepancy) => discrepancy.id));
      const fresh = added.filter((discrepancy) => !real.has(discrepancy.id));
      await route.fulfill({
        status: response.status(),
        headers: editedHeaders(response, fresh.length),
        json: [...listed, ...fresh],
      });
    },
  );
}

// ---------------------------------------------------------------------------------------------
// Feedback target
// ---------------------------------------------------------------------------------------------

/** The item the feedback test snoozes, with where it sits on the page. */
interface FeedbackTarget {
  sectionKey: SectionKey;
  /** The title of the item's group: the keyword tab to select. */
  groupTitle: string;
  item: Item;
  /** The run the item belongs to. */
  runId: number;
}

/** A target after its Snooze button was clicked. */
interface SubmittedFeedback {
  target: FeedbackTarget;
  /** What disappears once the item is hidden: its card, or its white-paper row toggle. */
  gone: Locator;
  /** The `feedback_snooze` count the target's run stored for its section, before this snooze. */
  baseline: number;
}

/** The on-page controls of a revealed target. */
interface RevealedTarget {
  /** The element holding the target's footer actions. */
  actions: Locator;
  /** What disappears once the item is hidden. */
  gone: Locator;
}

/**
 * Whether feedback may be given on the item from its card: not a discrepancy (C0 is exempt),
 * no decision subject (so `decisions.spec.ts` keeps its Record-decision candidates), not
 * historical, not held and not already hidden.
 */
function acceptsFeedback(item: Item): boolean {
  return (
    item.rule_id !== DISCREPANCY_RULE_ID &&
    item.decision_subject === null &&
    !item.current.historical &&
    !isHeld(item) &&
    !isHidden(item)
  );
}

/** Whether no other shown white-paper row carries the item's name, so its toggle is unique. */
function hasUniqueAssetName(section: Section, item: Item): boolean {
  const name = assetName(item);
  return (
    visibleItems(section).filter((other) => !isHeld(other) && assetName(other) === name)
      .length === 1
  );
}

/**
 * Whether the item of `section` may be the feedback target: it accepts feedback, and a
 * white-paper row's name is unique so its toggle can be found.
 */
function qualifiesForFeedback(section: Section, item: Item): boolean {
  return (
    acceptsFeedback(item) && (section.key !== 'white_papers' || hasUniqueAssetName(section, item))
  );
}

/** The first item of a section that accepts feedback, as a target. */
function firstTarget(section: Section, runId: number): FeedbackTarget | null {
  for (const group of section.groups) {
    for (const item of group.items) {
      if (qualifiesForFeedback(section, item)) {
        return {
          sectionKey: section.key,
          groupTitle: groupTitle(group.title, group.key),
          item,
          runId,
        };
      }
    }
  }
  return null;
}

/**
 * Chooses the feedback target among sections that show at least two items, so the section
 * stays populated after the snooze: a keyword item when one qualifies (keyword items never
 * carry a decision subject), otherwise the qualifying item of the section showing most items.
 */
function chooseFeedbackTarget(recommendations: RecommendationsPage): FeedbackTarget | null {
  const options = recommendations.sections
    .filter((section) => visibleItems(section).length >= 2)
    .map((section) => ({ section, target: firstTarget(section, recommendations.run.id) }))
    .filter((option) => option.target !== null);
  const keywords = options.find((option) => option.section.key === 'keywords');
  const busiest = options.toSorted(
    (left, right) => visibleItems(right.section).length - visibleItems(left.section).length,
  )[0];
  return (keywords ?? busiest)?.target ?? null;
}

/**
 * Why `chooseFeedbackTarget` found no target: the condition it applies and, per section, how
 * many items the section shows and how many of them qualify.
 */
function noFeedbackTargetMessage(recommendations: RecommendationsPage): string {
  const counts = recommendations.sections.map((section) => {
    const qualifying = allItems(section).filter((item) => qualifiesForFeedback(section, item));
    return `${section.key}: ${visibleItems(section).length} shown, ${qualifying.length} qualifying`;
  });
  return (
    `Run ${recommendations.run.id} offers no snooze target: the test needs a section showing at ` +
    'least two items, one of them open to feedback (not C0, no decision subject, not historical, ' +
    `held or hidden; a white-paper row with a unique name). ${counts.join('; ')}`
  );
}

/**
 * Makes the target's actions visible: selects its keyword tab, or expands its white-paper row;
 * other sections show their cards directly.
 */
async function revealTarget(page: Page, target: FeedbackTarget): Promise<RevealedTarget> {
  const region = sectionRegion(page, target.sectionKey);
  if (target.sectionKey === 'white_papers') {
    // A target is open, so its row sits in its cohort's table of visible rows; the pattern
    // matches the toggle in either state, so the locator still resolves once it is expanded.
    const currency = itemCurrency(target.item, target.groupTitle);
    const toggle = region.getByRole('button', {
      name: detailsTogglePattern(assetName(target.item), currency),
    });
    await expect(toggle).toBeVisible({ timeout: READ_TIMEOUT_MS });
    const detailId = await toggle.getAttribute('aria-controls', { timeout: READ_TIMEOUT_MS });
    if (detailId === null) {
      throw new Error(`The row toggle of "${assetName(target.item)}" controls no detail row`);
    }
    if ((await toggle.getAttribute('aria-expanded')) !== 'true') {
      await toggle.click({ timeout: READ_TIMEOUT_MS });
    }
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const detail = page.locator(attributeSelector('id', detailId));
    await expect(detail).toBeVisible({ timeout: READ_TIMEOUT_MS });
    return { actions: detail, gone: toggle };
  }
  if (target.sectionKey === 'keywords') {
    const tab = page
      .getByRole('tablist', { name: KEYWORD_TABS_LABEL, exact: true })
      .getByRole('tab', {
        name: new RegExp(`^${escapeRegExp(target.groupTitle)}(?: \\d[\\d,]*)?$`, 'u'),
      });
    await tab.click({ timeout: READ_TIMEOUT_MS });
    await expect(tab).toHaveAttribute('aria-selected', 'true');
  }
  const card = region.locator(`#item-${target.item.id}`);
  await expect(card).toBeVisible({ timeout: READ_TIMEOUT_MS });
  return { actions: card, gone: page.locator(`#item-${target.item.id}`) };
}

/** Whether two items name the same rule and entity: what feedback suppresses in later runs. */
function sameRuleAndEntity(left: Item, right: Item): boolean {
  return (
    left.rule_id === right.rule_id &&
    left.entity.type === right.entity.type &&
    left.entity.id === right.entity.id
  );
}

/** The campaigns section's discrepancy (C0) items. */
function discrepancyItems(recommendations: RecommendationsPage): Item[] {
  return allItems(sectionOf(recommendations, 'campaigns')).filter(
    (item) => item.rule_id === DISCREPANCY_RULE_ID,
  );
}

/** Adds a note to the test's report, for a check the current data cannot exercise. */
function note(description: string): void {
  test.info().annotations.push({ type: 'note', description });
}

// ---------------------------------------------------------------------------------------------
// Restoring the snooze through the stack's database (the API cannot withdraw feedback)
// ---------------------------------------------------------------------------------------------

/**
 * The repository root, resolved from this file (`web/tests/e2e/`): the working directory of
 * every database command, so the default `docker compose` finds the stack's Compose file.
 */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

/** Starts the one result line of a spec query, telling it apart from anything else printed. */
const DATABASE_RESULT_MARKER = 'e2e-result:';

/** How much of a command's output an error message quotes. */
const OUTPUT_EXCERPT_CHARS = 2_000;

/** Starts the `note` of every snooze this spec stores; a random UUID per attempt follows it. */
const SNOOZE_TOKEN_PREFIX = 'e2e-actions-snooze:';

/**
 * A snooze token as {@link snoozeToken} writes it, matched in full: the prefix and one
 * lower-case UUID. Only a match reaches SQL.
 */
const SNOOZE_TOKEN = new RegExp(
  `^${escapeRegExp(SNOOZE_TOKEN_PREFIX)}` +
    '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
  'u',
);

/** The command that runs `psql` on the stack's database, reading SQL on stdin. */
interface DatabaseCommand {
  /** The shell command line. */
  command: string;
  /** Where it comes from, for messages: never `E2E_PSQL` itself, which may hold a password. */
  source: string;
}

/** A snooze the running test may have stored, and how its teardown finds the row again. */
interface OwnedSnooze {
  target: FeedbackTarget;
  /**
   * The attempt's own {@link snoozeToken}, set before the click: the routed request stores it
   * as the row's `note`, so the teardown finds this attempt's row, and no other, without any
   * response.
   */
  token: string;
  /** The stored row's id, from the 201 response; unset while the response is unknown. */
  feedbackId?: number;
  /**
   * Settles once the routed request forwarded to the API has been answered or has failed;
   * unset until the route forwards it. The teardown waits for it, so its delete never runs
   * ahead of the API's own write.
   */
  forwarded?: Promise<void>;
}

/**
 * The snoozes the running test may have stored, each registered before it is sent. The
 * `afterEach` hook restores and clears them after every test and retry, passed or failed.
 */
const ownedSnoozes: OwnedSnooze[] = [];

/**
 * The stack's database command: `E2E_PSQL` when set, else `psql` in the Compose `db` service
 * through `E2E_COMPOSE` (default `docker compose`), as the service's own `POSTGRES_USER` on its
 * `POSTGRES_DB`.
 */
function databaseCommand(): DatabaseCommand {
  const psql = process.env.E2E_PSQL?.trim() ?? '';
  if (psql !== '') {
    return { command: psql, source: 'E2E_PSQL' };
  }
  const configured = process.env.E2E_COMPOSE?.trim() ?? '';
  const compose = configured === '' ? 'docker compose' : configured;
  return {
    command:
      `${compose} exec -T db sh -c ` +
      `'exec psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`,
    source: `"${compose} exec -T db psql"`,
  };
}

/** `text` trimmed and cut to {@link OUTPUT_EXCERPT_CHARS} characters, for an error message. */
function excerpt(text: string): string {
  const trimmed = text.trim();
  if (trimmed === '') {
    return '(no output)';
  }
  return trimmed.length > OUTPUT_EXCERPT_CHARS
    ? `${trimmed.slice(0, OUTPUT_EXCERPT_CHARS)}…`
    : trimmed;
}

/** The message of a thrown value. */
function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Runs a psql script on the stack's database and resolves its standard output. The command runs
 * through `sh -c` from the repository root, in its own process group, with the script on stdin.
 * A failed start, a non-zero exit, an unread script or a run longer than
 * {@link DATABASE_COMMAND_TIMEOUT_MS} (which kills the group) rejects, quoting the command's
 * standard error.
 */
function runDatabaseScript(script: string): Promise<string> {
  const { command, source } = databaseCommand();
  return new Promise<string>((resolve, reject) => {
    const child = spawn('sh', ['-c', command], {
      cwd: REPOSITORY_ROOT,
      detached: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    const errorOutput = (): string => excerpt(Buffer.concat(stderr).toString('utf8'));
    let inputError: Error | undefined;
    let settled = false;
    // Called only from events and the timer, which fire after `timer` below is initialised.
    const settle = (error: Error | null): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (error === null) {
        resolve(Buffer.concat(stdout).toString('utf8'));
      } else {
        reject(error);
      }
    };
    const timer = setTimeout(() => {
      // The whole group: `sh` and the docker or psql client it started.
      try {
        if (child.pid !== undefined) {
          process.kill(-child.pid, 'SIGKILL');
        }
      } catch {
        child.kill('SIGKILL');
      }
      settle(
        new Error(
          `${source} did not finish within ${DATABASE_COMMAND_TIMEOUT_MS} ms; stderr: ` +
            errorOutput(),
        ),
      );
    }, DATABASE_COMMAND_TIMEOUT_MS);
    child.stdout.on('data', (chunk: Buffer) => stdout.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => stderr.push(chunk));
    // A command that exits without reading its input breaks the pipe; the close reports it.
    child.stdin.on('error', (error) => {
      inputError = error;
    });
    child.on('error', (error) => {
      settle(new Error(`${source} could not be started: ${error.message}`));
    });
    child.on('close', (code, signal) => {
      if (code !== 0) {
        const status = code === null ? `signal ${String(signal)}` : `exit status ${code}`;
        settle(new Error(`${source} failed with ${status}; stderr: ${errorOutput()}`));
      } else if (inputError !== undefined) {
        settle(
          new Error(
            `${source} exited before reading the whole script (${inputError.message}); ` +
              `stderr: ${errorOutput()}`,
          ),
        );
      } else {
        settle(null);
      }
    });
    child.stdin.end(script);
  });
}

/**
 * Runs one SQL statement whose single result row is `'e2e-result:' || <json>` and returns the
 * parsed JSON. psql prints tuples only, unaligned, so the row is one line of the output.
 */
async function queryDatabase(statement: string): Promise<unknown> {
  const output = await runDatabaseScript(
    `\\pset tuples_only on\n\\pset format unaligned\n${statement}\n`,
  );
  const line = output.split('\n').find((candidate) => candidate.startsWith(DATABASE_RESULT_MARKER));
  if (line === undefined) {
    throw new Error(
      `${databaseCommand().source} printed no "${DATABASE_RESULT_MARKER}" line: ${excerpt(output)}`,
    );
  }
  return JSON.parse(line.slice(DATABASE_RESULT_MARKER.length)) as unknown;
}

/** The SQL statement that prints `json` (a SQL expression) as the result line. */
function resultStatement(json: string): string {
  return `SELECT '${DATABASE_RESULT_MARKER}' || (${json})::text;`;
}

/** A positive safe integer as SQL text; anything else is refused before it reaches SQL. */
function sqlId(value: unknown, what: string): string {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`The ${what} ${String(value)} is not a positive safe integer`);
  }
  return String(value);
}

/** A fresh snooze token: {@link SNOOZE_TOKEN_PREFIX} and a random UUID, unique to one attempt. */
function snoozeToken(): string {
  return `${SNOOZE_TOKEN_PREFIX}${randomUUID()}`;
}

/** A snooze token as a SQL literal; anything but a {@link SNOOZE_TOKEN} match is refused. */
function sqlSnoozeToken(token: string): string {
  if (!SNOOZE_TOKEN.test(token)) {
    throw new Error(`"${token}" is not a snooze token of this spec`);
  }
  return `'${token}'`;
}

/** A feedback kind as a SQL literal; anything but the three kinds is refused. */
function sqlFeedbackKind(kind: FeedbackKind): string {
  if (!Object.hasOwn(SUPPRESSION_LABELS, kind)) {
    throw new Error(`"${String(kind)}" is not a feedback kind`);
  }
  return `'${kind}'`;
}

/**
 * Proves, before anything is written, that the database command reaches this stack's database:
 * the API's run must be stored there with the same kind, status and finish instant. A wrong
 * `E2E_PSQL`, Compose project or database therefore fails the test before any snooze is sent.
 */
async function expectDatabaseOfStack(run: Run): Promise<void> {
  const { source } = databaseCommand();
  const result = await queryDatabase(
    resultStatement(
      "json_build_object('run', (SELECT json_build_object('kind', kind, 'status', status, " +
        `'finished_at', finished_at) FROM analysis_run WHERE id = ${sqlId(run.id, 'run id')}))`,
    ),
  );
  const stored = isRecord(result) && isRecord(result.run) ? result.run : null;
  const finished = run.finished_at === null ? Number.NaN : Date.parse(run.finished_at);
  const storedFinished =
    typeof stored?.finished_at === 'string' ? Date.parse(stored.finished_at) : Number.NaN;
  if (
    stored === null ||
    stored.kind !== run.kind ||
    stored.status !== run.status ||
    Number.isNaN(finished) ||
    storedFinished !== finished
  ) {
    throw new Error(
      `${source} does not reach the database of the stack under test: the API's run ${run.id} ` +
        `(${run.kind}, ${run.status}, finished ${String(run.finished_at)}) is ` +
        `${stored === null ? 'not stored there' : `stored there as ${JSON.stringify(stored)}`}. ` +
        'Point E2E_PSQL, or the -p/-f flags of E2E_COMPOSE, at this stack.',
    );
  }
}

/**
 * Proves the target is this stack's item: the recommendation row the API named, with its run and
 * rule.
 */
async function preflightTarget(target: FeedbackTarget): Promise<void> {
  const { source } = databaseCommand();
  const result = await queryDatabase(
    resultStatement(
      "json_build_object('item', (SELECT json_build_object('run_id', run_id, " +
        `'rule_id', rule_id) FROM recommendation WHERE id = ${sqlId(target.item.id, 'item id')}))`,
    ),
  );
  const stored = isRecord(result) && isRecord(result.item) ? result.item : null;
  if (stored === null || stored.run_id !== target.runId || stored.rule_id !== target.item.rule_id) {
    throw new Error(
      `${source} does not hold item ${target.item.id} of run ${target.runId} ` +
        `(${target.item.rule_id}) as the API reports it: ` +
        `${stored === null ? 'no such row' : JSON.stringify(stored)}`,
    );
  }
}

/**
 * The stored feedback's id from a 201 body, checked against the item, kind and token it was
 * sent with.
 */
function storedFeedbackId(body: unknown, owned: OwnedSnooze): number {
  const { target } = owned;
  const id = isRecord(body) ? body.id : undefined;
  if (
    !isRecord(body) ||
    typeof id !== 'number' ||
    !Number.isSafeInteger(id) ||
    id <= 0 ||
    body.recommendation_id !== target.item.id ||
    body.kind !== FEEDBACK_KIND ||
    body.note !== owned.token
  ) {
    throw new Error(
      `The 201 body of the ${FEEDBACK_KIND} of item ${target.item.id} is not its Feedback ` +
        `noted ${owned.token}: ${excerpt(JSON.stringify(body))}`,
    );
  }
  return id;
}

/** The path of the target's feedback endpoint, `POST /recommendations/{id}/feedback`. */
function feedbackPath(target: FeedbackTarget): string {
  return `${RECOMMENDATIONS_PATH}/${target.item.id}/feedback`;
}

/**
 * Why a feedback request body is not the UI's snooze, `{"kind": "snooze"}` with no note or other
 * field; `null` when it is.
 */
function uiSnoozeBodyDefect(postData: string | null): string | null {
  if (postData === null) {
    return 'it has no body';
  }
  let body: unknown;
  try {
    body = JSON.parse(postData);
  } catch {
    return `its body is not JSON: ${excerpt(postData)}`;
  }
  if (!isRecord(body) || Object.keys(body).length !== 1 || body.kind !== FEEDBACK_KIND) {
    return `its body ${excerpt(postData)} is not {"kind":"${FEEDBACK_KIND}"} alone`;
  }
  return null;
}

/** The route of one attempt's snooze request, from {@link routeOwnedSnooze}. */
interface RoutedSnooze {
  /** What the route refused or could not do; empty while the UI's own snooze went through. */
  failures: string[];
  /** Removes the route; a request it already holds is still answered. */
  remove: () => Promise<void>;
}

/**
 * Routes the target's `POST /recommendations/{id}/feedback` for one attempt. The UI's own body
 * must be `{"kind": "snooze"}` alone; the route forwards it to the API with every original header
 * and the attempt's {@link OwnedSnooze.token} as its `note`, so the stored row names its owner,
 * reads the 201 into {@link OwnedSnooze.feedbackId} even when the browser's response is lost, and
 * serves the API's own response to the browser. Any other body, or a second post, is recorded in
 * {@link RoutedSnooze.failures} and aborted unsent, so the spec never stores a row it cannot find.
 */
async function routeOwnedSnooze(page: Page, owned: OwnedSnooze): Promise<RoutedSnooze> {
  const path = feedbackPath(owned.target);
  const item = `item ${owned.target.item.id}`;
  const failures: string[] = [];
  let posts = 0;
  const matcher = (url: URL): boolean => url.pathname === path;
  const handler = async (route: Route): Promise<void> => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    posts += 1;
    const defect =
      posts > 1 ? 'the UI posted it again' : uiSnoozeBodyDefect(route.request().postData());
    if (defect !== null) {
      failures.push(`The ${FEEDBACK_KIND} of ${item} was not sent to the API: ${defect}`);
      await route.abort('failed');
      return;
    }
    const forward = route.fetch({
      postData: JSON.stringify({ kind: FEEDBACK_KIND, note: owned.token }),
      timeout: CARD_REMOVAL_TIMEOUT_MS,
    });
    owned.forwarded = forward.then(
      () => undefined,
      () => undefined,
    );
    let response: APIResponse;
    try {
      response = await forward;
    } catch (error) {
      failures.push(`Forwarding the ${FEEDBACK_KIND} of ${item} failed: ${errorMessage(error)}`);
      await route.abort('failed');
      return;
    }
    if (response.status() === 201) {
      try {
        owned.feedbackId = storedFeedbackId(await response.json(), owned);
      } catch (error) {
        failures.push(errorMessage(error));
      }
    }
    await route.fulfill({ response });
  };
  await page.route(matcher, handler);
  return { failures, remove: () => page.unroute(matcher, handler) };
}

/**
 * Undoes one owned snooze. Once its forwarded request has been answered or has failed, one
 * statement, so one transaction, deletes that snooze's row and nothing else: the target's
 * snooze whose `note` is the attempt's {@link OwnedSnooze.token}, which no other row carries,
 * and, when the response named it, whose id is {@link OwnedSnooze.feedbackId}. It queues one
 * `analysis_request` (`e2e_restore`), as the feedback write queued its own, so the next default
 * run is computed without the suppression. It then waits until a run that claimed that request,
 * or a later one, is the latest default run and shows the target's rule and entity unhidden. A
 * snooze that was never stored restores nothing.
 */
async function restoreSnooze(page: Page, owned: OwnedSnooze): Promise<void> {
  if (owned.forwarded !== undefined) {
    await owned.forwarded;
  }
  const { target } = owned;
  const subject = `${target.item.rule_id} ${String(target.item.entity.id)}`;
  const condition =
    `recommendation_id = ${sqlId(target.item.id, 'item id')} ` +
    `AND kind = ${sqlFeedbackKind(FEEDBACK_KIND)} AND note = ${sqlSnoozeToken(owned.token)}` +
    (owned.feedbackId === undefined ? '' : ` AND id = ${sqlId(owned.feedbackId, 'feedback id')}`);
  let result: unknown;
  try {
    result = await queryDatabase(
      `WITH removed AS (DELETE FROM recommendation_feedback WHERE ${condition} RETURNING id), ` +
        'requested AS (INSERT INTO analysis_request (requested_at, reason) ' +
        "SELECT now(), 'e2e_restore' WHERE EXISTS (SELECT 1 FROM removed) RETURNING id) " +
        resultStatement(
          "json_build_object('removed', (SELECT count(*) FROM removed), " +
            "'request', (SELECT id FROM requested))",
        ),
    );
  } catch (error) {
    throw new Error(
      `The ${FEEDBACK_KIND} of item ${target.item.id} (${subject}) may still be stored; delete ` +
        `it with "DELETE FROM recommendation_feedback WHERE ${condition}": ${errorMessage(error)}`,
      { cause: error },
    );
  }
  const request = isRecord(result) ? result.request : undefined;
  if (request === null) {
    return;
  }
  const requestId = sqlId(request, 'restoring analysis request id');
  const restoring =
    `restoring the ${FEEDBACK_KIND} of item ${target.item.id} (${subject}): the latest default ` +
    'run must be computed after the restore and show it unhidden';
  await expect(async () => {
    const served = await queryDatabase(
      resultStatement(
        "json_build_object('run_id', (SELECT run_id FROM analysis_request " +
          `WHERE id = ${requestId}))`,
      ),
    );
    const rerun = isRecord(served) ? served.run_id : undefined;
    if (typeof rerun !== 'number') {
      throw new Error(`The restoring analysis request ${requestId} is not served yet`);
    }
    const recommendations = await fetchRecommendations(page);
    expect(
      recommendations.run.id,
      `the latest default run is run ${rerun}, which served the restore, or a later one`,
    ).toBeGreaterThanOrEqual(rerun);
    expect(
      allItems(sectionOf(recommendations, target.sectionKey)).some(
        (item) => !isHidden(item) && sameRuleAndEntity(item, target.item),
      ),
      `run ${recommendations.run.id} shows ${subject} unhidden again`,
    ).toBe(true);
  }, restoring).toPass(RESTORE_RETRY);
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

test.describe('actions page', () => {
  test.describe.configure({ timeout: TEST_TIMEOUT_MS });

  test('shows the five sections in order, with navigation counts matching the API', async ({
    page,
  }) => {
    await openActions(page);
    await expectSectionsLoaded(page);
    const recommendations = await fetchRecommendations(page);
    expect(
      recommendations.sections.map((section) => section.key),
      'the API returns the five sections in the fixed order',
    ).toEqual(SECTION_KEYS);

    const rendered = await page.locator('section[id^="section-"]').evaluateAll((elements) =>
      elements.map((element) => {
        const headingId = element.getAttribute('aria-labelledby');
        const heading = headingId === null ? null : document.getElementById(headingId);
        return { id: element.id, title: heading?.textContent?.trim() ?? '' };
      }),
    );
    expect(
      rendered.map((section) => section.id),
      'the page renders the five sections in the fixed order',
    ).toEqual(SECTION_KEYS.map((key) => `section-${key}`));
    expect(
      rendered.map((section) => section.title),
      'each section is titled as the API titles it',
    ).toEqual(recommendations.sections.map((section) => section.title.trim()));
    for (const section of recommendations.sections) {
      // Scoped by id: the region named by the title must be the section itself. A group whose
      // title repeats its section's renders no region of its own, so this region is unique.
      await expect(
        page
          .getByRole('region', { name: section.title.trim(), exact: true })
          .and(sectionRegion(page, section.key)),
        `the "${section.key}" section is a region named by its title`,
      ).toBeVisible();
    }

    const links = await sectionNav(page)
      .locator('a[href^="#section-"]')
      .evaluateAll((elements) => elements.map((element) => element.getAttribute('href')));
    expect(links, 'the section navigation links the sections in the fixed order').toEqual(
      SECTION_KEYS.map((key) => `#section-${key}`),
    );

    await expect(async () => {
      await reloadActions(page);
      const fresh = await fetchRecommendations(page);
      for (const key of SECTION_KEYS) {
        const expected = visibleItems(sectionOf(fresh, key)).length;
        expect(
          await sectionNavCount(page, key),
          `the "${key}" link counts the ${expected} items of run ${fresh.run.id} ` +
            'that feedback has not hidden',
        ).toBe(expected);
      }
    }).toPass(SETTLE);
  });

  test('cards show their evidence, window chip and flags in every section', async ({ page }) => {
    await openActions(page);

    for (const key of CARD_SECTION_KEYS) {
      await test.step(`${key}: every card, every value`, async () => {
        await expect(async () => {
          await reloadActions(page);
          const recommendations = await fetchRecommendations(page);
          expect(
            visibleItems(sectionOf(recommendations, key)).length,
            `the "${key}" section of run ${recommendations.run.id} has items`,
          ).toBeGreaterThan(0);
          await expectSectionCards(page, recommendations, key);
        }).toPass(SETTLE);
      });
    }

    await test.step('white_papers: every row of every cohort, expanded', async () => {
      await expect(async () => {
        await reloadActions(page);
        const recommendations = await fetchRecommendations(page);
        expect(
          visibleItems(sectionOf(recommendations, 'white_papers')).length,
          `the white papers of run ${recommendations.run.id} have rows`,
        ).toBeGreaterThan(0);
        await expectWhitePaperRows(page, recommendations);
      }).toPass(SETTLE);
    });

    await test.step('a routed copy: floors, unavailable values, zero denominators', async () => {
      const served = await serveEditedRecommendations(page, withRenderingEdgeCases);
      try {
        await expect(async () => {
          await reloadActions(page);
          const latest = served.latest;
          if (latest === null) {
            throw new Error('The browser has not read the routed recommendations yet');
          }
          const { card, assetItemId } = latest;
          if (card === null || assetItemId === null) {
            throw new Error(
              `Run ${latest.page.run.id} has no open card or white-paper row to edit`,
            );
          }
          // Every check of the real page holds for the edited copy, then the edits read back.
          const cards = await expectSectionCards(page, latest.page, card.sectionKey);
          expectEdgeCaseCard(cards.get(card.itemId), card.currency);
          const asset = allItems(sectionOf(latest.page, 'white_papers')).find(
            (item) => item.id === assetItemId,
          );
          if (asset === undefined) {
            throw new Error(`White paper ${assetItemId} left the routed page`);
          }
          expectEdgeCaseAsset(await expectWhitePaperRows(page, latest.page), asset);
        }).toPass(SETTLE);
      } finally {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
      }
    });
  });

  test('the window picker offers the configured presets and opens a custom run', async ({
    page,
  }) => {
    await openActions(page);
    await expectSectionsLoaded(page);
    const config = await requireJson<ConfigView>(page.request, CONFIG_PATH);
    const { presets, default_days: defaultDays } = config.window;

    const picker = page.getByRole('group', { name: WINDOW_PICKER_LABEL, exact: true });
    await expect(picker).toBeVisible();
    const names = (await picker.getByRole('button').allInnerTexts()).map((name) => name.trim());
    const shown = names.flatMap((name) => {
      const match = PRESET_BUTTON.exec(name);
      const days = match?.[1];
      if (days === undefined) {
        return [];
      }
      return [{ days: Number(days), isDefault: match?.[2] !== undefined }];
    });
    expect(
      names.filter((name) => !PRESET_BUTTON.test(name)),
      'besides the presets, the picker offers only the custom range',
    ).toEqual([CUSTOM_RANGE_LABEL]);
    expect(
      shown.map((preset) => preset.days).toSorted((left, right) => left - right),
      'the picker offers each configured preset once',
    ).toEqual([...new Set(presets)].toSorted((left, right) => left - right));
    expect(
      shown.filter((preset) => preset.isDefault).map((preset) => preset.days),
      'exactly one preset is marked default, the configured default',
    ).toEqual([defaultDays]);
    await expect(
      picker.getByRole('button', { name: `${presetLabel(defaultDays)} (default)`, exact: true }),
      'the default run selects the default preset',
    ).toHaveAttribute('aria-pressed', 'true');

    const days = presets.find((preset) => preset !== defaultDays);
    if (days === undefined) {
      note(`window.presets ${JSON.stringify(presets)} holds no non-default window to open`);
      return;
    }

    const preset = picker.getByRole('button', { name: presetLabel(days), exact: true });
    const busy = page.getByText(RUN_IN_PROGRESS_MESSAGE, { exact: true });
    await expect(async () => {
      if (CUSTOM_RUN_URL.test(page.url())) {
        return;
      }
      await preset.click({ timeout: CUSTOM_RUN_OPEN_TIMEOUT_MS });
      await expect
        .poll(
          async () => {
            if (CUSTOM_RUN_URL.test(page.url())) {
              return 'opened';
            }
            return (await busy.isVisible()) ? 'busy' : 'computing';
          },
          {
            message: `choosing ${presetLabel(days)} opens a custom run or reports the lock`,
            timeout: CUSTOM_RUN_OPEN_TIMEOUT_MS,
          },
        )
        .not.toBe('computing');
      if (!CUSTOM_RUN_URL.test(page.url())) {
        throw new Error(`${RUN_IN_PROGRESS_MESSAGE} Retrying ${presetLabel(days)}.`);
      }
    }).toPass(CUSTOM_RUN_RETRY);

    const runParam = new URL(page.url()).searchParams.get('run');
    if (runParam === null) {
      throw new Error(`${page.url()} carries no run parameter`);
    }
    const custom = await requireJson<Run>(page.request, `/api/v1/runs/${runParam}`);
    expect(custom.kind, `run ${custom.id} is a custom run`).toBe('custom');
    expect(custom.days, `run ${custom.id} spans the chosen preset`).toBe(days);
    await expectSectionsLoaded(page);
    const banner = page.locator(CUSTOM_BANNER_SELECTOR);
    await expect(banner).toContainText(
      `Custom window ${custom.window_start} – ${custom.window_end}`,
    );

    await banner.getByRole('link', { name: BACK_TO_DEFAULT_LABEL, exact: true }).click();
    await expect(page, 'Back to default opens /actions without a run').toHaveURL(
      (url) => url.pathname === ACTIONS_PATH && !url.searchParams.has('run'),
    );
    await expectSectionsLoaded(page);
    await expect(page.locator(CUSTOM_BANNER_SELECTOR)).toHaveCount(0);
  });

  // Runs after every test of the describe, passed or failed, and after every retry; only the
  // snooze test registers owned state, so for the others it does nothing.
  test.afterEach(async ({ page }) => {
    const failures: string[] = [];
    const snoozes = ownedSnoozes.splice(0);
    if (snoozes.length > 0) {
      // A test that timed out mid-click never removed its snooze route; its forwarded request
      // still settles, and the restore waits for it.
      await page.unrouteAll({ behavior: 'ignoreErrors' });
    }
    for (const owned of snoozes) {
      try {
        await restoreSnooze(page, owned);
      } catch (error) {
        failures.push(errorMessage(error));
      }
    }
    if (failures.length > 0) {
      throw new Error(`The snooze teardown did not restore the stack:\n${failures.join('\n')}`);
    }
  });

  test('snoozing a card hides it and its section reports it', async ({ page }) => {
    const run = await openActions(page);
    // Before anything is written: the teardown must be able to undo the snooze on this stack.
    await expectDatabaseOfStack(run);

    const attempt: { submitted?: SubmittedFeedback; sent?: number; failure?: unknown } = {};
    await expect(async () => {
      // Once a snooze may be stored, no other is sent; the teardown undoes the one in doubt.
      if (attempt.sent !== undefined) {
        return;
      }
      attempt.submitted = undefined;
      attempt.failure = undefined;
      await reloadActions(page);
      const recommendations = await fetchRecommendations(page);
      const target = chooseFeedbackTarget(recommendations);
      if (target === null) {
        throw new Error(noFeedbackTargetMessage(recommendations));
      }
      const { actions, gone } = await revealTarget(page, target);
      // Every non-C0 item without a decision subject offers Mark done, Dismiss and Snooze, so a
      // missing Snooze is a defect this test reports; it never falls back to Dismiss, which
      // would hide a broken Snooze label.
      for (const label of Object.values(FEEDBACK_BUTTONS)) {
        await expect(
          actions.getByRole('button', { name: label, exact: true }),
          `item ${target.item.id} offers "${label}"`,
        ).toBeVisible({ timeout: READ_TIMEOUT_MS });
      }
      await expect(
        actions.getByRole('button', { name: RECORD_DECISION_LABEL, exact: true }),
        `item ${target.item.id} has no decision subject, so no "${RECORD_DECISION_LABEL}"`,
      ).toHaveCount(0);
      const baseline =
        sectionOf(recommendations, target.sectionKey).suppressed[`feedback_${FEEDBACK_KIND}`] ?? 0;
      await preflightTarget(target);
      // Owned, with its token, before the click: the teardown finds the row by that token alone.
      const owned: OwnedSnooze = { target, token: snoozeToken() };
      ownedSnoozes.push(owned);
      const routed = await routeOwnedSnooze(page, owned);
      try {
        const [response] = await Promise.all([
          page.waitForResponse(
            (candidate) =>
              candidate.request().method() === 'POST' &&
              new URL(candidate.url()).pathname === feedbackPath(target),
            { timeout: CARD_REMOVAL_TIMEOUT_MS },
          ),
          actions
            .getByRole('button', { name: FEEDBACK_BUTTONS[FEEDBACK_KIND], exact: true })
            .click({ timeout: READ_TIMEOUT_MS })
            .then(() => {
              attempt.sent = target.item.id;
            }),
        ]);
        if (response.status() !== 201) {
          // The API stored nothing, so another try may snooze.
          attempt.sent = undefined;
        }
        expect(response.status(), `the snooze of item ${target.item.id} is recorded`).toBe(201);
        owned.feedbackId = storedFeedbackId(await response.json(), owned);
        attempt.submitted = { target, gone, baseline };
      } catch (error) {
        // The route's account says more than a timeout. A refused body is a UI defect another
        // try would only repeat, and a failed forward may have stored the row, so either way
        // the sent click stands.
        attempt.failure =
          routed.failures.length > 0
            ? new Error(routed.failures.join('\n'), { cause: error })
            : error;
        throw attempt.failure;
      } finally {
        await routed.remove();
      }
    }).toPass(SETTLE);

    const submitted = attempt.submitted;
    if (attempt.failure !== undefined || submitted === undefined) {
      // The snooze was sent, then its answer was lost or unusable; the teardown removes it.
      throw attempt.failure instanceof Error
        ? attempt.failure
        : new Error(`The snooze of item ${String(attempt.sent)} was sent but not recorded`);
    }
    const { target, gone, baseline } = submitted;
    const hiddenWord = SUPPRESSION_LABELS[FEEDBACK_KIND];
    await expect(gone, `item ${target.item.id} leaves the page once ${hiddenWord}`).toHaveCount(
      0,
      { timeout: CARD_REMOVAL_TIMEOUT_MS },
    );

    // Until the coalesced rerun lands, the run still holds the item, hidden live by the
    // overlay; afterwards the new run leaves the rule and entity out and stores the count.
    await expect(async () => {
      await reloadActions(page);
      const recommendations = await fetchRecommendations(page);
      const runId = recommendations.run.id;
      const section = sectionOf(recommendations, target.sectionKey);
      const items = allItems(section);
      const stored = section.suppressed[`feedback_${FEEDBACK_KIND}`] ?? 0;
      if (runId === target.runId) {
        const same = items.find((item) => item.id === target.item.id);
        expect(same?.current.hidden_by, `run ${runId} marks item ${target.item.id} hidden`).toBe(
          FEEDBACK_KIND,
        );
      } else {
        if (target.item.entity.id !== null) {
          expect(
            items.some((item) => !isHidden(item) && sameRuleAndEntity(item, target.item)),
            `run ${runId} leaves the ${hiddenWord} rule and entity out`,
          ).toBe(false);
        }
        // At least one more than the target's run stored: this snooze, never an older one alone.
        expect(
          stored,
          `run ${runId} stores a ${hiddenWord} count above run ${target.runId}'s ${baseline}`,
        ).toBeGreaterThanOrEqual(baseline + 1);
      }

      const region = sectionRegion(page, target.sectionKey);
      await expect(region.locator(`#item-${target.item.id}`)).toHaveCount(0);
      const notShown = region.getByRole('list', { name: NOT_SHOWN_LABEL, exact: true });
      const hiddenTotal = items.filter(isHidden).length;
      if (hiddenTotal > 0) {
        await expect(notShown).toContainText(
          `${COUNT_FORMAT.format(hiddenTotal)} hidden since this run`,
        );
      }
      if (stored > 0) {
        await expect(notShown).toContainText(`${COUNT_FORMAT.format(stored)} ${hiddenWord}`);
      }
    }).toPass(SUPPRESSION_RETRY);
  });

  test('discrepancy cards offer their resolver and no feedback action', async ({ page }) => {
    await openActions(page);

    await test.step("the run's C0 cards, every batch revealed", async () => {
      const checked: { count: number } = { count: 0 };
      await expect(async () => {
        await reloadActions(page);
        const fresh = await fetchRecommendations(page);
        checked.count =
          discrepancyItems(fresh).length === 0
            ? 0
            : await expectDiscrepancyCards(page, fresh, new Map());
      }).toPass(SETTLE);
      if (checked.count === 0) {
        note('No C0 item in this run; the routed page of the next step covers C0 cards');
      }
    });

    const boundaryStep = `a routed page with more C0 cards than one batch of ${CARD_BATCH_SIZE}`;
    await test.step(boundaryStep, async () => {
      const served = await serveEditedRecommendations(page, withDiscrepancyBoundary);
      await serveBoundaryDiscrepancies(page);
      try {
        await expect(async () => {
          await reloadActions(page);
          const latest = served.latest;
          if (latest === null) {
            throw new Error('The browser has not read the routed recommendations yet');
          }
          const listed = discrepancyCardList(latest.page);
          const ids = listed.map((item) => item.id);
          expect(ids.length, 'the routed group holds more cards than one batch').toBeGreaterThan(
            CARD_BATCH_SIZE,
          );
          const group = page.locator(DISCREPANCY_GROUP_SELECTOR);
          const next = Math.min(CARD_BATCH_SIZE, ids.length - CARD_BATCH_SIZE);
          await expect(
            group.getByRole('button', {
              name: `Show ${COUNT_FORMAT.format(next)} more ${next === 1 ? 'action' : 'actions'}`,
              exact: true,
            }),
            'the group offers the next batch',
          ).toBeVisible({ timeout: READ_TIMEOUT_MS });
          const total = COUNT_FORMAT.format(ids.length);
          await expect(
            group.getByText(`Showing ${COUNT_FORMAT.format(CARD_BATCH_SIZE)} of ${total} actions`, {
              exact: true,
            }),
            'the group reports one batch shown',
          ).toBeVisible();
          const mounted = new Set(
            (await renderedResolvers(group)).map((card) => cardItemId(card.elementId)),
          );
          expect(
            ids.filter((id) => mounted.has(id)),
            `before it is revealed, the group mounts one batch of ${CARD_BATCH_SIZE}`,
          ).toEqual(ids.slice(0, CARD_BATCH_SIZE));

          const checked = await expectDiscrepancyCards(page, latest.page, latest.resolutions);
          expect(checked, 'every C0 card of the routed page is checked').toBe(
            discrepancyItems(latest.page).length,
          );
          expect(
            discrepancyItems(latest.page).filter((item) => latest.resolutions.has(item.id)).length,
            'the routed page adds its boundary C0 cards',
          ).toBe(BOUNDARY_COUNT);
          await expect(
            group.getByText(`Showing all ${total} actions`, { exact: true }),
            'the group reports every card shown',
          ).toBeVisible();
          const shown = (await renderedResolvers(group)).map((card) => cardItemId(card.elementId));
          expect(
            ids.filter((id) => shown.includes(id)),
            'revealed, the group shows its whole card list',
          ).toEqual(ids);
        }).toPass(SETTLE);
      } finally {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
      }
    });
  });
});
