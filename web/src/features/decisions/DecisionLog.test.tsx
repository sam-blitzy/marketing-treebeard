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
 * - the loading, failed, empty and row-limit states, and a failed refetch that keeps the log;
 * - "Record decision", which opens one page-level dialog hosting `DecisionForm` for that subject
 *   in its currency, closes on save or cancel, and lets the refetched log show the new row first.
 *
 * `../../test/mockApi` is imported first so its `afterEach` check runs after Testing Library's
 * cleanup (Vitest skips later hooks once one throws).
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';

import { queryKeys } from '../../api/queries';
import type { Decision, DecisionPatchView, SubjectType, SubjectView } from '../../api/types';
import {
  FIXTURE_BUDGET_IDS,
  FIXTURE_CAMPAIGN_IDS,
  makeDecision,
  makeRun,
} from '../../test/fixtures';
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

    expect(await screen.findByText('No decisions recorded yet')).toBeTruthy();
    expect(screen.getByText('Baseline decisions appear after the first entity sync.')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it("shows the API's message when the log cannot be loaded", async () => {
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [LOG_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderLog();

    expect(await screen.findByText('Decisions could not be loaded')).toBeTruthy();
    expect(screen.getByText('Database unavailable')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
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
    fireEvent.click(buttonNamed('Record decision', section));

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
});
