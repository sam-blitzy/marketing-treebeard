/**
 * Tests for `RecommendationCard`, one "do this because of this" action on `/actions`
 * (AAP 0.5.5 Recommendation card; AAP 0.1.2: the card leads with the action, and every number
 * sits beside the claim it supports).
 *
 * The suite holds the card to its TSDoc contract and to AAP 0.5.5:
 *
 * - **Header**: an `<article>` named by the action, with the rank and "{entity name} · {Source}".
 * - **Body**: the reason, the caller's fields, the evidence table, the window chip
 *   "{start} – {end} · {N} days · {source boundaries}" and the flags (recorded plus current).
 * - **Footer**: "Record decision" only on items with a `decision_subject`, opening the
 *   `DecisionForm` prefilled with the proposed change (status `paused` for C1; the proposed daily
 *   budget on a shared budget, with every member listed); "Mark done" on every other action;
 *   "Dismiss" and "Snooze for 7 days" on all but C0, whose resolver renders instead. Each
 *   feedback posts once to `POST /recommendations/{id}/feedback` with the CSRF header.
 * - **Failures and locks**: a 409 `subject_retired` shows "Computed from replaced data" and locks
 *   every action; any other failure shows the API's message; a historical item shows the badge
 *   and its frozen provenance with every action disabled, the C0 resolver too.
 * - **Holds**: a held item states why and links the Campaigns discrepancy group.
 *
 * The network is stubbed below the real API client by `../../test/mockApi`, which fails a test
 * on any request it has no handler for; it is imported before `../../test/render` and Testing
 * Library so its `afterEach` runs first. Items come from `../../test/fixtures`: the default
 * page's C1, C0 and historical CR1 cards, or `makeItem` for a single variant.
 */
import { apiError, json, mockApi, type ApiMock, type MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DataStatus, Decision, FeedbackKind, Item, ProvenanceEntry } from '../../api/types';
import { Button } from '../../design-system';
import { formatWindowChip } from '../../lib/format';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_DATASET_IDS,
  FIXTURE_FLAGS,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeDecision,
  makeEvidence,
  makeFeedback,
  makeFlag,
  makeItem,
  makeItemOverlay,
  makeRecommendationsPage,
} from '../../test/fixtures';
import {
  BLOCKING_DISCREPANCY_LINK_LABEL,
  DECISION_RECORDED_MESSAGE,
  FEEDBACK_LABELS,
  FEEDBACK_STATUS_MESSAGES,
  HISTORICAL_LABEL,
  RECORD_DECISION_LABEL,
  RecommendationActions,
  RecommendationCard,
  historicalProvenanceText,
} from './RecommendationCard';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The in-page anchor of the Campaigns discrepancy group (`SectionFrame.groupAnchorId`). */
const DISCREPANCY_ANCHOR = '#group-campaigns-discrepancies';

/** The CSRF header every mutating request carries (AAP 0.5.3). */
const CSRF = ['X-Requested-With', 'ads-engine'] as const;

/** The feedback kinds, each with its footer label (AAP 0.5.5). */
const FEEDBACK_KINDS: readonly FeedbackKind[] = ['done', 'dismiss', 'snooze'];

/** A fresh default fixture page item, by id. */
function pageItem(id: number): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** The default page's C1 "Pause" on "Generic CRM — US", which carries a decision subject. */
function c1Item(): Item {
  return pageItem(FIXTURE_ITEM_IDS.c1);
}

/**
 * The baseline decision of "Generic CRM — US" (the C1 subject): own budget, status `active`,
 * daily budget USD 200.00, as `GET /decisions?subject_type=campaign&subject_id=302` lists it,
 * with `overrides` applied (shallow).
 */
function genericCrmDecision(overrides: Partial<Decision> = {}): Decision {
  return makeDecision('own_budget', {
    subject: {
      type: 'campaign',
      id: FIXTURE_CAMPAIGN_IDS.genericCrm,
      name: 'Generic CRM — US',
      source: 'google',
      currency: 'USD',
    },
    ...overrides,
  });
}

/**
 * Members of the shared "Search Shared Budget", as `budget._members_extra` records them:
 * the fixture world's shared member and a second campaign built for this suite.
 */
const SHARED_MEMBERS = [
  {
    campaign_id: FIXTURE_CAMPAIGN_IDS.searchPartners,
    name: 'Search Partners — US',
    member_days: 28,
  },
  { campaign_id: 309, name: 'Search Competitors — US', member_days: 21 },
];

/**
 * A B1 increase on the shared "Search Shared Budget": its decision subject is the budget, and
 * its evidence carries the proposed daily budget and every member campaign (the shape of
 * `analysis/budget.py`'s `budget_evidence` for a shared subject).
 */
function sharedB1Item(): Item {
  return makeItem({
    id: 1250,
    rule_id: 'B1',
    action: 'Raise daily budget from USD 500.00 to USD 600.00',
    reason: 'Capped on 22 of 26 budget-active days: raise the daily budget by 20%.',
    entity: {
      source: 'google',
      type: 'budget',
      id: '31000402',
      name: 'Search Shared Budget',
    },
    evidence: makeEvidence({
      extra: {
        subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
        subject_type: 'budget',
        shared: true,
        currency: 'USD',
        current: '500.00',
        proposed_daily_budget: '600.00',
        members: SHARED_MEMBERS,
      },
    }),
    flags: [makeFlag('simulated'), makeFlag('shared_budget')],
    decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
  });
}

/** The overlay of a historical item: its frozen dataset was replaced, and the source is live. */
function historicalOverlay(overrides: Partial<Item['current']> = {}): Item['current'] {
  return makeItemOverlay({
    historical: true,
    held_now: true,
    held_reason_now: 'Computed from data that has since been replaced',
    data_status_now: 'live',
    ...overrides,
  });
}

/** The frozen provenance of an item read from LinkedIn's retired simulated dataset. */
const RETIRED_LINKEDIN_PROVENANCE = [
  {
    source: 'linkedin',
    dataset_id: FIXTURE_DATASET_IDS.retiredLinkedin,
    mode: 'simulated',
    role: 'primary',
  },
] as const satisfies readonly ProvenanceEntry[];

/** Renders `ui` with a query client and router, at `/actions`. */
async function renderCard(ui: ReactNode) {
  return renderWithProviders(<>{ui}</>, { path: '/actions' });
}

/** The card of `item`, found by its accessible name (the action). */
function cardOf(item: Item): HTMLElement {
  return screen.getByRole('article', { name: item.action });
}

/** Every button of the card, in document order. */
function cardButtons(item: Item): HTMLElement[] {
  return within(cardOf(item)).queryAllByRole('button');
}

/** The names of the card's buttons, in document order. */
function buttonNames(item: Item): string[] {
  return cardButtons(item).map((button) => button.textContent);
}

/** The POST requests the card sent, in order. */
function posts(api: ApiMock) {
  return api.calls.filter((call) => call.method === 'POST');
}

/** The open "Record decision" dialog, once its form has loaded the subject's decision log. */
async function openDecisionDialog(item: Item): Promise<HTMLElement> {
  fireEvent.click(within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL }));
  const dialog = await screen.findByRole('dialog', { name: RECORD_DECISION_LABEL });
  await within(dialog).findByRole('button', { name: 'Save decision' });
  return dialog;
}

let api: ApiMock;

beforeEach(() => {
  // No handler yet: any request a test does not expect fails it.
  api = mockApi({});
});

// ---------------------------------------------------------------------------------------------
// historicalProvenanceText
// ---------------------------------------------------------------------------------------------

describe('historicalProvenanceText', () => {
  it('states the frozen provenance and the current data status apart', () => {
    expect(historicalProvenanceText(RETIRED_LINKEDIN_PROVENANCE, 'live')).toBe(
      'Simulated LinkedIn data, since replaced by live',
    );
  });

  it('says the source data was replaced when the evidence names no dataset', () => {
    expect(historicalProvenanceText([], 'live')).toBe('Source data has since been replaced');
  });

  it('joins one phrase per distinct mode and source, in evidence order', () => {
    const text = historicalProvenanceText(
      [
        ...RETIRED_LINKEDIN_PROVENANCE,
        {
          source: 'hubspot',
          dataset_id: FIXTURE_DATASET_IDS.hubspotCompanion,
          mode: 'simulated',
          role: 'companion',
        },
        // Another dataset of the same source and mode adds no second phrase.
        {
          source: 'linkedin',
          dataset_id: FIXTURE_DATASET_IDS.linkedin,
          mode: 'simulated',
          role: 'primary',
        },
      ],
      'mixed',
    );

    expect(text).toBe(
      'Simulated LinkedIn data and Simulated HubSpot data, since replaced by mixed',
    );
  });

  it('leaves out a blank mode or status rather than printing it', () => {
    // Only a cast can express the blank values a runtime payload could still carry.
    const blankMode = [
      { ...RETIRED_LINKEDIN_PROVENANCE[0], mode: ' ' },
    ] as unknown as ProvenanceEntry[];
    const blankStatus = '' as unknown as DataStatus;

    expect(historicalProvenanceText(blankMode, 'live')).toBe(
      'LinkedIn data, since replaced by live',
    );
    expect(historicalProvenanceText(RETIRED_LINKEDIN_PROVENANCE, blankStatus)).toBe(
      'Simulated LinkedIn data, since replaced',
    );
  });
});

// ---------------------------------------------------------------------------------------------
// Header and body
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: header and body', () => {
  it('is an article named by the action, with the rank and "{name} · {Source}"', async () => {
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(card.id).toBe(`item-${String(item.id)}`);
    expect(within(card).getByRole('heading', { level: 3, name: 'Pause' })).toBeDefined();
    expect(within(card).getByText('Rank').parentElement?.textContent).toBe('Rank 1');
    expect(within(card).getByText('Generic CRM — US · Google')).toBeDefined();
  });

  it('renders its headline at the level the caller asks for', async () => {
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} headingLevel={4} />);

    expect(within(cardOf(item)).getByRole('heading', { level: 4, name: 'Pause' })).toBeDefined();
  });

  it('leaves out an entity line the stored entity cannot fill', async () => {
    const item = makeItem({ entity: { source: null, type: 'campaign', id: '1', name: '  ' } });
    await renderCard(<RecommendationCard item={item} />);

    expect(cardOf(item).querySelector('.ds-card__meta')).toBeNull();
  });

  it('shows the source alone when the entity has no name', async () => {
    const item = makeItem({
      entity: { source: 'linkedin', type: 'campaign', id: '1', name: null },
    });
    await renderCard(<RecommendationCard item={item} />);

    expect(within(cardOf(item)).getByText('LinkedIn')).toBeDefined();
  });

  it('shows the reason, caller fields, evidence, window chip and flags, in order', async () => {
    const item = c1Item();
    await renderCard(
      <RecommendationCard item={item}>
        <div>Section fields</div>
      </RecommendationCard>,
    );

    const card = cardOf(item);
    const reason = within(card).getByText(item.reason);
    const fields = within(card).getByText('Section fields');
    const table = within(card).getByRole('table', { name: 'Evidence' });
    const chip = within(card).getByText(formatWindowChip(item.evidence.window));
    const flags = within(card).getByRole('list', { name: 'Flags' });

    const order = [reason, fields, table, chip, flags];
    order.slice(1).forEach((node, index) => {
      const previous = order[index];
      expect(previous?.compareDocumentPosition(node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    });
    expect(within(table).getByRole('rowheader', { name: 'Window spend' })).toBeDefined();
  });

  it('shows the window chip as "{start} – {end} · {N} days · {boundaries}"', async () => {
    const item = makeItem();
    await renderCard(<RecommendationCard item={item} />);

    const chip = formatWindowChip(item.evidence.window);
    expect(chip).toBe(
      '2026-09-08 – 2026-10-05 · 28 days · Google days: America/New_York · LinkedIn days: UTC',
    );
    expect(within(cardOf(item)).getByText(chip)).toBeDefined();
  });

  it('shows the recorded flags plus the current ones', async () => {
    const item = makeItem({
      flags: [makeFlag('simulated'), makeFlag('search_term_coverage')],
      current: makeItemOverlay({ flags_now: [makeFlag('stale')] }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const flags = within(cardOf(item)).getByRole('list', { name: 'Flags' });
    expect(
      within(flags)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual([
      'Simulated data',
      FIXTURE_FLAGS.search_term_coverage.message,
      FIXTURE_FLAGS.stale.message,
    ]);
  });

  it('renders no reason line when the reason is blank', async () => {
    const item = makeItem({ reason: '   ' });
    const { container } = await renderCard(<RecommendationCard item={item} />);

    // Nothing, not even a blank line, precedes the evidence table in the body.
    const body = container.querySelector('.ds-card__body');
    expect(body?.textContent?.startsWith('Evidence')).toBe(true);
  });
});

// ---------------------------------------------------------------------------------------------
// Holds and historical items
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: holds', () => {
  it('states why a held item is held and links the blocking discrepancy', async () => {
    const item = makeItem({
      current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(within(card).getByText('Resolve discrepancy first')).toBeDefined();
    const link = within(card).getByRole('link', { name: BLOCKING_DISCREPANCY_LINK_LABEL });
    expect(link.getAttribute('href')).toBe(DISCREPANCY_ANCHOR);
  });

  it('shows the live hold reason before the one frozen with the run', async () => {
    const item = makeItem({
      held: true,
      held_reason: 'Resolve discrepancy on Brand Search first',
      current: makeItemOverlay({
        held_now: true,
        held_reason_now: 'Resolve 2 discrepancies first',
      }),
    });
    await renderCard(<RecommendationCard item={item} />);

    expect(within(cardOf(item)).getByText('Resolve 2 discrepancies first')).toBeDefined();
    expect(
      within(cardOf(item)).queryByText('Resolve discrepancy on Brand Search first'),
    ).toBeNull();
  });

  it('falls back to the run reason, then the P1 wording, without a live reason', async () => {
    const recorded = makeItem({ held: true, held_reason: 'Held by the run' });
    const { unmount } = await renderCard(<RecommendationCard item={recorded} />);
    expect(within(cardOf(recorded)).getByText('Held by the run')).toBeDefined();
    unmount();

    const unexplained = makeItem({ held: true, held_reason: null });
    await renderCard(<RecommendationCard item={unexplained} />);
    expect(within(cardOf(unexplained)).getByText('Resolve discrepancy first')).toBeDefined();
  });

  it('shows the held page card from the fixture with its discrepancy link', async () => {
    const item = pageItem(FIXTURE_ITEM_IDS.heldPause);
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(within(card).getByText('Resolve discrepancy first')).toBeDefined();
    expect(
      within(card)
        .getByRole('link', { name: BLOCKING_DISCREPANCY_LINK_LABEL })
        .getAttribute('href'),
    ).toBe(DISCREPANCY_ANCHOR);
  });

  it('never shows a hold notice on a C0 discrepancy, which is exempt from holds', async () => {
    const base = pageItem(FIXTURE_ITEM_IDS.c0);
    const item = { ...base, held: true, current: { ...base.current, held_now: true } };
    await renderCard(<RecommendationCard item={item} />);

    expect(within(cardOf(item)).queryByRole('link')).toBeNull();
  });
});

describe('RecommendationCard: historical items', () => {
  it('shows the badge and the frozen provenance and disables every action', async () => {
    const base = pageItem(FIXTURE_ITEM_IDS.cr1Historical);
    const item = { ...base, current: { ...base.current, data_status_now: 'live' as const } };
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(within(card).getByText(HISTORICAL_LABEL).className).toBe('ds-badge ds-badge--warning');
    expect(within(card).getByText('Simulated LinkedIn data, since replaced by live')).toBeDefined();
    // The overlay holds it with the replaced-data reason, which the badge already states.
    expect(within(card).queryByRole('link', { name: BLOCKING_DISCREPANCY_LINK_LABEL })).toBeNull();

    expect(buttonNames(item)).toEqual([
      FEEDBACK_LABELS.done,
      FEEDBACK_LABELS.dismiss,
      FEEDBACK_LABELS.snooze,
    ]);
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
      fireEvent.click(button);
    }
    expect(api.calls).toEqual([]);
  });

  it('disables "Record decision" on a historical item with a decision subject', async () => {
    const item: Item = { ...c1Item(), current: historicalOverlay() };
    await renderCard(<RecommendationCard item={item} />);

    expect(
      within(cardOf(item)).getByText('Simulated Google data, since replaced by live'),
    ).toBeDefined();

    expect(buttonNames(item)).toEqual([
      RECORD_DECISION_LABEL,
      FEEDBACK_LABELS.dismiss,
      FEEDBACK_LABELS.snooze,
    ]);
    cardButtons(item).forEach((button) => {
      expect(button).toHaveProperty('disabled', true);
    });
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Footer actions
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: footer actions', () => {
  it('offers "Mark done", "Dismiss" and "Snooze for 7 days" without a subject', async () => {
    const item = makeItem();
    await renderCard(<RecommendationCard item={item} />);

    expect(buttonNames(item)).toEqual(['Mark done', 'Dismiss', 'Snooze for 7 days']);
    expect(within(cardOf(item)).queryByRole('button', { name: RECORD_DECISION_LABEL })).toBeNull();
    cardButtons(item).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });
  });

  it('offers "Record decision" instead of "Mark done" with a decision subject', async () => {
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    expect(buttonNames(item)).toEqual([RECORD_DECISION_LABEL, 'Dismiss', 'Snooze for 7 days']);
    expect(
      within(cardOf(item))
        .getByRole('button', { name: RECORD_DECISION_LABEL })
        .getAttribute('aria-haspopup'),
    ).toBe('dialog');
    expect(within(cardOf(item)).queryByRole('button', { name: 'Mark done' })).toBeNull();
  });

  it.each(FEEDBACK_KINDS)(
    'posts "%s" feedback once, with the CSRF header, and then locks the card',
    async (kind) => {
      api.use({ 'POST /recommendations/:id/feedback': json(201, makeFeedback(kind)) });
      const item = pageItem(FIXTURE_ITEM_IDS.k1);
      await renderCard(<RecommendationCard item={item} />);

      fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS[kind] }));

      const status = await within(cardOf(item)).findByText(FEEDBACK_STATUS_MESSAGES[kind]);
      expect(status.getAttribute('role')).toBe('status');
      const [post] = posts(api);
      expect(posts(api)).toHaveLength(1);
      expect(post?.path).toBe(`/recommendations/${String(item.id)}/feedback`);
      expect(post?.params).toEqual({ id: String(item.id) });
      expect(post?.body).toEqual({ kind });
      expect(post?.headers.get(CSRF[0])).toBe(CSRF[1]);

      // A recorded feedback ends the card's actions: a second post would only repeat it.
      for (const button of cardButtons(item)) {
        expect(button).toHaveProperty('disabled', true);
        fireEvent.click(button);
      }
      expect(posts(api)).toHaveLength(1);
    },
  );

  it('shows the pressed action loading and disables every action while it is pending', async () => {
    let release: (response: MockResponse) => void = () => {
      throw new Error('the pending feedback response was never created');
    };
    const pending = new Promise<MockResponse>((resolve) => {
      release = resolve;
    });
    api.use({ 'POST /recommendations/:id/feedback': () => pending });
    const item = makeItem();
    await renderCard(<RecommendationCard item={item} />);

    const snooze = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.snooze });
    fireEvent.click(snooze);

    await waitFor(() => {
      expect(snooze.getAttribute('aria-busy')).toBe('true');
    });
    cardButtons(item).forEach((button) => {
      expect(button).toHaveProperty('disabled', true);
    });
    expect(
      within(cardOf(item))
        .getByRole('button', { name: FEEDBACK_LABELS.dismiss })
        .getAttribute('aria-busy'),
    ).toBeNull();

    release(json(201, makeFeedback('snooze', { recommendation_id: item.id })));
    expect(await within(cardOf(item)).findByText('Snoozed')).toBeDefined();
    expect(snooze.getAttribute('aria-busy')).toBeNull();
    expect(posts(api)).toHaveLength(1);
  });

  it('shows "Computed from replaced data" on a 409 subject_retired and locks', async () => {
    api.use({
      'POST /recommendations/:id/feedback': apiError(
        409,
        'subject_retired',
        'Computed from data that has since been replaced.',
      ),
    });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));

    const message = await within(cardOf(item)).findByText(HISTORICAL_LABEL);
    expect(
      within(cardOf(item)).queryByText('Computed from data that has since been replaced.'),
    ).toBeNull();
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(document.activeElement?.getAttribute('tabindex')).toBe('-1');
    expect(within(cardOf(item)).queryByRole('alert')).toBeNull();
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
      fireEvent.click(button);
    }
    expect(posts(api)).toHaveLength(1);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it("shows the API's message on a 422, focuses it, and lets the action be retried", async () => {
    api.use({
      'POST /recommendations/:id/feedback': apiError(
        422,
        'feedback_not_allowed',
        'Feedback is not accepted for this item.',
      ),
    });
    const item = makeItem();
    await renderCard(<RecommendationCard item={item} />);

    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done }));

    const message = await within(cardOf(item)).findByText(
      'Feedback is not accepted for this item.',
    );
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(within(cardOf(item)).queryByText(HISTORICAL_LABEL)).toBeNull();
    cardButtons(item).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });

    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('dismiss', { recommendation_id: item.id }),
      ),
    });
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));

    expect(await within(cardOf(item)).findByText('Dismissed')).toBeDefined();
    expect(within(cardOf(item)).queryByText('Feedback is not accepted for this item.')).toBeNull();
    expect(posts(api).map((call) => call.body)).toEqual([{ kind: 'done' }, { kind: 'dismiss' }]);
  });

  it('keeps its action state with its item, never carrying it to another', async () => {
    const first = makeItem({ id: 61, action: 'Add "crm demo" as exact-match negative' });
    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('dismiss', { recommendation_id: first.id }),
      ),
    });
    const second = makeItem({ id: 62, action: 'Add "crm trial" as exact-match negative' });

    /** Shows `first`, then `second` in the same place once "Show next item" is pressed. */
    function ItemSwitcher() {
      const [item, setItem] = useState(first);
      return (
        <>
          <Button
            onClick={() => {
              setItem(second);
            }}
          >
            Show next item
          </Button>
          <RecommendationCard item={item} />
        </>
      );
    }
    await renderCard(<ItemSwitcher />);

    fireEvent.click(within(cardOf(first)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    expect(await within(cardOf(first)).findByText('Dismissed')).toBeDefined();

    fireEvent.click(screen.getByRole('button', { name: 'Show next item' }));

    const card = cardOf(second);
    expect(within(card).queryByText('Dismissed')).toBeNull();
    cardButtons(second).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Record decision
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: record decision', () => {
  it('opens the decision form for the C1 campaign with status prefilled "Paused"', async () => {
    api.use({ 'GET /decisions': json(200, [genericCrmDecision()]) });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);

    const status = within(dialog).getByRole('combobox', { name: 'Status' });
    expect(status).toHaveProperty('value', 'paused');
    expect(within(status).getByRole('option', { name: 'Paused' })).toHaveProperty('selected', true);
    // Own budget: the daily budget is offered too, unchanged from the last decision.
    expect(within(dialog).getByRole('spinbutton', { name: 'Daily budget' })).toHaveProperty(
      'value',
      '200.00',
    );

    const [read] = api.calls;
    expect(read?.method).toBe('GET');
    expect(read?.path).toBe('/decisions');
    expect(read?.search.get('subject_type')).toBe('campaign');
    expect(read?.search.get('subject_id')).toBe(String(FIXTURE_CAMPAIGN_IDS.genericCrm));
  });

  it('records the paused status from the dialog, closes it and reports the decision', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 811,
          origin: 'user',
          decided_by: 'admin@example.com',
          status: 'paused',
          patch: { status: 'paused' },
          recommendation_id: FIXTURE_ITEM_IDS.c1,
        }),
      ),
    });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    expect(await within(cardOf(item)).findByText(DECISION_RECORDED_MESSAGE)).toBeDefined();
    expect(screen.queryByRole('dialog')).toBeNull();
    const [post] = posts(api);
    expect(posts(api)).toHaveLength(1);
    expect(post?.path).toBe('/decisions');
    expect(post?.body).toEqual({
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
      patch: { status: 'paused' },
      recommendation_id: FIXTURE_ITEM_IDS.c1,
    });
    expect(post?.headers.get(CSRF[0])).toBe(CSRF[1]);
    // Recording a decision is not feedback.
    expect(api.calls.some((call) => call.path.endsWith('/feedback'))).toBe(false);
  });

  it('closes the dialog without a request on Cancel or Close', async () => {
    api.use({ 'GET /decisions': json(200, [genericCrmDecision()]) });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    const reopened = await openDecisionDialog(item);
    fireEvent.click(within(reopened).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    expect(posts(api)).toEqual([]);
    expect(within(cardOf(item)).queryByText(DECISION_RECORDED_MESSAGE)).toBeNull();
  });

  it('prefills the proposed daily budget on a shared budget and lists every member', async () => {
    api.use({ 'GET /decisions': json(200, [makeDecision('budget')]) });
    const item = sharedB1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);

    expect(within(dialog).getByRole('spinbutton', { name: 'Daily budget' })).toHaveProperty(
      'value',
      '600.00',
    );
    expect(within(dialog).getByText('In USD')).toBeDefined();
    expect(
      within(dialog).getByText(
        'Applies to the shared budget used by: Search Partners — US, Search Competitors — US',
      ),
    ).toBeDefined();
    // A shared budget has no status of its own (AAP 0.5.2).
    expect(within(dialog).queryByRole('combobox', { name: 'Status' })).toBeNull();
    expect(api.calls[0]?.search.get('subject_type')).toBe('budget');
    expect(api.calls[0]?.search.get('subject_id')).toBe(String(FIXTURE_BUDGET_IDS.search));
  });

  it("lets the caller's decision context refine the item's own", async () => {
    api.use({ 'GET /decisions': json(200, [makeDecision('budget')]) });
    const item = sharedB1Item();
    await renderCard(
      <RecommendationCard
        item={item}
        decisionContext={{
          prefill: { daily_budget: '650.00' },
          members: [
            { id: String(FIXTURE_CAMPAIGN_IDS.searchPartners), name: 'Search Partners — US' },
          ],
        }}
      />,
    );

    const dialog = await openDecisionDialog(item);

    expect(within(dialog).getByRole('spinbutton', { name: 'Daily budget' })).toHaveProperty(
      'value',
      '650.00',
    );
    expect(
      within(dialog).getByText('Applies to the shared budget used by: Search Partners — US'),
    ).toBeDefined();
  });
});

// ---------------------------------------------------------------------------------------------
// C0 discrepancies
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: C0 discrepancies', () => {
  /** A resolver that renders one resolution button, disabled as the card asks. */
  function makeResolver() {
    return vi.fn(({ disabled }: { disabled: boolean }) => (
      <Button disabled={disabled}>Platform is correct</Button>
    ));
  }

  it('renders the resolver instead of any feedback or decision action', async () => {
    const resolver = makeResolver();
    const item = pageItem(FIXTURE_ITEM_IDS.c0);
    await renderCard(<RecommendationCard item={item} resolver={resolver} />);

    expect(buttonNames(item)).toEqual(['Platform is correct']);
    expect(
      within(cardOf(item)).getByRole('button', { name: 'Platform is correct' }),
    ).toHaveProperty('disabled', false);
    expect(resolver).toHaveBeenCalledWith({ disabled: false });
  });

  it('passes disabled: true to the resolver of a historical C0 item', async () => {
    const resolver = makeResolver();
    const item: Item = { ...pageItem(FIXTURE_ITEM_IDS.c0), current: historicalOverlay() };
    await renderCard(<RecommendationCard item={item} resolver={resolver} />);

    expect(resolver).toHaveBeenCalledWith({ disabled: true });
    expect(resolver).not.toHaveBeenCalledWith({ disabled: false });
    expect(
      within(cardOf(item)).getByRole('button', { name: 'Platform is correct' }),
    ).toHaveProperty('disabled', true);
    expect(within(cardOf(item)).getByText(HISTORICAL_LABEL)).toBeDefined();
  });

  it('renders no footer when there is no resolver, or the resolver renders nothing', async () => {
    const item = pageItem(FIXTURE_ITEM_IDS.c0);
    const { container, unmount } = await renderCard(<RecommendationCard item={item} />);
    expect(cardButtons(item)).toEqual([]);
    expect(container.querySelector('.ds-card__footer')).toBeNull();
    unmount();

    const empty = await renderCard(<RecommendationCard item={item} resolver={() => null} />);
    expect(empty.container.querySelector('.ds-card__footer')).toBeNull();
  });

  it('gives a C0 item no actions when RecommendationActions renders it', async () => {
    const { container } = await renderCard(
      <RecommendationActions item={pageItem(FIXTURE_ITEM_IDS.c0)} />,
    );

    expect(screen.queryAllByRole('button')).toEqual([]);
    expect(container.textContent).toBe('');
  });
});
