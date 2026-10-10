/**
 * `B1Cards`: where a C2 "Scale" card finds the B1 card it links to (AAP 0.5.4.4 C2: "'Scale',
 * with a link to its B1 item, which carries the amount").
 *
 * A C2 "Scale" item names its B1 item by the subject both decide on, the campaign or shared
 * budget (`evidence.extra.b1_link`, read by `b1Link` in `./evidenceExtra`), because the run
 * assigns item ids only when it persists the items. The B1 card sits in another section, Budget
 * changes, so the link is resolved at page level: {@link B1CardsProvider} wraps the five
 * sections on `/actions` and indexes the run's B1 cards by decision subject (`"{type}:{id}"`),
 * and {@link useLinkedB1Card} finds the one a C2 item's link names.
 *
 * Every B1 card the page renders is indexed, and only those, so a link never points at nothing:
 *
 * - a card feedback hides now (dismissed, snoozed or done) is counted, never rendered, so it is
 *   left out;
 * - a card held until discrepancies are resolved is indexed: it sits in its group's collapsed
 *   held disclosure, which expands when the link's hash names the card (`HeldDisclosure`), and
 *   the hold stays in place. A C2 "Scale" and its B1 share a subject, so a discrepancy on that
 *   subject normally holds both;
 * - a card computed from replaced data is shown openly under "Computed from replaced data", so
 *   it is indexed too.
 *
 * Outside the provider, such as a card rendered alone, no B1 card is known and no link is
 * offered. A card list reveals the batch holding the B1 card when the link's hash names it
 * (`CardList`), so a long Budget section still scrolls to it.
 *
 * Nothing here fetches (AAP 0.11.1): the index is built from the same `GET /recommendations`
 * response the sections render. The provider renders no element of its own, so the sections stay
 * direct children of the page's `Stack`.
 *
 * @packageDocumentation
 */
import { createContext, useContext, useMemo } from 'react';
import type { ReactElement, ReactNode } from 'react';

import type { Item, Section } from '../../api/types';
import type { B1Link } from './evidenceExtra';
import { isHidden } from './sections/SectionFrame';

/** The budget-increase rule (AAP 0.5.4.5 B1), the only rule a C2 "Scale" item links to. */
const B1_RULE_ID = 'B1';

/** The run's B1 card for one subject: where it is and what it recommends. */
export interface B1Card {
  /** The B1 item's id; its card carries the anchor `item-{itemId}`. */
  readonly itemId: Item['id'];
  /**
   * The B1 item's action, its card's headline, for example "Raise daily budget from USD 200.00
   * to USD 240.00"; `null` when blank.
   */
  readonly action: string | null;
}

/**
 * The run's B1 cards the page renders (every one feedback does not hide, held and historical
 * included), keyed by decision subject (`"{type}:{id}"`).
 */
export type B1CardIndex = ReadonlyMap<string, B1Card>;

/** Trimmed text, or `null` for a missing or blank value. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** The key of a decision subject in a {@link B1CardIndex}: `"{type}:{id}"`. */
function subjectKey(type: string, id: string | number): string {
  return `${type}:${String(id)}`;
}

/** Every item of every group of `sections`, in API order. */
function itemsOf(sections: readonly Section[]): Item[] {
  return sections.flatMap((section) => section.groups.flatMap((group) => group.items));
}

/**
 * Indexes the run's B1 cards that the page renders by the decision subject each one raises:
 * every B1 item with a decision subject that feedback does not hide now, including one held
 * until discrepancies are resolved, which renders behind its group's held disclosure, and one
 * computed from replaced data (see the module overview). A B1 item without a decision subject
 * and one feedback hides are left out; the first card of a repeated subject is kept.
 *
 * @param sections - the page's sections, as `GET /recommendations` returned them
 * @returns the index
 *
 * @example
 * ```ts
 * indexB1Cards(page.sections).get('campaign:305');
 * // { itemId: 1201, action: 'Raise daily budget from USD 200.00 to USD 240.00' }
 * ```
 */
export function indexB1Cards(sections: readonly Section[]): B1CardIndex {
  const index = new Map<string, B1Card>();
  for (const item of itemsOf(sections)) {
    const subject = item.decision_subject;
    if (item.rule_id !== B1_RULE_ID || subject === null || isHidden(item)) {
      continue;
    }
    const key = subjectKey(subject.type, subject.id);
    if (!index.has(key)) {
      index.set(key, { itemId: item.id, action: textOrNull(item.action) });
    }
  }
  return index;
}

/** The provider's index; `null` outside a {@link B1CardsProvider}. */
const B1CardsContext = createContext<B1CardIndex | null>(null);

/** Props of {@link B1CardsProvider}. */
export interface B1CardsProviderProps {
  /** The page's sections, from the same `GET /recommendations` response the sections render. */
  sections: readonly Section[];
  /** The sections, whose C2 "Scale" cards look up their B1 card through {@link useLinkedB1Card}. */
  children?: ReactNode;
}

/**
 * Shares the run's B1 cards the page renders ({@link indexB1Cards}) with every C2 "Scale" card
 * below it. The index is rebuilt only when `sections` changes, which a refetched page that changed
 * nothing does not do, because TanStack Query hands unchanged data back as the same objects. It
 * renders no element of its own.
 *
 * @param props - see {@link B1CardsProviderProps}
 * @returns the children, inside the provider
 *
 * @example
 * ```tsx
 * <B1CardsProvider sections={page.sections}>
 *   <CampaignsSection section={campaigns} run={page.run} overlay={page.overlay} />
 *   <BudgetSection section={budget} run={page.run} overlay={page.overlay} />
 * </B1CardsProvider>
 * ```
 */
export function B1CardsProvider({ sections, children }: B1CardsProviderProps): ReactElement {
  const index = useMemo(() => indexB1Cards(sections), [sections]);
  return <B1CardsContext.Provider value={index}>{children}</B1CardsContext.Provider>;
}

/**
 * The B1 card a C2 "Scale" item's link names: the run's B1 card for the link's subject that the
 * page renders, openly or behind its group's held disclosure.
 *
 * @param link - the C2 item's link (`b1Link` in `./evidenceExtra`), or `null` when it carries
 *   none
 * @returns the card, or `null` without a link, for a link to any rule but B1, outside a
 *   {@link B1CardsProvider}, and when the page renders no B1 card for the subject (none in the
 *   run, or the one there hidden by feedback)
 *
 * @example
 * ```tsx
 * const target = useLinkedB1Card(b1Link(item));
 * return target === null ? null : <a href={`#item-${String(target.itemId)}`}>{target.action}</a>;
 * ```
 */
export function useLinkedB1Card(link: B1Link | null): B1Card | null {
  const index = useContext(B1CardsContext);
  if (index === null || link === null || link.ruleId !== B1_RULE_ID) {
    return null;
  }
  return index.get(subjectKey(link.subject.type, link.subject.id)) ?? null;
}
