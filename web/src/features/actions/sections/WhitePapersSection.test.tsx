/**
 * Component tests for `WhitePapersSection`, the White papers section of `/actions` (AAP 0.5.5
 * White papers; AAP 0.5.4.7 attribution chain; FR-8).
 *
 * They hold the section to its TSDoc contract and to the AAP's rendering rules:
 * - **One table per currency cohort**, captioned "White papers ({currency})", rows in the API's
 *   rank order, with the columns exactly in this order: Asset, Spend, LinkedIn leads, MQL headed
 *   with the bound stage label ("MQL (First meeting booked)"), Qualified Lead, Meetings booked,
 *   Other meetings (ordering unverified), Deals after touch, Cost per Qualified Lead, Cost per
 *   meeting, Action. MQL and Qualified Lead are never blended into one column.
 * - **The MQL label is frozen**: each table takes the label its rows recorded
 *   (`extra.mql_label`, else the recorded MQL metric label), so a later binding never relabels
 *   them; plain "MQL" when its rows recorded different ones. Only a table whose rows recorded
 *   none takes `GET /config`'s label, plain "MQL" while that loads or fails.
 * - **Rendering rules.** Floors render "≥ N" for counts and "≤ X" for costs, exactly as
 *   `formatMetric` renders the evidence; an unavailable count (MQL or Qualified Lead) renders
 *   "unavailable", never 0; a cost with a zero denominator (QL 0, no meetings) renders "—" with
 *   its reason. Rows keep the API's order, even when a costlier row comes first.
 * - **Rows expand** to their per-creative rows, "unknown creative" included, then the reason, the
 *   baselines and sample, the flags (the floor flag among them), the window chip and the footer
 *   actions, whose "Mark done" posts feedback once; held rows sit behind the held disclosure,
 *   linked to the blocking discrepancy; historical rows carry the replaced-data badge with every
 *   action disabled.
 * - **Cohort notes**: the backend's spend total in the cohort's own currency, "counts overlap
 *   across assets", and what the LinkedIn leads and meetings booked columns count. No other
 *   total exists, and no amount is summed across currencies.
 *
 * The section reads `GET /config` (for the fallback MQL label) and nothing else; any other
 * request fails the test. Only the "Mark done" case also answers
 * `POST /recommendations/:id/feedback`.
 */
import { apiError, json, mockApi } from '../../../test/mockApi';
import type { MockHandler, MockResponse } from '../../../test/mockApi';

import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ConfigView,
  Evidence,
  Group,
  Item,
  Metric,
  ResolvedBindingView,
  Section,
} from '../../../api/types';
import {
  FIXTURE_ITEM_IDS,
  findPageItem,
  makeConfig,
  makeEvidence,
  makeFeedback,
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import { renderWithProviders } from '../../../test/render';
import { formatMetric } from '../../../lib/format';
import * as evidenceExtraModule from '../evidenceExtra';
import { WHITE_PAPER_METRIC_KEYS, type WhitePaperMetricKey } from '../evidenceExtra';
import { WhitePapersSection, mqlHeader } from './WhitePapersSection';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The fixture run and overlay every section render receives. */
const RUN = makeRun();
const OVERLAY = makeOverlay();

/** The bound MQL header of the shipped configuration. */
const MQL_BOUND = 'MQL (First meeting booked)';

/** The metric column headers after Asset and before Action, with the MQL header given. */
function metricHeaders(mql: string): string[] {
  return [
    'Spend',
    'LinkedIn leads',
    mql,
    'Qualified Lead',
    'Meetings booked',
    'Other meetings (ordering unverified)',
    'Deals after touch',
    'Cost per Qualified Lead',
    'Cost per meeting',
  ];
}

/** The asset table's headers in AAP 0.5.5 order, after the expand column ("Details"). */
function assetHeaders(mql: string): string[] {
  return ['Details', 'Asset', ...metricHeaders(mql), 'Action'];
}

/** The note on what the LinkedIn leads column counts. */
const LINKEDIN_LEADS_NOTE =
  'LinkedIn leads: platform counts from LinkedIn, never added to HubSpot counts';

/** The note on what the Meetings booked column counts. */
const MEETINGS_BOOKED_NOTE =
  "Meetings booked: booked (record created). HubSpot records no booking instant, so the meeting record's creation stands in for it";

/** The default page's White papers section: two EUR rows and one USD row. */
function defaultWhitePapers(): Section {
  const section = makeRecommendationsPage().sections.find((each) => each.key === 'white_papers');
  if (section === undefined) {
    throw new Error('the default page has no white papers section');
  }
  return section;
}

/** One default-page item, a fresh copy. */
function pageItem(id: number): Item {
  return findPageItem(makeRecommendationsPage(), id);
}

/** Renders the section with `GET /config` answered by `config` (the shipped one by default). */
async function renderWhitePapers(section: Section, config: MockHandler = json(200, makeConfig())) {
  const api = mockApi({ 'GET /config': config });
  const result = await renderWithProviders(
    <WhitePapersSection section={section} run={RUN} overlay={OVERLAY} />,
  );
  return { ...result, api };
}

/** Renders the default section and waits for the bound MQL header. */
async function renderDefault() {
  const result = await renderWhitePapers(defaultWhitePapers());
  await screen.findAllByRole('columnheader', { name: MQL_BOUND });
  return result;
}

/** The column headers of a table. */
function headers(table: HTMLElement): (string | null)[] {
  return within(table)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent);
}

/** The visible body rows of a table, each as its cells' text (expand toggle first, if any). */
function bodyRows(table: HTMLElement): (string | null)[][] {
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .filter((row) => row.closest('table') === table)
    .map((row) =>
      Array.from(row.children, (cell) => cell.textContent).filter((text) => text !== null),
    );
}

/** The table named `caption`. */
function table(caption: string): HTMLElement {
  return screen.getByRole('table', { name: caption });
}

/** The entries of the list named `name` inside `container`, as text. */
function listEntries(container: HTMLElement, name: string): (string | null)[] {
  return within(within(container).getByRole('list', { name }))
    .getAllByRole('listitem')
    .map((entry) => entry.textContent);
}

/** The position of a metric's cell in a {@link bodyRows} row: after the toggle and the asset. */
function cellIndex(key: WhitePaperMetricKey): number {
  return 2 + WHITE_PAPER_METRIC_KEYS.indexOf(key);
}

/** The evidence metric an item cites under `key`. */
function citedMetric(item: Item, key: string): Metric {
  const cited = item.evidence.metrics.find((each) => each.key === key);
  if (cited === undefined) {
    throw new Error(`item ${String(item.id)} cites no ${key}`);
  }
  return cited;
}

/** One evidence metric, as the backend serialises it. */
function metric(key: string, value: Metric['value'], unit: string, currency: string | null): Metric {
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

/**
 * A minimal "No change warranted" white-paper row with no reason or flag, computed from live
 * data, so its Asset cell is its name alone. `overrides` replaces any field (shallow).
 */
function whitePaperItem(
  id: number,
  entity: { name: string; id: string },
  evidence: Evidence,
  overrides: Partial<Item> = {},
): Item {
  return makeItem({
    id,
    rule_id: 'W',
    action: 'No change warranted',
    reason: '',
    entity: { source: 'linkedin', type: 'content_asset', ...entity },
    evidence,
    flags: [],
    data_status: 'live',
    current: makeItemOverlay({ data_status_now: 'live' }),
    ...overrides,
  });
}

/** The current `simulated` flag of every default-page white paper (`analysis/overlay.py`). */
const SIMULATED_LINKEDIN_HUBSPOT = 'Simulated data (LinkedIn, HubSpot)';

/** A default-page Asset cell's text: the asset name, then its simulated-data badge. */
function simulatedAsset(name: string): string {
  return `${name}${SIMULATED_LINKEDIN_HUBSPOT}`;
}

/** The Asset cell (row header) of the row named `name`, among `container`'s row headers. */
function assetCell(container: HTMLElement, name: string): HTMLElement {
  const cell = within(container)
    .getAllByRole('rowheader')
    .find((header) => header.textContent.startsWith(name));
  if (cell === undefined) {
    throw new Error(`no Asset cell for ${name}`);
  }
  return cell;
}

/**
 * A white-paper row whose evidence records no MQL stage label and no MQL metric, only its spend
 * and cohort, so its table's MQL header can come only from `GET /config`.
 */
function unlabelledItem(id: number, name: string, currency: string): Item {
  return whitePaperItem(
    id,
    { name, id: `${name.toLowerCase().replaceAll(' ', '-')}|${currency}` },
    makeEvidence({
      metrics: [metric('spend', '100.00', 'currency', currency)],
      extra: { cohort: { currency, spend_total: '100.00', overlap_note: '' } },
    }),
  );
}

/** A GBP cohort of one row, "UK Report", whose evidence records no MQL label. */
function unlabelledCohort(): Group {
  return makeGroup({
    key: 'cohort:GBP',
    title: 'GBP',
    items: [unlabelledItem(71, 'UK Report', 'GBP')],
  });
}

/** A resolved stage binding with `label` and `property`. */
function binding(label: string | null, property: string | null): ResolvedBindingView {
  return { available: true, property, stage_id: null, label, reason: null };
}

/** The shipped configuration with the MQL bindings and mapping replaced. */
function configWithMql(
  primary: ResolvedBindingView | null,
  companion: ResolvedBindingView | null,
  mapping: Partial<ConfigView['hubspot_mapping']['mql']>,
): ConfigView {
  const base = makeConfig();
  const withMql = (
    bindings: ConfigView['crm_bindings'],
    mql: ResolvedBindingView | null,
  ): ConfigView['crm_bindings'] =>
    bindings === null || mql === null ? null : { ...bindings, mql };
  return {
    ...base,
    crm_bindings: withMql(base.crm_bindings, primary),
    companion_crm_bindings: withMql(base.companion_crm_bindings, companion),
    hubspot_mapping: {
      ...base.hubspot_mapping,
      mql: { ...base.hubspot_mapping.mql, ...mapping },
    },
  };
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('WhitePapersSection', () => {
  describe('cohort tables', () => {
    it('renders one captioned table per currency cohort, with the columns in the AAP order', async () => {
      const { api } = await renderDefault();

      const section = screen.getByRole('region', { name: 'White papers' });
      const eur = within(section).getByRole('table', { name: 'White papers (EUR)' });
      const usd = within(section).getByRole('table', { name: 'White papers (USD)' });
      expect(eur.compareDocumentPosition(usd) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(headers(eur)).toEqual(assetHeaders(MQL_BOUND));
      expect(headers(usd)).toEqual(assetHeaders(MQL_BOUND));
      // MQL and Qualified Lead are two columns; no header combines them.
      for (const header of headers(eur)) {
        expect(header?.includes('MQL') === true && header.includes('Qualified')).toBe(false);
      }
      // The configuration is the only read.
      expect(api.calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /config']);
    });

    it('renders floored counts as "≥ N" and costs as "≤ X", in the API rank order', async () => {
      await renderDefault();

      expect(bodyRows(table('White papers (EUR)'))).toEqual([
        [
          'Details for The 2026 B2B Pipeline Report',
          simulatedAsset('The 2026 B2B Pipeline Report'),
          '€2,840.00',
          '116',
          '≥ 14',
          '≥ 6',
          '≥ 3',
          '≥ 1',
          '≥ 1',
          '≤ €473.33',
          '≤ €946.67',
          'Double down: increase spend on Lead Gen — Pipeline Report EMEA',
        ],
        [
          'Details for The Attribution Playbook',
          simulatedAsset('The Attribution Playbook'),
          '€1,500.00',
          '31',
          '5',
          '2',
          '1',
          '0',
          '0',
          '€750.00',
          '€1,500.00',
          'No change warranted',
        ],
      ]);
    });

    it('renders every floored cell as formatMetric renders its evidence metric', async () => {
      await renderDefault();

      // The W1 row sits under a 12% contact floor: every count is a floor, every cost a ceiling.
      const wEur = pageItem(FIXTURE_ITEM_IDS.wEur);
      const [row] = bodyRows(table('White papers (EUR)'));
      const expected = WHITE_PAPER_METRIC_KEYS.map((key) => formatMetric(citedMetric(wEur, key)));
      expect(row?.slice(cellIndex('spend'), cellIndex('cost_per_meeting') + 1)).toEqual(expected);
      expect(row?.[cellIndex('mql')]).toBe('≥ 14');
      expect(row?.[cellIndex('ql')]).toBe('≥ 6');
      expect(row?.[cellIndex('cost_per_ql')]).toMatch(/^≤ €/u);
      expect(row?.[cellIndex('cost_per_meeting')]).toMatch(/^≤ €/u);
      // Spend and platform leads are not attribution counts, so they carry no floor mark.
      expect(row?.[cellIndex('spend')]).toBe('€2,840.00');
      expect(row?.[cellIndex('linkedin_leads')]).toBe('116');
    });

    it("keeps MQL and Qualified Lead in separate cells, and totals nothing but each cohort's spend", async () => {
      await renderDefault();

      const section = screen.getByRole('region', { name: 'White papers' });
      const eur = table('White papers (EUR)');
      const columns = headers(eur);
      const mqlColumn = columns.indexOf(MQL_BOUND);
      const qlColumn = columns.indexOf('Qualified Lead');
      expect(mqlColumn).toBe(cellIndex('mql'));
      expect(qlColumn).toBe(cellIndex('ql'));
      // Only the MQL header names MQL, only the QL columns name Qualified Leads, and no header
      // names a bare "QL" that could stand for a blend of the two.
      expect(columns.filter((header) => header?.includes('MQL'))).toEqual([MQL_BOUND]);
      expect(columns.filter((header) => header?.includes('Qualified'))).toEqual([
        'Qualified Lead',
        'Cost per Qualified Lead',
      ]);
      expect(columns.filter((header) => header !== null && /\bQL\b/u.test(header))).toEqual([]);
      // Each row's MQL and QL are two cells, each with its own count.
      for (const row of within(eur).getAllByRole('row').slice(1)) {
        const cells = Array.from(row.children);
        expect(cells[mqlColumn]).toBeDefined();
        expect(cells[mqlColumn]).not.toBe(cells[qlColumn]);
      }
      expect(bodyRows(eur).map((row) => [row[mqlColumn], row[qlColumn]])).toEqual([
        ['≥ 14', '≥ 6'],
        ['5', '2'],
      ]);

      // The captions name a cohort and nothing else; the only totals are each cohort's own
      // spend total, so no MQL, QL, meeting or deal total exists and no amount crosses currencies.
      expect(
        within(section)
          .getAllByRole('table')
          .map((each) => each.querySelector('caption')?.textContent),
      ).toEqual(['White papers (EUR)', 'White papers (USD)']);
      expect(within(section).queryAllByText(/total/iu).map((node) => node.textContent)).toEqual([
        'Spend total (EUR): €4,340.00',
        'Spend total (USD): $1,210.00',
      ]);
      // EUR 4,340.00 + USD 1,210.00 would be 5,550.00; MQL + QL of the first row would be 20.
      expect(section.textContent).not.toContain('5,550');
      expect(within(section).queryByText(/^(≥ )?20$/u)).toBeNull();
    });

    it('keeps the API order: a higher-cost row given first stays first', async () => {
      const section = defaultWhitePapers();
      const eur = section.groups[0];
      if (eur === undefined) {
        throw new Error('the default page has no EUR cohort');
      }
      // The API sends the €750.00 row ahead of the ≤ €473.33 one; the section never re-sorts.
      eur.items = [...eur.items].reverse().map((item, index) => ({ ...item, rank: index + 1 }));
      await renderWhitePapers(section);
      await screen.findAllByRole('columnheader', { name: MQL_BOUND });

      expect(
        bodyRows(table('White papers (EUR)')).map((row) => [row[1], row[cellIndex('cost_per_ql')]]),
      ).toEqual([
        [simulatedAsset('The Attribution Playbook'), '€750.00'],
        [simulatedAsset('The 2026 B2B Pipeline Report'), '≤ €473.33'],
      ]);
    });

    it('renders an unavailable Qualified Lead count as "unavailable", never 0, per row and per creative', async () => {
      const unavailableCount = {
        value: null,
        floor: false,
        unavailable: true,
        reason: 'QL stage "Qualified Lead" not found among the lifecyclestage options',
        display: 'unavailable',
      };
      const unavailableCost = {
        value: null,
        ceiling: false,
        note: 'the Qualified Lead count is unavailable',
        unavailable: true,
        display: 'unavailable',
      };
      const count = (value: number) => ({
        value,
        floor: false,
        unavailable: false,
        reason: null,
        display: String(value),
      });
      const meetingCost = {
        value: '600.00',
        ceiling: false,
        note: null,
        unavailable: false,
        display: '600.00',
      };
      const unboundQl = whitePaperItem(
        66,
        { name: 'Unbound QL Report', id: 'unbound-ql-report|USD' },
        makeEvidence({
          metrics: [
            metric('spend', '600.00', 'currency', 'USD'),
            metric('linkedin_leads', 18, 'leads', null),
            { ...metric('mql', 4, 'contacts', null), label: MQL_BOUND },
            metric('ql', null, 'contacts', null),
            metric('meetings_booked', 1, 'meetings', null),
            metric('other_meetings', 0, 'meetings', null),
            metric('deals_after_touch', 0, 'deals', null),
            metric('cost_per_ql', null, 'currency', 'USD'),
            metric('cost_per_meeting', '600.00', 'currency', 'USD'),
          ],
          extra: {
            asset: { slug: 'unbound-ql-report', title: 'Unbound QL Report', currency: 'USD' },
            rows: [
              {
                creative_id: 1701,
                creative_urn: 'urn:li:sponsoredCreative:990001701',
                label: 'Variant Q',
                spend: '600.00',
                leads: 18,
                mql: count(4),
                ql: unavailableCount,
                meetings_booked: count(1),
                other_meetings: count(0),
                deals: count(0),
                cost_per_ql: unavailableCost,
                cost_per_meeting: meetingCost,
              },
            ],
            costs: { cost_per_ql: unavailableCost, cost_per_meeting: meetingCost },
            cohort: {
              currency: 'USD',
              spend_total: '600.00',
              overlap_note: 'counts overlap across assets',
            },
          },
        }),
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [makeGroup({ key: 'cohort:USD', title: 'USD', items: [unboundQl] })],
        }),
      );
      await screen.findAllByRole('columnheader', { name: MQL_BOUND });

      const [row] = bodyRows(table('White papers (USD)'));
      expect(row?.[cellIndex('ql')]).toBe('unavailable');
      expect(row?.[cellIndex('ql')]).toBe(formatMetric(citedMetric(unboundQl, 'ql')));
      // The cost over an unavailable count is unavailable too: never "—", "0" or a finite value.
      expect(row?.[cellIndex('cost_per_ql')]).toBe('unavailable');
      // The known counts beside it are unaffected, and MQL stays its own count.
      expect(row).toEqual([
        'Details for Unbound QL Report',
        'Unbound QL Report',
        '$600.00',
        '18',
        '4',
        'unavailable',
        '1',
        '0',
        '0',
        'unavailable',
        '$600.00',
        'No change warranted',
      ]);

      fireEvent.click(screen.getByRole('button', { name: 'Details for Unbound QL Report' }));
      expect(bodyRows(table('Per creative: Unbound QL Report'))).toEqual([
        ['Variant Q', '$600.00', '18', '4', 'unavailable', '1', '0', '0', 'unavailable', '$600.00'],
      ]);
    });

    it('renders an unavailable MQL as "unavailable" and the costs at QL 0 as "—" with their reasons', async () => {
      await renderDefault();

      expect(bodyRows(table('White papers (USD)'))).toEqual([
        [
          'Details for Paid Media Benchmarks 2026',
          simulatedAsset('Paid Media Benchmarks 2026'),
          '$1,210.00',
          '41',
          'unavailable',
          '0',
          '0',
          '0',
          '0',
          '— (no QL yet)',
          '— (no meetings yet)',
          'Insufficient data',
        ],
      ]);
    });

    it('notes each cohort: its own spend total, overlapping counts, platform leads and booked meetings', async () => {
      await renderDefault();

      expect(listEntries(document.body, 'Notes on the EUR white papers')).toEqual([
        'Spend total (EUR): €4,340.00',
        'counts overlap across assets',
        LINKEDIN_LEADS_NOTE,
        MEETINGS_BOOKED_NOTE,
      ]);
      expect(listEntries(document.body, 'Notes on the USD white papers')).toEqual([
        'Spend total (USD): $1,210.00',
        'counts overlap across assets',
        LINKEDIN_LEADS_NOTE,
        MEETINGS_BOOKED_NOTE,
      ]);
      // The skipped-stage finding sits at the top of the section.
      const section = screen.getByRole('region', { name: 'White papers' });
      const finding = within(section).getByText(
        'MQL (First meeting booked): 2 contacts skipped this stage (not counted)',
      );
      expect(
        finding.compareDocumentPosition(table('White papers (EUR)')) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });

    it('expands a row to its per-creative rows, unknown creative included, with its evidence and actions', async () => {
      await renderDefault();

      const toggle = screen.getByRole('button', {
        name: 'Details for The 2026 B2B Pipeline Report',
      });
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('true');

      const perCreative = table('Per creative: The 2026 B2B Pipeline Report');
      expect(headers(perCreative)).toEqual(['Creative', ...metricHeaders(MQL_BOUND)]);
      expect(bodyRows(perCreative)).toEqual([
        ['Variant A', '€1,480.00', '64', '≥ 8', '≥ 4', '≥ 2', '≥ 1', '≥ 1', '≤ €370.00', '≤ €740.00'],
        ['Variant B', '€1,360.00', '49', '≥ 5', '≥ 2', '≥ 1', '≥ 0', '≥ 0', '≤ €680.00', '≤ €1,360.00'],
        [
          'unknown creative',
          '€0.00',
          '3',
          '≥ 1',
          '≥ 0',
          '≥ 0',
          '≥ 0',
          '≥ 0',
          '— (the unknown creative row holds no spend)',
          '— (the unknown creative row holds no spend)',
        ],
      ]);

      const details = perCreative.closest('td');
      if (details === null) {
        throw new Error('the per-creative table is not inside the detail cell');
      }
      const wEur = pageItem(FIXTURE_ITEM_IDS.wEur);
      expect(within(details).getByText(wEur.reason)).toBeTruthy();
      // The evidence as every recommendation shows it (AAP 0.5.4.8): each cited metric against
      // its baseline, the sample, the thresholds applied and the datasets used.
      const evidence = table('Evidence for The 2026 B2B Pipeline Report');
      expect(evidence.closest('td')).toBe(details);
      expect(headers(evidence)).toEqual(['Metric', 'Value', 'Baseline']);
      const evidenceRows = bodyRows(evidence);
      expect(evidenceRows.map((row) => row[0])).toEqual(
        wEur.evidence.metrics.map((cited) => cited.label),
      );
      expect(evidenceRows.find((row) => row[0] === 'Cost per Qualified Lead')).toEqual([
        'Cost per Qualified Lead',
        '≤ €473.33',
        'Cohort median cost per Qualified Lead (EUR): €611.67',
      ]);
      expect(evidenceRows.find((row) => row[0] === MQL_BOUND)?.[1]).toBe('≥ 14');
      expect(within(details).getByText('Sample: 6 / 3 Qualified Leads')).toBeTruthy();
      const thresholds = listEntries(details, 'Thresholds applied');
      expect(thresholds).toHaveLength(Object.keys(wEur.evidence.thresholds).length);
      expect(thresholds).toContain('w.min_ql = 3');
      expect(thresholds).toContain('w.double_down_ratio = 0.8');
      expect(listEntries(details, 'Data used')).toEqual(
        wEur.evidence.datasets.map(
          (entry) => `${entry.dataset}: complete, covered through ${String(entry.covered_through)}`,
        ),
      );
      expect(listEntries(details, 'Data used')).toHaveLength(6);
      expect(within(details).getByText('Floor: 12% of leads unmatched in HubSpot')).toBeTruthy();
      expect(
        within(details).getByText(
          'Meeting time proxy: 1 other meeting (ordering unverified): booking time is proxied ' +
            'by record creation, so it is excluded from cost per meeting',
        ),
      ).toBeTruthy();
      expect(within(details).getByText(/^2026-09-08 – 2026-10-05 · 28 days/u)).toBeTruthy();
      // White-paper rows carry no decision subject: Mark done, Dismiss and Snooze only.
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(details).getByRole('button', { name }).hasAttribute('disabled')).toBe(false);
      }
      expect(within(details).queryByRole('button', { name: 'Record decision' })).toBeNull();
      expect(within(details).queryByText('Computed from replaced data')).toBeNull();
      expect(
        within(details).queryByRole('link', { name: /^View (blocking|open) discrepanc/u }),
      ).toBeNull();

      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByRole('table', { name: 'Per creative: The 2026 B2B Pipeline Report' })).toBeNull();
    });

    it('posts "Mark done" feedback for an expanded row once, with the CSRF header', async () => {
      const { api } = await renderDefault();
      // Only this case acts on a row, so only this case answers the feedback request.
      api.use({
        'POST /recommendations/:id/feedback': json(
          201,
          makeFeedback('done', {
            recommendation_id: FIXTURE_ITEM_IDS.wEur,
            rule_id: 'W1',
            entity_type: 'content_asset',
            entity_id: 'b2b-pipeline-report-2026|EUR',
          }),
        ),
      });

      fireEvent.click(screen.getByRole('button', { name: 'Details for The 2026 B2B Pipeline Report' }));
      const details = table('Per creative: The 2026 B2B Pipeline Report').closest('td');
      if (details === null) {
        throw new Error('the per-creative table is not inside the detail cell');
      }
      fireEvent.click(within(details).getByRole('button', { name: 'Mark done' }));

      expect(await within(details).findByText('Marked done')).toBeTruthy();
      const posts = api.calls.filter((call) => call.method === 'POST');
      expect(posts).toHaveLength(1);
      const [post] = posts;
      expect(post?.path).toBe(`/recommendations/${String(FIXTURE_ITEM_IDS.wEur)}/feedback`);
      expect(post?.body).toEqual({ kind: 'done' });
      expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');
      // Feedback never writes a decision, and a recorded feedback ends the row's actions.
      expect(api.calls.some((call) => call.path === '/decisions')).toBe(false);
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(details).getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
      }
    });
  });

  describe('the simulated-data label', () => {
    it("labels each collapsed row of simulated data in its Asset cell, in the flag's own words", async () => {
      await renderDefault();

      for (const [caption, names] of [
        ['White papers (EUR)', ['The 2026 B2B Pipeline Report', 'The Attribution Playbook']],
        ['White papers (USD)', ['Paid Media Benchmarks 2026']],
      ] as const) {
        const cohort = table(caption);
        for (const name of names) {
          const cell = assetCell(cohort, name);
          expect(cell.textContent).toBe(simulatedAsset(name));
          expect(within(cell).getByText(SIMULATED_LINKEDIN_HUBSPOT)).toBeTruthy();
          // Nothing was expanded to show it, and the toggle keeps the plain name.
          const toggle = within(cohort).getByRole('button', { name: `Details for ${name}` });
          expect(toggle.getAttribute('aria-expanded')).toBe('false');
          expect(screen.queryByRole('table', { name: `Per creative: ${name}` })).toBeNull();
        }
      }
    });

    it('shows the mixed-data message, and a recorded flag after the source went live', async () => {
      const mixed = whitePaperItem(
        81,
        { name: 'Mixed Data Report', id: 'mixed-data|EUR' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
        {
          flags: [
            { code: 'simulated', message: 'Joins live and simulated data (HubSpot simulated)' },
          ],
          data_status: 'mixed',
          current: makeItemOverlay({
            data_status_now: 'mixed',
            flags_now: [
              { code: 'simulated', message: 'Joins live and simulated data (HubSpot simulated)' },
            ],
          }),
        },
      );
      // Computed from simulated data, read after LinkedIn and HubSpot went live: the overlay
      // carries no simulated flag now, and the recorded one still says so (AAP 0.3.2).
      const recorded = whitePaperItem(
        82,
        { name: 'Recorded Simulation Report', id: 'recorded-simulation|EUR' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
        {
          flags: [{ code: 'simulated', message: SIMULATED_LINKEDIN_HUBSPOT }],
          data_status: 'simulated',
          current: makeItemOverlay({ data_status_now: 'live', flags_now: [] }),
        },
      );
      const live = whitePaperItem(
        83,
        { name: 'Live Data Report', id: 'live-data|EUR' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [mixed, recorded, live] })],
        }),
      );

      const cohort = table('White papers (EUR)');
      expect(assetCell(cohort, 'Mixed Data Report').textContent).toBe(
        'Mixed Data ReportJoins live and simulated data (HubSpot simulated)',
      );
      expect(assetCell(cohort, 'Recorded Simulation Report').textContent).toBe(
        `Recorded Simulation Report${SIMULATED_LINKEDIN_HUBSPOT}`,
      );
      // A row of live data only carries no label.
      expect(assetCell(cohort, 'Live Data Report').textContent).toBe('Live Data Report');
      expect(bodyRows(cohort).map((row) => row[0])).toEqual([
        'Details for Mixed Data Report',
        'Details for Recorded Simulation Report',
        'Details for Live Data Report',
      ]);
    });

    it('names the label from the data status when no simulated flag was given', async () => {
      const simulated = whitePaperItem(
        84,
        { name: 'Unflagged Simulated Report', id: 'unflagged-simulated|EUR' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
        { data_status: 'simulated' },
      );
      const mixedNow = whitePaperItem(
        85,
        { name: 'Unflagged Mixed Report', id: 'unflagged-mixed|EUR' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
        { current: makeItemOverlay({ data_status_now: 'mixed' }) },
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [simulated, mixedNow] })],
        }),
      );

      const cohort = table('White papers (EUR)');
      expect(assetCell(cohort, 'Unflagged Simulated Report').textContent).toBe(
        'Unflagged Simulated ReportSimulated data',
      );
      expect(assetCell(cohort, 'Unflagged Mixed Report').textContent).toBe(
        'Unflagged Mixed ReportJoins live and simulated data',
      );
    });
  });

  describe('the MQL header', () => {
    it('heads every table and per-creative table with the frozen label, never a later binding', async () => {
      // The binding has since moved to another stage. The default page's rows recorded "First
      // meeting booked" (extra.mql_label), so their counts keep that label; only the table
      // whose row recorded none takes the current configuration's.
      const rebound = configWithMql(binding('Demo held', null), binding('Demo held', null), {
        stage_labels: ['Demo held'],
      });
      const section = defaultWhitePapers();
      section.groups.push(
        unlabelledCohort(),
      );
      await renderWhitePapers(section, json(200, rebound));
      await screen.findAllByRole('columnheader', { name: 'MQL (Demo held)' });

      expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders(MQL_BOUND));
      expect(headers(table('White papers (USD)'))).toEqual(assetHeaders(MQL_BOUND));
      expect(headers(table('White papers (GBP)'))).toEqual(assetHeaders('MQL (Demo held)'));
      fireEvent.click(screen.getByRole('button', { name: 'Details for The 2026 B2B Pipeline Report' }));
      expect(headers(table('Per creative: The 2026 B2B Pipeline Report'))).toEqual([
        'Creative',
        ...metricHeaders(MQL_BOUND),
      ]);
      fireEvent.click(screen.getByRole('button', { name: 'Details for UK Report' }));
      expect(headers(table('Per creative: UK Report'))).toEqual([
        'Creative',
        ...metricHeaders('MQL (Demo held)'),
      ]);
    });

    it('takes the recorded MQL metric label when the evidence records no stage label', async () => {
      // A binding that named no label is recorded as `mql_label: null` with the metric headed
      // plainly "MQL": the column stays plain, whatever the configuration binds now.
      const plain = whitePaperItem(
        72,
        { name: 'Unlabelled Binding Report', id: 'unlabelled-binding|EUR' },
        makeEvidence({
          metrics: [{ ...metric('mql', 3, 'contacts', null), label: 'MQL' }],
          extra: { mql_label: null, cohort: { currency: 'EUR' } },
        }),
      );
      const custom = whitePaperItem(
        73,
        { name: 'Custom Run Report', id: 'custom-run|USD' },
        makeEvidence({
          metrics: [{ ...metric('mql', 2, 'contacts', null), label: 'MQL (Demo held)' }],
          extra: { cohort: { currency: 'USD' } },
        }),
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [
            makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [plain] }),
            makeGroup({ key: 'cohort:USD', title: 'USD', items: [custom] }),
            unlabelledCohort(),
          ],
        }),
      );
      // Only the GBP row recorded no label, so only its table takes the configured one.
      await screen.findAllByRole('columnheader', { name: MQL_BOUND });

      expect(headers(table('White papers (GBP)'))).toEqual(assetHeaders(MQL_BOUND));
      expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders('MQL'));
      expect(headers(table('White papers (USD)'))).toEqual(assetHeaders('MQL (Demo held)'));
    });

    it('claims no single label for a table whose rows recorded different ones', async () => {
      const first = whitePaperItem(
        74,
        { name: 'First Binding Report', id: 'first-binding|EUR' },
        makeEvidence({ extra: { mql_label: 'First meeting booked', cohort: { currency: 'EUR' } } }),
      );
      const second = whitePaperItem(
        75,
        { name: 'Second Binding Report', id: 'second-binding|EUR' },
        makeEvidence({ extra: { mql_label: 'Demo held', cohort: { currency: 'EUR' } } }),
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [first, second] })],
        }),
      );

      expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders('MQL'));
      // Each row's own frozen label heads its per-creative table.
      fireEvent.click(screen.getByRole('button', { name: 'Details for First Binding Report' }));
      fireEvent.click(screen.getByRole('button', { name: 'Details for Second Binding Report' }));
      expect(headers(table('Per creative: First Binding Report'))).toContain(MQL_BOUND);
      expect(headers(table('Per creative: Second Binding Report'))).toContain('MQL (Demo held)');
    });

    it('heads rows without a frozen label plainly while the configuration loads, then with its label', async () => {
      // The configuration answers only when the test releases it.
      const gate: { release?: (response: MockResponse) => void } = {};
      const pending = (): Promise<MockResponse> =>
        new Promise<MockResponse>((resolve) => {
          gate.release = resolve;
        });
      const section = defaultWhitePapers();
      section.groups.push(
        unlabelledCohort(),
      );
      await renderWhitePapers(section, pending);

      expect(headers(table('White papers (GBP)'))).toEqual(assetHeaders('MQL'));
      // Frozen labels need no configuration.
      expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders(MQL_BOUND));
      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      gate.release?.(json(200, makeConfig()));

      await waitFor(() => {
        expect(headers(table('White papers (GBP)'))).toEqual(assetHeaders(MQL_BOUND));
      });
    });

    it('heads rows without a frozen label plainly when the configuration cannot be read', async () => {
      const section = defaultWhitePapers();
      section.groups.push(
        unlabelledCohort(),
      );
      const { api } = await renderWhitePapers(
        section,
        apiError(500, 'config_error', 'thresholds.yaml is invalid'),
      );

      await waitFor(() => {
        expect(api.calls).toHaveLength(1);
      });
      // The failed read settles without retries; the fallback header stays plain, and the
      // frozen headers are unaffected.
      await waitFor(() => {
        expect(headers(table('White papers (GBP)'))).toEqual(assetHeaders('MQL'));
      });
      expect(headers(table('White papers (USD)'))).toEqual(assetHeaders(MQL_BOUND));
    });

    it('follows the backend precedence: resolved label, configured label, then bound property', () => {
      expect(mqlHeader(undefined)).toBe('MQL');
      expect(mqlHeader(makeConfig())).toBe(MQL_BOUND);
      // While LinkedIn is simulated its companion CRM's label wins over the primary portal's.
      expect(
        mqlHeader(configWithMql(binding('Primary stage', null), binding('Companion stage', null), {})),
      ).toBe('MQL (Companion stage)');
      expect(mqlHeader(configWithMql(binding('Primary stage', null), null, {}))).toBe(
        'MQL (Primary stage)',
      );
      expect(
        mqlHeader(configWithMql(binding('  ', null), null, { stage_labels: ['Configured stage'] })),
      ).toBe('MQL (Configured stage)');
      expect(
        mqlHeader(
          configWithMql(binding(null, 'first_meeting_booked_date'), null, {
            mode: 'property',
            stage_labels: [],
            property: 'configured_property',
          }),
        ),
      ).toBe('MQL (first_meeting_booked_date)');
      expect(
        mqlHeader(
          configWithMql(binding(null, null), null, {
            mode: 'property',
            stage_labels: [],
            property: 'configured_property',
          }),
        ),
      ).toBe('MQL (configured_property)');
      // A label that only repeats "MQL", or no label at all, leaves the header plain.
      expect(mqlHeader(configWithMql(binding('mql', null), null, {}))).toBe('MQL');
      expect(
        mqlHeader(configWithMql(binding(null, null), null, { stage_labels: [], property: null })),
      ).toBe('MQL');
    });
  });

  describe('held and historical rows', () => {
    it('holds a held row behind the disclosure in its own table, linked to the blocking discrepancy', async () => {
      const section = defaultWhitePapers();
      const eur = section.groups[0];
      if (eur === undefined) {
        throw new Error('the default page has no EUR cohort');
      }
      eur.items = eur.items.map((item) =>
        item.id === FIXTURE_ITEM_IDS.wEurNoChange
          ? {
              ...item,
              // The discrepancy that held it when the run computed it.
              evidence: {
                ...item.evidence,
                extra: { ...item.evidence.extra, blocking_discrepancies: [51] },
              },
              current: makeItemOverlay({
                held_now: true,
                held_reason_now: 'Resolve discrepancy on Brand Search — US first',
              }),
            }
          : item,
      );
      await renderWhitePapers(section);

      expect(bodyRows(table('White papers (EUR)')).map((row) => row[1])).toEqual([
        simulatedAsset('The 2026 B2B Pipeline Report'),
      ]);
      const heldCaption = 'White papers (EUR) held until discrepancies are resolved';
      expect(screen.queryByRole('table', { name: heldCaption })).toBeNull();
      // The held table is not built until the disclosure is first opened; its region exists.
      const region = document.getElementById('group-white_papers-cohort-EUR-held');
      expect(region?.hidden).toBe(true);
      expect(region?.querySelector('table')).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }));
      expect(region?.hidden).toBe(false);
      const held = table(heldCaption);
      // Its overlay was replaced without current flags, so the recorded flag labels it.
      expect(bodyRows(held).map((row) => row[1])).toEqual([
        'The Attribution PlaybookSimulated data',
      ]);

      fireEvent.click(within(held).getByRole('button', { name: 'Details for The Attribution Playbook' }));
      expect(screen.getByText('Resolve discrepancy on Brand Search — US first')).toBeTruthy();
      // Rendered without the page, no run C0 card is known, so the blocker opens on /decisions.
      expect(
        screen.getByRole('link', { name: 'View blocking discrepancy 51' }).getAttribute('href'),
      ).toBe('/decisions#discrepancy-51');
      expect(screen.queryByRole('link', { name: 'View open discrepancies' })).toBeNull();
    });

    it('links a row held without a recorded blocker to the discrepancy list, saying so', async () => {
      const section = defaultWhitePapers();
      const eur = section.groups[0];
      if (eur === undefined) {
        throw new Error('the default page has no EUR cohort');
      }
      eur.items = eur.items.map((item) =>
        item.id === FIXTURE_ITEM_IDS.wEurNoChange
          ? { ...item, current: makeItemOverlay({ held_now: true }) }
          : item,
      );
      await renderWhitePapers(section);

      fireEvent.click(screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }));
      const held = table('White papers (EUR) held until discrepancies are resolved');
      fireEvent.click(within(held).getByRole('button', { name: 'Details for The Attribution Playbook' }));
      expect(
        screen.getByRole('link', { name: 'View open discrepancies' }).getAttribute('href'),
      ).toBe('#group-campaigns-discrepancies');
      expect(screen.queryByRole('link', { name: /^View blocking discrepancy/u })).toBeNull();
    });

    it('marks a historical row with the replaced-data badge and disables its actions', async () => {
      const section = defaultWhitePapers();
      const usd = section.groups[1];
      const wUsd = usd?.items[0];
      if (usd === undefined || wUsd === undefined) {
        throw new Error('the default page has no USD row');
      }
      // Held by the overlay because its data was replaced: the badge says so, not a link.
      usd.items = [
        {
          ...wUsd,
          current: makeItemOverlay({ historical: true, held_now: true, data_status_now: 'live' }),
        },
      ];
      await renderWhitePapers(section);

      // Readable at once in a table of its own, never behind the held disclosure.
      expect(
        screen.queryByRole('button', { name: /^Held until discrepancies are resolved/u }),
      ).toBeNull();
      expect(
        screen.queryByRole('table', {
          name: 'White papers (USD) held until discrepancies are resolved',
        }),
      ).toBeNull();
      const historical = table('White papers (USD) computed from replaced data');
      const row = bodyRows(historical)[0];
      expect(row?.at(-1)).toBe('Insufficient dataComputed from replaced data');

      fireEvent.click(
        within(historical).getByRole('button', { name: 'Details for Paid Media Benchmarks 2026' }),
      );
      const details = table('Per creative: Paid Media Benchmarks 2026').closest('td');
      if (details === null) {
        throw new Error('no detail cell');
      }
      expect(
        within(details).getByText(
          'Simulated LinkedIn data and Simulated HubSpot data, since replaced by live',
        ),
      ).toBeTruthy();
      expect(
        within(details).queryByRole('link', { name: /^View (blocking|open) discrepanc/u }),
      ).toBeNull();
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(details).getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
      }
    });

    it('orders a cohort as visible rows, rows computed from replaced data, then the held disclosure', async () => {
      const section = defaultWhitePapers();
      const eur = section.groups[0];
      const [current, other] = eur?.items ?? [];
      if (eur === undefined || current === undefined || other === undefined) {
        throw new Error('the default page has no two EUR rows');
      }
      eur.items = [
        current,
        {
          ...other,
          current: makeItemOverlay({ historical: true, held_now: true, data_status_now: 'live' }),
        },
        {
          ...current,
          id: 9901,
          entity: { ...current.entity, id: 'held-report|EUR', name: 'Held Report' },
          current: makeItemOverlay({
            held_now: true,
            held_reason_now: 'Resolve discrepancy first',
          }),
        },
      ];
      await renderWhitePapers(section);

      const visible = table('White papers (EUR)');
      const historical = table('White papers (EUR) computed from replaced data');
      const toggle = screen.getByRole('button', {
        name: 'Held until discrepancies are resolved (1)',
      });
      expect(bodyRows(visible).map((row) => row[1])).toEqual([
        simulatedAsset('The 2026 B2B Pipeline Report'),
      ]);
      // Its overlay was replaced without current flags, so the recorded flag labels it.
      expect(bodyRows(historical).map((row) => row[1])).toEqual([
        'The Attribution PlaybookSimulated data',
      ]);
      expect(
        visible.compareDocumentPosition(historical) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(
        historical.compareDocumentPosition(toggle) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();

      fireEvent.click(toggle);
      // Its overlay, too, carries no current flags, so the recorded flag labels it.
      expect(
        bodyRows(table('White papers (EUR) held until discrepancies are resolved')).map(
          (row) => row[1],
        ),
      ).toEqual(['Held ReportSimulated data']);
    });
  });

  describe('fallbacks for sparse evidence', () => {
    it('takes the cohort currency from the evidence, then the group title, then the key', async () => {
      const gbp = whitePaperItem(
        61,
        { name: 'UK Report', id: 'uk-report|GBP' },
        makeEvidence({ metrics: [metric('spend', '900.00', 'currency', 'GBP')] }),
      );
      const chf = whitePaperItem(62, { name: 'Swiss Report', id: 'swiss-report|CHF' }, makeEvidence());
      const none = whitePaperItem(63, { name: 'Untitled Report', id: 'untitled|XYZ' }, makeEvidence());
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [
            makeGroup({ key: 'cohort:GBP', title: 'pounds', items: [gbp] }),
            makeGroup({ key: 'cohort:CHF', title: 'CHF', items: [chf] }),
            makeGroup({ key: 'cohort:XYZ', title: '  ', items: [none] }),
          ],
        }),
      );

      expect(table('White papers (GBP)')).toBeTruthy();
      expect(table('White papers (CHF)')).toBeTruthy();
      expect(table('White papers (cohort:XYZ)')).toBeTruthy();
      // Without the backend's cohort view the total is unavailable, never summed here.
      expect(listEntries(document.body, 'Notes on the GBP white papers')).toEqual([
        'Spend total (GBP): unavailable',
        'counts overlap across assets',
        LINKEDIN_LEADS_NOTE,
        MEETINGS_BOOKED_NOTE,
      ]);
    });

    it('reads "unavailable" for every missing cell, names an unnamed asset, and details what is there', async () => {
      const unnamed = whitePaperItem(
        64,
        { name: '  ', id: 'orphan-asset|EUR' },
        makeEvidence({
          baselines: [{ key: 'unpaired_baseline', label: 'Unpaired baseline', value: '0.5' }],
          sample: { observed: null, required: null, unit: '', projected: null },
          extra: { cohort: { currency: 'EUR', spend_total: '10.00', overlap_note: '' } },
        }),
      );
      const anonymous = whitePaperItem(
        65,
        { name: '', id: '' },
        makeEvidence({ extra: { cohort: { currency: 'EUR' } } }),
      );
      await renderWhitePapers(
        makeSection('white_papers', {
          groups: [makeGroup({ key: 'cohort:EUR', title: 'EUR', items: [unnamed, anonymous] })],
        }),
      );
      await screen.findAllByRole('columnheader', { name: MQL_BOUND });

      const rows = bodyRows(table('White papers (EUR)'));
      const unavailableCells = Array.from({ length: 9 }, () => 'unavailable');
      expect(rows).toEqual([
        ['Details for orphan-asset|EUR', 'orphan-asset|EUR', ...unavailableCells, 'No change warranted'],
        ['Details for White paper 65', 'White paper 65', ...unavailableCells, 'No change warranted'],
      ]);
      // A blank overlap note falls back to the backend's wording.
      expect(listEntries(document.body, 'Notes on the EUR white papers').slice(0, 2)).toEqual([
        'Spend total (EUR): €10.00',
        'counts overlap across assets',
      ]);

      fireEvent.click(screen.getByRole('button', { name: 'Details for orphan-asset|EUR' }));
      const perCreative = table('Per creative: orphan-asset|EUR');
      expect(within(perCreative).getByText('No per-creative rows recorded for this white paper')).toBeTruthy();
      const details = perCreative.closest('td');
      if (details === null) {
        throw new Error('no detail cell');
      }
      // No metric is cited, so the evidence table says so; a baseline with no metric to pair
      // with is listed as its plain value, and no sample applies.
      const evidence = table('Evidence for orphan-asset|EUR');
      expect(within(evidence).getByText('No metrics recorded')).toBeTruthy();
      expect(listEntries(details, 'Baselines')).toEqual(['Unpaired baseline: 0.5']);
      expect(within(details).queryByText(/^Sample:/u)).toBeNull();
      expect(within(details).queryByRole('list', { name: 'Thresholds applied' })).toBeNull();

      fireEvent.click(screen.getByRole('button', { name: 'Details for White paper 65' }));
      const sparse = table('Evidence for White paper 65').closest('td');
      if (sparse === null) {
        throw new Error('no detail cell');
      }
      expect(within(sparse).queryByRole('list', { name: 'Baselines' })).toBeNull();
      expect(listEntries(sparse, 'Data used')).toEqual([
        'google.metrics_daily: complete, covered through 2026-10-05',
      ]);
    });

    it('shows the empty state when no white paper has a row to show', async () => {
      await renderWhitePapers(makeSection('white_papers'));

      const section = screen.getByRole('region', { name: 'White papers' });
      expect(within(section).queryByRole('table')).toBeNull();
      expect(within(section).getByText('No actions in this section')).toBeTruthy();
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Bounded batches: assets × creatives × currencies
// ---------------------------------------------------------------------------------------------

describe('long white-paper tables', () => {
  /** `count` copies of `source` ranked 1…count, ids from `firstId`, named "{currency} asset {n}". */
  function cohortOf(source: Item, count: number, firstId: number, currency: string): Item[] {
    return Array.from({ length: count }, (_, index) => ({
      ...source,
      id: firstId + index,
      rank: index + 1,
      entity: {
        ...source.entity,
        id: `asset-${String(index + 1)}|${currency}`,
        name: `${currency} asset ${String(index + 1)}`,
      },
    }));
  }

  /**
   * `item` with `count` per-creative rows "Creative 1"… (copies of its first row under new
   * creative ids), followed by its own last row, "unknown creative".
   */
  function withCreatives(item: Item, count: number): Item {
    const extra = item.evidence.extra ?? {};
    const rows: unknown[] = Array.isArray(extra.rows) ? extra.rows : [];
    const [template] = rows;
    const unknownCreative = rows.at(-1);
    if (typeof template !== 'object' || template === null || unknownCreative === undefined) {
      throw new Error(`item ${String(item.id)} has no per-creative rows to copy`);
    }
    const creatives = Array.from({ length: count }, (_, index) => ({
      ...template,
      creative_id: 30001 + index,
      creative_urn: `urn:li:sponsoredCreative:${String(30001 + index)}`,
      label: `Creative ${String(index + 1)}`,
    }));
    return {
      ...item,
      evidence: { ...item.evidence, extra: { ...extra, rows: [...creatives, unknownCreative] } },
    };
  }

  /** The row headers of `table`'s own rows (a nested per-creative table's excluded). */
  function rowHeaders(scope: HTMLElement): string[] {
    return within(scope)
      .getAllByRole('rowheader')
      .filter((cell) => cell.closest('table') === scope)
      .map((cell) => cell.textContent);
  }

  /** The batch statuses inside `scope` ("Showing …"), told apart from the rows' own regions. */
  function batchStatuses(scope: HTMLElement): string[] {
    return within(scope)
      .queryAllByRole('status')
      .map((status) => status.textContent)
      .filter((text) => text.startsWith('Showing '));
  }

  /** "{currency} asset {n}" for n = 1…count. */
  function assetNames(currency: string, count: number): string[] {
    return Array.from({ length: count }, (_, index) => `${currency} asset ${String(index + 1)}`);
  }

  /** The Asset cells of {@link assetNames}: each name, then its simulated-data badge. */
  function assetCells(currency: string, count: number): string[] {
    return assetNames(currency, count).map(simulatedAsset);
  }

  it('shows each currency cohort and an expanded asset’s creatives one batch at a time', async () => {
    const eur = cohortOf(pageItem(FIXTURE_ITEM_IDS.wEur), 120, 9001, 'EUR');
    const [firstEur, ...otherEur] = eur;
    if (firstEur === undefined) {
      throw new Error('the EUR cohort is empty');
    }
    const usd = cohortOf(pageItem(FIXTURE_ITEM_IDS.wUsd), 60, 9501, 'USD');
    await renderWhitePapers(
      makeSection('white_papers', {
        groups: [
          makeGroup({
            key: 'cohort:EUR',
            title: 'EUR',
            items: [withCreatives(firstEur, 130), ...otherEur],
          }),
          makeGroup({ key: 'cohort:USD', title: 'USD', items: usd }),
        ],
      }),
    );
    await screen.findAllByRole('columnheader', { name: MQL_BOUND });
    const eurTable = table('White papers (EUR)');
    const usdTable = table('White papers (USD)');

    // Each cohort keeps its own table, caption and bound, in API order.
    expect(rowHeaders(eurTable)).toEqual(assetCells('EUR', 50));
    expect(rowHeaders(usdTable)).toEqual(assetCells('USD', 50));
    const section = screen.getByRole('region', { name: 'White papers' });
    expect(batchStatuses(section)).toEqual([
      'Showing 50 of 120 white papers',
      'Showing 50 of 60 white papers',
    ]);
    // USD amounts stay in USD: no row is converted or summed across cohorts.
    expect(bodyRows(usdTable)[0]?.[2]?.startsWith('$')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: 'Show 50 more white papers' }));
    expect(rowHeaders(eurTable)).toEqual(assetCells('EUR', 100));
    expect(document.activeElement?.textContent).toBe('EUR asset 51');
    expect(document.activeElement?.closest('table')).toBe(eurTable);
    fireEvent.click(screen.getByRole('button', { name: 'Show 20 more white papers' }));
    expect(rowHeaders(eurTable)).toEqual(assetCells('EUR', 120));
    fireEvent.click(screen.getByRole('button', { name: 'Show 10 more white papers' }));
    expect(rowHeaders(usdTable)).toEqual(assetCells('USD', 60));
    expect(batchStatuses(section)).toEqual([
      'Showing all 120 white papers',
      'Showing all 60 white papers',
    ]);

    // The expanded asset's per-creative table is bounded too, "unknown creative" kept last.
    fireEvent.click(screen.getByRole('button', { name: 'Details for EUR asset 1' }));
    const perCreative = table('Per creative: EUR asset 1');
    const creativeNames = (count: number) =>
      Array.from({ length: count }, (_, index) => `Creative ${String(index + 1)}`);
    expect(rowHeaders(perCreative)).toEqual(creativeNames(50));
    const details = perCreative.closest('td');
    if (details === null) {
      throw new Error('the per-creative table is not inside the detail cell');
    }
    expect(batchStatuses(details)).toEqual(['Showing 50 of 131 creatives']);
    fireEvent.click(within(details).getByRole('button', { name: 'Show 50 more creatives' }));
    expect(rowHeaders(perCreative)).toEqual(creativeNames(100));
    expect(document.activeElement?.textContent).toBe('Creative 51');
    fireEvent.click(within(details).getByRole('button', { name: 'Show 31 more creatives' }));
    expect(rowHeaders(perCreative)).toEqual([...creativeNames(130), 'unknown creative']);
    expect(batchStatuses(details)).toEqual(['Showing all 131 creatives']);
    // The evidence after the table is the asset's own, complete.
    expect(within(details).getByText('Floor: 12% of leads unmatched in HubSpot')).toBeTruthy();
  });

  it('renders a cohort at the batch size with every row and no control', async () => {
    await renderWhitePapers(
      makeSection('white_papers', {
        groups: [
          makeGroup({
            key: 'cohort:EUR',
            title: 'EUR',
            items: cohortOf(pageItem(FIXTURE_ITEM_IDS.wEur), 50, 9001, 'EUR'),
          }),
        ],
      }),
    );
    await screen.findAllByRole('columnheader', { name: MQL_BOUND });

    expect(rowHeaders(table('White papers (EUR)'))).toEqual(assetCells('EUR', 50));
    expect(screen.queryByRole('button', { name: /^Show \d+ more/u })).toBeNull();
    expect(batchStatuses(screen.getByRole('region', { name: 'White papers' }))).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------
// A section rendered again
// ---------------------------------------------------------------------------------------------

describe('WhitePapersSection rendered again', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /**
   * Renders the section from state, then returns `show`, which hands it a new section object as a
   * refetched page does, and waits for the bound MQL header.
   */
  async function renderReplaceable(initial: Section): Promise<(next: Section) => void> {
    const control: { show?: (next: Section) => void } = {};
    function Host(): ReactElement {
      const [section, setSection] = useState(initial);
      control.show = setSection;
      return <WhitePapersSection section={section} run={RUN} overlay={OVERLAY} />;
    }
    mockApi({ 'GET /config': json(200, makeConfig()) });
    await renderWithProviders(<Host />);
    await screen.findAllByRole('columnheader', { name: MQL_BOUND });
    return (next) => {
      act(() => {
        control.show?.(next);
      });
    };
  }

  /** A new section object holding the very same item objects, as structural sharing keeps them. */
  function sameItems(section: Section): Section {
    return { ...section, groups: section.groups.map((group) => ({ ...group })) };
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

  it('formats each asset row once per item object, and again only for an item that changed', async () => {
    const section = defaultWhitePapers();
    const newSpend = { ...citedMetric(pageItem(FIXTURE_ITEM_IDS.wEur), 'spend'), value: '3000.00' };
    const costNotes = vi.spyOn(evidenceExtraModule, 'whitePaperCostNotes');
    const show = await renderReplaceable(section);
    // Three rows, each formatted once, although the section rendered again for the configuration.
    expect(costNotes).toHaveBeenCalledTimes(3);

    show(sameItems(section));
    expect(costNotes).toHaveBeenCalledTimes(3);
    expect(bodyRows(table('White papers (EUR)'))[0]?.[cellIndex('spend')]).toBe('€2,840.00');

    show(
      withItem(section, FIXTURE_ITEM_IDS.wEur, (item) => ({
        ...item,
        evidence: {
          ...item.evidence,
          metrics: item.evidence.metrics.map((each) => (each.key === 'spend' ? newSpend : each)),
        },
      })),
    );
    expect(costNotes).toHaveBeenCalledTimes(4);
    const [first, second] = bodyRows(table('White papers (EUR)'));
    expect(first?.[cellIndex('spend')]).toBe(formatMetric(newSpend));
    expect(second?.[cellIndex('spend')]).toBe('€1,500.00');
  });

  /** `section` as a newer run recomputes it: every asset the same, every item under a new id. */
  function newerRun(section: Section): Section {
    return {
      ...section,
      groups: section.groups.map((group) => ({
        ...group,
        items: group.items.map((item) => ({ ...item, id: item.id + 1000 })),
      })),
    };
  }

  it('keeps an expanded row open, with focus on its toggle, when a newer run gives its assets new ids', async () => {
    const section = defaultWhitePapers();
    const show = await renderReplaceable(section);
    const name = 'Details for The 2026 B2B Pipeline Report';
    const toggle = screen.getByRole('button', { name });
    fireEvent.click(toggle);
    toggle.focus();
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const markDone = screen.getByRole('button', { name: 'Mark done' });

    show(newerRun(section));

    expect(screen.getByRole('button', { name })).toBe(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(document.activeElement).toBe(toggle);
    const details = table('Per creative: The 2026 B2B Pipeline Report').closest('td');
    if (details === null) {
      throw new Error('the per-creative table is not inside the detail cell');
    }
    expect(within(details).getByText('Variant A')).toBeTruthy();
    // The footer actions in the details keep their elements too.
    expect(within(details).getByRole('button', { name: 'Mark done' })).toBe(markDone);
  });

  it("keeps an expanded row open and shows its item's new per-creative rows", async () => {
    const section = defaultWhitePapers();
    const creatives = vi.spyOn(evidenceExtraModule, 'creativeRows');
    const show = await renderReplaceable(section);
    fireEvent.click(
      screen.getByRole('button', { name: 'Details for The 2026 B2B Pipeline Report' }),
    );
    const caption = 'Per creative: The 2026 B2B Pipeline Report';
    expect(within(table(caption)).getByText('Variant A')).toBeTruthy();
    const read = creatives.mock.calls.length;
    expect(read).toBeGreaterThanOrEqual(1);

    // The same item: its per-creative rows are not read again.
    show(sameItems(section));
    expect(creatives.mock.calls).toHaveLength(read);

    // The item's credit rows change: the open details read and show the new rows.
    show(
      withItem(section, FIXTURE_ITEM_IDS.wEur, (item) => {
        const extra = item.evidence.extra ?? {};
        const rows: unknown[] = Array.isArray(extra.rows) ? extra.rows : [];
        return {
          ...item,
          evidence: {
            ...item.evidence,
            extra: {
              ...extra,
              rows: rows.map((row) =>
                evidenceExtraModule.isRecord(row) && row.label === 'Variant A'
                  ? { ...row, label: 'Variant A (refreshed)' }
                  : row,
              ),
            },
          },
        };
      }),
    );
    expect(creatives.mock.calls.length).toBeGreaterThan(read);
    const details = table(caption);
    expect(within(details).getByText('Variant A (refreshed)')).toBeTruthy();
    expect(within(details).queryByText('Variant A')).toBeNull();
    expect(within(details).getByText('unknown creative')).toBeTruthy();
  });
});
