/**
 * Component tests for `DecisionsPage.tsx`, the `/decisions` screen (AAP 0.5.5 "Other screens →
 * `/decisions`").
 *
 * They hold the page to its TSDoc contract: one `<h1>` "Decisions", then the "Discrepancies"
 * section (`<h2>`, anchor `#discrepancies`) holding `DiscrepancyResolver`, then the "Decision log"
 * section (`<h2>`, anchor `#decision-log`) holding `DecisionLog`; headings on screen from the
 * first render whatever the API answers, with no level skipped below them; no `<main>` frame of
 * its own; and each child owning its state, so a failed read of one never hides the other.
 *
 * `../../test/mockApi` is imported first so its `afterEach` check runs after Testing Library's
 * cleanup (Vitest skips later hooks once one throws).
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { MockHandlers, MockResponse } from '../../test/mockApi';
import { screen, within } from '@testing-library/react';

import { makeDecision, makeDiscrepancy, makeRun } from '../../test/fixtures';
import { renderWithProviders } from '../../test/render';
import { DecisionsPage } from './DecisionsPage';

/** The handler key of the resolver's first page: the default unresolved states. */
const DISCREPANCIES_KEY = 'GET /discrepancies?state=open&state=awaiting_platform';

/** The handler key of the unfiltered decision log. */
const DECISIONS_KEY = 'GET /decisions';

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

/**
 * The page's handlers: the default run, one open discrepancy and two subjects' decisions.
 *
 * The log carries two subjects (a campaign and a shared budget) rather than one, so the
 * heading-nesting test sees more than one `<h3>` under "Decision log"; each subject still names
 * its own "Decisions for {name}" table.
 */
function pageHandlers(): MockHandlers {
  return {
    'GET /runs/latest': json(200, makeRun()),
    [DISCREPANCIES_KEY]: json(200, [makeDiscrepancy('status')]),
    [DECISIONS_KEY]: json(200, [makeDecision('own_budget'), makeDecision('budget')]),
  };
}

/** A page section by its anchor id. */
function section(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`No section has the id "${id}"`);
  }
  return element;
}

describe('DecisionsPage', () => {
  it('renders the title, then the discrepancies, then the decision log', async () => {
    mockApi(pageHandlers());

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });

    const [title] = screen.getAllByRole('heading', { level: 1 });
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(title?.textContent).toBe('Decisions');
    // WCAG 2.2 SC 2.4.2: the document title names the page.
    expect(document.title).toBe('Decisions · Ads Action Engine');
    expect(
      screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
    ).toEqual(['Discrepancies', 'Decision log']);

    const discrepancies = screen.getByRole('region', { name: 'Discrepancies' });
    const log = screen.getByRole('region', { name: 'Decision log' });
    expect(discrepancies).toBe(section('discrepancies'));
    expect(log).toBe(section('decision-log'));
    expect(discrepancies.getAttribute('aria-labelledby')).toBe('discrepancies-title');
    expect(log.getAttribute('aria-labelledby')).toBe('decision-log-title');
    expect(
      discrepancies.compareDocumentPosition(log) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('places the resolver under "Discrepancies" and the log under "Decision log"', async () => {
    mockApi(pageHandlers());

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });

    const discrepancies = section('discrepancies');
    const log = section('decision-log');
    const card = await within(discrepancies).findByRole('article', { name: 'Brand Search — US' });
    expect(within(card).getByText('Platform shows Paused, last decided Active')).toBeTruthy();
    // A status discrepancy offers exactly its two resolutions, each as a button on its card.
    expect(within(card).getByRole('button', { name: 'Platform is correct' })).toBeTruthy();
    expect(within(card).getByRole('button', { name: 'Decision is correct' })).toBeTruthy();
    expect(within(card).getAllByRole('button')).toHaveLength(2);
    expect(
      await within(log).findByRole('table', { name: 'Decisions for Brand Search — US' }),
    ).toBeTruthy();
    expect(
      within(log).getByRole('table', { name: 'Decisions for Search Shared Budget' }),
    ).toBeTruthy();
    expect(within(log).queryByRole('article')).toBeNull();
    expect(within(discrepancies).queryByRole('table')).toBeNull();
  });

  it('nests resolver cards and log subjects as <h3> under their <h2>, skipping none', async () => {
    mockApi(pageHandlers());

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });
    await within(section('decision-log')).findByRole('table', {
      name: 'Decisions for Brand Search — US',
    });
    await within(section('discrepancies')).findByRole('article');

    const levels = screen
      .getAllByRole('heading')
      .map((heading) => Number(heading.tagName.slice(1)));
    expect(levels).toEqual([1, 2, 3, 2, 3, 3]);
    expect(
      within(section('discrepancies'))
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Brand Search — US']);
    expect(
      within(section('decision-log'))
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual([
      'Brand Search — US · Campaign · Google',
      'Search Shared Budget · Shared budget · Google',
    ]);
  });

  it('shows the title and both headings from the first render, while each loads', async () => {
    const discrepancies = deferred<MockResponse>();
    const decisions = deferred<MockResponse>();
    mockApi({
      'GET /runs/latest': json(200, makeRun()),
      [DISCREPANCIES_KEY]: () => discrepancies.promise,
      [DECISIONS_KEY]: () => decisions.promise,
    });

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });

    expect(screen.getByRole('heading', { level: 1, name: 'Decisions' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Discrepancies' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'Decision log' })).toBeTruthy();
    expect(within(section('discrepancies')).getByText('Loading discrepancies')).toBeTruthy();
    expect(within(section('decision-log')).getByText('Loading decisions')).toBeTruthy();

    discrepancies.resolve(json(200, []));
    decisions.resolve(json(200, []));

    expect(
      await within(section('discrepancies')).findByText('No open discrepancies'),
    ).toBeTruthy();
    expect(
      await within(section('decision-log')).findByText('No decisions recorded yet'),
    ).toBeTruthy();
  });

  it('keeps the decision log when the discrepancy read fails', async () => {
    mockApi({
      ...pageHandlers(),
      [DISCREPANCIES_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });

    expect(
      await within(section('discrepancies')).findByText('Discrepancies could not be loaded'),
    ).toBeTruthy();
    expect(
      await within(section('decision-log')).findByRole('table', {
        name: 'Decisions for Brand Search — US',
      }),
    ).toBeTruthy();
  });

  it('keeps the resolver when the decision read fails', async () => {
    mockApi({
      ...pageHandlers(),
      [DECISIONS_KEY]: apiError(500, 'internal_error', 'Database unavailable'),
    });

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });

    expect(
      await within(section('decision-log')).findByText('Decisions could not be loaded'),
    ).toBeTruthy();
    expect(
      await within(section('discrepancies')).findByRole('article', { name: 'Brand Search — US' }),
    ).toBeTruthy();
  });

  it('adds no frame of its own: no main landmark and no banner', async () => {
    mockApi(pageHandlers());

    await renderWithProviders(<DecisionsPage />, { path: '/decisions' });
    await within(section('discrepancies')).findByRole('article');

    expect(screen.queryByRole('main')).toBeNull();
    expect(screen.queryByRole('banner')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
