/**
 * `Banner`: the product's global message banner (AAP 0.4.2, `Banner` with `tone`, `title`,
 * `actions`).
 *
 * The `/actions` page shows its global banners through this component, in the order AAP 0.5.5
 * fixes: open discrepancies, tracking findings, simulated sources, stale or incomplete datasets
 * and the short window; the custom-window notice uses it as well. The styling lives in
 * `Banner.css`, whose class names are the contract this component renders:
 *
 * - `ds-banner`: always;
 * - `ds-banner--{tone}`: always, from the `tone` prop (default `'info'`), one of `TONES` in
 *   `../tokens.ts`;
 * - `ds-banner__title`: the title paragraph, always;
 * - `ds-banner__content`: only when there is supporting content to show;
 * - `ds-banner__actions`: only when there are actions to show.
 *
 * Live-region policy (AAP 0.5.5: "Banners use the `alert` role only for discrepancies"): the
 * banner carries `role="alert"` only when `alert` is set, and feature code sets it on the
 * open-discrepancies banner alone. Every other banner is plain content with no `role="status"`,
 * no `aria-live` and no region role, so assistive technology announces nothing but the
 * discrepancy warning on its own.
 *
 * The title is a paragraph, not a heading, so banners never add entries to the page heading
 * outline (feature code renders headings only through the `PageShell` slots, AAP 0.4.4).
 *
 * The Blitzy OS source is not available, so this is a product wrapper at the last
 * graceful-degradation step: it renders semantic HTML styled solely by semantic tokens, and
 * binding Blitzy OS later changes only this file and `Banner.css` (AAP 0.4.5, `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './Banner.css';

import type { ReactNode } from 'react';

import type { Tone } from '../tokens';
import { withIdentifierBreaks } from './identifierBreaks';

/**
 * Props of {@link Banner}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because
 * feature code must not restyle a wrapper (AAP 0.4.4). The tone is the only visual choice.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS
 * props; on binding, they map onto the Blitzy OS banner or notification component.
 */
export interface BannerProps {
  /**
   * The semantic tone: it colours the banner's inline-start edge and its title. Defaults to
   * `'info'`. The title text must name the condition itself, because colour is never the only
   * cue.
   */
  tone?: Tone;
  /**
   * The headline that names the condition, rendered as a paragraph rather than a heading. A
   * string title gets a line-break opportunity after each underscore of a long identifier
   * ({@link withIdentifierBreaks}), with its text unchanged.
   */
  title: ReactNode;
  /**
   * Supporting content below the title, such as a reason or a list of affected datasets.
   * Omitted (or `null`, a boolean or an empty string), no content container is rendered. String
   * content gets the same identifier breaks as a string title.
   */
  children?: ReactNode;
  /**
   * Actions below the content: buttons or links, laid out in a wrapping row. Omitted (or
   * `null`, a boolean or an empty string), no actions container is rendered.
   */
  actions?: ReactNode;
  /**
   * Renders the banner with `role="alert"`, so assistive technology announces it at once.
   * Defaults to `false`. Set it only on the open-discrepancies banner (AAP 0.5.5); every other
   * banner stays silent content.
   */
  alert?: boolean;
  /** The banner element's `id`, for example the target of an in-page anchor link. */
  id?: string;
}

/**
 * Whether a slot value renders anything.
 *
 * React renders nothing for `undefined`, `null`, booleans and the empty string, so a
 * conditional slot such as `actions={canAct && <Button>…</Button>}` must not leave an empty
 * container behind: `Banner.css` spaces the parts with a flex gap, and an empty container would
 * still add that gap below the title. Any other value, including `0`, renders and counts.
 *
 * @param node - the `children` or `actions` value
 * @returns `true` when the slot has content to render
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * Global message banner: a surface panel with a toned edge, a title paragraph and optional
 * supporting content and actions.
 *
 * Only a banner rendered with `alert` is exposed as a live region (`role="alert"`); feature code
 * sets `alert` on the open-discrepancies banner and on no other (AAP 0.5.5).
 *
 * A string title or content, usually the API's own message, passes through
 * {@link withIdentifierBreaks}, so a long identifier such as an environment variable name wraps
 * at its underscores on a narrow screen instead of at an arbitrary character.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <Banner
 *   tone="critical"
 *   alert
 *   id="banner-discrepancies"
 *   title="3 open discrepancies to resolve first"
 *   actions={<a href="#group-campaigns-discrepancies">Review discrepancies</a>}
 * >
 *   Platform state no longer matches the last recorded decisions.
 * </Banner>
 *
 * <Banner tone="warning" title="Simulated data: LinkedIn (credentials not configured)" />
 * ```
 */
export function Banner({
  tone = 'info',
  title,
  children,
  actions,
  alert = false,
  id,
}: BannerProps) {
  return (
    <div id={id} className={`ds-banner ds-banner--${tone}`} role={alert ? 'alert' : undefined}>
      <p className="ds-banner__title">{withIdentifierBreaks(title)}</p>
      {hasContent(children) && (
        <div className="ds-banner__content">{withIdentifierBreaks(children)}</div>
      )}
      {hasContent(actions) && <div className="ds-banner__actions">{actions}</div>}
    </div>
  );
}
