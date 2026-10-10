/**
 * Bounded batches: how the `/actions` sections keep a long list from mounting all at once
 * (AAP 0.1.1 Performance: "the five sections render from precomputed runs in under 2 s").
 *
 * A run can hold hundreds of cards in one group, dozens of campaign blocks in Copy/creative,
 * pair cards that grow as V(V−1)/2 with a campaign's variants, and white papers whose tables
 * grow with assets × creatives. Each of these lists renders its first batch in the order the API
 * ranked it, followed by a control:
 *
 * - a polite status, "Showing 20 of 340 actions", which becomes "Showing all 340 actions" once
 *   everything is shown. It is a `role="status"` region, never an `alert`: the alert role is
 *   reserved for the open-discrepancies banner (AAP 0.5.5 Behaviour);
 * - a design-system `Button`, "Show 20 more actions", that reveals the next batch. It is a real
 *   button, so it is reached with Tab and operated with Enter or Space (WCAG 2.2 SC 2.1.1). It
 *   disappears once every entry is shown.
 *
 * After a reveal, focus moves to the first newly revealed entry (its card, group region or table
 * row header), so a keyboard user continues reading from there instead of from the top of the
 * list (WCAG 2.2 SC 2.4.3). An entry that is not focusable by itself becomes programmatically
 * focusable (`tabindex="-1"`, never a positive value) for as long as it holds that focus.
 *
 * A list whose `location.hash` names one of its entries beyond the shown batches, such as a held
 * card's link to its blocking C0 card (`#item-{id}`) or a group anchor (`#group-…`), reveals the
 * batches through that entry and scrolls it into view: when the list mounts, and on every
 * `hashchange`. In-page links therefore never point at an entry that is not rendered. A
 * `hashchange`, which follows such a link, also moves focus to the revealed element (or to the
 * entry's own focus target when the entry does not render the element the hash names), as the
 * root route does for a link target already rendered (`src/router.tsx`), so the next Tab
 * continues from there; a hash present when the list mounts, as on a page load, moves no focus.
 *
 * The bound is a presentation bound, not an analysis threshold: it changes how many entries are
 * mounted at once, never which entries exist, their order, rank, flags or evidence. Every
 * revealed entry renders exactly as it would in an unbatched list. A list at or under its batch
 * size renders exactly as an unbatched list does, with no control and no extra element.
 *
 * Built only on the design-system index (AAP 0.4.4): `Button` and `Inline`, plus a `div` for the
 * status region, with no inline styles and no stylesheet.
 *
 * @packageDocumentation
 */
import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { Button, Inline } from '../../design-system';
import { focusElement, idFromHash } from '../../lib/focus';
import { formatCount } from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Batch sizes
// ---------------------------------------------------------------------------------------------

/**
 * Cards per batch in a list of recommendation cards. A card carries a full evidence table, a
 * flags row and footer actions, so it is the heaviest entry on the page: twenty fill several
 * screens on a desktop, more than a user reads before deciding to scroll on, while keeping the
 * first render of a section of hundreds of cards to a small, fixed number of cards.
 */
export const CARD_BATCH_SIZE = 20;

/**
 * Group regions per batch in a section's groups area, such as the campaign blocks of
 * Copy/creative changes. Each group holds a table and up to {@link CARD_BATCH_SIZE} cards of
 * its own, so ten groups already bound the first render to a few hundred cards at most.
 */
export const GROUP_BATCH_SIZE = 10;

/**
 * Table rows per batch, for the white-paper asset tables, their per-creative tables and the
 * variants tables. A row is a line of formatted cells, far lighter than a card, so fifty rows
 * keep a long table readable and its first render small.
 */
export const ROW_BATCH_SIZE = 50;

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/** What a batched list counts, in the singular and the plural, for its control's text. */
export interface BatchNoun {
  /** The noun for one entry, for example "action". */
  readonly singular: string;
  /** The noun for any other count, for example "actions". */
  readonly plural: string;
}

/** The noun of a list of recommendation cards: "Showing 20 of 340 actions". */
export const ACTION_NOUN: BatchNoun = Object.freeze({ singular: 'action', plural: 'actions' });

/** The noun of a section's list of group regions: "Showing 10 of 24 groups". */
export const GROUP_NOUN: BatchNoun = Object.freeze({ singular: 'group', plural: 'groups' });

/** The noun for a count: the singular for exactly 1, the plural otherwise. */
function nounFor(count: number, noun: BatchNoun): string {
  return count === 1 ? noun.singular : noun.plural;
}

/**
 * The text of the button that reveals the next batch: "Show {next} more {noun}".
 *
 * @param next - how many entries the next batch reveals
 * @param noun - what the list counts
 * @returns the button text, for example "Show 20 more actions" or "Show 1 more action"
 *
 * @example
 * ```ts
 * showMoreLabel(20, ACTION_NOUN); // "Show 20 more actions"
 * ```
 */
export function showMoreLabel(next: number, noun: BatchNoun): string {
  return `Show ${formatCount(next)} more ${nounFor(next, noun)}`;
}

/**
 * The text of a batched list's status: "Showing {shown} of {total} {noun}" while entries remain
 * hidden, "Showing all {total} {noun}" once every entry is shown.
 *
 * @param shown - how many entries are shown
 * @param total - how many entries the list holds
 * @param noun - what the list counts
 * @returns the status text
 *
 * @example
 * ```ts
 * batchStatusText(20, 340, ACTION_NOUN); // "Showing 20 of 340 actions"
 * batchStatusText(340, 340, ACTION_NOUN); // "Showing all 340 actions"
 * ```
 */
export function batchStatusText(shown: number, total: number, noun: BatchNoun): string {
  const counted = nounFor(total, noun);
  return shown >= total
    ? `Showing all ${formatCount(total)} ${counted}`
    : `Showing ${formatCount(shown)} of ${formatCount(total)} ${counted}`;
}

// ---------------------------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------------------------

/**
 * A batch size as a positive whole number: a size that is not one (0, a negative or fractional
 * number, `NaN`) is read as 1, so a misconfigured list still reveals every entry, one at a time,
 * instead of never revealing any.
 */
function normalBatchSize(size: number): number {
  return Number.isSafeInteger(size) && size > 0 ? size : 1;
}

/**
 * The element id `location.hash` names, decoded, or `null` when the hash is empty or `#`. A
 * fragment that is not valid percent-encoding is used as written ({@link idFromHash}).
 *
 * Exported so a disclosure that holds in-page link targets (`HeldDisclosure`) reads the hash
 * exactly as a batched list does.
 *
 * @returns the id, or `null` without one
 *
 * @example
 * ```ts
 * // at /actions#item-1201
 * hashTarget(); // "item-1201"
 * ```
 */
export function hashTarget(): string | null {
  return idFromHash(window.location.hash);
}

/** The position of the first entry whose anchors include `target`, or -1 when none does. */
function indexOfAnchor<T>(
  entries: readonly T[],
  anchorsOf: ((entry: T) => readonly string[]) | undefined,
  target: string | null,
): number {
  if (target === null || anchorsOf === undefined) {
    return -1;
  }
  return entries.findIndex((entry) => anchorsOf(entry).includes(target));
}

/** The smallest whole number of batches that shows the entry at `index`, as an entry count. */
function limitThrough(index: number, batchSize: number): number {
  return (Math.floor(index / batchSize) + 1) * batchSize;
}

/**
 * Scrolls the element with this id into view, aligned to the top of the viewport. Nothing
 * happens when no such element exists, or where the environment implements no scrolling. Focus
 * never moves.
 *
 * Exported so a disclosure that reveals the element a hash names (`HeldDisclosure`) scrolls to
 * it exactly as a batched list does.
 *
 * @param id - the element's id, such as `item-1201`
 * @returns whether the element exists
 *
 * @example
 * ```ts
 * scrollToId('item-1201'); // true once the card is rendered
 * ```
 */
export function scrollToId(id: string): boolean {
  const element = document.getElementById(id);
  if (element === null) {
    return false;
  }
  // Environments without layout, such as jsdom, do not implement scrollIntoView.
  if (typeof element.scrollIntoView === 'function') {
    element.scrollIntoView({ block: 'start' });
  }
  return true;
}

/**
 * Moves focus to the element with this id. An element that is not focusable by itself, such as
 * a card's `<article>` or a group's `<section>`, gets `tabindex="-1"` for as long as it holds
 * focus, which makes it focusable by script but never a Tab stop; the attribute is removed again
 * when it loses focus, so the element is left exactly as it was rendered ({@link focusElement}).
 *
 * @param options - passed to `focus()`, such as `{ preventScroll: true }` for an element already
 *   scrolled into view
 * @returns whether the element exists
 */
function focusId(id: string, options?: FocusOptions): boolean {
  const element = document.getElementById(id);
  if (element === null) {
    return false;
  }
  focusElement(element, options);
  return true;
}

// ---------------------------------------------------------------------------------------------
// The hook
// ---------------------------------------------------------------------------------------------

/** Options of {@link useBatches}. */
export interface BatchOptions<T> {
  /**
   * Entries per batch: {@link CARD_BATCH_SIZE}, {@link GROUP_BATCH_SIZE} or
   * {@link ROW_BATCH_SIZE}. A size that is not a positive whole number is read as 1.
   */
  readonly batchSize: number;
  /**
   * The element ids an entry holds, such as a card's `item-{id}` or a group's anchor together
   * with the anchors of its cards. When `location.hash` names one of them, on mount or on a
   * `hashchange`, the list reveals its batches through that entry and scrolls the named element
   * into view (or the entry's own focus target, when the named element is not rendered); after a
   * `hashchange` it also focuses that element. Omitted, the list ignores the hash.
   */
  readonly anchorsOf?: (entry: T) => readonly string[];
  /**
   * The id of the element that takes focus when the entry at `index` is the first one a reveal
   * shows, such as the card's `item-{id}` or the group's anchor id. Omitted, it is
   * {@link Batches.entryId}, which the caller renders on the entry itself (for example on a table
   * row's header cell, through {@link BatchEntryAnchor}). When no element has the id, focus moves
   * to the list's status instead.
   */
  readonly focusIdOf?: (entry: T, index: number) => string;
}

/** What {@link useBatches} returns: the entries to render and the state of the control. */
export interface Batches<T> {
  /** The entries to render now: the first ones of the list, in its order. */
  readonly shown: readonly T[];
  /** How many entries the list holds. */
  readonly total: number;
  /**
   * Whether the list holds more entries than one batch. Only then does it render a
   * {@link BatchControl}; otherwise every entry is shown and the list renders as unbatched.
   */
  readonly batched: boolean;
  /** How many entries the next reveal shows: at most one batch, 0 once every entry is shown. */
  readonly next: number;
  /**
   * Shows the next batch, then moves focus to its first entry. Does nothing once every entry is
   * shown.
   */
  readonly revealNext: () => void;
  /**
   * The id the entry at `index` carries as its default focus target, unique on the page. Render
   * it through {@link BatchEntryAnchor} when no `focusIdOf` is given.
   */
  readonly entryId: (index: number) => string;
  /** The id of the list's status region, which takes focus when the entry's target is missing. */
  readonly statusId: string;
}

/**
 * The state of one bounded list: which entries to render, how to reveal more and where focus and
 * scrolling go afterwards (see the module overview).
 *
 * The hook keeps a reveal limit, a whole number of batches: one batch on mount, or every batch
 * through the entry the hash names. While the list stays mounted the limit never shrinks; it
 * grows by one batch per reveal and through the entry a `hashchange` names. The shown entries are
 * the first `min(limit, entries.length)` of the current list, so the last batch can be short, and
 * a list that shrinks, such as after a poll or once feedback hides a card, shows fewer entries.
 * When the list grows again, up to the same limit is shown.
 *
 * @param entries - the list, in the order it must render (the API's rank order); never re-sorted
 * @param options - see {@link BatchOptions}
 * @returns see {@link Batches}
 *
 * @example
 * ```tsx
 * const batches = useBatches(rows, { batchSize: ROW_BATCH_SIZE });
 * <DataTable caption="Variants" columns={columns} rows={batches.shown} getRowKey={keyOf} />
 * <BatchControl batches={batches} noun={{ singular: 'variant', plural: 'variants' }} />
 * ```
 */
export function useBatches<T>(entries: readonly T[], options: BatchOptions<T>): Batches<T> {
  const batchSize = normalBatchSize(options.batchSize);
  const { anchorsOf, focusIdOf } = options;
  const baseId = useId();
  // The first render shows one batch, or, when the hash names a later entry, every batch
  // through it.
  const [limit, setLimit] = useState(() => {
    const index = indexOfAnchor(entries, anchorsOf, hashTarget());
    return index < batchSize ? batchSize : limitThrough(index, batchSize);
  });
  /** The first entry of the batch a reveal showed, which takes focus once it has rendered. */
  const pendingFocus = useRef<number | null>(null);
  /**
   * The hash target a `hashchange` revealed, scrolled into view and focused once it has
   * rendered.
   */
  const pendingScroll = useRef<{ target: string; index: number } | null>(null);
  /** The latest render's inputs, read by the `hashchange` listener and the mount effect. */
  const latest = useRef({ entries, anchorsOf, focusIdOf, limit, batchSize, baseId });

  useLayoutEffect(() => {
    latest.current = { entries, anchorsOf, focusIdOf, limit, batchSize, baseId };
  });

  const entryId = (index: number): string => `${baseId}-entry-${String(index)}`;
  const statusId = `${baseId}-status`;

  /** The focus target of the entry at `index` of the latest render. */
  const targetOf = (index: number): string | null => {
    const current = latest.current;
    const entry = current.entries[index];
    if (entry === undefined) {
      return null;
    }
    return current.focusIdOf === undefined
      ? `${current.baseId}-entry-${String(index)}`
      : current.focusIdOf(entry, index);
  };

  // On mount: a hash naming an entry beyond the first batch has been revealed by the initial
  // state, so it is scrolled into view now that it is rendered.
  useEffect(() => {
    const target = hashTarget();
    const current = latest.current;
    const index = indexOfAnchor(current.entries, current.anchorsOf, target);
    if (target !== null && index >= current.batchSize && !scrollToId(target)) {
      const own = targetOf(index);
      if (own !== null) {
        scrollToId(own);
      }
    }
    // Runs once, on mount: later hash changes are handled by the listener below.
  }, []);

  const hashAware = anchorsOf !== undefined;
  useEffect(() => {
    if (!hashAware) {
      return undefined;
    }
    const onHashChange = (): void => {
      const target = hashTarget();
      const current = latest.current;
      const index = indexOfAnchor(current.entries, current.anchorsOf, target);
      // An entry already shown is scrolled to by the browser itself.
      if (target === null || index < Math.min(current.limit, current.entries.length)) {
        return;
      }
      pendingScroll.current = { target, index };
      setLimit((previous) => Math.max(previous, limitThrough(index, current.batchSize)));
    };
    window.addEventListener('hashchange', onHashChange);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
    };
  }, [hashAware]);

  // After a reveal has rendered: scroll to and focus what a hash change revealed, focus what a
  // press of the button revealed.
  useEffect(() => {
    const scroll = pendingScroll.current;
    pendingScroll.current = null;
    if (scroll !== null) {
      let revealed: string | null = scroll.target;
      if (!scrollToId(revealed)) {
        // The entry does not render the element the hash names (such as a collapsed held
        // card's anchor): its own focus target stands in for it.
        revealed = targetOf(scroll.index);
        if (revealed !== null && !scrollToId(revealed)) {
          revealed = null;
        }
      }
      if (revealed !== null) {
        // Already scrolled into view, aligned to the top: focusing must not scroll it again.
        focusId(revealed, { preventScroll: true });
      }
    }
    const focusIndex = pendingFocus.current;
    pendingFocus.current = null;
    if (focusIndex !== null) {
      const target = targetOf(focusIndex);
      if (target === null || !focusId(target)) {
        focusId(statusId);
      }
    }
  }, [limit]);

  const total = entries.length;
  const shownCount = Math.min(limit, total);
  return {
    shown: shownCount === total ? entries : entries.slice(0, shownCount),
    total,
    batched: total > batchSize,
    next: Math.min(batchSize, total - shownCount),
    revealNext: () => {
      if (shownCount >= total) {
        return;
      }
      pendingFocus.current = shownCount;
      setLimit(limitThrough(shownCount, batchSize));
    },
    entryId,
    statusId,
  };
}

// ---------------------------------------------------------------------------------------------
// The control
// ---------------------------------------------------------------------------------------------

/** Props of {@link BatchControl}. */
export interface BatchControlProps<T> {
  /** The list's state, from {@link useBatches}. */
  readonly batches: Batches<T>;
  /** What the list counts, for the status and the button text. */
  readonly noun: BatchNoun;
}

/**
 * The control after a batched list: the polite status ({@link batchStatusText}) and, while
 * entries remain hidden, a secondary `Button` ({@link showMoreLabel}) that reveals the next
 * batch. Both sit in one `Inline` row, the status first, so the count is read before the action.
 *
 * The status is a `role="status"` region that stays rendered once every entry is shown, so its
 * last change, "Showing all {total} {noun}", is announced too. It carries `tabindex="-1"`, so it
 * can take focus after a reveal whose first entry has no focus target of its own; it is never a
 * Tab stop.
 *
 * @param props - see {@link BatchControlProps}
 * @returns the control, or `null` for a list that is not batched (at or under one batch)
 *
 * @example
 * ```tsx
 * <BatchControl batches={batches} noun={ACTION_NOUN} />
 * // Showing 20 of 340 actions  [Show 20 more actions]
 * ```
 */
export function BatchControl<T>({ batches, noun }: BatchControlProps<T>): ReactElement | null {
  if (!batches.batched) {
    return null;
  }
  const shownCount = batches.shown.length;
  return (
    <Inline gap={3} align="center">
      <div role="status" id={batches.statusId} tabIndex={-1}>
        {batchStatusText(shownCount, batches.total, noun)}
      </div>
      {batches.next > 0 ? (
        <Button variant="secondary" onClick={batches.revealNext}>
          {showMoreLabel(batches.next, noun)}
        </Button>
      ) : null}
    </Inline>
  );
}

/** Props of {@link BatchEntryAnchor}. */
export interface BatchEntryAnchorProps {
  /**
   * The focus target's id, {@link Batches.entryId} of the entry's position; `null` renders the
   * children alone, as in a list that is not batched.
   */
  readonly id: string | null;
  /** The entry's identifying content, such as a table row's header text. */
  readonly children: ReactNode;
}

/**
 * The focus target of an entry that renders no element of its own to focus, such as a table row,
 * whose cells the design-system `DataTable` renders: a `<span>` with the entry's id around its
 * identifying content (the row header), so focus lands on the first new row after a reveal.
 *
 * @param props - see {@link BatchEntryAnchorProps}
 * @returns the anchored content, or the content alone without an id
 *
 * @example
 * ```tsx
 * render: (row) => <BatchEntryAnchor id={row.focusId}>{row.name}</BatchEntryAnchor>
 * ```
 */
export function BatchEntryAnchor({ id, children }: BatchEntryAnchorProps): ReactElement {
  return id === null ? <>{children}</> : <span id={id}>{children}</span>;
}

// ---------------------------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------------------------

/** Props of {@link BatchedList}. */
export interface BatchedListProps<T> {
  /** The entries, in the order they must render (the API's rank order); never re-sorted. */
  readonly entries: readonly T[];
  /** Entries per batch, see {@link BatchOptions.batchSize}. */
  readonly batchSize: number;
  /** What the list counts, for its control. */
  readonly noun: BatchNoun;
  /** A key unique within the list: the React key of the entry's fragment. */
  readonly getKey: (entry: T) => string | number;
  /** Renders one entry, such as its card or its group region. */
  readonly renderEntry: (entry: T) => ReactNode;
  /** The element ids an entry holds, see {@link BatchOptions.anchorsOf}. */
  readonly anchorsOf?: (entry: T) => readonly string[];
  /** The id of the element that takes focus when the entry leads a reveal. */
  readonly focusIdOf: (entry: T, index: number) => string;
}

/**
 * A list of rendered entries, such as cards or group regions, mounted one batch at a time (see
 * the module overview).
 *
 * Each shown entry renders as `renderEntry` returns it, in a `Fragment` keyed by `getKey`, so it
 * adds no element and stays a direct child of the enclosing `Stack`, exactly as in an unbatched
 * list, and an entry keeps its own state when earlier entries come or go. A list longer than one
 * batch is followed by its {@link BatchControl}, which also becomes a child of that `Stack`.
 *
 * @param props - see {@link BatchedListProps}
 * @returns the shown entries, then the control when the list is batched
 *
 * @example
 * ```tsx
 * <Stack>
 *   <BatchedList
 *     entries={items}
 *     batchSize={CARD_BATCH_SIZE}
 *     noun={ACTION_NOUN}
 *     getKey={(item) => item.id}
 *     renderEntry={(item) => <RecommendationCard item={item} />}
 *     anchorsOf={(item) => [`item-${item.id}`]}
 *     focusIdOf={(item) => `item-${item.id}`}
 *   />
 * </Stack>
 * ```
 */
export function BatchedList<T>({
  entries,
  batchSize,
  noun,
  getKey,
  renderEntry,
  anchorsOf,
  focusIdOf,
}: BatchedListProps<T>): ReactElement {
  const batches = useBatches(
    entries,
    anchorsOf === undefined ? { batchSize, focusIdOf } : { batchSize, anchorsOf, focusIdOf },
  );
  return (
    <>
      {batches.shown.map((entry) => (
        <Fragment key={getKey(entry)}>{renderEntry(entry)}</Fragment>
      ))}
      <BatchControl batches={batches} noun={noun} />
    </>
  );
}
