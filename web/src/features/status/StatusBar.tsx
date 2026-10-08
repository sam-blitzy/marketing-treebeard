/**
 * The status bar at the top of `/actions` (AAP 0.5.5, `/actions` layout): what the ranked
 * actions below it were read over, and how current their sources are.
 *
 * Three rows, top to bottom:
 *
 * 1. **Window picker** ({@link WindowPicker}): the configured presets, the default marked, and a
 *    custom range. Choosing a window opens the default run or computes a custom one.
 * 2. **Day boundaries and display time zone**: each source's native day boundary from
 *    `Run.source_days`, for example "Google days: America/New_York · LinkedIn days: UTC", and
 *    "Times shown in {Run.display_timezone}". AAP 0.5.4.1 keeps every source's daily rows on
 *    their own native day and uses `DISPLAY_TIMEZONE` only to format instants, never to move a
 *    day boundary, so the bar states both. When the sources end their days at different
 *    boundaries it also says so, so misaligned daily buckets are never read as one common day.
 * 3. **Source chips** ({@link SourceChips}): Live, Simulated, Stale or Incomplete per source,
 *    with its last successful sync, from the read-time overlay.
 *
 * Rules this file keeps:
 * - **Presentational.** It calls no data hook. The children fetch what they need through the
 *   TanStack Query hooks (AAP 0.11.1), and every value shown here comes from the `run` and
 *   `overlay` props.
 * - **No time zone or boundary in the client.** Every zone shown is the API's own value, and
 *   the boundary line is built only by `formatSourceBoundaries` in `src/lib/format.ts`.
 * - **Design system only** (AAP 0.4.4). UI comes from the design-system index, and layout only
 *   from `Stack` and `Inline`, whose wrapping reflows the bar on narrow screens. There are no
 *   styles, raw headings or interactive elements of this module's own. The bar is a labelled
 *   region rather than a headed one, because feature code never renders a raw heading.
 * - **No alert.** Nothing here carries `role="alert"`, which AAP 0.5.5 reserves for the
 *   open-discrepancies banner.
 *
 * @packageDocumentation
 */
import type { Overlay, Run } from '../../api/types';
import { Inline, Stack } from '../../design-system';
import { boundariesDiffer, formatSourceBoundaries } from '../../lib/format';
import { SourceChips } from './SourceChips';
import { WindowPicker } from './WindowPicker';

/**
 * The note shown when the run's sources end their days at different boundaries. One calendar
 * date then covers different instants in each source (AAP 0.5.4.1).
 */
const BOUNDARIES_DIFFER_NOTE = 'Day boundaries differ between sources';

/**
 * Props of {@link StatusBar}.
 */
export interface StatusBarProps {
  /**
   * The run being shown: the latest succeeded default run at `/actions`, or the custom run that
   * `/actions?run=<id>` opened. Its window drives the picker, its `source_days` the boundary
   * line, and its `display_timezone` the zone that every instant in the bar is formatted in.
   */
  run: Run;
  /**
   * The page-level current state, `RecommendationsPage.overlay`, computed at read time
   * (AAP 0.3.2). The source chips read their mode, Stale and Incomplete state from it.
   */
  overlay: Overlay;
}

/**
 * The `/actions` status bar: a region named "Analysis status" holding the window picker, the
 * per-source day boundaries with the display time zone, and the source chips, in that order.
 *
 * - The boundary line is exactly `formatSourceBoundaries(run.source_days)`. It is left out when
 *   the run lists no source days, so the bar never shows an empty label.
 * - "Day boundaries differ between sources" follows it when `boundariesDiffer` reports more
 *   than one distinct boundary, for example Google in `America/New_York` and LinkedIn in `UTC`.
 * - "Times shown in {run.display_timezone}" names the zone that the bar's instants, such as
 *   each source's last sync, are formatted in.
 *
 * The component holds no state and has no effects. Its children read server state and
 * navigate, so it must render inside the app's `QueryClientProvider` and TanStack
 * `RouterProvider`, as every `/actions` screen does.
 *
 * @param props - see {@link StatusBarProps}
 * @returns the labelled status region
 *
 * @example
 * ```tsx
 * <StatusBar run={page.run} overlay={page.overlay} />
 * ```
 */
export function StatusBar({ run, overlay }: StatusBarProps) {
  const boundaries = formatSourceBoundaries(run.source_days);
  return (
    <Stack as="section" gap={3} aria-label="Analysis status">
      <WindowPicker run={run} />
      <Inline gap={4}>
        {boundaries !== '' && <span>{boundaries}</span>}
        {boundariesDiffer(run.source_days) && <span>{BOUNDARIES_DIFFER_NOTE}</span>}
        <span>{`Times shown in ${run.display_timezone}`}</span>
      </Inline>
      <SourceChips overlay={overlay} displayTimezone={run.display_timezone} />
    </Stack>
  );
}
