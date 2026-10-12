/**
 * `WithheldList`: the patterns a section withheld for sample size or validity (AAP 0.5.4.2 P6).
 *
 * A pattern below its minimum never becomes an item. The run stores it in the section's
 * `withheld` list (AAP 0.5.3 `Withheld`: `{rule_id, entity, reason, sample: {observed, required,
 * unit}}`), and the section lists it after its items, with the sample it saw against the sample
 * it needed. The Budget section titles the list "Patterns withheld for sample size" (AAP 0.5.5),
 * where a B4 line reads, for example, "B4 · Brand search: 3 weekend days of 4 required — 3 / 4
 * weekend days". Any other list is titled from its lines: "Withheld for sample size" when every
 * line records observed below required, else "Withheld", because a pattern can be withheld for
 * validity ("pipeline unavailable") rather than for sample size.
 *
 * Presentation rules this file keeps:
 * - Every number comes from the API. The sample line is rendered by `formatSample`, which
 *   formats the recorded observed and required sizes; no threshold or minimum is written into
 *   the client (AAP 0.5.4.1).
 * - The reason is the backend's own sentence, frozen with the run, and is never re-derived.
 * - Lines keep the order the API returns them in.
 * - A long list mounts in bounded batches (`./batches`; AAP 0.1.1 "the five sections render from
 *   precomputed runs in under 2 s"): `ROW_BATCH_SIZE` lines at a time, followed by a "Show {n}
 *   more withheld patterns" control, so a run that withholds thousands of search terms never
 *   mounts them all at once. Every line stays reachable, with its observed and required sizes
 *   (AAP 0.5.4.2 P6).
 * - {@link withheldReason} turns the leading line into the one-sentence reason an empty section
 *   or sub-list shows (AAP 0.5.5: "No search term met the K1 minimum of 37 clicks"), with the
 *   minimum taken from the recorded `sample.required`, stated only when every line of the
 *   leading line's rule and entity type records an observed size below it.
 *
 * Built only on the design-system index (AAP 0.4.4): `Stack` for layout and the list, and the
 * shared batch control (`Inline` and `Button`), with no styles of its own, no raw headings and
 * no inline styles.
 *
 * @packageDocumentation
 */
import { useMemo } from 'react';
import type { ReactElement } from 'react';

import type { EntityView, Withheld } from '../../api/types';
import { Stack } from '../../design-system';
import { formatCount, formatSample, isDecimalString } from '../../lib/format';
import { BatchControl, ROW_BATCH_SIZE, useBatches } from './batches';
import type { BatchNoun } from './batches';

/**
 * The list title when the caller passes none, or a blank one, and every line records a sample
 * shortfall: observed below required ({@link defaultTitle}).
 */
const SAMPLE_SIZE_TITLE = 'Withheld for sample size';

/**
 * The list title when the caller passes none, or a blank one, and some line records no sample
 * shortfall, such as C1b's "pipeline unavailable" or a line with no recorded sizes
 * ({@link defaultTitle}).
 */
const NEUTRAL_TITLE = 'Withheld';

/** What the list counts in its batch control: "Showing 50 of 16,943 withheld patterns". */
const WITHHELD_NOUN: BatchNoun = Object.freeze({
  singular: 'withheld pattern',
  plural: 'withheld patterns',
});

/** Names the entity of an account-wide withholding, which has none (`entity: null`). */
const ACCOUNT_WIDE = 'Account-wide';

/** Separator between the rule id and the entity: a middle dot (U+00B7) with spaces. */
const RULE_SEPARATOR = ' · ';

/** Separator between the statement and its sample line: an em dash (U+2014) with spaces. */
const SAMPLE_SEPARATOR = ' — ';

/**
 * The entity type of an account-level withholding. Its minimum applies to the whole account,
 * not to entities of a kind, so it never reads "No account met the … minimum".
 */
const ACCOUNT_TYPE = 'account';

/** Returns `text` trimmed, or `null` when it is not a string or holds only whitespace. */
function textOrNull(text: unknown): string | null {
  if (typeof text !== 'string') {
    return null;
  }
  const trimmed = text.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * The display label of a withheld pattern's entity.
 *
 * The contract types `entity` as an `EntityView` or `null` (AAP 0.5.3), and a plain string is
 * accepted as well, so a label recorded as text still renders. The label is, in order of
 * preference: the string itself; the entity's `name`; its type and id ("campaign 812"), or
 * whichever of the two is present; and "Account-wide" when the entity is `null` or carries none
 * of these, which is how the backend records an account-wide withholding. It is never empty and
 * never the text "null" or "undefined".
 */
function entityLabel(entity: EntityView | string | null | undefined): string {
  if (typeof entity === 'string') {
    return textOrNull(entity) ?? ACCOUNT_WIDE;
  }
  if (entity === null || entity === undefined) {
    return ACCOUNT_WIDE;
  }
  const name = textOrNull(entity.name);
  if (name !== null) {
    return name;
  }
  const type = textOrNull(entity.type);
  const id = textOrNull(entity.id);
  if (type !== null && id !== null) {
    return `${type} ${id}`;
  }
  return id ?? type ?? ACCOUNT_WIDE;
}

/**
 * Renders one withheld pattern as a single line of text:
 * "{rule_id} · {entity}: {reason} — {observed} / {required} {unit}".
 *
 * - The entity is its display name; an account-wide withholding (`entity: null`) reads
 *   "Account-wide".
 * - The sample segment is `formatSample(w.sample)`: the recorded sizes as counts with grouping
 *   (`"1,234"`), a fractional size never shown as an integer but at `formatCount`'s two-place
 *   precision (a tiny nonzero one as a bound such as `"< 0.01"`), a missing size as "unavailable"
 *   and the unit appended when present. A withheld sample has no projection, so this is exactly
 *   the "observed / required unit" form.
 * - When neither size is recorded, no sample applies (for example C3a's
 *   `segment_window_unavailable`), and the line ends at the reason rather than showing
 *   "unavailable / unavailable".
 * - A blank reason drops the ": {reason}" segment, so the line never shows an empty clause.
 *
 * The function is pure: it reads only `w` and adds no number of its own.
 *
 * @param w - the withheld pattern, as the API returned it
 * @returns the line
 *
 * @example
 * ```ts
 * withheldLine({
 *   rule_id: 'B4',
 *   entity: { source: 'google', type: 'campaign', id: '812', name: 'Brand search' },
 *   reason: '3 weekend days of 4 required',
 *   sample: { observed: 3, required: 4, unit: 'weekend days' },
 * });
 * // "B4 · Brand search: 3 weekend days of 4 required — 3 / 4 weekend days"
 * ```
 */
export function withheldLine(w: Withheld): string {
  const head = `${w.rule_id.trim()}${RULE_SEPARATOR}${entityLabel(w.entity)}`;
  const reason = textOrNull(w.reason);
  const statement = reason === null ? head : `${head}: ${reason}`;
  const sample = formatSample(w.sample);
  return sample === '' ? statement : `${statement}${SAMPLE_SEPARATOR}${sample}`;
}

/**
 * The entity type of a withheld pattern, trimmed, or `null` when the pattern is account-wide
 * (`entity: null`), its entity is recorded as plain text, or the entity carries no type.
 */
function entityType(entity: Withheld['entity'] | string | undefined): string | null {
  if (entity === null || entity === undefined || typeof entity === 'string') {
    return null;
  }
  return textOrNull(entity.type);
}

/**
 * A recorded sample size as text two sizes are compared by: a finite number as `String` writes
 * it, or a decimal string trimmed (`"2.900000"`); `null` for a missing or unreadable size. The
 * raw text is compared rather than a rounded rendering, so two minimums that differ only past
 * the displayed precision never pass as one.
 */
function sizeKey(size: unknown): string | null {
  if (typeof size === 'number') {
    return Number.isFinite(size) ? String(size) : null;
  }
  if (typeof size === 'string') {
    const trimmed = size.trim();
    return isDecimalString(trimmed) ? trimmed : null;
  }
  return null;
}

/**
 * A recorded sample size in the form two sizes are ordered by exactly: its sign and, for a
 * non-zero size, the fraction `0.{digits} × 10^scale`, with `digits` free of leading and trailing
 * zeros (`"12.50"` → digits "125", scale 2; `0.0025` → "25", -2). Zero in any form is sign 0.
 */
interface ExactSize {
  readonly sign: -1 | 0 | 1;
  readonly scale: number;
  readonly digits: string;
}

/**
 * Reads a recorded sample size as an {@link ExactSize}, with no binary float and no rounding: a
 * decimal string (`"2.500000"`) digit by digit, and a finite number as `String` writes it, the
 * shortest decimal that round-trips it, so a JSON number compares as the decimal it was written
 * as. Digit text is compared instead of `Number()` values so that two sizes differing only past
 * a double's precision never pass as equal or misordered.
 *
 * @param size - the recorded size
 * @returns the exact size, or `null` for a missing or unreadable size (text such as "n/a", a
 *   non-finite number, `null`) or one whose exponent or scale is not a safe integer
 */
function exactSize(size: unknown): ExactSize | null {
  let text: string;
  if (typeof size === 'number' && Number.isFinite(size)) {
    text = String(size);
  } else if (typeof size === 'string' && isDecimalString(size)) {
    text = size.trim();
  } else {
    return null;
  }
  const unsigned = text.replace(/^[+-]/, '');
  const exponentAt = unsigned.search(/[eE]/);
  const mantissa = exponentAt === -1 ? unsigned : unsigned.slice(0, exponentAt);
  const exponent = exponentAt === -1 ? 0 : Number(unsigned.slice(exponentAt + 1));
  const allDigits = mantissa.replace('.', '');
  const significant = allDigits.replace(/^0+/, '');
  const digits = significant.replace(/0+$/, '');
  if (digits === '') {
    return { sign: 0, scale: 0, digits: '' };
  }
  const pointAt = mantissa.indexOf('.');
  const wholeDigits = pointAt === -1 ? mantissa.length : pointAt;
  const scale = wholeDigits - (allDigits.length - significant.length) + exponent;
  if (!Number.isSafeInteger(exponent) || !Number.isSafeInteger(scale)) {
    return null;
  }
  return { sign: text.startsWith('-') ? -1 : 1, scale, digits };
}

/** Orders two non-zero exact sizes of one sign by magnitude: -1, 0 or 1. */
function compareMagnitude(a: ExactSize, b: ExactSize): -1 | 0 | 1 {
  if (a.scale !== b.scale) {
    return a.scale < b.scale ? -1 : 1;
  }
  // At one scale each size is the fraction 0.{digits} with no trailing zero, so the digit
  // strings order as text: "25" < "251" < "3".
  if (a.digits === b.digits) {
    return 0;
  }
  return a.digits < b.digits ? -1 : 1;
}

/**
 * Whether a withheld line's sample records a shortfall: its `observed` and `required` sizes are
 * both readable ({@link exactSize}) and observed is strictly below required. A line at or above
 * its minimum, such as a B4 pattern withheld because its rate test failed at 264 of 10 outcomes,
 * or one with a missing or unreadable size, records none.
 */
function fallsShort(sample: Withheld['sample']): boolean {
  const observed = exactSize(sample.observed);
  const required = exactSize(sample.required);
  if (observed === null || required === null) {
    return false;
  }
  if (observed.sign !== required.sign) {
    return observed.sign < required.sign;
  }
  if (observed.sign === 0) {
    return false;
  }
  const order = compareMagnitude(observed, required);
  return observed.sign > 0 ? order < 0 : order > 0;
}

/**
 * The title of a withheld list whose caller passes none: "Withheld for sample size" only when
 * every line records a sample shortfall ({@link fallsShort}: readable observed strictly below
 * readable required), so the heading never names sample size for a pattern withheld for another
 * reason; otherwise the neutral "Withheld". A line withheld for validity, such as C1b's
 * "pipeline unavailable: Qualified Lead count unavailable" (no sizes recorded) or a B4 rate test
 * that failed at 264 of 10 outcomes, or one with a missing size, records no shortfall.
 *
 * @param withheld - every line of the list, shown or not yet revealed, as the API returned them,
 *   so revealing more lines never changes the title
 * @returns the title
 */
function defaultTitle(withheld: readonly Withheld[]): string {
  return withheld.every((w) => fallsShort(w.sample)) ? SAMPLE_SIZE_TITLE : NEUTRAL_TITLE;
}

/**
 * The minimum every withheld line of the leading line's rule and entity type falls short of, as
 * the parts of "No {noun} met the {rule} minimum of {size} {unit}". It is `null` when the leading
 * line names no entity type, names the account, or records no readable `required` size or no
 * unit; when another line of the same rule and type records a different size or unit; and when
 * any line of that rule and type, the leading one included, does not fall short of its minimum
 * ({@link fallsShort}): its observed size is missing, unreadable, or at or above the required
 * one. The sentence then would claim a shortfall the lines do not record.
 */
function uniformMinimum(
  lead: Withheld,
  rule: string,
  withheld: readonly Withheld[],
): { noun: string; size: string; unit: string } | null {
  const type = entityType(lead.entity);
  if (type === null || type === ACCOUNT_TYPE) {
    return null;
  }
  const required = sizeKey(lead.sample.required);
  const unit = textOrNull(lead.sample.unit);
  if (required === null || unit === null) {
    return null;
  }
  for (const w of withheld) {
    if (textOrNull(w.rule_id) !== rule || entityType(w.entity) !== type) {
      continue;
    }
    if (
      sizeKey(w.sample.required) !== required ||
      textOrNull(w.sample.unit) !== unit ||
      !fallsShort(w.sample)
    ) {
      return null;
    }
  }
  return { noun: type.replaceAll('_', ' '), size: formatCount(required), unit };
}

/**
 * The one-sentence reason an empty section or sub-list gives (AAP 0.5.5: "Each section shows an
 * `EmptyState` with the reason when it has no items, for example 'No search term met the K1
 * minimum of 37 clicks'"), read from its leading withheld line `withheld[0]`, in API order:
 *
 * 1. "No {entity type} met the {rule_id} minimum of {required} {unit}" when the leading line
 *    names an entity type other than `account` and every line of the same rule and entity type
 *    records the same `sample.required` and the same unit, and falls short of it: its
 *    `sample.observed` and `sample.required` are both readable sizes and observed is strictly
 *    below required, compared exactly (decimal strings digit by digit, never rounded). A line
 *    at or above its minimum, such as a B4 pattern withheld because the weekend and weekday
 *    rates are not distinguishable at 264 of 10 outcomes, or one with no readable observed
 *    size, does not substantiate the sentence, and the reason comes from step 2 instead. The
 *    type is written with spaces for underscores (`search_term` → "search term"), and the size
 *    is counted as the sample line counts it (`formatCount`: `1234` → "1,234", `"2.900000"` →
 *    "2.9");
 * 2. otherwise, when the leading line has a reason, "{rule_id} withheld: {reason}" for an
 *    account-wide line and "{rule_id} withheld for {entity}: {reason}" for any other, the entity
 *    named as in {@link withheldLine};
 * 3. otherwise the leading line itself, {@link withheldLine}.
 *
 * Every number is the API's own recorded size; no threshold or minimum is written into the
 * client (AAP 0.5.4.1). The function is pure: it reads only `withheld`.
 *
 * @param withheld - the withheld patterns to explain, as the API returned them
 * @returns the reason, or `null` when nothing is withheld
 *
 * @example
 * ```ts
 * const term = (name: string, observed: number): Withheld => ({
 *   rule_id: 'K1',
 *   entity: { source: 'google', type: 'search_term', id: name, name },
 *   reason: `clicks ${String(observed)} of 37 required`,
 *   sample: { observed, required: 37, unit: 'clicks' },
 * });
 * withheldReason([term('crm pricing', 30), term('crm demo', 12)]);
 * // "No search term met the K1 minimum of 37 clicks"
 * withheldReason([
 *   {
 *     rule_id: 'K3',
 *     entity: null,
 *     reason: 'window 7 days of 14 required',
 *     sample: { observed: 7, required: 14, unit: 'days' },
 *   },
 * ]);
 * // "K3 withheld: window 7 days of 14 required"
 * withheldReason([
 *   {
 *     rule_id: 'B4',
 *     entity: { source: 'linkedin', type: 'campaign', id: '1', name: 'Capped - Lead gen' },
 *     reason: 'weekend and weekday rates not distinguishable',
 *     sample: { observed: 264, required: 10, unit: 'outcomes' },
 *   },
 * ]);
 * // "B4 withheld for Capped - Lead gen: weekend and weekday rates not distinguishable"
 * withheldReason([]); // null
 * ```
 */
export function withheldReason(withheld: readonly Withheld[]): string | null {
  const lead = withheld[0];
  if (lead === undefined) {
    return null;
  }
  const rule = textOrNull(lead.rule_id);
  const reason = textOrNull(lead.reason);
  if (rule === null) {
    return withheldLine(lead);
  }
  const minimum = uniformMinimum(lead, rule, withheld);
  if (minimum !== null) {
    return `No ${minimum.noun} met the ${rule} minimum of ${minimum.size} ${minimum.unit}`;
  }
  if (reason === null) {
    return withheldLine(lead);
  }
  const entity = entityLabel(lead.entity);
  return entity === ACCOUNT_WIDE
    ? `${rule} withheld: ${reason}`
    : `${rule} withheld for ${entity}: ${reason}`;
}

/** Props of {@link WithheldList}. */
export interface WithheldListProps {
  /** The section's withheld patterns (`Section.withheld`), in the order the API returned. */
  withheld: readonly Withheld[];
  /**
   * The list's visible title, which also names the list for assistive technology; the Budget
   * section passes "Patterns withheld for sample size" (AAP 0.5.5). Omitted or blank, the title
   * is chosen from the lines ({@link defaultTitle}): "Withheld for sample size" when every line
   * records observed below required, else "Withheld", so the list is never unnamed.
   */
  title?: string;
}

/**
 * Lists a section's withheld patterns under a title, one {@link withheldLine} per pattern, in
 * the order the API returned them. The title is the caller's, or, without one, chosen from the
 * lines ({@link defaultTitle}).
 *
 * Renders a `Stack` (gap step 2, `breakLongWords`) holding the title as bold text and a
 * `Stack as="ul"` (gap step 1, `role="list"`) with one `<li>` per shown pattern. `breakLongWords`
 * lets a long unbroken title, entity name or search term wrap inside a narrow section instead of
 * overflowing it (WCAG 2.2 SC 1.4.10 Reflow). The list is named by its `aria-label`, equal to
 * the visible title, because `Stack` accepts no `aria-labelledby`. When there is nothing
 * withheld, nothing is rendered: no title and no empty list.
 *
 * The lines mount `ROW_BATCH_SIZE` at a time, in API order (`./batches`). A list longer than one
 * batch is followed, after the `<ul>` and inside the outer `Stack`, by the `BatchControl`: the
 * polite status "Showing 50 of 16,943 withheld patterns" and a "Show 50 more withheld patterns"
 * button. Each shown `<li>` of such a list carries its batch entry id, so after a reveal focus
 * moves to the first newly revealed line. A list at or under one batch renders every line with
 * no id and no control, exactly as an unbatched list.
 *
 * The component keeps only how many batches it shows; it fetches nothing.
 *
 * @param props - see {@link WithheldListProps}
 * @returns the titled list, or `null` when nothing is withheld
 *
 * @example
 * ```tsx
 * <WithheldList withheld={section.withheld} title="Patterns withheld for sample size" />
 * ```
 */
export function WithheldList({ withheld, title }: WithheldListProps): ReactElement | null {
  // Called before the early return, so every render calls the same hooks in the same order.
  const batches = useBatches(withheld, { batchSize: ROW_BATCH_SIZE });
  // Choosing the title reads every line, so it is not repeated when a reveal renders again.
  const label = useMemo(() => textOrNull(title) ?? defaultTitle(withheld), [title, withheld]);
  if (withheld.length === 0) {
    return null;
  }

  return (
    <Stack gap={2} breakLongWords>
      {/*
        BLITZY [DESIGN_SYSTEM_GAP]: the title should be semibold body text, but no design-system
        wrapper renders text at --font-weight-semibold and feature code carries no CSS of its own
        (AAP 0.4.4). It is a <strong> placed directly in the Stack, so it is a block flex item
        whose spacing is the gap token alone; a raw <p> would add its user-agent block margins,
        which are not spacing tokens. The design-system base layer sets <strong> to
        --font-weight-semibold until Blitzy OS supplies a text component. It is not a heading
        element, because headings come only from the PageShell heading slots.
      */}
      <strong>{label}</strong>
      <Stack as="ul" gap={1} aria-label={label}>
        {batches.shown.map((w, index) => (
          <li
            key={`${w.rule_id}-${index}`}
            // The focus target of the line when a reveal shows it first; none when unbatched.
            id={batches.batched ? batches.entryId(index) : undefined}
          >
            {withheldLine(w)}
          </li>
        ))}
      </Stack>
      <BatchControl batches={batches} noun={WITHHELD_NOUN} />
    </Stack>
  );
}
