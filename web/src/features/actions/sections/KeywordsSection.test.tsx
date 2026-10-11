/**
 * Component tests for `KeywordsSection`, the "Keywords to adjust" section of `/actions`
 * (AAP 0.5.5 Keywords; AAP 0.5.4.3 rules K1–K3; FR-4).
 *
 * They hold the section to its TSDoc contract and to the AAP's field set:
 * - **Sub-lists as tabs.** One `Tabs` named "Keyword actions" with one tab per API group (Block,
 *   Expand and Review: K1, K2, K3) in API order, each labelled with the group title and its count
 *   of every card its panel holds (visible, computed from replaced data and held; never one
 *   hidden by feedback), so the tab counts add up to the section's navigation count. A held
 *   card stays collapsed behind "Held until discrepancies are resolved". The section opens on
 *   the first sub-list with a visible card, else one with a card computed from replaced data,
 *   else one with a held card; an empty sub-list states why its rule has no card (cards hidden
 *   by feedback, else its rule's leading withheld line, such as "No search term met the K1
 *   minimum of 70 clicks", else the section's suppression counts as the frame writes them, such
 *   as "No K3 action to show; not shown in this section: 2 dismissed", else "{rule} did not fire
 *   for this window"); a chosen tab survives a
 *   refetch and falls back to the default when its group disappears. A section with no card to
 *   show renders the frame's empty state with its one-sentence reason (the cards feedback hides,
 *   a withheld K1 term's minimum) and no tabs.
 * - **The coverage finding.** The `search_term_coverage` finding (search-term spend ÷ keyword
 *   spend, below 80%) sits above the tabs as a banner, never an alert, and every keyword card
 *   carries the low-coverage flag.
 * - **Keyword cards.** One "Keyword details" list of exactly six fields, Term, Campaign and ad
 *   group, Spend, Conversions, Cost/conversion and Reason, displayed in that order with nothing
 *   between them (AAP 0.5.5, AAP 0.5.4.3; the other five keep AAP 0.1.2's order), and no other
 *   field list before them; the Term entry is the term alone, the Campaign and ad group entry
 *   reads "{campaign} · {ad group}", and the reason sentence appears once on the card, as the
 *   last field. Cost/conversion reads "—" at a known 0 conversions (any decimal form) and
 *   fractional conversions count as converting; a missing value reads "unavailable", never 0.
 *   K2 cards add up to five adjacent terms "from your search-term report"; K3 cards add the
 *   quality score first → last and CPC and cost/conversion by half; both follow the six fields,
 *   before the evidence table.
 *
 * - **A card hidden on the page.** Dismissing a sub-list's only card leaves its confirmation in
 *   the tab's panel, not an empty state, and the tab stays selected with its count of 0.
 *
 * The section fetches nothing, so every test stubs the API with no handler: any request fails
 * the test. The feedback-flow test alone renders the section from a stubbed
 * `GET /recommendations` (`../../../test/feedbackFlow`), whose feedback post hides the item.
 */
import { mockApi } from '../../../test/mockApi';

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import type { ReactElement } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';

import type { Group, Item, Metric, Section } from '../../../api/types';
import {
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeEvidence,
  makeFinding,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
  makeWithheld,
} from '../../../test/fixtures';
import { LiveSection, feedbackConfirmationText, feedbackFlow } from '../../../test/feedbackFlow';
import { renderWithProviders } from '../../../test/render';
import { visibleItemCount } from '../SectionNav';
import {
  ADJACENT_TERMS_TITLE,
  KEYWORD_DETAILS_LABEL,
  KEYWORD_TABS_LABEL,
  KeywordsSection,
  REVIEW_EVIDENCE_LABEL,
} from './KeywordsSection';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The fixture run and overlay every section render receives. */
const RUN = makeRun();
const OVERLAY = makeOverlay();

/** The tab names of the default Keywords section, in API order. */
const DEFAULT_TAB_NAMES = [
  'Block / negative-keyword candidates 1',
  'Expansion candidates 1',
  'Review 1',
];

/** The default page's Keywords section: K1, K2 and K3, the coverage finding and a withheld K1. */
function defaultKeywords(): Section {
  const section = makeRecommendationsPage().sections.find((each) => each.key === 'keywords');
  if (section === undefined) {
    throw new Error('the default page has no keywords section');
  }
  return section;
}

/** One default-page item, a fresh copy. */
function pageItem(id: number): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** A Keywords section holding exactly `groups`, with no finding or withheld line. */
function keywordsWith(...groups: Group[]): Section {
  return makeSection('keywords', { groups });
}

/** A K1 search term withheld at `observed` clicks of `required`, as the backend records it. */
function k1Withheld(name: string, observed: number, required: number) {
  return makeWithheld({
    entity: { source: 'google', type: 'search_term', id: `61000201|7700112|${name}`, name },
    reason: `clicks ${String(observed)} of ${String(required)} required`,
    sample: { observed, required, unit: 'clicks' },
  });
}

/** One evidence metric, as the backend serialises it. */
function metric(
  key: string,
  value: Metric['value'],
  unit: string | null,
  currency: string | null = null,
): Metric {
  return {
    key,
    label: key,
    value,
    unit,
    currency,
    floor: null,
    unavailable: value === null ? true : null,
  };
}

/** A K1 item with the given id, evidence metrics and `extra`, its entity named `term {id}`. */
function k1With(id: number, metrics: Metric[], extra: Record<string, unknown> | null): Item {
  const base = makeItem();
  return makeItem({
    id,
    entity: { ...base.entity, name: `term ${String(id)}` },
    evidence: makeEvidence({ metrics, extra }),
  });
}

/** A K2 item whose `extra.adjacent_terms` is `terms`. */
function k2With(id: number, terms: unknown): Item {
  return makeItem({
    id,
    rule_id: 'K2',
    action: `Add as phrase-match keyword ${String(id)}`,
    evidence: makeEvidence({
      metrics: [metric('term', `expansion ${String(id)}`, 'text')],
      extra: { campaign: 'Core CRM — US', ad_group: 'CRM Software', adjacent_terms: terms },
    }),
  });
}

/** Renders the section on its own, with no API handler registered. */
async function renderKeywords(section: Section) {
  return renderWithProviders(<KeywordsSection section={section} run={RUN} overlay={OVERLAY} />);
}

/**
 * The text a tab presents: its text content without its `aria-hidden` content, which leaves out
 * the hidden copy of the label that `Tabs` renders to reserve the label's semibold width.
 */
function tabText(tab: HTMLElement): string | null {
  const copy = tab.cloneNode(true) as HTMLElement;
  for (const hidden of copy.querySelectorAll('[aria-hidden="true"]')) {
    hidden.remove();
  }
  return copy.textContent;
}

/** The tab names, in display order. */
function tabNames(): (string | null)[] {
  return screen.getAllByRole('tab').map(tabText);
}

/** The sum of the counts the tabs show, each read from the end of its tab's name. */
function tabCountTotal(): number {
  return tabNames().reduce((total, name) => {
    const count = /(\d+)$/u.exec(name ?? '')?.[1];
    expect(count, `tab "${name ?? ''}" shows a count`).toBeDefined();
    return total + Number(count);
  }, 0);
}

/** The name of the selected tab. */
function selectedTabName(): string | null {
  const selected = screen
    .getAllByRole('tab')
    .filter((tab) => tab.getAttribute('aria-selected') === 'true');
  expect(selected).toHaveLength(1);
  const [tab] = selected;
  return tab === undefined ? null : tabText(tab);
}

/** The one visible tab panel. */
function visiblePanel(): HTMLElement {
  return screen.getByRole('tabpanel');
}

/** The entries of the list named `name` inside `container`, as text. */
function listEntries(container: HTMLElement, name: string): (string | null)[] {
  return within(within(container).getByRole('list', { name }))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** The entries of every list named `name` inside `container`, one array per card. */
function cardLists(container: HTMLElement, name: string): string[][] {
  return within(container)
    .getAllByRole('list', { name })
    .map((list) =>
      within(list)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent ?? ''),
    );
}

/** The "Keyword details" entries of the card headed `action`, inside the visible panel. */
function keywordDetails(action: string): string[] {
  const card = within(visiblePanel()).getByRole('article', { name: action });
  return within(within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent ?? '');
}

/**
 * The title of the one empty state inside `container`, its one-sentence reason; it fails the
 * test when `container` holds none or several, or when the empty state has a reason body.
 */
function emptyStateTitle(container: HTMLElement): string | null | undefined {
  const empties = container.querySelectorAll('.ds-emptystate');
  expect(empties).toHaveLength(1);
  expect(empties[0]?.querySelector('.ds-emptystate__reason')).toBeNull();
  return empties[0]?.querySelector('.ds-emptystate__title')?.textContent;
}

/** How many times `text` occurs in the text of `container`. */
function occurrences(container: HTMLElement, text: string): number {
  return (container.textContent ?? '').split(text).length - 1;
}

/** The campaign and ad group every default-page keyword card names, "{campaign} · {ad group}". */
const CORE_CRM_CAMPAIGN_AND_AD_GROUP = 'Core CRM — US · CRM Software';

/** The second field of every default-page keyword card. */
const CORE_CRM_ENTRY = `Campaign and ad group: ${CORE_CRM_CAMPAIGN_AND_AD_GROUP}`;

/** The label that starts every keyword card's second field. */
const CAMPAIGN_ENTRY_PREFIX = 'Campaign and ad group: ';

/** The accessible name of the separate list a keyword card once had; no card may render it. */
const RETIRED_CONTEXT_LIST = 'Keyword context';

/**
 * Asserts the card body's displayed text, as a reader meets it: it starts with
 * "Term: {term}", then "Campaign and ad group: {campaignAndAdGroup}", "Spend: ",
 * "Conversions: ", "Cost/conversion: " and "Reason: {reason}" at strictly increasing
 * positions; the Term and Campaign and ad group entries hold exactly their values, the
 * campaign and ad group appear once, and `followers` (the K2 adjacent-terms title or the K3
 * review evidence) come after the Reason entry.
 *
 * @param card - the keyword card
 * @param term - the term the Term entry must hold, alone
 * @param campaignAndAdGroup - the "{campaign} · {ad group}" the second entry must hold
 * @param reason - the item's reason sentence
 * @param followers - texts that must appear after the Reason entry, in this order
 */
function expectDisplayedSequence(
  card: HTMLElement,
  term: string,
  campaignAndAdGroup: string,
  reason: string,
  followers: readonly string[] = [],
): void {
  const text = card.querySelector('.ds-card__body')?.textContent ?? '';
  const reasonEntry = `Reason: ${reason}`;
  const labels = [
    'Term: ',
    CAMPAIGN_ENTRY_PREFIX,
    'Spend: ',
    'Conversions: ',
    'Cost/conversion: ',
    reasonEntry,
  ];
  const positions = labels.map((label) => text.indexOf(label));
  // Nothing (no reason line, no other list) precedes the Term entry.
  expect(positions[0]).toBe(0);
  positions.forEach((position, index) => {
    expect(position, `"${labels[index] ?? ''}" is displayed`).toBeGreaterThanOrEqual(0);
    if (index > 0) {
      expect(position, `"${labels[index] ?? ''}" follows the field before it`).toBeGreaterThan(
        positions[index - 1] ?? Number.POSITIVE_INFINITY,
      );
    }
  });
  // The Term entry is the term alone: the next thing displayed is the campaign and ad group.
  expect(text.slice(positions[0], positions[1])).toBe(`Term: ${term}`);
  // The second entry is the campaign and ad group alone: the next thing is the Spend entry.
  expect(text.slice(positions[1], positions[2])).toBe(
    `${CAMPAIGN_ENTRY_PREFIX}${campaignAndAdGroup}`,
  );
  // The campaign and ad group are displayed once, second, never again after the reason.
  expect(occurrences(card, CAMPAIGN_ENTRY_PREFIX)).toBe(1);
  let previous = (positions[5] ?? 0) + reasonEntry.length;
  for (const follower of followers) {
    const position = text.indexOf(follower, previous);
    expect(position, `"${follower}" follows the reason`).toBeGreaterThanOrEqual(previous);
    previous = position + follower.length;
  }
}

/**
 * Asserts that `card` renders its six fields as one "Keyword details" list and no separate
 * campaign-and-ad-group list.
 *
 * @param card - the keyword card
 */
function expectOneDetailsList(card: HTMLElement): void {
  expect(within(card).getAllByRole('list', { name: KEYWORD_DETAILS_LABEL })).toHaveLength(1);
  expect(within(card).queryByRole('list', { name: RETIRED_CONTEXT_LIST })).toBeNull();
}

/**
 * Asserts that `nodes` appear in document order, each one following the one before it.
 *
 * @param nodes - the elements, in their expected order
 */
function expectInOrder(...nodes: HTMLElement[]): void {
  nodes.slice(1).forEach((node, index) => {
    const previous = nodes[index];
    expect(previous?.compareDocumentPosition(node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
  });
}

/** Holds the setter a {@link Harness} exposes, so a test can replace the section it renders. */
interface SectionControl {
  replace?: (next: Section) => void;
}

/** Renders the section from state, as a refetch hands `ActionsPage` a new section object. */
function Harness({
  initial,
  control,
}: {
  initial: Section;
  control: SectionControl;
}): ReactElement {
  const [section, setSection] = useState(initial);
  control.replace = setSection;
  return <KeywordsSection section={section} run={RUN} overlay={OVERLAY} />;
}

beforeEach(() => {
  mockApi({});
});

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('KeywordsSection', () => {
  describe('the section and its sub-lists', () => {
    it('renders the coverage finding above the Block, Expand and Review tabs, in API order', async () => {
      await renderKeywords(defaultKeywords());

      const region = screen.getByRole('region', { name: 'Keywords to adjust' });
      expect(within(region).getByRole('heading', { level: 2 }).textContent).toBe(
        'Keywords to adjust',
      );
      const finding = within(region).getByText(/^Search terms cover 74% of keyword spend/u);
      const tablist = within(region).getByRole('tablist', { name: KEYWORD_TABS_LABEL });
      expect(
        finding.compareDocumentPosition(tablist) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      // AAP 0.5.5: the alert role is reserved for the open-discrepancies banner.
      expect(screen.queryByRole('alert')).toBeNull();

      expect(tabNames()).toEqual(DEFAULT_TAB_NAMES);
      // The tab counts add up to the section's navigation count.
      expect(tabCountTotal()).toBe(visibleItemCount(defaultKeywords()));
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 1');
      // Each panel element is its group's anchor and always exists, so #group-keywords-{key}
      // resolves, and the tab controls it, even before the panel first mounts its cards.
      for (const [index, key] of ['block', 'expand', 'review'].entries()) {
        const anchor = region.querySelector(`#group-keywords-${key}`);
        expect(anchor?.getAttribute('role')).toBe('tabpanel');
        expect(screen.getAllByRole('tab')[index]?.getAttribute('aria-controls')).toBe(
          `group-keywords-${key}`,
        );
      }
    });

    it('mounts a sub-list\'s cards only once its tab is first selected, and keeps them after', async () => {
      await renderKeywords(defaultKeywords());

      const review = document.getElementById('group-keywords-review');
      const action = 'Review keyword: bid, match type or landing page';
      expect(review?.hidden).toBe(true);
      expect(review?.childElementCount).toBe(0);
      expect(screen.queryByRole('article', { name: action, hidden: true })).toBeNull();

      // Keyboard switching selects and mounts it: End moves to the last tab.
      const block = screen.getByRole('tab', { name: 'Block / negative-keyword candidates 1' });
      block.focus();
      fireEvent.keyDown(block, { key: 'End' });
      expect(selectedTabName()).toBe('Review 1');
      expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Review 1' }));
      expect(within(visiblePanel()).getByRole('article', { name: action })).toBeTruthy();
      expect(visiblePanel()).toBe(review);

      // Back on Block, the Review cards stay mounted in their hidden panel.
      fireEvent.keyDown(screen.getByRole('tab', { name: 'Review 1' }), { key: 'Home' });
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 1');
      expect(review?.hidden).toBe(true);
      expect(review?.querySelector('article')?.textContent).toContain(action);
    });

    it('shows the K1 block card\'s six fields in order (term, campaign and ad group, spend, conversions, cost/conversion "—", reason)', async () => {
      await renderKeywords(defaultKeywords());

      const card = within(visiblePanel()).getByRole('article', {
        name: 'Add as exact-match negative',
      });
      expect(within(card).getByRole('heading', { level: 3 }).textContent).toBe(
        'Add as exact-match negative',
      );
      const reason = pageItem(FIXTURE_ITEM_IDS.k1).reason;
      expect(keywordDetails('Add as exact-match negative')).toEqual([
        'Term: free crm template',
        CORE_CRM_ENTRY,
        'Spend: USD\u00a0186.40',
        'Conversions: 0',
        'Cost/conversion: —',
        // The reason sentence, the specific reason it is flagged, is the API's, shown verbatim.
        `Reason: ${reason}`,
      ]);
      // The reason is the last field and the card prints no second copy above the fields.
      expect(occurrences(card, reason)).toBe(1);
      // The campaign and ad group are the second field of the one list, before the evidence
      // table, with no separate list for them.
      expectOneDetailsList(card);
      expectDisplayedSequence(card, 'free crm template', CORE_CRM_CAMPAIGN_AND_AD_GROUP, reason);
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByRole('table', { name: 'Evidence' }),
      );
      // A block candidate shows its wasted spend (AAP 0.5.4.3 K1) in the card's evidence.
      const evidence = within(card).getByRole('table', { name: 'Evidence' });
      expect(within(evidence).getByText('Wasted spend').closest('tr')?.textContent).toContain(
        'USD\u00a0186.40',
      );
      // Every keyword card of a low-coverage account carries the coverage flag.
      expect(within(card).getByText('Low search-term coverage: 74% of keyword spend')).toBeTruthy();
      // Only K2 cards list adjacent terms and only K3 cards show review evidence.
      expect(within(card).queryByRole('list', { name: ADJACENT_TERMS_TITLE })).toBeNull();
      expect(within(card).queryByRole('list', { name: REVIEW_EVIDENCE_LABEL })).toBeNull();
    });

    it('switches to Expand and lists the K2 adjacent terms from the search-term report', async () => {
      await renderKeywords(defaultKeywords());

      fireEvent.click(screen.getByRole('tab', { name: 'Expansion candidates 1' }));

      expect(selectedTabName()).toBe('Expansion candidates 1');
      const action = 'Add as phrase-match keyword in CRM Software';
      const reason = pageItem(FIXTURE_ITEM_IDS.k2).reason;
      expect(keywordDetails(action)).toEqual([
        'Term: crm for small business',
        CORE_CRM_ENTRY,
        'Spend: USD\u00a0240.00',
        'Conversions: 4',
        'Cost/conversion: USD\u00a060.00',
        `Reason: ${reason}`,
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
      expect(occurrences(card, reason)).toBe(1);
      expectOneDetailsList(card);
      // The six fields, then the adjacent terms, displayed in that order, all before the
      // evidence table.
      expectDisplayedSequence(
        card,
        'crm for small business',
        CORE_CRM_CAMPAIGN_AND_AD_GROUP,
        reason,
        [ADJACENT_TERMS_TITLE],
      );
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByText(ADJACENT_TERMS_TITLE),
        within(card).getByRole('list', { name: ADJACENT_TERMS_TITLE }),
        within(card).getByRole('table', { name: 'Evidence' }),
      );
      expect(listEntries(card, ADJACENT_TERMS_TITLE)).toEqual([
        'crm for small teams (1 conversion, 9 clicks)',
        'small business crm software (0 conversions, 7 clicks)',
        'best crm for small business (0 conversions, 5 clicks)',
      ]);
      // The visible title says where the terms come from.
      expect(within(card).getByText(ADJACENT_TERMS_TITLE).getAttribute('aria-hidden')).toBe('true');
      expect(within(card).queryByRole('list', { name: REVIEW_EVIDENCE_LABEL })).toBeNull();
    });

    it('switches to Review and shows the K3 quality score and CPC and cost/conversion by half', async () => {
      await renderKeywords(defaultKeywords());

      fireEvent.click(screen.getByRole('tab', { name: 'Review 1' }));

      expect(selectedTabName()).toBe('Review 1');
      const action = 'Review keyword: bid, match type or landing page';
      const reason = pageItem(FIXTURE_ITEM_IDS.k3).reason;
      expect(keywordDetails(action)).toEqual([
        'Term: crm software',
        CORE_CRM_ENTRY,
        'Spend: USD\u00a01,205.40',
        'Conversions: 14',
        'Cost/conversion: USD\u00a086.10',
        `Reason: ${reason}`,
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
      expect(occurrences(card, reason)).toBe(1);
      expectOneDetailsList(card);
      // The six fields, then the review evidence, displayed in that order, all before the
      // evidence table.
      expectDisplayedSequence(card, 'crm software', CORE_CRM_CAMPAIGN_AND_AD_GROUP, reason, [
        'Quality score: ',
        'CPC by half: ',
        'Cost/conversion by half: ',
      ]);
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByRole('list', { name: REVIEW_EVIDENCE_LABEL }),
        within(card).getByRole('table', { name: 'Evidence' }),
      );
      expect(listEntries(card, REVIEW_EVIDENCE_LABEL)).toEqual([
        'Quality score: 7 → 5.8',
        'CPC by half: USD\u00a02.10\u00A0/\u00A0USD\u00a02.52',
        'Cost/conversion by half: USD\u00a075.00\u00A0/\u00A0USD\u00a097.20',
      ]);
      expect(within(card).queryByRole('list', { name: ADJACENT_TERMS_TITLE })).toBeNull();
    });

    it('opens on the first sub-list with a visible card and states the K1 minimum in an empty Block tab', async () => {
      // The run-8 shape: K1 terms withheld at 70 clicks required, then a K2 term.
      const withheld = [
        k1Withheld('free attribution software download', 44, 70),
        k1Withheld('attribution modeling', 28, 70),
        makeWithheld({
          rule_id: 'K2',
          reason: 'conversions 1 of 3 required',
          sample: { observed: 1, required: 3, unit: 'conversions' },
        }),
      ];
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
            makeGroup({
              key: 'expand',
              title: 'Expansion candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k2)],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [pageItem(FIXTURE_ITEM_IDS.k3)] }),
          ],
          withheld,
        }),
      );

      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 0',
        'Expansion candidates 1',
        'Review 1',
      ]);
      expect(selectedTabName()).toBe('Expansion candidates 1');
      expect(
        within(visiblePanel()).getByRole('article', {
          name: 'Add as phrase-match keyword in CRM Software',
        }),
      ).toBeTruthy();

      fireEvent.click(screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' }));

      const panel = visiblePanel();
      // AAP 0.5.5's sentence, its minimum the K1 lines' recorded sample.required.
      expect(emptyStateTitle(panel)).toBe('No search term met the K1 minimum of 70 clicks');
      expect(within(panel).queryByRole('article')).toBeNull();
      expect(panel.id).toBe('group-keywords-block');
      // The withheld lines are listed once, below the tabs, and not repeated in the panel.
      expect(listEntries(document.body, 'Withheld for sample size')).toHaveLength(3);
      expect(panel.textContent).not.toContain('attribution modeling');
    });

    it('states an account-wide K3 line in an empty Review tab, and "No rule fired" for a group with no known rule', async () => {
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({
              key: 'block',
              title: 'Block / negative-keyword candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k1)],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [] }),
            makeGroup({ key: 'future', title: 'Future rule', items: [] }),
          ],
          withheld: [
            k1Withheld('attribution modeling', 28, 70),
            makeWithheld({
              rule_id: 'K3',
              entity: null,
              reason: 'window 7 days of 14 required',
              sample: { observed: 7, required: 14, unit: 'days' },
            }),
          ],
        }),
      );

      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe('K3 withheld: window 7 days of 14 required');

      fireEvent.click(screen.getByRole('tab', { name: 'Future rule 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe('No rule fired for this window');
    });

    it('counts each sub-list\'s visible cards (2, 1, 0) in API order and says K3 did not fire on an empty Review tab', async () => {
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({
              key: 'block',
              title: 'Block / negative-keyword candidates',
              items: [k1With(51, [], null), k1With(52, [], null)],
            }),
            makeGroup({
              key: 'expand',
              title: 'Expansion candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k2)],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [] }),
          ],
          // Only another rule's line is withheld, which the Review tab never quotes.
          withheld: [k1Withheld('attribution modeling', 28, 70)],
        }),
      );

      // One tab per API group, in API order, each its API title followed by its card count.
      const tablist = screen.getByRole('tablist', { name: KEYWORD_TABS_LABEL });
      expect(within(tablist).getAllByRole('tab').map(tabText)).toEqual([
        'Block / negative-keyword candidates 2',
        'Expansion candidates 1',
        'Review 0',
      ]);
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 2');
      expect(within(visiblePanel()).getAllByRole('article')).toHaveLength(2);

      // An empty group still gets its tab, whose panel says so instead of showing no card.
      fireEvent.click(within(tablist).getByRole('tab', { name: 'Review 0' }));

      expect(selectedTabName()).toBe('Review 0');
      const panel = visiblePanel();
      expect(emptyStateTitle(panel)).toBe('K3 did not fire for this window');
      expect(within(panel).queryByRole('article')).toBeNull();
      expect(panel.id).toBe('group-keywords-review');
    });

    it('names the section\'s suppression counts, never "did not fire", on an empty tab whose rule has no withheld line', async () => {
      // The default run after both K3 cards were dismissed: the rerun has no K3 item and no K3
      // withheld line, and the section reports the 2 dismissals (per section, never per rule).
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({
              key: 'block',
              title: 'Block / negative-keyword candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k1)],
            }),
            makeGroup({
              key: 'expand',
              title: 'Expansion candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k2)],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [] }),
          ],
          // Only another rule's line is withheld, which the Review tab never quotes.
          withheld: [k1Withheld('attribution modeling', 28, 70)],
          suppressed: { feedback_dismiss: 2 },
        }),
      );

      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      const panel = visiblePanel();
      expect(emptyStateTitle(panel)).toBe(
        'No K3 action to show; not shown in this section: 2 dismissed',
      );
      expect(panel.textContent).not.toContain('did not fire');
      // The frame's "Not shown" list writes the same count the same way.
      expect(listEntries(document.body, 'Not shown')).toEqual(['2 dismissed']);
    });

    it('writes every reported suppression count as the frame does, with g1_blocked singular at 1, and skips zero and non-finite counts', async () => {
      const control: SectionControl = {};
      const section = (suppressed: Section['suppressed']): Section =>
        makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
            makeGroup({
              key: 'expand',
              title: 'Expansion candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k2)],
            }),
            makeGroup({ key: 'future', title: 'Future rule', items: [] }),
          ],
          suppressed,
        });
      await renderWithProviders(
        <Harness
          initial={section({
            g1_blocked: 1,
            feedback_snooze: 0,
            feedback_done: 3,
            tracking_absent: Number.NaN,
            feedback_dismiss: 1,
          })}
          control={control}
        />,
      );

      fireEvent.click(screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe(
        'No K1 action to show; not shown in this section: 1 spend increase blocked by the ' +
          'high-utilization guard; 3 marked done; 1 dismissed',
      );
      expect(listEntries(document.body, 'Not shown')).toEqual([
        '1 spend increase blocked by the high-utilization guard',
        '3 marked done',
        '1 dismissed',
      ]);
      // A group key with no known rule keeps the frame's sentence.
      fireEvent.click(screen.getByRole('tab', { name: 'Future rule 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe('No rule fired for this window');

      // Plural labels above 1, and the other labels as the frame writes them.
      act(() => {
        control.replace?.(
          section({ g1_blocked: 2, feedback_snooze: 4, tracking_absent: 5, unknown_reason: 6 }),
        );
      });
      fireEvent.click(screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe(
        'No K1 action to show; not shown in this section: 2 spend increases blocked by the ' +
          'high-utilization guard; 4 snoozed; 5 suppressed until tracking is verified; ' +
          '6 unknown_reason',
      );

      // Only zero and non-finite counts: nothing is reported, so the rule did not fire.
      act(() => {
        control.replace?.(
          section({ feedback_dismiss: 0, g1_blocked: Number.POSITIVE_INFINITY, feedback_done: -1 }),
        );
      });
      expect(emptyStateTitle(visiblePanel())).toBe('K1 did not fire for this window');
      expect(screen.queryByRole('list', { name: 'Not shown' })).toBeNull();
    });

    it('keeps the rule\'s withheld line ahead of the section\'s suppression counts', async () => {
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
            makeGroup({ key: 'review', title: 'Review', items: [pageItem(FIXTURE_ITEM_IDS.k3)] }),
          ],
          withheld: [k1Withheld('free attribution software download', 44, 70)],
          suppressed: { feedback_dismiss: 2 },
        }),
      );

      fireEvent.click(screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe(
        'No search term met the K1 minimum of 70 clicks',
      );
    });

    it('keeps the cards feedback hides now ahead of the withheld line and the suppression counts', async () => {
      const hiddenK3 = {
        ...pageItem(FIXTURE_ITEM_IDS.k3),
        current: makeItemOverlay({ hidden_by: 'dismiss' }),
      };
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({
              key: 'block',
              title: 'Block / negative-keyword candidates',
              items: [pageItem(FIXTURE_ITEM_IDS.k1)],
            }),
            makeGroup({ key: 'review', title: 'Review', items: [hiddenK3] }),
          ],
          withheld: [
            makeWithheld({
              rule_id: 'K3',
              entity: null,
              reason: 'window 7 days of 14 required',
              sample: { observed: 7, required: 14, unit: 'days' },
            }),
          ],
          suppressed: { feedback_dismiss: 2 },
        }),
      );

      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe(
        'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
      );
      expect(listEntries(document.body, 'Not shown')).toEqual([
        '2 dismissed',
        '1 hidden since this run',
      ]);
    });

    it('opens on a sub-list with only a held card, which the tab counts, and leaves a hidden card out of its count', async () => {
      const heldK2 = { ...pageItem(FIXTURE_ITEM_IDS.k2), held: true };
      const hiddenK3 = {
        ...pageItem(FIXTURE_ITEM_IDS.k3),
        current: makeItemOverlay({ hidden_by: 'snooze' }),
      };
      const section = keywordsWith(
        makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
        makeGroup({ key: 'expand', title: 'Expansion candidates', items: [heldK2] }),
        makeGroup({ key: 'review', title: 'Review', items: [hiddenK3] }),
      );
      await renderKeywords(section);

      // The held card is counted, as the section's navigation counts it; the snoozed one is not.
      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 0',
        'Expansion candidates 1',
        'Review 0',
      ]);
      expect(tabCountTotal()).toBe(visibleItemCount(section));
      expect(selectedTabName()).toBe('Expansion candidates 1');
      const toggle = within(visiblePanel()).getByRole('button', {
        name: 'Held until discrepancies are resolved (1)',
      });
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(toggle);
      const heldAction = 'Add as phrase-match keyword in CRM Software';
      const heldCard = within(visiblePanel()).getByRole('article', { name: heldAction });
      // Held behind the disclosure, the card keeps the six fields in order, the campaign and ad
      // group second and the reason once and last.
      expect(keywordDetails(heldAction)).toEqual([
        'Term: crm for small business',
        CORE_CRM_ENTRY,
        'Spend: USD\u00a0240.00',
        'Conversions: 4',
        'Cost/conversion: USD\u00a060.00',
        `Reason: ${heldK2.reason}`,
      ]);
      expect(occurrences(heldCard, heldK2.reason)).toBe(1);
      expectOneDetailsList(heldCard);
      // The snoozed K3 is counted under "Not shown", never rendered.
      expect(listEntries(document.body, 'Not shown')).toEqual(['1 hidden since this run']);
      // The Review tab's only card is snoozed, so its reason names the hidden card.
      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe(
        'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
      );
      // The Block tab, empty with nothing withheld, says its rule did not fire.
      fireEvent.click(screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' }));
      expect(emptyStateTitle(visiblePanel())).toBe('K1 did not fire for this window');
    });

    it('counts a card computed from replaced data in its tab, shows it openly and opens on it before a held-only tab', async () => {
      const heldK2 = { ...pageItem(FIXTURE_ITEM_IDS.k2), held: true };
      const historicalK3: Item = {
        ...pageItem(FIXTURE_ITEM_IDS.k3),
        current: makeItemOverlay({
          historical: true,
          held_now: true,
          held_reason_now: 'Computed from data that has since been replaced',
          data_status_now: 'live',
        }),
      };
      const section = keywordsWith(
        makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
        makeGroup({ key: 'expand', title: 'Expansion candidates', items: [heldK2] }),
        makeGroup({ key: 'review', title: 'Review', items: [historicalK3] }),
      );
      await renderKeywords(section);

      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 0',
        'Expansion candidates 1',
        'Review 1',
      ]);
      expect(tabCountTotal()).toBe(visibleItemCount(section));
      expect(selectedTabName()).toBe('Review 1');
      const panel = visiblePanel();
      // Readable at once, never behind "Held until discrepancies are resolved".
      expect(within(panel).queryByRole('button', { name: /^Held until/u })).toBeNull();
      const block = within(panel).getByRole('group', { name: 'Computed from replaced data (1)' });
      const card = within(block).getByRole('article', {
        name: 'Review keyword: bid, match type or landing page',
      });
      expect(within(card).getByText('Computed from replaced data')).toBeTruthy();
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(card).getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
      }
      // In the replaced-data block, the card keeps the six fields in order, the campaign and ad
      // group second and the reason once and last.
      expect(listEntries(card, KEYWORD_DETAILS_LABEL)).toEqual([
        'Term: crm software',
        CORE_CRM_ENTRY,
        'Spend: USD\u00a01,205.40',
        'Conversions: 14',
        'Cost/conversion: USD\u00a086.10',
        `Reason: ${historicalK3.reason}`,
      ]);
      expect(occurrences(card, historicalK3.reason)).toBe(1);
      expectOneDetailsList(card);
    });

    it('counts every held card when open discrepancies hold every sub-list (3, 4, 2), matching the section count, and keeps them collapsed', async () => {
      const heldNow = (item: Item): Item => ({
        ...item,
        current: makeItemOverlay({
          held_now: true,
          held_reason_now: 'Held until discrepancies are resolved',
        }),
      });
      const block = [61, 62, 63].map((id) => heldNow(k1With(id, [], null)));
      const expand = [71, 72, 73, 74].map((id) => heldNow(k2With(id, [])));
      const review = [81, 82].map((id) =>
        heldNow(makeItem({ id, rule_id: 'K3', action: `Review keyword ${String(id)}` })),
      );
      const section = keywordsWith(
        makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: block }),
        makeGroup({ key: 'expand', title: 'Expansion candidates', items: expand }),
        makeGroup({ key: 'review', title: 'Review', items: review }),
      );
      await renderKeywords(section);

      // Each tab counts its held cards, and the three counts add up to the section's count.
      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 3',
        'Expansion candidates 4',
        'Review 2',
      ]);
      expect(visibleItemCount(section)).toBe(9);
      expect(tabCountTotal()).toBe(visibleItemCount(section));
      // With nothing visible, the section opens on the first sub-list holding a held card.
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 3');

      for (const [tab, count] of [
        ['Block / negative-keyword candidates 3', 3],
        ['Expansion candidates 4', 4],
        ['Review 2', 2],
      ] as const) {
        fireEvent.click(screen.getByRole('tab', { name: tab }));
        const panel = visiblePanel();
        // The counted cards stay collapsed behind the held disclosure until it is expanded.
        expect(panel.querySelector('.ds-emptystate')).toBeNull();
        expect(within(panel).queryAllByRole('article')).toHaveLength(0);
        const toggle = within(panel).getByRole('button', {
          name: `Held until discrepancies are resolved (${String(count)})`,
        });
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        fireEvent.click(toggle);
        expect(toggle.getAttribute('aria-expanded')).toBe('true');
        expect(within(panel).getAllByRole('article')).toHaveLength(count);
      }
    });

    it('keeps the chosen tab across a refetch and falls back to the default when its group goes', async () => {
      const control: SectionControl = {};
      await renderWithProviders(<Harness initial={defaultKeywords()} control={control} />);

      fireEvent.click(screen.getByRole('tab', { name: 'Review 1' }));
      expect(selectedTabName()).toBe('Review 1');

      // A refetch returns equal data as new objects: the choice stays.
      act(() => {
        control.replace?.(defaultKeywords());
      });
      expect(selectedTabName()).toBe('Review 1');

      // The latest response no longer has a Review group: the default tab is selected.
      const withoutReview = defaultKeywords();
      withoutReview.groups = withoutReview.groups.filter((group) => group.key !== 'review');
      act(() => {
        control.replace?.(withoutReview);
      });
      expect(tabNames()).toEqual(DEFAULT_TAB_NAMES.slice(0, 2));
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 1');
    });

    it('labels a tab with its group key when the title is blank or missing', async () => {
      // A malformed payload whose title is not a string at all.
      const missingTitle = null as unknown as string;
      await renderKeywords(
        keywordsWith(
          makeGroup({ key: 'block', title: '  ', items: [pageItem(FIXTURE_ITEM_IDS.k1)] }),
          makeGroup({ key: 'expand', title: missingTitle, items: [] }),
        ),
      );

      expect(tabNames()).toEqual(['block 1', 'expand 0']);
      fireEvent.click(screen.getByRole('tab', { name: 'expand 0' }));
      // The reason is the rule's, whatever the tab's label.
      expect(emptyStateTitle(visiblePanel())).toBe('K2 did not fire for this window');
    });

    it('shows the frame empty state with its reasons, and no tabs, when no card can be shown', async () => {
      const hiddenK1 = {
        ...pageItem(FIXTURE_ITEM_IDS.k1),
        current: makeItemOverlay({ hidden_by: 'dismiss' }),
      };
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [hiddenK1] }),
          ],
          findings: [makeFinding()],
        }),
      );

      expect(screen.queryByRole('tablist')).toBeNull();
      expect(emptyStateTitle(document.body)).toBe(
        'No actions to show: 1 hidden by feedback since this run (dismissed, snoozed or done)',
      );
      // The coverage finding is the banner above, and only that: the empty state names the
      // hidden card instead of repeating it.
      expect(screen.getAllByText(/^Search terms cover 74% of keyword spend/u)).toHaveLength(1);
    });

    it('gives a withheld K1 term as the empty state\'s reason, with no tabs, when no group has an item', async () => {
      await renderKeywords(
        makeSection('keywords', {
          groups: [
            makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
            makeGroup({ key: 'expand', title: 'Expansion candidates', items: [] }),
            makeGroup({ key: 'review', title: 'Review', items: [] }),
          ],
          withheld: [makeWithheld()],
        }),
      );

      // Empty groups never open a tab set of empty panels: the frame's empty state stands alone.
      expect(screen.queryByRole('tablist')).toBeNull();
      expect(screen.queryByRole('tab')).toBeNull();

      // The reason is the leading K1 line's recorded minimum (the fixture's 37 clicks), in
      // AAP 0.5.5's words.
      expect(emptyStateTitle(document.body)).toBe(
        'No search term met the K1 minimum of 37 clicks',
      );
      // The withheld line, "{rule} · {term}: {reason} — {observed} / {required} {unit}", with the
      // recorded sample of the fixture (30 clicks of the 37 required), is listed once, below the
      // empty state, and not repeated inside it. Its figure is joined by no-break spaces
      // (U+00A0): getByText matches the rendered text with them collapsed, and the list entry's
      // exact text keeps them.
      const line = 'K1 · crm pricing comparison: clicks 30 of 37 required — 30 / 37 clicks';
      const exactLine =
        'K1 · crm pricing comparison: clicks 30 of 37 required — 30\u00A0/\u00A037 clicks';
      expect(listEntries(document.body, 'Withheld for sample size')).toEqual([exactLine]);
      expect(screen.getAllByText(line)).toHaveLength(1);
    });
  });

  describe('keyword card field order', () => {
    it('displays exactly Term, Campaign and ad group, Spend, Conversions, Cost/conversion and Reason in one list on every K1, K2 and K3 card, nothing between them, the reason once and last', async () => {
      await renderKeywords(defaultKeywords());

      const cards: readonly (readonly [string, number, string, readonly string[]])[] = [
        ['Block / negative-keyword candidates 1', FIXTURE_ITEM_IDS.k1, 'free crm template', []],
        [
          'Expansion candidates 1',
          FIXTURE_ITEM_IDS.k2,
          'crm for small business',
          [ADJACENT_TERMS_TITLE],
        ],
        ['Review 1', FIXTURE_ITEM_IDS.k3, 'crm software', ['Quality score: ']],
      ];
      for (const [tab, id, term, followers] of cards) {
        fireEvent.click(screen.getByRole('tab', { name: tab }));
        const item = pageItem(id);
        const card = within(visiblePanel()).getByRole('article', { name: item.action });
        const entries = keywordDetails(item.action);

        // AAP 0.5.5: term → campaign and ad group → spend → conversions → cost/conversion →
        // reason, and nothing else.
        const labels = entries.map((entry) => entry.slice(0, entry.indexOf(':')));
        expect(labels).toEqual([
          'Term',
          'Campaign and ad group',
          'Spend',
          'Conversions',
          'Cost/conversion',
          'Reason',
        ]);
        // AAP 0.1.2's five keep their order around the campaign and ad group.
        expect(labels.filter((label) => label !== 'Campaign and ad group')).toEqual([
          'Term',
          'Spend',
          'Conversions',
          'Cost/conversion',
          'Reason',
        ]);
        // The Term entry is the term alone, the campaign and ad group come second, and the
        // reason is the last of the six.
        expect(entries[0]).toBe(`Term: ${term}`);
        expect(entries[1]).toBe(CORE_CRM_ENTRY);
        expect(entries[5]).toBe(`Reason: ${item.reason}`);
        // One list holds all six: there is no separate campaign-and-ad-group list.
        expectOneDetailsList(card);
        // The reason is printed once, as the last field, and no reason line precedes the fields.
        expect(occurrences(card, item.reason)).toBe(1);
        // As displayed: the six in order with nothing between them, then the rule's extras, all
        // before the evidence table.
        expectDisplayedSequence(card, term, CORE_CRM_CAMPAIGN_AND_AD_GROUP, item.reason, followers);
        expectInOrder(
          within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
          within(card).getByRole('table', { name: 'Evidence' }),
        );
      }
    });
  });

  describe('keyword card values', () => {
    it('reads "—" for cost/conversion at a known zero in any form, and counts fractions as converting', async () => {
      const cost = metric('cost_per_conversion', '100.00', 'currency', 'USD');
      const spend = metric('spend', '30.00', 'currency', 'USD');
      const extra = { campaign: 'Core CRM — US', ad_group: 'CRM Software' };
      const items = [
        k1With(11, [spend, metric('conversions', 0, 'conversions'), cost], extra),
        k1With(12, [spend, metric('conversions', '0.000000', 'conversions'), cost], extra),
        k1With(13, [spend, metric('conversions', '-0', 'conversions'), cost], extra),
        k1With(14, [spend, metric('conversions', '0.3', 'conversions'), cost], extra),
        // Neither a number nor a decimal string: not a known zero.
        k1With(15, [spend, metric('conversions', 'n/a', 'text'), cost], extra),
        k1With(16, [spend, metric('conversions', false, 'conversions'), cost], extra),
      ];
      await renderKeywords(keywordsWith(makeGroup({ items })));

      // Cost/conversion is the fifth of the six fields, before the reason.
      const costLines = cardLists(visiblePanel(), KEYWORD_DETAILS_LABEL).map(
        (entries) => entries[4],
      );
      expect(costLines).toEqual([
        'Cost/conversion: —',
        'Cost/conversion: —',
        'Cost/conversion: —',
        'Cost/conversion: USD\u00a0100.00',
        'Cost/conversion: USD\u00a0100.00',
        'Cost/conversion: USD\u00a0100.00',
      ]);
      // A fractional conversion is shown as returned, never rounded to 0.
      expect(within(visiblePanel()).getByText('Conversions: 0.3')).toBeTruthy();
    });

    it('reads "unavailable" for values the evidence lacks, never 0', async () => {
      const unreported = k1With(
        21,
        [
          metric('conversions', null, 'conversions'),
          metric('cost_per_conversion', null, 'currency', 'USD'),
        ],
        { campaign: 'Core CRM — US' },
      );
      const noAdGroupOnly = k1With(22, [], { ad_group: 'CRM Software' });
      const noNames = k1With(23, [], null);
      // Nothing names the term (no metric, no extra, no entity name) and the reason is blank.
      const blankBase = makeItem();
      const unnamed = makeItem({
        id: 24,
        action: 'Add as exact-match negative 24',
        reason: '   ',
        entity: { ...blankBase.entity, name: null },
        evidence: makeEvidence({ metrics: [], extra: null }),
      });
      await renderKeywords(
        keywordsWith(makeGroup({ items: [unreported, noAdGroupOnly, noNames, unnamed] })),
      );

      const reason = makeItem().reason;
      // The second field names the unknown part, or reads "unavailable" alone when the evidence
      // names neither the campaign nor the ad group.
      expect(cardLists(visiblePanel(), KEYWORD_DETAILS_LABEL)).toEqual([
        [
          // The term falls back to the entity name when the evidence names none.
          'Term: term 21',
          'Campaign and ad group: Core CRM — US · unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: term 22',
          'Campaign and ad group: unavailable · CRM Software',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: term 23',
          'Campaign and ad group: unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: unavailable',
          'Campaign and ad group: unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          'Reason: unavailable',
        ],
      ]);
      // A blank reason leaves no empty reason line on the card either: the body starts with
      // the six fields.
      const unnamedCard = within(visiblePanel()).getByRole('article', {
        name: 'Add as exact-match negative 24',
      });
      expect(
        unnamedCard.querySelector('.ds-card__body')?.textContent?.startsWith('Term: unavailable'),
      ).toBe(true);
      // Missing values keep the displayed order of the six fields, in one list.
      expectOneDetailsList(unnamedCard);
      expectDisplayedSequence(unnamedCard, 'unavailable', 'unavailable', 'unavailable');
    });

    it('lists at most five adjacent terms, plain or with counts, and none when there are none', async () => {
      const many = k2With(31, [
        'crm demo',
        { term: 'crm trial' },
        { term: 'crm onboarding', conversions: '0.5', clicks: 1 },
        { term: 'crm setup', conversions: '1' },
        { nothing: 'here' },
        42,
        { term: 'crm import', clicks: 3 },
        { term: 'crm migration', clicks: 2 },
      ]);
      const none = k2With(32, []);
      await renderKeywords(keywordsWith(makeGroup({ items: [many, none] })));

      const manyCard = within(visiblePanel()).getByRole('article', {
        name: 'Add as phrase-match keyword 31',
      });
      expect(listEntries(manyCard, ADJACENT_TERMS_TITLE)).toEqual([
        'crm demo',
        'crm trial',
        'crm onboarding (0.5 conversions, 1 click)',
        'crm setup (1 conversion)',
        'crm import (3 clicks)',
      ]);
      const noneCard = within(visiblePanel()).getByRole('article', {
        name: 'Add as phrase-match keyword 32',
      });
      expect(within(noneCard).queryByRole('list', { name: ADJACENT_TERMS_TITLE })).toBeNull();
      expect(within(noneCard).queryByText(ADJACENT_TERMS_TITLE)).toBeNull();
    });

    it('reads "unavailable" in each K3 review field the evidence lacks', async () => {
      const k3 = makeItem({
        id: 41,
        rule_id: 'K3',
        action: 'Review keyword 41',
        evidence: makeEvidence({
          metrics: [
            metric('term', 'crm suite', 'text'),
            metric('qs_last', '6.5', 'score'),
            metric('cpc_first_half', '1.80', 'currency', 'USD'),
          ],
          extra: { campaign: 'Core CRM — US', ad_group: 'CRM Suite' },
        }),
      });
      await renderKeywords(keywordsWith(makeGroup({ key: 'review', title: 'Review', items: [k3] })));

      const card = within(visiblePanel()).getByRole('article', { name: 'Review keyword 41' });
      expect(listEntries(card, REVIEW_EVIDENCE_LABEL)).toEqual([
        'Quality score: unavailable → 6.5',
        'CPC by half: USD\u00a01.80\u00A0/\u00A0unavailable',
        'Cost/conversion by half: unavailable\u00A0/\u00A0unavailable',
      ]);
    });
  });
});

describe('KeywordsSection: the confirmation of a card hidden on the page', () => {
  it("keeps a sub-list whose only card was dismissed selected, with the card's confirmation", async () => {
    const flow = feedbackFlow();
    mockApi(flow.handlers);
    await renderWithProviders(<LiveSection sectionKey="keywords" component={KeywordsSection} />, {
      path: '/actions',
    });
    const k1 = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.k1);
    const card = await screen.findByRole('article', { name: k1.action });
    const dismiss = within(card).getByRole('button', { name: 'Dismiss' });
    dismiss.focus();
    fireEvent.click(dismiss);

    await waitFor(() => {
      expect(screen.queryByRole('article', { name: k1.action })).toBeNull();
    });
    // The Block tab stays selected; its count still leaves the hidden card out.
    const block = screen.getByRole('tab', { name: 'Block / negative-keyword candidates 0' });
    expect(block.getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('tab', { name: 'Expansion candidates 1' })).toBeDefined();
    expect(screen.getByRole('tab', { name: 'Review 1' })).toBeDefined();
    // Its panel shows the confirmation where the card stood, not an empty state.
    const panel = screen.getByRole('tabpanel');
    expect(within(panel).getByText(feedbackConfirmationText('dismiss', k1))).toBeDefined();
    expect(panel.querySelector('.ds-emptystate')).toBeNull();
    expect(panel.textContent).not.toContain('No actions to show');
    // The other sub-lists' cards sit in hidden panels, so focus goes to the section heading.
    const region = screen.getByRole('region', { name: 'Keywords to adjust' });
    await waitFor(() => {
      expect(document.activeElement).toBe(
        within(region).getByRole('heading', { level: 2, name: 'Keywords to adjust' }),
      );
    });
    expect(within(region).getByRole('list', { name: 'Not shown' }).textContent).toContain(
      '1 hidden since this run',
    );
  });
});
