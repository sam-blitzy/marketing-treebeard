/**
 * The provider wrapper of the web component tests: {@link renderWithProviders} renders a
 * component inside a fresh TanStack Query client and a TanStack Router on a memory history, the
 * two providers every feature component needs, and resolves once the router is idle and the
 * component has mounted.
 *
 * The test router mirrors the app's paths and search keys (`src/router.tsx`) without importing
 * it, because that module imports every feature page and features are what the tests render:
 * `/`, `/login` with a string `redirect`, `/actions` with a `run` id coerced as the app coerces
 * it, `/decisions`, `/sources` and `/settings`. It has no auth guard and no redirects. The
 * component under test renders in the root route, beside an `Outlet` whose routes render
 * nothing, so `Link`, `useNavigate` and `useSearch({ strict: false })` work and every
 * navigation is visible in `router.state`.
 *
 * Only test files import this module; application code never does. It renders nothing but the
 * two providers, the component under test and the `Outlet`, and it never fetches: stub the
 * network with `./mockApi`.
 *
 * @example
 * ```tsx
 * const { router } = await renderWithProviders(<WindowPicker />, { path: '/actions?run=202' });
 * expect(router.state.location.search).toEqual({ run: 202 });
 * ```
 *
 * @packageDocumentation
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRouteWithContext,
  createRoute,
  createRouter,
  useRouter,
} from '@tanstack/react-router';
import { render, waitFor } from '@testing-library/react';
import type { RenderResult } from '@testing-library/react';
import { useEffect } from 'react';
import type { ReactElement } from 'react';

import type { Run } from '../api/types';

// ---------------------------------------------------------------------------------------------
// Query client
// ---------------------------------------------------------------------------------------------

/**
 * A TanStack Query client for one test: no retries, so a failing query shows its error at
 * once; cached data kept for the whole test (`gcTime: Infinity`), so no garbage-collection timer
 * outlives it; data always stale and never refetched on window focus, so only the component's
 * own behaviour triggers requests.
 *
 * It is the client `src/router.test.tsx` passes to `createAppRouter`, and the one
 * {@link renderWithProviders} creates when none is given.
 *
 * @returns A new, empty client.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime: 0, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}

// ---------------------------------------------------------------------------------------------
// Test router
// ---------------------------------------------------------------------------------------------

/** The routers whose root has committed, that is whose component under test has mounted. */
const mountedRouters = new WeakSet<object>();

/**
 * The component of every child route and of the root's not-found slot: the component under
 * test is the only screen.
 */
function renderNothing(): null {
  return null;
}

/**
 * `/login`'s search, typed as in `src/router.tsx`: `redirect` is kept when it is a string (the
 * app also drops one that is not a same-origin path, which `LoginPage` re-checks itself). The
 * key is always returned, `undefined` when dropped, because TanStack Router merges the
 * validated search over the raw one and an omitted key would keep its raw value.
 *
 * @param search - The search parsed from the URL.
 * @returns `{ redirect }`.
 */
function validateLoginSearch(search: Record<string, unknown>): { redirect?: string } {
  return { redirect: typeof search.redirect === 'string' ? search.redirect : undefined };
}

/**
 * `/actions`'s search, coerced as `src/router.tsx` does: `run` is kept when it is a positive
 * safe integer, given as a number or as a string of digits without a leading zero, and dropped
 * (`undefined`) otherwise.
 *
 * @param search - The search parsed from the URL.
 * @returns `{ run }`.
 */
function validateActionsSearch(search: Record<string, unknown>): { run?: Run['id'] } {
  const value = search.run;
  const candidate =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && /^[1-9][0-9]*$/u.test(value)
        ? Number(value)
        : undefined;
  return {
    run:
      candidate !== undefined && Number.isSafeInteger(candidate) && candidate > 0
        ? candidate
        : undefined,
  };
}

/**
 * Builds the test router: a root route that renders `ui` beside an `Outlet`, and the app's
 * paths as children that render nothing. An unknown path still renders `ui` through the root.
 *
 * @param ui - The component under test.
 * @param queryClient - The client every route receives as `context.queryClient`, as in the app.
 * @param path - The initial entry of the memory history, query included.
 * @returns The router, not loaded yet.
 */
function buildTestRouter(ui: ReactElement, queryClient: QueryClient, path: string) {
  /** Renders the component under test and records, once committed, that it has mounted. */
  function TestRoot(): ReactElement {
    const router = useRouter();
    useEffect(() => {
      mountedRouters.add(router);
      return () => {
        mountedRouters.delete(router);
      };
    }, [router]);
    return (
      <>
        {ui}
        <Outlet />
      </>
    );
  }

  // An unknown path renders `ui` and nothing else: without a notFoundComponent, TanStack Router
  // would add its generic "Not Found" paragraph beside it and log a warning.
  const rootRoute = createRootRouteWithContext<{ queryClient: QueryClient }>()({
    component: TestRoot,
    notFoundComponent: renderNothing,
  });
  const routeTree = rootRoute.addChildren([
    createRoute({ getParentRoute: () => rootRoute, path: '/', component: renderNothing }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: '/login',
      validateSearch: validateLoginSearch,
      component: renderNothing,
    }),
    createRoute({
      getParentRoute: () => rootRoute,
      path: '/actions',
      validateSearch: validateActionsSearch,
      component: renderNothing,
    }),
    createRoute({ getParentRoute: () => rootRoute, path: '/decisions', component: renderNothing }),
    createRoute({ getParentRoute: () => rootRoute, path: '/sources', component: renderNothing }),
    createRoute({ getParentRoute: () => rootRoute, path: '/settings', component: renderNothing }),
  ]);
  return createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
    context: { queryClient },
    defaultPendingMinMs: 0,
  });
}

/**
 * The router {@link renderWithProviders} renders, on a memory history.
 *
 * Feature code types `Link` and `useNavigate` against the app's registered router (the
 * `Register` augmentation in `src/router.tsx`); at runtime this test router resolves the same
 * paths and search keys, so `router.state.location` shows where the component navigated.
 */
export type TestRouter = ReturnType<typeof buildTestRouter>;

// ---------------------------------------------------------------------------------------------
// renderWithProviders
// ---------------------------------------------------------------------------------------------

/** Options of {@link renderWithProviders}. */
export interface RenderWithProvidersOptions {
  /**
   * The client to render with, for example one a test has seeded with `setQueryData`; a new
   * {@link createTestQueryClient} when omitted.
   */
  queryClient?: QueryClient;
  /** The initial location, query included, such as `/actions?run=202`; `/actions` by default. */
  path?: string;
}

/**
 * What {@link renderWithProviders} resolves: Testing Library's render result, plus the client
 * and the router it rendered with. `rerender` and `unmount` act on the whole tree, providers
 * included, so render again with {@link renderWithProviders} to show another component.
 */
export interface RenderWithProvidersResult extends RenderResult {
  /** The query client of the render, so a test can read or seed its cache. */
  queryClient: QueryClient;
  /** The router of the render, whose `state.location` shows every navigation. */
  router: TestRouter;
}

/**
 * Renders `ui` inside a {@link createTestQueryClient} (or `options.queryClient`) and the
 * {@link TestRouter} at `options.path`, and resolves once the router is idle and `ui` has
 * mounted. Tests must await it.
 *
 * @param ui - The component under test.
 * @param options - The query client and the initial path; both optional.
 * @returns The render result, with its query client and router.
 * @throws Error when the router does not settle or `ui` does not mount within Testing Library's
 *   `waitFor` timeout, for example because `ui` threw while rendering (its error is logged).
 *
 * @example
 * ```tsx
 * mockApi({ 'GET /runs': json(200, [makeRun({ id: 202, kind: 'custom' })]) });
 * const { router } = await renderWithProviders(<WindowPicker />);
 * // … pick the custom run …
 * expect(router.state.location.search).toEqual({ run: 202 });
 * ```
 */
export async function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): Promise<RenderWithProvidersResult> {
  const queryClient = options.queryClient ?? createTestQueryClient();
  const path = options.path ?? '/actions';
  const router = buildTestRouter(ui, queryClient, path);
  await router.load();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  await waitFor(() => {
    if (router.state.status !== 'idle') {
      throw new Error(
        `renderWithProviders: the router is still ${router.state.status} at "${router.state.location.href}"`,
      );
    }
    if (!mountedRouters.has(router)) {
      throw new Error(
        `renderWithProviders: the component under test did not mount at "${path}"; it threw while rendering (see the logged error) or is still suspended`,
      );
    }
  });
  return { ...result, queryClient, router };
}
