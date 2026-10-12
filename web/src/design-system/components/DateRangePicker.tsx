/**
 * `DateRangePicker`: the product's analysis-window picker (AAP 0.4.2, "Window picker", status
 * GAP).
 *
 * The status bar on `/actions` offers each configured window preset plus a custom range
 * (AAP 0.5.5). This component renders that control and nothing else: it has no window length
 * of its own. The presets (`window.presets`), the default marker (`window.default_days`), the
 * longest span (`window.max_days`) and the latest selectable date (the default window end) all
 * arrive as props from `features/status/WindowPicker.tsx`, which reads them from `GET /config`
 * and the latest run, so "no window length is hard-coded in the web client" (AAP 0.5.5).
 *
 * Structure, matching the class-name contract in `DateRangePicker.css`:
 *
 * ```text
 * div.ds-daterange
 *   p.ds-daterange__label#{labelId}          visible label naming the preset group
 *   div.ds-daterange__row                    the preset row
 *     div.ds-daterange__presets[role=group]  aria-labelledby the label: one toggle Button per
 *                                            preset, in the given order, then "Custom range"
 *     {actions}                              only when given: the caller's controls, after
 *                                            the group and never inside it
 *   div.ds-daterange__custom#{panelId}       [hidden] while collapsed; controlled by the
 *                                            "Custom range" toggle (aria-expanded/controls)
 *     form[novalidate]
 *       div.ds-daterange__custom-fields      Start date and End date TextFields, then
 *         div.ds-daterange__apply            the wrapper of the Apply Button
 *       div.ds-visually-hidden[role=status]  always rendered; the latest failed Apply's
 *                                            submit-error summary (`./shared.css`)
 *   div.ds-visually-hidden[role=status]      always rendered, last; the caller's
 *                                            `statusMessage` about the window shown
 * ```
 *
 * With an invalid window configuration, `div.ds-daterange` holds only the label, a critical
 * `Banner` (no `alert`) listing each problem and, when `actions` are given, a
 * `div.ds-daterange__row` holding them, so the caller's controls stay available. It renders no
 * status region.
 *
 * Interaction and accessibility (WCAG 2.2 AA, AAP 0.1.1):
 * - Presets are toggle buttons carrying `aria-pressed`, not radios. Choosing a preset navigates
 *   to another run, and a button press is an explicit activation, so changing the window never
 *   happens as a side effect of moving through a radio group (SC 3.2.2 On Input).
 * - Every control is a native `<button>` or `<input>`, so the picker is fully operable with
 *   Tab, Enter and Space (SC 2.1.1); Enter in a date field submits the custom range.
 * - The custom range is validated here and every problem is named in text on the field it
 *   concerns (SC 3.3.1): the field carries `aria-invalid` and references its message. Messages
 *   appear only after the first Apply and then update on every edit, so they clear as the user
 *   fixes them. `noValidate` keeps the browser's own bubbles out, so the messages are always
 *   this component's.
 * - A failed Apply moves focus to the first invalid field in an effect, after its message and
 *   `aria-describedby` have rendered, so the field is announced with its error. Focus does not
 *   move when that field already has it (Enter in the field), so the failure is also announced
 *   by a polite `role="status"` region (SC 4.1.3): it is rendered with the form, so it exists
 *   before its text changes, and receives a concise summary such as "1 error: Start date: Enter
 *   a start date." on every failed Apply, a repeated identical one included. It clears as soon
 *   as an edit changes what it reports (a field fixed or a message changed), and on a valid
 *   Apply or a new custom `value`, so it never reports an error the user has fixed. Clearing is
 *   not announced, and edits never add or change a summary, so typing is not announced: an edit
 *   that leaves the errors as they were keeps the summary, a field broken again shows its
 *   message, and the next Apply announces the errors that then stand. The fields carry no live
 *   region of their own.
 * - The caller's `statusMessage`, such as "Custom window 2026-09-20 – 2026-10-10 opened", is
 *   announced by a second polite `role="status"` region at picker level, outside the custom
 *   panel, so it is heard while the panel is collapsed too (SC 4.1.3). The region is committed
 *   empty, and a message lands in it shortly after (`STATUS_MESSAGE_DELAY_MS`), never in the
 *   task that committed the region, so the text always changes a region already in the
 *   accessibility tree. A newer message cancels one still pending and replaces the one shown;
 *   an absent or blank one empties the region at once. It is never an `alert`, which AAP 0.5.5
 *   reserves for discrepancies.
 * - `actions` sit on the preset row after the preset group, outside it, so the group still
 *   holds only the presets and "Custom range".
 * - Both fields carry `max={maxDate}`, so the native calendar offers no later date. The fields
 *   carry no hint text: `DateRangePicker.css` gives each field exactly three grid rows shared
 *   with its neighbours (label, control, message), so labels, inputs and Apply stay level and an
 *   error never moves them, and a hint would have no row of its own beside the error.
 * - The client-side checks mirror `POST /runs` (AAP 0.5.3): the span runs from 1 day to
 *   `maxDays` and the end is no later than `maxDate`. Both checks always apply. The API remains
 *   authoritative.
 * - The window configuration fails closed. Every preset and `maxDays` must be a whole number of
 *   days, 1 or more; `defaultPreset` must be one too and one of `presets` (AAP 0.5.4.1); and
 *   `maxDate` must be a real calendar date. Otherwise the picker shows its label and a static
 *   configuration error naming each problem, and renders no preset, toggle, field or Apply, so
 *   no window is ever selected against unusable bounds.
 *
 * Dates are ISO `YYYY-MM-DD` calendar dates, compared in UTC only: a calendar date has no time
 * zone, and parsing it as local time could move it by a day.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it composes the product `Button`, `TextField` and `Banner` wrappers
 * styled by semantic tokens, except for the literals that `../GAPS.md` lists, such as the status
 * region's visually hidden `1px` box and `inset(50%)` clip, and binding Blitzy OS later changes
 * only this file and `DateRangePicker.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './DateRangePicker.css';

import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';

import { hasContent } from './a11y';
import { Banner } from './Banner';
import { Button } from './Button';
import { TextField } from './TextField';

/**
 * The window a {@link DateRangePicker} shows as selected:
 *
 * - `{ kind: 'preset', days }`: the preset of `days` days ending on the default window end;
 * - `{ kind: 'custom', start, end }`: an explicit calendar-date range, both ends inclusive, as
 *   ISO `YYYY-MM-DD` strings.
 *
 * GAP: bind to Blitzy OS on source arrival. This is a product-level shape, not a Blitzy OS
 * type; on binding, it maps onto the Blitzy OS date-range value.
 */
export type DateRangeValue =
  | { kind: 'preset'; days: number }
  | { kind: 'custom'; start: string; end: string };

/**
 * Props of {@link DateRangePicker}.
 *
 * The prop set is closed on purpose: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4).
 *
 * `presets`, `defaultPreset`, `maxDays` and `maxDate` are the window configuration. When any of
 * them breaks its stated contract, the picker fails closed: it shows its label and a
 * configuration error naming each problem, and renders no control that could select a window.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS date-range picker.
 */
export interface DateRangePickerProps {
  /**
   * Visible label of the picker, for example "Analysis window". It names the preset group
   * (`aria-labelledby`), so it must describe the choice on its own.
   */
  label: string;
  /**
   * The preset window lengths in days, in display order: `window.presets` from `GET /config`.
   * Each must be a whole number of days, 1 or more. One toggle button is rendered per distinct
   * value; a repeated value is shown once.
   */
  presets: readonly number[];
  /**
   * The preset marked "(default)": `window.default_days` from `GET /config`. It must be a whole
   * number of days, 1 or more, and one of `presets` (AAP 0.5.4.1).
   */
  defaultPreset: number;
  /**
   * The window currently shown. A matching preset is pressed; a custom value presses no preset,
   * opens the custom panel and prefills its fields. The picker follows later changes: when
   * `value` becomes a different custom range, the panel opens with that range.
   */
  value: DateRangeValue;
  /** Called with a preset's length in days when its button is activated. */
  onSelectPreset: (days: number) => void;
  /**
   * Called once per valid Apply with the custom range's inclusive start and end dates
   * (`YYYY-MM-DD`). It is never called while any validation message stands.
   */
  onSelectCustom: (start: string, end: string) => void;
  /**
   * The latest selectable date (`YYYY-MM-DD`): the default window end. It must be a real
   * calendar date. It is the date inputs' `max`, and a custom end after it is rejected.
   */
  maxDate: string;
  /**
   * The longest custom span in days, both ends inclusive: `window.max_days` from `GET /config`.
   * It must be a whole number of days, 1 or more. A longer range is rejected.
   */
  maxDays: number;
  /**
   * Disables every preset, the "Custom range" toggle, both date fields and Apply, for example
   * while a custom run is being computed. Defaults to `false`.
   */
  disabled?: boolean;
  /**
   * Extra controls of the caller, for example a "Recent custom windows" disclosure, placed on
   * the preset row after the preset group and never inside it, so the group holds only the
   * presets and "Custom range". They are kept with an invalid window configuration too, on a
   * row after the configuration error. Nothing that renders no content (`undefined`, `null`, a
   * boolean or `''`) adds an element. `disabled` does not reach them: the caller disables its
   * own controls.
   */
  actions?: ReactNode;
  /**
   * A polite status message about the window shown, for example "Custom window 2026-09-20 –
   * 2026-10-10 opened". It is announced through the picker's visually hidden `role="status"`
   * region, outside the custom panel, so it is heard whether or not the panel is open. The text
   * is trimmed and lands in the region shortly after the region is committed, never in the same
   * task, so the region is in the accessibility tree before its text changes. A different
   * message replaces the one before, and one still pending when a newer one arrives is never
   * shown; an absent or blank message announces nothing and empties the region at once. It is
   * never an alert.
   */
  statusMessage?: string;
}

/** Validation messages of the custom range, per field; an absent key means the field is valid. */
interface RangeErrors {
  start?: string;
  end?: string;
}

/** The visible labels of the date fields, which also name them in the submit-error summary. */
const FIELD_LABELS: Readonly<Record<keyof RangeErrors, string>> = {
  start: 'Start date',
  end: 'End date',
};

/**
 * The latest failed Apply, held until an edit changes what its summary reports (a field fixed
 * or a message changed), a valid Apply, or a new custom `value` clears it.
 */
interface FailedApply {
  /** The first invalid field, which receives focus once its message has rendered. */
  field: keyof RangeErrors;
  /**
   * Counts failed Applies. A new count is a new focus request and remounts the summary, so an
   * identical failure still moves focus and is announced again.
   */
  attempt: number;
  /** The submit-error summary the status region announces. */
  summary: string;
}

/** Milliseconds in one UTC calendar day: a unit conversion, not a window length. */
const MS_PER_DAY = 86_400_000;

/**
 * How long, in milliseconds, the picker waits before a non-empty `statusMessage` enters its
 * picker-level status region.
 *
 * A live region must already be in the accessibility tree when its text changes, or screen
 * readers may skip the announcement. React can flush the effect that adopts the message in the
 * same task as the commit that inserted the region, with no rendering step between them, so the
 * region would reach the accessibility tree already filled; that same-task fill was observed in
 * a production build after a run opened. The delay puts the text in a later task, at least one
 * rendered frame after the region. It is a timing literal, not a design token, and short enough
 * that the announcement still follows the window change at once.
 */
const STATUS_MESSAGE_DELAY_MS = 100;

/** An ISO calendar date, `YYYY-MM-DD`, capturing year, month and day. */
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * Parses an ISO `YYYY-MM-DD` calendar date to the UTC midnight of that date in epoch
 * milliseconds, or `null` when the text is not a real calendar date.
 *
 * The date is set on a UTC epoch with `setUTCFullYear`, never parsed as local time, so no time
 * zone can shift the day. `Date.UTC` is not used: it remaps years 0 to 99 onto 1900 to 1999,
 * which would reject the valid years `0001` to `0099`. Year `0000` is not a calendar year of
 * the ISO date contract and is rejected. The result is read back and compared with the input,
 * which rejects dates that would otherwise roll over, such as `2026-02-30`.
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

/** "1 day", "7 days": a day count with its noun. */
function formatDays(days: number): string {
  return `${days} ${days === 1 ? 'day' : 'days'}`;
}

/**
 * The outcome of {@link checkConfig}: either the accepted configuration, with the distinct
 * presets in display order and `maxDate` as UTC epoch milliseconds, or the problems that
 * rejected it, one sentence each.
 */
type ConfigCheck =
  | { ok: true; presetDays: readonly number[]; maxMs: number }
  | { ok: false; problems: readonly string[] };

/**
 * Whether `value` is a usable day count: a whole number, 1 or more, that a `number` holds
 * exactly. `NaN`, infinities, fractions, zero and negatives all fail.
 */
function isDayCount(value: number): boolean {
  return Number.isSafeInteger(value) && value >= 1;
}

/**
 * Checks the window configuration the picker receives before any control is rendered, so the
 * picker fails closed: a configuration that breaks its contract never yields a preset button
 * or a custom range checked against unusable bounds.
 *
 * Every preset and `maxDays` must be a day count ({@link isDayCount}). `defaultPreset` must be
 * a day count and one of `presets`, because `window.presets` must contain
 * `window.default_days` (AAP 0.5.4.1). `maxDate` must be a real ISO calendar date
 * ({@link parseIsoDate}). Every failing check contributes its own sentence.
 */
function checkConfig(
  presets: readonly number[],
  defaultPreset: number,
  maxDate: string,
  maxDays: number,
): ConfigCheck {
  const problems: string[] = [];

  const invalidPresets = presets.filter((days) => !isDayCount(days)).join(', ');
  if (invalidPresets !== '') {
    problems.push(
      `Every preset must be a whole number of days, 1 or more; received ${invalidPresets}.`,
    );
  }
  if (!isDayCount(defaultPreset)) {
    problems.push(
      `The default window must be a whole number of days, 1 or more; received ${defaultPreset}.`,
    );
  } else if (!presets.includes(defaultPreset)) {
    problems.push(`The default window of ${formatDays(defaultPreset)} is not one of the presets.`);
  }
  if (!isDayCount(maxDays)) {
    problems.push(
      `The longest custom range must be a whole number of days, 1 or more; received ${maxDays}.`,
    );
  }
  const maxMs = parseIsoDate(maxDate);
  if (maxMs === null) {
    problems.push(
      `The latest selectable date must be a calendar date (YYYY-MM-DD); received "${maxDate}".`,
    );
  }

  if (maxMs === null || problems.length > 0) {
    return { ok: false, problems };
  }
  // One button per distinct preset, in the given order, so React keys stay unique even if the
  // configuration repeats a value.
  const presetDays = presets.filter((days, index) => presets.indexOf(days) === index);
  return { ok: true, presetDays, maxMs };
}

/**
 * Validates a custom range against the `POST /runs` window rules (AAP 0.5.3) and returns one
 * message per invalid field.
 *
 * Start: required, a real date, and on or before the end. End: required, a real date, on or
 * before `maxDate`, and within `maxDays` of the start, both ends inclusive. Each field reports
 * its first failing check only. The limits come from a configuration {@link checkConfig}
 * accepted (`maxMs` is `maxDate` parsed, `maxDays` a day count), so both limit checks always
 * apply; `maxDate` itself is used only to word the message.
 */
function validateRange(
  start: string,
  end: string,
  maxDate: string,
  maxMs: number,
  maxDays: number,
): RangeErrors {
  const errors: RangeErrors = {};
  const startMs = start === '' ? null : parseIsoDate(start);
  const endMs = end === '' ? null : parseIsoDate(end);

  if (start === '') {
    errors.start = 'Enter a start date';
  } else if (startMs === null) {
    errors.start = 'Enter a valid start date';
  } else if (endMs !== null && startMs > endMs) {
    errors.start = 'Start date must be on or before the end date';
  }

  if (end === '') {
    errors.end = 'Enter an end date';
  } else if (endMs === null) {
    errors.end = 'Enter a valid end date';
  } else if (endMs > maxMs) {
    errors.end = `End date must be on or before ${maxDate}`;
  } else if (
    startMs !== null &&
    startMs <= endMs &&
    (endMs - startMs) / MS_PER_DAY + 1 > maxDays
  ) {
    errors.end = `Range must be at most ${formatDays(maxDays)}`;
  }

  return errors;
}

/**
 * The concise submit-error summary of a failed Apply, built from the field labels and their
 * messages in field order, for example "2 errors: Start date: Enter a start date. End date:
 * Enter an end date."
 */
function summarizeErrors(errors: RangeErrors): string {
  const parts: string[] = [];
  if (errors.start !== undefined) {
    parts.push(`${FIELD_LABELS.start}: ${errors.start}.`);
  }
  if (errors.end !== undefined) {
    parts.push(`${FIELD_LABELS.end}: ${errors.end}.`);
  }
  return `${parts.length} ${parts.length === 1 ? 'error' : 'errors'}: ${parts.join(' ')}`;
}

/**
 * A stable identity for a {@link DateRangeValue}, used to notice when the `value` prop changes
 * to a different window between renders.
 */
function valueKey(value: DateRangeValue): string {
  return value.kind === 'preset'
    ? `preset:${value.days}`
    : `custom:${value.start}:${value.end}`;
}

/**
 * Analysis-window picker: one toggle button per configured preset, the default one marked
 * "(default)", and a "Custom range" disclosure with start and end date fields and Apply.
 *
 * Activating a preset calls `onSelectPreset(days)`. Apply (or Enter in a date field) validates
 * the range and calls `onSelectCustom(start, end)` only when it is valid; otherwise each
 * invalid field shows its message, focus moves to the first of them once the messages have
 * rendered, and a polite status region announces a summary, which clears as soon as an edit
 * changes what it reports (a field fixed or a message changed), and on a valid Apply or a new
 * custom `value`. The picker never changes the selection itself: the caller navigates to the
 * chosen run and passes the window shown back through `value`. The caller's `actions` follow
 * the preset group on its row, and its `statusMessage` is announced by a picker-level polite
 * status region. With an invalid window configuration it renders only the label, a
 * configuration error naming each problem and the caller's `actions`, and neither callback can
 * be called.
 *
 * State held here is limited to the custom panel and the announced status message: whether the
 * panel is open, the two date drafts, whether Apply has been attempted (messages show only after
 * the first Apply), the latest failed Apply (the field to focus and the summary to announce) and
 * the status message the picker-level region holds.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <DateRangePicker
 *   label="Analysis window"
 *   presets={config.window.presets}
 *   defaultPreset={config.window.default_days}
 *   value={value}
 *   onSelectPreset={choosePreset}
 *   onSelectCustom={(start, end) => createRun.mutate({ window_start: start, window_end: end })}
 *   maxDate={defaultRun.window_end}
 *   maxDays={config.window.max_days}
 *   disabled={createRun.isPending}
 *   actions={recentCustomWindowsToggle}
 *   statusMessage={openedMessage}
 * />
 * ```
 */
export function DateRangePicker({
  label,
  presets,
  defaultPreset,
  value,
  onSelectPreset,
  onSelectCustom,
  maxDate,
  maxDays,
  disabled = false,
  actions,
  statusMessage,
}: DateRangePickerProps) {
  const id = useId();
  const labelId = `${id}-label`;
  const panelId = `${id}-custom`;

  const [customOpen, setCustomOpen] = useState(value.kind === 'custom');
  const [start, setStart] = useState(value.kind === 'custom' ? value.start : '');
  const [end, setEnd] = useState(value.kind === 'custom' ? value.end : '');
  const [submitted, setSubmitted] = useState(false);
  const [syncedKey, setSyncedKey] = useState(() => valueKey(value));
  const [failure, setFailure] = useState<FailedApply | null>(null);
  const [announcement, setAnnouncement] = useState('');

  const startRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLInputElement>(null);

  // Follow a change of `value` to a different window, for example after the caller navigates
  // to a reopened custom run: a new custom range opens the panel with that range and clears
  // earlier messages and the submit summary. State is adjusted during render (React's pattern
  // for deriving state from a changed prop), so the new range shows in the same render, with no
  // effect and no flash. A change to a preset leaves the panel and its drafts as the user left
  // them.
  const currentKey = valueKey(value);
  if (currentKey !== syncedKey) {
    setSyncedKey(currentKey);
    if (value.kind === 'custom') {
      setCustomOpen(true);
      setStart(value.start);
      setEnd(value.end);
      setSubmitted(false);
      setFailure(null);
    }
  }

  // Focus the first invalid field after the failed Apply has committed, so its message and
  // aria-describedby are already in the DOM when it is focused and announced. Each failed Apply
  // is a new object, so a repeated identical failure focuses again.
  useEffect(() => {
    if (failure === null) {
      return;
    }
    const field = failure.field === 'start' ? startRef.current : endRef.current;
    field?.focus();
  }, [failure]);

  // The window configuration is checked on every render, so the picker fails closed as soon as
  // a prop breaks its contract and recovers as soon as it is fixed.
  const config = checkConfig(presets, defaultPreset, maxDate, maxDays);

  // Adopt the caller's status message STATUS_MESSAGE_DELAY_MS after the commit, never in the
  // render or the task that commits the picker-level status region: the region is committed
  // empty first and a message, the first one included, lands shortly after in a later task, so
  // it always changes the text of a live region already in the accessibility tree and is
  // announced (WCAG 2.2 SC 4.1.3). A newer message, or unmounting, cancels a pending one, so
  // only the latest is announced; a different message replaces the one shown. An absent or
  // blank message empties the region at once: removing text is not announced, so it needs no
  // delay. While the configuration is invalid no region is rendered and its text is cleared, so
  // the region rendered again once the configuration is fixed is committed empty too.
  useEffect(() => {
    const message = config.ok ? (statusMessage?.trim() ?? '') : '';
    if (message === '') {
      setAnnouncement('');
      return;
    }
    const timer = window.setTimeout(() => {
      setAnnouncement(message);
    }, STATUS_MESSAGE_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [statusMessage, config.ok]);

  // Messages appear only once Apply has been attempted, then follow every edit.
  const errors: RangeErrors =
    submitted && config.ok ? validateRange(start, end, maxDate, config.maxMs, maxDays) : {};

  // Clear the submit summary as soon as an edit changes what it reports (a field fixed or a
  // message changed), so the status region never keeps reporting errors the user has fixed.
  // Right after a failed Apply the summary equals the summary of the same drafts, so it stays.
  // State is adjusted during render, as for `value` above, so the summary goes in the same
  // render as the message that changed. Clearing removes text, which is not announced, and edits
  // never add or change a summary, so typing is never announced: the next Apply announces the
  // errors that then stand.
  if (
    failure !== null &&
    submitted &&
    ((errors.start === undefined && errors.end === undefined) ||
      summarizeErrors(errors) !== failure.summary)
  ) {
    setFailure(null);
  }

  const toggleCustom = () => {
    setCustomOpen((open) => !open);
  };

  const apply = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (disabled || !config.ok) {
      return;
    }
    setSubmitted(true);
    const result = validateRange(start, end, maxDate, config.maxMs, maxDays);
    if (result.start !== undefined || result.end !== undefined) {
      // Focus and the announcement wait for the commit: see the effect above.
      const field = result.start !== undefined ? 'start' : 'end';
      const summary = summarizeErrors(result);
      setFailure((previous) => ({ field, attempt: (previous?.attempt ?? 0) + 1, summary }));
      return;
    }
    // A valid range: later edits show no message until the next Apply.
    setSubmitted(false);
    setFailure(null);
    onSelectCustom(start, end);
  };

  // The caller's controls add an element only when they render content (see hasContent).
  const showActions = hasContent(actions);

  // An unusable configuration renders the label and the problems as static text (not an alert:
  // AAP 0.5.5 reserves that role for discrepancies) and no window control, so neither callback
  // can run. The caller's own controls stay on a row after the problems, and no status region is
  // rendered: there is no window shown to announce.
  if (!config.ok) {
    return (
      <div className="ds-daterange">
        <p className="ds-daterange__label" id={labelId}>
          {label}
        </p>
        <Banner tone="critical" title="No window can be chosen: the window settings are invalid">
          <ul>
            {config.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </Banner>
        {showActions && <div className="ds-daterange__row">{actions}</div>}
      </div>
    );
  }

  return (
    <div className="ds-daterange">
      <p className="ds-daterange__label" id={labelId}>
        {label}
      </p>
      {/* The preset group, then the caller's controls beside it and outside it, so the group
          holds only the presets and "Custom range". */}
      <div className="ds-daterange__row">
        <div className="ds-daterange__presets" role="group" aria-labelledby={labelId}>
          {config.presetDays.map((days) => (
            <Button
              key={days}
              size="sm"
              aria-pressed={value.kind === 'preset' && value.days === days}
              disabled={disabled}
              onClick={() => {
                onSelectPreset(days);
              }}
            >
              {days === defaultPreset ? `${formatDays(days)} (default)` : formatDays(days)}
            </Button>
          ))}
          <Button
            size="sm"
            variant="quiet"
            aria-expanded={customOpen}
            aria-controls={panelId}
            disabled={disabled}
            onClick={toggleCustom}
          >
            Custom range
          </Button>
        </div>
        {showActions && actions}
      </div>
      <div className="ds-daterange__custom" id={panelId} hidden={!customOpen}>
        <form noValidate onSubmit={apply}>
          <div className="ds-daterange__custom-fields">
            <TextField
              ref={startRef}
              type="date"
              label={FIELD_LABELS.start}
              value={start}
              onChange={setStart}
              max={maxDate}
              required
              disabled={disabled}
              error={errors.start}
            />
            <TextField
              ref={endRef}
              type="date"
              label={FIELD_LABELS.end}
              value={end}
              onChange={setEnd}
              max={maxDate}
              required
              disabled={disabled}
              error={errors.end}
            />
            {/* The wrapper takes the fields' label, control and message rows (see the CSS), so
                Apply sits in the control row at the inputs' height; md matches their text. */}
            <div className="ds-daterange__apply">
              <Button type="submit" variant="primary" size="md" disabled={disabled}>
                Apply
              </Button>
            </div>
          </div>
          {/* Always rendered, so the live region exists before its text changes. The key remounts
              the summary on every failed Apply, so an identical failure is announced again. */}
          <div className="ds-visually-hidden" role="status">
            {failure !== null && <span key={failure.attempt}>{failure.summary}</span>}
          </div>
        </form>
      </div>
      {/* The caller's status message, at picker level and last, so it is announced while the
          custom panel is collapsed too. Always rendered, committed empty and filled in a later
          task (see the effect above), so the region exists before its text changes. */}
      <div className="ds-visually-hidden" role="status">
        {announcement}
      </div>
    </div>
  );
}
