/**
 * `SideNav`: the product's vertical navigation list with optional item counts (AAP 0.4.2,
 * "Section navigation: `SideNav` with anchor items and counts", status GAP).
 *
 * Two places render it (AAP 0.5.5): the app's primary navigation in the router layout, and the
 * `/actions` section navigation, whose anchor links carry each section's item count. It renders
 * one `<nav>` landmark named by `label`, holding a list of links, so assistive technology can
 * jump to the navigation and hear how many destinations it holds (WCAG 2.2 SC 1.3.1, 2.4.1 and
 * 4.1.2). The styling lives in `SideNav.css`, whose class names are the contract rendered here:
 *
 * ```html
 * <nav class="ds-sidenav" aria-label="…">
 *   <ul class="ds-sidenav__list">
 *     <li class="ds-sidenav__item">
 *       <a class="ds-sidenav__link [ds-sidenav__link--current]" href="…" [aria-current="page"]>
 *         <span class="ds-sidenav__label">…</span>
 *         <span class="ds-sidenav__count">4</span>   (only when the item has a count)
 *       </a>
 *     </li>
 *   </ul>
 * </nav>
 * ```
 *
 * The design system stays router-agnostic: it never imports TanStack Router. A caller that needs
 * client-side navigation passes `renderLink`, which receives the ready-made {@link SideNavLinkProps}
 * and returns its own link element (for example a TanStack Router `Link` in `src/router.tsx`).
 * Without `renderLink`, each item renders a plain `<a href>`, which suits in-page anchors such
 * as `#section-keywords`.
 *
 * The Blitzy OS source is unavailable, so this is a product wrapper at the last
 * graceful-degradation step (AAP 0.4.4): semantic HTML styled solely by semantic tokens. Binding
 * Blitzy OS later changes only this file's internals and `SideNav.css`; the props below are the
 * stable contract for feature code, re-exported by the design-system index (AAP 0.4.5,
 * `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './SideNav.css';

import type { ReactNode } from 'react';

/** Class of every link; the contract defined in `SideNav.css`. */
const LINK_CLASS = 'ds-sidenav__link';

/** Class of the link of the current page or section: the base class plus the modifier. */
const CURRENT_LINK_CLASS = `${LINK_CLASS} ${LINK_CLASS}--current`;

/**
 * One destination in a {@link SideNav}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level fields, not Blitzy OS props.
 */
export interface SideNavItem {
  /** Stable identity of the item, unique within one `SideNav`; used as the React list key. */
  key: string;
  /** Visible text of the link, and the start of its accessible name. */
  label: ReactNode;
  /**
   * Destination of the link: a route path such as `/decisions`, or an in-page anchor such as
   * `#section-keywords`.
   */
  href: string;
  /**
   * Optional count shown after the label, for example the number of items in an `/actions`
   * section. It is visible text and part of the link's accessible name ("Keywords to adjust 4"),
   * so its meaning never rests on colour. A count of `0` is shown, not omitted; an omitted count,
   * or a value that is not a finite number, renders no count at all, so the link can never read
   * "NaN" or "Infinity".
   */
  count?: number;
  /**
   * Whether the item is the current page or section. The current link gets
   * `aria-current="page"` and the `ds-sidenav__link--current` modifier, which `SideNav.css` marks
   * by weight and an accent bar, never by colour alone. Defaults to `false`.
   */
  current?: boolean;
}

/**
 * Props that {@link SideNav} builds for each item's link and hands to `renderLink`.
 *
 * Spread these onto the custom link element (`<Link {...linkProps} to={…} />`), so its styling
 * (`className`) and its current-state semantics (`aria-current`) are preserved and the link looks
 * and reads exactly like the default `<a>`. `'aria-current'` is present only on the current item's
 * props; it is absent, not `undefined`, on every other item.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
export interface SideNavLinkProps {
  /** The item's `href`, unchanged. */
  href: string;
  /** `ds-sidenav__link`, plus `ds-sidenav__link--current` on the current item. */
  className: string;
  /** `'page'` on the current item only. */
  'aria-current'?: 'page';
  /** The label span and, when the item has a count, the count span. */
  children: ReactNode;
}

/**
 * Props of {@link SideNav}.
 *
 * The interface is deliberately closed: there is no `className` or `style` prop, because feature
 * code must not restyle a wrapper (AAP 0.4.4).
 *
 * GAP: bind to Blitzy OS on source arrival. On binding, these map onto the Blitzy OS navigation
 * component.
 */
export interface SideNavProps {
  /**
   * Accessible name of the `<nav>` landmark, for example `"Sections"` or `"Main"`. Required,
   * because a page with more than one navigation landmark needs each one named to tell them
   * apart.
   */
  label: string;
  /**
   * The destinations, rendered in the given order. An empty list renders the named landmark with
   * an empty list.
   */
  items: readonly SideNavItem[];
  /**
   * Optional custom link renderer, called once per item with the item and the props built for
   * its link. It must return a single link element carrying every prop in `linkProps`. Omitted,
   * each item renders a plain `<a>`.
   */
  renderLink?: (item: SideNavItem, linkProps: SideNavLinkProps) => ReactNode;
}

/**
 * Builds the link props of one item: the class contract, the current-state attribute and the
 * label and count content.
 *
 * The count is preceded by a space text node. In the flex layout of `SideNav.css` that
 * whitespace-only node renders nothing, but it separates the label from the count in the link's
 * accessible name ("Keywords to adjust 4", not "Keywords to adjust4") in every accessible-name
 * implementation, whether or not it reads the CSS display of the two spans.
 *
 * @param item - the item the link is for
 * @returns the props for the item's link
 */
function buildLinkProps(item: SideNavItem): SideNavLinkProps {
  const hasCount = item.count !== undefined && Number.isFinite(item.count);
  const children = (
    <>
      <span className="ds-sidenav__label">{item.label}</span>
      {hasCount && (
        <>
          {' '}
          <span className="ds-sidenav__count">{item.count}</span>
        </>
      )}
    </>
  );

  // The current-state attribute is left out of a non-current item's props rather than set to
  // undefined, so a custom link that spreads them renders no aria-current at all.
  return item.current === true
    ? { href: item.href, className: CURRENT_LINK_CLASS, 'aria-current': 'page', children }
    : { href: item.href, className: LINK_CLASS, children };
}

/**
 * Vertical navigation list: a `<nav>` landmark named by `label`, holding one link per item with
 * its optional count, in the order given.
 *
 * The list stays an `<ul>` without an explicit `role="list"`: `SideNav.css` removes its markers,
 * and WebKit keeps the list semantics of an unmarked list inside a `<nav>`, so VoiceOver still
 * announces the number of destinations.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * // In-page section navigation on /actions, with plain anchors:
 * <SideNav
 *   label="Sections"
 *   items={[
 *     { key: 'keywords', label: 'Keywords to adjust', href: '#section-keywords', count: 4, current: true },
 *     { key: 'campaigns', label: 'Campaigns to pause or launch', href: '#section-campaigns', count: 0 },
 *   ]}
 * />
 *
 * // Primary navigation with router links, in src/router.tsx:
 * <SideNav
 *   label="Main"
 *   items={routes}
 *   renderLink={(item, linkProps) => <Link {...linkProps} to={item.href} />}
 * />
 * ```
 *
 * @param props - see {@link SideNavProps}
 * @returns the navigation landmark with its list of links
 */
export function SideNav({ label, items, renderLink }: SideNavProps) {
  return (
    <nav className="ds-sidenav" aria-label={label}>
      <ul className="ds-sidenav__list">
        {items.map((item) => {
          const linkProps = buildLinkProps(item);
          const link =
            renderLink === undefined ? (
              <a
                href={linkProps.href}
                className={linkProps.className}
                aria-current={linkProps['aria-current']}
              >
                {linkProps.children}
              </a>
            ) : (
              renderLink(item, linkProps)
            );

          return (
            <li key={item.key} className="ds-sidenav__item">
              {link}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
