/**
 * The TanStack Query layer of the SPA: every read and write of `/api/v1` server state
 * (AAP 0.5.1 "Web API layer", 0.5.3).
 *
 * AAP 0.11.1 routes all server state through this module: components, `src/router.tsx` and
 * `src/main.tsx` call the hooks and helpers below and never fetch. Network access comes only
 * from {@link apiFetch} in `./client`, which adds the session cookie, the CSRF header on every
 * mutation and the error mapping. ESLint keeps `fetch` out of every other module and the
 * client out of feature code. Consumers import {@link ApiError}, {@link isApiError} and
 * {@link ApiErrorCode} from here, never from `./client`.
 *
 * Conventions every hook follows:
 *
 * - **Keys.** Each query uses a key from {@link queryKeys}. Keys are plain, serialisable
 *   tuples, so a root such as `queryKeys.decisionsAll` prefix-matches every filtered variant
 *   below it.
 * - **Cancellation.** Every `queryFn` passes TanStack's `signal` to `apiFetch`, so an
 *   unmounted screen or a superseded key aborts its request.
 * - **Errors.** {@link apiFetch} rejects only with {@link ApiError}, so every hook's `error`
 *   is `ApiError | null` (see the `Register` augmentation below).
 * - **Invalidation.** After a successful write, a mutation invalidates the `*All` roots of
 *   everything the write can change. Its `onSuccess` returns that promise, so `mutateAsync`
 *   resolves only after the refetch has started. Mutations set no `retry`. The app's
 *   `QueryClient` (`src/main.tsx`) does not retry them, because a POST is not idempotent from
 *   the UI's point of view.
 * - **Polling.** {@link useDefaultRun} and {@link useRecommendations} without a run id poll
 *   every {@link FIRST_RUN_POLL_MS} while no default run has succeeded yet. That lets the
 *   first-start screen fill in by itself. {@link useSources} polls every
 *   {@link SOURCES_POLL_MS}.
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
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query';
import type { QueryClient } from '@tanstack/react-query';

import { apiFetch, isApiError } from './client';
import type { ApiError } from './client';
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
 * (AAP 0.11.2 "First start"). Polling shows that run without a reload. It stops once the run
 * arrives.
 */
export const FIRST_RUN_POLL_MS = 15_000;

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
 * The page size {@link useDiscrepancies} requests: the largest `limit` that
 * `GET /discrepancies` accepts (`MAX_LIMIT` in
 * `backend/src/ads_engine/api/routes/discrepancies.py`).
 */
const DISCREPANCY_PAGE_LIMIT = 500;

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
 * The `refetchInterval` of the default-run reads, from the query's last error:
 * {@link FIRST_RUN_POLL_MS} while it says no default run exists yet, and no polling
 * otherwise. After the run arrives, `error` is `null` and polling stops.
 *
 * It takes the error rather than the query, and each hook calls it from an inline
 * `(query) => …` callback. A standalone callback with its own parameter type would become an
 * inference site for `useQuery`'s error type and widen every hook's `error` to `unknown`.
 */
function firstRunPollInterval(error: unknown): number | false {
  return isAwaitingFirstRun(error) ? FIRST_RUN_POLL_MS : false;
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
 * | `discrepancies(states)` | `GET /discrepancies?state=…` |
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
 * The signed-in user: `GET /auth/me` → {@link Me}, for the top bar's email. See
 * {@link meQueryOptions} for caching, retries and status codes.
 *
 * @returns The query result; `error` is a 401 `unauthenticated` {@link ApiError} when
 *   signed out.
 */
export function useMe() {
  return useQuery(meQueryOptions());
}

/**
 * The latest **succeeded default** run: `GET /runs/latest` → {@link Run} (AAP 0.5.3 run
 * selection). A custom run, even one whose window equals the default, never stands in for
 * it, and a running or failed default run never replaces the last good one.
 *
 * Status codes: 200; 404 `not_found` ("No analysis run yet") before the first default run
 * succeeds. While that 404 persists the query refetches every {@link FIRST_RUN_POLL_MS}, so
 * the first-start screen fills in by itself (e2e `first-start.spec.ts`).
 *
 * @returns The query result, keyed by `queryKeys.runs.latest`.
 */
export function useDefaultRun() {
  return useQuery({
    queryKey: queryKeys.runs.latest,
    queryFn: ({ signal }) => apiFetch<Run>('/runs/latest', { signal }),
    refetchInterval: (query) => firstRunPollInterval(query.state.error),
  });
}

/**
 * One run by id, default or custom, in any status: `GET /runs/{id}` → {@link Run}. The
 * `/actions?run=<id>` page reads a custom run's header through it. With `runId` `undefined`
 * the query is disabled and sends nothing.
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
 * Status codes: 200, an empty list when there are none; 422 `validation_error` for a `limit`
 * outside 1–100.
 *
 * @param limit - How many runs to list, 1 to 100; 20 when omitted, the API's own default.
 * @returns The query result, keyed by `queryKeys.runs.custom(limit)`.
 */
export function useCustomRuns(limit: number = DEFAULT_CUSTOM_RUNS_LIMIT) {
  return useQuery({
    queryKey: queryKeys.runs.custom(limit),
    queryFn: ({ signal }) =>
      apiFetch<Run[]>('/runs', { query: { kind: 'custom', limit }, signal }),
  });
}

/**
 * The five sections of one run with the current-state overlay:
 * `GET /recommendations[?run_id=]` → {@link RecommendationsPage}.
 *
 * Without `runId` the request carries no `run_id`, and the API serves the latest succeeded
 * default run. That page polls every {@link FIRST_RUN_POLL_MS} while no default run exists
 * yet (404 `not_found`), like {@link useDefaultRun}. With `runId`, used for a custom window
 * at `/actions?run=<id>`, the request carries `run_id` and never polls.
 *
 * The page's order (five sections in `SECTION_ORDER`, ranked groups and items) comes from the
 * API exactly as the run stored it. Each item's `current` and the page's `overlay` and
 * `banners` are read-time state, computed when the API serves the request: live
 * discrepancies, source modes, freshness, feedback and historical-only marking (AAP 0.3.2).
 * Every write that changes them invalidates this key's root.
 *
 * Status codes: 200; 404 `not_found` for an unknown run or before the first default run; 409
 * `run_not_ready` for a run that is still `running` or has `failed`.
 *
 * @param runId - The run to show; omitted for the latest default run.
 * @returns The query result, keyed by `queryKeys.recommendations(runId ?? 'default')`.
 */
export function useRecommendations(runId?: RunId) {
  return useQuery({
    queryKey: queryKeys.recommendations(runId ?? 'default'),
    queryFn: ({ signal }) =>
      apiFetch<RecommendationsPage>('/recommendations', {
        query: runId === undefined ? undefined : { run_id: runId },
        signal,
      }),
    refetchInterval: (query) =>
      runId === undefined ? firstRunPollInterval(query.state.error) : false,
  });
}

/**
 * The decision log of the active datasets, newest first: `GET /decisions` → {@link Decision}[]
 * (AAP 0.5.5 `DecisionLog`). It sends `subject_type` and `subject_id` only when the filter
 * sets them. The API returns at most its default 200 rows.
 *
 * Status codes: 200; 422 `invalid_query` when `subject_id` comes without `subject_type`.
 * Recording a decision or resolving a discrepancy invalidates every variant.
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
  });
}

/**
 * Reads every discrepancy in `states`, oldest first, following the `after` keyset cursor
 * page by page.
 *
 * `GET /discrepancies` returns at most `limit` rows per page (200 by default) and names the
 * next page only in a `Link` response header, which `apiFetch` does not expose. A single
 * default page would silently truncate a long list, and discrepancies are resolved before
 * anything else is trusted (AAP 0.5.4.2 P1), so they must all be shown. Pages are therefore
 * requested at the API's maximum size, and while a page comes back full the next one starts
 * after its last discrepancy's id: the same cursor the `Link` header carries. A shorter page
 * ends the list. The API can leave out of a full page a discrepancy whose subject it cannot
 * load, so a page short for that reason also ends it. The cursor-progress check stops a
 * misbehaving server from looping the read forever.
 */
async function fetchAllDiscrepancies(
  states: readonly DiscrepancyState[],
  signal: AbortSignal,
): Promise<Discrepancy[]> {
  const all: Discrepancy[] = [];
  let after: DiscrepancyId | undefined;
  for (;;) {
    const page = await apiFetch<Discrepancy[]>('/discrepancies', {
      query: { state: states, limit: DISCREPANCY_PAGE_LIMIT, after },
      signal,
    });
    all.push(...page);
    const last = page.at(-1);
    if (page.length < DISCREPANCY_PAGE_LIMIT || last === undefined || last.id === after) {
      return all;
    }
    after = last.id;
  }
}

/**
 * The discrepancies of the active datasets in `states`, oldest `detected_at` first, the
 * order C0 items are pinned in: `GET /discrepancies?state=…` → {@link Discrepancy}[]
 * (AAP 0.5.5 `DiscrepancyResolver`). Each state is sent as its own repetition of the
 * parameter (`state=open&state=awaiting_platform`). An empty list sends no `state`, which the
 * API reads as its default unresolved states. Every page is read, so the list is never
 * truncated at the API's page size.
 *
 * Status codes: 200; 422 `validation_error` for an unknown state. Recording a decision or
 * resolving a discrepancy invalidates every variant.
 *
 * @param states - The states to list; `open` and `awaiting_platform` when omitted.
 * @returns The query result, keyed by `queryKeys.discrepancies(states)`.
 */
export function useDiscrepancies(
  states: readonly DiscrepancyState[] = DEFAULT_DISCREPANCY_STATES,
) {
  return useQuery({
    queryKey: queryKeys.discrepancies(states),
    queryFn: ({ signal }) => fetchAllDiscrepancies(states, signal),
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
  return useQuery({
    queryKey: queryKeys.sources,
    queryFn: ({ signal }) => apiFetch<Source[]>('/sources', { signal }),
    refetchInterval: SOURCES_POLL_MS,
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
  return useQuery({
    queryKey: queryKeys.config,
    queryFn: ({ signal }) => apiFetch<ConfigView>('/config', { signal }),
    staleTime: LONG_STALE_MS,
  });
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
 * Invalidates everything a decision write can change: the recommendations pages, whose
 * overlay holds, discrepancy banner and historical marking are read live; the discrepancy
 * lists, because a decision can open, close or await a discrepancy; and the decision log.
 * Resolves once every refetch has started.
 */
function invalidateDecisionState(queryClient: QueryClient): Promise<unknown> {
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
 * On success the run is written into `queryKeys.runs.byId(run.id)`, so `/actions?run=<id>`
 * opens without another request, and `queryKeys.runs.customAll` is invalidated. The run never
 * becomes `/runs/latest`, so the default run and its page stay as they are.
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
      return queryClient.invalidateQueries({ queryKey: queryKeys.runs.customAll });
    },
  });
}

/**
 * Records a decision on a campaign or shared budget as a patch: `POST /decisions` with a
 * {@link DecisionRequest} → {@link Decision} (201, AAP 0.5.2). The response carries the
 * subject's decision shape and its complete materialised expected state.
 *
 * On success it invalidates `queryKeys.recommendationsAll`, `queryKeys.discrepanciesAll` and
 * `queryKeys.decisionsAll`.
 *
 * Status codes: 201; 404 `not_found` for an unknown subject or `recommendation_id`; 409
 * `subject_retired` when the subject's dataset, or a dataset the item read, is no longer
 * active; 422 for the patch rules, among them `empty_patch`, `no_change`, `sub_minor_unit`,
 * `field_not_allowed`, `shared_budget_amount`, `budget_changed`, `untracked_subject`,
 * `decision_not_offered` and `subject_mismatch`.
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
 * On success it invalidates `queryKeys.recommendationsAll`, `queryKeys.discrepanciesAll` and
 * `queryKeys.decisionsAll`, like {@link useRecordDecision}, because a resolution can write a
 * decision and changes the open-discrepancy holds and banner.
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
 * in later runs until `until` (dismiss 30 days, snooze 7, done 30), and hides the item at
 * once through the overlay. "Mark done" is feedback only and never writes a decision.
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
 * {@link SyncRequestView} (202). The worker picks the request up on its next 30 s wake; the
 * API itself calls no platform.
 *
 * On success it invalidates `queryKeys.sources`, so the source shows its pending request.
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
  });
}
