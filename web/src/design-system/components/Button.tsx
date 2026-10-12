/**
 * `Button`: the product's action button (AAP 0.4.2, "Primary, secondary and quiet actions").
 *
 * Serves every control that performs an action: Record decision, Mark done, Dismiss, Snooze,
 * Sync now, the discrepancy resolutions, form submits and the DateRangePicker presets
 * (AAP 0.5.5). Feature code never renders a raw `<button>`; it renders this wrapper, imported
 * through the design-system index (AAP 0.4.4, enforced by `web/eslint.config.js`).
 *
 * The styling lives in `Button.css`, whose class names are the contract this component renders:
 *
 * - `ds-button`: always;
 * - `ds-button--{primary|secondary|quiet}`: always exactly one, from `variant` (default
 *   `secondary`);
 * - `ds-button--{sm|md}`: always exactly one, from `size` (default `md`);
 * - `ds-button__label`: the span around the children, always rendered;
 * - `ds-button__spinner`: the span around the {@link Spinner}, rendered after the label only
 *   while `loading`, and hidden from the accessibility tree. `Button.css` lays it over the
 *   label, which it hides visually but keeps as the accessible name, so loading never changes
 *   the button's size or moves its neighbours.
 *
 * States are carried by attributes, which `Button.css` styles: native `disabled` (the
 * `disabled` prop on a button that is neither loading nor focused), `aria-disabled="true"` (a
 * loading button, and a disabled one that still holds focus), `aria-busy="true"` (while
 * loading) and `aria-pressed` (passed through by toggle callers such as the DateRangePicker
 * presets).
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1):
 * - It is a native `<button>`, so it is focusable and operable with Enter and Space with no
 *   script, and `Button.css` draws a `:focus-visible` ring that is never removed.
 * - `type` defaults to `"button"`, so a Button placed inside a form never submits it by accident;
 *   a form's submit control passes `type="submit"` explicitly.
 * - Keyboard focus is never dropped by a state change (SC 2.4.3): a browser moves focus off a
 *   button that becomes natively disabled, to the page body, so a button that holds focus when
 *   it starts loading or becomes disabled keeps it, inert and marked `aria-disabled="true"`.
 *   A loading button is never natively disabled; a disabled one is, once focus leaves it.
 * - While loading, the button's accessible name stays its own label: the Spinner slot is
 *   `aria-hidden`, so the Spinner's "Loading" label is never appended to the name, and the busy
 *   state is conveyed by `aria-busy="true"` instead. Queries such as
 *   `getByRole('button', { name: 'Sign in' })` therefore match in both states.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * `150ms ease-out` shadow transition that `../GAPS.md` lists, and binding Blitzy OS later
 * changes only this file and `Button.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Button.css';

import { useState } from 'react';
import type { ComponentPropsWithRef, FocusEvent, MouseEvent } from 'react';

import { Spinner } from './Spinner';

/**
 * The visual weight of a {@link Button}, ordered from most to least emphasis:
 *
 * - `primary`: the one main action of a card, form or dialog, such as "Record decision";
 * - `secondary`: an ordinary action, such as "Mark done" or "Sync now" (the default);
 * - `quiet`: a low-emphasis action beside others, such as "Dismiss" or "Snooze for 7 days".
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level variant names, not Blitzy
 * OS ones; on binding, each maps onto the matching Blitzy OS button variant.
 */
export type ButtonVariant = 'primary' | 'secondary' | 'quiet';

/**
 * The size of a {@link Button}: `md` (the default) for page and form actions, `sm` for compact
 * rows such as card footers and window presets. `sm` has a smaller label and padding, but both
 * sizes keep a target of at least 44 by 44 CSS pixels (WCAG 2.2 SC 2.5.5, beyond the SC 2.5.8
 * minimum of 24 by 24; see `Button.css`).
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level sizes, not Blitzy OS ones;
 * on binding, each maps onto the matching Blitzy OS button size.
 */
export type ButtonSize = 'sm' | 'md';

/**
 * Props of {@link Button}.
 *
 * Every native `<button>` attribute is accepted and forwarded to the element (`onClick`,
 * `disabled`, `id`, `name`, `value`, `form`, `children`, every `aria-*` attribute such as
 * `aria-pressed`, `aria-expanded`, `aria-controls`, `aria-describedby` and `aria-label`, and
 * `ref`, which React 19 passes as a regular prop), except three:
 *
 * - there is no `className` or `style` prop, because feature code must not restyle a wrapper
 *   (AAP 0.4.4). Appearance comes only from `variant` and `size`;
 * - there is no `dangerouslySetInnerHTML` prop, because the content always goes through
 *   `children`, which React escapes, into the label slot. React rejects an element given both,
 *   and raw HTML would bypass that escaping.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS button.
 */
export interface ButtonProps extends Omit<
  ComponentPropsWithRef<'button'>,
  'className' | 'style' | 'type' | 'dangerouslySetInnerHTML'
> {
  /** The visual weight. Defaults to `'secondary'`. */
  variant?: ButtonVariant;
  /** The size. Defaults to `'md'`. */
  size?: ButtonSize;
  /**
   * Whether the action is unavailable. A disabled button fires no click, submits no form and
   * is natively `disabled`, out of the tab order; while it holds keyboard focus it is instead
   * marked `aria-disabled="true"` and stays focusable, so the browser does not drop focus to
   * the page, until focus leaves it. Defaults to `false`.
   */
  disabled?: boolean;
  /**
   * Whether the action is in progress, for example while its request is pending. A loading
   * button is inert, so it fires no click and submits no form, carries `aria-busy="true"` and
   * `aria-disabled="true"` and shows a Spinner in place of its label, at the same size; its
   * accessible name stays the label. It is never natively disabled, so it keeps keyboard focus
   * for the whole request and stays in the tab order. It wins over `disabled`. Defaults to
   * `false`.
   */
  loading?: boolean;
  /**
   * The native button type. Defaults to `'button'`, so the button never submits a surrounding
   * form unless the caller asks for `'submit'`.
   */
  type?: 'button' | 'submit' | 'reset';
}

/**
 * Action button: renders one native `<button>` carrying `ds-button` and its variant and size
 * modifiers, with the children in the label slot and, while loading, a Spinner laid over them.
 *
 * The button is inactive when `disabled` is `true` or while `loading`, in one of two forms:
 *
 * - natively `disabled`, which keeps the click from firing and takes the button out of the tab
 *   order until it is enabled again: a disabled button that is neither loading nor focused,
 *   such as every footer action of a historical card, disabled from its first render;
 * - `aria-disabled="true"`, which keeps the button focusable: a loading button, and a disabled
 *   one that held keyboard focus when it became disabled, until focus leaves it. A browser
 *   moves focus off a button that becomes natively disabled, to the page body, which would
 *   strand keyboard and screen-reader users for the whole request (WCAG 2.2 SC 2.4.3). The
 *   click is still delivered, so the component cancels it (`preventDefault`, which also stops
 *   a `type="submit"` button, and a form's implicit submission through it, from submitting)
 *   and never calls the caller's `onClick`.
 *
 * The attributes this component owns (`type`, `className`, `disabled`, `aria-disabled`,
 * `aria-busy`, `onClick`, `onFocus`, `onBlur` and `dangerouslySetInnerHTML`) are applied after
 * the forwarded props, so they always reflect `type`, `variant`, `size`, `disabled`, `loading`
 * and focus; the caller's `onClick` runs only while the button is active, and its `onFocus` and
 * `onBlur` always run. `dangerouslySetInnerHTML` is always unset: the content goes only through
 * the escaped children in the label slot, so a `dangerouslySetInnerHTML` that reaches the props
 * untyped (from JavaScript, or a spread of an untyped object) is discarded instead of colliding
 * with the label.
 *
 * The component's one piece of state is whether the button holds focus, which its own
 * `focus` and `blur` events keep; it has no effects, and `ref` is forwarded unchanged.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Button variant="primary" onClick={openDecisionForm}>Record decision</Button>
 *
 * <Button variant="quiet" size="sm" onClick={dismiss}>Dismiss</Button>
 *
 * <Button variant="secondary" loading={requestSync.isPending} onClick={() => requestSync.mutate()}>
 *   Sync now
 * </Button>
 *
 * <Button type="submit" variant="primary" loading={login.isPending}>Sign in</Button>
 *
 * <Button size="sm" aria-pressed={preset === selected} onClick={() => choose(preset)}>
 *   28 days
 * </Button>
 * ```
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  type = 'button',
  disabled,
  onClick,
  onFocus,
  onBlur,
  children,
  ...rest
}: ButtonProps) {
  // Whether the button holds keyboard focus, so a button disabled while focused keeps focus.
  const [focused, setFocused] = useState(false);
  const inactive = loading || disabled === true;
  // Native disabled only where it cannot take focus away: never while loading or focused.
  const nativelyDisabled = disabled === true && !loading && !focused;

  const handleClick = (event: MouseEvent<HTMLButtonElement>) => {
    if (inactive) {
      // A focusable inactive button still receives the click (pointer, Enter, Space, or a
      // form's implicit submission): cancelling it stops a submit, and the action never runs.
      event.preventDefault();
      return;
    }
    onClick?.(event);
  };

  const handleFocus = (event: FocusEvent<HTMLButtonElement>) => {
    setFocused(true);
    onFocus?.(event);
  };

  const handleBlur = (event: FocusEvent<HTMLButtonElement>) => {
    setFocused(false);
    onBlur?.(event);
  };

  return (
    <button
      {...rest}
      type={type}
      className={`ds-button ds-button--${variant} ds-button--${size}`}
      disabled={nativelyDisabled}
      aria-disabled={inactive && !nativelyDisabled ? true : undefined}
      aria-busy={loading ? true : undefined}
      onClick={handleClick}
      onFocus={handleFocus}
      onBlur={handleBlur}
      dangerouslySetInnerHTML={undefined}
    >
      <span className="ds-button__label">{children}</span>
      {loading ? (
        <span className="ds-button__spinner" aria-hidden="true">
          <Spinner label="Loading" />
        </span>
      ) : null}
    </button>
  );
}
