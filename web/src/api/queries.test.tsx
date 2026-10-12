/**
 * Unit tests for src/api/queries.ts, the TanStack Query layer through which the SPA reads and
 * writes every piece of `/api/v1` server state (AAP 0.5.1 "Web API layer", 0.5.3, 0.11.1).
 *
 * Each hook runs under `renderHook` inside the provider {@link createWrapper} builds, whose
 * client never retries and never garbage-collects, so a failure surfaces on the first response
 * and a seeded entry stays readable. The global `fetch` is replaced with `vi.stubGlobal` by a
 * typed `vi.fn` that answers with real `Response`s built by {@link json}. Every request is read
 * back with `new URL(…, 'http://localhost')`, so the tests assert `pathname` and
 * `searchParams` rather than string layouts. The cases cover:
 *
 * - the query keys, the polling and freshness constants and the re-exported error API;
 * - every query hook's request (method, `/api/v1` path, query parameters, abort signal), its
 *   cache key, the `skipToken` placeholder of `useRun(undefined)`, the 5-minute freshness of the
 *   user and configuration reads, cancellation on unmount, the poll interval each read chooses
 *   after a success, a missing first run, a transient failure and a final error, and the polls
 *   themselves under fake timers;
 * - the query-option factories, the latest default run seeded from a fresh cached default page
 *   (never from a page addressed by run id), the custom-run list held back while disabled and
 *   forgotten on request (only its own limit, aborting a read in flight, and read once more by
 *   an observer enabled again), and
 *   the `/actions` prefetch, which reads the configuration and sources (and the latest default
 *   run only for an addressed run) without polling and never reads the custom-run list;
 * - `useActionsRefreshFailure`: the failed refresh of the page, the sources, the unresolved
 *   discrepancies or the configuration while the entry keeps its data, page first, the same
 *   error object while the entry is read again and `null` after its next success; never a
 *   failed first read, the latest default run, the resolved list or another run's page; no
 *   request, no observer, and no re-render while the reported failure stays the same;
 * - `useDiscrepancies` paging: a first page at `limit=500`, every `Link: rel="next"` page
 *   followed exactly as named however short the page, a repeated page rejected, one restart
 *   from the first page after a 422 `invalid_cursor`, and its `enabled: false` option, which
 *   sends nothing and never polls yet returns what another reader cached under the same key;
 * - `useDiscrepancyPages`: a first page at `limit=50` with its `X-Total-Count`, each "Show more"
 *   following the `Link` exactly as named, empty pages followed within one load, a link repeated
 *   within one load or across loads rejected, one restart after `invalid_cursor` that re-reads
 *   what was shown, polling that re-reads only the loaded pages, a failed refetch keeping them,
 *   abort on unmount, and invalidation by both decision-state mutations;
 * - the current-state polling of `useDecisions` and `useDiscrepancies`;
 * - the polling lifecycle of all six polling hooks: no request while the document is hidden or
 *   `focusManager` reports the page unfocused, an immediate refetch and the usual cadence once
 *   it is shown again, and no request once the last observing component has unmounted;
 * - every mutation's request (CSRF header, JSON body, encoded path) and its cache effects: the
 *   exact `invalidateQueries` keys (spied), the seeded and invalidated entries, the keys left
 *   alone, nothing touched on failure, and `mutateAsync` of a decision write or resolution
 *   still pending while the refetch of a shown list is held, resolving only once it has landed;
 * - every write while `onlineManager` reports the browser offline: it fails at once with status
 *   0 `network_error` and its message, is never paused, invalidates nothing, and sends nothing
 *   more once the browser is back online.
 *
 * The file is self-contained. It imports only `./queries`, `./client` and `./types`: the shared
 * helpers in `src/test/` import this folder, so importing them here would close a cycle.
 * Payloads are minimal inline objects, because `apiFetch` does not validate response shapes;
 * each is checked with `satisfies` against the fields of its `./types` type that it carries.
 * The discriminators a payload does carry stay ones the API can produce: a decision's shape fits
 * its subject, and a discrepancy's kind and shape form a pair C0 raises.
 */
import {
  QueryClient,
  QueryClientProvider,
  focusManager,
  onlineManager,
} from '@tanstack/react-query';
import type { QueryKey } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';

import { ApiError as ClientApiError, isApiError as clientIsApiError } from './client';
import {
  ApiError,
  CURRENT_STATE_POLL_MS,
  DEFAULT_DISCREPANCY_STATES,
  DISCREPANCY_PAGE_SIZE,
  FIRST_RUN_POLL_MS,
  SOURCES_POLL_MS,
  WRITE_NETWORK_MODE,
  configQueryOptions,
  defaultRunQueryOptions,
  isApiError,
  isAwaitingFirstRun,
  meQueryOptions,
  queryKeys,
  sourcesQueryOptions,
  useActionsRefreshFailure,
  useConfig,
  useCreateRun,
  useCustomRuns,
  useDecisions,
  useDefaultRun,
  useDiscrepancies,
  useDiscrepancyPages,
  useFeedback,
  useForgetCustomRuns,
  useHasCachedCustomRuns,
  useLogin,
  useLogout,
  useMe,
  usePrefetchActionsReads,
  useRecommendations,
  useRecordDecision,
  useRequestSync,
  useResolveDiscrepancy,
  useRun,
  useSources,
} from './queries';
import type {
  ConfigView,
  Decision,
  DecisionRequest,
  DecisionShape,
  Discrepancy,
  DiscrepancyKind,
  DiscrepancyState,
  Feedback,
  FeedbackRequest,
  Item,
  Me,
  RecommendationsPage,
  ResolveRequest,
  Run,
  RunId,
  Source,
  SubjectIn,
  SyncRequestView,
} from './types';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * The signature of the browser's global fetch function, written out because ESLint keeps that
 * identifier inside src/api/client.ts.
 */
type FetchImpl = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** The stubbed global fetch function. */
type FetchMock = Mock<FetchImpl>;

/** Builds the response to one request from its URL and `RequestInit`. */
type Responder = (url: string, init: RequestInit) => Response | Promise<Response>;

/** The provider component `renderHook` wraps each hook in. */
type Wrapper = (props: { children: ReactNode }) => ReactNode;

/** A query client and the provider that supplies it, as {@link createWrapper} returns them. */
interface WrapperSetup {
  /** The client under the provider; spy on it, seed it or read it. */
  queryClient: QueryClient;
  /** Renders `children` inside `<QueryClientProvider client={queryClient}>`. */
  wrapper: Wrapper;
}

/** One request as the stub received it. */
interface SentRequest {
  /** `init.method`; the client always sets it, `GET` included. */
  method: string | undefined;
  /** The URL the client passed, as a string. */
  url: string;
  /** {@link url} parsed against `http://localhost`, for `pathname` and `searchParams`. */
  parsed: URL;
  /** `init.headers`, which the client always builds as a plain record. */
  headers: Record<string, string>;
  /** `init.body`, the JSON text of a mutation, `undefined` without one. */
  body: BodyInit | null | undefined;
  /** `init.signal`. */
  signal: AbortSignal | null | undefined;
}

/** Ids of the inline payloads, typed as `./types` names them. */
const DEFAULT_RUN_ID: Run['id'] = 101;
const CUSTOM_RUN_ID: Run['id'] = 202;
const ITEM_ID: Item['id'] = 5001;
const C0_ITEM_ID: Item['id'] = 5000;
const DISCREPANCY_ID: Discrepancy['id'] = 61;
const CAMPAIGN_ID: SubjectIn['id'] = 301;
const BUDGET_ID: SubjectIn['id'] = 402;

/** The 5-minute freshness of `GET /auth/me` and `GET /config`. */
const LONG_STALE_MS = 5 * 60_000;

/** Every `QueryClient` a test created, cleared after it so no timer outlives the test. */
const clients: QueryClient[] = [];

/** The app's default freshness (`QUERY_STALE_MS` in `src/main.tsx`), for {@link createWrapper}. */
const APP_STALE_MS = 30_000;

/**
 * A query client that never retries and never garbage-collects, and the provider for it.
 *
 * @param staleTime - The default freshness of every query; `0`, TanStack's default, when
 *   omitted. Pass {@link APP_STALE_MS} to reuse cached entries as the app does.
 * @returns The client and a `wrapper` for `renderHook`.
 */
function createWrapper(staleTime?: number): WrapperSetup {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: Infinity, staleTime },
      mutations: { retry: false },
    },
  });
  clients.push(queryClient);
  function wrapper({ children }: { children: ReactNode }): ReactNode {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return { queryClient, wrapper };
}

/**
 * Renders `hook` inside the provider of `setup`.
 *
 * @param hook - The hook (or hooks) under test.
 * @param setup - The client and provider; a fresh {@link createWrapper} when omitted, so a
 *   second mount can share the first one's cache.
 * @returns `renderHook`'s result plus the client and wrapper.
 */
function renderWithWrapper<Result>(hook: () => Result, setup: WrapperSetup = createWrapper()) {
  return { ...setup, ...renderHook(hook, { wrapper: setup.wrapper }) };
}

/**
 * Replaces the global fetch function with a mock that answers through `responder`.
 *
 * @param responder - Builds each response.
 * @returns The mock, whose calls record every request.
 */
function stubFetch(responder: Responder): FetchMock {
  const impl = vi.fn<FetchImpl>(async (input, init) => responder(String(input), init ?? {}));
  vi.stubGlobal('fetch', impl);
  return impl;
}

/**
 * A JSON response.
 *
 * @param status - The HTTP status.
 * @param body - The value to serialise.
 * @param headers - Extra response headers.
 * @returns A fresh `Response`.
 */
function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

/**
 * The API's error envelope.
 *
 * @param code - `error.code`.
 * @param message - `error.message`.
 * @returns `{"error": {"code", "message"}}`.
 */
function envelope(code: string, message: string): { error: { code: string; message: string } } {
  return { error: { code, message } };
}

/**
 * Parses a request URL the way the browser resolves it against the SPA's origin.
 *
 * @param input - The URL the client passed.
 * @returns The parsed URL.
 */
function parseUrl(input: RequestInfo | URL): URL {
  return new URL(String(input), 'http://localhost');
}

/**
 * A 418 the hooks never expect, naming the request no route or sequence step answered, so an
 * unexpected request fails its test with a readable message.
 *
 * @param url - The unanswered request's URL.
 * @param init - Its `RequestInit`, for the method.
 * @returns An `unexpected_request` error response.
 */
function unexpected(url: string, init: RequestInit): Response {
  return json(418, envelope('unexpected_request', `${init.method ?? 'GET'} ${url}`));
}

/**
 * A responder that answers `METHOD pathname` from `table`, whatever the query string. Each
 * test asserts the query parameters it cares about on the recorded request.
 *
 * @param table - Responses keyed by method and path, for example `GET /api/v1/sources`.
 * @returns The responder.
 */
function routes(table: Record<string, () => Response | Promise<Response>>): Responder {
  return (url, init) => {
    const respond = table[`${init.method ?? 'GET'} ${parseUrl(url).pathname}`];
    return respond === undefined ? unexpected(url, init) : respond();
  };
}

/**
 * A responder that answers the n-th request with the n-th step.
 *
 * @param steps - One responder per expected request, in order.
 * @returns The responder.
 */
function sequence(...steps: Responder[]): Responder {
  let next = 0;
  return (url, init) => {
    const step = steps[next];
    next += 1;
    return step === undefined ? unexpected(url, init) : step(url, init);
  };
}

/**
 * Every request the stub received, in order.
 *
 * @param mock - The stubbed fetch function.
 * @returns The requests.
 */
function sent(mock: FetchMock): SentRequest[] {
  return mock.mock.calls.map(([input, init]) => ({
    method: init?.method,
    url: String(input),
    parsed: parseUrl(input),
    headers: (init?.headers ?? {}) as Record<string, string>,
    body: init?.body,
    signal: init?.signal,
  }));
}

/**
 * The single request the stub received.
 *
 * @param mock - The stubbed fetch function.
 * @returns The request.
 * @throws Error when there was not exactly one.
 */
function onlyRequest(mock: FetchMock): SentRequest {
  const requests = sent(mock);
  const [request] = requests;
  if (requests.length !== 1 || request === undefined) {
    throw new Error(`expected one request, got ${String(requests.length)}`);
  }
  return request;
}

/**
 * The query parameters of a request as `[name, value]` pairs, in the order sent.
 *
 * @param request - A recorded request.
 * @returns Its parameters.
 */
function params(request: SentRequest): [string, string][] {
  return [...request.parsed.searchParams.entries()];
}

/**
 * What `promise` rejects with.
 *
 * @param promise - A promise expected to reject.
 * @returns The rejection reason.
 * @throws Error when the promise resolves.
 */
async function rejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error('expected the promise to reject');
}

/**
 * The poll interval the query under `queryKey` chooses for its current state, as its
 * observer's `refetchInterval` resolves it after each fetch.
 *
 * @param client - The query client.
 * @param queryKey - The exact key.
 * @returns The interval in milliseconds, or `false` when polling stops.
 */
function pollInterval(client: QueryClient, queryKey: QueryKey): number | false | undefined {
  const query = client.getQueryCache().find({ queryKey, exact: true });
  const observer = query?.observers[0];
  if (query === undefined || observer === undefined) {
    throw new Error(`no observed query under ${JSON.stringify(queryKey)}`);
  }
  const option = observer.options.refetchInterval;
  return typeof option === 'function' ? option(query) : option;
}

/**
 * Whether the cache entry under `queryKey` is marked invalidated.
 *
 * @param client - The query client.
 * @param queryKey - The exact key.
 * @returns `isInvalidated`, `undefined` when the key has no entry.
 */
function invalidated(client: QueryClient, queryKey: QueryKey): boolean | undefined {
  return client.getQueryState(queryKey)?.isInvalidated;
}

/**
 * Spies on `queryClient.invalidateQueries`, still calling through, so a test can assert the
 * exact keys a mutation invalidates.
 *
 * @param client - The query client.
 * @returns The spy.
 */
function spyOnInvalidate(client: QueryClient) {
  return vi.spyOn(client, 'invalidateQueries');
}

/**
 * The `queryKey` of every `invalidateQueries` call the spy saw, in call order.
 *
 * @param spy - A spy from {@link spyOnInvalidate}.
 * @returns The keys; `undefined` for a call without one.
 */
function invalidatedKeys(spy: ReturnType<typeof spyOnInvalidate>): unknown[] {
  return spy.mock.calls.map(([filters]) => filters?.queryKey);
}

/**
 * Seeds one cache entry per key, so a test can see which a mutation invalidates.
 *
 * @param client - The query client.
 * @param keys - The keys to seed.
 */
function seed(client: QueryClient, keys: readonly QueryKey[]): void {
  for (const key of keys) {
    client.setQueryData(key, { seeded: key });
  }
}

/** A promise the test fulfils by hand, as {@link deferred} returns it. */
interface Deferred<Value> {
  /** Pending until {@link Deferred.resolve} is called. */
  promise: Promise<Value>;
  /** Fulfils {@link Deferred.promise} with `value`. */
  resolve: (value: Value) => void;
}

/**
 * A promise that stays pending until the test fulfils it, so a stubbed response can be held
 * while the test asserts what is true with its request still in flight.
 *
 * @returns The pending promise and the function that fulfils it.
 */
function deferred<Value>(): Deferred<Value> {
  // The Promise executor runs synchronously, so this is replaced before it can be returned.
  let resolve: Deferred<Value>['resolve'] = () => {
    throw new Error('the Promise executor has not run');
  };
  const promise = new Promise<Value>((fulfil) => {
    resolve = fulfil;
  });
  return { promise, resolve };
}

/**
 * Lets one macrotask turn pass inside `act`: every promise callback queued before it runs, and
 * so does TanStack's batched notification on its zero-delay timer, which re-renders the hook.
 */
async function nextMacrotask(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
  });
}

/**
 * Every request the stub received as `METHOD pathname`, in order.
 *
 * @param mock - The stubbed fetch function.
 * @returns One `GET /api/v1/decisions`-style line per request.
 */
function sentPaths(mock: FetchMock): string[] {
  return sent(mock).map((request) => `${request.method ?? ''} ${request.parsed.pathname}`);
}

// ---------------------------------------------------------------------------------------------
// Inline payloads
// ---------------------------------------------------------------------------------------------

/** The signed-in admin. */
function mePayload(): Me {
  return { email: 'admin@example.com' };
}

/**
 * A run carrying only the fields the hooks and assertions read.
 *
 * @param id - The run id.
 * @param kind - `default` or `custom`.
 * @returns A fresh run payload.
 */
function runPayload(id: Run['id'] = DEFAULT_RUN_ID, kind: Run['kind'] = 'default') {
  return { id, kind, status: 'succeeded' } satisfies Pick<Run, 'id' | 'kind' | 'status'>;
}

/**
 * A recommendations page with no sections.
 *
 * @param runId - The run the page belongs to.
 * @param kind - That run's kind.
 * @returns A fresh page payload.
 */
function pagePayload(runId: Run['id'] = DEFAULT_RUN_ID, kind: Run['kind'] = 'default') {
  return { run: runPayload(runId, kind), sections: [] } satisfies {
    run: Pick<Run, 'id' | 'kind' | 'status'>;
    sections: RecommendationsPage['sections'];
  };
}

/** The decision shapes of a campaign subject; a Google shared budget is decided as `budget`. */
type CampaignShape = Exclude<DecisionShape, 'budget'>;

/**
 * The trailing `kind` and `shape` arguments of {@link discrepancyPayload}, paired as C0 raises
 * them (the backend's `RESOLUTIONS` table): a `status` discrepancy only on a campaign, because a
 * shared budget has no status, and every other kind on any shape.
 */
type DiscrepancyKindAndShape =
  | [kind?: 'status', shape?: CampaignShape]
  | [kind: Exclude<DiscrepancyKind, 'status'>, shape?: DecisionShape];

/**
 * A decision-log row whose shape fits its subject: `own_budget` or `shared_member` for a
 * campaign, and `budget` for a shared budget, whose row also carries its `null` status.
 *
 * @param id - The decision id.
 * @param origin - How the decision was written.
 * @param shape - The subject's decision shape; an own-budget campaign's when omitted.
 * @returns A fresh decision payload.
 */
function decisionPayload(
  id: Decision['id'],
  origin: Decision['origin'] = 'baseline',
  shape: DecisionShape = 'own_budget',
) {
  if (shape === 'budget') {
    return { id, shape, status: null, origin } satisfies Pick<
      Decision,
      'id' | 'shape' | 'status' | 'origin'
    >;
  }
  return { id, shape, origin } satisfies Pick<Decision, 'id' | 'shape' | 'origin'>;
}

/**
 * A discrepancy of a kind its subject's shape can raise ({@link DiscrepancyKindAndShape}).
 *
 * @param id - The discrepancy id.
 * @param state - Its state.
 * @param kind - `status`, `budget`, `untracked` or `missing`; `status` when omitted.
 * @param shape - The subject's decision shape; an own-budget campaign's when omitted.
 * @returns A fresh discrepancy payload.
 */
function discrepancyPayload(
  id: Discrepancy['id'],
  state: Discrepancy['state'] = 'open',
  ...[kind = 'status', shape = 'own_budget']: DiscrepancyKindAndShape
) {
  return { id, kind, shape, state } satisfies Pick<Discrepancy, 'id' | 'kind' | 'shape' | 'state'>;
}

/**
 * Discrepancies with consecutive ids.
 *
 * @param first - The first id.
 * @param count - How many to build.
 * @returns `count` fresh open `status` discrepancies on own-budget campaigns, ids `first` upwards.
 */
function discrepancies(first: Discrepancy['id'], count: number) {
  return Array.from({ length: count }, (_, offset) => discrepancyPayload(first + offset));
}

/** The three sources, in the API's order. */
function sourcesPayload() {
  return [
    { source: 'google' },
    { source: 'linkedin' },
    { source: 'hubspot' },
  ] satisfies Pick<Source, 'source'>[];
}

/** The configuration, reduced to its hash. */
function configPayload() {
  return { config_hash: 'cfg-0001' } satisfies Pick<ConfigView, 'config_hash'>;
}

/**
 * A recorded feedback.
 *
 * @param kind - `dismiss`, `snooze` or `done`.
 * @returns A fresh feedback payload on {@link ITEM_ID}.
 */
function feedbackPayload(kind: Feedback['kind']) {
  return { id: 9001, recommendation_id: ITEM_ID, kind } satisfies Pick<
    Feedback,
    'id' | 'recommendation_id' | 'kind'
  >;
}

/**
 * The `Link` header the API sends on a page of the default-state discrepancy list whose last
 * read row is `after`.
 *
 * @param after - The id of the last discrepancy row the page read.
 * @returns The header value, for example
 *   `</api/v1/discrepancies?state=open&state=awaiting_platform&limit=500&after=53>; rel="next"`.
 */
function discrepancyLink(after: number): string {
  const query = DEFAULT_DISCREPANCY_STATES.map((state) => `state=${state}`).join('&');
  return `</api/v1/discrepancies?${query}&limit=500&after=${String(after)}>; rel="next"`;
}

/** The first request of every `useDiscrepancyPages` load from the start of the list. */
const FIRST_PAGED = '/api/v1/discrepancies?state=open&state=awaiting_platform&limit=50';

/**
 * The headers of one `useDiscrepancyPages` page: its `X-Total-Count`, and the `Link` to the
 * next page when `after` is given.
 *
 * @param total - The list's total.
 * @param after - The id of the last row the page read, or `undefined` for the last page.
 * @returns The response headers.
 */
function pagedHeaders(total: number, after?: number): Record<string, string> {
  const headers: Record<string, string> = { 'X-Total-Count': String(total) };
  if (after !== undefined) {
    headers.Link = `<${FIRST_PAGED}&after=${String(after)}>; rel="next"`;
  }
  return headers;
}

afterEach(() => {
  // Inside act: this runs before Testing Library unmounts the hook under test, and a mounted
  // `useActionsRefreshFailure` reads the query cache directly, so clearing it re-renders the hook.
  act(() => {
    for (const client of clients.splice(0)) {
      client.clear();
    }
  });
  // The polling lifecycle cases take the page out of view through a spied
  // `document.visibilityState` getter or TanStack's `focusManager`. Both go back to the
  // browser's own state, so no later test starts with its polls suspended.
  vi.restoreAllMocks();
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.unstubAllGlobals();
});


// ---------------------------------------------------------------------------------------------
// Re-exports, constants and keys
// ---------------------------------------------------------------------------------------------

describe('re-exports and constants', () => {
  it('re-exports the client error class and guard unchanged', () => {
    expect(ApiError).toBe(ClientApiError);
    expect(isApiError).toBe(clientIsApiError);
    expect(isApiError(new ClientApiError(409, 'run_in_progress', 'x'))).toBe(true);
    expect(isApiError(new Error('x'))).toBe(false);
  });

  it('polls at the documented cadences', () => {
    expect(FIRST_RUN_POLL_MS).toBe(15_000);
    expect(CURRENT_STATE_POLL_MS).toBe(30_000);
    expect(SOURCES_POLL_MS).toBe(30_000);
  });

  it('lists open and awaiting_platform discrepancies by default, frozen', () => {
    expect(DEFAULT_DISCREPANCY_STATES).toEqual(['open', 'awaiting_platform']);
    expect(Object.isFrozen(DEFAULT_DISCREPANCY_STATES)).toBe(true);
  });
});

describe('queryKeys', () => {
  it('are the documented tuples', () => {
    expect(queryKeys.me).toEqual(['auth', 'me']);
    expect(queryKeys.runs.all).toEqual(['runs']);
    expect(queryKeys.runs.latest).toEqual(['runs', 'latest']);
    expect(queryKeys.runs.byId(CUSTOM_RUN_ID)).toEqual(['runs', 'byId', CUSTOM_RUN_ID]);
    expect(queryKeys.runs.byId(undefined)).toEqual(['runs', 'byId', undefined]);
    expect(queryKeys.runs.customAll).toEqual(['runs', 'custom']);
    expect(queryKeys.runs.custom(20)).toEqual(['runs', 'custom', 20]);
    expect(queryKeys.recommendationsAll).toEqual(['recommendations']);
    expect(queryKeys.recommendations('default')).toEqual(['recommendations', 'default']);
    expect(queryKeys.recommendations(CUSTOM_RUN_ID)).toEqual(['recommendations', CUSTOM_RUN_ID]);
    expect(queryKeys.decisionsAll).toEqual(['decisions']);
    expect(queryKeys.discrepanciesAll).toEqual(['discrepancies']);
    expect(queryKeys.discrepancyPages(['open'])).toEqual(['discrepancies', 'pages', ['open']]);
    expect(queryKeys.sources).toEqual(['sources']);
    expect(queryKeys.config).toEqual(['config']);
  });

  it('keep the default page and a run-id page apart', () => {
    expect(queryKeys.recommendations('default')).not.toEqual(
      queryKeys.recommendations(DEFAULT_RUN_ID),
    );
  });

  it('nest every variant under its root', () => {
    expect(queryKeys.runs.latest.slice(0, 1)).toEqual(queryKeys.runs.all);
    expect(queryKeys.runs.custom(5).slice(0, 2)).toEqual(queryKeys.runs.customAll);
    expect(queryKeys.recommendations(7).slice(0, 1)).toEqual(queryKeys.recommendationsAll);
    expect(queryKeys.decisions({}).slice(0, 1)).toEqual(queryKeys.decisionsAll);
    expect(queryKeys.discrepancies(['open']).slice(0, 1)).toEqual(queryKeys.discrepanciesAll);
    expect(queryKeys.discrepancyPages(['open']).slice(0, 1)).toEqual(queryKeys.discrepanciesAll);
  });

  it('drop undefined decision filter members, so equal filters share one key', () => {
    expect(queryKeys.decisions({})).toEqual(['decisions', {}]);
    // toStrictEqual also fails on a key that is present with the value undefined.
    expect(queryKeys.decisions({})).toStrictEqual(queryKeys.decisions({ subject_id: undefined }));
    expect(queryKeys.decisions({ subject_type: 'budget', subject_id: undefined })).toStrictEqual(
      queryKeys.decisions({ subject_type: 'budget' }),
    );
    expect(queryKeys.decisions({ subject_type: 'campaign', subject_id: CAMPAIGN_ID })).toEqual([
      'decisions',
      { subject_type: 'campaign', subject_id: CAMPAIGN_ID },
    ]);
  });

  it('copy the discrepancy states, so a later change to the array cannot alter the key', () => {
    const states: DiscrepancyState[] = ['open'];
    const key = queryKeys.discrepancies(states);
    states.push('resolved');
    expect(key).toEqual(['discrepancies', ['open']]);
    expect(key[1]).not.toBe(states);
    const pages = queryKeys.discrepancyPages(states);
    states.push('awaiting_platform');
    expect(pages).toEqual(['discrepancies', 'pages', ['open', 'resolved']]);
    expect(pages[2]).not.toBe(states);
  });
});

describe('isAwaitingFirstRun', () => {
  it('is true for not_found and run_not_ready ApiErrors', () => {
    expect(isAwaitingFirstRun(new ApiError(404, 'not_found', 'No analysis run yet'))).toBe(true);
    expect(isAwaitingFirstRun(new ApiError(409, 'run_not_ready', 'Run 7 is running'))).toBe(true);
  });

  it('is false for every other error and value', () => {
    expect(isAwaitingFirstRun(new ApiError(409, 'run_in_progress', 'Busy'))).toBe(false);
    expect(isAwaitingFirstRun(new ApiError(0, 'network_error', 'Offline'))).toBe(false);
    expect(isAwaitingFirstRun(new ApiError(404, 'http_404', 'x'))).toBe(false);
    expect(isAwaitingFirstRun(new ApiError(401, 'unauthenticated', 'x'))).toBe(false);
    expect(isAwaitingFirstRun({ status: 404, code: 'not_found' })).toBe(false);
    expect(isAwaitingFirstRun(new Error('not_found'))).toBe(false);
    expect(isAwaitingFirstRun(null)).toBe(false);
    expect(isAwaitingFirstRun(undefined)).toBe(false);
  });
});

// ---------------------------------------------------------------------------------------------
// The signed-in user
// ---------------------------------------------------------------------------------------------

describe('meQueryOptions and useMe', () => {
  it('keys GET /auth/me under queryKeys.me, fresh for 5 minutes, never retried', () => {
    const options = meQueryOptions();
    expect(options.queryKey).toEqual(queryKeys.me);
    expect(options.staleTime).toBe(LONG_STALE_MS);
    expect(options.retry).toBe(false);
  });

  it('resolves the user from GET /api/v1/auth/me with an abort signal', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/auth/me': () => json(200, mePayload()) }));
    const { queryClient } = createWrapper();
    await expect(queryClient.fetchQuery(meQueryOptions())).resolves.toEqual(mePayload());
    const request = onlyRequest(mock);
    expect(request.method).toBe('GET');
    expect(request.parsed.pathname).toBe('/api/v1/auth/me');
    expect(request.parsed.search).toBe('');
    expect(request.headers).toEqual({ Accept: 'application/json' });
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('rejects a signed-out user after one request, even when the client retries', async () => {
    const mock = stubFetch(() => json(401, envelope('unauthenticated', 'Sign in again.')));
    const retrying = new QueryClient({ defaultOptions: { queries: { retry: 3 } } });
    clients.push(retrying);
    const error = await rejection(retrying.ensureQueryData(meQueryOptions()));
    expect(isApiError(error) && error.status === 401 && error.code === 'unauthenticated').toBe(
      true,
    );
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('useMe requests /api/v1/auth/me and does not refetch on a second mount', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/auth/me': () => json(200, mePayload()) }));
    const first = renderWithWrapper(() => useMe());
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
    expect(first.result.current.data).toEqual(mePayload());
    expect(first.queryClient.getQueryData(queryKeys.me)).toEqual(mePayload());
    expect(onlyRequest(mock).parsed.pathname).toBe('/api/v1/auth/me');

    const second = renderWithWrapper(() => useMe(), first);
    await act(() => Promise.resolve());
    expect(second.result.current.data).toEqual(mePayload());
    expect(second.result.current.isStale).toBe(false);
    expect(mock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------------------------

describe('useDefaultRun', () => {
  it('reads GET /api/v1/runs/latest under queryKeys.runs.latest', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs/latest': () => json(200, runPayload()) }));
    const { result, queryClient } = renderWithWrapper(() => useDefaultRun());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(runPayload());
    expect(queryClient.getQueryData(queryKeys.runs.latest)).toEqual(runPayload());
    const request = onlyRequest(mock);
    expect(request.method).toBe('GET');
    expect(request.parsed.pathname).toBe('/api/v1/runs/latest');
    expect(request.parsed.search).toBe('');
    expect(request.signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(queryClient, queryKeys.runs.latest)).toBe(CURRENT_STATE_POLL_MS);
  });

  it.each([
    [
      '404 not_found before the first run',
      FIRST_RUN_POLL_MS,
      () => json(404, envelope('not_found', 'No analysis run yet')),
    ],
    ['409 run_not_ready', FIRST_RUN_POLL_MS, () => json(409, envelope('run_not_ready', 'Running'))],
    [
      'an unreachable server',
      CURRENT_STATE_POLL_MS,
      () => Promise.reject(new TypeError('Failed to fetch')),
    ],
    ['a 429', CURRENT_STATE_POLL_MS, () => json(429, envelope('rate_limited', 'Slow down'))],
    [
      'nginx 502 while the API restarts',
      CURRENT_STATE_POLL_MS,
      () => new Response('<html>Bad Gateway</html>', { status: 502 }),
    ],
    ['a 500', CURRENT_STATE_POLL_MS, () => json(500, envelope('internal_error', 'Boom'))],
    ['an expired session', false, () => json(401, envelope('unauthenticated', 'Sign in again.'))],
    ['a 422', false, () => json(422, envelope('validation_error', 'Bad'))],
  ] as const)('after %s polls at %s', async (_label, interval, respond) => {
    stubFetch(respond);
    const { result, queryClient } = renderWithWrapper(() => useDefaultRun());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(isApiError(result.current.error)).toBe(true);
    expect(pollInterval(queryClient, queryKeys.runs.latest)).toBe(interval);
  });

  it('polls until the first default run exists, then holds it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = stubFetch(
      sequence(
        () => json(404, envelope('not_found', 'No analysis run yet')),
        () => json(200, runPayload()),
      ),
    );
    const { result } = renderWithWrapper(() => useDefaultRun());
    await waitFor(() => expect(result.current.error?.code).toBe('not_found'));
    expect(isAwaitingFirstRun(result.current.error)).toBe(true);
    expect(mock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(FIRST_RUN_POLL_MS));
    await waitFor(() => expect(result.current.data?.id).toBe(DEFAULT_RUN_ID));
    expect(result.current.error).toBeNull();
    expect(mock).toHaveBeenCalledTimes(2);
    expect(sent(mock).map((request) => request.parsed.pathname)).toEqual([
      '/api/v1/runs/latest',
      '/api/v1/runs/latest',
    ]);
  });

  it('refetches every 15 s while no run exists, then every 30 s once one does', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    // Each fetch outcome restarts the interval shortly after its tick, so the checks sit a
    // second either side of each expected refetch.
    const margin = 1_000;
    const mock = stubFetch(
      sequence(
        () => json(404, envelope('not_found', 'No analysis run yet')),
        () => json(200, runPayload()),
        () => json(200, runPayload(DEFAULT_RUN_ID + 1)),
      ),
    );
    const { result } = renderWithWrapper(() => useDefaultRun());
    await act(() => vi.advanceTimersByTimeAsync(0));
    await waitFor(() => expect(result.current.error?.code).toBe('not_found'));
    expect(mock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(FIRST_RUN_POLL_MS - margin));
    expect(mock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(2 * margin));
    await waitFor(() => expect(result.current.data?.id).toBe(DEFAULT_RUN_ID));
    expect(mock).toHaveBeenCalledTimes(2);

    // A 15 s cadence would refetch within the next 15 s; the 30 s cadence does not.
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS - 2 * margin));
    expect(mock).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(2 * margin));
    await waitFor(() => expect(result.current.data?.id).toBe(DEFAULT_RUN_ID + 1));
    expect(mock).toHaveBeenCalledTimes(3);
  });

  it('aborts the request in flight when the last component unmounts', async () => {
    const mock = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );
    const { unmount } = renderWithWrapper(() => useDefaultRun());
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
    const { signal } = onlyRequest(mock);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('defaultRunQueryOptions keys GET /runs/latest and polls from the query state', () => {
    const { queryClient } = createWrapper();
    const options = defaultRunQueryOptions(queryClient);
    expect(options.queryKey).toEqual(queryKeys.runs.latest);
    expect(typeof options.refetchInterval).toBe('function');
    expect(typeof options.initialData).toBe('function');
    expect(typeof options.initialDataUpdatedAt).toBe('function');
  });

  it('starts from a fresh cached default page and sends no GET /runs/latest', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs/latest': () => json(200, runPayload()) }));
    const setup = createWrapper(APP_STALE_MS);
    const page = pagePayload();
    setup.queryClient.setQueryData(queryKeys.recommendations('default'), page);
    const pageUpdatedAt = setup.queryClient.getQueryState(
      queryKeys.recommendations('default'),
    )?.dataUpdatedAt;

    const { result, queryClient } = renderWithWrapper(() => useDefaultRun(), setup);
    await act(() => Promise.resolve());

    expect(result.current.data).toEqual(page.run);
    expect(result.current.isStale).toBe(false);
    expect(queryClient.getQueryState(queryKeys.runs.latest)?.dataUpdatedAt).toBe(pageUpdatedAt);
    expect(mock).not.toHaveBeenCalled();
    expect(pollInterval(queryClient, queryKeys.runs.latest)).toBe(CURRENT_STATE_POLL_MS);
  });

  it('reads GET /runs/latest when the cached default page is stale', async () => {
    const latest = runPayload(DEFAULT_RUN_ID + 1);
    const mock = stubFetch(routes({ 'GET /api/v1/runs/latest': () => json(200, latest) }));
    const setup = createWrapper(APP_STALE_MS);
    setup.queryClient.setQueryData(queryKeys.recommendations('default'), pagePayload(), {
      updatedAt: Date.now() - 2 * APP_STALE_MS,
    });

    const { result } = renderWithWrapper(() => useDefaultRun(), setup);
    await waitFor(() => expect(result.current.data).toEqual(latest));
    expect(onlyRequest(mock).parsed.pathname).toBe('/api/v1/runs/latest');
  });

  it('never starts from a page addressed by run id, which may show a custom run', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs/latest': () => json(200, runPayload()) }));
    const setup = createWrapper(APP_STALE_MS);
    setup.queryClient.setQueryData(
      queryKeys.recommendations(CUSTOM_RUN_ID),
      pagePayload(CUSTOM_RUN_ID, 'custom'),
    );

    const { result } = renderWithWrapper(() => useDefaultRun(), setup);
    expect(result.current.data).toBeUndefined();
    await waitFor(() => expect(result.current.data).toEqual(runPayload()));
    expect(onlyRequest(mock).parsed.pathname).toBe('/api/v1/runs/latest');
  });
});


describe('useRun', () => {
  it('sends nothing while no run is addressed (skipToken)', async () => {
    const mock = stubFetch(() => json(200, runPayload()));
    const { result, queryClient } = renderWithWrapper(() => useRun(undefined));
    await act(() => Promise.resolve());
    expect(mock).not.toHaveBeenCalled();
    expect(result.current.status).toBe('pending');
    expect(result.current.fetchStatus).toBe('idle');
    expect(queryClient.getQueryState(queryKeys.runs.byId(undefined))).toBeDefined();
  });

  it('reads GET /api/v1/runs/{id} once an id arrives', async () => {
    const run = runPayload(CUSTOM_RUN_ID, 'custom');
    const mock = stubFetch(
      routes({ [`GET /api/v1/runs/${String(CUSTOM_RUN_ID)}`]: () => json(200, run) }),
    );
    const addressed: { id: RunId | undefined } = { id: undefined };
    const { result, rerender, queryClient } = renderWithWrapper(() => useRun(addressed.id));
    await act(() => Promise.resolve());
    expect(mock).not.toHaveBeenCalled();

    addressed.id = CUSTOM_RUN_ID;
    rerender();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(run);
    expect(queryClient.getQueryData(queryKeys.runs.byId(CUSTOM_RUN_ID))).toEqual(run);
    const request = onlyRequest(mock);
    expect(request.parsed.pathname).toBe(`/api/v1/runs/${String(CUSTOM_RUN_ID)}`);
    expect(request.parsed.search).toBe('');
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('encodes the id as one path segment', async () => {
    const mock = stubFetch(() => json(200, runPayload()));
    const { result } = renderWithWrapper(() => useRun('7/../x y' as unknown as RunId));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(onlyRequest(mock).url).toBe('/api/v1/runs/7%2F..%2Fx%20y');
  });

  it('surfaces a 404 for an unknown id as an ApiError', async () => {
    stubFetch(() => json(404, envelope('not_found', 'Run 9 not found')));
    const { result } = renderWithWrapper(() => useRun(9));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(isApiError(result.current.error)).toBe(true);
    expect(result.current.error?.status).toBe(404);
    expect(result.current.error?.code).toBe('not_found');
  });
});

describe('useCustomRuns', () => {
  it('lists 20 custom runs by default', async () => {
    const runs = [runPayload(CUSTOM_RUN_ID, 'custom')];
    const mock = stubFetch(routes({ 'GET /api/v1/runs': () => json(200, runs) }));
    const { result, queryClient } = renderWithWrapper(() => useCustomRuns());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(runs);
    expect(queryClient.getQueryData(queryKeys.runs.custom(20))).toEqual(runs);
    const request = onlyRequest(mock);
    expect(params(request)).toEqual([
      ['kind', 'custom'],
      ['limit', '20'],
    ]);
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('sends the limit it is given', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs': () => json(200, []) }));
    const { result, queryClient } = renderWithWrapper(() => useCustomRuns(5));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
    expect(queryClient.getQueryData(queryKeys.runs.custom(5))).toEqual([]);
    const { parsed } = onlyRequest(mock);
    expect(parsed.searchParams.get('kind')).toBe('custom');
    expect(parsed.searchParams.get('limit')).toBe('5');
  });

  it('sends nothing while disabled, and reads the list once enabled', async () => {
    const runs = [runPayload(CUSTOM_RUN_ID, 'custom')];
    const mock = stubFetch(routes({ 'GET /api/v1/runs': () => json(200, runs) }));
    const state = { enabled: false };
    const { result, rerender } = renderWithWrapper(() =>
      useCustomRuns(undefined, { enabled: state.enabled }),
    );
    await act(() => Promise.resolve());
    expect(mock).not.toHaveBeenCalled();
    expect(result.current.status).toBe('pending');
    expect(result.current.fetchStatus).toBe('idle');

    state.enabled = true;
    rerender();
    await waitFor(() => expect(result.current.data).toEqual(runs));
    expect(params(onlyRequest(mock))).toEqual([
      ['kind', 'custom'],
      ['limit', '20'],
    ]);
  });
});

describe('useHasCachedCustomRuns', () => {
  it('is true only once the cache holds the list for that limit', () => {
    const setup = createWrapper();
    const before = renderWithWrapper(() => useHasCachedCustomRuns(), setup);
    expect(before.result.current).toBe(false);

    setup.queryClient.setQueryData(queryKeys.runs.custom(20), [runPayload(CUSTOM_RUN_ID, 'custom')]);
    expect(renderWithWrapper(() => useHasCachedCustomRuns(), setup).result.current).toBe(true);
    expect(renderWithWrapper(() => useHasCachedCustomRuns(5), setup).result.current).toBe(false);
  });

  it('sends no request', () => {
    const mock = stubFetch(() => json(200, []));
    renderWithWrapper(() => useHasCachedCustomRuns());
    expect(mock).not.toHaveBeenCalled();
  });
});

describe('useForgetCustomRuns', () => {
  it('removes only the custom-run list for 20 by default, and sends nothing', () => {
    const mock = stubFetch(() => json(200, []));
    const setup = createWrapper();
    const kept = [
      queryKeys.runs.custom(5),
      queryKeys.runs.latest,
      queryKeys.runs.byId(CUSTOM_RUN_ID),
      queryKeys.recommendations('default'),
    ];
    seed(setup.queryClient, [queryKeys.runs.custom(20), ...kept]);
    const { result } = renderWithWrapper(
      () => ({ forget: useForgetCustomRuns(), cached: useHasCachedCustomRuns() }),
      setup,
    );
    expect(result.current.cached).toBe(true);

    act(() => {
      result.current.forget();
    });

    const cache = setup.queryClient.getQueryCache();
    expect(cache.find({ queryKey: queryKeys.runs.custom(20), exact: true })).toBeUndefined();
    for (const key of kept) {
      expect(setup.queryClient.getQueryData(key)).toEqual({ seeded: key });
    }
    expect(renderWithWrapper(() => useHasCachedCustomRuns(), setup).result.current).toBe(false);
    expect(mock).not.toHaveBeenCalled();
  });

  it('removes only the list for the limit it is given', () => {
    const setup = createWrapper();
    seed(setup.queryClient, [queryKeys.runs.custom(20), queryKeys.runs.custom(5)]);
    const { result } = renderWithWrapper(() => useForgetCustomRuns(5), setup);

    act(() => {
      result.current();
    });

    expect(setup.queryClient.getQueryData(queryKeys.runs.custom(5))).toBeUndefined();
    expect(setup.queryClient.getQueryData(queryKeys.runs.custom(20))).toEqual({
      seeded: queryKeys.runs.custom(20),
    });
  });

  it('returns the same callback across renders', () => {
    const { result, rerender } = renderWithWrapper(() => useForgetCustomRuns());
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });

  it('leaves a mounted, disabled observer working: enabled again, it reads the list once', async () => {
    const runs = [runPayload(CUSTOM_RUN_ID, 'custom')];
    const mock = stubFetch(routes({ 'GET /api/v1/runs': () => json(200, runs) }));
    const state = { enabled: true };
    const { result, rerender, queryClient } = renderWithWrapper(() => ({
      list: useCustomRuns(undefined, { enabled: state.enabled }),
      forget: useForgetCustomRuns(),
    }));
    await waitFor(() => expect(result.current.list.data).toEqual(runs));
    expect(sent(mock)).toHaveLength(1);

    // As WindowPicker closes its disclosure: the observer is disabled and the list forgotten.
    act(() => {
      state.enabled = false;
      result.current.forget();
    });
    rerender();
    await act(() => Promise.resolve());

    expect(result.current.list.data).toBeUndefined();
    expect(result.current.list.fetchStatus).toBe('idle');
    expect(queryClient.getQueryData(queryKeys.runs.custom(20))).toBeUndefined();
    expect(sent(mock)).toHaveLength(1);

    state.enabled = true;
    rerender();

    await waitFor(() => expect(result.current.list.data).toEqual(runs));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const requests = sent(mock);
    expect(requests).toHaveLength(2);
    expect(requests[1] === undefined ? [] : params(requests[1])).toEqual([
      ['kind', 'custom'],
      ['limit', '20'],
    ]);
    expect(queryClient.getQueryData(queryKeys.runs.custom(20))).toEqual(runs);
  });

  it('aborts a read of the list still in flight', async () => {
    const mock = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );
    const { result } = renderWithWrapper(() => ({
      list: useCustomRuns(),
      forget: useForgetCustomRuns(),
    }));
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
    const { signal } = onlyRequest(mock);
    expect(signal?.aborted).toBe(false);

    act(() => {
      result.current.forget();
    });

    expect(signal?.aborted).toBe(true);
  });
});

// ---------------------------------------------------------------------------------------------
// Recommendations
// ---------------------------------------------------------------------------------------------

describe('useRecommendations', () => {
  it('reads the default run without run_id under the "default" key', async () => {
    const page = pagePayload();
    const mock = stubFetch(routes({ 'GET /api/v1/recommendations': () => json(200, page) }));
    const { result, queryClient } = renderWithWrapper(() => useRecommendations());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(page);
    expect(queryClient.getQueryData(queryKeys.recommendations('default'))).toEqual(page);
    const request = onlyRequest(mock);
    expect(request.parsed.pathname).toBe('/api/v1/recommendations');
    expect(request.parsed.searchParams.has('run_id')).toBe(false);
    expect(request.parsed.search).toBe('');
    expect(request.signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(queryClient, queryKeys.recommendations('default'))).toBe(
      CURRENT_STATE_POLL_MS,
    );
  });

  it('reads a custom run with run_id under its own key', async () => {
    const page = pagePayload(CUSTOM_RUN_ID, 'custom');
    const mock = stubFetch(routes({ 'GET /api/v1/recommendations': () => json(200, page) }));
    const { result, queryClient } = renderWithWrapper(() => useRecommendations(CUSTOM_RUN_ID));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.run.id).toBe(CUSTOM_RUN_ID);
    const request = onlyRequest(mock);
    expect(request.parsed.pathname).toBe('/api/v1/recommendations');
    expect(params(request)).toEqual([['run_id', String(CUSTOM_RUN_ID)]]);
    const key = queryKeys.recommendations(CUSTOM_RUN_ID);
    expect(queryClient.getQueryData(key)).toEqual(page);
    expect(queryClient.getQueryData(queryKeys.recommendations('default'))).toBeUndefined();
    expect(pollInterval(queryClient, key)).toBe(CURRENT_STATE_POLL_MS);
  });

  it.each([
    ['the default page', undefined, 404, 'not_found', FIRST_RUN_POLL_MS],
    ['the default page', undefined, 401, 'unauthenticated', false],
    ['the default page', undefined, 503, 'internal_error', CURRENT_STATE_POLL_MS],
    ['a custom page', CUSTOM_RUN_ID, 404, 'not_found', false],
    ['a custom page', CUSTOM_RUN_ID, 409, 'run_not_ready', false],
    ['a custom page', CUSTOM_RUN_ID, 502, 'internal_error', CURRENT_STATE_POLL_MS],
  ] as const)(
    '%s (run id %s) after a %i %s polls at %s',
    async (_label, runId, status, code, interval) => {
      stubFetch(() => json(status, envelope(code, 'x')));
      const { result, queryClient } = renderWithWrapper(() => useRecommendations(runId));
      await waitFor(() => expect(result.current.isError).toBe(true));
      expect(result.current.error?.code).toBe(code);
      const key = queryKeys.recommendations(runId ?? 'default');
      expect(pollInterval(queryClient, key)).toBe(interval);
    },
  );

  it('polls the default page until the first default run exists', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const page = pagePayload();
    const mock = stubFetch(
      sequence(
        () => json(404, envelope('not_found', 'No analysis run yet')),
        () => json(200, page),
      ),
    );
    const { result } = renderWithWrapper(() => useRecommendations());
    await waitFor(() => expect(result.current.error?.code).toBe('not_found'));
    expect(mock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(FIRST_RUN_POLL_MS));
    await waitFor(() => expect(result.current.data).toEqual(page));
    expect(mock).toHaveBeenCalledTimes(2);
    for (const request of sent(mock)) {
      expect(request.parsed.pathname).toBe('/api/v1/recommendations');
      expect(request.parsed.searchParams.has('run_id')).toBe(false);
    }
  });

  it('does not poll a run-id page after its 404', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = stubFetch(() => json(404, envelope('not_found', 'Run 202 not found')));
    const { result } = renderWithWrapper(() => useRecommendations(CUSTOM_RUN_ID));
    await waitFor(() => expect(result.current.error?.code).toBe('not_found'));
    expect(isAwaitingFirstRun(result.current.error)).toBe(true);

    await act(() => vi.advanceTimersByTimeAsync(FIRST_RUN_POLL_MS));
    expect(mock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
    expect(mock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------------------------

describe('useDecisions', () => {
  it('reads every decision without a filter, sending neither parameter', async () => {
    const log = [decisionPayload(1), decisionPayload(2)];
    const mock = stubFetch(routes({ 'GET /api/v1/decisions': () => json(200, log) }));
    const { result, queryClient } = renderWithWrapper(() => useDecisions());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(log);
    expect(queryClient.getQueryData(queryKeys.decisions({}))).toEqual(log);
    const request = onlyRequest(mock);
    expect(request.parsed.searchParams.has('subject_type')).toBe(false);
    expect(request.parsed.searchParams.has('subject_id')).toBe(false);
    expect(request.parsed.search).toBe('');
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('sends subject_type and subject_id when the filter sets them', async () => {
    const log = [decisionPayload(1)];
    const mock = stubFetch(routes({ 'GET /api/v1/decisions': () => json(200, log) }));
    const filter = { subject_type: 'campaign', subject_id: CAMPAIGN_ID } as const;
    const { result, queryClient } = renderWithWrapper(() => useDecisions(filter));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(log);
    expect(queryClient.getQueryData(queryKeys.decisions(filter))).toEqual(log);
    expect(params(onlyRequest(mock))).toEqual([
      ['subject_type', 'campaign'],
      ['subject_id', String(CAMPAIGN_ID)],
    ]);
  });

  it('omits an undefined member and shares the key of the filter without it', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/decisions': () => json(200, []) }));
    const { result, queryClient } = renderWithWrapper(() =>
      useDecisions({ subject_type: 'budget', subject_id: undefined }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(queryClient.getQueryData(queryKeys.decisions({ subject_type: 'budget' }))).toEqual([]);
    expect(params(onlyRequest(mock))).toEqual([['subject_type', 'budget']]);
  });

  it('surfaces 422 invalid_query as an ApiError', async () => {
    stubFetch(() => json(422, envelope('invalid_query', 'subject_id needs subject_type')));
    const { result } = renderWithWrapper(() => useDecisions({ subject_id: CAMPAIGN_ID }));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.code).toBe('invalid_query');
  });

  it.each([
    ['a success', CURRENT_STATE_POLL_MS, () => json(200, [decisionPayload(1)])],
    ['a 502', CURRENT_STATE_POLL_MS, () => new Response('Bad Gateway', { status: 502 })],
    ['an unreachable server', CURRENT_STATE_POLL_MS, () => Promise.reject(new TypeError('x'))],
    ['an expired session', false, () => json(401, envelope('unauthenticated', 'Sign in again.'))],
    ['a 422', false, () => json(422, envelope('invalid_query', 'Bad filter'))],
  ] as const)('after %s polls at %s', async (_label, interval, respond) => {
    stubFetch(respond);
    const { result, queryClient } = renderWithWrapper(() => useDecisions());
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(pollInterval(queryClient, queryKeys.decisions({}))).toBe(interval);
  });

  it('refetches the log after CURRENT_STATE_POLL_MS', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const before = [decisionPayload(1)];
    const after = [decisionPayload(2, 'baseline'), ...before];
    const mock = stubFetch(
      sequence(
        () => json(200, before),
        () => json(200, after),
      ),
    );
    const { result } = renderWithWrapper(() => useDecisions());
    await waitFor(() => expect(result.current.data).toEqual(before));
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
    await waitFor(() => expect(result.current.data).toEqual(after));
    expect(mock).toHaveBeenCalledTimes(2);
  });
});


// ---------------------------------------------------------------------------------------------
// Discrepancies and paging
// ---------------------------------------------------------------------------------------------

describe('useDiscrepancies', () => {
  const FIRST_PAGE = '/api/v1/discrepancies?state=open&state=awaiting_platform&limit=500';

  it('reads the default states at limit 500 under their key', async () => {
    const rows = discrepancies(DISCREPANCY_ID, 2);
    const mock = stubFetch(routes({ 'GET /api/v1/discrepancies': () => json(200, rows) }));
    const { result, queryClient } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(rows);
    const key = queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES);
    expect(key).toEqual(['discrepancies', ['open', 'awaiting_platform']]);
    expect(queryClient.getQueryData(key)).toEqual(rows);
    const request = onlyRequest(mock);
    expect(request.method).toBe('GET');
    expect(request.parsed.pathname).toBe('/api/v1/discrepancies');
    expect(request.parsed.searchParams.getAll('state')).toEqual(DEFAULT_DISCREPANCY_STATES);
    expect(request.parsed.searchParams.get('limit')).toBe('500');
    expect(request.url).toBe(FIRST_PAGE);
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('sends nothing while disabled, yet returns the list another reader cached', async () => {
    const rows = discrepancies(DISCREPANCY_ID, 2);
    const mock = stubFetch(routes({ 'GET /api/v1/discrepancies': () => json(200, rows) }));
    const setup = createWrapper();
    const disabled = renderWithWrapper(
      () => useDiscrepancies(DEFAULT_DISCREPANCY_STATES, { enabled: false }),
      setup,
    );
    expect(disabled.result.current.fetchStatus).toBe('idle');
    expect(disabled.result.current.data).toBeUndefined();
    expect(mock).not.toHaveBeenCalled();

    // An enabled reader of the same states (the C0 card lookup) fills the shared entry.
    const enabled = renderWithWrapper(() => useDiscrepancies(), setup);
    await waitFor(() => expect(enabled.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(disabled.result.current.data).toEqual(rows));
    expect(onlyRequest(mock).url).toBe(FIRST_PAGE);
  });

  it('reads once it is enabled, under the same key', async () => {
    const rows = discrepancies(DISCREPANCY_ID, 1);
    const mock = stubFetch(routes({ 'GET /api/v1/discrepancies': () => json(200, rows) }));
    const { wrapper, queryClient } = createWrapper();
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useDiscrepancies(DEFAULT_DISCREPANCY_STATES, { enabled }),
      { wrapper, initialProps: { enabled: false } },
    );
    expect(mock).not.toHaveBeenCalled();

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(rows);
    expect(queryClient.getQueryData(queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES))).toEqual(
      rows,
    );
    expect(onlyRequest(mock).url).toBe(FIRST_PAGE);
  });

  it('never polls while disabled, even over a cached list, and polls only while enabled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 2)),
        () => json(200, discrepancies(51, 3)),
      ),
    );
    const { wrapper, queryClient } = createWrapper();
    queryClient.setQueryData(
      queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
      discrepancies(51, 1),
    );
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) =>
        useDiscrepancies(DEFAULT_DISCREPANCY_STATES, { enabled }),
      { wrapper, initialProps: { enabled: false } },
    );
    await act(() => vi.advanceTimersByTimeAsync(2 * CURRENT_STATE_POLL_MS));
    expect(mock).not.toHaveBeenCalled();
    expect(result.current.data).toHaveLength(1);

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toHaveLength(2));
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
    await waitFor(() => expect(result.current.data).toHaveLength(3));
    expect(mock).toHaveBeenCalledTimes(2);

    rerender({ enabled: false });
    await act(() => vi.advanceTimersByTimeAsync(2 * CURRENT_STATE_POLL_MS));
    expect(mock).toHaveBeenCalledTimes(2);
    expect(result.current.data).toHaveLength(3);
  });

  it('sends only the states it is given, and no state for an empty list', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/discrepancies': () => json(200, []) }));
    const open = renderWithWrapper(() => useDiscrepancies(['open']));
    const none = renderWithWrapper(() => useDiscrepancies([]));
    await waitFor(() => expect(open.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(none.result.current.isSuccess).toBe(true));
    expect(open.queryClient.getQueryData(queryKeys.discrepancies(['open']))).toEqual([]);
    expect(none.queryClient.getQueryData(queryKeys.discrepancies([]))).toEqual([]);
    const states = sent(mock)
      .map((request) => request.parsed.searchParams.getAll('state'))
      .sort((left, right) => left.length - right.length);
    expect(states).toEqual([[], ['open']]);
    for (const request of sent(mock)) {
      expect(request.parsed.searchParams.get('limit')).toBe('500');
    }
  });

  it('follows every next page exactly as named, however short, until none is named', async () => {
    const first = discrepancies(51, 3);
    const short = discrepancies(60, 1);
    const last = discrepancies(80, 2);
    const mock = stubFetch(
      sequence(
        () => json(200, first, { Link: discrepancyLink(53) }),
        () => json(200, short, { Link: discrepancyLink(70) }),
        () => json(200, [], { Link: discrepancyLink(75) }),
        () => json(200, last),
      ),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.map((row) => row.id)).toEqual([51, 52, 53, 60, 80, 81]);
    const requests = sent(mock);
    expect(requests.map((request) => request.url)).toEqual([
      FIRST_PAGE,
      `${FIRST_PAGE}&after=53`,
      `${FIRST_PAGE}&after=70`,
      `${FIRST_PAGE}&after=75`,
    ]);
    const signal = requests[0]?.signal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(requests.every((request) => request.signal === signal)).toBe(true);
    expect(requests.every((request) => request.method === 'GET')).toBe(true);
  });

  it('rejects a next page it has already followed rather than loop', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), { Link: discrepancyLink(51) }),
        () => json(200, discrepancies(52, 1), { Link: discrepancyLink(52) }),
        () => json(200, discrepancies(53, 1), { Link: discrepancyLink(51) }),
      ),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(200);
    expect(result.current.error?.code).toBe('invalid_response');
    expect(result.current.error?.message).toBe(
      'The server returned a link to a page it had already sent.',
    );
    expect(result.current.data).toBeUndefined();
    expect(mock).toHaveBeenCalledTimes(3);
  });

  it('restarts once from the first page on invalid_cursor, discarding what it read', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 2), { Link: discrepancyLink(52) }),
        () => json(422, envelope('invalid_cursor', 'List again from the first page')),
        () => json(200, discrepancies(53, 2), { Link: discrepancyLink(54) }),
        () => json(200, discrepancies(55, 1)),
      ),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((row) => row.id)).toEqual([53, 54, 55]);
    expect(sent(mock).map((request) => request.url)).toEqual([
      FIRST_PAGE,
      `${FIRST_PAGE}&after=52`,
      FIRST_PAGE,
      `${FIRST_PAGE}&after=54`,
    ]);
  });

  it('rejects a second invalid_cursor', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), { Link: discrepancyLink(51) }),
        () => json(422, envelope('invalid_cursor', 'List again')),
        () => json(200, discrepancies(51, 1), { Link: discrepancyLink(51) }),
        () => json(422, envelope('invalid_cursor', 'List again')),
      ),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(422);
    expect(result.current.error?.code).toBe('invalid_cursor');
    expect(mock).toHaveBeenCalledTimes(4);
  });

  it('does not restart for invalid_cursor on the first page', async () => {
    const mock = stubFetch(() => json(422, envelope('invalid_cursor', 'List again')));
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('invalid_cursor');
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a 500', () => json(500, envelope('internal_error', 'Boom')), 'internal_error'],
    [
      'an unreachable server',
      () => Promise.reject(new TypeError('Failed to fetch')),
      'network_error',
    ],
    [
      'a link off the API base',
      () => json(200, [], { Link: '<https://evil.example/x>; rel=next' }),
      'invalid_response',
    ],
  ] as const)('rejects %s on a later page without a restart', async (_label, later, code) => {
    const mock = stubFetch(
      sequence(() => json(200, discrepancies(51, 1), { Link: discrepancyLink(51) }), later),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe(code);
    expect(result.current.data).toBeUndefined();
    expect(mock).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['a success', CURRENT_STATE_POLL_MS, () => json(200, [])],
    ['a 503', CURRENT_STATE_POLL_MS, () => json(503, envelope('internal_error', 'Restarting'))],
    ['an expired session', false, () => json(401, envelope('unauthenticated', 'Sign in again.'))],
    ['a 422', false, () => json(422, envelope('validation_error', 'Bad state'))],
  ] as const)('after %s polls at %s', async (_label, interval, respond) => {
    stubFetch(respond);
    const { result, queryClient } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(pollInterval(queryClient, queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES))).toBe(
      interval,
    );
  });

  it('refetches the whole list after CURRENT_STATE_POLL_MS', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1)),
        () => json(200, discrepancies(51, 2)),
      ),
    );
    const { result } = renderWithWrapper(() => useDiscrepancies());
    await waitFor(() => expect(result.current.data).toHaveLength(1));
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
    await waitFor(() => expect(result.current.data).toHaveLength(2));
    expect(mock).toHaveBeenCalledTimes(2);
  });
});

describe('useDiscrepancyPages', () => {
  const KEY = queryKeys.discrepancyPages(DEFAULT_DISCREPANCY_STATES);

  /**
   * `useDiscrepancyPages`, reading during render every result property the tests assert on.
   * TanStack re-renders a hook only when a property read from its result changes, so a
   * property first read after a `fetchNextPage` would otherwise still show its old value.
   *
   * @returns The hook's result.
   */
  function useTrackedPages() {
    const query = useDiscrepancyPages();
    void [
      query.data,
      query.error,
      query.hasNextPage,
      query.isError,
      query.isFetchNextPageError,
      query.isRefetchError,
      query.isSuccess,
    ];
    return query;
  }

  /**
   * Runs `work` (a `fetchNextPage` or `refetch`) inside `act`, then lets TanStack's batched
   * notification, scheduled on a zero-delay timer, re-render the hook.
   *
   * @param work - The call to settle.
   */
  async function settle(work: () => Promise<unknown>): Promise<void> {
    await act(async () => {
      await work();
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0);
      });
    });
  }

  it('reads the first page of the default states at the page size, with its total', async () => {
    const rows = discrepancies(DISCREPANCY_ID, 2);
    const mock = stubFetch(() => json(200, rows, pagedHeaders(2)));
    const { result, queryClient } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(DISCREPANCY_PAGE_SIZE).toBe(50);
    expect(result.current.data).toEqual({ discrepancies: rows, total: 2 });
    expect(result.current.hasNextPage).toBe(false);
    expect(KEY).toEqual(['discrepancies', 'pages', ['open', 'awaiting_platform']]);
    expect(queryClient.getQueryState(KEY)?.status).toBe('success');
    const request = onlyRequest(mock);
    expect(request.method).toBe('GET');
    expect(request.url).toBe(FIRST_PAGED);
    expect(request.parsed.searchParams.getAll('state')).toEqual(DEFAULT_DISCREPANCY_STATES);
    expect(request.parsed.searchParams.get('limit')).toBe(String(DISCREPANCY_PAGE_SIZE));
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('sends only the states it is given under their own key', async () => {
    const mock = stubFetch(() => json(200, [], pagedHeaders(0)));
    const { result, queryClient } = renderWithWrapper(() => useDiscrepancyPages(['resolved']));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ discrepancies: [], total: 0 });
    expect(queryClient.getQueryState(queryKeys.discrepancyPages(['resolved']))?.status).toBe(
      'success',
    );
    expect(params(onlyRequest(mock))).toEqual([
      ['state', 'resolved'],
      ['limit', '50'],
    ]);
  });

  it('loads the next page from the Link exactly as named, taking the newest total', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 3), pagedHeaders(5, 53)),
        () => json(200, discrepancies(60, 2), pagedHeaders(6)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51, 52, 53]);
    expect(result.current.data?.total).toBe(5);
    expect(result.current.hasNextPage).toBe(true);

    await settle(() => result.current.fetchNextPage());
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51, 52, 53, 60, 61]);
    expect(result.current.data?.total).toBe(6);
    expect(result.current.hasNextPage).toBe(false);
    const requests = sent(mock);
    expect(requests.map((request) => request.url)).toEqual([
      FIRST_PAGED,
      `${FIRST_PAGED}&after=53`,
    ]);
    expect(requests.every((request) => request.signal instanceof AbortSignal)).toBe(true);
  });

  it('keeps following next links within one load while it has read no discrepancy', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, [], pagedHeaders(3, 50)),
        () => json(200, [], pagedHeaders(3, 55)),
        () => json(200, discrepancies(56, 1), pagedHeaders(3, 56)),
        () => json(200, [], pagedHeaders(3, 60)),
        () => json(200, discrepancies(61, 1), pagedHeaders(3)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([56]);
    expect(mock).toHaveBeenCalledTimes(3);
    expect(result.current.hasNextPage).toBe(true);

    await settle(() => result.current.fetchNextPage());
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([56, 61]);
    expect(result.current.hasNextPage).toBe(false);
    expect(sent(mock).map((request) => request.url)).toEqual([
      FIRST_PAGED,
      `${FIRST_PAGED}&after=50`,
      `${FIRST_PAGED}&after=55`,
      `${FIRST_PAGED}&after=56`,
      `${FIRST_PAGED}&after=60`,
    ]);
  });

  it('ends an empty list after one request', async () => {
    const mock = stubFetch(() => json(200, [], pagedHeaders(0)));
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ discrepancies: [], total: 0 });
    expect(result.current.hasNextPage).toBe(false);
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('rejects a next link followed in an earlier load, keeping the loaded pages', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), pagedHeaders(3, 51)),
        () => json(200, discrepancies(52, 1), pagedHeaders(3, 51)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.fetchNextPage());
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51, 52]);
    expect(result.current.hasNextPage).toBe(true);

    await settle(() => result.current.fetchNextPage());
    expect(result.current.isFetchNextPageError).toBe(true);
    expect(result.current.error?.status).toBe(200);
    expect(result.current.error?.code).toBe('invalid_response');
    expect(result.current.error?.message).toBe(
      'The server returned a link to a page it had already sent.',
    );
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51, 52]);
    expect(mock).toHaveBeenCalledTimes(2);
  });

  it('rejects a next link repeated within one load rather than loop', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, [], pagedHeaders(1, 51)),
        () => json(200, [], pagedHeaders(1, 51)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('invalid_response');
    expect(result.current.data).toBeUndefined();
    expect(mock).toHaveBeenCalledTimes(2);
  });

  it('restarts once from the first page on invalid_cursor, re-reading what it showed', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 2), pagedHeaders(4, 52)),
        () => json(422, envelope('invalid_cursor', 'List again from the first page')),
        () => json(200, discrepancies(53, 2), pagedHeaders(3, 54)),
        () => json(200, discrepancies(55, 1), pagedHeaders(3)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.fetchNextPage());

    expect(result.current.isError).toBe(false);
    expect(result.current.data).toEqual({
      discrepancies: discrepancies(53, 3),
      total: 3,
    });
    expect(result.current.hasNextPage).toBe(false);
    expect(sent(mock).map((request) => request.url)).toEqual([
      FIRST_PAGED,
      `${FIRST_PAGED}&after=52`,
      FIRST_PAGED,
      `${FIRST_PAGED}&after=54`,
    ]);
  });

  it('continues after a restarted load from its own next link', async () => {
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), pagedHeaders(9, 51)),
        () => json(422, envelope('invalid_cursor', 'List again')),
        () => json(200, discrepancies(60, 2), pagedHeaders(9, 61)),
        () => json(200, discrepancies(70, 1), pagedHeaders(9)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.fetchNextPage());
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([60, 61]);
    expect(result.current.hasNextPage).toBe(true);

    await settle(() => result.current.fetchNextPage());
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([60, 61, 70]);
    expect(sent(mock).map((request) => request.url).slice(2)).toEqual([
      FIRST_PAGED,
      `${FIRST_PAGED}&after=61`,
    ]);
  });

  it('rejects a second invalid_cursor, and never restarts for the first page itself', async () => {
    stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), pagedHeaders(2, 51)),
        () => json(422, envelope('invalid_cursor', 'List again')),
        () => json(200, [], pagedHeaders(2, 51)),
        () => json(422, envelope('invalid_cursor', 'List again')),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.fetchNextPage());
    expect(result.current.isFetchNextPageError).toBe(true);
    expect(result.current.error?.status).toBe(422);
    expect(result.current.error?.code).toBe('invalid_cursor');
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51]);

    const first = stubFetch(() => json(422, envelope('invalid_cursor', 'List again')));
    const fresh = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(fresh.result.current.isError).toBe(true));
    expect(fresh.result.current.error?.code).toBe('invalid_cursor');
    expect(first).toHaveBeenCalledTimes(1);
  });

  it('rejects a malformed X-Total-Count as invalid_response', async () => {
    stubFetch(() => json(200, discrepancies(51, 1), { 'X-Total-Count': '-1' }));
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('invalid_response');
  });

  it('leaves the total undefined when the API sends none', async () => {
    stubFetch(() => json(200, discrepancies(51, 1)));
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.total).toBeUndefined();
  });

  it.each([
    ['a success', CURRENT_STATE_POLL_MS, () => json(200, [], pagedHeaders(0))],
    ['an unreachable server', CURRENT_STATE_POLL_MS, () => Promise.reject(new TypeError('x'))],
    ['a 429', CURRENT_STATE_POLL_MS, () => json(429, envelope('rate_limited', 'Slow down'))],
    ['an expired session', false, () => json(401, envelope('unauthenticated', 'Sign in again.'))],
    ['a 422', false, () => json(422, envelope('validation_error', 'Bad state'))],
  ] as const)('after %s polls at %s', async (_label, interval, respond) => {
    stubFetch(respond);
    const { result, queryClient } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(pollInterval(queryClient, KEY)).toBe(interval);
  });

  it('polls by re-reading only the pages already loaded, from the first', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const mock = stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), pagedHeaders(3, 51)),
        () => json(200, discrepancies(52, 1), pagedHeaders(3, 52)),
        () => json(200, discrepancies(50, 2), pagedHeaders(4, 51)),
        () => json(200, discrepancies(52, 1), pagedHeaders(4, 52)),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.fetchNextPage());
    expect(mock).toHaveBeenCalledTimes(2);

    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS));
    await waitFor(() => expect(result.current.data?.total).toBe(4));
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([50, 51, 52]);
    expect(result.current.hasNextPage).toBe(true);
    expect(sent(mock).map((request) => request.url)).toEqual([
      FIRST_PAGED,
      `${FIRST_PAGED}&after=51`,
      FIRST_PAGED,
      `${FIRST_PAGED}&after=51`,
    ]);
  });

  it('keeps the loaded list when a refetch fails', async () => {
    stubFetch(
      sequence(
        () => json(200, discrepancies(51, 1), pagedHeaders(1)),
        () => json(503, envelope('internal_error', 'Restarting')),
      ),
    );
    const { result } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await settle(() => result.current.refetch());
    expect(result.current.isRefetchError).toBe(true);
    expect(result.current.error?.code).toBe('internal_error');
    expect(result.current.data?.discrepancies.map((row) => row.id)).toEqual([51]);
  });

  it('aborts the page in flight when the last component unmounts', async () => {
    const mock = stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }),
    );
    const { unmount } = renderWithWrapper(() => useTrackedPages());
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
    const { signal } = onlyRequest(mock);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});

// ---------------------------------------------------------------------------------------------
// Sources and configuration
// ---------------------------------------------------------------------------------------------

describe('useSources', () => {
  it('sourcesQueryOptions keys GET /sources under queryKeys.sources, polling every SOURCES_POLL_MS', () => {
    const options = sourcesQueryOptions();
    expect(options.queryKey).toEqual(queryKeys.sources);
    expect(options.refetchInterval).toBe(SOURCES_POLL_MS);
    expect(options.staleTime).toBeUndefined();
  });

  it('reads GET /api/v1/sources and polls every SOURCES_POLL_MS', async () => {
    const sources = sourcesPayload();
    const mock = stubFetch(routes({ 'GET /api/v1/sources': () => json(200, sources) }));
    const { result, queryClient } = renderWithWrapper(() => useSources());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((source) => source.source)).toEqual([
      'google',
      'linkedin',
      'hubspot',
    ]);
    expect(queryClient.getQueryData(queryKeys.sources)).toEqual(sources);
    const request = onlyRequest(mock);
    expect(request.parsed.pathname).toBe('/api/v1/sources');
    expect(request.parsed.search).toBe('');
    expect(request.signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(queryClient, queryKeys.sources)).toBe(SOURCES_POLL_MS);
  });

  it('refetches after SOURCES_POLL_MS', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const before = sourcesPayload();
    const after = [{ source: 'google' }] satisfies Pick<Source, 'source'>[];
    const mock = stubFetch(
      sequence(
        () => json(200, before),
        () => json(200, after),
      ),
    );
    const { result } = renderWithWrapper(() => useSources());
    await waitFor(() => expect(result.current.data).toEqual(before));
    expect(mock).toHaveBeenCalledTimes(1);

    await act(() => vi.advanceTimersByTimeAsync(SOURCES_POLL_MS));
    await waitFor(() => expect(result.current.data).toEqual(after));
    expect(mock).toHaveBeenCalledTimes(2);
    expect(sent(mock).map((request) => request.parsed.pathname)).toEqual([
      '/api/v1/sources',
      '/api/v1/sources',
    ]);
  });
});

describe('useConfig', () => {
  it('configQueryOptions keys GET /config under queryKeys.config, fresh for 5 minutes', () => {
    const options = configQueryOptions();
    expect(options.queryKey).toEqual(queryKeys.config);
    expect(options.staleTime).toBe(LONG_STALE_MS);
    expect(options.refetchInterval).toBeUndefined();
  });

  it('reads GET /api/v1/config and stays fresh for a second mount', async () => {
    const config = configPayload();
    const mock = stubFetch(routes({ 'GET /api/v1/config': () => json(200, config) }));
    const first = renderWithWrapper(() => useConfig());
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true));
    expect(first.result.current.data).toEqual(config);
    expect(first.queryClient.getQueryData(queryKeys.config)).toEqual(config);
    const request = onlyRequest(mock);
    expect(request.parsed.pathname).toBe('/api/v1/config');
    expect(request.signal).toBeInstanceOf(AbortSignal);

    const second = renderWithWrapper(() => useConfig(), first);
    await act(() => Promise.resolve());
    expect(second.result.current.data).toEqual(config);
    expect(second.result.current.isStale).toBe(false);
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('surfaces 500 config_error', async () => {
    stubFetch(() => json(500, envelope('config_error', 'thresholds.yaml is invalid')));
    const { result } = renderWithWrapper(() => useConfig());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('config_error');
    expect(result.current.error?.message).toBe('thresholds.yaml is invalid');
  });
});

describe('usePrefetchActionsReads', () => {
  /** Answers the three reads the hook may prefetch, and the custom-run list it must not. */
  function prefetchRoutes(): Responder {
    return routes({
      'GET /api/v1/config': () => json(200, configPayload()),
      'GET /api/v1/sources': () => json(200, sourcesPayload()),
      'GET /api/v1/runs/latest': () => json(200, runPayload()),
      'GET /api/v1/runs': () => json(200, []),
    });
  }

  /** The number of components observing the entry under `queryKey`. */
  function observers(client: QueryClient, queryKey: QueryKey): number | undefined {
    return client.getQueryCache().find({ queryKey, exact: true })?.getObserversCount();
  }

  it('reads GET /config and GET /sources for the default page, without polling', async () => {
    const mock = stubFetch(prefetchRoutes());
    const { queryClient } = renderWithWrapper(() => {
      usePrefetchActionsReads();
    });
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    expect(sent(mock).map((request) => request.parsed.pathname).sort()).toEqual([
      '/api/v1/config',
      '/api/v1/sources',
    ]);
    expect(queryClient.getQueryData(queryKeys.config)).toEqual(configPayload());
    expect(queryClient.getQueryData(queryKeys.sources)).toEqual(sourcesPayload());
    // No default-run entry, so the default page's own run can seed it later.
    expect(queryClient.getQueryState(queryKeys.runs.latest)).toBeUndefined();
    expect(queryClient.getQueryState(queryKeys.runs.custom(20))).toBeUndefined();
    // A prefetch registers no observer, so nothing polls.
    expect(observers(queryClient, queryKeys.config)).toBe(0);
    expect(observers(queryClient, queryKeys.sources)).toBe(0);
  });

  it('also reads GET /runs/latest for an addressed run, and never the custom-run list', async () => {
    const mock = stubFetch(prefetchRoutes());
    const { queryClient } = renderWithWrapper(() => {
      usePrefetchActionsReads(CUSTOM_RUN_ID);
    });
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    expect(sent(mock).map((request) => request.parsed.pathname).sort()).toEqual([
      '/api/v1/config',
      '/api/v1/runs/latest',
      '/api/v1/sources',
    ]);
    expect(queryClient.getQueryData(queryKeys.runs.latest)).toEqual(runPayload());
    expect(observers(queryClient, queryKeys.runs.latest)).toBe(0);
    expect(sent(mock).some((request) => request.parsed.pathname === '/api/v1/runs')).toBe(false);
  });

  it('reads nothing again on a re-render for the same run, and leaves fresh entries alone', async () => {
    const mock = stubFetch(prefetchRoutes());
    const setup = createWrapper(APP_STALE_MS);
    const { queryClient, rerender } = renderWithWrapper(() => {
      usePrefetchActionsReads();
    }, setup);
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    rerender();
    // A second page using the same client finds both entries fresh.
    renderWithWrapper(() => {
      usePrefetchActionsReads();
    }, setup);
    await act(() => Promise.resolve());
    expect(mock).toHaveBeenCalledTimes(2);
  });
});

describe('useActionsRefreshFailure', () => {
  /** The keys the hook checks for the default page, in its order. */
  const WATCHED_KEYS: readonly (readonly [string, QueryKey])[] = [
    ['the page', queryKeys.recommendations('default')],
    ['the sources', queryKeys.sources],
    ['the unresolved discrepancies', queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES)],
    ['the configuration', queryKeys.config],
  ];

  /**
   * The error the client rejects with while nginx answers 502 for a stopped API.
   *
   * @returns A fresh error.
   */
  function outage(): ApiError {
    return new ApiError(
      502,
      'http_502',
      'The server is temporarily unavailable. Try again shortly.',
    );
  }

  /**
   * Settles the entry under `queryKey` with a failed fetch, as a failed poll does: the entry keeps
   * whatever data it holds. The query function rejects by itself, so no request is sent.
   *
   * @param client - The query client.
   * @param queryKey - The exact key.
   * @param error - What the fetch rejects with.
   */
  async function failRead(client: QueryClient, queryKey: QueryKey, error: ApiError): Promise<void> {
    await act(async () => {
      await rejection(client.fetchQuery({ queryKey, queryFn: () => Promise.reject(error) }));
    });
  }

  /**
   * Settles the entry under `queryKey` with a successful fetch of `data`, sending no request.
   *
   * @param client - The query client.
   * @param queryKey - The exact key.
   * @param data - What the fetch resolves to.
   */
  async function succeedRead(
    client: QueryClient,
    queryKey: QueryKey,
    data: unknown,
  ): Promise<void> {
    await act(async () => {
      await client.fetchQuery({ queryKey, queryFn: () => Promise.resolve(data) });
    });
  }

  it('reports nothing while no watched entry holds data, a failed first read included', async () => {
    const mock = stubFetch(unexpected);
    const { result, queryClient } = renderWithWrapper(() => useActionsRefreshFailure());
    expect(result.current).toBeNull();

    // A first read that failed holds no data: the component reading it shows its own failure.
    await failRead(queryClient, queryKeys.sources, outage());
    await nextMacrotask();
    expect(queryClient.getQueryState(queryKeys.sources)?.status).toBe('error');
    expect(result.current).toBeNull();
    expect(mock).not.toHaveBeenCalled();
  });

  it.each(WATCHED_KEYS)(
    'reports a failed refresh of %s, which keeps its data, until the next success',
    async (_name, queryKey) => {
      stubFetch(unexpected);
      const { result, queryClient } = renderWithWrapper(() => useActionsRefreshFailure());
      queryClient.setQueryData(queryKey, { seeded: queryKey });
      await nextMacrotask();
      expect(result.current).toBeNull();

      const error = outage();
      await failRead(queryClient, queryKey, error);
      await waitFor(() => expect(result.current).toBe(error));
      expect(queryClient.getQueryData(queryKey)).toEqual({ seeded: queryKey });

      await succeedRead(queryClient, queryKey, { seeded: queryKey });
      await waitFor(() => expect(result.current).toBeNull());
    },
  );

  it('keeps the same error while the entry is read again, so the notice never flickers', async () => {
    stubFetch(unexpected);
    const queryKey = queryKeys.recommendations('default');
    const { result, queryClient } = renderWithWrapper(() => useActionsRefreshFailure());
    queryClient.setQueryData(queryKey, pagePayload());
    const error = outage();
    await failRead(queryClient, queryKey, error);
    await waitFor(() => expect(result.current).toBe(error));

    const held = deferred<ReturnType<typeof pagePayload>>();
    let refetch: Promise<unknown> = Promise.resolve();
    act(() => {
      refetch = queryClient.fetchQuery({ queryKey, queryFn: () => held.promise });
    });
    await nextMacrotask();
    expect(queryClient.getQueryState(queryKey)?.fetchStatus).toBe('fetching');
    expect(result.current).toBe(error);

    held.resolve(pagePayload());
    await act(async () => {
      await refetch;
    });
    await waitFor(() => expect(result.current).toBeNull());
  });

  it('reports the page before the other reads, then the next failure once the page recovers', async () => {
    stubFetch(unexpected);
    const { result, queryClient } = renderWithWrapper(() => useActionsRefreshFailure());
    for (const [, queryKey] of WATCHED_KEYS) {
      queryClient.setQueryData(queryKey, { seeded: queryKey });
    }
    const configFailure = outage();
    const pageFailure = new ApiError(503, 'database_unavailable', 'Database unavailable');
    await failRead(queryClient, queryKeys.config, configFailure);
    await failRead(queryClient, queryKeys.recommendations('default'), pageFailure);
    await waitFor(() => expect(result.current).toBe(pageFailure));

    await succeedRead(queryClient, queryKeys.recommendations('default'), pagePayload());
    await waitFor(() => expect(result.current).toBe(configFailure));
  });

  it('watches the addressed run only, and never the latest default run or the resolved list', async () => {
    stubFetch(unexpected);
    const { result, queryClient } = renderWithWrapper(() =>
      useActionsRefreshFailure(CUSTOM_RUN_ID),
    );
    const ignored: readonly QueryKey[] = [
      queryKeys.recommendations('default'),
      queryKeys.recommendations(DEFAULT_RUN_ID),
      queryKeys.runs.latest,
      queryKeys.discrepancies(['resolved']),
      queryKeys.discrepancyPages(DEFAULT_DISCREPANCY_STATES),
      queryKeys.decisions({}),
    ];
    for (const queryKey of ignored) {
      queryClient.setQueryData(queryKey, { seeded: queryKey });
      await failRead(queryClient, queryKey, outage());
    }
    await nextMacrotask();
    expect(result.current).toBeNull();

    const addressed = queryKeys.recommendations(CUSTOM_RUN_ID);
    queryClient.setQueryData(addressed, pagePayload(CUSTOM_RUN_ID, 'custom'));
    const error = outage();
    await failRead(queryClient, addressed, error);
    await waitFor(() => expect(result.current).toBe(error));
  });

  it('sends no request, registers no observer and re-renders only when the failure changes', async () => {
    const mock = stubFetch(unexpected);
    let renders = 0;
    const { result, queryClient } = renderWithWrapper(() => {
      renders += 1;
      return useActionsRefreshFailure();
    });
    queryClient.setQueryData(queryKeys.sources, sourcesPayload());
    const error = outage();
    await failRead(queryClient, queryKeys.sources, error);
    await waitFor(() => expect(result.current).toBe(error));
    const settledRenders = renders;

    // Cache events that leave the reported failure as it is: another entry's data, and a second
    // entry failing behind the first in the hook's order.
    queryClient.setQueryData(queryKeys.config, configPayload());
    queryClient.setQueryData(queryKeys.runs.latest, runPayload());
    await failRead(queryClient, queryKeys.config, outage());
    await nextMacrotask();
    expect(result.current).toBe(error);
    expect(renders).toBe(settledRenders);

    for (const [, queryKey] of WATCHED_KEYS) {
      expect(
        queryClient.getQueryCache().find({ queryKey, exact: true })?.getObserversCount() ?? 0,
      ).toBe(0);
    }
    expect(mock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
// Polling lifecycle
// ---------------------------------------------------------------------------------------------

describe('polling lifecycle', () => {
  /** One polling hook and what a lifecycle case needs to drive it and check its requests. */
  interface PollingHook {
    /** The call as the test titles show it. */
    label: string;
    /** Calls the hook under test. */
    hook: () => unknown;
    /** The exact key of the query the hook observes. */
    queryKey: QueryKey;
    /** The interval, in milliseconds, at which the hook polls after a success. */
    interval: number;
    /** The path and query string of every request the hook sends. */
    request: string;
    /** Answers the hook's own `GET` with a minimal success, and any other request with a 418. */
    respond: Responder;
  }

  /** One way the page leaves view, as TanStack Query's `focusManager` observes it. */
  interface OutOfView {
    /** The signal as the test titles show it. */
    name: string;
    /**
     * Takes the page out of view.
     *
     * @returns The call that brings the page back into view.
     */
    hide: () => () => void;
  }

  /** How many poll intervals pass, in each case, while the hook may send nothing. */
  const SILENT_INTERVALS = 3;

  /** The six polling hooks, the default and the run-id recommendations page both included. */
  const POLLING_HOOKS: readonly PollingHook[] = [
    {
      label: 'useDefaultRun()',
      hook: () => useDefaultRun(),
      queryKey: queryKeys.runs.latest,
      interval: CURRENT_STATE_POLL_MS,
      request: '/api/v1/runs/latest',
      respond: routes({ 'GET /api/v1/runs/latest': () => json(200, runPayload()) }),
    },
    {
      label: 'useRecommendations()',
      hook: () => useRecommendations(),
      queryKey: queryKeys.recommendations('default'),
      interval: CURRENT_STATE_POLL_MS,
      request: '/api/v1/recommendations',
      respond: routes({ 'GET /api/v1/recommendations': () => json(200, pagePayload()) }),
    },
    {
      label: 'useRecommendations(CUSTOM_RUN_ID)',
      hook: () => useRecommendations(CUSTOM_RUN_ID),
      queryKey: queryKeys.recommendations(CUSTOM_RUN_ID),
      interval: CURRENT_STATE_POLL_MS,
      request: `/api/v1/recommendations?run_id=${String(CUSTOM_RUN_ID)}`,
      respond: routes({
        'GET /api/v1/recommendations': () => json(200, pagePayload(CUSTOM_RUN_ID, 'custom')),
      }),
    },
    {
      label: 'useDecisions()',
      hook: () => useDecisions(),
      queryKey: queryKeys.decisions({}),
      interval: CURRENT_STATE_POLL_MS,
      request: '/api/v1/decisions',
      respond: routes({ 'GET /api/v1/decisions': () => json(200, [decisionPayload(1)]) }),
    },
    {
      label: 'useDiscrepancies()',
      hook: () => useDiscrepancies(),
      queryKey: queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
      interval: CURRENT_STATE_POLL_MS,
      request: '/api/v1/discrepancies?state=open&state=awaiting_platform&limit=500',
      respond: routes({
        'GET /api/v1/discrepancies': () => json(200, discrepancies(DISCREPANCY_ID, 1)),
      }),
    },
    {
      label: 'useDiscrepancyPages()',
      hook: () => useDiscrepancyPages(),
      queryKey: queryKeys.discrepancyPages(DEFAULT_DISCREPANCY_STATES),
      interval: CURRENT_STATE_POLL_MS,
      request: FIRST_PAGED,
      respond: routes({
        'GET /api/v1/discrepancies': () =>
          json(200, discrepancies(DISCREPANCY_ID, 1), pagedHeaders(1)),
      }),
    },
    {
      label: 'useSources()',
      hook: () => useSources(),
      queryKey: queryKeys.sources,
      interval: SOURCES_POLL_MS,
      request: '/api/v1/sources',
      respond: routes({ 'GET /api/v1/sources': () => json(200, sourcesPayload()) }),
    },
  ];

  /** Tells the document's listeners, TanStack's `focusManager` among them, to re-read it. */
  function dispatchVisibilityChange(): void {
    document.dispatchEvent(new Event('visibilitychange', { bubbles: true }));
  }

  /** Both signals TanStack Query's `focusManager` reads to decide whether the page is shown. */
  const OUT_OF_VIEW: readonly OutOfView[] = [
    {
      name: 'the document is hidden',
      hide: () => {
        const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
        dispatchVisibilityChange();
        return () => {
          visibility.mockRestore();
          dispatchVisibilityChange();
        };
      },
    },
    {
      name: 'focusManager reports it unfocused',
      hide: () => {
        focusManager.setFocused(false);
        return () => {
          focusManager.setFocused(true);
        };
      },
    },
  ];

  /**
   * Every request the stub received, as its path and query string.
   *
   * @param mock - The stubbed fetch function.
   * @returns One `pathname` plus `search` per request, in order.
   */
  function requestPaths(mock: FetchMock): string[] {
    return sent(mock).map((request) => `${request.parsed.pathname}${request.parsed.search}`);
  }

  /**
   * The number of components observing the entry under `queryKey`.
   *
   * @param client - The query client.
   * @param queryKey - The exact key.
   * @returns The observer count, `undefined` when the key has no entry.
   */
  function observerCount(client: QueryClient, queryKey: QueryKey): number | undefined {
    return client.getQueryCache().find({ queryKey, exact: true })?.getObserversCount();
  }

  /**
   * Waits until the entry under `queryKey` has succeeded `count` times and is idle, so every
   * request so far was answered and none is in flight.
   *
   * @param client - The query client.
   * @param queryKey - The exact key.
   * @param count - The expected number of successful fetches.
   */
  async function waitForSuccesses(
    client: QueryClient,
    queryKey: QueryKey,
    count: number,
  ): Promise<void> {
    await waitFor(() => {
      expect(client.getQueryState(queryKey)).toMatchObject({
        status: 'success',
        fetchStatus: 'idle',
        dataUpdateCount: count,
      });
    });
  }

  describe.each(POLLING_HOOKS)('$label', (row) => {
    it.each(OUT_OF_VIEW)(
      'sends nothing while $name, then refetches at once and polls again when shown',
      async ({ hide }) => {
        vi.useFakeTimers({ shouldAdvanceTime: true });
        const mock = stubFetch(row.respond);
        const { queryClient } = renderWithWrapper(row.hook, createWrapper(APP_STALE_MS));
        await waitForSuccesses(queryClient, row.queryKey, 1);
        expect(mock).toHaveBeenCalledTimes(1);

        const show = hide();
        expect(focusManager.isFocused()).toBe(false);
        await act(() => vi.advanceTimersByTimeAsync(SILENT_INTERVALS * row.interval));
        expect(mock).toHaveBeenCalledTimes(1);

        // The entry went stale while hidden, so coming back refetches it at once, well before
        // the next interval tick; polling then resumes at the hook's own cadence.
        const shownAt = Date.now();
        act(() => {
          show();
        });
        expect(focusManager.isFocused()).toBe(true);
        await waitForSuccesses(queryClient, row.queryKey, 2);
        expect(Date.now() - shownAt).toBeLessThan(row.interval);
        expect(mock).toHaveBeenCalledTimes(2);
        await act(() => vi.advanceTimersByTimeAsync(row.interval));
        await waitForSuccesses(queryClient, row.queryKey, 3);
        expect(mock).toHaveBeenCalledTimes(3);
        expect(requestPaths(mock)).toEqual([row.request, row.request, row.request]);
      },
    );

    it('polls while one component still uses it, and sends nothing once the last unmounts', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const mock = stubFetch(row.respond);
      const setup = createWrapper(APP_STALE_MS);
      const first = renderWithWrapper(row.hook, setup);
      await waitForSuccesses(setup.queryClient, row.queryKey, 1);
      // The entry is fresh, so the second component reads it without a request.
      const second = renderWithWrapper(row.hook, setup);
      expect(observerCount(setup.queryClient, row.queryKey)).toBe(2);
      expect(mock).toHaveBeenCalledTimes(1);

      first.unmount();
      expect(observerCount(setup.queryClient, row.queryKey)).toBe(1);
      await act(() => vi.advanceTimersByTimeAsync(row.interval));
      await waitForSuccesses(setup.queryClient, row.queryKey, 2);
      expect(mock).toHaveBeenCalledTimes(2);

      // The entry stays cached with no observer, so any timer left running would fetch it.
      second.unmount();
      expect(observerCount(setup.queryClient, row.queryKey)).toBe(0);
      await act(() => vi.advanceTimersByTimeAsync(SILENT_INTERVALS * row.interval));
      expect(mock).toHaveBeenCalledTimes(2);
      expect(requestPaths(mock)).toEqual([row.request, row.request]);
    });
  });
});


// ---------------------------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------------------------

/** The headers every JSON mutation carries, the CSRF header included. */
const JSON_MUTATION_HEADERS = {
  Accept: 'application/json',
  'Content-Type': 'application/json',
  'X-Requested-With': 'ads-engine',
};

/** The headers a mutation without a body carries. */
const BODYLESS_MUTATION_HEADERS = {
  Accept: 'application/json',
  'X-Requested-With': 'ads-engine',
};

/** The roots a decision write or a discrepancy resolution invalidates, in call order. */
const DECISION_STATE_ROOTS: readonly QueryKey[] = [
  queryKeys.recommendationsAll,
  queryKeys.discrepanciesAll,
  queryKeys.decisionsAll,
];

/**
 * Cached recommendations pages, both under `recommendationsAll`. Seeded without an observer,
 * they are inactive, so a decision write or a resolution removes them rather than
 * invalidating them.
 */
const RECOMMENDATIONS_KEYS: readonly QueryKey[] = [
  queryKeys.recommendations('default'),
  queryKeys.recommendations(CUSTOM_RUN_ID),
];

/** Cached list variants under the other two roots, each of which the prefix match must reach. */
const DECISION_LIST_KEYS: readonly QueryKey[] = [
  queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
  queryKeys.discrepancies(['resolved']),
  queryKeys.discrepancyPages(DEFAULT_DISCREPANCY_STATES),
  queryKeys.decisions({}),
  queryKeys.decisions({ subject_type: 'campaign', subject_id: CAMPAIGN_ID }),
];

/** Every cached variant under the three roots a decision write or a resolution refreshes. */
const DECISION_STATE_KEYS: readonly QueryKey[] = [...RECOMMENDATIONS_KEYS, ...DECISION_LIST_KEYS];

/** The one removal a decision write or a resolution makes: the pages no screen shows. */
const INACTIVE_RECOMMENDATIONS = {
  queryKey: queryKeys.recommendationsAll,
  type: 'inactive',
} as const;

/**
 * Spies on `queryClient.removeQueries`, still calling through, so a test can assert the exact
 * filters a mutation removes by.
 *
 * @param client - The query client.
 * @returns The spy.
 */
function spyOnRemove(client: QueryClient) {
  return vi.spyOn(client, 'removeQueries');
}

/** Keys no decision, resolution or feedback write touches. */
const UNRELATED_KEYS: readonly QueryKey[] = [
  queryKeys.me,
  queryKeys.runs.latest,
  queryKeys.runs.custom(20),
  queryKeys.sources,
  queryKeys.config,
];

describe('useLogin', () => {
  it('posts the credentials with the CSRF header and seeds queryKeys.me', async () => {
    const me = mePayload();
    const mock = stubFetch(routes({ 'POST /api/v1/auth/login': () => json(200, me) }));
    const { result, queryClient } = renderWithWrapper(() => useLogin());
    const invalidate = spyOnInvalidate(queryClient);
    const credentials = { email: 'admin@example.com', password: 'correct horse' };
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(credentials);
    });

    expect(resolved).toEqual(me);
    expect(queryClient.getQueryData(queryKeys.me)).toEqual(me);
    expect(invalidate).not.toHaveBeenCalled();
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe('/api/v1/auth/login');
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(credentials));
  });

  it('leaves queryKeys.me empty on invalid_credentials', async () => {
    stubFetch(() => json(401, envelope('invalid_credentials', 'Invalid email or password.')));
    const { result, queryClient } = renderWithWrapper(() => useLogin());
    let failure: unknown;
    await act(async () => {
      failure = await rejection(result.current.mutateAsync({ email: 'a@b.c', password: 'x' }));
    });
    expect(isApiError(failure) && failure.code === 'invalid_credentials').toBe(true);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe('Invalid email or password.');
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
  });

  it('exposes Retry-After seconds on rate_limited', async () => {
    stubFetch(() =>
      json(429, envelope('rate_limited', 'Too many login attempts.'), { 'Retry-After': '60' }),
    );
    const { result } = renderWithWrapper(() => useLogin());
    await act(async () => {
      await rejection(result.current.mutateAsync({ email: 'a@b.c', password: 'x' }));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(429);
    expect(result.current.error?.code).toBe('rate_limited');
    expect(result.current.error?.retryAfter).toBe(60);
  });
});

describe('useLogout', () => {
  const SEEDED: readonly QueryKey[] = [queryKeys.me, queryKeys.config, queryKeys.runs.latest];

  it.each([
    ['204', () => new Response(null, { status: 204 })],
    ['200 with the signed-out user', () => json(200, mePayload())],
  ])('posts with the CSRF header and clears the whole cache on %s', async (_label, respond) => {
    const mock = stubFetch(routes({ 'POST /api/v1/auth/logout': respond }));
    const { result, queryClient } = renderWithWrapper(() => useLogout());
    seed(queryClient, SEEDED);
    let resolved: unknown = 'not resolved';
    await act(async () => {
      resolved = await result.current.mutateAsync();
    });

    expect(resolved).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.config)).toBeUndefined();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe('/api/v1/auth/logout');
    expect(request.headers).toEqual(BODYLESS_MUTATION_HEADERS);
    expect(request.body).toBeUndefined();
  });

  it('clears the cache when the session had already expired (401 unauthenticated)', async () => {
    stubFetch(() => json(401, envelope('unauthenticated', 'Sign in again.')));
    const { result, queryClient } = renderWithWrapper(() => useLogout());
    seed(queryClient, SEEDED);
    let failure: unknown;
    await act(async () => {
      failure = await rejection(result.current.mutateAsync());
    });
    expect(isApiError(failure) && failure.code === 'unauthenticated').toBe(true);
    expect(queryClient.getQueryData(queryKeys.me)).toBeUndefined();
    expect(queryClient.getQueryData(queryKeys.config)).toBeUndefined();
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it.each([
    ['a 500', () => json(500, envelope('internal_error', 'Boom'))],
    [
      'a 401 without the unauthenticated envelope',
      () => new Response('<h1>401</h1>', { status: 401 }),
    ],
    ['an unreachable server', () => Promise.reject(new TypeError('Failed to fetch'))],
  ] as const)('keeps the cache on %s, as the user is still signed in', async (_label, respond) => {
    stubFetch(respond);
    const { result, queryClient } = renderWithWrapper(() => useLogout());
    seed(queryClient, SEEDED);
    await act(async () => {
      await rejection(result.current.mutateAsync());
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    for (const key of SEEDED) {
      expect(queryClient.getQueryData(key)).toEqual({ seeded: key });
    }
  });
});

describe('useCreateRun', () => {
  const body = { window_start: '2026-07-08', window_end: '2026-10-05' };

  it('posts the window, seeds the run, caches its page, invalidates custom lists', async () => {
    const run = runPayload(CUSTOM_RUN_ID, 'custom');
    const page = pagePayload(CUSTOM_RUN_ID, 'custom');
    const mock = stubFetch(
      routes({
        'POST /api/v1/runs': () => json(201, run),
        'GET /api/v1/recommendations': () => json(200, page),
      }),
    );
    const { result, queryClient } = renderWithWrapper(() => useCreateRun());
    const untouched = [
      queryKeys.runs.latest,
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
    ];
    seed(queryClient, [queryKeys.runs.custom(20), queryKeys.runs.custom(5), ...untouched]);
    const invalidate = spyOnInvalidate(queryClient);
    let resolved: unknown;
    let pageOnResolve: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
      pageOnResolve = queryClient.getQueryData(queryKeys.recommendations(run.id));
    });

    expect(resolved).toEqual(run);
    expect(pageOnResolve).toEqual(page);
    expect(queryClient.getQueryData(queryKeys.runs.byId(run.id))).toEqual(run);
    expect(invalidatedKeys(invalidate)).toEqual([queryKeys.runs.customAll]);
    expect(invalidated(queryClient, queryKeys.runs.custom(20))).toBe(true);
    expect(invalidated(queryClient, queryKeys.runs.custom(5))).toBe(true);
    for (const key of untouched) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const [post, read, ...rest] = sent(mock);
    expect(rest).toEqual([]);
    expect(post?.method).toBe('POST');
    expect(post?.parsed.pathname).toBe('/api/v1/runs');
    expect(post?.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(post?.body).toBe(JSON.stringify(body));
    expect(read?.method).toBe('GET');
    expect(read?.parsed.pathname).toBe('/api/v1/recommendations');
    expect(read === undefined ? [] : params(read)).toEqual([['run_id', String(run.id)]]);
    expect(read?.signal).toBeInstanceOf(AbortSignal);
  });

  it('stays pending until the new run’s page is cached, and that page never polls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const run = runPayload(CUSTOM_RUN_ID, 'custom');
    const page = pagePayload(CUSTOM_RUN_ID, 'custom');
    const pageRead = deferred<Response>();
    const mock = stubFetch(
      sequence(
        () => json(201, run),
        () => pageRead.promise,
      ),
    );
    const { result, queryClient } = renderWithWrapper(() => useCreateRun());
    let settled = false;
    let pageOnResolve: unknown;
    const creating = result.current
      .mutateAsync(body)
      .then((created) => {
        pageOnResolve = queryClient.getQueryData(queryKeys.recommendations(created.id));
        return created;
      })
      .finally(() => {
        settled = true;
      });
    await waitFor(() =>
      expect(sentPaths(mock)).toEqual(['POST /api/v1/runs', 'GET /api/v1/recommendations']),
    );
    await nextMacrotask();

    // The POST has answered and the page read is held: the mutation must still be pending.
    expect(settled).toBe(false);
    expect(result.current.isPending).toBe(true);

    pageRead.resolve(json(200, page));
    let resolved: unknown;
    await act(async () => {
      resolved = await creating;
    });
    expect(resolved).toEqual(run);
    expect(pageOnResolve).toEqual(page);

    // No component shows the page yet, so the prefetch reads it once and never polls.
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS * 2));
    expect(mock).toHaveBeenCalledTimes(2);
  });

  it.each([
    ['a 500', () => json(500, envelope('internal_error', 'Database unavailable'))],
    ['an unreachable server', () => Promise.reject(new TypeError('Failed to fetch'))],
  ] as const)(
    'still succeeds when the new run’s page read fails with %s, leaving the error cached',
    async (_label, respondToRead) => {
      const run = runPayload(CUSTOM_RUN_ID, 'custom');
      const mock = stubFetch(
        routes({
          'POST /api/v1/runs': () => json(201, run),
          'GET /api/v1/recommendations': respondToRead,
        }),
      );
      const { result, queryClient } = renderWithWrapper(() => useCreateRun());
      let resolved: unknown;
      await act(async () => {
        resolved = await result.current.mutateAsync(body);
      });

      expect(resolved).toEqual(run);
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(queryClient.getQueryData(queryKeys.runs.byId(run.id))).toEqual(run);
      const pageState = queryClient.getQueryState(queryKeys.recommendations(run.id));
      expect(pageState?.status).toBe('error');
      expect(pageState?.data).toBeUndefined();
      expect(isApiError(pageState?.error)).toBe(true);
      expect(sentPaths(mock)).toEqual(['POST /api/v1/runs', 'GET /api/v1/recommendations']);
    },
  );

  it('surfaces 409 run_in_progress as an ApiError and touches or reads nothing', async () => {
    const mock = stubFetch(
      () => json(409, envelope('run_in_progress', 'An analysis run is in progress')),
    );
    const { result, queryClient } = renderWithWrapper(() => useCreateRun());
    seed(queryClient, [queryKeys.runs.custom(20)]);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync(body));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    const { error } = result.current;
    expect(isApiError(error)).toBe(true);
    expect(error?.status).toBe(409);
    expect(error?.code).toBe('run_in_progress');
    expect(queryClient.getQueryData(queryKeys.runs.byId(CUSTOM_RUN_ID))).toBeUndefined();
    expect(invalidate).not.toHaveBeenCalled();
    expect(invalidated(queryClient, queryKeys.runs.custom(20))).toBe(false);
    expect(queryClient.getQueryState(queryKeys.recommendations(CUSTOM_RUN_ID))).toBeUndefined();
    expect(sentPaths(mock)).toEqual(['POST /api/v1/runs']);
  });
});


describe('useRecordDecision', () => {
  const body: DecisionRequest = {
    subject: { type: 'campaign', id: CAMPAIGN_ID },
    patch: { status: 'paused' },
    recommendation_id: ITEM_ID,
    note: 'Paused after review',
  };

  it('posts the patch, removes unshown pages and invalidates the decision state', async () => {
    const decision = decisionPayload(11, 'recommendation');
    const mock = stubFetch(routes({ 'POST /api/v1/decisions': () => json(201, decision) }));
    const { result, queryClient } = renderWithWrapper(() => useRecordDecision());
    seed(queryClient, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    const invalidate = spyOnInvalidate(queryClient);
    const remove = spyOnRemove(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
    });

    expect(resolved).toEqual(decision);
    expect(remove.mock.calls).toEqual([[INACTIVE_RECOMMENDATIONS]]);
    expect(invalidatedKeys(invalidate)).toEqual(DECISION_STATE_ROOTS);
    expect(invalidatedKeys(invalidate)).not.toContainEqual(queryKeys.sources);
    for (const key of RECOMMENDATIONS_KEYS) {
      expect(queryClient.getQueryState(key)).toBeUndefined();
    }
    for (const key of DECISION_LIST_KEYS) {
      expect(invalidated(queryClient, key)).toBe(true);
    }
    for (const key of UNRELATED_KEYS) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe('/api/v1/decisions');
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.headers['X-Requested-With']).toBe('ads-engine');
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('has refetched a shown decision log by the time mutateAsync resolves', async () => {
    const saved = decisionPayload(2, 'recommendation');
    const before = [decisionPayload(1)];
    const after = [saved, ...before];
    const refetch = deferred<Response>();
    const mock = stubFetch(
      sequence(
        () => json(200, before),
        () => json(201, saved),
        () => refetch.promise,
      ),
    );
    const { result, queryClient } = renderWithWrapper(() => ({
      log: useDecisions(),
      record: useRecordDecision(),
    }));
    await waitFor(() => expect(result.current.log.isSuccess).toBe(true));

    const key = queryKeys.decisions({});
    let settled = false;
    let cachedOnResolve: unknown;
    const recording = result.current.record
      .mutateAsync(body)
      .then((decision) => {
        cachedOnResolve = queryClient.getQueryData(key);
        return decision;
      })
      .finally(() => {
        settled = true;
      });
    const order = ['GET /api/v1/decisions', 'POST /api/v1/decisions', 'GET /api/v1/decisions'];
    await waitFor(() => expect(sentPaths(mock)).toEqual(order));
    await nextMacrotask();

    // The POST has answered and the refetch is held: the mutation must still be waiting on it.
    expect(settled).toBe(false);
    expect(result.current.record.isPending).toBe(true);
    expect(queryClient.getQueryState(key)?.fetchStatus).toBe('fetching');

    refetch.resolve(json(200, after));
    let resolved: unknown;
    await act(async () => {
      resolved = await recording;
    });
    expect(resolved).toEqual(saved);
    expect(cachedOnResolve).toEqual(after);
    expect(sentPaths(mock)).toEqual(order);
    await waitFor(() => expect(result.current.log.data).toEqual(after));
  });

  it('refetches a shown paged discrepancy list through the discrepanciesAll prefix', async () => {
    let saved = false;
    const mock = stubFetch(
      routes({
        'GET /api/v1/discrepancies': () =>
          json(200, saved ? [] : [discrepancyPayload(DISCREPANCY_ID)], pagedHeaders(saved ? 0 : 1)),
        'POST /api/v1/decisions': () => {
          saved = true;
          return json(201, decisionPayload(12, 'recommendation'));
        },
      }),
    );
    const { result } = renderWithWrapper(() => ({
      list: useDiscrepancyPages(),
      record: useRecordDecision(),
    }));
    await waitFor(() => expect(result.current.list.data?.total).toBe(1));
    await act(async () => {
      await result.current.record.mutateAsync(body);
    });
    expect(sent(mock).filter((request) => request.method === 'GET')).toHaveLength(2);
    await waitFor(() => expect(result.current.list.data?.total).toBe(0));
  });

  it('refetches a filtered decision log through the decisionsAll prefix', async () => {
    const budgetBody: DecisionRequest = {
      subject: { type: 'budget', id: BUDGET_ID },
      patch: { daily_budget: '500.00' },
    };
    const before = [decisionPayload(3, 'baseline', 'budget')];
    const after = [decisionPayload(4, 'user', 'budget'), ...before];
    let logReads = 0;
    const mock = stubFetch(
      routes({
        'GET /api/v1/decisions': () => {
          logReads += 1;
          return json(200, logReads === 1 ? before : after);
        },
        'POST /api/v1/decisions': () => json(201, decisionPayload(4, 'user', 'budget')),
      }),
    );
    const filter = { subject_type: 'budget', subject_id: BUDGET_ID } as const;
    const { result } = renderWithWrapper(() => ({
      log: useDecisions(filter),
      record: useRecordDecision(),
    }));
    await waitFor(() => expect(result.current.log.data).toEqual(before));

    await act(async () => {
      await result.current.record.mutateAsync(budgetBody);
    });
    await waitFor(() => expect(result.current.log.data).toEqual(after));

    const logRequests = sent(mock).filter((request) => request.method === 'GET');
    expect(logRequests).toHaveLength(2);
    for (const request of logRequests) {
      expect(request.parsed.pathname).toBe('/api/v1/decisions');
      expect(params(request)).toEqual([
        ['subject_type', 'budget'],
        ['subject_id', String(BUDGET_ID)],
      ]);
    }
  });

  it('removes and invalidates nothing when the patch is rejected', async () => {
    stubFetch(() => json(422, envelope('no_change', 'The patch changes nothing')));
    const { result, queryClient } = renderWithWrapper(() => useRecordDecision());
    seed(queryClient, DECISION_STATE_KEYS);
    const invalidate = spyOnInvalidate(queryClient);
    const remove = spyOnRemove(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync(body));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('no_change');
    expect(invalidate).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
  });
});

describe('useResolveDiscrepancy', () => {
  const body: ResolveRequest = {
    resolution: 'record_intended',
    intended_state: { status: 'paused' },
    note: 'Paused in the platform',
  };

  it('posts to /discrepancies/{id}/resolve, removes unshown pages and invalidates', async () => {
    const updated = discrepancyPayload(
      DISCREPANCY_ID,
      'awaiting_platform',
      'untracked',
      'shared_member',
    );
    const path = `/api/v1/discrepancies/${String(DISCREPANCY_ID)}/resolve`;
    const mock = stubFetch(routes({ [`POST ${path}`]: () => json(200, updated) }));
    const { result, queryClient } = renderWithWrapper(() => useResolveDiscrepancy());
    seed(queryClient, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    const invalidate = spyOnInvalidate(queryClient);
    const remove = spyOnRemove(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync({ id: DISCREPANCY_ID, body });
    });

    expect(resolved).toEqual(updated);
    expect(remove.mock.calls).toEqual([[INACTIVE_RECOMMENDATIONS]]);
    expect(invalidatedKeys(invalidate)).toEqual(DECISION_STATE_ROOTS);
    expect(invalidatedKeys(invalidate)).not.toContainEqual(queryKeys.sources);
    for (const key of RECOMMENDATIONS_KEYS) {
      expect(queryClient.getQueryState(key)).toBeUndefined();
    }
    for (const key of DECISION_LIST_KEYS) {
      expect(invalidated(queryClient, key)).toBe(true);
    }
    for (const key of UNRELATED_KEYS) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe(path);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('has refetched a shown paged discrepancy list by the time mutateAsync resolves', async () => {
    const path = `/api/v1/discrepancies/${String(DISCREPANCY_ID)}/resolve`;
    const closed = discrepancyPayload(DISCREPANCY_ID, 'resolved');
    const refetch = deferred<Response>();
    const mock = stubFetch(
      sequence(
        () => json(200, [discrepancyPayload(DISCREPANCY_ID)], pagedHeaders(1)),
        () => json(200, closed),
        () => refetch.promise,
      ),
    );
    const { result, queryClient } = renderWithWrapper(() => ({
      list: useDiscrepancyPages(),
      resolve: useResolveDiscrepancy(),
    }));
    await waitFor(() => expect(result.current.list.data?.total).toBe(1));

    const key = queryKeys.discrepancyPages(DEFAULT_DISCREPANCY_STATES);
    let settled = false;
    let cachedOnResolve: unknown;
    const resolving = result.current.resolve
      .mutateAsync({ id: DISCREPANCY_ID, body: { resolution: 'platform_correct' } })
      .then((discrepancy) => {
        cachedOnResolve = queryClient.getQueryData(key);
        return discrepancy;
      })
      .finally(() => {
        settled = true;
      });
    const list = 'GET /api/v1/discrepancies';
    await waitFor(() => expect(sentPaths(mock)).toEqual([list, `POST ${path}`, list]));
    await nextMacrotask();

    // The POST has answered and the refetch is held: the mutation must still be waiting on it.
    expect(settled).toBe(false);
    expect(result.current.resolve.isPending).toBe(true);
    expect(queryClient.getQueryState(key)?.fetchStatus).toBe('fetching');

    refetch.resolve(json(200, [], pagedHeaders(0)));
    let resolved: unknown;
    await act(async () => {
      resolved = await resolving;
    });
    expect(resolved).toEqual(closed);
    expect(cachedOnResolve).toMatchObject({ pages: [{ discrepancies: [], total: 0 }] });
    expect(sentPaths(mock)).toEqual([list, `POST ${path}`, list]);
    await waitFor(() => expect(result.current.list.data).toEqual({ discrepancies: [], total: 0 }));
  });

  it('removes and invalidates nothing on 409 discrepancy_resolved', async () => {
    stubFetch(() => json(409, envelope('discrepancy_resolved', 'Already resolved')));
    const { result, queryClient } = renderWithWrapper(() => useResolveDiscrepancy());
    seed(queryClient, DECISION_STATE_KEYS);
    const invalidate = spyOnInvalidate(queryClient);
    const remove = spyOnRemove(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync({ id: DISCREPANCY_ID, body }));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('discrepancy_resolved');
    expect(invalidate).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
  });
});

describe('decision-state writes and the cached recommendations pages', () => {
  /** One of the two writes that refresh the decision state. */
  interface DecisionStateWrite {
    /** The hook as the test titles show it. */
    label: string;
    /** `METHOD pathname` of its request. */
    request: string;
    /** Its success response. */
    respond: () => Response;
    /** Mounts the hook and returns a call that runs one write through `mutateAsync`. */
    hook: () => () => Promise<unknown>;
  }

  const RESOLVE_PATH = `/api/v1/discrepancies/${String(DISCREPANCY_ID)}/resolve`;

  const WRITES: readonly DecisionStateWrite[] = [
    {
      label: 'useRecordDecision',
      request: 'POST /api/v1/decisions',
      respond: () => json(201, decisionPayload(13, 'recommendation')),
      hook: () => {
        const record = useRecordDecision();
        return () =>
          record.mutateAsync({
            subject: { type: 'campaign', id: CAMPAIGN_ID },
            patch: { status: 'paused' },
          });
      },
    },
    {
      label: 'useResolveDiscrepancy',
      request: `POST ${RESOLVE_PATH}`,
      respond: () => json(200, discrepancyPayload(DISCREPANCY_ID, 'resolved')),
      hook: () => {
        const resolve = useResolveDiscrepancy();
        return () =>
          resolve.mutateAsync({ id: DISCREPANCY_ID, body: { resolution: 'platform_correct' } });
      },
    },
  ];

  /**
   * A page whose overlay counts `open` unresolved discrepancies, the count the discrepancy
   * banner (`role="alert"`) is built from.
   *
   * @param open - `overlay.open_discrepancies`.
   * @param runId - A custom run's id; the latest default run's page when omitted.
   * @returns A fresh page payload.
   */
  function pageWithDiscrepancies(open: number, runId?: RunId) {
    const page = runId === undefined ? pagePayload() : pagePayload(runId, 'custom');
    return { ...page, overlay: { open_discrepancies: open } } satisfies {
      overlay: Pick<RecommendationsPage['overlay'], 'open_discrepancies'>;
    };
  }

  it.each(WRITES)(
    '$label drops an unshown page, so the next visit renders only the current overlay',
    async ({ request, respond, hook }) => {
      const current = pageWithDiscrepancies(0);
      const mock = stubFetch(
        routes({ [request]: respond, 'GET /api/v1/recommendations': () => json(200, current) }),
      );
      // The app's freshness: a page kept in the cache would render at once, without a read.
      const setup = createWrapper(APP_STALE_MS);
      setup.queryClient.setQueryData(
        queryKeys.recommendations('default'),
        pageWithDiscrepancies(1),
      );
      const { result: write } = renderWithWrapper(hook, setup);
      await act(async () => {
        await write.current();
      });
      expect(setup.queryClient.getQueryState(queryKeys.recommendations('default'))).toBeUndefined();
      // Nothing showed the page, so the write read none.
      expect(sentPaths(mock)).toEqual([request]);

      const { result: visit } = renderWithWrapper(() => useRecommendations(), setup);
      // The first render has no page: the outdated count of 1 is never shown.
      expect(visit.current.data).toBeUndefined();
      expect(visit.current.isPending).toBe(true);
      await waitFor(() => expect(visit.current.data).toEqual(current));
      expect(sentPaths(mock)).toEqual([request, 'GET /api/v1/recommendations']);
    },
  );

  it.each(WRITES)(
    '$label refetches a shown page in place and resolves only once it has landed',
    async ({ request, respond, hook }) => {
      const before = pageWithDiscrepancies(1, CUSTOM_RUN_ID);
      const after = pageWithDiscrepancies(0, CUSTOM_RUN_ID);
      const refetch = deferred<Response>();
      const mock = stubFetch(
        sequence(
          () => json(200, before),
          () => respond(),
          () => refetch.promise,
        ),
      );
      const { result, queryClient } = renderWithWrapper(() => ({
        page: useRecommendations(CUSTOM_RUN_ID),
        write: hook(),
      }));
      await waitFor(() => expect(result.current.page.data).toEqual(before));
      seed(queryClient, DECISION_LIST_KEYS);

      const key = queryKeys.recommendations(CUSTOM_RUN_ID);
      let settled = false;
      let cachedOnResolve: unknown;
      const writing = result.current
        .write()
        .then((value) => {
          cachedOnResolve = queryClient.getQueryData(key);
          return value;
        })
        .finally(() => {
          settled = true;
        });
      const page = 'GET /api/v1/recommendations';
      await waitFor(() => expect(sentPaths(mock)).toEqual([page, request, page]));
      await nextMacrotask();

      // The shown page is kept and refetched, not removed, and the write waits on its refetch.
      expect(settled).toBe(false);
      expect(queryClient.getQueryState(key)?.fetchStatus).toBe('fetching');
      expect(result.current.page.data).toEqual(before);
      for (const listKey of DECISION_LIST_KEYS) {
        expect(invalidated(queryClient, listKey)).toBe(true);
      }

      refetch.resolve(json(200, after));
      await act(async () => {
        await writing;
      });
      expect(cachedOnResolve).toEqual(after);
      const runIdParams = [['run_id', String(CUSTOM_RUN_ID)]];
      const pageReads = sent(mock).filter((sentRequest) => sentRequest.method === 'GET');
      expect(pageReads.map(params)).toEqual([runIdParams, runIdParams]);
      await waitFor(() => expect(result.current.page.data).toEqual(after));
    },
  );
});

describe('useFeedback', () => {
  it('posts to /recommendations/{id}/feedback, invalidating only recommendations', async () => {
    const feedback = feedbackPayload('snooze');
    const path = `/api/v1/recommendations/${String(ITEM_ID)}/feedback`;
    const mock = stubFetch(routes({ [`POST ${path}`]: () => json(201, feedback) }));
    const { result, queryClient } = renderWithWrapper(() => useFeedback());
    const recommendations = [
      queryKeys.recommendations('default'),
      queryKeys.recommendations(CUSTOM_RUN_ID),
    ];
    const others = [
      queryKeys.decisions({}),
      queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
      ...UNRELATED_KEYS,
    ];
    seed(queryClient, [...recommendations, ...others]);
    const invalidate = spyOnInvalidate(queryClient);
    const body: FeedbackRequest = { kind: 'snooze' };
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync({ recommendationId: ITEM_ID, body });
    });

    expect(resolved).toEqual(feedback);
    expect(invalidatedKeys(invalidate)).toEqual([queryKeys.recommendationsAll]);
    for (const key of recommendations) {
      expect(invalidated(queryClient, key)).toBe(true);
    }
    for (const key of others) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe(path);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify({ kind: 'snooze' }));
  });

  it('surfaces 422 feedback_not_allowed for a C0 item and invalidates nothing', async () => {
    stubFetch(() => json(422, envelope('feedback_not_allowed', 'Resolve the discrepancy instead')));
    const { result, queryClient } = renderWithWrapper(() => useFeedback());
    seed(queryClient, [queryKeys.recommendations('default')]);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(
        result.current.mutateAsync({ recommendationId: C0_ITEM_ID, body: { kind: 'dismiss' } }),
      );
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('feedback_not_allowed');
    expect(invalidate).not.toHaveBeenCalled();
    expect(invalidated(queryClient, queryKeys.recommendations('default'))).toBe(false);
  });
});

describe('useRequestSync', () => {
  it('posts to /sources/{source}/sync without a body, invalidating only sources', async () => {
    const view: SyncRequestView = {
      id: 9,
      source: 'google',
      requested_at: '2026-10-06T09:00:00Z',
      picked_at: null,
    };
    const mock = stubFetch(routes({ 'POST /api/v1/sources/google/sync': () => json(202, view) }));
    const { result, queryClient } = renderWithWrapper(() => useRequestSync());
    const others = [
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
      queryKeys.config,
    ];
    seed(queryClient, [queryKeys.sources, ...others]);
    const invalidate = spyOnInvalidate(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync('google');
    });

    expect(resolved).toEqual(view);
    expect(invalidatedKeys(invalidate)).toEqual([queryKeys.sources]);
    expect(invalidated(queryClient, queryKeys.sources)).toBe(true);
    for (const key of others) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe('/api/v1/sources/google/sync');
    expect(request.headers).toEqual(BODYLESS_MUTATION_HEADERS);
    expect(request.body).toBeUndefined();
  });

  it('surfaces 409 sync_pending and invalidates only sources', async () => {
    stubFetch(() => json(409, envelope('sync_pending', 'A sync is already pending')));
    const { result, queryClient } = renderWithWrapper(() => useRequestSync());
    const others = [
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
      queryKeys.config,
    ];
    seed(queryClient, [queryKeys.sources, ...others]);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync('linkedin'));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('sync_pending');
    expect(invalidatedKeys(invalidate)).toEqual([queryKeys.sources]);
    expect(invalidated(queryClient, queryKeys.sources)).toBe(true);
    for (const key of others) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
  });

  it('turns to error on 409 sync_pending only once a shown sources query has refetched', async () => {
    const pending = [{ source: 'linkedin', sync_pending: true }] satisfies Pick<
      Source,
      'source' | 'sync_pending'
    >[];
    const refetch = deferred<Response>();
    const mock = stubFetch(
      sequence(
        () => json(200, sourcesPayload()),
        () => json(409, envelope('sync_pending', 'A sync is already pending')),
        () => refetch.promise,
      ),
    );
    const { result, queryClient } = renderWithWrapper(() => ({
      sources: useSources(),
      sync: useRequestSync(),
    }));
    await waitFor(() => expect(result.current.sources.isSuccess).toBe(true));

    let settled = false;
    let cachedOnReject: unknown;
    const requesting = rejection(
      result.current.sync.mutateAsync('linkedin').finally(() => {
        cachedOnReject = queryClient.getQueryData(queryKeys.sources);
        settled = true;
      }),
    );
    const order = [
      'GET /api/v1/sources',
      'POST /api/v1/sources/linkedin/sync',
      'GET /api/v1/sources',
    ];
    await waitFor(() => expect(sentPaths(mock)).toEqual(order));
    await nextMacrotask();

    // The 409 has answered and the refetch is held: the mutation must still be waiting on it.
    expect(settled).toBe(false);
    expect(result.current.sync.isPending).toBe(true);
    expect(queryClient.getQueryState(queryKeys.sources)?.fetchStatus).toBe('fetching');

    refetch.resolve(json(200, pending));
    let rejected: unknown;
    await act(async () => {
      rejected = await requesting;
    });
    expect(isApiError(rejected) && rejected.code).toBe('sync_pending');
    expect(cachedOnReject).toEqual(pending);
    expect(sentPaths(mock)).toEqual(order);
    await waitFor(() => expect(result.current.sync.isError).toBe(true));
  });

  it('invalidates nothing for any other failure', async () => {
    stubFetch(() => json(503, envelope('service_unavailable', 'Database unavailable')));
    const { result, queryClient } = renderWithWrapper(() => useRequestSync());
    seed(queryClient, [queryKeys.sources]);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync('google'));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('service_unavailable');
    expect(invalidate).not.toHaveBeenCalled();
    expect(invalidated(queryClient, queryKeys.sources)).toBe(false);
  });
});

describe('writes while the browser is offline', () => {
  /** The message `apiFetch` gives a request that never reached the server. */
  const NETWORK_MESSAGE = 'The server could not be reached. Check your connection and try again.';

  /** One write hook under test: its mutation's state and a call that sends one write. */
  interface OfflineWrite {
    send: () => Promise<unknown>;
    isPaused: boolean;
    isError: boolean;
    error: ApiError | null;
  }

  /** The mutation's state and `send`, from any of the write hooks. */
  function offlineWrite(
    mutation: Pick<OfflineWrite, 'isPaused' | 'isError' | 'error'>,
    send: () => Promise<unknown>,
  ): OfflineWrite {
    return { send, isPaused: mutation.isPaused, isError: mutation.isError, error: mutation.error };
  }

  /** {@link useLogin}, signing in. */
  function useLoginWrite(): OfflineWrite {
    const mutation = useLogin();
    return offlineWrite(mutation, () =>
      mutation.mutateAsync({ email: 'admin@example.com', password: 'correct horse' }),
    );
  }

  /** {@link useLogout}, signing out. */
  function useLogoutWrite(): OfflineWrite {
    const mutation = useLogout();
    return offlineWrite(mutation, () => mutation.mutateAsync());
  }

  /** {@link useCreateRun}, for a custom window. */
  function useCreateRunWrite(): OfflineWrite {
    const mutation = useCreateRun();
    return offlineWrite(mutation, () =>
      mutation.mutateAsync({ window_start: '2026-07-08', window_end: '2026-10-05' }),
    );
  }

  /** {@link useRecordDecision}, pausing a campaign. */
  function useRecordDecisionWrite(): OfflineWrite {
    const mutation = useRecordDecision();
    return offlineWrite(mutation, () =>
      mutation.mutateAsync({
        subject: { type: 'campaign', id: CAMPAIGN_ID },
        patch: { status: 'paused' },
      }),
    );
  }

  /** {@link useResolveDiscrepancy}, confirming the platform state. */
  function useResolveDiscrepancyWrite(): OfflineWrite {
    const mutation = useResolveDiscrepancy();
    return offlineWrite(mutation, () =>
      mutation.mutateAsync({ id: DISCREPANCY_ID, body: { resolution: 'platform_correct' } }),
    );
  }

  /** {@link useFeedback}, dismissing an item. */
  function useFeedbackWrite(): OfflineWrite {
    const mutation = useFeedback();
    return offlineWrite(mutation, () =>
      mutation.mutateAsync({ recommendationId: ITEM_ID, body: { kind: 'dismiss' } }),
    );
  }

  /** {@link useRequestSync}, for Google. */
  function useRequestSyncWrite(): OfflineWrite {
    const mutation = useRequestSync();
    return offlineWrite(mutation, () => mutation.mutateAsync('google'));
  }

  it('runs every write in the always network mode', () => {
    expect(WRITE_NETWORK_MODE).toBe('always');
  });

  it.each([
    ['useLogin', useLoginWrite],
    ['useLogout', useLogoutWrite],
    ['useCreateRun', useCreateRunWrite],
    ['useRecordDecision', useRecordDecisionWrite],
    ['useResolveDiscrepancy', useResolveDiscrepancyWrite],
    ['useFeedback', useFeedbackWrite],
    ['useRequestSync', useRequestSyncWrite],
  ] as const)(
    '%s fails at once with network_error, never paused, and sends nothing on reconnect',
    async (_name, useWrite) => {
      // A browser offline rejects every fetch, as this stub does.
      const mock = stubFetch(() => Promise.reject(new TypeError('Failed to fetch')));
      const { result, queryClient } = renderWithWrapper(useWrite);
      seed(queryClient, [queryKeys.recommendations('default'), queryKeys.sources]);
      const invalidate = spyOnInvalidate(queryClient);
      let settled: Promise<unknown> = Promise.resolve();
      onlineManager.setOnline(false);
      try {
        act(() => {
          settled = rejection(result.current.send());
        });

        await waitFor(() => expect(result.current.isError).toBe(true));
        expect(result.current.isPaused).toBe(false);
        expect(result.current.error?.status).toBe(0);
        expect(result.current.error?.code).toBe('network_error');
        expect(result.current.error?.message).toBe(NETWORK_MESSAGE);
        const rejected = await settled;
        expect(isApiError(rejected) && rejected.code).toBe('network_error');
        expect(mock).toHaveBeenCalledTimes(1);
        expect(invalidate).not.toHaveBeenCalled();
      } finally {
        onlineManager.setOnline(true);
      }

      // Back online, nothing paused resumes: the write is sent again only when the user retries.
      await act(async () => {
        await queryClient.resumePausedMutations();
      });
      expect(mock).toHaveBeenCalledTimes(1);
      expect(queryClient.getMutationCache().getAll().some((m) => m.state.isPaused)).toBe(false);
    },
  );
});
