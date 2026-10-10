/**
 * Test support for the feedback flow of the `/actions` sections: a section rendered from the
 * live `GET /recommendations` read inside an `ActionOutcomeProvider`, as `ActionsPage` renders
 * it, against stub handlers whose `POST /recommendations/{id}/feedback` hides that item in every
 * later read (`current.hidden_by`, AAP 0.3.2: feedback is read live). A test presses a card's
 * feedback action and watches the card leave the page and its confirmation take its place.
 *
 * Only test files import this module; application code never does. It fetches nothing itself:
 * pass {@link FeedbackFlow.handlers} to `./mockApi`.
 *
 * @example
 * ```tsx
 * const flow = feedbackFlow();
 * mockApi(flow.handlers);
 * await renderWithProviders(<LiveSection sectionKey="creative" component={CreativeSection} />);
 * fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
 * await screen.findByText(feedbackConfirmationText('dismiss', item));
 * ```
 *
 * @packageDocumentation
 */
import type { ComponentType, ReactElement } from 'react';

import { useRecommendations } from '../api/queries';
import type { FeedbackKind, Item, RecommendationsPage, SectionKey } from '../api/types';
import { ActionOutcomeProvider } from '../features/actions/ActionOutcomes';
import { FEEDBACK_STATUS_MESSAGES } from '../features/actions/RecommendationCard';
import type { SectionComponentProps } from '../features/actions/sections/SectionFrame';
import { makeFeedback, makeRecommendationsPage } from './fixtures';
import { json } from './mockApi';
import type { MockHandlers } from './mockApi';

/** The feedback kinds a request may carry. */
const FEEDBACK_KINDS: readonly FeedbackKind[] = ['dismiss', 'snooze', 'done'];

/** Props of {@link LiveSection}. */
export interface LiveSectionProps {
  /** The section of the page to render. */
  sectionKey: SectionKey;
  /** The section's component, such as `CreativeSection`. */
  component: ComponentType<SectionComponentProps>;
}

/**
 * One section of the default `GET /recommendations` page, rendered with the page's run and
 * overlay inside an `ActionOutcomeProvider`, which stays mounted while the read refetches, as
 * on `/actions`. Nothing renders in the provider until the first read answers.
 *
 * @param props - see {@link LiveSectionProps}
 * @returns the provider, holding the section once the page is read
 */
export function LiveSection({
  sectionKey,
  component: SectionComponent,
}: LiveSectionProps): ReactElement {
  const page = useRecommendations().data;
  const section = page?.sections.find((candidate) => candidate.key === sectionKey);
  return (
    <ActionOutcomeProvider>
      {page === undefined || section === undefined ? null : (
        <SectionComponent section={section} run={page.run} overlay={page.overlay} />
      )}
    </ActionOutcomeProvider>
  );
}

/** What {@link feedbackFlow} returns. */
export interface FeedbackFlow {
  /** `GET /recommendations` and `POST /recommendations/:id/feedback`, for `mockApi`. */
  handlers: MockHandlers;
  /** The items feedback has hidden so far, by id, with the kind recorded. */
  hidden: ReadonlyMap<Item['id'], FeedbackKind>;
  /**
   * Leaves the item out of every later read, as a rerun that no longer lists it does: the
   * runner applies feedback before ranking, so its next run drops a hidden item.
   */
  drop: (id: Item['id']) => void;
}

/**
 * The feedback kind a request body carries.
 *
 * @throws Error when the body names none, which fails the request and the test
 */
function feedbackKindOf(body: unknown): FeedbackKind {
  const kind: unknown =
    typeof body === 'object' && body !== null ? Reflect.get(body, 'kind') : undefined;
  const known = FEEDBACK_KINDS.find((candidate) => candidate === kind);
  if (known === undefined) {
    throw new Error(`the feedback request carries no kind: ${JSON.stringify(body)}`);
  }
  return known;
}

/**
 * Stub handlers for the feedback flow: every `GET /recommendations` answers a fresh page from
 * `build`, with each item a feedback was posted for carrying `current.hidden_by`, and without
 * the items {@link FeedbackFlow.drop} has dropped; `POST /recommendations/:id/feedback` records
 * the kind for that item and answers 201 with its `Feedback`.
 *
 * @param build - builds a fresh page; the default fixture page when omitted
 * @returns the handlers and the state they share
 */
export function feedbackFlow(
  build: () => RecommendationsPage = makeRecommendationsPage,
): FeedbackFlow {
  const hidden = new Map<Item['id'], FeedbackKind>();
  const dropped = new Set<Item['id']>();
  const pageNow = (): RecommendationsPage => {
    const page = build();
    for (const section of page.sections) {
      for (const group of section.groups) {
        group.items = group.items.filter((item) => !dropped.has(item.id));
        for (const item of group.items) {
          const kind = hidden.get(item.id);
          if (kind !== undefined) {
            item.current.hidden_by = kind;
          }
        }
      }
    }
    return page;
  };
  return {
    hidden,
    drop: (id) => {
      dropped.add(id);
    },
    handlers: {
      'GET /recommendations': () => json(200, pageNow()),
      'POST /recommendations/:id/feedback': (call) => {
        const id = Number(call.params.id);
        const kind = feedbackKindOf(call.body);
        hidden.set(id, kind);
        return json(201, makeFeedback(kind, { recommendation_id: id }));
      },
    },
  };
}

/**
 * The confirmation a recorded feedback leaves in its card's place, and announces:
 * "{Dismissed | Snoozed | Marked done}: {action} ({entity name})", without the
 * " ({entity name})" when the action already contains the name, for example "Snoozed: Pause
 * Partner Search — US" on the entity "Partner Search — US".
 *
 * @param kind - the feedback recorded
 * @param item - the item it was recorded on, which must carry an action and an entity name,
 *   neither padded with whitespace; a CR1 item's action must list its pair in the name's own
 *   order
 * @returns the confirmation text
 */
export function feedbackConfirmationText(kind: FeedbackKind, item: Item): string {
  const status = FEEDBACK_STATUS_MESSAGES[kind];
  const name = String(item.entity.name);
  return item.action.includes(name)
    ? `${status}: ${item.action}`
    : `${status}: ${item.action} (${name})`;
}
