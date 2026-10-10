/**
 * Card outcomes on `/actions`: how the outcome of a card action stays on the page after a
 * recorded feedback removes the card (WCAG 2.2 SC 4.1.3 Status Messages).
 *
 * A recorded feedback (dismiss, snooze, done) can unmount the card that recorded it: the
 * refetched recommendations carry `current.hidden_by`, and the section drops the item. The
 * {@link ActionOutcomeProvider}, placed above the sections, therefore keeps two things the card
 * cannot:
 *
 * - **the announcement**: a polite `role="status"` region, rendered before the sections from
 *   the first render, in which every card action confirms its outcome ("Dismissed: Pause
 *   (Generic CRM — US)", "Decision recorded: Active · daily budget $160.00 (…)"). It is never
 *   `role="alert"`, which AAP 0.5.5 reserves for the discrepancy banner;
 * - **the confirmations by recommendation**: each feedback recorded on this page, keyed by the
 *   item's recommendation identity (`SectionFrame`'s `itemIdentity`), with the same message.
 *   The sections read them ({@link useFeedbackConfirmations}) and show a
 *   {@link FeedbackConfirmation} where the hidden card stood, so the outcome is visible where
 *   the user acted, not only at the top of the page.
 *
 * This module imports neither `RecommendationCard` nor `SectionFrame`, so both can import it:
 * the card records, the frame reads, and no import cycle forms (the card imports the frame).
 * UI comes only from the design-system index (AAP 0.4.4): no raw buttons, headings or tables,
 * no inline styles and no stylesheet. Nothing here fetches (AAP 0.11.1); the confirmations are
 * client state that lives as long as the provider, which on `/actions` is the loaded page.
 *
 * @packageDocumentation
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useSyncExternalStore,
} from 'react';
import type { ReactElement, ReactNode } from 'react';

import { Banner } from '../../design-system';

// ---------------------------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------------------------

/**
 * Announces one outcome in the polite region of the nearest {@link ActionOutcomeProvider}.
 * Blank text is ignored.
 *
 * @param message - the confirmation, for example "Marked done: Add "crm demo" as exact-match
 *   negative (Generic CRM — US)"
 */
export type AnnounceActionOutcome = (message: string) => void;

/**
 * Records the confirmation of a feedback recorded on a card, under the card's recommendation
 * identity, so the sections show it where the card stood once the feedback hides the item. A
 * later feedback on the same recommendation replaces it. A blank identity or message is
 * ignored. Recording announces nothing: the card announces through
 * {@link AnnounceActionOutcome}.
 *
 * @param identity - the item's recommendation identity (`SectionFrame`'s `itemIdentity`)
 * @param message - the confirmation, for example "Dismissed: Pause (Generic CRM — US)"
 */
export type RecordFeedbackConfirmation = (identity: string, message: string) => void;

/**
 * The feedback confirmations recorded on this page, by recommendation identity: what
 * {@link useFeedbackConfirmations} returns, each value the message to show.
 */
export type FeedbackConfirmations = ReadonlyMap<string, string>;

/** No confirmations: outside a provider, or before the first feedback. */
export const NO_FEEDBACK_CONFIRMATIONS: FeedbackConfirmations = new Map<string, string>();

/** The latest announced outcome. */
interface ActionOutcome {
  /** The text announced. */
  message: string;
  /** Increases with every announcement, so each one renders as a new node. */
  sequence: number;
}

/**
 * The confirmations of one provider, held outside React state so that each reader re-renders
 * only when a confirmation it shows changes ({@link useFeedbackConfirmations}): the five
 * sections are memoized, and a feedback recorded in one of them must not render the others
 * again.
 */
interface ConfirmationStore {
  /** Adds a listener called after every change; returns its removal. */
  readonly subscribe: (listener: () => void) => () => void;
  /** The confirmations now, an immutable map replaced on every change. */
  readonly current: () => FeedbackConfirmations;
  /** Records one confirmation, then notifies the listeners when it changed anything. */
  readonly record: RecordFeedbackConfirmation;
}

// ---------------------------------------------------------------------------------------------
// The store
// ---------------------------------------------------------------------------------------------

/** A new, empty {@link ConfirmationStore}. */
function createConfirmationStore(): ConfirmationStore {
  let confirmations: FeedbackConfirmations = NO_FEEDBACK_CONFIRMATIONS;
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    current: () => confirmations,
    record: (identity, message) => {
      const text = message.trim();
      if (identity.trim() === '' || text === '' || confirmations.get(identity) === text) {
        return;
      }
      const next = new Map(confirmations);
      next.set(identity, text);
      confirmations = next;
      for (const listener of [...listeners]) {
        listener();
      }
    },
  };
}

/** The subscription of a reader outside a provider: there is nothing to listen to. */
function subscribeToNothing(): () => void {
  return () => undefined;
}

/**
 * The confirmations among `identities`, each once, in the order given, as one string: the
 * snapshot a reader compares, so it changes exactly when one of those confirmations does.
 * Empty when there is none.
 */
function confirmationsKey(
  confirmations: FeedbackConfirmations,
  identities: readonly string[],
): string {
  const pairs: [string, string][] = [];
  for (const identity of new Set(identities)) {
    const message = confirmations.get(identity);
    if (message !== undefined) {
      pairs.push([identity, message]);
    }
  }
  return pairs.length === 0 ? '' : JSON.stringify(pairs);
}

/** The confirmations among `identities`, as a map. */
function confirmationsAmong(
  confirmations: FeedbackConfirmations,
  identities: readonly string[],
): FeedbackConfirmations {
  const among = new Map<string, string>();
  for (const identity of identities) {
    const message = confirmations.get(identity);
    if (message !== undefined) {
      among.set(identity, message);
    }
  }
  return among;
}

// ---------------------------------------------------------------------------------------------
// The provider
// ---------------------------------------------------------------------------------------------

/** The announcer of the nearest {@link ActionOutcomeProvider}; `null` outside one. */
const ActionOutcomeContext = createContext<AnnounceActionOutcome | null>(null);

/** The confirmation store of the nearest {@link ActionOutcomeProvider}; `null` outside one. */
const ConfirmationStoreContext = createContext<ConfirmationStore | null>(null);

/** Props of {@link ActionOutcomeProvider}. */
export interface ActionOutcomeProviderProps {
  /** The content whose card actions announce their outcomes here: on `/actions`, the sections. */
  children?: ReactNode;
}

/**
 * A polite live region for the outcomes of the card actions inside it (WCAG 2.2 SC 4.1.3),
 * placed above those cards so that it outlives every one of them, and the confirmations of the
 * feedback recorded on them, by recommendation identity, which the sections show where a card
 * a feedback hid stood.
 *
 * A recorded feedback can unmount the card that recorded it: the refetched recommendations
 * carry `current.hidden_by`, and the section drops the item. A status line inside that card
 * would leave with it, unread. Every `RecommendationActions` inside this provider therefore
 * announces "Marked done", "Dismissed", "Snoozed" or "Decision recorded: {state}", with the
 * entity name, here; its own status line stays visible but is no second live region, so
 * nothing is announced twice. A feedback is also recorded under the item's recommendation
 * identity ({@link useRecordFeedbackConfirmation}), and while the hidden item is still in the
 * run's list, its section shows the same message in the item's place
 * ({@link FeedbackConfirmation}), visible beside the cards the user was working on. That
 * confirmation is no live region either.
 *
 * The region is a `<div role="status">` rendered before the children from the first render, so
 * every message lands in a region that already exists. It is never `role="alert"`, which
 * AAP 0.5.5 reserves for the discrepancy banner. It stays empty until the first outcome, and a
 * `Stack` takes an empty status child out of its column, so it adds no space until then. Each
 * message is a new child node, so the same text twice in a row (two cards dismissed with the
 * same action on the same entity) is announced twice. The latest message stays until the next
 * one replaces it. The confirmations last as long as the provider.
 *
 * The provider renders no element around its children: place it among the children of a
 * `Stack`, where the region and the children each take the column's gap.
 *
 * @param props - see {@link ActionOutcomeProviderProps}
 * @returns the live region, followed by the children
 *
 * @example
 * ```tsx
 * <Stack gap={6}>
 *   <SectionNav sections={sections} />
 *   <ActionOutcomeProvider>
 *     <KeywordsSection section={keywords} run={run} overlay={overlay} />
 *     <BudgetSection section={budget} run={run} overlay={overlay} />
 *   </ActionOutcomeProvider>
 * </Stack>
 * ```
 */
export function ActionOutcomeProvider({ children }: ActionOutcomeProviderProps): ReactElement {
  const [outcome, setOutcome] = useState<ActionOutcome | null>(null);
  // One store for the provider's lifetime.
  const [store] = useState(createConfirmationStore);
  // Stable for the provider's lifetime, so a card may call the announcer it captured from a
  // request that settles after the card itself has unmounted.
  const announce = useCallback<AnnounceActionOutcome>((message) => {
    const text = message.trim();
    if (text === '') {
      return;
    }
    setOutcome((previous) => ({ message: text, sequence: (previous?.sequence ?? 0) + 1 }));
  }, []);
  return (
    <ActionOutcomeContext value={announce}>
      <ConfirmationStoreContext value={store}>
        <div role="status">
          {outcome === null ? null : <span key={outcome.sequence}>{outcome.message}</span>}
        </div>
        {children}
      </ConfirmationStoreContext>
    </ActionOutcomeContext>
  );
}

// ---------------------------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------------------------

/**
 * The announcer of the nearest {@link ActionOutcomeProvider}, which `RecommendationActions`
 * uses to confirm its outcomes outside the card.
 *
 * @returns the announcer, or `null` outside a provider, where each card keeps its own polite
 *   status line
 *
 * @example
 * ```tsx
 * const announce = useActionOutcome();
 * announce?.('Dismissed: Pause (Generic CRM — US)');
 * ```
 */
export function useActionOutcome(): AnnounceActionOutcome | null {
  return useContext(ActionOutcomeContext);
}

/**
 * The recorder of feedback confirmations of the nearest {@link ActionOutcomeProvider}, which
 * `RecommendationActions` calls once a feedback is recorded, so the section shows the outcome
 * where the card stood after the feedback hides it.
 *
 * @returns the recorder, stable for the provider's lifetime, so a request that settles after
 *   its card unmounted may still call it; `null` outside a provider
 *
 * @example
 * ```tsx
 * const recordConfirmation = useRecordFeedbackConfirmation();
 * recordConfirmation?.(itemIdentity(item), 'Dismissed: Pause (Generic CRM — US)');
 * ```
 */
export function useRecordFeedbackConfirmation(): RecordFeedbackConfirmation | null {
  return useContext(ConfirmationStoreContext)?.record ?? null;
}

/**
 * The feedback confirmations recorded on this page for the given recommendation identities,
 * read from the nearest {@link ActionOutcomeProvider}.
 *
 * The caller re-renders only when one of those confirmations is recorded or changes, never for
 * another recommendation's, so a feedback recorded in one memoized section leaves the others
 * as they are. Pass the identities of the items the caller may show a confirmation for, such
 * as a section's hidden items; a stable array (memoized on the section) keeps the returned map
 * stable between renders.
 *
 * @param identities - the recommendation identities to read (`SectionFrame`'s `itemIdentity`)
 * @returns each identity's message, for those with a confirmation; empty outside a provider
 *
 * @example
 * ```tsx
 * const identities = useMemo(() => hiddenItems.map(itemIdentity), [hiddenItems]);
 * const confirmations = useFeedbackConfirmations(identities);
 * confirmations.get(itemIdentity(item)); // "Dismissed: Pause (Generic CRM — US)"
 * ```
 */
export function useFeedbackConfirmations(identities: readonly string[]): FeedbackConfirmations {
  const store = useContext(ConfirmationStoreContext);
  const snapshot = useSyncExternalStore(store?.subscribe ?? subscribeToNothing, () =>
    store === null ? '' : confirmationsKey(store.current(), identities),
  );
  return useMemo(
    () =>
      snapshot === '' || store === null
        ? NO_FEEDBACK_CONFIRMATIONS
        : confirmationsAmong(store.current(), identities),
    [snapshot, store, identities],
  );
}

// ---------------------------------------------------------------------------------------------
// The confirmation
// ---------------------------------------------------------------------------------------------

/** Props of {@link FeedbackConfirmation}. */
export interface FeedbackConfirmationProps {
  /** The confirmation, for example "Dismissed: Pause (Generic CRM — US)". */
  message: string;
}

/**
 * The confirmation a section shows in place of a card a feedback recorded on this page has
 * hidden: a success `Banner` titled with the outcome, the action and the entity, for example
 * "Snoozed: Pause (Generic CRM — US)", so the outcome is visible where the user acted.
 *
 * It is plain content: no live region (the provider's region announces the outcome once), no
 * `role="alert"` (AAP 0.5.5 reserves it for discrepancies) and no control, so it adds no Tab
 * stop and keyboard focus moves past it to the next card's action.
 *
 * @param props - see {@link FeedbackConfirmationProps}
 * @returns the banner
 *
 * @example
 * ```tsx
 * <FeedbackConfirmation message="Dismissed: Pause (Generic CRM — US)" />
 * ```
 */
export function FeedbackConfirmation({ message }: FeedbackConfirmationProps): ReactElement {
  return <Banner tone="success" title={message} />;
}
