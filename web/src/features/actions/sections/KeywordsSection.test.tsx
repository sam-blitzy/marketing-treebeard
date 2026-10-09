/**
 * Component tests for `KeywordsSection`, the "Keywords to adjust" section of `/actions`
 * (AAP 0.5.5 Keywords; AAP 0.5.4.3 rules K1–K3; FR-4).
 *
 * They hold the section to its TSDoc contract and to the AAP's field set:
 * - **Sub-lists as tabs.** One `Tabs` named "Keyword actions" with one tab per API group (Block,
 *   Expand and Review: K1, K2, K3) in API order, each labelled with the group title and its count
 *   of visible cards. The section opens on the first sub-list with a visible card, else one with
 *   a held card; an empty sub-list says "No items"; a chosen tab survives a refetch and falls
 *   back to the default when its group disappears. A section with no card to show renders the
 *   frame's empty state with its reasons (feedback, findings, a withheld K1 term) and no tabs.
 * - **The coverage finding.** The `search_term_coverage` finding (search-term spend ÷ keyword
 *   spend, below 80%) sits above the tabs as a banner, never an alert, and every keyword card
 *   carries the low-coverage flag.
 * - **Keyword cards.** Term, Campaign and ad group, Spend, Conversions and Cost/conversion, in
 *   that order, with the reason sentence on the card; cost/conversion reads "—" at a known 0
 *   conversions (any decimal form) and fractional conversions count as converting; a missing
 *   value reads "unavailable", never 0. K2 cards add up to five adjacent terms "from your
 *   search-term report"; K3 cards add the quality score first → last and CPC and cost/conversion
 *   by half.
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

/** A K1 item with the given id, metrics and `extra`, named `name`. */
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

/** The "Keyword details" entries of the card headed `action`, inside the visible panel. */
function keywordDetails(action: string): (string | null)[] {
  const card = within(visiblePanel()).getByRole('article', { name: action });
  return listEntries(card, KEYWORD_DETAILS_LABEL);
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
      // Each panel holds its group's anchor, so #group-keywords-{key} always resolves.
      for (const key of ['block', 'expand', 'review']) {
        const anchor = region.querySelector(`#group-keywords-${key}`);
        expect(anchor?.closest('[role="tabpanel"]')).not.toBeNull();
      }
    });

    it('shows the K1 block card with term, campaign and ad group, spend, conversions and cost/conversion "—"', async () => {
      await renderKeywords(defaultKeywords());

      const card = within(visiblePanel()).getByRole('article', {
        name: 'Add as exact-match negative',
      });
      expect(within(card).getByRole('heading', { level: 3 }).textContent).toBe(
        'Add as exact-match negative',
      );
      expect(keywordDetails('Add as exact-match negative')).toEqual([
        'Term: free crm template',
        'Campaign and ad group: Core CRM — US · CRM Software',
        'Spend: $186.40',
        'Conversions: 0',
        'Cost/conversion: —',
      ]);
      // The reason sentence, the specific reason it is flagged, is the API's, shown verbatim.
      expect(within(card).getByText(pageItem(FIXTURE_ITEM_IDS.k1).reason)).toBeTruthy();
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
      expect(keywordDetails(action)).toEqual([
        'Term: crm for small business',
        'Campaign and ad group: Core CRM — US · CRM Software',
        'Spend: $240.00',
        'Conversions: 4',
        'Cost/conversion: $60.00',
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
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
      expect(keywordDetails(action)).toEqual([
        'Term: crm software',
        'Campaign and ad group: Core CRM — US · CRM Software',
        'Spend: $1,205.40',
        'Conversions: 14',
        'Cost/conversion: $86.10',
      ]);
      const card = within(visiblePanel()).getByRole('article', { name: action });
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
      expect(panel.querySelector('#group-keywords-block')).not.toBeNull();
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
      expect(panel.querySelector('#group-keywords-review')).not.toBeNull();
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
      expect(
        within(visiblePanel()).getByRole('article', {
          name: 'Add as phrase-match keyword in CRM Software',
        }),
      ).toBeTruthy();
      // The snoozed K3 is counted under "Not shown", never rendered.
      expect(listEntries(document.body, 'Not shown')).toEqual(['1 hidden since this run']);
      fireEvent.click(screen.getByRole('tab', { name: 'Review 0' }));
      expect(within(visiblePanel()).getByText(EMPTY_GROUP_TITLE)).toBeTruthy();
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

      const costLines = within(visiblePanel())
        .getAllByRole('list', { name: KEYWORD_DETAILS_LABEL })
        .map((list) => within(list).getAllByRole('listitem').at(-1)?.textContent);
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
      await renderKeywords(keywordsWith(makeGroup({ items: [unreported, noAdGroupOnly, noNames] })));

      const lists = within(visiblePanel()).getAllByRole('list', { name: KEYWORD_DETAILS_LABEL });
      const entries = lists.map((list) =>
        within(list)
          .getAllByRole('listitem')
          .map((entry) => entry.textContent),
      );
      expect(entries).toEqual([
        [
          // The term falls back to the entity name when the evidence names none.
          'Term: term 21',
          'Campaign and ad group: Core CRM — US · unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
        ],
        [
          'Term: term 22',
          'Campaign and ad group: unavailable · CRM Software',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
        ],
        [
          'Term: term 23',
          'Campaign and ad group: unavailable',
          'Spend: unavailable',
          'Conversions: unavailable',
          'Cost/conversion: unavailable',
        ],
      ]);
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
