/**
 * `PageShell`: the product's page frame and its heading slots (AAP 0.4.2, status GAP).
 *
 * Every screen renders inside one `PageShell` (AAP 0.5.5): the router layout renders it for the
 * authenticated pages and `LoginPage` renders its own, so a page always has exactly one
 * `<main>` landmark. The frame holds, in order:
 *
 * 1. a "Skip to main content" link, hidden until it receives focus (WCAG 2.2 SC 2.4.1 Bypass
 *    Blocks), that moves focus to the main region;
 * 2. the `<header>` (banner) landmark, holding `TopBar`;
 * 3. the body row: an optional side-navigation column, holding `SideNav`, which supplies its own
 *    `<nav>` landmark, beside the `<main>` landmark that holds the page content. The column
 *    stacks above main whenever main would otherwise fall below its single-column width, and
 *    always below the `48rem` Grid breakpoint. Stacked from the breakpoint up, the column and
 *    main both keep that single-column width, so main never narrows as the viewport widens.
 *
 * Feature code never renders raw heading elements (AAP 0.4.4). It uses the two heading slots:
 *
 * - `PageShell.Title`: the page's single `<h1>`, with an optional action slot;
 * - `PageShell.Section`: a `<section>` region named by its `<h2>` (default) or `<h3>` heading,
 *   with an optional action slot. Its `id` is the in-page anchor target, used verbatim, such as
 *   `section-{key}` for the five action sections and `group-{sectionKey}-{groupKey}` for their
 *   groups (the discrepancy banner links to `#group-campaigns-discrepancies`). The heading's id
 *   is `${id}-title`, and the section is labelled by it (WCAG 2.2 SC 1.3.1).
 *
 * The styling lives in `PageShell.css`, whose class names are the contract this file renders,
 * and the skip link's hide-until-focused behaviour in `shared.css`. The Blitzy OS source is not
 * available, so this is a product wrapper at the last graceful-degradation step: semantic HTML
 * styled by semantic tokens, except for the frame's `100vh` minimum height, its flex factors (1
 * on the body row and the side navigation, 999 on main), the `999` width-switch multiplier in
 * the side-navigation and main caps, the `48rem` breakpoint and the skip link's visually hidden
 * `1px` box and `inset(50%)` clip, which `../GAPS.md` lists. Binding Blitzy OS later changes
 * only this file and its stylesheet, never the feature code that uses it (AAP 0.4.5,
 * `../GAPS.md`).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @packageDocumentation
 */
import './shared.css';
import './PageShell.css';

import { useRef } from 'react';
import type { MouseEvent, ReactNode } from 'react';

/** The id of the `<main>` landmark: the skip link's target. */
const MAIN_ID = 'ds-main';

/**
 * Whether an optional slot was given something to render. `undefined`, `null`, booleans and the
 * empty string render nothing in React, so a slot passed `cond && <SideNav />` that evaluates to
 * `false`, `null` or `''` gets no container either: an empty side-navigation column would still
 * take its track beside main (or a line of its own above main when stacked) plus the body gap,
 * pushing main content aside or down, and an empty actions container would still take a gap
 * step beside its heading. Any other value, including `0`, renders and keeps its container.
 *
 * @param node the slot's content
 * @returns `true` when the slot's container should be rendered
 */
function hasContent(node: ReactNode): boolean {
  return node !== undefined && node !== null && typeof node !== 'boolean' && node !== '';
}

/**
 * Props of {@link PageShell}.
 *
 * The prop set is closed on purpose: there is no `className` or `style`, so feature code cannot
 * restyle the frame (AAP 0.4.4).
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS page shell.
 */
export interface PageShellProps {
  /** The top bar, normally `TopBar`, rendered inside the `<header>` (banner) landmark. */
  topBar: ReactNode;
  /**
   * The side navigation, normally `SideNav`, which supplies its own `<nav>` landmark. Omitted
   * (or `null`, a boolean or an empty string), no side-navigation column is rendered and main
   * takes the full row; any other value, including `0`, renders the column.
   */
  sideNav?: ReactNode;
  /** The page content, rendered inside the `<main>` landmark. */
  children: ReactNode;
}

/**
 * Props of {@link PageShell.Title}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS page header.
 */
export interface PageShellTitleProps {
  /** The page title, rendered as the page's single `<h1>`. */
  children: ReactNode;
  /**
   * Controls placed beside the title, such as a "Sync now" button. Omitted (or `null`, a
   * boolean or an empty string), no actions container is rendered; any other value, including
   * `0`, renders it.
   */
  actions?: ReactNode;
}

/**
 * Props of {@link PageShell.Section}.
 *
 * GAP: bind to Blitzy OS on source arrival. These are product-level props, not Blitzy OS props;
 * on binding, they map onto the Blitzy OS section or heading component.
 */
export interface PageShellSectionProps {
  /**
   * The section's `id`, used verbatim as the in-page anchor target, such as `section-keywords`
   * or `group-campaigns-discrepancies`. The heading's id is `${id}-title`. It must be unique on
   * the page.
   */
  id: string;
  /** The section heading's content. It also names the section region. */
  title: ReactNode;
  /**
   * The heading level: `2` (default) for a top-level section of the page, `3` for a group
   * nested inside a level-2 section, so heading levels are never skipped.
   */
  headingLevel?: 2 | 3;
  /**
   * Controls placed beside the heading. Omitted (or `null`, a boolean or an empty string), no
   * actions container is rendered; any other value, including `0`, renders it.
   */
  actions?: ReactNode;
  /** The section content, rendered below the heading row. */
  children: ReactNode;
}

/**
 * The page frame: skip link, banner landmark with the top bar, optional side navigation and the
 * main landmark. Render exactly one per page.
 *
 * Activating the skip link (click, or Enter from the keyboard) moves focus to `<main>`, which
 * carries `tabindex="-1"` so it can take focus without entering the tab order. The default
 * navigation is prevented, so the router's URL is never rewritten to `#ds-main`; focusing the
 * element scrolls it into view in browsers, and `PageShell.css` gives main a scroll margin so its
 * focus ring stays inside the viewport. Enter activates the link with a click whose `detail` is
 * 0, and a pointer click has a `detail` of 1 or more. The focus call passes that as
 * `focusVisible`, so keyboard activation always shows main's focus ring (WCAG 2.2 SC 2.4.7) and a
 * pointer click shows none. Without it, main would inherit the link's keyboard-visible focus,
 * because the link is hidden until focused and so is only ever clicked after a Tab. Browsers
 * that ignore the option fall back to their own focus-visible heuristic.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function PageShellRoot({ topBar, sideNav, children }: PageShellProps) {
  const mainRef = useRef<HTMLElement>(null);

  const skipToMain = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    mainRef.current?.focus({ focusVisible: event.detail === 0 });
  };

  return (
    <div className="ds-pageshell">
      <a
        className="ds-pageshell__skip ds-visually-hidden ds-visually-hidden--focusable"
        href={`#${MAIN_ID}`}
        onClick={skipToMain}
      >
        Skip to main content
      </a>
      <header className="ds-pageshell__header">{topBar}</header>
      <div className="ds-pageshell__body">
        {hasContent(sideNav) && <div className="ds-pageshell__sidenav">{sideNav}</div>}
        <main ref={mainRef} id={MAIN_ID} tabIndex={-1} className="ds-pageshell__main">
          {children}
        </main>
      </div>
    </div>
  );
}

/**
 * The page title slot: the page's single `<h1>`, with optional actions beside it. Render it once,
 * as the first child of `PageShell`.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function PageShellTitle({ children, actions }: PageShellTitleProps) {
  return (
    <div className="ds-pageshell__titlebar">
      <h1 className="ds-pageshell__title">{children}</h1>
      {hasContent(actions) && <div className="ds-pageshell__actions">{actions}</div>}
    </div>
  );
}

/**
 * The section slot: a `<section>` region with the given anchor `id`, labelled by its `<h2>` or
 * `<h3>` heading (id `${id}-title`), with optional actions beside the heading and the content
 * below it.
 *
 * GAP: bind to Blitzy OS on source arrival.
 */
function PageShellSection({
  id,
  title,
  headingLevel = 2,
  actions,
  children,
}: PageShellSectionProps) {
  const titleId = `${id}-title`;
  const Heading = headingLevel === 3 ? 'h3' : 'h2';

  return (
    <section
      id={id}
      aria-labelledby={titleId}
      className={`ds-pageshell__section ds-pageshell__section--level-${headingLevel}`}
    >
      <div className="ds-pageshell__section-header">
        <Heading id={titleId} className="ds-pageshell__section-title">
          {title}
        </Heading>
        {hasContent(actions) && <div className="ds-pageshell__actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

/**
 * The page frame with its heading slots attached: `PageShell.Title` (the page `<h1>`) and
 * `PageShell.Section` (an anchored `<h2>` or `<h3>` section).
 *
 * GAP: bind to Blitzy OS on source arrival.
 *
 * @example
 * ```tsx
 * <PageShell topBar={<TopBar … />} sideNav={<SideNav … />}>
 *   <PageShell.Title actions={<Button variant="secondary">Sync now</Button>}>
 *     Actions
 *   </PageShell.Title>
 *   <PageShell.Section id="section-campaigns" title="Campaigns to pause or launch">
 *     <PageShell.Section
 *       id="group-campaigns-discrepancies"
 *       title="Discrepancies"
 *       headingLevel={3}
 *     >
 *       …
 *     </PageShell.Section>
 *   </PageShell.Section>
 * </PageShell>
 * ```
 */
export const PageShell = Object.assign(PageShellRoot, {
  Title: PageShellTitle,
  Section: PageShellSection,
});
