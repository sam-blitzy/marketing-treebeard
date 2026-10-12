/**
 * The document title of every screen (WCAG 2.2 SC 2.4.2 Page Titled): the page's own title, then
 * the product name, as "Decisions · Ads Action Engine", so the browser tab, the history list and
 * a screen reader's announcement of the window name the screen shown rather than the product
 * alone.
 *
 * Each screen calls {@link useDocumentTitle} beside its single `PageShell.Title`, with the same
 * text, so the document title follows the screen actually rendered: the five routed screens
 * (`features/auth/LoginPage.tsx`, `features/actions/ActionsPage.tsx`,
 * `features/decisions/DecisionsPage.tsx`, `features/sources/SourcesPage.tsx` and
 * `features/settings/SettingsPage.tsx`) in each of their states, and the router's error and
 * not-found screens (`src/router.tsx`). Until the first screen renders, the boot document's own
 * `<title>` in `index.html`, the product name alone, stands.
 *
 * The module imports only React, so any module can use it without an import cycle, and a
 * code-split screen that imports it pulls nothing else into the entry bundle.
 *
 * @packageDocumentation
 */
import { useEffect } from 'react';

/**
 * The product name: the end of every document title, and the whole title of a page whose own
 * title is blank. It matches the `<title>` of `index.html`.
 */
const PRODUCT_NAME = 'Ads Action Engine';

/** What stands between a page's title and the product name: a middle dot (U+00B7) with spaces. */
const SEPARATOR = ' · ';

/**
 * Sets the document title to `pageTitle` followed by the product name, for example
 * "Sign in · Ads Action Engine".
 *
 * - The title is written in an effect, after the screen has committed, and again after any
 *   commit in which `pageTitle` changed.
 * - `pageTitle` is used without its surrounding whitespace; one that is empty or only whitespace
 *   leaves the product name alone, never a title that starts with the separator.
 * - Nothing is restored when the calling component unmounts: the next screen's own call replaces
 *   the title. A screen that sets none of its own, such as the router's pending spinner, keeps
 *   the previous screen's title until the next screen renders.
 *
 * Call it once per screen, from the component that renders the screen's `PageShell.Title`, with
 * that title's text.
 *
 * @param pageTitle - the page's own title, the text of its `PageShell.Title`
 *
 * @example
 * ```tsx
 * const PAGE_TITLE = 'Decisions';
 *
 * export function DecisionsPage(): ReactElement {
 *   useDocumentTitle(PAGE_TITLE); // document.title: "Decisions · Ads Action Engine"
 *   return <PageShell.Title>{PAGE_TITLE}</PageShell.Title>;
 * }
 * ```
 */
export function useDocumentTitle(pageTitle: string): void {
  useEffect(() => {
    const title = pageTitle.trim();
    document.title = title === '' ? PRODUCT_NAME : `${title}${SEPARATOR}${PRODUCT_NAME}`;
  }, [pageTitle]);
}
