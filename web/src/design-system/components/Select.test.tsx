/**
 * Contract tests for `Select`, the design-system single-choice list (AAP 0.4.2, status GAP;
 * `../GAPS.md`, binding procedure step 6).
 *
 * They pin what feature code and assistive technology rely on, so a later Blitzy OS binding that
 * changes the wrapper's internals must still pass them:
 * - a native `combobox` named by its visible label, also when required, whose asterisk stays
 *   out of the name;
 * - one `option` per entry of `options`, in order, with disabled options disabled;
 * - the hint and error are described by `aria-describedby` in the order hint, error, extra ids,
 *   each id once, and `aria-invalid="true"` appears only while an error message is shown;
 * - the select is controlled and typed: `onChange` receives the chosen option's value, and a
 *   string no option carries is ignored;
 * - `name`, `id`, `disabled` and `ref` reach the `<select>`.
 */
import { createRef } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Select, type SelectOption } from './Select';

/** The test-only union typing the select's value; `removed` carries the disabled option. */
type Status = 'active' | 'paused' | 'removed';

/** The status choices, with one option shown but not choosable. */
const STATUS_OPTIONS: readonly SelectOption<Status>[] = [
  { value: 'active', label: 'Active' },
  { value: 'paused', label: 'Paused' },
  { value: 'removed', label: 'Removed', disabled: true },
];

/** The no-op change handler for renders whose test never changes the choice. */
const ignoreChange = (): void => undefined;

describe('Select', () => {
  describe('role, name and options', () => {
    it('renders a combobox named by its label with one option per choice in order', () => {
      render(<Select label="Status" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />);

      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });
      expect(select.tagName).toBe('SELECT');
      const options = within(select).getAllByRole<HTMLOptionElement>('option');
      expect(options.map((option) => option.textContent)).toEqual(['Active', 'Paused', 'Removed']);
      expect(options.map((option) => option.value)).toEqual(['active', 'paused', 'removed']);
      expect(options.map((option) => option.disabled)).toEqual([false, false, true]);
      const label = screen.getByText('Status');
      expect(label.tagName).toBe('LABEL');
      expect(label.getAttribute('for')).toBe(select.id);
    });

    it('is found by its label text, typed by an explicit value union, holding value', () => {
      render(
        <Select<Status> label="Status" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />,
      );

      // getByLabelText follows the label's `for` to the control, as a sighted user reads it.
      const select = screen.getByLabelText<HTMLSelectElement>('Status');
      expect(select.tagName).toBe('SELECT');
      expect(select.value).toBe('active');
      expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
        'Active',
        'Paused',
        'Removed',
      ]);
    });

    it('selects the option matching value', () => {
      render(<Select label="Status" options={STATUS_OPTIONS} value="paused" onChange={ignoreChange} />);

      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });
      expect(select.value).toBe('paused');
      expect(screen.getByRole<HTMLOptionElement>('option', { name: 'Paused' }).selected).toBe(true);
      expect(screen.getByRole<HTMLOptionElement>('option', { name: 'Active' }).selected).toBe(false);
    });

    it('marks a required select natively and keeps the asterisk out of its name', () => {
      render(
        <Select label="Status" required options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />,
      );

      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });
      expect(select.required).toBe(true);
      const label = document.querySelector(`label[for="${select.id}"]`);
      expect(label?.textContent).toBe('Status *');
      expect(label?.querySelector('.ds-field__required')?.getAttribute('aria-hidden')).toBe('true');
    });

    it('shows no asterisk on an optional select', () => {
      render(<Select label="Status" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />);

      expect(screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' }).required).toBe(false);
      expect(document.querySelector('.ds-field__required')).toBeNull();
    });

    it('renders an empty select for an empty option list', () => {
      render(<Select label="Status" options={[]} value="" onChange={ignoreChange} />);

      const select = screen.getByRole('combobox', { name: 'Status' });
      expect(within(select).queryAllByRole('option')).toHaveLength(0);
    });

    it('generates distinct ids for selects rendered side by side', () => {
      render(
        <>
          <Select label="First" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />
          <Select label="Second" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />
        </>,
      );

      expect(screen.getByRole('combobox', { name: 'First' }).id).not.toBe(
        screen.getByRole('combobox', { name: 'Second' }).id,
      );
    });
  });

  describe('hint, error and aria-describedby', () => {
    it('renders no description and no invalid state without a hint or error', () => {
      render(<Select label="Status" options={STATUS_OPTIONS} value="active" onChange={ignoreChange} />);

      const select = screen.getByRole('combobox', { name: 'Status' });
      expect(select.hasAttribute('aria-describedby')).toBe(false);
      expect(select.hasAttribute('aria-invalid')).toBe(false);
      expect(document.querySelector('p')).toBeNull();
    });

    it('describes the select by its hint, with ids derived from the given id', () => {
      render(
        <Select
          id="status"
          label="Status"
          hint="The status you set in the ad platform."
          options={STATUS_OPTIONS}
          value="active"
          onChange={ignoreChange}
        />,
      );

      const select = screen.getByRole('combobox', { name: 'Status' });
      expect(select.id).toBe('status');
      expect(select.getAttribute('aria-describedby')).toBe('status-hint');
      expect(document.getElementById('status-hint')?.textContent).toBe(
        'The status you set in the ad platform.',
      );
      expect(select.hasAttribute('aria-invalid')).toBe(false);
    });

    it('marks the select invalid while an error is shown and reads the hint first', () => {
      render(
        <Select
          id="status"
          label="Status"
          hint="Hint text."
          error="Choose a status"
          options={STATUS_OPTIONS}
          value="active"
          onChange={ignoreChange}
        />,
      );

      const select = screen.getByRole('combobox', { name: 'Status' });
      expect(select.getAttribute('aria-invalid')).toBe('true');
      expect(select.getAttribute('aria-describedby')).toBe('status-hint status-error');
      const error = document.getElementById('status-error');
      expect(error?.tagName).toBe('P');
      expect(error?.textContent).toBe('Choose a status');
      expect(screen.getByRole('combobox', { description: 'Hint text. Choose a status' })).toBe(select);
    });

    it.each([
      ['an empty string', ''],
      ['null', null],
      ['false', false],
    ])('treats %s as no hint and no error', (_name, empty) => {
      render(
        <Select
          id="s"
          label="Status"
          hint={empty}
          error={empty}
          options={STATUS_OPTIONS}
          value="active"
          onChange={ignoreChange}
        />,
      );

      const select = screen.getByRole('combobox', { name: 'Status' });
      expect(select.hasAttribute('aria-invalid')).toBe(false);
      expect(select.hasAttribute('aria-describedby')).toBe(false);
      expect(document.getElementById('s-hint')).toBeNull();
      expect(document.getElementById('s-error')).toBeNull();
    });

    it('appends extra description ids after the hint and error, each id once', () => {
      render(
        <Select
          id="s"
          label="Status"
          hint="Hint"
          error="Error"
          aria-describedby="tip s-error tip"
          options={STATUS_OPTIONS}
          value="active"
          onChange={ignoreChange}
        />,
      );

      expect(screen.getByRole('combobox', { name: 'Status' }).getAttribute('aria-describedby')).toBe(
        's-hint s-error tip',
      );
    });
  });

  describe('controlled, typed change', () => {
    it('calls onChange with the chosen option value and stays on value until passed back', () => {
      const onChange = vi.fn<(value: Status) => void>();
      const { rerender } = render(
        <Select label="Status" options={STATUS_OPTIONS} value="active" onChange={onChange} />,
      );
      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });

      fireEvent.change(select, { target: { value: 'paused' } });

      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('paused');
      expect(select.value).toBe('active');

      rerender(<Select label="Status" options={STATUS_OPTIONS} value="paused" onChange={onChange} />);
      expect(select.value).toBe('paused');
    });

    it('ignores a value that no option carries', () => {
      const onChange = vi.fn<(value: Status) => void>();
      render(<Select label="Status" options={STATUS_OPTIONS} value="active" onChange={onChange} />);
      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });

      // Only a script can produce such a value: inject a foreign option and choose it.
      const foreign = document.createElement('option');
      foreign.value = 'archived';
      foreign.textContent = 'Archived';
      select.append(foreign);
      fireEvent.change(select, { target: { value: 'archived' } });

      expect(onChange).not.toHaveBeenCalled();
      // Controlled: the select is restored to the caller's value after the ignored change.
      expect(select.value).toBe('active');
    });

    it('passes name, disabled and ref to the select', () => {
      const ref = createRef<HTMLSelectElement>();
      render(
        <Select
          ref={ref}
          name="status"
          disabled
          label="Status"
          options={STATUS_OPTIONS}
          value="active"
          onChange={ignoreChange}
        />,
      );

      const select = screen.getByRole<HTMLSelectElement>('combobox', { name: 'Status' });
      expect(ref.current).toBe(select);
      expect(select.getAttribute('name')).toBe('status');
      expect(select.disabled).toBe(true);
    });
  });
});
