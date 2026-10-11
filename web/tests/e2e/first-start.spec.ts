/**
 * The first-start path, end to end (AAP 0.11.2 definition of done, "First start"; AAP 0.1.1
 * "The UI requires a login").
 *
 * The documented first start is `make init ADMIN_PASSWORD=<value>`, then `docker compose up`.
 * With no source credentials the worker resolves every source to simulated ("credentials not
 * configured"), builds and backfills the simulated datasets and runs the default analysis, and
 * the admin signs in and sees `/actions` with all five sections populated on simulated data.
 * This spec is the CI proof of that path. Its first test walks the path itself:
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
 *    link counts one or more items, it shows no empty-state notice (`.ds-emptystate`), and it
 *    renders an action that matches the same `GET /recommendations` read. Where only held
 *    actions exist, the "Held until discrepancies are resolved" list is opened first.
 *    - Keywords, Campaigns, Budget and Copy/creative: the first visible card
 *      (`<article id="item-{id}">`) is an item of that section that feedback does not hide,
 *      headed with the item's action and showing its entity line, "{name} · {Source}".
 *    - White papers: the first visible asset row names an asset of the section that feedback
 *      does not hide, its Action cell reads that asset's action and every cell holds a value;
 *      its "Show details" toggle, named "Show details for {asset} ({currency})", opens the
 *      "Per creative: {asset}" table with at least one creative row.
 * 5. **Simulated labelling**: at least one source chip in the status bar reads "Simulated".
 *
 * Its second test, declared after the first so the first-start sign-in is never preceded by a
 * refused one, proves that a wrong password signs nobody in (CWE-20 browser coverage). From a
 * signed-out browser on `/login`, it enters the admin email and a password generated at run time
 * (`wrong-{UUID}`, checked to differ from `E2E_ADMIN_PASSWORD`). `POST /api/v1/auth/login`
 * answers 401 `invalid_credentials`, and the page stays on `/login`. The page shows "Email or
 * password is incorrect" as the form's banner and as the Password field's error, with the field
 * marked invalid, cleared and focused, and the email kept. There is no "Sign out"; `/auth/me`
 * still answers 401, and a new page of the same browser opening `/actions` is sent to `/login`.
 * The correct password, entered on the same form, then opens `/actions` with a session
 * `/auth/me` names. The test never signs out, because sign-out revokes every session, the one
 * `auth.setup.ts` saved for the other specs included.
 *
 * Harness (`playwright.config.ts`): the spec runs in the `chromium` project, whose specs normally
 * start from the session `auth.setup.ts` saved. This file alone overrides that with an empty
 * storage state, because the first-start sign-in is what it asserts. The API counts every
 * sign-in attempt per IP, refused ones included, and answers the 6th within 60 s with 429. The
 * first-start test makes 1 attempt and the wrong-password test 2, so with the setup project's 1
 * a run without retries makes 4. A CI retry repeats a failed test's attempts (up to 8 in one
 * run), and a second run started within a minute adds its own, so either can get 429: wait a
 * minute before running the suite again. A sign-in answered 429 fails with that cause.
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
import { randomUUID } from 'node:crypto';
import { errors, expect, test } from '@playwright/test';
import type { Locator, Page, Response } from '@playwright/test';

import type {
  ErrorResponse,
  Item,
  Me,
  RecommendationsPage,
  Run,
  Section,
  SectionKey,
  SourceName,
} from '../../src/api/types';

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

/** The sign-in the form posts; each call is one attempt against the 5-per-minute limit. */
const LOGIN_API_PATH = '/api/v1/auth/login';

/** The error code of a refused sign-in, for an unknown email and a wrong password alike. */
const INVALID_CREDENTIALS_CODE = 'invalid_credentials';

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

/**
 * The sign-in page's message for 401 `invalid_credentials` (`LoginPage.tsx`): it heads the form
 * and describes the Password field.
 */
const INVALID_CREDENTIALS_MESSAGE = 'Email or password is incorrect';

/** The start of the wrong password generated at run time; the rest is a random UUID. */
const WRONG_PASSWORD_PREFIX = 'wrong-';

/** The Actions page's level-1 heading, rendered in every state of the page. */
const ACTIONS_TITLE = 'Actions';

/** The Actions page's notice before the first default run has succeeded. */
const FIRST_RUN_NOTICE = 'No analysis run yet';

/** The `/actions` section navigation landmark (`SectionNav.tsx`). */
const SECTION_NAV_LABEL = 'Sections';

/**
 * The empty state a section shows in place of its cards (`SectionFrame.tsx`), the root of the
 * design-system `EmptyState`. Its title is the reason the section is empty, read from the run,
 * so the spec finds it by the wrapper's class rather than by a fixed text. An empty Keywords
 * sub-list's own empty state mounts only once its tab is selected, and a populated section opens
 * on a tab with cards, so in a populated section none is present.
 */
const EMPTY_STATE_SELECTOR = '.ds-emptystate';

/** The status bar's list of source chips (`SourceChips.tsx`). */
const SOURCE_CHIPS_LABEL = 'Data sources';

/** The mode chip of a simulated source. */
const SIMULATED_CHIP = 'Simulated';

/**
 * A recommendation card: the `<article id="item-{id}">` `RecommendationCard.tsx` renders, named
 * by its heading, whose text is the item's action (`Card.tsx`).
 */
const CARD_SELECTOR = 'article[id^="item-"]';

/** The toggle of a collapsed held list, "Held until discrepancies are resolved ({count})". */
const HELD_DISCLOSURE = /^Held until discrepancies are resolved/u;

/**
 * A white-paper row's toggle, matched by its accessible name: "Show details for {asset}
 * ({scope})" while collapsed, "Hide details for …" while expanded, the scope being the cohort's
 * currency and, outside its table of visible rows, the table's qualifier (`DataTable.tsx`,
 * `rowLabelOf` in `WhitePapersSection.tsx`).
 */
const ASSET_TOGGLE = /^(?:Show|Hide) details for /u;

/**
 * A white-paper toggle's name taken apart: the asset (group 1), then the scope in its closing
 * parentheses (group 2), which holds none of its own.
 */
const ASSET_TOGGLE_NAME = /^(?:Show|Hide) details for (.+) \(([^()]+)\)$/u;

/** The start of an expanded white-paper row's table caption, "Per creative: {asset}". */
const PER_CREATIVE_CAPTION_PREFIX = 'Per creative: ';

/** The badge a white-paper row computed from replaced data shows beside its action. */
const HISTORICAL_LABEL = 'Computed from replaced data';

/**
 * The cells of a white-paper row besides its Asset row header: the "Show details" toggle, the
 * nine figure columns between Asset and Action (spend through cost per meeting), and Action.
 */
const ASSET_ROW_CELL_COUNT = 1 + 9 + 1;

/** The separator of a card header's entity line, "{entity name} · {Source}". */
const ENTITY_LINE_SEPARATOR = ' · ';

/** The display name of each source (`sourceLabel` in `src/lib/format.ts`). */
const SOURCE_LABELS: Readonly<Record<SourceName, string>> = {
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
};

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
 * Clicks "Sign in" and returns the answer to the `POST /api/v1/auth/login` the click sends. The
 * wait is armed before the click, so a quick answer is never missed. A 429 fails at once with
 * its cause, so a used-up login limit is never mistaken for a wrong outcome of the sign-in.
 *
 * @param page - The page showing the sign-in form, both fields filled.
 * @param attempt - What the attempt is, for example "The wrong-password sign-in", for messages.
 * @returns The login response.
 * @throws Error when the API answers 429 `rate_limited`.
 */
async function submitSignIn(page: Page, attempt: string): Promise<Response> {
  const answered = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === LOGIN_API_PATH,
    { timeout: SIGN_IN_TIMEOUT_MS },
  );
  await page.getByRole('button', { name: SIGN_IN_LABEL, exact: true }).click();
  const response = await answered;
  if (response.status() === 429) {
    throw new Error(
      `${attempt} was answered 429: the API's login limit of 5 attempts per minute per IP is ` +
        'used up, by this run, a retry or another run within the last minute. Wait a minute ' +
        'before running the suite again.',
    );
  }
  return response;
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

/** Every item of a section, in group order, hidden ones included. */
function allItems(section: Section): Item[] {
  return section.groups.flatMap((group) => group.items);
}

/** Whether feedback given since the run hides the item now (`current.hidden_by`). */
function isHidden(item: Item): boolean {
  return item.current.hidden_by !== null && item.current.hidden_by !== undefined;
}

/**
 * `text` with each run of whitespace collapsed to one space and its ends trimmed, the way
 * Playwright compares text, so texts read from the page compare with texts from the API.
 */
function normalizeText(text: string): string {
  return text.replace(/\s+/gu, ' ').trim();
}

/**
 * An attribute selector matching `value` exactly, for the generated ids this spec passes.
 * `JSON.stringify` quotes `value` as a CSS string, which is exact only while it holds no control
 * character (U+0000–U+001F) and no lone surrogate: JSON escapes those in forms CSS reads
 * differently.
 */
function attributeSelector(attribute: string, value: string): string {
  return `[${attribute}=${JSON.stringify(value)}]`;
}

/** The display name of a source, or the raw value for a source this spec does not know. */
function sourceLabel(source: SourceName): string {
  return Object.hasOwn(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : String(source);
}

/**
 * The entity line a card's header shows (`entityLine` in `RecommendationCard.tsx`): the trimmed
 * entity name and the source's display name, joined by " · ", leaving out a part the entity does
 * not carry.
 *
 * @param item - The card's item.
 * @returns The line, or an empty string when the entity has neither part and the card shows none.
 */
function entityLine(item: Item): string {
  const name = item.entity.name?.trim() ?? '';
  const source = item.entity.source ? sourceLabel(item.entity.source) : '';
  return [name, source].filter((part) => part !== '').join(ENTITY_LINE_SEPARATOR);
}

/**
 * The name a white-paper row shows (`assetName` in `WhitePapersSection.tsx`): the entity name,
 * else its id, else "White paper {item id}", so a row is never unnamed.
 *
 * @param item - The white-paper item.
 * @returns The asset's display name.
 */
function assetName(item: Item): string {
  const name = item.entity.name?.trim() ?? '';
  if (name !== '') {
    return name;
  }
  const id = item.entity.id?.trim() ?? '';
  return id !== '' ? id : `White paper ${String(item.id)}`;
}

/**
 * Whether a white-paper row's Action cell shows `item`'s action: exactly the action, or, on a row
 * computed from replaced data, the action followed by its "Computed from replaced data" badge.
 *
 * @param item - A white-paper item of the API's section.
 * @param cellText - The Action cell's text, whitespace-normalized.
 * @returns `true` when the cell renders this item's action.
 */
function showsAction(item: Item, cellText: string): boolean {
  const action = normalizeText(item.action);
  if (!item.current.historical) {
    return cellText === action;
  }
  return cellText.startsWith(action) && cellText.endsWith(HISTORICAL_LABEL);
}

/**
 * The item id of a card's element id, `item-{id}`.
 *
 * @param elementId - The card's `id` attribute.
 * @returns The item id.
 * @throws Error when the id is not a recommendation card id.
 */
function cardItemId(elementId: string | null): number {
  const digits = /^item-(\d+)$/u.exec(elementId ?? '')?.[1];
  if (digits === undefined) {
    throw new Error(`"${String(elementId)}" is not a recommendation card id`);
  }
  return Number.parseInt(digits, 10);
}

/**
 * Expands every collapsed "Held until discrepancies are resolved" list shown in `region`, so the
 * held cards or rows it holds mount and become visible: `HeldDisclosure` mounts its content the
 * first time it opens. Each toggle is pinned by its `aria-controls` id before it is clicked,
 * because a locator filtered on `expanded: false` moves to the next toggle once one opens.
 *
 * @param region - A section's region.
 */
async function expandHeld(region: Locator): Promise<void> {
  const ids = await region
    .getByRole('button', { name: HELD_DISCLOSURE, expanded: false })
    .evaluateAll((toggles) => toggles.map((toggle) => toggle.getAttribute('aria-controls') ?? ''));
  for (const id of ids.filter((value) => value !== '')) {
    const toggle = region.locator(attributeSelector('aria-controls', id));
    await toggle.click({ timeout: READ_TIMEOUT_MS });
    await expect(toggle, `the held list "${id}" opens`).toHaveAttribute('aria-expanded', 'true', {
      timeout: READ_TIMEOUT_MS,
    });
  }
}

/**
 * Asserts that a card section renders an action as a card that matches the API. The first
 * visible card, or the first held one when only held cards exist, must be an item of this
 * section that feedback does not hide, headed with the item's action and showing its entity
 * line. Keywords selects the first tab with a card (`defaultTabKey` in `KeywordsSection.tsx`),
 * so its first visible card sits in the open tab.
 *
 * @param region - The section's region, `#section-{key}`.
 * @param section - The same section of the `GET /recommendations` read the page is compared to.
 * @param runLabel - "run {id}" of that read, for the failure messages.
 */
async function expectRenderedCard(
  region: Locator,
  section: Section,
  runLabel: string,
): Promise<void> {
  const title = section.title.trim();
  const cards = region.locator(CARD_SELECTOR).filter({ visible: true });
  if ((await cards.count()) === 0) {
    await expandHeld(region);
  }
  const card = cards.first();
  await expect(card, `the "${title}" section of ${runLabel} renders an action card`).toBeVisible({
    timeout: READ_TIMEOUT_MS,
  });

  const id = cardItemId(await card.getAttribute('id', { timeout: READ_TIMEOUT_MS }));
  const label = `card item-${String(id)} of the "${title}" section in ${runLabel}`;
  const item = allItems(section).find((candidate) => candidate.id === id);
  if (item === undefined) {
    throw new Error(`The ${label} is not an item of that section; a rerun may have replaced it`);
  }
  expect(isHidden(item), `the ${label} is not hidden by feedback`).toBe(false);
  await expect(card, `the ${label} is headed with its action`).toHaveAccessibleName(
    item.action.trim(),
    { timeout: READ_TIMEOUT_MS },
  );
  // An entity with neither a name nor a source gives the header no entity line to compare.
  const line = entityLine(item);
  if (line !== '') {
    await expect(
      card.getByText(line, { exact: true }).first(),
      `the ${label} names its entity "${line}"`,
    ).toBeVisible({ timeout: READ_TIMEOUT_MS });
  }
}

/**
 * Asserts that the White papers section renders an asset row that matches the API and reveals
 * its per-creative breakdown. The first visible row with a "Show details" toggle, or the
 * first held one when only held rows exist, must name an asset of this section that feedback
 * does not hide, with that asset's action in its Action cell and text in every cell; expanding
 * it must show the "Per creative: {asset}" table with at least one creative row.
 *
 * @param page - The test's page.
 * @param region - The section's region, `#section-white_papers`.
 * @param section - The same section of the `GET /recommendations` read the page is compared to.
 * @param runLabel - "run {id}" of that read, for the failure messages.
 */
async function expectRenderedAssetRow(
  page: Page,
  region: Locator,
  section: Section,
  runLabel: string,
): Promise<void> {
  const title = section.title.trim();
  const rows = region
    .getByRole('row')
    .filter({ has: page.getByRole('button', { name: ASSET_TOGGLE }) })
    .filter({ visible: true });
  if ((await rows.count()) === 0) {
    await expandHeld(region);
  }
  const row = rows.first();
  await expect(row, `the "${title}" section of ${runLabel} renders an asset row`).toBeVisible({
    timeout: READ_TIMEOUT_MS,
  });

  const toggle = row.getByRole('button', { name: ASSET_TOGGLE });
  const toggleText = normalizeText((await toggle.textContent({ timeout: READ_TIMEOUT_MS })) ?? '');
  const toggleName = ASSET_TOGGLE_NAME.exec(toggleText);
  if (toggleName === null) {
    throw new Error(
      `The first asset row of the "${title}" section of ${runLabel} has the toggle ` +
        `"${toggleText}", which is not "Show details for {asset} ({currency})"`,
    );
  }
  const name = toggleName[1] ?? '';
  const cells = row.getByRole('cell');
  const actionText = normalizeText(
    (await cells.last().textContent({ timeout: READ_TIMEOUT_MS })) ?? '',
  );
  const item = allItems(section).find(
    (candidate) =>
      !isHidden(candidate) &&
      normalizeText(assetName(candidate)) === name &&
      showsAction(candidate, actionText),
  );
  if (item === undefined) {
    throw new Error(
      `The "${title}" section of ${runLabel} shows the asset row "${name}" with the action ` +
        `"${actionText}", which is no white paper of that section that feedback does not hide`,
    );
  }
  const label =
    `asset row item ${String(item.id)} ("${name}") of the "${title}" section in ` + runLabel;

  await expect(cells, `the ${label} has its toggle, figure and Action cells`).toHaveCount(
    ASSET_ROW_CELL_COUNT,
    { timeout: READ_TIMEOUT_MS },
  );
  const cellTexts = (await cells.allTextContents()).map(normalizeText);
  expect(
    cellTexts.flatMap((text, index) => (text === '' ? [index + 1] : [])),
    `every cell of the ${label} shows a value (blank cells listed by position)`,
  ).toEqual([]);
  const header = row.getByRole('rowheader');
  await expect(header, `the ${label} has one Asset row header`).toHaveCount(1);
  await expect(header, `the ${label} is headed with its asset name`).toContainText(name, {
    timeout: READ_TIMEOUT_MS,
  });

  // Pinned by its detail id: "expanded: false" would no longer match once the row opens.
  const detailId = await toggle.getAttribute('aria-controls', { timeout: READ_TIMEOUT_MS });
  if (detailId === null || detailId === '') {
    throw new Error(`The toggle of the ${label} controls no detail row`);
  }
  const pinned = region.locator(attributeSelector('aria-controls', detailId));
  await pinned.click({ timeout: READ_TIMEOUT_MS });
  await expect(pinned, `the ${label} expands`).toHaveAttribute('aria-expanded', 'true', {
    timeout: READ_TIMEOUT_MS,
  });
  const detail = page.locator(attributeSelector('id', detailId));
  await expect(detail, `the ${label} shows its details`).toBeVisible({ timeout: READ_TIMEOUT_MS });
  const perCreative = detail.getByRole('table', {
    name: `${PER_CREATIVE_CAPTION_PREFIX}${name}`,
    exact: true,
  });
  await expect(perCreative, `the ${label} details its creatives`).toBeVisible({
    timeout: READ_TIMEOUT_MS,
  });
  await expect(
    perCreative.getByRole('rowheader').first(),
    `the per-creative table of the ${label} lists at least one creative`,
  ).toBeVisible({ timeout: READ_TIMEOUT_MS });
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
        // Scoped by id as well: the region named by the title must be the section itself. A
        // group whose title repeats its section's renders no region of its own.
        await expect(
          page.getByRole('region', { name: title, exact: true }).and(region),
          `the "${title}" section is a region named by its title`,
        ).toBeVisible({ timeout: READ_TIMEOUT_MS });
        expect(
          await sectionNavCount(page, section.key),
          `the "${title}" link in the "${SECTION_NAV_LABEL}" navigation counts at least one item`,
        ).toBeGreaterThanOrEqual(1);
        await expect(
          region.locator(EMPTY_STATE_SELECTOR),
          `the "${title}" section is not empty`,
        ).toHaveCount(0, { timeout: READ_TIMEOUT_MS });
        // The counts above come from the API; the section must also render one of its actions.
        if (section.key === 'white_papers') {
          await expectRenderedAssetRow(page, region, section, runLabel);
        } else {
          await expectRenderedCard(region, section, runLabel);
        }
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

// Declared after the first-start test, so that test's sign-in is never preceded by a refused one
// in this file. Exactly two login attempts: the wrong password, then the right one.
test('a wrong password is refused, leaves no session, and the correct one then signs in', async ({
  page,
}) => {
  const { email, password } = adminCredentials();
  // Generated per run, so no password is written in the repository. The check compares the two
  // without printing either.
  const wrongPassword = `${WRONG_PASSWORD_PREFIX}${randomUUID()}`;
  expect(
    wrongPassword === password,
    'the generated wrong password differs from E2E_ADMIN_PASSWORD',
  ).toBe(false);

  const emailField = page.getByRole('textbox', { name: EMAIL_LABEL, exact: true });
  const passwordField = page.getByRole('textbox', { name: PASSWORD_LABEL, exact: true });
  const signOut = page.getByRole('button', { name: SIGN_OUT_LABEL, exact: true });

  await test.step('the browser starts signed out, on the sign-in page', async () => {
    const me = await page.request.get(ME_PATH);
    expect(me.status(), `the browser starts without a session (${ME_PATH} answers 401)`).toBe(401);
    await page.goto(LOGIN_PATH);
    await expect(page, `${LOGIN_PATH} opens`).toHaveURL((url) => url.pathname === LOGIN_PATH);
    await expect(page.getByRole('button', { name: SIGN_IN_LABEL, exact: true })).toBeVisible();
  });

  await test.step('the API refuses the wrong password with 401 invalid_credentials', async () => {
    await emailField.fill(email);
    await enterPassword(passwordField, wrongPassword);
    const response = await submitSignIn(page, 'The wrong-password sign-in');
    expect(response.status(), `${LOGIN_API_PATH} refuses the wrong password with 401`).toBe(401);
    const body = (await response.json()) as Partial<ErrorResponse>;
    expect(body.error?.code, `the refusal is "${INVALID_CREDENTIALS_CODE}"`).toBe(
      INVALID_CREDENTIALS_CODE,
    );
  });

  await test.step('the form shows the refusal and stays usable on /login', async () => {
    await expect(page, `a refused sign-in stays on ${LOGIN_PATH}`).toHaveURL(
      (url) => url.pathname === LOGIN_PATH,
    );
    await expect(passwordField, 'the Password field is marked invalid').toHaveAttribute(
      'aria-invalid',
      'true',
    );
    await expect
      .poll(() => fieldDescription(passwordField), {
        message: 'the Password field is described by the refusal message',
        timeout: READ_TIMEOUT_MS,
      })
      .toBe(INVALID_CREDENTIALS_MESSAGE);
    // Once in the critical banner that heads the form, once as the Password field's error.
    await expect(
      page.locator('form').getByText(INVALID_CREDENTIALS_MESSAGE, { exact: true }),
      `the form shows "${INVALID_CREDENTIALS_MESSAGE}" as its banner and as the field error`,
    ).toHaveCount(2);
    // Compared as a boolean, so a failure never prints the field's value.
    await expect
      .poll(async () => (await passwordField.inputValue()) === '', {
        message: 'the refused password is cleared from the Password field',
        timeout: READ_TIMEOUT_MS,
      })
      .toBe(true);
    await expect(passwordField, 'focus returns to the Password field').toBeFocused();
    await expect(emailField, 'the Email field keeps the entered email').toHaveValue(email);
    await expect(signOut, `no "${SIGN_OUT_LABEL}" button: nobody is signed in`).toHaveCount(0);
  });

  await test.step('the refused sign-in created no session', async () => {
    const me = await page.request.get(ME_PATH);
    expect(me.status(), `${ME_PATH} still answers 401 after the refusal`).toBe(401);
    // A second page of the same browser context shares its cookies; it is a GET, not a login.
    const other = await page.context().newPage();
    try {
      await other.goto(ACTIONS_PATH);
      await expect(other, `a new page opening ${ACTIONS_PATH} is sent to ${LOGIN_PATH}`).toHaveURL(
        (url) => url.pathname === LOGIN_PATH,
      );
      await expect(other.getByRole('button', { name: SIGN_IN_LABEL, exact: true })).toBeVisible();
    } finally {
      await other.close();
    }
  });

  // No sign-out afterwards: logging out revokes every session, the suite's saved auth.setup.ts
  // session included, which the other specs start from.
  await test.step('the correct password then signs in on the same form', async () => {
    await enterPassword(passwordField, password);
    const response = await submitSignIn(page, 'The sign-in with the correct password');
    expect(response.status(), `${LOGIN_API_PATH} accepts the correct password`).toBe(200);
    await expect(page, `the accepted sign-in opens ${ACTIONS_PATH}`).toHaveURL(
      (url) => url.pathname === ACTIONS_PATH,
      { timeout: SIGN_IN_TIMEOUT_MS },
    );
    await expect(actionsTitle(page)).toBeVisible();
    const me = await getJson<Me>(page, ME_PATH);
    expect(me.email, `${ME_PATH} names the admin who signed in`).toBe(email.trim().toLowerCase());
  });
});
