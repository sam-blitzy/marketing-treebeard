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
 * - `ds-stack--align-{start|center|end|stretch}`: only when `align` is set;
 * - `ds-stack--break-long-words`: only when `breakLongWords` is `true`.
 *
 * `Stack.css` also takes an empty `role="status"` child out of the column, so a polite live
 * region rendered ahead of its first message adds no space and no gap (see {@link Stack}).
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
 * feature code must not restyle a wrapper (AAP 0.4.4). Spacing comes only from `gap`,
 * cross-axis alignment only from `align`, and long-word wrapping only from `breakLongWords`.
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
   * Whether long unbroken text, such as URNs, search terms, dataset names and threshold keys,
   * may break at any point, so it wraps inside the column instead of widening the layout past a
   * narrow viewport (WCAG 2.2 SC 1.4.10 Reflow). The treatment is inherited by every descendant,
   * including a nested `Inline` row, whose items then shrink to fit their line. Defaults to
   * `false`. Leave it off on a stack that holds a `DataTable`, whose cells keep their own sizing.
   */
  breakLongWords?: boolean;
  /**
   * The element rendered. Defaults to `'div'`.
   *
   * - `'ul'` or `'ol'`: a list; every child must be an `<li>` element so the list semantics stay
   *   valid. The user-agent bullets and indentation are removed, so the gap alone spaces items.
   *   The list carries `role="list"`, because `Stack.css` removes list markers and Safari's
   *   VoiceOver stops announcing an unmarked list as a list without the explicit role.
   * - `'section'`: a landmark region only when it has an accessible name, so give it an
   *   `aria-label` (or label it by its content); an unnamed section is not exposed as a region.
   *   No explicit role is set, so the section keeps its native semantics.
   */
  as?: 'div' | 'ul' | 'ol' | 'section';
  /** The element's `id`, for example the target of an in-page anchor link. */
  id?: string;
  /**
   * The accessible name of the rendered element. On a list it names the list, and on a section it
   * makes the section a `region` landmark. On a `div` the stack is exposed as a labelled `group`,
   * because ARIA does not allow a name on an element with no role and assistive technology would
   * never announce it. An empty label is treated as no label.
   */
  'aria-label'?: string;
  /**
   * The stacked content. With `as="ul"` or `as="ol"`, `<li>` elements only. A direct child with
   * `role="status"` and no child nodes adds no space or gap until it has content.
   */
  children?: ReactNode;
}

/**
 * Vertical layout primitive: renders one element (`div`, `ul`, `ol` or `section`) that lays its
 * children out in a column, separated by a spacing token.
 *
 * When `as` is `'ul'` or `'ol'`, children must be `<li>` elements, and the list carries
 * `role="list"` so it stays a list under the marker reset. `as="section"` should carry an
 * `aria-label` (or be labelled by its content) to form a landmark region. A `div` given a
 * non-empty `aria-label` carries `role="group"`, so its name is exposed; without one it has no
 * role. `breakLongWords` lets long unbroken text anywhere inside the stack break so it wraps
 * within the column.
 *
 * Empty live regions: a direct child with `role="status"` and no child nodes adds no space or
 * gap until it has content. A polite status region can therefore be rendered ahead of its
 * first message, as WCAG 2.2 SC 4.1.3 needs for that message to be announced, without leaving a
 * blank gap in the column. Render nothing inside it, not even whitespace, while there is no
 * message; it stays in the accessibility tree and takes its place in the column, spaced by
 * `gap`, as soon as it has content.
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
  breakLongWords = false,
  as = 'div',
  id,
  'aria-label': ariaLabel,
  children,
}: StackProps) {
  const layoutClassName =
    align === undefined
      ? `ds-stack ds-stack--gap-${gap}`
      : `ds-stack ds-stack--gap-${gap} ds-stack--align-${align}`;
  const className = breakLongWords
    ? `${layoutClassName} ds-stack--break-long-words`
    : layoutClassName;
  const Element = as;

  // Lists keep their list role under Stack.css's marker reset; a labelled div becomes a group so
  // its name is exposed; a section keeps its native role (see StackProps.as and
  // StackProps['aria-label']).
  const role =
    as === 'ul' || as === 'ol' ? 'list' : as === 'div' && ariaLabel ? 'group' : undefined;

  return (
    <Element className={className} id={id} aria-label={ariaLabel} role={role}>
      {children}
    </Element>
  );
}
