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
 *   reports the API's response to `onResolved`;
 * - "Record intended state" opens a dialog holding `DecisionForm` with exactly the fields of the
 *   subject's decision shape, and posts the complete intended state;
 * - 409 `subject_retired` and 409 `discrepancy_resolved` lock every control; 409
 *   `platform_state_unknown`, 422 `resolution_not_allowed` and 422 `intended_state_invalid` show
 *   the API's own message and leave the controls usable; `disabled` locks every control;
 * - the list's loading, empty, failed and failed-refresh states, every page of the `Link`
 *   cursor, and each side of a discrepancy rendered from its stored keys by kind and shape.
 *
 * `../../test/mockApi` is imported first so its `afterEach` check runs after Testing Library's
 * cleanup (Vitest skips later hooks once one throws).
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';

import { queryKeys } from '../../api/queries';
import type { Discrepancy, SubjectView } from '../../api/types';
import { Button } from '../../design-system';
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

/** The `Link` header of a default-state page whose last read row is `after`. */
function nextLink(after: number): string {
  const query = `state=open&state=awaiting_platform&limit=500&after=${String(after)}`;
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

    page.resolve(json(200, [makeDiscrepancy('status')]));

    const card = await findCard('Brand Search — US');
    expect(card.id).toBe(`discrepancy-${String(FIXTURE_DISCREPANCY_ID)}`);
    expect(screen.queryByText('Loading discrepancies')).toBeNull();
  });

  it('reads the open and awaiting-platform states at the largest page size', async () => {
    const api = mockApi(listHandlers([makeDiscrepancy('status')]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });
    await findCard('Brand Search — US');

    const [first] = listCalls(api);
    expect(first?.search.getAll('state')).toEqual(['open', 'awaiting_platform']);
    expect(first?.search.get('limit')).toBe('500');
  });

  it('lists every Link cursor page, oldest first, each card under its own anchor', async () => {
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
      [LIST_KEY]: json(200, [oldest, member], { Link: nextLink(52) }),
      [`${LIST_KEY}&after=52`]: json(200, [budget]),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
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
    expect(document.getElementById('discrepancy-52')).toBe(await findCard('Search Partners — US'));
    expect(document.getElementById('discrepancy-60')).toBe(await findCard('Search Shared Budget'));
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
        'Platform shows not returned by the platform, last decided daily budget $500.00',
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

  it('says so when there is no open discrepancy', async () => {
    mockApi(listHandlers([]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    expect(await screen.findByText('No open discrepancies')).toBeTruthy();
    expect(screen.getByText('Platform state matches every last decision.')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('shows the API message when the first read fails', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LIST_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    expect(await screen.findByText('Discrepancies could not be loaded')).toBeTruthy();
    expect(screen.getByText('Database unavailable')).toBeTruthy();
    expect(screen.queryByRole('list')).toBeNull();
  });

  it('keeps the list shown and warns when a refresh fails', async () => {
    const api = mockApi(listHandlers([makeDiscrepancy('status')]));
    const { queryClient } = await renderWithProviders(<DiscrepancyResolver />, {
      path: '/decisions',
    });
    await findCard('Brand Search — US');

    api.use({ [LIST_KEY]: apiError(503, 'unavailable', 'The database is restarting') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.discrepanciesAll });
    });

    expect(await screen.findByText('Discrepancies could not be refreshed')).toBeTruthy();
    expect(screen.getByText('The database is restarting')).toBeTruthy();
    expect(screen.getByRole('article', { name: 'Brand Search — US' })).toBeTruthy();
  });

  it('drops a discrepancy the resolution closed when the list refetches', async () => {
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
    fireEvent.click(buttonNamed('Platform is correct', card));

    expect(await screen.findByText('No open discrepancies')).toBeTruthy();
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
    'Platform shows daily budget $250.00, last decided daily budget $200.00',
  ],
  [
    "a shared member's budget assignment",
    makeDiscrepancy('budget', { shape: 'shared_member' }),
    'Platform shows shared budget 31000401, last decided shared budget 31000402',
  ],
  [
    "a shared budget's amount",
    makeDiscrepancy('budget', { shape: 'budget' }),
    'Platform shows daily budget $550.00, last decided daily budget $500.00',
  ],
  [
    'an untracked own-budget campaign',
    makeDiscrepancy('untracked'),
    'Platform shows Active · daily budget $200.00, last decided nothing decided yet',
  ],
  [
    'an untracked shared member',
    makeDiscrepancy('untracked', { shape: 'shared_member' }),
    'Platform shows Active · shared budget 31000402, last decided nothing decided yet',
  ],
  [
    'an untracked shared budget, which has no status',
    makeDiscrepancy('untracked', { shape: 'budget' }),
    'Platform shows daily budget $500.00, last decided nothing decided yet',
  ],
  [
    'a missing campaign',
    makeDiscrepancy('missing'),
    'Platform shows not returned by the platform, last decided Active · daily budget $200.00',
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
    'Platform shows daily budget $550.00, last decided Removal acknowledged',
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
    'Platform shows daily budget $200.00 (exactly 200.000001), last decided daily budget $200.00',
  ],
  [
    'an amount known only in micros',
    makeDiscrepancy('budget', { platform_value: { currency: 'USD', amount_micros: 200_000_001 } }),
    'Platform shows budget $200.00 (exactly 200.000001), last decided daily budget $200.00',
  ],
  [
    'a lifetime budget',
    makeDiscrepancy('budget', {
      decided_value: { status: 'active', daily_budget: null, total_budget: '5000.00' },
      platform_value: { daily_budget: null, total_budget: '4500.00' },
    }),
    'Platform shows total budget $4,500.00, last decided total budget $5,000.00',
  ],
  [
    'a micro amount beyond exact integer precision',
    makeDiscrepancy('budget', {
      platform_value: { currency: 'USD', amount_micros: Number.MAX_SAFE_INTEGER + 2 },
    }),
    'Platform shows budget unavailable, last decided daily budget $200.00',
  ],
  [
    'a side that reports no budget',
    makeDiscrepancy('budget', { platform_value: { daily_budget: null, total_budget: null } }),
    'Platform shows no budget set, last decided daily budget $200.00',
  ],
  [
    'a side whose reported amount is unknown',
    makeDiscrepancy('budget', {
      platform_value: { daily_budget: null, total_budget: null, amount_micros: null },
    }),
    'Platform shows budget unavailable, last decided daily budget $200.00',
  ],
  [
    'a side with no budget key at all',
    makeDiscrepancy('budget', { platform_value: {} }),
    'Platform shows budget unavailable, last decided daily budget $200.00',
  ],
  [
    'a status the platform did not report',
    makeDiscrepancy('status', { platform_value: { status: null } }),
    'Platform shows status not reported, last decided Active',
  ],
  [
    'a member assigned to a budget known only by internal id',
    makeDiscrepancy('budget', {
      shape: 'shared_member',
      platform_value: { budget_id: FIXTURE_BUDGET_IDS.display, budget_external_id: null },
    }),
    'Platform shows shared budget (internal id 401), last decided shared budget 31000402',
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
    "amounts in a side's currency when the subject names none",
    makeDiscrepancy('budget', {
      subject: subjectWithCurrency(null),
      decided_value: { status: 'active', daily_budget: '200.00' },
      platform_value: { daily_budget: '250.00', currency: 'EUR' },
    }),
    'Platform shows daily budget €250.00, last decided daily budget €200.00',
  ],
];

describe('DiscrepancyResolver values', () => {
  it.each(COMPARISONS)('renders %s from its stored keys', async (_description, item, line) => {
    mockApi(listHandlers([item]));

    await renderWithProviders(<DiscrepancyResolver />, { path: '/decisions' });

    const list = await screen.findByRole('list', { name: 'Discrepancies to resolve' });
    expect(within(list).getByText(line)).toBeTruthy();
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
    expect(buttonNamed('Platform is correct').disabled).toBe(true);
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
});
