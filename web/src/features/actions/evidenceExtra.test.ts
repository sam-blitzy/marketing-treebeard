// @vitest-environment node
/**
 * Unit tests for src/features/actions/evidenceExtra.ts, the guarded readers of recommendation
 * evidence (AAP 0.5.3 `Evidence.extra`, AAP 0.5.5 field sets per section).
 *
 * Every exported reader is held to its TSDoc twice: first on the well-formed evidence its rule
 * freezes, taken from the default fixture page wherever that page carries the rule, then on
 * malformed and missing fields, which must read as `null`, `[]` or an empty field and never
 * throw. The assertions pin the module's contract: amounts stay the exact decimal strings the
 * backend sent (trailing zeros included, so nothing went through `Number()`), an absent count
 * is `null` while a known zero stays `0`, MQL and QL are separate keys, and the white-paper
 * cells carry the `floor` and `unavailable` markers that render "≥ N", "≤ X", "unavailable" and
 * "—". The module is pure, so the suite runs in Node.
 */
import { describe, expect, it } from 'vitest';

import type { Evidence, Item, Metric } from '../../api/types';
import { EM_DASH, UNAVAILABLE } from '../../lib/format';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  FIXTURE_RESOLUTIONS,
  findPageItem,
  makeCompletionRateTestItem,
  makeEvidence,
  makeItem,
  makeRecommendationsPage,
  pageItems,
} from '../../test/fixtures';
import type { EvidenceHolder } from './evidenceExtra';
import {
  ADJACENT_TERMS_MAX,
  PAUSE_RULE_IDS,
  PAUSE_SCALE_METRIC_KEYS,
  PIPELINE_METRIC_KEYS,
  UNKNOWN_CREATIVE_KEY,
  WHITE_PAPER_METRIC_KEYS,
  adjacentTerms,
  allocations,
  baselineByKey,
  blockingDiscrepancies,
  cohortSummary,
  creativeRows,
  dayCounts,
  decisionContextFor,
  dependsOn,
  discrepancyRef,
  evidenceCurrency,
  freedAmount,
  funding,
  headlineMetric,
  isRecord,
  keywordFields,
  launchGap,
  metricByKey,
  metricsByKeys,
  pipelineFields,
  proposedDailyBudget,
  readExtra,
  reviewHalves,
  scaleOutcome,
  sharedMembers,
  unallocatedAmount,
  variants,
  whitePaperAlreadyBefore,
  whitePaperCells,
  whitePaperCostNotes,
  whitePaperMqlLabel,
  whitePaperMqlMetricLabel,
  whitePaperOrphanDealLabel,
} from './evidenceExtra';

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

/** One item of a freshly built default page, so no test shares a payload with another. */
function pageItem(id: Item['id']): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** A well-formed evidence metric whose label is its key unless overridden. */
function metric(key: string, value: Metric['value'], overrides: Partial<Metric> = {}): Metric {
  return {
    key,
    label: key,
    value,
    unit: null,
    currency: null,
    floor: null,
    unavailable: null,
    ...overrides,
  };
}

/** Evidence carrying `extra` and, optionally, well-formed metrics. */
function withExtra(extra: Record<string, unknown> | null, metrics: Metric[] = []): Evidence {
  return makeEvidence({ extra, metrics });
}

/**
 * Evidence whose fields hold wire JSON the contract types refuse, such as a metric without a
 * key or `metrics` that is not an array. The readers guard at run time rather than trusting the
 * types, so the tests send what a drifted backend could.
 */
function rawEvidence(fields: Record<string, unknown>): Evidence {
  return { ...makeEvidence(), ...fields } as unknown as Evidence;
}

/** A white-paper evidence block whose only content is `extra.rows`, plus optional fields. */
function rowsEvidence(
  rows: readonly unknown[],
  extra: Record<string, unknown> = {},
  metrics: Metric[] = [],
): Evidence {
  return withExtra({ ...extra, rows }, metrics);
}

// ---------------------------------------------------------------------------------------------
// Primitives and sources
// ---------------------------------------------------------------------------------------------

describe('isRecord', () => {
  it('accepts plain JSON objects, empty or without a prototype', () => {
    expect(isRecord({})).toBe(true);
    expect(isRecord({ key: 'spend' })).toBe(true);
    expect(isRecord(Object.create(null))).toBe(true);
  });

  it('refuses null, undefined, arrays and every primitive', () => {
    const values: readonly unknown[] = [null, undefined, [], ['rows'], 'rows', 0, 1.5, true];
    for (const value of values) {
      expect(isRecord(value)).toBe(false);
    }
  });

  it('narrows unknown to a record whose keys read as unknown', () => {
    const value: unknown = { kind: 'status' };
    expect(isRecord(value) ? value.kind : null).toBe('status');
  });
});

describe('readExtra', () => {
  it("returns an item's extra object, and the same object from its bare evidence", () => {
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    expect(readExtra(c1)).toEqual({ proposed_status: 'paused' });
    expect(readExtra(c1)).toBe(c1.evidence.extra);
    expect(readExtra(c1.evidence)).toBe(c1.evidence.extra);
  });

  it('reads null, undefined and a missing group item as absent', () => {
    const items: readonly Item[] = [];
    expect(readExtra(null)).toBeNull();
    expect(readExtra(undefined)).toBeNull();
    expect(readExtra(items[0])).toBeNull();
  });

  it('returns null when extra is null or not a plain object', () => {
    expect(readExtra(makeEvidence())).toBeNull();
    expect(readExtra(rawEvidence({ extra: ['rows'] }))).toBeNull();
    expect(readExtra(rawEvidence({ extra: 'rows' }))).toBeNull();
    expect(readExtra(rawEvidence({ extra: undefined }))).toBeNull();
  });

  it('reads a holder whose evidence is not an object as carrying nothing', () => {
    const holder = { evidence: null } as unknown as EvidenceHolder;
    expect(readExtra(holder)).toBeNull();
    expect(metricByKey(holder, 'spend')).toBeNull();
    expect(baselineByKey(holder, 'baseline_cost_per_outcome')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Metrics and baselines
// ---------------------------------------------------------------------------------------------

describe('metricByKey', () => {
  it('returns a guarded copy of the metric, not the payload object', () => {
    const k1 = pageItem(FIXTURE_ITEM_IDS.k1);
    const spend = metricByKey(k1, 'spend');
    expect(spend).toEqual({
      key: 'spend',
      label: 'Spend',
      value: '186.40',
      unit: 'currency',
      currency: 'USD',
      floor: null,
      unavailable: null,
    });
    expect(spend).not.toBe(k1.evidence.metrics.find((entry) => entry.key === 'spend'));
    expect(metricByKey(k1.evidence, 'spend')).toEqual(spend);
  });

  it('returns the first metric when a key repeats', () => {
    const evidence = makeEvidence({
      metrics: [metric('spend', '10.00'), metric('spend', '20.00')],
    });
    expect(metricByKey(evidence, 'spend')?.value).toBe('10.00');
  });

  it('returns null for a missing key, an absent source and evidence without metrics', () => {
    expect(metricByKey(pageItem(FIXTURE_ITEM_IDS.k1), 'ql')).toBeNull();
    expect(metricByKey(null, 'spend')).toBeNull();
    expect(metricByKey(undefined, 'spend')).toBeNull();
    expect(metricByKey(makeEvidence(), 'spend')).toBeNull();
    expect(metricByKey(rawEvidence({ metrics: { spend: '1.00' } }), 'spend')).toBeNull();
    expect(metricByKey(rawEvidence({ metrics: null }), 'spend')).toBeNull();
  });

  it('skips entries that are not objects or have no non-blank key, and defaults the label', () => {
    const evidence = rawEvidence({
      metrics: [
        null,
        'spend',
        ['spend'],
        { label: 'No key', value: '1.00' },
        { key: '   ', value: '2.00' },
        { key: 7, value: '3.00' },
        { key: 'spend', value: '12.50' },
      ],
    });
    expect(metricByKey(evidence, 'spend')).toEqual({
      key: 'spend',
      label: 'spend',
      value: '12.50',
      unit: null,
      currency: null,
      floor: null,
      unavailable: null,
    });
  });

  it('turns field values of the wrong type into null rather than passing them on', () => {
    const evidence = rawEvidence({
      metrics: [
        {
          key: 'clicks',
          label: 7,
          value: Number.POSITIVE_INFINITY,
          unit: '',
          currency: 840,
          floor: 'yes',
          unavailable: 1,
        },
        { key: 'nan', value: Number.NaN },
        { key: 'nested', value: { amount: '1.00' } },
        { key: 'flag', value: true },
        { key: 'count', value: 3, floor: true, unavailable: false },
      ],
    });
    expect(metricByKey(evidence, 'clicks')).toEqual({
      key: 'clicks',
      label: 'clicks',
      value: null,
      unit: null,
      currency: null,
      floor: null,
      unavailable: null,
    });
    expect(metricByKey(evidence, 'nan')?.value).toBeNull();
    expect(metricByKey(evidence, 'nested')?.value).toBeNull();
    expect(metricByKey(evidence, 'flag')?.value).toBe(true);
    expect(metricByKey(evidence, 'count')).toMatchObject({
      value: 3,
      floor: true,
      unavailable: false,
    });
  });
});

describe('metricsByKeys', () => {
  it('returns one entry per key, in key order, with null where the evidence lacks one', () => {
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const pause = metricsByKeys(c1, PAUSE_SCALE_METRIC_KEYS);
    expect(pause.map((entry) => entry?.key ?? null)).toEqual([
      'spend',
      'outcomes',
      'expected_outcomes',
      null,
    ]);
    expect(pause.map((entry) => entry?.value ?? null)).toEqual(['314.50', 0, '3.4', null]);

    const reversed = metricsByKeys(c1, ['expected_outcomes', 'spend']);
    expect(reversed.map((entry) => entry?.key)).toEqual(['expected_outcomes', 'spend']);
  });

  it('keeps the first metric of a repeated key', () => {
    const evidence = makeEvidence({
      metrics: [metric('ql', 2), metric('mql', 5), metric('ql', 9)],
    });
    expect(metricsByKeys(evidence, ['ql', 'mql']).map((entry) => entry?.value)).toEqual([2, 5]);
  });

  it('returns all nulls for an absent source, and nothing for no keys', () => {
    expect(metricsByKeys(null, ['spend', 'ql'])).toEqual([null, null]);
    expect(metricsByKeys(pageItem(FIXTURE_ITEM_IDS.c1), [])).toEqual([]);
  });
});

describe('headlineMetric', () => {
  /** The page's K1 item with `extra.headline_metric` added to its frozen evidence. */
  function k1WithHeadline(headline: unknown): Item {
    const k1 = pageItem(FIXTURE_ITEM_IDS.k1);
    return {
      ...k1,
      evidence: { ...k1.evidence, extra: { ...k1.evidence.extra, headline_metric: headline } },
    };
  }

  it("returns a guarded copy of the metric extra.headline_metric names: K1's wasted spend", () => {
    const k1 = k1WithHeadline('wasted_spend');
    const headline = headlineMetric(k1);
    expect(headline).toEqual({
      key: 'wasted_spend',
      label: 'Wasted spend',
      value: '186.40',
      unit: 'currency',
      currency: 'USD',
      floor: null,
      unavailable: null,
    });
    expect(headline).not.toBe(k1.evidence.metrics.find((entry) => entry.key === 'wasted_spend'));
    expect(headlineMetric(k1.evidence)).toEqual(headline);
  });

  it('keeps the unavailable and floor markers the evidence froze', () => {
    const unavailableQl = metric('ql', null, { unavailable: true });
    expect(headlineMetric(withExtra({ headline_metric: 'ql' }, [unavailableQl]))).toMatchObject({
      key: 'ql',
      value: null,
      unavailable: true,
    });
    expect(
      headlineMetric(
        withExtra({ headline_metric: 'cost_per_ql' }, [
          metric('cost_per_ql', '40.00', { currency: 'USD', floor: true }),
        ]),
      ),
    ).toMatchObject({ value: '40.00', currency: 'USD', floor: true });
  });

  it('returns the first metric when the named key repeats', () => {
    const evidence = withExtra({ headline_metric: 'spend' }, [
      metric('spend', '10.00'),
      metric('spend', '20.00'),
    ]);
    expect(headlineMetric(evidence)?.value).toBe('10.00');
  });

  it('returns null for evidence frozen without a headline, and for an absent source', () => {
    const items: readonly Item[] = [];
    expect(headlineMetric(pageItem(FIXTURE_ITEM_IDS.k1))).toBeNull();
    expect(headlineMetric(makeEvidence())).toBeNull();
    expect(headlineMetric(rawEvidence({ extra: ['headline_metric'] }))).toBeNull();
    expect(headlineMetric(null)).toBeNull();
    expect(headlineMetric(undefined)).toBeNull();
    expect(headlineMetric(items[0])).toBeNull();
  });

  it('returns null when extra.headline_metric is not a non-blank string', () => {
    const values: readonly unknown[] = ['', '   ', 7, true, null, ['wasted_spend'], {}];
    for (const value of values) {
      expect(headlineMetric(k1WithHeadline(value))).toBeNull();
    }
  });

  it('returns null, never another metric, when the key names no well-formed metric', () => {
    expect(headlineMetric(k1WithHeadline('cost_per_ql'))).toBeNull();
    expect(
      headlineMetric(withExtra({ headline_metric: 'wasted_spend' }, [metric('spend', '10.00')])),
    ).toBeNull();
    expect(
      headlineMetric(
        rawEvidence({
          extra: { headline_metric: 'wasted_spend' },
          metrics: [{ key: '  ', value: '1.00' }, { label: 'Wasted spend', value: '2.00' }],
        }),
      ),
    ).toBeNull();
    expect(
      headlineMetric(
        rawEvidence({ extra: { headline_metric: 'wasted_spend' }, metrics: { wasted_spend: '1' } }),
      ),
    ).toBeNull();
  });
});

describe('baselineByKey', () => {
  it('returns a copy of the first baseline with the key', () => {
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    expect(baselineByKey(c1, 'baseline_cost_per_outcome')).toEqual({
      key: 'baseline_cost_per_outcome',
      label: 'Account cost per conversion (same source and objective)',
      value: '92.50',
    });
    const wEur = pageItem(FIXTURE_ITEM_IDS.wEur);
    expect(baselineByKey(wEur.evidence, 'cohort_median_cost_per_ql')?.value).toBe('611.67');
  });

  it('returns null for a missing key or an absent source', () => {
    expect(baselineByKey(pageItem(FIXTURE_ITEM_IDS.c1), 'cohort_median_cost_per_ql')).toBeNull();
    expect(baselineByKey(null, 'baseline_cost_per_outcome')).toBeNull();
    expect(baselineByKey(makeEvidence(), 'baseline_cost_per_outcome')).toBeNull();
  });

  it('skips malformed entries, defaults the label and nulls a non-scalar value', () => {
    const evidence = rawEvidence({
      baselines: [
        null,
        'baseline',
        { key: 5, value: '1.00' },
        { key: 'baseline', label: '', value: { amount: '2.00' } },
        { key: 'baseline', label: 'Second', value: '3.00' },
      ],
    });
    expect(baselineByKey(evidence, 'baseline')).toEqual({
      key: 'baseline',
      label: 'baseline',
      value: null,
    });
    expect(baselineByKey(rawEvidence({ baselines: 'baseline' }), 'baseline')).toBeNull();
  });
});

describe('evidenceCurrency', () => {
  it('names the currency of the first metric that carries one', () => {
    expect(evidenceCurrency(pageItem(FIXTURE_ITEM_IDS.k1))).toBe('USD');
    expect(evidenceCurrency(pageItem(FIXTURE_ITEM_IDS.wEur))).toBe('EUR');
    const evidence = makeEvidence({
      metrics: [metric('clicks', 5), metric('spend', '1.00', { currency: 'EUR' })],
      extra: { currency: 'USD' },
    });
    expect(evidenceCurrency(evidence)).toBe('EUR');
  });

  it('falls back to extra.currency (B1–B4, CR3), then extra.asset.currency (W1/W2)', () => {
    expect(evidenceCurrency(withExtra({ currency: 'USD', asset: { currency: 'EUR' } }))).toBe(
      'USD',
    );
    expect(evidenceCurrency(withExtra({ asset: { currency: 'EUR' } }))).toBe('EUR');
  });

  it('returns null when the evidence names no currency', () => {
    expect(evidenceCurrency(null)).toBeNull();
    expect(evidenceCurrency(makeEvidence())).toBeNull();
    expect(evidenceCurrency(pageItem(FIXTURE_ITEM_IDS.c0))).toBeNull();
    expect(evidenceCurrency(withExtra({ currency: '  ', asset: 'EUR' }))).toBeNull();
    expect(evidenceCurrency(withExtra({ asset: { currency: 978 } }))).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// C0 discrepancies and P1 dependencies
// ---------------------------------------------------------------------------------------------

describe('discrepancyRef', () => {
  it("reads the C0 card's discrepancy, its render_value texts and its resolutions", () => {
    expect(discrepancyRef(pageItem(FIXTURE_ITEM_IDS.c0))).toEqual({
      discrepancyId: String(FIXTURE_DISCREPANCY_ID),
      kind: 'status',
      shape: 'own_budget',
      state: 'open',
      decided: 'active',
      platform: 'paused',
      resolutions: [...FIXTURE_RESOLUTIONS.status],
      detectedAt: '2026-10-06T03:00:41Z',
    });
  });

  it('accepts an integer or string id and refuses any other value', () => {
    expect(discrepancyRef(withExtra({ discrepancy_id: 51 }))?.discrepancyId).toBe('51');
    expect(discrepancyRef(withExtra({ discrepancy_id: 'd-51' }))?.discrepancyId).toBe('d-51');
    expect(discrepancyRef(withExtra({ discrepancy_id: 51.5 }))).toBeNull();
    expect(discrepancyRef(withExtra({ discrepancy_id: true }))).toBeNull();
    expect(discrepancyRef(withExtra({ discrepancy_id: '  ' }))).toBeNull();
    expect(discrepancyRef(withExtra({ kind: 'status' }))).toBeNull();
    expect(discrepancyRef(makeEvidence())).toBeNull();
    expect(discrepancyRef(pageItem(FIXTURE_ITEM_IDS.c1))).toBeNull();
    expect(discrepancyRef(null)).toBeNull();
  });

  it('defaults missing fields to empty text, "unavailable" texts and no resolutions', () => {
    expect(discrepancyRef(withExtra({ discrepancy_id: 7 }))).toEqual({
      discrepancyId: '7',
      kind: '',
      shape: '',
      state: '',
      decided: UNAVAILABLE,
      platform: UNAVAILABLE,
      resolutions: [],
      detectedAt: null,
    });
  });

  it('prefers the metric texts, and reads extra texts only when they are strings', () => {
    const fromMetrics = withExtra(
      { discrepancy_id: 7, decided: 'ignored', platform: 'not returned' },
      [
        metric('decided', 'USD 50.00'),
        metric('platform', 'paused', { unavailable: true }),
      ],
    );
    expect(discrepancyRef(fromMetrics)).toMatchObject({
      decided: 'USD 50.00',
      platform: 'not returned',
    });

    const fromExtra = withExtra({ discrepancy_id: 7, decided: 'no decision', platform: 'ended' });
    expect(discrepancyRef(fromExtra)).toMatchObject({
      decided: 'no decision',
      platform: 'ended',
    });

    const rawStates = withExtra({
      discrepancy_id: 7,
      decided: { status: 'active' },
      platform: { status: 'paused' },
    });
    expect(discrepancyRef(rawStates)).toMatchObject({
      decided: UNAVAILABLE,
      platform: UNAVAILABLE,
    });
  });

  it('renders a numeric metric as text and treats boolean or blank metric text as missing', () => {
    const evidence = withExtra({ discrepancy_id: 7, platform: 'shared budget 123' }, [
      metric('decided', 50),
      metric('platform', true),
    ]);
    expect(discrepancyRef(evidence)).toMatchObject({
      decided: '50',
      platform: 'shared budget 123',
    });
    const blank = withExtra({ discrepancy_id: 7 }, [metric('decided', '   ')]);
    expect(discrepancyRef(blank)?.decided).toBe(UNAVAILABLE);
  });

  it('keeps only non-blank string resolutions, in stored order', () => {
    const evidence = withExtra({
      discrepancy_id: 7,
      kind: 'untracked',
      resolutions: ['adopt_platform', 3, '', null, { key: 'x' }, 'record_intended'],
    });
    expect(discrepancyRef(evidence)?.resolutions).toEqual([...FIXTURE_RESOLUTIONS.untracked]);
    const notArray = withExtra({ discrepancy_id: 7, resolutions: 'adopt_platform' });
    expect(discrepancyRef(notArray)?.resolutions).toEqual([]);
  });
});

describe('dependsOn', () => {
  it('renders each {type, id} subject as "{type}:{id}" and passes string entries through', () => {
    const evidence = withExtra({
      depends_on: [
        { type: 'campaign', id: 12 },
        { type: 'budget', id: '401' },
        'campaign:7',
      ],
    });
    expect(dependsOn(evidence)).toEqual(['campaign:12', 'budget:401', 'campaign:7']);
  });

  it('skips subjects without a type or an id, and entries of any other type', () => {
    const evidence = withExtra({
      depends_on: [
        { type: 'campaign' },
        { id: 3 },
        { type: 'campaign', id: 1.5 },
        { type: '', id: 4 },
        5,
        '',
        null,
        { type: 'budget', id: 402 },
      ],
    });
    expect(dependsOn(evidence)).toEqual(['budget:402']);
  });

  it('returns [] when the item records no dependency', () => {
    expect(dependsOn(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual([]);
    expect(dependsOn(withExtra({ depends_on: { type: 'campaign', id: 12 } }))).toEqual([]);
    expect(dependsOn(null)).toEqual([]);
  });
});

describe('blockingDiscrepancies', () => {
  it('returns the blocking discrepancy ids as strings, in stored order', () => {
    const evidence = withExtra({ blocking_discrepancies: [51, '52', 1.5, true, '', null, {}] });
    expect(blockingDiscrepancies(evidence)).toEqual(['51', '52']);
  });

  it('returns [] for an item no discrepancy held', () => {
    expect(blockingDiscrepancies(pageItem(FIXTURE_ITEM_IDS.c1))).toEqual([]);
    expect(blockingDiscrepancies(withExtra({ blocking_discrepancies: 51 }))).toEqual([]);
    expect(blockingDiscrepancies(undefined)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------
// Keywords to adjust: K1, K2, K3
// ---------------------------------------------------------------------------------------------

describe('keywordFields', () => {
  it('reads the K1 card fields in the user order, with cost/conversion "—" as sent', () => {
    const fields = keywordFields(pageItem(FIXTURE_ITEM_IDS.k1));
    expect(fields).toEqual({
      term: 'free crm template',
      campaign: 'Core CRM — US',
      adGroup: 'CRM Software',
      spend: metric('spend', '186.40', { label: 'Spend', unit: 'currency', currency: 'USD' }),
      conversions: metric('conversions', '0', { label: 'Conversions', unit: 'conversions' }),
      costPerConversion: metric('cost_per_conversion', EM_DASH, {
        label: 'Cost/conversion',
        unit: 'currency',
        currency: 'USD',
      }),
    });
    expect(Object.keys(fields)).toEqual([
      'term',
      'campaign',
      'adGroup',
      'spend',
      'conversions',
      'costPerConversion',
    ]);
  });

  it('reads the K3 keyword text and its metrics the same way', () => {
    const fields = keywordFields(pageItem(FIXTURE_ITEM_IDS.k3));
    expect(fields.term).toBe('crm software');
    expect(fields.spend?.value).toBe('1205.40');
    expect(fields.costPerConversion?.value).toBe('86.10');
  });

  it('falls back from the term metric to extra.term, then to the entity name', () => {
    expect(keywordFields(withExtra({ term: 'crm demo' })).term).toBe('crm demo');

    const item = makeItem({ evidence: withExtra({ campaign: 'Core CRM — US' }) });
    expect(keywordFields(item).term).toBe('crm demo');
    expect(keywordFields(item.evidence, item).term).toBe('crm demo');

    const unavailableTerm = withExtra({ term: 'from extra' }, [
      metric('term', 'from metric', { unavailable: true }),
    ]);
    expect(keywordFields(unavailableTerm).term).toBe('from extra');
    expect(keywordFields(withExtra(null, [metric('term', 2026)])).term).toBe('2026');
  });

  it('prefers the item argument over the source entity for bare evidence', () => {
    const named: EvidenceHolder = { evidence: makeEvidence(), entity: { name: 'from item' } };
    const source: EvidenceHolder = { evidence: makeEvidence(), entity: { name: 'from source' } };
    expect(keywordFields(source, named).term).toBe('from item');
    expect(keywordFields(source, null).term).toBe('from source');
  });

  it('returns "" for the term and nulls for the rest when nothing names them', () => {
    expect(keywordFields(makeEvidence())).toEqual({
      term: '',
      campaign: null,
      adGroup: null,
      spend: null,
      conversions: null,
      costPerConversion: null,
    });
    expect(keywordFields(null).term).toBe('');
    const unnamed: EvidenceHolder = { evidence: makeEvidence(), entity: { name: '  ' } };
    expect(keywordFields(unnamed).term).toBe('');
    const noEntity: EvidenceHolder = { evidence: makeEvidence(), entity: null };
    expect(keywordFields(noEntity).term).toBe('');
    const booleanTerm = withExtra({ campaign: 7, ad_group: '' }, [metric('term', false)]);
    expect(keywordFields(booleanTerm)).toMatchObject({ term: '', campaign: null, adGroup: null });
  });
});

describe('adjacentTerms', () => {
  it('reads the K2 adjacent terms in stored order, amounts kept as sent', () => {
    expect(adjacentTerms(pageItem(FIXTURE_ITEM_IDS.k2))).toEqual([
      {
        term: 'crm for small teams',
        conversions: '1',
        clicks: 9,
        spend: '38.20',
        currency: 'USD',
      },
      {
        term: 'small business crm software',
        conversions: '0',
        clicks: 7,
        spend: '27.10',
        currency: 'USD',
      },
      {
        term: 'best crm for small business',
        conversions: '0',
        clicks: 5,
        spend: '19.80',
        currency: 'USD',
      },
    ]);
  });

  it('reads plain string entries as terms without figures', () => {
    expect(adjacentTerms(withExtra({ adjacent_terms: ['crm demo', 'crm trial'] }))).toEqual([
      { term: 'crm demo', conversions: null, clicks: null, spend: null, currency: null },
      { term: 'crm trial', conversions: null, clicks: null, spend: null, currency: null },
    ]);
  });

  it('skips entries without a term and nulls figures of the wrong type', () => {
    const evidence = withExtra({
      adjacent_terms: [
        null,
        5,
        '   ',
        { term: '' },
        { spend: '1.00' },
        { term: 'crm pricing', clicks: 2.5, conversions: 0.5, spend: 12, currency: 840 },
        { term: 'crm reviews', clicks: '4', conversions: 'n/a', spend: ' 3.10 ', currency: '' },
      ],
    });
    expect(adjacentTerms(evidence)).toEqual([
      { term: 'crm pricing', conversions: '0.5', clicks: null, spend: '12', currency: null },
      { term: 'crm reviews', conversions: null, clicks: 4, spend: '3.10', currency: null },
    ]);
  });

  it(`lists at most ${String(ADJACENT_TERMS_MAX)} terms, counting only valid entries`, () => {
    expect(ADJACENT_TERMS_MAX).toBe(5);
    const evidence = withExtra({
      adjacent_terms: [{ term: '' }, 't1', 't2', null, 't3', 't4', 't5', 't6', 't7'],
    });
    expect(adjacentTerms(evidence).map((entry) => entry.term)).toEqual([
      't1',
      't2',
      't3',
      't4',
      't5',
    ]);
  });

  it('returns [] when the item has no adjacent terms', () => {
    expect(adjacentTerms(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual([]);
    expect(adjacentTerms(withExtra({ adjacent_terms: 'crm demo' }))).toEqual([]);
    expect(adjacentTerms(null)).toEqual([]);
  });
});

describe('reviewHalves', () => {
  it('reads the K3 quality score and CPC by half', () => {
    const halves = reviewHalves(pageItem(FIXTURE_ITEM_IDS.k3));
    expect({
      qsFirst: halves.qsFirst?.value,
      qsLast: halves.qsLast?.value,
      cpcFirstHalf: halves.cpcFirstHalf?.value,
      cpcSecondHalf: halves.cpcSecondHalf?.value,
      cpaFirstHalf: halves.cpaFirstHalf?.value,
      cpaSecondHalf: halves.cpaSecondHalf?.value,
    }).toEqual({
      qsFirst: '7.0',
      qsLast: '5.8',
      cpcFirstHalf: '2.10',
      cpcSecondHalf: '2.52',
      cpaFirstHalf: '75.00',
      cpaSecondHalf: '97.20',
    });
    expect(halves.qsFirst?.label).toBe('Mean quality score, first 7 days');
    expect(halves.cpcSecondHalf?.currency).toBe('USD');
  });

  it('returns null for every half the evidence lacks', () => {
    expect(reviewHalves(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual({
      qsFirst: null,
      qsLast: null,
      cpcFirstHalf: null,
      cpcSecondHalf: null,
      cpaFirstHalf: null,
      cpaSecondHalf: null,
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Campaigns to pause or launch: C1, C1b, C2, C3a, C3b
// ---------------------------------------------------------------------------------------------

describe('PAUSE_SCALE_METRIC_KEYS and PIPELINE_METRIC_KEYS', () => {
  it('list the card columns in the AAP 0.5.5 order', () => {
    expect(PAUSE_SCALE_METRIC_KEYS).toEqual(['spend', 'outcomes', 'expected_outcomes', 'ql']);
    expect(PIPELINE_METRIC_KEYS).toEqual([
      'matured_leads',
      'matched_leads',
      'unresolved',
      'eligible_contacts',
      'mql',
      'ql',
      'meetings',
    ]);
  });

  it('keep MQL and QL as two separate keys, with no key combining them', () => {
    const stageKeys = PIPELINE_METRIC_KEYS.filter((key) => key.includes('ql'));
    expect(stageKeys).toEqual(['mql', 'ql']);
  });
});

describe('pipelineFields', () => {
  it('reads the C2 "Hold" pipeline columns, keeping a known QL of 0 as 0', () => {
    const fields = pipelineFields(pageItem(FIXTURE_ITEM_IDS.c2Hold));
    expect({
      spend: fields.spend?.value,
      outcomes: fields.outcomes?.value,
      leads: fields.leads?.value,
      maturedLeads: fields.maturedLeads?.value,
      matchedLeads: fields.matchedLeads?.value,
      unresolvedLeads: fields.unresolvedLeads?.value,
      eligibleContacts: fields.eligibleContacts?.value,
      mql: fields.mql?.value,
      ql: fields.ql?.value,
      meetings: fields.meetings?.value,
    }).toEqual({
      spend: '1500.00',
      outcomes: 48,
      leads: 48,
      maturedLeads: 30,
      matchedLeads: 28,
      unresolvedLeads: 2,
      eligibleContacts: 26,
      mql: 4,
      ql: 0,
      meetings: 0,
    });
    expect(fields.expectedOutcomes).toBeNull();
    expect(fields.mql?.key).toBe('mql');
    expect(fields.ql?.key).toBe('ql');
    expect(fields.crmQl).toEqual(fields.ql);
  });

  it('reads the C1 pause columns and leaves CRM QL null when the campaign has no CRM link', () => {
    const fields = pipelineFields(pageItem(FIXTURE_ITEM_IDS.c1));
    expect(fields.spend?.value).toBe('314.50');
    expect(fields.outcomes?.value).toBe(0);
    expect(fields.expectedOutcomes?.value).toBe('3.4');
    expect(fields.crmQl).toBeNull();
    expect(fields.ql).toBeNull();
    expect(fields.mql).toBeNull();
  });

  it('returns null for every column of an absent source', () => {
    expect(Object.values(pipelineFields(null)).every((value) => value === null)).toBe(true);
    expect(Object.keys(pipelineFields(null))).toHaveLength(12);
  });
});

describe('scaleOutcome', () => {
  it('reads "Hold" on a vetoed C2 and "Scale" on a B1', () => {
    expect(scaleOutcome(pageItem(FIXTURE_ITEM_IDS.c2Hold))).toBe('Hold');
    expect(scaleOutcome(pageItem(FIXTURE_ITEM_IDS.b1))).toBe('Scale');
    expect(scaleOutcome(withExtra({ outcome: 'Scale' }))).toBe('Scale');
  });

  it('returns null for any other item or value', () => {
    expect(scaleOutcome(pageItem(FIXTURE_ITEM_IDS.c1))).toBeNull();
    expect(scaleOutcome(withExtra({ outcome: 'scale' }))).toBeNull();
    expect(scaleOutcome(withExtra({ outcome: 1 }))).toBeNull();
    expect(scaleOutcome(makeEvidence())).toBeNull();
    expect(scaleOutcome(null)).toBeNull();
  });
});

describe('launchGap', () => {
  it('reads the C3b cluster with its head tokens and member terms', () => {
    expect(launchGap(pageItem(FIXTURE_ITEM_IDS.c3b))).toEqual({
      kind: 'cluster',
      facet: null,
      value: null,
      headTokens: ['data', 'warehouse'],
      terms: [
        { term: 'cloud data warehouse', spend: '132.00', conversions: '3', clicks: 41 },
        { term: 'data warehouse tools', spend: '96.00', conversions: '2.5', clicks: 33 },
        { term: 'data warehouse pricing', spend: '59.00', conversions: '1.5', clicks: 22 },
      ],
    });
  });

  it('reads a C3a segment from facet_label, else facet, with its value', () => {
    const labelled = withExtra({
      facet: 'job_function',
      facet_label: 'job function',
      value: 'Finance',
      head_tokens: ['ignored'],
    });
    expect(launchGap(labelled)).toEqual({
      kind: 'segment',
      facet: 'job function',
      value: 'Finance',
      headTokens: [],
      terms: [],
    });
    const unlabelled = withExtra({ facet: 'seniority', facet_label: '', value: 'Director' });
    expect(launchGap(unlabelled)).toMatchObject({
      kind: 'segment',
      facet: 'seniority',
      value: 'Director',
    });
  });

  it('reads a cluster from head tokens alone or from member terms alone', () => {
    expect(launchGap(withExtra({ head_tokens: ['crm', ' ', 3] }))).toEqual({
      kind: 'cluster',
      facet: null,
      value: null,
      headTokens: ['crm'],
      terms: [],
    });
    const membersOnly = withExtra({
      facet: 'job function',
      members: [
        null,
        'crm',
        { term: '' },
        { term: 'crm suite', spend: 'n/a', conversions: Number.NaN, clicks: 2.5 },
      ],
    });
    expect(launchGap(membersOnly)).toEqual({
      kind: 'cluster',
      facet: null,
      value: null,
      headTokens: [],
      terms: [{ term: 'crm suite', spend: null, conversions: null, clicks: null }],
    });
  });

  it('never reads shared-budget members (campaigns, no term) as cluster terms', () => {
    const b3 = pageItem(FIXTURE_ITEM_IDS.b3);
    expect(sharedMembers(b3)).toHaveLength(2);
    expect(launchGap(b3)).toEqual({
      kind: null,
      facet: null,
      value: null,
      headTokens: [],
      terms: [],
    });
  });

  it('returns a frozen empty gap for an item that is no launch idea', () => {
    const gap = launchGap(null);
    expect(gap).toEqual({ kind: null, facet: null, value: null, headTokens: [], terms: [] });
    expect(Object.isFrozen(gap)).toBe(true);
    expect(Object.isFrozen(gap.headTokens)).toBe(true);
    expect(Object.isFrozen(gap.terms)).toBe(true);
    expect(launchGap(pageItem(FIXTURE_ITEM_IDS.k1)).kind).toBeNull();
    expect(launchGap(withExtra({ facet: 'job function' })).kind).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Budget changes: B1, B2, B3 and C2 "Scale"
// ---------------------------------------------------------------------------------------------

describe('proposedDailyBudget', () => {
  it('reads the B1 and B3 proposed daily budgets from their metrics', () => {
    expect(proposedDailyBudget(pageItem(FIXTURE_ITEM_IDS.b1))).toBe('240.00');
    expect(proposedDailyBudget(pageItem(FIXTURE_ITEM_IDS.b3))).toBe('110.00');
  });

  it('reads extra.proposed_daily_budget, then b1_link, then the metric, then proposed', () => {
    const proposed = metric('proposed_daily_budget', '280.00', { currency: 'USD' });
    const b1Link = { proposed_daily_budget: '260.00' };
    const own = { proposed_daily_budget: '250.00', b1_link: b1Link, proposed: '270.00' };
    expect(proposedDailyBudget(withExtra(own, [proposed]))).toBe('250.00');
    const linked = { b1_link: b1Link, proposed: '270.00' };
    expect(proposedDailyBudget(withExtra(linked, [proposed]))).toBe('260.00');
    expect(proposedDailyBudget(withExtra({ proposed: '270.00' }, [proposed]))).toBe('280.00');
    expect(proposedDailyBudget(withExtra({ proposed: '270.00' }))).toBe('270.00');
  });

  it('skips a malformed layer and reads the next one', () => {
    const evidence = withExtra(
      { proposed_daily_budget: 'USD 240', b1_link: 'B1', proposed: '90.00' },
      [metric('proposed_daily_budget', null, { unavailable: true })],
    );
    expect(proposedDailyBudget(evidence)).toBe('90.00');
  });

  it('converts a JSON number with String and trims a decimal string', () => {
    expect(proposedDailyBudget(withExtra({ proposed_daily_budget: 240 }))).toBe('240');
    expect(proposedDailyBudget(withExtra({ proposed_daily_budget: ' 240.50 ' }))).toBe('240.50');
  });

  it('refuses a malformed amount in every layer rather than parsing part of it', () => {
    const malformed: readonly unknown[] = ['12.3.4', { amount: '12.00' }, ['12.00'], '', '1e999x'];
    for (const amount of malformed) {
      const label = JSON.stringify(amount);
      const own = withExtra({ proposed_daily_budget: amount });
      const linked = withExtra({ b1_link: { proposed_daily_budget: amount } });
      const legacy = withExtra({ proposed: amount });
      expect(proposedDailyBudget(own), label).toBeNull();
      expect(proposedDailyBudget(linked), label).toBeNull();
      expect(proposedDailyBudget(legacy), label).toBeNull();
    }
    const objectMetric = rawEvidence({
      metrics: [{ key: 'proposed_daily_budget', value: { amount: '12.00' } }],
    });
    expect(proposedDailyBudget(objectMetric)).toBeNull();
    const dottedMetric = withExtra(null, [metric('proposed_daily_budget', '12.3.4')]);
    expect(proposedDailyBudget(dottedMetric)).toBeNull();
  });

  it('always returns a decimal string, never a number, so no amount went through Number()', () => {
    const sources: readonly Evidence[] = [
      withExtra({ proposed_daily_budget: 240 }),
      withExtra({ b1_link: { proposed_daily_budget: 0.1 } }),
      withExtra(null, [metric('proposed_daily_budget', 250)]),
      withExtra({ proposed: '240.10' }),
    ];
    expect(sources.map((source) => typeof proposedDailyBudget(source))).toEqual([
      'string',
      'string',
      'string',
      'string',
    ]);
    expect(proposedDailyBudget(withExtra({ proposed: '240.10' }))).toBe('240.10');
    expect(proposedDailyBudget(withExtra({ proposed_daily_budget: '240.000000' }))).toBe(
      '240.000000',
    );
  });

  it('returns null when the item proposes no amount', () => {
    expect(proposedDailyBudget(pageItem(FIXTURE_ITEM_IDS.c1))).toBeNull();
    expect(
      proposedDailyBudget(
        withExtra({ proposed_daily_budget: Number.NaN, proposed: Number.POSITIVE_INFINITY }),
      ),
    ).toBeNull();
    expect(proposedDailyBudget(withExtra({ proposed: true }))).toBeNull();
    expect(proposedDailyBudget(null)).toBeNull();
  });
});

describe('sharedMembers', () => {
  it("lists the B3 shared budget's member campaigns with their member days", () => {
    expect(sharedMembers(pageItem(FIXTURE_ITEM_IDS.b3))).toEqual([
      {
        id: String(FIXTURE_CAMPAIGN_IDS.displayProspecting),
        name: 'Display Prospecting — US',
        memberDays: 24,
      },
      {
        id: String(FIXTURE_CAMPAIGN_IDS.displayRetargeting),
        name: 'Display Retargeting — US',
        memberDays: 18,
      },
    ]);
  });

  it('returns [] for an own-budget subject and for C3b cluster members', () => {
    expect(sharedMembers(pageItem(FIXTURE_ITEM_IDS.b1))).toEqual([]);
    expect(sharedMembers(pageItem(FIXTURE_ITEM_IDS.c3b))).toEqual([]);
    expect(sharedMembers(withExtra({ members: { campaign_id: 1, name: 'A' } }))).toEqual([]);
    expect(sharedMembers(null)).toEqual([]);
  });

  it('falls back to id, needs an id and a name, and keeps only whole member days', () => {
    const evidence = withExtra({
      members: [
        { id: 9, name: 'A' },
        { campaign_id: 'c-1', name: 'B', member_days: ' 12 ' },
        { campaign_id: 3, name: 'C', member_days: 2.5 },
        { campaign_id: 6, name: 'G', member_days: '1e999' },
        { campaign_id: 8, name: 'H', member_days: true },
        { campaign_id: 10, name: 'I', member_days: Number.POSITIVE_INFINITY },
        { campaign_id: 1.5, name: 'D' },
        { name: 'E' },
        { campaign_id: 4 },
        { campaign_id: 5, name: '  ' },
        null,
        'F',
      ],
    });
    expect(sharedMembers(evidence)).toEqual([
      { id: '9', name: 'A', memberDays: null },
      { id: 'c-1', name: 'B', memberDays: 12 },
      { id: '3', name: 'C', memberDays: null },
      { id: '6', name: 'G', memberDays: null },
      { id: '8', name: 'H', memberDays: null },
      { id: '10', name: 'I', memberDays: null },
    ]);
  });
});

describe('funding', () => {
  it("reads the B1 increase, its donors and the backend's funding sentence", () => {
    expect(funding(pageItem(FIXTURE_ITEM_IDS.b1))).toEqual({
      increase: '40.00',
      fromReallocation: '30.00',
      fromNewBudget: '10.00',
      donors: [
        {
          id: String(FIXTURE_BUDGET_IDS.display),
          type: 'budget',
          name: 'Display Shared Budget',
          amount: '30.00',
        },
      ],
      text:
        'Raise by USD 40.00: USD 30.00 funded by reallocation from Display Shared Budget, ' +
        'USD 10.00 from new budget',
    });
  });

  it('returns null when extra.increase is missing or not an amount', () => {
    expect(funding(pageItem(FIXTURE_ITEM_IDS.b3))).toBeNull();
    expect(funding(pageItem(FIXTURE_ITEM_IDS.c1))).toBeNull();
    expect(funding(withExtra({ increase: 'forty' }))).toBeNull();
    expect(funding(makeEvidence())).toBeNull();
    expect(funding(null)).toBeNull();
  });

  it('reads an unfunded increase as no donors and no sentence', () => {
    expect(funding(withExtra({ increase: 40, allocations: 'none' }))).toEqual({
      increase: '40',
      fromReallocation: null,
      fromNewBudget: null,
      donors: [],
      text: null,
    });
  });

  it('skips donors without a subject id, a name or an amount, and B3 receiver entries', () => {
    const evidence = withExtra({
      increase: '40.00',
      allocations: [
        null,
        { donor: 'budget:401', donor_name: 'X', amount: '1.00' },
        { donor: { type: 'budget' }, donor_name: 'X', amount: '1.00' },
        { donor: { id: 401 }, amount: '1.00' },
        { donor: { id: 401 }, donor_name: 'X', amount: 'one' },
        { receiver: { type: 'campaign', id: 305 }, receiver_name: 'P', amount: '30.00' },
        { donor: { id: 402, type: 7 }, donor_name: 'Search Shared Budget', amount: 12.5 },
      ],
    });
    expect(funding(evidence)?.donors).toEqual([
      { id: '402', type: null, name: 'Search Shared Budget', amount: '12.5' },
    ]);
  });
});

describe('freedAmount and unallocatedAmount', () => {
  it('read the B3 freed and unallocated amounts, keeping a known zero', () => {
    const b3 = pageItem(FIXTURE_ITEM_IDS.b3);
    expect(freedAmount(b3)).toBe('30.00');
    expect(unallocatedAmount(b3)).toBe('0.00');
    expect(freedAmount(withExtra({ freed: 30 }))).toBe('30');
    expect(unallocatedAmount(withExtra({ unallocated: 0 }))).toBe('0');
  });

  it('return null when absent or not an amount', () => {
    const b1 = pageItem(FIXTURE_ITEM_IDS.b1);
    expect(freedAmount(b1)).toBeNull();
    expect(unallocatedAmount(b1)).toBeNull();
    expect(freedAmount(withExtra({ freed: '' }))).toBeNull();
    expect(freedAmount(withExtra({ freed: 'Infinity' }))).toBeNull();
    expect(unallocatedAmount(withExtra({ unallocated: true }))).toBeNull();
    expect(unallocatedAmount(withExtra({ unallocated: Number.NEGATIVE_INFINITY }))).toBeNull();
    expect(freedAmount(null)).toBeNull();
    expect(unallocatedAmount(undefined)).toBeNull();
  });
});

describe('allocations', () => {
  it('reads each B3 reallocation with its receiver and the backend line', () => {
    expect(allocations(pageItem(FIXTURE_ITEM_IDS.b3))).toEqual([
      {
        amount: '30.00',
        receiverId: String(FIXTURE_CAMPAIGN_IDS.pipelineSearch),
        receiverType: 'campaign',
        receiverName: 'Pipeline Search — US',
        receiverItemId: null,
        text: "Move USD 30.00 to fund Pipeline Search — US's recommended increase (B1)",
      },
    ]);
  });

  it("yields [] for a B1's donor list and for an item without allocations", () => {
    expect(allocations(pageItem(FIXTURE_ITEM_IDS.b1))).toEqual([]);
    expect(allocations(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual([]);
    expect(allocations(withExtra({ allocations: { receiver: {} } }))).toEqual([]);
    expect(allocations(null)).toEqual([]);
  });

  it("reads the receiver's B1 item id when given, and a blank line as none", () => {
    const evidence = withExtra({
      allocations: [
        {
          receiver: { type: 'campaign', id: 305 },
          receiver_name: 'Pipeline Search — US',
          amount: '30.00',
          receiver_item_id: FIXTURE_ITEM_IDS.b1,
          text: '  ',
        },
      ],
    });
    expect(allocations(evidence)).toEqual([
      {
        amount: '30.00',
        receiverId: '305',
        receiverType: 'campaign',
        receiverName: 'Pipeline Search — US',
        receiverItemId: String(FIXTURE_ITEM_IDS.b1),
        text: null,
      },
    ]);
  });

  it('skips allocations without a receiver id, a receiver name or an amount', () => {
    const evidence = withExtra({
      allocations: [
        null,
        { receiver: 'campaign:305', receiver_name: 'P', amount: '1.00' },
        { receiver: { type: 'campaign' }, receiver_name: 'P', amount: '1.00' },
        { receiver: { id: 305 }, amount: '1.00' },
        { receiver: { id: 305 }, receiver_name: 'P', amount: null },
        { receiver: { id: 'r-305' }, receiver_name: 'P', amount: '5.00' },
      ],
    });
    expect(allocations(evidence)).toEqual([
      {
        amount: '5.00',
        receiverId: 'r-305',
        receiverType: null,
        receiverName: 'P',
        receiverItemId: null,
        text: null,
      },
    ]);
  });
});

describe('dayCounts', () => {
  it('lists the budget day counts in API order with the stricter gate requirement', () => {
    const b1 = pageItem(FIXTURE_ITEM_IDS.b1);
    // The gate exactly as metrics.cap_constrained freezes it: the share checks are whole days
    // (⌈0.7 × 26⌉ = 19), so capped days bind on the share, not on the 5-day minimum.
    const evidence: Evidence = {
      ...b1.evidence,
      extra: {
        ...b1.evidence.extra,
        gate: {
          known_budget_days: { observed: 26, required: 7 },
          spending_days: { observed: 26, required: 5 },
          capped_days: { observed: 22, required: 5 },
          capped_share: { observed: 22, required: 19 },
        },
      },
    };
    expect(dayCounts(evidence)).toEqual([
      { key: 'known_days', label: 'Known-budget days', observed: 26, required: 7 },
      { key: 'spending_days', label: 'Spending days', observed: 26, required: 5 },
      { key: 'capped_days', label: 'Capped days', observed: 22, required: 19 },
      { key: 'idle_days', label: 'Known idle days', observed: 0, required: null },
      {
        key: 'changed_intraday_days',
        label: 'Changed-intraday days (unknown budget)',
        observed: 1,
        required: null,
      },
      {
        key: 'unobserved_days',
        label: 'Unobserved days (unknown budget)',
        observed: 1,
        required: null,
      },
    ]);
  });

  it('keeps the shared-budget label and maps the B2 and B3 predicates to their checks', () => {
    const evidence = withExtra(
      {
        gate: {
          known_budget_days: { observed: 7, required: 7 },
          spending_days: { observed: 6, required: 5 },
          spending_share: { observed: 6, required: 6 },
          high_utilization_days: { observed: 5, required: 5 },
          under_utilized_days: { observed: 4, required: '5' },
        },
      },
      [
        metric('known_days', 7, { label: 'Budget-active days' }),
        metric('window_days', 28),
        metric('spending_days', 6),
        metric('high_utilization_days', 5),
        metric('under_utilized_days', '4'),
        metric('capped_days', 2.5),
        metric('unobserved_days', 'n/a'),
        metric('idle_days', 1),
      ],
    );
    expect(dayCounts(evidence)).toEqual([
      { key: 'known_days', label: 'Budget-active days', observed: 7, required: 7 },
      { key: 'spending_days', label: 'spending_days', observed: 6, required: 6 },
      { key: 'high_utilization_days', label: 'high_utilization_days', observed: 5, required: 5 },
      { key: 'under_utilized_days', label: 'under_utilized_days', observed: 4, required: 5 },
      { key: 'idle_days', label: 'idle_days', observed: 1, required: null },
    ]);
  });

  it('reads a requirement only from a whole-day gate entry', () => {
    const metrics = [metric('known_days', 7), metric('spending_days', 6), metric('capped_days', 5)];
    expect(dayCounts(withExtra(null, metrics)).map((count) => count.required)).toEqual([
      null,
      null,
      null,
    ]);
    const malformed = withExtra(
      {
        gate: {
          known_budget_days: 7,
          spending_days: { required: 4.5 },
          spending_share: { required: 6 },
          capped_days: { observed: 5 },
        },
      },
      metrics,
    );
    expect(dayCounts(malformed).map((count) => count.required)).toEqual([null, 6, null]);
    expect(dayCounts(withExtra({ gate: 'cap_constrained' }, metrics))[0]?.required).toBeNull();
  });

  it('returns [] for an item without budget history', () => {
    expect(dayCounts(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual([]);
    expect(dayCounts(null)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------
// Copy/creative changes: CR1, CR2, CR3
// ---------------------------------------------------------------------------------------------

describe('variants', () => {
  it("reads the CR3 item's variants table, rates kept as ratio decimal strings", () => {
    expect(variants(pageItem(FIXTURE_ITEM_IDS.cr3))).toEqual([
      {
        creativeId: '1101',
        urn: 'urn:li:sponsoredCreative:990001101',
        name: 'Variant A',
        impressions: 13250,
        clickRate: '0.004377',
        openRate: '0.012226',
        completionRate: '0.395062',
        leads: 64,
        formOpens: 162,
        leadGen: true,
      },
      {
        creativeId: '1102',
        urn: 'urn:li:sponsoredCreative:990001102',
        name: 'Variant B',
        impressions: 12400,
        clickRate: '0.004032',
        openRate: '0.011935',
        completionRate: '0.331081',
        leads: 49,
        formOpens: 148,
        leadGen: true,
      },
    ]);
  });

  it('keeps trailing zeros of the completion-rate test rates, so no rate is parsed', () => {
    const rows = variants(makeCompletionRateTestItem());
    expect(rows.map((row) => row.name)).toEqual(['Benchmarks Variant A', 'Benchmarks Variant B']);
    expect(rows[0]).toMatchObject({
      creativeId: '1701',
      impressions: 42000,
      openRate: '0.012000',
      completionRate: '0.250000',
      leads: 126,
    });
  });

  it('leaves the open and completion rates null for a non-lead-gen variant, marked not lead-gen', () => {
    // The row exactly as creative._variant_row writes it for a website-conversions campaign:
    // only the click rate applies, so open_rate and completion_rate are None, never 0, and the
    // frozen objective group (creative._Campaign.summary) says the stages do not apply.
    const nonLeadGen = withExtra({
      campaign: { id: 2100, name: 'Webinar sign-ups', objective_group: 'linkedin_other' },
      variants: [
        {
          creative_id: 2101,
          urn: 'urn:li:sponsoredCreative:990002101',
          name: 'Webinar Variant A',
          headline: 'Join the live demo',
          cta_label: 'Register',
          impressions: 18400,
          clicks: 92,
          form_opens: null,
          leads: null,
          click_rate: '0.005000',
          open_rate: null,
          completion_rate: null,
          cost: '640.00',
          currency: 'USD',
          outcomes: 11,
        },
      ],
    });
    expect(variants(nonLeadGen)).toEqual([
      {
        creativeId: '2101',
        urn: 'urn:li:sponsoredCreative:990002101',
        name: 'Webinar Variant A',
        impressions: 18400,
        clickRate: '0.005000',
        openRate: null,
        completionRate: null,
        leads: null,
        formOpens: null,
        leadGen: false,
      },
    ]);
  });

  it('reads a lead-gen variant with zero form opens as lead-gen, its opens a known zero', () => {
    // creative._variant_row for a lead-gen creative that delivered but whose form was never
    // opened: the completion rate's denominator is 0, so _ratio leaves the rate None.
    const zeroOpens = withExtra({
      campaign: { id: 2200, name: 'Lead Gen — Guide', objective_group: 'linkedin_lead_gen' },
      variants: [
        {
          creative_id: 2201,
          name: 'Guide Variant A',
          impressions: 3100,
          clicks: 9,
          form_opens: 0,
          leads: 0,
          click_rate: '0.002903',
          open_rate: '0.000000',
          completion_rate: null,
        },
      ],
    });
    expect(variants(zeroOpens)).toEqual([
      {
        creativeId: '2201',
        urn: null,
        name: 'Guide Variant A',
        impressions: 3100,
        clickRate: '0.002903',
        openRate: '0.000000',
        completionRate: null,
        leads: 0,
        formOpens: 0,
        leadGen: true,
      },
    ]);
  });

  it('leaves leadGen null when the evidence names no objective group, or a malformed one', () => {
    const row = { creative_id: 2301, name: 'Variant', impressions: 1500, form_opens: '12' };
    const unknown = [
      withExtra({ variants: [row] }),
      withExtra({ campaign: null, variants: [row] }),
      withExtra({ campaign: 'linkedin_lead_gen', variants: [row] }),
      withExtra({ campaign: { name: 'No objective' }, variants: [row] }),
      withExtra({ campaign: { objective_group: 7 }, variants: [row] }),
      withExtra({ campaign: { objective_group: true }, variants: [row] }),
      withExtra({ campaign: { objective_group: ['linkedin_lead_gen'] }, variants: [row] }),
      withExtra({ campaign: { objective_group: '  ' }, variants: [row] }),
    ];
    for (const evidence of unknown) {
      expect(variants(evidence)).toEqual([
        expect.objectContaining({ creativeId: '2301', formOpens: 12, leadGen: null }),
      ]);
    }
    // The objective is the campaign's, so every row carries the same answer.
    const twoRows = withExtra({
      campaign: { objective_group: 'linkedin_other' },
      variants: [row, { ...row, creative_id: 2302, form_opens: 2.5 }],
    });
    expect(variants(twoRows).map((each) => [each.leadGen, each.formOpens])).toEqual([
      [false, 12],
      [false, null],
    ]);
  });

  it('is safe on the first item of an empty creative group', () => {
    const items: readonly Item[] = [];
    expect(variants(items[0])).toEqual([]);
    expect(variants(pageItem(FIXTURE_ITEM_IDS.cr1Historical))).toEqual([]);
    expect(variants(withExtra({ variants: { creative_id: 1 } }))).toEqual([]);
  });

  it('falls back to the URN for id and name, and nulls rates and counts it cannot read', () => {
    const evidence = withExtra({
      variants: [
        { urn: 'urn:li:sponsoredCreative:1' },
        { creative_id: 7 },
        {
          creative_id: 'c-9',
          name: 'Named',
          impressions: '1200',
          click_rate: 0.004,
          open_rate: null,
          completion_rate: 'n/a',
          leads: 1.5,
        },
        { name: 'No id' },
        { creative_id: 2.5, urn: '' },
        null,
        'Variant C',
      ],
    });
    expect(variants(evidence)).toEqual([
      {
        creativeId: 'urn:li:sponsoredCreative:1',
        urn: 'urn:li:sponsoredCreative:1',
        name: 'urn:li:sponsoredCreative:1',
        impressions: null,
        clickRate: null,
        openRate: null,
        completionRate: null,
        leads: null,
        formOpens: null,
        leadGen: null,
      },
      {
        creativeId: '7',
        urn: null,
        name: '7',
        impressions: null,
        clickRate: null,
        openRate: null,
        completionRate: null,
        leads: null,
        formOpens: null,
        leadGen: null,
      },
      {
        creativeId: 'c-9',
        urn: null,
        name: 'Named',
        impressions: 1200,
        clickRate: '0.004',
        openRate: null,
        completionRate: null,
        leads: null,
        formOpens: null,
        leadGen: null,
      },
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// White papers: W1, W2 and "No change warranted" rows
// ---------------------------------------------------------------------------------------------

describe('WHITE_PAPER_METRIC_KEYS and UNKNOWN_CREATIVE_KEY', () => {
  it('list the white-paper columns in table order, MQL and QL apart', () => {
    expect(WHITE_PAPER_METRIC_KEYS).toEqual([
      'spend',
      'linkedin_leads',
      'mql',
      'ql',
      'meetings_booked',
      'other_meetings',
      'deals_after_touch',
      'cost_per_ql',
      'cost_per_meeting',
    ]);
    expect(WHITE_PAPER_METRIC_KEYS.filter((key) => key === 'mql' || key === 'ql')).toEqual([
      'mql',
      'ql',
    ]);
    expect(UNKNOWN_CREATIVE_KEY).toBe('unknown-creative');
  });

  it('hold no key that combines MQL and QL, so the two counts are never blended', () => {
    const tokensOf = (key: string): readonly string[] => key.split('_');
    const combined = WHITE_PAPER_METRIC_KEYS.filter((key) => {
      const tokens = tokensOf(key);
      return tokens.includes('mql') && tokens.includes('ql');
    });
    expect(combined).toEqual([]);
    const mqlKeys = WHITE_PAPER_METRIC_KEYS.filter((key) => tokensOf(key).includes('mql'));
    expect(mqlKeys).toEqual(['mql']);
    for (const key of WHITE_PAPER_METRIC_KEYS) {
      expect(key, key).not.toMatch(/mql.*ql|ql.*mql|leads_total|mql_or_ql|blended/);
    }
  });
});

describe('whitePaperCells', () => {
  it('reads the W1 asset row under a floor, every key in column order', () => {
    const cells = whitePaperCells(pageItem(FIXTURE_ITEM_IDS.wEur));
    expect(Object.keys(cells)).toEqual([...WHITE_PAPER_METRIC_KEYS]);
    expect(cells.spend).toEqual(
      metric('spend', '2840.00', { label: 'Spend', unit: 'currency', currency: 'EUR' }),
    );
    expect(cells.mql).toEqual(
      metric('mql', 14, { label: 'MQL (First meeting booked)', unit: 'contacts', floor: true }),
    );
    expect(cells.ql).toEqual(
      metric('ql', 6, { label: 'Qualified Lead', unit: 'contacts', floor: true }),
    );
    expect(cells.cost_per_ql).toEqual(
      metric('cost_per_ql', '473.33', {
        label: 'Cost per Qualified Lead',
        unit: 'currency',
        currency: 'EUR',
        floor: true,
      }),
    );
  });

  it('keeps an unavailable MQL unavailable and a zero-denominator cost as "—"', () => {
    const cells = whitePaperCells(pageItem(FIXTURE_ITEM_IDS.wUsd));
    expect(cells.mql).toMatchObject({ value: null, unavailable: true, floor: null });
    expect(cells.ql).toMatchObject({ value: 0, unavailable: null });
    expect(cells.cost_per_ql).toMatchObject({ value: EM_DASH, floor: null, currency: 'USD' });
    expect(cells.cost_per_meeting).toMatchObject({ value: EM_DASH, currency: 'USD' });
  });

  it('returns null for every key the evidence lacks', () => {
    const cells = whitePaperCells(makeEvidence());
    expect(Object.keys(cells)).toEqual([...WHITE_PAPER_METRIC_KEYS]);
    expect(Object.values(cells).every((cell) => cell === null)).toBe(true);
    expect(Object.values(whitePaperCells(null)).every((cell) => cell === null)).toBe(true);
  });
});

describe('creativeRows', () => {
  it('reads the W1 per-creative rows with asset labels, currency and floors', () => {
    const rows = creativeRows(pageItem(FIXTURE_ITEM_IDS.wEur));
    expect(rows.map((row) => [row.key, row.label, row.isUnknownCreative])).toEqual([
      ['urn:li:sponsoredCreative:990001101', 'Variant A', false],
      ['urn:li:sponsoredCreative:990001102', 'Variant B', false],
      [UNKNOWN_CREATIVE_KEY, 'unknown creative', true],
    ]);
    expect(rows[0]?.cells).toEqual({
      spend: metric('spend', '1480.00', {
        label: 'Spend',
        unit: 'currency',
        currency: 'EUR',
        unavailable: false,
      }),
      linkedin_leads: metric('linkedin_leads', 64, {
        label: 'LinkedIn leads (platform)',
        unit: 'leads',
        unavailable: false,
      }),
      mql: metric('mql', 8, {
        label: 'MQL (First meeting booked)',
        unit: 'contacts',
        floor: true,
        unavailable: false,
      }),
      ql: metric('ql', 4, {
        label: 'Qualified Lead',
        unit: 'contacts',
        floor: true,
        unavailable: false,
      }),
      meetings_booked: metric('meetings_booked', 2, {
        label: 'Meetings booked (record created)',
        unit: 'meetings',
        floor: true,
        unavailable: false,
      }),
      other_meetings: metric('other_meetings', 1, {
        label: 'Other meetings (ordering unverified)',
        unit: 'meetings',
        floor: true,
        unavailable: false,
      }),
      deals_after_touch: metric('deals_after_touch', 1, {
        label: 'Deals after touch',
        unit: 'deals',
        floor: true,
        unavailable: false,
      }),
      cost_per_ql: metric('cost_per_ql', '370.00', {
        label: 'Cost per Qualified Lead',
        unit: 'currency',
        currency: 'EUR',
        floor: true,
        unavailable: false,
      }),
      cost_per_meeting: metric('cost_per_meeting', '740.00', {
        label: 'Cost per meeting booked',
        unit: 'currency',
        currency: 'EUR',
        floor: true,
        unavailable: false,
      }),
    });
    expect(Object.values(rows[0]?.notes ?? {}).every((note) => note === null)).toBe(true);
  });

  it('keeps the "unknown creative" row last, its costs "—" with the backend note', () => {
    const unknown = creativeRows(pageItem(FIXTURE_ITEM_IDS.wEur)).at(-1);
    expect(unknown?.isUnknownCreative).toBe(true);
    expect(unknown?.cells.spend).toMatchObject({ value: '0', unavailable: false });
    expect(unknown?.cells.cost_per_ql).toMatchObject({
      value: EM_DASH,
      floor: false,
      unavailable: false,
      currency: 'EUR',
    });
    expect(unknown?.notes.cost_per_ql).toBe('the unknown creative row holds no spend');
    expect(unknown?.notes.cost_per_meeting).toBe('the unknown creative row holds no spend');
  });

  it('marks an unavailable MQL unavailable with its reason, never 0', () => {
    const [row] = creativeRows(pageItem(FIXTURE_ITEM_IDS.wUsd));
    expect(row?.cells.mql).toEqual(
      metric('mql', null, {
        label: 'MQL (First meeting booked)',
        unit: 'contacts',
        unavailable: true,
      }),
    );
    expect(row?.notes.mql).toBe('MQL stage "First meeting booked" not resolved');
    expect(row?.cells.ql).toMatchObject({ value: 0, floor: false, unavailable: false });
    expect(row?.cells.cost_per_ql).toMatchObject({ value: EM_DASH, currency: 'USD' });
    expect(row?.notes.cost_per_ql).toBe('no QL yet');
    expect(row?.notes.cost_per_meeting).toBe('no meetings yet');
  });

  it('uses the default labels and units when the asset row has no metrics', () => {
    const [row] = creativeRows(
      rowsEvidence([
        {
          creative_id: 1101,
          spend: '10.00',
          leads: 3,
          mql: { value: 2 },
          ql: { value: 1 },
          meetings_booked: { value: 1 },
          other_meetings: { value: 0 },
          deals: { value: 0 },
          cost_per_ql: { value: '10.00' },
          cost_per_meeting: { value: '10.00' },
        },
      ]),
    );
    const labelled = WHITE_PAPER_METRIC_KEYS.map((key) => [
      key,
      row?.cells[key]?.label,
      row?.cells[key]?.unit,
    ]);
    expect(labelled).toEqual([
      ['spend', 'Spend', 'currency'],
      ['linkedin_leads', 'LinkedIn leads (platform)', 'leads'],
      ['mql', 'MQL', 'contacts'],
      ['ql', 'Qualified Lead', 'contacts'],
      ['meetings_booked', 'Meetings booked (record created)', 'meetings'],
      ['other_meetings', 'Other meetings (ordering unverified)', 'meetings'],
      ['deals_after_touch', 'Deals after touch', 'deals'],
      ['cost_per_ql', 'Cost per Qualified Lead', 'currency'],
      ['cost_per_meeting', 'Cost per meeting booked', 'currency'],
    ]);
  });

  it('leaves a cell the row omits empty, and an omitted spend unavailable', () => {
    const [row] = creativeRows(rowsEvidence([{ creative_id: 1101, leads: 3 }]));
    expect(row?.cells.spend).toMatchObject({ value: null, unavailable: true, currency: null });
    expect(row?.cells.linkedin_leads).toMatchObject({ value: 3, unavailable: false });
    for (const key of ['mql', 'ql', 'meetings_booked', 'cost_per_ql', 'cost_per_meeting']) {
      expect(row?.cells[key], key).toBeNull();
    }
  });

  it('derives the key and label from the URN, then the id, then the unknown-creative row', () => {
    const rows = creativeRows(
      rowsEvidence([
        { creative_id: 1101 },
        { creative_urn: 'urn:li:sponsoredCreative:2', creative_id: 2 },
        { creative_id: null, creative_urn: '', label: '' },
        { creative_id: 3, label: 'Headline' },
        null,
        'row',
        ['row'],
      ]),
    );
    expect(rows.map((row) => [row.key, row.label, row.isUnknownCreative])).toEqual([
      ['1101', '1101', false],
      ['urn:li:sponsoredCreative:2', 'urn:li:sponsoredCreative:2', false],
      [UNKNOWN_CREATIVE_KEY, 'unknown creative', true],
      ['3', 'Headline', false],
    ]);
  });

  it("puts the cells in the asset's currency, ahead of the metrics' currency", () => {
    const metrics = [metric('spend', '10.00', { currency: 'EUR' })];
    const rows = [{ creative_id: 1, spend: '10.00' }];
    const asset = rowsEvidence(rows, { asset: { currency: 'USD' } }, metrics);
    expect(creativeRows(asset)[0]?.cells.spend?.currency).toBe('USD');
    expect(evidenceCurrency(asset)).toBe('EUR');
    const fromMetrics = rowsEvidence(rows, {}, metrics);
    expect(creativeRows(fromMetrics)[0]?.cells.spend?.currency).toBe('EUR');
    expect(creativeRows(rowsEvidence(rows))[0]?.cells.spend?.currency).toBeNull();
  });

  it('reads count cells from CountValue JSON or a bare count, never inventing a zero', () => {
    const [row] = creativeRows(
      rowsEvidence([
        {
          creative_id: 1,
          leads: 2.5,
          mql: { value: '3' },
          ql: { value: 4, floor: true, unavailable: true, reason: 'QL stage not resolved' },
          meetings_booked: {},
          other_meetings: 'two',
          deals: { value: 1, floor: 'yes' },
        },
      ]),
    );
    expect(row?.cells.linkedin_leads).toMatchObject({
      value: null,
      unavailable: true,
      floor: null,
    });
    expect(row?.cells.mql).toMatchObject({ value: 3, floor: false, unavailable: false });
    expect(row?.cells.ql).toMatchObject({ value: null, floor: null, unavailable: true });
    expect(row?.notes.ql).toBe('QL stage not resolved');
    expect(row?.cells.meetings_booked).toMatchObject({ value: null, unavailable: true });
    expect(row?.cells.other_meetings).toBeNull();
    expect(row?.cells.deals_after_touch).toMatchObject({ value: 1, floor: false });
  });

  it('reads only the backend cost shapes as known, everything else as unavailable', () => {
    const cost = (raw: unknown): Metric | null | undefined =>
      creativeRows(rowsEvidence([{ creative_id: 1, cost_per_ql: raw }]))[0]?.cells.cost_per_ql;

    expect(cost({ value: '370.00', ceiling: true, unavailable: false })).toMatchObject({
      value: '370.00',
      floor: true,
      unavailable: false,
    });
    expect(cost({ value: ' 370.00 ' })).toMatchObject({ value: '370.00', floor: false });
    expect(cost({ value: null, ceiling: false, unavailable: false })).toMatchObject({
      value: EM_DASH,
      floor: false,
      unavailable: false,
    });

    const unknownShapes: readonly unknown[] = [
      { value: '370.00', unavailable: true },
      { value: null, unavailable: false, ceiling: true },
      { value: null },
      { value: 370, unavailable: false },
      { value: 'n/a', unavailable: false },
      { unavailable: false },
    ];
    for (const raw of unknownShapes) {
      expect(cost(raw)).toMatchObject({ value: null, floor: null, unavailable: true });
    }
    expect(cost('370.00')).toBeNull();
    expect(cost(undefined)).toBeNull();
  });

  it("explains a missing value with a cost's note, else a count's reason", () => {
    const [row] = creativeRows(
      rowsEvidence([
        {
          creative_id: 1,
          cost_per_ql: { value: null, unavailable: false, note: 'no QL yet', reason: 'ignored' },
          cost_per_meeting: { value: null, unavailable: true, note: '  ', reason: 'unread' },
          mql: { value: null, unavailable: true, reason: 7 },
        },
      ]),
    );
    expect(row?.notes.cost_per_ql).toBe('no QL yet');
    expect(row?.notes.cost_per_meeting).toBe('unread');
    expect(row?.notes.mql).toBeNull();
    expect(row?.notes.spend).toBeNull();
  });

  it('returns [] for an item without rows', () => {
    expect(creativeRows(pageItem(FIXTURE_ITEM_IDS.k1))).toEqual([]);
    expect(creativeRows(withExtra({ rows: { creative_id: 1 } }))).toEqual([]);
    expect(creativeRows(null)).toEqual([]);
  });
});

describe('whitePaperCostNotes', () => {
  it('reads why the asset costs are "—"', () => {
    expect(whitePaperCostNotes(pageItem(FIXTURE_ITEM_IDS.wUsd))).toEqual({
      cost_per_ql: 'no QL yet',
      cost_per_meeting: 'no meetings yet',
    });
  });

  it('returns null notes for known costs, absent costs and malformed entries', () => {
    const empty = { cost_per_ql: null, cost_per_meeting: null };
    expect(whitePaperCostNotes(pageItem(FIXTURE_ITEM_IDS.wEur))).toEqual(empty);
    expect(whitePaperCostNotes(makeEvidence())).toEqual(empty);
    expect(
      whitePaperCostNotes(
        withExtra({ costs: { cost_per_ql: 'no QL yet', cost_per_meeting: { note: 4 } } }),
      ),
    ).toEqual(empty);
    expect(whitePaperCostNotes(withExtra({ costs: 'no QL yet' }))).toEqual(empty);
  });
});

describe('whitePaperMqlLabel', () => {
  it('reads the stage label each white-paper row recorded', () => {
    const ids = [FIXTURE_ITEM_IDS.wEur, FIXTURE_ITEM_IDS.wEurNoChange, FIXTURE_ITEM_IDS.wUsd];
    for (const id of ids) {
      const item = pageItem(id);
      expect(whitePaperMqlLabel(item)).toBe('First meeting booked');
      expect(whitePaperMqlLabel(item.evidence)).toBe('First meeting booked');
    }
  });

  it('returns null for an absent, null, blank or non-string label', () => {
    expect(whitePaperMqlLabel(withExtra({ mql_label: null }))).toBeNull();
    expect(whitePaperMqlLabel(withExtra({ mql_label: '  ' }))).toBeNull();
    expect(
      whitePaperMqlLabel(withExtra({ mql_label: { label: 'First meeting booked' } })),
    ).toBeNull();
    expect(whitePaperMqlLabel(withExtra({}))).toBeNull();
    expect(whitePaperMqlLabel(withExtra(null))).toBeNull();
    expect(whitePaperMqlLabel(pageItem(FIXTURE_ITEM_IDS.b1))).toBeNull();
    expect(whitePaperMqlLabel(null)).toBeNull();
  });
});

describe('whitePaperMqlMetricLabel', () => {
  it("reads the header the backend recorded on a row's MQL metric", () => {
    expect(whitePaperMqlMetricLabel(pageItem(FIXTURE_ITEM_IDS.wEur))).toBe(
      'MQL (First meeting booked)',
    );
    // An unavailable MQL count keeps its recorded label.
    expect(whitePaperMqlMetricLabel(pageItem(FIXTURE_ITEM_IDS.wUsd).evidence)).toBe(
      'MQL (First meeting booked)',
    );
    expect(whitePaperMqlMetricLabel(withExtra(null, [metric('mql', 3, { label: 'MQL' })]))).toBe(
      'MQL',
    );
  });

  it('reads the first mql metric, as metricByKey does', () => {
    const evidence = withExtra(null, [
      metric('ql', 2, { label: 'Qualified Lead' }),
      metric('mql', 3, { label: 'MQL (First meeting booked)' }),
      metric('mql', 4, { label: 'MQL (Demo held)' }),
    ]);
    expect(whitePaperMqlMetricLabel(evidence)).toBe('MQL (First meeting booked)');
  });

  it('returns null without an mql metric or a recorded label, never the key', () => {
    expect(
      whitePaperMqlMetricLabel(rawEvidence({ metrics: [{ key: 'mql', value: 3 }] })),
    ).toBeNull();
    expect(
      whitePaperMqlMetricLabel(rawEvidence({ metrics: [{ key: 'mql', label: ' ', value: 3 }] })),
    ).toBeNull();
    expect(
      whitePaperMqlMetricLabel(rawEvidence({ metrics: [{ key: 'mql', label: 7, value: 3 }] })),
    ).toBeNull();
    expect(whitePaperMqlMetricLabel(rawEvidence({ metrics: 'mql' }))).toBeNull();
    expect(whitePaperMqlMetricLabel(rawEvidence({ metrics: [null, 'mql'] }))).toBeNull();
    expect(whitePaperMqlMetricLabel(withExtra(null, [metric('ql', 2)]))).toBeNull();
    expect(whitePaperMqlMetricLabel(pageItem(FIXTURE_ITEM_IDS.k1))).toBeNull();
    expect(whitePaperMqlMetricLabel(null)).toBeNull();
  });
});

describe('whitePaperAlreadyBefore', () => {
  const none = { mql: null, ql: null, meetings: null, deals: null };

  it('reads what the causal check excluded, known zeros kept as 0', () => {
    expect(whitePaperAlreadyBefore(pageItem(FIXTURE_ITEM_IDS.wEur))).toEqual({
      mql: 1,
      ql: 0,
      meetings: 0,
      deals: 0,
    });
    const evidence = withExtra({ already_before: { mql: 2, ql: 1, meetings: 1, deals: 3 } });
    expect(whitePaperAlreadyBefore(evidence)).toEqual({ mql: 2, ql: 1, meetings: 1, deals: 3 });
    expect(whitePaperAlreadyBefore({ evidence })).toEqual(whitePaperAlreadyBefore(evidence));
    // A whole number sent as a numeric string still reads.
    expect(whitePaperAlreadyBefore(withExtra({ already_before: { mql: '4' } })).mql).toBe(4);
  });

  it('reads a missing, fractional or non-numeric count as null, never 0', () => {
    expect(
      whitePaperAlreadyBefore(
        withExtra({ already_before: { mql: null, ql: 1.5, meetings: 'two', deals: true } }),
      ),
    ).toEqual(none);
    expect(whitePaperAlreadyBefore(withExtra({ already_before: { ql: 0 } }))).toEqual({
      ...none,
      ql: 0,
    });
    expect(whitePaperAlreadyBefore(withExtra({ already_before: [1, 0, 0, 0] }))).toEqual(none);
    expect(whitePaperAlreadyBefore(withExtra({ already_before: 3 }))).toEqual(none);
    expect(whitePaperAlreadyBefore(withExtra({}))).toEqual(none);
    expect(whitePaperAlreadyBefore(withExtra(null))).toEqual(none);
    expect(whitePaperAlreadyBefore(pageItem(FIXTURE_ITEM_IDS.b1))).toEqual(none);
    expect(whitePaperAlreadyBefore(null)).toEqual(none);
    expect(whitePaperAlreadyBefore(undefined)).toEqual(none);
  });

  it("reads a count as null while the asset's own metric is unavailable", () => {
    // The fixture's USD row has an unavailable MQL count; the backend still records 0 before.
    expect(whitePaperAlreadyBefore(pageItem(FIXTURE_ITEM_IDS.wUsd))).toEqual({
      mql: null,
      ql: 0,
      meetings: 0,
      deals: 0,
    });
    const before = { already_before: { mql: 0, ql: 0, meetings: 0, deals: 0 } };
    const unavailable = { unavailable: true };
    expect(
      whitePaperAlreadyBefore(
        withExtra(before, [
          metric('mql', 3),
          metric('ql', null, unavailable),
          metric('meetings_booked', null),
          metric('deals_after_touch', 2),
        ]),
      ),
    ).toEqual({ mql: 0, ql: null, meetings: null, deals: 0 });
    expect(
      whitePaperAlreadyBefore(withExtra(before, [metric('deals_after_touch', 1, unavailable)])),
    ).toEqual({ mql: 0, ql: 0, meetings: 0, deals: null });
  });
});

describe('whitePaperOrphanDealLabel', () => {
  it("reads the orphan-deal share's label verbatim, the unknown label included", () => {
    const label = '5.7% (2 of 35 deals without a contact)';
    const share = { assessed: 35, orphans: 2, not_assessed: 0, share: '0.057143', unknown: false };
    const known = withExtra({ orphan_deals: { ...share, label, reason: null } });
    expect(whitePaperOrphanDealLabel(known)).toBe(label);
    expect(whitePaperOrphanDealLabel({ evidence: known })).toBe(label);
    expect(
      whitePaperOrphanDealLabel(
        withExtra({
          orphan_deals: {
            assessed: 30,
            orphans: 1,
            not_assessed: 3,
            share: null,
            unknown: true,
            label: 'unknown (3 deals not assessed)',
            reason: '3 deals not assessed',
          },
        }),
      ),
    ).toBe('unknown (3 deals not assessed)');
    // The fixture page's white papers carry the share as `OrphanDealShare.to_json` writes it.
    expect(whitePaperOrphanDealLabel(pageItem(FIXTURE_ITEM_IDS.wEur))).toBe(
      '0% (0 of 4 deals without a contact)',
    );
  });

  it('returns null for an absent, blank or non-string label', () => {
    expect(whitePaperOrphanDealLabel(withExtra({ orphan_deals: { label: '  ' } }))).toBeNull();
    expect(whitePaperOrphanDealLabel(withExtra({ orphan_deals: { label: 0.057 } }))).toBeNull();
    expect(whitePaperOrphanDealLabel(withExtra({ orphan_deals: { share: '0.05' } }))).toBeNull();
    expect(whitePaperOrphanDealLabel(withExtra({ orphan_deals: '5.7%' }))).toBeNull();
    expect(whitePaperOrphanDealLabel(withExtra({}))).toBeNull();
    // A fixture-page item that is no white paper carries no orphan-deal share.
    expect(whitePaperOrphanDealLabel(pageItem(FIXTURE_ITEM_IDS.b1))).toBeNull();
    expect(whitePaperOrphanDealLabel(null)).toBeNull();
  });
});

describe('cohortSummary', () => {
  it("reads each currency cohort's spend total and overlap note", () => {
    expect(cohortSummary(pageItem(FIXTURE_ITEM_IDS.wEur))).toEqual({
      currency: 'EUR',
      spendTotal: '4340.00',
      overlapNote: 'counts overlap across assets',
    });
    expect(cohortSummary(pageItem(FIXTURE_ITEM_IDS.wUsd))).toEqual({
      currency: 'USD',
      spendTotal: '1210.00',
      overlapNote: 'counts overlap across assets',
    });
  });

  it('carries no MQL, QL, meeting or deal total', () => {
    const summary = cohortSummary(pageItem(FIXTURE_ITEM_IDS.wEur));
    expect(Object.keys(summary ?? {})).toEqual(['currency', 'spendTotal', 'overlapNote']);
  });

  it('nulls malformed fields and returns null without a cohort currency', () => {
    const malformed = withExtra({
      cohort: { currency: 'EUR', spend_total: 'n/a', overlap_note: '' },
    });
    expect(cohortSummary(malformed)).toEqual({
      currency: 'EUR',
      spendTotal: null,
      overlapNote: null,
    });
    expect(cohortSummary(withExtra({ cohort: { spend_total: '10.00' } }))).toBeNull();
    expect(cohortSummary(withExtra({ cohort: 'EUR' }))).toBeNull();
    expect(cohortSummary(pageItem(FIXTURE_ITEM_IDS.b1))).toBeNull();
    expect(cohortSummary(null)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Decision prefill
// ---------------------------------------------------------------------------------------------

describe('decisionContextFor', () => {
  it('names C1 and C1b as the pause rules', () => {
    expect(PAUSE_RULE_IDS).toEqual(['C1', 'C1b']);
  });

  it('prefills status paused for C1 and C1b, never an amount', () => {
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.c1))).toStrictEqual({
      prefill: { status: 'paused' },
      currency: 'USD',
    });
    const c1b = makeItem({
      rule_id: 'C1b',
      decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
      evidence: withExtra({ proposed_daily_budget: '10.00' }),
    });
    expect(decisionContextFor(c1b)).toStrictEqual({ prefill: { status: 'paused' } });
  });

  it('prefills the proposed daily budget of a B1 on its own campaign', () => {
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.b1))).toStrictEqual({
      prefill: { daily_budget: '240.00' },
      currency: 'USD',
    });
  });

  it('writes a shared-budget B3 on the budget, listing every member campaign', () => {
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.b3))).toStrictEqual({
      prefill: { daily_budget: '110.00' },
      members: [
        { id: String(FIXTURE_CAMPAIGN_IDS.displayProspecting), name: 'Display Prospecting — US' },
        { id: String(FIXTURE_CAMPAIGN_IDS.displayRetargeting), name: 'Display Retargeting — US' },
      ],
      currency: 'USD',
    });
  });

  it('writes a B1 on a shared budget on the budget subject, with every member listed', () => {
    // A shared-budget B1 as budget.budget_evidence and budget._members_extra freeze it: the
    // proposed amount is a metric and the members sit in extra.members.
    const b1Shared = makeItem({
      id: 9101,
      rule_id: 'B1',
      decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
      evidence: withExtra(
        {
          outcome: 'Scale',
          increase: '30.00',
          members: [
            {
              campaign_id: FIXTURE_CAMPAIGN_IDS.pipelineSearch,
              name: 'Pipeline Search — US',
              member_days: 28,
            },
            {
              campaign_id: FIXTURE_CAMPAIGN_IDS.searchPartners,
              name: 'Search Partners — US',
              member_days: 21,
            },
          ],
        },
        [
          metric('proposed_daily_budget', '180.00', {
            label: 'Proposed daily budget',
            unit: 'currency',
            currency: 'USD',
          }),
        ],
      ),
    });
    const context = decisionContextFor(b1Shared);
    expect(context).toStrictEqual({
      prefill: { daily_budget: '180.00' },
      members: [
        { id: String(FIXTURE_CAMPAIGN_IDS.pipelineSearch), name: 'Pipeline Search — US' },
        { id: String(FIXTURE_CAMPAIGN_IDS.searchPartners), name: 'Search Partners — US' },
      ],
      currency: 'USD',
    });
    expect(context.prefill?.daily_budget).toBe(proposedDailyBudget(b1Shared));
    expect(typeof context.prefill?.daily_budget).toBe('string');
    expect(context.prefill).not.toHaveProperty('status');
  });

  it('prefills a C2 "Scale" from its linked B1 amount, without a currency it cannot name', () => {
    const c2Scale = makeItem({
      rule_id: 'C2',
      decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineSearch },
      evidence: withExtra({ outcome: 'Scale', b1_link: { proposed_daily_budget: '240.00' } }),
    });
    expect(decisionContextFor(c2Scale)).toStrictEqual({ prefill: { daily_budget: '240.00' } });
  });

  it('invents no amount for a subject without a proposal', () => {
    const b2 = makeItem({
      rule_id: 'B2',
      decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
    });
    expect(decisionContextFor(b2)).toStrictEqual({ members: [] });
    const lowerCase = makeItem({
      rule_id: 'c1',
      decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
    });
    expect(decisionContextFor(lowerCase)).toStrictEqual({});
  });

  it('returns {} for an item that offers no decision', () => {
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.c2Hold))).toStrictEqual({});
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.c0))).toStrictEqual({});
    expect(decisionContextFor(pageItem(FIXTURE_ITEM_IDS.k1))).toStrictEqual({});
  });
});

// ---------------------------------------------------------------------------------------------
// The whole default page
// ---------------------------------------------------------------------------------------------

describe('readers over every card of the default page', () => {
  const items = pageItems(makeRecommendationsPage());

  it('find each rule-specific detail on exactly the cards whose rule writes it', () => {
    expect(items).toHaveLength(15);
    for (const item of items) {
      const rule = item.rule_id;
      expect(discrepancyRef(item) !== null, rule).toBe(rule === 'C0');
      expect(adjacentTerms(item).length > 0, rule).toBe(rule === 'K2');
      expect(launchGap(item).kind === 'cluster', rule).toBe(rule === 'C3b');
      expect(funding(item) !== null, rule).toBe(rule === 'B1');
      expect(allocations(item).length > 0, rule).toBe(rule === 'B3');
      expect(variants(item).length > 0, rule).toBe(rule === 'CR3');
      expect(creativeRows(item).length > 0, rule).toBe(rule.startsWith('W'));
      expect(cohortSummary(item) !== null, rule).toBe(rule.startsWith('W'));
      expect(whitePaperMqlLabel(item) !== null, rule).toBe(rule.startsWith('W'));
      expect(sharedMembers(item).length > 0, rule).toBe(item.decision_subject?.type === 'budget');
      expect(Object.keys(decisionContextFor(item)).length === 0, rule).toBe(
        item.decision_subject === null,
      );
    }
  });

  it('read an item and its bare evidence alike', () => {
    for (const item of items) {
      expect(keywordFields(item.evidence, item)).toEqual(keywordFields(item));
      expect(pipelineFields(item.evidence)).toEqual(pipelineFields(item));
      expect(dayCounts(item.evidence)).toEqual(dayCounts(item));
      expect(creativeRows(item.evidence)).toEqual(creativeRows(item));
      expect(proposedDailyBudget(item.evidence)).toBe(proposedDailyBudget(item));
      expect(evidenceCurrency(item.evidence)).toBe(evidenceCurrency(item));
      expect(dependsOn(item.evidence)).toEqual(dependsOn(item));
    }
  });
});
