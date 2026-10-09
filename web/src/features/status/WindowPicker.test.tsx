/**
 * Component tests for `WindowPicker`, the analysis-window picker of the `/actions` status bar
 * (AAP 0.5.5, Window picker), and for its exported calendar helper `presetWindow`.
 *
 * The picker offers each window of `window.presets` from `GET /config`, marks the preset equal
 * to `window.default_days` "(default)", and offers a custom range. The default preset opens the
 * latest default run at `/actions` and posts nothing; any other preset, and a custom range,
 * posts `POST /runs` (AAP 0.5.3) and opens the created run at `/actions?run=<id>`. A 409
 * `run_in_progress` reads "Another analysis is running. Try again shortly."; a 422 and every
 * other API error show the API's own message. Failed reads of `GET /runs/latest` and
 * `GET /runs?kind=custom` are stated with a Retry button, and recent custom runs can be reopened
 * by id. No window length is written into the client: every preset, the default and the longest
 * span come from the configuration, and window ends come from runs.
 *
 * Requests are answered by `../../test/mockApi`, so the real client and TanStack Query hooks
 * run; payloads come from `../../test/fixtures`; navigation is read from the test router's
 * `router.state.location`. `mockApi` is imported before `../../test/render` and Testing Library,
 * so its unmatched-request check runs after Testing Library's cleanup.
 */
import { apiError, html, json, mockApi } from '../../test/mockApi';
import type { ApiMock, MockHandlers, MockResponse, RecordedCall } from '../../test/mockApi';
import { renderWithProviders } from '../../test/render';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { queryKeys } from '../../api/queries';
import type { ConfigView, Run } from '../../api/types';
import { CUSTOM_RUN_ID, makeConfig, makeRun } from '../../test/fixtures';
import { WindowPicker, presetWindow } from './WindowPicker';

// ---------------------------------------------------------------------------------------------
// Fixtures and helpers
// ---------------------------------------------------------------------------------------------

/** The 90-day custom run that `POST /runs` creates for the 90-day preset of the fixture window. */
const customRun = makeRun({
  id: CUSTOM_RUN_ID,
  kind: 'custom',
  trigger: 'manual',
  window_start: '2026-07-08',
  window_end: '2026-10-05',
  days: 90,
});

/** A request whose answer the test releases when it chooses. */
interface Deferred {
  /** The handler that holds every request until {@link Deferred.resolve} is called. */
  handler: () => Promise<MockResponse>;
  /** Answers every held request, and every later one, with `response`. */
  resolve: (response: MockResponse) => void;
}

/** A {@link Deferred} handler. */
function deferred(): Deferred {
  let answer: MockResponse | undefined;
  const waiting: ((response: MockResponse) => void)[] = [];
  return {
    handler: () =>
      answer === undefined
        ? new Promise<MockResponse>((resolve) => {
            waiting.push(resolve);
          })
        : Promise.resolve(answer),
    resolve: (response) => {
      answer = response;
      for (const release of waiting.splice(0)) {
        release(response);
      }
    },
  };
}

/**
 * Stubs the picker's reads with the fixture world: the shipped configuration, the latest
 * default run, no custom run and a `POST /runs` that creates {@link customRun}. `overrides`
 * replaces any of them.
 */
function stubApi(overrides: MockHandlers = {}): ApiMock {
  return mockApi({
    'GET /config': json(200, makeConfig()),
    'GET /runs/latest': json(200, makeRun()),
    'GET /runs': json(200, []),
    'POST /runs': json(201, customRun),
    ...overrides,
  });
}

/** Renders the picker for `run` at `path`. */
function renderPicker(run: Run = makeRun(), path = '/actions') {
  return renderWithProviders(<WindowPicker run={run} />, { path });
}

/** A preset or picker button by its accessible name. */
function button(name: string): HTMLElement {
  return screen.getByRole('button', { name });
}

/** Waits until the picker's controls are on screen. */
async function presetsShown(): Promise<void> {
  await waitFor(() => {
    expect(screen.getByRole('group', { name: 'Analysis window' })).toBeTruthy();
  });
}

/** The accessible names of the preset group's buttons, in order. */
function presetNames(): string[] {
  const group = screen.getByRole('group', { name: 'Analysis window' });
  return within(group)
    .getAllByRole('button')
    .map((element) => element.textContent ?? '');
}

/** The `POST /runs` requests made so far. */
function posts(api: ApiMock): RecordedCall[] {
  return api.calls.filter((call) => call.method === 'POST' && call.path === '/runs');
}

/** The picker's polite status region, which holds every warning it states. */
function noticeRegion(): HTMLElement {
  const regions = screen
    .getAllByRole('status')
    .filter((region) => !region.classList.contains('ds-visually-hidden'));
  const region = regions.find((candidate) => candidate.querySelector('.ds-banner') !== null);
  if (region === undefined) {
    throw new Error('No warning is shown in the picker’s status region.');
  }
  return region;
}

/** The titles of the warnings in the status region, in order. */
function noticeTitles(): string[] {
  return Array.from(noticeRegion().querySelectorAll('.ds-banner__title'), (title) =>
    (title.textContent ?? '').trim(),
  );
}

/**
 * Leaves focus on the page body, as a browser does once the focused control is disabled (the
 * focus fixup rule). jsdom implements no fixup, and its `blur()` ignores a disabled element, so
 * focus is moved to a temporary focusable element that is then removed, which jsdom does
 * follow by returning focus to the body.
 */
function dropFocusToBody(): void {
  const holder = document.createElement('span');
  holder.tabIndex = -1;
  document.body.append(holder);
  holder.focus();
  holder.remove();
  expect(document.activeElement).toBe(document.body);
}

/** The configuration with its window settings replaced. */
function configWithWindow(settings: Partial<ConfigView['window']>): ConfigView {
  const config = makeConfig();
  return { ...config, window: { ...config.window, ...settings } };
}

// ---------------------------------------------------------------------------------------------
// presetWindow
// ---------------------------------------------------------------------------------------------

describe('presetWindow', () => {
  it('returns the window of `days` calendar dates ending on the given end', () => {
    expect(presetWindow(7, '2026-10-01')).toEqual({
      window_start: '2026-09-25',
      window_end: '2026-10-01',
    });
    expect(presetWindow(90, '2026-10-05')).toEqual({
      window_start: '2026-07-08',
      window_end: '2026-10-05',
    });
  });

  it('gives a one-day window that starts and ends on the same date', () => {
    expect(presetWindow(1, '2026-10-01')).toEqual({
      window_start: '2026-10-01',
      window_end: '2026-10-01',
    });
    expect(presetWindow(1, '0001-01-01')).toEqual({
      window_start: '0001-01-01',
      window_end: '0001-01-01',
    });
  });

  it('counts calendar dates across a leap day and a year end', () => {
    expect(presetWindow(60, '2024-03-01').window_start).toBe('2024-01-02');
    expect(presetWindow(14, '2027-01-05').window_start).toBe('2026-12-23');
  });

  it.each([0, -3, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    'rejects %s as a window length',
    (days) => {
      expect(() => presetWindow(days, '2026-10-01')).toThrow(RangeError);
      expect(() => presetWindow(days, '2026-10-01')).toThrow(/whole number of days, 1 or more/u);
    },
  );

  it.each(['2026-02-30', '2026/10/01', '2026-10-1', '0000-01-01', ''])(
    'rejects "%s" as a window end',
    (end) => {
      expect(() => presetWindow(7, end)).toThrow(RangeError);
      expect(() => presetWindow(7, end)).toThrow(/must be a calendar date \(YYYY-MM-DD\)/u);
    },
  );

  it('rejects a window that would start before the year 1', () => {
    expect(() => presetWindow(2, '0001-01-01')).toThrow(
      'A window of 2 days ending 0001-01-01 would start before the year 1.',
    );
  });
});

// ---------------------------------------------------------------------------------------------
// WindowPicker
// ---------------------------------------------------------------------------------------------

describe('WindowPicker', () => {
  describe('presets from GET /config', () => {
    it('shows a spinner until GET /config answers, then every preset with the default marked', async () => {
      const config = deferred();
      stubApi({ 'GET /config': config.handler });
      await renderPicker();

      expect(screen.getByText('Loading windows')).toBeTruthy();
      expect(screen.queryByRole('group', { name: 'Analysis window' })).toBeNull();

      config.resolve(json(200, makeConfig()));
      await presetsShown();

      expect(screen.queryByText('Loading windows')).toBeNull();
      expect(presetNames()).toEqual([
        '14 days',
        '28 days (default)',
        '56 days',
        '90 days',
        'Custom range',
      ]);
      // The run on screen is the latest default run, so the default preset is pressed.
      expect(button('28 days (default)').getAttribute('aria-pressed')).toBe('true');
      for (const name of ['14 days', '56 days', '90 days']) {
        expect(button(name).getAttribute('aria-pressed')).toBe('false');
      }
    });

    it('shows a spinner until GET /runs/latest first answers', async () => {
      const latest = deferred();
      const api = stubApi({ 'GET /runs/latest': latest.handler });
      const { queryClient } = await renderPicker();

      // The configuration has answered; only the default run holds the picker back.
      await waitFor(() => {
        expect(queryClient.getQueryData(queryKeys.config)).toBeDefined();
      });
      expect(api.calls.some((call) => call.path === '/runs/latest')).toBe(true);
      expect(screen.getByText('Loading windows')).toBeTruthy();
      expect(screen.queryByRole('group', { name: 'Analysis window' })).toBeNull();

      latest.resolve(json(200, makeRun()));
      await presetsShown();
      expect(screen.queryByText('Loading windows')).toBeNull();
    });

    it('takes the presets and the default from the configuration, with no window length of its own', async () => {
      const run = makeRun({ window_start: '2026-09-15', days: 21, default_days: 21 });
      stubApi({
        'GET /config': json(
          200,
          configWithWindow({ default_days: 21, presets: [7, 21, 30], max_days: 60 }),
        ),
        'GET /runs/latest': json(200, run),
      });
      await renderPicker(run);
      await presetsShown();

      expect(presetNames()).toEqual(['7 days', '21 days (default)', '30 days', 'Custom range']);
      expect(button('21 days (default)').getAttribute('aria-pressed')).toBe('true');
      expect(screen.queryByRole('button', { name: /^28 days/u })).toBeNull();
    });

    it('states a configuration that cannot be read, and renders no picker', async () => {
      stubApi({
        'GET /config': apiError(
          500,
          'config_error',
          'config/thresholds.yaml: window.presets must include window.default_days',
        ),
      });
      await renderPicker();

      await waitFor(() => {
        expect(screen.getByText('Window presets unavailable')).toBeTruthy();
      });
      expect(
        screen.getByText('config/thresholds.yaml: window.presets must include window.default_days'),
      ).toBeTruthy();
      expect(screen.queryByRole('group', { name: 'Analysis window' })).toBeNull();
      expect(screen.queryByRole('button')).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    });

    it('states a configuration without window settings, and renders no picker', async () => {
      stubApi({ 'GET /config': json(200, { ...makeConfig(), window: undefined }) });
      await renderPicker();

      await waitFor(() => {
        expect(screen.getByText('Window presets unavailable')).toBeTruthy();
      });
      expect(screen.getByText('The configuration carried no window settings.')).toBeTruthy();
      expect(screen.queryByRole('button')).toBeNull();
    });
  });

  describe('choosing a preset', () => {
    it('opens the latest default run for the default preset and posts nothing', async () => {
      const api = stubApi({ 'GET /runs': json(200, [customRun]) });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();
      // The custom 90-day run ends on the default window end, so its preset is pressed.
      expect(button('90 days').getAttribute('aria-pressed')).toBe('true');
      expect(button('28 days (default)').getAttribute('aria-pressed')).toBe('false');

      fireEvent.click(button('28 days (default)'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(router.state.location.pathname).toBe('/actions');
      expect(posts(api)).toHaveLength(0);
    });

    it('computes any other preset ending on the default window end, then opens the run by id', async () => {
      const created = deferred();
      const api = stubApi({
        'GET /runs': json(200, [customRun]),
        'POST /runs': created.handler,
      });
      const { router } = await renderPicker();
      await presetsShown();
      await waitFor(() => {
        expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBeTruthy();
      });

      fireEvent.click(button('90 days'));

      await waitFor(() => {
        expect(posts(api)).toHaveLength(1);
      });
      const [post] = posts(api);
      expect(post?.body).toEqual({ window_start: '2026-07-08', window_end: '2026-10-05' });
      expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');

      // While the run computes, every control is disabled and a spinner says so.
      await waitFor(() => {
        expect(screen.getByText('Computing custom window')).toBeTruthy();
      });
      for (const name of ['14 days', '28 days (default)', '56 days', '90 days', 'Custom range']) {
        expect(button(name).hasAttribute('disabled')).toBe(true);
      }
      expect(
        screen.getByRole('combobox', { name: 'Reopen a custom window' }).hasAttribute('disabled'),
      ).toBe(true);
      expect(router.state.location.search).toEqual({});

      created.resolve(json(201, customRun));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      expect(router.state.location.pathname).toBe('/actions');
      await waitFor(() => {
        expect(screen.queryByText('Computing custom window')).toBeNull();
      });
      expect(button('90 days').hasAttribute('disabled')).toBe(false);
    });

    it('ends every preset on the latest default run, not on an older run on screen', async () => {
      const older = makeRun({
        id: 205,
        kind: 'custom',
        window_start: '2026-09-17',
        window_end: '2026-09-30',
        days: 14,
      });
      const api = stubApi({ 'POST /runs': json(201, { ...customRun, id: 206 }) });
      const { router } = await renderPicker(older, '/actions?run=205');
      await presetsShown();

      // The run on screen does not end on the default window end: it shows as a custom range.
      for (const name of ['14 days', '28 days (default)', '56 days', '90 days']) {
        expect(button(name).getAttribute('aria-pressed')).toBe('false');
      }
      expect(button('Custom range').getAttribute('aria-expanded')).toBe('true');
      expect(screen.getByLabelText<HTMLInputElement>(/^Start date/u).value).toBe('2026-09-17');
      expect(screen.getByLabelText<HTMLInputElement>(/^End date/u).value).toBe('2026-09-30');

      fireEvent.click(button('14 days'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: 206 });
      });
      expect(posts(api).map((call) => call.body)).toEqual([
        { window_start: '2026-09-22', window_end: '2026-10-05' },
      ]);
    });

    it('ends the presets on the run on screen while no default run has succeeded yet', async () => {
      const shown = makeRun({
        id: 207,
        kind: 'custom',
        window_start: '2026-09-20',
        window_end: '2026-10-03',
        days: 14,
      });
      const api = stubApi({
        'GET /runs/latest': apiError(404, 'not_found', 'No analysis run yet'),
      });
      const { router } = await renderPicker(shown, '/actions?run=207');
      await presetsShown();

      // "No default run yet" is not a failure: no warning, and the run on screen ends the presets.
      expect(screen.queryByText('Could not read the latest default run')).toBeNull();
      expect(button('14 days').getAttribute('aria-pressed')).toBe('true');

      fireEvent.click(button('56 days'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      expect(posts(api).map((call) => call.body)).toEqual([
        { window_start: '2026-08-09', window_end: '2026-10-03' },
      ]);
    });

    it('refuses a preset that would start before the year 1, without posting', async () => {
      const earliest = makeRun({
        id: 208,
        kind: 'custom',
        window_start: '0001-01-01',
        window_end: '0001-01-01',
        days: 1,
      });
      const api = stubApi({
        'GET /runs/latest': apiError(404, 'not_found', 'No analysis run yet'),
      });
      const { router } = await renderPicker(earliest, '/actions?run=208');
      await presetsShown();

      fireEvent.click(button('14 days'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'A window of 14 days ending 0001-01-01 would start before the year 1.',
        ]);
      });
      expect(posts(api)).toHaveLength(0);
      expect(router.state.location.search).toEqual({ run: 208 });

      // The next choice clears the message.
      fireEvent.click(button('28 days (default)'));
      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(
        screen.queryByText('A window of 14 days ending 0001-01-01 would start before the year 1.'),
      ).toBeNull();
    });
  });

  describe('choosing a custom range', () => {
    it('posts the range and opens the created run by id', async () => {
      const api = stubApi();
      const { router } = await renderPicker();
      await presetsShown();

      const toggle = button('Custom range');
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      fireEvent.click(toggle);
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
      const start = screen.getByLabelText<HTMLInputElement>(/^Start date/u);
      const end = screen.getByLabelText<HTMLInputElement>(/^End date/u);
      // The latest selectable date is the default window end.
      expect(start.getAttribute('max')).toBe('2026-10-05');
      expect(end.getAttribute('max')).toBe('2026-10-05');

      fireEvent.change(start, { target: { value: '2026-08-01' } });
      fireEvent.change(end, { target: { value: '2026-08-30' } });
      fireEvent.click(button('Apply'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      const [post] = posts(api);
      expect(posts(api)).toHaveLength(1);
      expect(post?.body).toEqual({ window_start: '2026-08-01', window_end: '2026-08-30' });
      expect(post?.headers.get('X-Requested-With')).toBe('ads-engine');
    });

    it('holds a range to the default window end and the configured longest span, posting nothing', async () => {
      const api = stubApi({ 'GET /config': json(200, configWithWindow({ max_days: 30 })) });
      await renderPicker();
      await presetsShown();

      fireEvent.click(button('Custom range'));
      const start = screen.getByLabelText<HTMLInputElement>(/^Start date/u);
      const end = screen.getByLabelText<HTMLInputElement>(/^End date/u);

      fireEvent.change(start, { target: { value: '2026-10-01' } });
      fireEvent.change(end, { target: { value: '2026-10-06' } });
      fireEvent.click(button('Apply'));
      await waitFor(() => {
        expect(screen.getByText('End date must be on or before 2026-10-05')).toBeTruthy();
      });

      fireEvent.change(start, { target: { value: '2026-08-01' } });
      fireEvent.change(end, { target: { value: '2026-09-30' } });
      await waitFor(() => {
        expect(screen.getByText('Range must be at most 30 days')).toBeTruthy();
      });
      expect(posts(api)).toHaveLength(0);
    });
  });

  describe('a failed computation', () => {
    it('reads "Another analysis is running" for 409 run_in_progress, stays put and returns focus', async () => {
      const created = deferred();
      const api = stubApi({ 'POST /runs': created.handler });
      const { router } = await renderPicker();
      await presetsShown();

      const preset = button('56 days');
      preset.focus();
      fireEvent.click(preset);
      await waitFor(() => {
        expect(preset.hasAttribute('disabled')).toBe(true);
      });
      // A browser moves focus off a control once it is disabled; jsdom does not.
      dropFocusToBody();

      created.resolve(apiError(409, 'run_in_progress', 'An analysis is already running.'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Another analysis is running. Try again shortly.']);
      });
      expect(screen.queryByText('An analysis is already running.')).toBeNull();
      // A polite status message, never an alert (AAP 0.5.5 reserves alert for discrepancies).
      expect(noticeRegion().getAttribute('role')).toBe('status');
      expect(screen.queryByRole('alert')).toBeNull();
      expect(posts(api).map((call) => call.body)).toEqual([
        { window_start: '2026-08-11', window_end: '2026-10-05' },
      ]);
      expect(router.state.location.search).toEqual({});
      // The picker is usable again, and focus is back on the control that started the run.
      await waitFor(() => {
        expect(preset.hasAttribute('disabled')).toBe(false);
      });
      expect(document.activeElement).toBe(preset);
    });

    it('shows the API’s own message for a 422, until the next choice clears it', async () => {
      stubApi({
        'POST /runs': apiError(
          422,
          'validation_error',
          'window_end must be on or before the default window end 2026-10-05',
        ),
      });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();

      fireEvent.click(button('14 days'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'window_end must be on or before the default window end 2026-10-05',
        ]);
      });
      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });

      fireEvent.click(button('28 days (default)'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({});
      });
      expect(
        screen.queryByText('window_end must be on or before the default window end 2026-10-05'),
      ).toBeNull();
    });

    it('shows the client’s wording of any other failure, until the next computation clears it', async () => {
      const api = stubApi({ 'POST /runs': html(502, '<html><body>Bad Gateway</body></html>') });
      const { router } = await renderPicker();
      await presetsShown();

      fireEvent.click(button('90 days'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'The server is temporarily unavailable. Try again shortly.',
        ]);
      });

      api.use({ 'POST /runs': json(201, customRun) });
      fireEvent.click(button('90 days'));

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
      });
      expect(
        screen.queryByText('The server is temporarily unavailable. Try again shortly.'),
      ).toBeNull();
      expect(posts(api)).toHaveLength(2);
    });
  });

  describe('failed reads', () => {
    it('states a failed GET /runs/latest with a Retry that reads it again', async () => {
      const api = stubApi({
        'GET /runs/latest': apiError(500, 'internal_error', 'The database is unavailable.'),
      });
      await renderPicker();

      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Could not read the latest default run']);
      });
      const region = noticeRegion();
      expect(within(region).getByText('The database is unavailable.')).toBeTruthy();
      expect(
        within(region).getByText(
          'Until the latest default run can be read, preset windows end on 2026-10-05.',
        ),
      ).toBeTruthy();
      // The picker stays usable, on the run on screen's window end.
      expect(button('28 days (default)').getAttribute('aria-pressed')).toBe('true');

      // A retry that fails again keeps the warning, with the new reason, and focus on Retry.
      const latest = deferred();
      api.use({ 'GET /runs/latest': latest.handler });
      const retry = button('Retry reading the latest default run');
      expect(retry.textContent).toBe('Retry');
      retry.focus();
      fireEvent.click(retry);

      await waitFor(() => {
        expect(retry.getAttribute('aria-busy')).toBe('true');
      });
      expect(retry.hasAttribute('disabled')).toBe(true);
      // The warning is kept in place while the read runs again.
      expect(noticeTitles()).toEqual(['Could not read the latest default run']);
      expect(retry.isConnected).toBe(true);
      // A browser moves focus off the button once it is disabled; jsdom does not.
      dropFocusToBody();

      latest.resolve(apiError(500, 'internal_error', 'The database is still unavailable.'));
      await waitFor(() => {
        expect(within(noticeRegion()).getByText('The database is still unavailable.')).toBeTruthy();
      });
      await waitFor(() => {
        expect(retry.getAttribute('aria-busy')).toBeNull();
      });
      expect(document.activeElement).toBe(retry);

      // A retry that succeeds removes the warning.
      api.use({ 'GET /runs/latest': json(200, makeRun()) });
      fireEvent.click(retry);

      await waitFor(() => {
        expect(screen.queryByText('Could not read the latest default run')).toBeNull();
      });
      expect(
        api.calls.filter((call) => call.method === 'GET' && call.path === '/runs/latest'),
      ).toHaveLength(3);
    });

    it('states a failed GET /runs?kind=custom with a Retry, and lists the runs once it succeeds', async () => {
      const api = stubApi({
        'GET /runs': apiError(500, 'internal_error', 'Could not list runs.'),
      });
      await renderPicker();
      await presetsShown();

      await waitFor(() => {
        expect(noticeTitles()).toEqual(['Could not load recent custom windows']);
      });
      expect(within(noticeRegion()).getByText('Could not list runs.')).toBeTruthy();
      expect(screen.queryByRole('combobox', { name: 'Reopen a custom window' })).toBeNull();
      const customCall = api.calls.find((call) => call.method === 'GET' && call.path === '/runs');
      expect(customCall?.search.get('kind')).toBe('custom');

      api.use({ 'GET /runs': json(200, [customRun]) });
      fireEvent.click(button('Retry loading recent custom windows'));

      await waitFor(() => {
        expect(screen.getByRole('combobox', { name: 'Reopen a custom window' })).toBeTruthy();
      });
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
    });

    it('states the computation first, then the default-run read, then the custom-runs read', async () => {
      stubApi({
        'GET /runs/latest': apiError(500, 'internal_error', 'The database is unavailable.'),
        'GET /runs': apiError(503, 'unavailable', 'The API is restarting.'),
        'POST /runs': apiError(409, 'run_in_progress', 'An analysis is already running.'),
      });
      await renderPicker();
      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'Could not read the latest default run',
          'Could not load recent custom windows',
        ]);
      });

      fireEvent.click(button('14 days'));

      await waitFor(() => {
        expect(noticeTitles()).toEqual([
          'Another analysis is running. Try again shortly.',
          'Could not read the latest default run',
          'Could not load recent custom windows',
        ]);
      });
      // Every warning sits in the one polite region; none is an alert.
      expect(screen.queryByRole('alert')).toBeNull();
    });
  });

  describe('reopening a custom run', () => {
    /** A succeeded 30-day custom run, newer than {@link customRun}. */
    const september = makeRun({
      id: 203,
      kind: 'custom',
      trigger: 'manual',
      window_start: '2026-09-01',
      window_end: '2026-09-30',
      days: 30,
    });
    /** A failed custom run, which cannot be shown and so cannot be reopened. */
    const failed = makeRun({
      id: 204,
      kind: 'custom',
      trigger: 'manual',
      window_start: '2026-08-01',
      window_end: '2026-08-14',
      days: 14,
      status: 'failed',
    });

    it('lists only succeeded custom runs, newest first, and opens the chosen one by id', async () => {
      const api = stubApi({ 'GET /runs': json(200, [september, failed, customRun]) });
      const { router } = await renderPicker();
      await presetsShown();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(Array.from(select.options, (option) => option.textContent)).toEqual([
        'Choose a custom window',
        '2026-09-01 – 2026-09-30 · 30 days',
        '2026-07-08 – 2026-10-05 · 90 days',
      ]);
      // The default run is on screen, so no custom run is chosen.
      expect(select.value).toBe('');

      fireEvent.change(select, { target: { value: '203' } });

      await waitFor(() => {
        expect(router.state.location.search).toEqual({ run: 203 });
      });
      expect(posts(api)).toHaveLength(0);
    });

    it('selects the custom run on screen, and ignores the empty choice', async () => {
      stubApi({ 'GET /runs': json(200, [september, customRun]) });
      const { router } = await renderPicker(customRun, `/actions?run=${String(CUSTOM_RUN_ID)}`);
      await presetsShown();

      const select = await screen.findByRole<HTMLSelectElement>('combobox', {
        name: 'Reopen a custom window',
      });
      expect(select.value).toBe(String(CUSTOM_RUN_ID));

      fireEvent.change(select, { target: { value: '' } });

      expect(router.state.location.search).toEqual({ run: CUSTOM_RUN_ID });
    });

    it('hides the select while no succeeded custom run exists', async () => {
      const api = stubApi({ 'GET /runs': json(200, [failed]) });
      await renderPicker();
      await presetsShown();

      await waitFor(() => {
        expect(api.calls.some((call) => call.method === 'GET' && call.path === '/runs')).toBe(
          true,
        );
      });
      await waitFor(() => {
        expect(screen.queryByText('Loading windows')).toBeNull();
      });
      expect(screen.queryByRole('combobox', { name: 'Reopen a custom window' })).toBeNull();
      expect(screen.queryByText('Could not load recent custom windows')).toBeNull();
    });
  });
});
