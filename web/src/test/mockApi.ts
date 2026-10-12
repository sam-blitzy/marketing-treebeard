/**
 * The `fetch` stub of the web component tests: it answers {@link API_BASE} requests from
 * handlers a test registers, so the test exercises the real client of `src/api/client.ts`
 * (`apiFetch`, its mapping of the {@link ErrorResponse} envelope to `ApiError`, `Retry-After`,
 * 204 handling) and the real TanStack Query hooks above it, with nothing but the network
 * replaced.
 *
 * - **Stubbing.** {@link mockApi} replaces the global `fetch` through `vi.stubGlobal`, which
 *   `src/api/client.ts` looks up on every call. This is not a setup file: a test imports it.
 *   `client.test.ts` and `queries.test.tsx` deliberately stub `fetch` themselves instead.
 * - **Handlers.** Each handler is keyed `"<METHOD> <path>[?<query>]"`, relative to
 *   {@link API_BASE}, and answers with a {@link MockResponse} built by {@link json},
 *   {@link noContent}, {@link apiError} or {@link html}, or with a function of the
 *   {@link RecordedCall}. A fresh `Response` is built for every request.
 * - **Recording.** Every request, matched or not, is appended to {@link ApiMock.calls} in
 *   request order, with its method, path, query, headers, parsed body and path parameters.
 * - **Loud failures.** A request that no handler matches, or whose handler throws, is rejected,
 *   logged with `console.error`, and fails the running test from an `afterEach` hook this
 *   module registers once, so a missing handler can never pass unnoticed as a loading state.
 *   Import this module before `./render` and `@testing-library/react` (see that hook).
 *
 * Only test files import this module; application code never does. It imports nothing from
 * `src/features`, `src/router.tsx` or `./fixtures`, so any test can use it.
 *
 * @example
 * ```tsx
 * import { apiError, json, mockApi } from '../../test/mockApi';
 *
 * const api = mockApi({
 *   'GET /auth/me': json(200, makeMe()),
 *   'GET /recommendations?run_id=101': json(200, makeRecommendationsPage()),
 *   'POST /recommendations/:id/feedback': json(201, makeFeedback('dismiss')),
 * });
 * // … add handlers for the page's other reads, render it and dismiss card 1001 …
 * expect(api.calls.find((call) => call.method === 'POST')?.params).toEqual({ id: '1001' });
 * api.use({ 'POST /runs': apiError(409, 'run_in_progress', 'An analysis is already running.') });
 * ```
 *
 * @packageDocumentation
 */
import { afterEach, vi } from 'vitest';

import { API_BASE } from '../api/client';
import type { ErrorResponse } from '../api/types';

// ---------------------------------------------------------------------------------------------
// Response descriptors
// ---------------------------------------------------------------------------------------------

/**
 * What a handler answers with: a plain descriptor, not a `Response`, so one descriptor can
 * answer any number of requests. Each request gets a fresh `Response` built from it, whose body
 * is `null` for a 204, 205 or 304 status, as the Fetch standard requires.
 */
export interface MockResponse {
  /** The HTTP status, 200–599. */
  status: number;
  /** The response body text, or `null` for no body. */
  body: string | null;
  /** The response headers, for example `{ 'Retry-After': '30' }`. */
  headers: Record<string, string>;
}

/** Statuses in the 200–599 range whose `Response` must have a `null` body (Fetch standard). */
const NULL_BODY_STATUSES: ReadonlySet<number> = new Set([204, 205, 304]);

/**
 * Merges header records case-insensitively: a later name replaces an earlier one that differs
 * only in case, so `{ 'content-type': … }` overrides the default `Content-Type` instead of being
 * joined to it.
 */
function mergeHeaders(
  ...records: readonly Readonly<Record<string, string>>[]
): Record<string, string> {
  const byName = new Map<string, readonly [string, string]>();
  for (const record of records) {
    for (const [name, value] of Object.entries(record)) {
      byName.set(name.toLowerCase(), [name, value]);
    }
  }
  return Object.fromEntries(byName.values());
}

/**
 * A JSON response: `body` serialised with `JSON.stringify` and sent with
 * `Content-Type: application/json`, plus any `headers` given.
 *
 * @param status - The HTTP status.
 * @param body - The payload, typically a `types.ts` value built from `./fixtures`.
 * @param headers - Extra headers, for example a paged list's `Link`; a `Content-Type` here
 *   replaces the default.
 * @returns The descriptor.
 * @throws TypeError when `body` has no JSON text (`undefined`, a function or a symbol); use
 *   {@link noContent} for a response without a body.
 *
 * @example
 * ```ts
 * json(200, makeRun({ id: 202, kind: 'custom' }));
 * json(200, [makeDiscrepancy('status')], {
 *   Link: '</api/v1/discrepancies?state=open&limit=500&after=812>; rel="next"',
 * });
 * ```
 */
export function json(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): MockResponse {
  const text = JSON.stringify(body) as string | undefined;
  if (text === undefined) {
    throw new TypeError(
      'mockApi json(): the body has no JSON representation; use noContent() for a response without a body',
    );
  }
  return {
    status,
    body: text,
    headers: mergeHeaders({ 'Content-Type': 'application/json' }, headers),
  };
}

/**
 * A `204 No Content` response without a body, which `apiFetch` resolves as `undefined`.
 *
 * @returns The descriptor.
 */
export function noContent(): MockResponse {
  return { status: 204, body: null, headers: {} };
}

/**
 * An error response in the API's envelope, `{"error": {"code", "message"}}`, which `apiFetch`
 * rejects as an `ApiError` carrying `status`, `code` and `message`.
 *
 * @param status - The HTTP status, for example 409 or 422.
 * @param code - The machine code, for example `run_in_progress` or `subject_retired`.
 * @param message - The text the UI shows.
 * @param headers - Extra headers; a `Retry-After` becomes `ApiError.retryAfter`.
 * @returns The descriptor.
 *
 * @example
 * ```ts
 * apiError(429, 'rate_limited', 'Too many attempts', { 'Retry-After': '30' });
 * ```
 */
export function apiError(
  status: number,
  code: string,
  message: string,
  headers: Record<string, string> = {},
): MockResponse {
  return json(status, { error: { code, message } } satisfies ErrorResponse, headers);
}

/**
 * An HTML response, `Content-Type: text/html`, such as a 502 page from a proxy in front of
 * nginx (whose own errors under `/api/` are JSON envelopes). `apiFetch` rejects an
 * error status without the API's envelope as `http_<status>`, and a 2xx with an unparsable body
 * as `invalid_response`.
 *
 * @param status - The HTTP status.
 * @param text - The HTML text.
 * @returns The descriptor.
 */
export function html(status: number, text: string): MockResponse {
  return { status, body: text, headers: { 'Content-Type': 'text/html' } };
}

// ---------------------------------------------------------------------------------------------
// Calls, handlers and the controller
// ---------------------------------------------------------------------------------------------

/** One request the stub received, matched or not, as {@link ApiMock.calls} records it. */
export interface RecordedCall {
  /** The method, upper-cased; `GET` when neither the `Request` nor `init` names one. */
  method: string;
  /**
   * The path relative to {@link API_BASE}, without the query, for example
   * `/recommendations/1101/feedback`. A request outside {@link API_BASE} keeps its full path.
   */
  path: string;
  /** The query parameters, for example `search.getAll('state')`. */
  search: URLSearchParams;
  /** The request headers, for example `headers.get('X-Requested-With')`. */
  headers: Headers;
  /**
   * The body: parsed with `JSON.parse` when it is JSON, otherwise its raw text; `undefined`
   * when the request has no body.
   */
  body: unknown;
  /**
   * The `:param` captures of the matching handler key, URI-decoded, for example `{ id: '1101' }`
   * for `POST /recommendations/:id/feedback`. Empty when no handler matched.
   */
  params: Record<string, string>;
  /** The absolute URL the request resolved to, query included. */
  url: string;
}

/**
 * The answer to the requests one key matches: a fixed {@link MockResponse}, or a function of
 * the {@link RecordedCall} that returns one, synchronously or not. A function that throws or
 * rejects fails the test like an unmatched request.
 */
export type MockHandler =
  | MockResponse
  | ((call: RecordedCall) => MockResponse | Promise<MockResponse>);

/**
 * Handlers by key: `"<METHOD> <path>[?<query>]"`, relative to {@link API_BASE}.
 *
 * - The method is upper-case, and one space separates it from the path, which starts with `/`.
 *   A `:name` segment matches any one non-empty segment and is captured in
 *   {@link RecordedCall.params}.
 * - Without `?`, the request's query is ignored. With one, every parameter listed must be
 *   present with the same values in the same order (`?state=open&state=awaiting_platform`);
 *   the request may carry other parameters as well.
 * - When several keys match, the one with more literal path segments wins, then the one with
 *   more query parameters, then the one registered last.
 * - A malformed key throws when it is registered.
 *
 * @example
 * ```ts
 * const handlers: MockHandlers = {
 *   'GET /auth/me': json(200, makeMe()),
 *   'GET /recommendations': json(200, makeRecommendationsPage()),
 *   'GET /recommendations?run_id=202': json(200, makeRecommendationsPage({ run: customRun })),
 *   'POST /discrepancies/:id/resolve': (call) =>
 *     json(200, makeDiscrepancy('status', { id: Number(call.params.id), state: 'resolved' })),
 *   'POST /sources/:source/sync': (call) => json(202, { id: 9, source: call.params.source }),
 * };
 * ```
 */
export type MockHandlers = Record<string, MockHandler>;

/** What {@link mockApi} returns: the request log and a way to change handlers mid-test. */
export interface ApiMock {
  /**
   * Every request the stub received since this {@link mockApi} call, in request order,
   * unmatched ones included. The array is live: it grows as requests arrive.
   */
  readonly calls: RecordedCall[];
  /**
   * Adds handlers, or replaces those with the same key, from the next request on; a replacing
   * handler counts as registered last.
   *
   * @param handlers - The handlers to add or replace.
   * @throws Error when a key is malformed; no handler of the call is then applied.
   */
  use(handlers: MockHandlers): void;
}

/** One path segment of a handler key. */
type Segment =
  | { readonly kind: 'literal'; readonly value: string }
  | { readonly kind: 'param'; readonly name: string };

/** A registered handler with its parsed key. */
interface Route {
  /** The key as written, shown in failure messages. */
  readonly key: string;
  /** The key in canonical form, so equivalent keys replace each other. */
  readonly id: string;
  /** The upper-case method. */
  readonly method: string;
  /** The path segments, after the leading `/`. */
  readonly segments: readonly Segment[];
  /** Each query parameter the key lists, with its values in order. */
  readonly query: readonly (readonly [string, readonly string[]])[];
  /** How many path segments are literal. */
  readonly literals: number;
  /** Registration order within its {@link ApiMock}; higher is later. */
  readonly order: number;
  /** The answer. */
  readonly handler: MockHandler;
}

/** The parse of a handler key, before it is given an order and a handler. */
type ParsedKey = Omit<Route, 'order' | 'handler'>;

/** The state behind one {@link ApiMock}. */
interface Controller {
  /** The live request log. */
  readonly calls: RecordedCall[];
  /** The registered handlers, by canonical key. */
  readonly routes: Map<string, Route>;
  /** The next registration order. */
  nextOrder: number;
}

/** The signature `vi.stubGlobal('fetch', …)` installs. */
type FetchImpl = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

// ---------------------------------------------------------------------------------------------
// Module state and the afterEach check
// ---------------------------------------------------------------------------------------------

/** The controller of the latest {@link mockApi} call in the running test. */
let active: Controller | undefined;

/** Descriptions of the running test's failed requests, drained after each test. */
const failures: string[] = [];

/**
 * After every test of a file that imports this module: restores every stubbed global, forgets
 * the active mock and fails the test when any request went unmatched or its handler threw.
 *
 * `vi.unstubAllGlobals()` restores every global stubbed with `vi.stubGlobal`, not only `fetch`.
 * Vitest skips the remaining `afterEach` hooks once one throws, so import this module before
 * `./render` and `@testing-library/react`: hooks run in reverse order of registration, and this
 * one then runs after Testing Library's automatic cleanup.
 */
afterEach(() => {
  const failed = failures.splice(0);
  vi.unstubAllGlobals();
  active = undefined;
  if (failed.length > 0) {
    throw new Error(`mockApi: unmatched requests: ${failed.join('; ')}`);
  }
});

/** Records a failed request for the afterEach check, logs it and returns the rejection error. */
function fail(description: string, message: string, cause?: unknown): Error {
  failures.push(description);
  console.error(message);
  return cause === undefined ? new Error(message) : new Error(message, { cause });
}

// ---------------------------------------------------------------------------------------------
// Handler keys
// ---------------------------------------------------------------------------------------------

/** `"<METHOD> <path>[?<query>]"`: an upper-case method, one space, a path with no whitespace. */
const KEY_PATTERN = /^([A-Z]+) (\/[^\s?#]*)(?:\?([^\s#]*))?$/u;

/** A `:name` path segment. */
const PARAM_SEGMENT = /^:([A-Za-z_][A-Za-z0-9_]*)$/u;

/** Origin used only to normalise a key's path the way a request URL's path is normalised. */
const KEY_ORIGIN = 'http://mock-api.invalid';

/**
 * Parses a handler key, throwing an error that names the key when it is malformed.
 *
 * The path is normalised as a request URL's path is (dot segments resolved, characters
 * percent-encoded), so a literal segment compares equal to the request's.
 */
function parseKey(key: string): ParsedKey {
  const malformed = (reason: string): Error =>
    new Error(
      `mockApi: malformed handler key "${key}": ${reason}; expected "<METHOD> <path>[?<query>]" relative to ${API_BASE}, for example "GET /auth/me" or "POST /recommendations/:id/feedback"`,
    );
  const match = KEY_PATTERN.exec(key);
  const method = match?.[1];
  const rawPath = match?.[2];
  const rawQuery = match?.[3];
  if (method === undefined || rawPath === undefined) {
    throw malformed('it is not an upper-case method, one space and a path starting with "/"');
  }
  if (rawPath === API_BASE || rawPath.startsWith(`${API_BASE}/`)) {
    throw malformed(`the path must be written relative to ${API_BASE}, without it`);
  }

  const path = new URL(rawPath, KEY_ORIGIN).pathname;
  const segments: Segment[] = [];
  const names = new Set<string>();
  for (const segment of path.split('/').slice(1)) {
    if (!segment.startsWith(':')) {
      segments.push({ kind: 'literal', value: segment });
      continue;
    }
    const name = PARAM_SEGMENT.exec(segment)?.[1];
    if (name === undefined) {
      throw malformed(`the segment "${segment}" is not a ":name" parameter`);
    }
    if (names.has(name)) {
      throw malformed(`the parameter ":${name}" appears twice`);
    }
    names.add(name);
    segments.push({ kind: 'param', name });
  }

  const query: (readonly [string, readonly string[]])[] = [];
  if (rawQuery !== undefined) {
    const params = new URLSearchParams(rawQuery);
    for (const name of new Set(params.keys())) {
      if (name === '') {
        throw malformed('a query parameter has no name');
      }
      query.push([name, params.getAll(name)]);
    }
    if (query.length === 0) {
      throw malformed('the "?" is followed by no query parameter');
    }
  }

  // Canonical form: parameters sorted by name, each name's values kept in order, so
  // "?b=2&a=1" and "?a=1&b=2" name the same handler and replace each other.
  const canonicalQuery = new URLSearchParams(
    [...query]
      .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
      .flatMap(([name, values]) => values.map((value): [string, string] => [name, value])),
  ).toString();
  return {
    key,
    id: `${method} ${path}${canonicalQuery === '' ? '' : `?${canonicalQuery}`}`,
    method,
    segments,
    query,
    literals: segments.filter((segment) => segment.kind === 'literal').length,
  };
}

/**
 * Adds the handlers to the controller, replacing those with the same canonical key. Every key
 * is parsed before any is applied, so a malformed key leaves the controller unchanged.
 */
function register(controller: Controller, handlers: MockHandlers): void {
  const parsed = Object.entries(handlers).map(
    ([key, handler]) => [parseKey(key), handler] as const,
  );
  for (const [key, handler] of parsed) {
    controller.routes.delete(key.id);
    controller.routes.set(key.id, { ...key, order: controller.nextOrder, handler });
    controller.nextOrder += 1;
  }
}

/** The registered keys as written, for failure messages. */
function registeredKeys(controller: Controller): string {
  const keys = [...controller.routes.values()].map((route) => route.key);
  return keys.length === 0 ? '(none)' : keys.join(', ');
}

// ---------------------------------------------------------------------------------------------
// Matching
// ---------------------------------------------------------------------------------------------

/** A path segment, URI-decoded, or unchanged when it is not valid percent-encoding. */
function decodeSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/**
 * The `:param` captures when `route` matches the request, otherwise `undefined`.
 *
 * @param route - The registered handler.
 * @param method - The request's upper-case method.
 * @param segments - The request's path segments relative to {@link API_BASE}, still encoded.
 * @param search - The request's query.
 */
function matchRoute(
  route: Route,
  method: string,
  segments: readonly string[],
  search: URLSearchParams,
): Record<string, string> | undefined {
  if (route.method !== method || route.segments.length !== segments.length) {
    return undefined;
  }
  const params: Record<string, string> = {};
  for (const [index, pattern] of route.segments.entries()) {
    const actual = segments[index];
    if (actual === undefined) {
      return undefined;
    }
    if (pattern.kind === 'literal' ? pattern.value !== actual : actual === '') {
      return undefined;
    }
    if (pattern.kind === 'param') {
      params[pattern.name] = decodeSegment(actual);
    }
  }
  for (const [name, values] of route.query) {
    const actual = search.getAll(name);
    const differs =
      actual.length !== values.length || actual.some((value, index) => value !== values[index]);
    if (differs) {
      return undefined;
    }
  }
  return params;
}

/** Whether `candidate` is more specific than `current`, or as specific and registered later. */
function outranks(candidate: Route, current: Route): boolean {
  if (candidate.literals !== current.literals) {
    return candidate.literals > current.literals;
  }
  if (candidate.query.length !== current.query.length) {
    return candidate.query.length > current.query.length;
  }
  return candidate.order > current.order;
}

/** The most specific route matching the request, with its captures, or `undefined`. */
function findRoute(
  controller: Controller,
  call: RecordedCall,
): { route: Route; params: Record<string, string> } | undefined {
  const segments = call.path.split('/').slice(1);
  let best: { route: Route; params: Record<string, string> } | undefined;
  for (const route of controller.routes.values()) {
    const params = matchRoute(route, call.method, segments, call.search);
    if (params !== undefined && (best === undefined || outranks(route, best.route))) {
      best = { route, params };
    }
  }
  return best;
}

// ---------------------------------------------------------------------------------------------
// Requests, responses and the stub
// ---------------------------------------------------------------------------------------------

/** The rejection of an aborted request, as `fetch` rejects it. */
function abortError(): DOMException {
  return new DOMException('The operation was aborted.', 'AbortError');
}

/** The message of a thrown value. */
function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Reads the request body as text: `init.body` when given, otherwise the `Request`'s own body
 * (from a clone, so the caller's `Request` stays unread); `undefined` when there is none.
 */
async function readBody(
  request: Request | undefined,
  init: RequestInit | undefined,
): Promise<string | undefined> {
  const body = init?.body;
  if (body !== undefined && body !== null) {
    return typeof body === 'string' ? body : new Response(body).text();
  }
  if (request !== undefined && request.body !== null) {
    return request.clone().text();
  }
  return undefined;
}

/** A body's JSON value when it parses, its raw text when it does not, `undefined` without one. */
function parseBody(text: string | undefined): unknown {
  if (text === undefined) {
    return undefined;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

/** A fresh `Response` for one request; a null-body status never carries the descriptor's body. */
function toResponse({ status, body, headers }: MockResponse): Response {
  return new Response(NULL_BODY_STATUSES.has(status) ? null : body, { status, headers });
}

/**
 * Answers one recorded request: parses its body into the call, finds the most specific
 * handler and builds its response. An unmatched request, a handler that throws or returns an
 * invalid descriptor, and an unreadable body are all recorded as failures and rejected.
 */
async function respond(
  controller: Controller | undefined,
  call: RecordedCall,
  inside: boolean,
  description: string,
  bodyText: Promise<string | undefined>,
): Promise<Response> {
  try {
    call.body = parseBody(await bodyText);
  } catch (cause) {
    throw fail(
      `${description} (unreadable body: ${errorText(cause)})`,
      `mockApi: could not read the body of ${description}: ${errorText(cause)}`,
      cause,
    );
  }

  const found = controller !== undefined && inside ? findRoute(controller, call) : undefined;
  if (found === undefined) {
    const registered =
      controller === undefined ? '(none: mockApi was not called)' : registeredKeys(controller);
    throw fail(description, `mockApi: no handler for ${description}; registered: ${registered}`);
  }

  call.params = found.params;
  const { route } = found;
  try {
    const descriptor =
      typeof route.handler === 'function' ? await route.handler(call) : route.handler;
    return toResponse(descriptor);
  } catch (cause) {
    throw fail(
      `${description} (handler "${route.key}" threw: ${errorText(cause)})`,
      `mockApi: the handler "${route.key}" threw on ${description}: ${errorText(cause)}`,
      cause,
    );
  }
}

/** Rejects with an `AbortError` as soon as `signal` aborts, otherwise settles as `response`. */
function abortable(response: Promise<Response>, signal: AbortSignal): Promise<Response> {
  return new Promise<Response>((resolve, reject) => {
    const onAbort = (): void => {
      reject(abortError());
    };
    signal.addEventListener('abort', onAbort, { once: true });
    void response.then(resolve, reject).finally(() => {
      signal.removeEventListener('abort', onAbort);
    });
  });
}

/**
 * The `fetch` stub. Everything a request is recorded with is read synchronously, so
 * {@link ApiMock.calls} keeps request order even when a body has to be read asynchronously.
 */
const stub: FetchImpl = (input, init) => {
  const request = input instanceof Request ? input : undefined;
  const signal = init?.signal ?? request?.signal ?? null;
  if (signal?.aborted === true) {
    return Promise.reject(abortError());
  }

  const controller = active;
  const method = (init?.method ?? request?.method ?? 'GET').toUpperCase();
  const base = globalThis.location?.href ?? 'http://localhost/';
  let url: URL;
  let origin: string;
  try {
    url = new URL(String(request === undefined ? input : request.url), base);
    origin = new URL(base).origin;
  } catch (cause) {
    const description = `${method} ${String(request === undefined ? input : request.url)}`;
    return Promise.reject(
      fail(
        `${description} (invalid URL)`,
        `mockApi: no handler for ${description}: it is not a valid URL`,
        cause,
      ),
    );
  }

  const inside =
    url.origin === origin &&
    (url.pathname === API_BASE || url.pathname.startsWith(`${API_BASE}/`));
  const relative = url.pathname.slice(API_BASE.length);
  const path = inside ? (relative === '' ? '/' : relative) : url.pathname;

  const headers = new Headers(request?.headers);
  if (init?.headers !== undefined) {
    new Headers(init.headers).forEach((value, name) => {
      headers.set(name, value);
    });
  }

  const call: RecordedCall = {
    method,
    path,
    search: new URLSearchParams(url.search),
    headers,
    body: undefined,
    params: {},
    url: url.href,
  };
  controller?.calls.push(call);

  const description = inside
    ? `${method} ${path}${url.search}`
    : `${method} ${url.href} (outside ${API_BASE})`;
  const response = respond(controller, call, inside, description, readBody(request, init));
  return signal === null ? response : abortable(response, signal);
};

// ---------------------------------------------------------------------------------------------
// mockApi
// ---------------------------------------------------------------------------------------------

/**
 * Stubs the global `fetch` with handlers for `/api/v1` and returns the controller.
 *
 * Calling it again in the same test replaces the stub: the new handlers alone answer from then
 * on, and the new controller's {@link ApiMock.calls} starts empty. After the test, the stub is
 * removed and the test fails if any request was unmatched or its handler threw (see the
 * `afterEach` hook above, which also restores every other global stubbed with `vi.stubGlobal`).
 *
 * A request outside {@link API_BASE} is unmatched. A request whose signal has already aborted
 * rejects with an `AbortError` `DOMException`, without consulting any handler or being
 * recorded, and one aborted while its handler is pending rejects the same way.
 *
 * @param handlers - The handlers, keyed as {@link MockHandlers} describes.
 * @returns The controller: the request log and {@link ApiMock.use}.
 * @throws Error when a key is malformed; nothing is stubbed then.
 *
 * @example
 * ```tsx
 * const api = mockApi({
 *   'GET /config': json(200, makeConfig()),
 *   'GET /runs/latest': json(200, makeRun()),
 *   'POST /runs': json(201, makeRun({ id: 202, kind: 'custom' })),
 * });
 * const { router } = await renderWithProviders(<WindowPicker run={makeRun()} />);
 * // … choose a custom window and submit …
 * await waitFor(() => expect(router.state.location.search).toEqual({ run: 202 }));
 * const post = api.calls.find((call) => call.method === 'POST' && call.path === '/runs');
 * expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');
 * expect(post?.body).toEqual({ window_start: '2026-08-01', window_end: '2026-08-30' });
 * ```
 */
export function mockApi(handlers: MockHandlers): ApiMock {
  const controller: Controller = { calls: [], routes: new Map(), nextOrder: 0 };
  register(controller, handlers);
  active = controller;
  vi.stubGlobal('fetch', stub);
  return {
    calls: controller.calls,
    use: (more: MockHandlers): void => {
      register(controller, more);
    },
  };
}
