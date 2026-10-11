/**
 * End-to-end: a decision is recorded and the discrepancy it creates is resolved through the UI
 * (AAP 0.11.2 "Decisions in the UI"; AAP 0.5.5 card footer, `/decisions`, `/sources`;
 * AAP 0.5.4.4 C0; AAP 0.5.4.2 C0 exempt from feedback, banner count read live).
 *
 * The one test walks the whole decision loop against the running stack:
 *
 * 1. waits for the latest default analysis run and picks an actionable campaign item from
 *    "Campaigns to pause or launch", preferring a C1 / C1b pause, whose "Record decision" form
 *    is prefilled with status `paused`, on a campaign with its own budget, so the form offers a
 *    budget amount;
 * 2. enters a budget amount only the server refuses, and checks that the 422's message is shown
 *    in the dialog, which stays open with the entered values and nothing written; then corrects
 *    the amount, records the pause through the same dialog and finds it at the top of the
 *    subject's table in the `/decisions` log;
 * 3. presses "Sync now" for the item's source on `/sources`. The product is recommend-only
 *    (AAP 0.7.2), so the platform still reports the campaign active, and the entity sync raises
 *    a `status` discrepancy against the `paused` decision. The `/actions` discrepancy banner
 *    (the page's only `role="alert"`) then reports the live count of unresolved discrepancies;
 * 4. waits for the coalesced rerun to pin the C0 card in the Campaigns discrepancy group and
 *    checks that it offers its resolutions and no Dismiss, Snooze or Mark done;
 * 5. resolves the discrepancy on `/decisions` with "Platform is correct", which writes the
 *    platform status as the decision and closes the discrepancy at once, and checks that the
 *    banner count drops with it.
 *
 * Shared, mutating stack: other specs and the worker change runs, feedback and discrepancies
 * while this test runs, so every name, id and count is read from the API at the moment it is
 * compared, and every page check retries with a reload. Recommendation ids are per run, so an
 * item is re-picked whenever a rerun replaced the run a page showed.
 *
 * Restoration is guaranteed on every outcome. On the happy path, resolving with "Platform is
 * correct" restores the campaign's decided status. Whatever the outcome (a failed assertion, a
 * timeout, or a discrepancy left awaiting the platform), the `afterEach` teardown then restores
 * what this test changed and nothing else: it resolves the campaign's `status` and `budget`
 * discrepancies that were not unresolved before its first save, and records the campaign's
 * earlier decided state again when its newest decision differs from it. The next run of the
 * suite, a retry included, can therefore pause and resolve the same campaign again: once the
 * teardown settles, the campaign is back at the decided state it had before the first save, and
 * none of the discrepancies the test raised is unresolved. The decisions the test and its
 * teardown write stay in the append-only decision log, and those discrepancies stay on record
 * as resolved. On the happy path the teardown finds nothing to do.
 *
 * Harness rules (`playwright.config.ts`): the session comes from the storage state that
 * `auth.setup.ts` saved, so this file never signs in; reads go through `page.request`, which
 * shares the browser context's cookies; every write the test itself makes goes through the UI,
 * which adds the CSRF header. The teardown is the only API write: it goes through
 * `page.request` with that header, because it must restore the state whatever the UI did. UI
 * labels are mirrored as local constants rather than imported, because the feature modules that
 * own them load React and stylesheets, which the Node-side test runner cannot.
 *
 * @example
 * ```sh
 * make init ADMIN_PASSWORD=pw && docker compose up -d
 * cd web && E2E_ADMIN_PASSWORD=pw npx playwright test tests/e2e/decisions.spec.ts
 * ```
 *
 * @packageDocumentation
 */
import { test, expect } from '@playwright/test';
import type { APIResponse, Locator, Page, Response } from '@playwright/test';

import type {
  CampaignStatus,
  Decision,
  DecisionPatchIn,
  DecisionRequest,
  Discrepancy,
  DiscrepancyKind,
  ErrorResponse,
  FeedbackKind,
  Item,
  RecommendationsPage,
  Resolution,
  ResolveRequest,
  Run,
  SourceName,
  SubjectView,
} from '../../src/api/types';

// ---------------------------------------------------------------------------------------------
// API paths (backend/src/ads_engine/api/routes/*.py, mounted under /api/v1)
// ---------------------------------------------------------------------------------------------

/** The latest succeeded default run; 404 until the worker's first default run succeeds. */
const RUNS_LATEST_PATH = '/api/v1/runs/latest';

/** The five sections of the latest default run, with the read-time overlay. */
const RECOMMENDATIONS_PATH = '/api/v1/recommendations';

/** Unresolved (`open` and `awaiting_platform`) discrepancies of the active datasets. */
const DISCREPANCIES_PATH = '/api/v1/discrepancies';

/** One subject's decision log, newest first. */
const DECISIONS_PATH = '/api/v1/decisions';

/** The largest page `GET /discrepancies` serves (`MAX_LIMIT` in `routes/discrepancies.py`). */
const DISCREPANCY_PAGE_LIMIT = 500;

/** A bound on `Link: rel="next"` pages followed, so a looping header can never hang a read. */
const MAX_DISCREPANCY_PAGES = 50;

/**
 * The CSRF header every mutating request carries, with its exact value (`CSRF_HEADER` and
 * `CSRF_VALUE` of `src/api/client.ts`, mirroring `api/security.py`); without it the API answers
 * 403. Only the teardown sends it itself; the UI's client adds it to every other write.
 */
const CSRF_HEADERS: Readonly<Record<string, string>> = { 'X-Requested-With': 'ads-engine' };

// ---------------------------------------------------------------------------------------------
// UI labels and anchors (mirrored from the feature modules named beside each)
// ---------------------------------------------------------------------------------------------

/** The card footer action and its dialog title (`RecommendationCard.tsx`). */
const RECORD_DECISION_LABEL = 'Record decision';

/** The decision form's submit buttons (`DecisionForm.tsx`). */
const SAVE_DECISION_LABEL = 'Save decision';
const SAVE_INTENDED_STATE_LABEL = 'Save intended state';

/** The decision form's status select (`DecisionForm.tsx`). */
const STATUS_FIELD_LABEL = 'Status';

/** The budget inputs of the decision form, by expected-state field (`DecisionForm.tsx`). */
const AMOUNT_FIELD_LABELS = {
  daily_budget: 'Daily budget',
  total_budget: 'Total budget',
} as const satisfies Record<'daily_budget' | 'total_budget', string>;

/** `CAMPAIGN_STATUS_LABELS` of `DecisionForm.tsx`, shared by the log and the resolver. */
const CAMPAIGN_STATUS_LABELS: Readonly<Record<CampaignStatus, string>> = {
  active: 'Active',
  paused: 'Paused',
  ended: 'Ended',
};

/** `FEEDBACK_LABELS` of `RecommendationCard.tsx`: the actions C0 cards must not offer. */
const FEEDBACK_LABELS: Readonly<Record<FeedbackKind, string>> = {
  done: 'Mark done',
  dismiss: 'Dismiss',
  snooze: 'Snooze for 7 days',
};

/** `RESOLUTION_LABELS` of `DiscrepancyResolver.tsx`, which documents that this spec uses them. */
const RESOLUTION_LABELS: Readonly<Record<Resolution, string>> = {
  platform_correct: 'Platform is correct',
  decision_correct: 'Decision is correct',
  adopt_platform: 'Adopt platform state',
  record_intended: 'Record intended state',
  acknowledge_removal: 'Acknowledge removal',
};

/** The resolver's button that loads its next page of discrepancies (`DiscrepancyResolver.tsx`). */
const SHOW_MORE_DISCREPANCIES_LABEL = 'Show more discrepancies';

/** `/sources` "Sync now" and its two outcomes, 202 and 409 (`SourcesPage.tsx`). */
const SYNC_NOW_LABEL = 'Sync now';
const SYNC_OUTCOME_PATTERN = /Sync requested|A sync is already pending/;

/**
 * The open-discrepancies banner title (`GlobalBanners.tsx`): "{N} open discrepancy to resolve
 * first" or "{N} open discrepancies to resolve first", N grouped with commas (`LOCALE` `en`).
 */
const BANNER_COUNT_PATTERN = /(\d[\d,]*) open discrepanc(?:y|ies) to resolve first/;

/** The Campaigns section and its discrepancy group (`SectionFrame.tsx`). */
const CAMPAIGNS_SECTION_SELECTOR = '#section-campaigns';
const DISCREPANCY_GROUP_SELECTOR = '#group-campaigns-discrepancies';

/** The section and group keys the API returns (`analysis/context.py`). */
const CAMPAIGNS_SECTION_KEY = 'campaigns';
const PAUSE_GROUP_KEY = 'pause';
const DISCREPANCY_GROUP_KEY = 'discrepancies';

/** The rules whose "Record decision" form is prefilled with `paused` (`evidenceExtra.ts`). */
const PAUSE_RULES: ReadonlySet<string> = new Set(['C1', 'C1b']);

/**
 * A budget amount only the server refuses: 10^12, the smallest amount `decisions.py` rejects with
 * 422 `invalid_amount` ("must be a positive amount below 10^12"). The decision form checks an
 * amount's sign and precision but not its size, and the request schema admits up to 18 digits,
 * so the form posts it and the API refuses it and writes nothing. It is a whole number, so no
 * currency's minor unit and no Google micro rule refuses it first.
 */
const SERVER_REFUSED_AMOUNT = '1000000000000';

// ---------------------------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------------------------

/** The whole test: first-start backfill, a sync, a coalesced rerun and the resolution. */
const TEST_TIMEOUT_MS = 15 * 60_000;

/** How long the first default run may take on a fresh stack, and how often it is polled. */
const DEFAULT_RUN_TIMEOUT_MS = 9 * 60_000;
const SLOW_POLL_INTERVALS_MS = [5_000, 10_000, 15_000];

/** How long the worker may take to pick up "Sync now" and raise the discrepancy. */
const DISCREPANCY_TIMEOUT_MS = 6 * 60_000;

/** How long the coalesced rerun (up to 60 s, plus compute) may take to pin the C0 card. */
const C0_CARD_TIMEOUT_MS = 5 * 60_000;
const C0_CARD_INTERVALS_MS = [10_000, 15_000, 20_000];

/** Retries of a page check against a rerun or sync that lands between two reads. */
const PAGE_CHECK_TIMEOUT_MS = 2 * 60_000;
const PAGE_CHECK_INTERVALS_MS = [2_000, 5_000, 10_000];

/** The decision log must show a saved decision within this budget (one or two reloads). */
const DECISION_LOG_TIMEOUT_MS = 30_000;

/** One element check inside a retried block; short, so the block can retry with a reload. */
const ELEMENT_TIMEOUT_MS = 10_000;

/** How long a page may take to load its data before its anchor appears. */
const PAGE_LOAD_TIMEOUT_MS = 30_000;

/** How long a closed discrepancy may take to leave the API's unresolved list. */
const RESOLUTION_TIMEOUT_MS = 60_000;

// ---------------------------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------------------------

/**
 * The most teardown rounds. A round that restores nothing ends the teardown; when every round
 * still had something to restore, the state keeps changing under it, and the teardown fails
 * naming what remains.
 */
const MAX_TEARDOWN_ROUNDS = 4;

/** The note on every write the teardown makes, so the decision log names who wrote it. */
const TEARDOWN_NOTE =
  'e2e teardown (decisions.spec.ts): restores the state its test changed on this campaign';

/** The discrepancy kinds a decision can raise against the platform (`status`, `budget`). */
const DECISION_DISCREPANCY_KINDS: ReadonlySet<DiscrepancyKind> = new Set(['status', 'budget']);

// ---------------------------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------------------------

/** The campaign the test decides on, as picked from the latest default run. */
interface ChosenSubject {
  /** The run the item was read from. */
  runId: Run['id'];
  /** The recommendation whose card records the decision (`#item-{id}`). */
  itemId: Item['id'];
  /** The rule of that item; C1 and C1b prefill status `paused`. */
  ruleId: string;
  /** The campaign's internal id, the decision subject (`decision_subject.id`). */
  subjectId: number;
  /** The item's entity name, as the run froze it. */
  entityName: string | null;
  /** The source whose entity sync compares the campaign with its decision. */
  source: SourceName;
}

/** An item with the key of the group it was listed in. */
interface GroupedItem {
  /** The group key, for example `pause`. */
  group: string;
  /** The item. */
  item: Item;
}

/**
 * What the test owns from its first save on: the campaign, and its state just before that save.
 * The teardown restores exactly this, and nothing else.
 */
interface OwnedDecisionState {
  /** The campaign's internal id, the decision subject. */
  subjectId: number;
  /** The ids of the campaign's unresolved discrepancies before the first save; left alone. */
  before: ReadonlySet<number>;
  /** The campaign's newest decision before the first save: the decided state to restore. */
  decidedBefore: Decision;
}

/** The expected-state budget fields the decision form can offer. */
type AmountField = keyof typeof AMOUNT_FIELD_LABELS;

/** The budget fields in the order the decision form renders them. */
const AMOUNT_FIELDS: readonly AmountField[] = ['daily_budget', 'total_budget'];

// ---------------------------------------------------------------------------------------------
// API reads (page.request shares the signed-in browser context's cookies)
// ---------------------------------------------------------------------------------------------

/**
 * Reads one JSON resource and fails unless it answered 200.
 *
 * @param page - The test's page; its request context carries the session cookie.
 * @param path - The API path, relative to `baseURL`.
 * @returns The parsed body, typed as the contract in `src/api/types.ts` defines it.
 */
async function getJson<T>(page: Page, path: string): Promise<T> {
  const response = await page.request.get(path);
  expect(response.status(), `GET ${path} answers 200`).toBe(200);
  return (await response.json()) as T;
}

/**
 * The relative target of an RFC 8288 `Link: <…>; rel="next"` header, or `null` on the last page.
 *
 * @param response - A `GET /discrepancies` page.
 * @returns The next page's path and query, as the API names it.
 */
function nextPageLink(response: APIResponse): string | null {
  const header = response.headers()['link'];
  if (header === undefined) {
    return null;
  }
  const match = /<([^>]+)>\s*;\s*rel="next"/.exec(header);
  return match?.[1] ?? null;
}

/**
 * The unresolved discrepancies of the active datasets that the API lists, oldest first:
 * `GET /discrepancies` with its default states (`open`, `awaiting_platform`), following
 * `Link: rel="next"` to the end. The route leaves out a discrepancy whose subject it cannot load.
 *
 * @param page - The test's page.
 * @returns The live list. Its length is the count the discrepancy banner reports only while every
 *   unresolved discrepancy's subject loads: `X-Total-Count` and the banner count the left-out
 *   ones too.
 */
async function listUnresolvedDiscrepancies(page: Page): Promise<Discrepancy[]> {
  const all: Discrepancy[] = [];
  let path: string | null = `${DISCREPANCIES_PATH}?limit=${DISCREPANCY_PAGE_LIMIT}`;
  for (let pages = 0; path !== null; pages += 1) {
    if (pages >= MAX_DISCREPANCY_PAGES) {
      throw new Error(
        `GET ${DISCREPANCIES_PATH} announced more than ${MAX_DISCREPANCY_PAGES} pages`,
      );
    }
    const response = await page.request.get(path);
    expect(response.status(), `GET ${path} answers 200`).toBe(200);
    all.push(...((await response.json()) as Discrepancy[]));
    path = nextPageLink(response);
  }
  return all.filter((discrepancy) => discrepancy.state !== 'resolved');
}

/**
 * The status `GET /runs/latest` answers, or a short description when the stack does not answer,
 * so `expect.poll` keeps polling through a restart instead of failing on the first refusal.
 */
async function latestRunStatus(page: Page): Promise<number | string> {
  try {
    return (await page.request.get(RUNS_LATEST_PATH)).status();
  } catch (error) {
    return `unreachable: ${error instanceof Error ? error.message : String(error)}`;
  }
}

/**
 * Waits until a default analysis run has succeeded: on a fresh stack the worker first builds and
 * backfills the simulated datasets, then computes the default run (AAP 0.11.2 first start).
 *
 * @param page - The test's page.
 * @returns The latest succeeded default run.
 */
async function waitForDefaultRun(page: Page): Promise<Run> {
  await expect
    .poll(() => latestRunStatus(page), {
      message: `a default analysis run has succeeded (GET ${RUNS_LATEST_PATH} answers 200)`,
      timeout: DEFAULT_RUN_TIMEOUT_MS,
      intervals: SLOW_POLL_INTERVALS_MS,
    })
    .toBe(200);
  return getJson<Run>(page, RUNS_LATEST_PATH);
}

/**
 * A subject's decision log, newest first, so `[0]` is its last decided state.
 *
 * @param page - The test's page.
 * @param subjectId - The campaign's internal id.
 * @returns The decisions of the active dataset.
 */
async function campaignDecisions(page: Page, subjectId: number): Promise<Decision[]> {
  return getJson<Decision[]>(
    page,
    `${DECISIONS_PATH}?subject_type=campaign&subject_id=${String(subjectId)}`,
  );
}

// ---------------------------------------------------------------------------------------------
// Picking what to act on
// ---------------------------------------------------------------------------------------------

/** Every item of one section, with its group key, in API order. */
function sectionItems(page: RecommendationsPage, sectionKey: string): GroupedItem[] {
  const section = page.sections.find((candidate) => candidate.key === sectionKey);
  if (section === undefined) {
    return [];
  }
  return section.groups.flatMap((group) => group.items.map((item) => ({ group: group.key, item })));
}

/**
 * Whether the card of an item offers an enabled "Record decision" for a campaign right now: it
 * has a campaign decision subject and a source to sync, was computed from data still active, is
 * not held (by the run or by a discrepancy the overlay reads live) and is not hidden by feedback,
 * for example a snooze from another spec.
 */
function isActionableCampaignItem(item: Item): boolean {
  return (
    item.decision_subject?.type === 'campaign' &&
    item.entity.source !== null &&
    !item.current.historical &&
    !item.held &&
    !item.current.held_now &&
    item.current.hidden_by === null
  );
}

/**
 * Whether a campaign's last decision makes its decision form offer a budget input: shape
 * `own_budget` (a shared member's form offers status only), not an acknowledged removal (whose
 * form offers nothing), and a daily or total budget decided (an own-budget form offers exactly
 * the budget fields the decision holds).
 *
 * @param latest - The campaign's newest decision, if any.
 */
function offersAmountInput(latest: Decision | undefined): boolean {
  return (
    latest !== undefined &&
    latest.shape === 'own_budget' &&
    !latest.tombstone &&
    (latest.daily_budget !== null || latest.total_budget !== null)
  );
}

/**
 * Picks the campaign to decide on, in order of preference: the actionable C1 / C1b items of the
 * Campaigns "Pause" group, whose decision form is prefilled with status `paused`, then every
 * other actionable campaign-subject item of the section. The first whose campaign's last
 * decision {@link offersAmountInput} is chosen, so its form has a budget input for the refused
 * amount; the candidates' decision logs are read in that order, stopping at the first match.
 *
 * @param page - The test's page.
 * @param recommendations - The latest default run's recommendations.
 * @returns The chosen item and its subject.
 * @throws When no actionable campaign item has a campaign with its own budget; the message
 *   names the run.
 */
async function pickSubject(
  page: Page,
  recommendations: RecommendationsPage,
): Promise<ChosenSubject> {
  const candidates = sectionItems(recommendations, CAMPAIGNS_SECTION_KEY).filter(({ item }) =>
    isActionableCampaignItem(item),
  );
  const isPreferred = ({ group, item }: GroupedItem): boolean =>
    group === PAUSE_GROUP_KEY && PAUSE_RULES.has(item.rule_id);
  const ordered = [
    ...candidates.filter(isPreferred),
    ...candidates.filter((candidate) => !isPreferred(candidate)),
  ];
  for (const { item } of ordered) {
    const subject = item.decision_subject;
    const source = item.entity.source;
    if (subject === undefined || subject === null || !source) {
      continue;
    }
    if (offersAmountInput((await campaignDecisions(page, subject.id))[0])) {
      return {
        runId: recommendations.run.id,
        itemId: item.id,
        ruleId: item.rule_id,
        subjectId: subject.id,
        entityName: item.entity.name,
        source,
      };
    }
  }
  throw new Error(
    `Run ${String(recommendations.run.id)} has no actionable campaign item with ` +
      `"${RECORD_DECISION_LABEL}" in "Campaigns to pause or launch" whose campaign has its own ` +
      'budget (an own_budget decision with a daily or total budget, not a removal); the default ' +
      'simulated world pauses "Competitor - Conquest" (C1), which has its own daily budget',
  );
}

/**
 * The unresolved discrepancy the decision raised for the campaign: one that was not open before
 * the decision, preferring kind `status`; when none is new, an existing `status` discrepancy of
 * the campaign (the comparison this decision makes).
 *
 * @param list - The live unresolved list.
 * @param subjectId - The campaign's internal id.
 * @param before - The ids of the campaign's unresolved discrepancies before the decision.
 * @returns The discrepancy, or `undefined` while the worker has not raised it.
 */
function findRaisedDiscrepancy(
  list: readonly Discrepancy[],
  subjectId: number,
  before: ReadonlySet<number>,
): Discrepancy | undefined {
  const forSubject = list.filter(
    (discrepancy) =>
      discrepancy.subject.type === 'campaign' && discrepancy.subject.id === subjectId,
  );
  const fresh = forSubject.filter((discrepancy) => !before.has(discrepancy.id));
  return (
    fresh.find((discrepancy) => discrepancy.kind === 'status') ??
    fresh[0] ??
    forSubject.find((discrepancy) => discrepancy.kind === 'status')
  );
}

/** The discrepancy id a C0 item froze in `evidence.extra.discrepancy_id`, or `null`. */
function frozenDiscrepancyId(item: Item): number | null {
  const value = item.evidence.extra?.discrepancy_id;
  if (typeof value === 'number' && Number.isSafeInteger(value)) {
    return value;
  }
  if (typeof value === 'string' && /^\d+$/.test(value)) {
    return Number(value);
  }
  return null;
}

/**
 * The C0 item of a discrepancy in the Campaigns discrepancy group: matched by the discrepancy
 * id the item froze, else by the subject's source, type and name.
 *
 * @param page - A run's recommendations.
 * @param discrepancy - The discrepancy.
 * @returns The item, or `undefined` while no rerun has pinned it.
 */
function findC0Item(page: RecommendationsPage, discrepancy: Discrepancy): Item | undefined {
  const items = sectionItems(page, CAMPAIGNS_SECTION_KEY)
    .filter(({ group, item }) => group === DISCREPANCY_GROUP_KEY && item.rule_id === 'C0')
    .map(({ item }) => item);
  const { subject } = discrepancy;
  return (
    items.find((item) => frozenDiscrepancyId(item) === discrepancy.id) ??
    items.find(
      (item) =>
        frozenDiscrepancyId(item) === null &&
        item.entity.source === subject.source &&
        item.entity.type === subject.type &&
        item.entity.name !== null &&
        item.entity.name === subject.name,
    )
  );
}

/**
 * The resolution to apply: "Platform is correct" when offered (it closes at once and restores
 * the platform status as the decision), else the first resolution the API offers.
 *
 * @throws When the discrepancy offers no resolution.
 */
function chooseResolution(discrepancy: Discrepancy): Resolution {
  if (discrepancy.resolutions.includes('platform_correct')) {
    return 'platform_correct';
  }
  const first = discrepancy.resolutions[0];
  if (first === undefined) {
    throw new Error(`Discrepancy ${String(discrepancy.id)} offers no resolution`);
  }
  return first;
}

// ---------------------------------------------------------------------------------------------
// Page helpers
// ---------------------------------------------------------------------------------------------

/**
 * The caption name the decision log gives a subject (`subjectName` in `DecisionLog.tsx`): its
 * trimmed name, or "Unnamed campaign (id N)".
 */
function decisionLogName(subject: SubjectView): string {
  const name = subject.name?.trim() ?? '';
  const typeLabel = subject.type === 'budget' ? 'shared budget' : 'campaign';
  return name === '' ? `Unnamed ${typeLabel} (id ${String(subject.id)})` : name;
}

/**
 * The headline the `/decisions` resolver gives a discrepancy card (`subjectTitle` in
 * `DiscrepancyResolver.tsx`): the subject's trimmed name, or "Campaign N" / "Shared budget N".
 */
function resolverTitle(subject: SubjectView): string {
  const name = subject.name?.trim() ?? '';
  const typeLabel = subject.type === 'budget' ? 'Shared budget' : 'Campaign';
  return name === '' ? `${typeLabel} ${String(subject.id)}` : name;
}

/**
 * The resolver card of one discrepancy on a loaded `/decisions`. The resolver lists a page at a
 * time, oldest first, so a newly raised discrepancy is usually on a later page: "Show more
 * discrepancies" is pressed, each time waiting for its page to load, until the card is rendered
 * or no further page exists, and at most `MAX_DISCREPANCY_PAGES` times.
 *
 * @param page - The test's page, on `/decisions`.
 * @param id - The discrepancy's id.
 * @returns The card's locator, `#discrepancies #discrepancy-{id}`; the caller asserts on it.
 */
async function revealResolverEntry(page: Page, id: number): Promise<Locator> {
  const section = page.locator('#discrepancies');
  const entry = section.locator(`#discrepancy-${String(id)}`);
  const more = section.getByRole('button', { name: SHOW_MORE_DISCREPANCIES_LABEL, exact: true });
  // The first page is shown once the card or "Show more" is.
  await expect(entry.or(more).first()).toBeVisible({ timeout: PAGE_LOAD_TIMEOUT_MS });
  for (let pressed = 0; pressed < MAX_DISCREPANCY_PAGES; pressed += 1) {
    if ((await entry.count()) > 0 || (await more.count()) === 0) {
      break;
    }
    await more.click();
    // The button is loading (aria-busy) until its page is shown; the last page removes it.
    await expect
      .poll(
        () =>
          more.evaluateAll((buttons) =>
            buttons.every((button) => button.getAttribute('aria-busy') !== 'true'),
          ),
        { message: 'the next resolver page loads', timeout: PAGE_LOAD_TIMEOUT_MS },
      )
      .toBe(true);
  }
  return entry;
}

/**
 * The count the open-discrepancies banner reports, parsed from its title.
 *
 * @param text - The banner's text.
 * @returns N of "{N} open discrepancies to resolve first".
 * @throws When the text carries no such title.
 */
function parseBannerCount(text: string): number {
  const digits = BANNER_COUNT_PATTERN.exec(text)?.[1];
  if (digits === undefined) {
    throw new Error(
      `The discrepancy banner reads "${text}", with no "{N} open discrepancies" title`,
    );
  }
  return Number.parseInt(digits.replaceAll(',', ''), 10);
}

/**
 * Opens `/actions` and waits until the default run's sections have rendered, so a check for the
 * absence of the discrepancy banner is never made against the loading state.
 */
async function openActions(page: Page): Promise<void> {
  await page.goto('/actions');
  await expect(page.locator(CAMPAIGNS_SECTION_SELECTOR)).toBeVisible({
    timeout: PAGE_LOAD_TIMEOUT_MS,
  });
}

/**
 * Checks the discrepancy banner on a loaded `/actions` against the live API count: absent at 0,
 * otherwise the only `role="alert"`, titled with that count.
 *
 * @param page - The test's page, on a loaded `/actions`.
 * @returns The count the banner reports (0 when it is absent).
 */
async function expectBannerMatchesLiveCount(page: Page): Promise<number> {
  const alert = page.getByRole('alert');
  const unresolved = await listUnresolvedDiscrepancies(page);
  if (unresolved.length === 0) {
    await expect(alert, 'no discrepancy banner while none is unresolved').toHaveCount(0);
    return 0;
  }
  await expect(alert, 'the discrepancy banner is the only alert').toBeVisible({
    timeout: ELEMENT_TIMEOUT_MS,
  });
  const shown = parseBannerCount(await alert.innerText());
  expect(shown, 'the banner reports the live count of unresolved discrepancies').toBe(
    unresolved.length,
  );
  return shown;
}

/**
 * Presses "Sync now" for one source on `/sources`. When a request is already pending, the
 * button is disabled with the hint "A sync is already pending", and that pending request serves
 * the same purpose, so it is not pressed.
 *
 * @param page - The test's page.
 * @param source - The source to sync.
 */
async function requestSyncThroughUi(page: Page, source: SourceName): Promise<void> {
  await page.goto('/sources');
  const section = page.locator(`#source-${source}`);
  await expect(section).toBeVisible({ timeout: PAGE_LOAD_TIMEOUT_MS });
  const syncNow = section.getByRole('button', { name: SYNC_NOW_LABEL, exact: true });
  await expect(syncNow).toBeVisible();
  if (await syncNow.isEnabled()) {
    await syncNow.click();
  }
  await expect(section.getByText(SYNC_OUTCOME_PATTERN).first()).toBeVisible({
    timeout: PAGE_LOAD_TIMEOUT_MS,
  });
}

/**
 * Sets each budget input the decision dialog shows back to the last decided amount, so a form
 * prefilled with a proposed budget posts the status change alone. A form with no budget input
 * (a shared member) is left as it is.
 *
 * @param dialog - The open "Record decision" dialog.
 * @param latest - The subject's last decision.
 */
async function resetAmountsToDecided(dialog: Locator, latest: Decision): Promise<void> {
  for (const field of AMOUNT_FIELDS) {
    const input = dialog.getByRole('spinbutton', { name: AMOUNT_FIELD_LABELS[field], exact: true });
    const decided = latest[field];
    if (decided !== null && (await input.count()) > 0) {
      await input.fill(decided);
    }
  }
}

/**
 * The first budget input the decision dialog shows, in the form's order (daily, then total).
 *
 * @param dialog - The open "Record decision" dialog, with its form rendered.
 * @returns The input's locator.
 * @throws When the dialog shows no budget input; {@link pickSubject} picks only campaigns whose
 *   form offers one.
 */
async function firstAmountInput(dialog: Locator): Promise<Locator> {
  for (const field of AMOUNT_FIELDS) {
    const input = dialog.getByRole('spinbutton', { name: AMOUNT_FIELD_LABELS[field], exact: true });
    if ((await input.count()) > 0) {
      return input;
    }
  }
  throw new Error(
    `The "${RECORD_DECISION_LABEL}" dialog shows no ` +
      `${Object.values(AMOUNT_FIELD_LABELS).join(' or ')} input`,
  );
}

/** Whether a response answers the decision form's `POST /api/v1/decisions`. */
function isDecisionPost(response: Response): boolean {
  return (
    response.request().method() === 'POST' && new URL(response.url()).pathname === DECISIONS_PATH
  );
}

// ---------------------------------------------------------------------------------------------
// Teardown helpers (the only API writes of this file)
// ---------------------------------------------------------------------------------------------

/**
 * The machine code of an API error body (`{"error": {"code", "message"}}`).
 *
 * @param body - The response body as text.
 * @returns `error.code`, or `null` when the body has no such envelope, for example a proxy's
 *   error page.
 */
function errorCode(body: string): string | null {
  try {
    const parsed = JSON.parse(body) as Partial<ErrorResponse> | null;
    const code = parsed?.error?.code;
    return typeof code === 'string' ? code : null;
  } catch {
    return null;
  }
}

/**
 * The patch that records the earlier decided state again over the campaign's newest decision:
 * each of status, daily budget and total budget that the earlier decision holds and the newest
 * one differs on. A field the earlier decision leaves unset is not sent, because a decided field
 * cannot be cleared, and the test never sets one.
 *
 * @param newest - The campaign's newest decision, if any.
 * @param decided - The decision the test found before its first save.
 * @returns The fields to send; empty when the newest decision is that one or matches it.
 */
function restorationPatch(newest: Decision | undefined, decided: Decision): DecisionPatchIn {
  const patch: DecisionPatchIn = {};
  if (newest === undefined || newest.id === decided.id) {
    return patch;
  }
  if (decided.status !== null && newest.status !== decided.status) {
    patch.status = decided.status;
  }
  for (const field of AMOUNT_FIELDS) {
    const amount = decided[field];
    if (amount !== null && newest[field] !== amount) {
      patch[field] = amount;
    }
  }
  return patch;
}

/**
 * The discrepancies the test raised on its campaign: unresolved ones of a kind a decision can
 * raise (`status`, `budget`) that were not unresolved before its first save. `untracked` and
 * `missing` discrepancies, and every other subject's, are never the test's.
 *
 * @param page - The test's page.
 * @param state - What the test owns.
 * @returns The live list, oldest first.
 */
async function raisedSinceFirstSave(page: Page, state: OwnedDecisionState): Promise<Discrepancy[]> {
  return (await listUnresolvedDiscrepancies(page)).filter(
    (discrepancy) =>
      discrepancy.subject.type === 'campaign' &&
      discrepancy.subject.id === state.subjectId &&
      !state.before.has(discrepancy.id) &&
      DECISION_DISCREPANCY_KINDS.has(discrepancy.kind),
  );
}

/**
 * Resolves one discrepancy the test raised with "Platform is correct", which writes the
 * platform's value as the decision and closes the discrepancy at once.
 *
 * @param page - The test's page.
 * @param discrepancy - The discrepancy.
 * @returns `true` when this call closed it; `false` when it was already closed (409
 *   `discrepancy_resolved`), for example by an entity sync meanwhile.
 * @throws On any other answer, naming its status and body.
 */
async function resolvePlatformCorrect(page: Page, discrepancy: Discrepancy): Promise<boolean> {
  const path = `${DISCREPANCIES_PATH}/${String(discrepancy.id)}/resolve`;
  const body: ResolveRequest = { resolution: 'platform_correct', note: TEARDOWN_NOTE };
  const response = await page.request.post(path, { headers: CSRF_HEADERS, data: body });
  if (response.status() === 200) {
    return true;
  }
  const text = await response.text();
  if (response.status() === 409 && errorCode(text) === 'discrepancy_resolved') {
    return false;
  }
  throw new Error(`Teardown: POST ${path} answered ${String(response.status())}: ${text}`);
}

/**
 * Records the earlier decided state again when the campaign's newest decision differs from it,
 * as a patch of exactly the differing fields.
 *
 * @param page - The test's page.
 * @param state - What the test owns.
 * @returns What it recorded, or `null` when nothing differed (including 422 `no_change`).
 * @throws On any other answer, naming its status and body.
 */
async function recordDecidedStateAgain(
  page: Page,
  state: OwnedDecisionState,
): Promise<string | null> {
  const newest = (await campaignDecisions(page, state.subjectId))[0];
  const patch = restorationPatch(newest, state.decidedBefore);
  if (newest === undefined || Object.keys(patch).length === 0) {
    return null;
  }
  const body: DecisionRequest = {
    subject: { type: 'campaign', id: state.subjectId },
    patch,
    note: TEARDOWN_NOTE,
  };
  const response = await page.request.post(DECISIONS_PATH, { headers: CSRF_HEADERS, data: body });
  if (response.status() === 201) {
    return `recorded ${JSON.stringify(patch)} over decision ${String(newest.id)}`;
  }
  const text = await response.text();
  if (response.status() === 422 && errorCode(text) === 'no_change') {
    return null;
  }
  throw new Error(
    `Teardown: POST ${DECISIONS_PATH} answered ${String(response.status())}: ${text}`,
  );
}

/**
 * Restores what the test changed on its campaign, and nothing else, whatever the test's
 * outcome. Each round first resolves every discrepancy the test raised
 * ({@link raisedSinceFirstSave}) with "Platform is correct", then records the earlier decided
 * state again where the newest decision differs from it. It runs in rounds because the state
 * can change under it: a pending "Sync now" can raise a discrepancy between the two halves of a
 * round, and after a timeout the test body may still be running beside it. A round that
 * restores nothing ends it.
 *
 * @param page - The test's page; its request context carries the session cookie.
 * @param state - What the test owns.
 * @returns What each round restored; empty when the test left nothing to restore.
 * @throws When a write is refused, or when every one of {@link MAX_TEARDOWN_ROUNDS} rounds still
 *   had something to restore; the message names what remains.
 */
async function restoreOwnedState(page: Page, state: OwnedDecisionState): Promise<string[]> {
  const restored: string[] = [];
  for (let round = 1; round <= MAX_TEARDOWN_ROUNDS; round += 1) {
    const changes: string[] = [];
    for (const discrepancy of await raisedSinceFirstSave(page, state)) {
      if (await resolvePlatformCorrect(page, discrepancy)) {
        changes.push(`resolved ${discrepancy.kind} discrepancy ${String(discrepancy.id)}`);
      }
    }
    const recorded = await recordDecidedStateAgain(page, state);
    if (recorded !== null) {
      changes.push(recorded);
    }
    if (changes.length === 0) {
      return restored;
    }
    restored.push(...changes.map((change) => `round ${String(round)}: ${change}`));
  }
  const remaining = (await raisedSinceFirstSave(page, state)).map(
    (discrepancy) =>
      `${discrepancy.kind} discrepancy ${String(discrepancy.id)} (${discrepancy.state})`,
  );
  const newest = (await campaignDecisions(page, state.subjectId))[0];
  const patch = restorationPatch(newest, state.decidedBefore);
  if (newest !== undefined && Object.keys(patch).length > 0) {
    remaining.push(
      `decision ${String(newest.id)} differs from decision ` +
        `${String(state.decidedBefore.id)} by ${JSON.stringify(patch)}`,
    );
  }
  throw new Error(
    `Teardown of campaign ${String(state.subjectId)} did not settle in ` +
      `${String(MAX_TEARDOWN_ROUNDS)} rounds (${restored.join('; ')}); still to restore: ` +
      (remaining.length === 0 ? 'nothing at the last read' : remaining.join('; ')),
  );
}

// ---------------------------------------------------------------------------------------------
// The test
// ---------------------------------------------------------------------------------------------

/**
 * What the running test owns, set immediately before its first save so that every write it may
 * make, even one whose response was lost, is restored; taken and cleared by the teardown.
 */
let owned: OwnedDecisionState | undefined;

// The teardown departs from this file's rule that every mutation goes through the UI: it must
// restore the campaign whatever the UI did, after a failed assertion, a timeout with the page
// mid-flow, or a closed dialog, so it writes through `page.request` with the CSRF header the
// UI's client would have added.
test.afterEach(async ({ page }, testInfo) => {
  const state = owned;
  owned = undefined;
  if (state === undefined) {
    return;
  }
  const restored = await restoreOwnedState(page, state);
  testInfo.annotations.push({
    type: 'teardown',
    description:
      `Campaign ${String(state.subjectId)}: ` +
      (restored.length === 0 ? 'nothing to restore' : restored.join('; ')),
  });
});

test('records a decision and resolves the discrepancy it creates', async ({ page }) => {
  test.setTimeout(TEST_TIMEOUT_MS);

  const chosen =
    await test.step('wait for the default run and pick a campaign to pause', async () => {
      await waitForDefaultRun(page);
      let picked: ChosenSubject | undefined;
      // Item ids belong to one run: when a rerun replaces the run between the API read and the
      // page load, the card is missing, so the item is picked again from the newer run.
      await expect(async () => {
        const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
        const candidate = await pickSubject(page, recommendations);
        await page.goto('/actions');
        await expect(page.locator(`#item-${String(candidate.itemId)}`)).toBeVisible({
          timeout: PAGE_LOAD_TIMEOUT_MS,
        });
        picked = candidate;
      }).toPass({ timeout: PAGE_CHECK_TIMEOUT_MS, intervals: PAGE_CHECK_INTERVALS_MS });
      if (picked === undefined) {
        throw new Error('No campaign item was picked');
      }
      test.info().annotations.push({
        type: 'subject',
        description:
          `${picked.ruleId} item ${String(picked.itemId)} of run ${String(picked.runId)}: ` +
          `${picked.source} campaign ${String(picked.subjectId)} ` +
          `(${picked.entityName ?? 'unnamed'})`,
      });
      return picked;
    });

  const { itemId, subjectId, source } = chosen;
  const prefilledPause = PAUSE_RULES.has(chosen.ruleId);

  // The campaign's unresolved discrepancies before the decision: any of them is left alone, and
  // the one this decision raises is the one not in this set.
  const before = new Set(
    (await listUnresolvedDiscrepancies(page))
      .filter((entry) => entry.subject.type === 'campaign' && entry.subject.id === subjectId)
      .map((entry) => entry.id),
  );
  const decidedBefore = (await campaignDecisions(page, subjectId))[0];
  if (decidedBefore === undefined) {
    throw new Error(`Campaign ${String(subjectId)} has no decision, not even its baseline`);
  }
  const subjectName = decisionLogName(decidedBefore.subject);

  const card = page.locator(`#item-${String(itemId)}`);
  const dialog = page.getByRole('dialog', { name: RECORD_DECISION_LABEL, exact: true });
  const status = dialog.getByRole('combobox', { name: STATUS_FIELD_LABEL, exact: true });
  const save = dialog.getByRole('button', { name: SAVE_DECISION_LABEL, exact: true });

  const refusedMessage =
    await test.step('an amount the server refuses (422) is shown and writes nothing', async () => {
      await expect(card).toBeVisible({ timeout: PAGE_LOAD_TIMEOUT_MS });
      await card.getByRole('button', { name: RECORD_DECISION_LABEL, exact: true }).click();

      await expect(dialog).toBeVisible();
      if (prefilledPause) {
        // C1 and C1b prefill the decision with status `paused` (AAP 0.5.5).
        await expect(status).toHaveValue('paused');
      } else {
        await expect(status).toBeVisible();
        await status.selectOption('paused');
        await resetAmountsToDecided(dialog, decidedBefore);
      }
      await expect(status.locator('option:checked')).toHaveText(CAMPAIGN_STATUS_LABELS.paused);

      const amount = await firstAmountInput(dialog);
      await amount.fill(SERVER_REFUSED_AMOUNT);
      await expect(save, 'the form accepts the amount; only the server refuses it').toBeEnabled();

      const answered = page.waitForResponse(isDecisionPost);
      // From this click on the test may have written, so the teardown restores the campaign.
      owned = { subjectId, before, decidedBefore };
      await save.click();
      const response = await answered;
      expect(response.status(), 'the API refuses the amount with 422').toBe(422);
      const { error } = (await response.json()) as ErrorResponse;
      expect(error.code, 'the 422 names the amount rule').toBe('invalid_amount');
      expect(error.message.trim(), 'the 422 carries a message').not.toBe('');
      test.info().annotations.push({
        type: 'refused-decision',
        description: `POST ${DECISIONS_PATH} answered 422 ${error.code}: ${error.message}`,
      });

      // The dialog stays open and usable: it shows the API's message as a plain banner (the
      // alert role is reserved for the discrepancy banner) and keeps every entered value.
      await expect(dialog, 'the dialog stays open after the refusal').toBeVisible();
      await expect(
        dialog.getByText(error.message, { exact: true }),
        'the dialog shows the API message',
      ).toBeVisible();
      await expect(dialog.getByRole('alert'), 'the error is not an alert').toHaveCount(0);
      await expect(status, 'the status stays paused').toHaveValue('paused');
      await expect(amount, 'the refused amount stays entered').toHaveValue(SERVER_REFUSED_AMOUNT);
      await expect(amount, 'the amount stays editable').toBeEnabled();
      await expect(save, 'the decision can be saved again').toBeEnabled();
      await expect(
        card.getByText(error.message, { exact: true }),
        'the card shows the refused save message too',
      ).toHaveCount(1);

      const newest = (await campaignDecisions(page, subjectId))[0];
      expect(newest?.id, 'the refused decision wrote no decision').toBe(decidedBefore.id);
      const unresolved = (await listUnresolvedDiscrepancies(page))
        .filter((entry) => entry.subject.type === 'campaign' && entry.subject.id === subjectId)
        .map((entry) => entry.id);
      expect(new Set(unresolved), 'the refused decision wrote no discrepancy').toEqual(before);
      return error.message;
    });

  await test.step('the corrected decision saves and appears in the /decisions log', async () => {
    await resetAmountsToDecided(dialog, decidedBefore);
    await expect(status).toHaveValue('paused');
    await expect(save, 'pausing changes the last decided status').toBeEnabled();
    const answered = page.waitForResponse(isDecisionPost);
    await save.click();
    const response = await answered;
    expect(response.status(), 'the corrected decision is saved').toBe(201);
    await expect(dialog, 'the dialog closes once the decision is saved').toBeHidden({
      timeout: PAGE_LOAD_TIMEOUT_MS,
    });
    await expect(
      card.getByText(refusedMessage, { exact: true }),
      'the card no longer shows the refused save message',
    ).toHaveCount(0);

    const latest = (await campaignDecisions(page, subjectId))[0];
    expect(latest?.status, 'the newest decision pauses the campaign').toBe('paused');
    expect(latest?.recommendation_id, 'the decision names the item it was recorded from').toBe(
      itemId,
    );
    expect(latest?.daily_budget, 'the refused amount was not kept').toBe(
      decidedBefore.daily_budget,
    );
    expect(latest?.total_budget, 'the refused amount was not kept').toBe(
      decidedBefore.total_budget,
    );

    await expect(async () => {
      await page.goto('/decisions');
      await expect(
        page.getByRole('heading', { level: 1, name: 'Decisions', exact: true }),
      ).toBeVisible({ timeout: ELEMENT_TIMEOUT_MS });
      const table = page
        .locator(`#decision-log #decision-subject-campaign-${String(subjectId)}`)
        .getByRole('table', { name: `Decisions for ${subjectName}`, exact: true });
      await expect(table).toBeVisible({ timeout: ELEMENT_TIMEOUT_MS });
      // Row 0 is the header row; rows are newest first, so row 1 is the decision just saved.
      const newest = table.getByRole('row').nth(1);
      await expect(newest).toContainText(CAMPAIGN_STATUS_LABELS.paused, {
        timeout: ELEMENT_TIMEOUT_MS,
      });
      await expect(newest).toContainText(`status: ${CAMPAIGN_STATUS_LABELS.paused}`);
      await expect(newest).toContainText('Recommendation');
    }).toPass({ timeout: DECISION_LOG_TIMEOUT_MS, intervals: PAGE_CHECK_INTERVALS_MS });
  });

  const raised = await test.step('sync the source and see the discrepancy it raises', async () => {
    await requestSyncThroughUi(page, source);

    // The worker picks the request up when its scheduling permits: once startup has finished, at a
    // 30 s wake that finds the source's lane idle or, when a wake finds it busy, as soon as its
    // pass ends. It then runs the entity sync and compares the platform's snapshot (still active)
    // with the decision (paused).
    let found: Discrepancy | undefined;
    await expect
      .poll(
        async () => {
          try {
            found = findRaisedDiscrepancy(
              await listUnresolvedDiscrepancies(page),
              subjectId,
              before,
            );
          } catch {
            found = undefined;
          }
          return found?.id ?? null;
        },
        {
          message: `an unresolved discrepancy is raised for campaign ${String(subjectId)}`,
          timeout: DISCREPANCY_TIMEOUT_MS,
          intervals: SLOW_POLL_INTERVALS_MS,
        },
      )
      .not.toBeNull();
    if (found === undefined) {
      throw new Error(`No discrepancy was raised for campaign ${String(subjectId)}`);
    }
    const discrepancy = found;
    const { id, kind, state } = discrepancy;
    test.info().annotations.push({
      type: 'discrepancy',
      description: `${kind} discrepancy ${String(id)} (${state})`,
    });

    // The overlay reads discrepancies live, so the banner counts this one before any rerun.
    await expect(async () => {
      await openActions(page);
      const shown = await expectBannerMatchesLiveCount(page);
      expect(shown, 'the banner counts the new discrepancy').toBeGreaterThanOrEqual(1);
    }).toPass({ timeout: PAGE_CHECK_TIMEOUT_MS, intervals: PAGE_CHECK_INTERVALS_MS });

    return discrepancy;
  });

  await test.step('the C0 card offers its resolutions and no feedback actions', async () => {
    // The C0 card appears only once the coalesced rerun the discrepancy enqueued has finished.
    await expect(async () => {
      await openActions(page);
      const recommendations = await getJson<RecommendationsPage>(page, RECOMMENDATIONS_PATH);
      const c0 = findC0Item(recommendations, raised);
      expect(
        c0,
        `run ${String(recommendations.run.id)} pins a C0 item for discrepancy ${String(raised.id)}`,
      ).toBeDefined();
      if (c0 === undefined) {
        return;
      }
      const card = page.locator(DISCREPANCY_GROUP_SELECTOR).locator(`#item-${String(c0.id)}`);
      await expect(card).toBeVisible({ timeout: ELEMENT_TIMEOUT_MS });

      // C0 is exempt from feedback (AAP 0.5.4.2): its footer is the resolver.
      for (const label of Object.values(FEEDBACK_LABELS)) {
        await expect(card.getByRole('button', { name: label, exact: true })).toHaveCount(0);
      }
      const live = (await listUnresolvedDiscrepancies(page)).find(
        (entry) => entry.id === raised.id,
      );
      const resolutions = live?.resolutions ?? raised.resolutions;
      expect(resolutions.length, 'the discrepancy offers at least one resolution').toBeGreaterThan(
        0,
      );
      for (const resolution of resolutions) {
        await expect(
          card.getByRole('button', { name: RESOLUTION_LABELS[resolution], exact: true }),
        ).toBeVisible({ timeout: ELEMENT_TIMEOUT_MS });
      }
    }).toPass({ timeout: C0_CARD_TIMEOUT_MS, intervals: C0_CARD_INTERVALS_MS });
  });

  await test.step('resolve the discrepancy on /decisions and see the banner drop', async () => {
    const liveBefore = await listUnresolvedDiscrepancies(page);
    const current = liveBefore.find((entry) => entry.id === raised.id) ?? raised;
    const countBefore = liveBefore.length;
    const resolution = chooseResolution(current);

    await page.goto('/decisions');
    await expect(
      page.getByRole('heading', { level: 1, name: 'Decisions', exact: true }),
    ).toBeVisible();
    const entry = await revealResolverEntry(page, current.id);
    await expect(entry).toBeVisible({ timeout: PAGE_LOAD_TIMEOUT_MS });
    await expect(entry).toContainText(resolverTitle(current.subject));
    await entry.getByRole('button', { name: RESOLUTION_LABELS[resolution], exact: true }).click();

    if (resolution === 'decision_correct') {
      // The discrepancy waits for a snapshot that matches the decision instead of closing.
      await expect
        .poll(
          async () => {
            try {
              const list = await listUnresolvedDiscrepancies(page);
              return list.find((item) => item.id === current.id)?.state ?? 'closed';
            } catch {
              return null;
            }
          },
          {
            message: `discrepancy ${String(current.id)} awaits the platform`,
            timeout: RESOLUTION_TIMEOUT_MS,
            intervals: PAGE_CHECK_INTERVALS_MS,
          },
        )
        .toBe('awaiting_platform');
      test.info().annotations.push({
        type: 'skipped-check',
        description:
          `Discrepancy ${String(current.id)} offered no closing resolution, so it awaits the ` +
          'platform and the banner count is not expected to drop',
      });
      return;
    }

    if (resolution === 'record_intended') {
      // The intended state is the platform's own, so the next entity sync's snapshot matches it.
      const dialog = page.getByRole('dialog', {
        name: RESOLUTION_LABELS.record_intended,
        exact: true,
      });
      await expect(dialog).toBeVisible();
      const status = dialog.getByRole('combobox', { name: STATUS_FIELD_LABEL, exact: true });
      const platformStatus = current.platform_value?.status;
      if (typeof platformStatus === 'string' && (await status.count()) > 0) {
        await status.selectOption(platformStatus);
      }
      const save = dialog.getByRole('button', { name: SAVE_INTENDED_STATE_LABEL, exact: true });
      await expect(save).toBeEnabled();
      await save.click();
      await expect(dialog).toBeHidden({ timeout: PAGE_LOAD_TIMEOUT_MS });
      await requestSyncThroughUi(page, source);
    } else {
      // "Platform is correct", "Adopt platform state" and "Acknowledge removal" close at once,
      // and the refetched resolver list drops the card.
      await expect(entry).toHaveCount(0, { timeout: PAGE_LOAD_TIMEOUT_MS });
    }

    await expect
      .poll(
        async () => {
          try {
            return (await listUnresolvedDiscrepancies(page)).some((item) => item.id === current.id);
          } catch {
            return null;
          }
        },
        {
          message: `discrepancy ${String(current.id)} is resolved`,
          timeout:
            resolution === 'record_intended' ? DISCREPANCY_TIMEOUT_MS : RESOLUTION_TIMEOUT_MS,
          intervals: PAGE_CHECK_INTERVALS_MS,
        },
      )
      .toBe(false);

    // The banner now reports N - 1, or the fresh live count when the worker raised or closed
    // other discrepancies meanwhile; with none left it is gone (the helper asserts both).
    let shownAfter = -1;
    await expect(async () => {
      await openActions(page);
      shownAfter = await expectBannerMatchesLiveCount(page);
    }).toPass({ timeout: PAGE_CHECK_TIMEOUT_MS, intervals: PAGE_CHECK_INTERVALS_MS });
    if (shownAfter !== countBefore - 1) {
      test.info().annotations.push({
        type: 'concurrent-change',
        description:
          `The banner went from ${String(countBefore)} to ${String(shownAfter)}: other ` +
          'discrepancies were raised or closed while this one was resolved',
      });
    }

    if (resolution === 'platform_correct') {
      const restored = (await campaignDecisions(page, subjectId))[0];
      expect(restored?.status, 'the resolution adopted the platform status').toBe(
        current.platform_value?.status ?? 'active',
      );
    }
  });
});
