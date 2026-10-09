/**
 * Display formatting for the Ads Action Engine SPA (AAP 0.5.5 Behaviour): amounts in the
 * account currency, counts, percentages, evidence metrics, sample and test lines,
 * source-labelled daily dates, and instants in `DISPLAY_TIMEZONE`.
 *
 * Three rules hold for every function here:
 *
 * - **No cross-currency aggregation.** Each money function formats one amount in one currency.
 *   The module offers nothing that adds, compares, converts or takes a median of amounts across
 *   currencies, because amounts are shown per account currency and never combined
 *   (AAP 0.1.1).
 * - **Money from strings, never through `Number()`.** Amounts arrive as fixed-point decimal
 *   strings ({@link Money}) and are handed to `Intl.NumberFormat` as strings, which rounds the
 *   exact decimal: `"999999999999.994999"` USD renders `$999,999,999,999.99`, while a `Number`
 *   round trip would render `$1,000,000,000,000.00`. Money is never converted to a binary
 *   float, with no exception: the quotient of {@link formatCost} is exact decimal arithmetic on
 *   `BigInt`, handed to `Intl` as a decimal string.
 * - **Dates as stored.** A daily date is the source's native calendar date (Google in the
 *   account's time zone, LinkedIn in UTC). It is printed exactly as stored and labelled with its
 *   source's day boundary, and is never parsed into a `Date`, which would shift it in other
 *   zones. Only instants are converted, into the display time zone the caller passes
 *   (AAP 0.5.4.1).
 *
 * Every function is pure and deterministic: output always uses the fixed locale
 * {@link LOCALE}, never the browser's, and the only module state is a cache of `Intl` formatter
 * instances keyed by their options. A value that cannot be formatted renders
 * {@link UNAVAILABLE}, never `0` (AAP 0.11.1).
 *
 * @example
 * ```ts
 * import { formatCost, formatInstant, formatMoney, formatWindowChip } from '../lib/format';
 *
 * formatMoney('1234.567891', 'EUR'); // "€1,234.57"
 * formatCost('300.000000', 0, 'USD', 'no QL yet'); // "— (no QL yet)"
 * formatWindowChip(item.evidence.window);
 * // "2026-09-04 – 2026-10-01 · 28 days · Google days: America/New_York · LinkedIn days: UTC"
 * formatInstant(run.finished_at, run.display_timezone); // "Oct 6, 2026, 11:22 AM America/New_York"
 * ```
 */
import type {
  EvidenceWindow,
  IsoDate,
  IsoDateTime,
  Metric,
  MetricValue,
  Money,
  Projection,
  Sample,
  SourceDay,
  SourceName,
  StatTest,
} from '../api/types';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/**
 * The one locale every formatter uses, so output never depends on the browser's locale and
 * tests are deterministic. The product ships in English only (AAP 0.7.2).
 */
export const LOCALE = 'en';

/**
 * The text of a value that is absent or cannot be formatted. An unavailable value is never
 * rendered as `0` (AAP 0.5.4.7, 0.11.1: absent values are "unavailable").
 */
export const UNAVAILABLE = 'unavailable';

/**
 * The em dash shown for a cost whose known denominator is zero, such as cost per Qualified Lead
 * with no QL yet (AAP 0.5.4.7), or cost/conversion when conversions are 0 (AAP 0.5.4.3).
 */
export const EM_DASH = '—';

/**
 * The text of a measure that does not apply, such as a lifetime budget's utilization
 * (AAP 0.5.4.1).
 */
export const NOT_APPLICABLE = 'n/a';

/** The zone an instant falls back to when the display time zone is not a valid IANA id. */
const FALLBACK_TIMEZONE = 'UTC';

/** The most fraction digits any formatter is asked for; larger requests are clamped. */
const MAX_FRACTION_DIGITS = 20;

/** Minor-unit digits assumed for an amount whose currency is missing or not recognised. */
const DEFAULT_MINOR_DIGITS = 2;

/**
 * The highest power-of-ten place, and the negated lowest, that a non-zero digit of a
 * {@link formatCost} operand or quotient may occupy. Every accepted value then lies below
 * 10^308, inside the finite range of a JSON number (`Number.MAX_VALUE` ≈ 1.8 × 10^308), so
 * `Intl.NumberFormat` renders it as a finite figure rather than `∞`, and the exact `BigInt`
 * arithmetic never exceeds about 640 digits. A value with a digit outside the 10^-307 to
 * 10^307 places, such as `"1e400"` or `"1e999999999"`, is unavailable, and is rejected before
 * any power of ten is built.
 */
const MAX_EXACT_PLACE = 307;

/** Fraction digits of a per-unit-of-spend rate: the six places the backend records it at. */
const RATE_DIGITS = 6;

/** Below this a p-value renders as "< 0.001". */
const P_VALUE_FLOOR = 0.001;

/** Fraction digits of a p-value at or above {@link P_VALUE_FLOOR}. */
const P_VALUE_DIGITS = 3;

/** Fraction digits of a significance level α. */
const ALPHA_DIGITS = 3;

/** Separator between the segments of a chip or line: a middle dot (U+00B7) with spaces. */
const SEPARATOR = ' · ';

/** Separator between the two ends of a date range: an en dash (U+2013) with spaces. */
const RANGE_SEPARATOR = ' – ';

/** Rendering of a boolean evidence value. */
const YES = 'yes';
const NO = 'no';

/** A plain decimal number as a string: optional sign, digits, optional fraction and exponent. */
const DECIMAL_PATTERN = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/;

/** Text that already states a bound or approximation, which a floor marker must not repeat. */
const BOUND_PREFIX = /^[≥≤<>≈]/;

/** Spaces some ICU versions emit inside formatted output: U+202F and U+00A0. */
const SPECIAL_SPACES = /[\u202F\u00A0]/g;

// ---------------------------------------------------------------------------------------------
// Input types
// ---------------------------------------------------------------------------------------------

/**
 * A numeric field as it may arrive from evidence or a form: a decimal string (the backend
 * serialises `Decimal` as a fixed-point string), a JSON number, or `null`/`undefined` when the
 * value is absent. Every numeric formatter accepts all of these and renders an absent or
 * non-numeric value as {@link UNAVAILABLE}.
 */
export type NumericInput = number | string | null | undefined;

/** What `Intl.NumberFormat.prototype.format` takes exactly: a finite number or a decimal string. */
type Formattable = number | `${number}`;

/**
 * The fields of an evidence {@link Metric} that {@link formatMetric} reads. A full `Metric`
 * satisfies it, and so does a baseline paired with its metric's unit and currency.
 */
export type MetricInput = { value: MetricValue | undefined } & Partial<
  Pick<Metric, 'unit' | 'currency' | 'floor' | 'unavailable'>
>;

/**
 * The fields of a sample line that {@link formatSample} reads: an evidence {@link Sample}, or a
 * withheld pattern's sample, whose unit may be `null` and which has no projection.
 */
export type SampleInput = Pick<Sample, 'observed' | 'required'> & {
  unit?: string | null;
  projected?: Projection | null;
};

/** The fields of a {@link StatTest} that {@link formatTestLine} reads. */
export type StatTestInput = Pick<StatTest, 'name' | 'p_value'> &
  Partial<Pick<StatTest, 'p_adjusted' | 'alpha'>>;

/**
 * One source's day boundary, with its own date range when the caller has it: a
 * {@link SourceDay}, or just its `source` and `boundary`.
 */
export type SourceDayInput = Pick<SourceDay, 'source' | 'boundary'> &
  Partial<Pick<SourceDay, 'start' | 'end'>>;

// ---------------------------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------------------------

/**
 * Whether `value`, once trimmed, is a plain decimal number such as `"120.500000"`, `"-3"`,
 * `".5"` or `"1e-7"`. A match is a `StringNumericLiteral`, which `Intl.NumberFormat` formats
 * exactly, so money never passes through a binary float (AAP 0.5.2 money precision).
 *
 * @param value - The string to test.
 * @returns `true` when it is a decimal number; `false` for text such as `"n/a"` or `""`.
 */
export function isDecimalString(value: string): value is `${number}` {
  return DECIMAL_PATTERN.test(value.trim());
}

/** Returns a finite number or a trimmed decimal string, or `null` for anything unformattable. */
function toFormattable(value: NumericInput): Formattable | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    return isDecimalString(trimmed) ? trimmed : null;
  }
  return null;
}

/** Narrows an evidence value to a numeric input; a boolean is not a number. */
function asNumeric(value: MetricValue | undefined): NumericInput {
  return typeof value === 'boolean' ? null : value;
}

/** Replaces the narrow no-break space (U+202F) and no-break space (U+00A0) with a plain space. */
function normalizeSpaces(text: string): string {
  return text.replace(SPECIAL_SPACES, ' ');
}

/** Returns the trimmed text, or `null` when it is missing or blank. */
function textOrNull(text: string | null | undefined): string | null {
  if (typeof text !== 'string') {
    return null;
  }
  const trimmed = text.trim();
  return trimmed === '' ? null : trimmed;
}

/** A validated decimal string split into its unsigned mantissa and its base-10 exponent. */
interface DecimalParts {
  readonly mantissa: string;
  readonly exponent: number;
}

/** Splits a validated decimal string such as `"-1.5e3"` into `"1.5"` and `3`. */
function splitDecimal(value: `${number}`): DecimalParts {
  const unsigned = value.replace(/^[+-]/, '');
  const exponentAt = unsigned.search(/[eE]/);
  if (exponentAt === -1) {
    return { mantissa: unsigned, exponent: 0 };
  }
  return {
    mantissa: unsigned.slice(0, exponentAt),
    exponent: Number(unsigned.slice(exponentAt + 1)),
  };
}

/** Whether a formattable value is exactly zero, including `-0` and `"0.000"`. */
function isZero(value: Formattable): boolean {
  if (typeof value === 'number') {
    return value === 0;
  }
  return !/[1-9]/.test(splitDecimal(value).mantissa);
}

/** Whether a formattable value is below zero; `-0` and `"-0.00"` are not. */
function isNegative(value: Formattable): boolean {
  return typeof value === 'number' ? value < 0 : value.startsWith('-') && !isZero(value);
}

/**
 * Whether a formattable value has a non-zero fractional part. Decimal strings are inspected
 * digit by digit, so `"9007199254740993.0"` is whole and `"1e-1"` is fractional without a
 * float conversion: the digits after the decimal point, once moved by the exponent, decide.
 */
function hasFraction(value: Formattable): boolean {
  if (typeof value === 'number') {
    return !Number.isInteger(value);
  }
  const { mantissa, exponent } = splitDecimal(value);
  const pointAt = mantissa.indexOf('.');
  const wholeDigits = pointAt === -1 ? mantissa.length : pointAt;
  const digits = mantissa.replace('.', '');
  return /[1-9]/.test(digits.slice(Math.max(wholeDigits + exponent, 0)));
}

/** An exact decimal value, `coefficient × 10^exponent`, held without any binary float. */
interface ExactDecimal {
  readonly coefficient: bigint;
  readonly exponent: number;
}

/**
 * Converts a formattable value to an exact decimal whose coefficient carries the sign and no
 * trailing zeros: `"-1.50e3"` → `-15n × 10^2`, `"0.075"` → `75n × 10^-3`. Zero in any form
 * (`0`, `-0`, `"0.000"`, `"0e999"`) is `0n × 10^0`. A number is read as `String(value)`, the
 * shortest decimal that round-trips it (`0.1` → `"0.1"`, `1e21` → `"1e+21"`), so a JSON number
 * counts as the decimal it was written as.
 *
 * The bounds are checked on the digit text before any `BigInt` is built, so a pathological
 * exponent or a very long digit string costs only a scan.
 *
 * @param value - A finite number or a validated decimal string.
 * @returns The exact decimal, or `null` when a non-zero digit lies outside the places
 *   ±{@link MAX_EXACT_PLACE}.
 */
function toExactDecimal(value: Formattable): ExactDecimal | null {
  const text: `${number}` = typeof value === 'number' ? `${value}` : value;
  const { mantissa, exponent } = splitDecimal(text);
  const pointAt = mantissa.indexOf('.');
  const fraction = pointAt === -1 ? 0 : mantissa.length - pointAt - 1;
  const digits = mantissa.replace('.', '').replace(/^0+/, '');
  const significant = digits.replace(/0+$/, '');
  if (significant === '') {
    return { coefficient: 0n, exponent: 0 };
  }
  const lowest = exponent - fraction + (digits.length - significant.length);
  const highest = lowest + significant.length - 1;
  if (lowest < -MAX_EXACT_PLACE || highest > MAX_EXACT_PLACE) {
    return null;
  }
  const magnitude = BigInt(significant);
  return { coefficient: text.startsWith('-') ? -magnitude : magnitude, exponent: lowest };
}

/**
 * The quotient `spend / denominator` as a decimal string, computed exactly in `BigInt`, for
 * {@link formatMoney} to round at `places` fraction digits.
 *
 * Rounding contract: the string holds the exact quotient truncated toward zero at `places + 1`
 * fraction digits, followed by one sticky digit `1` when the division leaves a non-zero
 * remainder, with the quotient's sign: at 2 places `"0.075"` / 3 → `"0.025"` and `"-1"` / 3 →
 * `"-0.3331"`; at 0 places `"1000"` / 3 → `"333.31"`. It agrees with the exact quotient on
 * every digit up to `places + 1` and records whether anything non-zero follows, so the single
 * rounding `Intl.NumberFormat` or {@link formatDecimal} then applies at `places` digits or
 * fewer equals rounding the exact quotient: the same rounding {@link formatMoney} gives a cost
 * the backend stored. The sticky digit also keeps a tiny non-zero quotient non-zero, so
 * formatDecimal's `"< 0.01"` bound still fires for it.
 *
 * @param spend - The exact spend.
 * @param denominator - The exact denominator, above zero.
 * @param places - The fraction digits the result is rounded to: the currency's minor digits.
 * @returns The quotient text, or `null` when its integer part reaches past the
 *   {@link MAX_EXACT_PLACE} place, where `Intl` could no longer render it as a finite figure.
 */
function exactQuotientText(
  spend: ExactDecimal,
  denominator: ExactDecimal,
  places: number,
): string | null {
  const kept = places + 1;
  const shift = spend.exponent - denominator.exponent + kept;
  const magnitude = spend.coefficient < 0n ? -spend.coefficient : spend.coefficient;
  const numerator = shift >= 0 ? magnitude * 10n ** BigInt(shift) : magnitude;
  const divisor =
    shift >= 0 ? denominator.coefficient : denominator.coefficient * 10n ** BigInt(-shift);
  const digits = (numerator / divisor).toString().padStart(kept + 1, '0');
  const whole = digits.slice(0, -kept);
  if (whole.length - 1 > MAX_EXACT_PLACE) {
    return null;
  }
  const sign = spend.coefficient < 0n ? '-' : '';
  const sticky = numerator % divisor === 0n ? '' : '1';
  return `${sign}${whole}.${digits.slice(-kept)}${sticky}`;
}

/** Clamps a requested digit count to an integer `Intl` accepts; `NaN` becomes 0. */
function fractionDigits(digits: number): number {
  if (Number.isNaN(digits)) {
    return 0;
  }
  return Math.min(Math.max(Math.trunc(digits), 0), MAX_FRACTION_DIGITS);
}

/** The smallest step at `digits` fraction digits: `"1"`, `"0.1"`, `"0.01"`, `"0.001"` and so on. */
function stepText(digits: number): string {
  return digits === 0 ? '1' : `0.${'0'.repeat(digits - 1)}1`;
}

// ---------------------------------------------------------------------------------------------
// Formatter caches. Cards render hundreds of numbers, so each `Intl` instance is built once per
// option set and reused. The keys are bounded in practice: a handful of digit counts, the
// account currencies and the display time zone.
// ---------------------------------------------------------------------------------------------

const numberFormats = new Map<string, Intl.NumberFormat>();

/** `null` caches a currency code `Intl` refuses, so the refusal is not retried on every render. */
const currencyFormats = new Map<string, Intl.NumberFormat | null>();

/** An instant formatter and the zone label printed after its output. */
interface InstantFormat {
  readonly formatter: Intl.DateTimeFormat;
  readonly label: string;
}

const instantFormats = new Map<string, InstantFormat>();

/**
 * A decimal or percent formatter with grouping and the given fraction digits. `signDisplay:
 * 'negative'` keeps the minus sign off a value that rounds to zero, so `-0.004` never renders
 * `"-0"`.
 */
function numberFormat(
  style: 'decimal' | 'percent',
  minimumFractionDigits: number,
  maximumFractionDigits: number,
): Intl.NumberFormat {
  const key = `${style}|${minimumFractionDigits}|${maximumFractionDigits}`;
  const cached = numberFormats.get(key);
  if (cached !== undefined) {
    return cached;
  }
  const created = new Intl.NumberFormat(LOCALE, {
    style,
    minimumFractionDigits,
    maximumFractionDigits,
    signDisplay: 'negative',
  });
  numberFormats.set(key, created);
  return created;
}

/**
 * The currency formatter for an ISO 4217 code, which fixes the currency's own minor digits
 * (JPY 0, USD 2, KWD 3). Returns `null` when `Intl` refuses the code: a malformed code such as
 * `"EURO"` makes the constructor throw `RangeError`. A well-formed but unknown code such as
 * `"XYZ"` is accepted and printed as its code.
 */
function currencyFormat(code: string): Intl.NumberFormat | null {
  const key = code.toUpperCase();
  const cached = currencyFormats.get(key);
  if (cached !== undefined) {
    return cached;
  }
  let created: Intl.NumberFormat | null;
  try {
    created = new Intl.NumberFormat(LOCALE, {
      style: 'currency',
      currency: key,
      currencyDisplay: 'symbol',
      signDisplay: 'negative',
    });
  } catch {
    // The only way this constructor fails with these options is a currency code Intl refuses.
    created = null;
  }
  currencyFormats.set(key, created);
  return created;
}

/** Builds an instant formatter for an IANA zone; throws `RangeError` for an invalid zone. */
function dateTimeFormat(timeZone: string): Intl.DateTimeFormat {
  return new Intl.DateTimeFormat(LOCALE, { timeZone, dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * The instant formatter for a display zone, labelled with the zone as given. An invalid or
 * blank zone (for example `"Mars/Base"`) falls back to UTC and is labelled `"UTC"`, so the
 * output always names the zone it was actually formatted in.
 */
function instantFormat(zone: string): InstantFormat {
  const cached = instantFormats.get(zone);
  if (cached !== undefined) {
    return cached;
  }
  let created: InstantFormat;
  try {
    created = { formatter: dateTimeFormat(zone), label: zone };
  } catch {
    // Intl rejects the zone id with RangeError: format in UTC and say so in the label.
    created = { formatter: dateTimeFormat(FALLBACK_TIMEZONE), label: FALLBACK_TIMEZONE };
  }
  instantFormats.set(zone, created);
  return created;
}

// ---------------------------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------------------------

const SOURCE_LABELS: Readonly<Record<SourceName, string>> = {
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
};

/**
 * The display name of a data source: `"Google"`, `"LinkedIn"` or `"HubSpot"` (AAP 0.5.5 source
 * chips and day-boundary labels). An unknown source string received at runtime is returned
 * unchanged, so a source added to the API later never breaks a render.
 *
 * @param source - The source name from the API.
 * @returns The display name.
 */
export function sourceLabel(source: SourceName): string {
  return Object.hasOwn(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : String(source);
}

// ---------------------------------------------------------------------------------------------
// Numbers and money
// ---------------------------------------------------------------------------------------------

/**
 * Formats one amount in one account currency (AAP 0.5.5 Behaviour: "Numbers are formatted in
 * the account currency"), rounded to the currency's minor unit from the exact decimal string.
 *
 * - `formatMoney("1234.567891", "EUR")` → `"€1,234.57"`; `("40", "GBP")` → `"£40.00"`;
 *   `("1234.5", "JPY")` → `"¥1,235"`; `("-40", "EUR")` → `"-€40.00"`.
 * - A missing or blank currency renders the plain decimal with no symbol, so no currency is
 *   ever implied: `("40.5", null)` → `"40.5"`.
 * - A code `Intl` refuses renders the decimal followed by the code: `("5", "EURO")` →
 *   `"5 EURO"`. A well-formed unknown code formats normally: `("5", "XYZ")` → `"XYZ 5.00"`.
 * - An absent or non-numeric value renders {@link UNAVAILABLE}.
 *
 * Each call formats a single amount; nothing in this module sums, compares or converts amounts
 * across currencies (AAP 0.1.1).
 *
 * @param value - The amount: a {@link Money} decimal string, or a number for a derived figure.
 * @param currency - The account's ISO 4217 currency code.
 * @returns The formatted amount.
 */
export function formatMoney(
  value: Money | NumericInput,
  currency: string | null | undefined,
): string {
  const amount = toFormattable(value);
  if (amount === null) {
    return UNAVAILABLE;
  }
  const code = textOrNull(currency);
  if (code === null) {
    return formatDecimal(amount, DEFAULT_MINOR_DIGITS);
  }
  const formatter = currencyFormat(code);
  if (formatter === null) {
    return `${formatDecimal(amount, DEFAULT_MINOR_DIGITS)} ${code}`;
  }
  return normalizeSpaces(formatter.format(amount));
}

/**
 * The number of minor-unit digits of a currency: USD 2, EUR 2, JPY 0, KWD 3, BHD 3. The
 * decision form uses it for the amount input's step and its minor-unit hint, because the API
 * rejects an amount finer than the currency's minor unit with 422 (AAP 0.5.2). A missing or
 * refused code returns 2.
 *
 * @param currency - The ISO 4217 currency code.
 * @returns The currency's fraction digits.
 */
export function currencyMinorDigits(currency: string | null | undefined): number {
  const code = textOrNull(currency);
  const formatter = code === null ? null : currencyFormat(code);
  if (formatter === null) {
    return DEFAULT_MINOR_DIGITS;
  }
  return formatter.resolvedOptions().maximumFractionDigits ?? DEFAULT_MINOR_DIGITS;
}

/**
 * The currency's minor unit as an `<input step>` value, built from
 * {@link currencyMinorDigits}: `"0.01"` for 2 digits, `"1"` for 0 and `"0.001"` for 3
 * (AAP 0.5.2: amounts are validated to the currency's minor unit).
 *
 * @param currency - The ISO 4217 currency code.
 * @returns The step text.
 */
export function currencyStep(currency: string | null | undefined): string {
  return stepText(currencyMinorDigits(currency));
}

/**
 * Formats a plain decimal with grouping and at most `digits` fraction digits, without padding:
 * `0.3` → `"0.3"`, `2.456` → `"2.46"`, `12345.6` → `"12,345.6"`.
 *
 * A non-zero value that would round to zero renders as a bound instead, so a non-zero figure
 * such as a fractional conversion never reads as 0: `0.004` → `"< 0.01"`, `-0.004` →
 * `"> -0.01"`. An absent or non-numeric value renders {@link UNAVAILABLE}.
 *
 * @param value - The number or decimal string.
 * @param digits - The maximum fraction digits, clamped to 0–20. Defaults to 2.
 * @returns The formatted decimal.
 */
export function formatDecimal(value: NumericInput, digits = 2): string {
  const amount = toFormattable(value);
  if (amount === null) {
    return UNAVAILABLE;
  }
  const places = fractionDigits(digits);
  const text = normalizeSpaces(numberFormat('decimal', 0, places).format(amount));
  if (!isZero(amount) && !/[1-9]/.test(text)) {
    const step = stepText(places);
    return isNegative(amount) ? `> -${step}` : `< ${step}`;
  }
  return text;
}


/**
 * Formats a count: a whole value with grouping and no fraction digits (`1234` → `"1,234"`;
 * the string `"9007199254740993"` keeps every digit).
 *
 * A fractional value is never rounded to an integer, because Google conversions can be
 * fractional (AAP 0.2.4) and K1 treats 0.3 conversions as converting (AAP 0.5.4.3). It renders
 * through {@link formatDecimal} at 2 digits (`0.3` → `"0.3"`, `0.004` → `"< 0.01"`), and a
 * fraction that rounds to a whole number keeps its two places (`0.999` → `"1.00"`). An absent or
 * non-numeric value renders {@link UNAVAILABLE}.
 *
 * @param value - The count.
 * @returns The formatted count.
 */
export function formatCount(value: NumericInput): string {
  const amount = toFormattable(value);
  if (amount === null) {
    return UNAVAILABLE;
  }
  if (!hasFraction(amount)) {
    return normalizeSpaces(numberFormat('decimal', 0, 0).format(amount));
  }
  const text = formatDecimal(amount, 2);
  if (text.includes('.') || BOUND_PREFIX.test(text)) {
    return text;
  }
  return normalizeSpaces(numberFormat('decimal', 2, 2).format(amount));
}

/**
 * Formats a 0–1 ratio as a percentage with at most `digits` fraction digits: `0.123` →
 * `"12.3%"`, `"0.5"` → `"50%"`, `1.4` → `"140%"`. Utilization, shares and stage rates use it
 * (AAP 0.5.4.1, 0.5.4.6). Decimal strings are formatted exactly, without a float conversion.
 *
 * A non-zero ratio that would round to `"0%"` renders as a bound: `0.0002` → `"< 0.1%"`,
 * `-0.0002` → `"> -0.1%"`. An absent or non-numeric value renders {@link UNAVAILABLE}.
 *
 * @param ratio - The ratio, where 1 is 100%.
 * @param digits - The maximum fraction digits of the percentage, clamped to 0–20. Defaults to 1.
 * @returns The formatted percentage.
 */
export function formatPercent(ratio: NumericInput, digits = 1): string {
  const amount = toFormattable(ratio);
  if (amount === null) {
    return UNAVAILABLE;
  }
  const places = fractionDigits(digits);
  const text = normalizeSpaces(numberFormat('percent', 0, places).format(amount));
  if (!isZero(amount) && !/[1-9]/.test(text)) {
    const step = stepText(places);
    return isNegative(amount) ? `> -${step}%` : `< ${step}%`;
  }
  return text;
}

/**
 * Formats a cost per outcome, such as cost per Qualified Lead or cost per meeting booked, from
 * a spend and a count (AAP 0.5.4.7).
 *
 * - A denominator that is absent, non-numeric, non-finite, out of range (a digit beyond the
 *   10^307 or 10^-307 place, such as `"1e400"`) or negative renders {@link UNAVAILABLE}, even
 *   when a reason is given: an unknown count is unavailable evidence, never a known zero
 *   (AAP 0.5.4.7, 0.11.1).
 * - A denominator known to be zero (`0`, `-0`, `"0.000"`) renders {@link EM_DASH}, or
 *   `"— ({emptyReason})"` when a reason is given: `"— (no QL yet)"`, `"— (no meetings yet)"`.
 *   The spend is not consulted. It never renders a finite value or a "≤ X" ceiling, because a
 *   cost exists only when the known denominator is above 0 (AAP 0.5.4.7; cost/conversion "—"
 *   when conversions are 0, AAP 0.5.4.3).
 * - Otherwise the quotient spend / denominator is computed exactly in decimal arithmetic, never
 *   through a binary float, and formatted in the currency, rounded once at the currency's minor
 *   digits as {@link formatMoney} rounds a stored amount: `("300", 4, "USD")` → `"$75.00"`,
 *   `("0.075", 3, "USD")` → `"$0.03"`, `("999999999999.994999", 1, "USD")` →
 *   `"$999,999,999,999.99"`. A cost the backend already computed, as evidence carries it,
 *   renders through {@link formatMoney} or {@link formatMetric} instead.
 * - A spend that is absent, non-numeric, non-finite or out of range renders
 *   {@link UNAVAILABLE}, and so does a quotient of 10^308 or more, which no finite figure shows.
 *
 * @param spend - The spend: a {@link Money} decimal string, or a number read as its shortest
 *   decimal.
 * @param denominator - The count the spend is divided by.
 * @param currency - The spend's ISO 4217 currency code.
 * @param emptyReason - Why the denominator is zero, shown after the dash.
 * @returns The formatted cost.
 */
export function formatCost(
  spend: Money | NumericInput,
  denominator: NumericInput,
  currency: string | null | undefined,
  emptyReason?: string,
): string {
  const divisor = toFormattable(denominator);
  if (divisor === null || isNegative(divisor)) {
    return UNAVAILABLE;
  }
  if (isZero(divisor)) {
    const reason = textOrNull(emptyReason);
    return reason === null ? EM_DASH : `${EM_DASH} (${reason})`;
  }
  const count = toExactDecimal(divisor);
  if (count === null) {
    return UNAVAILABLE;
  }
  const amount = toFormattable(spend);
  const exact = amount === null ? null : toExactDecimal(amount);
  if (exact === null) {
    return UNAVAILABLE;
  }
  const quotient = exactQuotientText(exact, count, currencyMinorDigits(currency));
  return quotient === null ? UNAVAILABLE : formatMoney(quotient, currency);
}

/**
 * Marks a formatted value as an attribution floor (AAP 0.5.4.7): `"≥ N"` for a floored count
 * (`'min'`: MQL, QL, meetings, deals) and `"≤ X"` for a floored cost (`'max'`).
 *
 * The text is returned unchanged when a marker would be wrong or repeated:
 * - it already starts with a bound or approximation (`≥`, `≤`, `<`, `>`, `≈`), as when the
 *   backend rendered the floor itself, so a prefix is never doubled;
 * - it is {@link UNAVAILABLE}, {@link NOT_APPLICABLE} or starts with {@link EM_DASH}: a
 *   zero-denominator cost stays `"—"` under a floor, because "≤ X" applies only when the known
 *   denominator is above 0;
 * - it holds no digit at all, such as `"unknown (deal search incomplete)"`.
 *
 * @param text - The formatted value.
 * @param kind - `'min'` for a count's lower bound, `'max'` for a cost's upper bound.
 * @returns The value with its floor marker.
 */
export function formatFloor(text: string, kind: 'min' | 'max'): string {
  const trimmed = text.trim();
  if (
    trimmed === UNAVAILABLE ||
    trimmed === NOT_APPLICABLE ||
    trimmed.startsWith(EM_DASH) ||
    BOUND_PREFIX.test(trimmed) ||
    !/\d/.test(trimmed)
  ) {
    return text;
  }
  return `${kind === 'min' ? '≥' : '≤'} ${trimmed}`;
}

// ---------------------------------------------------------------------------------------------
// Evidence metrics, samples and tests
// ---------------------------------------------------------------------------------------------

/**
 * Metric units whose value is an amount of money, formatted by {@link formatMoney}
 * (AAP 0.5.4.8 "with units and currency"). The backend emits `currency`. A metric with a
 * `currency` code is money whatever its unit.
 */
export const MONEY_UNITS: ReadonlySet<string> = new Set([
  'money',
  'currency',
  'amount',
  'cost',
  'spend',
]);

/**
 * Metric units whose value is a ratio where 1 is 100%, formatted by {@link formatPercent}. The
 * backend emits `ratio` for shares, utilization, stage rates with their Wilson bounds, and
 * relative ratios such as CPC second ÷ first half (`1.15` → `"115%"`).
 */
export const RATIO_UNITS: ReadonlySet<string> = new Set([
  'ratio',
  'share',
  'utilization',
  'fraction',
]);

/**
 * Metric units whose value is a rate per unit of exposure rather than a 0–1 ratio: B4's
 * outcomes per unit of spend (backend unit `rate`, recorded at six places). They render as a
 * plain decimal at those six places (`"0.012345"`), because a percentage would misstate an
 * outcomes-per-currency-unit rate as a share.
 */
export const RATE_UNITS: ReadonlySet<string> = new Set(['rate']);

/** Metric units whose value is already in percent (`79.1` → `"79.1%"`). */
export const PERCENT_POINT_UNITS: ReadonlySet<string> = new Set(['percent', 'pct', '%']);

/** Metric units whose value is a day count, formatted by {@link formatDays}. */
export const DAY_UNITS: ReadonlySet<string> = new Set(['days', 'day']);

/** Metric units whose value is a p-value, formatted by {@link formatPValue}. */
export const P_VALUE_UNITS: ReadonlySet<string> = new Set(['p', 'p_value']);

/** Metric units whose value is a multiple, formatted as `"1.5×"`. */
export const MULTIPLE_UNITS: ReadonlySet<string> = new Set(['x', 'multiple', 'ratio_x']);

/**
 * Metric units whose value is text shown verbatim, even when it looks numeric: the backend's
 * `text` unit carries a search term or matched keyword, so a term such as `"2024"` must never be
 * regrouped into `"2,024"` (AAP 0.5.4.3 "term").
 */
export const TEXT_UNITS: ReadonlySet<string> = new Set(['text']);

/** A unit compared case-insensitively and without surrounding space; `null` becomes `''`. */
function normalizeUnit(unit: string | null | undefined): string {
  return typeof unit === 'string' ? unit.trim().toLowerCase() : '';
}

/** Appends a suffix to a formatted figure unless the figure is unavailable. */
function withSuffix(text: string, suffix: string): string {
  return text === UNAVAILABLE ? text : `${text}${suffix}`;
}

/** Formats a numeric metric value that is not money, by its unit. */
function formatByUnit(value: number | string, unit: string): string {
  if (RATIO_UNITS.has(unit)) {
    return formatPercent(value);
  }
  if (RATE_UNITS.has(unit)) {
    return formatDecimal(value, RATE_DIGITS);
  }
  if (PERCENT_POINT_UNITS.has(unit)) {
    return withSuffix(formatDecimal(value, 1), '%');
  }
  if (DAY_UNITS.has(unit)) {
    return formatDays(value);
  }
  if (P_VALUE_UNITS.has(unit)) {
    return formatPValue(value);
  }
  if (MULTIPLE_UNITS.has(unit)) {
    return withSuffix(formatDecimal(value, 2), '×');
  }
  return formatCount(value);
}


/**
 * Formats one evidence metric for the card's evidence table (AAP 0.5.4.8: every number the
 * reason cites, with units and currency). The checks run in this order:
 *
 * 1. `unavailable: true`, or a `null`/`undefined` value → {@link UNAVAILABLE}, never `"0"`.
 * 2. A boolean value → `"yes"` or `"no"`.
 * 3. A {@link TEXT_UNITS} unit, or a string that is not a decimal number, is returned verbatim:
 *    search terms, and text the backend rendered itself such as `"n/a"`, `"—"`, `"≥ 12"` or
 *    `"unknown (3 deals not assessed)"`, which is never re-parsed.
 * 4. Money (a `currency` code, or a {@link MONEY_UNITS} unit) → {@link formatMoney}.
 * 5. {@link RATIO_UNITS} → {@link formatPercent}; {@link RATE_UNITS} → a six-place decimal;
 *    {@link PERCENT_POINT_UNITS} → the decimal plus `%`; {@link DAY_UNITS} →
 *    {@link formatDays}; {@link P_VALUE_UNITS} → {@link formatPValue};
 *    {@link MULTIPLE_UNITS} → the decimal plus `×`.
 * 6. Any other unit, such as `clicks`, `leads` or `contacts` → {@link formatCount}, which keeps
 *    fractional values as decimals. The metric's label carries the meaning, so no unit word is
 *    appended.
 *
 * A `floor: true` metric is marked by {@link formatFloor}: `"≤ X"` for money, because floored
 * costs are ceilings, and `"≥ N"` otherwise (AAP 0.5.4.7). Text returned at steps 1–3 is never
 * marked.
 *
 * @param metric - The metric, or any value paired with a unit and currency.
 * @returns The display text.
 */
export function formatMetric(metric: MetricInput): string {
  const { value } = metric;
  if (metric.unavailable === true || value === null || value === undefined) {
    return UNAVAILABLE;
  }
  if (typeof value === 'boolean') {
    return value ? YES : NO;
  }
  const unit = normalizeUnit(metric.unit);
  if (TEXT_UNITS.has(unit) || (typeof value === 'string' && !isDecimalString(value))) {
    return String(value);
  }
  const currency = textOrNull(metric.currency);
  const isMoney = currency !== null || MONEY_UNITS.has(unit);
  const text = isMoney ? formatMoney(value, currency) : formatByUnit(value, unit);
  return metric.floor === true ? formatFloor(text, isMoney ? 'max' : 'min') : text;
}

/**
 * Formats a day count with its unit: `"1 day"`, `"28 days"` (AAP 0.5.4.1 windows and the
 * day counts of budget evidence). An absent or non-numeric value renders {@link UNAVAILABLE}.
 *
 * @param days - The number of days.
 * @returns The formatted day count.
 */
export function formatDays(days: NumericInput): string {
  const amount = toFormattable(days);
  if (amount === null) {
    return UNAVAILABLE;
  }
  return `${formatCount(amount)} ${Number(amount) === 1 ? 'day' : 'days'}`;
}

/** Formats one figure of a sample line: a count, verbatim text, or unavailable. */
function formatSampleValue(value: MetricValue | undefined): string {
  if (value === null || value === undefined) {
    return UNAVAILABLE;
  }
  if (typeof value === 'boolean') {
    return value ? YES : NO;
  }
  return typeof value === 'string' && !isDecimalString(value) ? value : formatCount(value);
}

/** Formats a labelled projection, `"≈ 4,200 impressions (projected …)"`; `''` without value. */
function formatProjection(projected: Projection | null | undefined): string {
  if (projected === null || projected === undefined) {
    return '';
  }
  const { value } = projected;
  if (value === null || value === undefined || typeof value === 'boolean') {
    return '';
  }
  const unit = textOrNull(projected.unit);
  const basis = textOrNull(projected.basis);
  let text = `≈ ${formatSampleValue(value)}`;
  if (unit !== null) {
    text += ` ${unit}`;
  }
  if (basis !== null) {
    text += ` (${basis})`;
  }
  return text;
}

/**
 * Formats the sample line "observed / required" with its unit (AAP 0.5.5 card body; AAP 0.5.4.2
 * P6 withheld patterns carry their sample sizes): `"37 / 300 clicks"`, `"3 / 5 spending days"`.
 *
 * - Counts render through {@link formatCount}; the unit is appended when present.
 * - A projection is labelled as one (AAP 0.5.4.6 CR3): `"3,100 / 4,200 form opens ·
 *   ≈ 52,000 impressions (projected through the observed open rate)"`.
 * - Both evidence samples and withheld samples (unit possibly `null`, no projection) are
 *   accepted. When neither figure is present, no sample applies and the result is `''`, so the
 *   caller omits the line.
 *
 * @param sample - The sample.
 * @returns The sample line, or `''` when no sample applies.
 */
export function formatSample(sample: SampleInput): string {
  const { observed, required } = sample;
  const noObserved = observed === null || observed === undefined;
  const noRequired = required === null || required === undefined;
  if (noObserved && noRequired) {
    return '';
  }
  let line = `${formatSampleValue(observed)} / ${formatSampleValue(required)}`;
  const unit = textOrNull(sample.unit);
  if (unit !== null) {
    line += ` ${unit}`;
  }
  const projection = formatProjection(sample.projected);
  return projection === '' ? line : `${line}${SEPARATOR}${projection}`;
}

/**
 * Formats a p-value (AAP 0.5.4.6 CR1, 0.5.4.5 B4): `"< 0.001"` below 0.001, otherwise up to
 * three fraction digits without padding (`"0.0321"` → `"0.032"`, `"0.05"` → `"0.05"`). An
 * absent or non-numeric value renders {@link UNAVAILABLE}.
 *
 * @param p - The p-value.
 * @returns The formatted p-value.
 */
export function formatPValue(p: NumericInput): string {
  const value = toFormattable(p);
  if (value === null) {
    return UNAVAILABLE;
  }
  if (Number(value) < P_VALUE_FLOOR) {
    return `< ${formatDecimal(P_VALUE_FLOOR, P_VALUE_DIGITS)}`;
  }
  return normalizeSpaces(numberFormat('decimal', 0, P_VALUE_DIGITS).format(value));
}

/**
 * Display labels of the test names the backend records in `evidence.test.name`
 * (`analysis/stats.py`, `analysis/budget.py`). A name not listed is shown unchanged.
 */
export const TEST_LABELS: ReadonlyMap<string, string> = new Map([
  ['fisher_exact', "Fisher's exact test"],
  ['two_proportion_z', 'Two-proportion z-test'],
  ['poisson_rate', 'Poisson rate test'],
  ['poisson_rate_test', 'Poisson rate test'],
  ['two_sample_poisson_score', 'Two-sample Poisson rate test'],
  ['two-sample Poisson rate test', 'Two-sample Poisson rate test'],
  ['holm', 'Holm-adjusted'],
]);

/** `"= 0.012"`, or the bound itself for `"< 0.001"`, so the line never reads `"p = < 0.001"`. */
function pRelation(text: string): string {
  return text.startsWith('<') ? text : `= ${text}`;
}

/**
 * Formats the test line of a card (AAP 0.5.5: "the test line where a test was used"), for
 * example `"Fisher's exact test · p = 0.012 · adjusted p = 0.036 · α 0.05"`. Known backend
 * method names map through {@link TEST_LABELS}; the adjusted segment appears only when an
 * adjusted p-value exists (Holm, AAP 0.5.4.6), and the α segment only when α is present.
 *
 * @param test - The test from the item's evidence.
 * @returns The test line.
 */
export function formatTestLine(test: StatTestInput): string {
  const name = test.name.trim();
  const segments = [
    TEST_LABELS.get(name) ?? name,
    `p ${pRelation(formatPValue(asNumeric(test.p_value)))}`,
  ];
  const adjusted = asNumeric(test.p_adjusted);
  if (adjusted !== null && adjusted !== undefined) {
    segments.push(`adjusted p ${pRelation(formatPValue(adjusted))}`);
  }
  const alpha = asNumeric(test.alpha);
  if (alpha !== null && alpha !== undefined) {
    segments.push(`α ${formatDecimal(alpha, ALPHA_DIGITS)}`);
  }
  return segments.join(SEPARATOR);
}


// ---------------------------------------------------------------------------------------------
// Dates, windows and instants. Daily dates are calendar strings in each source's native day
// (Google in the account's time zone, LinkedIn in UTC) and are printed exactly as stored: they
// are never passed to `new Date()`, which reads `"2026-09-04"` as UTC midnight and shifts the
// day elsewhere, and never re-bucketed. The display time zone formats instants only
// (AAP 0.5.4.1).
// ---------------------------------------------------------------------------------------------

/** The stored date, or {@link UNAVAILABLE} when it is missing. */
function dateText(date: IsoDate | null | undefined): string {
  return textOrNull(date) ?? UNAVAILABLE;
}

/**
 * Labels a daily date with its source's day (AAP 0.5.5 Behaviour: "Daily dates are shown as
 * stored, labelled with their source's day boundary"): `"2026-09-04 (Google day)"`, or
 * `"2026-09-04 (Google day, America/New_York)"` with the boundary. The date string is never
 * parsed or shifted. A missing date renders {@link UNAVAILABLE}.
 *
 * @param date - The source-native calendar date.
 * @param source - The source whose day it is.
 * @param boundary - The IANA zone whose midnight ends that source's days.
 * @returns The labelled date.
 */
export function formatSourceDate(
  date: IsoDate | null | undefined,
  source: SourceName,
  boundary?: string | null,
): string {
  const day = textOrNull(date);
  if (day === null) {
    return UNAVAILABLE;
  }
  const zone = textOrNull(boundary);
  const label = `${sourceLabel(source)} day`;
  return zone === null ? `${day} (${label})` : `${day} (${label}, ${zone})`;
}

/**
 * Formats an inclusive range of calendar dates as stored, joined by a spaced en dash:
 * `"2026-09-04 – 2026-10-01"`. The custom-run banner ("Custom window {start} – {end}") and the
 * window chip use it (AAP 0.5.5). A missing end renders {@link UNAVAILABLE}.
 *
 * @param start - The first date, inclusive.
 * @param end - The last date, inclusive.
 * @returns The date range.
 */
export function formatDateRange(
  start: IsoDate | null | undefined,
  end: IsoDate | null | undefined,
): string {
  return `${dateText(start)}${RANGE_SEPARATOR}${dateText(end)}`;
}

/**
 * Renders each source's boundary once, in input order: `"{Label} days: {boundary}"`, with the
 * source's own range appended when it differs from `range`.
 */
function boundaryEntries(
  sourceDays: readonly SourceDayInput[],
  range?: Pick<EvidenceWindow, 'start' | 'end'>,
): string {
  const seen = new Set<string>();
  const entries: string[] = [];
  for (const day of sourceDays) {
    if (seen.has(day.source)) {
      continue;
    }
    seen.add(day.source);
    let entry = `${sourceLabel(day.source)} days: ${textOrNull(day.boundary) ?? UNAVAILABLE}`;
    if (range !== undefined) {
      const start = day.start ?? range.start;
      const end = day.end ?? range.end;
      if (start !== range.start || end !== range.end) {
        entry += ` (${formatDateRange(start, end)})`;
      }
    }
    entries.push(entry);
  }
  return entries.join(SEPARATOR);
}

/**
 * Formats each source's native day boundary for the status bar (AAP 0.5.5: "Google days:
 * America/New_York · LinkedIn days: UTC"). Entries keep their input order, a repeated source
 * keeps its first entry, and an empty list renders `''`. Use {@link boundariesDiffer} to add a
 * "day boundaries differ" note, so misaligned buckets are never presented as one common day
 * (AAP 0.5.4.1).
 *
 * @param sourceDays - The run's or item's `source_days`.
 * @returns The boundary line, or `''`.
 */
export function formatSourceBoundaries(sourceDays: readonly SourceDayInput[]): string {
  return boundaryEntries(sourceDays);
}

/**
 * Whether the sources of a window end their days at different boundaries, for example Google
 * in `America/New_York` and LinkedIn in `UTC`. Evidence must then say so, because the same
 * calendar date covers different instants in each source (AAP 0.5.4.1).
 *
 * @param sourceDays - The run's or item's `source_days`.
 * @returns `true` when more than one distinct boundary appears.
 */
export function boundariesDiffer(sourceDays: readonly Pick<SourceDay, 'boundary'>[]): boolean {
  return new Set(sourceDays.map((day) => textOrNull(day.boundary) ?? UNAVAILABLE)).size > 1;
}

/**
 * Formats a card's window chip (AAP 0.5.5: "{start} – {end} · {N} days · {source
 * boundaries}"), for example `"2026-09-04 – 2026-10-01 · 28 days · Google days:
 * America/New_York · LinkedIn days: UTC"`. A single-day window reads `"1 day"`, the boundary
 * segment is omitted when the window lists no source days, and a source whose own range differs
 * from the window's shows it: `"LinkedIn days: UTC (2026-09-05 – 2026-10-02)"`.
 *
 * @param window - The item's `evidence.window`.
 * @returns The chip text.
 */
export function formatWindowChip(
  window: Pick<EvidenceWindow, 'start' | 'end' | 'days'> & {
    readonly source_days: readonly SourceDayInput[];
  },
): string {
  const segments = [formatDateRange(window.start, window.end), formatDays(window.days)];
  const boundaries = boundaryEntries(window.source_days, window);
  if (boundaries !== '') {
    segments.push(boundaries);
  }
  return segments.join(SEPARATOR);
}

/**
 * Formats an instant in the display time zone, followed by that zone's IANA id (AAP 0.5.5
 * Behaviour: "Instants are formatted in `DISPLAY_TIMEZONE`"):
 * `formatInstant('2026-10-06T15:22:00Z', 'America/New_York')` →
 * `"Oct 6, 2026, 11:22 AM America/New_York"`.
 *
 * - The zone always comes from the caller, which reads it from `Run.display_timezone`; this
 *   module holds no display zone of its own. An invalid zone falls back to UTC and is labelled
 *   `"UTC"`.
 * - The zone id is appended as text rather than through `timeZoneName`, which cannot be
 *   combined with `dateStyle`.
 * - A missing instant renders {@link EM_DASH}; an unparsable one is returned unchanged.
 *
 * @param iso - The instant, ISO 8601 with an offset.
 * @param displayTimezone - The IANA zone to display it in.
 * @returns The formatted instant.
 */
export function formatInstant(
  iso: IsoDateTime | null | undefined,
  displayTimezone: string,
): string {
  if (typeof iso !== 'string' || iso.trim() === '') {
    return EM_DASH;
  }
  const instant = new Date(iso);
  if (Number.isNaN(instant.getTime())) {
    return iso;
  }
  const { formatter, label } = instantFormat(textOrNull(displayTimezone) ?? '');
  return normalizeSpaces(`${formatter.format(instant)} ${label}`);
}

