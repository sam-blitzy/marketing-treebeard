/**
 * `SideNav`: the product's navigation list with optional item counts (AAP 0.4.2, "Section
 * navigation: `SideNav` with anchor items and counts", status GAP). It is a single column in a
 * narrow container, such as `PageShell`'s side column, and a compact wrapping row of
 * content-sized items in a wide one, such as the primary navigation stacked above main or the
 * `/actions` section navigation, with each count right after its label.
 *
 * Three places render it (AAP 0.5.5): the app's primary navigation in the router layout, the
 * sign-in screen's own primary navigation in `LoginPage`, and the `/actions` section navigation,
 * whose anchor links carry each section's item count. It renders
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
 * Every destination is validated before either rendering path sees it. Only a same-origin route
 * path (`/decisions`, `/actions?run=3`) or an in-page fragment (`#section-keywords`) is a safe
 * `href`. An item with any other `href` (a scheme such as `javascript:` or `data:`, a
 * protocol-relative `//host`, a relative path, or a string with whitespace, control characters
 * or a backslash) fails closed: it renders an inert placeholder `<a>` with no `href`, keeping
 * the class contract, the label and count and `aria-current`, and `renderLink` is never called
 * for it. A custom renderer therefore only ever receives a validated destination.
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
   * Destination of the link, in one of two accepted forms:
   * - a same-origin route path: a single leading `/` not followed by another `/` or a `\`, with
   *   an optional query or fragment, such as `/decisions` or `/actions?run=3`;
   * - an in-page anchor: a leading `#`, such as `#section-keywords`.
   *
   * Any other value is unsafe and is never rendered as a destination: a scheme of any kind
   * (`javascript:`, `data:`, `https:`, `mailto:`…), a protocol-relative `//host` or `/\host`, a
   * relative path without the leading `/`, the empty string, or a string holding whitespace, an
   * ASCII control character (U+0000–U+001F, U+007F) or a backslash anywhere, since browsers drop
   * tabs and newlines inside a URL and read a backslash as a slash. An item with an unsafe `href`
   * renders an inert placeholder `<a>` with no `href` attribute, with the same class, label,
   * count and `aria-current` as its link would have had, and `renderLink` is not called for it.
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
  /**
   * The item's `href`, unchanged, and only ever a validated one: a same-origin route path or an
   * in-page anchor (see {@link SideNavItem.href}). An item with an unsafe `href` gets no link
   * props at all.
   */
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
   * Optional custom link renderer, called once per item whose `href` is safe, with the item and
   * the props built for its link; `item.href` and `linkProps.href` are then the same validated
   * destination. It must return a single link element carrying every prop in `linkProps`. It is
   * never called for an item with an unsafe `href`, which renders the inert placeholder
   * described at {@link SideNavItem.href} instead. Omitted, each safe item renders a plain `<a>`.
   */
  renderLink?: (item: SideNavItem, linkProps: SideNavLinkProps) => ReactNode;
}

/** Highest code point of the ASCII C0 control range, U+0000–U+001F. */
const C0_CONTROL_MAX = 0x1f;

/** Code point of DELETE, U+007F, the one ASCII control character above the C0 range. */
const DELETE_CODE = 0x7f;

/**
 * Whether `href` holds a character that can change how a browser parses it: an ASCII control
 * character (U+0000–U+001F or U+007F), whitespace of any kind, or a backslash.
 *
 * Browsers remove tabs and newlines anywhere in a URL, so `/\t/evil.com` would be read as the
 * protocol-relative `//evil.com`, and they read a backslash as a slash, so `/\evil.com` would be
 * too. The control characters are compared by code unit, because a regular expression holding
 * them is what ESLint's `no-control-regex` reports.
 *
 * @param href - the destination to inspect
 * @returns `true` when `href` holds at least one such character
 */
function hasUnsafeCharacter(href: string): boolean {
  for (let index = 0; index < href.length; index += 1) {
    const code = href.charCodeAt(index);
    if (code <= C0_CONTROL_MAX || code === DELETE_CODE) {
      return true;
    }
  }
  return /\s/u.test(href) || href.includes('\\');
}

/**
 * Whether `href` is a safe destination for a {@link SideNav} link: an in-page anchor (a leading
 * `#`) or a same-origin route path (a single leading `/` not followed by another `/` or a `\`,
 * with an optional query or fragment after the path), free of the characters
 * `hasUnsafeCharacter` rejects. Every other string is unsafe, the empty string included.
 *
 * It is an allowlist of the two forms the navigation needs, not a blocklist of schemes: a string
 * that starts with `#` or `/` cannot carry a scheme, so no spelling of `javascript:`, `data:` or
 * any other scheme passes.
 *
 * @param href - the item's destination
 * @returns `true` when `href` may be rendered as a link destination
 */
function isSafeHref(href: string): boolean {
  if (hasUnsafeCharacter(href)) {
    return false;
  }
  if (href.startsWith('#')) {
    return true;
  }
  const afterLeadingSlash = href.charAt(1);
  return href.startsWith('/') && afterLeadingSlash !== '/' && afterLeadingSlash !== '\\';
}

/**
 * What an item's link shows and states, apart from its destination: every
 * {@link SideNavLinkProps} field except `href`. A safe item's link and an unsafe item's inert
 * placeholder render the same content.
 */
type SideNavLinkContent = Omit<SideNavLinkProps, 'href'>;

/**
 * Builds the content of one item's link: the class contract, the current-state attribute and the
 * label and count content. The destination is added only once it is validated, by
 * {@link SideNav}.
 *
 * The count is preceded by a space text node. In the flex layout of `SideNav.css` that
 * whitespace-only node renders nothing, but it separates the label from the count in the link's
 * accessible name ("Keywords to adjust 4", not "Keywords to adjust4") in every accessible-name
 * implementation, whether or not it reads the CSS display of the two spans.
 *
 * @param item - the item the link is for
 * @returns the content of the item's link, without its `href`
 */
function buildLinkContent(item: SideNavItem): SideNavLinkContent {
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
    ? { className: CURRENT_LINK_CLASS, 'aria-current': 'page', children }
    : { className: LINK_CLASS, children };
}

/**
 * Navigation list: a `<nav>` landmark named by `label`, holding one link per item with its
 * optional count, in the order given. The items form one column in a narrow container and a
 * compact wrapping row of content-sized items in a wide one, each count right after its label.
 *
 * Each item's `href` is validated before it is rendered or handed to `renderLink`. An item with
 * an unsafe `href` renders an inert placeholder `<a>` without `href`, and `renderLink` is not
 * called for it (see {@link SideNavItem.href}). The component neither throws nor logs.
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
          const content = buildLinkContent(item);
          let link: ReactNode;
          if (isSafeHref(item.href)) {
            const linkProps: SideNavLinkProps = { href: item.href, ...content };
            link =
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
          } else {
            // Fails closed: an unsafe destination is never rendered and never reaches renderLink.
            // An <a> without href is the HTML placeholder for a link: it is not focusable and
            // cannot be followed, yet keeps the class contract, label, count and current state.
            link = (
              <a className={content.className} aria-current={content['aria-current']}>
                {content.children}
              </a>
            );
          }

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
