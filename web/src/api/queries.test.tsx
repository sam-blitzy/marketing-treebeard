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
 * - `useDiscrepancies` paging: a first page at `limit=500`, every `Link: rel="next"` page
 *   followed exactly as named however short the page, a repeated page rejected, and one
 *   restart from the first page after a 422 `invalid_cursor`;
 * - every mutation's request (CSRF header, JSON body, encoded path) and its cache effects: the
 *   exact `invalidateQueries` keys (spied), the seeded and invalidated entries, the keys left
 *   alone, and nothing touched on failure.
 *
 * The file is self-contained. It imports only `./queries`, `./client` and `./types`: the shared
 * helpers in `src/test/` import this folder, so importing them here would close a cycle.
 * Payloads are minimal inline objects, because `apiFetch` does not validate response shapes;
 * each is checked with `satisfies` against the fields of its `./types` type that it carries.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
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
  FIRST_RUN_POLL_MS,
  SOURCES_POLL_MS,
  isApiError,
  isAwaitingFirstRun,
  meQueryOptions,
  queryKeys,
  useConfig,
  useCreateRun,
  useCustomRuns,
  useDecisions,
  useDefaultRun,
  useDiscrepancies,
  useFeedback,
  useLogin,
  useLogout,
  useMe,
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
  Discrepancy,
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

/**
 * A query client that never retries and never garbage-collects, and the provider for it.
 *
 * @returns The client and a `wrapper` for `renderHook`.
 */
function createWrapper(): WrapperSetup {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity }, mutations: { retry: false } },
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

/**
 * A decision-log row.
 *
 * @param id - The decision id.
 * @param origin - How the decision was written.
 * @returns A fresh decision payload.
 */
function decisionPayload(id: Decision['id'], origin: Decision['origin'] = 'baseline') {
  return { id, shape: 'own_budget', origin } satisfies Pick<Decision, 'id' | 'shape' | 'origin'>;
}

/**
 * A status discrepancy.
 *
 * @param id - The discrepancy id.
 * @param state - Its state.
 * @returns A fresh discrepancy payload.
 */
function discrepancyPayload(id: Discrepancy['id'], state: Discrepancy['state'] = 'open') {
  return { id, kind: 'status', state } satisfies Pick<Discrepancy, 'id' | 'kind' | 'state'>;
}

/**
 * Discrepancies with consecutive ids.
 *
 * @param first - The first id.
 * @param count - How many to build.
 * @returns `count` fresh status discrepancies, ids `first` upwards.
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

afterEach(() => {
  for (const client of clients.splice(0)) {
    client.clear();
  }
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
});

// ---------------------------------------------------------------------------------------------
// Sources and configuration
// ---------------------------------------------------------------------------------------------

describe('useSources', () => {
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

/** Cached variants under those roots, every one of which the prefix match must reach. */
const DECISION_STATE_KEYS: readonly QueryKey[] = [
  queryKeys.recommendations('default'),
  queryKeys.recommendations(CUSTOM_RUN_ID),
  queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
  queryKeys.discrepancies(['resolved']),
  queryKeys.decisions({}),
  queryKeys.decisions({ subject_type: 'campaign', subject_id: CAMPAIGN_ID }),
];

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

  it('posts the window, seeds runs.byId and invalidates only the custom-run lists', async () => {
    const run = runPayload(CUSTOM_RUN_ID, 'custom');
    const mock = stubFetch(routes({ 'POST /api/v1/runs': () => json(201, run) }));
    const { result, queryClient } = renderWithWrapper(() => useCreateRun());
    const untouched = [
      queryKeys.runs.latest,
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
    ];
    seed(queryClient, [queryKeys.runs.custom(20), queryKeys.runs.custom(5), ...untouched]);
    const invalidate = spyOnInvalidate(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
    });

    expect(resolved).toEqual(run);
    expect(queryClient.getQueryData(queryKeys.runs.byId(run.id))).toEqual(run);
    expect(invalidatedKeys(invalidate)).toEqual([queryKeys.runs.customAll]);
    expect(invalidated(queryClient, queryKeys.runs.custom(20))).toBe(true);
    expect(invalidated(queryClient, queryKeys.runs.custom(5))).toBe(true);
    for (const key of untouched) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.method).toBe('POST');
    expect(request.parsed.pathname).toBe('/api/v1/runs');
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('surfaces 409 run_in_progress as an ApiError and seeds or invalidates nothing', async () => {
    stubFetch(() => json(409, envelope('run_in_progress', 'An analysis run is in progress')));
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
  });
});


describe('useRecordDecision', () => {
  const body: DecisionRequest = {
    subject: { type: 'campaign', id: CAMPAIGN_ID },
    patch: { status: 'paused' },
    recommendation_id: ITEM_ID,
    note: 'Paused after review',
  };

  it('posts the patch and invalidates recommendations, discrepancies and decisions', async () => {
    const decision = decisionPayload(11, 'recommendation');
    const mock = stubFetch(routes({ 'POST /api/v1/decisions': () => json(201, decision) }));
    const { result, queryClient } = renderWithWrapper(() => useRecordDecision());
    seed(queryClient, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    const invalidate = spyOnInvalidate(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
    });

    expect(resolved).toEqual(decision);
    expect(invalidatedKeys(invalidate)).toEqual(DECISION_STATE_ROOTS);
    expect(invalidatedKeys(invalidate)).not.toContainEqual(queryKeys.sources);
    for (const key of DECISION_STATE_KEYS) {
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
    const before = [decisionPayload(1)];
    const after = [decisionPayload(2, 'user'), ...before];
    const mock = stubFetch(
      sequence(
        () => json(200, before),
        () => json(201, decisionPayload(2, 'user')),
        () => json(200, after),
      ),
    );
    const { result } = renderWithWrapper(() => ({
      log: useDecisions(),
      record: useRecordDecision(),
    }));
    await waitFor(() => expect(result.current.log.isSuccess).toBe(true));
    await act(async () => {
      await result.current.record.mutateAsync(body);
    });
    const calls = sent(mock).map((request) => `${request.method ?? ''} ${request.parsed.pathname}`);
    expect(calls).toEqual([
      'GET /api/v1/decisions',
      'POST /api/v1/decisions',
      'GET /api/v1/decisions',
    ]);
    await waitFor(() => expect(result.current.log.data).toEqual(after));
  });

  it('refetches a filtered decision log through the decisionsAll prefix', async () => {
    const budgetBody: DecisionRequest = {
      subject: { type: 'budget', id: BUDGET_ID },
      patch: { daily_budget: '500.00' },
    };
    const before = [decisionPayload(3)];
    const after = [decisionPayload(4, 'user'), ...before];
    let logReads = 0;
    const mock = stubFetch(
      routes({
        'GET /api/v1/decisions': () => {
          logReads += 1;
          return json(200, logReads === 1 ? before : after);
        },
        'POST /api/v1/decisions': () => json(201, decisionPayload(4, 'user')),
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

  it('invalidates nothing when the patch is rejected', async () => {
    stubFetch(() => json(422, envelope('no_change', 'The patch changes nothing')));
    const { result, queryClient } = renderWithWrapper(() => useRecordDecision());
    seed(queryClient, DECISION_STATE_KEYS);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync(body));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('no_change');
    expect(invalidate).not.toHaveBeenCalled();
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

  it('posts to /discrepancies/{id}/resolve and invalidates the decision state', async () => {
    const updated = discrepancyPayload(DISCREPANCY_ID, 'awaiting_platform');
    const path = `/api/v1/discrepancies/${String(DISCREPANCY_ID)}/resolve`;
    const mock = stubFetch(routes({ [`POST ${path}`]: () => json(200, updated) }));
    const { result, queryClient } = renderWithWrapper(() => useResolveDiscrepancy());
    seed(queryClient, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    const invalidate = spyOnInvalidate(queryClient);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync({ id: DISCREPANCY_ID, body });
    });

    expect(resolved).toEqual(updated);
    expect(invalidatedKeys(invalidate)).toEqual(DECISION_STATE_ROOTS);
    expect(invalidatedKeys(invalidate)).not.toContainEqual(queryKeys.sources);
    for (const key of DECISION_STATE_KEYS) {
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

  it('invalidates nothing on 409 discrepancy_resolved', async () => {
    stubFetch(() => json(409, envelope('discrepancy_resolved', 'Already resolved')));
    const { result, queryClient } = renderWithWrapper(() => useResolveDiscrepancy());
    seed(queryClient, DECISION_STATE_KEYS);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync({ id: DISCREPANCY_ID, body }));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('discrepancy_resolved');
    expect(invalidate).not.toHaveBeenCalled();
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(queryClient, key)).toBe(false);
    }
  });
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

  it('surfaces 409 sync_pending and leaves the sources valid', async () => {
    stubFetch(() => json(409, envelope('sync_pending', 'A sync is already pending')));
    const { result, queryClient } = renderWithWrapper(() => useRequestSync());
    seed(queryClient, [queryKeys.sources]);
    const invalidate = spyOnInvalidate(queryClient);
    await act(async () => {
      await rejection(result.current.mutateAsync('linkedin'));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('sync_pending');
    expect(invalidate).not.toHaveBeenCalled();
    expect(invalidated(queryClient, queryKeys.sources)).toBe(false);
  });
});

