/**
 * Unit tests for src/api/client.ts, the SPA's one `fetch` wrapper for the `/api/v1` contract
 * (AAP 0.5.3, 0.3.2).
 *
 * The network is replaced per test with `vi.stubGlobal('fetch', …)`: the client looks the
 * global up on every call, so each stub sees exactly the URL and `RequestInit` the client
 * built, and answers with a real `Response`. The cases cover:
 *
 * - request construction: the `/api/v1` base, query encoding (skipped, boolean, numeric and
 *   repeated values, insertion order), `credentials: 'same-origin'`, `Accept`, the JSON body
 *   with its `Content-Type`, the `X-Requested-With: ads-engine` CSRF header on every mutating
 *   method and never on `GET`, and the signal;
 * - bodies that are never sent: a body on `GET` and bodies `JSON.stringify` cannot encode;
 * - response parsing: JSON, 204/205 and empty bodies, unreadable and non-JSON success bodies;
 * - error mapping: the API's `{"error": {"code", "message"}}` envelope, `http_<status>` for
 *   anything without one, the 502/503/504 message, `Retry-After` in seconds or as an HTTP-date,
 *   `network_error` for a rejected `fetch`, and aborts passed through unchanged;
 * - `apiFetchPage`: RFC 8288 `Link` parsing, the `rel="next"` target relative to `/api/v1`,
 *   and every malformed or off-base link rejected as `invalid_response`; the `X-Total-Count`
 *   header read into `total`, `undefined` without it, and every malformed count rejected as
 *   `invalid_response`.
 *
 * The file is self-contained: it builds its own responses and imports no shared test helper.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';

import {
  API_BASE,
  ApiError,
  CSRF_HEADER,
  CSRF_VALUE,
  apiFetch,
  apiFetchPage,
  isApiError,
} from './client';
import type { HttpMethod } from './client';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * The signature of the global the client calls, written out rather than derived from the
 * global itself: only `client.ts` may name that global (`eslint.config.js`, block
 * `web/api-client`), so the tests never reference it, even in a type position.
 */
type FetchImpl = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** The stub installed with `vi.stubGlobal('fetch', …)`. */
type FetchMock = Mock<FetchImpl>;

/** One request as the stub received it. */
interface SentRequest {
  /** The URL the client passed to `fetch`. */
  url: string;
  /** The `RequestInit` the client passed to `fetch`. */
  init: RequestInit;
  /**
   * `init.headers` normalised through `new Headers(…)`, so the assertions hold whether the
   * client builds a record, an entry list or a `Headers` object, and names compare
   * case-insensitively as HTTP defines them.
   */
  headers: Headers;
}

/** The `Content-Type` of the API's JSON responses. */
const JSON_CONTENT_TYPE = { 'Content-Type': 'application/json' } as const;

/** Messages the client documents for failures without the API's envelope. */
const MESSAGES = {
  network: 'The server could not be reached. Check your connection and try again.',
  unreadable: 'The server returned an unreadable response.',
  invalidLink: 'The server returned an invalid link to the next page.',
  invalidTotal: 'The server returned an invalid total count.',
  invalidRequest: 'The request could not be sent: its body cannot be encoded.',
  unavailable: 'The server is temporarily unavailable. Try again shortly.',
} as const;

/**
 * Replaces the global `fetch` with a mock that answers every call through `handler`.
 *
 * @param handler - Builds the response for the URL and init the client sent.
 * @returns The mock, for asserting on its calls.
 */
function stubFetch(
  handler: (url: string, init: RequestInit) => Response | Promise<Response>,
): FetchMock {
  const mock = vi.fn<FetchImpl>(async (input, init) => handler(String(input), init ?? {}));
  vi.stubGlobal('fetch', mock);
  return mock;
}

/**
 * Replaces the global `fetch` with a mock that rejects every call with `failure`.
 *
 * @param failure - What the rejected `fetch` throws.
 * @returns The mock.
 */
function stubFetchRejecting(failure: unknown): FetchMock {
  const mock = vi.fn<FetchImpl>(() => Promise.reject(failure));
  vi.stubGlobal('fetch', mock);
  return mock;
}

/**
 * The request of the `index`-th `fetch` call.
 *
 * @param mock - The stubbed `fetch`.
 * @param index - 0 for the first call.
 * @returns The URL, the init and its headers.
 */
function sentRequest(mock: FetchMock, index = 0): SentRequest {
  const call = mock.mock.calls[index];
  if (call === undefined) {
    const calls = String(mock.mock.calls.length);
    throw new Error(`fetch was called ${calls} time(s), not ${String(index + 1)}`);
  }
  const [input, init] = call;
  if (typeof input !== 'string' || init === undefined) {
    throw new Error('the client must call fetch(url: string, init: RequestInit)');
  }
  return { url: input, init, headers: new Headers(init.headers) };
}

/**
 * Every header of a request as a plain object, for an exact `toEqual`. `Headers` lowercases
 * names, so the keys are lowercase.
 *
 * @param headers - The normalised request headers.
 * @returns Header names mapped to their values.
 */
function headerRecord(headers: Headers): Record<string, string> {
  return Object.fromEntries(headers.entries());
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
    headers: { ...JSON_CONTENT_TYPE, ...headers },
  });
}

/**
 * A plain-text response, for bodies that are not the API's JSON.
 *
 * @param body - The raw body.
 * @param status - The HTTP status.
 * @param headers - Extra response headers.
 * @returns A fresh `Response`.
 */
function text(body: string, status: number, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { 'Content-Type': 'text/html', ...headers } });
}

/**
 * A response whose body read rejects with `failure`, as when the connection drops mid-body.
 *
 * @param status - The HTTP status.
 * @param failure - What `response.text()` rejects with.
 * @returns A fresh `Response`.
 */
function unreadable(status: number, failure: unknown): Response {
  const response = new Response(null, { status });
  vi.spyOn(response, 'text').mockRejectedValue(failure);
  return response;
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
 * The {@link ApiError} `promise` rejects with.
 *
 * @param promise - A promise expected to reject with an `ApiError`.
 * @returns The error.
 * @throws Error when it resolves or rejects with anything else.
 */
async function apiErrorOf(promise: Promise<unknown>): Promise<ApiError> {
  const error = await rejection(promise);
  if (!(error instanceof ApiError)) {
    throw new Error(`expected an ApiError, got ${String(error)}`);
  }
  return error;
}

/**
 * The fields of an {@link ApiError} a caller reads, as a plain object for `toEqual`.
 *
 * @param error - The error.
 * @returns Its name, status, code, message and `retryAfter`.
 */
function fields(error: ApiError): {
  name: string;
  status: number;
  code: string;
  message: string;
  retryAfter: number | undefined;
} {
  return {
    name: error.name,
    status: error.status,
    code: error.code,
    message: error.message,
    retryAfter: error.retryAfter,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------------------------
// Constants and ApiError
// ---------------------------------------------------------------------------------------------

describe('constants', () => {
  it('names the /api/v1 base and the CSRF header the API requires', () => {
    expect(API_BASE).toBe('/api/v1');
    expect(CSRF_HEADER).toBe('X-Requested-With');
    expect(CSRF_VALUE).toBe('ads-engine');
  });
});

describe('ApiError', () => {
  it('carries status, code, message, retryAfter and cause', () => {
    const cause = new TypeError('Failed to fetch');
    const error = new ApiError(429, 'rate_limited', 'Too many attempts.', {
      retryAfter: 60,
      cause,
    });
    expect(error).toBeInstanceOf(Error);
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 429,
      code: 'rate_limited',
      message: 'Too many attempts.',
      retryAfter: 60,
    });
    expect(error.cause).toBe(cause);
  });

  it('leaves retryAfter undefined and sets no cause when they are not given', () => {
    const error = new ApiError(404, 'not_found', 'No analysis run yet');
    expect(error.retryAfter).toBeUndefined();
    expect('cause' in error).toBe(false);
  });

  it('keeps a retryAfter of 0', () => {
    expect(new ApiError(503, 'http_503', 'x', { retryAfter: 0 }).retryAfter).toBe(0);
  });
});

describe('isApiError', () => {
  it('accepts ApiError instances only', () => {
    expect(isApiError(new ApiError(0, 'network_error', 'x'))).toBe(true);
    expect(isApiError(new Error('x'))).toBe(false);
    expect(isApiError({ name: 'ApiError', status: 404, code: 'not_found', message: 'x' })).toBe(
      false,
    );
    expect(isApiError({ status: 401, code: 'unauthenticated' })).toBe(false);
    expect(isApiError(null)).toBe(false);
    expect(isApiError(undefined)).toBe(false);
    expect(isApiError('not_found')).toBe(false);
  });
});

// ---------------------------------------------------------------------------------------------
// Request construction
// ---------------------------------------------------------------------------------------------

describe('apiFetch request construction', () => {
  it('sends a GET to /api/v1 with the session cookie, Accept and nothing else', async () => {
    const mock = stubFetch(() => json({ email: 'admin@example.com' }));
    await apiFetch('/auth/me');

    expect(mock).toHaveBeenCalledTimes(1);
    const { url, init, headers } = sentRequest(mock);
    expect(url).toBe('/api/v1/auth/me');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('same-origin');
    expect(headerRecord(headers)).toEqual({ accept: 'application/json' });
    expect(headers.has(CSRF_HEADER)).toBe(false);
    expect('body' in init).toBe(false);
    expect('signal' in init).toBe(false);
  });

  it('sends GET /runs/latest without the CSRF header, a body or a Content-Type', async () => {
    const mock = stubFetch(() => json({ id: 101, kind: 'default' }));
    await apiFetch('/runs/latest');

    const call = mock.mock.calls[0];
    expect(call).toBeDefined();
    const [input, init] = call ?? [];
    const headers = new Headers(init?.headers);
    expect(input).toBe('/api/v1/runs/latest');
    expect(init?.method).toBe('GET');
    expect(init?.credentials).toBe('same-origin');
    expect(headers.get('Accept')).toBe('application/json');
    expect(headers.has(CSRF_HEADER)).toBe(false);
    expect(headers.has('Content-Type')).toBe(false);
    expect(init?.body).toBeUndefined();
  });

  it('sends POST /auth/login as JSON with the CSRF header', async () => {
    const mock = stubFetch(() => json({ email: 'admin@example.com' }));
    const credentials = { email: 'admin@example.com', password: 'not-a-real-password' };
    await expect(
      apiFetch('/auth/login', { method: 'POST', body: credentials }),
    ).resolves.toEqual({ email: 'admin@example.com' });

    const { url, init, headers } = sentRequest(mock);
    expect(url).toBe('/api/v1/auth/login');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect(headers.get(CSRF_HEADER)).toBe(CSRF_VALUE);
    expect(headers.get('X-Requested-With')).toBe('ads-engine');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('Accept')).toBe('application/json');
    expect(init.body).toBe(JSON.stringify(credentials));
  });

  it('builds a repeated, skipped and typed query as URLSearchParams reads it', async () => {
    const mock = stubFetch(() => json([]));
    await apiFetch('/discrepancies', {
      query: {
        state: ['open', 'awaiting_platform'],
        run_id: 7,
        flag: true,
        skipped: undefined,
        empty: null,
      },
    });

    const { url } = sentRequest(mock);
    const [path, search] = url.split('?');
    expect(path).toBe('/api/v1/discrepancies');
    expect(search).toBe('state=open&state=awaiting_platform&run_id=7&flag=true');
    const params = new URLSearchParams(search);
    expect(params.getAll('state')).toEqual(['open', 'awaiting_platform']);
    expect(params.get('run_id')).toBe('7');
    expect(params.get('flag')).toBe('true');
    expect(params.has('skipped')).toBe(false);
    expect(params.has('empty')).toBe(false);
  });

  it('adds the missing leading slash to a path', async () => {
    const mock = stubFetch(() => json({}));
    await apiFetch('runs/latest');
    expect(sentRequest(mock).url).toBe('/api/v1/runs/latest');
  });

  it('encodes query values in insertion order, skipping null and undefined', async () => {
    const mock = stubFetch(() => json([]));
    await apiFetch('/discrepancies', {
      query: {
        state: ['open', 'awaiting_platform'],
        skipped: undefined,
        limit: 500,
        none: null,
        yes: true,
        no: false,
        ids: [7, 8],
        empty: [],
        text: 'a b&c=d',
      },
    });
    expect(sentRequest(mock).url).toBe(
      '/api/v1/discrepancies?state=open&state=awaiting_platform&limit=500' +
        '&yes=true&no=false&ids=7&ids=8&text=a+b%26c%3Dd',
    );
  });

  it.each([
    ['no query', undefined],
    ['an empty query', {}],
    ['a query whose every value is null or undefined', { run_id: undefined, kind: null }],
  ] as const)('adds no "?" for %s', async (_label, query) => {
    const mock = stubFetch(() => json({}));
    await apiFetch('/recommendations', { query });
    expect(sentRequest(mock).url).toBe('/api/v1/recommendations');
  });

  it('appends the query with "&" to a path that already has one', async () => {
    const mock = stubFetch(() => json([]));
    await apiFetch('/runs?kind=custom', { query: { limit: 5 } });
    expect(sentRequest(mock).url).toBe('/api/v1/runs?kind=custom&limit=5');
  });

  it('sends a POST body as JSON with Content-Type and the CSRF header', async () => {
    const mock = stubFetch(() => json({ id: 1 }, 201));
    const body = { subject: { type: 'campaign', id: 7 }, patch: { status: 'paused' } };
    await apiFetch('/decisions', { method: 'POST', body });

    const { url, init, headers } = sentRequest(mock);
    expect(url).toBe('/api/v1/decisions');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect(init.body).toBe(JSON.stringify(body));
    expect(headerRecord(headers)).toEqual({
      accept: 'application/json',
      'content-type': 'application/json',
      'x-requested-with': 'ads-engine',
    });
    expect(headers.get(CSRF_HEADER)).toBe(CSRF_VALUE);
  });

  it('sends the CSRF header on a POST without a body, and no Content-Type', async () => {
    const mock = stubFetch(() => new Response(null, { status: 204 }));
    await expect(apiFetch('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();

    const { url, init, headers } = sentRequest(mock);
    expect(url).toBe('/api/v1/auth/logout');
    expect(init.method).toBe('POST');
    expect(headerRecord(headers)).toEqual({
      accept: 'application/json',
      'x-requested-with': 'ads-engine',
    });
    expect(headers.get(CSRF_HEADER)).toBe(CSRF_VALUE);
    expect(headers.has('Content-Type')).toBe(false);
    expect('body' in init).toBe(false);
  });

  it.each(['PUT', 'PATCH', 'DELETE'] as const satisfies readonly HttpMethod[])(
    'sends the CSRF header on %s',
    async (method) => {
      const mock = stubFetch(() => json({}));
      await apiFetch('/x', { method, body: { a: 1 } });
      const { init, headers } = sentRequest(mock);
      expect(init.method).toBe(method);
      expect(headers.get(CSRF_HEADER)).toBe(CSRF_VALUE);
    },
  );

  it('sends a JSON null body, because only undefined means "no body"', async () => {
    const mock = stubFetch(() => json({}));
    await apiFetch('/x', { method: 'POST', body: null });
    const { init, headers } = sentRequest(mock);
    expect(init.body).toBe('null');
    expect(headers.get('Content-Type')).toBe('application/json');
  });

  it('passes the caller signal through', async () => {
    const mock = stubFetch(() => json({}));
    const controller = new AbortController();
    await apiFetch('/sources', { signal: controller.signal });
    expect(sentRequest(mock).init.signal).toBe(controller.signal);
  });

  it('refuses a body on a GET without sending anything', async () => {
    const mock = stubFetch(() => json({}));
    const error = await apiErrorOf(apiFetch('/sources', { body: { a: 1 } }));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 0,
      code: 'invalid_request',
      message: MESSAGES.invalidRequest,
      retryAfter: undefined,
    });
    expect(mock).not.toHaveBeenCalled();
  });

  it('refuses a body JSON.stringify throws on, keeping the cause, without sending', async () => {
    const mock = stubFetch(() => json({}));
    const circular: Record<string, unknown> = {};
    circular.self = circular;

    for (const body of [{ amount: 10n }, circular]) {
      const error = await apiErrorOf(apiFetch('/decisions', { method: 'POST', body }));
      expect(error.status).toBe(0);
      expect(error.code).toBe('invalid_request');
      expect(error.message).toBe(MESSAGES.invalidRequest);
      expect(error.cause).toBeInstanceOf(TypeError);
    }
    expect(mock).not.toHaveBeenCalled();
  });

  it('refuses a body that encodes to nothing (a function or symbol)', async () => {
    const mock = stubFetch(() => json({}));
    for (const body of [() => 1, Symbol('x')]) {
      const error = await apiErrorOf(apiFetch('/decisions', { method: 'POST', body }));
      expect(error.code).toBe('invalid_request');
      expect(error.status).toBe(0);
      expect('cause' in error).toBe(false);
    }
    expect(mock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
// Success responses
// ---------------------------------------------------------------------------------------------

describe('apiFetch success responses', () => {
  it('resolves the parsed JSON body', async () => {
    const run = { id: 101, kind: 'default', days: 28 };
    stubFetch(() => json(run));
    await expect(apiFetch('/runs/latest')).resolves.toEqual(run);
  });

  it('resolves the body of a 201 as well', async () => {
    stubFetch(() => json({ id: 202, kind: 'custom' }, 201));
    await expect(apiFetch('/runs', { method: 'POST', body: {} })).resolves.toEqual({
      id: 202,
      kind: 'custom',
    });
  });

  it.each([204, 205])('resolves undefined for %i without reading a body', async (status) => {
    const response = new Response(null, { status });
    const read = vi.spyOn(response, 'text');
    stubFetch(() => response);
    await expect(apiFetch('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
    expect(read).not.toHaveBeenCalled();
  });

  it.each([
    ['an empty body', ''],
    ['a whitespace-only body', ' \n\t '],
  ])('resolves undefined for a 200 with %s', async (_label, body) => {
    stubFetch(() => new Response(body, { status: 200 }));
    await expect(apiFetch('/x')).resolves.toBeUndefined();
  });

  it('rejects a 2xx body that is not JSON as invalid_response with the parse error', async () => {
    stubFetch(() => text('<html>OK</html>', 200));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 200,
      code: 'invalid_response',
      message: MESSAGES.unreadable,
      retryAfter: undefined,
    });
    expect(error.cause).toBeInstanceOf(SyntaxError);
  });

  it('rejects a 200 whose body is "not json" as invalid_response', async () => {
    stubFetch(() => new Response('not json', { status: 200, headers: JSON_CONTENT_TYPE }));
    const error = await rejection(apiFetch('/runs/latest'));
    expect(isApiError(error)).toBe(true);
    expect(error).toMatchObject({ status: 200, code: 'invalid_response' });
  });

  it('rejects a 2xx body that cannot be read as invalid_response with the read error', async () => {
    const failure = new TypeError('terminated');
    stubFetch(() => unreadable(200, failure));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.status).toBe(200);
    expect(error.code).toBe('invalid_response');
    expect(error.message).toBe(MESSAGES.unreadable);
    expect(error.cause).toBe(failure);
  });

  it('rethrows an abort during the body read unchanged', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError');
    stubFetch(() => unreadable(200, abort));
    await expect(apiFetch('/x')).rejects.toBe(abort);
  });
});

// ---------------------------------------------------------------------------------------------
// Error responses
// ---------------------------------------------------------------------------------------------

describe('apiFetch error responses', () => {
  it('keeps the envelope code and message with the response status', async () => {
    stubFetch(() =>
      json(envelope('subject_retired', 'Computed from data that has since been replaced.'), 409),
    );
    const error = await apiErrorOf(apiFetch('/decisions', { method: 'POST', body: {} }));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 409,
      code: 'subject_retired',
      message: 'Computed from data that has since been replaced.',
      retryAfter: undefined,
    });
  });

  it('rejects a 409 run_in_progress with an ApiError that is an Error', async () => {
    stubFetch(() =>
      json({ error: { code: 'run_in_progress', message: 'Another analysis is running' } }, 409),
    );
    const error = await rejection(
      apiFetch('/runs', {
        method: 'POST',
        body: { window_start: '2026-09-01', window_end: '2026-09-28' },
      }),
    );

    expect(isApiError(error)).toBe(true);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({
      name: 'ApiError',
      status: 409,
      code: 'run_in_progress',
      message: 'Another analysis is running',
    });
  });

  it("maps FastAPI's default 422 {detail: [...]} body to http_422", async () => {
    stubFetch(() =>
      json(
        {
          detail: [
            {
              type: 'missing',
              loc: ['body', 'window_end'],
              msg: 'Field required',
              input: { window_start: '2026-09-01' },
            },
          ],
        },
        422,
      ),
    );
    const error = await apiErrorOf(
      apiFetch('/runs', { method: 'POST', body: { window_start: '2026-09-01' } }),
    );
    expect(error.status).toBe(422);
    expect(error.code).toBe('http_422');
    expect(error.message).toBe('Request failed with status 422.');
  });

  it('keeps the server code of a 422 in the error shape', async () => {
    stubFetch(() => json(envelope('validation_error', 'window_end: Field required'), 422));
    const error = await apiErrorOf(apiFetch('/runs', { method: 'POST', body: {} }));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 422,
      code: 'validation_error',
      message: 'window_end: Field required',
      retryAfter: undefined,
    });
  });

  it('keeps the server code of a 503 in the error shape, not the fallback', async () => {
    stubFetch(() => json(envelope('database_unavailable', 'Database unavailable'), 503));
    const error = await apiErrorOf(apiFetch('/healthz'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 503,
      code: 'database_unavailable',
      message: 'Database unavailable',
      retryAfter: undefined,
    });
  });

  it('maps an nginx 502 HTML page to http_502 and the "temporarily unavailable" text', async () => {
    stubFetch(() => text('<html>Bad Gateway</html>', 502));
    const error = await apiErrorOf(apiFetch('/runs/latest'));
    expect(error.status).toBe(502);
    expect(error.code).toBe('http_502');
    expect(error.message).toMatch(/temporarily unavailable/);
    expect(error.message).toBe(MESSAGES.unavailable);
  });

  it('keeps upstream_unavailable and the "temporarily unavailable" text of nginx\'s JSON 502', async () => {
    // The exact body web/nginx.conf sends from @api_upstream_unavailable while the API is down.
    const body =
      '{"error":{"code":"upstream_unavailable","message":"The server is temporarily unavailable. Try again shortly."}}';
    stubFetch(() => text(body, 502, JSON_CONTENT_TYPE));
    const error = await apiErrorOf(apiFetch('/runs/latest'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 502,
      code: 'upstream_unavailable',
      message: MESSAGES.unavailable,
      retryAfter: undefined,
    });
    expect(error.message).toMatch(/temporarily unavailable/);
  });

  it('passes an envelope code the client does not know through unchanged', async () => {
    stubFetch(() => json(envelope('brand_new_code', 'Something new.'), 422));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.code).toBe('brand_new_code');
    expect(error.message).toBe('Something new.');
  });

  it.each([
    [409, 'Request failed with status 409.'],
    [503, MESSAGES.unavailable],
  ])('replaces an empty envelope message on %i with the fallback', async (status, message) => {
    stubFetch(() => json(envelope('run_not_ready', ''), status));
    const error = await apiErrorOf(apiFetch('/runs/7'));
    expect(error.code).toBe('run_not_ready');
    expect(error.message).toBe(message);
  });

  it.each([502, 503, 504])(
    'maps a %i without an envelope to http_<status> and the unavailable message',
    async (status) => {
      stubFetch(() => text('<html><body>Bad Gateway</body></html>', status));
      const error = await apiErrorOf(apiFetch('/runs/latest'));
      expect(fields(error)).toEqual({
        name: 'ApiError',
        status,
        code: `http_${String(status)}`,
        message: MESSAGES.unavailable,
        retryAfter: undefined,
      });
    },
  );

  it.each([
    ['an empty body', () => new Response('', { status: 404 }), 404],
    ['a whitespace-only body', () => new Response('  ', { status: 400 }), 400],
    ['an HTML page', () => text('<h1>Not Found</h1>', 404), 404],
    ['JSON without an envelope', () => json({ detail: 'boom' }, 500), 500],
    ['a JSON array', () => json([1, 2], 500), 500],
    ['JSON null', () => json(null, 500), 500],
    ['an envelope whose error is null', () => json({ error: null }, 500), 500],
    ['an envelope whose error is a string', () => json({ error: 'boom' }, 500), 500],
    ['an envelope with an empty code', () => json(envelope('', 'x'), 422), 422],
    [
      'an envelope with a non-string message',
      () => json({ error: { code: 'x', message: 5 } }, 422),
      422,
    ],
    ['an envelope without a message', () => json({ error: { code: 'x' } }, 422), 422],
    ['an unreadable body', () => unreadable(500, new TypeError('terminated')), 500],
  ])('maps %s to http_<status> and the generic message', async (_label, respond, status) => {
    stubFetch(respond);
    const error = await apiErrorOf(apiFetch('/x'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status,
      code: `http_${String(status)}`,
      message: `Request failed with status ${String(status)}.`,
      retryAfter: undefined,
    });
  });

  it('rethrows an abort during an error body read unchanged', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError');
    stubFetch(() => unreadable(500, abort));
    await expect(apiFetch('/x')).rejects.toBe(abort);
  });
});

describe('Retry-After', () => {
  it('reads delta-seconds on a rate-limited envelope', async () => {
    stubFetch(() =>
      json(envelope('rate_limited', 'Too many login attempts.'), 429, { 'Retry-After': '60' }),
    );
    const error = await apiErrorOf(
      apiFetch('/auth/login', { method: 'POST', body: { email: 'a', password: 'b' } }),
    );
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 429,
      code: 'rate_limited',
      message: 'Too many login attempts.',
      retryAfter: 60,
    });
  });

  it('reads Retry-After: 42 on a 429 rate_limited', async () => {
    stubFetch(() =>
      json(envelope('rate_limited', 'Too many login attempts.'), 429, { 'Retry-After': '42' }),
    );
    const error = await apiErrorOf(
      apiFetch('/auth/login', { method: 'POST', body: { email: 'a', password: 'b' } }),
    );
    expect(error.status).toBe(429);
    expect(error.code).toBe('rate_limited');
    expect(error.retryAfter).toBe(42);
  });

  it('reads an HTTP-date 30 s in the future as 30 under fake timers', async () => {
    vi.useFakeTimers();
    const now = new Date('2026-10-06T09:00:00Z');
    vi.setSystemTime(now);
    const at = new Date(now.getTime() + 30_000).toUTCString();
    stubFetch(() => json(envelope('rate_limited', 'x'), 429, { 'Retry-After': at }));

    const error = await apiErrorOf(apiFetch('/auth/login', { method: 'POST', body: {} }));
    expect(at).toBe('Tue, 06 Oct 2026 09:00:30 GMT');
    expect(error.retryAfter).toBe(30);
  });

  it.each([
    ['missing', undefined],
    ['unparsable', 'in a while'],
  ])('leaves retryAfter undefined when Retry-After is %s', async (_label, header) => {
    stubFetch(() =>
      json(
        envelope('rate_limited', 'x'),
        429,
        header === undefined ? {} : { 'Retry-After': header },
      ),
    );
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.code).toBe('rate_limited');
    expect(error.retryAfter).toBeUndefined();
  });

  it('reads it on a response without an envelope too', async () => {
    stubFetch(() => text('<h1>Service Unavailable</h1>', 503, { 'Retry-After': '120' }));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.code).toBe('http_503');
    expect(error.retryAfter).toBe(120);
  });

  it.each([
    ['0', 0],
    [' 7 ', 7],
    ['5.5', undefined],
    ['-1', undefined],
    ['1e3', undefined],
    ['soon', undefined],
    ['', undefined],
    ['99999999999999999999', undefined],
  ])('parses %j as %s', async (header, expected) => {
    stubFetch(() => json(envelope('rate_limited', 'x'), 429, { 'Retry-After': header }));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.retryAfter).toBe(expected);
  });

  it('reads an HTTP-date as whole seconds from now, rounded up', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-06T09:00:00.500Z'));
    stubFetch(() =>
      json(envelope('rate_limited', 'x'), 429, { 'Retry-After': 'Tue, 06 Oct 2026 09:01:30 GMT' }),
    );
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.retryAfter).toBe(90);
  });

  it('reads an HTTP-date already past as 0', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-10-06T09:00:00Z'));
    stubFetch(() =>
      json(envelope('rate_limited', 'x'), 429, { 'Retry-After': 'Tue, 06 Oct 2026 08:59:00 GMT' }),
    );
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.retryAfter).toBe(0);
  });

  it('ignores a header with letters that is not a date', async () => {
    stubFetch(() => json(envelope('rate_limited', 'x'), 429, { 'Retry-After': 'Tomorrow-ish' }));
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.code).toBe('rate_limited');
    expect(error.retryAfter).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------------------------
// Network failures and aborts
// ---------------------------------------------------------------------------------------------

describe('apiFetch network failures', () => {
  it('maps a rejected fetch to status 0 network_error with the cause', async () => {
    const failure = new TypeError('Failed to fetch');
    stubFetchRejecting(failure);
    const error = await apiErrorOf(apiFetch('/runs/latest'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 0,
      code: 'network_error',
      message: MESSAGES.network,
      retryAfter: undefined,
    });
    expect(error.cause).toBe(failure);
  });

  it('rethrows an AbortError from fetch unchanged', async () => {
    const abort = new DOMException('The operation was aborted.', 'AbortError');
    stubFetchRejecting(abort);
    await expect(apiFetch('/runs/latest')).rejects.toBe(abort);
  });

  it.each([
    ['before', true],
    ['during', false],
  ])(
    'rejects with the original AbortError, not an ApiError, when aborted %s the call',
    async (_label, abortFirst) => {
      const controller = new AbortController();
      const abort = new DOMException('Aborted', 'AbortError');
      if (abortFirst) {
        controller.abort();
      }
      const mock = vi.fn<FetchImpl>(() => {
        if (!abortFirst) {
          controller.abort();
        }
        return Promise.reject(abort);
      });
      vi.stubGlobal('fetch', mock);

      const error = await rejection(apiFetch('/runs/latest', { signal: controller.signal }));
      expect(error).toBe(abort);
      expect(error).toBeInstanceOf(DOMException);
      expect((error as DOMException).name).toBe('AbortError');
      expect(isApiError(error)).toBe(false);
      expect(mock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
    },
  );

  it('rethrows an error named AbortError from another realm unchanged', async () => {
    const abort = { name: 'AbortError', message: 'aborted' };
    stubFetchRejecting(abort);
    await expect(apiFetch('/runs/latest')).rejects.toBe(abort);
  });

  it('rethrows any rejection unchanged once the caller signal has aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    const reason = new Error('request cancelled');
    stubFetchRejecting(reason);
    const error = await rejection(apiFetch('/runs/latest', { signal: controller.signal }));
    expect(error).toBe(reason);
    expect(isApiError(error)).toBe(false);
  });

  it('wraps a rejection that is not an object', async () => {
    stubFetchRejecting('offline');
    const error = await apiErrorOf(apiFetch('/x'));
    expect(error.code).toBe('network_error');
    expect(error.cause).toBe('offline');
  });
});

// ---------------------------------------------------------------------------------------------
// apiFetchPage and the Link header
// ---------------------------------------------------------------------------------------------

describe('apiFetchPage', () => {
  /**
   * Stubs one paged response with the given `Link` header value.
   *
   * @param link - The `Link` header, or `undefined` for none.
   * @param body - The page's JSON body; one discrepancy-like row when omitted.
   * @returns The mock.
   */
  function stubPage(link: string | undefined, body: unknown = [{ id: 1 }]): FetchMock {
    return stubFetch(() => json(body, 200, link === undefined ? {} : { Link: link }));
  }

  it('sends the request like apiFetch and resolves next undefined without a Link', async () => {
    const mock = stubPage(undefined);
    const controller = new AbortController();
    const page = await apiFetchPage('/discrepancies', {
      query: { state: ['open', 'awaiting_platform'], limit: 500 },
      signal: controller.signal,
    });

    expect(page).toEqual({ data: [{ id: 1 }], next: undefined });
    const { url, init, headers } = sentRequest(mock);
    expect(url).toBe('/api/v1/discrepancies?state=open&state=awaiting_platform&limit=500');
    expect(init.method).toBe('GET');
    expect(init.credentials).toBe('same-origin');
    expect(init.signal).toBe(controller.signal);
    expect(headerRecord(headers)).toEqual({ accept: 'application/json' });
  });

  it('resolves the rel="next" target relative to /api/v1, query included', async () => {
    const next = '/discrepancies?state=open&state=awaiting_platform&limit=500&after=812';
    stubPage(`<${API_BASE}${next}>; rel="next"`);
    const page = await apiFetchPage('/discrepancies');
    expect(page.next).toBe(next);
  });

  it('requests a later page from its next path unchanged', async () => {
    const mock = stubPage(undefined);
    await apiFetchPage('/discrepancies?state=open&limit=500&after=812');
    expect(sentRequest(mock).url).toBe('/api/v1/discrepancies?state=open&limit=500&after=812');
  });

  it.each([
    ['an unquoted rel', '</api/v1/d?after=1>; rel=next', '/d?after=1'],
    ['an upper-case parameter and relation', '</api/v1/d?after=1>; REL="NEXT"', '/d?after=1'],
    ['several relation types', '</api/v1/d?after=1>; rel="prev next"', '/d?after=1'],
    ['tabs between relation types', '</api/v1/d?after=1>; rel="prev\tnext"', '/d?after=1'],
    ['whitespace around separators', '</api/v1/d?after=1>\t ;  rel = "next" ', '/d?after=1'],
    [
      'a prev link before it',
      '</api/v1/d?after=0>; rel="prev", </api/v1/d?after=9>; rel="next"',
      '/d?after=9',
    ],
    ['empty list elements', ' , ,</api/v1/d?after=2>; rel=next,', '/d?after=2'],
    ['the same next twice', '</api/v1/d?a=1>; rel=next, </api/v1/d?a=1>; rel="next"', '/d?a=1'],
    ['a parameter without a value', '</api/v1/d>; crossorigin; rel=next', '/d'],
    ['a non-token unquoted value', '</api/v1/d>; type=application/json; rel=next', '/d'],
    [
      'a quoted value holding separators and escapes',
      '</api/v1/d>; title="a; b, \\"c\\""; rel="next"',
      '/d',
    ],
    ['a later rel ignored when the first is next', '</api/v1/d>; rel=next; rel=prev', '/d'],
    [
      'dot segments that stay under the base',
      '</api/v1/./x/../discrepancies?a=1>; rel=next',
      '/discrepancies?a=1',
    ],
    ['a fragment, which is dropped', '</api/v1/d?after=3#top>; rel=next', '/d?after=3'],
  ])('reads %s', async (_label, link, next) => {
    stubPage(link);
    await expect(apiFetchPage('/d')).resolves.toEqual({ data: [{ id: 1 }], next });
  });

  it('reads several Link headers, which arrive joined into one list', async () => {
    const headers = new Headers({ 'Content-Type': 'application/json' });
    headers.append('Link', '</api/v1/d?after=0>; rel="prev"');
    headers.append('Link', '</api/v1/d?after=9>; rel="next"');
    stubFetch(() => new Response('[]', { status: 200, headers }));
    await expect(apiFetchPage('/d')).resolves.toEqual({ data: [], next: '/d?after=9' });
  });

  it.each([
    ['only a prev link', '</api/v1/d?after=0>; rel="prev"'],
    ['a link without rel', '</api/v1/d?after=0>'],
    ['a rel that only contains "next"', '</api/v1/d?after=0>; rel="nextpage"'],
    ['a later rel=next after another rel', '</api/v1/d>; rel=prev; rel=next'],
    ['an empty header', ''],
  ])('resolves next undefined for %s', async (_label, link) => {
    stubPage(link);
    await expect(apiFetchPage('/d')).resolves.toEqual({ data: [{ id: 1 }], next: undefined });
  });

  it('resolves undefined data with the next page for a 204', async () => {
    stubFetch(
      () => new Response(null, { status: 204, headers: { Link: '</api/v1/d?after=4>; rel=next' } }),
    );
    await expect(apiFetchPage('/d')).resolves.toEqual({ data: undefined, next: '/d?after=4' });
  });

  it.each([
    ['a value that does not start with "<"', '/api/v1/d; rel=next'],
    ['an unterminated "<"', '</api/v1/d; rel=next'],
    ['an unterminated quoted string', '</api/v1/d>; rel="next'],
    ['a quoted string ending in a backslash', '</api/v1/d>; rel="next\\'],
    ['a parameter without a name', '</api/v1/d>; =next'],
    ['text between parameters', '</api/v1/d> junk; rel=next'],
    ['text after a quoted value', '</api/v1/d>; rel="next"x'],
    ['a malformed link-value after a valid next', '</api/v1/d>; rel=next, garbage'],
    ['two different next pages', '</api/v1/d?after=1>; rel=next, </api/v1/d?after=2>; rel=next'],
    ['an absolute URL', '<https://evil.example/api/v1/d>; rel=next'],
    ['a protocol-relative URL', '<//evil.example/api/v1/d>; rel=next'],
    ['a relative path', '<api/v1/d>; rel=next'],
    ['a backslash', '</api/v1\\d>; rel=next'],
    ['a path outside the base', '</other/d>; rel=next'],
    ['a path with the base only as a prefix', '</api/v1d>; rel=next'],
    ['the base itself without a trailing slash', '</api/v1>; rel=next'],
    ['dot segments that leave the base', '</api/v1/../admin>; rel=next'],
    ['encoded dot segments that leave the base', '</api/v1/%2e%2e/admin>; rel=next'],
  ])('rejects %s as invalid_response with the response status', async (_label, link) => {
    stubPage(link);
    const error = await apiErrorOf(apiFetchPage('/d'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 200,
      code: 'invalid_response',
      message: MESSAGES.invalidLink,
      retryAfter: undefined,
    });
  });

  it('maps errors exactly as apiFetch does and never reads the Link of a failure', async () => {
    stubFetch(() =>
      json(envelope('invalid_cursor', 'The after cursor names no discrepancy.'), 422, {
        Link: 'garbage',
      }),
    );
    const error = await apiErrorOf(apiFetchPage('/discrepancies?after=9'));
    expect(error.status).toBe(422);
    expect(error.code).toBe('invalid_cursor');
    expect(error.message).toBe('The after cursor names no discrepancy.');
  });

  it('maps a rejected fetch to network_error', async () => {
    stubFetchRejecting(new TypeError('Failed to fetch'));
    const error = await apiErrorOf(apiFetchPage('/discrepancies'));
    expect(error.status).toBe(0);
    expect(error.code).toBe('network_error');
  });

  it('resolves total undefined without an X-Total-Count header', async () => {
    stubPage('</api/v1/d?after=1>; rel=next');
    const page = await apiFetchPage('/d');
    expect(Object.hasOwn(page, 'total')).toBe(true);
    expect(page.total).toBeUndefined();
    expect(page.next).toBe('/d?after=1');
  });

  it.each([
    ['zero', '0', 0],
    ['a count', '64', 64],
    ['surrounding whitespace', ' 12 ', 12],
    ['leading zeros', '007', 7],
    ['the largest safe integer', String(Number.MAX_SAFE_INTEGER), Number.MAX_SAFE_INTEGER],
  ])('reads an X-Total-Count of %s into total, next page or not', async (_label, header, total) => {
    stubFetch(() =>
      json([{ id: 1 }], 200, { 'X-Total-Count': header, Link: '</api/v1/d?after=1>; rel=next' }),
    );
    await expect(apiFetchPage('/d')).resolves.toStrictEqual({
      data: [{ id: 1 }],
      next: '/d?after=1',
      total,
    });
    stubFetch(() => json([], 200, { 'X-Total-Count': header }));
    await expect(apiFetchPage('/d?after=1')).resolves.toStrictEqual({
      data: [],
      next: undefined,
      total,
    });
  });

  it.each([
    ['an empty value', ''],
    ['a negative count', '-1'],
    ['a signed count', '+3'],
    ['a fraction', '1.5'],
    ['exponent form', '1e3'],
    ['a hexadecimal literal', '0x10'],
    ['text', 'many'],
    ['two values joined', '5, 5'],
    ['a count beyond the safe integers', '9007199254740993'],
  ])('rejects an X-Total-Count of %s as invalid_response', async (_label, header) => {
    stubFetch(() => json([{ id: 1 }], 200, { 'X-Total-Count': header }));
    const error = await apiErrorOf(apiFetchPage('/d'));
    expect(fields(error)).toEqual({
      name: 'ApiError',
      status: 200,
      code: 'invalid_response',
      message: MESSAGES.invalidTotal,
      retryAfter: undefined,
    });
  });

  it('never reads the X-Total-Count of a failure', async () => {
    stubFetch(() =>
      json(envelope('invalid_cursor', 'The after cursor names no discrepancy.'), 422, {
        'X-Total-Count': 'garbage',
      }),
    );
    const error = await apiErrorOf(apiFetchPage('/discrepancies?after=9'));
    expect(error.code).toBe('invalid_cursor');
  });
});
