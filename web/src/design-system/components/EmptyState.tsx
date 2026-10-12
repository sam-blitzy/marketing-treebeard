/**
 * `EmptyState`: the panel a list shows in place of its items when it has none (AAP 0.4.2,
 * "Loading and empty states", status GAP).
 *
 * Each section of `/actions` renders an `EmptyState` with the reason when it has no items, for
 * example "No search term met the K1 minimum of 37 clicks" (AAP 0.5.5), so an empty list always
 * says why it is empty instead of looking like a failed load. The styling lives in
 * `EmptyState.css`, whose class names are the contract this component renders:
 *
 * - `ds-emptystate`: the root, always; it carries the optional `id`;
 * - `ds-emptystate__title`: the title, always, as a paragraph;
 * - `ds-emptystate__reason`: only when a reason (`children`) is given;
 * - `ds-emptystate__action`: only when an `action` is given.
 *
 * Semantics:
 * - The title is a `<p>`, never a heading. Feature code never renders raw headings (AAP 0.4.4),
 *   and an empty state replaces a list inside a section that already has its heading, so it
 *   must not add a level to the document outline.
 * - There is no live-region role. The content is static: it is rendered with the section, not
 *   announced as a change. A view that polls shows its own `Spinner` while it waits.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `EmptyState.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './EmptyState.css';

import type { ReactNode } from 'react';

/**
 * Props of {@link EmptyState}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). Spacing, colour and typography come
 * only from `EmptyState.css` and its tokens.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS empty-state component.
 */
export interface EmptyStateProps {
  /**
   * What is empty, as a short statement such as "No keywords to adjust". Rendered as a
   * paragraph, so it may hold inline content only.
   */
  title: ReactNode;
  /**
   * The reason the list is empty, stated from the run's evidence, for example "No search term
   * met the K1 minimum of 37 clicks". Omitted (or `null`, `false` or an empty string), no
   * reason element is rendered.
   */
  children?: ReactNode;
  /**
   * An optional next step, such as a link to the Sources page or a design-system `Button`.
   * Omitted (or `null`, `false` or an empty string), no action element is rendered.
   */
  action?: ReactNode;
  /** The root element's `id`, for example the target of an in-page anchor link. */
  id?: string;
}

/**
 * Whether a slot holds something React would render.
 *
 * `undefined`, `null`, booleans and the empty string render nothing, so their slot wrapper is
 * omitted rather than left as an empty element that would still take a gap step in the
 * panel's column. Conditional content such as `{withheld > 0 && text}` is therefore safe in
 * either slot.
 *
 * @param node the slot content
 * @returns `true` when the slot has renderable content
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * Empty state with its reason: renders a `div` holding the title paragraph, then the reason
 * and the action when they are given.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <EmptyState title="No keywords to adjust">
 *   No search term met the K1 minimum of 37 clicks.
 * </EmptyState>
 *
 * <EmptyState title="Page not found" action={<Link to="/actions">Go to actions</Link>} />
 * ```
 */
export function EmptyState({ title, children, action, id }: EmptyStateProps) {
  return (
    <div id={id} className="ds-emptystate">
      <p className="ds-emptystate__title">{title}</p>
      {hasContent(children) && <div className="ds-emptystate__reason">{children}</div>}
      {hasContent(action) && <div className="ds-emptystate__action">{action}</div>}
    </div>
  );
}
