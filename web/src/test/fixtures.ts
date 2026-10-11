/**
 * Typed `/api/v1` payload fixtures for the web component tests.
 *
 * Component tests mock the network by stubbing `fetch` with payloads shaped exactly like
 * `src/api/types.ts`; this module is the one place those payloads are built, so the ~40 feature
 * tests share one contract-checked data set instead of hand-writing JSON. It is a pure data
 * module: no JSX, no React, no network, and nothing but the API types and `SECTION_ORDER` is
 * imported. Only test files import it; application code never does, and nothing here is a
 * default a component may read (components take windows and thresholds from the API).
 *
 * Conventions, all mirroring the backend contract:
 *
 * - **Every builder returns a fresh object.** `makeX(overrides)` builds its default with
 *   `satisfies X`, so a missing or misspelt field fails `npm run typecheck`, then applies the
 *   overrides **shallowly**: compose nested builders for nested changes, for example
 *   `makeItem({ evidence: makeEvidence({ sample: null }) })`. Two calls never share a mutable
 *   object, so a test that mutates its payload cannot leak into the next test.
 * - **Discriminators come first.** Builders whose shape depends on a kind take it as the first
 *   argument (`makeSection('budget')`, `makeSource('google')`, `makeDecision('budget')`,
 *   `makeDiscrepancy('missing')`, `makeFeedback('dismiss')`), then the overrides.
 * - **Deterministic.** Every id, date and instant is a fixed literal; nothing reads the clock or a
 *   random source, and there is no module-level counter, so every run renders the same page.
 * - **Response fields are never omitted.** The API serialises `None` as `null` and never drops a
 *   key, so an absent value is `null` here (`Evidence.sample`, `ItemOverlay.hidden_by`,
 *   `Metric.currency` …), exactly as `types.ts` declares it.
 * - **Money is a decimal string** (`Money`), never a number; fractional Google conversions are
 *   decimal strings as well (`'62.5'`), because the backend renders every `Decimal` that way.
 *   Counts are integers. Amounts are never summed across currencies.
 * - **Texts mirror the backend.** Section, group, banner, flag and reason texts are the ones
 *   `backend/src/ads_engine/analysis/*.py` and `api/routes/*.py` render, for example
 *   "Raise daily budget from USD 200.00 to USD 240.00", so a component test asserts on text the
 *   API really sends.
 * - **Page items carry the thresholds precedence freezes.** Every item's `evidence.thresholds`
 *   lists its rule's own keys, then `sources.stale_after_hours` and `window.trusted_min_days`,
 *   which precedence records on every item whether or not it comes out stale or short (C0
 *   carries only those two). A bare `makeEvidence()` records none, for tests of absent fields;
 *   findings and withheld lines never pass precedence and keep only their rule's keys.
 *
 * The default recommendations page ({@link makeRecommendationsPage}) carries every card variant
 * the `/actions` sections render: the K1/K2/K3 keyword cards, the pinned C0 discrepancy, a pause
 * card, a held card, a C2 "Hold", a C3b launch idea, a B1 increase funded partly by a B3
 * reallocation from a shared budget, a CR3 next-test card, a historical CR1 pair, and white-paper
 * rows under a floor (with a meeting of unverified ordering), without a decision, and with an
 * unavailable count. Tests find cards by {@link FIXTURE_ITEM_IDS}, never by array position.
 * Beside the page, {@link FIXTURE_FLAGS} holds one flag of each of the fifteen codes
 * ({@link makeFlag}), and {@link makeCompletionRateTestItem} builds the completion-rate CR3 card,
 * whose sample in form opens carries a labelled projection ({@link makeProjectedSample}).
 *
 * @example
 * ```ts
 * import { FIXTURE_ITEM_IDS, makeRecommendationsPage, makeRun } from '../../test/fixtures';
 *
 * const page = makeRecommendationsPage({ run: makeRun({ short_window: true, days: 10 }) });
 * const k1 = page.sections
 *   .flatMap((section) => section.groups.flatMap((group) => group.items))
 *   .find((item) => item.id === FIXTURE_ITEM_IDS.k1);
 * ```
 *
 * @packageDocumentation
 */
import { SECTION_ORDER } from '../api/types';
import type {
  Banner,
  Baseline,
  ConfigView,
  CrmBindingsView,
  DatasetCoverage,
  DatasetFreshnessView,
  DatasetView,
  Decision,
  DecisionShape,
  Discrepancy,
  DiscrepancyKind,
  DiscrepancyValue,
  EntityView,
  Evidence,
  EvidenceWindow,
  Feedback,
  FeedbackKind,
  Finding,
  Flag,
  FlagCode,
  Group,
  Item,
  ItemOverlay,
  Me,
  Metric,
  MetricValue,
  Overlay,
  ProvenanceEntry,
  RecommendationsPage,
  Resolution,
  Run,
  Sample,
  Section,
  SectionKey,
  Source,
  SourceDay,
  SourceName,
  SubjectView,
  SyncRunView,
  Withheld,
} from '../api/types';

// ---------------------------------------------------------------------------------------------
// Fixed instants, windows and ids
// ---------------------------------------------------------------------------------------------

/**
 * When the fixture run finished and its overlay was computed: the run's `finished_at` and the
 * overlay's `computed_at`.
 */
export const FIXTURE_COMPUTED_AT = '2026-10-06T06:00:00Z';

/**
 * The default analysis window of the fixture run: 28 complete native days ending yesterday
 * (AAP 0.5.4.1), as calendar dates applied to each source's own days.
 */
export const FIXTURE_WINDOW: Readonly<{ start: string; end: string; days: number }> =
  Object.freeze({ start: '2026-09-08', end: '2026-10-05', days: 28 });

/**
 * Each day source's range and native day boundary for {@link FIXTURE_WINDOW}, Google then
 * LinkedIn, as `Run.source_days` and `Evidence.window.source_days` carry them. HubSpot holds
 * instants only, so it has no entry (`schemas.SourceDay`). Builders copy it, so mutating a
 * built payload never changes this constant.
 */
export const FIXTURE_SOURCE_DAYS: readonly Readonly<SourceDay>[] = Object.freeze([
  Object.freeze({
    source: 'google',
    start: '2026-09-08',
    end: '2026-10-05',
    boundary: 'America/New_York',
  } satisfies SourceDay),
  Object.freeze({
    source: 'linkedin',
    start: '2026-09-08',
    end: '2026-10-05',
    boundary: 'UTC',
  } satisfies SourceDay),
]);

/** The id of the fixture's latest succeeded default run ({@link makeRun}). */
export const DEFAULT_RUN_ID: Run['id'] = 101;

/** The id tests give a custom-window run, for example `/actions?run=202`. */
export const CUSTOM_RUN_ID: Run['id'] = 202;

/**
 * The configuration hash the fixture run and {@link makeConfig} share (a SHA-256 hex digest), so
 * a test can check the settings page and the run header agree.
 */
export const FIXTURE_CONFIG_HASH =
  '6f3a06618de049cfdc00190f1f31dc90f1c4f36fdba30e2ea50d244a498973bb';

/**
 * The `source_dataset` ids of the fixture world. Every source is simulated (no credentials), so
 * LinkedIn's simulated dataset has a companion CRM dataset that LinkedIn-attributed CRM evidence
 * reads (AAP 0.3.3). `retiredLinkedin` is a replaced LinkedIn dataset: an item whose provenance
 * names it is historical.
 */
export const FIXTURE_DATASET_IDS = Object.freeze({
  google: 11,
  linkedin: 12,
  hubspot: 13,
  hubspotCompanion: 14,
  retiredLinkedin: 7,
} as const);

/**
 * Internal campaign ids (`campaign.id`) used by decision subjects, discrepancies and evidence.
 * Google campaigns are in USD; LinkedIn campaigns are in EUR (`pipelineEmea`, `attributionEmea`,
 * `benchmarksEmea`) and USD (`benchmarksUs`).
 */
export const FIXTURE_CAMPAIGN_IDS = Object.freeze({
  /** Google "Brand Search — US": the C0 discrepancy subject, own daily budget. */
  brandSearch: 301,
  /** Google "Generic CRM — US": the C1 pause candidate. */
  genericCrm: 302,
  /** Google "Core CRM — US": the campaign of the K1, K2 and K3 keyword items. */
  coreCrm: 303,
  /** Google "Display Remarketing — US": the held pause candidate. */
  displayRemarketing: 304,
  /** Google "Pipeline Search — US": the B1 receiver. */
  pipelineSearch: 305,
  /** Google "Display Prospecting — US": a member of the B3 shared budget. */
  displayProspecting: 306,
  /** Google "Display Retargeting — US": a member of the B3 shared budget. */
  displayRetargeting: 307,
  /** Google "Search Partners — US": the campaign of {@link makeDecision}('shared_member'). */
  searchPartners: 308,
  /** LinkedIn "Lead Gen — Pipeline Report EMEA" (EUR): the copy/creative block, W1. */
  pipelineEmea: 501,
  /** LinkedIn "Lead Gen — Attribution EMEA" (EUR): the "No change warranted" white paper. */
  attributionEmea: 502,
  /**
   * LinkedIn "Lead Gen — Benchmarks EMEA" (EUR): the completion-rate next test of
   * {@link makeCompletionRateTestItem}; no default-page item names it.
   */
  benchmarksEmea: 503,
  /** LinkedIn "Lead Gen — Benchmarks US" (USD): the C2 "Hold" and the USD white paper. */
  benchmarksUs: 601,
} as const);

/**
 * Internal ids of the Google explicitly shared budgets (`budget.id`): `display` is the B3
 * donor, `search` the subject of {@link makeDecision}('budget') and of a budget-shaped
 * {@link makeDiscrepancy}.
 */
export const FIXTURE_BUDGET_IDS = Object.freeze({ display: 401, search: 402 } as const);

/** The id of the open discrepancy behind the C0 item, equal to `makeDiscrepancy().id`. */
export const FIXTURE_DISCREPANCY_ID: Discrepancy['id'] = 51;

/**
 * The title of each section, in the user's wording (`analysis.context.SECTION_TITLES`;
 * AAP 0.5.5).
 */
export const FIXTURE_SECTION_TITLES: Readonly<Record<SectionKey, string>> = Object.freeze({
  keywords: 'Keywords to adjust',
  campaigns: 'Campaigns to pause or launch',
  budget: 'Budget changes',
  creative: 'Copy/creative changes',
  white_papers: 'White papers',
});

/**
 * The id of every item on the default page ({@link makeRecommendationsPage}), so tests find a
 * card by name instead of by position. Every id is unique.
 */
export const FIXTURE_ITEM_IDS = Object.freeze({
  /** K1 block candidate "free crm template" (keywords → block). */
  k1: 1001,
  /** K2 expansion candidate "crm for small business" with adjacent terms (keywords → expand). */
  k2: 1002,
  /** K3 review of keyword "crm software" (keywords → review). */
  k3: 1003,
  /** C0 status discrepancy on "Brand Search — US", pinned first (campaigns → discrepancies). */
  c0: 1101,
  /** C1 "Pause" on "Generic CRM — US", with a decision subject (campaigns → pause). */
  c1: 1102,
  /** C1 "Pause" on "Display Remarketing — US", held until the discrepancy is resolved. */
  heldPause: 1103,
  /** C2 "Hold" on "Lead Gen — Benchmarks US", no decision subject (campaigns → scale). */
  c2Hold: 1104,
  /** C3b launch idea for the "data warehouse" cluster (campaigns → launch). */
  c3b: 1105,
  /** B1 increase on "Pipeline Search — US", partly funded by the B3 reallocation. */
  b1: 1201,
  /** B3 decrease on the shared "Display Shared Budget", funding the B1. */
  b3: 1202,
  /** CR3 next test (with CR2's weak stage) for "Lead Gen — Pipeline Report EMEA". */
  cr3: 1301,
  /** CR1 pair "Variant A vs Variant B", computed from a replaced dataset (historical). */
  cr1Historical: 1302,
  /** W1 "Double down" on "The 2026 B2B Pipeline Report", EUR cohort, under a contact floor. */
  wEur: 1401,
  /** W "No change warranted" on "The Attribution Playbook", EUR cohort. */
  wEurNoChange: 1402,
  /** W "Insufficient data" on "Paid Media Benchmarks 2026", USD cohort, MQL unavailable. */
  wUsd: 1403,
} as const);

/**
 * The id of {@link makeCompletionRateTestItem}, the completion-rate CR3 card. It is outside
 * {@link FIXTURE_ITEM_IDS} and the {@link makeItem} default, so the card can join the default
 * page without a collision.
 */
export const FIXTURE_COMPLETION_TEST_ITEM_ID: Item['id'] = 1303;

/**
 * The resolutions offered for each discrepancy kind, in the fixed display order
 * (`analysis.discrepancy.RESOLUTION_ORDER`, `decisions.RESOLUTIONS`; AAP 0.5.4.4). The lists are
 * the same for every decision shape a kind occurs with.
 */
export const FIXTURE_RESOLUTIONS: Readonly<Record<DiscrepancyKind, readonly Resolution[]>> =
  Object.freeze({
    status: Object.freeze(['platform_correct', 'decision_correct'] as const),
    budget: Object.freeze(['platform_correct', 'decision_correct'] as const),
    untracked: Object.freeze(['adopt_platform', 'record_intended'] as const),
    // RESOLUTION_ORDER lists decision_correct before acknowledge_removal.
    missing: Object.freeze(['decision_correct', 'acknowledge_removal'] as const),
  });

/**
 * Five selected banner cases, not one of every kind, in the API's fixed order
 * (`analysis.context.BANNER_ORDER`; AAP 0.5.5), with the tones of `BANNER_TONES` and the texts
 * `analysis/overlay.py` renders: open discrepancies, tracking absent, simulated sources,
 * incomplete datasets and short window. No `stale_datasets` banner is among them; the complete
 * six-code order, stale slot included, is checked in `features/status/GlobalBanners.test.tsx`
 * (`AAP_BANNER_ORDER`, `staleBanner`). The default page shows the first four; a short-window test
 * adds the fifth. Builders copy these objects, so mutating a page never changes this constant.
 */
export const FIXTURE_BANNERS: readonly Readonly<Banner>[] = Object.freeze([
  Object.freeze({
    code: 'open_discrepancies',
    tone: 'critical',
    message: '1 discrepancy to resolve before acting on other recommendations',
    count: 1,
  } satisfies Banner),
  Object.freeze({
    code: 'tracking_absent',
    tone: 'warning',
    message:
      'No conversions or leads recorded — verify tracking before acting on zero-outcome rules',
    count: null,
  } satisfies Banner),
  Object.freeze({
    code: 'simulated_sources',
    tone: 'info',
    message:
      'Simulated data: Google (credentials not configured); LinkedIn (credentials not ' +
      'configured); HubSpot (credentials not configured)',
    count: 3,
  } satisfies Banner),
  Object.freeze({
    code: 'incomplete_datasets',
    tone: 'warning',
    message: 'Incomplete data: google.search_terms (result limit reached at finest partition)',
    count: 1,
  } satisfies Banner),
  Object.freeze({
    code: 'short_window',
    tone: 'warning',
    message: 'Window of 10 days is below the 14-day trusted minimum',
    count: null,
  } satisfies Banner),
]);

// ---------------------------------------------------------------------------------------------
// Private building blocks
// ---------------------------------------------------------------------------------------------

/** Display names of the sources, as banner and flag messages write them. */
const SOURCE_LABELS: Readonly<Record<SourceName, string>> = Object.freeze({
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
});

/** Why every fixture source is simulated (`sync.mode.REASON_NO_CREDENTIALS`). */
const NO_CREDENTIALS = 'credentials not configured';

/** `analysis.context.HELD_DISCREPANCY_REASON`. */
const HELD_REASON = 'Resolve discrepancy first';

/** `analysis.context.HISTORICAL_REASON`. */
const HISTORICAL_REASON = 'Computed from data that has since been replaced';

/** Why `google.search_terms` is incomplete in the fixture world (AAP 0.3.3 Q-G6). */
const SEARCH_TERMS_INCOMPLETE = 'result limit reached at finest partition';

/** The signed-in admin (AAP 0.6.2 `APP_ADMIN_EMAIL` default). */
const ADMIN_EMAIL = 'admin@example.com';

/**
 * Why the optional Google-to-CRM link is unavailable in the fixture world (AAP 0.3.3): the
 * binding reason `connectors/hubspot/client.py` records when neither property is set.
 */
const GOOGLE_LINK_UNCONFIGURED =
  'HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured';

/** Why an unavailable MQL count is unavailable in the fixture. */
const MQL_UNAVAILABLE_REASON = 'MQL stage "First meeting booked" not resolved';

/**
 * When the 03:00 entity syncs finished: Google's run detected the fixture discrepancy then
 * (`DISCREPANCY_DETECTED_AT`).
 */
const ENTITY_SYNC_END = '2026-10-06T03:00:41Z';

/** A fresh copy of {@link FIXTURE_SOURCE_DAYS}. */
function sourceDays(): SourceDay[] {
  return FIXTURE_SOURCE_DAYS.map((day) => ({ ...day }));
}

/**
 * A fresh `Evidence.window` for {@link FIXTURE_WINDOW}. Google's `America/New_York` and
 * LinkedIn's `UTC` day boundaries differ, so the window says so, with the sentence
 * `AnalysisWindow.describe()` renders.
 */
function evidenceWindow(): EvidenceWindow {
  return {
    ...FIXTURE_WINDOW,
    source_days: sourceDays(),
    boundaries_differ: true,
    description: `Google ${FIXTURE_WINDOW.start} to ${FIXTURE_WINDOW.end} (America/New_York); LinkedIn same dates (UTC)`,
  };
}

/** Optional parts of a {@link metric}. */
interface MetricOptions {
  /** ISO currency code of a money value. */
  readonly currency?: string;
  /** Whether the value is an attribution floor ("≥ N", or "≤ X" for a cost). */
  readonly floor?: boolean;
}

/**
 * One cited number, as `analysis.context.Metric.to_json` renders it: `unavailable` is set
 * exactly when the value is `null`, and `floor` only for a known value that is a floor (an
 * unavailable count carries no floor, `attribution.CountValue`).
 */
function metric(
  key: string,
  label: string,
  value: MetricValue,
  unit: string | null,
  options: MetricOptions = {},
): Metric {
  return {
    key,
    label,
    value,
    unit,
    currency: options.currency ?? null,
    floor: options.floor === true && value !== null ? true : null,
    unavailable: value === null ? true : null,
  } satisfies Metric;
}

/** One baseline a metric was compared against. */
function baseline(key: string, label: string, value: MetricValue): Baseline {
  return { key, label, value } satisfies Baseline;
}

/**
 * The day boundary the backend freezes with a dataset's coverage date: the one
 * {@link FIXTURE_SOURCE_DAYS} names for a Google or LinkedIn dataset, and UTC for a HubSpot one,
 * whose sync jobs record UTC dates; `null` for a name whose prefix is no day source.
 */
function coverageBoundary(dataset: string): string | null {
  const prefix = dataset.slice(0, Math.max(dataset.indexOf('.'), 0));
  if (prefix === 'hubspot') {
    return 'UTC';
  }
  return FIXTURE_SOURCE_DAYS.find((day) => day.source === prefix)?.boundary ?? null;
}

/**
 * One complete dataset per name, covered through the window end, as of compute time, with the
 * day boundary of that date ({@link coverageBoundary}).
 */
function coverage(...names: readonly string[]): DatasetCoverage[] {
  return names.map(
    (dataset) =>
      ({
        dataset,
        completeness: 'complete',
        covered_through: FIXTURE_WINDOW.end,
        boundary: coverageBoundary(dataset),
      }) satisfies DatasetCoverage,
  );
}

/** The provenance of an item that read Google's simulated primary dataset. */
function googleProvenance(): ProvenanceEntry[] {
  return [
    {
      source: 'google',
      dataset_id: FIXTURE_DATASET_IDS.google,
      mode: 'simulated',
      role: 'primary',
    },
  ];
}

/**
 * The provenance of an item that read LinkedIn's simulated primary dataset and, when `crm` is
 * set, its companion CRM dataset (the effective CRM dataset while LinkedIn is simulated).
 */
function linkedinProvenance(crm: boolean): ProvenanceEntry[] {
  const entries: ProvenanceEntry[] = [
    {
      source: 'linkedin',
      dataset_id: FIXTURE_DATASET_IDS.linkedin,
      mode: 'simulated',
      role: 'primary',
    },
  ];
  if (crm) {
    entries.push({
      source: 'hubspot',
      dataset_id: FIXTURE_DATASET_IDS.hubspotCompanion,
      mode: 'simulated',
      role: 'companion',
    });
  }
  return entries;
}

/** A flag with its backend message. */
function flag(code: Flag['code'], message: string): Flag {
  return { code, message } satisfies Flag;
}

/** The `simulated` flag the run records on every item of a simulated dataset (P3). */
function simulatedFlag(): Flag {
  return makeFlag('simulated');
}

/** Which read-time states {@link currentFor} adds to an item. */
interface CurrentOptions {
  /** The item reads `google.search_terms`, which is incomplete now. */
  readonly incomplete?: boolean;
  /** The item is a C0 discrepancy, which never carries `unverified_discrepancies`. */
  readonly c0?: boolean;
  /** The item is held by an open discrepancy. */
  readonly held?: boolean;
}

/**
 * The read-time overlay `analysis/overlay.py` gives an item of the default page: the simulated
 * sources it reads now, the incomplete search-term dataset where it reads it, and the live
 * open-discrepancy count on every item but C0.
 */
function currentFor(sources: readonly SourceName[], options: CurrentOptions = {}): ItemOverlay {
  const flagsNow: Flag[] = [
    flag(
      'simulated',
      `Simulated data (${sources.map((source) => SOURCE_LABELS[source]).join(', ')})`,
    ),
  ];
  if (options.incomplete === true) {
    flagsNow.push(makeFlag('incomplete'));
  }
  if (options.c0 !== true) {
    flagsNow.push(makeFlag('unverified_discrepancies'));
  }
  return makeItemOverlay({
    held_now: options.held === true,
    held_reason_now: options.held === true ? HELD_REASON : null,
    flags_now: flagsNow,
  });
}

/** An entity reference: `{source, type, id, name}` with the platform id as a string. */
function entity(source: SourceName, type: string, id: string, name: string): EntityView {
  return { source, type, id, name } satisfies EntityView;
}

/**
 * Every threshold of `config/thresholds.yaml` by namespace, with `crm` also carrying the
 * mapping's meeting rules, as `GET /config` returns it (`Thresholds.as_view`). Built fresh on
 * each call, so no caller shares its arrays.
 */
function thresholdsTree(): Record<string, Record<string, unknown>> {
  return {
    window: {
      default_days: 28,
      trusted_min_days: 14,
      presets: [14, 28, 56, 90],
      sustained_min_days: 7,
      max_days: 180,
    },
    sources: { stale_after_hours: 24 },
    budget: {
      max_observation_gap_hours: 6,
      capped_day_ratio: 0.95,
      capped_lost_is_share: 0.1,
      high_utilization: 0.9,
      under_utilization: 0.5,
      min_known_days: 7,
      min_spending_days: 5,
      min_capped_days: 5,
      sustained_share: 0.7,
    },
    crm: {
      lead_maturity_days: 14,
      veto_min_leads: 20,
      booking_sources: ['MEETINGS_PUBLIC'],
      meeting_outcome_exclusions: ['CANCELED'],
    },
    attribution: { floor_share: 0.1 },
    keywords: { min_search_term_coverage: 0.8 },
    k1: {
      baseline_days: 90,
      min_clicks_floor: 10,
      rule_of_three: 3,
      min_baseline_conversions: 10,
      min_spend_cpa_multiple: 1.0,
    },
    k2: {
      min_conversions: 3,
      max_cpa_ratio: 1.0,
      adjacent_jaccard: 0.5,
      adjacent_max_terms: 5,
      adjacent_min_clicks: 1,
    },
    k3: {
      min_window_days: 14,
      min_clicks_per_half: 30,
      qs_span_days: 7,
      min_qs_days_per_span: 5,
      qs_drop: 1,
      rise_ratio: 1.15,
      min_first_half_conversions: 1,
    },
    c1: { min_window_days: 14, min_spend_day_share: 0.7, min_expected_outcomes: 3 },
    c1b: { min_window_days: 28 },
    scale: { min_outcomes: 5, max_cpa_ratio: 1.0 },
    c3a: { min_leads: 5, max_cpl_ratio: 0.8, max_impression_share: 0.15 },
    c3b: { jaccard: 0.5, min_terms: 3, min_conversions: 5, head_token_share: 0.5 },
    b1: { step: 0.2 },
    b2: { poor_efficiency_ratio: 1.5, min_expected_outcomes: 3 },
    b3: { spend_percentile: 90, headroom: 1.1 },
    b4: { min_weekend_days: 4, min_weekdays: 10, min_outcomes: 10, alpha: 0.05 },
    cr: {
      min_impressions: 1000,
      min_variants: 2,
      alpha: 0.05,
      power: 0.8,
      target_lift: 0.2,
      fisher_min_expected: 5,
      ci_level: 0.95,
    },
    w: {
      min_ql: 3,
      median_min_ql: 1,
      double_down_ratio: 0.8,
      retire_spend_multiple: 3,
      retire_min_window_days: 28,
      cohort_min_total_ql: 3,
    },
    feedback: { dismiss_days: 30, snooze_days: 7, done_days: 30 },
  };
}

/**
 * `{dotted key: configured value}` for exactly `keys`, as a rule copies them into
 * `evidence.thresholds` (`Thresholds.subset`). This is a rule's own record, before precedence:
 * findings and withheld lines keep it as it is, while a page item's evidence is built with
 * {@link itemThresholds}, which adds the policy keys precedence freezes.
 *
 * @throws RangeError when a key is not in {@link thresholdsTree}, so a misspelt key fails the
 *   first test that builds the page instead of rendering an empty threshold.
 */
function thresholdSubset(...keys: readonly string[]): Record<string, unknown> {
  const tree = thresholdsTree();
  const subset: Record<string, unknown> = {};
  for (const key of keys) {
    const [namespace, name, ...rest] = key.split('.');
    const value =
      namespace === undefined || name === undefined || rest.length > 0
        ? undefined
        : tree[namespace]?.[name];
    if (value === undefined) {
      throw new RangeError(`fixtures: unknown threshold key ${JSON.stringify(key)}`);
    }
    subset[key] = value;
  }
  return subset;
}

/**
 * The policy thresholds precedence freezes into every item's `evidence.thresholds`, in the order
 * it appends them: P4 judges each required dataset against `sources.stale_after_hours`
 * (`precedence._apply_data_state`, on every item that reads a dataset) and P5 the window against
 * `window.trusted_min_days` (`precedence._apply_short_window`, on every item), whether or not the
 * item comes out stale or short.
 */
const PRECEDENCE_THRESHOLD_KEYS: readonly string[] = Object.freeze([
  'sources.stale_after_hours',
  'window.trusted_min_days',
]);

/**
 * A page item's `evidence.thresholds`: the rule's own `keys` ({@link thresholdSubset}) in their
 * order, followed by each of {@link PRECEDENCE_THRESHOLD_KEYS} the rule did not record, with its
 * configured value from {@link thresholdsTree}. A key the rule already records keeps its place,
 * as `precedence._with_applied_thresholds` leaves it.
 *
 * @throws RangeError when a key is not in {@link thresholdsTree}
 */
function itemThresholds(...keys: readonly string[]): Record<string, unknown> {
  const thresholds = thresholdSubset(...keys);
  for (const [key, value] of Object.entries(thresholdSubset(...PRECEDENCE_THRESHOLD_KEYS))) {
    if (!Object.hasOwn(thresholds, key)) {
      thresholds[key] = value;
    }
  }
  return thresholds;
}

// ---------------------------------------------------------------------------------------------
// Flag catalogue
// ---------------------------------------------------------------------------------------------

/**
 * Every flag code, in the backend's badge order (`analysis.context.FLAG_CODES`; AAP 0.5.3
 * `Flag`): data state, discrepancies, attribution, pipeline and statistics, then budget scope.
 * Deep-frozen; equal to the keys of {@link FIXTURE_FLAGS}, in the same order.
 */
export const FIXTURE_FLAG_CODES: readonly FlagCode[] = Object.freeze([
  'simulated',
  'stale',
  'incomplete',
  'short_window',
  'unverified_discrepancies',
  'attribution_floor',
  'creative_floor',
  'deal_floor',
  'unknown_crm_evidence',
  'meeting_time_proxy',
  'pipeline_unavailable',
  'no_crm_link',
  'not_distinguishable',
  'search_term_coverage',
  'shared_budget',
] as const);

/** A catalogue entry: a flag filed under its own code. */
type CatalogueFlag<K extends FlagCode> = Readonly<Flag & { code: K }>;

/**
 * One deep-frozen flag per code, with the message the backend renders for it in the fixture
 * world (window {@link FIXTURE_WINDOW}, computed at {@link FIXTURE_COMPUTED_AT}). A missing code,
 * or an entry filed under another code, fails `npm run typecheck`. Shares render as
 * `attribution._percent` does: at most one decimal, trailing zeros dropped.
 *
 * - The nine codes the default page carries reuse its texts, and the page's cards draw them from
 *   here: `simulated`, `incomplete` (`google.search_terms`), `unverified_discrepancies` (one
 *   open discrepancy), `attribution_floor` (the EUR white paper), `pipeline_unavailable` (the
 *   unresolved MQL stage), `no_crm_link` (every Google card), `not_distinguishable` (the CR1
 *   pair), `search_term_coverage` (74%) and `shared_budget` (the B3 donor).
 * - `stale`: `google.metrics_daily` last succeeded at 2026-10-04 05:31 UTC, more than
 *   `sources.stale_after_hours` (24) before the run, and covers only through 2026-10-03
 *   (`precedence._stale_flag`). The instant reads in the fixture run's `display_timezone`
 *   (`UTC`), formatted as `formatInstant` formats it (`context.format_instant`); the coverage
 *   end stays an ISO native date.
 * - `short_window`: a 10-day window, the same text as the short-window banner of
 *   {@link FIXTURE_BANNERS} (`precedence._apply_short_window`).
 * - `creative_floor`: 12 of 96 leads without a creative (12.5%).
 * - `deal_floor`: 1 of 4 assessed deals without a contact (25%); an incomplete deal search
 *   renders the share "unknown (deal search incomplete)" instead, or "unknown (deal search
 *   incomplete; at least N deals not assessed)" when N > 0 (`attribution._orphan_deals`).
 * - `unknown_crm_evidence`: the white-paper form, 2 of 20 matched contacts (10%). Campaign and
 *   budget cards, judged on the pipeline-veto population, word it "Unknown CRM evidence for {n}
 *   of {m} eligible contacts" (`campaigns._veto_flags`).
 * - `meeting_time_proxy`: the EUR white paper's one other meeting (ordering unverified).
 */
export const FIXTURE_FLAGS: Readonly<Record<FlagCode, Readonly<Flag>>> = Object.freeze({
  simulated: Object.freeze({ code: 'simulated', message: 'Simulated data' } satisfies Flag),
  stale: Object.freeze({
    code: 'stale',
    message: 'Data as of Oct 4, 2026, 5:31 AM UTC; google.metrics_daily covers through 2026-10-03',
  } satisfies Flag),
  incomplete: Object.freeze({
    code: 'incomplete',
    message: `Incomplete: google.search_terms (${SEARCH_TERMS_INCOMPLETE})`,
  } satisfies Flag),
  short_window: Object.freeze({
    code: 'short_window',
    message: 'Window of 10 days is below the 14-day trusted minimum',
  } satisfies Flag),
  unverified_discrepancies: Object.freeze({
    code: 'unverified_discrepancies',
    message: 'Unverified while 1 discrepancy is open',
  } satisfies Flag),
  attribution_floor: Object.freeze({
    code: 'attribution_floor',
    message: 'Floor: 12% of leads unmatched in HubSpot',
  } satisfies Flag),
  creative_floor: Object.freeze({
    code: 'creative_floor',
    message: 'Per-creative breakdown is a floor: 12.5% of leads have no creative',
  } satisfies Flag),
  deal_floor: Object.freeze({
    code: 'deal_floor',
    message: 'Deal counts are a floor: orphan-deal share 25% (1 of 4 deals without a contact)',
  } satisfies Flag),
  unknown_crm_evidence: Object.freeze({
    code: 'unknown_crm_evidence',
    message: 'Floor: CRM evidence unknown for 10% of matched contacts (2 of 20)',
  } satisfies Flag),
  meeting_time_proxy: Object.freeze({
    code: 'meeting_time_proxy',
    message:
      '1 other meeting (ordering unverified): booking time is proxied by record creation, so ' +
      'it is excluded from cost per meeting',
  } satisfies Flag),
  pipeline_unavailable: Object.freeze({
    code: 'pipeline_unavailable',
    message: `Pipeline unavailable: ${MQL_UNAVAILABLE_REASON}`,
  } satisfies Flag),
  no_crm_link: Object.freeze({
    code: 'no_crm_link',
    message: `No CRM link for Google campaigns: ${GOOGLE_LINK_UNCONFIGURED}`,
  } satisfies Flag),
  not_distinguishable: Object.freeze({
    code: 'not_distinguishable',
    message: 'Not yet distinguishable (adjusted p = 0.21)',
  } satisfies Flag),
  search_term_coverage: Object.freeze({
    code: 'search_term_coverage',
    message: 'Low search-term coverage: 74% of keyword spend',
  } satisfies Flag),
  shared_budget: Object.freeze({
    code: 'shared_budget',
    message: 'Shared budget across 2 campaigns: decided at budget level',
  } satisfies Flag),
} satisfies { readonly [K in FlagCode]: CatalogueFlag<K> });

/**
 * The {@link FIXTURE_FLAGS} flag of `code`, for example `makeFlag('stale')` on a card whose
 * data is stale, or `makeFlag('short_window')` on every item of a short-window page.
 *
 * @param code - the flag code
 * @param overrides - fields to replace (shallow), typically `message`
 * @returns a fresh `Flag`
 */
export function makeFlag(code: FlagCode, overrides: Partial<Flag> = {}): Flag {
  const base = { ...FIXTURE_FLAGS[code] } satisfies Flag;
  return { ...base, ...overrides };
}

// ---------------------------------------------------------------------------------------------
// Scalar builders
// ---------------------------------------------------------------------------------------------

/**
 * The signed-in user of `GET /auth/me` and `POST /auth/login`: the default admin.
 *
 * @param overrides - fields to replace
 * @returns a fresh `Me`
 */
export function makeMe(overrides: Partial<Me> = {}): Me {
  const base = { email: ADMIN_EMAIL } satisfies Me;
  return { ...base, ...overrides };
}

/**
 * The latest succeeded default-window run: {@link FIXTURE_WINDOW} with each source's native day
 * boundary, every source simulated, finished at {@link FIXTURE_COMPUTED_AT}. `default_days` and
 * `trusted_min_days` are the shipped `config/thresholds.yaml` values.
 *
 * @example
 * ```ts
 * // A 90-day custom run, as POST /runs returns it.
 * makeRun({
 *   id: CUSTOM_RUN_ID,
 *   kind: 'custom',
 *   trigger: 'manual',
 *   window_start: '2026-07-08',
 *   days: 90,
 * });
 * ```
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Run`
 */
export function makeRun(overrides: Partial<Run> = {}): Run {
  const base = {
    id: DEFAULT_RUN_ID,
    kind: 'default',
    window_start: FIXTURE_WINDOW.start,
    window_end: FIXTURE_WINDOW.end,
    days: FIXTURE_WINDOW.days,
    source_days: sourceDays(),
    display_timezone: 'UTC',
    short_window: false,
    default_days: 28,
    trusted_min_days: 14,
    trigger: 'event',
    data_modes: { google: 'simulated', linkedin: 'simulated', hubspot: 'simulated' },
    config_hash: FIXTURE_CONFIG_HASH,
    status: 'succeeded',
    finished_at: FIXTURE_COMPUTED_AT,
  } satisfies Run;
  return { ...base, ...overrides };
}

/**
 * A minimal frozen evidence block: the fixture window with its source boundaries, no metric,
 * baseline or threshold, one complete dataset and its simulated provenance. `sample`, `test` and
 * `extra` are `null` (none recorded) unless given.
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Evidence`
 */
export function makeEvidence(overrides: Partial<Evidence> = {}): Evidence {
  const base = {
    window: evidenceWindow(),
    metrics: [],
    baselines: [],
    sample: null,
    test: null,
    thresholds: {},
    datasets: coverage('google.metrics_daily'),
    provenance: googleProvenance(),
    extra: null,
  } satisfies Evidence;
  return { ...base, ...overrides };
}

/**
 * An item's read-time overlay with nothing current: not held, not historical, still
 * simulated, no current flag and not hidden by feedback.
 *
 * To render a historical card "since replaced by live", override `data_status_now: 'live'`
 * together with `historical: true`.
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `ItemOverlay`
 */
export function makeItemOverlay(overrides: Partial<ItemOverlay> = {}): ItemOverlay {
  const base = {
    held_now: false,
    held_reason_now: null,
    historical: false,
    data_status_now: 'simulated',
    flags_now: [],
    hidden_by: null,
  } satisfies ItemOverlay;
  return { ...base, ...overrides };
}

/**
 * A minimal valid K1 item: rank 1, simulated, not held, no decision subject, with
 * {@link makeEvidence} and {@link makeItemOverlay}. Its id (1) is outside
 * {@link FIXTURE_ITEM_IDS}, so it can join the default page without a collision.
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Item`
 */
export function makeItem(overrides: Partial<Item> = {}): Item {
  const base = {
    id: 1,
    rank: 1,
    rule_id: 'K1',
    action: 'Add as exact-match negative',
    reason: 'Spent USD 50.00 over 28 days with 0 conversions across 40 clicks.',
    entity: entity('google', 'search_term', '61000201|7700119|crm demo', 'crm demo'),
    evidence: makeEvidence(),
    flags: [simulatedFlag()],
    data_status: 'simulated',
    held: false,
    held_reason: null,
    decision_subject: null,
    current: makeItemOverlay(),
  } satisfies Item;
  return { ...base, ...overrides };
}

/**
 * An empty Keywords "Block" group (`analysis.context.GROUP_TITLES`).
 *
 * @param overrides - fields to replace, typically `key`, `title` and `items`
 * @returns a fresh `Group`
 */
export function makeGroup(overrides: Partial<Group> = {}): Group {
  const base = {
    key: 'block',
    title: 'Block / negative-keyword candidates',
    items: [],
  } satisfies Group;
  return { ...base, ...overrides };
}

/**
 * An empty section: its title from {@link FIXTURE_SECTION_TITLES}, with no group, finding,
 * withheld pattern or suppression count.
 *
 * @param key - the section key
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Section`
 */
export function makeSection(key: SectionKey, overrides: Partial<Section> = {}): Section {
  const base = {
    key,
    title: FIXTURE_SECTION_TITLES[key],
    groups: [],
    findings: [],
    withheld: [],
    suppressed: {},
  } satisfies Section;
  return { ...base, ...overrides };
}

/**
 * The page-level overlay of the default page: one open discrepancy (the C0 item), every source
 * simulated, nothing stale and `google.search_terms` incomplete.
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Overlay`
 */
export function makeOverlay(overrides: Partial<Overlay> = {}): Overlay {
  const base = {
    open_discrepancies: 1,
    source_modes: { google: 'simulated', linkedin: 'simulated', hubspot: 'simulated' },
    stale_datasets: [],
    incomplete_datasets: ['google.search_terms'],
    computed_at: FIXTURE_COMPUTED_AT,
  } satisfies Overlay;
  return { ...base, ...overrides };
}

/**
 * The open-discrepancy banner, the first of {@link FIXTURE_BANNERS}.
 *
 * @param overrides - fields to replace, for example another banner's `code`, `tone` and
 *   `message`
 * @returns a fresh `Banner`
 */
export function makeBanner(overrides: Partial<Banner> = {}): Banner {
  const base = {
    code: 'open_discrepancies',
    tone: 'critical',
    message: '1 discrepancy to resolve before acting on other recommendations',
    count: 1,
  } satisfies Banner;
  return { ...base, ...overrides };
}

/**
 * The Keywords section's `search_term_coverage` finding: search terms cover 74% of keyword
 * spend, below the 80% minimum (`analysis/keywords.py`).
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Finding`
 */
export function makeFinding(overrides: Partial<Finding> = {}): Finding {
  const base = {
    code: 'search_term_coverage',
    section: 'keywords',
    entity: entity('google', 'account', '1234567890', 'Acme Corp — Google Ads'),
    message:
      'Search terms cover 74% of keyword spend (USD 2,960.00 of USD 4,000.00) over the 28-day ' +
      'window. Google withholds low-volume search terms for privacy, so search-term totals fall ' +
      'below keyword totals. Coverage is below the 80% minimum, so every keyword item carries ' +
      'the low-coverage flag.',
    evidence: {
      currency: 'USD',
      search_term_spend: '2960.00',
      keyword_spend: '4000.00',
      coverage_share: '0.74',
      coverage_percent: '74',
      min_share: '0.8',
      below_minimum: true,
      thresholds: thresholdSubset('keywords.min_search_term_coverage'),
    },
  } satisfies Finding;
  return { ...base, ...overrides };
}

/**
 * A K1 search term withheld for sample size: 30 clicks of the 37 the account's conversion rate
 * requires (`analysis/keywords.py`).
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Withheld`
 */
export function makeWithheld(overrides: Partial<Withheld> = {}): Withheld {
  const base = {
    rule_id: 'K1',
    entity: entity(
      'google',
      'search_term',
      '61000201|7700112|crm pricing comparison',
      'crm pricing comparison',
    ),
    reason: 'clicks 30 of 37 required',
    sample: { observed: 30, required: 37, unit: 'clicks' },
  } satisfies Withheld;
  return { ...base, ...overrides };
}

/**
 * When each feedback kind's suppression ends for feedback given at 2026-10-06T09:00:00Z:
 * `feedback.snooze_days` (7) and `feedback.dismiss_days` / `feedback.done_days` (30) later
 * (AAP 0.5.2).
 */
const FEEDBACK_UNTIL: Readonly<Record<FeedbackKind, string>> = Object.freeze({
  // created_at 2026-10-06T09:00:00Z + 7 days (feedback.snooze_days)
  snooze: '2026-10-13T09:00:00Z',
  // created_at 2026-10-06T09:00:00Z + 30 days (feedback.dismiss_days, feedback.done_days)
  dismiss: '2026-11-05T09:00:00Z',
  done: '2026-11-05T09:00:00Z',
});

/**
 * Feedback the admin gave on the K1 item, as `POST /recommendations/{id}/feedback` returns it:
 * `until` is 7 days after `created_at` for a snooze and 30 days for a dismiss or done.
 *
 * @param kind - `snooze` (default), `dismiss` or `done`
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Feedback`
 */
export function makeFeedback(
  kind: FeedbackKind = 'snooze',
  overrides: Partial<Feedback> = {},
): Feedback {
  const base = {
    id: 71,
    recommendation_id: FIXTURE_ITEM_IDS.k1,
    rule_id: 'K1',
    entity_type: 'search_term',
    entity_id: 'google:search_term:61000201|7700112|free crm template',
    kind,
    until: FEEDBACK_UNTIL[kind],
    note: null,
    created_at: '2026-10-06T09:00:00Z',
    created_by: ADMIN_EMAIL,
  } satisfies Feedback;
  return { ...base, ...overrides };
}


// ---------------------------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------------------------

/**
 * The CRM bindings as verified against a (simulated) portal: MQL bound to the custom
 * "First meeting booked" stage and QL to the custom "Qualified Lead" stage, each through its
 * `hs_v2_date_entered_{stage id}` property; the optional Google-to-CRM link is not configured.
 */
function crmBindings(verifiedAt: string): CrmBindingsView {
  return {
    mql: {
      available: true,
      property: 'hs_v2_date_entered_1084512345',
      stage_id: '1084512345',
      label: 'First meeting booked',
      reason: null,
    },
    ql: {
      available: true,
      property: 'hs_v2_date_entered_1084512399',
      stage_id: '1084512399',
      label: 'Qualified Lead',
      reason: null,
    },
    google: {
      available: false,
      campaign_property: null,
      touch_date_property: null,
      reason: GOOGLE_LINK_UNCONFIGURED,
    },
    verified_at: verifiedAt,
  } satisfies CrmBindingsView;
}

/**
 * `GET /config` for the shipped configuration: every threshold of `config/thresholds.yaml`
 * (window presets 14/28/56/90 with 28 as the default and 14 as the trusted minimum), the
 * `config/hubspot_mapping.yaml` bindings (MQL "First meeting booked"; QL "Qualified Lead", then
 * "Sales Qualified Lead"), the three white papers of `config/content_assets.yaml`, the bindings
 * resolved against HubSpot's primary dataset and against LinkedIn's companion CRM dataset, and
 * {@link FIXTURE_CONFIG_HASH}.
 *
 * @param overrides - fields to replace (shallow), for example
 *   `{ window: { ...makeConfig().window, default_days: 21, presets: [14, 21, 28, 56, 90] } }`
 * @returns a fresh `ConfigView`
 */
export function makeConfig(overrides: Partial<ConfigView> = {}): ConfigView {
  const base = {
    thresholds: thresholdsTree(),
    window: {
      default_days: 28,
      trusted_min_days: 14,
      presets: [14, 28, 56, 90],
      max_days: 180,
      sustained_min_days: 7,
    },
    hubspot_mapping: {
      mql: { mode: 'stage_label', stage_labels: ['First meeting booked'], property: null },
      ql: {
        mode: 'stage_label',
        stage_labels: ['Qualified Lead', 'Sales Qualified Lead'],
        property: null,
      },
      booking_sources: ['MEETINGS_PUBLIC'],
      meeting_outcome_exclusions: ['CANCELED'],
    },
    crm_bindings: crmBindings('2026-10-01T08:10:00Z'),
    companion_crm_bindings: crmBindings('2026-10-01T08:11:00Z'),
    content_assets: [
      {
        slug: 'b2b-pipeline-report-2026',
        title: 'The 2026 B2B Pipeline Report',
        lead_forms: ['urn:li:leadGenForm:990000101', 'urn:li:leadGenForm:990000102'],
        creative_overrides: [],
      },
      {
        slug: 'paid-media-benchmarks-2026',
        title: 'Paid Media Benchmarks 2026',
        lead_forms: ['urn:li:leadGenForm:990000201'],
        creative_overrides: [],
      },
      {
        slug: 'attribution-playbook',
        title: 'The Attribution Playbook',
        lead_forms: ['urn:li:leadGenForm:990000301'],
        creative_overrides: ['urn:li:sponsoredCreative:990001301'],
      },
    ],
    config_hash: FIXTURE_CONFIG_HASH,
  } satisfies ConfigView;
  return { ...base, ...overrides };
}

// ---------------------------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------------------------

/** The freshness-tracked datasets of each source (`db.models.ops.DATASETS`; AAP 0.3.2). */
const SOURCE_DATASETS: Readonly<Record<SourceName, readonly string[]>> = Object.freeze({
  google: Object.freeze(['google.entities', 'google.metrics_daily', 'google.search_terms']),
  linkedin: Object.freeze([
    'linkedin.entities',
    'linkedin.analytics_daily',
    'linkedin.segments',
    'linkedin.leads',
  ]),
  hubspot: Object.freeze([
    'hubspot.contacts',
    'hubspot.associations',
    'hubspot.deal_search',
    'hubspot.google_contacts',
  ]),
});

/** The datasets a companion CRM dataset writes (AAP 0.3.3: no Google-touch discovery). */
const COMPANION_DATASETS: readonly string[] = Object.freeze([
  'hubspot.contacts',
  'hubspot.associations',
  'hubspot.deal_search',
]);

/** The last attempt and last success of every fixture dataset: the 05:30 sync cycle. */
const LAST_SYNC_AT = '2026-10-06T05:30:00Z';

/** When Google's 05:30 metrics sync finished. */
const METRICS_SYNC_END = '2026-10-06T05:31:12Z';

/** When LinkedIn's 03:05 lead sync finished. */
const LEAD_SYNC_END = '2026-10-06T03:05:20Z';

/**
 * The freshness row of one dataset: refreshed at {@link LAST_SYNC_AT} over the 180-day backfill
 * (`BACKFILL_DAYS`) through the window end. Full-state entity reads record no date range.
 * `google.search_terms` is incomplete (a Q-G6 partition hit the result limit), and
 * `hubspot.google_contacts` is unknown because the Google-to-CRM link is not configured.
 */
function freshness(dataset: string): DatasetFreshnessView {
  if (dataset === 'hubspot.google_contacts') {
    return {
      dataset,
      last_attempt_at: null,
      last_success_at: null,
      covered_from: null,
      covered_through: null,
      completeness: 'unknown',
      reason: 'Google-to-CRM link not configured',
      stale: false,
      incomplete: true,
    } satisfies DatasetFreshnessView;
  }
  const fullState = dataset.endsWith('.entities');
  const incomplete = dataset === 'google.search_terms';
  return {
    dataset,
    last_attempt_at: LAST_SYNC_AT,
    last_success_at: LAST_SYNC_AT,
    covered_from: fullState ? null : '2026-04-09',
    covered_through: fullState ? null : FIXTURE_WINDOW.end,
    completeness: incomplete ? 'incomplete' : 'complete',
    reason: incomplete ? SEARCH_TERMS_INCOMPLETE : null,
    stale: false,
    incomplete,
  } satisfies DatasetFreshnessView;
}

/** An active simulated dataset view. */
function datasetView(
  id: number,
  role: DatasetView['role'],
  reason: string,
  createdAt: string,
): DatasetView {
  return {
    id,
    mode: 'simulated',
    role,
    state: 'active',
    reason,
    created_at: createdAt,
    activated_at: '2026-10-01T08:12:00Z',
  } satisfies DatasetView;
}

/** One succeeded sync job run. */
function syncRun(
  id: number,
  job: string,
  dataset: string | null,
  startedAt: string,
  finishedAt: string,
  rows: number,
): SyncRunView {
  return {
    id,
    job,
    dataset,
    started_at: startedAt,
    finished_at: finishedAt,
    status: 'succeeded',
    rows,
    completeness: 'complete',
    error: null,
  } satisfies SyncRunView;
}

/** The two newest sync runs of each source, newest first. */
function recentRuns(source: SourceName): SyncRunView[] {
  switch (source) {
    case 'google':
      return [
        syncRun(9002, 'metrics', 'google.metrics_daily', LAST_SYNC_AT, METRICS_SYNC_END, 4210),
        syncRun(9001, 'entities', 'google.entities', '2026-10-06T03:00:00Z', ENTITY_SYNC_END, 186),
      ];
    case 'linkedin':
      return [
        syncRun(9004, 'leads', 'linkedin.leads', '2026-10-06T03:05:00Z', LEAD_SYNC_END, 37),
        syncRun(9003, 'entities', 'linkedin.entities', '2026-10-06T03:00:00Z', ENTITY_SYNC_END, 64),
      ];
    case 'hubspot':
      return [
        syncRun(9006, 'crm', null, '2026-10-06T03:10:00Z', '2026-10-06T03:11:02Z', 512),
        syncRun(9005, 'lead_match', null, '2026-10-06T03:08:00Z', '2026-10-06T03:08:30Z', 37),
      ];
  }
}

/** The `source_dataset` id of each source's active primary dataset. */
const PRIMARY_DATASET_IDS: Readonly<Record<SourceName, number>> = Object.freeze({
  google: FIXTURE_DATASET_IDS.google,
  linkedin: FIXTURE_DATASET_IDS.linkedin,
  hubspot: FIXTURE_DATASET_IDS.hubspot,
});

/**
 * The native day boundary `GET /sources` reports for each source's coverage dates: Google's
 * account time zone, the same zone {@link FIXTURE_SOURCE_DAYS} names, and UTC for LinkedIn and
 * HubSpot (AAP 0.5.4.1).
 */
const SOURCE_DAY_BOUNDARIES: Readonly<Record<SourceName, string>> = Object.freeze({
  google: 'America/New_York',
  linkedin: 'UTC',
  hubspot: 'UTC',
});

/**
 * One source of `GET /sources`: simulated because credentials are not configured, status `ok`,
 * its native day boundary ({@link SOURCE_DAY_BOUNDARIES}), an active simulated primary dataset
 * with a freshness row per dataset (none stale; `google.search_terms` incomplete), no pending
 * "Sync now" and its two newest sync runs. Google also reports 112 webhook calls in the last
 * 24 h against the product's cap of 1,000; HubSpot also carries LinkedIn's companion CRM
 * dataset, because LinkedIn is simulated.
 *
 * @param source - the source
 * @param overrides - fields to replace (shallow), for example `{ sync_pending: true }`
 * @returns a fresh `Source`
 */
export function makeSource(source: SourceName, overrides: Partial<Source> = {}): Source {
  const companion = source === 'hubspot';
  const base = {
    source,
    mode: 'simulated',
    status: 'ok',
    reason: NO_CREDENTIALS,
    day_boundary: SOURCE_DAY_BOUNDARIES[source],
    active_dataset: datasetView(
      PRIMARY_DATASET_IDS[source],
      'primary',
      NO_CREDENTIALS,
      '2026-10-01T08:00:00Z',
    ),
    companion_dataset: companion
      ? datasetView(
          FIXTURE_DATASET_IDS.hubspotCompanion,
          'companion',
          `companion of simulated LinkedIn dataset ${String(FIXTURE_DATASET_IDS.linkedin)}`,
          '2026-10-01T08:01:00Z',
        )
      : null,
    datasets: SOURCE_DATASETS[source].map(freshness),
    companion_datasets: companion ? COMPANION_DATASETS.map(freshness) : [],
    last_attempt_at: LAST_SYNC_AT,
    calls_24h: source === 'google' ? 112 : null,
    call_cap: source === 'google' ? 1000 : null,
    sync_pending: false,
    recent_runs: recentRuns(source),
  } satisfies Source;
  return { ...base, ...overrides };
}

/**
 * `GET /sources`: Google, LinkedIn and HubSpot, in that order, each from {@link makeSource}.
 *
 * @returns a fresh list of three sources
 */
export function makeSources(): Source[] {
  return [makeSource('google'), makeSource('linkedin'), makeSource('hubspot')];
}

// ---------------------------------------------------------------------------------------------
// Decisions and discrepancies
// ---------------------------------------------------------------------------------------------

/** The default subject of each decision shape. */
const SHAPE_SUBJECTS: Readonly<Record<DecisionShape, Readonly<SubjectView>>> = Object.freeze({
  own_budget: Object.freeze({
    type: 'campaign',
    id: FIXTURE_CAMPAIGN_IDS.brandSearch,
    name: 'Brand Search — US',
    source: 'google',
    currency: 'USD',
  } satisfies SubjectView),
  shared_member: Object.freeze({
    type: 'campaign',
    id: FIXTURE_CAMPAIGN_IDS.searchPartners,
    name: 'Search Partners — US',
    source: 'google',
    currency: 'USD',
  } satisfies SubjectView),
  budget: Object.freeze({
    type: 'budget',
    id: FIXTURE_BUDGET_IDS.search,
    name: 'Search Shared Budget',
    source: 'google',
    currency: 'USD',
  } satisfies SubjectView),
});

/** A fresh copy of a shape's default subject. */
function subjectView(shape: DecisionShape): SubjectView {
  return { ...SHAPE_SUBJECTS[shape] };
}

/** The fields of a decision row its shape fixes: the id and the expected state. */
type ShapeState = Pick<
  Decision,
  'id' | 'status' | 'daily_budget' | 'total_budget' | 'budget_id' | 'budget_external_id'
>;

/** The decision ids and expected states of {@link makeDecision}, per shape (AAP 0.5.2). */
const SHAPE_STATES: Readonly<Record<DecisionShape, Readonly<ShapeState>>> = Object.freeze({
  own_budget: Object.freeze({
    id: 801,
    status: 'active',
    daily_budget: '200.00',
    total_budget: null,
    budget_id: null,
    budget_external_id: null,
  }),
  shared_member: Object.freeze({
    id: 802,
    status: 'active',
    daily_budget: null,
    total_budget: null,
    budget_id: FIXTURE_BUDGET_IDS.search,
    budget_external_id: '31000402',
  }),
  budget: Object.freeze({
    id: 803,
    status: null,
    daily_budget: '500.00',
    total_budget: null,
    budget_id: null,
    budget_external_id: null,
  }),
});

/**
 * The baseline decision of one subject, written by the first entity sync of the dataset
 * (AAP 0.5.2), with the complete expected state its shape owns:
 *
 * - `own_budget`: "Brand Search — US", status `active`, daily budget `200.00`;
 * - `shared_member`: "Search Partners — US", status `active`, assigned to the shared budget
 *   {@link FIXTURE_BUDGET_IDS}.search, whose Google id (`budget_external_id`) is `31000402`, no
 *   amount;
 * - `budget`: "Search Shared Budget", no status, daily budget `500.00`.
 *
 * A baseline row records no submitted patch (`patch: {}`) and no author (`decided_by: null`).
 *
 * @example
 * ```ts
 * // A user's budget-only patch on the own-budget campaign.
 * makeDecision('own_budget', {
 *   origin: 'user',
 *   decided_by: 'admin@example.com',
 *   daily_budget: '240.00',
 *   patch: { daily_budget: '240.00' },
 * });
 * ```
 *
 * @param shape - `own_budget` (default), `shared_member` or `budget`
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Decision`
 */
export function makeDecision(
  shape: DecisionShape = 'own_budget',
  overrides: Partial<Decision> = {},
): Decision {
  const base = {
    ...SHAPE_STATES[shape],
    subject: subjectView(shape),
    shape,
    tombstone: false,
    decided_at: '2026-10-01T08:12:00Z',
    decided_by: null,
    origin: 'baseline',
    recommendation_id: null,
    patch: {},
    note: null,
  } satisfies Decision;
  return { ...base, ...overrides };
}

/** When the fixture discrepancy was detected: the end of the 03:00 entity sync. */
const DISCREPANCY_DETECTED_AT = ENTITY_SYNC_END;

/** The complete expected state a shape owns, as a discrepancy side stores it. */
function fullState(shape: DecisionShape): DiscrepancyValue {
  switch (shape) {
    case 'own_budget':
      return {
        status: 'active',
        daily_budget: '200.00',
        total_budget: null,
        currency: 'USD',
        amount_micros: 200_000_000,
      };
    case 'shared_member':
      return {
        status: 'active',
        budget_id: FIXTURE_BUDGET_IDS.search,
        budget_external_id: '31000402',
      };
    case 'budget':
      return {
        daily_budget: '500.00',
        total_budget: null,
        currency: 'USD',
        amount_micros: 500_000_000,
      };
  }
}

/** The decided and platform sides of a discrepancy, fitting its kind and shape. */
function discrepancySides(
  kind: DiscrepancyKind,
  shape: DecisionShape,
): Pick<Discrepancy, 'decided_value' | 'platform_value'> {
  switch (kind) {
    case 'status':
      return { decided_value: { status: 'active' }, platform_value: { status: 'paused' } };
    case 'budget':
      if (shape === 'shared_member') {
        // A member's budget discrepancy is about its assignment, never an amount (AAP 0.5.2).
        return {
          decided_value: { budget_id: FIXTURE_BUDGET_IDS.search, budget_external_id: '31000402' },
          platform_value: { budget_id: FIXTURE_BUDGET_IDS.display, budget_external_id: '31000401' },
        };
      }
      if (shape === 'budget') {
        return {
          decided_value: fullState('budget'),
          platform_value: {
            daily_budget: '550.00',
            total_budget: null,
            currency: 'USD',
            amount_micros: 550_000_000,
          },
        };
      }
      return {
        decided_value: fullState('own_budget'),
        platform_value: {
          daily_budget: '250.00',
          total_budget: null,
          currency: 'USD',
          amount_micros: 250_000_000,
        },
      };
    case 'untracked':
      return { decided_value: null, platform_value: fullState(shape) };
    case 'missing':
      return { decided_value: fullState(shape), platform_value: null };
  }
}

/**
 * One open discrepancy as `GET /discrepancies` lists it, with exactly the resolutions its kind
 * allows ({@link FIXTURE_RESOLUTIONS}) and sides fitting its kind and shape:
 *
 * - `status` (default): "Brand Search — US", decided `active`, platform `paused`, the
 *   discrepancy behind the default page's C0 item ({@link FIXTURE_DISCREPANCY_ID});
 * - `budget`: decided `200.00`, platform `250.00` (own budget); `500.00` against `550.00` on a
 *   shared budget; a moved assignment on a shared member;
 * - `untracked`: no decided side; the platform side is the subject's complete state;
 * - `missing`: the decided complete state; no platform side.
 *
 * The shape is `overrides.shape` when given, else `budget` when `overrides.subject` is a budget,
 * else `own_budget`; the subject defaults to that shape's subject (see {@link makeDecision}).
 *
 * @param kind - `status` (default), `budget`, `untracked` or `missing`
 * @param overrides - fields to replace (shallow), for example `{ state: 'awaiting_platform' }`
 * @returns a fresh `Discrepancy`
 * @throws RangeError for a `status` discrepancy on a shared budget, which has no status and
 *   which the API therefore never produces (`decisions.RESOLUTIONS` has no such pair)
 */
export function makeDiscrepancy(
  kind: DiscrepancyKind = 'status',
  overrides: Partial<Discrepancy> = {},
): Discrepancy {
  const shape: DecisionShape =
    overrides.shape ?? (overrides.subject?.type === 'budget' ? 'budget' : 'own_budget');
  if (kind === 'status' && shape === 'budget') {
    throw new RangeError('fixtures: a shared budget has no status, so no status discrepancy');
  }
  const base = {
    id: FIXTURE_DISCREPANCY_ID,
    subject: subjectView(shape),
    shape,
    kind,
    ...discrepancySides(kind, shape),
    detected_at: DISCREPANCY_DETECTED_AT,
    state: 'open',
    resolved_at: null,
    resolution: null,
    resolutions: [...FIXTURE_RESOLUTIONS[kind]],
  } satisfies Discrepancy;
  return { ...base, ...overrides };
}


// ---------------------------------------------------------------------------------------------
// Default page: Keywords to adjust (Google, USD)
// ---------------------------------------------------------------------------------------------

/** The campaign and ad group every keyword item names (`keywords._campaign_extra`). */
function coreCrmExtra(): Record<string, unknown> {
  return {
    campaign: 'Core CRM — US',
    campaign_external_id: '21000303',
    ad_group: 'CRM Software',
  };
}

/** The account's search-term coverage, 74% of keyword spend (`keywords._Coverage.extra`). */
function coverageExtra(): Record<string, unknown> {
  return {
    search_term_spend: '2960.00',
    keyword_spend: '4000.00',
    percent: '74',
    below_minimum: true,
  };
}

/** The low-coverage flag every keyword item of the account carries (below 80%). */
function coverageFlag(): Flag {
  return makeFlag('search_term_coverage');
}

/** The money metrics of the Google account, in USD. */
const USD: MetricOptions = Object.freeze({ currency: 'USD' });

/** The money metrics of the EUR LinkedIn account. */
const EUR: MetricOptions = Object.freeze({ currency: 'EUR' });

/**
 * Keywords to adjust: one K1 block candidate (wasted spend, cost/conversion "—"), one K2
 * expansion candidate with adjacent terms, one K3 review, the search-term coverage finding and a
 * K1 term withheld below its minimum clicks.
 */
function keywordsSection(): Section {
  const k1 = makeItem({
    id: FIXTURE_ITEM_IDS.k1,
    rank: 1,
    rule_id: 'K1',
    action: 'Add as exact-match negative',
    reason:
      'Spent USD 186.40 over 28 days with 0 conversions across 64 clicks, at or above the ' +
      "minimum of 37 clicks for the account's conversion rate of 0.082 (conversions ÷ clicks), " +
      'and at least 1x the account cost/conversion of USD 92.50. Matched keyword: crm template ' +
      '(BROAD).',
    entity: entity(
      'google',
      'search_term',
      '61000201|7700112|free crm template',
      'free crm template',
    ),
    evidence: makeEvidence({
      metrics: [
        metric('term', 'Search term', 'free crm template', 'text'),
        metric('spend', 'Spend', '186.40', 'currency', USD),
        metric('conversions', 'Conversions', '0', 'conversions'),
        metric('cost_per_conversion', 'Cost/conversion', '—', 'currency', USD),
        metric('wasted_spend', 'Wasted spend', '186.40', 'currency', USD),
        metric('clicks', 'Clicks', 64, 'clicks'),
        metric('min_clicks', "Minimum clicks for the account's conversion rate", 37, 'clicks'),
        metric('matched_keyword', 'Matched keyword', 'crm template (BROAD)', 'text'),
      ],
      baselines: [
        baseline('account_cost_per_conversion', 'Account cost/conversion (USD)', '92.50'),
        baseline(
          'baseline_rate',
          'Account conversions per click, 2026-07-08 to 2026-10-05',
          '0.082',
        ),
        baseline('baseline_conversions', 'Baseline conversions', '410'),
        baseline('baseline_clicks', 'Baseline clicks', 5000),
      ],
      sample: { observed: 64, required: 37, unit: 'clicks', projected: null },
      thresholds: itemThresholds(
        'k1.baseline_days',
        'k1.min_clicks_floor',
        'k1.rule_of_three',
        'k1.min_baseline_conversions',
        'k1.min_spend_cpa_multiple',
        'keywords.min_search_term_coverage',
      ),
      datasets: coverage('google.search_terms', 'google.metrics_daily'),
      // mirrors backend/src/ads_engine/analysis/keywords.py (_k1_candidate)
      extra: {
        ...coreCrmExtra(),
        term_status: 'NONE',
        matched_criterion_id: '7700112',
        matched_keyword_text: 'crm template',
        matched_match_type: 'BROAD',
        negative_match_type: 'EXACT',
        min_spend_cpa_multiple: '1.0',
        min_clicks_label: "Minimum clicks for the account's conversion rate",
        baseline_window: { start: '2026-07-08', end: '2026-10-05', days: 90 },
        search_term_coverage: coverageExtra(),
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag(), coverageFlag()],
    current: currentFor(['google'], { incomplete: true }),
  });

  const k2 = makeItem({
    id: FIXTURE_ITEM_IDS.k2,
    rank: 1,
    rule_id: 'K2',
    action: 'Add as phrase-match keyword in CRM Software',
    reason:
      "4 conversions at USD 60.00 per conversion over 28 days, at or below 1x the campaign's " +
      'USD 84.00 per conversion. Adjacent terms to try, from your search-term report: crm for ' +
      'small teams, small business crm software, best crm for small business.',
    entity: entity(
      'google',
      'search_term',
      '61000201|7700113|crm for small business',
      'crm for small business',
    ),
    evidence: makeEvidence({
      metrics: [
        metric('term', 'Search term', 'crm for small business', 'text'),
        metric('spend', 'Spend', '240.00', 'currency', USD),
        metric('conversions', 'Conversions', '4', 'conversions'),
        metric('cost_per_conversion', 'Cost/conversion', '60.00', 'currency', USD),
        metric('clicks', 'Clicks', 52, 'clicks'),
        metric('matched_keyword', 'Matched keyword', 'small business crm (BROAD)', 'text'),
      ],
      baselines: [
        baseline('campaign_cost_per_conversion', 'Campaign cost/conversion (USD)', '84.00'),
        baseline('max_cost_per_conversion', 'Highest qualifying cost/conversion (USD)', '84.00'),
      ],
      sample: { observed: '4', required: '3', unit: 'conversions', projected: null },
      thresholds: itemThresholds(
        'k2.min_conversions',
        'k2.max_cpa_ratio',
        'k2.adjacent_jaccard',
        'k2.adjacent_max_terms',
        'k2.adjacent_min_clicks',
        'keywords.min_search_term_coverage',
      ),
      datasets: coverage('google.search_terms', 'google.metrics_daily', 'google.entities'),
      // mirrors backend/src/ads_engine/analysis/keywords.py (_k2_candidate)
      extra: {
        ...coreCrmExtra(),
        term_status: 'NONE',
        matched_criterion_id: '7700113',
        matched_keyword_text: 'small business crm',
        matched_match_type: 'BROAD',
        suggested_match_type: 'PHRASE',
        max_cpa_ratio: '1.0',
        adjacent_terms: [
          {
            term: 'crm for small teams',
            spend: '38.20',
            currency: 'USD',
            clicks: 9,
            conversions: '1',
          },
          {
            term: 'small business crm software',
            spend: '27.10',
            currency: 'USD',
            clicks: 7,
            conversions: '0',
          },
          {
            term: 'best crm for small business',
            spend: '19.80',
            currency: 'USD',
            clicks: 5,
            conversions: '0',
          },
        ],
        adjacent_terms_label: 'from your search-term report',
        search_term_coverage: coverageExtra(),
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag(), coverageFlag()],
    current: currentFor(['google'], { incomplete: true }),
  });

  const k3 = makeItem({
    id: FIXTURE_ITEM_IDS.k3,
    rank: 1,
    rule_id: 'K3',
    action: 'Review keyword: bid, match type or landing page',
    reason:
      'Mean quality score fell from 7.0 over the first 7 days to 5.8 over the last 7 days. CPC ' +
      'rose from USD 2.10 to USD 2.52 (1.2x) while cost/conversion rose from USD 75.00 to ' +
      'USD 97.20, 1.296x.',
    entity: entity('google', 'keyword', '61000201|7700114', 'crm software'),
    evidence: makeEvidence({
      metrics: [
        metric('term', 'Keyword', 'crm software', 'text'),
        metric('spend', 'Spend', '1205.40', 'currency', USD),
        metric('conversions', 'Conversions', '14', 'conversions'),
        metric('cost_per_conversion', 'Cost/conversion', '86.10', 'currency', USD),
        metric('clicks', 'Clicks', 520, 'clicks'),
        metric('qs_first', 'Mean quality score, first 7 days', '7.0', 'score'),
        metric('qs_last', 'Mean quality score, last 7 days', '5.8', 'score'),
        metric('cpc_first_half', 'CPC, first half', '2.10', 'currency', USD),
        metric('cpc_second_half', 'CPC, second half', '2.52', 'currency', USD),
        metric('cpc_ratio', 'CPC, second ÷ first half', '1.2', 'ratio'),
        metric(
          'cost_per_conversion_first_half',
          'Cost/conversion, first half',
          '75.00',
          'currency',
          USD,
        ),
        metric(
          'cost_per_conversion_second_half',
          'Cost/conversion, second half',
          '97.20',
          'currency',
          USD,
        ),
        metric('cpa_ratio', 'Cost/conversion, second ÷ first half', '1.296', 'ratio'),
        metric('conversions_first_half', 'Conversions, first half', '7', 'conversions'),
        metric('conversions_second_half', 'Conversions, second half', '7', 'conversions'),
        metric('clicks_first_half', 'Clicks, first half', 250, 'clicks'),
        metric('clicks_second_half', 'Clicks, second half', 270, 'clicks'),
      ],
      sample: { observed: 250, required: 30, unit: 'clicks per half', projected: null },
      thresholds: itemThresholds(
        'k3.min_window_days',
        'window.sustained_min_days',
        'k3.min_clicks_per_half',
        'k3.qs_span_days',
        'k3.min_qs_days_per_span',
        'k3.qs_drop',
        'k3.rise_ratio',
        'k3.min_first_half_conversions',
        'keywords.min_search_term_coverage',
      ),
      datasets: coverage('google.metrics_daily', 'google.entities', 'google.search_terms'),
      // mirrors backend/src/ads_engine/analysis/keywords.py (_k3_candidate)
      extra: {
        ...coreCrmExtra(),
        criterion_id: '7700114',
        match_type: 'PHRASE',
        triggers: ['quality_score', 'cpc'],
        qs_basis: 'daily',
        qs_days_first: 7,
        qs_days_last: 7,
        qs_spans: {
          first: { start: '2026-09-08', end: '2026-09-14' },
          last: { start: '2026-09-29', end: '2026-10-05' },
        },
        qs_snapshots: null,
        halves: {
          first: { start: '2026-09-08', end: '2026-09-21' },
          second: { start: '2026-09-22', end: '2026-10-05' },
        },
        conversions_fell: false,
        search_term_coverage: coverageExtra(),
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag(), coverageFlag()],
    current: currentFor(['google'], { incomplete: true }),
  });

  return makeSection('keywords', {
    groups: [
      makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [k1] }),
      makeGroup({ key: 'expand', title: 'Expansion candidates', items: [k2] }),
      makeGroup({ key: 'review', title: 'Review', items: [k3] }),
    ],
    findings: [makeFinding()],
    withheld: [makeWithheld()],
  });
}

// ---------------------------------------------------------------------------------------------
// Default page: Campaigns to pause or launch
// ---------------------------------------------------------------------------------------------

/**
 * The `no_crm_link` flag of Google items while the Google-to-CRM link is not configured
 * (`context.no_crm_link_flag`; AAP 0.3.3). Every Google card of the default page carries it
 * (K1, K2, K3, C0, C1, C3b, B1, B3), right after `simulated`; LinkedIn cards never do.
 */
function noCrmLinkFlag(): Flag {
  return makeFlag('no_crm_link');
}

/** What a C1 pause card shows (`campaigns._c1_candidate`). */
interface PauseSpec {
  readonly id: number;
  readonly rank: number;
  readonly campaignId: number;
  readonly externalId: string;
  readonly name: string;
  readonly spend: string;
  readonly spendDays: number;
  readonly spendDayShare: string;
  readonly expected: string;
  readonly held: boolean;
}

/**
 * A C1 "Pause" item on a Google campaign with steady spend and no conversion, against the
 * account's USD 92.50 per conversion; a held one also records the discrepancy hold.
 */
function pauseItem(spec: PauseSpec): Item {
  const flags = [simulatedFlag(), noCrmLinkFlag()];
  if (spec.held) {
    flags.push(makeFlag('unverified_discrepancies'));
  }
  return makeItem({
    id: spec.id,
    rank: spec.rank,
    rule_id: 'C1',
    action: 'Pause',
    reason:
      `Spent USD ${spec.spend} on ${String(spec.spendDays)} of 28 days with 0 conversions; at ` +
      `the account's USD 92.50 per conversion, ${spec.expected} conversions were expected.`,
    entity: entity('google', 'campaign', spec.externalId, spec.name),
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', spec.spend, 'currency', USD),
        metric('spend_days', 'Days with spend', spec.spendDays, 'days'),
        metric('window_days', 'Window days', 28, 'days'),
        metric('spend_day_share', 'Share of window days with spend', spec.spendDayShare, 'ratio'),
        metric('outcomes', 'Window conversions', 0, 'conversions'),
        metric(
          'expected_outcomes',
          'Expected conversions at the account baseline',
          spec.expected,
          'conversions',
        ),
      ],
      baselines: [
        baseline(
          'baseline_cost_per_outcome',
          'Account cost per conversion (same source and objective)',
          '92.50',
        ),
      ],
      sample: {
        observed: spec.expected,
        required: 3,
        unit: 'expected conversions',
        projected: null,
      },
      thresholds: itemThresholds(
        'c1.min_window_days',
        'c1.min_spend_day_share',
        'c1.min_expected_outcomes',
        'window.sustained_min_days',
      ),
      datasets: coverage('google.metrics_daily', 'google.entities'),
      // mirrors backend/src/ads_engine/analysis/campaigns.py (_c1_candidate)
      extra: { proposed_status: 'paused' },
    }),
    flags,
    held: spec.held,
    held_reason: spec.held ? HELD_REASON : null,
    decision_subject: { type: 'campaign', id: spec.campaignId },
    current: currentFor(['google'], { held: spec.held }),
  });
}

/**
 * Campaigns to pause or launch: the pinned C0 status discrepancy, a C1 pause with a decision
 * subject, a second C1 held until the discrepancy is resolved (last in its group), a C2 "Hold"
 * under the pipeline veto and a C3b launch idea.
 */
function campaignsSection(): Section {
  const c0 = makeItem({
    id: FIXTURE_ITEM_IDS.c0,
    rank: 1,
    rule_id: 'C0',
    action: 'Resolve: platform shows Paused, last decided Active',
    reason: 'Platform status differs from the last decided status.',
    entity: entity('google', 'campaign', '21000301', 'Brand Search — US'),
    evidence: makeEvidence({
      metrics: [
        metric('platform', 'Platform', 'Paused', 'status'),
        metric('decided', 'Last decided', 'Active', 'status'),
      ],
      // C0 records no threshold of its own; precedence still freezes the policy keys, because
      // the discrepancy reads google.entities.
      thresholds: itemThresholds(),
      datasets: coverage('google.entities'),
      // mirrors backend/src/ads_engine/analysis/discrepancy.py (_candidate)
      extra: {
        discrepancy_id: FIXTURE_DISCREPANCY_ID,
        kind: 'status',
        shape: 'own_budget',
        state: 'open',
        detected_at: DISCREPANCY_DETECTED_AT,
        decided: { status: 'active' },
        platform: { status: 'paused' },
        resolutions: [...FIXTURE_RESOLUTIONS.status],
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag()],
    // C0 offers its resolver instead of "Record decision" (AAP 0.5.5).
    decision_subject: null,
    current: currentFor(['google'], { c0: true }),
  });

  const c1 = pauseItem({
    id: FIXTURE_ITEM_IDS.c1,
    rank: 1,
    campaignId: FIXTURE_CAMPAIGN_IDS.genericCrm,
    externalId: '21000302',
    name: 'Generic CRM — US',
    spend: '314.50',
    spendDays: 26,
    spendDayShare: '0.929',
    expected: '3.4',
    held: false,
  });

  const heldPause = pauseItem({
    id: FIXTURE_ITEM_IDS.heldPause,
    rank: 2,
    campaignId: FIXTURE_CAMPAIGN_IDS.displayRemarketing,
    externalId: '21000304',
    name: 'Display Remarketing — US',
    spend: '296.00',
    spendDays: 25,
    spendDayShare: '0.893',
    expected: '3.2',
    held: true,
  });

  const c2Hold = makeItem({
    id: FIXTURE_ITEM_IDS.c2Hold,
    rank: 1,
    rule_id: 'C2',
    action: 'Hold: platform outcomes are not reaching Qualified Lead',
    reason:
      'Capped on 21 of 26 known-budget days with 48 leads at USD 31.25 per lead (account ' +
      'USD 40.00), but 26 eligible contacts from matured leads reached 0 Qualified Leads: hold ' +
      'the budget.',
    entity: entity('linkedin', 'campaign', '700000601', 'Lead Gen — Benchmarks US'),
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', '1500.00', 'currency', USD),
        metric('window_days', 'Window days', 28, 'days'),
        metric('outcomes', 'Window leads', 48, 'leads'),
        metric('cost_per_outcome', 'Cost per lead', '31.25', 'currency', USD),
        metric('daily_budget', 'Latest effective daily budget', '60.00', 'currency', USD),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.96', 'ratio'),
        metric('known_days', 'Known-budget days', 26, 'days'),
        metric('spending_days', 'Spending days', 26, 'days'),
        metric('capped_days', 'Capped days', 21, 'days'),
        metric('idle_days', 'Known idle days', 0, 'days'),
        metric('changed_intraday_days', 'Changed-intraday days (unknown budget)', 1, 'days'),
        metric('unobserved_days', 'Unobserved days (unknown budget)', 1, 'days'),
        metric('leads', 'LinkedIn leads (platform)', 48, 'leads'),
        metric('matured_leads', 'Matured leads (at least 14 days before window end)', 30, 'leads'),
        metric('matched_leads', 'Matched matured leads', 28, 'leads'),
        metric('unresolved', 'Unresolved matured leads', 2, 'leads'),
        metric('unresolved_share', 'Unresolved share of matured leads', '0.067', 'ratio'),
        metric('eligible_contacts', 'Eligible contacts', 26, 'contacts'),
        metric(
          'unknown_evidence_share',
          'Share of eligible contacts with unknown CRM evidence',
          '0',
          'ratio',
        ),
        metric('mql', 'MQL (First meeting booked)', 4, 'contacts'),
        metric('ql', 'Qualified Leads', 0, 'contacts'),
        metric('meetings', 'Meetings', 0, 'meetings'),
      ],
      baselines: [
        baseline(
          'baseline_cost_per_outcome',
          'Account cost per lead (same source and objective)',
          '40.00',
        ),
      ],
      sample: { observed: 26, required: 20, unit: 'eligible contacts', projected: null },
      thresholds: itemThresholds(
        'scale.min_outcomes',
        'scale.max_cpa_ratio',
        'b1.step',
        'budget.min_known_days',
        'budget.min_spending_days',
        'budget.min_capped_days',
        'budget.sustained_share',
        'budget.capped_day_ratio',
        'crm.veto_min_leads',
        'crm.lead_maturity_days',
        'attribution.floor_share',
        'crm.meeting_outcome_exclusions',
      ),
      datasets: coverage(
        'linkedin.analytics_daily',
        'linkedin.entities',
        'linkedin.leads',
        'hubspot.contacts',
        'hubspot.associations',
      ),
      provenance: linkedinProvenance(true),
      // mirrors backend/src/ads_engine/analysis/campaigns.py (C2 "Hold" outcome)
      extra: {
        outcome: 'Hold',
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
        veto: { fired: true, eligible_contacts: 26, required: 20, ql: 0, meetings: 0 },
      },
    }),
    flags: [simulatedFlag()],
    // A Hold writes no decision: the card offers "Mark done" (AAP 0.5.5).
    decision_subject: null,
    current: currentFor(['linkedin', 'hubspot']),
  });

  const c3b = makeItem({
    id: FIXTURE_ITEM_IDS.c3b,
    rank: 1,
    rule_id: 'C3b',
    action: "Create ad group or campaign for 'data warehouse'",
    reason:
      "3 converting search terms cluster around the words 'data warehouse' with 7 conversions " +
      'at USD 41.00 per conversion; no keyword, ad group or campaign name contains those words.',
    entity: entity('google', 'term_cluster', 'data warehouse', 'data warehouse'),
    evidence: makeEvidence({
      metrics: [
        metric('members', 'Converting search terms', 3, 'search terms'),
        metric('conversions', 'Cluster conversions', '7', 'conversions'),
        metric('spend', 'Cluster spend', '287.00', 'currency', USD),
        metric('clicks', 'Cluster clicks', 96, 'clicks'),
        metric('cost_per_conversion', 'Cluster cost per conversion', '41.00', 'currency', USD),
      ],
      sample: { observed: '7', required: 5, unit: 'conversions', projected: null },
      thresholds: itemThresholds(
        'c3b.jaccard',
        'c3b.min_terms',
        'c3b.min_conversions',
        'c3b.head_token_share',
      ),
      datasets: coverage('google.search_terms', 'google.entities'),
      // mirrors backend/src/ads_engine/analysis/campaigns.py (_c3b_candidate)
      extra: {
        head_tokens: ['data', 'warehouse'],
        members: [
          { term: 'cloud data warehouse', spend: '132.00', conversions: '3', clicks: 41 },
          { term: 'data warehouse tools', spend: '96.00', conversions: '2.5', clicks: 33 },
          { term: 'data warehouse pricing', spend: '59.00', conversions: '1.5', clicks: 22 },
        ],
        campaign_ids: [FIXTURE_CAMPAIGN_IDS.coreCrm],
        source_label: 'from your search-term report',
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag()],
    current: currentFor(['google'], { incomplete: true }),
  });

  return makeSection('campaigns', {
    groups: [
      makeGroup({ key: 'discrepancies', title: 'Discrepancies', items: [c0] }),
      makeGroup({ key: 'pause', title: 'Pause', items: [c1, heldPause] }),
      makeGroup({ key: 'scale', title: 'Scale', items: [c2Hold] }),
      makeGroup({ key: 'launch', title: 'Launch ideas', items: [c3b] }),
    ],
  });
}


// ---------------------------------------------------------------------------------------------
// Default page: Budget changes (Google, USD)
// ---------------------------------------------------------------------------------------------

/** The `budget.*` gate keys every budget card applies (`budget.BUDGET_THRESHOLD_KEYS`). */
const BUDGET_GATE_KEYS: readonly string[] = Object.freeze([
  'budget.max_observation_gap_hours',
  'budget.capped_day_ratio',
  'budget.capped_lost_is_share',
  'budget.high_utilization',
  'budget.under_utilization',
  'budget.min_known_days',
  'budget.min_spending_days',
  'budget.min_capped_days',
  'budget.sustained_share',
]);

/** The Google day boundary a budget card names (`extra.day_boundary`). */
function googleDayBoundary(): Record<string, unknown> {
  return { source: 'google', boundary: 'America/New_York' };
}

/**
 * Budget changes: a B1 increase on "Pipeline Search — US" (USD 200.00 → 240.00) of which
 * USD 30.00 is funded by reallocation and USD 10.00 by new budget; a B3 decrease on the shared
 * "Display Shared Budget" (two members) whose freed USD 30.00 funds that B1; a B4 weekday /
 * weekend pattern withheld for sample size; and one increase blocked by G1.
 */
function budgetSection(): Section {
  const b1 = makeItem({
    id: FIXTURE_ITEM_IDS.b1,
    rank: 1,
    rule_id: 'B1',
    action: 'Raise daily budget from USD 200.00 to USD 240.00',
    reason:
      'Capped on 22 of 26 known-budget days (window utilization 99%) with 62.5 conversions at ' +
      'USD 82.37 per conversion (account USD 95.00): raise the daily budget by 20% from ' +
      'USD 200.00 to USD 240.00. Raise by USD 40.00: USD 30.00 funded by reallocation from ' +
      'Display Shared Budget, USD 10.00 from new budget.',
    entity: entity('google', 'campaign', '21000305', 'Pipeline Search — US'),
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', '5148.00', 'currency', USD),
        metric('window_days', 'Window days', 28, 'days'),
        metric('outcomes', 'Window conversions', '62.5', 'conversions'),
        metric('cost_per_outcome', 'Cost per conversion', '82.37', 'currency', USD),
        metric('daily_budget', 'Latest effective daily budget', '200.00', 'currency', USD),
        metric('proposed_daily_budget', 'Proposed daily budget', '240.00', 'currency', USD),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.99', 'ratio'),
        metric('known_days', 'Known-budget days', 26, 'days'),
        metric('spending_days', 'Spending days', 26, 'days'),
        metric('spending_day_share', 'Share of known-budget days with spend', '1', 'ratio'),
        metric('capped_days', 'Capped days', 22, 'days'),
        metric(
          'capped_days_share',
          'Share of known-budget days that are capped days',
          '0.846',
          'ratio',
        ),
        metric('idle_days', 'Known idle days', 0, 'days'),
        metric('changed_intraday_days', 'Changed-intraday days (unknown budget)', 1, 'days'),
        metric('unobserved_days', 'Unobserved days (unknown budget)', 1, 'days'),
        metric('increase', 'Recommended increase', '40.00', 'currency', USD),
      ],
      baselines: [
        baseline(
          'baseline_cost_per_outcome',
          'Account cost per conversion (same source and objective)',
          '95.00',
        ),
        baseline('max_cost_per_outcome', 'Highest cost per outcome allowed to scale', '95.00'),
      ],
      sample: { observed: 26, required: 7, unit: 'known-budget days', projected: null },
      thresholds: itemThresholds(
        ...BUDGET_GATE_KEYS,
        'b1.step',
        'scale.min_outcomes',
        'scale.max_cpa_ratio',
        'window.sustained_min_days',
      ),
      datasets: coverage('google.metrics_daily', 'google.entities'),
      // Modelled on the B1 extra of backend/src/ads_engine/analysis/budget.py (budget_evidence,
      // _b1_candidate) and analysis/precedence.py (_with_b1_funding), not its exact payload:
      // `capped_share` carries ratios here, where `metrics.cap_constrained` records capped days
      // against whole days (22 of ⌈0.7 × 26⌉ = 19).
      extra: {
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
        subject_type: 'campaign',
        shared: false,
        currency: 'USD',
        objective_groups: ['google'],
        gate: {
          known_budget_days: { observed: 26, required: 7 },
          spending_days: { observed: 26, required: 5 },
          capped_days: { observed: 22, required: 5 },
          capped_share: { observed: '0.846', required: '0.7' },
        },
        gate_shortfalls: [],
        day_counts: {
          known_budget_days: 26,
          spending: 26,
          capped: 22,
          high: 24,
          under: 0,
          idle: 0,
          changed_intraday: 1,
          unobserved: 1,
        },
        observation_assumption: 'unchanged across 216 observations, at most 6 h apart',
        idle_dates: [],
        changed_intraday_dates: ['2026-09-17'],
        unobserved_dates: ['2026-09-08'],
        unknown_dates: ['2026-09-08', '2026-09-17'],
        day_boundary: googleDayBoundary(),
        current: '200.00',
        proposed: '240.00',
        increase: '40.00',
        step: '0.2',
        outcome: 'Scale',
        c2_link: {
          rule_id: 'C2',
          subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
        },
        allocations: [
          {
            donor: { type: 'budget', id: FIXTURE_BUDGET_IDS.display },
            donor_name: 'Display Shared Budget',
            amount: '30.00',
          },
        ],
        funded_by_reallocation: '30.00',
        from_new_budget: '10.00',
        funding:
          'Raise by USD 40.00: USD 30.00 funded by reallocation from Display Shared Budget, ' +
          'USD 10.00 from new budget',
      },
    }),
    flags: [simulatedFlag(), noCrmLinkFlag()],
    decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
    current: currentFor(['google']),
  });

  const b3 = makeItem({
    id: FIXTURE_ITEM_IDS.b3,
    rank: 2,
    rule_id: 'B3',
    action: 'Lower daily budget to USD 110.00',
    reason:
      'Under-utilized on 20 of 24 budget-active days (daily utilization at or below 50%; window ' +
      'utilization 44.3%); the 90th percentile of daily spend is USD 100.00 and 1.1x headroom ' +
      'gives USD 110.00: lower the daily budget from USD 140.00 to USD 110.00, freeing ' +
      "USD 30.00. Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1).",
    entity: entity('google', 'budget', '31000401', 'Display Shared Budget'),
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', '1488.00', 'currency', USD),
        metric('window_days', 'Window days', 28, 'days'),
        metric('outcomes', 'Window conversions', '31', 'conversions'),
        metric('cost_per_outcome', 'Cost per conversion', '48.00', 'currency', USD),
        metric('daily_budget', 'Latest effective daily budget', '140.00', 'currency', USD),
        metric('proposed_daily_budget', 'Proposed daily budget', '110.00', 'currency', USD),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.443', 'ratio'),
        metric('known_days', 'Budget-active days', 24, 'days'),
        metric('spending_days', 'Spending days', 24, 'days'),
        metric('spending_day_share', 'Share of budget-active days with spend', '1', 'ratio'),
        metric('under_utilized_days', 'Under-utilized days', 20, 'days'),
        metric(
          'under_utilized_days_share',
          'Share of budget-active days that are under-utilized days',
          '0.833',
          'ratio',
        ),
        metric('idle_days', 'Known idle days', 0, 'days'),
        metric('changed_intraday_days', 'Changed-intraday days (unknown budget)', 0, 'days'),
        metric('unobserved_days', 'Unobserved days (unknown budget)', 4, 'days'),
        metric(
          'spend_percentile',
          '90th percentile of daily spend on budget-active days',
          '100.00',
          'currency',
          USD,
        ),
        metric('freed', 'Freed daily amount', '30.00', 'currency', USD),
      ],
      baselines: [
        baseline(
          'baseline_cost_per_outcome',
          'Account cost per conversion (same source and objective)',
          '95.00',
        ),
      ],
      sample: { observed: 24, required: 7, unit: 'budget-active days', projected: null },
      thresholds: itemThresholds(
        ...BUDGET_GATE_KEYS,
        'b3.spend_percentile',
        'b3.headroom',
        'window.sustained_min_days',
      ),
      datasets: coverage('google.metrics_daily', 'google.entities'),
      // Modelled on the B3 extra of backend/src/ads_engine/analysis/budget.py (budget_evidence,
      // _b3) and analysis/precedence.py (_with_b3_allocations), not its exact payload:
      // `under_utilized_share` carries ratios here, where `metrics.under_utilized` records
      // under-utilized days as `under_utilized_days` against whole days (20 of ⌈0.7 × 24⌉ = 17),
      // and `day_counts` keys the budget-active days `known_budget_days`, where
      // `BudgetHistory.counts()` writes `known_active`.
      extra: {
        subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.display },
        subject_type: 'budget',
        shared: true,
        currency: 'USD',
        objective_groups: ['google'],
        gate: {
          known_budget_days: { observed: 24, required: 7 },
          spending_days: { observed: 24, required: 5 },
          under_utilized_share: { observed: '0.833', required: '0.7' },
        },
        gate_shortfalls: [],
        day_counts: {
          known_budget_days: 24,
          spending: 24,
          capped: 0,
          high: 0,
          under: 20,
          idle: 0,
          changed_intraday: 0,
          unobserved: 4,
        },
        observation_assumption: 'unchanged across 192 observations, at most 6 h apart',
        idle_dates: [],
        changed_intraday_dates: [],
        unobserved_dates: ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11'],
        unknown_dates: ['2026-09-08', '2026-09-09', '2026-09-10', '2026-09-11'],
        day_boundary: googleDayBoundary(),
        current: '140.00',
        proposed: '110.00',
        members: [
          {
            campaign_id: FIXTURE_CAMPAIGN_IDS.displayProspecting,
            name: 'Display Prospecting — US',
            member_days: 24,
          },
          {
            campaign_id: FIXTURE_CAMPAIGN_IDS.displayRetargeting,
            name: 'Display Retargeting — US',
            member_days: 18,
          },
        ],
        freed: '30.00',
        percentile: 90,
        headroom: '1.1',
        under_utilization: '0.5',
        allocated: '30.00',
        unallocated: '0.00',
        allocations: [
          {
            receiver: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
            receiver_name: 'Pipeline Search — US',
            amount: '30.00',
            text: "Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1)",
          },
        ],
      },
    }),
    flags: [
      simulatedFlag(),
      noCrmLinkFlag(),
      makeFlag('shared_budget'),
    ],
    // A shared budget's decision is written on the budget subject, never on its members.
    decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.display },
    current: currentFor(['google']),
  });

  return makeSection('budget', {
    groups: [makeGroup({ key: 'changes', title: 'Budget changes', items: [b1, b3] })],
    withheld: [
      makeWithheld({
        rule_id: 'B4',
        entity: entity('google', 'campaign', '21000302', 'Generic CRM — US'),
        reason: '3 weekend days of 4 required',
        sample: { observed: 3, required: 4, unit: 'weekend days' },
      }),
    ],
    suppressed: { g1_blocked: 1 },
  });
}

// ---------------------------------------------------------------------------------------------
// Default page: Copy/creative changes (LinkedIn, EUR)
// ---------------------------------------------------------------------------------------------

/** The campaign summary every creative item freezes (`creative._Campaign.summary`). */
function pipelineEmeaSummary(): Record<string, unknown> {
  return {
    id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
    external_id: '700000501',
    name: 'Lead Gen — Pipeline Report EMEA',
    objective_group: 'linkedin_lead_gen',
  };
}

/**
 * Copy/creative changes: one block for "Lead Gen — Pipeline Report EMEA" with the CR3 next
 * test first (CR2's weak stage as its reason, the variants table in `extra.variants`), then the
 * CR1 pair, which was computed from a LinkedIn dataset that has since been replaced, so it is
 * historical and every action on it is disabled.
 *
 * To render the historical card "since replaced by live", override the CR1 item's
 * `current.data_status_now` to `'live'`.
 */
function creativeSection(): Section {
  const cr3 = makeItem({
    id: FIXTURE_ITEM_IDS.cr3,
    rank: 1,
    rule_id: 'CR3',
    action:
      'Next test: change only headline; keep everything else identical; 2 variants; 35,427 ' +
      'impressions per variant to detect a 20% lift (α 0.05, power 0.8); ≈ 51 days at current ' +
      'per-variant delivery (projection)',
    reason: 'Click rate is below account baseline: 0.42% [0.35%–0.51%] vs 0.61%',
    entity: entity('linkedin', 'campaign', '700000501', 'Lead Gen — Pipeline Report EMEA'),
    evidence: makeEvidence({
      metrics: [
        metric('click_rate', 'Click rate', '0.00421', 'ratio'),
        metric('click_rate_ci_low', 'Click rate interval low', '0.00349', 'ratio'),
        metric('click_rate_ci_high', 'Click rate interval high', '0.00508', 'ratio'),
        metric('open_rate', 'Open rate', '0.01209', 'ratio'),
        metric('completion_rate', 'Completion rate', '0.36452', 'ratio'),
        metric(
          'sample_per_variant',
          'Impressions per variant for the next test',
          35427,
          'impressions',
        ),
        metric('projected_days', 'Days at current per-variant delivery (projection)', 51, 'days'),
        metric('variants', 'Variants', 2, 'variants'),
        metric('campaign_spend', 'Campaign spend', '2840.00', 'currency', EUR),
      ],
      baselines: [
        baseline('click_rate_baseline', 'Account click rate baseline', '0.0061'),
        baseline('open_rate_baseline', 'Account open rate baseline', '0.0115'),
        baseline('completion_rate_baseline', 'Account completion rate baseline', '0.34'),
      ],
      sample: { observed: 12400, required: 1000, unit: 'impressions', projected: null },
      thresholds: itemThresholds(
        'cr.min_impressions',
        'cr.min_variants',
        'cr.alpha',
        'cr.power',
        'cr.target_lift',
        'cr.ci_level',
      ),
      datasets: coverage('linkedin.analytics_daily', 'linkedin.entities'),
      provenance: linkedinProvenance(false),
      // mirrors backend/src/ads_engine/analysis/creative.py (_test_candidate, _variant_row)
      extra: {
        campaign: pipelineEmeaSummary(),
        variant_count: 2,
        variants: [
          {
            creative_id: 1101,
            urn: 'urn:li:sponsoredCreative:990001101',
            name: 'Variant A',
            headline: 'Benchmark your pipeline against 400 B2B teams',
            cta_label: 'Download',
            impressions: 13250,
            clicks: 58,
            form_opens: 162,
            leads: 64,
            click_rate: '0.004377',
            open_rate: '0.012226',
            completion_rate: '0.395062',
            cost: '1480.00',
            currency: 'EUR',
            outcomes: 64,
            cost_per_outcome: '23.13',
            delivery_days: 28,
          },
          {
            creative_id: 1102,
            urn: 'urn:li:sponsoredCreative:990001102',
            name: 'Variant B',
            headline: 'The 2026 B2B Pipeline Report is here',
            cta_label: 'Download',
            impressions: 12400,
            clicks: 50,
            form_opens: 148,
            leads: 49,
            click_rate: '0.004032',
            open_rate: '0.011935',
            completion_rate: '0.331081',
            cost: '1360.00',
            currency: 'EUR',
            outcomes: 49,
            cost_per_outcome: '27.76',
            delivery_days: 28,
          },
        ],
        excluded: [],
        stages: [
          { stage: 'click_rate', label: 'Click rate', successes: 108, trials: 25650, weak: true },
          { stage: 'open_rate', label: 'Open rate', successes: 310, trials: 25650, weak: false },
          {
            stage: 'completion_rate',
            label: 'Completion rate',
            successes: 113,
            trials: 310,
            weak: false,
          },
        ],
        weak_stage: {
          stage: 'click_rate',
          label: 'Click rate',
          rate: '0.00421',
          interval: ['0.00349', '0.00508'],
          baseline: '0.0061',
          shortfall: '0.31',
          weak: true,
          exploratory: false,
        },
        next_test: {
          stage: 'click_rate',
          variable: 'headline',
          variable_label: 'headline',
          keep_unchanged: ['intro text', 'call-to-action label', 'form questions', 'offer'],
          variants: 2,
          unit: 'impressions',
          per_variant: 35427,
          projected_impressions: null,
          days: 51,
        },
        campaign_spend: '2840.00',
        currency: 'EUR',
      },
    }),
    flags: [simulatedFlag()],
    current: currentFor(['linkedin']),
  });

  const pairLabel =
    'Variant A ahead, not yet distinguishable (adjusted p = 0.21); about 41,800 more ' +
    'impressions per variant needed at the observed difference';
  const cr1Historical = makeItem({
    id: FIXTURE_ITEM_IDS.cr1Historical,
    rank: 2,
    rule_id: 'CR1',
    action: `Variant A vs Variant B: ${pairLabel}`,
    reason:
      'Leads per impression: Variant A 0.53% (8 of 1,500) vs Variant B 0.25% (3 of 1,200); ' +
      "Fisher's exact test p = 0.21, Holm-adjusted across 1 pair in Lead Gen — Pipeline Report " +
      'EMEA = 0.21, against α 0.05; the gap is not yet distinguishable from noise.',
    entity: entity(
      'linkedin',
      'creative_pair',
      'urn:li:sponsoredCreative:990001101|urn:li:sponsoredCreative:990001102',
      'Variant A vs Variant B',
    ),
    evidence: makeEvidence({
      metrics: [
        metric('rate_a', 'Variant A leads per impression', '0.005333', 'ratio'),
        metric('rate_b', 'Variant B leads per impression', '0.0025', 'ratio'),
        metric('leads_a', 'Variant A leads', 8, 'leads'),
        metric('leads_b', 'Variant B leads', 3, 'leads'),
        metric('impressions_a', 'Variant A impressions', 1500, 'impressions'),
        metric('impressions_b', 'Variant B impressions', 1200, 'impressions'),
        metric('expected_min_cell', 'Smallest expected cell', '4.89', 'count'),
        metric(
          'additional_impressions_per_variant',
          'More impressions per variant needed at the observed difference',
          41800,
          'impressions',
        ),
      ],
      sample: { observed: 1200, required: 1000, unit: 'impressions', projected: null },
      test: { name: 'fisher_exact', p_value: '0.21', p_adjusted: '0.21', alpha: '0.05' },
      thresholds: itemThresholds(
        'cr.min_impressions',
        'cr.alpha',
        'cr.power',
        'cr.fisher_min_expected',
      ),
      datasets: coverage('linkedin.analytics_daily'),
      // Frozen provenance: the LinkedIn dataset this pair was computed from has been retired.
      provenance: [
        {
          source: 'linkedin',
          dataset_id: FIXTURE_DATASET_IDS.retiredLinkedin,
          mode: 'simulated',
          role: 'primary',
        },
      ],
      // mirrors backend/src/ads_engine/analysis/creative.py (CR1 pair candidate), less its
      // `variants` table, which the CR3 item ahead of it carries: tests read this item as a copy
      // item that carries no variants.
      extra: {
        campaign: pipelineEmeaSummary(),
        outcome: 'leads',
        pair: ['urn:li:sponsoredCreative:990001101', 'urn:li:sponsoredCreative:990001102'],
        names: ['Variant A', 'Variant B'],
        ahead: 'urn:li:sponsoredCreative:990001101',
        distinguishable: false,
        label: pairLabel,
        test: { method: 'fisher_exact', label: "Fisher's exact test", expected_min_cell: '4.89' },
        comparisons: 1,
        sample_needed: 41800,
        power: '0.8',
        power_reached_at_observed_difference: false,
        not_estimable_reason: null,
        variant_count: 2,
        excluded: [],
        campaign_spend: '2840.00',
        currency: 'EUR',
      },
    }),
    flags: [simulatedFlag(), makeFlag('not_distinguishable')],
    current: {
      ...currentFor(['linkedin']),
      held_now: true,
      held_reason_now: HISTORICAL_REASON,
      historical: true,
    },
  });

  return makeSection('creative', {
    groups: [
      makeGroup({
        key: `campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineEmea)}`,
        title: 'Lead Gen — Pipeline Report EMEA',
        items: [cr3, cr1Historical],
      }),
    ],
  });
}

// ---------------------------------------------------------------------------------------------
// Off the default page: a completion-rate next test (LinkedIn, EUR)
// ---------------------------------------------------------------------------------------------

/** How CR3 projects a form-open sample to impressions (`creative.NextTestPlan.to_json`). */
const OPEN_RATE_BASIS = 'projected through the observed open rate';

/**
 * The figures of the completion-rate next test for "Lead Gen — Benchmarks EMEA", computed with
 * `creative.plan_next_test` at the shipped `cr.*` values (α 0.05, power 0.8, lift 20%). Its two
 * variants pooled 976 form opens over 81,200 impressions (observed open rate 0.012020); detecting
 * a lift of the account completion baseline 0.34 to 0.408 needs 794 form opens per variant
 * (`stats.required_n`), which is ⌈794 × 81,200 ÷ 976⌉ = 66,059 impressions per variant, and
 * ⌈66,059 ÷ 1,450⌉ = 46 days at the current 1,450 impressions per variant per day.
 */
const COMPLETION_TEST = Object.freeze({
  pooledFormOpens: 976,
  formOpensPerVariant: 794,
  impressionsPerVariant: 66059,
  days: 46,
});

/**
 * A completion-rate sample in the tested stage's own denominator, form opens, with its labelled
 * projection to impressions (AAP 0.5.3 `Evidence.sample.projected`, 0.5.4.6 CR3, 0.5.4.8): the
 * 976 form opens the variants pooled against the 794 form opens per variant the next test needs,
 * and "≈ 66,059 impressions (projected through the observed open rate)", the figures of
 * {@link makeCompletionRateTestItem}. The projection is a fresh object on every call.
 *
 * @param overrides - fields to replace (shallow), for example `{ projected: null }`
 * @returns a fresh `Sample`
 */
export function makeProjectedSample(overrides: Partial<Sample> = {}): Sample {
  const base = {
    observed: COMPLETION_TEST.pooledFormOpens,
    required: COMPLETION_TEST.formOpensPerVariant,
    unit: 'form opens',
    projected: {
      value: COMPLETION_TEST.impressionsPerVariant,
      unit: 'impressions',
      basis: OPEN_RATE_BASIS,
    },
  } satisfies Sample;
  return { ...base, ...overrides };
}

/**
 * A CR3 next-test card (with CR2's weak stage as its reason) whose tested stage is the
 * completion rate, on "Lead Gen — Benchmarks EMEA" ({@link FIXTURE_CAMPAIGN_IDS}.benchmarksEmea,
 * EUR, simulated): change only the number of form questions, 794 form opens per variant,
 * projected to ≈ 66,059 impressions and ≈ 46 days. Every text and figure is what
 * `analysis/creative.py` (`_test_candidate`, `NextTestPlan.action` and `to_json`) renders for
 * these variants, and the sample, metrics and `extra.next_test` cite the same figures.
 *
 * Its `evidence.sample` is the tested stage's sample in form opens with the labelled projection
 * ({@link makeProjectedSample}), so the card's sample line renders the projection. The analysis
 * module's CR3 instead freezes its variant minimum (the smallest variant's impressions against
 * `cr.min_impressions`) as `sample`, and carries the same projection in its metrics
 * (`projected_impressions_per_variant`) and in `extra.next_test.projected`.
 *
 * The card is not on the default page. Its id ({@link FIXTURE_COMPLETION_TEST_ITEM_ID}),
 * campaign and entity name are used by no default-page item, so it can join one.
 *
 * @example
 * ```ts
 * // Add the card to the creative section in its own campaign block (ranked by spend: first).
 * const page = makeRecommendationsPage();
 * page.sections
 *   .find((section) => section.key === 'creative')
 *   ?.groups.unshift(
 *     makeGroup({
 *       key: `campaign:${String(FIXTURE_CAMPAIGN_IDS.benchmarksEmea)}`,
 *       title: 'Lead Gen — Benchmarks EMEA',
 *       items: [makeCompletionRateTestItem()],
 *     }),
 *   );
 * ```
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `Item`
 */
export function makeCompletionRateTestItem(overrides: Partial<Item> = {}): Item {
  const base = {
    id: FIXTURE_COMPLETION_TEST_ITEM_ID,
    rank: 1,
    rule_id: 'CR3',
    action:
      'Next test: change only number of form questions; keep everything else identical; 2 ' +
      'variants; 794 form opens per variant to detect a 20% lift (α 0.05, power 0.8); ' +
      `≈ 66,059 impressions per variant (${OPEN_RATE_BASIS}); ≈ 46 days at current ` +
      'per-variant delivery (projection)',
    reason: 'Completion rate is below account baseline: 24.18% [21.60%–26.96%] vs 34.00%',
    entity: entity('linkedin', 'campaign', '700000503', 'Lead Gen — Benchmarks EMEA'),
    evidence: makeEvidence({
      metrics: [
        metric('click_rate', 'Click rate', '0.006158', 'ratio'),
        metric('open_rate', 'Open rate', '0.012020', 'ratio'),
        metric('completion_rate', 'Completion rate', '0.241803', 'ratio'),
        metric('completion_rate_ci_low', 'Completion rate interval low', '0.215987', 'ratio'),
        metric('completion_rate_ci_high', 'Completion rate interval high', '0.269644', 'ratio'),
        metric(
          'sample_per_variant',
          'Form opens per variant for the next test',
          COMPLETION_TEST.formOpensPerVariant,
          'form opens',
        ),
        metric(
          'projected_impressions_per_variant',
          `Impressions per variant (${OPEN_RATE_BASIS})`,
          COMPLETION_TEST.impressionsPerVariant,
          'impressions',
        ),
        metric(
          'projected_days',
          'Days at current per-variant delivery (projection)',
          COMPLETION_TEST.days,
          'days',
        ),
        metric('variants', 'Variants', 2, 'variants'),
        metric('campaign_spend', 'Campaign spend', '3996.00', 'currency', EUR),
      ],
      baselines: [
        baseline('click_rate_baseline', 'Account click rate baseline', '0.006100'),
        baseline('open_rate_baseline', 'Account open rate baseline', '0.011500'),
        baseline('completion_rate_baseline', 'Account completion rate baseline', '0.340000'),
      ],
      sample: makeProjectedSample(),
      // creative.CR3_THRESHOLD_KEYS, in its order
      thresholds: itemThresholds(
        'cr.min_impressions',
        'cr.min_variants',
        'cr.ci_level',
        'cr.alpha',
        'cr.power',
        'cr.target_lift',
      ),
      datasets: coverage('linkedin.analytics_daily', 'linkedin.entities'),
      provenance: linkedinProvenance(false),
      // mirrors backend/src/ads_engine/analysis/creative.py (_test_candidate, _variant_row,
      // _Measure.to_json, _weak_stage_json, NextTestPlan.to_json)
      extra: {
        campaign: {
          id: FIXTURE_CAMPAIGN_IDS.benchmarksEmea,
          external_id: '700000503',
          name: 'Lead Gen — Benchmarks EMEA',
          objective_group: 'linkedin_lead_gen',
        },
        variant_count: 2,
        variants: [
          {
            creative_id: 1701,
            urn: 'urn:li:sponsoredCreative:990001701',
            name: 'Benchmarks Variant A',
            headline: 'Benchmark your paid media against 300 EMEA teams',
            cta_label: 'Download',
            impressions: 42000,
            clicks: 264,
            form_opens: 504,
            leads: 126,
            click_rate: '0.006286',
            open_rate: '0.012000',
            completion_rate: '0.250000',
            cost: '2016.00',
            currency: 'EUR',
            outcomes: 126,
            cost_per_outcome: '16.00',
            delivery_days: 28,
          },
          {
            creative_id: 1702,
            urn: 'urn:li:sponsoredCreative:990001702',
            name: 'Benchmarks Variant B',
            headline: 'Paid Media Benchmarks 2026: the EMEA edition',
            cta_label: 'Download',
            impressions: 39200,
            clicks: 236,
            form_opens: 472,
            leads: 110,
            click_rate: '0.006020',
            open_rate: '0.012041',
            completion_rate: '0.233051',
            cost: '1980.00',
            currency: 'EUR',
            outcomes: 110,
            cost_per_outcome: '18.00',
            delivery_days: 28,
          },
        ],
        excluded: [],
        stages: [
          {
            stage: 'click_rate',
            label: 'Click rate',
            successes: 500,
            trials: 81200,
            unit: 'impressions',
            rate: '0.006158',
            ci_low: '0.005642',
            ci_high: '0.006720',
            baseline: '0.006100',
            weak: false,
            shortfall: '-0.009508',
            unmeasured: null,
          },
          {
            stage: 'open_rate',
            label: 'Open rate',
            successes: COMPLETION_TEST.pooledFormOpens,
            trials: 81200,
            unit: 'impressions',
            rate: '0.012020',
            ci_low: '0.011293',
            ci_high: '0.012793',
            baseline: '0.011500',
            weak: false,
            shortfall: '-0.045217',
            unmeasured: null,
          },
          {
            stage: 'completion_rate',
            label: 'Completion rate',
            successes: 236,
            trials: COMPLETION_TEST.pooledFormOpens,
            unit: 'form opens',
            rate: '0.241803',
            ci_low: '0.215987',
            ci_high: '0.269644',
            baseline: '0.340000',
            weak: true,
            shortfall: '0.288815',
            unmeasured: null,
          },
        ],
        weak_stage: {
          stage: 'completion_rate',
          label: 'Completion rate',
          rate: '0.241803',
          interval: ['0.215987', '0.269644'],
          baseline: '0.340000',
          shortfall: '0.288815',
          weak: true,
          exploratory: false,
        },
        next_test: {
          stage: 'completion_rate',
          variable: 'form_questions',
          variable_label: 'number of form questions',
          keep_unchanged: ['headline', 'intro text', 'call-to-action label', 'offer'],
          variants: 2,
          unit: 'form opens',
          baseline: '0.340000',
          target: '0.408000',
          sample_needed: COMPLETION_TEST.formOpensPerVariant,
          not_estimable_reason: null,
          projected: {
            value: COMPLETION_TEST.impressionsPerVariant,
            unit: 'impressions',
            basis: OPEN_RATE_BASIS,
          },
          projected_unit: 'impressions',
          observed_open_rate: '0.012020',
          days: COMPLETION_TEST.days,
          projection_reason: null,
          exploratory: false,
        },
        campaign_spend: '3996.00',
        currency: 'EUR',
      },
    }),
    flags: [simulatedFlag()],
    data_status: 'simulated',
    held: false,
    held_reason: null,
    decision_subject: null,
    current: currentFor(['linkedin']),
  } satisfies Item;
  return { ...base, ...overrides };
}

// ---------------------------------------------------------------------------------------------
// Default page: White papers (LinkedIn, one table per currency cohort)
// ---------------------------------------------------------------------------------------------

/** The thresholds every white-paper item applies (`white_papers.THRESHOLD_KEYS`). */
const WHITE_PAPER_THRESHOLD_KEYS: readonly string[] = Object.freeze([
  'w.min_ql',
  'w.median_min_ql',
  'w.double_down_ratio',
  'w.retire_spend_multiple',
  'w.retire_min_window_days',
  'w.cohort_min_total_ql',
  'attribution.floor_share',
  'crm.booking_sources',
  'crm.meeting_outcome_exclusions',
]);

/** The datasets every white-paper item reads; `hubspot.*` are the companion CRM's. */
const WHITE_PAPER_DATASETS: readonly string[] = Object.freeze([
  'linkedin.analytics_daily',
  'linkedin.entities',
  'linkedin.leads',
  'hubspot.contacts',
  'hubspot.associations',
  'hubspot.deal_search',
]);

/** The MQL column header, with the bound stage label (`white_papers._mql_label`). */
const MQL_LABEL = 'MQL (First meeting booked)';

/** The meetings-booked label, with the booking-time proxy (`attribution.BOOKED_LABEL`). */
const MEETINGS_BOOKED_LABEL = 'Meetings booked (record created)';

/** `attribution.OTHER_MEETINGS_LABEL`: meetings whose ordering against the touch is unverified. */
const OTHER_MEETINGS_LABEL = 'Other meetings (ordering unverified)';

/** The cost-per-meeting label (`white_papers._row_metrics`). */
const COST_PER_MEETING_LABEL = 'Cost per meeting booked';

/** A CRM count as `attribution.CountValue.to_json` renders it; `null` is unavailable. */
function countJson(
  value: number | null,
  floor: boolean,
  reason: string | null = null,
): Record<string, unknown> {
  if (value === null) {
    return { value: null, floor: false, unavailable: true, reason, display: 'unavailable' };
  }
  return {
    value,
    floor,
    unavailable: false,
    reason: null,
    display: floor ? `≥ ${String(value)}` : String(value),
  };
}

/**
 * A cost as `attribution.CostValue.to_json` renders it: `null` for a known denominator of 0
 * ("—", with its note), else the amount, a ceiling ("≤ X") under a floor.
 */
function costJson(value: string | null, ceiling: boolean, note: string): Record<string, unknown> {
  if (value === null) {
    return { value: null, ceiling: false, note, unavailable: false, display: '—' };
  }
  return {
    value,
    ceiling,
    note: null,
    unavailable: false,
    display: ceiling ? `≤ ${value}` : value,
  };
}

/** The numbers of one white-paper row or per-creative row. */
interface CreditSpec {
  readonly spend: string;
  readonly leads: number;
  /** `null` when the MQL stage is unavailable. */
  readonly mql: number | null;
  readonly ql: number;
  readonly meetings: number;
  readonly other: number;
  readonly deals: number;
  /** `null` when the known QL count is 0 ("—", "no QL yet"). */
  readonly costPerQl: string | null;
  /** `null` when the known meeting count is 0 ("—", "no meetings yet"). */
  readonly costPerMeeting: string | null;
  /** Whether the counts are attribution floors (and the costs ceilings). */
  readonly floor: boolean;
}

/** One row of `evidence.extra.rows` (`attribution.CreditRow.to_json`); MQL and QL stay apart. */
function creditRow(
  creativeId: number | null,
  urn: string | null,
  label: string,
  spec: CreditSpec,
): Record<string, unknown> {
  const unknownCreative = creativeId === null;
  return {
    creative_id: creativeId,
    creative_urn: urn,
    label,
    spend: spec.spend,
    leads: spec.leads,
    mql: countJson(spec.mql, spec.floor, spec.mql === null ? MQL_UNAVAILABLE_REASON : null),
    ql: countJson(spec.ql, spec.floor),
    meetings_booked: countJson(spec.meetings, spec.floor),
    other_meetings: countJson(spec.other, spec.floor),
    deals: countJson(spec.deals, spec.floor),
    cost_per_ql: costJson(
      spec.costPerQl,
      spec.floor,
      unknownCreative ? 'the unknown creative row holds no spend' : 'no QL yet',
    ),
    cost_per_meeting: costJson(
      spec.costPerMeeting,
      spec.floor,
      unknownCreative ? 'the unknown creative row holds no spend' : 'no meetings yet',
    ),
  };
}

/**
 * The asset row's metrics in the table's column order (`white_papers._row_metrics`, AAP 0.5.5):
 * spend, LinkedIn leads, MQL, Qualified Lead, meetings booked, other meetings, deals after
 * touch, cost per Qualified Lead, cost per meeting. A zero denominator gives the cost "—".
 */
function assetMetrics(currency: 'EUR' | 'USD', spec: CreditSpec): Metric[] {
  const money: MetricOptions = { currency };
  const counted: MetricOptions = { floor: spec.floor };
  return [
    metric('spend', 'Spend', spec.spend, 'currency', money),
    metric('linkedin_leads', 'LinkedIn leads (platform)', spec.leads, 'leads'),
    metric('mql', MQL_LABEL, spec.mql, 'contacts', counted),
    metric('ql', 'Qualified Lead', spec.ql, 'contacts', counted),
    metric('meetings_booked', MEETINGS_BOOKED_LABEL, spec.meetings, 'meetings', counted),
    metric('other_meetings', OTHER_MEETINGS_LABEL, spec.other, 'meetings', counted),
    metric('deals_after_touch', 'Deals after touch', spec.deals, 'deals', counted),
    metric('cost_per_ql', 'Cost per Qualified Lead', spec.costPerQl ?? '—', 'currency', {
      currency,
      floor: spec.floor && spec.costPerQl !== null,
    }),
    metric('cost_per_meeting', COST_PER_MEETING_LABEL, spec.costPerMeeting ?? '—', 'currency', {
      currency,
      floor: spec.floor && spec.costPerMeeting !== null,
    }),
  ];
}

/** The EUR cohort's cross-asset view, carried by each row (`white_papers._cohort_extra`). */
function eurCohortExtra(): Record<string, unknown> {
  return {
    currency: 'EUR',
    assets: 2,
    spend_total: '4340.00',
    overlap_note: 'counts overlap across assets',
    median_cost_per_ql: '611.67',
    double_down_ceiling: '489.33',
    retire_spend_floor: '1835.00',
  };
}

/**
 * The portal's orphan-deal share over the assessed deals, as the backend serialises it
 * (`attribution.OrphanDealShare.to_json`: `assessed`, `orphans`, `not_assessed`, `share` bounded
 * to six decimals, `unknown`, `label`, `reason`): 0 of 4 assessed deals without a contact, every
 * deal assessed, so the share is known and its label reads as `attribution._orphan_deals` words
 * it, "{percent}% ({orphans} of {N} deals without a contact)", the percentage rendered as
 * `attribution._percent` does (at most one decimal, trailing zeros dropped).
 */
function orphanDealsExtra(): Record<string, unknown> {
  return {
    assessed: 4,
    orphans: 0,
    not_assessed: 0,
    share: '0',
    unknown: false,
    label: '0% (0 of 4 deals without a contact)',
    reason: null,
  };
}

/**
 * White papers: the EUR cohort with a W1 "Double down" under a 12% contact floor (every count
 * "≥ N", every cost "≤ X", per-creative rows including "unknown creative") and a "No change
 * warranted" row; the USD cohort with an "Insufficient data" row whose MQL is unavailable and
 * whose costs are "—" (no QL, no meetings yet); a `skipped_stage` finding; and the USD cohort's
 * withheld line. MQL and QL are separate metrics everywhere; no field combines them.
 */
function whitePapersSection(): Section {
  const wEur = makeItem({
    id: FIXTURE_ITEM_IDS.wEur,
    rank: 1,
    rule_id: 'W1',
    action: 'Double down: increase spend on Lead Gen — Pipeline Report EMEA',
    reason:
      'Cost per Qualified Lead ≤ 473.33 is at or below the double-down ceiling of EUR 489.33 ' +
      '(0.8 x the EUR cohort median of EUR 611.67), with ≥ 6 Qualified Leads (W1 needs 3).',
    entity: entity(
      'linkedin',
      'content_asset',
      'b2b-pipeline-report-2026|EUR',
      'The 2026 B2B Pipeline Report',
    ),
    evidence: makeEvidence({
      metrics: assetMetrics('EUR', {
        spend: '2840.00',
        leads: 116,
        mql: 14,
        ql: 6,
        meetings: 3,
        other: 1,
        deals: 1,
        costPerQl: '473.33',
        costPerMeeting: '946.67',
        floor: true,
      }),
      baselines: [
        baseline(
          'cohort_median_cost_per_ql',
          'Cohort median cost per Qualified Lead (EUR)',
          '611.67',
        ),
      ],
      sample: { observed: 6, required: 3, unit: 'Qualified Leads', projected: null },
      thresholds: itemThresholds(...WHITE_PAPER_THRESHOLD_KEYS),
      datasets: coverage(...WHITE_PAPER_DATASETS),
      provenance: linkedinProvenance(true),
      // mirrors backend/src/ads_engine/analysis/white_papers.py (_row_extra, _decide_with_ql)
      extra: {
        asset: {
          slug: 'b2b-pipeline-report-2026',
          title: 'The 2026 B2B Pipeline Report',
          currency: 'EUR',
        },
        mql_label: 'First meeting booked',
        ql_label: 'Qualified Lead',
        rows: [
          creditRow(1101, 'urn:li:sponsoredCreative:990001101', 'Variant A', {
            spend: '1480.00',
            leads: 64,
            mql: 8,
            ql: 4,
            meetings: 2,
            other: 1,
            deals: 1,
            costPerQl: '370.00',
            costPerMeeting: '740.00',
            floor: true,
          }),
          creditRow(1102, 'urn:li:sponsoredCreative:990001102', 'Variant B', {
            spend: '1360.00',
            leads: 49,
            mql: 5,
            ql: 2,
            meetings: 1,
            other: 0,
            deals: 0,
            costPerQl: '680.00',
            costPerMeeting: '1360.00',
            floor: true,
          }),
          creditRow(null, null, 'unknown creative', {
            spend: '0',
            leads: 3,
            mql: 1,
            ql: 0,
            meetings: 0,
            other: 0,
            deals: 0,
            costPerQl: null,
            costPerMeeting: null,
            floor: true,
          }),
        ],
        shares: {
          contact: { share: '0.1207', count: 14, of: 116 },
          unknown_evidence: { share: '0', count: 0, of: 102 },
          creative: { share: '0.0259', count: 3, of: 116 },
        },
        orphan_deals: orphanDealsExtra(),
        already_before: { mql: 1, ql: 0, meetings: 0, deals: 0 },
        skipped: { mql: 2, ql: 0 },
        costs: {
          cost_per_ql: costJson('473.33', true, 'no QL yet'),
          cost_per_meeting: costJson('946.67', true, 'no meetings yet'),
        },
        campaigns: [
          { id: FIXTURE_CAMPAIGN_IDS.pipelineEmea, name: 'Lead Gen — Pipeline Report EMEA' },
        ],
        cohort: eurCohortExtra(),
        double_down_campaigns: [
          { id: FIXTURE_CAMPAIGN_IDS.pipelineEmea, name: 'Lead Gen — Pipeline Report EMEA' },
        ],
        blocked_campaigns: [],
      },
    }),
    // Its one other meeting (ordering unverified) carries meeting_time_proxy (P8).
    flags: [simulatedFlag(), makeFlag('attribution_floor'), makeFlag('meeting_time_proxy')],
    current: currentFor(['linkedin', 'hubspot']),
  });

  const wEurNoChange = makeItem({
    id: FIXTURE_ITEM_IDS.wEurNoChange,
    rank: 2,
    rule_id: 'W',
    action: 'No change warranted',
    reason:
      '2 Qualified Leads, below the 3 W1 needs; cost per Qualified Lead 750.00 against the EUR ' +
      'cohort median of EUR 611.67.',
    entity: entity(
      'linkedin',
      'content_asset',
      'attribution-playbook|EUR',
      'The Attribution Playbook',
    ),
    evidence: makeEvidence({
      metrics: assetMetrics('EUR', {
        spend: '1500.00',
        leads: 31,
        mql: 5,
        ql: 2,
        meetings: 1,
        other: 0,
        deals: 0,
        costPerQl: '750.00',
        costPerMeeting: '1500.00',
        floor: false,
      }),
      baselines: [
        baseline(
          'cohort_median_cost_per_ql',
          'Cohort median cost per Qualified Lead (EUR)',
          '611.67',
        ),
      ],
      // P6: a QL count below W1's minimum is a failed gate, never the item's sample.
      sample: null,
      thresholds: itemThresholds(...WHITE_PAPER_THRESHOLD_KEYS),
      datasets: coverage(...WHITE_PAPER_DATASETS),
      provenance: linkedinProvenance(true),
      // mirrors backend/src/ads_engine/analysis/white_papers.py (_row_extra, _no_change)
      extra: {
        asset: { slug: 'attribution-playbook', title: 'The Attribution Playbook', currency: 'EUR' },
        mql_label: 'First meeting booked',
        ql_label: 'Qualified Lead',
        rows: [
          creditRow(1301, 'urn:li:sponsoredCreative:990001301', 'The attribution gap, explained', {
            spend: '1500.00',
            leads: 31,
            mql: 5,
            ql: 2,
            meetings: 1,
            other: 0,
            deals: 0,
            costPerQl: '750.00',
            costPerMeeting: '1500.00',
            floor: false,
          }),
        ],
        shares: {
          contact: { share: '0.0645', count: 2, of: 31 },
          unknown_evidence: { share: '0', count: 0, of: 29 },
          creative: { share: '0', count: 0, of: 31 },
        },
        orphan_deals: orphanDealsExtra(),
        already_before: { mql: 0, ql: 0, meetings: 0, deals: 0 },
        skipped: { mql: 0, ql: 0 },
        costs: {
          cost_per_ql: costJson('750.00', false, 'no QL yet'),
          cost_per_meeting: costJson('1500.00', false, 'no meetings yet'),
        },
        campaigns: [
          { id: FIXTURE_CAMPAIGN_IDS.attributionEmea, name: 'Lead Gen — Attribution EMEA' },
        ],
        cohort: eurCohortExtra(),
        w1_sample: { observed: 2, required: 3, unit: 'Qualified Leads' },
      },
    }),
    flags: [simulatedFlag()],
    current: currentFor(['linkedin', 'hubspot']),
  });

  const wUsd = makeItem({
    id: FIXTURE_ITEM_IDS.wUsd,
    rank: 1,
    rule_id: 'W',
    action: 'Insufficient data',
    reason:
      "The USD cohort's white papers have 0 Qualified Leads together, fewer than the 3 needed " +
      'for a stable median cost per Qualified Lead.',
    entity: entity(
      'linkedin',
      'content_asset',
      'paid-media-benchmarks-2026|USD',
      'Paid Media Benchmarks 2026',
    ),
    evidence: makeEvidence({
      metrics: assetMetrics('USD', {
        spend: '1210.00',
        leads: 41,
        mql: null,
        ql: 0,
        meetings: 0,
        other: 0,
        deals: 0,
        costPerQl: null,
        costPerMeeting: null,
        floor: false,
      }),
      sample: null,
      thresholds: itemThresholds(...WHITE_PAPER_THRESHOLD_KEYS),
      datasets: coverage(...WHITE_PAPER_DATASETS),
      provenance: linkedinProvenance(true),
      // mirrors backend/src/ads_engine/analysis/white_papers.py (_row_extra, _insufficiency)
      extra: {
        asset: {
          slug: 'paid-media-benchmarks-2026',
          title: 'Paid Media Benchmarks 2026',
          currency: 'USD',
        },
        mql_label: 'First meeting booked',
        ql_label: 'Qualified Lead',
        rows: [
          creditRow(1601, 'urn:li:sponsoredCreative:990001601', 'Where paid media budgets went', {
            spend: '1210.00',
            leads: 41,
            mql: null,
            ql: 0,
            meetings: 0,
            other: 0,
            deals: 0,
            costPerQl: null,
            costPerMeeting: null,
            floor: false,
          }),
        ],
        shares: {
          contact: { share: '0.0488', count: 2, of: 41 },
          unknown_evidence: { share: '0', count: 0, of: 39 },
          creative: { share: '0', count: 0, of: 41 },
        },
        orphan_deals: orphanDealsExtra(),
        already_before: { mql: 0, ql: 0, meetings: 0, deals: 0 },
        skipped: { mql: 0, ql: 0 },
        costs: {
          cost_per_ql: costJson(null, false, 'no QL yet'),
          cost_per_meeting: costJson(null, false, 'no meetings yet'),
        },
        campaigns: [{ id: FIXTURE_CAMPAIGN_IDS.benchmarksUs, name: 'Lead Gen — Benchmarks US' }],
        cohort: {
          currency: 'USD',
          assets: 1,
          spend_total: '1210.00',
          overlap_note: 'counts overlap across assets',
          median_cost_per_ql: null,
          double_down_ceiling: null,
          retire_spend_floor: null,
        },
        cohort_sample: {
          observed: 0,
          required: 3,
          unit: "Qualified Leads across the cohort's assets",
        },
      },
    }),
    flags: [simulatedFlag(), makeFlag('pipeline_unavailable')],
    current: currentFor(['linkedin', 'hubspot']),
  });

  return makeSection('white_papers', {
    groups: [
      makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [wEur, wEurNoChange] }),
      makeGroup({ key: 'cohort:USD', title: 'USD', items: [wUsd] }),
    ],
    findings: [
      makeFinding({
        code: 'skipped_stage',
        section: 'white_papers',
        entity: entity(
          'linkedin',
          'content_asset',
          'b2b-pipeline-report-2026|EUR',
          'The 2026 B2B Pipeline Report',
        ),
        message: 'MQL (First meeting booked): 2 contacts skipped this stage (not counted)',
        evidence: {
          stage: 'MQL',
          label: 'First meeting booked',
          property: 'hs_v2_date_entered_1084512345',
          skipped: 2,
          asset: 'b2b-pipeline-report-2026',
          currency: 'EUR',
        },
      }),
    ],
    withheld: [
      makeWithheld({
        rule_id: 'W',
        entity: null,
        reason:
          'W1 and W2 not evaluated for the USD cohort: 0 Qualified Leads of 3 required for a ' +
          'stable median cost per Qualified Lead',
        sample: { observed: 0, required: 3, unit: 'Qualified Leads' },
      }),
    ],
  });
}

// ---------------------------------------------------------------------------------------------
// The default recommendations page
// ---------------------------------------------------------------------------------------------

/** The builder of each section of the default page. */
const SECTION_BUILDERS: Readonly<Record<SectionKey, () => Section>> = Object.freeze({
  keywords: keywordsSection,
  campaigns: campaignsSection,
  budget: budgetSection,
  creative: creativeSection,
  white_papers: whitePapersSection,
});

/**
 * `GET /recommendations` for the latest default run: {@link makeRun}, {@link makeOverlay}, the
 * first four {@link FIXTURE_BANNERS} (no short-window banner: the run spans 28 days) and the
 * five sections in `SECTION_ORDER`, each with every card variant its section renders. Ranks
 * ascend within each group, held cards come last in theirs, entity names are unique, every item
 * is simulated, and the one open C0 discrepancy matches `overlay.open_discrepancies`.
 *
 * The rows are chosen to exercise each rendering rather than to be one reconciled account: the
 * USD white paper's MQL is unavailable to render "unavailable", while the EUR rows and the C2
 * "Hold" card show known MQL counts.
 *
 * @example
 * ```ts
 * // A short custom window: flag the run, add the short-window banner, and flag every item (P5).
 * const page = makeRecommendationsPage({
 *   run: makeRun({ id: CUSTOM_RUN_ID, kind: 'custom', days: 10, short_window: true }),
 *   banners: [...FIXTURE_BANNERS],
 * });
 * pageItems(page).forEach((item) => item.flags.push(makeFlag('short_window')));
 * ```
 *
 * @param overrides - fields to replace (shallow)
 * @returns a fresh `RecommendationsPage`; every call builds new objects
 */
export function makeRecommendationsPage(
  overrides: Partial<RecommendationsPage> = {},
): RecommendationsPage {
  const base = {
    run: makeRun(),
    overlay: makeOverlay(),
    banners: FIXTURE_BANNERS.slice(0, 4).map((banner) => ({ ...banner })),
    sections: SECTION_ORDER.map((key) => SECTION_BUILDERS[key]()),
  } satisfies RecommendationsPage;
  return { ...base, ...overrides };
}

/**
 * Every item of a page, section by section and group by group, in render order.
 *
 * @param page - the page to read
 * @returns the items, flattened
 */
export function pageItems(page: RecommendationsPage): Item[] {
  return page.sections.flatMap((section) => section.groups.flatMap((group) => group.items));
}

/**
 * The item with `id` on a page, for example `findPageItem(page, FIXTURE_ITEM_IDS.b1)`.
 *
 * @param page - the page to read
 * @param id - the item id, usually one of {@link FIXTURE_ITEM_IDS}
 * @returns the item
 * @throws Error when the page holds no item with that id, so a test never asserts on
 *   `undefined`
 */
export function findPageItem(page: RecommendationsPage, id: Item['id']): Item {
  const item = pageItems(page).find((candidate) => candidate.id === id);
  if (item === undefined) {
    throw new Error(`fixtures: no item ${String(id)} on the page`);
  }
  return item;
}
