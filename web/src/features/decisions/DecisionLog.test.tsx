/**
 * Component tests for `DecisionLog.tsx`, the decision log on `/decisions` (AAP 0.5.2, 0.5.5
 * "`DecisionLog` lists decisions per subject (campaign or shared budget), with origin, time and
 * the patch applied"; FR-10).
 *
 * They hold the component to its TSDoc contract against the real query hooks, with only the
 * network stubbed by `../../test/mockApi`:
 *
 * - `GET /decisions` with no filter, rendered as one `<h3>` section per subject (keyed by type
 *   and id) in the order of each subject's newest decision, rows kept newest first;
 * - the six columns, Decided, Origin, Decided by, Patch applied, Resulting expected state and
 *   Note, with instants in the default run's time zone (UTC until one exists), amounts in each
 *   subject's own currency, the patch read defensively from stored JSON, and the expected state
 *   rendered by decision shape, tombstones included;
 * - the loading, failed, empty and row-limit states, and a failed refetch that keeps the log,
 *   each outcome in one polite status region kept from the first render (never an alert), with
 *   Retry, the last-loaded instant, no flicker while a poll reads again, and polling itself;
 * - "Record decision", which opens one page-level dialog hosting `DecisionForm` for that subject
 *   in its currency, closes on save or cancel, lets the refetched log show the new row first,
 *   and announces "Decision recorded" in the log's region after a save; a save that ends after
 *   its dialog closed leaves a dialog opened since for another subject open with its edits;
 * - saves that outlive their dialog: while a subject's save runs its "Record decision" is busy
 *   and cannot reopen the dialog, while other subjects stay usable; a failure whose form is no
 *   longer shown appears in the log's region as a warning banner naming the subject, takes
 *   focus only when focus was lost, never as an alert, and is cleared by reopening or by a
 *   later successful save; a 409 `subject_retired` locks the subject with "Computed from
 *   replaced data".
 *
 * `../../test/mockApi` is imported first so its `afterEach` check runs after Testing Library's
 * cleanup (Vitest skips later hooks once one throws).
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';

import { CURRENT_STATE_POLL_MS, queryKeys } from '../../api/queries';
import type { Decision, DecisionPatchView, SubjectType, SubjectView } from '../../api/types';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  makeDecision,
  makeRun,
} from '../../test/fixtures';
import { formatInstant } from '../../lib/format';
import { renderWithProviders } from '../../test/render';
import { DecisionLog } from './DecisionLog';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The handler key of the unfiltered log. */
const LOG_KEY = 'GET /decisions';

/** The column headers, in their fixed order. */
const COLUMN_HEADERS = [
  'Decided',
  'Origin',
  'Decided by',
  'Patch applied',
  'Resulting expected state',
  'Note',
];

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

/** The log's handlers: the default run and the decisions. */
function logHandlers(decisions: readonly Decision[]): MockHandlers {
  return { 'GET /runs/latest': json(200, makeRun()), [LOG_KEY]: json(200, decisions) };
}

/** Renders the log at `/decisions` inside the test query client and router. */
async function renderLog() {
  return renderWithProviders(<DecisionLog />, { path: '/decisions' });
}

/** A subject's decisions table, found by its caption. */
async function findTable(name: string): Promise<HTMLElement> {
  return screen.findByRole('table', { name: `Decisions for ${name}` });
}

/** The text of every body cell of a table, row by row. */
function bodyRows(table: HTMLElement): string[][] {
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) =>
      Array.from(row.querySelectorAll('th, td')).map((cell) => cell.textContent),
    );
}

/** The single body row of a table. */
function onlyRow(table: HTMLElement): string[] {
  const rows = bodyRows(table);
  expect(rows).toHaveLength(1);
  return rows[0] ?? [];
}

/** The handler key of `DecisionForm`'s read of one subject's decisions. */
function formKey(type: SubjectType, id: number): string {
  return `GET /decisions?subject_type=${type}&subject_id=${String(id)}`;
}

/** Every `GET /decisions` the stub received. */
function logCalls(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'GET' && call.path === '/decisions');
}

/** A subject view for a test subject. */
function subject(overrides: Partial<SubjectView>): SubjectView {
  return {
    type: 'campaign',
    id: FIXTURE_CAMPAIGN_IDS.genericCrm,
    name: 'Generic CRM — US',
    source: 'google',
    currency: 'USD',
    ...overrides,
  };
}

/** A button by its accessible name, typed as a button element. */
function buttonNamed(name: string, scope: HTMLElement = document.body): HTMLButtonElement {
  const button = within(scope).getByRole('button', { name });
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`"${name}" is not a button element`);
  }
  return button;
}

/**
 * The log's own polite status region: the one `div` status element outside the dialog.
 * `hidden` is set so the region is found while it is still empty.
 */
function logRegion(): HTMLElement {
  const regions = screen
    .getAllByRole('status', { hidden: true })
    .filter((element) => element.tagName === 'DIV' && element.closest('[role="dialog"]') === null);
  const [region] = regions;
  if (region === undefined || regions.length !== 1) {
    throw new Error(`Expected one log status region, found ${String(regions.length)}`);
  }
  return region;
}

/** A user's budget-only patch on the own-budget campaign, newer than its baseline. */
const USER_BUDGET_DECISION = makeDecision('own_budget', {
  id: 811,
  origin: 'user',
  decided_by: 'admin@example.com',
  decided_at: '2026-10-03T09:30:00Z',
  daily_budget: '240.00',
  patch: { daily_budget: '240.00' },
  note: 'Raised after the B1 card',
});

/** The default log: one subject of each shape, the own-budget campaign decided twice. */
function defaultLog(): Decision[] {
  return [
    USER_BUDGET_DECISION,
    makeDecision('shared_member'),
    makeDecision('budget'),
    makeDecision('own_budget'),
  ];
}

// ---------------------------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------------------------

describe('DecisionLog states', () => {
  it('shows a spinner while the log loads, then the subjects', async () => {
    const log = deferred<MockResponse>();
    mockApi({ 'GET /runs/latest': json(200, makeRun()), [LOG_KEY]: () => log.promise });

    await renderLog();

    expect(screen.getByText('Loading decisions')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();

    log.resolve(json(200, defaultLog()));

    await findTable('Brand Search — US');
    expect(screen.queryByText('Loading decisions')).toBeNull();
  });

  it('reads the whole log, with no subject filter', async () => {
    const api = mockApi(logHandlers(defaultLog()));

    await renderLog();
    await findTable('Brand Search — US');

    const [call] = logCalls(api);
    expect(call?.search.has('subject_type')).toBe(false);
    expect(call?.search.has('subject_id')).toBe(false);
  });

  it('says baseline decisions come with the first entity sync when the log is empty', async () => {
    mockApi(logHandlers([]));

    await renderLog();

    const region = logRegion();
    expect(await within(region).findByText('No decisions recorded yet')).toBeTruthy();
    expect(
      within(region).getByText('Baseline decisions appear after the first entity sync.'),
    ).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it("shows the API's message when the log cannot be loaded", async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LOG_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderLog();

    const region = logRegion();
    expect(await within(region).findByText('Decisions could not be loaded')).toBeTruthy();
    expect(within(region).getByText('Database unavailable')).toBeTruthy();
    expect(buttonNamed('Retry', region)).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps one polite status region, never an alert, from loading to data', async () => {
    const second = deferred<MockResponse>();
    let reads = 0;
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LOG_KEY]: () => {
        reads += 1;
        return reads === 1
          ? apiError(500, 'internal_error', 'Database unavailable')
          : second.promise;
      },
    });

    await renderLog();
    const region = logRegion();
    expect(region.textContent).toBe('');
    expect(region.contains(screen.getByText('Loading decisions'))).toBe(false);

    const retry = await within(region).findByRole('button', { name: 'Retry' });
    fireEvent.click(retry);
    // The failure stays in place while the read runs again: no spinner, the button is loading.
    await waitFor(() => {
      expect(retry.getAttribute('aria-busy')).toBe('true');
    });
    expect(screen.queryByText('Loading decisions')).toBeNull();
    expect(within(region).getByText('Decisions could not be loaded')).toBeTruthy();

    second.resolve(json(200, defaultLog()));
    await findTable('Brand Search — US');
    expect(logRegion()).toBe(region);
    expect(region.textContent).toBe('');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(logCalls(api)).toHaveLength(2);
  });

  it('returns focus to Retry when a retry the user started fails again', async () => {
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LOG_KEY]: apiError(503, 'unavailable', 'Database unavailable'),
    });

    await renderLog();
    const region = logRegion();
    const retry = await within(region).findByRole('button', { name: 'Retry' });
    // A browser drops focus to the body when the focused button turns disabled while loading;
    // jsdom keeps it, so the click is made with focus already on the body.
    expect(document.activeElement).toBe(document.body);
    fireEvent.click(retry);

    await waitFor(() => {
      expect(logCalls(api)).toHaveLength(2);
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(within(region).getByRole('button', { name: 'Retry' }));
    });
    expect(within(region).getByText('Database unavailable')).toBeTruthy();
  });

  it('keeps a failed first read on screen while a poll reads the log again', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const poll = deferred<MockResponse>();
      let reads = 0;
      mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LOG_KEY]: () => {
          reads += 1;
          return reads === 1 ? apiError(502, 'http_502', 'Bad gateway') : poll.promise;
        },
      });

      await renderLog();
      const region = logRegion();
      expect(await within(region).findByText('Decisions could not be loaded')).toBeTruthy();

      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      expect(reads).toBe(2);
      expect(screen.queryByText('Loading decisions')).toBeNull();
      expect(within(region).getByText('Bad gateway')).toBeTruthy();

      poll.resolve(json(200, defaultLog()));
      await findTable('Brand Search — US');
      expect(within(region).queryByText('Decisions could not be loaded')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps showing the loaded log when a refetch fails', async () => {
    const api = mockApi(logHandlers(defaultLog()));
    const { queryClient } = await renderLog();
    await findTable('Brand Search — US');

    api.use({ [LOG_KEY]: apiError(503, 'unavailable', 'The database is restarting') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.decisionsAll });
    });

    expect(logCalls(api)).toHaveLength(2);
    expect(screen.getByRole('table', { name: 'Decisions for Brand Search — US' })).toBeTruthy();
    expect(screen.queryByText('Decisions could not be loaded')).toBeNull();
  });

  it('warns, as last loaded, with Retry, in its status region when a refetch fails', async () => {
    const api = mockApi(logHandlers(defaultLog()));
    const { queryClient } = await renderLog();
    await findTable('Brand Search — US');
    const loadedAt = queryClient.getQueryState(queryKeys.decisions({}))?.dataUpdatedAt;

    api.use({ [LOG_KEY]: apiError(503, 'unavailable', 'The database is restarting') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.decisionsAll });
    });

    const region = logRegion();
    expect(await within(region).findByText('Decisions could not be refreshed')).toBeTruthy();
    expect(within(region).getByText('The database is restarting')).toBeTruthy();
    expect(loadedAt).toBeDefined();
    const loadedText = formatInstant(new Date(loadedAt ?? 0).toISOString(), 'UTC');
    expect(within(region).getByText(`Showing the log as last loaded ${loadedText}.`)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();

    api.use(logHandlers(defaultLog()));
    fireEvent.click(buttonNamed('Retry', region));
    await waitFor(() => {
      expect(within(region).queryByText('Decisions could not be refreshed')).toBeNull();
    });
    expect(screen.getByRole('table', { name: 'Decisions for Brand Search — US' })).toBeTruthy();
  });

  it('never presents an earlier empty log as current when a refetch fails', async () => {
    const api = mockApi(logHandlers([]));
    const { queryClient } = await renderLog();
    const region = logRegion();
    expect(await within(region).findByText('No decisions recorded yet')).toBeTruthy();

    api.use({ [LOG_KEY]: apiError(502, 'http_502', 'Bad gateway') });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: queryKeys.decisionsAll });
    });

    expect(await within(region).findByText('Decisions could not be refreshed')).toBeTruthy();
    expect(within(region).getByText('Bad gateway')).toBeTruthy();
    expect(
      within(region).getByText(/^No decisions were recorded when the log was last loaded .+\.$/),
    ).toBeTruthy();
    expect(within(region).queryByText('No decisions recorded yet')).toBeNull();
    expect(buttonNamed('Retry', region)).toBeTruthy();
  });

  it('refreshes itself while shown, picking up a decision recorded elsewhere', async () => {
    // A later decision on the same own-budget campaign, with an id of its own, as another
    // session would record it after the default log's newest row.
    const elsewhere = makeDecision('own_budget', {
      id: 812,
      origin: 'user',
      decided_by: 'admin@example.com',
      decided_at: '2026-10-04T14:15:00Z',
      status: 'paused',
      daily_budget: '240.00',
      patch: { status: 'paused' },
      note: 'Paused from another session',
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let recorded = false;
      mockApi({
        'GET /runs/latest': json(200, makeRun()),
        [LOG_KEY]: () => json(200, recorded ? [elsewhere, ...defaultLog()] : defaultLog()),
      });

      await renderLog();
      const ownTable = await findTable('Brand Search — US');
      expect(bodyRows(ownTable)).toHaveLength(2);

      recorded = true;
      await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
      const refreshedTable = (): HTMLElement =>
        screen.getByRole('table', { name: 'Decisions for Brand Search — US' });
      await waitFor(() => {
        expect(bodyRows(refreshedTable())).toHaveLength(3);
      });
      expect(bodyRows(refreshedTable())[0]).toEqual([
        'Oct 4, 2026, 2:15 PM UTC',
        'User',
        'admin@example.com',
        'status: paused',
        'Paused · daily budget $240.00',
        'Paused from another session',
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('notes that older decisions may be missing at the 200-row limit', async () => {
    const rows = Array.from({ length: 200 }, (_, index) =>
      makeDecision('own_budget', { id: index + 1 }),
    );
    mockApi(logHandlers(rows));

    await renderLog();

    const table = await findTable('Brand Search — US');
    expect(bodyRows(table)).toHaveLength(200);
    expect(
      screen.getByText('Showing the newest 200 decisions; older decisions are not listed.'),
    ).toBeTruthy();
  });

  it('adds no limit note below 200 rows', async () => {
    const rows = Array.from({ length: 199 }, (_, index) =>
      makeDecision('own_budget', { id: index + 1 }),
    );
    mockApi(logHandlers(rows));

    await renderLog();

    const table = await findTable('Brand Search — US');
    expect(bodyRows(table)).toHaveLength(199);
    expect(screen.queryByText(/^Showing the newest/)).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Subjects and rows
// ---------------------------------------------------------------------------------------------

describe('DecisionLog subjects', () => {
  it('groups rows by subject in the order of their newest decision, newest first', async () => {
    mockApi(logHandlers(defaultLog()));

    await renderLog();
    await findTable('Brand Search — US');

    const titles = screen
      .getAllByRole('heading', { level: 3 })
      .map((heading) => heading.textContent);
    expect(titles).toEqual([
      'Brand Search — US · Campaign · Google',
      'Search Partners — US · Campaign · Google',
      'Search Shared Budget · Shared budget · Google',
    ]);
    const own = await findTable('Brand Search — US');
    expect(bodyRows(own).map((row) => row[1])).toEqual(['User', 'Baseline']);
  });

  it('anchors each subject section by type and id, its action described by its title', async () => {
    mockApi(logHandlers(defaultLog()));

    await renderLog();
    await findTable('Brand Search — US');

    const campaign = document.getElementById(
      `decision-subject-campaign-${String(FIXTURE_CAMPAIGN_IDS.brandSearch)}`,
    );
    const budget = document.getElementById(
      `decision-subject-budget-${String(FIXTURE_BUDGET_IDS.search)}`,
    );
    expect(campaign?.tagName).toBe('SECTION');
    expect(budget?.tagName).toBe('SECTION');
    if (campaign === null || budget === null) {
      throw new Error('A subject section is missing');
    }
    expect(within(campaign).getAllByRole('table')).toEqual([
      within(campaign).getByRole('table', { name: 'Decisions for Brand Search — US' }),
    ]);
    expect(
      within(budget).getByRole('table', { name: 'Decisions for Search Shared Budget' }),
    ).toBeTruthy();
    const record = buttonNamed('Record decision', campaign);
    const describedBy = record.getAttribute('aria-describedby');
    expect(describedBy).toBe(`${campaign.id}-title`);
    expect(document.getElementById(describedBy ?? '')?.textContent).toBe(
      'Brand Search — US · Campaign · Google',
    );
  });

  it('keeps a campaign and a shared budget with the same id apart', async () => {
    mockApi(
      logHandlers([
        makeDecision('own_budget', { subject: subject({ id: 77, name: 'Campaign 77' }) }),
        makeDecision('budget', {
          subject: subject({ type: 'budget', id: 77, name: 'Shared budget 77' }),
        }),
      ]),
    );

    await renderLog();

    expect(onlyRow(await findTable('Campaign 77'))[4]).toBe('Active · daily budget $200.00');
    expect(onlyRow(await findTable('Shared budget 77'))[4]).toBe('daily budget $500.00');
    expect(document.getElementById('decision-subject-campaign-77')).not.toBeNull();
    expect(document.getElementById('decision-subject-budget-77')).not.toBeNull();
  });

  it('names a subject the API could not name, and leaves out an unknown source', async () => {
    mockApi(
      logHandlers([
        makeDecision('own_budget', { subject: subject({ id: 309, name: null, source: null }) }),
        makeDecision('budget', {
          subject: subject({ type: 'budget', id: FIXTURE_BUDGET_IDS.display, name: '  ' }),
        }),
      ]),
    );

    await renderLog();

    await findTable('Unnamed campaign (id 309)');
    await findTable(`Unnamed shared budget (id ${String(FIXTURE_BUDGET_IDS.display)})`);
    expect(
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent),
    ).toEqual([
      'Unnamed campaign (id 309) · Campaign',
      'Unnamed shared budget (id 401) · Shared budget · Google',
    ]);
  });

  it('labels the six columns in their fixed order', async () => {
    mockApi(logHandlers(defaultLog()));

    await renderLog();

    const table = await findTable('Brand Search — US');
    const headers = within(table)
      .getAllByRole('columnheader')
      .map((header) => header.textContent);
    expect(headers).toEqual(COLUMN_HEADERS);
    expect(within(table).getAllByRole('rowheader')).toHaveLength(2);
  });

  it('renders the origin, author, patch and state of baseline and user rows', async () => {
    mockApi(logHandlers(defaultLog()));

    await renderLog();

    expect(bodyRows(await findTable('Brand Search — US'))).toEqual([
      [
        'Oct 3, 2026, 9:30 AM UTC',
        'User',
        'admin@example.com',
        'daily budget: $240.00',
        'Active · daily budget $240.00',
        'Raised after the B1 card',
      ],
      [
        'Oct 1, 2026, 8:12 AM UTC',
        'Baseline',
        'System',
        'Platform state at first sync',
        'Active · daily budget $200.00',
        '—',
      ],
    ]);
    expect(onlyRow(await findTable('Search Partners — US'))).toEqual([
      'Oct 1, 2026, 8:12 AM UTC',
      'Baseline',
      'System',
      'Platform state at first sync',
      'Active · shared budget Search Shared Budget',
      '—',
    ]);
    expect(onlyRow(await findTable('Search Shared Budget'))).toEqual([
      'Oct 1, 2026, 8:12 AM UTC',
      'Baseline',
      'System',
      'Platform state at first sync',
      'daily budget $500.00',
      '—',
    ]);
  });

  it('formats instants in the default run time zone', async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun({ display_timezone: 'America/New_York' })),
      [LOG_KEY]: json(200, [USER_BUDGET_DECISION]),
    });

    await renderLog();

    const table = await findTable('Brand Search — US');
    await waitFor(() => {
      expect(onlyRow(table)[0]).toBe('Oct 3, 2026, 5:30 AM America/New_York');
    });
  });

  it('formats instants in UTC, so labelled, while no default run exists', async () => {
    mockApi({
      'GET /runs/latest': apiError(404, 'not_found', 'No analysis run yet'),
      [LOG_KEY]: json(200, [USER_BUDGET_DECISION]),
    });

    await renderLog();

    expect(onlyRow(await findTable('Brand Search — US'))[0]).toBe('Oct 3, 2026, 9:30 AM UTC');
  });
});

// ---------------------------------------------------------------------------------------------
// Cells by origin, patch and shape
// ---------------------------------------------------------------------------------------------

/** One decision of its own subject, the columns checked, and their expected text. */
type CellCase = readonly [
  description: string,
  decision: Decision,
  cells: Readonly<Partial<Record<1 | 2 | 3 | 4 | 5, string>>>,
];

/** A stored patch the contract does not describe, as `jsonb` may hold it. */
const MALFORMED_PATCH = { status: '  ', daily_budget: 120 } as unknown as DecisionPatchView;

const CELLS: readonly CellCase[] = [
  [
    'a decision recorded from a recommendation',
    makeDecision('own_budget', {
      origin: 'recommendation',
      recommendation_id: 1102,
      decided_by: 'admin@example.com',
      status: 'paused',
      patch: { status: 'paused' },
    }),
    { 1: 'Recommendation', 3: 'status: paused', 4: 'Paused · daily budget $200.00' },
  ],
  [
    'a row carrying a recommendation id whatever its stored origin',
    makeDecision('own_budget', { origin: 'user', recommendation_id: 1201 }),
    { 1: 'Recommendation' },
  ],
  [
    'a resolution row, whose patch is empty',
    makeDecision('own_budget', { origin: 'resolution', decided_by: 'admin@example.com' }),
    { 1: 'Resolution', 2: 'admin@example.com', 3: '—' },
  ],
  [
    'a patch of several fields, in the order status, daily budget, total budget',
    makeDecision('own_budget', {
      origin: 'user',
      status: 'paused',
      daily_budget: '120.00',
      total_budget: '4500.00',
      patch: { total_budget: '4500', daily_budget: '120', status: 'paused' },
    }),
    {
      3: 'status: paused; daily budget: $120.00; total budget: $4,500.00',
      4: 'Paused · daily budget $120.00 · total budget $4,500.00',
    },
  ],
  [
    'a stored patch with no field the contract reads as set',
    makeDecision('own_budget', { origin: 'user', patch: MALFORMED_PATCH }),
    { 3: '—' },
  ],
  [
    'a baseline whose stored patch is not an object',
    makeDecision('own_budget', { patch: [] as unknown as DecisionPatchView }),
    { 3: 'Platform state at first sync' },
  ],
  [
    'an acknowledged removal of a campaign',
    makeDecision('own_budget', { origin: 'resolution', tombstone: true, status: 'ended' }),
    { 4: 'Removal acknowledged · Ended' },
  ],
  [
    'an acknowledged removal of a campaign whose status is not stored',
    makeDecision('own_budget', { origin: 'resolution', tombstone: true, status: null }),
    { 4: 'Removal acknowledged · Ended' },
  ],
  [
    'an acknowledged removal of a shared budget',
    makeDecision('budget', { origin: 'resolution', tombstone: true, daily_budget: null }),
    { 4: 'Removal acknowledged' },
  ],
  [
    'a shared budget with no amount recorded',
    makeDecision('budget', { daily_budget: null }),
    { 4: 'no amount recorded' },
  ],
  [
    'a shared budget on a custom period',
    makeDecision('budget', { daily_budget: null, total_budget: '9000.00' }),
    { 4: 'total budget $9,000.00' },
  ],
  [
    'an own-budget row without a status',
    makeDecision('own_budget', { status: null }),
    { 4: 'status not reported · daily budget $200.00' },
  ],
  [
    'a shared member whose budget no row of the log names',
    makeDecision('shared_member'),
    { 4: 'Active · shared budget id 402' },
  ],
  [
    'a shared member with no budget assignment',
    makeDecision('shared_member', { budget_id: null }),
    { 4: 'Active' },
  ],
  [
    'a blank author and a blank note',
    makeDecision('own_budget', { origin: 'user', decided_by: ' ', note: '  ' }),
    { 2: 'System', 5: '—' },
  ],
  [
    'amounts in the currency of a LinkedIn EUR campaign',
    makeDecision('own_budget', {
      subject: subject({
        id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
        name: 'Lead Gen — Pipeline Report EMEA',
        source: 'linkedin',
        currency: 'EUR',
      }),
      origin: 'user',
      daily_budget: '100.00',
      total_budget: '3000.00',
      patch: { daily_budget: '100.00' },
    }),
    { 3: 'daily budget: €100.00', 4: 'Active · daily budget €100.00 · total budget €3,000.00' },
  ],
  [
    'a status and daily budget patch on a LinkedIn EUR campaign',
    makeDecision('own_budget', {
      subject: subject({
        id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
        name: 'Lead Gen — Pipeline Report EMEA',
        source: 'linkedin',
        currency: 'EUR',
      }),
      origin: 'user',
      decided_by: 'admin@example.com',
      status: 'paused',
      daily_budget: '120.00',
      patch: { status: 'paused', daily_budget: '120.00' },
    }),
    {
      1: 'User',
      3: 'status: paused; daily budget: €120.00',
      4: 'Paused · daily budget €120.00',
    },
  ],
];

describe('DecisionLog cells', () => {
  it.each(CELLS)('renders %s', async (_description, decision, cells) => {
    mockApi(logHandlers([decision]));

    await renderLog();

    const row = onlyRow(await findTable(decision.subject.name ?? ''));
    // Keyed by column header, so a mismatch names the column it is in.
    const header = (column: string): string => COLUMN_HEADERS[Number(column)] ?? column;
    const shown = Object.keys(cells).map((column) => [header(column), row[Number(column)]]);
    const expected = Object.entries(cells).map(([column, text]) => [header(column), text]);
    expect(Object.fromEntries(shown)).toEqual(Object.fromEntries(expected));
  });

  it("names a member's shared budget without the API's own prefix", async () => {
    mockApi(
      logHandlers([
        makeDecision('shared_member'),
        makeDecision('budget', {
          subject: subject({
            type: 'budget',
            id: FIXTURE_BUDGET_IDS.search,
            name: 'Shared budget 31000402',
          }),
        }),
      ]),
    );

    await renderLog();

    expect(onlyRow(await findTable('Search Partners — US'))[4]).toBe(
      'Active · shared budget 31000402',
    );
  });
});

// ---------------------------------------------------------------------------------------------
// Record decision
// ---------------------------------------------------------------------------------------------

describe('DecisionLog "Record decision"', () => {
  /** The handler key of the decision form's read of the own-budget campaign. */
  const FORM_KEY = formKey('campaign', FIXTURE_CAMPAIGN_IDS.brandSearch);

  /** The section title of the LinkedIn EUR campaign, which a failure banner names. */
  const EMEA_TITLE = 'Lead Gen — Pipeline Report EMEA · Campaign · LinkedIn';

  /** The baseline of the LinkedIn EUR campaign the save-state tests record on. */
  function emeaDecision(): Decision {
    return makeDecision('own_budget', {
      subject: subject({
        id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
        name: 'Lead Gen — Pipeline Report EMEA',
        source: 'linkedin',
        currency: 'EUR',
      }),
      daily_budget: '100.00',
    });
  }

  /** The handlers of a log holding the EMEA campaign and the shared budget, and their forms. */
  function twoSubjectHandlers(post: MockHandlers[string]): MockHandlers {
    const emea = emeaDecision();
    const shared = makeDecision('budget');
    return {
      ...logHandlers([emea, shared]),
      [formKey('campaign', emea.subject.id)]: json(200, [emea]),
      [formKey('budget', shared.subject.id)]: json(200, [shared]),
      'POST /decisions': post,
    };
  }

  /** The "Record decision" action of a subject's section. */
  function recordButton(type: SubjectType, id: number): HTMLButtonElement {
    const section = document.getElementById(`decision-subject-${type}-${String(id)}`);
    if (section === null) {
      throw new Error(`No section for ${type} ${String(id)}`);
    }
    return buttonNamed('Record decision', section);
  }

  /** The EMEA campaign's "Record decision". */
  function emeaRecord(): HTMLButtonElement {
    return recordButton('campaign', FIXTURE_CAMPAIGN_IDS.pipelineEmea);
  }

  /** The shared budget's "Record decision". */
  function budgetRecord(): HTMLButtonElement {
    return recordButton('budget', FIXTURE_BUDGET_IDS.search);
  }

  /** Every `POST` the stub received. */
  function posts(api: ApiMock): RecordedCall[] {
    return api.calls.filter((call) => call.method === 'POST');
  }

  /**
   * Opens the EMEA campaign's dialog, as a click does in a browser (focus on the opener first),
   * and saves a pause.
   *
   * @returns The open dialog.
   */
  async function saveEmeaPause(api: ApiMock): Promise<HTMLElement> {
    const record = emeaRecord();
    record.focus();
    fireEvent.click(record);
    const dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    fireEvent.change(await within(dialog).findByLabelText('Status'), {
      target: { value: 'paused' },
    });
    const before = posts(api).length;
    fireEvent.click(buttonNamed('Save decision', dialog));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(before + 1);
    });
    return dialog;
  }

  /** Saves a pause on the EMEA campaign, then closes its dialog while the save runs. */
  async function saveEmeaPauseThenClose(api: ApiMock): Promise<void> {
    const dialog = await saveEmeaPause(api);
    fireEvent.click(buttonNamed('Close', dialog));
    expect(screen.queryByRole('dialog')).toBeNull();
  }

  /** A failure banner the log's region shows, found by its message, and its focus target. */
  async function findFailure(message: string): Promise<HTMLElement> {
    const text = await within(logRegion()).findByText(message);
    const target = text.closest('[tabindex="-1"]');
    if (!(target instanceof HTMLElement)) {
      throw new Error(`The failure "${message}" has no focus target`);
    }
    return target;
  }

  it('records a patch from the dialog, and the new row then leads its subject', async () => {
    const paused = makeDecision('own_budget', {
      id: 820,
      origin: 'user',
      decided_by: 'admin@example.com',
      decided_at: '2026-10-06T10:00:00Z',
      status: 'paused',
      daily_budget: '240.00',
      patch: { status: 'paused' },
    });
    let saved = false;
    const current = (): Decision[] => (saved ? [paused, ...defaultLog()] : defaultLog());
    const api = mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LOG_KEY]: () => json(200, current()),
      [FORM_KEY]: () =>
        json(
          200,
          current().filter((decision) => decision.subject.id === FIXTURE_CAMPAIGN_IDS.brandSearch),
        ),
      'POST /decisions': () => {
        saved = true;
        return json(201, paused);
      },
    });

    await renderLog();
    await findTable('Brand Search — US');
    const section = document.getElementById(
      `decision-subject-campaign-${String(FIXTURE_CAMPAIGN_IDS.brandSearch)}`,
    );
    if (section === null) {
      throw new Error('The own-budget campaign has no section');
    }
    const trigger = buttonNamed('Record decision', section);
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    expect(within(dialog).getByText('Brand Search — US · Campaign · Google')).toBeTruthy();
    expect(await within(dialog).findByText('In USD')).toBeTruthy();
    fireEvent.change(within(dialog).getByLabelText('Status'), { target: { value: 'paused' } });
    fireEvent.click(buttonNamed('Save decision', dialog));

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    const post = api.calls.find((call) => call.method === 'POST');
    expect(post?.path).toBe('/decisions');
    expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(post?.body).toEqual({
      subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.brandSearch },
      patch: { status: 'paused' },
    });
    const ownTable = (): HTMLElement =>
      screen.getByRole('table', { name: 'Decisions for Brand Search — US' });
    await waitFor(() => {
      expect(bodyRows(ownTable())).toHaveLength(3);
    });
    const [first] = bodyRows(ownTable());
    expect(first?.slice(1, 5)).toEqual([
      'User',
      'admin@example.com',
      'status: paused',
      'Paused · daily budget $240.00',
    ]);
    // The form's own status left with the dialog; the log's region announces the save, and the
    // dialog returned focus to the action that opened it.
    expect(logRegion().textContent).toBe('Decision recorded');
    expect(document.activeElement).toBe(trigger);

    fireEvent.click(buttonNamed('Record decision', section));
    await screen.findByRole('dialog', { name: 'Record decision' });
    expect(logRegion().textContent).toBe('');
  });

  it('closes on Cancel without posting', async () => {
    const api = mockApi({
      ...logHandlers(defaultLog()),
      [FORM_KEY]: json(200, [USER_BUDGET_DECISION, makeDecision('own_budget')]),
    });

    await renderLog();
    await findTable('Brand Search — US');
    const [first] = screen.getAllByRole('button', { name: 'Record decision' });
    if (first === undefined) {
      throw new Error('No "Record decision" action is rendered');
    }
    fireEvent.click(first);

    const dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.calls.some((call) => call.method === 'POST')).toBe(false);
    expect(logRegion().textContent).toBe('');
  });

  it("opens the form in the subject's own currency, with the fields of its shape", async () => {
    const emea = makeDecision('own_budget', {
      subject: subject({
        id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
        name: 'Lead Gen — Pipeline Report EMEA',
        source: 'linkedin',
        currency: 'EUR',
      }),
      daily_budget: '100.00',
    });
    const shared = makeDecision('budget');
    const api = mockApi({
      ...logHandlers([emea, shared]),
      [formKey('campaign', emea.subject.id)]: json(200, [emea]),
      [formKey('budget', shared.subject.id)]: json(200, [shared]),
    });

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    const [linkedin, budget] = screen.getAllByRole('button', { name: 'Record decision' });
    if (linkedin === undefined || budget === undefined) {
      throw new Error('A "Record decision" action is missing');
    }

    fireEvent.click(linkedin);
    let dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    expect(await within(dialog).findByText('In EUR')).toBeTruthy();
    expect(within(dialog).getByLabelText('Status')).toBeTruthy();
    expect(within(dialog).getByLabelText('Daily budget')).toBeTruthy();
    fireEvent.click(buttonNamed('Close', dialog));
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(budget);
    dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    expect(within(dialog).getByText('Search Shared Budget · Shared budget · Google')).toBeTruthy();
    expect(await within(dialog).findByLabelText('Daily budget')).toBeTruthy();
    expect(within(dialog).queryByLabelText('Status')).toBeNull();

    const formReads = logCalls(api).filter((call) => call.search.has('subject_type'));
    expect(formReads.map((call) => call.search.toString())).toEqual([
      `subject_type=campaign&subject_id=${String(FIXTURE_CAMPAIGN_IDS.pipelineEmea)}`,
      `subject_type=budget&subject_id=${String(FIXTURE_BUDGET_IDS.search)}`,
    ]);
  });

  it("keeps another subject's dialog open when a save ends after its own dialog closed", async () => {
    const emea = makeDecision('own_budget', {
      subject: subject({
        id: FIXTURE_CAMPAIGN_IDS.pipelineEmea,
        name: 'Lead Gen — Pipeline Report EMEA',
        source: 'linkedin',
        currency: 'EUR',
      }),
      daily_budget: '100.00',
    });
    const shared = makeDecision('budget');
    const save = deferred<MockResponse>();
    const api = mockApi({
      ...logHandlers([emea, shared]),
      [formKey('campaign', emea.subject.id)]: json(200, [emea]),
      [formKey('budget', shared.subject.id)]: json(200, [shared]),
      'POST /decisions': () => save.promise,
    });

    const { queryClient } = await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    const [linkedin, budget] = screen.getAllByRole('button', { name: 'Record decision' });
    if (linkedin === undefined || budget === undefined) {
      throw new Error('A "Record decision" action is missing');
    }

    fireEvent.click(linkedin);
    const first = await screen.findByRole('dialog', { name: 'Record decision' });
    fireEvent.change(await within(first).findByLabelText('Status'), {
      target: { value: 'paused' },
    });
    fireEvent.click(buttonNamed('Save decision', first));
    await waitFor(() => {
      expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
    });
    // The first dialog closes while its save runs: its subject stays busy, and the shared
    // budget's action stays usable and opens its dialog.
    fireEvent.click(buttonNamed('Close', first));
    expect(linkedin.getAttribute('aria-busy')).toBe('true');
    expect(linkedin).toHaveProperty('disabled', true);
    expect(budget.getAttribute('aria-busy')).toBeNull();
    expect(budget).toHaveProperty('disabled', false);
    fireEvent.click(budget);
    const second = await screen.findByRole('dialog', { name: 'Record decision' });
    const amount = await within(second).findByLabelText('Daily budget');
    fireEvent.change(amount, { target: { value: '275.00' } });

    const unfilteredReads = () => logCalls(api).filter((call) => !call.search.has('subject_type'));
    const readsBefore = unfilteredReads().length;
    save.resolve(json(201, { ...emea, id: 830, origin: 'user', status: 'paused' }));

    // The form's save settles only once its onSuccess has awaited the invalidation, that is once
    // every active decisions read, the log and the open dialog's own filtered read, has read
    // again. The form then reports onPendingChange(false) and onDone, which free the subject
    // and announce the save. Wait for that outcome, with no read or save left in flight.
    await waitFor(() => {
      expect(unfilteredReads().length).toBeGreaterThan(readsBefore);
      expect(logRegion().textContent).toBe('Decision recorded');
      expect(linkedin.getAttribute('aria-busy')).toBeNull();
      expect(queryClient.isFetching()).toBe(0);
      expect(queryClient.isMutating()).toBe(0);
    });
    // TanStack Query hands each settled read to React from a zero-delay timer queued when the
    // read settled, and timers of equal delay run in the order they were created. This later
    // one therefore runs after all of them, and act renders their updates and runs the effects
    // those renders schedule before it returns. Nothing left can close, reset or refocus the
    // open dialog, so the assertions below are final.
    await act(async () => {
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
    expect(screen.getByRole('dialog', { name: 'Record decision' })).toBe(second);
    expect(
      within(second).getByText('Search Shared Budget · Shared budget · Google'),
    ).toBeTruthy();
    expect((within(second).getByLabelText('Daily budget') as HTMLInputElement).value).toBe(
      '275.00',
    );
    // The late save is announced and frees its subject; the open dialog keeps focus.
    expect(logRegion().textContent).toBe('Decision recorded');
    expect(linkedin.getAttribute('aria-busy')).toBeNull();
    expect(second.contains(document.activeElement)).toBe(true);
  });

  it('keeps a subject busy while its save runs after its dialog closed, so it cannot reopen', async () => {
    const emea = emeaDecision();
    const saved: Decision = { ...emea, id: 830, origin: 'user', status: 'paused' };
    const firstSave = deferred<MockResponse>();
    let saves = 0;
    const api = mockApi({
      ...logHandlers([emea]),
      [formKey('campaign', emea.subject.id)]: json(200, [emea]),
      // The first save waits for the test; any later one is answered at once.
      'POST /decisions': () => {
        saves += 1;
        return saves === 1 ? firstSave.promise : json(201, saved);
      },
    });

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    const record = emeaRecord();
    const noteText = 'Paused for the EMEA quarter close';
    // A click focuses the button in a browser; fireEvent does not.
    record.focus();
    fireEvent.click(record);
    const first = await screen.findByRole('dialog', { name: 'Record decision' });
    fireEvent.change(await within(first).findByLabelText('Status'), {
      target: { value: 'paused' },
    });
    fireEvent.change(within(first).getByLabelText('Note'), { target: { value: noteText } });
    fireEvent.click(buttonNamed('Save decision', first));
    await waitFor(() => {
      expect(posts(api)).toHaveLength(1);
    });

    // The dialog closes while its save runs. Until the save settles the action shows its
    // loading state, and nothing reopens the dialog or posts again.
    fireEvent.click(buttonNamed('Close', first));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(record.getAttribute('aria-busy')).toBe('true');
    expect(record.disabled).toBe(true);
    fireEvent.click(record);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(posts(api)).toHaveLength(1);

    firstSave.resolve(json(201, saved));

    // The late save is announced, frees the action and, as the closed dialog could not return
    // focus to the disabled button, puts focus back on it.
    await waitFor(() => {
      expect(logRegion().textContent).toBe('Decision recorded');
    });
    await waitFor(() => {
      expect(document.activeElement).toBe(record);
    });
    expect(record.getAttribute('aria-busy')).toBeNull();
    expect(record.disabled).toBe(false);

    // Reopening now starts a fresh form, without the closed one's note, whose save closes it.
    fireEvent.click(record);
    const second = await screen.findByRole('dialog', { name: 'Record decision' });
    expect(logRegion().textContent).toBe('');
    expect((within(second).getByLabelText('Note') as HTMLTextAreaElement).value).toBe('');
    fireEvent.change(await within(second).findByLabelText('Status'), {
      target: { value: 'ended' },
    });
    fireEvent.click(buttonNamed('Save decision', second));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    expect(posts(api).map((call) => call.body)).toEqual([
      {
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { status: 'paused' },
        note: noteText,
      },
      {
        subject: { type: 'campaign', id: FIXTURE_CAMPAIGN_IDS.pipelineEmea },
        patch: { status: 'ended' },
      },
    ]);
  });

  it.each([
    [
      'a 404',
      apiError(404, 'not_found', 'Campaign 501 was not found.'),
      'Campaign 501 was not found.',
    ],
    [
      'a 409 platform_state_unknown',
      apiError(409, 'platform_state_unknown', 'No entity sync has reported this campaign yet.'),
      'No entity sync has reported this campaign yet.',
    ],
    [
      'a 422',
      apiError(422, 'invalid_patch', 'The patch changes nothing.'),
      'The patch changes nothing.',
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
    'shows %s that ends after its dialog closed in the region, naming the subject, focused',
    async (_failure, response, message) => {
      const save = deferred<MockResponse>();
      const api = mockApi(twoSubjectHandlers(() => save.promise));

      await renderLog();
      await findTable('Lead Gen — Pipeline Report EMEA');
      await saveEmeaPauseThenClose(api);

      save.resolve(response);

      // The form that would have shown the failure is gone, so the log's region shows it,
      // naming the subject, and focus, lost with the closed dialog, moves to it.
      const failure = await findFailure(message);
      expect(within(failure).getByText(`Decision not recorded for ${EMEA_TITLE}`)).toBeTruthy();
      await waitFor(() => {
        expect(document.activeElement).toBe(failure);
      });
      expect(logRegion().contains(failure)).toBe(true);
      // A polite status, never an alert (AAP 0.5.5).
      expect(screen.queryByRole('alert')).toBeNull();
      // The failure frees the action, and nothing was posted twice.
      expect(emeaRecord().getAttribute('aria-busy')).toBeNull();
      expect(emeaRecord().disabled).toBe(false);
      expect(budgetRecord().disabled).toBe(false);
      expect(posts(api)).toHaveLength(1);

      // Reopening the subject clears it; the fresh form shows no earlier failure.
      fireEvent.click(emeaRecord());
      const dialog = await screen.findByRole('dialog', { name: 'Record decision' });
      await within(dialog).findByLabelText('Status');
      expect(within(logRegion()).queryByText(message)).toBeNull();
      expect(within(dialog).queryByText(message)).toBeNull();
    },
  );

  it('shows a failure only in its open form, and clears it when a retry succeeds', async () => {
    const message = 'The patch changes nothing.';
    let saves = 0;
    mockApi(
      twoSubjectHandlers(() => {
        saves += 1;
        return saves === 1
          ? apiError(422, 'invalid_patch', message)
          : json(201, { ...emeaDecision(), id: 830, origin: 'user', status: 'paused' });
      }),
    );

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    fireEvent.click(emeaRecord());
    const dialog = await screen.findByRole('dialog', { name: 'Record decision' });
    fireEvent.change(await within(dialog).findByLabelText('Status'), {
      target: { value: 'paused' },
    });
    fireEvent.click(buttonNamed('Save decision', dialog));

    // The open form shows its failure; the log's region does not repeat it.
    expect(await within(dialog).findByText(message)).toBeTruthy();
    expect(within(logRegion()).queryByText(message)).toBeNull();

    // A retry from the same dialog succeeds: the dialog closes, and the earlier failure is gone
    // rather than shown in the region once its dialog closed.
    fireEvent.click(buttonNamed('Save decision', dialog));
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).toBeNull();
    });
    expect(logRegion().textContent).toBe('Decision recorded');
  });

  it('keeps a failure seen in its form after closing, leaving focus on the opener', async () => {
    const message = 'The patch changes nothing.';
    const api = mockApi(twoSubjectHandlers(apiError(422, 'invalid_patch', message)));

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    const dialog = await saveEmeaPause(api);
    expect(await within(dialog).findByText(message)).toBeTruthy();
    expect(within(logRegion()).queryByText(message)).toBeNull();

    // Closed after the failure: the dialog returns focus to its opener, which the failure, now
    // shown in the region with its subject, leaves there.
    fireEvent.click(buttonNamed('Close', dialog));
    const failure = await findFailure(message);
    expect(within(failure).getByText(`Decision not recorded for ${EMEA_TITLE}`)).toBeTruthy();
    expect(document.activeElement).toBe(emeaRecord());

    // Reopening clears it.
    fireEvent.click(emeaRecord());
    await screen.findByRole('dialog', { name: 'Record decision' });
    expect(within(logRegion()).queryByText(message)).toBeNull();
  });

  it("shows a closed dialog's failure while another subject's dialog keeps focus", async () => {
    const message = 'Too many requests. Try again shortly.';
    const save = deferred<MockResponse>();
    const api = mockApi(twoSubjectHandlers(() => save.promise));

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    await saveEmeaPauseThenClose(api);
    const budget = budgetRecord();
    budget.focus();
    fireEvent.click(budget);
    const second = await screen.findByRole('dialog', { name: 'Record decision' });
    const amount = await within(second).findByLabelText('Daily budget');
    fireEvent.change(amount, { target: { value: '275.00' } });

    save.resolve(apiError(429, 'rate_limited', message));

    // The failure is shown in the region, naming its subject, while the open dialog keeps its
    // edits and its focus.
    const failure = await findFailure(message);
    expect(within(failure).getByText(`Decision not recorded for ${EMEA_TITLE}`)).toBeTruthy();
    expect(screen.getByRole('dialog', { name: 'Record decision' })).toBe(second);
    expect((amount as HTMLInputElement).value).toBe('275.00');
    expect(second.contains(document.activeElement)).toBe(true);
    expect(emeaRecord().disabled).toBe(false);

    // Closing that dialog returns focus to its own opener; the failure stays.
    fireEvent.click(buttonNamed('Close', second));
    expect(document.activeElement).toBe(budget);
    expect(within(logRegion()).getByText(message)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it("leaves focus where the user moved it when a closed dialog's save fails", async () => {
    const message = 'The patch changes nothing.';
    const save = deferred<MockResponse>();
    const api = mockApi(twoSubjectHandlers(() => save.promise));

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    await saveEmeaPauseThenClose(api);
    const elsewhere = budgetRecord();
    elsewhere.focus();

    save.resolve(apiError(422, 'invalid_patch', message));

    await findFailure(message);
    await waitFor(() => {
      expect(emeaRecord().disabled).toBe(false);
    });
    expect(document.activeElement).toBe(elsewhere);
  });

  it('locks a subject whose save is refused as retired after its dialog closed', async () => {
    const save = deferred<MockResponse>();
    const api = mockApi(twoSubjectHandlers(() => save.promise));

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    await saveEmeaPauseThenClose(api);

    save.resolve(
      apiError(409, 'subject_retired', 'Campaign 501 does not belong to an active dataset.'),
    );

    // The refusal reads as the card's historical message, focused, never as an alert.
    const failure = await findFailure('Computed from replaced data');
    expect(within(failure).getByText(`Decision not recorded for ${EMEA_TITLE}`)).toBeTruthy();
    await waitFor(() => {
      expect(document.activeElement).toBe(failure);
    });
    expect(screen.queryByRole('alert')).toBeNull();

    // The server refuses every later decision on the subject, so its action stays disabled;
    // the other subject's still opens.
    const record = emeaRecord();
    expect(record.disabled).toBe(true);
    expect(record.getAttribute('aria-busy')).toBeNull();
    fireEvent.click(record);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(budgetRecord());
    expect(await screen.findByRole('dialog', { name: 'Record decision' })).toBeTruthy();
    expect(posts(api)).toHaveLength(1);
  });

  it('locks a subject refused as retired in its open dialog, showing it once closed', async () => {
    const api = mockApi(
      twoSubjectHandlers(
        apiError(409, 'subject_retired', 'Campaign 501 does not belong to an active dataset.'),
      ),
    );

    await renderLog();
    await findTable('Lead Gen — Pipeline Report EMEA');
    const dialog = await saveEmeaPause(api);

    // The open form shows the refusal itself; the region does not repeat it.
    expect(await within(dialog).findByText('Computed from replaced data')).toBeTruthy();
    expect(within(logRegion()).queryByText('Computed from replaced data')).toBeNull();

    // Once closed, the dialog cannot return focus to the now disabled opener, so the region's
    // banner takes it.
    fireEvent.click(buttonNamed('Close', dialog));
    const failure = await findFailure('Computed from replaced data');
    await waitFor(() => {
      expect(document.activeElement).toBe(failure);
    });
    expect(emeaRecord().disabled).toBe(true);
    expect(posts(api)).toHaveLength(1);
  });
});
