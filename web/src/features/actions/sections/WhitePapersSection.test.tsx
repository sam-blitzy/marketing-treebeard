/**
 * Component tests for `WhitePapersSection`, the White papers section of `/actions` (AAP 0.5.5
 * White papers; AAP 0.5.4.7 attribution chain; FR-8).
 *
 * They hold the section to its TSDoc contract and to the AAP's rendering rules:
 * - **One table per currency cohort**, captioned "White papers ({currency})", rows in the API's
 *   rank order, with the columns exactly in this order: Asset, Spend, LinkedIn leads, MQL headed
 *   with the bound stage label ("MQL (First meeting booked)", plain "MQL" while `GET /config`
 *   loads or fails), Qualified Lead, Meetings booked, Other meetings (ordering unverified), Deals
 *   after touch, Cost per Qualified Lead, Cost per meeting, Action. MQL and Qualified Lead are
 *   never blended into one column.
 * - **Rendering rules.** Floors render "≥ N" for counts and "≤ X" for costs; an unavailable
 *   count renders "unavailable", never 0; a cost with a zero denominator (QL 0, no meetings)
 *   renders "—" with its reason.
 * - **Rows expand** to their per-creative rows, "unknown creative" included, then the reason, the
 *   baselines and sample, the flags (the floor flag among them), the window chip and the footer
 *   actions; held rows sit behind the held disclosure, linked to the blocking discrepancy;
 *   historical rows carry the replaced-data badge with every action disabled.
 * - **Cohort notes**: the backend's spend total in the cohort's own currency, "counts overlap
 *   across assets", and what the LinkedIn leads and meetings booked columns count.
 *
 * The section reads `GET /config` (for the MQL label) and nothing else; any other request fails
 * the test.
 */
import { apiError, json, mockApi } from '../../../test/mockApi';
import type { MockHandler, MockResponse } from '../../../test/mockApi';

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type {
  ConfigView,
  Evidence,
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
  makeGroup,
  makeItem,
  makeItemOverlay,
  makeOverlay,
  makeRecommendationsPage,
  makeRun,
  makeSection,
} from '../../../test/fixtures';
import { renderWithProviders } from '../../../test/render';
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

/** A minimal "No change warranted" white-paper row with no reason or flag. */
function whitePaperItem(id: number, entity: { name: string; id: string }, evidence: Evidence): Item {
  return makeItem({
    id,
    rule_id: 'W',
    action: 'No change warranted',
    reason: '',
    entity: { source: 'linkedin', type: 'content_asset', ...entity },
    evidence,
    flags: [],
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
          'The 2026 B2B Pipeline Report',
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
          'The Attribution Playbook',
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

    it('renders an unavailable MQL as "unavailable" and the costs at QL 0 as "—" with their reasons', async () => {
      await renderDefault();

      expect(bodyRows(table('White papers (USD)'))).toEqual([
        [
          'Details for Paid Media Benchmarks 2026',
          'Paid Media Benchmarks 2026',
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
      expect(listEntries(details, 'Evidence for The 2026 B2B Pipeline Report')).toEqual([
        'Cohort median cost per Qualified Lead (EUR): €611.67',
        'Sample: 6 / 3 Qualified Leads',
      ]);
      expect(within(details).getByText('Floor: 12% of leads unmatched in HubSpot')).toBeTruthy();
      expect(within(details).getByText(/^1 other meeting \(ordering unverified\)/u)).toBeTruthy();
      expect(within(details).getByText(/^2026-09-08 – 2026-10-05 · 28 days/u)).toBeTruthy();
      // White-paper rows carry no decision subject: Mark done, Dismiss and Snooze only.
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(details).getByRole('button', { name }).hasAttribute('disabled')).toBe(false);
      }
      expect(within(details).queryByRole('button', { name: 'Record decision' })).toBeNull();
      expect(within(details).queryByText('Computed from replaced data')).toBeNull();
      expect(within(details).queryByRole('link', { name: 'View blocking discrepancy' })).toBeNull();

      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      expect(screen.queryByRole('table', { name: 'Per creative: The 2026 B2B Pipeline Report' })).toBeNull();
    });
  });

  describe('the MQL header', () => {
    it('heads MQL plainly while the configuration loads, then with the bound stage label', async () => {
      // The configuration answers only when the test releases it.
      const gate: { release?: (response: MockResponse) => void } = {};
      const pending = (): Promise<MockResponse> =>
        new Promise<MockResponse>((resolve) => {
          gate.release = resolve;
        });
      await renderWhitePapers(defaultWhitePapers(), pending);

      expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders('MQL'));
      await waitFor(() => {
        expect(gate.release).toBeDefined();
      });
      gate.release?.(json(200, makeConfig()));

      await waitFor(() => {
        expect(headers(table('White papers (EUR)'))).toEqual(assetHeaders(MQL_BOUND));
      });
    });

    it('heads MQL plainly when the configuration cannot be read', async () => {
      const { api } = await renderWhitePapers(
        defaultWhitePapers(),
        apiError(500, 'config_error', 'thresholds.yaml is invalid'),
      );

      await waitFor(() => {
        expect(api.calls).toHaveLength(1);
      });
      // The failed read settles without retries; the header stays plain.
      await waitFor(() => {
        expect(headers(table('White papers (USD)'))).toEqual(assetHeaders('MQL'));
      });
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
              current: makeItemOverlay({
                held_now: true,
                held_reason_now: 'Resolve discrepancy on Brand Search — US first',
              }),
            }
          : item,
      );
      await renderWhitePapers(section);

      expect(bodyRows(table('White papers (EUR)')).map((row) => row[1])).toEqual([
        'The 2026 B2B Pipeline Report',
      ]);
      const heldCaption = 'White papers (EUR) held until discrepancies are resolved';
      expect(screen.queryByRole('table', { name: heldCaption })).toBeNull();
      fireEvent.click(screen.getByRole('button', { name: 'Held until discrepancies are resolved (1)' }));
      const held = table(heldCaption);
      expect(bodyRows(held).map((row) => row[1])).toEqual(['The Attribution Playbook']);

      fireEvent.click(within(held).getByRole('button', { name: 'Details for The Attribution Playbook' }));
      expect(screen.getByText('Resolve discrepancy on Brand Search — US first')).toBeTruthy();
      expect(
        screen.getByRole('link', { name: 'View blocking discrepancy' }).getAttribute('href'),
      ).toBe('#group-campaigns-discrepancies');
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

      const disclosure = screen.getByRole('button', {
        name: 'Held until discrepancies are resolved (1)',
      });
      fireEvent.click(disclosure);
      const held = table('White papers (USD) held until discrepancies are resolved');
      const row = bodyRows(held)[0];
      expect(row?.at(-1)).toBe('Insufficient dataComputed from replaced data');

      fireEvent.click(within(held).getByRole('button', { name: 'Details for Paid Media Benchmarks 2026' }));
      const details = table('Per creative: Paid Media Benchmarks 2026').closest('td');
      if (details === null) {
        throw new Error('no detail cell');
      }
      expect(
        within(details).getByText(
          'Simulated LinkedIn data and Simulated HubSpot data, since replaced by live',
        ),
      ).toBeTruthy();
      expect(within(details).queryByRole('link', { name: 'View blocking discrepancy' })).toBeNull();
      for (const name of ['Mark done', 'Dismiss', 'Snooze for 7 days']) {
        expect(within(details).getByRole('button', { name }).hasAttribute('disabled')).toBe(true);
      }
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
      // A baseline with no metric to pair with is shown as its plain value; no sample applies.
      expect(listEntries(details, 'Evidence for orphan-asset|EUR')).toEqual([
        'Unpaired baseline: 0.5',
      ]);

      fireEvent.click(screen.getByRole('button', { name: 'Details for White paper 65' }));
      expect(screen.queryByRole('list', { name: 'Evidence for White paper 65' })).toBeNull();
    });

    it('shows the empty state when no white paper has a row to show', async () => {
      await renderWhitePapers(makeSection('white_papers'));

      const section = screen.getByRole('region', { name: 'White papers' });
      expect(within(section).queryByRole('table')).toBeNull();
      expect(within(section).getByText('No actions in this section')).toBeTruthy();
    });
  });
});
