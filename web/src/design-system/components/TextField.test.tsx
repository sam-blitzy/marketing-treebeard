/**
 * Contract tests for `TextField`, the design-system text input and text area (AAP 0.4.2, status
 * GAP; `../GAPS.md`, binding procedure step 6).
 *
 * They pin what feature code and assistive technology rely on, so a later Blitzy OS binding that
 * changes the wrapper's internals must still pass them:
 * - the visible label is the control's accessible name, also when the field is required, whose
 *   asterisk stays out of the name;
 * - a field is found by its label text (`getByLabelText`), the lookup the login form and the
 *   end-to-end specs depend on, for inputs and text areas alike;
 * - the hint and error are described by `aria-describedby` in the order hint, error, extra ids,
 *   each id once, and only content-bearing slots render;
 * - `aria-invalid="true"` appears only while an error message is shown;
 * - the field is controlled: `onChange` receives the new string and the DOM shows `value`;
 * - the single-line props (`type`, `inputMode`, `step`, `min`, `max`) and the multiline `rows`
 *   rule (default 3, unusable values fall back, fractions round down) reach the element;
 * - `ref` points at the rendered `<input>` or `<textarea>`.
 */
import { createRef } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { TextField } from './TextField';

/** The no-op change handler for renders whose test never edits the field. */
const ignoreChange = (): void => undefined;

describe('TextField', () => {
  describe('label and accessible name', () => {
    it('names a text input by its visible label', () => {
      render(<TextField label="Email" value="" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Email' });
      expect(input.tagName).toBe('INPUT');
      expect(input.getAttribute('type')).toBe('text');
      const label = screen.getByText('Email');
      expect(label.tagName).toBe('LABEL');
      expect(label.getAttribute('for')).toBe(input.id);
      expect(input.id).not.toBe('');
    });

    it('marks a required field natively and keeps the asterisk out of its name', () => {
      render(<TextField label="Email" required value="" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Email' });
      expect(input.hasAttribute('required')).toBe(true);
      const label = document.querySelector(`label[for="${input.id}"]`);
      expect(label?.textContent).toBe('Email *');
      const asterisk = label?.querySelector('.ds-field__required');
      expect(asterisk?.getAttribute('aria-hidden')).toBe('true');
      expect(asterisk?.textContent).toBe(' *');
    });

    it('shows no asterisk and no required attribute on an optional field', () => {
      render(<TextField label="Note" value="" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Note' });
      expect(input.hasAttribute('required')).toBe(false);
      expect(document.querySelector('.ds-field__required')).toBeNull();
    });

    it('generates distinct ids for fields rendered side by side', () => {
      render(
        <>
          <TextField label="First" value="" onChange={ignoreChange} />
          <TextField label="Second" value="" onChange={ignoreChange} />
        </>,
      );

      const first = screen.getByRole('textbox', { name: 'First' });
      const second = screen.getByRole('textbox', { name: 'Second' });
      expect(first.id).not.toBe(second.id);
    });
  });

  describe('label-based lookup', () => {
    it('finds an email input by its label text and reports edits', () => {
      const onChange = vi.fn<(value: string) => void>();
      render(<TextField label="Email" type="email" value="" onChange={onChange} />);

      const input = screen.getByLabelText<HTMLInputElement>('Email');
      expect(input.tagName).toBe('INPUT');
      expect(input.getAttribute('type')).toBe('email');

      fireEvent.change(input, { target: { value: 'a@b.c' } });

      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('a@b.c');
    });

    it('still finds a required field by its label text', () => {
      render(<TextField label="Email" required value="" onChange={ignoreChange} />);

      // getByLabelText matches the label's raw text, which includes the aria-hidden asterisk
      // ("Email *"), so an exact 'Email' would miss it; the anchored pattern is what the login
      // form's tests use. The accessible name, which skips aria-hidden content, stays "Email".
      const input = screen.getByLabelText<HTMLInputElement>(/^Email/u);
      expect(input.tagName).toBe('INPUT');
      expect(input.required).toBe(true);
      expect(screen.getByRole('textbox', { name: 'Email' })).toBe(input);
      const asterisk = document.querySelector(`label[for="${input.id}"] .ds-field__required`);
      expect(asterisk?.getAttribute('aria-hidden')).toBe('true');
    });

    it('finds a multiline field by its label text as a textarea', () => {
      render(<TextField multiline rows={4} label="Note" value="" onChange={ignoreChange} />);

      const area = screen.getByLabelText('Note');
      expect(area.tagName).toBe('TEXTAREA');
      expect(area.getAttribute('rows')).toBe('4');
    });
  });

  describe('hint, error and aria-describedby', () => {
    it('renders no description and no invalid state without a hint or error', () => {
      render(<TextField label="Email" value="" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Email' });
      expect(input.hasAttribute('aria-describedby')).toBe(false);
      expect(input.hasAttribute('aria-invalid')).toBe(false);
      expect(document.querySelector('p')).toBeNull();
    });

    it('describes the control by its hint, with ids derived from the given id', () => {
      render(
        <TextField
          id="budget"
          label="Daily budget"
          hint="In the account currency (USD)."
          value=""
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByRole('textbox', { name: 'Daily budget' });
      expect(input.id).toBe('budget');
      expect(input.getAttribute('aria-describedby')).toBe('budget-hint');
      const hint = document.getElementById('budget-hint');
      expect(hint?.tagName).toBe('P');
      expect(hint?.textContent).toBe('In the account currency (USD).');
      expect(input.hasAttribute('aria-invalid')).toBe(false);
    });

    it('marks the field invalid while an error is shown and reads the hint first', () => {
      render(
        <TextField
          id="amount"
          label="Amount"
          hint="Up to two decimals."
          error="Enter an amount"
          value=""
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByRole('textbox', { name: 'Amount' });
      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(input.getAttribute('aria-describedby')).toBe('amount-hint amount-error');
      expect(document.getElementById('amount-error')?.textContent).toBe('Enter an amount');
      expect(screen.getByRole('textbox', { description: 'Up to two decimals. Enter an amount' })).toBe(
        input,
      );
    });

    it('links generated hint and error ids that resolve to their messages', () => {
      render(
        <TextField
          label="Email"
          type="email"
          hint="Use your work email"
          error="Email is required"
          value=""
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByLabelText('Email');
      expect(input.getAttribute('aria-invalid')).toBe('true');
      const ids = (input.getAttribute('aria-describedby') ?? '').split(' ');
      expect(ids).toHaveLength(2);
      const [hintId = '', errorId = ''] = ids;
      expect(hintId).not.toBe(errorId);
      expect(document.getElementById(hintId)?.textContent).toBe('Use your work email');
      expect(document.getElementById(errorId)?.textContent).toBe('Email is required');
    });

    it('clears the invalid state and the message once the error is removed', () => {
      const { rerender } = render(
        <TextField id="amount" label="Amount" error="Enter an amount" value="" onChange={ignoreChange} />,
      );
      expect(screen.getByRole('textbox', { name: 'Amount' }).getAttribute('aria-invalid')).toBe('true');

      rerender(<TextField id="amount" label="Amount" value="12" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Amount' });
      expect(input.hasAttribute('aria-invalid')).toBe(false);
      expect(input.hasAttribute('aria-describedby')).toBe(false);
      expect(document.getElementById('amount-error')).toBeNull();
    });

    it.each([
      ['an empty string', ''],
      ['null', null],
      ['false', false],
      ['true', true],
    ])('treats %s as no hint and no error', (_name, empty) => {
      render(
        <TextField id="f" label="Field" hint={empty} error={empty} value="" onChange={ignoreChange} />,
      );

      const input = screen.getByRole('textbox', { name: 'Field' });
      expect(input.hasAttribute('aria-invalid')).toBe(false);
      expect(input.hasAttribute('aria-describedby')).toBe(false);
      expect(document.getElementById('f-hint')).toBeNull();
      expect(document.getElementById('f-error')).toBeNull();
    });

    it('counts 0 as content for the hint and the error', () => {
      render(<TextField id="f" label="Field" hint={0} error={0} value="" onChange={ignoreChange} />);

      const input = screen.getByRole('textbox', { name: 'Field' });
      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(document.getElementById('f-hint')?.textContent).toBe('0');
      expect(document.getElementById('f-error')?.textContent).toBe('0');
    });

    it('appends extra description ids after the hint and error, each id once', () => {
      render(
        <TextField
          id="f"
          label="Field"
          hint="Hint"
          error="Error"
          aria-describedby="  tip-1 f-hint  tip-2 tip-1 "
          value=""
          onChange={ignoreChange}
        />,
      );

      expect(screen.getByRole('textbox', { name: 'Field' }).getAttribute('aria-describedby')).toBe(
        'f-hint f-error tip-1 tip-2',
      );
    });

    it('uses the extra ids alone when there is no hint or error', () => {
      render(<TextField label="Field" aria-describedby="tip" value="" onChange={ignoreChange} />);

      expect(screen.getByRole('textbox', { name: 'Field' }).getAttribute('aria-describedby')).toBe(
        'tip',
      );
    });

    it('omits aria-describedby when the extra ids are blank', () => {
      render(<TextField label="Field" aria-describedby="   " value="" onChange={ignoreChange} />);

      expect(screen.getByRole('textbox', { name: 'Field' }).hasAttribute('aria-describedby')).toBe(
        false,
      );
    });
  });

  describe('controlled value and change', () => {
    it('shows value and reports each edit as a string without changing itself', () => {
      const onChange = vi.fn<(value: string) => void>();
      render(<TextField label="Email" value="a@example.com" onChange={onChange} />);

      const input = screen.getByRole<HTMLInputElement>('textbox', { name: 'Email' });
      expect(input.value).toBe('a@example.com');

      fireEvent.change(input, { target: { value: 'b@example.com' } });

      expect(onChange).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalledWith('b@example.com');
      // Controlled: the DOM reverts to the value prop until the caller passes the new one.
      expect(input.value).toBe('a@example.com');
    });

    it('shows a new value passed back by the caller', () => {
      const { rerender } = render(<TextField label="Email" value="" onChange={ignoreChange} />);

      rerender(<TextField label="Email" value="c@example.com" onChange={ignoreChange} />);

      expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Email' }).value).toBe(
        'c@example.com',
      );
    });

    it('reports a cleared field as the empty string', () => {
      const onChange = vi.fn<(value: string) => void>();
      render(<TextField label="Email" value="x" onChange={onChange} />);

      fireEvent.change(screen.getByRole('textbox', { name: 'Email' }), { target: { value: '' } });

      expect(onChange).toHaveBeenCalledWith('');
    });

    it('disables the control natively', () => {
      render(<TextField label="Email" disabled value="" onChange={ignoreChange} />);

      expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Email' }).disabled).toBe(true);
    });
  });

  describe('single-line attributes', () => {
    it('passes name, autocomplete and placeholder to the input', () => {
      render(
        <TextField
          label="Email"
          type="email"
          name="email"
          autoComplete="email"
          placeholder="name@example.com"
          value=""
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByRole('textbox', { name: 'Email' });
      expect(input.getAttribute('type')).toBe('email');
      expect(input.getAttribute('name')).toBe('email');
      expect(input.getAttribute('autocomplete')).toBe('email');
      expect(input.getAttribute('placeholder')).toBe('name@example.com');
    });

    it('renders a password input named by its label', () => {
      render(
        <TextField
          label="Password"
          type="password"
          autoComplete="current-password"
          value="secret-value"
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByLabelText<HTMLInputElement>('Password');
      expect(input.type).toBe('password');
      expect(input.value).toBe('secret-value');
      expect(screen.queryByRole('textbox')).toBeNull();
    });

    it('renders a number input with its input mode, step and bounds', () => {
      const onChange = vi.fn<(value: string) => void>();
      render(
        <TextField
          label="Daily budget"
          type="number"
          inputMode="decimal"
          step="0.01"
          min={0}
          max={1000}
          value="12.50"
          onChange={onChange}
        />,
      );

      const input = screen.getByRole<HTMLInputElement>('spinbutton', { name: 'Daily budget' });
      expect(input.type).toBe('number');
      expect(input.getAttribute('inputmode')).toBe('decimal');
      expect(input.getAttribute('step')).toBe('0.01');
      expect(input.getAttribute('min')).toBe('0');
      expect(input.getAttribute('max')).toBe('1000');

      fireEvent.change(input, { target: { value: '13.25' } });
      expect(onChange).toHaveBeenCalledWith('13.25');
    });

    it('renders a date input bounded by ISO dates', () => {
      render(
        <TextField
          label="Start"
          type="date"
          min="2026-01-01"
          max="2026-10-10"
          value="2026-05-01"
          onChange={ignoreChange}
        />,
      );

      const input = screen.getByLabelText<HTMLInputElement>('Start');
      expect(input.type).toBe('date');
      expect(input.value).toBe('2026-05-01');
      expect(input.getAttribute('min')).toBe('2026-01-01');
      expect(input.getAttribute('max')).toBe('2026-10-10');
    });

    it('forwards ref to the input element', () => {
      const ref = createRef<HTMLInputElement>();
      render(<TextField ref={ref} label="Email" value="" onChange={ignoreChange} />);

      expect(ref.current).toBe(screen.getByRole('textbox', { name: 'Email' }));
      expect(ref.current?.tagName).toBe('INPUT');
    });
  });

  describe('multiline', () => {
    it('renders a textarea with three rows by default and the shared wiring', () => {
      const onChange = vi.fn<(value: string) => void>();
      render(
        <TextField
          multiline
          id="note"
          label="Note"
          hint="Optional."
          error="Too long"
          required
          name="note"
          placeholder="Why"
          value="first"
          onChange={onChange}
        />,
      );

      const area = screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Note' });
      expect(area.tagName).toBe('TEXTAREA');
      expect(area.rows).toBe(3);
      expect(area.value).toBe('first');
      expect(area.required).toBe(true);
      expect(area.getAttribute('name')).toBe('note');
      expect(area.getAttribute('placeholder')).toBe('Why');
      expect(area.getAttribute('aria-invalid')).toBe('true');
      expect(area.getAttribute('aria-describedby')).toBe('note-hint note-error');
      expect(area.hasAttribute('type')).toBe(false);

      fireEvent.change(area, { target: { value: 'second' } });
      expect(onChange).toHaveBeenCalledWith('second');
    });

    it.each([
      [4, 4],
      [2.7, 2],
      [1, 1],
      [0, 3],
      [-2, 3],
      [Number.NaN, 3],
      [Number.POSITIVE_INFINITY, 3],
    ])('shows rows=%s as %s visible rows', (rows, expected) => {
      render(<TextField multiline rows={rows} label="Note" value="" onChange={ignoreChange} />);

      expect(screen.getByRole<HTMLTextAreaElement>('textbox', { name: 'Note' }).rows).toBe(expected);
    });

    it('forwards ref to the textarea element and disables it natively', () => {
      const ref = createRef<HTMLTextAreaElement>();
      render(
        <TextField multiline ref={ref} disabled label="Note" value="" onChange={ignoreChange} />,
      );

      expect(ref.current?.tagName).toBe('TEXTAREA');
      expect(ref.current?.disabled).toBe(true);
    });
  });
});
