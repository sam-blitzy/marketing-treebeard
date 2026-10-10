/**
 * Component tests for `KeywordsSection`, the "Keywords to adjust" section of `/actions`
 * (AAP 0.5.5 Keywords; AAP 0.5.4.3 rules K1–K3; FR-4).
 *
 * They hold the section to its TSDoc contract and to the AAP's field set:
 * - **Sub-lists as tabs.** One `Tabs` named "Keyword actions" with one tab per API group (Block,
 *   Expand and Review: K1, K2, K3) in API order, each labelled with the group title and its count
 *   of the cards shown openly (visible, and computed from replaced data). The section opens on
 *   the first sub-list with a visible card, else one with a card computed from replaced data,
 *   else one with a held card; an empty sub-list says "No items"; a chosen tab survives a
 *   refetch and falls back to the default when its group disappears. A section with no card to
 *   show renders the frame's empty state with its reasons (feedback, findings, a withheld K1
 *   term) and no tabs.
 * - **The coverage finding.** The `search_term_coverage` finding (search-term spend ÷ keyword
 *   spend, below 80%) sits above the tabs as a banner, never an alert, and every keyword card
 *   carries the low-coverage flag.
 * - **Keyword cards.** Exactly five fields, Term, Spend, Conversions, Cost/conversion and Reason,
 *   displayed in that order with nothing between them (AAP 0.1.2); the Term entry is the term
 *   alone, and the reason sentence appears once on the card, as the last field. A separate
 *   "Keyword context" list, "Campaign and ad group: …", follows the five fields. Cost/conversion
 *   reads "—" at a known 0 conversions (any decimal form) and fractional conversions count as
 *   converting; a missing value reads "unavailable", never 0. K2 cards add up to five adjacent
 *   terms "from your search-term report"; K3 cards add the quality score first → last and CPC
 *   and cost/conversion by half; both follow the context, before the evidence table.
 *
 * The section fetches nothing, so every test stubs the API with no handler: any request fails
 * the test.
 */
import { mockApi } from '../../../test/mockApi';

import { act, fireEvent, screen, within } from '@testing-library/react';
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
import { renderWithProviders } from '../../../test/render';
import {
  ADJACENT_TERMS_TITLE,
  EMPTY_GROUP_TITLE,
  KEYWORD_CONTEXT_LABEL,
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

/** The tab names, in display order. */
function tabNames(): (string | null)[] {
  return screen.getAllByRole('tab').map((tab) => tab.textContent);
}

/** The name of the selected tab. */
function selectedTabName(): string | null {
  const selected = screen
    .getAllByRole('tab')
    .filter((tab) => tab.getAttribute('aria-selected') === 'true');
  expect(selected).toHaveLength(1);
  return selected[0]?.textContent ?? null;
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

/** How many times `text` occurs in the text of `container`. */
function occurrences(container: HTMLElement, text: string): number {
  return (container.textContent ?? '').split(text).length - 1;
}

/** The context entry every default-page keyword card shows after its five fields. */
const CORE_CRM_CONTEXT = 'Campaign and ad group: Core CRM — US · CRM Software';

/** The label that starts the context entry. */
const CONTEXT_ENTRY_PREFIX = 'Campaign and ad group: ';

/**
 * Asserts the card body's displayed text, as a reader meets it: it starts with
 * "Term: {term}", then "Spend: ", "Conversions: ", "Cost/conversion: " and
 * "Reason: {reason}" at strictly increasing positions, with no campaign and ad group anywhere
 * among the five; the context entry starts exactly where the Reason entry ends and appears once;
 * and `followers` (the K2 adjacent-terms title or the K3 review evidence) come after it.
 *
 * @param card - the keyword card
 * @param term - the term the Term entry must hold, alone
 * @param reason - the item's reason sentence
 * @param followers - texts that must appear after the context entry, in this order
 */
function expectDisplayedSequence(
  card: HTMLElement,
  term: string,
  reason: string,
  followers: readonly string[] = [],
): void {
  const text = card.querySelector('.ds-card__body')?.textContent ?? '';
  const reasonEntry = `Reason: ${reason}`;
  const labels = ['Term: ', 'Spend: ', 'Conversions: ', 'Cost/conversion: ', reasonEntry];
  const positions = labels.map((label) => text.indexOf(label));
  // Nothing (no reason line, no context) precedes the Term entry.
  expect(positions[0]).toBe(0);
  positions.forEach((position, index) => {
    expect(position, `"${labels[index] ?? ''}" is displayed`).toBeGreaterThanOrEqual(0);
    if (index > 0) {
      expect(position, `"${labels[index] ?? ''}" follows the field before it`).toBeGreaterThan(
        positions[index - 1] ?? Number.POSITIVE_INFINITY,
      );
    }
  });
  // The Term entry is the term alone: the next thing displayed is the Spend entry.
  expect(text.slice(positions[0], positions[1])).toBe(`Term: ${term}`);
  // Nothing about the campaign or ad group is displayed among the five fields.
  const reasonEnd = (positions[4] ?? 0) + reasonEntry.length;
  expect(text.slice(0, reasonEnd)).not.toContain('Campaign and ad group');
  // The separate context follows the Reason entry directly, and appears once.
  expect(text.indexOf(CONTEXT_ENTRY_PREFIX)).toBe(reasonEnd);
  expect(occurrences(card, CONTEXT_ENTRY_PREFIX)).toBe(1);
  let previous = reasonEnd;
  for (const follower of followers) {
    const position = text.indexOf(follower, previous);
    expect(position, `"${follower}" follows the context`).toBeGreaterThan(previous);
    previous = position;
  }
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

    it('shows the K1 block card\'s five fields in order (term, spend, conversions, cost/conversion "—", reason), then its campaign and ad group', async () => {
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
        'Spend: $186.40',
        'Conversions: 0',
        'Cost/conversion: —',
        // The reason sentence, the specific reason it is flagged, is the API's, shown verbatim.
        `Reason: ${reason}`,
      ]);
      // The reason is the last field and the card prints no second copy above the fields.
      expect(occurrences(card, reason)).toBe(1);
      // The campaign and ad group are separate context, displayed after the five fields and
      // before the evidence table.
      expect(listEntries(card, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
      expectDisplayedSequence(card, 'free crm template', reason);
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByRole('list', { name: KEYWORD_CONTEXT_LABEL }),
        within(card).getByRole('table', { name: 'Evidence' }),
      );
      // A block candidate shows its wasted spend (AAP 0.5.4.3 K1) in the card's evidence.
      const evidence = within(card).getByRole('table', { name: 'Evidence' });
      expect(within(evidence).getByText('Wasted spend').closest('tr')?.textContent).toContain(
        '$186.40',
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
        'Spend: $240.00',
        'Conversions: 4',
        'Cost/conversion: $60.00',
        `Reason: ${reason}`,
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
      expect(occurrences(card, reason)).toBe(1);
      expect(listEntries(card, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
      // The five fields, then the campaign and ad group, then the adjacent terms, displayed in
      // that order, all before the evidence table.
      expectDisplayedSequence(card, 'crm for small business', reason, [ADJACENT_TERMS_TITLE]);
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByRole('list', { name: KEYWORD_CONTEXT_LABEL }),
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
        'Spend: $1,205.40',
        'Conversions: 14',
        'Cost/conversion: $86.10',
        `Reason: ${reason}`,
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
      expect(occurrences(card, reason)).toBe(1);
      expect(listEntries(card, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
      // The five fields, then the campaign and ad group, then the review evidence, displayed in
      // that order, all before the evidence table.
      expectDisplayedSequence(card, 'crm software', reason, [
        'Quality score: ',
        'CPC by half: ',
        'Cost/conversion by half: ',
      ]);
      expectInOrder(
        within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
        within(card).getByRole('list', { name: KEYWORD_CONTEXT_LABEL }),
        within(card).getByRole('list', { name: REVIEW_EVIDENCE_LABEL }),
        within(card).getByRole('table', { name: 'Evidence' }),
      );
      expect(listEntries(card, REVIEW_EVIDENCE_LABEL)).toEqual([
        'Quality score: 7 → 5.8',
        'CPC by half: $2.10 / $2.52',
        'Cost/conversion by half: $75.00 / $97.20',
      ]);
      expect(within(card).queryByRole('list', { name: ADJACENT_TERMS_TITLE })).toBeNull();
    });

    it('opens on the first sub-list with a visible card and says "No items" in an empty one', async () => {
      await renderKeywords(
        keywordsWith(
          makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
          makeGroup({
            key: 'expand',
            title: 'Expansion candidates',
            items: [pageItem(FIXTURE_ITEM_IDS.k2)],
          }),
          makeGroup({ key: 'review', title: 'Review', items: [pageItem(FIXTURE_ITEM_IDS.k3)] }),
        ),
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
      expect(within(panel).getByText(EMPTY_GROUP_TITLE)).toBeTruthy();
      expect(
        within(panel).getByText('No Block / negative-keyword candidates actions in this window'),
      ).toBeTruthy();
      expect(within(panel).queryByRole('article')).toBeNull();
      expect(panel.id).toBe('group-keywords-block');
    });

    it('counts each sub-list\'s visible cards (2, 1, 0) in API order and says "No items" on an empty Review tab', async () => {
      await renderKeywords(
        keywordsWith(
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
        ),
      );

      // One tab per API group, in API order, each its API title followed by its card count.
      const tablist = screen.getByRole('tablist', { name: KEYWORD_TABS_LABEL });
      expect(
        within(tablist)
          .getAllByRole('tab')
          .map((tab) => tab.textContent),
      ).toEqual(['Block / negative-keyword candidates 2', 'Expansion candidates 1', 'Review 0']);
      expect(selectedTabName()).toBe('Block / negative-keyword candidates 2');
      expect(within(visiblePanel()).getAllByRole('article')).toHaveLength(2);

      // An empty group still gets its tab, whose panel says so instead of showing no card.
      fireEvent.click(within(tablist).getByRole('tab', { name: 'Review 0' }));

      expect(selectedTabName()).toBe('Review 0');
      const panel = visiblePanel();
      expect(within(panel).getByText(EMPTY_GROUP_TITLE)).toBeTruthy();
      expect(within(panel).getByText('No Review actions in this window')).toBeTruthy();
      expect(within(panel).queryByRole('article')).toBeNull();
      expect(panel.id).toBe('group-keywords-review');
    });

    it('opens on a sub-list with only a held card, which the tab does not count', async () => {
      const heldK2 = { ...pageItem(FIXTURE_ITEM_IDS.k2), held: true };
      const hiddenK3 = {
        ...pageItem(FIXTURE_ITEM_IDS.k3),
        current: makeItemOverlay({ hidden_by: 'snooze' }),
      };
      await renderKeywords(
        keywordsWith(
          makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
          makeGroup({ key: 'expand', title: 'Expansion candidates', items: [heldK2] }),
          makeGroup({ key: 'review', title: 'Review', items: [hiddenK3] }),
        ),
      );

      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 0',
        'Expansion candidates 0',
        'Review 0',
      ]);
      expect(selectedTabName()).toBe('Expansion candidates 0');
      const toggle = within(visiblePanel()).getByRole('button', {
        name: 'Held until discrepancies are resolved (1)',
      });
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(toggle);
      const heldAction = 'Add as phrase-match keyword in CRM Software';
      const heldCard = within(visiblePanel()).getByRole('article', { name: heldAction });
      // Held behind the disclosure, the card keeps the five fields in order with the reason once
      // and last, then its campaign and ad group.
      expect(keywordDetails(heldAction)).toEqual([
        'Term: crm for small business',
        'Spend: $240.00',
        'Conversions: 4',
        'Cost/conversion: $60.00',
        `Reason: ${heldK2.reason}`,
      ]);
      expect(occurrences(heldCard, heldK2.reason)).toBe(1);
      expect(listEntries(heldCard, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
      // The snoozed K3 is counted under "Not shown", never rendered.
      expect(listEntries(document.body, 'Not shown')).toEqual(['1 hidden since this run']);
      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      expect(within(visiblePanel()).getByText(EMPTY_GROUP_TITLE)).toBeTruthy();
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
      await renderKeywords(
        keywordsWith(
          makeGroup({ key: 'block', title: 'Block / negative-keyword candidates', items: [] }),
          makeGroup({ key: 'expand', title: 'Expansion candidates', items: [heldK2] }),
          makeGroup({ key: 'review', title: 'Review', items: [historicalK3] }),
        ),
      );

      expect(tabNames()).toEqual([
        'Block / negative-keyword candidates 0',
        'Expansion candidates 0',
        'Review 1',
      ]);
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
      // In the replaced-data block, the card keeps the five fields in order with the reason
      // once and last, then its campaign and ad group.
      expect(listEntries(card, KEYWORD_DETAILS_LABEL)).toEqual([
        'Term: crm software',
        'Spend: $1,205.40',
        'Conversions: 14',
        'Cost/conversion: $86.10',
        `Reason: ${historicalK3.reason}`,
      ]);
      expect(occurrences(card, historicalK3.reason)).toBe(1);
      expect(listEntries(card, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
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
      expect(within(visiblePanel()).getByText('No expand actions in this window')).toBeTruthy();
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
      expect(screen.getByText('No actions in this section')).toBeTruthy();
      expect(
        screen.getByText('1 hidden by feedback since this run (dismissed, snoozed or done)'),
      ).toBeTruthy();
      // The coverage finding is both the banner above and a reason line of the empty state.
      expect(screen.getAllByText(/^Search terms cover 74% of keyword spend/u)).toHaveLength(2);
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

      // The withheld line, "{rule} · {term}: {reason} — {observed} / {required} {unit}", with the
      // recorded sample of the fixture (30 clicks of the 37 required).
      const line = 'K1 · crm pricing comparison: clicks 30 of 37 required — 30 / 37 clicks';
      const emptyState = screen.getByText('No actions in this section').parentElement;
      if (emptyState === null) {
        throw new Error('the empty-state title has no container');
      }
      expect(within(emptyState).getByText(line)).toBeTruthy();
      // A section with a withheld pattern has a reason, so the no-reason fallback is not shown.
      expect(within(emptyState).queryByText('No rule fired for this window')).toBeNull();
      // The full withheld list follows the empty state, with the same line.
      expect(listEntries(document.body, 'Withheld for sample size')).toEqual([line]);
      expect(screen.getAllByText(line)).toHaveLength(2);
    });
  });

  describe('keyword card field order', () => {
    it('displays exactly Term, Spend, Conversions, Cost/conversion and Reason on every K1, K2 and K3 card, nothing between them, the reason once, then the campaign and ad group', async () => {
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

        // AAP 0.1.2: term → spend → conversions → cost/conversion → reason, and nothing else.
        expect(entries.map((entry) => entry.slice(0, entry.indexOf(':')))).toEqual([
          'Term',
          'Spend',
          'Conversions',
          'Cost/conversion',
          'Reason',
        ]);
        // The Term entry is the term alone; the reason is the last of the five.
        expect(entries[0]).toBe(`Term: ${term}`);
        expect(entries[4]).toBe(`Reason: ${item.reason}`);
        // The campaign and ad group are a separate list, never one of the five fields.
        expect(entries.some((entry) => entry.includes('Campaign and ad group'))).toBe(false);
        expect(listEntries(card, KEYWORD_CONTEXT_LABEL)).toEqual([CORE_CRM_CONTEXT]);
        // The reason is printed once, as the last field, and no reason line precedes the fields.
        expect(occurrences(card, item.reason)).toBe(1);
        // As displayed: the five in order with nothing between them, then the context, then the
        // rule's extras, all before the evidence table.
        expectDisplayedSequence(card, term, item.reason, followers);
        expectInOrder(
          within(card).getByRole('list', { name: KEYWORD_DETAILS_LABEL }),
          within(card).getByRole('list', { name: KEYWORD_CONTEXT_LABEL }),
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

      // Cost/conversion is the fourth of the five fields, before the reason.
      const costLines = cardLists(visiblePanel(), KEYWORD_DETAILS_LABEL).map(
        (entries) => entries[3],
      );
      expect(costLines).toEqual([
        'Cost/conversion: —',
        'Cost/conversion: —',
        'Cost/conversion: —',
        'Cost/conversion: $100.00',
        'Cost/conversion: $100.00',
        'Cost/conversion: $100.00',
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
      expect(cardLists(visiblePanel(), KEYWORD_DETAILS_LABEL)).toEqual([
        [
          // The term falls back to the entity name when the evidence names none.
          'Term: term 21',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: term 22',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: term 23',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          `Reason: ${reason}`,
        ],
        [
          'Term: unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
          'Reason: unavailable',
        ],
      ]);
      // Each card's separate context names the unknown part, or reads "unavailable" alone when
      // the evidence names neither the campaign nor the ad group.
      expect(cardLists(visiblePanel(), KEYWORD_CONTEXT_LABEL)).toEqual([
        ['Campaign and ad group: Core CRM — US · unavailable'],
        ['Campaign and ad group: unavailable · CRM Software'],
        ['Campaign and ad group: unavailable'],
        ['Campaign and ad group: unavailable'],
      ]);
      // A blank reason leaves no empty reason line on the card either: the body starts with
      // the five fields.
      const unnamedCard = within(visiblePanel()).getByRole('article', {
        name: 'Add as exact-match negative 24',
      });
      expect(
        unnamedCard.querySelector('.ds-card__body')?.textContent?.startsWith('Term: unavailable'),
      ).toBe(true);
      // Missing values keep the displayed order: the five fields, then the context.
      expectDisplayedSequence(unnamedCard, 'unavailable', 'unavailable');
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
        'CPC by half: $1.80 / unavailable',
        'Cost/conversion by half: unavailable / unavailable',
      ]);
    });
  });
});
