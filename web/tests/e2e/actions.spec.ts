/**
 * End-to-end checks of the `/actions` screen (AAP 0.5.5, 0.11.2), run against one shared stack in
 * simulated mode (`DATA_MODE=simulated`, no source credentials).
 *
 * What the spec proves, test by test:
 * 1. The five sections render in the user's fixed order (Keywords, Campaigns, Budget, Copy /
 *    creative, White papers), titled as the API titles them, and each "Sections" navigation link
 *    counts the section's items that feedback has not hidden.
 * 2. A card in every section shows its evidence table, the window chip (window, day count and
 *    each source's day boundary) and its flags, including the "Simulated data" label of an item
 *    computed from simulated data; a white-paper row shows the same in its expanded details.
 * 3. The window picker offers exactly the configured presets with the configured default marked;
 *    a non-default preset opens a custom run at `/actions?run=<id>` with its banner, and "Back to
 *    default" returns to the default run.
 * 4. Snoozing a card hides it, and its section reports the hidden or suppressed count.
 * 5. Discrepancy (C0) cards carry their resolver and never a feedback action (AAP 0.5.4.2).
 *
 * How it stays reliable on a shared, mutating stack:
 * - Every expectation is derived from the API (`page.request`, which carries the signed-in
 *   session of `auth.setup.ts`), never from fixed names, ids or counts.
 * - The worker reruns the default analysis after every sync, decision and feedback write, so
 *   item ids and counts change under the spec. Every UI-versus-API comparison therefore runs in
 *   an `expect(...).toPass()` block that reloads the page and re-reads the API.
 * - With one worker, specs run in file order and this one runs first, possibly before the
 *   worker's first default run exists, so every test waits for that run itself.
 *
 * The spec never signs in (the API allows 5 sign-ins per minute per IP; `auth.setup.ts` signs in
 * once) and writes only through the UI, which adds the CSRF header. It writes one custom run and
 * one snooze, of an item without a decision subject in a section that keeps at least one item,
 * so the later specs still find every section populated and their Record-decision candidates
 * untouched.
 *
 * @example
 * ```sh
 * cd web && E2E_BASE_URL=http://localhost:8080 E2E_ADMIN_PASSWORD=pw npx playwright test actions
 * ```
 *
 * @packageDocumentation
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Locator, Page } from '@playwright/test';

import type {
  ConfigView,
  FeedbackKind,
  Flag,
  FlagCode,
  Item,
  RecommendationsPage,
  Run,
  Section,
  SectionKey,
  SourceName,
} from '../../src/api/types';

// ---------------------------------------------------------------------------------------------
// API paths and timing
// ---------------------------------------------------------------------------------------------

const LATEST_RUN_PATH = '/api/v1/runs/latest';
const RECOMMENDATIONS_PATH = '/api/v1/recommendations';
const CONFIG_PATH = '/api/v1/config';
const ACTIONS_PATH = '/actions';

/** Whole-test budget: the first test may wait minutes for the worker's first default run. */
const TEST_TIMEOUT_MS = 10 * 60_000;

/**
 * How long the first default run may take: on first start the worker backfills 180 days for
 * three simulated sources and the companion CRM dataset before it analyses.
 */
const FIRST_RUN_TIMEOUT_MS = 9 * 60_000;
const FIRST_RUN_INTERVALS_MS = [5_000, 10_000, 15_000];

/** Bound on one API read, so a hung connection never stalls a poll. */
const API_READ_TIMEOUT_MS = 30_000;

/** Bound on one locator read inside a retried block, so a retry starts promptly. */
const READ_TIMEOUT_MS = 10_000;

/** Retry budget of a UI-versus-API comparison while reruns replace the run under the spec. */
const SETTLE = { intervals: [2_000, 5_000, 10_000], timeout: 120_000 };

/** Retry budget of the custom-run request while another process holds the analysis lock. */
const CUSTOM_RUN_RETRY = { intervals: [5_000, 10_000, 20_000], timeout: 180_000 };

/** `POST /runs` computes synchronously; a typical account computes in under 30 s. */
const CUSTOM_RUN_OPEN_TIMEOUT_MS = 45_000;

/** How long a snoozed card may stay on screen while the recommendations refetch. */
const CARD_REMOVAL_TIMEOUT_MS = 30_000;

/** Retry budget of the suppression report, which may wait for the coalesced (60 s) rerun. */
const SUPPRESSION_RETRY = { intervals: [5_000, 10_000, 15_000], timeout: 150_000 };

// ---------------------------------------------------------------------------------------------
// UI vocabulary (each value is the label the written UI renders)
// ---------------------------------------------------------------------------------------------

/** The five sections in the user's order (AAP 0.5.5); the assertion target, not a copy. */
const SECTION_KEYS: readonly SectionKey[] = [
  'keywords',
  'campaigns',
  'budget',
  'creative',
  'white_papers',
];

/** The sections whose items render as recommendation cards (`<article id="item-{id}">`). */
const CARD_SECTION_KEYS: readonly SectionKey[] = ['keywords', 'campaigns', 'budget', 'creative'];

const PAGE_HEADING = 'Actions';
const SECTION_NAV_LABEL = 'Sections';
const EVIDENCE_CAPTION = 'Evidence';
const FLAGS_LABEL = 'Flags';
const NOT_SHOWN_LABEL = 'Not shown';
const KEYWORD_TABS_LABEL = 'Keyword actions';
const HELD_DISCLOSURE = /^Held until discrepancies are resolved/u;
const WHITE_PAPER_TABLE = /^White papers \(/u;
const WINDOW_PICKER_LABEL = 'Analysis window';
const CUSTOM_RANGE_LABEL = 'Custom range';
const PRESET_BUTTON = /^(\d+) days?( \(default\))?$/u;
const RUN_IN_PROGRESS_MESSAGE = 'Another analysis is running. Try again shortly.';
const CUSTOM_BANNER_SELECTOR = '#banner-custom-window';
const BACK_TO_DEFAULT_LABEL = 'Back to default';
const CUSTOM_RUN_URL = /\/actions\?run=\d+/u;
const DISCREPANCY_GROUP_SELECTOR = '#group-campaigns-discrepancies';
const DISCREPANCY_RULE_ID = 'C0';
const RESOLVED_SINCE_RUN = 'Resolved since this run';

/** The C0 resolver's button labels, one per resolution key (AAP 0.5.4.4). */
const RESOLVER_BUTTON = new RegExp(
  `^(${[
    'Platform is correct',
    'Decision is correct',
    'Adopt platform state',
    'Record intended state',
    'Acknowledge removal',
  ].join('|')})$`,
  'u',
);

/** Footer button names of a recommendation card, per feedback kind. */
const FEEDBACK_BUTTONS: Readonly<Record<FeedbackKind, string>> = {
  done: 'Mark done',
  dismiss: 'Dismiss',
  snooze: 'Snooze for 7 days',
};

/** The feedback the spec gives: a 7-day snooze, the shortest suppression (AAP 0.5.2). */
const FEEDBACK_KIND: FeedbackKind = 'snooze';

/** The primary action of an item with a decision subject. */
const RECORD_DECISION_LABEL = 'Record decision';

/** How a section's "Not shown" list names the stored count of each feedback kind. */
const SUPPRESSION_LABELS: Readonly<Record<FeedbackKind, string>> = {
  done: 'marked done',
  dismiss: 'dismissed',
  snooze: 'snoozed',
};

/** Display names of the sources, as the day-boundary labels show them. */
const SOURCE_LABELS: Readonly<Record<SourceName, string>> = {
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
};

const COUNT_FORMAT = new Intl.NumberFormat('en');

// ---------------------------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------------------------

/** One JSON read: the HTTP status and, on a 2xx, the parsed body. */
interface JsonResult<T> {
  status: number;
  body: T | null;
}

/** GETs `path` with the page's session and parses the body when the status is 2xx. */
async function getJson<T>(request: APIRequestContext, path: string): Promise<JsonResult<T>> {
  const response = await request.get(path, { timeout: API_READ_TIMEOUT_MS });
  if (!response.ok()) {
    return { status: response.status(), body: null };
  }
  return { status: response.status(), body: (await response.json()) as T };
}

/** GETs `path` and fails the step unless it answers 200 with a body. */
async function requireJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const { status, body } = await getJson<T>(request, path);
  expect(status, `GET ${path} answers 200`).toBe(200);
  if (body === null) {
    throw new Error(`GET ${path} answered ${status} without a body`);
  }
  return body;
}

/** The status of `GET /runs/latest`, or 0 while the stack does not answer. */
async function latestRunStatus(request: APIRequestContext): Promise<number> {
  try {
    return (await getJson<Run>(request, LATEST_RUN_PATH)).status;
  } catch {
    return 0;
  }
}

/**
 * Waits until the worker's first default run has succeeded (`/runs/latest` answers 404
 * `not_found` until then) and returns the latest default run.
 */
async function waitForDefaultRun(page: Page): Promise<Run> {
  await expect
    .poll(() => latestRunStatus(page.request), {
      message: `the worker has finished a default analysis run (${LATEST_RUN_PATH} answers 200)`,
      timeout: FIRST_RUN_TIMEOUT_MS,
      intervals: FIRST_RUN_INTERVALS_MS,
    })
    .toBe(200);
  return requireJson<Run>(page.request, LATEST_RUN_PATH);
}

/** The latest default run's five sections, with the current-state overlay. */
function fetchRecommendations(page: Page): Promise<RecommendationsPage> {
  return requireJson<RecommendationsPage>(page.request, RECOMMENDATIONS_PATH);
}

/** The section of `key`; fails when the API omitted it. */
function sectionOf(recommendations: RecommendationsPage, key: SectionKey): Section {
  const section = recommendations.sections.find((candidate) => candidate.key === key);
  if (section === undefined) {
    throw new Error(`The API page of run ${recommendations.run.id} has no "${key}" section`);
  }
  return section;
}

/** Every item of a section, in group order. */
function allItems(section: Section): Item[] {
  return section.groups.flatMap((group) => group.items);
}

/** Whether feedback given since the run hides the item (`current.hidden_by`). */
function isHidden(item: Item): boolean {
  return item.current.hidden_by !== null && item.current.hidden_by !== undefined;
}

/** Whether a discrepancy holds the item, as recorded or read now. */
function isHeld(item: Item): boolean {
  return item.held || item.current.held_now;
}

/**
 * The items a section shows, held ones included: what its navigation link counts
 * (`visibleItemCount` in `SectionNav.tsx`).
 */
function visibleItems(section: Section): Item[] {
  return allItems(section).filter((item) => !isHidden(item));
}

/** The item with `id` in any section of the page, if the page's run produced it. */
function findItem(recommendations: RecommendationsPage, id: number): Item | undefined {
  return recommendations.sections.flatMap(allItems).find((item) => item.id === id);
}

// ---------------------------------------------------------------------------------------------
// Expected text, derived from API values the way `src/lib/format.ts` renders them
// ---------------------------------------------------------------------------------------------

/** A day count as `formatDays` renders it: `"1 day"`, `"28 days"`. */
function dayCount(days: number): string {
  return `${COUNT_FORMAT.format(days)} ${days === 1 ? 'day' : 'days'}`;
}

/** A preset button's day label as `DateRangePicker` renders it (no grouping separator). */
function presetLabel(days: number): string {
  return `${days} ${days === 1 ? 'day' : 'days'}`;
}

/** The display name of a source, or the raw value for a source this spec does not know. */
function sourceLabel(source: SourceName): string {
  return Object.hasOwn(SOURCE_LABELS, source) ? SOURCE_LABELS[source] : String(source);
}

/**
 * The fragments every window chip of the run's items must contain (`formatWindowChip`):
 * `"{start} – {end} · {N} days"`, then `"{Label} days: {boundary}"` for each source, once per
 * source. The chip may append a source's own range to its boundary entry, so each entry is
 * checked as a fragment rather than the chip as one string.
 */
function chipFragments(run: Run): string[] {
  const fragments = [`${run.window_start} – ${run.window_end} · ${dayCount(run.days)}`];
  const seen = new Set<SourceName>();
  for (const day of run.source_days) {
    if (!seen.has(day.source)) {
      seen.add(day.source);
      fragments.push(`${sourceLabel(day.source)} days: ${day.boundary}`);
    }
  }
  return fragments;
}

/**
 * The flag badges an item shows (`mergeFlags` in `FlagList.tsx`): every code once, recorded
 * codes first, each labelled with its current message, else its recorded one, else its code.
 */
function shownFlags(item: Item): Flag[] {
  const order: FlagCode[] = [];
  const recorded = new Map<FlagCode, string>();
  const current = new Map<FlagCode, string>();
  const collect = (flags: readonly Flag[], messages: Map<FlagCode, string>): void => {
    for (const flag of flags) {
      if (!order.includes(flag.code)) {
        order.push(flag.code);
      }
      if (!messages.has(flag.code) && flag.message.trim() !== '') {
        messages.set(flag.code, flag.message);
      }
    }
  };
  collect(item.flags, recorded);
  collect(item.current.flags_now, current);
  return order.map((code) => ({
    code,
    message: current.get(code) ?? recorded.get(code) ?? code,
  }));
}

/** The name a white-paper row shows (`assetName` in `WhitePapersSection.tsx`). */
function assetName(item: Item): string {
  const name = item.entity.name?.trim() ?? '';
  if (name !== '') {
    return name;
  }
  const id = item.entity.id?.trim() ?? '';
  return id !== '' ? id : `White paper ${item.id}`;
}

/** A group's tab or heading title (the key when the title is blank). */
function groupTitle(title: string, key: string): string {
  const trimmed = title.trim();
  return trimmed === '' ? key : trimmed;
}

/** `text` escaped for use inside a regular expression. */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
}

/** An attribute selector matching `value` exactly, whatever characters it holds. */
function attributeSelector(attribute: string, value: string): string {
  return `[${attribute}=${JSON.stringify(value)}]`;
}

/** The item id of a card's `item-{id}` element id; fails for anything else. */
function cardItemId(elementId: string | null): number {
  const match = /^item-(\d+)$/u.exec(elementId ?? '');
  const digits = match?.[1];
  if (digits === undefined) {
    throw new Error(`"${String(elementId)}" is not a recommendation card id`);
  }
  return Number.parseInt(digits, 10);
}

// ---------------------------------------------------------------------------------------------
// Page helpers
// ---------------------------------------------------------------------------------------------

/** The page's level-1 heading, rendered in every state of `/actions`, loading included. */
function actionsHeading(page: Page): Locator {
  return page.getByRole('heading', { level: 1, name: PAGE_HEADING, exact: true });
}

/** The "Sections" navigation, rendered only once a run's sections have loaded. */
function sectionNav(page: Page): Locator {
  return page.getByRole('navigation', { name: SECTION_NAV_LABEL, exact: true });
}

/** A section's `<section id="section-{key}">` region. */
function sectionRegion(page: Page, key: SectionKey): Locator {
  return page.locator(`#section-${key}`);
}

/**
 * Waits for the worker's first default run, opens `/actions` and waits for its heading: the
 * opening of every test.
 */
async function openActions(page: Page): Promise<Run> {
  const run = await waitForDefaultRun(page);
  await page.goto(ACTIONS_PATH);
  await expect(actionsHeading(page)).toBeVisible();
  return run;
}

/** Waits until the page shows a run's content, not its loading state. */
async function expectSectionsLoaded(page: Page): Promise<void> {
  await expect(actionsHeading(page)).toBeVisible();
  await expect(sectionNav(page)).toBeVisible();
}

/** Reloads the page and waits for its content, so it reads the newest run and overlay. */
async function reloadActions(page: Page): Promise<void> {
  await page.reload();
  await expectSectionsLoaded(page);
}

/**
 * The item count a section's navigation link shows: the trailing number of the link
 * `a[href="#section-{key}"]` ("Keywords to adjust 12").
 */
async function sectionNavCount(page: Page, key: SectionKey): Promise<number> {
  const link = sectionNav(page).locator(attributeSelector('href', `#section-${key}`));
  const text = ((await link.textContent({ timeout: READ_TIMEOUT_MS })) ?? '').trim();
  const digits = /(\d[\d,]*)$/u.exec(text)?.[1];
  if (digits === undefined) {
    throw new Error(`The "${key}" section link "${text}" shows no item count`);
  }
  return Number.parseInt(digits.replaceAll(',', ''), 10);
}

/** Expands every collapsed "Held until discrepancies are resolved" disclosure in `region`. */
async function expandHeld(region: Locator): Promise<void> {
  const disclosures = region.getByRole('button', { name: HELD_DISCLOSURE, expanded: false });
  for (const disclosure of await disclosures.all()) {
    if (await disclosure.isVisible()) {
      await disclosure.click();
    }
  }
}

/**
 * Asserts that `container` shows the window chip fragments of `run` and every flag badge of
 * `item`, including "Simulated data" when the item was computed from simulated data.
 */
async function expectWindowAndFlags(container: Locator, item: Item, run: Run): Promise<void> {
  for (const fragment of chipFragments(run)) {
    await expect(container, `item ${item.id} shows the window chip "${fragment}"`).toContainText(
      fragment,
    );
  }
  const flags = shownFlags(item);
  expect(
    flags.length,
    `item ${item.id} (${item.rule_id}) carries at least one flag; under DATA_MODE=simulated ` +
      'every item is flagged as simulated data',
  ).toBeGreaterThan(0);
  if (item.data_status === 'simulated' || item.data_status === 'mixed') {
    expect(
      flags.map((flag) => flag.code),
      `item ${item.id} computed from ${item.data_status} data carries the simulated flag`,
    ).toContain('simulated');
  }
  const badges = container.getByRole('list', { name: FLAGS_LABEL, exact: true });
  await expect(badges).toBeVisible();
  for (const flag of flags) {
    await expect(badges, `item ${item.id} shows the "${flag.code}" badge`).toContainText(
      flag.message,
    );
  }
}

// ---------------------------------------------------------------------------------------------
// Feedback target
// ---------------------------------------------------------------------------------------------

/** The item the feedback test snoozes, with where it sits on the page. */
interface FeedbackTarget {
  sectionKey: SectionKey;
  /** The title of the item's group: the keyword tab to select. */
  groupTitle: string;
  item: Item;
  /** The run the item belongs to. */
  runId: number;
}

/** A target after its Snooze button was clicked. */
interface SubmittedFeedback {
  target: FeedbackTarget;
  /** What disappears once the item is hidden: its card, or its white-paper row toggle. */
  gone: Locator;
}

/** The on-page controls of a revealed target. */
interface RevealedTarget {
  /** The element holding the target's footer actions. */
  actions: Locator;
  /** What disappears once the item is hidden. */
  gone: Locator;
}

/**
 * Whether feedback may be given on the item from its card: not a discrepancy (C0 is exempt),
 * no decision subject (so `decisions.spec.ts` keeps its Record-decision candidates), not
 * historical, not held and not already hidden.
 */
function acceptsFeedback(item: Item): boolean {
  return (
    item.rule_id !== DISCREPANCY_RULE_ID &&
    item.decision_subject === null &&
    !item.current.historical &&
    !isHeld(item) &&
    !isHidden(item)
  );
}

/** Whether no other shown white-paper row carries the item's name, so its toggle is unique. */
function hasUniqueAssetName(section: Section, item: Item): boolean {
  const name = assetName(item);
  return (
    visibleItems(section).filter((other) => !isHeld(other) && assetName(other) === name)
      .length === 1
  );
}

/** The first item of a section that accepts feedback, as a target. */
function firstTarget(section: Section, runId: number): FeedbackTarget | null {
  for (const group of section.groups) {
    for (const item of group.items) {
      if (
        acceptsFeedback(item) &&
        (section.key !== 'white_papers' || hasUniqueAssetName(section, item))
      ) {
        return {
          sectionKey: section.key,
          groupTitle: groupTitle(group.title, group.key),
          item,
          runId,
        };
      }
    }
  }
  return null;
}

/**
 * Chooses the feedback target among sections that show at least two items, so the section
 * stays populated after the snooze: a keyword item when one qualifies (keyword items never
 * carry a decision subject), otherwise the qualifying item of the section showing most items.
 */
function chooseFeedbackTarget(recommendations: RecommendationsPage): FeedbackTarget | null {
  const options = recommendations.sections
    .filter((section) => visibleItems(section).length >= 2)
    .map((section) => ({ section, target: firstTarget(section, recommendations.run.id) }))
    .filter((option) => option.target !== null);
  const keywords = options.find((option) => option.section.key === 'keywords');
  const busiest = options.toSorted(
    (left, right) => visibleItems(right.section).length - visibleItems(left.section).length,
  )[0];
  return (keywords ?? busiest)?.target ?? null;
}

/**
 * Makes the target's actions visible: selects its keyword tab, or expands its white-paper row;
 * other sections show their cards directly.
 */
async function revealTarget(page: Page, target: FeedbackTarget): Promise<RevealedTarget> {
  const region = sectionRegion(page, target.sectionKey);
  if (target.sectionKey === 'white_papers') {
    const toggle = region.getByRole('button', {
      name: `Details for ${assetName(target.item)}`,
      exact: true,
    });
    await expect(toggle).toBeVisible({ timeout: READ_TIMEOUT_MS });
    const detailId = await toggle.getAttribute('aria-controls', { timeout: READ_TIMEOUT_MS });
    if (detailId === null) {
      throw new Error(`The row toggle of "${assetName(target.item)}" controls no detail row`);
    }
    if ((await toggle.getAttribute('aria-expanded')) !== 'true') {
      await toggle.click({ timeout: READ_TIMEOUT_MS });
    }
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const detail = page.locator(attributeSelector('id', detailId));
    await expect(detail).toBeVisible({ timeout: READ_TIMEOUT_MS });
    return { actions: detail, gone: toggle };
  }
  if (target.sectionKey === 'keywords') {
    const tab = page
      .getByRole('tablist', { name: KEYWORD_TABS_LABEL, exact: true })
      .getByRole('tab', {
        name: new RegExp(`^${escapeRegExp(target.groupTitle)}(?: \\d[\\d,]*)?$`, 'u'),
      });
    await tab.click({ timeout: READ_TIMEOUT_MS });
    await expect(tab).toHaveAttribute('aria-selected', 'true');
  }
  const card = region.locator(`#item-${target.item.id}`);
  await expect(card).toBeVisible({ timeout: READ_TIMEOUT_MS });
  return { actions: card, gone: page.locator(`#item-${target.item.id}`) };
}

/** Whether two items name the same rule and entity: what feedback suppresses in later runs. */
function sameRuleAndEntity(left: Item, right: Item): boolean {
  return (
    left.rule_id === right.rule_id &&
    left.entity.type === right.entity.type &&
    left.entity.id === right.entity.id
  );
}

/** The campaigns section's discrepancy (C0) items. */
function discrepancyItems(recommendations: RecommendationsPage): Item[] {
  return allItems(sectionOf(recommendations, 'campaigns')).filter(
    (item) => item.rule_id === DISCREPANCY_RULE_ID,
  );
}

/** Adds a note to the test's report, for a check the current data cannot exercise. */
function note(description: string): void {
  test.info().annotations.push({ type: 'note', description });
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

test.describe('actions page', () => {
  test.describe.configure({ timeout: TEST_TIMEOUT_MS });

  test('shows the five sections in order, with navigation counts matching the API', async ({
    page,
  }) => {
    await openActions(page);
    await expectSectionsLoaded(page);
    const recommendations = await fetchRecommendations(page);
    expect(
      recommendations.sections.map((section) => section.key),
      'the API returns the five sections in the fixed order',
    ).toEqual(SECTION_KEYS);

    const rendered = await page.locator('section[id^="section-"]').evaluateAll((elements) =>
      elements.map((element) => {
        const headingId = element.getAttribute('aria-labelledby');
        const heading = headingId === null ? null : document.getElementById(headingId);
        return { id: element.id, title: heading?.textContent?.trim() ?? '' };
      }),
    );
    expect(
      rendered.map((section) => section.id),
      'the page renders the five sections in the fixed order',
    ).toEqual(SECTION_KEYS.map((key) => `section-${key}`));
    expect(
      rendered.map((section) => section.title),
      'each section is titled as the API titles it',
    ).toEqual(recommendations.sections.map((section) => section.title.trim()));
    for (const section of recommendations.sections) {
      // Scoped by id: a group inside a section may carry the section's own title.
      await expect(
        page
          .getByRole('region', { name: section.title.trim(), exact: true })
          .and(sectionRegion(page, section.key)),
        `the "${section.key}" section is a region named by its title`,
      ).toBeVisible();
    }

    const links = await sectionNav(page)
      .locator('a[href^="#section-"]')
      .evaluateAll((elements) => elements.map((element) => element.getAttribute('href')));
    expect(links, 'the section navigation links the sections in the fixed order').toEqual(
      SECTION_KEYS.map((key) => `#section-${key}`),
    );

    await expect(async () => {
      await reloadActions(page);
      const fresh = await fetchRecommendations(page);
      for (const key of SECTION_KEYS) {
        const expected = visibleItems(sectionOf(fresh, key)).length;
        expect(
          await sectionNavCount(page, key),
          `the "${key}" link counts the ${expected} items of run ${fresh.run.id} ` +
            'that feedback has not hidden',
        ).toBe(expected);
      }
    }).toPass(SETTLE);
  });

  test('cards show their evidence, window chip and flags in every section', async ({ page }) => {
    await openActions(page);

    for (const key of CARD_SECTION_KEYS) {
      await test.step(`${key}: the first visible card`, async () => {
        await expect(async () => {
          await reloadActions(page);
          const recommendations = await fetchRecommendations(page);
          expect(
            visibleItems(sectionOf(recommendations, key)).length,
            `the "${key}" section of run ${recommendations.run.id} has items`,
          ).toBeGreaterThan(0);

          const region = sectionRegion(page, key);
          const cards = region.locator('article[id^="item-"]').filter({ visible: true });
          if ((await cards.count()) === 0) {
            // Every shown item may be held behind its discrepancy; open the disclosures.
            await expandHeld(region);
          }
          const card = cards.first();
          await expect(card).toBeVisible({ timeout: READ_TIMEOUT_MS });
          const id = cardItemId(await card.getAttribute('id', { timeout: READ_TIMEOUT_MS }));
          const item = findItem(recommendations, id);
          if (item === undefined) {
            throw new Error(
              `Card item-${id} is not in run ${recommendations.run.id}; a rerun replaced it`,
            );
          }

          await expect(
            card.getByRole('table', { name: EVIDENCE_CAPTION, exact: true }),
            `card item-${id} shows its evidence table`,
          ).toBeVisible();
          await expectWindowAndFlags(card, item, recommendations.run);
        }).toPass(SETTLE);
      });
    }

    await test.step('white_papers: the first asset row, expanded', async () => {
      await expect(async () => {
        await reloadActions(page);
        const recommendations = await fetchRecommendations(page);
        const section = sectionOf(recommendations, 'white_papers');
        // The first table is the first currency cohort with an unheld row, and its first row
        // that cohort's first unheld item (WhitePapersSection renders cohorts in group order).
        const expected = section.groups
          .map((group) => group.items.find((item) => !isHidden(item) && !isHeld(item)))
          .find((item) => item !== undefined);
        if (expected === undefined) {
          throw new Error(`The white papers of run ${recommendations.run.id} have no unheld row`);
        }
        const name = assetName(expected);

        const table = sectionRegion(page, 'white_papers')
          .getByRole('table', { name: WHITE_PAPER_TABLE })
          .first();
        await expect(table).toBeVisible({ timeout: READ_TIMEOUT_MS });
        const firstToggle = table.getByRole('button', { expanded: false }).first();
        await expect(firstToggle).toHaveText(`Details for ${name}`, { timeout: READ_TIMEOUT_MS });
        const detailId = await firstToggle.getAttribute('aria-controls', {
          timeout: READ_TIMEOUT_MS,
        });
        if (detailId === null) {
          throw new Error(`The row toggle of "${name}" controls no detail row`);
        }
        // Pinned by its detail id: "expanded: false" would move to the next row once clicked.
        const toggle = table.locator(attributeSelector('aria-controls', detailId));
        await toggle.click({ timeout: READ_TIMEOUT_MS });
        await expect(toggle).toHaveAttribute('aria-expanded', 'true');

        const detail = page.locator(attributeSelector('id', detailId));
        await expect(detail).toBeVisible();
        await expect(
          detail.getByRole('table', { name: `Per creative: ${name}`, exact: true }),
        ).toBeVisible();
        await expectWindowAndFlags(detail, expected, recommendations.run);
      }).toPass(SETTLE);
    });
  });

  test('the window picker offers the configured presets and opens a custom run', async ({
    page,
  }) => {
    await openActions(page);
    await expectSectionsLoaded(page);
    const config = await requireJson<ConfigView>(page.request, CONFIG_PATH);
    const { presets, default_days: defaultDays } = config.window;

    const picker = page.getByRole('group', { name: WINDOW_PICKER_LABEL, exact: true });
    await expect(picker).toBeVisible();
    const names = (await picker.getByRole('button').allInnerTexts()).map((name) => name.trim());
    const shown = names.flatMap((name) => {
      const match = PRESET_BUTTON.exec(name);
      const days = match?.[1];
      if (days === undefined) {
        return [];
      }
      return [{ days: Number(days), isDefault: match?.[2] !== undefined }];
    });
    expect(
      names.filter((name) => !PRESET_BUTTON.test(name)),
      'besides the presets, the picker offers only the custom range',
    ).toEqual([CUSTOM_RANGE_LABEL]);
    expect(
      shown.map((preset) => preset.days).toSorted((left, right) => left - right),
      'the picker offers each configured preset once',
    ).toEqual([...new Set(presets)].toSorted((left, right) => left - right));
    expect(
      shown.filter((preset) => preset.isDefault).map((preset) => preset.days),
      'exactly one preset is marked default, the configured default',
    ).toEqual([defaultDays]);
    await expect(
      picker.getByRole('button', { name: `${presetLabel(defaultDays)} (default)`, exact: true }),
      'the default run selects the default preset',
    ).toHaveAttribute('aria-pressed', 'true');

    const days = presets.find((preset) => preset !== defaultDays);
    if (days === undefined) {
      note(`window.presets ${JSON.stringify(presets)} holds no non-default window to open`);
      return;
    }

    const preset = picker.getByRole('button', { name: presetLabel(days), exact: true });
    const busy = page.getByText(RUN_IN_PROGRESS_MESSAGE, { exact: true });
    await expect(async () => {
      if (CUSTOM_RUN_URL.test(page.url())) {
        return;
      }
      await preset.click({ timeout: CUSTOM_RUN_OPEN_TIMEOUT_MS });
      await expect
        .poll(
          async () => {
            if (CUSTOM_RUN_URL.test(page.url())) {
              return 'opened';
            }
            return (await busy.isVisible()) ? 'busy' : 'computing';
          },
          {
            message: `choosing ${presetLabel(days)} opens a custom run or reports the lock`,
            timeout: CUSTOM_RUN_OPEN_TIMEOUT_MS,
          },
        )
        .not.toBe('computing');
      if (!CUSTOM_RUN_URL.test(page.url())) {
        throw new Error(`${RUN_IN_PROGRESS_MESSAGE} Retrying ${presetLabel(days)}.`);
      }
    }).toPass(CUSTOM_RUN_RETRY);

    const runParam = new URL(page.url()).searchParams.get('run');
    if (runParam === null) {
      throw new Error(`${page.url()} carries no run parameter`);
    }
    const custom = await requireJson<Run>(page.request, `/api/v1/runs/${runParam}`);
    expect(custom.kind, `run ${custom.id} is a custom run`).toBe('custom');
    expect(custom.days, `run ${custom.id} spans the chosen preset`).toBe(days);
    await expectSectionsLoaded(page);
    const banner = page.locator(CUSTOM_BANNER_SELECTOR);
    await expect(banner).toContainText(
      `Custom window ${custom.window_start} – ${custom.window_end}`,
    );

    await banner.getByRole('link', { name: BACK_TO_DEFAULT_LABEL, exact: true }).click();
    await expect(page, 'Back to default opens /actions without a run').toHaveURL(
      (url) => url.pathname === ACTIONS_PATH && !url.searchParams.has('run'),
    );
    await expectSectionsLoaded(page);
    await expect(page.locator(CUSTOM_BANNER_SELECTOR)).toHaveCount(0);
  });

  test('snoozing a card hides it and its section reports it', async ({ page }) => {
    await openActions(page);

    const attempt: { submitted?: SubmittedFeedback } = {};
    await expect(async () => {
      attempt.submitted = undefined;
      await reloadActions(page);
      const target = chooseFeedbackTarget(await fetchRecommendations(page));
      if (target === null) {
        return;
      }
      const { actions, gone } = await revealTarget(page, target);
      // Every non-C0 item without a decision subject offers Mark done, Dismiss and Snooze, so a
      // missing Snooze is a defect this test reports; it never falls back to Dismiss, which
      // would hide a broken Snooze label.
      for (const label of Object.values(FEEDBACK_BUTTONS)) {
        await expect(
          actions.getByRole('button', { name: label, exact: true }),
          `item ${target.item.id} offers "${label}"`,
        ).toBeVisible({ timeout: READ_TIMEOUT_MS });
      }
      await expect(
        actions.getByRole('button', { name: RECORD_DECISION_LABEL, exact: true }),
        `item ${target.item.id} has no decision subject, so no "${RECORD_DECISION_LABEL}"`,
      ).toHaveCount(0);
      const [response] = await Promise.all([
        page.waitForResponse(
          (candidate) =>
            candidate.request().method() === 'POST' &&
            new URL(candidate.url()).pathname ===
              `/api/v1/recommendations/${target.item.id}/feedback`,
          { timeout: CARD_REMOVAL_TIMEOUT_MS },
        ),
        actions
          .getByRole('button', { name: FEEDBACK_BUTTONS[FEEDBACK_KIND], exact: true })
          .click({ timeout: READ_TIMEOUT_MS }),
      ]);
      expect(response.status(), `the snooze of item ${target.item.id} is recorded`).toBe(201);
      attempt.submitted = { target, gone };
    }).toPass(SETTLE);

    const submitted = attempt.submitted;
    if (submitted === undefined) {
      note('No section shows two items with one open to feedback; nothing was snoozed');
      return;
    }
    const { target, gone } = submitted;
    const hiddenWord = SUPPRESSION_LABELS[FEEDBACK_KIND];
    await expect(gone, `item ${target.item.id} leaves the page once ${hiddenWord}`).toHaveCount(
      0,
      { timeout: CARD_REMOVAL_TIMEOUT_MS },
    );

    // Until the coalesced rerun lands, the run still holds the item, hidden live by the
    // overlay; afterwards the new run leaves the rule and entity out and stores the count.
    await expect(async () => {
      await reloadActions(page);
      const recommendations = await fetchRecommendations(page);
      const runId = recommendations.run.id;
      const section = sectionOf(recommendations, target.sectionKey);
      const items = allItems(section);
      const stored = section.suppressed[`feedback_${FEEDBACK_KIND}`] ?? 0;
      if (runId === target.runId) {
        const same = items.find((item) => item.id === target.item.id);
        expect(same?.current.hidden_by, `run ${runId} marks item ${target.item.id} hidden`).toBe(
          FEEDBACK_KIND,
        );
      } else {
        if (target.item.entity.id !== null) {
          expect(
            items.some((item) => !isHidden(item) && sameRuleAndEntity(item, target.item)),
            `run ${runId} leaves the ${hiddenWord} rule and entity out`,
          ).toBe(false);
        }
        expect(stored, `run ${runId} stores the ${hiddenWord} count`).toBeGreaterThan(0);
      }

      const region = sectionRegion(page, target.sectionKey);
      await expect(region.locator(`#item-${target.item.id}`)).toHaveCount(0);
      const notShown = region.getByRole('list', { name: NOT_SHOWN_LABEL, exact: true });
      const hiddenTotal = items.filter(isHidden).length;
      if (hiddenTotal > 0) {
        await expect(notShown).toContainText(
          `${COUNT_FORMAT.format(hiddenTotal)} hidden since this run`,
        );
      }
      if (stored > 0) {
        await expect(notShown).toContainText(`${COUNT_FORMAT.format(stored)} ${hiddenWord}`);
      }
    }).toPass(SUPPRESSION_RETRY);
  });

  test('discrepancy cards offer their resolver and no feedback action', async ({ page }) => {
    await openActions(page);
    if (discrepancyItems(await fetchRecommendations(page)).length === 0) {
      note('No C0 item in this run; decisions.spec.ts covers C0 deterministically');
      return;
    }

    const checked: { count: number } = { count: 0 };
    await expect(async () => {
      checked.count = 0;
      await reloadActions(page);
      const items = discrepancyItems(await fetchRecommendations(page));
      const group = page.locator(DISCREPANCY_GROUP_SELECTOR);
      for (const item of items) {
        const card = group.locator(`#item-${item.id}`);
        await expect(card, `C0 card item-${item.id} sits in the discrepancy group`).toBeVisible({
          timeout: READ_TIMEOUT_MS,
        });
        await expect(
          card
            .getByRole('button', { name: RESOLVER_BUTTON })
            .or(card.getByText(RESOLVED_SINCE_RUN, { exact: true }))
            .first(),
          `C0 card item-${item.id} offers its resolutions`,
        ).toBeVisible();
        for (const label of Object.values(FEEDBACK_BUTTONS)) {
          await expect(
            card.getByRole('button', { name: label, exact: true }),
            `C0 card item-${item.id} has no "${label}" action`,
          ).toHaveCount(0);
        }
        checked.count += 1;
      }
    }).toPass(SETTLE);
    if (checked.count === 0) {
      note('The discrepancies closed before their cards could be checked');
    }
  });
});
