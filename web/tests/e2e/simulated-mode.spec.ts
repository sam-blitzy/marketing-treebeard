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
 * 4. **Cards**: no recommendation is `live`, every one carries the `simulated` flag, and every
 *    visible card on `/actions` shows its "Simulated data" badge.
 * 5. **Sources page**: each `#source-{source}` section shows the "Simulated" mode badge and the
 *    API's reason.
 *
 * Harness (`playwright.config.ts`): the `chromium` project starts from the session that
 * `auth.setup.ts` saved, so this file never signs in. The spec is read-only: it records no
 * feedback, decision or sync request, so it cannot disturb the specs that share the stack.
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

import type { Item, RecommendationsPage, Run, Source, SourceName } from '../../src/api/types';

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

  test('every recommendation is simulated and every visible card shows "Simulated data"', async ({
    page,
  }) => {
    await expect(async () => {
      const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
      const items = allItems(recommendations);
      expect(items.length, 'the latest default run has recommendations').toBeGreaterThan(0);
      for (const item of items) {
        const what = `item ${String(item.id)} (${item.rule_id})`;
        expect(item.data_status, `${what} data status at compute time`).not.toBe('live');
        expect(item.current.data_status_now, `${what} data status now`).not.toBe('live');
        expect(hasSimulatedFlag(item), `${what} carries the ${SIMULATED_FLAG_CODE} flag`).toBe(
          true,
        );
      }

      await page.goto(ACTIONS_PATH);
      await expect(pageTitle(page, 'Actions')).toBeVisible();

      // The "Simulated data" badge in a card's own flags row, so no other text on the card can
      // stand in for it.
      const simulatedBadge = page
        .getByRole('list', { name: 'Flags', exact: true })
        .getByText(SIMULATED_FLAG_TEXT);
      let checked = 0;
      for (const section of recommendations.sections) {
        const region = page.locator(`#section-${section.key}`);
        await expect(region, `the ${section.key} section is shown`).toBeVisible();
        // Held cards sit in a collapsed disclosure and keyword cards outside the selected tab
        // in a hidden panel; only the cards on screen are checked.
        const cards = region.locator(CARD_SELECTOR).filter({ visible: true });
        const count = await cards.count();
        await expect(
          cards.filter({ hasNot: simulatedBadge }),
          `every visible ${section.key} card shows "${SIMULATED_FLAG_TEXT}"`,
        ).toHaveCount(0);
        checked += count;
      }
      expect(checked, 'at least one recommendation card is visible').toBeGreaterThan(0);
    }).toPass(SETTLE);
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
