/**
 * Component tests for `DiscrepancyResolver.tsx`: the `/decisions` list of open and
 * awaiting-platform discrepancies, and `DiscrepancyResolution`, the controls of one discrepancy
 * (AAP 0.5.4.4, 0.5.5).
 *
 * They hold both components to their TSDoc contract against the real query hooks, with only the
 * network stubbed by `../../test/mockApi`:
 *
 * - the resolutions offered are exactly `Discrepancy.resolutions`, in the API's order, labelled
 *   per resolution: `status` and `budget` offer "Platform is correct" and "Decision is correct",
 *   `untracked` only "Adopt platform state" and "Record intended state", and `missing` "Decision
 *   is correct" and "Acknowledge removal";
 * - every resolution but "Record intended state" posts `POST /discrepancies/{id}/resolve` with
 *   `{ resolution }` alone and the CSRF header, shows its outcome in a polite status region and
 *   reports the API's response to `onResolved`; two presses before React renders again post
 *   once and open no dialog, and a settled request, either way, frees a later press;
 * - "Record intended state" opens a dialog holding `DecisionForm` with exactly the fields of the
 *   subject's decision shape, and posts the complete intended state; the dialog may close while
 *   its save runs, but until the save settles every resolution stays disabled and the dialog
 *   does not reopen, and a late outcome is still reported, shown and focused outside the dialog;
 * - 409 `subject_retired` and 409 `discrepancy_resolved` lock every control, whether a resolution
 *   or the intended-state form received them; 409 `platform_state_unknown`, 422
 *   `resolution_not_allowed` and 422 `intended_state_invalid` show the API's own message and
 *   leave the controls usable; a failure that is not an `ApiError` shows the generic text,
 *   never its own message; `disabled` locks every control;
 * - the list's loading, empty, failed and failed-refresh states, announced in one polite status
 *   region kept from the first render (never an alert), with Retry and the last-loaded instant;
 *   the first page of `DISCREPANCY_PAGE_SIZE` with its `X-Total-Count` summary, the line on
 *   discrepancies a fully read list cannot show because their subject could not be loaded (never
 *   the empty state while the total is positive), later `Link`
 *   cursor pages on "Show more discrepancies", polling, the closure announcement, and each side
 *   of a discrepancy rendered from its stored keys by kind and shape;
 * - a `#discrepancy-{id}` hash, on arrival or changed later: the pages it needs loaded one at a
 *   time, the card scrolled into view once, and the region's line when the complete list lacks
 *   it.
 *
 * `../../test/mockApi` is imported first so its `afterEach` check runs after Testing Library's
 * cleanup (Vitest skips later hooks once one throws).
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';
import type { QueryClient } from '@tanstack/react-query';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';

import { CURRENT_STATE_POLL_MS, DISCREPANCY_PAGE_SIZE, queryKeys } from '../../api/queries';
import type { CampaignStatus, Discrepancy, SubjectView } from '../../api/types';
import { Button } from '../../design-system';
import { MONEY_SEPARATOR, formatInstant } from '../../lib/format';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_DISCREPANCY_ID,
  makeDiscrepancy,
  makeRun,
} from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import {
  DiscrepancyResolution,
  DiscrepancyResolver,
  RESOLUTION_LABELS,
} from './DiscrepancyResolver';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The handler key of the resolver's first page: the default unresolved states. */
const LIST_KEY = 'GET /discrepancies?state=open&state=awaiting_platform';

/** The handler key of every resolution. */
const RESOLVE_KEY = 'POST /discrepancies/:id/resolve';

/** The label of the button that loads the resolver's next page. */
const SHOW_MORE = 'Show more discrepancies';

/** The `Link` header of a default-state page whose last read row is `after`. */
function nextLink(after: number): string {
  const limit = String(DISCREPANCY_PAGE_SIZE);
  const query = `state=open&state=awaiting_platform&limit=${limit}&after=${String(after)}`;
  return `</api/v1/discrepancies?${query}>; rel="next"`;
}

/** A response held until the test releases it, to observe a pending state. */
interface Deferred<T> {
  /** Settles when {@link Deferred.resolve} is called. */
  promise: Promise<T>;
  /** Releases the response. */
  resolve: (value: T) => void;
}

/** Creates a {@link Deferred}. */
function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

/** The resolver's handlers: the default run and one page of discrepancies. */
function listHandlers(discrepancies: readonly Discrepancy[]): MockHandlers {
  return {
    'GET /runs/latest': json(200, makeRun()),
    [LIST_KEY]: json(200, discrepancies),
  };
}

/** Every resolution the stub received. */
function resolveCalls(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path.endsWith('/resolve'));
}

/** Every `GET /discrepancies` page the stub received. */
function listCalls(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/discrepancies');
}

/** The resolver card of a subject, found by its headline. */
async function findCard(name: string): Promise<HTMLElement> {
  return screen.findByRole('article', { name });
}

/** The visible labels of the buttons in `scope`, in document order. */
function buttonLabels(scope: HTMLElement): string[] {
  return within(scope)
    .getAllByRole('button')
    .map((button) => button.textContent);
}

/** A button by its accessible name, typed as a button element. */
function buttonNamed(name: string, scope: HTMLElement = document.body): HTMLButtonElement {
  const button = within(scope).getByRole('button', { name });
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`"${name}" is not a button element`);
  }
  return button;
}

/** The polite outcome region of the resolution controls (a `div`, not a button's spinner). */
function outcomeRegion(scope: HTMLElement = document.body): HTMLElement {
  const region = within(scope)
    .getAllByRole('status')
    .find((element) => element.tagName === 'DIV');
  if (region === undefined) {
    throw new Error('No outcome status region is rendered');
  }
  return region;
}

/**
 * The resolver's own polite status region: the one `div` status element outside every card.
 * `hidden: true` also includes status elements excluded from the accessibility tree: those with
 * `visibility: hidden`, or with the `hidden` attribute, `aria-hidden="true"` or `display: none`
 * on themselves or an ancestor. The one-region check therefore counts every matching `div`,
 * whether or not it is exposed to assistive technology.
 */
function listRegion(): HTMLElement {
  const regions = screen
    .getAllByRole('status', { hidden: true })
    .filter((element) => element.tagName === 'DIV' && element.closest('article') === null);
  const [region] = regions;
  if (region === undefined || regions.length !== 1) {
    throw new Error(`Expected one list status region, found ${String(regions.length)}`);
  }
  return region;
}

/** Renders the controls of one discrepancy, with a spy for `onResolved`. */
async function renderResolution(discrepancy: Discrepancy, disabled?: boolean) {
  const onResolved = vi.fn<(updated: Discrepancy) => void>();
  const result = await renderWithProviders(
    <DiscrepancyResolution discrepancy={discrepancy} disabled={disabled} onResolved={onResolved} />,
    { path: '/decisions' },
  );
  return { ...result, onResolved };
}

/** A subject view with its currency replaced. */
function subjectWithCurrency(currency: string | null): SubjectView {
  return {
    type: 'campaign',
    id: FIXTURE_CAMPAIGN_IDS.brandSearch,
    name: 'Brand Search — US',
    source: 'google',
    currency,
  };
}

// ---------------------------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------------------------

describe('DiscrepancyResolver list', () => {
  it('shows a spinner while the first read runs, then one card per discrepancy', async () => {
    const page = deferred<MockResponse>();
    mockApi({ 'GET /runs/latest': json(200, makeRun()), [LIST_KEY]: () => page.promise });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    expect(screen.getByText('Loading discrepancies')).toBeTruthy();
    expect(screen.queryByRole('article')).toBeNull();
    // The spinner is a status element of its own, outside the list's region, never nested in it.
    expect(listRegion().contains(screen.getByText('Loading discrepancies'))).toBe(false);

    page.resolve(json(200, [makeDiscrepancy('status')]));

    const card = await findCard('Brand Search — US');
    expect(card.id).toBe(`discrepancy-${String(FIXTURE_DISCREPANCY_ID)}`);
    expect(screen.queryByText('Loading discrepancies')).toBeNull();
  });

  it('reads the open and awaiting-platform states one page at a time', async () => {
    const api = mockApi(listHandlers([makeDiscrepancy('status')]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    await findCard('Brand Search — US');

    const [first] = listCalls(api);
    expect(first?.search.getAll('state')).toEqual(['open', 'awaiting_platform']);
    expect(first?.search.get('limit')).toBe(String(DISCREPANCY_PAGE_SIZE));
    expect(listCalls(api)).toHaveLength(1);
    expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
  });

  it('keeps one polite status region, never an alert, from loading to data', async () => {
    const page = deferred<MockResponse>();
    let reads = 0;
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () => {
        reads += 1;
        if (reads === 1) {
          return apiError(500, 'internal_error', 'Database unavailable');
        }
        return page.promise;
      },
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const region = listRegion();
    expect(region.textContent).toBe('');

    await waitFor(() => {
      expect(within(region).getByText('Discrepancies could not be loaded')).toBeTruthy();
    });
    expect(within(region).getByText('Database unavailable')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();

    const retry = buttonNamed('Retry', region);
    retry.focus();
    fireEvent.click(retry);
    // The failure stays in place while the read runs again: no spinner, the button is loading.
    await waitFor(() => {
      expect(retry.getAttribute('aria-busy')).toBe('true');
    });
    expect(screen.queryByText('Loading discrepancies')).toBeNull();
    expect(within(region).getByText('Discrepancies could not be loaded')).toBeTruthy();

    page.resolve(
      json(200, [makeDiscrepancy('status')], { 'X-Total-Count': '1' }),
    );
    await findCard('Brand Search — US');
    expect(listRegion()).toBe(region);
    expect(region.textContent).toBe('Showing 1 of 1 unresolved discrepancy');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(listCalls(api)).toHaveLength(2);
  });

  it('returns focus to Retry when a retry the user started fails again', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: apiError(503, 'internal_error', 'Database unavailable'),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const region = listRegion();
    const retry = await within(region).findByRole('button', { name: 'Retry' });
    // The click is made with focus on the body, as when focus is lost while the read runs (a
    // loading Button itself keeps focus).
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(retry);

    await waitFor(() => {
      expect(listCalls(api)).toHaveLength(2);
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(within(region).getByRole('button', { name: 'Retry' }));
    });
    expect(within(region).getByText('Database unavailable')).toBeTruthy();
  });

  it('shows the first page with its total, then the next one on "Show more"', async () => {
    const oldest = makeDiscrepancy('status');
    const member = makeDiscrepancy('budget', {
      id: 52,
      shape: 'shared_member',
      detected_at: '2026-10-06T03:10:00Z',
    });
    const budget = makeDiscrepancy('untracked', {
      id: 60,
      shape: 'budget',
      state: 'awaiting_platform',
      detected_at: '2026-10-06T04:00:00Z',
    });
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [oldest, member], { Link: nextLink(52), 'X-Total-Count': '3' }),
      [`${LIST_KEY}&after=52`]: json(200, [budget], { 'X-Total-Count': '3' }),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    const region = listRegion();
    expect(region.textContent).toBe('Showing 2 of 3 unresolved discrepancies');
    expect(listCalls(api)).toHaveLength(1);

    expect(document.activeElement).toBe(document.body);
    fireEvent.click(buttonNamed(SHOW_MORE));

    await waitFor(() => {
      expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    });
    const headlines = within(list)
      .getAllByRole('heading', { level: 3 })
      .map((heading) => heading.textContent);
    expect(headlines).toEqual([
      'Brand Search — US',
      'Search Partners — US',
      'Search Shared Budget',
    ]);
    expect(listCalls(api).map((call) => call.search.get('after'))).toEqual([null, '52']);
    expect(region.textContent).toBe('Showing 3 of 3 unresolved discrepancies');
    expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
    // The last page removed the button, so focus moves to the first discrepancy it added.
    const added = within(list).getAllByRole('listitem')[2];
    await waitFor(() => {
      expect(document.activeElement).toBe(added);
    });
    expect(document.getElementById('discrepancy-52')).toBe(await findCard('Search Partners — US'));
    expect(document.getElementById('discrepancy-60')).toBe(await findCard('Search Shared Budget'));
  });

  it('keeps focus on "Show more" while further pages remain', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], { Link: nextLink(51) }),
      [`${LIST_KEY}&after=51`]: json(200, [makeDiscrepancy('budget', { id: 52 })], {
        Link: nextLink(52),
      }),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    // The click is made with focus on the body, as when focus is lost while the page loads.
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(buttonNamed(SHOW_MORE));

    await waitFor(() => {
      expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(buttonNamed(SHOW_MORE));
    });
    expect(listRegion().textContent).toBe('Showing 2 unresolved discrepancies');
  });

  it('keeps the loaded list and "Show more" when the next page fails', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], { Link: nextLink(51) }),
      [`${LIST_KEY}&after=51`]: apiError(503, 'internal_error', 'The database is restarting'),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    await findCard('Brand Search — US');
    fireEvent.click(buttonNamed(SHOW_MORE));

    const region = listRegion();
    await waitFor(() => {
      expect(within(region).getByText('More discrepancies could not be loaded')).toBeTruthy();
    });
    expect(within(region).getByText('The database is restarting')).toBeTruthy();
    expect(within(region).queryByText('Discrepancies could not be refreshed')).toBeNull();
    expect(screen.getByRole('article', { name: 'Brand Search — US' })).toBeTruthy();
    expect(buttonNamed(SHOW_MORE).disabled).toBe(false);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows the state, the comparison and the detection instant in the run time zone', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun({ display_timezone: 'America/New_York' })),
      [LIST_KEY]: json(200, [
        makeDiscrepancy('status'),
        makeDiscrepancy('missing', {
          id: 53,
          shape: 'budget',
          state: 'awaiting_platform',
          resolution: 'decision_correct',
        }),
      ]),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(
      within(list)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Brand Search — US', 'Search Shared Budget']);

    const status = await findCard('Brand Search — US');
    expect(within(status).getByText('Google · Campaign · Status mismatch')).toBeTruthy();
    expect(within(status).getByText('Open')).toBeTruthy();
    expect(within(status).getByText('Platform shows Paused, last decided Active')).toBeTruthy();
    await waitFor(() => {
      expect(
        within(status).getByText('Detected Oct 5, 2026, 11:00 PM America/New_York'),
      ).toBeTruthy();
    });

    const missing = await findCard('Search Shared Budget');
    expect(
      within(missing).getByText('Google · Shared budget · Missing from platform'),
    ).toBeTruthy();
    expect(within(missing).getByText('Awaiting platform')).toBeTruthy();
    expect(
      within(missing).getByText(
        'Platform shows not returned by the platform, last decided daily budget USD 500.00',
      ),
    ).toBeTruthy();
  });

  it('formats detection instants in UTC, so labelled, while no default run exists', async () => {
    mockApi({
      'GET /runs/latest': apiError(404, 'not_found', 'No analysis run yet'),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')]),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const card = await findCard('Brand Search — US');
    expect(within(card).getByText('Detected Oct 6, 2026, 3:00 AM UTC')).toBeTruthy();
  });

  it('names a subject without a name by its type and id, and omits an unknown source', async () => {
    mockApi(
      listHandlers([
        makeDiscrepancy('untracked', {
          subject: { type: 'campaign', id: 309, name: '  ', source: null, currency: 'USD' },
        }),
        makeDiscrepancy('budget', {
          id: 54,
          shape: 'budget',
          subject: {
            type: 'budget',
            id: FIXTURE_BUDGET_IDS.display,
            name: null,
            source: 'google',
            currency: 'USD',
          },
        }),
      ]),
    );

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const campaign = await findCard('Campaign 309');
    expect(within(campaign).getByText('Campaign · Untracked entity')).toBeTruthy();
    const budget = await findCard(`Shared budget ${String(FIXTURE_BUDGET_IDS.display)}`);
    expect(within(budget).getByText('Google · Shared budget · Budget mismatch')).toBeTruthy();
  });

  it('says so in its status region when there is no open discrepancy', async () => {
    mockApi(listHandlers([]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const region = listRegion();
    await waitFor(() => {
      expect(within(region).getByText('No open discrepancies')).toBeTruthy();
    });
    expect(within(region).getByText('Platform state matches every last decision.')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
  });

  it.each([
    ['an X-Total-Count of 0', { 'X-Total-Count': '0' }],
    ['no X-Total-Count', {}],
  ])('shows the empty state for an empty list with %s', async (_description, headers) => {
    mockApi({ 'GET /runs/latest': json(200, makeRun()), [LIST_KEY]: json(200, [], headers) });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const region = listRegion();
    await waitFor(() => {
      expect(region.textContent).toBe(
        'No open discrepanciesPlatform state matches every last decision.',
      );
    });
    expect(screen.queryByRole('list')).toBeNull();
  });

  it.each([
    [
      '1',
      '1 unresolved discrepancy cannot be shown' +
        ' because its campaign or shared budget could not be loaded.',
    ],
    [
      '2',
      '2 unresolved discrepancies cannot be shown' +
        ' because their campaigns or shared budgets could not be loaded.',
    ],
  ])(
    'never reports an empty list with an X-Total-Count of %s as matching every decision',
    async (total, line) => {
      mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LIST_KEY]: json(200, [], { 'X-Total-Count': total }),
      });

      await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

      // The API counts discrepancies whose subject cannot be loaded but leaves them out of the
      // list, so the region keeps the total and says why nothing is listed.
      const region = listRegion();
      await waitFor(() => {
        expect(region.textContent).toBe(line);
      });
      expect(screen.queryByText('No open discrepancies')).toBeNull();
      expect(screen.queryByText('Platform state matches every last decision.')).toBeNull();
      expect(screen.queryByRole('list')).toBeNull();
      expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it.each([
    [
      '2 of 3',
      [makeDiscrepancy('status'), makeDiscrepancy('budget', { id: 52 })],
      'Showing 2 of 3 unresolved discrepancies',
      '1 cannot be shown because its campaign or shared budget could not be loaded.',
    ],
    [
      '1 of 3',
      [makeDiscrepancy('status')],
      'Showing 1 of 3 unresolved discrepancies',
      '2 cannot be shown because their campaigns or shared budgets could not be loaded.',
    ],
  ])(
    'explains beneath the summary what a complete list of %s cannot show',
    async (_description, discrepancies, summary, line) => {
      mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LIST_KEY]: json(200, discrepancies, { 'X-Total-Count': '3' }),
      });

      await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

      const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
      expect(within(list).getAllByRole('listitem')).toHaveLength(discrepancies.length);
      const region = listRegion();
      expect(within(region).getByText(summary)).toBeTruthy();
      expect(within(region).getByText(line)).toBeTruthy();
      expect(region.textContent).toBe(`${summary}${line}`);
      expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('says nothing of unavailable discrepancies until the last page is read', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status'), makeDiscrepancy('budget', { id: 52 })], {
        Link: nextLink(52),
        'X-Total-Count': '4',
      }),
      [`${LIST_KEY}&after=52`]: json(200, [makeDiscrepancy('untracked', { id: 60 })], {
        'X-Total-Count': '4',
      }),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    // The next page may still hold the rest, so only the summary is shown.
    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    const region = listRegion();
    expect(region.textContent).toBe('Showing 2 of 4 unresolved discrepancies');
    expect(within(region).queryByText(/cannot be shown/)).toBeNull();

    fireEvent.click(buttonNamed(SHOW_MORE));

    await waitFor(() => {
      expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    });
    expect(region.textContent).toBe(
      'Showing 3 of 4 unresolved discrepancies' +
        '1 cannot be shown because its campaign or shared budget could not be loaded.',
    );
    expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();
  });

  it('shows the API message and Retry when the first read fails', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const region = listRegion();
    expect(await within(region).findByText('Discrepancies could not be loaded')).toBeTruthy();
    expect(within(region).getByText('Database unavailable')).toBeTruthy();
    expect(buttonNamed('Retry', region)).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('keeps the list shown as last loaded, with Retry, when a refresh fails', async () => {
    const api = mockApi(listHandlers([makeDiscrepancy('status')]));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    await findCard('Brand Search — US');
    const loadedAt = queryClient.getQueryState(
      queryKeys.discrepancyPages(['open', 'awaiting_platform']),
    )?.dataUpdatedAt;

    api.use({ [LIST_KEY]: apiError(503, 'unavailable', 'The database is restarting') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    const region = listRegion();
    expect(await within(region).findByText('Discrepancies could not be refreshed')).toBeTruthy();
    expect(within(region).getByText('The database is restarting')).toBeTruthy();
    expect(loadedAt).toBeDefined();
    const loadedText = formatInstant(new Date(loadedAt ?? 0).toISOString(), 'UTC');
    expect(within(region).getByText(`Showing the list as last loaded ${loadedText}.`)).toBeTruthy();
    expect(screen.getByRole('article', { name: 'Brand Search — US' })).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();

    api.use(listHandlers([makeDiscrepancy('status'), makeDiscrepancy('budget', { id: 52 })]));
    fireEvent.click(buttonNamed('Retry', region));
    await waitFor(() => {
      expect(within(region).queryByText('Discrepancies could not be refreshed')).toBeNull();
    });
    expect(screen.getAllByRole('article')).toHaveLength(2);
  });

  it('never presents a cached empty list as current when its refresh fails', async () => {
    const api = mockApi(listHandlers([]));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const region = listRegion();
    await waitFor(() => {
      expect(within(region).getByText('No open discrepancies')).toBeTruthy();
    });

    api.use({ [LIST_KEY]: apiError(502, 'http_502', 'Bad gateway') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    expect(await within(region).findByText('Discrepancies could not be refreshed')).toBeTruthy();
    expect(within(region).getByText('Bad gateway')).toBeTruthy();
    expect(
      within(region).getByText(/^No discrepancies were open when the list was last loaded .+\.$/),
    ).toBeTruthy();
    expect(within(region).queryByText('No open discrepancies')).toBeNull();
    expect(within(region).queryByText('Platform state matches every last decision.')).toBeNull();
    expect(buttonNamed('Retry', region)).toBeTruthy();
  });

  it('never says none were open when an empty list with a total fails to refresh', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [], { 'X-Total-Count': '1' }),
    });
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const region = listRegion();
    const line =
      '1 unresolved discrepancy cannot be shown' +
      ' because its campaign or shared budget could not be loaded.';
    await waitFor(() => {
      expect(region.textContent).toBe(line);
    });

    api.use({ [LIST_KEY]: apiError(502, 'http_502', 'Bad gateway') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    expect(await within(region).findByText('Discrepancies could not be refreshed')).toBeTruthy();
    expect(within(region).getByText('Bad gateway')).toBeTruthy();
    expect(within(region).getByText(/^Showing the list as last loaded .+\.$/)).toBeTruthy();
    expect(within(region).getByText(line)).toBeTruthy();
    expect(within(region).queryByText(/^No discrepancies were open/)).toBeNull();
    expect(within(region).queryByText('No open discrepancies')).toBeNull();
    expect(buttonNamed('Retry', region)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('drops a discrepancy the resolution closed and announces the closure', async () => {
    let resolved = false;
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () => json(200, resolved ? [] : [makeDiscrepancy('status')]),
      [RESOLVE_KEY]: () => {
        resolved = true;
        return json(
          200,
          makeDiscrepancy('status', { state: 'resolved', resolution: 'platform_correct' }),
        );
      },
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const card = await findCard('Brand Search — US');
    const region = listRegion();
    fireEvent.click(buttonNamed('Platform is correct', card));

    expect(await within(region).findByText('No open discrepancies')).toBeTruthy();
    expect(await within(region).findByText('Brand Search — US resolved')).toBeTruthy();
    expect(listRegion()).toBe(region);
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'platform_correct' },
    ]);
    expect(listCalls(api)).toHaveLength(2);
  });

  it('keeps an awaiting-platform discrepancy on screen with its new state', async () => {
    let awaiting = false;
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () =>
        json(200, [makeDiscrepancy('budget', awaiting ? { state: 'awaiting_platform' } : {})]),
      [RESOLVE_KEY]: () => {
        awaiting = true;
        return json(
          200,
          makeDiscrepancy('budget', {
            state: 'awaiting_platform',
            resolution: 'decision_correct',
          }),
        );
      },
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const card = await findCard('Brand Search — US');
    fireEvent.click(buttonNamed('Decision is correct', card));

    await waitFor(() => {
      expect(outcomeRegion(card).textContent).toBe(
        'Waiting for the platform to match the decision',
      );
    });
    expect(within(card).getByText('Awaiting platform')).toBeTruthy();
    expect(within(listRegion()).queryByText(/resolved$/)).toBeNull();
  });

  it('refreshes itself while shown, picking up a discrepancy the worker raised', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let raised = false;
      mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LIST_KEY]: () =>
          json(
            200,
            raised
              ? [makeDiscrepancy('status'), makeDiscrepancy('budget', { id: 52 })]
              : [makeDiscrepancy('status')],
            { 'X-Total-Count': raised ? '2' : '1' },
          ),
      });

      await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
      await findCard('Brand Search — US');
      expect(screen.getAllByRole('article')).toHaveLength(1);

      raised = true;
      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      await waitFor(() => {
        expect(screen.getAllByRole('article')).toHaveLength(2);
      });
      expect(listRegion().textContent).toBe('Showing 2 of 2 unresolved discrepancies');
    } finally {
      vi.useRealTimers();
    }
  });
});

// ---------------------------------------------------------------------------------------------
// Focus after a closing resolution (WCAG 2.2 SC 2.4.3)
// ---------------------------------------------------------------------------------------------

/**
 * Three discrepancies in the API's order, each offering "Platform is correct": "Brand Search —
 * US", "Search Partners — US" and "Search Shared Budget".
 */
function threeCards(): Discrepancy[] {
  return [
    makeDiscrepancy('status'),
    makeDiscrepancy('budget', {
      id: 52,
      shape: 'shared_member',
      detected_at: '2026-10-06T03:10:00Z',
    }),
    makeDiscrepancy('budget', { id: 53, shape: 'budget', detected_at: '2026-10-06T04:00:00Z' }),
  ];
}

/** `discrepancy` as the API answers a resolution that closed it. */
function closedBy(discrepancy: Discrepancy): Discrepancy {
  return { ...discrepancy, state: 'resolved', resolution: 'platform_correct' };
}

/**
 * The resolver's handlers for `cards`, where every resolution closes the discrepancy it names:
 * the API answers it `resolved`, and every later read leaves it out.
 */
function closingHandlers(cards: readonly Discrepancy[]): MockHandlers {
  const closed = new Set<string>();
  return {
    'GET /runs/latest': json(200, makeRun()),
    [LIST_KEY]: () => json(200, cards.filter((card) => !closed.has(String(card.id)))),
    [RESOLVE_KEY]: (call) => {
      const card = cards.find((candidate) => String(candidate.id) === call.params.id);
      if (card === undefined) {
        return apiError(404, 'not_found', 'Discrepancy not found');
      }
      closed.add(String(card.id));
      return json(200, closedBy(card));
    },
  };
}

/** The list item that holds a subject's resolver card. */
async function findCardItem(name: string): Promise<HTMLElement> {
  const item = (await findCard(name)).closest('li');
  if (item === null) {
    throw new Error(`The card of "${name}" is not in a list item`);
  }
  return item;
}

describe('DiscrepancyResolver focus after a closing resolution', () => {
  it('moves focus to the next card when a resolution closes the first one', async () => {
    mockApi(closingHandlers(threeCards()));
    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const closing = await findCard('Brand Search — US');
    const next = await findCardItem('Search Partners — US');
    const last = await findCardItem('Search Shared Budget');
    // As in a browser, where the loading (disabled) button drops focus to the body.
    expect(document.activeElement).toBe(document.body);

    fireEvent.click(buttonNamed('Platform is correct', closing));

    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
    expect(closing.isConnected).toBe(false);
    expect(next.getAttribute('tabindex')).toBe('-1');
    expect(last.hasAttribute('tabindex')).toBe(false);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
    expect(within(listRegion()).getByText('Brand Search — US resolved')).toBeTruthy();
  });

  it('moves focus to the previous card when a resolution closes the last one', async () => {
    mockApi(closingHandlers(threeCards()));
    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const first = await findCardItem('Brand Search — US');
    const previous = await findCardItem('Search Partners — US');
    const closing = await findCard('Search Shared Budget');

    fireEvent.click(buttonNamed('Platform is correct', closing));

    await waitFor(() => {
      expect(document.activeElement).toBe(previous);
    });
    expect(closing.isConnected).toBe(false);
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(first.hasAttribute('tabindex')).toBe(false);
  });

  it('moves focus to the status region when a resolution closes the only card', async () => {
    mockApi(closingHandlers([makeDiscrepancy('status')]));
    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const closing = await findCard('Brand Search — US');
    const region = listRegion();
    expect(region.hasAttribute('tabindex')).toBe(false);

    fireEvent.click(buttonNamed('Platform is correct', closing));

    await waitFor(() => {
      expect(document.activeElement).toBe(region);
    });
    expect(region.getAttribute('tabindex')).toBe('-1');
    expect(listRegion()).toBe(region);
    expect(within(region).getByText('No open discrepancies')).toBeTruthy();
    expect(within(region).getByText('Brand Search — US resolved')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps the focused status region focusable when a later read lists a new card', async () => {
    const [first, second] = threeCards();
    if (first === undefined || second === undefined) {
      throw new Error('threeCards() returned fewer than two discrepancies');
    }
    let read = json(200, [first]);
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () => read,
      [RESOLVE_KEY]: () => {
        read = json(200, []);
        return json(200, closedBy(first));
      },
    });
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const closing = await findCard('Brand Search — US');
    const region = listRegion();
    fireEvent.click(buttonNamed('Platform is correct', closing));
    await waitFor(() => {
      expect(document.activeElement).toBe(region);
    });

    // The worker raises another discrepancy; the next poll lists it.
    read = json(200, [second]);
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    const raised = await findCardItem('Search Partners — US');
    await settle(queryClient);
    expect(region.getAttribute('tabindex')).toBe('-1');
    expect(document.activeElement).toBe(region);
    expect(raised.contains(document.activeElement)).toBe(false);
  });

  it('moves focus to a card listed since when the closed card had no neighbour', async () => {
    const [first, second] = threeCards();
    if (first === undefined || second === undefined) {
      throw new Error('threeCards() returned fewer than two discrepancies');
    }
    let closed = false;
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      // The worker raised another discrepancy while the only listed one was being resolved.
      [LIST_KEY]: () => json(200, closed ? [second] : [first]),
      [RESOLVE_KEY]: () => {
        closed = true;
        return json(200, closedBy(first));
      },
    });
    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    const closing = await findCard('Brand Search — US');
    const region = listRegion();

    fireEvent.click(buttonNamed('Platform is correct', closing));

    const raised = await findCardItem('Search Partners — US');
    await waitFor(() => {
      expect(document.activeElement).toBe(raised);
    });
    expect(closing.isConnected).toBe(false);
    expect(raised.getAttribute('tabindex')).toBe('-1');
    expect(region.hasAttribute('tabindex')).toBe(false);
  });

  it('leaves focus the user moved while the resolution ran where it is', async () => {
    const [first, second] = threeCards();
    if (first === undefined || second === undefined) {
      throw new Error('threeCards() returned fewer than two discrepancies');
    }
    const response = deferred<MockResponse>();
    let closed = false;
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () => json(200, closed ? [second] : [first, second]),
      [RESOLVE_KEY]: () => response.promise,
    });
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const closing = await findCard('Brand Search — US');
    const other = await findCard('Search Partners — US');

    fireEvent.click(buttonNamed('Platform is correct', closing));
    // The loading button stays focusable (aria-disabled) while its request runs.
    await waitFor(() => {
      expect(buttonNamed('Platform is correct', closing).getAttribute('aria-busy')).toBe('true');
    });
    const moved = buttonNamed('Decision is correct', other);
    moved.focus();
    closed = true;
    response.resolve(json(200, closedBy(first)));

    await waitFor(() => {
      expect(closing.isConnected).toBe(false);
    });
    await settle(queryClient);
    expect(document.activeElement).toBe(moved);
  });

  it('keeps focus on the clicked button when the discrepancy waits for the platform', async () => {
    const [, second] = threeCards();
    if (second === undefined) {
      throw new Error('threeCards() returned fewer than two discrepancies');
    }
    let awaiting = false;
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () =>
        json(200, [
          makeDiscrepancy('status', awaiting ? { state: 'awaiting_platform' } : {}),
          second,
        ]),
      [RESOLVE_KEY]: () => {
        awaiting = true;
        return json(
          200,
          makeDiscrepancy('status', {
            state: 'awaiting_platform',
            resolution: 'decision_correct',
          }),
        );
      },
    });
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const card = await findCard('Brand Search — US');

    fireEvent.click(buttonNamed('Decision is correct', card));

    await waitFor(() => {
      expect(document.activeElement).toBe(buttonNamed('Decision is correct', card));
    });
    await settle(queryClient);
    expect(document.activeElement).toBe(buttonNamed('Decision is correct', card));
    expect(within(card).getByText('Awaiting platform')).toBeTruthy();
    const focusable = screen
      .getAllByRole('listitem')
      .filter((item) => item.hasAttribute('tabindex'));
    expect(focusable).toEqual([]);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
    expect(within(listRegion()).queryByText(/resolved$/)).toBeNull();
  });

  it('focuses the closed card a failed refresh keeps, then the next card once it leaves', async () => {
    const [first, second] = threeCards();
    if (first === undefined || second === undefined) {
      throw new Error('threeCards() returned fewer than two discrepancies');
    }
    let read = json(200, [first, second]);
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: () => read,
      [RESOLVE_KEY]: () => {
        read = apiError(503, 'unavailable', 'The database is restarting');
        return json(200, closedBy(first));
      },
    });
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const closing = await findCardItem('Brand Search — US');
    const next = await findCardItem('Search Partners — US');

    fireEvent.click(buttonNamed('Platform is correct', closing));

    const region = listRegion();
    expect(await within(region).findByText('Discrepancies could not be refreshed')).toBeTruthy();
    await waitFor(() => {
      expect(document.activeElement).toBe(closing);
    });
    expect(closing.getAttribute('tabindex')).toBe('-1');
    expect(outcomeRegion(closing).textContent).toBe('Resolved');
    expect(next.hasAttribute('tabindex')).toBe(false);

    read = json(200, [second]);
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
    expect(closing.isConnected).toBe(false);
    expect(within(region).queryByText('Discrepancies could not be refreshed')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// The linked discrepancy (`/decisions#discrepancy-{id}`)
// ---------------------------------------------------------------------------------------------

/**
 * The most rounds {@link settle} runs. A round past the first follows one in which the query
 * cache emitted an event or after which a read was in flight. The bound caps the number of
 * rounds, not the time they take: a resolver that keeps changing its queries, or starts another
 * read in every round, fails inside {@link settle} after the last round instead of looping. Each
 * round's Testing Library `waitFor` can still fail on its own timeout, and rounds that are slow
 * to settle can together outlast the test's timeout.
 */
const SETTLE_ROUNDS = 10;

/**
 * Resolves once the resolver is quiescent, so a test can assert that nothing more happens: no
 * read of `queryClient` is in flight, its observer notifications have been delivered, and the
 * renders and effects they cause, where a further `fetchNextPage` or `scrollIntoView` would
 * start, have run. It waits on these conditions, never on a delay.
 *
 * TanStack Query delivers each observer notification from a global `setTimeout(callback, 0)`,
 * queued in the same synchronous batch that emits a query-cache event. A finished read queues
 * its notifications as it sets its `fetchStatus` to `idle`, so once `queryClient.isFetching()`
 * reads 0 the notifications of every finished read are queued. Each round waits for that, then
 * flushes inside `act`, which commits the renders the notifications cause and runs their
 * effects when it exits:
 *
 * - With real timers, the flush awaits a zero-delay timer created after the queued ones. Timers
 *   of equal delay run in creation order, so the notifications run first.
 * - Under `vi.useFakeTimers()` that `setTimeout` is fake: a timer runs when the fake clock
 *   reaches its due time, and the flush moves the clock itself rather than waiting for real time
 *   to move it. Fake-timers makes a zero-delay timer due at the clock's time when it is created,
 *   plus 1 ms when it is created during a tick, that is while its `duringTick` flag is set.
 *   `vi.advanceTimersByTimeAsync` and the `shouldAdvanceTime` auto-advance (a tick every 20 ms
 *   of real time) both set that flag while they run timers. Every queued notification is
 *   therefore due at most 1 ms after the clock. A notification queued as the 30 s poll's read
 *   settles, inside the tick that fired the poll, can be due 1 ms after the clock, and
 *   `vi.advanceTimersByTimeAsync(0)` would leave it to the auto-advance. The flush is
 *   `vi.advanceTimersByTimeAsync(1)`: it runs every notification queued before the round, and
 *   moves the clock 1 ms per round, far from the next {@link CURRENT_STATE_POLL_MS} poll.
 *
 * A notification or effect run by a round can change a query again or start a read, and every
 * notification such a change queues comes with a query-cache event, so another round follows a
 * round in which the query cache emitted an event or after which a read is in flight. The first
 * round with neither ends the wait.
 *
 * @param queryClient - The client of the render, as {@link renderWithProviders} resolves it.
 * @throws Error when the query cache still changed, or a read was still in flight, in the last
 *   of {@link SETTLE_ROUNDS} rounds, or when Testing Library's `waitFor` times out before it sees
 *   no read in flight, as when reads follow one another back to back.
 */
async function settle(queryClient: QueryClient): Promise<void> {
  let cacheEvents = 0;
  const unsubscribe = queryClient.getQueryCache().subscribe(() => {
    cacheEvents += 1;
  });
  try {
    for (let round = 0; round < SETTLE_ROUNDS; round += 1) {
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });
      const cacheEventsBefore = cacheEvents;
      await act(async () => {
        if (vi.isFakeTimers()) {
          await vi.advanceTimersByTimeAsync(1);
          return;
        }
        await new Promise<void>((resolve) => {
          setTimeout(resolve, 0);
        });
      });
      if (cacheEvents === cacheEventsBefore && queryClient.isFetching() === 0) {
        return;
      }
    }
    throw new Error(
      `settle: the query cache still changed or a read was in flight after ${String(SETTLE_ROUNDS)} rounds, so the resolver kept reading`,
    );
  } finally {
    unsubscribe();
  }
}

/** The `after` cursor of every `GET /discrepancies` page the stub received, in order. */
function pageCursors(api: ApiMock): (string | null)[] {
  return listCalls(api).map((call) => call.search.get('after'));
}

/** The resolver card of a discrepancy, found by the element id links name as their hash. */
function cardById(id: number): HTMLElement {
  const card = document.getElementById(`discrepancy-${String(id)}`);
  if (card === null) {
    throw new Error(`No card for discrepancy ${String(id)} is rendered`);
  }
  return card;
}

describe('DiscrepancyResolver linked discrepancy', () => {
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
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
  });

  it('scrolls a linked card on the first page into view once loaded, and only once', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const page = deferred<MockResponse>();
      let reads = 0;
      const discrepancies = [makeDiscrepancy('status'), makeDiscrepancy('budget', { id: 52 })];
      const api = mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LIST_KEY]: () => {
          reads += 1;
          return reads === 1
            ? page.promise
            : json(200, discrepancies, { 'X-Total-Count': '2' });
        },
      });

      const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
        path: '/decisions#discrepancy-52',
      });
      const region = listRegion();
      expect(screen.getByText('Loading discrepancies')).toBeTruthy();
      expect(scrollIntoView).not.toHaveBeenCalled();

      page.resolve(json(200, discrepancies, { 'X-Total-Count': '2' }));
      await waitFor(() => {
        expect(scrollIntoView).toHaveBeenCalledTimes(1);
      });
      expect(scrollIntoView.mock.contexts[0]).toBe(cardById(52));
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
      expect(pageCursors(api)).toEqual([null]);
      expect(region.textContent).toBe('Showing 2 of 2 unresolved discrepancies');
      // The card is scrolled to, not focused.
      expect(document.activeElement).toBe(document.body);

      // The 30 s poll re-reads the list, and the card is not scrolled to again.
      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      await waitFor(() => {
        expect(listCalls(api)).toHaveLength(2);
      });
      await settle(queryClient);
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(listRegion()).toBe(region);
    } finally {
      vi.useRealTimers();
    }
  });

  it('loads pages 2 and 3 to reach a linked card on page 3, then scrolls to it', async () => {
    const second = deferred<MockResponse>();
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], {
        Link: nextLink(51),
        'X-Total-Count': '4',
      }),
      [`${LIST_KEY}&after=51`]: () => second.promise,
      [`${LIST_KEY}&after=52`]: json(200, [makeDiscrepancy('untracked', { id: 60 })], {
        Link: nextLink(60),
        'X-Total-Count': '4',
      }),
      [`${LIST_KEY}&after=60`]: json(200, [makeDiscrepancy('missing', { id: 61 })], {
        'X-Total-Count': '4',
      }),
    });

    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions#discrepancy-60',
    });

    // Page 1 is shown and page 2 is loading: "Show more" shows that load.
    await waitFor(() => {
      expect(pageCursors(api)).toEqual([null, '51']);
    });
    expect(cardById(51)).toBeTruthy();
    await waitFor(() => {
      expect(buttonNamed(SHOW_MORE).getAttribute('aria-busy')).toBe('true');
    });
    expect(scrollIntoView).not.toHaveBeenCalled();

    second.resolve(
      json(200, [makeDiscrepancy('budget', { id: 52 })], {
        Link: nextLink(52),
        'X-Total-Count': '4',
      }),
    );
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });
    expect(scrollIntoView.mock.contexts[0]).toBe(cardById(60));
    expect(pageCursors(api)).toEqual([null, '51', '52']);
    for (const call of listCalls(api)) {
      expect(call.search.get('limit')).toBe(String(DISCREPANCY_PAGE_SIZE));
    }
    expect(listRegion().textContent).toBe('Showing 3 of 4 unresolved discrepancies');

    // Page 4 is left for "Show more", and no focus was moved.
    await settle(queryClient);
    expect(pageCursors(api)).toEqual([null, '51', '52']);
    expect(buttonNamed(SHOW_MORE).disabled).toBe(false);
    expect(document.activeElement).toBe(document.body);
    expect(document.getElementById('discrepancy-61')).toBeNull();
  });

  it('follows a hash that changes after mount, loading only what the target needs', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], { Link: nextLink(51) }),
      [`${LIST_KEY}&after=51`]: json(200, [makeDiscrepancy('budget', { id: 52 })], {
        Link: nextLink(52),
      }),
      [`${LIST_KEY}&after=52`]: json(200, [makeDiscrepancy('untracked', { id: 60 })]),
    });

    const { queryClient, router } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    await findCard('Brand Search — US');
    await settle(queryClient);
    expect(pageCursors(api)).toEqual([null]);
    expect(scrollIntoView).not.toHaveBeenCalled();

    // A card already loaded is scrolled to without a request.
    await act(() => router.navigate({ to: '/decisions', hash: 'discrepancy-51' }));
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    });
    expect(scrollIntoView.mock.contexts[0]).toBe(cardById(51));
    expect(pageCursors(api)).toEqual([null]);

    // A card on a later page is loaded first, then scrolled to.
    await act(() => router.navigate({ to: '/decisions', hash: 'discrepancy-52' }));
    await waitFor(() => {
      expect(scrollIntoView).toHaveBeenCalledTimes(2);
    });
    expect(scrollIntoView.mock.contexts[1]).toBe(cardById(52));
    expect(pageCursors(api)).toEqual([null, '51']);

    await settle(queryClient);
    expect(pageCursors(api)).toEqual([null, '51']);
    expect(scrollIntoView).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(document.body);
  });

  it('says a linked discrepancy the complete list lacks is no longer unresolved', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], {
        Link: nextLink(51),
        'X-Total-Count': '2',
      }),
      [`${LIST_KEY}&after=51`]: json(200, [makeDiscrepancy('budget', { id: 52 })], {
        'X-Total-Count': '2',
      }),
    });

    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions#discrepancy-77',
    });
    const region = listRegion();

    await waitFor(() => {
      expect(within(region).getByText('Discrepancy 77 is no longer unresolved.')).toBeTruthy();
    });
    expect(within(region).getByText('Showing 2 of 2 unresolved discrepancies')).toBeTruthy();
    expect(pageCursors(api)).toEqual([null, '51']);
    expect(listRegion()).toBe(region);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: SHOW_MORE })).toBeNull();

    // The list has ended: nothing more is read, and nothing is scrolled.
    await settle(queryClient);
    expect(pageCursors(api)).toEqual([null, '51']);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });

  it('names an unloadable subject as a possible reason when the total is larger', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], { 'X-Total-Count': '2' }),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions#discrepancy-77' });

    const region = listRegion();
    await waitFor(() => {
      expect(
        within(region).getByText(
          'Discrepancy 77 is not listed: it is no longer unresolved, or its campaign or shared ' +
            'budget could not be loaded.',
        ),
      ).toBeTruthy();
    });
    expect(within(region).getByText('Showing 1 of 2 unresolved discrepancies')).toBeTruthy();
  });

  it('reports a failed page load toward the linked card as "Show more" would, once', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: json(200, [makeDiscrepancy('status')], { Link: nextLink(51) }),
      [`${LIST_KEY}&after=51`]: apiError(503, 'internal_error', 'The database is restarting'),
    });

    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions#discrepancy-60',
    });

    const region = listRegion();
    await waitFor(() => {
      expect(within(region).getByText('More discrepancies could not be loaded')).toBeTruthy();
    });
    expect(within(region).getByText('The database is restarting')).toBeTruthy();
    expect(within(region).queryByText('Discrepancies could not be refreshed')).toBeNull();
    expect(within(region).queryByText(/no longer unresolved/u)).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();

    // The failed load is not repeated on its own; "Show more" stays to try again.
    await settle(queryClient);
    expect(pageCursors(api)).toEqual([null, '51']);
    expect(buttonNamed(SHOW_MORE).disabled).toBe(false);
    expect(scrollIntoView).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
// Values of each side
// ---------------------------------------------------------------------------------------------

/** One discrepancy and the comparison line its card must show. */
type ComparisonCase = readonly [description: string, discrepancy: Discrepancy, line: string];

const COMPARISONS: readonly ComparisonCase[] = [
  ['a status mismatch', makeDiscrepancy('status'), 'Platform shows Paused, last decided Active'],
  [
    'an own budget amount',
    makeDiscrepancy('budget'),
    'Platform shows daily budget USD 250.00, last decided daily budget USD 200.00',
  ],
  [
    "a shared member's budget assignment",
    makeDiscrepancy('budget', { shape: 'shared_member' }),
    'Platform shows shared budget 31000401, last decided shared budget 31000402',
  ],
  [
    "a shared budget's amount",
    makeDiscrepancy('budget', { shape: 'budget' }),
    'Platform shows daily budget USD 550.00, last decided daily budget USD 500.00',
  ],
  [
    'an untracked own-budget campaign',
    makeDiscrepancy('untracked'),
    'Platform shows Active · daily budget USD 200.00, last decided no decision recorded',
  ],
  [
    'an untracked shared member',
    makeDiscrepancy('untracked', { shape: 'shared_member' }),
    'Platform shows Active · shared budget 31000402, last decided no decision recorded',
  ],
  [
    'an untracked shared budget, which has no status',
    makeDiscrepancy('untracked', { shape: 'budget' }),
    'Platform shows daily budget USD 500.00, last decided no decision recorded',
  ],
  [
    'a missing campaign',
    makeDiscrepancy('missing'),
    'Platform shows not returned by the platform, last decided Active · daily budget USD 200.00',
  ],
  [
    'a campaign returned after its acknowledged removal',
    makeDiscrepancy('status', {
      decided_value: { status: 'ended', tombstone: true },
      platform_value: { status: 'active' },
    }),
    'Platform shows Active, last decided Removal acknowledged · Ended',
  ],
  [
    'a tombstone side without a status',
    makeDiscrepancy('status', { decided_value: { tombstone: true } }),
    'Platform shows Paused, last decided Removal acknowledged · Ended',
  ],
  [
    'a shared budget returned after its acknowledged removal',
    makeDiscrepancy('budget', { shape: 'budget', decided_value: { tombstone: true } }),
    'Platform shows daily budget USD 550.00, last decided Removal acknowledged',
  ],
  [
    'a Google amount one micro finer than the minor unit',
    makeDiscrepancy('budget', {
      platform_value: {
        daily_budget: '200.000001',
        total_budget: null,
        currency: 'USD',
        amount_micros: 200_000_001,
      },
    }),
    'Platform shows daily budget USD 200.000001, last decided daily budget USD 200.00',
  ],
  [
    'an amount known only in micros',
    makeDiscrepancy('budget', { platform_value: { currency: 'USD', amount_micros: 200_000_001 } }),
    'Platform shows budget USD 200.000001, last decided daily budget USD 200.00',
  ],
  [
    'a lifetime budget',
    makeDiscrepancy('budget', {
      decided_value: { status: 'active', daily_budget: null, total_budget: '5000.00' },
      platform_value: { daily_budget: null, total_budget: '4500.00' },
    }),
    'Platform shows total budget USD 4,500.00, last decided total budget USD 5,000.00',
  ],
  [
    'a micro amount beyond exact integer precision',
    makeDiscrepancy('budget', {
      platform_value: { currency: 'USD', amount_micros: Number.MAX_SAFE_INTEGER + 2 },
    }),
    'Platform shows budget unavailable, last decided daily budget USD 200.00',
  ],
  [
    'a side that reports no budget',
    makeDiscrepancy('budget', { platform_value: { daily_budget: null, total_budget: null } }),
    'Platform shows no budget set, last decided daily budget USD 200.00',
  ],
  [
    'a side whose reported amount is unknown',
    makeDiscrepancy('budget', {
      platform_value: { daily_budget: null, total_budget: null, amount_micros: null },
    }),
    'Platform shows budget unavailable, last decided daily budget USD 200.00',
  ],
  [
    'a side with no budget key at all',
    makeDiscrepancy('budget', { platform_value: {} }),
    'Platform shows budget unavailable, last decided daily budget USD 200.00',
  ],
  [
    'a status the platform did not report',
    makeDiscrepancy('status', { platform_value: { status: null } }),
    'Platform shows status not reported, last decided Active',
  ],
  [
    // The API names no Google id for a budget it does not know as shared, such as the unshared
    // budget a member moved onto, so the id alone is worded neutrally, never as shared.
    'a member moved onto a budget known only by internal id',
    makeDiscrepancy('budget', {
      shape: 'shared_member',
      platform_value: { budget_id: FIXTURE_BUDGET_IDS.display, budget_external_id: null },
    }),
    'Platform shows budget (internal id 401), last decided shared budget 31000402',
  ],
  [
    'a member moved to a budget of its own',
    makeDiscrepancy('budget', {
      shape: 'shared_member',
      platform_value: { budget_id: null, budget_external_id: null },
    }),
    'Platform shows own budget (not shared), last decided shared budget 31000402',
  ],
  [
    'a member side without assignment keys',
    makeDiscrepancy('budget', { shape: 'shared_member', platform_value: {} }),
    'Platform shows budget assignment unavailable, last decided shared budget 31000402',
  ],
  [
    // Each side names its own currency, else the subject's; with neither, the amount implies
    // none, as the backend's render_value words it.
    "amounts in each side's own currency when the subject names none",
    makeDiscrepancy('budget', {
      subject: subjectWithCurrency(null),
      decided_value: { status: 'active', daily_budget: '200.00' },
      platform_value: { daily_budget: '250.00', currency: 'EUR' },
    }),
    'Platform shows daily budget EUR 250.00, last decided daily budget 200.00',
  ],
  [
    "a side's own currency ahead of the subject's",
    makeDiscrepancy('budget', {
      platform_value: { daily_budget: '250.00', currency: 'eur' },
    }),
    'Platform shows daily budget EUR 250.00, last decided daily budget USD 200.00',
  ],
  [
    'a daily and a total budget, joined as the parts of one state',
    makeDiscrepancy('untracked', {
      subject: subjectWithCurrency('EUR'),
      platform_value: { status: 'active', daily_budget: '50.00', total_budget: '1000.00' },
    }),
    'Platform shows Active · daily budget EUR 50.00 · total budget EUR 1,000.00, ' +
      'last decided no decision recorded',
  ],
  [
    'a Google amount whose micros differ from its decimal text, read from the micros',
    makeDiscrepancy('budget', {
      platform_value: {
        daily_budget: '999.00',
        total_budget: null,
        currency: 'USD',
        amount_micros: 12_345_678,
      },
    }),
    'Platform shows daily budget USD 12.345678, last decided daily budget USD 200.00',
  ],
  [
    'an amount in a currency without a minor unit, keeping a significant fraction',
    makeDiscrepancy('budget', {
      subject: subjectWithCurrency('JPY'),
      decided_value: { status: 'active', daily_budget: '5000', total_budget: null },
      platform_value: { daily_budget: '5000.5', total_budget: null },
    }),
    'Platform shows daily budget JPY 5,000.5, last decided daily budget JPY 5,000',
  ],
  [
    // ISO 4217, and so the backend that validates decisions, gives IQD three places and AFN
    // two; CLDR gives both none. The resolver follows the backend.
    'an amount in a thousandth currency, at the ISO minor unit',
    makeDiscrepancy('budget', {
      subject: subjectWithCurrency('IQD'),
      decided_value: { status: 'active', daily_budget: '60.00', total_budget: null },
      platform_value: { daily_budget: '50.00', total_budget: null },
    }),
    'Platform shows daily budget IQD 50.000, last decided daily budget IQD 60.000',
  ],
  [
    'an amount in a two-place currency that CLDR gives none',
    makeDiscrepancy('budget', {
      subject: subjectWithCurrency('AFN'),
      decided_value: { status: 'active', daily_budget: '60', total_budget: null },
      platform_value: { daily_budget: '50', total_budget: null },
    }),
    'Platform shows daily budget AFN 50.00, last decided daily budget AFN 60.00',
  ],
  [
    'a reported amount that is not a number',
    makeDiscrepancy('budget', { platform_value: { daily_budget: 'fifty', total_budget: null } }),
    'Platform shows budget unavailable, last decided daily budget USD 200.00',
  ],
  [
    'a status outside the contract, shown as sent',
    makeDiscrepancy('status', {
      platform_value: { status: ' archived ' as unknown as CampaignStatus },
    }),
    'Platform shows archived, last decided Active',
  ],
  [
    'a tombstone recording another status',
    makeDiscrepancy('status', { decided_value: { status: 'paused', tombstone: true } }),
    'Platform shows Paused, last decided Removal acknowledged · Paused',
  ],
  [
    'a side the API sent no value for, outside missing and untracked',
    makeDiscrepancy('status', { platform_value: null }),
    'Platform shows unavailable, last decided Active',
  ],
];

describe('DiscrepancyResolver values', () => {
  it.each(COMPARISONS)('renders %s from its stored keys', async (_description, item, line) => {
    mockApi(listHandlers([item]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    expect(within(list).getByText(line)).toBeTruthy();
  });

  it('words an IQD amount exactly as the C0 card on /actions does', async () => {
    mockApi(
      listHandlers([
        makeDiscrepancy('budget', {
          subject: subjectWithCurrency('IQD'),
          decided_value: { status: 'active', daily_budget: '60.00', total_budget: null },
          platform_value: { daily_budget: '50.00', total_budget: null, currency: 'IQD' },
        }),
      ]),
    );

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    const line = within(list).getByText(/^Platform shows /u);
    // backend/tests/unit/test_analysis_discrepancy.py (MONEY_PARITY) asserts render_value words
    // the platform side "daily budget IQD 50.000", with a plain space where the resolver puts
    // MONEY_SEPARATOR (U+00A0). Change both sides together.
    expect(line.textContent).toBe(
      'Platform shows daily budget IQD\u00a050.000, last decided daily budget IQD\u00a060.000',
    );
    expect(line.textContent.replaceAll(MONEY_SEPARATOR, ' ')).toBe(
      'Platform shows daily budget IQD 50.000, last decided daily budget IQD 60.000',
    );
  });
});

// ---------------------------------------------------------------------------------------------
// Resolutions offered
// ---------------------------------------------------------------------------------------------

/** A discrepancy kind and shape, and the button labels its controls must offer. */
type OfferCase = readonly [
  description: string,
  discrepancy: Discrepancy,
  labels: readonly string[],
];

const OFFERS: readonly OfferCase[] = [
  [
    'status on an own-budget campaign',
    makeDiscrepancy('status'),
    ['Platform is correct', 'Decision is correct'],
  ],
  [
    'status on a shared member',
    makeDiscrepancy('status', { shape: 'shared_member' }),
    ['Platform is correct', 'Decision is correct'],
  ],
  [
    'budget on an own-budget campaign',
    makeDiscrepancy('budget'),
    ['Platform is correct', 'Decision is correct'],
  ],
  [
    'budget on a shared member',
    makeDiscrepancy('budget', { shape: 'shared_member' }),
    ['Platform is correct', 'Decision is correct'],
  ],
  [
    'budget on a shared budget',
    makeDiscrepancy('budget', { shape: 'budget' }),
    ['Platform is correct', 'Decision is correct'],
  ],
  [
    'untracked on an own-budget campaign',
    makeDiscrepancy('untracked'),
    ['Adopt platform state', 'Record intended state'],
  ],
  [
    'untracked on a shared member',
    makeDiscrepancy('untracked', { shape: 'shared_member' }),
    ['Adopt platform state', 'Record intended state'],
  ],
  [
    'untracked on a shared budget',
    makeDiscrepancy('untracked', { shape: 'budget' }),
    ['Adopt platform state', 'Record intended state'],
  ],
  [
    'missing on a campaign',
    makeDiscrepancy('missing'),
    ['Decision is correct', 'Acknowledge removal'],
  ],
  [
    'missing on a shared budget',
    makeDiscrepancy('missing', { shape: 'budget' }),
    ['Decision is correct', 'Acknowledge removal'],
  ],
];

describe('DiscrepancyResolution resolutions offered', () => {
  it.each(OFFERS)('offers exactly the resolutions of %s', async (_description, item, labels) => {
    mockApi({});
    const { container } = await renderResolution(item);

    expect(buttonLabels(container)).toEqual(labels);
    const [first, ...rest] = within(container).getAllByRole('button');
    expect(first?.className).toContain('ds-button--primary');
    for (const button of rest) {
      expect(button.className).toContain('ds-button--secondary');
    }
  });

  it('labels every resolution with the labels the e2e suite clicks', () => {
    expect(RESOLUTION_LABELS).toEqual({
      platform_correct: 'Platform is correct',
      decision_correct: 'Decision is correct',
      adopt_platform: 'Adopt platform state',
      record_intended: 'Record intended state',
      acknowledge_removal: 'Acknowledge removal',
    });
    expect(Object.isFrozen(RESOLUTION_LABELS)).toBe(true);
  });

  it('keeps the order the API lists, adding and dropping nothing', async () => {
    mockApi({});
    // The reverse of FIXTURE_RESOLUTIONS.budget, so only the API's order can produce it.
    const { container } = await renderResolution(
      makeDiscrepancy('budget', { resolutions: ['decision_correct', 'platform_correct'] }),
    );

    expect(buttonLabels(container)).toEqual(['Decision is correct', 'Platform is correct']);
  });

  it('never offers a resolution of another kind for an untracked discrepancy', async () => {
    mockApi({});
    const { container } = await renderResolution(makeDiscrepancy('untracked'));

    for (const label of ['Platform is correct', 'Decision is correct', 'Acknowledge removal']) {
      expect(within(container).queryByRole('button', { name: label })).toBeNull();
    }
  });
});

// ---------------------------------------------------------------------------------------------
// Posting a resolution
// ---------------------------------------------------------------------------------------------

describe('DiscrepancyResolution posting', () => {
  it('posts the resolution alone with the CSRF header and reports the API response', async () => {
    const updated = makeDiscrepancy('status', {
      state: 'resolved',
      resolution: 'platform_correct',
    });
    const api = mockApi({ [RESOLVE_KEY]: json(200, updated) });
    const { onResolved } = await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    const [call] = resolveCalls(api);
    expect(call?.path).toBe(`/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`);
    expect(call?.params).toEqual({ id: String(FIXTURE_DISCREPANCY_ID) });
    expect(call?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(call?.body).toEqual({ resolution: 'platform_correct' });
    expect(onResolved).toHaveBeenCalledTimes(1);
    expect(onResolved).toHaveBeenCalledWith(updated);
  });

  it('shows the clicked resolution loading and disables the others while it runs', async () => {
    const response = deferred<MockResponse>();
    mockApi({ [RESOLVE_KEY]: () => response.promise });
    await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(buttonNamed('Platform is correct').getAttribute('aria-busy')).toBe('true');
    });
    // The loading button is inert but stays focusable (aria-disabled); the other is disabled.
    expect(buttonNamed('Platform is correct').getAttribute('aria-disabled')).toBe('true');
    expect(buttonNamed('Platform is correct').disabled).toBe(false);
    expect(buttonNamed('Decision is correct').disabled).toBe(true);
    expect(outcomeRegion().textContent).toBe('');

    response.resolve(json(200, makeDiscrepancy('status', { state: 'resolved' })));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
  });

  it('awaits the platform after "Decision is correct" and refocuses the button', async () => {
    const updated = makeDiscrepancy('status', {
      state: 'awaiting_platform',
      resolution: 'decision_correct',
    });
    const api = mockApi({ [RESOLVE_KEY]: json(200, updated) });
    const { onResolved } = await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Decision is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Waiting for the platform to match the decision');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'decision_correct' },
    ]);
    expect(onResolved).toHaveBeenCalledWith(updated);
    await waitFor(() => {
      expect(document.activeElement).toBe(buttonNamed('Decision is correct'));
    });
    expect(buttonNamed('Platform is correct').disabled).toBe(false);
  });

  it('acknowledges a removal at once, with no dialog', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: json(
        200,
        makeDiscrepancy('missing', { state: 'resolved', resolution: 'acknowledge_removal' }),
      ),
    });
    const { onResolved } = await renderResolution(makeDiscrepancy('missing'));

    fireEvent.click(buttonNamed('Acknowledge removal'));

    // The click itself posts: no dialog opens and nothing waits for confirmation.
    expect(screen.queryByRole('dialog')).toBeNull();
    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    const calls = resolveCalls(api);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.path).toBe(`/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`);
    expect(calls[0]?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(calls[0]?.body).toEqual({ resolution: 'acknowledge_removal' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onResolved).toHaveBeenCalledTimes(1);
    expect(onResolved.mock.calls[0]?.[0].state).toBe('resolved');
  });

  it('adopts the platform state of an untracked subject without opening the dialog', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: json(
        200,
        makeDiscrepancy('untracked', { state: 'resolved', resolution: 'adopt_platform' }),
      ),
    });
    await renderResolution(makeDiscrepancy('untracked'));

    fireEvent.click(buttonNamed('Adopt platform state'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([{ resolution: 'adopt_platform' }]);
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

// The codes are the ones the API sends (`decisions.py`): 409 `discrepancy_resolved` for a closed
// discrepancy and 422 `resolution_not_allowed` / `intended_state_invalid` for a refused one. The
// API has no `discrepancy_closed` or `invalid_resolution`, so those are never stubbed here.
describe('DiscrepancyResolution errors', () => {
  it('reads "Computed from replaced data" on 409 subject_retired and locks all', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: apiError(409, 'subject_retired', 'The subject belongs to a retired dataset.'),
    });
    const { onResolved } = await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Platform is correct'));

    const title = await screen.findByText('Computed from replaced data');
    expect(screen.queryByText('The subject belongs to a retired dataset.')).toBeNull();
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });
    expect(buttonNamed('Platform is correct').disabled).toBe(true);
    expect(buttonNamed('Decision is correct').disabled).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(onResolved).not.toHaveBeenCalled();

    fireEvent.click(buttonNamed('Decision is correct'));
    expect(resolveCalls(api)).toHaveLength(1);
  });

  it('reads "Already resolved" on 409 discrepancy_resolved and locks every control', async () => {
    mockApi({
      [RESOLVE_KEY]: apiError(409, 'discrepancy_resolved', 'Discrepancy 51 is already resolved.'),
    });
    await renderResolution(makeDiscrepancy('untracked'));

    fireEvent.click(buttonNamed('Adopt platform state'));

    expect(await screen.findByText('Already resolved')).toBeTruthy();
    expect(buttonNamed('Adopt platform state').disabled).toBe(true);
    expect(buttonNamed('Record intended state').disabled).toBe(true);
    fireEvent.click(buttonNamed('Record intended state'));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('shows the message of 409 platform_state_unknown and keeps the controls usable', async () => {
    mockApi({
      [RESOLVE_KEY]: apiError(
        409,
        'platform_state_unknown',
        'No entity sync has reported Brand Search — US yet.',
      ),
    });
    await renderResolution(makeDiscrepancy('untracked'));

    fireEvent.click(buttonNamed('Adopt platform state'));

    expect(
      await screen.findByText('No entity sync has reported Brand Search — US yet.'),
    ).toBeTruthy();
    await waitFor(() => {
      expect(buttonNamed('Adopt platform state').disabled).toBe(false);
    });
    expect(buttonNamed('Record intended state').disabled).toBe(false);
  });

  it('shows the message of 422 resolution_not_allowed until the next attempt', async () => {
    const NOT_ALLOWED_MESSAGE = 'decision_correct is not valid for this discrepancy.';
    let attempts = 0;
    const api = mockApi({
      [RESOLVE_KEY]: () => {
        attempts += 1;
        return attempts === 1
          ? apiError(422, 'resolution_not_allowed', NOT_ALLOWED_MESSAGE)
          : json(200, makeDiscrepancy('status', { state: 'resolved' }));
      },
    });
    await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Decision is correct'));
    expect(await screen.findByText(NOT_ALLOWED_MESSAGE)).toBeTruthy();
    await waitFor(() => {
      expect(buttonNamed('Platform is correct').disabled).toBe(false);
    });

    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(screen.queryByText(NOT_ALLOWED_MESSAGE)).toBeNull();
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'decision_correct' },
      { resolution: 'platform_correct' },
    ]);
  });

  it('shows the generic text, never the message, of a failure that is not an ApiError', async () => {
    // Assembled at run time, so the secret scan never meets a literal secret assignment.
    const secret = `${['SESSION', 'SECRET'].join('_')}=${'9f3c'.repeat(8)}`;
    const internalPath = '/srv/ads_engine/.env';
    const api = mockApi({
      [RESOLVE_KEY]: json(200, makeDiscrepancy('status', { state: 'resolved' })),
    });
    const { queryClient, onResolved } = await renderResolution(makeDiscrepancy('status'));
    // The POST succeeds, then the refetch `useResolveDiscrepancy` awaits rejects with a plain
    // Error, which TanStack Query makes the mutation's error.
    vi.spyOn(queryClient, 'invalidateQueries').mockRejectedValue(
      new Error(`ENOENT: no such file or directory, open '${internalPath}' (${secret})`),
    );

    fireEvent.click(buttonNamed('Platform is correct'));

    const title = await screen.findByText('The resolution failed.');
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });
    expect(resolveCalls(api)).toHaveLength(1);
    expect(onResolved).not.toHaveBeenCalled();
    expect(outcomeRegion().textContent).toBe('');
    expect(document.body.innerHTML).not.toContain(internalPath);
    expect(document.body.innerHTML).not.toContain(secret);
    expect(document.body.innerHTML).not.toContain('ENOENT');
  });

  it('starts fresh for another discrepancy, so a lock never spreads to another', async () => {
    mockApi({ [RESOLVE_KEY]: apiError(409, 'subject_retired', 'Retired.') });

    /** Shows `first`, then `second` in the same mount once "Show next" is clicked. */
    function Switching({ first, second }: { first: Discrepancy; second: Discrepancy }) {
      const [current, setCurrent] = useState(first);
      return (
        <>
          <Button
            onClick={() => {
              setCurrent(second);
            }}
          >
            Show next
          </Button>
          <DiscrepancyResolution discrepancy={current} />
        </>
      );
    }

    await renderWithProviders(
      <Switching
        first={makeDiscrepancy('status')}
        second={makeDiscrepancy('status', { id: 52 })}
      />,
      { path: '/decisions' },
    );
    fireEvent.click(buttonNamed('Platform is correct'));
    await screen.findByText('Computed from replaced data');
    expect(buttonNamed('Decision is correct').disabled).toBe(true);

    fireEvent.click(buttonNamed('Show next'));

    expect(screen.queryByText('Computed from replaced data')).toBeNull();
    expect(buttonNamed('Platform is correct').disabled).toBe(false);
    expect(buttonNamed('Decision is correct').disabled).toBe(false);
  });
});

// ---------------------------------------------------------------------------------------------
// disabled
// ---------------------------------------------------------------------------------------------

describe('DiscrepancyResolution disabled', () => {
  it('disables every resolution and posts nothing', async () => {
    const api = mockApi({});
    await renderResolution(makeDiscrepancy('missing'), true);

    expect(buttonNamed('Decision is correct').disabled).toBe(true);
    expect(buttonNamed('Acknowledge removal').disabled).toBe(true);
    fireEvent.click(buttonNamed('Acknowledge removal'));

    expect(api.calls).toHaveLength(0);
    expect(outcomeRegion().textContent).toBe('');
  });

  it('keeps the intended-state dialog from opening', async () => {
    const api = mockApi({});
    await renderResolution(makeDiscrepancy('untracked'), true);

    expect(buttonNamed('Adopt platform state').disabled).toBe(true);
    expect(buttonNamed('Record intended state').disabled).toBe(true);
    fireEvent.click(buttonNamed('Record intended state'));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------------------------
// Record intended state
// ---------------------------------------------------------------------------------------------

/** The handlers of the intended-state dialog: the subject's (empty) log and the resolution. */
function intendedHandlers(resolve: MockResponse): MockHandlers {
  return { 'GET /decisions': json(200, []), [RESOLVE_KEY]: resolve };
}

/** Opens the intended-state dialog and waits for its form. */
async function openIntendedState(): Promise<HTMLElement> {
  fireEvent.click(buttonNamed('Record intended state'));
  const dialog = await screen.findByRole('dialog', { name: 'Record intended state' });
  await within(dialog).findByRole('button', { name: 'Save intended state' });
  return dialog;
}

/** Submits the intended-state form. */
function saveIntendedState(dialog: HTMLElement): void {
  fireEvent.click(buttonNamed('Save intended state', dialog));
}

describe('DiscrepancyResolution "Record intended state"', () => {
  it('offers status and the own budget of an own-budget campaign, posting both', async () => {
    const discrepancy = makeDiscrepancy('untracked');
    const api = mockApi(
      intendedHandlers(json(200, { ...discrepancy, state: 'awaiting_platform' })),
    );
    const { onResolved } = await renderResolution(discrepancy);

    const dialog = await openIntendedState();

    const [logRead] = api.calls.filter((call) => call.path === '/decisions');
    expect(logRead?.search.get('subject_type')).toBe('campaign');
    expect(logRead?.search.get('subject_id')).toBe(String(FIXTURE_CAMPAIGN_IDS.brandSearch));
    expect(within(dialog).getByLabelText('Status')).toBeTruthy();
    const daily = within(dialog).getByLabelText('Daily budget');
    expect(within(dialog).queryByLabelText('Total budget')).toBeNull();
    expect((daily as HTMLInputElement).value).toBe('200.00');

    fireEvent.change(within(dialog).getByLabelText('Status'), { target: { value: 'paused' } });
    fireEvent.change(daily, { target: { value: '240' } });
    saveIntendedState(dialog);

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    const [call] = resolveCalls(api);
    expect(call?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(call?.body).toEqual({
      resolution: 'record_intended',
      intended_state: { status: 'paused', daily_budget: '240' },
    });
    expect(outcomeRegion().textContent).toBe('Waiting for the platform to match the decision');
    expect(onResolved).toHaveBeenCalledWith({
      ...discrepancy,
      state: 'awaiting_platform',
      resolution: 'record_intended',
    });
  });

  it('offers status only for a shared member, naming the budget that owns the amount', async () => {
    const discrepancy = makeDiscrepancy('untracked', { shape: 'shared_member' });
    const api = mockApi(
      intendedHandlers(json(200, { ...discrepancy, state: 'awaiting_platform' })),
    );
    await renderResolution(discrepancy);

    const dialog = await openIntendedState();

    expect(within(dialog).getByLabelText('Status')).toBeTruthy();
    expect(within(dialog).queryByLabelText('Daily budget')).toBeNull();
    expect(within(dialog).queryByLabelText('Total budget')).toBeNull();
    expect(within(dialog).getByText('The amount is set on shared budget 31000402')).toBeTruthy();

    saveIntendedState(dialog);

    await waitFor(() => {
      expect(resolveCalls(api)).toHaveLength(1);
    });
    expect(resolveCalls(api)[0]?.body).toEqual({
      resolution: 'record_intended',
      intended_state: { status: 'active' },
    });
  });

  it('offers the one amount and no status for a shared budget', async () => {
    const discrepancy = makeDiscrepancy('untracked', { shape: 'budget' });
    const api = mockApi(
      intendedHandlers(json(200, { ...discrepancy, state: 'awaiting_platform' })),
    );
    await renderResolution(discrepancy);

    const dialog = await openIntendedState();

    expect(api.calls[0]?.search.get('subject_type')).toBe('budget');
    expect(within(dialog).queryByLabelText('Status')).toBeNull();
    expect(within(dialog).queryByLabelText('Total budget')).toBeNull();
    fireEvent.change(within(dialog).getByLabelText('Daily budget'), {
      target: { value: '600.50' },
    });
    saveIntendedState(dialog);

    await waitFor(() => {
      expect(resolveCalls(api)).toHaveLength(1);
    });
    expect(resolveCalls(api)[0]?.body).toEqual({
      resolution: 'record_intended',
      intended_state: { daily_budget: '600.50' },
    });
  });

  it('keeps the dialog open with the API message on 422 intended_state_invalid', async () => {
    const discrepancy = makeDiscrepancy('untracked');
    mockApi(
      intendedHandlers(
        apiError(422, 'intended_state_invalid', 'The intended state needs the daily budget.'),
      ),
    );
    const { onResolved } = await renderResolution(discrepancy);

    const dialog = await openIntendedState();
    saveIntendedState(dialog);

    expect(
      await within(dialog).findByText('The intended state needs the daily budget.'),
    ).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'Record intended state' })).toBe(dialog);
    expect(onResolved).not.toHaveBeenCalled();
    expect(outcomeRegion().textContent).toBe('');
  });

  it('closes without posting on Cancel and on the dialog Close button', async () => {
    const api = mockApi(intendedHandlers(json(200, makeDiscrepancy('untracked'))));
    const { onResolved } = await renderResolution(makeDiscrepancy('untracked'));

    const first = await openIntendedState();
    fireEvent.click(buttonNamed('Cancel', first));
    expect(screen.queryByRole('dialog')).toBeNull();

    const second = await openIntendedState();
    fireEvent.click(buttonNamed('Close', second));
    expect(screen.queryByRole('dialog')).toBeNull();

    expect(resolveCalls(api)).toHaveLength(0);
    expect(onResolved).not.toHaveBeenCalled();
  });

  it('clears the failure of an earlier resolution when the dialog opens', async () => {
    mockApi(
      intendedHandlers(
        apiError(409, 'platform_state_unknown', 'The platform state is not known yet.'),
      ),
    );
    await renderResolution(makeDiscrepancy('untracked'));

    fireEvent.click(buttonNamed('Adopt platform state'));
    await screen.findByText('The platform state is not known yet.');
    await waitFor(() => {
      expect(buttonNamed('Record intended state').disabled).toBe(false);
    });

    await openIntendedState();

    expect(screen.queryByText('The platform state is not known yet.')).toBeNull();
  });

  it('refuses another resolution while its save runs, then reports it and opens a fresh form', async () => {
    const discrepancy = makeDiscrepancy('untracked');
    const updated: Discrepancy = {
      ...discrepancy,
      state: 'awaiting_platform',
      resolution: 'record_intended',
    };
    const firstSave = deferred<MockResponse>();
    let saves = 0;
    const api = mockApi({
      'GET /decisions': json(200, []),
      // The first save waits for the test; any later one is answered at once.
      [RESOLVE_KEY]: () => {
        saves += 1;
        return saves === 1 ? firstSave.promise : json(200, updated);
      },
    });
    const { onResolved } = await renderResolution(discrepancy);

    const opener = buttonNamed('Record intended state');
    // A click focuses the button in a browser; fireEvent does not.
    opener.focus();
    const first = await openIntendedState();
    const noteText = 'Budget raised on the platform by the agency';
    fireEvent.change(within(first).getByLabelText('Daily budget'), { target: { value: '240' } });
    fireEvent.change(within(first).getByLabelText('Note'), { target: { value: noteText } });
    saveIntendedState(first);
    await waitFor(() => {
      expect(resolveCalls(api)).toHaveLength(1);
    });
    expect(buttonNamed('Adopt platform state').disabled).toBe(true);
    // Closing the dialog unmounts the form while its request runs.
    fireEvent.click(buttonNamed('Close', first));
    expect(screen.queryByRole('dialog')).toBeNull();

    // Until the save settles, no control reopens the dialog or posts another resolution. The
    // closed dialog returned focus to the loading opener, which keeps it (aria-disabled).
    expect(opener.getAttribute('aria-busy')).toBe('true');
    expect(opener.getAttribute('aria-disabled')).toBe('true');
    expect(opener.disabled).toBe(false);
    expect(document.activeElement).toBe(opener);
    expect(buttonNamed('Adopt platform state').disabled).toBe(true);
    fireEvent.click(buttonNamed('Adopt platform state'));
    fireEvent.click(opener);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(resolveCalls(api)).toHaveLength(1);

    firstSave.resolve(json(200, updated));

    // The late save is reported in the status line and to onResolved...
    await waitFor(() => {
      expect(onResolved).toHaveBeenCalledTimes(1);
    });
    expect(onResolved).toHaveBeenCalledWith(updated);
    expect(outcomeRegion().textContent).toBe('Waiting for the platform to match the decision');
    // ...the controls are usable again, and focus is still on "Record intended state".
    await waitFor(() => {
      expect(document.activeElement).toBe(opener);
    });
    expect(opener.getAttribute('aria-busy')).toBeNull();
    expect(buttonNamed('Adopt platform state').disabled).toBe(false);

    // Reopening now starts a fresh form, without the closed one's edits, whose save closes it.
    const second = await openIntendedState();
    expect((within(second).getByLabelText('Daily budget') as HTMLInputElement).value).toBe(
      '200.00',
    );
    expect((within(second).getByLabelText('Note') as HTMLTextAreaElement).value).toBe('');
    saveIntendedState(second);
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      {
        resolution: 'record_intended',
        intended_state: { status: 'active', daily_budget: '240' },
        note: noteText,
      },
      { resolution: 'record_intended', intended_state: { status: 'active', daily_budget: '200.00' } },
    ]);
    expect(onResolved).toHaveBeenCalledTimes(2);
  });

  it.each([
    [
      '409 subject_retired',
      apiError(409, 'subject_retired', 'The subject belongs to a retired dataset.'),
      'Computed from replaced data',
    ],
    [
      '409 discrepancy_resolved',
      apiError(409, 'discrepancy_resolved', 'Discrepancy 51 is already resolved.'),
      'Already resolved',
    ],
  ] as const)(
    'locks every control on a %s to the intended state, after the dialog closes',
    async (_refusal, response, text) => {
      const api = mockApi(intendedHandlers(response));
      const { onResolved } = await renderResolution(makeDiscrepancy('untracked'));

      buttonNamed('Record intended state').focus();
      const dialog = await openIntendedState();
      saveIntendedState(dialog);

      // The form shows the refusal itself while its dialog is open.
      expect(await within(dialog).findByText(text)).toBeTruthy();
      fireEvent.click(buttonNamed('Close', dialog));
      expect(screen.queryByRole('dialog')).toBeNull();

      // The lock is the controls' own now: the same message outside the dialog, focused, and
      // every resolution disabled, so no fresh, unlocked form can open.
      const title = screen.getByText(text);
      await waitFor(() => {
        expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
      });
      expect(screen.queryByText('The subject belongs to a retired dataset.')).toBeNull();
      expect(buttonNamed('Adopt platform state').disabled).toBe(true);
      expect(buttonNamed('Record intended state').disabled).toBe(true);
      fireEvent.click(buttonNamed('Adopt platform state'));
      fireEvent.click(buttonNamed('Record intended state'));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(resolveCalls(api)).toHaveLength(1);
      expect(screen.queryByRole('alert')).toBeNull();
      expect(onResolved).not.toHaveBeenCalled();
    },
  );

  it('still locks every control when the 409 subject_retired ends a save after its dialog closed', async () => {
    const response = deferred<MockResponse>();
    const api = mockApi({ 'GET /decisions': json(200, []), [RESOLVE_KEY]: () => response.promise });
    await renderResolution(makeDiscrepancy('untracked'));

    const opener = buttonNamed('Record intended state');
    opener.focus();
    const dialog = await openIntendedState();
    saveIntendedState(dialog);
    await waitFor(() => {
      expect(resolveCalls(api)).toHaveLength(1);
    });
    fireEvent.click(buttonNamed('Close', dialog));
    expect(opener.getAttribute('aria-busy')).toBe('true');
    expect(buttonNamed('Adopt platform state').disabled).toBe(true);

    response.resolve(apiError(409, 'subject_retired', 'The subject belongs to a retired dataset.'));

    const title = await screen.findByText('Computed from replaced data');
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });
    expect(opener.getAttribute('aria-busy')).toBeNull();
    expect(opener.disabled).toBe(true);
    expect(buttonNamed('Adopt platform state').disabled).toBe(true);
    fireEvent.click(buttonNamed('Adopt platform state'));
    fireEvent.click(opener);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(resolveCalls(api)).toHaveLength(1);
  });

  it.each([
    [
      'a 422 intended_state_invalid',
      apiError(422, 'intended_state_invalid', 'The intended state needs the daily budget.'),
      'The intended state needs the daily budget.',
    ],
    [
      'a server failure',
      html(502, '<html><body>Bad Gateway</body></html>'),
      'The server is temporarily unavailable. Try again shortly.',
    ],
  ] as const)(
    'shows %s that ends after the dialog was closed outside it, focused',
    async (_failure, failure, text) => {
      const response = deferred<MockResponse>();
      const api = mockApi({
        'GET /decisions': json(200, []),
        [RESOLVE_KEY]: () => response.promise,
      });
      const { onResolved } = await renderResolution(makeDiscrepancy('untracked'));

      buttonNamed('Record intended state').focus();
      const dialog = await openIntendedState();
      saveIntendedState(dialog);
      await waitFor(() => {
        expect(resolveCalls(api)).toHaveLength(1);
      });
      fireEvent.click(buttonNamed('Close', dialog));

      response.resolve(failure);

      // The form that would have shown the failure is gone, so the controls show it, focused.
      const title = await screen.findByText(text);
      expect(screen.queryByRole('dialog')).toBeNull();
      await waitFor(() => {
        expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
      });
      expect(screen.queryByRole('alert')).toBeNull();
      // The failure locks nothing, and nothing was posted twice.
      expect(buttonNamed('Record intended state').disabled).toBe(false);
      expect(buttonNamed('Adopt platform state').disabled).toBe(false);
      expect(resolveCalls(api)).toHaveLength(1);
      expect(onResolved).not.toHaveBeenCalled();
      expect(outcomeRegion().textContent).toBe('');

      // Opening the dialog again clears it.
      await openIntendedState();
      expect(screen.queryByText(text)).toBeNull();
    },
  );

  it('keeps an intended-state failure that ends after its dialog closed until another resolution', async () => {
    const INVALID_MESSAGE = 'The intended state needs the daily budget.';
    const firstSave = deferred<MockResponse>();
    let attempts = 0;
    const api = mockApi({
      'GET /decisions': json(200, []),
      [RESOLVE_KEY]: () => {
        attempts += 1;
        return attempts === 1
          ? firstSave.promise
          : json(200, makeDiscrepancy('untracked', { state: 'resolved' }));
      },
    });
    await renderResolution(makeDiscrepancy('untracked'));

    const dialog = await openIntendedState();
    saveIntendedState(dialog);
    await waitFor(() => {
      expect(resolveCalls(api)).toHaveLength(1);
    });
    // Cancelled while the save runs, so the form that would have shown its failure is gone.
    fireEvent.click(buttonNamed('Cancel', dialog));
    expect(screen.queryByRole('dialog')).toBeNull();
    firstSave.resolve(apiError(422, 'intended_state_invalid', INVALID_MESSAGE));

    const title = await screen.findByText(INVALID_MESSAGE);
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });

    fireEvent.click(buttonNamed('Adopt platform state'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(screen.queryByText(INVALID_MESSAGE)).toBeNull();
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'record_intended', intended_state: { status: 'active', daily_budget: '200.00' } },
      { resolution: 'adopt_platform' },
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Double submits
// ---------------------------------------------------------------------------------------------

// Two presses before React renders again, as a double click or a script's `click(); click();`
// delivers them: TanStack Query reports the first resolution pending only from a timer after
// the press, so the rendered `disabled` state cannot stop the second one. Each pair is fired
// back to back, with nothing awaited between the two.
describe('DiscrepancyResolution double submits', () => {
  it('posts a resolution once when it is pressed twice within one frame', async () => {
    const updated = makeDiscrepancy('status', {
      state: 'resolved',
      resolution: 'platform_correct',
    });
    const api = mockApi({ [RESOLVE_KEY]: json(200, updated) });
    const { onResolved } = await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Platform is correct'));
    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'platform_correct' },
    ]);
    expect(onResolved).toHaveBeenCalledTimes(1);
    // The 409 discrepancy_resolved a second post would draw never arrives, so nothing locks.
    expect(screen.queryByText('Already resolved')).toBeNull();
  });

  it('posts only the first of two resolutions pressed within one frame', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: json(200, makeDiscrepancy('status', { state: 'resolved' })),
    });
    await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Platform is correct'));
    fireEvent.click(buttonNamed('Decision is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'platform_correct' },
    ]);
  });

  it('keeps the intended-state dialog closed when it is pressed in the frame of a resolution', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: json(
        200,
        makeDiscrepancy('untracked', { state: 'resolved', resolution: 'adopt_platform' }),
      ),
    });
    await renderResolution(makeDiscrepancy('untracked'));

    fireEvent.click(buttonNamed('Adopt platform state'));
    fireEvent.click(buttonNamed('Record intended state'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    // An opened form would also read the subject's decision log, which the stub refuses.
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      `POST /discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`,
    ]);
  });

  it('lets a later press post again once an awaiting-platform outcome has settled', async () => {
    const api = mockApi({
      [RESOLVE_KEY]: (call) =>
        json(
          200,
          makeDiscrepancy('status', {
            state:
              (call.body as { resolution?: string } | undefined)?.resolution === 'decision_correct'
                ? 'awaiting_platform'
                : 'resolved',
          }),
        ),
    });
    await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Decision is correct'));
    fireEvent.click(buttonNamed('Decision is correct'));
    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Waiting for the platform to match the decision');
    });
    await waitFor(() => {
      expect(buttonNamed('Platform is correct').disabled).toBe(false);
    });

    fireEvent.click(buttonNamed('Platform is correct'));
    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'decision_correct' },
      { resolution: 'platform_correct' },
    ]);
  });

  it('frees the controls after a failed resolution, so a retry posts again, once', async () => {
    const NOT_ALLOWED_MESSAGE = 'decision_correct is not valid for this discrepancy.';
    let attempts = 0;
    const api = mockApi({
      [RESOLVE_KEY]: () => {
        attempts += 1;
        return attempts === 1
          ? apiError(422, 'resolution_not_allowed', NOT_ALLOWED_MESSAGE)
          : json(200, makeDiscrepancy('status', { state: 'resolved' }));
      },
    });
    await renderResolution(makeDiscrepancy('status'));

    fireEvent.click(buttonNamed('Decision is correct'));
    fireEvent.click(buttonNamed('Decision is correct'));
    expect(await screen.findByText(NOT_ALLOWED_MESSAGE)).toBeTruthy();
    await waitFor(() => {
      expect(buttonNamed('Decision is correct').disabled).toBe(false);
    });
    expect(resolveCalls(api)).toHaveLength(1);

    fireEvent.click(buttonNamed('Platform is correct'));
    fireEvent.click(buttonNamed('Platform is correct'));

    await waitFor(() => {
      expect(outcomeRegion().textContent).toBe('Resolved');
    });
    expect(resolveCalls(api).map((call) => call.body)).toEqual([
      { resolution: 'decision_correct' },
      { resolution: 'platform_correct' },
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Focus after a refresh drops the focused card (WCAG 2.2 SC 2.4.3)
// ---------------------------------------------------------------------------------------------

/**
 * The resolver's handlers for a list changed elsewhere, by another tab or by the worker: every
 * read answers `listed()` as it stands then, and every resolution answers 409
 * `discrepancy_resolved`, as the API answers a stale screen whose discrepancy was resolved
 * elsewhere. The subject's (empty) decision log serves the intended-state dialog.
 */
function changedElsewhereHandlers(listed: () => readonly Discrepancy[]): MockHandlers {
  return {
    'GET /runs/latest': json(200, makeRun()),
    'GET /decisions': json(200, []),
    [LIST_KEY]: () => json(200, listed()),
    [RESOLVE_KEY]: (call) =>
      apiError(409, 'discrepancy_resolved', `Discrepancy ${call.params.id ?? ''} is resolved.`),
  };
}

/** Reads the resolver's list again, as its 30 s poll does. */
async function refreshList(queryClient: QueryClient): Promise<void> {
  await act(async () => {
    await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
  });
}

/** {@link threeCards}, checked to hold three discrepancies. */
function threeListed(): [Discrepancy, Discrepancy, Discrepancy] {
  const [first, second, third] = threeCards();
  if (first === undefined || second === undefined || third === undefined) {
    throw new Error('threeCards() returned fewer than three discrepancies');
  }
  return [first, second, third];
}

/** The list items made focusable as a place to put focus (`tabindex`), in document order. */
function focusableItems(): HTMLElement[] {
  return screen.queryAllByRole('listitem').filter((item) => item.hasAttribute('tabindex'));
}

describe('DiscrepancyResolver focus after a refresh drops the focused card', () => {
  it('moves focus to the next card once a refresh drops a card answered "Already resolved"', async () => {
    const [first, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [first, second, third];
    const api = mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const stale = await findCardItem('Brand Search — US');
    const next = await findCardItem('Search Partners — US');

    fireEvent.click(buttonNamed('Platform is correct', stale));
    const title = await within(stale).findByText('Already resolved');
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });
    expect(stale.contains(document.activeElement)).toBe(true);

    // Another tab resolved it; the next read leaves it out.
    listed = [second, third];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
    expect(stale.isConnected).toBe(false);
    expect(focusableItems()).toEqual([next]);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
    // This tab closed nothing, so no closure is announced.
    expect(within(listRegion()).queryByText(/ resolved$/u)).toBeNull();
    expect(resolveCalls(api)).toHaveLength(1);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('moves focus to the status region once a refresh drops the only, "Already resolved" card', async () => {
    const [first] = threeListed();
    let listed: readonly Discrepancy[] = [first];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const stale = await findCardItem('Brand Search — US');
    const region = listRegion();

    fireEvent.click(buttonNamed('Platform is correct', stale));
    const title = await within(stale).findByText('Already resolved');
    await waitFor(() => {
      expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
    });

    listed = [];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(document.activeElement).toBe(region);
    });
    expect(listRegion()).toBe(region);
    expect(region.getAttribute('tabindex')).toBe('-1');
    expect(within(region).getByText('No open discrepancies')).toBeTruthy();
    expect(within(region).queryByText('Brand Search — US resolved')).toBeNull();
    expect(screen.queryByRole('list')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('moves focus to the next card when the poll drops the focused card, unresolved here', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const [first, second, third] = threeListed();
      let listed: readonly Discrepancy[] = [first, second, third];
      const api = mockApi(changedElsewhereHandlers(() => listed));
      await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
      const dropped = await findCardItem('Brand Search — US');
      const next = await findCardItem('Search Partners — US');
      const control = buttonNamed('Decision is correct', dropped);
      control.focus();
      expect(document.activeElement).toBe(control);

      // The worker auto-resolved it; the 30 s poll leaves it out.
      listed = [second, third];
      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));

      await waitFor(() => {
        expect(document.activeElement).toBe(next);
      });
      expect(dropped.isConnected).toBe(false);
      expect(focusableItems()).toEqual([next]);
      expect(within(listRegion()).queryByText(/ resolved$/u)).toBeNull();
      expect(resolveCalls(api)).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('moves focus to the previous card when a refresh drops the focused last card', async () => {
    const [first, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [first, second, third];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const firstItem = await findCardItem('Brand Search — US');
    const previous = await findCardItem('Search Partners — US');
    const dropped = await findCardItem('Search Shared Budget');
    buttonNamed('Platform is correct', dropped).focus();

    listed = [first, second];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(document.activeElement).toBe(previous);
    });
    expect(dropped.isConnected).toBe(false);
    expect(firstItem.hasAttribute('tabindex')).toBe(false);
    expect(focusableItems()).toEqual([previous]);
  });

  it('moves focus to the next card when a refresh drops the card whose dialog holds focus', async () => {
    const [, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [makeDiscrepancy('untracked'), second, third];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const dropped = await findCardItem('Brand Search — US');
    const next = await findCardItem('Search Partners — US');
    fireEvent.click(buttonNamed('Record intended state', dropped));
    const dialog = await screen.findByRole('dialog', { name: 'Record intended state' });
    await within(dialog).findByRole('button', { name: 'Save intended state' });
    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true);
    });

    listed = [second, third];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(document.activeElement).toBe(next);
    });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(dropped.isConnected).toBe(false);
    expect(focusableItems()).toEqual([next]);
  });

  it('leaves focus the user moved to another card when a refresh drops the card left', async () => {
    const [first, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [first, second, third];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const left = await findCardItem('Brand Search — US');
    const other = await findCardItem('Search Partners — US');
    buttonNamed('Decision is correct', left).focus();
    const moved = buttonNamed('Platform is correct', other);
    moved.focus();

    listed = [second, third];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(left.isConnected).toBe(false);
    });
    await settle(queryClient);
    expect(document.activeElement).toBe(moved);
    expect(focusableItems()).toEqual([]);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
  });

  it('leaves focus on the page when the user put it there after leaving the dropped card', async () => {
    const [first, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [first, second, third];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(
      <>
        <Button onClick={() => undefined}>Elsewhere</Button>
        <DiscrepancyResolver />
      </>,
      { path: '/decisions' },
    );
    const left = await findCardItem('Brand Search — US');
    buttonNamed('Decision is correct', left).focus();
    // Focus moves to a control outside the list, then onto the page, as a click on it does.
    const elsewhere = buttonNamed('Elsewhere');
    elsewhere.focus();
    elsewhere.blur();
    expect(document.activeElement).toBe(document.body);

    listed = [second, third];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(left.isConnected).toBe(false);
    });
    await settle(queryClient);
    expect(document.activeElement).toBe(document.body);
    expect(focusableItems()).toEqual([]);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
  });

  it('leaves focus on the page when the user moved it there from the dropped card', async () => {
    const [first, second, third] = threeListed();
    let listed: readonly Discrepancy[] = [first, second, third];
    mockApi(changedElsewhereHandlers(() => listed));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    const left = await findCardItem('Brand Search — US');
    const control = buttonNamed('Decision is correct', left);
    control.focus();
    // As a click on an empty part of the page does.
    control.blur();
    expect(document.activeElement).toBe(document.body);

    listed = [second, third];
    await refreshList(queryClient);

    await waitFor(() => {
      expect(left.isConnected).toBe(false);
    });
    await settle(queryClient);
    expect(document.activeElement).toBe(document.body);
    expect(focusableItems()).toEqual([]);
    expect(listRegion().hasAttribute('tabindex')).toBe(false);
  });
});

// A failure of the intended-state save that the open form already showed is dismissed with the
// dialog, however the user closes it; either 409 lock outlives the dialog with its own message.
describe('DiscrepancyResolution intended-state failure closed with its dialog', () => {
  const REFUSED_MESSAGE = 'The daily budget must be a positive amount below 1,000,000,000,000.';

  /** The backdrop of the open dialog, whose click asks the dialog to close. */
  function backdropOf(dialog: HTMLElement): Element {
    const backdrop = dialog.parentElement?.querySelector('.ds-dialog__backdrop') ?? null;
    if (backdrop === null) {
      throw new Error('The dialog renders no backdrop');
    }
    return backdrop;
  }

  /** Each way of closing the dialog without saving. */
  const CLOSES: readonly (readonly [string, (dialog: HTMLElement) => void])[] = [
    [
      'Cancel',
      (dialog) => {
        fireEvent.click(buttonNamed('Cancel', dialog));
      },
    ],
    [
      'the Close button',
      (dialog) => {
        fireEvent.click(buttonNamed('Close', dialog));
      },
    ],
    [
      'Escape',
      (dialog) => {
        fireEvent.keyDown(dialog, { key: 'Escape' });
      },
    ],
    [
      'the backdrop',
      (dialog) => {
        fireEvent.click(backdropOf(dialog));
      },
    ],
  ];

  it.each(CLOSES)(
    'clears a 422 the open form showed once %s closes the dialog',
    async (_close, close) => {
      const api = mockApi(
        intendedHandlers(apiError(422, 'intended_state_invalid', REFUSED_MESSAGE)),
      );
      const { onResolved } = await renderResolution(makeDiscrepancy('untracked'));

      const opener = buttonNamed('Record intended state');
      opener.focus();
      const dialog = await openIntendedState();
      saveIntendedState(dialog);
      expect(await within(dialog).findByText(REFUSED_MESSAGE)).toBeTruthy();

      close(dialog);

      expect(screen.queryByRole('dialog')).toBeNull();
      expect(screen.queryByText(REFUSED_MESSAGE)).toBeNull();
      // The dialog hands focus back to its opener, which no banner takes from it.
      await waitFor(() => {
        expect(document.activeElement).toBe(opener);
      });
      expect(opener.disabled).toBe(false);
      expect(buttonNamed('Adopt platform state').disabled).toBe(false);
      expect(resolveCalls(api)).toHaveLength(1);
      expect(onResolved).not.toHaveBeenCalled();
      expect(outcomeRegion().textContent).toBe('');
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('clears a server failure the open form showed once Cancel closes the dialog', async () => {
    const text = 'The server is temporarily unavailable. Try again shortly.';
    mockApi(intendedHandlers(html(502, '<html><body>Bad Gateway</body></html>')));
    await renderResolution(makeDiscrepancy('untracked'));

    const dialog = await openIntendedState();
    saveIntendedState(dialog);
    expect(await within(dialog).findByText(text)).toBeTruthy();
    fireEvent.click(buttonNamed('Cancel', dialog));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByText(text)).toBeNull();
    expect(buttonNamed('Record intended state').disabled).toBe(false);
  });

  it.each([
    [
      '409 subject_retired',
      apiError(409, 'subject_retired', 'The subject belongs to a retired dataset.'),
      'Computed from replaced data',
      'Escape',
    ],
    [
      '409 discrepancy_resolved',
      apiError(409, 'discrepancy_resolved', 'Discrepancy 51 is already resolved.'),
      'Already resolved',
      'Cancel',
    ],
  ] as const)(
    'keeps the %s lock and its message once the dialog closes',
    async (_refusal, response, text, closeName) => {
      const api = mockApi(intendedHandlers(response));
      await renderResolution(makeDiscrepancy('untracked'));

      buttonNamed('Record intended state').focus();
      const dialog = await openIntendedState();
      saveIntendedState(dialog);
      expect(await within(dialog).findByText(text)).toBeTruthy();
      const close = CLOSES.find(([name]) => name === closeName)?.[1];
      if (close === undefined) {
        throw new Error(`No way to close the dialog named ${closeName}`);
      }
      close(dialog);

      expect(screen.queryByRole('dialog')).toBeNull();
      const title = screen.getByText(text);
      await waitFor(() => {
        expect(document.activeElement).toBe(title.closest('[tabindex="-1"]'));
      });
      expect(buttonNamed('Adopt platform state').disabled).toBe(true);
      expect(buttonNamed('Record intended state').disabled).toBe(true);
      fireEvent.click(buttonNamed('Record intended state'));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(resolveCalls(api)).toHaveLength(1);
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );
});
