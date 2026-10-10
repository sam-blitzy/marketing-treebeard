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
 *   feedback posts once to `POST /recommendations/{id}/feedback` with the CSRF header.
 * - **Failures and locks**: a 409 `subject_retired` shows "Computed from replaced data" and locks
 *   every action, whether a feedback or the decision form received it, also once the dialog has
 *   closed and when it was closed while the save ran; any other failure shows the API's
 *   message, and a failure that is not an `ApiError` the generic text, never its own message;
 *   a historical item shows the badge and its frozen provenance with every action disabled,
 *   the C0 resolver too.
 * - **Outcomes and focus**: inside an `ActionOutcomeProvider`, a recorded feedback or decision is
 *   announced in the provider's polite region, which outlives the card a refetch removes, and
 *   focus moves to the next card's first enabled action, else the previous card's, else the
 *   section heading, unless the user has moved it elsewhere.
 * - **Decision saves**: the dialog may close while its save runs, but every action stays
 *   disabled and "Record decision" loading until the save settles, so nothing reopens it or
 *   posts again. A late success is reported and announced, a late failure of any kind is shown
 *   on the card and takes focus, and focus otherwise returns to "Record decision" when it was
 *   lost.
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

  it('drops the recorded unverified flag once no discrepancy is open now', async () => {
    // Recorded while 1 discrepancy was open; the overlay omits the flag at 0 open.
    const item = makeItem({
      flags: [
        makeFlag('simulated'),
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancies are open',
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
    ).toEqual(['Simulated data', FIXTURE_FLAGS.stale.message]);
    expect(within(flags).queryByText(/Unverified while/u)).toBeNull();
    expect(within(cardOf(item)).queryByText(/Unverified while/u)).toBeNull();
  });

  it('shows the live open-discrepancy count once, in place of the recorded one', async () => {
    const item = makeItem({
      flags: [
        makeFlag('simulated'),
        makeFlag('unverified_discrepancies', {
          message: 'Unverified while 1 discrepancies are open',
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
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
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

    const line = await within(cardOf(item)).findByText(DECISION_RECORDED_MESSAGE);
    // Inside a provider the card's own line is visible but no second live region.
    expect(line.getAttribute('role')).toBeNull();
    expect(outcomeRegion().textContent).toBe(
      `${DECISION_RECORDED_MESSAGE}: ${item.action} (Generic CRM — US)`,
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

    // Until the save settles, nothing on the card reopens the dialog or posts again.
    expect(opener.getAttribute('aria-busy')).toBe('true');
    expect(within(cardOf(item)).getByText(DECISION_SAVING_MESSAGE)).toBeDefined();
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
      fireEvent.click(button);
    }
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api)).toHaveLength(1);

    release(json(201, saved));

    // The late save is reported on the card and in the provider's region...
    expect(await within(cardOf(item)).findByText(DECISION_RECORDED_MESSAGE)).toBeDefined();
    expect(within(cardOf(item)).queryByText(DECISION_SAVING_MESSAGE)).toBeNull();
    expect(outcomeRegion().textContent).toBe(
      `${DECISION_RECORDED_MESSAGE}: ${item.action} (Generic CRM — US)`,
    );
    // ...the actions are usable again, and focus, which the closed dialog could not return to
    // the disabled button, is back on "Record decision".
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
      expect(opener).toHaveProperty('disabled', true);
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

    expect(await within(cardOf(item)).findByText(DECISION_RECORDED_MESSAGE)).toBeDefined();
    await waitFor(() => {
      expect(opener).toHaveProperty('disabled', false);
    });
    expect(document.activeElement).toBe(elsewhere);
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
    for (const button of cardButtons(item)) {
      expect(button).toHaveProperty('disabled', true);
    }
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
