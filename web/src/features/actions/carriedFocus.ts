/**
 * Keyboard focus carried to a card or table row that React mounts anew in another place on the
 * page (WCAG 2.2 SC 2.4.3 Focus Order).
 *
 * A refetch that finds an item computed from replaced data (`current.historical`, AAP 0.3.2)
 * moves its card out of its group's visible cards into the group's "Computed from replaced data"
 * block (`SectionFrame`'s `HistoricalItems`), and its white-paper row out of its cohort's table
 * into the cohort's replaced-data table. React cannot move an element to another parent, so it
 * removes the card or row, with a decision dialog it holds open, and mounts a new one in the new
 * place. Focus inside the removed one would fall to the page body. The hooks here hand it to the
 * new one in the commit that moves it:
 *
 * 1. a card or row that leaves the page while it holds focus notes its recommendation identity
 *    (`itemIdentity`, {@link useFocusLeaving}). It holds focus when focus is inside it, or when
 *    a decision dialog of its own is open, since that modal dialog then owns focus. The note is
 *    taken in a layout-effect cleanup, which React runs while the removed elements, the dialog's
 *    included, are still in the document;
 * 2. a card or row of the same identity that mounts in that commit claims the note in a layout
 *    effect and takes focus in a passive effect ({@link useCarriedFocus}). React runs the passive
 *    cleanup of the removed dialog, which frees the page behind it from `inert` and finds no
 *    opener to return focus to, before any passive effect of a mounting component, so the new
 *    card or row can take focus;
 * 3. a list that mounts its entries in batches shows the batches through an entry whose identity
 *    was noted ({@link focusLeftWith}), so that entry mounts, in the synchronous re-render React
 *    runs for a layout-effect update, before its note is dropped.
 *
 * Notes are dropped in a microtask, which runs once the commit, and every re-render it started
 * synchronously, has finished. A note nothing claimed, such as that of a card a feedback has
 * hidden or of a page left behind, therefore never moves focus later.
 *
 * Each caller decides what holds focus and where it goes, with its own elements; the module
 * imports React alone, so the card, the section frame and the white-paper section share it
 * without an import cycle.
 *
 * @packageDocumentation
 */
import { useEffect, useLayoutEffect, useRef } from 'react';

/**
 * The recommendation identities of the cards and rows that left the page holding keyboard focus
 * in the commit React is running now.
 */
const leftHoldingFocus = new Set<string>();

/**
 * Notes that the card or row of `identity` is leaving the page while it holds keyboard focus.
 * The first note of a commit schedules the microtask that drops every note left unclaimed.
 *
 * @param identity - the recommendation identity (`itemIdentity`) of the leaving card or row
 */
function noteFocusLeaving(identity: string): void {
  if (leftHoldingFocus.size === 0) {
    queueMicrotask(() => {
      leftHoldingFocus.clear();
    });
  }
  leftHoldingFocus.add(identity);
}

/**
 * Whether the card or row of `identity` left the page holding keyboard focus in the commit
 * React is running now, so the card or row of that identity mounting elsewhere takes it over.
 * A list that mounts its entries in batches reads it to show such an entry at once.
 *
 * @param identity - a recommendation identity (`itemIdentity`)
 * @returns `true` while the note is neither claimed nor dropped
 *
 * @example
 * ```ts
 * useBatches(items, { batchSize, mustShow: (item) => focusLeftWith(itemIdentity(item)) });
 * ```
 */
export function focusLeftWith(identity: string): boolean {
  return leftHoldingFocus.has(identity);
}

/**
 * Notes the component's recommendation identity when it unmounts, or renders another identity,
 * while `holdsFocus()` reports that it holds keyboard focus, so a card or row of the same
 * identity mounting in the same commit takes focus over ({@link useCarriedFocus}).
 *
 * `holdsFocus` is the latest render's and is called in a layout-effect cleanup, while the
 * component's elements are still in the document. A component whose open decision dialog holds
 * focus uses this hook alone: the card or row it belongs to claims the note and takes focus.
 *
 * @param identity - the recommendation identity (`itemIdentity`) of the item rendered
 * @param holdsFocus - whether keyboard focus belongs to the component now
 *
 * @example
 * ```ts
 * useFocusLeaving(itemIdentity(item), () => dialog.open);
 * ```
 */
export function useFocusLeaving(identity: string, holdsFocus: () => boolean): void {
  const holdsFocusRef = useRef(holdsFocus);
  useLayoutEffect(() => {
    holdsFocusRef.current = holdsFocus;
  });
  useLayoutEffect(
    () => () => {
      if (holdsFocusRef.current()) {
        noteFocusLeaving(identity);
      }
    },
    [identity],
  );
}

/** Options of {@link useCarriedFocus}. */
export interface CarriedFocusOptions {
  /**
   * Whether keyboard focus belongs to the card or row now: inside its elements, or inside a
   * dialog of its own. Called as it leaves the page, see {@link useFocusLeaving}.
   */
  holdsFocus: () => boolean;
  /**
   * Moves keyboard focus into the card or row that has just mounted, when focus is lost or
   * already inside it, never from where the user has put it. Called once, after the commit that
   * mounted it, when a card or row of the same identity left the page holding focus in that
   * commit.
   */
  takeFocus: () => void;
}

/**
 * Keeps keyboard focus on an item whose card or row React removes in one place and mounts in
 * another within one commit, such as a card a refetch moves into its group's "Computed from
 * replaced data" block (see the module overview): the card or row leaving with focus notes its
 * identity ({@link useFocusLeaving}), and the one mounting with that identity claims the note in
 * a layout effect, then calls `takeFocus` in a passive effect, once the removed card's dialog has
 * freed the page behind it. A note is claimed once, also under React StrictMode's repeated
 * effects.
 *
 * @param identity - the recommendation identity (`itemIdentity`) of the item rendered
 * @param options - see {@link CarriedFocusOptions}; the latest render's functions are used
 *
 * @example
 * ```ts
 * useCarriedFocus(itemIdentity(item), {
 *   holdsFocus: () => card.contains(document.activeElement),
 *   takeFocus: () => {
 *     if (focusIsLostOrWithin(card)) {
 *       moveFocusTo(card, true);
 *     }
 *   },
 * });
 * ```
 */
export function useCarriedFocus(identity: string, options: CarriedFocusOptions): void {
  useFocusLeaving(identity, options.holdsFocus);
  const takeFocusRef = useRef(options.takeFocus);
  useLayoutEffect(() => {
    takeFocusRef.current = options.takeFocus;
  });
  // Set by the claim, and kept until the passive effect has used it: StrictMode runs the layout
  // effect a second time, which finds the note claimed already and must not clear the flag.
  const carriedRef = useRef(false);
  useLayoutEffect(() => {
    if (leftHoldingFocus.delete(identity)) {
      carriedRef.current = true;
    }
  }, [identity]);
  useEffect(() => {
    if (!carriedRef.current) {
      return;
    }
    carriedRef.current = false;
    takeFocusRef.current();
  }, [identity]);
}
