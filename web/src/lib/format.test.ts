// @vitest-environment node
/**
 * Unit tests for src/lib/format.ts, the SPA's display formatting (AAP 0.5.5 Behaviour).
 *
 * Every branch of the module has a case here: absent and non-numeric inputs, tiny non-zero
 * values, fractional counts, floors that must not double, refused currency codes, invalid
 * display zones and unparsable instants. Expected strings are literal, so a change in rendering
 * fails a test rather than passing silently. The verified ICU outputs quoted in the module's
 * contract are asserted verbatim.
 */
import { describe, expect, it } from 'vitest';

import type { EvidenceWindow, Metric, SourceName } from '../api/types';
import * as format from './format';
import {
  DAY_UNITS,
  EM_DASH,
  LOCALE,
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

/** The spaces format.ts must never leave in its output. */
const SPECIAL_SPACES = /[\u202F\u00A0]/;

describe('constants', () => {
  it('fixes the locale and the placeholder texts', () => {
    expect(LOCALE).toBe('en');
    expect(UNAVAILABLE).toBe('unavailable');
    expect(EM_DASH).toBe('\u2014');
    expect(NOT_APPLICABLE).toBe('n/a');
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
});

describe('isDecimalString', () => {
  it.each(['120.500000', ' -3 ', '.5', '1e-7', '+5', '5.', '0', '-0.00', '2E+3'])(
    'accepts %j',
    (value) => {
      expect(isDecimalString(value)).toBe(true);
    },
  );

  it.each(['', '  ', 'n/a', '1,234', '0x10', 'Infinity', 'NaN', '--1', '1.2.3', '≥ 12', 'e5'])(
    'rejects %j',
    (value) => {
      expect(isDecimalString(value)).toBe(false);
    },
  );
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
    expect(formatMoney('1234.567891', 'EUR')).toBe('€1,234.57');
    expect(formatMoney('40', 'GBP')).toBe('£40.00');
    expect(formatMoney('1234.5', 'JPY')).toBe('¥1,235');
    expect(formatMoney('-40', 'EUR')).toBe('-€40.00');
  });

  it('rounds the exact decimal string, never a float', () => {
    expect(formatMoney('999999999999.994999', 'USD')).toBe('$999,999,999,999.99');
    expect(formatMoney('120.500000', 'USD')).toBe('$120.50');
  });

  it('formats numbers and lower-case codes', () => {
    expect(formatMoney(12.5, 'usd')).toBe('$12.50');
    expect(formatMoney(' 12.5 ', ' USD ')).toBe('$12.50');
  });

  it('never shows a sign on an amount that rounds to zero', () => {
    expect(formatMoney('-0.001', 'USD')).toBe('$0.00');
  });

  it.each([null, undefined, '', '  ', 'abc', Number.NaN, Number.POSITIVE_INFINITY])(
    'renders %j as unavailable',
    (value) => {
      expect(formatMoney(value, 'USD')).toBe(UNAVAILABLE);
    },
  );

  it('implies no currency when the code is missing', () => {
    expect(formatMoney('40.5', null)).toBe('40.5');
    expect(formatMoney('40.5', undefined)).toBe('40.5');
    expect(formatMoney('40.5', '  ')).toBe('40.5');
  });

  it('falls back to the decimal and the code for a code Intl refuses', () => {
    expect(formatMoney('5', 'EURO')).toBe('5 EURO');
    // A second call reads the cached refusal.
    expect(formatMoney('5.256', 'EURO')).toBe('5.26 EURO');
  });

  it('formats a well-formed unknown code with plain spaces', () => {
    const text = formatMoney('5', 'XYZ');
    expect(text).toBe('XYZ 5.00');
    expect(SPECIAL_SPACES.test(text)).toBe(false);
  });
});

describe('currencyMinorDigits and currencyStep', () => {
  it.each([
    ['USD', 2, '0.01'],
    ['EUR', 2, '0.01'],
    ['JPY', 0, '1'],
    ['KWD', 3, '0.001'],
    ['BHD', 3, '0.001'],
  ])('%s has %i minor digits and step %s', (code, digits, step) => {
    expect(currencyMinorDigits(code)).toBe(digits);
    expect(currencyStep(code)).toBe(step);
  });

  it.each(['EURO', '', '  ', null, undefined])('defaults %j to 2 digits', (code) => {
    expect(currencyMinorDigits(code)).toBe(2);
    expect(currencyStep(code)).toBe('0.01');
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

  it.each([null, undefined, '', 'n/a', Number.NaN, Number.NEGATIVE_INFINITY])(
    'renders %j as unavailable',
    (value) => {
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

  it.each([null, undefined, '', 'abc', Number.NaN])('renders %j as unavailable', (value) => {
    expect(formatCount(value)).toBe(UNAVAILABLE);
  });
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

  it.each([null, undefined, '', 'n/a', Number.NaN])('renders %j as unavailable', (value) => {
    expect(formatPercent(value)).toBe(UNAVAILABLE);
  });
});

describe('formatCost', () => {
  it('renders an em dash for a zero or unusable denominator', () => {
    expect(formatCost('300', 0, 'USD')).toBe(EM_DASH);
    expect(formatCost('300', '0.000', 'USD')).toBe(EM_DASH);
    expect(formatCost('300', -1, 'USD')).toBe(EM_DASH);
    expect(formatCost('300', null, 'USD')).toBe(EM_DASH);
    expect(formatCost('300', undefined, 'USD')).toBe(EM_DASH);
    expect(formatCost('300', 'abc', 'USD')).toBe(EM_DASH);
    expect(formatCost('300', Number.POSITIVE_INFINITY, 'USD')).toBe(EM_DASH);
    expect(formatCost('300', '1e400', 'USD')).toBe(EM_DASH);
  });

  it('names the reason after the dash', () => {
    expect(formatCost('300', 0, 'USD', 'no QL yet')).toBe('— (no QL yet)');
    expect(formatCost('300', 0, 'USD', 'no meetings yet')).toBe('— (no meetings yet)');
    expect(formatCost('300', 0, 'USD', '  ')).toBe(EM_DASH);
    expect(formatCost(null, 0, 'USD', 'no QL yet')).toBe('— (no QL yet)');
  });

  it('divides spend by the denominator in the currency', () => {
    expect(formatCost('300', 4, 'USD')).toBe('$75.00');
    expect(formatCost('300.000000', '3', 'EUR')).toBe('€100.00');
    expect(formatCost('1000', 3, 'JPY')).toBe('¥333');
    expect(formatCost('10', 4, null)).toBe('2.5');
  });

  it('renders unformattable spend as unavailable', () => {
    expect(formatCost(null, 3, 'USD')).toBe(UNAVAILABLE);
    expect(formatCost('n/a', 3, 'USD')).toBe(UNAVAILABLE);
  });
});

describe('formatFloor', () => {
  it('marks counts and costs', () => {
    expect(formatFloor('12', 'min')).toBe('≥ 12');
    expect(formatFloor('$75.00', 'max')).toBe('≤ $75.00');
    expect(formatFloor(' 12 ', 'min')).toBe('≥ 12');
    expect(formatFloor('XYZ 5.00', 'max')).toBe('≤ XYZ 5.00');
  });

  it('never doubles a prefix the text already carries', () => {
    expect(formatFloor('≥ 12', 'min')).toBe('≥ 12');
    expect(formatFloor('≤ $75.00', 'max')).toBe('≤ $75.00');
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

  it('formats money by currency code or money unit', () => {
    expect(formatMetric(metric('12.340000', { unit: 'currency', currency: 'USD' }))).toBe(
      '$12.34',
    );
    expect(formatMetric(metric('12.340000', { unit: 'currency' }))).toBe('12.34');
    expect(formatMetric(metric('12.340000', { unit: 'spend', currency: '  ' }))).toBe('12.34');
    expect(formatMetric(metric('7', { unit: 'clicks', currency: 'EUR' }))).toBe('€7.00');
    expect(formatMetric(metric(1500, { unit: 'Cost', currency: 'JPY' }))).toBe('¥1,500');
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

  it('formats counts and unknown units as counts', () => {
    expect(formatMetric(metric(1234, { unit: 'clicks' }))).toBe('1,234');
    expect(formatMetric(metric('0.3', { unit: 'conversions' }))).toBe('0.3');
    expect(formatMetric(metric('6.5', { unit: 'score' }))).toBe('6.5');
    expect(formatMetric(metric(12))).toBe('12');
    expect(formatMetric({ value: '12' })).toBe('12');
  });

  it('marks floors: a ceiling for money, a lower bound otherwise', () => {
    expect(
      formatMetric(metric('75', { unit: 'currency', currency: 'USD', floor: true })),
    ).toBe('≤ $75.00');
    expect(formatMetric(metric(12, { unit: 'contacts', floor: true }))).toBe('≥ 12');
    expect(formatMetric(metric(12, { unit: 'contacts', floor: false }))).toBe('12');
    expect(formatMetric(metric(Number.NaN, { unit: 'meetings', floor: true }))).toBe(UNAVAILABLE);
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
      '37 / 300 clicks',
    );
    expect(formatSample({ observed: 3, required: 5, unit: 'spending days' })).toBe(
      '3 / 5 spending days',
    );
    expect(formatSample({ observed: 3100, required: '4200', unit: 'form opens' })).toBe(
      '3,100 / 4,200 form opens',
    );
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
      '3,100 / 4,200 form opens · ≈ 52,000 impressions (projected through the observed open rate)',
    );
    expect(
      formatSample({
        observed: 1,
        required: 2,
        unit: 'variants',
        projected: { value: 3.5, unit: ' ', basis: '' },
      }),
    ).toBe('1 / 2 variants · ≈ 3.5');
  });

  it('omits a projection without a numeric value', () => {
    const base = { observed: 1, required: 2, unit: 'days' };
    expect(formatSample({ ...base, projected: { value: null, unit: 'days', basis: 'b' } })).toBe(
      '1 / 2 days',
    );
    expect(formatSample({ ...base, projected: { value: true, unit: 'days', basis: 'b' } })).toBe(
      '1 / 2 days',
    );
  });

  it('accepts a withheld sample with no unit', () => {
    expect(formatSample({ observed: 3, required: 5, unit: null })).toBe('3 / 5');
    expect(formatSample({ observed: '0.3', required: 10, unit: '  ' })).toBe('0.3 / 10');
  });

  it('returns an empty line when no sample applies', () => {
    expect(formatSample({ observed: null, required: null, unit: null })).toBe('');
    expect(formatSample({ observed: null, required: null, unit: 'days', projected: null })).toBe(
      '',
    );
  });

  it('renders a missing, boolean or pre-rendered figure without guessing', () => {
    expect(formatSample({ observed: null, required: 5, unit: 'days' })).toBe(
      'unavailable / 5 days',
    );
    expect(formatSample({ observed: 4, required: null, unit: 'days' })).toBe(
      '4 / unavailable days',
    );
    expect(formatSample({ observed: true, required: 'n/a', unit: null })).toBe('yes / n/a');
    expect(formatSample({ observed: false, required: 1, unit: null })).toBe('no / 1');
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
  } as const;
  const linkedin = {
    source: 'linkedin',
    start: '2026-09-04',
    end: '2026-10-01',
    boundary: 'UTC',
  } as const;

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

  it('renders an empty list as empty and a blank boundary as unavailable', () => {
    expect(formatSourceBoundaries([])).toBe('');
    expect(formatSourceBoundaries([{ source: 'google', boundary: ' ' }])).toBe(
      'Google days: unavailable',
    );
  });

  it('detects differing boundaries', () => {
    expect(boundariesDiffer([google, linkedin])).toBe(true);
    expect(boundariesDiffer([linkedin, { boundary: 'UTC' }])).toBe(false);
    expect(boundariesDiffer([linkedin])).toBe(false);
    expect(boundariesDiffer([])).toBe(false);
    expect(boundariesDiffer([{ boundary: ' ' }, { boundary: 'UTC' }])).toBe(true);
    expect(boundariesDiffer([{ boundary: ' UTC ' }, { boundary: 'UTC' }])).toBe(false);
  });
});

describe('formatWindowChip', () => {
  const window: EvidenceWindow = {
    start: '2026-09-04',
    end: '2026-10-01',
    days: 28,
    source_days: [
      { source: 'google', start: '2026-09-04', end: '2026-10-01', boundary: 'America/New_York' },
      { source: 'linkedin', start: '2026-09-04', end: '2026-10-01', boundary: 'UTC' },
    ],
  };

  it('renders the card chip example exactly', () => {
    expect(formatWindowChip(window)).toBe(
      '2026-09-04 – 2026-10-01 · 28 days · Google days: America/New_York · LinkedIn days: UTC',
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
