/**
 * Component tests for `SourcesPage`, the `/sources` screen (AAP 0.5.5 "Other screens →
 * `/sources`"; AAP 0.5.3 `GET /sources`, `POST /sources/{source}/sync`; AAP 0.3.2 dataset
 * freshness; AAP 0.3.3 simulated fallback and the Google call cap).
 *
 * The page renders inside the providers of `../../test/render`, and the network is stubbed with
 * `../../test/mockApi`, so the real hooks read `GET /sources` and `GET /runs/latest` (the
 * display time zone) and the real client posts "Sync now". The cases cover, per source:
 *
 * - the status line: mode (Live, Simulated, or none yet) with its reason, connection status,
 *   the derived "Stale" badge and the last attempt in the display time zone;
 * - the Google webhook calls of the last 24 h against the product's own cap;
 * - the active primary and companion datasets, and each dataset's freshness, coverage and
 *   completeness exactly as stored;
 * - the recent sync runs, finished, running and failed;
 * - "Sync now": the POST with its CSRF header, the 202, 409 `sync_pending` and failure outcomes
 *   kept to their own section, the pending hint, and focus returned to the button;
 * - the page states: loading, failure with Retry, no sources, and a failed background refresh.
 *
 * @packageDocumentation
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { MockHandler, MockHandlers, MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { queryKeys } from '../../api/queries';
import type {
  ConnectionStatus,
  DataMode,
  DatasetFreshnessView,
  DatasetView,
  Source,
  SyncRequestView,
} from '../../api/types';
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
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual(['HubSpot', 'Google', 'LinkedIn']);
      // Only the open-discrepancies banner may be an alert (AAP 0.5.5).
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('shows each simulated source with its mode, status, reason and last attempt', async () => {
      stubPage();
      await renderLoaded();

      for (const label of ['Google', 'LinkedIn', 'HubSpot']) {
        const line = statusLine(section(label));
        expect(badges(line)).toEqual(['Simulated (info)', 'OK (success)']);
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

      expect(badges(statusLine(section('HubSpot')))).toEqual([
        'Simulated (info)',
        'OK (success)',
        'Stale (warning)',
      ]);
      // The connection already reports Stale, so no second badge is added.
      expect(badges(statusLine(section('Google')))).toEqual([
        'Simulated (info)',
        'Stale (warning)',
      ]);
    });

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
      expect(bodyRows(freshness)).toEqual([
        ['google.entities', 'complete', '—', '—', LAST_SYNC_UTC, LAST_SYNC_UTC, '—'],
        [
          'google.metrics_daily',
          'complete',
          '2026-04-09',
          '2026-10-05',
          LAST_SYNC_UTC,
          LAST_SYNC_UTC,
          '—',
        ],
        [
          'google.search_terms',
          'incomplete',
          '2026-04-09',
          '2026-10-05',
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

  describe('Sync now', () => {
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

    it('keeps the outcome and the button available once the API reports the request pending', async () => {
      let requested = false;
      // After the POST, the API reports the queued request (and a newer attempt, so the
      // re-read is visible on screen).
      stubPage(
        () =>
          json(200, [
            makeSource(
              'google',
              requested ? { sync_pending: true, last_attempt_at: '2026-10-06T06:01:00Z' } : {},
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
      await renderLoaded();

      fireEvent.click(syncButton('Google'));
      await within(outcomeRegion('Google')).findByText('Sync requested');
      expect(
        await within(section('Google')).findByText('Last attempt Oct 6, 2026, 6:01 AM UTC'),
      ).toBeTruthy();

      expect(syncButton('Google').disabled).toBe(false);
      expect(syncButton('Google').getAttribute('aria-describedby')).toBeNull();
      expect(within(outcomeRegion('Google')).getByText('Sync requested')).toBeTruthy();
      expect(within(outcomeRegion('Google')).queryByText('A sync is already pending')).toBeNull();
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
      expect(syncButton('LinkedIn').disabled).toBe(true);
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
        expect(button.disabled).toBe(true);
      });
      // While the button is disabled, focus rests on the page body, where a browser drops it
      // from a button that becomes disabled.
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
        expect(google.disabled).toBe(true);
      });
      const hubspot = syncButton('HubSpot');
      hubspot.focus();
      response.release(json(202, syncRequest('google')));

      expect(await within(outcomeRegion('Google')).findByText('Sync requested')).toBeTruthy();
      expect(document.activeElement).toBe(hubspot);
    });

    it('says a sync is already pending for 409 sync_pending', async () => {
      stubPage(json(200, makeSources()), {
        'POST /sources/:source/sync': apiError(
          409,
          'sync_pending',
          'A sync request for hubspot is already pending',
        ),
      });
      await renderLoaded();

      fireEvent.click(syncButton('HubSpot'));

      const outcome = outcomeRegion('HubSpot');
      expect(await within(outcome).findByText('A sync is already pending')).toBeTruthy();
      expect(bannerTone(outcome)).toBe('info');
      expect(within(outcome).queryByText('Sync requested')).toBeNull();
      expect(syncButton('HubSpot').disabled).toBe(false);
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
    it('shows a spinner until the sources arrive', async () => {
      const response = heldResponse();
      stubPage(() => response.promise);
      await renderWithProviders(<SourcesPage />, { path: '/sources' });

      expect(screen.getByText('Loading sources')).toBeTruthy();
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

      api.use({ 'GET /sources': json(200, makeSources()) });
      fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

      expect(await screen.findByRole('region', { name: 'Google' })).toBeTruthy();
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
      api.use({
        'GET /sources': apiError(503, 'service_unavailable', 'Database unavailable'),
      });

      await act(async () => {
        await queryClient.refetchQueries({ queryKey: queryKeys.sources });
      });

      expect(await screen.findByText('Sources could not be refreshed')).toBeTruthy();
      expect(
        screen.getByText('Database unavailable Showing the last loaded state.'),
      ).toBeTruthy();
      expect(section('Google')).toBeTruthy();
      expect(screen.queryByText('Sources unavailable')).toBeNull();
    });
  });
});
