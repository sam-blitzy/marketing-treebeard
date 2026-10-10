/**
 * `FlagList`: the flags row of a recommendation card, one badge per flag code (AAP 0.5.5).
 *
 * An item carries two sets of flags (AAP 0.5.3 `Item`):
 * - `flags`, recorded when the run computed the item and frozen with its evidence;
 * - `current.flags_now`, added by the read-time overlay (AAP 0.3.2), such as a live
 *   `unverified_discrepancies` count or a dataset that turned `stale` after the run.
 *
 * The row shows their union, one chip per code: the overlay adds current state beside the
 * frozen record and never rewrites it, so the recorded flags keep their order and the current
 * set only adds codes or refreshes a message. The one exception is a live-only code
 * ({@link LIVE_ONLY_FLAG_CODES}), whose recorded copy states a present-tense value frozen with
 * the run: the row shows it only while the current set carries it, in the current words, so a
 * card stops saying "Unverified while 1 discrepancies are open" once every discrepancy is
 * resolved (AAP 0.5.4.2 P1: the count is read from the overlay).
 *
 * Every label is the flag's own `message`, written by the backend (`analysis/context.py`,
 * `analysis/overlay.py`), for example "Simulated data (LinkedIn)" or "Unverified while 2
 * discrepancies are open". The client never re-derives a label from the code. Chips are text
 * labels only, with no icons (AAP 0.4.5), and the tone colour only repeats what the label
 * says (WCAG 2.2 SC 1.4.1).
 *
 * Built only on the design-system index (AAP 0.4.4): `Inline` for layout and `Badge` for the
 * chips, with no styles of its own.
 *
 * @packageDocumentation
 */
import type { ReactElement } from 'react';

import type { Flag, FlagCode } from '../../api/types';
import { Badge, Inline, type Tone } from '../../design-system';

/**
 * The flag codes rendered with the `warning` tone: each one says the item's numbers or its
 * conclusion cannot be fully trusted yet.
 *
 * - Data state: `stale`, `incomplete`, `short_window`.
 * - Discrepancies: `unverified_discrepancies`.
 * - Attribution: `attribution_floor`, `creative_floor`, `deal_floor`, `unknown_crm_evidence`.
 * - Pipeline: `pipeline_unavailable`.
 *
 * Every other code (`simulated`, `meeting_time_proxy`, `no_crm_link`, `not_distinguishable`,
 * `search_term_coverage`, `shared_budget`) is context for reading the item and takes the `info`
 * tone; see {@link flagTone}.
 */
export const WARNING_FLAG_CODES: ReadonlySet<FlagCode> = new Set<FlagCode>([
  'stale',
  'incomplete',
  'short_window',
  'unverified_discrepancies',
  'attribution_floor',
  'creative_floor',
  'deal_floor',
  'unknown_crm_evidence',
  'pipeline_unavailable',
]);

/**
 * The badge tone for a flag code.
 *
 * @param code - the flag's code
 * @returns `'warning'` for a code in {@link WARNING_FLAG_CODES}, otherwise `'info'`, which
 *   includes `simulated` and any code a later API version adds
 *
 * @example
 * ```ts
 * flagTone('stale'); // 'warning'
 * flagTone('simulated'); // 'info'
 * ```
 */
export function flagTone(code: FlagCode): Tone {
  return WARNING_FLAG_CODES.has(code) ? 'warning' : 'info';
}

/**
 * The flag codes read from the current set alone: each one's recorded copy states a
 * present-tense live value, so the copy frozen with the run would go on asserting it after it
 * stopped being true.
 *
 * - `unverified_discrepancies` ("Unverified while N discrepancies are open"): the count of
 *   unresolved discrepancies is read from the overlay (AAP 0.5.4.2 P1), and the overlay adds the
 *   flag to `current.flags_now` only while at least one is open.
 *
 * Every other code is shown as recorded plus current (AAP 0.5.5), `stale`, `incomplete` and
 * `simulated` included: a card computed from simulated data still says so after the source goes
 * live. See {@link mergeFlags}.
 */
export const LIVE_ONLY_FLAG_CODES: ReadonlySet<FlagCode> = new Set<FlagCode>([
  'unverified_discrepancies',
]);

/** Whether `value` is a string with at least one non-whitespace character. */
function hasText(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== '';
}

/**
 * Records the message of each code's first occurrence in `flags` into `messages`, and appends
 * each code not yet in `order`. A later repeat of a code only fills in a message that is still
 * blank, so a repeat never moves a code or replaces a label already chosen.
 */
function collect(
  flags: readonly Flag[],
  order: Set<FlagCode>,
  messages: Map<FlagCode, string>,
): void {
  for (const flag of flags) {
    order.add(flag.code);
    const kept = messages.get(flag.code);
    if (kept === undefined || (!hasText(kept) && hasText(flag.message))) {
      messages.set(flag.code, hasText(flag.message) ? flag.message : '');
    }
  }
}

/**
 * Merges an item's recorded flags with the flags the overlay applies now, one flag per code.
 *
 * Order: the recorded flags first, in their recorded order, then each code that only the
 * current set carries, in its current order. A code present in both keeps its recorded
 * position and takes the **current** message, so a live value such as the open-discrepancy
 * count replaces the one frozen with the run. A repeated code within one list counts once, at
 * its first position.
 *
 * Live-only codes ({@link LIVE_ONLY_FLAG_CODES}) are read from the current set alone. A
 * recorded one is kept, at its recorded position, only when the current set carries the same
 * code, and is dropped otherwise, so a count frozen with the run never outlives the overlay's.
 * Its label always comes from the current set, never from the recorded copy.
 *
 * Labels are never derived from codes, with one defensive exception: a current message that
 * is blank never replaces a recorded label, and a code whose message is blank in both sets is
 * labelled with the code itself, so a chip is never empty (the `Badge` contract). A live-only
 * code whose current message is blank is labelled with the code itself too, never with its
 * frozen recorded text.
 *
 * The inputs are not mutated; the result holds new `Flag` objects.
 *
 * @param recorded - `Item.flags`, recorded when the run computed the item
 * @param current - `Item.current.flags_now`, from the read-time overlay; defaults to none
 * @returns the merged flags, each code exactly once
 *
 * @example
 * ```ts
 * mergeFlags(
 *   [
 *     { code: 'simulated', message: 'Simulated data' },
 *     { code: 'unverified_discrepancies', message: 'Unverified while 1 discrepancies are open' },
 *   ],
 *   [
 *     { code: 'unverified_discrepancies', message: 'Unverified while 3 discrepancies are open' },
 *     { code: 'stale', message: 'Data as of 2026-10-01 03:10' },
 *   ],
 * );
 * // [
 * //   { code: 'simulated', message: 'Simulated data' },
 * //   { code: 'unverified_discrepancies', message: 'Unverified while 3 discrepancies are open' },
 * //   { code: 'stale', message: 'Data as of 2026-10-01 03:10' },
 * // ]
 *
 * // Recorded with 1 discrepancy open; 0 are open now, so the overlay no longer carries the flag.
 * mergeFlags(
 *   [
 *     { code: 'simulated', message: 'Simulated data' },
 *     { code: 'unverified_discrepancies', message: 'Unverified while 1 discrepancies are open' },
 *   ],
 *   [{ code: 'simulated', message: 'Simulated data (LinkedIn)' }],
 * );
 * // [{ code: 'simulated', message: 'Simulated data (LinkedIn)' }]
 * ```
 */
export function mergeFlags(recorded: readonly Flag[], current: readonly Flag[] = []): Flag[] {
  const order = new Set<FlagCode>();
  const recordedMessages = new Map<FlagCode, string>();
  const currentMessages = new Map<FlagCode, string>();

  collect(recorded, order, recordedMessages);
  collect(current, order, currentMessages);

  return Array.from(order).flatMap((code): Flag[] => {
    const now = currentMessages.get(code);
    if (LIVE_ONLY_FLAG_CODES.has(code)) {
      // Only the current set says whether the flag applies now, and in which words.
      return now === undefined ? [] : [{ code, message: hasText(now) ? now : code }];
    }
    const then = recordedMessages.get(code);
    const message = hasText(now) ? now : hasText(then) ? then : code;
    return [{ code, message }];
  });
}

/** Props of {@link FlagList}. */
export interface FlagListProps {
  /** The flags recorded with the item when its run computed it (`Item.flags`). */
  flags: readonly Flag[];
  /**
   * The flags the read-time overlay applies now (`Item.current.flags_now`). A code also in
   * {@link FlagListProps.flags} keeps its place and shows this message instead. A live-only
   * code ({@link LIVE_ONLY_FLAG_CODES}) is shown only when this set carries it, so omitting it
   * means no current flag applies and a recorded live-only code is not shown.
   */
  current?: readonly Flag[];
}

/**
 * Renders a recommendation card's flags row: the merge of the recorded and current flags
 * ({@link mergeFlags}), one `Badge` per code in {@link flagTone}'s tone.
 *
 * The row is a list named "Flags" (`Inline as="ul"`, which carries `role="list"`), with one
 * `<li>` per flag, keyed by its code, so assistive technology announces how many flags apply.
 * When the merge holds no flag (neither set carries one, or the only recorded ones are
 * live-only codes the current set no longer carries), nothing is rendered, so a card without
 * flags shows no empty row and no empty list.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * @param props - see {@link FlagListProps}
 * @returns the flags list, or `null` when there are no flags
 *
 * @example
 * ```tsx
 * <FlagList flags={item.flags} current={item.current.flags_now} />
 * ```
 */
export function FlagList({ flags, current = [] }: FlagListProps): ReactElement | null {
  const merged = mergeFlags(flags, current);
  if (merged.length === 0) {
    return null;
  }

  return (
    <Inline as="ul" gap={2} aria-label="Flags">
      {merged.map(({ code, message }) => (
        <li key={code}>
          <Badge tone={flagTone(code)}>{message}</Badge>
        </li>
      ))}
    </Inline>
  );
}
