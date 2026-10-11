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
 *   Start date and End date fields bounded by `maxDate`, and an md Apply in its own grid
 *   wrapper after them;
 * - custom-range validation mirroring `POST /runs`: required fields, real calendar dates (an
 *   impossible date such as 2026-02-30 is rejected), start on or before end, end on or before
 *   `maxDate`, and at most `maxDays` days, both ends inclusive. Messages appear on the field
 *   (`aria-invalid`, `aria-describedby`) only after the first Apply and follow later edits;
 *   focus moves to the first invalid field and a polite `status` region announces a summary,
 *   which clears as soon as an edit changes what it reports (a field fixed or a message
 *   changed), stays while edits leave the errors as they were, and returns only with the next
 *   Apply;
 * - the caller owns the selection: callbacks fire, the picker follows `value`;
 * - the caller's `actions` sit on the preset row after the preset group, never inside it, and
 *   survive an invalid configuration; its `statusMessage` is announced by a picker-level polite
 *   `status` region outside the custom panel, committed empty and filled shortly after, never in
 *   the commit's task, with only the latest of several quick messages shown and a cleared one
 *   removed at once;
 * - `disabled` disables every control, and an invalid window configuration fails closed with a
 *   static error naming each problem and no control.
 *
 * The fixture's window configuration is deliberately unlike the product's `config/thresholds.yaml`
 * defaults (presets 14, 28, 56 and 90, default 28, at most 180 days), and one test swaps the
 * preset array for another, so the tests prove the picker renders the configuration it is given
 * and hard-codes no window length (AAP 0.5.5).
 *
 * jsdom, like a browser, sanitizes the value of an `<input type="date">`, so a malformed or
 * impossible date cannot be typed into a field. Such dates reach the picker's drafts the way a
 * reopened custom run does: through a custom `value`.
 *
 * Keyboard operation is exercised with Tab, Shift+Tab, Enter and Space. jsdom performs no
 * sequential navigation, key activation or implicit submission of its own, so the local helpers
 * dispatch each key to the focused element and take the browser's default step (moving focus,
 * activating a focused button, or submitting the form from a date field through its default
 * button) only when no handler prevented the key. One case wraps the picker in a handler that
 * prevents Enter and Space, showing that the keyboard cases depend on that step. jsdom cannot
 * type into a date input either, so field values are still entered with `change` events.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DateRangePicker, type DateRangePickerProps, type DateRangeValue } from './DateRangePicker';

/** `window.presets` for these tests: deliberately not the product's default presets. */
const PRESETS: readonly number[] = [7, 21, 35];

/** `window.default_days` for these tests: one of {@link PRESETS}, but not the product's 28. */
const DEFAULT_PRESET = 21;

/** The default window end: the latest selectable date. */
const MAX_DATE = '2026-10-05';

/** `window.max_days` for these tests: deliberately not the product's 180. */
const MAX_DAYS = 120;

/** The default run's window. */
const DEFAULT_VALUE: DateRangeValue = { kind: 'preset', days: DEFAULT_PRESET };

/** The props of a picker under test, with mock callbacks the test can inspect. */
function pickerProps(overrides: Partial<DateRangePickerProps> = {}) {
  const onSelectPreset = vi.fn<(days: number) => void>();
  const onSelectCustom = vi.fn<(start: string, end: string) => void>();
  const props: DateRangePickerProps = {
    label: 'Analysis window',
    presets: PRESETS,
    defaultPreset: DEFAULT_PRESET,
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

/**
 * The polite submit-summary status region of the custom form, visible or not. It is found
 * inside the form of the Start date field, so the picker-level region that announces
 * `statusMessage` is never mistaken for it.
 */
function statusRegion(): HTMLElement {
  const form = startField().form;
  if (form === null) {
    throw new Error('Start date is not inside the custom range form.');
  }
  return within(form).getByRole('status', { hidden: true });
}

/**
 * The picker-level polite status region that announces `statusMessage`, visible or not: the one
 * status region outside the custom form.
 *
 * @throws when there is not exactly one such region
 */
function windowStatusRegion(): HTMLElement {
  const regions = screen
    .getAllByRole('status', { hidden: true })
    .filter((region) => region.closest('form') === null);
  const [region] = regions;
  if (regions.length !== 1 || region === undefined) {
    throw new Error(`Expected one picker-level status region, found ${regions.length}.`);
  }
  return region;
}

/**
 * The longest a `statusMessage` may take to reach the picker-level status region: the picker
 * holds a message back this long after the commit (its `STATUS_MESSAGE_DELAY_MS`), so the region
 * is in the accessibility tree before its text changes.
 */
const STATUS_MESSAGE_DELAY_MS = 100;

/**
 * One rendered frame at 60 Hz, rounded up to whole milliseconds: a message must not land within
 * it, so a browser renders the empty region at least once before its text arrives.
 */
const ONE_FRAME_MS = 17;

/** Advances the fake clock by `ms`, flushing every render the timers that fire cause. */
function elapse(ms: number): void {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
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

/**
 * The `<input>` types that block implicit submission (HTML, "Implicit submission"): Enter in one
 * of these fields submits its form.
 */
const IMPLICIT_SUBMISSION_TYPES: ReadonlySet<string> = new Set([
  'text',
  'search',
  'url',
  'tel',
  'email',
  'password',
  'date',
  'month',
  'week',
  'time',
  'datetime-local',
  'number',
]);

/**
 * Whether `element` is a stop in the simplified Tab model the picker fixtures need, judged
 * independently of the picker: a non-negative `tabIndex`, not a disabled form control or a
 * hidden input, and not inside a `[hidden]`, `[inert]` or `display: none` subtree. Browser focus
 * rules no fixture here exercises, such as `visibility: hidden`, a radio group's single stop and
 * shadow roots, are not modelled.
 */
function isTabStop(element: HTMLElement): boolean {
  if (element.tabIndex < 0 || element.matches(':disabled')) {
    return false;
  }
  if (element instanceof HTMLInputElement && element.type === 'hidden') {
    return false;
  }
  if (element.closest('[hidden], [inert]') !== null) {
    return false;
  }
  for (let node: Element | null = element; node !== null; node = node.parentElement) {
    if (getComputedStyle(node).display === 'none') {
      return false;
    }
  }
  return true;
}

/**
 * The modelled Tab order of the rendered picker's {@link isTabStop} stops: positive `tabIndex`
 * stops in ascending order (ties in tree order), then the `tabIndex` 0 stops in tree order.
 */
function tabOrder(): HTMLElement[] {
  const stops = Array.from(document.body.querySelectorAll('*')).filter(
    (element): element is HTMLElement => element instanceof HTMLElement && isTabStop(element),
  );
  const positive = stops
    .filter((stop) => stop.tabIndex > 0)
    .sort((a, b) => a.tabIndex - b.tabIndex);
  return [...positive, ...stops.filter((stop) => stop.tabIndex === 0)];
}

/**
 * The stop Tab (or Shift+Tab) moves to from `from` in the simplified Tab model: from the body,
 * the first (last) stop of {@link tabOrder}; from a stop, the next (previous) one; from a focused
 * element outside the order, the neighbouring stops of its place in tree order, as if it had
 * `tabIndex` 0.
 *
 * @param from - the focused element
 * @param backwards - `true` for Shift+Tab
 * @returns the stop to focus, or `null` when the sequence is exhausted in that direction
 */
function nextTabStop(from: Element, backwards: boolean): HTMLElement | null {
  const order = tabOrder();
  if (from === document.body) {
    return (backwards ? order.at(-1) : order[0]) ?? null;
  }
  const index = order.findIndex((stop) => stop === from);
  if (index >= 0) {
    return order[backwards ? index - 1 : index + 1] ?? null;
  }
  const following = order.findIndex(
    (stop) =>
      stop.tabIndex === 0 &&
      (from.compareDocumentPosition(stop) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
  );
  const place = following >= 0 ? following : order.length;
  return order[backwards ? place - 1 : place] ?? null;
}

/**
 * Presses Tab (or Shift+Tab) on the focused element. A keydown that no handler prevents leaves the
 * step to the browser, so the browser's step is simulated: focus moves to the next (previous)
 * stop of the modelled {@link tabOrder}. The keyup goes to the element focused afterwards, as in
 * a browser.
 *
 * @param shiftKey - `true` for Shift+Tab
 * @returns the newly focused element, or `null` when a handler prevented the keydown or the
 * sequence is exhausted, in which case focus has not moved
 */
function pressTab(shiftKey = false): HTMLElement | null {
  const from = document.activeElement ?? document.body;
  const allowed = fireEvent.keyDown(from, { key: 'Tab', code: 'Tab', shiftKey });
  const next = allowed ? nextTabStop(from, shiftKey) : null;
  if (next !== null) {
    act(() => {
      next.focus();
    });
  }
  fireEvent.keyUp(document.activeElement ?? document.body, { key: 'Tab', code: 'Tab', shiftKey });
  return next;
}

/** Whether `element` is a native button, which Enter and Space activate. */
function isNativeButton(element: Element): element is HTMLButtonElement | HTMLInputElement {
  return (
    element instanceof HTMLButtonElement ||
    (element instanceof HTMLInputElement && ['button', 'submit', 'reset'].includes(element.type))
  );
}

/**
 * Submits `form` implicitly, as Enter in one of its fields does (HTML, "Implicit submission"):
 * a click on the form's default button, its first submit button in tree order, which does nothing
 * while that button is disabled; with no submit button, the form is submitted only when at most
 * one of its fields blocks implicit submission. The controls are read from the document rather
 * than `form.elements`, which omits image buttons.
 */
function submitImplicitly(form: HTMLFormElement): void {
  const controls = Array.from(form.ownerDocument.querySelectorAll('button, input')).filter(
    (control): control is HTMLButtonElement | HTMLInputElement =>
      (control instanceof HTMLButtonElement || control instanceof HTMLInputElement) &&
      control.form === form,
  );
  const defaultButton = controls.find(
    (control) => control.type === 'submit' || control.type === 'image',
  );
  if (defaultButton !== undefined) {
    act(() => {
      defaultButton.click();
    });
    return;
  }
  const blocking = controls.filter(
    (control) => control instanceof HTMLInputElement && IMPLICIT_SUBMISSION_TYPES.has(control.type),
  );
  if (blocking.length <= 1) {
    act(() => {
      form.requestSubmit();
    });
  }
}

/**
 * Presses Enter on the focused element. When no handler prevents the keydown or the keypress,
 * the browser's step is simulated: a native button is activated, and a text-like field of a form
 * (a date field included) submits that form implicitly. The keyup goes to the element focused
 * afterwards, since a failed Apply may move focus.
 *
 * @returns `true` when no handler prevented the key, so the browser's step was taken
 */
function pressEnter(): boolean {
  const target = document.activeElement ?? document.body;
  const allowed =
    fireEvent.keyDown(target, { key: 'Enter', code: 'Enter' }) &&
    fireEvent.keyPress(target, { key: 'Enter', code: 'Enter', charCode: 13 });
  if (allowed) {
    if (isNativeButton(target)) {
      act(() => {
        target.click();
      });
    } else if (
      target instanceof HTMLInputElement &&
      target.form !== null &&
      IMPLICIT_SUBMISSION_TYPES.has(target.type)
    ) {
      submitImplicitly(target.form);
    }
  }
  fireEvent.keyUp(document.activeElement ?? document.body, { key: 'Enter', code: 'Enter' });
  return allowed;
}

/**
 * Presses Space on the focused element. A native button activates on the keyup, so it is
 * activated only when no handler prevented the keydown or the keyup.
 *
 * @returns `true` when no handler prevented the key, so the browser's step was taken
 */
function pressSpace(): boolean {
  const target = document.activeElement ?? document.body;
  const downAllowed = fireEvent.keyDown(target, { key: ' ', code: 'Space' });
  const upAllowed = fireEvent.keyUp(target, { key: ' ', code: 'Space' });
  const allowed = downAllowed && upAllowed;
  if (allowed && isNativeButton(target)) {
    act(() => {
      target.click();
    });
  }
  return allowed;
}

/**
 * Presses Tab until `element` has focus.
 *
 * @throws when the Tab sequence ends before reaching `element`
 */
function tabTo(element: HTMLElement): void {
  while (document.activeElement !== element) {
    if (pressTab() === null) {
      throw new Error(`Tab never reached ${element.outerHTML}`);
    }
  }
}

/**
 * Presses Tab (or Shift+Tab) once per element of `stops`, asserting that focus lands on each in
 * turn.
 */
function expectTabStops(stops: readonly HTMLElement[], shiftKey = false): void {
  for (const stop of stops) {
    expect(pressTab(shiftKey)).toBe(stop);
    expect(document.activeElement).toBe(stop);
  }
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
        '7 days',
        '21 days (default)',
        '35 days',
        'Custom range',
      ]);
      for (const name of ['7 days', '21 days (default)', '35 days', 'Custom range']) {
        expect(within(group).getByRole('button', { name })).toBeDefined();
      }
    });

    it('presses the default preset when value is the default run', () => {
      renderPicker();

      const pressed = (name: string) =>
        screen.getByRole('button', { name }).getAttribute('aria-pressed');
      expect(pressed('21 days (default)')).toBe('true');
      expect(pressed('7 days')).toBe('false');
      expect(pressed('35 days')).toBe('false');
    });

    it('presses only the preset matching value', () => {
      renderPicker({ value: { kind: 'preset', days: 35 } });

      expect(pressedStates()).toEqual(['false', 'false', 'true']);
      expect(customToggle().hasAttribute('aria-pressed')).toBe(false);
    });

    it('reports an activated preset and leaves the selection to the caller', () => {
      const { onSelectPreset, onSelectCustom } = renderPicker();

      fireEvent.click(screen.getByRole('button', { name: '35 days' }));

      expect(onSelectPreset).toHaveBeenCalledTimes(1);
      expect(onSelectPreset).toHaveBeenCalledWith(35);
      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(pressedStates()).toEqual(['false', 'true', 'false']);
    });

    it('reports the default preset by its length too', () => {
      const { onSelectPreset } = renderPicker({ value: { kind: 'preset', days: 7 } });

      fireEvent.click(screen.getByRole('button', { name: '21 days (default)' }));

      expect(onSelectPreset).toHaveBeenCalledTimes(1);
      expect(onSelectPreset).toHaveBeenCalledWith(21);
    });

    it('replaces its options when the configured presets and default change', () => {
      const { rerenderWith } = renderPicker();

      rerenderWith({ presets: [10, 30], defaultPreset: 30 });

      const group = screen.getByRole('group', { name: 'Analysis window' });
      expect(within(group).getAllByRole('button').map((button) => button.textContent)).toEqual([
        '10 days',
        '30 days (default)',
        'Custom range',
      ]);
      for (const gone of ['7 days', '21 days (default)', '21 days', '35 days']) {
        expect(screen.queryByRole('button', { name: gone })).toBeNull();
      }
      // The shown window (21 days) is no longer offered, so no preset is pressed.
      expect(pressedStates()).toEqual(['false', 'false']);

      rerenderWith({ presets: [10, 30], defaultPreset: 30, value: { kind: 'preset', days: 30 } });

      expect(pressedStates()).toEqual(['false', 'true']);
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

      expect(pressedStates()).toEqual(['false', 'false', 'false']);
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

    it('lays out the two fields, then an md primary Apply in its own wrapper, on one grid', () => {
      renderPicker({ value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' } });

      const applyButton = screen.getByRole('button', { name: 'Apply' });
      const wrapper = applyButton.parentElement;
      const fields = wrapper?.parentElement;
      expect(wrapper?.className).toBe('ds-daterange__apply');
      expect(wrapper?.children).toHaveLength(1);
      expect(applyButton.classList.contains('ds-button--md')).toBe(true);
      expect(applyButton.classList.contains('ds-button--sm')).toBe(false);
      expect(applyButton.classList.contains('ds-button--primary')).toBe(true);
      expect(fields?.className).toBe('ds-daterange__custom-fields');
      expect(Array.from(fields?.children ?? [], (child) => child.className)).toEqual([
        'ds-field',
        'ds-field',
        'ds-daterange__apply',
      ]);
      expect(fields?.children[0]?.contains(startField())).toBe(true);
      expect(fields?.children[1]?.contains(endField())).toBe(true);
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

    it('opens with the range of a custom value prefilled and no preset pressed', () => {
      renderPicker({ value: { kind: 'custom', start: '2026-09-01', end: '2026-09-30' } });

      const toggle = customToggle();
      const panel = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(panel?.hidden).toBe(false);
      expect(startField().value).toBe('2026-09-01');
      expect(endField().value).toBe('2026-09-30');
      expect(pressedStates()).toEqual(['false', 'false', 'false']);
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
      ['a one-day range ending on maxDate', '2026-10-05', '2026-10-05'],
      // 2026-06-08 to 2026-10-05, both ends inclusive: 23 + 31 + 31 + 30 + 5 = 120 days.
      ['a range of exactly maxDays days', '2026-06-08', '2026-10-05'],
      ['a year 0001 range', '0001-01-01', '0001-01-31'],
    ])('accepts %s', (_case, start, end) => {
      const { onSelectCustom } = renderPicker({ value: { kind: 'custom', start, end } });

      apply();

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith(start, end);
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
    });

    it('accepts a typed range of exactly maxDays days', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());

      fireEvent.change(startField(), { target: { value: '2026-06-08' } });
      fireEvent.change(endField(), { target: { value: '2026-10-05' } });
      apply();

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-06-08', '2026-10-05');
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
        '2026-09-20',
        '2026-09-10',
        'start',
        'Start date must be on or before the end date',
      ],
      [
        'an end after maxDate',
        '2026-09-01',
        '2026-10-06',
        'end',
        'End date must be on or before 2026-10-05',
      ],
      // 2026-06-07 to 2026-10-05, both ends inclusive: 121 days.
      [
        'a range one day over maxDays',
        '2026-06-07',
        '2026-10-05',
        'end',
        'Range must be at most 120 days',
      ],
      // 2026-01-01 to 2026-10-05, both ends inclusive: 273 + 5 = 278 days.
      ['a 278-day range', '2026-01-01', '2026-10-05', 'end', 'Range must be at most 120 days'],
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

    it.each([
      [
        'a start after the end',
        '2026-09-20',
        '2026-09-10',
        'start',
        'Start date must be on or before the end date',
      ],
      [
        'an end after maxDate',
        '2026-09-01',
        '2026-10-06',
        'end',
        'End date must be on or before 2026-10-05',
      ],
      ['a 278-day range', '2026-01-01', '2026-10-05', 'end', 'Range must be at most 120 days'],
    ])('rejects typed %s after Apply without reporting it', (_case, start, end, field, message) => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());

      fireEvent.change(startField(), { target: { value: start } });
      fireEvent.change(endField(), { target: { value: end } });
      apply();

      const invalid = field === 'start' ? startField() : endField();
      const valid = field === 'start' ? endField() : startField();
      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(invalid.getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(invalid)).toBe(message);
      expect(screen.getByText(message)).toBeDefined();
      expect(valid.hasAttribute('aria-invalid')).toBe(false);
    });

    it('words a one-day maximum in the singular', () => {
      renderPicker({
        maxDays: 1,
        value: { kind: 'custom', start: '2026-10-04', end: '2026-10-05' },
      });

      apply();

      expect(messageOf(endField())).toBe('Range must be at most 1 day');
    });

    it('updates messages on every edit; an edit empties the summary until the next Apply', () => {
      renderPicker();
      fireEvent.click(customToggle());
      apply();
      expect(statusRegion().textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );

      fireEvent.change(startField(), { target: { value: '2026-09-01' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(startField())).toBeNull();
      expect(messageOf(endField())).toBe('Enter an end date');
      expect(statusRegion().textContent).toBe('');

      fireEvent.change(endField(), { target: { value: '2026-08-01' } });

      expect(messageOf(startField())).toBe('Start date must be on or before the end date');
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('');
      expect(statusRegion().children).toHaveLength(0);

      apply();

      expect(statusRegion().textContent).toBe(
        '1 error: Start date: Start date must be on or before the end date.',
      );
    });

    it('empties the summary on a partial fix, and Apply announces the error left', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());
      apply();
      expect(statusRegion().textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );

      fireEvent.change(startField(), { target: { value: '2026-09-20' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(messageOf(startField())).toBeNull();
      expect(endField().getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(endField())).toBe('Enter an end date');
      expect(statusRegion().textContent).toBe('');
      expect(statusRegion().children).toHaveLength(0);

      apply();

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(endField());
      expect(statusRegion().textContent).toBe('1 error: End date: Enter an end date.');
    });

    it('keeps the summary, unannounced again, while edits leave the errors as they were', () => {
      renderPicker();
      fireEvent.click(customToggle());
      fireEvent.change(startField(), { target: { value: '2026-09-20' } });
      fireEvent.change(endField(), { target: { value: '2026-09-10' } });
      apply();
      const summary = '1 error: Start date: Start date must be on or before the end date.';
      const announced = statusRegion().firstElementChild;
      expect(announced?.textContent).toBe(summary);

      fireEvent.change(startField(), { target: { value: '2026-09-25' } });
      fireEvent.change(endField(), { target: { value: '2026-09-15' } });

      expect(messageOf(startField())).toBe('Start date must be on or before the end date');
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe(summary);
      expect(statusRegion().firstElementChild).toBe(announced);
    });

    it('empties the summary when an edit changes a message without fixing its field', () => {
      renderPicker();
      fireEvent.click(customToggle());
      fireEvent.change(startField(), { target: { value: '2026-09-20' } });
      fireEvent.change(endField(), { target: { value: '2026-09-10' } });
      apply();
      expect(statusRegion().textContent).toBe(
        '1 error: Start date: Start date must be on or before the end date.',
      );

      fireEvent.change(startField(), { target: { value: '' } });

      expect(startField().getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(startField())).toBe('Enter a start date');
      expect(statusRegion().textContent).toBe('');

      apply();

      expect(statusRegion().textContent).toBe('1 error: Start date: Enter a start date.');
    });

    it('keeps the summary empty and focus where it is as edits fix every field', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());
      apply();
      expect(document.activeElement).toBe(startField());

      fireEvent.change(startField(), { target: { value: '2026-09-20' } });

      expect(messageOf(endField())).toBe('Enter an end date');
      expect(statusRegion().textContent).toBe('');
      expect(document.activeElement).toBe(startField());

      const end = endField();
      end.focus();
      fireEvent.change(end, { target: { value: '2026-10-01' } });

      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('');
      expect(statusRegion().children).toHaveLength(0);
      expect(document.activeElement).toBe(end);
      expect(onSelectCustom).not.toHaveBeenCalled();
    });

    it('adds no summary for a field broken again after it cleared, until the next Apply', () => {
      const { onSelectCustom } = renderPicker();
      fireEvent.click(customToggle());
      apply();
      fireEvent.change(startField(), { target: { value: '2026-09-20' } });
      fireEvent.change(endField(), { target: { value: '2026-10-01' } });
      expect(statusRegion().textContent).toBe('');

      fireEvent.change(endField(), { target: { value: '2026-09-10' } });

      expect(startField().getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(startField())).toBe('Start date must be on or before the end date');
      expect(statusRegion().textContent).toBe('');

      apply();

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(startField());
      expect(statusRegion().textContent).toBe(
        '1 error: Start date: Start date must be on or before the end date.',
      );

      fireEvent.change(endField(), { target: { value: '2026-10-01' } });
      expect(statusRegion().textContent).toBe('');
      apply();

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-09-20', '2026-10-01');
      expect(statusRegion().textContent).toBe('');
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
      act(() => {
        applyButton.focus();
      });
      expect(document.activeElement).toBe(applyButton);
      apply();

      expect(document.activeElement).toBe(startField());
      const secondSummary = statusRegion().firstElementChild;
      expect(secondSummary?.textContent).toBe(firstSummary?.textContent);
      expect(secondSummary).not.toBe(firstSummary);
    });

    it('validates a direct form submission from a date field and keeps focus there', () => {
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
      expect(pressedStates()).toEqual(['false', 'false', 'false']);
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

      rerenderWith({ value: { kind: 'preset', days: 35 } });

      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
      expect(startField().value).toBe('2026-09-01');
      expect(pressedStates()).toEqual(['false', 'false', 'true']);
    });
  });

  describe('actions', () => {
    /** A caller control, as the status bar's recent custom windows disclosure would pass. */
    const ACTION = <button type="button">Recent custom windows</button>;

    it('places the actions on the preset row after the group, outside it', () => {
      renderPicker({ actions: ACTION });

      const group = screen.getByRole('group', { name: 'Analysis window' });
      const action = screen.getByRole('button', { name: 'Recent custom windows' });
      const row = group.parentElement;
      expect(group.contains(action)).toBe(false);
      expect(within(group).getAllByRole('button').map((button) => button.textContent)).toEqual([
        '7 days',
        '21 days (default)',
        '35 days',
        'Custom range',
      ]);
      expect(row?.className).toBe('ds-daterange__row');
      expect(Array.from(row?.children ?? [])).toEqual([group, action]);
    });

    it.each([
      ['no actions', undefined],
      ['null', null],
      ['false', false],
      ['an empty string', ''],
    ])('adds no element for %s', (_case, actions) => {
      renderPicker({ actions });

      const group = screen.getByRole('group', { name: 'Analysis window' });
      expect(group.parentElement?.className).toBe('ds-daterange__row');
      expect(Array.from(group.parentElement?.children ?? [])).toEqual([group]);
      expect(group.parentElement?.childNodes).toHaveLength(1);
    });

    it('keeps the actions after the configuration error when the configuration is invalid', () => {
      const { container, rerenderWith } = renderPicker({
        defaultPreset: 28,
        actions: ACTION,
        statusMessage: 'Custom window 2026-09-01 – 2026-09-30 opened',
      });

      const picker = container.firstElementChild;
      const row = picker?.lastElementChild;
      expect(row?.className).toBe('ds-daterange__row');
      expect(row?.previousElementSibling?.classList.contains('ds-banner')).toBe(true);
      expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
        'Recent custom windows',
      ]);
      expect(row?.contains(screen.getByRole('button', { name: 'Recent custom windows' }))).toBe(
        true,
      );
      expect(screen.queryByRole('group')).toBeNull();
      expect(screen.queryByRole('status', { hidden: true })).toBeNull();
      expect(document.querySelector('input')).toBeNull();

      rerenderWith({ actions: undefined });

      expect(container.querySelector('.ds-daterange__row')).toBeNull();
      expect(picker?.lastElementChild?.classList.contains('ds-banner')).toBe(true);
    });
  });

  describe('status message', () => {
    /** A message the status bar might pass once a custom run is shown. */
    const OPENED = 'Custom window 2026-09-20 – 2026-10-01 opened';

    /** The message of another custom window, which replaces {@link OPENED}. */
    const REOPENED = 'Custom window 2026-08-01 – 2026-08-31 opened';

    // A message lands a moment after the commit, so these cases run on a fake clock and let that
    // moment pass explicitly with `elapse`.
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('renders an empty picker-level region last, outside the custom panel', () => {
      const { container } = renderPicker();

      const region = windowStatusRegion();
      const panel = document.getElementById(customToggle().getAttribute('aria-controls') ?? '');
      expect(region.textContent).toBe('');
      expect(region.className).toBe('ds-visually-hidden');
      expect(container.firstElementChild?.lastElementChild).toBe(region);
      expect(panel?.contains(region)).toBe(false);
    });

    it('announces a trimmed message shortly after, while the custom panel is collapsed, never as an alert', () => {
      renderPicker({ statusMessage: `  ${OPENED}  ` });
      const region = windowStatusRegion();
      expect(region.textContent).toBe('');

      elapse(STATUS_MESSAGE_DELAY_MS);

      expect(windowStatusRegion()).toBe(region);
      expect(customToggle().getAttribute('aria-expanded')).toBe('false');
      expect(region.textContent).toBe(OPENED);
      // The submit summary is inside the collapsed panel, so this is the one exposed region.
      expect(screen.getByRole('status')).toBe(region);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('commits its region empty and fills it after a rendered frame, so a first message is announced', () => {
      const host = document.createElement('div');
      document.body.append(host);
      const records: MutationRecord[] = [];
      const observer = new MutationObserver((batch) => {
        records.push(...batch);
      });
      observer.observe(host, { childList: true, subtree: true, characterData: true });
      const { props } = pickerProps({ statusMessage: OPENED });

      // render() runs the commit and flushes its effects, as React may do in the commit's own
      // task, so nothing the region holds now could have been rendered before it was filled.
      render(<DateRangePicker {...props} />, { container: host });
      records.push(...observer.takeRecords());
      const region = windowStatusRegion();
      expect(region.textContent).toBe('');
      expect(records.some((record) => record.target === region)).toBe(false);

      elapse(ONE_FRAME_MS);
      expect(region.textContent).toBe('');

      elapse(STATUS_MESSAGE_DELAY_MS - ONE_FRAME_MS);
      records.push(...observer.takeRecords());
      observer.disconnect();

      expect(windowStatusRegion()).toBe(region);
      expect(region.textContent).toBe(OPENED);
      // A mutation inside the region means it was already in the document, empty, when its
      // text arrived; text rendered on the first commit would arrive with the region itself.
      expect(records.some((record) => record.target === region)).toBe(true);
    });

    it('replaces a message with a different one, in the same region, once its delay has passed', () => {
      const { rerenderWith } = renderPicker({ statusMessage: OPENED });
      const region = windowStatusRegion();
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(region.textContent).toBe(OPENED);

      rerenderWith({ statusMessage: REOPENED });

      // The message shown stays until the new one lands, so the region is never blanked.
      expect(region.textContent).toBe(OPENED);
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(windowStatusRegion()).toBe(region);
      expect(region.textContent).toBe(REOPENED);
    });

    it('never shows a message replaced before its delay passed', () => {
      const half = STATUS_MESSAGE_DELAY_MS / 2;
      const { rerenderWith } = renderPicker({ statusMessage: OPENED });
      const region = windowStatusRegion();
      elapse(half);
      expect(region.textContent).toBe('');

      rerenderWith({ statusMessage: REOPENED });
      // The first message's delay passes here: it was cancelled, so it never lands.
      elapse(half);
      expect(region.textContent).toBe('');

      elapse(half);
      expect(windowStatusRegion()).toBe(region);
      expect(region.textContent).toBe(REOPENED);
      expect(vi.getTimerCount()).toBe(0);
    });

    it('empties the region at once when the message is cleared or blank, dropping one still pending', () => {
      const { rerenderWith } = renderPicker({ statusMessage: OPENED });
      const region = windowStatusRegion();
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(region.textContent).toBe(OPENED);

      rerenderWith({ statusMessage: undefined });

      // No time passes: removing text is silent, so it is not held back.
      expect(windowStatusRegion()).toBe(region);
      expect(region.textContent).toBe('');

      rerenderWith({ statusMessage: OPENED });
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(region.textContent).toBe(OPENED);

      rerenderWith({ statusMessage: '   ' });
      expect(region.textContent).toBe('');

      rerenderWith({ statusMessage: REOPENED });
      rerenderWith({ statusMessage: undefined });
      elapse(STATUS_MESSAGE_DELAY_MS);

      expect(region.textContent).toBe('');
      expect(vi.getTimerCount()).toBe(0);
    });

    it('cancels a pending message when the picker unmounts', () => {
      const { unmount } = renderPicker({ statusMessage: OPENED });
      expect(vi.getTimerCount()).toBe(1);

      unmount();

      expect(vi.getTimerCount()).toBe(0);
    });

    it('commits its region empty again when a fixed configuration brings it back', () => {
      // 28 is not one of the fixture presets, so the configuration is invalid.
      const { rerenderWith } = renderPicker({ defaultPreset: 28, statusMessage: OPENED });
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(screen.queryByRole('status', { hidden: true })).toBeNull();

      rerenderWith({ defaultPreset: DEFAULT_PRESET });

      const region = windowStatusRegion();
      expect(region.textContent).toBe('');
      elapse(STATUS_MESSAGE_DELAY_MS);
      expect(windowStatusRegion()).toBe(region);
      expect(region.textContent).toBe(OPENED);
    });

    it('keeps the status message apart from the submit summary of a failed Apply', () => {
      renderPicker({ statusMessage: OPENED });
      elapse(STATUS_MESSAGE_DELAY_MS);
      fireEvent.click(customToggle());

      apply();

      expect(statusRegion()).not.toBe(windowStatusRegion());
      expect(statusRegion().textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );
      expect(windowStatusRegion().textContent).toBe(OPENED);
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
        '7 days',
        '21 days (default)',
        '35 days',
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

  describe('keyboard operation', () => {
    /** A custom value whose range is valid for the fixture: it opens the panel prefilled. */
    const CUSTOM_VALUE: DateRangeValue = { kind: 'custom', start: '2026-09-01', end: '2026-09-30' };

    /**
     * Renders a picker followed by a raw "After" button, the page's next stop, so a Tab past the
     * picker's last stop shows where focus goes. With `blockKeys`, the picker sits inside a
     * wrapper whose keydown handler prevents Enter and Space, as a page-level key handler might.
     */
    function renderInPage(overrides: Partial<DateRangePickerProps> = {}, blockKeys = false) {
      const { props, onSelectPreset, onSelectCustom } = pickerProps(overrides);
      const picker = <DateRangePicker {...props} />;
      render(
        <>
          {blockKeys ? (
            <div
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                }
              }}
            >
              {picker}
            </div>
          ) : (
            picker
          )}
          <button type="button">After</button>
        </>,
      );
      return { onSelectPreset, onSelectCustom, after: button('After') };
    }

    /** The visible button named `name`. */
    function button(name: string): HTMLElement {
      return screen.getByRole('button', { name });
    }

    /** Tabs from where focus is to the "Custom range" toggle and opens its panel with Enter. */
    function openCustomByKeyboard(): void {
      tabTo(customToggle());
      expect(pressEnter()).toBe(true);
      expect(customToggle().getAttribute('aria-expanded')).toBe('true');
    }

    it('reaches the presets and Custom range in order with Tab, not the collapsed panel', () => {
      const { after } = renderInPage();
      const presets = ['7 days', '21 days (default)', '35 days'].map(button);
      const panel = document.getElementById(customToggle().getAttribute('aria-controls') ?? '');
      expect(panel?.hidden).toBe(true);
      expect(document.activeElement).toBe(document.body);

      expectTabStops([...presets, customToggle(), after]);
      expect(pressTab()).toBeNull();
      expect(document.activeElement).toBe(after);

      expectTabStops([customToggle(), ...presets.toReversed()], true);
      expect(pressTab(true)).toBeNull();
      expect(document.activeElement).toBe(presets[0]);
    });

    it('toggles Custom range with Enter and Space, its fields Tab stops only while open', () => {
      const { after } = renderInPage();
      const toggle = customToggle();
      const panel = document.getElementById(toggle.getAttribute('aria-controls') ?? '');
      tabTo(toggle);

      expect(pressEnter()).toBe(true);

      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      expect(panel?.hidden).toBe(false);
      expect(document.activeElement).toBe(toggle);
      const applyButton = button('Apply');
      expectTabStops([startField(), endField(), applyButton, after]);
      expectTabStops([applyButton, endField(), startField(), toggle], true);

      expect(pressSpace()).toBe(true);

      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(panel?.hidden).toBe(true);
      expect(document.activeElement).toBe(toggle);
      expectTabStops([after]);
      expectTabStops([toggle], true);
    });

    it('reports a preset activated with Enter and one with Space, keeping focus on each', () => {
      const { onSelectPreset, onSelectCustom } = renderInPage();
      const longest = button('35 days');
      const shortest = button('7 days');
      tabTo(longest);

      expect(pressEnter()).toBe(true);

      expect(onSelectPreset).toHaveBeenCalledTimes(1);
      expect(onSelectPreset).toHaveBeenNthCalledWith(1, 35);
      expect(document.activeElement).toBe(longest);

      expectTabStops([button('21 days (default)'), shortest], true);
      expect(pressSpace()).toBe(true);

      expect(onSelectPreset).toHaveBeenCalledTimes(2);
      expect(onSelectPreset).toHaveBeenNthCalledWith(2, 7);
      expect(document.activeElement).toBe(shortest);
      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(pressedStates()).toEqual(['false', 'true', 'false']);
    });

    it('submits a valid range on Enter in End date and keeps focus there', () => {
      const { onSelectCustom } = renderInPage();
      openCustomByKeyboard();
      expectTabStops([startField()]);
      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      expectTabStops([endField()]);
      fireEvent.change(endField(), { target: { value: '2026-09-30' } });

      expect(pressEnter()).toBe(true);

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-09-01', '2026-09-30');
      expect(statusRegion().textContent).toBe('');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(document.activeElement).toBe(endField());
    });

    it('submits a valid range on Enter in Start date and keeps focus there', () => {
      const { onSelectCustom } = renderInPage();
      openCustomByKeyboard();
      expectTabStops([startField()]);
      fireEvent.change(startField(), { target: { value: '2026-08-03' } });
      expectTabStops([endField()]);
      fireEvent.change(endField(), { target: { value: '2026-08-30' } });
      expectTabStops([startField()], true);

      expect(pressEnter()).toBe(true);

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-08-03', '2026-08-30');
      expect(statusRegion().textContent).toBe('');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
      expect(document.activeElement).toBe(startField());
    });

    it('names an empty End date on Enter there, keeps focus on it and announces the error', () => {
      const { onSelectCustom } = renderInPage();
      openCustomByKeyboard();
      expectTabStops([startField()]);
      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      expectTabStops([endField()]);

      expect(pressEnter()).toBe(true);

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(endField());
      expect(endField().getAttribute('aria-invalid')).toBe('true');
      expect(messageOf(endField())).toBe('Enter an end date');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(statusRegion().textContent).toBe('1 error: End date: Enter an end date.');
    });

    it.each([
      ['an empty Start date', '', '2026-09-30', 'Enter a start date'],
      [
        'a Start date after the End date',
        '2026-09-20',
        '2026-09-10',
        'Start date must be on or before the end date',
      ],
    ])(
      'moves focus to Start date for %s on Enter in End date and announces the error',
      (_case, start, end, message) => {
        const { onSelectCustom } = renderInPage();
        openCustomByKeyboard();
        tabTo(endField());
        fireEvent.change(startField(), { target: { value: start } });
        fireEvent.change(endField(), { target: { value: end } });

        expect(pressEnter()).toBe(true);

        expect(onSelectCustom).not.toHaveBeenCalled();
        expect(document.activeElement).toBe(startField());
        expect(startField().getAttribute('aria-invalid')).toBe('true');
        expect(messageOf(startField())).toBe(message);
        expect(endField().hasAttribute('aria-invalid')).toBe(false);
        expect(statusRegion().textContent).toBe(`1 error: Start date: ${message}.`);
      },
    );

    it.each([
      ['Space', pressSpace],
      ['Enter', pressEnter],
    ])('applies a valid range with %s on Apply', (_key, press) => {
      const { onSelectCustom } = renderInPage();
      openCustomByKeyboard();
      fireEvent.change(startField(), { target: { value: '2026-09-01' } });
      fireEvent.change(endField(), { target: { value: '2026-09-30' } });
      const applyButton = button('Apply');
      tabTo(applyButton);

      expect(press()).toBe(true);

      expect(onSelectCustom).toHaveBeenCalledTimes(1);
      expect(onSelectCustom).toHaveBeenCalledWith('2026-09-01', '2026-09-30');
      expect(statusRegion().textContent).toBe('');
      expect(document.activeElement).toBe(applyButton);
    });

    it('names both empty fields on Space on Apply and moves focus to Start date', () => {
      const { onSelectCustom } = renderInPage();
      openCustomByKeyboard();
      tabTo(button('Apply'));

      expect(pressSpace()).toBe(true);

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(startField());
      expect(messageOf(startField())).toBe('Enter a start date');
      expect(messageOf(endField())).toBe('Enter an end date');
      expect(statusRegion().textContent).toBe(
        '2 errors: Start date: Enter a start date. End date: Enter an end date.',
      );
    });

    it('takes no keyboard action when a handler prevents Enter and Space', () => {
      const { onSelectPreset, onSelectCustom } = renderInPage({ value: CUSTOM_VALUE }, true);
      const toggle = customToggle();

      tabTo(button('35 days'));
      expect(pressEnter()).toBe(false);
      expect(pressSpace()).toBe(false);
      expect(onSelectPreset).not.toHaveBeenCalled();

      tabTo(toggle);
      expect(pressEnter()).toBe(false);
      expect(pressSpace()).toBe(false);
      expect(toggle.getAttribute('aria-expanded')).toBe('true');

      tabTo(endField());
      expect(endField().value).toBe('2026-09-30');
      expect(pressEnter()).toBe(false);
      tabTo(button('Apply'));
      expect(pressEnter()).toBe(false);
      expect(pressSpace()).toBe(false);

      expect(onSelectCustom).not.toHaveBeenCalled();
      expect(onSelectPreset).not.toHaveBeenCalled();
      expect(statusRegion().textContent).toBe('');
      expect(startField().hasAttribute('aria-invalid')).toBe(false);
      expect(endField().hasAttribute('aria-invalid')).toBe(false);
    });

    it('offers no Tab stop while disabled, even with its custom panel open', () => {
      const { after } = renderInPage({ disabled: true, value: CUSTOM_VALUE });
      expect(customToggle().getAttribute('aria-expanded')).toBe('true');

      expectTabStops([after]);
      expect(pressTab()).toBeNull();
      expect(pressTab(true)).toBeNull();
      expect(document.activeElement).toBe(after);

      act(() => {
        after.blur();
      });
      expect(document.activeElement).toBe(document.body);
      expectTabStops([after], true);
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
      // 28 is the product's default window, but not one of these presets.
      const { rerenderWith } = renderPicker({ defaultPreset: 28 });
      expect(screen.queryByRole('group')).toBeNull();
      expect(listedProblems()).toEqual([
        'The default window of 28 days is not one of the presets.',
      ]);

      rerenderWith({ defaultPreset: DEFAULT_PRESET });

      expect(screen.getByRole('group', { name: 'Analysis window' })).toBeDefined();
      expect(screen.getByRole('button', { name: '21 days (default)' })).toBeDefined();
      expect(screen.queryByText('No window can be chosen: the window settings are invalid')).toBeNull();
    });
  });
});
