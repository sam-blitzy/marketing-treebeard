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
 *    `extra.variants` of the group's first item, which the analysis always ranks first (the CR3
 *    next test). The table is evidence, so it is read even when feedback hides that card;
 * 2. the frame's default body: the cards in API order. That is the CR3 card first, carrying the
 *    CR2 weak stage as its reason and the next test as its action, then one CR1 card per variant
 *    pair, then held cards behind the "Held until discrepancies are resolved" disclosure.
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
import type { ReactElement } from 'react';

import type { Group } from '../../../api/types';
import { DataTable, Stack } from '../../../design-system';
import type { DataTableColumn } from '../../../design-system';
import { NOT_APPLICABLE, formatCount, formatPercent } from '../../../lib/format';
import { RecommendationCard } from '../RecommendationCard';
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
 * Formats one stage rate of a variant: a 0–1 ratio decimal string as a percentage at
 * {@link VARIANT_RATE_DIGITS} fraction digits (`"0.004377"` → `"0.44%"`).
 *
 * `null` means the stage does not apply to the variant, such as the open and completion rates
 * of a campaign outside lead generation (AAP 0.5.4.6: non-lead-gen campaigns use only click
 * rate), so it renders {@link NOT_APPLICABLE} (`"n/a"`), never `"0%"`.
 *
 * @param rate - The rate as `variants()` read it: a ratio decimal string, or `null`.
 * @returns The formatted percentage, or `"n/a"`.
 *
 * @example
 * ```ts
 * formatVariantRate('0.395062'); // "39.51%"
 * formatVariantRate(null); // "n/a"
 * ```
 */
export function formatVariantRate(rate: string | null): string {
  return rate === null ? NOT_APPLICABLE : formatPercent(rate, VARIANT_RATE_DIGITS);
}

// ---------------------------------------------------------------------------------------------
// Variants table
// ---------------------------------------------------------------------------------------------

/** One variants-table row: the variant and its React key, unique within the table. */
interface KeyedVariant {
  /** The row key: the creative id, suffixed with its position when a key repeats. */
  readonly key: string;
  /** The variant, as `variants()` read it. */
  readonly variant: VariantRow;
}

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
 * {@link formatVariantRate}.
 */
const VARIANT_COLUMNS: readonly DataTableColumn<KeyedVariant>[] = [
  {
    key: 'creative',
    header: 'Creative',
    isRowHeader: true,
    render: (row) => row.variant.name,
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
    render: (row) => formatVariantRate(row.variant.clickRate),
  },
  {
    key: 'open_rate',
    header: 'Open rate',
    align: 'end',
    render: (row) => formatVariantRate(row.variant.openRate),
  },
  {
    key: 'completion_rate',
    header: 'Completion rate',
    align: 'end',
    render: (row) => formatVariantRate(row.variant.completionRate),
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
 * rate and Leads. A rate that does not apply renders "n/a"; an absent count renders
 * "unavailable". The table states numbers only: which variant leads, and whether that lead is
 * distinguishable, is said by the API's CR1 cards (AAP 0.5.4.2 P7).
 *
 * @param props - see {@link VariantsTableProps}
 * @returns the table, or `null` when there are no variants
 *
 * @example
 * ```tsx
 * <VariantsTable campaignName="Lead Gen — Pipeline Report EMEA" rows={variants(group.items[0])} />
 * ```
 */
export function VariantsTable({ campaignName, rows }: VariantsTableProps): ReactElement | null {
  if (rows.length === 0) {
    return null;
  }
  return (
    <DataTable
      caption={`Variants in ${campaignName}`}
      columns={VARIANT_COLUMNS}
      rows={keyedVariants(rows)}
      getRowKey={(row) => row.key}
    />
  );
}

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
 * The rows come from the group's first item in API order (the CR3 next test the analysis ranks
 * first), whether or not that card is visible now: feedback hides an action, not the evidence
 * the block's numbers rest on. A group without variants rows, such as a campaign whose only
 * items are CR1 pairs, shows its cards without a table.
 */
function renderCampaignBlock(group: Group, parts: GroupParts): ReactElement {
  return (
    <Stack>
      <VariantsTable campaignName={campaignNameOf(group)} rows={variants(group.items[0])} />
      {parts.body}
    </Stack>
  );
}

/**
 * The "Copy/creative changes" section (AAP 0.5.5): one block per LinkedIn campaign, each with
 * its variants table followed by its cards in API order (the CR3 next test with the CR2 weak
 * stage first, then the CR1 pair labels), inside the shared {@link SectionFrame}, which adds
 * the section's findings, held disclosure, empty state, withheld lines and suppression counts.
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
