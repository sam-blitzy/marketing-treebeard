/**
 * Tests for `EvidenceTable`, the numbers behind a recommendation card's conclusion (AAP 0.5.5
 * card body: "the cited metrics against their baselines, with currency, a sample line
 * ('observed / required') and the test line where a test was used"; AAP 0.5.4.8 evidence).
 *
 * The suite holds the module to its TSDoc contract:
 *
 * - {@link pairBaselines} pairs each baseline with at most one metric: an identical key first,
 *   then `{metric}_baseline` or `{qualifier}_{metric}` with the longest metric key winning; the
 *   rest stay unpaired, in evidence order, and the inputs are never mutated.
 * - {@link formatThresholdValue} prints every configured scalar, list and mapping as configured
 *   and never renders `null` as `0`.
 * - `EvidenceTable` renders a captioned table (one row per metric, the value in its unit and
 *   currency, the paired baselines in the metric's unit), then only what the evidence holds: the
 *   unpaired baselines, "Sample: observed / required unit", "Test: …", "Thresholds applied" in
 *   key order and "Data used" with each dataset's completeness and, when recorded, the date it
 *   covers through, labelled with its source's day boundary: the entry's own, else the one
 *   `window.source_days` recorded for the source. The
 *   window itself, provenance and `extra` belong to other parts of the card and are not
 *   rendered here.
 * - The table's layout: Value cells end-align figures (numbers, decimal strings, unavailable
 *   values and the figure placeholders "—" and "n/a" of money and ratio metrics) and start-align
 *   text (text-unit terms, booleans and other pre-rendered strings) under an end-aligned header;
 *   Baseline is start-aligned; Metric, Value and Baseline take 5, 3 and 4 twelfths.
 *
 * Evidence comes from `../../test/fixtures`, either as the backend-shaped page items (C1 pause,
 * B1 increase) or built in the test for a single edge case. The component makes no request, so
 * most cases render it without providers. The last block renders it through
 * `renderWithProviders`, inside the Query and Router providers every card renders in, and
 * compares each rendering with `formatMetric`, `formatSample` and `formatTestLine` from
 * `lib/format`, so those assertions restate no locale rule of their own.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  Baseline,
  DatasetCoverage,
  Item,
  Metric,
  Sample,
  SourceDay,
  StatTest,
} from '../../api/types';
import * as formatModule from '../../lib/format';
import { formatMetric, formatSample, formatTestLine } from '../../lib/format';
import {
  FIXTURE_ITEM_IDS,
  FIXTURE_WINDOW,
  findPageItem,
  makeEvidence,
  makeRecommendationsPage,
} from '../../test/fixtures';
import { KEEP_SPACES, renderWithProviders } from '../../test/render';
import { textWithoutTooltips } from '../../test/text';
import {
  BASELINES_LABEL,
  BASELINE_SHARE,
  DATASETS_LABEL,
  DEFAULT_EVIDENCE_CAPTION,
  EvidenceTable,
  METRIC_SHARE,
  NO_METRICS_MESSAGE,
  OTHER_BASELINES_LABEL,
  THRESHOLDS_LABEL,
  VALUE_SHARE,
  evidenceRatioDigits,
  formatThresholdValue,
  pairBaselines,
} from './EvidenceTable';
import { metricHelp } from './metricHelp';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** A metric as the backend serialises it: every field present, `null` when not recorded. */
function metric(
  key: string,
  label: string,
  value: Metric['value'],
  unit: string | null,
  extra: Partial<Metric> = {},
): Metric {
  return { key, label, value, unit, currency: null, floor: null, unavailable: null, ...extra };
}

/** A baseline as the backend serialises it. */
function baseline(key: string, label: string, value: Baseline['value']): Baseline {
  return { key, label, value };
}

/** The keys of a list of baselines, for compact pairing assertions. */
function keysOf(list: readonly Baseline[]): string[] {
  return list.map((entry) => entry.key);
}

/**
 * The text of every cell of the evidence table, row by row, header row first: row headers
 * (`<th>`) and cells (`<td>`) in document order, as a browser shows them, so a metric label
 * reads without its closed `Tooltip` help text.
 */
function tableTexts(caption: string = DEFAULT_EVIDENCE_CAPTION): string[][] {
  const table = screen.getByRole('table', { name: caption });
  return within(table)
    .getAllByRole('row')
    .map((row) => Array.from(row.querySelectorAll('th, td'), textWithoutTooltips));
}

/** The text of each entry of the list named `name`. */
function listTexts(name: string): string[] {
  const list = screen.getByRole('list', { name });
  return within(list)
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** An item of the default fixture page, whose evidence has the backend's shape. */
function pageItem(id: number): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

afterEach(() => {
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------------------------
// pairBaselines
// ---------------------------------------------------------------------------------------------

describe('pairBaselines', () => {
  it('pairs an identical key before any derived key', () => {
    const metrics = [
      metric('cpl', 'Cost per lead', '10', 'currency'),
      metric('segment_cpl', 'Segment cost per lead', '8', 'currency'),
    ];
    const pairing = pairBaselines(metrics, [
      baseline('segment_cpl', 'Campaign cost per lead', '9'),
    ]);

    expect(pairing.byMetric.map(keysOf)).toEqual([[], ['segment_cpl']]);
    expect(pairing.unpaired).toEqual([]);
  });

  it('pairs {metric}_baseline and {qualifier}_{metric}, the longest metric key winning', () => {
    const metrics = [
      metric('cpl', 'Cost per lead', '10', 'currency'),
      metric('segment_cpl', 'Segment cost per lead', '8', 'currency'),
      metric('click_rate', 'Click rate', '0.01', 'ratio'),
    ];
    const pairing = pairBaselines(metrics, [
      baseline('max_segment_cpl', 'Highest segment cost per lead', '8'),
      baseline('click_rate_baseline', 'Account click rate', '0.02'),
      baseline('account_cpl', 'Account cost per lead', '12'),
    ]);

    expect(pairing.byMetric.map(keysOf)).toEqual([
      ['account_cpl'],
      ['max_segment_cpl'],
      ['click_rate_baseline'],
    ]);
    expect(pairing.unpaired).toEqual([]);
  });

  it('gives one metric several baselines and keeps the unmatched ones in order', () => {
    const { evidence } = pageItem(FIXTURE_ITEM_IDS.b1);
    const extra = [
      baseline('baseline_rate', 'Account conversions per click', '0.04946'),
      baseline('zeta', 'Other', 1),
    ];
    const pairing = pairBaselines(evidence.metrics, [...evidence.baselines, ...extra]);

    const costIndex = evidence.metrics.findIndex((entry) => entry.key === 'cost_per_outcome');
    expect(pairing.byMetric[costIndex]?.map((entry) => entry.key)).toEqual([
      'baseline_cost_per_outcome',
      'max_cost_per_outcome',
    ]);
    expect(pairing.byMetric.filter((list) => list.length > 0)).toHaveLength(1);
    expect(keysOf(pairing.unpaired)).toEqual(['baseline_rate', 'zeta']);
  });

  it('pairs only the first metric of a repeated key, and never through a blank key', () => {
    const metrics = [
      metric('spend', 'Spend', '1', 'currency'),
      metric('spend', 'Spend again', '2', 'currency'),
      metric('', 'Blank key', 3, null),
    ];
    const pairing = pairBaselines(metrics, [
      baseline('spend', 'Account spend', '4'),
      baseline('cost_', 'Trailing underscore', '5'),
    ]);

    expect(pairing.byMetric.map(keysOf)).toEqual([['spend'], [], []]);
    expect(keysOf(pairing.unpaired)).toEqual(['cost_']);
  });

  it('does not mutate its inputs', () => {
    const metrics = Object.freeze([
      Object.freeze(metric('cost_per_outcome', 'Cost per conversion', '82.37', 'currency')),
    ]);
    const baselines = Object.freeze([
      Object.freeze(baseline('baseline_cost_per_outcome', 'Account', '95.00')),
    ]);

    const pairing = pairBaselines(metrics, baselines);

    expect(pairing.byMetric[0]?.[0]).toBe(baselines[0]);
    expect(metrics).toHaveLength(1);
    expect(baselines).toHaveLength(1);
  });

  it('returns one empty list per metric when there is no baseline', () => {
    const pairing = pairBaselines([metric('a', 'A', 1, null), metric('b', 'B', 2, null)], []);

    expect(pairing).toEqual({ byMetric: [[], []], unpaired: [] });
  });
});

// ---------------------------------------------------------------------------------------------
// formatThresholdValue
// ---------------------------------------------------------------------------------------------

describe('formatThresholdValue', () => {
  it.each<[string, unknown, string]>([
    ['an integer', 10, '10'],
    ['a fraction', 0.7, '0.7'],
    ['a grouped integer', 1000, '1,000'],
    ['a small fraction, exactly', 0.0001, '0.0001'],
    ['a decimal string', '1.15', '1.15'],
    ['a non-finite number', Number.POSITIVE_INFINITY, 'unavailable'],
    ['NaN', Number.NaN, 'unavailable'],
    ['text, verbatim', 'MEETINGS_PUBLIC', 'MEETINGS_PUBLIC'],
    ['blank text', '   ', 'unavailable'],
    ['true', true, 'yes'],
    ['false', false, 'no'],
    ['null', null, 'unavailable'],
    ['undefined', undefined, 'unavailable'],
    ['a list', [14, 28, 56, 90], '14, 28, 56, 90'],
    ['an empty list', [], 'none'],
    ['a list holding null', [1, null], '1, unavailable'],
    ['a mapping, in key order', { b: 2, a: 'x', c: [] }, '{a: x; b: 2; c: none}'],
    ['a bigint', 10n, 'unavailable'],
  ])('formats %s', (_label, value, expected) => {
    expect(formatThresholdValue(value)).toBe(expected);
  });

  it('never renders null or a missing value as 0', () => {
    expect(formatThresholdValue(null)).not.toBe('0');
    expect(formatThresholdValue({ floor: null })).toBe('{floor: unavailable}');
  });
});

// ---------------------------------------------------------------------------------------------
// EvidenceTable
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable', () => {
  it('lists each cited metric against its baselines, in the metric unit and currency', () => {
    const { evidence } = pageItem(FIXTURE_ITEM_IDS.b1);
    render(<EvidenceTable evidence={evidence} />);

    const rows = tableTexts();
    expect(rows[0]).toEqual(['Metric', 'Value', 'Baseline']);
    expect(rows).toHaveLength(evidence.metrics.length + 1);
    expect(rows).toContainEqual(['Window spend', 'USD\u00a05,148.00', '—']);
    expect(rows).toContainEqual(['Window days', '28 days', '—']);
    expect(rows).toContainEqual(['Window conversions', '62.5', '—']);
    expect(rows).toContainEqual(['Window utilization (spend vs cap)', '99.0%', '—']);
    expect(rows).toContainEqual([
      'Cost per conversion',
      'USD\u00a082.37',
      'Account cost per conversion (same source and objective): USD\u00a095.00' +
        'Highest cost per outcome allowed to scale: USD\u00a095.00',
    ]);

    const costRow = screen.getByRole('rowheader', { name: 'Cost per conversion' }).closest('tr');
    expect(costRow).not.toBeNull();
    if (costRow !== null) {
      expect(within(costRow).getAllByText('USD 95.00')).toHaveLength(2);
    }
    // Every baseline sits in the table, so no list of unpaired baselines follows it.
    expect(screen.queryByRole('list', { name: OTHER_BASELINES_LABEL })).toBeNull();
    expect(screen.queryByRole('list', { name: BASELINES_LABEL })).toBeNull();
  });

  it('shows the sample line as "observed / required unit"', () => {
    render(<EvidenceTable evidence={pageItem(FIXTURE_ITEM_IDS.b1).evidence} />);

    expect(screen.getByText('Sample: 26 / 7 known-budget days')).toBeDefined();
  });

  it('lists unpaired baselines under "Baselines" when none is in the table', () => {
    const { evidence } = pageItem(FIXTURE_ITEM_IDS.c1);
    render(<EvidenceTable evidence={evidence} />);

    const [entry, ...rest] = within(
      screen.getByRole('list', { name: BASELINES_LABEL }),
    ).getAllByRole('listitem');
    expect(rest).toEqual([]);
    expect(entry?.textContent).toMatch(
      /^Account cost per conversion \(same source and objective\): /,
    );
    // The value sits in its own element; only its figure is pinned here, not its money format.
    expect(entry?.querySelector('span')?.textContent).toMatch(/^\D{0,2}92\.50?$/);
    expect(screen.getByText(BASELINES_LABEL).tagName).toBe('B');
    expect(screen.queryByRole('list', { name: OTHER_BASELINES_LABEL })).toBeNull();
    expect(screen.getByText('Sample: 3.4 / 3 expected conversions')).toBeDefined();
  });

  it('lists unpaired baselines under "Other baselines" when some sit in the table', () => {
    const evidence = makeEvidence({
      metrics: [metric('click_rate', 'Click rate', '0.012', 'ratio')],
      baselines: [
        baseline('click_rate_baseline', 'Campaign click rate', '0.02'),
        baseline('baseline_rate', 'Account conversions per click', '0.04946'),
        baseline('enabled', 'Tracking enabled', true),
        baseline('note', 'Note', 'see run 101'),
        baseline('missing', 'Missing', null),
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(tableTexts()[1]).toEqual(['Click rate', '1.2%', 'Campaign click rate: 2.0%']);
    expect(listTexts(OTHER_BASELINES_LABEL)).toEqual([
      'Account conversions per click: 0.04946',
      'Tracking enabled: yes',
      'Note: see run 101',
      'Missing: unavailable',
    ]);
  });

  it('shows every threshold applied as "key = value" in key order', () => {
    render(<EvidenceTable evidence={pageItem(FIXTURE_ITEM_IDS.c1).evidence} />);

    expect(listTexts(THRESHOLDS_LABEL)).toEqual([
      'c1.min_expected_outcomes = 3',
      'c1.min_spend_day_share = 0.7',
      'c1.min_window_days = 14',
      'sources.stale_after_hours = 24',
      'window.sustained_min_days = 7',
      'window.trusted_min_days = 14',
    ]);
  });

  it('lists the datasets used with their completeness and the last date they cover', () => {
    const evidence = makeEvidence({
      datasets: [
        {
          dataset: 'google.metrics_daily',
          completeness: 'complete',
          covered_through: FIXTURE_WINDOW.end,
        },
        {
          dataset: 'google.search_terms',
          completeness: 'incomplete',
          covered_through: '2026-10-03',
        },
        { dataset: 'hubspot.contacts', completeness: 'unknown', covered_through: null },
        { dataset: 'hubspot.meetings', completeness: 'unknown', covered_through: '  ' },
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(listTexts(DATASETS_LABEL)).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-05 (Google day, America/New_York)',
      'google.search_terms: incomplete, covered through 2026-10-03 (Google day, America/New_York)',
      'hubspot.contacts: unknown',
      'hubspot.meetings: unknown',
    ]);
  });

  it('shows the test line with its adjusted p-value and alpha when a test was used', () => {
    const evidence = makeEvidence({
      test: { name: 'fisher_exact', p_value: 0.012, p_adjusted: 0.036, alpha: 0.05 },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(
      screen.getByText("Test: Fisher's exact test · p = 0.0120 · adjusted p = 0.0360 · α 0.05"),
    ).toBeDefined();
  });

  it('states a B4 p-value as its reason does: "p < 0.0001", never "p < 0.001"', () => {
    const evidence = makeEvidence({
      test: {
        name: 'two-sample Poisson rate test',
        p_value: '0.0000000000127560',
        p_adjusted: null,
        alpha: '0.05',
      },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(
      screen.getByText('Test: Two-sample Poisson rate test · p < 0.0001 · α 0.05'),
    ).toBeDefined();
  });

  it('states a B4 p-value with digits past 10^-307 as "p < 0.0001", never "unavailable"', () => {
    // poisson_rate_test(0, 1000, 1390, 1000) = 3.12936E-304, in the API's fixed point.
    const evidence = makeEvidence({
      test: {
        name: 'two_sample_poisson_score',
        p_value: `0.${'0'.repeat(303)}312936`,
        p_adjusted: null,
        alpha: '0.05',
      },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(
      screen.getByText('Test: Two-sample Poisson rate test · p < 0.0001 · α 0.05'),
    ).toBeDefined();
    expect(screen.queryByText(/p = unavailable/u)).toBeNull();
  });

  it('names a test recorded with a blank name, so the line never starts with a separator', () => {
    const evidence = makeEvidence({
      test: { name: '  ', p_value: '0.0004', p_adjusted: null, alpha: null },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(screen.getByText('Test: Statistical test · p = 0.0004')).toBeDefined();
  });

  it('defaults the caption, also for a blank one, and takes a custom one', () => {
    const evidence = makeEvidence({
      metrics: [metric('spend', 'Spend', '1', 'currency', { currency: 'USD' })],
    });
    const { unmount } = render(<EvidenceTable evidence={evidence} caption="   " />);
    expect(screen.getByRole('table', { name: DEFAULT_EVIDENCE_CAPTION })).toBeDefined();
    unmount();

    render(<EvidenceTable evidence={evidence} caption="Keyword evidence" />);
    expect(screen.getByRole('table', { name: 'Keyword evidence' })).toBeDefined();
    expect(screen.queryByRole('table', { name: DEFAULT_EVIDENCE_CAPTION })).toBeNull();
  });

  it('states that no metric was cited and renders nothing the evidence does not hold', () => {
    render(<EvidenceTable evidence={makeEvidence({ datasets: [] })} />);

    expect(tableTexts()).toEqual([['Metric', 'Value', 'Baseline'], [NO_METRICS_MESSAGE]]);
    expect(screen.queryAllByRole('list')).toEqual([]);
    expect(screen.queryByText(/^Sample:/)).toBeNull();
    expect(screen.queryByText(/^Test:/)).toBeNull();
  });

  it('omits the sample line when neither sample size is recorded', () => {
    const evidence = makeEvidence({
      sample: { observed: null, required: null, unit: 'clicks', projected: null },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(screen.queryByText(/^Sample:/)).toBeNull();
  });

  it('labels a projected sample as a projection', () => {
    const evidence = makeEvidence({
      sample: {
        observed: 3100,
        required: 4200,
        unit: 'form opens',
        projected: {
          value: 52000,
          unit: 'impressions',
          basis: 'projected through the observed open rate',
        },
      },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(
      screen.getByText(
        'Sample: 3,100 / 4,200 form opens · ' +
          '≈ 52,000 impressions (projected through the observed open rate)',
      ),
    ).toBeDefined();
  });

  it('renders unavailable values, floors and blank labels without ever showing null or 0', () => {
    const evidence = makeEvidence({
      metrics: [
        metric('ql', 'Qualified Leads', null, 'leads', { unavailable: true }),
        metric('mql', 'MQL', 12, 'leads', { floor: true }),
        metric('cost_per_ql', 'Cost per Qualified Lead', '40.00', 'currency', {
          currency: 'EUR',
          floor: true,
        }),
        metric('segment_cpl', '  ', '8.00', 'currency', { currency: 'EUR' }),
      ],
      baselines: [baseline('max_segment_cpl', ' ', null)],
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(tableTexts().slice(1)).toEqual([
      ['Qualified Leads', 'unavailable', '—'],
      ['MQL', '≥ 12', '—'],
      ['Cost per Qualified Lead', '≤ EUR\u00a040.00', '—'],
      ['segment_cpl', 'EUR\u00a08.00', 'max_segment_cpl: unavailable'],
    ]);
    expect(screen.queryByText(/\bnull\b|\bundefined\b/)).toBeNull();
  });

  it('end-aligns figures and start-aligns text values, with the Value header at the end', () => {
    const evidence = makeEvidence({
      metrics: [
        metric('term', 'Search term', 'free attribution software download', 'text'),
        metric('year_term', 'Year term', '2024', 'text'),
        metric('missing_term', 'Missing term', null, 'text'),
        metric('spend', 'Spend', '517.85', 'currency', { currency: 'USD' }),
        metric('conversions', 'Conversions', 0, 'conversions'),
        metric('ql', 'Qualified Leads', null, 'leads', { unavailable: true }),
        metric('mql', 'MQL', 12, 'leads', { floor: true }),
        metric('cost_per_ql', 'Cost per Qualified Lead', '—', 'currency', { currency: 'EUR' }),
        metric('utilization', 'Window utilization', 'n/a', 'ratio'),
        metric('status', 'Platform status', 'paused', 'status'),
        metric('status_none', 'Decided status', 'unavailable', 'status'),
        metric('budget', 'Platform budget', 'daily budget USD 50.00', 'budget'),
        metric('shared', 'Shared budget', true, null),
        metric('match', 'Match type', 'n/a', null),
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    const table = screen.getByRole('table', { name: DEFAULT_EVIDENCE_CAPTION });
    const valueHeader = within(table).getByRole('columnheader', { name: 'Value' });
    expect(valueHeader.classList.contains('ds-datatable__cell--end')).toBe(true);
    const alignments = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => {
        const value = row.querySelectorAll('td')[0];
        const header = row.querySelector('th');
        return [
          header === null ? null : textWithoutTooltips(header),
          value?.classList.contains('ds-datatable__cell--end') === true ? 'end' : 'start',
        ];
      });
    expect(alignments).toEqual([
      ['Search term', 'start'],
      ['Year term', 'start'],
      ['Missing term', 'start'],
      ['Spend', 'end'],
      ['Conversions', 'end'],
      ['Qualified Leads', 'end'],
      ['MQL', 'end'],
      ['Cost per Qualified Lead', 'end'],
      ['Window utilization', 'end'],
      ['Platform status', 'start'],
      ['Decided status', 'start'],
      ['Platform budget', 'start'],
      ['Shared budget', 'start'],
      ['Match type', 'start'],
    ]);
    // Each start cell carries the explicit start class, so no value is left unaligned.
    for (const row of within(table).getAllByRole('row').slice(1)) {
      const value = row.querySelectorAll('td')[0];
      expect(
        value?.classList.contains('ds-datatable__cell--start') !==
          value?.classList.contains('ds-datatable__cell--end'),
      ).toBe(true);
    }
  });

  it('start-aligns the Baseline header and every baseline cell', () => {
    const evidence = makeEvidence({
      metrics: [
        metric('cost_per_outcome', 'Cost per outcome', '80.00', 'currency', { currency: 'USD' }),
        metric('clicks', 'Clicks', 172, 'clicks'),
      ],
      baselines: [
        baseline(
          'account_cost_per_outcome',
          'Account cost per lead (same source and objective)',
          '32.08',
        ),
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    const table = screen.getByRole('table', { name: DEFAULT_EVIDENCE_CAPTION });
    const header = within(table).getByRole('columnheader', { name: 'Baseline' });
    expect(header.classList.contains('ds-datatable__cell--start')).toBe(true);
    const baselineCells = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.querySelectorAll('td')[1]);
    expect(baselineCells.map((cell) => cell?.textContent)).toEqual([
      'Account cost per lead (same source and objective): USD\u00a032.08',
      '—',
    ]);
    for (const cell of baselineCells) {
      expect(cell?.classList.contains('ds-datatable__cell--start')).toBe(true);
      expect(cell?.classList.contains('ds-datatable__cell--end')).toBe(false);
    }
  });

  it('gives Metric, Value and Baseline the shared 5, 3 and 4 twelfths of every evidence table', () => {
    render(
      <>
        <EvidenceTable evidence={pageItem(FIXTURE_ITEM_IDS.b1).evidence} caption="First" />
        <EvidenceTable evidence={makeEvidence({ metrics: [] })} caption="Second" />
      </>,
    );

    expect([METRIC_SHARE, VALUE_SHARE, BASELINE_SHARE]).toEqual([5, 3, 4]);
    for (const caption of ['First', 'Second']) {
      const table = screen.getByRole('table', { name: caption });
      expect(Array.from(table.querySelectorAll('colgroup > col'), (col) => col.className)).toEqual([
        'ds-datatable__col--share-5',
        'ds-datatable__col--share-3',
        'ds-datatable__col--share-4',
      ]);
    }
  });

  it('renders repeated metric, baseline and dataset keys apart, without key warnings', () => {
    const consoleError = vi.spyOn(console, 'error');
    const evidence = makeEvidence({
      metrics: [
        metric('spend', 'Spend (Google)', '10', 'currency', { currency: 'USD' }),
        metric('spend', 'Spend (LinkedIn)', '20', 'currency', { currency: 'EUR' }),
      ],
      baselines: [
        baseline('spend', 'Account spend', '30'),
        baseline('spend', 'Account spend, again', '40'),
        baseline('rate', 'Rate A', 0.1),
        baseline('rate', 'Rate B', 0.2),
      ],
      datasets: [
        {
          dataset: 'google.metrics_daily',
          completeness: 'complete',
          covered_through: '2026-10-05',
        },
        {
          dataset: 'google.metrics_daily',
          completeness: 'incomplete',
          covered_through: '2026-10-04',
        },
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(tableTexts().slice(1)).toEqual([
      [
        'Spend (Google)',
        'USD\u00a010.00',
        'Account spend: USD\u00a030.00Account spend, again: USD\u00a040.00',
      ],
      ['Spend (LinkedIn)', 'EUR\u00a020.00', '—'],
    ]);
    expect(listTexts(OTHER_BASELINES_LABEL)).toEqual(['Rate A: 0.1', 'Rate B: 0.2']);
    expect(listTexts(DATASETS_LABEL)).toHaveLength(2);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('leaves the window, provenance and extra to the rest of the card', () => {
    // No dataset is listed, so no "Data used" date carries the window's day-boundary label and
    // any boundary in the text would be the window itself.
    const evidence = makeEvidence({
      metrics: [metric('spend', 'Spend', '1', 'currency', { currency: 'USD' })],
      datasets: [],
      extra: { proposed_status: 'paused' },
    });
    const { container } = render(<EvidenceTable evidence={evidence} />);

    const text = container.textContent;
    expect(text).not.toContain(FIXTURE_WINDOW.start);
    expect(text).not.toContain('America/New_York');
    expect(text).not.toContain('simulated');
    expect(text).not.toContain('paused');
  });
});

// ---------------------------------------------------------------------------------------------
// EvidenceTable inside the app providers, against lib/format
// ---------------------------------------------------------------------------------------------

/**
 * The row of the evidence table whose row header is `label`.
 *
 * @param label - The metric's display label, the row header's accessible name.
 * @returns The `<tr>` holding that row header.
 * @throws Error when the row header sits outside a table row.
 */
function metricRow(label: string): HTMLElement {
  const row = screen.getByRole('rowheader', { name: label }).closest('tr');
  if (row === null) {
    throw new Error(`The row header "${label}" sits outside a table row.`);
  }
  return row;
}

/**
 * The value and baseline cells of the row whose row header is `label`, in column order.
 *
 * @param label - The metric's display label.
 * @returns The row's two data cells.
 * @throws Error when the row does not hold exactly a value and a baseline cell.
 */
function metricCells(label: string): { value: HTMLElement; baseline: HTMLElement } {
  const [value, baseline, ...rest] = within(metricRow(label)).getAllByRole('cell');
  if (value === undefined || baseline === undefined || rest.length > 0) {
    throw new Error(`The row "${label}" does not hold exactly a value and a baseline cell.`);
  }
  return { value, baseline };
}

describe('EvidenceTable inside the app providers', () => {
  /** A cited spend in its account currency. */
  const spend = metric('spend', 'Spend', '1240.50', 'currency', { currency: 'USD' });
  /** A cited count compared against nothing. */
  const clicks = metric('clicks', 'Clicks', 412, 'clicks');
  /** A baseline that shares the spend metric's key. */
  const accountSpend = baseline('spend', 'Account spend', '980.00');

  it('names the table "Evidence" by default and by the caption passed', async () => {
    const evidence = makeEvidence({ metrics: [spend] });
    const first = await renderWithProviders(<EvidenceTable evidence={evidence} />);

    expect(screen.getByRole('table', { name: 'Evidence' })).toBeDefined();
    expect(screen.getByText('Evidence').closest('caption')?.textContent).toBe('Evidence');
    first.unmount();

    await renderWithProviders(<EvidenceTable evidence={evidence} caption="Search term evidence" />);

    expect(screen.getByRole('table', { name: 'Search term evidence' })).toBeDefined();
    expect(screen.queryByRole('table', { name: 'Evidence' })).toBeNull();
  });

  it('shows a metric and its same-key baseline in one row, and "—" where none applies', async () => {
    const evidence = makeEvidence({ metrics: [spend, clicks], baselines: [accountSpend] });
    await renderWithProviders(<EvidenceTable evidence={evidence} />);

    // The baseline is formatted in its metric's unit and currency.
    const spendBaseline = formatMetric({
      value: accountSpend.value,
      unit: spend.unit,
      currency: spend.currency,
    });
    const spendRow = within(metricRow('Spend'));
    expect(spendRow.getByText(formatMetric(spend), KEEP_SPACES)).toBeDefined();
    expect(spendRow.getByText(spendBaseline, KEEP_SPACES)).toBeDefined();
    expect(metricCells('Spend').baseline.textContent).toBe(`Account spend: ${spendBaseline}`);

    const clickCells = metricCells('Clicks');
    expect(clickCells.value.textContent).toBe(formatMetric(clicks));
    expect(clickCells.baseline.textContent).toBe('—');
  });

  it('shows an unavailable metric as "unavailable", never as 0', async () => {
    const qualifiedLeads = metric('ql', 'Qualified Leads', null, 'leads', { unavailable: true });
    await renderWithProviders(
      <EvidenceTable evidence={makeEvidence({ metrics: [qualifiedLeads] })} />,
    );

    const { value } = metricCells('Qualified Leads');
    expect(value.textContent).toBe('unavailable');
    expect(value.textContent).toBe(formatMetric(qualifiedLeads));
    expect(value.textContent).not.toMatch(/0/);
  });

  it('marks a floored count "≥ N" and a floored cost "≤ X"', async () => {
    const mql = metric('mql', 'MQL', 12, 'leads', { floor: true });
    const costPerQl = metric('cost_per_ql', 'Cost per Qualified Lead', '412.50', 'currency', {
      currency: 'EUR',
      floor: true,
    });
    await renderWithProviders(
      <EvidenceTable evidence={makeEvidence({ metrics: [mql, costPerQl] })} />,
    );

    expect(metricCells('MQL').value.textContent).toBe('≥ 12');
    expect(metricCells('MQL').value.textContent).toBe(formatMetric(mql));
    const cost = metricCells('Cost per Qualified Lead').value.textContent;
    expect(cost).toBe(formatMetric(costPerQl));
    expect(cost).toMatch(/^≤ /);
  });

  it('shows the sample as "observed / required unit", with its projection when recorded', async () => {
    const clickSample: Sample = { observed: 37, required: 64, unit: 'clicks', projected: null };
    const first = await renderWithProviders(
      <EvidenceTable evidence={makeEvidence({ sample: clickSample })} />,
    );

    expect(screen.getByText('Sample: 37 / 64 clicks')).toBeDefined();
    // The figure is joined by no-break spaces (U+00A0), so it never wraps inside "37 / 64";
    // getByText above matches because Testing Library collapses them in the rendered text.
    expect(`Sample: ${formatSample(clickSample)}`).toBe('Sample: 37\u00A0/\u00A064 clicks');
    expect(screen.getByText(/^Sample: /u).textContent).toBe('Sample: 37\u00A0/\u00A064 clicks');
    first.unmount();

    const openSample: Sample = {
      observed: 1850,
      required: 2600,
      unit: 'form opens',
      projected: {
        value: 41000,
        unit: 'impressions',
        basis: 'projected through the observed open rate',
      },
    };
    await renderWithProviders(<EvidenceTable evidence={makeEvidence({ sample: openSample })} />);

    // Testing Library normalises the rendered text but not a string matcher, so the line is
    // found by its prefix and its exact text, no-break spaces included, compared after.
    const line = screen.getByText(/^Sample: /u);
    expect(line.textContent).toBe(`Sample: ${formatSample(openSample)}`);
    expect(line.textContent).toMatch(/^Sample: \S+\u00A0\/\u00A0\S+ form opens · ≈ /u);
    expect(line.textContent).toContain('impressions (projected through the observed open rate)');
  });

  it('shows a floored sample count as "≥ N", as the metric it repeats', async () => {
    // AAP 0.5.4.7/0.5.5: under an attribution floor W1's Qualified Leads read "≥ 34" in the
    // table, so the sample line repeats that count with its floor, never "34 / 3".
    const qlSample: Sample = {
      observed: 34,
      required: 3,
      unit: 'Qualified Leads',
      projected: null,
      floor: true,
    };
    await renderWithProviders(
      <EvidenceTable
        evidence={makeEvidence({
          metrics: [metric('ql', 'Qualified Lead', 34, 'contacts', { floor: true })],
          sample: qlSample,
        })}
      />,
    );

    expect(screen.getByText('Sample: ≥ 34 / 3 Qualified Leads')).toBeDefined();
    expect(screen.getByText(/^Sample: /u).textContent).toBe(
      'Sample: ≥ 34\u00A0/\u00A03 Qualified Leads',
    );
    expect(metricCells('Qualified Lead').value.textContent).toBe('≥ 34');
  });

  it('shows the test line with the adjusted p-value', async () => {
    const fisher: StatTest = {
      name: 'fisher_exact',
      p_value: 0.0081,
      p_adjusted: 0.0243,
      alpha: 0.05,
    };
    await renderWithProviders(<EvidenceTable evidence={makeEvidence({ test: fisher })} />);

    const line = screen.getByText(`Test: ${formatTestLine(fisher)}`);
    expect(line.textContent).toContain('adjusted p');
  });

  it('lists the thresholds applied and the datasets used exactly as stored', async () => {
    const evidence = makeEvidence({
      thresholds: { 'k1.min_clicks_floor': 10 },
      datasets: [
        {
          dataset: 'google.search_terms',
          completeness: 'complete',
          covered_through: '2026-10-01',
        },
      ],
    });
    await renderWithProviders(<EvidenceTable evidence={evidence} />);

    expect(listTexts(THRESHOLDS_LABEL)).toEqual(['k1.min_clicks_floor = 10']);
    expect(listTexts(DATASETS_LABEL)).toEqual([
      'google.search_terms: complete, covered through 2026-10-01 (Google day, America/New_York)',
    ]);
  });

  it('states "No metrics recorded" when the evidence cites no metric', async () => {
    await renderWithProviders(<EvidenceTable evidence={makeEvidence()} />);

    const table = screen.getByRole('table', { name: 'Evidence' });
    expect(within(table).getByText('No metrics recorded')).toBeDefined();
    expect(within(table).queryAllByRole('rowheader')).toEqual([]);
  });
});


// ---------------------------------------------------------------------------------------------
// EvidenceTable rendered again
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable rendered again', () => {
  it('keeps its output for the same evidence and caption, and renders changed evidence', () => {
    const spend = metric('spend', 'Spend', '120.00', 'currency', { currency: 'USD' });
    const evidence = makeEvidence({ metrics: [spend], baselines: [] });
    const raised = { ...spend, value: '180.00' };
    const changed = { ...evidence, metrics: [raised] };
    const expected = formatMetric(raised);
    const formats = vi.spyOn(formatModule, 'formatMetric');
    const { rerender } = render(<EvidenceTable evidence={evidence} />);
    const formatted = formats.mock.calls.length;
    expect(formatted).toBeGreaterThan(0);

    // The same evidence object, as a refetch that left the item unchanged hands back.
    rerender(<EvidenceTable evidence={evidence} />);
    expect(formats.mock.calls).toHaveLength(formatted);

    // Changed evidence arrives as a new object: the table shows the new value.
    rerender(<EvidenceTable evidence={changed} />);
    expect(formats.mock.calls.length).toBeGreaterThan(formatted);
    expect(tableTexts()[1]).toEqual(['Spend', expected, '—']);

    // A new caption renders too.
    rerender(<EvidenceTable evidence={changed} caption="Budget evidence" />);
    expect(tableTexts('Budget evidence')[1]).toEqual(['Spend', expected, '—']);
  });
});

// ---------------------------------------------------------------------------------------------
// "Data used" coverage dates and their day boundaries
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable "Data used" coverage dates', () => {
  /**
   * One dataset entry as the backend freezes it: with the day boundary of its coverage date
   * when `boundary` is given, else in the shape frozen before entries recorded one.
   */
  function dataset(
    name: string,
    coveredThrough: string | null,
    completeness: DatasetCoverage['completeness'] = 'complete',
    boundary?: string | null,
  ): DatasetCoverage {
    const entry: DatasetCoverage = { dataset: name, completeness, covered_through: coveredThrough };
    return boundary === undefined ? entry : { ...entry, boundary };
  }

  /**
   * Renders `datasets` in evidence whose window records `sourceDays` (the fixture's Google
   * `America/New_York` and LinkedIn `UTC` entries by default) and returns the "Data used"
   * entries.
   */
  function dataUsed(
    datasets: DatasetCoverage[],
    sourceDays: SourceDay[] = makeEvidence().window.source_days,
  ): string[] {
    const base = makeEvidence();
    const evidence = makeEvidence({
      datasets,
      window: { ...base.window, source_days: sourceDays },
    });
    render(<EvidenceTable evidence={evidence} />);
    return listTexts(DATASETS_LABEL);
  }

  it("labels a Google date with the Google day boundary the item's window recorded", () => {
    expect(dataUsed([dataset('google.metrics_daily', '2026-10-10')])).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-10 (Google day, America/New_York)',
    ]);
  });

  it('takes the boundary from the evidence window, never from a client constant', () => {
    const berlin: SourceDay = {
      source: 'google',
      start: '2026-09-13',
      end: '2026-10-10',
      boundary: 'Europe/Berlin',
    };
    expect(dataUsed([dataset('google.search_terms', '2026-10-10')], [berlin])).toEqual([
      'google.search_terms: complete, covered through 2026-10-10 (Google day, Europe/Berlin)',
    ]);
  });

  it('labels a LinkedIn date with its UTC day boundary', () => {
    expect(dataUsed([dataset('linkedin.analytics_daily', '2026-10-09', 'incomplete')])).toEqual([
      'linkedin.analytics_daily: incomplete, covered through 2026-10-09 (LinkedIn day, UTC)',
    ]);
  });

  it('labels a HubSpot date with the UTC boundary its entry recorded, as /sources does', () => {
    // HubSpot holds instants, so `source_days` never lists it: the entry carries its boundary.
    expect(
      dataUsed([
        dataset('hubspot.contacts', '2026-10-10', 'complete', 'UTC'),
        dataset('hubspot.deal_search', '2026-10-09', 'incomplete', 'UTC'),
      ]),
    ).toEqual([
      'hubspot.contacts: complete, covered through 2026-10-10 (HubSpot day, UTC)',
      'hubspot.deal_search: incomplete, covered through 2026-10-09 (HubSpot day, UTC)',
    ]);
  });

  it("takes the entry's own boundary over the one the window recorded for its source", () => {
    expect(
      dataUsed([
        dataset('google.metrics_daily', '2026-10-10', 'complete', ' Europe/Berlin '),
        dataset('linkedin.leads', '2026-10-10', 'complete', 'Asia/Tokyo'),
      ]),
    ).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-10 (Google day, Europe/Berlin)',
      'linkedin.leads: complete, covered through 2026-10-10 (LinkedIn day, Asia/Tokyo)',
    ]);
  });

  it("falls back to the window's boundary when the entry records none or a blank one", () => {
    // Evidence frozen before entries recorded a boundary omits the key; the API sends `null`.
    expect(
      dataUsed([
        dataset('google.metrics_daily', '2026-10-10'),
        dataset('google.search_terms', '2026-10-10', 'complete', null),
        dataset('linkedin.leads', '2026-10-10', 'complete', '  '),
      ]),
    ).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-10 (Google day, America/New_York)',
      'google.search_terms: complete, covered through 2026-10-10 (Google day, America/New_York)',
      'linkedin.leads: complete, covered through 2026-10-10 (LinkedIn day, UTC)',
    ]);
  });

  it('names the HubSpot day without a zone on evidence frozen without a boundary', () => {
    // An older entry has no boundary, and the window lists none for HubSpot: no zone is guessed.
    expect(
      dataUsed([
        dataset('hubspot.contacts', '2026-10-10'),
        dataset('hubspot.associations', '2026-10-10', 'complete', null),
      ]),
    ).toEqual([
      'hubspot.contacts: complete, covered through 2026-10-10 (HubSpot day)',
      'hubspot.associations: complete, covered through 2026-10-10 (HubSpot day)',
    ]);
  });

  it("names the source's day without a zone when its window entry has a blank boundary", () => {
    const linkedinOnly = makeEvidence().window.source_days.filter(
      (day) => day.source === 'linkedin',
    );
    const blankGoogle: SourceDay = {
      source: 'google',
      start: '2026-09-13',
      end: '2026-10-10',
      boundary: '  ',
    };
    expect(
      dataUsed(
        [dataset('google.metrics_daily', '2026-10-10'), dataset('linkedin.leads', '2026-10-10')],
        [blankGoogle, ...linkedinOnly],
      ),
    ).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-10 (Google day)',
      'linkedin.leads: complete, covered through 2026-10-10 (LinkedIn day, UTC)',
    ]);
  });

  it('leaves out the coverage clause when no date was recorded', () => {
    const entries = dataUsed([
      dataset('google.entities', null),
      dataset('linkedin.entities', '   ', 'unknown'),
    ]);

    expect(entries).toEqual(['google.entities: complete', 'linkedin.entities: unknown']);
    for (const entry of entries) {
      expect(entry).not.toContain('covered through');
      expect(entry).not.toContain('unavailable');
    }
  });

  it('shows the date as stored, unlabelled, when the dataset names no known source', () => {
    expect(
      dataUsed([
        dataset('meta.ads_daily', '2026-10-10'),
        dataset('constructor.daily', '2026-10-10'),
        dataset('google', '2026-10-10'),
        dataset('.metrics_daily', '2026-10-10'),
      ]),
    ).toEqual([
      'meta.ads_daily: complete, covered through 2026-10-10',
      'constructor.daily: complete, covered through 2026-10-10',
      'google: complete, covered through 2026-10-10',
      '.metrics_daily: complete, covered through 2026-10-10',
    ]);
  });

  it('keeps the stored calendar date, never shifting it into another zone', () => {
    // Parsing "2026-10-10" as UTC midnight and formatting it in Pacific/Honolulu (UTC-10) would
    // read 2026-10-09: the stored date passes through untouched, only trimmed.
    const honolulu: SourceDay = {
      source: 'google',
      start: '2026-09-13',
      end: '2026-10-10',
      boundary: 'Pacific/Honolulu',
    };
    expect(dataUsed([dataset('google.metrics_daily', ' 2026-10-10 ')], [honolulu])).toEqual([
      'google.metrics_daily: complete, covered through 2026-10-10 (Google day, Pacific/Honolulu)',
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Unpaired baselines that record their own unit and currency
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable unpaired baselines in their own unit and currency', () => {
  /** C1's pause card: no metric is a cost per outcome, so its baseline pairs with none. */
  const pauseMetrics: Metric[] = [
    metric('spend', 'Window spend', '1519.463320', 'currency', { currency: 'USD' }),
    metric('spend_days', 'Days with spend', 28, 'days'),
    metric('outcomes', 'Window conversions', 0, 'conversions'),
  ];
  const label = 'Account cost per conversion (same source and objective)';

  /** The C1 baseline as the backend now freezes it: unit `currency` and the account currency. */
  function moneyBaseline(value: Baseline['value'], currency: string | null): Baseline {
    return { key: 'baseline_cost_per_outcome', label, value, unit: 'currency', currency };
  }

  /** The text of the one entry under "Baselines", after checking nothing paired. */
  function unpairedEntry(baselines: Baseline[]): string | null | undefined {
    render(<EvidenceTable evidence={makeEvidence({ metrics: pauseMetrics, baselines })} />);
    expect(screen.queryByRole('list', { name: OTHER_BASELINES_LABEL })).toBeNull();
    const entries = listTexts(BASELINES_LABEL);
    expect(entries).toHaveLength(baselines.length);
    return entries[0];
  }

  it('shows an unpaired money baseline in its account currency, not as a raw decimal', () => {
    expect(unpairedEntry([moneyBaseline('37.986583', 'USD')])).toBe(`${label}: USD\u00a037.99`);
  });

  it('formats it exactly as formatMetric formats a metric of that unit and currency', () => {
    const recorded = moneyBaseline('412.505000', 'EUR');
    expect(unpairedEntry([recorded])).toBe(
      `${label}: ${formatMetric({ value: '412.505000', unit: 'currency', currency: 'EUR' })}`,
    );
    expect(screen.getByText(/412\.5/).textContent).toContain('EUR\u00a0');
  });

  it('keeps a zero-denominator "—" and an unavailable money baseline as they are', () => {
    expect(unpairedEntry([moneyBaseline('—', 'USD'), moneyBaseline(null, 'USD')])).toBe(
      `${label}: —`,
    );
    expect(listTexts(BASELINES_LABEL)[1]).toBe(`${label}: unavailable`);
  });

  it('formats a baseline with a unit and no currency in that unit', () => {
    const share: Baseline = {
      key: 'account_share',
      label: 'Account share',
      value: '0.25',
      unit: 'ratio',
      currency: null,
    };
    const expected = formatMetric({ value: '0.25', unit: 'ratio' });
    expect(unpairedEntry([share])).toBe(`Account share: ${expected}`);
    expect(expected).toBe('25%');
  });

  it('keeps six recorded places for a baseline with no unit, as frozen runs and K1 hold', () => {
    const frozen = baseline('baseline_cost_per_outcome', label, '37.986583');
    const rate: Baseline = {
      key: 'baseline_rate',
      label: 'Account conversions per click',
      value: '0.04946',
      unit: null,
      currency: null,
    };
    const blank: Baseline = {
      key: 'blank_unit',
      label: 'Blank unit',
      value: '0.123456',
      unit: ' ',
      currency: '',
    };
    expect(unpairedEntry([frozen, rate, blank])).toBe(`${label}: 37.986583`);
    expect(listTexts(BASELINES_LABEL).slice(1)).toEqual([
      'Account conversions per click: 0.04946',
      'Blank unit: 0.123456',
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Metric help (AAP 0.4.2 "Help text on metrics": the Tooltip wrapper)
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable metric help', () => {
  const idle = metric('idle_days', 'Known idle days', 3, 'days');
  const spend = metric('spend', 'Window spend', '120.00', 'currency', { currency: 'USD' });

  /** The parts of the tooltip in the row header named `label`. */
  function helpParts(label: string): {
    header: HTMLElement;
    wrapper: HTMLElement;
    trigger: HTMLElement;
    bubble: HTMLElement;
  } {
    const header = screen.getByRole('rowheader', { name: label });
    const wrapper = header.querySelector<HTMLElement>(':scope > .ds-tooltip');
    const trigger = wrapper?.querySelector<HTMLElement>(':scope > .ds-tooltip__trigger');
    const describedBy = trigger?.getAttribute('aria-describedby') ?? '';
    const bubble = describedBy === '' ? null : document.getElementById(describedBy);
    if (wrapper === null || trigger === undefined || trigger === null || bubble === null) {
      throw new Error(`the row header "${label}" has no tooltip`);
    }
    return { header, wrapper, trigger, bubble };
  }

  it('wraps a metric label that has help in a focusable Tooltip trigger described by its help', () => {
    render(<EvidenceTable evidence={makeEvidence({ metrics: [idle, spend] })} />);

    const { header, trigger, bubble } = helpParts('Known idle days');
    expect(trigger.textContent).toBe('Known idle days');
    expect(trigger.tabIndex).toBe(0);
    // aria-describedby resolves to the role="tooltip" bubble holding the metric's help text.
    expect(bubble.getAttribute('role')).toBe('tooltip');
    expect(bubble.textContent).toBe(metricHelp('idle_days'));
    expect(bubble.hidden).toBe(true);
    expect(header.contains(bubble)).toBe(true);
    // The help describes the label; the row header is still named by the label alone.
    expect(screen.getByRole('rowheader', { name: 'Known idle days' })).toBe(header);
    expect(textWithoutTooltips(header)).toBe('Known idle days');
  });

  it('shows the help while the label is hovered or focused, and Escape hides it', () => {
    render(<EvidenceTable evidence={makeEvidence({ metrics: [idle] })} />);
    const { wrapper, trigger, bubble } = helpParts('Known idle days');

    fireEvent.mouseEnter(wrapper);
    expect(bubble.hidden).toBe(false);
    fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(bubble.hidden).toBe(true);
    fireEvent.mouseLeave(wrapper);

    act(() => {
      trigger.focus();
    });
    expect(document.activeElement).toBe(trigger);
    expect(bubble.hidden).toBe(false);
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(bubble.hidden).toBe(true);
    // Escape hides the help without moving focus.
    expect(document.activeElement).toBe(trigger);
  });

  it('renders a metric without help as its plain label, with no tooltip or extra focus stop', () => {
    render(<EvidenceTable evidence={makeEvidence({ metrics: [idle, spend] })} />);

    const header = screen.getByRole('rowheader', { name: 'Window spend' });
    expect(metricHelp('spend')).toBeUndefined();
    expect(header.textContent).toBe('Window spend');
    expect(header.querySelector('.ds-tooltip')).toBeNull();
    expect(header.querySelector('[tabindex]')).toBeNull();
    expect(within(header).queryByRole('tooltip', { hidden: true })).toBeNull();
  });

  it('keys the help on the metric key, so a blank-labelled metric shows its key with the help', () => {
    const unknown = metric('not_a_metric', 'Custom count', 2, 'days');
    const blank = metric('idle_days', '  ', 4, 'days');
    render(<EvidenceTable evidence={makeEvidence({ metrics: [unknown, blank] })} />);

    expect(screen.getByRole('rowheader', { name: 'Custom count' }).textContent).toBe(
      'Custom count',
    );
    const { trigger, bubble } = helpParts('idle_days');
    expect(trigger.textContent).toBe('idle_days');
    expect(bubble.textContent).toBe(metricHelp('idle_days'));
  });

  it('gives every metric with help its own tooltip, so each label is described by its own help', () => {
    const known = metric('known_days', 'Known-budget days', 9, 'days');
    render(<EvidenceTable evidence={makeEvidence({ metrics: [known, idle, spend] })} />);

    const bubbles = screen.getAllByRole('tooltip', { hidden: true });
    expect(bubbles.map((bubble) => bubble.textContent)).toEqual([
      metricHelp('known_days'),
      metricHelp('idle_days'),
    ]);
    expect(new Set(bubbles.map((bubble) => bubble.id)).size).toBe(2);
    expect(helpParts('Known-budget days').bubble).toBe(bubbles[0]);
    expect(helpParts('Known idle days').bubble).toBe(bubbles[1]);
  });
});

// ---------------------------------------------------------------------------------------------
// EvidenceTable ratio precision: one fixed precision for every percentage of the table
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable ratio precision', () => {
  /** A CR1 pair's evidence as `creative._pair_candidate` writes it. */
  const pairEvidence = makeEvidence({
    metrics: [
      metric('rate_a', 'Variant A lead rate', '0.000722', 'ratio'),
      metric('rate_b', 'Variant B lead rate', '0.000698', 'ratio'),
      metric('leads_a', 'Variant A leads', 13, 'leads'),
      metric('impressions_a', 'Variant A impressions', 18000, 'impressions'),
    ],
  });

  /** A CR3 item's stage evidence (`creative._test_candidate`) with the QA run's figures. */
  const stageEvidence = makeEvidence({
    metrics: [
      metric('click_rate', 'Click rate', '0.007501', 'ratio'),
      metric('open_rate', 'Open rate', '0.004107', 'ratio'),
      metric('open_rate_ci_low', 'Open rate interval low', '0.003937', 'ratio'),
      metric('open_rate_ci_high', 'Open rate interval high', '0.004278', 'ratio'),
      metric('completion_rate', 'Completion rate', '0.111748', 'ratio'),
      metric('sample_per_variant', 'Impressions per variant for the next test', 52000, 'impressions'),
    ],
    baselines: [
      baseline('click_rate_baseline', 'Account click rate baseline', '0.008120'),
      baseline('open_rate_baseline', 'Account open rate baseline', '0.005547'),
      baseline('completion_rate_baseline', 'Account completion rate baseline', '0.119287'),
    ],
  });

  /** The Value column's text, top to bottom. */
  function valueColumn(): string[] {
    return tableTexts()
      .slice(1)
      .map((row) => row[1] ?? '');
  }

  it('keeps CR1 lead rates of 0.000722 and 0.000698 apart instead of "0.1%" twice', () => {
    render(<EvidenceTable evidence={pairEvidence} />);

    expect(valueColumn()).toEqual(['0.072%', '0.070%', '13', '18,000']);
    expect(evidenceRatioDigits(pairEvidence)).toBe(3);
  });

  it('keeps a stage rate apart from its baseline and its interval bounds apart', () => {
    render(<EvidenceTable evidence={stageEvidence} />);

    // Every percentage of the table, value or baseline, at the same two places.
    expect(valueColumn()).toEqual(['0.75%', '0.41%', '0.39%', '0.43%', '11.17%', '52,000']);
    expect(metricCells('Click rate').baseline.textContent).toBe(
      'Account click rate baseline: 0.81%',
    );
    expect(metricCells('Open rate').baseline.textContent).toBe(
      'Account open rate baseline: 0.55%',
    );
    expect(metricCells('Completion rate').baseline.textContent).toBe(
      'Account completion rate baseline: 11.93%',
    );
  });

  it('pads every percentage of the table to one precision, leaving other units alone', () => {
    const evidence = makeEvidence({
      metrics: [
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.99', 'ratio'),
        metric('spending_day_share', 'Share of known-budget days with spend', '1', 'ratio'),
        metric('capped_days_share', 'Share of known-budget days that are capped', '0.5', 'ratio'),
        metric('spend', 'Window spend', '5148.00', 'currency', { currency: 'USD' }),
        metric('known_days', 'Known-budget days', 26, 'days'),
        metric('utilization_text', 'Lifetime utilization', 'n/a', 'ratio'),
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(valueColumn()).toEqual([
      '99.0%',
      '100.0%',
      '50.0%',
      'USD\u00a05,148.00',
      '26 days',
      'n/a',
    ]);
  });

  it('counts paired baselines toward the precision, but not unpaired ones', () => {
    const evidence = makeEvidence({
      metrics: [metric('share', 'Share', '0.5', 'ratio')],
      baselines: [
        baseline('share_baseline', 'Account share', '0.5004'),
        baseline('baseline_rate', 'Account conversions per click', '0.0000001'),
      ],
    });
    render(<EvidenceTable evidence={evidence} />);

    // 50% and 50.04% both read "50.0%" at one place, so the table takes two.
    expect(evidenceRatioDigits(evidence)).toBe(2);
    expect(metricCells('Share').value.textContent).toBe('50.00%');
    expect(metricCells('Share').baseline.textContent).toBe('Account share: 50.04%');
    expect(evidenceRatioDigits({ ...evidence, baselines: [] })).toBe(1);
  });

  it('formats each value as formatMetric does at the evidence digits', () => {
    render(<EvidenceTable evidence={stageEvidence} />);

    const ratioDigits = evidenceRatioDigits(stageEvidence);
    expect(valueColumn()).toEqual(
      stageEvidence.metrics.map((entry) => formatMetric(entry, { ratioDigits })),
    );
  });
});

// ---------------------------------------------------------------------------------------------
// EvidenceTable rate precision: B4's rates per unit of spend, per 100 of the account currency
// ---------------------------------------------------------------------------------------------

describe('EvidenceTable rates per unit of spend', () => {
  /** A rate metric as `budget._b4_candidate` records it: unit `rate`, no currency of its own. */
  function rate(
    key: string,
    label: string,
    value: Metric['value'],
    extra: Partial<Metric> = {},
  ): Metric {
    return metric(key, label, value, 'rate', extra);
  }

  /** The money and rate metrics of the default world's "Benchmarks - Lead gen (NA)" B4 card. */
  const b4Metrics: Metric[] = [
    metric('weekend_spend', 'Weekend spend', '732.500000', 'currency', { currency: 'USD' }),
    metric('weekday_spend', 'Weekday spend', '1856.990000', 'currency', { currency: 'USD' }),
    metric('weekend_outcomes', 'Weekend leads', 10, 'leads'),
    metric('weekday_outcomes', 'Weekday leads', 170, 'leads'),
    rate('weekend_rate', 'Weekend leads per unit of spend', '0.013652'),
    rate('weekday_rate', 'Weekday leads per unit of spend', '0.091546'),
  ];

  /** The Value cell of the row whose header is `label`. */
  function valueOf(label: string): string | null {
    return metricCells(label).value.textContent;
  }

  it('states B4 rates per USD 100 at three significant digits, never six raw places', () => {
    render(<EvidenceTable evidence={makeEvidence({ metrics: b4Metrics })} />);

    expect(valueOf('Weekend leads per unit of spend')).toBe('1.37 per USD\u00a0100');
    expect(valueOf('Weekday leads per unit of spend')).toBe('9.15 per USD\u00a0100');
    expect(valueOf('Weekend spend')).toBe('USD\u00a0732.50');
    expect(screen.queryByText(/0\.013652|0\.091546|%/u)).toBeNull();
  });

  it('gives every rate of the table one precision, wide enough to tell a close pair apart', () => {
    const metrics = [
      metric('weekend_spend', 'Weekend spend', '732.500000', 'currency', { currency: 'EUR' }),
      rate('weekend_rate', 'Weekend leads per unit of spend', '0.013652'),
      rate('weekday_rate', 'Weekday leads per unit of spend', '0.013656'),
      rate('other_rate', 'Other leads per unit of spend', '0.091546'),
    ];
    render(<EvidenceTable evidence={makeEvidence({ metrics })} />);

    expect(valueOf('Weekend leads per unit of spend')).toBe('1.365 per EUR\u00a0100');
    expect(valueOf('Weekday leads per unit of spend')).toBe('1.366 per EUR\u00a0100');
    expect(valueOf('Other leads per unit of spend')).toBe('9.155 per EUR\u00a0100');
  });

  it("takes the currency from the evidence's extra when no metric carries one", () => {
    const metrics = b4Metrics.filter((entry) => entry.unit === 'rate');
    render(<EvidenceTable evidence={makeEvidence({ metrics, extra: { currency: 'GBP' } })} />);

    expect(valueOf('Weekend leads per unit of spend')).toBe('1.37 per GBP\u00a0100');
    expect(valueOf('Weekday leads per unit of spend')).toBe('9.15 per GBP\u00a0100');
  });

  it('shows the rates unscaled and unlabelled, still at one precision, with no currency known', () => {
    const metrics = b4Metrics.filter((entry) => entry.unit === 'rate');
    render(<EvidenceTable evidence={makeEvidence({ metrics })} />);

    expect(valueOf('Weekend leads per unit of spend')).toBe('0.0137');
    expect(valueOf('Weekday leads per unit of spend')).toBe('0.0915');
  });

  it('renders a zero rate as "0 per USD 100" and an unavailable one as "unavailable"', () => {
    const metrics = [
      metric('weekend_spend', 'Weekend spend', '0.000000', 'currency', { currency: 'USD' }),
      rate('weekend_rate', 'Weekend leads per unit of spend', '0.000000'),
      rate('weekday_rate', 'Weekday leads per unit of spend', null, { unavailable: true }),
      rate('other_rate', 'Other leads per unit of spend', '0.091546'),
    ];
    render(<EvidenceTable evidence={makeEvidence({ metrics })} />);

    expect(valueOf('Weekend leads per unit of spend')).toBe('0 per USD\u00a0100');
    expect(valueOf('Weekday leads per unit of spend')).toBe(formatModule.UNAVAILABLE);
    expect(valueOf('Other leads per unit of spend')).toBe('9.15 per USD\u00a0100');
  });

  it('end-aligns every rate figure, unavailable included, as it does every other figure', () => {
    const metrics = [
      ...b4Metrics,
      rate('missing_rate', 'Missing leads per unit of spend', null, { unavailable: true }),
    ];
    render(<EvidenceTable evidence={makeEvidence({ metrics })} />);

    for (const label of [
      'Weekend leads per unit of spend',
      'Weekday leads per unit of spend',
      'Missing leads per unit of spend',
    ]) {
      expect(metricCells(label).value.classList.contains('ds-datatable__cell--end')).toBe(true);
    }
  });

  it('formats each rate as formatMetric does with the evidence currency and shared digits', () => {
    render(<EvidenceTable evidence={makeEvidence({ metrics: b4Metrics })} />);

    const options = {
      rateCurrency: 'USD',
      rateDigits: formatModule.rateMetricDigits(b4Metrics, 'USD'),
    };
    for (const entry of b4Metrics.filter((each) => each.unit === 'rate')) {
      expect(valueOf(entry.label)).toBe(formatMetric(entry, options));
    }
  });
});
