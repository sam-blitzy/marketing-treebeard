/**
 * `WhitePapersSection`: the fifth `/actions` section, one table of white papers per currency
 * cohort (AAP 0.5.5 White papers, AAP 0.5.4.7 attribution chain, FR-8).
 *
 * Inside the shared {@link SectionFrame} (findings first, withheld lines and suppression counts
 * after the groups), each `cohort:{CUR}` group renders:
 *
 * 1. a `DataTable` captioned "White papers ({currency})" with one row per visible asset, in the
 *    order the API ranked them, and these columns exactly in this order: Asset, Spend, LinkedIn
 *    leads, MQL headed with the bound stage label, for example "MQL (First meeting booked)",
 *    Qualified Lead, Meetings booked, Other meetings (ordering unverified), Deals after touch,
 *    Cost per Qualified Lead, Cost per meeting, Action. The MQL label is the one the rows'
 *    frozen evidence records ({@link tableMqlHeader}), so the current configuration
 *    ({@link mqlHeader}) heads a table only when no row of it records one. The Asset cell
 *    carries the row's simulated-data badge ({@link AssetCell}), so a collapsed row of
 *    simulated or mixed data says so;
 * 2. each row expands ("Details for {asset}") to its per-creative rows, "unknown creative"
 *    included, then the reason, the evidence it was judged on in the shared
 *    {@link EvidenceTable} (metrics against baselines, sample, test, thresholds applied, data
 *    used), the flags (floor flags among them), the window chip, any hold or replaced-data
 *    notice and the footer actions ("Mark done", "Dismiss", "Snooze for 7 days": white-paper
 *    rows carry no decision subject);
 * 3. rows computed from replaced data (AAP 0.3.2: readable, every action disabled), never
 *    collapsed, in a table of their own captioned "White papers ({currency}) computed from
 *    replaced data", each with its replaced-data badge in the Action cell;
 * 4. rows a discrepancy holds, collapsed behind the frame's {@link HeldDisclosure}, in a table
 *    captioned "White papers ({currency}) held until discrepancies are resolved";
 * 5. the cohort's notes: its spend total in its own currency, as the backend computed it, the
 *    note that counts overlap across assets, and what the LinkedIn leads and meetings booked
 *    columns count.
 *
 * Each asset table and each per-creative table shows `ROW_BATCH_SIZE` rows at a time, in the
 * API's order, followed by a "Show {n} more" control when it holds more (`../batches`;
 * AAP 0.1.1 "the five sections render from precomputed runs in under 2 s"), so a cohort of
 * hundreds of assets, or an asset of hundreds of creatives, mounts a bounded table.
 *
 * Rendering rules (AAP 0.5.5, 0.5.4.7), all decided by the backend and kept here:
 * - floors render "≥ N" for counts and "≤ X" for costs ({@link formatMetric});
 * - an unavailable count renders "unavailable", never 0;
 * - a cost whose known denominator is 0 renders "—" followed by its reason, "— (no QL yet)" or
 *   "— (no meetings yet)", never a finite value or a ceiling;
 * - MQL and Qualified Lead are always two columns: no column, total or sort key combines them;
 * - LinkedIn leads are platform counts and are never added to a CRM count;
 * - nothing is summed or compared across currencies, and the client sums nothing at all: the
 *   only total shown is the backend's spend total of one cohort, beside "counts overlap across
 *   assets", and no MQL, QL, meeting or deal total exists anywhere.
 *
 * Built only on the design-system index (AAP 0.4.4): `DataTable`, `Badge`, `Stack` and
 * `Inline`, with no raw tables, buttons or headings, no inline styles and no stylesheet. Server
 * state comes only from `useConfig()` (AAP 0.11.1), for the fallback MQL header, and evidence is
 * read only through the guarded readers of `../evidenceExtra`.
 *
 * @packageDocumentation
 */
import { useMemo } from 'react';
import type { ReactElement } from 'react';

import { useConfig } from '../../../api/queries';
import type {
  ConfigView,
  CrmBindingsView,
  DataStatus,
  Flag,
  Item,
  Metric,
} from '../../../api/types';
import { Badge, DataTable, Inline, Stack } from '../../../design-system';
import type { DataTableColumn } from '../../../design-system';
import {
  EM_DASH,
  UNAVAILABLE,
  formatMetric,
  formatMoney,
  formatWindowChip,
} from '../../../lib/format';
import { EvidenceTable } from '../EvidenceTable';
import {
  WHITE_PAPER_METRIC_KEYS,
  cohortSummary,
  creativeRows,
  evidenceCurrency,
  whitePaperCells,
  whitePaperCostNotes,
  whitePaperMqlLabel,
  whitePaperMqlMetricLabel,
  type CohortSummary,
  type WhitePaperMetricKey,
  type WhitePaperRow,
} from '../evidenceExtra';
import { BlockingDiscrepancyLinks } from '../BlockingDiscrepancies';
import { FlagList, flagText, flagTone, mergeFlags } from '../FlagList';
import { BatchControl, BatchEntryAnchor, ROW_BATCH_SIZE, useBatches } from '../batches';
import type { BatchNoun } from '../batches';
import {
  HISTORICAL_LABEL,
  RecommendationActions,
  historicalProvenanceText,
} from '../RecommendationCard';
import {
  FieldList,
  HeldDisclosure,
  SectionFrame,
  isHeld,
  itemIdentity,
  itemKeys,
  type FieldItem,
  type GroupParts,
  type SectionComponentProps,
} from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/**
 * The MQL column header without a stage label: for a label that is blank or only repeats
 * "MQL", for a table whose rows recorded different labels, and, when no row recorded one, while
 * the configuration loads or when it names no label.
 */
const MQL_PLAIN = 'MQL';

/** The header of the identifying column of the asset tables. */
const ASSET_HEADER = 'Asset';

/** The header of the identifying column of the per-creative tables. */
const CREATIVE_HEADER = 'Creative';

/** The header of the asset tables' last column, the row's action. */
const ACTION_HEADER = 'Action';

/**
 * The header of every metric column except MQL, whose header carries the bound stage label
 * ({@link tableMqlHeader}). Typed over every other key, so a key added to `WHITE_PAPER_METRIC_KEYS`
 * fails the type check until it has a header. MQL and Qualified Lead are separate entries.
 */
const METRIC_HEADERS: Readonly<Record<Exclude<WhitePaperMetricKey, 'mql'>, string>> =
  Object.freeze({
    spend: 'Spend',
    linkedin_leads: 'LinkedIn leads',
    ql: 'Qualified Lead',
    meetings_booked: 'Meetings booked',
    other_meetings: 'Other meetings (ordering unverified)',
    deals_after_touch: 'Deals after touch',
    cost_per_ql: 'Cost per Qualified Lead',
    cost_per_meeting: 'Cost per meeting',
  });

/** The cross-asset note when the evidence carries none (`attribution.OVERLAP_NOTE`). */
const OVERLAP_NOTE_FALLBACK = 'counts overlap across assets';

/**
 * What the LinkedIn leads column counts (AAP 0.5.4.7 "Never blended": LinkedIn platform leads
 * are labelled as platform counts and never summed with CRM counts).
 */
const LINKEDIN_LEADS_NOTE =
  'LinkedIn leads: platform counts from LinkedIn, never added to HubSpot counts';

/**
 * What the Meetings booked column counts (AAP 0.5.4.7 "Causal ordering check": HubSpot records
 * no booking instant, so booking time is the meeting record's creation, "booked (record
 * created)").
 */
const MEETINGS_BOOKED_NOTE =
  "Meetings booked: booked (record created). HubSpot records no booking instant, so the meeting record's creation stands in for it";

/** The hold reason when a held row arrives without one (precedence P1, AAP 0.5.4.2). */
const HELD_REASON_FALLBACK = 'Resolve discrepancy first';

/** The per-creative table's empty message: an asset row whose evidence holds no creative row. */
const NO_CREATIVE_ROWS = 'No per-creative rows recorded for this white paper';

/**
 * The asset row's simulated-data label when its flags carry no `simulated` flag but its data
 * status is `simulated`: the backend's own wording without its source list
 * (`precedence._simulated_flag`, "Simulated data").
 */
const SIMULATED_DATA_FALLBACK = 'Simulated data';

/**
 * The asset row's simulated-data label when its flags carry no `simulated` flag but its data
 * status is `mixed`: the backend's own wording without its detail (`precedence._simulated_flag`,
 * "Joins live and simulated data").
 */
const MIXED_DATA_FALLBACK = 'Joins live and simulated data';

// ---------------------------------------------------------------------------------------------
// MQL header
// ---------------------------------------------------------------------------------------------

/** Trimmed text, or `null` for anything that is not a non-blank string. */
function textOrNull(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null;
  }
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/**
 * The bindings white-paper counts were read with. White papers are LinkedIn-attributed, so they
 * read the effective CRM dataset (AAP 0.3.3): the companion CRM dataset while LinkedIn is
 * simulated, which is exactly when `companion_crm_bindings` is set, and HubSpot's primary
 * dataset otherwise.
 */
function effectiveBindings(config: ConfigView): CrmBindingsView | null {
  return config.companion_crm_bindings ?? config.crm_bindings;
}

/**
 * An MQL header for a stage label, formatted as the backend heads its MQL metric
 * (`white_papers._mql_label`): "MQL ({label})", or plain "MQL" when the label is blank or only
 * repeats "MQL" (in any case).
 */
function headerForLabel(label: string | null | undefined): string {
  const text = textOrNull(label);
  if (text === null || text.toLowerCase() === MQL_PLAIN.toLowerCase()) {
    return MQL_PLAIN;
  }
  return `${MQL_PLAIN} (${text})`;
}

/**
 * The header of the white-paper MQL column as the **current** configuration binds it (AAP 0.5.5:
 * "MQL, headed with the bound stage label from config, for example 'MQL (First meeting
 * booked)'"). A table heads its MQL column with this only when none of its rows recorded the
 * label its run was bound to ({@link tableMqlHeader}): the frozen label is the configuration's
 * label at compute time, and frozen counts are never relabelled by a later binding (AAP 0.3.2).
 *
 * The label follows the precedence the backend heads its own MQL metric with
 * (`attribution._stage_label`, `white_papers._mql_label`), so the column and the evidence never
 * disagree:
 *
 * 1. the label the binding resolved to in the portal: the companion CRM's while LinkedIn is
 *    simulated, HubSpot's primary portal's otherwise;
 * 2. the configured label (`hubspot_mapping.mql.stage_labels[0]`), which `property` mode keeps
 *    for display only;
 * 3. the bound property: the resolved one, else the configured one (`property` mode).
 *
 * No label is written into the client. While the configuration loads (`undefined`), when no
 * label is configured, or when the label only repeats "MQL", the header is plain "MQL".
 *
 * @param config - `GET /config`, or `undefined` while it loads or after it failed
 * @returns "MQL ({label})", or "MQL"
 *
 * @example
 * ```ts
 * mqlHeader(undefined); // "MQL"
 * mqlHeader(config); // "MQL (First meeting booked)" with the shipped hubspot_mapping.yaml
 * ```
 */
export function mqlHeader(config: ConfigView | undefined): string {
  if (config === undefined) {
    return MQL_PLAIN;
  }
  const resolved = effectiveBindings(config)?.mql ?? null;
  const configured = config.hubspot_mapping.mql;
  return headerForLabel(
    textOrNull(resolved?.label) ??
      textOrNull(configured.stage_labels[0]) ??
      textOrNull(resolved?.property) ??
      textOrNull(configured.property),
  );
}

/**
 * The MQL header one row's frozen evidence records, the binding its run computed the counts
 * with (AAP 0.3.2: frozen evidence is never rewritten):
 *
 * 1. the recorded stage label (`extra.mql_label`), formatted as {@link mqlHeader} formats a
 *    label;
 * 2. else the recorded label of the `mql` metric, verbatim, which the backend already formats
 *    ("MQL (First meeting booked)", or "MQL" for a binding that named no label).
 *
 * `null` when the evidence records neither.
 */
function frozenMqlHeader(item: Item): string | null {
  const label = whitePaperMqlLabel(item);
  if (label !== null) {
    return headerForLabel(label);
  }
  return textOrNull(whitePaperMqlMetricLabel(item));
}

/**
 * The MQL header of one table, from the rows it holds, shown or not yet revealed: the frozen
 * header its rows recorded ({@link frozenMqlHeader}). Rows of one run share one binding; should
 * the rows of one table record different headers, the table claims none of them and heads the
 * column plainly "MQL", while each row's own label stays in its details. Only when no row
 * records a header does the column take `configured`, the current configuration's
 * ({@link mqlHeader}).
 */
function tableMqlHeader(items: readonly Item[], configured: string): string {
  const frozen = new Set<string>();
  for (const item of items) {
    const header = frozenMqlHeader(item);
    if (header !== null) {
      frozen.add(header);
    }
  }
  if (frozen.size === 0) {
    return configured;
  }
  const [only] = frozen;
  return frozen.size === 1 && only !== undefined ? only : MQL_PLAIN;
}

/** The header of one metric column. */
function metricHeader(key: WhitePaperMetricKey, mql: string): string {
  return key === 'mql' ? mql : METRIC_HEADERS[key];
}

// ---------------------------------------------------------------------------------------------
// Cells
// ---------------------------------------------------------------------------------------------

/** Whether a metric key is one of the two costs, whose "—" carries a reason. */
function isCostKey(key: WhitePaperMetricKey): boolean {
  return key === 'cost_per_ql' || key === 'cost_per_meeting';
}

/**
 * One white-paper cell as display text, through {@link formatMetric}: "≥ N" and "≤ X" under a
 * floor, "unavailable" for a missing or unavailable value (never 0), and the backend's "—" for a
 * cost whose known denominator is 0, followed by its reason when the evidence gives one: "— (no
 * QL yet)", "— (no meetings yet)".
 *
 * @param metric - the cell, as the guarded readers return it; absent reads as unavailable
 * @param note - why the value is missing, read only after a "—"
 * @returns the cell text
 */
function cellText(metric: Metric | null | undefined, note: string | null | undefined): string {
  if (metric === null || metric === undefined) {
    return UNAVAILABLE;
  }
  const text = formatMetric(metric);
  const reason = textOrNull(note);
  return text.trim() === EM_DASH && reason !== null ? `${EM_DASH} (${reason})` : text;
}

/** One asset row, formatted once for its table and its details. */
interface AssetRowView {
  /** The white-paper item the row shows. */
  readonly item: Item;
  /** The asset's display name: the row header and the per-creative caption. */
  readonly name: string;
  /** Each metric's display text, keyed by `WHITE_PAPER_METRIC_KEYS`. */
  readonly cells: ReadonlyMap<WhitePaperMetricKey, string>;
  /**
   * The id of the row header's focus target while its table shows rows in batches
   * (`BatchEntryAnchor`), so focus lands on the row when a reveal shows it first; `null` when
   * the table is not batched.
   */
  readonly focusId: string | null;
  /** The MQL header the row's frozen evidence records ({@link frozenMqlHeader}), or `null`. */
  readonly frozenMql: string | null;
}

/** The asset's display name: the entity name, else its id, so a row header is never blank. */
function assetName(item: Item): string {
  return textOrNull(item.entity.name) ?? textOrNull(item.entity.id) ?? `White paper ${item.id}`;
}

/**
 * Formats an asset row's cells from its evidence (`whitePaperCells`), with the cost notes of
 * `extra.costs` (`whitePaperCostNotes`) after a "—", and reads the MQL header it recorded. The
 * row has no focus target; {@link assetRowOf} gives one to a row of a batched table.
 */
function toAssetRow(item: Item): AssetRowView {
  const metrics = whitePaperCells(item);
  const costNotes = whitePaperCostNotes(item);
  const cells = new Map<WhitePaperMetricKey, string>();
  for (const key of WHITE_PAPER_METRIC_KEYS) {
    cells.set(key, cellText(metrics[key], isCostKey(key) ? costNotes[key] : null));
  }
  return { item, name: assetName(item), cells, focusId: null, frozenMql: frozenMqlHeader(item) };
}

/**
 * Each item's formatted asset row, kept while the item object lives. Items from
 * `GET /recommendations` are never changed in place: a refetch that leaves an item unchanged
 * hands back the same object, and a changed item arrives as a new one, so a kept row always
 * matches its item.
 */
const ASSET_ROWS = new WeakMap<Item, AssetRowView>();

/**
 * An item's asset row ({@link toAssetRow}), formatted once per item object, so a cohort that
 * renders again reformats only the rows whose item changed. `focusId` is the row header's focus
 * target in a batched table, `null` otherwise; a row given one shares the kept row's cells.
 */
function assetRowOf(item: Item, focusId: string | null = null): AssetRowView {
  let row = ASSET_ROWS.get(item);
  if (row === undefined) {
    row = toAssetRow(item);
    ASSET_ROWS.set(item, row);
  }
  return focusId === null ? row : { ...row, focusId };
}

/** One per-creative row with its position, so equal creative keys never collide. */
interface CreativeRowView {
  /** The row's position in the stored partition. */
  readonly index: number;
  /** The per-creative row. */
  readonly row: WhitePaperRow;
  /**
   * The id of the row header's focus target while the per-creative table shows rows in batches
   * (`BatchEntryAnchor`); `null` when it is not batched.
   */
  readonly focusId: string | null;
}

/** What an asset table counts in its batch control: "Showing 50 of 120 white papers". */
const ASSET_ROW_NOUN: BatchNoun = Object.freeze({
  singular: 'white paper',
  plural: 'white papers',
});

/** What a per-creative table counts in its batch control: "Show 50 more creatives". */
const CREATIVE_ROW_NOUN: BatchNoun = Object.freeze({ singular: 'creative', plural: 'creatives' });

// ---------------------------------------------------------------------------------------------
// Columns
// ---------------------------------------------------------------------------------------------

/** The fallback simulated-data label of a data status; `null` for `live` and `stale`. */
function statusLabel(status: DataStatus): string | null {
  if (status === 'simulated') {
    return SIMULATED_DATA_FALLBACK;
  }
  return status === 'mixed' ? MIXED_DATA_FALLBACK : null;
}

/**
 * The simulated-data flag an asset row shows on its collapsed row (FR-11: simulated data is
 * labelled everywhere; AAP 0.3.3: a "Simulated data" flag on each affected item).
 *
 * It is the `simulated` flag of the row's recorded and current flags, merged exactly as the
 * flags row in its details merges them ({@link mergeFlags}), and the Asset cell shows it with
 * the same badge text ({@link flagText}), so both say the same thing, for example "Simulated
 * data (LinkedIn, HubSpot)" or "Joins live and simulated data (HubSpot simulated)". The
 * recorded flag stays after the source goes live, because a row computed from simulated data
 * still says so (AAP 0.3.2). Only when neither set carries the flag does the row's data status
 * name it, recorded first, then current ({@link SIMULATED_DATA_FALLBACK},
 * {@link MIXED_DATA_FALLBACK}).
 *
 * @returns the flag, or `null` for a row computed from live data only
 */
function simulatedFlag(item: Item): Flag | null {
  const flag = mergeFlags(item.flags, item.current.flags_now).find(
    (each) => each.code === 'simulated',
  );
  if (flag !== undefined) {
    return flag;
  }
  const label = statusLabel(item.data_status) ?? statusLabel(item.current.data_status_now);
  return label === null ? null : { code: 'simulated', message: label };
}

/**
 * The Asset cell, the row header: the asset's name, then its simulated-data badge
 * ({@link simulatedFlag}) in the flag's tone, with the text the flags row gives the same flag
 * ({@link flagText}), so a collapsed row states that its numbers are
 * simulated without being expanded, in the cell that names the row. Captions and the expand
 * label keep the plain name. In a batched table the name is the row's focus target
 * ({@link BatchEntryAnchor}), so focus lands on the row when a reveal shows it first.
 */
function AssetCell({ row }: { row: AssetRowView }): ReactElement {
  const flag = simulatedFlag(row.item);
  const name = <BatchEntryAnchor id={row.focusId}>{row.name}</BatchEntryAnchor>;
  if (flag === null) {
    return name;
  }
  return (
    <Inline gap={2}>
      <span>{name}</span>
      <Badge tone={flagTone(flag.code)}>{flagText(flag)}</Badge>
    </Inline>
  );
}

/** The Action cell: the action, with the replaced-data badge on a historical row. */
function ActionCell({ item }: { item: Item }): ReactElement {
  if (!item.current.historical) {
    return <>{item.action}</>;
  }
  return (
    <Inline gap={2}>
      <span>{item.action}</span>
      <Badge tone="warning">{HISTORICAL_LABEL}</Badge>
    </Inline>
  );
}

/**
 * The asset table's columns, exactly in the order AAP 0.5.5 fixes: Asset (the row header),
 * the nine metric columns of `WHITE_PAPER_METRIC_KEYS` (numeric, aligned to the end), Action.
 */
function assetColumns(mql: string): DataTableColumn<AssetRowView>[] {
  return [
    {
      key: 'asset',
      header: ASSET_HEADER,
      isRowHeader: true,
      render: (row) => <AssetCell row={row} />,
    },
    ...WHITE_PAPER_METRIC_KEYS.map(
      (key): DataTableColumn<AssetRowView> => ({
        key,
        header: metricHeader(key, mql),
        align: 'end',
        render: (row) => row.cells.get(key) ?? UNAVAILABLE,
      }),
    ),
    { key: 'action', header: ACTION_HEADER, render: (row) => <ActionCell item={row.item} /> },
  ];
}

/**
 * The per-creative table's columns: Creative (the row header, "unknown creative" included),
 * then the same metric columns in the same order, without Action.
 */
function creativeColumns(mql: string): DataTableColumn<CreativeRowView>[] {
  return [
    {
      key: 'creative',
      header: CREATIVE_HEADER,
      isRowHeader: true,
      render: ({ row, focusId }) => <BatchEntryAnchor id={focusId}>{row.label}</BatchEntryAnchor>,
    },
    ...WHITE_PAPER_METRIC_KEYS.map(
      (key): DataTableColumn<CreativeRowView> => ({
        key,
        header: metricHeader(key, mql),
        align: 'end',
        render: ({ row }) => cellText(row.cells[key], row.notes[key]),
      }),
    ),
  ];
}

// ---------------------------------------------------------------------------------------------
// Row details
// ---------------------------------------------------------------------------------------------

/** Props of {@link AssetDetails}. */
interface AssetDetailsProps {
  /** The asset row to detail. */
  row: AssetRowView;
  /**
   * The per-creative table's MQL header: the header the asset row's frozen evidence records,
   * else the one its asset table is headed with.
   */
  mql: string;
}

/**
 * An asset row's expanded detail (AAP 0.5.5 "Each row expands to per-creative rows, including
 * 'unknown creative'"), top to bottom: the per-creative table captioned "Per creative:
 * {asset}", whose rows partition the asset's events; the reason sentence; the evidence it was
 * judged on, as every recommendation shows it ({@link EvidenceTable} captioned "Evidence for
 * {asset}", AAP 0.5.4.8): the cited metrics against their baselines (the cohort median cost per
 * Qualified Lead beside the cost per Qualified Lead), the sample and test lines, the thresholds
 * applied and the datasets used with their completeness and coverage; the flags, recorded and
 * current (floor flags among them); the window chip; a historical row's "Computed from replaced
 * data" badge with its original provenance, or a held row's hold reason with its link to the
 * blocking discrepancy; the footer actions, all disabled on a historical row.
 *
 * The per-creative table shows `ROW_BATCH_SIZE` rows at a time, in stored order, followed by a
 * "Show {n} more creatives" control when the asset has more (`../batches`), so an asset with
 * hundreds of creatives mounts a bounded table when it is expanded.
 */
function AssetDetails({ row, mql }: AssetDetailsProps): ReactElement {
  const { item, name } = row;
  const stored = useMemo(() => creativeRows(item), [item]);
  const batches = useBatches(stored, { batchSize: ROW_BATCH_SIZE });
  const creatives: CreativeRowView[] = batches.shown.map((creative, index) => ({
    index,
    row: creative,
    focusId: batches.batched ? batches.entryId(index) : null,
  }));
  const columns = useMemo(() => creativeColumns(mql), [mql]);
  const reason = textOrNull(item.reason);
  const historical = item.current.historical;
  // As on a card: a historical row is held by its replaced data, which its badge states, so the
  // discrepancy notice and link show only for a discrepancy hold.
  const heldReason =
    !historical && isHeld(item)
      ? (textOrNull(item.current.held_reason_now) ??
        textOrNull(item.held_reason) ??
        HELD_REASON_FALLBACK)
      : null;

  return (
    <Stack gap={3}>
      <DataTable
        caption={`Per creative: ${name}`}
        columns={columns}
        rows={creatives}
        getRowKey={(creative) => `${creative.index}-${creative.row.key}`}
        emptyMessage={NO_CREATIVE_ROWS}
      />
      <BatchControl batches={batches} noun={CREATIVE_ROW_NOUN} />
      {reason === null ? null : <div>{reason}</div>}
      <EvidenceTable evidence={item.evidence} caption={`Evidence for ${name}`} />
      <FlagList flags={item.flags} current={item.current.flags_now} />
      <Inline>
        <Badge tone="neutral">{formatWindowChip(item.evidence.window)}</Badge>
      </Inline>
      {historical ? (
        <Inline gap={2}>
          <Badge tone="warning">{HISTORICAL_LABEL}</Badge>
          <span>
            {historicalProvenanceText(item.evidence.provenance, item.current.data_status_now)}
          </span>
        </Inline>
      ) : null}
      {heldReason === null ? null : (
        <Inline gap={2}>
          <span>{heldReason}</span>
          <BlockingDiscrepancyLinks item={item} />
        </Inline>
      )}
      <RecommendationActions item={item} />
    </Stack>
  );
}

/** Props of {@link AssetTable}. */
interface AssetTableProps {
  /** The caption, which names the table. */
  caption: string;
  /** The white-paper items, one row each, in API order. */
  items: readonly Item[];
  /**
   * The MQL header the current configuration gives ({@link mqlHeader}), used only when no row
   * of the table records its frozen header ({@link tableMqlHeader}).
   */
  mql: string;
}

/**
 * One cohort's asset table, each row expandable to its {@link AssetDetails}. The MQL column is
 * headed from the rows' frozen evidence ({@link tableMqlHeader}), and each row's per-creative
 * table with that row's own frozen header, else the table's. The table formats its own rows
 * ({@link assetRowOf}) when it renders, so a table that is not mounted, such as the held rows'
 * table inside a disclosure nobody has opened, formats nothing, and a table that renders again
 * reformats only the rows whose item changed.
 *
 * It shows `ROW_BATCH_SIZE` rows at a time, in API order, followed by a "Show {n} more white
 * papers" control when the cohort has more (`../batches`): only the shown rows are formatted,
 * and a revealed row keeps the columns, caption and expansion of the rows before it. The MQL
 * header is taken from every row of the table, shown or not yet, so revealing rows never
 * renames the column.
 *
 * Rows are keyed by their recommendation's identity across runs ({@link itemKeys}), never by the
 * per-run item id, and the table keeps a row's expansion by that key. A newer run's row of the
 * same asset therefore stays expanded, and its expand toggle and details keep their elements, so
 * keyboard focus on them, and the footer actions' state, survive the swap (WCAG 2.2 SC 2.4.3).
 */
function AssetTable({ caption, items, mql }: AssetTableProps): ReactElement {
  const header = useMemo(() => tableMqlHeader(items, mql), [items, mql]);
  const columns = useMemo(() => assetColumns(header), [header]);
  const rowKeys = useMemo(() => itemKeys(items), [items]);
  const batches = useBatches(items, { batchSize: ROW_BATCH_SIZE });
  const rows = batches.shown.map((item, index) =>
    assetRowOf(item, batches.batched ? batches.entryId(index) : null),
  );
  return (
    <>
      <DataTable
        caption={caption}
        columns={columns}
        rows={rows}
        getRowKey={(row) => rowKeys.get(row.item) ?? itemIdentity(row.item)}
        renderExpanded={(row) => <AssetDetails row={row} mql={row.frozenMql ?? header} />}
        expandLabel={(row) => `Details for ${row.name}`}
      />
      <BatchControl batches={batches} noun={ASSET_ROW_NOUN} />
    </>
  );
}

/**
 * The fallback rendering of one white-paper row outside a table: the asset name with its
 * simulated-data badge ({@link AssetCell}), its cells as a labelled list in column order, then
 * its {@link AssetDetails}. `SectionFrame` builds it for its
 * default group body, which {@link WhitePapersSection} replaces with the cohort tables, so the
 * row stays readable wherever the frame falls back to it, held rows included. Its MQL label is
 * the one the row's frozen evidence records, else `mql`, the current configuration's.
 */
function WhitePaperItemFallback({ item, mql }: { item: Item; mql: string }): ReactElement {
  const row = assetRowOf(item);
  const header = row.frozenMql ?? mql;
  const fields: FieldItem[] = [
    ...WHITE_PAPER_METRIC_KEYS.map((key) => ({
      key,
      label: metricHeader(key, header),
      value: row.cells.get(key) ?? UNAVAILABLE,
    })),
    { key: 'action', label: ACTION_HEADER, value: item.action },
  ];
  return (
    <Stack gap={3}>
      <div>
        <AssetCell row={row} />
      </div>
      <FieldList fields={fields} label={`Figures for ${row.name}`} />
      <AssetDetails row={row} mql={header} />
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// Cohorts
// ---------------------------------------------------------------------------------------------

/** The first cross-asset view any row of the cohort carries (`extra.cohort`). */
function findCohortSummary(items: readonly Item[]): CohortSummary | null {
  for (const item of items) {
    const summary = cohortSummary(item);
    if (summary !== null) {
      return summary;
    }
  }
  return null;
}

/**
 * The cohort's currency: the cross-asset view's, else the first currency a row's evidence
 * names, else the group title the API gives it (the currency code), else its key.
 */
function cohortCurrency(parts: GroupParts, summary: CohortSummary | null): string {
  if (summary !== null) {
    return summary.currency;
  }
  for (const item of parts.group.items) {
    const currency = evidenceCurrency(item);
    if (currency !== null) {
      return currency;
    }
  }
  return textOrNull(parts.group.title) ?? parts.group.key;
}

/** Props of {@link CohortTables}. */
interface CohortTablesProps {
  /** The `cohort:{CUR}` group as the frame partitioned it. */
  parts: GroupParts;
  /**
   * The MQL header the current configuration gives ({@link mqlHeader}); each table uses it only
   * when none of its rows records its frozen header ({@link AssetTable}).
   */
  mql: string;
}

/**
 * One currency cohort (AAP 0.5.5, 0.5.4.7 "Currency cohorts"): the table of visible rows, the
 * open table of rows computed from replaced data, the held rows' table behind the held
 * disclosure, then the cohort's notes. Every amount is in the cohort's one currency. The spend
 * total is the backend's (`extra.cohort.spend_total`), shown as "unavailable" when absent and
 * never summed here; no other total is shown, because counts overlap across assets.
 */
function CohortTables({ parts, mql }: CohortTablesProps): ReactElement {
  const summary = findCohortSummary(parts.group.items);
  const currency = cohortCurrency(parts, summary);
  const overlapNote = textOrNull(summary?.overlapNote) ?? OVERLAP_NOTE_FALLBACK;

  return (
    <Stack gap={4}>
      {parts.visible.length > 0 ? (
        <AssetTable caption={`White papers (${currency})`} items={parts.visible} mql={mql} />
      ) : null}
      {parts.historical.length > 0 ? (
        <AssetTable
          caption={`White papers (${currency}) computed from replaced data`}
          items={parts.historical}
          mql={mql}
        />
      ) : null}
      {/* The held table formats its rows only once the disclosure is first opened. */}
      <HeldDisclosure id={`${parts.anchorId}-held`} count={parts.held.length}>
        <AssetTable
          caption={`White papers (${currency}) held until discrepancies are resolved`}
          items={parts.held}
          mql={mql}
        />
      </HeldDisclosure>
      <Stack as="ul" gap={1} aria-label={`Notes on the ${currency} white papers`}>
        <li>{`Spend total (${currency}): ${formatMoney(summary?.spendTotal, currency)}`}</li>
        <li>{overlapNote}</li>
        <li>{LINKEDIN_LEADS_NOTE}</li>
        <li>{MEETINGS_BOOKED_NOTE}</li>
      </Stack>
    </Stack>
  );
}

// ---------------------------------------------------------------------------------------------
// The section
// ---------------------------------------------------------------------------------------------

/**
 * The White papers section of `/actions` (AAP 0.5.5, 0.5.4.7; FR-8): one table per currency
 * cohort, each row an asset with its spend, LinkedIn leads, MQL and Qualified Lead side by side
 * and never blended, meetings, deals, costs and action, expandable to its per-creative rows. See
 * the module overview for the layout and rendering rules.
 *
 * The MQL column is headed with the stage label each table's rows recorded when their run
 * computed them (`extra.mql_label`, else the recorded MQL metric label; {@link tableMqlHeader}),
 * so a later change of the binding never relabels frozen counts (AAP 0.3.2). It reads
 * `GET /config` once, through `useConfig()`, only for the fallback header of a table none of
 * whose rows recorded a label ({@link mqlHeader}); every other text and number comes from the
 * section as the API returned it. Rows keep the API's rank order (within each cohort, rows with
 * QL ≥ 1 by cost per QL, then zero-QL rows by spend); the section never re-sorts them.
 *
 * @param props - see `SectionComponentProps`; only `section` is read
 * @returns the section region
 *
 * @example
 * ```tsx
 * <WhitePapersSection section={whitePapers} run={page.run} overlay={page.overlay} />
 * ```
 */
export function WhitePapersSection({ section }: SectionComponentProps): ReactElement {
  const config = useConfig();
  const configuredMql = mqlHeader(config.data);

  return (
    <SectionFrame
      section={section}
      renderItem={(item) => <WhitePaperItemFallback item={item} mql={configuredMql} />}
      renderGroup={(_group, parts) => <CohortTables parts={parts} mql={configuredMql} />}
    />
  );
}
