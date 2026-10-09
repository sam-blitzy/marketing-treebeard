/**
 * Page tests for `ActionsPage`, the `/actions` screen (AAP 0.5.5 `/actions` layout; AAP 0.5.3
 * run selection; AAP 0.11.2 first start).
 *
 * The page renders inside the test router through a route component that mirrors
 * `src/router.tsx` (`/actions?run=<id>` → `<ActionsPage runId={run} />`), against the real query
 * hooks with only `fetch` stubbed. They hold the page to its TSDoc contract:
 * - **Run selection.** Without `?run=` the page reads `GET /recommendations` without `run_id`,
 *   the latest default run; with `?run=<id>` it reads that run, shows "Custom window {start} –
 *   {end}" with "Back to default", and the link returns to the default run.
 * - **The loaded page**, top to bottom: the single `<h1>` "Actions", the status bar, the custom
 *   banner (custom runs only), the global banners in their fixed order (only the discrepancy
 *   banner is an alert, and the short-window banner reads "Window of N days is below the
 *   {trusted_min_days}-day trusted minimum"), the section navigation, the empty polite region
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
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useSearch } from '@tanstack/react-router';
import type { ReactElement } from 'react';
import { describe, expect, it } from 'vitest';

import { queryKeys } from '../../api/queries';
import { SECTION_ORDER } from '../../api/types';
import type { RecommendationsPage, Run } from '../../api/types';
import {
  CUSTOM_RUN_ID,
  FIXTURE_BANNERS,
  FIXTURE_ITEM_IDS,
  FIXTURE_SECTION_TITLES,
  findPageItem,
  makeConfig,
  makeDiscrepancy,
  makeFeedback,
  makeRecommendationsPage,
  makeRun,
  makeSection,
  makeSources,
} from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { ActionsPage, SECTION_COMPONENTS } from './ActionsPage';

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

/**
 * The reads a loaded page makes besides `GET /recommendations`: the window picker's
 * configuration, default run and custom runs, the source chips' sources, and the Campaigns
 * section's discrepancies (White papers shares the configuration read).
 */
function pageReads(): MockHandlers {
  return {
    'GET /config': json(200, makeConfig()),
    'GET /runs/latest': json(200, makeRun()),
    'GET /runs': json(200, [customRun()]),
    'GET /sources': json(200, makeSources()),
    'GET /discrepancies': json(200, [makeDiscrepancy('status')]),
  };
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
        // Only GET /recommendations: nothing else is read before the first run exists.
        const { api, queryClient } = await renderPage({ 'GET /recommendations': response });

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
      expect(screen.getByRole('button', { name: /Retry/u }).hasAttribute('disabled')).toBe(true);
      expect(screen.getByText('Actions could not be loaded')).toBeTruthy();
      expect(screen.queryByText('Loading actions')).toBeNull();
      // A browser moves focus off a button that becomes disabled, to the page body. jsdom keeps
      // it on the disabled button and ignores blur() there, so the test moves it to the body.
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
      await renderPage({ 'GET /recommendations': apiError(500, 'internal_error', '   ') });

      await screen.findByText('Actions could not be loaded');
      expect(
        within(noticeRegion()).getByText('The recommendations could not be read. Try again.'),
      ).toBeTruthy();
    });
  });
});
