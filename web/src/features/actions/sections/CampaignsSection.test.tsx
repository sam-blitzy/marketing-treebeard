/**
 * Component tests for `CampaignsSection`, the "Campaigns to pause or launch" section of
 * `/actions` (AAP 0.5.5; AAP 0.5.4.4 C0–C3; AAP 0.5.4.2 P1).
 *
 * They hold the section to its TSDoc contract on the default fixture page and on items built
 * here in the backend's shape:
 * - the groups Discrepancies, Pause, Scale and Launch ideas in API order, the discrepancy group
 *   first and anchored at `#group-campaigns-discrepancies`;
 * - C0 cards: "Platform shows {platform}, last decided {decided}" and, in place of feedback
 *   actions, exactly the resolutions `GET /discrepancies` lists for the live discrepancy, read
 *   once for the whole group, with its loading, failed, checking, resolved-since-run (only when
 *   `GET /discrepancies?state=resolved` lists it), no-longer-listed and missing-id states, each
 *   in one persistent polite status line that takes focus when the controls leave with it; a
 *   historical C0 stays pinned with its frozen resolutions disabled and reads no list;
 * - held cards collapsed at the end of their group; one without a recorded blocker links the
 *   discrepancy group as "View open discrepancies";
 * - the C1, C1b and C2 field sets ("Campaign evidence", and the "Pipeline evidence" table with
 *   MQL and QL in separate columns for C1b and C2 "Hold" only);
 * - the C2 "Scale" decision workflow posting its B1 amount, own or shared; "Hold" marks done;
 * - the C2 "Scale" link to the B1 card for its subject, inside a `B1CardsProvider`, named by
 *   the B1 action, kept while that B1 card is held or computed from replaced data, and no link
 *   without a B1 card rendered for that subject (absent or hidden by feedback), outside the
 *   provider, or on a "Hold";
 * - the C3a segment gap and the C3b keyword-cluster gap;
 * - the empty discrepancy group kept, with the live count and a link to `/decisions`, while the
 *   overlay counts discrepancies detected after the run;
 * - no `role="alert"` anywhere in the section;
 * - a card hidden by its own feedback leaves its confirmation where it stood, in the discrepancy
 *   group after the pinned C0 cards as in any other group, until a rerun drops it.
 *
 * The network is stubbed with `mockApi`, which fails a test on any request it does not expect,
 * so a section without a C0 card is also shown to send no request at all. Loading states are
 * asserted against a response the test holds pending, never against a race with the stub.
 * Assertions use Testing Library queries and Vitest's own matchers only (no jest-dom, no
 * user-event).
 */
import { apiError, json, mockApi } from '../../../test/mockApi';
import type { MockHandlers, MockResponse, RecordedCall } from '../../../test/mockApi';
import { LiveSection, feedbackConfirmationText, feedbackFlow } from '../../../test/feedbackFlow';
import { KEEP_SPACES, renderWithProviders } from '../../../test/render';
import type { QueryClient } from '@tanstack/react-query';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { memo, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DEFAULT_DISCREPANCY_STATES, queryKeys } from '../../../api/queries';
import type {
  DatasetCoverage,
  Decision,
  Discrepancy,
  Item,
  Metric,
  Overlay,
  Section,
} from '../../../api/types';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_DATASET_IDS,
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  FIXTURE_RESOLUTIONS,
  FIXTURE_WINDOW,
  findPageItem,
  makeDecision,
  makeDiscrepancy,
  makeEvidence,
  makeFlag,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../../test/timeouts';
import { B1CardsProvider } from '../B1Cards';
import { Button } from '../../../design-system';
import {
  DECISION_RECORDED_MESSAGE,
  FEEDBACK_LABELS,
  HISTORICAL_LABEL,
  RECORD_DECISION_LABEL,
} from '../RecommendationCard';
import {
  B1_LINK_LABEL,
  CAMPAIGN_EVIDENCE_LABEL,
  CLUSTER_GAP_LABEL,
  CLUSTER_TERMS_CAPTION,
  CampaignsSection,
  DISCREPANCY_CHECKING_TEXT,
  DISCREPANCY_LOAD_ERROR_PREFIX,
  DISCREPANCY_NOT_LISTED_TEXT,
  DISCREPANCY_UNAVAILABLE_TEXT,
  HISTORICAL_RESOLUTION_NOTICE,
  LOADING_DISCREPANCY_LABEL,
  PIPELINE_EVIDENCE_CAPTION,
  RESOLVED_SINCE_RUN_TEXT,
  RESOLVE_ON_DECISIONS_LABEL,
  SEGMENT_GAP_LABEL,
} from './CampaignsSection';
import { DISCREPANCY_GROUP_ANCHOR } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------------------------

/** One evidence metric as the backend serialises it (`unavailable` exactly when `null`). */
function metric(
  key: string,
  label: string,
  value: Metric['value'],
  unit: string | null,
  currency: string | null = null,
): Metric {
  return { key, label, value, unit, currency, floor: null, unavailable: value === null };
}

/** The default page's Campaigns section. */
function defaultCampaigns(): Section {
  const section = makeRecommendationsPage().sections.find((entry) => entry.key === 'campaigns');
  if (section === undefined) {
    throw new Error('the default fixture page has no campaigns section');
  }
  return section;
}

/** The default page's item `id`. */
function pageItem(id: Item['id']): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** A Campaigns section holding exactly these groups, in this order. */
function campaignsWith(groups: Section['groups']): Section {
  return makeSection('campaigns', { groups });
}

/** A Campaigns section with one group of the given key holding `items`. */
function oneGroup(key: string, title: string, items: Item[]): Section {
  return campaignsWith([makeGroup({ key, title, items })]);
}

/** Renders the section with the default run and the given overlay, inside the providers. */
async function renderCampaigns(section: Section, overlay: Overlay = makeOverlay()) {
  return renderWithProviders(
    <CampaignsSection section={section} run={makeRun()} overlay={overlay} />,
  );
}

/** The card (`<article>`) named by its action. */
function card(action: string): HTMLElement {
  return screen.getByRole('article', { name: action });
}

/** The text of every `<li>` of the list with this accessible name inside `scope`. */
function listLines(scope: HTMLElement, name: string): string[] {
  return within(within(scope).getByRole('list', { name }))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** The header texts and every body row's cell texts of the table with this caption. */
function tableOf(scope: HTMLElement, caption: string): { headers: string[]; rows: string[][] } {
  const table = within(scope).getByRole('table', { name: caption });
  const headers = within(table)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent);
  // Body rows only: each row's header cell (when the table has one) and its data cells.
  const rows = within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent));
  return { headers, rows };
}

/** The C0 action of the default page. */
const C0_ACTION = 'Resolve: platform shows Paused, last decided Active';

/** The live list the C0 card of the default page reads. */
function openDiscrepancies() {
  return json(200, [makeDiscrepancy('status')]);
}

/**
 * The key of the resolved-list read. It outranks `GET /discrepancies`, which lists no state and
 * so matches every read, because it lists one query parameter more.
 */
const RESOLVED_KEY = 'GET /discrepancies?state=resolved';

/** The resolved list holding the default page's C0 discrepancy, closed after the run. */
function resolvedDiscrepancies() {
  return json(200, [
    makeDiscrepancy('status', {
      state: 'resolved',
      resolution: 'platform_correct',
      resolved_at: '2026-10-06T09:30:00Z',
    }),
  ]);
}

/** Every `GET /discrepancies` the stub received, as the list of `state` values each sent. */
function discrepancyReads(calls: readonly RecordedCall[]): string[][] {
  return calls
    .filter((call) => call.method === 'GET' && call.path === '/discrepancies')
    .map((call) => call.search.getAll('state'));
}

/**
 * A C0 card's status line: the first `role="status"` element of its footer, which is always
 * there, empty while nothing is to be said. A `Spinner` and the resolution controls' own
 * outcome line may follow it.
 */
function statusLine(scope: HTMLElement): HTMLElement {
  const [line] = within(scope).getAllByRole('status');
  if (line === undefined) {
    throw new Error('the card has no status line');
  }
  return line;
}

/**
 * Gives any effect-started query a macrotask to reach the stub before calls are counted, inside
 * `act`, so an answer that lands meanwhile updates the tree as it would in the browser.
 */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

/** Where {@link SwappableCampaigns} publishes how to replace its section. */
interface SectionSwap {
  /** Replaces the rendered section, as a newer run replaces the page's sections. */
  swap?: (next: Section) => void;
}

/** Props of {@link SwappableCampaigns}. */
interface SwappableCampaignsProps {
  /** The section rendered first. */
  initial: Section;
  /** Receives the function that replaces the section once the harness has mounted. */
  handle: SectionSwap;
}

/** The section in a harness whose section a test replaces while it stays mounted. */
function SwappableCampaigns({ initial, handle }: SwappableCampaignsProps): ReactElement {
  const [section, setSection] = useState(initial);
  useEffect(() => {
    handle.swap = setSection;
  }, [handle]);
  return <CampaignsSection section={section} run={makeRun()} overlay={makeOverlay()} />;
}

/** The text labels (`textContent`) of every button inside `scope`, in document order. */
function buttonNames(scope: HTMLElement): string[] {
  return within(scope)
    .queryAllByRole('button')
    .map((button) => button.textContent);
}

/** The footer actions C0 never offers: feedback (AAP 0.5.4.2) and "Record decision". */
const NON_C0_ACTIONS = ['Dismiss', 'Snooze for 7 days', 'Mark done', 'Record decision'] as const;

/** A response the test releases by hand, so the request stays pending until it does. */
interface HeldResponse {
  /** The handler: every request waits for {@link HeldResponse.release}. */
  handler: () => Promise<MockResponse>;
  /** Answers every waiting request, and every later one, with `response`. */
  release: (response: MockResponse) => void;
}

/**
 * A {@link HeldResponse}, so a loading state is asserted while the request is certainly still
 * pending rather than racing the stub's asynchronous answer.
 */
function heldResponse(): HeldResponse {
  let answer: MockResponse | undefined;
  const waiting: ((response: MockResponse) => void)[] = [];
  return {
    handler: () =>
      answer === undefined
        ? new Promise<MockResponse>((resolve) => {
            waiting.push(resolve);
          })
        : Promise.resolve(answer),
    release: (response) => {
      answer = response;
      for (const resolve of waiting.splice(0)) {
        resolve(response);
      }
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Section layout
// ---------------------------------------------------------------------------------------------

describe('CampaignsSection layout', () => {
  it('renders Discrepancies, Pause, Scale and Launch ideas in API order', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(defaultCampaigns());

    const section = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    expect(section.id).toBe('section-campaigns');
    expect(
      within(section)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Pause', 'Scale', 'Launch ideas']);

    const discrepancies = screen.getByRole('region', { name: 'Discrepancies' });
    expect(discrepancies.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(discrepancies.id).toBe('group-campaigns-discrepancies');
    expect(screen.getByRole('region', { name: 'Pause' }).id).toBe('group-campaigns-pause');
    expect(screen.getByRole('region', { name: 'Scale' }).id).toBe('group-campaigns-scale');
    expect(screen.getByRole('region', { name: 'Launch ideas' }).id).toBe('group-campaigns-launch');

    // Every card heads at level 4, below its group's <h3>, and carries its item anchor.
    expect(within(discrepancies).getByRole('heading', { level: 4, name: C0_ACTION })).toBeTruthy();
    expect(card(C0_ACTION).id).toBe(`item-${String(FIXTURE_ITEM_IDS.c0)}`);

    // The open-discrepancies banner is the page's only alert (AAP 0.5.5 Behaviour).
    expect(screen.queryByRole('alert')).toBeNull();
    await screen.findByRole('button', { name: 'Platform is correct' });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the API rank order inside a group and collapses held cards at its end', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(defaultCampaigns());

    const pause = screen.getByRole('region', { name: 'Pause' });
    const toggle = within(pause).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const heldRegion = document.getElementById('group-campaigns-pause-held');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('group-campaigns-pause-held');
    expect(heldRegion?.hidden).toBe(true);
    // The held card mounts only once the disclosure is first opened.
    expect(heldRegion?.querySelector('article')).toBeNull();

    const visibleCard = card('Pause');
    expect(visibleCard.id).toBe(`item-${String(FIXTURE_ITEM_IDS.c1)}`);
    expect(heldRegion?.contains(visibleCard)).toBe(false);

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(heldRegion?.hidden).toBe(false);

    const held = heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`);
    expect(held).not.toBeNull();
    expect(held?.textContent).toContain('Display Remarketing — US');
    expect(held?.textContent).toContain('Resolve discrepancy first');
    // The fixture's held card records no blocker id, so it links the discrepancy list.
    const link = within(held as HTMLElement).getByRole('link', {
      name: 'View open discrepancies',
      hidden: true,
    });
    expect(link.getAttribute('href')).toBe(`#${DISCREPANCY_GROUP_ANCHOR}`);

    // Collapsed again, the held card stays mounted, hidden.
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(heldRegion?.hidden).toBe(true);
    expect(heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`)).toBe(held);
    await screen.findByRole('button', { name: 'Platform is correct' });
  });

  it('shows an empty state when the section has no group with a card', async () => {
    const api = mockApi({});
    await renderCampaigns(makeSection('campaigns'), makeOverlay({ open_discrepancies: 0 }));

    const empty = document.querySelector('.ds-emptystate');
    expect(empty?.querySelector('.ds-emptystate__title')?.textContent).toBe(
      'No rule fired for this window',
    );
    expect(empty?.querySelector('.ds-emptystate__reason')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it('sends no GET /discrepancies when the section holds no C0 card', async () => {
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(
      campaignsWith([
        makeGroup({ key: 'pause', title: 'Pause', items: [pageItem(FIXTURE_ITEM_IDS.c1)] }),
        makeGroup({ key: 'scale', title: 'Scale', items: [pageItem(FIXTURE_ITEM_IDS.c2Hold)] }),
      ]),
      makeOverlay({ open_discrepancies: 0 }),
    );

    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(card('Pause')).toBeTruthy();
    expect(card('Hold: platform outcomes are not reaching Qualified Lead')).toBeTruthy();
    // Give any effect-started query a macrotask to reach the stub before counting the calls.
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(api.calls.filter((call) => call.path === '/discrepancies')).toHaveLength(0);
    expect(api.calls).toHaveLength(0);
  });

  it('renders an unknown rule as the plain card, so no item is dropped', async () => {
    const api = mockApi({});
    const future = makeItem({
      id: 3001,
      rule_id: 'C9',
      action: 'Review campaign naming',
      reason: 'A rule added to the backend later.',
      entity: { source: 'google', type: 'campaign', id: '21000399', name: 'Future — US' },
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [future]));

    const plain = card('Review campaign naming');
    expect(plain.textContent).toContain('A rule added to the backend later.');
    expect(within(plain).queryByRole('list', { name: CAMPAIGN_EVIDENCE_LABEL })).toBeNull();
    expect(within(plain).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    expect(api.calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------------------------
// C0 discrepancies
// ---------------------------------------------------------------------------------------------

describe('C0 discrepancy cards', () => {
  it('compares platform and decision, and offers live resolutions, not feedback', async () => {
    // The read is held pending, so the loading state is asserted before any list arrives.
    const read = heldResponse();
    const api = mockApi({ 'GET /discrepancies': read.handler });
    await renderCampaigns(defaultCampaigns());

    const c0 = card(C0_ACTION);
    expect(c0.textContent).toContain('Platform shows Paused, last decided Active');
    // While the first read runs, the footer holds the persistent status line, still empty, and
    // after it the spinner's own polite region; no control yet.
    const statuses = within(c0).getAllByRole('status');
    expect(statuses).toHaveLength(2);
    const line = statusLine(c0);
    expect(line.tagName).toBe('DIV');
    expect(line.childNodes).toHaveLength(0);
    expect(line.getAttribute('tabindex')).toBe('-1');
    expect(statuses[1]?.textContent).toBe(LOADING_DISCREPANCY_LABEL);
    expect(within(c0).getByText(LOADING_DISCREPANCY_LABEL)).toBeTruthy();
    expect(within(c0).queryByRole('button')).toBeNull();

    read.release(openDiscrepancies());
    await within(c0).findByRole('button', { name: 'Platform is correct' });
    expect(within(c0).getByRole('button', { name: 'Decision is correct' })).toBeTruthy();
    expect(within(c0).queryByText(LOADING_DISCREPANCY_LABEL)).toBeNull();
    // The same status line stays first, still empty, beside the controls.
    expect(statusLine(c0)).toBe(line);
    expect(line.childNodes).toHaveLength(0);
    expect(screen.queryByRole('alert')).toBeNull();
    // C0 is exempt from dismiss, snooze and done, and records no decision from its footer.
    for (const label of NON_C0_ACTIONS) {
      expect(within(c0).queryByRole('button', { name: label })).toBeNull();
    }

    // One read of the open and awaiting-platform discrepancies, and no resolved read, because
    // the list holds the card's discrepancy.
    await settle();
    const reads = api.calls.filter((call) => call.path === '/discrepancies');
    expect(reads).toHaveLength(1);
    expect(reads[0]?.method).toBe('GET');
    expect(reads[0]?.search.getAll('state')).toEqual(['open', 'awaiting_platform']);
  });

  /** The action of {@link budgetC0}. */
  const BUDGET_C0_ACTION = 'Resolve: platform budget differs from the last decided budget';

  /** The open budget discrepancy behind {@link budgetC0}. */
  function budgetDiscrepancy(): Discrepancy {
    return makeDiscrepancy('budget', { id: FIXTURE_DISCREPANCY_ID + 1 });
  }

  /** A second current C0 item, for the budget discrepancy {@link budgetDiscrepancy}. */
  function budgetC0(): Item {
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    return {
      ...c0,
      id: 3012,
      action: BUDGET_C0_ACTION,
      evidence: makeEvidence({
        ...c0.evidence,
        extra: {
          ...c0.evidence.extra,
          discrepancy_id: budgetDiscrepancy().id,
          kind: 'budget',
          resolutions: [...FIXTURE_RESOLUTIONS.budget],
        },
      }),
    };
  }

  it('reads the live list once for every current C0 card of the group', async () => {
    const api = mockApi({
      'GET /discrepancies': json(200, [makeDiscrepancy('status'), budgetDiscrepancy()]),
    });
    const { queryClient } = await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0), budgetC0()]),
    );

    const first = card(C0_ACTION);
    const other = card(BUDGET_C0_ACTION);
    await within(first).findByRole('button', { name: 'Platform is correct' });
    await within(other).findByRole('button', { name: 'Platform is correct' });
    // Each card has its own controls, for its own discrepancy.
    expect(buttonNames(first)).toEqual(['Platform is correct', 'Decision is correct']);
    expect(buttonNames(other)).toEqual(['Platform is correct', 'Decision is correct']);
    expect(first.contains(other)).toBe(false);
    // One GET /discrepancies for the group, and one observer of the list, not one per card.
    await settle();
    expect(discrepancyReads(api.calls)).toEqual([['open', 'awaiting_platform']]);
    const openList = queryClient.getQueryCache().find({
      queryKey: queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
      exact: true,
    });
    expect(openList?.getObserversCount()).toBe(1);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('gives a C0 card outside the discrepancy group a read of its own', async () => {
    // The API never puts C0 elsewhere; a card that arrives in another group still works.
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(oneGroup('pause', 'Pause', [pageItem(FIXTURE_ITEM_IDS.c0)]));

    const c0 = card(C0_ACTION);
    await within(c0).findByRole('button', { name: 'Platform is correct' });
    expect(buttonNames(c0)).toEqual(['Platform is correct', 'Decision is correct']);
    expect(statusLine(c0).childNodes).toHaveLength(0);
    await settle();
    expect(discrepancyReads(api.calls)).toEqual([['open', 'awaiting_platform']]);
  });

  it('refreshes the live list for a C0 card a newer run adds, checking until it lands', async () => {
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const handle: SectionSwap = {};
    await renderWithProviders(
      <SwappableCampaigns
        initial={oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)])}
        handle={handle}
      />,
    );
    await within(card(C0_ACTION)).findByRole('button', { name: 'Platform is correct' });

    // The cached list predates the budget discrepancy; its refresh is held, and the resolved
    // list, read meanwhile, does not hold it either.
    const refresh = heldResponse();
    api.use({ 'GET /discrepancies': refresh.handler, [RESOLVED_KEY]: json(200, []) });
    act(() => {
      handle.swap?.(
        oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0), budgetC0()]),
      );
    });

    const added = card(BUDGET_C0_ACTION);
    await waitFor(() => {
      expect(statusLine(added).textContent).toBe(DISCREPANCY_CHECKING_TEXT);
    });
    await settle();
    expect(statusLine(added).textContent).toBe(DISCREPANCY_CHECKING_TEXT);
    expect(within(added).queryByText(DISCREPANCY_NOT_LISTED_TEXT)).toBeNull();

    refresh.release(json(200, [makeDiscrepancy('status'), budgetDiscrepancy()]));
    await within(added).findByRole('button', { name: 'Platform is correct' });
    expect(statusLine(added).childNodes).toHaveLength(0);
    // The first card still has its controls, and the list was read again for the new card.
    expect(buttonNames(card(C0_ACTION))).toEqual(['Platform is correct', 'Decision is correct']);
    expect(
      discrepancyReads(api.calls).filter((states) => states[0] === 'open'),
    ).toHaveLength(2);
  });

  it('keeps a C0 card, and focus on its resolution, when a newer run gives it a new id', async () => {
    // Two discrepancies on one subject: their cards are told apart by their discrepancy ids.
    mockApi({
      'GET /discrepancies': json(200, [makeDiscrepancy('status'), budgetDiscrepancy()]),
    });
    const handle: SectionSwap = {};
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    await renderWithProviders(
      <SwappableCampaigns
        initial={oneGroup('discrepancies', 'Discrepancies', [c0, budgetC0()])}
        handle={handle}
      />,
    );
    const shown = card(C0_ACTION);
    const other = card(BUDGET_C0_ACTION);
    const resolution = await within(shown).findByRole('button', { name: 'Platform is correct' });
    act(() => {
      resolution.focus();
    });

    // Both discrepancies recomputed by the next run: new recommendation ids, nothing else.
    const budget = budgetC0();
    act(() => {
      handle.swap?.(
        oneGroup('discrepancies', 'Discrepancies', [
          { ...c0, id: c0.id + 1000 },
          { ...budget, id: budget.id + 1000 },
        ]),
      );
    });
    await settle();

    expect(card(BUDGET_C0_ACTION)).toBe(other);
    expect(card(C0_ACTION)).toBe(shown);
    expect(shown.id).toBe(`item-${String(c0.id + 1000)}`);
    expect(within(shown).getByRole('button', { name: 'Platform is correct' })).toBe(resolution);
    expect(document.activeElement).toBe(resolution);
  });

  it('offers exactly the resolutions the API lists, never derived ones', async () => {
    // The run froze a status discrepancy (its extra lists platform_correct and
    // decision_correct); the live list now reports it as untracked, and the card follows the API.
    mockApi({
      'GET /discrepancies': json(200, [
        makeDiscrepancy('untracked', {
          id: FIXTURE_DISCREPANCY_ID,
          resolutions: [...FIXTURE_RESOLUTIONS.untracked],
        }),
      ]),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByRole('button', { name: 'Adopt platform state' });
    // Exactly the two untracked resolutions, in the API's order, and no other control.
    expect(buttonNames(c0)).toEqual(['Adopt platform state', 'Record intended state']);
    expect(within(c0).queryByRole('button', { name: 'Platform is correct' })).toBeNull();
    expect(within(c0).queryByRole('button', { name: 'Decision is correct' })).toBeNull();
    for (const label of NON_C0_ACTIONS) {
      expect(within(c0).queryByRole('button', { name: label })).toBeNull();
    }
    // The frozen comparison line stays on the card beside the live controls.
    expect(c0.textContent).toContain('Platform shows Paused, last decided Active');
  });

  it('renders a single resolution when that is all the API lists', async () => {
    mockApi({
      'GET /discrepancies': json(200, [
        makeDiscrepancy('missing', {
          id: FIXTURE_DISCREPANCY_ID,
          resolutions: ['acknowledge_removal'],
        }),
      ]),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByRole('button', { name: 'Acknowledge removal' });
    expect(buttonNames(c0)).toEqual(['Acknowledge removal']);
  });

  /** Handlers for a resolution of the default discrepancy that closes it at once. */
  function closingResolution(): MockHandlers {
    return {
      'GET /discrepancies': openDiscrepancies(),
      'POST /discrepancies/:id/resolve': (call) =>
        json(
          200,
          makeDiscrepancy('status', {
            id: Number(call.params.id),
            state: 'resolved',
            resolution: 'platform_correct',
            resolved_at: '2026-10-06T09:30:00Z',
          }),
        ),
    };
  }

  it('resolves the live discrepancy, then says so in the same status line, focused', async () => {
    const api = mockApi(closingResolution());
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    const platformCorrect = await within(c0).findByRole('button', { name: 'Platform is correct' });
    const line = statusLine(c0);
    // The refetched unresolved list drops it, and the resolved list holds it.
    api.use({ 'GET /discrepancies': json(200, []), [RESOLVED_KEY]: resolvedDiscrepancies() });
    act(() => {
      platformCorrect.focus();
    });
    expect(document.activeElement).toBe(platformCorrect);
    fireEvent.click(platformCorrect);

    await waitFor(() => {
      expect(line.textContent).toBe(RESOLVED_SINCE_RUN_TEXT);
    });
    // The same persistent status line carries the outcome, and it took the focus the removed
    // controls lost.
    expect(statusLine(c0)).toBe(line);
    expect(line.isConnected).toBe(true);
    expect(document.activeElement).toBe(line);
    const post = api.calls.find((call) => call.method === 'POST');
    expect(post?.path).toBe(`/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`);
    expect(post?.body).toEqual({ resolution: 'platform_correct' });
    expect(within(c0).queryByRole('button')).toBeNull();
    // The resolved list is read once, only after the unresolved one lacked the discrepancy.
    expect(discrepancyReads(api.calls).filter((states) => states[0] === 'resolved')).toEqual([
      ['resolved'],
    ]);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('leaves focus the user moved elsewhere where it is when the controls leave', async () => {
    const api = mockApi(closingResolution());
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    const platformCorrect = await within(c0).findByRole('button', { name: 'Platform is correct' });
    const line = statusLine(c0);
    const elsewhere = document.createElement('a');
    elsewhere.href = '#elsewhere';
    elsewhere.textContent = 'Elsewhere';
    document.body.append(elsewhere);
    try {
      api.use({ 'GET /discrepancies': json(200, []), [RESOLVED_KEY]: resolvedDiscrepancies() });
      fireEvent.click(platformCorrect);
      elsewhere.focus();

      await waitFor(() => {
        expect(line.textContent).toBe(RESOLVED_SINCE_RUN_TEXT);
      });
      expect(document.activeElement).toBe(elsewhere);
    } finally {
      elsewhere.remove();
    }
  });

  /** Unresolved lists that no longer hold the C0 card's discrepancy. */
  const listsWithoutIt: readonly [string, () => Discrepancy[]][] = [
    ['is empty', () => []],
    [
      'holds only another discrepancy',
      () => [makeDiscrepancy('budget', { id: FIXTURE_DISCREPANCY_ID + 1 })],
    ],
  ];

  it.each(listsWithoutIt)(
    'reads as resolved since the run when the list %s and the resolved list holds it',
    async (_, list) => {
      const api = mockApi({
        'GET /discrepancies': json(200, list()),
        [RESOLVED_KEY]: resolvedDiscrepancies(),
      });
      await renderCampaigns(
        oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
      );

      const c0 = card(C0_ACTION);
      await waitFor(() => {
        expect(statusLine(c0).textContent).toBe(RESOLVED_SINCE_RUN_TEXT);
      });
      expect(within(c0).queryByRole('button')).toBeNull();
      expect(within(c0).queryByText(DISCREPANCY_NOT_LISTED_TEXT)).toBeNull();
      // The frozen comparison still states what the run saw.
      expect(c0.textContent).toContain('Platform shows Paused, last decided Active');
      await settle();
      expect(discrepancyReads(api.calls)).toEqual([['open', 'awaiting_platform'], ['resolved']]);
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it.each(listsWithoutIt)(
    'says it is no longer listed, never resolved, when the list %s and the resolved list lacks it',
    async (_, list) => {
      mockApi({
        'GET /discrepancies': json(200, list()),
        [RESOLVED_KEY]: json(200, [
          makeDiscrepancy('budget', { id: FIXTURE_DISCREPANCY_ID + 2, state: 'resolved' }),
        ]),
      });
      await renderCampaigns(
        oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
      );

      const c0 = card(C0_ACTION);
      await waitFor(() => {
        expect(statusLine(c0).textContent).toBe(DISCREPANCY_NOT_LISTED_TEXT);
      });
      expect(within(c0).queryByText(RESOLVED_SINCE_RUN_TEXT)).toBeNull();
      expect(within(c0).queryByRole('button')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('checks the resolved list before it says anything about a closure', async () => {
    const resolvedRead = heldResponse();
    mockApi({ 'GET /discrepancies': json(200, []), [RESOLVED_KEY]: resolvedRead.handler });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await waitFor(() => {
      expect(statusLine(c0).textContent).toBe(DISCREPANCY_CHECKING_TEXT);
    });
    const line = statusLine(c0);
    expect(within(c0).queryByText(RESOLVED_SINCE_RUN_TEXT)).toBeNull();
    expect(within(c0).queryByRole('button')).toBeNull();

    resolvedRead.release(resolvedDiscrepancies());
    await waitFor(() => {
      expect(line.textContent).toBe(RESOLVED_SINCE_RUN_TEXT);
    });
    expect(statusLine(c0)).toBe(line);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it("shows the API's message when the resolved list cannot be read", async () => {
    mockApi({
      'GET /discrepancies': json(200, []),
      [RESOLVED_KEY]: apiError(503, 'database_unavailable', 'The database is unavailable.'),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await waitFor(() => {
      expect(statusLine(c0).textContent).toBe(
        `${DISCREPANCY_LOAD_ERROR_PREFIX}: The database is unavailable.`,
      );
    });
    expect(within(c0).queryByText(RESOLVED_SINCE_RUN_TEXT)).toBeNull();
    expect(within(c0).queryByRole('button')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it("shows the API's message when the live list cannot be read", async () => {
    mockApi({
      'GET /discrepancies': apiError(503, 'database_unavailable', 'The database is unavailable.'),
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByText(`${DISCREPANCY_LOAD_ERROR_PREFIX}: The database is unavailable.`);
    // The failure is announced by the persistent status line, not by an alert.
    expect(statusLine(c0).textContent).toBe(
      `${DISCREPANCY_LOAD_ERROR_PREFIX}: The database is unavailable.`,
    );
    expect(within(c0).queryByText(LOADING_DISCREPANCY_LABEL)).toBeNull();
    expect(within(c0).queryByRole('button')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the controls of a list already read when its refresh fails', async () => {
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const { queryClient } = await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    const c0 = card(C0_ACTION);
    await within(c0).findByRole('button', { name: 'Platform is correct' });
    api.use({
      'GET /discrepancies': apiError(503, 'database_unavailable', 'The database is unavailable.'),
    });
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    await waitFor(() => {
      expect(discrepancyReads(api.calls)).toHaveLength(2);
    });
    await settle();
    expect(buttonNames(c0)).toEqual(['Platform is correct', 'Decision is correct']);
    expect(statusLine(c0).childNodes).toHaveLength(0);
    expect(within(c0).queryByText(/^Could not load this discrepancy/u)).toBeNull();
    // A list already read is kept, so no resolved read follows the failed refresh.
    expect(discrepancyReads(api.calls).filter((states) => states[0] === 'resolved')).toEqual([]);
  });

  it('falls back to a generic detail when the failed read carries a blank message', async () => {
    mockApi({ 'GET /discrepancies': apiError(500, 'internal_error', '   ') });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [pageItem(FIXTURE_ITEM_IDS.c0)]),
    );

    await within(card(C0_ACTION)).findByText(
      `${DISCREPANCY_LOAD_ERROR_PREFIX}: the request failed`,
    );
  });

  it('says details are unavailable, reading nothing, without a discrepancy id', async () => {
    const api = mockApi({});
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    const orphan: Item = {
      ...c0,
      evidence: makeEvidence({ ...c0.evidence, extra: { kind: 'status', shape: 'own_budget' } }),
    };
    await renderCampaigns(oneGroup('discrepancies', 'Discrepancies', [orphan]));

    const article = card(C0_ACTION);
    expect(within(article).getByText(DISCREPANCY_UNAVAILABLE_TEXT)).toBeTruthy();
    expect(article.textContent).not.toContain('Platform shows');
    expect(within(article).queryByRole('button')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  /** The default page's C0 item as the overlay marks it once its dataset was replaced. */
  function historicalC0(extra?: Record<string, unknown>): Item {
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    return {
      ...c0,
      evidence:
        extra === undefined
          ? c0.evidence
          : makeEvidence({ ...c0.evidence, extra: { ...c0.evidence.extra, ...extra } }),
      // The overlay holds every item computed from replaced data, C0 included (AAP 0.3.2).
      current: makeItemOverlay({
        historical: true,
        held_now: true,
        held_reason_now: 'Computed from data that has since been replaced',
        data_status_now: 'live',
      }),
    };
  }

  it('pins a historical C0 with its frozen resolutions disabled and reads nothing', async () => {
    // No handler: any discrepancy read or resolution would fail the test.
    const api = mockApi({});
    await renderCampaigns(oneGroup('discrepancies', 'Discrepancies', [historicalC0()]));

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    // Pinned, never behind the held disclosure nor moved into the replaced-data block.
    expect(
      within(group).queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
    ).toBeNull();
    expect(
      within(group).queryByRole('group', { name: /^Computed from replaced data/u }),
    ).toBeNull();
    const article = card(C0_ACTION);
    expect(group.contains(article)).toBe(true);
    expect(article.textContent).toContain('Computed from replaced data');
    expect(
      within(article).queryByRole('link', { name: /^View (blocking|open) discrepanc/u }),
    ).toBeNull();
    // The frozen comparison and the history-only notice, in the status line.
    expect(article.textContent).toContain('Platform shows Paused, last decided Active');
    expect(statusLine(article).textContent).toBe(HISTORICAL_RESOLUTION_NOTICE);

    // The resolutions the run froze (platform_correct, decision_correct), in that order, every
    // one disabled, and none a feedback action.
    expect(buttonNames(article)).toEqual(['Platform is correct', 'Decision is correct']);
    for (const button of within(article).getAllByRole('button')) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    fireEvent.click(within(article).getByRole('button', { name: 'Platform is correct' }));
    fireEvent.click(within(article).getByRole('button', { name: 'Decision is correct' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    // A replaced discrepancy is never in the active-dataset lists, so none is read, and its
    // absence is never taken for a resolution.
    await settle();
    expect(api.calls).toHaveLength(0);
    expect(within(article).queryByText(RESOLVED_SINCE_RUN_TEXT)).toBeNull();
    expect(within(article).queryByText(DISCREPANCY_NOT_LISTED_TEXT)).toBeNull();
    expect(within(article).queryByText(LOADING_DISCREPANCY_LABEL)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a historical C0 its labelled frozen resolutions once each, in order', async () => {
    const api = mockApi({});
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [
        historicalC0({
          kind: 'missing',
          // An unlabelled key and a repeat are left out; the rest keep the frozen order.
          resolutions: [
            'decision_correct',
            'retire_subject',
            'acknowledge_removal',
            'decision_correct',
          ],
        }),
      ]),
    );

    const article = card(C0_ACTION);
    expect(buttonNames(article)).toEqual(['Decision is correct', 'Acknowledge removal']);
    expect(statusLine(article).textContent).toBe(HISTORICAL_RESOLUTION_NOTICE);
    await settle();
    expect(api.calls).toHaveLength(0);
  });

  it('reads the live list only for the current C0 card beside a historical one', async () => {
    const api = mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const current = { ...pageItem(FIXTURE_ITEM_IDS.c0), id: 3013, action: 'Resolve: current' };
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [
        historicalC0({ discrepancy_id: FIXTURE_DISCREPANCY_ID + 5 }),
        current,
      ]),
    );

    const live = card('Resolve: current');
    const platformCorrect = await within(live).findByRole('button', {
      name: 'Platform is correct',
    });
    expect((platformCorrect as HTMLButtonElement).disabled).toBe(false);
    const frozen = card(C0_ACTION);
    expect(statusLine(frozen).textContent).toBe(HISTORICAL_RESOLUTION_NOTICE);
    for (const button of within(frozen).getAllByRole('button')) {
      expect((button as HTMLButtonElement).disabled).toBe(true);
    }
    // The historical card's discrepancy is absent from the list, yet no resolved read follows,
    // because only current cards are looked up.
    await settle();
    expect(discrepancyReads(api.calls)).toEqual([['open', 'awaiting_platform']]);
  });

  it('pins every C0 first, then the visible cards, the replaced-data block and the held disclosure', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const replaced = (): Item['current'] =>
      makeItemOverlay({
        historical: true,
        held_now: true,
        held_reason_now: 'Computed from data that has since been replaced',
        data_status_now: 'live',
      });
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    const historicalC0: Item = { ...c0, id: 3010, current: replaced() };
    const historicalPause = makeItem({
      id: 3011,
      rule_id: 'C1',
      action: 'Pause replaced campaign',
      current: replaced(),
    });
    const visible = makeItem({ id: 3003, rule_id: 'C1', action: 'Pause visible campaign' });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [
        visible,
        historicalPause,
        c0,
        pageItem(FIXTURE_ITEM_IDS.heldPause),
        historicalC0,
      ]),
    );

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    const block = within(group).getByRole('group', { name: 'Computed from replaced data (1)' });
    expect(block.id).toBe(`${DISCREPANCY_GROUP_ANCHOR}-historical`);
    expect(within(block).getAllByRole('article').map((article) => article.id)).toEqual([
      'item-3011',
    ]);
    const replacedCard = within(block).getByRole('article', { name: 'Pause replaced campaign' });
    expect(replacedCard.textContent).toContain('Computed from replaced data');
    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    const heldRegion = document.getElementById(`${DISCREPANCY_GROUP_ANCHOR}-held`);

    // Both C0 cards, current and historical, in API order, then the visible card.
    const pinned = [...group.querySelectorAll('article')].filter(
      (article) => !block.contains(article) && heldRegion?.contains(article) !== true,
    );
    expect(pinned.map((article) => article.id)).toEqual([
      `item-${String(FIXTURE_ITEM_IDS.c0)}`,
      'item-3010',
      'item-3003',
    ]);
    const lastPinned = pinned.at(-1);
    expect(
      lastPinned !== undefined &&
        (lastPinned.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
    ).toBe(true);
    expect((block.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );
    // The historical C0 shows its frozen resolutions at once and the current one its live list's;
    // let that read settle before the test ends.
    await waitFor(() => {
      expect(within(group).getAllByRole('button', { name: 'Platform is correct' })).toHaveLength(2);
    });
  });

  it('pins C0 cards and puts other held items behind the disclosure', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    const held = pageItem(FIXTURE_ITEM_IDS.heldPause);
    const hidden = makeItem({
      id: 3002,
      rule_id: 'C1',
      action: 'Pause dismissed campaign',
      current: makeItemOverlay({ hidden_by: 'dismiss' }),
    });
    const visible = makeItem({
      id: 3003,
      rule_id: 'C1',
      action: 'Pause visible campaign',
    });
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', [
        held,
        pageItem(FIXTURE_ITEM_IDS.c0),
        hidden,
        visible,
      ]),
    );

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    const toggle = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    expect(toggle.getAttribute('aria-controls')).toBe(`${DISCREPANCY_GROUP_ANCHOR}-held`);
    const heldRegion = document.getElementById(`${DISCREPANCY_GROUP_ANCHOR}-held`);
    expect(heldRegion?.hidden).toBe(true);
    expect(heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`)).toBeNull();
    fireEvent.click(toggle);
    expect(heldRegion?.querySelector(`#item-${String(FIXTURE_ITEM_IDS.heldPause)}`)).not.toBeNull();

    // The C0 and the visible card are pinned in API order, before the disclosure.
    const pinned = [...group.querySelectorAll('article')].filter(
      (article) => heldRegion?.contains(article) !== true,
    );
    expect(pinned.map((article) => article.id)).toEqual([
      `item-${String(FIXTURE_ITEM_IDS.c0)}`,
      'item-3003',
    ]);
    // The hidden card is skipped here and counted by the frame.
    expect(
      screen.queryByRole('article', { name: 'Pause dismissed campaign', hidden: true }),
    ).toBeNull();
    expect(
      listLines(screen.getByRole('region', { name: 'Campaigns to pause or launch' }), 'Not shown'),
    ).toEqual(['1 hidden since this run']);
    await within(card(C0_ACTION)).findByRole('button', { name: 'Platform is correct' });
  });
});

// ---------------------------------------------------------------------------------------------
// Empty discrepancy group
// ---------------------------------------------------------------------------------------------

describe('the empty discrepancy group', () => {
  /** A section whose discrepancy group is empty in the run, beside one pause card. */
  function lateSection(): Section {
    return campaignsWith([
      makeGroup({ key: 'discrepancies', title: 'Discrepancies', items: [] }),
      makeGroup({ key: 'pause', title: 'Pause', items: [pageItem(FIXTURE_ITEM_IDS.c1)] }),
      makeGroup({ key: 'launch', title: 'Launch ideas', items: [] }),
    ]);
  }

  it('stays anchored with the live count and a /decisions link while open', async () => {
    const api = mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: 3 }));

    const group = screen.getByRole('region', { name: 'Discrepancies' });
    expect(group.id).toBe(DISCREPANCY_GROUP_ANCHOR);
    expect(group.textContent).toContain(
      '3 open discrepancies detected since this run was computed',
    );
    const link = within(group).getByRole('link', { name: RESOLVE_ON_DECISIONS_LABEL });
    expect(link.getAttribute('href')).toBe('/decisions');
    // It is still first, and every other empty group is left out.
    expect(
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Pause']);
    // The notice reads no list.
    expect(api.calls).toHaveLength(0);
  });

  it('uses the singular for one open discrepancy', async () => {
    mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: 1 }));
    expect(screen.getByRole('region', { name: 'Discrepancies' }).textContent).toContain(
      '1 open discrepancy detected since this run was computed',
    );
  });

  it.each([
    ['zero', 0],
    ['a fraction', 1.5],
    ['a negative count', -2],
    ['NaN', Number.NaN],
  ])('is left out when the live count is %s', async (_label, count) => {
    mockApi({});
    await renderCampaigns(lateSection(), makeOverlay({ open_discrepancies: count }));
    expect(screen.queryByRole('region', { name: 'Discrepancies' })).toBeNull();
    expect(document.getElementById(DISCREPANCY_GROUP_ANCHOR)).toBeNull();
    expect(screen.getByRole('region', { name: 'Pause' })).toBeTruthy();
  });

  it('keeps the anchor in an otherwise empty section beside its empty state', async () => {
    mockApi({});
    await renderCampaigns(
      oneGroup('discrepancies', 'Discrepancies', []),
      makeOverlay({ open_discrepancies: 2 }),
    );
    expect(document.getElementById(DISCREPANCY_GROUP_ANCHOR)?.textContent).toContain(
      '2 open discrepancies detected since this run was computed',
    );
    expect(document.querySelector('.ds-emptystate__title')?.textContent).toBe(
      'No rule fired for this window',
    );
  });
});

// ---------------------------------------------------------------------------------------------
// Pause, scale and hold field sets (C1, C1b, C2)
// ---------------------------------------------------------------------------------------------

/** The CSRF header every mutating request carries (AAP 0.5.3). */
const CSRF = ['X-Requested-With', 'ads-engine'] as const;

/** The footer of a C2 "Scale" card, which records a decision (AAP 0.5.5 Footer actions). */
const SCALE_FOOTER = [RECORD_DECISION_LABEL, 'Dismiss', 'Snooze for 7 days'] as const;

/** The footer of a C2 "Hold" card, which writes no decision and is marked done instead. */
const HOLD_FOOTER = ['Mark done', 'Dismiss', 'Snooze for 7 days'] as const;

/** The id of {@link ownBudgetScaleItem}, outside {@link FIXTURE_ITEM_IDS}. */
const OWN_BUDGET_SCALE_ID = 3004;

/** The id of {@link sharedBudgetScaleItem}, outside {@link FIXTURE_ITEM_IDS}. */
const SHARED_BUDGET_SCALE_ID = 3014;

/** One complete dataset per name, covered through the window end, as of compute time. */
function coverage(...names: readonly string[]): DatasetCoverage[] {
  return names.map((dataset) => ({
    dataset,
    completeness: 'complete',
    covered_through: FIXTURE_WINDOW.end,
  }));
}

/**
 * A C2 item's `evidence.thresholds`, built fresh on each call: `campaigns.C2_THRESHOLD_KEYS`
 * and, when the pipeline veto was read, `VETO_THRESHOLD_KEYS`, with their configured values,
 * then the policy keys precedence freezes into every item.
 */
function c2Thresholds(veto: boolean): Record<string, unknown> {
  return {
    'scale.min_outcomes': 5,
    'scale.max_cpa_ratio': 1,
    'b1.step': 0.2,
    'window.sustained_min_days': 7,
    'budget.max_observation_gap_hours': 6,
    'budget.capped_day_ratio': 0.95,
    'budget.capped_lost_is_share': 0.1,
    'budget.high_utilization': 0.9,
    'budget.under_utilization': 0.5,
    'budget.min_known_days': 7,
    'budget.min_spending_days': 5,
    'budget.min_capped_days': 5,
    'budget.sustained_share': 0.7,
    ...(veto
      ? {
          'crm.veto_min_leads': 20,
          'crm.lead_maturity_days': 14,
          'attribution.floor_share': 0.1,
          'crm.meeting_outcome_exclusions': ['CANCELED'],
        }
      : {}),
    'sources.stale_after_hours': 24,
    'window.trusted_min_days': 14,
  };
}

/**
 * A C2 "Scale" on the LinkedIn campaign "Lead Gen — Benchmarks US"
 * ({@link FIXTURE_CAMPAIGN_IDS}.benchmarksUs, own daily budget USD 60.00): a selected
 * display-contract example modelled on the own-budget "Scale" shape of `campaigns._c2_candidate`,
 * not the exact payload it writes, with the cap gate holding and the pipeline veto passing
 * (6 Qualified Leads): the veto counts beside the outcomes, its B1 item's proposed daily budget
 * (USD 72.00, +20%) as a metric and in `extra.b1_link`, its campaign group, and the campaign as
 * its decision subject.
 */
function ownBudgetScaleItem(): Item {
  return makeItem({
    id: OWN_BUDGET_SCALE_ID,
    rank: 1,
    rule_id: 'C2',
    action: 'Scale',
    reason:
      'Capped on 21 of 26 known-budget days with 48 leads at USD 31.25 per lead (account ' +
      'USD 40.00): raise the daily budget from USD 60.00 to USD 72.00 (B1).',
    entity: {
      source: 'linkedin',
      type: 'campaign',
      id: '700000601',
      name: 'Lead Gen — Benchmarks US',
    },
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', '1500.00', 'currency', 'USD'),
        metric('window_days', 'Window days', 28, 'days'),
        metric('outcomes', 'Window leads', 48, 'leads'),
        metric('expected_outcomes', 'Expected leads at the account baseline', '37.5', 'leads'),
        metric('cost_per_outcome', 'Cost per lead', '31.25', 'currency', 'USD'),
        metric('daily_budget', 'Latest effective daily budget', '60.00', 'currency', 'USD'),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.96', 'ratio'),
        metric('known_days', 'Known-budget days', 26, 'days'),
        metric('spending_days', 'Spending days', 26, 'days'),
        metric('capped_days', 'Capped days', 21, 'days'),
        metric('idle_days', 'Known idle days', 0, 'days'),
        metric('changed_intraday_days', 'Changed-intraday days (unknown budget)', 1, 'days'),
        metric('unobserved_days', 'Unobserved days (unknown budget)', 1, 'days'),
        metric('proposed_daily_budget', 'Proposed daily budget (B1)', '72.00', 'currency', 'USD'),
        metric('leads', 'LinkedIn leads (platform)', 48, 'leads'),
        metric('matured_leads', 'Matured leads (at least 14 days before window end)', 30, 'leads'),
        metric('matched_leads', 'Matched matured leads', 28, 'leads'),
        metric('unresolved', 'Unresolved matured leads', 2, 'leads'),
        metric('unresolved_share', 'Unresolved share of matured leads', '0.0667', 'ratio'),
        metric('eligible_contacts', 'Eligible contacts', 26, 'contacts'),
        metric(
          'unknown_evidence_share',
          'Share of eligible contacts with unknown CRM evidence',
          '0',
          'ratio',
        ),
        metric('mql', 'MQL (First meeting booked)', 9, 'contacts'),
        metric('ql', 'Qualified Leads', 6, 'contacts'),
        metric('meetings', 'Meetings', 4, 'meetings'),
        metric('young_leads', 'Leads younger than 14 days (not judged)', 18, 'leads'),
        metric('young_ql', 'Qualified Leads from those leads (not judged)', 1, 'contacts'),
      ],
      baselines: [
        {
          key: 'baseline_cost_per_outcome',
          label: 'Account cost per lead (same source and objective)',
          value: '40.00',
        },
        {
          key: 'max_cost_per_outcome',
          label: 'Highest cost per outcome allowed to scale',
          value: '40.00',
        },
      ],
      sample: { observed: 26, required: 7, unit: 'known-budget days', projected: null },
      thresholds: c2Thresholds(true),
      datasets: coverage(
        'linkedin.analytics_daily',
        'linkedin.entities',
        'linkedin.leads',
        'hubspot.contacts',
        'hubspot.associations',
      ),
      provenance: [
        {
          source: 'linkedin',
          dataset_id: FIXTURE_DATASET_IDS.linkedin,
          mode: 'simulated',
          role: 'primary',
        },
        {
          source: 'hubspot',
          dataset_id: FIXTURE_DATASET_IDS.hubspotCompanion,
          mode: 'simulated',
          role: 'companion',
        },
      ],
      // Modelled on the "Scale" extra of backend/src/ads_engine/analysis/campaigns.py
      // (_c2_candidate), not its exact payload: `capped_share` carries ratios here, where
      // `metrics.cap_constrained` records capped days against whole days (21 of ⌈0.7 × 26⌉ = 19).
      extra: {
        outcome: 'Scale',
        b1: true,
        gate: {
          known_budget_days: { observed: 26, required: 7 },
          spending_days: { observed: 26, required: 5 },
          capped_days: { observed: 21, required: 5 },
          capped_share: { observed: '0.8077', required: '0.7' },
        },
        observation_assumption: 'unchanged across 216 observations, at most 6 h apart',
        idle_dates: [],
        changed_intraday_dates: ['2026-09-17'],
        unobserved_dates: ['2026-09-08'],
        b1_link: {
          rule_id: 'B1',
          subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
          current_daily_budget: '60.00',
          proposed_daily_budget: '72.00',
        },
        campaign_group: {
          state: 'loaded',
          id: 611,
          external_id: '600000611',
          name: 'Benchmarks — US',
          status: 'ACTIVE',
          daily_budget: null,
          total_budget: null,
          currency: 'USD',
        },
        matured_ql: 6,
      },
    }),
    flags: [makeFlag('simulated')],
    decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
  });
}

/**
 * The members of the shared "Search Shared Budget" with their member days, as
 * `campaigns._members_extra` records them: the fixture world's shared member and a second
 * campaign built for this suite.
 */
const SEARCH_BUDGET_MEMBERS = [
  {
    campaign_id: FIXTURE_CAMPAIGN_IDS.searchPartners,
    name: 'Search Partners — US',
    member_days: 28,
  },
  { campaign_id: 309, name: 'Search Competitors — US', member_days: 21 },
];

/**
 * A C2 "Scale" on the Google explicitly shared "Search Shared Budget"
 * ({@link FIXTURE_BUDGET_IDS}.search, daily budget USD 500.00): a selected display-contract
 * example modelled on the shared-budget "Scale" shape of `campaigns._c2_candidate`, not the exact
 * payload it writes, with the cap gate holding over its budget-active days and no Google CRM link
 * (no veto read, flagged `no_crm_link`): its B1 item's proposed daily budget (USD 600.00, +20%),
 * every member campaign, and the budget, never a member, as its decision subject (AAP 0.5.4.5).
 */
function sharedBudgetScaleItem(): Item {
  return makeItem({
    id: SHARED_BUDGET_SCALE_ID,
    rank: 1,
    rule_id: 'C2',
    action: 'Scale',
    reason:
      'Capped on 16 of 20 budget-active days with 125 conversions at USD 76.00 per conversion ' +
      '(account USD 95.00): raise the daily budget from USD 500.00 to USD 600.00 (B1).',
    entity: { source: 'google', type: 'budget', id: '31000402', name: 'Search Shared Budget' },
    evidence: makeEvidence({
      metrics: [
        metric('spend', 'Window spend', '9500.00', 'currency', 'USD'),
        metric('window_days', 'Window days', 28, 'days'),
        metric('outcomes', 'Window conversions', '125', 'conversions'),
        metric(
          'expected_outcomes',
          'Expected conversions at the account baseline',
          '100',
          'conversions',
        ),
        metric('cost_per_outcome', 'Cost per conversion', '76.00', 'currency', 'USD'),
        metric('daily_budget', 'Latest effective daily budget', '500.00', 'currency', 'USD'),
        metric('window_utilization', 'Window utilization (spend vs cap)', '0.95', 'ratio'),
        metric('known_days', 'Budget-active days', 20, 'days'),
        metric('spending_days', 'Spending days', 20, 'days'),
        metric('capped_days', 'Capped days', 16, 'days'),
        metric('idle_days', 'Known idle days', 0, 'days'),
        metric('changed_intraday_days', 'Changed-intraday days (unknown budget)', 2, 'days'),
        metric('unobserved_days', 'Unobserved days (unknown budget)', 6, 'days'),
        metric('proposed_daily_budget', 'Proposed daily budget (B1)', '600.00', 'currency', 'USD'),
      ],
      baselines: [
        {
          key: 'baseline_cost_per_outcome',
          label: 'Account cost per conversion (same source and objective)',
          value: '95.00',
        },
        {
          key: 'max_cost_per_outcome',
          label: 'Highest cost per outcome allowed to scale',
          value: '95.00',
        },
      ],
      sample: { observed: 20, required: 7, unit: 'budget-active days', projected: null },
      thresholds: c2Thresholds(false),
      datasets: coverage('google.metrics_daily', 'google.entities'),
      // Modelled on the shared-budget "Scale" extra of
      // backend/src/ads_engine/analysis/campaigns.py (_c2_candidate), not its exact payload:
      // `capped_share` carries ratios here, where `metrics.cap_constrained` records capped days
      // against whole days (16 of ⌈0.7 × 20⌉ = 14).
      extra: {
        outcome: 'Scale',
        b1: true,
        gate: {
          known_budget_days: { observed: 20, required: 7 },
          spending_days: { observed: 20, required: 5 },
          capped_days: { observed: 16, required: 5 },
          capped_share: { observed: '0.8', required: '0.7' },
        },
        observation_assumption: 'unchanged across 168 observations, at most 6 h apart',
        idle_dates: [],
        changed_intraday_dates: ['2026-09-15', '2026-09-22'],
        unobserved_dates: [
          '2026-09-08',
          '2026-09-09',
          '2026-09-10',
          '2026-09-11',
          '2026-09-12',
          '2026-09-13',
        ],
        b1_link: {
          rule_id: 'B1',
          subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
          current_daily_budget: '500.00',
          proposed_daily_budget: '600.00',
        },
        members: SEARCH_BUDGET_MEMBERS,
      },
    }),
    flags: [makeFlag('simulated'), makeFlag('no_crm_link'), makeFlag('shared_budget')],
    decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
  });
}

/**
 * The baseline decision of "Lead Gen — Benchmarks US", the subject of
 * {@link ownBudgetScaleItem}: own budget, status `active`, daily budget USD 60.00, as
 * `GET /decisions?subject_type=campaign&subject_id=601` lists it, with `overrides` applied
 * (shallow).
 */
function benchmarksUsDecision(overrides: Partial<Decision> = {}): Decision {
  return makeDecision('own_budget', {
    subject: {
      type: 'campaign',
      id: FIXTURE_CAMPAIGN_IDS.benchmarksUs,
      name: 'Lead Gen — Benchmarks US',
      source: 'linkedin',
      currency: 'USD',
    },
    daily_budget: '60.00',
    ...overrides,
  });
}

/** The `[subject_type, subject_id]` every `GET /decisions` the stub received carried, in order. */
function decisionReads(calls: readonly RecordedCall[]): (string | null)[][] {
  return calls
    .filter((call) => call.method === 'GET' && call.path === '/decisions')
    .map((call) => [call.search.get('subject_type'), call.search.get('subject_id')]);
}

/** Every `POST /decisions` the stub received, in order. */
function decisionPosts(calls: readonly RecordedCall[]): RecordedCall[] {
  return calls.filter((call) => call.method === 'POST' && call.path === '/decisions');
}

/**
 * Opens the "Record decision" dialog of the card `scope` and returns it once its form has
 * loaded the subject's decision log (its "Save decision" button is then rendered).
 */
async function openDecisionDialog(scope: HTMLElement): Promise<HTMLElement> {
  fireEvent.click(within(scope).getByRole('button', { name: RECORD_DECISION_LABEL }));
  const dialog = await screen.findByRole('dialog', { name: RECORD_DECISION_LABEL });
  await within(dialog).findByRole('button', { name: 'Save decision' });
  return dialog;
}

describe('pause and scale cards', () => {
  it('shows a C1 card spend, outcomes and expected outcomes, no pipeline', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('pause', 'Pause', [pageItem(FIXTURE_ITEM_IDS.c1)]));

    const c1 = card('Pause');
    expect(listLines(c1, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: USD\u00a0314.50',
      'Window conversions: 0',
      'Expected conversions at the account baseline: 3.4',
    ]);
    expect(within(c1).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
  });

  it('adds the CRM Qualified Leads to a linked C1 card', async () => {
    mockApi({});
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const linked: Item = {
      ...c1,
      evidence: makeEvidence({
        ...c1.evidence,
        metrics: [...c1.evidence.metrics, metric('ql', 'Qualified Leads (CRM)', 2, 'contacts')],
      }),
    };
    await renderCampaigns(oneGroup('pause', 'Pause', [linked]));

    expect(listLines(card('Pause'), CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: USD\u00a0314.50',
      'Window conversions: 0',
      'Expected conversions at the account baseline: 3.4',
      'Qualified Leads (CRM): 2',
    ]);
  });

  it('shows a C2 "Hold" the pipeline counts, MQL and QL in separate columns', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('scale', 'Scale', [pageItem(FIXTURE_ITEM_IDS.c2Hold)]));

    const hold = card('Hold: platform outcomes are not reaching Qualified Lead');
    expect(listLines(hold, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: USD\u00a01,500.00',
      'Window leads: 48',
      'Qualified Leads: 0',
    ]);
    const pipeline = tableOf(hold, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual([
      'Matured leads',
      'Matched matured leads',
      'Unresolved matured leads',
      'Eligible contacts',
      'MQL (First meeting booked)',
      'Qualified Leads',
      'Meetings',
    ]);
    expect(pipeline.rows).toEqual([['30', '28', '2', '26', '4', '0', '0']]);
    // MQL and QL are two separate header cells, each with its own count (AAP 0.5.4.7).
    const mqlColumns = pipeline.headers.filter((header) => /\bMQL\b/u.test(header));
    const qlColumns = pipeline.headers.filter((header) => /\bQL\b|Qualified Lead/u.test(header));
    expect(mqlColumns).toEqual(['MQL (First meeting booked)']);
    expect(qlColumns).toEqual(['Qualified Leads']);
    const mqlCell = pipeline.rows[0]?.[pipeline.headers.indexOf('MQL (First meeting booked)')];
    const qlCell = pipeline.rows[0]?.[pipeline.headers.indexOf('Qualified Leads')];
    expect([mqlCell, qlCell]).toEqual(['4', '0']);
    // No header combines them: no "MQL + QL", "MQL/QL", "MQL and Qualified Leads" or the like.
    const combines = (header: string): boolean =>
      /\bMQL\b/u.test(header) && /\bQL\b|Qualified/u.test(header);
    expect(pipeline.headers.filter(combines)).toEqual([]);
    for (const combined of ['MQL + QL', 'MQL/QL', 'MQL & QL', 'MQL + Qualified Leads']) {
      expect(pipeline.headers).not.toContain(combined);
    }
  });

  it('offers a C2 "Hold" "Mark done", never "Record decision"', async () => {
    const api = mockApi({});
    await renderCampaigns(oneGroup('scale', 'Scale', [pageItem(FIXTURE_ITEM_IDS.c2Hold)]));

    // A Hold has no decision subject: it writes no decision and is marked done (AAP 0.5.5).
    const hold = card('Hold: platform outcomes are not reaching Qualified Lead');
    expect(buttonNames(hold)).toEqual([...HOLD_FOOTER]);
    expect(within(hold).queryByRole('button', { name: RECORD_DECISION_LABEL })).toBeNull();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });

  it('shows a C2 "Scale" its evidence, no pipeline table, and records its B1 amount', async () => {
    const api = mockApi({
      'GET /decisions': json(200, [benchmarksUsDecision()]),
      'POST /decisions': json(
        201,
        benchmarksUsDecision({
          id: 812,
          origin: 'recommendation',
          decided_at: '2026-10-06T09:30:00Z',
          decided_by: 'admin@example.com',
          daily_budget: '72.00',
          patch: { daily_budget: '72.00' },
          recommendation_id: OWN_BUDGET_SCALE_ID,
        }),
      ),
    });
    await renderCampaigns(oneGroup('scale', 'Scale', [ownBudgetScaleItem()]));

    const article = card('Scale');
    expect(article.id).toBe(`item-${String(OWN_BUDGET_SCALE_ID)}`);
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: USD\u00a01,500.00',
      'Window leads: 48',
      'Expected leads at the account baseline: 37.5',
      'Qualified Leads: 6',
    ]);
    // The veto passed, so its counts stay in the evidence table: only a Hold gets the table.
    expect(within(article).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    // A Scale has a decision subject: "Record decision", never "Mark done" (AAP 0.5.5).
    expect(buttonNames(article)).toEqual([...SCALE_FOOTER]);
    expect(within(article).queryByRole('button', { name: 'Mark done' })).toBeNull();

    // The dialog reads the campaign's decision log and is prefilled with the linked B1 amount.
    const dialog = await openDecisionDialog(article);
    expect(within(dialog).getByRole('spinbutton', { name: 'Daily budget' })).toHaveProperty(
      'value',
      '72.00',
    );
    expect(within(dialog).getByRole('combobox', { name: 'Status' })).toHaveProperty(
      'value',
      'active',
    );
    expect(decisionReads(api.calls)).toEqual([
      ['campaign', String(FIXTURE_CAMPAIGN_IDS.benchmarksUs)],
    ]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    // The card states what the saved decision recorded, its status kept and the new amount.
    const recorded = await within(article).findByText(
      `${DECISION_RECORDED_MESSAGE}: Active · daily budget USD\u00a072.00`,
      KEEP_SPACES,
    );
    expect(recorded.getAttribute('role')).toBe('status');
    expect(screen.queryByRole('dialog')).toBeNull();
    // One patch with the changed field only: the unchanged status is not sent (AAP 0.5.2).
    const posts = decisionPosts(api.calls);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toEqual({
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
      patch: { daily_budget: '72.00' },
      recommendation_id: OWN_BUDGET_SCALE_ID,
    });
    expect(posts[0]?.headers.get(CSRF[0])).toBe(CSRF[1]);
    // Every read, the refetch after the save included, names the same subject.
    for (const read of decisionReads(api.calls)) {
      expect(read).toEqual(['campaign', String(FIXTURE_CAMPAIGN_IDS.benchmarksUs)]);
    }
    // Recording a decision is not feedback.
    expect(api.calls.some((call) => call.path.endsWith('/feedback'))).toBe(false);
  });

  it('records a shared-budget C2 "Scale" on the budget subject, listing every member', async () => {
    const api = mockApi({
      'GET /decisions': json(200, [makeDecision('budget')]),
      'POST /decisions': json(
        201,
        makeDecision('budget', {
          id: 813,
          origin: 'recommendation',
          decided_at: '2026-10-06T09:30:00Z',
          decided_by: 'admin@example.com',
          daily_budget: '600.00',
          patch: { daily_budget: '600.00' },
          recommendation_id: SHARED_BUDGET_SCALE_ID,
        }),
      ),
    });
    await renderCampaigns(oneGroup('scale', 'Scale', [sharedBudgetScaleItem()]));

    const article = card('Scale');
    expect(article.textContent).toContain('Search Shared Budget');
    // No Google CRM link, so no veto was read: no QL line and no pipeline table.
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: USD\u00a09,500.00',
      'Window conversions: 125',
      'Expected conversions at the account baseline: 100',
    ]);
    expect(within(article).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    expect(buttonNames(article)).toEqual([...SCALE_FOOTER]);
    expect(within(article).queryByRole('button', { name: 'Mark done' })).toBeNull();

    // The decision is written on the shared budget, with every member campaign listed.
    const dialog = await openDecisionDialog(article);
    expect(within(dialog).getByRole('spinbutton', { name: 'Daily budget' })).toHaveProperty(
      'value',
      '600.00',
    );
    // The one amount field a DAILY budget allows, and no status: a budget has none (AAP 0.5.2).
    expect(within(dialog).getAllByRole('spinbutton')).toHaveLength(1);
    expect(within(dialog).queryByRole('combobox', { name: 'Status' })).toBeNull();
    expect(
      within(dialog).getByText(
        'Applies to the shared budget used by: Search Partners — US, Search Competitors — US',
      ),
    ).toBeDefined();
    expect(decisionReads(api.calls)).toEqual([['budget', String(FIXTURE_BUDGET_IDS.search)]]);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Save decision' }));

    // A shared budget has no status: the card states the amount the decision recorded on it.
    const recorded = await within(article).findByText(
      `${DECISION_RECORDED_MESSAGE}: daily budget USD\u00a0600.00`,
      KEEP_SPACES,
    );
    expect(recorded.getAttribute('role')).toBe('status');
    expect(screen.queryByRole('dialog')).toBeNull();
    const posts = decisionPosts(api.calls);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toEqual({
      subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
      patch: { daily_budget: '600.00' },
      recommendation_id: SHARED_BUDGET_SCALE_ID,
    });
    expect(posts[0]?.headers.get(CSRF[0])).toBe(CSRF[1]);
    for (const read of decisionReads(api.calls)) {
      expect(read).toEqual(['budget', String(FIXTURE_BUDGET_IDS.search)]);
    }
    expect(api.calls.some((call) => call.path.endsWith('/feedback'))).toBe(false);
  });

  it('shows a C1b card its spend, outcomes and the pipeline counts side by side', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3005,
      rule_id: 'C1b',
      action: 'Pause or rework targeting: leads are not becoming Qualified Leads',
      reason: '22 eligible contacts from matured leads reached 0 Qualified Leads and 0 meetings.',
      entity: {
        source: 'linkedin',
        type: 'campaign',
        id: '700000602',
        name: 'Lead Gen — Ops EMEA',
      },
      evidence: makeEvidence({
        metrics: [
          metric('spend', 'Window spend', '2240.00', 'currency', 'EUR'),
          metric('outcomes', 'Window leads', 40, 'leads'),
          metric(
            'expected_outcomes',
            'Expected leads at the account baseline',
            '32.000000',
            'leads',
          ),
          metric('leads', 'LinkedIn leads (platform)', 41, 'leads'),
          metric(
            'matured_leads',
            'Matured leads (at least 14 days before window end)',
            25,
            'leads',
          ),
          metric('matched_leads', 'Matched matured leads', 24, 'leads'),
          metric('unresolved', 'Unresolved matured leads', 1, 'leads'),
          metric('eligible_contacts', 'Eligible contacts', 22, 'contacts'),
          metric('mql', 'MQL (First meeting booked)', 3, 'contacts'),
          metric('ql', 'Qualified Leads', 0, 'contacts'),
          metric('meetings', 'Meetings', 0, 'meetings'),
        ],
        baselines: [
          {
            key: 'baseline_cost_per_outcome',
            label: 'Account cost per lead (same source and objective)',
            value: '70.000000',
          },
        ],
        extra: { proposed_status: 'paused' },
      }),
      decision_subject: { type: 'campaign', id: 12 },
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting: leads are not becoming Qualified Leads');
    // A pause card lists spend, outcomes, expected outcomes and the CRM QL (AAP 0.5.5); the
    // platform leads and the other pipeline counts stay out of this list.
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual([
      'Window spend: EUR\u00a02,240.00',
      'Window leads: 40',
      'Expected leads at the account baseline: 32',
      'Qualified Leads: 0',
    ]);
    const pipeline = tableOf(article, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual([
      'Matured leads',
      'Matched matured leads',
      'Unresolved matured leads',
      'Eligible contacts',
      'MQL (First meeting booked)',
      'Qualified Leads',
      'Meetings',
    ]);
    expect(pipeline.rows).toEqual([['25', '24', '1', '22', '3', '0', '0']]);
  });

  it('keeps the matured label without a unit and drops missing columns', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3006,
      rule_id: 'C1b',
      action: 'Pause or rework targeting',
      evidence: makeEvidence({
        metrics: [
          metric('matured_leads', 'Matured contacts (14 days)', 20, null),
          metric('mql', 'MQL (First meeting booked)', null, 'contacts'),
        ],
      }),
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting');
    const pipeline = tableOf(article, PIPELINE_EVIDENCE_CAPTION);
    expect(pipeline.headers).toEqual(['Matured contacts (14 days)', 'MQL (First meeting booked)']);
    // An unavailable count reads "unavailable", never 0.
    expect(pipeline.rows).toEqual([['20', 'unavailable']]);
    // No metric of the campaign list, so the list is left out.
    expect(within(article).queryByRole('list', { name: CAMPAIGN_EVIDENCE_LABEL })).toBeNull();
  });

  it('leaves the pipeline table out when the evidence carries none of its metrics', async () => {
    mockApi({});
    const c1b = makeItem({
      id: 3007,
      rule_id: 'C1b',
      action: 'Pause or rework targeting',
      evidence: makeEvidence({
        metrics: [metric('spend', 'Window spend', '10.00', 'currency', 'USD')],
      }),
    });
    await renderCampaigns(oneGroup('pause', 'Pause', [c1b]));

    const article = card('Pause or rework targeting');
    expect(within(article).queryByRole('table', { name: PIPELINE_EVIDENCE_CAPTION })).toBeNull();
    expect(listLines(article, CAMPAIGN_EVIDENCE_LABEL)).toEqual(['Window spend: USD\u00a010.00']);
  });
});

// ---------------------------------------------------------------------------------------------
// The C2 "Scale" link to its B1 item
// ---------------------------------------------------------------------------------------------

/** The id of {@link benchmarksUsB1Item}, outside {@link FIXTURE_ITEM_IDS}. */
const BENCHMARKS_US_B1_ID = 3204;

/** The id of {@link searchBudgetB1Item}, outside {@link FIXTURE_ITEM_IDS}. */
const SEARCH_BUDGET_B1_ID = 3214;

/** The action of {@link benchmarksUsB1Item}: the B1 of {@link ownBudgetScaleItem}, +20%. */
const BENCHMARKS_US_B1_ACTION = 'Raise daily budget from USD 60.00 to USD 72.00';

/** The action of {@link searchBudgetB1Item}: the B1 of {@link sharedBudgetScaleItem}, +20%. */
const SEARCH_BUDGET_B1_ACTION = 'Raise daily budget from USD 500.00 to USD 600.00';

/**
 * The B1 item of {@link ownBudgetScaleItem}: the default page's B1 card moved onto the scale's
 * subject, "Lead Gen — Benchmarks US", with the read-time overlay `current` applied.
 */
function benchmarksUsB1Item(current: Partial<Item['current']> = {}): Item {
  const b1 = pageItem(FIXTURE_ITEM_IDS.b1);
  return {
    ...b1,
    id: BENCHMARKS_US_B1_ID,
    action: BENCHMARKS_US_B1_ACTION,
    entity: ownBudgetScaleItem().entity,
    decision_subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
    current: makeItemOverlay(current),
  };
}

/** The B1 item of {@link sharedBudgetScaleItem}, on the "Search Shared Budget" itself. */
function searchBudgetB1Item(): Item {
  return {
    ...pageItem(FIXTURE_ITEM_IDS.b1),
    id: SEARCH_BUDGET_B1_ID,
    action: SEARCH_BUDGET_B1_ACTION,
    entity: sharedBudgetScaleItem().entity,
    decision_subject: { type: 'budget', id: FIXTURE_BUDGET_IDS.search },
  };
}

/** A Budget section whose one group, "changes", holds `items`. */
function budgetWith(items: Item[]): Section {
  return makeSection('budget', {
    groups: [makeGroup({ key: 'changes', title: 'Budget changes', items })],
  });
}

/**
 * Renders the Campaigns section as `/actions` does, inside a `B1CardsProvider` given the page's
 * sections: `campaigns` and a Budget section holding `budgetItems`.
 */
async function renderLinked(campaigns: Section, budgetItems: Item[]) {
  return renderWithProviders(
    <B1CardsProvider sections={[campaigns, budgetWith(budgetItems)]}>
      <CampaignsSection section={campaigns} run={makeRun()} overlay={makeOverlay()} />
    </B1CardsProvider>,
  );
}

/** The text of every link inside `scope`, in document order. */
function linkNames(scope: HTMLElement): string[] {
  return within(scope)
    .queryAllByRole('link')
    .map((link) => link.textContent);
}

/** The Campaigns section memoized as `/actions` renders it, so only the provider can update it. */
const MemoizedCampaignsSection = memo(CampaignsSection);

/** Where {@link LinkedCampaigns} publishes how to replace the page's Budget section. */
interface BudgetSwap {
  /** Replaces the Budget section the provider indexes, as a newer page does. */
  swap?: (next: Section) => void;
}

/** Props of {@link LinkedCampaigns}. */
interface LinkedCampaignsProps {
  /** The Campaigns section, kept the same object for the harness's lifetime. */
  campaigns: Section;
  /** The Budget section the provider indexes first. */
  initialBudget: Section;
  /** Receives the function that replaces the Budget section once the harness has mounted. */
  handle: BudgetSwap;
}

/**
 * The memoized Campaigns section inside a `B1CardsProvider` whose Budget section a test replaces
 * while the Campaigns section's own props stay the same objects, as a poll that changed only a
 * B1 card leaves them.
 */
function LinkedCampaigns({ campaigns, initialBudget, handle }: LinkedCampaignsProps): ReactElement {
  const [budget, setBudget] = useState(initialBudget);
  const [run] = useState(makeRun);
  const [overlay] = useState(makeOverlay);
  useEffect(() => {
    handle.swap = setBudget;
  }, [handle]);
  const sections = useMemo(() => [campaigns, budget], [campaigns, budget]);
  return (
    <B1CardsProvider sections={sections}>
      <MemoizedCampaignsSection section={campaigns} run={run} overlay={overlay} />
    </B1CardsProvider>
  );
}

describe('the C2 "Scale" link to its B1 item', () => {
  it('links a "Scale" card to the B1 card for its subject, named by the B1 action', async () => {
    const api = mockApi({});
    await renderLinked(oneGroup('scale', 'Scale', [ownBudgetScaleItem()]), [
      pageItem(FIXTURE_ITEM_IDS.b1),
      benchmarksUsB1Item(),
    ]);

    const article = card('Scale');
    // "'Scale', with a link to its B1 item, which carries the amount" (AAP 0.5.4.4); the name
    // says where the link leads without the card around it (WCAG 2.2 SC 2.4.4).
    const link = within(article).getByRole('link', {
      name: `${B1_LINK_LABEL}: ${BENCHMARKS_US_B1_ACTION}`,
    });
    expect(link.getAttribute('href')).toBe(`#item-${String(BENCHMARKS_US_B1_ID)}`);
    expect(linkNames(article)).toEqual([`${B1_LINK_LABEL}: ${BENCHMARKS_US_B1_ACTION}`]);
    // Beside the card's fields: after its campaign evidence.
    const fields = within(article).getByRole('list', { name: CAMPAIGN_EVIDENCE_LABEL });
    expect(fields.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The B1 card is found in the page's own response: nothing is fetched for it.
    await settle();
    expect(api.calls).toHaveLength(0);
  });

  it('links a shared-budget "Scale" to the B1 on the budget subject, never a same-id campaign', async () => {
    mockApi({});
    const sameIdCampaign: Item = {
      ...pageItem(FIXTURE_ITEM_IDS.b1),
      decision_subject: { type: 'campaign', id: FIXTURE_BUDGET_IDS.search },
    };
    const campaigns = oneGroup('scale', 'Scale', [sharedBudgetScaleItem()]);
    await renderLinked(campaigns, [sameIdCampaign, searchBudgetB1Item()]);

    const link = within(card('Scale')).getByRole('link', {
      name: `${B1_LINK_LABEL}: ${SEARCH_BUDGET_B1_ACTION}`,
    });
    expect(link.getAttribute('href')).toBe(`#item-${String(SEARCH_BUDGET_B1_ID)}`);
  });

  it('shows no link when the page has no B1 card for its subject, or outside the provider', async () => {
    mockApi({});
    // The default page's B1 is on "Pipeline Search — US", another subject.
    const { unmount } = await renderLinked(oneGroup('scale', 'Scale', [ownBudgetScaleItem()]), [
      pageItem(FIXTURE_ITEM_IDS.b1),
    ]);
    expect(linkNames(card('Scale'))).toEqual([]);
    unmount();

    await renderCampaigns(oneGroup('scale', 'Scale', [ownBudgetScaleItem()]));
    expect(linkNames(card('Scale'))).toEqual([]);
  });

  it('links a B1 card a discrepancy holds or from replaced data, never one feedback hides', async () => {
    mockApi({});
    const campaigns = oneGroup('scale', 'Scale', [ownBudgetScaleItem()]);
    const name = `${B1_LINK_LABEL}: ${BENCHMARKS_US_B1_ACTION}`;
    const href = `#item-${String(BENCHMARKS_US_B1_ID)}`;
    const hidden = await renderLinked(campaigns, [benchmarksUsB1Item({ hidden_by: 'dismiss' })]);
    expect(linkNames(card('Scale'))).toEqual([]);
    hidden.unmount();
    // Hidden by feedback while held: counted, never rendered, so never linked.
    const hiddenHeld = await renderLinked(campaigns, [
      { ...benchmarksUsB1Item({ held_now: true, hidden_by: 'snooze' }), held: true },
    ]);
    expect(linkNames(card('Scale'))).toEqual([]);
    hiddenHeld.unmount();

    // A held B1 sits in its group's held disclosure, which the link's hash expands: held when
    // the run computed it, or held now by a discrepancy detected since.
    const frozenHold: Item = { ...benchmarksUsB1Item(), held: true };
    const held = await renderLinked(campaigns, [frozenHold]);
    const heldLink = within(card('Scale')).getByRole('link', { name });
    expect(heldLink.getAttribute('href')).toBe(href);
    held.unmount();
    const heldNow = await renderLinked(campaigns, [benchmarksUsB1Item({ held_now: true })]);
    expect(within(card('Scale')).getByRole('link', { name }).getAttribute('href')).toBe(href);
    heldNow.unmount();

    // A held "Scale" links its held B1 too: one discrepancy on their shared subject holds both.
    const heldScale = oneGroup('scale', 'Scale', [{ ...ownBudgetScaleItem(), held: true }]);
    const bothHeld = await renderLinked(heldScale, [frozenHold]);
    fireEvent.click(
      screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }),
    );
    expect(within(card('Scale')).getByRole('link', { name }).getAttribute('href')).toBe(href);
    bothHeld.unmount();

    // A B1 computed from replaced data is still shown openly, under "Computed from replaced
    // data", so the link stays.
    await renderLinked(campaigns, [benchmarksUsB1Item({ historical: true, held_now: true })]);
    expect(linkNames(card('Scale'))).toEqual([`${B1_LINK_LABEL}: ${BENCHMARKS_US_B1_ACTION}`]);
  });

  it('gives a "Hold" no link, even beside a B1 card for its subject', async () => {
    mockApi({});
    const hold = pageItem(FIXTURE_ITEM_IDS.c2Hold);
    // A Hold names no B1; one that did would still get no link, because a Hold has no B1.
    const holdWithLink: Item = {
      ...hold,
      evidence: makeEvidence({
        ...hold.evidence,
        extra: {
          ...hold.evidence.extra,
          b1_link: {
            rule_id: 'B1',
            subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs },
          },
        },
      }),
    };
    await renderLinked(oneGroup('scale', 'Scale', [holdWithLink]), [benchmarksUsB1Item()]);

    expect(linkNames(card('Hold: platform outcomes are not reaching Qualified Lead'))).toEqual([]);
  });

  it('shows no link for a link to another rule or one without a subject', async () => {
    mockApi({});
    const scale = ownBudgetScaleItem();
    const withLink = (b1Link: unknown, id: Item['id']): Item => ({
      ...scale,
      id,
      evidence: makeEvidence({
        ...scale.evidence,
        extra: { ...scale.evidence.extra, b1_link: b1Link },
      }),
    });
    const subject = { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.benchmarksUs };
    await renderLinked(
      oneGroup('scale', 'Scale', [
        withLink({ rule_id: 'B3', subject }, 3221),
        withLink({ rule_id: 'B1' }, 3222),
      ]),
      [benchmarksUsB1Item()],
    );

    for (const article of screen.getAllByRole('article', { name: 'Scale' })) {
      expect(linkNames(article)).toEqual([]);
    }
  });

  it('names the link "View B1 budget increase" alone when the B1 action is blank', async () => {
    mockApi({});
    const blank: Item = { ...benchmarksUsB1Item(), action: ' ' };
    await renderLinked(oneGroup('scale', 'Scale', [ownBudgetScaleItem()]), [blank]);

    const link = within(card('Scale')).getByRole('link', { name: B1_LINK_LABEL });
    expect(link.getAttribute('href')).toBe(`#item-${String(BENCHMARKS_US_B1_ID)}`);
  });

  it('follows the B1 card as a newer page hides and restores it, through the section memo', async () => {
    mockApi({});
    const handle: BudgetSwap = {};
    await renderWithProviders(
      <LinkedCampaigns
        campaigns={oneGroup('scale', 'Scale', [ownBudgetScaleItem()])}
        initialBudget={budgetWith([benchmarksUsB1Item()])}
        handle={handle}
      />,
    );
    const name = `${B1_LINK_LABEL}: ${BENCHMARKS_US_B1_ACTION}`;
    expect(linkNames(card('Scale'))).toEqual([name]);

    act(() => {
      handle.swap?.(budgetWith([benchmarksUsB1Item({ hidden_by: 'snooze' })]));
    });
    expect(linkNames(card('Scale'))).toEqual([]);

    act(() => {
      handle.swap?.(budgetWith([benchmarksUsB1Item()]));
    });
    expect(linkNames(card('Scale'))).toEqual([name]);
  });
});

// ---------------------------------------------------------------------------------------------
// Launch ideas (C3a, C3b)
// ---------------------------------------------------------------------------------------------

describe('launch idea cards', () => {
  it('shows a C3b card its head tokens and member terms', async () => {
    mockApi({});
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [pageItem(FIXTURE_ITEM_IDS.c3b)]));

    const c3b = card("Create ad group or campaign for 'data warehouse'");
    expect(listLines(c3b, CLUSTER_GAP_LABEL)).toEqual(['Head tokens: data, warehouse']);
    const terms = tableOf(c3b, CLUSTER_TERMS_CAPTION);
    expect(terms.headers).toEqual(['Term', 'Spend', 'Conversions']);
    // Conversions stay fractional under data-driven attribution.
    expect(terms.rows).toEqual([
      ['cloud data warehouse', 'USD\u00a0132.00', '3'],
      ['data warehouse tools', 'USD\u00a096.00', '2.5'],
      ['data warehouse pricing', 'USD\u00a059.00', '1.5'],
    ]);
  });

  it('shows a cluster without head tokens only its terms', async () => {
    mockApi({});
    const c3b = makeItem({
      id: 3008,
      rule_id: 'C3b',
      action: "Create ad group or campaign for 'erp'",
      evidence: makeEvidence({
        metrics: [metric('spend', 'Cluster spend', '40.00', 'currency', 'USD')],
        extra: {
          head_tokens: [],
          members: [
            { term: 'erp suite', spend: null, conversions: '2', clicks: 9 },
            { term: 'erp pricing', spend: '40.00', conversions: null, clicks: 7 },
            { spend: '1.00', conversions: '1' },
          ],
        },
      }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3b]));

    const article = card("Create ad group or campaign for 'erp'");
    expect(within(article).queryByRole('list', { name: CLUSTER_GAP_LABEL })).toBeNull();
    // A member without a term is not a cluster term.
    expect(tableOf(article, CLUSTER_TERMS_CAPTION).rows).toEqual([
      ['erp suite', 'unavailable', '2'],
      ['erp pricing', 'USD\u00a040.00', 'unavailable'],
    ]);
  });

  it('shows a cluster with head tokens but no member term only its head tokens', async () => {
    mockApi({});
    const c3b = makeItem({
      id: 3009,
      rule_id: 'C3b',
      action: "Create ad group or campaign for 'crm migration'",
      evidence: makeEvidence({ extra: { head_tokens: ['crm', 'migration'], members: [] } }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3b]));

    const article = card("Create ad group or campaign for 'crm migration'");
    expect(listLines(article, CLUSTER_GAP_LABEL)).toEqual(['Head tokens: crm, migration']);
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });

  it('shows a C3a card the segment, its leads, CPL and impression share', async () => {
    mockApi({});
    const action =
      'Launch a campaign targeting job function: Finance with ' +
      "Lead Gen — Pipeline Report EMEA's offer";
    const c3a = makeItem({
      id: 3010,
      rule_id: 'C3a',
      action,
      entity: {
        source: 'linkedin',
        type: 'campaign',
        id: '700000501',
        name: 'Lead Gen — Pipeline Report EMEA',
      },
      evidence: makeEvidence({
        metrics: [
          metric('segment_leads', 'Segment leads', 6, 'leads'),
          metric('segment_spend', 'Segment spend', '171.00', 'currency', 'EUR'),
          metric('segment_cpl', 'Segment cost per lead', '28.50', 'currency', 'EUR'),
          metric('campaign_cpl', 'Campaign cost per lead', '41.20', 'currency', 'EUR'),
          metric('impression_share', 'Segment share of campaign impressions', '0.12', 'ratio'),
        ],
        extra: { facet: 'JOB_FUNCTION', facet_label: 'job function', value: 'Finance' },
      }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [c3a]));

    const article = card(action);
    expect(listLines(article, SEGMENT_GAP_LABEL)).toEqual([
      'Segment: job function: Finance',
      'Segment leads: 6',
      'Segment cost per lead: EUR\u00a028.50',
      'Segment share of campaign impressions: 12.0%',
    ]);
    // The field reads exactly as the same metric's row of the card's evidence table.
    const evidenceRow = within(within(article).getByRole('table', { name: 'Evidence' }))
      .getByRole('rowheader', { name: 'Segment share of campaign impressions' })
      .closest('tr');
    expect(evidenceRow?.querySelector('td')?.textContent).toBe('12.0%');
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });

  it("names a C3a segment by LinkedIn's name, else by the URN an older run stored", async () => {
    mockApi({});
    const segmentCard = (id: number, campaign: string, extra: Record<string, unknown>) =>
      makeItem({
        id,
        rule_id: 'C3a',
        action: `Launch a campaign targeting seniority: ${campaign}`,
        evidence: makeEvidence({
          metrics: [metric('segment_leads', 'Segment leads', 20, 'leads')],
          extra: { facet: 'seniority', facet_label: 'seniority', ...extra },
        }),
      });
    const named = segmentCard(3012, 'Director', {
      value: 'urn:li:seniority:6',
      value_label: 'Director',
    });
    const frozen = segmentCard(3013, 'urn:li:seniority:6', { value: 'urn:li:seniority:6' });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [named, frozen]));

    expect(
      listLines(card('Launch a campaign targeting seniority: Director'), SEGMENT_GAP_LABEL),
    ).toEqual(['Segment: seniority: Director', 'Segment leads: 20']);
    expect(
      listLines(
        card('Launch a campaign targeting seniority: urn:li:seniority:6'),
        SEGMENT_GAP_LABEL,
      ),
    ).toEqual(['Segment: seniority: urn:li:seniority:6', 'Segment leads: 20']);
  });

  it('shows a launch idea without gap evidence as its plain card', async () => {
    mockApi({});
    const bare = makeItem({
      id: 3011,
      rule_id: 'C3a',
      action: 'Launch a campaign',
      evidence: makeEvidence({ extra: null }),
    });
    await renderCampaigns(oneGroup('launch', 'Launch ideas', [bare]));

    const article = card('Launch a campaign');
    expect(within(article).queryByRole('list', { name: SEGMENT_GAP_LABEL })).toBeNull();
    expect(within(article).queryByRole('list', { name: CLUSTER_GAP_LABEL })).toBeNull();
    expect(within(article).queryByRole('table', { name: CLUSTER_TERMS_CAPTION })).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Flags and data state
// ---------------------------------------------------------------------------------------------

describe('data-state flags on campaign cards', () => {
  it('shows the simulated, incomplete and unverified flags the run and overlay carry', async () => {
    mockApi({});
    await renderCampaigns(
      campaignsWith([
        makeGroup({ key: 'pause', title: 'Pause', items: [pageItem(FIXTURE_ITEM_IDS.c1)] }),
        makeGroup({
          key: 'launch',
          title: 'Launch ideas',
          items: [pageItem(FIXTURE_ITEM_IDS.c3b)],
        }),
      ]),
    );

    const c1Flags = listLines(card('Pause'), 'Flags');
    expect(c1Flags).toContain('Simulated data (Google)');
    expect(c1Flags).toContain('Unverified while 1 discrepancy is open');
    const c3bFlags = listLines(card("Create ad group or campaign for 'data warehouse'"), 'Flags');
    expect(c3bFlags).toContain(
      'Incomplete: google.search_terms (result limit reached at finest partition)',
    );
    expect(c3bFlags).toContain('Simulated data (Google)');
    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Bounded batches in the discrepancy group
// ---------------------------------------------------------------------------------------------

describe('a long discrepancy group', () => {
  /** A stand-in for `Element.prototype.scrollIntoView`, which jsdom does not implement. */
  let scrollIntoView: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      writable: true,
      value: scrollIntoView,
    });
  });

  afterEach(() => {
    window.history.replaceState(null, '', window.location.pathname);
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  /**
   * 5 visible C1 cards (ids 7101–7105) listed before 45 C0 cards (ids 7001–7045), so the tests
   * prove the group pins every C0 card first, in the C0 cards' own order.
   */
  function longDiscrepancyGroup(): Section {
    const c0 = pageItem(FIXTURE_ITEM_IDS.c0);
    const pinned = Array.from({ length: 45 }, (_, index) => ({
      ...c0,
      id: 7001 + index,
      rank: index + 1,
    }));
    const visible = Array.from({ length: 5 }, (_, index) =>
      makeItem({
        id: 7101 + index,
        rank: 46 + index,
        rule_id: 'C1',
        action: `Pause campaign ${String(index + 1)}`,
      }),
    );
    return oneGroup('discrepancies', 'Discrepancies', [...visible, ...pinned]);
  }

  /** The ids of the cards the discrepancy group renders, in document order. */
  function cardIds(): string[] {
    return within(screen.getByRole('region', { name: 'Discrepancies' }))
      .getAllByRole('article')
      .map((article) => article.id);
  }

  /** The text of the group's batch status, told apart from the cards' own status regions. */
  function batchStatus(scope: HTMLElement): string | null {
    const statuses = within(scope)
      .getAllByRole('status')
      .filter((status) => status.textContent.startsWith('Showing '));
    expect(statuses).toHaveLength(1);
    return statuses[0]?.textContent ?? null;
  }

  /** `item-{id}` for each id from `first` to `last`. */
  function itemIds(first: number, last: number): string[] {
    return Array.from({ length: last - first + 1 }, (_, index) => `item-${String(first + index)}`);
  }

  it('reveals every C0 card, in API order, before the first other card', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(longDiscrepancyGroup());
    const group = screen.getByRole('region', { name: 'Discrepancies' });
    await within(group).findAllByRole('button', { name: 'Platform is correct' });

    expect(cardIds()).toEqual(itemIds(7001, 7020));
    expect(batchStatus(group)).toBe('Showing 20 of 50 actions');
    fireEvent.click(within(group).getByRole('button', { name: 'Show 20 more actions' }));
    expect(cardIds()).toEqual(itemIds(7001, 7040));
    expect(document.activeElement?.id).toBe('item-7021');

    fireEvent.click(within(group).getByRole('button', { name: 'Show 10 more actions' }));
    expect(cardIds()).toEqual([...itemIds(7001, 7045), ...itemIds(7101, 7105)]);
    expect(batchStatus(group)).toBe('Showing all 50 actions');
    // Every C0 card keeps its live resolver; none is an alert.
    expect(within(group).getAllByRole('button', { name: 'Platform is correct' })).toHaveLength(45);
    expect(screen.queryByRole('alert')).toBeNull();
  }, LARGE_RENDER_TEST_TIMEOUT_MS);

  it('reveals the C0 card a held card links to when it lies beyond the first batch', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    window.history.replaceState(null, '', '#item-7038');
    await renderCampaigns(longDiscrepancyGroup());

    expect(cardIds()).toEqual(itemIds(7001, 7040));
    const target = document.getElementById('item-7038');
    expect(target).not.toBeNull();
    expect(scrollIntoView.mock.contexts).toContain(target);
    await screen.findAllByRole('button', { name: 'Platform is correct' });
  });

  it('reveals a C0 card a hash change names, beyond the shown batch', async () => {
    mockApi({ 'GET /discrepancies': openDiscrepancies() });
    await renderCampaigns(longDiscrepancyGroup());
    expect(document.getElementById('item-7044')).toBeNull();

    act(() => {
      window.history.replaceState(null, '', '#item-7044');
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });

    expect(cardIds()).toEqual(itemIds(7001, 7045).concat(itemIds(7101, 7105)));
    expect(scrollIntoView.mock.contexts).toEqual([document.getElementById('item-7044')]);
    await screen.findAllByRole('button', { name: 'Platform is correct' });
  });
});

describe('CampaignsSection: the confirmation of a card hidden on the page', () => {
  /** Whether `first` precedes `second` in document order. */
  function precedes(first: Element, second: Element): boolean {
    return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  }

  /** A C1 "Pause" in the discrepancy group, after the pinned C0 card. */
  const DISCREPANCY_GROUP_PAUSE = makeItem({
    id: 1190,
    rank: 2,
    rule_id: 'C1',
    action: 'Pause Partner Search — US',
    entity: { source: 'google', type: 'campaign', id: '61000990', name: 'Partner Search — US' },
  });

  /** The default page with {@link DISCREPANCY_GROUP_PAUSE} after its C0 card. */
  function pageWithDiscrepancyGroupPause() {
    const page = makeRecommendationsPage();
    const campaigns = page.sections.find((section) => section.key === 'campaigns');
    const discrepancies = campaigns?.groups.find((group) => group.key === 'discrepancies');
    if (discrepancies === undefined) {
      throw new Error('the fixture page has no discrepancy group');
    }
    discrepancies.items = [...discrepancies.items, structuredClone(DISCREPANCY_GROUP_PAUSE)];
    return page;
  }

  it('shows it after the pinned C0 card in the discrepancy group, which keeps its resolver', async () => {
    const flow = feedbackFlow(pageWithDiscrepancyGroupPause);
    mockApi({ ...flow.handlers, 'GET /discrepancies': openDiscrepancies() });
    await renderWithProviders(<LiveSection sectionKey="campaigns" component={CampaignsSection} />, {
      path: '/actions',
    });
    const pause = await screen.findByRole('article', { name: DISCREPANCY_GROUP_PAUSE.action });
    fireEvent.click(within(pause).getByRole('button', { name: 'Snooze for 7 days' }));

    await waitFor(() => {
      expect(screen.queryByRole('article', { name: DISCREPANCY_GROUP_PAUSE.action })).toBeNull();
    });
    const group = screen.getByRole('region', { name: 'Discrepancies' });
    const confirmation = within(group).getByText(
      feedbackConfirmationText('snooze', DISCREPANCY_GROUP_PAUSE),
    );
    const c0 = within(group).getByRole('article', { name: C0_ACTION });
    expect(precedes(c0, confirmation)).toBe(true);
    expect(confirmation.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
    expect(flow.hidden.get(DISCREPANCY_GROUP_PAUSE.id)).toBe('snooze');
  });

  it('shows it in a Pause group before the held disclosure, whose count is unchanged', async () => {
    const flow = feedbackFlow();
    mockApi({ ...flow.handlers, 'GET /discrepancies': openDiscrepancies() });
    await renderWithProviders(<LiveSection sectionKey="campaigns" component={CampaignsSection} />, {
      path: '/actions',
    });
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const card = await screen.findByRole('article', { name: c1.action });
    fireEvent.click(within(card).getByRole('button', { name: 'Dismiss' }));

    await waitFor(() => {
      expect(document.getElementById(`item-${String(c1.id)}`)).toBeNull();
    });
    // The group's only visible card was hidden here: the group stays, with the confirmation.
    const group = screen.getByRole('region', { name: 'Pause' });
    const confirmation = within(group).getByText(feedbackConfirmationText('dismiss', c1));
    const held = within(group).getByRole('button', {
      name: 'Held until discrepancies are resolved (1)',
    });
    expect(precedes(confirmation, held)).toBe(true);
    const section = screen.getByRole('region', { name: 'Campaigns to pause or launch' });
    expect(within(section).getByRole('list', { name: 'Not shown' }).textContent).toContain(
      '1 hidden since this run',
    );
    expect(within(section).queryAllByRole('alert')).toEqual([]);
  });
});

describe('CampaignsSection: a card a refetch moves into the replaced-data block', () => {
  /**
   * The overlay of an item whose frozen dataset a dataset activation has replaced since its run
   * (AAP 0.3.2): historical, held by the replaced data, with every action disabled.
   */
  function replacedOverlay(): Item['current'] {
    return makeItemOverlay({
      historical: true,
      held_now: true,
      held_reason_now: 'Computed from data that has since been replaced',
      data_status_now: 'live',
    });
  }

  /**
   * `GET /recommendations` answering the page `build` makes, with each item in `replaced`
   * computed from replaced data, as the overlay reads it live after a dataset activation.
   */
  function servePage(
    replaced: ReadonlySet<Item['id']>,
    build: () => ReturnType<typeof makeRecommendationsPage> = makeRecommendationsPage,
  ): MockHandlers {
    return {
      'GET /recommendations': () => {
        const page = build();
        for (const id of replaced) {
          findPageItem(page, id).current = replacedOverlay();
        }
        return json(200, page);
      },
    };
  }

  /** The "Computed from replaced data ({count})" block of the group region named `group`. */
  function replacedBlock(group: string, count: number): HTMLElement {
    return within(screen.getByRole('region', { name: group })).getByRole('group', {
      name: `Computed from replaced data (${String(count)})`,
    });
  }

  /** Refetches every recommendations page, as the 30 s poll of `/actions` does. */
  async function refetch(queryClient: QueryClient): Promise<void> {
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.recommendationsAll });
    });
  }

  /**
   * Waits for keyboard focus on the replaced-data line of `card`, a historical card: the line
   * with its "Computed from replaced data" badge and provenance, never the page body.
   */
  async function expectFocusOnReplacedLine(card: HTMLElement): Promise<void> {
    const badge = within(card).getByText(HISTORICAL_LABEL);
    await waitFor(() => {
      const active = document.activeElement;
      expect(active).not.toBe(document.body);
      expect(active !== null && active !== card && card.contains(active)).toBe(true);
      expect(active?.contains(badge)).toBe(true);
    });
  }

  it('moves focus from the open decision dialog to the same card in its new place', async () => {
    const replaced = new Set<Item['id']>();
    mockApi({
      ...servePage(replaced),
      'GET /discrepancies': openDiscrepancies(),
      // The subject's dataset is replaced before the page's own refetch shows it.
      'GET /decisions': apiError(409, 'subject_retired', 'The dataset was replaced.'),
    });
    const { queryClient } = await renderWithProviders(
      <LiveSection sectionKey="campaigns" component={CampaignsSection} />,
      { path: '/actions' },
    );
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const visible = await screen.findByRole('article', { name: c1.action });
    const opener = within(visible).getByRole('button', { name: RECORD_DECISION_LABEL });
    act(() => {
      opener.focus();
    });
    fireEvent.click(opener);
    const dialog = await screen.findByRole('dialog', { name: RECORD_DECISION_LABEL });
    // The form shows its own lock first: the decision log read was refused as retired.
    await within(dialog).findByText(HISTORICAL_LABEL);
    expect(dialog.contains(document.activeElement)).toBe(true);

    replaced.add(c1.id);
    await refetch(queryClient);

    const moved = await waitFor(() =>
      within(replacedBlock('Pause', 1)).getByRole('article', { name: c1.action }),
    );
    expect(moved).not.toBe(visible);
    expect(screen.queryByRole('dialog')).toBeNull();
    await expectFocusOnReplacedLine(moved);
    // The line states why, and every action of the card is disabled (AAP 0.5.5 "Historical
    // items").
    expect(document.activeElement?.textContent).toContain('Simulated Google data, since replaced');
    const buttons = within(moved).getAllByRole('button');
    expect(buttons.map((button) => button.textContent)).toEqual([
      RECORD_DECISION_LABEL,
      FEEDBACK_LABELS.dismiss,
      FEEDBACK_LABELS.snooze,
    ]);
    for (const button of buttons) {
      expect(button.hasAttribute('disabled')).toBe(true);
    }
  });

  it("moves focus from the card's own footer action to the same card in its new place", async () => {
    const replaced = new Set<Item['id']>();
    mockApi({ ...servePage(replaced), 'GET /discrepancies': openDiscrepancies() });
    const { queryClient } = await renderWithProviders(
      <LiveSection sectionKey="campaigns" component={CampaignsSection} />,
      { path: '/actions' },
    );
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    const visible = await screen.findByRole('article', { name: c1.action });
    act(() => {
      within(visible).getByRole('button', { name: FEEDBACK_LABELS.dismiss }).focus();
    });

    replaced.add(c1.id);
    await refetch(queryClient);

    const moved = await waitFor(() =>
      within(replacedBlock('Pause', 1)).getByRole('article', { name: c1.action }),
    );
    await expectFocusOnReplacedLine(moved);
  });

  it('moves focus to the same card in the discrepancy group, below its pinned C0 card', async () => {
    const pause = makeItem({
      id: 1190,
      rank: 2,
      rule_id: 'C1',
      action: 'Pause Partner Search — US',
      entity: { source: 'google', type: 'campaign', id: '61000990', name: 'Partner Search — US' },
    });
    const build = () => {
      const page = makeRecommendationsPage();
      const campaigns = page.sections.find((section) => section.key === 'campaigns');
      const discrepancies = campaigns?.groups.find((group) => group.key === 'discrepancies');
      if (discrepancies === undefined) {
        throw new Error('the fixture page has no discrepancy group');
      }
      discrepancies.items = [...discrepancies.items, structuredClone(pause)];
      return page;
    };
    const replaced = new Set<Item['id']>();
    mockApi({ ...servePage(replaced, build), 'GET /discrepancies': openDiscrepancies() });
    const { queryClient } = await renderWithProviders(
      <LiveSection sectionKey="campaigns" component={CampaignsSection} />,
      { path: '/actions' },
    );
    const visible = await screen.findByRole('article', { name: pause.action });
    act(() => {
      within(visible).getByRole('button', { name: FEEDBACK_LABELS.snooze }).focus();
    });

    replaced.add(pause.id);
    await refetch(queryClient);

    const block = await waitFor(() => replacedBlock('Discrepancies', 1));
    const moved = within(block).getByRole('article', { name: pause.action });
    await expectFocusOnReplacedLine(moved);
    // The C0 card stays pinned above the block, outside it.
    const c0 = screen.getByRole('article', { name: C0_ACTION });
    expect(block.contains(c0)).toBe(false);
  });

  it('leaves focus the user has put elsewhere where it is', async () => {
    const replaced = new Set<Item['id']>();
    mockApi({ ...servePage(replaced), 'GET /discrepancies': openDiscrepancies() });
    const { queryClient } = await renderWithProviders(
      <>
        <Button>Elsewhere</Button>
        <LiveSection sectionKey="campaigns" component={CampaignsSection} />
      </>,
      { path: '/actions' },
    );
    const c1 = pageItem(FIXTURE_ITEM_IDS.c1);
    await screen.findByRole('article', { name: c1.action });
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' });
    act(() => {
      elsewhere.focus();
    });

    replaced.add(c1.id);
    await refetch(queryClient);

    await waitFor(() =>
      within(replacedBlock('Pause', 1)).getByRole('article', { name: c1.action }),
    );
    await settle();
    expect(document.activeElement).toBe(elsewhere);
  });
});
