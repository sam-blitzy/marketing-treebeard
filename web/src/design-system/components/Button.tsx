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
 *   while `loading`, and hidden from the accessibility tree.
 *
 * States are carried by native attributes, which `Button.css` styles: `disabled` (the
 * `disabled` prop, and always while loading), `aria-busy="true"` (while loading) and
 * `aria-pressed` (passed through by toggle callers such as the DateRangePicker presets).
 *
 * Accessibility (WCAG 2.2 AA, AAP 0.1.1):
 * - It is a native `<button>`, so it is focusable and operable with Enter and Space with no
 *   script, and `Button.css` draws a `:focus-visible` ring that is never removed.
 * - `type` defaults to `"button"`, so a Button placed inside a form never submits it by accident;
 *   a form's submit control passes `type="submit"` explicitly.
 * - While loading, the button's accessible name stays its own label: the Spinner slot is
 *   `aria-hidden`, so the Spinner's "Loading" label is never appended to the name, and the busy
 *   state is conveyed by `aria-busy="true"` instead. Queries such as
 *   `getByRole('button', { name: 'Sign in' })` therefore match in both states.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Button.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Button.css';

import type { ComponentPropsWithRef } from 'react';

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
 * rows such as card footers and window presets. Both meet the WCAG 2.2 SC 2.5.8 minimum target
 * size of 24 by 24 CSS pixels (see `Button.css`).
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
   * Whether the action is in progress, for example while its request is pending. A loading
   * button is disabled, so it fires no click, carries `aria-busy="true"` and shows a Spinner
   * after its label; its accessible name stays the label. Defaults to `false`.
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
 * modifiers, with the children in the label slot and, while loading, a Spinner after them.
 *
 * The button is disabled when `disabled` is `true` or while `loading`. Native `disabled` keeps
 * the click from firing and takes the button out of the tab order until it is enabled again.
 * The attributes this component owns (`type`, `className`, `disabled`, `aria-busy` and
 * `dangerouslySetInnerHTML`) are applied after the forwarded props, so they always reflect
 * `type`, `variant`, `size`, `disabled` and `loading`. `dangerouslySetInnerHTML` is always
 * unset: the content goes only through the escaped children in the label slot, so a
 * `dangerouslySetInnerHTML` that reaches the props untyped (from JavaScript, or a spread of an
 * untyped object) is discarded instead of colliding with the label.
 *
 * The component holds no state and has no effects; its output depends only on its props.
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
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      className={`ds-button ds-button--${variant} ds-button--${size}`}
      disabled={disabled === true || loading}
      aria-busy={loading ? true : undefined}
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
