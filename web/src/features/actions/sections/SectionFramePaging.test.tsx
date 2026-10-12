/**
 * Component tests for the paging of groups larger than the page in `SectionFrame` and the
 * sections built on it (AAP 0.1.1: the five sections render from precomputed runs in under
 * 2 s, so `GET /recommendations` carries each group's first `GROUP_PAGE_SIZE` items, with
 * `total` and `hidden` counting every stored one, and `GET /recommendations/items` serves the
 * rest).
 *
 * They hold the frame to its TSDoc contract:
 * - a group the page carries in part counts every stored card it shows in its list's status,
 *   "Showing 20 of 250 actions", and reads nothing until its "Show more" has shown every loaded
 *   card; the press that reaches past them loads the next page from the page's own count for
 *   the page's run, section and group, keeps the button in its loading state meanwhile, then
 *   reveals the batch and focuses its first card, until the group's last card is shown;
 * - a failed load is said in the list's status, which takes focus, and the next press reads it
 *   again;
 * - a group whose every loaded card feedback hides, while it stores cards to show, is never
 *   empty: it loads by itself until one shows;
 * - the "Not shown" count of hidden cards counts those beyond the page (`Group.hidden`);
 * - without `runId` the frame reads nothing;
 * - every stored card stays reachable in the sections whose bodies are their own: the
 *   Campaigns discrepancy group's pinned C0 list, a Copy/creative campaign block, whose
 *   variants table stays, and a White papers cohort table.
 *
 * Each test answers `GET /recommendations/items` from its own stored group through
 * `../../../test/mockApi`, which fails the test on any other request.
 */
import { apiError, json, mockApi } from '../../../test/mockApi';
import type { MockResponse, RecordedCall } from '../../../test/mockApi';

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import type { Group, Item, Section, SectionKey } from '../../../api/types';
import {
  DEFAULT_RUN_ID,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeConfig,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import { renderWithProviders } from '../../../test/render';
import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../../test/timeouts';
import { CampaignsSection } from './CampaignsSection';
import { CreativeSection } from './CreativeSection';
import { SectionFrame, itemAnchorId } from './SectionFrame';
import { WhitePapersSection } from './WhitePapersSection';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The run every page belongs to. */
const RUN = makeRun();

/** The page's overlay: nothing open, so no section reads the live discrepancies. */
const OVERLAY = makeOverlay({ open_discrepancies: 0 });

/** The items the page carries per group (`GROUP_PAGE_SIZE`). */
const PAGE_SIZE = 100;

/**
 * Copies of `template` with ids `first`, `first + 1`, … ranked in that order, each its own
 * entity, the ids for which `hidden` holds hidden by a dismissal.
 */
function copies(
  template: Item,
  first: number,
  count: number,
  hidden: (id: number) => boolean = () => false,
): Item[] {
  return Array.from({ length: count }, (_, index) => {
    const id = first + index;
    return {
      ...template,
      id,
      rank: index + 1,
      entity: { ...template.entity, id: `${String(template.entity.id)}-${String(id)}` },
      current: { ...template.current, hidden_by: hidden(id) ? 'dismiss' : null },
    };
  });
}

/** The group as the page carries it: the first {@link PAGE_SIZE} of `stored`, counting all. */
function pageGroup(key: string, title: string, stored: readonly Item[]): Group {
  return {
    key,
    title,
    items: stored.slice(0, PAGE_SIZE),
    total: stored.length,
    hidden: stored.filter((item) => item.current.hidden_by !== null).length,
  };
}

/** Answers `GET /recommendations/items` from `stored`, sliced as the API slices it. */
function serveGroup(
  sectionKey: SectionKey,
  groupKey: string,
  stored: readonly Item[],
): (call: RecordedCall) => MockResponse {
  return (call) => {
    const offset = Number(call.search.get('offset'));
    const limit = Number(call.search.get('limit'));
    return json(200, {
      run_id: RUN.id,
      section: sectionKey,
      group: groupKey,
      total: stored.length,
      hidden: stored.filter((item) => item.current.hidden_by !== null).length,
      offset,
      items: stored.slice(offset, offset + limit),
    });
  };
}

/** The `GET /recommendations/items` requests the stub received. */
function itemReads(calls: readonly RecordedCall[]): RecordedCall[] {
  return calls.filter((call) => call.path === '/recommendations/items');
}

/** The offsets of those requests, in request order. */
function readOffsets(calls: readonly RecordedCall[]): number[] {
  return itemReads(calls).map((call) => Number(call.search.get('offset')));
}

/** The frame tests' `renderItem`: a card anchored at `item-{id}`, as `RecommendationCard` is. */
function renderCard(item: Item): ReactNode {
  return (
    <article id={itemAnchorId(item.id)} aria-label={`Card ${String(item.id)}`}>
      {item.action}
    </article>
  );
}

/** Every rendered card's item id, in document order. */
function cardIds(): number[] {
  return screen
    .queryAllByRole('article')
    .map((card) => Number(card.id.replace(/^item-/u, '')))
    .filter((id) => Number.isSafeInteger(id));
}

/** The status of the batched list inside `container`: the one region reading "Showing …". */
function batchStatus(container: HTMLElement): HTMLElement {
  const status = within(container)
    .getAllByRole('status')
    .find((region) => region.textContent.startsWith('Showing '));
  if (status === undefined) {
    throw new Error('no batch status in the container');
  }
  return status;
}

/** The lines of the frame's "Not shown" list. */
function notShownLines(): (string | null)[] {
  return within(screen.getByRole('list', { name: 'Not shown' }))
    .getAllByRole('listitem')
    .map((line) => line.textContent);
}

/** The "Show … more" button, or `null` when the list shows every entry. */
function showMore(): HTMLElement | null {
  return screen.queryByRole('button', { name: /^Show \d+ more/u });
}

/** Presses "Show … more" `times` times. */
function pressShowMore(times: number): void {
  for (let press = 0; press < times; press += 1) {
    fireEvent.click(showMore() as HTMLElement);
  }
}

/** The ids `first..last`. */
function idRange(first: number, last: number): number[] {
  return Array.from({ length: last - first + 1 }, (_, index) => first + index);
}

/** A Keywords section holding one "block" group as the page carries it. */
function keywordsWith(group: Group): Section {
  return makeSection('keywords', { groups: [group] });
}

/** The stored K1 items of a "block" group: ids from 1, those `hidden` names dismissed. */
function storedBlock(count: number, hidden?: (id: number) => boolean): Item[] {
  return copies(makeItem({ current: makeItemOverlay() }), 1, count, hidden);
}

/** Renders the frame for the page's run with {@link renderCard}. */
function renderPagedFrame(section: Section) {
  return renderWithProviders(
    <SectionFrame section={section} runId={RUN.id} renderItem={renderCard} />,
  );
}

// ---------------------------------------------------------------------------------------------
// The frame
// ---------------------------------------------------------------------------------------------

describe('SectionFrame: a group larger than the page', () => {
  it('counts every stored card and reads nothing until the loaded ones are shown', async () => {
    const stored = storedBlock(250);
    const api = mockApi({ 'GET /recommendations/items': serveGroup('keywords', 'block', stored) });
    await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));

    expect(cardIds()).toEqual(idRange(1, 20));
    expect(screen.getByRole('status').textContent).toBe('Showing 20 of 250 actions');
    pressShowMore(4);
    expect(cardIds()).toEqual(idRange(1, 100));
    expect(screen.getByRole('status').textContent).toBe('Showing 100 of 250 actions');
    expect(itemReads(api.calls)).toHaveLength(0);
  });

  it(
    'loads the next page past the loaded cards, reveals it and focuses its first card, to the end',
    async () => {
      const stored = storedBlock(250);
      const api = mockApi({
        'GET /recommendations/items': serveGroup('keywords', 'block', stored),
      });
      await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));
      pressShowMore(4);

      fireEvent.click(showMore() as HTMLElement);

      await waitFor(() => expect(cardIds()).toEqual(idRange(1, 120)));
      const [read] = itemReads(api.calls);
      expect(read?.method).toBe('GET');
      expect(read === undefined ? [] : [...read.search.entries()]).toEqual([
        ['run_id', String(DEFAULT_RUN_ID)],
        ['section', 'keywords'],
        ['group', 'block'],
        ['offset', '100'],
        ['limit', '100'],
      ]);
      await waitFor(() =>
        expect(document.activeElement).toBe(screen.getByRole('article', { name: 'Card 101' })),
      );
      expect(screen.getByRole('status').textContent).toBe('Showing 120 of 250 actions');

      // Every stored card is reachable, each once, in rank order.
      for (let shown = 140; shown <= 250; shown += 20) {
        fireEvent.click(showMore() as HTMLElement);
        const through = Math.min(shown, 250);
        await waitFor(() => expect(cardIds()).toEqual(idRange(1, through)));
      }
      fireEvent.click(showMore() as HTMLElement);
      await waitFor(() => expect(cardIds()).toEqual(idRange(1, 250)));
      expect(showMore()).toBeNull();
      expect(screen.getByRole('status').textContent).toBe('Showing all 250 actions');
      expect(readOffsets(api.calls)).toEqual([100, 200]);
    },
    LARGE_RENDER_TEST_TIMEOUT_MS,
  );

  it('keeps the button busy while the page loads', async () => {
    const stored = storedBlock(150);
    const serve = serveGroup('keywords', 'block', stored);
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi({
      'GET /recommendations/items': async (call) => {
        await held;
        return serve(call);
      },
    });
    await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));
    pressShowMore(4);
    const button = showMore() as HTMLElement;

    fireEvent.click(button);

    await waitFor(() => expect(button.getAttribute('aria-busy')).toBe('true'));
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.textContent).toContain('Show 20 more actions');
    release();
    await waitFor(() => expect(cardIds()).toEqual(idRange(1, 120)));
    expect(showMore()?.hasAttribute('aria-busy')).toBe(false);
  });

  it('says a failed load in the status, focuses it, and reads it again on the next press', async () => {
    const stored = storedBlock(150);
    const serve = serveGroup('keywords', 'block', stored);
    let failing = true;
    const api = mockApi({
      'GET /recommendations/items': (call) =>
        failing
          ? apiError(502, 'upstream_unavailable', 'The server is temporarily unavailable.')
          : serve(call),
    });
    await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));
    pressShowMore(4);

    fireEvent.click(showMore() as HTMLElement);

    const status = screen.getByRole('status');
    await waitFor(() =>
      expect(status.textContent).toBe(
        'Showing 100 of 150 actions. ' +
          'Could not load more actions: The server is temporarily unavailable.',
      ),
    );
    await waitFor(() => expect(document.activeElement).toBe(status));
    expect(screen.queryByRole('alert')).toBeNull();

    failing = false;
    fireEvent.click(showMore() as HTMLElement);

    await waitFor(() => expect(cardIds()).toEqual(idRange(1, 120)));
    expect(status.textContent).toBe('Showing 120 of 150 actions');
    expect(readOffsets(api.calls)).toEqual([100, 100]);
  });

  it('loads by itself while every loaded card is hidden and the group stores more', async () => {
    const stored = storedBlock(150, (id) => id <= PAGE_SIZE);
    const api = mockApi({ 'GET /recommendations/items': serveGroup('keywords', 'block', stored) });
    await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));

    await waitFor(() => expect(cardIds()).toEqual(idRange(101, 120)));
    expect(readOffsets(api.calls)).toEqual([100]);
    expect(screen.getByRole('status').textContent).toBe('Showing 20 of 50 actions');
    expect(notShownLines()).toEqual(['100 hidden since this run']);
    expect(document.querySelector('.ds-emptystate')).toBeNull();
  });

  it('counts hidden cards beyond the page in "Not shown", and shown ones in the status', async () => {
    // Two of the page's cards and five beyond it are hidden.
    const stored = storedBlock(250, (id) => id === 3 || id === 50 || (id > 200 && id <= 205));
    const api = mockApi({ 'GET /recommendations/items': serveGroup('keywords', 'block', stored) });
    await renderPagedFrame(keywordsWith(pageGroup('block', 'Block', stored)));

    expect(screen.getByRole('status').textContent).toBe('Showing 20 of 243 actions');
    expect(notShownLines()).toEqual(['7 hidden since this run']);
    expect(itemReads(api.calls)).toHaveLength(0);
  });

  it('reads nothing without a run id', () => {
    const stored = storedBlock(250);
    // No provider: the frame must not need a query client either.
    render(
      <SectionFrame
        section={keywordsWith(pageGroup('block', 'Block', stored))}
        renderItem={renderCard}
      />,
    );

    expect(cardIds()).toEqual(idRange(1, 20));
    expect(screen.getByRole('status').textContent).toBe('Showing 20 of 100 actions');
  });
});

// ---------------------------------------------------------------------------------------------
// Sections with bodies of their own
// ---------------------------------------------------------------------------------------------

describe('sections with bodies of their own keep every stored item reachable', () => {
  /** The section `key` of the default page with `group` in place of the one with its key. */
  function sectionWith(key: SectionKey, group: Group): Section {
    const section = makeRecommendationsPage().sections.find((candidate) => candidate.key === key);
    if (section === undefined) {
      throw new Error(`the default page has no ${key} section`);
    }
    return {
      ...section,
      groups: section.groups.map((candidate) => (candidate.key === group.key ? group : candidate)),
    };
  }

  it(
    'Campaigns: the discrepancy group loads its C0 cards beyond the page through its pinned list',
    async () => {
      const template = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.c0);
      // Computed from replaced data, so no card reads the live discrepancies.
      const historical = { ...template, current: { ...template.current, historical: true } };
      const stored = copies(historical, 5001, 110);
      const api = mockApi({
        'GET /recommendations/items': serveGroup('campaigns', 'discrepancies', stored),
      });
      const section = sectionWith(
        'campaigns',
        pageGroup('discrepancies', 'Discrepancies', stored),
      );
      await renderWithProviders(<CampaignsSection section={section} run={RUN} overlay={OVERLAY} />);
      const group = document.getElementById('group-campaigns-discrepancies') as HTMLElement;
      const button = () => within(group).getByRole('button', { name: /^Show \d+ more actions$/u });
      expect(batchStatus(group).textContent).toBe('Showing 20 of 110 actions');

      for (let press = 0; press < 5; press += 1) {
        fireEvent.click(button());
      }

      await waitFor(() => expect(document.getElementById(itemAnchorId(5101))).not.toBeNull());
      expect(readOffsets(api.calls)).toEqual([100]);
      expect(batchStatus(group).textContent).toBe('Showing all 110 actions');
    },
    LARGE_RENDER_TEST_TIMEOUT_MS,
  );

  it(
    'Copy/creative: a campaign block loads its cards beyond the page and keeps its variants table',
    async () => {
      const template = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.cr3);
      const stored = copies(template, 6001, 105);
      const api = mockApi({
        'GET /recommendations/items': serveGroup('creative', 'campaign:501', stored),
      });
      const page = makeRecommendationsPage();
      const title = page.sections
        .find((candidate) => candidate.key === 'creative')
        ?.groups.find((candidate) => candidate.key === 'campaign:501')?.title;
      const section = sectionWith(
        'creative',
        pageGroup('campaign:501', title ?? 'campaign:501', stored),
      );
      await renderWithProviders(<CreativeSection section={section} run={RUN} overlay={OVERLAY} />);
      const tablesBefore = screen.getAllByRole('table', { name: /^Variants in /u }).length;
      expect(tablesBefore).toBeGreaterThan(0);

      for (let press = 0; press < 5; press += 1) {
        fireEvent.click(screen.getByRole('button', { name: /^Show \d+ more actions$/u }));
      }

      await waitFor(() => expect(document.getElementById(itemAnchorId(6101))).not.toBeNull());
      expect(document.getElementById(itemAnchorId(6105))).not.toBeNull();
      expect(readOffsets(api.calls)).toEqual([100]);
      expect(screen.getAllByRole('table', { name: /^Variants in /u })).toHaveLength(tablesBefore);
    },
    LARGE_RENDER_TEST_TIMEOUT_MS,
  );

  it(
    'White papers: a cohort table loads its rows beyond the page',
    async () => {
      const template = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.wEur);
      const stored = copies(template, 7001, 120).map((item, index) => ({
        ...item,
        entity: { ...item.entity, name: `White paper ${String(index + 1)}` },
      }));
      const api = mockApi({
        'GET /config': json(200, makeConfig()),
        'GET /recommendations/items': serveGroup('white_papers', 'cohort:EUR', stored),
      });
      const section = sectionWith('white_papers', pageGroup('cohort:EUR', 'EUR', stored));
      await renderWithProviders(
        <WhitePapersSection section={section} run={RUN} overlay={OVERLAY} />,
      );
      const table = screen.getByRole('table', { name: 'White papers (EUR)' });
      expect(within(table).getAllByRole('rowheader')).toHaveLength(50);

      fireEvent.click(screen.getByRole('button', { name: 'Show 50 more white papers' }));
      expect(within(table).getAllByRole('rowheader')).toHaveLength(100);
      expect(readOffsets(api.calls)).toEqual([]);
      fireEvent.click(screen.getByRole('button', { name: 'Show 20 more white papers' }));

      await waitFor(() => expect(within(table).getAllByRole('rowheader')).toHaveLength(120));
      expect(readOffsets(api.calls)).toEqual([100]);
      expect(within(table).getByText('White paper 120')).toBeTruthy();
    },
    LARGE_RENDER_TEST_TIMEOUT_MS,
  );
});
