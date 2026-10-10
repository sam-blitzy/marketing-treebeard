/**
 * Tests for `src/router.tsx`, the SPA's route tree, auth guard and authenticated app layout
 * (AAP 0.5.5 routes; AAP 0.1.1 "The UI requires a login"; AAP 0.5.3 session endpoints).
 *
 * Each test builds the real app router with `createAppRouter` on a memory history and renders it
 * inside a `QueryClientProvider` of the same {@link createQueryClient}, exactly as `src/main.tsx`
 * does. The network is stubbed with `./test/mockApi`, so the real API client and
 * TanStack Query hooks run, and every request a screen makes must be answered. The cases cover
 * the module's TSDoc contract:
 *
 * - **Auth guard**: a 401 from `GET /auth/me` redirects to `/login` carrying the requested href
 *   (path, search and hash) as `redirect`, and sign-in returns there without asking
 *   `GET /auth/me` again; any other failure shows the error screen with Retry, never the sign-in
 *   form; the user is read once across navigations; a slow guard shows the "Loading" spinner.
 * - **`/`** redirects to `/actions`.
 * - **Layout**: each of the four screens renders inside one `PageShell` with the signed-in email,
 *   "Sign out" and the primary navigation, whose current link carries `aria-current="page"`.
 * - **Code splitting**: the Decisions, Sources and Settings routes have lazy components, Actions
 *   and Login eager ones, and each lazy screen renders inside the layout once its chunk loads.
 * - **`/actions?run=`** keeps only positive safe integers, and a rejected value is removed from the
 *   URL by a replacing redirect; **`/login?redirect=`** keeps only same-origin paths the same way.
 * - **Not found**: an unknown path renders "Page not found" inside the shell when signed in, and
 *   sends a signed-out visitor to sign in.
 * - **Sign out**: success and a 401 clear the cache and open `/login`, so the guard asks the API
 *   again; any other failure keeps the session and explains itself beside the button, in the
 *   polite status region mounted with the layout, and returns focus to the button only when focus
 *   had fallen to `<body>`.
 * - **Focus after navigation**: focus moves to `<main>` only when the focused control was removed.
 */
// mockApi registers an afterEach hook that fails a test with an unmatched request. Vitest skips
// the remaining afterEach hooks once one throws and runs them in reverse order of registration,
// so this import comes before Testing Library's, whose cleanup must still run.
import { apiError, json, mockApi } from './test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from './test/mockApi';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { RenderResult } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { queryKeys } from './api/queries';
import { createAppRouter } from './router';
import type { AppRouter } from './router';
import { makeConfig, makeMe, makeRun, makeSources } from './test/fixtures';

// ---------------------------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------------------------

/**
 * A query client for one test, with the options of the component tests' client. It is built here
 * so the suite takes nothing from `./test/render`, whose memory router stands in for the route
 * tree that these tests exercise through `./router`.
 *
 * - Nothing retries, so a stubbed failure (the guard's 401 or 503, a failed sign-out) settles at
 *   once instead of after the hooks' backoff.
 * - `gcTime: Infinity` starts no garbage-collection timer that could outlive the test.
 * - `staleTime: 0` and no refetch on window focus keep every request one the test caused.
 *
 * @returns A fresh client.
 */
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: 0, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

/** The signed-in admin's email, as `makeMe()` returns it. */
const ADMIN_EMAIL = makeMe().email;

/** A password typed into the sign-in form; the stubbed API accepts any. */
const ADMIN_PASSWORD = 'hunter2';

/** `GET /auth/me` for a valid session. */
const SIGNED_IN: MockHandlers = { 'GET /auth/me': json(200, makeMe()) };

/** `GET /auth/me` without a session: the API's 401 `unauthenticated`. */
const SIGNED_OUT: MockHandlers = {
  'GET /auth/me': apiError(401, 'unauthenticated', 'Not signed in'),
};

/**
 * Every read the four screens make, so any screen the router opens is answered. `/actions` is
 * answered with the first-start 404, its lightest state, which still renders the page's title.
 *
 * @returns Fresh handlers for one test.
 */
function pageReads(): MockHandlers {
  return {
    'GET /recommendations': apiError(404, 'not_found', 'No analysis run yet'),
    'GET /config': json(200, makeConfig()),
    'GET /sources': json(200, makeSources()),
    'GET /runs/latest': json(200, makeRun()),
    'GET /discrepancies': json(200, []),
    'GET /decisions': json(200, []),
  };
}

/** What {@link renderApp} returns: the render result plus the router and its query client. */
interface AppHarness extends RenderResult {
  /** The app router under test, on its own memory history. */
  router: AppRouter;
  /** The client passed to `createAppRouter` and to the `QueryClientProvider`. */
  queryClient: QueryClient;
}

/**
 * Renders the whole app at `path`: `createAppRouter` on a memory history, inside the
 * `QueryClientProvider` of the same client, as `src/main.tsx` renders it.
 *
 * @param path - The initial entry, search and hash included.
 * @returns The harness.
 */
function renderApp(path: string): AppHarness {
  const queryClient = createQueryClient();
  const router = createAppRouter(queryClient, {
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router, queryClient };
}

/**
 * Waits until the router is idle at `pathname`.
 *
 * @param router - The router under test.
 * @param pathname - The expected pathname.
 */
async function waitForPath(router: AppRouter, pathname: string): Promise<void> {
  await waitFor(() => {
    expect(router.state.status).toBe('idle');
    expect(router.state.location.pathname).toBe(pathname);
  });
}

/**
 * Waits for the page title, the page's single `<h1>`.
 *
 * @param name - The title text.
 * @returns The heading.
 */
function findTitle(name: string): Promise<HTMLElement> {
  return screen.findByRole('heading', { level: 1, name });
}

/**
 * The requests recorded for one method and path, in request order.
 *
 * @param api - The mock.
 * @param method - The upper-case method.
 * @param path - The path relative to `/api/v1`, without the query.
 * @returns The matching calls.
 */
function callsTo(api: ApiMock, method: string, path: string): RecordedCall[] {
  return api.calls.filter((call) => call.method === method && call.path === path);
}

/**
 * The primary navigation landmark.
 *
 * @returns The `<nav>` named "Primary".
 */
function primaryNav(): HTMLElement {
  return screen.getByRole('navigation', { name: 'Primary' });
}

/**
 * Fills and submits the sign-in form. The fields are required, so their label text carries an
 * `aria-hidden` asterisk: the email field is found by its accessible name, and the password
 * field, which has no role, by the start of its label text.
 */
function signIn(): void {
  fireEvent.change(screen.getByRole('textbox', { name: 'Email' }), {
    target: { value: ADMIN_EMAIL },
  });
  fireEvent.change(screen.getByLabelText(/^Password/u), { target: { value: ADMIN_PASSWORD } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

/** A `POST /auth/login` that accepts the credentials and returns the admin. */
const LOGIN_OK: MockHandlers = { 'POST /auth/login': json(200, makeMe()) };

// jsdom does not implement `window.scrollTo`, which the router calls to reset the scroll
// position after each navigation and which jsdom would otherwise report as "Not implemented".
// mockApi's afterEach restores it with every other global stubbed through `vi.stubGlobal`.
beforeEach(() => {
  vi.stubGlobal('scrollTo', () => undefined);
});

// ---------------------------------------------------------------------------------------------
// Auth guard
// ---------------------------------------------------------------------------------------------

describe('auth guard', () => {
  it('redirects a signed-out visitor to /login with the requested href and hash', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_OUT });
    const { router } = renderApp('/actions?run=202#section-budget');

    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({
      redirect: '/actions?run=202#section-budget',
    });
    await findTitle('Sign in');
    expect(callsTo(api, 'GET', '/auth/me')).toHaveLength(1);
    // The guard runs before the page, so nothing of the signed-in screen is requested.
    expect(callsTo(api, 'GET', '/recommendations')).toHaveLength(0);
    // The sign-in screen's own navigation lists itself only, never a signed-in screen.
    expect(within(primaryNav()).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Sign in',
    ]);
  });

  it('sends a signed-out visitor at /actions to the sign-in form, keeping /actions', async () => {
    mockApi({ ...pageReads(), ...SIGNED_OUT });
    const { router } = renderApp('/actions');

    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: '/actions' });
    await findTitle('Sign in');
    // Both fields are required, so each label also holds an aria-hidden asterisk: the email
    // field is found by its accessible name, the password field, which has no role, by its label.
    const email = screen.getByRole('textbox', { name: 'Email' });
    expect(email.getAttribute('type')).toBe('email');
    const password = screen.getByLabelText(/^Password/u);
    expect(password.getAttribute('type')).toBe('password');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
  });

  it('returns to the requested page after sign-in without asking GET /auth/me again', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_OUT, ...LOGIN_OK });
    const { router } = renderApp('/settings');

    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: '/settings' });
    await findTitle('Sign in');

    signIn();

    await findTitle('Settings');
    await waitForPath(router, '/settings');
    expect(callsTo(api, 'POST', '/auth/login').map((call) => call.body)).toEqual([
      { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    ]);
    // Only the first, signed-out read: sign-in seeded the user the guard then reads.
    expect(callsTo(api, 'GET', '/auth/me')).toHaveLength(1);
    expect(screen.getByText(ADMIN_EMAIL)).toBeTruthy();
    expect(primaryNav()).toBeTruthy();
  });

  it('returns to a custom run with its search after sign-in', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_OUT, ...LOGIN_OK });
    const { router } = renderApp('/actions?run=202');

    await waitForPath(router, '/login');
    await findTitle('Sign in');
    signIn();

    await findTitle('Actions');
    await waitForPath(router, '/actions');
    expect(router.state.location.search).toEqual({ run: 202 });
    await screen.findByText('Run not found');
    const reads = callsTo(api, 'GET', '/recommendations');
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.every((call) => call.search.get('run_id') === '202')).toBe(true);
  });

  it('shows any other failure on the error screen with Retry, not the sign-in form', async () => {
    const api = mockApi({
      ...pageReads(),
      'GET /auth/me': apiError(503, 'database_unavailable', 'Database unavailable'),
    });
    const { router } = renderApp('/settings');

    await findTitle('Something went wrong');
    expect(screen.getByText('Database unavailable')).toBeTruthy();
    expect(router.state.location.pathname).toBe('/settings');
    expect(screen.queryByRole('heading', { name: 'Sign in' })).toBeNull();
    // Outside the layout the error screen brings its own shell: one <main>, one <h1>, no
    // navigation or sign-out, and no alert role (reserved for open discrepancies).
    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByText('Ads Action Engine')).toBeTruthy();
    expect(screen.queryByRole('navigation', { name: 'Primary' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Sign out' })).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();

    api.use(SIGNED_IN);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    await findTitle('Settings');
    expect(primaryNav()).toBeTruthy();
    expect(screen.queryByText('Database unavailable')).toBeNull();
    expect(callsTo(api, 'GET', '/auth/me')).toHaveLength(2);
  });

  it('reads GET /auth/me once while navigating between screens', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/settings');
    await findTitle('Settings');

    fireEvent.click(within(primaryNav()).getByRole('link', { name: 'Sources' }));
    await findTitle('Sources');
    await waitForPath(router, '/sources');

    fireEvent.click(within(primaryNav()).getByRole('link', { name: 'Actions' }));
    await findTitle('Actions');
    await waitForPath(router, '/actions');

    expect(callsTo(api, 'GET', '/auth/me')).toHaveLength(1);
  });

  it('shows the "Loading" spinner while the guard waits for GET /auth/me', async () => {
    let answer: (response: MockResponse) => void = () => undefined;
    const pending = new Promise<MockResponse>((resolve) => {
      answer = resolve;
    });
    mockApi({ ...pageReads(), 'GET /auth/me': () => pending });
    const { router } = renderApp('/settings');

    const label = await screen.findByText('Loading', {}, { timeout: 4000 });
    expect(label.closest('[role="status"]')).not.toBeNull();
    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.queryByRole('navigation', { name: 'Primary' })).toBeNull();

    answer(json(200, makeMe()));

    await findTitle('Settings');
    await waitForPath(router, '/settings');
    expect(screen.queryByText('Loading')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Index route
// ---------------------------------------------------------------------------------------------

describe('/', () => {
  it('redirects a signed-in user to /actions', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/');

    await waitForPath(router, '/actions');
    await findTitle('Actions');
    expect(router.state.location.search).toEqual({});
  });

  it('sends a signed-out visitor through /actions to sign in', async () => {
    mockApi({ ...pageReads(), ...SIGNED_OUT });
    const { router } = renderApp('/');

    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: '/actions' });
    await findTitle('Sign in');
  });
});

// ---------------------------------------------------------------------------------------------
// Authenticated layout
// ---------------------------------------------------------------------------------------------

describe('authenticated layout', () => {
  /** The primary navigation, in AAP 0.5.5 order: link text, path and page title. */
  const SCREENS: readonly (readonly [string, string, string])[] = [
    ['Actions', '/actions', 'Actions'],
    ['Decisions', '/decisions', 'Decisions'],
    ['Sources', '/sources', 'Sources'],
    ['Settings', '/settings', 'Settings'],
  ];

  it.each(SCREENS)(
    'renders %s inside the shell with the navigation, the email and "Sign out"',
    async (label, path, title) => {
      mockApi({ ...pageReads(), ...SIGNED_IN });
      const { router } = renderApp(path);

      await findTitle(title);
      await waitForPath(router, path);

      expect(screen.getAllByRole('main')).toHaveLength(1);
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
      expect(screen.getByRole('banner').textContent).toContain('Ads Action Engine');
      expect(within(screen.getByRole('banner')).getByText(ADMIN_EMAIL)).toBeTruthy();
      expect(
        within(screen.getByRole('banner')).getByRole('button', { name: 'Sign out' }),
      ).toBeTruthy();

      const links = within(primaryNav()).getAllByRole('link');
      expect(links.map((link) => link.textContent)).toEqual(SCREENS.map(([text]) => text));
      expect(links.map((link) => link.getAttribute('href'))).toEqual(
        SCREENS.map(([, to]) => to),
      );
      const current = links.filter((link) => link.getAttribute('aria-current') === 'page');
      expect(current.map((link) => link.textContent)).toEqual([label]);
      // The page sits in <main>, beside the navigation rather than inside it.
      const main = screen.getByRole('main');
      expect(within(main).getByRole('heading', { level: 1, name: title })).toBeTruthy();
      expect(main.contains(primaryNav())).toBe(false);
    },
  );

  it('keeps Actions current on a custom run, whose search the match ignores', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/actions?run=202');

    await findTitle('Actions');
    await waitForPath(router, '/actions');
    const actions = within(primaryNav()).getByRole('link', { name: 'Actions' });
    expect(actions.getAttribute('aria-current')).toBe('page');
    expect(actions.getAttribute('class')).toContain('ds-sidenav__link--current');
  });

  it('marks the current screen when the path has a trailing slash', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/settings/');

    await findTitle('Settings');
    await waitForPath(router, '/settings/');
    const settings = within(primaryNav()).getByRole('link', { name: 'Settings' });
    expect(settings.getAttribute('aria-current')).toBe('page');
  });

  it('navigates client-side from the navigation and moves the current marker', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/actions');
    await findTitle('Actions');

    fireEvent.click(within(primaryNav()).getByRole('link', { name: 'Decisions' }));

    await findTitle('Decisions');
    await waitForPath(router, '/decisions');
    const nav = within(primaryNav());
    expect(nav.getByRole('link', { name: 'Decisions' }).getAttribute('aria-current')).toBe('page');
    expect(nav.getByRole('link', { name: 'Actions' }).getAttribute('aria-current')).toBeNull();
    // A client-side navigation adds one history entry.
    expect(router.history.length).toBe(2);
  });
});

// ---------------------------------------------------------------------------------------------
// Code splitting
// ---------------------------------------------------------------------------------------------

describe('code splitting', () => {
  it('loads Decisions, Sources and Settings lazily and keeps Actions and Login eager', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/actions');
    await findTitle('Actions');
    const { routesByPath } = router;

    // lazyRouteComponent gives its component a `preload` that loads the chunk, and sets it to
    // undefined once the chunk has loaded. The route tree is module state shared by every test
    // in this file, so the key, present either way, marks a lazy component, not its value.
    const lazy = [routesByPath['/decisions'], routesByPath['/sources'], routesByPath['/settings']];
    for (const route of lazy) {
      expect(route.options.component).toHaveProperty('preload');
    }
    const eager = [routesByPath['/actions'], routesByPath['/login']];
    for (const route of eager) {
      expect(route.options.component).toBeDefined();
      expect(route.options.component).not.toHaveProperty('preload');
    }
  });

  /** The code-split screens: link text, path and page title. */
  const LAZY_SCREENS = [
    ['Decisions', '/decisions', 'Decisions'],
    ['Sources', '/sources', 'Sources'],
    ['Settings', '/settings', 'Settings'],
  ] as const;

  it.each(LAZY_SCREENS)(
    'renders %s inside the layout once its chunk loads after a navigation from /actions',
    async (label, path, title) => {
      mockApi({ ...pageReads(), ...SIGNED_IN });
      const { router } = renderApp('/actions');
      await findTitle('Actions');

      fireEvent.click(within(primaryNav()).getByRole('link', { name: label }));

      // The screen arrives asynchronously, after its chunk.
      await findTitle(title);
      await waitForPath(router, path);
      const main = screen.getByRole('main');
      expect(within(main).getByRole('heading', { level: 1, name: title })).toBeTruthy();
      expect(main.contains(primaryNav())).toBe(false);
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
      expect(
        within(screen.getByRole('banner')).getByRole('button', { name: 'Sign out' }),
      ).toBeTruthy();
      // The chunk has loaded, so the lazy component no longer offers a preload.
      expect(router.routesByPath[path].options.component?.preload).toBeUndefined();
    },
  );
});

// ---------------------------------------------------------------------------------------------
// /actions?run=
// ---------------------------------------------------------------------------------------------

describe('/actions run search', () => {
  const ACCEPTED: readonly (readonly [string, string, number])[] = [
    ['a number', '202', 202],
    ['a quoted string of digits', '%22202%22', 202],
    ['Number.MAX_SAFE_INTEGER', '9007199254740991', Number.MAX_SAFE_INTEGER],
  ];

  it.each(ACCEPTED)('opens the run for %s', async (_case, raw, id) => {
    const api = mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp(`/actions?run=${raw}`);

    await findTitle('Actions');
    // The page reads the addressed run, which the stub does not know.
    await screen.findByText('Run not found');
    await waitForPath(router, '/actions');
    const reads = callsTo(api, 'GET', '/recommendations');
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.every((call) => call.search.get('run_id') === String(id))).toBe(true);
    expect(router.history.length).toBe(1);
  });

  const REJECTED: readonly (readonly [string, string])[] = [
    ['zero', '0'],
    ['a negative number', '-3'],
    ['a fraction', '1.5'],
    ['a word', 'abc'],
    ['digits with a leading zero', '0202'],
    ['a quoted string with a leading zero', '%220202%22'],
    ['a boolean', 'true'],
    ['an empty value', ''],
    ['an integer beyond Number.MAX_SAFE_INTEGER', '9007199254740992'],
  ];

  it.each(REJECTED)(
    'removes %s from the URL with a replacing redirect and shows the default run',
    async (_case, raw) => {
      const api = mockApi({ ...pageReads(), ...SIGNED_IN });
      const { router } = renderApp(`/actions?run=${raw}`);

      await findTitle('Actions');
      await waitForPath(router, '/actions');
      await waitFor(() => {
        expect(router.state.location.href).toBe('/actions');
      });
      expect(router.state.location.search).toEqual({});
      // Replaced, not pushed: Back cannot return to the rejected value.
      expect(router.history.length).toBe(1);
      await screen.findByText('No analysis run yet');
      const reads = callsTo(api, 'GET', '/recommendations');
      expect(reads.length).toBeGreaterThan(0);
      expect(reads.every((call) => !call.search.has('run_id'))).toBe(true);
    },
  );

  it.each(REJECTED)(
    'removes %s that a later history entry carries, replacing that entry',
    async (_case, raw) => {
      const api = mockApi({ ...pageReads(), ...SIGNED_IN });
      const { router } = renderApp('/actions');
      await findTitle('Actions');
      await waitForPath(router, '/actions');

      // The address bar or Back/Forward changes the URL under the running app.
      act(() => {
        router.history.push(`/actions?run=${raw}`);
      });

      await waitFor(() => {
        expect(router.state.status).toBe('idle');
        expect(router.state.location.href).toBe('/actions');
      });
      expect(router.state.location.search).toEqual({});
      // The rejected entry was replaced: the initial entry plus the cleaned one.
      expect(router.history.length).toBe(2);
      expect(screen.getByRole('heading', { level: 1, name: 'Actions' })).toBeTruthy();
      const reads = callsTo(api, 'GET', '/recommendations');
      expect(reads.every((call) => !call.search.has('run_id'))).toBe(true);
    },
  );

  it('opens the run a later history entry addresses', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/actions');
    await screen.findByText('No analysis run yet');

    act(() => {
      router.history.push('/actions?run=202');
    });

    await screen.findByText('Run not found');
    await waitForPath(router, '/actions');
    expect(router.state.location.search).toEqual({ run: 202 });
    expect(callsTo(api, 'GET', '/recommendations').at(-1)?.search.get('run_id')).toBe('202');
  });
});

// ---------------------------------------------------------------------------------------------
// /login?redirect=
// ---------------------------------------------------------------------------------------------

describe('/login redirect search', () => {
  const KEPT: readonly string[] = ['/settings', '/actions?run=202', '/actions#section-budget'];

  it.each(KEPT)('keeps the same-origin path %s', async (target) => {
    mockApi({});
    const { router } = renderApp(`/login?redirect=${encodeURIComponent(target)}`);

    await findTitle('Sign in');
    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: target });
    expect(router.history.length).toBe(1);
  });

  const DROPPED: readonly (readonly [string, string])[] = [
    ['an absolute https: URL', encodeURIComponent('https://evil.example/')],
    ['a javascript: URL', encodeURIComponent('javascript:alert(1)')],
    ['a protocol-relative URL', encodeURIComponent('//evil.example')],
    ['a slash then a backslash', encodeURIComponent('/\\evil.example')],
    ['a relative path', 'actions'],
    ['a tab that browsers strip', encodeURIComponent('/\t/evil.example')],
    ['a newline', encodeURIComponent('/actions\n')],
    ['a DELETE character', encodeURIComponent('/actions\u007f')],
    ['a number', '5'],
  ];

  it.each(DROPPED)('removes %s from the URL with a replacing redirect', async (_case, raw) => {
    mockApi({});
    const { router } = renderApp(`/login?redirect=${raw}`);

    await findTitle('Sign in');
    await waitForPath(router, '/login');
    await waitFor(() => {
      expect(router.state.location.href).toBe('/login');
    });
    expect(router.state.location.search).toEqual({});
    expect(router.history.length).toBe(1);
  });

  it.each(DROPPED)(
    'removes %s that a later history entry carries, replacing that entry',
    async (_case, raw) => {
      mockApi({});
      const { router } = renderApp('/login');
      await findTitle('Sign in');
      await waitForPath(router, '/login');

      act(() => {
        router.history.push(`/login?redirect=${raw}`);
      });

      await waitFor(() => {
        expect(router.state.status).toBe('idle');
        expect(router.state.location.href).toBe('/login');
      });
      expect(router.state.location.search).toEqual({});
      expect(router.history.length).toBe(2);
      expect(screen.getByRole('heading', { level: 1, name: 'Sign in' })).toBeTruthy();
    },
  );

  it('signs in to /actions when the redirect was dropped', async () => {
    mockApi({ ...pageReads(), ...LOGIN_OK });
    const { router } = renderApp(`/login?redirect=${encodeURIComponent('//evil.example')}`);
    await findTitle('Sign in');
    await waitForPath(router, '/login');

    signIn();

    await findTitle('Actions');
    await waitForPath(router, '/actions');
    expect(router.state.location.href).toBe('/actions');
  });

  it('signs in to /actions when /login carries no redirect', async () => {
    mockApi({ ...pageReads(), ...LOGIN_OK });
    const { router } = renderApp('/login');
    await findTitle('Sign in');

    signIn();

    await findTitle('Actions');
    await waitForPath(router, '/actions');
  });
});

// ---------------------------------------------------------------------------------------------
// Not found
// ---------------------------------------------------------------------------------------------

describe('not found', () => {
  it.each(['/no-such-page', '/actions/nope'])(
    'renders "Page not found" at %s inside the shell, with no navigation link current',
    async (path) => {
      mockApi({ ...pageReads(), ...SIGNED_IN });
      const { router } = renderApp(path);

      await findTitle('Page not found');
      await waitForPath(router, path);
      expect(screen.getByText('No page exists at this address.')).toBeTruthy();
      expect(screen.getAllByRole('main')).toHaveLength(1);
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
      expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy();
      const links = within(primaryNav()).getAllByRole('link');
      expect(links).toHaveLength(4);
      expect(links.some((link) => link.hasAttribute('aria-current'))).toBe(false);
      const back = within(screen.getByRole('main')).getByRole('link', { name: 'Go to actions' });
      expect(back.getAttribute('href')).toBe('/actions');
    },
  );

  it('leads back to /actions', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/no-such-page');
    await findTitle('Page not found');

    fireEvent.click(screen.getByRole('link', { name: 'Go to actions' }));

    await findTitle('Actions');
    await waitForPath(router, '/actions');
    expect(screen.queryByText('Page not found')).toBeNull();
  });

  it('sends a signed-out visitor to sign in instead of revealing which paths exist', async () => {
    mockApi({ ...pageReads(), ...SIGNED_OUT });
    const { router } = renderApp('/no-such-page');

    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: '/no-such-page' });
    await findTitle('Sign in');
    expect(screen.queryByText('Page not found')).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// Sign out
// ---------------------------------------------------------------------------------------------

/** A `POST /auth/logout` the test answers by hand, so the request stays pending until then. */
interface HeldLogout {
  /** The handler to register for `POST /auth/logout`. */
  handler: () => Promise<MockResponse>;
  /** Answers the pending request with `response`. */
  release: (response: MockResponse) => void;
}

/**
 * A sign-out request held pending until the test releases it, so the test can move focus while
 * the "Sign out" button is loading.
 *
 * @returns The handler and its release.
 */
function heldLogout(): HeldLogout {
  let release: (response: MockResponse) => void = () => undefined;
  const promise = new Promise<MockResponse>((resolve) => {
    release = resolve;
  });
  return { handler: () => promise, release };
}

/** The 500 of a failed sign-out that keeps the session. */
const LOGOUT_FAILED = apiError(500, 'internal_error', 'Internal error');

/** What the top bar says beside "Sign out" after a failed sign-out. */
const SIGN_OUT_FAILED = 'Sign-out failed. Try again.';

/**
 * The top bar's polite status region, which holds "Sign out" and, after a failure, the message.
 *
 * @returns The `role="status"` element inside the `banner` landmark.
 */
function signOutStatus(): HTMLElement {
  return within(screen.getByRole('banner')).getByRole('status');
}

describe('sign out', () => {
  it('posts /auth/logout with the CSRF header, clears the cache and opens /login', async () => {
    const api = mockApi({
      ...pageReads(),
      ...SIGNED_IN,
      // The stub answers as the API does, 200 with the signed-out `{email}` (routes/auth.py),
      // not with a 204: the test exercises the response the client really receives.
      'POST /auth/logout': json(200, makeMe()),
    });
    const { router, queryClient } = renderApp('/settings');
    await findTitle('Settings');
    expect(queryClient.getQueryData(queryKeys.me)).toEqual(makeMe());

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));

    await findTitle('Sign in');
    await waitForPath(router, '/login');
    const posts = callsTo(api, 'POST', '/auth/logout');
    expect(posts).toHaveLength(1);
    expect(posts[0]?.headers.get('X-Requested-With')).toBe('ads-engine');
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
    // The sign-in screen's own navigation lists itself only, never a signed-in screen.
    expect(within(primaryNav()).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Sign in',
    ]);

    // The cleared cache cannot let the guard pass: it asks the API, which says signed out.
    api.use(SIGNED_OUT);
    await act(() => router.navigate({ to: '/actions' }));
    await waitForPath(router, '/login');
    expect(router.state.location.search).toEqual({ redirect: '/actions' });
    expect(callsTo(api, 'GET', '/auth/me')).toHaveLength(2);
  });

  it('treats a 401 from /auth/logout as signed out: the session had already ended', async () => {
    mockApi({
      ...pageReads(),
      ...SIGNED_IN,
      'POST /auth/logout': apiError(401, 'unauthenticated', 'Not signed in'),
    });
    const { router, queryClient } = renderApp('/settings');
    await findTitle('Settings');

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));

    await findTitle('Sign in');
    await waitForPath(router, '/login');
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
    expect(screen.queryByText('Sign-out failed. Try again.')).toBeNull();
  });

  it('keeps the session and explains a failed sign-out beside the button', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_IN, 'POST /auth/logout': LOGOUT_FAILED });
    const { router, queryClient } = renderApp('/settings');
    await findTitle('Settings');
    const button = screen.getByRole('button', { name: 'Sign out' });
    expect(button.hasAttribute('aria-describedby')).toBe(false);
    // The polite region is mounted before any failure, holding the button and no message.
    const status = signOutStatus();
    expect(status.getAttribute('aria-live')).toBe('polite');
    expect(status.getAttribute('aria-atomic')).toBe('false');
    expect(status.contains(button)).toBe(true);
    expect(status.textContent).toBe('Sign out');

    fireEvent.click(button);

    const message = await screen.findByText(SIGN_OUT_FAILED);
    expect(router.state.location.pathname).toBe('/settings');
    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeTruthy();
    expect(queryClient.getQueryData(queryKeys.me)).toEqual(makeMe());
    const signOut = screen.getByRole('button', { name: 'Sign out' });
    // The message lands in the region that already existed, beside the same button.
    expect(signOutStatus()).toBe(status);
    expect(status.contains(message)).toBe(true);
    expect(signOut).toBe(button);
    expect(message.id).not.toBe('');
    expect(signOut.getAttribute('aria-describedby')).toBe(message.id);
    await waitFor(() => {
      expect(signOut.hasAttribute('disabled')).toBe(false);
    });
    expect(screen.queryByRole('alert')).toBeNull();

    // A second attempt that succeeds signs out.
    api.use({ 'POST /auth/logout': json(200, makeMe()) });
    fireEvent.click(signOut);
    await findTitle('Sign in');
    await waitForPath(router, '/login');
    expect(callsTo(api, 'POST', '/auth/logout')).toHaveLength(2);
  });

  it('clears the message while a retry is pending, so a repeated failure is new content', async () => {
    const api = mockApi({ ...pageReads(), ...SIGNED_IN, 'POST /auth/logout': LOGOUT_FAILED });
    renderApp('/settings');
    await findTitle('Settings');
    const status = signOutStatus();
    const button = screen.getByRole('button', { name: 'Sign out' });
    fireEvent.click(button);
    await screen.findByText(SIGN_OUT_FAILED);
    await waitFor(() => {
      expect(button.hasAttribute('disabled')).toBe(false);
    });

    const retry = heldLogout();
    api.use({ 'POST /auth/logout': retry.handler });
    fireEvent.click(button);

    await waitFor(() => {
      expect(button.hasAttribute('disabled')).toBe(true);
    });
    expect(screen.queryByText(SIGN_OUT_FAILED)).toBeNull();
    expect(button.hasAttribute('aria-describedby')).toBe(false);

    retry.release(LOGOUT_FAILED);

    const message = await screen.findByText(SIGN_OUT_FAILED);
    expect(signOutStatus()).toBe(status);
    expect(status.contains(message)).toBe(true);
    expect(button.getAttribute('aria-describedby')).toBe(message.id);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(callsTo(api, 'POST', '/auth/logout')).toHaveLength(2);
  });

  it('returns focus to "Sign out" when it fell to <body> while the request was pending', async () => {
    const held = heldLogout();
    mockApi({ ...pageReads(), ...SIGNED_IN, 'POST /auth/logout': held.handler });
    const { router } = renderApp('/settings');
    await findTitle('Settings');
    const button = screen.getByRole('button', { name: 'Sign out' });
    button.focus();
    fireEvent.click(button);
    await waitFor(() => {
      expect(button.hasAttribute('disabled')).toBe(true);
    });
    // A browser moves focus off a control that becomes disabled; jsdom keeps it there and
    // ignores blur() on it, so focus passes through <main> (tabindex="-1") to reach <body>.
    const main = screen.getByRole('main');
    main.focus();
    main.blur();
    expect(document.activeElement).toBe(document.body);

    held.release(LOGOUT_FAILED);

    await screen.findByText(SIGN_OUT_FAILED);
    await waitFor(() => {
      expect(document.activeElement).toBe(button);
    });
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(router.state.location.pathname).toBe('/settings');
  });

  it('leaves focus on the control the user moved to while the request was pending', async () => {
    const held = heldLogout();
    mockApi({ ...pageReads(), ...SIGNED_IN, 'POST /auth/logout': held.handler });
    const { router } = renderApp('/settings');
    await findTitle('Settings');
    const button = screen.getByRole('button', { name: 'Sign out' });
    button.focus();
    fireEvent.click(button);
    await waitFor(() => {
      expect(button.hasAttribute('disabled')).toBe(true);
    });
    const link = within(primaryNav()).getByRole('link', { name: 'Decisions' });
    link.focus();

    held.release(LOGOUT_FAILED);

    await screen.findByText(SIGN_OUT_FAILED);
    await waitFor(() => {
      expect(button.hasAttribute('disabled')).toBe(false);
    });
    expect(document.activeElement).toBe(link);
    expect(router.state.location.pathname).toBe('/settings');
  });
});

// ---------------------------------------------------------------------------------------------
// Focus after navigation
// ---------------------------------------------------------------------------------------------

describe('focus after navigation', () => {
  it('leaves focus on <body> after the initial load', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/settings');

    await findTitle('Settings');
    await waitForPath(router, '/settings');
    expect(document.activeElement).toBe(document.body);
  });

  it('moves focus to the new <main> when the focused control left with its screen', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN, 'POST /auth/logout': json(200, makeMe()) });
    const { router } = renderApp('/settings');
    await findTitle('Settings');
    const button = screen.getByRole('button', { name: 'Sign out' });
    button.focus();
    expect(document.activeElement).toBe(button);

    fireEvent.click(button);

    await findTitle('Sign in');
    await waitForPath(router, '/login');
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole('main'));
    });
  });

  it('keeps focus on a control that is still mounted, such as the activated link', async () => {
    mockApi({ ...pageReads(), ...SIGNED_IN });
    const { router } = renderApp('/actions');
    await findTitle('Actions');
    const link = within(primaryNav()).getByRole('link', { name: 'Settings' });
    link.focus();

    fireEvent.click(link);

    await findTitle('Settings');
    await waitForPath(router, '/settings');
    expect(document.activeElement).toBe(link);
    expect(link.getAttribute('aria-current')).toBe('page');
  });
});
