/**
 * One-time UI sign-in for the end-to-end suite: the `setup` project of `playwright.config.ts`.
 *
 * The test waits for the stack to answer `GET /api/v1/healthz`, signs in as the single admin
 * through the real `/login` form, confirms that the session reaches `/actions` and that the API
 * accepts its cookie, and saves the browser storage state to {@link STORAGE_STATE}
 * (`web/test-results/.auth/admin.json`). Every spec in the `chromium` project depends on this
 * project and starts from that state, already signed in.
 *
 * Why one sign-in: the API accepts 5 login attempts per minute per client IP and answers the
 * 6th with 429 `rate_limited`. Signing in per test would trip that limit, so only this file and
 * `first-start.spec.ts` (which asserts the first-start sign-in itself) ever use the login form.
 * A run therefore stays well under the limit, including a retry and a second run within the same
 * minute.
 *
 * The test reads and writes no application data: it never waits for an analysis run, which the
 * specs that need one wait for themselves. Sign-in works as soon as the `migrate` service has run
 * `ads-engine ensure-admin`.
 *
 * Environment variables (credentials come only from the environment and are never logged):
 * - `E2E_ADMIN_PASSWORD`: the `ADMIN_PASSWORD` given to `make init`. Required.
 * - `E2E_ADMIN_EMAIL`: the admin login, `admin@example.com` (the `APP_ADMIN_EMAIL` default) when
 *   unset or blank.
 * - `E2E_BASE_URL`: the stack origin, read by `playwright.config.ts` as `baseURL`.
 *
 * @example
 * ```sh
 * make init ADMIN_PASSWORD=pw && docker compose up -d
 * cd web && E2E_ADMIN_PASSWORD=pw npm run e2e -- --project=setup
 * ```
 *
 * @packageDocumentation
 */
import { test as setup, expect } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';

import { STORAGE_STATE } from '../../playwright.config';
import type { Health } from '../../src/api/types';

/** Whole-test budget: a stack started moments ago may still be migrating and booting. */
const SETUP_TIMEOUT_MS = 180_000;

/** How long the stack may take to answer the health check with `{"status":"ok"}`. */
const STACK_READY_TIMEOUT_MS = 150_000;

/** Poll cadence while the stack starts: quick at first, then every 5 s. */
const STACK_READY_INTERVALS_MS = [1_000, 2_000, 5_000];

/** Bound on one health probe, so a hung connection never stalls the poll. */
const HEALTH_PROBE_TIMEOUT_MS = 10_000;

/** How long the sign-in may take to land on `/actions` once the form is submitted. */
const SIGN_IN_TIMEOUT_MS = 30_000;

/** The unauthenticated liveness and database check (`backend/.../api/routes/health.py`). */
const HEALTH_PATH = '/api/v1/healthz';

/** The signed-in user; answers 200 only for a valid session cookie. */
const ME_PATH = '/api/v1/auth/me';

/** The page a successful sign-in opens (`src/router.tsx`, the default after login). */
const ACTIONS_PATH = '/actions';

/** The default admin login, the `APP_ADMIN_EMAIL` default in `.env.example`. */
const DEFAULT_ADMIN_EMAIL = 'admin@example.com';

// Traces stay off for this file in every run, including local runs, where playwright.config.ts
// otherwise retains a trace on failure: a trace records every action's arguments, the password
// among them, and a retained trace is copied into the HTML report.
setup.use({ trace: 'off' });

/**
 * One health probe, reduced to a value `expect.poll` can compare and print: the body's `status`
 * on a 2xx, the HTTP status code otherwise, and a short description when the stack does not
 * answer or answers with something other than JSON (for example the SPA's `index.html` when
 * `/api` is not proxied).
 *
 * @param request - The suite's API request context, bound to `baseURL`.
 * @returns `'ok'` once the API is serving and its database answered.
 */
async function probeHealth(request: APIRequestContext): Promise<string | number> {
  let response;
  try {
    response = await request.get(HEALTH_PATH, { timeout: HEALTH_PROBE_TIMEOUT_MS });
  } catch {
    return 'unreachable';
  }
  if (!response.ok()) {
    return response.status();
  }
  try {
    const body = (await response.json()) as Partial<Health>;
    return body.status ?? 'no status in response';
  } catch {
    return 'non-JSON response';
  }
}

setup('authenticate as admin', async ({ page, request }) => {
  setup.setTimeout(SETUP_TIMEOUT_MS);

  // `||` rather than `??`: a blank variable counts as unset, as everywhere in the project.
  const email = process.env.E2E_ADMIN_EMAIL || DEFAULT_ADMIN_EMAIL;
  const password = process.env.E2E_ADMIN_PASSWORD;
  if (password === undefined || password.trim() === '') {
    throw new Error(
      'E2E_ADMIN_PASSWORD must be set (the password passed to make init ADMIN_PASSWORD=…)',
    );
  }

  await expect
    .poll(() => probeHealth(request), {
      message: `the stack at baseURL answers ${HEALTH_PATH} with {"status":"ok"}`,
      timeout: STACK_READY_TIMEOUT_MS,
      intervals: STACK_READY_INTERVALS_MS,
    })
    .toBe('ok');

  await page.goto('/login');

  // Both fields are required, so their visible label text reads "Email *" and "Password *" and an
  // exact `getByLabel` would miss them. The accessible name skips the `aria-hidden` asterisk, so
  // the fields are found by role and exact name, as `TextField` documents for required fields.
  await page.getByRole('textbox', { name: 'Email', exact: true }).fill(email);

  // The password is entered without `fill`, so it is never logged: Playwright renders a fill's
  // value into the step title (`Fill "<value>"`), which the HTML report that CI uploads shows.
  // Setting the value through the native setter and dispatching `input` drives the same React
  // `onChange` as typing, while the step reads only "Evaluate"; with traces off the argument is
  // recorded nowhere. The check after it never prints the value.
  const passwordField = page.getByRole('textbox', { name: 'Password', exact: true });
  await expect(passwordField).toBeEditable();
  await passwordField.evaluate((element, value) => {
    if (!(element instanceof HTMLInputElement)) {
      throw new Error('The Password field is not an <input> element');
    }
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setValue === undefined) {
      throw new Error('HTMLInputElement.value has no setter');
    }
    setValue.call(element, value);
    element.dispatchEvent(new Event('input', { bubbles: true }));
  }, password);
  await expect(passwordField, 'the Password field holds the entered value').not.toHaveValue('');

  await page.getByRole('button', { name: 'Sign in', exact: true }).click();

  // The message is the step title on success and the failure text otherwise, so it states the
  // expectation and the two usual causes of a miss.
  await expect(
    page,
    'sign-in opens /actions (otherwise check that E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD match ' +
      'the admin from make init, and that the 5-per-minute login limit was not exceeded)',
  ).toHaveURL((url) => url.pathname === ACTIONS_PATH, { timeout: SIGN_IN_TIMEOUT_MS });

  // Every state of the Actions page, loading and first start included, renders this heading,
  // so the check never waits for an analysis run. Its name also tells it apart from the
  // sign-in page's own level-1 heading.
  await expect(page.getByRole('heading', { level: 1, name: 'Actions', exact: true })).toBeVisible();

  // `page.request` shares the browser context's cookies, so this read proves that the state
  // about to be saved carries a session the API accepts. It is a GET, not a login attempt.
  const me = await page.request.get(ME_PATH);
  expect(me.status(), `${ME_PATH} accepts the new session`).toBe(200);

  await page.context().storageState({ path: STORAGE_STATE });
});
