/**
 * `ActionsPage`: the `/actions` screen, the default page after login (AAP 0.5.5).
 *
 * The page is a ranked list of "do this because of this", not a dashboard (AAP 0.1.2): it has no
 * charts, and every number it shows sits on the card it justifies. Top to bottom it renders:
 *
 * 1. the page title, the page's single `<h1>` ("Actions");
 * 2. the {@link StatusBar}: window picker, each source's day boundary and the source chips;
 * 3. the {@link CustomRunBanner}, only for a custom-window run ("Custom window {start} – {end}"
 *    with "Back to default");
 * 4. the {@link GlobalBanners}, in their fixed order, open discrepancies first;
 * 5. the {@link SectionNav}: anchor links with item counts for the five sections;
 * 6. the five sections in the user's order ({@link SECTION_ORDER}): Keywords to adjust,
 *    Campaigns to pause or launch, Budget changes, Copy / creative changes, White papers, each
 *    rendered by its own component from {@link SECTION_COMPONENTS}.
 *
 * Run selection (AAP 0.5.3): without `runId` the page shows the latest succeeded **default** run,
 * because `GET /recommendations` is sent without `run_id`. With `runId`, which `src/router.tsx`
 * reads from `/actions?run=<id>`, it shows that run, typically a custom window created by the
 * window picker. A custom run never stands in for the default one.
 *
 * Performance (AAP 0.1.1): the whole page comes from one precomputed `GET /recommendations`
 * response. This module sends no per-item request; the children read only page-level state
 * (configuration, sources, the discrepancy list, recent custom runs), each once.
 *
 * States, in precedence order:
 *
 * - **Data**: the page above. A background refetch that fails keeps the last page on screen,
 *   because TanStack Query keeps `data` when a refetch errors; the hook's own polling recovers by
 *   itself once the API answers again.
 * - **Loading** (no data yet): the title and a `Spinner` labelled "Loading actions".
 * - **No analysis run yet** (default run only, 404 `not_found` or 409 `run_not_ready`): the first
 *   start, while the worker backfills and computes the first default run (AAP 0.11.2). The hook
 *   keeps polling every `FIRST_RUN_POLL_MS`, so the page fills in by itself; `first-start.spec.ts`
 *   waits on this state.
 * - **Run not found** (a `runId` the API does not know): a "Back to default" link.
 * - **Any other failure**: the API's message and a "Retry" button that refetches.
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
import { useEffect, useRef, useState } from 'react';
import type { ComponentType, ReactElement, ReactNode } from 'react';

import { isApiError, isAwaitingFirstRun, useRecommendations } from '../../api/queries';
import type { ApiError } from '../../api/queries';
import { SECTION_ORDER } from '../../api/types';
import type { RecommendationsPage, Run, RunId, Section, SectionKey } from '../../api/types';
import { Button, EmptyState, PageShell, Spinner, Stack } from '../../design-system';
import { CustomRunBanner } from '../status/CustomRunBanner';
import { GlobalBanners } from '../status/GlobalBanners';
import { StatusBar } from '../status/StatusBar';
import { SectionNav } from './SectionNav';
import { BudgetSection } from './sections/BudgetSection';
import { CampaignsSection } from './sections/CampaignsSection';
import { CreativeSection } from './sections/CreativeSection';
import { KeywordsSection } from './sections/KeywordsSection';
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
   * A state message to announce, such as the "No analysis run yet" or failure `EmptyState`.
   * It is rendered inside the frame's polite status region; omitted, the region stays empty.
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
 * no space.
 */
function ActionsFrame({ notice, children }: ActionsFrameProps): ReactElement {
  return (
    <Stack gap={6}>
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <div role="status">{notice}</div>
      {children}
    </Stack>
  );
}

/** Props of {@link ActionsContent}. */
interface ActionsContentProps {
  /** One `GET /recommendations` response: the run, its overlay, banners and sections. */
  page: RecommendationsPage;
}

/**
 * The loaded page: status bar, custom-window banner (custom runs only), global banners, section
 * navigation and the five sections in {@link SECTION_ORDER}, each with the run and overlay of the
 * same response, so every part describes one moment.
 */
function ActionsContent({ page }: ActionsContentProps): ReactElement {
  const { run, overlay, banners, sections } = page;
  const sectionsByKey = firstSectionByKey(sections);

  return (
    <ActionsFrame>
      <StatusBar run={run} overlay={overlay} />
      {run.kind === 'custom' && <CustomRunBanner run={run} />}
      <GlobalBanners banners={banners} overlay={overlay} />
      <SectionNav sections={sections} />
      {SECTION_ORDER.map((key) => {
        const section = sectionsByKey.get(key);
        // The API always returns all five sections; a key it left out is skipped, never stubbed.
        if (section === undefined) {
          return null;
        }
        const SectionComponent = SECTION_COMPONENTS[key];
        return <SectionComponent key={key} section={section} run={run} overlay={overlay} />;
      })}
    </ActionsFrame>
  );
}

/**
 * The `/actions` screen: the five ranked action sections of one analysis run, with the status
 * bar, the global banners and the section navigation above them (AAP 0.5.5).
 *
 * It reads the page with one `useRecommendations(runId)` call and renders, in precedence order:
 *
 * - the loaded page whenever data exists, including after a failed background refetch;
 * - `PageShell.Title` "Actions" with a `Spinner` labelled "Loading actions" until the first
 *   response settles;
 * - without `runId`, an `EmptyState` "No analysis run yet" with "The first analysis is still
 *   computing. This page refreshes automatically." when the API reports no default run yet
 *   (404 `not_found` or 409 `run_not_ready`), beside a spinner; the hook polls until the first
 *   run arrives, and the page then shows it by itself;
 * - with `runId`, an `EmptyState` "Run not found" with a "Back to default" link to `/actions`
 *   when the API does not know the run (404, or 422 for an id that cannot name a run);
 * - otherwise an `EmptyState` "Actions could not be loaded" with the API's message and a
 *   "Retry" `Button` that reads the page again.
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
  const failure = useSettledFailure(
    runId,
    query.error,
    query.fetchStatus !== 'idle',
    query.data !== undefined,
  );

  // A Retry the user started: the button shows as loading, hence disabled, until it settles.
  const [retrying, setRetrying] = useState(false);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);

  // A disabled button loses focus to the page body, so once a Retry settles and the failure is
  // still shown, focus goes back to its button. Focus the user moved elsewhere in the meantime is
  // left alone, and a button that left the page (the retry succeeded) is skipped.
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
    return <ActionsContent page={query.data} />;
  }

  if (failure === null) {
    return (
      <ActionsFrame>
        <Spinner label={LOADING_LABEL} />
      </ActionsFrame>
    );
  }

  if (runId === undefined && isAwaitingFirstRun(failure)) {
    return (
      <ActionsFrame notice={<EmptyState title={FIRST_RUN_TITLE}>{FIRST_RUN_REASON}</EmptyState>}>
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
    <ActionsFrame
      notice={
        <EmptyState
          title={LOAD_ERROR_TITLE}
          action={
            <Button
              ref={retryButtonRef}
              loading={retrying}
              onClick={() => {
                void retry();
              }}
            >
              {RETRY_LABEL}
            </Button>
          }
        >
          {failureReason(failure)}
        </EmptyState>
      }
    />
  );
}
