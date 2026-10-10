/**
 * Tests for `RecommendationCard`, one "do this because of this" action on `/actions`
 * (AAP 0.5.5 Recommendation card; AAP 0.1.2: the card leads with the action, and every number
 * sits beside the claim it supports).
 *
 * The suite holds the card to its TSDoc contract and to AAP 0.5.5:
 *
 * - **Header**: an `<article>` named by the action, with the rank, the headline number the
 *   frozen evidence selects ("{label} {value}", outside the heading; none when it selects none)
 *   and "{entity name} · {Source}".
 * - **Body**: the reason, the caller's fields, the evidence table, the window chip
 *   "{start} – {end} · {N} days · {source boundaries}" and the flags (recorded plus current,
 *   with the open-discrepancy count read from the overlay alone).
 *   With `reasonPlacement="fields"` the card prints no reason line, so the caller's fields
 *   carry the reason once.
 * - **Footer**: "Record decision" only on items with a `decision_subject`, opening the
 *   `DecisionForm` prefilled with the proposed change (status `paused` for C1; the proposed daily
 *   budget on a shared budget, with every member listed); "Mark done" on every other action;
 *   "Dismiss" and "Snooze for 7 days" on all but C0, whose resolver renders instead. Each
 *   feedback posts once to `POST /recommendations/{id}/feedback` with the CSRF header, also
 *   when pressed twice, or with another action, before React renders again; a failed one
 *   frees the actions for one retry.
 * - **Failures and locks**: a 409 `subject_retired` shows "Computed from replaced data" and locks
 *   every action, whether a feedback or the decision form received it, also once the dialog has
 *   closed and when it was closed while the save ran; any other failure shows the API's
 *   message, and a failure that is not an `ApiError` the generic text, never its own message;
 *   a historical item shows the badge and its frozen provenance with every action disabled,
 *   the C0 resolver too.
 * - **Outcomes and focus**: inside an `ActionOutcomeProvider`, a recorded feedback or decision is
 *   announced in the provider's polite region, which outlives the card a refetch removes, naming
 *   the entity once: after the action unless the action already contains its name, and always
 *   after a decision's recorded state; and
 *   focus moves to the next card's first enabled action, else the previous card's, else the
 *   section heading, unless the user has moved it elsewhere.
 * - **Decision saves**: the dialog may close while its save runs, but every action stays
 *   disabled and "Record decision" loading until the save settles, so nothing reopens it or
 *   posts again. A late success is reported and announced, a late failure of any kind is shown
 *   on the card and takes focus, and focus otherwise returns to "Record decision" when it was
 *   lost.
 * - **A newer run**: the same recommendation under a new id keeps the footer's buttons, focus on
 *   them, a recorded feedback, a failure's text and an open decision dialog with its edits; a
 *   retirement lock stays with the item it was learned on, the recommendation id the refused
 *   request submitted, so the newer item is unlocked while the "Computed from replaced data"
 *   banner, and the focus on it, stay until the next action starts; a refusal that arrives for a
 *   replaced item, or for the item a still-open dialog was opened from, shows the same message
 *   without locking the item shown. Another recommendation in the same place starts fresh.
 * - **Holds**: a held item states why and links its recorded blocking discrepancy on
 *   `/decisions` (rendered alone, the card has no run C0 card to link), or, with no blocker to
 *   name, "View open discrepancies" to the Campaigns discrepancy group.
 *
 * The network is stubbed below the real API client by `../../test/mockApi`, which fails a test
 * on any request it has no handler for; it is imported before `../../test/render` and Testing
 * Library so its `afterEach` runs after Testing Library's automatic cleanup (Vitest runs
 * after-hooks in reverse order of registration). Items come from `../../test/fixtures`: the
 * default page's C1, C0 and historical CR1 cards, or `makeItem` for a single variant.
 */
import { apiError, html, json, mockApi, type ApiMock, type MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useRecommendations } from '../../api/queries';
import type {
  DataStatus,
  Decision,
  FeedbackKind,
  Item,
  Metric,
  ProvenanceEntry,
} from '../../api/types';
import { Button, PageShell, Stack } from '../../design-system';
import { formatWindowChip } from '../../lib/format';
import { OPEN_DISCREPANCIES_LINK_LABEL } from './BlockingDiscrepancies';
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
  ActionOutcomeProvider,
  BLOCKING_DISCREPANCY_LINK_LABEL,
  DECISION_RECORDED_MESSAGE,
  DECISION_SAVING_MESSAGE,
  FEEDBACK_LABELS,
  FEEDBACK_STATUS_MESSAGES,
  HISTORICAL_LABEL,
  RECORD_DECISION_LABEL,
  RecommendationActions,
  RecommendationCard,
  historicalProvenanceText,
  useActionOutcome,
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
 * The card's status line once a "Generic CRM — US" decision with status `paused` is saved, its
 * daily budget kept at USD 200.00: what the saved decision recorded, as the decision log words
 * it, never the recommendation's action.
 */
const PAUSED_RECORDED_LINE = `${DECISION_RECORDED_MESSAGE}: Paused · daily budget $200.00`;

/** Any recorded-decision status line, whatever state it names. */
const RECORDED_LINE_PATTERN = new RegExp(`^${DECISION_RECORDED_MESSAGE}`, 'u');

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

/**
 * Whether a button is inactive as `Button` renders it: natively disabled, or `aria-disabled`
 * while it loads or keeps the keyboard focus it held when it was disabled.
 */
function isInactive(button: HTMLElement): boolean {
  return button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';
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
      `Stale: ${FIXTURE_FLAGS.stale.message}`,
    ]);
  });

  it('drops the recorded unverified flag once no discrepancy is open now', async () => {
    // Recorded while 1 discrepancy was open; the overlay omits the flag at 0 open.
    const item = makeItem({
      flags: [
        makeFlag('simulated'),
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancy is open',
        }),
      ],
      current: makeItemOverlay({ flags_now: [makeFlag('stale')] }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const flags = within(cardOf(item)).getByRole('list', { name: 'Flags' });
    expect(
      within(flags)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual(['Simulated data', `Stale: ${FIXTURE_FLAGS.stale.message}`]);
    expect(within(flags).queryByText(/Unverified while/u)).toBeNull();
    expect(within(cardOf(item)).queryByText(/Unverified while/u)).toBeNull();
  });

  it('shows the live open-discrepancy count once, in place of the recorded one', async () => {
    const item = makeItem({
      flags: [
        makeFlag('simulated'),
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancy is open',
        }),
      ],
      current: makeItemOverlay({
        flags_now: [
          makeFlag('unverified_discrepancies', {
            message: 'Unverified while 2 discrepancies are open',
          }),
        ],
      }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const flags = within(cardOf(item)).getByRole('list', { name: 'Flags' });
    const unverified = within(flags).getAllByText(/Unverified while/u);
    expect(unverified.map((badge) => badge.textContent)).toEqual([
      'Unverified while 2 discrepancies are open',
    ]);
    expect(
      within(flags)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual(['Simulated data', 'Unverified while 2 discrepancies are open']);
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
// Headline number
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: headline number', () => {
  /** A K1 cited number as `analysis.context.Metric.to_json` renders it, in USD. */
  function usdMetric(key: string, label: string, value: Metric['value']): Metric {
    return {
      key,
      label,
      value,
      unit: 'currency',
      currency: 'USD',
      floor: null,
      unavailable: value === null ? true : null,
    };
  }

  /**
   * A K1 block candidate on "crm demo" whose frozen evidence cites its spend and wasted spend
   * and, with `extra`, selects its headline number (`extra.headline_metric`).
   */
  function k1Item(extra: Record<string, unknown> | null, wasted: Metric['value'] = '515.34'): Item {
    return makeItem({
      evidence: makeEvidence({
        metrics: [
          usdMetric('spend', 'Spend', '515.34'),
          usdMetric('wasted_spend', 'Wasted spend', wasted),
        ],
        extra,
      }),
    });
  }

  /** The header of `item`'s card. */
  function headerOf(item: Item): HTMLElement {
    const header = cardOf(item).querySelector('.ds-card__header');
    if (!(header instanceof HTMLElement)) {
      throw new Error('The card header was not rendered');
    }
    return header;
  }

  it('leads a K1 card with its wasted spend, between the action and the entity line', async () => {
    const item = k1Item({ headline_metric: 'wasted_spend' });
    await renderCard(<RecommendationCard item={item} />);

    // The card is still named by the action alone: the figure is not part of its name.
    const card = screen.getByRole('article', { name: 'Add as exact-match negative' });
    const header = headerOf(item);
    const heading = within(header).getByRole('heading', {
      level: 3,
      name: 'Add as exact-match negative',
    });
    const figure = within(header).getByText('Wasted spend').parentElement;
    if (figure === null) {
      throw new Error('The headline figure was not rendered');
    }

    expect(figure.textContent).toBe('Wasted spend $515.34');
    expect(heading.contains(figure)).toBe(false);
    const entity = within(header).getByText('crm demo · Google');
    expect(heading.compareDocumentPosition(figure)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(figure.compareDocumentPosition(entity)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // The full evidence still follows in the body.
    const table = within(card).getByRole('table', { name: 'Evidence' });
    expect(within(table).getByRole('rowheader', { name: 'Wasted spend' })).toBeDefined();
  });

  it('shows no headline number when the frozen evidence selects none', async () => {
    const item = k1Item(null);
    await renderCard(<RecommendationCard item={item} />);

    const header = headerOf(item);
    expect(header.querySelector('.ds-card__figure')).toBeNull();
    expect(within(header).queryByText('Wasted spend')).toBeNull();
    expect(within(header).queryByText('$515.34')).toBeNull();
  });

  it('shows no headline number, and never another metric, when the key names none', async () => {
    const item = k1Item({ headline_metric: 'cost_per_conversion' });
    await renderCard(<RecommendationCard item={item} />);

    const header = headerOf(item);
    expect(header.querySelector('.ds-card__figure')).toBeNull();
    expect(within(header).queryByText('$515.34')).toBeNull();
  });

  it('renders an unavailable headline value as "unavailable", never 0', async () => {
    const item = k1Item({ headline_metric: 'wasted_spend' }, null);
    await renderCard(<RecommendationCard item={item} />);

    const figure = within(headerOf(item)).getByText('Wasted spend').parentElement;
    expect(figure?.textContent).toBe('Wasted spend unavailable');
    expect(figure?.textContent).not.toMatch(/\d/);
  });
});

// ---------------------------------------------------------------------------------------------
// Reason placement
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: reason placement', () => {
  it('prints the reason once, before the caller fields, by default', async () => {
    const item = c1Item();
    await renderCard(
      <RecommendationCard item={item}>
        <div>Section fields</div>
      </RecommendationCard>,
    );

    const card = cardOf(item);
    const reasons = within(card).getAllByText(item.reason);
    expect(reasons).toHaveLength(1);
    const fields = within(card).getByText('Section fields');
    expect(reasons[0]?.compareDocumentPosition(fields)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('prints the reason once, before the caller fields, with reasonPlacement "card"', async () => {
    const item = c1Item();
    await renderCard(
      <RecommendationCard item={item} reasonPlacement="card">
        <div>Section fields</div>
      </RecommendationCard>,
    );

    const card = cardOf(item);
    const reasons = within(card).getAllByText(item.reason);
    expect(reasons).toHaveLength(1);
    const fields = within(card).getByText('Section fields');
    expect(reasons[0]?.compareDocumentPosition(fields)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });

  it('prints no reason line of its own with reasonPlacement "fields", leaving the reason to the caller fields', async () => {
    const item = c1Item();
    const fieldText = `Reason: ${item.reason}`;
    await renderCard(
      <RecommendationCard item={item} reasonPlacement="fields">
        <div>{fieldText}</div>
      </RecommendationCard>,
    );

    const card = cardOf(item);
    // The card's own reason line is gone; the caller's field holds the only copy.
    expect(within(card).queryByText(item.reason)).toBeNull();
    expect((card.textContent ?? '').split(item.reason)).toHaveLength(2);
    // The body opens with the caller's fields, then the evidence, window chip and flags.
    const body = card.querySelector('.ds-card__body');
    expect(body?.textContent?.startsWith(fieldText)).toBe(true);
    const order = [
      within(card).getByText(fieldText),
      within(card).getByRole('table', { name: 'Evidence' }),
      within(card).getByText(formatWindowChip(item.evidence.window)),
      within(card).getByRole('list', { name: 'Flags' }),
    ];
    order.slice(1).forEach((node, index) => {
      expect(order[index]?.compareDocumentPosition(node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    });
  });

  it('still states a hold before the caller fields with reasonPlacement "fields"', async () => {
    const item = makeItem({
      current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    });
    await renderCard(
      <RecommendationCard item={item} reasonPlacement="fields">
        <div>Section fields</div>
      </RecommendationCard>,
    );

    const card = cardOf(item);
    expect(within(card).queryByText(item.reason)).toBeNull();
    const held = within(card).getByText('Resolve discrepancy first');
    expect(held.compareDocumentPosition(within(card).getByText('Section fields'))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });
});

// ---------------------------------------------------------------------------------------------
// Holds and historical items
// ---------------------------------------------------------------------------------------------

describe('RecommendationCard: holds', () => {
  it('states why a held item is held and links its recorded blocking discrepancy', async () => {
    const item = makeItem({
      held: true,
      held_reason: 'Resolve discrepancy first',
      evidence: makeEvidence({
        extra: {
          depends_on: [{ type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch }],
          blocking_discrepancies: [51],
        },
      }),
      current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(within(card).getByText('Resolve discrepancy first')).toBeDefined();
    // Rendered alone, the card has no run to find the C0 card in, so it links /decisions.
    const link = within(card).getByRole('link', { name: `${BLOCKING_DISCREPANCY_LINK_LABEL} 51` });
    expect(link.getAttribute('href')).toBe('/decisions#discrepancy-51');
    expect(within(card).queryByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL })).toBeNull();
  });

  it('links the discrepancy list, saying so, when no blocker can be named', async () => {
    const item = makeItem({
      current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    });
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    const link = within(card).getByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL });
    expect(link.getAttribute('href')).toBe(DISCREPANCY_ANCHOR);
    expect(
      within(card).queryByRole('link', { name: new RegExp(BLOCKING_DISCREPANCY_LINK_LABEL) }),
    ).toBeNull();
    expect(api.calls).toEqual([]);
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
    // The fixture records no blocker id, so the card links the discrepancy list.
    const item = pageItem(FIXTURE_ITEM_IDS.heldPause);
    await renderCard(<RecommendationCard item={item} />);

    const card = cardOf(item);
    expect(within(card).getByText('Resolve discrepancy first')).toBeDefined();
    expect(
      within(card)
        .getByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL })
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
    expect(
      within(card).queryByRole('link', { name: new RegExp(BLOCKING_DISCREPANCY_LINK_LABEL) }),
    ).toBeNull();
    expect(within(card).queryByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL })).toBeNull();

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
    // The loading button stays focusable (aria-disabled); the others are natively disabled.
    expect(snooze.getAttribute('aria-disabled')).toBe('true');
    cardButtons(item).forEach((button) => {
      expect(isInactive(button)).toBe(true);
      expect(button).toHaveProperty('disabled', button !== snooze);
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

  it('shows the generic text, never the message, of a failure that is not an ApiError', async () => {
    // Assembled at run time, so the secret scan never meets a literal secret assignment.
    const secret = `${['SESSION', 'SECRET'].join('_')}=${'9f3c'.repeat(8)}`;
    const internalPath = '/srv/ads_engine/.env';
    api.use({
      'POST /recommendations/:id/feedback': json(201, makeFeedback('done')),
    });
    const item = makeItem();
    const { queryClient } = await renderCard(<RecommendationCard item={item} />);
    // The POST succeeds, then the refetch `useFeedback` awaits rejects with a plain Error,
    // which TanStack Query makes the mutation's error.
    vi.spyOn(queryClient, 'invalidateQueries').mockRejectedValue(
      new Error(`ENOENT: no such file or directory, open '${internalPath}' (${secret})`),
    );

    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done }));

    const message = await within(cardOf(item)).findByText(
      'The action could not be recorded. Try again.',
    );
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(posts(api)).toHaveLength(1);
    expect(document.body.innerHTML).not.toContain(internalPath);
    expect(document.body.innerHTML).not.toContain(secret);
    expect(document.body.innerHTML).not.toContain('ENOENT');
  });

  it('keeps its action state with its item, never carrying it to another', async () => {
    const first = makeItem({ id: 61, action: 'Add "crm demo" as exact-match negative' });
    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('dismiss', { recommendation_id: first.id }),
      ),
    });
    // Another recommendation: the same rule on another search term.
    const second = makeItem({
      id: 62,
      action: 'Add "crm trial" as exact-match negative',
      entity: {
        source: 'google',
        type: 'search_term',
        id: '61000201|7700119|crm trial',
        name: 'crm trial',
      },
    });

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

    expect(await within(cardOf(item)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
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
    expect(within(cardOf(item)).queryByText(RECORDED_LINE_PATTERN)).toBeNull();
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

  it('locks every action on a 409 subject_retired from the form, after the dialog closes', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': apiError(409, 'subject_retired', 'The dataset is no longer active.'),
    });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const opener = within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL });
    opener.focus();
    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    // The form shows the refusal itself, and the card leaves focus to it while the dialog is
    // open.
    expect(await within(dialog).findByText(HISTORICAL_LABEL)).toBeDefined();
    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true);
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    // The lock is the card's own now: the same message, focused, and every action disabled.
    const message = within(cardOf(item)).getByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(document.activeElement?.getAttribute('tabindex')).toBe('-1');
    expect(buttonNames(item)).toEqual([RECORD_DECISION_LABEL, 'Dismiss', 'Snooze for 7 days']);
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
      fireEvent.click(button);
    }
    // Reopening, which would start a fresh, unlocked form, is impossible.
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api)).toHaveLength(1);
    expect(within(cardOf(item)).queryByRole('alert')).toBeNull();
  });

  it('still locks the card when the 409 arrives after the dialog was closed mid-save', async () => {
    let release: (response: MockResponse) => void = () => {
      throw new Error('the pending decision response was never created');
    };
    const pending = new Promise<MockResponse>((resolve) => {
      release = resolve;
    });
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': () => pending,
    });
    const item = c1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });
    // Closing the dialog unmounts the form while its request runs; the card holds every action
    // until the save settles, so the dialog cannot be reopened meanwhile.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    const opener = within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL });
    expect(opener.getAttribute('aria-busy')).toBe('true');
    // The loading opener stays focusable (aria-disabled); the others are natively disabled.
    for (const button of cardButtons(item)) {
      expect(isInactive(button)).toBe(true);
      expect(button).toHaveProperty('disabled', button !== opener);
    }

    release(apiError(409, 'subject_retired', 'The dataset is no longer active.'));

    const message = await within(cardOf(item)).findByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(opener.getAttribute('aria-busy')).toBeNull();
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
      fireEvent.click(button);
    }
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api)).toHaveLength(1);
  });

  it('announces a recorded decision in the provider region, outside the card', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 811,
          origin: 'user',
          status: 'paused',
          patch: { status: 'paused' },
        }),
      ),
    });
    const item = c1Item();
    await renderCard(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
      </ActionOutcomeProvider>,
    );

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    const line = await within(cardOf(item)).findByText(PAUSED_RECORDED_LINE);
    // Inside a provider the card's own line is visible but no second live region.
    expect(line.getAttribute('role')).toBeNull();
    expect(outcomeRegion().textContent).toBe(
      `${PAUSED_RECORDED_LINE} (Generic CRM — US)`,
    );
  });

  it('refuses to reopen the dialog while its save runs, then reports it and opens a fresh form', async () => {
    let release: (response: MockResponse) => void = () => {
      throw new Error('the pending decision response was never created');
    };
    const pending = new Promise<MockResponse>((resolve) => {
      release = resolve;
    });
    const saved = genericCrmDecision({
      id: 811,
      origin: 'user',
      status: 'paused',
      patch: { status: 'paused' },
    });
    let saves = 0;
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      // The first save waits for the test; any later one is answered at once.
      'POST /decisions': () => {
        saves += 1;
        return saves === 1 ? pending : json(201, saved);
      },
    });
    const item = c1Item();
    await renderCard(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
      </ActionOutcomeProvider>,
    );

    const opener = within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL });
    // A click focuses the button in a browser; fireEvent does not.
    opener.focus();
    const first = await openDecisionDialog(item);
    const noteText = 'Pause until the CRM sync is fixed';
    fireEvent.change(within(first).getByRole('textbox', { name: 'Note' }), {
      target: { value: noteText },
    });
    fireEvent.click(within(first).getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });
    // Closing the dialog unmounts the form while its request runs.
    fireEvent.click(within(first).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    // Until the save settles, nothing on the card reopens the dialog or posts again. The closed
    // dialog returned focus to the loading "Record decision", which keeps it (aria-disabled).
    expect(opener.getAttribute('aria-busy')).toBe('true');
    expect(document.activeElement).toBe(opener);
    expect(within(cardOf(item)).getByText(DECISION_SAVING_MESSAGE)).toBeDefined();
    for (const button of cardButtons(item)) {
      expect(isInactive(button)).toBe(true);
      expect(button).toHaveProperty('disabled', button !== opener);
      fireEvent.click(button);
    }
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api)).toHaveLength(1);

    release(json(201, saved));

    // The late save is reported on the card and in the provider's region...
    expect(await within(cardOf(item)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
    expect(within(cardOf(item)).queryByText(DECISION_SAVING_MESSAGE)).toBeNull();
    expect(outcomeRegion().textContent).toBe(
      `${PAUSED_RECORDED_LINE} (Generic CRM — US)`,
    );
    // ...the actions are usable again, and focus is still on "Record decision".
    await waitFor(() => {
      expect(document.activeElement).toBe(opener);
    });
    expect(opener.getAttribute('aria-busy')).toBeNull();
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', false);
    }

    // Reopening now starts a fresh form, without the closed one's note, whose save closes it.
    const second = await openDecisionDialog(item);
    expect(within(second).getByRole('textbox', { name: 'Note' })).toHaveProperty('value', '');
    fireEvent.click(within(second).getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    const decisionPost = {
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
      patch: { status: 'paused' },
      recommendation_id: FIXTURE_ITEM_IDS.c1,
    };
    expect(posts(api).map((call) => call.body)).toEqual([
      { ...decisionPost, note: noteText },
      decisionPost,
    ]);
  });

  it.each([
    [
      'a 422',
      apiError(422, 'invalid_patch', 'A daily budget needs whole cents.'),
      'A daily budget needs whole cents.',
    ],
    [
      'a 409 platform_state_unknown',
      apiError(409, 'platform_state_unknown', 'No entity sync has reported Generic CRM — US yet.'),
      'No entity sync has reported Generic CRM — US yet.',
    ],
    [
      'a 429',
      apiError(429, 'rate_limited', 'Too many requests. Try again shortly.'),
      'Too many requests. Try again shortly.',
    ],
    [
      'a server failure',
      html(502, '<html><body>Bad Gateway</body></html>'),
      'The server is temporarily unavailable. Try again shortly.',
    ],
  ] as const)(
    'shows %s that ends after the dialog was closed on the card, focused',
    async (_failure, response, text) => {
      let release: (response: MockResponse) => void = () => {
        throw new Error('the pending decision response was never created');
      };
      const pending = new Promise<MockResponse>((resolve) => {
        release = resolve;
      });
      api.use({
        'GET /decisions': json(200, [genericCrmDecision()]),
        'POST /decisions': () => pending,
      });
      const item = c1Item();
      await renderCard(<RecommendationCard item={item} />);

      const opener = within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL });
      opener.focus();
      const dialog = await openDecisionDialog(item);
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));
      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      // The closed dialog returned focus to the loading opener, which keeps it.
      expect(opener.getAttribute('aria-disabled')).toBe('true');
      expect(opener).toHaveProperty('disabled', false);
      expect(document.activeElement).toBe(opener);
      // Outside a provider the card's own polite line says the save is still running.
      expect(within(cardOf(item)).getByRole('status').textContent).toBe(DECISION_SAVING_MESSAGE);

      release(response);

      // The form that would have shown the failure is gone, so the card shows it, focused.
      const message = await within(cardOf(item)).findByText(text);
      await waitFor(() => {
        expect(document.activeElement?.contains(message)).toBe(true);
      });
      expect(document.activeElement?.getAttribute('tabindex')).toBe('-1');
      expect(within(cardOf(item)).queryByRole('alert')).toBeNull();
      expect(within(cardOf(item)).queryByText(HISTORICAL_LABEL)).toBeNull();
      expect(within(cardOf(item)).queryByText(DECISION_SAVING_MESSAGE)).toBeNull();
      // Unlike a retirement, the failure leaves every action usable, and nothing posted twice.
      for (const button of cardButtons(item)) {
        expect(button).toHaveProperty('disabled', false);
      }
      expect(posts(api)).toHaveLength(1);

      // A new opening clears the failure.
      await openDecisionDialog(item);
      expect(within(cardOf(item)).queryByText(text)).toBeNull();
    },
  );

  it("leaves focus where the user moved it when a closed dialog's save ends", async () => {
    let release: (response: MockResponse) => void = () => {
      throw new Error('the pending decision response was never created');
    };
    const pending = new Promise<MockResponse>((resolve) => {
      release = resolve;
    });
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': () => pending,
    });
    const item = c1Item();
    await renderCard(
      <>
        <Button>Elsewhere</Button>
        <RecommendationCard item={item} />
      </>,
    );

    const opener = within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL });
    opener.focus();
    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    elsewhere.focus();

    release(json(201, genericCrmDecision({ id: 811, origin: 'user', status: 'paused' })));

    expect(await within(cardOf(item)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
    await waitFor(() => {
      expect(opener).toHaveProperty('disabled', false);
    });
    expect(document.activeElement).toBe(elsewhere);
  });
});

// ---------------------------------------------------------------------------------------------
// A newer run's item of the same recommendation
// ---------------------------------------------------------------------------------------------

/**
 * Renders the card of `initial` from state and returns `show`, which puts another item in its
 * place, as the refetched page of a newer run does.
 */
async function renderReplaceableCard(initial: Item): Promise<(next: Item) => void> {
  const control: { show?: (next: Item) => void } = {};
  function Host() {
    const [item, setItem] = useState(initial);
    control.show = setItem;
    return <RecommendationCard item={item} />;
  }
  await renderCard(<Host />);
  return (next) => {
    act(() => {
      control.show?.(next);
    });
  };
}

/** `item` as a newer run recomputes it: the same recommendation under a new id. */
function newerRun(item: Item): Item {
  return { ...item, id: item.id + 1000 };
}

/** A response a test releases itself, for a request it keeps pending until then. */
function heldResponse(): {
  pending: Promise<MockResponse>;
  release: (response: MockResponse) => void;
} {
  let release: (response: MockResponse) => void = () => {
    throw new Error('the pending response was never created');
  };
  const pending = new Promise<MockResponse>((resolve) => {
    release = resolve;
  });
  return { pending, release };
}

describe('RecommendationActions: a newer run of the same recommendation', () => {
  it('keeps its buttons, and focus on them, when the same recommendation arrives under a new id', async () => {
    const item = makeItem({ id: 61 });
    const show = await renderReplaceableCard(item);
    const dismiss = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss });
    dismiss.focus();

    const next = newerRun(item);
    show(next);

    expect(cardOf(next).id).toBe('item-1061');
    expect(within(cardOf(next)).getByRole('button', { name: FEEDBACK_LABELS.dismiss })).toBe(
      dismiss,
    );
    expect(document.activeElement).toBe(dismiss);
    cardButtons(next).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });
  });

  it('carries a recorded feedback and a failure, with the focus on it, over to the newer item', async () => {
    api.use({
      'POST /recommendations/:id/feedback': apiError(
        422,
        'feedback_not_allowed',
        'Feedback is not accepted for this item.',
      ),
    });
    const item = makeItem({ id: 61 });
    const show = await renderReplaceableCard(item);
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done }));
    const message = await within(cardOf(item)).findByText(
      'Feedback is not accepted for this item.',
    );
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    const focused = document.activeElement;

    const next = newerRun(item);
    show(next);

    expect(within(cardOf(next)).getByText('Feedback is not accepted for this item.')).toBe(message);
    expect(document.activeElement).toBe(focused);

    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('snooze', { recommendation_id: next.id }),
      ),
    });
    fireEvent.click(within(cardOf(next)).getByRole('button', { name: FEEDBACK_LABELS.snooze }));
    expect(await within(cardOf(next)).findByText('Snoozed')).toBeDefined();
    // The newer item posts under its own id.
    expect(posts(api).map((call) => call.path)).toEqual([
      '/recommendations/61/feedback',
      '/recommendations/1061/feedback',
    ]);

    // Recorded on this run's item, the snooze holds the recommendation's actions in the next.
    const latest = newerRun(next);
    show(latest);
    expect(within(cardOf(latest)).getByText('Snoozed')).toBeDefined();
    cardButtons(latest).forEach((button) => {
      expect(button).toHaveProperty('disabled', true);
    });
  });

  it('lifts a retirement learned on the old id, keeping its banner and the focus on it until the next action', async () => {
    api.use({
      'POST /recommendations/:id/feedback': apiError(
        409,
        'subject_retired',
        'Computed from data that has since been replaced.',
      ),
    });
    const item = makeItem({ id: 61 });
    const show = await renderReplaceableCard(item);
    const markDone = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done });
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    const message = await within(cardOf(item)).findByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    const focused = document.activeElement;
    cardButtons(item).forEach((button) => {
      expect(button).toHaveProperty('disabled', true);
    });

    // The newer item, computed from current data, is judged by its own overlay: the lock lifts,
    // while the refusal's explanation, like any failure text, stays with the focus on it.
    const next = newerRun(item);
    show(next);

    cardButtons(next).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });
    expect(within(cardOf(next)).getByRole('button', { name: FEEDBACK_LABELS.done })).toBe(
      markDone,
    );
    expect(within(cardOf(next)).getByText(HISTORICAL_LABEL)).toBe(message);
    expect(document.activeElement).toBe(focused);
    expect(document.activeElement?.contains(message)).toBe(true);

    // The next action starts afresh, and clears the explanation.
    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('done', { recommendation_id: next.id }),
      ),
    });
    fireEvent.click(markDone);
    expect(await within(cardOf(next)).findByText('Marked done')).toBeDefined();
    expect(within(cardOf(next)).queryByText(HISTORICAL_LABEL)).toBeNull();
    expect(posts(api).map((call) => call.path)).toEqual([
      '/recommendations/61/feedback',
      '/recommendations/1061/feedback',
    ]);
  });

  it('keeps a newer historical item locked, with the banner and the focus on it in place', async () => {
    api.use({
      'POST /recommendations/:id/feedback': apiError(409, 'subject_retired', 'Replaced.'),
    });
    const item = makeItem({ id: 61 });
    const show = await renderReplaceableCard(item);
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    const message = await within(cardOf(item)).findByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    const focused = document.activeElement;

    const next = { ...newerRun(item), current: historicalOverlay() };
    show(next);

    // Locked by its own overlay, whose badge in the body states it beside the footer's banner.
    cardButtons(next).forEach((button) => {
      expect(button).toHaveProperty('disabled', true);
    });
    expect(within(cardOf(next)).getAllByText(HISTORICAL_LABEL)).toHaveLength(2);
    expect(message.isConnected).toBe(true);
    expect(document.activeElement).toBe(focused);
    expect(document.activeElement?.contains(message)).toBe(true);
  });

  it('shows the retirement message, never locking the newer item, on a refusal for the item it replaced', async () => {
    const held = heldResponse();
    api.use({ 'POST /recommendations/:id/feedback': () => held.pending });
    const item = makeItem({ id: 61 });
    const show = await renderReplaceableCard(item);
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });

    const next = newerRun(item);
    show(next);
    held.release(apiError(409, 'subject_retired', 'Replaced.'));

    const message = await within(cardOf(next)).findByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(
      within(cardOf(next)).queryByText('The action could not be recorded. Try again.'),
    ).toBeNull();
    expect(within(cardOf(next)).queryByText('Replaced.')).toBeNull();
    cardButtons(next).forEach((button) => {
      expect(button).toHaveProperty('disabled', false);
    });
  });

  it('shows the retirement message, never locking the newer item, on a decision refusal after it replaced the item', async () => {
    const held = heldResponse();
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': () => held.pending,
    });
    const item = c1Item();
    const show = await renderReplaceableCard(item);
    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));

    const next = newerRun(item);
    show(next);
    held.release(apiError(409, 'subject_retired', 'The dataset is no longer active.'));

    const message = await within(cardOf(next)).findByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    expect(
      within(cardOf(next)).queryByText('The action could not be recorded. Try again.'),
    ).toBeNull();
    await waitFor(() => {
      cardButtons(next).forEach((button) => {
        expect(button).toHaveProperty('disabled', false);
      });
    });
  });

  it('attributes a decision refusal to the recommendation the open form posted, not the newer item', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': apiError(409, 'subject_retired', 'The dataset is no longer active.'),
    });
    const item = c1Item();
    const show = await renderReplaceableCard(item);
    const dialog = await openDecisionDialog(item);

    // A newer run's item of the same recommendation and decision subject arrives while the
    // dialog opened on the older one is still open, and the user saves from it.
    const next = newerRun(item);
    show(next);
    expect(screen.getByRole('dialog', { name: RECORD_DECISION_LABEL })).toBe(dialog);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    expect(await within(dialog).findByText(HISTORICAL_LABEL)).toBeDefined();
    const decisionBody = {
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
      patch: { status: 'paused' },
    };
    expect(posts(api).map((call) => call.body)).toEqual([
      { ...decisionBody, recommendation_id: item.id },
    ]);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    // The refusal was the older item's: the item shown is not locked, and the card explains it.
    const message = within(cardOf(next)).getByText(HISTORICAL_LABEL);
    await waitFor(() => {
      expect(document.activeElement?.contains(message)).toBe(true);
    });
    await waitFor(() => {
      cardButtons(next).forEach((button) => {
        expect(button).toHaveProperty('disabled', false);
      });
    });

    // Reopened, the dialog belongs to the item shown, and its save posts that item's id.
    api.use({
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 811,
          origin: 'user',
          status: 'paused',
          patch: { status: 'paused' },
        }),
      ),
    });
    const reopened = await openDecisionDialog(next);
    expect(within(cardOf(next)).queryByText(HISTORICAL_LABEL)).toBeNull();
    fireEvent.click(within(reopened).getByRole('button', { name: 'Save decision' }));

    expect(await within(cardOf(next)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api).map((call) => call.body)).toEqual([
      { ...decisionBody, recommendation_id: item.id },
      { ...decisionBody, recommendation_id: next.id },
    ]);
  });

  it('keeps an open "Record decision" dialog, its edits and focus across a new id', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 811,
          origin: 'user',
          status: 'paused',
          patch: { status: 'paused' },
        }),
      ),
    });
    const item = c1Item();
    const show = await renderReplaceableCard(item);
    const dialog = await openDecisionDialog(item);
    const noteText = 'Pause until the CRM sync is fixed';
    const note = within(dialog).getByRole('textbox', { name: 'Note' });
    fireEvent.change(note, { target: { value: noteText } });
    note.focus();

    const next = newerRun(item);
    show(next);

    expect(screen.getByRole('dialog', { name: RECORD_DECISION_LABEL })).toBe(dialog);
    expect(within(dialog).getByRole('textbox', { name: 'Note' })).toBe(note);
    expect(note).toHaveProperty('value', noteText);
    expect(document.activeElement).toBe(note);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    expect(await within(cardOf(next)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
    expect(screen.queryByRole('dialog')).toBeNull();
    // The form posts the recommendation it was opened for, whose subject is the same.
    expect(posts(api).map((call) => call.body)).toEqual([
      {
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
        patch: { status: 'paused' },
        recommendation_id: item.id,
        note: noteText,
      },
    ]);
  });

  it('closes the dialog when the newer item has no decision subject, focusing its first action', async () => {
    api.use({ 'GET /decisions': json(200, [genericCrmDecision()]) });
    const item = c1Item();
    const show = await renderReplaceableCard(item);
    within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL }).focus();
    await openDecisionDialog(item);

    const next = { ...newerRun(item), decision_subject: null };
    show(next);

    expect(screen.queryByRole('dialog')).toBeNull();
    const markDone = within(cardOf(next)).getByRole('button', { name: FEEDBACK_LABELS.done });
    await waitFor(() => {
      expect(document.activeElement).toBe(markDone);
    });
    expect(buttonNames(next)).toEqual(['Mark done', 'Dismiss', 'Snooze for 7 days']);
  });
});


// ---------------------------------------------------------------------------------------------
// Outcome announcements and focus recovery
// ---------------------------------------------------------------------------------------------

/** The keyword cards of the default fixture page, in page order: block, expand, review. */
const KEYWORD_IDS = [FIXTURE_ITEM_IDS.k1, FIXTURE_ITEM_IDS.k2, FIXTURE_ITEM_IDS.k3] as const;

/**
 * The polite region of the `ActionOutcomeProvider`: the one `role="status"` element outside
 * every card.
 */
function outcomeRegion(): HTMLElement {
  const regions = screen
    .getAllByRole('status')
    .filter((region) => region.closest('article') === null);
  expect(regions).toHaveLength(1);
  const [region] = regions;
  if (region === undefined) {
    throw new Error('no outcome region outside the cards');
  }
  return region;
}

/** The confirmation the provider announces for `kind` on `item`. */
function feedbackOutcome(kind: FeedbackKind, item: Item): string {
  const name = item.entity.name;
  expect(name).not.toBeNull();
  return `${FEEDBACK_STATUS_MESSAGES[kind]}: ${item.action} (${String(name)})`;
}

/**
 * Serves the default fixture page so that, as the API's overlay does, every feedback the stub
 * has accepted hides its item from the next read (`current.hidden_by`).
 *
 * @param respond - answers each feedback post; a 201 with the posted kind when omitted
 */
function serveLiveOverlay(respond?: () => Promise<MockResponse>): void {
  const hidden = new Map<number, FeedbackKind>();
  api.use({
    'GET /recommendations': () => {
      const page = makeRecommendationsPage();
      for (const [id, kind] of hidden) {
        findPageItem(page, id).current.hidden_by = kind;
      }
      return json(200, page);
    },
    'POST /recommendations/:id/feedback': async (call) => {
      const id = Number(call.params.id);
      const { kind } = call.body as { kind: FeedbackKind };
      const response =
        respond === undefined
          ? json(201, makeFeedback(kind, { recommendation_id: id }))
          : await respond();
      if (response.status === 201) {
        hidden.set(id, kind);
      }
      return response;
    },
  });
}

/**
 * The cards of `ids` from the live recommendations query, in order, less those the overlay
 * hides, as a section drops them: inside one `PageShell.Section` (an `<h2>`) and an
 * `ActionOutcomeProvider`, after an "Elsewhere" button outside both.
 */
function LiveCards({ ids }: { ids: readonly number[] }) {
  const { data } = useRecommendations();
  const items =
    data === undefined
      ? []
      : ids
          .map((id) => findPageItem(data, id))
          .filter((item) => item.current.hidden_by === null);
  return (
    <Stack>
      <Button>Elsewhere</Button>
      <ActionOutcomeProvider>
        <PageShell.Section id="section-keywords" title="Keywords to adjust">
          <Stack>
            {items.map((item) => (
              <RecommendationCard key={item.id} item={item} />
            ))}
          </Stack>
        </PageShell.Section>
      </ActionOutcomeProvider>
    </Stack>
  );
}

/**
 * {@link LiveCards} with the cards in a group: a `PageShell.Section` (an `<h3>`) inside the
 * `<h2>` section, rendered only while it has a card, as a section drops an emptied group.
 */
function LiveGroup({ ids }: { ids: readonly number[] }) {
  const { data } = useRecommendations();
  const items =
    data === undefined
      ? []
      : ids
          .map((id) => findPageItem(data, id))
          .filter((item) => item.current.hidden_by === null);
  return (
    <ActionOutcomeProvider>
      <PageShell.Section id="section-keywords" title="Keywords to adjust">
        {items.length === 0 ? null : (
          <PageShell.Section id="group-keywords-block" title="Block" headingLevel={3}>
            {items.map((item) => (
              <RecommendationCard key={item.id} item={item} headingLevel={4} />
            ))}
          </PageShell.Section>
        )}
      </PageShell.Section>
    </ActionOutcomeProvider>
  );
}

/** Renders {@link LiveCards} and waits for its cards. */
async function renderLiveCards(ids: readonly number[]): Promise<Item[]> {
  const page = makeRecommendationsPage();
  const items = ids.map((id) => findPageItem(page, id));
  await renderCard(<LiveCards ids={ids} />);
  await screen.findByRole('article', { name: items[0]?.action ?? '' });
  return items;
}

/** Presses a card's action as a keyboard user does: focus first, then activate. */
function press(item: Item, label: string): void {
  const button = within(cardOf(item)).getByRole('button', { name: label });
  button.focus();
  fireEvent.click(button);
}

describe('RecommendationActions: outcome announcement and focus after feedback', () => {
  it('starts with an empty provider region before the cards', async () => {
    serveLiveOverlay();
    const [first] = await renderLiveCards(KEYWORD_IDS);
    if (first === undefined) {
      throw new Error('no card');
    }

    const region = outcomeRegion();
    expect(region.textContent).toBe('');
    expect(region.childNodes).toHaveLength(0);
    expect(region.compareDocumentPosition(cardOf(first))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    // Inside the provider no card renders a live region of its own.
    expect(within(cardOf(first)).queryByRole('status')).toBeNull();
  });

  it.each(FEEDBACK_KINDS)(
    'announces "%s" outside the removed card and focuses the next card',
    async (kind) => {
      serveLiveOverlay();
      const [first, second] = await renderLiveCards(KEYWORD_IDS);
      if (first === undefined || second === undefined) {
        throw new Error('missing cards');
      }

      press(first, FEEDBACK_LABELS[kind]);

      // The refetch carries hidden_by, so the card leaves the page; its confirmation does not.
      await waitFor(() => {
        expect(screen.queryByRole('article', { name: first.action })).toBeNull();
      });
      expect(outcomeRegion().textContent).toBe(feedbackOutcome(kind, first));
      expect(outcomeRegion().closest('section')).toBeNull();
      const next = within(cardOf(second)).getByRole('button', { name: FEEDBACK_LABELS.done });
      await waitFor(() => {
        expect(document.activeElement).toBe(next);
      });
    },
  );

  it('focuses the previous card after the last one leaves, then the one before it', async () => {
    serveLiveOverlay();
    const [first, second, third] = await renderLiveCards(KEYWORD_IDS);
    if (first === undefined || second === undefined || third === undefined) {
      throw new Error('missing cards');
    }

    press(third, FEEDBACK_LABELS.snooze);

    await waitFor(() => {
      expect(screen.queryByRole('article', { name: third.action })).toBeNull();
    });
    const previous = within(cardOf(second)).getByRole('button', { name: FEEDBACK_LABELS.done });
    await waitFor(() => {
      expect(document.activeElement).toBe(previous);
    });
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('snooze', third));

    press(second, FEEDBACK_LABELS.dismiss);

    // The removed third card is skipped; the nearest card still on the page takes focus.
    const remaining = within(cardOf(first)).getByRole('button', { name: FEEDBACK_LABELS.done });
    await waitFor(() => {
      expect(document.activeElement).toBe(remaining);
    });
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('dismiss', second));
  });

  it('announces a repeated message as a new node, so it is read again', async () => {
    /** Announces one fixed text through the nearest provider. */
    function Announcer() {
      const announce = useActionOutcome();
      return (
        <Button
          onClick={() => {
            announce?.('Dismissed: Pause (Generic CRM — US)');
          }}
        >
          Announce
        </Button>
      );
    }
    await renderCard(
      <ActionOutcomeProvider>
        <Announcer />
      </ActionOutcomeProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Announce' }));
    const first = outcomeRegion().firstChild;
    expect(outcomeRegion().textContent).toBe('Dismissed: Pause (Generic CRM — US)');

    fireEvent.click(screen.getByRole('button', { name: 'Announce' }));
    expect(outcomeRegion().textContent).toBe('Dismissed: Pause (Generic CRM — US)');
    expect(outcomeRegion().childNodes).toHaveLength(1);
    expect(outcomeRegion().firstChild).not.toBe(first);
    // An outcome is polite, never an alert (AAP 0.5.5 reserves alert for discrepancies).
    expect(screen.queryAllByRole('alert')).toEqual([]);
  });

  it('focuses the section heading when the only card leaves', async () => {
    serveLiveOverlay();
    const [only] = await renderLiveCards([FIXTURE_ITEM_IDS.k1]);
    if (only === undefined) {
      throw new Error('missing card');
    }

    press(only, FEEDBACK_LABELS.done);

    const heading = screen.getByRole('heading', { level: 2, name: 'Keywords to adjust' });
    await waitFor(() => {
      expect(document.activeElement).toBe(heading);
    });
    expect(heading.getAttribute('tabindex')).toBe('-1');
    expect(screen.queryByRole('article')).toBeNull();
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('done', only));
  });

  it('waits for the refetched page: an emptied group leaves, so its section heading takes focus', async () => {
    serveLiveOverlay();
    const only = pageItem(FIXTURE_ITEM_IDS.k1);
    await renderCard(<LiveGroup ids={[only.id]} />);
    await screen.findByRole('article', { name: only.action });

    press(only, FEEDBACK_LABELS.dismiss);

    // The group's <h3> leaves the page with its last card, so the enclosing <h2> takes focus.
    const heading = screen.getByRole('heading', { level: 2, name: 'Keywords to adjust' });
    await waitFor(() => {
      expect(document.activeElement).toBe(heading);
    });
    expect(screen.queryByRole('heading', { level: 3 })).toBeNull();
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('dismiss', only));
  });

  it('leaves focus where the user moved it while the feedback was pending', async () => {
    let release: (response: MockResponse) => void = () => {
      throw new Error('the pending feedback response was never created');
    };
    const pending = new Promise<MockResponse>((resolve) => {
      release = resolve;
    });
    serveLiveOverlay(() => pending);
    const [first] = await renderLiveCards(KEYWORD_IDS);
    if (first === undefined) {
      throw new Error('missing card');
    }

    press(first, FEEDBACK_LABELS.dismiss);
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    elsewhere.focus();
    release(json(201, makeFeedback('dismiss', { recommendation_id: first.id })));

    await waitFor(() => {
      expect(screen.queryByRole('article', { name: first.action })).toBeNull();
    });
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('dismiss', first));
    // The announcement proves the feedback's success continuation has run, and that continuation
    // queues the zero-delay focus-recovery timer right after announcing. The zero-delay timer
    // below is queued after it, and equal-delay timers run in the order they were queued, so
    // once it fires the recovery has run: it found focus moved away from the card and left it.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(document.activeElement).toBe(elsewhere);
  });

  it('keeps the recorded card locked with its status line while it stays on the page', async () => {
    api.use({ 'POST /recommendations/:id/feedback': json(201, makeFeedback('dismiss')) });
    const item = pageItem(FIXTURE_ITEM_IDS.k1);
    await renderCard(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
      </ActionOutcomeProvider>,
    );

    press(item, FEEDBACK_LABELS.dismiss);

    const line = await within(cardOf(item)).findByText(FEEDBACK_STATUS_MESSAGES.dismiss);
    expect(line.getAttribute('role')).toBeNull();
    expect(outcomeRegion().textContent).toBe(feedbackOutcome('dismiss', item));
    // With no other card, heading or main to move to, focus stays on the pressed Dismiss, which
    // is aria-disabled until focus leaves it; the other actions are natively disabled.
    const dismiss = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss });
    expect(document.activeElement).toBe(dismiss);
    expect(dismiss.getAttribute('aria-disabled')).toBe('true');
    for (const button of cardButtons(item)) {
      expect(isInactive(button)).toBe(true);
      expect(button).toHaveProperty('disabled', button !== dismiss);
    }
  });
});

// ---------------------------------------------------------------------------------------------
// How a confirmation names the card's entity
// ---------------------------------------------------------------------------------------------

/** A CR1 card's variant pair, which is also its entity's name. */
const CR1_PAIR = 'Is your EMEA pipeline on track? vs Seven in ten EMEA deals start with content';

/** A CR1 action, which begins with the variant pair its entity is named after. */
const CR1_ACTION =
  `${CR1_PAIR}: Is your EMEA pipeline on track? ahead, not yet distinguishable (adjusted p = ` +
  '0.21); about 41,800 more impressions per variant needed at the observed difference';

/**
 * {@link makeItem}'s default item, which offers "Mark done", with `action` and an entity named
 * `name`, of `type` (a CR1 creative pair unless given), so only the parts a confirmation names
 * vary.
 */
function namedItem(action: string, name: string | null, type = 'creative_pair'): Item {
  return makeItem({
    action,
    entity: { source: 'linkedin', type, id: '990001101|990001102', name },
  });
}

/**
 * Renders `item` alone inside an `ActionOutcomeProvider`, records "Mark done" on it, and waits
 * for the card's own status line, by which time the provider's region holds the confirmation.
 *
 * @returns the provider's region
 */
async function markDoneOutcome(item: Item): Promise<HTMLElement> {
  api.use({
    'POST /recommendations/:id/feedback': json(
      201,
      makeFeedback('done', { recommendation_id: item.id }),
    ),
  });
  await renderCard(
    <ActionOutcomeProvider>
      <RecommendationCard item={item} />
    </ActionOutcomeProvider>,
  );
  // The only card, found by role alone because a blank action leaves it without a name.
  const card = screen.getByRole('article');
  const button = within(card).getByRole('button', { name: FEEDBACK_LABELS.done });
  button.focus();
  fireEvent.click(button);
  await within(card).findByText(FEEDBACK_STATUS_MESSAGES.done);
  return outcomeRegion();
}

describe('RecommendationActions: the confirmation names the entity once', () => {
  it('leaves out the entity name the action already contains, so a CR1 pair reads once', async () => {
    const region = await markDoneOutcome(namedItem(CR1_ACTION, CR1_PAIR));

    expect(region.textContent).toBe(`Marked done: ${CR1_ACTION}`);
    expect(region.textContent).not.toContain(`(${CR1_PAIR})`);
  });

  it('compares the trimmed name with the trimmed action', async () => {
    const region = await markDoneOutcome(namedItem(`  ${CR1_ACTION} `, ` ${CR1_PAIR}  `));

    expect(region.textContent).toBe(`Marked done: ${CR1_ACTION}`);
  });

  it('leaves out a creative pair the action names leader first, in the other order', async () => {
    // The pair is named in its own order; the action names the observed leader first.
    const action =
      'The Attribution Playbook vs Stop guessing which channel works: The Attribution ' +
      'Playbook ahead, not yet distinguishable (adjusted p = 0.1019)';
    const region = await markDoneOutcome(
      namedItem(action, 'Stop guessing which channel works vs The Attribution Playbook'),
    );

    expect(region.textContent).toBe(`Marked done: ${action}`);
  });

  it.each([
    [
      'a creative pair the action names one variant of',
      'Pause The Attribution Playbook',
      'Stop guessing which channel works vs The Attribution Playbook',
      'creative_pair',
    ],
    // Only a creative pair's name lists variants; any other name must appear whole.
    [
      'another entity whose name parts the action contains apart',
      'Double down: increase spend on Them and Us',
      'Us vs Them',
      'campaign',
    ],
  ])('keeps the name the action does not state whole (%s)', async (_label, action, name, type) => {
    const region = await markDoneOutcome(namedItem(action, name, type));

    expect(region.textContent).toBe(`Marked done: ${action} (${name})`);
  });

  it.each([
    [
      'a K2 keyword',
      'Add as phrase-match keyword in Attribution software',
      'marketing attribution software',
      'search_term',
    ],
    [
      'a W1 white paper',
      'Double down: increase spend on Benchmarks - Lead gen (NA)',
      'Paid Media Benchmarks 2026',
      'content_asset',
    ],
    // Case-sensitive: "CRM Software" in the action does not contain the name "crm software".
    [
      'a name in another case',
      'Add as phrase-match keyword in CRM Software',
      'crm software',
      'search_term',
    ],
  ])(
    'keeps the entity name the action does not contain (%s)',
    async (_label, action, name, type) => {
      const region = await markDoneOutcome(namedItem(action, name, type));

      expect(region.textContent).toBe(`Marked done: ${action} (${name})`);
    },
  );

  it('states the action alone when the item carries no entity name', async () => {
    const region = await markDoneOutcome(namedItem('Add as exact-match negative', null));

    expect(region.textContent).toBe('Marked done: Add as exact-match negative');
  });

  it('states the entity name alone when the action is blank', async () => {
    const region = await markDoneOutcome(namedItem('   ', 'crm demo'));

    expect(region.textContent).toBe('Marked done: crm demo');
  });

  it('states the status alone when the item carries neither an action nor a name', async () => {
    const region = await markDoneOutcome(namedItem('', null));

    expect(region.textContent).toBe('Marked done');
  });

  it("names the entity after a recorded decision's state, even when the action contains it", async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 815,
          origin: 'recommendation',
          status: 'paused',
          patch: { status: 'paused' },
          recommendation_id: FIXTURE_ITEM_IDS.c1,
        }),
      ),
    });
    const item = { ...c1Item(), action: 'Pause Generic CRM — US' };
    await renderCard(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
      </ActionOutcomeProvider>,
    );

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    expect(await within(cardOf(item)).findByText(PAUSED_RECORDED_LINE)).toBeDefined();
    // The state names no entity, so the name still follows it.
    expect(outcomeRegion().textContent).toBe(`${PAUSED_RECORDED_LINE} (Generic CRM — US)`);
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

// ---------------------------------------------------------------------------------------------
// Double submits
// ---------------------------------------------------------------------------------------------

// Two presses before React renders again, as a double click or a script's `click(); click();`
// delivers them: TanStack Query reports the first request pending only from a timer after the
// press, so the render-time `disabled` state cannot stop the second one. Each pair is fired back
// to back, with nothing awaited between the two.
describe('RecommendationActions: double submits', () => {
  /** Waits until the card's feedback has settled: no button busy, every action disabled. */
  async function waitForRecorded(item: Item): Promise<void> {
    await waitFor(() => {
      for (const button of cardButtons(item)) {
        expect(button.getAttribute('aria-busy')).toBeNull();
        expect(button).toHaveProperty('disabled', true);
      }
    });
  }

  it.each(FEEDBACK_KINDS)(
    'posts "%s" once when it is pressed twice within one frame',
    async (kind) => {
      const item = makeItem();
      api.use({
        'POST /recommendations/:id/feedback': json(
          201,
          makeFeedback(kind, { recommendation_id: item.id }),
        ),
      });
      await renderCard(<RecommendationCard item={item} />);
      const button = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS[kind] });

      fireEvent.click(button);
      fireEvent.click(button);

      await waitForRecorded(item);
      expect(posts(api).map((call) => call.body)).toEqual([{ kind }]);
    },
  );

  it('posts only the first of two different feedback actions pressed within one frame', async () => {
    const item = makeItem();
    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('snooze', { recommendation_id: item.id }),
      ),
    });
    await renderCard(<RecommendationCard item={item} />);

    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.snooze }));
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done }));

    await waitForRecorded(item);
    expect(posts(api).map((call) => call.body)).toEqual([{ kind: 'snooze' }]);
  });

  it('keeps "Record decision" closed when it is pressed in the frame of a running feedback', async () => {
    const item = c1Item();
    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('dismiss', { recommendation_id: item.id }),
      ),
    });
    await renderCard(<RecommendationCard item={item} />);

    fireEvent.click(within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.dismiss }));
    fireEvent.click(within(cardOf(item)).getByRole('button', { name: RECORD_DECISION_LABEL }));

    await waitForRecorded(item);
    // An opened dialog would also read the subject's decision log, which the stub refuses.
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api).map((call) => call.body)).toEqual([{ kind: 'dismiss' }]);
    expect(api.calls.filter((call) => call.method === 'GET')).toEqual([]);
  });

  it('frees the actions after a failed feedback, so a retry posts again, once', async () => {
    const item = makeItem();
    api.use({
      'POST /recommendations/:id/feedback': apiError(503, 'service_unavailable', 'Try later.'),
    });
    await renderCard(<RecommendationCard item={item} />);
    const done = within(cardOf(item)).getByRole('button', { name: FEEDBACK_LABELS.done });

    fireEvent.click(done);
    fireEvent.click(done);

    expect(await within(cardOf(item)).findByText('Try later.')).toBeDefined();
    await waitFor(() => {
      cardButtons(item).forEach((button) => {
        expect(button).toHaveProperty('disabled', false);
      });
    });
    expect(posts(api)).toHaveLength(1);

    api.use({
      'POST /recommendations/:id/feedback': json(
        201,
        makeFeedback('done', { recommendation_id: item.id }),
      ),
    });
    fireEvent.click(done);
    fireEvent.click(done);

    await waitForRecorded(item);
    expect(posts(api).map((call) => call.body)).toEqual([{ kind: 'done' }, { kind: 'done' }]);
  });
});


// ---------------------------------------------------------------------------------------------
// What a recorded decision states
// ---------------------------------------------------------------------------------------------

describe("RecommendationActions: the recorded decision's state", () => {
  it('states the recorded state, not the recommendation, on the card and in the announcement', async () => {
    api.use({
      'GET /decisions': json(200, [genericCrmDecision()]),
      'POST /decisions': json(
        201,
        genericCrmDecision({
          id: 812,
          origin: 'recommendation',
          decided_by: 'admin@example.com',
          status: 'active',
          daily_budget: '160.00',
          patch: { daily_budget: '160.00' },
          recommendation_id: FIXTURE_ITEM_IDS.c1,
        }),
      ),
    });
    const item = c1Item();
    await renderCard(
      <ActionOutcomeProvider>
        <RecommendationCard item={item} />
      </ActionOutcomeProvider>,
    );

    // The C1 card proposes "Pause"; the user records Active with a daily budget of 160 instead.
    const dialog = await openDecisionDialog(item);
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Status' }), {
      target: { value: 'active' },
    });
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: 'Daily budget' }), {
      target: { value: '160.00' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    const line = await within(cardOf(item)).findByText(
      'Decision recorded: Active · daily budget $160.00',
    );
    expect(line.getAttribute('role')).toBeNull();
    expect(outcomeRegion().textContent).toBe(
      'Decision recorded: Active · daily budget $160.00 (Generic CRM — US)',
    );
    expect(outcomeRegion().textContent).not.toContain(item.action);
    expect(posts(api).map((call) => call.body)).toEqual([
      {
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.genericCrm },
        patch: { daily_budget: '160.00' },
        recommendation_id: FIXTURE_ITEM_IDS.c1,
      },
    ]);
  });

  it("states a shared budget's recorded amount, in its currency, with no status", async () => {
    api.use({
      'GET /decisions': json(200, [makeDecision('budget')]),
      'POST /decisions': json(
        201,
        makeDecision('budget', {
          id: 813,
          origin: 'recommendation',
          daily_budget: '600.00',
          patch: { daily_budget: '600.00' },
          recommendation_id: 1250,
        }),
      ),
    });
    const item = sharedB1Item();
    await renderCard(<RecommendationCard item={item} />);

    const dialog = await openDecisionDialog(item);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    // Outside a provider the card's own line is the polite region.
    const line = await within(cardOf(item)).findByText('Decision recorded: daily budget $600.00');
    expect(line.getAttribute('role')).toBe('status');
  });

  it("states a shared member's recorded status and its budget, never an amount", async () => {
    const member = {
      type: 'campaign' as const,
      id: FIXTURE_CAMPAIGN_IDS.genericCrm,
      name: 'Generic CRM — US',
      source: 'google' as const,
      currency: 'USD',
    };
    api.use({
      'GET /decisions': json(200, [makeDecision('shared_member', { subject: member })]),
      'POST /decisions': json(
        201,
        makeDecision('shared_member', {
          id: 814,
          subject: member,
          origin: 'recommendation',
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

    // The member's shared budget is named by the Google id its decision carries
    // (`budget_external_id` 31000402 for FIXTURE_BUDGET_IDS.search), as the decision log names it.
    expect(
      await within(cardOf(item)).findByText('Decision recorded: Paused · shared budget 31000402'),
    ).toBeDefined();
  });
});
