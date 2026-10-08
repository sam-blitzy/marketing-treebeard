/**
 * The per-source chips in the `/actions` status bar (AAP 0.5.5): for Google, LinkedIn and
 * HubSpot, in that order, the source's current mode (Live or Simulated), a Stale and an
 * Incomplete chip when any of its datasets is stale or not complete now, and the time of its
 * last successful sync.
 *
 * Where each part comes from:
 *
 * - **Mode, Stale, Incomplete**: the read-time overlay of `GET /recommendations`
 *   (`Overlay.source_modes`, `Overlay.stale_datasets`, `Overlay.incomplete_datasets`;
 *   AAP 0.3.2), never the run's frozen `data_modes`, so the chips are current even on a run
 *   computed before a source changed mode. This is the per-source "Simulated" chip of AAP 0.3.3
 *   Labelling. A dataset belongs to a source when its name starts with `{source}.`, as every
 *   freshness-tracked name does (`google.metrics_daily`, `linkedin.leads`, `hubspot.contacts`,
 *   …), so no dataset name is written into this module.
 * - **Last sync**: `GET /sources` through {@link useSources}, which polls every 30 s. It is the
 *   newest `last_success_at` across the freshness rows of the source's active primary dataset
 *   ({@link latestSuccess}), formatted in the display time zone the caller passes
 *   (`Run.display_timezone`).
 *
 * Presentation: a list labelled "Data sources", one item per source, each starting with the
 * source's name so a screen reader announces it before its chips. Chips are text labels only
 * (AAP 0.4.5) and their tone only repeats what the text says. Nothing here is a live region and
 * nothing carries `role="alert"`, which AAP 0.5.5 reserves for discrepancies. UI comes only from
 * the design-system index (`Inline`, `Badge`), with no styles of this module's own (AAP 0.4.4).
 *
 * @packageDocumentation
 */
import { useSources } from '../../api/queries';
import type { DataMode, Overlay, Source, SourceName } from '../../api/types';
import { Badge, Inline } from '../../design-system';
import type { Tone } from '../../design-system';
import { formatInstant, sourceLabel } from '../../lib/format';

/**
 * The fixed display order of the source chips: Google, LinkedIn, HubSpot, the order
 * `GET /sources` and every other source list in the product use. The array is frozen, so no
 * caller can reorder it at run time.
 */
export const SOURCE_CHIP_ORDER: readonly SourceName[] = Object.freeze([
  'google',
  'linkedin',
  'hubspot',
] as const);

/**
 * Props of {@link SourceChips}.
 */
export interface SourceChipsProps {
  /**
   * The page-level current state, `RecommendationsPage.overlay`: its `source_modes`,
   * `stale_datasets` and `incomplete_datasets` drive the mode, Stale and Incomplete chips.
   */
  overlay: Overlay;
  /**
   * The IANA zone that instants are shown in: always `Run.display_timezone` (the server's
   * `DISPLAY_TIMEZONE`), never a literal. It only formats the last-sync instant and never moves
   * a day boundary (AAP 0.5.4.1).
   */
  displayTimezone: string;
}

/** The label and tone of one chip. */
interface ChipSpec {
  /** The chip's text, which carries its whole meaning. */
  label: string;
  /** The design-system tone that repeats the meaning in colour. */
  tone: Tone;
}

/** The Stale chip: a dataset of the source last succeeded too long ago or stops short. */
const STALE_CHIP: ChipSpec = Object.freeze({ label: 'Stale', tone: 'warning' });

/** The Incomplete chip: a dataset of the source is not `complete` over its range. */
const INCOMPLETE_CHIP: ChipSpec = Object.freeze({ label: 'Incomplete', tone: 'warning' });

/** Shown while `GET /sources` has failed and holds no earlier answer to fall back on. */
const LAST_SYNC_UNKNOWN = 'Last sync unknown';

/** Shown when the source's active primary dataset has no successful refresh yet. */
const NEVER_SYNCED = 'Never synced';

/**
 * The mode chip of a source.
 *
 * `live` is a success chip "Live" and `simulated` an info chip "Simulated" (AAP 0.3.3
 * Labelling). The overlay reports `null` for a source that has no dataset yet, before the
 * worker's first attempt; that case gets a neutral "No data yet" chip, so the chip never claims
 * a mode the source does not have. A value outside the contract is treated the same way.
 *
 * @param mode - the source's current mode from `Overlay.source_modes`
 * @returns the chip to render
 */
function modeChip(mode: DataMode | null): ChipSpec {
  switch (mode) {
    case 'live':
      return { label: 'Live', tone: 'success' };
    case 'simulated':
      return { label: 'Simulated', tone: 'info' };
    default:
      // The AAP names only Live and Simulated; a source before its first dataset is neither,
      // so it is labelled for what it is rather than given either mode.
      return { label: 'No data yet', tone: 'neutral' };
  }
}

/**
 * Whether any dataset name in `names` belongs to `source`, that is starts with `{source}.`.
 *
 * @param names - dataset names such as `Overlay.stale_datasets`
 * @param source - the source to test
 * @returns `true` when at least one name has the source's prefix
 */
function hasSourceDataset(names: readonly string[], source: SourceName): boolean {
  const prefix = `${source}.`;
  return names.some((name) => name.startsWith(prefix));
}

/**
 * The newest successful refresh of a source: the latest `last_success_at` across the freshness
 * rows of its active primary dataset (`Source.datasets`).
 *
 * - Instants are compared by `Date.parse`, never as strings, because two rows may carry
 *   different UTC offsets. A row with no success yet (`null`) or an unparsable value is
 *   skipped.
 * - The companion CRM dataset's rows (`Source.companion_datasets`) are not read: the chip
 *   reports the source's own data, and the companion belongs to simulated LinkedIn.
 * - A source with no active dataset has synced nothing that is in use, so it returns `null`.
 *
 * @param source - the source's entry from `GET /sources`, or `undefined` when the response does
 *   not list it
 * @returns the newest instant exactly as the API sent it, or `null` when there is none
 *
 * @example
 * ```ts
 * latestSuccess(makeSource('google')); // '2026-10-06T05:30:00Z'
 * latestSuccess(undefined); // null
 * ```
 */
export function latestSuccess(source: Source | undefined): string | null {
  if (source === undefined || source.active_dataset === null) {
    return null;
  }
  let newest: string | null = null;
  let newestMs = Number.NEGATIVE_INFINITY;
  for (const row of source.datasets) {
    const iso = row.last_success_at;
    if (iso === null) {
      continue;
    }
    const ms = Date.parse(iso);
    if (Number.isNaN(ms) || ms <= newestMs) {
      continue;
    }
    newest = iso;
    newestMs = ms;
  }
  return newest;
}

/**
 * The last-sync text of one source, or `null` when nothing should be shown.
 *
 * - While `GET /sources` has not answered yet, nothing is shown.
 * - Once it has answered, the newest success is shown as "Last sync {instant}", or
 *   "Never synced" when there is none. A later poll that fails keeps the last answer on screen,
 *   because that instant is still a true last success and a transient poll failure must not
 *   make the bar flicker every 30 s.
 * - When the request has failed and there is no earlier answer, it is "Last sync unknown".
 *
 * @param sources - the `GET /sources` data, `undefined` until the first answer
 * @param failed - whether the query is in its error state
 * @param source - the source of the chip
 * @param displayTimezone - the zone to format the instant in
 * @returns the text, or `null`
 */
function lastSyncText(
  sources: readonly Source[] | undefined,
  failed: boolean,
  source: SourceName,
  displayTimezone: string,
): string | null {
  if (sources === undefined) {
    return failed ? LAST_SYNC_UNKNOWN : null;
  }
  const newest = latestSuccess(sources.find((entry) => entry.source === source));
  return newest === null ? NEVER_SYNCED : `Last sync ${formatInstant(newest, displayTimezone)}`;
}

/** Props of {@link SourceChip}. */
interface SourceChipProps {
  /** The source shown. */
  source: SourceName;
  /** Its current mode from the overlay. */
  mode: DataMode | null;
  /** Whether any of its datasets is stale now. */
  stale: boolean;
  /** Whether any of its datasets is not complete now. */
  incomplete: boolean;
  /** Its last-sync text, or `null` to show none. */
  lastSync: string | null;
}

/**
 * One list item: the source's name first, then its mode chip, its Stale and Incomplete chips
 * when they apply, and its last-sync text, wrapping onto further lines on narrow screens.
 *
 * @param props - see {@link SourceChipProps}
 * @returns the `<li>` of the source
 */
function SourceChip({ source, mode, stale, incomplete, lastSync }: SourceChipProps) {
  const modeSpec = modeChip(mode);
  return (
    <li>
      <Inline gap={2}>
        <span>{sourceLabel(source)}</span>
        <Badge tone={modeSpec.tone}>{modeSpec.label}</Badge>
        {stale && <Badge tone={STALE_CHIP.tone}>{STALE_CHIP.label}</Badge>}
        {incomplete && <Badge tone={INCOMPLETE_CHIP.tone}>{INCOMPLETE_CHIP.label}</Badge>}
        {lastSync !== null && <span>{lastSync}</span>}
      </Inline>
    </li>
  );
}

/**
 * The source chips of the status bar: a list labelled "Data sources" with one item per source
 * in {@link SOURCE_CHIP_ORDER}.
 *
 * Each item reads, in order: the source name ("Google", "LinkedIn", "HubSpot"); a mode chip,
 * "Live" (success) or "Simulated" (info), or "No data yet" (neutral) before the source has a
 * dataset; "Stale" (warning) when any entry of `overlay.stale_datasets` starts with
 * `{source}.`; "Incomplete" (warning) likewise for `overlay.incomplete_datasets`; and
 * "Last sync {instant in displayTimezone}", "Never synced" or "Last sync unknown" (see
 * {@link latestSuccess}). The last-sync text is absent until `GET /sources` first answers.
 *
 * The component reads server state only through {@link useSources}, so it must render inside
 * the app's `QueryClientProvider`. It announces nothing: the chips are static content read in
 * document order.
 *
 * @param props - see {@link SourceChipsProps}
 * @returns the labelled list of source chips
 *
 * @example
 * ```tsx
 * <SourceChips overlay={page.overlay} displayTimezone={page.run.display_timezone} />
 * ```
 */
export function SourceChips({ overlay, displayTimezone }: SourceChipsProps) {
  const sources = useSources();
  return (
    <Inline as="ul" gap={4} aria-label="Data sources">
      {SOURCE_CHIP_ORDER.map((source) => (
        <SourceChip
          key={source}
          source={source}
          mode={overlay.source_modes[source]}
          stale={hasSourceDataset(overlay.stale_datasets, source)}
          incomplete={hasSourceDataset(overlay.incomplete_datasets, source)}
          lastSync={lastSyncText(sources.data, sources.isError, source, displayTimezone)}
        />
      ))}
    </Inline>
  );
}
