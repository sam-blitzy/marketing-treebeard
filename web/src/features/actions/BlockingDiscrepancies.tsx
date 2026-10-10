/**
 * `BlockingDiscrepancies`: the links from a held card, or a held white-paper row, to the
 * discrepancies that hold it (AAP 0.5.5 "Held cards appear collapsed at the end of their group
 * under 'Held until discrepancies are resolved', with the blocking discrepancy linked";
 * AAP 0.5.4.2 P1).
 *
 * Which discrepancies hold an item, in this order and each once:
 *
 * 1. the ones that held it when the run computed it (`evidence.extra.blocking_discrepancies`,
 *    ascending ids). The overlay never lifts a frozen hold;
 * 2. the unresolved discrepancies detected since, whose subject (`"{type}:{id}"`) is one the
 *    item depends on (`evidence.extra.depends_on`) or its own `decision_subject`. These are
 *    exactly the subjects through which the overlay holds an item at read time
 *    (`analysis/overlay.py`, `_dependent_subjects`), oldest `detected_at` first, as
 *    `GET /discrepancies` lists them.
 *
 * Each one links to where it can be resolved:
 *
 * - to the run's own C0 card for it (`#item-{id}`, an in-page link), when the run holds one;
 * - otherwise to its card on `/decisions` (`/decisions#discrepancy-{id}`, the id
 *   `DiscrepancyResolver` gives each card), through the router's `Link`, a client-side
 *   navigation. The resolver lists a page at a time, so it reads the hash itself: it loads pages
 *   until that card is shown and scrolls it into view, or says the discrepancy is no longer
 *   unresolved.
 *
 * Each link names its blocker: "View blocking discrepancy: {subject} ({kind})", for example
 * "View blocking discrepancy: Brand Search — US (Status mismatch)", so several links on one
 * card can be told apart (WCAG 2.2 SC 2.4.4 Link Purpose). When no blocker can be named (none
 * is recorded, and the live list names none, is loading or failed), the one link is
 * "View open discrepancies" to the Campaigns discrepancy group (`#group-campaigns-discrepancies`),
 * which says that it opens the list rather than a specific blocker.
 *
 * {@link BlockingDiscrepanciesProvider} wraps the five sections on `/actions`. It indexes the
 * run's C0 cards by discrepancy id and reads `GET /discrepancies` only when a shown,
 * non-historical, non-C0 item is held without a recorded blocker the run's own C0 cards can
 * show: a hold the overlay added after the run, or a recorded blocker that has no card in the
 * run. A page without such a hold sends no request of its own, though it still uses a list the
 * C0 cards have already read under the same query key. Outside the provider (a card rendered
 * alone), {@link BlockingDiscrepancyLinks} links each recorded blocker to `/decisions` and falls
 * back to the group link without one.
 *
 * Server state goes only through `../../api/queries` (AAP 0.11.1). UI comes only from the
 * design-system index (AAP 0.4.4), plus `<a>` and the router's `Link`: no raw buttons, headings
 * or tables, no inline styles and no stylesheet.
 *
 * @packageDocumentation
 */
import { Link } from '@tanstack/react-router';
import { createContext, useContext, useMemo } from 'react';
import type { ReactElement, ReactNode } from 'react';

import { DEFAULT_DISCREPANCY_STATES, useDiscrepancies } from '../../api/queries';
import type { Discrepancy, DiscrepancyKind, Item, Section } from '../../api/types';
import { Inline } from '../../design-system';
import { KIND_LABELS, SUBJECT_TYPE_LABELS } from '../decisions/DiscrepancyResolver';
import { blockingDiscrepancies, dependsOn, discrepancyRef } from './evidenceExtra';
import { DISCREPANCY_GROUP_ANCHOR, isHeld, isHidden } from './sections/SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/**
 * The text every link to a specific blocking discrepancy starts with (AAP 0.5.5), followed by
 * the blocker's subject and kind: "View blocking discrepancy: {subject} ({kind})".
 */
export const BLOCKING_DISCREPANCY_LINK_LABEL = 'View blocking discrepancy';

/**
 * The text of the link a held item shows when no specific blocker can be named: it opens the
 * Campaigns discrepancy group, the list of the run's discrepancies.
 */
export const OPEN_DISCREPANCIES_LINK_LABEL = 'View open discrepancies';

/** The accessible name of the list a held item shows when several discrepancies hold it. */
export const BLOCKING_DISCREPANCIES_LIST_LABEL = 'Blocking discrepancies';

/** The rule id of a discrepancy item, which is exempt from holds (AAP 0.5.4.2 P1). */
const C0_RULE_ID = 'C0';

/** The route of the discrepancy resolver. */
const DECISIONS_PATH = '/decisions';

// ---------------------------------------------------------------------------------------------
// The run's C0 cards
// ---------------------------------------------------------------------------------------------

/** The run's C0 card for one discrepancy: where it is and what it is about. */
export interface C0Card {
  /** The C0 item's id; its card carries the anchor `item-{itemId}`. */
  readonly itemId: Item['id'];
  /** The discrepant campaign's or shared budget's name, as the C0 item's entity names it. */
  readonly subjectName: string | null;
  /** The discrepancy kind the C0 item recorded (`status`, `budget`, `untracked`, `missing`). */
  readonly kind: string;
}

/** The run's C0 cards, keyed by discrepancy id as a string. */
export type C0CardIndex = ReadonlyMap<string, C0Card>;

/** An index with no card, used outside the provider. */
const NO_C0_CARDS: C0CardIndex = new Map();

/** Trimmed text, or `null` for a missing or blank value. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** Every item of every group of `sections`, in API order. */
function itemsOf(sections: readonly Section[]): Item[] {
  return sections.flatMap((section) => section.groups.flatMap((group) => group.items));
}

/**
 * Indexes the run's C0 cards by the id of the discrepancy each one shows
 * (`evidence.extra.discrepancy_id`). A C0 item without a readable id, or one feedback hides,
 * which renders no card, is left out; the first card of a repeated id is kept.
 *
 * @param sections - the page's sections, as `GET /recommendations` returned them
 * @returns the index
 *
 * @example
 * ```ts
 * indexC0Cards(page.sections).get('51');
 * // { itemId: 1201, subjectName: 'Brand Search — US', kind: 'status' }
 * ```
 */
export function indexC0Cards(sections: readonly Section[]): C0CardIndex {
  const index = new Map<string, C0Card>();
  for (const item of itemsOf(sections)) {
    if (item.rule_id !== C0_RULE_ID || isHidden(item)) {
      continue;
    }
    const ref = discrepancyRef(item);
    if (ref === null || index.has(ref.discrepancyId)) {
      continue;
    }
    index.set(ref.discrepancyId, {
      itemId: item.id,
      subjectName: textOrNull(item.entity.name),
      kind: ref.kind,
    });
  }
  return index;
}

/**
 * Whether a held card needs the live discrepancy list to name its blocker: some shown,
 * non-historical, non-C0 item is held now, and either recorded no blocker (a hold the overlay
 * added for a discrepancy detected after the run) or recorded one the run has no C0 card for.
 * A historical item is held by its replaced data, not by a discrepancy, and C0 is never held.
 *
 * @param sections - the page's sections
 * @param c0Cards - the run's C0 cards ({@link indexC0Cards})
 * @returns `true` when `GET /discrepancies` must be read
 */
export function needsLiveDiscrepancies(
  sections: readonly Section[],
  c0Cards: C0CardIndex,
): boolean {
  return itemsOf(sections).some((item) => {
    if (
      item.rule_id === C0_RULE_ID ||
      isHidden(item) ||
      item.current.historical ||
      !isHeld(item)
    ) {
      return false;
    }
    const recorded = blockingDiscrepancies(item);
    return recorded.length === 0 || recorded.some((id) => !c0Cards.has(id));
  });
}

// ---------------------------------------------------------------------------------------------
// Resolution
// ---------------------------------------------------------------------------------------------

/** One link to a blocking discrepancy. */
export interface BlockingLink {
  /** The discrepancy id, as a string. */
  readonly discrepancyId: string;
  /** The run's C0 card to link in-page, or `null` to link the discrepancy on `/decisions`. */
  readonly c0ItemId: Item['id'] | null;
  /** The link text, naming the blocker. */
  readonly label: string;
}

/** Whether `kind` is a discrepancy kind the label map names. */
function isDiscrepancyKind(kind: string): kind is DiscrepancyKind {
  return Object.hasOwn(KIND_LABELS, kind);
}

/** The label of a discrepancy kind as `/decisions` shows it; `null` for a kind it does not name. */
function kindLabel(kind: string): string | null {
  return isDiscrepancyKind(kind) ? KIND_LABELS[kind] : null;
}

/**
 * The subject of a live discrepancy as `/decisions` titles it: its name, else "Campaign 12" or
 * "Shared budget 401"; `null` when it has neither a name nor a known subject type.
 */
function liveSubjectName(discrepancy: Discrepancy): string | null {
  const { subject } = discrepancy;
  const name = textOrNull(subject.name);
  if (name !== null) {
    return name;
  }
  return Object.hasOwn(SUBJECT_TYPE_LABELS, subject.type)
    ? `${SUBJECT_TYPE_LABELS[subject.type]} ${String(subject.id)}`
    : null;
}

/**
 * The link text of one blocker: "View blocking discrepancy: {subject} ({kind})", or
 * "View blocking discrepancy {id} ({kind})" when no subject name is known; the kind is left out
 * when it is unknown.
 */
function blockerLabel(id: string, subjectName: string | null, kindLabel: string | null): string {
  const kind = kindLabel === null ? '' : ` (${kindLabel})`;
  return subjectName === null
    ? `${BLOCKING_DISCREPANCY_LINK_LABEL} ${id}${kind}`
    : `${BLOCKING_DISCREPANCY_LINK_LABEL}: ${subjectName}${kind}`;
}

/** The ids as numbers ascend; ids that are not integers keep their place after them. */
function ascendingIds(ids: readonly string[]): string[] {
  return [...ids].sort((left, right) => {
    const a = Number(left);
    const b = Number(right);
    if (Number.isInteger(a) && Number.isInteger(b)) {
      return a - b;
    }
    return Number.isInteger(a) ? -1 : Number.isInteger(b) ? 1 : 0;
  });
}

/**
 * The links to the discrepancies that hold `item`, in the order the module overview gives:
 * recorded blockers first (ascending), then the live unresolved discrepancies on a subject it
 * depends on or would write, each id once. Each links the run's C0 card when `c0Cards` holds one,
 * and `/decisions#discrepancy-{id}` otherwise. Its text names the subject and kind, from the C0
 * card or the live list, and a text two blockers would share gets ", discrepancy {id}" added.
 *
 * @param item - the held item
 * @param c0Cards - the run's C0 cards
 * @param live - the live unresolved discrepancies, or `undefined` while not read
 * @returns the links; `[]` when no blocker can be named
 */
export function blockingLinks(
  item: Item,
  c0Cards: C0CardIndex,
  live: readonly Discrepancy[] | undefined,
): BlockingLink[] {
  const liveById = new Map<string, Discrepancy>();
  for (const discrepancy of live ?? []) {
    liveById.set(String(discrepancy.id), discrepancy);
  }
  const subjects = new Set(dependsOn(item));
  if (item.decision_subject !== null) {
    subjects.add(`${item.decision_subject.type}:${String(item.decision_subject.id)}`);
  }
  const ids = new Set(ascendingIds(blockingDiscrepancies(item)));
  for (const discrepancy of live ?? []) {
    if (subjects.has(`${discrepancy.subject.type}:${String(discrepancy.subject.id)}`)) {
      ids.add(String(discrepancy.id));
    }
  }

  const links = [...ids].map((id): BlockingLink => {
    const card = c0Cards.get(id);
    const discrepancy = liveById.get(id);
    const liveName = discrepancy === undefined ? null : liveSubjectName(discrepancy);
    const liveKind = discrepancy === undefined ? null : kindLabel(discrepancy.kind);
    if (card !== undefined) {
      return {
        discrepancyId: id,
        c0ItemId: card.itemId,
        label: blockerLabel(id, card.subjectName ?? liveName, liveKind ?? kindLabel(card.kind)),
      };
    }
    return { discrepancyId: id, c0ItemId: null, label: blockerLabel(id, liveName, liveKind) };
  });

  const counts = new Map<string, number>();
  for (const link of links) {
    counts.set(link.label, (counts.get(link.label) ?? 0) + 1);
  }
  return links.map((link) =>
    (counts.get(link.label) ?? 0) > 1
      ? { ...link, label: `${link.label}, discrepancy ${link.discrepancyId}` }
      : link,
  );
}

// ---------------------------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------------------------

/** What {@link BlockingDiscrepanciesProvider} shares with each {@link BlockingDiscrepancyLinks}. */
interface BlockingDiscrepanciesState {
  /** The run's C0 cards. */
  readonly c0Cards: C0CardIndex;
  /** The live unresolved discrepancies, read or cached; `undefined` otherwise. */
  readonly live: readonly Discrepancy[] | undefined;
}

/** The provider's state; `null` outside a {@link BlockingDiscrepanciesProvider}. */
const BlockingDiscrepanciesContext = createContext<BlockingDiscrepanciesState | null>(null);

/** Props of {@link BlockingDiscrepanciesProvider}. */
export interface BlockingDiscrepanciesProviderProps {
  /** The page's sections, from the same `GET /recommendations` response the sections render. */
  sections: readonly Section[];
  /** The sections, whose held cards and rows render {@link BlockingDiscrepancyLinks}. */
  children?: ReactNode;
}

/**
 * Shares the run's C0 cards and, when a held item needs it ({@link needsLiveDiscrepancies}),
 * the live discrepancy list with every {@link BlockingDiscrepancyLinks} below it. It renders no
 * element of its own, so the sections stay direct children of the page's `Stack`.
 *
 * The list is the one `useDiscrepancies()` reads for the C0 card lookups in `CampaignsSection`,
 * under the same `queryKeys.discrepancies(states)` key, so the page reads it once. `/decisions`
 * does not share it: `DiscrepancyResolver` reads its own paged list (`useDiscrepancyPages`) and
 * itself loads and reveals the card a `/decisions#discrepancy-{id}` link names. While no held
 * item needs the list, the request is not sent, yet a list already cached is still used.
 *
 * @param props - see {@link BlockingDiscrepanciesProviderProps}
 * @returns the children, inside the provider
 *
 * @example
 * ```tsx
 * <BlockingDiscrepanciesProvider sections={page.sections}>
 *   <CampaignsSection section={campaigns} run={page.run} overlay={page.overlay} />
 * </BlockingDiscrepanciesProvider>
 * ```
 */
export function BlockingDiscrepanciesProvider({
  sections,
  children,
}: BlockingDiscrepanciesProviderProps): ReactElement {
  const c0Cards = useMemo(() => indexC0Cards(sections), [sections]);
  const enabled = useMemo(() => needsLiveDiscrepancies(sections, c0Cards), [sections, c0Cards]);
  const live = useDiscrepancies(DEFAULT_DISCREPANCY_STATES, { enabled }).data;
  const state = useMemo(() => ({ c0Cards, live }), [c0Cards, live]);
  return (
    <BlockingDiscrepanciesContext.Provider value={state}>
      {children}
    </BlockingDiscrepanciesContext.Provider>
  );
}

// ---------------------------------------------------------------------------------------------
// Links
// ---------------------------------------------------------------------------------------------

/** Props of {@link BlockingDiscrepancyLinks}. */
export interface BlockingDiscrepancyLinksProps {
  /** The held item, neither C0 nor historical, whose blockers are linked. */
  item: Item;
}

/** One blocker's link: in-page to the run's C0 card, or to its card on `/decisions`. */
function BlockerLink({ link }: { link: BlockingLink }): ReactElement {
  if (link.c0ItemId !== null) {
    return <a href={`#item-${String(link.c0ItemId)}`}>{link.label}</a>;
  }
  return (
    <Link to={DECISIONS_PATH} hash={`discrepancy-${link.discrepancyId}`}>
      {link.label}
    </Link>
  );
}

/**
 * The links from a held item to the discrepancies that hold it (see the module overview): one
 * link, a list named "Blocking discrepancies" when several hold it, or "View open
 * discrepancies" to the Campaigns discrepancy group when none can be named. The caller renders
 * it beside the hold reason, only for an item held by a discrepancy.
 *
 * @param props - see {@link BlockingDiscrepancyLinksProps}
 * @returns the link or links
 *
 * @example
 * ```tsx
 * <Inline gap={2}>
 *   <span>{heldReason}</span>
 *   <BlockingDiscrepancyLinks item={item} />
 * </Inline>
 * ```
 */
export function BlockingDiscrepancyLinks({ item }: BlockingDiscrepancyLinksProps): ReactElement {
  const state = useContext(BlockingDiscrepanciesContext);
  const links = blockingLinks(item, state?.c0Cards ?? NO_C0_CARDS, state?.live);
  const [only] = links;
  if (only === undefined) {
    return <a href={`#${DISCREPANCY_GROUP_ANCHOR}`}>{OPEN_DISCREPANCIES_LINK_LABEL}</a>;
  }
  if (links.length === 1) {
    return <BlockerLink link={only} />;
  }
  return (
    <Inline as="ul" gap={2} aria-label={BLOCKING_DISCREPANCIES_LIST_LABEL}>
      {links.map((link) => (
        <li key={link.discrepancyId}>
          <BlockerLink link={link} />
        </li>
      ))}
    </Inline>
  );
}
