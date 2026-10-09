/**
 * Contract tests for `DateRangePicker`, the design-system analysis-window picker (AAP 0.4.2
 * "Window picker", status GAP; `../GAPS.md`, binding procedure step 6).
 *
 * They pin what the status bar (AAP 0.5.5) and assistive technology rely on, so a later
 * Blitzy OS binding that changes the wrapper's internals must still pass them:
 * - a visible label naming a `group` of preset toggle buttons (`aria-pressed`), one per distinct
 *   configured preset in order, the default one marked "(default)", and no window length of the
 *   picker's own;
 * - a "Custom range" disclosure (`aria-expanded`, `aria-controls`) over a form with required
 *   Start date and End date fields bounded by `maxDate`, and Apply;
 * - custom-range validation mirroring `POST /runs`: required fields, real calendar dates (an
 *   impossible date such as 2026-02-30 is rejected), start on or before end, end on or before
 *   `maxDate`, and at most `maxDays` days, both ends inclusive. Messages appear on the field
 *   (`aria-invalid`, `aria-describedby`) only after the first Apply and follow later edits;
 *   focus moves to the first invalid field and a polite `status` region announces a summary;
 * - the caller owns the selection: callbacks fire, the picker follows `value`;
 * - `disabled` disables every control, and an invalid window configuration fails closed with a
 *   static error naming each problem and no control.
 *
 * jsdom, like a browser, sanitizes the value of an `<input type="date">`, so a malformed or
 * impossible date cannot be typed into a field. Such dates reach the picker's drafts the way a
 * reopened custom run does: through a custom `value`.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { DateRangePicker, type DateRangePickerProps, type DateRangeValue } from './DateRangePicker';

/** `window.presets` as `GET /config` serves it by default. */
const PRESETS: readonly number[] = [14, 28, 56, 90];

/** The default window end: the latest selectable date. */
const MAX_DATE = '2026-10-10';

/** `window.max_days` for these tests. */
const MAX_DAYS = 90;

/** The default run's window. */
const DEFAULT_VALUE: DateRangeValue = { kind: 'preset', days: 28 };

/** The props of a picker under test, with mock callbacks the test can inspect. */
function pickerProps(overrides: Partial<DateRangePickerProps> = {}) {
  const onSelectPreset = vi.fn<(days: number) => void>();
  const onSelectCustom = vi.fn<(start: string, end: string) => void>();
  const props: DateRangePickerProps = {
    label: 'Analysis window',
    presets: PRESETS,
    defaultPreset: 28,
    value: DEFAULT_VALUE,
    onSelectPreset,
    onSelectCustom,
    maxDate: MAX_DATE,
    maxDays: MAX_DAYS,
    ...overrides,
  };
  return { props, onSelectPreset, onSelectCustom };
}

/** Renders a picker and returns its callbacks and a rerender that keeps the other props. */
function renderPicker(overrides: Partial<DateRangePickerProps> = {}) {
  const { props, onSelectPreset, onSelectCustom } = pickerProps(overrides);
  const view = render(<DateRangePicker {...props} />);
  const rerenderWith = (next: Partial<DateRangePickerProps>): void => {
    view.rerender(<DateRangePicker {...props} {...next} />);
  };
  return { ...view, onSelectPreset, onSelectCustom, rerenderWith };
}

/** The Start date field. Date inputs have no ARIA role, and the required asterisk follows the label. */
function startField(): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(/^Start date/);
}

/** The End date field. */
function endField(): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(/^End date/);
}

/** The "Custom range" disclosure toggle. */
function customToggle(): HTMLElement {
  return screen.getByRole('button', { name: 'Custom range' });
}

/** The polite status region of the custom form, visible or not. */
function statusRegion(): HTMLElement {
  return screen.getByRole('status', { hidden: true });
}

/** Presses Apply. */
function apply(): void {
  fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
}

/** The message a field's `aria-describedby` points at, or `null` when it has none. */
function messageOf(field: HTMLElement): string | null {
  const ids = field.getAttribute('aria-describedby');
  if (ids === null) {
    return null;
  }
  return ids
    .split(' ')
    .map((id) => document.getElementById(id)?.textContent ?? '')
    .join(' ');
}

/** The `aria-pressed` value of every preset button, in display order. */
function pressedStates(): (string | null)[] {
  const group = screen.getByRole('group', { name: 'Analysis window' });
  return within(group)
    .getAllByRole('button')
    .filter((button) => button.textContent !== 'Custom range')
    .map((button) => button.getAttribute('aria-pressed'));
}

describe('DateRangePicker', () => {
  describe('label and presets', () => {
    it('names a group of preset toggles, the default marked, followed by Custom range', () => {
      renderPicker();

      const label = screen.getByText('Analysis window');
      expect(label.tagName).toBe('P');
      const group = screen.getByRole('group', { name: 'Analysis window' });
      expect(group.getAttribute('aria-labelledby')).toBe(label.id);
      expect(within(group).getAllByRole('button').map((button) => button.textContent)).toEqual([
        '14 days',
        '28 days (default)',
        '56 days',
        '90 days',
        'Custom range',
      ]);
    });

    it('presses only the preset matching value', () => {
      renderPicker({ value: { kind: 'preset', days: 56 } });

      expect(pressedStates()).toEqual(['false', 'false', 'true', 'false']);
      expect(customToggle().hasAttribute('aria-pressed')).toBe(false);
    });

    it('reports an activated preset and leaves the selection to the caller', () => {
      const { onSelectPreset } = renderPicker();

      fireEvent.click(screen.getByRole('button', { name: '90 days' }));

      expect(onSelectPreset).toHaveBeenCalledTimes(1);
      expect(onSelectPreset).toHaveBeenCalledWith(90);
      expect(pressedStates()).toEqual(['false', 'true', 'false', 'false']);
    });

    it('reports the default preset by its length too', () => {
      const { onSelectPreset } = renderPicker({ value: { kind: 'preset', days: 14 } });

      fireEvent.click(screen.getByRole('button', { name: '28 days (default)' }));

      expect(onSelectPreset).toHaveBeenCalledWith(28);
    });

    it('offers exactly the configured lengths, each distinct length once, with a singular day', () => {
      renderPicker({ presets: [1, 21, 21, 45], defaultPreset: 21, value: { kind: 'preset', days: 21 } });

      const group = screen.getByRole('group', { name: 'Analysis window' });
      expect(within(group).getAllByRole('button').map((button) => button.textContent)).toEqual([
        '1 day',
        '21 days (default)',
        '45 days',
        'Custom range',
      ]);
      expect(pressedStates()).toEqual(['false', 'true', 'false']);
    });

    it('presses no preset for a custom value', () => {
      renderPicker({ value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' } });

      expect(pressedStates()).toEqual(['false', 'false', 'false', 'false']);
    });
  });

  describe('custom range disclosure', () => {
    it('starts collapsed and opens and closes its panel from the toggle', () => {
      renderPicker();
      const toggle = customToggle();
      const panel = document.getElementById(toggle.getAttribute('aria-controls') ?? '');

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(panel?.hidden).toBe(true);
      expect(screen.queryByRole('button', { name: 'Apply' })).toBeNull();

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(panel?.hidden).toBe(false);
      expect(panel?.contains(startField())).toBe(true);
      expect(screen.getByRole('button', { name: 'Apply' }).getAttribute('type')).toBe('submit');

      fireEvent.click(toggle);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(panel?.hidden).toBe(true);
    });

    it('renders required date fields bounded by maxDate, empty for a preset value', () => {
      renderPicker();
      fireEvent.click(customToggle());

      for (const field of [startField(), endField()]) {
        expect(field.type).toBe('date');
        expect(field.required).toBe(true);
        expect(field.max).toBe(MAX_DATE);
        expect(field.value).toBe('');
        expect(field.hasAttribute('aria-invalid')).toBe(false);
      }
      expect(startField().form?.noValidate).toBe(true);
      expect(statusRegion().textContent).toBe('');
    });

    it('opens with the range of a custom value prefilled', () => {
      renderPicker({ value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' } });

      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
      expect(startField().value).toBe('2026-09-01');
      expect(endField().value).toBe('2026-09-30');
    });
  });

  describe('applying a custom range', () => {
    it('reports a valid typed range once and shows no message', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());

      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      fireEvent.change(endField(), { target: { value: '2026-09-30' } });
      apply();

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-09-01', '2026-09-30');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('');
    });

    it.each([
      ['a one-day range ending on maxDate', '2026-10-10', '2026-10-10'],
      ['a range of exactly maxDays days', '2026-07-13', '2026-10-10'],
      ['a year 0001 range', '0001-01-01', '0001-01-31'],
    ])('accepts %s', (_case, start, end) => {
      const { onSelectCustom } = renderPicker({ value: { kind: 'custom', start, end } });

      apply();

      expect(onSelectCustom).toHaveBeenCalledWith(start, end);
    });

    it('shows no message before the first Apply, however invalid the drafts', () => {
      renderPicker();
      fireEvent.click(customToggle());

      fireEvent.change(startField(), { target: { value: '2026-09-30' } });
      fireEvent.change(endField(), { target: { value: '2026-09-01' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(startField())).toBeNull();
      expect(messageOf(endField())).toBeNull();
      expect(statusRegion().textContent).toBe('');
    });

    it('names both empty fields, focuses the first and announces a summary', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());

      apply();

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(startField().getAttribute('aria-invalid')).toBe('true');
      expect(endField().getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(startField())).toBe('Enter a start date');
      expect(messageOf(endField())).toBe('Enter an end date');
      expect(document.activeElement).toBe(startField());
      expect(statusRegion().textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );
    });

    it.each([
      ['an impossible start date', '2026-02-30', '2026-03-05', 'start', 'Enter a valid start date'],
      ['an impossible end date', '2026-02-01', '2026-02-30', 'end', 'Enter a valid end date'],
      ['a malformed start date', '2026-9-1', '2026-09-30', 'start', 'Enter a valid start date'],
      ['a year 0000 start date', '0000-01-01', '2026-09-30', 'start', 'Enter a valid start date'],
      ['an empty start date', '', '2026-09-30', 'start', 'Enter a start date'],
      ['an empty end date', '2026-09-01', '', 'end', 'Enter an end date'],
      [
        'a start after the end',
        '2026-09-10',
        '2026-09-01',
        'start',
        'Start date must be on or before the end date',
      ],
      [
        'an end after maxDate',
        '2026-09-01',
        '2026-10-11',
        'end',
        'End date must be on or before 2026-10-10',
      ],
      ['a range one day over maxDays', '2026-07-12', '2026-10-10', 'end', 'Range must be at most 90 days'],
    ])('rejects %s on that field alone and focuses it', (_case, start, end, field, message) => {
      const { onSelectCustom } = renderPicker({ value: { kind: 'custom', start, end } });

      apply();

      const invalid = field === 'start' ? startField() : endField();
      const valid = field === 'start' ? endField() : startField();
      const label = field === 'start' ? 'Start date' : 'End date';
      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(invalid.getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(invalid)).toBe(message);
      expect(valid.hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(valid)).toBeNull();
      expect(document.activeElement).toBe(invalid);
      expect(statusRegion().textContent).toBe(`1 error: ${label}: ${message}.`);
    });

    it('words a one-day maximum in the singular', () => {
      renderPicker({ maxDays: 1, value: { kind: 'custom', start: '2026-10-09', end: '2026-10-10' } });

      apply();

      expect(messageOf(endField())).toBe('Range must be at most 1 day');
    });

    it('updates messages on every edit after a failed Apply, but not the summary', () => {
      renderPicker();
      fireEvent.click(customToggle());
      apply();
      const summary = statusRegion().textContent;

      fireEvent.change(startField(), { target: { value: '2026-09-01' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(startField())).toBeNull();
      expect(messageOf(endField())).toBe('Enter an end date');

      fireEvent.change(endField(), { target: { value: '2026-08-01' } });

      expect(messageOf(startField())).toBe('Start date must be on or before the end date');
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe(summary);
    });

    it('focuses again and re-announces a repeated identical failure', () => {
      renderPicker();
      fireEvent.click(customToggle());
      apply();
      const firstSummary = statusRegion().firstElementChild;
      expect(firstSummary?.textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );

      const applyButton = screen.getByRole('button', { name: 'Apply' });
      applyButton.focus();
      expect(document.activeElement).toBe(applyButton);
      apply();

      expect(document.activeElement).toBe(startField());
      const secondSummary = statusRegion().firstElementChild;
      expect(secondSummary?.textContent).toBe(firstSummary?.textContent);
      expect(secondSummary).not.toBe(firstSummary);
    });

    it('submits from a date field, as Enter does, and keeps focus there', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());
      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      const end = endField();
      end.focus();

      const form = end.form;
      expect(form).not.toBeNull();
      if (form !== null) {
        fireEvent.submit(form);
      }

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(end);
      expect(statusRegion().textContent).toBe('1 error: End date: Enter an end date.');
    });

    it('clears messages and the summary after a valid Apply, until the next Apply', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());
      apply();
      expect(statusRegion().textContent).not.toBe('');

      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      fireEvent.change(endField(), { target: { value: '2026-09-30' } });
      apply();

      expect(onSelectCustom).toHaveBeenCalledWith('2026-09-01', '2026-09-30');
      expect(statusRegion().textContent).toBe('');

      fireEvent.change(endField(), { target: { value: '2026-08-01' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(startField())).toBeNull();

      apply();

      expect(messageOf(startField())).toBe('Start date must be on or before the end date');
      expect(onSelectCustom).toHaveBeenCalledTimes(1);
    });
  });

  describe('following value', () => {
    it('opens a new custom value with its range and clears earlier messages', () => {
      const { rerenderWith } = renderPicker();
      fireEvent.click(customToggle());
      apply();
      expect(startField().getAttribute('aria-invalid')).toBe('true');

      rerenderWith({ value: { kind: 'custom', start: '2026-08-01', end: '2026-08-31' } });

      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
      expect(startField().value).toBe('2026-08-01');
      expect(endField().value).toBe('2026-08-31');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('');
      expect(pressedStates()).toEqual(['false', 'false', 'false', 'false']);
    });

    it('reopens a collapsed panel when value changes to another custom range', () => {
      const { rerenderWith } = renderPicker({
        value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' },
      });
      fireEvent.click(customToggle());
      expect(customToggle().getAttribute('aria-expanded')).toBe('false');

      rerenderWith({ value: { kind: 'custom', start: '2026-09-02', end: '2026-09-30' } });

      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
      expect(startField().value).toBe('2026-09-02');
    });

    it('leaves the panel and drafts as they were when value changes to a preset', () => {
      const { rerenderWith } = renderPicker();
      fireEvent.click(customToggle());
      fireEvent.change(startField(), { target: { value: '2026-09-01' } });

      rerenderWith({ value: { kind: 'preset', days: 56 } });

      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
      expect(startField().value).toBe('2026-09-01');
      expect(pressedStates()).toEqual(['false', 'false', 'true', 'false']);
    });
  });

  describe('disabled', () => {
    it('disables every preset, the toggle, both fields and Apply', () => {
      renderPicker({
        disabled: true,
        value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' },
      });

      const buttons = screen.getAllByRole<HTMLButtonElement>('button');
      expect(buttons.map((button) => button.textContent)).toEqual([
        '14 days',
        '28 days (default)',
        '56 days',
        '90 days',
        'Custom range',
        'Apply',
      ]);
      expect(buttons.every((button) => button.disabled)).toBe(true);
      expect(startField().disabled).toBe(true);
      expect(endField().disabled).toBe(true);
    });

    it('ignores a submit while disabled', () => {
      const { onSelectCustom } = renderPicker({
        disabled: true,
        value: { kind: 'custom', start: '', end: '' },
      });

      const form = startField().form;
      expect(form).not.toBeNull();
      if (form !== null) {
        fireEvent.submit(form);
      }

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('');
    });
  });

  describe('window configuration', () => {
    /** The configuration problems listed, in order, asserting that no control is rendered. */
    function listedProblems(): (string | null)[] {
      expect(screen.getByText('Analysis window').tagName).toBe('P');
      expect(screen.getByText('No window can be chosen: the window settings are invalid')).toBeDefined();
      expect(screen.queryByRole('group')).toBeNull();
      expect(screen.queryByRole('button')).toBeNull();
      expect(screen.queryByRole('status', { hidden: true })).toBeNull();
      expect(document.querySelector('input')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
      return screen.getAllByRole('listitem').map((item) => item.textContent);
    }

    it('fails closed and names every problem of an invalid configuration', () => {
      renderPicker({
        presets: [14, 0, 7.5, -3],
        defaultPreset: 30,
        maxDays: Number.NaN,
        maxDate: '2026-02-30',
      });

      expect(listedProblems()).toEqual([
        'Every preset must be a whole number of days, 1 or more; received 0, 7.5, -3.',
        'The default window of 30 days is not one of the presets.',
        'The longest custom range must be a whole number of days, 1 or more; received NaN.',
        'The latest selectable date must be a calendar date (YYYY-MM-DD); received "2026-02-30".',
      ]);
    });

    it('rejects a default window that is not a day count', () => {
      renderPicker({ presets: [0, 14], defaultPreset: 0 });

      expect(listedProblems()).toEqual([
        'Every preset must be a whole number of days, 1 or more; received 0.',
        'The default window must be a whole number of days, 1 or more; received 0.',
      ]);
    });

    it('rejects a default of one day that is not a preset, in the singular', () => {
      renderPicker({ defaultPreset: 1 });

      expect(listedProblems()).toEqual(['The default window of 1 day is not one of the presets.']);
    });

    it.each([
      ['an impossible date', '2026-02-30'],
      ['year 0000', '0000-12-31'],
      ['a non-ISO date', '10/10/2026'],
      ['an empty string', ''],
    ])('rejects a maxDate that is %s', (_case, maxDate) => {
      renderPicker({ maxDate });

      expect(listedProblems()).toEqual([
        `The latest selectable date must be a calendar date (YYYY-MM-DD); received "${maxDate}".`,
      ]);
    });

    it.each([
      ['zero', 0],
      ['a fraction', 1.5],
      ['infinity', Number.POSITIVE_INFINITY],
    ])('rejects a maxDays that is %s', (_case, maxDays) => {
      renderPicker({ maxDays });

      expect(listedProblems()).toEqual([
        `The longest custom range must be a whole number of days, 1 or more; received ${String(maxDays)}.`,
      ]);
    });

    it('recovers its controls as soon as the configuration is fixed', () => {
      const { rerenderWith } = renderPicker({ defaultPreset: 30 });
      expect(screen.queryByRole('group')).toBeNull();

      rerenderWith({ defaultPreset: 28 });

      expect(screen.getByRole('group', { name: 'Analysis window' })).toBeDefined();
      expect(screen.queryByText('No window can be chosen: the window settings are invalid')).toBeNull();
    });
  });
});
