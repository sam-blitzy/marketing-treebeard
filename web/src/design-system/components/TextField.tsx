/**
 * `TextField`: the product's labelled text input and multiline text area (AAP 0.4.2, status GAP).
 *
 * Feature code never renders a raw `<input>` or `<textarea>` (AAP 0.4.4). The login form, the
 * decision form's amounts and dates, and the free-text `note` on decisions and resolutions
 * (AAP 0.5.3, 0.5.5) all render through this wrapper, which owns the accessible wiring once:
 *
 * - a visible `<label for>` that is also the control's accessible name (WCAG 2.2 SC 1.3.1,
 *   3.3.2, 4.1.2), so end-to-end tests and assistive technology find a field by its label text;
 * - an optional hint and an optional error message, each a `<p>` with its own id referenced by
 *   the control's `aria-describedby`, hint first;
 * - `aria-invalid="true"` only while an error message is shown, so the critical border in
 *   `TextField.css` is never the only cue and the error is identified in words (SC 3.3.1);
 * - the native `required` attribute, which assistive technology announces, plus a visible
 *   asterisk marked `aria-hidden` so the name stays the label text alone.
 *
 * The error is static text, not a live region: a form announces its errors through focus and
 * reading order on submit, and a `role="alert"` on every field would be noisy.
 *
 * The rendered markup is the contract of `TextField.css` (shared with `Select`):
 *
 * ```text
 * div.ds-field
 *   label.ds-field__label[for]
 *     span.ds-field__required[aria-hidden]   only when required
 *   input.ds-field__control                  or textarea.ds-field__control.ds-textfield--multiline
 *   p.ds-field__hint[id]                     only when a hint is given
 *   p.ds-field__error[id]                    only when an error is given
 * ```
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * placeholder-text `opacity: 1` reset that `../GAPS.md` lists, and binding Blitzy OS later
 * changes only this file and `TextField.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './TextField.css';

import { useId, type ChangeEvent, type HTMLAttributes, type ReactNode, type Ref } from 'react';

import { hasContent, mergeIdRefs } from './a11y';

/**
 * The single-line input types `TextField` supports: free text, an email address, a password,
 * a number (budget amounts in the decision form) and a calendar date (custom window bounds).
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level values; on binding, they
 * map onto the Blitzy OS text-input variants.
 */
export type TextFieldType = 'text' | 'email' | 'password' | 'number' | 'date';

/**
 * Props shared by the single-line and multiline forms of {@link TextField}.
 *
 * The prop set is closed on purpose: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4), and no rest spread, so nothing unlisted reaches
 * the DOM.
 */
interface TextFieldBaseProps {
  /**
   * Visible label text. It is rendered in a `<label for>` tied to the control and is the
   * control's accessible name, so it must describe the field on its own (for example "Email").
   */
  label: ReactNode;
  /** The current value. The field is always controlled; an empty field is `''`. */
  value: string;
  /**
   * Called with the control's new string value on every change. The field holds no state of
   * its own, so the caller stores the value and passes it back through `value`.
   */
  onChange: (value: string) => void;
  /**
   * Supporting text under the control, referenced by `aria-describedby`. Rendered only when it
   * has content: `undefined`, `null`, booleans and `''` render nothing.
   */
  hint?: ReactNode;
  /**
   * Error message under the control. While it has content the control carries
   * `aria-invalid="true"` and references the message by `aria-describedby`; `undefined`,
   * `null`, booleans and `''` mean the field is valid.
   */
  error?: ReactNode;
  /**
   * Sets the native `required` attribute and shows an `aria-hidden` asterisk on the label.
   *
   * The accessible name stays the label text alone, because the computed name skips
   * `aria-hidden` content (Playwright's `getByLabel('Email')` and Testing Library's
   * `getByRole('textbox', { name: 'Email' })`). Testing Library's `getByLabelText` matches the
   * label's raw text instead, which then reads "Email *", so query a required field by role.
   */
  required?: boolean;
  /** Sets the native `disabled` attribute, which also removes the control from the tab order. */
  disabled?: boolean;
  /** The control's `name`, for form submission and password managers. */
  name?: string;
  /**
   * The control's `id`. Omitted, a stable id is generated with `useId`. The hint and error ids
   * derive from it as `{id}-hint` and `{id}-error`.
   */
  id?: string;
  /** The control's `autocomplete` token, for example `'email'` or `'current-password'`. */
  autoComplete?: string;
  /** Example input shown while the field is empty. It never replaces the visible label. */
  placeholder?: string;
  /**
   * Extra description ids for the control, such as the id a wrapping `Tooltip` passes. They are
   * appended to the control's `aria-describedby` after the hint and error ids, each id once.
   */
  'aria-describedby'?: string;
}

/**
 * Props of the single-line {@link TextField}: an `<input>`.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS text input.
 */
export interface TextFieldInputProps extends TextFieldBaseProps {
  /** Absent or `false` for the single-line form. */
  multiline?: false;
  /** The input type. Defaults to `'text'`. */
  type?: TextFieldType;
  /** The virtual-keyboard hint, for example `'decimal'` for a budget amount. */
  inputMode?: HTMLAttributes<HTMLInputElement>['inputMode'];
  /** Granularity of a `number` or `date` input, for example `'0.01'` for a currency amount. */
  step?: number | string;
  /** Lower bound of a `number` or `date` input; a date is an ISO `YYYY-MM-DD` string. */
  min?: number | string;
  /** Upper bound of a `number` or `date` input; a date is an ISO `YYYY-MM-DD` string. */
  max?: number | string;
  /** Ref to the rendered `<input>` element (React 19 passes `ref` as a regular prop). */
  ref?: Ref<HTMLInputElement>;
  /**
   * Not accepted on a single-line field. Without a `multiline` discriminant, TypeScript would
   * otherwise accept the multiline-only `rows` here through the union and drop it silently;
   * `never` turns that mistake into a compile error. Pass `multiline` to use `rows`.
   */
  rows?: never;
}

/**
 * Props of the multiline {@link TextField}: a vertically resizable `<textarea>`, for example
 * the optional note on a decision.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS text area.
 */
export interface TextFieldMultilineProps extends TextFieldBaseProps {
  /** `true` selects the multiline form. */
  multiline: true;
  /**
   * Visible text rows. Defaults to `3`; a value that is not a finite number of at least 1
   * falls back to that default, and a fractional value is rounded down.
   */
  rows?: number;
  /** Ref to the rendered `<textarea>` element (React 19 passes `ref` as a regular prop). */
  ref?: Ref<HTMLTextAreaElement>;
}

/**
 * Props of {@link TextField}, discriminated by `multiline`.
 *
 * A union, rather than one interface, keeps each branch's `ref` typed to its own element:
 * `Ref<HTMLInputElement | HTMLTextAreaElement>` is assignable to neither element's `ref`, and
 * the input-only props (`type`, `inputMode`, `step`, `min`, `max`) are rejected on a multiline
 * field at compile time.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS text input and text area.
 */
export type TextFieldProps = TextFieldInputProps | TextFieldMultilineProps;

/** Rows a multiline field shows when `rows` is omitted or unusable. */
const DEFAULT_ROWS = 3;

/** A positive whole number of rows, or {@link DEFAULT_ROWS} when `rows` is unusable. */
function normaliseRows(rows: number | undefined): number {
  if (rows === undefined || !Number.isFinite(rows) || rows < 1) {
    return DEFAULT_ROWS;
  }
  return Math.floor(rows);
}

/**
 * A labelled, controlled text field: an `<input>` by default, or a `<textarea>` with
 * `multiline`. It renders the label, the control, and the optional hint and error, wired
 * together by ids so the label names the control and the hint and error describe it.
 *
 * The component holds no state and has no effects; its output depends only on its props and
 * the id from `useId`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <TextField
 *   label="Email"
 *   type="email"
 *   name="email"
 *   autoComplete="email"
 *   required
 *   value={email}
 *   onChange={setEmail}
 * />
 *
 * <TextField
 *   label="Daily budget"
 *   type="number"
 *   inputMode="decimal"
 *   step="0.01"
 *   min={0}
 *   hint="In the account currency (USD)."
 *   error={amountError}
 *   value={amount}
 *   onChange={setAmount}
 * />
 *
 * <TextField multiline rows={4} label="Note" value={note} onChange={setNote} />
 * ```
 */
export function TextField(props: TextFieldProps) {
  const {
    label,
    value,
    onChange,
    hint,
    error,
    required,
    disabled,
    name,
    id,
    autoComplete,
    placeholder,
    'aria-describedby': extraDescribedBy,
  } = props;

  const autoId = useId();
  const inputId = id ?? `${autoId}-input`;
  const hintId = `${inputId}-hint`;
  const errorId = `${inputId}-error`;

  // A hint or error with no content renders nothing (see hasContent in ./a11y), so it neither
  // produces an empty <p> nor marks the field invalid. Extra ids, such as a Tooltip's, follow.
  const showHint = hasContent(hint);
  const showError = hasContent(error);
  const describedBy = mergeIdRefs(
    showHint ? hintId : undefined,
    showError ? errorId : undefined,
    extraDescribedBy,
  );

  // One handler serves both elements: a change event of either control is assignable to it.
  const handleChange = (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    onChange(event.currentTarget.value);
  };

  // The attributes both controls share. Every key is listed here, so no prop outside this set
  // (multiline, rows, label, hint, error, onChange) can reach the DOM element.
  const sharedAttributes = {
    id: inputId,
    name,
    value,
    onChange: handleChange,
    required,
    disabled,
    autoComplete,
    placeholder,
    'aria-invalid': showError ? true : undefined,
    'aria-describedby': describedBy,
  };

  let control: ReactNode;
  if (props.multiline === true) {
    const { rows, ref } = props;
    control = (
      <textarea
        {...sharedAttributes}
        ref={ref}
        className="ds-field__control ds-textfield--multiline"
        rows={normaliseRows(rows)}
      />
    );
  } else {
    const { type = 'text', inputMode, step, min, max, ref } = props;
    control = (
      <input
        {...sharedAttributes}
        ref={ref}
        className="ds-field__control"
        type={type}
        inputMode={inputMode}
        step={step}
        min={min}
        max={max}
      />
    );
  }

  return (
    <div className="ds-field">
      <label className="ds-field__label" htmlFor={inputId}>
        {label}
        {required === true ? (
          <span className="ds-field__required" aria-hidden="true">
            {' *'}
          </span>
        ) : null}
      </label>
      {control}
      {showHint ? (
        <p className="ds-field__hint" id={hintId}>
          {hint}
        </p>
      ) : null}
      {showError ? (
        <p className="ds-field__error" id={errorId}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
