/**
 * Tests for a code-split screen of `src/router.tsx` whose chunk fails to load, as after a deploy
 * that replaced the hashed chunks or a network failure: the error screen shows inside the app
 * layout, and its Retry reloads the page, because the browser keeps a failed module fetch for
 * the life of the document and re-running the import in it would fail again.
 *
 * `./features/settings/SettingsPage` is mocked with a factory that throws, so the route's
 * dynamic `import()` rejects. The mock is hoisted for this file only, which has a module registry
 * of its own, so it reaches no other suite; `router.test.tsx` covers the screens that load. The
 * network is stubbed with `./test/mockApi`, and the app is rendered as `src/main.tsx` renders it.
 */
// mockApi registers an afterEach hook that fails a test with an unmatched request and restores
// every global stubbed through `vi.stubGlobal`. Vitest skips the remaining afterEach hooks once
// one throws and runs them in reverse order of registration, so this import comes before
// Testing Library's, whose cleanup must still run.
import { apiError, json, mockApi } from './test/mockApi';
import type { MockHandlers } from './test/mockApi';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider, createMemoryHistory } from '@tanstack/react-router';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createAppRouter } from './router';
import { makeConfig, makeMe, makeRun, makeSources } from './test/fixtures';

vi.mock('./features/settings/SettingsPage', () => {
  throw new TypeError('Failed to load the Settings chunk');
});

/**
 * Every read `/actions` makes, with the signed-in user. Recommendations answer with the
 * first-start 404, the page's lightest state, which still renders its title.
 */
const SIGNED_IN_ACTIONS: MockHandlers = {
  'GET /auth/me': json(200, makeMe()),
  'GET /recommendations': apiError(404, 'not_found', 'No analysis run yet'),
  'GET /config': json(200, makeConfig()),
  'GET /sources': json(200, makeSources()),
  'GET /runs/latest': json(200, makeRun()),
};

/**
 * Replaces `window.location` with a snapshot of the real one's address whose `reload` is a mock,
 * so the test observes a reload instead of jsdom reporting navigation as not implemented. jsdom's
 * `reload` is a non-configurable own property, so neither a spy nor a proxy can replace it.
 * mockApi's afterEach restores the real location.
 *
 * @returns The mock standing in for `location.reload`.
 */
function stubReload(): ReturnType<typeof vi.fn> {
  const reload = vi.fn();
  const { href, origin, protocol, host, hostname, port, pathname, search, hash } = window.location;
  vi.stubGlobal('location', {
    href,
    origin,
    protocol,
    host,
    hostname,
    port,
    pathname,
    search,
    hash,
    reload,
  });
  return reload;
}

// jsdom does not implement `window.scrollTo`, which the router calls after each navigation.
beforeEach(() => {
  vi.stubGlobal('scrollTo', () => undefined);
});

describe('a code-split screen whose chunk fails to load', () => {
  it('shows the error screen inside the layout, and Retry reloads the page', async () => {
    mockApi(SIGNED_IN_ACTIONS);
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: Infinity, staleTime: 0, refetchOnWindowFocus: false },
        mutations: { retry: false },
      },
    });
    const router = createAppRouter(queryClient, {
      history: createMemoryHistory({ initialEntries: ['/actions'] }),
    });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    await screen.findByRole('heading', { level: 1, name: 'Actions' });

    const nav = screen.getByRole('navigation', { name: 'Primary' });
    fireEvent.click(within(nav).getByRole('link', { name: 'Settings' }));

    await screen.findByRole('heading', { level: 1, name: 'Something went wrong' });
    expect(router.state.location.pathname).toBe('/settings');
    // Inside the layout: the error fills <main> beside the navigation and below "Sign out".
    const main = screen.getByRole('main');
    expect(within(main).getByText('The page could not be loaded. Try again.')).toBeTruthy();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeTruthy();
    expect(main.contains(screen.getByRole('navigation', { name: 'Primary' }))).toBe(false);
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();

    const reload = stubReload();
    fireEvent.click(within(main).getByRole('button', { name: 'Retry' }));

    expect(reload).toHaveBeenCalledTimes(1);
  });
});
