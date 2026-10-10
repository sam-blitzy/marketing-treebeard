/**
 * TypeScript mirror of the `/api/v1` REST contract (AAP 0.5.3).
 *
 * `backend/src/ads_engine/api/schemas.py` is the source of truth: every interface here mirrors
 * one Pydantic model of that module field for field, with the same snake_case names, the same
 * nullability and the same `Literal` values. The file is hand-written against the OpenAPI
 * document FastAPI generates from those models (AAP 0.3.1); nothing is code-generated.
 *
 * Conventions, decided once here so every consumer reads the same contract:
 *
 * - **Money** is a decimal string ({@link Money}), never a JavaScript number, so amounts keep
 *   their exact `numeric(18,6)` precision. This module converts nothing; `lib/format.ts`
 *   formats the string in the account currency, and amounts are never summed across
 *   currencies (AAP 0.1.1).
 * - **Dates** are source-native calendar dates ({@link IsoDate}), never re-bucketed into
 *   another source's day (AAP 0.5.4.1). **Instants** are ISO 8601 with an offset
 *   ({@link IsoDateTime}).
 * - **Nullability.** Every field of a response model is always present: the API serialises a
 *   `None` default as `null` and never omits the key, and its published response schema lists
 *   every such field as required. A Pydantic field typed `X | None`, with or without a `None`
 *   default, is therefore a required `field: X | null` here. `?` marks only a request field the
 *   client may omit and a key of a stored-JSON view (see **Open JSON**), which appears only
 *   when the stored object holds it. A missing number is `null` ("unavailable"), never `0`
 *   (AAP 0.11.1).
 * - **Requests** are closed: the API answers an unknown field with 422. The decided fields of
 *   {@link DecisionPatchIn} and {@link IntendedStateIn} must be omitted rather than sent as
 *   `null`, because a decided field cannot be cleared.
 * - **Ids** carry exactly the type `schemas.py` gives them (`int` → `number`). Other modules
 *   refer to them as `Run['id']`, {@link RunId} and so on, so the choice stays local to this
 *   file.
 * - **Open JSON** (frozen evidence `extra`, thresholds) is typed `Record<string, unknown>`;
 *   features narrow it where they read it. No `any` is used. Stored decision JSON is typed:
 *   a decision's patch is a {@link DecisionPatchView} and each discrepancy side a
 *   {@link DiscrepancyValue}, whose keys are optional because the API returns exactly the
 *   keys the stored object holds.
 *
 * The module is type-only apart from {@link SECTION_ORDER}, and imports nothing.
 *
 * @example
 * ```ts
 * import type { RecommendationsPage, Item } from '../api/types';
 *
 * function heldItems(page: RecommendationsPage): Item[] {
 *   return page.sections
 *     .flatMap((section) => section.groups.flatMap((group) => group.items))
 *     .filter((item) => item.current.held_now);
 * }
 * ```
 */

// ---------------------------------------------------------------------------------------------
// Scalar aliases
// ---------------------------------------------------------------------------------------------

/**
 * An exact amount in one account's currency: the `Money` annotated `Decimal` of `schemas.py`,
 * serialised as a fixed-point decimal string such as `"120.500000"`, never in exponent
 * notation and never as a JSON number.
 *
 * Requests send amounts as plain decimal strings (`"120.50"`): ASCII digits with an optional
 * fraction, and no sign, spaces or exponent, exactly the published pattern. The API refuses any
 * other string with 422, and a JSON number with a fraction, because it has already lost
 * precision. Validation to the currency's minor unit is server-side (AAP 0.5.2).
 */
export type Money = string;

/**
 * A calendar date, `YYYY-MM-DD` (Python `date`). Daily dates are each source's native date:
 * Google in the customer account's time zone, LinkedIn in UTC; they are shown as stored,
 * labelled with their source's day boundary (AAP 0.5.4.1, 0.5.5).
 */
export type IsoDate = string;

/**
 * An instant, ISO 8601 with a UTC offset (Pydantic `AwareDatetime`), for example
 * `2026-10-06T06:00:00Z`. Formatted for display in `DISPLAY_TIMEZONE`, which never moves a day
 * boundary (AAP 0.5.4.1).
 */
export type IsoDateTime = string;

/**
 * A value copied from frozen evidence (`schemas.MetricValue`): a decimal string for exact
 * numbers (money, rates, shares, fractional Google conversions), an integer for counts, a
 * pre-rendered string such as `"n/a"`, a boolean, or `null` for unavailable. `null` renders
 * "unavailable", never `0` (AAP 0.11.1).
 */
export type MetricValue = string | number | boolean | null;

/**
 * A withheld pattern's observed or required sample size (`schemas.SampleSize`): an integer for a
 * whole-number size, as {@link Sample} carries counts, a fixed-point decimal string for a
 * fractional one (`"2.900000"`), or `null` when no sample applies (never `0`). Never a boolean.
 */
export type SampleSize = number | string | null;

// ---------------------------------------------------------------------------------------------
// Vocabularies (each equal to its `Literal` in schemas.py and the database tuple behind it)
// ---------------------------------------------------------------------------------------------

/** An external data source (`schemas.SourceName`, `db.models.ops.SOURCES`), in display order. */
export type SourceName = 'google' | 'linkedin' | 'hubspot';

/**
 * Whether a dataset holds real platform data or seeded mock data in the live wire shape
 * (`schemas.Mode`, `db.models.ops.MODES`; AAP 0.3.3).
 */
export type DataMode = 'live' | 'simulated';

/** The name `schemas.py` gives {@link DataMode}, kept so both files can be searched alike. */
export type Mode = DataMode;

/**
 * An item's data status (`schemas.DataStatus`, `db.models.analysis.DATA_STATUSES`). `mixed`
 * marks evidence joining a live and a simulated dataset (AAP 0.3.3).
 */
export type DataStatus = 'live' | 'simulated' | 'stale' | 'mixed';

/**
 * `default`: the worker's run of the configured default window. `custom`: any explicit window
 * from `POST /runs` or the CLI, even one equal to the default (`schemas.RunKind`; AAP 0.5.3).
 */
export type RunKind = 'default' | 'custom';

/** What started a run (`schemas.RunTrigger`, `db.models.analysis.RUN_TRIGGERS`). */
export type RunTrigger = 'scheduled' | 'event' | 'manual';

/** Run lifecycle (`schemas.RunStatus`, `db.models.analysis.RUN_STATUSES`). */
export type RunStatus = 'running' | 'succeeded' | 'failed';

/**
 * Every badge code an item can carry (`schemas.FlagCode`, `analysis.context.FLAG_CODES`;
 * AAP 0.5.3 `Flag`), grouped as data state, discrepancies, attribution, pipeline and
 * statistics, and budget scope.
 */
export type FlagCode =
  | 'simulated'
  | 'stale'
  | 'incomplete'
  | 'short_window'
  | 'unverified_discrepancies'
  | 'attribution_floor'
  | 'creative_floor'
  | 'deal_floor'
  | 'unknown_crm_evidence'
  | 'meeting_time_proxy'
  | 'pipeline_unavailable'
  | 'no_crm_link'
  | 'not_distinguishable'
  | 'search_term_coverage'
  | 'shared_budget';

/**
 * Every non-action observation code a rule can store (`schemas.FindingCode`,
 * `analysis.context.FINDING_CODES`; AAP 0.5.3 `Finding`).
 */
export type FindingCode =
  | 'no_crm_link'
  | 'search_term_coverage'
  | 'k1_no_baseline'
  | 'segment_window_unavailable'
  | 'pipeline_unavailable'
  | 'skipped_stage'
  | 'conversion_tracking_absent'
  | 'incomplete_dataset';

/**
 * A banner or badge tone (`schemas.BannerTone`, `analysis.context.TONES`), the tones the
 * design-system `Badge` and `Banner` wrappers accept (AAP 0.4.2).
 */
export type BannerTone = 'neutral' | 'info' | 'warning' | 'critical' | 'success';

/**
 * The global banner codes the API emits today, in their fixed render order
 * (`analysis.context.BANNER_ORDER`; AAP 0.5.5). {@link Banner.code} stays `string`, as in
 * `schemas.py`, so a code added later still type-checks; this union lets features compare
 * against known codes, such as `open_discrepancies`, the only banner rendered with the `alert`
 * role.
 */
export type BannerCode =
  | 'open_discrepancies'
  | 'tracking_absent'
  | 'simulated_sources'
  | 'stale_datasets'
  | 'incomplete_datasets'
  | 'short_window';

/** What a decision or discrepancy is about (`schemas.SubjectType`; AAP 0.5.2). */
export type SubjectType = 'campaign' | 'budget';

/**
 * The decision shape fixed by the subject's current state (`schemas.DecisionShape`; AAP 0.5.2):
 * `own_budget` (status plus the campaign's own budget fields), `shared_member` (status plus its
 * shared-budget assignment, never an amount) or `budget` (one amount, no status).
 */
export type DecisionShape = 'own_budget' | 'shared_member' | 'budget';

/** Who wrote a decision row (`schemas.DecisionOrigin`, `db.models.decisions.DECISION_ORIGINS`). */
export type DecisionOrigin = 'baseline' | 'user' | 'recommendation' | 'resolution';

/**
 * A campaign's normalised status (`schemas.CampaignStatus`,
 * `db.models.accounts.CAMPAIGN_STATUSES`).
 */
export type CampaignStatus = 'active' | 'paused' | 'ended';

/** The C0 discrepancy kinds (`schemas.DiscrepancyKind`; AAP 0.5.4.4). */
export type DiscrepancyKind = 'status' | 'budget' | 'untracked' | 'missing';

/**
 * Discrepancy lifecycle (`schemas.DiscrepancyState`). `awaiting_platform` waits for a later
 * snapshot that matches the decision (AAP 0.5.4.4).
 */
export type DiscrepancyState = 'open' | 'awaiting_platform' | 'resolved';

/**
 * The resolution API keys (`schemas.ResolutionKey`, `db.models.decisions.DISCREPANCY_RESOLUTIONS`;
 * AAP 0.5.4.4), in their fixed display order. Which keys a discrepancy accepts depends on its
 * kind and subject shape and is listed per discrepancy in {@link Discrepancy.resolutions}.
 */
export type Resolution =
  | 'platform_correct'
  | 'decision_correct'
  | 'adopt_platform'
  | 'record_intended'
  | 'acknowledge_removal';

/** The name `schemas.py` gives {@link Resolution}, kept so both files can be searched alike. */
export type ResolutionKey = Resolution;

/**
 * Feedback on an item (`schemas.FeedbackKind`; AAP 0.5.2): each kind suppresses the rule and
 * entity until the returned {@link Feedback.until}, which is authoritative. The lengths are
 * configurable as `feedback.dismiss_days`, `feedback.snooze_days` and `feedback.done_days` in
 * `config/thresholds.yaml`, with defaults of 30, 7 and 30 days. C0 items accept none of them.
 */
export type FeedbackKind = 'dismiss' | 'snooze' | 'done';

/** Completeness of a dataset over its covered range (`schemas.Completeness`; AAP 0.3.2). */
export type Completeness = 'complete' | 'incomplete' | 'unknown';

/**
 * Health of a source connection (`schemas.ConnectionStatus`,
 * `db.models.ops.CONNECTION_STATUSES`).
 */
export type ConnectionStatus = 'ok' | 'stale' | 'denied' | 'error';

/** Dataset lifecycle (`schemas.DatasetState`; AAP 0.3.3): backfilled while `building`. */
export type DatasetState = 'building' | 'active' | 'retired';

/**
 * A source's own dataset (`primary`), or the simulated CRM `companion` of a simulated LinkedIn
 * dataset (`schemas.DatasetRole`; AAP 0.3.3).
 */
export type DatasetRole = 'primary' | 'companion';

/** Outcome of one sync job run (`schemas.SyncRunStatus`, `db.models.ops.SYNC_RUN_STATUSES`). */
export type SyncRunStatus = 'running' | 'succeeded' | 'failed';

/**
 * How a CRM count is bound in `config/hubspot_mapping.yaml`: a lifecycle stage label, or a
 * contact date property (`schemas.BindingMode`, `analysis.thresholds.BindingMode`; AAP 0.5.4.7).
 */
export type BindingMode = 'stage_label' | 'property';

/**
 * The freshness-tracked dataset names (`db.models.ops.DATASETS`; AAP 0.3.2). Each rule declares
 * the datasets it reads, and an item's `stale` and `incomplete` flags derive from those only.
 * The API types every dataset field as a plain string, so fields here are `string`; this union
 * is for consumers that list or compare known names.
 */
export type DatasetName =
  | 'google.entities'
  | 'google.metrics_daily'
  | 'google.search_terms'
  | 'linkedin.entities'
  | 'linkedin.analytics_daily'
  | 'linkedin.segments'
  | 'linkedin.leads'
  | 'hubspot.contacts'
  | 'hubspot.associations'
  | 'hubspot.deal_search'
  | 'hubspot.google_contacts';

/**
 * The five sections of `/actions`, in the fixed order the API returns them and the UI renders
 * them: Keywords to adjust, Campaigns to pause or launch, Budget changes, Copy/creative changes,
 * White papers (AAP 0.5.5).
 *
 * Must equal `RECOMMENDATION_SECTIONS` in `backend/src/ads_engine/db/models/analysis.py`, in
 * order; `schemas.py` checks its own `SectionKey` against that tuple at import.
 */
export const SECTION_ORDER = [
  'keywords',
  'campaigns',
  'budget',
  'creative',
  'white_papers',
] as const;

/**
 * A section key (`schemas.SectionKey`): `'keywords' | 'campaigns' | 'budget' | 'creative' |
 * 'white_papers'`. Derived from {@link SECTION_ORDER}, so the union and the order can never
 * drift apart.
 */
export type SectionKey = (typeof SECTION_ORDER)[number];

// ---------------------------------------------------------------------------------------------
// Errors (AAP 0.5.3 error shape)
// ---------------------------------------------------------------------------------------------

/** The body of an error response (`schemas.ErrorDetail`). */
export interface ErrorDetail {
  /** Machine-readable code, for example `subject_retired` or `run_in_progress`. */
  code: string;
  /** Human-readable explanation. */
  message: string;
}

/**
 * Every error the API returns: `{"error": {"code", "message"}}` (`schemas.ErrorResponse`;
 * AAP 0.5.3). Statuses: 401 unauthenticated, 403 missing CSRF header, 404, 409 conflict,
 * 422 validation, 429 too many login attempts.
 */
export interface ErrorResponse {
  /** The error's code and message. */
  error: ErrorDetail;
}

// ---------------------------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------------------------

/**
 * `POST /auth/login` body (`schemas.LoginRequest`). The server trims and lowercases `email`
 * and uses `password` exactly as typed.
 */
export interface LoginRequest {
  /** The admin email (1–320 characters; no NUL character or unpaired surrogate). */
  email: string;
  /** The password (1–1024 characters), never trimmed. */
  password: string;
}

/**
 * The signed-in user (`schemas.Me`): `GET /auth/me`, `POST /auth/login` and
 * `POST /auth/logout`.
 */
export interface Me {
  /** The admin email. */
  email: string;
}

// ---------------------------------------------------------------------------------------------
// Runs (AAP 0.5.3 `Run`)
// ---------------------------------------------------------------------------------------------

/**
 * One source's date range and native day boundary within a window (`schemas.SourceDay`;
 * AAP 0.5.4.1). Google and LinkedIn appear; HubSpot holds instants only.
 */
export interface SourceDay {
  /** The day source. */
  source: SourceName;
  /** First native date, inclusive. */
  start: IsoDate;
  /** Last native date, inclusive. */
  end: IsoDate;
  /** The IANA zone whose midnight ends the source's days, such as `America/New_York` or `UTC`. */
  boundary: string;
}

/**
 * Each source's mode as a run read it, frozen with the run (`schemas.DataModes`). A source with
 * no active dataset at compute time is `null`.
 */
export interface DataModes {
  /** Google's mode at compute time. */
  google: DataMode | null;
  /** LinkedIn's mode at compute time. */
  linkedin: DataMode | null;
  /** HubSpot's mode at compute time. */
  hubspot: DataMode | null;
}

/**
 * Each source's mode now, read live by the overlay (`schemas.SourceModes`; AAP 0.3.2). A source
 * before its first dataset is `null`.
 */
export interface SourceModes {
  /** Google's current mode. */
  google: DataMode | null;
  /** LinkedIn's current mode. */
  linkedin: DataMode | null;
  /** HubSpot's current mode. */
  hubspot: DataMode | null;
}

/**
 * Run metadata (`schemas.Run`; AAP 0.5.3): `GET /runs/latest`, `GET /runs/{id}`, `GET /runs`,
 * `POST /runs` and the page header. `/runs/latest` is always the latest succeeded `default`
 * run; a custom run is addressed by id (`/actions?run=<id>`).
 */
export interface Run {
  /** The `analysis_run` id. */
  id: number;
  /** `default` or `custom`. */
  kind: RunKind;
  /** First calendar date of the window. */
  window_start: IsoDate;
  /** Last calendar date of the window, inclusive. */
  window_end: IsoDate;
  /** Days in the window, both ends included. */
  days: number;
  /** Each day source's range and native day boundary, Google then LinkedIn. */
  source_days: SourceDay[];
  /** `DISPLAY_TIMEZONE`, used only to format instants. */
  display_timezone: string;
  /** Whether `days` is below `trusted_min_days` (AAP 0.5.4.1). */
  short_window: boolean;
  /** The `window.default_days` the run was computed with, frozen with the run. */
  default_days: number;
  /**
   * The `window.trusted_min_days` the run was computed with, frozen with the run; the
   * short-window banner quotes it.
   */
  trusted_min_days: number;
  /** `scheduled`, `event` or `manual`. */
  trigger: RunTrigger;
  /** Each source's mode as the run read it. */
  data_modes: DataModes;
  /** Hash of the configuration files the run used. */
  config_hash: string;
  /** `running`, `succeeded` or `failed`. */
  status: RunStatus;
  /** When the run finished; `null` while running. */
  finished_at: IsoDateTime | null;
}

/** The id of a {@link Run} (`analysis_run.id`), as path parameters and query keys use it. */
export type RunId = Run['id'];

/**
 * `POST /runs` body (`schemas.CreateRunRequest`): a custom window of calendar dates applied to
 * each source's native days. The span runs from 1 day to `window.max_days`, and the end is no
 * later than the default window end; a violation is a 422, and a run already in progress a 409
 * `run_in_progress` (AAP 0.5.3).
 */
export interface CreateRunRequest {
  /** First calendar date, inclusive. */
  window_start: IsoDate;
  /** Last calendar date, inclusive. */
  window_end: IsoDate;
}

// ---------------------------------------------------------------------------------------------
// Evidence (AAP 0.5.3 `Evidence`, 0.5.4.8): frozen at compute time, never rewritten
// ---------------------------------------------------------------------------------------------

/** A badge on an item (`schemas.Flag`; AAP 0.5.3). */
export interface Flag {
  /** The flag code. */
  code: FlagCode;
  /** The badge's explanatory text, for example "Data as of 2026-10-01 03:10". */
  message: string;
}

/**
 * The entity an item, finding or withheld pattern is about (`schemas.EntityView`). `id` is an
 * opaque string in one namespace for items, findings and withheld patterns: the platform's
 * external id, or the composite key of an entity without a table row, so a withheld pattern or
 * finding names its entity exactly as that entity's items do. A field is `null` when the stored
 * entity does not carry it.
 */
export interface EntityView {
  /** The entity's source. */
  source: SourceName | null;
  /** The entity type, for example `campaign`, `search_term` or `content_asset`. */
  type: string | null;
  /** The identifier, always a string. */
  id: string | null;
  /** The display name. */
  name: string | null;
}

/**
 * The exact window an item was computed over (`schemas.EvidenceWindow`; AAP 0.5.4.8). When the
 * sources' day boundaries differ, `boundaries_differ` says so, so misaligned buckets are never
 * presented as one common day (AAP 0.5.4.1).
 */
export interface EvidenceWindow {
  /** First calendar date. */
  start: IsoDate;
  /** Last calendar date, inclusive. */
  end: IsoDate;
  /** Days in the window. */
  days: number;
  /** Each day source's range and native day boundary. */
  source_days: SourceDay[];
  /**
   * Whether the sources' day boundaries differ, for example Google on `America/New_York` and
   * LinkedIn on `UTC`; `null` on evidence frozen by a run that predates this key.
   */
  boundaries_differ: boolean | null;
  /**
   * Each source's range and boundary as one sentence, for example "Google 2026-09-04 to
   * 2026-10-01 (America/New_York); LinkedIn same dates (UTC)"; `null` on evidence frozen by a run
   * that predates this key.
   */
  description: string | null;
}

/**
 * One number an item's reason cites (`schemas.Metric`). A `null` value, or `unavailable: true`,
 * must render "unavailable", never `0`. A `floor` value renders "≥ N" for counts and "≤ X" for
 * costs (AAP 0.5.4.7).
 */
export interface Metric {
  /** Stable machine key, for example `spend` or `cost_per_conversion`. */
  key: string;
  /** Display label. */
  label: string;
  /** The value; `null` means unavailable. */
  value: MetricValue;
  /** The unit, for example `currency`, `clicks`, `ratio` or `days`; `null` when unitless. */
  unit: string | null;
  /** ISO currency code for money values. */
  currency: string | null;
  /** Whether the value is an attribution floor. */
  floor: boolean | null;
  /** Whether the value is unavailable. */
  unavailable: boolean | null;
}

/** A value an item's metrics were compared against (`schemas.Baseline`). */
export interface Baseline {
  /** Stable machine key, for example `account_cost_per_outcome`. */
  key: string;
  /** Display label. */
  label: string;
  /** The baseline; `null` when unavailable. */
  value: MetricValue;
}

/**
 * A labelled projection beside a sample (`schemas.Projection`), for example CR3's impressions
 * per variant projected through the observed open rate (AAP 0.5.4.6).
 */
export interface Projection {
  /** The projected figure. */
  value: MetricValue;
  /** Its unit, for example `impressions` or `days`. */
  unit: string;
  /** How it was projected, for example "projected through the observed open rate". */
  basis: string;
}

/**
 * A sample size against its minimum, in the unit the rule uses (`schemas.Sample`;
 * AAP 0.5.4.8), rendered as "observed / required".
 */
export interface Sample {
  /** The sample observed. */
  observed: MetricValue;
  /** The minimum the rule requires. */
  required: MetricValue;
  /** The unit, for example `clicks`, `impressions`, `form opens` or `days`. */
  unit: string;
  /** A labelled projection; `null` when there is none. */
  projected: Projection | null;
}

/** The statistical test behind an item (`schemas.StatTest`; AAP 0.5.4.6). */
export interface StatTest {
  /** The test, for example "Fisher's exact test". */
  name: string;
  /** The raw p-value; `null` when it could not be computed. */
  p_value: MetricValue;
  /** The Holm-adjusted p-value when a correction applies; `null` otherwise. */
  p_adjusted: MetricValue;
  /** The significance level the result is judged at. */
  alpha: MetricValue;
}

/**
 * A dataset an item read, with its completeness and coverage at compute time
 * (`schemas.DatasetCoverage`).
 */
export interface DatasetCoverage {
  /** The dataset name, one of {@link DatasetName}. */
  dataset: string;
  /** `complete`, `incomplete` or `unknown`. */
  completeness: Completeness;
  /** The last native date the dataset covered; `null` when unknown. */
  covered_through: IsoDate | null;
}

/**
 * One dataset an item read, as it was when the run computed it (`schemas.ProvenanceEntry`;
 * AAP 0.3.2). Frozen: it is never replaced by the current source status, which travels in
 * {@link ItemOverlay.data_status_now}. A provenance dataset that is no longer active makes the
 * item historical.
 */
export interface ProvenanceEntry {
  /** The dataset's source. */
  source: SourceName;
  /** The `source_dataset` id. */
  dataset_id: number;
  /** `live` or `simulated` at compute time. */
  mode: DataMode;
  /** `primary`, or `companion` for the simulated CRM of a simulated LinkedIn dataset. */
  role: DatasetRole;
}

/**
 * Everything an item's conclusion rests on, frozen with the run (`schemas.Evidence`;
 * AAP 0.5.4.8): the exact window with each source's day boundary, every cited number, the
 * baselines, the sample against its minimum, the test, the thresholds applied, the datasets
 * read and their provenance.
 *
 * `extra` carries rule-specific detail and stays `unknown`-typed; features narrow it where they
 * read it. `extra.headline_metric` names the `key` of the one metric in `metrics` the card leads
 * with (AAP 0.1.2; K1's is `wasted_spend`), and is absent on evidence that selects none.
 * White-paper items put their per-creative rows in `extra.rows`, expansion items (K2) their
 * adjacent terms in `extra.adjacent_terms`, and C0 items `extra.discrepancy_id`, `extra.kind`,
 * `extra.shape`, `extra.state`, `extra.decided`, `extra.platform` and `extra.resolutions`.
 */
export interface Evidence {
  /** The exact window and each source's day boundary. */
  window: EvidenceWindow;
  /** Every number the reason cites, with units and currency. */
  metrics: Metric[];
  /** The values those metrics were compared against. */
  baselines: Baseline[];
  /** The sample size against its minimum, when the rule is sample-gated. */
  sample: Sample | null;
  /** The statistical test, when one was used. */
  test: StatTest | null;
  /** `{dotted threshold key: configured value}` for every threshold applied. */
  thresholds: Record<string, unknown>;
  /** The datasets read, with completeness and coverage. */
  datasets: DatasetCoverage[];
  /** Each dataset read, with its id, mode and role at compute time. */
  provenance: ProvenanceEntry[];
  /** Rule-specific detail. */
  extra: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------------------------
// Recommendations page (AAP 0.5.3 `RecommendationsPage`): items, sections and the overlay
// ---------------------------------------------------------------------------------------------

/**
 * An item's current state, computed at read time and kept apart from its frozen evidence
 * (`schemas.ItemOverlay`; AAP 0.3.2 "current-state overlay").
 *
 * When `historical` is true, a dataset the item read is no longer active: the card shows
 * "Computed from replaced data", and every action on it is disabled (Record decision, Mark
 * done, Dismiss, Snooze and the C0 resolver).
 */
export interface ItemOverlay {
  /**
   * Whether the item is held now, for any of three causes: it is `historical` (a dataset it
   * read is no longer active, or its provenance cannot be verified); it was `held` at compute
   * time, a frozen hold the overlay never lifts; or, for every rule but C0, a subject it
   * depends on has an `open` or `awaiting_platform` discrepancy now, including one detected
   * after the run.
   */
  held_now: boolean;
  /** Why it is held now, for example "Resolve discrepancy first". */
  held_reason_now: string | null;
  /** Whether the item was computed from data that has since been replaced. */
  historical: boolean;
  /** The data status of the datasets the item reads, as they are now. */
  data_status_now: DataStatus;
  /** Flags that apply now, such as `unverified_discrepancies` or `stale`. */
  flags_now: Flag[];
  /** The active feedback hiding the item, if any. */
  hidden_by: FeedbackKind | null;
}

/**
 * The campaign or shared budget a "Record decision" on an item writes
 * (`schemas.DecisionSubjectRef`). Only C1, C1b, C2 "Scale", B1, B2 and B3 items carry one
 * (AAP 0.5.5).
 */
export interface DecisionSubjectRef {
  /** `campaign` or `budget` (a Google explicitly shared budget). */
  type: SubjectType;
  /** The subject's internal id in its dataset. */
  id: number;
}

/** One ranked action card (`schemas.Item`; AAP 0.5.3 `Item`, 0.5.5 recommendation card). */
export interface Item {
  /** The `recommendation` id, used by `POST /recommendations/{id}/feedback`. */
  id: number;
  /** Rank within its group, from 1. */
  rank: number;
  /** The rule that produced it, for example `K1` or `C0`. */
  rule_id: string;
  /** The recommended action, shown as the card's headline. */
  action: string;
  /** The reason sentence, rendered from the evidence. */
  reason: string;
  /** The entity the item is about. */
  entity: EntityView;
  /** The frozen evidence. */
  evidence: Evidence;
  /** Flags recorded at compute time; {@link ItemOverlay.flags_now} adds the current ones. */
  flags: Flag[];
  /** The data status at compute time. */
  data_status: DataStatus;
  /** Whether the item was held at compute time. */
  held: boolean;
  /** Why it was held. */
  held_reason: string | null;
  /**
   * The subject a "Record decision" on it writes, carried only by C1, C1b, C2 "Scale", B1, B2
   * and B3 items. `null` means no "Record decision": a C0 discrepancy shows its resolver instead
   * of feedback actions, and any other item offers "Mark done" in its place. On a historical
   * item (`current.historical`) every action is disabled, the C0 resolver included.
   */
  decision_subject: DecisionSubjectRef | null;
  /** The read-time overlay. */
  current: ItemOverlay;
}

/** The id of an {@link Item} (`recommendation.id`), as the feedback route takes it. */
export type RecommendationId = Item['id'];

/**
 * A group of items within a section (`schemas.Group`), for example `block`, `discrepancies`,
 * `campaign:{id}` for a creative block or `cohort:{currency}` for a white-paper table. Keys are
 * dynamic, so they are strings.
 */
export interface Group {
  /** The group key. */
  key: string;
  /** Display title. */
  title: string;
  /** The items, in rank order. */
  items: Item[];
}

/** A non-action observation a rule emitted and the run stored (`schemas.Finding`; AAP 0.5.3). */
export interface Finding {
  /** The finding code. */
  code: FindingCode;
  /** The section the finding is shown in. */
  section: SectionKey;
  /** The entity it is about; `null` for a section- or account-wide finding. */
  entity: EntityView | null;
  /** The rendered message. */
  message: string;
  /** The numbers behind the message. */
  evidence: Record<string, unknown>;
}

/**
 * The sample a withheld pattern saw against the minimum it needed (`schemas.WithheldSample`).
 * Each size is a {@link SampleSize}: an integer when it is a whole number, as `Sample` carries
 * counts, and a fixed-point decimal string when it is fractional. Both sizes are `null` when no
 * sample applies.
 */
export interface WithheldSample {
  /** The sample observed. */
  observed: SampleSize;
  /** The minimum required. */
  required: SampleSize;
  /** The sample's unit, for example `clicks` or `days`. */
  unit: string | null;
}

/**
 * A pattern withheld for sample size or validity (`schemas.Withheld`; AAP 0.5.4.2 P6). It never
 * becomes an item; the section lists it with observed and required sizes.
 */
export interface Withheld {
  /** The rule whose pattern was withheld, for example `B4`. */
  rule_id: string;
  /** The entity it is about; `null` for an account-wide withholding. */
  entity: EntityView | null;
  /** The shortfall, for example "3 weekend days of 4 required". */
  reason: string;
  /** Observed against required, in the rule's unit. */
  sample: WithheldSample;
}

/** One of the five sections of the page (`schemas.Section`; AAP 0.5.5). */
export interface Section {
  /** The section key. */
  key: SectionKey;
  /** Display title, for example "Keywords to adjust". */
  title: string;
  /** The section's groups, in display order. */
  groups: Group[];
  /** The section's findings, shown above its items. */
  findings: Finding[];
  /** Patterns withheld for sample size, with observed and required sizes. */
  withheld: Withheld[];
  /** Items hidden by feedback, blocked by G1 or suppressed by P2, counted by reason. */
  suppressed: Record<string, number>;
}

/**
 * The page-level current state, computed at read time (`schemas.Overlay`; AAP 0.3.2), so the
 * discrepancy count, source modes and stale or incomplete datasets are always current.
 */
export interface Overlay {
  /** Every unresolved discrepancy now, including any detected after the run. */
  open_discrepancies: number;
  /** Each source's mode now. */
  source_modes: SourceModes;
  /** Dataset names that are stale now. */
  stale_datasets: string[];
  /** Dataset names that are not complete now. */
  incomplete_datasets: string[];
  /** When the overlay was computed. */
  computed_at: IsoDateTime;
}

/**
 * A global banner, built from the run plus the overlay (`schemas.Banner`; AAP 0.5.5). Banners
 * arrive in their fixed order; known codes are listed in {@link BannerCode}.
 */
export interface Banner {
  /** The banner code, for example `open_discrepancies` or `short_window`. */
  code: string;
  /** The design-system tone. */
  tone: BannerTone;
  /** The banner text. */
  message: string;
  /** The count it reports, such as open discrepancies. */
  count: number | null;
}

/**
 * `GET /recommendations` (`schemas.RecommendationsPage`): the five sections of one run with the
 * current-state overlay. Without `run_id` it is the latest succeeded default run. `sections`
 * always holds exactly five entries in {@link SECTION_ORDER}; an empty section is still present.
 */
export interface RecommendationsPage {
  /** The run the sections come from. */
  run: Run;
  /** The page-level current state. */
  overlay: Overlay;
  /** The global banners, in their fixed order. */
  banners: Banner[];
  /** Exactly five sections, in {@link SECTION_ORDER}. */
  sections: Section[];
}

/**
 * `POST /recommendations/{id}/feedback` body (`schemas.FeedbackRequest`). A C0 item answers 422
 * and a historical item 409 `subject_retired` (AAP 0.5.3).
 */
export interface FeedbackRequest {
  /** `dismiss`, `snooze` or `done`. */
  kind: FeedbackKind;
  /** An optional note (at most 2,000 characters; no NUL character or unpaired surrogate). */
  note?: string | null;
}

/**
 * A recorded dismiss, snooze or done (`schemas.Feedback`), suppressing its rule and entity in
 * later runs until `until` (AAP 0.5.2).
 */
export interface Feedback {
  /** The feedback id. */
  id: number;
  /** The item it was given on. */
  recommendation_id: number;
  /** The item's rule. */
  rule_id: string;
  /** The item's entity type. */
  entity_type: string | null;
  /** The item's stable entity key, `{source}:{type}:{external_id}`. */
  entity_id: string | null;
  /** `dismiss`, `snooze` or `done`. */
  kind: FeedbackKind;
  /** When the suppression ends. */
  until: IsoDateTime;
  /** The user's note. */
  note: string | null;
  /** When the feedback was given. */
  created_at: IsoDateTime;
  /** Who gave it. */
  created_by: string | null;
}

// ---------------------------------------------------------------------------------------------
// Decisions and discrepancies (AAP 0.5.2 patch model, 0.5.4.4 resolutions)
// ---------------------------------------------------------------------------------------------

/** The decision subject a request names (`schemas.SubjectIn`). */
export interface SubjectIn {
  /** `campaign`, or `budget` for a Google explicitly shared budget. */
  type: SubjectType;
  /** The subject's internal id, a positive integer of at most 2^63 − 1 (PostgreSQL `bigint`). */
  id: number;
}

/**
 * The fields a decision submission decides (`schemas.DecisionPatchIn`); every other field keeps
 * its last decided value, and the server writes the complete materialised state (AAP 0.5.2).
 *
 * Carry only the fields being decided, and omit the others: an explicit `null` is refused. The
 * fields a subject accepts depend on its {@link DecisionShape}: `own_budget` takes status and
 * its own budget fields, `shared_member` takes status only, `budget` takes exactly the one
 * amount its period allows. Those rules, empty or no-change patches and the currency's minor
 * unit are validated server-side, with a 422 on violation.
 *
 * An `own_budget` patch is completed with the decided amounts it does not send. When that result
 * holds other budget fields than the campaign has now (its budget changed after the last
 * decision), it answers 422 `budget_changed`: resolve the budget discrepancy first, or, when the
 * campaign only gained a budget field, send that field in the patch. A Google campaign whose
 * budget period no entity sync reports is not checked and keeps its decided amount.
 */
export interface DecisionPatchIn {
  /** The decided campaign status. */
  status?: CampaignStatus;
  /** The decided daily budget, as a decimal string. */
  daily_budget?: Money;
  /** The decided lifetime or custom-period budget, as a decimal string. */
  total_budget?: Money;
}

/**
 * `POST /decisions` body (`schemas.DecisionRequest`): a patch on one campaign or shared budget.
 * A subject whose dataset is not active answers 409 `subject_retired`.
 *
 * With `recommendation_id`, an unknown item answers 404, and the item must offer exactly this
 * subject: one with no `decision_subject` answers 422 `decision_not_offered`, one naming another
 * subject 422 `subject_mismatch`. Every dataset its provenance names, and the subject's, must
 * stay current until the decision commits, else 409 `subject_retired`.
 */
export interface DecisionRequest {
  /** The decision subject. */
  subject: SubjectIn;
  /** The fields being decided. */
  patch: DecisionPatchIn;
  /** The item the decision was recorded from, if any; at most 2^63 − 1 (PostgreSQL `bigint`). */
  recommendation_id?: number | null;
  /** An optional note (at most 2,000 characters; no NUL character or unpaired surrogate). */
  note?: string | null;
}

/**
 * The intended state of a `record_intended` resolution, in the subject's shape
 * (`schemas.IntendedStateIn`; AAP 0.5.4.4): `own_budget` `{status, daily_budget?,
 * total_budget?}` with the budget fields the campaign has, `shared_member` `{status}`, `budget`
 * `{daily_budget}` or `{total_budget}` per its period. As in {@link DecisionPatchIn}, a field is
 * omitted rather than sent as `null`.
 */
export interface IntendedStateIn {
  /** The intended campaign status. */
  status?: CampaignStatus;
  /** The intended daily budget, as a decimal string. */
  daily_budget?: Money;
  /** The intended lifetime or custom-period budget, as a decimal string. */
  total_budget?: Money;
}

/**
 * `POST /discrepancies/{id}/resolve` body (`schemas.ResolveRequest`). Only a resolution listed
 * in {@link Discrepancy.resolutions} is valid; another answers 422, and so does a
 * `record_intended` whose intended state is incomplete or does not fit the shape.
 *
 * `platform_correct` and `adopt_platform` adopt the snapshot the discrepancy compared (else the
 * subject's newest), as does `acknowledge_removal` on a subject with no decision. They answer 409
 * `platform_state_unknown` while that state is unknown: no snapshot, a status outside the
 * normalised set, a shared member without its budget, a budget amount or period Google did not
 * report (of a shared budget, or of the budget an own-budget campaign's snapshot names), or a
 * snapshot observed in another budget shape or period than the subject has now.
 * `platform_correct` of a `status` discrepancy in an unchanged shape reads only the status, so an
 * unreported amount does not refuse it. Retry after the next entity sync.
 */
export interface ResolveRequest {
  /** The resolution. */
  resolution: Resolution;
  /** Required by `record_intended` only. */
  intended_state?: IntendedStateIn | null;
  /** An optional note (at most 2,000 characters; no NUL character or unpaired surrogate). */
  note?: string | null;
}

/** A decision subject with what the log and resolver need to render it (`schemas.SubjectView`). */
export interface SubjectView {
  /** `campaign` or `budget`. */
  type: SubjectType;
  /** The subject's internal id. */
  id: number;
  /** The campaign name, or the shared budget's display name. */
  name: string | null;
  /** The subject's source. */
  source: SourceName | null;
  /** The account currency its amounts are in. */
  currency: string | null;
}

/**
 * The `patch` of a {@link Decision} (`schemas.DecisionPatchView`): the fields the submission
 * itself carried. A key is present only when the stored patch holds it, and is never `null`,
 * because a decided field cannot be cleared.
 *
 * - `user` and `recommendation` rows: exactly the fields the {@link DecisionPatchIn} carried,
 *   for example `{ daily_budget: "120.50" }`.
 * - `baseline` and `resolution` rows, an `acknowledge_removal` tombstone included: `{}`.
 */
export interface DecisionPatchView {
  /** The decided campaign status. */
  status?: CampaignStatus;
  /** The decided daily budget, as a decimal string. */
  daily_budget?: Money;
  /** The decided lifetime or custom-period budget, as a decimal string. */
  total_budget?: Money;
}

/**
 * One side of a {@link Discrepancy} (`schemas.DiscrepancyValue`): the compared fields as stored.
 * A side holds only the keys its variant writes; a present key may still be `null`.
 *
 * - **Status**: `{ status }` on both sides of a `status` discrepancy.
 * - **Money** (a `budget` discrepancy on an `own_budget` or `budget` subject):
 *   `{ daily_budget, total_budget, currency }`, an amount `null` where the side has none, plus
 *   `amount_micros` on Google subjects only.
 * - **Assignment** (a `budget` discrepancy on a shared member, or on a campaign that moved onto
 *   a shared budget): `{ budget_id, budget_external_id }`. `budget_id` is `null` where the side
 *   names the campaign's own budget (an own-budget decided state) or reports no budget; on the
 *   platform side it is the snapshot's budget id, shared or not. `budget_external_id` is set
 *   only for a shared budget the subject knows, so a `budget_id` without it, as a member moved
 *   onto an unshared budget reports, does not establish that the budget is shared.
 * - **Tombstone reappearance**: a campaign's decided side is `{ status, tombstone: true }` and
 *   its platform side `{ status }`; a shared budget's sides are the money variant, the decided
 *   one with `tombstone: true`.
 * - **Complete state** in the subject's shape (the decided side of a `missing` discrepancy or
 *   of one waiting after `record_intended`; the platform side of an `untracked` one):
 *   `own_budget` gives `status` plus the money keys, `shared_member` `status` plus the
 *   assignment keys, `budget` the money keys only; a tombstone decision adds `tombstone: true`.
 */
export interface DiscrepancyValue {
  /** The normalised campaign status; `null` when the platform reported none. */
  status?: CampaignStatus | null;
  /** The daily budget; `null` where the side has none. */
  daily_budget?: Money | null;
  /** The lifetime or custom-period budget; `null` where the side has none. */
  total_budget?: Money | null;
  /** The ISO 4217 code of the subject account's currency. */
  currency?: string | null;
  /** Google only: the amount in micros, Google's native precision; `null` without an amount. */
  amount_micros?: number | null;
  /**
   * The internal id of the budget the campaign is assigned to on this side: on the platform
   * side the snapshot's budget id, shared or not (against an own-budget decision, the subject's
   * current shared budget when the snapshot names none); on the decided side the budget a
   * `shared_member` decision assigns. `null` where the side names the campaign's own budget (an
   * own-budget decided state) or reports no budget.
   */
  budget_id?: number | null;
  /**
   * That budget's Google id, set only when it is a shared budget the subject knows (its current
   * one or the one its decision assigns); `null` otherwise. A `budget_id` with this `null` does
   * not establish that the budget is shared.
   */
  budget_external_id?: string | null;
  /** Present, and `true`, only on a side describing an `acknowledge_removal` tombstone. */
  tombstone?: boolean;
}

/**
 * One decision log row (`schemas.Decision`): the complete materialised expected state for its
 * shape (AAP 0.5.2). A budget-only patch keeps the decided status, a status-only patch keeps
 * the decided budget, and `patch` records what the submission itself carried.
 */
export interface Decision {
  /** The decision id. */
  id: number;
  /** The subject decided. */
  subject: SubjectView;
  /** `own_budget`, `shared_member` or `budget`. */
  shape: DecisionShape;
  /** The decided status; `null` for budget subjects. */
  status: CampaignStatus | null;
  /** The decided daily budget; `null` where the shape has none. */
  daily_budget: Money | null;
  /** The decided lifetime or custom-period budget. */
  total_budget: Money | null;
  /** For `shared_member` rows, the shared budget the campaign is assigned to. */
  budget_id: number | null;
  /**
   * For `shared_member` rows, the Google id of the shared budget the campaign is assigned to,
   * the id the product names it by ("Shared budget {external_id}"); `null` for the other
   * shapes, or when that budget row cannot be read.
   */
  budget_external_id: string | null;
  /** Whether the row records an acknowledged removal. */
  tombstone: boolean;
  /** When the decision was recorded. */
  decided_at: IsoDateTime;
  /** Who recorded it; `null` for baseline and system rows. */
  decided_by: string | null;
  /** `baseline`, `user`, `recommendation` or `resolution`. */
  origin: DecisionOrigin;
  /** The item it was recorded from, if any. */
  recommendation_id: number | null;
  /** The fields the submission carried; `{}` for baseline and resolution rows. */
  patch: DecisionPatchView;
  /** The note entered with it. */
  note: string | null;
}

/**
 * A mismatch between a subject's platform state and its last decision (`schemas.Discrepancy`;
 * rule C0, AAP 0.5.4.4). The resolver renders exactly `resolutions`, the keys valid for the
 * discrepancy's kind and subject shape. `GET /discrepancies` returns them oldest first, one
 * page at a time by `limit` (1–500, default 200) and the `after` cursor, and names the next
 * page in a `Link: <…>; rel="next"` response header, absent on the last page.
 */
export interface Discrepancy {
  /** The discrepancy id. */
  id: number;
  /** The subject. */
  subject: SubjectView;
  /** The subject's current decision shape, which fixes the intended-state fields. */
  shape: DecisionShape;
  /** `status`, `budget`, `untracked` or `missing`. */
  kind: DiscrepancyKind;
  /**
   * The decided side. `null` for an `untracked` discrepancy until `record_intended` records an
   * intended state and moves it to `awaiting_platform`; it then shows that state, or a decision
   * recorded since.
   */
  decided_value: DiscrepancyValue | null;
  /** The platform side; `null` for `missing`. */
  platform_value: DiscrepancyValue | null;
  /** When it was detected; the oldest are shown first. */
  detected_at: IsoDateTime;
  /** `open`, `awaiting_platform` or `resolved`. */
  state: DiscrepancyState;
  /** When it was resolved. */
  resolved_at: IsoDateTime | null;
  /** The resolution applied, if any. */
  resolution: Resolution | null;
  /** The resolutions valid for its kind and shape, in their fixed display order. */
  resolutions: Resolution[];
}

/** The id of a {@link Discrepancy}, as the resolve route takes it. */
export type DiscrepancyId = Discrepancy['id'];

// ---------------------------------------------------------------------------------------------
// Sources (AAP 0.5.3 `GET /sources`, 0.5.5 `/sources`)
// ---------------------------------------------------------------------------------------------

/** One dataset of a source (`schemas.DatasetView`; AAP 0.3.3). */
export interface DatasetView {
  /** The `source_dataset` id. */
  id: number;
  /** `live` or `simulated`. */
  mode: DataMode;
  /** `primary` or `companion`. */
  role: DatasetRole;
  /** `building`, `active` or `retired`. */
  state: DatasetState;
  /** Why the dataset exists, for example "credentials not configured". */
  reason: string | null;
  /** When it was created. */
  created_at: IsoDateTime;
  /** When it became active, if it has. */
  activated_at: IsoDateTime | null;
}

/**
 * Freshness, coverage and completeness of one named dataset (`schemas.DatasetFreshnessView`;
 * AAP 0.3.2).
 */
export interface DatasetFreshnessView {
  /** The dataset name, one of {@link DatasetName}. */
  dataset: string;
  /** When a job last tried to refresh it. */
  last_attempt_at: IsoDateTime | null;
  /** When a job last refreshed it successfully. */
  last_success_at: IsoDateTime | null;
  /** First native date covered. */
  covered_from: IsoDate | null;
  /** Last native date covered. */
  covered_through: IsoDate | null;
  /** `complete`, `incomplete` or `unknown`. */
  completeness: Completeness;
  /** Why it is not complete. */
  reason: string | null;
  /**
   * Whether it never succeeded (`last_success_at` is `null`) or last succeeded more than
   * `sources.stale_after_hours` ago.
   */
  stale: boolean;
  /** Whether it is not `complete` (`incomplete` or `unknown`); derived from `completeness`. */
  incomplete: boolean;
}

/** One recent sync job run (`schemas.SyncRunView`). */
export interface SyncRunView {
  /** The `sync_run` id. */
  id: number;
  /** The job name. */
  job: string;
  /** The dataset it refreshed, when a single one. */
  dataset: string | null;
  /** When it started. */
  started_at: IsoDateTime;
  /** When it finished, if it has. */
  finished_at: IsoDateTime | null;
  /** `running`, `succeeded` or `failed`. */
  status: SyncRunStatus;
  /** Rows written, once known. */
  rows: number | null;
  /** Completeness of what it read, once known. */
  completeness: Completeness | null;
  /** The redacted failure message of a failed run. */
  error: string | null;
}

/**
 * Health, mode, datasets, freshness and call budget of one source (`schemas.Source`;
 * `GET /sources`).
 *
 * `calls_24h` and `call_cap` describe the product's own cap on Google webhook calls
 * (`GOOGLE_DAILY_CALL_BUDGET`, AAP 0.3.3), not Google's token-wide quota, and are `null` for
 * LinkedIn and HubSpot. The companion fields are set only for HubSpot while a simulated
 * LinkedIn dataset's companion CRM dataset is active.
 */
export interface Source {
  /** The source. */
  source: SourceName;
  /** `live` or `simulated`; `null` before the worker's first attempt. */
  mode: DataMode | null;
  /** `ok`, `stale`, `denied` or `error`; `null` before the first attempt. */
  status: ConnectionStatus | null;
  /** Why the source is in its mode or status. */
  reason: string | null;
  /**
   * The IANA zone whose midnight ends the native dates of this source's freshness rows
   * (`covered_from` and `covered_through`, primary and companion), so they can be labelled
   * (AAP 0.5.4.1, 0.5.5): for Google the active Google dataset's account time zone
   * (`customer.time_zone`), for LinkedIn and HubSpot `UTC`. `null` while Google has no active
   * dataset or no account row with a zone; the display time zone never stands in for it.
   */
  day_boundary: string | null;
  /** The active primary dataset. */
  active_dataset: DatasetView | null;
  /** HubSpot only: the active companion CRM dataset. */
  companion_dataset: DatasetView | null;
  /** Freshness of the active dataset's named datasets. */
  datasets: DatasetFreshnessView[];
  /** Freshness of the companion's named datasets. */
  companion_datasets: DatasetFreshnessView[];
  /** When a job last called the source. */
  last_attempt_at: IsoDateTime | null;
  /**
   * The product's own `GOOGLE_DAILY_CALL_BUDGET` usage: the Google webhook call reservations
   * still counting in its sliding 24 h window. A call is booked once admitted, in the minute by
   * which its POST has started (up to the 140 s dispatch bound ahead), and counts until 24 h
   * after that minute ends; a call that had to be reserved again may count twice. So this is a
   * conservative upper bound on the calls admitted in the trailing 24 h, not an exact count of
   * dispatched POSTs.
   */
  calls_24h: number | null;
  /** The product's Google webhook call cap. */
  call_cap: number | null;
  /** Whether a "Sync now" request is pending. */
  sync_pending: boolean;
  /** The newest sync runs, newest first. */
  recent_runs: SyncRunView[];
}

/**
 * A queued "Sync now" request (`schemas.SyncRequestView`; `POST /sources/{source}/sync`, 202).
 * A request already pending for the source answers 409.
 */
export interface SyncRequestView {
  /** The `sync_request` id. */
  id: number;
  /** The source to sync. */
  source: SourceName;
  /** When it was requested. */
  requested_at: IsoDateTime;
  /** When the worker picked it up; `null` while pending. */
  picked_at: IsoDateTime | null;
}

// ---------------------------------------------------------------------------------------------
// Config and health (AAP 0.5.3 `GET /config`, `GET /healthz`)
// ---------------------------------------------------------------------------------------------

/**
 * The configured window settings (`schemas.WindowConfig`). The UI reads window lengths only from
 * here and from {@link Run}, never from literals (AAP 0.5.4.1, 0.5.5).
 */
export interface WindowConfig {
  /** `window.default_days`; the picker marks this preset "default". */
  default_days: number;
  /** `window.trusted_min_days`; shorter windows are flagged. */
  trusted_min_days: number;
  /** `window.presets`, ascending: the picker's choices. */
  presets: number[];
  /** `window.max_days`: the longest custom window. */
  max_days: number;
  /** `window.sustained_min_days`: the minimum for sustained-pattern rules. */
  sustained_min_days: number;
}

/**
 * One white paper of the registry, `config/content_assets.yaml` (`schemas.ContentAssetView`;
 * AAP 0.5.4.7).
 */
export interface ContentAssetView {
  /** The asset's identifier. */
  slug: string;
  /** Display title. */
  title: string;
  /** Canonical lead-form URNs (`urn:li:leadGenForm:{id}`) that resolve to the asset. */
  lead_forms: string[];
  /** Creative URNs credited to the asset by override. */
  creative_overrides: string[];
}

/**
 * How one CRM count (MQL or Qualified Lead) is bound, as configured: the `mql` or `ql` entry
 * of `config/hubspot_mapping.yaml` (`schemas.StageBindingView`; AAP 0.5.4.7). In `stage_label`
 * mode the labels are resolved in order among the portal's `lifecyclestage` options and the
 * first match wins; in `property` mode a contact date property records the event directly.
 */
export interface StageBindingView {
  /** `stage_label` or `property`. */
  mode: BindingMode;
  /** The labels to try, in order; MQL's single `stage_label` is a one-element list. */
  stage_labels: string[];
  /** The bound contact property in `property` mode; `null` in `stage_label` mode. */
  property: string | null;
}

/**
 * `config/hubspot_mapping.yaml`: the MQL and QL bindings and the meeting rules
 * (`schemas.HubSpotMappingView`; AAP 0.5.4.7). MQL and QL are bound independently and never
 * blended.
 */
export interface HubSpotMappingView {
  /** The MQL binding. */
  mql: StageBindingView;
  /** The Qualified Lead binding. */
  ql: StageBindingView;
  /** `hs_meeting_source` values whose meetings can count as booked. */
  booking_sources: string[];
  /** `hs_meeting_outcome` values never counted. */
  meeting_outcome_exclusions: string[];
}

/**
 * One CRM count's binding as verified against the portal (`schemas.ResolvedBindingView`;
 * AAP 0.3.3, 0.5.4.7). Every key is present. An unavailable binding makes its count
 * "unavailable", shown as such and never as `0`.
 */
export interface ResolvedBindingView {
  /** Whether the count can be computed. */
  available: boolean;
  /** The contact date property the count reads; set when available. */
  property: string | null;
  /** The lifecycle stage id the label resolved to, in `stage_label` mode. */
  stage_id: string | null;
  /**
   * The label this binding resolved: the lifecycle stage label that matched in `stage_label`
   * mode, or the configured display label kept in `property` mode, when one is set. Only the MQL
   * binding's label heads the white-paper MQL column, e.g. "MQL (First meeting booked)".
   */
  label: string | null;
  /** Why the binding is unavailable; set when unavailable. */
  reason: string | null;
}

/**
 * The optional Google-to-CRM link as verified against the portal
 * (`schemas.GoogleCrmBindingView`; AAP 0.3.3). Every key is present. Without the link, Google
 * items carry `no_crm_link` and no Google QL count or pipeline veto is computed.
 */
export interface GoogleCrmBindingView {
  /** Whether the link can be used. */
  available: boolean;
  /** The contact property holding the Google campaign id. */
  campaign_property: string | null;
  /** The datetime property holding when that touch happened. */
  touch_date_property: string | null;
  /** Why the link is unavailable; set when unavailable. */
  reason: string | null;
}

/**
 * Every CRM binding verified against one HubSpot portal, as stored on its dataset
 * (`schemas.CrmBindingsView`; AAP 0.5.4.7).
 */
export interface CrmBindingsView {
  /** The resolved MQL binding. */
  mql: ResolvedBindingView;
  /** The resolved Qualified Lead binding, independent of MQL. */
  ql: ResolvedBindingView;
  /** The Google-to-CRM link. */
  google: GoogleCrmBindingView;
  /** When the portal was checked, in UTC. */
  verified_at: IsoDateTime;
}

/**
 * `GET /config` (`schemas.ConfigView`): the read-only configuration behind `/settings` and the
 * window picker.
 */
export interface ConfigView {
  /** Every threshold of `config/thresholds.yaml`, by namespace. */
  thresholds: Record<string, unknown>;
  /** The window settings. */
  window: WindowConfig;
  /** The MQL and Qualified Lead bindings and meeting rules as configured. */
  hubspot_mapping: HubSpotMappingView;
  /** The bindings as resolved against HubSpot's active primary portal; `null` until verified. */
  crm_bindings: CrmBindingsView | null;
  /** The bindings of the active companion CRM dataset; `null` when there is none. */
  companion_crm_bindings: CrmBindingsView | null;
  /** The white paper registry, in file order. */
  content_assets: ContentAssetView[];
  /** Hash of the three configuration files. */
  config_hash: string;
}

/**
 * `GET /healthz` (`schemas.Health`): the process is up and the database answered. A failure is
 * reported as an {@link ErrorResponse} instead.
 */
export interface Health {
  /** Always `ok`. */
  status: 'ok';
}
