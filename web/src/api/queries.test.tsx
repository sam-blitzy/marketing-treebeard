/**
 * Unit tests for src/api/queries.ts, the TanStack Query layer through which the SPA reads and
 * writes every piece of `/api/v1` server state (AAP 0.5.1 "Web API layer", 0.5.3, 0.11.1).
 *
 * Each hook runs under `renderHook` inside this file's own `QueryClientProvider`, whose client
 * never retries, so a failure surfaces on the first response. The global `fetch` is replaced
 * with `vi.stubGlobal('fetch', …)` and answers with real `Response`s built from the typed
 * payload factories in `src/test/fixtures.ts`. The cases cover:
 *
 * - the query keys, the polling and freshness constants and the re-exported error API;
 * - every query hook's request (method, `/api/v1` path, query string, abort signal), its cache
 *   key, the `skipToken` placeholder of `useRun(undefined)`, the 5-minute freshness of the
 *   user and configuration reads, cancellation on unmount, and the poll interval each read
 *   chooses after a success, a missing first run, a transient failure and a final error;
 * - `useDiscrepancies` paging: a first page at `limit=500`, every `Link: rel="next"` page
 *   followed exactly as named however short the page, a repeated page rejected, and one
 *   restart from the first page after a 422 `invalid_cursor`;
 * - every mutation's request (CSRF header, JSON body, encoded path) and its cache effects:
 *   seeded and invalidated keys, the keys left alone, and nothing touched on failure.
 *
 * The file is self-contained: it imports no shared render or API-mock helper.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { QueryKey } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';

import {
  CUSTOM_RUN_ID,
  DEFAULT_RUN_ID,
  FIXTURE_DISCREPANCY_ID,
  FIXTURE_ITEM_IDS,
  makeConfig,
  makeDecision,
  makeDiscrepancy,
  makeFeedback,
  makeMe,
  makeRecommendationsPage,
  makeRun,
  makeSources,
} from '../test/fixtures';
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
  DecisionRequest,
  Discrepancy,
  DiscrepancyState,
  ResolveRequest,
  RunId,
  SyncRequestView,
} from './types';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The stubbed global `fetch`. */
type FetchMock = Mock<typeof fetch>;

/** Builds the response to one request. */
type Responder = (url: string, init: RequestInit) => Response | Promise<Response>;

/** One request as the stub received it. */
interface SentRequest {
  /** `init.method`. */
  method: string | undefined;
  /** The URL the client passed to `fetch`. */
  url: string;
  /** `init.headers`, which the client always builds as a plain record. */
  headers: Record<string, string>;
  /** `init.body`, the JSON text of a mutation, `undefined` without one. */
  body: BodyInit | null | undefined;
  /** `init.signal`. */
  signal: AbortSignal | null | undefined;
}

/** Every `QueryClient` a test created, cleared after it so no timer outlives the test. */
const clients: QueryClient[] = [];

/** The 5-minute freshness of `GET /auth/me` and `GET /config`. */
const LONG_STALE_MS = 5 * 60_000;

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

/**
 * A query client that never retries, so a failure surfaces at once.
 *
 * @param retry - The default query `retry`; `false` unless a test proves an option overrides it.
 * @returns A fresh client, cleared after the test.
 */
function createClient(retry: boolean | number = false): QueryClient {
  const client = new QueryClient({
    defaultOptions: { queries: { retry }, mutations: { retry: false } },
  });
  clients.push(client);
  return client;
}

/**
 * Renders `hook` inside a `QueryClientProvider` for `client`.
 *
 * @param hook - The hook (or hooks) under test.
 * @param client - The query client; a fresh non-retrying one when omitted.
 * @returns `renderHook`'s result plus the client.
 */
function renderWithClient<Result>(hook: () => Result, client: QueryClient = createClient()) {
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return { client, ...renderHook(hook, { wrapper: Wrapper }) };
}

/**
 * Replaces the global `fetch` with a mock that answers through `responder`.
 *
 * @param responder - Builds each response.
 * @returns The mock.
 */
function stubFetch(responder: Responder): FetchMock {
  const mock = vi.fn<typeof fetch>(async (input, init) => responder(String(input), init ?? {}));
  vi.stubGlobal('fetch', mock);
  return mock;
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
  return json(envelope('unexpected_request', `${init.method ?? 'GET'} ${url}`), 418);
}

/**
 * A responder that answers `METHOD url` from `table`.
 *
 * @param table - Responses keyed by method and full URL, for example `GET /api/v1/sources`.
 * @returns The responder.
 */
function routes(table: Record<string, () => Response>): Responder {
  return (url, init) => {
    const respond = table[`${init.method ?? 'GET'} ${url}`];
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
 * @param mock - The stubbed `fetch`.
 * @returns The requests.
 */
function sent(mock: FetchMock): SentRequest[] {
  return mock.mock.calls.map(([input, init]) => ({
    method: init?.method,
    url: String(input),
    headers: (init?.headers ?? {}) as Record<string, string>,
    body: init?.body,
    signal: init?.signal,
  }));
}

/**
 * The single request the stub received.
 *
 * @param mock - The stubbed `fetch`.
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
 * A JSON response.
 *
 * @param body - The value to serialise.
 * @param status - The HTTP status; 200 when omitted.
 * @param headers - Extra response headers.
 * @returns A fresh `Response`.
 */
function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
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
 * Discrepancies with consecutive ids.
 *
 * @param first - The first id.
 * @param count - How many to build.
 * @returns `count` fresh status discrepancies, ids `first` upwards.
 */
function discrepancies(first: number, count: number): Discrepancy[] {
  return Array.from({ length: count }, (_, offset) =>
    makeDiscrepancy('status', { id: first + offset }),
  );
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
    expect(queryKeys.runs.byId(42)).toEqual(['runs', 'byId', 42]);
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

  it('nest every variant under its root', () => {
    expect(queryKeys.runs.latest.slice(0, 1)).toEqual(queryKeys.runs.all);
    expect(queryKeys.runs.custom(5).slice(0, 2)).toEqual(queryKeys.runs.customAll);
    expect(queryKeys.recommendations(7).slice(0, 1)).toEqual(queryKeys.recommendationsAll);
    expect(queryKeys.decisions({}).slice(0, 1)).toEqual(queryKeys.decisionsAll);
    expect(queryKeys.discrepancies(['open']).slice(0, 1)).toEqual(queryKeys.discrepanciesAll);
  });

  it('drop undefined decision filter members, so equal filters share one key', () => {
    expect(queryKeys.decisions({})).toEqual(['decisions', {}]);
    const [, partial] = queryKeys.decisions({ subject_type: 'budget', subject_id: undefined });
    expect(Object.keys(partial)).toEqual(['subject_type']);
    expect(queryKeys.decisions({ subject_type: 'campaign', subject_id: 301 })).toEqual([
      'decisions',
      { subject_type: 'campaign', subject_id: 301 },
    ]);
    expect(Object.keys(queryKeys.decisions({ subject_id: undefined })[1])).toEqual([]);
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
  it('is true for not_found and run_not_ready ApiErrors only', () => {
    expect(isAwaitingFirstRun(new ApiError(404, 'not_found', 'No analysis run yet'))).toBe(true);
    expect(isAwaitingFirstRun(new ApiError(409, 'run_not_ready', 'Run 7 is running'))).toBe(true);
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
    expect(options.queryKey).toEqual(['auth', 'me']);
    expect(options.staleTime).toBe(LONG_STALE_MS);
    expect(options.retry).toBe(false);
  });

  it('resolves the user from GET /api/v1/auth/me with an abort signal', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/auth/me': () => json(makeMe()) }));
    const client = createClient();
    await expect(client.fetchQuery(meQueryOptions())).resolves.toEqual(makeMe());
    const request = onlyRequest(mock);
    expect(request.headers).toEqual({ Accept: 'application/json' });
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('rejects a signed-out user after one request, even when the client retries', async () => {
    const mock = stubFetch(() => json(envelope('unauthenticated', 'Sign in again.'), 401));
    const client = createClient(3);
    const error = await rejection(client.ensureQueryData(meQueryOptions()));
    expect(isApiError(error) && error.status === 401 && error.code === 'unauthenticated').toBe(
      true,
    );
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('useMe exposes the user and does not refetch on a second mount', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/auth/me': () => json(makeMe()) }));
    const { result, client } = renderWithClient(() => useMe());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(makeMe());
    expect(client.getQueryData(queryKeys.me)).toEqual(makeMe());

    const second = renderWithClient(() => useMe(), client);
    await act(() => Promise.resolve());
    expect(second.result.current.data).toEqual(makeMe());
    expect(second.result.current.isStale).toBe(false);
    expect(mock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------------------------

describe('useDefaultRun', () => {
  it('reads GET /api/v1/runs/latest under queryKeys.runs.latest', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs/latest': () => json(makeRun()) }));
    const { result, client } = renderWithClient(() => useDefaultRun());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(makeRun());
    expect(client.getQueryData(queryKeys.runs.latest)).toEqual(makeRun());
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(client, queryKeys.runs.latest)).toBe(CURRENT_STATE_POLL_MS);
  });

  it.each([
    [
      '404 not_found before the first run',
      FIRST_RUN_POLL_MS,
      () => json(envelope('not_found', 'No analysis run yet'), 404),
    ],
    ['409 run_not_ready', FIRST_RUN_POLL_MS, () => json(envelope('run_not_ready', 'Running'), 409)],
    [
      'an unreachable server',
      CURRENT_STATE_POLL_MS,
      () => Promise.reject(new TypeError('Failed to fetch')),
    ],
    ['a 429', CURRENT_STATE_POLL_MS, () => json(envelope('rate_limited', 'Slow down'), 429)],
    [
      'nginx 502 while the API restarts',
      CURRENT_STATE_POLL_MS,
      () => new Response('<html>Bad Gateway</html>', { status: 502 }),
    ],
    ['a 500', CURRENT_STATE_POLL_MS, () => json(envelope('internal_error', 'Boom'), 500)],
    ['an expired session', false, () => json(envelope('unauthenticated', 'Sign in again.'), 401)],
    ['a 422', false, () => json(envelope('validation_error', 'Bad'), 422)],
  ] as const)('after %s polls at %s', async (_label, interval, respond) => {
    stubFetch(respond);
    const { result, client } = renderWithClient(() => useDefaultRun());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(isApiError(result.current.error)).toBe(true);
    expect(pollInterval(client, queryKeys.runs.latest)).toBe(interval);
  });

  it('refetches every 15 s while no run exists, then every 30 s once one does', async () => {
    // Only the timers are faked. Each fetch outcome restarts the interval a few milliseconds
    // after its tick, so the checks sit a second either side of each expected refetch.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
    const margin = 1_000;
    const mock = stubFetch(
      sequence(
        () => json(envelope('not_found', 'No analysis run yet'), 404),
        () => json(makeRun()),
        () => json(makeRun({ id: DEFAULT_RUN_ID + 1 })),
      ),
    );
    const { result } = renderWithClient(() => useDefaultRun());
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(mock).toHaveBeenCalledTimes(1);
    expect(result.current.error?.code).toBe('not_found');

    await act(() => vi.advanceTimersByTimeAsync(FIRST_RUN_POLL_MS - margin));
    expect(mock).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(2 * margin));
    expect(mock).toHaveBeenCalledTimes(2);
    expect(result.current.data?.id).toBe(DEFAULT_RUN_ID);

    // A 15 s cadence would refetch within the next 15 s; the 30 s cadence does not.
    await act(() => vi.advanceTimersByTimeAsync(CURRENT_STATE_POLL_MS - 2 * margin));
    expect(mock).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(2 * margin));
    expect(mock).toHaveBeenCalledTimes(3);
    expect(result.current.data?.id).toBe(DEFAULT_RUN_ID + 1);
    expect(sent(mock).map((request) => request.url)).toEqual([
      '/api/v1/runs/latest',
      '/api/v1/runs/latest',
      '/api/v1/runs/latest',
    ]);
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
    const { unmount } = renderWithClient(() => useDefaultRun());
    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1));
    const { signal } = onlyRequest(mock);
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});

describe('useRun', () => {
  it('sends nothing while no run is addressed (skipToken)', async () => {
    const mock = stubFetch(() => json(makeRun()));
    const { result, client } = renderWithClient(() => useRun(undefined));
    await act(() => Promise.resolve());
    expect(mock).not.toHaveBeenCalled();
    expect(result.current.status).toBe('pending');
    expect(result.current.fetchStatus).toBe('idle');
    expect(client.getQueryState(queryKeys.runs.byId(undefined))).toBeDefined();
  });

  it('reads GET /api/v1/runs/{id} once an id arrives', async () => {
    const run = makeRun({ id: CUSTOM_RUN_ID, kind: 'custom' });
    const mock = stubFetch(
      routes({ [`GET /api/v1/runs/${String(CUSTOM_RUN_ID)}`]: () => json(run) }),
    );
    const addressed: { id: RunId | undefined } = { id: undefined };
    const { result, rerender, client } = renderWithClient(() => useRun(addressed.id));
    await act(() => Promise.resolve());
    expect(mock).not.toHaveBeenCalled();

    addressed.id = CUSTOM_RUN_ID;
    rerender();
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(run);
    expect(client.getQueryData(queryKeys.runs.byId(CUSTOM_RUN_ID))).toEqual(run);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
  });

  it('encodes the id as one path segment', async () => {
    const mock = stubFetch(() => json(makeRun()));
    const { result } = renderWithClient(() => useRun('7/../x y' as unknown as RunId));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(onlyRequest(mock).url).toBe('/api/v1/runs/7%2F..%2Fx%20y');
  });

  it('surfaces a 404 for an unknown id as an ApiError', async () => {
    stubFetch(() => json(envelope('not_found', 'Run 9 not found'), 404));
    const { result } = renderWithClient(() => useRun(9));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(404);
    expect(result.current.error?.code).toBe('not_found');
  });
});

describe('useCustomRuns', () => {
  it('lists 20 custom runs by default', async () => {
    const runs = [makeRun({ id: CUSTOM_RUN_ID, kind: 'custom' })];
    const mock = stubFetch(routes({ 'GET /api/v1/runs?kind=custom&limit=20': () => json(runs) }));
    const { result, client } = renderWithClient(() => useCustomRuns());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(runs);
    expect(client.getQueryData(queryKeys.runs.custom(20))).toEqual(runs);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
  });

  it('sends the limit it is given', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/runs?kind=custom&limit=5': () => json([]) }));
    const { result, client } = renderWithClient(() => useCustomRuns(5));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([]);
    expect(client.getQueryData(queryKeys.runs.custom(5))).toEqual([]);
    expect(mock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------------------------
// Recommendations
// ---------------------------------------------------------------------------------------------

describe('useRecommendations', () => {
  it('reads the default run without run_id under the "default" key', async () => {
    const page = makeRecommendationsPage();
    const mock = stubFetch(routes({ 'GET /api/v1/recommendations': () => json(page) }));
    const { result, client } = renderWithClient(() => useRecommendations());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(page);
    expect(client.getQueryData(queryKeys.recommendations('default'))).toEqual(page);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(client, queryKeys.recommendations('default'))).toBe(CURRENT_STATE_POLL_MS);
  });

  it('reads a custom run with run_id under its id', async () => {
    const page = makeRecommendationsPage({ run: makeRun({ id: CUSTOM_RUN_ID, kind: 'custom' }) });
    const url = `/api/v1/recommendations?run_id=${String(CUSTOM_RUN_ID)}`;
    stubFetch(routes({ [`GET ${url}`]: () => json(page) }));
    const { result, client } = renderWithClient(() => useRecommendations(CUSTOM_RUN_ID));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.run.id).toBe(CUSTOM_RUN_ID);
    const key = queryKeys.recommendations(CUSTOM_RUN_ID);
    expect(client.getQueryData(key)).toEqual(page);
    expect(pollInterval(client, key)).toBe(CURRENT_STATE_POLL_MS);
  });

  it.each([
    ['the default page', undefined, 404, 'not_found', FIRST_RUN_POLL_MS],
    ['the default page', undefined, 401, 'unauthenticated', false],
    ['the default page', undefined, 503, 'internal_error', CURRENT_STATE_POLL_MS],
    ['a custom page', CUSTOM_RUN_ID, 404, 'not_found', false],
    ['a custom page', CUSTOM_RUN_ID, 409, 'run_not_ready', false],
    ['a custom page', CUSTOM_RUN_ID, 502, 'internal_error', CURRENT_STATE_POLL_MS],
  ] as const)('%s after a %i %s polls at %s', async (_label, runId, status, code, interval) => {
    stubFetch(() => json(envelope(code, 'x'), status));
    const { result, client } = renderWithClient(() => useRecommendations(runId));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe(code);
    expect(pollInterval(client, queryKeys.recommendations(runId ?? 'default'))).toBe(interval);
  });
});

// ---------------------------------------------------------------------------------------------
// Decisions
// ---------------------------------------------------------------------------------------------

describe('useDecisions', () => {
  it('reads every decision without a filter', async () => {
    const log = [makeDecision('own_budget'), makeDecision('budget')];
    const mock = stubFetch(routes({ 'GET /api/v1/decisions': () => json(log) }));
    const { result, client } = renderWithClient(() => useDecisions());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(log);
    expect(client.getQueryData(queryKeys.decisions({}))).toEqual(log);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
  });

  it('sends subject_type and subject_id when the filter sets them', async () => {
    const log = [makeDecision('own_budget')];
    const mock = stubFetch(
      routes({ 'GET /api/v1/decisions?subject_type=campaign&subject_id=301': () => json(log) }),
    );
    const filter = { subject_type: 'campaign', subject_id: 301 } as const;
    const { result, client } = renderWithClient(() => useDecisions(filter));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(log);
    expect(client.getQueryData(queryKeys.decisions(filter))).toEqual(log);
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('omits an undefined member and shares the key of the filter without it', async () => {
    const mock = stubFetch(routes({ 'GET /api/v1/decisions?subject_type=budget': () => json([]) }));
    const { result, client } = renderWithClient(() =>
      useDecisions({ subject_type: 'budget', subject_id: undefined }),
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(client.getQueryData(queryKeys.decisions({ subject_type: 'budget' }))).toEqual([]);
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('surfaces 422 invalid_query as an ApiError', async () => {
    stubFetch(() => json(envelope('invalid_query', 'subject_id needs subject_type'), 422));
    const { result } = renderWithClient(() => useDecisions({ subject_id: 301 }));
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
    const rows = discrepancies(FIXTURE_DISCREPANCY_ID, 2);
    const mock = stubFetch(routes({ [`GET ${FIRST_PAGE}`]: () => json(rows) }));
    const { result, client } = renderWithClient(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(rows);
    const key = queryKeys.discrepancies(['open', 'awaiting_platform']);
    expect(client.getQueryData(key)).toEqual(rows);
    const request = onlyRequest(mock);
    expect(request.method).toBe('GET');
    expect(request.signal).toBeInstanceOf(AbortSignal);
  });

  it('sends each given state as its own parameter, and no state for an empty list', async () => {
    const mock = stubFetch(
      routes({
        'GET /api/v1/discrepancies?state=resolved&limit=500': () => json([]),
        'GET /api/v1/discrepancies?limit=500': () => json([]),
      }),
    );
    const resolved = renderWithClient(() => useDiscrepancies(['resolved']));
    const none = renderWithClient(() => useDiscrepancies([]));
    await waitFor(() => expect(resolved.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(none.result.current.isSuccess).toBe(true));
    expect(resolved.client.getQueryData(queryKeys.discrepancies(['resolved']))).toEqual([]);
    expect(none.client.getQueryData(queryKeys.discrepancies([]))).toEqual([]);
    expect(sent(mock).map((request) => request.url).sort()).toEqual([
      '/api/v1/discrepancies?limit=500',
      '/api/v1/discrepancies?state=resolved&limit=500',
    ]);
  });

  it('follows every next page exactly as named, however short, until none is named', async () => {
    const first = discrepancies(51, 3);
    const short = discrepancies(60, 1);
    const last = discrepancies(80, 2);
    const mock = stubFetch(
      sequence(
        () => json(first, 200, { Link: discrepancyLink(53) }),
        () => json(short, 200, { Link: discrepancyLink(70) }),
        () => json([], 200, { Link: discrepancyLink(75) }),
        () => json(last),
      ),
    );
    const { result } = renderWithClient(() => useDiscrepancies());
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
        () => json(discrepancies(51, 1), 200, { Link: discrepancyLink(51) }),
        () => json(discrepancies(52, 1), 200, { Link: discrepancyLink(52) }),
        () => json(discrepancies(53, 1), 200, { Link: discrepancyLink(51) }),
      ),
    );
    const { result } = renderWithClient(() => useDiscrepancies());
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
        () => json(discrepancies(51, 2), 200, { Link: discrepancyLink(52) }),
        () => json(envelope('invalid_cursor', 'List again from the first page'), 422),
        () => json(discrepancies(53, 2), 200, { Link: discrepancyLink(54) }),
        () => json(discrepancies(55, 1)),
      ),
    );
    const { result } = renderWithClient(() => useDiscrepancies());
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
        () => json(discrepancies(51, 1), 200, { Link: discrepancyLink(51) }),
        () => json(envelope('invalid_cursor', 'List again'), 422),
        () => json(discrepancies(51, 1), 200, { Link: discrepancyLink(51) }),
        () => json(envelope('invalid_cursor', 'List again'), 422),
      ),
    );
    const { result } = renderWithClient(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(422);
    expect(result.current.error?.code).toBe('invalid_cursor');
    expect(mock).toHaveBeenCalledTimes(4);
  });

  it('does not restart for invalid_cursor on the first page', async () => {
    const mock = stubFetch(() => json(envelope('invalid_cursor', 'List again'), 422));
    const { result } = renderWithClient(() => useDiscrepancies());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('invalid_cursor');
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['a 500', () => json(envelope('internal_error', 'Boom'), 500), 'internal_error'],
    [
      'an unreachable server',
      () => Promise.reject(new TypeError('Failed to fetch')),
      'network_error',
    ],
    [
      'a link off the API base',
      () => json([], 200, { Link: '<https://evil.example/x>; rel=next' }),
      'invalid_response',
    ],
  ] as const)('rejects %s on a later page without a restart', async (_label, later, code) => {
    const mock = stubFetch(
      sequence(() => json(discrepancies(51, 1), 200, { Link: discrepancyLink(51) }), later),
    );
    const { result } = renderWithClient(() => useDiscrepancies());
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
    const sources = makeSources();
    const mock = stubFetch(routes({ 'GET /api/v1/sources': () => json(sources) }));
    const { result, client } = renderWithClient(() => useSources());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((source) => source.source)).toEqual([
      'google',
      'linkedin',
      'hubspot',
    ]);
    expect(client.getQueryData(queryKeys.sources)).toEqual(sources);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);
    expect(pollInterval(client, queryKeys.sources)).toBe(SOURCES_POLL_MS);
  });
});

describe('useConfig', () => {
  it('reads GET /api/v1/config and stays fresh for a second mount', async () => {
    const config = makeConfig();
    const mock = stubFetch(routes({ 'GET /api/v1/config': () => json(config) }));
    const { result, client } = renderWithClient(() => useConfig());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(config);
    expect(client.getQueryData(queryKeys.config)).toEqual(config);
    expect(onlyRequest(mock).signal).toBeInstanceOf(AbortSignal);

    const second = renderWithClient(() => useConfig(), client);
    await act(() => Promise.resolve());
    expect(second.result.current.data).toEqual(config);
    expect(second.result.current.isStale).toBe(false);
    expect(mock).toHaveBeenCalledTimes(1);
  });

  it('surfaces 500 config_error', async () => {
    stubFetch(() => json(envelope('config_error', 'thresholds.yaml is invalid'), 500));
    const { result } = renderWithClient(() => useConfig());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('config_error');
    expect(result.current.error?.message).toBe('thresholds.yaml is invalid');
  });
});

// ---------------------------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------------------------

/** The headers every JSON mutation carries. */
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

/** Every key a decision write must invalidate. */
const DECISION_STATE_KEYS: readonly QueryKey[] = [
  queryKeys.recommendations('default'),
  queryKeys.recommendations(CUSTOM_RUN_ID),
  queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
  queryKeys.discrepancies(['resolved']),
  queryKeys.decisions({}),
  queryKeys.decisions({ subject_type: 'campaign', subject_id: 301 }),
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
    const me = makeMe();
    const mock = stubFetch(routes({ 'POST /api/v1/auth/login': () => json(me) }));
    const { result, client } = renderWithClient(() => useLogin());
    const credentials = { email: 'admin@example.com', password: 'correct horse' };
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(credentials);
    });

    expect(resolved).toEqual(me);
    expect(client.getQueryData(queryKeys.me)).toEqual(me);
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(credentials));
  });

  it('leaves queryKeys.me empty on invalid_credentials', async () => {
    stubFetch(() => json(envelope('invalid_credentials', 'Invalid email or password.'), 401));
    const { result, client } = renderWithClient(() => useLogin());
    let failure: unknown;
    await act(async () => {
      failure = await rejection(result.current.mutateAsync({ email: 'a@b.c', password: 'x' }));
    });
    expect(isApiError(failure) && failure.code === 'invalid_credentials').toBe(true);
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe('Invalid email or password.');
    expect(client.getQueryData(queryKeys.me)).toBeUndefined();
  });

  it('exposes Retry-After seconds on rate_limited', async () => {
    stubFetch(() =>
      json(envelope('rate_limited', 'Too many login attempts.'), 429, { 'Retry-After': '60' }),
    );
    const { result } = renderWithClient(() => useLogin());
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
    ['200 with the signed-out user', () => json(makeMe())],
    ['204', () => new Response(null, { status: 204 })],
  ])('posts with the CSRF header and clears the whole cache on %s', async (_label, respond) => {
    const mock = stubFetch(routes({ 'POST /api/v1/auth/logout': respond }));
    const { result, client } = renderWithClient(() => useLogout());
    seed(client, SEEDED);
    let resolved: unknown = 'not resolved';
    await act(async () => {
      resolved = await result.current.mutateAsync();
    });

    expect(resolved).toBeUndefined();
    expect(client.getQueryCache().getAll()).toHaveLength(0);
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(BODYLESS_MUTATION_HEADERS);
    expect(request.body).toBeUndefined();
  });

  it('clears the cache when the session had already expired (401 unauthenticated)', async () => {
    stubFetch(() => json(envelope('unauthenticated', 'Sign in again.'), 401));
    const { result, client } = renderWithClient(() => useLogout());
    seed(client, SEEDED);
    let failure: unknown;
    await act(async () => {
      failure = await rejection(result.current.mutateAsync());
    });
    expect(isApiError(failure) && failure.code === 'unauthenticated').toBe(true);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });

  it.each([
    ['a 500', () => json(envelope('internal_error', 'Boom'), 500)],
    [
      'a 401 without the unauthenticated envelope',
      () => new Response('<h1>401</h1>', { status: 401 }),
    ],
    ['an unreachable server', () => Promise.reject(new TypeError('Failed to fetch'))],
  ] as const)('keeps the cache on %s, as the user is still signed in', async (_label, respond) => {
    stubFetch(respond);
    const { result, client } = renderWithClient(() => useLogout());
    seed(client, SEEDED);
    await act(async () => {
      await rejection(result.current.mutateAsync());
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    for (const key of SEEDED) {
      expect(client.getQueryData(key)).toEqual({ seeded: key });
    }
  });
});

describe('useCreateRun', () => {
  const body = { window_start: '2026-07-08', window_end: '2026-10-05' };

  it('posts the window, seeds runs.byId and invalidates only the custom-run lists', async () => {
    const run = makeRun({ id: CUSTOM_RUN_ID, kind: 'custom', trigger: 'manual', days: 90 });
    const mock = stubFetch(routes({ 'POST /api/v1/runs': () => json(run, 201) }));
    const { result, client } = renderWithClient(() => useCreateRun());
    const untouched = [
      queryKeys.runs.latest,
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
    ];
    seed(client, [queryKeys.runs.custom(20), queryKeys.runs.custom(5), ...untouched]);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
    });

    expect(resolved).toEqual(run);
    expect(client.getQueryData(queryKeys.runs.byId(CUSTOM_RUN_ID))).toEqual(run);
    expect(invalidated(client, queryKeys.runs.custom(20))).toBe(true);
    expect(invalidated(client, queryKeys.runs.custom(5))).toBe(true);
    for (const key of untouched) {
      expect(invalidated(client, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('seeds and invalidates nothing on 409 run_in_progress', async () => {
    stubFetch(() => json(envelope('run_in_progress', 'An analysis run is in progress'), 409));
    const { result, client } = renderWithClient(() => useCreateRun());
    seed(client, [queryKeys.runs.custom(20)]);
    await act(async () => {
      await rejection(result.current.mutateAsync(body));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('run_in_progress');
    expect(client.getQueryData(queryKeys.runs.byId(CUSTOM_RUN_ID))).toBeUndefined();
    expect(invalidated(client, queryKeys.runs.custom(20))).toBe(false);
  });
});

describe('useRecordDecision', () => {
  const body: DecisionRequest = {
    subject: { type: 'campaign', id: 301 },
    patch: { status: 'paused' },
    recommendation_id: FIXTURE_ITEM_IDS.c1,
    note: 'Paused after review',
  };

  it('posts the patch and invalidates recommendations, discrepancies and decisions', async () => {
    const decision = makeDecision('own_budget', { origin: 'user', status: 'paused' });
    const mock = stubFetch(routes({ 'POST /api/v1/decisions': () => json(decision, 201) }));
    const { result, client } = renderWithClient(() => useRecordDecision());
    seed(client, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync(body);
    });

    expect(resolved).toEqual(decision);
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(client, key)).toBe(true);
    }
    for (const key of UNRELATED_KEYS) {
      expect(invalidated(client, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('has refetched a shown decision log by the time mutateAsync resolves', async () => {
    const before = [makeDecision('own_budget')];
    const after = [makeDecision('own_budget', { origin: 'user' }), ...before];
    const mock = stubFetch(
      sequence(
        () => json(before),
        () => json(makeDecision('own_budget', { origin: 'user' }), 201),
        () => json(after),
      ),
    );
    const { result } = renderWithClient(() => ({
      log: useDecisions(),
      record: useRecordDecision(),
    }));
    await waitFor(() => expect(result.current.log.isSuccess).toBe(true));
    await act(async () => {
      await result.current.record.mutateAsync(body);
    });
    expect(sent(mock).map((request) => `${request.method ?? ''} ${request.url}`)).toEqual([
      'GET /api/v1/decisions',
      'POST /api/v1/decisions',
      'GET /api/v1/decisions',
    ]);
    await waitFor(() => expect(result.current.log.data).toEqual(after));
  });

  it('invalidates nothing when the patch is rejected', async () => {
    stubFetch(() => json(envelope('no_change', 'The patch changes nothing'), 422));
    const { result, client } = renderWithClient(() => useRecordDecision());
    seed(client, DECISION_STATE_KEYS);
    await act(async () => {
      await rejection(result.current.mutateAsync(body));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('no_change');
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(client, key)).toBe(false);
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
    const updated = makeDiscrepancy('status', { state: 'awaiting_platform' });
    const url = `/api/v1/discrepancies/${String(FIXTURE_DISCREPANCY_ID)}/resolve`;
    const mock = stubFetch(routes({ [`POST ${url}`]: () => json(updated) }));
    const { result, client } = renderWithClient(() => useResolveDiscrepancy());
    seed(client, [...DECISION_STATE_KEYS, ...UNRELATED_KEYS]);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync({ id: FIXTURE_DISCREPANCY_ID, body });
    });

    expect(resolved).toEqual(updated);
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(client, key)).toBe(true);
    }
    for (const key of UNRELATED_KEYS) {
      expect(invalidated(client, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('invalidates nothing on 409 discrepancy_resolved', async () => {
    stubFetch(() => json(envelope('discrepancy_resolved', 'Already resolved'), 409));
    const { result, client } = renderWithClient(() => useResolveDiscrepancy());
    seed(client, DECISION_STATE_KEYS);
    await act(async () => {
      await rejection(result.current.mutateAsync({ id: FIXTURE_DISCREPANCY_ID, body }));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('discrepancy_resolved');
    for (const key of DECISION_STATE_KEYS) {
      expect(invalidated(client, key)).toBe(false);
    }
  });
});

describe('useFeedback', () => {
  it('posts to /recommendations/{id}/feedback, invalidating only recommendations', async () => {
    const feedback = makeFeedback('snooze');
    const mock = stubFetch(
      routes({
        [`POST /api/v1/recommendations/${String(FIXTURE_ITEM_IDS.k1)}/feedback`]: () =>
          json(feedback, 201),
      }),
    );
    const { result, client } = renderWithClient(() => useFeedback());
    const recommendations = [
      queryKeys.recommendations('default'),
      queryKeys.recommendations(CUSTOM_RUN_ID),
    ];
    const others = [
      queryKeys.decisions({}),
      queryKeys.discrepancies(DEFAULT_DISCREPANCY_STATES),
      ...UNRELATED_KEYS,
    ];
    seed(client, [...recommendations, ...others]);
    const body = { kind: 'snooze', note: null } as const;
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync({ recommendationId: FIXTURE_ITEM_IDS.k1, body });
    });

    expect(resolved).toEqual(feedback);
    for (const key of recommendations) {
      expect(invalidated(client, key)).toBe(true);
    }
    for (const key of others) {
      expect(invalidated(client, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(JSON_MUTATION_HEADERS);
    expect(request.body).toBe(JSON.stringify(body));
  });

  it('surfaces 422 feedback_not_allowed for a C0 item and invalidates nothing', async () => {
    stubFetch(() => json(envelope('feedback_not_allowed', 'Resolve the discrepancy instead'), 422));
    const { result, client } = renderWithClient(() => useFeedback());
    seed(client, [queryKeys.recommendations('default')]);
    await act(async () => {
      await rejection(
        result.current.mutateAsync({
          recommendationId: FIXTURE_ITEM_IDS.c0,
          body: { kind: 'dismiss' },
        }),
      );
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('feedback_not_allowed');
    expect(invalidated(client, queryKeys.recommendations('default'))).toBe(false);
  });
});

describe('useRequestSync', () => {
  it('posts to /sources/{source}/sync without a body, invalidating only sources', async () => {
    const view: SyncRequestView = {
      id: 9,
      source: 'linkedin',
      requested_at: '2026-10-06T09:00:00Z',
      picked_at: null,
    };
    const mock = stubFetch(routes({ 'POST /api/v1/sources/linkedin/sync': () => json(view, 202) }));
    const { result, client } = renderWithClient(() => useRequestSync());
    const others = [
      queryKeys.recommendations('default'),
      queryKeys.decisions({}),
      queryKeys.config,
    ];
    seed(client, [queryKeys.sources, ...others]);
    let resolved: unknown;
    await act(async () => {
      resolved = await result.current.mutateAsync('linkedin');
    });

    expect(resolved).toEqual(view);
    expect(invalidated(client, queryKeys.sources)).toBe(true);
    for (const key of others) {
      expect(invalidated(client, key)).toBe(false);
    }
    const request = onlyRequest(mock);
    expect(request.headers).toEqual(BODYLESS_MUTATION_HEADERS);
    expect(request.body).toBeUndefined();
  });

  it('surfaces 409 sync_pending and leaves the sources valid', async () => {
    stubFetch(() => json(envelope('sync_pending', 'A sync is already pending'), 409));
    const { result, client } = renderWithClient(() => useRequestSync());
    seed(client, [queryKeys.sources]);
    await act(async () => {
      await rejection(result.current.mutateAsync('google'));
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe('sync_pending');
    expect(invalidated(client, queryKeys.sources)).toBe(false);
  });
});
