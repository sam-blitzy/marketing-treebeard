/**
 * `TopBar`: the product's application top bar (AAP 0.4.2 "Page shell and top bar", status GAP).
 *
 * Every screen renders its top bar through this component, passed to `PageShell` (AAP 0.5.5):
 * the product name at the inline start and the session controls, such as the signed-in email
 * and "Sign out", at the inline end. The styling lives in `TopBar.css`, whose class names are
 * the contract this component renders:
 *
 * - `ds-topbar`: the bar, always;
 * - `ds-topbar__product`: the product name, always;
 * - `ds-topbar__actions`: the actions row, only when `children` renders something (not when it
 *   is omitted, `null`, a boolean, an empty string or an array of children none of which
 *   renders; `0` renders).
 *
 * Landmarks and headings: `PageShell` wraps its `topBar` slot in the page's `<header>`, which is
 * the one `banner` landmark, so `TopBar` renders a plain `<div>` and never a second `<header>`.
 * The product name is plain text, not a heading, because each page's single `<h1>` is
 * `PageShell.Title`; a heading here would add a second top-level heading to every page.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step (AAP 0.4.4): it renders semantic HTML styled solely by semantic
 * tokens, and binding Blitzy OS later changes only this file and `TopBar.css`, never the feature
 * code that uses it (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './TopBar.css';

import type { ReactNode } from 'react';

/**
 * Props of {@link TopBar}.
 *
 * The prop set is closed on purpose: there is no `className` or `style`, so a caller cannot
 * restyle the bar or add literal values around it (AAP 0.4.4). Its content is chosen only
 * through the props below.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS top bar or app header.
 */
export interface TopBarProps {
  /**
   * The product name shown at the inline start of the bar, for example `"Ads Action Engine"`.
   * Rendered as plain text, never as a heading: the page's `<h1>` is `PageShell.Title`.
   */
  productName: ReactNode;
  /**
   * Controls shown at the inline end of the bar, for example the signed-in email and a
   * "Sign out" `Button`. They are laid out in a wrapping row, so on a narrow viewport they drop
   * onto their own line. When omitted (or `null`, a boolean, an empty string, or an array of
   * children none of which renders, as several `{signedIn && …}` controls give when signed
   * out), the actions row is not rendered at all; any other value, including `0`, renders it.
   */
  children?: ReactNode;
}

/**
 * Whether a slot value renders anything.
 *
 * React renders nothing for `undefined`, `null`, booleans and the empty string, so conditional
 * controls must not leave an empty container behind: `TopBar.css` spaces the bar's items with a
 * flex gap and wraps them, so an empty actions row would still take a gap step and, on a narrow
 * bar, wrap onto a line of its own and add a row gap below the product name.
 *
 * Several conditional controls, such as
 * `{signedIn && <span>{email}</span>}{signedIn && <Button>Sign out</Button>}`, reach `children`
 * as an array (`[false, false]` when signed out), and React flattens nested arrays when it
 * renders them. An array therefore has content only when at least one of its items, at any
 * depth, has content. Any other value, including `0`, renders and counts; an iterable that is
 * not an array is not walked, because reading a one-shot iterator here would leave nothing for
 * React to render.
 *
 * @param node - the `children` value, or one item of it
 * @returns `true` when the slot has content to render
 */
function hasContent(node: ReactNode): boolean {
  if (Array.isArray(node)) {
    return node.some(hasContent);
  }
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * Application top bar: the product name followed, when given, by a row of session controls.
 *
 * Renders a plain `<div>` because `PageShell` supplies the enclosing `<header>` (the `banner`
 * landmark); place `TopBar` only in `PageShell`'s `topBar` slot. The component holds no state
 * and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <PageShell
 *   topBar={
 *     <TopBar productName="Ads Action Engine">
 *       <span>admin@example.com</span>
 *       <Button variant="quiet" onClick={signOut}>Sign out</Button>
 *     </TopBar>
 *   }
 *   sideNav={<SideNav … />}
 * >
 *   <PageShell.Title>Actions</PageShell.Title>
 * </PageShell>
 * ```
 *
 * @param props - see {@link TopBarProps}
 * @returns a `div.ds-topbar` holding the product name and, when `children` renders something
 *   (anything but `undefined`, `null`, a boolean, an empty string or an array of children none
 *   of which renders), a `div.ds-topbar__actions` holding the children
 */
export function TopBar({ productName, children }: TopBarProps) {
  return (
    <div className="ds-topbar">
      <span className="ds-topbar__product">{productName}</span>
      {hasContent(children) && <div className="ds-topbar__actions">{children}</div>}
    </div>
  );
}
