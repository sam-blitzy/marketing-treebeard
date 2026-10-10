/**
 * `SourcesPage`: the `/sources` screen, the health of every data source (AAP 0.5.5 "Other
 * screens → `/sources`").
 *
 * One `GET /sources` response (AAP 0.5.3) drives the page. For each source, in the order the API
 * returns them (Google, LinkedIn, HubSpot), it renders one section with:
 *
 * 1. a status line: the connection's mode ("Live" or "Simulated"), its status ("OK", "Stale",
 *    "Access denied" or "Error"), a "Stale" badge when one of the source's datasets is stale while
 *    the connection reports another status, the reason exactly as the API returns it (for example
 *    "credentials not configured", AAP 0.3.3) and the time of the last attempt;
 * 2. a polite status region with the outcome of this session's "Sync now" request;
 * 3. Google only: the webhook calls of the last 24 h against the product's own cap
 *    (`GOOGLE_DAILY_CALL_BUDGET`), which is separate from the Google Ads quota other n8n
 *    workflows share (AAP 0.2.4, 0.3.3);
 * 4. the active datasets: the primary dataset and, for HubSpot while LinkedIn is simulated, the
 *    companion CRM dataset (AAP 0.3.3);
 * 5. "Dataset freshness": the source's native day boundary ("Google days: America/New_York",
 *    "LinkedIn days: UTC", or "day boundary not yet known" while the API reports none), then one
 *    table per dataset, primary and companion kept apart, with each named dataset's completeness,
 *    covered range, last success, last attempt and reason (AAP 0.3.2). The covered dates are
 *    labelled with that boundary (AAP 0.5.5);
 * 6. "Recent sync runs": the newest `sync_run` rows, newest first;
 * 7. a "Sync now" button beside the section heading (`POST /sources/{source}/sync`).
 *
 * Rules this file keeps:
 * - **Server state only through `../../api/queries`** (AAP 0.11.1). `useSources` already polls and
 *   `useRequestSync` already invalidates the sources query, so nothing here sets an interval or
 *   invalidates.
 * - **Design system only** (AAP 0.4.4): UI from the design-system index, layout only through
 *   `Stack` and `Inline`; no raw headings, tables or buttons, no styles and no stylesheet.
 * - **Values from the API, verbatim**: dataset names, stored dates, completeness values, dataset
 *   modes and states, run statuses and reasons are printed as returned. Only the status line's
 *   badges carry display labels for enum values. Stored native dates are never passed through
 *   `Date`, which could shift a day (AAP 0.5.4.1): they are printed as stored, labelled with the
 *   day boundary `Source.day_boundary` reports, which the display time zone never replaces;
 *   instants are formatted in the display time zone. No call cap, poll interval, window length
 *   or time zone other than the documented UTC fallback for instants is written here.
 * - **No alert**: nothing here carries `role="alert"`, which AAP 0.5.5 reserves for discrepancies.
 * - **No router import**: `src/router.tsx` imports this page and renders the `PageShell` frame
 *   around it.
 *
 * @packageDocumentation
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactElement, ReactNode, RefObject } from 'react';

import { isApiError, useDefaultRun, useRequestSync, useSources } from '../../api/queries';
import type { ApiError } from '../../api/queries';
import type {
  Completeness,
  ConnectionStatus,
  DataMode,
  DatasetFreshnessView,
  DatasetView,
  Source,
  SourceName,
  SyncRunView,
} from '../../api/types';
import {
  Badge,
  Banner,
  Button,
  DataTable,
  EmptyState,
  Inline,
  PageShell,
  Spinner,
  Stack,
} from '../../design-system';
import type { DataTableColumn, Tone } from '../../design-system';
import {
  EM_DASH,
  formatCount,
  formatInstant,
  formatSourceDate,
  sourceLabel,
} from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Texts and label maps
// ---------------------------------------------------------------------------------------------

/** The page title, rendered as the page's single `<h1>` in every state. */
const PAGE_TITLE = 'Sources';

/**
 * The zone instants are formatted in until the latest default run reports `display_timezone`
 * (`GET /runs/latest` answers 404 `not_found` before the first analysis). The line above the
 * sources says so, so the fallback is never presented as the configured zone.
 */
const FALLBACK_TIMEZONE = 'UTC';

/** Accessible label of the spinner shown before the first `GET /sources` response. */
const LOADING_LABEL = 'Loading sources';

/** Title of the failure state when `GET /sources` fails before any response was loaded. */
const LOAD_ERROR_TITLE = 'Sources unavailable';

/** Reason shown for a failure that carries no usable message of its own. */
const GENERIC_ERROR_MESSAGE = 'Unexpected error';

/** Label of the button that repeats a failed `GET /sources`. */
const RETRY_LABEL = 'Retry';

/** Title of the state where the API lists no source at all. */
const NO_SOURCES_TITLE = 'No sources configured';

/** Title of the notice shown when a background refetch fails while earlier data is on screen. */
const REFRESH_ERROR_TITLE = 'Sources could not be refreshed';

/** Label of every source's sync button; the end-to-end specs press it by this name. */
const SYNC_NOW_LABEL = 'Sync now';

/** Title of the banner shown after a 202 from `POST /sources/{source}/sync`. */
const SYNC_REQUESTED_TITLE = 'Sync requested';

/** Body of that banner. The worker's wake interval is not written here. */
const SYNC_REQUESTED_BODY = 'The worker picks it up on its next scheduled wake.';

/**
 * Title of the banner for 409 `sync_pending`, and the hint beside a disabled "Sync now" while the
 * API reports a pending request.
 */
const SYNC_PENDING_TEXT = 'A sync is already pending';

/** Title of the banner for any other failure of `POST /sources/{source}/sync`. */
const SYNC_FAILED_TITLE = 'Sync request failed';

/** Row label of a source's active primary dataset. */
const PRIMARY_ROW_LABEL = 'Primary';

/** Row label of HubSpot's active companion CRM dataset, built with a simulated LinkedIn dataset. */
const COMPANION_ROW_LABEL = 'Companion (simulated LinkedIn)';

/** Shown in place of the active-datasets table before any dataset of the source is active. */
const NO_ACTIVE_DATASET_TEXT = 'No active dataset yet';

/** Activated cell of a dataset that has not become active. */
const NOT_ACTIVATED_TEXT = 'Not yet activated';

/** Freshness instant that was never recorded. */
const NEVER_TEXT = 'Never';

/** Finished cell of a sync run that has not finished. */
const RUNNING_TEXT = 'Running';

/** Empty message of a freshness table. */
const NO_FRESHNESS_TEXT = 'No freshness recorded yet';

/**
 * Stands in for the zone in a source's day-boundary line while the API reports none (Google
 * before its account time zone is stored), so no zone is invented for the covered dates.
 */
const UNKNOWN_BOUNDARY_TEXT = 'day boundary not yet known';

/** Empty message of the recent sync runs table. */
const NO_RUNS_TEXT = 'No sync runs yet';

/** Text of the status line before the worker's first attempt on a source. */
const NO_ATTEMPT_TEXT = 'No attempt yet';

/** Text of the "Stale" badges: a dataset never succeeded or last succeeded too long ago. */
const STALE_LABEL = 'Stale';

/** Explains the Google call count: the product's own cap, not Google's token-wide quota. */
const CALL_CAP_NOTE =
  "This cap is the product's own limit on webhook calls (GOOGLE_DAILY_CALL_BUDGET), separate from the Google Ads quota shared with other n8n workflows.";

/** The text and tone of one status-line badge. */
interface BadgeSpec {
  /** The badge text. */
  readonly label: string;
  /** The design-system tone that repeats the meaning of the text in colour. */
  readonly tone: Tone;
}

/**
 * The mode badge of each {@link DataMode}. The `Record` over the union makes a new mode a type
 * error until it has a label.
 */
const MODE_BADGES: Readonly<Record<DataMode, BadgeSpec>> = Object.freeze({
  live: { label: 'Live', tone: 'success' },
  simulated: { label: 'Simulated', tone: 'info' },
});

/**
 * The mode badge before the worker's first attempt (`Source.mode` is `null`): neither live nor
 * simulated yet, worded as the status bar's source chips word it.
 */
const MODE_UNKNOWN_BADGE: BadgeSpec = Object.freeze({ label: 'No data yet', tone: 'neutral' });

/**
 * The status badge of each {@link ConnectionStatus}. The `Record` over the union makes a new
 * status a type error until it has a label.
 */
const STATUS_BADGES: Readonly<Record<ConnectionStatus, BadgeSpec>> = Object.freeze({
  ok: { label: 'OK', tone: 'success' },
  stale: { label: 'Stale', tone: 'warning' },
  denied: { label: 'Access denied', tone: 'critical' },
  error: { label: 'Error', tone: 'critical' },
});

/** The status badge before the worker's first attempt (`Source.status` is `null`). */
const STATUS_UNKNOWN_BADGE: BadgeSpec = Object.freeze({
  label: 'Not checked yet',
  tone: 'neutral',
});

/**
 * The tone of each {@link Completeness} badge; the badge text is the value itself. The `Record`
 * over the union makes a new value a type error until it has a tone.
 */
const COMPLETENESS_TONES: Readonly<Record<Completeness, Tone>> = Object.freeze({
  complete: 'success',
  incomplete: 'warning',
  unknown: 'neutral',
});

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * A text the API returned, when it has visible content: `null`, `undefined` and blank strings
 * become `null`, so the page never renders "null", "undefined" or an empty cell where a dash
 * belongs. A non-blank text is returned as given, never rewritten.
 *
 * @param value - the API's text
 * @returns the text, or `null` when there is nothing to show
 */
function visibleText(value: string | null | undefined): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

/**
 * The reason shown for a failed read or request: the `ApiError`'s message when it has one, and
 * {@link GENERIC_ERROR_MESSAGE} otherwise.
 *
 * @param error - the query's or mutation's error
 * @returns the non-empty reason text
 */
function failureReason(error: unknown): string {
  return (isApiError(error) ? visibleText(error.message) : null) ?? GENERIC_ERROR_MESSAGE;
}

/**
 * The error `GET /sources` last settled with while no response is loaded, kept while the read
 * runs again.
 *
 * TanStack Query v5 puts a query that holds no data back to `status: 'pending'` with
 * `error: null` each time it fetches. `useSources` polls, so without this hook every poll after a
 * failure, and every Retry, would swap the failure state for the loading spinner and back: the
 * screen would flicker and the Retry button, and keyboard focus with it, would be replaced
 * (WCAG 2.2 SC 2.4.3). The failure is kept until the read settles again, with a new error that
 * replaces it or with data that clears it.
 *
 * The state is adjusted while rendering, React's pattern for information kept from earlier
 * renders; the guard makes the second render a no-op.
 *
 * @param error - the query's `error`
 * @param hasData - whether the query holds data
 * @returns the failure to present, or `null` while there is data or no read has failed yet
 */
function useSettledError(error: ApiError | null, hasData: boolean): ApiError | null {
  const [kept, setKept] = useState<ApiError | null>(null);
  const settled = hasData ? null : (error ?? kept);
  if (settled !== kept) {
    setKept(settled);
  }
  return settled;
}

/**
 * Returns keyboard focus to a button once the work it started has settled.
 *
 * A design-system `Button` is natively disabled while `loading`, and a disabled button drops
 * focus to the page body. Call the returned `arm` function when the user presses the button;
 * once `busy` turns `false` again, focus goes back to it, provided it is still on the page and
 * enabled and focus has not been moved elsewhere in the meantime (WCAG 2.2 SC 2.4.3). Background
 * work the user did not start never moves focus.
 *
 * @param busy - whether the work the button started is still running
 * @param buttonRef - the button to refocus
 * @returns `arm`, to call from the button's click handler
 */
function useFocusReturn(
  busy: boolean,
  buttonRef: RefObject<HTMLButtonElement | null>,
): () => void {
  const armedRef = useRef(false);

  useEffect(() => {
    if (busy || !armedRef.current) {
      return;
    }
    armedRef.current = false;
    const button = buttonRef.current;
    const active = document.activeElement;
    if (
      button !== null &&
      button.isConnected &&
      !button.disabled &&
      (active === null || active === document.body)
    ) {
      button.focus();
    }
  }, [busy, buttonRef]);

  return () => {
    armedRef.current = true;
  };
}

/**
 * Whether any dataset shown in a source's section is stale: it never succeeded, or it last
 * succeeded more than `sources.stale_after_hours` ago, as `GET /sources` decides from the
 * configuration. The coverage-versus-window-end check of AAP 0.3.2 applies to recommendation
 * items through the run overlay, not to this screen, which has no analysis window. `Source` has
 * no flag of its own for this, so it is derived from the freshness rows, primary and companion.
 *
 * @param source - one source of `GET /sources`
 * @returns `true` when at least one freshness row is stale
 */
function hasStaleDataset(source: Source): boolean {
  return (
    source.datasets.some((row) => row.stale) || source.companion_datasets.some((row) => row.stale)
  );
}

/**
 * The completeness badge of a freshness row or sync run: the value as the API returns it, in its
 * tone.
 *
 * @param completeness - `complete`, `incomplete` or `unknown`
 * @returns the badge
 */
function completenessBadge(completeness: Completeness): ReactElement {
  return <Badge tone={COMPLETENESS_TONES[completeness]}>{completeness}</Badge>;
}

/** One row of a source's active-datasets table. */
interface ActiveDatasetRow {
  /** Unique within the table: the dataset's role. */
  readonly key: string;
  /** The row header: {@link PRIMARY_ROW_LABEL} or {@link COMPANION_ROW_LABEL}. */
  readonly label: string;
  /** The dataset. */
  readonly dataset: DatasetView;
}

/**
 * The rows of a source's active-datasets table: the primary dataset, then HubSpot's companion,
 * each only when the API reports it active.
 *
 * @param source - one source of `GET /sources`
 * @returns zero, one or two rows
 */
function activeDatasetRows(source: Source): ActiveDatasetRow[] {
  const rows: ActiveDatasetRow[] = [];
  if (source.active_dataset !== null) {
    rows.push({ key: 'primary', label: PRIMARY_ROW_LABEL, dataset: source.active_dataset });
  }
  if (source.companion_dataset !== null) {
    rows.push({ key: 'companion', label: COMPANION_ROW_LABEL, dataset: source.companion_dataset });
  }
  return rows;
}

/**
 * The columns of the active-datasets table. Mode and state are printed as the API returns them.
 *
 * @param displayTimezone - the IANA zone instants are shown in
 * @returns Dataset (row header), ID, Mode, State, Activated and Reason
 */
function activeDatasetColumns(displayTimezone: string): DataTableColumn<ActiveDatasetRow>[] {
  return [
    { key: 'label', header: 'Dataset', isRowHeader: true, render: (row) => row.label },
    { key: 'id', header: 'ID', render: (row) => String(row.dataset.id) },
    { key: 'mode', header: 'Mode', render: (row) => row.dataset.mode },
    { key: 'state', header: 'State', render: (row) => row.dataset.state },
    {
      key: 'activated',
      header: 'Activated',
      render: (row) =>
        row.dataset.activated_at === null
          ? NOT_ACTIVATED_TEXT
          : formatInstant(row.dataset.activated_at, displayTimezone),
    },
    {
      key: 'reason',
      header: 'Reason',
      render: (row) => visibleText(row.dataset.reason) ?? EM_DASH,
    },
  ];
}

/**
 * A freshness instant in the display zone, or {@link NEVER_TEXT} when none was recorded.
 *
 * @param instant - the stored instant
 * @param displayTimezone - the IANA zone to show it in
 * @returns the cell text
 */
function freshnessInstant(instant: string | null, displayTimezone: string): string {
  return visibleText(instant) === null ? NEVER_TEXT : formatInstant(instant, displayTimezone);
}

/**
 * A covered date of a freshness row, as stored and labelled with its source's day (AAP 0.5.5:
 * "Daily dates are shown as stored, labelled with their source's day boundary"), for example
 * `"2026-10-05 (Google day, America/New_York)"`. Without a known boundary the label names the
 * source's day only (`"2026-10-05 (Google day)"`), never a guessed zone. A missing date is
 * {@link EM_DASH}.
 *
 * @param date - the stored native date
 * @param source - the source whose day it is
 * @param dayBoundary - the IANA zone that ends the source's days, or `null` while unknown
 * @returns the cell text
 */
function coveredDate(date: string | null, source: SourceName, dayBoundary: string | null): string {
  return visibleText(date) === null ? EM_DASH : formatSourceDate(date, source, dayBoundary);
}

/**
 * The day-boundary line above a source's freshness tables, worded as the status bar words a
 * boundary (`"{Label} days: {boundary}"`, for example "Google days: America/New_York" or
 * "HubSpot days: UTC"), or "{Label} days: day boundary not yet known" while the API reports
 * none. The boundary comes from the API only.
 *
 * @param source - the source whose dates the tables hold
 * @param dayBoundary - the IANA zone that ends the source's days, or `null` while unknown
 * @returns the line's text
 */
function dayBoundaryLine(source: SourceName, dayBoundary: string | null): string {
  return `${sourceLabel(source)} days: ${dayBoundary ?? UNKNOWN_BOUNDARY_TEXT}`;
}

/**
 * The columns of a dataset-freshness table, in the order AAP 0.3.2 lists the fields.
 *
 * The dataset name is printed exactly as stored, and so are the covered dates, each labelled
 * with the source's native day boundary ({@link coveredDate}): a native date is never parsed
 * through `Date`, which reads `"2026-10-01"` as UTC midnight and can shift the day in another
 * zone, and the display time zone never moves it (AAP 0.5.4.1). A stale row carries a "Stale"
 * badge beside its name. A source's primary and companion tables share its boundary: the
 * companion is a HubSpot dataset too.
 *
 * @param displayTimezone - the IANA zone instants are shown in
 * @param source - the source whose datasets the table lists
 * @param dayBoundary - the IANA zone that ends the source's days, or `null` while unknown
 * @returns Dataset (row header), Completeness, Covered from, Covered through, Last success,
 *   Last attempt and Reason
 */
function freshnessColumns(
  displayTimezone: string,
  source: SourceName,
  dayBoundary: string | null,
): DataTableColumn<DatasetFreshnessView>[] {
  return [
    {
      key: 'dataset',
      header: 'Dataset',
      isRowHeader: true,
      render: (row) =>
        row.stale ? (
          <Inline gap={2} align="center">
            <span>{row.dataset}</span>
            <Badge tone="warning">{STALE_LABEL}</Badge>
          </Inline>
        ) : (
          row.dataset
        ),
    },
    {
      key: 'completeness',
      header: 'Completeness',
      render: (row) => completenessBadge(row.completeness),
    },
    {
      key: 'covered_from',
      header: 'Covered from',
      render: (row) => coveredDate(row.covered_from, source, dayBoundary),
    },
    {
      key: 'covered_through',
      header: 'Covered through',
      render: (row) => coveredDate(row.covered_through, source, dayBoundary),
    },
    {
      key: 'last_success_at',
      header: 'Last success',
      render: (row) => freshnessInstant(row.last_success_at, displayTimezone),
    },
    {
      key: 'last_attempt_at',
      header: 'Last attempt',
      render: (row) => freshnessInstant(row.last_attempt_at, displayTimezone),
    },
    { key: 'reason', header: 'Reason', render: (row) => visibleText(row.reason) ?? EM_DASH },
  ];
}

/**
 * The columns of the recent sync runs table. Job, dataset, status and completeness are printed as
 * the API returns them; a run still in progress reads "Running" in Finished.
 *
 * @param displayTimezone - the IANA zone instants are shown in
 * @returns Job, Dataset, Started, Finished, Status, Rows (end-aligned), Completeness and Error
 */
function syncRunColumns(displayTimezone: string): DataTableColumn<SyncRunView>[] {
  return [
    { key: 'job', header: 'Job', isRowHeader: true, render: (run) => run.job },
    { key: 'dataset', header: 'Dataset', render: (run) => visibleText(run.dataset) ?? EM_DASH },
    {
      key: 'started_at',
      header: 'Started',
      render: (run) => formatInstant(run.started_at, displayTimezone),
    },
    {
      key: 'finished_at',
      header: 'Finished',
      render: (run) =>
        run.finished_at === null ? RUNNING_TEXT : formatInstant(run.finished_at, displayTimezone),
    },
    { key: 'status', header: 'Status', render: (run) => run.status },
    { key: 'rows', header: 'Rows', align: 'end', render: (run) => formatCount(run.rows) },
    {
      key: 'completeness',
      header: 'Completeness',
      render: (run) => (run.completeness === null ? EM_DASH : completenessBadge(run.completeness)),
    },
    { key: 'error', header: 'Error', render: (run) => visibleText(run.error) ?? EM_DASH },
  ];
}

// ---------------------------------------------------------------------------------------------
// "Sync now" outcome
// ---------------------------------------------------------------------------------------------

/**
 * The outcome of this session's last "Sync now" request for one source:
 * - `requested`: 202, a `sync_request` is queued;
 * - `pending`: 409 `sync_pending`, a request for the source was already queued;
 * - `failed`: any other error, with the API's message.
 */
type SyncOutcome =
  | { readonly kind: 'requested' }
  | { readonly kind: 'pending' }
  | { readonly kind: 'failed'; readonly message: string };

/**
 * The outcome to show, derived from the section's own mutation state, so it lasts until the
 * next press and never leaks into another source's section. While a request is in flight there
 * is none, so each settled outcome enters the status region afresh and is announced.
 *
 * @param mutation - the status and error of the section's `useRequestSync()` mutation
 * @returns the outcome, or `null` before the first press and while a request is in flight
 */
function syncOutcome(mutation: {
  readonly isSuccess: boolean;
  readonly error: ApiError | null;
}): SyncOutcome | null {
  if (mutation.isSuccess) {
    return { kind: 'requested' };
  }
  if (mutation.error === null) {
    return null;
  }
  if (isApiError(mutation.error) && mutation.error.code === 'sync_pending') {
    return { kind: 'pending' };
  }
  return { kind: 'failed', message: failureReason(mutation.error) };
}

/**
 * The banner of a "Sync now" outcome. None of them carries `alert`: AAP 0.5.5 reserves
 * `role="alert"` for discrepancies, and the section's status region announces the outcome.
 *
 * @param outcome - the settled outcome
 * @returns the banner
 */
function syncOutcomeBanner(outcome: SyncOutcome): ReactElement {
  switch (outcome.kind) {
    case 'requested':
      return (
        <Banner tone="success" title={SYNC_REQUESTED_TITLE}>
          {SYNC_REQUESTED_BODY}
        </Banner>
      );
    case 'pending':
      return <Banner tone="info" title={SYNC_PENDING_TEXT} />;
    case 'failed':
      return (
        <Banner tone="critical" title={SYNC_FAILED_TITLE}>
          {outcome.message}
        </Banner>
      );
  }
}

// ---------------------------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------------------------

/** Props of {@link SourceSection}. */
interface SourceSectionProps {
  /** One source of `GET /sources`. */
  source: Source;
  /** The IANA zone every instant of the section is shown in. */
  displayTimezone: string;
}

/**
 * One source's section of `/sources`: status line, "Sync now" outcome, Google's call budget, the
 * active datasets, dataset freshness and recent sync runs, with "Sync now" beside the heading.
 * "Dataset freshness" opens with the source's native day boundary, and its covered dates carry
 * the same label.
 *
 * The section is a `<section>` region named by its `<h2>`, the source's display name
 * (`source-{name}`), so assistive technology and tests reach it as the region "Google",
 * "LinkedIn" or "HubSpot". Each section owns its `useRequestSync()` mutation: a request pending
 * or failing for one source never changes another source's button or message.
 *
 * "Sync now" is disabled, with the visible hint "A sync is already pending" as its description,
 * while the API reports a pending request and no outcome of this session is shown. Once a press
 * has settled, its outcome stays and the button stays available: a second press while the first
 * request is still queued answers 409 and shows "A sync is already pending".
 */
function SourceSection({ source, displayTimezone }: SourceSectionProps): ReactElement {
  const requestSync = useRequestSync();
  const syncButtonRef = useRef<HTMLButtonElement>(null);
  const armSyncFocus = useFocusReturn(requestSync.isPending, syncButtonRef);

  const name = source.source;
  const label = sourceLabel(name);
  const sectionId = `source-${name}`;
  const pendingHintId = `${sectionId}-sync-pending`;

  const outcome = syncOutcome(requestSync);
  const showPendingHint = source.sync_pending && outcome === null && !requestSync.isPending;

  const modeBadge = source.mode === null ? MODE_UNKNOWN_BADGE : MODE_BADGES[source.mode];
  const statusBadge = source.status === null ? STATUS_UNKNOWN_BADGE : STATUS_BADGES[source.status];
  const showStaleBadge = source.status !== 'stale' && hasStaleDataset(source);
  const reason = visibleText(source.reason);

  const datasetRows = activeDatasetRows(source);
  const hasCompanion = source.companion_dataset !== null || source.companion_datasets.length > 0;
  const dayBoundary = visibleText(source.day_boundary);
  const freshness = freshnessColumns(displayTimezone, name, dayBoundary);

  const syncNow = (
    <Button
      ref={syncButtonRef}
      variant="secondary"
      loading={requestSync.isPending}
      disabled={showPendingHint}
      aria-describedby={showPendingHint ? pendingHintId : undefined}
      onClick={() => {
        armSyncFocus();
        requestSync.mutate(name);
      }}
    >
      {SYNC_NOW_LABEL}
    </Button>
  );

  return (
    <PageShell.Section id={sectionId} title={label} actions={syncNow}>
      <Stack gap={4}>
        <Inline gap={3} align="center">
          <Badge tone={modeBadge.tone}>{modeBadge.label}</Badge>
          <Badge tone={statusBadge.tone}>{statusBadge.label}</Badge>
          {showStaleBadge && <Badge tone="warning">{STALE_LABEL}</Badge>}
          {reason !== null && <span>{`Reason: ${reason}`}</span>}
          <span>
            {source.last_attempt_at === null
              ? NO_ATTEMPT_TEXT
              : `Last attempt ${formatInstant(source.last_attempt_at, displayTimezone)}`}
          </span>
        </Inline>

        {/*
          The outcome region is rendered in every state, empty until a request settles, so the
          outcome lands in a region that already exists and is announced politely
          (WCAG 2.2 SC 4.1.3). Stack takes the empty region out of the column, so it adds no
          space. It is a plain region, never an alert (AAP 0.5.5).
        */}
        <div role="status" aria-live="polite">
          {outcome !== null && syncOutcomeBanner(outcome)}
          {showPendingHint && <div id={pendingHintId}>{SYNC_PENDING_TEXT}</div>}
        </div>

        {name === 'google' && source.call_cap !== null && (
          <Stack gap={1}>
            <div>
              {`Google calls in the last 24 h: ${formatCount(source.calls_24h)} of ${formatCount(source.call_cap)}`}
            </div>
            <div>{CALL_CAP_NOTE}</div>
          </Stack>
        )}

        {source.active_dataset === null && <div>{NO_ACTIVE_DATASET_TEXT}</div>}
        {datasetRows.length > 0 && (
          <DataTable
            caption={`${label} active datasets`}
            columns={activeDatasetColumns(displayTimezone)}
            rows={datasetRows}
            getRowKey={(row) => row.key}
          />
        )}

        <PageShell.Section id={`${sectionId}-freshness`} title="Dataset freshness" headingLevel={3}>
          <Stack gap={4}>
            {/*
              The native day boundary of every covered date below, primary and companion; the
              page-level "Times shown in" line names the zone of instants, never of these dates.
            */}
            <div>{dayBoundaryLine(name, dayBoundary)}</div>
            <DataTable
              caption={`${label} dataset freshness`}
              columns={freshness}
              rows={source.datasets}
              getRowKey={(row) => row.dataset}
              emptyMessage={NO_FRESHNESS_TEXT}
            />
            {hasCompanion && (
              <DataTable
                caption={`${label} companion dataset freshness (simulated LinkedIn)`}
                columns={freshness}
                rows={source.companion_datasets}
                getRowKey={(row) => row.dataset}
                emptyMessage={NO_FRESHNESS_TEXT}
              />
            )}
          </Stack>
        </PageShell.Section>

        <PageShell.Section id={`${sectionId}-runs`} title="Recent sync runs" headingLevel={3}>
          <DataTable
            caption={`Recent sync runs (${label})`}
            columns={syncRunColumns(displayTimezone)}
            rows={source.recent_runs}
            getRowKey={(run) => String(run.id)}
            emptyMessage={NO_RUNS_TEXT}
          />
        </PageShell.Section>
      </Stack>
    </PageShell.Section>
  );
}

/**
 * The `/sources` screen: health, mode, active datasets, dataset freshness, the Google call budget
 * and recent sync runs of every source, each with "Sync now" (AAP 0.5.5 "Other screens →
 * `/sources`").
 *
 * Data comes from `useSources()` (`GET /sources`, polled by the hook) and the display time zone
 * from `useDefaultRun()` (`Run.display_timezone`; `GET /config` carries none). Until a default
 * run exists (404 `not_found` at first start) instants are shown in UTC, and the line under the
 * title says "Times shown in UTC (display time zone not yet known)".
 *
 * States, in precedence order:
 * - **Data**: one section per source in API order, never re-sorted; "No sources configured" for
 *   an empty list. A background refetch that fails keeps the last response on screen, the
 *   sections or the empty list alike, under a "Sources could not be refreshed" notice that ends
 *   "Showing the last loaded state.", because TanStack Query keeps `data` on a failed refetch.
 *   The hook's own polling recovers by itself, and the notice goes once a refetch succeeds.
 * - **Loading**: a `Spinner` labelled "Loading sources".
 * - **Failure** before any response: "Sources unavailable" with the API's message and a "Retry"
 *   button that reads the sources again. The failure stays on screen while the read runs again,
 *   by poll or by Retry, so it never flickers to the spinner, and focus returns to Retry once a
 *   retry the user started has settled.
 *
 * One polite live region (`aria-live="polite"`) sits under the title in every state and stays
 * the same element from the first render. It holds the spinner, the failure with its Retry, or
 * the refresh notice, so the failure that replaces the spinner and a refresh failure while data
 * is on screen are both announced (WCAG 2.2 SC 4.1.3); it is empty while the sources are current.
 * The sections, with their tables and their own "Sync now" status regions, render outside it.
 * The page renders inside the router's authenticated `PageShell` layout and its
 * `QueryClientProvider`; it takes no props and renders no frame.
 *
 * @returns the `/sources` page content
 *
 * @example
 * ```tsx
 * // src/router.tsx, /sources route component:
 * component: SourcesPage,
 * ```
 */
export function SourcesPage(): ReactElement {
  const sources = useSources();
  const defaultRun = useDefaultRun();
  const failure = useSettledError(sources.error, sources.data !== undefined);

  // A Retry the user started: the button shows as loading, hence disabled, until it settles.
  const [retrying, setRetrying] = useState(false);
  const retryButtonRef = useRef<HTMLButtonElement>(null);
  const armRetryFocus = useFocusReturn(retrying, retryButtonRef);

  const configuredZone = visibleText(defaultRun.data?.display_timezone);
  const displayTimezone = configuredZone ?? FALLBACK_TIMEZONE;
  const zoneLine =
    configuredZone === null
      ? `Times shown in ${FALLBACK_TIMEZONE} (display time zone not yet known)`
      : `Times shown in ${configuredZone}`;

  const retry = async (): Promise<void> => {
    armRetryFocus();
    setRetrying(true);
    try {
      // `refetch` never rejects: a failed read settles into the query's error instead.
      await sources.refetch();
    } finally {
      setRetrying(false);
    }
  };

  // What the page's polite region holds (`notice`) and what follows it outside the region
  // (`content`): only short state messages are announced, never the sources' tables.
  let notice: ReactNode = null;
  let content: ReactNode = null;
  if (sources.data !== undefined) {
    const list = sources.data;
    if (sources.error !== null) {
      notice = (
        <Banner tone="warning" title={REFRESH_ERROR_TITLE}>
          {`${failureReason(sources.error)} Showing the last loaded state.`}
        </Banner>
      );
    }
    content =
      list.length === 0 ? (
        <EmptyState title={NO_SOURCES_TITLE} />
      ) : (
        list.map((source) => (
          <SourceSection key={source.source} source={source} displayTimezone={displayTimezone} />
        ))
      );
  } else if (failure === null) {
    notice = <Spinner label={LOADING_LABEL} />;
  } else {
    notice = (
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
    );
  }

  return (
    <Stack gap={6}>
      <Stack gap={2}>
        <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
        {/*
          BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive and feature code
          carries no CSS (AAP 0.4.4), so this line is a plain <div> spaced by the Stack gap token;
          the muted text colour waits for a Blitzy OS text component.
        */}
        <div>{zoneLine}</div>
      </Stack>
      {/*
        The page's one polite region, at the same place in every state, so React keeps the same
        element from the first render: the failure that replaces the spinner, and a refresh
        failure while the sources are on screen, land in a region that already exists and are
        announced (WCAG 2.2 SC 4.1.3). It holds only those messages; the sources, their tables
        and their "Sync now" regions follow outside it. It is not a `status` role, because the
        Spinner inside it is the page's one status element while loading, and never an alert
        (AAP 0.5.5). While it is empty, Stack takes it out of the column, so it adds no space.
      */}
      <div aria-live="polite">{notice}</div>
      {content}
    </Stack>
  );
}
