/**
 * The TanStack Query layer of the SPA: every read and write of `/api/v1` server state
 * (AAP 0.5.1 "Web API layer", 0.5.3).
 *
 * AAP 0.11.1 routes all server state through this module: components, `src/router.tsx` and
 * `src/main.tsx` call the hooks and helpers below and never fetch. Network access comes only
 * from {@link apiFetch} and its paged form {@link apiFetchPage} in `./client`, which add the
 * session cookie, the CSRF header on every mutation and the error mapping. ESLint keeps
 * `fetch` out of every other module and the
 * client out of feature code. Consumers import {@link ApiError}, {@link isApiError} and
 * {@link ApiErrorCode} from here, never from `./client`.
 *
 * Conventions every hook follows:
 *
 * - **Keys.** Each query uses a key from {@link queryKeys}. Keys are plain, serialisable
 *   tuples, so a root such as `queryKeys.decisionsAll` prefix-matches every filtered variant
 *   below it.
 * - **Cancellation.** Every `queryFn` passes TanStack's `signal` to `apiFetch` or
 *   `apiFetchPage`, so an unmounted screen or a superseded key aborts its request.
 * - **Errors.** {@link apiFetch} and {@link apiFetchPage} reject only with {@link ApiError}, so
 *   every hook's `error` is `ApiError | null` (see the `Register` augmentation below).
 * - **Invalidation.** After a successful write, a mutation invalidates the `*All` roots of
 *   everything the write can change. Its `onSuccess` returns that promise, so `mutateAsync`
 *   resolves only once the refetch of every shown (active) entry it invalidated has settled.
 *   A decision write or a discrepancy resolution first removes the recommendations pages no
 *   screen shows: invalidation would only mark them stale, and a later visit would render
 *   their outdated overlay, a resolved discrepancy's `role="alert"` banner included, until
 *   the refetch landed. Mutations set no `retry`. The app's `QueryClient` (`src/main.tsx`)
 *   does not retry them, because a POST is not idempotent from the UI's point of view.
 * - **Polling.** {@link useDefaultRun} and {@link useRecommendations}, with or without a run
 *   id, refetch every {@link CURRENT_STATE_POLL_MS} while they are shown, so a new default
 *   run and the live overlay (holds, discrepancy count, source modes, freshness, feedback and
 *   historical marking) appear without a reload or a refocus. Without a run id they poll
 *   every {@link FIRST_RUN_POLL_MS} instead while no default run has succeeded yet, so the
 *   first-start screen fills in by itself. A transient failure keeps polling; an expired
 *   session (401) or any other error stops it. {@link useDecisions}, {@link useDiscrepancies}
 *   and {@link useDiscrepancyPages} refetch every {@link CURRENT_STATE_POLL_MS} with the same
 *   error rule, because the worker writes baseline decisions and opens and auto-resolves
 *   discrepancies on its own schedule; the paged list refetches only the pages already
 *   loaded. {@link useSources} polls every {@link SOURCES_POLL_MS}. Polling runs only while
 *   the page is visible and stops when the last component using the query unmounts. A
 *   prefetch ({@link usePrefetchActionsReads}, and {@link useCreateRun}'s read of the page of
 *   the run it creates) registers no component, so it reads once and never polls. A failed
 *   poll keeps the entry's last data, and {@link useActionsRefreshFailure} reports such a
 *   failure among the reads `/actions` shows, so the page can say so.
 *
 * Path ids are always sent as `encodeURIComponent(String(id))`, because `apiFetch` does not
 * encode path segments.
 *
 * @example
 * ```tsx
 * import { isApiError, useRecommendations, useRecordDecision } from '../../api/queries';
 *
 * function PauseButton({ runId, campaignId, itemId }: Props) {
 *   const page = useRecommendations(runId); // GET /recommendations?run_id=… (or the default run)
 *   const record = useRecordDecision();
 *   const retired = isApiError(record.error) && record.error.code === 'subject_retired';
 *   return (
 *     <Button
 *       disabled={page.isPending || retired}
 *       loading={record.isPending}
 *       onClick={() =>
 *         record.mutate({
 *           subject: { type: 'campaign', id: campaignId },
 *           patch: { status: 'paused' },
 *           recommendation_id: itemId,
 *         })
 *       }
 *     >
 *       Record decision
 *     </Button>
 *   );
 * }
 * ```
 */

import {
  queryOptions,
  skipToken,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import type { InfiniteData, QueryClient, QueryKey } from '@tanstack/react-query';
import { useCallback, useEffect, useSyncExternalStore } from 'react';

import { ApiError, apiFetch, apiFetchPage, isApiError } from './client';
import type { ApiPage } from './client';
import type {
  ConfigView,
  CreateRunRequest,
  Decision,
  DecisionRequest,
  Discrepancy,
  DiscrepancyId,
  DiscrepancyState,
  Feedback,
  FeedbackRequest,
  LoginRequest,
  Me,
  RecommendationId,
  RecommendationsPage,
  ResolveRequest,
  Run,
  RunId,
  Source,
  SourceName,
  SubjectIn,
  SubjectType,
  SyncRequestView,
} from './types';

// ---------------------------------------------------------------------------------------------
// Re-exports and error typing
// ---------------------------------------------------------------------------------------------

/**
 * The client's error class and guard, re-exported so features, `src/router.tsx` and
 * `src/main.tsx` never import `./client` directly. ESLint forbids that import in feature code
 * (`eslint.config.js`, block `web/features`).
 */
export { ApiError, isApiError } from './client';

/** The documented vocabulary of {@link ApiError.code}, re-exported from `./client`. */
export type { ApiErrorCode } from './client';

/**
 * Types the `error` of every query and mutation as {@link ApiError}.
 *
 * `apiFetch` rejects only with `ApiError`. The one exception is a caller-initiated abort,
 * which rejects with the original abort error, and TanStack Query treats that as a
 * cancellation rather than an error, so it never reaches `error`. Hence every hook's `error`
 * is `ApiError | null`, and callers can read `error.code`, `error.status` and
 * `error.retryAfter` without narrowing first.
 */
declare module '@tanstack/react-query' {
  interface Register {
    defaultError: ApiError;
  }
}

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/**
 * How often, in milliseconds, {@link useDefaultRun} and {@link useRecommendations} without a
 * run id poll while no default run has succeeded yet ({@link isAwaitingFirstRun}). At first
 * start the worker backfills simulated or live data and then runs the default analysis
 * (AAP 0.11.2 "First start"). Polling shows that run without a reload. Once the run arrives,
 * both reads switch to {@link CURRENT_STATE_POLL_MS}.
 */
export const FIRST_RUN_POLL_MS = 15_000;

/**
 * How often, in milliseconds, a shown {@link useRecommendations} page (default or custom run)
 * and {@link useDefaultRun}'s metadata refetch, so the screen does not go stale after a change
 * it did not make itself (AAP 0.3.2 "Refresh after change").
 *
 * The worker wakes every 30 s. A sync, a dataset activation, a decision or feedback write, or
 * a discrepancy being opened or resolved queues a default-window rerun, and the worker
 * coalesces the requests that arrive within 60 s into one run. Meanwhile
 * `GET /recommendations` reads open discrepancies, source modes, active datasets, freshness
 * and feedback live, and marks items from replaced data historical, indefinitely for an older
 * custom run. Refetching at the worker's own cadence shows a new hold, label or default run
 * within about one wake after it exists, including after a mutation's own invalidation
 * fetched the old run before its coalesced replacement finished.
 */
export const CURRENT_STATE_POLL_MS = 30_000;

/**
 * How often, in milliseconds, {@link useSources} refetches `GET /sources`. Source modes,
 * freshness, the Google call count and "Sync now" progress change as the worker runs
 * (AAP 0.5.5 `/sources`).
 */
export const SOURCES_POLL_MS = 30_000;

/**
 * The discrepancy states {@link useDiscrepancies} lists by default: the unresolved ones the
 * resolver shows (AAP 0.5.5 `/decisions`). They equal the API's own default for
 * `GET /discrepancies`.
 */
export const DEFAULT_DISCREPANCY_STATES: readonly DiscrepancyState[] = Object.freeze([
  'open',
  'awaiting_platform',
]);

/**
 * How long, in milliseconds, the signed-in user (`GET /auth/me`) and the configuration
 * (`GET /config`) stay fresh. The session lasts 12 h, and thresholds change only when the
 * API restarts, so neither needs the app-wide 30 s default.
 */
const LONG_STALE_MS = 5 * 60_000;

/** `GET /runs` returns this many custom runs when {@link useCustomRuns} gets no limit. */
const DEFAULT_CUSTOM_RUNS_LIMIT = 20;

/**
 * The `limit` the first `GET /discrepancies` page of {@link useDiscrepancies} asks for: the
 * largest that the API accepts (`MAX_LIMIT` in
 * `backend/src/ads_engine/api/routes/discrepancies.py`), so a long list takes the fewest pages.
 * Later pages carry it in the `Link` header's next target. It bounds the rows a page holds, and
 * the API leaves out a row whose subject it cannot load, so a page can hold fewer discrepancies
 * while more follow: a page's length never decides whether the list is complete.
 */
const DISCREPANCY_PAGE_LIMIT = 500;

/**
 * How many discrepancy rows each page of {@link useDiscrepancyPages} asks `GET /discrepancies`
 * for: the `limit` of its first request, which later pages carry in the `Link` header's next
 * target. The resolver mounts one card per discrepancy, so it shows this many at first and this
 * many more per "Show more", rather than downloading and mounting the whole list (AAP 0.1.1
 * performance). A page can hold fewer discrepancies while more follow, because the API leaves
 * out one whose subject it cannot load.
 */
export const DISCREPANCY_PAGE_SIZE = 50;

/** The error codes that mean "no default run has succeeded yet" for a run read. */
const AWAITING_FIRST_RUN_CODES: ReadonlySet<string> = new Set(['not_found', 'run_not_ready']);

/**
 * Whether a run read failed only because no default analysis run has succeeded yet, so the
 * screen should say "No analysis run yet" and keep polling rather than show an error.
 *
 * True for an {@link ApiError} with code `not_found` (404 from `GET /runs/latest` or
 * `GET /recommendations` before the first default run succeeds) or `run_not_ready` (409 for a
 * run that is still `running` or has `failed`). `ActionsPage` reuses it for its empty state.
 *
 * @param error - A hook's `error`, or anything caught.
 * @returns `true` for those two codes, `false` for every other value, `null` included.
 */
export function isAwaitingFirstRun(error: unknown): boolean {
  return isApiError(error) && AWAITING_FIRST_RUN_CODES.has(error.code);
}

/**
 * Whether a failed read may succeed if simply repeated: the server could not be reached
 * (status 0 `network_error`), it rate-limited the request (429), or it failed on its own side
 * (5xx, including nginx's 502 while the API restarts).
 */
function isTransientError(error: unknown): boolean {
  return (
    isApiError(error) &&
    ((error.status === 0 && error.code === 'network_error') ||
      error.status === 429 ||
      error.status >= 500)
  );
}

/**
 * The `refetchInterval` of the run and recommendations reads that show current state, from
 * the query's last error. TanStack re-evaluates it after every fetch.
 *
 * - No error (the last fetch succeeded, or the first is still pending):
 *   {@link CURRENT_STATE_POLL_MS}.
 * - With `awaitsFirstRun`, for the default-run reads only, an error saying no default run
 *   exists yet ({@link isAwaitingFirstRun}): {@link FIRST_RUN_POLL_MS}. For a custom run the
 *   same `not_found` or `run_not_ready` is final, so it stops polling.
 * - A transient failure ({@link isTransientError}): {@link CURRENT_STATE_POLL_MS}, so the
 *   screen recovers by itself once the API answers again.
 * - Any other error stops polling, because repeating the request cannot change its answer: a
 *   401 `unauthenticated` (the session is gone), a 404 for an unknown run, a 422, and so on.
 *
 * It takes the error rather than the query, and each hook calls it from an inline
 * `(query) => …` callback. A standalone callback with its own parameter type would become an
 * inference site for `useQuery`'s error type and widen every hook's `error` to `unknown`.
 *
 * @param error - The query's `state.error`: `null` after a success or before any result.
 * @param awaitsFirstRun - `true` for the reads of the latest default run.
 */
function currentStatePollInterval(error: unknown, awaitsFirstRun: boolean): number | false {
  if (error === null) {
    return CURRENT_STATE_POLL_MS;
  }
  if (awaitsFirstRun && isAwaitingFirstRun(error)) {
    return FIRST_RUN_POLL_MS;
  }
  return isTransientError(error) ? CURRENT_STATE_POLL_MS : false;
}

/**
 * Optional filters of {@link useDecisions}, sent as `GET /decisions` query parameters.
 * `subject_id` requires `subject_type`, because ids are unique only within a subject type. A
 * `subject_id` without one answers 422 `invalid_query`.
 */
export interface DecisionsFilter {
  /** Only `campaign` or only `budget` (Google explicitly shared budget) subjects. */
  subject_type?: SubjectType;
  /** Only this subject's history; needs `subject_type`. */
  subject_id?: SubjectIn['id'];
}

/**
 * Options of {@link useDiscrepancies}. They never enter the query key, so every reader of the
 * same states shares one cache entry and one request.
 */
export interface DiscrepanciesOptions {
  /**
   * Whether the hook may send `GET /discrepancies`; `true` when omitted. `false` sends nothing
   * and returns only what the cache already holds for the key.
   */
  enabled?: boolean;
}

/**
 * Returns a copy of `filter` holding only its defined members, so `{}` and
 * `{ subject_id: undefined }` produce the same, deep-equal query key.
 */
function normaliseDecisionsFilter(filter: DecisionsFilter): Readonly<DecisionsFilter> {
  const normalised: DecisionsFilter = {};
  if (filter.subject_type !== undefined) {
    normalised.subject_type = filter.subject_type;
  }
  if (filter.subject_id !== undefined) {
    normalised.subject_id = filter.subject_id;
  }
  return normalised;
}

/** Encodes one path segment, because `apiFetch` leaves path segments as given. */
function pathSegment(id: string | number): string {
  return encodeURIComponent(String(id));
}

// ---------------------------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------------------------

/**
 * Every query key of the app, in one place. The keys are stable, serialisable tuples
 * (`as const`), so features can read and seed the cache and tests can assert exact keys.
 *
 * Keys nest by prefix. Each mutation invalidates the `*All` root of what it changes
 * (`recommendationsAll`, `decisionsAll`, `discrepanciesAll`, `runs.customAll`), so every
 * filtered or per-run variant under that root refreshes. Exact keys such as `runs.byId(id)`
 * are for reading and seeding single entries.
 *
 * | Key | Endpoint |
 * |-----|----------|
 * | `me` | `GET /auth/me` |
 * | `runs.latest` | `GET /runs/latest` |
 * | `runs.byId(id)` | `GET /runs/{id}` |
 * | `runs.custom(limit)` | `GET /runs?kind=custom&limit=` |
 * | `recommendations(runId \| 'default')` | `GET /recommendations[?run_id=]` |
 * | `decisions(filter)` | `GET /decisions[?subject_type=&subject_id=]` |
 * | `discrepancies(states)` | `GET /discrepancies?state=…`, every page |
 * | `discrepancyPages(states)` | `GET /discrepancies?state=…&limit=50`, the pages loaded so far |
 * | `sources` | `GET /sources` |
 * | `config` | `GET /config` |
 *
 * @example
 * ```ts
 * queryClient.getQueryData<Run>(queryKeys.runs.byId(42));
 * void queryClient.invalidateQueries({ queryKey: queryKeys.decisionsAll });
 * ```
 */
export const queryKeys = {
  /** The signed-in user, `GET /auth/me`. */
  me: ['auth', 'me'] as const,
  /** Analysis run metadata. */
  runs: {
    /** Root of every run key. */
    all: ['runs'] as const,
    /** The latest succeeded default run, `GET /runs/latest`. */
    latest: ['runs', 'latest'] as const,
    /**
     * One run by id, `GET /runs/{id}`. `undefined` is the key of the disabled
     * `useRun(undefined)` placeholder, which never fetches.
     */
    byId: (id: RunId | undefined) => ['runs', 'byId', id] as const,
    /** Root of every custom-run list. */
    customAll: ['runs', 'custom'] as const,
    /** The newest `limit` custom runs, `GET /runs?kind=custom&limit=`. */
    custom: (limit: number) => ['runs', 'custom', limit] as const,
  },
  /** Root of every recommendations page. */
  recommendationsAll: ['recommendations'] as const,
  /** One recommendations page: `'default'` for the latest default run, else a run id. */
  recommendations: (runId: RunId | 'default') => ['recommendations', runId] as const,
  /** Root of every decision-log list. */
  decisionsAll: ['decisions'] as const,
  /** One decision-log list. Undefined filter members are dropped, so `{}` equals them. */
  decisions: (filter: DecisionsFilter) =>
    ['decisions', normaliseDecisionsFilter(filter)] as const,
  /** Root of every discrepancy list. */
  discrepanciesAll: ['discrepancies'] as const,
  /** The discrepancies in `states`, copied so a later change to the array cannot alter it. */
  discrepancies: (states: readonly DiscrepancyState[]) => ['discrepancies', [...states]] as const,
  /**
   * The paged discrepancy list in `states` ({@link useDiscrepancyPages}), under the same root,
   * so every write that invalidates `discrepanciesAll` refreshes it; the states are copied.
   */
  discrepancyPages: (states: readonly DiscrepancyState[]) =>
    ['discrepancies', 'pages', [...states]] as const,
  /** Every source's health, `GET /sources`. */
  sources: ['sources'] as const,
  /** The read-only configuration, `GET /config`. */
  config: ['config'] as const,
} as const;

// ---------------------------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------------------------

/**
 * Query options for the signed-in user, `GET /auth/me` → {@link Me}.
 *
 * `src/router.tsx`'s auth guard calls `queryClient.ensureQueryData(meQueryOptions())` and
 * redirects to `/login` on a 401 `unauthenticated` {@link ApiError}. The options never retry,
 * so a signed-out user is redirected at once. The result stays fresh for 5 minutes, and
 * {@link useLogin} seeds it and {@link useLogout} clears it, so navigation does not re-ask the
 * API on every route change.
 *
 * Status codes: 200 `{email}`; 401 `unauthenticated` without a valid session.
 *
 * @returns Options for `useQuery`, `ensureQueryData` or `fetchQuery`, keyed by `queryKeys.me`.
 */
export function meQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.me,
    queryFn: ({ signal }) => apiFetch<Me>('/auth/me', { signal }),
    staleTime: LONG_STALE_MS,
    retry: false,
  });
}

/**
 * The signed-in user: `GET /auth/me` → {@link Me}. See {@link meQueryOptions} for caching,
 * retries and status codes.
 *
 * @returns The query result; `error` is a 401 `unauthenticated` {@link ApiError} when
 *   signed out.
 */
export function useMe() {
  return useQuery(meQueryOptions());
}

/**
 * Query options for the latest **succeeded default** run, `GET /runs/latest` → {@link Run}:
 * the options {@link useDefaultRun} observes and {@link usePrefetchActionsReads} prefetches.
 *
 * **Seeded from the default page.** When the cache already holds the default recommendations
 * page (`queryKeys.recommendations('default')`, `GET /recommendations` without `run_id`), a
 * new `queryKeys.runs.latest` entry starts from that page's `run`, dated when the page was
 * read, so it is fetched only once that copy is stale. Both reads answer with the same run:
 * run selection gives `/runs/latest` and `GET /recommendations` without `run_id` only the
 * latest succeeded default run (AAP 0.5.3), and the API's `get_latest_run`
 * (`api/routes/runs.py`) and `get_recommendations` (`api/routes/recommendations.py`) both
 * render it as `schemas.Run.from_row(latest_default_run(...))`. Only the default page seeds
 * it: a page addressed by run id may show a custom run, which never stands in for the
 * default. The seed applies only while the entry does not exist yet; an existing entry keeps
 * its own data and polling.
 *
 * Polling is inline, `(query) => currentStatePollInterval(query.state.error, true)`, for the
 * reason {@link currentStatePollInterval} gives.
 *
 * @param queryClient - The client whose cache holds the default page to seed from.
 * @returns Options for `useQuery` or `prefetchQuery`, keyed by `queryKeys.runs.latest`.
 */
export function defaultRunQueryOptions(queryClient: QueryClient) {
  return queryOptions({
    queryKey: queryKeys.runs.latest,
    queryFn: ({ signal }) => apiFetch<Run>('/runs/latest', { signal }),
    initialData: () =>
      queryClient.getQueryData<RecommendationsPage>(queryKeys.recommendations('default'))?.run,
    initialDataUpdatedAt: () =>
      queryClient.getQueryState(queryKeys.recommendations('default'))?.dataUpdatedAt,
    refetchInterval: (query) => currentStatePollInterval(query.state.error, true),
  });
}

/**
 * The latest **succeeded default** run: `GET /runs/latest` → {@link Run} (AAP 0.5.3 run
 * selection). A custom run, even one whose window equals the default, never stands in for
 * it, and a running or failed default run never replaces the last good one.
 *
 * A first read after the default `/actions` page has loaded sends no request while that page
 * is fresh, because the entry starts from the page's own run ({@link defaultRunQueryOptions}).
 *
 * Status codes: 200; 404 `not_found` ("No analysis run yet") before the first default run
 * succeeds. While that 404 persists the query refetches every {@link FIRST_RUN_POLL_MS}, so
 * the first-start screen fills in by itself (e2e `first-start.spec.ts`).
 *
 * Once a run is shown it refetches every {@link CURRENT_STATE_POLL_MS}, so the worker's next
 * default run, for example the coalesced rerun after a decision, replaces it without a
 * reload. A transient failure (unreachable server, 429, 5xx) keeps that cadence; a 401 or any
 * other error stops polling. Polling runs only while the page is visible
 * (`refetchIntervalInBackground` stays `false`) and ends when the last component using it
 * unmounts.
 *
 * @returns The query result, keyed by `queryKeys.runs.latest`.
 */
export function useDefaultRun() {
  const queryClient = useQueryClient();
  return useQuery(defaultRunQueryOptions(queryClient));
}

/**
 * One run by id, default or custom, in any status: `GET /runs/{id}` → {@link Run}. With
 * `runId` `undefined` the query is disabled and sends nothing.
 *
 * Status codes: 200, with `status` `running`, `succeeded` or `failed`; 404 `not_found` for
 * an unknown id; 422 `validation_error` for an id outside 1 to 2^63 − 1. {@link useCreateRun}
 * seeds this key with the run it creates.
 *
 * @param runId - The run to read, or `undefined` while no run is addressed.
 * @returns The query result, keyed by `queryKeys.runs.byId(runId)`.
 */
export function useRun(runId: RunId | undefined) {
  return useQuery({
    queryKey: queryKeys.runs.byId(runId),
    // skipToken disables the query in a type-safe way: no request, no non-null assertion.
    queryFn:
      runId === undefined
        ? skipToken
        : ({ signal }) => apiFetch<Run>(`/runs/${pathSegment(runId)}`, { signal }),
  });
}

/**
 * The newest custom-window runs, newest first, in every status:
 * `GET /runs?kind=custom&limit=` → {@link Run}[].
 *
 * With `enabled: false` the query sends nothing and keeps whatever list it already holds, so a
 * caller can defer the read until the user asks for the list; `WindowPicker` does, behind its
 * "Recent custom windows" disclosure, and drops the list with {@link useForgetCustomRuns} once
 * the user no longer asks for it.
 *
 * Status codes: 200, an empty list when there are none; 422 `validation_error` for a `limit`
 * outside 1–100.
 *
 * @param limit - How many runs to list, 1 to 100; 20 when omitted, the API's own default.
 * @param options - `enabled`: whether the list may be read; `true` when omitted.
 * @returns The query result, keyed by `queryKeys.runs.custom(limit)`.
 */
export function useCustomRuns(
  limit: number = DEFAULT_CUSTOM_RUNS_LIMIT,
  options: { enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: queryKeys.runs.custom(limit),
    queryFn: ({ signal }) =>
      apiFetch<Run[]>('/runs', { query: { kind: 'custom', limit }, signal }),
    enabled: options.enabled ?? true,
  });
}

/**
 * Whether the cache already holds a custom-run list for `limit` ({@link useCustomRuns}), that
 * is whether it was read earlier in this session and not forgotten since
 * ({@link useForgetCustomRuns}). It reads the cache at render time and sends nothing, so a
 * caller can open a deferred list at once while the session still holds it.
 *
 * @param limit - The list's limit, as given to {@link useCustomRuns}; 20 when omitted.
 * @returns `true` when `queryKeys.runs.custom(limit)` holds data, even data being refetched.
 */
export function useHasCachedCustomRuns(limit: number = DEFAULT_CUSTOM_RUNS_LIMIT): boolean {
  const queryClient = useQueryClient();
  return queryClient.getQueryData<Run[]>(queryKeys.runs.custom(limit)) !== undefined;
}

/**
 * A callback that forgets the custom-run list for `limit` ({@link useCustomRuns}): it removes
 * exactly `queryKeys.runs.custom(limit)` from the cache, and with it the list, its error and
 * any read still in flight, which is aborted. Every other key stays, the lists of other limits
 * included. It sends nothing.
 *
 * `WindowPicker` calls it when the user closes "Recent custom windows", so
 * {@link useHasCachedCustomRuns} is `false` afterwards and a later mount starts with the list
 * closed and unread. A `useCustomRuns` observer that is still mounted, disabled, keeps working:
 * on its next render TanStack Query builds a new, empty entry under the same key, and enabling
 * the observer again reads the list once.
 *
 * @param limit - The list's limit, as given to {@link useCustomRuns}; 20 when omitted.
 * @returns A callback, stable while the client and `limit` stay the same.
 */
export function useForgetCustomRuns(limit: number = DEFAULT_CUSTOM_RUNS_LIMIT): () => void {
  const queryClient = useQueryClient();
  return useCallback(() => {
    queryClient.removeQueries({ queryKey: queryKeys.runs.custom(limit), exact: true });
  }, [queryClient, limit]);
}

/**
 * The five sections of one run with the current-state overlay:
 * `GET /recommendations[?run_id=]` → {@link RecommendationsPage}.
 *
 * Without `runId` the request carries no `run_id`, and the API serves the latest succeeded
 * default run. That page polls every {@link FIRST_RUN_POLL_MS} while no default run exists
 * yet (404 `not_found`), like {@link useDefaultRun}. With `runId`, used for a custom window
 * at `/actions?run=<id>`, the request carries `run_id`, and a 404 or a 409 `run_not_ready`
 * for it is final.
 *
 * The page's order (five sections in `SECTION_ORDER`, ranked groups and items) comes from the
 * API exactly as the run stored it. Each item's `current` and the page's `overlay` and
 * `banners` are read-time state, computed when the API serves the request: live
 * discrepancies, source modes, freshness, feedback and historical-only marking (AAP 0.3.2).
 * Every write that changes them invalidates this key's root; a decision write or a
 * discrepancy resolution first removes the pages no screen shows, so none renders outdated.
 *
 * The worker and other sessions change that state too, so a shown page, default or custom,
 * refetches every {@link CURRENT_STATE_POLL_MS}. A new discrepancy then holds its dependent
 * items, a replaced dataset turns items historical, and the default page moves to the
 * worker's next default run, all without a reload or a refocus. A transient failure
 * (unreachable server, 429, 5xx) keeps that cadence; a 401 or any other error stops polling.
 * Polling runs only while the page is visible (`refetchIntervalInBackground` stays `false`)
 * and ends when the last component using it unmounts.
 *
 * Status codes: 200; 404 `not_found` for an unknown run or before the first default run; 409
 * `run_not_ready` for a run that is still `running` or has `failed`.
 *
 * The options come from {@link recommendationsQueryOptions}, which {@link useCreateRun} also
 * prefetches, so a custom run it creates is already cached when `/actions?run=<id>` mounts
 * this hook, and the page renders at once instead of a loading state.
 *
 * @param runId - The run to show; omitted for the latest default run.
 * @returns The query result, keyed by `queryKeys.recommendations(runId ?? 'default')`.
 */
export function useRecommendations(runId?: RunId) {
  return useQuery(recommendationsQueryOptions(runId));
}

/**
 * Query options for the five sections of one run with the current-state overlay,
 * `GET /recommendations[?run_id=]` → {@link RecommendationsPage}: the options
 * {@link useRecommendations} observes and {@link useCreateRun} prefetches for the run it
 * creates. Without `runId` the request carries no `run_id` and reads the latest default run.
 *
 * Polling is inline, `(query) => currentStatePollInterval(query.state.error, runId ===
 * undefined)`, for the reason {@link currentStatePollInterval} gives: every
 * {@link CURRENT_STATE_POLL_MS} while shown, every {@link FIRST_RUN_POLL_MS} while the default
 * page awaits its first run, and never after a final error. A prefetch registers no
 * component, so it reads once and never polls.
 *
 * @param runId - The run to read; omitted for the latest default run.
 * @returns Options for `useQuery` or `prefetchQuery`, keyed by
 *   `queryKeys.recommendations(runId ?? 'default')`.
 */
export function recommendationsQueryOptions(runId?: RunId) {
  return queryOptions({
    queryKey: queryKeys.recommendations(runId ?? 'default'),
    queryFn: ({ signal }) =>
      apiFetch<RecommendationsPage>('/recommendations', {
        query: runId === undefined ? undefined : { run_id: runId },
        signal,
      }),
    refetchInterval: (query) => currentStatePollInterval(query.state.error, runId === undefined),
  });
}

/**
 * The decision log of the active datasets, newest first: `GET /decisions` → {@link Decision}[]
 * (AAP 0.5.5 `DecisionLog`). It sends `subject_type` and `subject_id` only when the filter
 * sets them. The API returns at most its default 200 rows.
 *
 * Status codes: 200; 422 `invalid_query` when `subject_id` comes without `subject_type`; 409
 * `subject_retired` when `subject_id` names a campaign or shared budget of a dataset that is
 * not active, as on a screen opened before its dataset was replaced (AAP 0.3.2). An unknown
 * `subject_id` answers 200 with no row. Recording a decision or resolving a discrepancy
 * invalidates every variant.
 *
 * The worker writes decisions too (the baseline of each new dataset, AAP 0.5.2), and other
 * sessions record them, so a shown log refetches every {@link CURRENT_STATE_POLL_MS} (AAP 0.3.2
 * "Refresh after change"). A transient failure (unreachable server, 429, 5xx) keeps that
 * cadence; a 401 or any other error stops polling. Polling runs only while the page is visible
 * and ends when the last component using it unmounts.
 *
 * @param filter - Restrict to one subject type, or to one subject (type and id).
 * @returns The query result, keyed by `queryKeys.decisions(filter)`.
 */
export function useDecisions(filter: DecisionsFilter = {}) {
  const queryKey = queryKeys.decisions(filter);
  const [, normalised] = queryKey;
  return useQuery({
    queryKey,
    queryFn: ({ signal }) =>
      apiFetch<Decision[]>('/decisions', {
        query: { subject_type: normalised.subject_type, subject_id: normalised.subject_id },
        signal,
      }),
    refetchInterval: (query) => currentStatePollInterval(query.state.error, false),
  });
}

/** Message of the error a `GET /discrepancies` read rejects with when its paging loops. */
const REPEATED_PAGE_MESSAGE = 'The server returned a link to a page it had already sent.';

/**
 * Reads every discrepancy in `states`, oldest first, by following the API's `Link` cursor.
 *
 * A `GET /discrepancies` page holds at most `limit` rows (200 by default), and the API names the
 * next page in a `Link: <…>; rel="next"` header only while more rows follow, so the last page
 * never links to an empty one. Discrepancies are resolved before anything else is trusted
 * (AAP 0.5.4.2 P1), so every page is read: the first at {@link DISCREPANCY_PAGE_LIMIT}, then
 * each `next` page from {@link apiFetchPage}, exactly as the API named it, until a page names
 * none. A page's length never ends the list.
 * The API leaves out a discrepancy whose subject it cannot load, so a page can hold fewer
 * discrepancies than `limit`, or none, while a next page follows, and only the API's cursor,
 * the id of the page's last row, steps past a row it left out.
 *
 * Two failures reject or restart the read rather than end the list short:
 *
 * - A next page this read has already followed means the paging loops. The read rejects with
 *   an `invalid_response` {@link ApiError} instead of returning what it has.
 * - A 422 `invalid_cursor` on a later page means the cursor's row was purged between pages,
 *   and the API says to list again from the first page. The read restarts once from the
 *   first page and discards the discrepancies collected so far. A second `invalid_cursor`
 *   rejects the read.
 *
 * Every request carries `signal`, so cancelling the query aborts the page in flight.
 */
async function fetchAllDiscrepancies(
  states: readonly DiscrepancyState[],
  signal: AbortSignal,
): Promise<Discrepancy[]> {
  let restarted = false;
  for (;;) {
    let page = await apiFetchPage<Discrepancy[]>('/discrepancies', {
      query: { state: states, limit: DISCREPANCY_PAGE_LIMIT },
      signal,
    });
    const all: Discrepancy[] = [...page.data];
    const followed = new Set<string>();
    try {
      while (page.next !== undefined) {
        if (followed.has(page.next)) {
          // GET /discrepancies answers every page with 200, the status this page came with.
          throw new ApiError(200, 'invalid_response', REPEATED_PAGE_MESSAGE);
        }
        followed.add(page.next);
        page = await apiFetchPage<Discrepancy[]>(page.next, { signal });
        all.push(...page.data);
      }
      return all;
    } catch (error) {
      if (restarted || !isApiError(error) || error.code !== 'invalid_cursor') {
        throw error;
      }
      restarted = true;
    }
  }
}

/**
 * The discrepancies of the active datasets in `states`, oldest `detected_at` first, the
 * order C0 items are pinned in: `GET /discrepancies?state=…` → {@link Discrepancy}[]
 * (AAP 0.5.5 `DiscrepancyResolver`). Each state is sent as its own repetition of the
 * parameter (`state=open&state=awaiting_platform`). An empty list sends no `state`, which the
 * API reads as its default unresolved states. Every page is read by following the API's
 * `Link: rel="next"` cursor until a page names none ({@link fetchAllDiscrepancies}), so the list
 * is never truncated at the API's page size. That includes a page holding fewer discrepancies
 * than its `limit`, or none, because the API left out one whose subject it cannot load.
 *
 * Status codes: 200; 422 `validation_error` for an unknown state; 422 `invalid_cursor` only
 * when it recurs after the read restarted once from the first page; and `invalid_response`
 * when the paging link cannot be parsed, leaves `/api/v1` or repeats a page already read.
 * Recording a decision or resolving a discrepancy invalidates every variant.
 *
 * The worker opens discrepancies after each entity sync and resolves awaiting-platform ones
 * when a snapshot matches, so a shown list refetches every {@link CURRENT_STATE_POLL_MS}
 * (AAP 0.3.2 "Refresh after change"). A transient failure keeps that cadence; a 401 or any
 * other error stops polling. Polling runs only while the page is visible and ends when the last
 * component using it unmounts. A screen that shows the list itself rather than looking entries
 * up in it reads {@link useDiscrepancyPages} instead, a page at a time.
 *
 * With `options.enabled` set to `false` the hook neither sends a request nor polls, yet still
 * returns the list that an enabled reader of the same states has cached, and each refetch that
 * reader makes. `/actions` uses this to read the list only when a held card's blocker cannot be
 * named from the run itself (`BlockingDiscrepancies`). The C0 card lookups in `CampaignsSection`
 * read the same `queryKeys.discrepancies(states)` entry. The `/decisions` resolver does not: it
 * reads {@link useDiscrepancyPages}, keyed by `queryKeys.discrepancyPages(states)` under the
 * same `discrepanciesAll` root, so one invalidation refreshes both.
 *
 * @param states - The states to list; `open` and `awaiting_platform` when omitted.
 * @param options - See {@link DiscrepanciesOptions}; the read is enabled when omitted.
 * @returns The query result, keyed by `queryKeys.discrepancies(states)`, whatever `options`.
 */
export function useDiscrepancies(
  states: readonly DiscrepancyState[] = DEFAULT_DISCREPANCY_STATES,
  options: DiscrepanciesOptions = {},
) {
  return useQuery({
    queryKey: queryKeys.discrepancies(states),
    queryFn: ({ signal }) => fetchAllDiscrepancies(states, signal),
    enabled: options.enabled ?? true,
    refetchInterval: (query) => currentStatePollInterval(query.state.error, false),
  });
}

/**
 * What {@link useDiscrepancyPages} resolves `data` to: the discrepancies of the pages loaded so
 * far and the size of the whole list.
 */
export interface DiscrepancyList {
  /** The discrepancies loaded so far, oldest `detected_at` first, each once. */
  discrepancies: Discrepancy[];
  /**
   * How many discrepancy rows of the active datasets are in the requested states, from the
   * `X-Total-Count` header of the most recently read page. It includes rows the API leaves out
   * of the list because their subject cannot be loaded, as the `/actions` banner counts them.
   * `undefined` when the API sent no count.
   */
  total: number | undefined;
}

/**
 * One load of {@link useDiscrepancyPages}, as its infinite query caches it: what one "Show more"
 * (or the first read) added. Kept private; consumers read the flattened {@link DiscrepancyList}.
 */
interface DiscrepancyPage {
  /** The discrepancies this load read, oldest first. */
  discrepancies: Discrepancy[];
  /** The `Link: rel="next"` page after this load, relative to `/api/v1`; `undefined` at the end. */
  next: string | undefined;
  /** The `X-Total-Count` of the last response this load read. */
  total: number | undefined;
  /**
   * Every next link the read has followed up to and including this load, since its first page,
   * so a later load can refuse a link already followed however many loads ago.
   */
  followed: readonly string[];
  /**
   * Whether this load re-read the list from its first page after a 422 `invalid_cursor`, so it
   * holds every discrepancy shown before it as well as the new ones, and replaces the loads
   * before it.
   */
  restarted: boolean;
}

/** Where one load of {@link useDiscrepancyPages} starts: its infinite query's page param. */
interface DiscrepancyPageParam {
  /** The previous load's next link, or `null` for the first page. */
  next: string | null;
  /** The next links the read followed before this load ({@link DiscrepancyPage.followed}). */
  followed: readonly string[];
  /** How many discrepancies are shown before this load, for a restart to re-read past them. */
  shown: number;
}

/** The page param of the first load: the first page, nothing followed or shown yet. */
const FIRST_DISCREPANCY_PAGE: DiscrepancyPageParam = Object.freeze({
  next: null,
  followed: Object.freeze([]),
  shown: 0,
});

/**
 * A 422 `invalid_cursor` on a followed link, inside one load of {@link useDiscrepancyPages}:
 * the row the cursor names was purged between pages. {@link fetchDiscrepancyPage} catches it
 * and restarts once from the first page, so it never leaves the query function.
 */
class ExpiredCursorError extends Error {
  /** The API's own error, rethrown when the restart meets an expired cursor too. */
  readonly apiError: ApiError;

  /** @param apiError - The 422 `invalid_cursor` the followed link answered with. */
  constructor(apiError: ApiError) {
    super(apiError.message);
    this.name = 'ExpiredCursorError';
    this.apiError = apiError;
  }
}

/**
 * Reads one load of {@link useDiscrepancyPages}, starting at `start`: the first page
 * (`state` repeated, `limit` {@link DISCREPANCY_PAGE_SIZE}) when `start` is `null`, otherwise
 * the next link exactly as the API named it.
 *
 * The load keeps following the API's next link while it holds `shown` discrepancies or fewer
 * and a next page exists. For a normal load `shown` is 0, so a load never ends empty while the
 * API leaves out discrepancies whose subject it cannot load and more follow; "Show more" then
 * always adds something or ends the list. A restart passes the count already shown, so it
 * re-reads past them and adds at least one more.
 *
 * A next link in `followedBefore`, or followed earlier in this load, means the paging loops:
 * the load rejects with an `invalid_response` {@link ApiError}. A 422 `invalid_cursor` on a
 * followed link rejects with {@link ExpiredCursorError}; on the first page's own request it is
 * the API's error unchanged. Every request carries `signal`.
 */
async function readDiscrepancyLoad(
  states: readonly DiscrepancyState[],
  start: string | null,
  followedBefore: readonly string[],
  shown: number,
  signal: AbortSignal,
): Promise<DiscrepancyPage> {
  const followed = new Set(followedBefore);
  const follow = async (next: string): Promise<ApiPage<Discrepancy[]>> => {
    if (followed.has(next)) {
      // GET /discrepancies answers every page with 200, the status this link came with.
      throw new ApiError(200, 'invalid_response', REPEATED_PAGE_MESSAGE);
    }
    followed.add(next);
    try {
      return await apiFetchPage<Discrepancy[]>(next, { signal });
    } catch (error) {
      if (isApiError(error) && error.code === 'invalid_cursor') {
        throw new ExpiredCursorError(error);
      }
      throw error;
    }
  };
  let page =
    start === null
      ? await apiFetchPage<Discrepancy[]>('/discrepancies', {
          query: { state: states, limit: DISCREPANCY_PAGE_SIZE },
          signal,
        })
      : await follow(start);
  const discrepancies: Discrepancy[] = [...page.data];
  while (discrepancies.length <= shown && page.next !== undefined) {
    page = await follow(page.next);
    discrepancies.push(...page.data);
  }
  return {
    discrepancies,
    next: page.next,
    total: page.total,
    followed: [...followed],
    restarted: false,
  };
}

/**
 * The query function of {@link useDiscrepancyPages}: one load from `param`
 * ({@link readDiscrepancyLoad}).
 *
 * A 422 `invalid_cursor` on a followed link means the cursor's row was purged between pages,
 * and the API says to list again from the first page. The load then restarts once from the
 * first page, re-reads past the `param.shown` discrepancies already on screen and returns them
 * with the new ones as a `restarted` load, which replaces the loads before it, so the list is
 * never ended short and holds no row twice. A second `invalid_cursor` rejects with it.
 */
async function fetchDiscrepancyPage(
  states: readonly DiscrepancyState[],
  param: DiscrepancyPageParam,
  signal: AbortSignal,
): Promise<DiscrepancyPage> {
  try {
    return await readDiscrepancyLoad(states, param.next, param.followed, 0, signal);
  } catch (error) {
    if (!(error instanceof ExpiredCursorError)) {
      throw error;
    }
  }
  try {
    const restart = await readDiscrepancyLoad(states, null, [], param.shown, signal);
    return { ...restart, restarted: true };
  } catch (error) {
    throw error instanceof ExpiredCursorError ? error.apiError : error;
  }
}

/**
 * How many discrepancies `pages` show together: every load's since the last `restarted` load,
 * which holds all those before it.
 */
function shownDiscrepancyCount(pages: readonly DiscrepancyPage[]): number {
  let count = 0;
  for (const page of pages) {
    count = page.restarted ? page.discrepancies.length : count + page.discrepancies.length;
  }
  return count;
}

/**
 * The `getNextPageParam` of {@link useDiscrepancyPages}: the last load's next link, the links
 * the read followed so far and the count shown, or `undefined` once the API names no next page,
 * which ends the list (`hasNextPage` false).
 */
function nextDiscrepancyPageParam(
  lastPage: DiscrepancyPage,
  allPages: DiscrepancyPage[],
): DiscrepancyPageParam | undefined {
  if (lastPage.next === undefined) {
    return undefined;
  }
  return {
    next: lastPage.next,
    followed: lastPage.followed,
    shown: shownDiscrepancyCount(allPages),
  };
}

/**
 * The `select` of {@link useDiscrepancyPages}: the loaded discrepancies in order, from the last
 * `restarted` load on, and the total of the most recently read load, which is the last one
 * (a refetch reads the loads in order, and "Show more" appends).
 */
function selectDiscrepancyList(
  data: InfiniteData<DiscrepancyPage, DiscrepancyPageParam>,
): DiscrepancyList {
  let first = 0;
  data.pages.forEach((page, index) => {
    if (page.restarted) {
      first = index;
    }
  });
  const last = data.pages[data.pages.length - 1];
  return {
    discrepancies: data.pages.slice(first).flatMap((page) => page.discrepancies),
    total: last?.total,
  };
}

/**
 * The discrepancies of the active datasets in `states`, oldest `detected_at` first, a page at a
 * time: `GET /discrepancies?state=…&limit=50`, then each `Link: rel="next"` page exactly as the
 * API names it (AAP 0.5.5 `DiscrepancyResolver`, AAP 0.1.1 performance).
 *
 * The first read loads {@link DISCREPANCY_PAGE_SIZE} rows' worth; `fetchNextPage()` loads the
 * next while `hasNextPage` is true, so the screen never downloads or mounts more than the user
 * asked to see, and every discrepancy stays reachable. `data` is a {@link DiscrepancyList}: the
 * loaded discrepancies and the list's total from the API's `X-Total-Count`, so "{n} of {total}"
 * can be shown without reading every page.
 *
 * Paging rules, as {@link useDiscrepancies} keeps them for the whole list:
 *
 * - A load keeps following the next link while it has read no discrepancy, because the API
 *   leaves out one whose subject it cannot load, so "Show more" never adds nothing while more
 *   exist.
 * - A next link the read has already followed, in this load or an earlier one, rejects with an
 *   `invalid_response` {@link ApiError} rather than loop.
 * - A 422 `invalid_cursor` on a followed link restarts the load once from the first page,
 *   re-reading past what is shown ({@link fetchDiscrepancyPage}); a second one rejects.
 * - Every request carries TanStack's `signal`, so unmounting the screen aborts the page in
 *   flight.
 *
 * A shown list refetches every {@link CURRENT_STATE_POLL_MS} (AAP 0.3.2): TanStack re-reads the
 * pages already loaded, from the first, following fresh links, so new and auto-resolved
 * discrepancies appear without a reload. A transient failure keeps that cadence; a 401 or any
 * other error stops polling. A failed refetch or `fetchNextPage` keeps the loaded `data`.
 * Recording a decision or resolving a discrepancy invalidates it through
 * `queryKeys.discrepanciesAll`.
 *
 * Status codes: 200; 422 `validation_error` for an unknown state; 422 `invalid_cursor` only when
 * it recurs after the restart; and `invalid_response` when a paging link or the total cannot be
 * parsed, a link leaves `/api/v1`, or a link repeats.
 *
 * @param states - The states to list; `open` and `awaiting_platform` when omitted.
 * @returns The infinite query result, keyed by `queryKeys.discrepancyPages(states)`, whose
 *   `data` is a {@link DiscrepancyList}.
 */
export function useDiscrepancyPages(
  states: readonly DiscrepancyState[] = DEFAULT_DISCREPANCY_STATES,
) {
  return useInfiniteQuery({
    queryKey: queryKeys.discrepancyPages(states),
    queryFn: ({ pageParam, signal }) => fetchDiscrepancyPage(states, pageParam, signal),
    initialPageParam: FIRST_DISCREPANCY_PAGE,
    getNextPageParam: nextDiscrepancyPageParam,
    select: selectDiscrepancyList,
    refetchInterval: (query) => currentStatePollInterval(query.state.error, false),
  });
}

/**
 * Health, mode, active dataset, dataset freshness, the Google call count and recent sync
 * runs of every source, in the order google, linkedin, hubspot: `GET /sources` →
 * {@link Source}[] (AAP 0.5.5 `/sources` and the status bar's source chips).
 *
 * Refetches every {@link SOURCES_POLL_MS}, because the worker changes all of these on its
 * own schedule. {@link useRequestSync} invalidates it.
 *
 * Status codes: 200.
 *
 * @returns The query result, keyed by `queryKeys.sources`.
 */
export function useSources() {
  return useQuery(sourcesQueryOptions());
}

/**
 * Query options for every source's health, `GET /sources` → {@link Source}[]: the options
 * {@link useSources} observes and {@link usePrefetchActionsReads} prefetches. They poll every
 * {@link SOURCES_POLL_MS} while a component observes them; a prefetch reads once.
 *
 * @returns Options for `useQuery` or `prefetchQuery`, keyed by `queryKeys.sources`.
 */
export function sourcesQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.sources,
    queryFn: ({ signal }) => apiFetch<Source[]>('/sources', { signal }),
    refetchInterval: SOURCES_POLL_MS,
  });
}

/**
 * Query options for the read-only configuration, `GET /config` → {@link ConfigView}: the
 * options {@link useConfig} observes and {@link usePrefetchActionsReads} prefetches. The
 * result stays fresh for 5 minutes, because thresholds change only when the API restarts.
 *
 * @returns Options for `useQuery` or `prefetchQuery`, keyed by `queryKeys.config`.
 */
export function configQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.config,
    queryFn: ({ signal }) => apiFetch<ConfigView>('/config', { signal }),
    staleTime: LONG_STALE_MS,
  });
}

/**
 * The read-only configuration: `GET /config` → {@link ConfigView}. It carries the thresholds,
 * the window settings that `WindowPicker` reads (`window.presets`, `window.default_days`), the
 * HubSpot mapping with its resolved bindings, the white paper registry and `config_hash`.
 * The web client hard-codes no window length (AAP 0.5.5).
 *
 * It stays fresh for 5 minutes, because thresholds change only when the API restarts.
 *
 * Status codes: 200; 500 `config_error` when a configuration file or stored binding is
 * invalid.
 *
 * @returns The query result, keyed by `queryKeys.config`.
 */
export function useConfig() {
  return useQuery(configQueryOptions());
}

/**
 * Starts the `/actions` status bar's reads alongside the page's own `GET /recommendations`,
 * instead of after it (AAP 0.1.1: the five sections render in under 2 s).
 *
 * The status bar mounts only once the page has data, so its reads would otherwise wait for
 * the page. From an effect keyed on `runId`, this hook prefetches:
 *
 * - `GET /config` ({@link configQueryOptions}), for the window picker and the White papers
 *   section;
 * - `GET /sources` ({@link sourcesQueryOptions}), for the source chips;
 * - `GET /runs/latest` ({@link defaultRunQueryOptions}), only when `runId` addresses a run.
 *   Without one the page itself reads the latest default run, and the picker's default-run
 *   entry starts from that page's `run`; prefetching it then would create the entry before
 *   the page lands, read the same run twice and defeat the seed.
 *
 * It never reads the custom-run list, which waits until the user asks for it. A prefetch
 * registers no component, so nothing polls while the page shows its loading, first-start or
 * failure state; the status bar's hooks take the entries over once it mounts. An entry that
 * is still fresh is not read again. A failed prefetch is read again when those hooks mount,
 * and the prefetch itself never rejects.
 *
 * Call it from the page component, which renders only after the router's auth guard has
 * passed, so no protected read starts before the session is known.
 *
 * @param runId - The run `/actions?run=<id>` addresses; omitted for the latest default run.
 */
export function usePrefetchActionsReads(runId?: RunId): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    void queryClient.prefetchQuery(configQueryOptions());
    void queryClient.prefetchQuery(sourcesQueryOptions());
    if (runId !== undefined) {
      void queryClient.prefetchQuery(defaultRunQueryOptions(queryClient));
    }
  }, [queryClient, runId]);
}

/**
 * The cache entries of the reads a loaded `/actions` page shows whose failed refresh
 * {@link useActionsRefreshFailure} reports, in the order it checks them:
 *
 * 1. `queryKeys.recommendations(runId ?? 'default')`: the page itself, its cards, banners and
 *    overlay (the discrepancy count among them);
 * 2. `queryKeys.sources`: the status bar's source chips and their last sync times;
 * 3. `queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES)`: the unresolved list the C0 cards
 *    (`CampaignsSection`) and the held cards' blocker links (`BlockingDiscrepancies`) read;
 * 4. `queryKeys.config`: the window picker's presets and the White papers fallback header.
 *
 * Two reads of the page are left out. `queryKeys.runs.latest`: the window picker states its
 * failure itself, with a Retry of its own ("Could not read the latest default run"), so a
 * second notice would repeat it. The resolved discrepancy list: `CampaignsSection` reads it only
 * while a C0 card's discrepancy is missing from the unresolved list, from the same endpoint at
 * the same cadence, so an outage fails the unresolved read as well, and its entry outlives that
 * lookup, so a failure left in it would stay on screen after the lookup ended.
 *
 * @param runId - The run `/actions?run=<id>` addresses; omitted for the latest default run.
 * @returns The exact keys, most important first.
 */
function actionsRefreshKeys(runId: RunId | undefined): readonly QueryKey[] {
  return [
    queryKeys.recommendations(runId ?? 'default'),
    queryKeys.sources,
    queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
    queryKeys.config,
  ];
}

/**
 * Whether a refresh of the loaded `/actions` page failed, and with what: the {@link ApiError}
 * of the first entry of {@link actionsRefreshKeys} that holds data but whose last fetch failed,
 * or `null` while each of them is current (AAP 0.5.5: trust problems are surfaced before the
 * actions they affect).
 *
 * TanStack Query keeps `data` when a refetch fails, so every component reading one of these
 * entries keeps showing its last result, and a failed poll would otherwise go unnoticed: during
 * an outage the cards, the source chips' sync times and the discrepancy count would look
 * current. TanStack v5 keeps the entry's `error` while a later refetch of an entry with data
 * runs, and clears it on the next success, so the result neither flickers during a poll nor
 * outlives the recovery. Each polled entry recovers by itself once the API answers again;
 * `config`, which is not polled, clears on its next successful read (on refocus, on reconnect,
 * or when a new reader finds it stale). An entry without data is not reported: the component
 * reading it shows its own loading or failure state.
 *
 * It only reads the query cache, through `QueryCache.subscribe` and React's
 * `useSyncExternalStore`. It sends no request and registers no observer, so it never fetches,
 * polls or keeps an entry alive. Its snapshot is the `ApiError` object the entry holds, or
 * `null`, each the same reference until that entry's state changes, so its caller re-renders
 * only when the reported failure changes and a memoized child given it is not rebuilt.
 *
 * @param runId - The run `/actions?run=<id>` addresses; omitted for the latest default run.
 * @returns The first failed refresh among those reads, or `null`.
 *
 * @example
 * ```tsx
 * const refreshFailure = useActionsRefreshFailure(runId);
 * // While the API is down and the page holds data:
 * // ApiError "The server is temporarily unavailable. Try again shortly." (502)
 * ```
 */
export function useActionsRefreshFailure(runId?: RunId): ApiError | null {
  const queryCache = useQueryClient().getQueryCache();
  const subscribe = useCallback(
    (onStoreChange: () => void) => queryCache.subscribe(onStoreChange),
    [queryCache],
  );
  const getSnapshot = (): ApiError | null => {
    for (const queryKey of actionsRefreshKeys(runId)) {
      const state = queryCache.find({ queryKey, exact: true })?.state;
      if (state !== undefined && state.data !== undefined && state.error !== null) {
        return state.error;
      }
    }
    return null;
  };
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

// ---------------------------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------------------------

/** Variables of {@link useResolveDiscrepancy}: the discrepancy and the resolution to apply. */
export interface ResolveDiscrepancyVariables {
  /** The discrepancy to resolve. */
  id: DiscrepancyId;
  /** The resolution, the intended state for `record_intended`, and an optional note. */
  body: ResolveRequest;
}

/** Variables of {@link useFeedback}: the item and the feedback to record on it. */
export interface FeedbackVariables {
  /** The recommendation (item) the feedback is given on. */
  recommendationId: RecommendationId;
  /** `dismiss`, `snooze` or `done`, and an optional note. */
  body: FeedbackRequest;
}

/**
 * Refreshes everything a decision write can change: the recommendations pages, whose
 * overlay holds, discrepancy banner and historical marking are read live; the discrepancy
 * lists, because a decision can open, close or await a discrepancy; and the decision log.
 *
 * It first removes every recommendations page no screen shows (`type: 'inactive'`), for
 * example the `/actions` pages cached before the user moved to `/decisions`. Invalidating
 * such a page would only mark it stale, and a later visit would render its outdated overlay
 * until the refetch lands: a resolved discrepancy would flash its `role="alert"` banner
 * again. A removed page is read afresh before it renders, so it can show only the current
 * state. It then invalidates the three roots, so every shown (active) entry, a page on screen
 * included, refetches in place, and resolves once those refetches have settled.
 */
function invalidateDecisionState(queryClient: QueryClient): Promise<unknown> {
  queryClient.removeQueries({ queryKey: queryKeys.recommendationsAll, type: 'inactive' });
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.recommendationsAll }),
    queryClient.invalidateQueries({ queryKey: queryKeys.discrepanciesAll }),
    queryClient.invalidateQueries({ queryKey: queryKeys.decisionsAll }),
  ]);
}

/**
 * Signs the admin in: `POST /auth/login` with a {@link LoginRequest} → {@link Me}. On success
 * the API sets the session cookie, and the hook writes the user into `queryKeys.me`, so the
 * auth guard passes without another `GET /auth/me`.
 *
 * Status codes: 200; 401 `invalid_credentials` for an unknown email or a wrong password (one
 * message for both); 429 `rate_limited` from the 6th attempt within a minute, with
 * {@link ApiError.retryAfter} in seconds; 422 `validation_error` for an empty field.
 * `LoginPage` shows each. Invalidates nothing.
 *
 * @returns The mutation; call `mutate({ email, password })`.
 */
export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: LoginRequest) =>
      apiFetch<Me>('/auth/login', { method: 'POST', body }),
    onSuccess: (me) => {
      queryClient.setQueryData<Me>(queryKeys.me, me);
    },
  });
}

/**
 * Signs the admin out: `POST /auth/logout`. The API expires the session cookie.
 *
 * On success the whole cache is cleared with `queryClient.clear()`, so a cached
 * `queryKeys.me` can never let the auth guard pass after sign-out and no other screen's data
 * survives the session. A 401 `unauthenticated` (the session had already expired) clears the
 * cache too, because the session is gone either way. Other errors leave the cache intact,
 * since the user is still signed in.
 *
 * Status codes: 200 with the signed-out `{email}`; 401 `unauthenticated` without a valid
 * session.
 *
 * @returns The mutation; call `mutate()`. It resolves `void`.
 */
export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    // The API answers 200 with the signed-out `{email}`; nothing reads it, so the body is
    // discarded and the mutation resolves `void` whether the response is 200 or 204.
    mutationFn: async (): Promise<void> => {
      await apiFetch<Me>('/auth/logout', { method: 'POST' });
    },
    onSuccess: () => {
      queryClient.clear();
    },
    onError: (error) => {
      if (isApiError(error) && error.status === 401 && error.code === 'unauthenticated') {
        queryClient.clear();
      }
    },
  });
}

/**
 * Computes a custom-window run synchronously from stored data: `POST /runs` with a
 * {@link CreateRunRequest} → {@link Run} (201, `kind=custom`, AAP 0.5.3). The window is
 * calendar dates applied to each source's native days.
 *
 * On success the run is written into `queryKeys.runs.byId(run.id)`, so a {@link useRun} read
 * of it starts from that copy, `queryKeys.runs.customAll` is invalidated, and the run's page
 * is prefetched (`GET /recommendations?run_id=<id>`, {@link recommendationsQueryOptions}). The
 * mutation stays pending until both have settled, so the caller's own `onSuccess`, which
 * opens `/actions?run=<id>`, runs once the page is cached: the page renders at once, and the
 * caller's pending state covers the read. The prefetch never rejects. A failed page read
 * leaves its error in the cache, the mutation still succeeds, and `/actions?run=<id>` states
 * that failure as it would for any read. The run never becomes `/runs/latest`, so the default
 * run and its page stay as they are. A failed `POST /runs` reads no page.
 *
 * Status codes: 201; 409 `run_in_progress` while any process holds the analysis lock; 422
 * `end_before_start`, `end_after_default_end`, `too_long` (over `window.max_days`),
 * `too_short` or `invalid_window` for a window outside its bounds.
 *
 * @returns The mutation; call `mutate({ window_start, window_end })`.
 */
export function useCreateRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateRunRequest) => apiFetch<Run>('/runs', { method: 'POST', body }),
    onSuccess: (run) => {
      queryClient.setQueryData<Run>(queryKeys.runs.byId(run.id), run);
      return Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.runs.customAll }),
        queryClient.prefetchQuery(recommendationsQueryOptions(run.id)),
      ]);
    },
  });
}

/**
 * Records a decision on a campaign or shared budget as a patch: `POST /decisions` with a
 * {@link DecisionRequest} → {@link Decision} (201, AAP 0.5.2). The response carries the
 * subject's decision shape and its complete materialised expected state.
 *
 * On success it removes the recommendations pages no screen shows, because their overlay and
 * discrepancy banner are outdated and a later visit must read them afresh rather than flash
 * them, and invalidates `queryKeys.recommendationsAll`, `queryKeys.discrepanciesAll` and
 * `queryKeys.decisionsAll`, so every shown entry refetches before `mutateAsync` resolves.
 *
 * Status codes: 201; 404 `not_found` for an unknown subject or `recommendation_id`; 409
 * `subject_retired` when the subject's dataset, or a dataset the item read, is no longer
 * active, and `platform_state_unknown` for a subject with no decision that no entity sync
 * has reported yet; 422 for the patch rules, among them `empty_patch`, `no_change`,
 * `sub_minor_unit`, `field_not_allowed`, `shared_budget_amount`, `budget_changed`,
 * `untracked_subject` (a reported subject with no decision yet), `decision_not_offered` and
 * `subject_mismatch`.
 *
 * @returns The mutation; call `mutate({ subject, patch, recommendation_id?, note? })`.
 */
export function useRecordDecision() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DecisionRequest) =>
      apiFetch<Decision>('/decisions', { method: 'POST', body }),
    onSuccess: () => invalidateDecisionState(queryClient),
  });
}

/**
 * Applies one C0 resolution: `POST /discrepancies/{id}/resolve` with a {@link ResolveRequest}
 * → the updated {@link Discrepancy} (200, AAP 0.5.4.4). `platform_correct`, `adopt_platform`
 * and `acknowledge_removal` close it at once (`resolved`). `decision_correct` and
 * `record_intended` move it to `awaiting_platform`.
 *
 * On success, like {@link useRecordDecision}, it removes the recommendations pages no screen
 * shows and invalidates `queryKeys.recommendationsAll`, `queryKeys.discrepanciesAll` and
 * `queryKeys.decisionsAll`, because a resolution can write a decision and changes the
 * open-discrepancy holds and banner. After a resolution on `/decisions`, where no `/actions`
 * page is shown, returning to `/actions` reads the page afresh and never shows the
 * `role="alert"` banner of the discrepancy just resolved; after one on an `/actions` card, the
 * page on screen refetches in place.
 *
 * Status codes: 200; 404 `not_found` for an unknown discrepancy or subject; 409
 * `subject_retired` when its dataset is no longer active, `discrepancy_resolved` when it is
 * already closed, `platform_state_unknown` while the platform state to adopt is not yet
 * known; 422 `resolution_not_allowed` for a resolution not valid for its kind and shape, and
 * `intended_state_invalid` for an incomplete or ill-fitting `record_intended` state.
 *
 * @returns The mutation; call `mutate({ id, body: { resolution, intended_state?, note? } })`.
 */
export function useResolveDiscrepancy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: ResolveDiscrepancyVariables) =>
      apiFetch<Discrepancy>(`/discrepancies/${pathSegment(id)}/resolve`, {
        method: 'POST',
        body,
      }),
    onSuccess: () => invalidateDecisionState(queryClient),
  });
}

/**
 * Dismisses, snoozes or marks one item done: `POST /recommendations/{id}/feedback` with a
 * {@link FeedbackRequest} → {@link Feedback} (201). It suppresses the item's rule and entity
 * in later runs until the response's `until`, which is authoritative: the lengths are the
 * configurable `feedback.dismiss_days`, `feedback.snooze_days` and `feedback.done_days`
 * (defaults 30, 7 and 30 days). It also hides the item at once through the overlay. "Mark
 * done" is feedback only and never writes a decision.
 *
 * On success it invalidates `queryKeys.recommendationsAll`.
 *
 * Status codes: 201; 404 `not_found` for an unknown item; 409 `subject_retired` for a
 * historical item computed from replaced data; 422 `feedback_not_allowed` for a C0
 * discrepancy item, which must be resolved instead.
 *
 * @returns The mutation; call `mutate({ recommendationId, body: { kind, note? } })`.
 */
export function useFeedback() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ recommendationId, body }: FeedbackVariables) =>
      apiFetch<Feedback>(`/recommendations/${pathSegment(recommendationId)}/feedback`, {
        method: 'POST',
        body,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.recommendationsAll }),
  });
}

/**
 * Requests a sync of one source ("Sync now"): `POST /sources/{source}/sync` →
 * {@link SyncRequestView} (202). The worker checks for pending requests at every 30 s wake and
 * claims this one once the source's lane can run, so the worker's startup, or a lane still busy
 * with an earlier pass such as a long backfill or Google request, can defer it. The API itself
 * calls no platform.
 *
 * On success it invalidates `queryKeys.sources`, so the source shows its pending request. A
 * 409 `sync_pending` invalidates it too, because the screen that pressed "Sync now" had not yet
 * read the request already pending; any other error invalidates nothing. Both callbacks wait
 * for the invalidation, so the mutation settles, as success or as error, only once the refetch
 * of a shown sources query has settled: the refetched `sync_pending` is in the cache by then.
 *
 * Status codes: 202; 409 `sync_pending` while a request for that source is already pending;
 * 422 `validation_error` for an unknown source.
 *
 * @returns The mutation; call `mutate('google' | 'linkedin' | 'hubspot')`.
 */
export function useRequestSync() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (source: SourceName) =>
      apiFetch<SyncRequestView>(`/sources/${pathSegment(source)}/sync`, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.sources }),
    onError: async (error) => {
      if (isApiError(error) && error.code === 'sync_pending') {
        await queryClient.invalidateQueries({ queryKey: queryKeys.sources });
      }
    },
  });
}
