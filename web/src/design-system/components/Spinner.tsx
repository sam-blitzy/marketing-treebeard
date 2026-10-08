/**
 * `Spinner`: the product's loading indicator (AAP 0.4.2, "Loading and empty states").
 *
 * It renders a decorative rotating ring plus a visually hidden text label inside a
 * `role="status"` element. `role="status"` is a polite live region (implicit
 * `aria-live="polite"` and `aria-atomic="true"`): assistive technology reads the label as a
 * status message without moving focus or interrupting what the user is doing, and the ring
 * itself stays out of the accessibility tree (WCAG 2.2 SC 4.1.3 Status Messages).
 *
 * The styling lives in `Spinner.css`, and the hidden-text utility in `shared.css`. Their class
 * names are the contract this component renders:
 *
 * - `ds-spinner`: the inline wrapper carrying `role="status"`;
 * - `ds-spinner__circle`: the ring, `aria-hidden="true"`; it stops turning under
 *   `prefers-reduced-motion: reduce` and stays visible in forced-colours mode;
 * - `ds-visually-hidden`: the label, read by assistive technology but not shown on screen.
 *
 * Inside `Button`, the Spinner sits in a slot that `Button` hides from the accessibility tree:
 * the button conveys its loading state with `aria-busy="true"`, so the button's accessible name
 * stays its own label and the Spinner's label is never appended to it.
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled by semantic tokens, except for the
 * ring's `1s linear` rotation and the label's visually hidden `1px` box and `inset(50%)` clip
 * that `../GAPS.md` lists, and binding Blitzy OS later changes only this file and `Spinner.css`
 * (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './Spinner.css';

/**
 * Props of {@link Spinner}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). Size, colours and motion come only from
 * the tokens `Spinner.css` reads.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS loading indicator.
 */
export interface SpinnerProps {
  /**
   * The accessible text announced by the live region and never shown on screen, for example
   * `"Loading recommendations"`. It is required and should name what is loading in a short,
   * non-empty phrase: the ring is decorative, so this label is the Spinner's only content for
   * assistive technology.
   */
  label: string;
}

/**
 * Loading indicator: a decorative ring with a visually hidden label inside a polite
 * `role="status"` live region.
 *
 * Render it in place of content that is still loading, and remove it once the content arrives.
 * Whether a screen reader speaks a live region the moment it is mounted varies between screen
 * readers, but the label is always reachable as the status element's text. The component holds
 * no state and has no effects; its output depends only on `label`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * if (recommendations.isPending) {
 *   return <Spinner label="Loading recommendations" />;
 * }
 * ```
 */
export function Spinner({ label }: SpinnerProps) {
  return (
    <span role="status" className="ds-spinner">
      <span className="ds-spinner__circle" aria-hidden="true" />
      <span className="ds-visually-hidden">{label}</span>
    </span>
  );
}
