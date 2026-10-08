/**
 * Guarded readers for recommendation evidence (AAP 0.5.3 `Evidence.extra`, AAP 0.5.5 field sets
 * per section).
 *
 * `Evidence.extra` is open JSON (`Record<string, unknown>` in `src/api/types.ts`): each rule in
 * `backend/src/ads_engine/analysis/*.py` freezes its own detail there, and the API returns it
 * unchanged. The recommendation card and the five `/actions` sections all need some of those
 * keys, and several need the same section metric keys. This module is the one place that names
 * them, so a backend rename breaks one file, and every read is a runtime guard rather than a
 * cast: a reader returns `null`, `[]` or an empty field when its data is absent or malformed, and
 * never throws.
 *
 * Contract, decided once here:
 *
 * - **Key names are the backend's.** Each reader's TSDoc names the rule IDs and the backend
 *   key it reads (AAP 0.11.2). Where the backend renders a value for display (C0's
 *   `render_value` text, the B1 funding sentence, B3 allocation lines, white-paper "—"), the
 *   reader passes that text through as given.
 * - **Readers take an item or its evidence.** Every reader accepts an {@link EvidenceSource}:
 *   an {@link Evidence}, or anything holding one, such as an {@link Item}. `null` and
 *   `undefined` read as absent, so `variants(group.items[0])` is safe under
 *   `noUncheckedIndexedAccess`.
 * - **Money stays a decimal string** ({@link Money}). A reader never converts an amount with
 *   `Number()`, never sums amounts and never converts across currencies (AAP 0.1.1). Totals the
 *   UI shows, such as a cohort's spend total or a B1's funded share, are the backend's own.
 * - **Absent is not zero.** A missing count is `null` ("unavailable"), never `0`
 *   (AAP 0.11.1). Metrics keep their `floor` and `unavailable` markers, so `formatMetric` renders
 *   "≥ N", "≤ X", "unavailable" and "—" exactly as the backend decided.
 * - **MQL and QL are separate keys** everywhere (`mql`, `ql`); no reader combines them
 *   (AAP 0.5.4.7).
 *
 * No React, no hooks and no I/O: pure functions over the API payload.
 *
 * @example
 * ```ts
 * import { keywordFields, adjacentTerms } from './evidenceExtra';
 *
 * const fields = keywordFields(item);
 * const terms = item.rule_id === 'K2' ? adjacentTerms(item) : [];
 * ```
 *
 * @packageDocumentation
 */
import type {
  Baseline,
  DecisionPatchIn,
  Evidence,
  Item,
  Metric,
  MetricValue,
  Money,
} from '../../api/types';
import { EM_DASH, UNAVAILABLE, isDecimalString } from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------------------------

/**
 * Anything that carries frozen evidence, such as an {@link Item}. `entity.name` is the display
 * name a reader may fall back to (for example the K1–K3 term).
 */
export interface EvidenceHolder {
  /** The frozen evidence. */
  readonly evidence: Evidence;
  /** The entity the evidence is about; only its display name is read. */
  readonly entity?: { readonly name?: string | null } | null;
}

/** What every reader accepts: an {@link Evidence}, or an {@link EvidenceHolder} such as an item. */
export type EvidenceSource = Evidence | EvidenceHolder;

/** A source that may be absent, for example `group.items[0]` under `noUncheckedIndexedAccess`. */
type MaybeSource = EvidenceSource | null | undefined;

// ---------------------------------------------------------------------------------------------
// Primitives
// ---------------------------------------------------------------------------------------------

/**
 * Whether `value` is a plain JSON object: non-null, not an array.
 *
 * @param value - Any value, typically a node of `Evidence.extra`.
 * @returns `true` for an object whose string keys may be read as `unknown`.
 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Whether `value` is an array, typed with `unknown` items so every item is still guarded. */
function isUnknownArray(value: unknown): value is readonly unknown[] {
  return Array.isArray(value);
}

/** `value` as an array of `unknown` items; `[]` for anything that is not an array. */
function readArray(value: unknown): readonly unknown[] {
  return isUnknownArray(value) ? value : [];
}

/** A non-blank string, unchanged; `null` for a blank string or any other type. */
function readString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

/** A boolean, or `null` for any other type. */
function readBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

/**
 * A plain decimal as a trimmed string (`"62.5"`, `"0.004377"`): a decimal string as the backend
 * renders every `Decimal`, or a finite JSON number converted with `String`. `null` otherwise.
 * Used for amounts, fractional conversions and rates, which are never parsed into floats here.
 */
function readDecimal(value: unknown): string | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? String(value) : null;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return trimmed !== '' && isDecimalString(trimmed) ? trimmed : null;
  }
  return null;
}

/**
 * An amount ({@link Money}): a decimal string, or a finite number converted with `String`
 * (never `Number()` on a string, so no precision is lost). `null` otherwise.
 */
function readMoney(value: unknown): Money | null {
  return readDecimal(value);
}

/**
 * A count or other plain number: a finite number, or a numeric string. Never used for money.
 */
function readNumber(value: unknown): number | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  const decimal = typeof value === 'string' ? readDecimal(value) : null;
  if (decimal === null) {
    return null;
  }
  const parsed = Number(decimal);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A whole number (a day, click or impression count); `null` for fractions and non-numbers. */
function readInteger(value: unknown): number | null {
  const parsed = readNumber(value);
  return parsed !== null && Number.isInteger(parsed) ? parsed : null;
}

/**
 * An identifier as a string: a non-blank string, or an integer (the backend's `int` ids)
 * converted with `String`. Booleans and fractions are not ids.
 */
function readId(value: unknown): string | null {
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : null;
  }
  return readString(value);
}

/** Every non-blank string item of an array; `[]` when `value` is not an array. */
function readStringArray(value: unknown): readonly string[] {
  const strings: string[] = [];
  for (const entry of readArray(value)) {
    const text = readString(entry);
    if (text !== null) {
      strings.push(text);
    }
  }
  return strings;
}

/** A metric or baseline value: a string, finite number or boolean; `null` otherwise. */
function readMetricValue(value: unknown): MetricValue {
  if (typeof value === 'string' || typeof value === 'boolean') {
    return value;
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  return null;
}

/** The evidence of a source, or `null` when the source is absent or carries no object. */
function evidenceOf(source: MaybeSource): Evidence | null {
  if (source === null || source === undefined) {
    return null;
  }
  const evidence = 'evidence' in source ? source.evidence : source;
  return isRecord(evidence) ? evidence : null;
}

/** The display name of a holder's entity, or `null` for bare evidence. */
function entityNameOf(source: MaybeSource): string | null {
  if (source === null || source === undefined || !('evidence' in source)) {
    return null;
  }
  const entity: unknown = source.entity;
  return isRecord(entity) ? readString(entity.name) : null;
}

/**
 * The rule-specific detail of an item's evidence (`evidence.extra`).
 *
 * @param source - The item or its evidence.
 * @returns The `extra` object, or `null` when the evidence has none or it is not an object.
 */
export function readExtra(source: MaybeSource): Record<string, unknown> | null {
  const evidence = evidenceOf(source);
  if (evidence === null) {
    return null;
  }
  const extra: unknown = evidence.extra;
  return isRecord(extra) ? extra : null;
}

/** `extra[key]` when it is an object, else `null`. */
function extraRecord(
  extra: Record<string, unknown> | null,
  key: string,
): Record<string, unknown> | null {
  if (extra === null) {
    return null;
  }
  const value = extra[key];
  return isRecord(value) ? value : null;
}

/**
 * A guarded copy of one `evidence.metrics` entry, with every field the web contract declares
 * (absent `currency`, `floor` and `unavailable` become `null`). `null` without a key.
 */
function toMetric(value: unknown): Metric | null {
  if (!isRecord(value)) {
    return null;
  }
  const key = readString(value.key);
  if (key === null) {
    return null;
  }
  return {
    key,
    label: readString(value.label) ?? key,
    value: readMetricValue(value.value),
    unit: readString(value.unit),
    currency: readString(value.currency),
    floor: readBoolean(value.floor),
    unavailable: readBoolean(value.unavailable),
  };
}

/** Every well-formed metric of a source, in API order. */
function metricsOf(source: MaybeSource): readonly Metric[] {
  const evidence = evidenceOf(source);
  if (evidence === null) {
    return [];
  }
  const metrics: Metric[] = [];
  for (const entry of readArray(evidence.metrics)) {
    const metric = toMetric(entry);
    if (metric !== null) {
      metrics.push(metric);
    }
  }
  return metrics;
}

// ---------------------------------------------------------------------------------------------
// Metrics and baselines
// ---------------------------------------------------------------------------------------------

/**
 * The first metric of `evidence.metrics` with this key (every rule; for example `spend`,
 * `cost_per_conversion`, `window_utilization`).
 *
 * @param source - The item or its evidence.
 * @param key - The backend metric key.
 * @returns A guarded copy of the metric, or `null` when no well-formed metric has the key.
 */
export function metricByKey(source: MaybeSource, key: string): Metric | null {
  return metricsOf(source).find((metric) => metric.key === key) ?? null;
}

/**
 * The metrics for `keys`, in the order of `keys` (for example
 * {@link PAUSE_SCALE_METRIC_KEYS}), with `null` where the evidence has no such metric, so a
 * section renders columns in a fixed order and skips or marks the missing ones.
 *
 * @param source - The item or its evidence.
 * @param keys - The backend metric keys, in display order.
 * @returns One entry per key.
 */
export function metricsByKeys(
  source: MaybeSource,
  keys: readonly string[],
): readonly (Metric | null)[] {
  const byKey = new Map<string, Metric>();
  for (const metric of metricsOf(source)) {
    if (!byKey.has(metric.key)) {
      byKey.set(metric.key, metric);
    }
  }
  return keys.map((key) => byKey.get(key) ?? null);
}

/**
 * The first baseline of `evidence.baselines` with this key (for example
 * `baseline_cost_per_outcome` of C1/C2/B1–B3, `cohort_median_cost_per_ql` of W1/W2).
 *
 * @param source - The item or its evidence.
 * @param key - The backend baseline key.
 * @returns A guarded copy of the baseline, or `null`.
 */
export function baselineByKey(source: MaybeSource, key: string): Baseline | null {
  const evidence = evidenceOf(source);
  if (evidence === null) {
    return null;
  }
  for (const entry of readArray(evidence.baselines)) {
    if (isRecord(entry) && readString(entry.key) === key) {
      return { key, label: readString(entry.label) ?? key, value: readMetricValue(entry.value) };
    }
  }
  return null;
}

/**
 * The currency an item's amounts are in: the first metric carrying a `currency`, else
 * `extra.currency` (B1–B4, CR3) or `extra.asset.currency` (W1/W2). Amounts of one item are
 * always in one account currency (AAP 0.1.1).
 *
 * @param source - The item or its evidence.
 * @returns The ISO currency code, or `null` when the evidence names none.
 */
export function evidenceCurrency(source: MaybeSource): string | null {
  for (const metric of metricsOf(source)) {
    if (metric.currency !== null) {
      return metric.currency;
    }
  }
  const extra = readExtra(source);
  return readString(extra?.currency) ?? readString(extraRecord(extra, 'asset')?.currency);
}

/** A metric's value as display text when it is text or a number; `null` when unavailable. */
function metricText(metric: Metric | null): string | null {
  if (metric === null || metric.unavailable === true) {
    return null;
  }
  const { value } = metric;
  if (typeof value === 'string') {
    return readString(value);
  }
  return typeof value === 'number' ? String(value) : null;
}

// ---------------------------------------------------------------------------------------------
// C0 discrepancies and P1 dependencies
// ---------------------------------------------------------------------------------------------

/**
 * What a C0 card needs to find and describe its discrepancy (AAP 0.5.4.4).
 */
export interface DiscrepancyRef {
  /** `extra.discrepancy_id`, as a string (the backend sends an integer). */
  readonly discrepancyId: string;
  /** `extra.kind`: `status`, `budget`, `untracked` or `missing`. */
  readonly kind: string;
  /** `extra.shape`: `own_budget`, `shared_member` or `budget`. */
  readonly shape: string;
  /** `extra.state`: `open` or `awaiting_platform`. */
  readonly state: string;
  /** The last decided state as the backend rendered it (`render_value`), shown as given. */
  readonly decided: string;
  /** The platform state as the backend rendered it (`render_value`), shown as given. */
  readonly platform: string;
  /** `extra.resolutions`: the resolution keys offered for this (kind, subject type). */
  readonly resolutions: readonly string[];
  /** `extra.detected_at`, an ISO instant; `null` when absent. */
  readonly detectedAt: string | null;
}

/**
 * Reads a C0 item's discrepancy (`discrepancy._candidate`): `extra.discrepancy_id`, `kind`,
 * `shape`, `state`, `detected_at` and `resolutions`. The display texts come from the `platform`
 * and `decided` metrics, which hold `render_value`'s strings ("paused", "USD 50.00", "shared
 * budget 123", "not returned", "no decision"); `extra.decided` and `extra.platform` hold the raw
 * state objects and are read only when they are already strings. A missing text renders
 * "unavailable".
 *
 * @param source - The C0 item or its evidence.
 * @returns The reference, or `null` when `extra.discrepancy_id` is missing or not an id.
 */
export function discrepancyRef(source: MaybeSource): DiscrepancyRef | null {
  const extra = readExtra(source);
  const discrepancyId = readId(extra?.discrepancy_id);
  if (extra === null || discrepancyId === null) {
    return null;
  }
  return {
    discrepancyId,
    kind: readString(extra.kind) ?? '',
    shape: readString(extra.shape) ?? '',
    state: readString(extra.state) ?? '',
    decided:
      metricText(metricByKey(source, 'decided')) ?? readString(extra.decided) ?? UNAVAILABLE,
    platform:
      metricText(metricByKey(source, 'platform')) ?? readString(extra.platform) ?? UNAVAILABLE,
    resolutions: readStringArray(extra.resolutions),
    detectedAt: readString(extra.detected_at),
  };
}

/** A decision subject `{type, id}` as `"{type}:{id}"`, or a string entry as given. */
function subjectKey(value: unknown): string | null {
  if (!isRecord(value)) {
    return readString(value);
  }
  const type = readString(value.type);
  const id = readId(value.id);
  return type === null || id === null ? null : `${type}:${id}`;
}

/**
 * The campaigns and budgets an item depends on, as frozen by precedence P1
 * (`precedence._apply_p1`, `extra.depends_on`, `[{type, id}]`), each as `"{type}:{id}"`, for
 * example `"campaign:12"` or `"budget:401"`. Every rule's items carry it; the overlay holds an
 * item when any of these subjects has an open discrepancy (AAP 0.5.4.2).
 *
 * @param source - The item or its evidence.
 * @returns The subject keys in stored order; `[]` when absent.
 */
export function dependsOn(source: MaybeSource): readonly string[] {
  const keys: string[] = [];
  for (const entry of readArray(readExtra(source)?.depends_on)) {
    const key = subjectKey(entry);
    if (key !== null) {
      keys.push(key);
    }
  }
  return keys;
}

/**
 * The ids of the discrepancies that held the item when the run computed it
 * (`precedence._apply_p1`, `extra.blocking_discrepancies`, ascending), for the card's link to
 * the blocking discrepancy (AAP 0.5.5).
 *
 * @param source - The item or its evidence.
 * @returns The discrepancy ids as strings; `[]` when the item was not held by one.
 */
export function blockingDiscrepancies(source: MaybeSource): readonly string[] {
  const ids: string[] = [];
  for (const entry of readArray(readExtra(source)?.blocking_discrepancies)) {
    const id = readId(entry);
    if (id !== null) {
      ids.push(id);
    }
  }
  return ids;
}

// ---------------------------------------------------------------------------------------------
// Keywords to adjust: K1, K2, K3 (Google)
// ---------------------------------------------------------------------------------------------

/** The fields every keyword card shows, in the user's order (AAP 0.5.4.3). */
export interface KeywordFields {
  /** The search term (K1, K2) or keyword text (K3). */
  readonly term: string;
  /** The campaign name. */
  readonly campaign: string | null;
  /** The ad group name. */
  readonly adGroup: string | null;
  /** Window spend (`spend`). */
  readonly spend: Metric | null;
  /** Window conversions (`conversions`), fractional under data-driven attribution. */
  readonly conversions: Metric | null;
  /**
   * Cost per conversion (`cost_per_conversion`). Its value is `null` at 0 conversions; keep it a
   * metric so the section renders "—" there.
   */
  readonly costPerConversion: Metric | null;
}

/**
 * Reads the K1–K3 card fields (`keywords._keyword_metrics`, `keywords._campaign_extra`): the
 * `term`, `spend`, `conversions` and `cost_per_conversion` metrics, and `extra.campaign` and
 * `extra.ad_group` (names). The term falls back to `extra.term`, then to the entity's name.
 *
 * @param source - The keyword item or its evidence.
 * @param item - The item, when `source` is bare evidence, for the entity-name fallback.
 * @returns The fields; `term` is `""` only when nothing names the term.
 */
export function keywordFields(source: MaybeSource, item?: EvidenceHolder | null): KeywordFields {
  const extra = readExtra(source);
  const term =
    metricText(metricByKey(source, 'term')) ??
    readString(extra?.term) ??
    entityNameOf(item) ??
    entityNameOf(source) ??
    '';
  const [spend, conversions, costPerConversion] = metricsByKeys(source, [
    'spend',
    'conversions',
    'cost_per_conversion',
  ]);
  return {
    term,
    campaign: readString(extra?.campaign),
    adGroup: readString(extra?.ad_group),
    spend: spend ?? null,
    conversions: conversions ?? null,
    costPerConversion: costPerConversion ?? null,
  };
}

/** The most adjacent terms a K2 card lists (AAP 0.5.4.3: "up to 5 other window search terms"). */
export const ADJACENT_TERMS_MAX = 5;

/** One adjacent term of a K2 expansion candidate. */
export interface AdjacentTerm {
  /** The search term. */
  readonly term: string;
  /** Its window conversions as a decimal string; `null` when not given. */
  readonly conversions: string | null;
  /** Its window clicks; `null` when not given. */
  readonly clicks: number | null;
  /** Its window spend; `null` when not given. */
  readonly spend: Money | null;
  /** The currency of `spend`; `null` when not given. */
  readonly currency: string | null;
}

/**
 * Reads a K2 item's adjacent terms (`keywords._k2_candidate`, `extra.adjacent_terms`:
 * `{term, spend, currency, clicks, conversions}` objects, or plain strings), labelled "from
 * your search-term report" by the card. At most {@link ADJACENT_TERMS_MAX}, in stored order.
 *
 * @param source - The K2 item or its evidence.
 * @returns The terms; `[]` when absent.
 */
export function adjacentTerms(source: MaybeSource): readonly AdjacentTerm[] {
  const terms: AdjacentTerm[] = [];
  for (const entry of readArray(readExtra(source)?.adjacent_terms)) {
    if (terms.length >= ADJACENT_TERMS_MAX) {
      break;
    }
    const plain = readString(entry);
    if (plain !== null) {
      terms.push({ term: plain, conversions: null, clicks: null, spend: null, currency: null });
      continue;
    }
    if (!isRecord(entry)) {
      continue;
    }
    const term = readString(entry.term);
    if (term === null) {
      continue;
    }
    terms.push({
      term,
      conversions: readDecimal(entry.conversions),
      clicks: readInteger(entry.clicks),
      spend: readMoney(entry.spend),
      currency: readString(entry.currency),
    });
  }
  return terms;
}

/** The K3 review evidence by half of the window (AAP 0.5.4.3). */
export interface ReviewHalves {
  /** Mean quality score over the first span (`qs_first`). */
  readonly qsFirst: Metric | null;
  /** Mean quality score over the last span (`qs_last`). */
  readonly qsLast: Metric | null;
  /** CPC over the first half (`cpc_first_half`). */
  readonly cpcFirstHalf: Metric | null;
  /** CPC over the second half (`cpc_second_half`). */
  readonly cpcSecondHalf: Metric | null;
  /** Cost/conversion over the first half (`cost_per_conversion_first_half`). */
  readonly cpaFirstHalf: Metric | null;
  /** Cost/conversion over the second half (`cost_per_conversion_second_half`). */
  readonly cpaSecondHalf: Metric | null;
}

/**
 * Reads a K3 item's quality score and CPC by half (`keywords._k3_candidate` metrics `qs_first`,
 * `qs_last`, `cpc_first_half`, `cpc_second_half`, `cost_per_conversion_first_half`,
 * `cost_per_conversion_second_half`).
 *
 * @param source - The K3 item or its evidence.
 * @returns Each metric, or `null` where the evidence lacks it.
 */
export function reviewHalves(source: MaybeSource): ReviewHalves {
  const [qsFirst, qsLast, cpcFirstHalf, cpcSecondHalf, cpaFirstHalf, cpaSecondHalf] =
    metricsByKeys(source, [
      'qs_first',
      'qs_last',
      'cpc_first_half',
      'cpc_second_half',
      'cost_per_conversion_first_half',
      'cost_per_conversion_second_half',
    ]);
  return {
    qsFirst: qsFirst ?? null,
    qsLast: qsLast ?? null,
    cpcFirstHalf: cpcFirstHalf ?? null,
    cpcSecondHalf: cpcSecondHalf ?? null,
    cpaFirstHalf: cpaFirstHalf ?? null,
    cpaSecondHalf: cpaSecondHalf ?? null,
  };
}

// ---------------------------------------------------------------------------------------------
// Campaigns to pause or launch: C1, C1b, C2, C3a, C3b
// ---------------------------------------------------------------------------------------------

/**
 * The metric keys pause and scale cards show, in order (AAP 0.5.5: "spend, outcomes, expected
 * outcomes and CRM QL"): window spend, window outcomes, expected outcomes at the account
 * baseline, and the CRM Qualified Leads of the veto population (`ql`, present only when the
 * campaign is linked to the CRM). Written by `campaigns._c1_candidate`, `_c1b_candidate` and
 * `_c2_candidate`.
 */
export const PAUSE_SCALE_METRIC_KEYS = ['spend', 'outcomes', 'expected_outcomes', 'ql'] as const;

/**
 * The pipeline metric keys of C1b and C2 "Hold" cards, in column order (AAP 0.5.5: "matured
 * leads, matched and unresolved matured leads, eligible contacts, MQL, QL and meetings"), as
 * `campaigns._veto_metrics` writes them. `mql` and `ql` are separate columns; no key combines
 * them (AAP 0.5.4.7).
 */
export const PIPELINE_METRIC_KEYS = [
  'matured_leads',
  'matched_leads',
  'unresolved',
  'eligible_contacts',
  'mql',
  'ql',
  'meetings',
] as const;

/** The C1, C1b and C2 card metrics (AAP 0.5.4.4), each `null` where the evidence lacks it. */
export interface PipelineFields {
  /** Window spend (`spend`). */
  readonly spend: Metric | null;
  /** Window outcomes (`outcomes`). */
  readonly outcomes: Metric | null;
  /** Expected outcomes at the account baseline (`expected_outcomes`). */
  readonly expectedOutcomes: Metric | null;
  /** The CRM Qualified Leads shown beside platform outcomes when linked (`ql`). */
  readonly crmQl: Metric | null;
  /** LinkedIn platform leads (`leads`), labelled as a platform count. */
  readonly leads: Metric | null;
  /** Matured leads (`matured_leads`). */
  readonly maturedLeads: Metric | null;
  /** Matched matured leads (`matched_leads`). */
  readonly matchedLeads: Metric | null;
  /** Unresolved matured leads (`unresolved`). */
  readonly unresolvedLeads: Metric | null;
  /** Eligible contacts (`eligible_contacts`). */
  readonly eligibleContacts: Metric | null;
  /** MQL count (`mql`), never combined with QL. */
  readonly mql: Metric | null;
  /** Qualified Lead count (`ql`), never combined with MQL. */
  readonly ql: Metric | null;
  /** Meetings (`meetings`). */
  readonly meetings: Metric | null;
}

/**
 * Reads the C1, C1b and C2 card metrics (`campaigns._c1_candidate`, `_c1b_candidate`,
 * `_c2_candidate`, `_veto_metrics`). `crmQl` and `ql` both read the `ql` metric: C1 and C2
 * "Scale" show it as the linked CRM QL, C1b and C2 "Hold" in the pipeline columns.
 *
 * @param source - The campaign item or its evidence.
 * @returns Each metric, or `null` where the evidence lacks it.
 */
export function pipelineFields(source: MaybeSource): PipelineFields {
  const [
    spend,
    outcomes,
    expectedOutcomes,
    leads,
    maturedLeads,
    matchedLeads,
    unresolvedLeads,
    eligibleContacts,
    mql,
    ql,
    meetings,
  ] = metricsByKeys(source, [
    'spend',
    'outcomes',
    'expected_outcomes',
    'leads',
    'matured_leads',
    'matched_leads',
    'unresolved',
    'eligible_contacts',
    'mql',
    'ql',
    'meetings',
  ]);
  return {
    spend: spend ?? null,
    outcomes: outcomes ?? null,
    expectedOutcomes: expectedOutcomes ?? null,
    crmQl: ql ?? null,
    leads: leads ?? null,
    maturedLeads: maturedLeads ?? null,
    matchedLeads: matchedLeads ?? null,
    unresolvedLeads: unresolvedLeads ?? null,
    eligibleContacts: eligibleContacts ?? null,
    mql: mql ?? null,
    ql: ql ?? null,
    meetings: meetings ?? null,
  };
}

/**
 * Which C2 outcome an item is (`campaigns._c2_candidate`, `extra.outcome`): `"Scale"` (with a
 * B1 item and a decision subject) or `"Hold"` under the pipeline veto (no decision; "Mark
 * done"). B1 items carry `extra.outcome = "Scale"` as well.
 *
 * @param source - The C2 or B1 item or its evidence.
 * @returns The outcome, or `null` for any other item.
 */
export function scaleOutcome(source: MaybeSource): 'Scale' | 'Hold' | null {
  const outcome = readExtra(source)?.outcome;
  return outcome === 'Scale' || outcome === 'Hold' ? outcome : null;
}

/** One member search term of a C3b cluster. */
export interface LaunchGapTerm {
  /** The search term. */
  readonly term: string;
  /** Its window spend. */
  readonly spend: Money | null;
  /** Its window conversions as a decimal string (fractional under data-driven attribution). */
  readonly conversions: string | null;
  /** Its window clicks. */
  readonly clicks: number | null;
}

/** The data gap a launch idea rests on (AAP 0.5.4.4 C3a, C3b). */
export interface LaunchGap {
  /** `segment` (C3a), `cluster` (C3b), or `null` when the evidence shows neither. */
  readonly kind: 'segment' | 'cluster' | null;
  /** C3a: the demographic facet as displayed, for example "job function". */
  readonly facet: string | null;
  /** C3a: the facet value, for example "Finance". */
  readonly value: string | null;
  /** C3b: the cluster's head tokens. */
  readonly headTokens: readonly string[];
  /** C3b: the cluster's member terms with their spend and conversions. */
  readonly terms: readonly LaunchGapTerm[];
}

/** The launch gap of an item that is no launch idea. */
const NO_LAUNCH_GAP: LaunchGap = Object.freeze({
  kind: null,
  facet: null,
  value: null,
  headTokens: Object.freeze([]),
  terms: Object.freeze([]),
});

/**
 * Reads a launch idea's gap evidence. C3a (`campaigns._c3a_candidate`): `extra.facet_label`
 * (else `extra.facet`) and `extra.value`. C3b (`campaigns._c3b_candidate`):
 * `extra.head_tokens` and `extra.members` (`{term, spend, conversions, clicks}`). Shared-budget
 * `extra.members` entries (campaigns, no `term`) are never read as cluster terms.
 *
 * @param source - The launch item or its evidence.
 * @returns The gap; `kind: null` with empty fields for any other item.
 */
export function launchGap(source: MaybeSource): LaunchGap {
  const extra = readExtra(source);
  if (extra === null) {
    return NO_LAUNCH_GAP;
  }
  const facet = readString(extra.facet_label) ?? readString(extra.facet);
  const value = readString(extra.value);
  if (facet !== null && value !== null) {
    return { kind: 'segment', facet, value, headTokens: [], terms: [] };
  }
  const headTokens = readStringArray(extra.head_tokens);
  const terms: LaunchGapTerm[] = [];
  for (const entry of readArray(extra.members)) {
    if (!isRecord(entry)) {
      continue;
    }
    const term = readString(entry.term);
    if (term === null) {
      continue;
    }
    terms.push({
      term,
      spend: readMoney(entry.spend),
      conversions: readDecimal(entry.conversions),
      clicks: readInteger(entry.clicks),
    });
  }
  if (headTokens.length === 0 && terms.length === 0) {
    return NO_LAUNCH_GAP;
  }
  return { kind: 'cluster', facet: null, value: null, headTokens, terms };
}

// ---------------------------------------------------------------------------------------------
// Budget changes: B1, B2, B3 (and C2 "Scale")
// ---------------------------------------------------------------------------------------------

/**
 * The proposed daily budget of a B1, B2, B3 or C2 "Scale" item, already rounded to the
 * currency's minor unit by the backend. Read from `extra.proposed_daily_budget`, then C2's
 * `extra.b1_link.proposed_daily_budget`, then the `proposed_daily_budget` metric
 * (`budget.budget_evidence`, `campaigns._c2_candidate`), then `extra.proposed`.
 *
 * @param source - The item or its evidence.
 * @returns The amount as a decimal string, or `null` when the item proposes none.
 */
export function proposedDailyBudget(source: MaybeSource): Money | null {
  const extra = readExtra(source);
  return (
    readMoney(extra?.proposed_daily_budget) ??
    readMoney(extraRecord(extra, 'b1_link')?.proposed_daily_budget) ??
    readMoney(metricByKey(source, 'proposed_daily_budget')?.value) ??
    readMoney(extra?.proposed)
  );
}

/** One member campaign of a Google shared budget (AAP 0.5.4.5). */
export interface SharedMember {
  /** The member campaign's internal id, as a string. */
  readonly id: string;
  /** The campaign name. */
  readonly name: string;
  /** The window days the campaign drew from this budget; `null` when not given. */
  readonly memberDays: number | null;
}

/**
 * Reads a shared budget's member campaigns (`budget._members_extra`, `campaigns._members_extra`:
 * `extra.members` as `{campaign_id, name, member_days}`), for the card's member list and the
 * decision form (AAP 0.5.5 "with every member listed").
 *
 * @param source - The B1–B3 or C2 item on a shared budget, or its evidence.
 * @returns The members in stored order; `[]` for an own-budget subject.
 */
export function sharedMembers(source: MaybeSource): readonly SharedMember[] {
  const members: SharedMember[] = [];
  for (const entry of readArray(readExtra(source)?.members)) {
    if (!isRecord(entry)) {
      continue;
    }
    const id = readId(entry.campaign_id) ?? readId(entry.id);
    const name = readString(entry.name);
    if (id === null || name === null) {
      continue;
    }
    members.push({ id, name, memberDays: readInteger(entry.member_days) });
  }
  return members;
}

/** One B3 donor funding part of a B1 increase. */
export interface FundingDonor {
  /** The donor subject's internal id, as a string. */
  readonly id: string;
  /** The donor subject's type, `campaign` or `budget`; `null` when not given. */
  readonly type: string | null;
  /** The donor's name. */
  readonly name: string;
  /** The amount it moves to this B1. */
  readonly amount: Money;
}

/** How a B1 increase is funded (AAP 0.5.4.2 "Presenting reallocations"). */
export interface Funding {
  /** The recommended increase (`extra.increase`). */
  readonly increase: Money;
  /** The part funded by B3 reallocations (`extra.funded_by_reallocation`). */
  readonly fromReallocation: Money | null;
  /** The part from new budget (`extra.from_new_budget`). */
  readonly fromNewBudget: Money | null;
  /** Each donor and its amount (`extra.allocations`). */
  readonly donors: readonly FundingDonor[];
  /**
   * The backend's sentence (`budget.render_b1_funding`, `extra.funding`): "Raise by
   * {increase}: {Σ allocations} funded by reallocation from {donors}, {remainder} from new
   * budget"; `null` when not given.
   */
  readonly text: string | null;
}

/**
 * Reads a B1 item's funding (`budget._b1_candidate` `extra.increase`;
 * `precedence._with_b1_funding` `extra.allocations` as `{donor: {type, id}, donor_name,
 * amount}`, `funded_by_reallocation`, `from_new_budget` and `funding`). The amounts are the
 * backend's; nothing is added here.
 *
 * @param source - The B1 item or its evidence.
 * @returns The funding, or `null` when `extra.increase` is missing.
 */
export function funding(source: MaybeSource): Funding | null {
  const extra = readExtra(source);
  const increase = readMoney(extra?.increase);
  if (extra === null || increase === null) {
    return null;
  }
  const donors: FundingDonor[] = [];
  for (const entry of readArray(extra.allocations)) {
    if (!isRecord(entry) || !isRecord(entry.donor)) {
      continue;
    }
    const id = readId(entry.donor.id);
    const name = readString(entry.donor_name);
    const amount = readMoney(entry.amount);
    if (id === null || name === null || amount === null) {
      continue;
    }
    donors.push({ id, type: readString(entry.donor.type), name, amount });
  }
  return {
    increase,
    fromReallocation: readMoney(extra.funded_by_reallocation),
    fromNewBudget: readMoney(extra.from_new_budget),
    donors,
    text: readString(extra.funding),
  };
}

/**
 * A B3 item's freed daily amount (`budget._b3_candidate`, `extra.freed`): current daily budget
 * minus the proposed one. Part of it may fund B1 increases ({@link allocations}); the rest is
 * {@link unallocatedAmount}.
 *
 * @param source - The B3 item or its evidence.
 * @returns The amount, or `null` when absent.
 */
export function freedAmount(source: MaybeSource): Money | null {
  return readMoney(readExtra(source)?.freed);
}

/**
 * The part of a B3 item's freed amount that funds no B1 and stays a plain decrease
 * (`precedence._with_b3_allocations`, `extra.unallocated`).
 *
 * @param source - The B3 item or its evidence.
 * @returns The amount, or `null` when absent.
 */
export function unallocatedAmount(source: MaybeSource): Money | null {
  return readMoney(readExtra(source)?.unallocated);
}

/** One B3 reallocation, funding a named B1's recommended increase (AAP 0.5.4.2). */
export interface Allocation {
  /** The amount moved. */
  readonly amount: Money;
  /** The receiver subject's internal id, as a string. */
  readonly receiverId: string;
  /** The receiver subject's type, `campaign` or `budget`; `null` when not given. */
  readonly receiverType: string | null;
  /** The receiver's name. */
  readonly receiverName: string;
  /** The receiver's B1 recommendation id when the evidence names it; else `null`. */
  readonly receiverItemId: string | null;
  /**
   * The backend's line (`budget.render_b3_allocation`): "Move {allocation} to fund
   * {receiver}'s recommended increase (B1)"; `null` when not given.
   */
  readonly text: string | null;
}

/**
 * Reads a B3 item's reallocations (`precedence._with_b3_allocations`, `extra.allocations` as
 * `{receiver: {type, id}, receiver_name, amount, text}`). A B1's own `extra.allocations` lists
 * donors, not receivers, and yields `[]` here.
 *
 * @param source - The B3 item or its evidence.
 * @returns The allocations in allocation order; `[]` when there are none.
 */
export function allocations(source: MaybeSource): readonly Allocation[] {
  const result: Allocation[] = [];
  for (const entry of readArray(readExtra(source)?.allocations)) {
    if (!isRecord(entry) || !isRecord(entry.receiver)) {
      continue;
    }
    const receiverId = readId(entry.receiver.id);
    const receiverName = readString(entry.receiver_name);
    const amount = readMoney(entry.amount);
    if (receiverId === null || receiverName === null || amount === null) {
      continue;
    }
    result.push({
      amount,
      receiverId,
      receiverType: readString(entry.receiver.type),
      receiverName,
      receiverItemId: readId(entry.receiver_item_id),
      text: readString(entry.text),
    });
  }
  return result;
}

/** One budget-card day count against its requirement (AAP 0.5.4.1 "Evidence"). */
export interface DayCount {
  /** The metric key, for example `known_days`, `spending_days` or `capped_days`. */
  readonly key: string;
  /** The backend's label, for example "Known-budget days" or "Budget-active days". */
  readonly label: string;
  /** The days observed. */
  readonly observed: number;
  /** The days the rule's gate requires; `null` for counts no gate requires (idle, unknown). */
  readonly required: number | null;
}

/**
 * The day-count metrics of budget cards (`budget._history_metrics`), mapped to the gate checks
 * (`metrics.cap_constrained`, `high_utilization`, `under_utilized`) that require them. A count
 * two checks require (spending days: a minimum and a share of known-budget days; capped days:
 * a minimum and a share) shows the stricter of the two, which is the one the gate binds on.
 * Known idle, changed-intraday and unobserved days have no requirement.
 */
const DAY_COUNT_CHECKS: ReadonlyMap<string, readonly string[]> = new Map([
  ['known_days', ['known_budget_days']],
  ['spending_days', ['spending_days', 'spending_share']],
  ['capped_days', ['capped_days', 'capped_share']],
  ['high_utilization_days', ['high_utilization_days']],
  ['under_utilized_days', ['under_utilized_days']],
  ['idle_days', []],
  ['changed_intraday_days', []],
  ['unobserved_days', []],
]);

/** The stricter whole-day requirement of `checks` in `extra.gate`; `null` when none is given. */
function dayRequirement(
  gate: Record<string, unknown> | null,
  checks: readonly string[],
): number | null {
  let required: number | null = null;
  for (const check of checks) {
    const entry = extraRecord(gate, check);
    const value = readInteger(entry?.required);
    if (value !== null && (required === null || value > required)) {
      required = value;
    }
  }
  return required;
}

/**
 * Reads a budget card's day counts (B1–B4, C2; `budget.budget_evidence`): the metrics
 * `known_days` (known-budget days, or budget-active days for a shared budget), `spending_days`,
 * the rule's predicate (`capped_days`, `high_utilization_days` or `under_utilized_days`),
 * `idle_days`, `changed_intraday_days` and `unobserved_days`, in API order, each with its
 * requirement from `extra.gate` (`{check: {observed, required}}`). Shares are the backend's
 * metrics and are not recomputed here.
 *
 * @param source - The budget item or its evidence.
 * @returns The counts; `[]` for an item without budget history.
 */
export function dayCounts(source: MaybeSource): readonly DayCount[] {
  const gate = extraRecord(readExtra(source), 'gate');
  const counts: DayCount[] = [];
  for (const metric of metricsOf(source)) {
    const checks = DAY_COUNT_CHECKS.get(metric.key);
    const observed = readInteger(metric.value);
    if (checks === undefined || observed === null) {
      continue;
    }
    counts.push({
      key: metric.key,
      label: metric.label,
      observed,
      required: dayRequirement(gate, checks),
    });
  }
  return counts;
}

// ---------------------------------------------------------------------------------------------
// Copy / creative changes: CR1, CR2, CR3 (LinkedIn)
// ---------------------------------------------------------------------------------------------

/** One creative variant of a campaign (AAP 0.5.4.6). */
export interface VariantRow {
  /** The creative's internal id as a string, else its URN. */
  readonly creativeId: string;
  /** The creative's URN; `null` when not given. */
  readonly urn: string | null;
  /** The creative's display name. */
  readonly name: string;
  /** Window impressions. */
  readonly impressions: number | null;
  /** Clicks ÷ impressions, a ratio decimal string. */
  readonly clickRate: string | null;
  /** Lead form opens ÷ impressions; `null` for non-lead-gen campaigns or unreported opens. */
  readonly openRate: string | null;
  /** Leads ÷ form opens; `null` for non-lead-gen campaigns or unreported counts. */
  readonly completionRate: string | null;
  /** Window leads. */
  readonly leads: number | null;
}

/**
 * Reads a campaign's variants table (`creative._variant_row`, `extra.variants` of the CR3
 * item: `{creative_id, urn, name, impressions, leads, click_rate, open_rate, completion_rate,
 * …}`). Rates are ratio decimal strings (a number is converted with `String`); a rate the
 * campaign does not use is `null`.
 *
 * @param source - The CR3 item (the first item of a creative group) or its evidence.
 * @returns The variants in stored order; `[]` when absent.
 */
export function variants(source: MaybeSource): readonly VariantRow[] {
  const rows: VariantRow[] = [];
  for (const entry of readArray(readExtra(source)?.variants)) {
    if (!isRecord(entry)) {
      continue;
    }
    const urn = readString(entry.urn);
    const creativeId = readId(entry.creative_id) ?? urn;
    if (creativeId === null) {
      continue;
    }
    rows.push({
      creativeId,
      urn,
      name: readString(entry.name) ?? urn ?? creativeId,
      impressions: readInteger(entry.impressions),
      clickRate: readDecimal(entry.click_rate),
      openRate: readDecimal(entry.open_rate),
      completionRate: readDecimal(entry.completion_rate),
      leads: readInteger(entry.leads),
    });
  }
  return rows;
}

// ---------------------------------------------------------------------------------------------
// White papers: W1, W2 and "No change warranted" rows
// ---------------------------------------------------------------------------------------------

/**
 * The white-paper metric keys in the table's column order (`white_papers.METRIC_KEYS`,
 * AAP 0.5.5): spend, LinkedIn leads (a platform count), MQL, Qualified Lead, meetings booked,
 * other meetings (ordering unverified), deals after touch, cost per Qualified Lead and cost per
 * meeting booked. `mql` and `ql` are distinct keys; no combined key exists (AAP 0.5.4.7).
 */
export const WHITE_PAPER_METRIC_KEYS = [
  'spend',
  'linkedin_leads',
  'mql',
  'ql',
  'meetings_booked',
  'other_meetings',
  'deals_after_touch',
  'cost_per_ql',
  'cost_per_meeting',
] as const;

/** One of {@link WHITE_PAPER_METRIC_KEYS}. */
export type WhitePaperMetricKey = (typeof WHITE_PAPER_METRIC_KEYS)[number];

/** Default column labels of the per-creative cells, matching `white_papers._row_metrics`. */
const WHITE_PAPER_LABELS: Readonly<Record<WhitePaperMetricKey, string>> = Object.freeze({
  spend: 'Spend',
  linkedin_leads: 'LinkedIn leads (platform)',
  mql: 'MQL',
  ql: 'Qualified Lead',
  meetings_booked: 'Meetings booked (record created)',
  other_meetings: 'Other meetings (ordering unverified)',
  deals_after_touch: 'Deals after touch',
  cost_per_ql: 'Cost per Qualified Lead',
  cost_per_meeting: 'Cost per meeting booked',
});

/**
 * The field of an `extra.rows` entry (`attribution.CreditRow.to_json`) behind each white-paper
 * metric key: a creative row names leads `leads` and deals `deals`.
 */
const CREDIT_ROW_FIELDS: Readonly<Record<WhitePaperMetricKey, string>> = Object.freeze({
  spend: 'spend',
  linkedin_leads: 'leads',
  mql: 'mql',
  ql: 'ql',
  meetings_booked: 'meetings_booked',
  other_meetings: 'other_meetings',
  deals_after_touch: 'deals',
  cost_per_ql: 'cost_per_ql',
  cost_per_meeting: 'cost_per_meeting',
});

/** The unit of each white-paper cell, matching `white_papers._row_metrics`. */
const WHITE_PAPER_UNITS: Readonly<Record<WhitePaperMetricKey, string>> = Object.freeze({
  spend: 'currency',
  linkedin_leads: 'leads',
  mql: 'contacts',
  ql: 'contacts',
  meetings_booked: 'meetings',
  other_meetings: 'meetings',
  deals_after_touch: 'deals',
  cost_per_ql: 'currency',
  cost_per_meeting: 'currency',
});

/** The `key` of the "unknown creative" row, which has neither creative id nor URN. */
export const UNKNOWN_CREATIVE_KEY = 'unknown-creative';

/** The label of the "unknown creative" row when the backend gives none (AAP 0.5.4.7). */
const UNKNOWN_CREATIVE_LABEL = 'unknown creative';

/**
 * Reads a white-paper asset row's cells (W1, W2 and "No change warranted" items;
 * `white_papers._row_metrics`), keyed by {@link WHITE_PAPER_METRIC_KEYS}. Each cell is the
 * backend metric with its `floor` and `unavailable` markers, so `formatMetric` renders "≥ N",
 * "≤ X", "unavailable" and the backend's "—" for a zero denominator.
 *
 * @param source - The white-paper item or its evidence.
 * @returns One entry per key; `null` where the evidence lacks the metric.
 */
export function whitePaperCells(source: MaybeSource): Readonly<Record<string, Metric | null>> {
  const values = metricsByKeys(source, WHITE_PAPER_METRIC_KEYS);
  const cells: Record<string, Metric | null> = {};
  WHITE_PAPER_METRIC_KEYS.forEach((key, index) => {
    cells[key] = values[index] ?? null;
  });
  return cells;
}

/** One per-creative row of a white-paper asset (AAP 0.5.4.7 "Credit and de-duplication"). */
export interface WhitePaperRow {
  /** A stable row key: the creative URN, else its id, else {@link UNKNOWN_CREATIVE_KEY}. */
  readonly key: string;
  /** The creative's label (its headline, else URN), or "unknown creative". */
  readonly label: string;
  /** Whether this is the "unknown creative" row, kept and shown (AAP 0.5.5). */
  readonly isUnknownCreative: boolean;
  /** The row's cells keyed by {@link WHITE_PAPER_METRIC_KEYS}, `Metric`-shaped. */
  readonly cells: Readonly<Record<string, Metric | null>>;
  /**
   * Per key, why a value is missing: an unavailable count's `reason` (for example the
   * unresolved stage) or a cost's `note` ("no QL yet", "no meetings yet"); `null` otherwise.
   */
  readonly notes: Readonly<Record<string, string | null>>;
}

/** The label a per-creative cell takes: the asset row's metric label, else the default. */
function cellLabel(
  assetCells: Readonly<Record<string, Metric | null>>,
  key: WhitePaperMetricKey,
): string {
  return assetCells[key]?.label ?? WHITE_PAPER_LABELS[key];
}

/** A per-creative count cell from a `CountValue` JSON (`{value, floor, unavailable, reason}`). */
function countCell(key: WhitePaperMetricKey, label: string, raw: unknown): Metric | null {
  if (typeof raw === 'number') {
    const count = readInteger(raw);
    return {
      key,
      label,
      value: count,
      unit: WHITE_PAPER_UNITS[key],
      currency: null,
      floor: null,
      unavailable: count === null,
    };
  }
  if (!isRecord(raw)) {
    return null;
  }
  const count = readInteger(raw.value);
  const unavailable = raw.unavailable === true || count === null;
  return {
    key,
    label,
    value: unavailable ? null : count,
    unit: WHITE_PAPER_UNITS[key],
    currency: null,
    floor: unavailable ? null : raw.floor === true,
    unavailable,
  };
}

/**
 * A per-creative cost cell from a `CostValue` JSON (`{value, ceiling, note, unavailable}`):
 * "unavailable" for an unavailable denominator, "—" for a known zero denominator, else the
 * amount, marked as a ceiling ("≤ X") under a floor.
 */
function costCell(
  key: WhitePaperMetricKey,
  label: string,
  raw: unknown,
  currency: string | null,
): Metric | null {
  if (!isRecord(raw)) {
    return null;
  }
  const base = { key, label, unit: WHITE_PAPER_UNITS[key], currency };
  if (raw.unavailable === true) {
    return { ...base, value: null, floor: null, unavailable: true };
  }
  const amount = readMoney(raw.value);
  if (amount === null) {
    return { ...base, value: EM_DASH, floor: false, unavailable: false };
  }
  return { ...base, value: amount, floor: raw.ceiling === true, unavailable: false };
}

/** A per-creative spend cell; spend is always an amount in the row's currency. */
function spendCell(label: string, raw: unknown, currency: string | null): Metric {
  const amount = readMoney(raw);
  return {
    key: 'spend',
    label,
    value: amount,
    unit: WHITE_PAPER_UNITS.spend,
    currency,
    floor: null,
    unavailable: amount === null,
  };
}

/** The currency of a white-paper item's rows: `extra.asset.currency`, else the evidence's. */
function whitePaperCurrency(source: MaybeSource): string | null {
  return (
    readString(extraRecord(readExtra(source), 'asset')?.currency) ?? evidenceCurrency(source)
  );
}

/** Why a cell's value is missing: a count's `reason` or a cost's `note`. */
function cellNote(raw: unknown): string | null {
  if (!isRecord(raw)) {
    return null;
  }
  return readString(raw.note) ?? readString(raw.reason);
}

/**
 * Reads a white-paper asset row's per-creative rows (`white_papers._row_extra`, `extra.rows`:
 * `attribution.CreditRow.to_json` with `creative_id`, `creative_urn`, `label`, `spend`, `leads`,
 * the `mql`, `ql`, `meetings_booked`, `other_meetings` and `deals` counts and the `cost_per_ql`
 * and `cost_per_meeting` costs). The rows partition the asset's events, so they sum to the asset
 * row; the "unknown creative" row is kept and flagged. Each cell is `Metric`-shaped in the
 * asset's currency, with `floor` and `unavailable` set as the backend decided.
 *
 * @param source - The white-paper item or its evidence.
 * @returns The rows in stored order ("unknown creative" last); `[]` when absent.
 */
export function creativeRows(source: MaybeSource): readonly WhitePaperRow[] {
  const currency = whitePaperCurrency(source);
  const assetCells = whitePaperCells(source);
  const rows: WhitePaperRow[] = [];
  for (const entry of readArray(readExtra(source)?.rows)) {
    if (!isRecord(entry)) {
      continue;
    }
    const creativeId = readId(entry.creative_id);
    const urn = readString(entry.creative_urn);
    const isUnknownCreative = creativeId === null && urn === null;
    const cells: Record<string, Metric | null> = {};
    const notes: Record<string, string | null> = {};
    for (const key of WHITE_PAPER_METRIC_KEYS) {
      const raw = entry[CREDIT_ROW_FIELDS[key]];
      const label = cellLabel(assetCells, key);
      if (key === 'spend') {
        cells[key] = spendCell(label, raw, currency);
      } else if (key === 'cost_per_ql' || key === 'cost_per_meeting') {
        cells[key] = costCell(key, label, raw, currency);
      } else {
        cells[key] = countCell(key, label, raw);
      }
      notes[key] = cellNote(raw);
    }
    const fallbackLabel = isUnknownCreative ? UNKNOWN_CREATIVE_LABEL : (urn ?? creativeId);
    rows.push({
      key: urn ?? creativeId ?? UNKNOWN_CREATIVE_KEY,
      label: readString(entry.label) ?? fallbackLabel ?? UNKNOWN_CREATIVE_LABEL,
      isUnknownCreative,
      cells,
      notes,
    });
  }
  return rows;
}

/**
 * Why an asset row's cost is "—" or unavailable (`white_papers._row_extra`, `extra.costs`:
 * `{cost_per_ql: {note}, cost_per_meeting: {note}}`), for example "no QL yet" or "no meetings
 * yet" (AAP 0.5.4.7).
 *
 * @param source - The white-paper item or its evidence.
 * @returns `cost_per_ql` and `cost_per_meeting`, each the note or `null`.
 */
export function whitePaperCostNotes(source: MaybeSource): Readonly<Record<string, string | null>> {
  const costs = extraRecord(readExtra(source), 'costs');
  return {
    cost_per_ql: readString(extraRecord(costs, 'cost_per_ql')?.note),
    cost_per_meeting: readString(extraRecord(costs, 'cost_per_meeting')?.note),
  };
}

/** The cross-asset view of one currency cohort (AAP 0.5.4.7 "Across assets"). */
export interface CohortSummary {
  /** The cohort's currency; every amount below is in it. */
  readonly currency: string;
  /** The cohort's spend total across assets, computed by the backend; `null` when absent. */
  readonly spendTotal: Money | null;
  /** The note that counts overlap across assets, as the backend words it. */
  readonly overlapNote: string | null;
}

/**
 * Reads the cross-asset view each white-paper row carries (`white_papers._cohort_extra`,
 * `extra.cohort`: `{currency, spend_total, overlap_note, …}`). Only spend is totalled, within
 * one currency; no MQL, QL, meeting or deal total exists, and nothing is summed here.
 *
 * @param source - Any white-paper item of the cohort, or its evidence.
 * @returns The summary, or `null` when the evidence carries no cohort currency.
 */
export function cohortSummary(source: MaybeSource): CohortSummary | null {
  const cohort = extraRecord(readExtra(source), 'cohort');
  const currency = readString(cohort?.currency);
  if (cohort === null || currency === null) {
    return null;
  }
  return {
    currency,
    spendTotal: readMoney(cohort.spend_total),
    overlapNote: readString(cohort.overlap_note),
  };
}

// ---------------------------------------------------------------------------------------------
// Decision prefill (AAP 0.5.5 "Record decision")
// ---------------------------------------------------------------------------------------------

/** The rules whose "Record decision" prefills status `paused` (AAP 0.5.5: C1 and C1b). */
export const PAUSE_RULE_IDS = ['C1', 'C1b'] as const;

/** What the decision form opened from an item starts with. */
export interface DecisionContext {
  /** The proposed change, as a patch: only the fields the item proposes. */
  prefill?: Partial<DecisionPatchIn>;
  /** For a shared-budget subject: every member campaign, listed on the form. */
  members?: readonly { id: string; name: string }[];
  /** The currency the amounts are in. */
  currency?: string;
}

/** Whether a rule's decision prefills status `paused`. */
function isPauseRule(ruleId: string): boolean {
  return PAUSE_RULE_IDS.some((id) => id === ruleId);
}

/**
 * The decision-form context of an item's "Record decision" (AAP 0.5.5): status `paused` for C1
 * and C1b ({@link PAUSE_RULE_IDS}); otherwise the proposed daily budget
 * ({@link proposedDailyBudget}) that B1, B2, B3 and C2 "Scale" carry, written on the budget
 * subject when the budget is shared, with every member ({@link sharedMembers}); and the
 * evidence currency. No amount is invented: an item without a proposed amount gets no budget
 * prefill. The form submits only fields its subject's shape allows (AAP 0.5.2).
 *
 * @param item - The recommendation.
 * @returns `{}` when the item offers no decision (`decision_subject` absent).
 */
export function decisionContextFor(
  item: Pick<Item, 'rule_id' | 'decision_subject' | 'evidence'>,
): DecisionContext {
  const subject = item.decision_subject;
  if (subject === null || subject === undefined) {
    return {};
  }
  const context: DecisionContext = {};
  if (isPauseRule(item.rule_id)) {
    context.prefill = { status: 'paused' };
  } else {
    const proposed = proposedDailyBudget(item);
    if (proposed !== null) {
      context.prefill = { daily_budget: proposed };
    }
  }
  if (subject.type === 'budget') {
    context.members = sharedMembers(item).map(({ id, name }) => ({ id, name }));
  }
  const currency = evidenceCurrency(item);
  if (currency !== null) {
    context.currency = currency;
  }
  return context;
}
