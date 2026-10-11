/**
 * Component tests for `SettingsPage`, the read-only `/settings` screen (AAP 0.5.5 "Other screens
 * → `/settings`"; AAP 0.5.3 `GET /config`).
 *
 * The page renders inside the providers of `../../test/render`, and the network is stubbed with
 * `../../test/mockApi`, so the real `useConfig` hook reads `GET /config` through the real client.
 * The cases cover:
 *
 * - the analysis window: default, trusted minimum, the presets with the default marked (also
 *   after the default changes), maximum and sustained-pattern minimum, all from the response;
 * - every threshold as a dotted, sorted key with its configured value verbatim;
 * - the HubSpot mapping: MQL and Qualified Lead bound separately, the meeting rules, and the
 *   bindings resolved against the primary and the companion CRM dataset, an unresolved one
 *   reading "unavailable" with its reason and never `0`, each verified at an instant shown in
 *   the default run's display time zone (UTC until a run names one) with its ISO 8601 value in
 *   `<time dateTime>`;
 * - the white paper registry and `config_hash`;
 * - table sizing: only the key, mode, property, reason, slug and URN columns break unbroken tokens
 *   anywhere, the resolved stage id never breaks, and the column shares that keep identifiers
 *   whole at desktop widths: binding, property and reason in the resolved tables, and the
 *   registry's title, with the resolved property and reason held at a minimum width;
 * - the read-only contract (no control, no request but `GET /config` and, while an instant is on
 *   screen, `GET /runs/latest`), text lines that are never paragraphs, loading, failure, and a
 *   failed background refresh that keeps the last configuration under a non-alert "Settings
 *   could not be refreshed" notice with the reason and the last-loaded instant in the default
 *   run's display time zone (UTC before the first run), which leaves once a refresh succeeds.
 *
 * @packageDocumentation
 */
import { apiError, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandler, MockResponse } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import type { QueryClient } from '@tanstack/react-query';
import { act, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { queryKeys } from '../../api/queries';
import type { ConfigView, CrmBindingsView, WindowConfig } from '../../api/types';
import { formatInstant } from '../../lib/format';
import { FIXTURE_CONFIG_HASH, makeConfig, makeRun } from '../../test/fixtures';
import { SettingsPage } from './SettingsPage';

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

/** Why the Google-to-CRM link is unavailable in the fixture world. */
const GOOGLE_LINK_UNCONFIGURED =
  'HUBSPOT_GOOGLE_CAMPAIGN_PROPERTY and HUBSPOT_GOOGLE_TOUCH_DATE_PROPERTY not configured';

/**
 * The display time zone of the default run the stubs answer with: not UTC, so a test sees each
 * instant converted from the API's UTC value.
 */
const DISPLAY_TIMEZONE = 'America/New_York';

/** When the fixture's primary and companion CRM datasets verified their bindings. */
const PRIMARY_VERIFIED_AT = '2026-10-01T08:10:00Z';
const COMPANION_VERIFIED_AT = '2026-10-01T08:11:00Z';

/**
 * Stubs the page's two reads: `GET /config`, and `GET /runs/latest`, which the page reads for the
 * display time zone of its instants while one is on screen.
 *
 * @param config - the answer to `GET /config`; the fixture configuration by default
 * @param defaultRun - the answer to `GET /runs/latest`; by default a run in
 *   {@link DISPLAY_TIMEZONE}
 * @returns the request log
 */
function stubConfig(
  config: MockHandler = json(200, makeConfig()),
  defaultRun: MockHandler = json(200, makeRun({ display_timezone: DISPLAY_TIMEZONE })),
) {
  return mockApi({ 'GET /config': config, 'GET /runs/latest': defaultRun });
}

/** Each request of the log as "METHOD /path", in request order. */
function requests(api: ApiMock): string[] {
  return api.calls.map((call) => `${call.method} ${call.path}`);
}

/**
 * A bindings table's verification line in `section`: the `<time>` showing `verifiedAt` as
 * `formatInstant` formats it in `zone`, once it does.
 *
 * @param section - the bindings table's section
 * @param verifiedAt - the API's ISO 8601 `verified_at`
 * @param zone - the IANA zone the instant is expected in
 * @returns the `<time>` element
 */
async function findVerifiedAt(
  section: HTMLElement,
  verifiedAt: string,
  zone: string,
): Promise<HTMLElement> {
  const shown = formatInstant(verifiedAt, zone);
  expect(shown.endsWith(` ${zone}`)).toBe(true);
  const time = await within(section).findByText(shown);
  expect(time.tagName).toBe('TIME');
  // The instant stays machine-readable exactly as the API returned it.
  expect(time.getAttribute('datetime')).toBe(verifiedAt);
  expect(time.parentElement?.textContent).toBe(`Verified against the portal at ${shown}.`);
  return time;
}

/** Renders the page at `/settings` and waits for the loaded configuration. */
async function renderLoaded() {
  const result = await renderWithProviders(<SettingsPage />, { path: '/settings' });
  await screen.findByRole('region', { name: 'Analysis window' });
  return result;
}

/** A configuration with the window settings replaced. */
function withWindow(window: Partial<WindowConfig>): ConfigView {
  return makeConfig({ window: { ...makeConfig().window, ...window } });
}

/** A table by its caption. */
function table(caption: string, scope: HTMLElement = document.body): HTMLElement {
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

/** The window table's row for one setting, by its row header. */
function windowRow(setting: string): string[] {
  const row = bodyRows(table('Analysis window settings')).find(([label]) => label === setting);
  if (row === undefined) {
    throw new Error(`The window table has no "${setting}" row.`);
  }
  return row;
}

/** The items of the "Window presets" list, as text. */
function presetItems(): string[] {
  const list = screen.getByRole('list', { name: 'Window presets' });
  return within(list)
    .getAllByRole('listitem')
    .map((item) => item.textContent ?? '');
}

/** The thresholds table as `{dotted key: value}` pairs, in table order. */
function thresholdPairs(): [string, string][] {
  return bodyRows(table('Thresholds')).map(([key, value]): [string, string] => [
    key ?? '',
    value ?? '',
  ]);
}

/** One meeting rule's line: its label followed by its values. */
function meetingRule(label: string): string {
  const group = screen.getByLabelText('Meeting rules');
  return within(group).getByText(label).parentElement?.textContent ?? '';
}

/**
 * A level-3 section, found by its heading. The companion's table carries the same caption as its
 * section heading, so the two regions share a name and the section is reached from its heading.
 */
function subsection(title: string): HTMLElement {
  const section = screen.getByRole('heading', { level: 3, name: title }).closest('section');
  if (section === null) {
    throw new Error(`The heading "${title}" is not inside a section.`);
  }
  return section;
}

/**
 * The fixture's resolved bindings with `changes` applied: each binding `changes` supplies replaces
 * the fixture's binding of that name whole, and each binding it omits is kept from the fixture.
 */
function bindings(changes: Partial<CrmBindingsView>): CrmBindingsView {
  const base = makeConfig().crm_bindings;
  if (base === null) {
    throw new Error('The fixture configuration has no resolved bindings.');
  }
  return { ...base, ...changes };
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

/** The five level-2 section headings of the loaded page, in order. */
const SECTION_TITLES = [
  'Analysis window',
  'Thresholds',
  'HubSpot mapping',
  'White paper registry',
  'Configuration hash',
];

/** The message of the failed background refresh the refresh tests stub. */
const REFRESH_FAILURE_MESSAGE = 'Database unavailable';

/**
 * Renders the loaded page while `GET /runs/latest` answers `defaultRun`, then refetches
 * `GET /config` against a 503, as a background refresh on window focus or remount would.
 *
 * @param defaultRun - the answer to every `GET /runs/latest`, the bindings tables' and the
 *   notice's
 * @returns the request log and the page's query client
 */
async function renderFailedRefresh(
  defaultRun: MockResponse,
): Promise<{ api: ApiMock; queryClient: QueryClient }> {
  // The loaded page's bindings tables read the default run first, so it answers `defaultRun`
  // from the start rather than only for the notice.
  const api = stubConfig(undefined, defaultRun);
  const { queryClient } = await renderLoaded();
  api.use({
    'GET /config': apiError(503, 'service_unavailable', REFRESH_FAILURE_MESSAGE),
  });
  await act(async () => {
    await queryClient.refetchQueries({ queryKey: queryKeys.config });
  });
  return { api, queryClient };
}

/**
 * The refresh notice's last-loaded line: when the cached configuration was loaded, as
 * `formatInstant` shows it in `zone`.
 *
 * @param queryClient - the page's query client
 * @param zone - the IANA zone the instant is expected in
 * @returns the expected line
 */
function lastLoadedLine(queryClient: QueryClient, zone: string): string {
  const updatedAt = queryClient.getQueryState(queryKeys.config)?.dataUpdatedAt;
  if (updatedAt === undefined || updatedAt === 0) {
    throw new Error('The configuration was never loaded.');
  }
  const loadedAt = formatInstant(new Date(updatedAt).toISOString(), zone);
  return `Showing the configuration as last loaded ${loadedAt}.`;
}

// ---------------------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------------------

describe('SettingsPage', () => {
  describe('layout and read-only contract', () => {
    it('renders the five sections in order under the read-only note', async () => {
      stubConfig();
      await renderLoaded();

      expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeTruthy();
      expect(
        screen.getByText('Read-only. Change the files in config/ and restart the services.'),
      ).toBeTruthy();
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual([
        'Analysis window',
        'Thresholds',
        'HubSpot mapping',
        'White paper registry',
        'Configuration hash',
      ]);
    });

    it('renders no control and sends no request but GET /config and GET /runs/latest', async () => {
      const api = stubConfig();
      await renderLoaded();
      await findVerifiedAt(
        subsection('Resolved stage properties'),
        PRIMARY_VERIFIED_AT,
        DISPLAY_TIMEZONE,
      );

      for (const role of ['button', 'textbox', 'combobox', 'checkbox', 'spinbutton'] as const) {
        expect(screen.queryAllByRole(role)).toHaveLength(0);
      }
      // Both bindings tables observe the one default-run query, so the zone is read once.
      expect(requests(api)).toEqual(['GET /config', 'GET /runs/latest']);
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('reads no run while no instant is on screen', async () => {
      const api = stubConfig(
        json(200, makeConfig({ crm_bindings: null, companion_crm_bindings: null })),
      );
      const { container } = await renderLoaded();

      expect(container.querySelectorAll('time')).toHaveLength(0);
      expect(requests(api)).toEqual(['GET /config']);
    });

    it.each([
      ['with resolved primary and companion bindings', makeConfig()],
      [
        'before HubSpot has verified its bindings',
        makeConfig({ crm_bindings: null, companion_crm_bindings: null }),
      ],
    ])(
      'renders its notes as Stack lines, never paragraphs, %s',
      async (_case, config: ConfigView) => {
        stubConfig(json(200, config));
        const { container } = await renderLoaded();

        // A <p>'s user-agent margins would add space that no spacing token sets (AAP 0.4.4).
        expect(container.querySelectorAll('p')).toHaveLength(0);
        // The read-only note follows the title bar in the page's Stack, so the Stack gap alone
        // separates them.
        const note = screen.getByText(
          'Read-only. Change the files in config/ and restart the services.',
        );
        expect(note.tagName).toBe('DIV');
        expect(
          note.previousElementSibling?.contains(
            screen.getByRole('heading', { level: 1, name: 'Settings' }),
          ),
        ).toBe(true);
      },
    );

    it('keeps the polite status region after the note, empty until there is news', async () => {
      stubConfig();
      await renderLoaded();

      // An empty status child of a Stack is taken out of flow, so it adds no gap, while the region
      // stays in the accessibility tree for a later failure to be announced (WCAG 2.2 SC 4.1.3).
      const status = screen.getByRole('status');
      expect(status.childNodes).toHaveLength(0);
      expect(status.previousElementSibling?.textContent).toBe(
        'Read-only. Change the files in config/ and restart the services.',
      );
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  describe('analysis window', () => {
    it('shows every window setting with its key and value from the response', async () => {
      stubConfig();
      await renderLoaded();

      const windowTable = table('Analysis window settings');
      expect(headers(windowTable)).toEqual(['Setting', 'Key', 'Value']);
      expect(windowRow('Default window')).toEqual([
        'Default window',
        'window.default_days',
        '28 days',
      ]);
      expect(windowRow('Trusted minimum')).toEqual([
        'Trusted minimum',
        'window.trusted_min_days',
        '14 days',
      ]);
      expect(windowRow('Presets').slice(0, 2)).toEqual(['Presets', 'window.presets']);
      expect(windowRow('Maximum window')).toEqual([
        'Maximum window',
        'window.max_days',
        '180 days',
      ]);
      expect(windowRow('Sustained-pattern minimum')).toEqual([
        'Sustained-pattern minimum',
        'window.sustained_min_days',
        '7 days',
      ]);
      expect(presetItems()).toEqual(['14 days', '28 days default', '56 days', '90 days']);
    });

    it('marks the configured default when it changes', async () => {
      stubConfig(json(200, withWindow({ default_days: 21, presets: [14, 21, 28, 56, 90] })));
      await renderLoaded();

      expect(windowRow('Default window')[2]).toBe('21 days');
      expect(presetItems()).toEqual([
        '14 days',
        '21 days default',
        '28 days',
        '56 days',
        '90 days',
      ]);
    });

    it('marks at most one preset, and none when no preset equals the default', async () => {
      stubConfig(json(200, withWindow({ default_days: 28, presets: [28, 28, 56] })));
      const { unmount } = await renderLoaded();

      expect(presetItems()).toEqual(['28 days default', '28 days', '56 days']);
      unmount();

      stubConfig(json(200, withWindow({ default_days: 30, presets: [14, 28] })));
      await renderLoaded();

      expect(presetItems()).toEqual(['14 days', '28 days']);
    });

    it('says "1 day" for a single day and "none" for an empty preset list', async () => {
      stubConfig(
        json(
          200,
          withWindow({ default_days: 1, trusted_min_days: 1, presets: [], sustained_min_days: 1 }),
        ),
      );
      await renderLoaded();

      expect(windowRow('Default window')[2]).toBe('1 day');
      expect(windowRow('Trusted minimum')[2]).toBe('1 day');
      expect(windowRow('Sustained-pattern minimum')[2]).toBe('1 day');
      expect(windowRow('Presets')[2]).toBe('none');
      expect(screen.queryByRole('list', { name: 'Window presets' })).toBeNull();
    });
  });

  describe('thresholds', () => {
    it('lists every configured threshold as a dotted key, sorted, with its value verbatim', async () => {
      const config = makeConfig();
      stubConfig(json(200, config));
      await renderLoaded();

      expect(headers(table('Thresholds'))).toEqual(['Key', 'Value']);
      const pairs = thresholdPairs();
      const keys = pairs.map(([key]) => key);
      // The fixture tree has two levels: every namespace key counts once.
      const leaves = Object.values(config.thresholds).reduce<number>(
        (count, namespace) => count + Object.keys(namespace as object).length,
        0,
      );
      expect(pairs).toHaveLength(leaves);
      expect(keys).toEqual([...keys].sort());
      // The dotted keys are the table's row headers (`<th scope="row">`), in the same order.
      expect(
        within(table('Thresholds'))
          .getAllByRole('rowheader')
          .map((cell) => cell.textContent),
      ).toEqual(keys);
      expect(keys[0]).toBe('attribution.floor_share');
      const values = new Map(pairs);
      // Ratios stay ratios: 0.95 is never shown as 95%.
      expect(values.get('budget.capped_day_ratio')).toBe('0.95');
      expect(values.get('k1.min_clicks_floor')).toBe('10');
      expect(values.get('window.presets')).toBe('14, 28, 56, 90');
      expect(values.get('crm.booking_sources')).toBe('MEETINGS_PUBLIC');
      expect(values.get('sources.stale_after_hours')).toBe('24');
    });

    it('renders every kind of configured value without dropping a key', async () => {
      stubConfig(
        json(
          200,
          makeConfig({
            thresholds: {
              top_scalar: 3,
              nested: { 'already.dotted': 5, deeper: { leaf: 'text' } },
              limits: {
                objects: [{ a: 1 }],
                none_yet: null,
                mixed: [1, 'two', true, null],
                empty_list: [],
              },
              flags: { strict: false, enabled: true },
              empty_namespace: {},
            },
          }),
        ),
      );
      await renderLoaded();

      expect(thresholdPairs()).toEqual([
        ['empty_namespace', 'none'],
        ['flags.enabled', 'true'],
        ['flags.strict', 'false'],
        ['limits.empty_list', 'none'],
        ['limits.mixed', '1, two, true, none'],
        ['limits.none_yet', 'none'],
        ['limits.objects', '{"a":1}'],
        ['nested.already.dotted', '5'],
        ['nested.deeper.leaf', 'text'],
        ['top_scalar', '3'],
      ]);
    });

    it('says so when no threshold is configured', async () => {
      stubConfig(json(200, makeConfig({ thresholds: {} })));
      await renderLoaded();

      expect(bodyRows(table('Thresholds'))).toEqual([['No thresholds configured.']]);
    });
  });

  describe('HubSpot mapping', () => {
    it('shows the MQL and Qualified Lead bindings separately, never combined', async () => {
      stubConfig();
      await renderLoaded();

      const mapping = table('MQL and Qualified Lead bindings');
      expect(headers(mapping)).toEqual(['Count', 'Mode', 'Stage label(s)', 'Property']);
      expect(bodyRows(mapping)).toEqual([
        ['MQL', 'stage_label', 'First meeting booked', '—'],
        ['Qualified Lead', 'stage_label', 'Qualified Lead, Sales Qualified Lead', '—'],
      ]);
      expect(
        screen.getByText('MQL and Qualified Lead are bound separately and never combined.'),
      ).toBeTruthy();
      expect(meetingRule('Booking sources')).toBe('Booking sourcesMEETINGS_PUBLIC');
      expect(meetingRule('Meeting outcome exclusions')).toBe(
        'Meeting outcome exclusionsCANCELED',
      );
    });

    it('shows a property binding by its property and skips blank labels and values', async () => {
      const base = makeConfig().hubspot_mapping;
      stubConfig(
        json(
          200,
          makeConfig({
            hubspot_mapping: {
              ...base,
              mql: { mode: 'property', stage_labels: [], property: 'mql_date' },
              ql: { mode: 'stage_label', stage_labels: ['Qualified Lead', '  '], property: null },
              booking_sources: ['MEETINGS_PUBLIC', 'CRM_UI', ' '],
              meeting_outcome_exclusions: [],
            },
          }),
        ),
      );
      await renderLoaded();

      expect(bodyRows(table('MQL and Qualified Lead bindings'))).toEqual([
        ['MQL', 'property', '—', 'mql_date'],
        ['Qualified Lead', 'stage_label', 'Qualified Lead', '—'],
      ]);
      expect(meetingRule('Booking sources')).toBe('Booking sourcesMEETINGS_PUBLIC, CRM_UI');
      expect(meetingRule('Meeting outcome exclusions')).toBe('Meeting outcome exclusionsnone');
    });

    it('shows the bindings resolved against the primary and the companion CRM dataset', async () => {
      stubConfig();
      await renderLoaded();

      const primary = table('Resolved stage properties (HubSpot)');
      expect(headers(primary)).toEqual([
        'Binding',
        'Status',
        'Matched label',
        'Stage id',
        'Property',
        'Reason',
      ]);
      expect(bodyRows(primary)).toEqual([
        [
          'MQL',
          'resolved',
          'First meeting booked',
          '1084512345',
          'hs_v2_date_entered_1084512345',
          '—',
        ],
        [
          'Qualified Lead',
          'resolved',
          'Qualified Lead',
          '1084512399',
          'hs_v2_date_entered_1084512399',
          '—',
        ],
        ['Google campaign property', 'unavailable', '—', '—', '—', GOOGLE_LINK_UNCONFIGURED],
        [
          'Google touch-date property',
          'unavailable',
          '—',
          '—',
          '—',
          GOOGLE_LINK_UNCONFIGURED,
        ],
      ]);
      // Each instant is shown in the default run's display time zone, never as the raw ISO text.
      const primarySection = subsection('Resolved stage properties');
      await findVerifiedAt(primarySection, PRIMARY_VERIFIED_AT, DISPLAY_TIMEZONE);
      expect(within(primarySection).queryByText(PRIMARY_VERIFIED_AT)).toBeNull();

      const companionSection = subsection('Resolved stage properties (simulated companion)');
      expect(
        within(companionSection).getByText(
          'Used for LinkedIn-attributed evidence while LinkedIn data is simulated.',
        ),
      ).toBeTruthy();
      expect(
        bodyRows(table('Resolved stage properties (simulated companion)', companionSection)),
      ).toHaveLength(4);
      await findVerifiedAt(companionSection, COMPANION_VERIFIED_AT, DISPLAY_TIMEZONE);
      expect(within(companionSection).queryByText(COMPANION_VERIFIED_AT)).toBeNull();
    });

    it('shows the verification instant in the configured zone', async () => {
      stubConfig();
      await renderLoaded();

      const time = await findVerifiedAt(
        subsection('Resolved stage properties'),
        PRIMARY_VERIFIED_AT,
        DISPLAY_TIMEZONE,
      );
      // 08:10 UTC is 04:10 in New York on daylight time.
      expect(time.textContent).toBe('Oct 1, 2026, 4:10 AM America/New_York');
    });

    it.each([
      ['before the first run (404)', apiError(404, 'not_found', 'No analysis run yet')],
      ['while the run names no zone', json(200, makeRun({ display_timezone: '  ' }))],
    ])('shows the verification instants in UTC %s', async (_case, defaultRun) => {
      stubConfig(undefined, defaultRun);
      const { queryClient } = await renderLoaded();

      // The read of the default run has settled, so the fallback is not just pending.
      await waitFor(() => {
        const run = queryClient.getQueryState(queryKeys.runs.latest);
        expect(['success', 'error']).toContain(run?.status);
        expect(run?.fetchStatus).toBe('idle');
      });
      await findVerifiedAt(subsection('Resolved stage properties'), PRIMARY_VERIFIED_AT, 'UTC');
      await findVerifiedAt(
        subsection('Resolved stage properties (simulated companion)'),
        COMPANION_VERIFIED_AT,
        'UTC',
      );
    });

    it('shows the instant in UTC while the run is pending, then in its zone', async () => {
      const run = heldResponse();
      stubConfig(undefined, () => run.promise);
      await renderLoaded();

      const primarySection = subsection('Resolved stage properties');
      await findVerifiedAt(primarySection, PRIMARY_VERIFIED_AT, 'UTC');

      run.release(json(200, makeRun({ display_timezone: ` ${DISPLAY_TIMEZONE} ` })));

      // The configured zone is trimmed before use.
      await findVerifiedAt(primarySection, PRIMARY_VERIFIED_AT, DISPLAY_TIMEZONE);
    });

    it('reads "unavailable" with its reason for an unresolved binding, never 0', async () => {
      stubConfig(
        json(
          200,
          makeConfig({
            crm_bindings: bindings({
              mql: {
                available: false,
                property: 'hs_v2_date_entered_stale',
                stage_id: '1084512345',
                label: 'First meeting booked',
                reason: 'MQL stage "First meeting booked" not resolved',
              },
              ql: { available: false, property: null, stage_id: null, label: null, reason: '' },
              google: {
                available: true,
                campaign_property: 'google_campaign_id',
                touch_date_property: '  ',
                reason: null,
              },
            }),
            companion_crm_bindings: null,
          }),
        ),
      );
      await renderLoaded();

      const primary = table('Resolved stage properties (HubSpot)');
      expect(bodyRows(primary)).toEqual([
        [
          'MQL',
          'unavailable',
          '—',
          '—',
          '—',
          'MQL stage "First meeting booked" not resolved',
        ],
        ['Qualified Lead', 'unavailable', '—', '—', '—', 'No reason recorded'],
        ['Google campaign property', 'resolved', '—', '—', 'google_campaign_id', '—'],
        ['Google touch-date property', 'resolved', '—', '—', '—', '—'],
      ]);
      expect(within(primary).queryByText('0')).toBeNull();
      // No companion CRM dataset is active, so its section and its table are absent.
      expect(
        screen.queryByRole('heading', { name: 'Resolved stage properties (simulated companion)' }),
      ).toBeNull();
      expect(
        screen.queryByRole('table', { name: 'Resolved stage properties (simulated companion)' }),
      ).toBeNull();
    });

    it('says the bindings are not resolved yet before HubSpot has verified them', async () => {
      stubConfig(json(200, makeConfig({ crm_bindings: null, companion_crm_bindings: null })));
      await renderLoaded();

      const primarySection = subsection('Resolved stage properties');
      expect(
        within(primarySection).getByText(
          'Not resolved yet. The HubSpot dataset records them when it verifies its properties at startup.',
        ),
      ).toBeTruthy();
      expect(
        screen.queryByRole('table', { name: 'Resolved stage properties (HubSpot)' }),
      ).toBeNull();
    });
  });

  describe('white paper registry and configuration hash', () => {
    it('lists every white paper in file order with its lead forms and creative overrides', async () => {
      const config = makeConfig();
      stubConfig(json(200, config));
      await renderLoaded();

      const registry = table('White paper registry');
      expect(headers(registry)).toEqual(['Slug', 'Title', 'Lead forms', 'Creative overrides']);
      expect(bodyRows(registry).map((row) => [row[0], row[1], row[3]])).toEqual([
        ['b2b-pipeline-report-2026', 'The 2026 B2B Pipeline Report', '—'],
        ['paid-media-benchmarks-2026', 'Paid Media Benchmarks 2026', '—'],
        ['attribution-playbook', 'The Attribution Playbook', 'urn:li:sponsoredCreative:990001301'],
      ]);
      // Each slug is its row's header, and every lead form of the asset is listed in that row.
      expect(
        within(registry)
          .getAllByRole('rowheader')
          .map((cell) => cell.textContent),
      ).toEqual(config.content_assets.map((asset) => asset.slug));
      const rows = within(registry).getAllByRole('row').slice(1);
      expect(rows).toHaveLength(config.content_assets.length);
      config.content_assets.forEach((asset, index) => {
        const row = rows[index];
        if (row === undefined) {
          throw new Error(`The registry has no row for "${asset.slug}".`);
        }
        const forms = within(row).getByRole('list', { name: `Lead forms of ${asset.slug}` });
        expect(within(forms).getAllByRole('listitem').map((item) => item.textContent)).toEqual(
          asset.lead_forms,
        );
      });
      const overrides = screen.getByRole('list', {
        name: 'Creative overrides of attribution-playbook',
      });
      expect(within(overrides).getAllByRole('listitem')).toHaveLength(1);
    });

    it('shows a dash for an asset without lead forms and says so for an empty registry', async () => {
      stubConfig(
        json(
          200,
          makeConfig({
            content_assets: [
              { slug: 'draft-guide', title: 'Draft Guide', lead_forms: [], creative_overrides: [] },
            ],
          }),
        ),
      );
      const { unmount } = await renderLoaded();

      expect(bodyRows(table('White paper registry'))).toEqual([
        ['draft-guide', 'Draft Guide', '—', '—'],
      ]);
      unmount();

      stubConfig(json(200, makeConfig({ content_assets: [] })));
      await renderLoaded();

      expect(bodyRows(table('White paper registry'))).toEqual([['No white papers registered.']]);
    });

    it('shows config_hash with what it is for', async () => {
      stubConfig();
      await renderLoaded();

      const hashSection = screen.getByRole('region', { name: 'Configuration hash' });
      expect(within(hashSection).getByText(FIXTURE_CONFIG_HASH)).toBeTruthy();
      expect(
        within(hashSection).getByText(
          "Stored on every analysis run, so a run's evidence can be traced to this configuration.",
        ),
      ).toBeTruthy();
    });
  });

  describe('table sizing', () => {
    /**
     * Each column's header text paired with whether every one of its body cells may break
     * unbroken tokens anywhere (the DataTable `wrap: 'anywhere'` class), in column order.
     */
    function breakAnywhereColumns(tableElement: HTMLElement): [string, boolean][] {
      const rows = within(tableElement).getAllByRole('row').slice(1);
      return headers(tableElement).map((header, index): [string, boolean] => [
        header,
        rows.every((row) =>
          row.children[index]?.classList.contains('ds-datatable__cell--wrap-anywhere'),
        ),
      ]);
    }

    it('lets only the key, mode, property, reason, slug and URN columns break tokens', async () => {
      stubConfig();
      await renderLoaded();

      expect(breakAnywhereColumns(table('Analysis window settings'))).toEqual([
        ['Setting', false],
        ['Key', true],
        ['Value', false],
      ]);
      expect(breakAnywhereColumns(table('Thresholds'))).toEqual([
        ['Key', true],
        ['Value', false],
      ]);
      expect(breakAnywhereColumns(table('MQL and Qualified Lead bindings'))).toEqual([
        ['Count', false],
        ['Mode', true],
        ['Stage label(s)', false],
        ['Property', true],
      ]);
      const resolved: [string, boolean][] = [
        ['Binding', false],
        ['Status', false],
        ['Matched label', false],
        ['Stage id', false],
        ['Property', true],
        ['Reason', true],
      ];
      const primary = table('Resolved stage properties (HubSpot)');
      expect(breakAnywhereColumns(primary)).toEqual(resolved);
      // A stage id never breaks, so it needs no minimum width to keep a numeric id on one line.
      const stageIdCells = within(primary)
        .getAllByRole('row')
        .map((row) => row.children[3]);
      expect(stageIdCells).toHaveLength(5);
      for (const cell of stageIdCells) {
        expect(cell?.className).not.toMatch(/ds-datatable__cell--min-/u);
      }
      // The property and reason hold the 'md' minimum, so a narrow region scrolls the table
      // rather than splitting an identifier into a few characters per line.
      for (const row of within(primary).getAllByRole('row')) {
        for (const index of [4, 5]) {
          expect(row.children[index]?.classList.contains('ds-datatable__cell--min-md')).toBe(true);
        }
      }
      const companionSection = subsection('Resolved stage properties (simulated companion)');
      expect(
        breakAnywhereColumns(
          table('Resolved stage properties (simulated companion)', companionSection),
        ),
      ).toEqual(resolved);
      expect(breakAnywhereColumns(table('White paper registry'))).toEqual([
        ['Slug', true],
        ['Title', false],
        ['Lead forms', true],
        ['Creative overrides', true],
      ]);
      // Headers keep breaking between words only, and no status chip is ever split mid-word.
      for (const header of screen.getAllByRole('columnheader')) {
        expect(header.classList.contains('ds-datatable__cell--wrap-anywhere')).toBe(false);
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

    it('shares the width so identifiers stay whole wherever the table has room', async () => {
      stubConfig();
      await renderLoaded();

      // Property and reason take a third each and the binding a sixth; the rest keep their
      // content's width.
      const resolved: [string, number | null][] = [
        ['Binding', 2],
        ['Status', null],
        ['Matched label', null],
        ['Stage id', null],
        ['Property', 4],
        ['Reason', 4],
      ];
      expect(columnShares(table('Resolved stage properties (HubSpot)'))).toEqual(resolved);
      const companionSection = subsection('Resolved stage properties (simulated companion)');
      expect(
        columnShares(table('Resolved stage properties (simulated companion)', companionSection)),
      ).toEqual(resolved);
      // The title wraps within a sixth, leaving the rest to the slug and URN columns.
      expect(columnShares(table('White paper registry'))).toEqual([
        ['Slug', null],
        ['Title', 2],
        ['Lead forms', null],
        ['Creative overrides', null],
      ]);
      for (const caption of [
        'Analysis window settings',
        'Thresholds',
        'MQL and Qualified Lead bindings',
      ]) {
        expect(table(caption).querySelector('colgroup')).toBeNull();
      }
    });
  });

  describe('page states', () => {
    it('shows a spinner until the configuration arrives', async () => {
      const response = heldResponse();
      stubConfig(() => response.promise);
      await renderWithProviders(<SettingsPage />, { path: '/settings' });

      expect(screen.getByText('Loading settings')).toBeTruthy();
      expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeTruthy();
      expect(screen.queryByRole('region', { name: 'Analysis window' })).toBeNull();

      response.release(json(200, makeConfig()));

      expect(await screen.findByRole('region', { name: 'Analysis window' })).toBeTruthy();
      expect(screen.queryByText('Loading settings')).toBeNull();
    });

    it("announces a failed read with the API's message in a polite status region", async () => {
      const message = 'window.default_days (21) is not one of window.presets';
      stubConfig(apiError(500, 'config_error', message));
      await renderWithProviders(<SettingsPage />, { path: '/settings' });

      const status = await waitFor(() => {
        const region = screen.getByRole('status');
        expect(within(region).getByText('Settings unavailable')).toBeTruthy();
        return region;
      });
      expect(within(status).getByText(message)).toBeTruthy();
      expect(screen.queryByText('Loading settings')).toBeNull();
      expect(screen.queryByRole('region', { name: 'Analysis window' })).toBeNull();
      expect(screen.queryAllByRole('table')).toHaveLength(0);
      expect(screen.queryByRole('alert')).toBeNull();
      // The page has no controls, so the failure offers no action.
      expect(screen.queryAllByRole('button')).toHaveLength(0);
    });

    it('never shows a blank reason: a whitespace-only API message gets the generic one', async () => {
      stubConfig(apiError(500, 'config_error', '   '));
      await renderWithProviders(<SettingsPage />, { path: '/settings' });

      const status = await waitFor(() => {
        const region = screen.getByRole('status');
        expect(within(region).getByText('Settings unavailable')).toBeTruthy();
        return region;
      });
      expect(within(status).getByText('The configuration could not be loaded.')).toBeTruthy();
      expect(screen.queryAllByRole('table')).toHaveLength(0);
    });

    it('keeps the last configuration and announces a failed background refresh', async () => {
      const { api, queryClient } = await renderFailedRefresh(
        json(200, makeRun({ display_timezone: 'America/New_York' })),
      );

      const status = screen.getByRole('status');
      const line = lastLoadedLine(queryClient, 'America/New_York');
      expect(line).toMatch(/ America\/New_York\.$/);
      expect(await within(status).findByText(line)).toBeTruthy();
      expect(within(status).getByText('Settings could not be refreshed')).toBeTruthy();
      expect(within(status).getByText(REFRESH_FAILURE_MESSAGE)).toBeTruthy();
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual(SECTION_TITLES);
      expect(within(status).queryByRole('table')).toBeNull();
      expect(screen.queryByText('Settings unavailable')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
      // The page has no controls, so the notice offers no Retry.
      expect(screen.queryAllByRole('button')).toHaveLength(0);
      // The notice shares the bindings tables' default-run query: the page reads nothing else.
      expect(new Set(requests(api))).toEqual(new Set(['GET /config', 'GET /runs/latest']));
      expect(requests(api).filter((call) => call === 'GET /config')).toHaveLength(2);
    });

    it.each([
      ['before the first run (404)', apiError(404, 'not_found', 'No analysis run yet')],
      ['while the run names no zone', json(200, makeRun({ display_timezone: '  ' }))],
    ])('labels the last-loaded instant UTC %s', async (_case, defaultRun) => {
      const { queryClient } = await renderFailedRefresh(defaultRun);

      // The notice's read of the default run has settled, so the fallback is not just pending.
      await waitFor(() => {
        const run = queryClient.getQueryState(queryKeys.runs.latest);
        expect(['success', 'error']).toContain(run?.status);
        expect(run?.fetchStatus).toBe('idle');
      });
      const status = screen.getByRole('status');
      const line = lastLoadedLine(queryClient, 'UTC');
      expect(line).toMatch(/ UTC\.$/);
      expect(within(status).getByText(line)).toBeTruthy();
      expect(within(status).getByText(REFRESH_FAILURE_MESSAGE)).toBeTruthy();
      expect(screen.getByRole('region', { name: 'Analysis window' })).toBeTruthy();
    });

    it('drops the refresh notice once a later refresh succeeds', async () => {
      const { api, queryClient } = await renderFailedRefresh(
        json(200, makeRun({ display_timezone: 'America/New_York' })),
      );
      await within(screen.getByRole('status')).findByText('Settings could not be refreshed');

      const refreshedHash = 'f'.repeat(64);
      api.use({ 'GET /config': json(200, makeConfig({ config_hash: refreshedHash })) });
      await act(async () => {
        await queryClient.refetchQueries({ queryKey: queryKeys.config });
      });

      await waitFor(() => {
        expect(screen.queryByText('Settings could not be refreshed')).toBeNull();
      });
      expect(screen.queryByText(REFRESH_FAILURE_MESSAGE)).toBeNull();
      expect(screen.getByRole('status').childNodes).toHaveLength(0);
      expect(
        screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent),
      ).toEqual(SECTION_TITLES);
      const hashSection = screen.getByRole('region', { name: 'Configuration hash' });
      expect(within(hashSection).getByText(refreshedHash)).toBeTruthy();
      expect(api.calls.filter((call) => call.path === '/config')).toHaveLength(3);
    });
  });
});
