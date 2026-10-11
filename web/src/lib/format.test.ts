// @vitest-environment node
/**
 * Unit tests for src/lib/format.ts, the SPA's display formatting (AAP 0.5.5 Behaviour).
 *
 * Every branch of the module has a case here: absent and non-numeric inputs, tiny non-zero
 * values, fractional counts, floors that must not double, refused currency codes, invalid
 * display zones and unparsable instants. Expected strings are literal, so a change in rendering
 * fails a test rather than passing silently. The verified ICU outputs quoted in the module's
 * contract are asserted verbatim.
 *
 * Fixtures are typed against `../api/types` with `satisfies`, so a schema change fails the
 * typecheck. The suite mocks nothing but `Intl.NumberFormat.prototype.formatToParts`, in the one
 * money test that stands in for ICU builds emitting another space beside the currency code. The
 * one file it reads is the backend's `decisions.py`, whose ISO 4217 minor-unit table the money
 * functions must mirror. It reads no clock, locale or `TZ`, and no test measures elapsed time:
 * the runner's per-test timeout is the only timing any test meets. It passes unchanged under
 * `TZ=Pacific/Auckland` and `TZ=America/Los_Angeles`.
 */
import { readFileSync } from 'node:fs';

import { describe, expect, it, vi } from 'vitest';

import type {
  EvidenceWindow,
  Metric,
  Sample,
  SourceDay,
  SourceName,
  StatTest,
} from '../api/types';
import * as format from './format';
import {
  DAY_UNITS,
  EM_DASH,
  FIGURE_SEPARATOR,
  LOCALE,
  MONEY_SEPARATOR,
  MONEY_UNITS,
  MULTIPLE_UNITS,
  NOT_APPLICABLE,
  PERCENT_POINT_UNITS,
  P_VALUE_UNITS,
  RATE_UNITS,
  RATIO_UNITS,
  TEST_LABELS,
  TEXT_UNITS,
  UNAVAILABLE,
  boundariesDiffer,
  currencyMinorDigits,
  currencyStep,
  formatCost,
  formatCount,
  formatDateRange,
  formatDays,
  formatDecimal,
  formatExactMoney,
  formatFloor,
  formatInstant,
  formatMetric,
  formatMoney,
  formatPValue,
  formatPercent,
  formatSample,
  formatSourceBoundaries,
  formatSourceDate,
  formatTestLine,
  formatWindowChip,
  isDecimalString,
  joinSentences,
  percentDigits,
  ratioMetricDigits,
  sourceLabel,
} from './format';

/** A full evidence metric with the given value and overrides. */
function metric(value: Metric['value'], overrides: Partial<Metric> = {}): Metric {
  return {
    key: 'k',
    label: 'Label',
    value,
    unit: null,
    currency: null,
    floor: null,
    unavailable: null,
    ...overrides,
  };
}

/**
 * The spaces ICU may emit inside `Intl` output, which format.ts replaces with plain spaces. The
 * only no-break spaces it emits on purpose are those of `FIGURE_SEPARATOR`, inside an "N / M"
 * figure, and `MONEY_SEPARATOR`, between a currency code and its figure; output with neither
 * figure nor amount, such as an instant, holds none.
 */
const SPECIAL_SPACES = /[\u202F\u00A0]/;

/** The backend module whose `minor_unit_exponent` the API validates decided amounts with. */
const DECISIONS_PY = new URL('../../../backend/src/ads_engine/decisions.py', import.meta.url);

/** ISO 4217 currencies without a minor unit, as `decisions.py` lists them, sorted. */
const ISO_ZERO_DIGITS: readonly string[] = [
  'BIF',
  'CLP',
  'DJF',
  'GNF',
  'ISK',
  'JPY',
  'KMF',
  'KRW',
  'PYG',
  'RWF',
  'UGX',
  'UYI',
  'VND',
  'VUV',
  'XAF',
  'XOF',
  'XPF',
];

/** ISO 4217 currencies whose minor unit is a thousandth, as `decisions.py` lists them, sorted. */
const ISO_THREE_DIGITS: readonly string[] = ['BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND'];

/**
 * The sorted codes of one `frozenset` constant of `decisions.py`, such as
 * `_ZERO_EXPONENT_CURRENCIES: Final[frozenset[str]] = frozenset({"BIF", …})`. Fails the test when
 * the constant is not found, so a rename cannot pass silently.
 */
function backendCurrencySet(source: string, name: string): string[] {
  const match = new RegExp(`^${name}\\b[^=\\n]*=\\s*frozenset\\(\\s*\\{([^}]*)\\}`, 'mu').exec(
    source,
  );
  if (match === null) {
    throw new Error(`${name} is not defined in decisions.py`);
  }
  return [...(match[1] ?? '').matchAll(/"([A-Z]{3})"/gu)].map(([, code = '']) => code).sort();
}

/**
 * Minutes since midnight of the 12-hour clock time in a formatted instant, such as
 * `"Oct 6, 2026, 11:22 AM America/New_York"` → 682. Fails the test when no time is present.
 */
function clockMinutes(text: string): number {
  const match = /(\d{1,2}):(\d{2}) (AM|PM)/.exec(text);
  if (match === null) {
    throw new Error(`no clock time in ${JSON.stringify(text)}`);
  }
  const [, hours = '', minutes = '', meridiem = ''] = match;
  return ((Number(hours) % 12) + (meridiem === 'PM' ? 12 : 0)) * 60 + Number(minutes);
}

/**
 * Each value beside the label an `it.each` title shows for it through `%s`: the value as `%j`
 * prints it, except that a number is written as JavaScript writes it. `%j` prints `NaN`,
 * `Infinity` and `-Infinity` all as `null`, and `-0` as `0`, so those cases would share a title.
 */
function labelled<T>(values: readonly T[]): [label: string, value: T][] {
  return values.map((value) => {
    if (typeof value === 'number') {
      return [Object.is(value, -0) ? '-0' : String(value), value];
    }
    return [value === undefined ? 'undefined' : JSON.stringify(value), value];
  });
}

describe('constants', () => {
  it('fixes the locale and the placeholder texts', () => {
    expect(LOCALE).toBe('en');
    expect(UNAVAILABLE).toBe('unavailable');
    expect(EM_DASH).toBe('\u2014');
    expect(NOT_APPLICABLE).toBe('n/a');
    expect(FIGURE_SEPARATOR).toBe('\u00A0/\u00A0');
    expect(MONEY_SEPARATOR).toBe('\u00a0');
  });

  it('emits no-break spaces only as FIGURE_SEPARATOR and MONEY_SEPARATOR', () => {
    // A by-half money pair, as the Keywords review card composes it, and a sample line.
    const pair = `${formatMoney('2.1', 'USD')}${FIGURE_SEPARATOR}${formatMoney('2.52', 'USD')}`;
    expect(pair).toBe('USD\u00a02.10\u00A0/\u00A0USD\u00a02.52');
    const sample = formatSample({ observed: 3100, required: 4200, unit: 'form opens' });
    const parts = [
      ...pair.split(FIGURE_SEPARATOR).flatMap((amount) => amount.split(MONEY_SEPARATOR)),
      ...sample.split(FIGURE_SEPARATOR),
    ];
    expect(parts).toEqual(['USD', '2.10', 'USD', '2.52', '3,100', '4,200 form opens']);
    expect(parts.filter((part) => SPECIAL_SPACES.test(part))).toEqual([]);
  });

  it('exports the unit vocabularies', () => {
    expect([...MONEY_UNITS]).toEqual(['money', 'currency', 'amount', 'cost', 'spend']);
    expect([...RATIO_UNITS]).toEqual(['ratio', 'share', 'utilization', 'fraction']);
    expect([...RATE_UNITS]).toEqual(['rate']);
    expect([...PERCENT_POINT_UNITS]).toEqual(['percent', 'pct', '%']);
    expect([...DAY_UNITS]).toEqual(['days', 'day']);
    expect([...P_VALUE_UNITS]).toEqual(['p', 'p_value']);
    expect([...MULTIPLE_UNITS]).toEqual(['x', 'multiple', 'ratio_x']);
    expect([...TEXT_UNITS]).toEqual(['text']);
  });

  it('has no default export', () => {
    expect('default' in format).toBe(false);
  });

  it('exports nothing that sums, totals, converts or aggregates across currencies', async () => {
    const exported = Object.keys(await import('./format')).sort();
    expect(exported).toEqual(
      [
        'DAY_UNITS',
        'EM_DASH',
        'FIGURE_SEPARATOR',
        'LOCALE',
        'MONEY_SEPARATOR',
        'MONEY_UNITS',
        'MULTIPLE_UNITS',
        'NOT_APPLICABLE',
        'PERCENT_POINT_UNITS',
        'P_VALUE_UNITS',
        'RATE_UNITS',
        'RATIO_UNITS',
        'TEST_LABELS',
        'TEXT_UNITS',
        'UNAVAILABLE',
        'boundariesDiffer',
        'currencyMinorDigits',
        'currencyStep',
        'formatCost',
        'formatCount',
        'formatDateRange',
        'formatDays',
        'formatDecimal',
        'formatExactMoney',
        'formatFloor',
        'formatInstant',
        'formatMetric',
        'formatMoney',
        'formatPValue',
        'formatPercent',
        'formatSample',
        'formatSourceBoundaries',
        'formatSourceDate',
        'formatTestLine',
        'formatWindowChip',
        'isDecimalString',
        'joinSentences',
        'percentDigits',
        'ratioMetricDigits',
        'sourceLabel',
      ].sort(),
    );
    expect(exported.filter((name) => /sum|total|convert|aggregate/i.test(name))).toEqual([]);
  });
});

describe('isDecimalString', () => {
  it.each([
    '12',
    '-0.5',
    '.5',
    '1E+2',
    ' 3.4 ',
    '120.500000',
    ' -3 ',
    '1e-7',
    '+5',
    '5.',
    '0',
    '-0.00',
    '2E+3',
  ])('accepts %j', (value) => {
    expect(isDecimalString(value)).toBe(true);
  });

  it.each([
    '',
    '  ',
    '1,000',
    'abc',
    'NaN',
    'Infinity',
    'n/a',
    '1,234',
    '0x10',
    '--1',
    '1.2.3',
    '≥ 12',
    'e5',
  ])('rejects %j', (value) => {
    expect(isDecimalString(value)).toBe(false);
  });
});

describe('sourceLabel', () => {
  it('names each source', () => {
    expect(sourceLabel('google')).toBe('Google');
    expect(sourceLabel('linkedin')).toBe('LinkedIn');
    expect(sourceLabel('hubspot')).toBe('HubSpot');
  });

  it('returns an unknown source unchanged, including prototype keys', () => {
    expect(sourceLabel('bing' as SourceName)).toBe('bing');
    expect(sourceLabel('constructor' as SourceName)).toBe('constructor');
  });
});

describe('formatMoney', () => {
  it('formats the verified outputs exactly', () => {
    expect(formatMoney('1234.567891', 'EUR')).toBe('EUR\u00a01,234.57');
    expect(formatMoney('40', 'GBP')).toBe('GBP\u00a040.00');
    expect(formatMoney('1234.5', 'JPY')).toBe('JPY\u00a01,235');
    expect(formatMoney('-40', 'EUR')).toBe('EUR\u00a0-40.00');
  });

  it('rounds the exact decimal string, never a float', () => {
    expect(formatMoney('999999999999.994999', 'USD')).toBe('USD\u00a0999,999,999,999.99');
    expect(formatMoney('123456789012.344999', 'USD')).toBe('USD\u00a0123,456,789,012.34');
    expect(formatMoney('120.500000', 'USD')).toBe('USD\u00a0120.50');
  });

  it('differs from the Number() round trip, which loses the precision', () => {
    // The nearest double's shortest decimal is 999999999999.995 (and 123456789012.345), which
    // Intl rounds half up; the string keeps the trailing 4999 and rounds down.
    expect(formatMoney(Number('999999999999.994999'), 'USD')).toBe('USD\u00a01,000,000,000,000.00');
    expect(formatMoney(Number('123456789012.344999'), 'USD')).toBe('USD\u00a0123,456,789,012.35');
    expect(formatMoney('999999999999.994999', 'USD')).not.toBe(
      formatMoney(Number('999999999999.994999'), 'USD'),
    );
  });

  it('formats each currency independently, one amount per call', () => {
    expect(formatMoney('40', 'GBP')).toBe('GBP\u00a040.00');
    expect(formatMoney('1234.5', 'JPY')).toBe('JPY\u00a01,235');
    expect(formatMoney('40', 'EUR')).toBe('EUR\u00a040.00');
    // Formatting one currency leaves the next call's currency untouched.
    expect(formatMoney('40', 'GBP')).toBe('GBP\u00a040.00');
  });

  it('formats numbers and lower-case codes', () => {
    expect(formatMoney(12.5, 'usd')).toBe('USD\u00a012.50');
    expect(formatMoney(' 12.5 ', ' USD ')).toBe('USD\u00a012.50');
  });

  it('never shows a sign on an amount that rounds to zero', () => {
    expect(formatMoney('-0.001', 'USD')).toBe('USD\u00a00.00');
  });

  it.each(labelled([null, undefined, '', '  ', 'abc', Number.NaN, Number.POSITIVE_INFINITY]))(
    'renders %s as unavailable',
    (_, value) => {
      expect(formatMoney(value, 'USD')).toBe(UNAVAILABLE);
      expect(formatMoney(value, 'USD')).not.toBe('0');
    },
  );

  it('implies no currency when the code is missing', () => {
    expect(formatMoney('12.5', null)).toBe('12.5');
    expect(formatMoney('40.5', null)).toBe('40.5');
    expect(formatMoney('40.5', undefined)).toBe('40.5');
    expect(formatMoney('40.5', '  ')).toBe('40.5');
  });

  it('falls back to the decimal and the code for a code Intl refuses', () => {
    expect(formatMoney('5', 'EURO')).toBe('5\u00a0EURO');
    // A second call reads the cached refusal.
    expect(formatMoney('5.256', 'EURO')).toBe('5.26\u00a0EURO');
  });

  it('joins a well-formed unknown code to its figure with the no-break separator only', () => {
    const text = formatMoney('5', 'XYZ');
    expect(text).toBe('XYZ\u00a05.00');
    expect(/[\u202F ]/u.test(text)).toBe(false);
  });

  it('words an amount as the backend reason texts do: the ISO code, a space, the figure', () => {
    // budget.py, campaigns.py, keywords.py and white_papers.py render "Spent USD 517.85", with a
    // plain space where the UI puts the no-break separator; the two render alike.
    expect(formatMoney('517.850000', 'USD')).toBe('USD\u00a0517.85');
    expect(formatMoney('1400', 'USD')).toBe('USD\u00a01,400.00');
    expect(formatMoney('1.5', 'KWD')).toBe('KWD\u00a01.500');
    expect(formatMoney('517.85', 'USD').replace(MONEY_SEPARATOR, ' ')).toBe('USD 517.85');
  });

  it.each([
    ['a no-break space', '\u00a0'],
    ['a narrow no-break space', '\u202f'],
    ['a plain space', ' '],
    ['no space', null],
  ])('joins code and figure with MONEY_SEPARATOR when ICU emits %s there', (_, space) => {
    const separator: Intl.NumberFormatPart[] =
      space === null ? [] : [{ type: 'literal', value: space }];
    const parts: Intl.NumberFormatPart[] = [
      { type: 'minusSign', value: '-' },
      { type: 'currency', value: 'USD' },
      ...separator,
      { type: 'integer', value: '1' },
      { type: 'group', value: ',' },
      { type: 'integer', value: '234' },
      { type: 'decimal', value: '.' },
      { type: 'fraction', value: '50' },
    ];
    const formatToParts = vi
      .spyOn(Intl.NumberFormat.prototype, 'formatToParts')
      .mockReturnValue(parts);
    try {
      expect(formatMoney('-1234.5', 'USD')).toBe('USD\u00a0-1,234.50');
      expect(formatCost('-2469', 2, 'USD')).toBe('USD\u00a0-1,234.50');
    } finally {
      formatToParts.mockRestore();
    }
    expect(formatMoney('-1234.5', 'USD')).toBe('USD\u00a0-1,234.50');
  });
});

describe('formatExactMoney', () => {
  it('shows at least the minor digits and every significant digit up to one micro', () => {
    // The backend's _format_amount in analysis/discrepancy.py renders the same strings, with a
    // plain space where these have the no-break separator.
    expect(formatExactMoney('200.000001', 'USD')).toBe('USD\u00a0200.000001');
    expect(formatExactMoney('50.000000', 'USD')).toBe('USD\u00a050.00');
    expect(formatExactMoney('100', 'USD')).toBe('USD\u00a0100.00');
    expect(formatExactMoney('1500.5', 'USD')).toBe('USD\u00a01,500.50');
    expect(formatExactMoney('12.345678', 'USD')).toBe('USD\u00a012.345678');
    expect(formatExactMoney('5000', 'JPY')).toBe('JPY\u00a05,000');
    expect(formatExactMoney('5000.5', 'JPY')).toBe('JPY\u00a05,000.5');
    expect(formatExactMoney('1.5', 'KWD')).toBe('KWD\u00a01.500');
    expect(formatExactMoney('2.5', 'JPY')).toBe('JPY\u00a02.5');
  });

  it('keeps a one-micro difference visible where formatMoney rounds it away', () => {
    expect(formatMoney('200.000001', 'USD')).toBe(formatMoney('200.000000', 'USD'));
    expect(formatExactMoney('200.000001', 'USD')).not.toBe(
      formatExactMoney('200.000000', 'USD'),
    );
  });

  it('rounds a digit beyond the sixth place half to even, as the backend Decimal does', () => {
    expect(formatExactMoney('1.1234567', 'USD')).toBe('USD\u00a01.123457');
    expect(formatExactMoney('1.1234565', 'USD')).toBe('USD\u00a01.123456');
    expect(formatExactMoney('1.1234575', 'USD')).toBe('USD\u00a01.123458');
  });

  it('formats the exact decimal string, never a float', () => {
    expect(formatExactMoney('999999999999.994999', 'USD')).toBe('USD\u00a0999,999,999,999.994999');
    expect(formatExactMoney(' 50.00 ', ' usd ')).toBe('USD\u00a050.00');
    expect(formatExactMoney(12.5, 'EUR')).toBe('EUR\u00a012.50');
    expect(formatExactMoney('1.5e3', 'EUR')).toBe('EUR\u00a01,500.00');
  });

  it('prints a code Intl refuses, at two minor digits, and no code when there is none', () => {
    expect(formatExactMoney('5', 'EURO')).toBe('EURO\u00a05.00');
    expect(formatExactMoney('5', 'XYZ')).toBe('XYZ\u00a05.00');
    expect(formatExactMoney('40.5', null)).toBe('40.50');
    expect(formatExactMoney('7.000001', undefined)).toBe('7.000001');
    expect(formatExactMoney('40.5', '  ')).toBe('40.50');
  });

  it('joins code and figure with the no-break separator, with no minus sign on a zero', () => {
    expect(formatExactMoney('1234.5', 'USD')).toBe('USD\u00a01,234.50');
    expect(formatExactMoney('-0.0000001', 'USD')).toBe('USD\u00a00.000000');
    expect(formatExactMoney('-40', 'USD')).toBe('USD\u00a0-40.00');
  });

  it.each(
    labelled([null, undefined, '', '  ', 'abc', Number.NaN, Number.POSITIVE_INFINITY, '1e400']),
  )('renders %s as unavailable', (_, value) => {
    expect(formatExactMoney(value, 'USD')).toBe(UNAVAILABLE);
  });
});

describe('currencyMinorDigits and currencyStep', () => {
  it.each([
    ['USD', 2, '0.01'],
    ['EUR', 2, '0.01'],
    ['JPY', 0, '1'],
    ['KWD', 3, '0.001'],
    ['BHD', 3, '0.001'],
    // ISO 4217 and the backend give IQD 3 places, AFN and ALL 2; CLDR, and so Intl, gives 0.
    ['IQD', 3, '0.001'],
    ['AFN', 2, '0.01'],
    ['ALL', 2, '0.01'],
    // A well-formed unknown code has the default two, as minor_unit_exponent gives it.
    ['XYZ', 2, '0.01'],
    // Trimmed and compared case-insensitively, as the backend compares it.
    [' iqd ', 3, '0.001'],
    ['jpy', 0, '1'],
  ])('%j has %i minor digits and step %s', (code, digits, step) => {
    expect(currencyMinorDigits(code)).toBe(digits);
    expect(currencyStep(code)).toBe(step);
  });

  it.each(['EURO', '', '  ', null, undefined])('defaults %j to 2 digits', (code) => {
    expect(currencyMinorDigits(code)).toBe(2);
    expect(currencyStep(code)).toBe('0.01');
  });

  it('pins the two ISO 4217 sets of the backend minor_unit_exponent', () => {
    // Read from backend/src/ads_engine/decisions.py, the table the API validates decided amounts
    // against: a currency added to or dropped from either side fails here.
    const source = readFileSync(DECISIONS_PY, 'utf8');
    expect(backendCurrencySet(source, '_ZERO_EXPONENT_CURRENCIES')).toEqual(ISO_ZERO_DIGITS);
    expect(backendCurrencySet(source, '_THREE_EXPONENT_CURRENCIES')).toEqual(ISO_THREE_DIGITS);
  });

  it('gives every currency Intl knows the digits of the backend table', () => {
    const zero = new Set(ISO_ZERO_DIGITS);
    const three = new Set(ISO_THREE_DIGITS);
    const codes = new Set([...Intl.supportedValuesOf('currency'), ...zero, ...three]);
    for (const code of codes) {
      const expected = zero.has(code) ? 0 : three.has(code) ? 3 : 2;
      expect([code, currencyMinorDigits(code)]).toEqual([code, expected]);
      expect([code, currencyMinorDigits(code.toLowerCase())]).toEqual([code, expected]);
    }
  });
});

describe('money parity with the backend reason and C0 texts', () => {
  // The same literal strings, with a plain space where these have MONEY_SEPARATOR, are asserted
  // by backend/tests/unit/test_analysis_money_text.py (ROUNDED_PARITY and GROUPED_PARITY:
  // white_papers, campaigns, budget and keywords) and test_analysis_discrepancy.py
  // (MONEY_PARITY: render_value). Change both sides together.
  it.each([
    ['517.85', 'USD', 'USD\u00a0517.85'],
    ['40', 'EUR', 'EUR\u00a040.00'],
    ['24.6', 'JPY', 'JPY\u00a025'],
    ['2.3456', 'KWD', 'KWD\u00a02.346'],
    ['50', 'IQD', 'IQD\u00a050.000'],
    ['50.00', 'AFN', 'AFN\u00a050.00'],
    ['12.3456', 'ALL', 'ALL\u00a012.35'],
    ['5', 'XYZ', 'XYZ\u00a05.00'],
    // Ties round half up, away from zero, as decisions.round_to_minor_unit rounds them.
    ['89.225000', 'USD', 'USD\u00a089.23'],
    ['-89.225', 'USD', 'USD\u00a0-89.23'],
    ['1400', 'USD', 'USD\u00a01,400.00'],
    ['2766', 'USD', 'USD\u00a02,766.00'],
    ['1234.5', 'IQD', 'IQD\u00a01,234.500'],
    ['5000', 'JPY', 'JPY\u00a05,000'],
  ])('formatMoney(%j, %j) is %j, as the reason texts word it', (amount, code, expected) => {
    expect(formatMoney(amount, code)).toBe(expected);
    expect(formatMetric(metric(amount, { unit: 'currency', currency: code }))).toBe(expected);
  });

  it('rounds a tie half away from zero at every minor-unit width, never half to even', () => {
    // The backend's test_white_paper_money_rounds_half_up_at_the_minor_unit_as_campaigns_and_
    // budget_do pins the same figures for white_papers, campaigns and budget.
    expect(formatMoney('0.125', 'USD')).toBe('USD\u00a00.13');
    expect(formatMoney('0.0125', 'IQD')).toBe('IQD\u00a00.013');
    expect(formatMoney('2.5', 'JPY')).toBe('JPY\u00a03');
    expect(formatMoney('-2.5', 'JPY')).toBe('JPY\u00a0-3');
    expect(formatCost('356.9', 4, 'USD')).toBe('USD\u00a089.23');
  });

  it.each([
    ['400.000000', 'USD', 'USD\u00a0400.00'],
    ['50.00', 'EUR', 'EUR\u00a050.00'],
    ['5000', 'JPY', 'JPY\u00a05,000'],
    ['5000.5', 'JPY', 'JPY\u00a05,000.5'],
    ['1.5', 'KWD', 'KWD\u00a01.500'],
    ['50.00', 'IQD', 'IQD\u00a050.000'],
    ['1234.5', 'IQD', 'IQD\u00a01,234.500'],
    ['50.000001', 'IQD', 'IQD\u00a050.000001'],
    ['50.00', 'AFN', 'AFN\u00a050.00'],
    ['50', 'ALL', 'ALL\u00a050.00'],
    ['5', 'XYZ', 'XYZ\u00a05.00'],
  ])('formatExactMoney(%j, %j) is %j, as render_value words it', (amount, code, expected) => {
    expect(formatExactMoney(amount, code)).toBe(expected);
    expect(formatExactMoney(amount, ` ${code.toLowerCase()} `)).toBe(expected);
  });

  it('rounds a cost once at the ISO minor unit of its currency', () => {
    expect(formatCost('100', 3, 'IQD')).toBe('IQD\u00a033.333');
    expect(formatCost('100', 3, 'AFN')).toBe('AFN\u00a033.33');
    expect(formatCost('100', 3, 'JPY')).toBe('JPY\u00a033');
  });
});

describe('formatDecimal', () => {
  it('formats with grouping and no padding', () => {
    expect(formatDecimal(0.3)).toBe('0.3');
    expect(formatDecimal(2.456)).toBe('2.46');
    expect(formatDecimal(12345.6)).toBe('12,345.6');
    expect(formatDecimal('12345.600000')).toBe('12,345.6');
    expect(formatDecimal(5)).toBe('5');
  });

  it('renders a tiny non-zero value as a bound, never as 0', () => {
    expect(formatDecimal(0.004)).toBe('< 0.01');
    expect(formatDecimal(-0.004)).toBe('> -0.01');
    expect(formatDecimal('0.004')).toBe('< 0.01');
    expect(formatDecimal('-0.004')).toBe('> -0.01');
    expect(formatDecimal('1e-7')).toBe('< 0.01');
    expect(formatDecimal(0.0004, 3)).toBe('< 0.001');
    expect(formatDecimal(0.3, 0)).toBe('< 1');
  });

  it('renders zero without a sign', () => {
    expect(formatDecimal(0)).toBe('0');
    expect(formatDecimal(-0)).toBe('0');
    expect(formatDecimal('0.000')).toBe('0');
    expect(formatDecimal('-0.00')).toBe('0');
    expect(formatDecimal('0e5')).toBe('0');
  });

  it('clamps the digit count', () => {
    expect(formatDecimal(1.23456, 2.7)).toBe('1.23');
    expect(formatDecimal(1.5, -1)).toBe('2');
    expect(formatDecimal(1.5, Number.NaN)).toBe('2');
    expect(formatDecimal('0.123456789012345678901234', 25)).toBe('0.1234567890123456789');
  });

  it.each(labelled([null, undefined, '', 'n/a', Number.NaN, Number.NEGATIVE_INFINITY]))(
    'renders %s as unavailable',
    (_, value) => {
      expect(formatDecimal(value)).toBe(UNAVAILABLE);
    },
  );
});

describe('formatCount', () => {
  it('formats whole values with grouping', () => {
    expect(formatCount(1234)).toBe('1,234');
    expect(formatCount('1234')).toBe('1,234');
    expect(formatCount('9007199254740993')).toBe('9,007,199,254,740,993');
    expect(formatCount('1.000')).toBe('1');
    expect(formatCount('1e3')).toBe('1,000');
    expect(formatCount('1.5e1')).toBe('15');
    expect(formatCount('-1.20e2')).toBe('-120');
    expect(formatCount('9007199254740993.0')).toBe('9,007,199,254,740,993');
    expect(formatCount(0)).toBe('0');
    expect(formatCount('-0')).toBe('0');
  });

  it('never rounds a fractional value to an integer', () => {
    expect(formatCount(0.3)).toBe('0.3');
    expect(formatCount('0.3')).toBe('0.3');
    expect(formatCount('2.456')).toBe('2.46');
    expect(formatCount('1e-1')).toBe('0.1');
    expect(formatCount(0.004)).toBe('< 0.01');
    expect(formatCount(-0.004)).toBe('> -0.01');
    expect(formatCount(0.999)).toBe('1.00');
    expect(formatCount('1234.999')).toBe('1,235.00');
    expect(formatCount('1e-1000000')).toBe('< 0.01');
  });

  it.each(labelled([null, undefined, '', 'abc', Number.NaN]))(
    'renders %s as unavailable',
    (_, value) => {
      expect(formatCount(value)).toBe(UNAVAILABLE);
    },
  );
});

describe('formatPercent', () => {
  it('formats a 0–1 ratio', () => {
    expect(formatPercent(0.123)).toBe('12.3%');
    expect(formatPercent('0.5')).toBe('50%');
    expect(formatPercent(1.4)).toBe('140%');
    expect(formatPercent('0.0909')).toBe('9.1%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(0.12345, 2)).toBe('12.35%');
  });

  it('renders a tiny non-zero ratio as a bound', () => {
    expect(formatPercent(0.0002)).toBe('< 0.1%');
    expect(formatPercent(-0.0002)).toBe('> -0.1%');
    expect(formatPercent(0.004, 0)).toBe('< 1%');
  });

  it.each(labelled([null, undefined, '', 'n/a', Number.NaN]))(
    'renders %s as unavailable',
    (_, value) => {
      expect(formatPercent(value)).toBe(UNAVAILABLE);
    },
  );

  it('shows exactly `digits` fraction digits when fixed, trailing zeros included', () => {
    expect(formatPercent('0.008', 2, { fixed: true })).toBe('0.80%');
    expect(formatPercent('0.000698', 3, { fixed: true })).toBe('0.070%');
    expect(formatPercent('0.5', 1, { fixed: true })).toBe('50.0%');
    expect(formatPercent('0.01', 2, { fixed: true })).toBe('1.00%');
    expect(formatPercent(0, 2, { fixed: true })).toBe('0.00%');
    expect(formatPercent('0.12345', 2, { fixed: true })).toBe('12.35%');
  });

  it('keeps the unpadded default when not fixed', () => {
    expect(formatPercent('0.5', 1, {})).toBe('50%');
    expect(formatPercent('0.5', 1, { fixed: false })).toBe('50%');
    expect(formatPercent('0.008', 2)).toBe('0.8%');
  });

  it('renders a tiny non-zero ratio as a bound at the fixed digits', () => {
    expect(formatPercent(0.000004, 3, { fixed: true })).toBe('< 0.001%');
    expect(formatPercent('-0.000004', 3, { fixed: true })).toBe('> -0.001%');
    expect(formatPercent('0.0002', 1, { fixed: true })).toBe('< 0.1%');
    expect(formatPercent('n/a', 2, { fixed: true })).toBe(UNAVAILABLE);
  });
});

describe('percentDigits', () => {
  /** Each ratio at the digits `percentDigits` picks for the set, fixed. */
  function rendered(ratios: readonly string[], min?: number): string[] {
    const digits = percentDigits(ratios, min === undefined ? {} : { min });
    return ratios.map((ratio) => formatPercent(ratio, digits, { fixed: true }));
  }

  it('keeps CR1 lead rates of 0.000722 and 0.000698 apart', () => {
    expect(percentDigits(['0.000722', '0.000698'])).toBe(3);
    expect(rendered(['0.000722', '0.000698'])).toEqual(['0.072%', '0.070%']);
  });

  it('keeps a CR3 click rate apart from its baseline, and an interval’s bounds apart', () => {
    expect(rendered(['0.007501', '0.008120'])).toEqual(['0.75%', '0.81%']);
    expect(rendered(['0.003937', '0.004278'])).toEqual(['0.39%', '0.43%']);
    expect(percentDigits(['0.007501', '0.008120', '0.003937', '0.004107', '0.004278'])).toBe(2);
  });

  it('gives a variants column one precision, at least two places', () => {
    expect(rendered(['0.008', '0.0058', '0.1239', '0.01'], 2)).toEqual([
      '0.80%',
      '0.58%',
      '12.39%',
      '1.00%',
    ]);
  });

  it('adds digits until differing values read apart, up to the recorded precision', () => {
    expect(rendered(['0.007994', '0.008014'], 2)).toEqual(['0.799%', '0.801%']);
    expect(rendered(['0.004377', '0.004381'], 2)).toEqual(['0.4377%', '0.4381%']);
    // Values recorded beyond six places can coincide at every count: the most, 4, is returned.
    expect(percentDigits(['0.33333301', '0.33333302'])).toBe(4);
    expect(percentDigits(['0.33333301', '0.33333302'], { max: 2 })).toBe(2);
  });

  it('counts equal values once, whatever their form', () => {
    expect(percentDigits(['0.008004', '0.0080040', '0.008004'])).toBe(2);
    expect(percentDigits([0.08, '0.0800', '.08'])).toBe(1);
  });

  it('asks two significant digits of every non-zero ratio, but none of zero', () => {
    expect(percentDigits(['0', '0.5'])).toBe(1);
    expect(percentDigits(['0.0000001'])).toBe(4);
    expect(percentDigits(['1.15', '0.92'])).toBe(1);
    expect(percentDigits(['-0.000722', '-0.000698'])).toBe(3);
  });

  it('ignores absent and non-numeric inputs', () => {
    expect(percentDigits([])).toBe(1);
    expect(percentDigits([null, undefined, '', 'n/a', Number.NaN, '0.5'])).toBe(1);
    expect(percentDigits([null, '0.000722', 'not estimable', '0.000698'])).toBe(3);
  });

  it('clamps min and max, and never returns less than min', () => {
    expect(percentDigits(['0.5'], { min: 0 })).toBe(0);
    expect(percentDigits(['0.5'], { min: 3 })).toBe(3);
    expect(percentDigits(['0.000722', '0.000698'], { min: 3, max: 1 })).toBe(3);
    expect(percentDigits(['0.5'], { min: Number.NaN })).toBe(0);
    expect(percentDigits(['0.5'], { min: 99 })).toBe(20);
  });
});

describe('formatCost', () => {
  it.each(labelled([0, -0, '0', '0.000', '-0.00']))(
    'renders an em dash for the known zero %s',
    (_, zero) => {
      expect(formatCost('300', zero, 'USD')).toBe(EM_DASH);
    },
  );

  it('renders a zero denominator as a dash, never a finite cost or a ceiling', () => {
    expect(formatCost('500', 0, 'USD')).toBe('—');
    expect(formatCost('500', 0, 'USD', 'no QL yet')).toBe('— (no QL yet)');
    expect(formatCost('500', 0, 'USD')).not.toMatch(/\d/);
    expect(formatCost('500', 4, 'USD')).toBe('USD\u00a0125.00');
  });

  // An absent denominator is unknown evidence, not a known zero: AAP 0.5.4.7 reserves "—" for
  // a cost whose known denominator is 0, and AAP 0.11.1 renders absent values "unavailable".
  // The module follows the AAP here, so a null or undefined denominator stays "unavailable"
  // rather than the "—" the test plan's wording suggests.
  it.each([null, undefined])(
    'keeps the absent denominator %j apart from a known zero',
    (absent) => {
      expect(formatCost('500', absent, 'USD')).toBe(UNAVAILABLE);
      expect(formatCost('500', absent, 'USD')).not.toBe(formatCost('500', 0, 'USD'));
    },
  );

  it.each(
    labelled([
      null,
      undefined,
      '',
      'abc',
      Number.NaN,
      Number.POSITIVE_INFINITY,
      Number.NEGATIVE_INFINITY,
      '1e400',
      -1,
      '-0.5',
    ]),
  )('renders the absent, unusable or negative denominator %s as unavailable', (_, denominator) => {
    expect(formatCost('300', denominator, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('300', denominator, 'USD', 'no QL yet')).toBe(UNAVAILABLE);
  });

  it('names the reason after the dash', () => {
    expect(formatCost('300', 0, 'USD', 'no QL yet')).toBe('— (no QL yet)');
    expect(formatCost('300', 0, 'USD', 'no meetings yet')).toBe('— (no meetings yet)');
    expect(formatCost('300', 0, 'USD', '  ')).toBe(EM_DASH);
    expect(formatCost(null, 0, 'USD', 'no QL yet')).toBe('— (no QL yet)');
  });

  it('divides spend by the denominator in the currency', () => {
    expect(formatCost('300', 4, 'USD')).toBe('USD\u00a075.00');
    expect(formatCost('300.000000', '3', 'EUR')).toBe('EUR\u00a0100.00');
    expect(formatCost('1000', 3, 'JPY')).toBe('JPY\u00a0333');
    expect(formatCost('10', 4, null)).toBe('2.5');
  });

  it('divides exactly, never through a binary float', () => {
    // A Number route renders USD 1,000,000,000,000.00 and USD 0.02 for the first two.
    expect(formatCost('999999999999.994999', 1, 'USD')).toBe('USD\u00a0999,999,999,999.99');
    expect(formatCost('0.075', 3, 'USD')).toBe('USD\u00a00.03');
    expect(formatCost('-0.075', 3, 'USD')).toBe('USD\u00a0-0.03');
    expect(formatCost('1.005', 1, 'USD')).toBe('USD\u00a01.01');
    expect(formatCost('1', '0.3', 'USD')).toBe('USD\u00a03.33');
    expect(formatCost('-300', 4, 'USD')).toBe('USD\u00a0-75.00');
    expect(formatCost('0', 3, 'USD')).toBe('USD\u00a00.00');
  });

  it('reads exponent forms and numbers as their exact decimals', () => {
    expect(formatCost('1.5e3', '1e1', 'USD')).toBe('USD\u00a0150.00');
    expect(formatCost('3e-2', '1.2E1', null)).toBe('< 0.01');
    expect(formatCost(1.005, 1, 'USD')).toBe('USD\u00a01.01');
    expect(formatCost(300.5, 2, 'USD')).toBe('USD\u00a0150.25');
  });

  it('rounds once at the currency minor digits', () => {
    expect(formatCost('2', '3', 'KWD')).toBe('KWD\u00a00.667');
    expect(formatCost('1', 3, 'EURO')).toBe('0.33\u00a0EURO');
    expect(formatCost('0.01', 3, null)).toBe('< 0.01');
    expect(formatCost('-0.01', 3, null)).toBe('> -0.01');
  });

  it('renders an out-of-range operand or quotient as unavailable', () => {
    // format.ts checks each operand's places against MAX_EXACT_PLACE on its digit text before
    // any BigInt is built, so an out-of-range operand is refused after a scan and a quotient
    // is only ever divided from in-range operands.
    expect(formatCost('1e999999999', 3, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('300', '1e999999999', 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('1e-999999999', 3, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('1e300', '1e-10', 'USD')).toBe(UNAVAILABLE);
    expect(formatCost(Number.MAX_VALUE, 1, 'USD')).toBe(UNAVAILABLE);
  });

  it('renders unformattable spend as unavailable', () => {
    expect(formatCost(null, 3, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('n/a', 3, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost(Number.NaN, 3, 'USD')).toBe(UNAVAILABLE);
  });
});

describe('formatFloor', () => {
  it('marks counts and costs', () => {
    expect(formatFloor('12', 'min')).toBe('≥ 12');
    expect(formatFloor(formatMoney('40', 'EUR'), 'max')).toBe('≤ EUR\u00a040.00');
    expect(formatFloor('USD\u00a075.00', 'max')).toBe('≤ USD\u00a075.00');
    expect(formatFloor(' 12 ', 'min')).toBe('≥ 12');
    expect(formatFloor('XYZ\u00a05.00', 'max')).toBe('≤ XYZ\u00a05.00');
  });

  it('never doubles a prefix the text already carries', () => {
    expect(formatFloor('≥ 12', 'min')).toBe('≥ 12');
    expect(formatFloor('≤ USD\u00a075.00', 'max')).toBe('≤ USD\u00a075.00');
    expect(formatFloor('≥ 12', 'max')).toBe('≥ 12');
    expect(formatFloor('< 0.01', 'min')).toBe('< 0.01');
    expect(formatFloor('> -0.01', 'min')).toBe('> -0.01');
    expect(formatFloor('≈ 4', 'min')).toBe('≈ 4');
  });

  it.each([UNAVAILABLE, NOT_APPLICABLE, EM_DASH, '— (no QL yet)', 'unknown (deals not assessed)', ''])(
    'leaves %j unchanged',
    (text) => {
      expect(formatFloor(text, 'max')).toBe(text);
      expect(formatFloor(text, 'min')).toBe(text);
    },
  );
});

describe('formatMetric', () => {
  it('renders unavailable values, never 0', () => {
    expect(formatMetric(metric('5', { unavailable: true }))).toBe(UNAVAILABLE);
    expect(formatMetric(metric(null))).toBe(UNAVAILABLE);
    expect(formatMetric(metric(null, { unit: 'currency', currency: 'USD', floor: true }))).toBe(
      UNAVAILABLE,
    );
    expect(formatMetric({ value: undefined })).toBe(UNAVAILABLE);
  });

  it('renders an unavailable zero as unavailable, not 0', () => {
    const unavailableZero = {
      key: 'ql',
      label: 'Qualified Leads',
      value: '0',
      unit: 'contacts',
      currency: null,
      floor: null,
      unavailable: true,
    } satisfies Metric;
    expect(formatMetric(unavailableZero)).toBe('unavailable');
    expect(formatMetric(unavailableZero)).not.toBe('0');
    expect(formatMetric({ ...unavailableZero, value: null, unavailable: null })).toBe(
      'unavailable',
    );
  });

  it('renders booleans as yes or no', () => {
    expect(formatMetric(metric(true))).toBe('yes');
    expect(formatMetric(metric(false))).toBe('no');
  });

  it('shows text-unit values verbatim, even when they look numeric', () => {
    expect(formatMetric(metric('2024', { unit: 'text' }))).toBe('2024');
    expect(formatMetric(metric(2024, { unit: 'text' }))).toBe('2024');
    expect(formatMetric(metric('crm software', { unit: 'text', floor: true }))).toBe(
      'crm software',
    );
  });

  it('returns pre-rendered text unchanged and unmarked', () => {
    expect(formatMetric(metric('n/a', { unit: 'ratio' }))).toBe('n/a');
    expect(formatMetric(metric('—', { unit: 'currency', currency: 'USD' }))).toBe('—');
    expect(formatMetric(metric('≥ 12', { unit: 'contacts', floor: true }))).toBe('≥ 12');
    expect(formatMetric(metric('unknown (3 deals not assessed)', { floor: true }))).toBe(
      'unknown (3 deals not assessed)',
    );
  });

  it.each(['n/a', '≥ 7', 'unknown (3 deals not assessed)'])(
    'passes the pre-rendered %j through unchanged, floored or not',
    (text) => {
      expect(formatMetric(metric(text))).toBe(text);
      expect(formatMetric(metric(text, { floor: true }))).toBe(text);
      expect(formatMetric(metric(text, { unit: 'currency', currency: 'EUR', floor: true }))).toBe(
        text,
      );
    },
  );

  it('formats money by currency code or money unit', () => {
    expect(formatMetric(metric('12.340000', { unit: 'currency', currency: 'USD' }))).toBe(
      'USD\u00a012.34',
    );
    expect(formatMetric(metric('12.340000', { unit: 'currency' }))).toBe('12.34');
    expect(formatMetric(metric('12.340000', { unit: 'spend', currency: '  ' }))).toBe('12.34');
    expect(formatMetric(metric('7', { unit: 'clicks', currency: 'EUR' }))).toBe('EUR\u00a07.00');
    expect(formatMetric(metric(1500, { unit: 'Cost', currency: 'JPY' }))).toBe('JPY\u00a01,500');
  });

  it('formats ratios, rates, percentage points, days, p-values and multiples', () => {
    expect(formatMetric(metric('0.0909', { unit: 'ratio' }))).toBe('9.1%');
    expect(formatMetric(metric('1.15', { unit: ' Ratio ' }))).toBe('115%');
    expect(formatMetric(metric('0.012345', { unit: 'rate' }))).toBe('0.012345');
    expect(formatMetric(metric('79.1', { unit: 'percent' }))).toBe('79.1%');
    expect(formatMetric(metric(Number.NaN, { unit: 'pct' }))).toBe(UNAVAILABLE);
    expect(formatMetric(metric(28, { unit: 'days' }))).toBe('28 days');
    expect(formatMetric(metric(1, { unit: 'day' }))).toBe('1 day');
    expect(formatMetric(metric('0.0321', { unit: 'p' }))).toBe('0.032');
    expect(formatMetric(metric('0.0004', { unit: 'p_value' }))).toBe('< 0.001');
    expect(formatMetric(metric('1.5', { unit: 'x' }))).toBe('1.5×');
    expect(formatMetric(metric(Number.NaN, { unit: 'multiple' }))).toBe(UNAVAILABLE);
  });

  it.each([...MONEY_UNITS])('formats the money unit %j in its currency', (unit) => {
    expect(formatMetric(metric('40', { unit, currency: 'EUR' }))).toBe('EUR\u00a040.00');
    expect(formatMetric(metric('40', { unit }))).toBe('40');
  });

  it.each([...RATIO_UNITS])('formats the ratio unit %j as a percentage', (unit) => {
    expect(formatMetric(metric('0.123', { unit }))).toBe('12.3%');
    expect(formatMetric(metric(1.4, { unit }))).toBe('140%');
  });

  it.each([...RATE_UNITS])('formats the rate unit %j as a six-place decimal', (unit) => {
    expect(formatMetric(metric('0.012345', { unit }))).toBe('0.012345');
  });

  it.each([...PERCENT_POINT_UNITS])('formats the percent-point unit %j with a % sign', (unit) => {
    expect(formatMetric(metric(12.3, { unit }))).toBe('12.3%');
  });

  it.each([...DAY_UNITS])('formats the day unit %j as a day count', (unit) => {
    expect(formatMetric(metric(28, { unit }))).toBe('28 days');
    expect(formatMetric(metric(1, { unit }))).toBe('1 day');
  });

  it.each([...P_VALUE_UNITS])('formats the p-value unit %j', (unit) => {
    expect(formatMetric(metric('0.0321', { unit }))).toBe('0.032');
    expect(formatMetric(metric(0.0004, { unit }))).toBe('< 0.001');
  });

  // A multiple renders through formatDecimal at two places without padding, the contract
  // format.ts documents ("1.5×"), so 1.5 reads "1.5×" rather than the test plan's "1.50×".
  it.each([...MULTIPLE_UNITS])('formats the multiple unit %j with a × sign', (unit) => {
    expect(formatMetric(metric(1.5, { unit }))).toBe('1.5×');
    expect(formatMetric(metric('1.456', { unit }))).toBe('1.46×');
  });

  it.each([...TEXT_UNITS])('shows the text unit %j verbatim', (unit) => {
    expect(formatMetric(metric('2024', { unit }))).toBe('2024');
  });

  it('formats counts and unknown units as counts', () => {
    expect(formatMetric(metric(0.25, { unit: 'widgets' }))).toBe('0.25');
    expect(formatMetric(metric(1234, { unit: 'clicks' }))).toBe('1,234');
    expect(formatMetric(metric('0.3', { unit: 'conversions' }))).toBe('0.3');
    expect(formatMetric(metric('6.5', { unit: 'score' }))).toBe('6.5');
    expect(formatMetric(metric(12))).toBe('12');
    expect(formatMetric({ value: '12' })).toBe('12');
  });

  it('marks floors: a ceiling for money, a lower bound otherwise', () => {
    expect(
      formatMetric(metric('75', { unit: 'currency', currency: 'USD', floor: true })),
    ).toBe('≤ USD\u00a075.00');
    expect(
      formatMetric(metric('40', { unit: 'currency', currency: 'EUR', floor: true })),
    ).toBe('≤ EUR\u00a040.00');
    expect(formatMetric(metric(12, { unit: 'contacts', floor: true }))).toBe('≥ 12');
    expect(formatMetric(metric(12, { unit: 'contacts', floor: false }))).toBe('12');
    expect(formatMetric(metric(Number.NaN, { unit: 'meetings', floor: true }))).toBe(UNAVAILABLE);
  });

  it.each([...RATIO_UNITS])('renders the ratio unit %j at fixed ratioDigits', (unit) => {
    expect(formatMetric(metric('0.123', { unit }), { ratioDigits: 2 })).toBe('12.30%');
    expect(formatMetric(metric('0.000698', { unit }), { ratioDigits: 3 })).toBe('0.070%');
    expect(formatMetric(metric('0.5', { unit }), { ratioDigits: 1 })).toBe('50.0%');
  });

  it('keeps the bound, the floor and pre-rendered text under ratioDigits', () => {
    expect(formatMetric(metric('0.000004', { unit: 'ratio' }), { ratioDigits: 3 })).toBe(
      '< 0.001%',
    );
    expect(formatMetric(metric('0.5', { unit: 'share', floor: true }), { ratioDigits: 2 })).toBe(
      '≥ 50.00%',
    );
    expect(formatMetric(metric('n/a', { unit: 'ratio' }), { ratioDigits: 2 })).toBe('n/a');
    expect(formatMetric(metric(null, { unit: 'ratio' }), { ratioDigits: 2 })).toBe(UNAVAILABLE);
  });

  it('applies ratioDigits to ratios only, and changes nothing without it', () => {
    const cost = metric('12.340000', { unit: 'currency', currency: 'USD' });
    expect(formatMetric(cost, { ratioDigits: 3 })).toBe('USD\u00a012.34');
    expect(formatMetric(metric('0.3', { unit: 'conversions' }), { ratioDigits: 3 })).toBe('0.3');
    expect(formatMetric(metric('79.1', { unit: 'percent' }), { ratioDigits: 3 })).toBe('79.1%');
    expect(formatMetric(metric('0.0909', { unit: 'ratio' }), {})).toBe('9.1%');
    expect(formatMetric(metric('0.0909', { unit: 'ratio' }))).toBe('9.1%');
  });
});

describe('ratioMetricDigits', () => {
  it('picks the digits of the metrics formatMetric renders as percentages', () => {
    const rateA = metric('0.000722', { unit: 'ratio' });
    const rateB = metric('0.000698', { unit: ' Ratio ' });
    const digits = ratioMetricDigits([rateA, rateB, metric(5, { unit: 'leads' })]);
    expect(digits).toBe(3);
    expect(formatMetric(rateA, { ratioDigits: digits })).toBe('0.072%');
    expect(formatMetric(rateB, { ratioDigits: digits })).toBe('0.070%');
  });

  it('ignores counts, money, unavailable, boolean and pre-rendered metrics', () => {
    const tiny = '0.0000001';
    expect(
      ratioMetricDigits([
        metric('0.5', { unit: 'ratio' }),
        metric(tiny, { unit: 'leads' }),
        metric(tiny, { unit: 'ratio', currency: 'USD' }),
        metric(tiny, { unit: 'ratio', unavailable: true }),
        metric(true, { unit: 'ratio' }),
        metric('n/a', { unit: 'ratio' }),
        { value: undefined, unit: 'ratio' },
        { value: tiny },
      ]),
    ).toBe(1);
  });

  it('passes its options on', () => {
    expect(ratioMetricDigits([])).toBe(1);
    expect(ratioMetricDigits([], { min: 2 })).toBe(2);
    expect(ratioMetricDigits([metric('0.000722', { unit: 'ratio' })], { max: 2 })).toBe(2);
  });
});

describe('formatDays', () => {
  it('uses the singular for exactly one day', () => {
    expect(formatDays(1)).toBe('1 day');
    expect(formatDays('1')).toBe('1 day');
    expect(formatDays('1.0')).toBe('1 day');
  });

  it('uses the plural otherwise', () => {
    expect(formatDays(28)).toBe('28 days');
    expect(formatDays(0)).toBe('0 days');
    expect(formatDays(1.5)).toBe('1.5 days');
    expect(formatDays(1180)).toBe('1,180 days');
  });

  it.each([null, undefined, '', 'n/a'])('renders %j as unavailable', (value) => {
    expect(formatDays(value)).toBe(UNAVAILABLE);
  });
});

describe('formatSample', () => {
  it('renders observed / required with the unit', () => {
    expect(formatSample({ observed: 37, required: 300, unit: 'clicks', projected: null })).toBe(
      '37\u00A0/\u00A0300 clicks',
    );
    expect(formatSample({ observed: 3, required: 5, unit: 'spending days' })).toBe(
      '3\u00A0/\u00A05 spending days',
    );
    expect(formatSample({ observed: 3100, required: '4200', unit: 'form opens' })).toBe(
      '3,100\u00A0/\u00A04,200 form opens',
    );
  });

  it('joins observed and required with the no-break separator, so the figure never wraps', () => {
    const line = formatSample({ observed: 0, required: 7, unit: 'known-budget days' });
    expect(line).toBe(`0${FIGURE_SEPARATOR}7 known-budget days`);
    // The only breakable space is the one before the unit; none sits inside "0 / 7".
    expect(line.split(' ')).toEqual(['0\u00A0/\u00A07', 'known-budget', 'days']);
    expect(line).not.toContain(' / ');
  });

  it('renders a typed evidence sample and its projection', () => {
    const clicks = {
      observed: 37,
      required: 300,
      unit: 'clicks',
      projected: null,
    } satisfies Sample;
    expect(formatSample(clicks)).toBe('37\u00A0/\u00A0300 clicks');

    const impressions = {
      observed: 37,
      required: 300,
      unit: 'clicks',
      projected: {
        value: 1200,
        unit: 'impressions',
        basis: 'projected through the observed open rate',
      },
    } satisfies Sample;
    const line = formatSample(impressions);
    expect(line).toContain('37\u00A0/\u00A0300 clicks');
    expect(line).toContain('≈ 1,200 impressions');
    expect(line).toContain('projected through the observed open rate');
  });

  it('labels a projection as one', () => {
    expect(
      formatSample({
        observed: 3100,
        required: 4200,
        unit: 'form opens',
        projected: {
          value: '52000',
          unit: 'impressions',
          basis: 'projected through the observed open rate',
        },
      }),
    ).toBe(
      '3,100\u00A0/\u00A04,200 form opens · ' +
        '≈ 52,000 impressions (projected through the observed open rate)',
    );
    expect(
      formatSample({
        observed: 1,
        required: 2,
        unit: 'variants',
        projected: { value: 3.5, unit: ' ', basis: '' },
      }),
    ).toBe('1\u00A0/\u00A02 variants · ≈ 3.5');
  });

  it('omits a projection without a numeric value', () => {
    const base = { observed: 1, required: 2, unit: 'days' };
    expect(formatSample({ ...base, projected: { value: null, unit: 'days', basis: 'b' } })).toBe(
      '1\u00A0/\u00A02 days',
    );
    expect(formatSample({ ...base, projected: { value: true, unit: 'days', basis: 'b' } })).toBe(
      '1\u00A0/\u00A02 days',
    );
  });

  it('accepts a withheld sample with no unit', () => {
    expect(formatSample({ observed: 3, required: 5, unit: null })).toBe('3\u00A0/\u00A05');
    expect(formatSample({ observed: '0.3', required: 10, unit: '  ' })).toBe(
      '0.3\u00A0/\u00A010',
    );
  });

  it('returns an empty line when no sample applies', () => {
    expect(formatSample({ observed: null, required: null, unit: null })).toBe('');
    expect(formatSample({ observed: null, required: null, unit: 'days', projected: null })).toBe(
      '',
    );
  });

  it('renders a missing, boolean or pre-rendered figure without guessing', () => {
    expect(formatSample({ observed: null, required: 5, unit: 'days' })).toBe(
      'unavailable\u00A0/\u00A05 days',
    );
    expect(formatSample({ observed: 4, required: null, unit: 'days' })).toBe(
      '4\u00A0/\u00A0unavailable days',
    );
    expect(formatSample({ observed: true, required: 'n/a', unit: null })).toBe(
      'yes\u00A0/\u00A0n/a',
    );
    expect(formatSample({ observed: false, required: 1, unit: null })).toBe('no\u00A0/\u00A01');
  });
});

describe('formatPValue', () => {
  it('rounds to at most three places without padding', () => {
    expect(formatPValue('0.0321')).toBe('0.032');
    expect(formatPValue('0.05')).toBe('0.05');
    expect(formatPValue(0.001)).toBe('0.001');
    expect(formatPValue(1)).toBe('1');
    expect(formatPValue('0.123456')).toBe('0.123');
  });

  it('bounds values below 0.001', () => {
    expect(formatPValue(0.0004)).toBe('< 0.001');
    expect(formatPValue('0.000999')).toBe('< 0.001');
    expect(formatPValue(0)).toBe('< 0.001');
  });

  it.each([null, undefined, '', 'n/a'])('renders %j as unavailable', (value) => {
    expect(formatPValue(value)).toBe(UNAVAILABLE);
  });
});

describe('formatTestLine', () => {
  it('renders the full line', () => {
    expect(
      formatTestLine({ name: 'fisher_exact', p_value: '0.012', p_adjusted: '0.036', alpha: '0.05' }),
    ).toBe("Fisher's exact test · p = 0.012 · adjusted p = 0.036 · α 0.05");
  });

  it('renders a typed evidence test, with the adjusted segment only when present', () => {
    const holm = {
      name: 'fisher_exact',
      p_value: '0.012',
      p_adjusted: '0.036',
      alpha: '0.05',
    } satisfies StatTest;
    const line = formatTestLine(holm);
    expect(line).toContain("Fisher's exact test");
    expect(line).toContain('p = 0.012');
    expect(line).toContain('adjusted p = 0.036');
    expect(line).toContain('α 0.05');

    const unadjusted = formatTestLine({ ...holm, p_adjusted: null } satisfies StatTest);
    expect(unadjusted).not.toContain('adjusted');
    expect(unadjusted).toBe("Fisher's exact test · p = 0.012 · α 0.05");

    expect(formatTestLine({ ...holm, name: 'mystery_test' } satisfies StatTest)).toContain(
      'mystery_test',
    );
  });

  it('maps every known backend name', () => {
    expect([...TEST_LABELS.keys()]).toEqual([
      'fisher_exact',
      'two_proportion_z',
      'poisson_rate',
      'poisson_rate_test',
      'two_sample_poisson_score',
      'two-sample Poisson rate test',
      'holm',
    ]);
    expect(formatTestLine({ name: 'two_proportion_z', p_value: 0.2, alpha: 0.05 })).toBe(
      'Two-proportion z-test · p = 0.2 · α 0.05',
    );
    expect(
      formatTestLine({ name: 'two-sample Poisson rate test', p_value: '0.0004', alpha: '0.05' }),
    ).toBe('Two-sample Poisson rate test · p < 0.001 · α 0.05');
    expect(formatTestLine({ name: ' holm ', p_value: '0.5' })).toBe('Holm-adjusted · p = 0.5');
  });

  it('shows an unknown name unchanged and omits absent segments', () => {
    expect(
      formatTestLine({ name: 'Wald test', p_value: '0.04', p_adjusted: null, alpha: null }),
    ).toBe('Wald test · p = 0.04');
    expect(formatTestLine({ name: 'Wald test', p_value: null, alpha: '0.05' })).toBe(
      'Wald test · p = unavailable · α 0.05',
    );
    expect(
      formatTestLine({ name: 'fisher_exact', p_value: true, p_adjusted: false, alpha: true }),
    ).toBe("Fisher's exact test · p = unavailable");
  });

  it('states a bounded adjusted p-value without an equals sign', () => {
    expect(
      formatTestLine({
        name: 'fisher_exact',
        p_value: '0.0001',
        p_adjusted: '0.0003',
        alpha: '0.05',
      }),
    ).toBe("Fisher's exact test · p < 0.001 · adjusted p < 0.001 · α 0.05");
  });
});

describe('formatSourceDate', () => {
  it('labels the stored date with its source day', () => {
    expect(formatSourceDate('2026-09-04', 'google')).toBe('2026-09-04 (Google day)');
    expect(formatSourceDate('2026-09-04', 'google', 'America/New_York')).toBe(
      '2026-09-04 (Google day, America/New_York)',
    );
    expect(formatSourceDate('2026-09-04', 'linkedin', 'UTC')).toBe(
      '2026-09-04 (LinkedIn day, UTC)',
    );
    expect(formatSourceDate('2026-09-04', 'linkedin', ' ')).toBe('2026-09-04 (LinkedIn day)');
    expect(formatSourceDate('2026-09-04', 'linkedin', null)).toBe('2026-09-04 (LinkedIn day)');
  });

  it('never shifts the calendar date, whatever the boundary', () => {
    // A Date round trip would read 2026-01-01 as UTC midnight and print 2025-12-31 in any zone
    // west of UTC, such as New York or Pago Pago; the stored string must survive unchanged.
    expect(formatSourceDate('2026-01-01', 'linkedin')).toBe('2026-01-01 (LinkedIn day)');
    expect(formatSourceDate('2026-01-01', 'google', 'America/New_York')).toBe(
      '2026-01-01 (Google day, America/New_York)',
    );
    expect(formatSourceDate('2026-01-01', 'google', 'America/New_York')).not.toContain(
      '2025-12-31',
    );
    expect(formatSourceDate('2026-01-01', 'google', 'Pacific/Kiritimati')).toContain('2026-01-01');
    expect(formatSourceDate('2026-01-01', 'google', 'Pacific/Pago_Pago')).toContain('2026-01-01');
  });

  it.each([null, undefined, '', '  '])('renders a missing date %j as unavailable', (value) => {
    expect(formatSourceDate(value, 'google')).toBe(UNAVAILABLE);
  });
});

describe('formatDateRange', () => {
  it('joins the stored dates with a spaced en dash', () => {
    expect(formatDateRange('2026-09-04', '2026-10-01')).toBe('2026-09-04 \u2013 2026-10-01');
  });

  it('names a missing end as unavailable', () => {
    expect(formatDateRange('2026-09-04', null)).toBe('2026-09-04 – unavailable');
    expect(formatDateRange(undefined, '2026-10-01')).toBe('unavailable – 2026-10-01');
  });
});

describe('formatSourceBoundaries and boundariesDiffer', () => {
  const google = {
    source: 'google',
    start: '2026-09-04',
    end: '2026-10-01',
    boundary: 'America/New_York',
  } as const satisfies SourceDay;
  const linkedin = {
    source: 'linkedin',
    start: '2026-09-04',
    end: '2026-10-01',
    boundary: 'UTC',
  } as const satisfies SourceDay;

  it('renders the status-bar example exactly', () => {
    expect(formatSourceBoundaries([google, linkedin])).toBe(
      'Google days: America/New_York \u00b7 LinkedIn days: UTC',
    );
  });

  it('keeps input order and the first entry of a repeated source', () => {
    expect(
      formatSourceBoundaries([linkedin, google, { source: 'linkedin', boundary: 'Europe/Paris' }]),
    ).toBe('LinkedIn days: UTC · Google days: America/New_York');
  });

  it('shows a duplicate source once', () => {
    expect(formatSourceBoundaries([google, linkedin, google])).toBe(
      'Google days: America/New_York · LinkedIn days: UTC',
    );
  });

  it('renders an empty list as empty and a blank boundary as unavailable', () => {
    expect(formatSourceBoundaries([])).toBe('');
    expect(formatSourceBoundaries([{ source: 'google', boundary: ' ' }])).toBe(
      'Google days: unavailable',
    );
  });

  it('detects differing boundaries', () => {
    expect(boundariesDiffer([google, linkedin])).toBe(true);
    expect(boundariesDiffer([linkedin, { ...google, boundary: 'UTC' }])).toBe(false);
    expect(boundariesDiffer([linkedin, { boundary: 'UTC' }])).toBe(false);
    expect(boundariesDiffer([linkedin])).toBe(false);
    expect(boundariesDiffer([])).toBe(false);
    expect(boundariesDiffer([{ boundary: ' ' }, { boundary: 'UTC' }])).toBe(true);
    expect(boundariesDiffer([{ boundary: ' UTC ' }, { boundary: 'UTC' }])).toBe(false);
  });
});

describe('formatWindowChip', () => {
  const window = {
    start: '2026-09-04',
    end: '2026-10-01',
    days: 28,
    source_days: [
      { source: 'google', start: '2026-09-04', end: '2026-10-01', boundary: 'America/New_York' },
      { source: 'linkedin', start: '2026-09-04', end: '2026-10-01', boundary: 'UTC' },
    ],
    boundaries_differ: true,
    description: 'Google 2026-09-04 to 2026-10-01 (America/New_York); LinkedIn same dates (UTC)',
  } satisfies EvidenceWindow;

  it('renders the card chip example exactly', () => {
    expect(formatWindowChip(window)).toBe(
      '2026-09-04 – 2026-10-01 · 28 days · Google days: America/New_York · LinkedIn days: UTC',
    );
  });

  it('reads one day in the singular and drops the boundary segment without source days', () => {
    expect(formatWindowChip({ ...window, days: 1 })).toBe(
      '2026-09-04 – 2026-10-01 · 1 day · Google days: America/New_York · LinkedIn days: UTC',
    );
    expect(formatWindowChip({ ...window, source_days: [] })).toBe(
      '2026-09-04 – 2026-10-01 · 28 days',
    );
  });

  it('reads a single day in the singular and omits an empty boundary segment', () => {
    expect(
      formatWindowChip({ start: '2026-10-01', end: '2026-10-01', days: 1, source_days: [] }),
    ).toBe('2026-10-01 – 2026-10-01 · 1 day');
  });

  it('shows a source range that differs from the window', () => {
    expect(
      formatWindowChip({
        ...window,
        source_days: [
          { source: 'google', start: '2026-09-04', end: '2026-10-01', boundary: 'UTC' },
          { source: 'linkedin', start: '2026-09-05', end: '2026-10-01', boundary: 'UTC' },
          { source: 'hubspot', start: '2026-09-04', end: '2026-10-02', boundary: 'UTC' },
        ],
      }),
    ).toBe(
      '2026-09-04 – 2026-10-01 · 28 days · Google days: UTC · ' +
        'LinkedIn days: UTC (2026-09-05 – 2026-10-01) · HubSpot days: UTC (2026-09-04 – 2026-10-02)',
    );
  });

  it('takes the window range for an entry without its own', () => {
    expect(
      formatWindowChip({ ...window, source_days: [{ source: 'linkedin', boundary: 'UTC' }] }),
    ).toBe('2026-09-04 – 2026-10-01 · 28 days · LinkedIn days: UTC');
  });
});

describe('formatInstant', () => {
  it('formats the verified outputs exactly', () => {
    expect(formatInstant('2026-10-06T15:22:00Z', 'America/New_York')).toBe(
      'Oct 6, 2026, 11:22 AM America/New_York',
    );
    expect(formatInstant('2026-10-06T15:22:00Z', 'UTC')).toBe('Oct 6, 2026, 3:22 PM UTC');
  });

  it('differs from UTC by the zone offset in force: 4 h in EDT, 5 h in EST', () => {
    const october = '2026-10-06T15:22:00Z';
    const january = '2026-01-15T15:22:00Z';
    expect(formatInstant(january, 'America/New_York')).toBe(
      'Jan 15, 2026, 10:22 AM America/New_York',
    );
    expect(formatInstant(january, 'UTC')).toBe('Jan 15, 2026, 3:22 PM UTC');
    expect(
      clockMinutes(formatInstant(october, 'UTC')) -
        clockMinutes(formatInstant(october, 'America/New_York')),
    ).toBe(4 * 60);
    expect(
      clockMinutes(formatInstant(january, 'UTC')) -
        clockMinutes(formatInstant(january, 'America/New_York')),
    ).toBe(5 * 60);
  });

  it('reuses the cached formatter and keeps plain spaces', () => {
    const first = formatInstant('2026-10-06T06:00:00+02:00', 'Europe/Berlin');
    const second = formatInstant('2026-10-06T06:00:00+02:00', 'Europe/Berlin');
    expect(first).toBe('Oct 6, 2026, 6:00 AM Europe/Berlin');
    expect(second).toBe(first);
    expect(SPECIAL_SPACES.test(first)).toBe(false);
  });

  it.each([null, undefined, '', '   '])('renders a missing instant %j as an em dash', (value) => {
    expect(formatInstant(value, 'UTC')).toBe(EM_DASH);
  });

  it('returns an unparsable instant unchanged', () => {
    expect(formatInstant('not-a-date', 'UTC')).toBe('not-a-date');
  });

  it('falls back to UTC, labelled UTC, for an invalid or blank zone', () => {
    expect(formatInstant('2026-10-06T15:22:00Z', 'Mars/Base')).toBe('Oct 6, 2026, 3:22 PM UTC');
    expect(formatInstant('2026-10-06T15:22:00Z', 'Mars/Base')).toBe('Oct 6, 2026, 3:22 PM UTC');
    expect(formatInstant('2026-10-06T15:22:00Z', '')).toBe('Oct 6, 2026, 3:22 PM UTC');
    expect(formatInstant('2026-10-06T15:22:00Z', '  ')).toBe('Oct 6, 2026, 3:22 PM UTC');
  });
});

describe('joinSentences', () => {
  const SUFFIX = 'Showing the last loaded state.';

  it.each([
    ['Internal server error', 'Internal server error. Showing the last loaded state.'],
    [
      'The request did not complete in time',
      'The request did not complete in time. Showing the last loaded state.',
    ],
    ['Database unavailable', 'Database unavailable. Showing the last loaded state.'],
  ])('ends the unpunctuated reason %j with a full stop', (reason, expected) => {
    expect(joinSentences(reason, SUFFIX)).toBe(expected);
  });

  it.each([
    [
      'The server is temporarily unavailable. Try again shortly.',
      'The server is temporarily unavailable. Try again shortly. Showing the last loaded state.',
    ],
    ['The API is down!', 'The API is down! Showing the last loaded state.'],
    ['Is the worker running?', 'Is the worker running? Showing the last loaded state.'],
    ['Still waiting…', 'Still waiting… Showing the last loaded state.'],
    ['Still waiting...', 'Still waiting... Showing the last loaded state.'],
  ])('keeps the sentence end of %j and never doubles a period', (reason, expected) => {
    expect(joinSentences(reason, SUFFIX)).toBe(expected);
  });

  it.each([
    ['Retry "later."', 'Retry "later." Showing the last loaded state.'],
    ["Retry 'later.'", "Retry 'later.' Showing the last loaded state."],
    ['Retry “later.”', 'Retry “later.” Showing the last loaded state.'],
    ['Retry ‘later!’', 'Retry ‘later!’ Showing the last loaded state.'],
    ['Failed (see the log.)', 'Failed (see the log.) Showing the last loaded state.'],
    ['Failed [timeout?]', 'Failed [timeout?] Showing the last loaded state.'],
    ['Failed (“timeout.”)', 'Failed (“timeout.”) Showing the last loaded state.'],
  ])('keeps the closing quotes or brackets after the sentence end of %j', (reason, expected) => {
    expect(joinSentences(reason, SUFFIX)).toBe(expected);
  });

  it.each([
    ['Unknown field "window"', 'Unknown field "window". Showing the last loaded state.'],
    ['Upstream failed (502)', 'Upstream failed (502). Showing the last loaded state.'],
    ['Rejected [window]', 'Rejected [window]. Showing the last loaded state.'],
  ])('ends %j with a full stop when its closing mark ends no sentence', (reason, expected) => {
    expect(joinSentences(reason, SUFFIX)).toBe(expected);
  });

  it('trims every part and joins the parts with exactly one space', () => {
    expect(joinSentences('  Database unavailable \n', `\t${SUFFIX}  `)).toBe(
      'Database unavailable. Showing the last loaded state.',
    );
    expect(joinSentences('One', 'two', 'three!')).toBe('One. two. three!');
  });

  it('skips blank parts, so no stray period or double space appears', () => {
    expect(joinSentences('', SUFFIX)).toBe(SUFFIX);
    expect(joinSentences('   ', SUFFIX, '\n')).toBe(SUFFIX);
    expect(joinSentences('Database unavailable', '')).toBe('Database unavailable.');
    expect(joinSentences('', '  ')).toBe('');
    expect(joinSentences()).toBe('');
  });

  it('keeps the words of every part verbatim', () => {
    expect(joinSentences('internal server error', 'see /sources')).toBe(
      'internal server error. see /sources.',
    );
    expect(joinSentences('Single part')).toBe('Single part.');
  });
});
