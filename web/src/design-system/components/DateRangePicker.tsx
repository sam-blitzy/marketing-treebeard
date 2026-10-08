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
 *   div.ds-daterange__presets[role=group]    aria-labelledby the label: one toggle Button per
 *                                            preset, in the given order, then "Custom range"
 *   div.ds-daterange__custom#{panelId}       [hidden] while collapsed; controlled by the
 *                                            "Custom range" toggle (aria-expanded/controls)
 *     form[novalidate]
 *       div.ds-daterange__custom-fields      Start date and End date TextFields, then Apply
 *       div.ds-visually-hidden[role=status]  always rendered; the latest failed Apply's
 *                                            submit-error summary (`./shared.css`)
 * ```
 *
 * With an invalid window configuration, `div.ds-daterange` holds only the label and a critical
 * `Banner` (no `alert`) listing each problem.
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
 *   a start date." on every failed Apply, a repeated identical one included. Edits never update
 *   it, so typing is not announced; a valid Apply or a new custom `value` clears it. The fields
 *   carry no live region of their own.
 * - Both fields carry `max={maxDate}`, so the native calendar offers no later date. The fields
 *   carry no hint text: a hint under one field only would lift that field above its neighbour
 *   in the bottom-aligned field row of `DateRangePicker.css`.
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
import type { FormEvent } from 'react';

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

/** The latest failed Apply, held until a valid Apply or a new custom `value` clears it. */
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
 * rendered, and a polite status region announces a summary. The picker never changes the
 * selection itself: the caller navigates to the chosen run and passes the window shown back
 * through `value`. With an invalid window configuration it renders only the label and a
 * configuration error naming each problem, and neither callback can be called.
 *
 * State held here is limited to the custom panel: whether it is open, the two date drafts,
 * whether Apply has been attempted (messages show only after the first Apply) and the latest
 * failed Apply (the field to focus and the summary to announce).
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

  // Messages appear only once Apply has been attempted, then follow every edit.
  const errors: RangeErrors =
    submitted && config.ok ? validateRange(start, end, maxDate, config.maxMs, maxDays) : {};

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

  // An unusable configuration renders the label and the problems as static text (not an alert:
  // AAP 0.5.5 reserves that role for discrepancies) and no control, so neither callback can run.
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
      </div>
    );
  }

  return (
    <div className="ds-daterange">
      <p className="ds-daterange__label" id={labelId}>
        {label}
      </p>
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
            <Button type="submit" variant="primary" size="sm" disabled={disabled}>
              Apply
            </Button>
          </div>
          {/* Always rendered, so the live region exists before its text changes. The key remounts
              the summary on every failed Apply, so an identical failure is announced again. */}
          <div className="ds-visually-hidden" role="status">
            {failure !== null && <span key={failure.attempt}>{failure.summary}</span>}
          </div>
        </form>
      </div>
    </div>
  );
}
