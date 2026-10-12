/**
 * The run notices on `/actions` (AAP 0.5.5, Window picker): the custom-window notice and the
 * older-default-run notice.
 *
 * Choosing any window other than the configured default computes a custom run (`POST /runs`)
 * and opens it at `/actions?run=<id>`. The page then shows this banner: "Custom window
 * {start} – {end}", the window's length and a "Back to default" link, as the run notice that
 * `GlobalBanners` renders right after the open-discrepancies banner. The default view is
 * always the latest succeeded default run and a custom run is addressed only by its id
 * (AAP 0.5.3, run selection), so the link returns to `/actions` with no `run` search param.
 *
 * A default run can be addressed by id as well (`/actions?run=<id>`). Once a later default run
 * has succeeded, that run is no longer what the default view shows, so the page shows
 * {@link OlderDefaultRunBanner} for it ("Older default run {start} – {end}", its length, "not
 * the latest default run" and "Back to default"), decided by {@link isOlderDefaultRun}.
 *
 * Presentation rules this file keeps:
 * - The dates are the run's calendar dates exactly as stored, joined by `formatDateRange`
 *   from `lib/format.ts`. A window is one calendar-date range applied to each source's native
 *   days (AAP 0.5.4.1), so the dates are never parsed, reformatted or shifted into
 *   `DISPLAY_TIMEZONE`; the status bar names each source's day boundary separately.
 * - The length comes from `Run.days`, phrased by `formatDays` from `lib/format.ts`; no window
 *   length is written into the web client (AAP 0.5.5).
 * - Each banner is plain content with tone `info` and no `alert`: AAP 0.5.5 reserves
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
 * // The page's run notice, which `GlobalBanners` places right after the discrepancy banner:
 * <GlobalBanners
 *   banners={page.banners}
 *   overlay={page.overlay}
 *   runNotice={run.kind === 'custom' ? <CustomRunBanner run={run} /> : null}
 * />
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

/**
 * The instant a run finished, in milliseconds since the epoch, or `null` when `finished_at` is
 * `null` or does not parse as an instant.
 */
function finishedInstant(run: Run): number | null {
  if (run.finished_at === null) {
    return null;
  }
  const instant = Date.parse(run.finished_at);
  return Number.isNaN(instant) ? null : instant;
}

/**
 * Whether `candidate` comes before `other` in the backend's latest-default order
 * (`analysis/runner.py` `latest_default_run`): `finished_at` newest first with a missing
 * instant last, then the higher id first.
 */
function isNewerDefaultRun(candidate: Run, other: Run): boolean {
  const candidateFinished = finishedInstant(candidate);
  const otherFinished = finishedInstant(other);
  if (candidateFinished !== otherFinished) {
    if (candidateFinished === null) {
      return false;
    }
    return otherFinished === null || candidateFinished > otherFinished;
  }
  return candidate.id > other.id;
}

/**
 * Whether `run` is a default run that is no longer the latest default run.
 *
 * True only when `run` is a `default` run, `latest` is a different run, and `latest` comes
 * before `run` in the order the backend picks the latest default run by
 * (`analysis/runner.py` `latest_default_run`: `finished_at` newest first, a missing instant
 * last, then the higher id). Instants are compared with `Date.parse`; a `finished_at` that is
 * `null` or does not parse sorts as the oldest, and equal instants fall back to the ids.
 *
 * Comparing the order, rather than only the ids, keeps a run that finished after the copy of
 * `GET /runs/latest` the client holds from being called older before that copy is read again.
 *
 * @param run - the run on screen
 * @param latest - the latest succeeded default run, as `GET /runs/latest` returned it
 * @returns `true` when `run` is an older default run
 *
 * @example
 * ```ts
 * isOlderDefaultRun(makeRun({ id: 6, finished_at: '2026-10-01T06:00:00Z' }), latest); // true
 * isOlderDefaultRun(latest, latest); // false
 * ```
 */
export function isOlderDefaultRun(run: Run, latest: Run): boolean {
  return run.kind === 'default' && run.id !== latest.id && isNewerDefaultRun(latest, run);
}

/**
 * Props of {@link OlderDefaultRunBanner}.
 *
 * The caller renders the banner only for a default run that {@link isOlderDefaultRun} reports
 * as older than the latest one; the component renders nothing for a custom run.
 */
export interface OlderDefaultRunBannerProps {
  /** The older default run on screen: its `kind`, `window_start`, `window_end` and `days`. */
  run: Run;
}

/**
 * Banner that marks an older default run, opened by id, as not the latest default run and links
 * back to the latest one.
 *
 * Renders a design-system `Banner` with `id="banner-older-default-run"`, tone `info` and no
 * live region. The title is "Older default run {window_start} – {window_end}"
 * (`formatDateRange`, dates as stored), the content is the window's length from `formatDays`
 * followed by "not the latest default run" ("28 days · not the latest default run"), and the
 * action is a "Back to default" link to `/actions` with every search param cleared, which shows
 * the latest default run.
 *
 * Returns `null` for a run whose `kind` is not `default`: a custom run has
 * {@link CustomRunBanner} instead.
 *
 * The component holds no state and has no effects; its output depends only on `run`. It must
 * render inside a TanStack `RouterProvider`, as every `/actions` screen does.
 *
 * @example
 * ```tsx
 * {isOlderDefaultRun(run, latest) && <OlderDefaultRunBanner run={run} />}
 * ```
 */
export function OlderDefaultRunBanner({ run }: OlderDefaultRunBannerProps) {
  if (run.kind !== 'default') {
    return null;
  }

  return (
    <Banner
      tone="info"
      id="banner-older-default-run"
      title={`Older default run ${formatDateRange(run.window_start, run.window_end)}`}
      actions={
        // Exact matching, as in CustomRunBanner: the empty target search `{}` would otherwise
        // match `?run=<id>` and mark this link `aria-current="page"` on the older run.
        <Link to="/actions" search={{}} activeOptions={{ exact: true }}>
          Back to default
        </Link>
      }
    >
      {`${formatDays(run.days)} · not the latest default run`}
    </Banner>
  );
}
