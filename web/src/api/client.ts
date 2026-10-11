/**
 * The one HTTP client of the SPA: a typed `fetch` wrapper for the `/api/v1` REST contract
 * (AAP 0.5.1, 0.5.3).
 *
 * Every request the web client makes goes through {@link apiFetch}, or through
 * {@link apiFetchPage} for a paged list, which sends the request the same way and also returns
 * the next page the response's `Link` header names. This is the only module allowed to call
 * `fetch` (`eslint.config.js`, block `web/api-client`), and feature code never imports it:
 * features use the TanStack Query hooks of `queries.ts`, which re-exports {@link ApiError},
 * {@link isApiError} and {@link ApiErrorCode} (AAP 0.11.1).
 *
 * What it does for every call:
 *
 * - **Same origin.** URLs are `/api/v1/…` relative to the page. nginx proxies `/api/` to the API
 *   in Compose, and the Vite dev server proxies it in development, so no host, port or other
 *   build-time configuration reaches the bundle (AAP 0.3.2).
 * - **Session.** The session is a signed, HttpOnly, `SameSite=Lax` cookie. It is sent with
 *   `credentials: 'same-origin'` and is never read or stored by this module.
 * - **CSRF.** Every mutating request (`POST`, `PUT`, `PATCH`, `DELETE`) carries
 *   `X-Requested-With: ads-engine`, the header and value `backend/src/ads_engine/api/security.py`
 *   requires (`CSRF_HEADER`, `CSRF_VALUE`, `MUTATING_METHODS`). `GET` never carries it.
 * - **Errors.** Every failure becomes an {@link ApiError}: the API's
 *   `{"error": {"code", "message"}}` envelope keeps its code and message, and so does the same
 *   envelope that `web/nginx.conf` sends for an error nginx makes itself (the API down, a body
 *   over 1 MB). A response without that envelope (an empty body, or an HTML page from a proxy
 *   in front of nginx) gets `http_<status>`, an unreachable server gets status 0 and
 *   `network_error`, and an unreadable success body gets `invalid_response`,
 *   as does a paged response whose `Link` header cannot be parsed or names a next page outside
 *   {@link API_BASE}. The one exception is a caller-initiated abort, which rejects with the
 *   original abort error so TanStack Query treats it as a cancellation rather than a failure.
 * - **Paging.** A paged list such as `GET /discrepancies` names its next page in an RFC 8288
 *   `Link: <…>; rel="next"` header. {@link apiFetchPage} returns that target as a path
 *   relative to {@link API_BASE}, query included, which is passed back unchanged to read the
 *   next page. A page can hold fewer rows than its `limit` while more follow, so only a
 *   missing `rel="next"` ends a list. The page's `X-Total-Count` header, the size of the whole
 *   list, becomes {@link ApiPage.total}; a malformed one is an `invalid_response`.
 *
 * The module has no state and no side effects at load time. It calls the global `fetch` at
 * request time, so `vi.stubGlobal('fetch', impl)` replaces it in tests. It never logs: request
 * bodies can carry the admin's email and password (AAP 0.11.1).
 *
 * @example
 * ```ts
 * import { apiFetch, isApiError } from './client';
 * import type { Decision } from './types';
 *
 * async function campaignDecisions(id: number, signal?: AbortSignal): Promise<Decision[]> {
 *   return apiFetch<Decision[]>('/decisions', {
 *     query: { subject_type: 'campaign', subject_id: id },
 *     signal,
 *   });
 * }
 *
 * try {
 *   await apiFetch('/auth/login', { method: 'POST', body: { email, password } });
 * } catch (error) {
 *   if (isApiError(error) && error.code === 'rate_limited') {
 *     showMessage(`Try again in ${error.retryAfter ?? 60} seconds`);
 *   }
 * }
 * ```
 */

import type { ErrorResponse } from './types';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/**
 * Base path of every API route (AAP 0.5.3), same origin as the SPA. {@link apiFetch} and
 * {@link apiFetchPage} prefix it to the path they receive, so callers write `/runs/latest`, not
 * `/api/v1/runs/latest`, and {@link ApiPage.next} is relative to it in the same way.
 */
export const API_BASE = '/api/v1';

/**
 * Name of the CSRF header every mutating request carries (AAP 0.3.2). Mirrors `CSRF_HEADER` in
 * `backend/src/ads_engine/api/security.py`.
 */
export const CSRF_HEADER = 'X-Requested-With';

/**
 * The exact, case-sensitive value of {@link CSRF_HEADER}. Mirrors `CSRF_VALUE` in
 * `backend/src/ads_engine/api/security.py`; the API answers a mutating request without it with
 * 403 `csrf_header_missing`.
 */
export const CSRF_VALUE = 'ads-engine';

/** Fallback message for a request that never reached the server (status 0). */
const NETWORK_ERROR_MESSAGE =
  'The server could not be reached. Check your connection and try again.';

/** Fallback message for a 2xx response whose body is not JSON. */
const INVALID_RESPONSE_MESSAGE = 'The server returned an unreadable response.';

/** Message for a paged response whose `Link` header is malformed or names an invalid next page. */
const INVALID_LINK_MESSAGE = 'The server returned an invalid link to the next page.';

/**
 * The header a paged list sends with the size of the whole list on every page, for example
 * `GET /discrepancies` (`TOTAL_COUNT_HEADER` in
 * `backend/src/ads_engine/api/routes/discrepancies.py`).
 */
const TOTAL_COUNT_HEADER = 'X-Total-Count';

/** Message for a paged response whose `X-Total-Count` header is not a non-negative integer. */
const INVALID_TOTAL_MESSAGE = 'The server returned an invalid total count.';

/** A non-negative decimal integer as an HTTP header carries it: digits only, no sign. */
const TOTAL_COUNT_PATTERN = /^\d+$/;

/**
 * Origin used only to resolve a `Link` target and check that it stays on the SPA's origin. The
 * `.invalid` top-level domain never resolves (RFC 6761), and no request is ever sent to it.
 */
const LINK_RESOLUTION_ORIGIN = 'http://link-target.invalid';

/** RFC 9110 `tchar`: the characters of a token, such as a `Link` parameter name or value. */
const TOKEN_CHAR = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]$/;

/** Fallback message for a request this client cannot encode, so never sends. */
const INVALID_REQUEST_MESSAGE = 'The request could not be sent: its body cannot be encoded.';

/**
 * Fallback message for a gateway or availability failure without the API's error envelope.
 * `web/nginx.conf` sends the same text in its own 502, 503 and 504 envelopes.
 */
const UNAVAILABLE_MESSAGE = 'The server is temporarily unavailable. Try again shortly.';

/** Statuses that mean the API is down or unreachable behind nginx, not that the request failed. */
const UNAVAILABLE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

/** Successful statuses that carry no body by definition. */
const NO_CONTENT_STATUSES: ReadonlySet<number> = new Set([204, 205]);

// ---------------------------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------------------------

/**
 * The HTTP methods the API uses. Every method other than `GET` is mutating and carries the CSRF
 * header (`MUTATING_METHODS` in `security.py`).
 */
export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/**
 * One query-string value. `null` and `undefined` omit the parameter, booleans become `true` or
 * `false`, numbers are written with `String()`, and each array element is appended as its own
 * repetition of the parameter (`state=open&state=awaiting_platform`).
 */
export type QueryValue =
  | string
  | number
  | boolean
  | readonly (string | number)[]
  | null
  | undefined;

/** Options of one {@link apiFetch} or {@link apiFetchPage} call. Every field is optional. */
export interface ApiFetchInit {
  /** The HTTP method; `GET` when omitted. */
  method?: HttpMethod;
  /**
   * The request body, sent as JSON with `Content-Type: application/json`. Omitted (or
   * `undefined`) means no body. A `GET` request cannot carry one.
   */
  body?: unknown;
  /**
   * Query parameters, appended in insertion order. Values follow {@link QueryValue}; a query
   * whose every value is `null` or `undefined` adds no `?` at all.
   */
  query?: Readonly<Record<string, QueryValue>>;
  /** Cancels the request; TanStack Query passes its own signal here. */
  signal?: AbortSignal;
}

/**
 * One page of a paged list, as {@link apiFetchPage} resolves it.
 *
 * @typeParam T - The page's response type from `types.ts`, for example `Discrepancy[]`.
 */
export interface ApiPage<T> {
  /** The parsed JSON body, exactly as {@link apiFetch} would resolve it. */
  data: T;
  /**
   * The next page the response's `Link: <…>; rel="next"` header names, as a path relative to
   * {@link API_BASE} with its query, for example
   * `/discrepancies?state=open&state=awaiting_platform&limit=500&after=812`. Pass it back to
   * {@link apiFetchPage} unchanged, with no `query`. `undefined` when the response has no
   * `Link` header or names no next page, which means the list is complete.
   */
  next: string | undefined;
  /**
   * The size of the whole list from the response's `X-Total-Count` header, read when this page
   * was served and independent of its cursor and `limit`; for `GET /discrepancies` the rows of
   * the active datasets in the requested states, including any the page leaves out. `undefined`
   * when the response has no such header.
   */
  total: number | undefined;
}

/**
 * The error codes this client can report in {@link ApiError.code}.
 *
 * Server codes are the `error.code` values the API's `{"error": {"code", "message"}}` envelope
 * carries, taken from the backend that emits them: `api/deps.py`, `api/app.py`,
 * `api/routes/*.py`, `analysis/window.py`, `analysis/lock.py` and `decisions.py`. Gateway
 * codes are the ones `web/nginx.conf` sends in the same envelope for an error nginx makes
 * itself under `/api/`, before or instead of the API's answer. Client codes (`network_error`,
 * `invalid_response`, `invalid_request` and `http_<status>`) are produced here when no
 * envelope is available.
 *
 * The union documents the contract and gives `switch` statements a checked vocabulary.
 * {@link ApiError.code} itself stays `string`, so a code a newer API adds still reaches the
 * caller unchanged.
 *
 * The resolution and closed-discrepancy codes are the ones `decisions.py` emits,
 * `resolution_not_allowed`, `intended_state_invalid` and `discrepancy_resolved`; the API sends
 * no `invalid_resolution` or `discrepancy_closed`, so the union does not list them.
 */
export type ApiErrorCode =
  // --- Authentication and request guards ---------------------------------------------------
  /** 401: no valid session cookie; sign in again (`deps.require_user`). */
  | 'unauthenticated'
  /** 401: `POST /auth/login` with an unknown email or a wrong password. */
  | 'invalid_credentials'
  /** 403: a mutating request without `X-Requested-With: ads-engine` (`deps.require_csrf`). */
  | 'csrf_header_missing'
  /** 403: refused by the framework itself rather than by a route. */
  | 'forbidden'
  /**
   * 429: too many login attempts (5 per minute per IP). {@link ApiError.retryAfter} holds the
   * `Retry-After` seconds.
   */
  | 'rate_limited'
  // --- Lookup ----------------------------------------------------------------------------------
  /**
   * 404: the run, recommendation, decision subject or discrepancy does not exist, the path is
   * unknown, or no default analysis run has succeeded yet ("No analysis run yet").
   */
  | 'not_found'
  /**
   * 405: the path does not support the method. nginx (`web/nginx.conf`) sends the same code and
   * body for a `TRACE` request, or one whose method is not `GET`, `HEAD`, `POST`, `PUT`,
   * `PATCH`, `DELETE` or `OPTIONS`, which it refuses before the API sees it.
   */
  | 'method_not_allowed'
  // --- Conflicts (409) -------------------------------------------------------------------------
  /** 409: the requested run has not succeeded (it is still running or it failed). */
  | 'run_not_ready'
  /** 409: `POST /runs` while any process holds the shared analysis lock. */
  | 'run_in_progress'
  /**
   * 409: the item or subject belongs to a dataset that has since been replaced, so decisions,
   * resolutions and feedback are refused ("Computed from data that has since been replaced").
   */
  | 'subject_retired'
  /** 409: `POST /sources/{source}/sync` while a sync request for that source is pending. */
  | 'sync_pending'
  /** 409: the discrepancy is already resolved. */
  | 'discrepancy_resolved'
  /**
   * 409: the platform state of the subject is not known yet (for example a snapshot without a
   * budget amount, or a subject with no decision that no entity sync has reported), so it
   * cannot be adopted or decided until a later sync observes it.
   */
  | 'platform_state_unknown'
  /** 409: a conflict raised by the framework itself rather than by a route. */
  | 'conflict'
  // --- Validation (422) ------------------------------------------------------------------------
  /** 422: the request body, path or query does not match its schema. */
  | 'validation_error'
  /** 422: feedback on a C0 discrepancy item, which is exempt from dismiss, snooze and done. */
  | 'feedback_not_allowed'
  /**
   * 422: `GET /discrepancies` with an `after` cursor that names no discrepancy, for example
   * because its dataset was purged between pages; list again from the first page.
   */
  | 'invalid_cursor'
  /** 422: `GET /decisions` with `subject_id` but no `subject_type`. */
  | 'invalid_query'
  /** 422: `recommendation_id` names an item that offers no decision to record. */
  | 'decision_not_offered'
  /** 422: `recommendation_id` names an item whose decision is about another subject. */
  | 'subject_mismatch'
  /** 422: a custom window that is otherwise invalid. */
  | 'invalid_window'
  /** 422: a custom window whose end is before its start. */
  | 'end_before_start'
  /** 422: a custom window that ends after the default window end. */
  | 'end_after_default_end'
  /** 422: a custom window longer than `window.max_days`. */
  | 'too_long'
  /** 422: a custom window shorter than 1 day. */
  | 'too_short'
  /** 422: a decision request rejected without a more specific code. */
  | 'decision_error'
  /** 422: a decision patch rejected without a more specific code. */
  | 'patch_rejected'
  /** 422: a decision patch that carries no field. */
  | 'empty_patch'
  /** 422: a decision patch that changes nothing in the last decided state. */
  | 'no_change'
  /** 422: an amount that is not positive, not finite or too large. */
  | 'invalid_amount'
  /** 422: an amount finer than the currency's minor unit (Google: whole micros). */
  | 'sub_minor_unit'
  /** 422: a field the subject's decision shape does not allow. */
  | 'field_not_allowed'
  /** 422: a budget amount on a shared-budget member; the message names the shared budget. */
  | 'shared_budget_amount'
  /**
   * 422: a decision on a reported subject with no decision yet; the message says whether its
   * `untracked` discrepancy is open to resolve or not raised yet.
   */
  | 'untracked_subject'
  /** 422: a decision on a subject whose removal was acknowledged. */
  | 'subject_removed'
  /** 422: the subject's decision shape changed since the last decision. */
  | 'shape_changed'
  /** 422: the subject's budget or budget period changed since the last decision. */
  | 'budget_changed'
  /** 422: a patch field the decision contract does not define. */
  | 'unknown_field'
  /** 422: a patch value of the wrong type or outside its allowed values. */
  | 'invalid_value'
  /** 422: a resolution not valid for the discrepancy's (kind, subject type) pair. */
  | 'resolution_not_allowed'
  /** 422: a `record_intended` state that is incomplete or does not fit the subject's shape. */
  | 'intended_state_invalid'
  // --- Server errors ---------------------------------------------------------------------------
  /** 500: a `config/*.yaml` file or stored CRM binding is invalid; the message names it. */
  | 'config_error'
  /** 500: an unexpected server failure, in the API or in nginx (`web/nginx.conf`). */
  | 'internal_error'
  /** 503: `GET /healthz` could not reach the database. */
  | 'database_unavailable'
  /** Any other framework-raised HTTP error status. */
  | 'http_error'
  // --- Gateway codes (`web/nginx.conf`) --------------------------------------------------------
  /**
   * 400: nginx could not read the request (a malformed request line, header or body), so the
   * API never saw it.
   */
  | 'bad_request'
  /** 400: the request's headers or cookies are larger than nginx accepts. */
  | 'request_header_too_large'
  /** 413: the request body is larger than nginx's 1 MB limit (`client_max_body_size`). */
  | 'payload_too_large'
  /** 501: the request's `Transfer-Encoding` is one nginx does not support (only `chunked` is). */
  | 'not_implemented'
  /**
   * 502 or 503: nginx could not reach the API, which is stopped, restarting or not yet
   * resolvable. The message is the "temporarily unavailable" text.
   */
  | 'upstream_unavailable'
  /**
   * 504: nginx gave up on the API, with no connection within 5 s or no answer within 120 s.
   * The message is the "temporarily unavailable" text.
   */
  | 'upstream_timeout'
  // --- Client-side codes -------------------------------------------------------------------
  /** Status 0: the request never reached the server, or its response was lost. */
  | 'network_error'
  /**
   * A 2xx response whose body could not be read or is not JSON, or a paged response whose
   * `Link` header cannot be parsed or names a `rel="next"` page that is not a path under
   * {@link API_BASE}, or that repeats a page already read, or whose `X-Total-Count` header is
   * not a non-negative integer.
   */
  | 'invalid_response'
  /** Status 0: the body cannot be encoded as JSON, or a `GET` was given one; nothing was sent. */
  | 'invalid_request'
  /**
   * An error response without the envelope, for example an empty body or an HTML page from a
   * proxy in front of nginx: `http_502`, `http_404` and so on.
   */
  | `http_${number}`;

/** Optional details of an {@link ApiError}. */
export interface ApiErrorOptions {
  /** Whole seconds to wait before retrying, from the response's `Retry-After` header. */
  retryAfter?: number;
  /** The underlying failure, for example the `TypeError` a rejected `fetch` raised. */
  cause?: unknown;
}

// ---------------------------------------------------------------------------------------------
// ApiError
// ---------------------------------------------------------------------------------------------

/**
 * A failed API call. {@link apiFetch} and {@link apiFetchPage} reject with this class for every
 * failure except a caller-initiated abort.
 *
 * - {@link ApiError.status} is the HTTP status, or 0 when no response arrived.
 * - {@link ApiError.code} is the envelope's machine code, or a client-side code (see
 *   {@link ApiErrorCode}). It is typed `string` so unknown server codes survive.
 * - `message` is the server's message, safe to show to the user, or this client's fallback
 *   text.
 * - {@link ApiError.retryAfter} is set when the response carried a parsable `Retry-After`.
 *
 * @example
 * ```ts
 * if (isApiError(error) && error.code === 'subject_retired') {
 *   showMessage(error.message); // "Computed from data that has since been replaced."
 * }
 * ```
 */
export class ApiError extends Error {
  /** Always `'ApiError'`, so the class survives minification and structured logging. */
  override readonly name = 'ApiError';

  /** HTTP status of the response; 0 for a network failure or a request never sent. */
  readonly status: number;

  /** Machine-readable code: the API's `error.code`, or a client-side code. */
  readonly code: string;

  /** Seconds to wait before retrying, from `Retry-After`; `undefined` when absent or unparsable. */
  readonly retryAfter?: number;

  /**
   * @param status - HTTP status, or 0 when no response arrived.
   * @param code - The machine-readable error code.
   * @param message - The text shown to the user.
   * @param options - `Retry-After` seconds and the underlying cause, both optional.
   */
  constructor(status: number, code: string, message: string, options: ApiErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.status = status;
    this.code = code;
    if (options.retryAfter !== undefined) {
      this.retryAfter = options.retryAfter;
    }
  }
}

/**
 * Whether `value` is an {@link ApiError}, for example in a TanStack Query `onError` or a
 * `catch` block.
 *
 * @param value - Anything thrown or rejected.
 * @returns `true` when `value` is an {@link ApiError} instance.
 */
export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

// ---------------------------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------------------------

/**
 * Builds `/api/v1{path}` plus the query string. `null` and `undefined` values are skipped,
 * booleans become `true`/`false`, numbers use `String()`, and array elements are appended one by
 * one under the same name. Parameters keep insertion order.
 */
function buildUrl(path: string, query: Readonly<Record<string, QueryValue>> | undefined): string {
  // A path without its leading slash would glue onto the base (`/api/v1runs`).
  const relative = path.startsWith('/') ? path : `/${path}`;
  const url = `${API_BASE}${relative}`;
  if (query === undefined) {
    return url;
  }
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined || value === null) {
      continue;
    }
    if (Array.isArray(value)) {
      for (const element of value as readonly (string | number)[]) {
        params.append(name, String(element));
      }
    } else {
      params.append(name, String(value));
    }
  }
  const search = params.toString();
  if (search === '') {
    return url;
  }
  return `${url}${relative.includes('?') ? '&' : '?'}${search}`;
}

/**
 * Whether a rejection is an abort: the caller's signal fired, or the error is named
 * `AbortError`. The name is checked structurally because jsdom, Node and browsers each ship
 * their own `DOMException`, so `instanceof` would miss an abort from another realm.
 */
function isAbort(error: unknown, signal: AbortSignal | undefined): boolean {
  if (signal?.aborted === true) {
    return true;
  }
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name: unknown }).name === 'AbortError'
  );
}

/** Whether a parsed body is the API's `{"error": {"code", "message"}}` envelope. */
function isErrorResponse(value: unknown): value is ErrorResponse {
  if (typeof value !== 'object' || value === null || !('error' in value)) {
    return false;
  }
  const detail: unknown = (value as { error: unknown }).error;
  if (typeof detail !== 'object' || detail === null) {
    return false;
  }
  const { code, message } = detail as { code?: unknown; message?: unknown };
  return typeof code === 'string' && code !== '' && typeof message === 'string';
}

/**
 * Parses a `Retry-After` header (RFC 9110 §10.2.3) into whole seconds from now: delta-seconds
 * (a non-negative integer) or an HTTP-date. Every HTTP-date format names its weekday or month,
 * so a value without a letter is never read as a date; that keeps lenient date parsing from
 * turning `5.5` or `-1` into a time. Returns `undefined` when the header is absent or
 * unparsable, and 0 for a date already past.
 */
function parseRetryAfter(header: string | null): number | undefined {
  if (header === null) {
    return undefined;
  }
  const value = header.trim();
  if (/^\d+$/.test(value)) {
    const seconds = Number(value);
    return Number.isSafeInteger(seconds) ? seconds : undefined;
  }
  if (!/[a-z]/i.test(value)) {
    return undefined;
  }
  const at = Date.parse(value);
  if (!Number.isFinite(at)) {
    return undefined;
  }
  return Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

/** Fallback message for an error response that carries no usable envelope. */
function fallbackMessage(status: number): string {
  return UNAVAILABLE_STATUSES.has(status)
    ? UNAVAILABLE_MESSAGE
    : `Request failed with status ${status}.`;
}

/** The outcome of reading or parsing a body: the value, or the error that prevented it. */
type Outcome<V> = { ok: true; value: V } | { ok: false; error: unknown };

/**
 * Reads the response body as text. Resolves `{ ok: false }` when the body cannot be read (the
 * connection dropped mid-body); rethrows the original error when the read failed because the
 * caller aborted.
 */
async function readText(
  response: Response,
  signal: AbortSignal | undefined,
): Promise<Outcome<string>> {
  try {
    return { ok: true, value: await response.text() };
  } catch (error) {
    if (isAbort(error, signal)) {
      throw error;
    }
    return { ok: false, error };
  }
}

/** Parses JSON text, resolving `{ ok: false }` instead of throwing on malformed input. */
function parseJson(text: string): Outcome<unknown> {
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch (error) {
    return { ok: false, error };
  }
}

/** Builds the {@link ApiError} for a non-2xx response from its body and headers. */
async function errorFromResponse(
  response: Response,
  signal: AbortSignal | undefined,
): Promise<ApiError> {
  const { status } = response;
  const retryAfter = parseRetryAfter(response.headers.get('Retry-After'));
  const options: ApiErrorOptions = retryAfter === undefined ? {} : { retryAfter };
  // A body that cannot be read or parsed is treated like one without an envelope: the status
  // alone still says what happened.
  const text = await readText(response, signal);
  if (text.ok && text.value.trim() !== '') {
    const parsed = parseJson(text.value);
    if (parsed.ok && isErrorResponse(parsed.value)) {
      const { code, message } = parsed.value.error;
      const shown = message === '' ? fallbackMessage(status) : message;
      return new ApiError(status, code, shown, options);
    }
  }
  return new ApiError(status, `http_${status}`, fallbackMessage(status), options);
}

/**
 * Serialises a request body as JSON. Throws an `invalid_request` {@link ApiError} when
 * `JSON.stringify` fails (a `BigInt` or a circular reference) or yields nothing to send (a
 * function or symbol), so a body that cannot be encoded is never sent.
 */
function encodeBody(body: unknown): string {
  let encoded: string | undefined;
  try {
    encoded = JSON.stringify(body) as string | undefined;
  } catch (cause) {
    throw new ApiError(0, 'invalid_request', INVALID_REQUEST_MESSAGE, { cause });
  }
  if (encoded === undefined) {
    // JSON.stringify returns undefined for a function or symbol: there is no JSON to send.
    throw new ApiError(0, 'invalid_request', INVALID_REQUEST_MESSAGE);
  }
  return encoded;
}

/** One link-value of a `Link` header. */
interface LinkValue {
  /** The URI-Reference between `<` and `>`, as sent. */
  target: string;
  /** The relation types of its first `rel` parameter, lowercased; empty without one. */
  rels: readonly string[];
}

/**
 * Parses a `Link` header field value (RFC 8288 §3) into its link-values.
 *
 * The value is a comma-separated list, in which empty elements are allowed (RFC 9110 §5.6.1),
 * of `<URI-Reference>` followed by `;`-separated parameters with optional whitespace around
 * each separator and `=`. A parameter name is a case-insensitive token. Its value, when it has
 * one, is a quoted string with `\` escapes or, as Appendix B reads it, everything up to the
 * next `;` or `,`. Only the first `rel` parameter of a link-value counts (§3.3). Its value is a
 * space-separated list of relation types, which compare case-insensitively, so they are
 * lowercased.
 *
 * Returns `undefined` for a value whose structure is broken (a link-value that does not start
 * with `<`, an unterminated `<` or quoted string, a parameter without a name, or text between
 * parameters that is not a `;` or `,`): a link this client cannot read could be the one that
 * names the next page.
 */
function parseLinkHeader(value: string): LinkValue[] | undefined {
  let at = 0;
  const skipWhitespace = (): void => {
    while (value.charAt(at) === ' ' || value.charAt(at) === '\t') {
      at += 1;
    }
  };
  const readToken = (): string => {
    const start = at;
    while (TOKEN_CHAR.test(value.charAt(at))) {
      at += 1;
    }
    return value.slice(start, at);
  };
  // Reads a quoted string whose opening quote is at `at`; undefined when it is unterminated.
  const readQuoted = (): string | undefined => {
    let text = '';
    at += 1;
    while (at < value.length) {
      const char = value.charAt(at);
      if (char === '"') {
        at += 1;
        return text;
      }
      if (char === '\\') {
        if (at + 1 >= value.length) {
          return undefined;
        }
        text += value.charAt(at + 1);
        at += 2;
      } else {
        text += char;
        at += 1;
      }
    }
    return undefined;
  };

  const links: LinkValue[] = [];
  while (at < value.length) {
    skipWhitespace();
    if (at >= value.length) {
      break;
    }
    if (value.charAt(at) === ',') {
      at += 1;
      continue;
    }
    if (value.charAt(at) !== '<') {
      return undefined;
    }
    const close = value.indexOf('>', at + 1);
    if (close === -1) {
      return undefined;
    }
    const target = value.slice(at + 1, close);
    at = close + 1;
    let rel: string | undefined;
    for (;;) {
      skipWhitespace();
      if (at >= value.length || value.charAt(at) === ',') {
        break;
      }
      if (value.charAt(at) !== ';') {
        return undefined;
      }
      at += 1;
      skipWhitespace();
      const name = readToken().toLowerCase();
      if (name === '') {
        return undefined;
      }
      skipWhitespace();
      let parameter = '';
      if (value.charAt(at) === '=') {
        at += 1;
        skipWhitespace();
        if (value.charAt(at) === '"') {
          const quoted = readQuoted();
          if (quoted === undefined) {
            return undefined;
          }
          parameter = quoted;
        } else {
          // RFC 8288 Appendix B reads an unquoted value up to the next ';' or ',', so legacy
          // values such as `type=application/json`, which are not tokens, still parse.
          const start = at;
          while (at < value.length && value.charAt(at) !== ';' && value.charAt(at) !== ',') {
            at += 1;
          }
          parameter = value.slice(start, at).trim();
        }
      }
      if (name === 'rel' && rel === undefined) {
        rel = parameter;
      }
    }
    const rels = (rel ?? '')
      .split(/[ \t]+/)
      .filter((type) => type !== '')
      .map((type) => type.toLowerCase());
    links.push({ target, rels });
  }
  return links;
}

/**
 * The path, relative to {@link API_BASE} and with its query, that a `Link` target names, or
 * `undefined` when the target is not a path under {@link API_BASE} on the SPA's own origin.
 *
 * Only a path-absolute reference qualifies (`/api/v1/…`, as the API sends it). An absolute URL,
 * a protocol-relative `//host/…` reference, a relative path or a backslash, which browsers
 * read as a slash, is refused. The target is then resolved the way `fetch` will resolve it,
 * dot segments included (`/api/v1/../x` is `/x`), and the resolved path must still start with
 * `/api/v1/`. A fragment is dropped, because it is never sent.
 */
function pathUnderApiBase(target: string): string | undefined {
  if (!target.startsWith('/') || target.startsWith('//') || target.includes('\\')) {
    return undefined;
  }
  let resolved: URL;
  try {
    resolved = new URL(target, LINK_RESOLUTION_ORIGIN);
  } catch {
    return undefined;
  }
  if (resolved.origin !== LINK_RESOLUTION_ORIGIN || !resolved.pathname.startsWith(`${API_BASE}/`)) {
    return undefined;
  }
  return `${resolved.pathname.slice(API_BASE.length)}${resolved.search}`;
}

/**
 * The next page a paged response's `Link` header names, relative to {@link API_BASE}, or
 * `undefined` when there is no `Link` header or no link-value with the `next` relation.
 *
 * Several `Link` headers arrive joined by `, `, which is the same list. Throws an
 * `invalid_response` {@link ApiError} with the response's status when the header does not
 * parse, when it names two different next pages, or when the next target is not a path under
 * {@link API_BASE} ({@link pathUnderApiBase}).
 */
function nextPage(response: Response): string | undefined {
  const header = response.headers.get('Link');
  if (header === null) {
    return undefined;
  }
  const invalid = (): ApiError =>
    new ApiError(response.status, 'invalid_response', INVALID_LINK_MESSAGE);
  const links = parseLinkHeader(header);
  if (links === undefined) {
    throw invalid();
  }
  let target: string | undefined;
  for (const link of links) {
    if (!link.rels.includes('next')) {
      continue;
    }
    if (target !== undefined && target !== link.target) {
      throw invalid();
    }
    target = link.target;
  }
  if (target === undefined) {
    return undefined;
  }
  const next = pathUnderApiBase(target);
  if (next === undefined) {
    throw invalid();
  }
  return next;
}

/**
 * The size of the whole list a paged response's `X-Total-Count` header states, or `undefined`
 * when the response has no such header.
 *
 * Throws an `invalid_response` {@link ApiError} with the response's status when the header is
 * present but is not a non-negative safe integer in plain decimal digits: empty, signed,
 * fractional, in exponent form, larger than `Number.MAX_SAFE_INTEGER`, or several values joined
 * by `, `. Showing a guessed total would misstate how much of the list is still unread.
 */
function totalCount(response: Response): number | undefined {
  const header = response.headers.get(TOTAL_COUNT_HEADER);
  if (header === null) {
    return undefined;
  }
  const text = header.trim();
  const total = Number(text);
  if (!TOTAL_COUNT_PATTERN.test(text) || !Number.isSafeInteger(total)) {
    throw new ApiError(response.status, 'invalid_response', INVALID_TOTAL_MESSAGE);
  }
  return total;
}

/** A successful response and its parsed body, as {@link send} resolves them. */
interface Received<T> {
  /** The 2xx response. Its body has been read; its headers can still be read. */
  response: Response;
  /** The parsed JSON body, or `undefined` for a response without one. */
  data: T;
}

/**
 * Sends one request and reads its JSON body, rejecting exactly as {@link apiFetch} documents.
 * {@link apiFetch} and {@link apiFetchPage} both call it, so they send, map errors and parse
 * identically; it also resolves the response, whose headers {@link apiFetchPage} reads.
 */
async function send<T>(path: string, init: ApiFetchInit): Promise<Received<T>> {
  const method: HttpMethod = init.method ?? 'GET';
  const { signal } = init;
  const headers: Record<string, string> = { Accept: 'application/json' };
  const request: RequestInit = { method, credentials: 'same-origin', headers };

  if (init.body !== undefined) {
    if (method === 'GET') {
      throw new ApiError(0, 'invalid_request', INVALID_REQUEST_MESSAGE);
    }
    request.body = encodeBody(init.body);
    headers['Content-Type'] = 'application/json';
  }
  if (method !== 'GET') {
    headers[CSRF_HEADER] = CSRF_VALUE;
  }
  if (signal !== undefined) {
    request.signal = signal;
  }

  let response: Response;
  try {
    // The global fetch is looked up on every call, never captured at load time, so a test's
    // vi.stubGlobal('fetch', …) takes effect.
    response = await fetch(buildUrl(path, init.query), request);
  } catch (cause) {
    if (isAbort(cause, signal)) {
      throw cause;
    }
    throw new ApiError(0, 'network_error', NETWORK_ERROR_MESSAGE, { cause });
  }

  if (!response.ok) {
    throw await errorFromResponse(response, signal);
  }
  if (NO_CONTENT_STATUSES.has(response.status)) {
    return { response, data: undefined as T };
  }
  const text = await readText(response, signal);
  if (!text.ok) {
    throw new ApiError(response.status, 'invalid_response', INVALID_RESPONSE_MESSAGE, {
      cause: text.error,
    });
  }
  if (text.value.trim() === '') {
    return { response, data: undefined as T };
  }
  const parsed = parseJson(text.value);
  if (!parsed.ok) {
    throw new ApiError(response.status, 'invalid_response', INVALID_RESPONSE_MESSAGE, {
      cause: parsed.error,
    });
  }
  return { response, data: parsed.value as T };
}

// ---------------------------------------------------------------------------------------------
// apiFetch
// ---------------------------------------------------------------------------------------------

/**
 * Calls one `/api/v1` endpoint and resolves its JSON body as `T`.
 *
 * The request is same-origin with `credentials: 'same-origin'` and `Accept: application/json`.
 * A `body` is sent as JSON with `Content-Type: application/json`. Every method other than `GET`
 * carries `X-Requested-With: ads-engine` ({@link CSRF_HEADER}, {@link CSRF_VALUE}), the login
 * and logout posts included.
 *
 * A 204 or 205 response, or a 2xx with an empty body, resolves `undefined` (typed as `T`). The
 * payload is not validated at runtime: its type comes from `types.ts`, which mirrors the API's
 * response models.
 *
 * Rejects only with {@link ApiError}, except a caller-initiated abort, which rejects with the
 * original abort error unchanged so TanStack Query treats it as a cancellation:
 *
 * - an error response with the API's envelope: its status, `error.code` and `error.message`;
 * - an error response without it: `http_<status>` and a generic message ("The server is
 *   temporarily unavailable. Try again shortly." for 502, 503 and 504);
 * - either kind with a `Retry-After` header: {@link ApiError.retryAfter} in seconds;
 * - no response at all: status 0, `network_error`;
 * - a 2xx body that cannot be read or parsed: `invalid_response`;
 * - a body that cannot be encoded, or a body on a `GET`: status 0, `invalid_request`, and
 *   nothing is sent.
 *
 * @typeParam T - The response type from `types.ts`, for example `Run` or `Decision[]`.
 * @param path - The route relative to {@link API_BASE}, starting with `/`, for example
 *   `/runs/latest`. Path segments are not encoded here; callers pass ids that need none.
 * @param init - Method, JSON body, query parameters and abort signal; all optional.
 * @returns The parsed JSON body, or `undefined` for a response without one.
 *
 * @example
 * ```ts
 * const run = await apiFetch<Run>('/runs/latest', { signal });
 * const page = await apiFetch<RecommendationsPage>('/recommendations', {
 *   query: { run_id: runId },
 * });
 * const decision = await apiFetch<Decision>('/decisions', {
 *   method: 'POST',
 *   body: { subject: { type: 'campaign', id: 7 }, patch: { status: 'paused' } },
 * });
 * ```
 */
export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const { data } = await send<T>(path, init);
  return data;
}

/**
 * Calls one page of a paged `/api/v1` list and resolves its JSON body together with the next
 * page the response names.
 *
 * The request is sent and its body read exactly as {@link apiFetch} does, with the same
 * {@link ApiError} for every failure and the same exception for a caller-initiated abort. The
 * response's RFC 8288 `Link` header is then read: comma-separated link-values, each a
 * `<target>` followed by `;` parameters, whose `rel` is a space-separated, case-insensitive
 * list of relation types, quoted or not. The target of the link-value whose relations include
 * `next` must be a path under {@link API_BASE} on the SPA's own origin, as the API sends it
 * (`</api/v1/discrepancies?state=open&limit=500&after=812>; rel="next"`).
 * {@link ApiPage.next} holds it relative to {@link API_BASE}, query included.
 *
 * Also rejects with an `invalid_response` {@link ApiError}, carrying the response's status,
 * when the `Link` header cannot be parsed, names two different next pages, or names a next
 * page that is an absolute or protocol-relative URL, a relative path or a path outside
 * {@link API_BASE}. Ending the list there would hide its remaining pages, and following such
 * a target could leave the API.
 *
 * The response's `X-Total-Count` header, which the API sends on every page of
 * `GET /discrepancies`, is read into {@link ApiPage.total}: `undefined` without the header,
 * and the same `invalid_response` rejection when it is present but not a non-negative safe
 * integer in plain digits.
 *
 * A page can hold fewer rows than the `limit` it asked for while a next page follows, for
 * example when the API leaves out a discrepancy whose subject it cannot load, so only an
 * `undefined` {@link ApiPage.next} ends a list.
 *
 * @typeParam T - The page's response type from `types.ts`, for example `Discrepancy[]`.
 * @param path - For the first page, the route relative to {@link API_BASE}, starting with `/`.
 *   For a later page, the previous page's {@link ApiPage.next}, unchanged.
 * @param init - Method, JSON body, query parameters and abort signal; all optional. A later
 *   page takes no `query`, because its `next` already carries it.
 * @returns The page's parsed body, its next page, which is `undefined` when the list is
 *   complete, and the list's total, `undefined` when the response states none.
 *
 * @example
 * ```ts
 * let page = await apiFetchPage<Discrepancy[]>('/discrepancies', {
 *   query: { state: ['open', 'awaiting_platform'], limit: 500 },
 *   signal,
 * });
 * const all = [...page.data];
 * while (page.next !== undefined) {
 *   page = await apiFetchPage<Discrepancy[]>(page.next, { signal });
 *   all.push(...page.data);
 * }
 * ```
 */
export async function apiFetchPage<T>(
  path: string,
  init: ApiFetchInit = {},
): Promise<ApiPage<T>> {
  const { response, data } = await send<T>(path, init);
  return { data, next: nextPage(response), total: totalCount(response) };
}
