/**
 * `SectionFrame`: the behaviour the five `/actions` sections share (AAP 0.5.5), so no section
 * file duplicates it.
 *
 * Each section (Keywords, Campaigns, Budget, Copy/creative, White papers) renders its cards
 * through one `SectionFrame`, which owns everything that is the same for all five:
 *
 * 1. the section region: a `PageShell.Section` with the anchor id `section-{key}` (the target of
 *    `SectionNav`) and the API's title as its `<h2>`;
 * 2. the section's findings first, one `Banner` each, so tracking findings sit at the top of
 *    each affected section (AAP 0.5.5). None carries `alert`: the `alert` role is reserved for
 *    the open-discrepancies banner in `GlobalBanners` (AAP 0.5.5 Behaviour);
 * 3. the groups in API order, each a `PageShell.Section` (`<h3>`) with the anchor id
 *    `group-{sectionKey}-{groupKey}`, except a group whose title repeats the section's
 *    ({@link groupRepeatsSectionTitle}), such as Budget's "Budget changes": it keeps that anchor
 *    on a `Stack` with no heading, so the section shows its title once. Visible cards come
 *    first, in API rank order; cards computed from replaced data follow, readable and never
 *    collapsed, under "Computed from replaced data" (AAP 0.3.2: a historical card stays
 *    readable with every action disabled, and no discrepancy resolution makes it current);
 *    cards held by a discrepancy come last, collapsed behind a "Held until discrepancies are
 *    resolved" disclosure (AAP 0.5.5 "Placement of items that must be resolved first"), which
 *    expands when the hash names one of them (`#item-{id}`); cards
 *    hidden by feedback are counted, never rendered (AAP 0.3.2: `current.held_now`,
 *    `current.historical` and `current.hidden_by` are read live). A card hidden by a feedback
 *    recorded on this page leaves its confirmation ("Dismissed: {action} ({entity})",
 *    `../ActionOutcomes`) in its place, between the same neighbours, for as long as the run
 *    still lists the item, so the outcome shows where the user acted; it is counted as hidden
 *    all the same;
 * 4. an `EmptyState` whose title is the one sentence that says why, when no group has a card or
 *    a confirmation to show (AAP 0.5.5): the cards feedback has hidden since the run and the
 *    run's suppression counts first, so a section emptied by feedback or suppression says so;
 *    else the leading withheld line's reason (`withheldReason`, for example "No search term met
 *    the K1 minimum of 70 clicks"); else, when datasets the section needs do not cover the
 *    window, one sentence summarising its `incomplete_dataset` findings
 *    (`incompleteDataReason`, for example "Data does not fully cover this window:
 *    google.entities, google.metrics_daily (no active Google dataset)"); else the first finding
 *    that explains the absence. "No rule fired for this window" is reserved for a section with
 *    none of these. The findings above and the lists below are not repeated inside it, and a
 *    long identifier the sentence quotes wraps at its underscores, as in the finding's banner;
 * 5. the patterns withheld for sample size, with observed and required sizes (AAP 0.5.4.2 P6);
 * 6. the counts of what the run did not show, by suppression reason (`run_suppression`,
 *    AAP 0.5.4.2), plus the cards hidden by feedback given since the run.
 *
 * The frame never renders a card itself: each section passes `renderItem`, which is how it
 * receives `RecommendationCard` without this file importing it (that import would close a cycle,
 * because the card imports {@link isHeld} from here). `renderGroup` replaces
 * one group's body (Copy/creative, White papers) and `renderGroups` the whole groups area
 * (Keywords renders one tab set across its groups).
 *
 * Presentation rules this file keeps:
 * - Every key, title, finding message, withheld line and count comes from the API. No threshold
 *   or window length is written into the client (AAP 0.5.4.1).
 * - Items keep the order the API ranked them in; the frame partitions, it never sorts.
 * - Long lists mount in bounded batches (`../batches`; AAP 0.1.1 "the five sections render from
 *   precomputed runs in under 2 s"): each card list ({@link CardList}) shows
 *   `CARD_BATCH_SIZE` cards at a time and the groups area `GROUP_BATCH_SIZE` groups at a time,
 *   each followed by a "Show {n} more" control. Every card that renders is the card
 *   `renderItem` returns, with its rank, flags and evidence, and a link to one not yet shown
 *   (`#item-{id}`, `#group-…`) reveals it.
 * - A card is keyed by its recommendation's identity across runs ({@link itemIdentity}), never by
 *   the per-run item id, so a newer run's items update the cards in place: keyboard focus stays
 *   on the control that had it (WCAG 2.2 SC 2.4.3). Anchors stay `item-{id}`.
 * - Built only on the design-system index (AAP 0.4.4): no raw buttons, headings or tables, no
 *   inline styles and no stylesheet. Layout comes from `Stack` and `Inline` alone.
 *
 * @packageDocumentation
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import type {
  Finding,
  FindingCode,
  Group,
  Item,
  Overlay,
  Run,
  Section,
  SectionKey,
} from '../../../api/types';
import {
  Banner,
  Button,
  EmptyState,
  Inline,
  PageShell,
  Stack,
  withIdentifierBreaks,
} from '../../../design-system';
import type { Tone } from '../../../design-system';
import { focusElement } from '../../../lib/focus';
import { UNAVAILABLE, formatCount } from '../../../lib/format';
import {
  FeedbackConfirmation,
  NO_FEEDBACK_CONFIRMATIONS,
  useFeedbackConfirmations,
  type FeedbackConfirmations,
} from '../ActionOutcomes';
import { WithheldList, withheldReason } from '../WithheldList';
import { discrepancyRef } from '../evidenceExtra';
import {
  ACTION_NOUN,
  BatchedList,
  CARD_BATCH_SIZE,
  GROUP_BATCH_SIZE,
  GROUP_NOUN,
  hashTarget,
  scrollToId,
} from '../batches';

// ---------------------------------------------------------------------------------------------
// Section component contract
// ---------------------------------------------------------------------------------------------

/**
 * Props every section component takes: `ActionsPage` renders each of the five sections as
 * `<XSection section={section} run={run} overlay={overlay} />`.
 */
export interface SectionComponentProps {
  /** The section to render, as `GET /recommendations` returned it (with the item overlay). */
  section: Section;
  /** The run the section belongs to, for its window and configured minimums. */
  run: Run;
  /** The page-level current state, such as the live open-discrepancy count. */
  overlay: Overlay;
}

// ---------------------------------------------------------------------------------------------
// Anchors
// ---------------------------------------------------------------------------------------------

/**
 * Every code point outside `[A-Za-z0-9_-]`. The `u` flag makes a character outside the Basic
 * Multilingual Plane one match, so it becomes one `-` rather than one per UTF-16 surrogate.
 */
const ANCHOR_UNSAFE = /[^A-Za-z0-9_-]/gu;

/**
 * The anchor id of a section region, the target of the section navigation's links.
 *
 * @param key - the section key
 * @returns `section-{key}`
 *
 * @example
 * ```ts
 * sectionAnchorId('keywords'); // "section-keywords"
 * ```
 */
export function sectionAnchorId(key: SectionKey): string {
  return `section-${key}`;
}

/**
 * The anchor id of a group region within a section: `group-{sectionKey}-{groupKey}`, with every
 * character outside `[A-Za-z0-9_-]` replaced by `-`, so dynamic group keys such as
 * `campaign:123` or `cohort:EUR` yield a valid, link-safe id.
 *
 * @param sectionKey - the section the group belongs to
 * @param groupKey - the group key, as the API returned it
 * @returns the sanitised anchor id
 *
 * @example
 * ```ts
 * groupAnchorId('creative', 'campaign:123'); // "group-creative-campaign-123"
 * groupAnchorId('white_papers', 'cohort:EUR'); // "group-white_papers-cohort-EUR"
 * groupAnchorId('campaigns', 'discrepancies'); // DISCREPANCY_GROUP_ANCHOR
 * ```
 */
export function groupAnchorId(sectionKey: SectionKey, groupKey: string): string {
  return `group-${sectionKey}-${groupKey}`.replace(ANCHOR_UNSAFE, '-');
}

/**
 * The anchor id of the Campaigns section's discrepancy group, equal to
 * `groupAnchorId('campaigns', 'discrepancies')`. The open-discrepancies banner points here, and
 * so does a held card's "View open discrepancies" link when no specific blocker can be named
 * (AAP 0.5.5).
 */
export const DISCREPANCY_GROUP_ANCHOR = 'group-campaigns-discrepancies';

/**
 * The anchor id of an item's card: `item-{id}`, the id `RecommendationCard` gives its
 * `<article>` (written here rather than imported from the card, which imports this module). A
 * held card's link to its blocking C0 card and a B3 card's link to the B1 it funds point here,
 * and a card list reveals the card a hash names ({@link CardList}).
 *
 * @param id - the item id
 * @returns `item-{id}`
 *
 * @example
 * ```ts
 * itemAnchorId(1201); // "item-1201"
 * ```
 */
export function itemAnchorId(id: Item['id']): string {
  return `item-${String(id)}`;
}

// ---------------------------------------------------------------------------------------------
// Item state
// ---------------------------------------------------------------------------------------------

/**
 * Whether an item was computed from data that has since been replaced (AAP 0.3.2 "Items from
 * replaced data are historical-only"): the overlay found one of its provenance datasets no
 * longer active. Such an item stays readable with every action disabled, and only a run on the
 * current data supersedes it; resolving a discrepancy never makes it current.
 *
 * @param item - the item
 * @returns `true` when `current.historical` is set
 */
export function isHistorical(item: Item): boolean {
  return item.current.historical;
}

/**
 * Whether an item is held until discrepancies are resolved: held when the run computed it, or
 * held now by a discrepancy the overlay reads live, including one detected after the run
 * (AAP 0.5.4.2 P1, 0.3.2).
 *
 * A historical item ({@link isHistorical}) is never held in this sense. The overlay sets its
 * `held_now` with the reason "Computed from data that has since been replaced" (AAP 0.3.2), a
 * hold no discrepancy resolution lifts, so it is shown openly under "Computed from replaced
 * data" ({@link HistoricalItems}) rather than behind the held disclosure.
 *
 * @param item - the item
 * @returns `true` when the item belongs behind the held disclosure
 */
export function isHeld(item: Item): boolean {
  return !isHistorical(item) && (item.held || item.current.held_now);
}

/**
 * Whether active feedback (dismiss, snooze or done) given since the run hides the item now
 * (AAP 0.3.2: feedback is read live). A hidden item is counted, never rendered.
 *
 * @param item - the item
 * @returns `true` when `current.hidden_by` is set
 */
export function isHidden(item: Item): boolean {
  const hiddenBy = item.current.hidden_by;
  return hiddenBy !== null && hiddenBy !== undefined;
}

/** The result of {@link partitionGroup}. */
export interface GroupPartition {
  /** Items neither hidden, historical nor held, in API order. */
  visible: Item[];
  /** Items computed from replaced data and not hidden, in API order. */
  historical: Item[];
  /** Items held until discrepancies are resolved and not hidden, in API order. */
  held: Item[];
  /** How many items feedback hides now. */
  hiddenCount: number;
}

/**
 * Splits a group's items into what the group shows openly, what it shows as computed from
 * replaced data, what it shows behind the held disclosure and what it only counts.
 *
 * - visible: not hidden, not historical and not held;
 * - historical: historical ({@link isHistorical}) and not hidden, whatever its hold;
 * - held: held until discrepancies are resolved ({@link isHeld}) and not hidden;
 * - hidden ({@link isHidden}): counted in `hiddenCount`, whether historical, held or not.
 *
 * Every list keeps the API order: ranks are the API's, so the frame never re-sorts.
 *
 * @param group - the group, as the API returned it
 * @returns the partition
 */
export function partitionGroup(group: Group): GroupPartition {
  const visible: Item[] = [];
  const historical: Item[] = [];
  const held: Item[] = [];
  let hiddenCount = 0;
  for (const item of group.items) {
    if (isHidden(item)) {
      hiddenCount += 1;
    } else if (isHistorical(item)) {
      historical.push(item);
    } else if (isHeld(item)) {
      held.push(item);
    } else {
      visible.push(item);
    }
  }
  return { visible, historical, held, hiddenCount };
}

/**
 * Whether the page shows a confirmation in an item's place: feedback hides the item now, and
 * that feedback was recorded on this page, so `confirmations` holds its recommendation's
 * message (`../ActionOutcomes`).
 *
 * @param item - the item
 * @param confirmations - the feedback confirmations recorded on this page, by
 *   {@link itemIdentity}
 * @returns `true` for a hidden item with a recorded confirmation
 */
export function isConfirmed(item: Item, confirmations: FeedbackConfirmations): boolean {
  return isHidden(item) && confirmations.has(itemIdentity(item));
}

/** A group's three card lists with their confirmations in place ({@link placeConfirmations}). */
export interface PlacedLists {
  /** The visible items, and the confirmed items neither historical nor held, in API order. */
  visible: Item[];
  /** The items computed from replaced data, and the confirmed ones among them, in API order. */
  historical: Item[];
  /** The held items, and the confirmed ones a discrepancy holds, in API order. */
  held: Item[];
  /** How many confirmed items ({@link isConfirmed}) the three lists hold. */
  confirmed: number;
}

/**
 * The lists {@link partitionGroup} makes, with each confirmed item ({@link isConfirmed}) placed
 * in the list it would stand in were it not hidden, at its API position: computed from replaced
 * data, else held, else visible. A {@link CardList} given such a list renders the confirmation
 * where the card stood, between the same neighbours. Every other hidden item is left out, as
 * the partition leaves it out, and the partition's own lists and `hiddenCount` are unchanged,
 * so every count the page shows still counts the item as hidden.
 *
 * @param group - the group, as the API returned it
 * @param confirmations - the feedback confirmations recorded on this page, by
 *   {@link itemIdentity}
 * @returns the placed lists
 *
 * @example
 * ```ts
 * // A group [visible 1, hidden 2 (dismissed on this page), visible 3]:
 * placeConfirmations(group, confirmations).visible; // [item 1, item 2, item 3]
 * partitionGroup(group).visible; // [item 1, item 3]
 * ```
 */
export function placeConfirmations(
  group: Group,
  confirmations: FeedbackConfirmations,
): PlacedLists {
  const visible: Item[] = [];
  const historical: Item[] = [];
  const held: Item[] = [];
  let confirmed = 0;
  for (const item of group.items) {
    if (isHidden(item)) {
      if (!isConfirmed(item, confirmations)) {
        continue;
      }
      confirmed += 1;
    }
    if (isHistorical(item)) {
      historical.push(item);
    } else if (isHeld(item)) {
      held.push(item);
    } else {
      visible.push(item);
    }
  }
  return { visible, historical, held, confirmed };
}

/**
 * One group as the frame prepared it, handed to `renderGroup` and `renderGroups` so an override
 * can reuse the frame's partition, anchor and default body.
 */
export interface GroupParts {
  /** The group, as the API returned it. */
  group: Group;
  /** The group's anchor id ({@link groupAnchorId}). */
  anchorId: string;
  /** Items neither hidden, historical nor held, in API order. */
  visible: readonly Item[];
  /** Items computed from replaced data and not hidden, in API order. */
  historical: readonly Item[];
  /** Items held until discrepancies are resolved and not hidden, in API order. */
  held: readonly Item[];
  /** How many of the group's items feedback hides now. */
  hiddenCount: number;
  /**
   * The feedback confirmations recorded on this page for the section's hidden items, by
   * {@link itemIdentity}: what a {@link CardList} or {@link ConfirmedItems} of an override
   * needs to show a confirmation where a hidden card stood.
   */
  confirmations: FeedbackConfirmations;
  /**
   * The group's lists with each confirmed item in place ({@link placeConfirmations}); `placed`
   * `.confirmed` counts them. `visible`, `historical`, `held` and `hiddenCount` never include
   * them, so every count keeps counting the item as hidden.
   */
  placed: PlacedLists;
  /**
   * The frame's default group body: a `Stack` of `renderItem` for each visible item, then the
   * {@link HistoricalItems} block holding `renderItem` for each historical item, then the
   * {@link HeldDisclosure} holding `renderItem` for each held item, which expands when the hash
   * names one of the held cards (`#item-{id}`). Each of the three is a {@link CardList} of the
   * `placed` list, so a long one mounts `CARD_BATCH_SIZE` cards at a time and an item a
   * feedback recorded on this page has hidden shows its confirmation where its card stood; when
   * the block it stood in is gone with it (no historical or held item is left), the
   * confirmation takes the block's place.
   */
  body: ReactNode;
}

// ---------------------------------------------------------------------------------------------
// Card lists
// ---------------------------------------------------------------------------------------------

/** The rule whose items are discrepancies (C0): one item per discrepancy, not per subject. */
const DISCREPANCY_RULE = 'C0';

/**
 * The identity of an item's recommendation across runs: what stays the same when a newer run
 * recomputes the same action on the same entity, while its `id` (one `recommendation` row per
 * run) changes with every run.
 *
 * It is the key feedback matches an item by in later runs (the rule id with the entity's type
 * and id, `analysis/overlay.py` `FeedbackKey`), plus the entity's source. The entity id is built
 * from platform ids, never internal row ids, so it survives a re-sync and a dataset transition
 * of the same platform entity. A C0 discrepancy adds its discrepancy id (`extra.discrepancy_id`,
 * {@link discrepancyRef}): there is one C0 item per discrepancy, and one subject can have
 * several. The parts are serialised as one JSON array, so separators inside an entity id (such
 * as `|` and `:`) never make two different identities equal.
 *
 * An item whose entity has no id (`null` or blank) has no identity across runs and gets
 * `run-item:{id}`, which lasts for its run only and never equals an identity built from parts,
 * since that always starts with `[`.
 *
 * @param item - the item
 * @returns the identity, as a string
 *
 * @example
 * ```ts
 * itemIdentity(k1Item); // '["K1","google","search_term","61000201|7700119|crm demo"]'
 * itemIdentity({ ...k1Item, entity: { ...k1Item.entity, id: null } }); // "run-item:1"
 * ```
 */
export function itemIdentity(item: Item): string {
  const { entity } = item;
  // A runtime payload may still send a non-string id, which has no identity across runs.
  if (typeof entity.id !== 'string' || entity.id.trim() === '') {
    return `run-item:${String(item.id)}`;
  }
  const parts: (string | null)[] = [item.rule_id, entity.source, entity.type, entity.id];
  if (item.rule_id === DISCREPANCY_RULE) {
    parts.push(discrepancyRef(item)?.discrepancyId ?? null);
  }
  return JSON.stringify(parts);
}

/**
 * The React key of each item's card in one list: its {@link itemIdentity}, so a newer run's item
 * of the same recommendation keeps the card, with its DOM, keyboard focus and local state, in
 * place of remounting it (WCAG 2.2 SC 2.4.3). A later item of the list with an identity already
 * taken gets `#{n}` after it, its occurrence count, so every key in the list is unique even when
 * two items share an identity. An identity ends with `]` and a `run-item:{id}` fallback holds no
 * `#`, so a suffixed key never equals another item's key.
 *
 * Keys are given in list order, over the whole list rather than the batch shown, so revealing
 * more of the list never changes the key of a card already shown. An item object listed twice
 * keeps the key of its first occurrence.
 *
 * @param items - the list's items, in API order
 * @returns each item's key
 *
 * @example
 * ```tsx
 * const keys = itemKeys(items);
 * items.map((item) => <Fragment key={keys.get(item)}>{renderCard(item)}</Fragment>);
 * ```
 */
export function itemKeys(items: readonly Item[]): ReadonlyMap<Item, string> {
  const keys = new Map<Item, string>();
  const occurrences = new Map<string, number>();
  for (const item of items) {
    if (keys.has(item)) {
      continue;
    }
    const identity = itemIdentity(item);
    const seen = occurrences.get(identity) ?? 0;
    occurrences.set(identity, seen + 1);
    keys.set(item, seen === 0 ? identity : `${identity}#${String(seen)}`);
  }
  return keys;
}

/** The anchors a card holds: its `item-{id}` ({@link itemAnchorId}). */
function itemAnchors(item: Item): readonly string[] {
  return [itemAnchorId(item.id)];
}

/** The card's own `<article>`, which takes focus when the card leads a revealed batch. */
function itemFocusId(item: Item): string {
  return itemAnchorId(item.id);
}

/** Props of {@link CardList}. */
export interface CardListProps {
  /**
   * The items, in API order; never re-sorted. An item feedback hides ({@link isHidden}) shows
   * its confirmation from `confirmations` in its place, or nothing when it has none, such as a
   * `placed` list of {@link placeConfirmations}.
   */
  items: readonly Item[];
  /** Renders one item as its card, such as a `RecommendationCard`. */
  renderCard: (item: Item) => ReactNode;
  /**
   * The feedback confirmations recorded on this page, by {@link itemIdentity}
   * (`GroupParts.confirmations`). Omitted, a hidden item shows nothing.
   */
  confirmations?: FeedbackConfirmations;
}

/** One confirmation a card list shows in a hidden card's place, with its React key. */
interface ListConfirmation {
  /** Unique among the confirmations shown at the same place. */
  readonly key: string;
  /** The confirmation, for example "Dismissed: Pause (Generic CRM — US)". */
  readonly message: string;
}

/** No confirmations at a place in a card list. */
const NO_LIST_CONFIRMATIONS: readonly ListConfirmation[] = Object.freeze([]);

/** How a card list lays out its items ({@link cardListLayout}). */
interface CardListLayout {
  /** The items shown as cards: those feedback does not hide, in API order. */
  readonly cards: readonly Item[];
  /** The confirmations of hidden items that come before every card, in API order. */
  readonly leading: readonly ListConfirmation[];
  /** The confirmations of hidden items that follow each card, before the next card. */
  readonly after: ReadonlyMap<Item, readonly ListConfirmation[]>;
}

/**
 * Splits a card list into its cards and the confirmations of its confirmed items
 * ({@link isConfirmed}), each placed directly after the card before it, or before every card,
 * so it stands exactly where its card did. A hidden item without a confirmation is left out.
 *
 * Only the cards are the batched entries: a confirmation never counts as an action in the
 * list's "Showing {n} of {total} actions" status, and it always shows, because the card before
 * it was shown when the user acted on the card it replaces. Each confirmation is keyed by its
 * recommendation identity, with `#{n}` after a repeat, so its key is unique where it stands.
 */
function cardListLayout(
  items: readonly Item[],
  confirmations: FeedbackConfirmations,
): CardListLayout {
  const cards: Item[] = [];
  const leading: ListConfirmation[] = [];
  const after = new Map<Item, ListConfirmation[]>();
  const occurrences = new Map<string, number>();
  for (const item of items) {
    if (!isHidden(item)) {
      cards.push(item);
      continue;
    }
    const identity = itemIdentity(item);
    const message = confirmations.get(identity);
    if (message === undefined) {
      continue;
    }
    const seen = occurrences.get(identity) ?? 0;
    occurrences.set(identity, seen + 1);
    const confirmation: ListConfirmation = {
      key: seen === 0 ? `confirmation:${identity}` : `confirmation:${identity}#${String(seen)}`,
      message,
    };
    const previous = cards.at(-1);
    if (previous === undefined) {
      leading.push(confirmation);
    } else {
      const following = after.get(previous);
      if (following === undefined) {
        after.set(previous, [confirmation]);
      } else {
        following.push(confirmation);
      }
    }
  }
  return { cards, leading, after };
}

/** Each confirmation as its {@link FeedbackConfirmation}, keyed. */
function renderConfirmations(confirmations: readonly ListConfirmation[]): ReactNode {
  return confirmations.map(({ key, message }) => (
    <FeedbackConfirmation key={key} message={message} />
  ));
}

/**
 * A list of cards in API order, mounted `CARD_BATCH_SIZE` at a time (`../batches`). Each card
 * is exactly what `renderCard` returns, in a `Fragment` keyed by the item's recommendation
 * identity across runs ({@link itemKeys}), so it stays a direct child of the enclosing `Stack`
 * and keeps its rank, flags and evidence. When a newer run replaces the items, every id changes
 * but a card of the same recommendation keeps its element: keyboard focus on one of its
 * controls, and the card's local state, survive the swap instead of falling to the page
 * (WCAG 2.2 SC 2.4.3).
 *
 * A list longer than one batch is followed by a polite "Showing {n} of {total} actions" status
 * and a "Show {k} more actions" button; each press reveals the next batch and moves focus to its
 * first card (the card's `item-{id}` element). A list at or under one batch renders every card
 * and no control, exactly as an unbatched map.
 *
 * When `location.hash` names a card not shown yet (`#item-{id}`, such as a held card's link to
 * its blocking C0 card), the list reveals the batches through it and scrolls it into view, on
 * mount and on `hashchange`; a `hashchange` also moves focus to it.
 *
 * An item a feedback recorded on this page has hidden ({@link isConfirmed}) shows, in its card's
 * place, its {@link FeedbackConfirmation} from `confirmations`: "Dismissed: {action}
 * ({entity})", directly after the card before it, so the outcome is visible where the user
 * acted (WCAG 2.2 SC 4.1.3, beside the polite announcement of `../ActionOutcomes`). It is no
 * card: it is no batched entry, counts in no status, holds no control and takes no focus, so
 * keyboard focus, recovered to the next card's first action, lands right after it. A list that
 * holds only confirmations renders them alone. Any other hidden item is left out.
 *
 * Exported so a section that builds a group body of its own (Campaigns' discrepancy group)
 * bounds its lists the same way.
 *
 * @param props - see {@link CardListProps}
 * @returns the shown cards with their confirmations, then the control when the list is batched
 *
 * @example
 * ```tsx
 * <Stack>
 *   <CardList
 *     items={parts.placed.visible}
 *     confirmations={parts.confirmations}
 *     renderCard={(item) => <RecommendationCard item={item} />}
 *   />
 * </Stack>
 * ```
 */
export function CardList({
  items,
  renderCard,
  confirmations = NO_FEEDBACK_CONFIRMATIONS,
}: CardListProps): ReactElement {
  const layout = useMemo(() => cardListLayout(items, confirmations), [items, confirmations]);
  const keys = useMemo(() => itemKeys(layout.cards), [layout.cards]);
  return (
    <>
      {renderConfirmations(layout.leading)}
      <BatchedList
        entries={layout.cards}
        batchSize={CARD_BATCH_SIZE}
        noun={ACTION_NOUN}
        getKey={(item) => keys.get(item) ?? itemIdentity(item)}
        // Every card renders in the same two-part fragment, so a confirmation appearing after it
        // never remounts the card, and keyboard focus and its local state stay.
        renderEntry={(item) => (
          <>
            {renderCard(item)}
            {renderConfirmations(layout.after.get(item) ?? NO_LIST_CONFIRMATIONS)}
          </>
        )}
        anchorsOf={itemAnchors}
        focusIdOf={itemFocusId}
      />
    </>
  );
}

/** Props of {@link ConfirmedItems}. */
export interface ConfirmedItemsProps {
  /** The items, in API order, such as a `placed` list; only the confirmed ones render. */
  items: readonly Item[];
  /** The feedback confirmations recorded on this page (`GroupParts.confirmations`). */
  confirmations: FeedbackConfirmations;
}

/**
 * The {@link FeedbackConfirmation} of each confirmed item among `items` ({@link isConfirmed}),
 * in API order, for a section whose items stand where no confirmation can: the rows of a
 * `DataTable`, such as the white-paper asset tables. The section renders them directly after
 * the table that held the rows, so each outcome shows beside where it was given.
 *
 * @param props - see {@link ConfirmedItemsProps}
 * @returns the confirmations, or `null` when there is none
 *
 * @example
 * ```tsx
 * <AssetTable items={parts.visible} />
 * <ConfirmedItems items={parts.placed.visible} confirmations={parts.confirmations} />
 * ```
 */
export function ConfirmedItems({ items, confirmations }: ConfirmedItemsProps): ReactElement | null {
  const { leading } = cardListLayout(items.filter(isHidden), confirmations);
  return leading.length === 0 ? null : <>{renderConfirmations(leading)}</>;
}

// ---------------------------------------------------------------------------------------------
// Items computed from replaced data
// ---------------------------------------------------------------------------------------------

/**
 * The label of a group's block of items computed from replaced data. It repeats the badge each
 * of those cards carries (AAP 0.5.5 "Historical items": "Computed from replaced data"). The text
 * is written here rather than imported from `RecommendationCard`, which imports this module.
 */
export const REPLACED_DATA_LABEL = 'Computed from replaced data';

/** Props of {@link HistoricalItems}. */
export interface HistoricalItemsProps {
  /** The block's id, unique on the page, for example `${anchorId}-historical`. */
  id: string;
  /** How many items the block holds; shown in its label. */
  count: number;
  /** The items computed from replaced data, such as their cards, in API order. */
  children: ReactNode;
}

/**
 * A group's items computed from replaced data (AAP 0.3.2 "Items from replaced data are
 * historical-only": "The card stays readable"), after the group's visible cards and before its
 * {@link HeldDisclosure}.
 *
 * Renders a `Stack` with this `id`, exposed as a group named "Computed from replaced data
 * ({count})", whose first line shows that name, followed by the children. The block is never
 * collapsed. Each card inside keeps its own badge, original provenance and disabled actions,
 * which the card renders. The held disclosure therefore holds only non-historical items a
 * discrepancy holds, which wait on that discrepancy's resolution, and the rerun it triggers,
 * before they can be trusted.
 *
 * Exported so a section that builds a group body of its own (Campaigns' discrepancy group) uses
 * the same block.
 *
 * @param props - see {@link HistoricalItemsProps}
 * @returns the block, or `null` when there is nothing in it (`count` is not positive)
 *
 * @example
 * ```tsx
 * <HistoricalItems id={`${parts.anchorId}-historical`} count={parts.historical.length}>
 *   <CardList items={parts.historical} renderCard={(item) => <RecommendationCard item={item} />} />
 * </HistoricalItems>
 * ```
 */
export function HistoricalItems({
  id,
  count,
  children,
}: HistoricalItemsProps): ReactElement | null {
  // `!(count > 0)` also rejects NaN, so a malformed count never renders an empty block.
  if (!(count > 0)) {
    return null;
  }
  const label = `${REPLACED_DATA_LABEL} (${formatCount(count)})`;
  return (
    <Stack id={id} aria-label={label}>
      {/*
        BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive and Stack takes no
        aria-labelledby, so the visible label is a <div> and the block carries the same text as
        its aria-label. The line is hidden from assistive technology, which announces the text
        once, as the group's name, instead of reading it twice.
      */}
      <div aria-hidden="true">{label}</div>
      {children}
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// Held disclosure
// ---------------------------------------------------------------------------------------------

/** Props of {@link HeldDisclosure}. */
export interface HeldDisclosureProps {
  /**
   * The id of the collapsible region, which the toggle names in `aria-controls`; unique on the
   * page, for example `${anchorId}-held`.
   */
  id: string;
  /** How many held items the region holds; shown in the toggle's label. */
  count: number;
  /**
   * The ids of the in-page link targets the held content renders, such as the `item-{id}` of
   * each held card ({@link itemAnchorId}). When `location.hash` names one of them, on mount or
   * on a `hashchange`, the region expands and, once its content has rendered, the named element
   * is scrolled into view; after a `hashchange` (the link followed) it also takes focus. A link
   * to a held card (such as a C2 "Scale" card's link to its held B1 card) therefore reveals it
   * and puts focus on it. Omitted, the disclosure ignores the hash.
   */
  anchors?: readonly string[];
  /**
   * The held content, such as the held cards or a table of held rows. It mounts the first time
   * the region is expanded, never before.
   */
  children: ReactNode;
}

/** A held element a hash names, revealed once the expanded region's content has rendered. */
interface HeldReveal {
  /** The id the hash names: one of the disclosure's `anchors`. */
  readonly target: string;
  /**
   * Whether the element then takes focus: after a `hashchange` (the link followed), never for
   * the hash the disclosure mounted with (a page load).
   */
  readonly focus: boolean;
}

/** The id `location.hash` names when it is one of `anchors`, else `null`. */
function heldHashTarget(anchors: readonly string[] | undefined): string | null {
  const target = hashTarget();
  return target !== null && anchors?.includes(target) === true ? target : null;
}

/**
 * The collapsed list of held items at the end of a group (AAP 0.5.5: "Held cards appear
 * collapsed at the end of their group under 'Held until discrepancies are resolved'").
 *
 * Renders a quiet `Button` labelled "Held until discrepancies are resolved ({count})" that
 * carries `aria-expanded` and `aria-controls={id}`, and a region with that `id` that is
 * `hidden` until the button expands it. The region is collapsed on first render, unless the
 * hash names one of its `anchors`. It is a real button, so it is reachable with Tab and operated
 * with Enter or Space (WCAG 2.2 SC 2.1.1, 4.1.2). The button sits in an `Inline` row, so it
 * keeps its own width instead of stretching across the column.
 *
 * The region element is always rendered, so `aria-controls` always resolves, but the children
 * mount only the first time the region is expanded: a disclosure nobody opens formats and mounts
 * none of its cards. Once mounted they stay mounted, hidden, while the region is collapsed
 * again, so a held card keeps its local state across a collapse.
 *
 * An in-page link to a held card opens the disclosure: when `location.hash` names one of
 * `anchors`, as the disclosure mounts (a page loaded at `#item-{id}`) or on a `hashchange` (the
 * link followed), the region expands, mounting its content if it was never opened, and once that
 * content has rendered the named element is scrolled into view, as a batched list reveals an
 * entry a hash names (`../batches`). After a `hashchange`, focus then moves to the element
 * without scrolling again ({@link focusElement}, a temporary `tabindex="-1"`), as the root route
 * focuses a link target already rendered (`src/router.tsx`), so the next Tab continues from the
 * card, not from the link the page scrolled away from (WCAG 2.2 SC 2.4.3). An element that
 * already holds focus, such as a card the list inside has just revealed and focused, keeps it.
 * A hash present as the disclosure mounts, as on a page load, moves no focus. The button can
 * collapse the region again. Expanding releases no hold: the cards stay under "Held until
 * discrepancies are resolved". The card list inside reveals its later batches through the named
 * card itself ({@link CardList}).
 *
 * Only non-historical items a discrepancy holds ({@link isHeld}) belong here: they wait on that
 * discrepancy's resolution, and the rerun it triggers, before they can be trusted. Items computed
 * from replaced data go in {@link HistoricalItems} instead.
 *
 * Exported so a section that renders held rows in a table of their own (White papers) puts them
 * behind the same control.
 *
 * @param props - see {@link HeldDisclosureProps}
 * @returns the disclosure, or `null` when there is nothing held (`count` is not positive)
 *
 * @example
 * ```tsx
 * <HeldDisclosure
 *   id={`${parts.anchorId}-held`}
 *   count={parts.held.length}
 *   anchors={parts.held.map((item) => itemAnchorId(item.id))}
 * >
 *   <CardList items={parts.held} renderCard={(item) => <RecommendationCard item={item} />} />
 * </HeldDisclosure>
 * ```
 */
export function HeldDisclosure({
  id,
  count,
  anchors,
  children,
}: HeldDisclosureProps): ReactElement | null {
  // The held target the hash names as the disclosure mounts: it then starts expanded.
  const [mountTarget] = useState(() => heldHashTarget(anchors));
  const [expanded, setExpanded] = useState(mountTarget !== null);
  // Whether the region has ever been expanded: its children mount then and stay mounted.
  const [opened, setOpened] = useState(mountTarget !== null);
  // How many expansions a `hashchange` asked for, so each one renders, then scrolls and focuses.
  const [hashReveals, setHashReveals] = useState(0);
  /** The hash target to reveal once the expanded region's content has rendered. */
  const pendingReveal = useRef<HeldReveal | null>(
    mountTarget === null ? null : { target: mountTarget, focus: false },
  );
  /** The latest render's anchors, read by the `hashchange` listener. */
  const latestAnchors = useRef(anchors);

  useLayoutEffect(() => {
    latestAnchors.current = anchors;
  });

  const hashAware = anchors !== undefined;
  useEffect(() => {
    if (!hashAware) {
      return undefined;
    }
    const onHashChange = (): void => {
      const target = heldHashTarget(latestAnchors.current);
      if (target === null) {
        return;
      }
      pendingReveal.current = { target, focus: true };
      setExpanded(true);
      setOpened(true);
      setHashReveals((reveals) => reveals + 1);
    };
    window.addEventListener('hashchange', onHashChange);
    return () => {
      window.removeEventListener('hashchange', onHashChange);
    };
  }, [hashAware]);

  // Once the expansion a hash asked for has rendered, on mount or after a `hashchange`, the
  // named element exists and is shown: scroll it into view and, after a `hashchange`, focus it
  // without scrolling again. The card list inside runs its effects first, so an element it has
  // just revealed and focused already holds focus and is not focused a second time.
  useEffect(() => {
    const reveal = pendingReveal.current;
    pendingReveal.current = null;
    if (reveal === null || !scrollToId(reveal.target) || !reveal.focus) {
      return;
    }
    const element = document.getElementById(reveal.target);
    if (element !== null && element.ownerDocument.activeElement !== element) {
      focusElement(element, { preventScroll: true });
    }
  }, [hashReveals]);

  // `!(count > 0)` also rejects NaN, so a malformed count never renders an empty disclosure.
  if (!(count > 0)) {
    return null;
  }

  return (
    <Stack>
      <Inline>
        <Button
          variant="quiet"
          aria-expanded={expanded}
          aria-controls={id}
          onClick={() => {
            setExpanded((open) => !open);
            setOpened(true);
          }}
        >
          {`Held until discrepancies are resolved (${formatCount(count)})`}
        </Button>
      </Inline>
      <div id={id} hidden={!expanded}>
        {opened ? <Stack>{children}</Stack> : null}
      </div>
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// Labelled fields
// ---------------------------------------------------------------------------------------------

/** One labelled field of a card, such as "Spend" and its formatted amount. */
export interface FieldItem {
  /** Identifies the field within its list, for example the metric key. */
  key: string;
  /** The visible label, for example "Cost/conversion". */
  label: string;
  /**
   * The formatted value. A missing value (`null`, `undefined`, a boolean or an empty string)
   * renders "unavailable", so the list never shows a label with nothing after it.
   */
  value: ReactNode;
}

/** Props of {@link FieldList}. */
export interface FieldListProps {
  /** The fields, in the order they must appear. */
  fields: readonly FieldItem[];
  /** The list's accessible name, for example "Keyword details". Omitted, the list is unnamed. */
  label?: string;
}

/**
 * Whether a slot holds something React renders: `undefined`, `null`, booleans and the empty
 * string render nothing.
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * A card's labelled fields as a list, one "{label}: {value}" entry per field, in exactly the
 * order given, so each section's field order is explicit and testable (AAP 0.5.5 field sets).
 *
 * Renders a `Stack as="ul"` (gap step 1, `role="list"`) named by `label` when given, with one
 * `<li>` per field. A field without a value shows "unavailable", never an empty value or the
 * text "null".
 *
 * @param props - see {@link FieldListProps}
 * @returns the list, or `null` when there are no fields
 *
 * @example
 * ```tsx
 * <FieldList
 *   label="Keyword details"
 *   fields={[
 *     { key: 'term', label: 'Term', value: 'crm software' },
 *     { key: 'spend', label: 'Spend', value: 'USD 412.30' },
 *   ]}
 * />
 * // <ul aria-label="Keyword details"><li>Term: crm software</li><li>Spend: USD 412.30</li></ul>
 * ```
 */
export function FieldList({ fields, label }: FieldListProps): ReactElement | null {
  if (fields.length === 0) {
    return null;
  }
  return (
    <Stack as="ul" gap={1} aria-label={label}>
      {fields.map((field, index) => (
        <li key={`${field.key}-${index}`}>
          {`${field.label}: `}
          {hasContent(field.value) ? field.value : UNAVAILABLE}
        </li>
      ))}
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// Suppression reasons and finding tones
// ---------------------------------------------------------------------------------------------

/**
 * The display label of each suppression reason the runner writes to `run_suppression`
 * (`analysis.context.SUPPRESSION_REASONS`; AAP 0.5.4.2):
 *
 * - `feedback_dismiss`, `feedback_snooze`, `feedback_done`: items hidden by feedback;
 * - `g1_blocked`: spend increases removed by the G1 guard;
 * - `tracking_absent`: zero-outcome rules suppressed by the P2 tracking finding.
 *
 * A line reads "{count} {label}", for example "2 dismissed". A label that names a plural noun
 * has a singular form in {@link SUPPRESSION_SINGULAR_LABELS}, which a count of exactly 1 uses
 * instead: "1 spend increase blocked by the high-utilization guard".
 */
export const SUPPRESSION_LABELS: Readonly<Record<string, string>> = Object.freeze({
  feedback_dismiss: 'dismissed',
  feedback_snooze: 'snoozed',
  feedback_done: 'marked done',
  g1_blocked: 'spend increases blocked by the high-utilization guard',
  tracking_absent: 'suppressed until tracking is verified',
});

/**
 * The singular form of each {@link SUPPRESSION_LABELS} label that names a plural noun, used for
 * a count of exactly 1. Only `g1_blocked` has one: every other label ("dismissed", "snoozed",
 * "marked done", "suppressed until tracking is verified") reads correctly at any count.
 */
const SUPPRESSION_SINGULAR_LABELS: Readonly<Record<string, string>> = Object.freeze({
  g1_blocked: 'spend increase blocked by the high-utilization guard',
});

/**
 * The display label of a suppression reason ({@link SUPPRESSION_LABELS}). A reason the client
 * does not know, such as one added to the backend later, is shown as its raw key, so its count
 * is never dropped. Only the map's own keys match, so a key such as `constructor` is not read
 * from the object prototype.
 *
 * @param reason - the `run_suppression.reason` key
 * @returns the label
 */
export function suppressionLabel(reason: string): string {
  return Object.hasOwn(SUPPRESSION_LABELS, reason)
    ? (SUPPRESSION_LABELS[reason] ?? reason)
    : reason;
}

/**
 * The suppression counts a section reports: each `[reason, count]` entry of `suppressed` whose
 * count is finite and positive, in API order. The "Not shown" list, the empty-state reason and
 * a Keywords sub-list's empty-tab reason all read this one result, so they never disagree. The
 * counts are the section's, never a rule's (AAP 0.5.3 `Section.suppressed: {reason: count}`).
 *
 * @param section - the section, as the API returned it
 * @returns the entries, in API order; empty when the section suppressed nothing
 *
 * @example
 * ```ts
 * reportedSuppressions({ ...section, suppressed: { feedback_dismiss: 2, g1_blocked: 0 } });
 * // [['feedback_dismiss', 2]]
 * ```
 */
export function reportedSuppressions(section: Section): [string, number][] {
  return Object.entries(section.suppressed).filter(
    ([, count]) => Number.isFinite(count) && count > 0,
  );
}

/**
 * One suppression count as a line, "{count} {label}", for example "2 dismissed". A count of
 * exactly 1 takes the reason's singular label when {@link SUPPRESSION_SINGULAR_LABELS} has one
 * ("1 spend increase blocked by the high-utilization guard"); every other count, and every
 * reason without a singular form, takes {@link suppressionLabel}. Only the singular map's own
 * keys match, so a key such as `constructor` is not read from the object prototype. The "Not
 * shown" list, {@link notShownReason} and a Keywords sub-list's empty-tab reason all write their
 * counts with it, so a count reads the same everywhere.
 *
 * @param reason - the `run_suppression.reason` key
 * @param count - the count, finite and positive ({@link reportedSuppressions})
 * @returns the line
 *
 * @example
 * ```ts
 * suppressionLine('feedback_dismiss', 2); // "2 dismissed"
 * suppressionLine('g1_blocked', 1); // "1 spend increase blocked by the high-utilization guard"
 * ```
 */
export function suppressionLine(reason: string, count: number): string {
  const label =
    count === 1 && Object.hasOwn(SUPPRESSION_SINGULAR_LABELS, reason)
      ? (SUPPRESSION_SINGULAR_LABELS[reason] ?? suppressionLabel(reason))
      : suppressionLabel(reason);
  return `${formatCount(count)} ${label}`;
}

/**
 * Findings that question the section's items rather than describe them: missing tracking, an
 * incomplete dataset, an unavailable pipeline, no segment window and no K1 baseline.
 */
const WARNING_FINDINGS: ReadonlySet<FindingCode> = new Set<FindingCode>([
  'conversion_tracking_absent',
  'incomplete_dataset',
  'pipeline_unavailable',
  'segment_window_unavailable',
  'k1_no_baseline',
]);

/**
 * The banner tone of a section finding: `'warning'` for `conversion_tracking_absent`,
 * `incomplete_dataset`, `pipeline_unavailable`, `segment_window_unavailable` and
 * `k1_no_baseline`, `'info'` for every other code. The message names the condition itself, so
 * the tone is never its only cue.
 *
 * @param code - the finding code
 * @returns the tone
 */
export function findingTone(code: FindingCode): Tone {
  return WARNING_FINDINGS.has(code) ? 'warning' : 'info';
}

// ---------------------------------------------------------------------------------------------
// The frame
// ---------------------------------------------------------------------------------------------

/** Props of {@link SectionFrame}. */
export interface SectionFrameProps {
  /** The section, as the API returned it. */
  section: Section;
  /**
   * Renders one item, visible, historical or held, as its card. It is called for every item the
   * frame shows and receives the item's group, so a card can adapt to it.
   */
  renderItem: (item: Item, group: Group) => ReactNode;
  /**
   * Replaces one group's body inside its group region, for example a table of white-paper rows.
   * `parts.body` holds the default body for an override that wraps rather than replaces it.
   * Called only for groups with at least one visible, historical or held item, or a
   * confirmation of a feedback recorded on this page (`parts.placed.confirmed`), which an
   * override shows where the hidden item stood (`parts.placed`, `parts.confirmations`).
   */
  renderGroup?: (group: Group, parts: GroupParts) => ReactNode;
  /**
   * Replaces the whole groups area, for example one tab set across the Keywords groups. It is
   * called once, with the parts of every API group, empty ones included, and only when the
   * section has at least one visible, historical or held item, or a confirmation. The frame
   * then renders no group regions of its own, so each part's `anchorId` is the override's to
   * place.
   */
  renderGroups?: (parts: readonly GroupParts[]) => ReactNode;
  /**
   * Content for a group with no visible, historical or held item and no confirmation. Omitted,
   * or returning nothing (`null`, `undefined`, a boolean or an empty string), such a group is
   * left out, which is the default. Returning content keeps the group region, with its anchor,
   * and shows that content in it. Not used with `renderGroups`, which receives every group
   * itself.
   */
  renderEmptyGroup?: (group: Group, parts: GroupParts) => ReactNode;
  /**
   * The title of the withheld list. Omitted, `WithheldList`'s default applies; the Budget
   * section passes "Patterns withheld for sample size" (AAP 0.5.5).
   */
  withheldTitle?: string;
  /** Content placed after the findings and before the groups, such as a section-level note. */
  beforeGroups?: ReactNode;
}

/**
 * The empty-state reason of a genuinely empty section: no card hidden by feedback since the run,
 * no suppression count, nothing withheld and no finding with a message. Exported so a sub-list
 * of an unknown rule (Keywords) says the same.
 */
export const NO_RULE_FIRED_REASON = 'No rule fired for this window';

/**
 * The count text for items feedback given since the run hides now, after their count:
 * "{n} hidden by feedback since this run (dismissed, snoozed or done)".
 */
const EMPTY_HIDDEN_REASON = 'hidden by feedback since this run (dismissed, snoozed or done)';

/** The lead of the empty-state reason of cards that exist but are not shown. */
const NOT_SHOWN_LEAD = 'No actions to show';

/**
 * Findings that describe what a section shows rather than explain why it shows nothing: search-
 * term coverage, the missing Google CRM link and skipped stages. An empty section names one of
 * these as its reason only when no other finding has a message.
 */
const INFORMATIONAL_FINDINGS: ReadonlySet<FindingCode> = new Set<FindingCode>([
  'search_term_coverage',
  'no_crm_link',
  'skipped_stage',
]);

/**
 * The empty-state reason of cards that exist but are not shown: "No actions to show: {parts}",
 * the parts joined by "; " in this order:
 *
 * 1. "{n} hidden by feedback since this run (dismissed, snoozed or done)", when `hiddenCount` is
 *    positive;
 * 2. one "{count} {label}" part per suppression entry, in the order given, as the "Not shown"
 *    list writes it ("2 dismissed", "1 spend increase blocked by the high-utilization guard").
 *
 * Every count is the API's (the run's `suppressed` counts) or the live overlay's (cards with
 * `current.hidden_by`); none is written into the client.
 *
 * @param hiddenCount - how many cards feedback given since the run hides now
 * @param suppressions - the run's suppression counts as `[reason, count]` entries, each finite
 *   and positive, in API order; none by default
 * @returns the reason, or `null` when nothing is hidden or suppressed
 *
 * @example
 * ```ts
 * notShownReason(1); // "No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)"
 * notShownReason(0, [['feedback_dismiss', 2]]); // "No actions to show: 2 dismissed"
 * notShownReason(0); // null
 * ```
 */
export function notShownReason(
  hiddenCount: number,
  suppressions: readonly (readonly [string, number])[] = [],
): string | null {
  const parts = suppressions.map(([reason, count]) => suppressionLine(reason, count));
  if (hiddenCount > 0) {
    parts.unshift(`${formatCount(hiddenCount)} ${EMPTY_HIDDEN_REASON}`);
  }
  return parts.length === 0 ? null : `${NOT_SHOWN_LEAD}: ${parts.join('; ')}`;
}

/** The lead of the empty-state reason of a section whose datasets do not cover the window. */
const INCOMPLETE_DATA_LEAD = 'Data does not fully cover this window';

/**
 * The reason text of an `incomplete_dataset` finding's `evidence.reasons`: its non-blank string
 * entries, trimmed, joined by "; ". `null` when `reasons` is not an array or leaves no entry.
 */
function datasetReasonsText(reasons: unknown): string | null {
  if (!Array.isArray(reasons)) {
    return null;
  }
  const texts = reasons.map((entry: unknown) => textOr(entry, '')).filter((text) => text !== '');
  return texts.length === 0 ? null : texts.join('; ');
}

/**
 * The empty-state reason of a section whose datasets do not cover the window, summarised from
 * its `incomplete_dataset` findings (AAP 0.5.4.2 P4) in one sentence that names every such
 * dataset once per cause, rather than repeating one finding's banner:
 * "Data does not fully cover this window: {part}; {part}".
 *
 * The findings with code `incomplete_dataset` are read in API order. Each names its dataset in
 * `evidence.dataset`, a non-blank string; a finding without one is skipped (its message still
 * reaches the empty state through `findingReason` when nothing else explains the absence). Its
 * cause is `evidence.reasons`: the non-blank string entries, joined by "; ". Entries of any
 * other type are ignored, and a finding with none left, or whose `reasons` is not an array, has
 * no cause. Datasets that share a cause form one part, "{datasets} ({cause})", or just
 * "{datasets}" when they have none: parts in the order their cause first appears, datasets in
 * API order and joined by ", ", each named once in its part. `evidence` is read defensively,
 * because the API types it only as an object.
 *
 * Every name and cause is the API's own text; nothing is written into the client. The function
 * is pure: it reads only `findings`.
 *
 * @param findings - the section's findings, as the API returned them
 * @returns the reason, or `null` when no `incomplete_dataset` finding names a dataset
 *
 * @example
 * ```ts
 * const gap = (dataset: unknown, reasons: unknown): Finding => ({
 *   code: 'incomplete_dataset',
 *   section: 'keywords',
 *   entity: null,
 *   message: `${String(dataset)} is not complete over the window`,
 *   evidence: { dataset, reasons },
 * });
 * incompleteDataReason([
 *   gap('google.entities', ['no active Google dataset']),
 *   gap('google.metrics_daily', ['no active Google dataset']),
 * ]);
 * // "Data does not fully cover this window: google.entities, google.metrics_daily (no active Google dataset)"
 * incompleteDataReason([
 *   gap('hubspot.contacts', ['covers from 2026-04-12']),
 *   gap('linkedin.leads', [7, ' ']),
 *   gap('hubspot.deal_search', ['covers from 2026-04-12']),
 * ]);
 * // "Data does not fully cover this window: hubspot.contacts, hubspot.deal_search (covers from 2026-04-12); linkedin.leads"
 * incompleteDataReason([gap(42, ['no active Google dataset'])]); // null
 * ```
 */
export function incompleteDataReason(findings: readonly Finding[]): string | null {
  const byCause = new Map<string | null, string[]>();
  for (const finding of findings) {
    if (finding.code !== 'incomplete_dataset') {
      continue;
    }
    const evidence: unknown = finding.evidence;
    if (typeof evidence !== 'object' || evidence === null || Array.isArray(evidence)) {
      continue;
    }
    const fields = evidence as Readonly<Record<string, unknown>>;
    const dataset = textOr(fields.dataset, '');
    if (dataset === '') {
      continue;
    }
    const cause = datasetReasonsText(fields.reasons);
    const datasets = byCause.get(cause);
    if (datasets === undefined) {
      byCause.set(cause, [dataset]);
    } else if (!datasets.includes(dataset)) {
      datasets.push(dataset);
    }
  }
  if (byCause.size === 0) {
    return null;
  }
  const parts = [...byCause].map(([cause, datasets]) =>
    cause === null ? datasets.join(', ') : `${datasets.join(', ')} (${cause})`,
  );
  return `${INCOMPLETE_DATA_LEAD}: ${parts.join('; ')}`;
}

/**
 * The message of the first finding, in API order, that explains an absence (any code outside
 * {@link INFORMATIONAL_FINDINGS}, such as `incomplete_dataset` or `k1_no_baseline`), else of the
 * first finding of any code; blank messages are skipped. `null` when no finding has a message.
 */
function findingReason(findings: readonly Finding[]): string | null {
  let informational: string | null = null;
  for (const finding of findings) {
    const message = textOr(finding.message, '');
    if (message === '') {
      continue;
    }
    if (!INFORMATIONAL_FINDINGS.has(finding.code)) {
      return message;
    }
    informational ??= message;
  }
  return informational;
}

/** Returns `text` trimmed, or `fallback` when it is not a string or holds only whitespace. */
function textOr(text: unknown, fallback: string): string {
  if (typeof text !== 'string') {
    return fallback;
  }
  const trimmed = text.trim();
  return trimmed === '' ? fallback : trimmed;
}

/**
 * Whether a group has something to show: at least one visible, historical or held item, or the
 * confirmation of an item a feedback recorded on this page has hidden, which a group whose
 * every item was hidden here shows instead of an empty state while it lasts.
 */
function hasShownItems(parts: GroupParts): boolean {
  return (
    parts.visible.length > 0 ||
    parts.historical.length > 0 ||
    parts.held.length > 0 ||
    parts.placed.confirmed > 0
  );
}

/** Props of {@link ItemCards}. */
interface ItemCardsProps {
  /** The items to render, in API order, confirmed hidden items included. */
  items: readonly Item[];
  /** The group the items belong to, passed on to `renderItem`. */
  group: Group;
  /** The section's `renderItem`. */
  renderItem: SectionFrameProps['renderItem'];
  /** The feedback confirmations recorded on this page, passed on to the {@link CardList}. */
  confirmations: FeedbackConfirmations;
}

/**
 * `renderItem` for each item, in order, through a {@link CardList}: each card in a `Fragment`
 * keyed by the item's recommendation identity across runs ({@link itemKeys}), so `renderItem`
 * may return any node without a key of its own, a long list mounts one batch of cards at a time,
 * and a newer run's item of the same recommendation keeps its card, so keyboard focus and the
 * card's local state survive the swap.
 *
 * A component rather than an inline map, so `renderItem` runs only when the list renders: the
 * held cards of a {@link HeldDisclosure} nobody has opened are never even built, and a card
 * beyond the shown batches is built only once it is revealed.
 */
function ItemCards({ items, group, renderItem, confirmations }: ItemCardsProps): ReactElement {
  return (
    <CardList
      items={items}
      renderCard={(item) => renderItem(item, group)}
      confirmations={confirmations}
    />
  );
}

/**
 * The recommendation identities ({@link itemIdentity}) of every item of the section feedback
 * hides now, in API order: the only items that can show a confirmation.
 */
function hiddenItemIdentities(section: Section): string[] {
  return section.groups.flatMap((group) => group.items.filter(isHidden).map(itemIdentity));
}

/**
 * Prepares one group: partitions its items, places the confirmations of the items a feedback
 * recorded on this page has hidden, derives its anchor and builds the default body.
 */
function buildGroupParts(
  sectionKey: SectionKey,
  group: Group,
  renderItem: SectionFrameProps['renderItem'],
  confirmations: FeedbackConfirmations,
): GroupParts {
  const { visible, historical, held, hiddenCount } = partitionGroup(group);
  // Without a confirmation on the page, the placed lists are the partition's own.
  const placed =
    confirmations.size === 0
      ? { visible, historical, held, confirmed: 0 }
      : placeConfirmations(group, confirmations);
  const anchorId = groupAnchorId(sectionKey, group.key);
  const cards = (items: readonly Item[]) => (
    <ItemCards
      items={items}
      group={group}
      renderItem={renderItem}
      confirmations={confirmations}
    />
  );
  // A block emptied by the feedback renders nothing, so its confirmations take its place.
  const body = (
    <Stack>
      {cards(placed.visible)}
      <HistoricalItems id={`${anchorId}-historical`} count={historical.length}>
        {cards(placed.historical)}
      </HistoricalItems>
      {historical.length === 0 && placed.historical.length > 0 ? cards(placed.historical) : null}
      <HeldDisclosure
        id={`${anchorId}-held`}
        count={held.length}
        anchors={held.map((item) => itemAnchorId(item.id))}
      >
        {cards(placed.held)}
      </HeldDisclosure>
      {held.length === 0 && placed.held.length > 0 ? cards(placed.held) : null}
    </Stack>
  );
  return { group, anchorId, visible, historical, held, hiddenCount, confirmations, placed, body };
}

/** One group region the frame's groups area renders: the group and the content it holds. */
interface GroupEntry {
  /** The group, as the frame prepared it. */
  readonly part: GroupParts;
  /** `renderGroup`'s output, the default body, or `renderEmptyGroup`'s content. */
  readonly content: ReactNode;
  /** Whether the group's title repeats its section's ({@link groupRepeatsSectionTitle}). */
  readonly repeatsSectionTitle: boolean;
}

/** The React key of a group region: the group key, unique within a section. */
function groupEntryKey(entry: GroupEntry): string {
  return entry.part.group.key;
}

/**
 * The anchors a group region holds: its own anchor and the `item-{id}` of each of its items, so
 * a link to a card in a group not shown yet reveals the group, whose card list then reveals the
 * card.
 */
function groupEntryAnchors(entry: GroupEntry): readonly string[] {
  return [entry.part.anchorId, ...entry.part.group.items.map((item) => itemAnchorId(item.id))];
}

/** The group region itself, which takes focus when it leads a revealed batch of groups. */
function groupEntryFocusId(entry: GroupEntry): string {
  return entry.part.anchorId;
}

/**
 * Whether a group's title repeats its section's title, such as the Budget section's only group,
 * "Budget changes", inside the "Budget changes" section. Both titles are trimmed and compared
 * case-insensitively, each as its heading shows it: a blank or missing title as the key it
 * falls back to.
 *
 * Such a group renders no heading of its own: its cards sit directly under the section's `<h2>`,
 * so a section never shows the same heading twice. A section passing `headingLevel` to its
 * cards reads this to put them at level 3 in such a group and at level 4 below a group's
 * `<h3>`, so no heading level is skipped (WCAG 2.2 SC 1.3.1).
 *
 * @param section - the section the group belongs to
 * @param group - the group, as the API returned it
 * @returns `true` when the group's title equals the section's
 *
 * @example
 * ```ts
 * groupRepeatsSectionTitle(budgetSection, { key: 'changes', title: 'Budget changes', items });
 * // true
 * groupRepeatsSectionTitle(campaignsSection, { key: 'pause', title: 'Pause', items }); // false
 * ```
 */
export function groupRepeatsSectionTitle(section: Section, group: Group): boolean {
  const sectionTitle = textOr(section.title, section.key);
  const groupTitle = textOr(group.title, group.key);
  return groupTitle.toLowerCase() === sectionTitle.toLowerCase();
}

/**
 * A group region: a `PageShell.Section` (`<h3>`) anchored at the group's anchor id, or, for a
 * group whose title repeats its section's ({@link groupRepeatsSectionTitle}), a `Stack` with no
 * heading carrying that anchor id. The anchor stays the target of in-page links, of the
 * revealing hash and of the focus a revealed batch of groups moves to; the `Stack` is a plain
 * container, never an unnamed region.
 */
function renderGroupEntry({ part, content, repeatsSectionTitle }: GroupEntry): ReactNode {
  if (repeatsSectionTitle) {
    return <Stack id={part.anchorId}>{content}</Stack>;
  }
  return (
    <PageShell.Section
      id={part.anchorId}
      title={textOr(part.group.title, part.group.key)}
      headingLevel={3}
    >
      {content}
    </PageShell.Section>
  );
}

/**
 * The one-sentence reason of an empty section, the first that applies:
 *
 * 1. {@link notShownReason}, when feedback hides cards now or the run suppressed some;
 * 2. the leading withheld line's reason ({@link withheldReason}), for example "No search term
 *    met the K1 minimum of 70 clicks";
 * 3. the data-gap sentence of its `incomplete_dataset` findings ({@link incompleteDataReason}),
 *    for example "Data does not fully cover this window: google.entities, google.metrics_daily
 *    (no active Google dataset)";
 * 4. the first finding that explains the absence ({@link findingReason}), such as a
 *    `k1_no_baseline` finding, or an `incomplete_dataset` finding whose evidence names no
 *    dataset;
 * 5. {@link NO_RULE_FIRED_REASON}.
 *
 * The section's findings, withheld list and "Not shown" counts render in full around the empty
 * state, so the reason states one cause and never repeats those lists: the data-gap sentence
 * names each dataset once per cause, and is never the text of one finding's banner.
 *
 * @param section - the section, as the API returned it
 * @param hiddenTotal - how many of the section's items feedback hides now
 * @param suppressions - the section's {@link reportedSuppressions}
 * @returns the reason
 */
function emptyReason(
  section: Section,
  hiddenTotal: number,
  suppressions: readonly [string, number][],
): string {
  return (
    notShownReason(hiddenTotal, suppressions) ??
    withheldReason(section.withheld) ??
    incompleteDataReason(section.findings) ??
    findingReason(section.findings) ??
    NO_RULE_FIRED_REASON
  );
}

/**
 * The frame every `/actions` section renders inside (AAP 0.5.5). In order, within a
 * `PageShell.Section` anchored at `section-{key}` with the section title as its `<h2>`:
 *
 * 1. one `Banner` per finding, in API order, toned by {@link findingTone}, never an `alert`;
 * 2. `beforeGroups`, when given;
 * 3. the groups, in API order: each group with a visible, historical or held item becomes a
 *    `PageShell.Section` (`<h3>`) anchored at {@link groupAnchorId}, holding `renderGroup`'s
 *    output or the default body (visible cards, then the {@link HistoricalItems} block with the
 *    cards computed from replaced data, then the {@link HeldDisclosure} with the held ones). A
 *    group whose title repeats the section's ({@link groupRepeatsSectionTitle}) gets no `<h3>`:
 *    the same content sits in a `Stack` anchored at {@link groupAnchorId}, directly under the
 *    section's `<h2>`, so the title is never shown twice. A group with no visible, historical
 *    or held item and no confirmation is left out unless `renderEmptyGroup` supplies content
 *    for it.
 *    The groups mount `GROUP_BATCH_SIZE` at a time, followed by a "Show {n} more groups"
 *    control when there are more, and each card list inside them `CARD_BATCH_SIZE` cards at a
 *    time ({@link CardList}). With `renderGroups`, its output replaces this whole step. An item
 *    a feedback recorded on this page has hidden shows its {@link FeedbackConfirmation} where
 *    its card stood ({@link placeConfirmations}), and a group whose items were all hidden here
 *    keeps its region with the confirmations while the run still lists them;
 * 4. when no group has a visible, historical or held item or a confirmation, an `EmptyState`
 *    whose title is the one sentence that says why ({@link emptyReason}): "No actions to show:
 *    {n} hidden by feedback since this run (dismissed, snoozed or done); {count} {label}" when
 *    feedback hides items now or the run suppressed some, else the leading withheld line's
 *    reason, else "Data does not fully cover this window: {datasets} ({cause}); …" from the
 *    section's `incomplete_dataset` findings, else the first finding that explains the absence,
 *    else "No rule fired for this window". Its long identifiers wrap at their underscores
 *    (`withIdentifierBreaks`), as they do in a finding's banner. It has no reason body: the
 *    finding banners above and the lists below are not repeated in it;
 * 5. the `WithheldList` of the section's withheld patterns;
 * 6. a list named "Not shown": one "{count} {label}" line per suppression reason, in API order,
 *    and "{n} hidden since this run" for items feedback hides now, confirmed ones included.
 *    Nothing when both are empty.
 *
 * The frame holds no state of its own (each held disclosure keeps its open state, and each
 * batched list how many batches it shows) and fetches nothing. It reads the confirmations of
 * its own hidden items from the nearest `ActionOutcomeProvider` (`../ActionOutcomes`), and
 * none outside one.
 *
 * @param props - see {@link SectionFrameProps}
 * @returns the section region
 *
 * @example
 * ```tsx
 * export function BudgetSection({ section }: SectionComponentProps) {
 *   return (
 *     <SectionFrame
 *       section={section}
 *       withheldTitle="Patterns withheld for sample size"
 *       renderItem={(item, group) => (
 *         <RecommendationCard
 *           item={item}
 *           headingLevel={groupRepeatsSectionTitle(section, group) ? 3 : 4}
 *         />
 *       )}
 *     />
 *   );
 * }
 * ```
 */
export function SectionFrame({
  section,
  renderItem,
  renderGroup,
  renderGroups,
  renderEmptyGroup,
  withheldTitle,
  beforeGroups,
}: SectionFrameProps): ReactElement {
  // Only this section's hidden items can show a confirmation, so the frame reads theirs alone,
  // and a feedback recorded in another section does not render this one again.
  const hiddenIdentities = useMemo(() => hiddenItemIdentities(section), [section]);
  const confirmations = useFeedbackConfirmations(hiddenIdentities);
  const parts = section.groups.map((group) =>
    buildGroupParts(section.key, group, renderItem, confirmations),
  );
  const anyShown = parts.some(hasShownItems);
  const hiddenTotal = parts.reduce((sum, part) => sum + part.hiddenCount, 0);

  let groupsArea: ReactNode;
  if (renderGroups !== undefined) {
    groupsArea = anyShown ? renderGroups(parts) : null;
  } else {
    const shownGroups: GroupEntry[] = [];
    for (const part of parts) {
      const repeatsSectionTitle = groupRepeatsSectionTitle(section, part.group);
      if (hasShownItems(part)) {
        const content = renderGroup === undefined ? part.body : renderGroup(part.group, part);
        shownGroups.push({ part, content, repeatsSectionTitle });
      } else {
        // Empty groups are left out by default; renderEmptyGroup lets a section render one anyway.
        // The overlay can report a discrepancy detected after the run while the run's discrepancy
        // group is empty, and the section that holds the overlay (Campaigns) can then keep
        // DISCREPANCY_GROUP_ANCHOR as the target of the banner's and held cards' links instead of
        // leaving them pointing at nothing.
        const fallback = renderEmptyGroup?.(part.group, part);
        if (hasContent(fallback)) {
          shownGroups.push({ part, content: fallback, repeatsSectionTitle });
        }
      }
    }
    // The groups keep the API's order, so the first batch always holds the first group, such as
    // Campaigns' discrepancy group.
    groupsArea = (
      <BatchedList
        entries={shownGroups}
        batchSize={GROUP_BATCH_SIZE}
        noun={GROUP_NOUN}
        getKey={groupEntryKey}
        renderEntry={renderGroupEntry}
        anchorsOf={groupEntryAnchors}
        focusIdOf={groupEntryFocusId}
      />
    );
  }

  const suppressed = reportedSuppressions(section);

  return (
    <PageShell.Section id={sectionAnchorId(section.key)} title={textOr(section.title, section.key)}>
      <Stack>
        {section.findings.map((finding, index) => (
          <Banner
            key={`${finding.code}-${index}`}
            tone={findingTone(finding.code)}
            title={textOr(finding.message, finding.code)}
          />
        ))}
        {beforeGroups}
        {groupsArea}
        {anyShown ? null : (
          <EmptyState
            // The sentence can quote a finding message or a dataset name, whose long identifiers
            // wrap at their underscores, as they do in the finding's banner.
            title={withIdentifierBreaks(emptyReason(section, hiddenTotal, suppressed))}
          />
        )}
        {withheldTitle === undefined ? (
          <WithheldList withheld={section.withheld} />
        ) : (
          <WithheldList withheld={section.withheld} title={withheldTitle} />
        )}
        {suppressed.length > 0 || hiddenTotal > 0 ? (
          <Stack as="ul" gap={1} aria-label="Not shown">
            {suppressed.map(([reason, count]) => (
              <li key={reason}>{suppressionLine(reason, count)}</li>
            ))}
            {hiddenTotal > 0 ? (
              <li key="hidden-since-run">{`${formatCount(hiddenTotal)} hidden since this run`}</li>
            ) : null}
          </Stack>
        ) : null}
      </Stack>
    </PageShell.Section>
  );
}
