/**
 * `Inline`: the product's horizontal layout primitive (AAP 0.4.2, status GAP).
 *
 * Feature code applies no flex CSS of its own (AAP 0.4.4): every horizontal arrangement of
 * badges, buttons, chips and short labels goes through this component, and its spacing comes
 * only from the spacing tokens. The component maps its props to the classes in `Inline.css`
 * and renders nothing else, so binding Blitzy OS later changes only this file and its
 * stylesheet, never the feature code that uses it.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Inline.css';

import type { ReactNode } from 'react';

import type { SpaceStep } from '../tokens';

/**
 * Props of {@link Inline}.
 *
 * The prop set is closed on purpose: there is no `className` or `style`, so a caller cannot
 * add literal styles or custom flex rules around the primitive (AAP 0.4.4). Spacing, wrapping,
 * alignment and long-word wrapping are chosen only through the props below.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export interface InlineProps {
  /**
   * Space between children, as a step of the spacing scale: step `n` resolves to the
   * `--space-n` token. Defaults to `2`.
   */
  gap?: SpaceStep;
  /**
   * Whether children wrap onto further lines when the row runs out of room. Defaults to
   * `true`; `false` keeps every child on one line (the children's own text may still wrap).
   */
  wrap?: boolean;
  /**
   * Cross-axis (vertical) alignment of the children within each line. When unset, children
   * are centred, which lines up items of differing heights.
   */
  align?: 'start' | 'center' | 'end' | 'baseline' | 'stretch';
  /**
   * Main-axis (horizontal) distribution of the children along each line. When unset,
   * children are packed at the start; `between` pushes the first and last child to the ends.
   */
  justify?: 'start' | 'center' | 'end' | 'between';
  /**
   * Whether long unbroken text, such as URNs, search terms, dataset names and threshold keys,
   * may break at any point, so a child wraps inside its line instead of widening the row past a
   * narrow viewport (WCAG 2.2 SC 1.4.10 Reflow). The treatment is inherited by every descendant.
   * Defaults to `false`. Leave it off on a row that holds a `DataTable`, whose cells keep their
   * own sizing. Inside a `DataTable` cell it has no effect: the row keeps its words whole, as the
   * cell does, and the table scrolls in its region instead.
   */
  breakLongWords?: boolean;
  /**
   * The element to render. Defaults to `div`. Use `ul` or `ol` when the children form a list,
   * for example a row of flags or source chips; every child must then be an `<li>`, because
   * a list element may contain only list items. The list's markers, indentation and outer
   * spacing are cleared, so it carries `role="list"`: Safari's VoiceOver stops announcing an
   * unmarked list as a list without the explicit role.
   */
  as?: 'div' | 'ul' | 'ol';
  /** The element's `id`, for example as the target of an in-page link or `aria-controls`. */
  id?: string;
  /**
   * Accessible name of the row. On a list (`as="ul"` or `as="ol"`) it names the list. On a
   * `div` the row is exposed as a labelled `group`, because ARIA does not allow a name on an
   * element with no role and assistive technology would never announce it. An empty label is
   * treated as no label.
   */
  'aria-label'?: string;
  /** The row's content. With `as="ul"` or `as="ol"`, `<li>` elements only. */
  children?: ReactNode;
}

/**
 * Lays its children out in a horizontal row separated by a token gap, wrapping by default.
 *
 * Classes set, all defined in `Inline.css`:
 * - `ds-inline` and `ds-inline--gap-{gap}`, always;
 * - `ds-inline--nowrap` when `wrap` is `false`;
 * - `ds-inline--align-{align}` and `ds-inline--justify-{justify}` when those props are set;
 * - `ds-inline--break-long-words` when `breakLongWords` is `true`.
 *
 * When `as` is `ul` or `ol`, every child must be an `<li>`, and the list carries `role="list"`.
 * A `div` given a non-empty `aria-label` carries `role="group"`; without one it has no role.
 *
 * @example
 * ```tsx
 * <Inline gap={1} as="ul" aria-label="Flags">
 *   <li><Badge tone="info">Simulated data</Badge></li>
 *   <li><Badge tone="warning">Stale</Badge></li>
 * </Inline>
 *
 * <Inline justify="between" align="baseline">
 *   <span>Rank 1</span>
 *   <Button variant="primary">Record decision</Button>
 * </Inline>
 * ```
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @param props - see {@link InlineProps}
 * @returns the `as` element carrying the layout classes, `id`, `aria-label`, the derived `role`
 *   and children
 */
export function Inline({
  gap = 2,
  wrap = true,
  align,
  justify,
  breakLongWords = false,
  as = 'div',
  id,
  'aria-label': ariaLabel,
  children,
}: InlineProps) {
  const classes = ['ds-inline', `ds-inline--gap-${gap}`];
  if (!wrap) {
    classes.push('ds-inline--nowrap');
  }
  if (align !== undefined) {
    classes.push(`ds-inline--align-${align}`);
  }
  if (justify !== undefined) {
    classes.push(`ds-inline--justify-${justify}`);
  }
  if (breakLongWords) {
    classes.push('ds-inline--break-long-words');
  }

  // `as` is a closed union of intrinsic tags, so the JSX below renders exactly one of them.
  const Element = as;

  // Lists keep their list role under Inline.css's marker reset; a labelled div becomes a group so
  // its name is exposed (see InlineProps.as and InlineProps['aria-label']).
  const role = as === 'div' ? (ariaLabel ? 'group' : undefined) : 'list';

  return (
    <Element className={classes.join(' ')} id={id} aria-label={ariaLabel} role={role}>
      {children}
    </Element>
  );
}
