/**
 * Component tests for `CreativeSection`, the "Copy/creative changes" section of `/actions`
 * (AAP 0.5.5 Copy; AAP 0.5.4.6 rules CR1–CR3; AAP 0.5.4.2 P7; FR-7).
 *
 * They hold the section to its TSDoc contract and to the AAP's field set:
 * - **One block per campaign**, in API order, each a region headed by the campaign name and
 *   anchored at `groupAnchorId('creative', key)` (`campaign:1` → `group-creative-campaign-1`),
 *   holding its own variants table ("Variants in {campaign}": Creative, Impressions, Click rate,
 *   Open rate, Completion rate, Leads; rates as `formatPercent` renders them at one fixed
 *   precision per column, at least two digits and more where different rates would read alike, "n/a"
 *   only for a stage the campaign's objective does not use, "not estimable" for a missing rate
 *   over a zero denominator, "unavailable" for any other missing rate and for an absent count),
 *   then the cards in API order: the CR3 next test with the CR2 weak stage as its reason, then
 *   the CR1 pairs, those computed from replaced data shown openly under their label, held ones
 *   behind the held disclosure.
 * - **Distinguishability (CR1, P7).** Pair labels are the API's own words, "Distinguishable
 *   (adjusted p = …)" or "{A} ahead, not yet distinguishable …", and the "Not yet
 *   distinguishable" badge is the API's flag; the section never declares a winner.
 * - **Completion-rate units (CR3).** A completion-rate next test is stated in form opens, the
 *   stage's own denominator, with its labelled projection to impressions.
 * - The variants table is evidence: it stays when feedback hides the CR3 card, it is read from
 *   the first item in API order that carries variants (a CR1 pair when the block holds no CR3,
 *   whose next test was withheld or suppressed by feedback), and it is left out only for a block
 *   none of whose items carries variants.
 *
 * - **A card hidden on the page** by its own Dismiss, Snooze or Mark done leaves its
 *   confirmation where it stood in the campaign block, after the variants table and before the
 *   cards computed from replaced data, until a rerun no longer lists it.
 *
 * The section fetches nothing, so every test stubs the API with no handler: any request fails
 * the test. The feedback-flow tests alone render the section from a stubbed
 * `GET /recommendations` (`../../../test/feedbackFlow`), whose feedback post hides the item.
 */
import { mockApi } from '../../../test/mockApi';

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Group, Item, Section } from '../../../api/types';
import {
  FIXTURE_CAMPAIGN_IDS,
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeCompletionRateTestItem,
  makeEvidence,
  makeFlag,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import * as formatModule from '../../../lib/format';
import { formatCount, formatPercent } from '../../../lib/format';
import { LiveSection, feedbackConfirmationText, feedbackFlow } from '../../../test/feedbackFlow';
import { renderWithProviders } from '../../../test/render';
import { LARGE_RENDER_TEST_TIMEOUT_MS } from '../../../test/timeouts';
import * as evidenceExtraModule from '../evidenceExtra';
import {
  CreativeSection,
  NOT_ESTIMABLE,
  VARIANT_RATE_DIGITS,
  VariantsTable,
  formatVariantRate,
} from './CreativeSection';
import { groupAnchorId } from './SectionFrame';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The fixture run and overlay every section render receives. */
const RUN = makeRun();
const OVERLAY = makeOverlay();

/** The campaign of the default page's copy block. */
const PIPELINE_EMEA = 'Lead Gen — Pipeline Report EMEA';

/** The variants table's column headers, in the order AAP 0.5.5 lists them. */
const VARIANT_HEADERS = [
  'Creative',
  'Impressions',
  'Click rate',
  'Open rate',
  'Completion rate',
  'Leads',
];

/** The default page's creative section: the CR3 next test and the historical CR1 pair. */
function defaultCreative(): Section {
  const section = makeRecommendationsPage().sections.find((each) => each.key === 'creative');
  if (section === undefined) {
    throw new Error('the default page has no creative section');
  }
  return section;
}

/** One default-page item, a fresh copy. */
function pageItem(id: number): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** A creative section holding exactly `groups`. */
function creativeWith(...groups: Group[]): Section {
  return makeSection('creative', { groups });
}

/**
 * A visible, live CR1 pair card with the API's action text. A pair that is not distinguishable
 * carries the API's `not_distinguishable` flag with `flagMessage`. With `rows`, the pair carries
 * them as the campaign's variants table (`extra.variants`), as `creative._pair_candidate` freezes
 * it; without, it carries none.
 */
function pairItem(id: number, action: string, flagMessage: string | null, rows?: unknown[]): Item {
  const distinguishable = flagMessage === null;
  return makeItem({
    id,
    rank: id,
    rule_id: 'CR1',
    action,
    reason: `Pair ${String(id)} compared on leads per impression.`,
    flags: flagMessage === null ? [] : [makeFlag('not_distinguishable', { message: flagMessage })],
    evidence: makeEvidence({
      extra: rows === undefined ? { distinguishable } : { distinguishable, variants: rows },
    }),
  });
}

/**
 * A CR3 item whose `extra.variants` is `rows` and, when given, whose `extra.campaign` is
 * `campaign` (`creative._Campaign.summary`, which names the objective group). Without it the
 * evidence names no objective.
 */
function testItemWith(id: number, rows: unknown[], campaign?: Record<string, unknown>): Item {
  const extra: Record<string, unknown> =
    campaign === undefined ? { variants: rows } : { campaign, variants: rows };
  return makeItem({
    id,
    rule_id: 'CR3',
    action: `Next test ${String(id)}`,
    evidence: makeEvidence({ extra }),
  });
}

/** Renders the section on its own, with no API handler registered. */
async function renderCreative(section: Section) {
  return renderWithProviders(<CreativeSection section={section} run={RUN} overlay={OVERLAY} />);
}

/** The column headers of a table. */
function headers(table: HTMLElement): (string | null)[] {
  return within(table)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent);
}

/** The body rows of a table, each as its cells' text (row header first). */
function bodyRows(table: HTMLElement): (string | null)[][] {
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.querySelectorAll('th, td'), (cell) => cell.textContent));
}

/** Whether `first` precedes `second` in document order. */
function precedes(first: Element, second: Element): boolean {
  return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

/** The element with `id`, the target of a group anchor link; throws when there is none. */
function elementById(id: string): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`no element with id "${id}"`);
  }
  return element;
}

/** The level-4 card headings of `block`, in document order. */
function cardHeadings(block: HTMLElement): HTMLElement[] {
  return within(block).getAllByRole('heading', { level: 4 });
}

beforeEach(() => {
  mockApi({});
});

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('CreativeSection', () => {
  describe('campaign blocks', () => {
    it('renders the block of a campaign: variants table, then the CR3 next test, then the CR1 pairs', async () => {
      await renderCreative(defaultCreative());

      const section = screen.getByRole('region', { name: 'Copy/creative changes' });
      const block = within(section).getByRole('region', { name: PIPELINE_EMEA });
      expect(within(block).getByRole('heading', { level: 3 }).textContent).toBe(PIPELINE_EMEA);

      const table = within(block).getByRole('table', { name: `Variants in ${PIPELINE_EMEA}` });
      expect(headers(table)).toEqual(VARIANT_HEADERS);
      expect(bodyRows(table)).toEqual([
        ['Variant A', '13,250', '0.44%', '1.22%', '39.51%', '64'],
        ['Variant B', '12,400', '0.40%', '1.19%', '33.11%', '49'],
      ]);

      // CR3 carries the CR2 weak stage as its reason and the single variable as its action.
      const cr3 = pageItem(FIXTURE_ITEM_IDS.cr3);
      const card = within(block).getByRole('article', { name: cr3.action });
      expect(within(card).getByRole('heading', { level: 4 }).textContent).toBe(cr3.action);
      expect(cr3.action).toMatch(/^Next test: change only headline; keep everything else identical/u);
      expect(
        within(card).getByText('Click rate is below account baseline: 0.42% [0.35%–0.51%] vs 0.61%'),
      ).toBeTruthy();

      // The historical CR1 pair was computed from replaced data: it follows the CR3 card openly,
      // under its replaced-data label, and no discrepancy hold collapses it.
      const replaced = within(block).getByRole('group', {
        name: 'Computed from replaced data (1)',
      });
      const cr1 = pageItem(FIXTURE_ITEM_IDS.cr1Historical);
      expect(within(replaced).getByRole('article', { name: cr1.action })).toBeTruthy();
      expect(
        within(block).queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
      ).toBeNull();
      expect(precedes(table, card)).toBe(true);
      expect(precedes(card, replaced)).toBe(true);
    });

    it('renders one captioned variants table per campaign, each inside its own group anchor', async () => {
      const rowsA = [
        {
          creative_id: 7101,
          name: 'A1',
          impressions: 4000,
          leads: 12,
          click_rate: '0.004377',
          open_rate: '0.0125',
          completion_rate: '0.24',
        },
        {
          creative_id: 7102,
          name: 'A2',
          impressions: 3800,
          leads: 9,
          click_rate: '0.0041',
          open_rate: '0.011842',
          completion_rate: '0.2',
        },
      ];
      const rowsB = [
        {
          creative_id: 7201,
          name: 'B1',
          impressions: 2500,
          leads: 4,
          click_rate: '0.0068',
          open_rate: '0.0096',
          completion_rate: '0.166667',
        },
        {
          creative_id: 7202,
          name: 'B2',
          impressions: 2100,
          leads: 6,
          click_rate: '0.007143',
          open_rate: '0.012381',
          completion_rate: '0.230769',
        },
      ];
      const campaignA = makeGroup({
        key: 'campaign:1',
        title: 'Campaign A',
        items: [
          testItemWith(61, rowsA),
          pairItem(
            62,
            'A1 vs A2: A1 ahead, not yet distinguishable (adjusted p = 0.41)',
            'Not yet distinguishable (adjusted p = 0.41)',
          ),
        ],
      });
      const campaignB = makeGroup({
        key: 'campaign:2',
        title: 'Campaign B',
        items: [
          testItemWith(63, rowsB),
          pairItem(64, 'B1 vs B2: Distinguishable (adjusted p = 0.008)', null),
        ],
      });
      await renderCreative(creativeWith(campaignA, campaignB));

      // The anchors follow SectionFrame's sanitised `group-{section}-{group key}` scheme.
      expect(groupAnchorId('creative', campaignA.key)).toBe('group-creative-campaign-1');
      expect(groupAnchorId('creative', campaignB.key)).toBe('group-creative-campaign-2');

      // Exactly one variants table per campaign, in API order.
      const allTables = screen.getAllByRole('table', { name: /^Variants in/u });
      expect(allTables).toHaveLength(2);
      const [firstTable, secondTable] = allTables;
      if (firstTable === undefined || secondTable === undefined) {
        throw new Error('expected two variants tables');
      }
      expect(precedes(firstTable, secondTable)).toBe(true);

      const blocks = [
        { anchor: 'group-creative-campaign-1', name: 'Campaign A', group: campaignA, rows: rowsA },
        { anchor: 'group-creative-campaign-2', name: 'Campaign B', group: campaignB, rows: rowsB },
      ];
      for (const { anchor, name, group, rows } of blocks) {
        const block = elementById(anchor);
        // The anchor is the campaign's own group region, headed by the campaign name.
        expect(screen.getByRole('region', { name })).toBe(block);

        const tables = within(block).getAllByRole('table', { name: /^Variants in/u });
        expect(tables).toHaveLength(1);
        const table = within(block).getByRole('table', { name: `Variants in ${name}` });
        expect(tables[0]).toBe(table);
        expect(headers(table)).toEqual(VARIANT_HEADERS);
        // Every rate is formatPercent's output at its column's fixed precision (two places for
        // these rates, which read apart at two), every count formatCount's.
        const fixed = { fixed: true };
        expect(bodyRows(table)).toEqual(
          rows.map((row) => [
            row.name,
            formatCount(row.impressions),
            formatPercent(row.click_rate, VARIANT_RATE_DIGITS, fixed),
            formatPercent(row.open_rate, VARIANT_RATE_DIGITS, fixed),
            formatPercent(row.completion_rate, VARIANT_RATE_DIGITS, fixed),
            formatCount(row.leads),
          ]),
        );

        // The CR3 card's heading precedes the CR1 pair's, in API order, after the table.
        const [cr3Heading, cr1Heading] = cardHeadings(block);
        if (cr3Heading === undefined || cr1Heading === undefined) {
          throw new Error(`expected two card headings in ${name}`);
        }
        expect([cr3Heading.textContent, cr1Heading.textContent]).toEqual(
          group.items.map((item) => item.action),
        );
        expect(precedes(table, cr3Heading)).toBe(true);
        expect(precedes(cr3Heading, cr1Heading)).toBe(true);
      }
      // Pinned once in literal form, so a change in the formatter itself shows up here too.
      expect(bodyRows(firstTable)[0]).toEqual(['A1', '4,000', '0.44%', '1.25%', '24.00%', '12']);
      expect(bodyRows(firstTable)[1]).toEqual(['A2', '3,800', '0.41%', '1.18%', '20.00%', '9']);
    });

    it('shows the historical CR1 pair openly with its API wording, badge and disabled actions', async () => {
      await renderCreative(defaultCreative());

      const cr1 = pageItem(FIXTURE_ITEM_IDS.cr1Historical);
      // Readable without operating any control: replaced data is not a discrepancy hold.
      const card = within(
        screen.getByRole('group', { name: 'Computed from replaced data (1)' }),
      ).getByRole('article', { name: cr1.action });
      expect(within(card).getByRole('heading', { level: 4 }).textContent).toBe(
        'Variant A vs Variant B: Variant A ahead, not yet distinguishable (adjusted p = 0.21); ' +
          'about 41,800 more impressions per variant needed at the observed difference',
      );
      expect(within(card).getByText('Not yet distinguishable (adjusted p = 0.21)')).toBeTruthy();
      expect(within(card).getByText('Computed from replaced data')).toBeTruthy();
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(card).getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
      }
    });

    it('keeps CR1 labels verbatim, badges only the pairs the API flags, and names no winner', async () => {
      const distinguishable = pairItem(
        51,
        'Variant A vs Variant B: Distinguishable (adjusted p = 0.012)',
        null,
      );
      const ahead = pairItem(
        52,
        'Variant A vs Variant C: Variant A ahead, not yet distinguishable (adjusted p = 0.34); ' +
          'about 9,600 more impressions per variant needed at the observed difference',
        'Not yet distinguishable (adjusted p = 0.34)',
      );
      const group = makeGroup({
        key: `campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineEmea)}`,
        title: PIPELINE_EMEA,
        items: [pageItem(FIXTURE_ITEM_IDS.cr3), distinguishable, ahead],
      });
      await renderCreative(creativeWith(group));

      const cards = within(screen.getByRole('region', { name: PIPELINE_EMEA })).getAllByRole(
        'article',
      );
      // API order: the CR3 next test first, then the pairs as ranked.
      expect(cards.map((card) => within(card).getByRole('heading', { level: 4 }).textContent)).toEqual([
        pageItem(FIXTURE_ITEM_IDS.cr3).action,
        distinguishable.action,
        ahead.action,
      ]);
      const [, distinguishableCard, aheadCard] = cards;
      if (distinguishableCard === undefined || aheadCard === undefined) {
        throw new Error('expected three cards');
      }
      expect(within(distinguishableCard).queryByText(/not yet distinguishable/iu)).toBeNull();
      expect(within(aheadCard).getByText('Not yet distinguishable (adjusted p = 0.34)')).toBeTruthy();
      expect(screen.queryByText(/winner/iu)).toBeNull();
    });

    it('states a completion-rate next test in form opens, with its projection to impressions', async () => {
      const completion = makeCompletionRateTestItem();
      const section = defaultCreative();
      section.groups.unshift(
        makeGroup({
          key: `campaign:${String(FIXTURE_CAMPAIGN_IDS.benchmarksEmea)}`,
          title: 'Lead Gen — Benchmarks EMEA',
          items: [completion],
        }),
      );
      await renderCreative(section);

      // Blocks keep the API order.
      const benchmarks = screen.getByRole('region', { name: 'Lead Gen — Benchmarks EMEA' });
      expect(precedes(benchmarks, screen.getByRole('region', { name: PIPELINE_EMEA }))).toBe(true);

      const card = within(benchmarks).getByRole('article', { name: completion.action });
      const headline = within(card).getByRole('heading', { level: 4 }).textContent ?? '';
      expect(headline).toContain('change only number of form questions');
      expect(headline).toContain('794 form opens per variant');
      expect(headline).toContain(
        '≈ 66,059 impressions per variant (projected through the observed open rate)',
      );
      expect(
        within(card).getByText(
          'Completion rate is below account baseline: 24.18% [21.60%–26.96%] vs 34.00%',
        ),
      ).toBeTruthy();
      // The sample is in the stage's own denominator, with the labelled projection.
      expect(
        within(card).getByText(
          'Sample: 976 / 794 form opens · ≈ 66,059 impressions (projected through the observed open rate)',
        ),
      ).toBeTruthy();

      const table = within(benchmarks).getByRole('table', {
        name: 'Variants in Lead Gen — Benchmarks EMEA',
      });
      // Open rates of 0.012000 and 0.012041 would both read "1.20%" at two places, so that
      // column takes three; the other rate columns keep two, trailing zeros included.
      expect(bodyRows(table)).toEqual([
        ['Benchmarks Variant A', '42,000', '0.63%', '1.200%', '25.00%', '126'],
        ['Benchmarks Variant B', '39,200', '0.60%', '1.204%', '23.31%', '110'],
      ]);
    });

    it('keeps the variants table when feedback hides the CR3 card', async () => {
      const hiddenCr3 = {
        ...pageItem(FIXTURE_ITEM_IDS.cr3),
        current: makeItemOverlay({ hidden_by: 'done' }),
      };
      const pair = pairItem(53, 'Variant A vs Variant B: Distinguishable (adjusted p = 0.004)', null);
      await renderCreative(
        creativeWith(makeGroup({ key: 'campaign:501', title: PIPELINE_EMEA, items: [hiddenCr3, pair] })),
      );

      const block = screen.getByRole('region', { name: PIPELINE_EMEA });
      expect(within(block).getByRole('table', { name: `Variants in ${PIPELINE_EMEA}` })).toBeTruthy();
      expect(within(block).queryByRole('article', { name: hiddenCr3.action })).toBeNull();
      expect(within(block).getAllByRole('article')).toHaveLength(1);
    });

    it('renders the variants table of a block holding only CR1 pairs, its CR3 withheld or suppressed', async () => {
      // Every CR1 pair freezes the campaign's variants table (creative._pair_candidate).
      const rows = [
        { creative_id: 9011, name: 'Variant D', impressions: 6000, leads: 30, click_rate: '0.005' },
        { creative_id: 9012, name: 'Variant E', impressions: 5500, leads: 11, click_rate: '0.004' },
        { creative_id: 9013, name: 'Variant F', impressions: 5100, leads: 12, click_rate: '0.006' },
      ];
      const first = pairItem(
        54,
        'Variant D vs Variant E: Distinguishable (adjusted p = 0.03)',
        null,
        rows,
      );
      const second = pairItem(
        57,
        'Variant D vs Variant F: Variant D ahead, not yet distinguishable (adjusted p = 0.09)',
        'Not yet distinguishable (adjusted p = 0.09)',
        rows,
      );
      await renderCreative(
        creativeWith(
          makeGroup({ key: 'campaign:777', title: 'Pairs only', items: [first, second] }),
        ),
      );

      const block = screen.getByRole('region', { name: 'Pairs only' });
      const table = within(block).getByRole('table', { name: 'Variants in Pairs only' });
      expect(headers(table)).toEqual(VARIANT_HEADERS);
      expect(bodyRows(table).map((row) => [row[0], row[1], row[5]])).toEqual([
        ['Variant D', '6,000', '30'],
        ['Variant E', '5,500', '11'],
        ['Variant F', '5,100', '12'],
      ]);
      expect(within(block).getAllByRole('table', { name: /^Variants in/u })).toHaveLength(1);
      // The table leads the block, then the pair cards follow in API order.
      const firstCard = within(block).getByRole('article', { name: first.action });
      const secondCard = within(block).getByRole('article', { name: second.action });
      expect(within(block).getAllByRole('article')).toEqual([firstCard, secondCard]);
      expect(precedes(table, firstCard)).toBe(true);
    });

    it('reads the variants table from the first item that carries it when an earlier item carries none', async () => {
      const bare = pairItem(
        54,
        'Variant D vs Variant E: Distinguishable (adjusted p = 0.03)',
        null,
      );
      const carrying = pairItem(
        57,
        'Variant D vs Variant F: Distinguishable (adjusted p = 0.04)',
        null,
        [{ creative_id: 9021, name: 'Later variant', impressions: 2500, click_rate: '0.006' }],
      );
      await renderCreative(
        creativeWith(
          makeGroup({ key: 'campaign:780', title: 'Later rows', items: [bare, carrying] }),
        ),
      );

      const block = screen.getByRole('region', { name: 'Later rows' });
      const table = within(block).getByRole('table', { name: 'Variants in Later rows' });
      expect(bodyRows(table).map((row) => [row[0], row[1]])).toEqual([['Later variant', '2,500']]);
      expect(within(block).getAllByRole('article')).toHaveLength(2);
    });

    it('leaves the table out of a block none of whose items carries variants, and captions by key when untitled', async () => {
      const bare = makeGroup({
        key: 'campaign:777',
        title: 'No variants',
        items: [
          pairItem(54, 'Variant D vs Variant E: Distinguishable (adjusted p = 0.03)', null),
          pairItem(57, 'Variant D vs Variant F: Distinguishable (adjusted p = 0.04)', null, []),
        ],
      });
      const untitled = makeGroup({
        key: 'campaign:778',
        title: '   ',
        items: [
          testItemWith(55, [
            { creative_id: 9001, name: 'Only variant', impressions: 1500, click_rate: '0.01' },
          ]),
        ],
      });
      await renderCreative(creativeWith(bare, untitled));

      const bareBlock = screen.getByRole('region', { name: 'No variants' });
      expect(within(bareBlock).queryByRole('table', { name: /^Variants in/u })).toBeNull();
      // The cards still render, each with its own evidence table.
      expect(within(bareBlock).getAllByRole('article')).toHaveLength(2);
      expect(within(bareBlock).getAllByRole('table', { name: 'Evidence' })).toHaveLength(2);
      // The caption names the same campaign as the heading: the key, for a blank title.
      const untitledBlock = screen.getByRole('region', { name: 'campaign:778' });
      expect(
        within(untitledBlock).getByRole('table', { name: 'Variants in campaign:778' }),
      ).toBeTruthy();
    });

    it('renders a block titled as the section with no <h3>, its cards at level 3 and its table captioned', async () => {
      const repeating = makeGroup({
        key: 'campaign:779',
        title: 'Copy/creative changes',
        items: [
          testItemWith(58, [
            { creative_id: 9002, name: 'Same-name variant', impressions: 1800, click_rate: '0.02' },
          ]),
        ],
      });
      await renderCreative(creativeWith(repeating));

      const section = screen.getByRole('region', { name: 'Copy/creative changes' });
      expect(screen.getAllByRole('heading', { name: 'Copy/creative changes' })).toHaveLength(1);
      expect(screen.getAllByRole('region', { name: 'Copy/creative changes' })).toEqual([section]);
      const block = elementById(groupAnchorId('creative', 'campaign:779'));
      expect(block.tagName).toBe('DIV');
      expect(block.parentElement?.closest('section')).toBe(section);
      // No heading level is skipped: the card sits directly under the section's <h2>.
      expect(within(block).getByRole('heading', { level: 3, name: 'Next test 58' })).toBeTruthy();
      expect(within(block).queryAllByRole('heading', { level: 4 })).toHaveLength(0);
      expect(
        within(block).getByRole('table', { name: 'Variants in Copy/creative changes' }),
      ).toBeTruthy();
    });


    it('shows the empty state, and no block, when no rule fired', async () => {
      await renderCreative(makeSection('creative'));

      const section = screen.getByRole('region', { name: 'Copy/creative changes' });
      expect(within(section).queryByRole('table')).toBeNull();
      const empty = section.querySelector('.ds-emptystate');
      expect(empty?.querySelector('.ds-emptystate__title')?.textContent).toBe(
        'No rule fired for this window',
      );
      expect(empty?.querySelector('.ds-emptystate__reason')).toBeNull();
    });
  });

  describe('variants table values', () => {
    it('renders "n/a" for stages a non-lead-gen campaign does not use and "unavailable" for absent counts', async () => {
      await renderCreative(
        creativeWith(
          makeGroup({
            key: 'campaign:779',
            title: 'Website visits',
            items: [
              testItemWith(
                56,
                [
                  {
                    creative_id: 9101,
                    urn: 'urn:li:sponsoredCreative:9101',
                    name: 'Visit A',
                    impressions: 20000,
                    click_rate: '0.0105',
                    open_rate: null,
                    completion_rate: null,
                  },
                  { urn: 'urn:li:sponsoredCreative:9102', click_rate: 0.009 },
                  { name: 'no id at all' },
                  'not a row',
                ],
                { id: 779, name: 'Website visits', objective_group: 'linkedin_other' },
              ),
            ],
          }),
        ),
      );

      const table = screen.getByRole('table', { name: 'Variants in Website visits' });
      expect(bodyRows(table)).toEqual([
        ['Visit A', '20,000', '1.05%', 'n/a', 'n/a', 'unavailable'],
        // A variant without a name is shown by its URN; a number rate is read as its decimal.
        ['urn:li:sponsoredCreative:9102', 'unavailable', '0.90%', 'n/a', 'n/a', 'unavailable'],
      ]);
    });

    it('renders a missing rate of a lead-gen stage as "unavailable", or "not estimable" over zero form opens, never "n/a"', async () => {
      // Rows as creative._variant_row writes them for a lead-gen campaign: opens unreported
      // leave the open and completion rates None; zero form opens leave the completion rate
      // None over a known-zero denominator; more clicks than impressions leave the click rate
      // None over a known, non-zero denominator.
      await renderCreative(
        creativeWith(
          makeGroup({
            key: 'campaign:781',
            title: 'Lead Gen — Guide',
            items: [
              testItemWith(
                58,
                [
                  {
                    creative_id: 9301,
                    name: 'Opens unreported',
                    impressions: 5000,
                    clicks: 20,
                    form_opens: null,
                    leads: 10,
                    click_rate: '0.004',
                    open_rate: null,
                    completion_rate: null,
                  },
                  {
                    creative_id: 9302,
                    name: 'Form never opened',
                    impressions: 4200,
                    clicks: 21,
                    form_opens: 0,
                    leads: 0,
                    click_rate: '0.005',
                    open_rate: '0.000000',
                    completion_rate: null,
                  },
                  {
                    creative_id: 9303,
                    name: 'Inconsistent clicks',
                    impressions: 1200,
                    clicks: 1300,
                    form_opens: 30,
                    leads: 6,
                    click_rate: null,
                    open_rate: '0.025',
                    completion_rate: '0.2',
                  },
                ],
                { id: 781, name: 'Lead Gen — Guide', objective_group: 'linkedin_lead_gen' },
              ),
            ],
          }),
        ),
      );

      const table = screen.getByRole('table', { name: 'Variants in Lead Gen — Guide' });
      expect(bodyRows(table)).toEqual([
        ['Opens unreported', '5,000', '0.40%', 'unavailable', 'unavailable', '10'],
        ['Form never opened', '4,200', '0.50%', '0.00%', 'not estimable', '0'],
        ['Inconsistent clicks', '1,200', 'unavailable', '2.50%', '20.00%', '6'],
      ]);
      expect(within(table).queryByText('n/a')).toBeNull();
    });

    it('never renders "n/a" when the evidence names no objective group', async () => {
      await renderCreative(
        creativeWith(
          makeGroup({
            key: 'campaign:782',
            title: 'Objective unknown',
            items: [
              testItemWith(59, [
                {
                  creative_id: 9401,
                  name: 'Unknown A',
                  impressions: 3000,
                  click_rate: '0.01',
                  open_rate: null,
                  completion_rate: null,
                },
                {
                  creative_id: 9402,
                  name: 'Unknown B',
                  impressions: 2800,
                  form_opens: 0,
                  leads: 0,
                  click_rate: '0.0125',
                  open_rate: null,
                  completion_rate: null,
                },
              ]),
            ],
          }),
        ),
      );

      const table = screen.getByRole('table', { name: 'Variants in Objective unknown' });
      expect(bodyRows(table)).toEqual([
        ['Unknown A', '3,000', '1.00%', 'unavailable', 'unavailable', 'unavailable'],
        ['Unknown B', '2,800', '1.25%', 'unavailable', 'not estimable', '0'],
      ]);
      expect(within(table).queryByText('n/a')).toBeNull();
    });

    it('keeps every variant row when creative ids repeat', async () => {
      await renderCreative(
        creativeWith(
          makeGroup({
            key: 'campaign:780',
            title: 'Repeats',
            items: [
              testItemWith(57, [
                { creative_id: 9201, name: 'First', impressions: 1200 },
                { creative_id: 9201, name: 'Second', impressions: 1300 },
                { creative_id: 9201, name: 'Third', impressions: 1400 },
              ]),
            ],
          }),
        ),
      );

      const rows = bodyRows(screen.getByRole('table', { name: 'Variants in Repeats' }));
      expect(rows.map((row) => row[0])).toEqual(['First', 'Second', 'Third']);
    });

    it('formats a stage rate at fixed digits, two by default, "n/a" only where the stage does not apply, and a missing rate as "not estimable" or "unavailable"', () => {
      expect(VARIANT_RATE_DIGITS).toBe(2);
      expect(NOT_ESTIMABLE).toBe('not estimable');
      // A rate, whether its stage is known to apply or its applicability is unknown, at exactly
      // its column's digits, trailing zeros included.
      expect(formatVariantRate('0.395062', true, 162)).toBe('39.51%');
      expect(formatVariantRate('0.004377', null, 13250)).toBe('0.44%');
      expect(formatVariantRate('0.000000', true, 4200)).toBe('0.00%');
      expect(formatVariantRate('0.01', true, 3000)).toBe('1.00%');
      expect(formatVariantRate('0.008', true, 3000)).toBe('0.80%');
      expect(formatVariantRate('0.007994', true, 13250, 3)).toBe('0.799%');
      expect(formatVariantRate('0.000004', true, 250000, 3)).toBe('< 0.001%');
      expect(formatVariantRate(null, false, null, 3)).toBe('n/a');
      // A stage the campaign does not use is "n/a", checked before anything else.
      expect(formatVariantRate(null, false, null)).toBe('n/a');
      expect(formatVariantRate(null, false, 0)).toBe('n/a');
      expect(formatVariantRate('0.01', false, 1000)).toBe('n/a');
      // A missing rate over a known-zero denominator, applicable or of unknown applicability.
      expect(formatVariantRate(null, true, 0)).toBe('not estimable');
      expect(formatVariantRate(null, null, 0)).toBe('not estimable');
      // Any other missing rate: an unreported denominator, or inconsistent counts.
      expect(formatVariantRate(null, true, null)).toBe('unavailable');
      expect(formatVariantRate(null, true, 1200)).toBe('unavailable');
      expect(formatVariantRate(null, null, null)).toBe('unavailable');
    });

    it('renders nothing for a campaign without variants', () => {
      const { container } = render(<VariantsTable campaignName={PIPELINE_EMEA} rows={[]} />);

      expect(container.childElementCount).toBe(0);
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Variants table precision: one fixed precision per rate column
// ---------------------------------------------------------------------------------------------

describe('variants table precision', () => {
  /** A lead-gen variant row in the backend's shape, with the given rates. */
  function rateRow(
    id: number,
    clickRate: string | null,
    openRate: string | null,
    completionRate: string | null,
  ): Record<string, unknown> {
    return {
      creative_id: id,
      name: `Creative ${String(id)}`,
      impressions: 5000,
      form_opens: 40,
      leads: 4,
      click_rate: clickRate,
      open_rate: openRate,
      completion_rate: completionRate,
    };
  }

  /** Renders one lead-gen campaign block holding a CR3 item with `rows`; returns its table. */
  async function tableOf(rows: Record<string, unknown>[]): Promise<HTMLElement> {
    await renderCreative(
      creativeWith(
        makeGroup({
          key: 'campaign:790',
          title: 'Precision',
          items: [
            testItemWith(90, rows, {
              id: 790,
              name: 'Precision',
              objective_group: 'linkedin_lead_gen',
            }),
          ],
        }),
      ),
    );
    return screen.getByRole('table', { name: 'Variants in Precision' });
  }

  /** The text of column `index` (row header 0) over every shown row. */
  function column(table: HTMLElement, index: number): (string | null)[] {
    return bodyRows(table).map((row) => row[index] ?? null);
  }

  it('shows a column of mixed magnitudes at one precision, never mixing "0.8%" and "0.58%"', async () => {
    const table = await tableOf([
      rateRow(1, '0.008', '0.0058', '0.1239'),
      rateRow(2, '0.0058', '0.01', '0.5'),
      rateRow(3, '0.1239', '0.008', '0.25'),
      rateRow(4, '0.01', '0.1239', '1'),
    ]);

    expect(column(table, 2)).toEqual(['0.80%', '0.58%', '12.39%', '1.00%']);
    expect(column(table, 3)).toEqual(['0.58%', '1.00%', '0.80%', '12.39%']);
    expect(column(table, 4)).toEqual(['12.39%', '50.00%', '25.00%', '100.00%']);
  });

  it('adds places to a column until different rates read apart, and only to that column', async () => {
    const table = await tableOf([
      rateRow(1, '0.007994', '0.004377', '0.25'),
      rateRow(2, '0.008014', '0.004381', '0.2'),
    ]);

    // 0.7994% and 0.8014% both read "0.80%" at two places; 0.4377% and 0.4381% agree to three.
    expect(column(table, 2)).toEqual(['0.799%', '0.801%']);
    expect(column(table, 3)).toEqual(['0.4377%', '0.4381%']);
    expect(column(table, 4)).toEqual(['25.00%', '20.00%']);
  });

  it('takes a column’s precision from every variant, not only the rows shown', async () => {
    // 51 variants: the first batch shows 50, all at 0.004377; the 51st, at 0.004381, is hidden
    // until revealed, yet both read apart only at four places.
    const rows = Array.from({ length: 51 }, (_, index) =>
      rateRow(index + 1, index === 50 ? '0.004381' : '0.004377', '0.01', '0.1'),
    );
    const table = await tableOf(rows);

    expect(new Set(column(table, 2))).toEqual(new Set(['0.4377%']));
    fireEvent.click(screen.getByRole('button', { name: 'Show 1 more variant' }));
    const clickRates = column(table, 2);
    expect(clickRates).toHaveLength(51);
    expect(clickRates.at(-1)).toBe('0.4381%');
    expect(new Set(column(table, 3))).toEqual(new Set(['1.00%']));
  });
});

// ---------------------------------------------------------------------------------------------
// Bounded batches: variants, pair cards and campaign blocks
// ---------------------------------------------------------------------------------------------

describe('long copy blocks', () => {
  /** One `extra.variants` entry for "Variant {n}", in the backend's shape. */
  function variantEntry(n: number): Record<string, unknown> {
    return {
      creative_id: 40000 + n,
      urn: `urn:li:sponsoredCreative:${String(40000 + n)}`,
      name: `Variant ${String(n)}`,
      impressions: 1000 + n,
      leads: n,
      click_rate: '0.004377',
      open_rate: '0.395062',
      completion_rate: '0.5',
    };
  }

  /** The batch statuses inside `scope` ("Showing …"), told apart from the cards' own regions. */
  function batchStatuses(scope: HTMLElement): string[] {
    return within(scope)
      .queryAllByRole('status')
      .map((status) => status.textContent)
      .filter((text) => text.startsWith('Showing '));
  }

  /** The row headers of a table. */
  function rowHeaders(table: HTMLElement): string[] {
    return within(table)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent);
  }

  /** "Variant {n}" for n = 1…count. */
  function variantNames(count: number): string[] {
    return Array.from({ length: count }, (_, index) => `Variant ${String(index + 1)}`);
  }

  it('bounds a campaign’s variants table and its pair cards, keeping their order', async () => {
    const nextTest = testItemWith(
      8000,
      Array.from({ length: 64 }, (_, index) => variantEntry(index + 1)),
    );
    // 64 variants make 2,016 pairs; 45 cards of full evidence cross every batch boundary (the
    // bound itself is exercised past 200 entries in batches.test.tsx and SectionFrame.test.tsx).
    const pairs = Array.from({ length: 45 }, (_, index) =>
      pairItem(
        8001 + index,
        `Variant ${String(index + 1)} vs Variant ${String(index + 2)}`,
        'Not yet distinguishable at the configured α and power',
      ),
    );
    await renderCreative(
      creativeWith(
        makeGroup({ key: 'campaign:1', title: 'Many variants', items: [nextTest, ...pairs] }),
      ),
    );
    const block = screen.getByRole('region', { name: 'Many variants' });
    const variantsTable = within(block).getByRole('table', { name: 'Variants in Many variants' });

    expect(rowHeaders(variantsTable)).toEqual(variantNames(50));
    expect(batchStatuses(block)).toEqual(['Showing 50 of 64 variants', 'Showing 20 of 46 actions']);
    fireEvent.click(within(block).getByRole('button', { name: 'Show 14 more variants' }));
    expect(rowHeaders(variantsTable)).toEqual(variantNames(64));
    expect(document.activeElement?.textContent).toBe('Variant 51');

    const cardIds = () =>
      within(block)
        .getAllByRole('article')
        .map((article) => article.id);
    const itemIds = (count: number) =>
      Array.from({ length: count }, (_, index) => `item-${String(8000 + index)}`);
    expect(cardIds()).toEqual(itemIds(20));
    fireEvent.click(within(block).getByRole('button', { name: 'Show 20 more actions' }));
    expect(cardIds()).toEqual(itemIds(40));
    fireEvent.click(within(block).getByRole('button', { name: 'Show 6 more actions' }));
    expect(cardIds()).toEqual(itemIds(46));
    expect(document.activeElement?.id).toBe('item-8040');
    // Every pair card keeps its own "not distinguishable" flag.
    expect(
      within(screen.getByRole('article', { name: 'Variant 45 vs Variant 46' })).getByText(
        /Not yet distinguishable/u,
      ),
    ).toBeTruthy();
    expect(batchStatuses(block)).toEqual(['Showing all 64 variants', 'Showing all 46 actions']);
  }, LARGE_RENDER_TEST_TIMEOUT_MS);

  it('shows ten campaign blocks at a time, in API order', async () => {
    const groups = Array.from({ length: 12 }, (_, index) =>
      makeGroup({
        key: `campaign:${String(index + 1)}`,
        title: `Campaign ${String(index + 1)}`,
        items: [testItemWith(8500 + index, [variantEntry(1), variantEntry(2)])],
      }),
    );
    await renderCreative(creativeWith(...groups));
    const section = screen.getByRole('region', { name: 'Copy/creative changes' });
    // The block regions, without the table regions inside them.
    const blocks = () =>
      within(section)
        .getAllByRole('region')
        .map((region) => region.id)
        .filter((id) => id.startsWith('group-creative-'));

    expect(blocks()).toEqual(
      Array.from({ length: 10 }, (_, index) =>
        groupAnchorId('creative', `campaign:${String(index + 1)}`),
      ),
    );
    fireEvent.click(within(section).getByRole('button', { name: 'Show 2 more groups' }));
    expect(blocks()).toHaveLength(12);
    expect(document.activeElement?.id).toBe(groupAnchorId('creative', 'campaign:11'));
  });
});

// ---------------------------------------------------------------------------------------------
// A section rendered again
// ---------------------------------------------------------------------------------------------

describe('CreativeSection rendered again', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /**
   * Renders the section from state, then returns `show`, which hands it a new section object as a
   * refetched page does.
   */
  async function renderReplaceable(initial: Section): Promise<(next: Section) => void> {
    const control: { show?: (next: Section) => void } = {};
    function Host(): ReactElement {
      const [section, setSection] = useState(initial);
      control.show = setSection;
      return <CreativeSection section={section} run={RUN} overlay={OVERLAY} />;
    }
    await renderWithProviders(<Host />);
    return (next) => {
      act(() => {
        control.show?.(next);
      });
    };
  }

  /** `section` with the item `id` replaced by `replace(item)`, every other item kept as is. */
  function withItem(section: Section, id: number, replace: (item: Item) => Item): Section {
    return {
      ...section,
      groups: section.groups.map((group) => ({
        ...group,
        items: group.items.map((item) => (item.id === id ? replace(item) : item)),
      })),
    };
  }

  /** `item` with the leads of its variant `name` set to `leads`. */
  function withVariantLeads(item: Item, name: string, leads: number): Item {
    const extra = item.evidence.extra ?? {};
    const rows: unknown[] = Array.isArray(extra.variants) ? extra.variants : [];
    return {
      ...item,
      evidence: {
        ...item.evidence,
        extra: {
          ...extra,
          variants: rows.map((row) =>
            evidenceExtraModule.isRecord(row) && row.name === name ? { ...row, leads } : row,
          ),
        },
      },
    };
  }

  it("reads a block's variants once per first item, and again only when that item changes", async () => {
    const section = defaultCreative();
    const reads = vi.spyOn(evidenceExtraModule, 'variants');
    const show = await renderReplaceable(section);
    const caption = `Variants in ${PIPELINE_EMEA}`;
    expect(reads).toHaveBeenCalledTimes(1);
    expect(bodyRows(screen.getByRole('table', { name: caption }))[0]?.[5]).toBe('64');

    // A new section object holding the same items: the rows are reused.
    show({ ...section, groups: section.groups.map((group) => ({ ...group })) });
    expect(reads).toHaveBeenCalledTimes(1);

    // The CR3 item's variants change: the table reads and shows them.
    show(
      withItem(section, FIXTURE_ITEM_IDS.cr3, (item) => withVariantLeads(item, 'Variant A', 70)),
    );
    expect(reads).toHaveBeenCalledTimes(2);
    expect(bodyRows(screen.getByRole('table', { name: caption }))).toEqual([
      ['Variant A', '13,250', '0.44%', '1.22%', '39.51%', '70'],
      ['Variant B', '12,400', '0.40%', '1.19%', '33.11%', '49'],
    ]);
  });

  it('keeps its table for the same rows and renders new rows', () => {
    const rows = evidenceExtraModule.variants(pageItem(FIXTURE_ITEM_IDS.cr3));
    const changed = rows.map((row) => (row.name === 'Variant B' ? { ...row, leads: 51 } : row));
    const counts = vi.spyOn(formatModule, 'formatCount');
    const { rerender } = render(<VariantsTable campaignName={PIPELINE_EMEA} rows={rows} />);
    const formatted = counts.mock.calls.length;
    expect(formatted).toBeGreaterThan(0);

    rerender(<VariantsTable campaignName={PIPELINE_EMEA} rows={rows} />);
    expect(counts.mock.calls).toHaveLength(formatted);

    rerender(<VariantsTable campaignName={PIPELINE_EMEA} rows={changed} />);
    expect(counts.mock.calls.length).toBeGreaterThan(formatted);
    const table = screen.getByRole('table', { name: `Variants in ${PIPELINE_EMEA}` });
    expect(bodyRows(table).map((row) => row[5])).toEqual(['64', '51']);
  });
});

describe('CreativeSection: the confirmation of a card hidden on the page', () => {
  /** Whether `first` precedes `second` in document order. */
  function precedes(first: Element, second: Element): boolean {
    return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
  }

  it.each(['dismiss', 'snooze', 'done'] as const)(
    'shows a %s where the CR3 card stood, in its campaign block, until a rerun drops it',
    async (kind) => {
      const flow = feedbackFlow();
      const api = mockApi(flow.handlers);
      const { queryClient } = await renderWithProviders(
        <LiveSection sectionKey="creative" component={CreativeSection} />,
        { path: '/actions' },
      );
      const cr3 = findPageItem(makeRecommendationsPage(), FIXTURE_ITEM_IDS.cr3);
      const card = await screen.findByRole('article', { name: cr3.action });
      const label = { dismiss: 'Dismiss', snooze: 'Snooze for 7 days', done: 'Mark done' }[kind];
      const button = within(card).getByRole('button', { name: label });
      button.focus();
      fireEvent.click(button);

      await waitFor(() => {
        expect(screen.queryByRole('article', { name: cr3.action })).toBeNull();
      });
      const block = document.getElementById(
        groupAnchorId('creative', `campaign:${String(FIXTURE_CAMPAIGN_IDS.pipelineEmea)}`),
      );
      if (block === null) {
        throw new Error('the campaign block left the page with its card');
      }
      const text = feedbackConfirmationText(kind, cr3);
      const confirmation = within(block).getByText(text);
      // Plain content: neither a live region (the provider announces it) nor an alert, nor a
      // Tab stop.
      expect(confirmation.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
      expect(confirmation.closest('button, a, [tabindex]')).toBeNull();
      expect(within(block).queryAllByRole('alert')).toEqual([]);
      // Where the card stood: after the variants table, before the replaced-data cards.
      const variants = within(block).getByRole('table', { name: /^Variants in / });
      const replaced = within(block).getByRole('group', {
        name: 'Computed from replaced data (1)',
      });
      expect(precedes(variants, confirmation)).toBe(true);
      expect(precedes(confirmation, replaced)).toBe(true);
      // The item still counts as hidden.
      const section = screen.getByRole('region', { name: 'Copy/creative changes' });
      expect(within(section).getByRole('list', { name: 'Not shown' }).textContent).toContain(
        '1 hidden since this run',
      );
      // Focus did not fall to the page: the next card offers no enabled action (computed from
      // replaced data), so it is on the block's heading.
      await waitFor(() => {
        expect(document.activeElement).toBe(
          within(block).getByRole('heading', { name: 'Lead Gen — Pipeline Report EMEA' }),
        );
      });
      expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);

      // A rerun that no longer lists the item removes its confirmation.
      flow.drop(cr3.id);
      await act(async () => {
        await queryClient.refetchQueries();
      });
      await waitFor(() => {
        expect(within(section).queryByText(text)).toBeNull();
      });
      expect(within(section).queryByRole('list', { name: 'Not shown' })).toBeNull();
    },
  );
});
