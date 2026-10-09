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
 *   key order and "Data used" with each dataset's completeness and coverage. The window,
 *   provenance and `extra` belong to other parts of the card and are not rendered here.
 *
 * Evidence comes from `../../test/fixtures`, either as the backend-shaped page items (C1 pause,
 * B1 increase) or built in the test for a single edge case. The component makes no request, so
 * it renders without providers.
 */
import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Baseline, Item, Metric } from '../../api/types';
import {
  FIXTURE_ITEM_IDS,
  FIXTURE_WINDOW,
  findPageItem,
  makeEvidence,
  makeRecommendationsPage,
} from '../../test/fixtures';
import {
  BASELINES_LABEL,
  DATASETS_LABEL,
  DEFAULT_EVIDENCE_CAPTION,
  EvidenceTable,
  NO_METRICS_MESSAGE,
  OTHER_BASELINES_LABEL,
  THRESHOLDS_LABEL,
  formatThresholdValue,
  pairBaselines,
} from './EvidenceTable';

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
 * (`<th>`) and cells (`<td>`) in document order.
 */
function tableTexts(caption: string = DEFAULT_EVIDENCE_CAPTION): string[][] {
  const table = screen.getByRole('table', { name: caption });
  return within(table)
    .getAllByRole('row')
    .map((row) => Array.from(row.querySelectorAll('th, td'), (cell) => cell.textContent));
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
    expect(rows).toContainEqual(['Window spend', '$5,148.00', '—']);
    expect(rows).toContainEqual(['Window days', '28 days', '—']);
    expect(rows).toContainEqual(['Window conversions', '62.5', '—']);
    expect(rows).toContainEqual(['Window utilization (spend vs cap)', '99%', '—']);
    expect(rows).toContainEqual([
      'Cost per conversion',
      '$82.37',
      'Account cost per conversion (same source and objective): $95.00' +
        'Highest cost per outcome allowed to scale: $95.00',
    ]);

    const costRow = screen.getByRole('rowheader', { name: 'Cost per conversion' }).closest('tr');
    expect(costRow).not.toBeNull();
    if (costRow !== null) {
      expect(within(costRow).getAllByText('$95.00')).toHaveLength(2);
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

    expect(tableTexts()[1]).toEqual(['Click rate', '1.2%', 'Campaign click rate: 2%']);
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
      'google.metrics_daily: complete, covered through 2026-10-05',
      'google.search_terms: incomplete, covered through 2026-10-03',
      'hubspot.contacts: unknown, covered through unavailable',
      'hubspot.meetings: unknown, covered through unavailable',
    ]);
  });

  it('shows the test line with its adjusted p-value and alpha when a test was used', () => {
    const evidence = makeEvidence({
      test: { name: 'fisher_exact', p_value: 0.012, p_adjusted: 0.036, alpha: 0.05 },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(
      screen.getByText("Test: Fisher's exact test · p = 0.012 · adjusted p = 0.036 · α 0.05"),
    ).toBeDefined();
  });

  it('names a test recorded with a blank name, so the line never starts with a separator', () => {
    const evidence = makeEvidence({
      test: { name: '  ', p_value: '0.0004', p_adjusted: null, alpha: null },
    });
    render(<EvidenceTable evidence={evidence} />);

    expect(screen.getByText('Test: Statistical test · p < 0.001')).toBeDefined();
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
      ['Cost per Qualified Lead', '≤ €40.00', '—'],
      ['segment_cpl', '€8.00', 'max_segment_cpl: unavailable'],
    ]);
    expect(screen.queryByText(/\bnull\b|\bundefined\b/)).toBeNull();
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
      ['Spend (Google)', '$10.00', 'Account spend: $30.00Account spend, again: $40.00'],
      ['Spend (LinkedIn)', '€20.00', '—'],
    ]);
    expect(listTexts(OTHER_BASELINES_LABEL)).toEqual(['Rate A: 0.1', 'Rate B: 0.2']);
    expect(listTexts(DATASETS_LABEL)).toHaveLength(2);
    expect(consoleError).not.toHaveBeenCalled();
  });

  it('leaves the window, provenance and extra to the rest of the card', () => {
    const evidence = makeEvidence({
      metrics: [metric('spend', 'Spend', '1', 'currency', { currency: 'USD' })],
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
