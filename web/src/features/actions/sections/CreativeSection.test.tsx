/**
 * Component tests for `CreativeSection`, the "Copy/creative changes" section of `/actions`
 * (AAP 0.5.5 Copy; AAP 0.5.4.6 rules CR1–CR3; AAP 0.5.4.2 P7; FR-7).
 *
 * They hold the section to its TSDoc contract and to the AAP's field set:
 * - **One block per campaign**, in API order, each a region headed by the campaign name and
 *   anchored at `groupAnchorId('creative', key)` (`campaign:1` → `group-creative-campaign-1`),
 *   holding its own variants table ("Variants in {campaign}": Creative, Impressions, Click rate,
 *   Open rate, Completion rate, Leads; rates as `formatPercent` renders them at two digits, "n/a"
 *   for a stage the campaign does not use, "unavailable" for an absent count), then the cards in
 *   API order: the CR3 next test with the CR2 weak stage as its reason, then the CR1 pairs, held
 *   ones behind the held disclosure.
 * - **Distinguishability (CR1, P7).** Pair labels are the API's own words, "Distinguishable
 *   (adjusted p = …)" or "{A} ahead, not yet distinguishable …", and the "Not yet
 *   distinguishable" badge is the API's flag; the section never declares a winner.
 * - **Completion-rate units (CR3).** A completion-rate next test is stated in form opens, the
 *   stage's own denominator, with its labelled projection to impressions.
 * - The variants table is evidence: it stays when feedback hides the CR3 card, and it is left out
 *   for a block whose first item carries no variants.
 *
 * The section fetches nothing, so every test stubs the API with no handler: any request fails
 * the test.
 */
import { mockApi } from '../../../test/mockApi';

import { fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

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
import { formatCount, formatPercent } from '../../../lib/format';
import { renderWithProviders } from '../../../test/render';
import {
  CreativeSection,
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
 * carries the API's `not_distinguishable` flag with `flagMessage`.
 */
function pairItem(id: number, action: string, flagMessage: string | null): Item {
  return makeItem({
    id,
    rank: id,
    rule_id: 'CR1',
    action,
    reason: `Pair ${String(id)} compared on leads per impression.`,
    flags: flagMessage === null ? [] : [makeFlag('not_distinguishable', { message: flagMessage })],
    evidence: makeEvidence({ extra: { distinguishable: flagMessage === null } }),
  });
}

/** A CR3 item whose `extra.variants` is `rows`. */
function testItemWith(id: number, rows: unknown[]): Item {
  return makeItem({
    id,
    rule_id: 'CR3',
    action: `Next test ${String(id)}`,
    evidence: makeEvidence({ extra: { variants: rows } }),
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
        ['Variant B', '12,400', '0.4%', '1.19%', '33.11%', '49'],
      ]);

      // CR3 carries the CR2 weak stage as its reason and the single variable as its action.
      const cr3 = pageItem(FIXTURE_ITEM_IDS.cr3);
      const card = within(block).getByRole('article', { name: cr3.action });
      expect(within(card).getByRole('heading', { level: 4 }).textContent).toBe(cr3.action);
      expect(cr3.action).toMatch(/^Next test: change only headline; keep everything else identical/u);
      expect(
        within(card).getByText('Click rate is below account baseline: 0.42% [0.35%–0.51%] vs 0.61%'),
      ).toBeTruthy();

      // The historical CR1 pair is held by its replaced data, so it sits behind the disclosure.
      const toggle = within(block).getByRole('button', {
        name: 'Held until discrepancies are resolved (1)',
      });
      expect(precedes(table, card)).toBe(true);
      expect(precedes(card, toggle)).toBe(true);
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
        // Every rate is formatPercent's output at the table's precision, every count formatCount's.
        expect(bodyRows(table)).toEqual(
          rows.map((row) => [
            row.name,
            formatCount(row.impressions),
            formatPercent(row.click_rate, VARIANT_RATE_DIGITS),
            formatPercent(row.open_rate, VARIANT_RATE_DIGITS),
            formatPercent(row.completion_rate, VARIANT_RATE_DIGITS),
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
      expect(bodyRows(firstTable)[0]).toEqual(['A1', '4,000', '0.44%', '1.25%', '24%', '12']);
    });

    it('shows the held historical CR1 pair with its API wording, badge and disabled actions', async () => {
      await renderCreative(defaultCreative());

      const cr1 = pageItem(FIXTURE_ITEM_IDS.cr1Historical);
      expect(screen.queryByRole('article', { name: cr1.action })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }));

      const card = screen.getByRole('article', { name: cr1.action });
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
      expect(bodyRows(table)).toEqual([
        ['Benchmarks Variant A', '42,000', '0.63%', '1.2%', '25%', '126'],
        ['Benchmarks Variant B', '39,200', '0.6%', '1.2%', '23.31%', '110'],
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

    it('leaves the table out of a block whose first item has no variants, and captions by key when untitled', async () => {
      const pairsOnly = makeGroup({
        key: 'campaign:777',
        title: 'Pairs only',
        items: [pairItem(54, 'Variant D vs Variant E: Distinguishable (adjusted p = 0.03)', null)],
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
      await renderCreative(creativeWith(pairsOnly, untitled));

      const pairsBlock = screen.getByRole('region', { name: 'Pairs only' });
      expect(within(pairsBlock).queryByRole('table', { name: /^Variants in/u })).toBeNull();
      // The card's own evidence table is still there.
      expect(within(pairsBlock).getByRole('table', { name: 'Evidence' })).toBeTruthy();
      expect(within(pairsBlock).getAllByRole('article')).toHaveLength(1);
      // The caption names the same campaign as the heading: the key, for a blank title.
      const untitledBlock = screen.getByRole('region', { name: 'campaign:778' });
      expect(
        within(untitledBlock).getByRole('table', { name: 'Variants in campaign:778' }),
      ).toBeTruthy();
    });

    it('shows the empty state, and no block, when no rule fired', async () => {
      await renderCreative(makeSection('creative'));

      const section = screen.getByRole('region', { name: 'Copy/creative changes' });
      expect(within(section).queryByRole('table')).toBeNull();
      expect(within(section).getByText('No actions in this section')).toBeTruthy();
      expect(within(section).getByText('No rule fired for this window')).toBeTruthy();
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
              testItemWith(56, [
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
              ]),
            ],
          }),
        ),
      );

      const table = screen.getByRole('table', { name: 'Variants in Website visits' });
      expect(bodyRows(table)).toEqual([
        ['Visit A', '20,000', '1.05%', 'n/a', 'n/a', 'unavailable'],
        // A variant without a name is shown by its URN; a number rate is read as its decimal.
        ['urn:li:sponsoredCreative:9102', 'unavailable', '0.9%', 'n/a', 'n/a', 'unavailable'],
      ]);
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

    it('formats a stage rate at two digits, and a stage that does not apply as "n/a"', () => {
      expect(VARIANT_RATE_DIGITS).toBe(2);
      expect(formatVariantRate('0.395062')).toBe('39.51%');
      expect(formatVariantRate('0.004377')).toBe('0.44%');
      expect(formatVariantRate(null)).toBe('n/a');
    });

    it('renders nothing for a campaign without variants', () => {
      const { container } = render(<VariantsTable campaignName={PIPELINE_EMEA} rows={[]} />);

      expect(container.childElementCount).toBe(0);
    });
  });
});
