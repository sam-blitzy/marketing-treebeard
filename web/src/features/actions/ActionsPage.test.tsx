/**
 * Page tests for `ActionsPage`, the `/actions` screen (AAP 0.5.5 `/actions` layout; AAP 0.5.3
 * run selection; AAP 0.11.2 first start).
 *
 * The page renders inside the test router through a route component that mirrors
 * `src/router.tsx` (`/actions?run=<id>` → `<ActionsPage runId={run} />`), against the real query
 * hooks with only `fetch` stubbed. They hold the page to its TSDoc contract:
 * - **Run selection.** Without `?run=` the page reads `GET /recommendations` without `run_id`,
 *   the latest default run; with `?run=<id>` it reads that run, shows "Custom window {start} –
 *   {end}" with "Back to default", and the link returns to the default run. An older default run
 *   opened by id shows "Older default run {start} – {end}" with "Back to default"; the latest
 *   default run, by id or not, shows no run notice.
 * - **Status bar reads.** `GET /config` and `GET /sources` start before `GET /recommendations`
 *   answers; a loaded default page then reads neither `GET /runs/latest` (its own run is the
 *   latest default run) nor `GET /runs?kind=custom`, and `/actions?run=<id>` also starts
 *   `GET /runs/latest` before its page answers.
 * - **The loaded page**, top to bottom: the single `<h1>` "Actions", the status bar, the global
 *   banners in their fixed order in one stack, with the custom banner (custom runs only) right
 *   after the discrepancy banner (only the discrepancy banner is an alert, and the short-window
 *   banner reads "Window of N days is below the {trusted_min_days}-day trusted minimum"), the
 *   section navigation, the empty polite region
 *   card outcomes are announced in, then all five sections in the user's order, from one
 *   `GET /recommendations` response. A section the API omits is skipped and a repeated key
 *   renders once. A recorded feedback whose refetch hides its card is still announced there,
 *   and focus moves to the next card's action.
 * - **States.** "Loading actions" until the first response; "No analysis run yet" (404
 *   `not_found` or 409 `run_not_ready` for the default run) that fills in by itself; "Run not
 *   found" with "Back to default" for an unknown custom run (404 or 422); otherwise "Actions
 *   could not be loaded" with the API's message and a Retry that keeps the failure screen
 *   while it reads again and returns focus to its button. A failed background refetch keeps the
 *   page; a failure never carries over to another run.
 * - **Polls.** A poll in flight, a poll that returns the same page and one whose overlay differs
 *   only in `computed_at` render no section again, even when the overlay's dataset lists arrive
 *   as new arrays equal in value; a changed overlay field, dataset list or item still reaches
 *   the screen, every section renders again for a changed list, and only the section holding a
 *   changed item renders again.
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { replaceEqualDeep } from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';
import { useSearch } from '@tanstack/react-router';
import { Profiler } from 'react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from '../../api/queries';
import { SECTION_ORDER } from '../../api/types';
import type { RecommendationsPage, Run, SectionKey } from '../../api/types';
import {
  CUSTOM_RUN_ID,
  DEFAULT_RUN_ID,
  FIXTURE_BANNERS,
  FIXTURE_ITEM_IDS,
  FIXTURE_SECTION_TITLES,
  findPageItem,
  makeConfig,
  makeDiscrepancy,
  makeFeedback,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
  makeSources,
} from '../../test/fixtures';
import { createTestQueryClient, renderWithProviders } from '../../test/render';
import * as statusBarModule from '../status/StatusBar';
import { ActionsPage, SECTION_COMPONENTS } from './ActionsPage';
import * as sectionFrameModule from './sections/SectionFrame';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The five section titles, in the user's order. */
const SECTION_TITLES = SECTION_ORDER.map((key) => FIXTURE_SECTION_TITLES[key]);

/** A 10-day custom run, flagged short against the 14-day trusted minimum. */
function customRun(overrides: Partial<Run> = {}): Run {
  return makeRun({
    id: CUSTOM_RUN_ID,
    kind: 'custom',
    trigger: 'manual',
    window_start: '2026-09-26',
    window_end: '2026-10-05',
    days: 10,
    short_window: true,
    ...overrides,
  });
}

/** The custom run's page: the run, every fixture banner (short window included). */
function customPage(): RecommendationsPage {
  return makeRecommendationsPage({
    run: customRun(),
    banners: FIXTURE_BANNERS.map((banner) => ({ ...banner })),
  });
}

/** The id of an older default run, opened at `/actions?run=6` after later default runs. */
const OLDER_DEFAULT_RUN_ID = 6;

/**
 * The page of a default run that finished a week before the latest one (`makeRun()`, which
 * `GET /runs/latest` returns): the run and every fixture banner.
 */
function olderDefaultPage(): RecommendationsPage {
  return makeRecommendationsPage({
    run: makeRun({
      id: OLDER_DEFAULT_RUN_ID,
      window_start: '2026-09-01',
      window_end: '2026-09-28',
      days: 28,
      finished_at: '2026-09-29T06:00:00Z',
    }),
    banners: FIXTURE_BANNERS.map((banner) => ({ ...banner })),
  });
}

/**
 * The reads the page starts beside `GET /recommendations` in every state, before any data
 * arrives: the status bar's configuration and sources (`usePrefetchActionsReads`).
 */
function prefetchedReads(): MockHandlers {
  return {
    'GET /config': json(200, makeConfig()),
    'GET /sources': json(200, makeSources()),
  };
}

/**
 * The reads a loaded page makes besides `GET /recommendations`: the window picker's
 * configuration and default run, the source chips' sources, and the Campaigns section's
 * discrepancies (White papers shares the configuration read). The recent custom runs are read
 * only once the user opens them, so `GET /runs` has no handler and a read of it fails the test.
 */
function pageReads(): MockHandlers {
  return {
    ...prefetchedReads(),
    'GET /runs/latest': json(200, makeRun()),
    'GET /discrepancies': json(200, [makeDiscrepancy('status')]),
  };
}

/**
 * A test client whose reads stay fresh for 30 s, the app's own default (`QUERY_STALE_MS` in
 * `src/main.tsx`), so a seeded or prefetched entry is reused as in the app rather than read
 * again on mount, as the default test client's `staleTime: 0` would.
 */
function appFreshClient(): QueryClient {
  const client = createTestQueryClient();
  const defaults = client.getDefaultOptions();
  client.setDefaultOptions({ ...defaults, queries: { ...defaults.queries, staleTime: 30_000 } });
  return client;
}

/** The paths of the `GET` requests sent so far, in request order. */
function readPaths(api: ApiMock): string[] {
  return api.calls.filter((call) => call.method === 'GET').map((call) => call.path);
}

/** Handlers for a loaded default page, plus `extra`. */
function defaultHandlers(extra: MockHandlers = {}): MockHandlers {
  return {
    'GET /recommendations': json(200, makeRecommendationsPage()),
    ...pageReads(),
    ...extra,
  };
}

/** A handler that answers only when the test calls `release`, and the gate that releases it. */
function gated(): {
  handler: () => Promise<MockResponse>;
  gate: { release?: (response: MockResponse) => void };
} {
  const gate: { release?: (response: MockResponse) => void } = {};
  return {
    gate,
    handler: () =>
      new Promise<MockResponse>((resolve) => {
        gate.release = resolve;
      }),
  };
}

/** The `/actions` route component of `src/router.tsx`: the run comes from `?run=<id>`. */
function ActionsRoute(): ReactElement {
  const { run } = useSearch({ strict: false });
  return <ActionsPage runId={run} />;
}

/** Renders the page at `path` (`/actions` by default) with `handlers`. */
async function renderPage(handlers: MockHandlers, path = '/actions') {
  const api = mockApi(handlers);
  const result = await renderWithProviders(<ActionsRoute />, { path });
  return { ...result, api };
}

/** The `GET /recommendations` requests sent so far. */
function recommendationReads(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/recommendations');
}

/** The level-2 headings, in document order: the section titles. */
function sectionHeadings(): (string | null)[] {
  return screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);
}

/** Waits until the loaded page is on screen: its five sections, or as many as `count`. */
async function loadedPage(count = SECTION_TITLES.length): Promise<void> {
  await waitFor(() => {
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(count);
  });
}

/**
 * The polite status region that holds the page's state notice. Before data, it is the page's
 * only `<div role="status">` (the design-system `Spinner` is a `<span role="status">`).
 */
function noticeRegion(): HTMLElement {
  const regions = document.querySelectorAll<HTMLElement>('div[role="status"]');
  expect(regions).toHaveLength(1);
  const region = regions[0];
  if (region === undefined) {
    throw new Error('the page has no notice region');
  }
  return region;
}

/** Whether `first` precedes `second` in document order. */
function precedes(first: Element, second: Element): boolean {
  return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

/**
 * The loaded page's card-outcome region (`ActionOutcomeProvider`): the one
 * `<div role="status">` between the section navigation and the first section.
 */
function outcomeRegion(): HTMLElement {
  const nav = screen.getByRole('navigation', { name: 'Sections' });
  const keywords = screen.getByRole('region', { name: 'Keywords to adjust' });
  const regions = Array.from(document.querySelectorAll<HTMLElement>('div[role="status"]')).filter(
    (region) => precedes(nav, region) && precedes(region, keywords),
  );
  expect(regions).toHaveLength(1);
  const [region] = regions;
  if (region === undefined) {
    throw new Error('the page has no outcome region');
  }
  return region;
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('ActionsPage', () => {
  it('maps each of the five section keys to its own frozen section component', () => {
    expect(Object.keys(SECTION_COMPONENTS)).toEqual([...SECTION_ORDER]);
    expect(new Set(Object.values(SECTION_COMPONENTS)).size).toBe(SECTION_ORDER.length);
    expect(Object.isFrozen(SECTION_COMPONENTS)).toBe(true);
  });

  describe('the loaded page', () => {
    it('shows the latest default run: all five sections in order, from one request without run_id', async () => {
      const { api } = await renderPage(defaultHandlers());

      await loadedPage();
      expect(sectionHeadings()).toEqual(SECTION_TITLES);
      expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual([
        'Actions',
      ]);
      // Every section region is the anchor its navigation link points at.
      const nav = screen.getByRole('navigation', { name: 'Sections' });
      for (const link of within(nav).getAllByRole('link')) {
        const target = link.getAttribute('href')?.slice(1) ?? '';
        expect(document.getElementById(target)?.tagName).toBe('SECTION');
      }
      expect(within(nav).getAllByRole('link').map((link) => link.textContent)).toEqual([
        'Keywords to adjust 3',
        'Campaigns to pause or launch 5',
        'Budget changes 2',
        'Copy/creative changes 2',
        'White papers 3',
      ]);

      // One precomputed response: no run_id, and no per-item request.
      const reads = recommendationReads(api);
      expect(reads).toHaveLength(1);
      expect(reads[0]?.search.has('run_id')).toBe(false);
      expect(api.calls.some((call) => /^\/recommendations\/./u.test(call.path))).toBe(false);
      // A default run has no custom-window banner.
      expect(screen.queryByText(/^Custom window /u)).toBeNull();
    });

    it('stacks status bar, global banners and section navigation above the sections', async () => {
      await renderPage(defaultHandlers());
      await loadedPage();

      const status = screen.getByRole('region', { name: 'Analysis status' });
      const alerts = screen.getAllByRole('alert');
      // AAP 0.5.5: banners use the alert role only for discrepancies.
      expect(alerts).toHaveLength(1);
      const discrepancies = alerts[0];
      if (discrepancies === undefined) {
        throw new Error('no discrepancy banner');
      }
      expect(within(discrepancies).getByText('1 open discrepancy to resolve first')).toBeTruthy();
      // The other default-page banners, tracking, simulated sources and incomplete data, in order.
      const [tracking, simulated, incomplete] = FIXTURE_BANNERS.slice(1, 4).map((banner) =>
        screen.getByText(banner.message),
      );
      const nav = screen.getByRole('navigation', { name: 'Sections' });
      const keywords = screen.getByRole('region', { name: 'Keywords to adjust' });
      const ordered = [status, discrepancies, tracking, simulated, incomplete, nav, keywords];
      for (const [index, element] of ordered.entries()) {
        const next = ordered[index + 1];
        if (element === undefined || next === undefined) {
          continue;
        }
        expect(precedes(element, next)).toBe(true);
      }
      // The run spans 28 days, so there is no short-window banner.
      expect(screen.queryByText(/is below the \d+-day trusted minimum/u)).toBeNull();
    });

    it('shows the run addressed by ?run=<id>, with its custom window and short-window banners', async () => {
      const { api, router } = await renderPage(
        defaultHandlers({ 'GET /recommendations?run_id=202': json(200, customPage()) }),
        `/actions?run=${String(CUSTOM_RUN_ID)}`,
      );

      await loadedPage();
      const reads = recommendationReads(api);
      expect(reads.map((call) => call.search.get('run_id'))).toEqual([String(CUSTOM_RUN_ID)]);

      const banner = document.getElementById('banner-custom-window');
      if (banner === null) {
        throw new Error('no custom-window banner');
      }
      expect(within(banner).getByText('Custom window 2026-09-26 – 2026-10-05')).toBeTruthy();
      expect(within(banner).getByText('10 days')).toBeTruthy();
      expect(banner.getAttribute('role')).toBeNull();
      // The discrepancy banner sits above everything; the custom-window banner follows it in the
      // same stack as every other global banner, so it is spaced like them.
      const alerts = screen.getAllByRole('alert');
      expect(alerts).toHaveLength(1);
      const [discrepancies] = alerts;
      if (discrepancies === undefined) {
        throw new Error('no discrepancy banner');
      }
      expect(precedes(discrepancies, banner)).toBe(true);
      expect(discrepancies.nextElementSibling).toBe(banner);
      expect(banner.parentElement).toBe(discrepancies.parentElement);
      expect(
        screen.getByText(FIXTURE_BANNERS[1]?.message ?? '').closest('.ds-banner')?.parentElement,
      ).toBe(banner.parentElement);
      // The short-window banner uses the run's trusted minimum.
      const shortWindow = screen.getByText('Window of 10 days is below the 14-day trusted minimum');
      // It comes last in the fixed banner order, after the incomplete-data banner.
      const incomplete = screen.getByText(FIXTURE_BANNERS[3]?.message ?? '');
      expect(precedes(incomplete, shortWindow)).toBe(true);
      expect(precedes(shortWindow, screen.getByRole('navigation', { name: 'Sections' }))).toBe(true);
      const nav = screen.getByRole('navigation', { name: 'Sections' });
      expect(precedes(banner, nav)).toBe(true);
      expect(sectionHeadings()).toEqual(SECTION_TITLES);

      // "Back to default" leaves the custom run for the latest default run.
      const back = within(banner).getByRole('link', { name: 'Back to default' });
      expect(back.getAttribute('aria-current')).toBeNull();
      fireEvent.click(back);
      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      await waitFor(() => {
        expect(document.getElementById('banner-custom-window')).toBeNull();
      });
      await loadedPage();
      expect(recommendationReads(api).map((call) => call.search.get('run_id'))).toEqual([
        String(CUSTOM_RUN_ID),
        null,
      ]);
    });

    it('marks an older default run opened by id as not the latest, with a way back', async () => {
      const { api, router } = await renderPage(
        defaultHandlers({
          [`GET /recommendations?run_id=${String(OLDER_DEFAULT_RUN_ID)}`]: json(200, olderDefaultPage()),
        }),
        `/actions?run=${String(OLDER_DEFAULT_RUN_ID)}`,
      );

      await loadedPage();
      const banner = await waitFor(() => {
        const element = document.getElementById('banner-older-default-run');
        if (element === null) {
          throw new Error('no older-default-run banner');
        }
        return element;
      });
      expect(within(banner).getByText('Older default run 2026-09-01 – 2026-09-28')).toBeTruthy();
      expect(within(banner).getByText('28 days · not the latest default run')).toBeTruthy();
      expect(banner.getAttribute('role')).toBeNull();
      expect(document.getElementById('banner-custom-window')).toBeNull();
      // Like the custom-window banner: right after the discrepancy banner, in the same stack.
      const [discrepancies] = screen.getAllByRole('alert');
      expect(discrepancies?.nextElementSibling).toBe(banner);

      const back = within(banner).getByRole('link', { name: 'Back to default' });
      expect(back.getAttribute('aria-current')).toBeNull();
      fireEvent.click(back);
      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      await waitFor(() => {
        expect(document.getElementById('banner-older-default-run')).toBeNull();
      });
      await loadedPage();
      expect(recommendationReads(api).map((call) => call.search.get('run_id'))).toEqual([
        String(OLDER_DEFAULT_RUN_ID),
        null,
      ]);
    });

    it('shows no run notice for the latest default run opened by id', async () => {
      const { queryClient } = await renderPage(
        defaultHandlers({
          [`GET /recommendations?run_id=${String(DEFAULT_RUN_ID)}`]: json(
            200,
            makeRecommendationsPage(),
          ),
        }),
        `/actions?run=${String(DEFAULT_RUN_ID)}`,
      );

      await loadedPage();
      // The latest default run has been read: it is the run on screen.
      await waitFor(() => {
        expect(queryClient.getQueryData<Run>(queryKeys.runs.latest)?.id).toBe(DEFAULT_RUN_ID);
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(document.getElementById('banner-older-default-run')).toBeNull();
      expect(document.getElementById('banner-custom-window')).toBeNull();
    });

    it('never marks the default page as older, even when GET /runs/latest has a newer run', async () => {
      const newer = makeRun({ id: DEFAULT_RUN_ID + 1, finished_at: '2026-10-07T06:00:00Z' });
      const { queryClient } = await renderPage(
        defaultHandlers({ 'GET /runs/latest': json(200, newer) }),
      );

      await loadedPage();
      // The default test client reads the seeded entry again at once, and gets the newer run,
      // while the page still shows the default run it read first.
      await waitFor(() => {
        expect(queryClient.getQueryData<Run>(queryKeys.runs.latest)?.id).toBe(newer.id);
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(SECTION_TITLES.length);
      expect(document.getElementById('banner-older-default-run')).toBeNull();
      expect(screen.queryByText(/not the latest default run/u)).toBeNull();
    });

    it('skips a section the API leaves out and renders a repeated key once', async () => {
      const page = makeRecommendationsPage();
      page.sections = page.sections
        .filter((section) => section.key !== 'creative')
        .concat(makeSection('keywords', { title: 'Repeated keywords' }));
      await renderPage(defaultHandlers({ 'GET /recommendations': json(200, page) }));

      await loadedPage(4);
      expect(sectionHeadings()).toEqual(
        SECTION_TITLES.filter((title) => title !== FIXTURE_SECTION_TITLES.creative),
      );
      expect(screen.queryByText('Repeated keywords')).toBeNull();
    });

    it('keeps the page on screen when a background refetch fails', async () => {
      const { api, queryClient } = await renderPage(defaultHandlers());
      await loadedPage();

      api.use({
        'GET /recommendations': apiError(503, 'unavailable', 'The API is restarting'),
      });
      await queryClient.refetchQueries({ queryKey: queryKeys.recommendationsAll });

      expect(recommendationReads(api)).toHaveLength(2);
      expect(sectionHeadings()).toEqual(SECTION_TITLES);
      expect(screen.queryByText('Actions could not be loaded')).toBeNull();
      expect(screen.queryByText('The API is restarting')).toBeNull();
    });

    it('holds an empty polite outcome region between the navigation and the sections', async () => {
      await renderPage(defaultHandlers());
      await loadedPage();

      const region = outcomeRegion();
      expect(region.getAttribute('role')).toBe('status');
      expect(region.childNodes).toHaveLength(0);
      expect(region.closest('article')).toBeNull();
    });

    it('announces a recorded feedback after its card leaves and focuses the next card', async () => {
      const hidden = new Set<number>();
      const pageNow = (): RecommendationsPage => {
        const page = makeRecommendationsPage();
        for (const id of hidden) {
          findPageItem(page, id).current.hidden_by = 'dismiss';
        }
        return page;
      };
      await renderPage(
        defaultHandlers({
          'GET /recommendations': () => json(200, pageNow()),
          'POST /recommendations/:id/feedback': (call) => {
            const id = Number(call.params.id);
            hidden.add(id);
            return json(201, makeFeedback('dismiss', { recommendation_id: id }));
          },
        }),
      );
      await loadedPage();
      const pause = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.c1);
      const hold = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.c2Hold);
      const card = (id: number): HTMLElement | null =>
        document.getElementById(`item-${String(id)}`);
      const pauseCard = card(pause.id);
      if (pauseCard === null) {
        throw new Error('the C1 card is not on the page');
      }

      const dismiss = within(pauseCard).getByRole('button', { name: 'Dismiss' });
      dismiss.focus();
      fireEvent.click(dismiss);

      // SectionFrame drops the hidden item; the region above the sections keeps its outcome.
      await waitFor(() => {
        expect(card(pause.id)).toBeNull();
      });
      expect(outcomeRegion().textContent).toBe(
        `Dismissed: ${pause.action} (${String(pause.entity.name)})`,
      );
      // The held card next in the Pause group is collapsed, so the Scale group's card is next.
      const holdCard = card(hold.id);
      if (holdCard === null) {
        throw new Error('the C2 Hold card is not on the page');
      }
      const next = within(holdCard).getByRole('button', { name: 'Mark done' });
      await waitFor(() => {
        expect(document.activeElement).toBe(next);
      });
    });
  });

  describe('status bar reads', () => {
    it('starts GET /config and GET /sources before the default page answers, and reads no run metadata', async () => {
      const { handler, gate } = gated();
      const api = mockApi(defaultHandlers({ 'GET /recommendations': handler }));
      const queryClient = appFreshClient();
      await renderWithProviders(<ActionsRoute />, { queryClient });

      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      await waitFor(() => {
        expect([...new Set(readPaths(api))].sort()).toEqual([
          '/config',
          '/recommendations',
          '/sources',
        ]);
      });
      // GET /recommendations has not answered yet.
      expect(screen.getByText('Loading actions')).toBeTruthy();

      gate.release?.(json(200, makeRecommendationsPage()));
      await loadedPage();
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });

      // The picker's latest default run is the page's own run, and the recent custom runs wait
      // until they are opened: no GET /runs/latest and no GET /runs?kind=custom.
      expect(readPaths(api).filter((path) => path.startsWith('/runs'))).toEqual([]);
      expect(screen.getByRole('button', { name: 'Recent custom windows' })).toBeTruthy();
      // The status bar took the prefetched entries over without reading them again.
      expect(readPaths(api).filter((path) => path === '/config')).toHaveLength(1);
      expect(readPaths(api).filter((path) => path === '/sources')).toHaveLength(1);
    });

    it('also starts GET /runs/latest before an addressed run answers, on /actions?run=<id>', async () => {
      const { handler, gate } = gated();
      const api = mockApi(
        defaultHandlers({ [`GET /recommendations?run_id=${String(CUSTOM_RUN_ID)}`]: handler }),
      );
      const queryClient = appFreshClient();
      await renderWithProviders(<ActionsRoute />, {
        queryClient,
        path: `/actions?run=${String(CUSTOM_RUN_ID)}`,
      });

      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      await waitFor(() => {
        expect([...new Set(readPaths(api))].sort()).toEqual([
          '/config',
          '/recommendations',
          '/runs/latest',
          '/sources',
        ]);
      });
      expect(screen.getByText('Loading actions')).toBeTruthy();

      gate.release?.(json(200, customPage()));
      await loadedPage();
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });

      // The picker reuses the prefetched default run, and still reads no custom-run list.
      expect(readPaths(api).filter((path) => path === '/runs/latest')).toHaveLength(1);
      expect(readPaths(api).filter((path) => path === '/runs')).toEqual([]);
    });
  });

  describe('states before data', () => {
    it('shows the title and "Loading actions" until the first response arrives', async () => {
      const { handler, gate } = gated();
      await renderPage(defaultHandlers({ 'GET /recommendations': handler }));

      expect(screen.getByRole('heading', { level: 1, name: 'Actions' })).toBeTruthy();
      const spinner = screen.getByText('Loading actions');
      expect(spinner.closest('[role="status"]')).not.toBeNull();
      expect(noticeRegion().textContent).toBe('');
      expect(screen.queryByRole('heading', { level: 2 })).toBeNull();

      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      gate.release?.(json(200, makeRecommendationsPage()));

      await loadedPage();
      expect(screen.queryByText('Loading actions')).toBeNull();
    });

    it.each([
      ['404 not_found', apiError(404, 'not_found', 'No default analysis run has succeeded yet')],
      ['409 run_not_ready', apiError(409, 'run_not_ready', 'The default run is still running')],
    ])(
      'says "No analysis run yet" on %s for the default run and fills in by itself',
      async (_label, response) => {
        // Only GET /recommendations and the status bar's prefetched reads: no run metadata or
        // section read is sent before the first run exists.
        const { api, queryClient } = await renderPage({
          'GET /recommendations': response,
          ...prefetchedReads(),
        });

        await screen.findByText('No analysis run yet');
        const notice = noticeRegion();
        expect(within(notice).getByText('No analysis run yet')).toBeTruthy();
        expect(
          within(notice).getByText(
            'The first analysis is still computing. This page refreshes automatically.',
          ),
        ).toBeTruthy();
        expect(screen.getByText('Waiting for the first analysis run')).toBeTruthy();
        expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
        expect(screen.queryByRole('alert')).toBeNull();
        expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);

        // The next poll finds the first run. While it runs, the state stays on screen.
        const { handler, gate } = gated();
        api.use({ ...pageReads(), 'GET /recommendations': handler });
        const refetch = queryClient.refetchQueries({ queryKey: queryKeys.recommendationsAll });
        await waitFor(() => {
          expect(gate.release).toBeDefined();
        });
        expect(screen.getByText('No analysis run yet')).toBeTruthy();
        expect(screen.queryByText('Loading actions')).toBeNull();
        gate.release?.(json(200, makeRecommendationsPage()));
        await refetch;

        await loadedPage();
        expect(screen.queryByText('No analysis run yet')).toBeNull();
      },
    );

    it.each([
      ['404 not_found', apiError(404, 'not_found', 'Run 999 not found')],
      ['422 validation_error', apiError(422, 'validation_error', 'run_id must be positive')],
    ])(
      'says "Run not found" on %s for an addressed run, with a link back to the default run',
      async (_label, response) => {
        const { router, api } = await renderPage(
          defaultHandlers({ 'GET /recommendations?run_id=999': response }),
          '/actions?run=999',
        );

        await screen.findByText('Run not found');
        expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
        const back = within(noticeRegion()).getByRole('link', { name: 'Back to default' });
        expect(back.getAttribute('href')).toBe('/actions');
        expect(back.getAttribute('aria-current')).toBeNull();

        fireEvent.click(back);
        await waitFor(() => {
          expect(router.state.location.search).toEqual({});
        });
        await loadedPage();
        expect(recommendationReads(api).map((call) => call.search.get('run_id'))).toEqual([
          '999',
          null,
        ]);
      },
    );

    it('never carries a failure over to another run: the default run shows "Loading actions" first', async () => {
      const { handler, gate } = gated();
      await renderPage(
        defaultHandlers({
          'GET /recommendations?run_id=999': apiError(404, 'not_found', 'Run 999 not found'),
          'GET /recommendations': handler,
        }),
        '/actions?run=999',
      );
      await screen.findByText('Run not found');

      fireEvent.click(within(noticeRegion()).getByRole('link', { name: 'Back to default' }));

      await screen.findByText('Loading actions');
      expect(screen.queryByText('Run not found')).toBeNull();
      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      gate.release?.(json(200, makeRecommendationsPage()));
      await loadedPage();
    });

    it('treats run_not_ready for an addressed run as a failure to retry, not a first start', async () => {
      await renderPage(
        defaultHandlers({
          'GET /recommendations?run_id=202': apiError(409, 'run_not_ready', 'Run 202 failed'),
        }),
        '/actions?run=202',
      );

      await screen.findByText('Actions could not be loaded');
      expect(within(noticeRegion()).getByText('Run 202 failed')).toBeTruthy();
      expect(screen.queryByText('No analysis run yet')).toBeNull();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
    });
  });

  describe('failure and Retry', () => {
    it('shows the API message and a Retry that keeps the screen while it reads again', async () => {
      const { api } = await renderPage({
        'GET /recommendations': apiError(500, 'internal_error', 'Database unavailable'),
        ...prefetchedReads(),
      });

      await screen.findByText('Actions could not be loaded');
      expect(within(noticeRegion()).getByText('Database unavailable')).toBeTruthy();
      expect(screen.queryByRole('alert')).toBeNull();
      const retry = screen.getByRole('button', { name: 'Retry' });
      expect(retry.hasAttribute('disabled')).toBe(false);

      // A Retry that fails again: the button is busy meanwhile, then gets focus back.
      const first = gated();
      api.use({ 'GET /recommendations': first.handler });
      retry.focus();
      fireEvent.click(retry);
      await waitFor(() => {
        expect(first.gate.release).toBeDefined();
      });
      // The loading Retry is aria-disabled, never natively disabled, so it keeps focus.
      const busy = screen.getByRole('button', { name: /Retry/u });
      expect(busy.getAttribute('aria-disabled')).toBe('true');
      expect(busy.hasAttribute('disabled')).toBe(false);
      expect(document.activeElement).toBe(busy);
      expect(screen.getByText('Actions could not be loaded')).toBeTruthy();
      expect(screen.queryByText('Loading actions')).toBeNull();
      // Focus lost to the page body meanwhile, as when the user clicks the page background,
      // still goes back to Retry once the read settles.
      const title = screen.getByRole('heading', { level: 1, name: 'Actions' });
      title.tabIndex = -1;
      title.focus();
      title.blur();
      expect(document.activeElement).toBe(document.body);
      first.gate.release?.(apiError(500, 'internal_error', 'Database still unavailable'));

      await screen.findByText('Database still unavailable');
      const again = screen.getByRole('button', { name: 'Retry' });
      await waitFor(() => {
        expect(again.hasAttribute('disabled')).toBe(false);
      });
      expect(again).toBe(retry);
      await waitFor(() => {
        expect(document.activeElement).toBe(again);
      });

      // A Retry that succeeds shows the page.
      api.use({ ...pageReads(), 'GET /recommendations': json(200, makeRecommendationsPage()) });
      fireEvent.click(again);
      await loadedPage();
      expect(screen.queryByText('Actions could not be loaded')).toBeNull();
      expect(recommendationReads(api)).toHaveLength(3);
    });

    it('leaves focus where the user moved it while a Retry ran', async () => {
      const { api } = await renderPage({
        'GET /recommendations': apiError(500, 'internal_error', 'Database unavailable'),
        ...prefetchedReads(),
      });
      await screen.findByText('Actions could not be loaded');
      const retry = screen.getByRole('button', { name: 'Retry' });

      const pending = gated();
      api.use({ 'GET /recommendations': pending.handler });
      retry.focus();
      fireEvent.click(retry);
      await waitFor(() => {
        expect(pending.gate.release).toBeDefined();
      });
      // Meanwhile the user moves focus elsewhere: here, to the page title, made focusable.
      const heading = screen.getByRole('heading', { level: 1, name: 'Actions' });
      heading.tabIndex = -1;
      heading.focus();
      pending.gate.release?.(apiError(500, 'internal_error', 'Still unavailable'));

      await screen.findByText('Still unavailable');
      await waitFor(() => {
        expect(screen.getByRole('button', { name: 'Retry' }).hasAttribute('disabled')).toBe(false);
      });
      expect(document.activeElement).toBe(heading);
    });

    it('falls back to a generic reason when the API message is blank', async () => {
      await renderPage({
        'GET /recommendations': apiError(500, 'internal_error', '   '),
        ...prefetchedReads(),
      });

      await screen.findByText('Actions could not be loaded');
      expect(
        within(noticeRegion()).getByText('The recommendations could not be read. Try again.'),
      ).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Polls: what renders again
// ---------------------------------------------------------------------------------------------

describe('ActionsPage polls', () => {
  /** A later read instant than the fixture overlay's `computed_at`, one poll after it. */
  const NEXT_READ_AT = '2026-10-06T06:00:30Z';

  afterEach(() => {
    vi.restoreAllMocks();
  });

  /**
   * Renders the default `/actions` page with `handlers` inside a `Profiler` that counts the
   * page's commits, so a test can wait for a render it then asserts on. `queryClient` is the
   * client to render with, a new test client by default.
   */
  async function renderProfiledPage(
    handlers: MockHandlers,
    queryClient: QueryClient = createTestQueryClient(),
  ) {
    const commits = { count: 0 };
    const api = mockApi(handlers);
    const result = await renderWithProviders(
      <Profiler
        id="actions-page"
        onRender={() => {
          commits.count += 1;
        }}
      >
        <ActionsRoute />
      </Profiler>,
      { queryClient },
    );
    return { ...result, api, commits };
  }

  /** The flush passes {@link settle} runs before it fails the test as a page that never settles. */
  const SETTLE_MAX_PASSES = 20;

  /**
   * Waits until the page is idle, so render counts taken afterwards change only through the
   * refetch under test. `page` is what {@link renderProfiledPage} returned: the client the page
   * reads through and its commit counter.
   *
   * It never waits on elapsed time. TanStack Query delivers every observer notification from a
   * zero-delay timer, scheduled in the same dispatch that ends a read, so once `isFetching()`
   * reads 0 the notifications of that read are already queued. Equal-delay timers run in the
   * order they were created, so a zero-delay timer created afterwards inside `act` runs after
   * them, and `act` then commits the renders they caused and runs their effects. Those effects
   * can start reads or change queries again, and a change whose notification is still queued
   * has already shown as a query-cache event, so the flush repeats until a pass in which the
   * page commits nothing, the query cache reports no event and no read is in flight.
   */
  async function settle(page: {
    queryClient: QueryClient;
    commits: { count: number };
  }): Promise<void> {
    const { queryClient, commits } = page;
    let cacheEvents = 0;
    const unsubscribe = queryClient.getQueryCache().subscribe(() => {
      cacheEvents += 1;
    });
    try {
      for (let pass = 0; pass < SETTLE_MAX_PASSES; pass += 1) {
        await waitFor(() => {
          expect(queryClient.isFetching()).toBe(0);
        });
        const commitsBefore = commits.count;
        const cacheEventsBefore = cacheEvents;
        await act(async () => {
          await new Promise<void>((resolve) => {
            setTimeout(resolve, 0);
          });
        });
        if (
          commits.count === commitsBefore &&
          cacheEvents === cacheEventsBefore &&
          queryClient.isFetching() === 0
        ) {
          return;
        }
      }
      throw new Error(`the page did not settle within ${String(SETTLE_MAX_PASSES)} flush passes`);
    } finally {
      unsubscribe();
    }
  }

  /**
   * Starts a refetch of the page whose `GET /recommendations` answers only on `release`, and
   * resolves once that request is in flight.
   */
  async function startPoll(api: ApiMock, queryClient: QueryClient) {
    const { handler, gate } = gated();
    api.use({ 'GET /recommendations': handler });
    const done = queryClient.refetchQueries({ queryKey: queryKeys.recommendationsAll });
    await waitFor(() => {
      expect(gate.release).toBeDefined();
    });
    return {
      done,
      release: (response: MockResponse) => {
        gate.release?.(response);
      },
    };
  }

  /** The region of one section, found by its anchor id. */
  function sectionRegion(key: SectionKey): HTMLElement {
    const region = document.getElementById(sectionFrameModule.sectionAnchorId(key));
    if (region === null) {
      throw new Error(`the page has no ${key} section`);
    }
    return region;
  }

  /** The default page with an empty discrepancy group, and `openNow` open discrepancies now. */
  function pageWithoutDiscrepancyCards(openNow: number): RecommendationsPage {
    const page = makeRecommendationsPage({ overlay: makeOverlay({ open_discrepancies: openNow }) });
    page.sections = page.sections.map((section) =>
      section.key !== 'campaigns'
        ? section
        : {
            ...section,
            groups: section.groups.map((group) =>
              group.key === 'discrepancies' ? { ...group, items: [] } : group,
            ),
          },
    );
    return page;
  }

  /** The source chip of one source in the status bar, found by the name it starts with. */
  function sourceChip(name: string): HTMLElement {
    const chips = within(screen.getByRole('list', { name: 'Data sources' })).getAllByRole(
      'listitem',
    );
    const chip = chips.find((item) => item.textContent?.startsWith(name) === true);
    if (chip === undefined) {
      throw new Error(`the status bar has no ${name} source chip`);
    }
    return chip;
  }

  /**
   * The default page's banners in the API's order with a dataset banner of a later read: a
   * stale-data `banner` goes just before the incomplete-data banner, and an incomplete-data
   * `banner` replaces it.
   */
  function bannersWith(
    banner: RecommendationsPage['banners'][number] & {
      code: 'stale_datasets' | 'incomplete_datasets';
    },
  ) {
    const banners = FIXTURE_BANNERS.slice(0, 4).map((entry) => ({ ...entry }));
    const incomplete = banners.findIndex((entry) => entry.code === 'incomplete_datasets');
    if (incomplete === -1) {
      throw new Error('the default page has no incomplete-data banner');
    }
    banners.splice(incomplete, banner.code === 'incomplete_datasets' ? 1 : 0, banner);
    return banners;
  }

  /** Whether a cached query value is a `GET /recommendations` page. */
  function isRecommendationsPage(value: unknown): value is RecommendationsPage {
    return typeof value === 'object' && value !== null && 'overlay' in value && 'sections' in value;
  }

  /**
   * A test client that shares each refetched page with the one it replaces, as TanStack Query
   * does by default, except that the overlay's dataset lists always arrive as new arrays. An
   * unchanged list is then equal in value to the one on screen but not the same array, as from
   * any client that does not share structure, so a section's overlay comparison reaches its
   * lists and has to compare them entry by entry. Every other part of the page keeps its
   * identity when unchanged, so the sections' `section` and `run` still compare as the same.
   */
  function freshOverlayListsClient(): QueryClient {
    const client = createTestQueryClient();
    client.setQueryDefaults(queryKeys.recommendationsAll, {
      structuralSharing: (previous: unknown, next: unknown): unknown => {
        const shared = replaceEqualDeep(previous, next);
        if (!isRecommendationsPage(shared)) {
          return shared;
        }
        const { overlay } = shared;
        return {
          ...shared,
          overlay: {
            ...overlay,
            stale_datasets: [...overlay.stale_datasets],
            incomplete_datasets: [...overlay.incomplete_datasets],
          },
        };
      },
    });
    return client;
  }

  /** The latest default page the client holds, the one on screen. */
  function shownPage(queryClient: QueryClient): RecommendationsPage {
    const page = queryClient.getQueryData<RecommendationsPage>(
      queryKeys.recommendations('default'),
    );
    if (page === undefined) {
      throw new Error('the client holds no default page');
    }
    return page;
  }

  it('renders no section again for a poll that returns the same page, or one that differs only in overlay.computed_at', async () => {
    const frames = vi.spyOn(sectionFrameModule, 'SectionFrame');
    const statusBars = vi.spyOn(statusBarModule, 'StatusBar');
    const { api, queryClient, commits } = await renderProfiledPage(defaultHandlers());
    await loadedPage();
    await settle({ queryClient, commits });
    const sectionRenders = frames.mock.calls.length;
    const statusRenders = statusBars.mock.calls.length;
    expect(sectionRenders).toBeGreaterThanOrEqual(SECTION_ORDER.length);
    expect(statusRenders).toBeGreaterThanOrEqual(1);

    // A poll in flight re-renders the page for its fetch status alone; the content does not.
    const beforeFetching = commits.count;
    const same = await startPoll(api, queryClient);
    await waitFor(() => {
      expect(commits.count).toBeGreaterThan(beforeFetching);
    });
    expect(statusBars.mock.calls).toHaveLength(statusRenders);
    expect(frames.mock.calls).toHaveLength(sectionRenders);

    // It returns a page equal to the one shown: the page renders as it goes idle, nothing else.
    const beforeIdle = commits.count;
    same.release(json(200, makeRecommendationsPage()));
    await same.done;
    await waitFor(() => {
      expect(commits.count).toBeGreaterThan(beforeIdle);
    });
    expect(recommendationReads(api)).toHaveLength(2);
    expect(statusBars.mock.calls).toHaveLength(statusRenders);
    expect(frames.mock.calls).toHaveLength(sectionRenders);

    // The next poll returns a new read instant only: the status bar takes the new overlay, while
    // no section renders again.
    const later = await startPoll(api, queryClient);
    later.release(
      json(200, makeRecommendationsPage({ overlay: makeOverlay({ computed_at: NEXT_READ_AT }) })),
    );
    await later.done;
    await waitFor(() => {
      expect(statusBars.mock.calls.length).toBeGreaterThan(statusRenders);
    });
    await settle({ queryClient, commits });
    expect(recommendationReads(api)).toHaveLength(3);
    expect(frames.mock.calls).toHaveLength(sectionRenders);
    expect(sectionHeadings()).toEqual(SECTION_TITLES);
  });

  it('shows a change in overlay.open_discrepancies in the Campaigns discrepancy group and the banner', async () => {
    const { api, queryClient, commits } = await renderProfiledPage(
      defaultHandlers({ 'GET /recommendations': json(200, pageWithoutDiscrepancyCards(0)) }),
    );
    await loadedPage();
    await settle({ queryClient, commits });
    const campaigns = sectionRegion('campaigns');
    expect(within(campaigns).queryByText(/detected since this run was computed$/u)).toBeNull();

    const poll = await startPoll(api, queryClient);
    poll.release(json(200, pageWithoutDiscrepancyCards(2)));
    await poll.done;

    expect(
      await within(campaigns).findByText('2 open discrepancies detected since this run was computed'),
    ).toBeTruthy();
    expect(screen.getByText('2 open discrepancies to resolve first')).toBeTruthy();
  });

  it("re-renders only the section whose item changed, and shows the item's new current state", async () => {
    const frames = vi.spyOn(sectionFrameModule, 'SectionFrame');
    const { api, queryClient, commits } = await renderProfiledPage(defaultHandlers());
    await loadedPage();
    await settle({ queryClient, commits });
    const itemId = `item-${String(FIXTURE_ITEM_IDS.b1)}`;
    expect(document.getElementById(itemId)).not.toBeNull();
    const budget = sectionRegion('budget');
    expect(within(budget).queryByText('1 hidden since this run')).toBeNull();
    const sectionRenders = frames.mock.calls.length;

    // Feedback given elsewhere since the last poll now hides the B1 card.
    const next = makeRecommendationsPage();
    findPageItem(next, FIXTURE_ITEM_IDS.b1).current.hidden_by = 'dismiss';
    const poll = await startPoll(api, queryClient);
    poll.release(json(200, next));
    await poll.done;

    expect(await within(budget).findByText('1 hidden since this run')).toBeTruthy();
    expect(document.getElementById(itemId)).toBeNull();
    await settle({ queryClient, commits });
    // Only the Budget section renders again; the other four keep their output.
    expect(frames.mock.calls).toHaveLength(sectionRenders + 1);
  });

  it('renders every section again for a poll whose overlay.stale_datasets gains a dataset, and shows it as stale', async () => {
    const staleDataset = 'google.metrics_daily';
    const staleMessage = `Stale data: ${staleDataset} (last success Oct 4, 2026, 5:30 AM UTC)`;
    const frames = vi.spyOn(sectionFrameModule, 'SectionFrame');
    const { api, queryClient, commits } = await renderProfiledPage(defaultHandlers());
    await loadedPage();
    await settle({ queryClient, commits });
    expect(within(sourceChip('Google')).queryByText('Stale')).toBeNull();
    expect(screen.queryByText(staleMessage)).toBeNull();
    const sectionRenders = frames.mock.calls.length;

    // The poll reads a Google dataset as stale now: the overlay's list gains it, and the API
    // adds the stale-data banner. Every section and run is unchanged, so structural sharing
    // keeps them as the objects on screen, and the overlay list alone differs.
    const poll = await startPoll(api, queryClient);
    poll.release(
      json(
        200,
        makeRecommendationsPage({
          overlay: makeOverlay({ stale_datasets: [staleDataset], computed_at: NEXT_READ_AT }),
          banners: bannersWith({
            code: 'stale_datasets',
            tone: 'warning',
            message: staleMessage,
            count: 1,
          }),
        }),
      ),
    );
    await poll.done;

    expect(await within(sourceChip('Google')).findByText('Stale')).toBeTruthy();
    expect(within(sourceChip('LinkedIn')).queryByText('Stale')).toBeNull();
    expect(within(sourceChip('HubSpot')).queryByText('Stale')).toBeNull();
    expect(screen.getByText(staleMessage)).toBeTruthy();
    await settle({ queryClient, commits });
    // Every section takes the changed overlay: each renders exactly once more.
    expect(frames.mock.calls).toHaveLength(sectionRenders + SECTION_ORDER.length);
  });

  it('renders no section again for overlay lists that arrive as new arrays equal in value, and every section once an entry changes', async () => {
    const laterReadAt = '2026-10-06T06:01:00Z';
    const movedDataset = 'linkedin.analytics_daily';
    const movedMessage =
      `Incomplete data: ${movedDataset} (covers through 2026-10-04, ` +
      'before the window end 2026-10-05)';
    const frames = vi.spyOn(sectionFrameModule, 'SectionFrame');
    const statusBars = vi.spyOn(statusBarModule, 'StatusBar');
    const { api, queryClient, commits } = await renderProfiledPage(
      defaultHandlers(),
      freshOverlayListsClient(),
    );
    await loadedPage();
    await settle({ queryClient, commits });
    const shown = shownPage(queryClient);
    const sectionRenders = frames.mock.calls.length;
    const statusRenders = statusBars.mock.calls.length;

    // A new read instant and the same lists, delivered as new arrays: the status bar takes the
    // new overlay, while no section renders again.
    const same = await startPoll(api, queryClient);
    same.release(
      json(200, makeRecommendationsPage({ overlay: makeOverlay({ computed_at: NEXT_READ_AT }) })),
    );
    await same.done;
    await waitFor(() => {
      expect(statusBars.mock.calls.length).toBeGreaterThan(statusRenders);
    });
    await settle({ queryClient, commits });
    const polled = shownPage(queryClient);
    // The sections keep their objects; each list is another array with the same entries.
    expect(polled.sections).toBe(shown.sections);
    expect(polled.run).toBe(shown.run);
    expect(polled.overlay.computed_at).toBe(NEXT_READ_AT);
    for (const list of ['stale_datasets', 'incomplete_datasets'] as const) {
      expect(polled.overlay[list]).not.toBe(shown.overlay[list]);
      expect(polled.overlay[list]).toEqual(shown.overlay[list]);
    }
    expect(frames.mock.calls).toHaveLength(sectionRenders);

    // The next poll moves the one incomplete dataset from Google to LinkedIn: a list of the same
    // length with another entry, which every section takes.
    const moved = await startPoll(api, queryClient);
    moved.release(
      json(
        200,
        makeRecommendationsPage({
          overlay: makeOverlay({ incomplete_datasets: [movedDataset], computed_at: laterReadAt }),
          banners: bannersWith({
            code: 'incomplete_datasets',
            tone: 'warning',
            message: movedMessage,
            count: 1,
          }),
        }),
      ),
    );
    await moved.done;

    expect(await within(sourceChip('LinkedIn')).findByText('Incomplete')).toBeTruthy();
    expect(within(sourceChip('Google')).queryByText('Incomplete')).toBeNull();
    expect(screen.getByText(movedMessage)).toBeTruthy();
    expect(screen.queryByText(FIXTURE_BANNERS[3]?.message ?? '')).toBeNull();
    await settle({ queryClient, commits });
    expect(shownPage(queryClient).sections).toBe(shown.sections);
    expect(frames.mock.calls).toHaveLength(sectionRenders + SECTION_ORDER.length);
  });
});
