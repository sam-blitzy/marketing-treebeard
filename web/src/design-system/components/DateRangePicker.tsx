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
 * ```
 *
 * Interaction and accessibility (WCAG 2.2 AA, AAP 0.1.1):
 * - Presets are toggle buttons carrying `aria-pressed`, not radios. Choosing a preset navigates
 *   to another run, and a button press is an explicit activation, so changing the window never
 *   happens as a side effect of moving through a radio group (SC 3.2.2 On Input).
 * - Every control is a native `<button>` or `<input>`, so the picker is fully operable with
 *   Tab, Enter and Space (SC 2.1.1); Enter in a date field submits the custom range.
 * - The custom range is validated here and every problem is named in text on the field it
 *   concerns (SC 3.3.1): the field carries `aria-invalid` and references its message, and focus
 *   moves to the first invalid field. Messages appear only after the first Apply and then
 *   update on every edit, so they clear as the user fixes them. `noValidate` keeps the
 *   browser's own bubbles out, so the messages are always this component's.
 * - Both fields carry `max={maxDate}`, so the native calendar offers no later date. The fields
 *   carry no hint text: a hint under one field only would lift that field above its neighbour
 *   in the bottom-aligned field row of `DateRangePicker.css`.
 * - The client-side checks mirror `POST /runs` (AAP 0.5.3): the span runs from 1 day to
 *   `maxDays` and the end is no later than `maxDate`. The API remains authoritative.
 *
 * Dates are ISO `YYYY-MM-DD` calendar dates, compared in UTC only: a calendar date has no time
 * zone, and parsing it as local time could move it by a day.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it composes the product `Button` and `TextField` wrappers styled
 * solely by semantic tokens, and binding Blitzy OS later changes only this file and
 * `DateRangePicker.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './DateRangePicker.css';

import { useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';

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
   * One toggle button is rendered per distinct value; a repeated value is shown once.
   */
  presets: readonly number[];
  /** The preset marked "(default)": `window.default_days` from `GET /config`. */
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
   * The latest selectable date (`YYYY-MM-DD`): the default window end. It is the date inputs'
   * `max`, and a custom end after it is rejected.
   */
  maxDate: string;
  /**
   * The longest custom span in days, both ends inclusive: `window.max_days` from `GET /config`.
   * A longer range is rejected.
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

/** Milliseconds in one UTC calendar day: a unit conversion, not a window length. */
const MS_PER_DAY = 86_400_000;

/** An ISO calendar date, `YYYY-MM-DD`, capturing year, month and day. */
const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * Parses an ISO `YYYY-MM-DD` calendar date to the UTC midnight of that date in epoch
 * milliseconds, or `null` when the text is not a real calendar date.
 *
 * Only `Date.UTC` is used, never local-time parsing, so no time zone can shift the day. The
 * result is read back and compared with the input, which rejects dates that `Date.UTC` would
 * otherwise roll over or remap, such as `2026-02-30` or a two-digit year.
 */
function parseIsoDate(text: string): number | null {
  const match = ISO_DATE.exec(text);
  if (match === null) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const ms = Date.UTC(year, month - 1, day);
  const parsed = new Date(ms);
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
 * Validates a custom range against the `POST /runs` window rules (AAP 0.5.3) and returns one
 * message per invalid field.
 *
 * Start: required, a real date, and on or before the end. End: required, a real date, on or
 * before `maxDate`, and within `maxDays` of the start, both ends inclusive. Each field reports
 * its first failing check only. A `maxDate` that is not a date, or a `maxDays` that is not a
 * finite number, skips its own check: the API still enforces it.
 */
function validateRange(start: string, end: string, maxDate: string, maxDays: number): RangeErrors {
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

  const maxMs = parseIsoDate(maxDate);
  if (end === '') {
    errors.end = 'Enter an end date';
  } else if (endMs === null) {
    errors.end = 'Enter a valid end date';
  } else if (maxMs !== null && endMs > maxMs) {
    errors.end = `End date must be on or before ${maxDate}`;
  } else if (
    startMs !== null &&
    startMs <= endMs &&
    Number.isFinite(maxDays) &&
    (endMs - startMs) / MS_PER_DAY + 1 > maxDays
  ) {
    errors.end = `Range must be at most ${formatDays(maxDays)}`;
  }

  return errors;
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
 * invalid field shows its message and focus moves to the first of them. The picker never
 * changes the selection itself: the caller navigates to the chosen run and passes the window
 * shown back through `value`.
 *
 * State held here is limited to the custom panel: whether it is open, the two date drafts and
 * whether Apply has been attempted (messages show only after the first Apply).
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

  const startRef = useRef<HTMLInputElement>(null);
  const endRef = useRef<HTMLInputElement>(null);

  // Follow a change of `value` to a different window, for example after the caller navigates
  // to a reopened custom run: a new custom range opens the panel with that range and clears
  // earlier messages. State is adjusted during render (React's pattern for deriving state from
  // a changed prop), so the new range shows in the same render, with no effect and no flash.
  // A change to a preset leaves the panel and its drafts as the user left them.
  const currentKey = valueKey(value);
  if (currentKey !== syncedKey) {
    setSyncedKey(currentKey);
    if (value.kind === 'custom') {
      setCustomOpen(true);
      setStart(value.start);
      setEnd(value.end);
      setSubmitted(false);
    }
  }

  // Messages appear only once Apply has been attempted, then follow every edit.
  const errors: RangeErrors = submitted ? validateRange(start, end, maxDate, maxDays) : {};

  // One button per distinct preset, in the given order, so React keys stay unique even if the
  // configuration repeats a value.
  const presetDays = presets.filter((days, index) => presets.indexOf(days) === index);

  const toggleCustom = () => {
    setCustomOpen((open) => !open);
  };

  const apply = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (disabled) {
      return;
    }
    setSubmitted(true);
    const result = validateRange(start, end, maxDate, maxDays);
    if (result.start !== undefined) {
      startRef.current?.focus();
      return;
    }
    if (result.end !== undefined) {
      endRef.current?.focus();
      return;
    }
    // A valid range: later edits show no message until the next Apply.
    setSubmitted(false);
    onSelectCustom(start, end);
  };

  return (
    <div className="ds-daterange">
      <p className="ds-daterange__label" id={labelId}>
        {label}
      </p>
      <div className="ds-daterange__presets" role="group" aria-labelledby={labelId}>
        {presetDays.map((days) => (
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
              label="Start date"
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
              label="End date"
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
        </form>
      </div>
    </div>
  );
}
