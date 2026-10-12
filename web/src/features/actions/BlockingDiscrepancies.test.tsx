/**
 * Tests for `BlockingDiscrepancies`: the links from a held card or row to the discrepancies that
 * hold it (AAP 0.5.5 "with the blocking discrepancy linked"; AAP 0.5.4.2 P1, including holds the
 * overlay adds at read time for discrepancies detected after the run).
 *
 * The suite covers:
 *
 * - `indexC0Cards`: the run's C0 cards by discrepancy id, with item id, subject name and kind;
 * - `needsLiveDiscrepancies`: only a shown, non-historical, non-C0 held item without a recorded
 *   blocker the run can show needs `GET /discrepancies`;
 * - `blockingLinks`: recorded ids ascending, then live discrepancies on a subject the item
 *   depends on or would write, each once, named by subject and kind and kept distinct;
 * - the component under the provider: an in-page link to the run's C0 card, a `/decisions`
 *   link for a blocker without one (which navigates to `/decisions#discrepancy-{id}`), the
 *   live lookup for a post-run hold, no request without such a hold, the "View open
 *   discrepancies" fallback while the list loads or after it failed, and without the provider;
 * - the wiring on `ActionsPage`, whose held card links the page's own C0 card.
 *
 * The network is stubbed by `../../test/mockApi`, which fails a test on any request it has no
 * handler for, so a test without a `GET /discrepancies` handler also proves none was sent. It
 * is imported before `../../test/render` and Testing Library so its unmatched-request check
 * runs after Testing Library's automatic cleanup (Vitest runs after-hooks in reverse order of
 * registration), and a check that throws cannot skip that cleanup.
 */
import { apiError, json, mockApi, type MockResponse } from '../../test/mockApi';
import { createTestQueryClient, renderWithProviders } from '../../test/render';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { DEFAULT_DISCREPANCY_STATES, queryKeys } from '../../api/queries';
import type { Discrepancy, Item, Section } from '../../api/types';
import {
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeConfig,
  makeDiscrepancy,
  makeEvidence,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
  makeSources,
} from '../../test/fixtures';
import { ActionsPage } from './ActionsPage';
import {
  BLOCKING_DISCREPANCIES_LIST_LABEL,
  BLOCKING_DISCREPANCY_LINK_LABEL,
  BlockingDiscrepanciesProvider,
  BlockingDiscrepancyLinks,
  OPEN_DISCREPANCIES_LINK_LABEL,
  blockingLinks,
  indexC0Cards,
  needsLiveDiscrepancies,
  type C0CardIndex,
} from './BlockingDiscrepancies';
import { RecommendationCard } from './RecommendationCard';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The in-page anchor of the Campaigns discrepancy group. */
const GROUP_HREF = '#group-campaigns-discrepancies';

/** The campaign and shared budget the held items below depend on. */
const CAMPAIGN = { type: 'campaign', id: 301 } as const;
const BUDGET = { type: 'budget', id: 402 } as const;

/** A C0 item showing discrepancy `discrepancyId` on the subject `name`. */
function c0Item(
  id: number,
  discrepancyId: number,
  name: string | null,
  kind = 'status',
  overrides: Partial<Item> = {},
): Item {
  return makeItem({
    id,
    rule_id: 'C0',
    action: `Resolve discrepancy ${String(discrepancyId)}`,
    entity: { source: 'google', type: 'campaign', id: String(id), name },
    evidence: makeEvidence({
      extra: {
        discrepancy_id: discrepancyId,
        kind,
        shape: 'own_budget',
        state: 'open',
        resolutions: ['platform_correct', 'decision_correct'],
        depends_on: [CAMPAIGN],
      },
    }),
    ...overrides,
  });
}

/**
 * A held K1 item: `blocking` are the ids the run recorded (a frozen hold when non-empty), and
 * the overlay holds it now either way. It depends on {@link CAMPAIGN}.
 */
function heldItem(id: number, blocking: readonly number[], overrides: Partial<Item> = {}): Item {
  return makeItem({
    id,
    action: `Held action ${String(id)}`,
    evidence: makeEvidence({
      extra:
        blocking.length === 0
          ? { depends_on: [CAMPAIGN] }
          : { depends_on: [CAMPAIGN], blocking_discrepancies: [...blocking] },
    }),
    held: blocking.length > 0,
    held_reason: blocking.length > 0 ? 'Resolve discrepancy first' : null,
    current: makeItemOverlay({ held_now: true, held_reason_now: 'Resolve discrepancy first' }),
    ...overrides,
  });
}

/** One Campaigns section holding `items` in its discrepancy group. */
function sectionsWith(...items: Item[]): Section[] {
  return [
    makeSection('campaigns', {
      groups: [makeGroup({ key: 'discrepancies', title: 'Discrepancies', items })],
    }),
  ];
}

/** A live discrepancy on `subject`. */
function live(
  id: number,
  subject: { type: 'campaign' | 'budget'; id: number },
  name: string | null,
  kind: Discrepancy['kind'] = 'status',
): Discrepancy {
  return makeDiscrepancy(kind, {
    id,
    shape: subject.type === 'budget' ? 'budget' : 'own_budget',
    subject: { ...subject, name, source: 'google', currency: 'USD' },
  });
}

/** Renders `item`'s links under the provider for `sections`. */
async function renderLinks(sections: readonly Section[], item: Item, path = '/actions') {
  return renderWithProviders(
    <BlockingDiscrepanciesProvider sections={sections}>
      <BlockingDiscrepancyLinks item={item} />
    </BlockingDiscrepanciesProvider>,
    { path },
  );
}

/** The link named `name`. */
function link(name: string): HTMLElement {
  return screen.getByRole('link', { name });
}

// ---------------------------------------------------------------------------------------------
// The run's C0 cards and whether the live list is needed
// ---------------------------------------------------------------------------------------------

describe('indexC0Cards', () => {
  it('indexes each shown C0 card by its discrepancy id, with its subject name and kind', () => {
    const index = indexC0Cards(
      sectionsWith(
        c0Item(11, 51, ' Brand Search — US '),
        c0Item(12, 52, null, 'budget'),
        // A repeated id keeps its first card; a hidden card and a card without an id render no
        // anchor and are left out, and so is every non-C0 item.
        c0Item(13, 51, 'Duplicate'),
        c0Item(14, 53, 'Hidden', 'status', {
          current: makeItemOverlay({ hidden_by: 'dismiss' }),
        }),
        makeItem({ id: 15, rule_id: 'C0', evidence: makeEvidence({ extra: { kind: 'status' } }) }),
        heldItem(16, [51]),
      ),
    );
    expect([...index.entries()]).toEqual([
      ['51', { itemId: 11, subjectName: 'Brand Search — US', kind: 'status' }],
      ['52', { itemId: 12, subjectName: null, kind: 'budget' }],
    ]);
  });
});

describe('needsLiveDiscrepancies', () => {
  const index = indexC0Cards(sectionsWith(c0Item(11, 51, 'Brand Search — US')));

  it('is false when every held item names a blocker the run has a card for', () => {
    expect(needsLiveDiscrepancies(sectionsWith(heldItem(1, [51])), index)).toBe(false);
    expect(needsLiveDiscrepancies(sectionsWith(makeItem({ id: 2 })), index)).toBe(false);
  });

  it('is true for a hold added after the run, or a recorded blocker without a card', () => {
    expect(needsLiveDiscrepancies(sectionsWith(heldItem(1, [])), index)).toBe(true);
    expect(needsLiveDiscrepancies(sectionsWith(heldItem(1, [51, 77])), index)).toBe(true);
  });

  it('ignores C0, historical and hidden items, which show no blocker link', () => {
    const sections = sectionsWith(
      c0Item(11, 51, 'Brand Search — US', 'status', {
        held: true,
        current: makeItemOverlay({ held_now: true }),
      }),
      heldItem(2, [], { current: makeItemOverlay({ held_now: true, historical: true }) }),
      heldItem(3, [], { current: makeItemOverlay({ held_now: true, hidden_by: 'snooze' }) }),
    );
    expect(needsLiveDiscrepancies(sections, index)).toBe(false);
  });
});

// ---------------------------------------------------------------------------------------------
// Resolution
// ---------------------------------------------------------------------------------------------

describe('blockingLinks', () => {
  const index: C0CardIndex = new Map([
    ['51', { itemId: 11, subjectName: 'Brand Search — US', kind: 'status' }],
    ['52', { itemId: 12, subjectName: null, kind: 'unknown_kind' }],
  ]);

  it('lists recorded ids ascending, then live ones on its subjects, each once', () => {
    const item = heldItem(1, [52, 51], { decision_subject: BUDGET });
    const links = blockingLinks(item, index, [
      live(60, { type: 'campaign', id: 999 }, 'Elsewhere'),
      live(52, CAMPAIGN, 'Brand Search — US', 'budget'),
      live(62, BUDGET, 'Search Shared Budget', 'budget'),
      live(61, CAMPAIGN, null, 'missing'),
    ]);
    expect(links).toEqual([
      {
        discrepancyId: '51',
        c0ItemId: 11,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Status mismatch)`,
      },
      {
        // The card has no name and an unknown kind, so the live list names both.
        discrepancyId: '52',
        c0ItemId: 12,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Budget mismatch)`,
      },
      {
        discrepancyId: '62',
        c0ItemId: null,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Search Shared Budget (Budget mismatch)`,
      },
      {
        discrepancyId: '61',
        c0ItemId: null,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Campaign 301 (Missing from platform)`,
      },
    ]);
  });

  it('names an unlisted blocker by id, and a card of unknown kind without a kind', () => {
    expect(blockingLinks(heldItem(1, [7, 52]), index, undefined)).toEqual([
      { discrepancyId: '7', c0ItemId: null, label: `${BLOCKING_DISCREPANCY_LINK_LABEL} 7` },
      { discrepancyId: '52', c0ItemId: 12, label: `${BLOCKING_DISCREPANCY_LINK_LABEL} 52` },
    ]);
  });

  it('leaves out what the API names in a way /decisions does not label', () => {
    const renamed = { ...live(90, CAMPAIGN, null), kind: 'renamed' } as unknown as Discrepancy;
    const unnamed = live(91, CAMPAIGN, null);
    const untyped = {
      ...unnamed,
      subject: { ...unnamed.subject, type: 'other' },
    } as unknown as Discrepancy;
    expect(blockingLinks(heldItem(1, [90, 91]), new Map(), [renamed, untyped])).toEqual([
      {
        discrepancyId: '90',
        c0ItemId: null,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Campaign 301`,
      },
      {
        discrepancyId: '91',
        c0ItemId: null,
        label: `${BLOCKING_DISCREPANCY_LINK_LABEL} 91 (Status mismatch)`,
      },
    ]);
  });

  it('sorts numeric ids before others, keeping the stored order of the rest', () => {
    const item = makeItem({
      evidence: makeEvidence({ extra: { blocking_discrepancies: ['b', 9, 'a', 2] } }),
    });
    expect(blockingLinks(item, new Map(), undefined).map((entry) => entry.discrepancyId)).toEqual(
      ['2', '9', 'b', 'a'],
    );
  });

  it('tells apart two blockers that would share a text', () => {
    const item = heldItem(1, [], { decision_subject: BUDGET });
    const labels = blockingLinks(item, new Map(), [
      live(70, CAMPAIGN, 'Brand', 'budget'),
      live(71, BUDGET, 'Brand', 'budget'),
    ]).map((entry) => entry.label);
    expect(labels).toEqual([
      `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand (Budget mismatch), discrepancy 70`,
      `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand (Budget mismatch), discrepancy 71`,
    ]);
  });

  it('names no blocker for an item that records none while the list is unread', () => {
    expect(blockingLinks(heldItem(1, []), index, undefined)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------
// The links under the provider
// ---------------------------------------------------------------------------------------------

describe('BlockingDiscrepancyLinks', () => {
  it("links a recorded blocker to the run's C0 card in-page, sending no request", async () => {
    const api = mockApi({});
    const held = heldItem(1, [51]);
    await renderLinks(sectionsWith(c0Item(11, 51, 'Brand Search — US'), held), held);

    const name = `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Status mismatch)`;
    expect(link(name).getAttribute('href')).toBe('#item-11');
    expect(screen.queryByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL })).toBeNull();
    expect(api.calls).toEqual([]);
  });

  it('links a recorded blocker without a C0 card to /decisions and navigates there', async () => {
    const api = mockApi({
      'GET /discrepancies': json(200, [live(77, CAMPAIGN, 'Generic CRM — US', 'budget')]),
    });
    const held = heldItem(1, [77]);
    const sections = sectionsWith(c0Item(11, 51, 'Brand Search — US'), held);
    const { router } = await renderLinks(sections, held);

    const name = `${BLOCKING_DISCREPANCY_LINK_LABEL}: Generic CRM — US (Budget mismatch)`;
    const target = await screen.findByRole('link', { name });
    expect(target.getAttribute('href')).toBe('/decisions#discrepancy-77');
    expect(api.calls.map((call) => call.path)).toEqual(['/discrepancies']);
    expect(api.calls[0]?.search.getAll('state')).toEqual(['open', 'awaiting_platform']);

    fireEvent.click(target);
    await waitFor(() => expect(router.state.location.pathname).toBe('/decisions'));
    expect(router.state.location.hash).toBe('discrepancy-77');
  });

  it('finds post-run blockers through depends_on and decision_subject, one link each', async () => {
    mockApi({
      'GET /discrepancies': json(200, [
        live(61, CAMPAIGN, 'Brand Search — US', 'status'),
        live(62, BUDGET, 'Search Shared Budget', 'budget'),
        live(63, { type: 'campaign', id: 999 }, 'Unrelated campaign', 'status'),
      ]),
    });
    const held = heldItem(1, [], { decision_subject: BUDGET });
    await renderLinks(sectionsWith(held), held);

    const list = await screen.findByRole('list', { name: BLOCKING_DISCREPANCIES_LIST_LABEL });
    const links = within(list).getAllByRole('link');
    expect(links.map((entry) => [entry.textContent, entry.getAttribute('href')])).toEqual([
      [
        `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Status mismatch)`,
        '/decisions#discrepancy-61',
      ],
      [
        `${BLOCKING_DISCREPANCY_LINK_LABEL}: Search Shared Budget (Budget mismatch)`,
        '/decisions#discrepancy-62',
      ],
    ]);
    expect(screen.queryByRole('link', { name: /Unrelated campaign/u })).toBeNull();
  });

  it('shows the list link while the live list loads, then the blocker', async () => {
    const gate: { release?: (response: MockResponse) => void } = {};
    mockApi({
      'GET /discrepancies': () =>
        new Promise<MockResponse>((resolve) => {
          gate.release = resolve;
        }),
    });
    const held = heldItem(1, []);
    await renderLinks(sectionsWith(held), held);

    expect(link(OPEN_DISCREPANCIES_LINK_LABEL).getAttribute('href')).toBe(GROUP_HREF);
    await waitFor(() => expect(gate.release).toBeDefined());
    gate.release?.(json(200, [live(61, CAMPAIGN, 'Brand Search — US')]));

    const name = `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Status mismatch)`;
    expect((await screen.findByRole('link', { name })).getAttribute('href')).toBe(
      '/decisions#discrepancy-61',
    );
    expect(screen.queryByRole('link', { name: OPEN_DISCREPANCIES_LINK_LABEL })).toBeNull();
  });

  it('keeps the list link when the live list fails', async () => {
    mockApi({ 'GET /discrepancies': apiError(500, 'internal_error', 'Boom') });
    const held = heldItem(1, []);
    const { queryClient } = await renderLinks(sectionsWith(held), held);
    const key = queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES);
    await waitFor(() => expect(queryClient.getQueryState(key)?.status).toBe('error'));
    expect(link(OPEN_DISCREPANCIES_LINK_LABEL).getAttribute('href')).toBe(GROUP_HREF);
    expect(screen.queryByRole('link', { name: /^View blocking discrepancy/u })).toBeNull();
  });

  it('keeps the list link when the live list names no blocker of the item', async () => {
    mockApi({ 'GET /discrepancies': json(200, [live(63, { type: 'campaign', id: 999 }, 'X')]) });
    const held = heldItem(1, []);
    const { queryClient } = await renderLinks(sectionsWith(held), held);
    const key = queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES);
    await waitFor(() => expect(queryClient.getQueryState(key)?.status).toBe('success'));
    expect(link(OPEN_DISCREPANCIES_LINK_LABEL).getAttribute('href')).toBe(GROUP_HREF);
    expect(screen.queryByRole('link', { name: /^View blocking discrepancy/u })).toBeNull();
  });

  it('adds a post-run blocker from a list another reader cached, sending no request', async () => {
    const api = mockApi({});
    const held = heldItem(1, [51]);
    const sections = sectionsWith(c0Item(11, 51, 'Brand Search — US'), held);
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES), [
      live(51, CAMPAIGN, 'Brand Search — US'),
      live(80, CAMPAIGN, 'Brand Search — US', 'budget'),
    ]);
    await renderWithProviders(
      <BlockingDiscrepanciesProvider sections={sections}>
        <BlockingDiscrepancyLinks item={held} />
      </BlockingDiscrepanciesProvider>,
      { queryClient },
    );

    const list = screen.getByRole('list', { name: BLOCKING_DISCREPANCIES_LIST_LABEL });
    expect(within(list).getAllByRole('link').map((entry) => entry.getAttribute('href'))).toEqual(
      ['#item-11', '/decisions#discrepancy-80'],
    );
    expect(api.calls).toEqual([]);
  });

  it('works without the provider: recorded ids open /decisions, none the list', async () => {
    const api = mockApi({});
    await renderWithProviders(
      <>
        <BlockingDiscrepancyLinks item={heldItem(1, [51])} />
        <BlockingDiscrepancyLinks item={heldItem(2, [])} />
      </>,
    );
    expect(link(`${BLOCKING_DISCREPANCY_LINK_LABEL} 51`).getAttribute('href')).toBe(
      '/decisions#discrepancy-51',
    );
    expect(link(OPEN_DISCREPANCIES_LINK_LABEL).getAttribute('href')).toBe(GROUP_HREF);
    expect(api.calls).toEqual([]);
  });

  it('renders inside a held RecommendationCard beside its hold reason', async () => {
    mockApi({});
    const held = heldItem(1, [51]);
    await renderWithProviders(
      <BlockingDiscrepanciesProvider sections={sectionsWith(c0Item(11, 51, 'Brand'), held)}>
        <RecommendationCard item={held} />
      </BlockingDiscrepanciesProvider>,
    );
    const card = screen.getByRole('article', { name: held.action });
    expect(within(card).getByText('Resolve discrepancy first')).toBeDefined();
    expect(
      within(card)
        .getByRole('link', { name: `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand (Status mismatch)` })
        .getAttribute('href'),
    ).toBe('#item-11');
  });
});

// ---------------------------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------------------------

describe('ActionsPage wiring', () => {
  it("links a held card to the page's own C0 card", async () => {
    const page = makeRecommendationsPage();
    const heldPause = findPageItem(page, FIXTURE_ITEM_IDS.heldPause);
    heldPause.evidence = {
      ...heldPause.evidence,
      extra: { ...heldPause.evidence.extra, blocking_discrepancies: [FIXTURE_DISCREPANCY_ID] },
    };
    mockApi({
      'GET /recommendations': json(200, page),
      'GET /config': json(200, makeConfig()),
      'GET /runs/latest': json(200, makeRun()),
      'GET /runs': json(200, []),
      'GET /sources': json(200, makeSources()),
      'GET /discrepancies': json(200, [makeDiscrepancy('status')]),
    });
    await renderWithProviders(<ActionsPage />);

    // The Pause group's held disclosure, which holds the card.
    const toggle = await waitFor(() => {
      const element = document.querySelector('[aria-controls="group-campaigns-pause-held"]');
      if (!(element instanceof HTMLElement)) {
        throw new Error('the Pause group has no held disclosure yet');
      }
      return element;
    });
    fireEvent.click(toggle);
    const card = document.getElementById(`item-${String(FIXTURE_ITEM_IDS.heldPause)}`);
    if (card === null) {
      throw new Error('the held pause card is not rendered');
    }
    const name = `${BLOCKING_DISCREPANCY_LINK_LABEL}: Brand Search — US (Status mismatch)`;
    expect(within(card).getByRole('link', { name }).getAttribute('href')).toBe(
      `#item-${String(FIXTURE_ITEM_IDS.c0)}`,
    );
    expect(document.getElementById(`item-${String(FIXTURE_ITEM_IDS.c0)}`)).not.toBeNull();
  });
});
