/**
 * The global banners on `/actions` (AAP 0.5.5), rendered above the section navigation in a
 * fixed order:
 *
 * 1. open discrepancies, with the live count and a link to the Campaigns discrepancy group;
 * 2. tracking findings (a source spent with no recorded outcomes, AAP 0.5.4.2 P2);
 * 3. simulated sources, each with its reason (AAP 0.3.3 Labelling, P3);
 * 4. stale datasets, then incomplete datasets (P4);
 * 5. the short window, "Window of N days is below the {trusted_min_days}-day trusted minimum"
 *    (P5).
 *
 * The API builds every banner from the run plus the read-time overlay (AAP 0.5.3), so the
 * texts are current when the page loads. This component only orders and presents them:
 *
 * - **Order**: {@link BANNER_ORDER} mirrors the backend's `analysis.context.BANNER_ORDER`, and
 *   {@link orderBanners} re-applies it whatever order the banners arrive in, so "the discrepancy
 *   banner sits above everything" holds even for a payload assembled out of order. A code the
 *   client does not know yet is kept, after the known ones.
 * - **Live count**: the open-discrepancies title is computed from
 *   `Overlay.open_discrepancies`, the count of every unresolved discrepancy now, including one
 *   detected after the run (AAP 0.5.4.2 P1). With no open discrepancy the banner is not shown,
 *   so the page never reads "0 open discrepancies".
 * - **Live region**: only the open-discrepancies banner carries `role="alert"` (AAP 0.5.5:
 *   "Banners use the `alert` role only for discrepancies"); every other banner is silent
 *   content. Its "Review discrepancies" action is a native in-page link, so it is reachable and
 *   operable from the keyboard.
 * - **Text**: every other banner's title is the API's own message. No threshold, count, window
 *   length or source name is written into the client; the short-window message already names
 *   the configured `trusted_min_days`, and the simulated-sources message names each simulated
 *   source with its reason, such as "credentials not configured".
 *
 * Presentational only: the page passes `banners` and `overlay` from one `GET /recommendations`
 * response, and this module calls no data hook. UI comes only from the design-system index:
 * `Stack` spaces the banners and `Banner` renders each one, with no styles of their own
 * (AAP 0.4.4).
 *
 * @packageDocumentation
 */
import type { ReactElement } from 'react';

import type { Banner as ApiBanner, BannerCode, BannerTone, Overlay } from '../../api/types';
import { Banner, Stack, TONES } from '../../design-system';

/**
 * The fixed render order of the global banners (AAP 0.5.5), as the API's banner codes.
 *
 * Each code mirrors a constant in `backend/src/ads_engine/analysis/context.py`, whose
 * `BANNER_ORDER` tuple lists them in this same order; `analysis/overlay.py` builds the banners
 * and `api/routes/recommendations.py` returns them unchanged:
 *
 * 1. `open_discrepancies`: `BANNER_OPEN_DISCREPANCIES`, the only `alert` banner;
 * 2. `tracking_absent`: `BANNER_TRACKING_ABSENT`, built from the run's
 *    `conversion_tracking_absent` findings;
 * 3. `simulated_sources`: `BANNER_SIMULATED_SOURCES`;
 * 4. `stale_datasets`: `BANNER_STALE_DATASETS`, then `incomplete_datasets`:
 *    `BANNER_INCOMPLETE_DATASETS` (the AAP's single "stale or incomplete" slot, stale first);
 * 5. `short_window`: `BANNER_SHORT_WINDOW`.
 *
 * The array is frozen, so no caller can reorder it at run time.
 */
export const BANNER_ORDER: readonly BannerCode[] = Object.freeze([
  'open_discrepancies',
  'tracking_absent',
  'simulated_sources',
  'stale_datasets',
  'incomplete_datasets',
  'short_window',
] as const);

/** The open-discrepancies banner code (`analysis.context.BANNER_OPEN_DISCREPANCIES`). */
const DISCREPANCY_CODE: BannerCode = 'open_discrepancies';

/** The open-discrepancies banner's element id, a target for in-page links. */
const DISCREPANCY_BANNER_ID = 'banner-discrepancies';

/**
 * The Campaigns section's discrepancy group. The API always returns that static group, empty
 * or not, so the anchor resolves whatever the run produced (`api/routes/recommendations.py`).
 */
const DISCREPANCY_GROUP_HREF = '#group-campaigns-discrepancies';

/** The open-discrepancies banner's action label. */
const REVIEW_DISCREPANCIES_LABEL = 'Review discrepancies';

/** The tone a banner falls back to when the API sends one the design system does not know. */
const FALLBACK_TONE: BannerTone = 'neutral';

/** Each known code's position in {@link BANNER_ORDER}. */
const BANNER_RANK: ReadonlyMap<string, number> = new Map(
  BANNER_ORDER.map((code, index) => [code, index]),
);

/** Formats the discrepancy count in English, with grouping for large counts ("1,204"). */
const COUNT_FORMAT = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

/**
 * Returns a copy of `banners` in {@link BANNER_ORDER}.
 *
 * The sort is stable: banners that share a position keep their API order, and banners whose
 * code is not in {@link BANNER_ORDER} follow every known banner, in their API order, so a code
 * the backend adds later is still shown rather than dropped. The input array is never mutated.
 *
 * @param banners - the banners as the API returned them
 * @returns a new array holding the same banner objects, in render order
 *
 * @example
 * ```ts
 * orderBanners([shortWindow, openDiscrepancies, futureCode]);
 * // → [openDiscrepancies, shortWindow, futureCode]
 * ```
 */
export function orderBanners(banners: readonly ApiBanner[]): ApiBanner[] {
  const unknownRank = BANNER_ORDER.length;
  return banners
    .map((banner, index) => ({
      banner,
      index,
      rank: BANNER_RANK.get(banner.code) ?? unknownRank,
    }))
    .sort((left, right) => left.rank - right.rank || left.index - right.index)
    .map(({ banner }) => banner);
}

/**
 * Props of {@link GlobalBanners}.
 *
 * Both values come from the same `GET /recommendations` response (`RecommendationsPage`), so
 * the banner list and the live counts describe one moment.
 */
export interface GlobalBannersProps {
  /** The page's global banners (`RecommendationsPage.banners`), in any order. */
  banners: readonly ApiBanner[];
  /**
   * The page-level current state (`RecommendationsPage.overlay`); its `open_discrepancies` is
   * the live count the open-discrepancies banner's title reports.
   */
  overlay: Overlay;
}

/**
 * The live open-discrepancy count, or `0` when the overlay carries no usable count.
 *
 * Anything other than a positive whole number (zero, a negative or non-finite value) reads as
 * no open discrepancy, so the banner is never titled with a count that is not a real one.
 */
function liveDiscrepancyCount(overlay: Overlay): number {
  const count = overlay.open_discrepancies;
  return Number.isInteger(count) && count > 0 ? count : 0;
}

/**
 * The open-discrepancies title for a live count: "1 open discrepancy to resolve first" or
 * "3 open discrepancies to resolve first".
 */
function discrepancyTitle(count: number): string {
  const noun = count === 1 ? 'discrepancy' : 'discrepancies';
  return `${COUNT_FORMAT.format(count)} open ${noun} to resolve first`;
}

/**
 * Returns `message` trimmed, or `null` when it is not a string or holds only whitespace, so a
 * banner never renders an empty title or the text "null" or "undefined".
 */
function messageText(message: unknown): string | null {
  if (typeof message !== 'string') {
    return null;
  }
  const trimmed = message.trim();
  return trimmed === '' ? null : trimmed;
}

/** Whether `tone` is one of the design system's tones (`TONES`). */
function isTone(tone: unknown): tone is BannerTone {
  return typeof tone === 'string' && (TONES as readonly string[]).includes(tone);
}

/**
 * The global banners of `/actions`, in the fixed order of AAP 0.5.5.
 *
 * Renders one design-system `Banner` per banner, inside a vertical `Stack`:
 *
 * - **Open discrepancies**: tone `critical`, `role="alert"` (the only banner with it) and
 *   `id="banner-discrepancies"`. The title reports the live count from
 *   `overlay.open_discrepancies` ("{n} open discrepancies to resolve first"), the content is
 *   the API's message, and the action is a "Review discrepancies" link to
 *   `#group-campaigns-discrepancies`. It is skipped while the live count is `0`, and shown at
 *   most once.
 * - **Every other banner**: the API's tone, with its message as the title, no content, no
 *   action and no live region. A banner with a blank message has nothing to say and is
 *   skipped; an unknown tone renders as `neutral`.
 *
 * Returns `null` when no banner is left to show, so the page keeps no empty container.
 *
 * The component holds no state and has no effects; its output depends only on its props.
 *
 * @example
 * ```tsx
 * <GlobalBanners banners={page.banners} overlay={page.overlay} />
 * ```
 */
export function GlobalBanners({ banners, overlay }: GlobalBannersProps): ReactElement | null {
  const openDiscrepancies = liveDiscrepancyCount(overlay);
  const occurrences = new Map<string, number>();
  const rendered: ReactElement[] = [];
  let discrepancyShown = false;

  for (const banner of orderBanners(banners)) {
    // The API sends each code at most once; a repeat still gets a key of its own.
    const occurrence = occurrences.get(banner.code) ?? 0;
    occurrences.set(banner.code, occurrence + 1);
    const key = occurrence === 0 ? banner.code : `${banner.code}#${occurrence}`;
    const message = messageText(banner.message);

    if (banner.code === DISCREPANCY_CODE) {
      // One alert at most, and only while the live count says something is open.
      if (discrepancyShown || openDiscrepancies === 0) {
        continue;
      }
      discrepancyShown = true;
      rendered.push(
        <Banner
          key={key}
          tone="critical"
          alert
          id={DISCREPANCY_BANNER_ID}
          title={discrepancyTitle(openDiscrepancies)}
          actions={<a href={DISCREPANCY_GROUP_HREF}>{REVIEW_DISCREPANCIES_LABEL}</a>}
        >
          {message}
        </Banner>,
      );
      continue;
    }

    if (message === null) {
      continue;
    }
    rendered.push(
      <Banner key={key} tone={isTone(banner.tone) ? banner.tone : FALLBACK_TONE} title={message} />,
    );
  }

  if (rendered.length === 0) {
    return null;
  }
  return <Stack>{rendered}</Stack>;
}
