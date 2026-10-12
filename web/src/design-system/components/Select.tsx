/**
 * `Select`: the product's labelled single-choice list (AAP 0.4.2 "Form controls", status GAP).
 *
 * Feature code never renders a raw `<select>` (AAP 0.4.4). The decision form's status choice
 * (for example `active` or `paused`, AAP 0.5.5) and any other fixed list of choices render
 * through this wrapper, which owns the accessible wiring once, exactly as `TextField` does:
 *
 * - a visible `<label for>` that is also the control's accessible name (WCAG 2.2 SC 1.3.1,
 *   3.3.2, 4.1.2), so end-to-end tests and assistive technology find the list by its label;
 * - an optional hint and an optional error message, each a `<p>` with its own id referenced by
 *   the control's `aria-describedby`, hint first;
 * - `aria-invalid="true"` only while an error message is shown, so the critical border in
 *   `TextField.css` is never the only cue and the error is identified in words (SC 3.3.1);
 * - the native `required` attribute, which assistive technology announces, plus a visible
 *   asterisk marked `aria-hidden` so the name stays the label text alone.
 *
 * The control is the native `<select>`, not a custom listbox: the browser supplies keyboard
 * operation (arrow keys, type-ahead, Space/Enter or Alt+Down to open), the platform picker on
 * touch devices and the screen-reader semantics, so no headless library is needed (AAP 0.4.5).
 *
 * Values are typed: `Select<'active' | 'paused'>` accepts only options of that union and calls
 * `onChange` with a member of it. The DOM hands back a plain string, so the change handler maps
 * it to the option that carries it rather than asserting its type; a string no option carries
 * (which only a script could produce) is ignored instead of leaking into the caller's state.
 *
 * The rendered markup is the contract of `TextField.css` (shared with `TextField`) and
 * `Select.css`, which is imported second so its end padding overrides the shared control's:
 *
 * ```text
 * div.ds-field
 *   label.ds-field__label[for]
 *     span.ds-field__required[aria-hidden]   only when required
 *   select.ds-field__control.ds-select
 *     option[value]                          one per option, in the given order
 *   p.ds-field__hint[id]                     only when a hint is given
 *   p.ds-field__error[id]                    only when an error is given
 * ```
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * placeholder-text `opacity: 1` reset in the shared `TextField.css` that `../GAPS.md` lists, and
 * binding Blitzy OS later changes only this file, `Select.css` and the shared `TextField.css`
 * (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './TextField.css';
import './Select.css';

import { useId, type ChangeEvent, type ReactNode, type Ref } from 'react';

import { hasContent, mergeIdRefs } from './a11y';

/**
 * One choice in a {@link Select}.
 *
 * GAP: bind to Blitzy OS on source arrival. This is a product-level shape; on binding, it maps
 * onto the Blitzy OS select option.
 *
 * @typeParam V - The string union of the select's values, for example `'active' | 'paused'`.
 */
export interface SelectOption<V extends string = string> {
  /**
   * The value submitted and passed to `onChange` when this option is chosen. Values must be
   * unique within one select: the value is the option's React key and the only way the change
   * handler tells options apart.
   */
  value: V;
  /** Visible option text, which is also what assistive technology announces for the option. */
  label: string;
  /** Sets the native `disabled` attribute: the option is shown but cannot be chosen. */
  disabled?: boolean;
}

/**
 * Props of {@link Select}.
 *
 * The prop set is closed on purpose: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4), and no rest spread, so nothing unlisted reaches
 * the DOM.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS select.
 *
 * @typeParam V - The string union of the select's values, inferred from `options` and `value`.
 */
export interface SelectProps<V extends string = string> {
  /**
   * Visible label text. It is rendered in a `<label for>` tied to the control and is the
   * control's accessible name, so it must describe the choice on its own (for example "Status").
   */
  label: ReactNode;
  /**
   * The choices, rendered as `<option>` elements in this order. Each `value` must be unique.
   * An empty list renders an empty select, which can never call `onChange`.
   */
  options: readonly SelectOption<V>[];
  /**
   * The chosen value. The select is always controlled, so `value` should be one of the
   * options' values: when it matches none, the browser shows its first option while the caller
   * still holds `value`, so add an option for every value the caller can hold.
   */
  value: V;
  /**
   * Called with the newly chosen option's value. The select holds no state of its own, so the
   * caller stores the value and passes it back through `value`.
   */
  onChange: (value: V) => void;
  /**
   * Supporting text under the control, referenced by `aria-describedby`. Rendered only when it
   * has content: `undefined`, `null`, booleans and `''` render nothing.
   */
  hint?: ReactNode;
  /**
   * Error message under the control. While it has content the control carries
   * `aria-invalid="true"` and references the message by `aria-describedby`; `undefined`,
   * `null`, booleans and `''` mean the choice is valid.
   */
  error?: ReactNode;
  /** Sets the native `disabled` attribute, which also removes the control from the tab order. */
  disabled?: boolean;
  /**
   * Sets the native `required` attribute and shows an `aria-hidden` asterisk on the label.
   *
   * The accessible name stays the label text alone, because the computed name skips
   * `aria-hidden` content (Playwright's `getByLabel('Status')` and Testing Library's
   * `getByRole('combobox', { name: 'Status' })`). Testing Library's `getByLabelText` matches
   * the label's raw text instead, which then reads "Status *", so query a required select by
   * role.
   */
  required?: boolean;
  /** The control's `name`, for form submission. */
  name?: string;
  /**
   * The control's `id`. Omitted, a stable id is generated with `useId`. The hint and error ids
   * derive from it as `{id}-hint` and `{id}-error`.
   */
  id?: string;
  /** Ref to the rendered `<select>` element (React 19 passes `ref` as a regular prop). */
  ref?: Ref<HTMLSelectElement>;
  /**
   * Extra description ids for the control, such as the id a wrapping `Tooltip` passes. They are
   * appended to the control's `aria-describedby` after the hint and error ids, each id once.
   */
  'aria-describedby'?: string;
}

/**
 * A labelled, controlled native select. It renders the label, the `<select>` with one
 * `<option>` per entry of `options`, and the optional hint and error, wired together by ids so
 * the label names the control and the hint and error describe it.
 *
 * The component holds no state and has no effects; its output depends only on its props and
 * the id from `useId`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * type Status = 'active' | 'paused';
 *
 * const STATUS_OPTIONS: readonly SelectOption<Status>[] = [
 *   { value: 'active', label: 'Active' },
 *   { value: 'paused', label: 'Paused' },
 * ];
 *
 * <Select
 *   label="Status"
 *   name="status"
 *   options={STATUS_OPTIONS}
 *   value={status}
 *   onChange={setStatus}
 *   hint="The status you set in the ad platform."
 *   error={statusError}
 * />
 * ```
 */
export function Select<V extends string = string>(props: SelectProps<V>) {
  const {
    label,
    options,
    value,
    onChange,
    hint,
    error,
    disabled,
    required,
    name,
    id,
    ref,
    'aria-describedby': extraDescribedBy,
  } = props;

  const autoId = useId();
  const selectId = id ?? `${autoId}-select`;
  const hintId = `${selectId}-hint`;
  const errorId = `${selectId}-error`;

  // The same content rule and description order as TextField, from ./a11y: a hint or error
  // with no content renders nothing, neither an empty <p> nor an invalid select. Extra ids,
  // such as a Tooltip's, follow.
  const showHint = hasContent(hint);
  const showError = hasContent(error);
  const describedBy = mergeIdRefs(
    showHint ? hintId : undefined,
    showError ? errorId : undefined,
    extraDescribedBy,
  );

  // The DOM reports the chosen value as a plain string. Mapping it back to the option that
  // carries it yields a typed V without an assertion; a string no option carries is ignored.
  const handleChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const raw = event.currentTarget.value;
    const match = options.find((option) => option.value === raw);
    if (match !== undefined) {
      onChange(match.value);
    }
  };

  return (
    <div className="ds-field">
      <label className="ds-field__label" htmlFor={selectId}>
        {label}
        {required === true ? (
          <span className="ds-field__required" aria-hidden="true">
            {' *'}
          </span>
        ) : null}
      </label>
      <select
        ref={ref}
        className="ds-field__control ds-select"
        id={selectId}
        name={name}
        value={value}
        onChange={handleChange}
        required={required}
        disabled={disabled}
        aria-invalid={showError ? true : undefined}
        aria-describedby={describedBy}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value} disabled={option.disabled}>
            {option.label}
          </option>
        ))}
      </select>
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
