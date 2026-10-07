/**
 * `Badge`: the product's status and flag chip (AAP 0.4.2, status GAP).
 *
 * Serves the per-source chips in the status bar (Live, Simulated, Stale, Incomplete) and the
 * flags row on recommendation cards (Simulated data, Short window, Floor, …) (AAP 0.5.5). The
 * styling lives in `Badge.css`, whose class names are the contract this component renders:
 *
 * - `ds-badge`: always;
 * - `ds-badge--{neutral|info|warning|critical|success}`: always exactly one, from the `tone`
 *   prop (default `neutral`).
 *
 * Chips are text labels only: no icon set ships until the Blitzy OS icon source arrives
 * (AAP 0.4.5). The text child always states the meaning, and the tone colour is decoration that
 * repeats it, never the sole indicator (WCAG 2.2 SC 1.4.1).
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Badge.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Badge.css';

import type { ReactNode } from 'react';

import type { Tone } from '../tokens';

/**
 * Props of {@link Badge}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). Colour comes only from `tone`.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS status chip.
 */
export interface BadgeProps {
  /**
   * The semantic tone, which sets the chip's text and border colour. Defaults to `'neutral'`.
   * A tone only reinforces the label: the children must state the meaning on their own.
   */
  tone?: Tone;
  /**
   * The chip's label, for example `"Simulated data"` or `"Stale"`. It carries the whole meaning
   * of the chip, so it must never be empty or rely on colour to be understood.
   */
  children: ReactNode;
  /**
   * The element's `id`, for example so another element can reference the chip through
   * `aria-describedby`.
   */
  id?: string;
}

/**
 * Status and flag chip: renders one `<span>` carrying `ds-badge` and the modifier for its tone.
 *
 * The chip is static text, so it takes no role and no live region: assistive technology reads
 * its label in document order, like any other inline text. Use `Banner` for messages that must
 * be announced.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Badge>Shared budget</Badge>
 *
 * <Badge tone="info">Simulated</Badge>
 *
 * <Inline as="ul" gap={2} aria-label="Flags">
 *   <li>
 *     <Badge tone="warning">Short window</Badge>
 *   </li>
 * </Inline>
 * ```
 */
export function Badge({ tone = 'neutral', children, id }: BadgeProps) {
  return (
    <span id={id} className={`ds-badge ds-badge--${tone}`}>
      {children}
    </span>
  );
}
