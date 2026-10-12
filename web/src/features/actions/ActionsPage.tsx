/**
 * `ActionsPage`: the `/actions` screen, the default page after login (AAP 0.5.5).
 *
 * The page is a ranked list of "do this because of this", not a dashboard (AAP 0.1.2): it has no
 * charts, and every number it shows sits on the card it justifies. Top to bottom it renders:
 *
 * 1. the page title, the page's single `<h1>` ("Actions"), which also titles the document
 *    "Actions · Ads Action Engine" in every state ({@link useDocumentTitle}, WCAG 2.2 SC 2.4.2);
 * 2. the {@link StatusBar}: window picker, each source's day boundary and the source chips;
 * 3. the {@link GlobalBanners}, in their fixed order and one `Stack`: open discrepancies first,
 *    because the discrepancy banner sits above everything, then the run notice, then the rest.
 *    The run notice is the {@link CustomRunBanner} for a custom-window run ("Custom window
 *    {start} – {end}" with "Back to default"), or the {@link OlderDefaultRunBanner} for a
 *    default run opened by id that is no longer the latest default run;
 * 4. the page's refresh region, a polite status region empty (and taking no space) until a read
 *    the page shows fails to refresh, when it holds "Actions could not be refreshed" (see
 *    "States" below);
 * 5. the {@link SectionNav}: anchor links with item counts for the five sections;
 * 6. the polite region of the {@link ActionOutcomeProvider}, empty (and taking no space) until a
 *    card confirms an outcome there, such as "Dismissed: Pause (Generic CRM — US)": it outlives
 *    the card a recorded feedback removes and announces the outcome. The visible confirmation
 *    of a feedback stands where the card stood, inside its section, so the user sees it where
 *    they acted. Both belong to the run view and the recommendation acted on: the region
 *    empties, staying in place, once the run no longer lists that recommendation (the rerun
 *    after a feedback) or when another run is opened, and the confirmation leaves with it;
 * 7. the five sections in the user's order ({@link SECTION_ORDER}): Keywords to adjust,
 *    Campaigns to pause or launch, Budget changes, Copy/creative changes, White papers, each
 *    rendered by its own component from {@link SECTION_COMPONENTS}.
 *
 * Run selection (AAP 0.5.3): without `runId` the page shows the latest succeeded **default** run,
 * because `GET /recommendations` is sent without `run_id`. With `runId`, which `src/router.tsx`
 * reads from `/actions?run=<id>`, it shows that run, typically a custom window created by the
 * window picker. A custom run never stands in for the default one, and a default run opened by
 * id that a later default run has replaced is marked as older, with a way back.
 *
 * Performance (AAP 0.1.1): the whole page comes from one precomputed `GET /recommendations`
 * response. This module sends no per-item request; the children read only page-level state
 * (configuration, sources, the discrepancy list), each once. The status bar's reads start
 * alongside the page's own read rather than after it, the default page's run doubles as the
 * latest default run the window picker and the run notice need, and recent custom runs are read
 * only when the user opens that list.
 *
 * States, in precedence order:
 *
 * - **Data**: the page above. A background refetch that fails keeps the last page on screen,
 *   because TanStack Query keeps `data` when a refetch errors; the hook's own polling recovers by
 *   itself once the API answers again. Meanwhile a warning "Actions could not be refreshed",
 *   with the failure's reason and "Showing the last loaded state." as two sentences, sits in
 *   the page's refresh region, after the global banners and before the section navigation, so
 *   the open-discrepancies banner stays above everything (AAP 0.5.5) and the warning still
 *   precedes the cards it qualifies. It covers a failed refresh of the page itself, of the
 *   source chips, of the unresolved discrepancy list and of the configuration, and clears once
 *   each of them has been read again.
 * - **Loading** (no data yet): the title and a `Spinner` labelled "Loading actions".
 * - **No analysis run yet** (default run only, 404 `not_found` or 409 `run_not_ready`): the first
 *   start, while the worker backfills and computes the first default run (AAP 0.11.2). The hook
 *   keeps polling every `FIRST_RUN_POLL_MS`, so the page fills in by itself; `first-start.spec.ts`
 *   waits on this state.
 * - **Run not found** (a `runId` the API does not know): a "Back to default" link.
 * - **Any other failure**: the API's message and a "Retry" button that refetches, rendered by
 *   {@link ActionsLoadFailure}. A reload during an outage shows it too: `src/router.tsx` renders
 *   this page even when the auth guard's `GET /auth/me` fails with anything but a 401, and the
 *   page's own read then fails the same way. The hook keeps polling after a transient failure,
 *   so the page recovers by itself once the API answers.
 *
 * A failure screen stays mounted while its read runs again, by poll or by Retry, so it never
 * flickers back to the spinner and keyboard focus is kept; each failure message is announced
 * through one polite status region (WCAG 2.2 SC 4.1.3).
 *
 * Rules this file keeps:
 * - **Server state only through `../../api/queries`** (AAP 0.11.1); nothing here fetches.
 * - **Design system only** (AAP 0.4.4): UI comes from the design-system index, layout only from
 *   `Stack`; no raw headings, no styles and no stylesheet of its own.
 * - **No literals the API owns**: section titles come from `Section.title`, banner texts from the
 *   API, and no window length or threshold is written here (AAP 0.5.4.1).
 * - **No alert**: nothing here carries `role="alert"`; AAP 0.5.5 reserves it for the
 *   open-discrepancies banner, which `GlobalBanners` owns.
 * - **No router import**: `src/router.tsx` imports this page, so `Link` comes straight from
 *   `@tanstack/react-router` and is typed by the router's `Register` augmentation.
 *
 * @packageDocumentation
 */
import { Link } from '@tanstack/react-router';
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import type { ComponentType, ReactElement, ReactNode, Ref } from 'react';

import {
  isApiError,
  isAwaitingFirstRun,
  useActionsRefreshFailure,
  useDefaultRun,
  usePrefetchActionsReads,
  useRecommendations,
} from '../../api/queries';
import type { ApiError } from '../../api/queries';
import { SECTION_ORDER } from '../../api/types';
import type { RecommendationsPage, Run, RunId, Section, SectionKey } from '../../api/types';
import { Banner, Button, EmptyState, PageShell, Spinner, Stack } from '../../design-system';
import { useDocumentTitle } from '../../lib/documentTitle';
import { joinSentences } from '../../lib/format';
import {
  CustomRunBanner,
  OlderDefaultRunBanner,
  isOlderDefaultRun,
} from '../status/CustomRunBanner';
import { GlobalBanners } from '../status/GlobalBanners';
import { StatusBar } from '../status/StatusBar';
import { B1CardsProvider } from './B1Cards';
import { BlockingDiscrepanciesProvider } from './BlockingDiscrepancies';
import { ActionOutcomeProvider } from './RecommendationCard';
import { SectionNav } from './SectionNav';
import { BudgetSection } from './sections/BudgetSection';
import { CampaignsSection } from './sections/CampaignsSection';
import { CreativeSection } from './sections/CreativeSection';
import { KeywordsSection } from './sections/KeywordsSection';
import { itemIdentity } from './sections/SectionFrame';
import type { SectionComponentProps } from './sections/SectionFrame';
import { WhitePapersSection } from './sections/WhitePapersSection';

// ---------------------------------------------------------------------------------------------
// Texts
// ---------------------------------------------------------------------------------------------

/** The page title, rendered as the page's single `<h1>` in every state. */
const PAGE_TITLE = 'Actions';

/** Accessible label of the spinner shown before the first response arrives. */
const LOADING_LABEL = 'Loading actions';

/** Title of the first-start state, before any default analysis run has succeeded. */
const FIRST_RUN_TITLE = 'No analysis run yet';

/** Reason shown in the first-start state; the hook keeps polling, so the page fills itself in. */
const FIRST_RUN_REASON =
  'The first analysis is still computing. This page refreshes automatically.';

/**
 * Accessible label of the spinner beside the first-start state. The design system's
 * `EmptyState` is static content, and a view that polls shows its own `Spinner` while it waits.
 */
const FIRST_RUN_WAITING_LABEL = 'Waiting for the first analysis run';

/** Title shown when the run addressed by `/actions?run=<id>` does not exist. */
const RUN_NOT_FOUND_TITLE = 'Run not found';

/** Text of the link from an unknown run back to the latest default run. */
const BACK_TO_DEFAULT_LABEL = 'Back to default';

/** Title of the failure state for every error other than the two above. */
const LOAD_ERROR_TITLE = 'Actions could not be loaded';

/** Reason shown for a failure that carries no usable message of its own. */
const GENERIC_ERROR_MESSAGE = 'The recommendations could not be read. Try again.';

/** Label of the button that repeats the failed `GET /recommendations`. */
const RETRY_LABEL = 'Retry';

/** Title of the notice shown while the loaded page's reads can no longer be refreshed. */
const REFRESH_ERROR_TITLE = 'Actions could not be refreshed';

/** What follows the failure's reason in that notice: the page keeps its last data. */
const REFRESH_ERROR_SUFFIX = 'Showing the last loaded state.';

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/** Props of {@link ActionsPage}. */
export interface ActionsPageProps {
  /**
   * The run to show, read by `src/router.tsx` from `/actions?run=<id>`. Omitted (or
   * `undefined`), the page shows the latest succeeded default run, and while none exists it
   * polls and shows "No analysis run yet".
   */
  runId?: Run['id'];
}

/**
 * The component that renders each of the five sections, keyed by {@link SectionKey}.
 *
 * Every section component takes the same {@link SectionComponentProps}
 * (`{ section, run, overlay }`), so the page renders them uniformly in {@link SECTION_ORDER}. The
 * `Record` over the `SectionKey` union makes a missing or extra section a type error, and the
 * object is frozen, so no caller can swap a section's renderer at run time.
 *
 * @example
 * ```tsx
 * const Component = SECTION_COMPONENTS.budget; // BudgetSection
 * <Component section={section} run={page.run} overlay={page.overlay} />;
 * ```
 */
export const SECTION_COMPONENTS: Record<SectionKey, ComponentType<SectionComponentProps>> =
  Object.freeze({
    keywords: KeywordsSection,
    campaigns: CampaignsSection,
    budget: BudgetSection,
    creative: CreativeSection,
    white_papers: WhitePapersSection,
  });

// ---------------------------------------------------------------------------------------------
// Section render boundary
// ---------------------------------------------------------------------------------------------

/**
 * The overlay field that only stamps when the API read the current state. Every
 * `GET /recommendations` response carries the instant of its own read, so each poll changes it
 * even when nothing on the page changed, and no section renders it.
 */
const OVERLAY_READ_TIME_KEY = 'computed_at' satisfies keyof SectionComponentProps['overlay'];

/** Whether a decoded JSON value is an object (neither `null` nor an array), read key by key. */
function isJsonObject(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Whether a decoded JSON value is an array. */
function isJsonArray(value: unknown): value is readonly unknown[] {
  return Array.isArray(value);
}

/**
 * Whether two values decoded from API JSON are equal in value: the same primitive, arrays with
 * equal entries in the same order, or objects with the same keys, in any order, and equal values.
 *
 * @param a - the earlier value
 * @param b - the later value
 * @param skipKey - a key left out when comparing the two top-level objects; nested objects are
 *   compared in full
 * @returns `true` when nothing but `skipKey` differs
 */
function sameJsonValue(a: unknown, b: unknown, skipKey?: string): boolean {
  if (a === b) {
    return true;
  }
  if (isJsonArray(a) || isJsonArray(b)) {
    return (
      isJsonArray(a) &&
      isJsonArray(b) &&
      a.length === b.length &&
      a.every((entry, index) => sameJsonValue(entry, b[index]))
    );
  }
  if (!isJsonObject(a) || !isJsonObject(b)) {
    return false;
  }
  for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (key !== skipKey && !sameJsonValue(a[key], b[key])) {
      return false;
    }
  }
  return true;
}

/**
 * Whether a section renders the same for `next` as it did for `prev`, so React keeps its last
 * output: the same `section` and `run` objects, and an overlay equal in value on every field but
 * its read time ({@link OVERLAY_READ_TIME_KEY}).
 *
 * TanStack Query's structural sharing hands a refetched page's unchanged parts back as the very
 * objects the page already held, so `section` and `run` compare by identity: a changed item,
 * group, finding, withheld line or suppression count gives its section a new object. The overlay
 * is a new object on every response, because its read time changes on every poll; its other
 * fields (open discrepancies, source modes, stale and incomplete datasets, and any field the API
 * adds) are compared by value. The read time is left out because no section renders it:
 * comparing it would rebuild every section on every poll.
 *
 * @param prev - the props of the section's last render
 * @param next - the props of this render
 * @returns `true` when the section can skip this render
 */
function sameSectionInputs(
  prev: Readonly<SectionComponentProps>,
  next: Readonly<SectionComponentProps>,
): boolean {
  return (
    prev.section === next.section &&
    prev.run === next.run &&
    sameJsonValue(prev.overlay, next.overlay, OVERLAY_READ_TIME_KEY)
  );
}

/**
 * The renderer the page uses for each section: its {@link SECTION_COMPONENTS} entry inside
 * `memo` with {@link sameSectionInputs}. The page re-renders whenever its read's fetch status
 * changes, at the start and the end of every poll, and each poll delivers a new overlay; this
 * boundary keeps a section whose inputs did not change from rebuilding its partitions, cards,
 * evidence and tables, while a section whose data or overlay state changed renders again.
 */
const SECTION_RENDERERS: Readonly<Record<SectionKey, ComponentType<SectionComponentProps>>> =
  Object.freeze({
    keywords: memo(SECTION_COMPONENTS.keywords, sameSectionInputs),
    campaigns: memo(SECTION_COMPONENTS.campaigns, sameSectionInputs),
    budget: memo(SECTION_COMPONENTS.budget, sameSectionInputs),
    creative: memo(SECTION_COMPONENTS.creative, sameSectionInputs),
    white_papers: memo(SECTION_COMPONENTS.white_papers, sameSectionInputs),
  });

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * The first section the API returned for each key. The API returns each of the five keys once;
 * should a key repeat, only its first section is rendered, which is also the one `SectionNav`
 * links to, because the anchor `section-{key}` can target one region only.
 *
 * @param sections - the sections, as `GET /recommendations` returned them
 * @returns a map from section key to its first section; a key the API omitted has no entry
 */
function firstSectionByKey(sections: readonly Section[]): ReadonlyMap<SectionKey, Section> {
  const byKey = new Map<SectionKey, Section>();
  for (const section of sections) {
    if (!byKey.has(section.key)) {
      byKey.set(section.key, section);
    }
  }
  return byKey;
}

/**
 * The recommendation identity (`itemIdentity`) of every item the run lists: each item of each
 * group of each section, hidden ones included, since a feedback hides its item while the run
 * still lists it.
 *
 * @param sections - the sections, as `GET /recommendations` returned them
 * @returns the identities, each once
 */
function listedIdentities(sections: readonly Section[]): ReadonlySet<string> {
  const identities = new Set<string>();
  for (const section of sections) {
    for (const group of section.groups) {
      for (const item of group.items) {
        identities.add(itemIdentity(item));
      }
    }
  }
  return identities;
}

/**
 * The reason shown for a failed read: the API's own message, trimmed, when the error is an
 * `ApiError` with a non-blank message, and a generic sentence otherwise, so the page never shows
 * an empty reason or the text "undefined".
 *
 * @param error - the query's error
 * @returns the non-empty reason text
 */
function failureReason(error: unknown): string {
  if (isApiError(error)) {
    const message = error.message.trim();
    if (message !== '') {
      return message;
    }
  }
  return GENERIC_ERROR_MESSAGE;
}

/**
 * Whether a read of an addressed run failed because that run does not exist: 404 `not_found`
 * for an unknown id, or 422 `validation_error` for an id the API rejects as unable to name any
 * run. Retrying cannot change either answer, so both lead back to the default run rather than to
 * a "Retry" button.
 *
 * @param error - the read's settled error
 * @returns `true` for those two statuses
 */
function isUnknownRun(error: unknown): boolean {
  return isApiError(error) && (error.status === 404 || error.status === 422);
}

/** The failure a read of one run settled with, remembered by {@link useSettledFailure}. */
interface SettledFailure {
  /** The run the failed read addressed; `undefined` for the latest default run. */
  runId: RunId | undefined;
  /** The error that read settled with. */
  error: ApiError;
}

/**
 * The error the page's read last settled with, kept while the same read runs again.
 *
 * TanStack Query v5 puts a query that holds no data back to `status: 'pending'` with
 * `error: null` each time it fetches. Without this hook every poll of the first-start state
 * (every `FIRST_RUN_POLL_MS`), every poll after a transient failure and every Retry would swap
 * the failure screen for the loading spinner and back, remounting it: the screen would flicker,
 * its Retry button would be replaced, and keyboard focus would fall to the page body
 * (WCAG 2.2 SC 2.4.3). The failure is therefore kept until the read settles again, with a new
 * error that replaces it or with data that clears it.
 *
 * The failure is remembered together with the run it belongs to, so a different run, for
 * example the default run after "Back to default", never inherits it: it shows the loading
 * spinner until its own read settles.
 *
 * The state is adjusted while rendering, React's pattern for information kept from earlier
 * renders: React renders again at once, before it commits anything.
 *
 * @param runId - the run the read addresses; `undefined` for the latest default run
 * @param error - the query's `error`
 * @param fetching - whether the query is fetching or paused waiting for the network
 * @param hasData - whether the query holds data
 * @returns the failure to present, or `null` while the read has not settled or last settled with
 *   data
 */
function useSettledFailure(
  runId: RunId | undefined,
  error: ApiError | null,
  fetching: boolean,
  hasData: boolean,
): ApiError | null {
  const [kept, setKept] = useState<SettledFailure | null>(null);
  const keptError = kept !== null && kept.runId === runId ? kept.error : null;
  const settled = error === null && fetching && !hasData ? keptError : error;
  const outdated =
    settled === null
      ? kept !== null
      : kept === null || kept.runId !== runId || kept.error !== settled;
  if (outdated) {
    setKept(settled === null ? null : { runId, error: settled });
  }
  return settled;
}

// ---------------------------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------------------------

/** Props of {@link ActionsFrame}. */
interface ActionsFrameProps {
  /**
   * A state message to announce, such as the "No analysis run yet" or failure `EmptyState`. It
   * is rendered inside the frame's polite status region; omitted, the region stays empty, as it
   * does on the loaded page, which announces a failed refresh in a region of its own below the
   * global banners ({@link ActionsContent}).
   */
  notice?: ReactNode;
  /** What the page shows below the title and the notice. */
  children?: ReactNode;
}

/**
 * The page's column: the "Actions" title, a polite status region and the state's content,
 * spaced by one `Stack`.
 *
 * Every state renders through it, so the page always has exactly one `<h1>`, which the
 * end-to-end specs and assistive technology both rely on. The loading, first-start and failure
 * states render it from the same place, so React keeps one status region element from the
 * first render: a notice that replaces the loading spinner lands in a region that already exists
 * and is announced politely (WCAG 2.2 SC 4.1.3), never as an alert, which AAP 0.5.5 reserves
 * for discrepancies. While the region is empty, `Stack` takes it out of the column, so it adds
 * no space. Being in every state, it is also where the document is titled "Actions · Ads Action
 * Engine" ({@link useDocumentTitle}), so the failure state {@link ActionsLoadFailure} carries
 * that title wherever it renders.
 */
function ActionsFrame({ notice, children }: ActionsFrameProps): ReactElement {
  useDocumentTitle(PAGE_TITLE);
  return (
    <Stack gap={6}>
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <div role="status">{notice}</div>
      {children}
    </Stack>
  );
}

/** Props of {@link ActionsLoadFailure}. */
export interface ActionsLoadFailureProps {
  /**
   * The failure to explain. The reason shown is its `ApiError` message, trimmed, which the API
   * client guarantees is safe to show, or a generic sentence for a blank message or any other
   * error, so a stack trace or internal error text never reaches the screen.
   */
  error: unknown;
  /** Runs the failed read again; called each time the user activates "Retry". */
  onRetry: () => void;
  /**
   * Whether a Retry the user started is still running: the button then shows as loading, which
   * makes it inert (`aria-disabled`) while it keeps focus. Defaults to `false`.
   */
  retrying?: boolean;
  /**
   * The "Retry" button's ref, through which the caller returns focus to the button once a Retry
   * has failed again and focus fell to the page body (WCAG 2.2 SC 2.4.3).
   */
  retryButtonRef?: Ref<HTMLButtonElement>;
}

/**
 * The `/actions` failure state: the page's `<h1>` "Actions" and, in its polite status region,
 * an `EmptyState` "Actions could not be loaded" with the failure's reason and a "Retry" `Button`.
 *
 * It is presentational and reads no server state: {@link ActionsPage} renders it when
 * `GET /recommendations` fails without data, a reload during an outage (a 502 or 503) included,
 * and passes the Retry that reads the page again. The notice is announced politely, never as an
 * alert, which AAP 0.5.5 reserves for discrepancies.
 *
 * @param props - see {@link ActionsLoadFailureProps}
 * @returns the failure state, rendered inside the app's `PageShell`
 *
 * @example
 * ```tsx
 * <ActionsLoadFailure
 *   error={failure}
 *   onRetry={() => void retry()}
 *   retrying={retrying}
 *   retryButtonRef={retryButtonRef}
 * />
 * ```
 */
export function ActionsLoadFailure({
  error,
  onRetry,
  retrying = false,
  retryButtonRef,
}: ActionsLoadFailureProps): ReactElement {
  return (
    <ActionsFrame
      notice={
        <EmptyState
          title={LOAD_ERROR_TITLE}
          action={
            <Button ref={retryButtonRef} loading={retrying} onClick={onRetry}>
              {RETRY_LABEL}
            </Button>
          }
        >
          {failureReason(error)}
        </EmptyState>
      }
    />
  );
}

/** Props of {@link ActionsContent}. */
interface ActionsContentProps {
  /** One `GET /recommendations` response: the run, its overlay, banners and sections. */
  page: RecommendationsPage;
  /**
   * The failed refresh of a read the page shows, from `useActionsRefreshFailure`, or `null`
   * while every one of them is current.
   */
  refreshFailure: ApiError | null;
  /**
   * Whether `/actions?run=<id>` addressed the run. Without an id the page is the latest default
   * run by definition, so it is never marked as an older one.
   */
  runAddressed: boolean;
}

/**
 * The notice about the run on screen that {@link GlobalBanners} shows right after the
 * open-discrepancies banner:
 *
 * - a custom run: the {@link CustomRunBanner};
 * - a default run addressed by id that {@link isOlderDefaultRun} places before `latest`: the
 *   {@link OlderDefaultRunBanner};
 * - anything else, including every run while `latest` has not been read: no notice.
 *
 * @param run - the run on screen
 * @param runAddressed - whether `/actions?run=<id>` addressed it
 * @param latest - the latest succeeded default run, or `undefined` while it is not known
 * @returns the notice, or `null`
 */
function runNoticeFor(
  run: Run,
  runAddressed: boolean,
  latest: Run | undefined,
): ReactElement | null {
  if (run.kind === 'custom') {
    return <CustomRunBanner run={run} />;
  }
  if (runAddressed && latest !== undefined && isOlderDefaultRun(run, latest)) {
    return <OlderDefaultRunBanner run={run} />;
  }
  return null;
}

/**
 * The loaded page: status bar, global banners with the run notice ({@link runNoticeFor}) right
 * after the open-discrepancies banner, section navigation and the five sections in
 * {@link SECTION_ORDER}, each with the run and overlay of the same response, so every part
 * describes one moment. The sections sit inside one
 * {@link ActionOutcomeProvider}, whose polite region, between the navigation and the first
 * section, announces each card's outcome even after the card has left the page, and whose
 * record of each feedback by recommendation lets the section show that outcome in the hidden
 * card's place, both for as long as the page's run still lists that recommendation (the
 * provider's `identities`, every item of every section) and only in the run view it happened
 * in (its `scope`: the run's id when `/actions?run=<id>` addressed it, else the default page),
 * so a rerun that drops the item or a switch to another run empties the region in place;
 * inside one
 * {@link BlockingDiscrepanciesProvider}, through which each held card links the discrepancies
 * that hold it, and inside one {@link B1CardsProvider}, through which each C2 "Scale" card links
 * the B1 card that carries its amount.
 *
 * While `refreshFailure` is set, a polite status region right after the global banners and
 * before the section navigation holds a warning `Banner` "Actions could not be refreshed" with
 * the failure's reason and "Showing the last loaded state." as two sentences, a reason without
 * closing punctuation ended by a full stop ({@link joinSentences}), so a failed poll of the page,
 * the source chips, the discrepancy list or the configuration never leaves the cards looking
 * current. It sits below the global banners, so the open-discrepancies banner, and the fixed
 * order of the banners after it, stay above it (AAP 0.5.5: the discrepancy banner sits above
 * everything), and above the sections and their cards. The region mounts with this content, so
 * a failure that arrives later lands in a region assistive technology already watches and is
 * announced politely (WCAG 2.2 SC 4.1.3), never as an alert, which AAP 0.5.5 reserves for
 * discrepancies; without a failure it is empty, and `Stack` takes it out of the column, so it
 * adds no space.
 *
 * Memoized on `page`, `refreshFailure` and `runAddressed`: the page re-renders whenever its
 * read's fetch status changes, at the start and the end of every poll, and this content renders
 * again only for a newly delivered page, a change of the reported failure, whose `ApiError`
 * stays the same object until it does, or a new latest default run, which it reads for the run
 * notice. Each section then renders again only when its own inputs changed
 * ({@link SECTION_RENDERERS}).
 */
const ActionsContent = memo(function ActionsContent({
  page,
  refreshFailure,
  runAddressed,
}: ActionsContentProps): ReactElement {
  const { run, overlay, banners, sections } = page;
  // The entry the window picker observes too, so this sends no request of its own. Only `data`
  // is read, so a poll that leaves the latest default run unchanged renders nothing here.
  const latestDefaultRun = useDefaultRun().data;
  const sectionsByKey = firstSectionByKey(sections);
  // A card's outcome stays only while the run lists its recommendation.
  const identities = useMemo(() => listedIdentities(sections), [sections]);

  return (
    <ActionsFrame>
      <StatusBar run={run} overlay={overlay} />
      <GlobalBanners
        banners={banners}
        overlay={overlay}
        runNotice={runNoticeFor(run, runAddressed, latestDefaultRun)}
      />
      {/* After the global banners, so the open-discrepancies banner stays above it (AAP 0.5.5),
          and mounted with the page, so a later failed refresh is announced. */}
      <div role="status">
        {refreshFailure !== null && (
          <Banner tone="warning" title={REFRESH_ERROR_TITLE}>
            {joinSentences(failureReason(refreshFailure), REFRESH_ERROR_SUFFIX)}
          </Banner>
        )}
      </div>
      <SectionNav sections={sections} />
      {/* Outcomes belong to this run view: opening another run empties the region in place. */}
      <ActionOutcomeProvider scope={runAddressed ? run.id : undefined} identities={identities}>
        {/* Held cards link the discrepancies that hold them: the run's C0 cards or /decisions. */}
        <BlockingDiscrepanciesProvider sections={sections}>
          {/* C2 "Scale" cards link the run's B1 card for their subject, in Budget changes. */}
          <B1CardsProvider sections={sections}>
            {SECTION_ORDER.map((key) => {
              const section = sectionsByKey.get(key);
              // The API always returns all five sections; a key it left out is skipped, never
              // stubbed.
              if (section === undefined) {
                return null;
              }
              const SectionComponent = SECTION_RENDERERS[key];
              return <SectionComponent key={key} section={section} run={run} overlay={overlay} />;
            })}
          </B1CardsProvider>
        </BlockingDiscrepanciesProvider>
      </ActionOutcomeProvider>
    </ActionsFrame>
  );
});

/**
 * The `/actions` screen: the five ranked action sections of one analysis run, with the status
 * bar, the global banners and the section navigation above them (AAP 0.5.5).
 *
 * It reads the page with one `useRecommendations(runId)` call and, beside it,
 * `usePrefetchActionsReads(runId)` starts the status bar's reads (`GET /config`, `GET /sources`
 * and, for an addressed run only, `GET /runs/latest`), so they do not wait for the page to
 * arrive. It renders, in precedence order:
 *
 * - the loaded page whenever data exists, including after a failed background refetch, which
 *   `useActionsRefreshFailure(runId)` reports: while the page's own read, the source chips'
 *   `GET /sources`, the unresolved discrepancy list or `GET /config` last failed to refresh,
 *   a warning `Banner` "Actions could not be refreshed", below the global banners and above the
 *   section navigation, gives the reason and "Showing the last loaded state." as two sentences,
 *   until the next successful read clears it;
 * - `PageShell.Title` "Actions" with a `Spinner` labelled "Loading actions" until the first
 *   response settles;
 * - without `runId`, an `EmptyState` "No analysis run yet" with "The first analysis is still
 *   computing. This page refreshes automatically." when the API reports no default run yet
 *   (404 `not_found` or 409 `run_not_ready`), beside a spinner; the hook polls until the first
 *   run arrives, and the page then shows it by itself;
 * - with `runId`, an `EmptyState` "Run not found" with a "Back to default" link to `/actions`
 *   when the API does not know the run (404, or 422 for an id that cannot name a run);
 * - otherwise {@link ActionsLoadFailure}: an `EmptyState` "Actions could not be loaded" with the
 *   API's message and a "Retry" `Button` that reads the page again.
 *
 * A failure screen stays in place while its read runs again, by poll or by Retry
 * ({@link useSettledFailure}), so it never flickers to the spinner. A Retry the user starts
 * shows the button as loading, which disables it; once the read settles, focus returns to the
 * button if the failure is still shown and focus has not moved elsewhere (WCAG 2.2 SC 2.4.3).
 * A background poll leaves the button enabled, so focus on it is never lost.
 *
 * Every state carries the page's single `<h1>` "Actions". The component must render inside the
 * app's `QueryClientProvider` and TanStack `RouterProvider`, as every authenticated page does.
 *
 * @param props - see {@link ActionsPageProps}
 * @returns the `/actions` page content, rendered inside the app's `PageShell`
 *
 * @example
 * ```tsx
 * // src/router.tsx, /actions route component:
 * const { run } = actionsRoute.useSearch();
 * return <ActionsPage runId={run} />;
 * ```
 */
export function ActionsPage({ runId }: ActionsPageProps): ReactElement {
  const query = useRecommendations(runId);
  // The status bar's reads start here, beside the page's own, rather than once the page has data.
  usePrefetchActionsReads(runId);
  // A failed poll of a read the loaded page shows; those reads keep their last data meanwhile.
  const refreshFailure = useActionsRefreshFailure(runId);
  const failure = useSettledFailure(
    runId,
    query.error,
    query.fetchStatus !== 'idle',
    query.data !== undefined,
  );

  // A Retry the user started: the button shows as loading, hence inert, until it settles.
  const [retrying, setRetrying] = useState(false);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);

  // The loading button keeps focus, but focus can still be lost to the page body meanwhile, so
  // once a Retry settles and the failure is still shown, focus on the body goes back to its
  // button. Focus the user moved elsewhere in the meantime is left alone, and a button that left
  // the page (the retry succeeded) is skipped.
  useEffect(() => {
    if (retrying || !returnFocusRef.current) {
      return;
    }
    returnFocusRef.current = false;
    const button = retryButtonRef.current;
    const active = document.activeElement;
    if (
      button !== null &&
      button.isConnected &&
      !button.disabled &&
      (active === null || active === document.body)
    ) {
      button.focus();
    }
  }, [retrying]);

  const retry = async (): Promise<void> => {
    returnFocusRef.current = true;
    setRetrying(true);
    try {
      // `refetch` never rejects: a failed read settles into the query's error instead.
      await query.refetch();
    } finally {
      setRetrying(false);
    }
  };

  if (query.data !== undefined) {
    return (
      <ActionsContent
        page={query.data}
        refreshFailure={refreshFailure}
        runAddressed={runId !== undefined}
      />
    );
  }

  if (failure === null) {
    return (
      <ActionsFrame>
        <Spinner label={LOADING_LABEL} showLabel />
      </ActionsFrame>
    );
  }

  if (runId === undefined && isAwaitingFirstRun(failure)) {
    return (
      <ActionsFrame notice={<EmptyState title={FIRST_RUN_TITLE}>{FIRST_RUN_REASON}</EmptyState>}>
        {/* The EmptyState above already says on screen that the first run is computing, so this
            ring keeps its label for assistive technology only rather than repeating it. */}
        <Spinner label={FIRST_RUN_WAITING_LABEL} />
      </ActionsFrame>
    );
  }

  if (runId !== undefined && isUnknownRun(failure)) {
    return (
      <ActionsFrame
        notice={
          <EmptyState
            title={RUN_NOT_FOUND_TITLE}
            action={
              // Exact matching, as in CustomRunBanner: the router compares search params
              // partially by default, so the empty target search would match `?run=<id>` and
              // mark this link `aria-current="page"` while it leads to a different view.
              <Link to="/actions" search={{}} activeOptions={{ exact: true }}>
                {BACK_TO_DEFAULT_LABEL}
              </Link>
            }
          />
        }
      />
    );
  }

  return (
    <ActionsLoadFailure
      error={failure}
      onRetry={() => {
        void retry();
      }}
      retrying={retrying}
      retryButtonRef={retryButtonRef}
    />
  );
}
