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
 *    is labelled with the API's group title and counts every card its panel holds: the visible
 *    ones, those computed from replaced data and the held ones collapsed behind their "Held
 *    until discrepancies are resolved" disclosure; only items hidden by feedback are left out.
 *    That is the rule the section's navigation count follows, so the tab counts add up to it.
 *    Every tab the API returns is shown, an empty one included. A tab that holds the
 *    confirmation of a card a feedback recorded on this page has hidden keeps it in the card's
 *    place; any other empty tab's panel is an `EmptyState` titled with the reason its rule has
 *    no card: the cards feedback hides now, else the reason of the leading withheld line of the
 *    group's rule (for example "No search term met the K1 minimum of 70 clicks", with the
 *    minimum read from the recorded sample), else, when the section reports suppression counts,
 *    "No {rule} action to show; not shown in this section: {count} {label}" (for example "No K3
 *    action to show; not shown in this section: 2 dismissed"; the counts are the section's,
 *    never the rule's), else "{rule} did not fire for this window".
 * 2. **Keyword cards.** Each item is a {@link RecommendationCard} whose body reads, in order:
 *    - **The six fields.** The "Keyword details" list holds exactly six fields, in the keyword
 *      field set's order (AAP 0.5.5 "Keywords: term, campaign and ad group, spend, conversions,
 *      cost/conversion, reason"; AAP 0.5.4.3): Term, Campaign and ad group, Spend, Conversions,
 *      Cost/conversion and Reason. The Term entry is the term alone, and the Campaign and ad
 *      group entry reads "{campaign} · {ad group}". Without that second entry the list is the
 *      user's five, in their order (AAP 0.1.2 "Each item shows: term, spend, conversions,
 *      cost/conversion, and the specific reason it's flagged").
 *    - **Rule extras.** K2 expansion cards add up to five adjacent terms, "from your
 *      search-term report"; K3 review cards add the quality score first → last and CPC and
 *      cost/conversion by half. Both come after the six fields, before the card's evidence
 *      table.
 *
 *    Within the six fields:
 *    - The Reason entry is the API's reason sentence. The card is told the fields carry it
 *      (`reasonPlacement="fields"`), so it prints no reason line of its own and the reason
 *      appears once, last of the six.
 *    - Cost/conversion reads "—" at a known 0 conversions (AAP 0.5.4.3).
 *    - Fractional conversions (data-driven attribution) are shown as returned, never rounded
 *      to 0.
 *
 * Every key, title, term and number comes from the API through the guarded readers in
 * `../evidenceExtra`; no threshold is written into the client (AAP 0.5.4.1). Items keep the
 * API's rank order: the frame partitions them into visible, historical, held and hidden, and
 * nothing here sorts or filters. UI comes only from the design-system index (AAP 0.4.4): no raw
 * buttons, headings or tables, no inline styles and no stylesheet. Nothing is fetched here
 * (AAP 0.11.1).
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

import type { Item, Metric, Withheld } from '../../../api/types';
import { EmptyState, Stack, Tabs } from '../../../design-system';
import type { TabItem } from '../../../design-system';
import {
  EM_DASH,
  FIGURE_SEPARATOR,
  UNAVAILABLE,
  formatCount,
  formatMetric,
  isDecimalString,
} from '../../../lib/format';
import { RecommendationCard } from '../RecommendationCard';
import { withheldReason } from '../WithheldList';
import { ADJACENT_TERMS_MAX, adjacentTerms, keywordFields, reviewHalves } from '../evidenceExtra';
import type { AdjacentTerm, KeywordFields } from '../evidenceExtra';
import {
  FieldList,
  NO_RULE_FIRED_REASON,
  SectionFrame,
  notShownReason,
  reportedSuppressions,
  suppressionLine,
} from './SectionFrame';
import type { FieldItem, GroupParts, SectionComponentProps } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------------------------

/** The accessible name of the tab list that switches between the keyword sub-lists. */
export const KEYWORD_TABS_LABEL = 'Keyword actions';

/**
 * The accessible name of every keyword card's field list (term, campaign and ad group, spend,
 * conversions, cost/conversion, reason).
 */
export const KEYWORD_DETAILS_LABEL = 'Keyword details';

/**
 * The visible title, and the accessible name, of a K2 card's adjacent terms. They are derived
 * from the account's own search-term report, because Keyword Planner ideas are unreachable
 * through the n8n webhook (AAP 0.2.4), so the label says where they come from (AAP 0.5.4.3).
 */
export const ADJACENT_TERMS_TITLE = 'Adjacent terms from your search-term report';

/** The accessible name of a K3 card's quality-score and by-half field list. */
export const REVIEW_EVIDENCE_LABEL = 'Review evidence';

/** The label of a keyword card's second field: the term's campaign and ad group. */
const CAMPAIGN_AND_AD_GROUP_LABEL = 'Campaign and ad group';

/** The separator between the campaign and ad group names: "Core CRM — US · CRM Software". */
const CAMPAIGN_SEPARATOR = ' · ';

/** The rule that produces block / negative-keyword candidates. */
const BLOCK_RULE_ID = 'K1';

/** The rule that produces expansion candidates, whose cards list adjacent terms. */
const EXPANSION_RULE_ID = 'K2';

/** The rule that produces keyword reviews, whose cards show quality score and CPC by half. */
const REVIEW_RULE_ID = 'K3';

/**
 * The rule behind each Keywords group key the API returns (AAP 0.5.4.3): `block` holds the K1
 * block / negative-keyword candidates, `expand` the K2 expansion candidates and `review` the K3
 * keyword reviews. An empty tab reads its reason from the withheld lines of its group's rule; a
 * group key not listed here has no known rule.
 */
const GROUP_RULES: Readonly<Record<string, string>> = Object.freeze({
  block: BLOCK_RULE_ID,
  expand: EXPANSION_RULE_ID,
  review: REVIEW_RULE_ID,
});

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
 * A first-half against second-half pair, each as {@link metricText}, joined by
 * {@link FIGURE_SEPARATOR}: "USD 2.10 / USD 2.52", which never wraps inside the pair.
 */
function halvesText(firstHalf: Metric | null, secondHalf: Metric | null): string {
  return `${metricText(firstHalf)}${FIGURE_SEPARATOR}${metricText(secondHalf)}`;
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
 * The campaign and ad group names, "{campaign} · {ad group}". A missing part reads
 * "unavailable", so the line never hides which name is unknown; "unavailable" alone when the
 * evidence names neither.
 */
function campaignAndAdGroupText(fields: KeywordFields): string {
  if (fields.campaign === null && fields.adGroup === null) {
    return UNAVAILABLE;
  }
  return [fields.campaign ?? UNAVAILABLE, fields.adGroup ?? UNAVAILABLE].join(CAMPAIGN_SEPARATOR);
}

/**
 * The Reason entry's value: the API's reason sentence, the specific reason the item is flagged,
 * trimmed; `null`, which the field list renders as "unavailable", when it is missing or blank.
 */
function reasonText(item: Item): string | null {
  const reason = textOr(item.reason, '');
  return reason === '' ? null : reason;
}

/**
 * The six fields every keyword card lists, in the keyword field set's order (AAP 0.5.5, AAP
 * 0.5.4.3, FR-4): the search term (K1, K2) or keyword text (K3), the campaign and ad group,
 * spend, conversions, cost/conversion and the reason. The Term entry holds the term alone,
 * "unavailable" when nothing names it; the Campaign and ad group entry reads
 * "{campaign} · {ad group}", a missing name reading "unavailable". The other five keep the
 * user's order (AAP 0.1.2: term, spend, conversions, cost/conversion, reason).
 *
 * @param fields - the item's keyword fields
 * @param item - the K1, K2 or K3 item, whose reason sentence is the last field
 * @returns the fields, in display order
 */
function keywordDetailFields(fields: KeywordFields, item: Item): FieldItem[] {
  return [
    { key: 'term', label: 'Term', value: textOr(fields.term, UNAVAILABLE) },
    {
      key: 'campaign_and_ad_group',
      label: CAMPAIGN_AND_AD_GROUP_LABEL,
      value: campaignAndAdGroupText(fields),
    },
    { key: 'spend', label: 'Spend', value: metricText(fields.spend) },
    { key: 'conversions', label: 'Conversions', value: metricText(fields.conversions) },
    { key: 'cost_per_conversion', label: 'Cost/conversion', value: costPerConversionText(fields) },
    { key: 'reason', label: 'Reason', value: reasonText(item) },
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
 * the first span → the last span, then CPC and cost/conversion as first half / second half
 * ({@link halvesText}). A value the evidence lacks reads "unavailable".
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
      value: halvesText(halves.cpcFirstHalf, halves.cpcSecondHalf),
    },
    {
      key: 'cost_per_conversion_by_half',
      label: 'Cost/conversion by half',
      value: halvesText(halves.cpaFirstHalf, halves.cpaSecondHalf),
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
 * - The "Keyword details" list holds the six fields in the keyword field set's order (AAP
 *   0.5.5: term, campaign and ad group, spend, conversions, cost/conversion, reason) and
 *   nothing else. The reason is the list's last entry, so the card is passed
 *   `reasonPlacement="fields"` and prints no reason line of its own above the list.
 * - K2 adjacent terms and K3 review evidence follow the six fields, before the card's evidence
 *   table.
 * - `breakLongWords` lets long search terms, keyword texts and campaign names wrap inside the
 *   card instead of widening it (WCAG 2.2 SC 1.4.10 Reflow).
 *
 * @returns the card
 */
function KeywordCard({ item }: KeywordCardProps): ReactElement {
  const fields = keywordFields(item);
  return (
    <RecommendationCard item={item} headingLevel={3} reasonPlacement="fields">
      <Stack gap={3} breakLongWords>
        <FieldList label={KEYWORD_DETAILS_LABEL} fields={keywordDetailFields(fields, item)} />
        {item.rule_id === EXPANSION_RULE_ID ? <AdjacentTermsList item={item} /> : null}
        {item.rule_id === REVIEW_RULE_ID ? (
          <FieldList label={REVIEW_EVIDENCE_LABEL} fields={reviewEvidenceFields(item)} />
        ) : null}
      </Stack>
    </RecommendationCard>
  );
}

/**
 * The frame's `renderItem`: every keyword item the frame shows (visible, computed from replaced
 * data or held) becomes a {@link KeywordCard}. The frame keys each rendered item by its id.
 */
function renderKeywordCard(item: Item): ReactNode {
  return <KeywordCard item={item} />;
}

// ---------------------------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------------------------

/**
 * How many cards a group's panel holds: every item feedback does not hide, whether visible,
 * computed from replaced data or held behind the disclosure. It is the per-group share of the
 * section's navigation count (`visibleItemCount` in `../SectionNav`), so the tabs add up to it.
 * A confirmation is never counted: its hidden item is counted as hidden.
 */
function shownItemCount(parts: GroupParts): number {
  return parts.visible.length + parts.historical.length + parts.held.length;
}

/**
 * Whether a group has something to show: at least one visible, historical or held item (a
 * {@link shownItemCount} above 0), or the confirmation of an item a feedback recorded on this
 * page has hidden, which the tab shows in place of its empty state ({@link emptyTabReason})
 * while the run still lists the item.
 */
function hasShownItems(parts: GroupParts): boolean {
  return shownItemCount(parts) > 0 || parts.placed.confirmed > 0;
}

/**
 * The tab selected until the user picks one: the first group, in API order, with a visible
 * card or a confirmation, else the first with a card computed from replaced data, else the
 * first with a held card, else the first group. A tab whose last visible card the user has just
 * dismissed, snoozed or marked done therefore stays selected with its confirmation, rather than
 * the selection jumping to another sub-list.
 *
 * The design-system `Tabs` would otherwise always open on the first tab. Opening on the first
 * sub-list with something to act on keeps a section whose Block list is empty from greeting
 * the user with an empty state while Expand or Review holds actions; the tab order, labels and
 * counts stay exactly as the API returned them.
 *
 * @param parts - every group of the section, as the frame prepared them
 * @returns the key of the tab to select, or `undefined` when there are no groups
 */
function defaultTabKey(parts: readonly GroupParts[]): string | undefined {
  const first =
    parts.find((part) => part.visible.length > 0 || part.placed.confirmed > 0) ??
    parts.find((part) => part.historical.length > 0) ??
    parts.find((part) => part.held.length > 0) ??
    parts[0];
  return first?.group.key;
}

/**
 * The rule behind a group key ({@link GROUP_RULES}), or `null` for a key it does not list. Only
 * the map's own keys match, so a key such as `constructor` is not read from the prototype.
 */
function groupRule(key: string): string | null {
  return Object.hasOwn(GROUP_RULES, key) ? (GROUP_RULES[key] ?? null) : null;
}

/**
 * The one-sentence reason of a tab whose group has no card and no confirmation to show
 * ({@link hasShownItems}), the first that applies:
 *
 * 1. "No actions to show: {n} hidden by feedback since this run (dismissed, snoozed or done)"
 *    when feedback given since the run hides the group's cards now ({@link notShownReason});
 * 2. the reason of the leading withheld line of the group's rule ({@link withheldReason}), for
 *    example "No search term met the K1 minimum of 70 clicks" or "K3 withheld: window 7 days of
 *    14 required", every number the recorded sample's;
 * 3. "No {rule} action to show; not shown in this section: {count} {label}; …" when the section
 *    reports suppression counts ({@link reportedSuppressions}), one part per count as the
 *    frame's "Not shown" list writes it ({@link suppressionLine}), for example "No K3 action to
 *    show; not shown in this section: 2 dismissed". The run records those counts per section,
 *    never per rule, so the sentence names them as the section's and never claims they are the
 *    tab's; it only keeps the tab from saying its rule did not fire when the rule may have fired
 *    and had its items suppressed, by feedback for example;
 * 4. "{rule} did not fire for this window" for a group of a known rule with nothing withheld and
 *    nothing suppressed in the section;
 * 5. "No rule fired for this window" ({@link NO_RULE_FIRED_REASON}) for a group key with no
 *    known rule, once nothing is hidden now.
 *
 * @param parts - the group, as the frame prepared it
 * @param withheld - the section's withheld patterns, in API order
 * @param suppressions - the section's suppression counts ({@link reportedSuppressions})
 * @returns the reason
 */
function emptyTabReason(
  parts: GroupParts,
  withheld: readonly Withheld[],
  suppressions: readonly (readonly [string, number])[],
): string {
  const notShown = notShownReason(parts.hiddenCount);
  if (notShown !== null) {
    return notShown;
  }
  const rule = groupRule(parts.group.key);
  if (rule === null) {
    return NO_RULE_FIRED_REASON;
  }
  const ruleLines = withheld.filter((w) => textOr(w.rule_id, '') === rule);
  const withheldText = withheldReason(ruleLines);
  if (withheldText !== null) {
    return withheldText;
  }
  if (suppressions.length > 0) {
    const counts = suppressions.map(([reason, count]) => suppressionLine(reason, count));
    return `No ${rule} action to show; not shown in this section: ${counts.join('; ')}`;
  }
  return `${rule} did not fire for this window`;
}

/**
 * One group as a tab: the API's key and title, the count of every card its panel holds
 * ({@link shownItemCount}: visible, computed from replaced data and held, so a group whose
 * items are all held counts them rather than reading 0), and as its panel either the frame's
 * default body (visible cards, the cards computed from replaced data, then the held disclosure,
 * collapsed, each card a feedback recorded on this page has hidden leaving its confirmation in
 * its place) or, for a group with nothing to show ({@link hasShownItems}: no card and no
 * confirmation), an `EmptyState` titled with the reason ({@link emptyTabReason}). The count
 * never includes a confirmation: the hidden item is counted as hidden.
 *
 * The group's anchor id is the id of the tab's panel element, which `Tabs` always renders, so
 * `#group-keywords-{key}` resolves to the element that holds the group even while a tab never
 * selected has not yet mounted its content.
 *
 * @param parts - the group, as the frame prepared it
 * @param withheld - the section's withheld patterns, in API order, for an empty tab's reason
 * @param suppressions - the section's suppression counts ({@link reportedSuppressions}), for an
 *   empty tab's reason
 * @returns the tab
 */
function groupTab(
  parts: GroupParts,
  withheld: readonly Withheld[],
  suppressions: readonly (readonly [string, number])[],
): TabItem {
  const title = textOr(parts.group.title, parts.group.key);
  const content = hasShownItems(parts) ? (
    <Stack>{parts.body}</Stack>
  ) : (
    <EmptyState title={emptyTabReason(parts, withheld, suppressions)} />
  );
  return {
    key: parts.group.key,
    label: title,
    count: shownItemCount(parts),
    content,
    id: parts.anchorId,
  };
}

/**
 * The "Keywords to adjust" section (AAP 0.5.5): the shared {@link SectionFrame} with one tab per
 * API group (Block, Expand and Review in the API's order and titles) and a keyword card per
 * item. See the module overview for the card's fields.
 *
 * The frame renders the section's findings, including the `search_term_coverage` finding,
 * above the tabs, and its own empty state, withheld list and suppression counts. An empty tab's
 * panel states why its rule has no card ({@link emptyTabReason}), from the section's withheld
 * lines and suppression counts, unless it holds the confirmation of a card a feedback recorded
 * on this page has hidden, which stays in the card's place. The tab list is the design-system
 * `Tabs`, so the arrow keys, Home and End move between tabs and Tab moves into the visible
 * panel. The component keeps only which tab the user selected; it fetches nothing.
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
    const suppressions = reportedSuppressions(section);
    return (
      <Tabs
        label={KEYWORD_TABS_LABEL}
        tabs={parts.map((part) => groupTab(part, section.withheld, suppressions))}
        selectedKey={selectedKey}
        onChange={setChosenKey}
      />
    );
  };

  return (
    <SectionFrame section={section} renderItem={renderKeywordCard} renderGroups={renderGroups} />
  );
}
