/**
 * `CreativeSection`: the "Copy/creative changes" section of `/actions` (AAP 0.5.5, 0.5.4.6,
 * FR-7).
 *
 * The section shows one block per LinkedIn campaign. The analysis groups every copy item by its
 * campaign (`creative_group`, key `campaign:{id}`, titled with the campaign name by the
 * recommendations route), so each API group is one block, rendered by {@link SectionFrame} as a
 * group region with the campaign name as its `<h3>`. Inside each block, in this order:
 *
 * 1. the variants table ({@link VariantsTable}): one row per variant (a creative of the campaign
 *    with at least the configured impression minimum) with its impressions, click rate, open
 *    rate, completion rate and leads (AAP 0.5.5 Copy field set). The rows are the frozen
 *    `extra.variants` of the group's first item. Only the CR3 next test carries them, and when
 *    the campaign has one the analysis ranks it first. A group that holds only CR1 pairs (no
 *    stage could be measured against a baseline, or feedback dropped the CR3 before the run)
 *    shows no table. The table is evidence, so it is read even when feedback given since the
 *    run hides that card;
 * 2. the frame's default body: the cards in API order. When the campaign has a CR3 next test,
 *    that card comes first, carrying the CR2 weak stage as its reason and the next test as its
 *    action; then one CR1 card per variant pair, then held cards behind the "Held until
 *    discrepancies are resolved" disclosure.
 *
 * Every list is bounded (`../batches`; AAP 0.1.1 "the five sections render from precomputed
 * runs in under 2 s"): the variants table shows `ROW_BATCH_SIZE` rows at a time, the cards,
 * whose CR1 pairs grow as V(V−1)/2 with the variants, `CARD_BATCH_SIZE` at a time, and the
 * campaign blocks `GROUP_BATCH_SIZE` at a time, each followed by a "Show {n} more" control.
 *
 * What this file deliberately does not do (AAP 0.5.4.2 P7: no premature winners):
 * - it never reorders, relabels or ranks items, and never infers a winner. Every pair label
 *   ("Distinguishable (adjusted p = …)", "{A} ahead, not yet distinguishable …") is the API's
 *   action and reason text, and every "Not yet distinguishable" badge is the API's own
 *   `not_distinguishable` flag, rendered by the card;
 * - it writes no threshold into the UI: the impression minimum, α and power appear only in the
 *   API's text and evidence (AAP 0.5.4.1);
 * - it fetches nothing; the page passes the section in.
 *
 * Built only on the design-system index (AAP 0.4.4): `DataTable` (always captioned) and
 * `Stack`. No raw tables or headings, no inline styles and no stylesheet.
 *
 * @packageDocumentation
 */
import { memo, useMemo } from 'react';
import type { ReactElement } from 'react';

import type { Group, Item } from '../../../api/types';
import { DataTable, Stack } from '../../../design-system';
import type { DataTableColumn } from '../../../design-system';
import { NOT_APPLICABLE, UNAVAILABLE, formatCount, formatPercent } from '../../../lib/format';
import { RecommendationCard } from '../RecommendationCard';
import { BatchControl, BatchEntryAnchor, ROW_BATCH_SIZE, useBatches } from '../batches';
import type { BatchNoun } from '../batches';
import { variants } from '../evidenceExtra';
import type { VariantRow } from '../evidenceExtra';
import { SectionFrame } from './SectionFrame';
import type { GroupParts, SectionComponentProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------------------------

/**
 * Fraction digits of a stage rate in the variants table. LinkedIn click and open rates sit well
 * below 1% (for example 0.44% against 0.40%), so the one digit of `formatPercent`'s default
 * would show two different variants as the same "0.4%". Two digits keep sibling variants
 * apart, as the backend's own reason text does when it renders these rates.
 */
export const VARIANT_RATE_DIGITS = 2;

/**
 * The text of a stage rate that applies but whose denominator is a known zero, such as the
 * click rate of a variant with zero impressions: no rate exists to show, and it is not 0%. The
 * same words as the backend's `creative.NOT_ESTIMABLE` for zero exposure (AAP 0.5.4.6).
 */
export const NOT_ESTIMABLE = 'not estimable';

/**
 * Formats one stage rate of a variant, telling a stage that does not apply apart from a rate
 * that cannot be stated (AAP 0.5.4.6, AAP 0.11.1). In this order:
 *
 * 1. a stage that does not apply (`applies` is `false`: the open and completion rates of a
 *    campaign outside lead generation, which uses only click rate) renders
 *    {@link NOT_APPLICABLE} (`"n/a"`);
 * 2. a rate renders as a percentage at {@link VARIANT_RATE_DIGITS} fraction digits
 *    (`"0.004377"` → `"0.44%"`);
 * 3. a missing rate whose denominator is a known zero (zero exposure) renders
 *    {@link NOT_ESTIMABLE} (`"not estimable"`);
 * 4. any other missing rate (unreported or inconsistent counts) renders `"unavailable"`.
 *
 * An unknown applicability (`applies` is `null`, the evidence names no objective) never yields
 * `"n/a"`, and a missing rate never yields `"0%"`.
 *
 * @param rate - The rate as `variants()` read it: a ratio decimal string, or `null`.
 * @param applies - Whether the stage applies to the variant's campaign; `null` when unknown.
 * @param denominator - The stage's own denominator (impressions for click and open rate, form
 *   opens for completion rate); `null` when unreported.
 * @returns The formatted percentage, `"n/a"`, `"not estimable"` or `"unavailable"`.
 *
 * @example
 * ```ts
 * formatVariantRate('0.395062', true, 162); // "39.51%"
 * formatVariantRate(null, false, null); // "n/a"
 * formatVariantRate(null, true, 0); // "not estimable"
 * formatVariantRate(null, null, null); // "unavailable"
 * ```
 */
export function formatVariantRate(
  rate: string | null,
  applies: boolean | null,
  denominator: number | null,
): string {
  if (applies === false) {
    return NOT_APPLICABLE;
  }
  if (rate !== null) {
    return formatPercent(rate, VARIANT_RATE_DIGITS);
  }
  return denominator === 0 ? NOT_ESTIMABLE : UNAVAILABLE;
}

// ---------------------------------------------------------------------------------------------
// Variants table
// ---------------------------------------------------------------------------------------------

/** The variants of a block without a first item: none, so it shows no table. */
const NO_VARIANTS: readonly VariantRow[] = Object.freeze([]);

/**
 * Each block's variants, read from its first item, kept while the item object lives. Items from
 * `GET /recommendations` are never changed in place: a refetch that leaves an item unchanged
 * hands back the same object, and a changed item arrives as a new one, so kept rows always
 * match their item.
 */
const VARIANT_ROWS = new WeakMap<Item, readonly VariantRow[]>();

/**
 * The variants table rows of a block (`variants()` of its first item), read once per item
 * object, so a block that renders again keeps the same rows unless its first item changed.
 */
function variantRowsOf(item: Item | undefined): readonly VariantRow[] {
  if (item === undefined) {
    return NO_VARIANTS;
  }
  const kept = VARIANT_ROWS.get(item);
  if (kept !== undefined) {
    return kept;
  }
  const rows = variants(item);
  VARIANT_ROWS.set(item, rows);
  return rows;
}

/** One variants-table row: the variant and its React key, unique within the table. */
interface KeyedVariant {
  /** The row key: the creative id, suffixed with its position when a key repeats. */
  readonly key: string;
  /** The variant, as `variants()` read it. */
  readonly variant: VariantRow;
}

/** One shown variants-table row: a {@link KeyedVariant} and its row header's focus target. */
interface VariantTableRow extends KeyedVariant {
  /**
   * The id of the row header's focus target while the table shows rows in batches
   * (`BatchEntryAnchor`), so focus lands on the row when a reveal shows it first; `null` when
   * the table is not batched.
   */
  readonly focusId: string | null;
}

/** What a variants table counts in its batch control: "Showing 50 of 64 variants". */
const VARIANT_ROW_NOUN: BatchNoun = Object.freeze({ singular: 'variant', plural: 'variants' });

/**
 * Pairs each variant with a key unique within the table. The creative id (`creativeId`, which
 * `variants()` takes from the creative's id or else its URN) is the key; a repeat, which only a
 * malformed payload carries, is suffixed with `#{position}`, repeatedly until the key is
 * unused, so `DataTable`'s keys stay unique and no row is dropped.
 */
function keyedVariants(rows: readonly VariantRow[]): KeyedVariant[] {
  const seen = new Set<string>();
  return rows.map((variant, index) => {
    let key = variant.creativeId;
    while (seen.has(key)) {
      key = `${key}#${String(index)}`;
    }
    seen.add(key);
    return { key, variant };
  });
}

/**
 * The variants table's columns, in the order AAP 0.5.5 lists them: Creative (the row header),
 * Impressions, Click rate, Open rate, Completion rate and Leads. Numeric columns align to the
 * end. Counts render through `formatCount` (absent → "unavailable", never 0) and rates through
 * {@link formatVariantRate}, each against its own denominator: click rate applies to every
 * objective, over impressions; open rate, over impressions, and completion rate, over form
 * opens, apply as the variant's `leadGen` says. So a stage the campaign does not use renders
 * "n/a", a missing rate over zero impressions or form opens "not estimable", and any other
 * missing rate "unavailable".
 */
const VARIANT_COLUMNS: readonly DataTableColumn<VariantTableRow>[] = [
  {
    key: 'creative',
    header: 'Creative',
    isRowHeader: true,
    render: (row) => <BatchEntryAnchor id={row.focusId}>{row.variant.name}</BatchEntryAnchor>,
  },
  {
    key: 'impressions',
    header: 'Impressions',
    align: 'end',
    render: (row) => formatCount(row.variant.impressions),
  },
  {
    key: 'click_rate',
    header: 'Click rate',
    align: 'end',
    render: (row) => formatVariantRate(row.variant.clickRate, true, row.variant.impressions),
  },
  {
    key: 'open_rate',
    header: 'Open rate',
    align: 'end',
    render: (row) =>
      formatVariantRate(row.variant.openRate, row.variant.leadGen, row.variant.impressions),
  },
  {
    key: 'completion_rate',
    header: 'Completion rate',
    align: 'end',
    render: (row) =>
      formatVariantRate(row.variant.completionRate, row.variant.leadGen, row.variant.formOpens),
  },
  {
    key: 'leads',
    header: 'Leads',
    align: 'end',
    render: (row) => formatCount(row.variant.leads),
  },
];

/** Props of {@link VariantsTable}. */
export interface VariantsTableProps {
  /** The campaign's name, shown in the caption "Variants in {campaignName}". */
  campaignName: string;
  /** The campaign's variants, in the order the analysis stored them. */
  rows: readonly VariantRow[];
}

/**
 * The variants table of one campaign block (AAP 0.5.5 Copy: "a variants table (impressions,
 * click rate, open rate, completion rate, leads)").
 *
 * A captioned `DataTable`, "Variants in {campaignName}", with one row per variant in stored
 * order and the columns Creative (row header), Impressions, Click rate, Open rate, Completion
 * rate and Leads. An open or completion rate of a campaign outside lead generation renders
 * "n/a" (AAP 0.5.4.6: non-lead-gen campaigns use only click rate); a missing rate of a stage
 * that applies, or whose applicability the evidence does not state, renders "not estimable"
 * over a zero denominator and "unavailable" otherwise (AAP 0.11.1), never "n/a" or "0%"; an
 * absent count renders "unavailable". The table states numbers only: which variant leads, and
 * whether that lead is distinguishable, is said by the API's CR1 cards (AAP 0.5.4.2 P7).
 *
 * The table shows `ROW_BATCH_SIZE` rows at a time, in stored order, followed by a "Show {n}
 * more variants" control when the campaign has more (`../batches`).
 *
 * Memoized on its props, with the keyed rows derived once per `rows` array: given the same
 * campaign name and the same rows, a block that renders again keeps the table, and the rows it
 * has revealed, as they are.
 *
 * @param props - see {@link VariantsTableProps}
 * @returns the table, or `null` when there are no variants
 *
 * @example
 * ```tsx
 * <VariantsTable campaignName="Lead Gen — Pipeline Report EMEA" rows={variants(group.items[0])} />
 * ```
 */
export const VariantsTable = memo(function VariantsTable({
  campaignName,
  rows,
}: VariantsTableProps): ReactElement | null {
  // Keys are assigned over every variant, so a shown row keeps the key it has in the full table.
  const keyed = useMemo(() => keyedVariants(rows), [rows]);
  const batches = useBatches(keyed, { batchSize: ROW_BATCH_SIZE });
  if (keyed.length === 0) {
    return null;
  }
  const shown: VariantTableRow[] = batches.shown.map((row, index) => ({
    ...row,
    focusId: batches.batched ? batches.entryId(index) : null,
  }));
  return (
    <>
      <DataTable
        caption={`Variants in ${campaignName}`}
        columns={VARIANT_COLUMNS}
        rows={shown}
        getRowKey={(row) => row.key}
      />
      <BatchControl batches={batches} noun={VARIANT_ROW_NOUN} />
    </>
  );
});

// ---------------------------------------------------------------------------------------------
// The section
// ---------------------------------------------------------------------------------------------

/**
 * The campaign name a block's caption uses: the group title trimmed, or the group key when the
 * title is blank, as `SectionFrame` falls back for the group heading, so the caption always
 * names the same campaign as the heading above it and the table is never left unnamed.
 */
function campaignNameOf(group: Group): string {
  const title = typeof group.title === 'string' ? group.title.trim() : '';
  return title === '' ? group.key : title;
}

/**
 * One campaign block's body: the variants table, then the frame's cards in API order.
 *
 * The rows come from the group's first item in API order, whether or not that card is visible
 * now: feedback hides an action, not the evidence the block's numbers rest on. When the
 * campaign has a CR3 next test, the analysis ranks it first and its evidence carries the
 * variants. A group without variants rows, such as a campaign whose only items are CR1 pairs,
 * shows its cards without a table.
 */
function renderCampaignBlock(group: Group, parts: GroupParts): ReactElement {
  return (
    <Stack>
      <VariantsTable campaignName={campaignNameOf(group)} rows={variantRowsOf(group.items[0])} />
      {parts.body}
    </Stack>
  );
}

/**
 * The "Copy/creative changes" section (AAP 0.5.5): one block per LinkedIn campaign, each with
 * its variants table when its first item carries variants, followed by its cards in API order
 * (the CR3 next test with the CR2 weak stage first when the campaign has one, then the CR1 pair
 * labels), inside the shared {@link SectionFrame}, which adds the section's findings, held
 * disclosure, empty state, withheld lines and suppression counts.
 *
 * Cards render with `headingLevel={4}`, below each block's `<h3>`, so no heading level is
 * skipped.
 *
 * @param props - see {@link SectionComponentProps}; only `section` is read
 * @returns the section region
 *
 * @example
 * ```tsx
 * <CreativeSection section={creativeSection} run={run} overlay={overlay} />
 * ```
 */
export function CreativeSection({ section }: SectionComponentProps): ReactElement {
  return (
    <SectionFrame
      section={section}
      renderItem={(item) => <RecommendationCard item={item} headingLevel={4} />}
      renderGroup={renderCampaignBlock}
    />
  );
}
