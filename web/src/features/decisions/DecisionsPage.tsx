/**
 * `DecisionsPage`: the `/decisions` screen (AAP 0.5.5 "Other screens → `/decisions`").
 *
 * The page is where the user records what was decided and settles what the platforms disagree
 * with (FR-10). Top to bottom it renders:
 *
 * 1. the page title, the page's single `<h1>` ("Decisions"), which also titles the document
 *    "Decisions · Ads Action Engine" ({@link useDocumentTitle}, WCAG 2.2 SC 2.4.2);
 * 2. the "Discrepancies" section (`<h2>`, anchor `#discrepancies`), holding the
 *    {@link DiscrepancyResolver}: every open and awaiting-platform discrepancy with the
 *    resolutions valid for its kind and subject type (AAP 0.5.4.4). It comes first because
 *    discrepancies are resolved before any other recommendation is trusted (AAP 0.5.4.2 P1);
 * 3. the "Decision log" section (`<h2>`, anchor `#decision-log`), holding the
 *    {@link DecisionLog}: the decisions per subject (campaign or shared budget) with origin, time
 *    and the patch applied, and the "Record decision" action that opens `DecisionForm`.
 *
 * Headings follow WCAG 2.2 AA order: this page owns the `<h1>` and the two `<h2>` section
 * headings, and its children render their entries as `<h3>` (resolver cards, decision-log
 * subjects), so no level is skipped.
 *
 * Rules this file keeps:
 * - **No frame of its own.** `src/router.tsx` renders this page inside the authenticated layout,
 *   which already provides the one `PageShell` (top bar, primary side navigation and the
 *   `<main>` landmark). The page uses only the heading slots `PageShell.Title` and
 *   `PageShell.Section`, so the screen keeps exactly one `<main>`.
 * - **No data of its own.** The page fetches and formats nothing. Each child reads its own server
 *   state through `../../api/queries` (AAP 0.11.1) and owns its loading, empty and error states,
 *   so a failed discrepancy read never hides the decision log, and the other way round.
 * - **Design system only** (AAP 0.4.4): UI comes from the design-system index and layout only
 *   from `Stack`; no raw headings, no styles and no stylesheet of its own.
 * - **No router import**: `src/router.tsx` imports this page, so importing it back would create
 *   a cycle.
 *
 * @packageDocumentation
 */
import type { ReactElement } from 'react';

import { PageShell, Stack } from '../../design-system';
import { useDocumentTitle } from '../../lib/documentTitle';
import { DecisionLog } from './DecisionLog';
import { DiscrepancyResolver } from './DiscrepancyResolver';

// ---------------------------------------------------------------------------------------------
// Texts and anchors
// ---------------------------------------------------------------------------------------------

/** The page title, rendered as the page's single `<h1>`. */
const PAGE_TITLE = 'Decisions';

/**
 * The anchor id of the discrepancies section. `PageShell.Section` uses it verbatim, so links
 * such as `/decisions#discrepancies` land on the resolver, and names the heading
 * `discrepancies-title`.
 */
const DISCREPANCIES_SECTION_ID = 'discrepancies';

/** The heading of the discrepancies section, an `<h2>`. */
const DISCREPANCIES_SECTION_TITLE = 'Discrepancies';

/**
 * The anchor id of the decision-log section, used verbatim by `PageShell.Section`; the heading's
 * id is `decision-log-title`.
 */
const DECISION_LOG_SECTION_ID = 'decision-log';

/** The heading of the decision-log section, an `<h2>`. */
const DECISION_LOG_SECTION_TITLE = 'Decision log';

// ---------------------------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------------------------

/**
 * The `/decisions` screen: the page title, then the discrepancy resolver, then the decision log
 * (AAP 0.5.5).
 *
 * It takes no props and holds no state: it lays out three blocks in one `Stack`, spaced like the
 * other screens' columns:
 *
 * - `PageShell.Title` "Decisions" (the page's `<h1>`), with the document title
 *   "Decisions · Ads Action Engine";
 * - `PageShell.Section` `id="discrepancies"` titled "Discrepancies" (an `<h2>`) holding
 *   {@link DiscrepancyResolver}, first because discrepancies must be resolved before other
 *   recommendations are trusted (AAP 0.5.4.2 P1);
 * - `PageShell.Section` `id="decision-log"` titled "Decision log" (an `<h2>`) holding
 *   {@link DecisionLog}.
 *
 * Every state below the headings (loading spinners, empty states, load errors, the
 * intended-state and record-decision dialogs) belongs to the two children, so the title and both
 * section headings are on screen from the first render, whatever the API answers. The component
 * must render inside the app's `QueryClientProvider` and TanStack `RouterProvider`, inside the
 * authenticated layout's `PageShell`, as every authenticated page does.
 *
 * @returns the `/decisions` page content, rendered inside the app's `PageShell`
 *
 * @example
 * ```tsx
 * // src/router.tsx, /decisions route under the authenticated layout:
 * const decisionsRoute = createRoute({
 *   getParentRoute: () => authenticatedRoute,
 *   path: '/decisions',
 *   component: DecisionsPage,
 * });
 * ```
 */
export function DecisionsPage(): ReactElement {
  useDocumentTitle(PAGE_TITLE);
  return (
    <Stack gap={6}>
      <PageShell.Title>{PAGE_TITLE}</PageShell.Title>
      <PageShell.Section id={DISCREPANCIES_SECTION_ID} title={DISCREPANCIES_SECTION_TITLE}>
        <DiscrepancyResolver />
      </PageShell.Section>
      <PageShell.Section id={DECISION_LOG_SECTION_ID} title={DECISION_LOG_SECTION_TITLE}>
        <DecisionLog />
      </PageShell.Section>
    </Stack>
  );
}
