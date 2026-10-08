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
 * - **A recent custom run** can be reopened from a select, which navigates to it by id.
 *
 * A custom run is computed synchronously and can take up to about half a minute (AAP 0.1.1), so
 * the picker is disabled and a spinner shows while it computes. A failure is stated in a
 * warning banner: 409 `run_in_progress` as "Another analysis is running. Try again shortly.",
 * and every other API error, the 422 window-validation errors included, with the API's own
 * message.
 *
 * Rules this file keeps:
 * - **No window length in the client** (AAP 0.5.4.1, 0.5.5). The presets, the default and the
 *   longest span come from `config.window`; window ends come from runs. While the configuration
 *   is unavailable, no picker is rendered rather than one with presets of its own.
 * - **Calendar dates stay calendar dates.** A window is one calendar-date range applied to each
 *   source's native days, so dates are computed in UTC only and shown exactly as stored; no
 *   time zone ever moves a day.
 * - **Messages are announced politely.** The error banner sits in a `role="status"` region that
 *   is always rendered, so the message is announced when it appears (WCAG 2.2 SC 4.1.3) without
 *   `role="alert"`, which AAP 0.5.5 reserves for discrepancies.
 * - **Boundaries.** UI comes only from the design-system index and is laid out with `Inline`,
 *   with no styles of its own (AAP 0.4.4); server state comes only from the TanStack Query hooks
 *   (AAP 0.11.1); navigation goes through TanStack Router's `useNavigate`. `src/router.tsx` is
 *   never imported: it imports the feature pages, and its `Register` augmentation types `to` and
 *   `search` here without an import.
 *
 * @packageDocumentation
 */
import { useNavigate } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';

import {
  isApiError,
  useConfig,
  useCreateRun,
  useCustomRuns,
  useDefaultRun,
} from '../../api/queries';
import type { CreateRunRequest, IsoDate, Run } from '../../api/types';
import { Banner, DateRangePicker, Inline, Select, Spinner } from '../../design-system';
import type { DateRangeValue, SelectOption } from '../../design-system';
import { formatDateRange, formatDays } from '../../lib/format';

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

/** The empty choice of the reopen select, which keeps the select on no run. */
const NO_CUSTOM_RUN: SelectOption = { value: '', label: 'Choose a custom window' };

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
 * The reopen-select label of a custom run: "{window_start} – {window_end} · {days}", for
 * example "2026-09-25 – 2026-10-01 · 7 days". The dates are shown exactly as stored and the
 * length is the run's own `days`.
 */
function customRunLabel(run: Run): string {
  return `${formatDateRange(run.window_start, run.window_end)} · ${formatDays(run.days)}`;
}

// ---------------------------------------------------------------------------------------------
// WindowPicker
// ---------------------------------------------------------------------------------------------

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
 * The status bar's window picker: the configured presets plus a custom range, and a select
 * that reopens a recent custom run.
 *
 * - **Loading**: a "Loading windows" spinner until `GET /config` and `GET /runs/latest` have
 *   answered.
 * - **No configuration**: a "Window presets unavailable" warning with the API's message, and no
 *   picker, because the client has no presets of its own.
 * - **Selection shown**: the preset of `run.days` is pressed when it is a configured preset and
 *   `run` ends on the default window end; otherwise `run`'s dates show as the custom range.
 * - **Choosing**: the default preset navigates to `/actions` with no search params; any other
 *   preset or a valid custom range posts `POST /runs` and, once the run is created, navigates to
 *   `/actions?run=<id>`. While it computes, every control is disabled and a "Computing custom
 *   window" spinner shows beside the controls; afterwards focus returns to the control that
 *   started it.
 * - **Failure**: a warning banner states it ({@link createRunErrorMessage}) until the next
 *   choice clears it. It is a polite status message, never an alert.
 * - **Reopening**: when succeeded custom runs exist, "Reopen a custom window" lists them newest
 *   first; choosing one navigates to it by id. The select is hidden while the list loads, when
 *   it fails and when it is empty.
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
  const customRuns = useCustomRuns();
  const navigate = useNavigate();
  // A window the client itself cannot turn into a request (presetWindow's RangeError). The
  // picker validates its configuration before it renders a preset, so this stays null unless
  // the two validations ever disagree; it is still shown rather than thrown from a handler.
  const [inputError, setInputError] = useState<string | null>(null);
  const pending = createRun.isPending;

  // The control that started the computation: a preset button, Apply or a date field. Every
  // control is disabled while the run computes, and a disabled control loses focus to the page
  // body, so focus is returned to it once the controls are enabled again (WCAG 2.2 SC 2.4.3).
  // Focus the user has moved elsewhere in the meantime is left alone, and a control that has
  // left the page, after navigation to the created run for example, is skipped.
  const focusReturnRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (pending) {
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
  }, [pending]);

  const windowConfig = config.data?.window;
  if (windowConfig === undefined) {
    if (config.isPending) {
      return <Spinner label="Loading windows" />;
    }
    // Never fall back to presets of the client's own: without the configuration no window
    // length is known (AAP 0.5.5).
    return (
      <Banner tone="warning" title="Window presets unavailable">
        {config.error?.message ?? NO_WINDOW_CONFIG_MESSAGE}
      </Banner>
    );
  }
  if (defaultRun.isPending) {
    return <Spinner label="Loading windows" />;
  }

  // The default window end: the latest succeeded default run's last date. Before the first
  // default run, or when it cannot be read, the run on screen ends the presets instead.
  const defaultEnd: IsoDate = defaultRun.data?.window_end ?? run.window_end;

  const value: DateRangeValue =
    windowConfig.presets.includes(run.days) && run.window_end === defaultEnd
      ? { kind: 'preset', days: run.days }
      : { kind: 'custom', start: run.window_start, end: run.window_end };

  // A run that has not succeeded cannot be shown (its page answers 409 `run_not_ready`), so
  // only succeeded custom runs can be reopened. The API lists them newest first.
  const reopenable = (customRuns.data ?? []).filter((custom) => custom.status === 'succeeded');
  const reopenOptions: readonly SelectOption[] = [
    NO_CUSTOM_RUN,
    ...reopenable.map((custom) => ({ value: String(custom.id), label: customRunLabel(custom) })),
  ];
  const reopenValue = reopenable.some((custom) => custom.id === run.id)
    ? String(run.id)
    : NO_CUSTOM_RUN.value;

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

  /** Computes a custom-window run and opens it once created; `mutate` clears the last error. */
  const compute = (body: CreateRunRequest) => {
    setInputError(null);
    const active = document.activeElement;
    focusReturnRef.current =
      active instanceof HTMLElement && active !== document.body ? active : null;
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

  const reopen = (selected: string) => {
    if (selected === NO_CUSTOM_RUN.value) {
      return;
    }
    const found = reopenable.find((custom) => String(custom.id) === selected);
    if (found === undefined) {
      return;
    }
    clearErrors();
    openRun(found.id);
  };

  return (
    // Top-aligned, so the picker's and the select's labels share a line and opening the custom
    // range never moves the controls beside it. The spinner follows the controls, so showing it
    // shifts nothing.
    <Inline gap={4} align="start">
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
      />
      {reopenable.length > 0 && (
        <Select
          label="Reopen a custom window"
          options={reopenOptions}
          value={reopenValue}
          onChange={reopen}
          disabled={pending}
        />
      )}
      {pending && <Spinner label="Computing custom window" />}
      {/* Always rendered, so the live region exists before a message appears and the message
          is announced politely (WCAG 2.2 SC 4.1.3); role="alert" is reserved for discrepancies
          (AAP 0.5.5). Empty, it adds nothing visible to the row. */}
      <div role="status">
        {errorMessage !== null && <Banner tone="warning" title={errorMessage} />}
      </div>
    </Inline>
  );
}
