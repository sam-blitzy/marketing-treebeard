/**
 * The custom-window notice on `/actions` (AAP 0.5.5, Window picker).
 *
 * Choosing any window other than the configured default computes a custom run (`POST /runs`)
 * and opens it at `/actions?run=<id>`. The page then shows this banner: "Custom window
 * {start} – {end}", the window's length and a "Back to default" link. The default view is
 * always the latest succeeded default run and a custom run is addressed only by its id
 * (AAP 0.5.3, run selection), so the link returns to `/actions` with no `run` search param.
 *
 * Presentation rules this file keeps:
 * - The dates are the run's calendar dates exactly as stored, joined by `formatDateRange`
 *   from `lib/format.ts`. A window is one calendar-date range applied to each source's native
 *   days (AAP 0.5.4.1), so the dates are never parsed, reformatted or shifted into
 *   `DISPLAY_TIMEZONE`; the status bar names each source's day boundary separately.
 * - The length comes from `Run.days`, phrased by `formatDays` from `lib/format.ts`; no window
 *   length is written into the web client (AAP 0.5.5).
 * - The banner is plain content with tone `info` and no `alert`: AAP 0.5.5 reserves
 *   `role="alert"` for the open-discrepancies banner.
 * - UI comes only from the design-system index and navigation only from the TanStack Router
 *   `Link`, so the move is client-side and keyboard-operable as a native anchor (AAP 0.4.4).
 *   `src/router.tsx` is never imported: it imports the feature pages, and its `Register`
 *   augmentation types `to` and `search` here without an import.
 *
 * @packageDocumentation
 */
import { Link } from '@tanstack/react-router';

import type { Run } from '../../api/types';
import { Banner } from '../../design-system';
import { formatDateRange, formatDays } from '../../lib/format';

/**
 * Props of {@link CustomRunBanner}.
 *
 * The caller renders the banner only for a run with `kind === 'custom'`, the run that
 * `/actions?run=<id>` opened; the component renders nothing for a default run.
 */
export interface CustomRunBannerProps {
  /** The custom run on screen: its `kind`, `window_start`, `window_end` and `days` are read. */
  run: Run;
}

/**
 * Banner that names the custom window on screen and links back to the default run.
 *
 * Renders a design-system `Banner` with `id="banner-custom-window"`, tone `info` and no live
 * region. The title is "Custom window {window_start} – {window_end}" (`formatDateRange`: an en
 * dash between spaces, dates as stored), the content is the window's length from `formatDays`
 * ("1 day", "56 days"), and the action is a "Back to default" link to `/actions` with every
 * search param cleared, which shows the latest default run again.
 *
 * Returns `null` for a run whose `kind` is not `custom`: a default run needs no way back, so a
 * caller that passes one by mistake shows nothing rather than a false notice.
 *
 * The component holds no state and has no effects; its output depends only on `run`. It must
 * render inside a TanStack `RouterProvider`, as every `/actions` screen does.
 *
 * @example
 * ```tsx
 * {run.kind === 'custom' && <CustomRunBanner run={run} />}
 * ```
 */
export function CustomRunBanner({ run }: CustomRunBannerProps) {
  if (run.kind !== 'custom') {
    return null;
  }

  return (
    <Banner
      tone="info"
      id="banner-custom-window"
      title={`Custom window ${formatDateRange(run.window_start, run.window_end)}`}
      actions={
        // Exact matching: by default the router compares search params partially, so the
        // empty target search `{}` would match `?run=<id>` and mark this link
        // `aria-current="page"` on the custom run, announcing it as the current page when it
        // leads to a different view.
        <Link to="/actions" search={{}} activeOptions={{ exact: true }}>
          Back to default
        </Link>
      }
    >
      {/* A custom window may span a single day (the span runs from 1 day to
          `window.max_days`), so `formatDays` keeps the singular "1 day", never "1 days". */}
      {formatDays(run.days)}
    </Banner>
  );
}
