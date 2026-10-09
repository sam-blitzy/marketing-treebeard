/**
 * `KeywordsSection`: the "Keywords to adjust" section of `/actions` (AAP 0.5.5, 0.5.4.3, FR-4).
 *
 * The section renders inside the shared {@link SectionFrame}, which owns the section region and
 * its `<h2>`, the findings (the `search_term_coverage` finding among them, as a banner above the
 * sub-lists), the empty state, the withheld K1 terms and the suppression counts. This file adds
 * only what is specific to Keywords:
 *
 * 1. **Sub-lists as tabs.** The API's groups (`block`, `expand`, `review`: K1, K2 and K3) become
 *    one design-system `Tabs` named "Keyword actions", one tab per group in API order. Each tab
 *    is labelled with the API's group title and counts the group's visible cards. Every tab the
 *    API returns is shown, an empty one included, whose panel says "No items".
 * 2. **Keyword cards.** Each item is a {@link RecommendationCard} whose section fields list, in
 *    the user's order (AAP 0.5.4.3 "Each item shows: term, spend, conversions, cost/conversion,
 *    and the specific reason it's flagged"): Term, Campaign and ad group, Spend, Conversions and
 *    Cost/conversion. The card prints the reason sentence itself, above these fields, so it is
 *    not repeated here.
 *    - Cost/conversion reads "—" at a known 0 conversions (AAP 0.5.4.3).
 *    - Fractional conversions (data-driven attribution) are shown as returned, never rounded
 *      to 0.
 *    - K2 expansion cards add up to five adjacent terms, "from your search-term report".
 *    - K3 review cards add the quality score first → last and CPC and cost/conversion by half.
 *
 * Every key, title, term and number comes from the API through the guarded readers in
 * `../evidenceExtra`; no threshold is written into the client (AAP 0.5.4.1). Items keep the
 * API's rank order: the frame partitions them into visible, held and hidden, and nothing here
 * sorts or filters. UI comes only from the design-system index (AAP 0.4.4): no raw buttons,
 * headings or tables, no inline styles and no stylesheet. Nothing is fetched here (AAP 0.11.1).
 *
 * @example
 * ```tsx
 * <KeywordsSection section={page.sections[0]} run={page.run} overlay={page.overlay} />
 * ```
 *
 * @packageDocumentation
 */
import { useState } from 'react';
import type { ReactElement, ReactNode } from 'react';

import type { Item, Metric } from '../../../api/types';
import { EmptyState, Stack, Tabs } from '../../../design-system';
import type { TabItem } from '../../../design-system';
import {
  EM_DASH,
  UNAVAILABLE,
  formatCount,
  formatMetric,
  isDecimalString,
} from '../../../lib/format';
import { RecommendationCard } from '../RecommendationCard';
import { ADJACENT_TERMS_MAX, adjacentTerms, keywordFields, reviewHalves } from '../evidenceExtra';
import type { AdjacentTerm, KeywordFields } from '../evidenceExtra';
import { FieldList, SectionFrame } from './SectionFrame';
import type { FieldItem, GroupParts, SectionComponentProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/** The accessible name of the tab list that switches between the keyword sub-lists. */
export const KEYWORD_TABS_LABEL = 'Keyword actions';

/** The accessible name of every keyword card's field list (term, spend, conversions, …). */
export const KEYWORD_DETAILS_LABEL = 'Keyword details';

/**
 * The visible title, and the accessible name, of a K2 card's adjacent terms. They are derived
 * from the account's own search-term report, because Keyword Planner ideas are unreachable
 * through the n8n webhook (AAP 0.2.4), so the label says where they come from (AAP 0.5.4.3).
 */
export const ADJACENT_TERMS_TITLE = 'Adjacent terms from your search-term report';

/** The accessible name of a K3 card's quality-score and by-half field list. */
export const REVIEW_EVIDENCE_LABEL = 'Review evidence';

/** The title of the empty state a tab shows when its group has no visible or held card. */
export const EMPTY_GROUP_TITLE = 'No items';

/** The separator between the campaign and ad group names: "Core CRM — US · CRM Software". */
const CAMPAIGN_SEPARATOR = ' · ';

/** The rule that produces expansion candidates, whose cards list adjacent terms. */
const EXPANSION_RULE_ID = 'K2';

/** The rule that produces keyword reviews, whose cards show quality score and CPC by half. */
const REVIEW_RULE_ID = 'K3';

// ---------------------------------------------------------------------------------------------
// Value helpers
// ---------------------------------------------------------------------------------------------

/** Returns `text` trimmed, or `fallback` when it is not a string or holds only whitespace. */
function textOr(text: unknown, fallback: string): string {
  if (typeof text !== 'string') {
    return fallback;
  }
  const trimmed = text.trim();
  return trimmed === '' ? fallback : trimmed;
}

/**
 * A metric's display text: {@link formatMetric}'s rendering (money in the metric's currency,
 * counts with their fractions kept, text such as "—" verbatim), or "unavailable" when the
 * evidence carries no such metric, so a field never shows an empty value.
 */
function metricText(metric: Metric | null): string {
  return metric === null ? UNAVAILABLE : formatMetric(metric);
}

/**
 * Whether a metric is a known zero: present, not unavailable, and a number or decimal string
 * whose digits are all 0 (`0`, `"0"`, `"0.000000"`, `"-0"`). The check reads the digits rather
 * than converting with `Number`, so a tiny non-zero value such as `"1e-400"` is never read as 0.
 * Any fractional conversion therefore counts as converting (AAP 0.5.4.3).
 */
function isKnownZero(metric: Metric | null): boolean {
  if (metric === null || metric.unavailable === true) {
    return false;
  }
  const { value } = metric;
  if (typeof value === 'number') {
    return value === 0;
  }
  if (typeof value !== 'string') {
    return false;
  }
  const trimmed = value.trim();
  if (!isDecimalString(trimmed)) {
    return false;
  }
  const mantissa = trimmed.split(/e/iu)[0] ?? '';
  return !/[1-9]/u.test(mantissa);
}

/**
 * The cost/conversion field: "—" when the conversions are a known 0 (a zero denominator, AAP
 * 0.5.4.3), whatever the cost metric holds; otherwise the cost metric as the backend sent it,
 * which is "unavailable" when the conversions were not reported.
 */
function costPerConversionText(fields: KeywordFields): string {
  return isKnownZero(fields.conversions) ? EM_DASH : metricText(fields.costPerConversion);
}

/**
 * The "Campaign and ad group" field, "{campaign} · {ad group}". A missing part reads
 * "unavailable", so the field never hides which name is unknown; `null`, which the field list
 * renders as "unavailable", when the evidence names neither.
 */
function campaignAndAdGroupText(fields: KeywordFields): string | null {
  if (fields.campaign === null && fields.adGroup === null) {
    return null;
  }
  return [fields.campaign ?? UNAVAILABLE, fields.adGroup ?? UNAVAILABLE].join(CAMPAIGN_SEPARATOR);
}

/**
 * The five fields every keyword card lists, in the user's order (AAP 0.5.4.3): the search term
 * (K1, K2) or keyword text (K3), the campaign and ad group, spend, conversions and
 * cost/conversion.
 *
 * @param item - the K1, K2 or K3 item
 * @returns the fields, in display order
 */
function keywordDetailFields(item: Item): FieldItem[] {
  const fields = keywordFields(item);
  return [
    { key: 'term', label: 'Term', value: fields.term },
    {
      key: 'campaign_ad_group',
      label: 'Campaign and ad group',
      value: campaignAndAdGroupText(fields),
    },
    { key: 'spend', label: 'Spend', value: metricText(fields.spend) },
    { key: 'conversions', label: 'Conversions', value: metricText(fields.conversions) },
    { key: 'cost_per_conversion', label: 'Cost/conversion', value: costPerConversionText(fields) },
  ];
}

/** "1 conversion", "0.5 conversions", "9 clicks": a formatted count with its noun. */
function countWithNoun(text: string, singular: string, plural: string): string {
  return `${text} ${text === '1' ? singular : plural}`;
}

/**
 * One adjacent term as a line: the term, followed by its window conversions and clicks when the
 * evidence gives them, for example "crm for small teams (1 conversion, 9 clicks)". Conversions
 * keep their fractions ({@link formatCount}).
 */
function adjacentTermText(term: AdjacentTerm): string {
  const counts: string[] = [];
  if (term.conversions !== null) {
    counts.push(countWithNoun(formatCount(term.conversions), 'conversion', 'conversions'));
  }
  if (term.clicks !== null) {
    counts.push(countWithNoun(formatCount(term.clicks), 'click', 'clicks'));
  }
  return counts.length === 0 ? term.term : `${term.term} (${counts.join(', ')})`;
}

/**
 * The K3 review fields (AAP 0.5.5 "review cards add QS and CPC by half"): the quality score of
 * the first span → the last span, then CPC and cost/conversion as first half / second half.
 * A value the evidence lacks reads "unavailable".
 *
 * @param item - the K3 item
 * @returns the fields, in display order
 */
function reviewEvidenceFields(item: Item): FieldItem[] {
  const halves = reviewHalves(item);
  return [
    {
      key: 'quality_score',
      label: 'Quality score',
      value: `${metricText(halves.qsFirst)} → ${metricText(halves.qsLast)}`,
    },
    {
      key: 'cpc_by_half',
      label: 'CPC by half',
      value: `${metricText(halves.cpcFirstHalf)} / ${metricText(halves.cpcSecondHalf)}`,
    },
    {
      key: 'cost_per_conversion_by_half',
      label: 'Cost/conversion by half',
      value: `${metricText(halves.cpaFirstHalf)} / ${metricText(halves.cpaSecondHalf)}`,
    },
  ];
}

// ---------------------------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------------------------

/** Props of {@link AdjacentTermsList}. */
interface AdjacentTermsListProps {
  /** The K2 item whose `extra.adjacent_terms` to list. */
  item: Item;
}

/**
 * A K2 card's adjacent terms (AAP 0.5.4.3: "up to 5 other window search terms … labelled
 * 'from your search-term report'"): the visible title, then a list named by the same text, one
 * entry per term in stored order. The reader already caps the list; it is sliced again here so
 * the card never shows more than {@link ADJACENT_TERMS_MAX}, whatever the reader returns.
 *
 * @returns the block, or `null` when the item has no adjacent terms
 */
function AdjacentTermsList({ item }: AdjacentTermsListProps): ReactElement | null {
  const terms = adjacentTerms(item).slice(0, ADJACENT_TERMS_MAX);
  if (terms.length === 0) {
    return null;
  }
  return (
    <Stack gap={1}>
      {/*
        BLITZY [DESIGN_SYSTEM_GAP]: the design system has no text primitive and Stack takes no
        aria-labelledby, so the title is a <div> and the list carries the same text as its
        aria-label: what a sighted user reads is exactly the list's accessible name. The title
        is hidden from assistive technology, which announces that text once, as the list's
        name, instead of reading it twice.
      */}
      <div aria-hidden="true">{ADJACENT_TERMS_TITLE}</div>
      <Stack as="ul" gap={1} aria-label={ADJACENT_TERMS_TITLE}>
        {terms.map((term, index) => (
          <li key={`${index}-${term.term}`}>{adjacentTermText(term)}</li>
        ))}
      </Stack>
    </Stack>
  );
}

/** Props of {@link KeywordCard}. */
interface KeywordCardProps {
  /** The K1, K2 or K3 item, as `GET /recommendations` returned it (with its overlay). */
  item: Item;
}

/**
 * One keyword card: the shared {@link RecommendationCard} with the keyword fields as its
 * section-specific content.
 *
 * - The headline is an `<h3>`: inside the tabs there is no group heading, so the card sits
 *   directly below the section's `<h2>`.
 * - The card prints the reason sentence above its `children`. AAP 0.5.5 lists the reason last
 *   in the keyword field set, but the shared card owns the reason for every section, so it is
 *   not repeated here; the five fields still precede the evidence table in the user's order.
 * - `breakLongWords` lets long search terms, keyword texts and campaign names wrap inside the
 *   card instead of widening it (WCAG 2.2 SC 1.4.10 Reflow).
 *
 * @returns the card
 */
function KeywordCard({ item }: KeywordCardProps): ReactElement {
  return (
    <RecommendationCard item={item} headingLevel={3}>
      <Stack gap={3} breakLongWords>
        <FieldList label={KEYWORD_DETAILS_LABEL} fields={keywordDetailFields(item)} />
        {item.rule_id === EXPANSION_RULE_ID ? <AdjacentTermsList item={item} /> : null}
        {item.rule_id === REVIEW_RULE_ID ? (
          <FieldList label={REVIEW_EVIDENCE_LABEL} fields={reviewEvidenceFields(item)} />
        ) : null}
      </Stack>
    </RecommendationCard>
  );
}

/**
 * The frame's `renderItem`: every visible and held keyword item becomes a {@link KeywordCard}.
 * The frame keys each rendered item by its id.
 */
function renderKeywordCard(item: Item): ReactNode {
  return <KeywordCard item={item} />;
}

// ---------------------------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------------------------

/** Whether a group has a card to show: at least one visible or held item. */
function hasShownItems(parts: GroupParts): boolean {
  return parts.visible.length > 0 || parts.held.length > 0;
}

/**
 * The tab selected until the user picks one: the first group, in API order, with a visible
 * card, else the first with a held card, else the first group.
 *
 * The design-system `Tabs` would otherwise always open on the first tab. Opening on the first
 * sub-list with something to act on keeps a section whose Block list is empty from greeting
 * the user with "No items" while Expand or Review holds actions; the tab order, labels and
 * counts stay exactly as the API returned them.
 *
 * @param parts - every group of the section, as the frame prepared them
 * @returns the key of the tab to select, or `undefined` when there are no groups
 */
function defaultTabKey(parts: readonly GroupParts[]): string | undefined {
  const first =
    parts.find((part) => part.visible.length > 0) ??
    parts.find((part) => part.held.length > 0) ??
    parts[0];
  return first?.group.key;
}

/**
 * One group as a tab: the API's key and title, the count of its visible cards, and as its
 * panel either the frame's default body (visible cards, then the held disclosure) or, for a
 * group with nothing to show, an empty state that says so.
 *
 * The group's anchor id sits on the panel content in both cases, so `#group-keywords-{key}`
 * always resolves to the element that holds the group.
 *
 * @param parts - the group, as the frame prepared it
 * @returns the tab
 */
function groupTab(parts: GroupParts): TabItem {
  const title = textOr(parts.group.title, parts.group.key);
  const content = hasShownItems(parts) ? (
    <Stack id={parts.anchorId}>{parts.body}</Stack>
  ) : (
    <EmptyState id={parts.anchorId} title={EMPTY_GROUP_TITLE}>
      {`No ${title} actions in this window`}
    </EmptyState>
  );
  return { key: parts.group.key, label: title, count: parts.visible.length, content };
}

/**
 * The "Keywords to adjust" section (AAP 0.5.5): the shared {@link SectionFrame} with one tab per
 * API group (Block, Expand and Review in the API's order and titles) and a keyword card per
 * item. See the module overview for the card's fields.
 *
 * The frame renders the section's findings, including the `search_term_coverage` finding,
 * above the tabs, and its own empty state, withheld list and suppression counts. The tab list
 * is the design-system `Tabs`, so the arrow keys, Home and End move between tabs and Tab moves
 * into the visible panel. The component keeps only which tab the user selected; it fetches
 * nothing.
 *
 * @param props - see {@link SectionComponentProps}; only `section` is read
 * @returns the section region
 *
 * @example
 * ```tsx
 * <KeywordsSection section={keywordsSection} run={run} overlay={overlay} />
 * ```
 */
export function KeywordsSection({ section }: SectionComponentProps): ReactElement {
  const [chosenKey, setChosenKey] = useState<string | undefined>(undefined);

  const renderGroups = (parts: readonly GroupParts[]): ReactNode => {
    // A choice whose group the latest response no longer returns falls back to the default.
    const chosenExists = parts.some((part) => part.group.key === chosenKey);
    const selectedKey = chosenExists ? chosenKey : defaultTabKey(parts);
    return (
      <Tabs
        label={KEYWORD_TABS_LABEL}
        tabs={parts.map(groupTab)}
        selectedKey={selectedKey}
        onChange={setChosenKey}
      />
    );
  };

  return (
    <SectionFrame section={section} renderItem={renderKeywordCard} renderGroups={renderGroups} />
  );
}
