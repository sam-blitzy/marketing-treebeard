/**
 * Playwright end-to-end configuration for the Ads Action Engine web client.
 *
 * The suite drives the real stack in a single Chromium browser: nginx serving the SPA and
 * proxying `/api` to the API, with the worker producing simulated data. The stack is started
 * beforehand by `make e2e` or the CI e2e job (`make init`, then `docker compose up -d`), so this
 * file configures no `webServer`. Run it with `npm run e2e` (`playwright test`) from `web/`.
 *
 * Environment variables:
 * - `E2E_BASE_URL`: origin of the running stack. Defaults to `http://localhost:8080`, the
 *   Compose `web` service. `PLAYWRIGHT_BASE_URL` is accepted as a fallback alias (see below).
 * - `E2E_ADMIN_EMAIL`: admin login. Defaults to `admin@example.com`, the `APP_ADMIN_EMAIL`
 *   default. The default is written back into `process.env`, so `auth.setup.ts` and the specs
 *   read one value; worker processes inherit the runner's environment.
 * - `E2E_ADMIN_PASSWORD`: the `ADMIN_PASSWORD` given to `make init`. Required, no default:
 *   loading this config fails at once without it, instead of every test failing with a 401.
 * - `CI`: when set, `test.only` is forbidden, failed tests are retried once, no trace is
 *   recorded (a trace would carry the session cookie into the uploaded artifact), and the HTML
 *   report is written to `playwright-report/` for upload.
 *
 * Login rate limit: the API accepts 5 login attempts per minute per IP and answers the 6th with
 * 429. Logging in per test would trip that limit, especially with retries, so the `setup`
 * project (`tests/e2e/auth.setup.ts`) signs in once through the UI and saves the session to
 * {@link STORAGE_STATE}, and every spec in the `chromium` project starts from that state.
 * `first-start.spec.ts` alone overrides it with an empty state, because it asserts the
 * first-start login path itself. A run therefore makes at most two UI logins, and at most four
 * with one retry of each, which stays under the limit.
 *
 * Specs rely on roles and accessible names rather than test ids, so the default
 * `testIdAttribute` is kept.
 *
 * @example
 * ```sh
 * make init ADMIN_PASSWORD=pw && docker compose up -d
 * cd web && E2E_ADMIN_PASSWORD=pw npm run e2e
 * ```
 */
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';

/**
 * Origin the suite targets. `E2E_BASE_URL` is the project convention and always wins.
 * `PLAYWRIGHT_BASE_URL` is accepted only as a fallback, because the host-level e2e gate passes
 * the per-checkout stack origin under that name; ignoring it would aim the suite at a different
 * stack on the default port.
 *
 * `||` rather than `??` is deliberate here and for the email below: an empty variable counts as
 * unset and takes the default, as blank values do everywhere else in the project.
 */
const baseURL =
  process.env.E2E_BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8080';

process.env.E2E_ADMIN_EMAIL ||= 'admin@example.com';

if (!process.env.E2E_ADMIN_PASSWORD?.trim()) {
  throw new Error('E2E_ADMIN_PASSWORD is required (the ADMIN_PASSWORD given to make init)');
}

/**
 * Session storage state written by `tests/e2e/auth.setup.ts` and loaded by every spec in the
 * `chromium` project. It is `test-results/.auth/admin.json` resolved against this file's
 * directory rather than the working directory, so it always lands inside `outputDir`, whichever
 * directory Playwright is launched from. That directory is cleared at the start of every run, so
 * a session never outlives its run; it is gitignored and excluded from the CI report artifact.
 */
export const STORAGE_STATE = fileURLToPath(
  new URL('./test-results/.auth/admin.json', import.meta.url),
);

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: 'test-results',

  // decisions.spec.ts records decisions and resolves discrepancies in the one shared stack,
  // so specs must never run concurrently.
  fullyParallel: false,
  workers: 1,

  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,

  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]]
    : [['list'], ['html', { open: 'never' }]],

  // On first start the worker builds and backfills the simulated datasets and runs the default
  // analysis before /actions is populated; specs that wait for it extend their own timeouts
  // with test.setTimeout or expect.poll.
  timeout: 120_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL,
    // A trace stores every request's headers, the session cookie included, and CI uploads
    // test-results/ and playwright-report/ (which copies traces) on failure, so CI records none.
    trace: process.env.CI ? 'off' : 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    {
      name: 'chromium',
      testMatch: /.*\.spec\.ts/,
      dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'], storageState: STORAGE_STATE },
    },
  ],
});
