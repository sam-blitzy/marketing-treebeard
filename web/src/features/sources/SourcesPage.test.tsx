/**
 * Component tests for `SourcesPage`, the `/sources` screen (AAP 0.5.5 "Other screens →
 * `/sources`"; AAP 0.5.3 `GET /sources`, `POST /sources/{source}/sync`; AAP 0.3.2 dataset
 * freshness; AAP 0.3.3 simulated fallback and the Google call cap).
 *
 * The page renders inside the providers of `../../test/render`, and the network is stubbed with
 * `../../test/mockApi`, so the real hooks read `GET /sources` and `GET /runs/latest` (the
 * display time zone) and the real client posts "Sync now". The cases cover, per source:
 *
 * - the status line: mode (Live, Simulated, or none yet, including a source that never went
 *   live) with its reason, connection status, the derived "Stale" badge and the last attempt in
 *   the display time zone;
 * - the Google webhook call count of the last 24 h against the product's own cap: its
 *   conservative reservation counter, an upper bound rather than the exact calls dispatched;
 * - the active primary and companion datasets, and each dataset's freshness, coverage and
 *   completeness exactly as stored;
 * - each source's native day boundary above its freshness tables and on every covered date
 *   (Google's account zone, LinkedIn and HubSpot UTC, "not yet known" without one), apart from
 *   the display time zone of instants;
 * - the recent sync runs, finished, running and failed;
 * - table sizing: every freshness table, primary and companion, at the same fixed column widths;
 *   dataset names, a stale one beside its badge included, breaking only after their separators
 *   with their text unchanged, and no cell breaking inside a word; and the sync runs' dataset
 *   column taking a quarter of the table's width;
 * - "Sync now": the POST with its CSRF header, the 202, 409 `sync_pending` and failure outcomes
 *   kept to their own section, one POST for two presses within one frame, the button disabled
 *   and described by its outcome or the pending hint while the API reports a pending request,
 *   the 202 and 409 banners cleared for good by the poll that reports the request picked up,
 *   and focus kept on a focused button, returned to the button or, while it stays disabled,
 *   moved to the outcome;
 * - the page states: loading, failure with Retry, no sources, and a failed background refresh.
 *
 * @packageDocumentation
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandler, MockHandlers, MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import type { QueryClient } from '@tanstack/react-query';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SOURCES_POLL_MS } from '../../api/queries';
import type {
  Completeness,
  ConnectionStatus,
  DataMode,
  DatasetFreshnessView,
  DatasetView,
  Source,
  SyncRequestView,
} from '../../api/types';
import { formatCount } from '../../lib/format';
import { makeRun, makeSource, makeSources } from '../../test/fixtures';
import { SourcesPage } from './SourcesPage';

// ---------------------------------------------------------------------------------------------
// Expected texts of the fixture world
// ---------------------------------------------------------------------------------------------

/** The fixture's 05:30 sync cycle (`last_attempt_at` and every freshness instant), in UTC. */
const LAST_SYNC_UTC = 'Oct 6, 2026, 5:30 AM UTC';

/** When every fixture dataset became active, in UTC. */
const ACTIVATED_UTC = 'Oct 1, 2026, 8:12 AM UTC';

/** Why every fixture source is simulated (AAP 0.3.3). */
const NO_CREDENTIALS = 'credentials not configured';

/** The label of a Google covered date: the fixture account's time zone ends Google's days. */
const GOOGLE_DAY = '(Google day, America/New_York)';

/** The label of a LinkedIn covered date: LinkedIn days are UTC (AAP 0.5.4.1). */
const LINKEDIN_DAY = '(LinkedIn day, UTC)';

/** The label of a HubSpot covered date, primary and companion: HubSpot records UTC dates. */
const HUBSPOT_DAY = '(HubSpot day, UTC)';

/** The queued request `POST /sources/{source}/sync` answers with (202). */
function syncRequest(source: Source['source']): SyncRequestView {
  return { id: 31, source, requested_at: '2026-10-06T06:01:00Z', picked_at: null };
}

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/**
 * Stubs the two reads the page makes, `GET /sources` and `GET /runs/latest`, plus `extra`.
 *
 * @param sources - the `GET /sources` answer, or a handler that builds it per request
 * @param extra - further handlers, for example the sync POST
 * @returns the mock
 */
function stubPage(sources: MockHandler = json(200, makeSources()), extra: MockHandlers = {}) {
  return mockApi({
    'GET /sources': sources,
    'GET /runs/latest': json(200, makeRun()),
    ...extra,
  });
}

/** Renders the page at `/sources` and waits for the given source's section. */
async function renderLoaded(first = 'Google') {
  const result = await renderWithProviders(<SourcesPage />, { path: '/sources' });
  await screen.findByRole('region', { name: first });
  return result;
}

/** One source's section, the region named by its level-2 heading. */
function section(label: string): HTMLElement {
  return screen.getByRole('region', { name: label });
}

/** A table by its caption, inside `scope`. */
function table(scope: HTMLElement, caption: string): HTMLElement {
  return within(scope).getByRole('table', { name: caption });
}

/** The text of every cell of every body row, row by row. */
function bodyRows(tableElement: HTMLElement): string[][] {
  return within(tableElement)
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.children, (cell) => cell.textContent ?? ''));
}

/** The column headers of a table, in order. */
function headers(tableElement: HTMLElement): string[] {
  return within(tableElement)
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent ?? '');
}

/** The status line of a section: the line that ends with the last attempt. */
function statusLine(region: HTMLElement): HTMLElement {
  const attempt = within(region).getByText(/^(Last attempt .+|No attempt yet)$/u);
  const line = attempt.parentElement;
  if (line === null) {
    throw new Error('The last-attempt text has no status line around it.');
  }
  return line;
}

/** Every badge of an element as `text (tone)`, in order. */
function badges(element: HTMLElement): string[] {
  return Array.from(element.querySelectorAll('.ds-badge'), (badge) => {
    const tone = /ds-badge--(\w+)/u.exec(badge.className)?.[1] ?? 'none';
    return `${badge.textContent ?? ''} (${tone})`;
  });
}

/** The section's "Sync now" button. */
function syncButton(label: string): HTMLButtonElement {
  return within(section(label)).getByRole<HTMLButtonElement>('button', { name: 'Sync now' });
}

/** The section's polite status region, where a "Sync now" outcome is announced. */
function outcomeRegion(label: string): HTMLElement {
  const [region] = within(section(label)).getAllByRole('status');
  if (region === undefined) {
    throw new Error(`The ${label} section has no status region.`);
  }
  return region;
}

/** The tone of the banner inside `element`, from its `ds-banner--{tone}` class. */
function bannerTone(element: HTMLElement): string | undefined {
  const banner = element.querySelector('.ds-banner');
  return banner === null ? undefined : /ds-banner--(\w+)/u.exec(banner.className)?.[1];
}

/** A copy of a source's freshness rows with the row at `index` marked stale. */
function withStaleRow(
  rows: readonly DatasetFreshnessView[],
  index: number,
): DatasetFreshnessView[] {
  return rows.map((row, at) => (at === index ? { ...row, stale: true } : { ...row }));
}

/**
 * A copy of a source's freshness rows with the row at `index` set to `completeness`, or every
 * row set to it when `index` is `'all'`. `incomplete` follows the completeness, as the API
 * derives it (`incomplete` or `unknown`).
 */
function withCompleteness(
  rows: readonly DatasetFreshnessView[],
  index: number | 'all',
  completeness: Completeness,
): DatasetFreshnessView[] {
  return rows.map((row, at) =>
    index === 'all' || at === index
      ? { ...row, completeness, incomplete: completeness !== 'complete' }
      : { ...row },
  );
}

/** A response a test releases by hand, to hold the request pending in between. */
function heldResponse(): {
  promise: Promise<MockResponse>;
  release: (response: MockResponse) => void;
} {
  let release: (response: MockResponse) => void = () => undefined;
  const promise = new Promise<MockResponse>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('SourcesPage', () => {
  describe('per-source health', () => {
    it('renders one section per source in API order, never re-sorted', async () => {
      stubPage(json(200, [makeSource('hubspot'), makeSource('google'), makeSource('linkedin')]));
      await renderLoaded('HubSpot');

      expect(screen.getByRole('heading', { level: 1, name: 'Sources' })).toBeTruthy();
      // WCAG 2.2 SC 2.4.2: the document title names the page.
      expect(document.title).toBe('Sources · Ads Action Engine');
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual(['HubSpot', 'Google', 'LinkedIn']);
      // Only the open-discrepancies banner may be an alert (AAP 0.5.5).
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('shows each simulated source with its mode, status, reason and last attempt', async () => {
      stubPage();
      await renderLoaded();

      // makeSources() answers Google, LinkedIn, HubSpot; each section is the region named by its
      // level-2 heading, so the headings give the sections' document order.
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual(['Google', 'LinkedIn', 'HubSpot']);
      expect(screen.queryAllByRole('alert')).toHaveLength(0);

      // Google's search terms are incomplete and HubSpot's Google contacts unknown in the
      // fixture world, so both also show the Incomplete badge.
      for (const [label, incomplete] of [
        ['Google', ['Incomplete (warning)']],
        ['LinkedIn', []],
        ['HubSpot', ['Incomplete (warning)']],
      ] as const) {
        const line = statusLine(section(label));
        expect(badges(line)).toEqual(['Simulated (info)', 'OK (success)', ...incomplete]);
        expect(within(line).getByText(`Reason: ${NO_CREDENTIALS}`)).toBeTruthy();
        expect(within(line).getByText(`Last attempt ${LAST_SYNC_UTC}`)).toBeTruthy();
      }
      expect(screen.getByText('Times shown in UTC')).toBeTruthy();
    });

    it.each<[DataMode, ConnectionStatus, string[]]>([
      ['live', 'ok', ['Live (success)', 'OK (success)']],
      ['live', 'stale', ['Live (success)', 'Stale (warning)']],
      ['live', 'denied', ['Live (success)', 'Access denied (critical)']],
      ['simulated', 'error', ['Simulated (info)', 'Error (critical)']],
    ])('labels mode %s with status %s', async (mode, status, expected) => {
      const reason = 'HTTP 403 from adAnalytics: missing r_ads_reporting';
      stubPage(json(200, [makeSource('linkedin', { mode, status, reason })]));
      await renderLoaded('LinkedIn');

      const line = statusLine(section('LinkedIn'));
      expect(badges(line)).toEqual(expected);
      // The reason is printed exactly as the API returns it.
      expect(within(line).getByText(`Reason: ${reason}`)).toBeTruthy();
    });

    it('says so before the worker has attempted a source and before any dataset exists', async () => {
      stubPage(
        json(200, [
          makeSource('linkedin', {
            mode: null,
            status: null,
            reason: '   ',
            active_dataset: null,
            datasets: [],
            last_attempt_at: null,
            recent_runs: [],
          }),
        ]),
      );
      await renderLoaded('LinkedIn');

      const linkedin = section('LinkedIn');
      const line = statusLine(linkedin);
      expect(badges(line)).toEqual(['No data yet (neutral)', 'Not checked yet (neutral)']);
      expect(line.textContent).toBe('No data yetNot checked yetNo attempt yet');
      expect(within(linkedin).getByText('No active dataset yet')).toBeTruthy();
      expect(
        within(linkedin).queryByRole('table', { name: 'LinkedIn active datasets' }),
      ).toBeNull();
      expect(bodyRows(table(linkedin, 'LinkedIn dataset freshness'))).toEqual([
        ['No freshness recorded yet'],
      ]);
      expect(bodyRows(table(linkedin, 'Recent sync runs (LinkedIn)'))).toEqual([
        ['No sync runs yet'],
      ]);
    });

    it('shows "No data yet", never Live, beside the error of a source that never went live', async () => {
      // A Google source whose first live query hit a defect has no active dataset, so the API
      // reports no mode beside its error and reason: there is no live data to call Live.
      const reason = 'QueryError in Q-G1: UNRECOGNIZED_FIELD';
      stubPage(
        json(200, [
          makeSource('google', {
            mode: null,
            status: 'error',
            reason,
            day_boundary: null,
            active_dataset: null,
            datasets: [],
          }),
        ]),
      );
      await renderLoaded('Google');

      const google = section('Google');
      const line = statusLine(google);
      expect(badges(line)).toEqual(['No data yet (neutral)', 'Error (critical)']);
      expect(within(line).getByText(`Reason: ${reason}`)).toBeTruthy();
      expect(within(google).queryByText('Live')).toBeNull();
      expect(within(google).getByText('No active dataset yet')).toBeTruthy();
    });

    it('adds a Stale badge when a dataset is stale while the connection reports OK', async () => {
      const linkedin = makeSource('linkedin');
      stubPage(
        json(200, [makeSource('linkedin', { datasets: withStaleRow(linkedin.datasets, 1) })]),
      );
      await renderLoaded('LinkedIn');

      const region = section('LinkedIn');
      expect(badges(statusLine(region))).toEqual([
        'Simulated (info)',
        'OK (success)',
        'Stale (warning)',
      ]);
      const rows = bodyRows(table(region, 'LinkedIn dataset freshness'));
      expect(rows.map((row) => row[0])).toEqual([
        'linkedin.entities',
        'linkedin.analytics_dailyStale',
        'linkedin.segments',
        'linkedin.leads',
      ]);
    });

    it('derives the Stale badge from the companion datasets too, and never shows it twice', async () => {
      const hubspot = makeSource('hubspot');
      stubPage(
        json(200, [
          makeSource('hubspot', {
            companion_datasets: withStaleRow(hubspot.companion_datasets, 0),
          }),
          makeSource('google', {
            status: 'stale',
            datasets: withStaleRow(makeSource('google').datasets, 1),
          }),
        ]),
      );
      await renderLoaded('HubSpot');

      // Both fixture sources also keep a dataset that is not complete.
      expect(badges(statusLine(section('HubSpot')))).toEqual([
        'Simulated (info)',
        'OK (success)',
        'Stale (warning)',
        'Incomplete (warning)',
      ]);
      // The connection already reports Stale, so no second badge is added.
      expect(badges(statusLine(section('Google')))).toEqual([
        'Simulated (info)',
        'Stale (warning)',
        'Incomplete (warning)',
      ]);
    });

    it('adds an Incomplete badge right after Stale for an incomplete Google search_terms row', async () => {
      const google = makeSource('google');
      const datasets = withStaleRow(
        withCompleteness(withCompleteness(google.datasets, 'all', 'complete'), 2, 'incomplete'),
        0,
      );
      stubPage(json(200, [makeSource('google', { datasets })]));
      await renderLoaded();

      const region = section('Google');
      // Worded and toned as the "Incomplete" source chip on /actions.
      expect(badges(statusLine(region))).toEqual([
        'Simulated (info)',
        'OK (success)',
        'Stale (warning)',
        'Incomplete (warning)',
      ]);
      const rows = bodyRows(table(region, 'Google dataset freshness'));
      expect(rows.map((row) => row.slice(0, 2))).toEqual([
        ['google.entitiesStale', 'complete'],
        ['google.metrics_daily', 'complete'],
        ['google.search_terms', 'incomplete'],
      ]);
    });

    it('shows no Incomplete badge while every dataset, primary and companion, is complete', async () => {
      const google = makeSource('google');
      const hubspot = makeSource('hubspot');
      stubPage(
        json(200, [
          makeSource('google', {
            datasets: withCompleteness(google.datasets, 'all', 'complete'),
          }),
          makeSource('hubspot', {
            datasets: withCompleteness(hubspot.datasets, 'all', 'complete'),
            companion_datasets: withCompleteness(hubspot.companion_datasets, 'all', 'complete'),
          }),
        ]),
      );
      await renderLoaded();

      for (const label of ['Google', 'HubSpot']) {
        expect(badges(statusLine(section(label)))).toEqual(['Simulated (info)', 'OK (success)']);
      }
    });

    it.each<[string, Completeness]>([
      ['incomplete', 'incomplete'],
      ['unknown', 'unknown'],
    ])(
      'derives the Incomplete badge from a companion row whose completeness is %s',
      async (_case, completeness) => {
        const hubspot = makeSource('hubspot');
        stubPage(
          json(200, [
            makeSource('hubspot', {
              datasets: withCompleteness(hubspot.datasets, 'all', 'complete'),
              companion_datasets: withCompleteness(
                withCompleteness(hubspot.companion_datasets, 'all', 'complete'),
                1,
                completeness,
              ),
            }),
          ]),
        );
        await renderLoaded('HubSpot');

        // An unknown completeness counts too, as it does for the /actions chip.
        expect(badges(statusLine(section('HubSpot')))).toEqual([
          'Simulated (info)',
          'OK (success)',
          'Incomplete (warning)',
        ]);
        const companion = table(
          section('HubSpot'),
          'HubSpot companion dataset freshness (simulated LinkedIn)',
        );
        expect(bodyRows(companion).map((row) => row.slice(0, 2))).toEqual([
          ['hubspot.contacts', 'complete'],
          ['hubspot.associations', completeness],
          ['hubspot.deal_search', 'complete'],
        ]);
      },
    );

    it('shows instants in the display time zone of the latest default run', async () => {
      mockApi({
        'GET /sources': json(200, [makeSource('google')]),
        'GET /runs/latest': json(200, makeRun({ display_timezone: 'America/New_York' })),
      });
      await renderLoaded();

      expect(await screen.findByText('Times shown in America/New_York')).toBeTruthy();
      const google = section('Google');
      expect(
        within(statusLine(google)).getByText('Last attempt Oct 6, 2026, 1:30 AM America/New_York'),
      ).toBeTruthy();
      const [entities] = bodyRows(table(google, 'Google dataset freshness'));
      expect(entities?.[4]).toBe('Oct 6, 2026, 1:30 AM America/New_York');
    });

    it('falls back to UTC, and says so, before the first default run exists', async () => {
      mockApi({
        'GET /sources': json(200, [makeSource('google')]),
        'GET /runs/latest': apiError(404, 'not_found', 'No analysis run yet'),
      });
      await renderLoaded();

      expect(
        await screen.findByText('Times shown in UTC (display time zone not yet known)'),
      ).toBeTruthy();
      expect(
        within(statusLine(section('Google'))).getByText(`Last attempt ${LAST_SYNC_UTC}`),
      ).toBeTruthy();
    });
  });

  describe('Google call budget', () => {
    it("shows Google's webhook calls of the last 24 h against the product's own cap", async () => {
      stubPage();
      await renderLoaded();

      const google = section('Google');
      expect(within(google).getByText('Google calls in the last 24 h: 112 of 1,000')).toBeTruthy();
      expect(within(google).getByText(/GOOGLE_DAILY_CALL_BUDGET/u).textContent).toBe(
        "This cap is the product's own limit on webhook calls (GOOGLE_DAILY_CALL_BUDGET), separate from the Google Ads quota shared with other n8n workflows.",
      );
      for (const label of ['LinkedIn', 'HubSpot']) {
        expect(within(section(label)).queryByText(/Google calls in the last 24 h/u)).toBeNull();
      }
    });

    it('formats the call count and the cap with thousands separators', async () => {
      stubPage(
        json(200, [
          makeSource('google', { calls_24h: 1234, call_cap: 2880 }),
          makeSource('linkedin'),
          makeSource('hubspot'),
        ]),
      );
      await renderLoaded();

      const calls = formatCount(1234);
      const cap = formatCount(2880);
      expect(
        within(section('Google')).getByText(`Google calls in the last 24 h: ${calls} of ${cap}`),
      ).toBeTruthy();
      for (const label of ['LinkedIn', 'HubSpot']) {
        expect(within(section(label)).queryByText(/Google calls/u)).toBeNull();
      }
    });

    it('shows no call line while Google reports no cap', async () => {
      stubPage(json(200, [makeSource('google', { calls_24h: null, call_cap: null })]));
      await renderLoaded();

      expect(screen.queryByText(/Google calls in the last 24 h/u)).toBeNull();
    });
  });

  describe('datasets', () => {
    it('lists the active primary dataset with its id, mode, state, activation and reason', async () => {
      stubPage();
      await renderLoaded();

      const datasets = table(section('Google'), 'Google active datasets');
      expect(headers(datasets)).toEqual(['Dataset', 'ID', 'Mode', 'State', 'Activated', 'Reason']);
      expect(bodyRows(datasets)).toEqual([
        ['Primary', '11', 'simulated', 'active', ACTIVATED_UTC, NO_CREDENTIALS],
      ]);
    });

    it('end-aligns the numeric ID, header and cells, as Rows is in the sync runs table', async () => {
      stubPage();
      await renderLoaded();

      const END = 'ds-datatable__cell--end';
      const hubspot = table(section('HubSpot'), 'HubSpot active datasets');
      const idHeader = within(hubspot).getByRole('columnheader', { name: 'ID' });
      expect(idHeader.classList.contains(END)).toBe(true);
      const idCells = within(hubspot)
        .getAllByRole('row')
        .slice(1)
        .map((row) => row.children[1]);
      expect(idCells.map((cell) => cell?.textContent)).toEqual(['13', '14']);
      expect(idCells.every((cell) => cell?.classList.contains(END) === true)).toBe(true);
      // Text columns keep the start alignment.
      const modeHeader = within(hubspot).getByRole('columnheader', { name: 'Mode' });
      expect(modeHeader.classList.contains(END)).toBe(false);
      const rowsHeader = within(table(section('HubSpot'), 'Recent sync runs (HubSpot)')).getByRole(
        'columnheader',
        { name: 'Rows' },
      );
      expect(rowsHeader.classList.contains(END)).toBe(true);
    });

    it("lists each dataset's completeness, coverage, last success, last attempt and reason as stored", async () => {
      stubPage();
      await renderLoaded();

      const freshness = table(section('Google'), 'Google dataset freshness');
      expect(headers(freshness)).toEqual([
        'Dataset',
        'Completeness',
        'Covered from',
        'Covered through',
        'Last success',
        'Last attempt',
        'Reason',
      ]);
      // Covered dates are the stored strings, labelled with Google's native day boundary.
      expect(bodyRows(freshness)).toEqual([
        ['google.entities', 'complete', '—', '—', LAST_SYNC_UTC, LAST_SYNC_UTC, '—'],
        [
          'google.metrics_daily',
          'complete',
          `2026-04-09 ${GOOGLE_DAY}`,
          `2026-10-05 ${GOOGLE_DAY}`,
          LAST_SYNC_UTC,
          LAST_SYNC_UTC,
          '—',
        ],
        [
          'google.search_terms',
          'incomplete',
          `2026-04-09 ${GOOGLE_DAY}`,
          `2026-10-05 ${GOOGLE_DAY}`,
          LAST_SYNC_UTC,
          LAST_SYNC_UTC,
          'result limit reached at finest partition',
        ],
      ]);
      expect(badges(freshness)).toEqual([
        'complete (success)',
        'complete (success)',
        'incomplete (warning)',
      ]);
    });

    it('keeps the HubSpot companion CRM dataset apart from the primary one', async () => {
      stubPage();
      await renderLoaded();

      const hubspot = section('HubSpot');
      expect(bodyRows(table(hubspot, 'HubSpot active datasets'))).toEqual([
        ['Primary', '13', 'simulated', 'active', ACTIVATED_UTC, NO_CREDENTIALS],
        [
          'Companion (simulated LinkedIn)',
          '14',
          'simulated',
          'active',
          ACTIVATED_UTC,
          'companion of simulated LinkedIn dataset 12',
        ],
      ]);
      const primary = bodyRows(table(hubspot, 'HubSpot dataset freshness'));
      expect(primary.map((row) => row[0])).toEqual([
        'hubspot.contacts',
        'hubspot.associations',
        'hubspot.deal_search',
        'hubspot.google_contacts',
      ]);
      // Never refreshed: no instant, no coverage, and the reason as recorded.
      expect(primary[3]).toEqual([
        'hubspot.google_contacts',
        'unknown',
        '—',
        '—',
        'Never',
        'Never',
        'Google-to-CRM link not configured',
      ]);
      const companion = bodyRows(
        table(hubspot, 'HubSpot companion dataset freshness (simulated LinkedIn)'),
      );
      expect(companion.map((row) => row[0])).toEqual([
        'hubspot.contacts',
        'hubspot.associations',
        'hubspot.deal_search',
      ]);
      // LinkedIn has no companion.
      expect(
        within(section('LinkedIn')).queryByRole('table', { name: /companion dataset freshness/u }),
      ).toBeNull();
    });

    it('gives every freshness table, primary and companion, the same fixed column widths', async () => {
      stubPage();
      await renderLoaded();

      const hubspot = section('HubSpot');
      const freshnessTables = [
        table(section('Google'), 'Google dataset freshness'),
        table(section('LinkedIn'), 'LinkedIn dataset freshness'),
        table(hubspot, 'HubSpot dataset freshness'),
        table(hubspot, 'HubSpot companion dataset freshness (simulated LinkedIn)'),
      ];
      // The design-system table lays a table out at fixed widths from one col per column.
      const widths = freshnessTables.map((element) =>
        Array.from(element.querySelectorAll<HTMLElement>('colgroup > col'), (col) =>
          col.style.getPropertyValue('--ds-datatable-col-steps'),
        ),
      );
      const expected = ['13', '8', '7', '7', '7', '7', '12'];
      expect(widths).toEqual(freshnessTables.map(() => expected));
      expect(
        freshnessTables.every((element) => element.classList.contains('ds-datatable--fixed')),
      ).toBe(true);
      // The other tables keep the automatic layout: the active datasets size their columns from
      // their content, the sync runs from their content and their dataset column's share.
      expect(table(hubspot, 'HubSpot active datasets').querySelector('colgroup')).toBeNull();
      const syncRuns = table(hubspot, 'Recent sync runs (HubSpot)');
      expect(syncRuns.classList.contains('ds-datatable--fixed')).toBe(false);
      expect(syncRuns.querySelector('col.ds-datatable__col')).toBeNull();
    });

    it('shows a dataset that is not active yet as not activated, without a reason', async () => {
      const building: DatasetView = {
        id: 21,
        mode: 'live',
        role: 'primary',
        state: 'building',
        reason: null,
        created_at: '2026-10-06T05:00:00Z',
        activated_at: null,
      };
      stubPage(json(200, [makeSource('google', { mode: 'live', active_dataset: building })]));
      await renderLoaded();

      expect(bodyRows(table(section('Google'), 'Google active datasets'))).toEqual([
        ['Primary', '21', 'live', 'building', 'Not yet activated', '—'],
      ]);
    });
  });

  describe('native day boundaries', () => {
    /** The "Dataset freshness" subsection of a source's section, named by its level-3 heading. */
    function freshnessSection(label: string): HTMLElement {
      return within(section(label)).getByRole('region', { name: 'Dataset freshness' });
    }

    /** The Covered from and Covered through cells of every body row of a table. */
    function coveredCells(tableElement: HTMLElement): string[][] {
      return bodyRows(tableElement).map((row) => row.slice(2, 4));
    }

    /** Whether `first` comes before `second` in document order. */
    function precedes(first: Node, second: Node): boolean {
      return (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
    }

    it('opens each freshness subsection with its own source boundary, before the tables', async () => {
      stubPage();
      await renderLoaded();

      for (const [label, line] of [
        ['Google', 'Google days: America/New_York'],
        ['LinkedIn', 'LinkedIn days: UTC'],
        ['HubSpot', 'HubSpot days: UTC'],
      ] as const) {
        const freshness = freshnessSection(label);
        const boundary = within(freshness).getByText(line);
        const [firstTable] = within(freshness).getAllByRole('table');
        if (firstTable === undefined) {
          throw new Error(`The ${label} freshness subsection has no table.`);
        }
        expect(precedes(boundary, firstTable)).toBe(true);
        // One boundary line per source, and none borrowed from another source.
        expect(within(section(label)).getAllByText(/ days: /u)).toEqual([boundary]);
      }
    });

    it("labels every covered date with its source's boundary, the companion's with HubSpot's", async () => {
      stubPage();
      await renderLoaded();

      expect(coveredCells(table(section('LinkedIn'), 'LinkedIn dataset freshness'))).toEqual([
        ['—', '—'],
        [`2026-04-09 ${LINKEDIN_DAY}`, `2026-10-05 ${LINKEDIN_DAY}`],
        [`2026-04-09 ${LINKEDIN_DAY}`, `2026-10-05 ${LINKEDIN_DAY}`],
        [`2026-04-09 ${LINKEDIN_DAY}`, `2026-10-05 ${LINKEDIN_DAY}`],
      ]);
      const hubspot = section('HubSpot');
      expect(coveredCells(table(hubspot, 'HubSpot dataset freshness'))).toEqual([
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
        // Never refreshed: no coverage, so nothing to label.
        ['—', '—'],
      ]);
      expect(
        coveredCells(table(hubspot, 'HubSpot companion dataset freshness (simulated LinkedIn)')),
      ).toEqual([
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
        [`2026-04-09 ${HUBSPOT_DAY}`, `2026-10-05 ${HUBSPOT_DAY}`],
      ]);
    });

    it('keeps the display time zone of instants apart from the day boundaries', async () => {
      mockApi({
        'GET /sources': json(200, makeSources()),
        'GET /runs/latest': json(200, makeRun({ display_timezone: 'Europe/Berlin' })),
      });
      await renderLoaded();

      const zoneLine = await screen.findByText('Times shown in Europe/Berlin');
      // The instants line names no day boundary and sits outside every source section.
      expect(zoneLine.closest('section')).toBeNull();
      expect(zoneLine.textContent).toBe('Times shown in Europe/Berlin');
      // DISPLAY_TIMEZONE formats instants only: it never moves or relabels a native day.
      const google = freshnessSection('Google');
      expect(within(google).getByText('Google days: America/New_York')).toBeTruthy();
      expect(within(freshnessSection('LinkedIn')).getByText('LinkedIn days: UTC')).toBeTruthy();
      expect(screen.queryByText(/days: Europe\/Berlin/u)).toBeNull();
      const [, metrics] = bodyRows(table(section('Google'), 'Google dataset freshness'));
      expect(metrics?.slice(2, 5)).toEqual([
        `2026-04-09 ${GOOGLE_DAY}`,
        `2026-10-05 ${GOOGLE_DAY}`,
        'Oct 6, 2026, 7:30 AM Europe/Berlin',
      ]);
    });

    it('lets a zone wrap after its slash in the fixed-width columns, its text unchanged', async () => {
      mockApi({
        'GET /sources': json(200, makeSources()),
        'GET /runs/latest': json(200, makeRun({ display_timezone: 'Europe/Berlin' })),
      });
      await renderLoaded();
      await screen.findByText('Times shown in Europe/Berlin');

      const metrics = within(table(section('Google'), 'Google dataset freshness')).getAllByRole(
        'row',
      )[2];
      const cells = Array.from(metrics?.children ?? []);
      // Covered from, Covered through, Last success and Last attempt.
      for (const cell of cells.slice(2, 6)) {
        const breaks = cell.querySelectorAll('wbr');
        expect(breaks).toHaveLength(1);
        expect(breaks[0]?.previousSibling?.textContent?.endsWith('/')).toBe(true);
      }
      expect(cells.slice(2, 6).map((cell) => cell.textContent)).toEqual([
        `2026-04-09 ${GOOGLE_DAY}`,
        `2026-10-05 ${GOOGLE_DAY}`,
        'Oct 6, 2026, 7:30 AM Europe/Berlin',
        'Oct 6, 2026, 7:30 AM Europe/Berlin',
      ]);
      // A text without a slash is rendered as given.
      const linkedinTable = table(section('LinkedIn'), 'LinkedIn dataset freshness');
      const linkedinCovered = within(linkedinTable).getAllByRole('row')[2]?.children[2];
      expect(linkedinCovered?.querySelector('wbr')).toBeNull();
      expect(linkedinCovered?.textContent).toBe(`2026-04-09 ${LINKEDIN_DAY}`);
    });

    it.each<[string, string | null]>([
      ['null', null],
      ['blank', '   '],
    ])(
      'says the Google boundary is not yet known when the API reports it %s, inventing no zone',
      async (_case, dayBoundary) => {
        stubPage(
          json(200, [makeSource('google', { day_boundary: dayBoundary }), makeSource('linkedin')]),
        );
        await renderLoaded();

        expect(await screen.findByText('Times shown in UTC')).toBeTruthy();
        const freshness = freshnessSection('Google');
        expect(within(freshness).getByText('Google days: day boundary not yet known')).toBeTruthy();
        expect(coveredCells(table(section('Google'), 'Google dataset freshness'))).toEqual([
          ['—', '—'],
          ['2026-04-09 (Google day)', '2026-10-05 (Google day)'],
          ['2026-04-09 (Google day)', '2026-10-05 (Google day)'],
        ]);
        // Neither the display zone (UTC here) nor LinkedIn's boundary stands in for Google's.
        expect(within(section('Google')).queryByText(/Google day, /u)).toBeNull();
        expect(within(freshness).queryByText(/Google days: UTC/u)).toBeNull();
        expect(within(freshnessSection('LinkedIn')).getByText('LinkedIn days: UTC')).toBeTruthy();
      },
    );
  });

  describe('recent sync runs', () => {
    it('lists the newest runs first with job, dataset, times, status, rows and completeness', async () => {
      stubPage();
      await renderLoaded();

      const runs = table(section('Google'), 'Recent sync runs (Google)');
      expect(headers(runs)).toEqual([
        'Job',
        'Dataset',
        'Started',
        'Finished',
        'Status',
        'Rows',
        'Completeness',
        'Error',
      ]);
      expect(bodyRows(runs)).toEqual([
        [
          'metrics',
          'google.metrics_daily',
          LAST_SYNC_UTC,
          'Oct 6, 2026, 5:31 AM UTC',
          'succeeded',
          '4,210',
          'complete',
          '—',
        ],
        [
          'entities',
          'google.entities',
          'Oct 6, 2026, 3:00 AM UTC',
          'Oct 6, 2026, 3:00 AM UTC',
          'succeeded',
          '186',
          'complete',
          '—',
        ],
      ]);
      // A run over several datasets names none.
      const hubspotRuns = bodyRows(table(section('HubSpot'), 'Recent sync runs (HubSpot)'));
      expect(hubspotRuns.map((row) => row.slice(0, 2))).toEqual([
        ['crm', '—'],
        ['lead_match', '—'],
      ]);
    });

    it('shows a running run as Running and a failed run with its error', async () => {
      stubPage(
        json(200, [
          makeSource('google', {
            recent_runs: [
              {
                id: 9010,
                job: 'search_terms',
                dataset: 'google.search_terms',
                started_at: '2026-10-06T06:00:00Z',
                finished_at: null,
                status: 'running',
                rows: null,
                completeness: null,
                error: null,
              },
              {
                id: 9009,
                job: 'metrics',
                dataset: 'google.metrics_daily',
                started_at: '2026-10-06T05:00:00Z',
                finished_at: '2026-10-06T05:00:09Z',
                status: 'failed',
                rows: 0,
                completeness: 'incomplete',
                error: 'n8n webhook answered HTTP 502',
              },
            ],
          }),
        ]),
      );
      await renderLoaded();

      expect(bodyRows(table(section('Google'), 'Recent sync runs (Google)'))).toEqual([
        [
          'search_terms',
          'google.search_terms',
          'Oct 6, 2026, 6:00 AM UTC',
          'Running',
          'running',
          'unavailable',
          '—',
          '—',
        ],
        [
          'metrics',
          'google.metrics_daily',
          'Oct 6, 2026, 5:00 AM UTC',
          'Oct 6, 2026, 5:00 AM UTC',
          'failed',
          '0',
          'incomplete',
          'n8n webhook answered HTTP 502',
        ],
      ]);
    });
  });

  describe('table sizing', () => {
    it('breaks dataset names only after their separators and never inside a word', async () => {
      const google = makeSource('google', {
        datasets: withStaleRow(makeSource('google').datasets, 1),
      });
      stubPage(json(200, [google, makeSource('linkedin'), makeSource('hubspot')]));
      await renderLoaded();

      const region = section('Google');
      // A sync run's dataset name wraps after its dot and underscore, with its text unchanged.
      const runs = table(region, 'Recent sync runs (Google)');
      const runDataset = within(runs).getAllByRole('row')[1]?.children[1];
      expect(runDataset?.textContent).toBe('google.metrics_daily');
      expect(runDataset?.innerHTML).toBe('<span>google.<wbr>metrics_<wbr>daily</span>');
      // So does a freshness row's name, beside its Stale badge too.
      const freshness = table(region, 'Google dataset freshness');
      expect(freshness.classList.contains('ds-datatable--fixed')).toBe(true);
      const [, stale, plain] = within(freshness).getAllByRole('rowheader');
      expect(stale?.textContent).toBe('google.metrics_dailyStale');
      expect(stale?.querySelector('span')?.innerHTML).toBe(
        '<span>google.<wbr>metrics_<wbr>daily</span>',
      );
      expect(plain?.innerHTML).toBe('<span>google.<wbr>search_<wbr>terms</span>');
      // A short name gets no break opportunity, every one in the section follows a separator,
      // and no cell opts into breaking inside a word.
      expect(within(freshness).getAllByRole('rowheader')[0]?.innerHTML).toBe('google.entities');
      for (const cell of region.querySelectorAll('td, th')) {
        expect(cell.className).not.toMatch(/anywhere/u);
        for (const wbr of cell.querySelectorAll('wbr')) {
          expect(wbr.previousSibling?.textContent).toMatch(/[._:=/]$/u);
        }
      }
    });

    /**
     * Each column's header text paired with its share of the table's width in twelfths, the
     * DataTable `share` prop that renders as a `ds-datatable__col--share-{n}` class on the
     * column's `<col>`, or `null` for a column without one, in column order.
     */
    function columnShares(tableElement: HTMLElement): [string, number | null][] {
      const cols = Array.from(tableElement.querySelectorAll('colgroup > col'));
      return headers(tableElement).map((header, index): [string, number | null] => {
        const share = /^ds-datatable__col--share-(\d+)$/u.exec(cols[index]?.className ?? '');
        return [header, share === null ? null : Number(share[1])];
      });
    }

    it("gives the sync runs' dataset column a quarter of the table, so names stay whole", async () => {
      stubPage();
      await renderLoaded();

      const syncRuns: [string, number | null][] = [
        ['Job', null],
        ['Dataset', 3],
        ['Started', null],
        ['Finished', null],
        ['Status', null],
        ['Rows', null],
        ['Completeness', null],
        ['Error', null],
      ];
      for (const label of ['Google', 'LinkedIn', 'HubSpot'] as const) {
        const region = section(label);
        expect(columnShares(table(region, `Recent sync runs (${label})`))).toEqual(syncRuns);
        // The freshness tables take fixed widths instead, which no share joins.
        expect(
          table(region, `${label} dataset freshness`).querySelector('[class*="col--share-"]'),
        ).toBeNull();
        // The active-datasets table holds no dataset name, so it keeps its content-sized columns.
        expect(table(region, `${label} active datasets`).querySelector('colgroup')).toBeNull();
      }
    });
  });

  describe('Sync now', () => {
    /** How many times the page has read `GET /sources` so far. */
    function sourceReads(api: ApiMock): number {
      return api.calls.filter((call) => call.method === 'GET' && call.path === '/sources').length;
    }

    /**
     * Lets one poll interval of `useSources` pass under fake timers, then waits until the read
     * it starts has been sent and every read has settled, so the page shows the poll's answer.
     */
    async function nextPoll(api: ApiMock, queryClient: QueryClient): Promise<void> {
      const readsBefore = sourceReads(api);
      await act(() => vi.advanceTimersByTimeAsync(SOURCES_POLL_MS));
      await waitFor(() => {
        expect(sourceReads(api)).toBeGreaterThan(readsBefore);
        expect(queryClient.isFetching()).toBe(0);
      });
      // TanStack Query notifies its observers on a zero-delay timer, and the reset an answer
      // can cause notifies on another, so two timer turns render all the answer changes.
      await act(() => vi.advanceTimersByTimeAsync(0));
      await act(() => vi.advanceTimersByTimeAsync(0));
    }

    it('posts the sync request with the CSRF header and confirms it in its own section', async () => {
      const api = stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': (call) =>
          json(202, syncRequest(call.params.source as Source['source'])),
      });
      await renderLoaded();
      const readsBefore = api.calls.filter((call) => call.path === '/sources').length;

      fireEvent.click(syncButton('Google'));

      const outcome = outcomeRegion('Google');
      expect(await within(outcome).findByText('Sync requested')).toBeTruthy();
      expect(
        within(outcome).getByText('The worker picks it up on its next scheduled wake.'),
      ).toBeTruthy();
      const posts = api.calls.filter((call) => call.method === 'POST');
      expect(posts).toHaveLength(1);
      expect(posts[0]?.path).toBe('/sources/google/sync');
      expect(posts[0]?.headers.get('X-Requested-With')).toBe('ads-engine');
      expect(posts[0]?.body).toBeUndefined();
      // The sources are read again, so the pending request shows.
      await waitFor(() => {
        expect(api.calls.filter((call) => call.path === '/sources').length).toBeGreaterThan(
          readsBefore,
        );
      });
      // The outcome never leaks into another source's section.
      expect(outcomeRegion('LinkedIn').textContent).toBe('');
      expect(outcomeRegion('HubSpot').textContent).toBe('');
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('keeps the outcome and disables the button, described by it, while the request is pending', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        let requested = false;
        let picked = false;
        // After the POST, the API reports the queued request (and a newer attempt, so the
        // re-read is visible on screen) until the worker picks it up.
        const api = stubPage(
          () =>
            json(200, [
              makeSource(
                'google',
                requested
                  ? { sync_pending: !picked, last_attempt_at: '2026-10-06T06:01:00Z' }
                  : {},
              ),
              makeSource('linkedin'),
            ]),
          {
            'POST /sources/:source/sync': () => {
              requested = true;
              return json(202, syncRequest('google'));
            },
          },
        );
        const { queryClient } = await renderLoaded();

        fireEvent.click(syncButton('Google'));
        await within(outcomeRegion('Google')).findByText('Sync requested');
        expect(
          await within(section('Google')).findByText('Last attempt Oct 6, 2026, 6:01 AM UTC'),
        ).toBeTruthy();

        // One pending request per source (AAP 0.5.3): another press could only answer 409, so
        // the button stays disabled. The banner is the visible text that says why; no hint
        // repeats it.
        const button = syncButton('Google');
        expect(button.disabled).toBe(true);
        const reasonId = button.getAttribute('aria-describedby');
        expect(reasonId).toBe('source-google-sync-outcome');
        const reason = document.getElementById(reasonId ?? '');
        expect(reason !== null && outcomeRegion('Google').contains(reason)).toBe(true);
        expect(reason?.textContent).toBe(
          'Sync requestedThe worker picks it up on its next scheduled wake.',
        );
        expect(within(outcomeRegion('Google')).queryByText('A sync is already pending')).toBeNull();

        // A press on the disabled button sends nothing, so no 409 can follow.
        fireEvent.click(button);
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
        expect(syncButton('LinkedIn').disabled).toBe(false);

        // The worker claims the request: the next poll no longer reports it pending, so the
        // button is enabled again, with no description, and the banner is cleared.
        picked = true;
        await nextPoll(api, queryClient);
        expect(syncButton('Google').disabled).toBe(false);
        expect(syncButton('Google').getAttribute('aria-describedby')).toBeNull();
        expect(outcomeRegion('Google').childNodes).toHaveLength(0);
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('clears "Sync requested" for good on the poll that reports the request picked up', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        let pending = false;
        const api = stubPage(
          () =>
            json(200, [makeSource('google', { sync_pending: pending }), makeSource('linkedin')]),
          {
            'POST /sources/:source/sync': () => {
              pending = true;
              return json(202, syncRequest('google'));
            },
          },
        );
        const { queryClient } = await renderLoaded();

        fireEvent.click(syncButton('Google'));
        const outcome = outcomeRegion('Google');
        expect(await within(outcome).findByText('Sync requested')).toBeTruthy();
        expect(syncButton('Google').disabled).toBe(true);

        // A poll while the request is still queued keeps the banner and the button disabled.
        await nextPoll(api, queryClient);
        expect(within(outcome).getByText('Sync requested')).toBeTruthy();
        expect(syncButton('Google').disabled).toBe(true);

        // The worker picks the request up: the next poll clears the banner and enables Sync now.
        pending = false;
        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
        expect(syncButton('Google').disabled).toBe(false);
        expect(syncButton('Google').getAttribute('aria-describedby')).toBeNull();

        // A later request from elsewhere makes the source pending again: the hint says so, and
        // the cleared banner does not come back.
        pending = true;
        await nextPoll(api, queryClient);
        expect(syncButton('Google').disabled).toBe(true);
        expect(syncButton('Google').getAttribute('aria-describedby')).toBe(
          'source-google-sync-pending',
        );
        expect(within(outcome).getByText('A sync is already pending')).toBeTruthy();
        expect(within(outcome).queryByText('Sync requested')).toBeNull();
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('keeps "Sync requested" until a poll after the press, even when no read saw it pending', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        // The worker picks the request up before the read the 202 starts, so every answer
        // reports nothing pending.
        const api = stubPage(json(200, makeSources()), {
          'POST /sources/:source/sync': json(202, syncRequest('google')),
        });
        const { queryClient } = await renderLoaded();

        fireEvent.click(syncButton('Google'));
        const outcome = outcomeRegion('Google');
        expect(await within(outcome).findByText('Sync requested')).toBeTruthy();
        expect(syncButton('Google').disabled).toBe(false);

        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
      } finally {
        vi.useRealTimers();
      }
    });

    it('posts once when it is pressed twice within one frame, and again on a later press', async () => {
      const api = stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': (call) =>
          json(202, syncRequest(call.params.source as Source['source'])),
      });
      await renderLoaded();

      // Back to back, as a double click or a script's `click(); click();` delivers them, before
      // React renders the first press as loading.
      fireEvent.click(syncButton('Google'));
      fireEvent.click(syncButton('Google'));

      expect(await within(outcomeRegion('Google')).findByText('Sync requested')).toBeTruthy();
      await waitFor(() => {
        expect(syncButton('Google').getAttribute('aria-busy')).toBeNull();
      });
      expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
      expect(within(outcomeRegion('Google')).queryByText('A sync is already pending')).toBeNull();

      // The stub never reports the request pending, so a deliberate press later posts again.
      expect(syncButton('Google').disabled).toBe(false);
      fireEvent.click(syncButton('Google'));
      await waitFor(() => {
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(2);
      });
    });

    it('frees the button after a failed request, so a retry posts again', async () => {
      const api = stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': apiError(503, 'service_unavailable', 'Database unavailable'),
      });
      await renderLoaded();

      fireEvent.click(syncButton('Google'));
      fireEvent.click(syncButton('Google'));

      expect(await within(outcomeRegion('Google')).findByText('Sync request failed')).toBeTruthy();
      await waitFor(() => {
        expect(syncButton('Google').disabled).toBe(false);
      });
      expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);

      api.use({
        'POST /sources/:source/sync': json(202, syncRequest('google')),
      });
      fireEvent.click(syncButton('Google'));

      expect(await within(outcomeRegion('Google')).findByText('Sync requested')).toBeTruthy();
      expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(2);
    });

    it('moves focus to the status region, not the heading, when the settled press leaves the button disabled', async () => {
      const response = heldResponse();
      let requested = false;
      stubPage(
        () =>
          json(200, [makeSource('google', { sync_pending: requested }), makeSource('linkedin')]),
        {
          'POST /sources/:source/sync': () => {
            requested = true;
            return response.promise;
          },
        },
      );
      await renderLoaded();
      const button = syncButton('Google');

      fireEvent.click(button);
      await waitFor(() => {
        expect(button.getAttribute('aria-busy')).toBe('true');
      });
      // fireEvent does not focus the button, so focus rests on the page body while the request
      // runs, as if it had been lost there.
      expect(document.activeElement).toBe(document.body);
      response.release(json(202, syncRequest('google')));

      // The status region, which holds the outcome that says why the button is disabled, takes
      // focus by script; the heading is left as it is, never made focusable.
      const outcome = outcomeRegion('Google');
      await waitFor(() => {
        expect(document.activeElement).toBe(outcome);
      });
      expect(outcome.tabIndex).toBe(-1);
      expect(within(outcome).getByText('Sync requested')).toBeTruthy();
      const heading = within(section('Google')).getByRole('heading', { level: 2, name: 'Google' });
      expect(heading.hasAttribute('tabindex')).toBe(false);
      expect(button.disabled).toBe(true);
    });

    it('explains a disabled button with the hint when a failure is shown and a request is pending', async () => {
      const api = stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': apiError(503, 'service_unavailable', 'Database unavailable'),
      });
      const { queryClient } = await renderLoaded();

      fireEvent.click(syncButton('Google'));
      expect(await within(outcomeRegion('Google')).findByText('Sync request failed')).toBeTruthy();

      // Another session's request is queued meanwhile; the next read reports it.
      api.use({
        'GET /sources': json(200, [makeSource('google', { sync_pending: true })]),
      });
      await act(async () => {
        await queryClient.refetchQueries({ type: 'active' });
      });

      await waitFor(() => {
        expect(syncButton('Google').disabled).toBe(true);
      });
      const hintId = syncButton('Google').getAttribute('aria-describedby');
      expect(hintId).toBe('source-google-sync-pending');
      expect(document.getElementById(hintId ?? '')?.textContent).toBe('A sync is already pending');
      expect(within(outcomeRegion('Google')).getByText('Sync request failed')).toBeTruthy();
    });

    it('is busy while the request is in flight', async () => {
      const response = heldResponse();
      stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': () => response.promise,
      });
      await renderLoaded();

      fireEvent.click(syncButton('LinkedIn'));

      await waitFor(() => {
        expect(syncButton('LinkedIn').getAttribute('aria-busy')).toBe('true');
      });
      // Inert while busy, but never natively disabled, so a focused button keeps focus.
      expect(syncButton('LinkedIn').getAttribute('aria-disabled')).toBe('true');
      expect(syncButton('LinkedIn').disabled).toBe(false);
      expect(syncButton('Google').disabled).toBe(false);
      expect(outcomeRegion('LinkedIn').textContent).toBe('');

      response.release(json(202, syncRequest('linkedin')));

      expect(await within(outcomeRegion('LinkedIn')).findByText('Sync requested')).toBeTruthy();
      expect(syncButton('LinkedIn').getAttribute('aria-busy')).toBeNull();
    });

    it('returns focus to Sync now once the request it started has settled', async () => {
      const response = heldResponse();
      stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': () => response.promise,
      });
      await renderLoaded();
      const button = syncButton('Google');

      fireEvent.click(button);
      await waitFor(() => {
        expect(button.getAttribute('aria-busy')).toBe('true');
      });
      // fireEvent does not focus the button, so focus rests on the page body while the request
      // runs, as if it had been lost there.
      expect(document.activeElement).toBe(document.body);
      response.release(json(202, syncRequest('google')));

      await waitFor(() => {
        expect(document.activeElement).toBe(button);
      });
      expect(button.disabled).toBe(false);
    });

    it('leaves focus where the user moved it while the request ran', async () => {
      const response = heldResponse();
      stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': () => response.promise,
      });
      await renderLoaded();
      const google = syncButton('Google');

      fireEvent.click(google);
      await waitFor(() => {
        expect(google.getAttribute('aria-busy')).toBe('true');
      });
      const hubspot = syncButton('HubSpot');
      // The design-system Button tracks its own focus in state, so focusing it renders.
      act(() => {
        hubspot.focus();
      });
      response.release(json(202, syncRequest('google')));

      expect(await within(outcomeRegion('Google')).findByText('Sync requested')).toBeTruthy();
      expect(document.activeElement).toBe(hubspot);
    });

    /**
     * Presses Google "Sync now" with its 202 held, lets the API queue the request so the read the
     * 202 starts reports it pending, releases the 202 and waits until focus has moved to the
     * Google status region, because the button stays disabled.
     *
     * @returns the mock, the query client, the button, the status region and `pickUp`, after
     *   which every read reports the request picked up
     */
    async function pressIntoPendingGoogleSync(): Promise<{
      api: ApiMock;
      queryClient: QueryClient;
      button: HTMLButtonElement;
      outcome: HTMLElement;
      pickUp: () => void;
    }> {
      const response = heldResponse();
      let pending = false;
      const api = stubPage(
        () => json(200, [makeSource('google', { sync_pending: pending }), makeSource('linkedin')]),
        { 'POST /sources/:source/sync': () => response.promise },
      );
      const { queryClient } = await renderLoaded();
      const button = syncButton('Google');

      fireEvent.click(button);
      await waitFor(() => {
        expect(button.getAttribute('aria-busy')).toBe('true');
      });
      // fireEvent does not focus the button, so focus rests on the page body while the request
      // runs, as if it had been lost there.
      expect(document.activeElement).toBe(document.body);
      pending = true;
      response.release(json(202, syncRequest('google')));

      const outcome = outcomeRegion('Google');
      await waitFor(() => {
        expect(document.activeElement).toBe(outcome);
      });
      return {
        api,
        queryClient,
        button,
        outcome,
        pickUp: () => {
          pending = false;
        },
      };
    }

    it('moves focus to the outcome while Sync now stays disabled, then back to the button', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { api, queryClient, button, outcome, pickUp } = await pressIntoPendingGoogleSync();

        // The button stays disabled, so the status region that says why holds focus.
        expect(within(outcome).getByText('Sync requested')).toBeTruthy();
        expect(button.disabled).toBe(true);
        expect(outcome.tabIndex).toBe(-1);

        // A poll while the request is still queued moves nothing.
        await nextPoll(api, queryClient);
        expect(document.activeElement).toBe(outcome);

        // The poll that reports the request picked up empties the region and enables the
        // button, so focus moves on to the button instead of resting on an empty region.
        pickUp();
        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
        expect(button.disabled).toBe(false);
        expect(document.activeElement).toBe(button);
      } finally {
        vi.useRealTimers();
      }
    });

    it('leaves focus the user moved while the request waited for pickup', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { api, queryClient, button, outcome, pickUp } = await pressIntoPendingGoogleSync();
        const linkedin = syncButton('LinkedIn');
        act(() => {
          linkedin.focus();
        });

        pickUp();
        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
        expect(button.disabled).toBe(false);
        expect(document.activeElement).toBe(linkedin);
      } finally {
        vi.useRealTimers();
      }
    });

    it('keeps focus on a focused Sync now the settled press leaves disabled, through the pickup', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const response = heldResponse();
        let pending = false;
        const api = stubPage(
          () => json(200, [makeSource('google', { sync_pending: pending }), makeSource('linkedin')]),
          { 'POST /sources/:source/sync': () => response.promise },
        );
        const { queryClient } = await renderLoaded();
        const button = syncButton('Google');
        const outcome = outcomeRegion('Google');

        // A keyboard press: the button holds focus, and keeps it while it loads.
        act(() => {
          button.focus();
        });
        fireEvent.click(button);
        await waitFor(() => {
          expect(button.getAttribute('aria-busy')).toBe('true');
        });
        expect(document.activeElement).toBe(button);
        pending = true;
        response.release(json(202, syncRequest('google')));

        // Settled with the request pending: the design-system Button keeps the focused button
        // focusable as `aria-disabled` rather than natively disabled, so focus stays on it,
        // described by the outcome, and a press on it sends nothing.
        expect(await within(outcome).findByText('Sync requested')).toBeTruthy();
        await waitFor(() => {
          expect(button.getAttribute('aria-busy')).toBeNull();
        });
        expect(document.activeElement).toBe(button);
        expect(button.disabled).toBe(false);
        expect(button.getAttribute('aria-disabled')).toBe('true');
        expect(button.getAttribute('aria-describedby')).toBe('source-google-sync-outcome');
        fireEvent.click(button);
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);

        // The pickup clears the banner and enables the button where focus already rests.
        pending = false;
        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
        expect(document.activeElement).toBe(button);
        expect(button.getAttribute('aria-disabled')).toBeNull();
        expect(button.getAttribute('aria-describedby')).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('says a sync is already pending for 409 sync_pending, reads the sources and disables the button', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        // Another session queued a request after this page's last read, so the page still
        // shows HubSpot with nothing pending when the 409 answers.
        let pending = false;
        const api = stubPage(
          () => json(200, [makeSource('google'), makeSource('hubspot', { sync_pending: pending })]),
          {
            'POST /sources/:source/sync': () => {
              pending = true;
              return apiError(409, 'sync_pending', 'A sync request for hubspot is already pending');
            },
          },
        );
        const { queryClient } = await renderLoaded();
        const readsBefore = sourceReads(api);

        fireEvent.click(syncButton('HubSpot'));

        const outcome = outcomeRegion('HubSpot');
        expect(await within(outcome).findByText('A sync is already pending')).toBeTruthy();
        expect(bannerTone(outcome)).toBe('info');
        expect(within(outcome).queryByText('Sync requested')).toBeNull();
        // The 409 reads the sources again, so the button learns of the pending request.
        expect(sourceReads(api)).toBeGreaterThan(readsBefore);
        const button = syncButton('HubSpot');
        expect(button.disabled).toBe(true);
        expect(button.getAttribute('aria-describedby')).toBe('source-hubspot-sync-outcome');
        // Said once: the banner, with no hint repeating it.
        expect(within(section('HubSpot')).getAllByText('A sync is already pending')).toHaveLength(
          1,
        );

        // The worker picks the request up: the next poll clears the banner and enables Sync now.
        pending = false;
        await nextPoll(api, queryClient);
        expect(outcome.childNodes).toHaveLength(0);
        expect(syncButton('HubSpot').disabled).toBe(false);
        expect(api.calls.filter((call) => call.method === 'POST')).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('disables the button after a 409 once a read reports the pending request, saying it once', async () => {
      let pending = false;
      stubPage(
        () =>
          json(200, [makeSource('google'), makeSource('hubspot', { sync_pending: pending })]),
        {
          'POST /sources/:source/sync': () => {
            pending = true;
            return apiError(409, 'sync_pending', 'A sync request for hubspot is already pending');
          },
        },
      );
      const { queryClient } = await renderLoaded();

      fireEvent.click(syncButton('HubSpot'));
      expect(
        await within(outcomeRegion('HubSpot')).findByText('A sync is already pending'),
      ).toBeTruthy();
      await act(async () => {
        await queryClient.refetchQueries({ type: 'active' });
      });

      await waitFor(() => {
        expect(syncButton('HubSpot').disabled).toBe(true);
      });
      expect(syncButton('HubSpot').getAttribute('aria-describedby')).toBe(
        'source-hubspot-sync-outcome',
      );
      expect(
        within(outcomeRegion('HubSpot')).getAllByText('A sync is already pending'),
      ).toHaveLength(1);
    });

    it("reports any other failure with the API's message", async () => {
      stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': apiError(503, 'service_unavailable', 'Database unavailable'),
      });
      await renderLoaded();

      fireEvent.click(syncButton('Google'));

      const outcome = outcomeRegion('Google');
      expect(await within(outcome).findByText('Sync request failed')).toBeTruthy();
      expect(within(outcome).getByText('Database unavailable')).toBeTruthy();
      expect(bannerTone(outcome)).toBe('critical');
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('keeps a failure across polls until the next press', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        let failing = true;
        const api = stubPage(json(200, makeSources()), {
          'POST /sources/:source/sync': () =>
            failing
              ? apiError(503, 'service_unavailable', 'Database unavailable')
              : json(202, syncRequest('google')),
        });
        const { queryClient } = await renderLoaded();

        fireEvent.click(syncButton('Google'));
        const outcome = outcomeRegion('Google');
        expect(await within(outcome).findByText('Sync request failed')).toBeTruthy();

        await nextPoll(api, queryClient);
        expect(within(outcome).getByText('Sync request failed')).toBeTruthy();
        expect(syncButton('Google').disabled).toBe(false);

        failing = false;
        fireEvent.click(syncButton('Google'));
        expect(await within(outcome).findByText('Sync requested')).toBeTruthy();
        expect(within(outcome).queryByText('Sync request failed')).toBeNull();
      } finally {
        vi.useRealTimers();
      }
    });

    it('is disabled with a visible hint while the API reports a pending request', async () => {
      stubPage(
        json(200, [makeSource('google', { sync_pending: true }), makeSource('linkedin')]),
      );
      await renderLoaded();

      const button = syncButton('Google');
      expect(button.disabled).toBe(true);
      const hintId = button.getAttribute('aria-describedby');
      expect(hintId).toBe('source-google-sync-pending');
      expect(document.getElementById(hintId ?? '')?.textContent).toBe('A sync is already pending');
      expect(within(outcomeRegion('Google')).getByText('A sync is already pending')).toBeTruthy();
      expect(syncButton('LinkedIn').disabled).toBe(false);
    });
  });

  describe('page states', () => {
    /**
     * The page's polite notice region: the one `aria-live="polite"` element outside every source
     * section, which announces the loading, failure and refresh-failure messages.
     */
    function noticeRegion(): HTMLElement {
      const regions = Array.from(
        document.querySelectorAll<HTMLElement>('[aria-live="polite"]'),
      ).filter((element) => element.closest('section') === null);
      expect(regions).toHaveLength(1);
      const [region] = regions;
      if (region === undefined) {
        throw new Error('The page has no polite notice region.');
      }
      return region;
    }

    /** Reads every active query again, as the hooks' polling would. */
    async function refetchActive(queryClient: QueryClient): Promise<void> {
      // Addressing all active queries keeps the tests free of the hooks' private query keys.
      await act(async () => {
        await queryClient.refetchQueries({ type: 'active' });
      });
    }

    it('keeps one polite notice region from the first render, with the sources outside it', async () => {
      const response = heldResponse();
      const api = stubPage(() => response.promise);
      await renderWithProviders(<SourcesPage />, { path: '/sources' });

      // Loading: the region holds the Spinner, which stays the page's one status element.
      const region = noticeRegion();
      expect(within(region).getByRole('status').textContent).toBe('Loading sources');
      expect(screen.getAllByRole('status')).toHaveLength(1);

      // The failure replaces the spinner inside the same region, so it is announced.
      response.release(apiError(500, 'internal_error', 'Database unavailable'));
      expect(await within(region).findByText('Sources unavailable')).toBeTruthy();
      expect(within(region).getByText('Database unavailable')).toBeTruthy();
      expect(noticeRegion()).toBe(region);

      // Once the sources load, the region stays, empty, and the sections follow outside it.
      api.use({ 'GET /sources': json(200, makeSources()) });
      fireEvent.click(within(region).getByRole('button', { name: 'Retry' }));
      await screen.findByRole('region', { name: 'Google' });
      expect(noticeRegion()).toBe(region);
      expect(region.childNodes).toHaveLength(0);
      expect(region.contains(section('Google'))).toBe(false);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('shows a spinner until the sources arrive', async () => {
      const response = heldResponse();
      stubPage(() => response.promise);
      await renderWithProviders(<SourcesPage />, { path: '/sources' });

      // The Spinner is the page's one status element while loading; its label is its text.
      expect(screen.getByRole('status').textContent).toBe('Loading sources');
      expect(screen.getByRole('heading', { level: 1, name: 'Sources' })).toBeTruthy();
      expect(screen.queryByRole('region', { name: 'Google' })).toBeNull();

      response.release(json(200, makeSources()));

      expect(await screen.findByRole('region', { name: 'Google' })).toBeTruthy();
      expect(screen.queryByText('Loading sources')).toBeNull();
    });

    it('says so when the API lists no source', async () => {
      stubPage(json(200, []));
      await renderWithProviders(<SourcesPage />, { path: '/sources' });

      expect(await screen.findByText('No sources configured')).toBeTruthy();
      expect(screen.queryByRole('button', { name: 'Sync now' })).toBeNull();
    });

    it('shows the failure with a Retry that reads the sources again', async () => {
      const api = stubPage(apiError(500, 'internal_error', 'Database unavailable'));
      await renderWithProviders(<SourcesPage />, { path: '/sources' });

      expect(await screen.findByText('Sources unavailable')).toBeTruthy();
      expect(screen.getByText('Database unavailable')).toBeTruthy();
      expect(screen.queryByText('Loading sources')).toBeNull();

      const sourceReads = (): number =>
        api.calls.filter((call) => call.method === 'GET' && call.path === '/sources').length;
      const readsBefore = sourceReads();
      api.use({ 'GET /sources': json(200, makeSources()) });
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

      expect(await screen.findByRole('region', { name: 'Google' })).toBeTruthy();
      expect(sourceReads()).toBeGreaterThan(readsBefore);
      expect(screen.queryByText('Sources unavailable')).toBeNull();
    });

    it('keeps the failure on screen while a retry runs, then returns focus to Retry', async () => {
      const api = stubPage(apiError(500, 'internal_error', 'Database unavailable'));
      await renderWithProviders(<SourcesPage />, { path: '/sources' });
      await screen.findByText('Sources unavailable');
      const response = heldResponse();
      api.use({ 'GET /sources': () => response.promise });
      const retry = screen.getByRole<HTMLButtonElement>('button', { name: 'Retry' });

      fireEvent.click(retry);

      await waitFor(() => {
        expect(retry.getAttribute('aria-busy')).toBe('true');
      });
      // No flicker back to the spinner while the read runs again.
      expect(screen.getByText('Sources unavailable')).toBeTruthy();
      expect(screen.queryByText('Loading sources')).toBeNull();

      response.release(apiError(503, 'service_unavailable', 'Still unavailable'));

      expect(await screen.findByText('Still unavailable')).toBeTruthy();
      expect(screen.queryByText('Database unavailable')).toBeNull();
      await waitFor(() => {
        expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Retry' }));
      });
    });

    it('keeps the last loaded sources under a notice when a refresh fails', async () => {
      const api = stubPage();
      const { queryClient } = await renderLoaded();
      const region = noticeRegion();
      expect(region.childNodes).toHaveLength(0);
      api.use({
        'GET /sources': apiError(503, 'service_unavailable', 'Database unavailable'),
      });

      // `GET /sources` fails while `GET /runs/latest` still answers.
      await refetchActive(queryClient);

      // The notice lands in the region that existed before the failure, so it is announced.
      expect(await within(region).findByText('Sources could not be refreshed')).toBeTruthy();
      expect(
        within(region).getByText('Database unavailable. Showing the last loaded state.'),
      ).toBeTruthy();
      expect(noticeRegion()).toBe(region);
      // Only the notice is announced: the sections, their tables and buttons stay outside it.
      expect(within(region).queryByRole('table')).toBeNull();
      expect(within(region).queryByRole('button')).toBeNull();
      expect(region.contains(section('Google'))).toBe(false);
      expect(within(section('Google')).getAllByRole('table').length).toBeGreaterThan(0);
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.queryByText('Sources unavailable')).toBeNull();

      // A later successful read clears the notice; the region itself stays.
      api.use({ 'GET /sources': json(200, makeSources()) });
      await refetchActive(queryClient);

      await waitFor(() => {
        expect(region.childNodes).toHaveLength(0);
      });
      expect(noticeRegion()).toBe(region);
      expect(screen.queryByText('Sources could not be refreshed')).toBeNull();
      expect(section('Google')).toBeTruthy();
    });

    it('keeps an empty source list under the notice when a refresh fails', async () => {
      const api = stubPage(json(200, []));
      const { queryClient } = await renderWithProviders(<SourcesPage />, { path: '/sources' });
      await screen.findByText('No sources configured');
      const region = noticeRegion();
      expect(region.childNodes).toHaveLength(0);
      api.use({
        'GET /sources': apiError(503, 'service_unavailable', 'Database unavailable'),
      });

      await refetchActive(queryClient);

      expect(await within(region).findByText('Sources could not be refreshed')).toBeTruthy();
      expect(
        within(region).getByText('Database unavailable. Showing the last loaded state.'),
      ).toBeTruthy();
      expect(noticeRegion()).toBe(region);
      expect(region.contains(screen.getByText('No sources configured'))).toBe(false);
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });
});
