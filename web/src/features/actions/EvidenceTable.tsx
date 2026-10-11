/**
 * `EvidenceTable`: the numbers behind a recommendation card's conclusion (AAP 0.5.5 card body;
 * AAP 0.5.4.8 evidence).
 *
 * The card leads with its action and reason; this component shows what the reason rests on, so
 * every number sits beside the claim it supports (AAP 0.1.2):
 *
 * 1. A captioned {@link DataTable} of the cited metrics (`evidence.metrics`), one row per metric:
 *    its label, with the metric's help text in a `Tooltip` when {@link metricHelp} defines one,
 *    its value in its unit and currency, and the baselines it was compared against.
 *    Floors render as "≥ N" for counts and "≤ X" for costs, and a missing value renders
 *    "unavailable", never `0` (AAP 0.5.4.7, 0.11.1). Every percentage in the table, value or
 *    paired baseline, shows one fixed precision ({@link evidenceRatioDigits}), so compared rates
 *    read apart. All of that formatting is {@link formatMetric}'s; this file formats no number
 *    of its own. Figures align to the end and text values and baselines to the start, and the
 *    three columns take fixed shares of the table, so the Value column ends at the same place on
 *    every card of the same width.
 * 2. The baselines no metric row carries (see {@link pairBaselines}), each in its own unit and
 *    currency when it records them, as C1's account cost per outcome does ("USD 37.99").
 * 3. The sample line, "Sample: {observed} / {required} {unit}", with its labelled projection when
 *    the rule recorded one (AAP 0.5.4.6 CR3).
 * 4. The test line, "Test: {test} · p = … · adjusted p = … · α …", when a test was used.
 * 5. "Thresholds applied": every `key = value` pair from `evidence.thresholds`, in key order. The
 *    values are the configured ones frozen with the run, never literals of this client
 *    (AAP 0.5.4).
 * 6. "Data used": each dataset the item read, with its completeness and, when the dataset
 *    recorded one, the last native date it covered: "google.metrics_daily: complete, covered
 *    through 2026-10-10 (Google day, America/New_York)". The date is shown exactly as stored and
 *    labelled with its source's day boundary (AAP 0.5.5 Behaviour; AAP 0.5.4.1: dates are never
 *    re-bucketed): the boundary the dataset entry recorded, as HubSpot's "(HubSpot day, UTC)",
 *    else, on evidence frozen before entries recorded one, the source's boundary from
 *    `evidence.window.source_days`. A dataset with no recorded date, such as `google.entities`,
 *    reads "google.entities: complete", with no coverage clause.
 *
 * Each part after the table renders only when the evidence holds it. Three evidence fields are
 * deliberately not rendered here, because another part of the card owns them: `window` (the
 * card's window chip; only its sources' day boundaries are read here, to label the "Data used"
 * dates), `provenance` (the card's historical-data notice) and `extra` (the section-specific
 * fields, such as white-paper creative rows or adjacent search terms).
 *
 * Built only on the design-system index (AAP 0.4.4): `DataTable` for the table, `Tooltip` for
 * metric help, `Stack` and `Inline` for layout and lists, with no styles of its own.
 *
 * @packageDocumentation
 */
import { memo } from 'react';
import type { ReactElement, ReactNode } from 'react';

import type {
  Baseline,
  DatasetCoverage,
  Evidence,
  Metric,
  SourceDay,
  SourceName,
  StatTest,
} from '../../api/types';
import {
  DataTable,
  Inline,
  Stack,
  Tooltip,
  type DataTableAlign,
  type DataTableColumn,
  type DataTableColumnShare,
} from '../../design-system';
import {
  DAY_UNITS,
  EM_DASH,
  MONEY_UNITS,
  MULTIPLE_UNITS,
  NOT_APPLICABLE,
  PERCENT_POINT_UNITS,
  P_VALUE_UNITS,
  RATE_UNITS,
  RATIO_UNITS,
  TEXT_UNITS,
  UNAVAILABLE,
  formatDecimal,
  formatMetric,
  formatSample,
  formatSourceDate,
  formatTestLine,
  isDecimalString,
  ratioMetricDigits,
  type MetricInput,
} from '../../lib/format';
import { metricHelp } from './metricHelp';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/** The table caption when the caller passes none, or only whitespace. */
export const DEFAULT_EVIDENCE_CAPTION = 'Evidence';

/** The table's single cell when the evidence cites no metric. */
export const NO_METRICS_MESSAGE = 'No metrics recorded';

/** The label of the thresholds list. */
export const THRESHOLDS_LABEL = 'Thresholds applied';

/** The label of the datasets list. */
export const DATASETS_LABEL = 'Data used';

/** The label of the unpaired baselines when some baselines sit in the table. */
export const OTHER_BASELINES_LABEL = 'Other baselines';

/** The label of the unpaired baselines when no baseline sits in the table. */
export const BASELINES_LABEL = 'Baselines';

/**
 * The Metric column's share of the evidence table, in twelfths. With {@link VALUE_SHARE} and
 * {@link BASELINE_SHARE} it makes one column grid for every evidence table, so the Value
 * column of every card of the same width ends at the same place, 8 twelfths across the table.
 * A sibling table whose figures should end where the evidence values end takes the same shares.
 */
export const METRIC_SHARE: DataTableColumnShare = 5;

/** The Value column's share of the evidence table, in twelfths (see {@link METRIC_SHARE}). */
export const VALUE_SHARE: DataTableColumnShare = 3;

/** The Baseline column's share of the evidence table, in twelfths (see {@link METRIC_SHARE}). */
export const BASELINE_SHARE: DataTableColumnShare = 4;

/** The name shown for a recorded test whose name is blank, so the line never starts with "·". */
const UNNAMED_TEST = 'Statistical test';

/** The rendering of an empty configured list, such as `[]`. */
const NONE = 'none';

/** The suffix of a baseline key derived from its metric's key, as in `click_rate_baseline`. */
const BASELINE_KEY_SUFFIX = '_baseline';

/**
 * Fraction digits of an unpaired baseline that records no unit or currency of its own. The
 * backend records rates and costs at up to six places (`analysis/budget.py`,
 * `analysis/keywords.py`), so six keeps the figure it recorded: K1's conversions per click
 * `0.04946` stays `0.04946`, from which the card's minimum click count can be checked, where a
 * two-place count would read `0.05`.
 */
const RECORDED_DIGITS = 6;

/**
 * Fraction digits of a numeric threshold: the most {@link formatDecimal} takes, so a
 * configured value prints as its shortest exact decimal (`0.95`, `1.15`, `0.0001`).
 */
const THRESHOLD_DIGITS = 20;

// ---------------------------------------------------------------------------------------------
// Baseline pairing
// ---------------------------------------------------------------------------------------------

/**
 * The evidence baselines split between the metric rows and the list below the table, as
 * {@link pairBaselines} decides.
 */
export interface BaselinePairing {
  /**
   * The baselines each metric row shows, by the metric's index in `evidence.metrics`, in
   * evidence order. A metric that no baseline pairs with has an empty list.
   */
  readonly byMetric: readonly (readonly Baseline[])[];
  /** The baselines no metric pairs with, in evidence order. */
  readonly unpaired: readonly Baseline[];
}

/**
 * Returns the index of the metric a baseline key pairs with, or `undefined` for none.
 *
 * Two forms pair, tried in this order:
 * 1. the same key, as in `campaign_cpl` and `campaign_cpl`;
 * 2. a key derived from the metric's: `{metric}_baseline` (CR2's `click_rate_baseline`) or any
 *    `{qualifier}_{metric}` (`baseline_cost_per_outcome`, `account_cost_per_conversion`,
 *    `max_cost_per_outcome`, `cohort_median_cost_per_ql`). When several metric keys fit, the
 *    longest wins, so `max_segment_cpl` pairs with `segment_cpl` rather than with a `cpl`.
 *
 * Ties keep the first metric in evidence order. Only the first metric holding a repeated key
 * takes part, so a baseline never pairs with two rows.
 *
 * @param baselineKey - The baseline's key.
 * @param metricIndex - Each metric key's first index in `evidence.metrics`.
 * @returns The metric index, or `undefined` when no metric fits.
 */
function matchMetric(
  baselineKey: string,
  metricIndex: ReadonlyMap<string, number>,
): number | undefined {
  const exact = metricIndex.get(baselineKey);
  if (exact !== undefined) {
    return exact;
  }
  let best: { readonly index: number; readonly length: number } | undefined;
  for (const [key, index] of metricIndex) {
    if (key === '') {
      continue;
    }
    const derived =
      baselineKey === `${key}${BASELINE_KEY_SUFFIX}` || baselineKey.endsWith(`_${key}`);
    if (derived && (best === undefined || key.length > best.length)) {
      best = { index, length: key.length };
    }
  }
  return best?.index;
}

/**
 * Pairs each evidence baseline with the metric it was compared against, so the table lists the
 * cited metrics against their baselines (AAP 0.5.5) and no baseline is ever left out
 * (AAP 0.5.4.8: every item stores "the baselines it was compared against").
 *
 * The backend names a comparison baseline after its metric rather than reusing the metric's
 * key: a budget item's `cost_per_outcome` is compared against `baseline_cost_per_outcome` and
 * `max_cost_per_outcome`, a K2 term's `cost_per_conversion` against
 * `campaign_cost_per_conversion`, and a CR2 stage rate `click_rate` against
 * `click_rate_baseline`. Pairing on identical keys alone would therefore pair none of them, so
 * the derived forms of {@link matchMetric} pair as well. Each baseline pairs with at most one
 * metric; a metric may carry several baselines. A baseline that fits no metric, such as K1's
 * account conversions per click, is returned in `unpaired` and listed below the table.
 *
 * The inputs are not mutated.
 *
 * @param metrics - `evidence.metrics`.
 * @param baselines - `evidence.baselines`.
 * @returns The baselines per metric row and those left unpaired.
 *
 * @example
 * ```ts
 * pairBaselines(
 *   [{ key: 'cost_per_outcome', … }, { key: 'spend', … }],
 *   [{ key: 'baseline_cost_per_outcome', … }, { key: 'baseline_rate', … }],
 * );
 * // { byMetric: [[baseline_cost_per_outcome], []], unpaired: [baseline_rate] }
 * ```
 */
export function pairBaselines(
  metrics: readonly Metric[],
  baselines: readonly Baseline[],
): BaselinePairing {
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

// ---------------------------------------------------------------------------------------------
// Value formatting (every number goes through lib/format)
// ---------------------------------------------------------------------------------------------

/**
 * A paired baseline as the value {@link formatMetric} formats: the baseline's value in its
 * metric's unit and currency. The metric's floor and unavailable markers describe the metric's
 * own value, so neither is carried over.
 */
function pairedBaselineInput(baseline: Baseline, metric: Metric): MetricInput {
  return { value: baseline.value, unit: metric.unit, currency: metric.currency };
}

/**
 * Formats a paired baseline in its metric's unit and currency: a cost-per-outcome baseline as
 * money in the metric's currency, a stage-rate baseline as a percentage at the evidence's
 * `ratioDigits` ({@link evidenceRatioDigits}), so a rate and its baseline read at one
 * precision. A `null` baseline still renders "unavailable", never `0`.
 */
function formatPairedBaseline(baseline: Baseline, metric: Metric, ratioDigits: number): string {
  return formatMetric(pairedBaselineInput(baseline, metric), { ratioDigits });
}

/**
 * The digits shared by the percentages of a metrics table: {@link ratioMetricDigits} over every
 * metric and every baseline paired with one, each as the table formats it.
 */
function tableRatioDigits(metrics: readonly Metric[], pairing: BaselinePairing): number {
  const baselines = metrics.flatMap((metric, index) =>
    (pairing.byMetric[index] ?? []).map((baseline) => pairedBaselineInput(baseline, metric)),
  );
  return ratioMetricDigits([...metrics, ...baselines]);
}

/**
 * The one fixed fraction-digit count of every percentage in an item's evidence table: its ratio
 * metrics and the baselines paired with them ({@link pairBaselines}). At it, a rate, its
 * baseline and its interval bounds read at one precision and stay apart whenever their values
 * differ, and each non-zero rate shows at least two significant digits (`percentDigits`):
 * CR1 lead rates of 0.000722 and 0.000698 read "0.072%" and "0.070%" rather than "0.1%" twice
 * (AAP 0.5.4.8). The card's headline figure is formatted with the same count, so it reads
 * exactly as the metric's row does.
 *
 * @param evidence - The item's frozen evidence.
 * @returns The fraction digits, to pass to {@link formatMetric} as `ratioDigits`.
 *
 * @example
 * ```ts
 * const digits = evidenceRatioDigits(item.evidence); // 3 for rates 0.000722 and 0.000698
 * formatMetric(rateB, { ratioDigits: digits }); // "0.070%"
 * ```
 */
export function evidenceRatioDigits(evidence: Pick<Evidence, 'metrics' | 'baselines'>): number {
  return tableRatioDigits(evidence.metrics, pairBaselines(evidence.metrics, evidence.baselines));
}

/**
 * Formats a baseline that no metric gives a unit to:
 *
 * - one that records its own unit or currency, as C1's account cost per outcome does, through
 *   {@link formatMetric} in that unit and currency, so `"37.986583"` in `USD` reads `"USD 37.99"`
 *   (AAP 0.5.5: numbers are formatted in the account currency);
 * - one with neither, such as K1's account conversions per click or a baseline frozen before
 *   baselines recorded a unit, as a number at the precision the backend recorded it
 *   ({@link RECORDED_DIGITS}), and anything else as {@link formatMetric} renders a unitless
 *   value (text verbatim, booleans as "yes" or "no", `null` as "unavailable").
 *
 * Blank unit or currency text counts as none, so it never turns a recorded rate into a count.
 *
 * @param baseline - The unpaired baseline as frozen in the evidence.
 * @returns The display text.
 */
function formatUnpairedBaseline(baseline: Baseline): string {
  const { value, unit, currency } = baseline;
  const ownsUnit = (unit ?? '').trim() !== '' || (currency ?? '').trim() !== '';
  if (ownsUnit) {
    return formatMetric({ value, unit, currency });
  }
  if (typeof value === 'number' || (typeof value === 'string' && isDecimalString(value))) {
    return formatDecimal(value, RECORDED_DIGITS);
  }
  return formatMetric({ value });
}

/**
 * Formats one configured threshold value from `evidence.thresholds` (AAP 0.5.4: every
 * threshold is a named value that appears in evidence). The values are the YAML scalars the
 * run used, so they print as configured:
 *
 * - a number, or a decimal string, as its shortest exact decimal: `10` → `"10"`, `0.7` →
 *   `"0.7"`, `1000` → `"1,000"`; a non-finite number → "unavailable";
 * - other text verbatim, and blank text as "unavailable";
 * - a boolean as "yes" or "no";
 * - `null` or `undefined` as "unavailable", never `0`;
 * - a list as its formatted entries joined by ", " (`[14, 28, 56, 90]` → `"14, 28, 56, 90"`),
 *   and an empty list as "none";
 * - a mapping as `{key: value; …}` in key order.
 *
 * @param value - The configured value.
 * @returns The display text.
 *
 * @example
 * ```ts
 * formatThresholdValue(0.95); // "0.95"
 * formatThresholdValue([14, 28, 56, 90]); // "14, 28, 56, 90"
 * ```
 */
export function formatThresholdValue(value: unknown): string {
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
      ? NONE
      : entries.map((entry) => formatThresholdValue(entry)).join(', ');
  }
  if (typeof value === 'object') {
    const pairs = sortedEntries(value);
    return `{${pairs.map(([key, entry]) => `${key}: ${formatThresholdValue(entry)}`).join('; ')}}`;
  }
  return UNAVAILABLE;
}

/** The own enumerable entries of an object, sorted by key (code-unit order, deterministic). */
function sortedEntries(record: object): [string, unknown][] {
  return Object.entries(record).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

/** The test line: "Test: " followed by {@link formatTestLine}'s rendering of the test. */
function testLine(test: StatTest): string {
  const name = test.name.trim() === '' ? UNNAMED_TEST : test.name;
  return `Test: ${formatTestLine({ ...test, name })}`;
}

/**
 * The sources a dataset name can be prefixed with (`google.metrics_daily`; `DatasetName`). A
 * record, so a source added to {@link SourceName} must be listed here before this compiles.
 */
const DATASET_SOURCES: Readonly<Record<SourceName, true>> = {
  google: true,
  linkedin: true,
  hubspot: true,
};

/** Whether `prefix` names a known source, so its dates can carry that source's day label. */
function isDatasetSource(prefix: string): prefix is SourceName {
  return Object.hasOwn(DATASET_SOURCES, prefix);
}

/**
 * The source a dataset belongs to: the name's prefix before its first `.`, as `google` in
 * `google.metrics_daily`. `null` when the name has no prefix or the prefix names no known
 * source.
 */
function datasetSource(dataset: string): SourceName | null {
  const dot = dataset.indexOf('.');
  if (dot <= 0) {
    return null;
  }
  const prefix = dataset.slice(0, dot);
  return isDatasetSource(prefix) ? prefix : null;
}

/**
 * The day boundary the item's window recorded for `source` (its first `source_days` entry), or
 * `null` when the window lists none. HubSpot holds instants, so its datasets have no entry.
 */
function sourceBoundary(sourceDays: readonly SourceDay[], source: SourceName): string | null {
  return sourceDays.find((day) => day.source === source)?.boundary ?? null;
}

/**
 * One dataset of "Data used" (AAP 0.5.4.8: the datasets read, with completeness and coverage):
 *
 * - `"{dataset}: {completeness}, covered through {date}"` when the dataset recorded the last
 *   native date it covered. The date is shown exactly as stored, never parsed or shifted, and
 *   labelled with its source's day boundary (AAP 0.5.5 Behaviour) by {@link formatSourceDate},
 *   the formatter `/sources` labels its dates with, so both read alike: `"2026-10-10 (Google
 *   day, America/New_York)"`, `"2026-10-10 (HubSpot day, UTC)"`. The boundary is the entry's own
 *   `boundary` when it is not blank; otherwise, for evidence frozen before entries recorded one,
 *   the boundary the item's `window.source_days` froze for the source. A source with neither
 *   names its day alone, never a guessed zone: an older HubSpot entry reads `"2026-10-10
 *   (HubSpot day)"`, since HubSpot holds instants and the window lists no HubSpot boundary. A
 *   dataset whose prefix names no known source shows the bare date, with no label.
 * - `"{dataset}: {completeness}"` when the date is `null` or blank, as for `google.entities`:
 *   the coverage clause is left out rather than reading "covered through unavailable".
 *
 * @param entry - the dataset as frozen in the evidence
 * @param sourceDays - the evidence window's `source_days`
 * @returns the entry's text
 */
function datasetText(entry: DatasetCoverage, sourceDays: readonly SourceDay[]): string {
  const head = `${entry.dataset}: ${entry.completeness}`;
  const through = entry.covered_through?.trim() ?? '';
  if (through === '') {
    return head;
  }
  const source = datasetSource(entry.dataset);
  if (source === null) {
    return `${head}, covered through ${through}`;
  }
  const own = entry.boundary?.trim() ?? '';
  const boundary = own === '' ? sourceBoundary(sourceDays, source) : own;
  return `${head}, covered through ${formatSourceDate(through, source, boundary)}`;
}

/**
 * Makes each key unique in order: the first occurrence keeps its key and a repeat becomes
 * `{key}~2`, `{key}~3`, and so on, skipping any form already taken. React keys and the table's
 * row keys must be unique, and frozen evidence is not guaranteed to hold distinct metric or
 * dataset names.
 */
function uniqueKeys(keys: readonly string[]): string[] {
  const used = new Set<string>();
  return keys.map((key) => {
    let candidate = key;
    for (let n = 2; used.has(candidate); n += 1) {
      candidate = `${key}~${String(n)}`;
    }
    used.add(candidate);
    return candidate;
  });
}

// ---------------------------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------------------------

/** One row of the metrics table: a cited metric and the baselines paired with it. */
interface MetricRow {
  /** The row's unique key: the metric's key, de-duplicated by {@link uniqueKeys}. */
  readonly rowKey: string;
  /** The metric as frozen in the evidence. */
  readonly metric: Metric;
  /** The baselines the metric was compared against, in evidence order. */
  readonly baselines: readonly Baseline[];
  /**
   * The fixed fraction digits of every percentage in the table ({@link evidenceRatioDigits}),
   * the same on every row, so the Value and Baseline columns each read at one precision.
   */
  readonly ratioDigits: number;
}

/** The metric's display label, or its key when the label is blank, so no row header is empty. */
function metricLabel(metric: Metric): string {
  return metric.label.trim() === '' ? metric.key : metric.label;
}

/**
 * The row header of a metric: its {@link metricLabel}, with the metric's help text in a
 * `Tooltip` when {@link metricHelp} has one for its key (AAP 0.4.2 "Help text on metrics"). The
 * help is the label's accessible description, through `aria-describedby`, never part of its
 * name, so the row header is still named by the label alone. A metric without help renders its
 * plain label.
 */
function MetricLabel({ metric }: { metric: Metric }): ReactNode {
  const label = metricLabel(metric);
  const help = metricHelp(metric.key);
  return help === undefined ? label : <Tooltip content={help}>{label}</Tooltip>;
}

/** The baseline's display label, or its key when the label is blank. */
function baselineLabel(baseline: Baseline): string {
  return baseline.label.trim() === '' ? baseline.key : baseline.label;
}

/**
 * One baseline as "{label}: {value}". The label says what the value is, such as "Account cost
 * per lead (same source and objective)" or "Highest cost per outcome allowed to scale", because
 * one metric can be compared against several. The value sits in its own element, so it can be
 * found on its own.
 */
function BaselineEntry({ label, value }: { label: string; value: string }): ReactElement {
  return (
    <div>
      {label}: <span>{value}</span>
    </div>
  );
}

/** The Baseline cell: each paired baseline on its own line, or "—" when there is none. */
function BaselineCell({ row }: { row: MetricRow }): ReactNode {
  if (row.baselines.length === 0) {
    return EM_DASH;
  }
  const keys = uniqueKeys(row.baselines.map((baseline) => baseline.key));
  return (
    <Stack gap={1}>
      {row.baselines.map((baseline, index) => (
        <BaselineEntry
          key={keys[index] ?? baseline.key}
          label={baselineLabel(baseline)}
          value={formatPairedBaseline(baseline, row.metric, row.ratioDigits)}
        />
      ))}
    </Stack>
  );
}

/**
 * The units {@link formatMetric} renders as a figure by name: money, ratios, rates, percentage
 * points, days, p-values and multiples. A metric with one of them, or with a currency, is a
 * figure even when the backend pre-rendered its value as a placeholder.
 */
const FIGURE_UNIT_SETS: readonly ReadonlySet<string>[] = [
  MONEY_UNITS,
  RATIO_UNITS,
  RATE_UNITS,
  PERCENT_POINT_UNITS,
  DAY_UNITS,
  P_VALUE_UNITS,
  MULTIPLE_UNITS,
];

/**
 * The texts that stand in for a figure that cannot be shown: "unavailable", "—" (a cost with a
 * zero denominator) and "n/a" (a utilization without a known-budget day), as `lib/format` and
 * the backend render them.
 */
const FIGURE_PLACEHOLDERS: ReadonlySet<string> = new Set([UNAVAILABLE, EM_DASH, NOT_APPLICABLE]);

/**
 * The alignment of a metric's Value cell: `'end'` for a figure, so it lines up with the other
 * numbers at the column's end in tabular figures and on one line; `'start'` for text, which then
 * reads from the start and wraps between words (AAP 0.5.5: numbers beside the claim they
 * support). The value's type decides, in the order {@link formatMetric} reads it:
 *
 * 1. A {@link TEXT_UNITS} metric (a search term, a matched keyword) is text, whatever its value,
 *    even "unavailable" or a term that looks numeric such as "2024".
 * 2. A boolean ("yes" or "no") is text.
 * 3. An unavailable metric (`unavailable: true` or a `null` value) is a figure: the backend
 *    records an absent number as `null`, never `0` (AAP 0.11.1), and its "unavailable" holds
 *    the figure's place.
 * 4. A number or a decimal string is a figure: money, a percentage, a count or day count, a
 *    p-value or a multiple, floors ("≥ N", "≤ X") included.
 * 5. Any other string is text the backend rendered itself, such as a status ("paused"), a
 *    budget ("daily budget USD 50.00") or a match type, unless it is one of the
 *    {@link FIGURE_PLACEHOLDERS} on a metric with a currency or a figure unit
 *    ({@link FIGURE_UNIT_SETS}): a white paper's "—" cost or a lifetime budget's "n/a"
 *    utilization holds a figure's place just as "unavailable" does.
 *
 * @param row - the metric row
 * @returns the Value cell's alignment
 */
function valueAlign(row: MetricRow): DataTableAlign {
  const { value, unit, currency, unavailable } = row.metric;
  const normalizedUnit = typeof unit === 'string' ? unit.trim().toLowerCase() : '';
  if (TEXT_UNITS.has(normalizedUnit) || typeof value === 'boolean') {
    return 'start';
  }
  if (unavailable === true || value === null || typeof value === 'number') {
    return 'end';
  }
  if (isDecimalString(value)) {
    return 'end';
  }
  const isFigureMetric =
    (typeof currency === 'string' && currency.trim() !== '') ||
    FIGURE_UNIT_SETS.some((units) => units.has(normalizedUnit));
  return isFigureMetric && FIGURE_PLACEHOLDERS.has(value.trim()) ? 'end' : 'start';
}

/**
 * The metrics table's columns: the metric label as the row header, then the value and its
 * baselines.
 *
 * - Value aligns each cell by its value's type ({@link valueAlign}): figures to the end in
 *   tabular figures, text such as a search term to the start, wrapping between words. Its
 *   header stays end-aligned over the figures.
 * - Baseline is start-aligned text: each entry reads "{label}: {value}", one per line, and a
 *   long label wraps between words.
 * - The three columns take fixed shares of the table, {@link METRIC_SHARE},
 *   {@link VALUE_SHARE} and {@link BASELINE_SHARE} twelfths, so every evidence table of a
 *   given width has the same column grid: the Value column ends at the same place on every
 *   card, whatever each table holds. A column whose content cannot fit its share at a narrow
 *   width takes its content's minimum, and the table scrolls in its region past that.
 */
const COLUMNS: readonly DataTableColumn<MetricRow>[] = [
  {
    key: 'metric',
    header: 'Metric',
    isRowHeader: true,
    share: METRIC_SHARE,
    render: (row) => <MetricLabel metric={row.metric} />,
  },
  {
    key: 'value',
    header: 'Value',
    align: 'end',
    alignCell: valueAlign,
    share: VALUE_SHARE,
    render: (row) => formatMetric(row.metric, { ratioDigits: row.ratioDigits }),
  },
  {
    key: 'baseline',
    header: 'Baseline',
    share: BASELINE_SHARE,
    render: (row) => <BaselineCell row={row} />,
  },
];

/**
 * A labelled list below the table. The label is visible text and also names the list, so
 * assistive technology announces which list it is and how many entries it holds.
 *
 * Spacing follows one rhythm, tightest inside a group: a label sits one step (`--space-1`)
 * above its list, entries and wrapped rows are two steps (`--space-2`) apart, and the blocks of
 * the evidence are three steps (`--space-3`) apart, the card body's own gap. A wrapped row of
 * thresholds therefore stays closer to its own list than to the next label.
 *
 * The group's `Stack` sets `breakLongWords`, so a long unbroken dataset name, threshold key or
 * baseline label wraps inside its entry instead of overflowing a narrow card (WCAG 2.2 SC
 * 1.4.10 Reflow). The evidence block's outer `Stack` leaves it off, because it holds the
 * `DataTable`, whose cells keep their own sizing.
 *
 * BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive with a semibold weight,
 * and feature code carries no CSS (AAP 0.4.4), so the label is a `<b>`, which the design-system
 * base layer sets to `--font-weight-semibold`, rather than a semibold paragraph; bind it to the
 * Blitzy OS label style when the source arrives. Text lines are `<div>` elements rather than
 * `<p>`: the base layer keeps the user-agent paragraph margins, which would add space that no
 * spacing token sets, so the `Stack` gaps alone space them.
 */
function LabelledList({
  label,
  layout,
  children,
}: {
  label: string;
  layout: 'inline' | 'stack';
  children: ReactNode;
}): ReactElement {
  return (
    <Stack gap={1} breakLongWords>
      <div>
        <b>{label}</b>
      </div>
      {layout === 'inline' ? (
        <Inline as="ul" gap={2} aria-label={label}>
          {children}
        </Inline>
      ) : (
        <Stack as="ul" gap={2} aria-label={label}>
          {children}
        </Stack>
      )}
    </Stack>
  );
}

/** Props of {@link EvidenceTable}. */
export interface EvidenceTableProps {
  /** The item's frozen evidence (`Item.evidence`), rendered exactly as the run recorded it. */
  evidence: Evidence;
  /**
   * The table's caption, which also names the table for assistive technology (every table has
   * one, AAP 0.5.5). Defaults to {@link DEFAULT_EVIDENCE_CAPTION}; blank text falls back to it,
   * so the table is always named.
   */
  caption?: string;
}

/**
 * Renders a recommendation's evidence: the captioned metrics table with each metric's baselines,
 * then, each only when present, the unpaired baselines, the sample line, the test line, the
 * thresholds applied and the datasets used (see the module overview).
 *
 * With no metrics the table still renders, captioned, with the single cell
 * {@link NO_METRICS_MESSAGE}, so the card states that nothing was cited rather than showing an
 * empty gap. Nothing here renders `null` or `undefined` as text: absent values read
 * "unavailable" and absent baselines "—".
 *
 * The component holds no state and has no effects; its output depends only on its props. It is
 * therefore memoized on them: an item's frozen evidence keeps its object identity across a
 * refetch that leaves it unchanged, so a card that renders again for another reason, such as a
 * live hold or flag, does not rebuild its evidence block.
 *
 * @param props - see {@link EvidenceTableProps}
 * @returns the evidence block
 *
 * @example
 * ```tsx
 * <EvidenceTable evidence={item.evidence} />
 * // Caption "Evidence"; rows such as "Cost per outcome | EUR 80.00 | Account cost per lead (same
 * // source and objective): EUR 40.00"; then "Sample: 9 / 7 known-budget days",
 * // "Thresholds applied" and "Data used".
 * ```
 */
export const EvidenceTable = memo(function EvidenceTable({
  evidence,
  caption,
}: EvidenceTableProps): ReactElement {
  const tableCaption =
    caption !== undefined && caption.trim() !== '' ? caption : DEFAULT_EVIDENCE_CAPTION;

  const { metrics, baselines, sample, test, thresholds, datasets } = evidence;
  const pairing = pairBaselines(metrics, baselines);
  const rowKeys = uniqueKeys(metrics.map((metric) => metric.key));
  const ratioDigits = tableRatioDigits(metrics, pairing);
  const rows: MetricRow[] = metrics.map((metric, index) => ({
    rowKey: rowKeys[index] ?? metric.key,
    metric,
    baselines: pairing.byMetric[index] ?? [],
    ratioDigits,
  }));

  const pairedCount = pairing.byMetric.reduce((count, list) => count + list.length, 0);
  const unpairedLabel = pairedCount > 0 ? OTHER_BASELINES_LABEL : BASELINES_LABEL;
  const unpairedKeys = uniqueKeys(pairing.unpaired.map((baseline) => baseline.key));

  const sampleText = sample === null ? '' : formatSample(sample);
  const thresholdEntries = sortedEntries(thresholds);
  const datasetKeys = uniqueKeys(datasets.map((entry) => entry.dataset));

  return (
    <Stack gap={3}>
      <DataTable
        caption={tableCaption}
        columns={COLUMNS}
        rows={rows}
        getRowKey={(row) => row.rowKey}
        emptyMessage={NO_METRICS_MESSAGE}
      />
      {pairing.unpaired.length > 0 ? (
        <LabelledList label={unpairedLabel} layout="stack">
          {pairing.unpaired.map((baseline, index) => (
            <li key={unpairedKeys[index] ?? baseline.key}>
              <BaselineEntry
                label={baselineLabel(baseline)}
                value={formatUnpairedBaseline(baseline)}
              />
            </li>
          ))}
        </LabelledList>
      ) : null}
      {sampleText !== '' ? <div>{`Sample: ${sampleText}`}</div> : null}
      {test !== null ? <div>{testLine(test)}</div> : null}
      {thresholdEntries.length > 0 ? (
        <LabelledList label={THRESHOLDS_LABEL} layout="inline">
          {thresholdEntries.map(([key, value]) => (
            <li key={key}>{`${key} = ${formatThresholdValue(value)}`}</li>
          ))}
        </LabelledList>
      ) : null}
      {datasets.length > 0 ? (
        <LabelledList label={DATASETS_LABEL} layout="stack">
          {datasets.map((entry, index) => (
            <li key={datasetKeys[index] ?? entry.dataset}>
              {datasetText(entry, evidence.window.source_days)}
            </li>
          ))}
        </LabelledList>
      ) : null}
    </Stack>
  );
});
