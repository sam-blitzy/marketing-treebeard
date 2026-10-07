/**
 * `Stack`: the product's vertical layout primitive (AAP 0.4.2).
 *
 * Feature code spaces and aligns content only through `Stack`, `Inline` and `Grid`, and never
 * writes flex or grid CSS on raw elements of its own (AAP 0.4.4). `Stack` places its children
 * in a single column separated by one step of the spacing token scale. The styling lives in
 * `Stack.css`, whose class names are the contract this component renders:
 *
 * - `ds-stack`: always;
 * - `ds-stack--gap-{1..8}`: always, from the `gap` prop (default `4`), mapping to `--space-{n}`;
 * - `ds-stack--align-{start|center|end|stretch}`: only when `align` is set.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Stack.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Stack.css';

import type { ReactNode } from 'react';

import type { SpaceStep } from '../tokens';

/**
 * Props of {@link Stack}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). Spacing comes only from `gap`, and
 * cross-axis alignment only from `align`.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS vertical layout primitive.
 */
export interface StackProps {
  /**
   * The space between children, as a step of the spacing scale: step `n` resolves to the
   * `--space-n` token. Defaults to `4`.
   */
  gap?: SpaceStep;
  /**
   * Where children sit across the column. Omitted, children stretch to the full inline size of
   * the stack (the flex default); `'stretch'` states that default explicitly.
   */
  align?: 'start' | 'center' | 'end' | 'stretch';
  /**
   * The element rendered. Defaults to `'div'`.
   *
   * - `'ul'` or `'ol'`: a list; every child must be an `<li>` element so the list semantics stay
   *   valid. The user-agent bullets and indentation are removed, so the gap alone spaces items.
   * - `'section'`: a landmark region only when it has an accessible name, so give it an
   *   `aria-label` (or label it by its content); an unnamed section is not exposed as a region.
   */
  as?: 'div' | 'ul' | 'ol' | 'section';
  /** The element's `id`, for example the target of an in-page anchor link. */
  id?: string;
  /** The accessible name of the rendered element, most useful with `as="section"`. */
  'aria-label'?: string;
  /** The stacked content. With `as="ul"` or `as="ol"`, `<li>` elements only. */
  children?: ReactNode;
}

/**
 * Vertical layout primitive: renders one element (`div`, `ul`, `ol` or `section`) that lays its
 * children out in a column, separated by a spacing token.
 *
 * When `as` is `'ul'` or `'ol'`, children must be `<li>` elements. `as="section"` should carry an
 * `aria-label` (or be labelled by its content) to form a landmark region.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Stack gap={6}>
 *   <StatusBar />
 *   <GlobalBanners />
 * </Stack>
 *
 * <Stack as="ul" gap={3} aria-label="Keyword actions">
 *   <li>…</li>
 *   <li>…</li>
 * </Stack>
 *
 * <Stack as="section" id="budget" aria-label="Budget changes" align="start">
 *   …
 * </Stack>
 * ```
 */
export function Stack({
  gap = 4,
  align,
  as = 'div',
  id,
  'aria-label': ariaLabel,
  children,
}: StackProps) {
  const className =
    align === undefined
      ? `ds-stack ds-stack--gap-${gap}`
      : `ds-stack ds-stack--gap-${gap} ds-stack--align-${align}`;
  const Element = as;

  return (
    <Element className={className} id={id} aria-label={ariaLabel}>
      {children}
    </Element>
  );
}
