/**
 * `Grid`: the product's grid layout primitive (AAP 0.4.2, `Grid` with columns).
 *
 * Feature code lays out tiles, cards and evidence blocks in columns only through this component
 * (AAP 0.4.4: layout through primitives, never custom grid CSS on a raw `div`). Columns and gaps
 * come from the class contract in `Grid.css`, whose values are spacing tokens except for the
 * `fr` tracks, the column-count fallback of `1`, the `min(100%, …)` track cap and the `48rem`
 * breakpoint, which `../GAPS.md` lists. Every grid stacks to a single column below the `Grid`
 * breakpoint (AAP 0.5.5); that collapse lives in `Grid.css`, so no column count set here can
 * defeat it.
 *
 * The Blitzy OS source is unavailable, so this is a product wrapper rendering semantic HTML at
 * the last graceful-degradation step (AAP 0.4.4). Binding Blitzy OS later changes only this
 * file's internals and `Grid.css`; {@link GridProps} is the stable contract for feature code,
 * re-exported by the design-system index.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Grid.css';

import type { CSSProperties, ReactNode } from 'react';

import type { SpaceStep } from '../tokens';

/**
 * Props of {@link Grid}.
 *
 * There is no `className` or `style` prop: feature code never restyles a wrapper (AAP 0.4.4).
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export interface GridProps {
  /**
   * The column mode.
   *
   * - A number fixes the track count: that many equal columns. It is normalised to a whole
   *   number of at least 1, so a fraction is floored (`2.7` gives 2 columns), and zero, a
   *   negative or a non-finite value gives 1 column.
   * - `{ min: 'narrow' }` or `{ min: 'wide' }` uses auto-fit tracks: as many columns as the
   *   container fits, each at least a token-derived minimum width (`narrow` is six `--space-8`
   *   steps, `wide` ten), never wider than the container itself.
   *
   * Whichever mode is used, every grid collapses to one column below the `Grid` breakpoint.
   */
  columns: number | { min: 'narrow' | 'wide' };
  /**
   * Gap between columns and between rows, as a step of the spacing scale: step `n` is the
   * `--space-n` token. Defaults to `4`.
   */
  gap?: SpaceStep;
  /**
   * The element rendered. Defaults to `'div'`.
   *
   * With `'ul'` or `'ol'` every child must be an `<li>`. The list carries `role="list"`, because
   * `Grid.css` removes list markers and Safari's VoiceOver stops announcing an unmarked list as a
   * list without the explicit role.
   */
  as?: 'div' | 'ul' | 'ol';
  /** Element id, for example the target of an in-page anchor. */
  id?: string;
  /**
   * Accessible name. On a list it names the list. On a `div` the grid is exposed as a labelled
   * `group`, because ARIA does not allow a name on an element with no role and assistive
   * technology would never announce it.
   */
  'aria-label'?: string;
  /** The grid cells: `<li>` elements when `as` is `'ul'` or `'ol'`, any content otherwise. */
  children?: ReactNode;
}

/**
 * Inline style of a fixed-count grid. It holds only the component-private custom property that
 * `.ds-grid--fixed` reads, typed without a cast and still assignable to `CSSProperties`.
 */
type GridStyle = CSSProperties & Record<'--ds-grid-columns', string>;

/**
 * Normalises a requested column count to the positive whole number `.ds-grid--fixed` expects.
 * `Math.floor` and `Math.max` both pass `NaN` through, so non-finite input is handled first.
 *
 * @param columns - the requested count, possibly fractional, non-positive or non-finite
 * @returns the track count: `Math.max(1, Math.floor(columns))` for finite input, otherwise 1
 */
function trackCount(columns: number): number {
  return Number.isFinite(columns) ? Math.max(1, Math.floor(columns)) : 1;
}

/**
 * Grid layout primitive: lays its children out in equal columns with a token gap, and stacks
 * them to a single column below the `Grid` breakpoint.
 *
 * Usage, in feature code through the design-system index:
 *
 * ```tsx
 * import { Grid } from '../../design-system';
 *
 * // Three equal columns, default gap (step 4).
 * <Grid columns={3}>{cards}</Grid>
 *
 * // As many columns as fit, each at least the narrow minimum, as a labelled list.
 * <Grid columns={{ min: 'narrow' }} gap={2} as="ul" aria-label="Data sources">
 *   {sources.map((source) => <li key={source.key}>{source.name}</li>)}
 * </Grid>
 * ```
 *
 * The only inline style is the component-private `--ds-grid-columns` property of a fixed-count
 * grid; React writes it through the CSSOM, which the `default-src 'self'` CSP permits. Every
 * design value comes from `Grid.css` and the spacing tokens.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @param props - see {@link GridProps}
 * @returns the `as` element carrying the grid classes
 */
export function Grid({
  columns,
  gap = 4,
  as = 'div',
  id,
  'aria-label': ariaLabel,
  children,
}: GridProps) {
  const Element = as;

  const fixed = typeof columns === 'number';
  const mode = fixed ? 'fixed' : columns.min;
  const style: GridStyle | undefined = fixed
    ? { '--ds-grid-columns': String(trackCount(columns)) }
    : undefined;

  // Lists keep their list role under Grid.css's marker reset; a labelled div becomes a group so
  // its name is exposed (see GridProps.as and GridProps['aria-label']).
  const role = as === 'div' ? (ariaLabel ? 'group' : undefined) : 'list';

  return (
    <Element
      id={id}
      aria-label={ariaLabel}
      role={role}
      className={`ds-grid ds-grid--${mode} ds-grid--gap-${gap}`}
      style={style}
    >
      {children}
    </Element>
  );
}
