/**
 * Simulated data is labelled everywhere (AAP 0.3.3 Labelling, FR-11, 0.11.2 "Simulated run").
 *
 * With no source credentials (the default first start: `make init`, then `docker compose up`)
 * or with `DATA_MODE=simulated`, every source runs on seeded simulated data, and the product
 * must say so wherever that data is shown:
 *
 * 1. **API**: `GET /api/v1/sources` lists exactly Google, LinkedIn and HubSpot, each with mode
 *    `simulated`, an active simulated dataset and the reason for its mode: "credentials not
 *    configured" under `DATA_MODE=auto`, "DATA_MODE=simulated" when the mode is forced
 *    (`backend/src/ads_engine/sync/mode.py`, `REASON_NO_CREDENTIALS` and `REASON_FORCED`).
 * 2. **Source chips**: the `/actions` status bar ("Data sources") shows a "Simulated" chip for
 *    every source and no "Live" chip.
 * 3. **Global banner**: the simulated-sources banner names every source with its reason. It is
 *    plain content, never the `role="alert"` banner, which AAP 0.5.5 reserves for discrepancies.
 * 4. **Every item**: no recommendation is `live` and every one carries the `simulated` flag. On
 *    `/actions`, every item that feedback does not hide shows its own "Simulated data" label,
 *    matched to the item by the API: each card's flags row in every group of every section, and
 *    each white-paper row's asset cell and expanded details in every currency cohort's tables.
 *    Each item is revealed first: every Keywords tab is selected, every "Held until
 *    discrepancies are resolved" disclosure opened and every "Show N more" batch shown, and the
 *    number of cards and rows on the page must equal the number of items.
 * 5. **Sources page**: each `#source-{source}` section shows the "Simulated" mode badge and the
 *    API's reason.
 *
 * Harness (`playwright.config.ts`): the `chromium` project starts from the session that
 * `auth.setup.ts` saved, so this file never signs in. The spec is read-only: it records no
 * feedback, decision or sync request, so it cannot disturb the specs that share the stack. Its
 * only clicks reveal content: tabs, held disclosures, "Show N more" batches and row details.
 *
 * Nothing the seeded world contains is written here: entity names, counts and window lengths
 * all come from the API. `process.env.DATA_MODE` is read only to choose which of the two
 * reason literals the API must report (normalised as `settings.py` normalises it: trimmed and
 * lowercased, blank meaning `auto`); every other expected text is taken from the API itself.
 *
 * Checks that depend on the current run retry with a fresh page load, because an analysis
 * rerun triggered by an earlier spec or a sync may be replacing the run while they read it.
 *
 * @example
 * ```sh
 * make init ADMIN_PASSWORD=pw && docker compose up -d
 * cd web && E2E_ADMIN_PASSWORD=pw npx playwright test simulated-mode.spec.ts
 * # forced simulation: DATA_MODE=simulated in .env, and exported to Playwright as well
 * cd web && DATA_MODE=simulated E2E_ADMIN_PASSWORD=pw npx playwright test simulated-mode.spec.ts
 * ```
 *
 * @packageDocumentation
 */
import { test, expect } from '@playwright/test';
import type { Locator, Page } from '@playwright/test';

import type {
  Flag,
  FlagCode,
  Group,
  Item,
  RecommendationsPage,
  Run,
  Section,
  SectionKey,
  Source,
  SourceName,
} from '../../src/api/types';

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** Budget of every test: the first test may wait for the worker's first default run. */
const SPEC_TIMEOUT_MS = 10 * 60_000;

/**
 * How long the worker may take on first start to build and backfill the simulated datasets and
 * finish the first default analysis run. It leaves a minute of {@link SPEC_TIMEOUT_MS} for the
 * test's own checks.
 */
const FIRST_RUN_TIMEOUT_MS = 9 * 60_000;

/** Poll cadence while waiting for the first default run. */
const FIRST_RUN_INTERVALS_MS = [5_000, 10_000, 15_000];

/** Bound on one `/runs/latest` probe, so a hung connection never stalls the poll. */
const PROBE_TIMEOUT_MS = 10_000;

/**
 * Retry budget of a check that reads the current run: a rerun queued by an earlier spec or a
 * sync may replace the run while the check reads it, so it reloads and tries again.
 */
const SETTLE = { timeout: 120_000, intervals: [5_000, 10_000] };

/** The latest succeeded default run; 404 until the first one has succeeded. */
const RUNS_LATEST_PATH = '/api/v1/runs/latest';

/** Health, mode, active dataset and freshness per source. */
const SOURCES_PATH = '/api/v1/sources';

/** The five sections of the latest default run, with the current-state overlay and banners. */
const RECOMMENDATIONS_PATH = '/api/v1/recommendations';

/** The action list, the default page after sign-in. */
const ACTIONS_PATH = '/actions';

/** The source health page. */
const SOURCES_PAGE_PATH = '/sources';

/** The sources, in the display order `GET /sources` and the status bar use. */
const SOURCE_ORDER: readonly SourceName[] = ['google', 'linkedin', 'hubspot'];

/** Display name of each source, equal to `sourceLabel` in `src/lib/format.ts`. */
const SOURCE_LABELS: Readonly<Record<SourceName, string>> = {
  google: 'Google',
  linkedin: 'LinkedIn',
  hubspot: 'HubSpot',
};

/** Mode reason under `DATA_MODE=auto` for a source without credentials (`REASON_NO_CREDENTIALS`). */
const REASON_NO_CREDENTIALS = 'credentials not configured';

/** Mode reason of every source while `DATA_MODE=simulated` forces simulation (`REASON_FORCED`). */
const REASON_FORCED = 'DATA_MODE=simulated';

/** The banner code of the simulated-sources banner (`analysis.context.BANNER_SIMULATED_SOURCES`). */
const SIMULATED_BANNER_CODE = 'simulated_sources';

/** The flag code every item computed from simulated data carries. */
const SIMULATED_FLAG_CODE = 'simulated';

/** The text the simulated flag's badge starts with ("Simulated data (Google, LinkedIn)"). */
const SIMULATED_FLAG_TEXT = 'Simulated data';

/** The mode chip and badge of a simulated source (`SourceChips.tsx`, `SourcesPage.tsx`). */
const SIMULATED_LABEL = 'Simulated';

/** The mode chip and badge of a live source; it must appear nowhere in this spec's scope. */
const LIVE_LABEL = 'Live';

/** Every recommendation card is an `<article id="item-{id}">` (`RecommendationCard.tsx`). */
const CARD_SELECTOR = 'article[id^="item-"]';

/**
 * The five sections of `/actions`, in the fixed order `GET /recommendations` returns them
 * (`SECTION_ORDER` in `src/api/types.ts`, restated because this spec imports only types).
 */
const SECTION_KEYS: readonly SectionKey[] = [
  'keywords',
  'campaigns',
  'budget',
  'creative',
  'white_papers',
];

/** The accessible name of an item's flags row, one badge per flag code (`FlagList.tsx`). */
const FLAGS_LABEL = 'Flags';

/** The accessible name of the Keywords section's tab list (`KeywordsSection.tsx`). */
const KEYWORD_TABS_LABEL = 'Keyword actions';

/**
 * The control that mounts the next batch of a bounded list of items (`showMoreLabel` in
 * `batches.tsx`): cards ("Show 20 more actions"), group regions ("Show 1 more group") and
 * white-paper asset rows ("Show 50 more white papers"). The per-creative and variants tables hold
 * no item, so their controls are left alone.
 */
const SHOW_MORE_CONTROL = /^Show \d[\d,]* more (?:actions?|groups?|white papers?)$/u;

/** The toggle of a group's held items (`HeldDisclosure` in `SectionFrame.tsx`). */
const HELD_DISCLOSURE = /^Held until discrepancies are resolved \(\d[\d,]*\)$/u;

/** Every character a group anchor id replaces with `-` (`groupAnchorId` in `SectionFrame.tsx`). */
const ANCHOR_UNSAFE = /[^A-Za-z0-9_-]/gu;

/** The key prefix of a white-paper currency cohort, `cohort:{CURRENCY}` (`api/schemas.py`). */
const COHORT_KEY_PREFIX = 'cohort:';

/**
 * The caption suffix of a cohort's table of rows computed from replaced data
 * (`WhitePapersSection.tsx`), after "White papers ({currency})".
 */
const HISTORICAL_TABLE_SUFFIX = ' computed from replaced data';

/**
 * The caption suffix of a cohort's table of held rows, behind its held disclosure
 * (`WhitePapersSection.tsx`), after "White papers ({currency})".
 */
const HELD_TABLE_SUFFIX = ' held until discrepancies are resolved';

/**
 * The data rows of a white-paper asset table, relative to the table: the body's own rows that
 * carry a row header. A row's detail row carries none, and the rows of the per-creative table
 * nested in it are not the body's own. XPath, because Playwright's CSS engine does not resolve
 * a relative `:has(> …)` against the scope.
 */
const ASSET_ROW_SELECTOR = 'xpath=./tbody/tr[th[@scope="row"]]';

/** The row header of an asset row, relative to the row: the asset's name and its badge. */
const ROW_HEADER_SELECTOR = 'xpath=./th[@scope="row"]';

/**
 * Bound on one reveal click or attribute read, so a control that is missing fails the attempt
 * and its retry reloads the page, instead of waiting for the test's timeout.
 */
const ACTION_TIMEOUT_MS = 15_000;

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * Reads one API resource with the signed-in session and returns its JSON body.
 *
 * `page.request` shares the browser context's cookies, so the read is authenticated by the
 * session `auth.setup.ts` saved. A status other than 200 fails the calling check.
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
 * @returns `200` once a default run has succeeded.
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
 * Waits until the worker's first default analysis run has succeeded, and returns it.
 *
 * On first start the worker resolves every source, builds and backfills the simulated datasets
 * and runs the default analysis before `/actions` has anything to show; until then
 * `/runs/latest` answers 404.
 *
 * @param page - The test's page.
 * @returns The latest succeeded default run.
 */
async function waitForDefaultRun(page: Page): Promise<Run> {
  await expect
    .poll(() => probeLatestRun(page), {
      message: `the first default analysis run has succeeded (${RUNS_LATEST_PATH} answers 200)`,
      timeout: FIRST_RUN_TIMEOUT_MS,
      intervals: FIRST_RUN_INTERVALS_MS,
    })
    .toBe(200);
  return getJson<Run>(page, RUNS_LATEST_PATH);
}

/**
 * Whether the stack is expected to force simulation. The backend normalises `DATA_MODE` by
 * trimming and lowercasing it, and a blank value means `auto`; this reads it the same way.
 */
function forcedSimulation(): boolean {
  return (process.env.DATA_MODE ?? '').trim().toLowerCase() === 'simulated';
}

/** The mode reason every source must report under the `DATA_MODE` this run was started with. */
function expectedModeReason(): string {
  return forcedSimulation() ? REASON_FORCED : REASON_NO_CREDENTIALS;
}

/**
 * Returns `value` trimmed, or `null` when it is missing or holds only whitespace.
 *
 * @param value - A text field of an API response.
 * @returns The visible text, or `null`.
 */
function visibleText(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? '';
  return trimmed === '' ? null : trimmed;
}

/**
 * The entry of `source` in a `GET /sources` response.
 *
 * @param sources - The response body.
 * @param source - The source to find.
 * @returns Its entry.
 * @throws Error when the response does not list the source.
 */
function sourceEntry(sources: readonly Source[], source: SourceName): Source {
  const entry = sources.find((candidate) => candidate.source === source);
  if (entry === undefined) {
    throw new Error(`${SOURCES_PATH} does not list ${source}`);
  }
  return entry;
}

/**
 * The reason the simulated-sources banner quotes for a source, read from `GET /sources`.
 *
 * The banner (`analysis/overlay.py`) names the reason of the source's active dataset, and the
 * connection's reason only when the dataset records none. Both are the mode reason when the
 * dataset is built, so on a fresh stack this equals {@link Source.reason}. They part only when
 * a stack that already holds simulated datasets restarts under the other `DATA_MODE`: the
 * connection then reports the new mode reason while the active dataset, which is kept rather
 * than rebuilt, keeps the reason it was built with. Expecting the dataset's reason keeps this
 * check true to what the banner states in both cases.
 *
 * @param source - The source's entry from `GET /sources`.
 * @returns The reason text.
 * @throws Error when neither the dataset nor the connection records a reason.
 */
function bannerReason(source: Source): string {
  const reason = visibleText(source.active_dataset?.reason) ?? visibleText(source.reason);
  if (reason === null) {
    throw new Error(`${source.source} reports no reason for its simulated mode`);
  }
  return reason;
}

/**
 * Every item of a recommendations page, across all sections and groups.
 *
 * @param recommendations - The `GET /recommendations` body.
 * @returns The items, in section, group and rank order.
 */
function allItems(recommendations: RecommendationsPage): Item[] {
  return recommendations.sections.flatMap((section) =>
    section.groups.flatMap((group) => group.items),
  );
}

/**
 * Whether an item carries the `simulated` flag, recorded with the run or added by the
 * read-time overlay; the card shows the union of both lists.
 *
 * @param item - One item.
 * @returns `true` when either list holds the flag.
 */
function hasSimulatedFlag(item: Item): boolean {
  return [...item.flags, ...item.current.flags_now].some(
    (flag) => flag.code === SIMULATED_FLAG_CODE,
  );
}

/**
 * The page's single level-1 heading with the given name.
 *
 * @param page - The test's page.
 * @param name - The page title, for example "Actions".
 * @returns Its locator.
 */
function pageTitle(page: Page, name: string): Locator {
  return page.getByRole('heading', { level: 1, name, exact: true });
}

/**
 * An item as assertion messages name it.
 *
 * @param item - One item.
 * @returns For example "item 12 (K1)".
 */
function describeItem(item: Item): string {
  return `item ${String(item.id)} (${item.rule_id})`;
}

/**
 * The section with `key` in a recommendations page.
 *
 * @param recommendations - The `GET /recommendations` body.
 * @param key - The section to find.
 * @returns The section.
 * @throws Error when the page has no such section.
 */
function sectionOf(recommendations: RecommendationsPage, key: SectionKey): Section {
  const section = recommendations.sections.find((candidate) => candidate.key === key);
  if (section === undefined) {
    throw new Error(`${RECOMMENDATIONS_PATH} has no ${key} section`);
  }
  return section;
}

/**
 * A group's items as `/actions` places them (`partitionGroup` in `SectionFrame.tsx`), each list
 * in API order. Items that feedback hides now are counted by the page and never rendered, so
 * they are in none of the lists.
 */
interface ShownItems {
  /** Items neither hidden, historical nor held: shown openly. */
  visible: Item[];
  /** Items computed from replaced data: shown openly, in a block or table of their own. */
  historical: Item[];
  /** Items held until discrepancies are resolved: behind the group's held disclosure. */
  held: Item[];
}

/**
 * Splits a group's items as the page does: hidden when `current.hidden_by` is set, else
 * historical when `current.historical` is set, else held when the run or the overlay holds it,
 * else visible.
 *
 * @param group - One group of a section.
 * @returns Its items that the page renders, by where it renders them.
 */
function shownItems(group: Group): ShownItems {
  const shown: ShownItems = { visible: [], historical: [], held: [] };
  for (const item of group.items) {
    const hiddenBy = item.current.hidden_by;
    if (hiddenBy !== null && hiddenBy !== undefined) {
      continue;
    }
    if (item.current.historical) {
      shown.historical.push(item);
    } else if (item.held || item.current.held_now) {
      shown.held.push(item);
    } else {
      shown.visible.push(item);
    }
  }
  return shown;
}

/**
 * Every item of a group that the page renders, in any of its places.
 *
 * @param group - One group of a section.
 * @returns The visible, historical and held items.
 */
function allShown(group: Group): Item[] {
  const { visible, historical, held } = shownItems(group);
  return [...visible, ...historical, ...held];
}

/**
 * How many reveal clicks a set of groups can need at most: each click mounts at least one more
 * card, row or group region, or opens one group's held disclosure.
 *
 * @param groups - The groups the clicks reveal.
 * @returns One click per item, plus two per group.
 */
function revealBound(groups: readonly Group[]): number {
  return groups.reduce((sum, group) => sum + group.items.length + 2, 0);
}

/**
 * The anchor id of a group's region or Keywords tab panel (`groupAnchorId` in
 * `SectionFrame.tsx`): `group-{sectionKey}-{groupKey}`, with every character outside
 * `[A-Za-z0-9_-]` replaced by `-`.
 *
 * @param sectionKey - The section the group belongs to.
 * @param groupKey - The group key, as the API returned it.
 * @returns For example `group-white_papers-cohort-EUR` for `cohort:EUR`.
 */
function groupAnchorId(sectionKey: SectionKey, groupKey: string): string {
  return `group-${sectionKey}-${groupKey}`.replace(ANCHOR_UNSAFE, '-');
}

/**
 * The title a group's tab or heading shows: its API title trimmed, else its key.
 *
 * @param group - One group of a section.
 * @returns The title.
 */
function groupTitle(group: Group): string {
  return visibleText(group.title) ?? group.key;
}

/**
 * The currency of a white-paper cohort: the `{CURRENCY}` of its `cohort:{CURRENCY}` key, else
 * its title, which the API sets to the currency.
 *
 * @param group - One group of the White papers section.
 * @returns The currency code, for example "EUR".
 */
function cohortCurrency(group: Group): string {
  const fromKey = group.key.startsWith(COHORT_KEY_PREFIX)
    ? visibleText(group.key.slice(COHORT_KEY_PREFIX.length))
    : null;
  return fromKey ?? groupTitle(group);
}

/**
 * The name a white-paper row shows (`assetName` in `WhitePapersSection.tsx`): the entity name
 * trimmed, else the entity id, else "White paper {id}".
 *
 * @param item - One white-paper item.
 * @returns The asset name.
 */
function assetName(item: Item): string {
  return (
    visibleText(item.entity.name) ?? visibleText(item.entity.id) ?? `White paper ${String(item.id)}`
  );
}

/**
 * `text` escaped for use inside a regular expression.
 *
 * @param text - Literal text.
 * @returns The pattern that matches exactly that text.
 */
function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
}

/**
 * An attribute selector matching `value` exactly, whatever characters it holds, such as the
 * colons of a React-generated id.
 *
 * @param attribute - The attribute name.
 * @param value - Its exact value.
 * @returns For example `[id="item-12"]`.
 */
function attributeSelector(attribute: string, value: string): string {
  return `[${attribute}=${JSON.stringify(value)}]`;
}

/**
 * The flag badges an item's flags row shows (`mergeFlags` in `FlagList.tsx`), in their order:
 * every code once, the recorded codes first, then those only the overlay carries, each labelled
 * with its current message, else its recorded one, else its code.
 *
 * @param item - One item.
 * @returns The flags as the row shows them.
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

/** An item's simulated-data label: the badge text and where its flags row shows it. */
interface SimulatedLabel {
  /** The badge text, for example "Simulated data (Google)". */
  message: string;
  /** The badge's position in the item's flags row, from 0. */
  position: number;
}

/**
 * The label an item's simulated flag shows, on its card's flags row, a white-paper row's asset
 * cell and that row's details alike (`simulatedFlag` in `WhitePapersSection.tsx` reads the same
 * merged flag).
 *
 * @param item - One item.
 * @returns The label and its position in the flags row.
 * @throws Error when the item carries no simulated flag.
 */
function simulatedLabel(item: Item): SimulatedLabel {
  const flags = shownFlags(item);
  const position = flags.findIndex((flag) => flag.code === SIMULATED_FLAG_CODE);
  const flag = flags[position];
  if (flag === undefined) {
    throw new Error(`${describeItem(item)} carries no ${SIMULATED_FLAG_CODE} flag`);
  }
  return { message: flag.message, position };
}

/**
 * Clicks every reveal control in `scope` until none is left: each collapsed "Held until
 * discrepancies are resolved" disclosure and each "Show N more" button of a list of cards,
 * groups or white papers. A reveal can bring more controls into view (a revealed group holds its
 * own lists and disclosure), so the controls are looked up again after every click. Controls in
 * a hidden Keywords panel are not visible and stay untouched.
 *
 * @param scope - The section region or tab panel to reveal.
 * @param bound - The most clicks the scope can need ({@link revealBound}).
 * @param where - The scope, as assertion messages name it.
 * @throws Error when controls remain after `bound` clicks.
 */
async function revealAll(scope: Locator, bound: number, where: string): Promise<void> {
  const held = scope
    .getByRole('button', { name: HELD_DISCLOSURE, expanded: false })
    .filter({ visible: true });
  const more = scope.getByRole('button', { name: SHOW_MORE_CONTROL }).filter({ visible: true });
  for (let clicks = 0; ; clicks += 1) {
    const heldCount = await held.count();
    const moreCount = heldCount > 0 ? 0 : await more.count();
    if (heldCount === 0 && moreCount === 0) {
      return;
    }
    if (clicks >= bound) {
      throw new Error(`${where} still shows reveal controls after ${String(bound)} clicks`);
    }
    if (heldCount > 0) {
      const region = await held.first().getAttribute('aria-controls', {
        timeout: ACTION_TIMEOUT_MS,
      });
      if (region === null) {
        throw new Error(`A held disclosure in ${where} controls no region`);
      }
      // Pinned by the region it controls: "expanded: false" would let go of it once clicked.
      const toggle = scope.locator(attributeSelector('aria-controls', region));
      await toggle.click({ timeout: ACTION_TIMEOUT_MS });
      await expect(toggle, `the held disclosure in ${where} opens`).toHaveAttribute(
        'aria-expanded',
        'true',
      );
    } else {
      await more.first().click({ timeout: ACTION_TIMEOUT_MS });
    }
  }
}

/**
 * Asserts that `container`, an item's card or a white-paper row's details, shows the item's own
 * simulated-data badge: in its own flags row, at the position the item's merged flags give it,
 * with the API's message, which names simulated data.
 *
 * @param container - The element that holds the item's flags row and no other item's.
 * @param item - The item.
 */
async function expectFlagsRowLabel(container: Locator, item: Item): Promise<void> {
  const label = simulatedLabel(item);
  expect(
    label.message.trim().startsWith(SIMULATED_FLAG_TEXT),
    `${describeItem(item)} labels its flag "${SIMULATED_FLAG_TEXT}…" ("${label.message}")`,
  ).toBe(true);
  const badge = container
    .getByRole('list', { name: FLAGS_LABEL, exact: true })
    .getByRole('listitem')
    .nth(label.position);
  await expect(badge, `${describeItem(item)} shows "${label.message}" in its flags row`).toHaveText(
    label.message,
  );
  await expect(badge).toBeVisible();
}

/**
 * Asserts that each item is shown as its own card inside `scope` and that the card's flags row
 * shows the item's simulated-data badge.
 *
 * @param scope - The item's group region or Keywords tab panel.
 * @param items - The group's items that the page renders.
 */
async function expectCards(scope: Locator, items: readonly Item[]): Promise<void> {
  for (const item of items) {
    const card = scope.locator(attributeSelector('id', `item-${String(item.id)}`));
    await expect(card, `${describeItem(item)} is shown as a card in its group`).toBeVisible();
    await expectFlagsRowLabel(card, item);
  }
}

/**
 * Checks a section whose items are cards (Campaigns, Budget, Copy/creative): reveals every
 * batch and held disclosure, then every item's card in its own group region, and requires the
 * section to render no card beyond them.
 *
 * @param region - The section region.
 * @param section - The section, as the API returned it.
 * @returns How many items were found and checked on the page.
 */
async function checkCardSection(region: Locator, section: Section): Promise<number> {
  await revealAll(region, revealBound(section.groups), `the ${section.key} section`);
  let checked = 0;
  for (const group of section.groups) {
    const items = allShown(group);
    if (items.length === 0) {
      continue;
    }
    const groupRegion = region.locator(
      attributeSelector('id', groupAnchorId(section.key, group.key)),
    );
    await expect(groupRegion, `the ${section.key} group "${group.key}" is shown`).toBeVisible();
    await expectCards(groupRegion, items);
    checked += items.length;
  }
  await expect(
    region.locator(CARD_SELECTOR),
    `the ${section.key} section renders one card per item not hidden by feedback`,
  ).toHaveCount(checked);
  return checked;
}

/**
 * Checks the Keywords section: selects each group's tab in turn, reveals every batch and held
 * disclosure in its panel, then every item's card in that panel. Once every tab has been
 * selected, every panel's cards are mounted, and the section must hold no card beyond them.
 *
 * @param region - The section region.
 * @param section - The Keywords section, as the API returned it.
 * @returns How many items were found and checked on the page.
 */
async function checkKeywordsSection(region: Locator, section: Section): Promise<number> {
  // With nothing to show the section renders its empty state, and no tab list.
  if (section.groups.every((group) => allShown(group).length === 0)) {
    return 0;
  }
  const tabList = region.getByRole('tablist', { name: KEYWORD_TABS_LABEL, exact: true });
  await expect(tabList.getByRole('tab'), 'one Keywords tab per API group').toHaveCount(
    section.groups.length,
  );
  let checked = 0;
  for (const group of section.groups) {
    const title = groupTitle(group);
    // The tab's name is its title, followed by the count of the cards it shows openly.
    const tab = tabList.getByRole('tab', {
      name: new RegExp(`^${escapeRegExp(title)}(?: \\d[\\d,]*)?$`, 'u'),
    });
    await tab.click({ timeout: ACTION_TIMEOUT_MS });
    await expect(tab, `the "${title}" tab is selected`).toHaveAttribute('aria-selected', 'true');
    const panel = region.locator(attributeSelector('id', groupAnchorId(section.key, group.key)));
    await expect(panel, `the "${title}" panel is shown`).toBeVisible();
    const items = allShown(group);
    if (items.length === 0) {
      continue;
    }
    await revealAll(panel, revealBound([group]), `the "${title}" tab`);
    await expectCards(panel, items);
    checked += items.length;
  }
  await expect(
    region.locator(CARD_SELECTOR),
    'the keywords section renders one card per item not hidden by feedback',
  ).toHaveCount(checked);
  return checked;
}

/**
 * Checks one white-paper table: it shows exactly one row per item, in API order, and each row
 * names its item's asset and shows the item's simulated-data badge in its asset cell; expanded,
 * the row's details show the same badge in their flags row.
 *
 * @param groupRegion - The cohort's group region.
 * @param caption - The table's caption, which names it.
 * @param items - The items the table holds, in API order.
 */
async function expectAssetRows(
  groupRegion: Locator,
  caption: string,
  items: readonly Item[],
): Promise<void> {
  const table = groupRegion.getByRole('table', { name: caption, exact: true });
  await expect(table, `the table "${caption}" is shown`).toBeVisible();
  const rows = table.locator(ASSET_ROW_SELECTOR);
  await expect(rows, `"${caption}" shows one row per item`).toHaveCount(items.length);
  for (const [index, item] of items.entries()) {
    const name = assetName(item);
    const label = simulatedLabel(item);
    const row = rows.nth(index);
    const what = `${describeItem(item)}, row ${String(index + 1)} of "${caption}"`;
    const header = row.locator(ROW_HEADER_SELECTOR);
    await expect(header.getByText(name, { exact: true }), `${what} names "${name}"`).toBeVisible();
    await expect(
      header.getByText(label.message, { exact: true }),
      `${what} shows "${label.message}" in its asset cell`,
    ).toBeVisible();

    const detailId = await row
      .getByRole('button', { name: `Details for ${name}`, exact: true })
      .getAttribute('aria-controls', { timeout: ACTION_TIMEOUT_MS });
    if (detailId === null) {
      throw new Error(`The details toggle of ${what} controls no detail row`);
    }
    // Pinned by its detail row, which is unique, unlike the asset name across cohorts.
    const toggle = table.locator(attributeSelector('aria-controls', detailId));
    await toggle.click({ timeout: ACTION_TIMEOUT_MS });
    await expect(toggle, `the details of ${what} open`).toHaveAttribute('aria-expanded', 'true');
    const details = table.locator(attributeSelector('id', detailId));
    await expect(details, `the details of ${what} are shown`).toBeVisible();
    await expectFlagsRowLabel(details, item);
  }
}

/**
 * Checks the White papers section: reveals every batch and held disclosure, then, for each
 * currency cohort, its table of visible rows, its table of rows computed from replaced data and
 * its table of held rows, each with exactly its items.
 *
 * @param region - The section region.
 * @param section - The White papers section, as the API returned it.
 * @returns How many items were found and checked on the page.
 */
async function checkWhitePapersSection(region: Locator, section: Section): Promise<number> {
  await revealAll(region, revealBound(section.groups), 'the white_papers section');
  let checked = 0;
  for (const group of section.groups) {
    const { visible, historical, held } = shownItems(group);
    if (visible.length + historical.length + held.length === 0) {
      continue;
    }
    const groupRegion = region.locator(
      attributeSelector('id', groupAnchorId(section.key, group.key)),
    );
    await expect(groupRegion, `the white-paper cohort "${group.key}" is shown`).toBeVisible();
    const caption = `White papers (${cohortCurrency(group)})`;
    const tables: [string, Item[]][] = [
      [caption, visible],
      [`${caption}${HISTORICAL_TABLE_SUFFIX}`, historical],
      [`${caption}${HELD_TABLE_SUFFIX}`, held],
    ];
    for (const [tableCaption, items] of tables) {
      if (items.length > 0) {
        await expectAssetRows(groupRegion, tableCaption, items);
        checked += items.length;
      }
    }
  }
  return checked;
}

/**
 * Finds every item of a section that the page renders and checks its own simulated-data label,
 * in the way the section renders its items.
 *
 * @param region - The section region.
 * @param section - The section, as the API returned it.
 * @returns How many items were found and checked on the page.
 */
function checkSection(region: Locator, section: Section): Promise<number> {
  switch (section.key) {
    case 'keywords':
      return checkKeywordsSection(region, section);
    case 'white_papers':
      return checkWhitePapersSection(region, section);
    default:
      return checkCardSection(region, section);
  }
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

test.describe('simulated mode', () => {
  test.describe.configure({ timeout: SPEC_TIMEOUT_MS });

  // Every test needs a succeeded default run: before it the API has no run to label, and the
  // Actions page shows only its first-run notice. Once the run exists this returns at once.
  test.beforeEach(async ({ page }) => {
    await waitForDefaultRun(page);
  });

  test('the API reports every source as simulated, with the reason for its mode', async ({
    page,
  }) => {
    const reason = expectedModeReason();

    await expect(async () => {
      const run = await getJson<Run>(page, RUNS_LATEST_PATH);
      const sources = await getJson<Source[]>(page, SOURCES_PATH);

      expect(
        sources.map((entry) => entry.source).sort(),
        `${SOURCES_PATH} lists exactly Google, LinkedIn and HubSpot`,
      ).toEqual([...SOURCE_ORDER].sort());

      for (const name of SOURCE_ORDER) {
        const source = sourceEntry(sources, name);
        expect(source.mode, `${name} mode`).toBe('simulated');
        expect(visibleText(source.reason), `${name} reports a reason`).not.toBeNull();
        expect(
          source.reason,
          `${name} reason under DATA_MODE=${forcedSimulation() ? 'simulated' : 'auto'}`,
        ).toBe(reason);
        expect(source.active_dataset?.mode, `${name} active dataset mode`).toBe('simulated');
        expect(source.active_dataset?.state, `${name} active dataset state`).toBe('active');
        // The run froze the mode of each source it read; none of them may be live.
        expect(run.data_modes[name], `${name} mode in the latest default run`).not.toBe('live');
      }
    }).toPass(SETTLE);
  });

  test('the status bar shows a Simulated chip for every source and no Live chip', async ({
    page,
  }) => {
    await expect(async () => {
      const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
      for (const name of SOURCE_ORDER) {
        expect(recommendations.overlay.source_modes[name], `${name} mode in the overlay`).toBe(
          'simulated',
        );
      }

      await page.goto(ACTIONS_PATH);
      await expect(pageTitle(page, 'Actions')).toBeVisible();

      const chips = page.getByRole('list', { name: 'Data sources', exact: true });
      await expect(chips).toBeVisible();
      for (const name of SOURCE_ORDER) {
        const label = SOURCE_LABELS[name];
        const chip = chips
          .getByRole('listitem')
          .filter({ has: page.getByText(label, { exact: true }) });
        await expect(chip, `one chip for ${label}`).toHaveCount(1);
        await expect(
          chip.getByText(SIMULATED_LABEL, { exact: true }),
          `${label} chip reads "${SIMULATED_LABEL}"`,
        ).toBeVisible();
      }
      await expect(
        chips.getByText(LIVE_LABEL, { exact: true }),
        `no source chip reads "${LIVE_LABEL}"`,
      ).toHaveCount(0);
    }).toPass(SETTLE);
  });

  test('a global banner names every simulated source with its reason', async ({ page }) => {
    await expect(async () => {
      const sources = await getJson<Source[]>(page, SOURCES_PATH);
      const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);

      const banner = recommendations.banners.find(
        (candidate) => candidate.code === SIMULATED_BANNER_CODE,
      );
      if (banner === undefined) {
        throw new Error(`${RECOMMENDATIONS_PATH} carries no ${SIMULATED_BANNER_CODE} banner`);
      }
      // Each source is named with its reason, as the overlay writes it: "{Label} ({reason})".
      const mentions = SOURCE_ORDER.map(
        (name) => `${SOURCE_LABELS[name]} (${bannerReason(sourceEntry(sources, name))})`,
      );
      for (const mention of mentions) {
        expect(banner.message, `the API banner names ${mention}`).toContain(mention);
      }

      await page.goto(ACTIONS_PATH);
      await expect(pageTitle(page, 'Actions')).toBeVisible();

      // A non-discrepancy banner renders the API message, verbatim, as its title.
      const shown = page.getByText(banner.message, { exact: true });
      await expect(shown, 'the simulated-sources banner is shown').toBeVisible();
      for (const name of SOURCE_ORDER) {
        await expect(shown).toContainText(SOURCE_LABELS[name]);
      }
      for (const mention of mentions) {
        await expect(shown).toContainText(mention);
      }
      await expect(
        page.getByRole('alert').filter({ hasText: banner.message }),
        'the simulated-sources banner is not an alert',
      ).toHaveCount(0);
    }).toPass(SETTLE);
  });

  test('every recommendation is simulated and every item shows its "Simulated data" label', async ({
    page,
  }) => {
    await test.step('the API reports every recommendation as simulated', async () => {
      await expect(async () => {
        const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
        expect(
          recommendations.sections.map((section) => section.key),
          `${RECOMMENDATIONS_PATH} returns the five sections in order`,
        ).toEqual(SECTION_KEYS);
        // Every item, including those feedback hides now, which the page only counts.
        const items = allItems(recommendations);
        expect(items.length, 'the latest default run has recommendations').toBeGreaterThan(0);
        for (const item of items) {
          const what = describeItem(item);
          expect(item.data_status, `${what} data status at compute time`).not.toBe('live');
          expect(item.current.data_status_now, `${what} data status now`).not.toBe('live');
          expect(hasSimulatedFlag(item), `${what} carries the ${SIMULATED_FLAG_CODE} flag`).toBe(
            true,
          );
        }
      }).toPass(SETTLE);
    });

    // One step per section, each with its own retry: a rerun that replaces the run while a
    // section is read only repeats that section, with a fresh read and a fresh page.
    let checked = 0;
    for (const key of SECTION_KEYS) {
      await test.step(`every ${key} item on /actions shows its own "${SIMULATED_FLAG_TEXT}" label`, async () => {
        let sectionChecked = 0;
        await expect(async () => {
          const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
          const section = sectionOf(recommendations, key);
          const expected = section.groups.reduce((sum, group) => sum + allShown(group).length, 0);

          await page.goto(ACTIONS_PATH);
          await expect(pageTitle(page, 'Actions')).toBeVisible();
          const region = page.locator(`#section-${key}`);
          await expect(region, `the ${key} section is shown`).toBeVisible();

          sectionChecked = await checkSection(region, section);
          expect(
            sectionChecked,
            `every ${key} item not hidden by feedback is found and checked on the page`,
          ).toBe(expected);
        }).toPass(SETTLE);
        checked += sectionChecked;
      });
    }
    expect(checked, 'at least one item is shown on /actions').toBeGreaterThan(0);
  });

  test('the Sources page shows each source as Simulated with its reason', async ({ page }) => {
    await expect(async () => {
      const sources = await getJson<Source[]>(page, SOURCES_PATH);

      await page.goto(SOURCES_PAGE_PATH);
      await expect(pageTitle(page, 'Sources')).toBeVisible();

      for (const name of SOURCE_ORDER) {
        const label = SOURCE_LABELS[name];
        const reason = visibleText(sourceEntry(sources, name).reason);
        if (reason === null) {
          throw new Error(`${name} reports no reason for its simulated mode`);
        }
        const region = page.locator(`#source-${name}`);
        await expect(region.getByRole('heading', { name: label, exact: true })).toBeVisible();
        await expect(
          region.getByText(SIMULATED_LABEL, { exact: true }),
          `${label} mode badge reads "${SIMULATED_LABEL}"`,
        ).toBeVisible();
        await expect(
          region.getByText(LIVE_LABEL, { exact: true }),
          `${label} shows no "${LIVE_LABEL}" badge`,
        ).toHaveCount(0);
        await expect(
          region.getByText(`Reason: ${reason}`, { exact: true }),
          `${label} shows its reason`,
        ).toBeVisible();
      }
    }).toPass(SETTLE);
  });
});
