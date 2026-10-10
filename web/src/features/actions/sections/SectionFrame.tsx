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
 *    `group-{sectionKey}-{groupKey}`. Visible cards come first, in API rank order; cards
 *    computed from replaced data follow, readable and never collapsed, under "Computed from
 *    replaced data" (AAP 0.3.2: a historical card stays readable with every action disabled,
 *    and no discrepancy resolution makes it current); cards held by a discrepancy come last,
 *    collapsed behind a "Held until discrepancies are resolved" disclosure (AAP 0.5.5
 *    "Placement of items that must be resolved first"); cards hidden by feedback are counted,
 *    never rendered (AAP 0.3.2: `current.held_now`, `current.historical` and
 *    `current.hidden_by` are read live);
 * 4. an `EmptyState` with its reason when no group has a card to show (AAP 0.5.5): the cards
 *    feedback has hidden since the run, the run's suppression counts, the first withheld lines
 *    and the finding messages, so a section emptied by feedback or suppression says so. "No rule
 *    fired for this window" is reserved for a section with none of these;
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
import { useMemo, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import type {
  FindingCode,
  Group,
  Item,
  Overlay,
  Run,
  Section,
  SectionKey,
} from '../../../api/types';
import { Banner, Button, EmptyState, Inline, PageShell, Stack } from '../../../design-system';
import type { Tone } from '../../../design-system';
import { UNAVAILABLE, formatCount } from '../../../lib/format';
import { WithheldList, withheldLine } from '../WithheldList';
import { discrepancyRef } from '../evidenceExtra';
import {
  ACTION_NOUN,
  BatchedList,
  CARD_BATCH_SIZE,
  GROUP_BATCH_SIZE,
  GROUP_NOUN,
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
   * The frame's default group body: a `Stack` of `renderItem` for each visible item, then the
   * {@link HistoricalItems} block holding `renderItem` for each historical item, then the
   * {@link HeldDisclosure} holding `renderItem` for each held item. Each of the three is a
   * {@link CardList}, so a long one mounts `CARD_BATCH_SIZE` cards at a time.
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
  /** The items, in API order; never re-sorted. */
  items: readonly Item[];
  /** Renders one item as its card, such as a `RecommendationCard`. */
  renderCard: (item: Item) => ReactNode;
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
 * Exported so a section that builds a group body of its own (Campaigns' discrepancy group)
 * bounds its lists the same way.
 *
 * @param props - see {@link CardListProps}
 * @returns the shown cards, then the control when the list is batched
 *
 * @example
 * ```tsx
 * <Stack>
 *   <CardList items={parts.visible} renderCard={(item) => <RecommendationCard item={item} />} />
 * </Stack>
 * ```
 */
export function CardList({ items, renderCard }: CardListProps): ReactElement {
  const keys = useMemo(() => itemKeys(items), [items]);
  return (
    <BatchedList
      entries={items}
      batchSize={CARD_BATCH_SIZE}
      noun={ACTION_NOUN}
      getKey={(item) => keys.get(item) ?? itemIdentity(item)}
      renderEntry={renderCard}
      anchorsOf={itemAnchors}
      focusIdOf={itemFocusId}
    />
  );
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
   * The held content, such as the held cards or a table of held rows. It mounts the first time
   * the region is expanded, never before.
   */
  children: ReactNode;
}

/**
 * The collapsed list of held items at the end of a group (AAP 0.5.5: "Held cards appear
 * collapsed at the end of their group under 'Held until discrepancies are resolved'").
 *
 * Renders a quiet `Button` labelled "Held until discrepancies are resolved ({count})" that
 * carries `aria-expanded` and `aria-controls={id}`, and a region with that `id` that is
 * `hidden` until the button expands it. The region is collapsed on first render. It is a real
 * button, so it is reachable with Tab and operated with Enter or Space (WCAG 2.2 SC 2.1.1,
 * 4.1.2). The button sits in an `Inline` row, so it keeps its own width instead of stretching
 * across the column.
 *
 * The region element is always rendered, so `aria-controls` always resolves, but the children
 * mount only the first time the region is expanded: a disclosure nobody opens formats and mounts
 * none of its cards. Once mounted they stay mounted, hidden, while the region is collapsed
 * again, so a held card keeps its local state across a collapse.
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
 * <HeldDisclosure id={`${parts.anchorId}-held`} count={parts.held.length}>
 *   <CardList items={parts.held} renderCard={(item) => <RecommendationCard item={item} />} />
 * </HeldDisclosure>
 * ```
 */
export function HeldDisclosure({ id, count, children }: HeldDisclosureProps): ReactElement | null {
  const [expanded, setExpanded] = useState(false);
  // Whether the region has ever been expanded: its children mount then and stay mounted.
  const [opened, setOpened] = useState(false);

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
 *     { key: 'spend', label: 'Spend', value: '$412.30' },
 *   ]}
 * />
 * // <ul aria-label="Keyword details"><li>Term: crm software</li><li>Spend: $412.30</li></ul>
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
 * count is finite and positive, in API order. The "Not shown" list and the empty-state reason
 * both read this one result, so they never disagree.
 */
function reportedSuppressions(section: Section): [string, number][] {
  return Object.entries(section.suppressed).filter(
    ([, count]) => Number.isFinite(count) && count > 0,
  );
}

/**
 * One suppression count as a line, "{count} {label}", for example "2 dismissed". A count of
 * exactly 1 takes the reason's singular label when {@link SUPPRESSION_SINGULAR_LABELS} has one
 * ("1 spend increase blocked by the high-utilization guard"); every other count, and every
 * reason without a singular form, takes {@link suppressionLabel}. Only the singular map's own
 * keys match, so a key such as `constructor` is not read from the object prototype.
 */
function suppressionLine(reason: string, count: number): string {
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
   * Called only for groups with at least one visible, historical or held item.
   */
  renderGroup?: (group: Group, parts: GroupParts) => ReactNode;
  /**
   * Replaces the whole groups area, for example one tab set across the Keywords groups. It is
   * called once, with the parts of every API group, empty ones included, and only when the
   * section has at least one visible, historical or held item. The frame then renders no group
   * regions of its own, so each part's `anchorId` is the override's to place.
   */
  renderGroups?: (parts: readonly GroupParts[]) => ReactNode;
  /**
   * Content for a group with no visible, historical or held item. Omitted, or returning nothing
   * (`null`, `undefined`, a boolean or an empty string), such a group is left out, which is the
   * default. Returning content keeps the group region, with its anchor, and shows that content
   * in it. Not used with `renderGroups`, which receives every group itself.
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

/** How many withheld lines an empty section quotes as its reason; the full list follows. */
const EMPTY_REASON_WITHHELD_LINES = 3;

/** The empty-state title of a section with no card to show. */
const EMPTY_TITLE = 'No actions in this section';

/**
 * The empty-state reason of a genuinely empty section: no card hidden by feedback since the run,
 * no suppression count, nothing withheld and no finding.
 */
const EMPTY_NO_REASON = 'No rule fired for this window';

/**
 * The empty-state reason for items feedback given since the run hides now, after their count:
 * "{n} hidden by feedback since this run (dismissed, snoozed or done)".
 */
const EMPTY_HIDDEN_REASON = 'hidden by feedback since this run (dismissed, snoozed or done)';

/** Returns `text` trimmed, or `fallback` when it is not a string or holds only whitespace. */
function textOr(text: unknown, fallback: string): string {
  if (typeof text !== 'string') {
    return fallback;
  }
  const trimmed = text.trim();
  return trimmed === '' ? fallback : trimmed;
}

/** Whether a group has something to show: at least one visible, historical or held item. */
function hasShownItems(parts: GroupParts): boolean {
  return parts.visible.length > 0 || parts.historical.length > 0 || parts.held.length > 0;
}

/** Props of {@link ItemCards}. */
interface ItemCardsProps {
  /** The items to render, in API order. */
  items: readonly Item[];
  /** The group the items belong to, passed on to `renderItem`. */
  group: Group;
  /** The section's `renderItem`. */
  renderItem: SectionFrameProps['renderItem'];
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
function ItemCards({ items, group, renderItem }: ItemCardsProps): ReactElement {
  return <CardList items={items} renderCard={(item) => renderItem(item, group)} />;
}

/** Prepares one group: partitions its items, derives its anchor and builds the default body. */
function buildGroupParts(
  sectionKey: SectionKey,
  group: Group,
  renderItem: SectionFrameProps['renderItem'],
): GroupParts {
  const { visible, historical, held, hiddenCount } = partitionGroup(group);
  const anchorId = groupAnchorId(sectionKey, group.key);
  const body = (
    <Stack>
      <ItemCards items={visible} group={group} renderItem={renderItem} />
      <HistoricalItems id={`${anchorId}-historical`} count={historical.length}>
        <ItemCards items={historical} group={group} renderItem={renderItem} />
      </HistoricalItems>
      <HeldDisclosure id={`${anchorId}-held`} count={held.length}>
        <ItemCards items={held} group={group} renderItem={renderItem} />
      </HeldDisclosure>
    </Stack>
  );
  return { group, anchorId, visible, historical, held, hiddenCount, body };
}

/** One group region the frame's groups area renders: the group and the content it holds. */
interface GroupEntry {
  /** The group, as the frame prepared it. */
  readonly part: GroupParts;
  /** `renderGroup`'s output, the default body, or `renderEmptyGroup`'s content. */
  readonly content: ReactNode;
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

/** A group region: a `PageShell.Section` (`<h3>`) anchored at the group's anchor id. */
function renderGroupEntry({ part, content }: GroupEntry): ReactNode {
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
 * The reason lines of an empty section, in this order:
 *
 * 1. "{n} hidden by feedback since this run (dismissed, snoozed or done)", when `hiddenTotal`
 *    is positive;
 * 2. one "{count} {label}" line per entry of `suppressions`, in API order;
 * 3. the first withheld lines, as the API wrote them;
 * 4. the finding messages, as the API wrote them.
 *
 * Empty only when the section has none of these, which is when no rule fired for the window.
 *
 * @param section - the section, as the API returned it
 * @param hiddenTotal - how many of the section's items feedback hides now
 * @param suppressions - the section's {@link reportedSuppressions}
 * @returns the lines, possibly none
 */
function emptyReasonLines(
  section: Section,
  hiddenTotal: number,
  suppressions: readonly [string, number][],
): string[] {
  const hidden = hiddenTotal > 0 ? [`${formatCount(hiddenTotal)} ${EMPTY_HIDDEN_REASON}`] : [];
  const suppressed = suppressions.map(([reason, count]) => suppressionLine(reason, count));
  const withheld = section.withheld.slice(0, EMPTY_REASON_WITHHELD_LINES).map(withheldLine);
  const findings = section.findings
    .map((finding) => textOr(finding.message, ''))
    .filter((message) => message !== '');
  return [...hidden, ...suppressed, ...withheld, ...findings];
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
 *    group with none of these is left out unless `renderEmptyGroup` supplies content for it.
 *    The groups mount `GROUP_BATCH_SIZE` at a time, followed by a "Show {n} more groups"
 *    control when there are more, and each card list inside them `CARD_BATCH_SIZE` cards at a
 *    time ({@link CardList}). With `renderGroups`, its output replaces this whole step;
 * 4. when no group has a visible, historical or held item, an `EmptyState` "No actions in this
 *    section" whose reason states why: "{n} hidden by feedback since this run (dismissed,
 *    snoozed or done)" when feedback hides items now, one "{count} {label}" line per
 *    suppression reason in API order, the first three withheld lines and the finding messages.
 *    "No rule fired for this window" appears only when there are none of these;
 * 5. the `WithheldList` of the section's withheld patterns;
 * 6. a list named "Not shown": one "{count} {label}" line per suppression reason, in API order,
 *    and "{n} hidden since this run" for items feedback hides now. Nothing when both are empty.
 *
 * The frame holds no state of its own (each held disclosure keeps its open state, and each
 * batched list how many batches it shows) and fetches nothing.
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
 *       renderItem={(item) => <RecommendationCard item={item} headingLevel={4} />}
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
  const parts = section.groups.map((group) => buildGroupParts(section.key, group, renderItem));
  const anyShown = parts.some(hasShownItems);
  const hiddenTotal = parts.reduce((sum, part) => sum + part.hiddenCount, 0);

  let groupsArea: ReactNode;
  if (renderGroups !== undefined) {
    groupsArea = anyShown ? renderGroups(parts) : null;
  } else {
    const shownGroups: GroupEntry[] = [];
    for (const part of parts) {
      if (hasShownItems(part)) {
        const content = renderGroup === undefined ? part.body : renderGroup(part.group, part);
        shownGroups.push({ part, content });
      } else {
        // Empty groups are left out by default; renderEmptyGroup lets a section render one anyway.
        // The overlay can report a discrepancy detected after the run while the run's discrepancy
        // group is empty, and the section that holds the overlay (Campaigns) can then keep
        // DISCREPANCY_GROUP_ANCHOR as the target of the banner's and held cards' links instead of
        // leaving them pointing at nothing.
        const fallback = renderEmptyGroup?.(part.group, part);
        if (hasContent(fallback)) {
          shownGroups.push({ part, content: fallback });
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
  const reasonLines = anyShown ? [] : emptyReasonLines(section, hiddenTotal, suppressed);

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
          <EmptyState title={EMPTY_TITLE}>
            {reasonLines.length === 0 ? (
              EMPTY_NO_REASON
            ) : (
              <Stack gap={1}>
                {reasonLines.map((line, index) => (
                  <span key={`${index}-${line}`}>{line}</span>
                ))}
              </Stack>
            )}
          </EmptyState>
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
