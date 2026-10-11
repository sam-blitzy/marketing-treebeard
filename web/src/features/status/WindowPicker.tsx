/**
 * The analysis-window picker of the `/actions` status bar (AAP 0.5.5, Window picker).
 *
 * It offers each window of `window.presets` from `GET /config`, marks the preset equal to
 * `window.default_days` "(default)", and offers a custom range. Choosing a window resolves to
 * exactly one run (AAP 0.5.3, run selection):
 *
 * - **The default preset** shows the latest succeeded default run: it navigates to `/actions`
 *   with no search params and posts nothing. A default run is the worker's run of the configured
 *   window, and a `POST /runs` would be a custom run even for the same dates.
 * - **Any other preset** posts `POST /runs` with the preset's window ending on the default
 *   window end ({@link presetWindow}), then opens the created run at `/actions?run=<id>`.
 * - **A custom range** posts its own start and end, then opens the created run the same way.
 * - **A recent custom run** can be reopened from behind the "Recent custom windows" disclosure:
 *   choosing it in a select and pressing "Reopen" navigates to it by id.
 *
 * Layout: the picker keeps its own width; the "Recent custom windows" toggle sits on its preset
 * row right after "Custom range"; below the picker come the disclosed recent custom windows,
 * then the status region with the computing spinner and the warnings. Everything that opens
 * opens below the preset row, so nothing moves a control on it.
 *
 * A custom run is computed synchronously and can take up to about half a minute (AAP 0.1.1), so
 * the picker is disabled and a spinner shows while it computes. A failure is stated in a
 * warning banner: 409 `run_in_progress` as "Another analysis is running. Try again shortly.",
 * and every other API error, the 422 window-validation errors included, with the API's own
 * message.
 *
 * The two reads the picker depends on never fail silently. A failed `GET /runs/latest`
 * and, while the recent custom windows are open, a failed `GET /runs?kind=custom` are each
 * stated in a warning banner that names the read, gives the API's message and offers a Retry
 * button; meanwhile the presets end on the last default run read, or else on the run on screen,
 * and the reopen select keeps the runs it last listed. A 404 `not_found` or 409 `run_not_ready`
 * from `GET /runs/latest` is not a failure: no default run has succeeded yet, and the read polls
 * by itself until one has.
 *
 * Rules this file keeps:
 * - **No window length in the client** (AAP 0.5.4.1, 0.5.5). The presets, the default and the
 *   longest span come from `config.window`; window ends come from runs. While the configuration
 *   is unavailable, no picker is rendered rather than one with presets of its own.
 * - **Calendar dates stay calendar dates.** A window is one calendar-date range applied to each
 *   source's native days, so dates are computed in UTC only and shown exactly as stored; no
 *   time zone ever moves a day.
 * - **Custom-run history on request.** `GET /runs?kind=custom` is read only while "Recent custom
 *   windows" is open. The disclosure opens on a press, and a picker starts with it open only
 *   while the session still holds the list: the user has opened it and not closed it since.
 *   Closing it forgets the list, so it stays closed and unread until the user opens it again.
 *   Reading it on every load of `/actions` would add a request the five sections do not need to
 *   render within two seconds, so reopening a custom run stays one press away instead. Moving
 *   through the select's options only chooses; the "Reopen" button opens the choice, so the
 *   page never changes on input (WCAG 2.2 SC 3.2.2). Each option names the run's window, and
 *   its label is bounded by design: the range alone, or, for runs that share a window, the
 *   range and a copy number of at most "(20)", so the closed select shows each label whole at
 *   every width down to the 320 px reflow width. The copy number counts the listed runs of
 *   that window from the oldest: the oldest is the range alone, the next adds "(2)", and so
 *   on. The hint under the select names the chosen run's id, its length and the instant it
 *   finished in the run's display time zone, in full at every width.
 * - **Messages are announced politely.** The "Computing custom window" spinner and every
 *   warning banner of a failed computation or read sit in one `role="status"` region that
 *   every state of the picker renders in the same place, so each is announced when it appears
 *   (WCAG 2.2 SC 4.1.3) without `role="alert"`, which AAP 0.5.5 reserves for discrepancies.
 *   While it is empty, `Stack` takes the region out of the column, so it adds no space. A
 *   custom run on screen is announced as "Custom window {start} – {end} opened, {days}, run
 *   {id}" through the date-range picker's own hidden status region, which receives the text
 *   only after it exists, so the run is announced however it came on screen.
 * - **Boundaries.** UI comes only from the design-system index and is laid out with `Stack` and
 *   `Inline`, with no styles of its own (AAP 0.4.4); server state comes only from the TanStack
 *   Query hooks (AAP 0.11.1); navigation goes through TanStack Router's `useNavigate`.
 *   `src/router.tsx` is never imported: it imports the feature pages, and its `Register`
 *   augmentation types `to` and `search` here without an import.
 *
 * @packageDocumentation
 */
import { useNavigate } from '@tanstack/react-router';
import { useEffect, useId, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import {
  isApiError,
  isAwaitingFirstRun,
  useConfig,
  useCreateRun,
  useCustomRuns,
  useDefaultRun,
  useForgetCustomRuns,
  useHasCachedCustomRuns,
} from '../../api/queries';
import type { ApiError } from '../../api/queries';
import type { CreateRunRequest, IsoDate, Run } from '../../api/types';
import {
  Banner,
  Button,
  DateRangePicker,
  Inline,
  Select,
  Spinner,
  Stack,
} from '../../design-system';
import type { DateRangeValue, SelectOption } from '../../design-system';
import { formatDateRange, formatDays, formatInstant } from '../../lib/format';

// ---------------------------------------------------------------------------------------------
// Calendar-date arithmetic (UTC only)
// ---------------------------------------------------------------------------------------------

/** Milliseconds in one UTC calendar day: a unit conversion, not a window length. */
const MS_PER_DAY = 86_400_000;

/** An ISO calendar date, `YYYY-MM-DD`, capturing year, month and day. */
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * Parses an ISO `YYYY-MM-DD` calendar date to the UTC midnight of that date in epoch
 * milliseconds, or `null` when the text is not a real calendar date.
 *
 * The date is set on a UTC epoch with `setUTCFullYear`, never parsed as local time, so no time
 * zone can shift the day. This is the parse `DateRangePicker` validates `maxDate` with, so a
 * window end the picker accepts is always one this module accepts. `Date.UTC` is not used: it
 * remaps the two-digit years 0 to 99 onto the twentieth century. The result is read back and compared with the
 * input, which rejects dates that would otherwise roll over, such as `2026-02-30`.
 */
function parseIsoDate(text: string): number | null {
  const match = ISO_DATE.exec(text);
  if (match === null) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1) {
    return null;
  }
  // The epoch is UTC midnight, so the time of day stays 00:00:00.000 UTC.
  const parsed = new Date(0);
  const ms = parsed.setUTCFullYear(year, month - 1, day);
  if (
    Number.isNaN(ms) ||
    parsed.getUTCFullYear() !== year ||
    parsed.getUTCMonth() !== month - 1 ||
    parsed.getUTCDate() !== day
  ) {
    return null;
  }
  return ms;
}

/**
 * The calendar window of a preset: `days` days, both ends included, ending on `windowEnd`.
 *
 * `window_end` is `windowEnd` unchanged, and `window_start` is `windowEnd` minus `days − 1`
 * days, so the window holds exactly `days` calendar dates, as `Run.days` counts them. The
 * arithmetic runs on UTC midnights only and the start is read back with `toISOString`, so the
 * result is the same in every time zone the browser runs in: a calendar date has no time zone,
 * and local-time parsing could move it by a day.
 *
 * The status bar passes the default window end (the latest succeeded default run's
 * `window_end`), so every preset ends on the same date as the default run and the presets
 * differ only in length (AAP 0.5.4.1). `POST /runs` takes the result as its body.
 *
 * @example
 * ```ts
 * presetWindow(7, '2026-10-01'); // { window_start: '2026-09-25', window_end: '2026-10-01' }
 * presetWindow(1, '2026-10-01'); // { window_start: '2026-10-01', window_end: '2026-10-01' }
 * ```
 *
 * @param days - The window length in days: a whole number, 1 or more, from `window.presets`.
 * @param windowEnd - The last date of the window, inclusive, as an ISO `YYYY-MM-DD` date.
 * @returns The `POST /runs` body for that window.
 * @throws RangeError When `days` is not a whole number of days, 1 or more; when `windowEnd` is
 *   not a real calendar date; or when the start would fall before the year 1.
 */
export function presetWindow(days: number, windowEnd: IsoDate): CreateRunRequest {
  if (!Number.isSafeInteger(days) || days < 1) {
    throw new RangeError(`A window must be a whole number of days, 1 or more; received ${days}.`);
  }
  const endMs = parseIsoDate(windowEnd);
  if (endMs === null) {
    throw new RangeError(
      `The window end must be a calendar date (YYYY-MM-DD); received "${windowEnd}".`,
    );
  }
  const start = new Date(endMs - (days - 1) * MS_PER_DAY);
  // A start beyond the representable range reads back as NaN, which fails this check too.
  if (!(start.getUTCFullYear() >= 1)) {
    throw new RangeError(
      `A window of ${formatDays(days)} ending ${windowEnd} would start before the year 1.`,
    );
  }
  return { window_start: start.toISOString().slice(0, 10), window_end: windowEnd };
}

// ---------------------------------------------------------------------------------------------
// Messages and labels
// ---------------------------------------------------------------------------------------------

/** Shown for 409 `run_in_progress`: another process holds the analysis lock (AAP 0.5.3). */
const RUN_IN_PROGRESS_MESSAGE = 'Another analysis is running. Try again shortly.';

/** Shown for a failure that carries no API message of its own. */
const RUN_FAILED_MESSAGE = 'Could not compute the custom window.';

/** Shown when `GET /config` succeeded without the window settings the picker needs. */
const NO_WINDOW_CONFIG_MESSAGE = 'The configuration carried no window settings.';

/** The title of the warning for a failed `GET /runs/latest`. */
const DEFAULT_RUN_FAILED_TITLE = 'Could not read the latest default run';

/** The title of the warning for a failed `GET /runs?kind=custom`. */
const CUSTOM_RUNS_FAILED_TITLE = 'Could not load recent custom windows';

/** Stated for a failed read whose error carries no message of its own. */
const READ_FAILED_REASON = 'No reason was given.';

/**
 * The visible label of both Retry buttons. Each has its own accessible name, which starts with
 * this label (WCAG 2.2 SC 2.5.3).
 */
const RETRY_LABEL = 'Retry';

/** The empty choice of the reopen select, which keeps the select on no run. */
const NO_CUSTOM_RUN: SelectOption = { value: '', label: 'Choose a custom window' };

/** The label of the button that discloses the recent custom runs. */
const RECENT_CUSTOM_RUNS_LABEL = 'Recent custom windows';

/** The label of the spinner shown while the recent custom runs are first read. */
const CUSTOM_RUNS_LOADING_LABEL = 'Loading recent custom windows';

/** Shown in place of the reopen select when no succeeded custom run can be reopened. */
const NO_CUSTOM_RUNS_TEXT = 'No custom windows yet';

/** The label of the button that opens the custom run chosen in the reopen select. */
const REOPEN_LABEL = 'Reopen';

/**
 * The text that states a failed `POST /runs`:
 *
 * - 409 `run_in_progress`: {@link RUN_IN_PROGRESS_MESSAGE}, because the lock clears on its own
 *   and the user only needs to retry;
 * - 422, a window outside its bounds such as an end after the default window end: the API's
 *   validation message, which names the bound that failed;
 * - any other `ApiError`: its message, which `api/client.ts` already words for the user
 *   (a network failure or an unavailable server, for example);
 * - anything else, or an `ApiError` with an empty message: {@link RUN_FAILED_MESSAGE}.
 */
function createRunErrorMessage(error: unknown): string {
  if (!isApiError(error)) {
    return RUN_FAILED_MESSAGE;
  }
  if (error.status === 409 && error.code === 'run_in_progress') {
    return RUN_IN_PROGRESS_MESSAGE;
  }
  return error.message === '' ? RUN_FAILED_MESSAGE : error.message;
}

/**
 * The reason a failed read states: the `ApiError`'s message, which `api/client.ts` already words
 * for the user (a network failure or an unavailable server, for example), or
 * {@link READ_FAILED_REASON} for anything else or an empty message.
 */
function readErrorMessage(error: unknown): string {
  return isApiError(error) && error.message !== '' ? error.message : READ_FAILED_REASON;
}

/**
 * The key of a run's window: its `window_start` and `window_end` as stored. Two listed runs
 * share a window exactly when their keys are equal.
 *
 * @param run - A custom run listed by `GET /runs?kind=custom`.
 * @returns The run's window key.
 */
function windowKey(run: Run): string {
  return `${run.window_start}/${run.window_end}`;
}

/**
 * The copy number of each custom run the reopen select lists, by run id: 1 for the oldest
 * listed run of its window, 2 for the next listed run of the same window, and so on.
 *
 * Two runs share a window exactly when their `window_start` and `window_end` are equal
 * ({@link windowKey}). The API lists custom runs newest first, so a run's age is its place in
 * that list, counted from the end; the list itself is not re-sorted. Only the runs passed in
 * are counted, so a failed run, which the select never lists, takes no number. The picker
 * lists at most the 20 runs of one `GET /runs?kind=custom` page, so no copy number exceeds 20.
 *
 * @example
 * ```ts
 * // Newest first: runs 9 and 4 share a window, and run 7 has its own.
 * copyNumbers([run9, run7, run4]); // Map { 4 => 1, 7 => 1, 9 => 2 }
 * ```
 *
 * @param reopenable - The custom runs the reopen select lists, newest first as the API orders
 *   them.
 * @returns Each listed run's copy number among the listed runs of its window, keyed by run id.
 */
function copyNumbers(reopenable: readonly Run[]): ReadonlyMap<Run['id'], number> {
  // How many runs of each window the walk below has numbered so far.
  const numberedPerWindow = new Map<string, number>();
  const numbers = new Map<Run['id'], number>();
  // The list is newest first, so its reverse visits the runs of each window from the oldest.
  for (const run of [...reopenable].reverse()) {
    const key = windowKey(run);
    const copyNumber = (numberedPerWindow.get(key) ?? 0) + 1;
    numberedPerWindow.set(key, copyNumber);
    numbers.set(run.id, copyNumber);
  }
  return numbers;
}

/**
 * The reopen-select label of a custom run: its window, "{window_start} – {window_end}", with
 * " ({copyNumber})" added on every listed run of that window but the oldest. The dates are
 * shown exactly as stored.
 *
 * A native select can neither wrap nor truncate the text of its chosen option, so the label
 * is bounded by design: the range alone, or, for runs that share a window, the range and a
 * copy number of at most "(20)" ({@link copyNumbers}). The closed select therefore shows each
 * label whole at every width down to the 320 px reflow width (WCAG 2.2 SC 1.4.10). The copy
 * number counts the listed runs of the window from the oldest, so runs that share a window
 * read differently even when they finished in the same minute; the hint under the select
 * names the chosen run's id ({@link customRunHint}).
 *
 * @example
 * ```ts
 * // Run 3, the oldest listed run of its window, or the only one:
 * customRunLabel(run3, 1); // '2026-08-15 – 2026-10-09'
 * // Run 203, the second listed run of its window, counted from the oldest:
 * customRunLabel(run203, 2); // '2026-09-01 – 2026-09-30 (2)'
 * ```
 *
 * @param run - A custom run listed by `GET /runs?kind=custom`.
 * @param copyNumber - The run's copy number among the listed runs of its window
 *   ({@link copyNumbers}): 1 for the oldest, which is labelled by its range alone.
 * @returns The run's option label.
 */
function customRunLabel(run: Run, copyNumber: number): string {
  const range = formatDateRange(run.window_start, run.window_end);
  return copyNumber === 1 ? range : `${range} (${copyNumber})`;
}

/**
 * The hint under the reopen select for the custom run it shows: "Run {id} · {days} · computed
 * {finished_at}", for example "Run 203 · 7 days · computed Oct 6, 2026, 11:22 AM UTC". It names
 * the run's id, which its label leaves out: the label gives the window and, on a run that
 * shares its window, a copy number ({@link customRunLabel}). It also gives the run's own `days`
 * and the instant the run finished, formatted in the run's `display_timezone` (AAP 0.5.5). The
 * hint is wrapping text, so it shows whole at every width. `Select` references it by
 * `aria-describedby`, so it is also the select's accessible description.
 *
 * @param run - The custom run the reopen select shows.
 * @returns The hint text for that run.
 */
function customRunHint(run: Run): string {
  return `Run ${run.id} · ${formatDays(run.days)} · computed ${formatInstant(run.finished_at, run.display_timezone)}`;
}

// ---------------------------------------------------------------------------------------------
// WindowPicker
// ---------------------------------------------------------------------------------------------

/**
 * The error a read last settled with, kept while the read runs again.
 *
 * TanStack Query v5 puts a query that holds no data back to `status: 'pending'` with
 * `error: null` each time it fetches, so while a failed first read is retried or polled, its
 * result no longer carries the failure. Its warning and Retry button would leave the page for
 * the length of the request and return as new elements, and focus would be lost. The error the
 * read last settled with is therefore kept until the read settles again: with a new error, which
 * replaces it, or with data, which clears it. A read that holds data keeps its own `error` while
 * it refetches, so it is returned unchanged.
 *
 * @param error - The query's `error`.
 * @param isFetching - The query's `isFetching`.
 * @param hasData - Whether the query holds data.
 * @returns The error to state, or `null` while the read has not settled or last settled with
 *   data.
 */
function useSettledError(
  error: ApiError | null,
  isFetching: boolean,
  hasData: boolean,
): ApiError | null {
  const [kept, setKept] = useState<ApiError | null>(null);
  const settled = error === null && isFetching && !hasData ? kept : error;
  if (settled !== kept) {
    // State adjusted while rendering, from the read's result: React's pattern for information
    // kept from earlier renders. React renders again at once, before it commits anything.
    setKept(settled);
  }
  return settled;
}

/**
 * A choice made in the reopen select and not yet opened. Changing the select only records it,
 * so moving through the options never changes the page (WCAG 2.2 SC 3.2.2 On Input); the
 * "Reopen" button opens it.
 */
interface ReopenDraft {
  /** The id of the run on screen when the choice was made: the choice belongs to that run. */
  runId: Run['id'];
  /** The chosen option's value: a custom run's id as text, or the empty choice. */
  value: string;
}

/** Props of {@link PickerFrame}. */
interface PickerFrameProps {
  /** What the picker shows: a loading spinner, the missing-configuration warning or controls. */
  children: ReactNode;
  /**
   * Progress of work the picker started, such as the "Computing custom window" spinner, shown
   * first in the live region, before the notices. Omitted or `undefined`: none.
   */
  progress?: ReactElement | undefined;
  /** The warnings to announce, each a keyed `Banner`, in order. Omitted or empty: none. */
  notices?: readonly ReactElement[];
}

/**
 * The one root of every picker state: a column of the state's content followed by the picker's
 * polite live region, which holds the progress of work the picker started and then its notices.
 *
 * Every state renders this frame, so the root element and the region's place in it never change
 * and React keeps the same region element from the first render. Progress or a message that
 * appears later therefore lands in a live region that already exists, and is announced
 * politely (WCAG 2.2 SC 4.1.3) without `role="alert"`, which AAP 0.5.5 reserves for
 * discrepancies.
 */
function PickerFrame({ children, progress, notices = [] }: PickerFrameProps) {
  const announced = progress !== undefined || notices.length > 0;
  return (
    <Stack gap={3}>
      {children}
      {/* Always rendered, and always the frame's last child. With no progress and no notice
          nothing is rendered inside it, so it stays empty, and Stack takes an empty status
          region out of the column: it adds no space and no gap until something appears. */}
      <div role="status">
        {announced && (
          <Stack gap={2}>
            {progress}
            {notices}
          </Stack>
        )}
      </div>
    </Stack>
  );
}

/** Props of {@link WindowPicker}. */
export interface WindowPickerProps {
  /**
   * The run on screen: the latest default run at `/actions`, or the custom run that
   * `/actions?run=<id>` opened. Its window decides which preset is pressed or which custom
   * range is shown, and its `window_end` stands in for the default window end when the latest
   * default run cannot be read.
   */
  run: Run;
}

/**
 * The status bar's window picker: the configured presets plus a custom range, and, behind a
 * "Recent custom windows" disclosure, a select and a "Reopen" button that reopen a recent custom
 * run.
 *
 * - **Loading**: a "Loading windows" spinner until `GET /config` and `GET /runs/latest` have
 *   answered. Once the default `/actions` page has loaded, the latest default run is already
 *   cached from that page's own read, so only the configuration can hold the picker back.
 * - **No configuration**: a "Window presets unavailable" warning with the API's message, and no
 *   picker, because the client has no presets of its own.
 * - **Selection shown**: the preset of `run.days` is pressed when it is a configured preset and
 *   `run` ends on the default window end; otherwise `run`'s dates show as the custom range.
 * - **Choosing**: the default preset navigates to `/actions` with no search params; any other
 *   preset or a valid custom range posts `POST /runs` and, once the run is created, navigates to
 *   `/actions?run=<id>`. While it computes, every control is disabled and a "Computing custom
 *   window" spinner shows in the picker's status region below the controls, before any
 *   warning, so it is announced and moves no control; afterwards focus returns to the control
 *   that started it.
 * - **Failure to compute**: a warning banner states it ({@link createRunErrorMessage}) until the
 *   next choice clears it.
 * - **Failure to read the default run**: when `GET /runs/latest` fails with anything but "no
 *   default run yet" ({@link isAwaitingFirstRun}), a "Could not read the latest default run"
 *   warning gives the API's message and the date the presets end on meanwhile: the last
 *   default run read, or else `run.window_end`.
 * - **Failure to list custom runs**: when `GET /runs?kind=custom` fails while the recent custom
 *   windows are open, a "Could not load recent custom windows" warning gives the API's message;
 *   runs an earlier read listed stay in the select. Closing the list removes the warning.
 * - **Retrying**: each read's warning has a Retry button that reads it again. The button is
 *   disabled while a run computes, and shows a spinner while its read runs, with the warning
 *   kept in place ({@link useSettledError}); when the read fails again, focus returns to it, and
 *   when the read succeeds the warning goes.
 * - **Announcing**: the computing spinner and every warning are polite status messages, never
 *   alerts, and the warnings appear in the order above. A custom run on screen is announced as
 *   "Custom window {start} – {end} opened, {days}, run {id}" through `DateRangePicker`'s
 *   `statusMessage`, whenever the picker shows it: after a choice here, a reopen, Back or
 *   Forward, or a direct load. The default run announces nothing.
 * - **Reopening**: a quiet "Recent custom windows" button on the preset row, right after
 *   "Custom range" and outside the preset group, discloses the recent custom runs below the
 *   picker. It carries `aria-expanded` and `aria-controls`, naming a region whose content
 *   exists only while the list is open. The list starts closed, and only opening it reads
 *   `GET /runs?kind=custom`. It starts open while the session still holds the list, so moving
 *   between runs keeps it in view with the run on screen selected. Closing it forgets the list
 *   ({@link useForgetCustomRuns}), so it starts closed and unread on every later mount until it
 *   is opened again, which reads the list once. While open, the region shows a "Loading recent
 *   custom windows" spinner until the first read answers; then "Reopen a custom window" lists
 *   the succeeded custom runs newest first, each by its window, with a copy number such as
 *   "(2)" added on every listed run of a window but its oldest ({@link customRunLabel}). A hint
 *   under the select gives "Run {id}", the chosen run's length and the instant it finished in
 *   the run's display time zone ({@link customRunHint}), and none shows on the empty choice. A
 *   "Reopen" button sits below, or "No custom windows yet" says there is none to reopen.
 *   Choosing an option only records it; "Reopen" navigates to the chosen run by id. The select
 *   shows the run on screen until a choice is made, and a choice is dropped once another run is
 *   on screen. "Reopen" is disabled on the empty choice and while a run computes. After a failed
 *   first read the region stays empty and hidden, and the warning below the controls states the
 *   failure. The "Recent custom windows" button is disabled while a run computes, like every
 *   other control, and pressing it leaves focus on it.
 *
 * The component must render inside a TanStack `RouterProvider` and a `QueryClientProvider`, as
 * every `/actions` screen does.
 *
 * @example
 * ```tsx
 * <WindowPicker run={page.run} />
 * ```
 */
export function WindowPicker({ run }: WindowPickerProps) {
  const config = useConfig();
  const defaultRun = useDefaultRun();
  const createRun = useCreateRun();
  // The recent custom runs are read only while their disclosure is open. It starts open only
  // while the list is cached, that is while the user has opened it and not closed it since:
  // closing it forgets the list.
  const customRunsCached = useHasCachedCustomRuns();
  const forgetCustomRuns = useForgetCustomRuns();
  const [customRunsOpen, setCustomRunsOpen] = useState(customRunsCached);
  const customRunsRegionId = useId();
  const customRuns = useCustomRuns(undefined, { enabled: customRunsOpen });
  const navigate = useNavigate();
  // A window the client itself cannot turn into a request (presetWindow's RangeError). The
  // picker validates its configuration before it renders a preset, so this stays null unless
  // the two validations ever disagree; it is still shown rather than thrown from a handler.
  const [inputError, setInputError] = useState<string | null>(null);
  const pending = createRun.isPending;
  // The reopen select's choice not yet opened (ReopenDraft). It belongs to the run on screen
  // when it was made, so it is dropped as soon as another run is on screen, and the select then
  // shows that run. State adjusted while rendering, as in useSettledError.
  const [reopenDraft, setReopenDraft] = useState<ReopenDraft | null>(null);
  if (reopenDraft !== null && reopenDraft.runId !== run.id) {
    setReopenDraft(null);
  }

  // The reads whose failure is stated in a warning with a Retry button, kept while they run
  // again (useSettledError). A 404 `not_found` or 409 `run_not_ready` from GET /runs/latest is
  // not a failure: it means no default run has succeeded yet, and the query polls by itself
  // until one has.
  const defaultRunError = useSettledError(
    defaultRun.error,
    defaultRun.isFetching,
    defaultRun.data !== undefined,
  );
  const customRunsError = useSettledError(
    customRuns.error,
    customRuns.isFetching,
    customRuns.data !== undefined,
  );
  const defaultRunFailed = defaultRunError !== null && !isAwaitingFirstRun(defaultRunError);
  // Stated only while the list is open: a closed list is not read, so its last failure is moot.
  const customRunsFailed = customRunsOpen && customRunsError !== null;
  // Busy while a run computes, or while a failed read runs again: that read's Retry button is
  // then loading, hence inert.
  const busy =
    pending ||
    (defaultRunFailed && defaultRun.isFetching) ||
    (customRunsFailed && customRuns.isFetching);

  // The control that started the work: a preset button, Apply or a date field for a
  // computation, or a Retry button. Every control is disabled while the run computes, and a
  // Retry button while its read runs. A Button keeps focus meanwhile, but a disabled date field
  // loses it to the page body, so focus is returned to the control that started the work once
  // the picker is no longer busy (WCAG 2.2 SC 2.4.3). Focus the user has moved elsewhere in the
  // meantime is left alone, and a control that has left the page, after navigation to the
  // created run or a successful retry for example, is skipped.
  const focusReturnRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (busy) {
      return;
    }
    const target = focusReturnRef.current;
    focusReturnRef.current = null;
    const active = document.activeElement;
    if (
      target !== null &&
      target.isConnected &&
      !target.matches(':disabled') &&
      (active === null || active === document.body)
    ) {
      target.focus();
    }
  }, [busy]);

  const windowConfig = config.data?.window;
  if (windowConfig === undefined) {
    if (config.isPending) {
      return (
        <PickerFrame>
          <Spinner label="Loading windows" />
        </PickerFrame>
      );
    }
    // Never fall back to presets of the client's own: without the configuration no window
    // length is known (AAP 0.5.5).
    return (
      <PickerFrame>
        <Banner tone="warning" title="Window presets unavailable">
          {config.error?.message ?? NO_WINDOW_CONFIG_MESSAGE}
        </Banner>
      </PickerFrame>
    );
  }
  // Only until the first answer: a read with no data is reported pending again each time it
  // runs again (useSettledError), and the picker then stays on screen with its warning.
  if (defaultRun.isPending && !defaultRun.isFetched) {
    return (
      <PickerFrame>
        <Spinner label="Loading windows" />
      </PickerFrame>
    );
  }

  // The default window end: the latest succeeded default run's last date. When a later read of
  // it fails, the last one read still ends the presets; before the first default run, or when
  // it has never been read, the run on screen ends them instead. A failed read is stated in a
  // warning below.
  const defaultEnd: IsoDate = defaultRun.data?.window_end ?? run.window_end;

  const value: DateRangeValue =
    windowConfig.presets.includes(run.days) && run.window_end === defaultEnd
      ? { kind: 'preset', days: run.days }
      : { kind: 'custom', start: run.window_start, end: run.window_end };

  // A run that has not succeeded cannot be shown (its page answers 409 `run_not_ready`), so
  // only succeeded custom runs can be reopened. The API lists them newest first. Runs listed by
  // an earlier read stay listed when a later read fails, which is stated in a warning below.
  const reopenable = (customRuns.data ?? []).filter((custom) => custom.status === 'succeeded');
  // Each listed run's copy number among the listed runs of its window, from the oldest: the
  // oldest is labelled by its range alone and each later one adds its number (customRunLabel).
  // The options keep the API's newest-first order. copyNumbers numbers every run it is given,
  // so the lookup always finds one; a run it had not seen would have no earlier listed copy.
  const copies = copyNumbers(reopenable);
  const reopenOptions: readonly SelectOption[] = [
    NO_CUSTOM_RUN,
    ...reopenable.map((custom) => ({
      value: String(custom.id),
      label: customRunLabel(custom, copies.get(custom.id) ?? 1),
    })),
  ];
  const reopenValue = reopenable.some((custom) => custom.id === run.id)
    ? String(run.id)
    : NO_CUSTOM_RUN.value;
  // The select shows the choice made on the run on screen while it is still listed; otherwise
  // the run on screen, or the empty choice when that run is not listed.
  const reopenSelection =
    reopenDraft !== null &&
    reopenDraft.runId === run.id &&
    reopenOptions.some((option) => option.value === reopenDraft.value)
      ? reopenDraft.value
      : reopenValue;
  // The custom run the select shows, which "Reopen" opens and the hint under the select
  // describes; `undefined` on the empty choice, which opens nothing and has no hint.
  const chosen = reopenable.find((custom) => String(custom.id) === reopenSelection);

  // A custom run on screen is announced politely by the picker whenever it shows one: after a
  // choice in the picker, a reopen, Back or Forward, or a direct load (WCAG 2.2 SC 4.1.3).
  const openedMessage =
    run.kind === 'custom'
      ? `Custom window ${formatDateRange(run.window_start, run.window_end)} opened, ${formatDays(run.days)}, run ${run.id}`
      : undefined;

  const errorMessage =
    inputError ?? (createRun.isError ? createRunErrorMessage(createRun.error) : null);

  /** Clears the previous choice's failure before a choice that posts nothing. */
  const clearErrors = () => {
    setInputError(null);
    createRun.reset();
  };

  /** Opens a run by id: the custom-run view of `/actions` (AAP 0.5.3). */
  const openRun = (id: Run['id']) => {
    void navigate({ to: '/actions', search: { run: id } });
  };

  /** Records the focused control as the one focus returns to once the picker is not busy. */
  const rememberFocus = () => {
    const active = document.activeElement;
    focusReturnRef.current =
      active instanceof HTMLElement && active !== document.body ? active : null;
  };

  /** Computes a custom-window run and opens it once created; `mutate` clears the last error. */
  const compute = (body: CreateRunRequest) => {
    setInputError(null);
    rememberFocus();
    createRun.mutate(body, {
      onSuccess: (created) => {
        openRun(created.id);
      },
    });
  };

  const selectPreset = (days: number) => {
    if (days === windowConfig.default_days) {
      // The default window is the worker's default run, never a POST /runs.
      clearErrors();
      void navigate({ to: '/actions', search: {} });
      return;
    }
    let body: CreateRunRequest;
    try {
      body = presetWindow(days, defaultEnd);
    } catch (error) {
      if (error instanceof RangeError) {
        createRun.reset();
        setInputError(error.message);
        return;
      }
      throw error;
    }
    compute(body);
  };

  const selectCustom = (start: IsoDate, end: IsoDate) => {
    // The picker has already checked the end against maxDate and the span against maxDays;
    // the API stays authoritative, and its 422 is shown like any other failure.
    compute({ window_start: start, window_end: end });
  };

  /** Records a choice of the reopen select; nothing opens until "Reopen" is pressed. */
  const chooseReopen = (selected: string) => {
    setReopenDraft({ runId: run.id, value: selected });
  };

  /** Opens the custom run the reopen select shows, by id; the empty choice opens nothing. */
  const reopen = () => {
    if (chosen === undefined) {
      return;
    }
    clearErrors();
    openRun(chosen.id);
  };

  /** Reads the latest default run again, from its warning's Retry button. */
  const retryDefaultRun = () => {
    rememberFocus();
    void defaultRun.refetch();
  };

  /** Lists the recent custom runs again, from their warning's Retry button. */
  const retryCustomRuns = () => {
    rememberFocus();
    void customRuns.refetch();
  };

  // In this order: the choice just made, then the reads the picker depends on.
  const notices: ReactElement[] = [];
  if (errorMessage !== null) {
    notices.push(<Banner key="create-run" tone="warning" title={errorMessage} />);
  }
  if (defaultRunFailed) {
    notices.push(
      <Banner
        key="default-run"
        tone="warning"
        title={DEFAULT_RUN_FAILED_TITLE}
        actions={
          <Button
            size="sm"
            aria-label="Retry reading the latest default run"
            loading={defaultRun.isFetching}
            disabled={pending}
            onClick={retryDefaultRun}
          >
            {RETRY_LABEL}
          </Button>
        }
      >
        <Stack gap={1}>
          <span>{readErrorMessage(defaultRunError)}</span>
          <span>
            {`Until the latest default run can be read, preset windows end on ${defaultEnd}.`}
          </span>
        </Stack>
      </Banner>,
    );
  }
  if (customRunsFailed) {
    notices.push(
      <Banner
        key="custom-runs"
        tone="warning"
        title={CUSTOM_RUNS_FAILED_TITLE}
        actions={
          <Button
            size="sm"
            aria-label="Retry loading recent custom windows"
            loading={customRuns.isFetching}
            disabled={pending}
            onClick={retryCustomRuns}
          >
            {RETRY_LABEL}
          </Button>
        }
      >
        {readErrorMessage(customRunsError)}
      </Banner>,
    );
  }

  // The disclosed recent custom runs: nothing while closed or after a failed first read (its
  // warning states it), a spinner until the first read answers, then the reopen select or the
  // statement that no custom run can be reopened.
  let recentCustomRuns: ReactNode = null;
  if (customRunsOpen) {
    if (reopenable.length > 0) {
      // A change of the select only records a choice, and "Reopen" opens it (WCAG 2.2 SC 3.2.2
      // On Input). The outer Inline keeps the select at its own width, and the button sits on
      // its own line below it, so no row pairs controls of different heights.
      recentCustomRuns = (
        <Inline>
          <Stack gap={2}>
            <Select
              label="Reopen a custom window"
              options={reopenOptions}
              value={reopenSelection}
              onChange={chooseReopen}
              hint={chosen === undefined ? undefined : customRunHint(chosen)}
              disabled={pending}
            />
            <Inline>
              <Button
                disabled={pending || reopenSelection === NO_CUSTOM_RUN.value}
                onClick={reopen}
              >
                {REOPEN_LABEL}
              </Button>
            </Inline>
          </Stack>
        </Inline>
      );
    } else if (customRuns.data !== undefined) {
      recentCustomRuns = <span>{NO_CUSTOM_RUNS_TEXT}</span>;
    } else if (!customRunsFailed) {
      recentCustomRuns = <Spinner label={CUSTOM_RUNS_LOADING_LABEL} />;
    }
  }

  // The disclosure's toggle, on the picker's preset row right after "Custom range" and outside
  // the preset group, so it shares the toggles' line and height at every width.
  const recentCustomRunsToggle = (
    <Button
      variant="quiet"
      size="sm"
      aria-expanded={customRunsOpen}
      aria-controls={customRunsRegionId}
      disabled={pending}
      onClick={() => {
        // A closed list is no longer needed: forgetting it keeps every later mount closed and
        // unread until the user opens it again, which reads it afresh.
        if (customRunsOpen) {
          forgetCustomRuns();
        }
        setCustomRunsOpen(!customRunsOpen);
      }}
    >
      {RECENT_CUSTOM_RUNS_LABEL}
    </Button>
  );

  return (
    <PickerFrame
      notices={notices}
      progress={pending ? <Spinner label="Computing custom window" /> : undefined}
    >
      {/* The Inline keeps the picker and its custom panel at their own width rather than the
          column's. Everything that opens (the custom panel, the recent custom windows and the
          computing spinner) opens below the preset row, so none of it moves a control on it. */}
      <Inline>
        <DateRangePicker
          label="Analysis window"
          presets={windowConfig.presets}
          defaultPreset={windowConfig.default_days}
          value={value}
          onSelectPreset={selectPreset}
          onSelectCustom={selectCustom}
          maxDate={defaultEnd}
          maxDays={windowConfig.max_days}
          disabled={pending}
          actions={recentCustomRunsToggle}
          statusMessage={openedMessage}
        />
      </Inline>
      {/* The region the toggle controls, below the picker: always in place, hidden while it has
          nothing to show, so the toggle's aria-controls always names an element. */}
      <div id={customRunsRegionId} hidden={recentCustomRuns === null}>
        {recentCustomRuns}
      </div>
    </PickerFrame>
  );
}
