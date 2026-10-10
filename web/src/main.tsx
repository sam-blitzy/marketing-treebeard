/**
 * The SPA's entry point: `web/index.html` loads this module, which builds the app's one
 * TanStack Query client and its router, and renders them into `<div id="root">`.
 *
 * What it sets up, in order:
 *
 * 1. **Semantic tokens.** `./design-system/tokens.css` is imported first, so every `var(--…)`
 *    token resolves before any component stylesheet or component renders. Nothing else from the
 *    design system is imported here: each wrapper brings its own stylesheet through
 *    `design-system/index.ts`, which the router and the screens import.
 * 2. **The query client.** Exactly one `QueryClient` serves the whole app; the router's auth
 *    guard reads `GET /auth/me` through the same cache the screens' hooks use.
 *    - Queries stay fresh for 30 s and refetch when the window regains focus, so the read-time
 *      overlay (open discrepancies, source modes, dataset freshness, feedback) is current when
 *      the user comes back to the tab.
 *    - A failed query is retried at most twice, with TanStack's exponential delay, but only
 *      when repeating it can help: a server failure (5xx) or a request that never reached the
 *      server. A 4xx answer (401, 404 `not_found`, 409 `run_not_ready`, 422 …) is final and
 *      shown at once. See {@link isRetryableQueryError}.
 *    - Mutations are never retried: a write is not idempotent from the UI's point of view.
 *    - An expired session (a 401 from any mutation, or from any query except the auth guard's
 *      `GET /auth/me`, outside `/login`) clears the cache and opens
 *      `/login?redirect=<the current href>`, so sign-in returns the user to the screen they were
 *      on. The session cookie lives 12 h, so this happens to any tab left open. The router's auth
 *      guard handles that read's 401 itself, during a hover preload too; see
 *      {@link isAuthGuardQuery}. Every other error is left to the screen that made the request.
 *      See {@link redirectToLoginIfSessionExpired}.
 * 3. **The router**, from {@link createAppRouter}, with the query client as every route's
 *    context.
 * 4. **The render**: `StrictMode`, then `QueryClientProvider`, then `RouterProvider`. The router
 *    must sit inside the provider of the same client, because the layout's hooks read the
 *    provider while the auth guard reads the route context.
 *
 * The bundle carries no configuration and loads nothing external: no environment values, no
 * devtools, no service worker and no analytics, so nginx's `default-src 'self'` policy holds
 * and no secret can reach the browser. Network access stays in `./api/client` behind the hooks
 * in `./api/queries`; this module never fetches.
 *
 * @packageDocumentation
 */
import './design-system/tokens.css';

import {
  MutationCache,
  QueryCache,
  QueryClient,
  QueryClientProvider,
  matchQuery,
} from '@tanstack/react-query';
import type { Query } from '@tanstack/react-query';
import { RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { isApiError, queryKeys } from './api/queries';
import { createAppRouter } from './router';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** `id` of the element in `web/index.html` that the app renders into. */
const ROOT_ELEMENT_ID = 'root';

/** The sign-in screen's path; a 401 seen there is a failed sign-in, not an expired session. */
const LOGIN_PATH = '/login';

/** HTTP status of a request made without a valid session. */
const UNAUTHENTICATED_STATUS = 401;

/** Lowest HTTP status of a server-side failure, which a repeat may get past. */
const SERVER_ERROR_MIN_STATUS = 500;

/** `ApiError.code` of a request that never reached the server, or whose response was lost. */
const NETWORK_ERROR_CODE = 'network_error';

/** Retries after a query's first failure, so at most three attempts in all. */
const MAX_QUERY_RETRIES = 2;

/**
 * How long, in milliseconds, a query's data stays fresh. Within it, a screen that mounts again
 * reuses the cache instead of re-asking the API; the polled reads in `./api/queries` set their
 * own intervals, and the user and configuration reads their own longer freshness.
 */
const QUERY_STALE_MS = 30_000;

// ---------------------------------------------------------------------------------------------
// Retry policy
// ---------------------------------------------------------------------------------------------

/**
 * Whether repeating a failed query may succeed.
 *
 * - A server-side failure (status 500 or above, including nginx's 502 while the API restarts):
 *   yes.
 * - A request that never reached the server (status 0, `network_error`): yes. Every `ApiError`
 *   without a response has status 0, so a bare "status below 500" test would also refuse to
 *   retry a dropped connection; the intent is only that 4xx answers are final, and a lost
 *   request is not an answer.
 * - Any 4xx (401, 404, 409, 422, 429 …), a request the client refused to send
 *   (`invalid_request`) and a response the client could not read (`invalid_response`): no. The
 *   same request would get the same answer.
 * - A failure that is not an `ApiError`: yes, as TanStack does by default, because nothing
 *   classified it as final.
 *
 * @param error - The query's failure.
 * @returns `true` when a retry is worth making.
 */
function isRetryableQueryError(error: unknown): boolean {
  if (!isApiError(error)) {
    return true;
  }
  if (error.status >= SERVER_ERROR_MIN_STATUS) {
    return true;
  }
  return error.status === 0 && error.code === NETWORK_ERROR_CODE;
}

/**
 * The queries' `retry` callback: at most {@link MAX_QUERY_RETRIES} retries, and only for a
 * failure {@link isRetryableQueryError} accepts.
 *
 * @param failureCount - Failures before this one: 0 after the first attempt fails.
 * @param error - The failure.
 * @returns `true` to retry.
 */
function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  return failureCount < MAX_QUERY_RETRIES && isRetryableQueryError(error);
}

// ---------------------------------------------------------------------------------------------
// Expired sessions
// ---------------------------------------------------------------------------------------------

/**
 * Whether `pathname` is the sign-in screen, with or without one trailing slash.
 *
 * @param pathname - A router location's pathname.
 * @returns `true` for `/login` and `/login/`.
 */
function isLoginPath(pathname: string): boolean {
  const normalised =
    pathname.length > 1 && pathname.endsWith('/') ? pathname.slice(0, -1) : pathname;
  return normalised === LOGIN_PATH;
}

/**
 * Sends the user to sign in when `error` says the session has ended: a 401, while the app is not
 * on `/login`. The query cache does not call it for the `queryKeys.me` read, whose 401 the
 * router's auth guard owns (see {@link isAuthGuardQuery}), so it sees every mutation's 401 and
 * every other query's. It clears the whole cache, so no data of the ended session survives, and
 * opens `/login` with the current href (path, search and hash) as `redirect`, which the login
 * route accepts only as a same-origin path. Every other error is left to the screen that made
 * the request.
 *
 * On `/login` a 401 is a wrong email or password, which the sign-in form shows itself.
 *
 * The check reads `router.latestLocation`, which moves to `/login` as soon as the navigation
 * below starts. When several requests of one screen fail with 401 together, the first one
 * redirects and the rest find the app already on its way to `/login`, so the user is
 * redirected once.
 *
 * It reads `router` and `queryClient` when an error arrives, never at module evaluation, so it
 * can be declared before either exists.
 *
 * @param error - The failure a query or mutation ended with.
 */
function redirectToLoginIfSessionExpired(error: unknown): void {
  if (!isApiError(error) || error.status !== UNAUTHENTICATED_STATUS) {
    return;
  }
  const current = router.latestLocation;
  if (isLoginPath(current.pathname)) {
    return;
  }
  queryClient.clear();
  void router.navigate({ to: LOGIN_PATH, search: { redirect: current.href } });
}

/**
 * Whether `query` is the signed-in user read, `GET /auth/me`.
 *
 * The router's auth guard owns that query's 401: it redirects to `/login` carrying the href the
 * user asked for, and a 401 seen while a link merely preloads its route on hover must not move
 * the user at all. The global handler therefore leaves it alone, so a first visit while signed
 * out produces one redirect instead of two competing ones.
 *
 * @param query - The query that failed.
 * @returns `true` for the `queryKeys.me` query.
 */
function isAuthGuardQuery(query: Query<unknown, unknown, unknown>): boolean {
  return matchQuery({ queryKey: queryKeys.me, exact: true }, query);
}

// ---------------------------------------------------------------------------------------------
// Query client and router
// ---------------------------------------------------------------------------------------------

/** The app's one query client: see the module header for its defaults. */
const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if (!isAuthGuardQuery(query)) {
        redirectToLoginIfSessionExpired(error);
      }
    },
  }),
  mutationCache: new MutationCache({
    onError: (error) => {
      redirectToLoginIfSessionExpired(error);
    },
  }),
  defaultOptions: {
    queries: {
      retry: shouldRetryQuery,
      staleTime: QUERY_STALE_MS,
      refetchOnWindowFocus: true,
    },
    mutations: {
      retry: 0,
    },
  },
});

/** The app's router, with {@link queryClient} as every route's context. */
const router = createAppRouter(queryClient);

// ---------------------------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------------------------

const container = document.getElementById(ROOT_ELEMENT_ID);
if (container === null) {
  throw new Error(`Root element #${ROOT_ELEMENT_ID} not found`);
}

createRoot(container).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
