/**
 * `WithheldList`: the patterns a section withheld for sample size or validity (AAP 0.5.4.2 P6).
 *
 * A pattern below its minimum never becomes an item. The run stores it in the section's
 * `withheld` list (AAP 0.5.3 `Withheld`: `{rule_id, entity, reason, sample: {observed, required,
 * unit}}`), and the section lists it after its items, with the sample it saw against the sample
 * it needed. The Budget section titles the list "Patterns withheld for sample size" (AAP 0.5.5),
 * where a B4 line reads, for example, "B4 · Brand search: 3 weekend days of 4 required — 3 / 4
 * weekend days".
 *
 * Presentation rules this file keeps:
 * - Every number comes from the API. The sample line is rendered by `formatSample`, which
 *   formats the recorded observed and required sizes; no threshold or minimum is written into
 *   the client (AAP 0.5.4.1).
 * - The reason is the backend's own sentence, frozen with the run, and is never re-derived.
 * - Lines keep the order the API returns them in.
 *
 * Built only on the design-system index (AAP 0.4.4): `Stack` for layout and the list, with no
 * styles of its own, no raw headings and no inline styles.
 *
 * @packageDocumentation
 */
import type { ReactElement } from 'react';

import type { EntityView, Withheld } from '../../api/types';
import { Stack } from '../../design-system';
import { formatSample } from '../../lib/format';

/** The list title when the caller passes none, or a blank one. */
const DEFAULT_TITLE = 'Withheld for sample size';

/** Names the entity of an account-wide withholding, which has none (`entity: null`). */
const ACCOUNT_WIDE = 'Account-wide';

/** Separator between the rule id and the entity: a middle dot (U+00B7) with spaces. */
const RULE_SEPARATOR = ' · ';

/** Separator between the statement and its sample line: an em dash (U+2014) with spaces. */
const SAMPLE_SEPARATOR = ' — ';

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

/** Props of {@link WithheldList}. */
export interface WithheldListProps {
  /** The section's withheld patterns (`Section.withheld`), in the order the API returned. */
  withheld: readonly Withheld[];
  /**
   * The list's visible title, which also names the list for assistive technology. Defaults to
   * "Withheld for sample size"; the Budget section passes "Patterns withheld for sample size"
   * (AAP 0.5.5). A blank title falls back to the default, so the list is never unnamed.
   */
  title?: string;
}

/**
 * Lists a section's withheld patterns under a title, one {@link withheldLine} per pattern, in
 * the order the API returned them.
 *
 * Renders a `Stack` (gap step 2, `breakLongWords`) holding the title as bold text and a
 * `Stack as="ul"` (gap step 1, `role="list"`) with one `<li>` per pattern. `breakLongWords` lets
 * a long unbroken title, entity name or search term wrap inside a narrow section instead of
 * overflowing it (WCAG 2.2 SC 1.4.10 Reflow). The list is named by its `aria-label`, equal to
 * the visible title, because `Stack` accepts no `aria-labelledby`. When there is nothing
 * withheld, nothing is rendered: no title and no empty list.
 *
 * The component holds no state and has no effects; its output depends only on its props.
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
  if (withheld.length === 0) {
    return null;
  }
  const label = textOrNull(title) ?? DEFAULT_TITLE;

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
        {withheld.map((w, index) => (
          <li key={`${w.rule_id}-${index}`}>{withheldLine(w)}</li>
        ))}
      </Stack>
    </Stack>
  );
}
