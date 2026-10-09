/**
 * The first-start path, end to end (AAP 0.11.2 definition of done, "First start"; AAP 0.1.1
 * "The UI requires a login").
 *
 * The documented first start is `make init ADMIN_PASSWORD=<value>`, then `docker compose up`.
 * With no source credentials the worker resolves every source to simulated ("credentials not
 * configured"), builds and backfills the simulated datasets and runs the default analysis, and
 * the admin signs in and sees `/actions` with all five sections populated on simulated data.
 * This spec is the CI proof of that path, in one test:
 *
 * 1. **Unauthenticated redirect**: the browser starts with no session, and `/actions` sends it
 *    to the sign-in page (`src/router.tsx`, `/login?redirect=…`).
 * 2. **Sign-in through the UI**: the admin signs in with the form's "Email", "Password" and
 *    "Sign in" (`features/auth/LoginPage.tsx`) and lands on `/actions`, with the signed-in email
 *    and "Sign out" in the top bar. A rejected sign-in fails this step with the message the page
 *    shows, so a wrong password is reported as such rather than as a later timeout.
 * 3. **First default run**: the spec waits until `GET /api/v1/runs/latest` answers 200, then
 *    until the page has replaced its "No analysis run yet" notice with the run by itself
 *    (`features/actions/ActionsPage.tsx` polls), reloading once only if it has not.
 * 4. **Five populated sections**: the sections render in the user's order (`section-keywords`,
 *    `section-campaigns`, `section-budget`, `section-creative`, `section-white_papers`), titled as
 *    the API titles them, and every one shows at least one action: its "Sections" navigation
 *    link counts one or more items and it shows no "No actions in this section" notice.
 * 5. **Simulated labelling**: at least one source chip in the status bar reads "Simulated".
 *
 * Harness (`playwright.config.ts`): the spec runs in the `chromium` project, whose specs normally
 * start from the session `auth.setup.ts` saved. This file alone overrides that with an empty
 * storage state, because the first-start sign-in is what it asserts. The API accepts 5 sign-in
 * attempts per minute per IP, so the test signs in exactly once per attempt; with the setup
 * project and one CI retry a run stays at four sign-ins at most.
 *
 * Nothing the seeded world contains is written here: titles, counts, window lengths and entity
 * names all come from the API or the page. The only literals are the fixed UI texts and the
 * section order the user specified (AAP 0.5.5). Earlier specs in the same run may have snoozed an
 * item or recorded a decision, so each section is only required to show at least one action.
 *
 * @example
 * ```sh
 * make init ADMIN_PASSWORD=pw && docker compose up -d
 * cd web && E2E_ADMIN_PASSWORD=pw npx playwright test tests/e2e/first-start.spec.ts
 * ```
 *
 * @packageDocumentation
 */
import { errors, expect, test } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

import type { Me, RecommendationsPage, Run, Section, SectionKey } from '../../src/api/types';

// ---------------------------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------------------------

/** Whole-test budget: on a fresh stack the first default run alone may take minutes. */
const SPEC_TIMEOUT_MS = 12 * 60_000;

/**
 * How long the worker may take on first start to build and backfill the simulated datasets of
 * all three sources (180 days) and the companion CRM dataset, and finish the first default run.
 */
const FIRST_RUN_TIMEOUT_MS = 10 * 60_000;

/** Poll cadence while waiting for the first default run: soon at first, then every 15 s. */
const FIRST_RUN_INTERVALS_MS = [5_000, 10_000, 15_000];

/** Bound on one `/runs/latest` probe, so a hung connection never stalls the poll. */
const PROBE_TIMEOUT_MS = 10_000;

/**
 * How long the open page may take to swap its first-run notice for the run by itself. The page
 * polls every 15 s, so this covers several polls.
 */
const SELF_REFRESH_TIMEOUT_MS = 60_000;

/** How long the sign-in may take to land on `/actions`, or to show why it was rejected. */
const SIGN_IN_TIMEOUT_MS = 30_000;

/** How long the signed-in Actions page may take to leave its loading state. */
const PAGE_STATE_TIMEOUT_MS = 30_000;

/** Bound on one locator check inside a retried block, so a retry starts promptly. */
const READ_TIMEOUT_MS = 10_000;

/**
 * Retry budget of the section checks: a rerun coalesced from a sync or an earlier spec's write
 * may be replacing the run while they read it, so each attempt reloads and reads again.
 */
const SETTLE = { timeout: 120_000, intervals: [5_000, 10_000] };

// ---------------------------------------------------------------------------------------------
// Paths and fixed UI texts
// ---------------------------------------------------------------------------------------------

/** The signed-in user; 401 without a valid session cookie. */
const ME_PATH = '/api/v1/auth/me';

/** The latest succeeded default run; 404 `not_found` until the first one has succeeded. */
const RUNS_LATEST_PATH = '/api/v1/runs/latest';

/** The five sections of the latest default run, with the current-state overlay. */
const RECOMMENDATIONS_PATH = '/api/v1/recommendations';

/** The action list, the default page after sign-in. */
const ACTIONS_PATH = '/actions';

/** The sign-in page an unauthenticated visit is redirected to. */
const LOGIN_PATH = '/login';

/** The default admin login, the `APP_ADMIN_EMAIL` default in `.env.example`. */
const DEFAULT_ADMIN_EMAIL = 'admin@example.com';

/** The sign-in form's field labels and submit button (`LoginPage.tsx`). */
const EMAIL_LABEL = 'Email';
const PASSWORD_LABEL = 'Password';
const SIGN_IN_LABEL = 'Sign in';

/** The top bar's sign-out button (`src/router.tsx`). */
const SIGN_OUT_LABEL = 'Sign out';

/** The Actions page's level-1 heading, rendered in every state of the page. */
const ACTIONS_TITLE = 'Actions';

/** The Actions page's notice before the first default run has succeeded. */
const FIRST_RUN_NOTICE = 'No analysis run yet';

/** The `/actions` section navigation landmark (`SectionNav.tsx`). */
const SECTION_NAV_LABEL = 'Sections';

/** The empty-state title of a section with no card to show (`SectionFrame.tsx`). */
const EMPTY_SECTION_TITLE = 'No actions in this section';

/** The status bar's list of source chips (`SourceChips.tsx`). */
const SOURCE_CHIPS_LABEL = 'Data sources';

/** The mode chip of a simulated source. */
const SIMULATED_CHIP = 'Simulated';

/** The five sections in the order the user specified (AAP 0.5.5). */
const SECTION_KEYS: readonly SectionKey[] = [
  'keywords',
  'campaigns',
  'budget',
  'creative',
  'white_papers',
];

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** The admin credentials the stack was initialised with. */
interface AdminCredentials {
  /** The login email, as entered in the form. */
  email: string;
  /** The password given to `make init`. */
  password: string;
}

/**
 * Reads the admin credentials from the environment. `||` rather than `??`: a blank variable
 * counts as unset, as everywhere in the project.
 *
 * @returns The email (`admin@example.com` by default) and the required password.
 * @throws Error when `E2E_ADMIN_PASSWORD` is unset or blank.
 */
function adminCredentials(): AdminCredentials {
  const email = process.env.E2E_ADMIN_EMAIL || DEFAULT_ADMIN_EMAIL;
  const password = process.env.E2E_ADMIN_PASSWORD;
  if (password === undefined || password.trim() === '') {
    throw new Error(
      'E2E_ADMIN_PASSWORD must be set (the password passed to make init ADMIN_PASSWORD=…)',
    );
  }
  return { email, password };
}

/**
 * Reads one API resource with the browser's session and returns its JSON body. `page.request`
 * shares the browser context's cookies, so after sign-in the read is authenticated by the
 * session the form created. A status other than 200 fails the calling check.
 *
 * @param page - The test's page.
 * @param path - The API path, relative to `baseURL`.
 * @returns The parsed body, typed as the caller expects it.
 */
async function getJson<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(path);
  expect(response.status(), `GET ${path} answers 200`).toBe(200);
  return (await response.json()) as T;
}

/**
 * One `/runs/latest` probe, reduced to a value `expect.poll` can compare and print: the HTTP
 * status, or `'unreachable'` when the stack does not answer.
 *
 * @param page - The test's page.
 * @returns `200` once the first default run has succeeded.
 */
async function probeLatestRun(page: Page): Promise<number | string> {
  try {
    const response = await page.request.get(RUNS_LATEST_PATH, { timeout: PROBE_TIMEOUT_MS });
    return response.status();
  } catch {
    return 'unreachable';
  }
}

/**
 * Enters the password without `fill`, so it is never logged: Playwright renders a fill's value
 * into the step title, which the HTML report shows. Setting the value through the native setter
 * and dispatching `input` drives the same React `onChange` as typing. Traces are off for this
 * file, so the evaluated argument is recorded nowhere either.
 *
 * @param field - The password field.
 * @param password - The password to enter.
 */
async function enterPassword(field: Locator, password: string): Promise<void> {
  await expect(field).toBeEditable();
  await field.evaluate((element, value) => {
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
  await expect(field, 'the Password field holds the entered value').not.toHaveValue('');
}

/**
 * The text that describes a form field: its hint and error messages, which `TextField`
 * references through `aria-describedby`. After a rejected sign-in it is the page's message,
 * such as "Email or password is incorrect"; it never contains the password.
 *
 * @param field - The field.
 * @returns The description, or an empty string when the field has none.
 */
async function fieldDescription(field: Locator): Promise<string> {
  return field.evaluate((element) =>
    (element.getAttribute('aria-describedby') ?? '')
      .split(/\s+/u)
      .filter((id) => id !== '')
      .map((id) => document.getElementById(id)?.textContent?.trim() ?? '')
      .filter((text) => text !== '')
      .join(' '),
  );
}

/**
 * Waits for `locator` to leave the page, and reports whether it did in time. Only a timeout is
 * reported as `false`; any other failure, such as a closed page, is rethrown.
 *
 * @param locator - The element expected to go.
 * @param timeout - How long to wait, in milliseconds.
 * @returns `true` once no element matches, `false` when one still matches after `timeout`.
 */
async function leavesPage(locator: Locator, timeout: number): Promise<boolean> {
  try {
    await locator.waitFor({ state: 'detached', timeout });
    return true;
  } catch (error) {
    if (error instanceof errors.TimeoutError) {
      return false;
    }
    throw error;
  }
}

/** The page's single level-1 heading "Actions". */
function actionsTitle(page: Page): Locator {
  return page.getByRole('heading', { level: 1, name: ACTIONS_TITLE, exact: true });
}

/** The "No analysis run yet" notice the Actions page shows before the first default run. */
function firstRunNotice(page: Page): Locator {
  return page.getByText(FIRST_RUN_NOTICE, { exact: true });
}

/** The "Sections" navigation, rendered only once a run's sections have loaded. */
function sectionNav(page: Page): Locator {
  return page.getByRole('navigation', { name: SECTION_NAV_LABEL, exact: true });
}

/** The anchor id of a section's region, `section-{key}` (`SectionFrame.tsx`). */
function sectionAnchor(key: SectionKey): string {
  return `section-${key}`;
}

/**
 * How many items the API's section shows now: every item, held ones included, that feedback
 * given since the run does not hide. It is what the section's navigation link counts.
 *
 * @param section - One section of `GET /recommendations`.
 * @returns The number of items the section renders.
 */
function shownItemCount(section: Section): number {
  return section.groups
    .flatMap((group) => group.items)
    .filter((item) => item.current.hidden_by === null).length;
}

/**
 * The item count a section's navigation link shows: the trailing number of the link
 * `a[href="#section-{key}"]`, whose text is "{title} {count}" (`SideNav.tsx`).
 *
 * @param page - The test's page.
 * @param key - The section.
 * @returns The count the link shows.
 * @throws Error when the link shows no count.
 */
async function sectionNavCount(page: Page, key: SectionKey): Promise<number> {
  const link = sectionNav(page).locator(`a[href="#${sectionAnchor(key)}"]`);
  const text = ((await link.textContent({ timeout: READ_TIMEOUT_MS })) ?? '').trim();
  const digits = /(\d[\d,]*)$/u.exec(text)?.[1];
  if (digits === undefined) {
    throw new Error(`The "${key}" section link "${text}" shows no item count`);
  }
  return Number.parseInt(digits.replaceAll(',', ''), 10);
}

// ---------------------------------------------------------------------------------------------
// Test
// ---------------------------------------------------------------------------------------------

// Start with no session, because the first-start sign-in is what this file asserts. Traces stay
// off in every run, including local runs, where playwright.config.ts otherwise retains a trace
// on failure: a trace records every action's arguments, the password among them, and a retained
// trace is copied into the HTML report. Screenshots on failure still apply.
test.use({ storageState: { cookies: [], origins: [] }, trace: 'off' });

test('first start: sign in and see five populated sections on simulated data', async ({
  page,
}) => {
  test.setTimeout(SPEC_TIMEOUT_MS);
  const { email, password } = adminCredentials();

  await test.step('an unauthenticated visit to /actions is sent to the sign-in page', async () => {
    const me = await page.request.get(ME_PATH);
    expect(me.status(), `the browser starts without a session (${ME_PATH} answers 401)`).toBe(401);

    await page.goto(ACTIONS_PATH);
    await expect(page).toHaveURL(/\/login/);
    await expect(page, `the redirect opens ${LOGIN_PATH}`).toHaveURL(
      (url) => url.pathname === LOGIN_PATH,
    );
    await expect(page.getByRole('button', { name: SIGN_IN_LABEL, exact: true })).toBeVisible();
  });

  await test.step('the admin signs in through the form and lands on /actions', async () => {
    // Both fields are required, so their visible labels read "Email *" and "Password *"; the
    // accessible name skips the aria-hidden asterisk, so each is found by role and exact name.
    await page.getByRole('textbox', { name: EMAIL_LABEL, exact: true }).fill(email);
    const passwordField = page.getByRole('textbox', { name: PASSWORD_LABEL, exact: true });
    await enterPassword(passwordField, password);
    await page.getByRole('button', { name: SIGN_IN_LABEL, exact: true }).click();

    // The sign-in settles either on /actions or back on the form with the password field
    // marked invalid and described by the page's message.
    const rejected = passwordField.and(page.locator('[aria-invalid="true"]'));
    await expect(
      actionsTitle(page).or(rejected),
      'sign-in opens /actions or shows why it was rejected',
    ).toBeVisible({ timeout: SIGN_IN_TIMEOUT_MS });
    if (await rejected.isVisible()) {
      throw new Error(
        `Sign-in was rejected: "${await fieldDescription(rejected)}". Check that ` +
          'E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD match the admin from make init, and that the ' +
          '5-per-minute login limit was not exceeded.',
      );
    }
    await page.waitForURL('**/actions**');
    await expect(page).toHaveURL((url) => url.pathname === ACTIONS_PATH);

    // Every state of the page renders the heading; before the first run it shows the first-run
    // notice, afterwards the run's section navigation.
    await expect(actionsTitle(page)).toBeVisible();
    await expect(
      firstRunNotice(page).or(sectionNav(page)),
      'the Actions page shows the first-run notice or a run',
    ).toBeVisible({ timeout: PAGE_STATE_TIMEOUT_MS });

    // The session the form created is the one the API serves, and the top bar names it.
    const me = await getJson<Me>(page, ME_PATH);
    expect(me.email, `${ME_PATH} names the admin who signed in`).toBe(email.trim().toLowerCase());
    const topBar = page.getByRole('banner');
    await expect(topBar, 'the top bar shows the signed-in email').toContainText(me.email);
    await expect(topBar.getByRole('button', { name: SIGN_OUT_LABEL, exact: true })).toBeVisible();
  });

  await test.step('the worker completes the first default analysis run', async () => {
    await expect
      .poll(() => probeLatestRun(page), {
        message: `the first default analysis run has succeeded (${RUNS_LATEST_PATH} answers 200)`,
        timeout: FIRST_RUN_TIMEOUT_MS,
        intervals: FIRST_RUN_INTERVALS_MS,
      })
      .toBe(200);
    const run = await getJson<Run>(page, RUNS_LATEST_PATH);
    expect(run.kind, `${RUNS_LATEST_PATH} is a default run`).toBe('default');
    expect(run.status, `run ${String(run.id)} succeeded`).toBe('succeeded');

    // The open page polls by itself; a reload is the fallback, used at most once.
    const notice = firstRunNotice(page);
    if (!(await leavesPage(notice, SELF_REFRESH_TIMEOUT_MS))) {
      const waitedSeconds = String(SELF_REFRESH_TIMEOUT_MS / 1000);
      test.info().annotations.push({
        type: 'first-run refresh',
        description:
          `"${FIRST_RUN_NOTICE}" was still shown ${waitedSeconds} s after the run succeeded; ` +
          'the page was reloaded once',
      });
      await page.reload();
      await expect(notice, `"${FIRST_RUN_NOTICE}" is gone after a reload`).toHaveCount(0, {
        timeout: SELF_REFRESH_TIMEOUT_MS,
      });
    }
    await expect(actionsTitle(page)).toBeVisible();
    await expect(sectionNav(page), 'the page shows the run').toBeVisible({
      timeout: PAGE_STATE_TIMEOUT_MS,
    });
  });

  await test.step('/actions shows the five sections in order, each with actions', async () => {
    await expect(async () => {
      await page.reload();
      await expect(sectionNav(page)).toBeVisible({ timeout: READ_TIMEOUT_MS });

      const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
      const runLabel = `run ${String(recommendations.run.id)}`;
      expect(recommendations.run.kind, `${RECOMMENDATIONS_PATH} serves a default run`).toBe(
        'default',
      );
      expect(
        recommendations.sections.map((section) => section.key),
        'the API returns the five sections in the fixed order',
      ).toEqual(SECTION_KEYS);

      // Each top-level section region with its heading, in DOM order. Group regions inside a
      // section are anchored `group-…`, so the prefix selects the five sections only.
      const rendered = await page.locator('section[id^="section-"]').evaluateAll((elements) =>
        elements.map((element) => {
          const headingId = element.getAttribute('aria-labelledby');
          const heading = headingId === null ? null : document.getElementById(headingId);
          return {
            id: element.id,
            level: heading?.tagName.toLowerCase() ?? '',
            title: heading?.textContent?.trim() ?? '',
          };
        }),
      );
      expect(
        rendered.map((section) => section.id),
        'the page renders the five sections in the fixed order',
      ).toEqual(SECTION_KEYS.map(sectionAnchor));
      expect(
        rendered.map((section) => section.level),
        'each section is titled by an h2',
      ).toEqual(SECTION_KEYS.map(() => 'h2'));
      expect(
        rendered.map((section) => section.title),
        'each section is titled as the API titles it',
      ).toEqual(recommendations.sections.map((section) => section.title.trim()));

      for (const section of recommendations.sections) {
        const title = section.title.trim();
        const region = page.locator(`#${sectionAnchor(section.key)}`);
        expect(
          shownItemCount(section),
          `the "${title}" section of ${runLabel} has at least one action to show`,
        ).toBeGreaterThanOrEqual(1);
        // Scoped by id as well: a group inside a section may carry the section's own title.
        await expect(
          page.getByRole('region', { name: title, exact: true }).and(region),
          `the "${title}" section is a region named by its title`,
        ).toBeVisible({ timeout: READ_TIMEOUT_MS });
        expect(
          await sectionNavCount(page, section.key),
          `the "${title}" link in the "${SECTION_NAV_LABEL}" navigation counts at least one item`,
        ).toBeGreaterThanOrEqual(1);
        await expect(
          region.getByText(EMPTY_SECTION_TITLE, { exact: true }),
          `the "${title}" section is not empty`,
        ).toHaveCount(0, { timeout: READ_TIMEOUT_MS });
      }
    }).toPass(SETTLE);
  });

  await test.step('the status bar labels the simulated sources', async () => {
    const chips = page.getByRole('list', { name: SOURCE_CHIPS_LABEL, exact: true });
    await expect(chips, 'the status bar lists the data sources').toBeVisible();
    const simulated = chips.getByText(SIMULATED_CHIP, { exact: true });
    await expect(simulated.first(), `a source chip reads "${SIMULATED_CHIP}"`).toBeVisible();
    expect(
      await simulated.count(),
      `at least one source chip reads "${SIMULATED_CHIP}"`,
    ).toBeGreaterThanOrEqual(1);
  });
});
